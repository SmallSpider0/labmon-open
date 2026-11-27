"""一次采集：按配置在各主机上跑探针，汇成统一快照（snapshot.json），再渲染成数据内联的静态页（index.html）。

输出目录（看板上登记的项目目录里的 out/，projects.out_dir；#161 起 monitor.toml 的 out_dir 不再生效）：
    index.html      数据内联、定时自刷新，浏览器直接打开文件即可
    snapshot.json   本次快照（结构见 docs/schema.md），供其它程序读取
    .labmon/        历史曲线、进度采样等内部状态

快照只含统一结构：hosts / lanes / jobs / groups / alerts / history。页面与告警只读快照，不认识具体项目。
新增队列类型：在 probe.py 写采集函数，在这里的 QUEUE_PARSERS 登记同名解析函数，返回统一的 lane 与 jobs。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import disk as DK
from . import ephemeral as EP
from . import lan as L
from . import lease as LS
from . import pool as PO
from . import projects as PJ
from .config import AUTO_GROUP_RE, GIB, POOL_LABEL, STALE_S, ConfigError, fmt_dur, fmt_time, is_pool_id, parse_time, pick_color, pool_id_of, stale_limit, with_queues
from .fleet import worker_fresh
from .health import link_step
from .state import load_json, load_rows, save_json, snapshot, usage_file, usage_hosts, write_atomic
from .worker.pure import IDLE_REG_S, OVER_FRAC, OVER_MIN_S, is_hold, list_path, overrun_left  # noqa: F401 — 与 worker 共用唯一实现

HERE = Path(__file__).resolve().parent
SCHEMA = 2
PROG_KEEP = 6 * 3600           # 运行中任务的进度采样保留时长
RECENT = 30 * 60               # 按最近这么久的进度估速度
NOPROG_MIN_S = 30 * 60         # 一直没有进度行的任务（超估时）要已用这么久才报 noprogress（几分钟的短任务不打进度也无妨）
IDLE_GRACE_S = 5 * 60         # 保活与任务都不在要连续这么久才报空闲关机风险（避开任务之间、拉起保活前的空档）
# 告警给谁看（#142，维护者 11-10 定：很多告警是给实验会话的，不都在看板上显眼列）：
# user 要用户处理（只有用户能做，或机器出了要人看的事）；agent 只给实验会话（会话自己能处理）；both 两边都要（page 给用户写人话，msg 给会话写操作）
AUDIENCE = ("user", "agent", "both")
POOL_WAIT = "在算力池里等着放到某台"   # 池车道的等待原因（#24；放到哪台由看板定，C 之后会写具体原因）
REG_KEEP_S = 24 * 3600        # 登记没了（worker rm、快照里这轮没有）的车道再按上次见到的主机留这么久（有在跑任务的防闪丢，monitor.toml 写的到期回原主机；跑完的已摘登记立即收起，#164）


# ---------------- 探针 ----------------
def probe_spec(cfg: dict, host: dict) -> dict:
    return {"mounts": [m["path"] for m in host["mounts"]],
            "checks": [{k: c[k] for k in ("id", "if", "cmd", "detail", "timeout") if k in c} for c in host["checks"]],
            # 本机的队列，以及已归还主机上由本机经共享盘代读的队列（readonly：只读文件，PID 属于别的机器，不查进程；已确认全跑完的带 done_sig 短路，#164）；
            # 算力池里的（pool，#78）同样代读，但清单是刚从别处搬回池里的：那边手上在跑的还在跑，current 里放行的主机不是这台的照 #95 按在跑算
            "queues": [{k: v for k, v in q.items() if k not in ("label", "host", "read_via", "reg", "reg_t", "reg_live", "reg_sleeping", "reg_stop", "reg_pool", "done_sig", "done_act")}
                       | ({"sleep_skip": True} if q.get("reg") else {})
                       | ({"readonly": True, **({"done_sig": q["done_sig"], "done_act": q.get("done_act")} if q.get("done_sig") else {})} if q["read_via"] else {})
                       | ({"pool": True} if is_pool_id(q["host"]) else {})
                       for q in cfg["queues"] if (q["read_via"] or q["host"]) == host["id"]],
            # 所有主机上显式登记的清单：共享盘上别的主机登记过的，自动发现时也要跳过
            "explicit_jobs": sorted({os.path.join(q["root"], q["jobs"]) for q in cfg["queues"] if not q["discover"]}),
            # worker 的状态：它在管、空着等资源的队列没有锁，不算掉了，显示它说的等待原因；被临时指令挡住的按暂停算
            "worker_status": host["worker_home"] + f"/hosts/{host['id']}/status.json", "worker_stale": STALE_S,
            "host": host["id"]}                  # current 里放行的主机不是这台时探针不查 PID（清单刚搬来、那台还在跑，#95）


def reg_queues(cfg: dict, regs: list | None, mem: dict, t: float) -> tuple[list[dict], dict]:
    """项目的车道从哪来（#94）：monitor.toml 的 [[queues]] 加上本项目在各主机上的 worker 登记（全局快照的 regs，overview._regs 读的），返回 (队列, 新的 mem)。
    同一条清单（目录 + 清单）以登记为准：主机取登记所在的那台，id 与 label 沿用 monitor.toml 的；只在登记里有的成为新车道，
    id 是「目录末级名/清单」（撞了接目录的短哈希），排在 monitor.toml 的后面，按看板第一次见到的先后（同时见到的按清单路径）编号。
    同一清单登记在两台（#88）取在用的、再取登记晚的；主机清单里没有的主机（unlisted）、这轮没读到的（queues 为 None）不算。
    登记没了的再按上次见到的主机留 REG_KEEP_S，之后 monitor.toml 写了的回到它写的主机，没写的收起。
    通配符队列不动：登记过的清单进探针的 explicit_jobs，自动发现跳过它们。
    同一清单两台都登记时比的登记时刻各是那台容器的时钟（状态里的 added_t、登记文件的 added），差几分钟，只在两份登记相隔较久时分得准。
    只在登记里有的车道带 reg（跑完久了收起，_quiet）、reg_t（在用主机上的登记时刻）与 reg_live（这一轮登记还在）；登记了的都带 reg_stop（登记的停法：
    算力池借过的主机还掉后，池的清单（pool._ours）看板搬回池里，别的照报 returned_unfinished）。
    mem：{清单路径: {id（只在登记里有的车道定下的 id，之后一直沿用）, host, root, jobs, first, seen, reg_t, reg_stop}}（项目状态目录的 reg_lanes.json，看板的时刻）。"""
    found: dict = {}
    for x in regs or []:
        if x.get("state") == "unlisted" or x.get("queues") is None:
            continue
        for q in x["queues"]:
            if q.get("project") != cfg["id"] or not q.get("root") or not q.get("jobs"):
                continue
            # 新旧：在用主机的取状态里的 added_t（epoch 秒），已归还的取登记文件里的 added 文字；先比在不在用，两种不会互相比
            act = x.get("state") == "active"
            k, rank = list_path(q["root"], q["jobs"]), (True, q.get("added_t") or 0) if act else (False, q.get("added") or "")
            if k not in found or rank > found[k][0]:
                found[k] = (rank, x["host"], x.get("state"), q)
    new = {}
    for k, (_, hid, st, q) in found.items():
        om = mem.get(k) or {}
        # done_sig 仅对同一台仍处于已归还状态且同一份登记的清单有效；主机恢复使用（active）或重新登记后立即失效（#164）
        keep_done = st == "returned" and om.get("host") == hid and om.get("done_sig") is not None and om.get("reg_added") == q.get("added")
        new[k] = {"id": om.get("id"), "host": hid, "root": q["root"], "jobs": q["jobs"], "first": om.get("first", t), "seen": t,
                  "reg_t": q.get("added_t"), "reg_added": q.get("added"), "reg_stop": q.get("stop"), "reg_pool": q.get("pool"),
                  "sleeping": bool(q.get("sleeping")),
                  **({"done_sig": om["done_sig"], "done_act": om.get("done_act")} if keep_done else {})}
    new.update({k: m for k, m in mem.items() if k not in new and t - m.get("seen", 0) <= REG_KEEP_S})
    unread = {x.get("host") for x in regs or [] if x.get("queues") is None}
    out, used = [], set()
    for q in cfg["queues"]:
        k = None if q["discover"] else list_path(q.get("root"), q.get("jobs"))
        used.add(k)
        out.append(dict(q, host=new[k]["host"], reg_stop=new[k].get("reg_stop"), reg_pool=new[k].get("reg_pool")) if k in new else q)
    extra = sorted((k for k in new if k not in used), key=lambda k: (new[k]["first"], k))
    # 车道 id 定下来就记在 mem 里一直沿用（撞名的那条先没了，后一条也不改名：进度采样、挂起累计按 车道/任务 记）
    ids = {q["id"] for q in cfg["queues"]} | {new[k]["id"] for k in extra if new[k].get("id")}
    for k in extra:
        m = new[k]
        if not m.get("id") or m["id"] in {q["id"] for q in cfg["queues"]}:
            lid = os.path.basename(os.path.normpath(m["root"])) + "/" + m["jobs"]
            if lid in ids:
                lid += "@" + hashlib.sha1(os.path.normpath(m["root"]).encode()).hexdigest()[:6]
            ids.add(lid)
            new[k] = m = dict(m, id=lid)
        # reg：只在登记里有的车道，收起由 _quiet 判；reg_live：这一轮登记还在（登记没了、已 worker rm 为 False，跑完立即收起，#164；
        # 全局快照缺 regs 或该主机没读到时不误标为已摘）；reg_sleeping：worker 已将该登记转休眠
        live = k in found or regs is None or m["host"] in unread
        out.append({"id": m["id"], "host": m["host"], "root": m["root"], "jobs": m["jobs"], "reg": True, "reg_t": m.get("reg_t"), "reg_live": live,
                    "reg_sleeping": bool(m.get("sleeping")) if k in found else False, "reg_stop": m.get("reg_stop"), "reg_pool": m.get("reg_pool"),
                    **({"done_sig": m["done_sig"], "done_act": m.get("done_act")} if m.get("done_sig") else {})})
    return out, new


def reg_sig(regs: list | None) -> dict[str, frozenset]:
    """各项目的登记此刻在哪（全局快照的 regs，reg_queues 认的那几项）：{项目: {(主机, 清单路径)}}。看板每轮全局采集后比上一轮，
    变了的项目（搬了、登记了、撤了，含算力池放到某台，#78）当场叫醒它的采集线程（serve.Overview.once），车道不用再等一个项目采集间隔才跟过去。"""
    out: dict = {}
    for x in regs or []:
        if x.get("state") == "unlisted" or x.get("queues") is None:
            continue
        for q in x["queues"]:
            if q.get("project") and q.get("root") and q.get("jobs"):
                out.setdefault(q["project"], set()).add((x["host"], list_path(q["root"], q["jobs"])))
    return {k: frozenset(v) for k, v in out.items()}


def lanes_cfg(cfg: dict, glob_snap: dict, sd: Path, t: float, save: bool = True) -> dict:
    """这一轮采集用的配置：按全局快照的登记拼好车道（reg_queues），登记带来的、项目里还没有的主机从用户级主机清单补上（config.with_queues）。
    sd：项目状态目录（.labmon/，reg_lanes.json 在这里）；save=False 只看不记（labmon check）。"""
    f = sd / "reg_lanes.json"
    qs, mem = reg_queues(cfg, glob_snap.get("regs"), load_json(f, {}) or {}, t)
    if save:
        save_json(f, mem)
    return with_queues(cfg, qs)


def run_probe(cfg: dict, host: dict) -> tuple[dict | None, str | None]:
    arg = base64.b64encode(json.dumps(probe_spec(cfg, host)).encode()).decode()
    if host["ssh"]:
        try:
            cmd = L.host_ssh_argv(host) + [f"{host['python']} - {arg}"]
        except ConfigError as e:
            return None, str(e)
    else:
        cmd = [host.get("python_local") or sys.executable, "-", arg]
    try:
        with open(HERE / "probe.py") as src:
            r = (L.ssh_run if host["ssh"] else subprocess.run)(cmd, stdin=src, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "探针超时"
    except OSError as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr.strip().splitlines() or [f"退出码 {r.returncode}"])[-1][:200]
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), None
    except (ValueError, IndexError):
        return None, "探针输出不是 JSON：" + r.stdout[-200:]


# ---------------- 队列解析（每种类型一个函数）----------------
def _parse_queue_log(lines: list[str]) -> dict:
    jobs: dict = {}
    for l in lines:
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (开始|完成|失败) (\S+?)(?:（|\s|$)", l)
        if not m:
            continue
        t = parse_time(m.group(1))
        j = jobs.setdefault(m.group(3), {})
        if m.group(2) == "开始":
            j.update(start=t, end=None, rc=None)
        else:
            rc = re.search(r"退出码 (\d+)", l)
            j.update(end=t, rc=int(rc.group(1)) if rc else None)
    return jobs


def parse_gpu_queue(raw: dict, t: float) -> tuple[dict, list[dict]]:
    log = _parse_queue_log(raw.get("log_tail", []))
    marks = raw.get("marks", {})
    curs = {c["name"]: c for c in raw.get("current") or [] if c.get("alive")}   # 在跑的（并行清单有几个，#147）
    wk = raw.get("worker") or {}
    durs, plan = wk.get("dur") or {}, wk.get("plan") or {}   # worker 的 duration 算的预计时长、推演出的预计开始与结束（不归 worker 管的队列没有）
    jobs = []
    for j in raw.get("jobs", []):
        name = j["name"]
        lj = log.get(name, {})
        row = {"id": name, "est_min": j.get("est_min"), "dur": durs.get(name), "plan": plan.get(name)}
        if f"{name}.done" in marks or f"{name}.failed" in marks:
            row["status"] = "done" if f"{name}.done" in marks else "failed"
            row["end_ts"] = lj.get("end") or marks.get(f"{name}.done") or marks.get(f"{name}.failed")
            row["start_ts"] = lj.get("start") if lj.get("start") and lj["start"] <= row["end_ts"] else None
            if row["status"] == "failed" and lj.get("rc") in (130, 137, 143):
                row["killed"] = lj["rc"]                 # 被信号终止（Ctrl-C / kill -9 / kill），不是程序自己出错
        elif name in curs:
            cur = curs[name]
            row["status"] = "running"
            row["where"] = cur.get("host")               # current 里放行的主机（#95），_lane 定画到哪台
            row["start_ts"] = lj.get("start") or cur.get("t0") or t
            m = re.search(r"(\d+)/(\d+)", cur.get("progress_line") or "")
            if m and int(m.group(2)) > 0:
                row["progress"] = [int(m.group(1)), int(m.group(2))]
            row["last_line"] = cur.get("last_line")
            row["log_mtime"] = cur.get("log_mtime")
            if cur.get("stopped"):
                row["suspended"] = True                  # 任务进程被 SIGSTOP 挂起（kill -CONT 恢复）
        else:
            row["status"] = "queued"
        jobs.append(row)
    lane = {"alive": bool(raw.get("lock_alive")), "pid": raw.get("lock_pid"), "state_dir": raw.get("state_dir"), "logs_dir": raw.get("logs_dir"),
            "paused": raw.get("paused") if not raw.get("lock_alive") else None,
            "managed": raw.get("worker") is not None, "waiting": wk.get("wait"), "held": wk.get("held"), "held_t": wk.get("held_t"), "dep": wk.get("dep"), "head": wk.get("head"), "bad": wk.get("bad") or {}, "stuck": wk.get("stuck"),
            "cpu_wait": wk.get("cpu"),      # 队首一直因 CPU 被挡满 10 分钟（worker 定）后已挡的秒数
            "par": wk.get("par") or 1, "running_n": sum(j["status"] == "running" for j in jobs)}   # 清单声明的并行数与此刻在跑几行（#147）
    return lane, jobs


def parse_ephemeral_queue(raw: dict, t: float) -> tuple[dict, list[dict]]:
    """解析推模式临时主机上报的任务列表（ephemeral.as_probe，#213）。"""
    jobs = []
    for j in raw.get("jobs") or []:
        row: dict = {
            "id": j["id"], "status": j["status"], "est_min": j.get("est_min"),
            "dur": None, "plan": None,
            "start_ts": j.get("start_ts"), "end_ts": j.get("end_ts"),
            "last_line": j.get("last_line"), "log_mtime": j.get("log_mtime"),
        }
        if j.get("progress"):
            row["progress"] = j["progress"]
        if j.get("artifact_dir"):
            row["artifact_dir"] = j["artifact_dir"]
        if j.get("fail_reason"):
            row["fail_reason"] = j["fail_reason"]
        if j["status"] == "failed" and j.get("rc") in (130, 137, 143):
            row["killed"] = j["rc"]
        jobs.append(row)
    lane = {
        "alive": bool(raw.get("online")), "pid": None, "state_dir": None, "logs_dir": None,
        "paused": None, "managed": True, "waiting": None, "held": None, "held_t": None,
        "dep": None, "head": None, "bad": {}, "stuck": None, "cpu_wait": None,
        "par": 1, "running_n": sum(j["status"] == "running" for j in jobs),
        "ephemeral": True, "instance_id": raw.get("instance_id"),
    }
    return lane, jobs


QUEUE_PARSERS = {"gpu_queue": parse_gpu_queue, "ephemeral": parse_ephemeral_queue}


# ---------------- 主体 ----------------
def _group_of(name: str, groups: list[dict], auto: bool = False) -> str | None:
    for g in groups:
        if ("prefix" in g and name.startswith(g["prefix"])) or ("match" in g and re.search(g["match"], name)):
            return g["id"]
    if auto:
        m = AUTO_GROUP_RE.match(name)
        if m:
            return m.group(0)                       # 自动分组的 id 即前缀本身（如 x5_），不会与配置里的 id 撞
    return None


def _last_act(q: dict, raw: dict) -> float:
    """车道最后一次活动（日志、锁、完成标记；没有为 0，0 任务未启动清单不取登记时刻，避免误触 no_work:）：收起判定与“暂时没实验”起点同一口径。"""
    return float(raw.get("activity") or max((raw.get("marks") or {}).values(), default=0) or 0)


def _pool_rec(pool: dict | None, q: dict, hcfg: dict | None = None) -> dict:
    """按池 id + 清单键在 pool.lists 里查找对应待放记录（隔离跨共享盘域同名路径），兼顾旧两段键与单池默认名。"""
    if not pool:
        return {}
    root, jobs, host = q.get("root"), q.get("jobs"), q.get("host")
    pid = ((hcfg or {}).get(host) or {}).get("pool_id") or pool_id_of(host)
    if pid:
        r = pool.get(PO.key(root, jobs, pid))
        if r is not None:
            return r
    rk = PO.key(root, jobs)
    if rk in pool:
        return pool[rk]
    for v in pool.values():
        if isinstance(v, dict) and v.get("pool_host") == host and PO.key(v.get("root"), v.get("jobs")) == rk:
            return v
    return {}


def _sleeping_of(q: dict, raw: dict | None, pool: dict | None = None) -> bool:
    """这条队列此刻是不是已休眠（#164）：在用主机有探针现场结果时只认现场（raw.sleeping / raw.worker.sleeping），
    不因 worker 状态过期（raw.worker 为 None）误回退旧快照的 reg_sleeping；算力池看 pool.lists[].sleeping；其余才退回 reg_sleeping。"""
    if is_pool_id(q.get("host")):
        if _pool_rec(pool, q).get("sleeping"):
            return True
        return bool(raw.get("sleeping")) if raw is not None else bool(q.get("reg_sleeping"))
    if raw is not None:
        return bool(raw.get("sleeping") or (raw.get("worker") or {}).get("sleeping"))
    return bool(q.get("reg_sleeping"))


def _quiet(q: dict, raw: dict | None, returned: bool, t: float, pool: dict | None = None) -> bool:
    """不是 monitor.toml 写明的车道（只在登记里有的、通配符自动发现的）收不收起（#117、#164）：
    - 这一轮没有这条清单的数据（刚登记、探针还没读到）：不收；
    - 探针已确认休眠短路（raw.sleeping）或已归还且确认全跑完短路（readonly_done）：立即收起；
    - 队列进程还在跑（已归还无代读除外）：不收；
    - 清单里还有在跑或排队的任务（现场读到的待办优先于任何旧休眠标记）：不收；
    - 其余情况下若已休眠（含算力池休眠）：立即收起；
    - 探针正文读取失败（jobs_err）：不误判为空清单或已跑完而收起；
    - 0 个任务的空清单：不出车道，立即收起；
    - 已摘登记（worker rm，reg_live 为 False）且无未完成任务：立即收起；
    - 已归还主机上没有未完成也没有失败任务：立即收起；
    - 其余（在用主机上登记还在、有任务的，或有失败任务的，或自动发现的）：最后一次活动早于 hide_done_hours 的收起。"""
    if raw is None:
        return False
    if raw.get("sleeping") or raw.get("readonly_done"):
        return True
    if raw.get("lock_alive") and not (returned and not q.get("read_via")):
        return False
    jobs = QUEUE_PARSERS[q["type"]](raw, t)[1]
    if any(j["status"] in ("running", "queued") for j in jobs):
        return False
    if _sleeping_of(q, raw, pool):
        return True
    if raw.get("jobs_err"):
        return False
    if "jobs" in raw and not jobs:
        return True
    if q.get("reg") and not q.get("reg_live", True):
        return True
    if returned and "jobs" in raw and not any(j["status"] == "failed" for j in jobs):
        return True
    act = _last_act(q, raw)
    return not act or t - act > q["hide_done_hours"] * 3600


def _is_done_lane(q: dict, raw: dict | None, returned: bool, t: float) -> bool:
    """不是 monitor.toml 写明的车道是否已全成功跑完（无在跑、排队、失败，锁不在）：不占“车道 N”编号并在项目页折起（#164）。"""
    if not (q.get("reg") or q.get("discover")) or raw is None:
        return False
    if raw.get("lock_alive") and not (returned and not q.get("read_via")):
        return False
    jobs = QUEUE_PARSERS[q["type"]](raw, t)[1]
    return bool(jobs) and all(j["status"] == "done" for j in jobs)


def _lane_specs(cfg: dict, raw_by_host: dict, t: float, pool: dict | None = None) -> tuple[list[dict], float, list[dict]]:
    """配置里的队列 → 车道规格、收起的车道里最晚的一次活动（_no_work 用；没有收起的为 0）、收起的只在登记里有的车道 [{host, root, queue, last, sleeping}]
    （快照的 quiet：项目「管理」抽屉逐条清单里不列这些，#121、#164；last 是最后一次活动，没有为 0；sleeping：worker 已转休眠）。
    通配符队列按探针发现的清单展开；展开出的与只在登记里有的车道按 _quiet 收起，monitor.toml 写明的一直显示。
    车道是一台主机上的一条队列（一次一行；清单写了 `# labmon: par=N` 的同时跑至多 N 行，#147），不绑定实验（跑什么看任务归的计划项）。车道名在这里定：
    同一台主机上照配置顺序叫“车道 1”“车道 2”，写了 label（用途）的接在后面；收起的不占号，24 小时内全成功跑完的登记/自动发现车道标 done_lane 也不占号（叫“已跑完”）。页面、告警都用它。"""
    returned = {h["id"]: h["returned"] for h in cfg["hosts"]}
    out, last, hid = [], 0.0, []

    def keep(q: dict, raw: dict | None) -> None:
        nonlocal last
        ret = bool(returned[q["host"]])
        if _quiet(q, raw, ret, t, pool):
            act = _last_act(q, raw) if raw else 0.0
            last = max(last, act)
            if q.get("reg"):
                hid.append({"host": q["host"], "root": q["root"], "queue": q["jobs"], "last": round(act), "sleeping": _sleeping_of(q, raw, pool)})
        else:
            out.append((q, _is_done_lane(q, raw, ret, t)))
    for q in cfg["queues"]:
        found = ((raw_by_host.get(q.get("read_via") or q["host"]) or {}).get("queues") or {})
        if q.get("reg"):
            keep(q, found.get(q["id"]))
        elif not q["discover"]:
            out.append((q, False))
        else:
            for qid in sorted(k for k in found if k.startswith(q["id"] + ":")):
                keep(dict(q, id=qid), found[qid])
    n: dict = {}
    res = []
    for q, done_lane in out:
        if done_lane:
            res.append(dict(q, name="已跑完" + (f" {q['label']}" if q.get("label") else ""), done_lane=True))
        else:
            n[q["host"]] = n.get(q["host"], 0) + 1
            res.append(dict(q, name=f"车道 {n[q['host']]}" + (f" {q['label']}" if q.get("label") else "")))
    return res, last, hid


def _sync_returned_done(cfg: dict, raw_by_host: dict, sd: Path, t: float) -> None:
    """已归还主机经在用主机代读（read_via）的登记队列：全量读到后若确认无未完成无失败（立即收起），把清单 file_sig 与活动时刻记进 reg_lanes.json，
    后续探针只 stat 清单文件短路；若清单变动有了新行则清除 done_sig，及时报 returned_unfinished（#164）。"""
    f = sd / "reg_lanes.json"
    if not f.exists():
        return
    mem = load_json(f, {}) or {}
    returned = {h["id"]: bool(h["returned"]) for h in cfg["hosts"]}
    changed = False
    for q in cfg["queues"]:
        if not (q.get("reg") and q.get("read_via") and returned.get(q["host"])):
            continue
        k = list_path(q.get("root"), q.get("jobs"))
        m = mem.get(k)
        if not isinstance(m, dict) or m.get("host") != q["host"]:
            continue
        raw = ((raw_by_host.get(q["read_via"]) or {}).get("queues") or {}).get(q["id"])
        if raw is None or raw.get("readonly_done") or raw.get("jobs_err"):
            continue
        sig = raw.get("jobs_sig")
        jobs = QUEUE_PARSERS[q["type"]](raw, t)[1]
        done = not any(j["status"] in ("running", "queued", "failed") for j in jobs)
        if done and sig:
            act = round(_last_act(q, raw))
            if m.get("done_sig") != sig or m.get("done_act") != act:
                mem[k] = dict(m, done_sig=sig, done_act=act)
                changed = True
        elif not done and "done_sig" in m:
            mem[k] = {kk: vv for kk, vv in m.items() if kk not in ("done_sig", "done_act")}
            changed = True
    if changed:
        save_json(f, mem)


KEEP_LONG_S = 30 * 86400


def _long_term(state: dict, snap: dict, hist: list, cfg: dict, t: float) -> None:
    """时间线看长期要的几样（存 30 天）：采集在跑的区间（没采的时段页面标灰，不当成主机空闲），
    各主机每小时的平均 GPU 利用率（逐次采样只留 history_hours）：吃满程度，即逐次采样的 busy（worker 的近 10 分钟平均，只有保活时按 0，#51）
    再按小时平均；worker 不在、没有 busy 的那几次不记。11-17 之前的小时行是探针瞬时值、含保活，存满 30 天自然滚出。
    同时按小时平均 CPU 占用百分比（cpu_pct，#208）存 30 天供长窗口时间线叠画。"""
    gap = max(3 * cfg["interval"], 600)
    if "spans" not in state:                        # 第一次：用现有的逐次采样（history_hours 内）补出区间与每小时利用率
        state["spans"], state["util_h"], state["cpu_h"] = [], {}, {}
        for x in sorted((x for x in hist if x["t"] < t), key=lambda x: x["t"]):
            _add_util(state["util_h"], x.get("host"), x.get("busy"), x["t"])
            _add_hourly(state["cpu_h"], x.get("host"), x.get("cpu_pct"), x["t"])
            sp = state["spans"]
            if sp and x["t"] - sp[-1][1] <= gap:
                sp[-1][1] = round(x["t"])
            else:
                sp.append([round(x["t"]), round(x["t"])])
    elif "cpu_h" not in state:                      # #208 部署那次的迁移：旧 state.json 缺 cpu_h 时从已有 history 回填；各活跃项目采过一轮后（2026-12-27）可删
        state["cpu_h"] = {}
        for x in sorted((x for x in hist if x["t"] < t), key=lambda x: x["t"]):
            _add_hourly(state["cpu_h"], x.get("host"), x.get("cpu_pct"), x["t"])
    spans = state["spans"]
    if spans and t - spans[-1][1] <= gap:
        spans[-1][1] = round(t)
    else:
        spans.append([round(t), round(t)])
    state["spans"] = [s for s in spans if s[1] >= t - KEEP_LONG_S]
    snap["spans"] = state["spans"]
    uh, ch = state.setdefault("util_h", {}), state.setdefault("cpu_h", {})
    for h in snap["hosts"]:
        rec = next((x for x in reversed(hist) if x.get("host") == h["id"] and x["t"] == t), None)
        _add_util(uh, h["id"], (rec or {}).get("busy"), t)
        _add_hourly(ch, h["id"], (rec or {}).get("cpu_pct"), t)
    for k in uh:
        uh[k] = [r for r in uh[k] if r[0] >= t - KEEP_LONG_S]
    for k in ch:
        ch[k] = [r for r in ch[k] if r[0] >= t - KEEP_LONG_S]
    snap["util_hourly"] = {k: [[r[0], round(r[1] / r[2], 1)] for r in v] for k, v in uh.items()}
    snap["cpu_hourly"] = {k: [[r[0], round(r[1] / r[2], 1)] for r in v] for k, v in ch.items()}


def _add_hourly(store: dict, host: str | None, val: float | None, t: float) -> None:
    if not host or val is None:
        return
    rows = store.setdefault(host, [])
    hr = int(t // 3600 * 3600)
    if rows and rows[-1][0] == hr:
        rows[-1][1] += val
        rows[-1][2] += 1
    else:
        rows.append([hr, val, 1])


def _add_util(uh: dict, host: str | None, util: list | None, t: float) -> None:
    u = [v for v in util or [] if v is not None]
    if u:
        _add_hourly(uh, host, sum(u) / len(u), t)


def build(cfg: dict, probes: dict, out_dir: Path, lan: dict | None = None, glob_snap: dict | None = None) -> dict:
    """项目快照。probes：各主机的 (探针结果, 错误)；lan：lan.gather 给的内网状态，不在内网时远程主机这轮没探（沿用上次的数据，不计连不上）；
    glob_snap：工作台的全局快照（collect 读好的，不给就读一次）。
    步骤：主机（_hosts）→ 采样历史（_history）→ 队列到车道与任务（_lanes）→ 计划项（_groups）→ 主机级告警（_host_alerts）→ 暂时没实验（_no_work）。
    状态（进度采样、挂起、分段、自动归组的配色、各主机的连通状态）在 out_dir/.labmon/state.json，这里读一次、最后写一次。"""
    t = time.time()
    sd = out_dir / ".labmon"
    sd.mkdir(parents=True, exist_ok=True)
    state = load_json(sd / "state.json", {})
    hist_path = sd / "history.jsonl"
    hist = [h for h in load_rows(hist_path) if t - h.get("t", 0) <= cfg["history_hours"] * 3600]

    snap: dict = {"schema": SCHEMA, "id": cfg["id"], "title": cfg["title"], "generated": t, "refresh": cfg["refresh"], "interval": cfg["interval"], "hosts": [], "lanes": [], "jobs": [],
                  "groups": [], "alerts": [], "markers": cfg["markers"]}

    glob_snap = snapshot() if glob_snap is None else glob_snap   # 工作台的全局快照（上一轮的）：借来的主机现在怎样（borrow.check 算好的）、同机别的项目在 worker 上跑什么
    raw_by_host = _hosts(cfg, probes, lan is not None and not lan["ok"], glob_snap, snap, state, hist, sd, t)
    _history(cfg, snap, state, hist, hist_path, t)
    snap["gpu_mine"] = gpu_mine(cfg["id"], [h["id"] for h in snap["hosts"]], glob_snap, t)
    snap["rebalance"] = (glob_snap.get("rebalance") or {}).get(cfg["id"])    # 挪法建议（rebalance.suggest 在工作台那一轮算好的）：_lane 的 borrow_cut 与 _rebalance 用
    # 算力池里这个项目待放的清单为什么还没放、从何时起等（全局快照 pool.lists）：池车道的等待原因与 pool_wait 告警用
    snap["pool"] = {k: v for k, v in ((glob_snap.get("pool") or {}).get("lists") or {}).items() if v.get("project") == cfg["id"]}
    ratios, quiet = _lanes(cfg, snap, state, raw_by_host, t)
    _sync_returned_done(cfg, raw_by_host, sd, t)
    _groups(cfg, snap, state, ratios)
    _host_alerts(cfg, snap, state, hist, glob_snap, t)
    _ephemeral_alerts(cfg, snap, raw_by_host, t)
    _no_work(cfg, snap, glob_snap, quiet, t)
    state.pop("idle_regs", None)                     # #164 已改由 worker 自动休眠，删除旧 idle_regs: 状态字段
    _storage(cfg, snap, glob_snap, t)
    _rebalance(snap)
    _wait_gone(cfg, snap, out_dir, t)
    snap["thresholds"] = {"stale_s": stale_limit(cfg["interval"], cfg["alerts"]["stale_min"])}      # 页面据此标“数据过期”，与 labmon wait 同一门槛
    save_json(sd / "state.json", state)
    return snap


def _last_probe(path: Path) -> dict | None:
    """上一次存的探针结果（last_probe_*.json：掉线、已归还时沿用）。#147 之前存的队列 current 是单个对象（一条清单只有一个在跑的），
    读到就一次迁成列表、写回（已归还又没有代读的主机一直读这份，不迁会一直是旧形状）。#147 部署后各项目采过一轮就可删这段迁移。"""
    p = load_json(path, None)
    qs = [q for q in ((p or {}).get("queues") or {}).values() if isinstance(q, dict) and isinstance(q.get("current"), dict)]
    for q in qs:
        q["current"] = [q["current"]]
    if qs:
        save_json(path, p)
    return p


def _host_cpu(cg: dict, prev: dict | None, t: float, w_entry: dict | None) -> dict | None:
    """一台主机的 CPU 实时占用摘要（项目快照 hosts[].cpu，#208）：
    优先复用工作台全局快照里新鲜的 worker 状态（fleet.worker_fresh：内网、未掉线且 worker_up，读 info.cpu_used / cpu_quota / nproc / press.thr 与 throttle），
    没有 worker 或状态不新鲜时退回探针 cgroup 的 cpu_usage_ns 差分与 cpu_quota。
    占用百分比统一按 0–100% 封顶：有容器配额时 min(100, used / quota * 100)，无配额时按可用核数（nproc）换算。"""
    st = w_entry["status"] if worker_fresh(w_entry) else None
    info = (st or {}).get("info") or {}
    used = info.get("cpu_used")
    if used is None and prev and cg.get("cpu_usage_ns") and t > prev["t"]:
        used = max(0.0, (cg["cpu_usage_ns"] - prev["cpu_ns"]) / 1e9 / (t - prev["t"]))
    quota = info.get("cpu_quota") if st else cg.get("cpu_quota")
    cpus = info.get("nproc") if st else None
    denom = quota if quota and quota > 0 else (cpus if cpus and cpus > 0 else None)
    pct = round(min(100.0, max(0.0, float(used) / float(denom) * 100.0)), 1) if used is not None and denom else None
    thr = (info.get("press") or {}).get("thr") if st else None
    th = st.get("throttle") if st else None
    if used is None and quota is None and cpus is None and thr is None:
        return None
    out: dict = {"used": round(float(used), 2) if used is not None else None,
                 "quota": round(float(quota), 2) if quota is not None else None,
                 "cpus": int(cpus) if cpus else None,
                 "pct": pct}
    if thr is not None:
        out["thr"] = round(float(thr), 1)
    if th:
        out["throttled"] = True
        out["throttle_alert"] = bool(th.get("alert"))
        if th.get("for") is not None:
            out["throttle_for"] = th["for"]
    return out


def _hosts(cfg: dict, probes: dict, lan_off: bool, glob_snap: dict, snap: dict, state: dict, hist: list, sd: Path, t: float) -> dict:
    """各主机：探针结果到 snap["hosts"]（GPU、cgroup、挂载点、自定义检查），连不上的沿用上次存的 last_probe_*.json 并标过期，
    已归还的只留一张标注卡片；这一轮的采样追加到 hist；连通状态（link_step）与磁盘余量的告警。返回各主机的探针原始结果（车道按它读队列）。"""
    hstate, alerts = state.setdefault("hosts", {}), snap["alerts"]
    raw_by_host = {}
    w_hosts = {e["id"]: e for e in glob_snap.get("hosts", [])}
    lent = {e["id"]: e["borrow"] for e in glob_snap.get("hosts", []) if e.get("borrow")}   # 借来的主机：没有就用主机清单里记的
    busy = {e["id"]: e["busy"] for e in glob_snap.get("hosts", []) if e.get("busy")}       # 各卡的吃满程度（工作台那一轮 overview.gpu_busy 算好的，#51）
    for h in cfg["hosts"]:
        p, err = probes[h["id"]]
        hs = hstate.setdefault(h["id"], {})           # 连通状态（health.link_step 记的 fails / since / down）与 idle_since
        last_path = sd / f"last_probe_{re.sub(r'[^A-Za-z0-9_.-]', '_', h['id'])}.json"
        if h.get("pool"):
            # 算力池（#24）：一张小卡，没有 GPU 与挂载点；连没连上看代读池车道的那台（q 的 read_via）
            rv = next((q["read_via"] for q in cfg["queues"] if q["host"] == h["id"] and q.get("read_via")), None)
            snap["hosts"].append({"id": h["id"], "label": h["label"], "pool": True, "ok": bool(rv) and probes.get(rv, (None,))[0] is not None,
                                  "error": None if rv else "没有在用的自用主机代读池里的清单"})
            raw_by_host[h["id"]] = None
            continue
        if h.get("reader_only"):
            # 只替算力池代读、不是本项目的主机：读到的只用来解析池车道，不进项目快照（主机卡、采样历史、告警都不要）
            if p is None:
                p = _last_probe(last_path)
            else:
                save_json(last_path, p)
            raw_by_host[h["id"]] = p
            continue
        if h.get("ephemeral"):
            # 推模式临时主机（#213）：数据来自服务端状态文件，不走 SSH/link_step/ssh_fail/disk_low
            online = bool(p and p.get("online"))
            hd = {"id": h["id"], "label": h["label"], "ephemeral": True, "ok": online,
                  "error": None if online else (p.get("down_reason") or err or "已离线" if p else err),
                  "instance_id": p.get("instance_id") if p else None}
            raw_by_host[h["id"]] = p
            rec = {"t": t, "host": h["id"], "ok": online}
            if p:
                gp = p.get("gpu") or []
                cg = p.get("cgroup") or {}
                hd.update(gpus=gp, gpu_procs=[], cgroup=cg, loadavg=None)
                if online:
                    rec.update(util=[g.get("util") for g in gp], busy=[g.get("util") for g in gp],
                               mem=[g.get("mem_used") for g in gp], ram=cg.get("mem_rss"))
                    cpu_pct = p.get("cpu_pct")
                    cpu_cnt = p.get("cpu_count")
                    if cpu_pct is not None:
                        rec["cpu_pct"] = round(min(100.0, max(0.0, float(cpu_pct))), 1)
                        used_c = round(rec["cpu_pct"] / 100.0 * cpu_cnt, 2) if cpu_cnt else None
                        hd["cpu"] = {"used": used_c, "quota": float(cpu_cnt) if cpu_cnt else None,
                                     "cpus": int(cpu_cnt) if cpu_cnt else None, "pct": rec["cpu_pct"]}
                        hd["cpu_pct"] = rec["cpu_pct"]
                hd["mounts"] = []
                for s in p.get("storage") or []:
                    tot_gb = float(s.get("total_gb") or 0.0)
                    av_gb = float(s.get("avail_gb") or 0.0)
                    kind = s.get("kind") or "local"
                    min_gb = EP.CLOUD_LOW_GB if kind == "cloud" else None
                    md = {"path": s["path"], "label": s.get("label") or s["path"], "kind": kind,
                          "total_gb": tot_gb, "avail_gb": av_gb,
                          "used_pct": max(0.0, min(100.0, (1.0 - av_gb / tot_gb) * 100.0)) if tot_gb > 0 else None,
                          "min_gb": min_gb}
                    hd["mounts"].append(md)
                    if online:
                        rec.setdefault("disk", {})[md["path"]] = round(av_gb, 2)
                        if kind == "cloud" and av_gb < EP.CLOUD_LOW_GB:
                            low = f"临时主机 {h['label']} 云盘 {md['label']} 仅剩 {av_gb:.2f} GiB（低于 {EP.CLOUD_LOW_GB:g} GiB）"
                            alerts.append(alert(
                                f"ephemeral_disk_low:{h['id']}:{md['path']}", "agent",
                                f"{low}：请及时清理云盘上旧 checkpoint 或已无用的归档，避免训练回写失败（严禁对 Google Drive 跑 du/find）",
                            ))
                hd["checks"] = []
            if online:
                hist.append(rec)
            snap["hosts"].append(hd)
            continue
        if h["returned"]:
            # 已归还：只留一张标注卡片；没有代读主机的队列沿用归还前最后一次采到的状态
            hs.update(fails=0, down=None)
            raw_by_host[h["id"]] = _last_probe(last_path)
            snap["hosts"].append({"id": h["id"], "label": h["label"], "ok": False, "returned": h["returned"], "error": "已归还"})
            continue
        # 掉线判定与工作台同一处（link_step：连续至少 2 次、持续 DOWN_S 秒连不上才算）；掉线的原因就是探针的错误
        if link_step(hs, None if lan_off and h["ssh"] else p is not None, t) == "down":
            hs["down"] = err
        hd: dict = {"id": h["id"], "label": h["label"], "ok": p is not None, "error": err, "borrow": lent.get(h["id"]) or h.get("borrow")}
        if p is None:
            p = _last_probe(last_path)            # 显示上一次的数据，并标明过期
            hd["stale"] = p is not None
            hd["last_ok"] = p["t"] if p else None
        else:
            save_json(last_path, p)
        raw_by_host[h["id"]] = p
        if hs.get("down"):
            alerts.append(alert(f"ssh_fail:{h['id']}", "user", f"{h['label']}：探针连续失败 {hs['fails']} 次（{fmt_time(hs['since'], date=False)} 起：{err}）"))
        rec = {"t": t, "host": h["id"], "ok": hd["ok"]}
        if p:
            gp = [dict(g) for g in (p.get("gpu") or [])]
            we = w_hosts.get(h["id"])
            wst = we.get("status") if we and we.get("worker_up") else None
            if wst and gp:
                wgs = {g.get("i"): g for g in (wst.get("info") or {}).get("gpus") or []}
                ru = set((wst.get("room") or {}).get("unacct") or ())
                for g in gp:
                    wg = wgs.get(g.get("index"))
                    if wg:
                        if wg.get("unacct_mb"):
                            g["unacct_mb"] = wg["unacct_mb"]
                        if wg.get("uuid") in ru:
                            g["unacct_paused"] = True
            cg = p.get("cgroup") or {}
            hd.update(gpus=gp, gpu_procs=p.get("gpu_procs") or [], cgroup=cg, loadavg=p.get("loadavg"))
            bi = {b["i"]: b for b in busy.get(h["id"]) or []}
            if bi:
                hd["busy"] = [bi.get(g.get("index"), {"i": g.get("index")}) for g in gp]     # 与 gpus 一一对应；页面上 utilText 出字
            if hd["ok"]:
                if any(b.get("m10") is not None for b in bi.values()):   # 各卡近 10 分钟的吃满程度（worker 刚起、样本不够时没有）：时间线主机行的浅底、利用率小图、每小时平均与 gpu_idle 都按它
                    rec["busy"] = [bi.get(g.get("index"), {}).get("m10") for g in gp]
                rec.update(util=[g.get("util") for g in gp], mem=[g.get("mem_used") for g in gp],
                           cpu_ns=cg.get("cpu_usage_ns"), cpu_quota=cg.get("cpu_quota"), ram=cg.get("mem_rss"))
                prev = next((x for x in reversed(hist) if x.get("host") == h["id"] and x.get("cpu_ns")), None)
                cpu = _host_cpu(cg, prev, t, w_hosts.get(h["id"]))
                if cpu:
                    hd["cpu"] = cpu
                    if cpu.get("pct") is not None:
                        rec["cpu_pct"] = cpu["pct"]
            hd["cpu_pct"] = rec.get("cpu_pct")
            # 磁盘
            hd["mounts"] = []
            for m in h["mounts"]:
                d = (p.get("mounts") or {}).get(m["path"])
                if not d:
                    continue
                md = {"path": m["path"], "label": m["label"], "total_gb": d["total"] / GIB, "avail_gb": d["avail"] / GIB,          # 探针给字节；页面与告警一律 GiB
                      "used_pct": d["used"] / d["total"] * 100 if d["total"] else None, "min_gb": m.get("min_gb")}
                hd["mounts"].append(md)
                if hd["ok"]:
                    rec.setdefault("disk", {})[m["path"]] = round(md["avail_gb"], 2)
                if hd["ok"] and ((m.get("min_gb") is not None and md["avail_gb"] < m["min_gb"]) or
                                 (m.get("max_used_pct") is not None and (md["used_pct"] or 0) > m["max_used_pct"])):
                    low = f"{h['label']} {m['label']} 只剩 {md['avail_gb']:.1f} GiB"
                    alerts.append(alert(f"disk_low:{h['id']}:{m['path']}", "both",
                                        f"{low}：先清本项目能重算的中间文件；删结果、旧 checkpoint 前先问用户，不是本项目的告诉用户、不动别人的文件"
                                        "（是谁占的：本地盘看工作台主机页“本地盘”一行，共享盘看组内服务器页「共享盘」；不跑 du、find）", low))
            # 自定义检查
            ck = {c["id"]: c for c in p.get("checks") or []}
            hd["checks"] = [{"id": c["id"], "label": c["label"], **{k: ck.get(c["id"], {}).get(k) for k in
                                                                   ("state", "out", "detail")}} for c in h["checks"]]
        hist.append(rec)
        snap["hosts"].append(hd)
    return raw_by_host


def _history(cfg: dict, snap: dict, state: dict, hist: list, hist_path: Path, t: float) -> None:
    """采样历史：写回 history.jsonl（只留 history_hours），长期的区间与每小时利用率（_long_term），快照里带近 24 小时的。"""
    write_atomic(hist_path, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in hist))
    _long_term(state, snap, hist, cfg, t)
    keys = ("t", "host", "ok", "util", "busy", "mem", "cpu_pct", "ram", "disk")
    snap["history"] = [{k: x[k] for k in keys if x.get(k) is not None} for x in hist if t - x["t"] <= 24 * 3600]


def _lanes(cfg: dict, snap: dict, state: dict, raw_by_host: dict, t: float) -> tuple[dict, float]:
    """队列 → 车道与任务（每条队列一次 _lane），然后收拾状态（只为运行中的任务留进度采样、挂起记录留 3 天、见过分段的留 200 个）、
    全部完成的预计时刻、失败尝试。返回各计划项同类任务的 实际/估时（_groups 写进计划项）与收起的车道里最晚的一次活动（_no_work 用）。"""
    work = {"prog": state.setdefault("progress", {}),                # 运行中任务的进度采样
            "segs": state.setdefault("segments", {}),                # 多段任务：各运行中任务当前这一段从何时、第几步开始
            "multi": set(state.get("multiseg", [])),                 # 见过分段（进度回退或总步数变）的分组与任务名
            "susp": state.setdefault("suspended", {}),               # 各任务被挂起的累计秒数：从耗时与速度里扣掉
            "ratios": {},                                            # 计划项 → {同类名: 实际/估时}，任务里汇，计划项里取
            "running": set(),                                        # 运行中任务的 车道/任务 键
            "hcfg": {h["id"]: h for h in cfg["hosts"]}}
    specs, quiet, snap["quiet"] = _lane_specs(cfg, raw_by_host, t, snap.get("pool"))
    for q in specs:
        _lane(q, cfg, snap, raw_by_host, work, t)
    for k in list(work["prog"]):                       # 只为运行中的任务保留进度采样
        if k not in work["running"]:
            del work["prog"][k]
    for k in list(work["susp"]):                       # 挂起记录留到任务结束后记完账（3 天足够）
        if t - work["susp"][k]["t"] > 3 * 86400:
            del work["susp"][k]
    busy = [ln for ln in snap["lanes"] if ln.get("unfinished")]
    snap["all_done_ts"] = max((ln["eta_ts"] for ln in busy if ln.get("eta_ts")), default=None)   # 有意暂停的车道没有 ETA，不计
    snap["attempts"] = _attempts(snap, cfg["id"], t)
    state["multiseg"] = sorted(work["multi"])[-200:]
    return work["ratios"], quiet


def _lane(q: dict, cfg: dict, snap: dict, raw_by_host: dict, work: dict, t: float) -> None:
    """一条队列：按它的类型解析成车道与任务（QUEUE_PARSERS），各任务的归组与时间（_job_times），车道的汇总（总数、完成、未完成、预计完成）；
    已归还主机的队列由别的主机代读或冻在归还前；失败、队列进程不在、借用约定挡住不会再放（borrow_cut）、归还后还有任务没跑完的告警。"""
    alerts, hcfg = snap["alerts"], work["hcfg"]
    src = q.get("read_via") or q["host"]                 # 实际读数据的主机（已归还的经共享盘由别的主机代读）
    hp = raw_by_host.get(src)
    raw = ((hp or {}).get("queues") or {}).get(q["id"])
    host_ok = next((x["ok"] for x in snap["hosts"] if x["id"] in (src, q["host"])), False)     # 只替池代读的主机不在快照里：看池那张卡
    returned = hcfg[q["host"]]["returned"]
    where = f"{hcfg[q['host']]['label']} {q['name']}"     # 告警里指明车道：主机名 + 车道名
    lane = {"id": q["id"], "label": q["name"], "host": q["host"], "type": q["type"], "stale": not host_ok and not returned and q["type"] != "ephemeral",
            **({"done_lane": True} if q.get("done_lane") else {})}
    if not q["discover"]:                                  # 页面按 root、queue 认 worker 事件（等待原因、队列结束）属于哪条车道
        lane.update(root=q.get("root"), queue=q.get("jobs"))
    if returned:
        lane.update(returned=returned, read_via=q.get("read_via"))
        if not q.get("read_via"):
            lane["frozen_at"] = hp["t"] if hp and hp.get("t") else None   # 归还前最后一次采到的时刻
    if raw is None:
        lane.update(missing=True, alive=False, total=0, done=0, unfinished=0)
        snap["lanes"].append(lane)
        return
    if returned and not q.get("read_via"):
        raw = dict(raw, lock_alive=False, worker=None, current=[dict(c, alive=False) for c in raw.get("current") or []])
    if q["discover"] and raw.get("discovered"):
        lane.update(root=raw["discovered"].get("root"), queue=raw["discovered"].get("jobs"))
    info, jobs = QUEUE_PARSERS[q["type"]](raw, t)
    if raw.get("fs_skew"):
        info["fs_skew"] = raw["fs_skew"]
    lane.update(info)
    if hcfg[q["host"]].get("pool"):                  # 算力池里待放的（#24）：没有 worker 管，等看板放到某台；不报队列进程不在
        pv = _pool_rec(snap.get("pool"), q, hcfg)     # 为什么还没放（全局快照 pool.lists）
        done = pv.get("done")                         # 池里跑完了的（pool.lists 的 done，#86）：不算在等、不写已等多久
        lane.update(pool=True, managed=True, alive=False, waiting=None if lane.get("paused") or done else POOL_WAIT + (f"：{pv['why']}" if pv.get("why") else ""),
                    pool_since=None if done else pv.get("since"))     # 从何时起在池里等（页面上写已等多久，#89）
    if lane.get("held") and not lane.get("paused") and not any(j["status"] == "running" for j in jobs):
        lane["paused"] = lane["waiting"]          # 被临时指令挡住（drain、本项目暂停、续租截止、约定归还）：按暂停算，不报空转（约定归还挡住的另报 borrow_cut）
    if lane.get("dep") not in (None, "wait"):    # 队首等的依赖跑失败了、找不到、成环：不改就一直不放行（labmon wait 与实验会话据此处理）
        # key 带队首与依赖状态（#129）：处理完这一个、下一个队首又等不来是新的一件，6 小时内也叫醒；同一个队首卡着时不变
        alerts.append(alert(f"dep:{q['id']}:{lane.get('head')}:{lane['dep']}", "agent", f"{where} 卡在依赖上：{lane['waiting']}"))
    if lane.get("cpu_wait"):                     # 队首一直因 CPU 被挡满 10 分钟（阈值在 worker）：只报给这条车道的项目
        alerts.append(alert(f"cpu:{q['id']}", "agent", f"{where} 因 CPU 被挡 {lane['cpu_wait'] // 60} 分钟：{lane['waiting']}"))
    lane["started"] = "activity" not in raw or raw["activity"] is not None   # 从没启动过（预先登记的队列）
    plans = {j["id"]: j.get("plan") for j in jobs}      # worker 推演里的这一条（_job_times 取走，_queued_times 还要按车道排）
    for j in jobs:
        j["lane"] = q["id"]
        away = j.pop("where", None)                    # current 里放行的主机（#95）：清单搬走前那台放的，那台接着跑完手上这几格
        if j["status"] == "running" and away and away != q["host"]:
            j["host"] = away                           # 运行中的这一格在哪台跑（#100）：车道仍按登记，项目页把它画到那台、标「在 X 上跑完」
        j["hold"] = is_hold(j["id"])
        j["group"] = None if j["hold"] else _group_of(j["id"], cfg["groups"], cfg["auto_groups"])
        _job_times(j, q["id"], cfg["id"], cfg["alerts"], work, alerts, t, where, fresh=host_ok)
        if j["status"] == "failed":
            art = j.get("artifact_dir")
            if q["type"] == "ephemeral":
                why = f"：{j['fail_reason']}" if j.get("fail_reason") else ""
                tail = f"；产物目录 {art}（确认旧实例已离线后可在新实例从 ckpt/latest.pt 续跑）" if art else ""
                alerts.append(alert(f"failed:{j['id']}", "agent",
                                    f"任务被终止（退出码 {j['killed']}）：{j['id']}（{where}）{why}{tail}" if j.get("killed")
                                    else f"任务失败：{j['id']}（{where}）{why}{tail}"))
            else:
                redo = f"要重跑请删除 {lane.get('state_dir') or '状态目录'}/{j['id']}.failed"
                alerts.append(alert(f"failed:{j['id']}", "agent",
                                    f"任务被终止（退出码 {j['killed']}）：{j['id']}（{where}）；{redo}" if j.get("killed")
                                    else f"任务失败：{j['id']}（{where}）；{redo}"))
    _queued_times(jobs, plans, t, cfg["id"], lane.get("par") or 1)
    snap["jobs"].extend(jobs)
    work["running"] |= {f"{q['id']}/{j['id']}" for j in jobs if j["status"] == "running"}
    unfinished = sum(j["status"] in ("running", "queued") for j in jobs)
    # 未完成里不用卡的（#73）：worker 推演里这一条用哪几张卡写的是 []（在跑的是开跑时分到的卡，排队的是推演里 place 选的；gpus=0 的不给卡）。
    # 推演里没有的（排不上、不归 worker 管、worker 不在）不算，照旧当要卡；gpu_idle 只数要卡的
    no_gpu = sum(j["status"] in ("running", "queued") and (plans.get(j["id"]) or {}).get("gpus") == [] for j in jobs)
    ends = [j.get("eta_ts") for j in jobs if j["status"] in ("running", "queued")]
    lane["holding"] = any((j["hold"] or j.get("suspended")) and j["status"] == "running" for j in jobs)
    lane["has_hold"] = any(j["hold"] and j["status"] in ("running", "queued") for j in jobs)
    lane["overrun"] = any(j.get("overrun_min") for j in jobs)     # 当前任务已超估时：本车道完成时间偏早
    lane.update(total=len(jobs), done=sum(j["status"] == "done" for j in jobs),
                failed=sum(j["status"] == "failed" for j in jobs), unfinished=unfinished, no_gpu=no_gpu,
                eta_ts=max(ends) if unfinished and None not in ends and not lane.get("paused") else None)   # 有推演不出的（stuck）就不写
    snap["lanes"].append(lane)
    pv = _pool_rec(snap.get("pool"), q, hcfg) if lane.get("pool") else {}
    if pv.get("since") and unfinished and not pv.get("done") and not lane.get("paused") and (pv.get("nofit") or t - pv["since"] >= PO.POOL_TELL_S):
        # 池里待放满 2 小时，或全池所有健康主机均放不下（nofit）立即报：一条清单一条，只给会话（池里排队是常态，不标红）。
        nofit = bool(pv.get("nofit"))
        alerts.append(alert(f"pool_wait:{cfg['id']}:{q['id']}" + (":nofit" if nofit else ""), "agent",
                            f"{q.get('jobs')} 在{hcfg[q['host']]['label']}里等了 {fmt_dur(t - pv['since'])}（{pv.get('why') or '还没放到哪台'}）。"
                            + ("池里此刻放不下它：要早点跑就登记到自己能用的主机" if nofit else "不急就接着等；要早点跑可以登记到自己能用的主机")
                            + "（labmon worker add <主机> ROOT JOBS --project <项目>，一条清单只归一台，它就出了池）"))
    if returned:
        if unfinished and not q.get("reg_pool"):
            left = f"{hcfg[q['host']]['label']} 已归还，{q['name']} 还有 {unfinished} 个任务没跑完"
            # key 带主机与归还时刻（#129）：搬到别的主机、那台又还掉是新的一件；归还期间不变
            alerts.append(alert(f"returned_unfinished:{q['id']}:{q['host']}:{int(returned)}", "both",
                                f"{left}：把这条清单 labmon worker add 到别的在用主机（车道跟着登记走，monitor.toml 不用改）",
                                f"{left}：可在项目的「管理」里换到别的主机，或由实验会话登记到别的主机"))
    elif lane.get("held") == "deadline" and unfinished and (bw := next(x for x in snap["hosts"] if x["id"] == q["host"]).get("borrow") or {}).get("late"):

        # 借来的主机上队首被约定归还挡住（borrow.late_jobs 认定约定归还是这台最早的截止）：这台不会再放它，叫醒会话换主机或请用户延长约定
        # （怎么办的那句取 borrow.late_do）；只读 worker 已有的状态，不改放行规则。归还后由上面的 returned_unfinished 接手，续租截止挡住的不报
        # 能挪到哪台、几点跑完（挪法建议里挪的有这条清单的）写在这条告警里，rebalance 不再为这批报一次（_rebalance）；
        # 带上建议时 key 加 :rebalance：先报过不带建议的（别的主机的 offers 晚一两轮才到），有了挪法再叫醒一次
        rb = snap.get("rebalance") or {}
        mv = rb.get("text") if f"{q['host']}|{lane.get('root')}|{lane.get('queue')}" in (rb.get("cut") or []) else None
        # 两边都要：会话那份写怎么办（borrow.late_do）与挪法；用户那份写人话（late_say：改约定或交给会话），挪法那半句不铺，「⇄ 可提前」图标悬停看（#40）
        head = f"{where} 还有 {unfinished} 个任务不会在这台放行：{lane['waiting']}"
        # key 带主机与这一次借用（借用起点，同 borrow: 的 ep，#109、#129）：换到另一台借来的又被挡是新的一件；不带截止（可能取对方租期，各轮差几秒）
        ep = f"{q['host']}:{round(bw['since'])}" if bw.get("since") else q["host"]
        alerts.append(alert(f"borrow_cut:{q['id']}:{ep}" + (":rebalance" if mv else ""), "both",
                            f"{head}；{bw.get('late_do')}" + (f"。可以这样挪：{mv}" if mv else ""), f"{head}；{bw.get('late_say')}"))
    elif host_ok and unfinished and not lane["alive"] and not lane["managed"] and lane["started"] and not lane.get("paused"):
        # key 带锁里的 PID（#129）：拉起后又挂是新的一件；没有锁文件时不带
        alerts.append(alert(f"lock_dead:{q['id']}" + (f":{lane['pid']}" if lane.get("pid") else ""), "agent", f"{where} 的队列进程不在（PID {lane.get('pid')}），还有 {unfinished} 个任务没跑"))


def _job_times(j: dict, lane_id: str, pid: str, al_cfg: dict, work: dict, alerts: list, t: float, where: str, fresh: bool = True) -> None:
    """一个任务的时间：预计时长（worker 的 duration，没有就清单估时）与同类的 实际/估时（汇到计划项）；跑完的实际耗时（不含挂起）；
    运行中的已用、进度与速度、预计结束（_segment_eta，退路是估时）、日志不更新与一直没有进度行（noprogress，where 是告警里的车道）的告警；排队的预计开始与结束按车道另算（_queued_times）。
    写了依赖（after=、afterany=）的带上推演里的 deps（[[项目, 任务(, "any")]]，页面写“等 X 跑完”、时间线画连线）；
    占位任务的预计结束也取 worker 推演的：写了依赖的是它等的任务预计结束里最晚的（worker 的 dep_end），时长来源写“等 X、Y 跑完”；
    没有推演（不归 worker 管）时按估时猜。
    fresh：这一轮读到了数据；探针失败、车道用的是上一次的（build 里的 last_probe）时为 False，已用时间与日志静默照常长，
    日志不更新与 noprogress 按它算不准，不报（主机掉线另有 ssh_fail；整个离开内网时 collect 另外整体过滤）。"""
    key = f"{lane_id}/{j['id']}"
    d, pl = j.pop("dur") or {}, j.pop("plan")   # 预计时长：worker 的 duration（同类历史、实际/估时比值），没有就用清单估时
    j["dur_min"] = d.get("min", j["est_min"])
    j["dur_src"] = d.get("src") or ("清单估时" if j["est_min"] is not None else None)
    if pl and pl.get("deps"):
        j["deps"] = pl["deps"]
        if j["hold"] and pl.get("t1"):
            j["dur_min"] = (pl["t1"] - pl["t0"]) / 60
            j["dur_src"] = "等 %s 跑完" % "、".join(d[1] if d[0] == pid else f"{d[0]} 的 {d[1]}" for d in pl["deps"])
    if d.get("k") is not None:                 # 各类任务的 实际/估时 汇到计划项（页面只看计划项的 ratios）
        x = work["ratios"].setdefault(j["group"], {}).setdefault(d["cls"], {"cls": d["cls"], "k": d["k"], "src": j["dur_src"], "open": False})
        x["open"] |= j["status"] in ("running", "queued")
    if j["status"] in ("done", "failed") and j.get("start_ts") and j.get("end_ts"):
        j["actual_min"] = (j["end_ts"] - j["start_ts"]) / 60
        sr = work["susp"].get(key)
        if sr and sr.get("start") == j["start_ts"] and sr["s"] > 0:
            j["actual_min"] = max(0.0, j["actual_min"] - sr["s"] / 60)   # 实际耗时不含挂起
    elif j["status"] == "running":
        sr = _suspend_track(work["susp"], key, j, t)
        el = max(1.0, t - j["start_ts"] - sr["s"])             # 已用时间不含挂起
        j["elapsed_min"] = el / 60
        pr = j.get("progress")
        samples, seg = _progress_samples(work["prog"], work["segs"], j, t, sr["s"])
        j["speed"] = _speeds(samples)
        if seg and seg["k"] > 1:
            work["multi"] |= {j["id"]} | ({j["group"]} if j["group"] else set())
        is_multi = j["id"] in work["multi"] or (j["group"] in work["multi"] if j["group"] else False) or bool(pr and pr[0] >= pr[1])
        floor = j["dur_min"]                   # 整个任务的时长兜底
        est = _segment_eta(pr, samples, seg, el, floor, is_multi, t, hold=j["hold"], paused=sr["s"] - (seg or {}).get("sus0", sr["s"]))
        if est:
            rem, j["eta_source"], rate = est
            if rate is not None:
                j["rate_per_min"] = rate
            if j["eta_source"] == "已超估时":
                j["overrun_min"] = (el - floor * 60) / 60
        elif j["hold"]:
            # 没写依赖、超了估时的占位任务同 worker 的 run_end：剩余按 overrun_left，不当“马上结束”
            e = (j["est_min"] or 0) * 60
            rem = (max(0.0, pl["t1"] - t) if pl and pl.get("t1") else overrun_left(el) if e and el > e and not j.get("deps")
                   else max(0.0, e - el))
            j["eta_source"] = j["dur_src"] if j.get("deps") else "暂停（等条件放行，时长按估时猜）"
        elif j["dur_min"] is not None:
            # 超了估时又看不出进度：剩余按 overrun_left（已用的 20%、至少 10 分钟，与 worker 的 run_end 同一口径），不写 0（#138）
            rem = j["dur_min"] * 60 - el if j["dur_min"] * 60 >= el else overrun_left(el)
            j["eta_source"] = j["dur_src"] if j["dur_min"] * 60 >= el else "已超估时"
            if j["dur_min"] * 60 < el:
                j["overrun_min"] = (el - j["dur_min"] * 60) / 60   # 页面据此显示“超估时 +N 分”，而不是停在 100%
        else:
            rem, j["eta_source"] = 0.0, "无估时"
        j["remaining_min"] = rem / 60
        j["eta_ts"] = t + rem
        # key 带这一次的开跑时刻（#129）：重跑后又卡住是新的一件；开跑时刻只是这一轮的现在（日志与 current 都没有）时不带，免得每轮变
        run = f":{int(j['start_ts'])}" if j.get("start_ts") and j["start_ts"] != t else ""
        if fresh and j.get("overrun_min") and not seg.get("seen") and not j.get("suspended") and el >= NOPROG_MIN_S:
            # 超了折算估时、这一次一直没见过进度行：看板估不出到哪了，让漏写进度行的项目自己暴露（#68）。占位任务不设 overrun_min
            alerts.append(alert(f"noprogress:{j['id']}{run}", "agent",
                                f"任务 {j['id']}（{where}）已跑 {fmt_dur(el)}，超出预计 {fmt_dur(j['overrun_min'] * 60)}，这一次的日志里一直没有进度行（PROGRESS k/N），"
                                "看板估不出它到哪了、何时跑完；先看它还在不在干活（工作台运行中任务展开看 CPU 与日志末尾），"
                                "还没开跑的同类任务先给脚本补上进度行（docs/worker.md「进度行」）"))
        if fresh and al_cfg["log_silent_min"] and j.get("log_mtime") and t - j["log_mtime"] > al_cfg["log_silent_min"] * 60:
            alerts.append(alert(f"log_silent:{j['id']}{run}", "agent", f"任务 {j['id']} 的日志已 {(t - j['log_mtime']) / 60:.0f} 分钟没更新"))


def _queued_times(jobs: list[dict], plans: dict, t: float, pid: str, par: int = 1) -> None:
    """一条车道上排队任务的预计开始与结束：取 worker 推演的（同机各项目一起排），再与这一轮对齐（#64）。
    推演用的在跑任务结束是看板上一轮写的 eta.json（整分钟取整），又是几分钟前算的；运行中的条却是这一轮按进度算的（_job_times），
    原样拼在一起，同一车道上前后两条就重叠或留缝。串行车道一次只跑一个，所以按推演顺序（在跑的在前）走一遍：
    推演里接着前一个开始的（t0 等于前一个推演的 t1：被它挡着）跟着前一个挪，可前可后；不是接着的（等资源、等依赖）不提前，
    只在会和前一个重叠时往后挪；都不早于现在（推演之后该开始还没开始的从现在算）。结束跟着开始挪，
    写了依赖的占位任务结束跟着它等的任务，不随它自己开始晚了往后挪：等的都是这条车道上还没跑完的（pid 是本项目、这一轮定下了结束）时，
    取它们这一轮的结束与它自己的开始里最晚的，往前拉时不留推演的旧结束（#86）；等别的车道、别的项目的，或等的在这条车道上已跑完
    （同名的可能在别处重跑，还会跑的盖过以前的结果，见 Worker.dep_state），这里不知道那边这一轮的结束，仍取 max(推演的结束, 开始)。不归 worker 管的队列没有推演，排队的不写预计时间。
    并行清单（par>1，或运行中调小了 par、在跑的还不止一行，#147）不对齐：几格怎么接、谁等谁只有 worker 的推演（simulate 的窗口）算得对，
    看板照搬推演的时刻，只把该开始还没开始的从现在算（结束跟着挪）。推演按看板每轮写的 eta.json 重算，落后的至多一轮采集。"""
    run = [j for j in jobs if j["status"] == "running"]
    queued = sorted((j for j in jobs if j["status"] == "queued" and plans.get(j["id"])), key=lambda j: plans[j["id"]]["t0"])
    if par > 1 or len(run) > 1:
        for j in queued:
            pl = plans[j["id"]]
            j["plan_start_ts"] = s = max(pl["t0"], t)
            j["eta_ts"] = pl["t1"] and (max(pl["t1"], s) if j["hold"] and j.get("deps") else pl["t1"] + s - pl["t0"])
            if pl.get("after"):
                j["after"] = pl["after"]
        return
    pt1, end = None, None          # 前一个：推演里的结束、这一轮定下的结束
    ends = {}                      # 这条车道上没跑完的：这一轮定下的结束
    for j in run:
        pt1, end = (plans.get(j["id"]) or {}).get("t1"), j.get("eta_ts")
        ends[j["id"]] = end
    for j in queued:
        pl = plans[j["id"]]
        s = pl["t0"] + (end - pt1 if pt1 is not None and end is not None and abs(pl["t0"] - pt1) <= 1 else 0)
        s = max(s, t, end or t)
        j["plan_start_ts"] = s
        if pl["t1"] and j["hold"] and j.get("deps"):
            same = all(d[0] == pid and ends.get(d[1]) for d in j["deps"])
            j["eta_ts"] = max([s] + [ends[d[1]] for d in j["deps"]]) if same else max(pl["t1"], s)
        else:
            j["eta_ts"] = pl["t1"] and pl["t1"] + s - pl["t0"]
        ends[j["id"]] = j["eta_ts"]
        if pl.get("after"):
            j["after"] = pl["after"]          # 等到谁跑完才轮到它（[[项目, 任务]]），时间线在空档上写
        pt1, end = pl["t1"], j["eta_ts"]

def _groups(cfg: dict, snap: dict, state: dict, ratios: dict) -> None:
    """计划项：配置的在前，自动归组的按首次出现排在后面（颜色记在状态里不随顺序变）；各项的状态、任务计数、实际/估时、预计完成。"""
    groups = cfg["groups"]
    ids = {g["id"] for g in groups}
    colors = state.setdefault("auto_group_colors", {})
    fixed = {g["color"] for g in groups}
    auto_groups = []
    for j in snap["jobs"]:
        gid = j["group"]
        if gid and gid not in ids:
            ids.add(gid)
            auto_groups.append({"id": gid, "label": gid + "*", "color": pick_color(colors, gid, fixed), "auto": True})
    for g in groups + auto_groups:
        js = [j for j in snap["jobs"] if j["group"] == g["id"]]
        st = {s: sum(j["status"] == s for j in js) for s in ("done", "failed", "running", "queued")}
        marked = False
        dw = g.get("done_when")
        if dw:
            try:
                marked = re.search(dw["pattern"], Path(dw["file"]).read_text(), re.M) is not None
            except OSError:
                pass
        if marked and not (st["running"] or st["queued"]):   # 队列里还有任务时以队列为准，勾选写错也不会误报完成
            status = "完成"
        elif st["running"]:
            status = "运行中"
        elif st["queued"]:
            status = "排队中"
        elif js:
            status = "有失败" if st["failed"] else ("跑完待汇总" if dw else "完成")
        else:
            status = "未排队"
        unfinished = [j for j in js if j["status"] in ("running", "queued")]
        ends = [j.get("eta_ts") for j in unfinished]
        snap["groups"].append({"id": g["id"], "label": g["label"], "color": g["color"], "status": status, "auto": g.get("auto", False),
                               "done_when": {"file": Path(dw["src"]).name, "pattern": dw["pattern"]} if dw else None,   # 待汇总时页面提示勾哪里：monitor.toml 里写的那个文件（读的是登记目录里的副本）
                               "total": len(js), **st, "ratios": sorted(ratios.get(g["id"], {}).values(), key=lambda x: x["cls"]),   # 各类任务的 实际/估时；open：这类还有没跑完的（页面的范围只算这些）
                               "eta_ts": max(ends) if unfinished and None not in ends else None})   # 与车道同一口径：有推演不出的就不写


def _host_alerts(cfg: dict, snap: dict, state: dict, hist: list, glob_snap: dict, t: float) -> None:
    """主机级告警：GPU 空转（还有任务未完成、利用率却一直低，写明各车道在做什么）、CPU 持续被限流且本项目有超核的任务、开跑核数给少给多（只给会话）、drain 挡住本项目的任务、空闲关机风险（when_idle 检查）。"""
    al_cfg, alerts, hstate = cfg["alerts"], snap["alerts"], state.setdefault("hosts", {})
    fits = {}                                     # (how, 类按什么认, 类) → [(运行中任务, cpu_fit, 在哪, 哪台)]：cpu_low:、cpu_high: 跨主机合并（_cpu_fit_alerts）
    for hd in snap["hosts"]:
        if not hd["ok"] or hd.get("ephemeral"):
            continue
        lanes = [ln for ln in snap["lanes"] if ln["host"] == hd["id"]]
        # 占位、暂停、队首在等依赖的车道不算：卡空着是在等别的任务（依赖等不来另有 dep 告警）；不用卡的任务（车道的 no_gpu，#73）不算：卡本来就不用
        gpu_left = {ln["id"]: ln.get("unfinished", 0) - ln.get("no_gpu", 0) for ln in lanes}
        unfinished = sum(gpu_left[ln["id"]] for ln in lanes if not ln.get("holding") and not ln.get("paused") and not ln.get("dep"))
        win = al_cfg["gpu_idle_min"] * 60
        recent = [x for x in hist if x.get("host") == hd["id"] and t - x["t"] <= win + 60 and x.get("ok")]
        # 每次采样看各卡近 10 分钟的吃满程度（busy：只有保活时按 0，#51），不看瞬时值：数据加载的空档不会误报，保活也挡不住；
        # 没有 worker 的主机（采样里没有 busy）只有探针的瞬时值
        hs = hstate.setdefault(hd["id"], {})
        # 外部活跃占卡默认整卡暂停放新任务（#114）：不报 gpu_idle 异常，免得反复叫醒 labmon wait
        all_unacct = bool(hd.get("gpus")) and all(g.get("unacct_paused") for g in hd["gpus"])
        idle_gpu = bool(unfinished and hd.get("gpus") and not all_unacct and recent and t - recent[0]["t"] >= win - 150 and
                        all(max([u or 0 for u in x.get("busy") or x.get("util") or [0]]) < al_cfg["gpu_idle_util"] for x in recent))
        used = bool(recent) and max([u or 0 for u in recent[-1].get("busy") or recent[-1].get("util") or [0]]) >= al_cfg["gpu_idle_util"]
        since = _idle_stretch(hs, idle_gpu, used, win, t)
        if idle_gpu:
            # 写明各车道当前在做什么，便于判断是有意让出 GPU 还是卡住了
            why = []
            for ln in lanes:
                if ln.get("unfinished") and not gpu_left[ln["id"]]:
                    continue                      # 只剩不用卡的任务的车道与 GPU 空着无关，不列
                j = next((x for x in snap["jobs"] if x["lane"] == ln["id"] and x["status"] == "running"), None)
                if ln.get("paused"):
                    why.append(f"{ln['label']} 已暂停")
                elif j is None:
                    why.append((f"{ln['label']} 等待中：{ln['waiting']}" if ln.get("waiting") else f"{ln['label']} 没有运行中的任务")
                               if ln.get("unfinished") else None)
                elif j.get("suspended"):
                    why.append(f"{j['id']} 被挂起")
                elif j.get("hold"):
                    why.append(f"{j['id']} 暂停中")
                elif j.get("log_mtime"):
                    why.append(f"{j['id']} 日志 {(t - j['log_mtime']) / 60:.0f} 分钟前更新")
            why = [w for w in why if w]
            alerts.append(alert(f"gpu_idle:{hd['id']}:{since}", "agent", f"{hd['label']}：还有 {unfinished} 个{'要用卡的' if any(ln.get('no_gpu') for ln in lanes) else ''}任务未完成，"
                                f"GPU 利用率（近 10 分钟平均，只有保活时按 0）已 {al_cfg['gpu_idle_min']} 分钟低于 {al_cfg['gpu_idle_util']:.0f}%"
                                + (f"（{'；'.join(why)}）" if why else "")))
        # CPU 被配额限流（#39）：worker 判的（Worker.throttle，门槛只在那边），持续满时本项目有超核的任务才报；key 带这一段的起点，同一段只叫醒一次
        st = next((e.get("status") for e in glob_snap.get("hosts", []) if e["id"] == hd["id"]), None)   # 工作台快照里这台 worker 的状态
        th = (st or {}).get("throttle")
        over = [o for o in (th or {}).get("over") or [] if o.get("project") == cfg["id"]] if (th or {}).get("alert") else []
        if over:
            who = "、".join(f"{o['name']} 实测 {o['cores']:g} 核（给 {o['give']} 核）" for o in over)
            thr = "" if th.get("thr") is None else f" {th['thr']:g}%"
            say = f"{hd['label']}：容器 CPU 被配额限流{thr}（已持续 {fmt_dur(th['for'])}），本项目超核的任务：{who}"
            alerts.append(alert(f"throttle:{hd['id']}:{th['since']}", "both",
                                f"{say}。没设的 OMP/MKL/OPENBLAS 线程数 worker 已缺省成 $LABMON_CPUS，仍超核的是别处写死了：查代码、.env 与清单行前缀里的线程数与进程数"
                                "（OMP_NUM_THREADS 等、torch.set_num_threads、DataLoader num_workers、进程池；按 nproc 取的是宿主机的核数），改成按 $LABMON_CPUS；"
                                "确实要更多核就在清单那一行写 cpus=，下次放行按它记账", say))
        # 开跑给的核数给少了、给多了（#46）：worker 判的（运行中任务的 cpu_fit，门槛只在 worker 的 cpu_fit），本项目的清单任务才报、只给会话；
        # 车道按清单认（目录 + 清单，list_path）：同一台上两条清单可以有同名任务。给少的加不了核（sug 为 None：已给到配额，或配额被同机别的任务与同类分完了，#112）不报。
        # 按类（cpu_fit 的 cls：tag，否则去种子号同名）跨主机合并、循环后一起报（#91）
        by_q = {list_path(ln["root"], ln["queue"]): ln for ln in lanes if ln.get("root") and ln.get("queue")}
        for r in (st or {}).get("running") or []:
            f, lane = r.get("cpu_fit"), by_q.get(list_path(r.get("root"), r.get("queue")))
            if not f or not f.get("sug") or r.get("kind") != "queue" or r.get("project") != cfg["id"] or lane is None:
                continue
            # 类连同来源（by：tag 还是去种子号的名字）认：tag 恰好等于另一类的名字时不合并（#99）。
            # 还没升级的 worker 不给 cls：按任务名各报一条（by 记 job；部署拉起新 worker 后就没有了）
            by, cls = (f.get("by") or "name", f["cls"]) if f.get("cls") else ("job", r["name"])
            fits.setdefault((f["how"], by, cls), []).append((r, f, f"{hd['label']} {lane['label']}", hd["label"]))
        # drain 是整台主机的（续租预备、跑完就还、别的会话手动下的）：挡住本项目的任务时报一次，免得会话一直睡着不知道
        dr = [ln for ln in lanes if ln.get("held") == "drain" and ln.get("unfinished")]
        if dr:
            ht = max((ln.get("held_t") or 0 for ln in dr), default=0)   # 这条 drain 下的时刻（#129）：撤了再下是新的一件
            alerts.append(alert(f"drain:{hd['id']}" + (f":{ht}" if ht else ""), "agent", f"{hd['label']} 不放新任务（{dr[0]['waiting']}），本项目还有 "
                                f"{sum(ln['unfinished'] for ln in dr)} 个任务在等；drain 管整台主机，可能是续租预备、跑完就还或别的会话下的，看工作台的临时指令"))
        # 空闲关机风险：卡上有进程、或有任务在跑就不算空闲。任务看本项目的车道，同机别的项目的经工作台快照里 worker 的状态看
        # （刚起来还没占卡的也算）；暂停占位不算，它不占卡、只能靠保活。
        ids = {ln["id"] for ln in lanes}
        wrun = (st or {}).get("running") or []
        busy = (any(ln.get("alive") for ln in lanes) or bool(hd.get("gpu_procs")) or any(not j.get("hold") for j in wrun)
                or any(g.get("unacct_paused") for g in hd.get("gpus") or [])
                or any(j["status"] == "running" and not j["hold"] and j["lane"] in ids for j in snap["jobs"]))
        hcfg = next(h for h in cfg["hosts"] if h["id"] == hd["id"])
        idle = [c for c, cd in zip(hcfg["checks"], hd.get("checks") or []) if c.get("when_idle") and cd.get("state") == "fail" and not busy]
        # 队列跑完到 worker 拉起保活之间有约半分钟空档，平台按空闲关机要几个小时：连续空闲 IDLE_GRACE_S 才报
        hs["idle_since"] = (hs.get("idle_since") or t) if idle else None
        for c in idle if hs["idle_since"] and t - hs["idle_since"] >= IDLE_GRACE_S else []:
            idle_say = f"{hd['label']}：{c['when_idle']}（卡上没有进程、也没有任务在跑）"
            alerts.append(alert(f"idle:{hd['id']}:{c['id']}", "both",
                                f"{idle_say}：先看本项目没跑的任务为什么没放（项目页车道状态、labmon worker status {hd['id']}），"
                                f"worker 不在就 labmon worker start {hd['id']}；保活由 worker 管，不自己 ka.sh start", idle_say))

    alerts.extend(_cpu_fit_alerts(fits))


def _idle_stretch(hs: dict, idle: bool, used: bool, gap: float, t: float) -> int | None:
    """GPU 空转的这一段从何时起（gpu_idle: 的 key 带它，#129）：条件第一次成立时记下（看板状态里这台的 gpu_idle.since）。
    idle 是整个窗口都低于门槛（_host_alerts 的条件），一个超线样本就让它假上 gpu_idle_min 分钟，不能拿它的否定判结束（Codex 审 #133）；
    所以只在 GPU 真用起来（used：最新一个采样过了门槛）时记 off，连续满 gap（gpu_idle_min）才算这一段结束，中途又有低于门槛的采样就清掉 off。
    门槛两侧短暂来回仍是同一段、同一个 key，labmon wait 不重复叫醒；GPU 真用起来过一阵再空转是新的一段，6 小时内也叫醒。
    返回此刻这一段的起点（不在空转为 None）。"""
    gi = hs.get("gpu_idle") or {}
    if gi.get("off") and t - gi["off"] >= gap:
        gi = {}
    if idle:
        gi = {"since": gi.get("since") or round(t)}
    elif gi and used:
        gi["off"] = gi.get("off") or round(t)
    elif gi:
        gi.pop("off", None)
    hs["gpu_idle"] = gi or None
    return gi.get("since") if idle else None


def _cpu_fit_alerts(fits: dict) -> list:
    """开跑核数给少、给多的告警（只给会话，#46）：同一个项目同一类（tag，否则去种子号同名）的合成一条，跨主机（#91）。
    key cpu_low:<来源>:<类>、cpu_high:<来源>:<类>（来源 tag 或 name，旧 worker 没给类的是 job:<任务名>）不带任务名：同类后面的任务接着被判出来时
    key 不变，消失后 6 小时内再出现不再叫醒（waits._new_alerts，#88）；带来源，tag 恰好等于另一类的名字时两类不共用去冗余记录（#99）。
    给少了的建议值 worker 已按各台的配额、同机别的任务已记的核与同类几条同时在跑封顶（cpu_budget，#112）：各台的值不同时按主机分写，
    并写出各台配额多少、别的任务记了多少（照取最大值写，在配额小的那台上照做会超）。
    fits 由 _host_alerts 收集：(how, 类按什么认, 类) → [(运行中任务, cpu_fit, 在哪, 哪台)]。"""
    out = []
    for (how, by, cls), xs in fits.items():
        if len(xs) == 1:
            subj = f"任务 {xs[0][0]['name']}（{xs[0][2]}）"
        else:                                     # 按主机车道列：本机 车道 1：a_s1、a_s2；别的主机 车道 2：a_s3
            who = "；".join(f"{w}：{'、'.join(x[0]['name'] for x in xs if x[2] == w)}" for w in dict.fromkeys(x[2] for x in xs))
            subj = f"{'同 tag' if by == 'tag' else '同类'} {cls} 的 {len(xs)} 个任务（{who}）"

        def rng(k):                               # 几个任务的平均核数、峰值：一样的写一个，否则写范围
            a, b = min(x[1][k] for x in xs), max(x[1][k] for x in xs)
            return f"{a:.1f}" if round(a, 1) == round(b, 1) else f"{a:.1f}–{b:.1f}"
        give = "、".join(f"{g:g}" for g in sorted({x[1]["give"] for x in xs}))
        if how == "low":
            best = {}                             # 各台建议值最大的那一份（同一台同类的封顶相同，give 不同时 2 倍不同）
            for x in xs:
                if x[3] not in best or x[1]["sug"] > best[x[3]]["sug"]:
                    best[x[3]] = x[1]
            sugs = {f["sug"] for f in best.values()}
            write = f"写 cpus={sugs.pop()}" if len(sugs) == 1 else "按主机写 " + "、".join(f"{h} 上 cpus={f['sug']}" for h, f in best.items())
            room = "；".join(_cpu_room_text(h if len(best) > 1 else "这台", f["room"]) for h, f in best.items() if f.get("room"))
            full = any(f.get("room") and f["sug"] >= f["room"]["per"] for f in best.values())   # 建议值已到每条最多能给的
            out.append(alert(f"cpu_low:{by}:{cls}", "agent",
                             f"{subj}近 10 分钟均用 {rng('avg')}/{give} 核且卡利用率 <50%：核数可能偏少。"
                             f"未开跑同类{write}" + (f"（{room}{'，已达上限' if full else ''}）" if room else "")
                             + "，按 $LABMON_CPUS 设线程，在跑不必停（见 alerts.md）"))
        else:
            sug = max(x[1]["sug"] for x in xs)
            hint = "、".join(f"{c:g}" for c in sorted({x[0]["cpus"] for x in xs if x[0].get("cpus") is not None})) or "?"
            out.append(alert(f"cpu_high:{by}:{cls}", "agent",
                             f"{subj}清单 cpus={hint}（给 {give} 核），1 分钟峰值仅 {rng('peak')} 核：写多了。"
                             f"同类跑满几次后自动按实测记账，一般不必改；想立即让出核可改为 cpus={sug} 或删 cpus=，在跑不必停（见 alerts.md）"))
    return out


def _cpu_room_text(where: str, rm: dict) -> str:
    """cpu_low: 里一台的核预算（worker 的 cpu_budget）：配额、同类几条同时在跑、别的任务已记的、labmon 之外占的、每条最多几核。"""
    bits = [f"{where}配额 {rm['cap']} 核" if where == "这台" else f"{where} 配额 {rm['cap']} 核"]
    if rm.get("same", 1) > 1:
        bits.append(f"同类 {rm['same']} 条同时在跑")
    if rm.get("other", 0) >= 0.1:
        bits.append(f"别的任务记了 {rm['other']:g} 核")
    if rm.get("out", 0) >= 0.1:
        bits.append(f"labmon 之外{'与给对方的预留' if rm.get('kept') else ''}占 {rm['out']:g} 核")
    return "，".join(bits) + f"，每条最多 {rm['per']} 核"


NO_WORK_WAKE_S = 86400   # 项目跑空后这么久内给实验会话那条 no_work:（只给会话，#43）


def _ephemeral_alerts(cfg: dict, snap: dict, raw_by_host: dict, t: float) -> None:
    """推模式临时主机专属告警（#213）：运行中失联/被回收（ephemeral_down，to=both）与同项目多实例重复 job_id（ephemeral_dup，to=agent）。"""
    alerts = snap["alerts"]
    for hd in snap["hosts"]:
        if not hd.get("ephemeral") or hd.get("ok"):
            continue
        p = raw_by_host.get(hd["id"]) or {}
        lost = p.get("lost_jobs") or []
        if not lost:
            continue
        names = "、".join(x["id"] for x in lost)
        arts = list(dict.fromkeys(x["artifact_dir"] for x in lost if x.get("artifact_dir")))
        art_s = f"；产物目录：{'、'.join(arts)}" if arts else ""
        why = hd.get("error") or "心跳超时"
        page_msg = f"临时主机 {hd['label']} 失联离线（{why}），中断任务：{names}{art_s}"
        agent_msg = f"{page_msg}。请确认旧实例已离线后，在新 Colab 实例挂载云盘并从产物目录的 ckpt/latest.pt 续跑"
        iid = p.get("instance_id") or "none"
        alerts.append(alert(f"ephemeral_down:{hd['id']}:{iid}", "both", agent_msg, page_msg))
    for jid, xs in sorted(EP.duplicate_jobs(cfg["id"], t).items()):
        hosts_s = "、".join(f"{x['label']}（{x['instance_id']}）" for x in xs)
        arts = list(dict.fromkeys(x["artifact_dir"] for x in xs if x.get("artifact_dir")))
        art_s = f"（产物目录 {'、'.join(arts)}）" if arts else ""
        alerts.append(alert(
            f"ephemeral_dup:{cfg['id']}:{jid}", "agent",
            f"同项目多个临时实例同时在跑任务 {jid}（{hosts_s}）{art_s}：存在云盘并发写入冲突风险，请核对并停止重复实例",
        ))


def _no_work(cfg: dict, snap: dict, glob_snap: dict, quiet: float, t: float) -> None:
    """暂时没实验：清单里没有待办，worker 上也没有本项目在跑、在等的（单元不进车道，经工作台快照看）。往清单里加任务就接着跑，不用做什么。
    起点取跑完的任务与收起的车道（quiet：_lane_specs 给的最后一次活动）里最晚的。
    这时主机的事（连不上、磁盘余量、空闲关机风险）不归它报：主机仍由工作台和在用它的项目盯着，没在用这台的会话不被叫醒。"""
    wjobs = [j for e in glob_snap.get("hosts", []) for k in ("running", "waiting") for j in (e.get("status") or {}).get(k) or []
             if j.get("project") == cfg["id"]]
    last = max([j.get("end_ts") or 0 for j in snap["jobs"]] + [quiet])
    snap["no_work_since"] = None if wjobs or any(ln.get("unfinished") for ln in snap["lanes"]) else last or t
    if snap["no_work_since"]:
        snap["alerts"] = [a for a in snap["alerts"] if a["key"].split(":")[0] not in ("ssh_fail", "disk_low", "idle")]
        # 跑空了叫醒实验会话一次（#43；维护者 2026-11-18 定只给会话、不推给用户）：key 带起点，这一段空着只报一次，加了任务再跑空才再报
        since = snap["no_work_since"]
        # 从没跑过任务的（刚登记）不算跑空：起点每轮都是现在，不报；空了一天以上的不再报（快照里的旧任务过了保留期，起点会往前挪）
        if last and t - since < NO_WORK_WAKE_S:
            snap["alerts"].append(alert(f"no_work:{round(since)}", "agent",
                                        f"项目从 {fmt_time(since)} 起没有活了：各车道跑空，也没有在等的任务。要接着跑就把下一批追加到各台的长期清单"
                                        f"（先 labmon worker fit <主机> <目录> <清单> --list --project {cfg['id']} 看能开几条车道）；确实做完了不用管"))


def dir_name(name: str) -> str:
    """项目根下一个目录在文字里的叫法（disk.project_storage 的 name）：「.」是项目根本层的文件，空串是还没拆开时的整个项目目录。页面上 dirName（page.html）同一口径。"""
    return "项目根下的文件" if name == "." else "整个项目目录" if not name else name


def _storage(cfg: dict, snap: dict, glob_snap: dict, t: float) -> None:
    """本项目的存储（disk.project_storage，取工作台上一轮的全局快照）进快照的 storage（项目页「本项目占用」、labmon disk 读它）；
    共享盘上本项目近 24 小时涨得快（burst，与按人的 burst: 同一条线）报 disk:<项目>，两边都给；
    单个任务跑的期间本项目目录净增超过 bigwrite_gb（big）报 bigwrite:<任务>:<结束时刻>，只给实验会话，结束 24 小时内报（#53）；
    并行的几个同项目任务用的是同一组扫描、量出来是同一个数，按 grp 合成一条（key 取最早结束的那个，_job_writes 定）。
    项目暂时没实验时照报：是项目自己的目录在涨，不是主机的事。"""
    st = snap["storage"] = DK.project_storage(cfg["id"], glob_snap)
    if st["burst"] and st["d24"]:
        top = max(st["dirs"], key=lambda d: d["d24"] or 0, default=None)
        head = (f"共享盘上本项目近 24 小时 +{gb(st['d24'])} GiB" + (f"（{dir_name(top['name'])} +{gb(top['d24'])} GiB）" if top and top["d24"] > 0 else "")
                + (f"，共享盘剩 {gb(st['free'])} GiB，按这个速度约 {st['free'] / st['d24']:.1f} 天占满" if st.get("free") else ""))
        snap["alerts"].append(alert(f"disk:{cfg['id']}", "both",
                                    head + "：能重算的缓存放 $LABMON_CACHE（用 needs= 声明），不再用的中间结果清掉；删结果、旧 checkpoint 前先问用户", head))
    for key, js in write_groups([j for j in st["jobs"] if j["big"] and t - j["t1"] < 86400]):
        snap["alerts"].append(alert(f"bigwrite:{key}", "agent", write_text(js) + "：能重算的缓存放 $LABMON_CACHE（用 needs= 声明），共享盘只留数据集、代码与结果"))
    seen_keys = {a["key"] for a in snap["alerts"]}
    for hid, sw in (st.get("standalone") or {}).items():
        root = sw.get("root") or "work"
        ak = f"disk_low:{hid}:{root}"
        if ak in seen_keys:
            continue
        dg = sw.get("disk_gate") or {}
        free, warn_gb, quota_gb, wb = sw.get("free"), sw.get("warn_gb"), sw.get("quota_gb"), sw.get("work_b")
        low = bool(dg.get("on")) or (free is not None and warn_gb and free < warn_gb * GIB) or (quota_gb and wb is not None and wb >= 0.85 * quota_gb * GIB)
        if not low:
            continue
        if dg.get("on"):
            short = f"{sw.get('label') or hid} 磁盘闸门已开启（暂停放新任务）"
            full = f"{sw.get('label') or hid}：{dg.get('why') or short}；可用 labmon worker clean {hid} --project {cfg['id']} 预览清理自管缓存，删 projects/ 旧产物前先问用户"
        elif quota_gb and wb is not None and wb >= 0.85 * quota_gb * GIB:
            short = f"{sw.get('label') or hid} 本地工作盘已占 {gb(wb)} / {quota_gb:g} GiB（达 85% 预警）"
            full = f"{short}：请运行 labmon disk 查看各目录与可回收项，先清理缓存（labmon worker clean {hid} --project {cfg['id']}），删旧产物前先问用户"
        else:
            short = f"{sw.get('label') or hid} 本地工作盘只剩 {gb(free)} GiB（低于 {warn_gb:g} GiB）"
            full = f"{short}：请运行 labmon disk 查看占用并清理缓存（labmon worker clean {hid} --project {cfg['id']}），低于停止线将暂停放新任务"
        snap["alerts"].append(alert(ak, "both", full, short))
        seen_keys.add(ak)


def gb(b: float) -> str:
    """共享盘上的量的文字（字节换 GiB 的数字，不带单位）：10 GiB 以内一位小数，再大取整（disk:、bigwrite: 告警与 labmon disk 共用；页面上 GB、dGB）。"""
    return f"{b / GIB:.1f}" if abs(b) < 10 * GIB else f"{b / GIB:.0f}"


def write_groups(jobs: list[dict]) -> list[tuple[str, list[dict]]]:
    """storage.jobs 按 grp 合成几组（并行的几个同项目任务用的是同一组扫描、量出来是同一个数，disk._job_writes 定 grp）：
    [(组的 key「任务:结束时刻」, 组里的任务按结束先后)]，新结束的组在前（bigwrite: 告警与 labmon disk 共用）。"""
    groups: dict = {}
    for j in jobs:
        groups.setdefault(j.get("grp") or f"{j['name']}:{j['t1']}", []).append(j)
    for js in groups.values():
        js.sort(key=lambda j: j["t1"])
    return sorted(groups.items(), key=lambda kv: -kv[1][-1]["t1"])


def write_text(js: list[dict]) -> str:
    """一组任务（write_groups 的一项）的写入量一句：谁跑的这段时间本项目在共享盘上净增多少、大头、同期还有几个本项目任务（bigwrite: 与 labmon disk 共用）。"""
    j, top = js[0], (js[0]["top"] or [None])[0]
    names = "、".join(x["name"] for x in js[:3]) + (f" 等 {len(js)} 个任务" if len(js) > 3 else "")
    more = max(x["with"] for x in js) - (len(js) - 1)
    return (f"{names} 跑的这段时间本项目在共享盘上净{'增' if j['b'] >= 0 else '减'} {gb(abs(j['b']))} GiB"
            + (f"，大头 {dir_name(top[0])} +{gb(top[1])} GiB" if top and top[1] > 0 else "")
            + (f"（同期还有 {more} 个本项目任务，一起算在内）" if more > 0 else ""))


def _rebalance(snap: dict) -> None:
    """多主机负载均衡的提示（snap["rebalance"]，rebalance.suggest 算的）：页面在“全部完成”下写那一句；有具体挪法时出
    rebalance:<项目>:<挪法> 告警叫醒实验会话（key 是 rebalance.move_key：同一挪法一直在时只叫醒一次，换目标、换清单、第一行换了再叫醒，#109）。
    这一轮已有带上这句的 borrow_cut 告警（_lane；挪的有被借用约定挡住的清单）就不再报；挪的清单没配进项目的车道、borrow_cut 不出时照报。
    挪不了的原因只写在页面上，不叫醒。整句只给实验会话（labmon wait、命令行）：to="agent"，页面上不算告警数、只收在折叠的「给实验会话的提示」里，
    「全部完成」旁标「⇄ 可提前」、悬停看（common.js 的 rebalMark；#40）。"""
    r = snap.get("rebalance")
    told = any(a["key"].startswith("borrow_cut:") and a["key"].endswith(":rebalance") for a in snap["alerts"])
    if r and r.get("key") and not (r.get("cut") and told) and not snap.get("no_work_since"):
        snap["alerts"].append(alert(r["key"], "agent", r["text"]))


def _wait_gone(cfg: dict, snap: dict, out_dir: Path, t: float) -> None:
    """别的会话想挂 wait 被拦下了（#142，waits.dup_refused）时出一条只给用户的 wait_dup:<项目>（被拦下后 1 小时内、正在等的会话还在时）。
    实验会话的 labmon wait 断了（#141，waits.gone_since）、项目还有活（不是暂时没实验）时，出一条只给用户的 wait_gone:<上次来等的时刻>：
    给会话的告警这时没人收。再来等一次就消失；key 带上次来等的时刻，回来后又断了是新的一件（普通用户的项目照 push_owner 推给主人一次）。"""
    from . import waits as WT                     # waits 经 overview 在上层 import 本模块，这里只能延迟 import
    r = WT.dup_refused(out_dir, t)
    if r:                                         # 一个项目只让一个会话等（#142）：别的会话想挂 wait 被拦下了，告诉用户
        snap["alerts"].append(alert(f"wait_dup:{cfg['id']}", "user",
                                    f"另有一个会话（{'账号 ' + r['a'] + '，' if r.get('a') else ''}{fmt_time(r['t'])}）想挂 labmon wait，被拦下了："
                                    "一个项目只让一个会话等，告警才不会分走。要改由那个会话负责实验，就在那边用 labmon wait --take；不负责实验的会话不用等"))
    last = WT.gone_since(out_dir, t)
    if last is None or snap.get("no_work_since"):
        return
    snap["alerts"].append(alert(f"wait_gone:{round(last)}", "user",
                                f"实验会话 {fmt_time(last)} 起没在挂 labmon wait：本项目还有任务没跑完，给会话的告警（失败、依赖卡住、借到卡等）送不到它。"
                                "回到负责这个项目的会话，让它重挂 labmon wait"))


def alert(key: str, to: str, msg: str, page: str | None = None) -> dict:
    """一条项目告警（#142）：key 稳定（labmon wait 按它只报一次）；to 给谁看（AUDIENCE）；msg 给实验会话（labmon wait、命令行）的那份，
    写会话照着做的操作；page 给用户（看板页面）的那份，写人话，只在 to="both" 且两份不同时给，缺省页面上也用 msg。"""
    if to not in AUDIENCE:
        raise ValueError(f"告警 {key} 的受众写错了：{to}")
    return {"key": key, "to": to, "msg": msg} | ({"page": page} if page is not None else {})


def user_alerts(alerts: list[dict], keys: bool = False) -> list:
    """页面上显眼列的告警文字（算告警数、进红框）：给用户的（to 不是 agent），文字取 page，没有就是 msg。
    只给实验会话的不列，页面上收在折叠的「给实验会话的提示」里（common.js 的 alertBox）。
    工作台的项目摘要（serve.proj_info）用它，页面上的 forUser（common.js）同一口径。keys：返回 (key, 文字)（推给项目主人按 key 去重，#167）。"""
    return [(a["key"], a.get("page") or a["msg"]) if keys else a.get("page") or a["msg"] for a in alerts if a.get("to") != "agent"]


MINE_WIN_S = 86400


def gpu_mine(pid: str, hids: list, glob_snap: dict, t: float) -> dict | None:
    """这个项目近 MINE_WIN_S 秒的平均 GPU 利用率（#51）：各任务所用卡的平均利用率（worker 的 util_mean：跑完的取工作台存的用量记录的 u，
    在跑的取全局快照 running[].use，worker 不在时只算到状态最后写的时刻），按 卡数 × 落在窗口里的秒数 加权。同卡有别的任务时 util_mean 是整卡合计。
    返回 {util 平均 %, card_s 卡·秒, n 任务数}；窗口里没有用卡的任务为 None。"""
    t0, acc, w, n = t - MINE_WIN_S, 0.0, 0.0, 0
    # 按 (主机, 任务, 开始时刻) 认一次运行：刚跑完的用量行已写、状态里还在 running 时只算一遍，用量行盖过在跑的（#54 审查）。
    # 在跑的算到现在；worker 不在（状态过期）的只算到状态最后写的时刻，停机后的时间不算
    one = {(e["id"], j.get("root"), j.get("queue"), j.get("name"), round(j.get("t0") or 0)): (len(j.get("gpus") or []), j.get("t0"), t if e.get("worker_up", True) else e["status"].get("t"),
                                                           (j.get("use") or {}).get("util_mean"))
           for e in glob_snap.get("hosts", []) if e["id"] in hids for j in (e.get("status") or {}).get("running") or [] if j.get("project") == pid}
    one.update({(h, r.get("root"), r.get("queue"), r.get("n"), round(r.get("s") or 0)): (len(r.get("g") or []), r.get("s"), r.get("t"), r.get("u"))
                for h in hids for r in load_rows(usage_file(h)) if r.get("p") == pid})
    for k, s0, s1, u in one.values():
        d = min(s1 or 0, t) - max(s0 or 0, t0)
        if k and u is not None and d > 0:
            acc, w, n = acc + u * k * d, w + k * d, n + 1
    return {"util": round(acc / w, 1), "card_s": round(w), "n": n} if w else None


def _attempts(snap: dict, pid: str, t: float) -> list[dict]:
    """失败的尝试（任务后来重跑成功也留着）：时间线画成细红条，解释那段时间车道上发生了什么。
    取工作台存的用量记录（state.usage_file：worker 跑完的任务一行，存 30 天），按主机、目录、清单对到本项目的车道。
    交回算力池的车道（#99）：车道主机是 pool，跑过的行记在实际跑的那几台下，各台都看；失败的尝试带上那台（host），
    跑完、失败的任务没有 host 的按最后一次的行补上那台，项目页把它们画在那台下面（#89：已经跑过的按实际跑的主机画）。"""
    out, rows, last = [], {}, {}
    for ln in snap["lanes"]:
        if not ln.get("root"):
            continue
        hids = usage_hosts() if ln.get("pool") else [ln["host"]]
        for h in hids:
            if h not in rows:
                rows[h] = load_rows(usage_file(h))
        mine = [(h, r) for h in hids for r in rows[h] if r.get("p") == pid and (r.get("root"), r.get("queue")) == (ln["root"], ln["queue"])]
        out += [{"name": r["n"], "lane": ln["id"], "t0": r["s"], "t1": r["t"], **({"host": h} if ln.get("pool") else {})}
                for h, r in mine if r.get("st") == "failed" and r["t"] >= t - KEEP_LONG_S]
        if ln.get("pool"):
            last.update({(ln["id"], r["n"]): h for h, r in sorted(mine, key=lambda x: x[1]["t"]) if r.get("st") in ("done", "failed")})   # 最后一次的那台
    for j in snap["jobs"]:
        if j["status"] in ("done", "failed") and not j.get("host") and (j["lane"], j["id"]) in last:
            j["host"] = last[(j["lane"], j["id"])]
    return out


def eta_by_progress(src: str | None) -> bool:
    """运行中任务的预计结束是不是真正按日志进度外推出来的（近期速度、进度、进度（停了一阵））：
    overview._to_worker 据此写进 eta.json 的 prog，worker 的 run_tot 只把这些算作“按进度估”，
    避免把没有进度行时看板回写的估时（开始 + 预计时长）当成按进度估的回声（#149）。"""
    return src in ("近期速度", "进度", "进度（停了一阵）")


def _segment_eta(pr, samples: list, seg: dict | None, el: float, floor: float | None, is_multi: bool, t: float,
                 hold: bool = False, paused: float = 0.0) -> tuple[float, str, float | None] | None:
    """运行中任务的剩余秒数：返回 (剩余, 来源, 速度 步/分钟) 或 None（交给估时、无估时等后备）。

    PROGRESS 只当作当前这一段的进度：有近期速度用近期速度，否则按这一段里第一个采样到最新采样的平均速度外推。
    第一段从第一个步数 > 0 的采样算起，不按任务开始时刻：开头加载、预热可能长时间不打进度（Issue #14：开头 102 分钟不打，
    按任务开始外推把预计结束推到 31～88 小时后），一行进度报出的步数也分不清是开头没做活还是做了没打。
    第一段只有一个步数 > 0 的采样时按 估时 − 已用；没有估时或已超估时按 overrun_left（已用的 20%、至少 10 分钟，与 worker 的
    run_end 同一口径），不按已用时间外推（Issue #17：开头静默比估时还长时同样出尖峰）。下一轮有了第二个采样就换成平均速度。
    返回的剩余都从 t 起算：按速度外推的（近期速度、进度）剩余步数是最后一个采样时的，从那个采样的时刻起算再扣掉到 t 的这段，
    日志隔一阵才打一行时预计结束不随采集轮次往后漂、新进度来了再跳回（#140；外推的结束已过还在跑时剩下的步数从现在起算，不写 0）；paused 是最后一个采样之后被挂起的秒数，不算在扣掉的这段里。
    多段任务（见过分段、同组任务分过段，或进度已到头仍在跑）剩余至少是 整个任务估时 − 已用，后面还没打进度的段不会被漏掉；
    刚换段还没有速度时也用它。进度停了（不打进度的段，如核对、审计）不再按比例外推，改用估时；没有估时才按平均速度（起点同上）。"""
    first = not seg or (seg["k"] == 1 and seg["i0"] == 0)
    gap = [b[0] - a[0] for a, b in zip(samples, samples[1:])]
    stale = (len(samples) >= 2 or not first) and bool(samples) and t - samples[-1][0] > max(600, 3 * (statistics.median(gap) if gap else 0))
    recent = None if stale else _recent_rate(samples, t)
    a = next((x for x in samples if x[1] > 0), None) if first else [seg["t0"], seg["i0"]]   # 平均速度的起点
    d = samples[-1][0] - a[0] if pr and a and samples and pr[0] > a[1] and samples[-1][0] > a[0] else None   # 平均速度的跨度（秒）
    # 第一段只有一个步数 > 0 的采样。采样修剪空了（日志 PROG_KEEP 以上没更新、进度还在）不算：那不是进度刚开始，交给估时等后备
    single = first and bool(pr) and pr[0] > 0 and d is None and bool(samples)
    over = (overrun_left(el), f"剩余按已用的 {OVER_FRAC:.0%}（进度刚开始）", None)   # 它又没有估时或已超估时
    out = None
    # 剩余步数是最后一个采样时的：按速度外推的从那时起算，换成从现在起的剩余（#140：不随轮次往后漂）；其间挂起的不算在跑。
    # 外推的结束已过、任务还在跑（最后一次进度之后变慢了）时不写 0（同 #138：预计结束总是“现在”，后面的任务每轮往后滑），
    # 剩下的步数按这个速度从现在起算
    since = max(0.0, t - samples[-1][0] - paused) if samples else 0.0
    left = lambda r: r - since if r > since else r   # noqa: E731
    if pr and recent:
        out = (left((pr[1] - pr[0]) / recent), "近期速度", recent * 60)
    elif pr and not stale:
        if d:
            out = (left(d * (pr[1] - pr[0]) / (pr[0] - a[1])), "进度", (pr[0] - a[1]) / d * 60)
        elif single:
            out = (floor * 60 - el, "估时（进度刚开始）", None) if floor and floor * 60 > el else over
    if out and is_multi and floor and floor * 60 - el > out[0]:
        out = (floor * 60 - el, "估时（多段任务，进度只算当前这段）", out[2])
    if out is None and pr and (stale or is_multi) and not hold:
        if floor:
            # 超了估时：剩余按 overrun_left，不写 0（否则预计结束总是“现在”，后面的任务每轮往后滑，#138）
            out = (floor * 60 - el, "估时（这段不打进度）" if stale else "估时（刚换段）", None) if floor * 60 >= el else (overrun_left(el), "已超估时", None)
        elif d:
            out = (d * (pr[1] - pr[0]) / (pr[0] - a[1]), "进度（停了一阵）", None)
        elif single:
            out = over
    return out


def _progress_samples(prog: dict, segs: dict, j: dict, t: float, sus: float = 0.0) -> tuple[list, dict | None]:
    """记下运行中任务这一次的进度（时刻取日志最后修改时间），返回 (这一段的全部采样 [[t, i, n], ...], 这一段的起点)。

    一个任务可以分几段各打各的 PROGRESS（如先训练一组、核对，再训练另一组）。进度回退或总步数变了就算换了一段：
    采样清空，段起点记为新段第一次看到的 (时刻, 步数)，第几段记在 k。这一次运行里见过进度行就记 seen（noprogress 告警看它）。
    sus 是这一次运行到此刻累计挂起的秒数（_suspend_track）：记下新采样时的那份 sus0，_segment_eta 据此扣掉最后一个采样之后挂起的时长。"""
    key = f"{j['lane']}/{j['id']}"
    ss = prog.setdefault(key, [])
    seg = segs.get(key)
    if not seg or seg.get("run") != j.get("start_ts"):
        if seg:                                                   # 新的一次运行（如失败后重跑）：第一段从任务开始算
            ss.clear()
        # 第一次见到这个任务：已有采样（升级前留下的）就从最早那个采样算起，它一定在当前这段里
        seg = segs[key] = ({"run": j.get("start_ts"), "t0": ss[0][0], "i0": ss[0][1], "k": 1} if ss else
                           {"run": j.get("start_ts"), "t0": t if j.get("start_ts") is None else j["start_ts"], "i0": 0, "k": 1})
    pr = j.get("progress")
    if pr or ss:
        seg["seen"] = True
    if pr:
        ts = j.get("log_mtime") or t
        if ss and (ss[-1][2] != pr[1] or pr[0] < ss[-1][1]):      # 总步数变了或进度回退：换了一段，重新采
            ss.clear()
            seg.update(t0=ts, i0=pr[0], k=seg["k"] + 1)
        if not ss or (ss[-1][1] != pr[0] and ts > ss[-1][0]):
            ss.append([ts, pr[0], pr[1]])
            seg["sus0"] = sus
    ss[:] = [x for x in ss if t - x[0] <= PROG_KEEP][-300:]
    return ss, seg


def _suspend_track(susp: dict, key: str, j: dict, t: float) -> dict:
    """累计运行中任务被挂起的时长。刚发现挂起时，从日志最后更新算起（挂起后日志不再写，最多多算一个日志间隔）；
    之后相邻两次采集都看到挂起，就把这段间隔记上。"""
    sr = susp.get(key)
    if not sr or sr.get("start") != j["start_ts"]:          # 新的一次运行（如失败后重跑）：重新计
        sr = susp[key] = {"start": j["start_ts"], "s": 0.0, "last": None, "t": t}
    if j.get("suspended"):
        if sr["last"] is not None:
            sr["s"] += t - sr["last"]
        elif j.get("log_mtime") and j["start_ts"] < j["log_mtime"] < t:
            sr["s"] += t - j["log_mtime"]
        sr["last"] = t
    else:
        sr["last"] = None
    sr["t"] = t
    return sr


def _speeds(ss: list) -> list:
    """相邻两次采样之间的速度（步 / 分钟），供页面画速度曲线。"""
    return [[b[0], round((b[1] - a[1]) / (b[0] - a[0]) * 60, 3)] for a, b in zip(ss, ss[1:]) if b[0] - a[0] > 20]


def _recent_rate(ss: list, t: float) -> float | None:
    """最近 RECENT 秒内的平均速度（步 / 秒）；跨度不足 5 分钟或没有前进时返回 None。"""
    rs = [x for x in ss if t - x[0] <= RECENT]
    if len(rs) >= 2 and rs[-1][0] - rs[0][0] >= 300 and rs[-1][1] > rs[0][1]:
        return (rs[-1][1] - rs[0][1]) / (rs[-1][0] - rs[0][0])
    return None


_JS_SLOT = re.compile(r"/\*__JS:([A-Za-z0-9_./-]+\.js)__\*/")


def _inline_js(html: str) -> str:
    """把模板里的 /*__JS:<相对路径>__*/ 替换成包内对应脚本原文（单文件渲染，file:// 打开也可用）。"""
    def _read(m: re.Match[str]) -> str:
        rel = m.group(1)
        p = (HERE / rel).resolve()
        if not p.is_relative_to(HERE) or not p.is_file():
            raise FileNotFoundError(f"找不到内联脚本：{rel}")
        return p.read_text(encoding="utf-8")
    return _JS_SLOT.sub(_read, html)


def fill(html: str, slot: str, data) -> str:
    """把页面里的 /*__<slot>__*/null 换成数据（第一处）：数据里的 < 一律写成 \\u003c，任务名、日志里的 </script>、<!-- 都不能让浏览器提前结束脚本。
    DATA 在渲染时填（render_page）；ME（看板账号，#160）由看板服务发页面时按请求填（serve.Handler._page）。"""
    return html.replace(f"/*__{slot}__*/null", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"), 1)


def render_page(tpl: str, data: dict, title: str | None = None) -> str:
    """把模板（page.html / overview.html / login.html）渲染成数据内联的静态页：先内联脚本（common.js 与 overview_js/*.js），再填数据。
    看板服务给普通用户现渲染去掉组内数据的工作台（users.view）也经它。"""
    html = fill(_inline_js((HERE / tpl).read_text(encoding="utf-8")), "DATA", data)
    if title:
        html = html.replace("<title>labmon</title>", f"<title>{_esc(title)}</title>", 1)
    return html


def write_page(tpl: str, data: dict, f: Path, title: str | None = None) -> Path:
    """render_page 渲染、原子写入 f（state.write_atomic）。"""
    write_atomic(f, render_page(tpl, data, title))
    return f


def render(snap: dict, out_dir: Path) -> Path:
    return write_page("page.html", snap, out_dir / "index.html", snap["title"])


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def collect(cfg: dict, out_dir: Path | None = None, use_ssh: bool = True) -> dict:
    """采一次、写快照与页面。out_dir 缺省是看板上这个项目的输出目录（projects.out_dir，看板服务与 labmon collect 写同一处）。
    备用时（服务租约在别的看板服务手里，#163）报 ConfigError：看板线程与 labmon collect 都经这里。"""
    LS.need("采集")
    out_dir = Path(out_dir or PJ.out_dir(cfg["id"])).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    glob_snap = snapshot()                             # 工作台上一轮的全局快照：本项目的登记（车道按它）、借来的主机、同机别的项目
    now = time.time()
    cfg = lanes_cfg(cfg, glob_snap, out_dir / ".labmon", now)
    ephs = EP.project_hosts(cfg["id"], now, hide_s=cfg["hide_done_hours"] * 3600)
    if ephs:
        ehs = list(cfg["hosts"])
        eqs = list(cfg["queues"])
        seen_h = {h["id"] for h in ehs}
        for rec in ephs:
            hid = rec["host_id"]
            if hid in seen_h:
                continue
            seen_h.add(hid)
            ehs.append({"id": hid, "label": rec.get("label") or hid, "ephemeral": True, "ssh": None, "returned": None,
                        "mounts": [], "checks": [], "shared": [], "worker_home": ""})
            eqs.append({"id": f"ephemeral:{hid}", "host": hid, "root": f"ephemeral/{hid}", "jobs": EP.EPHEMERAL_QUEUE,
                        "type": "ephemeral", "discover": False, "reg": True, "reg_live": True,
                        "hide_done_hours": cfg["hide_done_hours"], "read_via": None, "label": ""})
        cfg = dict(cfg, hosts=ehs, queues=eqs)
    live = {h["id"]: h for h in cfg["hosts"] if not h["returned"] and not h.get("pool") and not h.get("ephemeral")}     # 算力池与临时主机都不走 SSH 探测
    lan, probes = L.gather(live, lambda h: run_probe(cfg, h) if (use_ssh or not h["ssh"]) else (None, "未连接（--no-ssh）"),
                           lambda k: use_ssh and bool(live[k]["ssh"]))     # 本机不在内网：不连，也不算探针失败
    probes.update({h["id"]: (None, "已归还") for h in cfg["hosts"] if h["returned"]})     # 不再连接
    probes.update({h["id"]: (None, POOL_LABEL) for h in cfg["hosts"] if h.get("pool")})
    probes.update({h["id"]: EP.as_probe(h["id"], now) for h in cfg["hosts"] if h.get("ephemeral")})
    snap = build(cfg, probes, out_dir, lan, glob_snap)
    if not lan["ok"]:                                  # 数据是离开内网前的：仅过滤依赖内网旧探针的告警，保留在线临时主机的新鲜心跳告警（#213）
        eph_online_hosts = {h["id"] for h in snap["hosts"] if h.get("ephemeral") and h.get("ok")}
        lane_host = {ln["id"]: ln.get("host") for ln in snap["lanes"]}
        eph_online_jobs = {j["id"] for j in snap["jobs"] if lane_host.get(j.get("lane")) in eph_online_hosts}

        def _keep_offline(a: dict) -> bool:
            parts = a["key"].split(":")
            kind = parts[0]
            if kind in ("log_silent", "noprogress"):
                return len(parts) > 1 and parts[1] in eph_online_jobs
            if kind == "gpu_idle":
                return len(parts) > 1 and parts[1] in eph_online_hosts
            return kind != "ssh_fail"

        snap["alerts"] = [a for a in snap["alerts"] if _keep_offline(a)]
        snap["lan"] = lan
    save_json(out_dir / "snapshot.json", snap)
    render(snap, out_dir)
    return snap
