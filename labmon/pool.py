"""算力池（#13；B：池清单与入池开关，#24；C：自动借还与收回，#25）。池按借用的推广来做，复用借用、worker 调度与清单登记，不另写一套：
- 池清单：实验会话 labmon worker add pool ROOT JOBS --project P 交上来，登记落在共享盘 worker_home 的 hosts/pool/queues/
  （pool 是保留的伪主机 id，config.POOL；worker_home 取 config.pool_home），沿用“一条清单只归一台”。待放的清单经现有的代读
  进全局快照的 regs（state 为 pool）与项目车道（collect），别处 after= 等它由 worker 的 peers 认。
- 入池开关：[[fleet.servers]] 的 lend、lend_hours（fleet.conf 校验，fleet.lend_open 判时段），持有人本人在「我的账号 → 我的服务器」开，
  管理员在组内服务器抽屉代开（要能做平台操作、勾「已和这人说好」）。换人时 config.set_fleet_holder 一并去掉。
- 成员：管理员，或账号对应的人（users.person_of）有一台开了入池的组内服务器（member）。users.may_host 对 pool 问它。
- 自动借还（tick，工作台每轮、只在持有服务租约时，overview.collect 里快照写盘前）：
  还（_returns）：池借着的主机（主机项 borrow.pool）主人回来（worker 状态的 guest.owner）、关了入池或出了时段、POOL_IDLE_S 没有池任务在跑
  就 borrow.request_return（在跑的跑完就还；时段结束、租期截止由 borrow.check 的约定时间照旧管）；换了容器（组内探测的 started 与借来时记的不同）
  直接 borrow.finish(gone=True)。放（_place）：池里待放的、要还的池主机上的、在池主机上队首因资源等满 POOL_WAIT_S 的清单，按 fair_key 排先后，
  经 fit --list --json 判各台放不放得下，serve.reg_move 放过去（要还的主机上放不下的回池里待放）。借（want_borrow）：还有放不下的、
  平台操作格空着时，挑一台入池、在时段内、GPU 空着或只有保活、主人空闲够久的，fleetops.start("borrow", pool=True)。
- 收回（reclaim）：持有人或能做平台操作的管理员点，request_return(now=True) 按停法停下池任务、清单回池里重排。
持久的只有主机清单里的 lend、lend_hours、borrow_pool 与 borrow.json 各台的 pool 子项（started、since、busy、rest、fail、nofit、want）；
fit 的结果只缓存在内存里。
"""
from __future__ import annotations

import json
import posixpath
import time

from . import borrow as B
from . import fleet as F
from . import fleetops as FO
from . import lease as LS
from . import users as U
from .config import HOSTS_LOCK, POOL, ConfigError, fmt_dur, fmt_time, set_fleet_keys, user_hosts
from .labmon_worker import fair_key, person_use
from .records import log_events
from .service import log, notify
from .state import load_json, snapshot, state_dir
from .users import Denied, Ident, person_of
from .workerctl import run_out

POOL_IDLE_S = 15 * 60          # 借着的池服务器连着这么久没有池任务在跑（不算占位）就还（R4）
POOL_WAIT_S = 10 * 60          # 放过去的清单队首因资源等满这么久：当放不下，挪到别处（fit 判得下、worker 准入时放不下）
POOL_REST_S = 3 * 3600         # 主人回来、关了入池、收回之后这么久不再借这台（免得吃个饭回来又被借走）
POOL_MIN_LEFT_S = 2 * 3600     # 截止（时段结束与租期前 1 小时取早）离现在不到这么久的不借
POOL_FAIL_S = 3600             # 借不成（begin 失败）之后这么久不再借这台
POOL_NOFIT_S = 86400           # 借来后哪条都没跑起来、按 R4 还掉的：这个形状（卡数、单卡显存）这么久不再为它借这台
POOL_FIT_MAX = 4               # 每轮最多问几次 fit（每次一条 ssh，走长连接）
POOL_RETRY_S = 5 * 60          # 同一（清单, 候选主机）这么久内不重判
POOL_TELL_S = 2 * 3600         # 池里待放满这么久，只给实验会话报一次 pool_wait:（collect）
_FIT: dict = {}                # (清单, 候选主机) → (时刻, fit 的结果)：只在内存里，重启后重判


def _person(who: Ident, cfg: dict | None) -> str | None:
    """这个账号在组内人员表里是谁（users.person_of；没有 [fleet]、认不出为 None）。"""
    return person_of(who.name)[0] if cfg else None


def _servers(cfg: dict | None, person: str | None) -> list[dict]:
    return [s for s in (cfg or {}).get("servers", []) if person and s["holder"] == person]


def member(who: Ident, cfg: dict | None = None) -> bool:
    """能不能往池里交清单（worker add pool，users.may_host 的 pool）：管理员；或这人持有的组内服务器里有一台开了入池（lend，不看此刻在不在时段）。"""
    if who.role == "admin":
        return True
    try:
        cfg = F.conf() if cfg is None else cfg
    except ConfigError:
        return False
    return any(s.get("lend") for s in _servers(cfg, _person(who, cfg)))


def need_holder(who: Ident, sid: str, cfg: dict | None = None) -> dict:
    """要改 sid 这台的入池：是它的持有人本人（账号对应的人员是 holder）返回这台，否则抛 Denied(403)。管理员代开另由 set_lend 核。"""
    cfg = F.conf() if cfg is None else cfg
    s = next((x for x in (cfg or {}).get("servers", []) if x["id"] == sid), None)
    if s is None:
        raise Denied(404, f"组内服务器里没有 {sid}")
    if s["holder"] != _person(who, cfg):
        raise Denied(403, f"{sid} 不是你的服务器：只有持有人本人能开入池，管理员能代开")
    return s


def set_lend(sid: str, on: bool, hours: str | None, who: Ident, ops: bool, ok: bool) -> str:
    """POST /api/pool/lend：开、关这台的入池，改时段（None 或空是全天）。持有人本人直接改；管理员代别人开要能做平台操作（ops）且勾了
    「已和这人说好」（ok，关掉不用勾）。写进主机清单这台的 [[fleet.servers]]（set_fleet_keys，记一条配置改动）。返回给人看的一句。"""
    with HOSTS_LOCK:                 # 读配置、核持有人到写入同一把锁（与换人 set_fleet_holder 互斥：旧持有人的请求不会把 lend 写回新持有人名下）
        cfg = F.conf()
        s = next((x for x in (cfg or {}).get("servers", []) if x["id"] == sid), None)
        if s is None:
            raise Denied(404, f"组内服务器里没有 {sid}")
        mine = s["holder"] == _person(who, cfg)
        if not mine:
            if who.role != "admin":
                raise Denied(403, f"{sid} 不是你的服务器：只有持有人本人能开入池，管理员能代开")
            if not ops:
                raise Denied(403, "代别人开入池要能做平台操作：从内网入口来，或开启公网平台操作")
            if on and not ok:
                raise Denied(409, f"先和 {s['holder']} 说好，再勾「已和这人说好」")
        hours = (hours or "").strip() or None
        if on and hours and F.lend_span(hours) is None:
            raise Denied(400, f"时段「{hours}」认不出：写成 22:00-09:00 这样（每天一段，可跨零点），不写是全天")
        by = "" if mine else f"，{who.name} 代开（已和这人说好）" if on else f"，{who.name} 代关"
        say = f"{s['holder']}（{sid}）{'入池' + (f'，每天 {hours}' if hours else '，全天') if on else '退出算力池'}{by}"
        set_fleet_keys({"lend": True if on else None, "lend_hours": hours if on else None}, say, sid=sid)
    return say


def lend_view(s: dict, t: float, bst: dict | None = None) -> dict:
    """页面上一台的入池（「我的服务器」与组内服务器卡片、抽屉同一份）：{on 开没开, hours 时段（None 是全天）, open 此刻在不在时段里,
    lent 正借给算力池, returning 要还了（在跑的跑完就还）, rest 休息到几点（主人回来、关了入池、收回之后不借，epoch 秒；没有为 None）, say 那一句}。
    bst：borrow.json（不给就读）。"""
    out = {"on": bool(s.get("lend")), "hours": s.get("lend_hours"), "open": False, "lent": False, "returning": False, "rest": None, "say": "没入池"}
    st = (load_json(state_dir() / "borrow.json", {}) if bst is None else bst).get(s["id"]) or {}
    m = st.get("pool") or {}
    if F.borrowed(s) and s.get("borrow_pool"):
        out.update(lent=True, returning=bool(st.get("returning")), say="算力池借着，要还了（在跑的跑完就还）" if st.get("returning") else "算力池借着")
        return out
    if out["on"]:
        inside, nxt = F.lend_open(s, t)
        hm = nxt and time.strftime("%H:%M", time.localtime(nxt))
        out.update(open=inside, say="入池，全天" if nxt is None else f"入池，时段内（{hm} 结束）" if inside else f"入池，{hm} 开始")
        if (m.get("rest") or 0) > t:
            out.update(rest=m["rest"], say=f"入池，休息到 {fmt_time(m['rest'], date=False)}（{m.get('rest_why') or '刚还'}）")
    return out


def my_servers(who: Ident, t: float | None = None) -> list[dict]:
    """「我的账号 → 我的服务器」（/api/me 的 servers）：这个账号对应的人持有的组内服务器与各台的入池（lend_view）。认不出人、没有组内服务器为 []。"""
    try:
        cfg = F.conf()
    except ConfigError:
        return []
    t = time.time() if t is None else t
    return [{**{k: s.get(k) for k in ("id", "holder", "label", "pool")}, "lend": lend_view(s, t)} for s in _servers(cfg, _person(who, cfg))]


def locate(root: str, jobs: str, snap: dict | None = None) -> str:
    """一条池清单此刻登记在哪（labmon worker rm pool 用，实验会话不用知道它被放到了哪台）：全局快照 regs 里池里（pool）与借给算力池的主机
    （主机项的 borrow.pool，C 之后才有）上登记着它的那台；都没有就是 pool（worker 回“没登记过”）。"""
    snap = snapshot() if snap is None else snap
    lent = {str(h.get("id")) for h in snap.get("hosts") or [] if (h.get("borrow") or {}).get("pool")}
    same = lambda q: posixpath.normpath(str(q.get("root") or "")) == posixpath.normpath(root) and q.get("jobs") == jobs   # noqa: E731
    for x in snap.get("regs") or []:
        if (x.get("host") == POOL or x.get("host") in lent) and any(same(q) for q in x.get("queues") or []):
            return x["host"]
    return POOL


# ---------- C：自动借还与收回（#25） ----------
def key(root: str, jobs: str) -> str:
    """一条清单在池里的键「目录|清单」（目录规整过）：worker 状态的 pool_heads、全局快照的 pool.lists、项目车道（collect）同一个写法。"""
    return f"{posixpath.normpath(str(root or ''))}|{jobs}"


def lent_ids(hosts: list[dict] | None = None) -> set[str]:
    """此刻借给算力池的主机 id（主机项的 borrow.pool，没还的）：labmon worker add、页面上的「换到其他主机」都不许直接往上加（交给池）。"""
    return {str(h.get("id")) for h in (user_hosts() if hosts is None else hosts) if (h.get("borrow") or {}).get("pool") and not h.get("returned")}


def _owner_ok(pid: str, cfg: dict) -> bool:
    """项目主人是不是池成员（member）：主人按 users.owner_of 认；没有主人的（归管理员，含主人账号已删的）算。"""
    name = U.owner_of(pid)
    if not name:
        return True
    acc = U.account(name)
    return bool(acc) and member(Ident(acc["name"], acc.get("role") or "user", frozenset(), "token", ""), cfg)


def _mem(sid: str, **kw) -> None:
    """改 borrow.json 里这台的 pool 子项（借用的记忆：休息期、退避、nofit 等；与 borrow.check 的合并互不覆盖，同一把锁）。值为 None 的删掉。"""
    def f(st):
        m = st.setdefault(sid, {"warned": []}).setdefault("pool", {})
        m.update(kw)
        for k in [k for k, v in m.items() if v is None]:
            del m[k]
    B._update(f)


def _event(sid: str, holder: str | None, msg: str, t: float, **kw) -> None:
    log_events([{"kind": "pool", "host": sid, "who": holder, "msg": msg, **kw}], t)


def fit_head(item: dict, hosts: list[str]) -> dict | None:
    """问一次 worker：这条清单的队首（还会跑的第一行）要多少、在 hosts 上放不放得下（labmon worker fit --list --json，在池的代办主机上一条 ssh）。
    返回 {head, need {ngpu, vram 每卡 GiB, ram GiB, cpus}, dur 分钟, hosts {主机: {ok, why}}}；问不成为 None。"""
    args = [item["root"], item["jobs"], "--list", "--json"] + (["--hosts", ",".join(hosts)] if hosts else [])
    rc, out = run_out("fit", POOL, args, project=item["project"])
    line = next((x for x in reversed(out.strip().splitlines()) if x.startswith("{")), None)
    try:
        return json.loads(line) if line else None
    except ValueError:
        return None


def _heads(snap: dict) -> tuple[dict, bool]:
    """池清单各自的队首（worker 状态的 pool_heads：同一 worker_home 下各台都算一份，取最新的那份）与有没有一份新鲜的状态。"""
    best = None
    for e in snap.get("hosts") or []:
        st = e.get("status") or {}
        if e.get("worker_up") and "pool_heads" in st and (best is None or (st.get("t") or 0) > (best.get("t") or 0)):
            best = st
    return ((best or {}).get("pool_heads") or {}), best is not None


def _share(snap: dict) -> dict:
    """各项目的用量（衰减后的卡·时）：同一 worker_home 下各台状态里的 share.use 一样（都读全部用量记录），取最新的那份。"""
    sts = [e.get("status") or {} for e in snap.get("hosts") or [] if e.get("worker_up")]
    st = max(sts, key=lambda x: x.get("t") or 0, default={})
    return (st.get("share") or {}).get("use") or {}


def _todo(ws: dict, root: str, jobs: str) -> int | None:
    """worker 状态 todo 里这条清单还会跑、此刻没在跑的行数；状态里没有这条为 None。"""
    k = key(root, jobs)
    run = {j.get("name") for j in ws.get("running") or [] if key(j.get("root"), j.get("queue")) == k}
    for ln in ws.get("todo") or []:
        if key(ln.get("root"), ln.get("queue")) == k:
            return sum(1 for n in ln.get("jobs") or [] if n not in run)
    return None


def _returns(lent: list[dict], srv: dict, fst: dict, bst: dict, t: float, ret, fin) -> tuple[set, list[str]]:
    """池借着的各台该不该还：R1 主人回来（guest.owner）、R3 关了入池或出了时段、R4 连着 POOL_IDLE_S 没有池任务在跑、R5 换了容器
    （R2 约定时间与租期截止由 borrow.check 照旧管）。R1–R4 走 request_return（在跑的跑完就还），R5 直接 finish(gone=True)。
    返回 (要还了的主机, 换了容器的主机)。"""
    going, gone = set(), []
    for e in lent:
        sid = e["id"]
        s, b, st = srv[sid], e.get("borrow") or {}, fst.get(sid) or {}
        m = (bst.get(sid) or {}).get("pool") or {}
        ws = (e.get("status") or {}) if e.get("worker_up") else {}
        if m.get("started") and st.get("started") and st["started"] != m["started"]:      # R5：主人续租或重启了，旧容器里的 worker 与任务已没了
            e["borrow"] = dict(b, gone=True)                  # overview.relaunch 不在主人的新容器上拉起 worker
            gone.append(sid)
            try:
                _event(sid, s["holder"], (fin or B.finish)(sid, gone=True), t, act="gone")
            except ConfigError as x:
                log(f"算力池：{sid} 换了容器，还没还成：{x}")
            continue
        run = [j for j in ws.get("running") or [] if not j.get("hold")]
        if run:
            _mem(sid, busy=round(t))
        if b.get("returning"):
            going.add(sid)
            continue
        owner = (ws.get("guest") or {}).get("owner")
        last = t if run else max(m.get("busy") or 0, b.get("since") or 0, m.get("since") or 0)
        why, rest = None, None
        if owner:
            why, rest = f"{s['holder']} 回来了（{'；'.join(owner[:2])}）", "主人回来过"
        elif not s.get("lend"):
            why, rest = f"{s['holder']} 关了入池", "关了入池"
        elif not F.lend_open(s, t)[0]:
            why = "过了入池时段"
        elif ws and t - last >= POOL_IDLE_S:
            why = f"{fmt_dur(t - last)}没有池任务在跑"
            if not m.get("busy") and m.get("want"):         # 借来以后一个也没跑起来：这个形状一天内不再为它借这台
                _mem(sid, nofit=[x for x in m.get("nofit") or [] if x[2] > t] + [[m["want"]["ngpu"], m["want"]["vram"], round(t + POOL_NOFIT_S)]])
        if not why:
            continue
        try:
            (ret or B.request_return)(sid, False, None)
        except ConfigError as x:
            log(f"算力池：{sid} 要还（{why}）没成：{x}")
            continue
        going.add(sid)
        e["borrow"] = dict(b, returning=round(t))
        if rest:
            _mem(sid, rest=round(t + POOL_REST_S), rest_why=rest)
        _event(sid, s["holder"], f"算力池要还 {s['holder']}（{sid}）：{why}；不再放新的池任务，在跑的跑完就还", t, act="return", why=why)
    return going, gone


def want_borrow(need: dict, cfg: dict, fst: dict, bst: dict, t: float, busy=()) -> tuple[str | None, float | None, str]:
    """挑一台入池的服务器来借（D）：入池、在时段内、没借着、没关机、没在做平台操作、不在休息期与退避里；最近一次探测新鲜，
    GPU 上除保活外没有进程、can_borrow（VS Code、终端这类轻用借来后给主人留预留）；主人最近一次有实验已过 guard_idle_min；
    截止（时段结束与租期前 MARGIN_S 取早）离现在至少 POOL_MIN_LEFT_S；卡数与单卡显存装得下 need；没在 nofit 里。
    多台时卡数差最小的、单卡显存差最小的、空闲最久的。返回 (服务器 id, 时段结束（全天为 None）, 没挑到时各台为什么不行)。"""
    off, out, miss = F.off_servers(), [], {}
    ng, vr = need.get("ngpu") or 0, need.get("vram") or 0
    for s in cfg.get("servers", []):
        sid = s["id"]
        if not s.get("lend"):
            continue
        inside, nxt = F.lend_open(s, t)
        st, m = fst.get(sid) or {}, ((bst.get(sid) or {}).get("pool") or {})
        a = st.get("sample") or {}
        gs = a.get("gpus") or []
        card = min((g.get("total") or 0 for g in gs), default=0) / 1024
        lease = F.lease_end(st, cfg)
        cut = min([x for x in (nxt, lease - B.MARGIN_S if lease else None) if x], default=None)
        no = ("不在入池时段" if not inside else "借着" if F.borrowed(s) else "关机了" if sid in off else "在做平台操作" if sid in busy
              else "休息中" if (m.get("rest") or 0) > t else "刚借不成" if t - (m.get("fail") or 0) < POOL_FAIL_S
              else "没探到" if not a or st.get("down") or st.get("error") or t - (st.get("last") or 0) > 2 * cfg["interval"]
              else "GPU 上有主人的进程" if F._brief_work(a) or not B.can_borrow(a)
              else "主人刚用过" if st.get("last_job") and t - st["last_job"] < cfg["guard_idle_min"] * 60
              else "离截止不到 %d 小时" % (POOL_MIN_LEFT_S // 3600) if cut and cut - t < POOL_MIN_LEFT_S
              else "卡不够" if len(gs) < ng or (ng and card < vr + 1)
              else "借过放不下" if any(x[2] > t and x[0] <= ng and x[1] <= vr + 0.5 for x in m.get("nofit") or [])
              else None)
        if no:
            miss[no] = miss.get(no, 0) + 1
            continue
        out.append((len(gs) - ng, card - vr, st.get("idle_since") or t, sid, nxt))
    if not out:
        return None, None, "、".join(f"{k} {v} 台" for k, v in miss.items()) or "没有入池的服务器"
    best = min(out)
    return best[3], best[4], ""


def _failed(sid: str, why: str) -> None:
    """借不成（begin 失败，fleetops.run_borrow 调）：这台 POOL_FAIL_S 内不再借，记一条，每台每天通知管理员一次。"""
    t = time.time()
    m = (load_json(state_dir() / "borrow.json", {}).get(sid) or {}).get("pool") or {}
    day = time.strftime("%Y-%m-%d", time.localtime(t))
    _mem(sid, fail=round(t), fail_day=day)
    msg = f"算力池没借成 {sid}：{why}；{POOL_FAIL_S // 3600} 小时内不再借它"
    _event(sid, None, msg, t, act="fail")
    if m.get("fail_day") != day:
        notify([msg])


def _move(it: dict, to: str, move, t: float) -> bool:
    """把一条清单搬到 to（池主机，或 POOL 回池里待放），记一条 pool 事件；没搬成返回 False、原因写进 it["why"]。"""
    try:
        move(to, [{"root": it["root"], "jobs": it["jobs"], "project": it["project"], "host": it["host"], "stop": it.get("stop")}], pool=it.get("reason") or "")
    except ConfigError as x:
        it["why"] = f"没搬成：{x}"
        log(f"算力池：{it['key']} 没搬到 {to}：{x}")
        return False
    _event(it["host"] if to == POOL else to, None, f"算力池把 {it['project']} 的 {it['jobs']}" + (f" 从 {it['host']}" if it["host"] != POOL else "")
           + (" 放回池里待放" if to == POOL else f" 放到 {to}") + (f"（{it['reason']}）" if it.get("reason") else ""), t,
           act="place", project=it["project"])
    return True


def _place(items: list[dict], cands: list[dict], regs: dict, t: float, move, fit, budget: list) -> list[dict]:
    """把放不下的清单放到候选的池主机上（fit 判；每台每轮一条，免得读到旧的 room 放多了）；要还的主机上放不下的回池里（back）。
    budget[0]：这一轮还能问几次 fit。返回仍放不下的（带 need 与原因 why）。"""
    left, used = [], set()
    for it in items:
        hs = [e["id"] for e in cands if e["id"] != it["host"] and e["id"] not in used]
        ck = (it["key"], tuple(hs))
        hit, asked = _FIT.get(ck), True
        if hit and t - hit[0] < POOL_RETRY_S:
            res = hit[1]
        elif budget[0] > 0:
            budget[0] -= 1
            try:
                res = fit(it, hs)
            except ConfigError as x:
                res = None
                log(f"算力池：问 {it['key']} 放不放得下没成：{x}")
            _FIT[ck] = (t, res)
        else:
            res, asked = None, False
        oks = (res or {}).get("hosts") or {}
        ok = [h for h in hs if (oks.get(h) or {}).get("ok")]
        mine = [h for h in ok if any(q.get("project") == it["project"] for q in (regs.get(h) or {}).get("queues") or [])]
        to = (mine or ok or [POOL if it.get("back") else None])[0]
        it["need"] = (res or {}).get("need")
        if to is None:
            whys = [f"{h}：{(oks.get(h) or {}).get('why') or '放不下'}" for h in hs]
            it["why"] = ("这一轮问得够多了，下一轮接着看" if not asked else
                         "没问到 worker 这条清单要多少资源" if res is None else
                         "池里借着的服务器都放不下（" + "；".join(whys[:2]) + "）" if whys else "池里没有借着、空得出来的服务器")
            left.append(it)
            continue
        if _move(it, to, move, t):
            _FIT.pop(ck, None)
            if to != POOL:
                used.add(to)
        else:
            left.append(it)
    return left


def _item(host: str, q: dict, t: float, **kw) -> dict:
    return {"host": host, "root": q["root"], "jobs": q["jobs"], "project": q.get("project"), "stop": q.get("stop"),
            "key": key(q["root"], q["jobs"]), "since": t, **kw}


def tick(snap: dict, sw: dict | None = None, t: float | None = None, *, move, fit=None, start=None, ret=None, fin=None) -> dict:
    """工作台每轮（overview.collect 在 borrow.check 之后、快照写盘之前调；只在持有服务租约时）：还 → 放 → 借，见模块说明。
    sw：这一轮下发的调度设置（config.sched 加 owner）：排先后用 fair_key（与 worker 同一份，人的用量取 worker 状态的 share）。
    move(to, items, pool=原因)：serve.reg_move。fit、start、ret、fin 是 fit_head、fleetops.start、borrow.request_return、borrow.finish（测试换掉）。
    返回全局快照的 pool：{lists {键: {project, root, jobs, host, since 待放从何时起, why 为什么还没放}}, gone [换了容器的主机]}。"""
    if not LS.primary():
        return {}
    t = time.time() if t is None else t
    try:
        cfg = F.conf() or {}
    except ConfigError:
        return {}
    if not cfg:
        return {}
    fst, bst = F._load_state(), load_json(state_dir() / "borrow.json", {})
    srv = {s["id"]: s for s in cfg.get("servers", [])}
    lent = [e for e in snap.get("hosts") or [] if (e.get("borrow") or {}).get("pool") and e["id"] in srv]
    regs = {x["host"]: x for x in snap.get("regs") or []}
    going, gone = _returns(lent, srv, fst, bst, t, ret, fin)
    for x in regs.values():        # 还掉的池主机目录里还剩的登记（借着时没搬成的）与换了容器的：直接回池里待放
        s = srv.get(x["host"])
        if x["host"] in gone or (s and s.get("borrow_pool") and not F.borrowed(s) and x.get("state") == "returned"):
            for q in x.get("queues") or []:
                if q.get("stop") not in (None, "boundary"):     # 池放过去的都带停法；别的（借给池之前就留下的）池不收，留给会话自己搬
                    _move(_item(x["host"], q, t, reason=f"{x['host']} 已还"), POOL, move, t)
    heads, fresh = _heads(snap)
    paused = set((sw or {}).get("pause") or {})
    lists, pending = {}, []
    for q in (regs.get(POOL) or {}).get("queues") or []:
        it = _item(POOL, q, q.get("added_t") or t)
        pid, hd = it["project"], heads.get(it["key"])
        it["why"] = ("还没有在跑的 worker 读到池里的清单" if not fresh else "清单里没有还要跑的行" if not hd
                     else f"队首在等依赖：{hd['dep'][1]}" if hd.get("dep") else "清单暂停中" if hd.get("held") else "项目暂停中" if pid in paused
                     else "项目主人不是算力池成员（没有开了入池的服务器）" if not _owner_ok(pid, cfg) else None)
        lists[it["key"]] = {k: it[k] for k in ("project", "root", "jobs", "host", "since")}
        if it["why"] is None:
            pending.append(it)
        else:
            lists[it["key"]]["why"] = it["why"]
    for e in lent:
        if e["id"] in gone:
            continue
        ws = (e.get("status") or {}) if e.get("worker_up") else {}
        for q in (regs.get(e["id"]) or {}).get("queues") or []:
            it = _item(e["id"], q, t)
            if e["id"] in going:
                if not ws or _todo(ws, q["root"], q["jobs"]):    # 只剩在跑的那一格（或都跑完了）的不用搬：跑完随主机一起还
                    pending.append(dict(it, back=True, reason=f"{e['id']} 要还了"))
                continue
            w = next((w for w in ws.get("waiting") or [] if w.get("queue") and key(w.get("root"), w["queue"]) == it["key"]), None)
            if w and not w.get("held") and not w.get("dep") and t - (w.get("since") or t) >= POOL_WAIT_S:
                pending.append(dict(it, reason=f"在 {e['id']} 上等了 {fmt_dur(t - w['since'])}还没放行"))
    focus = set((sw or {}).get("focus") or {})
    pol = {"w": {str(k): float(v) for k, v in ((sw or {}).get("weight") or {}).items()}, "own": (sw or {}).get("owner") or {}}
    use = _share(snap)
    tot = person_use(pol, use)
    pending.sort(key=lambda it: (not it.get("back"), fair_key({"project": it["project"], "since": it["since"]}, focus, pol, use, tot)))
    cands = [e for e in lent if e["id"] not in going and e["id"] not in gone and e.get("worker_up")
             and not ((e.get("status") or {}).get("guest") or {}).get("owner")]
    cands.sort(key=lambda e: -sum((((e.get("status") or {}).get("room") or {}).get("gpus") or {}).values()))
    left = _place(pending, cands, regs, t, move, fit or fit_head, [POOL_FIT_MAX])
    busy, asked = FO.busy(), False
    for it in left:                                   # 还放不下的：挑一台来借（一轮最多借一台；平台操作格忙着就等下一轮）
        lv = lists.setdefault(it["key"], {k: it[k] for k in ("project", "root", "jobs", "host", "since")})
        lv["why"] = it.get("why")
        if asked or busy or not it.get("need"):
            continue
        sid, until, why = want_borrow(it["need"], cfg, fst, bst, t, busy)
        if not sid:
            lv["why"] = f"{lv['why']}；没有能借的入池服务器（{why}）"
            continue
        asked = True
        _mem(sid, want={"ngpu": it["need"].get("ngpu") or 0, "vram": it["need"].get("vram") or 0})
        try:
            (start or FO.start)("borrow", cfg, None, ids=[sid], until=until, pool=True, failed=_failed)
        except Exception as x:  # noqa: BLE001 — 平台操作格刚被占上等：这一轮不借，下一轮再看
            lv["why"] = f"{lv['why']}；借 {sid} 没开始：{x}"
            continue
        lv["why"] = f"正在借 {srv[sid]['holder']} 的 {sid}"
        _event(sid, srv[sid]["holder"], f"算力池开始借 {srv[sid]['holder']}（{sid}）给 {it['project']} 的 {it['jobs']}"
               + (f"，到 {fmt_time(until, date=False)}" if until else ""), t, act="borrow", project=it["project"])
    return {"lists": lists, "gone": gone}


def reclaim(sid: str, who: Ident, ops: bool, move, snap: dict | None = None) -> str:
    """收回（POST /api/pool/reclaim）：持有人本人，或能做平台操作的管理员（ops）。按各清单的停法停下池任务（request_return now：停下的重新排队、不记失败），
    这台上的清单登记回池里待放（下一轮放到别处）；这台休息 POOL_REST_S。返回给人看的一句。"""
    cfg = F.conf() or {}
    s = next((x for x in cfg.get("servers", []) if x["id"] == sid), None)
    if s is None:
        raise Denied(404, f"组内服务器里没有 {sid}")
    mine = s["holder"] == _person(who, cfg)
    if not mine and who.role != "admin":
        raise Denied(403, f"{sid} 不是你的服务器：只有持有人本人或管理员能收回")
    if not mine and not ops:
        raise Denied(403, "代别人收回要能做平台操作：从内网入口来，或开启公网平台操作")
    if not (F.borrowed(s) and s.get("borrow_pool")):
        raise Denied(409, f"{sid} 没借给算力池")
    snap = snapshot() if snap is None else snap
    e = next((x for x in snap.get("hosts") or [] if x["id"] == sid), {})
    msg = B.request_return(sid, True, (e.get("status") or {}).get("running"))
    t = time.time()
    _mem(sid, rest=round(t + POOL_REST_S), rest_why="收回过")
    qs = next((x.get("queues") or [] for x in snap.get("regs") or [] if x["host"] == sid), [])
    its = [_item(sid, q, t, reason=f"{s['holder']} 收回了 {sid}") for q in qs]
    moved = sum(_move(it, POOL, move, t) for it in its)
    say = (f"收回 {s['holder']}（{sid}）" + ("" if mine else f"（{who.name} 代收）") + f"：{msg}；{moved} 条清单回池里，下一轮放到别处"
           + (f"；{len(its) - moved} 条没搬成，下一轮再搬" if moved < len(its) else ""))
    _event(sid, s["holder"], say, t, act="reclaim")
    return say
