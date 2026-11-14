"""项目的运行资源与清单目的地。资格按项目主人核对，管理员代管不扩大资源范围。"""
from __future__ import annotations

import posixpath

from . import config, pool_supply, poolqueues as Q, poolview, projects, users, workerctl
from .records import log_events
from .state import snapshot


def identity(who, project):
    pid = Q.project_id(project)
    users.need_project(who, pid)
    if pid not in projects.project_ids():
        raise users.Denied(409, "项目已归档或未登记")
    owner = Q.pool_identity(projects.owner(pid))
    if not owner:
        raise users.Denied(409, "请先为项目设置有效的主人")
    return pid, owner


def private_hosts(owner):
    personal, error = poolview.personal_servers(owner)
    if error:
        return [], error
    owned = {h["id"] for h in personal}
    rows = []
    for host in config.active_hosts():
        if host.get("borrow") or host["id"] not in owned:
            continue
        why = ("这台主机尚未向项目主人开放，请先在人员权限中分配" if not users.may_host(owner, host["id"]) else
               "服务器正在供给算力池，收回并核对结束后才能用于专属任务" if pool_supply.held(host["id"]) else None)
        rows.append({"id": host["id"], "available": not why, "reason": why})
    return rows, None


def view(who, project):
    pid, owner = identity(who, project)
    hosts, error = private_hosts(owner)
    reason = poolview.member_reason(owner, poolview.trial())
    member = reason is None
    data = Q.read()
    workspace = Q.bound(owner, pid, data)
    if member and not workspace:
        reason = "请先绑定该项目的共享工作目录"
    rows = [q for q in data.get("queues", {}).values()
            if q["project"] == pid and q["account"].casefold() == owner.name.casefold() and q["state"] in Q.ACTIVE]
    routed = {(q["root"], q["jobs"]) for q in rows}
    queues = [{**Q.view_row(q), "destination": "pool"} for q in rows]
    for host in snapshot().get("regs", []):
        queues.extend({"destination": host["host"], "root": q["root"], "jobs": q["jobs"], "state": "registered"}
                      for q in host.get("queues", []) if q.get("project") == pid and (q["root"], q["jobs"]) not in routed)
    return {"project": pid, "owner": owner.name, "private": hosts, "private_reason": error,
            "pool": {"available": member and bool(workspace), "reason": reason,
                     "root": workspace["root"] if workspace else None}, "queues": queues}


def submit(who, project, data):
    """一条清单明确一个目的地；未知回执沿用原编号重试，不自动改道。"""
    with projects._reg_locked():
        pid, owner = identity(who, project)
        destination, jobs, request = (data.get(k) for k in ("destination", "jobs", "request_id"))
        jobs = Q.rel_jobs(jobs)
        if not isinstance(request, str) or not Q.REQUEST.fullmatch(request):
            raise users.Denied(400, "请提供提交编号；断线重试时沿用同一个编号")
        if destination == "pool":
            result = Q._submit(owner, pid, jobs, request)
            return {"destination": "pool", "queue": result}
        users.need_host(who, destination)
        hosts, error = private_hosts(owner)
        host = next((h for h in hosts if h["id"] == destination), None)
        if not host or not host["available"]:
            raise users.Denied(409, (host or {}).get("reason") or error or "这台不是项目主人池外的专属服务器")
        root = data.get("root")
        if not isinstance(root, str) or not root.startswith("/"):
            raise users.Denied(400, "请填写该项目的服务器工作目录")
        remote = next(h for h in config.active_hosts() if h["id"] == destination)
        canonical = posixpath.normpath(root)  # 真实路径由原 worker add 一次核对，不额外连服务器预读。
        # 已领取/未知尝试不能换路；尚未领取的先通过原工作区撤回并移交完成标记。
        for queue in Q.read().get("queues", {}).values():
            if queue["canonical"] == canonical and queue["jobs"] == jobs and queue["state"] != "withdrawn":
                if queue["project"] != pid or queue["account"].casefold() != owner.name.casefold():
                    raise users.Denied(409, "清单属于其他项目")
                source = Q.reader(queue["root"], queue["host"])
                if source["worker_home"] != remote["worker_home"] or source["data_root"] != remote["data_root"]:
                    raise users.Denied(409, "这两台不共用工作区，不能直接切换清单")
                Q._withdraw(owner, queue["id"])
        # worker 的登记锁做最后核对；已有 current 时拒绝更换，不迁移正在运行的任务。
        rc, out = workerctl.run_out("add", destination, [canonical, jobs], project=pid, expect_project=pid, idle=True)
        if rc:
            raise users.Denied(409, out.strip() or "未确认清单登记成功，请用原提交编号重试")
        result = {"project": pid, "destination": destination, "root": canonical, "jobs": jobs, "state": "registered"}
        log_events([{"kind": "config", "project": pid, "who": who.name, "host": destination, "msg": "设置项目清单的运行资源"}])
        return result
