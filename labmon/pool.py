"""算力池（#13；B：池清单与入池开关，#24；C：自动借还与收回，#25）。池按借用的推广来做，复用借用、worker 调度与清单登记，不另写一套：
- 池清单：实验会话 labmon worker add pool ROOT JOBS --project P 交上来，登记落在共享盘 worker_home 的 hosts/pool/queues/
  （pool 是保留的伪主机 id，config.POOL；worker_home 取 config.pool_home），沿用“一条清单只归一台”。待放的清单经现有的代读
  进全局快照的 regs（state 为 pool）与项目车道（collect），别处 after= 等它由 worker 的 peers 认。
- 入池开关：[[fleet.servers]] 的 lend、lend_hours（fleet.conf 校验，fleet.lend_open 判时段），持有人本人在「我的账号 → 我的服务器」开，
  管理员在组内服务器抽屉代开（要能做平台操作、勾「已和这人说好」）。换人时 config.set_fleet_holder 一并去掉。
- 成员：管理员，或账号对应的人（users.person_of）有一台开了入池的组内服务器（member）。users.may_host 对 pool 问它。
- 自动借还（tick，工作台每轮、只在持有服务租约时，overview.collect 里快照写盘前）：
  还（_returns）：池借着的主机（主机项 borrow.pool）主人回来（worker 状态的 guest.owner）、关了入池或出了时段、POOL_IDLE_S 没有池任务在跑
  （从借来、最近有池任务在跑、最近往上放清单里取晚的起算）就 borrow.request_return（在跑的跑完就还；时段结束、租期截止由 borrow.check 的约定时间照旧管）；换了容器（组内探测的 started 与借来时记的不同）
  直接 borrow.finish(gone=True)。放（_place）：池里待放的、要还的池主机上的、在池主机上队首因资源等满 POOL_WAIT_S 的池清单（_ours，会话自己的不动，#130），按 fair_key 排先后，
  经 fit --list --json 判各台放不放得下，serve.reg_move 放过去（要还的主机上放不下的回池里待放）；
  交给池之前在别的主机放行的那一格还没收尾的（worker 的 elsewhere）不放，已在池主机上的放回池里，等它跑完（#80）。借（want_borrow）：还有放不下的、
  平台操作格空着时，挑一台入池、在时段内、GPU 空着或只有保活、主人空闲够久的，fleetops.start("borrow", pool=True)。
- 收回（reclaim）：持有人或能做平台操作的管理员点，request_return(now=True) 按停法停下池任务、清单回池里重排。
持久的只有主机清单里的 lend、lend_hours、borrow_pool 与 borrow.json 各台的 pool 子项（started、since、busy、placed、rest、fail、nofit、want）；
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
from .config import HOSTS_LOCK, POOL, POOL_LABEL, ConfigError, active_hosts, fmt_dur, fmt_time, home_host, home_peers, pool_home, set_fleet_keys, share_host, user_hosts
from .worker import fair_key
from .worker.pure import person_use
from .records import log_events
from .service import log, notify
from .state import load_json, snapshot, state_dir
from .users import Denied, Ident, person_of
from .workerctl import override_each, run_out

POOL_IDLE_S = 15 * 60          # 借着的池服务器连着这么久没有池任务在跑（不算占位）就还（R4）
POOL_WAIT_S = 10 * 60          # 放过去的清单队首因资源等满这么久：当放不下，挪到别处（fit 判得下、worker 准入时放不下）
POOL_REST_S = 3 * 3600         # 主人回来、关了入池、收回之后这么久不再借这台（免得吃个饭回来又被借走）
POOL_MIN_LEFT_S = 2 * 3600     # 截止（时段结束与租期前 1 小时取早）离现在不到这么久的不借
POOL_FAIL_S = 3600             # 借不成（begin 失败）之后这么久不再借这台
POOL_NOFIT_S = 86400           # 借来后哪条都没跑起来、按 R4 还掉的：这个形状（卡数、单卡显存）这么久不再为它借这台
POOL_DONE = "清单里的行都跑完了：不用了就 labmon worker rm pool 撤掉，往清单里加了行会接着放"   # 池里跑完的清单那一句（#86；pool.lists 带 done）
POOL_FIT_MAX = 4               # 每轮最多问几次 fit（每次一条 ssh，走长连接）
POOL_RETRY_S = 5 * 60          # 同一（清单, 候选主机）这么久内不重判
POOL_TELL_S = 2 * 3600         # 池里待放满这么久，给实验会话报 pool_wait:（collect：一条清单一条，放不下也借不到时另报一次，#130）
_FIT: dict = {}                # (清单, 候选主机) → (时刻, fit 的结果)：只在内存里，重启后重判


def _person(who: Ident, cfg: dict | None) -> str | None:
    """这个账号在组内人员表里是谁（users.person_of；没有 [fleet]、认不出为 None）。"""
    return person_of(who.name)[0] if cfg else None


def _selfs(cfg: dict | None, hosts: list[dict] | None = None) -> list[dict]:
    """自用主机当成池的一台（#79）：在用的 [[hosts]] 里不是借来、换来的（没有 borrow），补上持有人 holder（fleet.host_holder，可为 None：只有管理员能开）
    与 mine。能不能入池另由 _self_bar 判。hosts 缺省 active_hosts。"""
    return [{**h, "holder": F.host_holder(h, cfg), "mine": True} for h in (active_hosts() if hosts is None else hosts) if not h.get("borrow")]


def _self_bar(h: dict) -> str | None:
    """这台自用主机为什么不能入池（#79）；能入池为 None。看板本机与回环不行（池里是组里别人的清单，跑在这台的系统账号下，同 config.share_host）；
    worker 目录与池的不同也不行（池的登记与清单在 config.pool_home 那块共享盘上，搬过去读不到）。"""
    if not share_host(h):
        return f"{h['id']} 是看板本机或回环：池里是组里别人的清单，不放到这台"
    home = pool_home()
    if not home or h["worker_home"].rstrip("/") != home.rstrip("/"):
        return f"{h['id']} 的 worker 目录与算力池的（{home or '没有'}）不同：池里的清单在那块盘上，这台读不到"
    return None


def _units(cfg: dict | None) -> list[dict]:
    """能入池的各台：组内服务器（经借用放）加自用主机（_selfs，直接放，#79）。入池、成员、我的服务器、收回都在这里找这一台。"""
    return list((cfg or {}).get("servers", [])) + _selfs(cfg)


def _unit(cfg: dict | None, sid: str) -> dict:
    """按 id 找 _units 里的一台，没有抛 Denied(404)。"""
    s = next((x for x in _units(cfg) if x["id"] == sid), None)
    if s is None:
        raise Denied(404, f"组内服务器与自用主机里都没有 {sid}")
    return s


def _servers(cfg: dict | None, person: str | None) -> list[dict]:
    """这人持有的各台（组内服务器与自用主机）。"""
    return [s for s in _units(cfg) if person and s["holder"] == person]


def member(who: Ident, cfg: dict | None = None) -> bool:
    """能不能往池里交清单（worker add pool，users.may_host 的 pool）：管理员；或这人持有的组内服务器、自用主机（#79，要能入池：_self_bar）里有一台开了入池
    （lend，不看此刻在不在时段）。"""
    if who.role == "admin":
        return True
    try:
        cfg = F.conf() if cfg is None else cfg
    except ConfigError:
        return False
    return any(s.get("lend") and not (s.get("mine") and _self_bar(s)) for s in _servers(cfg, _person(who, cfg)))


def need_holder(who: Ident, sid: str, cfg: dict | None = None) -> dict:
    """要改 sid 这台（组内服务器或自用主机）的入池：是它的持有人本人（账号对应的人员是 holder）返回这台，否则抛 Denied(403)。管理员代开另由 set_lend 核。"""
    cfg = F.conf() if cfg is None else cfg
    s = _unit(cfg, sid)
    if not s["holder"] or s["holder"] != _person(who, cfg):
        raise Denied(403, f"{sid} 不是你的服务器：只有持有人本人能开入池，管理员能代开")
    return s


def set_lend(sid: str, on: bool, hours: str | None, who: Ident, ops: bool, ok: bool) -> str:
    """POST /api/pool/lend：开、关这台的入池，改时段（None 或空是全天）。持有人本人直接改；管理员代别人开要能做平台操作（ops）且勾了
    「已和这人说好」（ok，关掉不用勾）。写进主机清单这台的 [[fleet.servers]]（set_fleet_keys，记一条配置改动）。返回给人看的一句。"""
    with HOSTS_LOCK:                 # 读配置、核持有人到写入同一把锁（与换人 set_fleet_holder 互斥：旧持有人的请求不会把 lend 写回新持有人名下）
        cfg = F.conf()
        s = _unit(cfg, sid)
        mine = bool(s["holder"]) and s["holder"] == _person(who, cfg) or not s["holder"] and who.role == "admin"   # 没有持有人的自用主机：管理员就是主人
        if on and s.get("mine") and (bar := _self_bar(s)):
            raise Denied(409, bar)
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
        say = f"{_name(s)}{'入池' + (f'，每天 {hours}' if hours else '，全天') if on else '退出算力池'}{by}"
        set_fleet_keys({"lend": True if on else None, "lend_hours": hours if on else None}, say, **({"host": sid} if s.get("mine") else {"sid": sid}))
    return say


def _name(s: dict) -> str:
    """一台在文字里的叫法：「持有人（id）」，没有持有人的自用主机只写 id。"""
    return f"{s['holder']}（{s['id']}）" if s.get("holder") else s["id"]


def lend_view(s: dict, t: float, bst: dict | None = None, placed: int = 0) -> dict:
    """页面上一台的入池（「我的服务器」、组内服务器与自用主机的卡片、抽屉、主机页同一份）：{on 开没开, hours 时段（None 是全天）, open 此刻在不在时段里,
    lent 正借给算力池, returning 要还了（在跑的跑完就还）, rest 休息到几点（主人回来、关了入池、收回之后不借、不放，epoch 秒；没有为 None）, say 那一句,
    mine 是自用主机（#79：不借、池清单直接放上来）, placed 这台上此刻有几条池放的清单（自用主机才数，_placed）, bar 不能入池的原因（自用主机，_self_bar）,
    holder 持有人（只自用主机带）}。
    bst：borrow.json（不给就读）。"""
    out = {"on": bool(s.get("lend")), "hours": s.get("lend_hours"), "open": False, "lent": False, "returning": False, "rest": None, "say": "没入池",
           "mine": bool(s.get("mine")), "placed": placed, "bar": _self_bar(s) if s.get("mine") else None,
           **({"holder": s.get("holder")} if s.get("mine") else {})}      # 自用主机的主机项上没有持有人：页面的弹窗取这里的
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
        elif out["bar"]:
            out.update(say=f"入池，但不放：{out['bar']}")
    if placed:
        out["say"] += f"；跑着池里 {placed} 条清单" if out["on"] and out["open"] and not out["rest"] and not out["bar"] else f"；池里的 {placed} 条清单正在搬走"
    return out


def _placed(regs: list[dict] | None) -> dict[str, int]:
    """各主机上池放的清单有几条（全局快照 regs 里带 pool 标记的登记，#79：自用主机上与主人自己的清单分开）。"""
    return {x["host"]: n for x in regs or [] if (n := sum(1 for q in x.get("queues") or [] if q.get("pool")))}


def my_servers(who: Ident, t: float | None = None, snap: dict | None = None) -> list[dict]:
    """「我的账号 → 我的服务器」（/api/me 的 servers）：这个账号对应的人持有的组内服务器、自用主机（#79，带 mine）与各台的入池（lend_view）。
    认不出人、两样都没有为 []。snap：全局快照（缺省现读），数各台上池放的清单。"""
    try:
        cfg = F.conf()
    except ConfigError:
        return []
    t = time.time() if t is None else t
    ss = _servers(cfg, _person(who, cfg))
    n = _placed((snapshot() if snap is None else snap).get("regs")) if any(s.get("mine") for s in ss) else {}
    return [{**{k: s.get(k) for k in ("id", "holder", "label")}, "pool": s.get("platform_pool") if s.get("mine") else s.get("pool"), "mine": bool(s.get("mine")),
             "lend": lend_view(s, t, placed=n.get(s["id"], 0) if s.get("mine") else 0)} for s in ss]


def locate(root: str, jobs: str, snap: dict | None = None) -> str:
    """一条池清单此刻登记在哪（labmon worker rm pool 用，实验会话不用知道它被放到了哪台）：全局快照 regs 里池里（pool）、借给算力池的主机
    （主机项的 borrow.pool，C 之后才有）上登记着它的，或别的主机上带池标记登记着它的（入池的自用主机，#79）那台；都没有就是 pool（worker 回“没登记过”）。"""
    snap = snapshot() if snap is None else snap
    lent = {str(h.get("id")) for h in snap.get("hosts") or [] if (h.get("borrow") or {}).get("pool")}
    for x in snap.get("regs") or []:
        if any(U.same_list(q, root, jobs) and (x.get("host") in (POOL, *lent) or q.get("pool")) for q in x.get("queues") or []):
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
    rc, out = run_out("fit", POOL, [item["root"], item["jobs"], "--json"], list=True, hosts=",".join(hosts), project=item["project"])
    line = next((x for x in reversed(out.strip().splitlines()) if x.startswith("{")), None)
    try:
        return json.loads(line) if line else None
    except ValueError:
        return None


def _home_sts(snap: dict) -> list[dict]:
    """池登记所在 worker_home（config.pool_home）下各台在写的 worker 状态：只有它们读得到池目录、用量记录也是这块盘上的；
    别的 worker_home 的 worker 写的 pool_heads 是空的，不能拿来比新旧。"""
    home = pool_home()
    h = home and home_host(home)
    ids = set(home_peers(h["id"])) if h else set()
    return [e.get("status") or {} for e in snap.get("hosts") or [] if e.get("worker_up") and str(e.get("id")) in ids]


def _heads(snap: dict) -> tuple[dict, bool]:
    """池清单各自的队首（worker 状态的 pool_heads：池那个 worker_home 下各台各算一份）与有没有一份新鲜的状态。逐条取：
    读得到清单的（没有 err）优先，再取读得最晚的那台的（状态的 pool_t：那台读池目录的时刻，没有按状态的 t）。以前整份取状态最新的那台：
    它还没读到刚交上来的（peers 每分钟才重读池目录）或读不到清单，别的台读到的也一起丢了，判成没有还要跑的行，池一条不放（#86）。"""
    sts = sorted((st for st in _home_sts(snap) if "pool_heads" in st), key=lambda x: x.get("pool_t") or x.get("t") or 0)
    out: dict = {}
    for st in sts:
        for k, v in (st.get("pool_heads") or {}).items():
            if not isinstance(v, dict) or (v.get("err") and k in out and not out[k].get("err")):
                continue
            out[k] = v
    return out, bool(sts)


def _share(snap: dict) -> dict:
    """各项目的用量（衰减后的卡·时）：池那个 worker_home 下各台状态里的 share.use 一样（都读全部用量记录），取最新的那份。"""
    st = max(_home_sts(snap), key=lambda x: x.get("t") or 0, default={})
    return (st.get("share") or {}).get("use") or {}


def _todo(ws: dict, root: str, jobs: str) -> int | None:
    """worker 状态 todo 里这条清单还会跑、此刻没在跑的行数；状态里没有这条为 None。"""
    k = key(root, jobs)
    run = {j.get("name") for j in ws.get("running") or [] if key(j.get("root"), j.get("queue")) == k}
    for ln in ws.get("todo") or []:
        if key(ln.get("root"), ln.get("queue")) == k:
            return sum(1 for n in ln.get("jobs") or [] if n not in run)
    return None


def _box(st: dict, m: dict) -> str | None:
    """借来时的容器（borrow.json 这台 pool 子项的 started）与组内探测此刻看到的（state.json 的 started）比：same、new（差 60 秒以上，
    同 health.container_step 认新容器的口径）；有一边不知道为 None。"""
    if not (m.get("started") and st.get("started")):
        return None
    if (st.get("last") or 0) <= (m.get("since") or 0):         # 这次借用之前的探测：记的可能是上一个容器（借来时 begin 现场探到的才算数），说不准
        return None
    return "new" if st["started"] - m["started"] > 60 else "same" if abs(st["started"] - m["started"]) <= 60 else None


def relaunch_ok(e: dict, t: float | None = None) -> bool:
    """算力池借着的主机 worker 不在时，看板能不能把它拉起来（overview.relaunch 问）：worker 不在多半是容器换了（主人续租或重启），
    不能在主人的新容器里拉起；只有组内探测在 worker 停写状态之后探过、看到的还是借来时那个容器，才是 worker 自己没了、可以拉起。
    换了容器的由 tick 的 R5 还掉。"""
    sid = e["id"]
    st = F._load_state().get(sid) or {}
    m = (load_json(state_dir() / "borrow.json", {}).get(sid) or {}).get("pool") or {}
    last = ((e.get("status") or {}).get("t")) or 0
    return _box(st, m) == "same" and (st.get("last") or 0) > last and not st.get("error")


def _returns(lent: list[dict], srv: dict, fst: dict, bst: dict, t: float, ret, fin) -> tuple[set, list[str]]:
    """池借着的各台该不该还：R1 主人回来（guest.owner）、R3 关了入池或出了时段、R4 连着 POOL_IDLE_S 没有池任务在跑（从借来、最近一次有池任务在跑、最近一次往上放清单里取晚的起算）、R5 换了容器
    （R2 约定时间与租期截止由 borrow.check 照旧管）。R1–R4 走 request_return（在跑的跑完就还），R5 直接 finish(gone=True)。
    返回 (要还了的主机, 换了容器的主机)。"""
    going, gone = set(), []
    for e in lent:
        sid = e["id"]
        s, b, st = srv[sid], e.get("borrow") or {}, fst.get(sid) or {}
        m = (bst.get(sid) or {}).get("pool") or {}
        ws = (e.get("status") or {}) if e.get("worker_up") else {}
        if _box(st, m) == "new":                           # R5：主人续租或重启了，旧容器里的 worker 与任务已没了
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
        last = t if run else max(m.get("busy") or 0, m.get("placed") or 0, b.get("since") or 0, m.get("since") or 0)   # 刚放上来的清单还没开跑也从放上来起算（#80）
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


def want_borrow(need: dict, cfg: dict, fst: dict, bst: dict, t: float, busy=(), regs: dict | None = None) -> tuple[str | None, float | None, str]:
    """挑一台入池的服务器来借（D）：入池、在时段内、没借着、没关机、没在做平台操作、不在休息期与退避里；最近一次探测新鲜，
    GPU 上除保活外没有进程、can_borrow（VS Code、终端这类轻用借来后给主人留预留）；主人最近一次有实验已过 guard_idle_min；
    截止（时段结束与租期前 MARGIN_S 取早）离现在至少 POOL_MIN_LEFT_S；卡数够、单卡显存装得下 need（清单写的提示值才比；按历史估的超过整卡
    时 worker 按整卡算，同 card_need）；没在 nofit 里；
    这台目录里没有留着别的清单登记（regs：全局快照的 regs 按主机；以前手动借时留下的，池借来一拉起 worker 就会接着跑它们；
    池自己放过去的（_ours，跑完的也算）不挡：还掉时 tick 把它们收回池里，没收成的借回来接着跑也是池的活，#80）。
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
              else "目录里还留着别的清单" if any(not _ours(q) for q in ((regs or {}).get(sid) or {}).get("queues") or [])
              else "休息中" if (m.get("rest") or 0) > t else "刚借不成" if t - (m.get("fail") or 0) < POOL_FAIL_S
              else "没探到" if not a or st.get("down") or st.get("error") or t - (st.get("last") or 0) > 2 * cfg["interval"]
              else "GPU 上有主人的进程" if F._brief_work(a) or not B.can_borrow(a)
              else "主人刚用过" if st.get("last_job") and t - st["last_job"] < cfg["guard_idle_min"] * 60
              else "离截止不到 %d 小时" % (POOL_MIN_LEFT_S // 3600) if cut and cut - t < POOL_MIN_LEFT_S
              else "卡不够" if len(gs) < ng or (ng and need.get("src") == "提示值" and card < vr + 1)   # 按历史估的超过整卡按整卡算（worker 的 card_need），空卡就放
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


def _move(it: dict, to: str, move, t: float, snap: dict | None = None) -> bool:
    """把一条清单搬到 to（池主机，或 POOL 回池里待放），记一条 pool 事件；没搬成返回 False、原因写进 it["why"]。
    snap：这一轮的全局快照（tick 传；reclaim 不在采集那一轮里，不传），搬成了就照搬后的样子改它的 regs（_shift）。"""
    try:
        move(to, [{"root": it["root"], "jobs": it["jobs"], "project": it["project"], "host": it["host"], "stop": it.get("stop")}], pool=it.get("reason") or "")
    except ConfigError as x:
        it["why"] = f"没搬成：{x}"
        log(f"算力池：{it['key']} 没搬到 {to}：{x}")
        return False
    if snap is not None:
        _shift(snap, it, to, t)
    _event(it["host"] if to == POOL else to, None, f"算力池把 {it['project']} 的 {it['jobs']}" + (f" 从 {it['host']}" if it["host"] != POOL else "")
           + (" 放回池里待放" if to == POOL else f" 放到 {to}") + (f"（{it['reason']}）" if it.get("reason") else ""), t,
           act="place", project=it["project"])
    if to != POOL:
        _mem(to, placed=round(t))                         # R4 从放上来起算：队首还没开跑（等依赖、等资源）也不马上还（#80）
    return True


def _shift(snap: dict, it: dict, to: str, t: float) -> None:
    """搬成一条之后，把这一轮全局快照的 regs 改成搬后的样子（#78）：原来那台（或池里）的那一项去掉这条，to 那一项加上（added_t 记看板此刻）。
    regs 是这一轮放、搬之前读的；不改的话这一轮写出的快照里清单还在原处，项目车道（collect.reg_queues）、pool.locate 要到下一轮全局采集才跟过去，
    会话这一轮就收到「放到某台」，项目页、wait --job 却还说它在池里等。搬由 worker add 当场做完（原处的登记它删掉），这里照着改就是现状；
    下一轮 regs 照常从各台状态与登记文件读。池里那一项原来没有时（池里本来是空的）补一项，排在在用主机之后。"""
    k, regs = it["key"], snap.setdefault("regs", [])
    q = {"root": it["root"], "jobs": it["jobs"], "project": it["project"], "stop": it.get("stop")}
    for x in regs:
        if x["host"] == it["host"] and x.get("queues"):
            q = next((y for y in x["queues"] if key(y.get("root"), y.get("jobs")) == k), q)
            x["queues"] = [y for y in x["queues"] if key(y.get("root"), y.get("jobs")) != k]
    dst = next((x for x in regs if x["host"] == to), None)
    if dst is None:
        if to != POOL:                                # 在用主机每轮都有一项；没有（这一轮刚借来的）留给下一轮读
            return
        dst = {"host": POOL, "label": POOL_LABEL, "state": "pool", "returned": None, "home": pool_home(), "stale": False, "t": t, "queues": []}
        regs.insert(next((i for i, x in enumerate(regs) if x.get("state") != "active"), len(regs)), dst)
    q = {kk: v for kk, v in q.items() if kk not in ("pool", "sleeping")}
    if to != POOL:                                    # 池放出去的登记带池标记（#79，同 worker add --pool），回池里的不带
        q["pool"] = True
    dst["queues"] = [y for y in dst.get("queues") or [] if key(y.get("root"), y.get("jobs")) != k] + [dict(q, added_t=round(t))]


def _place(items: list[dict], cands: list[dict], regs: dict, t: float, move, fit, budget: list, snap: dict | None = None) -> list[dict]:
    """把放不下的清单放到候选的池主机上（fit 判；每台每轮一条，免得读到旧的 room 放多了）；要还的主机上放不下的回池里（back）。
    budget[0]：这一轮还能问几次 fit。返回仍放不下的（带 need 与原因 why；问到了 fit、一台都放不下的带 nofit）。"""
    left, used = [], set()
    new = {it["key"] for it in items if not any(k[0] == it["key"] for k in _FIT)}   # 还没问过的清单：先给它们留预算，免得前面放不下的每轮重问把预算吃光
    for it in items:
        hs = [e["id"] for e in cands if e["id"] != it["host"] and e["id"] not in used]
        ck = (it["key"], tuple(hs))
        hit, asked = _FIT.get(ck), True
        new.discard(it["key"])
        if hit and t - hit[0] < POOL_RETRY_S:
            res = hit[1]
        elif budget[0] > 0 and (it["key"] not in {k[0] for k in _FIT} or budget[0] > len(new)):
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
            later = any(e["id"] in used for e in cands if e["id"] != it["host"])   # 有候选只因这一轮已放过一条被跳过：下一轮再看，不算放不下
            it["why"] = ("这一轮问得够多了，下一轮接着看" if not asked else
                         "没问到 worker 这条清单要多少资源" if res is None else
                         "池里借着的服务器都放不下（" + "；".join(whys[:2]) + "）" if whys and not later else
                         "这一轮已往空得出来的服务器各放了一条，下一轮接着放" if later else "池里没有借着、空得出来的服务器")
            it["nofit"] = res is not None and not later   # 问到了（含缓存）、所有候选都放不下；tick 再看借不借得到（#130）
            left.append(it)
            continue
        if _move(it, to, move, t, snap):
            _FIT.pop(ck, None)
            if to != POOL:
                used.add(to)
        else:
            left.append(it)
    return left


def _ours(q: dict) -> bool:
    """这条登记是不是池的：池里的清单都带停法（worker add pool 缺省 term，不许 boundary）；借给池之前就留下的多是 boundary 或没写，池不收、不搬。"""
    return q.get("stop") not in (None, "boundary")


def _elsewhere(x: list) -> str:
    """worker 说这条清单在等别的主机放行的那一格收尾（pool_heads 与等待项的 elsewhere [主机, 任务]，run_elsewhere 认的）：那一句。"""
    return f"{x[1]} 还在 {x[0]} 上跑（交给池之前在那台放行的），跑完再放"


def _item(host: str, q: dict, t: float, **kw) -> dict:
    return {"host": host, "root": q["root"], "jobs": q["jobs"], "project": q.get("project"), "stop": q.get("stop"),
            "key": key(q["root"], q["jobs"]), "since": t, **kw}


def _self_in(h: dict, t: float, bst: dict) -> str | None:
    """入池的自用主机此刻为什么不收池清单（#79）；收为 None：要入池、在时段里、不在休息期（收回过）、能入池（_self_bar）。"""
    if not h.get("lend"):
        return f"{_name(h)} 关了入池"
    if not F.lend_open(h, t)[0]:
        return "过了入池时段"
    rest = (((bst.get(h["id"]) or {}).get("pool") or {}).get("rest") or 0)
    if rest > t:
        return f"{_name(h)} 收回过，{fmt_time(rest, date=False)} 前不放"
    return _self_bar(h)


def _stuck(e: dict, regs: dict, t: float, out: list, move, snap: dict, *, only_pool: bool = False, back: str | None = None) -> None:
    """池主机 e 上的清单该不该挪（借着的池主机与入池的自用主机共用）：
    放过来时别的主机放行的那一格还没收尾（worker 等待项的 elsewhere）：这台一行也开不了，回池里等它跑完再放（#80）；
    back（这台不再收池清单的原因：要还了、自用主机关了入池等）：还要跑的加进 out 当要还的主机上的去放（借着的只剩在跑的那一格或都跑完了的不用搬，
    跑完随主机一起还；自用主机上池放的都搬，免得之后再往清单里加行还在这台跑）；
    否则队首因资源等满 POOL_WAIT_S 还没放行的（不算被临时指令、依赖挡着的）加进 out 去挪。
    只动池的登记：自用主机上认 pool 标记（only_pool，#79），借来的池主机上认 _ours；别的（借给池之前就登记在这台的会话自己的清单）池不收、不挪，
    在原处照旧排，主机还掉以后 returned_unfinished 叫会话自己搬（#88 E、#130：以前要还与等满两条不过 _ours，会话的清单被悄悄搬成池清单）。"""
    ws = (e.get("status") or {}) if e.get("worker_up") else {}
    for q in (regs.get(e["id"]) or {}).get("queues") or []:
        if not (q.get("pool") if only_pool else _ours(q)):
            continue
        it = _item(e["id"], q, t)
        wq = [w for w in ws.get("waiting") or [] if w.get("queue") and key(w.get("root"), w["queue"]) == it["key"]]   # 并行清单窗口里可能有几行（#147）
        w = next((w for w in wq if w.get("elsewhere")), None)
        busy = any(key(j.get("root"), j.get("queue")) == it["key"] for j in ws.get("running") or [])   # 并行清单有行在跑：另一格等着不算放不出去
        long = [w for w in wq if not busy and not w.get("held") and not w.get("dep") and t - (w.get("since") or t) >= POOL_WAIT_S]
        if w:
            _move(dict(it, reason=_elsewhere(w["elsewhere"])), POOL, move, t, snap)
        elif back is not None:
            if only_pool or not ws or _todo(ws, q["root"], q["jobs"]):
                out.append(dict(it, back=True, reason=back))
        elif long:
            out.append(dict(it, reason=f"在 {e['id']} 上等了 {fmt_dur(t - min(w['since'] for w in long))}还没放行"))


def tick(snap: dict, sw: dict | None = None, t: float | None = None, *, move, fit=None, start=None, ret=None, fin=None) -> dict:
    """工作台每轮（overview.collect 在 borrow.check 之后、快照写盘之前调；只在持有服务租约时）：还 → 放 → 借，见模块说明。
    入池的自用主机（#79）：不借不还，此刻在池里（_self_in）、worker 在写状态的当候选与借着的一起放；不在池里了的，这台上池放的清单（登记带 pool）
    当要还的主机上的去放（放不下回池里，在跑的那一格由这台跑完）。各台的入池（lend_view）写进快照的主机项与组内页 fleet.mine 的 lend。
    sw：这一轮下发的调度设置（config.sched 加 owner）：排先后用 fair_key（与 worker 同一份，人的用量取 worker 状态的 share）。
    move(to, items, pool=原因)：serve.reg_move。fit、start、ret、fin 是 fit_head、fleetops.start、borrow.request_return、borrow.finish（测试换掉）。
    返回全局快照的 pool：{lists {键: {project, root, jobs, host, since 待放从何时起, why 为什么还没放, nofit 借着的都放不下、也借不到（只在为真时带）}}, gone [换了容器的主机]}。"""
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
    selfs = {h["id"]: h for h in _selfs(cfg)}
    for x in list(regs.values()):  # 还掉的池主机目录里还剩的登记（借着时没搬成的、跑完了的）与换了容器的、归还了的自用主机上池放的：直接回池里待放
        s = srv.get(x["host"])
        if x["host"] in gone or (s and s.get("borrow_pool") and not F.borrowed(s)):   # 刚还的那一轮 regs 还是还之前读的（active），照收（#80）
            for q in x.get("queues") or []:
                if _ours(q):           # 别的（借给池之前就留下的）池不收，留给会话自己搬；收走的 _shift 当场从 regs 去掉，这一轮后面的放、借看收完的样子
                    _move(_item(x["host"], q, t, reason=f"{x['host']} 已还"), POOL, move, t, snap)
        elif not s and x["host"] not in selfs and x["host"] != POOL and x.get("state") == "returned":
            for q in x.get("queues") or []:
                if q.get("pool"):
                    _move(_item(x["host"], q, t, reason=f"{x['host']} 已归还"), POOL, move, t, snap)
    heads, fresh = _heads(snap)
    paused = set((sw or {}).get("pause") or {})
    lists, pending = {}, []
    for q in (regs.get(POOL) or {}).get("queues") or []:
        it = _item(POOL, q, q.get("added_t") or t)
        pid, hd = it["project"], heads.get(it["key"])
        it["why"] = ("还没有在跑的 worker 读到池里的清单" if not fresh else "还没有在跑的 worker 读到这条清单（交上来后约 1 分钟内读到）" if not hd
                     else hd["err"] if hd.get("err")
                     else _elsewhere(hd["elsewhere"]) if hd.get("elsewhere")
                     else POOL_DONE if not hd.get("head")
                     else f"队首在等依赖：{hd['dep'][1]}" if hd.get("dep") else "清单暂停中" if hd.get("held") else "项目暂停中" if pid in paused
                     else "项目主人不是算力池成员（没有开了入池的服务器）" if not _owner_ok(pid, cfg) else None)
        lists[it["key"]] = {k: it[k] for k in ("project", "root", "jobs", "host", "since")}
        if it["why"] == POOL_DONE:
            lists[it["key"]]["done"] = True             # 跑完了：页面上不算在等（poolCount），收在后面
            if hd and hd.get("sleeping"):
                lists[it["key"]]["sleeping"] = True     # 空满 IDLE_REG_S 已休眠（#164）
        if it["why"] is None:
            pending.append(it)
        else:
            lists[it["key"]]["why"] = it["why"]
    for e in lent:
        if e["id"] not in gone:
            _stuck(e, regs, t, pending, move, snap, back=f"{e['id']} 要还了" if e["id"] in going else None)
    hosts = {e["id"]: e for e in snap.get("hosts") or []}
    n, own_c = _placed(snap.get("regs")), []
    for hid, h in selfs.items():                      # 入池的自用主机（#79）：在池里的当候选，不在了的把池放的清单搬走
        e = hosts.get(hid)
        if e is None:
            continue
        e["lend"] = lend_view(h, t, bst, n.get(hid, 0))
        why = _self_in(h, t, bst)
        if why is None:
            if not e.get("worker_up"):                # worker 不在写状态：不当候选，已放上去的留着
                continue
            own_c.append(e)
        _stuck(e, regs, t, pending, move, snap, only_pool=True, back=why)
    for x in (snap.get("fleet") or {}).get("mine") or []:      # 组内页的自用主机卡片与抽屉：同一份入池
        if x["id"] in hosts and "lend" in hosts[x["id"]]:
            x["lend"] = hosts[x["id"]]["lend"]
    focus = set((sw or {}).get("focus") or {})
    pol = {"w": {str(k): float(v) for k, v in ((sw or {}).get("weight") or {}).items()}, "own": (sw or {}).get("owner") or {}}
    use = _share(snap)
    tot = person_use(pol, use)
    pending.sort(key=lambda it: (not it.get("back"), fair_key({"project": it["project"], "since": it["since"]}, focus, pol, use, tot)))
    cands = [e for e in lent if e["id"] not in going and e["id"] not in gone and e.get("worker_up")
             and not ((e.get("status") or {}).get("guest") or {}).get("owner")] + own_c
    cands.sort(key=lambda e: (-sum((((e.get("status") or {}).get("room") or {}).get("gpus") or {}).values()), e["id"] not in selfs))   # 同样空的自用主机在前（不用借）
    left = _place(pending, cands, regs, t, move, fit or fit_head, [POOL_FIT_MAX], snap)
    busy, asked = FO.busy(), False
    for it in left:                                   # 还放不下的：挑一台来借（一轮最多借一台；平台操作格忙着就等下一轮）
        lv = lists.setdefault(it["key"], {k: it[k] for k in ("project", "root", "jobs", "host", "since")})
        lv["why"] = it.get("why")
        if asked or busy or not it.get("need"):
            continue
        sid, until, why = want_borrow(it["need"], cfg, fst, bst, t, busy, regs)
        if not sid:
            lv["why"] = f"{lv['why']}；没有能借的入池服务器（{why}）"
            if it.get("nofit"):
                lv["nofit"] = True                    # 池里此刻放不下、也借不到：pool_wait 另报一次（collect，#130）
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


def reclaim(sid: str, who: Ident, ops: bool, move, snap: dict | None = None, ovr=None) -> str:
    """收回（POST /api/pool/reclaim）：持有人本人，或能做平台操作的管理员（ops）。按各清单的停法停下池任务（借来的：request_return now，停下的重新排队、不记失败），
    这台上池的清单登记（_ours）回池里待放（下一轮放到别处），借给池之前就登记在这台的会话自己的清单不搬（#130）；这台休息 POOL_REST_S。返回给人看的一句。
    自用主机（#79）不借不还：只把池放的（登记带 pool）搬回池里，再给这些清单在跑的项目在这台下 pause --now --once（ovr，缺省 workerctl.override_each）；
    同一项目在这台还有主人自己的清单的不停（只搬，在跑的跑完）。"""
    cfg = F.conf() or {}
    s = _unit(cfg, sid)
    mine = bool(s["holder"]) and s["holder"] == _person(who, cfg)
    if not mine and who.role != "admin":
        raise Denied(403, f"{sid} 不是你的服务器：只有持有人本人或管理员能收回")
    if not mine and not ops and not (s.get("mine") and not s["holder"]):
        raise Denied(403, "代别人收回要能做平台操作：从内网入口来，或开启公网平台操作")
    snap = snapshot() if snap is None else snap
    e = next((x for x in snap.get("hosts") or [] if x["id"] == sid), {})
    qs = next((x.get("queues") or [] for x in snap.get("regs") or [] if x["host"] == sid), [])
    t = time.time()
    if s.get("mine"):
        pq = [q for q in qs if q.get("pool")]
        if not pq:
            raise Denied(409, f"{sid} 上没有算力池放的清单")
        keys = {key(q["root"], q["jobs"]) for q in pq}
        pin = {q.get("project") for q in qs if not q.get("pool")}       # 这台上主人自己的清单（没有池标记）所属的项目：停了会停到它们
        run = {j.get("project") for j in (e.get("status") or {}).get("running") or [] if key(j.get("root"), j.get("queue")) in keys}
    elif not (F.borrowed(s) and s.get("borrow_pool")):
        raise Denied(409, f"{sid} 没借给算力池")
    else:
        pq, msg = [q for q in qs if _ours(q)], B.request_return(sid, True, (e.get("status") or {}).get("running"))   # 只收池的（#130），别的见下面那句
        if len(pq) < len(qs):
            msg += (f"；{len(qs) - len(pq)} 条借给池之前就登记在这台的清单池不收，留在这台，还掉后会话会收到 returned_unfinished 自己搬")
    _mem(sid, rest=round(t + POOL_REST_S), rest_why="收回过")
    its = [_item(sid, q, t, reason=f"{s['holder'] or '管理员'} 收回了 {sid}") for q in pq]
    moved = sum(_move(it, POOL, move, t) for it in its)
    if s.get("mine"):
        stop = sorted(p for p in run - pin if p)
        bad = [p for p in stop if (ovr or override_each)("add", "pause", p, [sid], now=True, once=True)[0]["rc"]]
        msg = ("在跑的池任务按清单的停法停下（" + "、".join(stop) + "），重新排到别处" if stop else "没有池任务在跑") \
            + (f"；{'、'.join(sorted(run & pin))} 在这台还有自己的清单，在跑的跑完" if run & pin else "") \
            + (f"；{'、'.join(bad)} 没停成，在跑的跑完" if bad else "") + f"，{POOL_REST_S // 3600} 小时内池不往这台放"
    say = (f"收回 {_name(s)}" + ("" if mine else f"（{who.name} 代收）") + f"：{msg}；{moved} 条清单回池里，下一轮放到别处"
           + (f"；{len(its) - moved} 条没搬成，下一轮再搬" if moved < len(its) else ""))
    _event(sid, s["holder"], say, t, act="reclaim")
    return say
