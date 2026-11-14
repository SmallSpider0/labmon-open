"""组内服务器的开机参数：人 × 池 × 端口（见 docs/fleet.md「人员、端口与模板作业」「关机、开机与换人」）。

- 人：一份名单 people.json，每人一份设置（镜像、启动命令、备注），提交时就用它，没有别的优先级。
  占着服务器的人第一次读到他现有作业的克隆配置时自动记进名单（只记一次，之后照名单）；新的人按「新的人默认」（defaults.json，
  没存过就取各台作业里最常见的镜像与启动命令）预填，存下来就是这人的设置。名单外的名字不列，要用就在页面上加。占着服务器的人不能删。
- 名字与作业名分开：作业名缺省就是名字，同名作业已在跑（比如自己已有 ALICE）就加后缀 ALICE-2，记进 [[fleet.servers]] 的 job。
- 池：模板作业 = boot.json 里这个池最近一份克隆配置（重开一台、换人时先用这台自己的），给出 poolId、规格、挂载卷、启动命令与环境变量。
  与平台核对时顺带读回资源池、规格（空卡、能否排队）、存储卷与镜像，存进 pools.json，供页面预览与提交前核对。
- 端口：换人沿用原来那台的端口；开新的在 [fleet] port_range（缺省 15151–15199，须在平台规定的 10000–20000 内）里
  挑最小的空闲端口：这个池的节点上，登记的服务器、自用主机、平台上运行中的作业都没占，本机连过去也被拒（别的账号的容器也避开）。

提交前逐项核对（check）：作业名、端口、规格属于这个池且有空卡、挂载卷属于账号、镜像在仓库里。
平台返回的字段是照平台前端代码推的：认得出格式而查不到的算不合法（不提交），格式认不出的只提示“没核对上”。
"""
from __future__ import annotations

import copy
import os
import re
from collections import Counter
import socket
import time

from . import fleet as F
from . import platform as P
from .config import active_hosts, all_hosts, fmt_dur, parse_time
from .records import log_events
from .state import load_json, save_json

PORT_RANGE = (15151, 15199)


# ---------- 人 ----------
def image_of(e: dict) -> str | None:
    c = e.get("clone") or {}
    return ((c.get("cloneTaskRoleVOList") or [{}])[0]).get("imageName") or e.get("image")


def mine(cfg: dict | None, hosts: list[dict] = ()) -> list[dict]:
    """自用主机（主机清单写了 platform_job、platform_pool 的）当成组内的一台：换人、关机、续租、配额与组内页列表里的一行都用它。
    holder 是这台自用主机的人员归属（fleet.host_holder），job 是平台作业名；known_hosts 是拉起 worker 的 ssh 用的那份（forget 删旧密钥、fleet._ssh_cmd 等 ssh 时记新密钥都用它，#109）。
    hosts：工作台快照里的主机，给了就带上状态。两种口径分开：
    - state、work 给平台操作（fleetops._probe_mine → _need_ack）与页面的红框、勾选：worker 的任务在跑（含只用 CPU 的）算 job、
      labmon 之外的 GPU 进程算 unknown（与组内服务器同口径：勾「已说好」才续租、关机、换人）、只有保活算 keepalive、没状态是 None（没探到）。
    - sample：与探针的 analyze 同形的样本（fleet.from_worker：使用记录、组内页的卡片与抽屉都读它），只在本机在内网、worker 状态新鲜且没掉线时有。
    - lw：本地盘开机以来的新写入（fleet.local_writes，overview._annotate 算好放在快照的主机上；续租、关机、换人的确认看它，#149）。
    其余字段与 fleetview.view 的 servers 同名（last、error、down、since、started、public），组内页抽屉同一套模板（#58）。"""
    snap = {e["id"]: e for e in hosts}
    priv = (cfg or {}).get("private_pools") or []
    out = []
    for h in active_hosts():
        if not (h.get("platform_job") and h.get("platform_pool") and h.get("ssh")):
            continue
        e = snap.get(h["id"]) or {}
        st, hl = e.get("status") or {}, e.get("health") or {}
        owners = {p.get("owner") for p in st.get("gpu_procs") or []}
        state = "down" if hl.get("state") == "down" else "job" if "job" in owners or st.get("running") \
            else "unknown" if "other" in owners else "keepalive" if "keepalive" in owners else "idle" if st else None
        fresh = bool(st) and state != "down" and e.get("worker_up") and not e.get("lan_off")
        error = None if fresh or state == "down" or not e else "本机不在内网" if e.get("lan_off") \
            else e.get("error") or e.get("status_error") or (f"worker 已停（{e['stopped']}）" if e.get("stopped") else
                                                              f"worker 状态 {fmt_dur(e['age'])} 没更新" if e.get("age") is not None else "worker 没有状态")
        name = F.host_holder(h, cfg)
        out.append({"id": h["id"], "holder": name, "job": str(h["platform_job"]), "label": h.get("label", h["id"]), "pool": h["platform_pool"],
                    "public": h["platform_pool"] not in priv, "ssh": h["ssh"], "port": int(h["port"]), "mine": True,
                    "known_hosts": h.get("known_hosts") or os.path.expanduser("~/.ssh/known_hosts"),
                    "state": state, "lease_end": e.get("lease_end"),
                    "work": [{"kind": "job", "label": j.get("name"), "mb": None} for j in st.get("running") or []]
                    + [{"kind": "unknown", "label": p.get("comm") or "labmon 之外的进程", "mb": p.get("mb")} for p in st.get("gpu_procs") or [] if p.get("owner") == "other"],
                    "last": st.get("t"), "error": error, "down": hl.get("why") or "连不上" if state == "down" else None,
                    "since": hl.get("since") if state == "down" else None, "started": (st.get("info") or {}).get("started"),
                    "sample": F.from_worker(st, cfg, name) if fresh else None, "lw": e.get("lw")})
    return out


def slots(cfg: dict | None) -> list[dict]:
    """能换人、关机的服务器：组内服务器（含关了机的）加自用主机。"""
    return list((cfg or {}).get("servers") or []) + mine(cfg)


def run_cmd_of(e: dict) -> str | None:
    return ((e.get("clone") or {}).get("cloneTaskRoleVOList") or [{}])[0].get("runCmd")


def _from_jobs() -> dict:
    """各人现有作业的镜像与启动命令：{使用人: {"image", "run_cmd"}}，同一人取最近的一份；没有克隆配置的不算。"""
    out = {}
    for _, e in sorted(load_json(F.state_file("boot.json"), {}).items(), key=lambda kv: kv[1].get("t", 0)):
        if e.get("holder") and e.get("clone") and not e.get("off"):
            out[e["holder"]] = {"image": image_of(e), "run_cmd": run_cmd_of(e)}
    return out


def defaults() -> dict:
    """新的人默认的镜像与启动命令：页面上存过的（defaults.json），否则取各台作业里最常见的。"""
    d = load_json(F.state_file("defaults.json"), {})
    if d.get("image"):
        return {"image": d["image"], "run_cmd": d.get("run_cmd"), "saved": True}
    es = [e for e in load_json(F.state_file("boot.json"), {}).values() if e.get("clone")]
    top = lambda xs: Counter(x for x in xs if x).most_common(1)[0][0] if any(xs) else None  # noqa: E731
    return {"image": top([image_of(e) for e in es]), "run_cmd": top([run_cmd_of(e) for e in es]), "saved": False}


def _check_image(img: str | None) -> None:
    if not img:
        raise P.PlatformError("镜像不能空")
    if re.search(r"\s", img) or ":" not in img.rsplit("/", 1)[-1]:
        raise P.PlatformError(f"镜像要写全（仓库地址/项目/名字:标签），不能有空格：{img}")


def set_defaults(image: str, run_cmd: str, t: float | None = None) -> dict:
    image, run_cmd = (image or "").strip(), (run_cmd or "").strip()
    _check_image(image)
    if not run_cmd:
        raise P.PlatformError("启动命令不能空")
    old = defaults()
    F.state_dir().mkdir(parents=True, exist_ok=True)
    save_json(F.state_file("defaults.json"), {"image": image, "run_cmd": run_cmd, "t": round(time.time() if t is None else t)})
    ch = _changes(old, {"image": image, "run_cmd": run_cmd})
    if ch:
        log_events([{"kind": "config", "msg": f"新的人默认：{ch}"}], t)
    return defaults()


def _changes(old: dict, new: dict) -> str:
    """人员设置改了哪几项（给记录看）：镜像、启动命令、备注各写旧 → 新。"""
    names = {"image": "镜像", "run_cmd": "启动命令", "note": "备注"}
    return "；".join(f"{n} {old.get(k) or '空'} → {new.get(k) or '空'}" for k, n in names.items() if k in new and (old.get(k) or None) != (new.get(k) or None))


def _seed(cfg: dict | None) -> dict:
    """占着服务器而名单里还没有的人：读到他现有作业的克隆配置就记进名单（只记这一次，src=job），返回名单。"""
    saved = F.registered_people()
    jobs = _from_jobs()
    new = {s["holder"]: jobs[s["holder"]] for s in slots(cfg)
           if (s["holder"] not in saved or saved[s["holder"]].get("src") == "roster") and s["holder"] in jobs}
    if new:
        t = round(time.time())
        def seed(d):
            for n, v in new.items():
                old = d.get(n, {})
                if n not in d or old.get("src") == "roster":
                    d[n] = {**old, **v, "src": "job", "t": t}

        F.edit_state("people.json", seed)
        saved = F.registered_people()
    return saved


def people(cfg: dict | None = None) -> dict:
    """{名字: {"image", "run_cmd", "note", "src", "saved", "slots"}}。
    占着服务器的也在：读到克隆配置前先列出来，image 为空。
    src：job 从现有作业读的、default 按新的人默认、manual 页面上设置的、roster 只登记了姓名。"""
    saved, off, servers = _seed(cfg), F.off_servers(), slots(cfg)
    blank = {"image": None, "run_cmd": None, "note": None, "src": None, "saved": False}
    out: dict[str, dict] = {n: {**blank, **{k: p.get(k) for k in ("image", "run_cmd", "note", "src", "t")}, "saved": True} for n, p in saved.items()}
    for s in servers:
        if s["holder"] and s["id"] not in off:
            out.setdefault(s["holder"], dict(blank))
    for name, p in out.items():
        p["slots"] = [s["id"] for s in servers if s["holder"] == name and s["id"] not in off]
    return out


def taken_jobs(cfg: dict, release: str | None = None, running=None) -> set[str]:
    """已在用的平台作业名：各台（关了机的不算）、在用的自用主机，加上平台上运行中的。release：马上要停掉的那个。
    running：刚读的平台作业列表里运行中的名字（真操作时给，只认它）；没给（预览）用上次核对存的 platform.json，
    labmon 自己停掉的作业已从里面删去（fleetops._mark_off），免得关了的自用主机还算占着名字。"""
    off = F.off_servers()
    if running is None:
        running = [r.get("name") for r in load_json(F.state_file("platform.json"), {}).get("running") or [] if r.get("name")]
    return ({s["job"] for s in slots(cfg) if s["id"] not in off} | set(running)) - {release}


def back_port(cfg: dict, pool: str, name: str | None) -> int | None:
    """给一个人在池里开新的：这人在这个池里上次关掉的那台（最近关的；组内服务器看 boot.json 的 off，自己的看已归还的自用主机）的端口，
    开回来优先沿用它。端口不变则作业多半落回同一 ssh 与端口：组内服务器登记回原来那台（_adopt 去掉 off），自用主机由 config.add_mine_host 恢复那一块，
    这人本机的 ssh 配置不用改（新容器的主机密钥会变：看板这边的旧条目由 run_swap 在等 ssh 前删掉，自用主机连同拉起 worker 用的那份）；
    被占了 pick_port 照常往后挑。"""
    if not name:
        return None
    off = F.off_servers()
    cands = [(e.get("t") or 0, s["port"]) for s in cfg.get("servers") or [] if s.get("pool") == pool and s.get("port")
             and (e := off.get(s["id"])) is not None and e.get("holder", s.get("holder")) == name]
    if h := last_mine(pool, cfg=cfg, holder=name):
        cands.append((parse_time(str(h["returned"])) or 0, h["port"]))
    return int(max(cands, key=lambda x: x[0])[1]) if cands else None


def last_mine(pool: str | None, skip: str | None = None, *, cfg: dict | None = None, holder: str | None = None) -> dict | None:
    """这个池里最近归还的自用主机（写了 platform_job 的，主机清单里的一块；skip 这一台不算）：给自己开回来时 back_port 沿用它的端口；
    端口被占、开成了新 id 时，新主机页的「接上原来的项目」把它留下的登记排第一（overview._regs 的 prev）。没有就是 None。"""
    hs = [h for h in all_hosts()
          if h.get("returned") and h.get("platform_job") and pool and h.get("platform_pool") == pool
          and h.get("port") and h["id"] != skip and (holder is None or F.host_holder(h, cfg) == holder)]
    return max(hs, key=lambda h: parse_time(str(h["returned"])) or 0, default=None)


def job_name(person: str, taken: set[str]) -> str:
    """作业名：缺省就是名字；同名的已在跑就加后缀 -2、-3……（还要合平台作业名的规则）。"""
    if person not in taken:
        return person
    for k in range(2, 100):
        n = f"{person}-{k}"
        if n not in taken and P.NAME_RE.match(n):
            return n
    raise P.PlatformError(f"{person} 加后缀后的作业名都被占了或太长")


def set_person(name: str, image: str | None = None, run_cmd: str | None = None, note: str | None = None,
               remove: bool = False, cfg: dict | None = None, t: float | None = None, *, create: bool = False, roster_only: bool = False) -> dict:
    """页面上存一个人的设置（镜像、启动命令都要写，备注可空）。src 记来源：镜像与启动命令没改就沿用原来的，与新的人默认相同记 default，
    否则 manual。本机首次登记可只写名册，之后仍可由现有作业补齐设置。remove 删掉（还占着服务器或已开通账号的不让删）。"""
    name = (name or "").strip()
    if not P.NAME_RE.match(name):
        raise P.PlatformError(f"名字也是平台作业名：只能用字母、数字、_ ~ -，最多 32 个：{name or '（空）'}")
    if remove:
        sl = (people(cfg).get(name) or {}).get("slots")
        if sl:
            raise P.PlatformError(f"{name} 还占着 {'、'.join(sl)}，不能删：先把服务器分配给别人或关机")
        from . import users as U
        with U.removing_person(name, cfg):
            F.edit_state("people.json", lambda d: d.pop(name, None))
        log_events([{"kind": "config", "who": name, "msg": f"人员：删了 {name}"}], t)
        return people(cfg)
    old = F.registered_people().get(name)
    if roster_only:
        rec = {"src": "roster"}
    else:
        rec = {"image": (image or "").strip(), "run_cmd": (run_cmd or "").strip()}
        _check_image(rec["image"])
        if not rec["run_cmd"]:
            raise P.PlatformError("启动命令不能空")
        d = defaults()
        ic = (rec["image"], rec["run_cmd"])
        rec["src"] = (old["src"] if old and old.get("src") and ic == (old.get("image"), old.get("run_cmd"))
                      else "default" if ic == (d["image"], d["run_cmd"]) else "manual")
    if note is not None:
        rec["note"] = note.strip() or None
    t = time.time() if t is None else t
    def save(d):
        if create and any(n.casefold() == name.casefold() for n in d):
            raise P.PlatformError(f"已登记组内人员 {name}，请在名单中选择")
        d[name] = {**d.get(name, {}), **rec, "t": round(t)}

    F.edit_state("people.json", save)
    if old is None:
        msg = f"人员：新加 {name}" + (f"（镜像 {rec['image']}）" if not roster_only else "")
    else:
        changes = _changes(old, rec)
        msg = f"人员：改了 {name} 的设置：{changes}" if changes else None
    if msg:
        log_events([{"kind": "config", "who": name, "msg": msg}], t)
    return people(cfg)


# ---------- 池 ----------
def is_private(cfg: dict, pool: str) -> bool:
    return pool in cfg["private_pools"]


def ref(pool: str, sid: str | None = None) -> tuple[str | None, dict | None, str | None]:
    """池的模板作业：boot.json 里这个池最近一份克隆配置，返回 (作业 id, 克隆配置, id)。给了 sid 且这台自己有就用它的。"""
    b = load_json(F.state_file("boot.json"), {})
    if sid and (b.get(sid) or {}).get("pool") == pool and b[sid].get("clone"):
        return b[sid].get("clone_job") or b[sid].get("job_id"), b[sid]["clone"], sid
    cs = [(e.get("clone_t") or e.get("t") or 0, k, e) for k, e in b.items() if e.get("pool") == pool and e.get("clone")]
    if not cs:
        return None, None, None
    _, k, e = max(cs, key=lambda x: x[0])
    return e.get("clone_job") or e.get("job_id"), e["clone"], k


def pool_names(cfg: dict) -> list[str]:
    """看板认得的池：专用池、登记的服务器与自用主机所在的池、开机参数里出现过的池。"""
    names = list(cfg["private_pools"]) + [s["pool"] for s in cfg["servers"]]
    names += [h.get("platform_pool") for h in active_hosts() if h.get("platform_pool")]
    names += [e.get("pool") for e in load_json(F.state_file("boot.json"), {}).values() if e.get("pool")]
    return list(dict.fromkeys(n for n in names if n))


def pools_info() -> dict:
    return load_json(F.state_file("pools.json"), {})


def compose(base: dict, name: str, image: str | None, run_cmd: str | None, port: int) -> dict:
    """模板作业的克隆配置 → 要提交的配置：改作业名、镜像、启动命令，端口只留 22→port（别的映射在新服务器上可能撞）。"""
    c = copy.deepcopy(base)
    c["taskName"] = name
    for role in c.get("cloneTaskRoleVOList") or []:
        if image:
            role["imageName"] = image
        if run_cmd:
            role["runCmd"] = run_cmd
        p22 = next((p for p in role.get("cloneTaskHostPortDTOList") or [] if str(p.get("containerPort")) == "22"), None)
        role["cloneTaskHostPortDTOList"] = [{**(p22 or {"environmentPortName": "OTHER"}), "containerPort": "22", "hostPort": str(port)}]
    return c


def summary(c: dict) -> dict:
    role = (c.get("cloneTaskRoleVOList") or [{}])[0]
    return {"name": c.get("taskName"), "image": role.get("imageName"), "run_cmd": role.get("runCmd"), "pool_id": c.get("poolId"),
            "spec": role.get("specificationId"), "env": c.get("envMap") or None,
            "volumes": [f"{v.get('storagePath')} → {v.get('mountPath')}（{v.get('perm')}）" for v in c.get("cloneStorageVolumeVOList") or []],
            "port": next((p.get("hostPort") for p in role.get("cloneTaskHostPortDTOList") or [] if str(p.get("containerPort")) == "22"), None)}


# ---------- 节点与端口 ----------
def _octet(x: str) -> str:
    return x.rsplit("@", 1)[-1].rsplit(".", 1)[-1]


def ext_host(ip: str | None, cfg: dict) -> str | None:
    """平台给的内网 IP（192.0.2.29）→ 本机连它用的 ssh 目标（root@198.51.100.29）：按已知的服务器与开机参数换算。"""
    if not ip:
        return None
    b = load_json(F.state_file("boot.json"), {})
    pairs = [(b[s["id"]]["ip"], s["ssh"]) for s in slots(cfg) if (b.get(s["id"]) or {}).get("ip")]
    for kip, ssh in pairs:
        if kip == ip:
            return ssh
    for kip, ssh in pairs:                     # 同一对网段：前三段换掉、末段照抄
        if _octet(kip) == _octet(ssh):
            return ssh.rsplit(".", 1)[0] + "." + _octet(ip)
    return None


def pool_nodes(cfg: dict, pool: str) -> list[str]:
    """这个池的节点（ssh 目标）：池里登记的服务器、自用主机，以及与平台核对时读回的节点。"""
    ns = [s["ssh"] for s in cfg["servers"] if s["pool"] == pool]
    ns += [h["ssh"] for h in active_hosts() if h.get("platform_pool") == pool and h.get("ssh")]
    for host in (pools_info().get("pools", {}).get(pool) or {}).get("hosts") or []:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", str(host)):
            ns.append(ext_host(str(host), cfg))
    return list(dict.fromkeys(n for n in ns if n))


def used_ports(cfg: dict, extra=()) -> dict[str, set[int]]:
    """各节点上已占的 ssh 端口：登记的服务器（关了机的不算）、自用主机，加上平台上没登记的运行中作业（extra：(节点, 端口)）。"""
    off = F.off_servers()
    out: dict[str, set[int]] = {}
    pairs = [(s["ssh"], s["port"]) for s in cfg["servers"] if s["id"] not in off]
    pairs += [(h["ssh"], h["port"]) for h in active_hosts() if h.get("platform_job") and h.get("ssh") and h.get("port")]
    for node, port in list(pairs) + list(extra):
        if node and port:
            out.setdefault(node, set()).add(int(port))
    return out


def tcp_open(node: str, port: int, timeout: float = 2) -> bool:
    """本机连得上这个端口（有容器或别的服务在听）就算占着；被拒算空闲，超时也算占着（拿不准就不用）。"""
    try:
        socket.create_connection((node.rsplit("@", 1)[-1], int(port)), timeout=timeout).close()
        return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True


TCP_MAX = 6                   # 一次挑端口最多向节点试连这么多个候选（对节点 sshd 连发新连接会被限流）
TCP_TTL = 600                 # 向导预览里试连的结果缓存这么久：每次 /fleet/plan.json 不再连节点；真提交前（run_swap）另探一次
_TCP: dict = {}               # {(节点, 端口): (时刻, 占着没有)}


def tcp_cached(node: str, port: int, t: float | None = None, tcp=tcp_open) -> bool:
    """预览用的端口试连：TCP_TTL 内同一个 (节点, 端口) 只连一次。"""
    t = time.time() if t is None else t
    c = _TCP.get((node, port))
    if c is None or t - c[0] > TCP_TTL:
        _TCP[(node, port)] = c = (t, tcp(node, port))
    return c[1]


def pick_port(cfg: dict, nodes: list[str], used: dict[str, set[int]], prefer: int | None = None,
              release=(), tcp=tcp_open, what: str = "原端口") -> tuple[int, str]:
    """挑端口，返回 (端口, 为什么选它)。prefer：优先的端口（接替的那台的“原端口”，或 back_port 给的“上次的端口”，说法由 what 给）；
    release：马上要停掉的 (节点, 端口)，不算占用。登记里没占的候选才向节点试连，最多 TCP_MAX 个；都占着就停下报错，不接着扫整段。
    “为什么”只在这里生成，向导预览与真操作（fleetops._new_job）同一句。"""
    lo, hi = cfg.get("port_range") or PORT_RANGE
    if not (P.PORTS[0] <= lo <= hi <= P.PORTS[1]):
        raise P.PlatformError(f"[fleet] port_range 要在平台规定的 {P.PORTS[0]}–{P.PORTS[1]} 内：{lo}–{hi}")
    if not nodes:
        raise P.PlatformError("不知道这个池在哪台节点上：先与平台核对，或在主机清单里登记一台")
    rel = {(n, int(p)) for n, p in release}
    cands = ([int(prefer)] if prefer else []) + [p for p in range(lo, hi + 1) if p != prefer]
    tried = []
    for p in cands:
        if not P.PORTS[0] <= p <= P.PORTS[1]:
            continue
        if any(p in used.get(n, set()) and (n, p) not in rel for n in nodes):
            continue
        if len(tried) >= TCP_MAX:
            raise P.PlatformError(f"{'、'.join(nodes)} 上试了 {'、'.join(map(str, tried))} 都有东西在听（没登记的容器或别的服务），没再试："
                                  f"先与平台核对，或在 [fleet] port_range 里换一段")
        tried.append(p)
        if any((n, p) not in rel and tcp(n, p) for n in nodes):
            continue
        low = f"{lo}–{hi} 里最小的空闲端口"
        return p, (f"沿用{what}" if p == prefer else f"{what} {prefer} 被占，改用 {p}（{low}）" if prefer else low)
    raise P.PlatformError(f"{'、'.join(nodes)} 上 {lo}–{hi} 都占满了：在 [fleet] port_range 里放宽")


def sid_of(node: str, port) -> str:
    return f"{_octet(node)}-{port}"


# ---------- 合法性 ----------
def _repo_of(image: str) -> tuple[str, str]:
    """192.0.2.20:8888/example-team/ubuntu:demo-tag → ("example-team/ubuntu", "demo-tag")。"""
    path, _, tag = image.rpartition(":")
    parts = path.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
        parts = parts[1:]
    return "/".join(parts), tag


def find_image(cli: P.Client, image: str, human=lambda: None) -> tuple[bool | None, str]:
    """镜像在不在仓库里：(True/False/None 认不出格式, 说明)。"""
    repo, tag = _repo_of(image)
    rows = cli.image_repos()
    if not rows or not any("depositoryName" in r for r in rows):
        return None, f"镜像仓库列表的格式认不出，没核对 {image}"
    hit = next((r for r in rows if r.get("depositoryName") in (repo, repo.split("/")[-1])
                or str(r.get("depositoryName", "")).endswith("/" + repo)), None)
    if hit is None:
        return False, f"仓库里没有 {repo}"
    human()
    tags = cli.image_tags(hit.get("id"))
    if not tags or not any("fullPath" in x or "tagName" in x for x in tags):
        return None, f"{repo} 的标签列表格式认不出，没核对 {tag}"
    if any(x.get("fullPath") == image or x.get("tagName") == tag for x in tags):
        return True, image
    return False, f"{repo} 里没有标签 {tag}"


def check(cli: P.Client, c: dict, human=lambda: None, freed: bool = False) -> list[dict]:
    """提交前逐项核对（只读）：[{"item", "ok": True/False/None, "note"}]。freed：同池先停一台再开（不要求现在有空卡）。"""
    s = summary(c)
    out = [{"item": "作业名", "ok": bool(P.NAME_RE.match(s["name"] or "")), "note": s["name"]},
           {"item": "端口", "ok": bool(s["port"]) and P.PORTS[0] <= int(s["port"]) <= P.PORTS[1], "note": f"22→{s['port']}"}]
    human()
    specs = cli.specs(c.get("poolId"))
    sp = next((x for x in specs if x.get("specificationId") == s["spec"]), None)
    if not specs or not any("specificationId" in x for x in specs):
        out.append({"item": "规格", "ok": None, "note": "规格列表格式认不出，没核对"})
    elif sp is None:
        out.append({"item": "规格", "ok": False, "note": f"池里没有规格 {s['spec']}"})
    else:
        avail = sp.get("quantityAvailable")
        q = "；不可排队" if str(sp.get("isSchedule")) == "0" else f"；排队 {sp.get('queueNum')}" if sp.get("queueNum") else ""
        ok = True if freed or avail is None else int(avail or 0) >= 1
        out.append({"item": "规格", "ok": ok, "note": f"{sp.get('specificationName') or s['spec']}，空 {avail} / {sp.get('specificationAmount')}{q}"})
    human()
    vols = cli.volumes()
    paths = {v.get("fullPath") for v in vols}
    want = [v.get("storagePath") for v in c.get("cloneStorageVolumeVOList") or []]
    if not vols or not any("fullPath" in v for v in vols):
        out.append({"item": "挂载卷", "ok": None, "note": "存储卷列表格式认不出，没核对"})
    else:
        miss = [w for w in want if w not in paths]
        out.append({"item": "挂载卷", "ok": not miss, "note": "、".join(miss) + " 不是这个账号的" if miss else "、".join(s["volumes"]) or "无"})
    human()
    ok, note = find_image(cli, s["image"] or "", human)
    out.append({"item": "镜像", "ok": ok, "note": note})
    return out


# ---------- 与平台核对时顺带读的 ----------
def read_pools(cli: P.Client, cfg: dict, human, t: float) -> dict:
    """资源池（含节点）、各池规格、存储卷、人用到的镜像仓库标签 → pools.json。每样只读一次，步骤间像人一样停一下。"""
    rows = cli.pools()
    pools: dict[str, dict] = {}
    for r in rows:
        p = pools.setdefault(r.get("poolName"), {"id": r.get("poolId"), "hosts": [], "specs": []})
        if r.get("host") and r["host"] not in p["hosts"]:
            p["hosts"].append(r["host"])
    for name in pool_names(cfg):
        pid = (pools.get(name) or {}).get("id") or ((ref(name)[1] or {}).get("poolId"))
        if not pid:
            continue
        human()
        p = pools.setdefault(name, {"id": pid, "hosts": [], "specs": []})
        p["specs"] = [{k: x.get(k) for k in ("specificationId", "specificationName", "quantityAvailable", "specificationAmount",
                                               "isSchedule", "queueNum")} for x in cli.specs(pid)]
    human()
    vols = [{"path": v.get("fullPath"), "name": v.get("volumeName")} for v in cli.volumes()]
    human()
    repos = cli.image_repos()
    want = {_repo_of(p["image"])[0] for p in people(cfg).values() if p.get("image")}
    images = {}
    for r in repos:
        if r.get("depositoryName") in want or any(str(r.get("depositoryName", "")).endswith("/" + w) or w.endswith("/" + str(r.get("depositoryName")))
                                                  for w in want):
            human()
            images[r["depositoryName"]] = [x.get("fullPath") or x.get("tagName") for x in cli.image_tags(r.get("id"))]
    res = {"t": round(t), "pools": pools, "volumes": vols, "images": images,
           "sample": {"pool": rows[:1], "repo": repos[:1]}}     # 头一行原样留着，接口格式与文档对照用
    F.state_dir().mkdir(parents=True, exist_ok=True)
    save_json(F.state_file("pools.json"), res)
    return res


# ---------- 给页面预览（不连平台） ----------
def settings_of(pp: dict) -> tuple[str | None, str | None, str]:
    """要提交的镜像、启动命令与来源：名单里这人的设置；不在名单里（新名字）或还没读到的，按新的人默认。"""
    if pp.get("image"):
        return pp["image"], pp.get("run_cmd"), "这人的设置"
    d = defaults()
    return d["image"], d["run_cmd"], "新的人默认"


def preview(cfg: dict, kind: str, sid: str | None = None, person: str | None = None, pool: str | None = None,
            tcp=tcp_open, *, account: str | None = None) -> dict:
    """向导里的预览：要提交的参数、挑出的端口、本地能核对的各项（真正提交前还会连平台再核一次，端口也再探一次）。
    端口试连的结果按 tcp_cached 缓存：页面每改一下就取一次预览，不能每次都去连节点的 sshd。"""
    srv = {s["id"]: s for s in slots(cfg)}
    old = srv.get(sid) if sid else None
    off = F.off_servers()
    if old is None and sid:
        raise P.PlatformError(f"组内服务器里没有 {sid}")
    pool = old["pool"] if old else pool
    if not pool:
        raise P.PlatformError("要选资源池")
    name = (person or ((old or {}).get("holder") if kind == "open" else "") or "").strip()     # 换人不默认原来的人
    ppl = people(cfg)
    pp = ppl.get(name) or {}
    actor, error = F.account_person(cfg, account)
    if error:
        raise P.PlatformError(error)
    mine = bool(name and actor and name.casefold() == actor.casefold())
    if mine:
        name = actor
        pp = ppl.get(name) or {}
    live = old and old["id"] not in off
    job_n = job_name(name, taken_jobs(cfg, old["job"] if live and kind == "swap" else None)) if P.NAME_RE.match(name) else name
    job, base, ref_sid = ref(pool, old["id"] if old else None)        # 换人、重开一台：先用这台自己的克隆配置
    items = [{"item": "作业名", "ok": bool(P.NAME_RE.match(name)), "note": (job_n + (f"（{name} 已在用，加了后缀）" if job_n != name else "")) if name else "要选人"}]
    out = {"kind": kind, "pool": pool, "private": is_private(cfg, pool), "person": name, "new_person": name not in ppl, "mine": mine,
           "job": job_n, "ref": ref_sid, "items": items}
    if base is None:
        items.append({"item": "模板作业", "ok": False, "note": "开机参数里没有这个池的克隆配置：先与平台核对"})
        return out
    nodes = pool_nodes(cfg, pool)
    release = [(old["ssh"], old["port"])] if live and kind == "swap" else []
    prefer = int(old["port"]) if old else back_port(cfg, pool, name)
    try:
        port, why = pick_port(cfg, nodes, used_ports(cfg), prefer, release, lambda n, p: tcp_cached(n, p, tcp=tcp), what="原端口" if old else "上次的端口")
        items.append({"item": "端口", "ok": True, "note": f"{port}（{why}）"})
    except P.PlatformError as x:
        port = None
        items.append({"item": "端口", "ok": False, "note": str(x)})
    img, cmd, src = settings_of(pp)
    c = compose(base, job_n or "?", img, cmd, port or 0)
    out.update(summary(c), nodes=nodes, sid=sid_of(nodes[0], port) if port and len(nodes) == 1 else None, image_src=src, job=job_n)
    if mine and old and not old.get("mine"):
        out["after"] = "开好后接入自用：主机清单里这台从组内服务器移到自用主机（照抄现有自用主机，worker 自动拉起、开保活）"
    elif mine and not old:
        # config.add_mine_host 遇到同一 ssh 与端口、已归还的自用主机就恢复那一块（端口沿用上次的、多半落回同一节点时）
        back = next((h["id"] for h in all_hosts() if h.get("returned") and port and int(h.get("port") or 0) == port and h.get("ssh") in nodes), None)
        out["after"] = (f"开好后接入自用：恢复已归还的自用主机 {back}（落在同一 ssh 与端口），worker 自动拉起、开保活" if back
                        else "开好后接入自用：主机清单加一台自用主机（照抄现有自用主机，worker 自动拉起、开保活）")
    elif mine and old:
        out["after"] = "开好后接成你的自用主机，worker 自动拉起、开保活"
    elif (old or {}).get("mine"):
        out["after"] = f"开好后这台从 {old['holder']} 的自用主机移到组内服务器，交给 {name or '？'}（自用主机 {old['id']} 标成已归还）"
    info = pools_info()
    known = info.get("images") or {}
    repo, tag = _repo_of(img or "")
    tags = next((v for k, v in known.items() if k == repo or repo.endswith("/" + k) or k.endswith("/" + repo)), None)
    items.append({"item": "镜像", "ok": None if tags is None else any(x in (img, tag) for x in tags),
                  "note": img if tags is None or any(x in (img, tag) for x in tags) else f"上次核对时仓库里没有 {tag}"})
    if pp.get("slots") and not pp.get("image"):       # 占着服务器、还没读到现有作业的：别默默用默认镜像
        items[-1] = {"item": "镜像", "ok": None, "note": f"还没读到 {name} 现有作业的镜像，先「与平台核对」；现在会用默认的 {img}"}
    priv = is_private(cfg, pool)
    lim = cfg.get("private_max") if priv else cfg.get("public_max")
    n = sum(1 for s in cfg["servers"] if s["id"] not in off and is_private(cfg, s["pool"]) == priv)
    n += sum(1 for h in active_hosts() if h.get("platform_job") and h.get("platform_pool")
             and is_private(cfg, h["platform_pool"]) == priv)
    after = n - (1 if live and kind == "swap" else 0) + 1
    items.append({"item": "配额", "ok": lim is None or after <= lim,
                  "note": f"{'专用' if priv else '公用'}池开完 {after} 台" + (f"（上限 {lim}）" if lim is not None else "")})
    sp = next((x for x in ((info.get("pools") or {}).get(pool) or {}).get("specs") or [] if x.get("specificationId") == out["spec"]), None)
    if sp:
        items.append({"item": "空卡", "ok": True if release else int(sp.get("quantityAvailable") or 0) >= 1,
                      "note": f"上次核对时空 {sp.get('quantityAvailable')} / {sp.get('specificationAmount')}"})
    out["checked_at"] = info.get("t")
    return out
