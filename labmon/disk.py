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
import re
import time

from . import fleet as F
from .config import GIB, active_hosts, all_hosts, pick_color
from .records import log_events
from .state import append_row, file_sig, load_json, load_rows, save_json, snapshot

WARN_GB = 30            # 剩余低于这么多 GiB 告警（[fleet.disk] warn_gb 可改）
SOON_D = 3              # 按近 24 小时的速度不到这么多天就满，且剩余已低于 SOON_FREE，告警（soon_days）
SOON_FREE = 0.2
BURST_GB = 30           # 一人近 24 小时增长超过这么多 GiB，且超过剩余的 BURST_FREE，告警（burst_gb）
BURST_FREE = 0.1
COLD = 3                # 分龄（7、30、90、365 天）从第几档起算“90 天没改过”
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
    born = lambda u: ou and u not in ou and not any(u.startswith(s + "/") for s in split)
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


def _df(hosts: list[dict], ix: dict) -> dict | None:
    """总量与剩余（字节）：最新的一台自用主机读的 statvfs；都没有就用索引里扫描者记的。"""
    best = None
    for e in hosts:
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
    for u, r in units.items():
        rel = u[len(root):].lstrip("/") if u.startswith(root) else u
        top = rel.split("/", 1)[0] or "."
        o = by.setdefault(owner_of(top, sets["owners"], names), {"dirs": set(), "units": []})
        o["dirs"].add(top)
        s = ser.get(u)
        grow = lambda w: (r["b"] - _at(s, now - w)) if s and r.get("b") is not None else 0
        o["units"].append({"u": rel, "b": r.get("b"), "n": r.get("n"), "m": r.get("m"), "cold": sum((r.get("age") or [])[COLD:]),
                           "d24": grow(86400), "d7": grow(7 * 86400), "t": r.get("t"), "s": r.get("s"), "due": r.get("due"),
                           "err": r.get("err"), "top": (r.get("top") or [])[:5]})
    for u, s in ser.items():                       # 删掉的单位：最后一次是 0，增量里算负的
        if u not in units and s[-1][1] == 0 and s[-1][0] >= now - 7 * 86400:
            rel = u[len(root):].lstrip("/") if u.startswith(root) else u
            top = rel.split("/", 1)[0] or "."
            o = by.setdefault(owner_of(top, sets["owners"], names), {"dirs": set(), "units": []})
            o.setdefault("gone", []).append({"u": rel, "d24": -_at(s, now - 86400), "d7": -_at(s, now - 7 * 86400)})
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
    scan = next((e["status"]["disk"] for e in hosts if ((e.get("status") or {}).get("disk") or {}).get("mine")), None) or {}   # 拿着租约在扫的那台
    v = {"root": root, "scanner": ix.get("host"), "at": ix.get("t"), "df": df, "owners": out,
         "eta": _eta(hrs, df, t - 86400), "eta7": _eta(hrs, df, t - 7 * 86400),
         "units": len(units), "pending": sum(o["pending"] for o in out), "unscanned": max(0, df["used"] - scanned),
         "d24": sum(o["d24"] for o in out), "d7": sum(o["d7"] for o in out), "cold": sum(o["cold"] for o in out),
         "hist": [[r["t"], r["used"]] for r in hrs if r["t"] >= t - 30 * 86400],
         "warn_gb": dc.get("warn_gb", WARN_GB), "soon_days": dc.get("soon_days", SOON_D), "burst_gb": dc.get("burst_gb", BURST_GB),
         "names": names, "owners_set": sets["owners"],
         "now": scan.get("now"), "yield": scan.get("yield")}       # 正在扫的单位；共享盘慢、推迟中的说明
    v["ours"] = _ours(hosts, units, root)
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


def _ours(hosts: list[dict], units: dict, root: str) -> dict | None:
    """自己的目录（自用主机的 data_root）下各单位，按 worker 记的项目目录分到项目：主机页的共享盘条、项目页的占用。"""
    hc = {h["id"]: h for h in active_hosts()}
    base = next((hc[e["id"]]["data_root"].rstrip("/") for e in hosts if e["id"] in hc), None)
    if not base:
        return None
    projects = {}
    for e in hosts:
        projects.update(((e.get("status") or {}).get("storage") or {}).get("projects") or {})
    dirs = []
    for u, r in units.items():
        if not u.startswith(base + "/") or r.get("b") is None:
            continue
        p = next((p for d, p in projects.items() if u == d or u.startswith(d.rstrip("/") + "/")), None)
        dirs.append({"u": u, "name": u[len(base) + 1:], "b": r["b"], "t": r.get("t"), "p": p})
    return {"base": base, "dirs": sorted(dirs, key=lambda x: -x["b"])}


def _alerts(v: dict, t: float) -> list[str]:
    """快满、某人暴涨弹通知；超过提醒线只记事件。都记进组内事件，同一件事恢复以前不重报。v["alerts"] 是此刻还成立的。"""
    df, free = v["df"], v["df"]["free"]
    cur = {}
    if free < v["warn_gb"] * GIB:
        cur["low"] = (f"共享盘只剩 {free / GIB:.0f} GiB（低于 {v['warn_gb']} GiB）", True)
    if v["eta"] is not None and v["eta"] < v["soon_days"] and free < SOON_FREE * df["total"]:
        cur["soon"] = (f"共享盘按近 24 小时的速度约 {v['eta']} 天后满（剩 {free / GIB:.0f} GiB）", True)
    for o in v["owners"]:
        if o["d24"] > v["burst_gb"] * GIB and o["d24"] > BURST_FREE * free:
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


def project_storage(pid: str, snap: dict | None = None) -> dict:
    """项目页要的项目 pid 的存储：共享盘上的目录与占用（dirs、at 是扫描时刻；free、total 是共享盘剩余与总量，都是字节；warn_gb 是告警线），
    各主机本地盘的缓存与单格临时目录。"""
    snap = snap if snap is not None else snapshot()
    dk = (snap or {}).get("disk") or {}
    ds = [d for d in (dk.get("ours") or {}).get("dirs") or [] if d["p"] == pid]
    local = {h["id"]: loc for h in (snap or {}).get("hosts") or [] for loc in [(((h.get("status") or {}).get("storage") or {}).get("local") or {}).get(pid)] if loc}
    df = dk.get("df") or {}
    return {"dirs": {d["u"]: d["b"] for d in ds}, "at": {d["u"]: d["t"] for d in ds}, "free": df.get("free"), "total": df.get("total"),
            "warn_gb": dk.get("warn_gb"), "local": local}
