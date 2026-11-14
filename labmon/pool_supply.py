"""管理员批准的单次池供给与固定 CUDA 验收。普通工作台仍只投影试算。"""
import json
import math
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, fleet, fleetops, lease, pool, poolview, projects, renew, users, workerctl
from .config import ConfigError
from .labmon_worker import POOL_HINTS, hints
from .records import log_events
from .state import load_json, snapshot

FILE = "pool-supply.json"
WINDOW_S = 1800
RUN_S = 125
CLOCK_S = 5
HELD = "这台已批准给算力池，先撤回供给并核对结束"


def held(host):
    """未开始的授权到期自动解锁，已经认领的要核实清理。"""
    row = load_json(fleet.state_file(FILE), {}).get(host)
    if not row:
        return False
    if row.get("daily"):
        if row.get("stage") != "released":
            return True
        from . import poolqueues
        if any((q.get("dispatch") or {}).get("node") == host and
               q["dispatch"]["phase"] in poolqueues.IN_FLIGHT for q in poolqueues.read().get("queues", {}).values()):
            return True
    return bool(row.get("attempt") or (not row.get("revoked") and row["until"] > time.time()))


def account_project(project, name):
    account = users.account(name) if isinstance(name, str) else None
    if not account or not isinstance(project, str) or not projects.ID_RE.fullmatch(project):
        raise ConfigError("要选有效的试点项目与账号")
    who = users.Ident(account["name"], account["role"], frozenset(), "pool", "")
    if project in config.sched().get("pause", {}):
        raise ConfigError("试点项目已暂停")
    if (poolview.member_reason(who, poolview.trial()) is not None or project not in projects.project_ids()
            or (projects.owner(project) or "").lower() != who.name.lower()):
        raise ConfigError("项目未登记、已归档，或不属于该试点成员")
    return who.name


def host_of(hid, holder, *, checking=False):
    cfg = fleet.conf() or {}
    if not isinstance(hid, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", hid):
        raise ConfigError("主机编号无效")
    hosts = config.active_hosts()
    own = next((h for h in hosts if h["id"] == hid), None)
    group = next((s for s in cfg.get("servers", []) if s["id"] == hid), None)
    if own and own.get("borrow") or group and fleet.borrowed(group):
        raise ConfigError(f"{hid} 正借用，不能同时加入试点")
    h, regular = own or group, own is not None
    if h is None:
        raise ConfigError(f"找不到可供给的节点 {hid}")
    actual = fleet.host_holder(h, cfg) if regular else group["holder"]
    if not actual or actual != holder:
        raise ConfigError(f"{hid} 的持有人已变化或未确认")
    if hid in fleet.off_servers() or hid in renew.state()["res"]:
        raise ConfigError(f"{hid} 已关机或正在预备续租")
    if hid in fleetops.busy():
        raise ConfigError(f"{hid} 正在做平台操作")
    board = snapshot()
    row = next((r for r in (board.get("hosts") or []) + ((board.get("fleet") or {}).get("servers") or []) if r["id"] == hid), {})
    cutoff = row.get("lease_end")
    if not checking and (not cutoff or cutoff <= time.time() + RUN_S):
        raise ConfigError(f"{hid} 没有已核实的可用租期")
    if regular:
        target = own
    else:
        # 共用 Python 与 worker 部署位置，授权仍只在本模块，不把组内服务器加入普通主机清单。
        template = config.guest_host(group, hosts)
        target = config.host_defaults({k: template[k] for k in ("id", "ssh", "port", "known_hosts", "python", "data_root", "ka_bin", "pool_python") if k in template})
    return target, regular, cutoff


def remote(h, regular, device, action, **options):
    args = ["--host", h["id"], "--device", device]
    if regular:
        args.append("--regular")
    if options.pop("daily", False):
        args.append("--daily")
    for k, v in options.items():
        args.extend(("--" + k.replace("_", "-"), str(v)))
    return workerctl.pool_remote(h, action, args, timeout=RUN_S + 15 if action == "pool-trial" else 28)


def supply_devices(row):
    return row["devices"] if row.get("daily") else [row["device"]]


def probe(row):
    h, regular, limit = host_of(row["host"], row["holder"])
    before = time.time()
    node = remote(h, regular, ",".join(supply_devices(row)), "pool-probe", daily=row.get("daily", False))
    return h, regular, checked_probe(row, h["id"], limit, node, before)


def checked_probe(row, hid, limit, node, before):
    """即时核对与采集批次共用时钟、租期口径。"""
    if not before - CLOCK_S <= node["clock"] <= time.time() + CLOCK_S:
        raise ConfigError(f"{hid} 的时钟与看板相差过大")
    if not limit:
        raise ConfigError(f"{hid} 没有已核实的可用租期")
    return {**node, "deadline": min(limit, row["until"])}


def supply(data, by):
    """明确授权一台已知节点、整卡、账户和项目。批准本身不启动任务。"""
    lease.need("授权算力池试点")
    hid, holder, device, until, project = (data.get(k) for k in ("host", "holder", "device", "until", "project"))
    if data.get("agreed") is not True:
        raise ConfigError("先确认持有人同意这一轮试点")
    if not isinstance(device, str) or not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", device):
        raise ConfigError("需要明确指定整卡 GPU UUID")
    now = time.time()
    if type(until) not in (int, float) or not math.isfinite(until) or not now + 180 <= until <= now + WINDOW_S:
        raise ConfigError("试点时限必须在现在起 3 到 30 分钟内")
    account = account_project(project, data.get("account"))
    row = {"host": hid, "holder": holder, "device": device, "project": project, "account": account, "until": until}
    _, _, node = probe(row)
    if node["deadline"] != until:
        raise ConfigError("试点窗口不能晚于租期")
    row["generation"] = node["generation"]
    # 在同一互斥段检查借用和平台操作，之后才登记本次供给。
    with config.HOSTS_LOCK:
        host_of(hid, holder)
        account_project(project, account)
        def put(rows):
            for key in list(rows):
                if not held(key):
                    rows.pop(key)
            if hid in rows or any(device in supply_devices(r) or r.get("project") == project for r in rows.values()):
                raise ConfigError("节点、设备或试点项目已被占用")
            rows[hid] = row
        fleet.edit_state(FILE, put)
    audit("批准独立算力池试点供给", row, by)
    return row


def audit(action, row, by):
    log_events([{"kind": "config", "host": row["host"], "project": row.get("project"),
                 "who": row["holder"], "msg": f"{by}：{action}"}])


def revoke(hid, by):
    lease.need("撤回算力池试点")
    def edit(rows):
        row = rows.get(hid)
        if not row:
            raise ConfigError("没有这台主机的池供给")
        if row.get("attempt") or row.get("daily"):
            row["revoked"] = True
        else:
            rows.pop(hid)
        return row
    with config.HOSTS_LOCK:
        row = fleet.edit_state(FILE, edit)
    audit("撤回算力池供给", row, by)
    return {"awaiting_cleanup": held(hid)}


def finish(row, result, by):
    """只有本次执行端完成回执才解除供给，异常保留给核对入口。"""
    def edit(rows):
        cur = rows.get(row["host"])
        if cur and cur.get("attempt") == row["attempt"] and result.get("finished") is True:
            rows.pop(row["host"])
    fleet.edit_state(FILE, edit)
    audit("试点结束" if result.get("finished") else "试点结果待核对", row, by)


def run(hosts, by):
    lease.need("运行算力池试点")
    if not isinstance(hosts, list) or len(hosts) != 2 or any(not isinstance(h, str) for h in hosts) or len(set(hosts)) != 2:
        raise ConfigError("本次验收要选两台不同的单卡节点")
    rows = {k: v for k, v in load_json(fleet.state_file(FILE), {}).items() if not v.get("daily")}
    try:
        approved = [rows[h] for h in hosts]
    except KeyError as e:
        raise ConfigError("两台节点都必须先批准供给") from e
    if any(r.get("revoked") or r.get("attempt") for r in approved):
        raise ConfigError("试点已撤回或同一尝试已提交")
    with ThreadPoolExecutor(2) as ex:
        facts = list(ex.map(probe, approved))
    now = time.time()
    nodes, jobs = [], []
    spec = hints(POOL_HINTS)
    devices = {n["node"]: [n["cards"][0]["uuid"]] for _, _, n in facts}
    for row, (_, _, n) in zip(approved, facts):
        if n["generation"] != row["generation"] or n["deadline"] < now + RUN_S:
            raise ConfigError("节点已换代或剩余试点窗口太短")
        nodes.append({**n, "observed_at": now})
        # 两个项目都经过真实账户/项目校验。互信试点批准两台都可跑两个固定任务。
        jobs.append(dict(job=row["project"], account=account_project(row["project"], row["account"]),
                         project=row["project"], gpus=1, since=now, eligible_nodes=devices,
                         vram_gib=spec["vram"], ram_gib=spec["ram"], cpu_cores=spec["cpus"], duration_min=spec["est_min"]))
    if len({j["account"] for j in jobs}) != 2:
        raise ConfigError("双人试点要选两个不同试点账号的项目")
    jobs.append({**jobs[0], "job": "同机双卡负例", "gpus": 2})
    plan = pool.plan({"now": now, "nodes": nodes, "jobs": jobs})
    assigned = {x["node"]: x for x in plan["assignments"]}
    if set(assigned) != set(hosts):
        raise ConfigError("试算未给两项单卡作业分别选出可用节点")
    neg = next((r for r in plan["waiting"] if r["job"] == "同机双卡负例"), {})
    if neg.get("reason") != "same_node":
        raise ConfigError("双卡负例未按同一节点拒绝")
    with config.HOSTS_LOCK:
        lease.need("运行算力池试点")
        for row in approved:
            host_of(row["host"], row["holder"])
            account_project(row["project"], row["account"])
        def claim(current):
            if any(current.get(r["host"]) != r for r in approved):
                raise ConfigError("试点供给已变化，请刷新")
            result = []
            for row in approved:
                alloc = assigned[row["host"]]
                a = {**row, "run_project": alloc["project"], "run_account": alloc["account"],
                     "attempt": secrets.token_hex(12), "run_until": now + RUN_S}
                current[row["host"]] = a
                result.append(a)
            return result
        claimed = fleet.edit_state(FILE, claim)
    start = now + 40
    def execute(pair):
        row, (h, regular, _) = pair
        audit("认领单次池试点", row, by)
        try:
            current = load_json(fleet.state_file(FILE), {}).get(row["host"]) or {}
            if current.get("revoked") or current.get("attempt") != row["attempt"]:
                result = {"finished": True, "success": False, "reason": "供给已撤回，未发送任务"}
            else:
                result = remote(h, regular, row["device"], "pool-trial", project=row["run_project"],
                                attempt=row["attempt"], generation=row["generation"], start_at=start, until=row["run_until"])
        except (ConfigError, OSError) as e:
            result = {"error": str(e), "finished": False}
        finish(row, result, by)
        return {"host": row["host"], "project": row["run_project"], "account": row["run_account"],
                "attempt": row["attempt"], "device": row["device"], "generation": row["generation"], **result}
    with ThreadPoolExecutor(2) as ex:
        out = list(ex.map(execute, zip(claimed, facts)))
    cuda = [x["cuda"] for x in out if x.get("success")]
    overlap = max(0, min(x["end"] for x in cuda) - max(x["start"] for x in cuda)) if len(cuda) == 2 else 0
    return {"attempts": out, "same_node": neg["text"], "overlap_s": round(overlap, 3)}


def reconcile(hid, by):
    """超时后核对同一次执行，未清理继续锁住资源。"""
    lease.need("核对算力池试点")
    row = load_json(fleet.state_file(FILE), {}).get(hid)
    if not row or not row.get("attempt"):
        raise ConfigError("这台没有待核对的试点尝试")
    h, regular, _ = host_of(hid, row["holder"], checking=True)
    result = remote(h, regular, row["device"], "pool-status", attempt=row["attempt"], generation=row["generation"], until=row["run_until"])
    finish(row, result, by)
    return result


def supply_daily(data, by):
    """批准一台共享执行节点及整卡名单。授权期限同时是执行硬截止。"""
    lease.need("授权算力池日常供给")
    hid, holder, until, devices = (data.get(k) for k in ("host", "holder", "until", "devices"))
    now = time.time()
    if data.get("agreed") is not True:
        raise ConfigError("请确认持有人同意本次日常供给")
    if not isinstance(devices, list) or not devices or len(devices) > 16 or any(
        not isinstance(d, str) or not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", d) for d in devices
    ) or len(set(devices)) != len(devices):
        raise ConfigError("请选择一组不同的整卡 GPU UUID")
    if type(until) not in (float, int) or not math.isfinite(until) or not now + 180 <= until <= now + 86400:
        raise ConfigError("日常供给时限必须在现在起 3 分钟到 1 天内")
    row = {"host": hid, "holder": holder, "until": until, "devices": devices, "daily": True,
           "supply_id": secrets.token_hex(16), "stage": "preparing"}
    _, _, limit = host_of(hid, holder)
    if until > limit:
        raise ConfigError("日常供给不能晚于租期")
    with config.HOSTS_LOCK:
        host_of(hid, holder)
        def put(rows):
            for key in list(rows):
                if not held(key):
                    rows.pop(key)
            if hid in rows or any(set(devices) & set(supply_devices(old)) for old in rows.values()):
                raise ConfigError("节点或设备已有有效供给")
            rows[hid] = row
        fleet.edit_state(FILE, put)
    audit("批准算力池日常供给", row, by)
    reconcile_daily()
    return load_json(fleet.state_file(FILE), {})[hid]


def reconcile_daily():
    """供给与收回都先持久化意图，再核对执行端；关闭派单时仍继续收尾。"""
    from . import poolqueues
    if not lease.primary():
        return
    for hid, old in load_json(fleet.state_file(FILE), {}).items():
        if not old.get("daily") or old.get("stage") == "released":
            continue
        row = dict(old)
        # 旧日常供给升级时也必须重新关闸；未拿到回执前不进入可派库存。
        row.setdefault("supply_id", secrets.token_hex(16))
        releasing = row.get("revoked") or row["until"] <= time.time()
        if not releasing and old.get("stage") == "ready":
            continue  # 稳定供给由已有库存探测核对，不每轮再次关闸和探测。
        row["stage"] = "releasing" if releasing else "preparing"
        def begin(rows):
            if rows.get(hid) != old:
                return False
            rows[hid] = row
            return True
        if not fleet.edit_state(FILE, begin):
            continue
        try:
            h, regular, _ = host_of(hid, row["holder"], checking=True)
            active = any((q.get("dispatch") or {}).get("node") == hid and
                         q["dispatch"].get("phase") in poolqueues.IN_FLIGHT for q in poolqueues.read().get("queues", {}).values())
            if active:
                continue  # 在途尝试收尾后才恢复专属通道。
            result = workerctl.pool_remote(h, "pool-gate", [json.dumps({"host": hid, "regular": regular,
                "supply_id": row["supply_id"], "action": "open" if releasing else "close"})], timeout=28)
            if releasing:
                if not result.get("released"):
                    raise ConfigError("尚未收到专属通道恢复回执")
                row["stage"] = "released"
            else:
                if not result.get("closed"):
                    raise ConfigError("尚未收到供给闸门关闭回执")
                if active:
                    row["stage"] = "ready"
                else:
                    _, _, node = probe(row)
                    if row.get("generation") and row["generation"] != node["generation"]:
                        raise ConfigError("服务器已换代，请收回后重新确认供给")
                    row.update(stage="ready", generation=node["generation"], model=node["model"])
            row.pop("gate_error", None)
        except (ConfigError, OSError) as e:
            row["gate_error"] = str(e)
        def finish_gate(rows):
            current = rows.get(hid)
            if current and current.get("supply_id") == row["supply_id"]:
                # 撤回可能与远端核对同时发生，不能被旧回执覆盖。
                row["revoked"] = current.get("revoked", False)
                if row["revoked"] and row["stage"] == "ready":
                    row["stage"] = "releasing"
                rows[hid] = row
        fleet.edit_state(FILE, finish_gate)
