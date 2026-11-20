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
from . import lan as L
from . import lease as LS
from . import pool as PO
from . import projects as PJ
from .config import AUTO_GROUP_RE, GIB, HOLD_RE, POOL, POOL_LABEL, STALE_S, fmt_dur, fmt_time, parse_time, pick_color, stale_limit, with_queues
from .health import link_step
from .state import load_json, load_rows, save_json, snapshot, usage_file, write_atomic

HERE = Path(__file__).resolve().parent
SCHEMA = 2
PROG_KEEP = 6 * 3600           # 运行中任务的进度采样保留时长
RECENT = 30 * 60               # 按最近这么久的进度估速度
OVER_FRAC, OVER_MIN_S = 0.2, 600   # 剩余说不准时按已用的这么多比例估、至少这么多秒（overrun_left）
NOPROG_MIN_S = 30 * 60         # 一直没有进度行的任务（超估时）要已用这么久才报 noprogress（几分钟的短任务不打进度也无妨）
IDLE_GRACE_S = 5 * 60         # 保活与任务都不在要连续这么久才报空闲关机风险（避开任务之间、拉起保活前的空档）
# 告警给谁看（#142，维护者 11-10 定：很多告警是给实验会话的，不都在看板上显眼列）：
# user 要用户处理（只有用户能做，或机器出了要人看的事）；agent 只给实验会话（会话自己能处理）；both 两边都要（page 给用户写人话，msg 给会话写操作）
AUDIENCE = ("user", "agent", "both")
POOL_WAIT = "在算力池里等着放到某台"   # 池车道的等待原因（#24；放到哪台由看板定，C 之后会写具体原因）
REG_KEEP_S = 24 * 3600        # 登记没了（worker rm、快照里这轮没有）的车道再按上次见到的主机留这么久（同自动发现的 hide_done_hours 缺省）
IDLE_REG_S = 2 * 86400        # 收起的登记车道空了这么久，提示实验会话不再用的 worker rm（_idle_regs，#47）


# ---------------- 探针 ----------------
def probe_spec(cfg: dict, host: dict) -> dict:
    return {"mounts": [m["path"] for m in host["mounts"]],
            "checks": [{k: c[k] for k in ("id", "if", "cmd", "detail", "timeout") if k in c} for c in host["checks"]],
            # 本机的队列，以及已归还主机上由本机经共享盘代读的队列（readonly：只读文件，PID 属于别的机器，不查进程）；
            # 算力池里的（pool，#78）同样代读，但清单是刚从别处搬回池里的：那边手上在跑的还在跑，current 里放行的主机不是这台的照 #95 按在跑算
            "queues": [{k: v for k, v in q.items() if k not in ("label", "host", "read_via", "reg", "reg_t", "reg_live")} | ({"readonly": True} if q["read_via"] else {})
                       | ({"pool": True} if q["host"] == POOL else {})
                       for q in cfg["queues"] if (q["read_via"] or q["host"]) == host["id"]],
            # 所有主机上显式登记的清单：共享盘上别的主机登记过的，自动发现时也要跳过
            "explicit_jobs": sorted({os.path.join(q["root"], q["jobs"]) for q in cfg["queues"] if not q["discover"]}),
            # worker 的状态：它在管、空着等资源的队列没有锁，不算掉了，显示它说的等待原因；被临时指令挡住的按暂停算
            "worker_status": host["worker_home"] + f"/hosts/{host['id']}/status.json", "worker_stale": STALE_S,
            "host": host["id"]}                  # current 里放行的主机不是这台时探针不查 PID（清单刚搬来、那台还在跑，#95）


def _qpath(root: str | None, jobs: str | None) -> str:
    """认清单：目录 + 清单路径规整后拼起来（与探针 worker_view 同一口径）。"""
    return os.path.normpath(os.path.join(root or "", jobs or ""))


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
            k, rank = _qpath(q["root"], q["jobs"]), (True, q.get("added_t") or 0) if act else (False, q.get("added") or "")
            if k not in found or rank > found[k][0]:
                found[k] = (rank, x["host"], q)
    new = {k: {"id": (mem.get(k) or {}).get("id"), "host": hid, "root": q["root"], "jobs": q["jobs"], "first": (mem.get(k) or {}).get("first", t), "seen": t,
               "reg_t": q.get("added_t"), "reg_stop": q.get("stop")} for k, (_, hid, q) in found.items()}
    new.update({k: m for k, m in mem.items() if k not in new and t - m.get("seen", 0) <= REG_KEEP_S})
    out, used = [], set()
    for q in cfg["queues"]:
        k = None if q["discover"] else _qpath(q.get("root"), q.get("jobs"))
        used.add(k)
        out.append(dict(q, host=new[k]["host"], reg_stop=new[k].get("reg_stop")) if k in new else q)
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
        # reg：只在登记里有的车道，跑完久了收起（_quiet）；reg_t：登记时刻（在用主机上的，刚登记的空清单不马上收）；
        # reg_live：这一轮登记还在（登记没了、按 REG_KEEP_S 留着的为 False：已经 worker rm 了，_idle_regs 不再提示）
        out.append({"id": m["id"], "host": m["host"], "root": m["root"], "jobs": m["jobs"], "reg": True, "reg_t": m.get("reg_t"), "reg_live": k in found,
                    "reg_stop": m.get("reg_stop")})
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
                out.setdefault(q["project"], set()).add((x["host"], _qpath(q["root"], q["jobs"])))
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
        cmd = L.ssh_argv(host["ssh"], host["port"], known_hosts=host.get("known_hosts")) + [f"{host['python']} - {arg}"]
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
    cur = raw.get("current") or {}
    cur_name = cur.get("name") if cur.get("alive") else None
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
        elif name == cur_name:
            row["status"] = "running"
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
            "managed": raw.get("worker") is not None, "waiting": wk.get("wait"), "held": wk.get("held"), "dep": wk.get("dep"), "stuck": wk.get("stuck"),
            "cpu_wait": wk.get("cpu")}      # 队首一直因 CPU 被挡满 10 分钟（worker 定）后已挡的秒数
    return lane, jobs


QUEUE_PARSERS = {"gpu_queue": parse_gpu_queue}


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
    """车道最后一次活动（日志、锁、完成标记；只在登记里有的再取登记时刻 reg_t）：收起判定与“暂时没实验”的起点同一口径。没有为 0。"""
    return max(raw.get("activity") or 0, q.get("reg_t") or 0)


def _quiet(q: dict, raw: dict | None, returned: bool, t: float) -> bool:
    """不是 monitor.toml 写明的车道（只在登记里有的、通配符自动发现的）收不收起（#117）：清单里没有在跑与排队的任务、队列进程不在、
    最后一次活动（只在登记里有的再取登记时刻 reg_t）早于 hide_done_hours 的收起。worker 在管不单独算“在”：登记过的清单它都管着，跑完的也是；
    空着等资源的有排队任务，照样显示。这一轮没有这条清单的数据（刚登记、探针还没读到）不收。已归还主机上没有代读的，锁不算活着。"""
    if raw is None:
        return False
    if raw.get("lock_alive") and not (returned and not q.get("read_via")):
        return False
    if any(j["status"] in ("running", "queued") for j in QUEUE_PARSERS[q["type"]](raw, t)[1]):
        return False
    act = _last_act(q, raw)
    return not act or t - act > q["hide_done_hours"] * 3600


def _lane_specs(cfg: dict, raw_by_host: dict, t: float) -> tuple[list[dict], float, list[dict]]:
    """配置里的队列 → 车道规格、收起的车道里最晚的一次活动（_no_work 用；没有收起的为 0）、收起的只在登记里有的车道 [{host, root, queue, last, idle}]
    （快照的 quiet：项目「管理」抽屉逐条清单里不列这些，#121；last 是最后一次活动，没有为 0；idle：登记还在、空了 IDLE_REG_S 以上，_idle_regs 提示 worker rm，#47）。
    通配符队列按探针发现的清单展开；展开出的与只在登记里有的车道跑完久了收起（_quiet），monitor.toml 写明的一直显示。
    车道是一台主机上的一条串行队列，即一个并行位，不绑定实验（跑什么看任务归的计划项）。车道名在这里定：
    同一台主机上照配置顺序叫“车道 1”“车道 2”，写了 label（用途）的接在后面，收起的不占号。页面、告警都用它。"""
    returned = {h["id"]: h["returned"] for h in cfg["hosts"]}
    out, last, hid = [], 0.0, []

    def keep(q: dict, raw: dict | None) -> None:
        nonlocal last
        if _quiet(q, raw, bool(returned[q["host"]]), t):
            act = _last_act(q, raw)
            last = max(last, act)
            if q.get("reg"):
                hid.append({"host": q["host"], "root": q["root"], "queue": q["jobs"], "last": round(act), "idle": bool(q.get("reg_live")) and t - act >= IDLE_REG_S})
        else:
            out.append(q)
    for q in cfg["queues"]:
        found = ((raw_by_host.get(q.get("read_via") or q["host"]) or {}).get("queues") or {})
        if q.get("reg"):
            keep(q, found.get(q["id"]))
        elif not q["discover"]:
            out.append(q)
        else:
            for qid in sorted(k for k in found if k.startswith(q["id"] + ":")):
                keep(dict(q, id=qid), found[qid])
    n: dict = {}
    for i, q in enumerate(out):
        n[q["host"]] = n.get(q["host"], 0) + 1
        out[i] = dict(q, name=f"车道 {n[q['host']]}" + (f" {q['label']}" if q.get("label") else ""))
    return out, last, hid


KEEP_LONG_S = 30 * 86400


def _long_term(state: dict, snap: dict, hist: list, cfg: dict, t: float) -> None:
    """时间线看长期要的两样（存 30 天）：采集在跑的区间（没采的时段页面标灰，不当成主机空闲），
    各主机每小时的平均 GPU 利用率（逐次采样只留 history_hours）：吃满程度，即逐次采样的 busy（worker 的近 10 分钟平均，只有保活时按 0，#51）
    再按小时平均；worker 不在、没有 busy 的那几次不记。11-17 之前的小时行是探针瞬时值、含保活，存满 30 天自然滚出。"""
    gap = max(3 * cfg["interval"], 600)
    if "spans" not in state:                        # 第一次：用现有的逐次采样（history_hours 内）补出区间与每小时利用率
        state["spans"], state["util_h"] = [], {}
        for x in sorted((x for x in hist if x["t"] < t), key=lambda x: x["t"]):
            _add_util(state["util_h"], x.get("host"), x.get("busy"), x["t"])
            sp = state["spans"]
            if sp and x["t"] - sp[-1][1] <= gap:
                sp[-1][1] = round(x["t"])
            else:
                sp.append([round(x["t"]), round(x["t"])])
    spans = state["spans"]
    if spans and t - spans[-1][1] <= gap:
        spans[-1][1] = round(t)
    else:
        spans.append([round(t), round(t)])
    state["spans"] = [s for s in spans if s[1] >= t - KEEP_LONG_S]
    snap["spans"] = state["spans"]
    uh = state.setdefault("util_h", {})
    for h in snap["hosts"]:
        rec = next((x for x in reversed(hist) if x.get("host") == h["id"] and x["t"] == t), None)
        _add_util(uh, h["id"], (rec or {}).get("busy"), t)
    for k in uh:
        uh[k] = [r for r in uh[k] if r[0] >= t - KEEP_LONG_S]
    snap["util_hourly"] = {k: [[r[0], round(r[1] / r[2], 1)] for r in v] for k, v in uh.items()}


def _add_util(uh: dict, host: str | None, util: list | None, t: float) -> None:
    u = [v for v in util or [] if v is not None]
    if not host or not u:
        return
    rows = uh.setdefault(host, [])
    hr = int(t // 3600 * 3600)
    if rows and rows[-1][0] == hr:
        rows[-1][1] += sum(u) / len(u)
        rows[-1][2] += 1
    else:
        rows.append([hr, sum(u) / len(u), 1])


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
    # 算力池里这个项目待放的清单为什么还没放、从何时起等（pool.tick 在工作台那一轮写的，#25）：池车道的等待原因与 pool_wait 告警用
    snap["pool"] = {k: v for k, v in ((glob_snap.get("pool") or {}).get("lists") or {}).items() if v.get("project") == cfg["id"]}
    ratios, quiet = _lanes(cfg, snap, state, raw_by_host, t)
    _groups(cfg, snap, state, ratios)
    _host_alerts(cfg, snap, state, hist, glob_snap, t)
    _no_work(cfg, snap, glob_snap, quiet, t)
    _idle_regs(cfg, snap, state)
    _storage(cfg, snap, glob_snap, t)
    _rebalance(snap)
    snap["thresholds"] = {"stale_s": stale_limit(cfg["interval"], cfg["alerts"]["stale_min"])}      # 页面据此标“数据过期”，与 labmon wait 同一门槛
    save_json(sd / "state.json", state)
    return snap


def _hosts(cfg: dict, probes: dict, lan_off: bool, glob_snap: dict, snap: dict, state: dict, hist: list, sd: Path, t: float) -> dict:
    """各主机：探针结果到 snap["hosts"]（GPU、cgroup、挂载点、自定义检查），连不上的沿用上次存的 last_probe_*.json 并标过期，
    已归还的只留一张标注卡片；这一轮的采样追加到 hist；连通状态（link_step）与磁盘余量的告警。返回各主机的探针原始结果（车道按它读队列）。"""
    hstate, alerts = state.setdefault("hosts", {}), snap["alerts"]
    raw_by_host = {}
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
                p = load_json(last_path, None)
            else:
                save_json(last_path, p)
            raw_by_host[h["id"]] = p
            continue
        if h["returned"]:
            # 已归还：只留一张标注卡片；没有代读主机的队列沿用归还前最后一次采到的状态
            hs.update(fails=0, down=None)
            raw_by_host[h["id"]] = load_json(last_path, None)
            snap["hosts"].append({"id": h["id"], "label": h["label"], "ok": False, "returned": h["returned"], "error": "已归还"})
            continue
        # 掉线判定与工作台同一处（link_step：连续至少 2 次、持续 DOWN_S 秒连不上才算）；掉线的原因就是探针的错误
        if link_step(hs, None if lan_off and h["ssh"] else p is not None, t) == "down":
            hs["down"] = err
        hd: dict = {"id": h["id"], "label": h["label"], "ok": p is not None, "error": err, "borrow": lent.get(h["id"]) or h.get("borrow")}
        if p is None:
            p = load_json(last_path, None)            # 显示上一次的数据，并标明过期
            hd["stale"] = p is not None
            hd["last_ok"] = p["t"] if p else None
        else:
            save_json(last_path, p)
        raw_by_host[h["id"]] = p
        if hs.get("down"):
            alerts.append(alert(f"ssh_fail:{h['id']}", "user", f"{h['label']}：探针连续失败 {hs['fails']} 次（{fmt_time(hs['since'], date=False)} 起：{err}）"))
        rec = {"t": t, "host": h["id"], "ok": hd["ok"]}
        if p:
            gp = p.get("gpu") or []
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
                if prev and rec.get("cpu_ns") and t > prev["t"]:
                    rec["cpu_pct"] = max(0.0, (rec["cpu_ns"] - prev["cpu_ns"]) / 1e9 / (t - prev["t"])
                                         / (rec.get("cpu_quota") or 1) * 100)
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
    specs, quiet, snap["quiet"] = _lane_specs(cfg, raw_by_host, t)
    work["pool_wait"] = []
    for q in specs:
        _lane(q, cfg, snap, raw_by_host, work, t)
    if work["pool_wait"]:
        # 池里待放满 2 小时的清单：每个项目一条、列出各条（#88：key 不带清单与起点，清单回池重新计时、别的清单跟着等满都不再叫醒），只给会话：池里排队是常态，不标红
        snap["alerts"].append(alert(f"pool_wait:{cfg['id']}", "agent",
                                    f"有 {len(work['pool_wait'])} 条清单在算力池里等了 {PO.POOL_TELL_S // 3600} 小时以上：{'；'.join(work['pool_wait'])}。"
                                    "不急就接着等；要早点跑可以登记到自己能用的主机（labmon worker add <主机> ROOT JOBS --project <项目>，一条清单只归一台，它就出了池）"))
    for k in list(work["prog"]):                       # 只为运行中的任务保留进度采样
        if k not in work["running"]:
            del work["prog"][k]
    for k in list(work["susp"]):                       # 挂起记录留到任务结束后记完账（3 天足够）
        if t - work["susp"][k]["t"] > 3 * 86400:
            del work["susp"][k]
    busy = [ln for ln in snap["lanes"] if ln.get("unfinished")]
    snap["all_done_ts"] = max((ln["eta_ts"] for ln in busy if ln.get("eta_ts")), default=None)   # 有意暂停的车道没有 ETA，不计
    snap["attempts"] = _attempts(snap["lanes"], cfg["id"], t)
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
    lane = {"id": q["id"], "label": q["name"], "host": q["host"], "type": q["type"], "stale": not host_ok and not returned}
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
        raw = dict(raw, lock_alive=False, worker=None, current=dict(raw.get("current") or {}, alive=False))
    if q["discover"] and raw.get("discovered"):
        lane.update(root=raw["discovered"].get("root"), queue=raw["discovered"].get("jobs"))
    info, jobs = QUEUE_PARSERS[q["type"]](raw, t)
    if raw.get("fs_skew"):
        info["fs_skew"] = raw["fs_skew"]
    lane.update(info)
    if hcfg[q["host"]].get("pool"):                  # 算力池里待放的（#24）：没有 worker 管，等看板放到某台；不报队列进程不在
        pv = (snap.get("pool") or {}).get(PO.key(q.get("root"), q.get("jobs"))) or {}     # 为什么还没放（pool.tick，#25）
        done = pv.get("done")                         # 池里跑完了的（pool.lists 的 done，#86）：不算在等、不写已等多久
        lane.update(pool=True, managed=True, alive=False, waiting=None if lane.get("paused") or done else POOL_WAIT + (f"：{pv['why']}" if pv.get("why") else ""),
                    pool_since=None if done else pv.get("since"))     # 从何时起在池里等（页面上写已等多久，#89）
    if lane.get("held") and not lane.get("paused") and not any(j["status"] == "running" for j in jobs):
        lane["paused"] = lane["waiting"]          # 被临时指令挡住（drain、本项目暂停、续租截止、约定归还）：按暂停算，不报空转（约定归还挡住的另报 borrow_cut）
    if lane.get("dep") not in (None, "wait"):    # 队首等的依赖跑失败了、找不到、成环：不改就一直不放行（labmon wait 与实验会话据此处理）
        alerts.append(alert(f"dep:{q['id']}", "agent", f"{where} 卡在依赖上：{lane['waiting']}"))
    if lane.get("cpu_wait"):                     # 队首一直因 CPU 被挡满 10 分钟（阈值在 worker）：只报给这条车道的项目
        alerts.append(alert(f"cpu:{q['id']}", "agent", f"{where} 因 CPU 被挡 {lane['cpu_wait'] // 60} 分钟：{lane['waiting']}"))
    lane["started"] = "activity" not in raw or raw["activity"] is not None   # 从没启动过（预先登记的队列）
    plans = {j["id"]: j.get("plan") for j in jobs}      # worker 推演里的这一条（_job_times 取走，_queued_times 还要按车道排）
    for j in jobs:
        j["lane"] = q["id"]
        j["hold"] = bool(HOLD_RE.search(j["id"]))
        j["group"] = None if j["hold"] else _group_of(j["id"], cfg["groups"], cfg["auto_groups"])
        _job_times(j, q["id"], cfg["id"], cfg["alerts"], work, alerts, t, where, fresh=host_ok)
        if j["status"] == "failed":
            redo = f"要重跑请删除 {lane.get('state_dir') or '状态目录'}/{j['id']}.failed"
            alerts.append(alert(f"failed:{j['id']}", "agent",
                                f"任务被终止（退出码 {j['killed']}）：{j['id']}（{where}）；{redo}" if j.get("killed")
                                else f"任务失败：{j['id']}（{where}）；{redo}"))
    _queued_times(jobs, plans, t, cfg["id"])
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
    pv = (snap.get("pool") or {}).get(PO.key(q.get("root"), q.get("jobs"))) or {} if lane.get("pool") else {}
    if pv.get("since") and unfinished and not pv.get("done") and not lane.get("paused") and t - pv["since"] >= PO.POOL_TELL_S:
        work["pool_wait"].append(f"{q.get('jobs')} 等了 {fmt_dur(t - pv['since'])}（{pv.get('why') or '还没放到哪台'}）")   # 项目一条，_lanes 汇总
    if returned and (hcfg[q["host"]].get("borrow") or {}).get("pool") and PO._ours({"stop": q.get("reg_stop")}):
        pass                                         # 算力池借过的主机还掉了：池的清单由看板搬回池里（pool.tick），不叫会话换主机；借给池之前留下的旧登记池不收，照下面报（#88 E）
    elif returned:
        if unfinished:
            left = f"{hcfg[q['host']]['label']} 已归还，{q['name']} 还有 {unfinished} 个任务没跑完"
            alerts.append(alert(f"returned_unfinished:{q['id']}", "both",
                                f"{left}：把这条清单 labmon worker add 到别的在用主机（车道跟着登记走，monitor.toml 不用改）",
                                f"{left}：可在项目的「管理」里换到别的主机，或由实验会话登记到别的主机"))
    elif lane.get("held") == "deadline" and unfinished and (bw := next(x for x in snap["hosts"] if x["id"] == q["host"]).get("borrow") or {}).get("late") \
            and not bw.get("pool"):                  # 算力池借来的：看板自己会搬（pool.tick），不叫会话

        # 借来的主机上队首被约定归还挡住（borrow.late_jobs 认定约定归还是这台最早的截止）：这台不会再放它，叫醒会话换主机或请用户延长约定
        # （怎么办的那句取 borrow.late_do）；只读 worker 已有的状态，不改放行规则。归还后由上面的 returned_unfinished 接手，续租截止挡住的不报
        # 能挪到哪台、几点跑完（挪法建议里挪的有这条清单的）写在这条告警里，rebalance 不再为这批报一次（_rebalance）；
        # 带上建议时 key 加 :rebalance：先报过不带建议的（别的主机的 offers 晚一两轮才到），有了挪法再叫醒一次
        rb = snap.get("rebalance") or {}
        mv = rb.get("text") if f"{q['host']}|{lane.get('root')}|{lane.get('queue')}" in (rb.get("cut") or []) else None
        # 两边都要：会话那份写怎么办（borrow.late_do）与挪法；用户那份写人话（late_say：改约定或交给会话），挪法那半句不铺，「⇄ 可提前」图标悬停看（#40）
        head = f"{where} 还有 {unfinished} 个任务不会在这台放行：{lane['waiting']}"
        alerts.append(alert(f"borrow_cut:{q['id']}" + (":rebalance" if mv else ""), "both",
                            f"{head}；{bw.get('late_do')}" + (f"。可以这样挪：{mv}" if mv else ""), f"{head}；{bw.get('late_say')}"))
    elif host_ok and unfinished and not lane["alive"] and not lane["managed"] and lane["started"] and not lane.get("paused"):
        alerts.append(alert(f"lock_dead:{q['id']}", "agent", f"{where} 的队列进程不在（PID {lane.get('pid')}），还有 {unfinished} 个任务没跑"))


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
        samples, seg = _progress_samples(work["prog"], work["segs"], j, t)
        j["speed"] = _speeds(samples)
        if seg and seg["k"] > 1:
            work["multi"] |= {j["id"]} | ({j["group"]} if j["group"] else set())
        is_multi = j["id"] in work["multi"] or (j["group"] in work["multi"] if j["group"] else False) or bool(pr and pr[0] >= pr[1])
        floor = j["dur_min"]                   # 整个任务的时长兜底
        est = _segment_eta(pr, samples, seg, el, floor, is_multi, t, hold=j["hold"])
        if est:
            rem, j["eta_source"], rate = est
            if rate is not None:
                j["rate_per_min"] = rate
            if j["eta_source"] == "已超估时":
                j["overrun_min"] = (el - floor * 60) / 60
        elif j["hold"]:
            rem = max(0.0, pl["t1"] - t) if pl and pl.get("t1") else max(0.0, (j["est_min"] or 0) * 60 - el)
            j["eta_source"] = j["dur_src"] if j.get("deps") else "暂停（等条件放行，时长按估时猜）"
        elif j["dur_min"] is not None:
            rem = max(0.0, j["dur_min"] * 60 - el)
            j["eta_source"] = j["dur_src"] if j["dur_min"] * 60 >= el else "已超估时"
            if j["dur_min"] * 60 < el:
                j["overrun_min"] = (el - j["dur_min"] * 60) / 60   # 页面据此显示“超估时 +N 分”，而不是停在 100%
        else:
            rem, j["eta_source"] = 0.0, "无估时"
        j["remaining_min"] = rem / 60
        j["eta_ts"] = t + rem
        if fresh and j.get("overrun_min") and not seg.get("seen") and not j.get("suspended") and el >= NOPROG_MIN_S:
            # 超了折算估时、这一次一直没见过进度行：看板估不出到哪了，让漏写进度行的项目自己暴露（#68）。占位任务不设 overrun_min
            alerts.append(alert(f"noprogress:{j['id']}", "agent",
                                f"任务 {j['id']}（{where}）已跑 {fmt_dur(el)}，超出预计 {fmt_dur(j['overrun_min'] * 60)}，这一次的日志里一直没有进度行（PROGRESS k/N），"
                                "看板估不出它到哪了、何时跑完；先看它还在不在干活（工作台运行中任务展开看 CPU 与日志末尾），"
                                "还没开跑的同类任务先给脚本补上进度行（docs/worker.md「进度行」）"))
        if fresh and al_cfg["log_silent_min"] and j.get("log_mtime") and t - j["log_mtime"] > al_cfg["log_silent_min"] * 60:
            alerts.append(alert(f"log_silent:{j['id']}", "agent", f"任务 {j['id']} 的日志已 {(t - j['log_mtime']) / 60:.0f} 分钟没更新"))


def _queued_times(jobs: list[dict], plans: dict, t: float, pid: str) -> None:
    """一条车道上排队任务的预计开始与结束：取 worker 推演的（同机各项目一起排），再与这一轮对齐（#64）。
    推演用的在跑任务结束是看板上一轮写的 eta.json（整分钟取整），又是几分钟前算的；运行中的条却是这一轮按进度算的（_job_times），
    原样拼在一起，同一车道上前后两条就重叠或留缝。一条队列一次只跑一个，所以按推演顺序（在跑的在前）走一遍：
    推演里接着前一个开始的（t0 等于前一个推演的 t1：被它挡着）跟着前一个挪，可前可后；不是接着的（等资源、等依赖）不提前，
    只在会和前一个重叠时往后挪；都不早于现在（推演之后该开始还没开始的从现在算）。结束跟着开始挪，
    写了依赖的占位任务结束跟着它等的任务，不随它自己开始晚了往后挪：等的都是这条车道上还没跑完的（pid 是本项目、这一轮定下了结束）时，
    取它们这一轮的结束与它自己的开始里最晚的，往前拉时不留推演的旧结束（#86）；等别的车道、别的项目的，或等的在这条车道上已跑完
    （同名的可能在别处重跑，还会跑的盖过以前的结果，见 Worker.dep_state），这里不知道那边这一轮的结束，仍取 max(推演的结束, 开始)。不归 worker 管的队列没有推演，排队的不写预计时间。"""
    run = [j for j in jobs if j["status"] == "running"]
    queued = sorted((j for j in jobs if j["status"] == "queued" and plans.get(j["id"])), key=lambda j: plans[j["id"]]["t0"])
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
    fits = {}                                     # (how, 类) → [(运行中任务, cpu_fit, 在哪, 类按什么认)]：cpu_low:、cpu_high: 跨主机合并（_cpu_fit_alerts）
    for hd in snap["hosts"]:
        if not hd["ok"]:
            continue
        lanes = [ln for ln in snap["lanes"] if ln["host"] == hd["id"]]
        # 占位、暂停、队首在等依赖的车道不算：卡空着是在等别的任务（依赖等不来另有 dep 告警）；不用卡的任务（车道的 no_gpu，#73）不算：卡本来就不用
        gpu_left = {ln["id"]: ln.get("unfinished", 0) - ln.get("no_gpu", 0) for ln in lanes}
        unfinished = sum(gpu_left[ln["id"]] for ln in lanes if not ln.get("holding") and not ln.get("paused") and not ln.get("dep"))
        win = al_cfg["gpu_idle_min"] * 60
        recent = [x for x in hist if x.get("host") == hd["id"] and t - x["t"] <= win + 60 and x.get("ok")]
        # 每次采样看各卡近 10 分钟的吃满程度（busy：只有保活时按 0，#51），不看瞬时值：数据加载的空档不会误报，保活也挡不住；
        # 没有 worker 的主机（采样里没有 busy）只有探针的瞬时值
        if unfinished and hd.get("gpus") and recent and t - recent[0]["t"] >= win - 150 and \
                all(max([u or 0 for u in x.get("busy") or x.get("util") or [0]]) < al_cfg["gpu_idle_util"] for x in recent):
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
            alerts.append(alert(f"gpu_idle:{hd['id']}", "agent", f"{hd['label']}：还有 {unfinished} 个{'要用卡的' if any(ln.get('no_gpu') for ln in lanes) else ''}任务未完成，"
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
        # 车道按清单认（目录 + 清单，_qpath）：同一台上两条清单可以有同名任务。给少的已给到配额（sug 为 None）写大也给不了，不报。
        # 按类（cpu_fit 的 cls：tag，否则去种子号同名）跨主机合并、循环后一起报（#91）
        by_q = {_qpath(ln["root"], ln["queue"]): ln for ln in lanes if ln.get("root") and ln.get("queue")}
        for r in (st or {}).get("running") or []:
            f, lane = r.get("cpu_fit"), by_q.get(_qpath(r.get("root"), r.get("queue")))
            if not f or not f.get("sug") or r.get("kind") != "queue" or r.get("project") != cfg["id"] or lane is None:
                continue
            # 还没升级的 worker 不给 cls：按任务名各报一条（部署拉起新 worker 后就没有了）
            fits.setdefault((f["how"], f.get("cls") or r["name"]), []).append((r, f, f"{hd['label']} {lane['label']}", f.get("by")))
        # drain 是整台主机的（续租预备、跑完就还、别的会话手动下的）：挡住本项目的任务时报一次，免得会话一直睡着不知道
        dr = [ln for ln in lanes if ln.get("held") == "drain" and ln.get("unfinished")]
        if dr:
            alerts.append(alert(f"drain:{hd['id']}", "agent", f"{hd['label']} 不放新任务（{dr[0]['waiting']}），本项目还有 "
                                f"{sum(ln['unfinished'] for ln in dr)} 个任务在等；drain 管整台主机，可能是续租预备、跑完就还或别的会话下的，看工作台的临时指令"))
        # 空闲关机风险：卡上有进程、或有任务在跑就不算空闲。任务看本项目的车道，同机别的项目的经工作台快照里 worker 的状态看
        # （刚起来还没占卡的也算）；暂停占位不算，它不占卡、只能靠保活。
        ids = {ln["id"] for ln in lanes}
        wrun = (st or {}).get("running") or []
        busy = (any(ln.get("alive") for ln in lanes) or bool(hd.get("gpu_procs")) or any(not j.get("hold") for j in wrun)
                or any(j["status"] == "running" and not j["hold"] and j["lane"] in ids for j in snap["jobs"]))
        hcfg = next(h for h in cfg["hosts"] if h["id"] == hd["id"])
        idle = [c for c, cd in zip(hcfg["checks"], hd.get("checks") or []) if c.get("when_idle") and cd.get("state") == "fail" and not busy]
        # 队列跑完到 worker 拉起保活之间有约半分钟空档，平台按空闲关机要几个小时：连续空闲 IDLE_GRACE_S 才报
        hs = hstate.setdefault(hd["id"], {})
        hs["idle_since"] = (hs.get("idle_since") or t) if idle else None
        for c in idle if hs["idle_since"] and t - hs["idle_since"] >= IDLE_GRACE_S else []:
            idle_say = f"{hd['label']}：{c['when_idle']}（卡上没有进程、也没有任务在跑）"
            alerts.append(alert(f"idle:{hd['id']}:{c['id']}", "both",
                                f"{idle_say}：先看本项目没跑的任务为什么没放（项目页车道状态、labmon worker status {hd['id']}），"
                                f"worker 不在就 labmon worker start {hd['id']}；保活由 worker 管，不自己 ka.sh start", idle_say))

    alerts.extend(_cpu_fit_alerts(fits))


def _cpu_fit_alerts(fits: dict) -> list:
    """开跑核数给少、给多的告警（只给会话，#46）：同一个项目同一类（tag，否则去种子号同名）的合成一条，跨主机（#91）。
    key cpu_low:<类>、cpu_high:<类> 不带任务名：同类后面的任务接着被判出来时 key 不变，消失后 6 小时内再出现不再叫醒（waits._new_alerts，#88）。
    fits 由 _host_alerts 收集：(how, 类) → [(运行中任务, cpu_fit, 在哪, 类按什么认)]。"""
    out = []
    for (how, cls), xs in fits.items():
        if len(xs) == 1:
            subj = f"任务 {xs[0][0]['name']}（{xs[0][2]}）"
        else:                                     # 按主机车道列：本机 车道 1：a_s1、a_s2；别的主机 车道 2：a_s3
            who = "；".join(f"{w}：{'、'.join(r['name'] for r, _, w2, _ in xs if w2 == w)}" for w in dict.fromkeys(w for _, _, w, _ in xs))
            subj = f"{'同 tag' if xs[0][3] == 'tag' else '同类'} {cls} 的 {len(xs)} 个任务（{who}）"
        sug = max(f["sug"] for _, f, _, _ in xs)

        def rng(k):                               # 几个任务的平均核数、峰值：一样的写一个，否则写范围
            a, b = min(f[k] for _, f, _, _ in xs), max(f[k] for _, f, _, _ in xs)
            return f"{a:.1f}" if round(a, 1) == round(b, 1) else f"{a:.1f}–{b:.1f}"
        give = "、".join(f"{g:g}" for g in sorted({f["give"] for _, f, _, _ in xs}))
        if how == "low":
            out.append(alert(f"cpu_low:{cls}", "agent",
                             f"{subj}近 10 分钟平均用了 {rng('avg')} 核，给的 {give} 核基本用满，所在的卡利用率不到 50%：可能给少了。"
                             f"没开跑的同类任务在清单那一行写 cpus={sug} 试试（cpus= 优先于历史；跑过以后可以删掉，历史只会往实测靠），"
                             "线程数、DataLoader 进程数按 $LABMON_CPUS 取；在跑的不必停"))
        else:
            hint = "、".join(f"{c:g}" for c in sorted({r["cpus"] for r, _, _, _ in xs if r.get("cpus") is not None})) or "?"
            out.append(alert(f"cpu_high:{cls}", "agent",
                             f"{subj}清单写了 cpus={hint}（给 {give} 核），跑到现在 1 分钟峰值只有 {rng('peak')} 核：写多了。"
                             "同类跑完几次、峰值都不到一半后，没开跑的自动按实测记账（开跑仍按 cpus= 给核），不必改清单；"
                             f"想马上让出核，没开跑的改成 cpus={sug}，或删掉 cpus= 改按历史估；在跑的不必停"))
    return out


NO_WORK_WAKE_S = 86400   # 项目跑空后这么久内给实验会话那条 no_work:（只给会话，#43）


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


def _idle_regs(cfg: dict, snap: dict, state: dict) -> None:
    """空了很久的登记（#47）：收起的只在登记里有的车道（快照的 quiet）里标了 idle 的（登记还在、最后一次活动早于 IDLE_REG_S，_lane_specs 定），出一条只给实验会话的 idle_regs:，
    列出这些清单、写 worker rm 的写法。只提示、不删：自动删了，会话以后再往旧清单追加，任务会悄悄不跑。
    monitor.toml 写明的与自动发现的车道不收进 quiet，也就不列。同一批只叫醒一次：key 带代数，代数与报过的清单记在项目状态的 idle_regs，
    出现没报过的清单才加一；会话删了一部分、留着长期清单时不变。清单又有活动就从报过的里去掉，再空满 IDLE_REG_S 算新的。"""
    st = state.setdefault("idle_regs", {"n": 0, "told": []})
    idle = [x for x in snap["quiet"] if x["idle"]]
    cur = {f"{x['host']}|{_qpath(x['root'], x['queue'])}" for x in idle}
    if cur - set(st["told"]):
        st["n"] += 1
    st["told"] = sorted(cur)
    if not idle:
        return
    by: dict = {}
    for x in sorted(idle, key=lambda x: (x["host"], x["root"], x["queue"])):
        by.setdefault(x["host"], {}).setdefault(x["root"], []).append(x["queue"])
    where = "；".join(f"{h} 上 " + "；".join(f"{r}：{'、'.join(js)}" for r, js in rs.items()) for h, rs in by.items())
    snap["alerts"].append(alert(f"idle_regs:{cfg['id']}:{st['n']}", "agent",
                                f"有 {len(idle)} 条清单空了 {IDLE_REG_S // 86400} 天以上（没有在跑与排队的任务、也没有新的活动），登记还留着：{where}。"
                                "worker 每轮都要在共享盘上查一遍每条登记：不再用的逐条 labmon worker rm <主机> <目录> <清单> 摘掉（只摘登记，不删文件）；"
                                "以后还要往里追加的长期清单留着就行，再有新的空清单才会再提示。新任务追加到项目固定目录下的长期清单，不按批、不按代码版本新开"))


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


def _attempts(lanes: list[dict], pid: str, t: float) -> list[dict]:
    """失败的尝试（任务后来重跑成功也留着）：时间线画成细红条，解释那段时间车道上发生了什么。
    取工作台存的用量记录（state.usage_file：worker 跑完的任务一行，存 30 天），按主机、目录、清单对到本项目的车道。"""
    out, rows = [], {}
    for ln in lanes:
        if not ln.get("root"):
            continue
        if ln["host"] not in rows:
            rows[ln["host"]] = load_rows(usage_file(ln["host"]))
        out += [{"name": r["n"], "lane": ln["id"], "t0": r["s"], "t1": r["t"]} for r in rows[ln["host"]]
                if r.get("st") == "failed" and r.get("p") == pid and (r.get("root"), r.get("queue")) == (ln["root"], ln["queue"]) and r["t"] >= t - KEEP_LONG_S]
    return out


def overrun_left(el: float) -> float:
    """剩余时长说不准时的估计（秒）：已用 el 秒的 OVER_FRAC、至少 OVER_MIN_S。第一段只有一个步数 > 0 的采样、又没有估时或已超估时
    时用它（_segment_eta）。与 worker 的 overrun_left（run_end：运行中任务超了预计）同一口径；worker 在服务器上跑、不 import 本包，
    各一份，改一处要改另一处。"""
    return max(OVER_MIN_S, OVER_FRAC * el)


def _segment_eta(pr, samples: list, seg: dict | None, el: float, floor: float | None, is_multi: bool, t: float,
                 hold: bool = False) -> tuple[float, str, float | None] | None:
    """运行中任务的剩余秒数：返回 (剩余, 来源, 速度 步/分钟) 或 None（交给估时、无估时等后备）。

    PROGRESS 只当作当前这一段的进度：有近期速度用近期速度，否则按这一段里第一个采样到最新采样的平均速度外推。
    第一段从第一个步数 > 0 的采样算起，不按任务开始时刻：开头加载、预热可能长时间不打进度（Issue #14：开头 102 分钟不打，
    按任务开始外推把预计结束推到 31～88 小时后），一行进度报出的步数也分不清是开头没做活还是做了没打。
    第一段只有一个步数 > 0 的采样时按 估时 − 已用；没有估时或已超估时按 overrun_left（已用的 20%、至少 10 分钟，与 worker 的
    run_end 同一口径），不按已用时间外推（Issue #17：开头静默比估时还长时同样出尖峰）。下一轮有了第二个采样就换成平均速度。
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
    if pr and recent:
        out = ((pr[1] - pr[0]) / recent, "近期速度", recent * 60)
    elif pr and not stale:
        if d:
            out = (d * (pr[1] - pr[0]) / (pr[0] - a[1]), "进度", (pr[0] - a[1]) / d * 60)
        elif single:
            out = (floor * 60 - el, "估时（进度刚开始）", None) if floor and floor * 60 > el else over
    if out and is_multi and floor and floor * 60 - el > out[0]:
        out = (floor * 60 - el, "估时（多段任务，进度只算当前这段）", out[2])
    if out is None and pr and (stale or is_multi) and not hold:
        if floor:
            out = (max(0.0, floor * 60 - el), ("估时（这段不打进度）" if stale else "估时（刚换段）") if floor * 60 >= el else "已超估时", None)
        elif d:
            out = (d * (pr[1] - pr[0]) / (pr[0] - a[1]), "进度（停了一阵）", None)
        elif single:
            out = over
    return out


def _progress_samples(prog: dict, segs: dict, j: dict, t: float) -> tuple[list, dict | None]:
    """记下运行中任务这一次的进度（时刻取日志最后修改时间），返回 (这一段的全部采样 [[t, i, n], ...], 这一段的起点)。

    一个任务可以分几段各打各的 PROGRESS（如先训练一组、核对，再训练另一组）。进度回退或总步数变了就算换了一段：
    采样清空，段起点记为新段第一次看到的 (时刻, 步数)，第几段记在 k。这一次运行里见过进度行就记 seen（noprogress 告警看它）。"""
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


def fill(html: str, slot: str, data) -> str:
    """把页面里的 /*__<slot>__*/null 换成数据（第一处）：数据里的 < 一律写成 \\u003c，任务名、日志里的 </script>、<!-- 都不能让浏览器提前结束脚本。
    DATA 在渲染时填（render_page）；ME（看板账号，#160）由看板服务发页面时按请求填（serve.Handler._page）。"""
    return html.replace(f"/*__{slot}__*/null", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"), 1)


def render_page(tpl: str, data: dict, title: str | None = None) -> str:
    """把模板（page.html / overview.html / login.html）渲染成数据内联的静态页：内联共用脚本 common.js 与数据。
    看板服务给普通用户现渲染去掉组内数据的工作台（users.view）也经它。"""
    html = fill((HERE / tpl).read_text().replace("/*__COMMON__*/", (HERE / "common.js").read_text(), 1), "DATA", data)
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
    cfg = lanes_cfg(cfg, glob_snap, out_dir / ".labmon", time.time())
    live = {h["id"]: h for h in cfg["hosts"] if not h["returned"] and not h.get("pool")}     # 算力池是合成主机（#24）：不探测，车道由代读的那台读
    lan, probes = L.gather(live, lambda h: run_probe(cfg, h) if (use_ssh or not h["ssh"]) else (None, "未连接（--no-ssh）"),
                           lambda k: use_ssh and bool(live[k]["ssh"]))     # 本机不在内网：不连，也不算探针失败
    probes.update({h["id"]: (None, "已归还") for h in cfg["hosts"] if h["returned"]})     # 不再连接
    probes.update({h["id"]: (None, POOL_LABEL) for h in cfg["hosts"] if h.get("pool")})
    snap = build(cfg, probes, out_dir, lan, glob_snap)
    if not lan["ok"]:                                  # 数据是离开内网前的：日志多久没更新、GPU 闲置这些按它算不准，不报
        snap["alerts"] = [a for a in snap["alerts"] if a["key"].split(":")[0] not in ("ssh_fail", "log_silent", "noprogress", "gpu_idle")]
        snap["lan"] = lan
    save_json(out_dir / "snapshot.json", snap)
    render(snap, out_dir)
    return snap
