"""共有型算力池引擎（#212）：多组员共享多台常驻 [[hosts]] 服务器，单一入口（pool / pool:<id>）自动公平调度。

核心机制（彻底与 borrow.py 个人借卡解耦）：
- 配置来自 hosts.toml 的 [pools.<id>]（config.pools，热读取免重启），支持多池、同 worker_home 与单机单池互斥强校验；
- 落机策略：即时落机 + 空闲窃取（Work-Stealing），避免 10 秒 worker 本地放行抢跑饥饿并立刻获得推演 plan ETA；
- 按人公平份额复用 worker.sched.fair_key + pure.person_use，选台打分复用 worker.fit.fit_head + pure.other_load；
- 异常/维护撤离：主机 returned / 移出池 / drain / 预备续租 deadline / 掉线或 worker 失联满 POOL_DOWN_S 自动 reg_move 撤离；
- 权限与隐私隔离：管理员可见所有池与明细，普通池成员仅见所属池聚合概况与本人清单，非池成员不可见池概况。
"""
from __future__ import annotations

import json
import math
import posixpath
import time
from pathlib import Path

from . import config as CF
from . import health as HL
from . import lease as LS
from . import projects as PJ
from . import users as U
from . import workerctl as W
from .records import tell
from .state import load_json, save_json, snapshot, state_dir
from .worker.pure import POOL, is_pool_id, other_load, other_load_text, person_use, pool_host_id, same_list
from .worker.sched import fair_key

POOL_TELL_S = 2 * 3600         # 池待放满 2 小时给实验会话报 pool_wait:（全池 nofit 立即报 pool_wait:...:nofit）
POOL_DOWN_S = 10 * 60          # 池主机连续掉线或 worker 失联满 10 分钟自动撤离待跑清单
POOL_WAIT_S = 10 * 60          # 已落机排队等满 10 分钟进入搬移/空闲窃取候选
POOL_MOVE_COOL_S = 10 * 60     # 单条清单搬移后冷却 10 分钟防乒乓抖动


def key(root: str, jobs: str, pid: str | None = None) -> str:
    """一条池清单的稳定键：传入池 id 时返回 池id|规整目录|规整清单（隔离不同共享盘域的同名路径），未传时返回 规整目录|规整清单。"""
    base = f"{posixpath.normpath(root or '')}|{posixpath.normpath(jobs or '')}"
    return f"{pid}|{base}" if pid else base


def state_file() -> Path:
    return state_dir() / "pool.json"


# ---------- 成员、池主机与权限查询 ----------
def member(who: U.Ident, pid: str | None = None, pool_map: dict[str, dict] | None = None) -> bool:
    """who 是否为共有池成员：pid 给定时核指定池，未给时核是否属于至少一个共有池。管理员对已配置的任意池为真。"""
    pm = CF.pools() if pool_map is None else pool_map
    if pid is not None:
        p = pm.get(pid)
        if not p:
            return False
        if who.role == "admin":
            return True
        low = who.name.lower()
        return "*" in p["members"] or low in p["members"]
    if who.role == "admin":
        return True
    low = who.name.lower()
    return any("*" in p["members"] or low in p["members"] for p in pm.values())


def user_pools(who: U.Ident, pool_map: dict[str, dict] | None = None) -> dict[str, dict]:
    """who 有权使用的所有共有池 {pid: cfg}（管理员为全部，普通用户为自己是成员的池）。"""
    pm = CF.pools() if pool_map is None else pool_map
    if who.role == "admin":
        return dict(pm)
    low = who.name.lower()
    return {pid: p for pid, p in pm.items() if "*" in p["members"] or low in p["members"]}


def user_pool_hosts(who: U.Ident, pool_map: dict[str, dict] | None = None) -> set[str]:
    """who 所属共有池中的所有常驻主机 id 集合（供只读查看与状态/拟合开放）。"""
    return {hid for p in user_pools(who, pool_map).values() for hid in p["hosts"]}


def exclusive_pool(hid: str, pool_map: dict[str, dict] | None = None) -> dict | None:
    """主机 hid 若属于某个 exclusive=True 的共有池，返回该池配置 dict；否则返回 None。"""
    p = CF.host_pool(hid, pool_map)
    return p if p and p.get("exclusive", True) else None


def _reg_pool_id(q: dict) -> str | None:
    """登记条目 q 的所属池 id：字符串原样返回；兼容历史 bool/1 归为默认池 POOL；无为 None。"""
    v = q.get("pool")
    if isinstance(v, str) and v:
        return v
    return POOL if v else None


def locate(root: str, jobs: str, snap: dict | None = None, pid: str | None = None) -> str:
    """`labmon worker rm pool[:<id>]` 定位这条清单此刻登记在哪：返回目标主机 id（池待放伪主机 id 或已落机的常驻主机 id）。"""
    snap = snapshot() if snap is None else snap
    want_phid = pool_host_id(pid) if pid else None
    fallback = want_phid or POOL
    for x in (snap or {}).get("regs") or []:
        xh = x.get("host")
        for q in x.get("queues") or []:
            if not same_list(q, root, jobs):
                continue
            qp = _reg_pool_id(q)
            if pid is not None:
                if xh == want_phid or qp == pid:
                    return xh
            elif is_pool_id(xh) or qp:
                return xh
    return fallback


def reg_home(hid: str, root: str | None = None, jobs: str | None = None, snap: dict | None = None) -> str | None:
    """从快照 regs 里查目标主机/池入口 hid（优先精确匹配 root+jobs）对应的共享域 worker_home；找不到返回 None。
    用于强制删除命名池后，按残留登记的共享域精准代办 worker rm，避免跨共享域误连。"""
    snap = snapshot() if snap is None else snap
    fallback = None
    for x in (snap or {}).get("regs") or []:
        if str(x.get("host") or "") != str(hid):
            continue
        h = x.get("home") or None
        if root and jobs and any(same_list(q, root, jobs) for q in x.get("queues") or []):
            return h
        if fallback is None and h:
            fallback = h
    return fallback


# ---------- 合并各在线 worker 汇报的 pool_heads ----------
def _heads(hosts: list[dict], home: str, hmap: dict[str, dict]) -> dict[tuple[str, str], dict]:
    """合并同一 worker_home 下各在线 worker 的 pool_heads：按 (无 err 优先, pool_t 较晚) 逐条择优（#86、#99 PR #93）。
    兼容新键 '<pool_host_id>|<root>|<jobs>' 与旧两段键 '<root>|<jobs>'。"""
    best: dict[tuple[str, str], tuple[tuple[int, float], dict]] = {}
    wh_norm = home.rstrip("/")
    for e in hosts:
        if e.get("ephemeral") or e.get("error") or not e.get("worker_up"):
            continue
        hc = hmap.get(e["id"])
        if not hc or CF.is_standalone(hc) or hc["worker_home"].rstrip("/") != wh_norm:
            continue
        st = e.get("status") or {}
        ph = st.get("pool_heads")
        if not isinstance(ph, dict):
            continue
        pt = float(st.get("pool_t") or st.get("t") or 0)
        for rk, val in ph.items():
            if not isinstance(val, dict):
                continue
            parts = rk.split("|")
            if len(parts) >= 3 and is_pool_id(parts[0]):
                phid, lk = parts[0], key(parts[1], "|".join(parts[2:]))
            elif len(parts) >= 2:
                phid = val.get("pool") if is_pool_id(val.get("pool")) else POOL
                lk = key(parts[0], "|".join(parts[1:]))
            else:
                continue
            score = (0 if val.get("err") else 1, pt)
            k = (phid, lk)
            if k not in best or score > best[k][0]:
                best[k] = (score, val)
    return {k: v for k, (_, v) in best.items()}


def _has_override(st: dict, kind: str, t: float, *, skip_target: str | None = None) -> bool:
    for o in st.get("overrides") or []:
        if o.get("kind") != kind:
            continue
        if skip_target is not None and o.get("target") == skip_target:
            continue
        until = o.get("until")
        if until is None or until > t:
            return True
    return False


def _proj_member(pid_proj: str, pcfg: dict) -> bool:
    """项目主人是否仍是该共有池成员：归管理员（owner_of 为 None 或管理员账号）的始终为真；普通用户检查 members。"""
    if "*" in pcfg["members"]:
        return True
    owner = U.owner_of(pid_proj)
    if not owner:
        return True
    acc = U.account(owner)
    if not acc or acc.get("role") == "admin":
        return True
    return owner.lower() in pcfg["members"]


def _pool_entry(pcfg: dict, pool_map: dict[str, dict]) -> str:
    """对外展示的提交入口：全配置仅 1 个池时用默认入口 pool，多池时用 pool:<id>（或 pool）。"""
    return POOL if len(pool_map) <= 1 else pcfg["host_id"]


def tell_saved(pcfg: dict, *, create: bool, old_pool: dict | None = None,
               old_pools: dict[str, dict] | None = None, snap: dict | None = None) -> None:
    """共有池新建/修改成功后，经 records.tell 通知相关未归档项目的实验会话（#249）：
    仅通知项目主人为管理员或属于该池 members 的未归档项目，严格不向非成员项目泄露；
    改池移除原成员时，若其名下未归档项目此刻在原池仍有待放或已落机清单，仅发送脱敏提示告知原池已调整。"""
    try:
        pm = CF.pools()
    except CF.ConfigError:
        pm = {pcfg["id"]: pcfg}
    entry = _pool_entry(pcfg, pm)
    action = "新建" if create else "修改"
    hosts_txt = "、".join(pcfg["hosts"])
    if pcfg.get("exclusive", True):
        msg = f"{action}了共有算力池「{pcfg['label']}」（入口 {entry}，主机：{hosts_txt}，独占模式）：后续清单请改投 {entry}"
    else:
        msg = f"{action}了共有算力池「{pcfg['label']}」（入口 {entry}，主机：{hosts_txt}，开放模式）：可经 {entry} 提交，也可直投池内单机"
    active_projs: set[str] = set()
    if not create and old_pool:
        _, _, active_projs = _scan_pool_activity(pcfg["id"], snap=snap, pool_map=old_pools or {pcfg["id"]: old_pool})
    for proj_id in PJ.project_ids():
        if _proj_member(proj_id, pcfg):
            tell(proj_id, "pool", msg)
        elif not create and old_pool and proj_id in active_projs and _proj_member(proj_id, old_pool):
            old_entry = _pool_entry(old_pool, old_pools or {pcfg["id"]: old_pool})
            tell(proj_id, "pool", f"共有算力池「{old_pool['label']}」（{old_entry}）已调整，当前账号已不在该池成员列表，请检查原池待放或在跑清单")


def tell_removed(old_pool: dict, *, old_pools: dict[str, dict] | None = None) -> None:
    """共有池删除成功后，经 records.tell 仅通知原池成员或管理员名下未归档项目（#249）。"""
    old_entry = _pool_entry(old_pool, old_pools or {old_pool["id"]: old_pool})
    hosts_txt = "、".join(old_pool["hosts"])
    msg = f"移除了共有算力池「{old_pool['label']}」（原入口 {old_entry}，原主机：{hosts_txt}）：后续请改投其他可用主机或算力池"
    for proj_id in PJ.project_ids():
        if _proj_member(proj_id, old_pool):
            tell(proj_id, "pool", msg)


def _free_gpu_count(st: dict) -> int:
    """一台在线主机此刻空闲的 GPU 卡数（不含占位与保活）。"""
    gs = (st.get("info") or {}).get("gpus") or []
    if not gs:
        return 0
    busy = ({u for j in st.get("running") or [] if not j.get("hold") for u in j.get("gpus") or []}
            | {u for j in st.get("external") or [] for u in j.get("gpus") or []}
            | {g.get("uuid") for g in st.get("gpu_procs") or [] if g.get("owner") != "keepalive"})
    return sum(1 for g in gs if g.get("uuid") not in busy)


def _default_fit(pcfg: dict, item: dict, eval_hosts: list[str]) -> dict | None:
    """在同域在线主机上调用 worker fit --list --json，评估队首在 eval_hosts 各台上的适配与负载。
    若首台因共享盘临时读失败返回非 0 或无有效 JSON，依次回退到其余候选健康主机重试，避免单机读失败误伤整个池。"""
    if not eval_hosts:
        return None
    hosts_arg = ",".join(eval_hosts)
    for via in eval_hosts:
        rc, out = W.run_out("fit", via, [item["root"], item["jobs"]],
                            project=item["project"], list=True, json=True, hosts=hosts_arg)
        if rc != 0:
            continue
        lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
        for ln in reversed(lines):
            try:
                data = json.loads(ln)
                if isinstance(data, dict) and "hosts" in data:
                    return data
            except ValueError:
                continue
    return None


def _default_move(to: str, item: dict, pid: str, reason: str) -> None:
    from . import serve as SV
    SV.reg_move(to, [{
        "host": item["src"], "root": item["root"], "jobs": item["jobs"],
        "project": item["project"], "stop": item.get("stop") or None,
    }], pool=pid, pool_reason=reason)


def _host_view(hid: str, hmap: dict[str, dict], by_host: dict[str, dict], t: float) -> dict:
    """构造单台池主机在全局快照 pool.pools[].hosts 中的只读摘要。"""
    e = by_host.get(hid)
    hc = hmap.get(hid) or {}
    label = (e or {}).get("label") or hc.get("label") or hid
    returned = bool(hc.get("returned"))
    st = (e or {}).get("status") or {}
    online = bool(e and not returned and not e.get("error") and e.get("worker_up"))
    down = bool(e and not returned and (e.get("error") or not e.get("worker_up")))
    drain = bool(online and _has_override(st, "drain", t))
    deadline = bool(online and _has_override(st, "deadline", t, skip_target="borrow"))
    lease_end = (e or {}).get("lease_end")
    gs = (st.get("info") or {}).get("gpus") or []
    n_gpu = len(gs)
    free_g = _free_gpu_count(st) if online else 0
    ld = other_load(st, None) if online else {"running": 0, "queued": 0, "until": None}
    busy_list = (e or {}).get("busy") or []
    vals = [b.get("h1") if b.get("h1") is not None else b.get("m10") if b.get("m10") is not None else b.get("now")
            for b in busy_list if isinstance(b, dict)]
    vals = [v for v in vals if v is not None]
    util = round(sum(vals) / len(vals), 1) if vals else None
    return {
        "id": hid, "label": label, "returned": returned, "online": online, "down": down,
        "drain": drain, "deadline": deadline, "lease_end": lease_end,
        "gpus": n_gpu, "free_gpus": free_g, "running": ld["running"], "queued": ld["queued"],
        "until": ld["until"], "util": util,
    }


def _est_card_hours(fit_res: dict | None) -> float:
    """估算队首任务的卡·时，用于同轮内存扣减公平份额，防止单人同轮连占多台空闲机。"""
    if not fit_res or not isinstance(fit_res.get("need"), dict):
        return 0.5
    ngpu = float(fit_res["need"].get("ngpu") or 0)
    if ngpu <= 0:
        return 0.0
    dur_min = fit_res.get("dur")
    hours = (float(dur_min) / 60.0) if dur_min and dur_min > 0 else 1.0
    return max(0.1, ngpu * hours)


def _rank_ok(hid: str, info: dict) -> tuple:
    """档 1（ok == True 可立刻开跑）：拥挤度轻 → 预计忙到早 → 空卡多 → 空显存多 → 同项目缓存亲和 → 主机 id。"""
    ld = info.get("load") or {}
    return (int(ld.get("running") or 0) + int(ld.get("queued") or 0),
            float(ld.get("until") or 0.0),
            -int(info.get("free_cards") or 0),
            -float(info.get("free_vram") or 0.0),
            0 if info.get("same_proj") else 1,
            hid)


def _rank_busy(hid: str, info: dict) -> tuple:
    """档 2（全池忙时即时落机）：预计最快空出 → 拥挤度轻 → 空卡多 → 主机 id。"""
    ld = info.get("load") or {}
    u = ld.get("until")
    return (float(u) if u is not None else math.inf,
            int(ld.get("running") or 0) + int(ld.get("queued") or 0),
            -int(info.get("free_cards") or 0),
            hid)


def _dep_wait_why(dep: list, dep_end: float | None) -> str:
    why = dep[1] if len(dep) > 1 and dep[1] else "队首依赖未满足"
    if dep_end and dep_end > 0 and "预计" not in why:
        why += f"（预计 {CF.fmt_time(dep_end)} 满足）"
    return why


# ---------- 每轮调度主循环 ----------
def tick(hosts: list[dict], regs: list[dict], sw: dict | None, t: float, *,
         lan_ok: bool = True, use_ssh: bool = True, move_fn=None, fit_fn=None,
         pool_map: dict[str, dict] | None = None) -> dict | None:
    """共有型算力池每轮调度（overview.collect 调，仅持主租约 LS.primary() 时落机/搬移）：
    返回写进全局快照 snap["pool"] 的字典 {pools: [...], lists: {...}}；未配置任何池返回 None。"""
    if pool_map is None:
        try:
            pool_map = CF.pools()
        except CF.ConfigError as e:
            return {"error": str(e), "pools": [], "lists": {}}
    if not pool_map:
        return None

    sw = sw or {}
    paused_projs = set((sw.get("pause") or {}).keys())
    focus_projs = set((sw.get("focus") or {}).keys())
    all_h = {str(h["id"]): h for h in CF.all_hosts()}
    by_host = {e["id"]: e for e in hosts if not e.get("ephemeral")}
    regs_by = {x["host"]: x for x in regs or []}

    pst = load_json(state_file(), {}) or {}
    wait_since: dict[str, float] = dict(pst.get("wait_since") or {})
    moved_at: dict[str, dict] = dict(pst.get("moved_at") or {})
    down_state: dict[str, dict] = {k: dict(v) for k, v in (pst.get("down_state") or {}).items() if isinstance(v, dict)}

    # 1. 更新池内主机连续失联状态（复用 health.link_step 唯一实现，离网 ok=None 清零未判掉线的计数）
    all_pool_hids = {hid for p in pool_map.values() for hid in p["hosts"]}
    for hid in all_pool_hids:
        e = by_host.get(hid)
        if not e:
            down_state.pop(hid, None)
            continue
        hs = down_state.setdefault(hid, {})
        ok = None if not lan_ok else bool(not e.get("error") and e.get("worker_up"))
        if HL.link_step(hs, ok, t, down_s=POOL_DOWN_S) == "down":
            hs["down"] = f"主机离线/worker 失联满 {POOL_DOWN_S // 60} 分钟"
    down_state = {hid: st for hid, st in down_state.items()
                  if hid in all_pool_hids and (st.get("fails") or st.get("down"))}

    can_act = lan_ok and LS.primary() and use_ssh
    do_fit = fit_fn or _default_fit
    do_move = move_fn or _default_move

    # 每个 worker_home 只合并一次 pool_heads
    heads_by_home: dict[str, dict] = {}
    out_pools: list[dict] = []
    out_lists: dict[str, dict] = {}
    seen_waiting_keys: set[str] = set()

    for pid, pcfg in pool_map.items():
        phid = pcfg["host_id"]
        wh = pcfg["worker_home"]
        if wh not in heads_by_home:
            heads_by_home[wh] = _heads(hosts, wh, all_h)
        hmap = heads_by_home[wh]

        # 区分本池健康候选主机与需撤离主机
        healthy: list[str] = []
        evac_hosts: dict[str, str] = {}
        for hid in pcfg["hosts"]:
            e = by_host.get(hid)
            hc = all_h.get(hid) or {}
            if hc.get("returned") or hid not in pcfg["active_hosts"]:
                evac_hosts[hid] = "主机已归还"
                continue
            if not e:
                evac_hosts[hid] = "主机不在用"
                continue
            if e.get("error") or not e.get("worker_up"):
                if (down_state.get(hid) or {}).get("down"):
                    evac_hosts[hid] = down_state[hid]["down"]
                continue
            st = e.get("status") or {}
            if _has_override(st, "drain", t):
                evac_hosts[hid] = "主机处于 drain 状态"
                continue
            if _has_override(st, "deadline", t, skip_target="borrow"):
                evac_hosts[hid] = "主机预备续租截止"
                continue
            healthy.append(hid)

        has_idle_host = any(_free_gpu_count((by_host[h].get("status") or {})) > 0 for h in healthy)

        # 收集本池共享盘最新 share（供公平份额 fair_key）
        pool_sts = [(by_host[h].get("status") or {}) for h in pcfg["active_hosts"]
                    if h in by_host and (by_host[h].get("status") or {}).get("share")]
        sh = max(pool_sts, key=lambda s: s.get("t", 0))["share"] if pool_sts else {}
        pol = {"own": sw.get("owner") or {}, "w": {**(sw.get("weight") or {}), **(sh.get("weight") or {})}}
        use = dict(sh.get("use") or {})

        cands: list[dict] = []
        retreats: list[tuple[dict, str]] = []
        pending_count = 0
        placed_count = 0

        # A. 收集待放目录清单
        for q in (regs_by.get(phid) or {}).get("queues") or []:
            root, jobs, proj = q.get("root"), q.get("jobs"), q.get("project")
            if not (root and jobs and proj):
                continue
            rk = key(root, jobs)
            lk = key(root, jobs, pid)
            seen_waiting_keys.add(lk)
            hi = hmap.get((phid, rk)) or (hmap.get((POOL, rk)) if pid == POOL else None)
            since = wait_since.setdefault(lk, t)
            rec = {
                "pool": pid, "pool_host": phid, "project": proj, "root": root, "jobs": jobs,
                "head": (hi or {}).get("head"), "n": (hi or {}).get("n") or 0,
                "since": round(since), "why": "", "nofit": False, "done": False,
                "sleeping": bool((hi or {}).get("sleeping")),
                "dep": (hi or {}).get("dep"), "dep_end": (hi or {}).get("dep_end"),
                "elsewhere": (hi or {}).get("elsewhere"), "held": (hi or {}).get("held"),
            }
            if not _proj_member(proj, pcfg):
                rec["why"] = "项目主人已不在本池成员列表"
                out_lists[lk] = rec
                pending_count += 1
                continue
            if hi is None:
                rec["why"] = "等待同域在线 worker 读取池清单"
                out_lists[lk] = rec
                pending_count += 1
                continue
            if hi.get("err"):
                rec["why"] = hi["err"]
                out_lists[lk] = rec
                pending_count += 1
                continue
            if hi.get("sleeping") or not hi.get("n"):
                rec["done"] = True
                wait_since.pop(lk, None)
                out_lists[lk] = rec
                continue
            pending_count += 1
            if hi.get("held") or proj in paused_projs:
                rec["why"] = "清单或项目已暂停"
                out_lists[lk] = rec
                continue
            if hi.get("elsewhere"):
                eh, ej = hi["elsewhere"][0], hi["elsewhere"][1]
                rec["why"] = f"上一格 {ej} 仍在 {eh} 收尾，完成后落机"
                out_lists[lk] = rec
                continue
            if hi.get("dep"):
                rec["why"] = _dep_wait_why(hi["dep"], hi.get("dep_end"))
                out_lists[lk] = rec
                continue
            out_lists[lk] = rec
            cands.append({
                "lk": lk, "src": phid, "root": root, "jobs": jobs, "project": proj,
                "stop": q.get("stop"), "head": hi.get("head"), "since": since,
                "pending": True, "back": False, "orig_nofit": False, "reason": "新清单落机",
            })

        # B. 收集已落机到各主机、显式标记属于本池的清单
        for x in regs or []:
            xh = x.get("host")
            if not xh or xh == phid or is_pool_id(xh) or x.get("queues") is None:
                continue
            for q in x["queues"]:
                qp = _reg_pool_id(q)
                if qp != pid:
                    continue
                root, jobs, proj = q.get("root"), q.get("jobs"), q.get("project")
                if not (root and jobs and proj):
                    continue
                if q.get("empty") or q.get("sleeping"):
                    continue
                placed_count += 1
                if proj in paused_projs:
                    continue
                lk = key(root, jobs, pid)
                e = by_host.get(xh)
                st = (e or {}).get("status") or {}
                busy = any(same_list({"root": j.get("root"), "jobs": j.get("queue")}, root, jobs)
                           for j in (st.get("running") or []) + (st.get("external") or []))
                ws = [w for w in st.get("waiting") or []
                      if same_list({"root": w.get("root"), "jobs": w.get("queue")}, root, jobs)]
                # 判断是否需撤离（移出池 / returned / drain / 预备续租 deadline / 掉线满 POOL_DOWN_S）
                evac_why = None
                if xh not in pcfg["hosts"]:
                    evac_why = f"主机 {xh} 已移出算力池"
                elif xh in evac_hosts:
                    if evac_hosts[xh] == "主机预备续租截止":
                        if any(w.get("held") == "deadline" for w in ws) or not busy:
                            evac_why = evac_hosts[xh]
                    else:
                        evac_why = evac_hosts[xh]
                if evac_why:
                    cands.append({
                        "lk": lk, "src": xh, "root": root, "jobs": jobs, "project": proj,
                        "stop": q.get("stop"), "head": next((w.get("name") for w in ws if w.get("name")), None),
                        "since": min((w.get("since") or t for w in ws if w.get("name")), default=t),
                        "pending": False, "back": True, "orig_nofit": False, "reason": evac_why,
                    })
                    continue
                # 健康池主机上的清单：只在无在跑任务时考虑退回或空闲窃取
                if busy or xh not in healthy:
                    continue
                if any(w.get("held") in ("paused", "pause") or w.get("dep") for w in ws):
                    continue
                # 刚搬来但出现 elsewhere：退回池待放目录等原机收尾
                if any("还在主机" in (w.get("why") or "") or "别的主机" in (w.get("why") or "") for w in ws):
                    retreats.append(({
                        "lk": lk, "src": xh, "root": root, "jobs": jobs, "project": proj, "stop": q.get("stop"),
                    }, "原主机上一格未收尾，暂退回待放目录"))
                    continue
                # 搬移冷却防抖
                last_mv = moved_at.get(lk)
                if last_mv and t - float(last_mv.get("t") or 0) < POOL_MOVE_COOL_S:
                    continue
                w0 = next((w for w in ws if w.get("name")), None)
                if not w0:
                    continue
                why_s = w0.get("why") or ""
                plan_stuck = any(same_list({"root": ln.get("root"), "jobs": ln.get("queue")}, root, jobs) and ln.get("stuck")
                                 for ln in (st.get("plan") or {}).get("lanes") or [])
                orig_nofit = ("放不下" in why_s or "这台跑不了" in why_s or bool(plan_stuck))
                waited = (t - float(w0["since"])) if w0.get("since") else 0.0
                if orig_nofit or has_idle_host or waited >= POOL_WAIT_S:
                    cands.append({
                        "lk": lk, "src": xh, "root": root, "jobs": jobs, "project": proj,
                        "stop": q.get("stop"), "head": w0.get("name"), "since": float(w0.get("since") or t),
                        "pending": False, "back": False, "orig_nofit": orig_nofit,
                        "reason": f"{xh} 放不下队首，重新选台" if orig_nofit else f"空闲窃取/排队搬移（原在 {xh}）",
                    })

        # 执行退回待放（elsewhere 暂避）
        if can_act:
            for item, rsn in retreats:
                try:
                    do_move(phid, item, pid, rsn)
                    moved_at[item["lk"]] = {"t": round(t), "host": phid}
                except CF.ConfigError:
                    pass

        # ② 按人公平份额排序 + ③ 选台打分落机/搬移
        picked: set[str] = set()
        left = list(cands)
        while can_act and left:
            tot = person_use(pol, use)
            idx = min(range(len(left)), key=lambda i: (
                not left[i]["back"],
                fair_key({"project": left[i]["project"], "since": left[i]["since"]}, focus_projs, pol, use, tot),
            ))
            it = left.pop(idx)
            avail = [h for h in healthy if h not in picked and h != it["src"]]
            if not avail:
                if it["back"] and it["src"] != phid:
                    try:
                        do_move(phid, it, pid, f"{it['reason']}，池内暂无其他健康主机，暂退回待放")
                        moved_at[it["lk"]] = {"t": round(t), "host": phid}
                        wait_since.setdefault(it["lk"], t)
                    except CF.ConfigError:
                        pass
                elif it["orig_nofit"] and it["src"] != phid and it["src"] in healthy and not (picked & (set(healthy) - {it["src"]})):
                    # 单健康机池且无其他候选被 picked 排除：经 do_fit 复核原机确认硬件 nofit 后退回待放，
                    # 避免清单永久滞留无法运行的原机、漏发 pool_wait:...:nofit 告警。
                    res0 = do_fit(pcfg, it, [it["src"]])
                    if res0 and ((res0.get("hosts") or {}).get(it["src"]) or {}).get("nofit"):
                        try:
                            do_move(phid, it, pid, f"{it['reason']}，全池暂无放得下的主机，退回待放")
                            moved_at[it["lk"]] = {"t": round(t), "host": phid}
                            wait_since.setdefault(it["lk"], t)
                        except CF.ConfigError:
                            pass
                elif it["pending"] and it["lk"] in out_lists:
                    out_lists[it["lk"]]["why"] = "池内暂时没有在线且健康的候选主机" if not healthy else "本轮候选主机已各落 1 条，下一轮继续"
                continue
            eval_hosts = list(avail)
            if not it["pending"] and not it["back"] and it["src"] in healthy and it["src"] not in eval_hosts:
                eval_hosts.append(it["src"])
            res = do_fit(pcfg, it, eval_hosts)
            if not res or res.get("head") is None:
                if it["pending"] and it["lk"] in out_lists:
                    if res and res.get("head") is None:
                        out_lists[it["lk"]]["done"] = True
                        wait_since.pop(it["lk"], None)
                    else:
                        out_lists[it["lk"]]["why"] = "这一轮评估主机负载失败，下一轮重试"
                continue
            h_res = res.get("hosts") or {}
            if not it["pending"] and not it["back"] and it["src"] in h_res:
                if h_res[it["src"]].get("nofit"):
                    it["orig_nofit"] = True
            cand_evals = {h: h_res[h] for h in avail if h in h_res and not h_res[h].get("skip")}
            ok_hosts = {h: v for h, v in cand_evals.items() if v.get("ok")}
            target = None
            tier = None
            if ok_hosts:
                target = min(ok_hosts, key=lambda h: _rank_ok(h, ok_hosts[h]))
                tier = "ok"
            else:
                # 档 2：全池忙
                if not it["pending"] and not it["back"] and not it["orig_nofit"]:
                    continue                                  # 已在健康机排队且非 nofit：不搬防抖动
                fit_hosts = {h: v for h, v in cand_evals.items() if not v.get("nofit")}
                if fit_hosts:
                    target = min(fit_hosts, key=lambda h: _rank_busy(h, fit_hosts[h]))
                    tier = "busy"
            if target:
                rsn = it["reason"] + ("（立刻可放行）" if tier == "ok" else "（全池忙，即时落到预计最快空出主机）")
                try:
                    do_move(target, it, pid, rsn)
                except CF.ConfigError as e:
                    if it["pending"] and it["lk"] in out_lists:
                        out_lists[it["lk"]]["why"] = f"落机失败：{e}"
                    continue
                picked.add(target)
                moved_at[it["lk"]] = {"t": round(t), "host": target}
                use[it["project"]] = use.get(it["project"], 0.0) + _est_card_hours(res)
                if it["pending"]:
                    wait_since.pop(it["lk"], None)
                    out_lists.pop(it["lk"], None)
                    pending_count = max(0, pending_count - 1)
                    placed_count += 1
            else:
                # 仅当本轮覆盖了全部健康主机（未被 picked 排除也未 skip）且均确认 nofit 时，才标记全池放不下；
                # 否则只是本轮大显存/候选主机已接 1 条或暂跳过，留待下一轮继续评估，不提前触发 pool_wait:...:nofit 告警。
                others = [h for h in healthy if h != it["src"]]
                all_healthy_nofit = (
                    bool(others)
                    and all(h in cand_evals and cand_evals[h].get("nofit") for h in others)
                    and (it["src"] not in healthy or it["orig_nofit"])
                )
                why_all = "；".join(f"{h}：{v.get('why') or '放不下'}" for h, v in sorted(cand_evals.items())) or "池内所有健康主机均放不下队首"
                if it["pending"] and it["lk"] in out_lists:
                    if all_healthy_nofit:
                        out_lists[it["lk"]]["why"] = why_all
                        out_lists[it["lk"]]["nofit"] = True
                    else:
                        out_lists[it["lk"]]["why"] = "本轮其余候选主机已落机或暂跳过，下一轮继续评估"
                        out_lists[it["lk"]]["nofit"] = False
                elif (it["back"] or (it["orig_nofit"] and all_healthy_nofit)) and it["src"] != phid:
                    try:
                        msg_back = (f"{it['reason']}，全池暂无放得下的主机，退回待放"
                                    if all_healthy_nofit else f"{it['reason']}，本轮其余健康主机已落机，暂退回待放")
                        do_move(phid, it, pid, msg_back)
                        moved_at[it["lk"]] = {"t": round(t), "host": phid}
                        wait_since.setdefault(it["lk"], t)
                    except CF.ConfigError:
                        pass

        hviews = [_host_view(hid, all_h, by_host, t) for hid in pcfg["hosts"]]
        out_pools.append({
            "id": pid,
            "host_id": phid,
            "label": pcfg["label"],
            "exclusive": pcfg["exclusive"],
            "members": list(pcfg["members"]),
            "hosts": hviews,
            "online": sum(1 for h in hviews if h["online"]),
            "total": sum(1 for h in hviews if not h["returned"]),
            "gpus": sum(h["gpus"] for h in hviews if h["online"]),
            "free_gpus": sum(h["free_gpus"] for h in hviews if h["online"]),
            "running": sum(h["running"] for h in hviews if h["online"]),
            "queued": sum(h["queued"] for h in hviews if h["online"]),
            "pending": pending_count,
            "placed": placed_count,
        })

    # 持久化状态（修剪已离开待放目录的 wait_since 与超过 24 小时的 moved_at）
    wait_since = {k: v for k, v in wait_since.items() if k in seen_waiting_keys}
    moved_at = {k: v for k, v in moved_at.items() if t - float((v or {}).get("t") or 0) <= 86400}
    if LS.primary():
        save_json(state_file(), {"t": round(t), "wait_since": wait_since, "moved_at": moved_at, "down_state": down_state})
    return {"pools": out_pools, "lists": out_lists}


def _scan_pool_activity(pid: str, *, snap: dict | None = None,
                        pool_map: dict[str, dict] | None = None) -> tuple[int, int, set[str]]:
    """共有算力池 pid 未完成待放与已落机清单的唯一扫描实现：返回 (待放条数, 已落机条数, 涉及项目集合)。"""
    snap = snapshot() if snap is None else snap
    if not isinstance(snap, dict):
        return 0, 0, set()
    if pool_map is None:
        try:
            pool_map = CF.pools()
        except CF.ConfigError:
            pool_map = {}
    pcfg = pool_map.get(pid)
    phid = pcfg["host_id"] if pcfg else (POOL if pid == POOL else f"pool:{pid}")
    match_phids = {phid}
    if pid == POOL or len(pool_map) <= 1:
        match_phids.add(POOL)
    pend_keys: set[str] = set()
    done_keys: set[str] = set()
    projs: set[str] = set()
    for v in ((snap.get("pool") or {}).get("lists") or {}).values():
        if isinstance(v, dict) and v.get("pool") == pid and v.get("root") and v.get("jobs"):
            rk = key(v["root"], v["jobs"])
            if v.get("done") or v.get("sleeping"):
                done_keys.add(rk)
            else:
                pend_keys.add(rk)
                if v.get("project"):
                    projs.add(str(v["project"]))
    placed = 0
    for x in snap.get("regs") or []:
        xh = str(x.get("host") or "")
        qs = x.get("queues") or []
        if xh in match_phids:
            for q in qs:
                if q.get("root") and q.get("jobs") and not q.get("empty") and not q.get("sleeping"):
                    rk = key(q["root"], q["jobs"])
                    if rk not in done_keys:
                        pend_keys.add(rk)
                        if q.get("project"):
                            projs.add(str(q["project"]))
            continue
        if is_pool_id(xh):
            continue
        for q in qs:
            if _reg_pool_id(q) == pid and not q.get("empty") and not q.get("sleeping"):
                placed += 1
                if q.get("project"):
                    projs.add(str(q["project"]))
    return len(pend_keys), placed, projs


def pool_activity(pid: str, *, snap: dict | None = None, pool_map: dict[str, dict] | None = None) -> dict:
    """统计共有算力池 pid 当前未完成的待放清单与已落机活跃清单条数（remove_pool 与 manage_data 共用）。"""
    pending, placed, _ = _scan_pool_activity(pid, snap=snap, pool_map=pool_map)
    return {"pending": pending, "placed": placed}


def manage_data(snap: dict | None = None) -> dict:
    """管理员 GUI 新建/编辑算力池所需的候选数据（GET /api/pools/options，仅内网管理员调用，绝不下发给普通用户）。
    实时读取 hosts.toml 与 users.json，返回当前池定义、候选 [[hosts]]（含 worker_home 与已归属池）与已开通普通账号列表。"""
    snap = snapshot() if snap is None else snap
    hf = CF.hosts_file()
    raw = CF._read_toml(hf) if hf.exists() else {}
    pool_error = None
    try:
        pm = CF.pools(raw)
    except CF.ConfigError as e:
        pm, pool_error = {}, str(e)
    host_to_pool: dict[str, dict] = {}
    for pcfg in pm.values():
        for hid in pcfg["hosts"]:
            host_to_pool[hid] = pcfg
    hosts_out: list[dict] = []
    for raw_h in raw.get("hosts") or []:
        if not isinstance(raw_h, dict) or not raw_h.get("id"):
            continue
        h = dict(raw_h)
        if h.get("borrow_until"):
            h["borrow"] = {"holder": h.get("borrow_from"), "swap": True}
        hd = CF.host_defaults(h)
        hid = hd["id"]
        returned = bool(hd.get("returned"))
        standalone = CF.is_standalone(hd)
        borrowed = bool(hd.get("borrow"))
        shared_ok = CF.share_host(hd)
        why = ""
        if returned:
            why = "主机已归还"
        elif borrowed:
            why = "借用主机不可入池"
        elif not shared_ok:
            why = "本机或回环 SSH 不可入池"
        elif standalone:
            why = "独立主机不共享集群 worker_home，不可入池"
        owner_pool = host_to_pool.get(hid)
        hosts_out.append({
            "id": hid,
            "label": hd.get("label") or hid,
            "worker_home": hd["worker_home"],
            "returned": returned,
            "standalone": standalone,
            "borrow": borrowed,
            "share_host": shared_ok,
            "eligible": not why,
            "why": why,
            "pool_id": owner_pool["id"] if owner_pool else None,
            "pool_label": owner_pool["label"] if owner_pool else None,
        })
    pools_out: list[dict] = []
    for pid, pcfg in pm.items():
        act = pool_activity(pid, snap=snap, pool_map=pm)
        pools_out.append({
            "id": pid,
            "host_id": pcfg["host_id"],
            "label": pcfg["label"],
            "hosts": list(pcfg["hosts"]),
            "members": list(pcfg["members"]),
            "exclusive": bool(pcfg["exclusive"]),
            "worker_home": pcfg["worker_home"],
            "pending": act["pending"],
            "placed": act["placed"],
        })
    users_map = U._read()["users"]
    links = U.people_of(users_map)
    users_out = [
        {"name": k, "fleet_person": links.get(k, (None, None))[0]}
        for k, u in sorted(users_map.items(), key=lambda x: x[0].lower())
        if u.get("role") != "admin"
    ]
    return {"pools": pools_out, "hosts": hosts_out, "users": users_out, "error": pool_error}


# ---------- 权限裁剪与命令行状态排版 ----------
def summary(snap_pool: dict | None, who: U.Ident, acc: U.Access | None = None,
            pool_map: dict[str, dict] | None = None) -> dict | None:
    """按查看者身份投影全局快照的 pool 字段（users.view 唯一实现）：
    - 管理员原样看到所有共有池、成员列表、各主机明细与全部待放清单；
    - 普通池成员仅看到所属共有池聚合概况与池内主机只读摘要（剥离 members 列表），待放清单仅留本人项目；
    - 非池成员返回 None（不可见池概况）。"""
    if not isinstance(snap_pool, dict):
        return None
    if who.role == "admin":
        return snap_pool
    if snap_pool.get("error"):
        return None
    pm = CF.pools() if pool_map is None else pool_map
    allowed_ids = set(user_pools(who, pm))
    if not allowed_ids:
        return None
    acc = acc or U.access(who)
    pools_out = [{k: v for k, v in p.items() if k != "members"}
                 for p in snap_pool.get("pools") or [] if p.get("id") in allowed_ids]
    if not pools_out:
        return None
    lists_out = {k: v for k, v in (snap_pool.get("lists") or {}).items()
                 if v.get("pool") in allowed_ids and v.get("project") in acc.projects}
    return {"pools": pools_out, "lists": lists_out}


def status_text(pcfg: dict, snap: dict | None, *, own: bool = False, projects: set[str] | None = None, now: float | None = None) -> str:
    """`labmon worker status pool[:<id>]` 排版输出：池摘要、各主机在线/空卡/负载、待放清单与原因、当前份额摘要。
    own=True（普通用户）时只列本人项目的待放清单，主机负载用脱敏 other_load_text，绝不泄露他人项目名。"""
    now = time.time() if now is None else now
    snap = snap or {}
    pid, phid = pcfg["id"], pcfg["host_id"]
    mode = "独占（仅经池入口提交）" if pcfg.get("exclusive", True) else "开放（允许直投单台）"
    lines = [f"算力池 {pcfg['label']}（{phid}，{mode}）：共 {len(pcfg['active_hosts'])} 台在用主机"]
    by_host = {e["id"]: e for e in snap.get("hosts") or [] if not e.get("ephemeral")}
    for hid in pcfg["hosts"]:
        e = by_host.get(hid)
        if not e:
            lines.append(f"  主机 {hid}：未在用或已归还")
            continue
        if e.get("error"):
            lines.append(f"  主机 {hid}：连不上（{e['error']}）")
            continue
        if not e.get("worker_up"):
            lines.append(f"  主机 {hid}：worker 未运行")
            continue
        st = e.get("status") or {}
        gs = (st.get("info") or {}).get("gpus") or []
        free_g = _free_gpu_count(st)
        drain = "，drain 中" if _has_override(st, "drain", now) else ""
        ld = other_load(st, None)
        ld_txt = other_load_text(ld, now=now, show_until=True) or "空闲"
        lines.append(f"  主机 {hid}：在线，空 {free_g}/{len(gs)} 张卡{drain}；{ld_txt}")
    pl = ((snap.get("pool") or {}).get("lists") or {})
    pending = [v for v in pl.values() if v.get("pool") == pid and not v.get("done")
               and (not own or v.get("project") in (projects or set()))]
    if pending:
        lines.append(f"待放清单（{len(pending)} 条）：")
        for v in pending:
            waited = CF.fmt_dur(max(0.0, now - float(v["since"]))) if v.get("since") else ""
            why = v.get("why") or "等待下一轮落机"
            tag = " [放不下]" if v.get("nofit") else ""
            lines.append(f"  {v['project']} {v['root']}/{v['jobs']}（队首 {v.get('head') or '—'}，等 {waited or '—'}）：{why}{tag}")
    else:
        lines.append("待放清单：无")
    sc = snap.get("sched") or {}
    projs = sc.get("projects") or []
    if projs:
        if own:
            mine = [p for p in projs if p.get("id") in (projects or set())]
            if mine:
                bits = "；".join(f"{p['id']} 近期 {p.get('use', 0):.1f} 卡·时（占 {p.get('cards', 0):g} 卡）" for p in mine)
                lines.append(f"本人项目份额：{bits}")
        else:
            top = sorted(projs, key=lambda p: -(p.get("use") or 0))[:6]
            bits = "；".join(f"{p.get('person') or p['id']} {p.get('use', 0):.1f} 卡·时" for p in top)
            lines.append(f"近期份额（半衰期 {sc.get('half_life_h', 24):g}h）：{bits}")
    return "\n".join(lines) + "\n"
