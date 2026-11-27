"""共享盘：组里各服务器挂的同一块 NFS（各台的 /root/data1）。看板这边只读：
- 总量与剩余：自用主机 worker 每轮读的 statvfs（取最新的一台）；
- 按目录的用量：拿着扫描租约的那台 worker 轮流扫出的索引（labmon_worker.Disk，<worker 目录>/disk/index.json，变了才取回）。
这里做归属（一级目录 → 人）、增量与增速、历史与告警，给「组内服务器 · 共享盘」、人员抽屉、主机页与项目页用；
组内服务器上的 GPU 负载也按这张归属表归人（whose）。

    状态目录 overview/fleet/disk/：
        index.json    最近一次取回的索引
        owners.json   页面上改的归属与提醒线 {"owners": {一级目录: 人或"公共"}, "limits": {人: GiB}}
        scans.jsonl   各单位每扫一次一行 {t, u, b}；某时刻的占用按前后两次线性插值（留 35 天）
        hours.jsonl   每小时一行：df 总量、已用、剩余与各人占用（留 1 年）
        state.json    取回的索引在服务器上的修改时刻、已报过的告警（恢复后再报）、名单里各人的颜色（定下后不随排名变）
"""
from __future__ import annotations

import bisect
import json
import math
import re
import time

from . import fleet as F
from .config import GIB, active_hosts, all_hosts, is_standalone, pick_color
from .records import log_events
from .state import append_row, file_sig, load_json, load_rows, save_json, snapshot, usage_file

WARN_GB = 30            # 剩余低于这么多 GiB 告警（[fleet.disk] warn_gb 可改）
BIGWRITE_GB = 5         # 一个任务跑的期间它的项目目录在共享盘上净增超过这么多 GiB，给实验会话提示（bigwrite_gb，#53）
JOBS_S = 7 * 86400      # 任务写入量算多久以内结束的
JOB_WAIT_S = 86400      # 任务结束后项目目录下还有单位没重扫的，最多等这么久就按已扫到的算
SOON_D = 3              # 按近 24 小时的速度不到这么多天就满，且剩余已低于 SOON_FREE，告警（soon_days）
SOON_FREE = 0.2
BURST_GB = 30           # 一人近 24 小时增长超过这么多 GiB，且超过剩余的 BURST_FREE，告警（burst_gb）
BURST_FREE = 0.1
COLD = 3                # 分龄（7、30、90、365 天）从第几档起算“90 天没改过”
RECLAIM_DAYS = 14       # 独立主机本地工作盘闲置可回收提示门槛（天）：超过 14 天未修改 + 无活跃队列 + >= 1 GiB（#114）
RECLAIM_MIN_B = GIB     # 独立主机闲置可回收提示最小体积（1 GiB）
SCANS_KEEP = 35 * 86400
HOURS_KEEP = 366 * 86400
LIST_S = 600            # worker 多久列一次目录（删掉的目录在两次之间消失）
PUBLIC = "公共"
_CACHE: dict = {}


def _f(name: str):
    return F.state_file("disk/" + name)


def _rows(name: str) -> list[dict]:
    """scans.jsonl、hours.jsonl：文件没变就用上次读的。"""
    f = _f(name)
    m = file_sig(f)
    c = _CACHE.get(str(f))                      # 按路径：LABMON_STATE 换了（测试）不拿另一个目录的
    if not c or c[0] != m:
        c = _CACHE[str(f)] = (m, load_rows(f) if m else [])
    return c[1]


def seen() -> int:
    """已取回的索引在服务器上的修改时刻：采集时没变就不再传。"""
    return int(load_json(_f("state.json"), {}).get("mtime") or 0)


def absorb(part: str, t: float) -> None:
    """采集取回的一段（第一行是服务器上索引的修改时刻，后面是索引，没变时为空）：存下索引，各单位新扫的一次记进 scans.jsonl。
    新出现的单位（不是拆开来的）从上次见到索引时算 0，消失的（删掉了）记一次 0，增量才不漏掉新建与删除。"""
    head, _, body = part.strip().partition("\n")
    if not body.strip():
        return
    try:
        ix = json.loads(body)
    except ValueError:
        return
    old = load_json(_f("index.json"), {})
    if (ix.get("t") or 0) < (old.get("t") or 0):
        return
    ou, nu, split = old.get("units") or {}, ix.get("units") or {}, set(ix.get("split") or [])
    # 刚拆开、还没列过的目录（扫得太慢、新登记的项目根；上一份索引里它下面还没有单位）：下面的单位是原来那个拆出来的，不算新建。
    # 不只比相邻两份索引的 split：worker 拆开时先并进 split、去掉父单位，子单位要等下一次列目录，看板可能在中间取回一份（PR #56 审查）。
    # 代价是原来空着的目录拆开后第一次新建的不从 0 算，只漏一次。
    fresh = [s for s in split if not any(o.startswith(s + "/") for o in ou)]
    born = lambda u: ou and u not in ou and not any(u.startswith(s + "/") for s in fresh)
    rows = []
    for u, r in nu.items():
        if r.get("t") is None or r.get("b") is None or (ou.get(u) or {}).get("t") == r["t"]:
            continue
        if born(u) and old.get("t"):
            rows.append({"t": old["t"], "u": u, "b": 0})
        elif r.get("pt") and r.get("pb") is not None and (ou.get(u) or {}).get("t") != r["pt"]:
            rows.append({"t": r["pt"], "u": u, "b": r["pb"]})       # 中间漏取过一次
        rows.append({"t": r["t"], "u": u, "b": r["b"]})
    gone = ix.get("listed") or ix.get("t") or t
    for u, r in ou.items():
        if u not in nu and u not in split and r.get("b") is not None:
            rows += [{"t": max(r["t"], gone - LIST_S), "u": u, "b": r["b"]}, {"t": gone, "u": u, "b": 0}]
    for row in sorted(rows, key=lambda r: r["t"]):
        append_row(_f("scans.jsonl"), row, SCANS_KEEP, t)
    save_json(_f("index.json"), ix)
    st = load_json(_f("state.json"), {})
    st["mtime"] = int(head.strip() or 0) if head.strip().isdigit() else 0
    save_json(_f("state.json"), st)


def _at(ser: list, t: float) -> float:
    """[(t, b)] 按时刻排好：t 时的占用（两次之间线性插值，第一次以前按第一次，最后一次以后按最后一次）。"""
    k = bisect.bisect_left(ser, (t, -1))
    if k == 0:
        return ser[0][1]
    if k == len(ser):
        return ser[-1][1]
    (t0, b0), (t1, b1) = ser[k - 1], ser[k]
    return b0 + (b1 - b0) * (t - t0) / (t1 - t0) if t1 > t0 else b1


def _series() -> dict:
    by: dict = {}
    for r in _rows("scans.jsonl"):
        by.setdefault(r["u"], []).append((r["t"], r["b"]))
    return {u: sorted(v) for u, v in by.items()}


def settings() -> dict:
    s = load_json(_f("owners.json"), {})
    return {"owners": s.get("owners") or {}, "limits": s.get("limits") or {}}


def set_owner(top: str, owner: str | None) -> dict:
    """一级目录归给谁（人名或「公共」）；owner 为空就回到缺省（与名字对得上的人，否则目录名本身）。"""
    top = (top or "").strip().strip("/")
    if not top or "/" in top:
        raise ValueError(f"要写共享盘根下的一级目录名：{top or '（空）'}")
    s = settings()
    was = s["owners"].get(top)
    s["owners"].pop(top, None) if not (owner or "").strip() else s["owners"].__setitem__(top, owner.strip())
    save_json(_f("owners.json"), s)
    now = s["owners"].get(top)
    if now != was:
        log_events([{"kind": "config", "who": now or was, "msg": f"共享盘目录 {top} 归到 {now or '缺省'}（原来 {was or '缺省'}）"}])
    return s


def set_limit(name: str, gb) -> dict:
    """这人的提醒线（GiB）：超过只标红、记事件，不弹通知；gb 为空就去掉。"""
    s = settings()
    was = s["limits"].get(name)
    s["limits"].pop(name, None) if gb in (None, "") else s["limits"].__setitem__(name, float(gb))
    save_json(_f("owners.json"), s)
    now, gib = s["limits"].get(name), (lambda x: "没有" if x is None else f"{x:g} GiB")
    if now != was:
        log_events([{"kind": "config", "who": name, "msg": f"{name} 在共享盘上的提醒线改为 {gib(now)}（原来 {gib(was)}）"}])
    return s


def _names(cfg: dict | None) -> list[str]:
    """人员名单里的名字（一级目录名与它不分大小写对上就归这人）。"""
    names = set(F.registered_people()) | {s["holder"] for s in (cfg or {}).get("servers", [])}
    names.update(F.host_holder(h, cfg) for h in all_hosts() if h.get("platform_job"))
    names.add(F.legacy_person(cfg))
    return sorted(names - {None})


def owner_of(top: str, owners: dict, names: list[str]) -> str:
    if top in owners:
        return owners[top]
    if top == ".":
        return PUBLIC
    return {n.lower(): n for n in names}.get(top.lower(), top)


def whose(cfg: dict | None, hosts: list[dict]):
    """按路径归人的函数（组内服务器的 GPU 负载用）：依次看给的工作目录与命令行，第一个落在共享盘一级目录下、
    且这个目录归到名单里的人的，就是这人；都对不上返回 None。hosts：主机清单里的主机（取共享盘挂载点）。"""
    root = next((h["shared"][0] for h in hosts if h.get("shared")), None)
    if not root:
        return lambda texts: None
    owners, names, pat = settings()["owners"], _names(cfg), re.compile(re.escape(root.rstrip("/")) + r"/([^/\s'\"]+)")

    def f(texts: list[str | None]) -> str | None:
        for x in texts:
            for top in pat.findall(x or ""):
                o = owner_of(top, owners, names)
                if o in names:
                    return o
        return None
    return f


def _shared_hosts(hosts: list[dict], hc: dict | None = None) -> list[dict]:
    """筛出非独立（共享集群盘）主机，防止独立主机本地盘混入集群「共享盘」容量与历史曲线。"""
    hc = {h["id"]: h for h in active_hosts()} if hc is None else hc
    return [e for e in hosts if not e.get("standalone") and not is_standalone(hc.get(e.get("id")))]


def _df(hosts: list[dict], ix: dict) -> dict | None:
    """总量与剩余（字节）：最新的一台非独立自用主机读的 statvfs；都没有就用索引里扫描者记的。"""
    best = None
    for e in _shared_hosts(hosts):
        st = e.get("status") or {}
        d = next(iter((st.get("info") or {}).get("disk") or []), None)
        if d and (not best or st.get("t", 0) > best["t"]):
            best = {"total": d["total"] * GIB, "free": d["free"] * GIB, "used": (d["total"] - d["free"]) * GIB, "t": st.get("t", 0)}
    return best or ix.get("df")


def _eta(rows: list[dict], df: dict, since: float) -> float | None:
    """按 since 以来 df 已用的增长速度（最小二乘斜率），剩余还够几天；不涨就是 None。"""
    pts = [(r["t"] / 86400, r["used"]) for r in rows if r["t"] >= since] + [(df["t"] / 86400, df["used"])]
    if len(pts) < 6 or pts[-1][0] - pts[0][0] < 0.25:
        return None
    mx, my = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
    slope = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
    return round(df["free"] / slope, 1) if slope > 0.05 * GIB else None


def update(hosts: list[dict], t: float | None = None, cfg: dict | None = None) -> tuple[dict | None, list[str]]:
    """页面数据（snapshot 的 disk）与要弹通知的消息。每小时记一行历史；告警只在刚出现时报，恢复后再报。"""
    t = time.time() if t is None else t
    cfg = cfg if cfg is not None else (F.conf() or {})
    ix = load_json(_f("index.json"), {})
    df = _df(hosts, ix)
    if not df:
        return None, []
    root, units, sets, names = ix.get("root") or "", ix.get("units") or {}, settings(), _names(cfg)
    ser, now = _series(), t
    by: dict = {}
    grow: dict = {}                                # 各单位近 24 小时、7 天的增量（按人与按项目共用）
    dead: dict = {}                                # 删掉的单位：最后一次是 0，增量里算负的
    for u, r in units.items():
        rel = u[len(root):].lstrip("/") if u.startswith(root) else u
        top = rel.split("/", 1)[0] or "."
        o = by.setdefault(owner_of(top, sets["owners"], names), {"dirs": set(), "units": []})
        o["dirs"].add(top)
        s = ser.get(u)
        g = grow[u] = tuple((r["b"] - _at(s, now - w)) if s and r.get("b") is not None else 0 for w in (86400, 7 * 86400))
        o["units"].append({"u": rel, "b": r.get("b"), "n": r.get("n"), "m": r.get("m"), "cold": sum((r.get("age") or [])[COLD:]),
                           "d24": g[0], "d7": g[1], "t": r.get("t"), "s": r.get("s"), "due": r.get("due"),
                           "err": r.get("err"), "top": (r.get("top") or [])[:5]})
    for u, s in ser.items():
        if u not in units and s[-1][1] == 0 and s[-1][0] >= now - 7 * 86400:
            rel = u[len(root):].lstrip("/") if u.startswith(root) else u
            top = rel.split("/", 1)[0] or "."
            g = dead[u] = (-_at(s, now - 86400), -_at(s, now - 7 * 86400))
            o = by.get(owner_of(top, sets["owners"], names))
            if o is not None:
                o.setdefault("gone", []).append({"u": rel, "d24": g[0], "d7": g[1]})
    hrs = _rows("hours.jsonl")
    out = []
    for name, o in by.items():
        us, gone = sorted(o["units"], key=lambda x: -(x["b"] or 0)), o.get("gone", [])
        done = [x for x in us if x["b"] is not None]
        lim = sets["limits"].get(name)
        b = sum(x["b"] for x in done)
        out.append({"name": name, "person": name in names, "public": name == PUBLIC, "dirs": sorted(o["dirs"]), "b": b,
                    "n": sum(x["n"] or 0 for x in done), "m": max((x["m"] or 0 for x in done), default=0) or None,
                    "cold": sum(x["cold"] for x in done), "d24": sum(x["d24"] for x in us + gone), "d7": sum(x["d7"] for x in us + gone),
                    "t0": min((x["t"] for x in done), default=None), "pending": len(us) - len(done), "limit": lim,
                    "over": bool(lim and b > lim * GIB), "units": us,
                    "spark": [[r["t"], (r.get("own") or {}).get(name)] for r in hrs[-30 * 24::24] if (r.get("own") or {}).get(name) is not None]})
    out.sort(key=lambda x: -x["b"])
    own = {o["name"]: o["b"] for o in out}
    if not hrs or t - hrs[-1]["t"] >= 3600:
        append_row(_f("hours.jsonl"), {"t": round(t), "total": df["total"], "used": df["used"], "free": df["free"], "own": own}, HOURS_KEEP, t)
        hrs = _rows("hours.jsonl")
    scanned = sum(o["b"] for o in out)
    dc = cfg.get("disk") or {}
    scan = next((e["status"]["disk"] for e in _shared_hosts(hosts) if ((e.get("status") or {}).get("disk") or {}).get("mine")), None) or {}   # 拿着租约在扫的那台
    v = {"root": root, "scanner": ix.get("host"), "at": ix.get("t"), "df": df, "owners": out,
         "eta": _eta(hrs, df, t - 86400), "eta7": _eta(hrs, df, t - 7 * 86400),
         "units": len(units), "pending": sum(o["pending"] for o in out), "unscanned": max(0, df["used"] - scanned),
         "d24": sum(g[0] for g in grow.values()) + sum(g[0] for g in dead.values()),
         "d7": sum(g[1] for g in grow.values()) + sum(g[1] for g in dead.values()), "cold": sum(o["cold"] for o in out),
         "hist": [[r["t"], r["used"]] for r in hrs if r["t"] >= t - 30 * 86400],
         "warn_gb": dc.get("warn_gb", WARN_GB), "soon_days": dc.get("soon_days", SOON_D), "burst_gb": dc.get("burst_gb", BURST_GB),
         "bigwrite_gb": dc.get("bigwrite_gb", BIGWRITE_GB), "names": names, "owners_set": sets["owners"],
         "now": scan.get("now"), "yield": scan.get("yield")}       # 正在扫的单位；共享盘慢、推迟中的说明
    v["ours"] = _ours(hosts, units, ser, grow, dead, t, v)
    st = load_json(_f("state.json"), {})
    cols, k = st.setdefault("colors", {}), len(st.get("colors") or {})
    for o in out:                                  # 只给名单里的人定颜色；对不上人的目录在页面上画灰色，免得和人撞色
        if o["person"]:
            pick_color(cols, o["name"])
    if len(cols) > k:
        save_json(_f("state.json"), st)
    v["colors"] = cols
    msgs = _alerts(v, t)
    return v, msgs


def _ours(hosts: list[dict], units: dict, ser: dict, grow: dict, gone: dict, t: float, v: dict) -> dict | None:
    """自己的目录（自用主机的 data_root）下各单位，按 worker 记的项目目录（状态的 storage.projects）分到项目：主机页的共享盘条、项目页的占用。
    dirs 每项带近 24 小时、7 天的增量（grow）；projects 是各项目的合计（含删掉的单位 gone）、还没扫到的单位数、涨得快不快（_burst），
    以及近来结束的任务的写入量（_job_writes），#53。worker 把登记了清单的项目根拆开扫（Disk.want），项目根下的一级目录各是一个单位。"""
    hc = {h["id"]: h for h in active_hosts()}
    sh = _shared_hosts(hosts, hc)
    base = next((hc[e["id"]]["data_root"].rstrip("/") for e in sh if e["id"] in hc), None)
    if not base:
        return None
    projects: dict = {}
    for e in sh:
        projects.update(((e.get("status") or {}).get("storage") or {}).get("projects") or {})
    roots: dict = {}
    for d, p in projects.items():
        roots.setdefault(p, set()).add(d.rstrip("/"))
    of = lambda u: next((p for d, p in projects.items() if u == d.rstrip("/") or u.startswith(d.rstrip("/") + "/")), None)   # noqa: E731
    dirs, pend = [], {}
    for u, r in units.items():
        if not u.startswith(base + "/"):
            continue
        p = of(u)
        if r.get("b") is None:
            pend[p] = pend.get(p, 0) + 1
            continue
        d24, d7 = grow.get(u, (0, 0))
        dirs.append({"u": u, "name": u[len(base) + 1:], "b": r["b"], "t": r.get("t"), "p": p, "d24": d24, "d7": d7,
                     "n": r.get("n"), "m": r.get("m"), "top": (r.get("top") or [])[:5]})
    rows = [dict(r, host=e["id"]) for e in sh for r in load_rows(usage_file(e["id"]))
            if r.get("t", 0) >= t - JOBS_S and r.get("p") in roots and r.get("root")]          # 只认清单任务：单元申请没有 root
    running = [(e["id"], j.get("project"), j.get("name"), j.get("t0")) for e in sh for j in (e.get("status") or {}).get("running") or []]
    out = {}
    for p, rs in roots.items():
        mine = [x for x in dirs if x["p"] == p]
        gn = [{"u": u, "d24": a, "d7": b} for u, (a, b) in gone.items() if of(u) == p]
        d24 = sum(x["d24"] for x in mine + gn)
        out[p] = {"roots": sorted(rs), "b": sum(x["b"] for x in mine), "d24": d24, "d7": sum(x["d7"] for x in mine + gn),
                  "pending": pend.get(p, 0), "burst": _burst(d24, v), "gone": gn,
                  "jobs": _job_writes(p, sorted(rs), ser, units, rows, running, t, v["bigwrite_gb"])}
    return {"base": base, "dirs": sorted(dirs, key=lambda x: -x["b"]), "projects": out}


def _burst(d24: float, v: dict) -> bool:
    """近 24 小时涨得太快（按人的 burst: 与按项目的 disk: 告警同一条线）：超过 burst_gb，且超过剩余的 BURST_FREE。"""
    return d24 > v["burst_gb"] * GIB and d24 > BURST_FREE * v["df"]["free"]


def _write(sers: list, s: float, e: float, live=()) -> tuple:
    """[s, e] 这段时间项目目录的净增：每个单位取开跑前最后一次扫描到结束后第一次扫描之差，加起来。
    开跑前还没有的单位只在它从 0 算起（看板记了新建，absorb 的 born）时按 0 起算；拆出来的、结束以后才建的算不了，跳过；
    结束以后还没重扫的记进 pend（最后一次是 0 的只有已不在索引里（不在 live）才算删掉了、之后一直是 0）。返回 (净增, [(单位, 增量)], 最早用到的扫描时刻, 最晚的, pend, 用到的那组扫描)。时刻都是 worker 写的（索引与用量行同一口径）。"""
    tot, parts, lo, hi, pend, used = 0, [], None, None, 0, []
    for u, sr in sers:
        k0 = bisect.bisect_right(sr, (s, math.inf)) - 1
        if k0 >= 0:
            t0, b0 = sr[k0]
        elif sr[0][1] == 0 and sr[0][0] <= e:
            t0, b0 = sr[0]
        else:
            continue
        k1 = bisect.bisect_left(sr, (e, -1))
        if k1 < len(sr):
            t1, b1 = sr[k1]
        elif sr[-1][1] == 0 and u not in live:     # 删掉了：之后一直是 0
            t1, b1 = sr[-1]
        else:
            pend += 1
            continue
        if b1 != b0:
            tot += b1 - b0
            parts.append((u, b1 - b0))
            used.append((u, t0, t1))
            lo, hi = min(lo or t0, t0), max(hi or t1, t1)
    return tot, parts, lo, hi, pend, tuple(sorted(used))


def _job_writes(p: str, roots: list[str], ser: dict, units: dict, rows: list[dict], running: list, t: float, big_gb: float) -> list[dict]:
    """项目 p 近 JOBS_S 内结束的任务各写了多少（_write：项目根下各单位开跑前与跑完那次重扫之差；worker 在任务跑完时提前重扫，Worker.disk_ends）。
    rows 是工作台存的用量记录（_keep_usage 的行加 host），running 是各主机在跑的 (主机, 项目, 任务, 开跑)。
    同一段时间本项目别的任务写的也在里面，with 记同期还有几个本项目任务（用量记录与在跑的）。还有单位没重扫的标 pending，等满 JOB_WAIT_S 就按已有的算；
    big：算完了、净增超过 big_gb GiB（collect 据此给实验会话 bigwrite: 提示）。用的是同一组扫描的几个任务（并行的）量出来是同一个数，
    grp 记其中最早结束的那个（「名字:结束时刻」），collect 按它合成一条。只留净增 64 MiB 以上或还在等重扫的，近的在前，最多 30 个。
    只看此刻索引里的单位（units）与删掉了的（最后一次是 0）：拆开以前整个扫的旧单位不再扫，不算等重扫。"""
    sers = [(u, sr) for u, sr in ser.items() if any(u == r or u.startswith(r + "/") for r in roots) and (u in units or sr[-1][1] == 0)]
    name = lambda u: next((u[len(r) + 1:] for r in roots if u == r or u.startswith(r + "/")), u)   # noqa: E731
    mine = sorted((r for r in rows if r.get("p") == p), key=lambda r: r["t"])
    out, grp = [], {}
    for r in mine:
        tot, parts, lo, hi, pend, used = _write(sers, r["s"], r["t"], units)
        wait = pend > 0 and t - r["t"] < JOB_WAIT_S
        if abs(tot) < 64 * 2 ** 20 and not wait:
            continue
        a, z = lo or r["s"], hi or r["t"]
        n = sum(1 for o in mine if o is not r and o["s"] < z and o["t"] > a) + sum(1 for _, q, _, s0 in running if q == p and (s0 or 0) < z)
        out.append({"name": r.get("n"), "host": r["host"], "t0": r["s"], "t1": r["t"], "st": r.get("st"), "b": tot,
                    "top": [[name(u), d] for u, d in sorted(parts, key=lambda x: -x[1])[:3]], "from": lo, "to": hi,
                    "pending": pend, "wait": wait, "with": n, "big": not wait and tot > big_gb * GIB,
                    "grp": grp.setdefault(used, f"{r.get('n')}:{r['t']}") if used else f"{r.get('n')}:{r['t']}"})
    return sorted(out, key=lambda x: -x["t1"])[:30]


def _alerts(v: dict, t: float) -> list[str]:
    """快满、某人暴涨弹通知；超过提醒线只记事件。都记进组内事件，同一件事恢复以前不重报。v["alerts"] 是此刻还成立的。"""
    df, free = v["df"], v["df"]["free"]
    cur = {}
    if free < v["warn_gb"] * GIB:
        cur["low"] = (f"共享盘只剩 {free / GIB:.0f} GiB（低于 {v['warn_gb']} GiB）", True)
    if v["eta"] is not None and v["eta"] < v["soon_days"] and free < SOON_FREE * df["total"]:
        cur["soon"] = (f"共享盘按近 24 小时的速度约 {v['eta']} 天后满（剩 {free / GIB:.0f} GiB）", True)
    for o in v["owners"]:
        if _burst(o["d24"], v):
            cur["burst:" + o["name"]] = (f"{o['name']} 近 24 小时共享盘 +{o['d24'] / GIB:.0f} GiB，按这个速度约 {free / o['d24']:.1f} 天占满剩余的 {free / GIB:.0f} GiB", True)
        if o["over"]:
            cur["over:" + o["name"]] = (f"{o['name']} 在共享盘上占 {o['b'] / GIB:.0f} GiB，超过提醒线 {o['limit']:g} GiB", False)
    st = load_json(_f("state.json"), {})
    was = set(st.get("alerts") or [])
    evs = [{"kind": "disk", "who": k.split(":", 1)[1] if ":" in k else None, "msg": m} for k, (m, _) in cur.items() if k not in was]
    log_events(evs, t)
    st["alerts"] = sorted(cur)
    save_json(_f("state.json"), st)
    v["alerts"] = [{"key": k, "msg": m, "notify": n} for k, (m, n) in cur.items()]
    return [m for k, (m, n) in cur.items() if n and k not in was]


def host_work_disk(e: dict, hcfg: dict | None = None, t: float | None = None) -> dict | None:
    """独立主机本地工作盘汇总（唯一实现，overview._annotate、disk.project_storage、页面与 labmon disk 共用，#114）：
    区分共享盘与独立机本地工作盘；计算各项目占用、公共目录占用、磁盘闸门与 14 天闲置可回收候选（>= 1 GiB 且无活跃队列），绝不自动删 projects/ 产物。"""
    if not (e.get("standalone") or is_standalone(hcfg)):
        return None
    t = time.time() if t is None else t
    st = e.get("status") or {}
    info = st.get("info") or {}
    d0 = next(iter(info.get("disk") or []), None)
    sg = st.get("storage") or {}
    wk = sg.get("work") or {}
    loc = sg.get("local") or {}
    if not d0 and not wk and not loc:
        return None
    total = d0["total"] * GIB if d0 and d0.get("total") is not None else None
    free = d0["free"] * GIB if d0 and d0.get("free") is not None else None
    used = (total - free) if total is not None and free is not None else None
    root = wk.get("root") or (d0.get("path") if d0 else None) or (hcfg or {}).get("data_root") or e.get("data_root") or ""
    warn_gb = (hcfg or {}).get("disk_warn_gb") if hcfg and hcfg.get("disk_warn_gb") is not None else e.get("disk_warn_gb")
    stop_gb = (hcfg or {}).get("disk_stop_gb") if hcfg and hcfg.get("disk_stop_gb") is not None else e.get("disk_stop_gb")
    quota_gb = (hcfg or {}).get("disk_quota_gb") if hcfg and hcfg.get("disk_quota_gb") is not None else e.get("disk_quota_gb")
    active_p = {j.get("project") for k in ("running", "waiting") for j in st.get(k) or [] if j.get("project")}
    active_p.update(q.get("project") for q in st.get("queues") or [] if q.get("project") and not q.get("empty") and not q.get("sleeping"))
    projects = {}
    reclaimable = []
    wprojs = wk.get("projects") or {}
    # 从 storage.projects（{物理路径: 项目 id}）建反向索引，避免仅凭项目 ID 猜测目录名
    reg_dir_by_pid = {}
    root_clean = root.rstrip("/")
    for rpath, rpid in (sg.get("projects") or {}).items():
        if rpid and rpath and rpid not in reg_dir_by_pid:
            rp = str(rpath).rstrip("/")
            reg_dir_by_pid[rpid] = rp[len(root_clean) + 1:] if root_clean and rp.startswith(root_clean + "/") else rp
    all_pids = sorted(set(wprojs) | set(loc))
    for pid in all_pids:
        wp = wprojs.get(pid) or {}
        lp = loc.get(pid) or {}
        b = int(wp.get("b") or 0)
        m = wp.get("m")
        cache_b = int(lp.get("cache") or 0)
        scratch_b = int(lp.get("scratch") or 0)
        is_act = pid in active_p
        idle_days = int((t - m) // 86400) if m and t > m else 0
        # 优先使用 WorkDisk.summary 记录的真实目录 dir 或登记映射，绝不盲目用 pid 反推物理路径
        rel_dir = wp.get("dir") or reg_dir_by_pid.get(pid) or f"projects/{pid}"
        full_dir = rel_dir if rel_dir.startswith("/") else f"{root_clean}/{rel_dir}" if root_clean else rel_dir
        rec_proj = (not is_act) and bool(m) and (t - m >= RECLAIM_DAYS * 86400) and (b >= RECLAIM_MIN_B)
        if rec_proj:
            reclaimable.append({"u": rel_dir, "path": full_dir, "project": pid, "kind": "project",
                                "b": b, "idle_days": idle_days, "m": m})
        lm = lp.get("m") or m
        l_idle = int((t - lm) // 86400) if lm and t > lm else 0
        if (not is_act) and cache_b >= RECLAIM_MIN_B and (not lm or t - lm >= RECLAIM_DAYS * 86400):
            reclaimable.append({"u": f".tmp/cache/{pid}", "path": f"{root_clean}/.tmp/cache/{pid}", "project": pid, "kind": "cache",
                                "b": cache_b, "idle_days": l_idle, "m": lm})
        if (not is_act) and scratch_b >= RECLAIM_MIN_B and (not lm or t - lm >= RECLAIM_DAYS * 86400):
            reclaimable.append({"u": f".tmp/jobs/{pid}", "path": f"{root_clean}/.tmp/jobs/{pid}", "project": pid, "kind": "scratch",
                                "b": scratch_b, "idle_days": l_idle, "m": lm})
        projects[pid] = {
            "b": b, "m": m, "d24": wp.get("d24") or 0, "top": wp.get("top") or [],
            "dir": rel_dir, "cache": cache_b, "scratch": scratch_b, "active": is_act,
            "idle_days": idle_days, "reclaimable": rec_proj,
        }
    reclaimable.sort(key=lambda x: -x["b"])
    common = wk.get("common") or {}
    dirs = [
        {"u": v["dir"], "name": pid, "p": pid, "b": v["b"], "d24": v["d24"], "m": v["m"]}
        for pid, v in projects.items() if v["b"] > 0
    ] + [
        {"u": k, "name": k, "p": None, "b": int(b), "d24": None, "m": None}
        for k, b in common.items() if (b or 0) > 0
    ]
    dirs.sort(key=lambda x: -x["b"])
    work_b = wk.get("b")
    low = bool(free is not None and warn_gb and warn_gb > 0 and free < warn_gb * GIB)
    quota_warn = bool(quota_gb and quota_gb > 0 and work_b is not None and work_b >= quota_gb * 0.9 * GIB)
    return {
        "host": e.get("id"), "label": e.get("label") or e.get("id"), "root": root,
        "total": total, "free": free, "used": used, "work_b": work_b,
        "t": wk.get("t"), "pending": wk.get("pending") or 0,
        "warn_gb": warn_gb, "stop_gb": stop_gb, "quota_gb": quota_gb,
        "low": low, "quota_warn": quota_warn,
        "disk_gate": st.get("disk_gate"),
        "dirs": dirs, "projects": projects, "common": common,
        "reclaimable": reclaimable,
    }


def project_storage(pid: str, snap: dict | None = None) -> dict:
    """项目 pid 的存储（项目快照的 storage：项目页「本项目占用」、labmon disk 读它，#53；字段见 docs/schema.md）。都是字节与 epoch 秒：
    共享盘上的项目根 roots、合计 b、近 24 小时 d24 与 7 天 d7（含删掉的）、还没扫到的单位数 pending、涨得快不快 burst（disk: 告警），
    项目根下各一级目录 dirs（name 是相对项目根的路径：「.」是项目根本层的文件，空串是还没拆开时的整个项目目录）、删掉的 gone、
    近来结束的任务的写入量 jobs（_job_writes）；共享盘整块的 at（扫描索引的时刻）、free、total、eta（按近 24 小时约几天满）、eta7、warn_gb、burst_gb、bigwrite_gb；
    各主机本地盘的缓存与单格临时 local；独立主机本地工作盘 standalone（#114）。"""
    snap = snap if snap is not None else snapshot()
    dk = (snap or {}).get("disk") or {}
    ours = dk.get("ours") or {}
    pr = (ours.get("projects") or {}).get(pid) or {}
    roots = pr.get("roots") or []
    rel = lambda u: next((u[len(r) + 1:] for r in roots if u == r or u.startswith(r + "/")), u)   # noqa: E731
    dirs = [{"path": d["u"], "name": rel(d["u"]), **{k: d.get(k) for k in ("b", "d24", "d7", "n", "m", "t", "top")}}
            for d in ours.get("dirs") or [] if d["p"] == pid]
    local = {h["id"]: loc for h in (snap or {}).get("hosts") or [] for loc in [(((h.get("status") or {}).get("storage") or {}).get("local") or {}).get(pid)] if loc}
    standalone = {}
    hc = {h["id"]: h for h in active_hosts()}
    t = (snap or {}).get("generated") or time.time()
    for h in (snap or {}).get("hosts") or []:
        if not (h.get("standalone") or is_standalone(hc.get(h.get("id")))):
            continue
        st = h.get("status") or {}
        wd = h.get("work_disk") or host_work_disk(h, hc.get(h.get("id")), t)
        if not wd:
            continue
        has_pid = (pid in (wd.get("projects") or {})
                   or any(q.get("project") == pid for q in st.get("queues") or [])
                   or any(j.get("project") == pid for k in ("running", "waiting") for j in st.get(k) or []))
        if not has_pid:
            continue
        pe = (wd.get("projects") or {}).get(pid) or {}
        standalone[h["id"]] = {
            "host": h["id"], "label": wd["label"], "root": wd["root"],
            "free": wd["free"], "total": wd["total"], "work_b": wd["work_b"],
            "warn_gb": wd["warn_gb"], "stop_gb": wd["stop_gb"], "quota_gb": wd["quota_gb"],
            "low": wd.get("low", False), "quota_warn": wd.get("quota_warn", False),
            "disk_gate": wd["disk_gate"], "t": wd["t"], "pending": wd["pending"],
            "b": pe.get("b", 0), "m": pe.get("m"), "d24": pe.get("d24", 0), "top": pe.get("top") or [],
            "cache": pe.get("cache", 0), "scratch": pe.get("scratch", 0),
            "reclaimable": [r for r in wd.get("reclaimable") or [] if r.get("project") == pid],
        }
    df = dk.get("df") or {}
    return {"roots": roots, "b": pr.get("b", sum(d["b"] for d in dirs)), "d24": pr.get("d24"), "d7": pr.get("d7"),
            "pending": pr.get("pending", 0), "burst": bool(pr.get("burst")), "dirs": dirs,
            "gone": [{"path": g["u"], "name": rel(g["u"]), "d24": g["d24"], "d7": g["d7"]} for g in pr.get("gone") or []],
            "jobs": pr.get("jobs") or [],
            "at": dk.get("at"), "free": df.get("free"), "total": df.get("total"), "eta": dk.get("eta"), "eta7": dk.get("eta7"),
            "warn_gb": dk.get("warn_gb"), "burst_gb": dk.get("burst_gb"), "bigwrite_gb": dk.get("bigwrite_gb"),
            "local": local, "standalone": standalone}


# 普通用户可见的共享盘字段白名单（#17、#211）：总盘概况与提醒线；不含全组 names、owners_set、正在扫的路径 now 与全组聚合量
VIEW_DISK = ("root", "df", "eta", "eta7", "hist", "warn_gb", "soon_days", "burst_gb", "at", "units", "pending", "yield")
VIEW_DISK_ALERTS = ("low", "soon")


def person_storage(person: str | None, snap: dict | None = None) -> dict | None:
    """共享盘按人投影（#211，users.view 与 api.my_disk 共用唯一实现）：
    保留总盘白名单字段（VIEW_DISK）与总盘告警（low、soon），以及 person 名下 owners 条目、本人颜色与本人告警（burst:<人>、over:<人>）；
    屏蔽他人目录、公共目录明细、全组名单 names、归属表 owners_set 与正在扫描的路径 now。person 为空时返回总盘概况与空 owners。"""
    snap = snapshot() if snap is None else snap
    dk = (snap or {}).get("disk")
    if not isinstance(dk, dict):
        return None
    d = {k: dk[k] for k in VIEW_DISK if k in dk}
    if "df" not in d and "error" in dk:
        d["error"] = dk["error"]
    mine = [o for o in dk.get("owners") or []
            if person and not o.get("public") and isinstance(o.get("name"), str) and o["name"].casefold() == person.casefold()]
    names = {o["name"] for o in mine}
    if person:
        names.add(person)
        d["person"] = mine[0]["name"] if mine else person
    d["owners"] = mine
    cols = {k: v for k, v in (dk.get("colors") or {}).items() if any(k.casefold() == n.casefold() for n in names)}
    if cols:
        d["colors"] = cols
    cf = {n.casefold() for n in names}
    d["alerts"] = [a for a in dk.get("alerts") or []
                   if a.get("key") in VIEW_DISK_ALERTS
                   or (isinstance(a.get("key"), str) and ":" in a["key"]
                       and a["key"].split(":", 1)[0] in ("burst", "over")
                       and a["key"].split(":", 1)[1].casefold() in cf)]
    return d
