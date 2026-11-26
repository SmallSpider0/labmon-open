"""worker 开并行车道与算力池选台评估（fit / fit_list / fit_head / FitHost）。"""
import glob
import json
import math
import os
import types

from . import common as _C
from . import pure as _P
from . import queue as _Q
from . import sched as _S
from . import core as _W

def known_lists(p):
    """fit 用：登记过的清单 [(主机, 队列, 清单行 list_jobs)]：本机（p）的在前，别的主机（共享 HOME 下的 hosts/*/queues）按主机名排在后面，
    各按登记文件名的顺序。一次 fit 调用只读一遍。"""
    mine = os.path.normpath(p.queues)
    dirs = [mine] + sorted(d for d in glob.glob(os.path.join(_C.HOME, "hosts", "*", "queues")) if os.path.normpath(d) != mine)
    return [(os.path.basename(os.path.dirname(d)), q, _Q.list_jobs(q)) for d in dirs for q in _Q.registry(d)[0]]


def list_row(lists, name, project=None):
    """known_lists 里叫 name 的那一行，取第一条；写了 project 的只认这个项目的清单。返回 (主机, 队列, 命令)，找不到为 None。"""
    for host, q, rows in lists:
        cmd = None if project and q["project"] != project else _Q.job_cmd(q, name, rows)
        if cmd is not None:
            return host, q, cmd
    return None


def fit_snapshot(st):
    """fit 用：一台主机的 status.json → 准入核心 place 认的资源快照（字段见 _place；与 Worker.snapshot 同形）。
    卡的空闲取状态的 room.gpus（已扣在跑任务的记账与给对方留的），加回保活占的（放任务前 worker 先停保活；停不了的 keepalive.stuck 不加回，#102），给对方留的照 room.keep（整卡封顶要扣）；在跑的只记内存与核（显存已扣在卡上），
    room、cpu_room 是 labmon 全停时能给的（room 加回在跑的记账：ram_need、cpu_need 按它封顶）；tag 上限照 limits；对方超出预留与有停不了保活的卡
    照 room.owner 不放（旧 worker 没写为空）。独占留卡、成组留的资源不在状态里，不模拟。"""
    room, info = st.get("room") or {}, st.get("info") or {}
    ka = {}                                   # worker 放任务前先停的保活占的（加回卡的空闲）；停不了的（keepalive.stuck，#102）不加回
    for g in st.get("gpu_procs") or [] if not (st.get("keepalive") or {}).get("stuck") else ():
        if g.get("owner") == "keepalive":
            ka[g["uuid"]] = ka.get(g["uuid"], 0.0) + g["mb"]
    acct = lambda j, k: (j.get("acct") or {}).get(k) or 0.0
    jobs = [{"key": "run:%d" % i, "project": j.get("project"), "name": j["name"], "t0": j.get("t0"), "t1": None, "gpus": j.get("gpus") or [],
             "vram": {}, "ram": acct(j, "ram"), "cpu": acct(j, "cpu"), "excl": j.get("excl"), "tag": j.get("tag"), "hold": bool(j.get("hold")),
             "ngpu": len(j.get("gpus") or []), "use": j.get("use") or {}, "cards": 0.0} for i, j in enumerate(st.get("running") or [])]
    by = {j["name"]: j for j in jobs}
    gp = [dict(g, _j=by.get(g.get("job")) if g.get("owner") == "job" else None) for g in st.get("gpu_procs") or []   # 独占的判断（intruders）要看卡上有谁；
          if g.get("owner") != "keepalive"]                     # worker 停得了的保活放任务前先停（卡的空闲已加回），停不了的是 other、照算
    cards = [{"i": g["i"], "uuid": g["uuid"], "total": g.get("total") or 0.0, "keep": (room.get("keep") or {}).get(g["uuid"], 0.0), "other": 0.0, "ka": False,
              "free": (room.get("gpus") or {}).get(g["uuid"], g.get("free") or 0.0) + ka.get(g["uuid"], 0.0)} for g in info.get("gpus") or []]
    return {"cards": cards, "limit": (info.get("mem") or {}).get("limit"),
            "room": None if room.get("ram") is None else room["ram"] + sum(j["ram"] for j in jobs), "margin": room.get("margin") or 0,
            "cpu_cap": room.get("cpu_cap"), "cpu_room": None if room.get("cpu") is None else room["cpu"] + sum(j["cpu"] for j in jobs),
            "jobs": jobs, "ext": st.get("external") or [], "gp": gp, "cpu": room.get("other_cpu"), "holds": {}, "tags": (st.get("limits") or {}).get("tags") or {},
            "owner": set(room.get("owner") or []), "who": "对方" if st.get("guest") else "labmon 之外的进程",
            "kept": bool((st.get("guest") or {}).get("keep")), "cut": None, "brief": False, "gholds": {}, "gangs": {}, "gfirst": {}}


class FitHost:
    """fit 眼里的一台主机：它的状态（status.json）拼成的快照（fit_snapshot），这次 fit 假设开的车道按 acct_job 记进快照再判下一行（与推演同一做法）。
    请求拼法与临时指令借 Worker 的 job_spec、override_hit（只填它们要的 info、ovr、host），不另写。"""

    def __init__(self, host, st, hist, eta):
        self.host, self.st, self.hist, self.eta = host, st, hist, eta
        self.w = object.__new__(_W.Worker)
        self.w.host, self.w.info, self.w.ovr = host, st.get("info") or {}, st.get("overrides") or []
        self.w.req_dur = lambda r, src=False, cut=False: ((r.get("cut"), r.get("cut_src")) if src else r.get("cut")) if cut else r.get("dur")   # override_hit 判截止前跑不跑得完要的时长与依据：fit 拼的请求里已有
        self.S = fit_snapshot(st)
        u10 = st.get("util10") or {}
        self.cards = [dict(c, util=u10.get(c["uuid"])) for c in self.S["cards"]]
        self.slack = _S.gpu_slack(self.cards, st.get("running") or [], st.get("throttle"), self.S["cpu_cap"])
        self.lanes, self.say, self.why = [], [], None     # 这次假设开的 [(任务, 卡号)] 与各行的说明；最近一行没开成的原因
        self.solo, self.solo_host = {}, None              # 这次分给只有提示值的行的卡 {uuid: 任务}（之后哪一行都不与它同卡）；不用卡的那种占了整台：任务名

    def card_lines(self, project=None):
        """各卡此刻的样子（可用显存、利用率、在跑谁、CPU 受限）、这台的 CPU 配额与记账（cpu_line，#112）与限流：fit 先列出来。
        给了 project（fit --list）时，非本项目的在跑任务只报聚合数（他项 N 跑），不输出他人任务名（#197）。"""
        free, out = _S.card_free(self.S), []
        by_proj = {j["name"]: j.get("project") for j in self.S["jobs"]} if project is not None else None
        for c in self.cards:
            if project is None:
                jobs = [j["name"] for j in self.S["jobs"] if c["uuid"] in j["gpus"]]
                on_card = "（在跑 %s）" % "、".join(jobs) if jobs else "（空着）"
            else:
                mine = [j["name"] for j in self.S["jobs"] if c["uuid"] in j["gpus"] and j.get("project") == project]
                other_n = sum(1 for j in self.S["jobs"] if c["uuid"] in j["gpus"] and j.get("project") != project and not j.get("hold"))
                bits = (["在跑 " + "、".join(mine)] if mine else []) + (["他项 %d 跑" % other_n] if other_n else [])
                on_card = "（%s）" % "；".join(bits) if bits else "（空着）"
            b = self.slack["bound"].get(c["uuid"])
            if b and by_proj is not None:
                b = [(n if by_proj.get(n) == project else "他项任务", avg, cap) for n, avg, cap in b]
            out.append("卡 %d：可用 %.1f GiB，近 10 分钟利用率 %s%s%s" % (
                c["i"], max(0.0, free[c["uuid"]]) / 1024, "%d%%" % c["util"] if c["util"] is not None else "—（样本不够）",
                on_card,
                "；%s 跑满了给的核（CPU 受限）" % "、".join("%s %.1f/%d 核" % x for x in b) if b else ""))
        if _S.cpu_line(self.st):
            out.append(_S.cpu_line(self.st))
        if self.slack["throttle"] is not None:
            out.append(self.slack["throttle"])
        return out

    def req(self, q, n, cmd, project):
        """清单 q 里叫 n 的一行（命令 cmd）在这台上的样子：(请求 r, 资源估计 e, place 认的 x)。q 是别的主机上登记的，--gpus 限卡只在那台上算。"""
        r = _Q.queue_req(q, _Q.job_of(n, cmd))
        if project:
            r["project"] = project
        e = _S.estimate(self.hist, r)
        # 时长同真放行（queue_durs）：按此刻放一个新任务会遇到的同卡并行数（最空的那张卡，同 k_now；这次假设开的也算）与在跑同类的进度挑记录
        kn = min((1 + sum(1 for j in self.S["jobs"] if not j["hold"] and c["uuid"] in j["gpus"]) for c in self.S["cards"]), default=1)
        d = _S.duration(self.hist, r, self.model(), kn, self.runs(r))
        x = dict(self.w.job_spec(n, r, e, d and d["min"], cut=d and d["cut"]), key="fit:%s" % n, project=r["project"], cut_src=d and "%s；%s" % (d["cut_src"], d["src"]),
                 allowed=q["gpus"] if q.get("_host") == self.host else None)
        return r, e, x

    def model(self):
        return ((self.w.info.get("gpus") or [{}])[0]).get("name")

    def runs(self, r):
        """这台在跑的同类（项目 + 去掉种子编号的名字）按进度估的总长（模块级 run_tot，与 worker 同口径；看板写来的 eta.json），不含它自己。"""
        p = self.eta.get("prog")
        prog = set(p) if isinstance(p, list) else set()
        return [m for y, m in _S.run_tot(self.st.get("running") or [], self.eta.get("jobs") or {}, self.eta.get("t") or 0, prog).get((r["project"], _C.base_name(r["name"])), [])
                if y != r["name"]]

    def blocked(self, project):
        """这台此刻不开新车道的整台原因：持久的临时指令挡住这个项目（drain、只给别的项目用、暂停，override_hit 的 lasting）、
        借来的主机上对方的内存或 CPU 超出预留、刚回落（真放行的 guest_block，状态的 guest.block）、容器在限流；没有为 None。"""
        hit = self.w.override_hit({"project": project, "name": ""}, ("drain", "guest", "pause"), lasting=True)
        if hit:
            return hit[1]
        block = (self.st.get("guest") or {}).get("block")       # 只有内存、CPU 超出（或刚回落）挡整台；某张卡超出的经 room.owner 只挡那张
        if block:
            return block + " → 这台不开新车道"
        if self.slack["throttle"] is not None:
            return self.slack["throttle"] + " → 不开新车道：先减线程（按 LABMON_CPUS 设线程数与 DataLoader 进程数）"
        return None

    def open(self, r, e, x, t, row=True):
        """为这一行再开一条车道放不放得下：放得下返回说明并把它记进快照（acct_job）；放不下返回 (None, 原因)。
        判断就是 place；fit 只多两条：估计只有提示值的（没有实测）只放空卡、不与别的任务同卡（不用卡的只在这台没有 labmon 任务时开；
        这次 fit 先分了它，后面的行也不与它同卡、同台：solo）；
        利用率低的卡上在跑的任务被 CPU 卡住时不在那张卡上开（gpu_slack）。"""
        why = self.blocked(x["project"])
        hit = None if why else self.w.override_hit(x, ("deadline",), lasting=True)    # 续租、约定归还的截止前跑不完的不放（同真放行，按这一行的预计时长）
        if why or hit:
            return None, why or hit[1]
        if self.solo_host:
            return None, "这次已把没有实测的 %s 分到这台（只在这台没有别的 labmon 任务时开） → 不再与它同开" % self.solo_host
        S, gpu = self.S, x["ngpu"] > 0
        hint = e["src" if gpu else "ram_src"] == "提示值"
        # 有别的任务的卡：labmon 在跑的、旧 gpu_queue.sh 的、卡上 labmon 之外的进程（保活不算：放任务前 worker 先停它）
        busy = ({u for j in S["jobs"] if not j["hold"] for u in j["gpus"]} | {u for j in S["ext"] for u in j.get("gpus") or []}
                | {g["uuid"] for g in self.st.get("gpu_procs") or [] if g.get("owner") != "keepalive"})
        free = _S.card_free(S)
        S0 = dict(S, holds={})
        full = _S.place(dict(x), S0, t)
        if full[0] is None and (S0["code"] or "").startswith("nofit"):    # labmon 全停也放不下（nofit）：这台跑不了，排在已有车道后面也不会跑（#94）
            return None, full[1] + " → 这台跑不了"
        if gpu:
            sel = [c["i"] for c in S["cards"] if (x["allowed"] is None or c["i"] in x["allowed"])
                   and c["uuid"] not in self.slack["bound"] and c["uuid"] not in self.solo and not (hint and c["uuid"] in busy)]
            cs, why, _ = _S.place(dict(x, allowed=sel), dict(S, holds={}), t)
            solo = [self.solo[u] for u in (c["uuid"] for c in (full[0] or [])) if u in self.solo]
            if cs is None and solo and not hint:
                return None, "这次已把没有实测的 %s 分到卡 %s → 不与它同卡" % ("、".join(solo), ",".join(str(c["i"]) for c in full[0] if c["uuid"] in self.solo))
        elif hint and any(not j["hold"] for j in S["jobs"]):
            cs, why = None, None
        else:
            cs, why = full[0], full[1]
        if cs is None:
            if full[0] is None:
                return None, full[1]
            c = full[0][0] if full[0] else None
            b = self.slack["bound"].get(c["uuid"]) if c else None
            if b and not hint:
                left = _S.cpu_free(S)
                by_proj = {j["name"]: j.get("project") for j in S["jobs"]}
                b_show = [(n if not x.get("project") or by_proj.get(n) == x["project"] else "他项任务", avg, cap) for n, avg, cap in b]
                return None, "卡 %d 近 10 分钟利用率 %d%%，低是因为在跑的 %s跑满了给它的核 → 不在这张卡上开新车道：先给它那条车道加核（清单行 cpus=）%s" % (
                    c["i"], next(x_["util"] for x_ in self.cards if x_["uuid"] == c["uuid"]),
                    "、".join("%s（近 10 分钟平均 %.1f 核，给了 %d 核）" % y for y in b_show),
                    "；这台还能给 %.0f 核" % left if left is not None and left >= 1 else "；这台已没有余核，瓶颈在 CPU")
            k, v = ("vram", r["vram"]) if gpu else ("ram", r["ram"])
            return None, "%s，%s → 不开新车道（%s），排在已有车道后面%s" % (
                ("没有同 tag、同名任务的实测" if row else "没有同类任务的历史") if v else
                "没有同类任务的历史%s" % ("（按 tag、去掉种子编号后的名字、同一条队列找）" if row else ""),
                "清单写了 %s=%g，还没有实测%s" % (k, v, "（同一条清单里别的任务的历史不用）" if row else "") if v else "估不出%s" % ("显存" if gpu else "内存"),
                "没有实测的只放空卡、不与别的任务同卡：卡上都有任务在跑" if gpu else "没有实测的只在这台没有 labmon 任务在跑时开",
                "；名字各异的同类任务写同一个 tag= 就认得出" if row and not r["tag"] else "")
        rneed, rsrc = _S.ram_need(x, S)
        need, csrc = _S.cpu_need(x, S)
        if gpu:
            c = cs[0]
            k = sum(c["uuid"] in j["gpus"] for j in S["jobs"] if not j["hold"]) + 1     # 与 worker 同口径：这台的型号、在跑同类按进度估的（eta.json）
            d = _S.duration(self.hist, r, self.model(), k, self.runs(r))
            util = next(y["util"] for y in self.cards if y["uuid"] == c["uuid"])
            say = "卡 %s 可用 %.1f GiB，要 %.1f GiB（%s，含另留的 1 GiB）；近 10 分钟利用率 %s%s%s" % (
                ",".join(str(y["i"]) for y in cs), free[c["uuid"]] / 1024, _S.need_mb(x["vram"]) / 1024, x["src"], "%d%%" % util if util is not None else "—",
                "；它单跑时利用率约 %d%%" % e["util"] if e["util"] is not None else "", "；同卡 %d 条时约要 %.0f 分钟（%s）" % (k, d["min"], d["src"]) if d else "")
        else:
            say = "不用 GPU，内存要 %.0f GiB（%s）、%d 核（%s）" % (rneed, rsrc, need, csrc)
        S["jobs"].append(_S.acct_job(x, cs, t, None, S["cpu_cap"]))
        self.lanes.append((r["name"], [y["i"] for y in cs]))
        if hint:                                          # 没有实测的不与别的同卡（同一次 fit 后面分的也算）
            self.solo.update({y["uuid"]: r["name"] for y in cs})
            self.solo_host = None if gpu else r["name"]
        return say, None


def fit_deps(p, q, rows, hist, now):
    """fit --list 判清单 q（行 rows：list_jobs）里写了依赖的行用的 Worker：与真放行同一份 dep_state（本机这条清单的标记、各主机状态的 todo、
    算力池、用量记录），之后 dep_block 判前置满没满足、dep_waiters 看谁在等哪一行（#144）。只填这几处要的字段，不另写一套依赖判断；
    状态文件都按别的主机读（这台自己的 status.json 也读：它的 todo 里有别的清单还会跑的行）。"""
    w = object.__new__(_W.Worker)
    w.host, w.p, w.info, w.ovr, w.running, w.hist = p.host, types.SimpleNamespace(status=None, returned=p.returned), {}, [], {}, hist
    w.qlists, w.olists, w.pool_heads, w.dep_miss, w.peer_t = {q["key"]: (None, [(n, _Q.hints(c)) for n, c in rows])}, {}, [], {}, -math.inf
    w.dep_state([q], now)
    w.ends = w.dep_base_ends()                # 与推演共用同一份基础依赖结束表：等待原因写“预计几点”（#166）
    return w


def fit_status(p, host, now):
    """fit 读一台的 status.json：(状态, 原因)；读不到或超过 STATUS_STALE_S 没更新的状态为 None、原因写明。"""
    try:
        st = json.loads(_C.read(p.status, "{}") or "{}")
    except ValueError:
        st = {}
    if not st.get("t"):
        return None, "%s 的 worker 没写过状态" % host
    if now - st["t"] > _C.STATUS_STALE_S:
        return None, "%s 的 worker 没在跑（状态 %d 分钟没更新）" % (host, (now - st["t"]) / 60)
    return st, None


def fit_eta(p):
    try:
        return json.loads(_C.read(p.eta, "{}") or "{}")
    except ValueError:
        return {}


def fit(p, names, project=None):
    """要不要为这些任务在这台上开并行车道（labmon worker fit 主机 任务…）：每行与真放行同一个请求、同一份准入 place（FitHost.open），
    判可以的按 acct_job 记进快照再判下一个。卡的利用率只列出来、不据此劝阻（#67：到 100% 的卡上小任务并行 4 条与 6 条每条一样快）；
    利用率低的卡上在跑的被 CPU 卡住、容器在限流时说先加核、先减线程（gpu_slack，#37）。可以开的写同类在那张卡上并行 k 条时约要多久。
    请求按登记过的清单里那一行拼（known_lists → list_row → job_of → queue_req；tag、同队列、cpus=、gpus= 照清单行，项目取登记的）；
    清单里找不到的只按名字找同类（项目缺省取同名任务最近一次的）。"""
    now = _C.time.time()
    st, why = fit_status(p, p.host, now)
    if st is None:
        print("%s：先 labmon worker start，再判断" % why)
        return 2
    hist = _C.load_hist({}) or []
    fh = FitHost(p.host, st, hist, fit_eta(p))
    if not fh.cards:
        print("看不到 GPU")
        return 2
    for x in fh.card_lines():
        print(x)
    lists = known_lists(p)
    for n in names:
        row = list_row(lists, n, project)
        if row:
            host, q, cmd = row
            where = "（清单：%s 上 %s/%s）" % (host, os.path.basename(q["root"].rstrip("/")), q["jobs"])
            r, e, x = fh.req(dict(q, _host=host), n, cmd, None)
        else:
            proj = project or next((_C.hist_key(h, "name")[1] for h in reversed(hist)
                                    if _C.base_name(h.get("name") or "") == _C.base_name(n) and (h.get("project") or h.get("root"))), None)
            where = "（清单里没找到这一行，按名字估；先把它追加到已登记的清单（比如占着卡的那条末尾）再问）"
            r = _Q.new_req("queue", None, proj, n)
            e = _S.estimate(hist, r)
            d = _S.duration(hist, r, fh.model())
            x = dict(fh.w.job_spec(n, r, e, d and d["min"], cut=d and d["cut"]), key="fit:%s" % n, project=proj, cut_src=d and "%s；%s" % (d["cut_src"], d["src"]), allowed=None)
        say, why = fh.open(r, e, x, now, bool(row))
        print("%s：%s%s" % (n, "可以开车道并行：" + say if say else why if "→" in why else why + " → 排在已有车道后面", where))
    return 0


def fit_list(p, root, jobs, project, hosts, as_json=False):
    """一条清单里还会跑、互不依赖的行此刻能在几台主机上再开几条车道（labmon worker fit 主机 ROOT JOBS --list --project P，#35 B）：
    行取清单文件里没标完（.done、.failed）、不在哪台跑着的（rows_left）；单列、不分的：前置还没满足的（after= 没跑成功、afterany= 没跑完、等不来、成环，
    同真放行的 dep_block，fit_deps）、有别的行写了依赖等着它的（同 offers 的 cant_move：挪走后等它的何时开始会变，dep_waiters）、gang=、占位任务（#144）。
    主机是 hosts（看板按共用 worker 目录的在用主机给，config.home_peers；不给只看这台），在这台上读它们的 status.json（共享 HOME，不多连 ssh），
    状态过期的写明没算。按清单顺序逐行，每行先在这次分到车道最少的那台上试（一样少的按 hosts 的顺序），判断同单行的 fit（FitHost.open）；
    哪台都放不下的到此为止，其余排在后面。只回答此刻放得下多少：怎么拆清单、开不开由实验会话定。
    as_json（--json，算力池往哪台放，pool.fit_head，#25）：只看队首（还会跑、没在跑的第一行；依赖由 worker 状态的 pool_heads 管，这里不判），
    打一行 JSON {head, need {ngpu, vram 每卡 GiB, src 显存估计的来源（提示值的才要装得下，按历史估的超过整卡按整卡算：card_need）, ram GiB, cpus}, dur 分钟（不知道为 null）, hosts {主机: {ok, why}}}；
    各台的判断同上（FitHost.open），没有能算的主机也给 need；没有还要跑的行 head 为 null。"""
    now = _C.time.time()
    root = os.path.abspath(root)
    path = os.path.normpath(os.path.join(root, jobs))
    lists = known_lists(p)
    reg = next(((h, q) for h, q, _ in lists if os.path.normpath(os.path.join(q["root"], q["jobs"])) == path), None)
    if reg:
        q = dict(reg[1], _host=reg[0])
        if project and q["project"] != project:
            print("%s 登记在 %s 上，属于项目 %s，不是 %s" % (path, reg[0], q["project"], project))
            return 2
    else:
        if not project:
            print("这条清单还没登记：写 --project <看板项目 id>")
            return 2
        q = _Q.load_queue({"_slug": _Q.qslug(root, jobs), "root": root, "jobs": jobs, "project": project})
    rows = _Q.list_jobs(q)
    if not rows and not os.path.exists(q["jobs_path"]):
        print("读不到清单 %s" % path)
        return 2
    hist, fhs, skip = _C.load_hist({}) or [], [], {}
    running = set()
    for h in hosts if as_json else hosts or [p.host]:        # --json 只看给的几台（池目录没有 worker，不看这台）
        hp = _C.Paths(h)
        st, why = fit_status(hp, h, now)
        if st is None:
            skip[h] = why
            continue
        running |= {j["name"] for j in st.get("running") or [] if os.path.normpath(os.path.join(j.get("root") or "", j.get("queue") or "")) == path}
        fh = FitHost(h, st, hist, fit_eta(hp))
        hit = fh.w.override_hit({"project": q["project"], "name": ""}, ("drain", "guest", "pause"), lasting=True)
        if hit or not fh.cards:
            skip[h] = "%s：%s" % (h, hit[1] if hit else "看不到 GPU")
        else:
            fhs.append(fh)                        # 在限流的照样列出来（各行都写先减线程）
    gone = set()                                  # 看板说已归还的主机（returned.json，各台一样）：容器没了，它放的任务也没了
    for h in {p.host, *hosts}:
        try:
            gone |= set(json.loads(_C.read(_C.Paths(h).returned, "{}") or "{}").get("hosts") or [])
        except (ValueError, AttributeError, TypeError):
            pass
    for cur in _Q.read_current(q["state"]):          # 状态目录里在跑的（并行清单有几行，#147）：放它的主机状态过期了（worker 停了、崩了）任务照样可能在跑，不能再分
        if not (cur[3] and cur[3] in gone):       # 放它的主机已归还的不算（算力池借的主机换了容器、清单回池里，#25；同 run_elsewhere）
            running.add(cur[0])
    _, pend = _Q.rows_left(q["state"], rows, None)
    pend = _Q.first_rows(pend)
    pend = [(n, c) for n, c in pend.items() if n not in running]
    if as_json:
        head = pend[0] if pend else None
        if (q.get("par") or 1) > 1 and pend:   # 并行清单（#147）：同 pool_view 取窗口里第一行依赖满足的
            dw = fit_deps(p, q, rows, hist, now)
            head = next((x for x in pend[:q["par"]] if not dw.dep_block(dict(_Q.job_of(*x), project=q["project"]), now)), head)
        print(json.dumps(fit_head(q, head, fhs, skip, hist, now), ensure_ascii=False))
        return 0
    skip = list(skip.values())
    apart, todo = [], []
    dw = fit_deps(p, q, rows, hist, now)
    waiters = dw.dep_waiters()
    for n, c in pend:
        h = _Q.job_of(n, c)
        dep = dw.dep_block(dict(h, project=q["project"], name=n), now)    # 前置都满足了（after= 跑成功、afterany= 跑完）的照常分，同真放行
        ws = sorted(x if p_ == q["project"] else "%s/%s" % (p_, x) for p_, x in waiters.get((q["project"], n), ()))
        what = ("占位" if _C.is_hold(n) else "成组 gang=%s" % h["gang"] if h["gang"] else dep[1] if dep
                else "%s 写了 after= 等着它" % "、".join(ws)[:200] if ws else None)
        (apart if what else todo).append((n, c, what))
    print("清单 %s（%s，项目 %s）：还会跑 %d 行%s" % (path, "登记在 %s 上" % reg[0] if reg else "还没登记", q["project"], len(pend),
                                             "，在跑 %s" % "、".join(sorted(running)) if running else ""))
    if (q.get("par") or 1) > 1:
        print("这条清单写了 par=%d：登记的那台上同时最多跑 %d 行，卡有余量时 worker 自己补，不用为这台拆清单（下面算的是还能再开几条车道）" % (q["par"], q["par"]))
    if apart:
        if len(apart) > 3:
            shown = "、".join("%s（%s）" % (n, w) for n, _, w in apart[:3])
            print("不分的 %d 行（留在原处）：%s 等共 %d 行" % (len(apart), shown, len(apart)))
        else:
            print("不分的 %d 行（留在原处）：%s" % (len(apart), "、".join("%s（%s）" % (n, w) for n, _, w in apart)))
    if skip:
        print("没算：" + "；".join(skip))
    if not fhs:
        print("没有能算的主机")
        return 2
    for fh in fhs:
        fh.other = _P.other_load(fh.st, q["project"])
        fh.pre = fh.card_lines(q["project"])      # 卡的样子是开车道之前的（非本项目任务脱敏）
    left = list(todo)
    while left:
        n, c, _ = left[0]
        done = False
        for fh in sorted(fhs, key=lambda f: (len(f.lanes), f.other["running"] + f.other["queued"], f.other["until"] or 0.0, fhs.index(f))):
            r, e, x = fh.req(q, n, c, None)
            say, why = fh.open(r, e, x, now)
            if say:
                fh.say.append("  %s ← 卡 %s：%s" % (n, ",".join(map(str, fh.lanes[-1][1])), say) if x["ngpu"] else "  %s（不用 GPU）：%s" % (n, say))
                done = True
                break
            fh.why = "%s：%s" % (n, why)
        if not done:
            break
        left.pop(0)
    for fh in fhs:
        hint = _P.other_load_text(fh.other, now=now, show_until=True)
        print("== %s%s ==" % (fh.host, "（%s）" % hint if hint else ""))
        for x in fh.pre:
            print("  " + x)
        if fh.lanes:
            print("  可再开 %d 条车道：" % len(fh.lanes))
            for x in fh.say:
                print("  " + x)
        if fh.why:
            print("  %s不再开：%s" % ("" if fh.lanes else "一条也", fh.why))
    got = sum(len(fh.lanes) for fh in fhs)
    if got:
        tail = "余 %d 行（%s…）留原清单或均摊" % (len(left), left[0][0]) if left else "无剩余行"
        print("合计可再开 %d 条车道（本机调 par=N，跨机剪行并 worker add，见 lists.md）；%s" % (got, tail))
    else:
        print("此刻哪台都不再开车道：%s" % ("这些行排在已有车道后面" if todo else "没有能分的行"))
    return 0


def fit_head(q, head, fhs, skip, hist, now):
    """fit --list --json 的那一行（fit_list）：清单 q 的队首 head（(任务名, 命令)，没有为 None）要多少、在各台（fhs：FitHost，skip：没算的 {主机: 原因}）放不放得下。
    need 不看哪台（没有能算的主机也给）：按一台空的 FitHost 拼请求（与真放行同一个 job_spec，只是没有型号与在跑同类）。"""
    if head is None:
        return {"head": None, "need": None, "dur": None, "hosts": {h: {"ok": False, "why": "清单里没有还要跑的行"} for h in [f.host for f in fhs] + list(skip)}}
    n, c = head
    r, e, x = FitHost(q.get("_host") or "", {}, hist, {}).req(q, n, c, None)
    out = {"head": n, "need": {"ngpu": x["ngpu"], "vram": round(x["vram"] or 0.0, 2), "src": x["src"], "ram": round(x["ram"] or 0.0, 2), "cpus": x["cpus"]},
           "dur": x["dur"] and round(x["dur"]), "hosts": {h: {"ok": False, "why": why} for h, why in skip.items()}}
    for fh in fhs:
        r, e, x = fh.req(q, n, c, None)
        say, why = fh.open(r, e, x, now)
        out["hosts"][fh.host] = {"ok": bool(say), "why": say or why}
    return out
