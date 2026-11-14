"""命令行那几条命令在看板服务这边的做法（#162）：命令行（__main__）经 client.call 发到这里的接口（serve 的 /api/…），这里做、回 {rc, out, err}；
客户端把 out、err 原样打到标准输出、标准错误，按 rc 退出：输出与退出码与以前在本机做的一样。回 usage 的是参数错（客户端照旧 ap.error，退出码 2）。
写主机清单（set_returned、set_sched_ovr、borrow.*）、ssh 到服务器（workerctl）从此只在看板服务这一个进程里做。
时刻由客户端解析成 epoch 秒传过来（相对写法“明天 9:00”按命令行那台机器的现在算，与以前一样）。
"""
from __future__ import annotations

import json
import time
from contextlib import nullcontext

from . import borrow as B
from . import collect as C
from . import fleet as F
from . import projects as PJ
from . import workerctl as W
from .config import HOSTS_LOCK, ConfigError, active_hosts, fmt_time, hosts_file, set_returned, set_sched_ovr, user_hosts
from .state import snapshot

OVR = {"pause": ("add", "pause"), "resume": ("rm", "pause"), "focus": ("add", "focus"), "unfocus": ("rm", "focus"),
       "drain": ("add", "drain"), "undrain": ("rm", "drain")}
WORKER_ACTS = ("start", "stop", "status", "add", "rm", "limit", "fit")


class Out:
    """一条命令的输出：p 打到标准输出、err 打到标准错误，done 收成回话。"""

    def __init__(self):
        self.o, self.e = [], []

    def p(self, s: str = "") -> None:
        self.o.append(s + "\n")

    def err(self, s: str) -> None:
        self.e.append(s + "\n")

    def done(self, rc: int, **kw) -> dict:
        return {"rc": rc, "out": "".join(self.o), "err": "".join(self.e), **kw}


def usage(msg: str) -> dict:
    """参数错：客户端照旧 ap.error（退出码 2）。"""
    return {"rc": 2, "out": "", "err": "", "usage": msg}


def unknown_project(pid: str, *, require: bool = False) -> str | None:
    """项目 id 不是看板上的（worker 按字符串比，写错了指令静默不生效）：返回说明；旧的调度命令在看板没项目时也能用，登记要求目标存在。"""
    if pid in PJ.archived():
        return f"项目 {pid} 已归档：先 labmon project restore {pid}"
    ids = PJ.project_ids()
    if pid not in ids and (require or ids):
        known = f"（有：{'、'.join(ids)}）" if ids else ""
        return f"看板上没有项目 {pid}{known}：用 monitor.toml 里的 id"
    return None


def hosts() -> dict:
    """GET /api/hosts：用户级主机清单里的主机（labmon host list、labmon borrow 不给参数时列）。没有清单时 missing 是它的路径。"""
    hf = hosts_file()
    keys = ("id", "returned", "ssh", "port", "label", "borrow")
    return {"missing": None if hf.exists() else str(hf), "hosts": [{k: h.get(k) for k in keys} for h in user_hosts()] if hf.exists() else []}


def host(sub: str, target: str, at: float | None = None, now: bool = False) -> dict:
    """labmon host return|restore：标记已归还、恢复在用。归还后各项目的看板不再连接它，下一轮采集自动生效。
    直接借来的组内服务器（borrow.py）：return 是要还（不再放新任务，跑完自动归还），restore 是还掉之前撤销（已经还了的再借用 labmon borrow）。
    at：归还时刻（epoch 秒，客户端解析；缺省现在），清单里写带年份的绝对时刻。"""
    o = Out()
    if sub not in ("return", "restore") or not target:
        return usage("用法：labmon host list | labmon host return <id> [--at 时间 | --now] | labmon host restore <id>（借来的：撤销跑完就还）")
    h = next((x for x in user_hosts() if str(x.get("id")) == target), None)
    try:
        if h and h.get("borrow") and not h["borrow"].get("swap"):
            if sub == "restore":                       # 借来的：撤销跑完就还（已经还了的要再借）
                o.p("[labmon] " + B.cancel_return(target))
                return o.done(0)
            e = next((x for x in snapshot().get("hosts", []) if x["id"] == target), {})
            o.p("[labmon] " + B.request_return(target, now, ((e.get("status") or {}).get("running"))))
            return o.done(0)
        when = fmt_time(at or time.time(), year=True) if sub == "return" else None
        from . import config, pool_supply

        with config.HOSTS_LOCK:
            if pool_supply.held(target):
                raise ConfigError(pool_supply.HELD)
            p = set_returned(target, when)
    except ConfigError as e:
        o.err(f"[labmon] {e}")
        return o.done(3)
    o.p(f"[labmon] {p}：主机 {target} " + (f"标为已归还（{when}），不再连接" if when else "恢复在用"))
    return o.done(0)


def borrow(sid: str, until: float | None, projects: str | None, holder: str | None, away: bool, *, account: str | None = None) -> dict:
    """labmon borrow <id>：组内服务器直接借（拉起 worker），或换给自己借来的自用主机（只记约定时间）。"""
    o = Out()
    try:
        if any(s["id"] == sid for s in (F.conf() or {}).get("servers", [])):
            o.p("[labmon] " + B.begin(sid, until, projects, light=True, away=away, account=account))     # 命令行等于页面上缺省勾着「对方在轻用」
        elif any(str(h.get("id")) == sid and not h.get("returned") for h in user_hosts()):
            if away:
                raise ConfigError(f"{sid} 是自用主机（换给自己借来的）：--away 只用于直接借的组内服务器")
            o.p("[labmon] " + B.set_swap(sid, until, holder))
        else:
            raise ConfigError(f"组内服务器与在用主机里都没有 {sid}")
    except ConfigError as e:
        o.err(f"[labmon] {e}")
        return o.done(3)
    return o.done(0)


def override(verb: str, target: str, host_ids: list[str] | None, until: float | None, now: bool, immediate_hosts: list[str] | None = None) -> dict:
    """labmon pause|resume|focus|unfocus|drain|undrain：项目的不带 --host 记在主机清单（所有主机，含之后加入的，#96），带 --host 的、drain 与 --now
    经 workerctl.override_each 下给那几台。每台一行：worker 打印的“生效中：…”，或 ssh 连不上的原因。"""
    o = Out()
    action, kind = OVR[verb]
    try:
        with HOSTS_LOCK if kind != "drain" else nullcontext():
            if kind == "drain":
                if host_ids:
                    return usage("drain / undrain 的主机写在位置参数上（labmon drain 29），不用 --host")
                hs, tgt = [target], None
            else:
                hs, tgt = host_ids or [h["id"] for h in active_hosts()], target
                if action == "add" and (bad := unknown_project(tgt, require=True)):
                    o.err(f"[labmon] {bad}")
                    return o.done(3)
            if kind != "drain" and not host_ids:      # 项目级设置随 sched.json 发给各主机
                o.p("[labmon] " + set_sched_ovr(kind, tgt, action == "add", until if action == "add" else None))
                if not (now and kind == "pause" and action == "add"):
                    return o.done(0)
                rs = W.override_each("add", "pause", tgt, hs if immediate_hosts is None else immediate_hosts, None, True, once=True)
            else:
                rs = W.override_each(action, kind, tgt, hs, until, now)
    except ConfigError as e:
        o.err(f"[labmon] {e}")
        return o.done(3)
    rc = 0
    for r in rs:
        o.p(f"[{r['host']}] {r['out'].strip() or ('完成' if r['rc'] == 0 else '退出码 %d' % r['rc'])}")
        rc |= r["rc"]
    return o.done(rc)


def worker(sub: str, hid: str, rest: list[str], gpus: str | None = None, project: str | None = None, stop: str | None = None, own: bool = False) -> dict:
    """labmon worker start|stop|status|add|rm|limit|fit：经 workerctl.run_out 一次 ssh；输出是 worker 打印的原文。"""
    if sub not in WORKER_ACTS or not hid:
        return usage(f"不认识的 worker 动作 {sub}")
    try:
        with PJ._reg_locked() if sub == "add" else nullcontext():
            if project and sub != "rm" and (reason := unknown_project(project, require=sub == "add")):
                return usage(reason)
            rc, out = W.run_out(sub, hid, rest, gpus, project=project if sub != "rm" else None, stop=stop,
                                **({"expect_project": project} if own and sub in ("add", "rm") else {}))
    except ConfigError as e:
        return {"rc": 3, "out": "", "err": f"[labmon] {e}\n"}
    return {"rc": rc, "out": out, "err": ""}


def project_init(hid: str, name: str | None) -> dict:
    """labmon project init：按共享盘目录规范建目录，列出可共用的环境、数据集与权重。"""
    try:
        rc, out = W.project_init(hid, name)
    except ConfigError as e:
        return {"rc": 3, "out": "", "err": f"[labmon] {e}\n"}
    return {"rc": rc, "out": out, "err": ""}


def check(pid: str, text: str) -> dict:
    """labmon check：按看板这边的用户级主机清单整套校验项目目录里那份（还没推上来的也行），打出各主机的探针规格（带上全局快照里本项目的登记）；
    after 是同步状态那行（客户端打）之后的几行：不再生效的旧键、wait 还没报过的页面上的改动与归档（列了的 wait 不重报，waits.check_told）。
    校验不过抛 projects.Refused(400)：客户端打“配置错误：…”、退出 3。"""
    from . import waits
    cfg = PJ.check_config(pid, text, {})
    out = PJ.out_dir(pid)
    with PJ._reg_locked():
        told = waits.check_told(cfg, out / ".labmon" / "wait_state.json", remember=PJ.cfg_file(pid).is_file())
    ign = cfg["ignored"]
    cfg = C.lanes_cfg(cfg, snapshot(), out / ".labmon", time.time(), save=False)
    o, after = Out(), []
    for h in cfg["hosts"]:
        o.p(f"# {h['id']}（{h['ssh'] or '本机'}）")
        o.p(json.dumps(C.probe_spec(cfg, h), ensure_ascii=False, indent=2))
    if ign:
        why = ([f"输出目录固定在 {out}"] if "out_dir" in ign else []) + (["项目页「本机」一栏删了"] if "local" in ign else [])
        after.append(f"[labmon] monitor.toml 里的 {'、'.join('[local]' if k == 'local' else k for k in ign)} 不再生效（{'，'.join(why)}），不用改")
    after += [f"[labmon] {m}" for m in told]
    return o.done(0, after=after)
