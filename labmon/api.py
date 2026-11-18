"""命令行那几条命令在看板服务这边的做法（#162）：命令行（__main__）经 client.call 发到这里的接口（serve 的 /api/…），这里做、回 {rc, out, err}；
客户端把 out、err 原样打到标准输出、标准错误，按 rc 退出：输出与退出码与以前在本机做的一样。回 usage 的是参数错（客户端照旧 ap.error，退出码 2）。
写主机清单（set_returned、set_sched_ovr、borrow.*）、ssh 到服务器（workerctl）从此只在看板服务这一个进程里做。
时刻由客户端解析成 epoch 秒传过来（相对写法“明天 9:00”按命令行那台机器的现在算，与以前一样）。
实验会话的只读命令（labmon status、disk、records，#52）也在这里排版：GET /api/project/<id>/…，回 {rc, out, err, data}，--json 打 data。
"""
from __future__ import annotations

import json
import time
from contextlib import nullcontext

from . import borrow as B
from . import collect as C
from . import disk as DK
from . import fleet as F
from . import pool as P
from . import projects as PJ
from . import workerctl as W
from .config import GIB, HOSTS_LOCK, POOL, POOL_LABEL, ConfigError, active_hosts, all_hosts, fmt_dur, fmt_time, home_peers, hosts_file, set_returned, set_sched_ovr, user_hosts
from .records import EVENT_KINDS, project_rows
from .state import load_json, snapshot

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


def borrow_text(b: dict | None) -> str:
    """借来的主机那一句（labmon host list、labmon borrow 列表、labmon status 的主机行共用）。"""
    if not b:
        return ""
    return ("换给自己借来的" if b.get("swap") else f"借 {b['holder']}（{fmt_time(b['since'])} 起）") + (f"，约定 {fmt_time(b['until'])} 还" if b.get("until") else "") \
        + (f"，只给 {'、'.join(b['for'])} 用" if b.get("for") else "")


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
    """GET /api/hosts：用户级主机清单里的主机（labmon host list、labmon borrow 不给参数时列）。没有清单时 missing 是它的路径。
    worker_home 已补缺省值；home_with 是与它共用 worker_home 的别的在用主机（config.home_peers，#35 A；已归还的为 []）。"""
    hf = hosts_file()
    if not hf.exists():
        return {"missing": str(hf), "hosts": []}
    keys = ("id", "returned", "ssh", "port", "label", "borrow")
    act, home = active_hosts(), {str(h["id"]): h["worker_home"] for h in all_hosts()}
    return {"missing": None, "hosts": [{**{k: h.get(k) for k in keys}, "worker_home": home.get(str(h.get("id"))),
                                        "home_with": [x for x in home_peers(str(h.get("id")), act) if x != str(h.get("id"))]}
                                       for h in user_hosts()]}


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


def override(verb: str, target: str, host_ids: list[str] | None, until: float | None, now: bool, immediate_hosts: list[str] | None = None,
             by: str | None = None, check=None) -> dict:
    """labmon pause|resume|focus|unfocus|drain|undrain：项目的不带 --host 记在主机清单（所有主机，含之后加入的，#96），带 --host 的、drain 与 --now
    经 workerctl.override_each 下给那几台。每台一行：worker 打印的“生效中：…”，或 ssh 连不上的原因。
    by：谁下的（项目级暂停记进主机清单，#16）；check()：项目级暂停写之前在主机清单的锁里核能不能动（users.need_pause，不行抛 users.Denied）。"""
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
                if kind == "pause" and check is not None:
                    check()
                o.p("[labmon] " + set_sched_ovr(kind, tgt, action == "add", until if action == "add" else None, by=by))
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


def worker(sub: str, hid: str, rest: list[str], gpus: str | None = None, project: str | None = None, stop: str | None = None, own: bool = False,
           lst: bool = False, may=None) -> dict:
    """labmon worker start|stop|status|add|rm|limit|fit：经 workerctl.run_out 一次 ssh；输出是 worker 打印的原文。
    status 成功时末尾补一行与这台共用 worker 目录的在用主机（home_text，#35 A）；may：普通用户能用哪台（None 不限），只列他能用的。
    hid 是 pool（算力池，#24）时只认 add、rm；rm 在清单此刻登记的地方撤（pool.locate）。
    fit --list（lst）：看与这台共用 worker 目录的在用主机（config.home_peers；may 是普通用户能用哪台，None 不限），在这台上读它们的状态（#35 B）。"""
    if sub not in WORKER_ACTS or not hid:
        return usage(f"不认识的 worker 动作 {sub}")
    if hid == POOL and sub not in ("add", "rm"):         # 算力池（#24）只收清单、撤清单：没有 worker，状态看项目页
        return usage(f"算力池只能 worker add pool、worker rm pool：池里的清单在项目页看（{POOL_LABEL}那一栏），放到哪台由看板定")
    if hid == POOL and sub == "rm" and len(rest) == 2:   # 撤回：清单此刻登记在哪（池里，或已放到借给池的主机上）就在哪撤
        hid = P.locate(rest[0], rest[1])
    try:
        with PJ._reg_locked() if sub == "add" else nullcontext():
            if project and sub != "rm" and (reason := unknown_project(project, require=sub == "add")):
                return usage(reason)
            fl = {"list": True, "hosts": ",".join(h for h in home_peers(hid) if h == hid or may is None or may(h))} if lst and sub == "fit" else {}
            rc, out = W.run_out(sub, hid, rest, gpus, project=project if sub != "rm" else None, stop=stop,
                                **({"expect_project": project} if own and sub in ("add", "rm") else {}), **fl)
    except ConfigError as e:
        return {"rc": 3, "out": "", "err": f"[labmon] {e}\n"}
    extra = home_text(hid, may) if sub == "status" and rc == 0 else ""
    if extra:
        out = out.rstrip("\n") + "\n" + extra
    return {"rc": rc, "out": out, "err": ""}


def home_text(hid: str, may=None) -> str:
    """worker status 末尾那一行：这台与哪几台在用主机共用 worker 目录（同一块共享盘）；只有它自己时为空。may 同 worker。"""
    act = active_hosts()
    ids = [x for x in home_peers(hid, act) if x != hid and (may is None or may(x))]
    if not ids:
        return ""
    home = next(h["worker_home"] for h in act if str(h["id"]) == hid)
    return f"  与 {'、'.join(ids)} 共用 worker 目录 {home}（同一块共享盘：放在这块盘上的文件各台都读得到）\n"


def project_init(hid: str, name: str | None) -> dict:
    """labmon project init：按共享盘目录规范建目录，列出可共用的环境、数据集与权重。"""
    try:
        rc, out = W.project_init(hid, name)
    except ConfigError as e:
        return {"rc": 3, "out": "", "err": f"[labmon] {e}\n"}
    return {"rc": rc, "out": out, "err": ""}


def check(pid: str, text: str, may=None) -> dict:
    """labmon check：按看板这边的用户级主机清单整套校验项目目录里那份（还没推上来的也行），打出各主机的探针规格（带上全局快照里本项目的登记）；
    may(主机 id)：看的人能不能用这台（普通用户经 users.may_host，#16）：不能用的只打一行，不打连接信息与探针规格（主机清单里有没有这台都一样）；None 全打。
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
        if may is not None and not may(h["id"]):
            o.p(f"# {h['id']}（没开给你用：不列连接信息与探针规格，要用找管理员开）")
            continue
        o.p(f"# {h['id']}（{h['ssh'] or '本机'}）")
        o.p(json.dumps(C.probe_spec(cfg, h), ensure_ascii=False, indent=2))
    if ign:
        why = ([f"输出目录固定在 {out}"] if "out_dir" in ign else []) + (["项目页「本机」一栏删了"] if "local" in ign else [])
        after.append(f"[labmon] monitor.toml 里的 {'、'.join('[local]' if k == 'local' else k for k in ign)} 不再生效（{'，'.join(why)}），不用改")
    after += [f"[labmon] {m}" for m in told]
    return o.done(0, after=after)


# ---------- 只读：项目现状、存储、记录（#52：实验会话不看页面，要读的都经命令行） ----------
# GET /api/project/<id>/(status|disk|records)：回 {rc, out, err, data}，命令行原样打出 out；--json 打 data。
def _gib(b: float | None) -> str:
    return "—" if b is None else f"{b / GIB:.1f} GiB"


def _delta(b: float | None) -> str:
    return "" if not b else f"{'+' if b > 0 else '−'}{abs(b) / GIB:.1f} GiB"


def _lane_name(ln: dict) -> str:
    return f"{ln.get('label') or ln.get('id')}（{ln.get('host')}，{ln.get('queue') or ln.get('id')}）"


def _lane_state(ln: dict) -> str:
    """车道此刻为什么不跑或怎样跑：已归还、暂停中、被临时指令挡、依赖、推演排不上、在等，都没有为空。"""
    if ln.get("returned"):
        return f"主机已归还（{fmt_time(ln['returned'])}）" + (f"，经 {ln['read_via']} 代读" if ln.get("read_via") else "")
    dep = {"failed": "队首等的任务跑失败了", "missing": "队首等的任务找不到", "cycle": "队首的依赖成环"}.get(ln.get("dep") or "")
    parts = [f"暂停中：{ln['paused']}" if ln.get("paused") else "", f"被临时指令挡住（{ln['held']}）" if ln.get("held") and not ln.get("paused") else "",
             dep or "", f"推演排不上：{ln['stuck']}" if ln.get("stuck") else "",
             f"等待中：{ln['waiting']}" if ln.get("waiting") and not ln.get("paused") else "",
             "队列进程不在" if ln.get("started") and not ln.get("alive") and not ln.get("managed") and not ln.get("paused") else ""]
    return "；".join(p for p in parts if p)


def status(pid: str, glob: dict | None = None, now: float | None = None) -> dict:
    """labmon status（#52）：项目快照（docs/schema.md）排成几段：各主机与卡、各车道、运行中（进度、预计结束、此刻 CPU、日志最后一行）、
    排队的预计开始、最近失败、计划项、挪法建议、此刻的告警。glob：看的人能看的全局快照（users.view 过的），只取本项目在跑任务的 CPU。
    快照还没有：rc 0、一句话；已归档的另写那句（projects.archived_msg）。data 是项目快照原样（没有为 None）。"""
    now = time.time() if now is None else now
    o = Out()
    gone = PJ.archived_msg(pid)
    if gone:
        o.p(f"[labmon] {gone}")
    snap = load_json(PJ.out_dir(pid) / "snapshot.json", None)
    if not snap:
        o.p(f"[labmon] 项目 {pid} 的第一次采集还没完成，过一两分钟再看")
        return o.done(0, data=None)
    age = now - (snap.get("generated") or now)
    stale = (snap.get("thresholds") or {}).get("stale_s")
    head = f"[labmon] {snap.get('title') or pid}（{pid}）快照 {fmt_time(snap.get('generated') or now)}（{fmt_dur(age)}前）"
    if stale and age > stale:
        head += "：看板没在采集，告诉用户（会话不重启看板）"
    if snap.get("all_done_ts"):
        head += f"；预计全部跑完 {fmt_time(snap['all_done_ts'])}"
    if snap.get("no_work_since"):
        head += f"；暂时没实验（{fmt_time(snap['no_work_since'])} 起）"
    o.p(head)
    if (snap.get("lan") or {}).get("ok") is False:
        o.p("  看板那台此刻不在内网：数据是离开前的")
    o.p("主机：")
    for h in snap.get("hosts") or []:
        name = h.get("label") or h["id"]
        if h.get("returned"):
            o.p(f"  {name}：已归还（{fmt_time(h['returned'])}）")
            continue
        st = "正常" if h.get("ok") else f"连不上：{h.get('error')}" + (f"（上次 {fmt_time(h['last_ok'])}）" if h.get("last_ok") else "")
        cards = "，".join(f"卡 {g['index']} {g.get('util', 0)}% {g.get('mem_used', 0) / 1024:.1f}/{g.get('mem_total', 0) / 1024:.0f} GiB"
                         for g in h.get("gpus") or [])
        o.p(f"  {name}：{st}" + (f"；{cards}" if cards else "") + (f"；{borrow_text(h['borrow'])}" if h.get("borrow") else ""))
    lanes = {ln["id"]: ln for ln in snap.get("lanes") or []}
    o.p("车道：")
    for ln in lanes.values():
        cnt = f"{ln.get('done', 0)}/{ln.get('total', 0)} 跑完" + (f"，失败 {ln['failed']}" if ln.get("failed") else "")
        end = f"；预计 {fmt_time(ln['eta_ts'])} 跑完" if ln.get("eta_ts") and ln.get("unfinished") else ""
        why = _lane_state(ln)
        o.p(f"  {_lane_name(ln)}：{cnt}{end}" + (f"；{why}" if why else ""))
    jobs = snap.get("jobs") or []

    def where(j):
        ln = lanes.get(j.get("lane"))
        return f"{ln.get('label') or j['lane']}（{ln.get('host')}）" if ln else str(j.get("lane"))
    cores = {(e.get("id"), r.get("name")): (r.get("use") or {}).get("cores") for e in (glob or {}).get("hosts") or []
             for r in (e.get("status") or {}).get("running") or [] if r.get("project") == pid}
    run = [j for j in jobs if j.get("status") == "running"]
    if run:
        o.p("运行中：")
    for j in run:
        host = (lanes.get(j.get("lane")) or {}).get("host")
        bits = [f"进度 {j['progress'][0]}/{j['progress'][1]}" if j.get("progress") else "没有进度行",
                f"已跑 {fmt_dur(j['elapsed_min'] * 60)}" if j.get("elapsed_min") is not None else "",
                f"预计 {fmt_time(j['eta_ts'])} 结束（{j.get('eta_source') or '—'}）" if j.get("eta_ts") else "",
                f"超估时 {fmt_dur(j['overrun_min'] * 60)}" if j.get("overrun_min") else "",
                "挂起中（SIGSTOP）" if j.get("suspended") else "",
                f"CPU {cores[(host, j['id'])]:.1f} 核" if cores.get((host, j["id"])) is not None else ""]
        o.p(f"  {j['id']}（{where(j)}）：" + "，".join(b for b in bits if b))
        if j.get("last_line"):
            o.p("      日志" + (f" {fmt_dur(now - j['log_mtime'])}前" if j.get("log_mtime") else "") + f"：{j['last_line']}")
    queued = [j for j in jobs if j.get("status") == "queued"]
    if queued:
        queued.sort(key=lambda j: (j.get("plan_start_ts") is None, j.get("plan_start_ts") or 0))
        o.p(f"排队（{len(queued)} 个" + ("，按预计开始列前 8 个" if len(queued) > 8 else "") + "）：")
        for j in queued[:8]:
            start = f"预计 {fmt_time(j['plan_start_ts'])} 开始" if j.get("plan_start_ts") else "推演不出何时开始"
            o.p(f"  {j['id']}（{where(j)}）：{start}" + (f"，{fmt_time(j['eta_ts'])} 结束" if j.get("eta_ts") else ""))
    failed = sorted((j for j in jobs if j.get("status") == "failed"), key=lambda j: -(j.get("end_ts") or 0))
    if failed:
        o.p(f"失败（{len(failed)} 个" + ("，列最近 5 个" if len(failed) > 5 else "") + "；修好后删状态目录里的 <名称>.failed 重跑）：")
        for j in failed[:5]:
            ln = lanes.get(j.get("lane")) or {}
            log = f"{ln['logs_dir']}/{j['id']}.log" if ln.get("logs_dir") else ""
            o.p(f"  {j['id']}（{where(j)}）" + (f" {fmt_time(j['end_ts'])}" if j.get("end_ts") else "") + (f"，退出码 {j['killed']}（被信号终止）" if j.get("killed") else "")
                + (f"：{j['last_line']}" if j.get("last_line") else "") + (f"；日志 {log}" if log else ""))
    groups = snap.get("groups") or []
    if groups:
        o.p("计划项：")
    for g in groups:
        ratio = "，".join(f"{r['cls']} 实际/估时 ×{r['k']:.2f}" for r in g.get("ratios") or [] if r.get("k") is not None)
        o.p(f"  {g.get('label') or g['id']}：{g.get('status')}，{g.get('done', 0)}/{g.get('total', 0)} 跑完"
            + (f"，{g['running']} 在跑" if g.get("running") else "") + (f"，{g['queued']} 排队" if g.get("queued") else "")
            + (f"，失败 {g['failed']}" if g.get("failed") else "") + (f"；预计 {fmt_time(g['eta_ts'])}" if g.get("eta_ts") and g.get("status") != "完成" else "")
            + (f"；{ratio}" if ratio else ""))
    rb = snap.get("rebalance") or {}
    if rb.get("text"):
        o.p(f"挪法建议：{rb['text']}")
    elif rb.get("why"):
        o.p("别的主机接不了本项目的任务：" + "；".join(rb["why"]))
    alerts = snap.get("alerts") or []
    o.p(f"告警（{len(alerts)} 条）" + ("：" if alerts else ""))
    for a in alerts:
        o.p(f"  - [{a['key']}] {a['msg']}" + ("（用户也看得到）" if a.get("to") == "both" else "（要用户处理）" if a.get("to") == "user" else ""))
    return o.done(0, data=snap)


def disk(pid: str, psnap: dict | None = None) -> dict:
    """labmon disk（#52）：本项目在共享盘上各目录的大小与增量、最大的几项，整块盘的剩余与几天满，各主机本地盘上本项目的缓存与单格临时、本地盘余量。
    数据 disk.project_storage（与项目页「本项目占用」同一份；#53 起细到项目根下的一级目录、带 24 小时与 7 天增量）。psnap：项目快照（本地盘余量；缺省现读）。"""
    o = Out()
    s = DK.project_storage(pid)
    psnap = psnap if psnap is not None else (load_json(PJ.out_dir(pid) / "snapshot.json", None) or {})
    free, total = s.get("free"), s.get("total")
    if total:
        line = f"[labmon] 共享盘剩 {_gib(free)} / 共 {_gib(total)}（{free / total:.0%}）"
        eta = "，".join(f"按近 {w}的速度约 {d:g} 天满" for w, d in (("24 小时", s.get("eta")), ("7 天", s.get("eta7"))) if d is not None)
        o.p(line + (f"；{eta}" if eta else "；近来没在涨"))
    else:
        o.p("[labmon] 共享盘的总量与剩余还没读到")
    dirs = s.get("dirs") or {}
    d24, d7, at, top = (s.get(k) or {} for k in ("d24", "d7", "at", "top"))
    if dirs:
        o.p("本项目在共享盘上（扫描时刻各目录不同，见行尾）：")
        for u, b in sorted(dirs.items(), key=lambda kv: -(kv[1] or 0)):
            grow = "，".join(f"近 {w} {_delta(x.get(u))}" for w, x in (("24 小时", d24), ("7 天", d7)) if x.get(u))
            o.p(f"  {u}  {_gib(b)}" + (f"（{grow}）" if grow else "") + (f"  扫描 {fmt_time(at[u])}" if at.get(u) else ""))
            for p, pb in (top.get(u) or [])[:5]:
                o.p(f"      {p}  {_gib(pb)}")
        tot24 = sum(x for x in d24.values() if x)
        o.p(f"  合计 {_gib(sum(b or 0 for b in dirs.values()))}" + (f"，近 24 小时 {_delta(tot24)}" if tot24 else ""))
    else:
        o.p("共享盘还没扫到本项目的目录（扫描轮流做，新目录要等一阵）")
    local = s.get("local") or {}
    hosts = {h["id"]: h for h in psnap.get("hosts") or []}
    ids = list(dict.fromkeys([*hosts, *local]))
    if ids:
        o.p("各主机本地盘（续租、容器重启会清空；能重算的缓存放这里，用 needs= 声明）：")
    for hid in ids:
        loc, h = local.get(hid) or {}, hosts.get(hid) or {}
        if h.get("returned"):
            continue
        mine = f"本项目缓存 {_gib(loc.get('cache') or 0)}，单格临时 {_gib(loc.get('scratch') or 0)}" if loc else "本项目没有缓存"
        room = "，".join(f"{m.get('label') or m['path']} 剩 {m['avail_gb']:.1f} GiB" for m in h.get("mounts") or [] if m.get("avail_gb") is not None)
        o.p(f"  {h.get('label') or hid}：{mine}" + (f"；{room}" if room else ""))
    return o.done(0, data=s)


def records(pid: str, host: str | None = None, cat: str | None = None, q: str | None = None, limit: int = 30, see=None) -> dict:
    """labmon records（#52）：本项目所用主机上算它的记录（records.project_rows，与项目页时间线的事件同一规则），新的在前，最多 limit 条。
    see：看的人能看哪些（users.visible）。data 是记录行。"""
    o = Out()
    psnap = load_json(PJ.out_dir(pid) / "snapshot.json", None) or {}
    hosts = [h["id"] for h in psnap.get("hosts") or []]
    if host and host not in hosts:
        o.err(f"[labmon] 主机 {host} 不在项目 {pid} 用的主机里（{'、'.join(hosts) or '还没有'}）")
        return o.done(3, data=[])
    rows = project_rows(pid, hosts, host, cat, q, limit, see)
    if not rows:
        o.p("[labmon] 没有符合的记录")
    for e in rows:
        name = EVENT_KINDS.get(e.get("kind"), (e.get("kind") or "",))[0]
        on = e.get("host") or "、".join(e.get("hosts") or []) or "全部主机"
        o.p(f"{fmt_time(e['t'])} [{on}] {name}：{e.get('msg') or ''}")
        if e.get("detail"):
            o.p("      " + str(e["detail"]).replace("\n", "\n      "))
    return o.done(0, data=rows)
