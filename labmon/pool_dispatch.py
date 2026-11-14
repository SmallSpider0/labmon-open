"""池派单的一轮：从受信库存取候选，先持久化尝试再调用执行端。"""
from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config, fleet, lease, pool, pool_supply, poolqueues, projects, users, workerctl
from .records import log_events
from .service import log
from .config import ConfigError
from .state import load_json, save_json, snapshot, state_dir

CONTROL = "pool-dispatch.json"
REMOTE_ERRORS = (ConfigError, OSError, users.Denied)


def verified_head(row):
    """只消费原 worker 的准备结果；沿既有采集获得，不为队首另连 SSH、读清单或算依赖。"""
    board = snapshot()
    host = next((h for h in board.get("hosts", []) if h["id"] == row["host"]), {})
    status = host.get("status") or {}
    observed = board.get("generated", 0) - max(0, host.get("clock", 0) - status.get("t", 0))
    def wait(code, text, task=None):
        return {"task_name": task, "wait_reason": {"code": code, "text": text}}
    if host.get("error") or host.get("lan_off") or host.get("stopped") or time.time() - observed > config.STALE_S:
        return wait("offline", "来源 worker 尚未提供新鲜状态")
    if not status.get("pool_ready_protocol"):
        return wait("ineligible", "来源 worker 需要更新后才能向池申请资源")
    request = next((r for r in status.get("pool_requests", []) if r["queue_id"] == row["id"]
                    and (r["project"], r["root"], r["jobs"]) == (row["project"], row["root"], row["jobs"])), None)
    if request is None:
        return wait("offline", "等待来源 worker 确认清单登记")
    # 结束后必须等来源 worker 观察原清单完成标记；旧的已就绪快照不能再次领取。
    ended = max((a.get("ended_at") or 0 for a in [row.get("dispatch") or {}, *row.get("history", {}).values()]), default=0)
    if observed <= ended:
        return wait("offline", "等待来源 worker 更新任务结果")
    if request.get("empty"):
        return {"fingerprint": row["fingerprint"], "task_name": None}
    task = request.get("task_name")
    if not request.get("ready"):
        return wait("ineligible", request.get("reason") or "任务尚未准备好", task)
    if request.get("gang"):
        return wait("ineligible", "成组任务沿原 worker 的明确节点安排执行", task)
    if request.get("gpus", 0) < 1:
        return wait("ineligible", "CPU 任务等待原 worker 执行", task)
    if not request.get("duration_min"):
        return wait("cut", "worker 尚无预计时长，暂不能核对供给截止", task)
    return {"fingerprint": row["fingerprint"], "task_name": task, "source_observed": observed,
            **{k: request[k] for k in ("gpus", "vram_gib", "ram_gib", "cpu_cores", "duration_min")}}


def job(row, head, nodes):
    return {"job": row["id"], "account": row["account"], "project": row["project"], "since": row["t"],
            "eligible_nodes": {n["node"]: [card["uuid"] for card in n["cards"]] for n in nodes if n["node"] in head["eligible_nodes"]},
            **{k: head[k] for k in ("gpus", "vram_gib", "ram_gib", "cpu_cores", "duration_min")}}


def account_attempt(row, record):
    accounting = {"attempt_id": record["id"], "job": row["id"], "account": row["account"], "project": row["project"],
                  "node": record["node"], "gpus": record["devices"], "cpu_cores": record["cpu_cores"], "ram_gib": record["ram_gib"],
                  "reserved_at": record["reserved_at"], "t0": record.get("started_at")}
    if ended := record.get("ended_at"):
        accounting["t1" if accounting["t0"] else "released_at"] = ended
    return accounting


def _run(backend, row, attempt, launch=False):
    try:
        result = (backend.launch if launch else backend.status)(row, attempt)
    except REMOTE_ERRORS:
        result = {"phase": "unknown"}
    poolqueues.dispatch_receipt(row["id"], attempt["id"], result)


def tick(backend, *, enabled=False):
    """仅主看板调用。关闭时继续核对现有尝试。backend 是受信库存、队首与执行端。"""
    if not lease.primary():
        return
    rows = list(poolqueues.read().get("queues", {}).values())
    active = [r for r in rows if (r.get("dispatch") or {}).get("phase") in poolqueues.IN_FLIGHT]
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda r: _run(backend, r, r["dispatch"]), active))
    if isinstance(backend, Backend):
        pool_supply.reconcile_daily()
    if not enabled:
        if isinstance(backend, Backend):
            nodes = backend.inventory()
            now = time.time()
            plan = pool.plan({"now": now, "nodes": nodes, "jobs": [], "attempts": []})
            backend.publish(plan, now, set())
        return

    nodes, pending = backend.inventory(), {}
    sched = config.sched()
    jobs_by_name = {}
    for row in poolqueues.read().get("queues", {}).values():
        if row["state"] != "queued":
            continue
        if row["project"] in sched.get("pause", {}):
            poolqueues.dispatch_wait(row["id"], None, "paused", "项目已暂停")
            continue
        try:
            head = backend.head(row)
        except REMOTE_ERRORS:
            poolqueues.dispatch_wait(row["id"], None, "offline", "暂时无法核对清单")
            continue
        if head.get("wait_reason"):
            why = head["wait_reason"]
            poolqueues.dispatch_wait(row["id"], head.get("task_name"), why["code"], why["text"])
            continue
        if head["fingerprint"] != row["fingerprint"]:
            poolqueues.dispatch_wait(row["id"], head.get("task_name"), "ineligible", "清单或环境已改变，请撤回后重新提交")
            continue
        if not head.get("task_name"):
            poolqueues.dispatch_complete(row["id"])
            continue
        key = (row["project"], head["task_name"])
        jobs_by_name.setdefault(key, []).append(row["id"])
        request = job(row, head, nodes)
        focus = sched.get("focus", {})
        request["focus"] = row["project"] in focus and focus[row["project"]] is None
        request["focus_until"] = focus.get(row["project"])
        pending[row["id"]] = (row, head, request)

    for (pid, name), ids in jobs_by_name.items():
        snap = load_json(projects.out_dir(pid) / "snapshot.json", {})
        if len(ids) > 1 or any(j.get("id") == name and j.get("status") == "running" for j in snap.get("jobs", [])):
            for identity in ids:
                poolqueues.dispatch_wait(identity, None, "ineligible", "该项目有同名任务，请为算力池任务使用独立名称")
                pending.pop(identity)
    requests = [r for _, _, r in pending.values()]
    data = poolqueues.read()
    now = time.time()
    attempts = []
    for row in data.get("queues", {}).values():
        attempts.extend(account_attempt(row, a) for a in (row.get("history") or {}).values())
        if (a := row.get("dispatch") or {}).get("phase") in poolqueues.IN_FLIGHT:
            attempts.append(account_attempt(row, a))
    plan = pool.plan({"now": now, "nodes": nodes, "jobs": requests, "attempts": attempts})
    for wait in plan["waiting"]:
        _, head, _ = pending[wait["job"]]
        poolqueues.dispatch_wait(wait["job"], head["task_name"], wait["reason"], wait["text"])

    by_node = {n["node"]: n for n in nodes}
    claimed = []
    for choice in plan["assignments"]:
        if not lease.primary():
            break
        row, head, req = pending[choice["job"]]
        node = by_node[choice["node"]]
        if any(a["node"] == node["node"] for _, a in claimed):
            continue
        attempt = {"id": uuid.uuid4().hex, "phase": "starting", "reserved_at": now, "node": choice["node"],
                   "devices": choice["devices"], "generation": node["generation"], "task_name": head["task_name"],
                   "ram_gib": req["ram_gib"], "cpu_cores": req["cpu_cores"], "until": node["deadline"],
                   "supply_until": node.get("supply_until"), "start_before": min(node["deadline"], now + 45)}
        try:
            fresh = backend.claim(row, attempt)
        except users.Denied:
            continue
        claimed.append((fresh, attempt))
    if isinstance(backend, Backend):
        backend.publish(plan, now, {a["node"] for _, a in claimed})
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda pair: _run(backend, *pair, launch=lease.primary()), claimed))


def attempt_spec(row, attempt, regular):
    """即时控制与批次观测使用同一份执行身份。"""
    spec = {k: attempt[k] for k in ("id", "node", "generation", "task_name", "devices", "until", "start_before") if k in attempt}
    spec.update(supply_id=attempt.get("supply_id"), queue_id=row["id"], regular=regular)
    spec.update({k: row[k] for k in ("root", "jobs", "project", "fingerprint") if k in row})
    return spec


def observation_hosts():
    """加入现有采集批次的供给节点；不把组内供给变成普通个人主机。"""
    supplies = load_json(fleet.state_file(pool_supply.FILE), {})
    active = [q for q in poolqueues.read().get("queues", {}).values()
              if (q.get("dispatch") or {}).get("phase") in poolqueues.IN_FLIGHT]
    out = {}
    for supply in supplies.values():
        if not supply.get("daily") or supply.get("stage") == "released":
            continue
        try:
            h, regular, limit = pool_supply.host_of(supply["host"], supply["holder"], checking=True)
        except REMOTE_ERRORS:
            continue
        spec = {"host": h["id"], "regular": regular, "devices": supply["devices"], "attempts": []}
        for q in active:
            a = q["dispatch"]
            if a["node"] == h["id"]:
                spec["attempts"].append(attempt_spec(q, a, regular))
        out[h["id"]] = dict(h, pool_observe=spec, pool_lease_end=limit)
    return out


def observation(hid):
    row = snapshot().get("pool_observed", {}).get(hid, {})
    return row if time.time() - row.get("observed", 0) < config.STALE_S else {}


def live_view():
    return {"mode": "live", "jobs": [], **load_json(state_dir() / "pool-live.json",
            {"capacity": {"total": 0, "free": 0, "max_same_node_free": 0}, "nodes": []})}


def enabled():
    return lease.primary() and load_json(fleet.state_file(CONTROL), {}).get("enabled") is True


def set_enabled(value, by):
    lease.need("设置算力池派单")
    if type(value) is not bool:
        raise ConfigError("派单开关需要明确开启或关闭")
    with config.HOSTS_LOCK:
        if value:
            fleet.edit_state(CONTROL, lambda data: data.update(enabled=False))
            poolqueues.migrate_routes()
        fleet.edit_state(CONTROL, lambda data: data.update(enabled=value))
    log_events([{"kind": "config", "who": by, "msg": "开启算力池派单" if value else "关闭算力池派单"}])
    return {"dispatch_enabled": value}


class Backend:
    def __init__(self):
        self.nodes = []

    def inventory(self):
        rows = load_json(fleet.state_file(pool_supply.FILE), {})
        active = {q["dispatch"]["node"] for q in poolqueues.read().get("queues", {}).values()
                  if (q.get("dispatch") or {}).get("phase") in poolqueues.IN_FLIGHT}
        nodes = []
        for row in rows.values():
            if not row.get("daily") or row.get("stage") != "ready" or row.get("revoked") or row["until"] <= time.time() or row["host"] in active:
                continue
            observed = observation(row["host"])
            node = observed.get("node")
            if node and node["generation"] == row["generation"]:
                try:
                    node = pool_supply.checked_probe(row, row["host"], observed.get("lease_end"), node, observed["observed"])
                except ConfigError:
                    continue
                nodes.append(dict(node, observed_at=observed["observed"], supply_until=row["until"], same_node_verified=True))
        self.nodes = nodes
        return nodes

    def publish(self, plan, now, claimed):
        rows = load_json(fleet.state_file(pool_supply.FILE), {})
        occupied = {q["dispatch"]["node"] for q in poolqueues.read().get("queues", {}).values()
                    if (q.get("dispatch") or {}).get("phase") in poolqueues.IN_FLIGHT} | claimed
        nodes = {n["node"]: n for n in plan["nodes"]}
        for row in rows.values():
            if not row.get("daily"):
                continue
            hid = row["host"]
            if hid not in occupied and (row.get("revoked") or row["until"] <= now):
                state = "withdrawn"
            elif row.get("revoked") or row["until"] <= now:
                state = "withdrawing"
            elif hid in occupied:
                state = "running"
            elif hid not in nodes:
                state = "offline"
            else:
                continue
            nodes[hid] = {"node": hid, "physical_host": None, "model": row.get("model"), "state": state,
                          "total": len(row["devices"]), "free": 0}
        view = sorted(nodes.values(), key=lambda node: node["node"])
        capacity = {"total": sum(n["total"] for n in view if n["state"] not in ("withdrawing", "withdrawn")),
                    "free": sum(n["free"] for n in view),
                    "max_same_node_free": max((n["free"] for n in view), default=0)}
        save_json(state_dir() / "pool-live.json", {"observed": now, "nodes": view, "capacity": capacity})

    def head(self, row):
        who = poolqueues.pool_identity(row["account"])
        if not who:
            raise users.Denied(403, "账号已撤销")
        poolqueues.member(who)
        if not poolqueues.bound(who, row["project"], poolqueues.read()):
            raise users.Denied(403, "项目工作区已变")
        return dict(verified_head(row), eligible_nodes=[n["node"] for n in self.nodes])

    def claim(self, row, attempt):
        """先占账本再启远端。与供给撤回、借用和平台操作共用外层锁。"""
        with config.HOSTS_LOCK:
            supply = load_json(fleet.state_file(pool_supply.FILE), {}).get(attempt["node"])
            if (not enabled() or not supply or not supply.get("daily") or supply.get("stage") != "ready" or supply.get("revoked")
                    or supply["generation"] != attempt["generation"] or supply["until"] != attempt["supply_until"]
                    or time.time() >= attempt["until"] or not set(attempt["devices"]) <= set(supply["devices"])):
                raise users.Denied(409, "供给已变化")
            host, _, _ = pool_supply.host_of(supply["host"], supply["holder"])
            if not host.get("ssh"):
                raise users.Denied(409, "派单节点必须经受信 SSH")
            attempt["holder"] = supply["holder"]
            attempt["supply_id"] = supply["supply_id"]
            return poolqueues.claim(row["id"], attempt["id"], expected=row, dispatch=attempt)

    def remote(self, action, row, attempt):
        node = attempt["node"]
        h, regular, _ = pool_supply.host_of(node, attempt["holder"], checking=True)
        spec = attempt_spec(row, attempt, regular)
        return workerctl.pool_remote(h, action, [json.dumps(spec, separators=(",", ":"))], timeout=30)

    def launch(self, row, attempt):
        with config.HOSTS_LOCK:
            supply = load_json(fleet.state_file(pool_supply.FILE), {}).get(attempt["node"], {})
            latest = poolqueues.find(poolqueues.read(), row["id"])["dispatch"]
            if (not enabled() or supply.get("stage") != "ready" or supply.get("supply_id") != attempt.get("supply_id")
                    or supply.get("revoked") or time.time() >= attempt["until"]
                    or latest.get("stop_requested") or latest["id"] != attempt["id"]):
                return {"phase": "stopped" if latest.get("stop_requested") else "failed",
                        "generation": attempt["generation"], "cleaned": True}
        return self.remote("pool-dispatch", row, attempt)

    def status(self, row, attempt):
        if attempt.get("stop_requested") and not attempt.get("stop_sent"):
            result = self.remote("pool-stop", row, attempt)
            if result.get("phase") != "unknown":
                def sent(data):
                    current = (poolqueues.find(data, row["id"]) or {}).get("dispatch", {})
                    if current.get("id") == attempt["id"]:
                        current["stop_sent"] = True
                fleet.edit_state(poolqueues.FILE, sent)
            return result
        sample = observation(attempt["node"])
        result = sample.get("attempts", {}).get(attempt["id"], {"phase": "unknown"})
        return dict(result, observed_at=sample.get("observed"))


def stop(who, identity, attempt_id):
    # 收尾不要求仍有新任务资格；原账号或管理员始终能停自己的在途尝试。
    def edit(data):
        row = poolqueues.find(data, identity)
        if not row or (row["account"] != who.name and who.role != "admin"):
            raise users.Denied(404, "没有可停止的算力池清单")
        a = row.get("dispatch") or {}
        if not isinstance(attempt_id, str) or a.get("id") != attempt_id or a.get("phase") not in poolqueues.IN_FLIGHT or "holder" not in a:
            raise users.Denied(409, "这条清单当前没有可停止的尝试")
        a.update(stop_requested=True, phase="stopping")
        return poolqueues.view_row(row)

    return fleet.edit_state(poolqueues.FILE, edit)


def loop(stopping):
    while not stopping.wait(5):
        try:
            tick(Backend(), enabled=enabled())
        except Exception as e:
            log(f"算力池派单轮次失败：{type(e).__name__}")
