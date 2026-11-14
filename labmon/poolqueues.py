"""成员清单登记与领取。SSH 只核对文件，真正启动由池派单负责。"""
from __future__ import annotations

import base64
import json
import math
from inspect import getsource
import posixpath
import re
import shlex
import time
import uuid
from pathlib import PurePosixPath

from . import config, fleet, poolview, projects, users, workerctl
from .records import log_events
from .labmon_worker import pool_checked_files
from .state import load_json

ACTIVE = frozenset({"preparing", "queued", "claimed"})
IN_FLIGHT = frozenset({"starting", "running", "stopping", "unknown"})
TERMINAL = frozenset({"done", "failed", "stopped"})

FILE = "pool-queues.json"
REQUEST = re.compile(r"[A-Za-z0-9_-]{1,80}")
# 只在看板选定的读盘主机运行。路径参数由 shlex.quote 引用，不读取清单中的命令。
CHECK_FILES = getsource(pool_checked_files) + r"""
import base64, json, sys
root, hashes, contents = pool_checked_files(*sys.argv[1:4])
response = [root, hashes]
if sys.argv[4:] == ["contents"]:
    response.append([base64.b64encode(data).decode("ascii") if data is not None else None for data in contents])
print(json.dumps(response))
"""


def state_file():
    return fleet.state_file(FILE)


def read():
    return load_json(state_file(), {})


def member(who):
    if reason := poolview.member_reason(who, poolview.trial()):
        raise users.Denied(403, reason)


def project_id(value):
    if not isinstance(value, str) or not projects.ID_RE.fullmatch(value):
        raise users.Denied(400, "项目 id 无效")
    return value


def rel_jobs(value):
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(ch) < 32 for ch in value):
        raise users.Denied(400, "清单要写工作区内的相对路径")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or not p.parts or not p.name.endswith(".txt"):
        raise users.Denied(400, "清单要写工作区内的相对 .txt 路径")
    return str(p)


def projects_root(host):
    return posixpath.join(host["data_root"], "projects")


def inside(path, root):
    return path != root and posixpath.commonpath((path, root)) == root


def reader(root, hid=None):
    hosts = [h for h in config.active_hosts() if not h.get("borrow") and config.share_host(h)
             and inside(root, projects_root(h)) and (hid is None or h["id"] == hid)]
    if len(hosts) != 1:
        raise users.Denied(409, "请指定能读取该共享项目目录的一台在用非借用远端主机")
    return hosts[0]


def inspect(host, root, jobs="", *, contents=False):
    args = (host["python"], "-", projects_root(host), root, jobs)
    cmd = shlex.join((*args, "contents") if contents else args)
    rc, out = workerctl.ssh(host, cmd, CHECK_FILES)
    if rc:
        raise users.Denied(409, "无法读取获准项目目录或清单，或实际路径超出工作区")
    try:
        reply = json.loads(out)
        canonical, hashes = reply[:2]
    except (TypeError, ValueError) as e:
        raise users.Denied(409, "读盘主机返回了无效的路径核对结果") from e
    if (not isinstance(canonical, str) or not canonical.startswith("/") or not isinstance(hashes, list)
            or len(hashes) != (2 if jobs else 0) or (jobs and hashes[0] is None)
            or any(x is not None and (not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x)) for x in hashes)):
        raise users.Denied(409, "读盘主机返回了无效的路径核对结果")
    if contents:
        try:
            raw = [base64.b64decode(x, validate=True) if x is not None else None for x in reply[2]]
        except (TypeError, ValueError, IndexError) as e:
            raise users.Denied(409, "读盘主机返回了无效的清单内容") from e
        return canonical, hashes, raw
    return canonical, hashes


def bind(project, root, host, by):
    with projects._reg_locked():
        return _bind(project, root, host, by)


def _bind(project, root, host, by):
    pid = project_id(project)
    if (not isinstance(root, str) or not root.startswith("/") or root.startswith("//")
            or ".." in PurePosixPath(root).parts or any(ord(ch) < 32 for ch in root)):
        raise users.Denied(400, "工作区要写共享项目目录下的绝对路径")
    root = posixpath.normpath(root)
    if pid not in projects.project_ids() or not projects.owner(pid):
        raise users.Denied(409, "请先登记项目并为它设置主人")
    h = reader(root, host)
    canonical, _ = inspect(h, root)
    if canonical != root:
        raise users.Denied(409, "工作区绑定须使用实际目录路径，不能经符号链接")

    def edit(data):
        owner = projects.owner(pid)
        if pid not in projects.project_ids() or not owner:
            raise users.Denied(409, "请先登记项目并为它设置主人")
        workspaces = data.setdefault("workspaces", {})
        binding = {"account": owner, "root": root, "canonical": canonical, "host": h["id"]}
        old = workspaces.get(pid)
        for p, ws in workspaces.items():
            if p != pid and posixpath.commonpath((canonical, ws["canonical"])) in (canonical, ws["canonical"]):
                raise users.Denied(409, "工作区与其他算力池项目重叠")
        active = [q for q in data.get("queues", {}).values() if q["project"] == pid and q["state"] in ACTIVE]
        if old != binding and active:
            raise users.Denied(409, "请先撤回该项目的池队列或等尝试结束")
        workspaces[pid] = binding
        return old != binding

    if fleet.edit_state(FILE, edit):
        log_events([{"kind": "config", "project": pid, "who": by, "msg": "绑定算力池项目工作区"}])


def pool_identity(name):
    account = users.account(name)
    return users.Ident(account["name"], account["role"], frozenset({"project"}), "pool", "", None) if account else None


def bound(who, pid, data, active=None):
    ws = data.get("workspaces", {}).get(pid)
    owner = projects.owner(pid) if ws else None
    if ws and owner and ws["account"].casefold() == owner.casefold() == who.name.casefold() and users.owns(who, pid):
        if pid in (projects.project_ids() if active is None else active):
            return ws
    return None


def view_row(row, writable=True):
    out = {k: row[k] for k in ("id", "project", "jobs", "state", "t")} | {"can_withdraw": writable and row["state"] in ("preparing", "queued", "completed")}
    # 已撤回的清单只展示撤回状态，账本仍保留此前的派单观察。
    if row["state"] != "withdrawn" and (d := row.get("dispatch")):
        out["dispatch"] = {k: d.get(k) for k in ("phase", "node", "task_name", "wait_reason")}
        out["dispatch"]["wait_job"] = f"{row['project']}/{d['task_name']}" if d.get("task_name") else None
        out["dispatch"]["attempt_id"] = d.get("id") if "holder" in d else None
        out["dispatch"]["can_stop"] = writable and "holder" in d and d["phase"] in IN_FLIGHT and not d.get("stop_requested")
    return out


def listing(who, *, dispatch_enabled=False):
    if not poolview.allowed(who, poolview.trial()):
        raise users.Denied(403, "你的账号没有加入算力池试点")
    data = read()
    active = set(projects.project_ids())
    writable = "project" in who.scopes
    rows = sorted((q for q in data.get("queues", {}).values() if q["account"] == who.name),
                  key=lambda q: (q["t"], q["id"]), reverse=True)
    return {"queues": [view_row(q, writable) for q in rows if q["state"] in ACTIVE]
            + [view_row(q, writable) for q in rows if q["state"] not in ACTIVE][:30],
            "can_submit": sorted(p for p in data.get("workspaces", {}) if writable and bound(who, p, data, active)),
            "dispatch_enabled": dispatch_enabled}


def submit(who, project, jobs, request_id):
    with projects._reg_locked():
        return _submit(who, project, jobs, request_id)


def _submit(who, project, jobs, request_id):
    member(who)
    pid, jobs = project_id(project), rel_jobs(jobs)
    if not isinstance(request_id, str) or not REQUEST.fullmatch(request_id):
        raise users.Denied(400, "提交编号无效")
    data = read()
    ws = bound(who, pid, data)
    if not ws:
        raise users.Denied(409 if users.owns(who, pid) else 403, "该项目没有获准的算力池工作区")
    key = who.name + ":" + request_id

    def previous(rows):
        row = rows.get(key)
        if row and (row["project"], row["jobs"]) != (pid, jobs):
            raise users.Denied(409, "此提交编号已用于另一条清单")
        return row

    old = previous(data.get("queues", {}))
    if old:
        return _activate(old) if old["state"] == "preparing" else view_row(old)
    root, fingerprint = inspect(reader(ws["root"], ws["host"]), ws["root"], jobs)
    if root != ws["canonical"]:
        raise users.Denied(409, "项目工作区实际路径已改变，请找管理员核对")

    def edit(data):
        member(who)
        if bound(who, pid, data) != ws:
            raise users.Denied(409, "项目工作区或归属已变")
        rows = data.setdefault("queues", {})
        old = previous(rows)
        if old:
            return view_row(old)
        if any(q["canonical"] == root and q["jobs"] == jobs and q["state"] in ACTIVE for q in rows.values()):
            raise users.Denied(409, "这条清单已提交到算力池")
        row = {**ws, "id": uuid.uuid4().hex, "account": who.name, "project": pid, "jobs": jobs,
               "request_id": request_id, "fingerprint": fingerprint, "state": "preparing", "t": time.time()}
        rows[key] = row
        return row

    return _activate(fleet.edit_state(FILE, edit))


def route_remote(row, action):
    host = reader(row["root"], row["host"])
    finished = {a["task_name"]: a["phase"] for a in row.get("history", {}).values()
                if a.get("phase") in ("done", "failed")}
    spec = {k: row[k] for k in ("root", "jobs", "project")}
    spec.update(queue_id=row["id"], action=action, finished=finished if row.get("route_protocol") != 1 else {}, source_host=host["id"])
    try:
        return workerctl.pool_remote(host, "pool-route", [json.dumps(spec)], timeout=30)
    except config.ConfigError as e:
        raise users.Denied(409, str(e)) from e


def _activate(row):
    """落盘后才交接远端路由；断线保留 preparing，可用原提交编号重试。"""
    result = route_remote(row, "claim")
    if not result.get("claimed"):
        raise users.Denied(409, "清单通道尚未核对，请沿用原提交编号重试")
    def edit(data):
        current = find(data, row["id"])
        if not current or current["state"] != "preparing":
            raise users.Denied(409, "提交状态已变化")
        current.update(state="queued", route_protocol=1, prior_finished=result.get("prior_finished", {}))
        return view_row(current)
    return fleet.edit_state(FILE, edit)


def migrate_routes():
    """开启新派单前原地迁移旧登记；逐条确认，中断后重做，不迁移在途尝试。"""
    with projects._reg_locked():
        rows = list(read().get("queues", {}).values())
        if any(q.get("route_protocol") != 1 and (q.get("dispatch") or {}).get("phase") in IN_FLIGHT for q in rows):
            raise users.Denied(409, "旧派单还有在途尝试，等执行端核清收尾后再开启")
        for row in rows:
            if row.get("route_protocol") == 1 or row["state"] not in ("preparing", "queued"):
                continue
            who = pool_identity(row["account"])
            if not who or not bound(who, row["project"], read()):
                raise users.Denied(409, "待迁移清单的项目归属或工作区已改变")
            member(who)
            fleet.edit_state(FILE, lambda data: find(data, row["id"]).update(state="preparing"))
            _activate(row)


def find(data, identity):
    return next((row for row in data.get("queues", {}).values() if row["id"] == identity), None)


def withdraw(who, identity):
    with projects._reg_locked():
        return _withdraw(who, identity)


def _withdraw(who, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise users.Denied(400, "队列编号无效")

    prior = find(read(), identity)
    if not prior or (prior["account"] != who.name and who.role != "admin"):
        raise users.Denied(404, "没有这条本人的算力池清单")
    if prior["state"] not in ("preparing", "queued", "withdrawn", "completed"):
        raise users.Denied(409, "这条清单已有在途尝试")
    if prior["state"] != "withdrawn":
        result = route_remote(prior, "release")
        if not result.get("released"):
            raise users.Denied(409, "清单通道尚未释放，请重试核对")

    def edit(data):
        row = find(data, identity)
        if not row or (row["account"] != who.name and who.role != "admin"):
            raise users.Denied(404, "没有这条本人的算力池清单")
        if row["state"] not in ("preparing", "queued", "withdrawn", "completed"):
            raise users.Denied(409, "这条清单已有在途尝试")
        row["state"] = "withdrawn"
        return view_row(row)

    return fleet.edit_state(FILE, edit)


def claim(identity, attempt_id, *, expected=None, dispatch=None):
    with projects._reg_locked():
        row = find(read(), identity)
        if not row or row["state"] != "queued":
            raise users.Denied(409, "清单已撤回或已领取")
        who = pool_identity(row["account"])
        if not who:
            raise users.Denied(409, "清单所属账号已不存在")
        member(who)
        if not bound(who, row["project"], read()):
            raise users.Denied(409, "清单的项目归属或工作区已改变")
        # 11-14 前落盘的队列缺交接结果，首次认领时迁移；新提交已在_activate交接。
        if "prior_finished" not in row:
            result = route_remote(row, "claim")
            if not result.get("claimed"):
                raise users.Denied(409, "尚未核对清单通道")
            finished = result.get("prior_finished", {})
            fleet.edit_state(FILE, lambda data: find(data, identity).update(prior_finished=finished))
            if dispatch and dispatch["task_name"] in finished:
                raise users.Denied(409, "这项任务已有完成标记，下一轮重新读取清单")
        return _claim(identity, attempt_id, expected=expected, dispatch=dispatch)


def _claim(identity, attempt_id, *, expected=None, dispatch=None):
    """受信派单器调用。领取与撤回互斥；仅返回内部记录，启动前仍须核对真实节点及冻结内容。"""
    if not isinstance(attempt_id, str) or not REQUEST.fullmatch(attempt_id):
        raise users.Denied(400, "尝试编号无效")

    def edit(data):
        row = find(data, identity)
        if not row or row["state"] != "queued":
            raise users.Denied(409, "清单已撤回或已领取")
        pid, account = row["project"], row["account"]
        who = pool_identity(account)
        if not who:
            raise users.Denied(409, "清单所属账号已不存在")
        member(who)
        ws = bound(who, pid, data)
        if not ws or any(ws[k] != row[k] for k in ("root", "canonical", "host")):
            raise users.Denied(409, "清单的项目归属、成员资格或工作区已改变")
        if expected and any(row[k] != expected[k] for k in ("fingerprint", "account", "project", "root", "jobs")):
            raise users.Denied(409, "清单在领取前已变化")
        others = data["queues"].values()
        if any(q.get("attempt_id") == attempt_id or attempt_id in (q.get("history") or {}) for q in others):
            raise users.Denied(409, "尝试编号已经使用")
        if dispatch:
            for q in others:
                current = q.get("dispatch") or {}
                if current.get("phase") not in IN_FLIGHT:
                    continue
                if set(dispatch["devices"]) & set(current["devices"]):
                    raise users.Denied(409, "设备已被另一次尝试领取")
                if (row["project"], dispatch["task_name"]) == (q["project"], current["task_name"]):
                    raise users.Denied(409, "同一项目已有同名队首")
            row["dispatch"] = dispatch
        row.update(state="claimed", attempt_id=attempt_id)
        return row

    return fleet.edit_state(FILE, edit)


def dispatch_wait(identity, task, code, text):
    def edit(data):
        row = find(data, identity)
        if row and row["state"] == "queued":
            row["dispatch"] = {"phase": "waiting", "task_name": task, "wait_reason": {"code": code, "text": text}, "observed_at": time.time()}
    fleet.edit_state(FILE, edit)


def dispatch_complete(identity):
    def edit(data):
        row = find(data, identity)
        if row and row["state"] == "queued":
            row["state"] = "completed"
    fleet.edit_state(FILE, edit)


def project_rows(project, data=None):
    """项目生命周期使用账本全部归属历史，不能因换主人隐藏在途尝试。"""
    return [q for q in (read() if data is None else data).get("queues", {}).values()
            if q["project"] == project]


def active_reason(project, data=None):
    rows = project_rows(project, data)
    if any((q.get("dispatch") or {}).get("phase") in IN_FLIGHT for q in rows):
        return "算力池仍有已领取、运行或待确认清理的尝试，请等收尾完成"
    if any(q["state"] in ACTIVE for q in rows):
        return "算力池仍有待位清单，请先撤回或等实验结束"
    return None


def delete_block(project, data):
    """归档项目永久删除前仍需确认池通道已释放。保留已撤回的清单和尝试历史。"""
    if reason := active_reason(project, data):
        return reason
    if any(row["state"] != "withdrawn" and row.get("route_protocol") for row in project_rows(project, data)):
        return "算力池清单仍有登记，请先解除登记"
    return None


def unbind_project(project):
    """项目登记锁内移除空闲工作区绑定，共享目录及池账本历史继续保留。"""
    if project in read().get("workspaces", {}):
        fleet.edit_state(FILE, lambda data: data.get("workspaces", {}).pop(project, None))


def release_project(project):
    """已持项目锁并通过归档检查后，沿撤回路径清理完成清单，保留历史。"""
    released, withdrawn = set(), set()
    for row in project_rows(project):
        if row["state"] != "withdrawn":
            who = users.Ident(row["account"], "user", frozenset(), "internal", "")
            _withdraw(who, row["id"])
            released.add((row["root"], row["jobs"]))
        withdrawn.add(row["id"])
    return released, withdrawn


def attempts(row):
    """同一尝试在当前回执与历史中只保留一份；等待观察不是运行历史。"""
    out = dict(row.get("history") or {})
    current = row.get("dispatch") or {}
    if current.get("id"):
        out[current["id"]] = current
    return sorted(out.values(), key=lambda a: (a.get("reserved_at", 0), a["id"]))


def project_view(project, data=None, now=None, writable=False):
    now = time.time() if now is None else now
    rows = project_rows(project, data)
    return {"queues": [view_row(q, writable) for q in rows],
            "active": any(q["state"] in ACTIVE or (q.get("dispatch") or {}).get("phase") in IN_FLIGHT for q in rows),
            "attempts": [{"queue_id": q["id"], "jobs": q["jobs"], "output_root": q["root"],
                          **{k: a.get(k) for k in ("id", "task_name", "phase", "node", "devices", "reserved_at", "started_at", "finished_at", "ended_at", "log_tail", "requeued")},
                          "observed_at": a.get("observed_at") or a.get("ended_at") or a.get("started_at")}
                         for q in rows for a in attempts(q)]}


def occupancy(data=None, since=0, skip=None, now=None):
    """池真实占卡区间；运行中的右端是观测时刻，不是预计完成。"""
    data = read() if data is None else data
    now = time.time() if now is None else now
    out = {}
    for q in data.get("queues", {}).values():
        if q["project"] == skip:
            continue
        for a in attempts(q):
            start = a.get("started_at")
            end = a.get("finished_at") or a.get("ended_at") or a.get("observed_at") or start
            if not start or not end or end < since:
                continue
            phase = a["phase"]
            out.setdefault(a["node"], []).append({"p": q["project"], "n": a["task_name"], "attempt_id": a["id"],
                "root": q["root"], "queue": q["jobs"], "lane": q["jobs"], "t0": start, "t1": max(start, min(now, end)), "g": a.get("devices", []),
                "st": {"done": "done", "failed": "failed", "stopped": "stop"}.get(phase, "run"),
                "observed": True, "phase": phase, "end_kind": "finished" if a.get("finished_at") else "cleaned" if a.get("ended_at") else "observed"})
    return out


def wait_status(project, task, data):
    """读取与成员列表同一个派单账本；wait 的项目权限仍由原入口核对。"""
    owner = projects.owner(project)
    if not owner:
        return None
    records = []
    for row in data.get("queues", {}).values():
        if row["project"] != project or row["account"].casefold() != owner.casefold() or row["state"] == "withdrawn":
            continue
        current = row.get("dispatch") or {}
        records.extend(a for a in [*row.get("history", {}).values(), current] if a.get("task_name") == task)
    if not records:
        return None
    a = max(records, key=lambda r: (r["phase"] in IN_FLIGHT, r.get("reserved_at") or r.get("observed_at") or 0))
    phase = a["phase"]
    if a.get("requeued"):
        return {"st": "queued", "text": "算力池运行中断，原 worker 已重新排队"}
    if phase in TERMINAL:
        return {"st": "done" if phase == "done" else "failed", "text": {"done": "算力池任务已完成", "failed": "算力池任务失败", "stopped": "算力池任务已停止"}[phase]}
    if phase == "running":
        return {"st": "running", "text": f"在算力池节点 {a['node']} 上运行"}
    return {"st": "queued", "text": (a.get("wait_reason") or {}).get("text") or
            ("正在核对算力池尝试，继续等待" if phase == "unknown" else "等待算力池放行")}


def dispatch_receipt(identity, attempt_id, result):
    """同一次尝试的回执可重发；未确认清理就继续占用设备。"""
    phase = result.get("phase")
    if phase not in IN_FLIGHT | TERMINAL:
        raise ValueError("派单回执状态无效")

    def edit(data):
        row = find(data, identity)
        a = row.get("dispatch") if row else None
        if not a or a["id"] != attempt_id or a["phase"] not in IN_FLIGHT:
            return
        if phase != "unknown" and result.get("generation") != a["generation"]:
            a["phase"] = "unknown"
            return
        # 只记录真实观察，不把失联查询时间当成任务仍在运行。
        observed = result.get("observed_at")
        if phase != "unknown" and type(observed) in (int, float) and math.isfinite(observed):
            a["observed_at"] = max(a.get("observed_at") or a["reserved_at"], min(time.time(), observed))
        if isinstance(result.get("log_tail"), list):
            a["log_tail"] = [str(line)[:2000] for line in result["log_tail"][-30:]]
        if result.get("started_at") and not a.get("started_at"):
            a["started_at"] = max(a["reserved_at"], min(time.time(), result["started_at"]))
        if isinstance(result.get("finished_at"), (int, float)) and a.get("started_at"):
            a["finished_at"] = max(a["started_at"], min(time.time(), result["finished_at"]))
        if phase in TERMINAL:
            if result.get("cleaned") is not True:
                a["phase"] = "unknown"
                return
            a.update(phase=phase, ended_at=time.time(), requeued=result.get("requeued") is True)
            row.setdefault("history", {})[attempt_id] = dict(a)
            # 原 worker 已经收尾时由原清单决定下一项（包括 afterany）；启动前失败仍需处理。
            row["state"] = "queued" if phase == "done" or (phase == "failed" and (result.get("task_finished") or result.get("requeued") is True)) else "completed"
            return
        if a.get("stop_requested") and phase != "unknown":
            a["phase"] = "stopping"
            return
        if (phase == "starting" and a["phase"] != "starting") or (phase == "running" and a["phase"] == "stopping"):
            return
        if phase == "running" and not a.get("started_at"):
            a["started_at"] = time.time()
        a["phase"] = phase

    fleet.edit_state(FILE, edit)
