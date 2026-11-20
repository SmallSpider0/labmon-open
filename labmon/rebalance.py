"""多主机负载均衡的提示（Issue #20 第一档“只提示”）：把各主机 worker 写的 offers 汇成每个项目一条挪法建议。

各台 worker 用自己的放行与推演规则算“别的主机上这个项目的哪几行我能接、接来后几点跑完”（labmon_worker 的 offers）；
这里按项目挑整批完工最早的一种挪法：哪台、挪前几行（同一台上多开一条车道、挪到同一台另一条早跑完的清单也算，offer 带 local，#43、#110），整批从何时提前到何时。
整批至少提前 max(GAIN_MIN_S, min(GAIN_S, 剩余时间 × GAIN_SHORT_FRAC), 剩余时间 × GAIN_FRAC) 才建议（_need；维护者 2026-11-08 定 1 小时与 10%，
#43 按推荐放宽短批：剩 2 小时的批早 36 分钟就报）；报过建议的项目降到阈值一半以下才收、目标主机沿用上一轮的（滞回，免得在阈值附近或几台之间来回叫醒）。挪不了的写原因（只在页面上，不叫醒）。
整批没有够门槛的挪法（整批被挪不动的行撑着，或何时跑完推不出），而接收方此刻没有 labmon 任务在跑（offer 的 idle）时，改按行比：
挪来的每一行都比在原处早 GAIN_MIN_S 以上跑完就建议（_idle_gain，#62）。
labmon 不改任何清单、不改放行规则：建议由实验会话照做（剪行、追加到目标主机的清单，见 SKILL「跑和盯」的 rebalance:）。

看板这头每轮再核对一遍（#25）：offers 里的行这一轮已在原处开跑、已不在原处的 todo（剪过、跑完了）就截掉（各 worker 读别的主机的状态最多晚几分钟）；
一台这一轮读失败时沿用它上一轮的 offers（keep_offers，照 borrow_cut 的先例），到它的状态按 STALE_S 过期为止，免得建议消失一轮再出现、重复叫醒。
原因句由这里拼（worker 的 why 只放原因、stop 是挪不动的那一行）。
结果放在工作台全局快照的 rebalance {项目: {...}}（overview.collect 算），项目看板据此出 rebalance:<项目>:<挪法> 告警（move_key）、页面写一句；
被借用约定挡住的车道（borrow.late_jobs）上的任务由 borrow_cut 告警带上“能挪到哪台、几点跑完”，这一轮出了那条就不在 rebalance 里再报。
"""
from __future__ import annotations

import re

from .config import STALE_S, fmt_dur, fmt_time

GAIN_S = 3600          # 整批完工至少提前这么多秒才建议（剩余时间短的批按 GAIN_SHORT_FRAC 放宽，不低于 GAIN_MIN_S）
GAIN_MIN_S = 900
GAIN_SHORT_FRAC = 0.3
GAIN_FRAC = 0.1        # 且至少占剩余时间的这么多
TAIL = "；按 %s 的推演"   # 建议句尾：按几点的推演（log_text 判变没变时去掉它）
TAIL_RE = re.compile(r"；按 \d\d:\d\d 的推演$")
STICK_S = 900          # 上一轮建议的那台跑完的时刻比最优的晚不到这么多时沿用它（几台一样空时谁领先每轮都可能换）


def _top(xs: list) -> float | None:
    """几条清单何时都跑完：有一条不知道就不知道（None）。"""
    return None if None in xs else max(xs)


def _lane(j: dict) -> str:
    return f"{j['host']}|{j['root']}|{j['queue']}"


def _finish(o: dict, k: int) -> float | None:
    """worker 的一项 offer 里挪前 k 行后整批何时跑完：原处各清单（挪过的取挪完后的 rest）、挪来的几行、本机这个项目自己的清单取最晚。"""
    js, ends = o["jobs"][:k], dict(o["ends"])
    for j in js:                       # 同一条清单挪了几行：后挪的在前，它的 rest 更早
        ends[_lane(j)] = j["rest"]
    return _top(list(ends.values()) + [js[-1]["end"], js[-1]["own"]])


def _idle_gain(js: list, gap: float):
    """接收方空着时按行比（#62）：挪来的这几行在这台都跑完的时刻（最后一项的 end）比它们在原处最早的预计结束（there 里最小的；
    原处说不出的当很晚）早至少 gap 秒就成立。成立时返回原处最早的那个结束（都说不出为 None），不成立为 False。"""
    known = [j["there"] for j in js if j.get("there") is not None]
    there = min(known) if known else None
    return there if there is None or there - js[-1]["end"] >= gap else False


def _need(f: float, t: float) -> float:
    """整批从 f 跑完（现在 t）至少要提前多久才建议：剩余 2 小时要早 36 分钟，剩余 10 小时以上仍按 1 小时与 10%。"""
    left = f - t
    return max(GAIN_MIN_S, min(GAIN_S, GAIN_SHORT_FRAC * left), GAIN_FRAC * left)


def _text(p: dict, proj: str, label: dict, t: float) -> str:
    """建议的一句话（告警与两个页面都用它）：把哪台哪条清单（写全目录：同项目几个目录下常有同名清单）的哪几行挪到哪台、接在哪
    （要新建的写出原清单的目录与项目 id：任务在清单的目录下跑、读这条清单的 .env，挪来的一批同一种环境）、整批从何时提前到何时、按几点的推演。"""
    src: dict = {}
    for j in reversed(p["jobs"]):      # 挪的顺序是从队尾往前，写的时候照原清单的先后
        src.setdefault((j["host"], j["root"], j["queue"]), []).append(j["name"])
    what = "，".join(f"{label.get(h, h)} 上 {r.rstrip('/')}/{q} 的 {'、'.join(ns)}" for (h, r, q), ns in src.items())
    tid = p["target"]
    if p.get("idle"):                  # 接收方空着、按行比的（#62）：整批推得出、也提前了的另写一句
        there = f"最早 {fmt_time(p['there'])} 才跑完" if p["there"] else "说不出何时跑完"
        batch = (f"；整批预计从 {fmt_time(p['from'])} 提前到 {fmt_time(p['to'])}" if p["from"] is not None and p["to"] is not None and p["to"] < p["from"]
                 else "")
        when = f"{label.get(tid, tid)} 上现在没有 labmon 任务在跑，这几行在原处{there}，挪过来 {fmt_time(p['done'])} 前都跑完{batch}"
    elif p["from"] is not None:
        when = f"整批预计从 {fmt_time(p['from'])} 提前到 {fmt_time(p['to'])}（早 {fmt_dur(p['from'] - p['to'])}）"
    else:
        when = f"整批预计 {fmt_time(p['to'])} 跑完（现在有任务在原处跑不了，整批何时跑完推不出）"
    dep = [j["name"] for j in p["jobs"] if j.get("dep")]
    tail = f"；写了 after= 的 {'、'.join(dep)} 排在挪来的几行最后（等别的任务的行在前会堵住后面的）" if dep and len(dep) < len(p["jobs"]) else ""
    lane = p.get("lane") and f"{p['lane'][0].rstrip('/')}/{p['lane'][1]}"
    where = (f"插在 {lane} 里 {p['before']} 前面（它写了 after=，要等别的任务；接在它后面会被它堵住）" if lane and p.get("before")
             else f"接在 {lane} 后面" if lane else None)
    if p.get("local") and lane:        # 同机挪到另一条早跑完的清单（#110）：车道数不变，原清单在同一台排到别的清单跑完后才跑完
        return f"把 {what} 挪到同一台上另一条早跑完的清单（{where}{tail}）：{when}{TAIL % fmt_time(t, date=False)}"
    if p.get("local"):                 # 同机多开一条车道（#43）：剪到新的长期清单，与原清单同目录、同环境
        # 推演只在没吃满的卡上排（offer 的 gpus）：登记时照样限卡，否则真放行可能放到吃满的卡上（Codex 审 #63）
        gp = f" --gpus {','.join(map(str, p['gpus']))}（只用这几张卡：别的卡已吃满）" if p.get("gpus") else ""
        return (f"在 {label.get(tid, tid)} 上多开一条车道：把 {what} 剪到一条新的长期清单（与原清单同目录，.env 照抄原清单的，但 QUEUE_STATE 与 QUEUE_LOGS 写这条自己的；"
                f"原清单没有 .env 时也新建一个，只写这两行）并 labmon worker add {tid} {p['jobs'][0]['root']} 新清单 --project {proj}{gp}，以后同类任务也往这条追加{tail}："
                f"{when}{TAIL % fmt_time(t, date=False)}")
    where = where or (f"这台还没有本项目同一种环境的清单：新建一条（与原清单同目录，.env 照抄原清单的，但 QUEUE_STATE 与 QUEUE_LOGS 写这条自己的；"
                      f"原清单没有 .env 时也新建一个，只写这两行）并 labmon worker add {tid} {p['jobs'][0]['root']} 新清单 --project {proj}")
    return f"把 {what} 挪到 {label.get(tid, tid)}（{where}{tail}）：{when}{TAIL % fmt_time(t, date=False)}"


def log_text(msg: str) -> str:
    """服务日志判告警变没变时用的文字：去掉建议句尾的推演时刻（每轮都变，页面与 labmon wait 照样显示它）。"""
    return msg[:-len(TAIL % "00:00")] if TAIL_RE.search(msg) else msg


def _why(hid: str, o: dict, f: float | None, label: dict, t: float) -> str:
    """一台一行也接不了时的原因句：“29（A6000）上本项目 11-09 06:34 后没活，接不了 X：放不下（…）”；整批推不出时不说没活（那台可能正排着
    本项目的活）。o：worker 的一项 offer（why 原因本身，stop 挪到这台排不上的那一行）。"""
    what = f"接不了 {o['stop']}：" if o.get("stop") else "接不了："
    if f is None:
        return f"{label.get(hid, hid)} {what}{o['why']}"
    idle = f" {fmt_time(o['free'])} 后" if o["free"] and o["free"] > t + 60 else ""
    return f"{label.get(hid, hid)} 上本项目{idle}没活，{what}{o['why']}"


def keep_offers(hosts: list[dict], old: dict | None, t: float) -> None:
    """一台这一轮读失败（error、status_error，没 worker stop、不是本机不在内网）时沿用它上一轮的 offers，写进这一轮的主机项 offers_kept
    {t 那份状态写下的时刻（本机时钟）, offers}，连续几轮失败也接得上（照 borrow.check 沿用 borrow_cut 的先例，#25）；那份状态按 STALE_S
    过期（与 worker_up 同一口径）就不再沿用，那台当作不在。old：上一轮的全局快照。"""
    prev = {x["id"]: x for x in (old or {}).get("hosts") or []}
    for e in hosts:
        oe = prev.get(e["id"]) or {}
        if not (e.get("error") or e.get("status_error")) or e.get("stopped") or e.get("lan_off") or oe.get("stopped"):
            continue
        st = oe.get("status") or {}
        kept = ({"t": (old or {}).get("generated", t) - (oe.get("age") or 0), "offers": st.get("offers") or []}
                if oe.get("worker_up") and not oe.get("lan_off") and st else oe.get("offers_kept"))
        if kept and t - kept["t"] < STALE_S:
            e["offers_kept"] = kept


def _live(hosts: list[dict]) -> dict:
    """这一轮读到了 worker 状态的主机：{主机: (在跑的 {(目录, 清单, 任务)}, 还会跑的 {(目录, 清单): {任务}})}，核对 offers 里的行还在不在原处。"""
    out = {}
    for e in hosts:
        st = e.get("status")
        if st and e.get("worker_up") and not e.get("lan_off") and not e.get("stopped"):
            out[e["id"]] = ({(j.get("root"), j.get("queue"), j.get("name")) for j in st.get("running") or []},
                            {(ln.get("root"), ln.get("queue")): set(ln.get("jobs") or []) for ln in st.get("todo") or []})
    return out


def _fresh(o: dict, live: dict) -> dict:
    """offer 截到第一行已不在原处的前面（这一轮读到的那台状态里它已在跑、或已不在那条清单的 todo：剪过、跑完了；前 k 行的数仍然成立）。
    那台这一轮没读到的不核对。"""
    for i, j in enumerate(o["jobs"]):
        src = live.get(j["host"])
        if src and ((j["root"], j["queue"], j["name"]) in src[0] or j["name"] not in src[1].get((j["root"], j["queue"]), ())):
            return dict(o, jobs=o["jobs"][:i])
    return o


def suggest(hosts: list[dict], prev: dict | None, t: float) -> dict:
    """各项目的挪法建议 {项目: 建议}。hosts：工作台这一轮的各主机（worker 状态在 status，借用的 borrow.late 已由 borrow.check 补上）；
    prev：上一轮的结果（滞回用）。只读 worker 在跑、状态新鲜、这轮连上了的主机（过期、worker stop 过的不读；本机不在内网时 overview 沿用上一轮的结果）；
    这轮读失败的读 keep_offers 沿用的 offers_kept。offers 里已不在原处的行按这一轮读到的来源状态截掉（_fresh）。
    key 带挪法（move_key：目标与第一行，#109）：同一挪法一直在、推演在抖时只叫醒一次，换目标、换清单、第一行换了算新的。
    报过建议的项目门槛减半（滞回）；上一轮的目标主机仍够门槛、跑完的时刻比最优的晚不到 STICK_S 时沿用它，免得几台一样空时每轮换一句。
    建议 {key 告警 key, target 挪到哪台, lane 接在它哪条清单后面, before 插在那条清单里哪一行前面（它写了依赖；None 接在末尾）,
    jobs [{host, root, queue, name, there, rest, end, own, dep 这一行写了 after=}], from 原来整批何时跑完
    （推不出为 None）, to 挪后（推不出为 None）, cut 其中被借用约定挡住的清单 ["主机|目录|清单"], local 是不是同机的挪法（lane 为 None 是多开一条车道，否则挪到同一台的那条清单）, text}；
    整批没有够门槛的挪法、而有一台空着（offer 的 idle）时按行比（_idle_gain，#62）：建议另带 idle: True、there 这几行在原处最早何时跑完（说不出为 None）、
    done 挪过去后何时都跑完；挪不了的 {why [每台一句], from}；
    没有可挪的、却有一台有空（这个项目早就没活，或同机能多开车道）只因挪来的那一行没有估时推演算不出（offer 的 nodur）时，
    {key "rebalance:<项目>:est", est: True, text 让会话写 est_min= 或 fit --list, why, from}（#43；同样只给会话、只叫醒一次）；都没有的项目不列。"""
    prev = prev or {}
    label = {e["id"]: e.get("label") or e["id"] for e in hosts}
    late = {f"{e['id']}|{x.get('root')}|{x.get('queue')}" for e in hosts for x in (e.get("borrow") or {}).get("late") or []}
    offs: dict = {}
    live = _live(hosts)
    for e in hosts:
        st = e.get("status") or {}
        if e.get("worker_up") and not e.get("lan_off") and not e.get("stopped"):
            got = st.get("offers") or []
        else:
            got = (e.get("offers_kept") or {}).get("offers") or []
        for o in got:
            offs.setdefault(o["project"], []).append((e["id"], _fresh(o, live)))
    out = {}
    for proj, xs in sorted(offs.items()):
        was = prev.get(proj) or {}
        opts, why, est, idles = [], [], [], []
        half = 0.5 if was.get("key") else 1
        for hid, o in xs:
            f = _top(list(o["ends"].values()) + [o["free"]])
            most = None                    # 这台空着时按行比成立的最多几行（#62）
            for k in range(1, len(o["jobs"]) + 1):
                new = _finish(o, k)
                opt = {"target": hid, "lane": o["lane"], "before": o.get("before"), "jobs": o["jobs"][:k], "from": f, "to": new,
                       "local": bool(o.get("local")), "gpus": o.get("gpus")}
                there = _idle_gain(o["jobs"][:k], GAIN_MIN_S * half) if o.get("idle") and not o.get("local") else False
                if there is not False:
                    most = dict(opt, idle=True, there=there, done=o["jobs"][k - 1]["end"])
                if new is None or (f is not None and f - new < _need(f, t) * half):
                    continue
                opts.append(opt)
            if most:
                idles.append(most)
            # 挪不了：这台这个项目早就没活（离整批跑完还够阈值），却一行也接不了，写明为什么
            idle = f is None or (o["free"] is not None and f - max(o["free"], t) >= _need(f, t))
            if not o["jobs"] and o.get("why") and idle and not o.get("local"):     # 同机多开不成的不写：fit 会说
                why.append(_why(hid, o, f, label, t))
            if not o["jobs"] and o.get("nodur") and (idle or o.get("local")):
                est.append((hid, o))
        if opts or idles:
            if opts:
                rank = lambda x: (x["to"], len(x["jobs"]))
                best = min(opts, key=rank)
                same = [x for x in opts if x["target"] == was.get("target") and x["local"] == bool(was.get("local")) and x["to"] <= best["to"] + STICK_S]
            else:                      # 没有整批的建议、有一台空着：按行比的（#62），挪得多的在前、再看跑完早的；上一轮的目标还成立就沿用
                rank = lambda x: (-len(x["jobs"]), x["done"])
                best, same = min(idles, key=rank), [x for x in idles if x["target"] == was.get("target") and not was.get("local")]
            best = dict(min(same or [best], key=rank))
            best["key"] = move_key(proj, best)
            best["cut"] = sorted({_lane(j) for j in best["jobs"]} & late)
            best["text"] = _text(best, proj, label, t)
            out[proj] = best
        elif why or est:
            out[proj] = {"why": why, "from": _top([_top(list(o["ends"].values()) + [o["free"]]) for _, o in xs])}
            if est:
                out[proj].update(key=f"rebalance:{proj}:est", est=True, text=_est_text(est, proj, label))
    return out


def move_key(proj: str, p: dict) -> str:
    """挪法建议的告警 key（#109）：rebalance:<项目>:<挪到哪台>:<原主机>/<清单>/<第一行>。第一行是最先挪的那一行（排得最晚那条清单的队尾）：
    推演在抖、多挪少挪一行它不变，不重报；换了目标、换了清单、照做剪掉队尾后又建议接着挪，都是新的挪法，labmon wait 再叫醒一次
    （同一挪法消失后 6 小时内再出现仍不叫醒，waits.REWAKE_S）。提前多少不进 key：每轮都在漂，句子里写最新的。同机的两种挪法（挪到另一条清单、多开一条车道）目标都是原主机。"""
    j = p["jobs"][0]
    return f"rebalance:{proj}:{p['target']}:{j['host']}/{j['queue']}/{j['name']}"


def _est_text(est: list, proj: str, label: dict) -> str:
    """挪来的那一行没有估时、推演算不出能提前多少时给会话的一句（#43）：哪台有空、卡在哪几行，怎么补。"""
    where = "；".join(f"{label.get(h, h)}" + ("（同机多开一条车道）" if o.get("local") else " ") + f"接 {o['stop']} 时" for h, o in est)
    return (f"有主机有空，但推演算不出挪过去能提前多少：{where}没有估时（同类没跑过，清单也没写 est_min）。"
            f"给这类行写 est_min=（分钟，粗估就行），或 labmon worker fit <主机> <目录> <清单> --list --project {proj} 看此刻能再开几条车道")
