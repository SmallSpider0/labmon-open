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
from . import platform as PF
from . import pool as P
from . import projects as PJ
from . import users as U
from . import workerctl as W
from .config import GIB, HOSTS_LOCK, ConfigError, active_hosts, all_hosts, fmt_dur, fmt_time, home_peers, host_platform, host_pool, host_share, host_ssh_key, hosts_file, is_pool_id, is_standalone, pools, resolve_pool, set_returned, set_sched_ovr, user_hosts
from .worker import cpu_line, ka_lines
from .worker.pure import other_load, other_load_text
from .records import EVENT_KINDS, project_rows
from .state import load_json, snapshot

OVR = {"pause": ("add", "pause"), "resume": ("rm", "pause"), "focus": ("add", "focus"), "unfocus": ("rm", "focus"),
       "drain": ("add", "drain"), "undrain": ("rm", "drain")}
WORKER_ACTS = ("start", "stop", "status", "add", "rm", "limit", "fit", "clean")


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
    worker_home 已补缺省值；home_with 是与它共用 worker_home 的别的在用主机（config.home_peers，#35 A；已归还或独立机为 []）。
    同时返回分类字段与托管公钥指纹（绝不返回私钥路径或原文）。"""
    hf = hosts_file()
    if not hf.exists():
        return {"missing": str(hf), "hosts": [], "pools": []}
    keys = ("id", "returned", "ssh", "port", "label", "borrow", "owner", "lease_until", "python", "cpu_quota", "mps_vram_gb")
    act, full = active_hosts(), {str(h["id"]): h for h in all_hosts()}
    try:
        pm = pools()
    except ConfigError:
        pm = {}
    out = []
    for raw in user_hosts():
        hid = str(raw.get("id"))
        h = full.get(hid) or raw
        sk = host_ssh_key(h)
        fp = None
        if sk == "managed":
            try:
                info = PF.ssh_key_info(h.get("owner"), hid)
                fp = info.get("fingerprint") if info else None
            except PF.PlatformError:
                fp = None
        hp = host_pool(hid, pm)
        out.append({
            **{k: raw.get(k) for k in keys},
            "worker_home": h.get("worker_home"),
            "data_root": h.get("data_root"),
            "local_dir": h.get("local_dir"),
            "disk_warn_gb": h.get("disk_warn_gb"),
            "disk_stop_gb": h.get("disk_stop_gb"),
            "disk_quota_gb": h.get("disk_quota_gb"),
            "platform_kind": host_platform(h),
            "share": host_share(h),
            "standalone": is_standalone(h),
            "ssh_key_mode": sk,
            "ssh_key_fp": fp,
            "home_with": [x for x in home_peers(hid, act) if x != hid],
            "pool_id": hp["id"] if hp else None,
            "pool_host_id": hp["host_id"] if hp else None,
            "pool_exclusive": bool(hp["exclusive"]) if hp else False,
        })
    p_out = [{"id": p["id"], "host_id": p["host_id"], "label": p["label"], "hosts": p["hosts"],
              "active_hosts": p["active_hosts"], "exclusive": p["exclusive"], "worker_home": p["worker_home"]}
             for p in pm.values()]
    return {"missing": None, "hosts": out, "pools": p_out}


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
            o.p("[labmon] " + B.begin(sid, until, projects, light=True, away=away, account=account, src="命令行 labmon borrow"))     # 命令行等于页面上缺省勾着「对方在轻用」
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
             by: str | None = None, check=None, own: bool = False) -> dict:
    """labmon pause|resume|focus|unfocus|drain|undrain：项目的不带 --host 记在主机清单（所有主机，含之后加入的，#96），带 --host 的、drain 与 --now
    经 workerctl.override_each 下给那几台。每台一行：worker 打印的“生效中：…”，或 ssh 连不上的原因。
    by：谁下的（项目级暂停记进主机清单 [sched.pause_by]，#16；带 --host 的暂停、优先记进那几台指令的 by，#27）。
    check(主机或 None)：暂停写之前核能不能动（users.need_pause，不行抛 users.Denied）：项目级的在主机清单的锁里核一次（None），带 --host 的逐台核，都过了才写。
    own：普通用户（#27）：带 --host 的暂停另带 expect_by，worker 在锁里再核一次那台的现状（看板按快照核，快照可能晚一轮）。"""
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
                    check(None)
                o.p("[labmon] " + set_sched_ovr(kind, tgt, action == "add", until if action == "add" else None, by=by))
                if not (now and kind == "pause" and action == "add"):
                    return o.done(0)
                rs = W.override_each("add", "pause", tgt, hs if immediate_hosts is None else immediate_hosts, None, True, once=True)
            else:
                if kind == "pause" and check is not None:
                    for h in hs:
                        check(h)
                rs = W.override_each(action, kind, tgt, hs, until, now, by=by if kind in ("pause", "focus") else None,
                                     expect_by=by if own and kind == "pause" else None)
    except ConfigError as e:
        o.err(f"[labmon] {e}")
        return o.done(3)
    rc = 0
    for r in rs:
        o.p(f"[{r['host']}] {r['out'].strip() or ('完成' if r['rc'] == 0 else '退出码 %d' % r['rc'])}")
        rc |= r["rc"]
    return o.done(rc)


def worker(sub: str, hid: str, rest: list[str], gpus: str | None = None, project: str | None = None, stop: str | None = None, own: bool = False,
           lst: bool = False, may=None, who: U.Ident | None = None, *,
           cache: bool = False, scratch: bool = False, dry_run: bool = False, yes: bool = False) -> dict:
    """labmon worker start|stop|status|add|rm|limit|fit|clean：经 workerctl.run_out 一次 ssh；输出是 worker 打印的原文。
    status 成功时末尾补一行与这台共用 worker 目录的在用主机（home_text，#35 A）；may：普通用户能用哪台（None 不限），只列他能用的。
    hid 是共有池入口（pool 或 pool:<id>，#212）时支持 add、rm、status、fit --list（单池时 pool 自动指向该池）；
    rm 在清单此刻登记的地方撤（pool.locate）；独占共有池（exclusive=true）内的单台常驻主机禁止直接 worker add。
    fit --list（lst）：看与这台共用 worker 目录的在用主机（config.home_peers；池入口展开池内在用主机），在这台上读它们的状态（#35 B）。
    clean（#114）：仅清理 <LABMON_LOCAL>/{cache,jobs} 自管缓存与已结束单格临时，默认 --dry-run，显式 --yes 才删，绝不触碰 projects/ 产物。"""
    if sub not in WORKER_ACTS or not hid:
        return usage(f"不认识的 worker 动作 {sub}")
    try:
        if is_pool_id(hid) or hid.startswith("pool:"):
            if sub not in ("add", "rm", "status", "fit"):
                return usage(f"算力池入口只支持 worker add / rm / status / fit --list（不支持 worker {sub}）")
            pcfg = resolve_pool(hid)
            if pcfg is None:
                if sub == "rm" and len(rest) == 2:
                    hid = P.locate(rest[0], rest[1])
                else:
                    return {"rc": 3, "out": "", "err": "[labmon] 主机清单（hosts.toml）尚未配置共有型算力池（[pools.<id>]）\n"}
            else:
                if sub == "status":
                    projs = U.access(who).projects if own and who else None
                    return {"rc": 0, "out": P.status_text(pcfg, snapshot(), own=own, projects=projs), "err": ""}
                if sub == "fit":
                    if not lst:
                        return usage(f"算力池评估请用：labmon worker fit {pcfg['host_id']} ROOT JOBS --list --project <项目>")
                    if not pcfg["active_hosts"]:
                        return {"rc": 3, "out": "", "err": f"[labmon] 算力池「{pcfg['label']}」（{pcfg['host_id']}）目前没有在用主机\n"}
                    if project and (reason := unknown_project(project, require=False)):
                        return usage(reason)
                    rc, out = W.run_out("fit", pcfg["active_hosts"][0], rest, project=project, list=True,
                                        hosts=",".join(pcfg["active_hosts"]))
                    return {"rc": rc, "out": out, "err": ""}
                if sub == "rm" and len(rest) == 2:
                    hid = P.locate(rest[0], rest[1], pid=pcfg["id"])
                else:
                    hid = pcfg["host_id"]
        elif sub == "add":
            if ep := P.exclusive_pool(hid):
                return {"rc": 2, "out": "",
                        "err": f"[labmon] 主机 {hid} 属于独占共有算力池「{ep['label']}」（{ep['host_id']}），禁止直接登记到单台；"
                               f"请改用：labmon worker add {ep['host_id']} ROOT JOBS --project <项目>"
                               "（如需允许直投，在 hosts.toml 对应池下设 exclusive = false）\n"}
        with PJ._reg_locked() if sub == "add" else nullcontext():
            if project and sub not in ("rm", "clean") and (reason := unknown_project(project, require=sub == "add")):
                return usage(reason)
            fl = {"list": True, "hosts": ",".join(h for h in home_peers(hid) if h == hid or may is None or may(h))} if lst and sub == "fit" else {}
            cl = {"cache": cache, "scratch": scratch, "dry_run": dry_run, "yes": yes} if sub == "clean" else {}
            rc, out = W.run_out(sub, hid, rest, gpus, project=project if sub != "rm" else None, stop=stop,
                                **({"expect_project": project} if own and sub in ("add", "rm") else {}), **fl, **cl)
    except ConfigError as e:
        return {"rc": 3, "out": "", "err": f"[labmon] {e}\n"}
    extra = home_text(hid, may) if sub == "status" and rc == 0 else ""
    if extra:
        out = out.rstrip("\n") + "\n" + extra
    return {"rc": rc, "out": out, "err": ""}


def home_text(hid: str, may=None) -> str:
    """worker status 末尾那一行：独立主机注明独立；共享盘主机列出与它共用 worker 目录的在用主机；只有它自己时为空。may 同 worker。"""
    act = active_hosts()
    me = next((h for h in act if str(h["id"]) == hid), None)
    if me and is_standalone(me):
        return f"  独立主机（不共享集群盘；工作根目录 {me['data_root']}）\n"
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


def check(pid: str, text: str, may=None, *, probe: bool = False) -> dict:
    """labmon check：按看板这边的用户级主机清单整套校验项目目录里那份（还没推上来的也行）。
    默认输出紧凑校验摘要（几台主机、几条清单通过校验）；probe=True（--probe）时打出各主机的完整探针规格 JSON（带上全局快照里本项目的登记）。
    may(主机 id)：看的人能不能用这台（普通用户经 users.may_host，#16）：不能用的只打一行，不打连接信息与探针规格（主机清单里有没有这台都一样）；None 全打。
    after 是同步状态那行（客户端打）之后的几行：不再生效的旧键、wait 还没报过的页面上的改动与归档（列了的 wait 不重报，waits.check_told）。
    校验不过抛 projects.Refused(400)：客户端打“配置错误：…”、退出 3。"""
    from . import waits
    cfg = PJ.check_config(pid, text, {})
    out = PJ.out_dir(pid)
    with PJ._reg_locked():
        told = waits.check_told(cfg, waits.state_file(out), remember=PJ.cfg_file(pid).is_file())
    ign = cfg["ignored"]
    cfg = C.lanes_cfg(cfg, snapshot(), out / ".labmon", time.time(), save=False)
    o, after = Out(), []
    if probe:
        for h in cfg["hosts"]:
            if may is not None and not may(h["id"]):
                o.p(f"# {h['id']}（没开给你用：不列连接信息与探针规格，要用找管理员开）")
                continue
            o.p(f"# {h['id']}（{h['ssh'] or '本机'}）")
            o.p(json.dumps(C.probe_spec(cfg, h), ensure_ascii=False, indent=2))
    else:
        ok_ids, denied, n_queues = [], [], 0
        for h in cfg["hosts"]:
            if may is not None and not may(h["id"]):
                denied.append(h["id"])
                continue
            ok_ids.append(h["id"])
            n_queues += len(C.probe_spec(cfg, h)["queues"])
        hosts_s = f"：{'、'.join(ok_ids)}" if ok_ids else ""
        o.p(f"[labmon] 校验通过：{pid}（{len(ok_ids)} 台主机{hosts_s}，{n_queues} 条清单）；加 --probe 看探针规格")
        for hid in denied:
            o.p(f"# {hid}（没开给你用：不列连接信息与探针规格，要用找管理员开）")
    if ign:
        why = ([f"输出目录固定在 {out}"] if "out_dir" in ign else []) + (["项目页「本机」一栏删了"] if "local" in ign else [])
        after.append(f"[labmon] monitor.toml 里的 {'、'.join('[local]' if k == 'local' else k for k in ign)} 不再生效（{'，'.join(why)}），不用改")
    # 静态核对独立主机的跨主机依赖 / 成组（读全局快照 todo，不新增 ssh 连接，#114）
    glob = snapshot() or {}
    sa_ids = {h["id"] for h in cfg["hosts"] if is_standalone(h)}
    if sa_ids:
        job_hosts: dict[tuple, set[str]] = {}
        for he in glob.get("hosts") or []:
            for ln in (he.get("status") or {}).get("todo") or []:
                for n in ln.get("jobs") or []:
                    job_hosts.setdefault((ln.get("project"), n), set()).add(he["id"])
        for he in glob.get("hosts") or []:
            hid = he.get("id")
            if hid not in sa_ids:
                continue
            loc_gang: dict[tuple, int] = {}
            for ln in (he.get("status") or {}).get("todo") or []:
                if ln.get("project") != pid:
                    continue
                for n, ds in (ln.get("deps") or {}).items():
                    for dp, dn in ds:
                        others = job_hosts.get((dp, dn), set()) - {hid}
                        if others and hid not in job_hosts.get((dp, dn), set()):
                            after.append(f"[labmon] 警告：独立主机 {hid} 的任务 {n} 依赖 {dp}/{dn}（位于 {'、'.join(sorted(others))}）；独立主机不共享 worker_home，不支持跨主机 after=")
                for n, (g, gn) in (ln.get("gang") or {}).items():
                    loc_gang[(g, gn)] = loc_gang.get((g, gn), 0) + 1
            for (g, gn), cnt in loc_gang.items():
                if gn and cnt < gn:
                    after.append(f"[labmon] 警告：独立主机 {hid} 的成组 {g} 声明 {gn} 个成员但本机仅 {cnt} 个；独立主机不支持跨主机成组，只支持同机多清单成组")
    after += [f"[labmon] {m}" for m in told]
    return o.done(0, after=after)


# ---------- 只读：项目现状、存储、记录（#52：实验会话不看页面，要读的都经命令行） ----------
# GET /api/project/<id>/(status|disk|records)：回 {rc, out, err, data}，命令行原样打出 out；--json 打 data。
def _gib(b: float | None) -> str:
    return "—" if b is None else f"{C.gb(b)} GiB"


def _clip_line(s: str, limit: int = 100) -> str:
    """将日志尾行压成单行并限制长度（<= limit 字符），防超长堆栈/参数刷屏。"""
    line = " ".join(s.splitlines()).strip()
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _delta(b: float | None) -> str:
    return "" if not b else f"{'+' if b > 0 else '−'}{C.gb(abs(b))} GiB"


def _card(g: dict) -> str:
    """一张卡的一句：利用率、显存（MiB 换 GiB）；探针读不到的（nvidia-smi 回 N/A，记 None）写“?”。"""
    def v(x, f):
        return "?" if x is None else format(x / 1024, f)
    u = g.get("util")
    return f"卡 {g['index']} {'?' if u is None else f'{u:g}'}% {v(g.get('mem_used'), '.1f')}/{v(g.get('mem_total'), '.0f')} GiB"


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
    排队的预计开始、最近失败、计划项、挪法建议、此刻的告警。glob：看的人能看的全局快照（users.view 过的），取本项目在跑任务的 CPU、各主机的保活与 CPU 配额（worker 状态，ka_lines、cpu_line）。
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
    wst = {e.get("id"): e.get("status") for e in (glob or {}).get("hosts") or [] if e.get("worker_up")}   # worker 不在时状态是旧的，不报保活
    for h in snap.get("hosts") or []:
        name = h.get("label") or h["id"]
        if h.get("returned"):
            o.p(f"  {name}：已归还（{fmt_time(h['returned'])}）")
            continue
        if h.get("pool"):                          # 算力池（#24）：合成主机，没有卡；连没连上看代读池车道的那台
            o.p(f"  {name}：交给池、还没放到哪台的清单在这里等" + ("" if h.get("ok") else f"；这一轮没读到池里的清单（{h.get('error') or '代读的主机连不上'}）"))
            continue
        if h.get("ephemeral"):                     # 推模式临时主机（#213）：标注 [临时] 并附云盘/本地盘余量，省略保活与借用
            st = "在线" if h.get("ok") else f"已离线：{h.get('error') or '离线'}"
            cards = "，".join(_card(g) for g in h.get("gpus") or [])
            mounts = "，".join(
                f"{m.get('label') or m['path']} 剩 {m['avail_gb']:.1f}"
                + (f"/{m['total_gb']:.1f}" if m.get("total_gb") else "") + " GiB"
                + (" [余量低]" if m.get("kind") == "cloud" and m.get("min_gb") is not None and m["avail_gb"] < m["min_gb"] else "")
                for m in h.get("mounts") or [] if m.get("avail_gb") is not None
            )
            o.p(f"  {name} [临时]：{st}" + (f"；{cards}" if cards else "") + (f"；{mounts}" if mounts else ""))
            continue
        st = "正常" if h.get("ok") else f"连不上：{h.get('error')}" + (f"（上次 {fmt_time(h['last_ok'])}）" if h.get("last_ok") else "")
        cards = "，".join(_card(g) for g in h.get("gpus") or [])
        hst = (wst.get(h["id"]) or {}) if h.get("ok") else {}
        other = other_load_text(other_load(hst, pid)) if hst else ""
        o.p(f"  {name}：{st}" + (f"；{cards}" if cards else "") + (f"；{other}" if other else "") + (f"；{borrow_text(h['borrow'])}" if h.get("borrow") else ""))
        for ln in ka_lines(hst) + [cpu_line(hst)] if hst else []:   # 这台的 GPU 保活（#61：nvidia-smi 里哪个进程是 labmon 的保活）与 CPU 配额、已记的核（#112）
            if ln:
                o.p(f"    {ln}")
    lanes = {ln["id"]: ln for ln in snap.get("lanes") or []}
    o.p("车道：")
    active_lanes = [ln for ln in lanes.values() if not ln.get("done_lane")]
    done_lanes = [ln for ln in lanes.values() if ln.get("done_lane")]
    show_lanes = active_lanes + (done_lanes if not active_lanes and len(done_lanes) <= 3 else [])
    for ln in show_lanes:
        cnt = f"{ln.get('done', 0)}/{ln.get('total', 0)} 跑完" + (f"，失败 {ln['failed']}" if ln.get("failed") else "") \
            + (f"，并行 {ln.get('running_n') or 0}/{ln['par']} 在跑" if (ln.get("par") or 1) > 1 else "")     # 并行清单（#147）
        end = f"；预计 {fmt_time(ln['eta_ts'])} 跑完" if ln.get("eta_ts") and ln.get("unfinished") else ""
        why = _lane_state(ln)
        o.p(f"  {_lane_name(ln)}：{cnt}{end}" + (f"；{why}" if why else ""))
    if done_lanes and len(show_lanes) < len(lanes):
        o.p(f"  另有 {len(done_lanes)} 条已跑完的车道（共 {sum(ln.get('total', 0) for ln in done_lanes)} 个任务，不占车道编号）")
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
        if j.get("artifact_dir"):
            o.p(f"      产物：{j['artifact_dir']}")
        if j.get("last_line") and (tail := _clip_line(str(j["last_line"]))):
            o.p("      日志" + (f" {fmt_dur(now - j['log_mtime'])}前" if j.get("log_mtime") else "") + f"：{tail}")
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
            if j.get("artifact_dir"):
                o.p(f"      产物：{j['artifact_dir']}")
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
    if rb.get("text") and not rb.get("est"):
        o.p(f"挪法建议：{rb['text']}")
    else:                                # 时长不明（est，#43）：有主机有空、推演算不出挪过去能提前多少；与挪不了的原因可以同时有
        if rb.get("est"):
            o.p(f"可能可以提前：{rb['text']}")
        if rb.get("why"):
            o.p("别的主机接不了本项目的任务：" + "；".join(rb["why"]))
    alerts = snap.get("alerts") or []
    o.p(f"告警（{len(alerts)} 条）" + ("：" if alerts else ""))
    for a in alerts:
        o.p(f"  - [{a['key']}] {a['msg']}" + ("（用户也看得到）" if a.get("to") == "both" else "（要用户处理）" if a.get("to") == "user" else ""))
    return o.done(0, data=snap)


def disk(pid: str, psnap: dict | None = None) -> dict:
    """labmon disk（#52、#114）：区分「共享盘（集群主机共用）」与「独立主机本地工作盘（各机独立）」。
    数据是项目快照的 storage（disk.project_storage，#53；与项目页「本项目占用」同一份）。
    纯独立机项目不再输出“共享盘的总量与剩余还没读到”的误导文案。"""
    o = Out()
    psnap = psnap if psnap is not None else load_json(PJ.out_dir(pid) / "snapshot.json", None)
    if not psnap:
        o.p(f"[labmon] 看板上还没有 {pid} 的快照：第一次采集还没完成，过一两分钟再看")
        return o.done(0, data=None)
    s = psnap.get("storage") or {}
    sa = s.get("standalone") or {}
    free, total = s.get("free"), s.get("total")
    dirs = sorted(s.get("dirs") or [], key=lambda d: -(d.get("b") or 0))
    has_shared = bool(total or dirs or s.get("roots"))
    if total:
        eta = "，".join(f"按近 {w}的速度约 {d:g} 天满" for w, d in (("24 小时", s.get("eta")), ("7 天", s.get("eta7"))) if d is not None)
        o.p(f"[labmon] 共享盘剩 {_gib(free)} / 共 {_gib(total)}（{free / total:.0%}）" + (f"；{eta}" if eta else "；近来没在涨")
            + (f"；索引 {fmt_time(s['at'])}" if s.get("at") else ""))
    elif not sa:
        o.p("[labmon] 共享盘的总量与剩余还没读到")
    if dirs:
        grow = "，".join(f"近 {w} {_delta(x) or '没变'}" for w, x in (("24 小时", s.get("d24")), ("7 天", s.get("d7"))) if x is not None)
        o.p(f"本项目在共享盘上（{'、'.join(s.get('roots') or []) or '项目目录'}）：合计 {_gib(s.get('b'))}" + (f"（{grow}）" if grow else "")
            + ("；近 24 小时涨得快（disk: 告警）" if s.get("burst") else ""))
        if s.get("pending"):
            o.p(f"  还有 {s['pending']} 个目录没扫到，合计只算扫过的")
        for d in dirs:
            g = "，".join(f"近 {w} {_delta(d.get(k))}" for w, k in (("24 小时", "d24"), ("7 天", "d7")) if d.get(k))
            o.p(f"  {C.dir_name(d.get('name'))}  {_gib(d.get('b'))}" + (f"（{g}）" if g else "") + (f"  {d['n']} 个文件" if d.get("n") else "")
                + (f"  扫描 {fmt_time(d['t'])}" if d.get("t") else ""))
            for x in (d.get("top") or [])[:5]:
                o.p(f"      {x[0]}  {_gib(x[1])}")
        for g in s.get("gone") or []:
            if g.get("d7"):
                o.p(f"  {C.dir_name(g.get('name'))}（已删）  近 7 天 {_delta(g['d7'])}")
    elif has_shared or not sa:
        o.p("共享盘还没扫到本项目的目录（扫描轮流做，新目录要等一阵）")
    groups = C.write_groups(s.get("jobs") or [])
    if groups:
        o.p("近来结束的任务（跑的期间本项目目录的净增，同期本项目别的任务写的也在内）：")
    for _, js in groups:
        j = js[-1]
        o.p(f"  {C.write_text(js)}；{j.get('host')} 上 {fmt_time(j['t1'])} 结束" + ("；还在等重扫，数字会变" if any(x.get("wait") for x in js) else "")
            + ("；超过提醒线（bigwrite: 告警）" if j.get("big") else ""))
    if sa:
        o.p("独立主机本地工作盘（各机持久保存、无共享盘；labmon 绝不自动删 projects/ 产物）：")
        for hid, sw in sa.items():
            dg = sw.get("disk_gate") or {}
            fr, tot = sw.get("free"), sw.get("total")
            room = f"剩 {_gib(fr)} / 共 {_gib(tot)}" if tot else "余量未读到"
            thr = []
            if sw.get("quota_gb"):
                thr.append(f"配额 {sw['quota_gb']:g} GiB")
            if sw.get("stop_gb"):
                thr.append(f"停止线 {sw['stop_gb']:g} GiB")
            if sw.get("warn_gb"):
                thr.append(f"预警 {sw['warn_gb']:g} GiB")
            gate = "；磁盘闸门已开启（暂停放新任务）" if dg.get("on") else ""
            o.p(f"  {sw.get('label') or hid}（{sw.get('root') or ''}）：{room}" + (f"（{'，'.join(thr)}）" if thr else "") + gate)
            o.p(f"    本项目占 {_gib(sw.get('b') or 0)}，缓存 {_gib(sw.get('cache') or 0)}，单格临时 {_gib(sw.get('scratch') or 0)}"
                + (f"（还有 {sw['pending']} 个目录未扫）" if sw.get("pending") else ""))
            for sub in (sw.get("top") or [])[:5]:
                o.p(f"      {sub[0]}  {_gib(sub[1])}")
            for r in sw.get("reclaimable") or []:
                kind_zh = {"project": "闲置项目目录（删前先问用户）", "cache": "闲置缓存（可用 labmon worker clean）", "scratch": "闲置单格临时（可用 labmon worker clean）"}.get(r.get("kind"), "可回收")
                o.p(f"      [可回收] {r['path']}  {_gib(r['b'])}（闲置 {r['idle_days']} 天，{kind_zh}）")
            if (sw.get("cache") or 0) + (sw.get("scratch") or 0) > 0:
                o.p(f"    清理自管缓存预览：labmon worker clean {hid} --project {pid}")
    local = s.get("local") or {}
    hosts = {h["id"]: h for h in psnap.get("hosts") or [] if not h.get("returned") and not h.get("pool") and not h.get("ephemeral") and h["id"] not in sa}
    ids = [i for i in dict.fromkeys([*hosts, *local]) if i in hosts]
    if ids:
        o.p("各集群主机本地盘（续租、容器重启会清空；能重算的缓存放这里，用 needs= 声明）：")
    for hid in ids:
        loc, h = local.get(hid) or {}, hosts[hid]
        mine = f"本项目缓存 {_gib(loc.get('cache') or 0)}，单格临时 {_gib(loc.get('scratch') or 0)}" if loc else "本项目没有缓存"
        room = "，".join(f"{m.get('label') or m['path']} 剩 {m['avail_gb']:.1f} GiB" for m in h.get("mounts") or [] if m.get("avail_gb") is not None)
        o.p(f"  {h.get('label') or hid}：{mine}" + (f"；{room}" if room else ""))
    eph_hosts = [h for h in psnap.get("hosts") or [] if h.get("ephemeral") and h.get("ok") and (h.get("mounts") or [])]
    if eph_hosts:
        o.p("临时主机存储（云盘与实例本地盘，不计入实验室共享盘）：")
        for h in eph_hosts:
            items = []
            for m in h.get("mounts") or []:
                if m.get("avail_gb") is None:
                    continue
                kind = "云盘" if m.get("kind") == "cloud" else "本地盘"
                tot = f"/{m['total_gb']:.1f}" if m.get("total_gb") else ""
                low = " [余量低]" if m.get("kind") == "cloud" and m.get("min_gb") is not None and m["avail_gb"] < m["min_gb"] else ""
                items.append(f"[{kind}] {m.get('label') or m['path']} 剩 {m['avail_gb']:.1f}{tot} GiB{low}")
            if items:
                o.p(f"  {h.get('label') or h['id']} [临时]：{'；'.join(items)}")
    return o.done(0, data=s)


def my_disk(who: U.Ident, snap: dict | None = None) -> dict:
    """GET /api/disk（#211，labmon disk --me 及无 monitor.toml 回退）：复用 disk.person_storage 排版输出
    总盘剩余/预计满盘/告警 + 本人名下目录占用、24h/7d 增量、90 天冷数据与各目录明细，不泄露他人目录。"""
    o = Out()
    person, _ = U.person_of(who.name)
    d = DK.person_storage(person, snap)
    if not d or not d.get("df"):
        o.p(f"[labmon] 共享盘数据读不了：{d['error']}" if d and d.get("error") else "[labmon] 共享盘的总量与剩余还没读到")
        return o.done(0, data=d)
    df = d["df"]
    free, total = df.get("free"), df.get("total")
    if total:
        eta = "，".join(f"按近 {w}的速度约 {x:g} 天满" for w, x in (("24 小时", d.get("eta")), ("7 天", d.get("eta7"))) if x is not None)
        o.p(f"[labmon] 共享盘剩 {_gib(free)} / 共 {_gib(total)}（{free / total:.0%}）" + (f"；{eta}" if eta else "；近来没在涨")
            + (f"；索引 {fmt_time(d['at'])}" if d.get("at") else ""))
    else:
        o.p("[labmon] 共享盘的总量与剩余还没读到")
    for a in d.get("alerts") or []:
        o.p(f"  告警：{a['msg']}")
    owners = d.get("owners") or []
    if not person:
        o.p("账号尚未关联组内人员，暂无本人共享盘目录明细。")
        return o.done(0, data=d)
    if not owners:
        o.p(f"共享盘上暂无归属 {person} 的目录。")
        return o.done(0, data=d)
    me = owners[0]
    grow = "，".join(f"近 {w} {_delta(x) or '没变'}" for w, x in (("24 小时", me.get("d24")), ("7 天", me.get("d7"))) if x is not None)
    lim = f"；提醒线 {me['limit']:g} GiB" + ("（已超线）" if me.get("over") else "") if me.get("limit") else ""
    o.p(f"本人（{me['name']}）在共享盘上：合计 {_gib(me.get('b'))}" + (f"（{grow}）" if grow else "") + f"；90 天未改 {_gib(me.get('cold'))}{lim}")
    if me.get("pending"):
        o.p(f"  还有 {me['pending']} 个目录没扫到，合计只算扫过的")
    for u in me.get("units") or []:
        if u.get("b") is None:
            o.p(f"  {u.get('u')}  还没扫")
            continue
        g = "，".join(f"近 {w} {_delta(u.get(k))}" for w, k in (("24 小时", "d24"), ("7 天", "d7")) if u.get(k))
        cold = f"90 天未改 {_gib(u['cold'])}" if (u.get("cold") or 0) >= GIB / 20 else ""
        meta = "，".join(x for x in (g, cold) if x)
        o.p(f"  {u.get('u')}  {_gib(u.get('b'))}" + (f"（{meta}）" if meta else "") + (f"  {u['n']} 个文件" if u.get("n") else "")
            + (f"  扫描 {fmt_time(u['t'])}" if u.get("t") else ""))
        for x in (u.get("top") or [])[:5]:
            o.p(f"      {x[0]}  {_gib(x[1])}")
    return o.done(0, data=d)


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
