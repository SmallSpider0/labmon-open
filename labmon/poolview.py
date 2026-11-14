"""算力池资源读模型与成员管理；实验仍归原项目。"""
import time

from . import config, fleet, users
from .state import load_json, state_dir


def trial() -> dict:
    """试点负责人写入成员与试算输入；未配置时仅管理员能看到空态。"""
    data = load_json(state_dir() / "pool-shadow.json", {})
    return data if isinstance(data, dict) else {}


def allowed(who: users.Ident, data: dict) -> bool:
    members = data.get("members")
    return who.role == "admin" or isinstance(members, list) and who.name in members


def personal_servers(who: users.Ident):
    """当前人员持有的服务器。入池和暂时掉线不改变归属；借入的不算。"""
    cfg = fleet.conf() or {}
    person, error = fleet.account_person(cfg, who.name)
    if error or not person:
        return [], error or "账号尚未关联组内人员"
    rows = {h["id"]: h for h in config.active_hosts()
            if not h.get("borrow") and fleet.host_holder(h, cfg) == person}
    for host in cfg.get("servers", []):
        if host.get("holder") == person and not host.get("returned"):
            rows.setdefault(host["id"], host)
    return list(rows.values()), None if rows else "本阶段仅支持已有个人服务器和共享盘环境的成员"


def member_reason(who: users.Ident, data: dict):
    if not allowed(who, data):
        return "项目主人尚未加入算力池，请联系管理员添加成员"
    _, error = personal_servers(who)
    return error


def set_member(name, included, by):
    """与提交共用队列事务，避免移出成员时新提交穿过检查。"""
    from . import fleet, lease, poolqueues
    from .records import log_events
    from .state import save_json
    lease.need("管理算力池成员")
    if not isinstance(name, str) or not name:
        raise users.Denied(400, "请选择已有账号")
    if type(included) is not bool:
        raise users.Denied(400, "请选择加入或移出")
    account = users.account(name)
    if not account:
        raise users.Denied(404, "账号不存在")
    name = account["name"]
    if not included and account["role"] == "admin":
        raise users.Denied(409, "管理员始终可以管理算力池，无需加入或移出")
    person, error = fleet.account_person(fleet.conf(), name)
    if included and (not person or error):
        raise users.Denied(409, "请先在人员页关联这个账号")
    if included:
        _, reason = personal_servers(poolqueues.pool_identity(name))
        if reason:
            raise users.Denied(409, reason)
    def edit(queues):
        if not included and any(q.get("account") == name and (
                q.get("state") in poolqueues.ACTIVE or
                (q.get("dispatch") or {}).get("phase") in poolqueues.IN_FLIGHT)
                for q in queues.get("queues", {}).values()):
            raise users.Denied(409, "这人还有待位或未收尾的任务，请先到项目处理")
        data = trial()
        members = set(data.get("members", []))
        members.add(name) if included else members.discard(name)
        data["members"] = sorted(members)
        save_json(state_dir() / "pool-shadow.json", data)
    fleet.edit_state(poolqueues.FILE, edit)
    log_events([{"kind": "config", "who": by, "msg": f"{name} {'加入' if included else '移出'}算力池"}])
    return {"msg": "成员已更新"}


def live(who):
    """资源观察不随派单开关切回试算；仅给可见项目的任务摘要。"""
    from . import fleet, pool_dispatch, pool_supply, poolqueues
    from .state import snapshot
    out = pool_dispatch.live_view()
    out['dispatch_enabled'] = pool_dispatch.enabled()
    out['inventory_fresh'] = 0 <= time.time() - out.get('observed', 0) <= config.STALE_S
    board = snapshot()
    supplies = load_json(fleet.state_file(pool_supply.FILE), {})
    queues = poolqueues.read().get('queues', {}).values()
    assignments, waiting = [], []
    for row in queues:
        attempt = row.get('dispatch') or {}
        visible = who.role == 'admin' or row.get('account') == who.name and users.owns(who, row.get('project'))
        if attempt.get('phase') not in poolqueues.IN_FLIGHT:
            if visible and row.get('state') in ('preparing', 'queued'):
                waiting.append({'project': row.get('project'), 'task': attempt.get('task_name') if attempt.get('phase') == 'waiting' else None,
                                'phase': 'preparing' if row['state'] == 'preparing' else 'waiting',
                                'reason': (attempt.get('wait_reason') or {}).get('text') if attempt.get('phase') == 'waiting' else None})
            continue
        assignments.append({'node': attempt.get('node'), 'phase': attempt['phase'],
                            'project': row.get('project') if visible else None,
                            'task': attempt.get('task_name') if visible else None,
                            'devices': attempt.get('devices', [])})
    out['assignments'] = assignments
    out['waiting'] = waiting
    out['nodes'] = [dict({k: node.get(k) for k in ('node', 'physical_host', 'model', 'state', 'total', 'free')}, holder=supplies.get(node['node'], {}).get('holder'),
                         until=supplies.get(node['node'], {}).get('until'),
                         devices=supplies.get(node['node'], {}).get('devices', []),
                         stage=supplies.get(node['node'], {}).get('stage'),
                         error=supplies.get(node['node'], {}).get('gate_error')) for node in out['nodes']]
    stages = {'preparing': 'preparing', 'releasing': 'withdrawing', 'released': 'withdrawn'}
    for node in out['nodes']:
        if node.get('stage') in stages:
            node['state'] = stages[node['stage']]
            node['free'] = 0
    out['capacity'] = {'total': sum(n['total'] for n in out['nodes'] if n['state'] != 'withdrawn'),
                       'free': sum(n['free'] for n in out['nodes']),
                       'max_same_node_free': max((n['free'] for n in out['nodes']), default=0)}
    for node in out['nodes']:
        node['sample'] = gpu_sample(node, board)
    if who.role == 'admin':
        names, _ = users.account_identity()
        members = trial().get('members', [])
        out['members'] = []
        f = board.get('fleet') or {}
        out['candidates'] = []
        seen = set()
        for host in f.get('servers', []) + f.get('mine', []):
            hid = host.get('id')
            if not hid or hid in seen or pool_supply.held(hid):
                continue
            seen.add(hid)
            cards = [dict(uuid=g['uuid'], label=g.get('name') or g['uuid'])
                     for g in (host.get('sample') or {}).get('gpus', []) if g.get('uuid')]
            if cards and host.get('holder') and not host.get('off'):
                out['candidates'].append(dict(host=hid, holder=host['holder'], devices=cards, until=host.get('lease_end')))
        cfg = fleet.conf()
        for name in names:
            person, error = fleet.account_person(cfg, name)
            if person and not error:
                account = users.account(name)
                if not account:
                    continue
                _, reason = personal_servers(poolqueues.pool_identity(name))
                out['members'].append({'eligible': not reason, 'reason': reason, 'name': name, 'person': person, 'admin': account['role'] == 'admin',
                                       'included': name in members})
    return out


def gpu_sample(node, board):
    """只读已有服务器采样，池成员只看供给卡的硬件指标，不带进程或路径。"""
    if node['state'] == 'withdrawn':
        return None
    f = board.get('fleet') or {}
    host = next((h for h in f.get('servers', []) + f.get('mine', []) if h.get('id') == node['node']), {})
    sample = host.get('sample') or {}
    sampled_at = sample.get('t', 0)
    if host.get('mine'):
        # 自用样本无 t；last 是远端时钟，复用采集已校正的 age 换回看板时刻。
        source = next((h for h in board.get('hosts', []) if h['id'] == node['node']), {})
        sampled_at = board.get('generated', 0) - source['age'] if source.get('age') is not None else 0
    if host.get('off') or host.get('state') == 'down' or not 0 <= time.time() - sampled_at <= config.STALE_S:
        return None
    cards = [{k: g.get(k) for k in ('i', 'uuid', 'name', 'util', 'used', 'total')}
             for g in sample.get('gpus', []) if g.get('uuid') in node['devices'] and g.get('util') is not None]
    return {'t': sampled_at, 'state': sample.get('state'), 'gpus': cards} if cards else None
