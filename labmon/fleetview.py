"""组内服务器给页面与命令行算的数据（探测、守护与状态文件在 fleet.py，平台操作在 fleetops.py）：
工作台「组内服务器」的 view（各台现状、近 7 天逐小时状态、7/30 天统计、按人汇总、配额、未识别进程、自用主机的行），
按人汇总与人员详情都经 _parts 拆小时行：借用中的那部分（行的 bw）算给借的人，主人那边记成“借出”（lent）；自用主机与组内服务器同一套。
详情页的曲线 detail（组内服务器与自用主机：known），人员详情 person，命令行 labmon fleet [status | probe] 的 show。不连 ssh，只读状态目录。"""
from __future__ import annotations

import time

from . import borrow as B
from . import disk as DK
from . import fleet as F
from . import fleetboot as FB
from . import fleetops as FO
from . import lease as LS
from .config import ConfigError, active_hosts, fmt_time, hosts_file, parse_time
from .service import notify
from .state import file_sig, load_rows

_CACHE: dict[str, tuple[tuple | None, list]] = {}


def _hours(sid: str) -> list[dict]:
    f = F.state_dir() / "hours" / f"{sid}.jsonl"
    m = file_sig(f)
    c = _CACHE.get(str(f))
    if not c or c[0] != m:
        _CACHE[str(f)] = c = (m, load_rows(f))
    return c[1]


def _stats(rows: list[dict], since: float) -> dict:
    """一组小时行（或 _parts 拆出的份）的统计。按小时归一：每份的计数先除以那一小时整台的样本数 N（缺省就是自己的 n），
    再相加。自用主机每分钟一次、探针约 10 分钟一次，混在一个人身上也按小时算；借的人那份只占一小时的一部分。
    lent（借出）只在主人那份里有：算进时间占比，不算进利用率与可共享。"""
    rs = [r for r in rows if r["h"] >= since and r["n"]]
    if not rs:
        return {"n": 0}
    w = lambda r: r.get("N") or r["n"]  # noqa: E731
    ks = F.STATES + (("lent",) if any(r["c"].get("lent") for r in rs) else ())
    hrs = sum(r["n"] / w(r) for r in rs)
    h = {k: sum(r["c"].get(k, 0) / w(r) for r in rs) for k in ks}
    nr, own = sum(r.get("nr", 0) / w(r) for r in rs), hrs - h.get("lent", 0)
    return {"n": sum(r["n"] for r in rs), "hours": round(hrs, 1), "frac": {k: round(h[k] / hrs, 3) for k in ks}, "h": {k: round(h[k], 1) for k in ks},
            "share": round(sum(r["sh"] / w(r) for r in rs) / own, 3) if own else 0, "share_h": round(sum(r["sh"] / w(r) for r in rs), 1),
            "util_job": round(sum(r["uj"] / w(r) for r in rs) / h["job"], 1) if h["job"] else None,
            "vram_job": round(sum(r["vj"] / w(r) for r in rs) / h["job"]) if h["job"] else None,
            "cpu_real": round(sum(r["cr"] / w(r) for r in rs) / nr, 2) if nr else None,
            "mem_real": round(sum(r["mr"] / w(r) for r in rs) / nr, 1) if nr else None}


def _parts(r: dict) -> list[tuple[str, dict]]:
    """一个小时行拆成按人的几份 [(名字, 份)]：份与小时行同形，N 是那一小时整台的样本数（_stats 按它归一）。
    主人那份：减去借用中的部分，借出的次数记成 c["lent"]（lent_by 按借的人分），n 仍是整台的；借的人那份：bw 里他的计数，from 是主人。"""
    bw = r.get("bw") or {}
    if not bw:
        return [(r["holder"], {**r, "N": r["n"]})]
    own = {k: r.get(k, 0) - sum(b.get(k, 0) for b in bw.values()) for k in F.HOUR_NUMS}
    c = {k: r["c"].get(k, 0) - sum(b["c"].get(k, 0) for b in bw.values()) for k in r["c"]}
    lent = {m: b["n"] for m, b in bw.items()}
    out = [(r["holder"], {**own, "h": r["h"], "N": r["n"], "n": r["n"], "c": {**{k: v for k, v in c.items() if v}, "lent": sum(lent.values())}, "lent_by": lent})]
    return out + [(m, {**b, "h": r["h"], "N": r["n"], "from": r["holder"]}) for m, b in bw.items()]


def _rows(sid: str, acc: dict | None) -> list[dict]:
    return _hours(sid) + ([acc] if acc else [])


def _by_person(since: float) -> dict[str, dict[str, list[dict]]]:
    """近期所有小时行（组内服务器、自用主机，含已换人、已删掉的、已归还的）按人拆开：{名字: {服务器: [份]}}。"""
    accs, d, out = F.accs(), F.state_dir() / "hours", {}
    ids = {f.stem for f in d.glob("*.jsonl")} if d.exists() else set()
    for sid in sorted(ids | set(accs)):
        for r in _rows(sid, accs.get(sid)):
            if r["h"] >= since:
                for name, part in _parts(r):
                    out.setdefault(name, {}).setdefault(sid, []).append(part)
    return out


def _lending(by: dict[str, list[dict]]) -> dict:
    """一人的借来、借出：borrowed [{id, from, h}]、lent [{id, to, h}]（小时数按那一小时整台的样本数折算）。"""
    bo, le = {}, {}
    for sid, ps in by.items():
        for p in ps:
            if p.get("from"):
                bo[(sid, p["from"])] = bo.get((sid, p["from"]), 0) + p["n"] / p["N"]
            for m, n in (p.get("lent_by") or {}).items():
                le[(sid, m)] = le.get((sid, m), 0) + n / p["N"]
    return {"borrowed": [{"id": k[0], "from": k[1], "h": round(v, 1)} for k, v in sorted(bo.items(), key=lambda kv: -kv[1])],
            "lent": [{"id": k[0], "to": k[1], "h": round(v, 1)} for k, v in sorted(le.items(), key=lambda kv: -kv[1])]}


def _strip(rows: list[dict], h0: int) -> tuple[list, int]:
    """近 7 天逐小时：[占多数的状态, 可共享比例(, 借给谁)] 或 None；以及最近连着几小时只有保活或空闲（卡片上标「闲置」）。"""
    cells = {r["h"]: r for r in rows if r["h"] >= h0}
    strip = [[_dominant(c), round(c["sh"] / c["n"], 2)] + ([sorted(c["bw"])] if c.get("bw") else []) if c else None
             for c in (cells.get(h) for h in range(h0, h0 + 168 * 3600, 3600))]
    idle_h = 0
    for c in reversed(strip[:-1] if strip and strip[-1] is None else strip):
        if not c or c[0] not in ("keepalive", "idle"):
            break
        idle_h += 1
    return strip, idle_h


def _dominant(r: dict) -> str:
    return max(F.STATES, key=lambda k: (r["c"].get(k, 0), -F.STATES.index(k)))


def view(hosts: list[dict] | None = None, t: float | None = None, cfg: dict | None = None) -> dict | None:
    """工作台「组内服务器」要的数据：各台现状（含保活守护的状态）、近 7 天逐小时状态、7/30 天统计、按人汇总、配额、未识别进程。
    hosts：工作台快照里的主机（overview.collect 给的），自用主机的行从它取状态。"""
    try:
        cfg = cfg if cfg is not None else F.conf()
    except ConfigError as x:
        return {"error": str(x), "servers": [], "mine": []}
    if not cfg:
        return None
    t = time.time() if t is None else t
    sts, off, busy, accs = F._load_state(), F.off_servers(), FO.busy(), F.accs()
    who = DK.whose(cfg, active_hosts())
    h0 = int(t // 3600 * 3600) - 167 * 3600
    snap = {e["id"]: e for e in hosts or []}
    servers = []
    for s in cfg["servers"]:
        st = sts.get(s["id"]) or {}
        rows = _rows(s["id"], accs.get(s["id"]))
        strip, idle_h = _strip(rows, h0)
        o = off.get(s["id"])
        # 借用中且 worker 状态新鲜（与自用主机同一门槛 fleet.worker_fresh，#174）：实时 sample 改用 worker 状态，避免滞后最多 10 分钟；
        # worker 过期、不在内网或掉线时回退探测样本。小时使用记录（hours/ 的 bw）仍由探针统一记，保持同一小时采样密度一致。
        e = snap.get(s["id"]) if not o and F.borrowed(s) else None
        live = F.worker_fresh(e)
        smp = F.from_worker(e["status"], cfg, s["holder"], job_owner=F.borrower(s, cfg)) if live else st.get("sample") or {}
        if smp:                                          # 每组实验归谁：按路径，对不上算兜底归属（借用中 labmon 任务归借用方，其余归使用人）
            smp = {**smp, "work": [w if w["kind"] == "keepalive" else {**w, "owner": who([w.get("cwd"), *reversed(w.get("chain") or [])]) or w.get("owner") or s["holder"]}
                                   for w in smp.get("work", [])]}
        lease = None if o else F.lease_end(st, cfg)
        servers.append({**{k: s.get(k) for k in ("id", "holder", "job", "label", "ssh", "port", "pool")},
                        "public": s["pool"] not in cfg["private_pools"], "last": st.get("last"), "error": None if o or live else st.get("error"),
                        "down": None if o or live else st.get("down"), "fails": 0 if live else st.get("fails", 0), "since": None if o or live else st.get("since"),
                        "started": (smp.get("started") if live else None) or st.get("started"),
                        "lease_end": lease, "lease_cut": B.cutoff({"holder": s["holder"]}, lease)[0], "off": o,   # lease_cut：借用最晚截止到它（借来用的表单提示）
                        "last_job": st.get("last_job"), "state": "off" if o else smp["state"] if live else "down" if st.get("down") else smp.get("state"),
                        "sample": None if o else smp or None,
                        # 本地盘开机以来的新写入（#149）：总量取这次探测的，路径取扫出的（换了容器的旧结果不要）
                        "lw": None if o else F.server_lw(st),
                        "strip": strip, "h0": h0, "idle_h": 0 if o else idle_h, "s7": _stats(rows, t - 7 * 86400), "s30": _stats(rows, t - 30 * 86400),
                        "guard": F.guard_state({**s, "off": o}, st, cfg, t, s["id"] in busy),
                        "borrow": {"since": parse_time(s.get("borrow_since")), "until": parse_time(s.get("borrow_until")), "for": s.get("borrow_for"), "by": F.borrower(s, cfg),
                                   "away": parse_time(s.get("borrow_away")), "returned": parse_time(s.get("borrow_returned"))} if s.get("borrow_since") else None,   # 时刻都是 epoch 秒
                        # 借得了（对方空着或轻用）的：对方在用什么、借来用时给对方留的量（按对方近 7 天轻用时的用量推荐）
                        "using": B.using(smp) if smp else "",
                        "reserve": B.reserve_for(s["id"], t) if smp and not o and not st.get("down") and not F.borrowed(s) and B.can_borrow(smp) else None})
    mine = []
    for x in FB.mine(cfg, hosts or []):                  # 自用主机：使用记录与组内服务器同一套（fleet.record_mine 记的）
        rows = _rows(x["id"], accs.get(x["id"]))
        strip, idle_h = _strip(rows, h0)
        mine.append({**x, "strip": strip, "h0": h0, "idle_h": idle_h, "s7": _stats(rows, t - 7 * 86400), "s30": _stats(rows, t - 30 * 86400),
                     "guard": F.guard_state(x, {}, cfg, t)})
    allx = [x for x in servers if not x["off"]] + mine
    priv = sum(1 for x in allx if x.get("pool") in cfg["private_pools"])
    pub = len(allx) - priv
    unknown = [{"id": x["id"], "holder": x["holder"], **w} for x in servers for w in (x["sample"] or {}).get("work", []) if w["kind"] == "unknown"]
    holders = sorted(({"holder": k, "servers": sorted({x["id"] for x in servers + mine if x["holder"] == k}),
                       **_stats([p for ps in v.values() for p in ps], t - 30 * 86400), **_lending(v)}
                      for k, v in _by_person(t - 30 * 86400).items()), key=lambda x: x["holder"])
    return {"ops": FO.view(cfg), "servers": servers, "mine": mine, "holders": holders, "unknown": unknown,
            "quota": {"private": priv, "private_max": cfg["private_max"], "public": pub, "public_max": cfg["public_max"],
                      "private_pools": cfg["private_pools"]},
            "interval": cfg["interval"], "share": cfg["share"], "names": F.NAMES}


def known(sid: str, cfg: dict | None) -> bool:
    """组内页能给曲线（detail）的服务器：[[fleet.servers]] 里的与自用主机（fleetboot.mine）。"""
    return sid in {s["id"] for s in (cfg or {}).get("servers", [])} or sid in {x["id"] for x in FB.mine(cfg)}


def detail(sid: str, days: float = 7, t: float | None = None) -> dict:
    """详情页要的曲线：近 days 天每次探测一行（页面点开详情页时再取，不放进每分钟生成的总览页）。"""
    t = time.time() if t is None else t
    return {"id": sid, "series": [r for r in load_rows(F.state_dir() / "series" / f"{sid}.jsonl") if r.get("t", 0) >= t - days * 86400]}


def person(name: str, days: int = 30, t: float | None = None) -> dict:
    """人员详情要的：近 days 天这人逐日各状态的小时数与实验时利用率，按服务器的统计（含已换给别人、已删掉的服务器、自用主机、借来的）。
    servers 每台一行：借来的带 from（主人），借出过的带 lent（借出的小时数，按借的人分在 lent_to）。"""
    t = time.time() if t is None else t
    since = t - days * 86400
    by = _by_person(since).get(name, {})
    day0 = time.mktime(time.localtime(since)[:3] + (0, 0, 0, 0, 0, -1))
    out = []
    while day0 < t:
        nxt = time.mktime(time.localtime(day0 + 36 * 3600)[:3] + (0, 0, 0, 0, 0, -1))
        st = _stats([r for v in by.values() for r in v if day0 <= r["h"] < nxt], 0)
        out.append({"t": round(day0), "h": st.get("h") or {k: 0 for k in F.STATES}, "util_job": st.get("util_job")})
        day0 = nxt
    srv = []
    for k, v in by.items():                              # 一台一行；借来的与自己占着的分开（同一台先占后借给别人、或换人后借来都可能）
        for frm in sorted({p.get("from") for p in v}, key=lambda x: x or ""):
            ps = [p for p in v if p.get("from") == frm]
            lend = _lending({k: ps})["lent"]
            srv.append({"id": k, "first": min(p["h"] for p in ps), "last": max(p["h"] for p in ps), **_stats(ps, since),
                        **({"from": frm} if frm else {}), **({"lent": round(sum(x["h"] for x in lend), 1), "lent_to": lend} if lend else {})})
    return {"name": name, "days": out, "servers": sorted(srv, key=lambda x: -x["last"]),
            "total": {**_stats([r for v in by.values() for r in v], since), **_lending(by)}}


def show(sid: str | None = None, probe: bool = False) -> int:
    """命令行：labmon fleet [status] 列各台现状；labmon fleet probe <id> 立即探一次并列出 GPU 进程、父进程链与归类。"""
    cfg = F.conf()
    if not cfg:
        print(f"[labmon] {hosts_file()} 里没有 [fleet] 段")
        return 1
    if probe:
        if sid not in {s["id"] for s in cfg["servers"]}:
            print(f"[labmon] 没有这一台：{sid}（可选：{'、'.join(s['id'] for s in cfg['servers'])}）")
            return 2
        if not LS.primary():                       # 备用的看板服务不探（#163，fleet.tick 里同样挡）：列的是当主时的最后一次
            print(f"[labmon] 没探：{LS.text()}")
        msgs = F.tick(force=sid, cfg=cfg, busy=FO.busy())
        notify(msgs)                               # 与看板一样弹通知（已提醒过的看板不会再弹）
        for m in msgs:
            print("  通知：" + m)
    v = view(cfg=cfg)
    for x in v["servers"]:
        if sid and x["id"] != sid:
            continue
        smp = x["sample"] or {}
        print(f"{x['holder']:<5} {x['id']:<12} {F.NAMES.get(x['state'], '未探到'):<6} 显存 {smp.get('vram', 0) / 1024:5.1f}/{(smp.get('vram_total') or 0) / 1024:.0f} GiB"
              f"  利用率 {smp.get('util') if smp.get('util') is not None else '—'}%  租期到 {fmt_time(x['lease_end']) if x['lease_end'] else '—'}"
              + (f"  错误：{x['error']}" if x["error"] else ""))
        if sid:
            for w in smp.get("work", []):
                print(f"  [{F.NAMES[w['kind']]}] {w['label']}  显存 {w['mb'] / 1024:.1f} GiB  {w['n']} 个 GPU 进程"
                      + (f"  规则 {w['rule']}" if w.get("rule") else ""))
                for a in w.get("chain") or []:
                    print(f"      {a}")
                if w.get("cwd"):
                    print(f"      目录 {w['cwd']}")
    return 0
