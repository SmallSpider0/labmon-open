"""单池整卡的离线试算。输入由调用方核对，结果不授予启动权限。"""

import json
import math
import sys
from collections import Counter

from . import labmon_worker as W

WHY = {
    "ineligible": "没有获准使用的试点资源",
    "paused": "管理员暂停接单",
    "offline": "试点节点离线或状态过期",
    "busy": "等下一张空卡",
    "same_node": "需要 %d 张同机空闲卡",
    "active": "这个实验已有在跑或待启动的尝试",
}
REASONS = ("active", "cut", "vram", "ram", "cpu", "same_node", "busy", "paused", "offline", "ineligible")
WORKER_REASONS = {
    "nofit_gpu_count": "same_node",
    "no_gpu": "same_node",
    "nofit_vram_count": "vram",
    "nofit_vram": "vram",
    "nofit_ram": "ram",
    "nofit_cpu": "cpu",
    "cpu_other": "cpu",
    "cpu_est": "cpu",
}
NODE_STATES = {"ready", "paused", "offline", "borrowed"}


def number(value, name, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} 必须是大于等于 {minimum} 的有限数字")
    return value


def by_id(rows, key):
    out = {}
    for row in rows:
        identity = row[key]
        if not isinstance(identity, str) or not identity or identity in out:
            raise ValueError(f"{key} 为空或重复")
        out[identity] = row
    return out


def attempts_of(rows, now):
    """合并同一尝试的增量回执，乱序重发不倒退、不重复计费。"""
    out = {}
    for row in rows:
        identity = row["attempt_id"]
        devices = tuple(sorted(by_id(({"uuid": u} for u in row["gpus"]), "uuid")))
        if not isinstance(identity, str) or not identity or not devices:
            raise ValueError("尝试必须有编号与整卡设备")
        attempt = out.setdefault(identity, {})
        for k, v in {**row, "gpus": devices}.items():
            if k in attempt and v != attempt[k]:
                raise ValueError(f"尝试 {identity} 的 {k} 回执冲突")
            attempt[k] = v
    for a in out.values():
        for k in ("job", "account", "project", "node"):
            if not isinstance(a[k], str) or not a[k]:
                raise ValueError(f"尝试的 {k} 不能为空")
        reservation, start, end, release = (a.get(k) for k in ("reserved_at", "t0", "t1", "released_at"))
        if (reservation is None and start is None
                or end is not None and start is None
                or release is not None and start is not None):
            raise ValueError("尝试的开始、结束或撤销回执不完整")
        times = [number(t, "回执时刻") for t in (reservation, start, end, release) if t is not None]
        if times != sorted(times) or times[-1] > now:
            raise ValueError("尝试的回执时刻无效")
    return out


def occupied(attempts, nodes, now):
    """返回在用与已承诺的设备、作业和资源，以及实际已占的卡秒。"""
    active, devices, reservations, usage = {}, {}, {}, {}
    for a in attempts.values():
        start, end = a.get("t0"), a.get("t1")
        use = usage.setdefault(a["account"], {"active_cards": 0, "card_seconds": 0})
        if start is not None:
            use["card_seconds"] += len(a["gpus"]) * ((now if end is None else end) - start)
        if end is not None or a.get("released_at") is not None:
            continue
        if a["job"] in active or any(u in devices for u in a["gpus"]):
            raise ValueError("多个活跃尝试占用了同一作业或设备")
        active[a["job"]] = a
        devices.update(dict.fromkeys(a["gpus"], a["node"]))
        use["active_cards"] += len(a["gpus"])
        node = nodes.get(a["node"])
        observed = node.get("observed_at") if node else None
        if node and (start is None or observed is None or start >= observed):
            # 快照未必见到刚开的作业，CPU／内存先按承诺扣。
            budget = reservations.setdefault(a["node"], Counter())
            budget["ram"] += number(a["ram_gib"], "承诺内存")
            budget["cpu"] += number(a["cpu_cores"], "承诺 CPU")
    return active, devices, reservations, usage


def node_state(node, now):
    state = node["state"]
    if state not in NODE_STATES:
        raise ValueError("未知的节点状态")
    if state == "borrowed":
        return state
    observed = node.get("observed_at")
    if observed is None or not 0 <= now - observed <= W.STATUS_STALE_S:
        return "offline"
    return state


def resource_view(node, cards, budget, state):
    """把试点节点的预算转换成 worker 的资源快照。观测余量已扣实测占用。"""
    ram = number(node["ram_gib"], "可用内存")
    cpu = number(node["cpu_cores"], "可用 CPU")
    limit = number(node["ram_limit_gib"], "内存上限", minimum=ram)
    return {
        "cards": cards, "room": max(0, ram - budget["ram"]), "margin": W.ram_margin(limit), "limit": limit,
        "cpu_cap": cpu, "cpu_room": max(0, cpu - budget["cpu"]),
        "jobs": [], "ext": [], "gp": [], "cpu": None, "holds": {}, "tags": {}, "owner": set(),
        "who": "", "kept": False, "cut": None, "brief": True, "deadline": node.get("deadline"), "state": state,
    }


def nodes_of(by_node, devices, reservations, now):
    views, summaries, known_devices = {}, [], {}
    for name, node in sorted(by_node.items()):
        state = node_state(node, now)
        physical = node["physical_host"]
        if physical is not None and (not isinstance(physical, str) or not physical):
            raise ValueError("物理机标识必须是非空字符串或 null")
        cards = []
        for i, (u, c) in enumerate(sorted(by_id(node["cards"], "uuid").items())):
            if u in known_devices or devices.get(u, name) != name:
                raise ValueError(f"GPU {u} 被登记在不同执行节点")
            known_devices[u] = name
            total = number(c["vram_mb"], "GPU 总显存", minimum=1)
            free = number(c["free_mb"], "GPU 空闲显存")
            if free > total or type(c["idle"]) is not bool:
                raise ValueError(f"GPU {u} 的显存或空闲状态无效")
            if c["idle"] and u not in devices:
                cards.append({"i": i, "uuid": u, "total": total, "free": free, "keep": 0, "other": 0, "ka": False})
        if state == "borrowed":
            continue
        summaries.append(dict(node=name, physical_host=physical, model=node["model"], state=state,
                              total=len(node["cards"]), free=len(cards) if state == "ready" else 0))
        views[name] = resource_view(node, cards, reservations.get(name, Counter()), state)
    return views, summaries


def request(job, now):
    if number(job["since"], "队首时间") > now:
        raise ValueError("队首时间晚于快照时间")
    allowed = job["eligible_nodes"]
    if not isinstance(allowed, dict) or any(not isinstance(devices, list) for devices in allowed.values()):
        raise ValueError("eligible_nodes 必须按节点提供设备列表")
    for k in ("duration_min", "focus_until", "deadline"):
        if job.get(k) is not None:
            number(job[k], k)
    count = job["gpus"]
    if type(count) is not int or count < 1:
        raise ValueError("试算作业必须申请至少一张整卡")
    for k in ("account", "project"):
        if not isinstance(job[k], str) or not job[k]:
            raise ValueError(f"作业的 {k} 不能为空")
    return {
        "key": job["job"], "name": job["job"], "project": job["project"], "ngpu": count,
        "vram": number(job["vram_gib"], "每卡显存"), "src": "提示值",
        "ram": number(job["ram_gib"], "作业内存"), "ram_src": "提示值",
        "cpus": number(job["cpu_cores"], "作业 CPU", minimum=1), "cpu_src": "提示值",
        "excl": None, "tag": None, "hold": False, "dur": job.get("duration_min"), "cards": count,
    }


def pool_reason(code, count):
    return code, WHY[code] % count if code == "same_node" else WHY[code]


def same_node_verified(node):
    return bool(node["physical_host"]) or node.get("same_node_verified") is True


def fit(job, req, node, view, now):
    if view["state"] != "ready":
        return None, pool_reason(view["state"], req["ngpu"])
    count = req["ngpu"]
    if count > 1 and not same_node_verified(node):
        return None, pool_reason("same_node", count)
    allowed = set(job["eligible_nodes"][node["node"]])
    reserved = {u for j in view["jobs"] for u in j["gpus"]}
    cards = [c for c in view["cards"] if c["uuid"] in allowed and c["uuid"] not in reserved]
    deadlines = [t for t in (view["deadline"], job.get("deadline")) if t is not None]
    if deadlines and not W.fits_before(now, req["dur"], min(deadlines)):
        return None, ("cut", W.why_text("cut", True))
    if len(cards) < count:
        return None, pool_reason("same_node" if count > 1 else "busy", count)
    snap = dict(view, cards=cards, holds={})
    selected, text, _ = W.place(dict(req, allowed=None), snap, now)
    if selected is not None:
        return selected, None
    code = snap["code"]
    return None, (WORKER_REASONS.get(code, code), text)


def plan(snapshot):
    """按当前整卡占用与队首先后试算一轮，输入与回包见 docs/scheduler.md。"""
    now = number(snapshot["now"], "快照时间")
    attempts = attempts_of(snapshot.get("attempts", []), now)
    by_node = by_id(snapshot["nodes"], "node")
    for node in by_node.values():
        for key in ("observed_at", "deadline"):
            if node.get(key) is not None:
                number(node[key], key)
    active, devices, reservations, usage = occupied(attempts, by_node, now)
    views, nodes = nodes_of(by_node, devices, reservations, now)
    jobs = by_id(snapshot["jobs"], "job")
    requests = {name: request(j, now) for name, j in jobs.items()}
    load = Counter({a: u["active_cards"] for a, u in usage.items()})
    assignments, waiting = [], []
    remaining = {n["node"]: n["free"] for n in nodes}
    pending = list(jobs.values())
    while pending:
        j = min(pending, key=lambda r: (not (r.get("focus") is True or (r.get("focus_until") or 0) > now), load[r["account"]], r["since"], r["job"]))
        pending.remove(j)
        name = j["job"]
        result = {k: j[k] for k in ("job", "project", "account", "gpus")}
        if name in active:
            if (j["account"], j["project"]) != (active[name]["account"], active[name]["project"]):
                raise ValueError(f"作业 {name} 与活跃尝试的账户或项目不一致")
            reasons = dict([pool_reason("active", j["gpus"])])
        elif j.get("paused"):
            reasons = dict([pool_reason("paused", j["gpus"])])
        else:
            reasons = {}
            for n in sorted(nodes, key=lambda n: (remaining[n["node"]], n["node"])):
                if n["node"] not in j["eligible_nodes"]:
                    continue
                view = views[n["node"]]
                cards, reason = fit(j, requests[name], by_node[n["node"]], view, now)
                if cards is None:
                    code, message = reason
                    reasons.setdefault(code, message)
                    continue
                view["jobs"].append(W.acct_job(requests[name], cards, now, None, view["cpu_cap"]))
                assignments.append(dict(result, node=n["node"], devices=[c["uuid"] for c in cards]))
                remaining[n["node"]] -= len(cards)
                load[j["account"]] += len(cards)
                break
            if assignments and assignments[-1]["job"] == name:
                continue
        if reasons:
            reason = min(reasons, key=lambda k: REASONS.index(k) if k in REASONS else len(REASONS))
            message = reasons[reason]
        else:
            reason, message = pool_reason("ineligible", j["gpus"])
        waiting.append(dict(result, reason=reason, text=message))
    return {
        "mode": "shadow", "nodes": nodes, "assignments": assignments, "waiting": waiting, "usage": usage,
        "capacity": {"total": sum(n["total"] for n in nodes), "free": sum(n["free"] for n in nodes),
                     "max_same_node_free": max((n["free"] if same_node_verified(by_node[n["node"]]) else min(n["free"], 1) for n in nodes), default=0)},
    }


def main():
    try:
        result = plan(json.load(sys.stdin))
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        print(f"算力池试算输入无效：{e}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
