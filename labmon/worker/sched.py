"""worker 资源估计、时长估计、记账与准入（place / admit）、公平份额与 CPU 诊断。"""
import glob
import json
import math
import os
import statistics

from . import common as _C
from . import pure as _P
from . import queue as _Q
from . import measure as _M
from . import deps as _DP
from .pure import person_use

def card_share(ngpu, vram, excl, card, ncards):
    """一个任务占几张卡（公平份额按它记 GPU 用量）：独占卡算整卡，独占整机算这台全部的卡，同卡并行的按显存估计值（GiB）
    占卡总显存（card，GiB；不知道时算整卡）的比例；不用卡的为 0。"""
    if excl == "host":
        return float(max(ngpu, ncards))
    if not ngpu:
        return 0.0
    return float(ngpu) if excl == "gpu" or not card else ngpu * min(1.0, vram / card)


def usage(rows, t, hl):
    """各项目衰减后的 GPU 用量（卡·时）：rows [(项目, 卡数, 开始, 结束)]，t 之前跑的部分按半衰期 hl（秒）衰减；
    结束在 t 之后的（在跑的、刚放行的）把预计还要跑的整段照记（开跑先记一笔，免得几台主机同时偏向同一个项目）；结束为 None 的只记已跑的。"""
    k, out = math.log(2) / hl, {}
    for p, n, a, b in rows:
        b = t if b is None else b
        e = min(b, t)
        past = (math.exp(-k * (t - e)) - math.exp(-k * (t - a))) / k if e > a else 0.0
        out[p] = out.get(p, 0.0) + n * (past + max(0.0, b - max(a, t))) / 3600
    return out


def fits_before(t, dur_min, until, m=_C.DUR_MARGIN):
    """回填与截止：从 t 起跑、预计 dur_min 分钟（再乘 m）能在 until 前跑完。不知道时长或 until 的不算。
    回填按预计时长 × DUR_MARGIN；截止前的准入传已含余量的 cut（duration 的 cut，cut_min）、m=1（#139）。"""
    return until is not None and dur_min is not None and t + dur_min * m * 60 <= until


def cut_min(x):
    """place 认的任务 x 截止前的准入用的时长（分钟，已含余量；job_spec 的 cut）：不知道为 None。"""
    return x["cut"] if "cut" in x else x["dur"] and x["dur"] * _C.DUR_MARGIN


def acct_job(x, cs, t, t1, cap):
    """开跑的（推演里）或留着资源的（成组成员，place）任务在快照 jobs 里的记账：显存按估计值记在选中的卡 cs 上（MB）、内存（GiB）、核数（cpu_give 封顶到 cap）。
    t、t1 是开始与预计结束（不知道为 None）。simulate 的 launch 与 place 的成组留资源共用。"""
    return {"key": x["key"], "lane": x.get("lane") or x["key"], "name": x["name"], "project": x["project"], "t0": t, "t1": t1, "gpus": [c["uuid"] for c in cs],
            "vram": {c["uuid"]: x["vram"] * 1024 for c in cs}, "ram": x["ram"], "cpu": float(cpu_give(x["cpus"], cap)),
            "excl": x["excl"], "tag": x["tag"], "hold": x["hold"], "ngpu": x["ngpu"], "use": {}, "cards": x["cards"]}


def need_mb(vram_gib):
    """一张卡上要有这么多空闲显存（MB）才放：估计值另留 1 GiB。准入、推演、放不下的判断与 fit 共用这一处。"""
    return (vram_gib + 1) * 1024


def card_need(x, c):
    """请求 x 在卡 c 上要有多少空闲显存（MB）才放（place、nofit 用）：need_mb；按历史估的（不是清单写的提示值）超过这张卡腾空后能给的
    （总显存 − 给对方留的 − 1 GiB，1 GiB 盖过驱动保留的几百 MB），按整卡算：卡上没有别的任务就放，不因为 ×1.1 的余量永远排不上
    （一次 OOM 的峰值就是整张卡）；配置没改的会再 OOM 一次、记失败。运行中的记账照估计值（整卡以上），这张卡上不再放别的。"""
    n = need_mb(x["vram"])
    return n if x["src"] == "提示值" or not c["total"] else min(n, c["total"] - c["keep"] - 1024)


def ram_need(x, S):
    """请求 x 放行要记多少内存（GiB）与原因里写的来源（place、nofit、fit 用；与 card_need、cpu_need 同理）：estimate 给的 ram；
    按历史估的（不是清单写的 ram=，含 OOM 后的下限）超过 labmon 全停时能给的减去余量（room − margin）时按这整份算：labmon 别的任务都没在跑就放，
    不因为翻倍后比容器还大永远“放不下”（#62）；来源里写明估计与封顶后的量。运行中的记账照估计值，跑着时不再放别的。读不到上限、整份不到 0 时不封顶。"""
    n, src = x["ram"], x["ram_src"]
    if src == "提示值" or S["room"] is None:
        return n, src
    m = S["room"] - S["margin"]
    return (n, src) if n <= m or m <= 0 else (m, "%s；估计 %.1f GiB，封顶为 %.1f GiB（labmon 全停时能给的减去余量）" % (src, n, m))


def ram_margin(limit_gib):
    """放行时容器内存另留的余量（GiB）：上限的 5%，至少 8。"""
    return max(8.0, 0.05 * limit_gib)


def card_free(S):
    """快照里各卡此刻还能给新任务的显存 {UUID: MB}：卡的 free 减去在跑任务的记账。"""
    out = {c["uuid"]: c["free"] for c in S["cards"]}
    for j in S["jobs"]:
        for u, mb in j["vram"].items():
            if u in out:
                out[u] -= mb
    return out


def ram_free(S):
    """快照里容器内存还能给新任务多少（GiB）：labmon 任务全停时的余量减去在跑任务的记账；读不到上限为 None。"""
    return None if S["room"] is None else S["room"] - sum(j["ram"] for j in S["jobs"])


def cpu_free(S):
    """快照里还能给新任务多少核（同 ram_free）：labmon 任务全停时还能给的（cpu_room）减去在跑任务的记账；读不到配额为 None（不记 CPU）。"""
    return None if S["cpu_room"] is None else S["cpu_room"] - sum(j["cpu"] for j in S["jobs"])


def cpu_need(x, S):
    """请求 x 放行要几个核与原因里写的来源（place、nofit、fit 用）：estimate 给的 cpus；按历史估的（不是 cpus= 写的）超过 labmon 全停时能给的
    （cpu_room，已扣 labmon 之外的与给对方留的），按这整份算，来源里写明估计与封顶后的核数（同 card_need：一次吃满配额的峰值 ×1.1 比配额还大，
    不封顶就永远排不上；没有别的 labmon 任务时照放）。写了 cpus=0 的（与占位任务）是 0：不记账，CPU 再紧也不挡它。"""
    n, src = x["cpus"], x["cpu_src"]
    if src == "提示值" or S["cpu_room"] is None:
        return n, src
    m = max(1, int(S["cpu_room"] + 1e-6))
    return (n, src) if n <= m else (m, "%s；估计 %d 核，封顶为 %d 核（labmon 全停时能给的）" % (src, n, m))


def cpu_held(S, cores, n):
    """因记账不够放不下时：原因代码与“核被谁记着”的说明（place、fit 用）。返回 (code, held 文字)。
    还没回收的在跑任务（开跑不满 CPU_WARM_S，记的是估计）记账比此刻实测多出的核（多 0.5 核以上的才列）加回去就够 n 核的是 cpu_est
    （记账挡住、实际空着，满 10 分钟会回收，要告警），否则 cpu（核真被用着，正常排队）。已回收的记账就是近期窗口核数的峰值，
    此刻在两次冒尖之间低于它也不算“记得多”（#86）。推演里开跑的任务没有实测，原因只要短的、也不告警，口径不影响。
    S 要 jobs[{cpu 记账核数, use}]、cpu_cap、cpu_room、kept。"""
    run = [j for j in S["jobs"] if j["cpu"] > 0]
    over = [j for j in run if (j.get("use") or {}).get("cores") is not None and not cpu_warm(j["use"])
            and j["cpu"] - j["use"]["cores"] >= 0.5]   # 还没量到核数的（刚开跑、推演里的）与已回收的不算
    spare = sum(j["cpu"] - j["use"]["cores"] for j in over)
    out = S["cpu_cap"] - S["cpu_room"]
    held = "：在跑 %d 个记 %g 核" % (len(run), round(sum(j["cpu"] for j in run), 1)) if run else ""
    if over:
        held += "，其中 %d 个记账比实测多（实测共 %.1f 核）" % (len(over), sum(j["use"]["cores"] for j in over))
    if out >= 0.1:
        held += "%slabmon 之外%s占 %.1f" % ("；" if held else "：", "与给对方的预留" if S["kept"] else "", out)
    return ("cpu_est" if over and cores + spare >= n - 1e-6 else "cpu"), held


def cpu_warm(u):
    """运行中任务的 CPU 记账已按窗口核数回收（#65）：从开始量窗口核数（use 的 pk0）起满 CPU_WARM_S、量到了 pks。
    Worker.acct（回收）与 cpu_held（只把没回收的算“记账比实测多”）共用这一个判断。"""
    return bool(u.get("pks") and u.get("t") and u.get("pk0") and u["t"] - u["pk0"] >= _C.CPU_WARM_S)


def cpu_threads(e):
    """估计 e（estimate 的结果）开跑时给几个核（导出 LABMON_CPUS、缺省线程数，判给多给少与超核都按它）：按实测收小了记账的（cpus_set，#91）
    仍给清单写的 cpus=，项目的线程数、DataLoader 进程数不因记账收小而变；其余同记账的 cpus。"""
    return e.get("cpus_set") or e.get("cpus", _C.CPU_DEFAULT)


def cpus_export(cpus, cap):
    """开跑时导出的 LABMON_CPUS（给这个任务的核数）：cpu_give 再至少 1。Worker.cpus_for 与判开跑核数给多给少（cpu_fit）共用。"""
    return max(1, cpu_give(cpus, cap))


def cpu_give(cpus, cap):
    """开跑后给任务的核数：估计（estimate 的 cpus）封顶到记账上限（cpu_cap，配额向下取整；读不到配额不封顶）。运行中的记账（Worker.acct、
    推演里开跑的）与导出的 LABMON_CPUS（再至少 1）都取它：历史是各主机一起看的，在核多的主机上量到的峰值可能比这台的配额还大，
    照原样导出等于又让任务按宿主机的核数开进程。"""
    return cpus if cap is None else min(cpus, cap)


def hold_until(S, cards, t):
    """为独占任务留着的卡（cards 为 None 是整台）预计何时腾空：上面 labmon 任务的预计结束取最晚的；这之前跑得完的任务可以回填。
    有一个时长不明的（t1 为 None）就不知道（None），不回填。占位任务与不用卡、不占 CPU 的任务不算。"""
    ts = [j["t1"] for j in S["jobs"] if not j["hold"] and not _C.idle_job(j) and (cards is None or set(j["gpus"]) & set(cards))]
    return None if None in ts else max(ts, default=t)


# 放不了的原因：同一个判定两种说法。真放行写全（给看板的等待表与事件），推演只要一句短的（plan 里 stuck 的“一直排不上，…”）。
WHY = {
    "nofit_ram": ("放不下（要的比容器内存还大）",
                  "放不下：要内存 %(ram).0f GiB（%(src)s，另留 %(margin).0f），这台容器上限 %(limit).0f GiB%(taken)s，labmon 的任务全停也不够；换内存大的主机或拆小"),
    "nofit_gpu_count": ("放不下（GPU 卡数不足：要 %(ngpu)d 张，只有 %(count)d 张）",
                        "放不下：要 %(ngpu)d 张 GPU，%(available)s；%(advice)s或减少用卡数"),
    "nofit_vram_count": ("放不下（显存够的卡数不足：要 %(ngpu)d 张，只有 %(count)d 张）",
                         "放不下：要 %(ngpu)d 张 GPU，每张要显存 %(vram).0f GiB（%(src)s，另留 1 GiB），只有 %(count)d 张满足；换有足够显存和卡数的主机或拆小"),
    "nofit_vram": ("放不下（要的比卡的显存还大）",
                   "放不下：要每张卡显存 %(vram).0f GiB（%(src)s，另留 1 GiB）× %(ngpu)d 张，这台的卡最大 %(max).0f GiB；换显存大的主机或拆小"),
    "cut": ("截止前跑不完", "截止前跑不完"),
    "tag": ("tag %(tag)s 已到上限", "tag %(tag)s 已有 %(n)d 个在跑（上限 %(lim)d）"),
    "xhost_run": ("等独占整机的任务", "独占整机的 %(name)s 在跑"),
    "xhost_hold": ("等独占整机的任务", "主机留给等腾空的独占任务（%(note)s）"),
    "xhost_theirs": ("独占整机：等%(who)s用完", "独占整机：%(who)s在用（%(names)s），等用完；不为它留主机，别的任务照常放"),
    "xhost_wait": ("独占整机：等主机腾空", "独占整机：等主机腾空（还在跑：%(names)s）"),
    "ram": ("内存不够", "内存不够：要 %(ram).0f GiB（%(src)s，另留 %(margin).0f），按记账只剩 %(left).0f GiB"),
    "nofit_cpu": ("放不下（要的核数比容器配额还多）",
                  "放不下：要 %(cpus)d 核（%(src)s），这台容器配额只有 %(cap)d 核；换配额大的主机或少开进程"),
    "cpu_other": ("CPU 不够（labmon 之外占着）",
                  "CPU 不够：要 %(cpus)d 核（%(src)s），配额 %(cap)d 核里 labmon 之外与给对方的预留此刻占 %(busy).1f 核，labmon 的任务全停也不够；"
                  "先等它们退下，一直占着再换主机或少开进程"),
    "cpu": ("CPU 不够", "CPU 不够：要 %(cpus)d 核（%(src)s），按记账只剩 %(left).1f 核（配额 %(cap)d%(held)s）"),
    "cpu_est": ("CPU 不够（记账比实测多）", "CPU 不够：要 %(cpus)d 核（%(src)s），按记账只剩 %(left).1f 核（配额 %(cap)d%(held)s）；"
                "实际空着的够，是在跑的记账比实测多，开跑满 %(warm)d 分钟后按实测回收"),
    "xgpu_none": ("独占：没有可用的卡", "独占：没有可用的卡"),
    "xgpu_theirs": ("独占：等%(who)s用完", "独占：%(who)s在用卡（%(names)s），等用完；不为它留卡，别的任务照常放"),
    "xgpu_nofit": ("独占：没有放得下的卡", "独占：没有放得下的卡（要显存 %(vram).0f GiB，另留 1 GiB%(kept)s）"),
    "xgpu_wait": ("独占：等卡腾空", "独占：等卡 %(idx)s 腾空（还在跑：%(names)s）"),
    "no_gpu": ("看不到 GPU（nvidia-smi 无输出）", "看不到 GPU（nvidia-smi 无输出）"),
    "gang_hold": ("成组 %(gang)s：已留好资源，等一起开跑",
                  "成组 %(gang)s：资源已留好，等同组的一起开跑（预计 %(at)s）；这之前跑得完的任务照样用这份资源"),
    "gang_unk": ("成组 %(gang)s：同组有成员不知道何时能放行", "成组 %(gang)s：放得下，但同组有成员说不出何时能放行，先不留资源"),
    "vram": ("显存不够%(ka_s)s", "没有可用显存够 %(vram).0f GiB 的卡（%(src)s，另留 1 GiB%(kept)s）%(held)s%(ka)s%(owner)s"),
}


def why_text(code, brief, **kw):
    return WHY[code][0] % kw if brief else WHY[code][1] % kw


def why_of(S, code, **kw):
    """place 里给出一个原因：按快照要短的还是全的拼文字（why_text），同时把原因代码记进快照的 code
    （Worker.place 据此认“因 CPU 被挡”，不按文字认；每次 place 开头清掉，缺文件、pre 这类不经它的为 None）。"""
    S["code"] = code
    return why_text(code, S["brief"], **kw)


def nofit(x, S):
    """labmon 的任务全停也放不下（估计值加余量超过容器内存、卡数或总显存不够、或要的核数超过全停时能给的）：返回原因。
    这类请求每轮照样再试，但不算“卡在资源上”，不会成为不让越过的那一个，免得整台主机的所有项目都停住。"""
    ram, rsrc = ram_need(x, S)
    if S["room"] is not None and ram + S["margin"] > S["room"]:
        return why_of(S, "nofit_ram", ram=ram, src=rsrc, margin=S["margin"], limit=S["limit"],
                      taken="，labmon 之外与给对方的预留占 %.0f" % (S["limit"] - S["room"]) if S["limit"] - S["room"] >= 1 else "")
    if x["ngpu"] > 0:
        cards = [c for c in S["cards"] if c["total"]]
        cs = [c for c in cards if x["allowed"] is None or c["i"] in x["allowed"]]
        if cs:
            ngpu = x["ngpu"]
            if len(cs) < ngpu:
                if len(cards) < ngpu:
                    count, available, advice = len(cards), f"这台只检测到 {len(cards)} 张", "换有足够卡数的主机"
                else:
                    count = len(cs)
                    available = f"这台检测到 {len(cards)} 张，清单限制后只有 {count} 张可选"
                    advice = "调整清单的选卡范围"
                return why_of(S, "nofit_gpu_count", ngpu=ngpu, count=count, available=available, advice=advice)
            enough = sum(c["total"] >= card_need(x, c) for c in cs)
            if enough < ngpu:
                if enough:
                    return why_of(S, "nofit_vram_count", vram=x["vram"], src=x["src"], ngpu=ngpu, count=enough)
                return why_of(S, "nofit_vram", vram=x["vram"], src=x["src"], ngpu=ngpu, max=max(c["total"] for c in cs) / 1024)
    n, src = cpu_need(x, S) if S["cpu_room"] is not None else (0, None)
    if n > 0 and n > S["cpu_room"] + 1e-6:      # 要的比配额还多才是真放不下；没超配额、只是 labmon 之外与给对方的预留占着的另说（cpu_other：等它们退下）
        return why_of(S, "nofit_cpu" if n > S["cpu_cap"] else "cpu_other", cpus=n, src=src, cap=S["cpu_cap"], busy=S["cpu_cap"] - S["cpu_room"])
    return None


def tag_full(x, S):
    """同 tag 在跑的已到上限（limits.json 的 tags；占位任务不算）：返回原因。"""
    lim = S["tags"].get(x["tag"] or "")
    n = sum(j["tag"] == x["tag"] for j in S["jobs"] if not j["hold"])
    if lim is not None and n >= lim:
        return why_of(S, "tag", tag=x["tag"], n=n, lim=lim)
    return None


def place(x, S, t, pre=None):
    """放不放一个请求 x：真放行（Worker.place）与推演（simulate）都调这一个，返回同 _place。在准入核心 _place 外面只多成组（#180）这一层：
    - 成组成员（x 的 gang，S["gangs"] 里有它的组）留着的资源（S["gholds"]：真放行就是 Worker.gang_hold，跨轮留着）当在跑的任务扣（acct_job 的记账），
      x 预计在留到的时刻（until）之前跑得完的不扣（回填，同独占留卡）；同组别的成员留的总扣：它们要同时占着，彼此不算回填；
    - 成员自己放得下时不开跑，留下这份资源（选中的卡与记账、留到组里预计一起开跑的时刻 gang_until），返回 (None, 原因, False)：不算卡在资源上、不成屏障；
      推演里 S["gfirst"] 记下它最早何时放得下（Worker.gang_eta，写进 gang.json 给同伴）。组里有成员说不出何时能放行（until 为 None）的不留；放不下的放掉。
      一起开跑由 Worker.gang_commit（真放行）与 simulate（推演）在一轮 admit 之后看；
    - 两组在两台主机上互相留着对方要的资源时（各自先留了一个成员），按 (项目, 组名) 的先后，排在后面的组留的让给前面的组，两台判法一样，不会互相卡死。"""
    gh, gk = S.setdefault("gholds", {}), (x["project"], x["gang"]) if x.get("gang") else None
    mine = gh.get(x["key"])
    held = [h for k, h in gh.items() if k != x["key"] and (h["gang"] == gk or not fits_before(t, x["dur"], h["until"]))]
    until = _DP.gang_until(S, x, t) if gk else None

    def try_(hs):
        S2 = dict(S, jobs=S["jobs"] + [h["job"] for h in hs]) if hs else S
        got = _place(x, S2, t, pre)
        S["code"] = S2["code"]
        return got
    cs, why, res = try_(held)
    low = [h for h in held if gk and until is not None and h["gang"] != gk and h["gang"] > gk]
    if cs is None and res and low:
        got = try_([h for h in held if h not in low])
        if got[0] is not None:
            for h in low:
                gh.pop(h["key"], None)
            cs, why, res = got
    if not gk:
        return cs, why, res
    if cs is None:
        gh.pop(x["key"], None)
        return cs, why, res
    S.setdefault("gfirst", {}).setdefault((x["key"], x["name"]), t)
    if until is None:
        gh.pop(x["key"], None)
        return None, why_of(S, "gang_unk", gang=x["gang"]), False
    gh[x["key"]] = {"key": x["key"], "gang": gk, "name": x["name"], "x": x, "cards": cs, "until": until, "seen": t,
                    "t": mine["t"] if mine and mine["name"] == x["name"] else t,
                    "job": acct_job(x, cs, t, until + x["dur"] * 60 if x["dur"] is not None else None, S["cpu_cap"])}
    return None, why_of(S, "gang_hold", gang=x["gang"], at=_C.now_str(until)[5:16]), False


def _place(x, S, t, pre=None):
    """准入核心：放不放一个请求 x（Worker.as_job / job_spec 的形状）：按资源快照 S 定，规则只有这一份，只经 place（外面一层是成组）调。
    返回 (给哪几张卡, 原因, 是否卡在资源上)：放行时卡是快照里的卡（不用卡为 []）、原因 None；不放时卡为 None。
    S（Worker.snapshot 从实时采样拼，simulate 在模拟的时间轴上维护；字段见 Worker.snapshot）：
    - cards [{i, uuid, total, free, keep, other, ka}]：total 总显存（放不下看它），keep 给对方留着、对方还没用上的，free 扣掉 keep 与
      labmon 之外的占用、还没扣在跑任务记账的空闲（记账在 jobs 里，card_free 再扣）；单位 MB；ka：这一轮卡上有借来主机的对方另起、ka.sh 停不了的保活
      （真放行经 guest_scan 记进 owner，推演由 sim_snapshot 照它挡，#45）；
    - room / margin / limit：labmon 任务全停时容器能放多少、另留的余量、上限（GiB；读不到上限都为 None；labmon 之外按收不回的内存算，不算页缓存）；
    - cpu_cap / cpu_room：CPU 记账上限（配额向下取整）、labmon 任务全停时还能给多少核（已扣 labmon 之外的与给对方留的；读不到配额都为 None，不记 CPU）；
    - jobs：labmon 在跑的 [{key, project, name, t0, t1（预计结束，不知道为 None）, gpus, vram {UUID: MB 记账}, ram（GiB 记账）, cpu（核，记账）, excl, tag, hold, ngpu, use, cards}]；
      ext：旧 gpu_queue.sh 的任务，只算打扰、不记账（用量已在 nvidia-smi 与容器用量里）；gp：GPU 进程（sample 认过归属）；cpu：labmon 之外占的核；
    - holds {请求 key: {cards: 留着的卡（整台为 ["*"]）, until: 预计腾空}}：独占任务轮到时在这里留卡，放行或不再等时清掉（真放行就是 excl_hold）；
    - tags：tag 上限；owner：借来的主机上对方超出预留（或刚回落）、有停不了的保活的卡，不放；who / kept：原因里怎么称呼对方、有没有扣给对方的预留；
    - cut：(截止时刻, 没估时的放不放)，截止前跑不完的不放；真放行里截止由 override_hit 在排队前挡住，这里 None；brief：原因只要短的；
    - code：place 写回的这一次的原因代码（WHY 的键，why_of 记；放行或缺文件、pre 时为 None）。
    pre：真放行在看资源前另要做的（拷本地依赖、内存闸门），返回原因或 None；推演不模拟这些，不传。
    独占任务轮到时先留住要腾空的卡（整机独占留住整台），记下预计何时腾空（hold_until）：别的任务预计在那之前跑完的照放（回填）；
    因为别的原因放不了（缺文件、放不下、截止、tag 上限、pre）时不留。
    挡着的是 labmon 之外的进程（借来的主机上多半是对方）时不留、也不算卡在资源上：留着它们也不会走，只会把别的任务一起堵住。"""
    w = lambda code, **kw: why_of(S, code, **kw)
    S["code"] = None
    if x["hold"] and not x["ngpu"]:
        return [], None, False
    if x["ngpu"] and any(card_need(x, c) < need_mb(x["vram"]) for c in S["cards"]):
        x = dict(x, src=x["src"] + "，超过整卡按整卡算")          # 原因里写明
    cut = S["cut"]
    why = (x.get("miss") or nofit(x, S)
           or (w("cut") if cut and (t >= cut[0] or (cut_min(x) is None and not cut[1]) or (cut_min(x) is not None and not fits_before(t, cut_min(x), cut[0], 1)))
               else None)
           or tag_full(x, S) or (pre() if pre else None))
    if why:                                   # 缺文件、放不下、截止前跑不完、tag 到上限、在拷本地依赖、内存闸门：走不到留卡这一步，留着的也放掉
        S["holds"].pop(x["key"], None)
        return None, why, False
    jobs = [j for j in S["jobs"] if not j["hold"]]
    rsv = {k: v for k, v in S["holds"].items() if k != x["key"] and not fits_before(t, x["dur"], v["until"])}   # 回填不了的留卡
    xh = next((j["name"] for j in jobs if j["excl"] == "host"), None)
    if xh:
        return None, w("xhost_run", name=xh), True
    until = [v["until"] for v in rsv.values() if v["cards"] == ["*"]]
    if until:
        return None, w("xhost_hold", note="预计 %s 腾空，这之前跑得完的才放" % _C.now_str(max(until))[5:16] if None not in until
                       else "上面有任务不知道要跑多久，不回填"), True
    held = {u for v in rsv.values() for u in v["cards"] if u != "*"}
    blocked = held | S["owner"] | {u for j in jobs if j["excl"] == "gpu" for u in j["gpus"]}
    mine = [c for c in S["cards"] if (x["allowed"] is None or c["i"] in x["allowed"]) and c["uuid"] not in blocked]
    all_jobs = S["jobs"] + S["ext"]
    if x["excl"] == "host":
        ours, theirs = _C.intruders("host", (), all_jobs, S["gp"], S["cpu"])
        if theirs:
            S["holds"].pop(x["key"], None)
            return None, w("xhost_theirs", who=S["who"], names="、".join(theirs[:3])), False
        if ours:
            S["holds"][x["key"]] = {"cards": ["*"], "until": hold_until(S, None, t)}
            return None, w("xhost_wait", names="、".join(ours)), True
        S["holds"].pop(x["key"], None)
    room_gib = ram_free(S)
    ram, rsrc = ram_need(x, S)
    if room_gib is not None and ram + S["margin"] > room_gib:
        return None, w("ram", ram=ram, src=rsrc, margin=S["margin"], left=max(0.0, room_gib)), True
    cores = cpu_free(S)
    n, src = cpu_need(x, S) if cores is not None else (0, None)
    if n > 0 and n > cores + 1e-6:            # 记账可能超过可放的（实测比估计多、labmon 之外的变多），这时 cpus=0 的照放
        code, held = cpu_held(S, cores, n)
        return None, w(code, held=held, cpus=n, src=src, left=max(0.0, cores), cap=S["cpu_cap"], warm=_C.CPU_WARM_S // 60), True
    kept = "，已扣给对方的预留" if S["kept"] else ""
    if x["excl"] == "gpu" and x["ngpu"] > 0:
        if not mine:
            return None, w("xgpu_none"), True
        free = card_free(S)                                  # MB
        occ = {c["uuid"]: _C.intruders("gpu", [c["uuid"]], all_jobs, S["gp"], None) for c in mine}
        # 候选：没有 labmon 之外的进程、扣掉给对方的预留也放得下；上面只有我们的任务时留着等腾空
        cand = [c for c in mine if not occ[c["uuid"]][1] and c["total"] - c["keep"] >= card_need(x, c)]
        keep = [u for u in (S["holds"].get(x["key"]) or {}).get("cards", []) if u in {c["uuid"] for c in cand}]
        empty = sorted((c for c in cand if not occ[c["uuid"]][0] and free[c["uuid"]] >= card_need(x, c)),
                       key=lambda c: (c["uuid"] not in keep, c["keep"], -free[c["uuid"]]))   # 先挑上一轮留的；多卡时先挑没给对方留的
        if len(empty) >= x["ngpu"]:
            S["holds"].pop(x["key"], None)
            return empty[:x["ngpu"]], None, False
        if len(cand) < x["ngpu"]:
            S["holds"].pop(x["key"], None)
            theirs = sorted({n for c in mine for n in occ[c["uuid"]][1]})
            return None, (w("xgpu_theirs", who=S["who"], names="、".join(theirs[:3])) if theirs else w("xgpu_nofit", vram=x["vram"], kept=kept)), False
        if len(keep) < x["ngpu"]:             # 留下上面任务最少、最快能腾空的卡
            keep = [c["uuid"] for c in sorted(cand, key=lambda c: (len(occ[c["uuid"]][0]), c["keep"]))[:x["ngpu"]]]
        S["holds"][x["key"]] = {"cards": keep, "until": hold_until(S, keep, t)}
        return None, w("xgpu_wait", idx=",".join(str(c["i"]) for c in S["cards"] if c["uuid"] in keep),
                       names="、".join(sorted({n for u in keep for n in occ[u][0]})) or "刚启动的任务"), True
    if x["ngpu"] == 0:
        return [], None, False
    free = card_free(S)                                      # MB
    ok = sorted((c for c in mine if free[c["uuid"]] >= card_need(x, c)), key=lambda c: -free[c["uuid"]])
    if len(ok) >= x["ngpu"]:
        return ok[:x["ngpu"]], None, False
    if not S["cards"]:                        # 不是资源等待，不成屏障：不然 30 分钟后连不用卡的任务也让位
        return None, w("no_gpu"), False
    ka = ",".join(str(c["i"]) for c in S["cards"] if c["uuid"] in S["owner"] and c["ka"])        # 停不了的保活不是对方在用，分开说
    owner = ",".join(str(c["i"]) for c in S["cards"] if c["uuid"] in S["owner"] and not c["ka"])
    return None, w("vram", vram=x["vram"], src=x["src"], kept=kept, held="；有卡留给独占任务" if blocked - S["owner"] else "",
                   ka="；卡 %s 上有对方另起、ka.sh 停不了的保活，不放（要停请对方停）" % ka if ka else "", ka_s="（卡 %s 有停不了的保活）" % ka if ka else "",
                   owner="；对方在用卡 %s（超出预留或刚回落，%d 分钟内不放）" % (owner, _C.GUEST_QUIET_S / 60) if owner else ""), True


def fair_key(r, focus, pol, use, tot=None):
    """放行的先后（admit 逐个比；真放行与推演同一份，#23）：
    (这人的用量, 项目不在 focus 里, 项目的用量 ÷ 权重, 开始等的时刻)，小的先。
    - 这人的用量：pol["own"] 里同一个人员键的各项目的 use（衰减后的卡·时）之和。所以先比人，一人一份。看板给它上的每个项目都写人员键；
      没写的（看板上没有的项目、旧看板没写 owner）合起来算一份；
    - 同一人的项目之间：focus 的先（维护者 2026-11-17 定：优先只在同一人的项目之间排前，不越过别人），再比用量 ÷ 权重（pol["w"] 里没写的为 1）；
    - 项目内先来先跑。所有项目属于同一人（或都没写人员键）时第一项都相同，顺序与按项目比时（#23 之前）一样。
    tot：person_use(pol, use) 先算好的（admit 一次挑选里各请求共用），不给时现算。"""
    p = r["project"]
    mine = (person_use(pol, use) if tot is None else tot).get((pol.get("own") or {}).get(p), 0.0)
    return (mine, p not in focus, use.get(p, 0.0) / (pol["w"].get(p) or 1.0), r["since"])


def admit(reqs, focus, pol, use, now, place, shadow=lambda r: None):
    """一轮放行的规则（tick 真放行与 simulate 推演共用，规则只在这一处）。每次取排在最前的一个（fair_key）：先比人（同一人的几个项目的用量合起来）；
    同一人的项目之间 focus 的先，再比衰减后的 GPU 用量 ÷ 权重（use，卡·时；pol["w"] 里没写的为 1），小的先，放行一个就记上它预计的卡·时（r["charge"]），
    成组成员留下资源（place 的原因代码 gang_hold，记在 r["code"]）同样记上：留着就占着，后面的按记上之后的排；
    项目内先来先跑。独占任务不另排前面，轮到它时才留卡（place 里）。等得久不另排前面（否则单卡跑长任务时轮到的总是等了一整个任务的那个，
    权重不起作用），只防被越过：place(r) 放行返回 None，否则返回 (原因, 是否卡在资源上)；按这个顺序第一个卡在资源上又等满 pol["cap"] 的
    成为屏障，shadow(r) 给它预计最早能放行的时刻，排在它后面的（占位任务除外）只有预计在那之前跑完的才放（回填，fits_before），其余让位。
    返回没放行的 [(请求, 原因, 排位)]：排位 {n 在没放行的里排第几, puse 轮到它时这人（项目主人）的用量, focus, ratio 轮到它时项目的 用量÷权重, barrier 是屏障,
    behind 被哪个屏障挡着 [项目, 任务]}，看板的等待表据此逐条解释。"""
    use, left, barrier, until, out = dict(use), list(reqs), None, False, []
    while left:
        tot = person_use(pol, use)
        key = lambda r: fair_key(r, focus, pol, use, tot)
        r = left.pop(min(range(len(left)), key=lambda i: key(left[i])))
        k = key(r)
        pos = {"n": len(out) + 1, "puse": round(k[0], 3), "focus": not k[1], "ratio": round(k[2], 3)}
        if barrier and not (r["kind"] == "queue" and _C.is_hold(r["name"])):
            if until is False:
                until = shadow(barrier)            # 后面有请求才推（推演里只排屏障一个时不再往下推）
            if not fits_before(now, r.get("dur"), until):
                out.append((r, "让位：%s 的 %s 已等 %d 分钟（等了 %d 分钟以上的不再被越过%s）" % (
                    barrier["project"], barrier["name"], (now - barrier["since"]) / 60, pol["cap"] / 60,
                    "；它预计 %s 能放行，这之前跑得完的才放" % _C.now_str(until)[5:16] if until else ""),
                    dict(pos, behind=[barrier["project"], barrier["name"]])))
                continue
        res = place(r)
        if res is None or r.get("code") == "gang_hold":
            use[r["project"]] = use.get(r["project"], 0.0) + (r.get("charge") or 0.0)
        if res is None:
            continue
        if res[1] and now - r["since"] >= pol["cap"] and not barrier:
            barrier, pos["barrier"] = r, True
        out.append((r, res[0], pos))
    return out


def estimate(hist, r):
    """资源估计：显存、内存取同类任务最近几次峰值的最大值 × 1.1，及单跑（同卡没有别的任务）时的平均 GPU 利用率。只看量到了用量的记录。worker 准入、推演与 fit 都用它。
    显存、内存逐项依次看（#61 维护者定）：同 tag、去掉种子编号后同名的历史（两项一起取自先找到的那一档）› 清单写的提示值（vram=、ram=，> 0 才算写了）
    › 同一条队列的历史（同一份清单里可以是完全不同的任务，最弱，不盖过明写的提示值）› 0。“近几次”按跑成功的数，跑满 5 分钟后失败的（非 OOM）
    只在比这几次里最旧的新时参与取最大值：没跑完的峰值只是下限，只能抬高、不占名额（peak_rows，#134）。来源：显存 src、内存 ram_src；util 跟着显存的来源走
    （显存用的是提示值时为 None，不拿别的任务的利用率）。
    CPU（cpus 整数核，job_cpus 取整；cpu_src 来源）：清单的 cpus= 优先（来源“提示值”），否则同类（同 tag › 同名 › 同队列）近几次 1 分钟峰值核数（cores_peak，
    期 1 起才记，旧行当没有历史）的最大值 × 1.1，都没有按 CPU_DEFAULT（#4 维护者定：cpus= 优先于历史，历史优先于缺省）。
    写了的 cpus= 同类实测一直远低于它时只收小记账（cpu_shrink，#91）：cpus 是记账核数，cpus_set 记下清单写的值（开跑仍按它给，cpu_threads）。
    OOM 后抬高（#62 维护者定）：用量行标了 oom 的（finish 经 oom_of 记）不进历史（它的峰值是被当时可用的量截住的）；同 tag、同名两档里，
    各取这一档最近一次跑成功之后的 OOM 记录，给 OOM 的那一项（vram 或 ram）一个下限 max(那次放行时的估计 est × 2, 那次的峰值 × 1.1)，几条取大；
    这一项的估计 = max(上面按顺序取的值, 下限)，提示值写小了的同样抬高。连续 OOM 连续翻倍（每条的 est 已含上一次的下限），封顶照 card_need、ram_need；
    这一档有一次新的跑成功（退出码 0；跑了 5 分钟后被停下、别的错误退出的不算）就回到按实测估。同队列一档的 OOM 不算（同一份清单里可以是完全不同的任务）。来源是下限时 util 为 None。"""
    keys = ([("tag", r["project"], r["tag"])] if r.get("tag") else []) + [("name", r["project"], _C.base_name(r["name"]))]
    if r.get("queue"):
        keys.append(("queue", r["root"], r["queue"]))
    ok = lambda h: not h.get("oom") and (ran_ok(h) or (h.get("min") or 0) >= 5)     # noqa: E731 — 跑成功的，或跑了 5 分钟以上、不是 OOM 的（没跑成功的只能抬高：peak_rows）
    label = {"tag": "同 tag", "name": "同名", "queue": "同队列"}
    peak = None                                   # 显存、内存取自的那一档历史：(档, 记录)
    floor = {"vram": (0.0, None), "ram": (0.0, None)}   # OOM 后的下限：(GiB, 来源)
    cpu = (_M.job_cpus(r["cpus"]), "提示值") if r.get("cpus") is not None else None
    shrink = None                                 # 写了 cpus= 时，同 tag › 同名第一档有峰值的历史（cpu_shrink 看）
    for k in keys:
        rows = [h for h in hist if _C.hist_key(h, k[0]) == k]   # hist 按结束时刻排好（load_hist）
        same = [h for h in rows if ok(h)]
        hs = peak_rows(same, "ram_peak")
        if hs and peak is None:
            peak = (k[0], hs)
        cr = peak_rows(same, "cores_peak")
        cs = [h["cores_peak"] for h in cr]
        if cs and cpu is None:
            cpu = (cpu_from_peak(max(cs)), "%s 近 %d 次峰值 ×1.1" % (label[k[0]], len(cs)))
        if k[0] == "queue":
            continue
        if cr and shrink is None:
            shrink = (label[k[0]], cr)
        last = max((i for i, h in enumerate(rows) if h.get("rc") == "0"), default=-1)   # 只认真跑成功的：跑了 5 分钟后被停下、别的错误退出的不算资源够了
        for h in rows[last + 1:]:
            f = h.get("oom")
            if f not in floor:
                continue
            e = (h.get("est") or {}).get(f) or 0.0
            pk = (h.get("vram_peak") or 0) / 1024 / max(1, len(h.get("gpus") or [1])) if f == "vram" else h.get("ram_peak") or 0
            for v, s in ((2 * e, "放行时估 %.1f GiB ×2" % e), (1.1 * pk, "OOM 时峰值 %.1f GiB ×1.1" % pk)):
                if round(v, 2) > floor[f][0]:
                    floor[f] = (round(v, 2), "%s 上次 OOM：%s" % (label[k[0]], s))
    cpu = cpu or (_C.CPU_DEFAULT, "缺省 %d 核" % _C.CPU_DEFAULT)
    set_ = None
    if cpu[1] == "提示值" and shrink:
        n = cpu_shrink(cpu[0], shrink[1])
        if n is not None:
            set_, cpu = cpu[0], (n, "清单 cpus=%d 偏大：%s 近 %d 次峰值 %.1f 核 ×1.1（开跑仍给 %d 核）"
                                 % (cpu[0], shrink[0], len(shrink[1]), max(h["cores_peak"] for h in shrink[1]), cpu[0]))
    if peak:
        kind, hs = peak
        src = "%s 近 %d 次峰值 ×1.1" % (label[kind], len(hs))
        solo = [h["gpu_util"] for h in hs if h.get("gpu_util") is not None and not [x for x in h.get("with_gpu") or [] if x != "GPU 保活"]]
        hv = {"ram": round(1.1 * max(h["ram_peak"] or 0 for h in hs), 2),
              "vram": round(1.1 * max((h.get("vram_peak") or 0) / 1024 / max(1, len(h.get("gpus") or [1])) for h in hs), 2),
              "util": round(sum(solo) / len(solo)) if solo else None}
    out = {}
    for f, s in (("vram", "src"), ("ram", "ram_src")):
        if peak and (peak[0] != "queue" or not r[f]):
            out[f], out[s] = hv[f], src
        else:
            out[f], out[s] = r[f], "提示值"
        if floor[f][0] > out[f]:
            out[f], out[s] = floor[f]
    out["util"] = hv["util"] if peak and out["src"] == src else None
    return dict(out, cpus=cpu[0], cpu_src=cpu[1], **({"cpus_set": set_} if set_ else {}))


def cpu_shrink(n, rows):
    """清单写了 cpus=n、同类历史 rows（estimate 给的：同 tag › 同名第一档有峰值的近 HIST_N 条，跑成功或跑满 5 分钟、非 OOM）一直远低于它时，
    没开跑的行按几核记账（#91）；不收小为 None。要至少 CPU_SHRINK_N 条，且每一条都满足：那次实际导出的核数（用量行的 give，即开跑时的
    LABMON_CPUS：按那台的配额封顶过，#99）不少于 n——量到的峰值才是在至少这么多线程下跑出来的；在配额小的主机上跑的、把 cpus= 改大以后的旧行、
    没记 give 的行（#99 之前的）都不算数——
    1 分钟峰值 cores_peak 不超过 n × CPU_SPARE_FRAC 且少至少 CPU_SPARE_MIN 核（与 cpu_fit 的“给多了”同一门槛）。
    记账取峰值最大值按 cpu_from_peak 估，比 n 小才算收小。只收记账：开跑仍按 n 给（cpu_threads），运行中照旧 max(估计, 实测) 与 #65 回收。"""
    if n < 2 or len(rows) < _C.CPU_SHRINK_N:
        return None
    for h in rows:
        give, pk = h.get("give"), h["cores_peak"]
        if give is None or give < n or pk > n * _C.CPU_SPARE_FRAC or n - pk < _C.CPU_SPARE_MIN:
            return None
    m = cpu_from_peak(max(h["cores_peak"] for h in rows))
    return m if m < n else None


def ran_ok(h):
    """一条用量记录是不是跑成功的：退出码 0，或拿不到退出码的单元（kind slot、rc 为空）。duration 只认它；estimate 的近 HIST_N 次窗口按它数（peak_rows）。"""
    return h.get("rc") == "0" or (h.get("rc") is None and h.get("kind") == "slot")


def trusted(q):
    """实际/估时 q 在 RATIO_OK 里：当时的估时可信（duration 才拿它折算改过的估时、借给别的名字，#134）。"""
    return _C.RATIO_OK[0] - 1e-9 <= q <= _C.RATIO_OK[1] + 1e-9


def peak_rows(rows, f):
    """estimate 取峰值 f（ram_peak、cores_peak）的那几条：rows 是同一档跑成功或跑满 5 分钟、非 OOM 的记录（按结束排好）。
    取有 f 的里最近 HIST_N 次跑成功的（ran_ok），比其中最旧那条新的没跑成功的也带上：没跑完的峰值只是下限，只能抬高、不占近几次的名额（#134）；
    这一档没有跑成功的，照旧取最近 HIST_N 条。"""
    hs = [h for h in rows if h.get(f) is not None]
    good = [i for i, h in enumerate(hs) if ran_ok(h)][-_C.HIST_N:]
    return hs[good[0]:] if good else hs[-_C.HIST_N:]


def rec_conc(h):
    """一条用量记录跑的时候同卡平均有几条 labmon 任务（含自己，measure 记的 conc，#67）。
    11-09 前的行没有 conc：同卡没有别的任务（with_gpu 只有保活）的当 1，其余不知道（None）。这条回退 11-23 后可删（那时同类近几次都有 conc）。"""
    if h.get("conc") is not None:
        return h["conc"]
    return None if [x for x in h.get("with_gpu") or [] if x != "GPU 保活"] else 1.0


def near(hs, k, n, need=1):
    """按同卡并行数挑记录（duration 用）：知道并行数的里按比例的差 |c − k| / k 取与 k 最接近的那一档（差不超过最接近的那条再多 NEAR_TOL），
    这一档不到 need 条时放宽到第 need 近的那条再多 NEAR_TOL；取其中最新的 n 条。同类那一支 need=1（有一条并行数对得上的就用它），
    借比值的各档 need=2（各档至少要 2 条）。以前按绝对差“最接近的 + 1”，1 条与 2 条并行总落在一起，k=1 也取挤着跑的（#138）。
    都不知道并行数（11-09 前的、挤着跑的旧行）时取最新的 n 条。保持原来的先后。"""
    known = [(h, abs(c - k) / k) for h in hs for c in [rec_conc(h)] if c is not None]
    if not known:
        return hs[-n:]
    ds = sorted(d for _, d in known)
    lim = ds[0] + _C.NEAR_TOL
    if sum(d <= lim + 1e-9 for d in ds) < need:
        lim = ds[min(need, len(ds)) - 1] + _C.NEAR_TOL
    return [h for h, d in known if d <= lim + 1e-9][-n:]


def conc_key(k):
    """同卡并行数取整、封顶到 [1, CONC_MAX]：Worker.dur_k 按它缓存，queue_durs 的缓存键也按它（k_of 是按时间加权的小数，每轮都在变）。"""
    return min(_C.CONC_MAX, max(1, int(round(k))))


def run_tot(running, eta, eta_t):
    """在跑任务按日志进度估的总时长 {(项目, 同类名): [(任务, 分钟)]}：看板写来的预计结束（eta.json 的 jobs，写的时刻 eta_t 在任务开始之后才算）− 开始。
    同类还没有跑完的时，排队的按它估（duration 的 run，#67）。占位任务不算（它没有日志进度）。Worker.run_tot 与 fit（读状态里的 running）共用。"""
    out = {}
    for j in running:
        e = None if _C.is_hold(j["name"]) else eta.get("%s|%s" % (j.get("project"), j["name"]))
        if e and eta_t >= j["t0"] and e > j["t0"]:
            out.setdefault((j.get("project"), _C.base_name(j["name"])), []).append((j["name"], round((e - j["t0"]) / 60)))
    return out


def with_cut(d, hi=None, why=None, m=_C.DUR_MARGIN):
    """给 duration 的结果补上截止前的准入用的时长（分钟，已含余量）cut 与说明 cut_src（#139）：中间值 × 余量 m；
    同类历史给的再与 hi（同类挑出的那几次里最长的，why 是它的说法）取大——同类波动大时中间值 × 1.2 盖不住慢的那几次。返回 d。"""
    c = d["min"] * m
    d["cut"], d["cut_src"] = (hi, "%s（中间值 %.0f）" % (why, d["min"])) if hi is not None and hi > c + 1e-9 else (c, "%.0f × 余量 %g" % (d["min"], m))
    return d


def borrowable(pr):
    """借比值的一档里能借的记录（#139）：实际/估时 在 RATIO_OK 里的照借；不在里面的只在离这一档的中位数不超过 3 倍（同 RATIO_OK 的宽度）、
    且这样的记录至少来自 2 个同类（base_name）时才借——项目一贯少估（或多估）3 倍以上时各类任务都偏，照借；#134 的一次写错只在一个同类上
    （它跑了几次都偏 ×11），不借。以前按绝对区间一律不借，一贯偏 ×4 的项目新任务反而按清单估时。"""
    if not pr:
        return pr
    qs = [h["min"] / h["est_min"] for h in pr]
    m = statistics.median(qs)
    odd = {_C.base_name(h.get("name") or "") for h, q in zip(pr, qs) if not trusted(q) and trusted(q / m)}
    return [h for h, q in zip(pr, qs) if trusted(q) or (len(odd) >= 2 and trusted(q / m))]


def gpus_of(hs):
    """记录里的 GPU 型号（duration 借别的型号时写进依据）。"""
    return "、".join(sorted({h["gpu"] for h in hs if h.get("gpu")}))


def same_dur(hs, r, e, k, other=False):
    """duration 的同类那一支（hs 是可用的记录）：同类近 HIST_N 次（按并行数挑）。这一个与记录都有估时就按 实际/估时 的中位数折算
    （估时与现在相同的优先），否则取实际用时的中位数；估时改过、旧比值又不在 RATIO_OK 里时也取实际用时（#134）。
    other：hs 是别的型号的记录（#139）——速度不同，只借比值（要这一个有估时）、不用实际用时，截止余量按 CUT_EST_MARGIN。没有为 None。"""
    cls = _C.base_name(r["name"])
    same = near([h for h in hs if _C.base_name(h.get("name") or "") == cls][-_C.RATIO_N:], k, _C.HIST_N)
    if not same:
        return None
    cs = [c for c in map(rec_conc, same) if c is not None]
    par = "（同卡并行约 %g 条）" % round(statistics.median(cs), 1) if cs and max(cs) > 1.05 else ""
    pfx = "别的型号 %s 上" % gpus_of(same) if other else ""
    ks = [(h["est_min"], h["min"] / h["est_min"]) for h in same if h.get("est_min")]
    off = None
    if e and ks:
        cur = [q for x, q in ks if abs(x - e) < 1e-6]     # 估时与现在一样的优先：改过估时后跑过一次就以新的为准
        qs = cur or [q for _, q in ks]
        q = statistics.median(qs)
        if cur or trusted(q):
            return with_cut({"min": e * q, "src": "%s同类近 %d 次%s 实际/估时 ×%.2f" % (pfx, len(qs), par, q), "k": round(q, 3), "cls": cls, "same": not other},
                            e * max(qs), "%s同类近 %d 次里最长的" % (pfx, len(qs)), _C.CUT_EST_MARGIN if other else _C.DUR_MARGIN)
        off = q                                              # 当时的估时与实际差 3 倍以上（写错了）：不拿比值放大改过的估时，按实际用时（#134）
    if other:
        return None
    ms = [h["min"] for h in same]
    m = statistics.median(ms)
    return with_cut({"min": m, "src": "同类近 %d 次%s实际用时%s" % (len(same), par, "（当时估时与实际差 ×%.1f，不按比值折算）" % off if off else ""),
                     "k": round(m / e, 3) if e else None, "cls": cls, "same": True}, max(ms), "同类近 %d 次里最长的" % len(same))


def ratio_dur(hs, r, e, k, other=False):
    """duration 借别的名字的 实际/估时 那一支（#113）：同 tag › 同名字前缀（GROUP_RE，同看板自动归组）› 同一清单 › 本项目，
    各档取这一档最近 2×RATIO_N 个里按并行数挑的 RATIO_N 个（near 至少给 2 个），其中能借的（borrowable）至少 2 个才用；
    比值取各条的中位数（不按时长加权：几条要跑几小时的不主导）。other：hs 是别的型号的记录（#139），截止余量按 CUT_EST_MARGIN。没有为 None。"""
    pre = lambda n: (_C.GROUP_RE.match(n or "") or [None])[0]   # noqa: E731 — 名字前缀，没有 _ 或 - 的为 None
    rated = [h for h in hs if h.get("est_min") and not _C.is_hold(h.get("name"))]
    tiers = ([("同 tag %s " % r["tag"], lambda h: h.get("tag") == r["tag"])] if r.get("tag") else []) \
        + ([("同前缀 %s " % pre(r["name"]), lambda h: pre(h.get("name")) == pre(r["name"]))] if pre(r["name"]) else []) \
        + ([("同一清单", lambda h: (h.get("root"), h.get("queue")) == (r["root"], r["queue"]))] if r.get("root") and r.get("queue") else []) \
        + [("本项目", lambda h: True)]
    for label, f in tiers:
        pr = borrowable(near([h for h in rated if f(h)][-2 * _C.RATIO_N:], k, _C.RATIO_N, 2))
        if len(pr) >= 2:
            q = statistics.median(h["min"] / h["est_min"] for h in pr)
            return with_cut({"min": e * q, "src": "%s%s近 %d 个 实际/估时 ×%.2f" % ("别的型号 %s 上" % gpus_of(pr) if other else "", label, len(pr), q),
                             "k": round(q, 3), "cls": _C.base_name(r["name"]), "same": False}, m=_C.CUT_EST_MARGIN if other else _C.DUR_MARGIN)
    return None


def duration(hist, r, model=None, k=1, run=None):
    """任务要跑多久（分钟，中间值）：看板的预计时间与截止前的准入共用这一处。r 是请求（项目、名字、est_min，有的再带 tag、清单 root + queue）。依次看：
    - 同型号的同类（项目 + 去掉种子编号的名字）跑成功过的（same_dur）；
    - 同类还没有跑完的、有在跑的：run 给的各个在跑同类按日志进度估的总时长（看板写来的 eta.json）的中位数；
    - 这一个有估时的：别的型号上同类的 实际/估时（#139：借来的主机常是别的型号，正是截止准入最要准的时候；速度不同，只借比值）；
      同型号别的名字的 实际/估时（ratio_dur，#113）；别的型号别的名字的；
    - 清单的 est_min。
    只认跑完的（停下重排的只跑了一段）、本项目的记录；没记型号的旧记录算同型号。同类与借来的比值的记录都按同卡并行数挑（near）：
    k 是这个任务预计会遇到的同卡 labmon 任务数（含自己），取并行数与它最接近的那几条——几条任务分时共用一张卡时，有的类几乎不拖慢、有的拖慢好几倍，
    单跑的记录估不了挤着跑的（#67：27 上的小任务并行 4 条与 6 条都是 20 分钟，sm_vit 并行 4 条要 300 分钟）。
    占位任务等的是条件、不是算力：就按清单估时，不看同类、不折算，它的时长也不进借来的比值（写了依赖的另由 dep_end 算）。
    返回 {"min", "src", "k"（实际/估时，不知道为 None）, "cls"（同类名）, "same"（是不是同型号同类历史给的：读写拥塞嫌疑只认它）,
    "cut", "cut_src"（截止前的准入用的时长与说明，with_cut：同型号的 × DUR_MARGIN、同类的再与最长的一次取大，别的型号与清单估时 × CUT_EST_MARGIN）}；
    没有估时也没有同类记录为 None。"""
    cls, proj, e = _C.base_name(r["name"]), r["project"], r.get("est_min")
    if _C.is_hold(r["name"]):
        return with_cut({"min": e, "src": "清单估时", "k": None, "cls": cls, "same": False}) if e else None
    ok = [h for h in hist if h.get("min") and ran_ok(h) and not h.get("requeued") and _C.hist_key(h, "name")[1] == proj]
    mine = [h for h in ok if not model or not h.get("gpu") or h["gpu"] == model]
    other = [h for h in ok if model and h.get("gpu") and h["gpu"] != model]
    d = same_dur(mine, r, e, k)
    if d:
        return d
    if run:
        m = statistics.median(run)
        return with_cut({"min": m, "src": "同类在跑的 %d 个按进度估" % len(run), "k": round(m / e, 3) if e else None, "cls": cls, "same": False},
                        max(run), "同类在跑的 %d 个里最长的" % len(run))
    if not e:
        return None
    d = same_dur(other, r, e, k, True) or ratio_dur(mine, r, e, k) or ratio_dur(other, r, e, k, True)
    return d or with_cut({"min": e, "src": "清单估时", "k": None, "cls": cls, "same": False}, m=_C.CUT_EST_MARGIN)


def shrunk_text(e):
    """labmon worker status 的运行、等待行末尾：按实测收小了 CPU 记账的（cpu_shrink，#91）写一句，别的为空。"""
    return "；CPU 按实测记 %d 核（清单 cpus=%d，开跑仍给这么多）" % (e["cpus"], e["cpus_set"]) if (e or {}).get("cpus_set") else ""


def cpu_cls(j):
    """同类任务的类（看板合并 cpu_low:、cpu_high: 用，#91；cpu_budget 认同类）：有 tag 按 tag，否则去掉种子编号的名字（同 estimate 的同 tag › 同名）。
    认同类时 by 与 cls 一起比：tag 恰好等于别的任务去种子号后的名字时是两类（#99）。"""
    return {"by": "tag", "cls": j["tag"]} if j.get("tag") else {"by": "name", "cls": _C.base_name(j["name"])}


def cpu_from_peak(peak):
    """按 1 分钟峰值核数估几个核：峰值 ×1.1 取整（job_cpus）、至少 1。estimate 按同类历史估、cpu_fit 给“写小”的建议值共用。"""
    return max(1, _M.job_cpus(1.1 * peak))


def cpu_budget(S, j):
    """在跑的任务 j 那一类每条最多能给几个核（cpu_fit 给少了的建议值封顶用，#112）：这台 labmon 全停时能给的（快照的 cpu_room，已扣 labmon 之外的、
    给对方的预留与停不了的保活）减去同机别的任务（别的项目的、本项目别的类的）已记的核，再按本项目同一类（cpu_cls）此刻同时在跑的条数均分、向下取整：
    同类几条车道照同一条建议改清单，加起来不能超过配额。返回 {per 每条最多几核, cap 记账上限, other 别的任务已记, out labmon 之外与预留占的, same 同类几条, kept 有没有给对方留}；
    读不到配额为 None（不记 CPU，建议不封顶）。"""
    if S["cpu_room"] is None:
        return None
    cls = (j.get("project"), cpu_cls(j))          # 类连同来源（tag 还是去种子号的名字）比：tag 恰好等于别的任务去种子号后的名字时不是同一类（#99）
    mine = [not x["hold"] and (x.get("project"), cpu_cls(x)) == cls for x in S["jobs"]]
    other = sum(x["cpu"] for x, m in zip(S["jobs"], mine) if not m)
    n = max(1, sum(mine))
    return {"per": max(0, int((S["cpu_room"] - other) / n + 1e-6)), "cap": S["cpu_cap"], "other": round(other, 1),
            "out": round(S["cpu_cap"] - S["cpu_room"], 1), "same": n, "kept": bool(S.get("kept"))}


def cpu_line(st):
    """一台 status.json 里的 CPU 说成一句（#112；`labmon worker status`、`worker fit` 每台的开头与看板的 `labmon status` 共用）：容器配额与记账上限
    （nproc 报的是宿主机的核数）、labmon 在跑的任务记了几核、labmon 之外（借来的主机上含给对方的预留）占几核、还能给新任务几核（状态的 room.cpu）。
    状态里没有主机信息为 None。"""
    info, room = st.get("info") or {}, st.get("room") or {}
    if not info:
        return None
    q, n = info.get("cpu_quota"), info.get("nproc")
    host = "nproc 报的 %d 是宿主机的核数" % n if n else ""
    if not q:
        return "CPU：容器没设配额，不按核记账" + ("（%s）" % host if host else "")
    cap = room.get("cpu_cap") or _M.cpu_cap(q)
    run = [j for j in st.get("running") or [] if (j.get("acct") or {}).get("cpu")]
    used = sum(j["acct"]["cpu"] for j in run)
    s = "CPU：配额 %g 核%s" % (round(q, 2), "（按 %d 核记账）" % cap if abs(q - cap) > 1e-6 else "")
    s += "，%s" % host if host else ""
    s += "；labmon 在跑 %d 个任务记 %g 核" % (len(run), round(used, 1)) if run else "；labmon 没有任务记着核"
    free = room.get("cpu")
    if free is not None:
        out = cap - free - used
        if out >= 0.1:
            s += "，labmon 之外%s占 %g 核" % ("与给对方的预留" if (st.get("guest") or {}).get("keep") else "", round(out, 1))
        s += "，还能给新任务 %g 核" % max(0.0, round(free, 1))
    return s


def cpu_fit(j, cap, util, room=None):
    """在跑的任务 j（状态 running 的形状：name、gpus、est、use）开跑时给的核数是给少了还是给多了（#46；#37 的“被 CPU 卡住”也是它）。
    从开始量窗口核数起满 CPU_WARM_S（cpu_warm）才判；给的核数 give 是开跑时导出的 LABMON_CPUS（cpus_export(cpu_threads)）；util 各卡近 UTIL_WIN_S 的利用率 {uuid: %}。
    - low（给少了、被 CPU 卡住）：近 CPU_WARM_S 各 1 分钟窗口核数（use.pks）的平均不少于 give × CPU_BOUND_FRAC，且所用的卡里有利用率低于 UTIL_LOW 的
      （卡吃饱了加核也没用；不用卡的不判，单线程脚本本来就只用 1 核）。sug 建议写的 cpus= 是 give 的 2 倍，封顶到这一类每条最多能给的（room：cpu_budget 的结果，#112；
      不给时封顶到记账上限 cap）；不比 give 多（已给到上限、配额被别的任务与同类分完了）为 None。low 另带 room 原样（看板写进告警：配额、别的任务记了多少）。
    - high（给多了）：估计来自清单的 cpus=（est.cpu_src 是“提示值”；按历史估的会往实测靠、按实测收小了记账的已经让出，都不判），跑到现在的 1 分钟峰值 use.cores_peak
      不超过 give × CPU_SPARE_FRAC、且少至少 CPU_SPARE_MIN 核（看整段峰值：开头加载阶段用过的核不算多给）。sug 按峰值估（cpu_from_peak，同 estimate）。
    返回 {how, avg 近 10 分钟平均核数, peak 整段峰值, give, sug}（low 另有 room），都不是为 None。占位任务、没有估计的不判。
    gpu_slack（fit 开车道）只取 low；状态里运行中任务的 cpu_fit 是它，看板据此出只给实验会话的 cpu_low:、cpu_high: 告警。"""
    u, e = j.get("use") or {}, j.get("est")
    if _C.is_hold(j.get("name")) or not e or not cpu_warm(u):
        return None
    give = cpus_export(cpu_threads(e), cap)
    avg = round(sum(x[1] for x in u["pks"]) / len(u["pks"]), 2)
    peak = u.get("cores_peak") or max(x[1] for x in u["pks"])
    out = {"avg": avg, "peak": peak, "give": give}
    if avg >= give * _C.CPU_BOUND_FRAC and any((util.get(g) is not None and util[g] < _C.UTIL_LOW) for g in j.get("gpus") or []):
        lim = room["per"] if room else cap
        return dict(out, how="low", sug=2 * give if lim is None else min(2 * give, lim) if lim > give else None, room=room)
    if e.get("cpu_src") == "提示值" and e.get("cpus") and peak <= give * _C.CPU_SPARE_FRAC and give - peak >= _C.CPU_SPARE_MIN:
        return dict(out, how="high", sug=cpu_from_peak(peak))
    return None


def gpu_slack(cards, jobs, th, cap):
    """卡吃不满是不是真有余量（fit 开车道、offers 同机多开车道共用一条规则，#35、#37）：
    cards [{uuid, util 近 10 分钟利用率 %（样本不够为 None）}]；jobs 在跑的（状态 running 的形状：name、gpus、est、use）；
    th 是状态的 throttle（Worker.throttle：这一段限流，门槛 THR_HOT、THR_COOL 只在 thr_step；没在限流为 None）；cap 记账上限（cpu_cap）。
    返回 {throttle: 限流时的那句说明、否则 None（这台先减线程，不开新车道）,
    bound: {卡 uuid: [(任务, 平均核数, 给的核数)]}：利用率低于 UTIL_LOW 的卡上被 CPU 卡住的在跑任务（cpu_fit 的 low；先给它们加核，不在这张卡上开新车道）}。"""
    util = {c["uuid"]: c.get("util") for c in cards}
    low = {u for u, v in util.items() if v is not None and v < _C.UTIL_LOW}
    bound = {}
    for j in jobs:
        b = cpu_fit(j, cap, util)
        for u in j.get("gpus") or []:
            if b and b["how"] == "low" and u in low:
                bound.setdefault(u, []).append((j["name"], b["avg"], b["give"]))
    thr = None if th is None else th.get("thr")
    return {"throttle": None if th is None else "容器 CPU 在限流（%s，已持续 %d 分钟）" % (
        "近 1 分钟 %.0f%% 的周期被限流" % thr if thr is not None else "近 1 分钟被限流的周期过了门槛", th.get("for", 0) // 60), "bound": bound}


class SchedMixin:

    # ---- 估计与记账 ----
    def acct(self, j):
        """运行中任务的记账：max(估计值, 实测)（CPU 开跑满 10 分钟后见下）。内存 GiB；显存按卡，MB；CPU 核（估计是 job_cpus 取整过的，实测取此刻的核数；占位任务不记）。
        CPU 另有回收（#65）：从开始量窗口核数（use 的 pk0）起满 CPU_WARM_S 后，估计那一份换成 min(开跑时给的核数, max(1, 近 CPU_WARM_S 内 1 分钟窗口核数的最大值))，
        再与此刻实测取大：估计取的是同类的 1 分钟峰值，多半出在开头加载阶段，之后整段按它占着，核记了账却没人用。内存、显存是硬资源（超了 OOM），不回收；
        CPU 超用只是被限流变慢。写了 cpus=0 的给 0 核，回收后仍是 0。"""
        e, u = j.get("est") or {"ram": 0.0, "vram": 0.0}, j.get("use") or {}
        vb = u.get("vram_by") or {}
        give = float(cpu_give(e.get("cpus", _C.CPU_DEFAULT), _M.cpu_cap(self.info.get("cpu_quota"))))
        if cpu_warm(u):
            give = min(give, max(1.0, max(x[1] for x in u["pks"])))
        return {"ram": max(e["ram"], u.get("ram") or 0.0),
                "vram": {g: max(e["vram"] * 1024, vb.get(g, 0.0)) for g in j["gpus"]},
                "cpu": 0.0 if _C.is_hold(j["name"]) else max(give, u.get("cores") or 0.0)}

    def cpus_for(self, j):
        """开跑时导出的 LABMON_CPUS：给的核数（cpu_threads：一般就是估计核数，按实测收小了记账的仍按清单的 cpus=）封顶到配额（cpu_give），
        至少 1（写 cpus=0 的不记账，也给 1）。"""
        return cpus_export(cpu_threads(j["est"]), _M.cpu_cap(self.info.get("cpu_quota")))

    def mem_room(self):
        """labmon 的任务全停时容器内存还能放多少（GiB）：上限 − labmon 之外收不回的内存 − 借来的主机上给对方留着、对方还没用上的部分。
        labmon 之外 = 收不回的内存（mem.anon，不算页缓存）− 我们任务的实测：读数据留下的页缓存内核会先回收，不算别人的（#4 维护者定）。
        读不到上限时为 None。在跑任务的记账由快照（snapshot）里的 jobs 扣（ram_free）。"""
        m = self.info.get("mem")
        if not m or not m.get("limit"):
            return None
        obs = sum((j.get("use") or {}).get("ram") or 0.0 for j in self.running.values())
        k = self.keep() or {}
        left = max(0.0, k["ram"] - (self.other["ram"] or 0.0)) if k.get("ram") and self.held() else 0.0
        return m["limit"] - max(0.0, m["anon"] - obs) - left

    def cpu_room(self):
        """labmon 的任务全停时还能给多少核（与 mem_room 同构）：记账上限（配额向下取整）− labmon 之外的 − 给对方留的 − ka.sh 停不了的保活。
        labmon 之外的按快照的 cpu（连着两轮占 1 核以上才算，零碎的 shell、一轮尖峰不算）；借来的主机上取 max(对方实测, keep.cpu)
        （对方还没用上的预留也扣；借来时对方空着的不预先留）。对方另起、ka.sh 停不了的保活不算对方的用量（不和预留取 max），
        它这一段占的核（other.ka_cpu）另扣，不看连着几轮（#37）。旧 gpu_queue.sh 的任务不记账，此刻用的核也算 labmon 之外。
        读不到配额为 None（不记 CPU）。在跑任务的记账由快照的 jobs 扣（cpu_free）。"""
        cap = _M.cpu_cap(self.info.get("cpu_quota"))
        if cap is None:
            return None
        o = (self.other["cpu"] or 0.0) if self.cpu_n >= 2 else 0.0
        k = self.keep() or {}
        ext = sum((j.get("use") or {}).get("cores") or 0.0 for j in self.ext.values())   # 旧 gpu_queue.sh 的任务不记账，此刻用的核算 labmon 之外（同 mem_room）
        return cap - (max(o, k["cpu"]) if k.get("cpu") and self.held() else o) - self.other.get("ka_cpu", 0.0) - ext

    def snapshot(self, limits, gpus=None, gp=()):
        """资源快照：准入核心 place 的输入（字段见 place 的说明），真放行每放一个就重拼一份（前面放行的已进 running），
        推演（forecast、shadow）拿它当起点。gpus 是 query_gpus 给的各卡空闲（停保活之后查的），不给时用 sample 读到的；
        gp 是 gpu_now 给的此刻 GPU 进程（有独占请求时才查）。holds 就是 excl_hold 本身：place 在里面留卡、清卡；gholds 同理是成组留的资源 gang_hold。"""
        now = _C.time.time()
        m = self.info.get("mem") or {}
        info = {g["uuid"]: g for g in self.info.get("gpus") or []}
        if gpus is None:
            gpus = [(g["i"], g["uuid"], g["free"]) for g in info.values()]
        seen, jobs = {}, []                   # seen：我们在跑任务在各卡上实测的显存。nvidia-smi 的空闲已经扣了它，记账在 jobs 里再扣，卡的 free 先加回去
        for j in self.running.values():
            a, vb = self.acct(j), (j.get("use") or {}).get("vram_by") or {}
            for g in j["gpus"]:
                seen[g] = seen.get(g, 0.0) + vb.get(g, 0.0)
            jobs.append({"key": j["key"], "lane": _C.lane_key(j), "project": j.get("project"), "name": j["name"], "t0": j["t0"], "t1": self.run_end(j, now),
                         "gpus": j["gpus"], "vram": a["vram"], "ram": a["ram"], "cpu": a["cpu"], "excl": j.get("excl"), "tag": j.get("tag"),
                         "hold": _C.is_hold(j["name"]), "ngpu": j.get("ngpu", 1), "use": j.get("use") or {}, "cards": self.job_cards(j), "gang": j.get("gang")})
        stuck = {}                            # worker 停不了的保活（ka_stuck，#102）：推演当它一直占着（卡的 other 只有推演用；真放行的空闲是 nvidia-smi 实测、已扣）
        for g in self.gp if self.ka_stuck() else ():
            if g["owner"] == "keepalive":
                stuck[g["uuid"]] = stuck.get(g["uuid"], 0.0) + g["mb"]
        cards = [{"i": i, "uuid": u, "total": (info.get(u) or {}).get("total") or 0.0, "keep": self.keep_left(i, u),
                  "other": self.other["vram"].get(u, 0.0) + stuck.get(u, 0.0), "free": f - self.keep_left(i, u) + seen.get(u, 0.0), "ka": bool(self.other["ka"].get(u))}
                 for i, u, f in gpus]
        return {"cards": cards, "limit": m.get("limit"), "room": self.mem_room(), "margin": ram_margin(m["limit"]) if m.get("limit") else None,
                "cpu_cap": _M.cpu_cap(self.info.get("cpu_quota")), "cpu_room": self.cpu_room(),
                "jobs": jobs, "ext": list(self.ext.values()), "gp": list(gp), "cpu": self.other["cpu"] if self.cpu_n >= 2 else None,
                "holds": self.excl_hold, "tags": limits.get("tags") or {}, "owner": {u for u in self.back_last if u not in ("ram", "cpu")},
                "who": "对方" if self.guest() else "labmon 之外的进程", "kept": self.held(), "cut": None, "brief": False,
                # 成组（#180）：gholds 就是 gang_hold 本身（place 在里面留、放、让）；gangs 组里何时能一起开跑（gang_sync 的 gsim）；gfirst 这一轮最早放得下的时刻（推演里用）
                "gholds": self.gang_hold, "gangs": self.gsim, "gfirst": {}}

    def model(self):
        """这台的 GPU 型号（时长只认同型号的记录）；看不到卡为 None。"""
        return ((self.info.get("gpus") or [{}])[0]).get("name")

    def req_dur(self, r, k=None, src=False, cut=False):
        """请求或运行中任务的预计时长（分钟，duration；估不出为 None）：清单任务取 queue_durs 算好的（按 k_now），单元现算（k 为 1）；
        给了 k（同卡并行数，运行中任务自己的 k_of）时按它挑同类记录，清单任务经 dur_k（与推演同一个缓存）。
        写了依赖的占位任务从开始（还没开始的按现在）到 dep_end 给的放行时刻，ends 用上一次推演汇的。
        成组的成员（不给 k 时）取组里各成员里最长的（gang_dur）：截止前跑不完、回填都按整组算（#180）。
        截止前的准入与回填（fits_before 再乘 DUR_MARGIN）、“开跑先记一笔”与运行中任务的预计结束（run_end，传 k）用。
        src=True 时返回 (分钟, 依据)：截止前跑不完的原因写明估时从哪来（override_hit，#134）。
        cut=True 时给截止前的准入用的时长（duration 的 cut，已含余量，#139），依据是“怎么算的；估时从哪来”。"""
        f = "cut" if cut else "min"
        if k is None and r.get("gang") and r.get("kind", "queue") == "queue":
            d = self.gang_dur(r, f)
            return (d, "成组 %s 里最长的成员" % r["gang"]) if src else d
        if r.get("kind", "queue") != "queue":
            d = duration(self.hist, r, self.model(), 1 if k is None else k)
        elif k is None:
            d = self.durs.get(_C.lane_key(r), {}).get(r["name"])
        else:
            d = self.dur_k(r)(k)
        why = d and ("%s；%s" % (d["cut_src"], d["src"]) if cut else d.get("src"))
        d = d and d[f]
        if _C.is_hold(r["name"]) and r.get("project") and _DP.dep_keys(r):
            t0 = r.get("t0") or _C.time.time()
            e = _DP.dep_end(_DP.dep_keys(r), t0, self.ends, self.dead)
            d, why = None if e is None else (e - t0) / 60 * (_C.DUR_MARGIN if cut else 1), "等依赖放行"
        return (d, why) if src else d

    def reload_hist(self):
        h = _C.load_hist(self.hist_cache)
        if h is not None:
            self.hist, self.hist_v = h, self.hist_v + 1

    def queue_durs(self, queues):
        """各队列清单里每个任务的预计时长（duration，写进状态）与资源估计（estimate），推演（forecast）与截止前的准入用；
        清单、历史与型号都没变就沿用上次的。运行中任务的依赖（deps、deps_any）也按此刻的清单行更新：占位任务跑着时补写的 after= 下一轮就生效
        （它是开跑时从清单行读的，不更新就要等 worker 重启接管）。"""
        lists = {}
        for q in queues:
            if q.get("sleeping"):                 # 休眠队列不读清单与不重算估时（#164）
                continue
            try:
                m = _C.fsig(q["jobs_path"])
            except OSError:
                continue
            c = self.qlists.get(q["key"])
            lists[q["key"]] = c if c and c[0] == m else (m, [(n, _Q.hints(cmd)) for n, cmd in _Q.list_jobs(q)])
        self.qlists = lists
        for j in self.running.values():
            k = _C.lane_key(j)
            h = _Q.first_rows(lists[k][1]).get(j["name"]) if k in lists else None
            if h:
                j["deps"], j["deps_any"] = h["deps"], h["deps_any"]
        kn, runs = self.k_now(), self.run_tot()
        # 运行中任务那一行按它自己的 k（k_of，同 run_end；dur_k 按取整的 k 缓存）：看板据它算剩余、写回 eta.json，不能按给新任务的 k_now（#138）
        rk = {k: conc_key(self.k_of(j)) for k, j in self.running.items() if not _C.is_hold(j["name"])}
        key = (self.hist_v, self.model(), tuple(sorted((k, c[0]) for k, c in lists.items())), kn, tuple(sorted((k, tuple(v)) for k, v in runs.items())),
               tuple(sorted((k, self.running[k]["name"], c) for k, c in rk.items())))
        if key == self.durs_key:
            return
        self.durs_key = key
        self.durs, self.qest, self.durk, memo = {}, {}, {}, {}
        for q in queues:
            if q.get("sleeping"):
                continue
            out = self.durs[q["key"]] = {}
            est = self.qest[q["key"]] = {}
            mine = {j["name"]: (j, rk[k]) for k, j in self.running.items() if k in rk and _C.lane_key(j) == q["key"]}   # 这条清单在跑的（并行清单有几行，#147）
            for n, h in lists.get(q["key"], (0, []))[1]:
                d = self.dur_k(mine[n][0])(mine[n][1]) if n in mine else self.dur_k(dict(h, name=n, project=q["project"], root=q["root"], queue=q["jobs"]))(kn)
                if d:
                    out[n] = dict(d, min=round(d["min"], 1))
                k = (q["key"], h["tag"], _C.base_name(n), h["ram"], h["vram"], h["cpus"])      # 同类任务的估计一样，清单长时省得每个都扫一遍历史
                if k not in memo:
                    memo[k] = estimate(self.hist, dict(h, name=n, project=q["project"], root=q["root"], queue=q["jobs"]))
                est[n] = memo[k]

    def k_now(self):
        """此刻放一个新任务会遇到几条同卡的 labmon 任务（含自己）：最空的那张卡上在跑的（占位任务不算）+ 1；没有卡为 1。
        清单任务写进状态的预计时长、截止前的准入（req_dur）按它挑同类记录（duration 的 k，#67；准入在选卡前判截止，按最空的卡）。"""
        cards = [g["uuid"] for g in self.info.get("gpus") or []]
        return min((1 + sum(1 for j in self.running.values() if not _C.is_hold(j["name"]) and u in (j.get("gpus") or [])) for u in cards), default=1)

    def k_of(self, j):
        """运行中任务自己的同卡并行数（含自己）：measure 记的 use.conc（按时间加权，与用量行的 conc 同口径，duration 挑记录比的就是它）；
        还没量到时按此刻它那几张卡上的 labmon 任务数（占位任务不算，多卡取平均）；不用卡的为 1。
        运行中任务的预计结束（run_end）与读写拥塞嫌疑（io_suspect）按它挑同类记录，不按给新任务的 k_now（#86）。"""
        c = (j.get("use") or {}).get("conc")
        on = j.get("gpus") or []
        if c or not on:
            return c or 1
        return _M.card_conc(j, on, self.running.values())

    def run_tot(self):
        """在跑任务按日志进度估的总时长（模块级 run_tot，fit 也用）。"""
        return run_tot(self.running.values(), self.eta, self.eta_t)

    def dur_k(self, r):
        """清单里一个任务在同卡有 k 条 labmon 任务（含自己）时的预计时长（duration 的结果；估不出为 None）：返回 k → 结果 的函数，
        推演按开跑时那几张卡上的任务数调它（job_spec 的 durk）。r 是清单行（hints 加 name、project、root、queue）、请求或运行中任务（清单在 q 里）：
        tag 与所在清单要传进 duration，同类没有历史时借同 tag、同一清单的比值（#113）。
        按 (任务, k) 缓存，清单、历史、在跑的变了（queue_durs 的 durs_key）就清掉。"""
        q = r.get("q") or {}
        x = {"name": r["name"], "project": r.get("project"), "est_min": r.get("est_min"), "tag": r.get("tag"),
             "root": r.get("root") or q.get("root"), "queue": r.get("queue") or q.get("jobs")}
        run = [m for n, m in self.run_tot().get((x["project"], _C.base_name(x["name"])), []) if n != x["name"]]
        ck = (x["project"], x["name"], x["est_min"], x["tag"], x["root"], x["queue"])

        def f(k):
            k = conc_key(k)
            c = self.durk.get(ck + (k,), 0)
            if c == 0:
                c = self.durk[ck + (k,)] = duration(self.hist, x, self.model(), k, run)
            return c
        return f

    def load_eta(self):
        """看板写来的运行中任务预计结束 {"t": 写的时刻, "jobs": {"项目|任务": 时刻}}（项目看板按日志进度算的，比时长估计准）；文件变了才重读。"""
        try:
            m = _C.fsig(self.p.eta)
        except OSError:
            self.eta, self.eta_t, self.eta_m = {}, 0, None
            return
        if m != self.eta_m:
            try:
                d = json.loads(_C.read(self.p.eta, "{}") or "{}")
            except ValueError:
                d = {}
            self.eta, self.eta_t, self.eta_m = d.get("jobs") or {}, d.get("t") or 0, m

    def run_end(self, j, now):
        """运行中任务的预计结束：看板写来的（任务开始后写的才算这一次）、否则 开始 + 预计时长；超了预计的剩余按 overrun_left 估（已跑的 20%、至少 10 分钟），
        不当“马上结束”。时长不明的（没估时也没有同类历史）为 None。
        占位任务不看看板写来的（它没有日志进度，看板的就是推演的回声）；写了 after= 的等的都已结束时算现在（它下一次检查就放行）。"""
        hold = _C.is_hold(j["name"])
        e = None if hold else self.eta.get("%s|%s" % (j.get("project"), j["name"]))
        d = self.req_dur(j, self.k_of(j))
        end = e if e and self.eta_t >= j["t0"] else j["t0"] + d * 60 if d is not None else None
        if end is None or end > now:
            return end
        return now if hold and _DP.dep_keys(j) else now + _C.overrun_left(now - j["t0"])

    # ---- 调度设置与公平份额 ----
    def policy(self):
        """调度设置：看板写来的 sched.json（用户级主机清单的 [sched]），没写的用缺省（POLICY）。返回 {cap 秒, hl 秒, w {项目: 权重}, own {项目: 人员键}}。
        own 是看板按项目主人写的（owner，#23；人员名的短哈希，不是名字）：同一个键的几个项目在 fair_key 里合起来算一份。"""
        try:
            d = json.loads(_C.read(self.p.sched, "{}") or "{}")
        except ValueError:
            d = {}
        own = d.get("owner") if isinstance(d.get("owner"), dict) else {}
        return {"cap": float(d["wait_cap_min"]) * 60 if d.get("wait_cap_min") else _C.POLICY["cap"],
                "hl": float(d["half_life_h"]) * 3600 if d.get("half_life_h") else _C.POLICY["hl"],
                "w": {str(k): float(v) for k, v in (d.get("weight") or {}).items()},
                "own": {str(k): str(v) for k, v in own.items() if v}}

    def cards(self, ngpu, vram_gib, excl):
        """这台主机上一个任务占几张卡（card_share；卡的总显存取这台最小的那张）。"""
        gs = self.info.get("gpus") or []
        return card_share(ngpu, vram_gib, excl, min((g.get("total") or 0) for g in gs) / 1024 if gs else None, len(gs))

    def job_cards(self, j):
        """运行中任务占几张卡（cards）：按所用的卡数、显存估计值与独占算。份额、快照与用量记录共用。"""
        return self.cards(len(j["gpus"]), (j.get("est") or {}).get("vram") or 0.0, j.get("excl"))

    def share(self, now, pol):
        """各项目衰减后的 GPU 用量（卡·时，usage）：各主机跑完的（usage.jsonl）、别的主机在跑的（peers）与本机在跑的（own_runs，按预计结束先记一笔）。"""
        if self.hrows_v != self.hist_v:
            self.hrows_v, self.hrows = self.hist_v, []
            for h in self.hist:
                # 只认记了 project 的行（看板的项目 id）：没记的旧行按目录名猜出来的（如 project-a）与项目 id 对不上，
                # 份额里会冒出幽灵项目，所以不计（只留给估时用，hist_key）；半衰期 12 小时，它们很快就不起作用
                n, p = h.get("cards"), h.get("project")
                if n and p and h.get("t1") and h["t1"] > now - 30 * 86400:
                    self.hrows.append((p, n, h["t0"], h["t1"]))
        return usage(self.hrows + self.peers(now) + self.own_runs(now), now, pol["hl"])

    def own_runs(self, now):
        """本机在跑的任务 [(项目, 卡数, 开始, 预计结束)]：算用量，也写进状态的 share.run 给别的主机。"""
        return [(j.get("project"), round(self.job_cards(j), 3), round(j["t0"]), round(self.run_end(j, now) or now))
                for j in self.running.values() if not _C.is_hold(j["name"])]

    def peers(self, now):
        """别的主机在跑的任务 [(项目, 卡数, 开始, 预计结束)]：各自状态里的 share.run（共享盘上同一个 HOME，PEER_EVERY 秒读一次；
        STATUS_STALE_S 秒没更新的状态不算，那台 worker 不在）。同时记下各自推演（plan，含在跑的 run 条）里每个任务的预计结束
        （peer_end）与还会跑的任务（状态里的 todo，见 list_state；peer_todo）：依赖等别的主机上的任务时查（dep_state、forecast）。
        todo 在那台 worker 不在时（状态没更新、又没有 worker stop 留下的 stopped）DEP_STALE_S 内照样算：续租、重启后那些任务还会跑；
        worker stop 过的（含借用归还）不算。另记下与本机在同一物理机上（状态里共享盘的 clientaddr 相同）的别的主机在跑的任务（peer_box，
        读写拥塞嫌疑的事件里列出）：29、29-15158、29-15159 这类同一物理机的几个容器读到的共享盘计数是同一份，按 addr 归到一起、不相加。
        没过期、没 worker stop 过的状态整份留在 peer_st（offers 读别的主机的清单、推演与临时指令），这些主机登记的清单记进 peer_regs（registry 认同一条清单归谁）。
        一并读看板写来的 returned.json（主机清单里已归还的，平台上关了机的自用主机也标成归还）进 gone：只给 offers 用，依赖照旧按 peer_todo。
        有没有更新看状态里那台 worker 自己写的时刻 t，不看文件修改时间：修改时间是共享盘服务器的时钟，比各容器慢 3 分多钟，看上去总超过 STATUS_STALE_S。"""
        if now - self.peer_t >= _C.PEER_EVERY:
            self.peer_t, self.peer, self.peer_end, self.peer_todo, self.peer_box, self.peer_st = now, [], {}, [], [], {}
            try:
                self.gone = set(json.loads(_C.read(self.p.returned, "{}") or "{}").get("hosts") or [])
            except (ValueError, AttributeError, TypeError):
                self.gone = set()
            addr = ((self.info.get("press") or {}).get("nfs") or {}).get("addr")
            for f in glob.glob(os.path.join(_C.HOME, "hosts", "*", "status.json")):
                try:
                    if f == self.p.status:
                        continue
                    st = json.loads(_C.read(f, "{}") or "{}")
                    d, age = os.path.dirname(f), now - (st.get("t") or 0)
                    if age <= _C.DEP_STALE_S and not os.path.exists(os.path.join(d, "stopped")):
                        self.peer_todo += [(os.path.basename(d), age, st["t"], ln) for ln in st.get("todo") or []]
                    if age > _C.STATUS_STALE_S:
                        continue
                    if not os.path.exists(os.path.join(d, "stopped")):
                        self.peer_st[os.path.basename(d)] = st
                    self.peer += [tuple(x) for x in (st.get("share") or {}).get("run") or []]
                    self.peer_end.update(((ln.get("project"), j["name"]), j.get("t1"))
                                         for ln in (st.get("plan") or {}).get("lanes") or [] for j in ln.get("jobs") or [])
                    if addr and (((st.get("info") or {}).get("press") or {}).get("nfs") or {}).get("addr") == addr:
                        self.peer_box += [(st.get("host") or os.path.basename(d), j.get("project"), j["name"]) for j in st.get("running") or [] if not j.get("hold")]
                except (OSError, ValueError, TypeError, AttributeError, KeyError):
                    continue
            self.peer_todo += self.pool_todo(now)
            self.peer_regs = {}
            for h in self.peer_st:
                for o in _Q.registry(os.path.join(_C.HOME, "hosts", h, "queues"), read_env=False)[0]:   # 只认那台自己也会跑的：坏了的登记（缺 project 等）那台跳过，不读 .env
                    try:
                        self.peer_regs.setdefault((os.path.normpath(o["root"]), o["jobs"]), []).append((os.stat(o["file"]).st_mtime, h))
                    except OSError:                   # 刚被删掉的
                        continue
        return self.peer

    def pool_todo(self, now):
        """算力池里待放的清单还会跑的行（#24）：peers 一起读，按别的主机的 todo 的样子 [("pool", 0, now, 清单项)] 记进 peer_todo，
        别处 after= 等它时算还会跑（不报找不到，dep_note 写在池里等）。池目录没有 worker、没有状态，按登记与清单的状态目录现读（清单按修改时间缓存）。
        交给池之前在别的主机上放行、还没收尾的那一格（状态目录的 current，run_elsewhere 认：放它的主机没归还）不算池里的：它在那台跑着
        （那台状态 todo 里列着），这条清单在它跑完之前放到哪台都一行开不了，队首取它后面那行、记 elsewhere [主机, 任务]，看板等它跑完再放（#80）。
        跑空满 IDLE_REG_S（2 天，#164）的池清单同样自动休眠：之后每轮只用 file_sig 看清单文件变没变，不读 .env 与状态目录，文件一变自动醒来。"""
        out, self.pool_heads = [], []
        if not hasattr(self, "pool_idle"):
            self.pool_idle, self.pool_reg_cache, self.opars = {}, {}, {}
        qdir = os.path.join(_C.HOME, "hosts", _C.POOL, "queues")
        seen_k = set()
        for f, reg, _rsig, _mtime, changed in _Q.regs(qdir, cache=self.pool_reg_cache, with_meta=True):
            if isinstance(reg, Exception) or not (reg.get("root") and reg.get("jobs") and reg.get("project")):
                continue
            key, root, jobs = reg["_slug"], reg["root"], reg["jobs"]
            seen_k.add(key)
            if changed:
                self.pool_idle.pop(key, None)
            idle = self.pool_idle.get(key)
            jpath = os.path.join(root, jobs)
            if idle and idle.get("sleep"):
                if _P.file_sig(jpath) == idle.get("sig"):
                    self.pool_heads.append({"project": reg["project"], "root": root, "queue": jobs, "name": None, "n": 0,
                                            "held": None, "elsewhere": None, "sleeping": True})
                    continue
                self.pool_idle.pop(key, None)
            try:
                q = _Q.load_queue(reg)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            jsig0 = _P.file_sig(q["jobs_path"])             # 读清单前的签名：进休眠前复核一致，避免读后追加的新行被吞（#164）
            rows = self.list_of(q["jobs_path"])
            if rows is None:                                # 这台读不到清单文件：照样列出来（带原因），看板取读得到的那台的（#86）
                self.pool_idle.pop(key, None)
                self.pool_heads.append({"project": q["project"], "root": q["root"], "queue": q["jobs"], "name": None, "n": 0, "held": None,
                                        "elsewhere": None, "err": "%s 上读不到清单 %s" % (self.host, q["jobs_path"])})
                continue
            away = [(c, h) for c in _Q.read_current(q["state"]) for h in [_Q.run_elsewhere(_C.POOL, q, c, self.gone)] if h]   # 并行清单可能有几行（#147）
            cur, other = away[0] if away else (None, None)
            ms, left = _Q.rows_left(q["state"], rows, {c[0] for c, _ in away})
            n, h = left[0] if left else (None, {})          # 队首：看板放之前看它还有没有、依赖满足没有（pool_view）；都跑完了的也列（n 为 0）
            jsig = _P.file_sig(q["jobs_path"])
            cp = self.opars.get(q["jobs_path"])
            if cp and cp[0] == jsig:
                par = cp[1]
            else:
                par = _Q.list_par(_C.read(q["jobs_path"], "") or "")   # 并行清单（#147）：窗口里前 par 行哪一行依赖满足都能放
                self.opars[q["jobs_path"]] = (jsig, par)
            paused = "paused" if ms and "paused" in ms else None
            sleeping = False
            if not left and not away and not paused:
                ent = self.pool_idle.get(key)
                if ent is None:
                    since = self._idle_start(q, now, False) if hasattr(self, "_idle_start") else now
                    ent = self.pool_idle[key] = {"since": since, "sig": None, "sleep": False}
                if now - ent["since"] >= _P.IDLE_REG_S and jsig0 is not None and jsig == jsig0:
                    ent["sig"] = jsig0
                    ent["sleep"] = True
                    sleeping = True
            else:
                self.pool_idle.pop(key, None)
            self.pool_heads.append({"project": q["project"], "root": q["root"], "queue": q["jobs"], "name": n, "n": len(left),
                                    "deps": h.get("deps"), "deps_any": h.get("deps_any"), "held": paused,
                                    "elsewhere": [other, cur[0]] if other else None, "win": left[:par] if par > 1 else None,
                                    **({"sleeping": True} if sleeping else {})})
            if not left:
                continue
            ln = {"project": q["project"], "root": q["root"], "queue": q["jobs"], "jobs": [n for n, _ in left], "gang": {},
                  "deps": {n: [[p, x] for p, x, _ in d] for n, h in left for d in [_DP.dep_keys(dict(h, project=q["project"]))] if d},
                  "held": paused, **({"par": par} if par > 1 else {})}
            out.append((_C.POOL, 0.0, now, ln))
        self.pool_idle = {k: v for k, v in self.pool_idle.items() if k in seen_k}
        return out

    def cpu_hints(self, S, util):
        """运行中任务开跑核数给少、给多（cpu_fit，#46）：同一次运行一旦判出来就留到跑完（j 的 cpu_fit，写进状态、接管时找回）。
        测量在门槛两侧来回时不反复出现、消失：看板的 cpu_low:、cpu_high: 只在条件出现时叫醒一次，消失后 6 小时内再出现不再叫醒（waits._new_alerts）。
        给少了的建议核数按这台此刻的快照 S 封顶（cpu_budget：配额减同机别的任务已记的、同类几条同时在跑就均分，#112）。
        fit 开车道（gpu_slack）要的是此刻的样子，照旧现算，不用这份。"""
        for j in self.running.values():
            if not j.get("cpu_fit"):
                j["cpu_fit"] = cpu_fit(j, S["cpu_cap"], util, cpu_budget(S, j))

    def io_suspect(self, now):
        """读写拥塞嫌疑（只记录、不拦，#4 维护者定）：在跑的任务已跑过同类历史预计时长（duration 的 same）的 SLOW_RATIO 倍，
        同时共享盘慢（nfs_slow），记一条 io_suspect 事件；同一任务只记一次（io_sus，写进状态、接管时找回）。不改准入与推演。
        事件里列出同一物理机上在跑的别的任务（本机的与 peers 读的 peer_box）；共享盘的延迟与次数只用本机这一份，不把同机几个容器的同一份计数相加。"""
        nf = self.slow
        if not nf:
            return
        for j in list(self.running.values()):
            if j.get("io_sus") or _C.is_hold(j["name"]):
                continue
            d = duration(self.hist, {"name": j["name"], "project": j.get("project"), "est_min": j.get("est_min"), "kind": j.get("kind", "queue")}, self.model(),
                         self.k_of(j))          # 按它自己的同卡并行数比：挤着跑的不和单跑的记录比（#86）
            if not d or not d["same"] or now - j["t0"] < _C.SLOW_RATIO * d["min"] * 60:
                continue
            j["io_sus"] = round(now)
            box = [x["name"] for x in self.running.values() if x is not j and not _C.is_hold(x["name"])] + ["%s 的 %s" % (h, n) for h, _, n in self.peer_box]
            self.event("io_suspect", "读写拥塞嫌疑：%s 已跑 %.0f 分钟，同类历史约 %.0f 分钟（%s）；共享盘此刻 %.0f ms/次（本机基线 %.0f），%.1f 次/秒；同一物理机上还在跑：%s" % (
                j["name"], (now - j["t0"]) / 60, d["min"], d["src"], nf["ms"], nf["base"], nf["ops"], "、".join(box[:6]) or "没有别的任务"),
                name=j["name"], project=j.get("project"), ms=nf["ms"], base=nf["base"], box=box[:12])
