"""算力池（#13；本文件是 B：池清单与入池开关，#24）。池按借用的推广来做，复用借用、worker 调度与清单登记，不另写一套：
- 池清单：实验会话 labmon worker add pool ROOT JOBS --project P 交上来，登记落在共享盘 worker_home 的 hosts/pool/queues/
  （pool 是保留的伪主机 id，config.POOL；worker_home 取 config.pool_home），沿用“一条清单只归一台”。待放的清单经现有的代读
  进全局快照的 regs（state 为 pool）与项目车道（collect），别处 after= 等它由 worker 的 peers 认。放到哪台、搬走由 C（#25）做。
- 入池开关：[[fleet.servers]] 的 lend、lend_hours（fleet.conf 校验，fleet.lend_open 判时段），持有人本人在「我的账号 → 我的服务器」开，
  管理员在组内服务器抽屉代开（要能做平台操作、勾「已和这人说好」）。换人时 config.set_fleet_holder 一并去掉。
- 成员：管理员，或账号对应的人（fleet.account_person）有一台开了入池的组内服务器（member）。users.may_host 对 pool 问它。
不新增状态文件：持久的只有主机清单里的两行。
"""
from __future__ import annotations

import posixpath
import time

from . import fleet as F
from .config import POOL, ConfigError, set_fleet_keys
from .state import snapshot
from .users import Denied, Ident


def _person(who: Ident, cfg: dict | None) -> str | None:
    """这个账号在组内人员表里是谁（fleet.account_person；认不出为 None）。"""
    return F.account_person(cfg, who.name)[0] if cfg else None


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


def lend_view(s: dict, t: float) -> dict:
    """页面上一台的入池（「我的服务器」与组内服务器卡片、抽屉同一份）：{on 开没开, hours 时段（None 是全天）, open 此刻在不在时段里, say 那一句}。
    借着、要还、休息期由 C 补进 say。"""
    out = {"on": bool(s.get("lend")), "hours": s.get("lend_hours"), "open": False, "say": "没入池"}
    if out["on"]:
        inside, nxt = F.lend_open(s, t)
        hm = nxt and time.strftime("%H:%M", time.localtime(nxt))
        out.update(open=inside, say="入池，全天" if nxt is None else f"入池，时段内（{hm} 结束）" if inside else f"入池，{hm} 开始")
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
