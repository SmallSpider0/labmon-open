"""全局调度页的数据：读各在用主机上 worker 写的 status.json（主机现状、每个任务的用量与推演）、worker.log、usage.jsonl 与 events.jsonl。

每台主机一次 ssh（复用看板的连接）。只往服务器写两样（变了才写）：运行中任务的预计结束（项目看板按日志进度算的）写进 eta.json，
worker 推演时用它；用户级主机清单的 [sched]（config.sched）写进 sched.json，worker 排放行顺序用它。主机的分钟级曲线与占用记录存在本机状态目录，不占共享盘。

    状态目录（state.state_dir）：$LABMON_STATE（缺省 ~/.local/state/labmon）/overview/
        snapshot.json   最近一次的数据        index.html   渲染好的页面（数据内联）
        health.json     各主机连通状态、最近一次见到的容器启动时间、已发过的租期提醒
        series/<主机>.jsonl   每轮一行：内存工作集、各卡显存与利用率、CPU（保留 7 天）
        colors.json     项目配色（按出现先后取色，记住后不随顺序变）
        events.jsonl    记录：看板与组内服务器的事件、平台操作、配置改动（保留一年，见 records.py）
        hostlog/<主机>.jsonl   各主机 worker 的事件（去掉任务起止，保留 30 天）
        usage/<主机>.jsonl     各主机跑完的任务（谁、哪几张卡、起止；保留 30 天）：时间线画已跑的占用（occupancy）

时刻：worker 写的都是 epoch 秒（running[].t0、usage 行的 t0 / t1），看板只认它们。
项目 id：只认 worker 状态里队列与任务的 project（worker add --project 写的，就是看板的项目 id），看板不按目录猜。
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path

from . import borrow as B
from . import disk as DK
from . import fleet as F
from . import fleetboot as FB
from . import fleetops as FO
from . import fleetview as FV
from . import hostcheck as HC
from . import lan as L
from . import lease as LS
from . import rebalance as RB
from . import renew as R
from .projects import archived_rows, registered
from .collect import write_page
from .config import POOL, POOL_LABEL, STALE_S, ConfigError, active_hosts, all_hosts, expire_sched, fmt_time, home_host, parse_time, pause_by, pick_color, sched, user_hosts
from .health import container_step, lease_due, lease_end, link_step
from .records import EVENT_CATS, EVENT_KINDS, log_events, told
from .state import append_row, load_json, load_rows, parse_rows, save_json, snapshot, state_dir, usage_file, write_atomic
from .service import log
from .workerctl import run_out, ssh

KEEP_S = 7 * 86400
SHOW_S = 24 * 3600
SEP = "@@labmon@@"
REG = "@@labmon-reg@@"


def fetch(h: dict, timeout: int = 60) -> tuple[str | None, str | None]:
    """一次 ssh 读回状态、日志尾、用量历史尾，以及共享盘扫描索引（服务器上的修改时刻变了才传）；返回 (原文, 错误)。
    h["put"] 是给 worker 的文件 {文件名: 内容}（eta.json、sched.json、returned.json），与上次写的不同（不看写的时刻 t）才顺带写进这台的目录；
    目录还没有（worker 没 start 过）就写不进去，远端把真写成的文件名回显在输出末尾，只有这些才记成已发（_SENT），下一轮再写。
    本机还没存这台的占用记录时多取些用量历史补上。没有 ssh 的主机（测试）在本机读。
    h["regs"]（每个 worker_home 挑的那一台才有）：顺带读这个 worker_home 下别的主机目录里的登记文件（_reg_cmd），给登记的全局视图（_regs）。
    h["lease"]（collect 给的 "renew"）：先在这台的 worker_home 上续服务租约（lease.remote_sh），租约这一刻是这台看板服务的才写 put（#163）；
    读回的租约是倒数第二段（lease_part 取）。没给的（测试直接调）照旧写。"""
    d = shlex.quote(h["worker_home"].rstrip("/") + "/hosts/" + h["id"])
    ix = shlex.quote(h["worker_home"].rstrip("/") + "/disk/index.json")
    new = {n: v for n, v in (h.get("put") or {}).items() if _sig(v) != _SENT.get((h["id"], n))}
    put = "".join(f"[ -d {d} ] && printf '%s\\n' {shlex.quote(json.dumps(v, ensure_ascii=False))} > {d}/{n}.tmp && mv {d}/{n}.tmp {d}/{n} && put=\"$put {n}\"; "
                  for n, v in new.items())
    n = 60 if usage_file(h["id"]).exists() else 3000
    # 服务租约（#163）：先续约，$lok 为 0（这一刻租约是这台看板服务的）才写 put；读回的租约作为倒数第二段带回（collect 交给 lease.decide）
    lz = LS.remote_sh(h, h["lease"]) if h.get("lease") else "lease=; lok=0; "
    remote = (f"{lz}put=; if [ \"$lok\" = 0 ]; then :; {put}fi; cat {d}/status.json 2>/dev/null; echo; echo {SEP}; tail -n 40 {d}/worker.log 2>/dev/null; "
              f"echo {SEP}; tail -n {n} {d}/usage.jsonl 2>/dev/null; echo {SEP}; date +%s; "
              f"echo {SEP}; tail -n 100 {d}/events.jsonl 2>/dev/null; echo {SEP}; cat {d}/stopped 2>/dev/null; echo; "
              f"echo {SEP}; m=$(stat -c %Y {ix} 2>/dev/null || stat -f %m {ix} 2>/dev/null); echo \"$m\"; "
              f"[ -n \"$m\" ] && [ \"$m\" != {DK.seen()} ] && cat {ix}; echo; echo {SEP}; {_reg_cmd(h)}echo {SEP}; echo \"$lease\"; echo {SEP}; echo \"$put\"")
    cmd = L.ssh_argv(h["ssh"], h["port"], known_hosts=h.get("known_hosts")) + [remote] if h.get("ssh") else ["bash", "-c", remote]
    try:
        r = (L.ssh_run if h.get("ssh") else subprocess.run)(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, "ssh 超时"
    if r.returncode != 0 and SEP not in r.stdout:
        return None, (r.stderr.strip().splitlines() or ["ssh 失败（退出码 %d）" % r.returncode])[-1]
    out, _, written = r.stdout.rpartition(SEP + "\n")                   # 末尾一段：这次真写成的文件名
    for n in written.split():
        if n in new:
            _SENT[(h["id"], n)] = _sig(new[n])
    return out, None


def lease_part(raw: str | None) -> dict | None:
    """fetch 读回的原文里服务租约那一段（第 9 段）→ lease.parse 的结果；这一轮没读到为 None。"""
    parts = raw.split(SEP + "\n") if raw else []
    return LS.parse(parts[8]) if len(parts) > 8 else None


def reset_sent() -> None:
    """忘掉写给 worker 的文件记到哪了（_SENT）：这台当备用的那段时间主写过别的内容，重新当主后第一轮照写一遍。"""
    _SENT.clear()


def _reg_cmd(h: dict) -> str:
    """fetch 里读登记文件的那一段（只有 h["regs"] 的那一台有）：这个 worker_home 下每个有 queues/ 的主机目录先打一行「REG 主机」，
    再逐个登记文件打「REG 主机 文件名 修改时刻」与原文（修改时刻是共享盘服务器的时钟，epoch 秒：登记的 added_t，算力池待放从它算，#24）。h["regs"] 是在用主机的 id：它们有 status.json 时跳过（登记取状态里的 queues），
    worker 从没写过状态的照读（刚 worker add、worker 还没起）。只列 hosts/*/queues/*.json 几个小文件，不遍历别的目录。"""
    if h.get("regs") is None:
        return ""
    home, act = shlex.quote(h["worker_home"].rstrip("/")), shlex.quote(" " + " ".join(h["regs"]) + " ")
    return (f"for hd in {home}/hosts/*/; do hd=${{hd%/}}; [ -d \"$hd/queues\" ] || continue; id=${{hd##*/}}; "
            f"case {act} in *\" $id \"*) [ -f \"$hd/status.json\" ] && continue;; esac; echo \"{REG} $id\"; "
            f"for f in \"$hd\"/queues/*.json; do [ -f \"$f\" ] || continue; "
            f"echo \"{REG} $id ${{f##*/}} $(stat -c %Y \"$f\" 2>/dev/null || stat -f %m \"$f\" 2>/dev/null)\"; cat \"$f\"; echo; done; done; ")


REG_KEYS = ("root", "jobs", "project", "stop", "gpus")     # 登记的全局视图里一条登记带的字段（状态里的 queues 与登记文件共有的）


def _parse_regs(text: str, *, strict: bool = False) -> dict:
    """_reg_cmd 读回的那一段 → {主机: [登记]}（登记文件里的 REG_KEYS、added，added_t 是登记文件的修改时刻，key 是登记键：文件名去掉 .json）。
    读坏了的文件跳过（worker 那边记事件）。"""
    out: dict = {}
    for chunk in re.split("^" + re.escape(REG) + " ", text, flags=re.M)[1:]:
        head, _, body = chunk.partition("\n")
        hid, _, f = head.strip().partition(" ")
        f, _, mt = f.partition(" ")
        regs = out.setdefault(hid, [])
        if not f:
            continue
        try:
            reg = json.loads(body)
        except ValueError:
            if strict:
                raise
            continue
        if strict and not (isinstance(reg, dict) and isinstance(reg.get("project"), str)):
            raise ValueError("登记归属未核实")
        if isinstance(reg, dict):
            regs.append({**{k: reg.get(k) for k in REG_KEYS}, "added": reg.get("added"), "added_t": int(mt) if mt.isdigit() else None,
                         "key": f[:-5] if f.endswith(".json") else f})
    return out


def _regs(hosts: list[dict], by: dict, got: dict, old: dict, t: float, fleet_cfg: dict | None = None) -> list[dict]:
    """登记的全局视图（全局快照的 regs）：各主机目录下登记的清单。在用主机取这一轮 worker 状态里的 queues；
    已归还的与主机清单里已没有的（state 为 returned、unlisted）取每个 worker_home 挑的那一台读回的登记文件（_reg_cmd），
    在用而 worker 从没写过状态的也取文件。这一轮没读到的沿用上一轮快照的那一项、标 stale（上一轮也没有的 queues 为 None）：代读的那台连不上、
    本机不在内网；在用主机这轮连不上（它有 status.json，代读时跳过了它，不能当成没有登记）；刚归还、代读的又连不上（上一轮还是在用，状态按主机清单改）。
    在用的在前（照主机清单顺序，带 pool（平台资源池名）与上一台 prev），再是算力池（host 为 config.POOL、state 为 pool，#24）、
    已归还的（归还晚的在前）、主机清单里没有的。
    只读，不改登记。"""
    files, read = {}, set()                          # 主机 → (worker_home, 登记)；这一轮读到了的 worker_home
    for hid, h in by.items():
        raw = got[hid][0] if h.get("regs") is not None else None
        parts = raw.split(SEP + "\n") if raw else []
        if len(parts) > 7:
            files.update({k: (h["worker_home"], v) for k, v in _parse_regs(parts[7]).items()})
            read.add(h["worker_home"])
    prev = {x["host"]: x for x in old.get("regs") or []}
    out = []
    for e in hosts:
        h, st = by[e["id"]], e.get("status") or {}
        # pool、prev：自用主机的资源池与同池最近归还的那台（fleetboot.last_mine）：换了 id 的新主机页上「接上原来的项目」把 prev 的登记排第一
        pool = h.get("platform_pool")
        prev_host = FB.last_mine(pool, skip=e["id"], cfg=fleet_cfg, holder=F.host_holder(h, fleet_cfg)) if pool else None
        x = {"host": e["id"], "label": e["label"], "state": "active", "returned": None, "home": h["worker_home"], "stale": False,
             "pool": h.get("platform_pool"), "prev": (prev_host or {}).get("id")}
        if "queues" in st:                            # 不在内网时 status 是沿用上一次采到的
            x.update(t=st.get("t"), queues=[{**{k: q.get(k) for k in REG_KEYS}, "empty": bool(q.get("empty")), "added_t": q.get("added_t")} for q in st["queues"]],
                     stale=bool(e.get("lan_off")))
        elif e["id"] in files or (h["worker_home"] in read and got[e["id"]][0] is not None and not st and not e.get("status_error")):
            # 代读读到了这台的目录，或这台连得上、没有 status.json（代读不会跳过它）而目录里没有登记
            x.update(t=t, queues=files.get(e["id"], (None, []))[1])
        elif e["id"] in prev:
            x.update(t=prev[e["id"]].get("t"), queues=prev[e["id"]].get("queues"), stale=True)
        else:
            x.update(t=None, queues=None)
        out.append(x)
    act, known = {e["id"] for e in hosts}, {str(h["id"]): h for h in all_hosts()}
    # 算力池（#24）：保留 id pool 下的登记是交给池、还没放到哪台的清单（state 为 pool）
    tag = lambda hid: {"label": POOL_LABEL, "state": "pool", "returned": None} if hid == POOL else {   # noqa: E731
        "label": known[hid]["label"] if hid in known else hid, "state": "returned" if hid in known else "unlisted",
        "returned": parse_time(str(known[hid]["returned"])) if hid in known and known[hid].get("returned") else None}
    rest = [{"host": hid, **tag(hid), "home": home, "stale": False, "t": t, "queues": qs} for hid, (home, qs) in files.items() if hid not in act]
    seen = {x["host"] for x in rest}
    rest += [{**x, **tag(hid), "stale": True} for hid, x in prev.items()
             if hid not in act | seen and x.get("home") not in read and x.get("queues") is not None]
    rest.sort(key=lambda x: (x["state"] != "pool", x["state"] != "returned", -(x.get("returned") or 0), x["host"]))
    return out + rest


def _project_worker_state(host: dict) -> tuple[str | None, str | None]:
    """即使有 status.json，也读实际的登记文件。只读这份 worker 目录，留着的归还或旧主机目录一并核对。"""
    home = shlex.quote(host["worker_home"].rstrip("/"))
    remote = (f"set -e; if [ -e {home}/hosts ]; then [ -d {home}/hosts ] && [ -r {home}/hosts ] && [ -x {home}/hosts ] || exit 1; fi; "
              f"{_reg_cmd({**host, 'regs': []})}echo {SEP}; "
              f'for f in {home}/hosts/*/status.json {home}/hosts/*/overrides.json; do '
              '[ -f "$f" ] || continue; printf "%s\\n" "$f"; cat "$f"; echo; ' + f'echo {SEP}; done')
    try:
        rc, out = ssh(host, remote, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None, "读不到 worker 目录"
    return (out, None) if rc == 0 else (None, "读不到 worker 目录")


def check_project_idle(pid: str, entry: dict, cfg: dict, local: dict, glob: dict) -> None:
    """只核对与本项目相关的 worker 目录；快照提供线索，删除以本次读到的文件为准。"""
    known = {h["id"]: h for h in all_hosts()}
    configured = {h["id"]: h for h in cfg.get("hosts", [])}
    active = [h for h in [*known.values(), *configured.values()] if not h.get("returned")]
    associated = set(configured)
    associated.update(h["id"] for h in local.get("hosts", []))
    associated.update(r["host"] for r in entry.get("regs", []))
    associated.update(r.get("host", r.get("reader")) for r in local.get("lanes", []))
    states = ("running", "waiting", "external")
    associated.update(h["id"] for h in glob.get("hosts", [])
                      if any(j.get("project") == pid for key in (*states, "queues") for j in (h.get("status") or {}).get(key) or []))
    reg_homes = {r["host"]: r.get("home") for r in glob.get("regs", [])}
    related_homes = {h["worker_home"].rstrip("/") for h in configured.values()}
    for reg in glob.get("regs", []):
        if any(q.get("project") == pid for q in reg.get("queues") or []):
            associated.add(reg["host"])
            if reg.get("home"):
                related_homes.add(reg["home"].rstrip("/"))
    active_ids = {h["id"] for h in active}
    for hid in associated - {None, ""}:
        h = known.get(hid) or configured.get(hid)
        home = (h or {}).get("worker_home") or reg_homes.get(hid)
        if not home:
            raise ConfigError(f"主机 {hid} 的 worker 目录未知，暂时无法确认任务和登记已收尾")
        home = home.rstrip("/")
        related_homes.add(home)
    targets = {}
    for home in sorted(related_homes):
        h = home_host(home, active)
        if h is None:
            raise ConfigError(f"worker 目录 {home} 没有在用主机代读，暂时无法确认任务和登记已收尾")
        targets[home] = h
    _, readings = L.gather(targets, _project_worker_state, lambda key: bool(targets[key].get("ssh")))
    for home, (raw, error) in readings.items():
        try:
            if error or not raw:
                raise ValueError("读不到状态")
            registrations, *files = raw.split(SEP + "\n")
            if not files or files[-1].strip():
                raise ValueError("未收到完整的 worker 状态")
            regs = _parse_regs(registrations, strict=True)
            if any(q["project"] == pid for qs in regs.values() for q in qs):
                raise ConfigError("项目仍有队列登记，请先移除登记")
            for part in files[:-1]:
                path, _, body = part.partition("\n")
                hid, name = Path(path).parts[-2:]
                row = json.loads(body)
                if name == "overrides.json" and isinstance(row, list):
                    if any(o.get("target") == pid and o.get("kind") in {"pause", "focus"} and
                           (not o.get("until") or o["until"] > time.time()) for o in row):
                        raise ConfigError("项目仍有主机上的暂停或优先设置，请先撤销")
                elif name == "status.json" and isinstance(row, dict):
                    # 归还主机的等待表和旧字段不再参与调度；若仍记着项目进程，则保留阻断。
                    if hid in active_ids and not all(isinstance(row.get(key), list) for key in states):
                        raise ValueError("在用 worker 的任务列表不完整")
                    keys = states if hid in active_ids else ("running", "external")
                    if any(j.get("project") == pid for key in keys for j in row.get(key, [])):
                        raise ConfigError("worker 仍有本项目在跑或等待的任务，请先确认已收尾")
                else:
                    raise ValueError("无法识别的 worker 状态")
        except ConfigError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"未能核实 worker 目录 {home} 的任务和登记，请恢复连接或检查状态后重试") from e


_SENT: dict = {}                     # (主机, 文件名) → 上次写给 worker 的内容（变了才写）
_sig = lambda v: {k: x for k, x in v.items() if k != "t"}    # 比内容时不看写的时刻
_CLOCK: dict = {}                    # 主机 → 它的时钟比本机快多少秒（上一轮 fetch 的 date +%s 与本机比）；写给 worker 的时刻按它换算


def _series(h: dict, st: dict, t: float) -> list[dict]:
    """主机曲线（series/<主机>.jsonl，7 天）记这一轮一行，返回近 SHOW_S 的全部行（页面上的点由 _thin 抽稀，gpu_busy 的 24 小时平均用全部行）。"""
    info = st["info"]
    f = state_dir() / "series" / f"{h['id']}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    mem, pr, busy = info.get("mem") or {}, info.get("press") or {}, st.get("gpu_busy") or {}
    # mem 是工作集，anon 是收不回的内存（放行与内存闸门看它；旧 worker 没有为 None）
    row = {"t": round(t), "mem": round(mem.get("used", 0), 2) if mem else None, "lim": round(mem.get("limit", 0), 2) if mem else None,
           "anon": round(mem["anon"], 2) if mem.get("anon") is not None else None,
           "cpu": info.get("cpu_used"),
           # 主机压力（worker 的 info.press；旧 worker 没有为 None）：CPU 被限流的周期 %、共享盘每次读写 ms、本地盘忙 %
           "thr": pr.get("thr"), "nfs": (pr.get("nfs") or {}).get("ms"), "busy": (pr.get("disk") or {}).get("busy"),
           # 各卡 [已用, 总量, 此刻利用率, 吃满程度近 10 分钟平均（worker 的 gpu_busy.m10，只有保活时按 0，#51）]
           "gpu": [[round(g.get("used") or 0), round(g.get("total") or 0), g.get("util"), (busy.get(g.get("uuid")) or {}).get("m10")]
                   for g in info.get("gpus", [])]}
    append_row(f, row, KEEP_S, t, 3600)                      # 一小时修剪一次
    return [r for r in load_rows(f) if r["t"] >= t - SHOW_S]


def _thin(rows: list[dict]) -> list[dict]:
    step = max(1, len(rows) // 360)                          # 页面上最多约 360 个点
    return rows[::step] + ([rows[-1]] if rows and (len(rows) - 1) % step else [])


def gpu_busy(st: dict, rows: list[dict]) -> list[dict]:
    """主机项的 busy（#51）：各卡（info.gpus 的顺序）的「吃满程度」，worker 给的 gpu_busy {now, m10, h1, cov, ka} 加上
    近 24 小时平均 d1 与它覆盖的秒数 d1cov（series 各行的 m10 求平均：每轮的 10 分钟窗口首尾相接，相当于全部 10 秒样本的平均）。
    页面上 utilText 出字；项目快照的主机项照搬（collect._hosts）。"""
    busy, out = st.get("gpu_busy") or {}, []
    for k, g in enumerate((st.get("info") or {}).get("gpus") or []):
        v = [(r["t"], r["gpu"][k][3]) for r in rows if len(r.get("gpu") or []) > k and len(r["gpu"][k]) > 3 and r["gpu"][k][3] is not None]
        out.append({"i": g.get("i", k), **(busy.get(g.get("uuid")) or {}),
                    "d1": round(sum(x[1] for x in v) / len(v), 1) if v else None, "d1cov": round(v[-1][0] - v[0][0]) if v else 0})
    return out


_EXPIRE_ERR = [None]


def _expire(t: float) -> None:
    """到期的项目级暂停、优先：删掉主机清单里那行、记一条（config.expire_sched；worker 自己也跳过过期的）。删不了（如手写成内联表）
    不挡这一轮采集：过期的 sched 本来就不下发，只在服务日志记一次（同一句不重复记）。"""
    try:
        expire_sched(t)
        _EXPIRE_ERR[0] = None
    except ConfigError as e:
        if str(e) != _EXPIRE_ERR[0]:
            _EXPIRE_ERR[0] = str(e)
            log(f"到期的暂停、优先没删掉（不影响生效）：{e}")


def _to_worker(projects: list[dict], t: float, sw: dict):
    """给 worker 的文件（eta.json、sched.json、returned.json；overview.fetch 变了才写）：运行中任务的预计结束（项目看板按日志进度算的）给 worker 推演，
    整分钟取整，免得每轮都写。写的时刻 t 是 worker 拿来与任务开始时刻（服务器时钟）比的，按这台的时钟差换算（第一轮还不知道，按本机的）。
    returned.json 是主机清单里已归还的主机（host return，平台上关了机的自用主机也标成归还）：worker 不在时负载均衡的提示不让它们挡（#25）。
    sched.json 是这一轮读的调度设置 sw（config.sched：权重与项目级的暂停、优先；collect 加上 owner：各项目算在谁名下，#23），各在用主机同一份，后加入的主机下一轮就有。"""
    prog = {(p["id"], j["id"]): j for p in projects for j in p.get("running") or []}
    jobs = {f"{p}|{j}": round(x["eta_ts"] / 60) * 60 for (p, j), x in prog.items() if x.get("eta_ts")}
    gone = {"hosts": sorted({str(h["id"]) for h in user_hosts() if h.get("returned")})}
    return prog, lambda h: {"eta.json": {"t": round(t + _CLOCK.get(h["id"], 0)), "jobs": jobs}, "sched.json": sw, "returned.json": gone}


def _host_entry(h: dict, raw: str | None, err: str | None, lan_off: bool, prev: dict | None, sd: Path, t: float) -> dict:
    """一台主机这轮采到的：拆 fetch 读回的几段（状态、日志尾、用量尾、时钟、事件、stopped、共享盘索引；第 8 段别的主机目录里的登记由 _regs 拆），事件与用量存进本机记录。
    error 是连不上（进掉线判定）；status_error 是连上了但 status.json 读不出（半行、坏文件），页面另标，不算掉线；
    lan_off 是这轮没连（远程主机、本机不在内网），error 里是给页面看的那句话，状态沿用上一次采到的 prev。"""
    e = {"id": h["id"], "label": h.get("label", h["id"]), "error": err, "status_error": None, "status": None, "log": [], "done": [],
         "lan_off": lan_off, "auto": h.get("worker") == "auto", "platform": {"job": h.get("platform_job")} if h.get("platform") else None}
    if lan_off and prev:
        e.update({k: v for k, v in prev.items() if k in ("status", "log", "done", "clock", "stopped", "series")})
    if raw is None:
        return e
    parts = raw.split(SEP + "\n")
    try:
        e["status"] = json.loads(parts[0]) if parts[0].strip() else None
    except ValueError:
        e["status_error"] = "status.json 解析不了"
    e["log"] = [l for l in (parts[1] if len(parts) > 1 else "").splitlines() if l.strip()]
    e["done"] = parse_rows(parts[2] if len(parts) > 2 else "")
    clock = (parts[3] if len(parts) > 3 else "").strip()
    e["clock"] = int(clock) if clock.isdigit() else None
    if e["clock"]:
        _CLOCK[h["id"]] = e["clock"] - round(t)
    e["stopped"] = (parts[5] if len(parts) > 5 else "").strip() or None   # 手动 labmon worker stop 过：不自动拉起
    if len(parts) > 6:
        DK.absorb(parts[6], t)
    _keep_log(sd, h["id"], parse_rows(parts[4] if len(parts) > 4 else ""), t)    # worker 的事件进记录（页面经 records 读）
    _keep_usage(sd, h["id"], e["done"], t, {g["uuid"]: g["i"] for g in ((e["status"] or {}).get("info") or {}).get("gpus") or []})
    e["done"] = e["done"][-60:]
    return e


def _annotate(e: dict, h: dict, projects: list[dict], prog: dict, colors: dict, t: float) -> None:
    """按 worker 状态补页面要的：状态新旧（按主机自己的时钟）、任务链到项目页与配色、运行中任务的进度、资源曲线、本地盘开机以来的新写入（lw）。"""
    st = e["status"]
    if not st:
        return
    sg = st.get("storage") or {}
    e["lw"] = F.local_writes((st.get("info") or {}).get("rootfs"), sg.get("wrote"), sg.get("own"))   # 借来的主机上 scan 只数借来以后的（归还确认列它）
    now = e.get("clock") or t
    e["age"] = now - st.get("t", 0)
    e["worker_up"] = e["age"] < STALE_S
    ids = {p["id"] for p in projects}
    for j in st["running"] + st["external"] + st["waiting"] + e["done"]:
        if j.get("project"):                  # worker 写的项目 id（就是看板的）：看板上有这个项目就链到项目页
            j["link"] = j["project"] if j["project"] in ids else None
            pick_color(colors, j["project"])
    for j in st["running"] + st["external"]:
        pj = prog.get((j.get("project"), j.get("name")))
        if pj:
            j["prog"] = {k: v for k, v in pj.items() if k != "id"}
    sg = st.get("storage") or {}
    for p in list((sg.get("projects") or {}).values()) + list(sg.get("local") or {}):
        pick_color(colors, p)
    if st.get("info") and not e["lan_off"]:   # 离线时沿用的旧状态不写进曲线，也不出平均利用率
        # worker 不在（状态过期，与 worker_up 同一门槛）：状态里的平均值是停机前的，不记进曲线、不出 busy（页面与 gpu_idle 退回探针此刻值，#54 审查）
        rows = _series(h, st if e["worker_up"] else dict(st, gpu_busy={}), t)
        e["series"] = _thin(rows)
        if e["worker_up"]:
            e["busy"] = gpu_busy(st, rows)


def _voided(hosts: list[dict], t: float) -> list[dict]:
    """停下重排的任务（作废、被对方挤掉、跟着成组的同伴停下）给 labmon wait 的消息（近一天的）；job 是 [项目, 任务]，labmon wait --job 按它认等的任务被作废。"""
    how = {"squeeze": "被对方挤掉", "gang": "跟着成组的同伴停下"}
    return [{"key": f"void:{e['id']}:{v['name']}:{v['t']}", "projects": [v["project"]] if v.get("project") else None, "job": [v.get("project"), v["name"]],
             "msg": f"{e['label']} 上的 {v['name']} " + how.get(v.get("how"), "作废") + f"：{v['why']}，"
             + ("已重新排队" if v.get("kind") != "slot" else "单元已停下，要重新申请")}
            for e in hosts for v in (e.get("status") or {}).get("voided") or [] if t - v["t"] < 86400]


def _notices(hosts: list[dict], hcfg: dict, t: float, per_host: dict | None = None) -> tuple[list[str], list[dict], dict, dict, dict | None]:
    """采完各台后的判定：连通与租期（health）、预备续租、借用、共享盘、组内服务器。后三样出错不影响全局页。
    返回 (提醒, wake, 共享盘, 组内服务器, 组内配置)；wake 另有作废的任务（_voided）与页面上的改动（records.told）。per_host 见 health。"""
    notices = health(hosts, hcfg, t, per_host=per_host)
    notices += R.check(hosts, hcfg, t, busy=set(FO.busy()))     # 预备续租：到期前自动预备、判断能否续租（正在做平台操作的不预备）
    try:
        msgs, wake = B.check(hosts, t)                # 借来的主机：补齐指令、到点提醒、跑完归还；wake 给 labmon wait
    except Exception as x:  # noqa: BLE001
        msgs, wake = [f"借用的检查出错：{x}"], []
    notices += msgs
    try:
        disk, msgs = DK.update(hosts, t)              # 共享盘：总量、按人与目录的用量、增量与告警
    except Exception as x:  # noqa: BLE001
        disk, msgs = {"error": f"共享盘数据读不了：{x}"}, []
    notices += msgs
    cfg = None
    try:
        cfg = F.conf()
        if cfg:
            F.record_mine(FB.mine(cfg, hosts), t, cfg)    # 自用主机的使用记录：样本取这一轮的 worker 状态（与组内服务器的小时行同一套）
    except Exception as x:  # noqa: BLE001
        notices.append(f"自用主机的使用记录没记上：{x}")
    try:
        fleet = FV.view(hosts, t)                     # 组内服务器：fleet 线程探到的现状（这里不连 ssh）
    except Exception as x:  # noqa: BLE001
        fleet = {"error": f"组内服务器数据读不了：{x}", "servers": [], "mine": []}
    return notices, wake + _voided(hosts, t) + told(t), disk, fleet, cfg     # told：页面上的改动（搬、移除登记，恢复、暂停项目；#121）


def collect(projects: list[dict] | None = None, use_ssh: bool = True) -> dict | None:
    """工作台的一轮采集：各台并行 fetch（本机不在内网：不连、不算连不上，页面沿用上一次采到的；顺带续服务租约）→ 按读回的租约定主备（lease.decide，#163），
    不是主就停在这里、返回 None（不写快照）→ 到期的暂停、优先（_expire）→ 拆读回的几段（_host_entry）→ 补页面要的（_annotate）→ 判定与提醒（_notices）→ 快照与页面。
    projects：看板里各项目的摘要（serve.Overview.proj_info：id、title、告警、车道、里程碑、计划项、cfg 等），用来把任务链到项目页、给车道起名，
    除 running（运行中任务的进度，写 eta.json）外原样进快照的 projects，页面（项目「管理」抽屉等）读。"""
    t = time.time()
    projects = projects or []
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    colors = load_json(sd / "colors.json", {})
    sw = sched(t)
    # 按人的公平份额（#23）：各项目算在谁名下（含已归档的：它们的用量还在半衰期里），随 sched.json 发给 worker（owner：人员名的短哈希）
    try:
        fcfg = F.conf() or {}
    except Exception:  # noqa: BLE001  [fleet] 写错了：_notices 照样报这条错；认人按没有 [fleet] 降级（账号库的绑定照用），不挡整轮采集与调度设置的下发
        fcfg = {}
    people = F.project_people(sorted({*registered(), *(p["id"] for p in projects)}), fcfg)
    sw = dict(sw, owner={p: F.share_key(n) for p, n in people.items()}) if people else sw
    prog, put = _to_worker(projects, t, sw)
    hcfg = {h["id"]: h for h in active_hosts()}
    by = {h["id"]: dict(h, put=put(h), lease="renew") if use_ssh else dict(h, ssh=None, put=put(h), lease="renew") for h in hcfg.values()}
    for home in {h["worker_home"] for h in by.values()}:   # 每个 worker_home 挑一台在用主机（config.home_host）顺带读别的主机目录里的登记（_regs）
        by[home_host(home, list(by.values()))["id"]]["regs"] = list(by)
    lan, got = L.gather(by, fetch, lambda k: bool(by[k].get("ssh")), t)
    homes = LS.homes(list(hcfg.values()))
    readings = {home: [lease_part(got[h["id"]][0]) for h in g] for home, g in homes.items()}
    if LS.decide(readings, list(homes), t) != "primary":   # 租约在别人手里（#163）：这一轮到此为止，不动指令、不续租不借用、不写快照、不通知
        reset_sent()
        return None
    _expire(t)
    old = snapshot()                                  # 上一轮的全局快照：不在内网时沿用各台的状态；挪法建议的滞回
    prev = {} if lan["ok"] else {x["id"]: x for x in old.get("hosts", [])}
    hosts = []
    for h in by.values():
        e = _host_entry(h, *got[h["id"]], not lan["ok"] and bool(h.get("ssh")), prev.get(h["id"]), sd, t)
        _annotate(e, h, projects, prog, colors, t)
        hosts.append(e)
    for p in projects:                                # 看板上的项目都有配色，还没跑过任务的也有（以前灰色，与“labmon 之外”分不开）
        pick_color(colors, p["id"])
    save_json(sd / "colors.json", colors)
    per_host: dict[str, list[str]] = {}
    notices, wake, disk, fleet, fleet_cfg = _notices(hosts, hcfg, t, per_host)
    if lan["ok"]:
        RB.keep_offers(hosts, old, t)                 # 这一轮读失败的主机沿用上一轮的 offers（挪法建议不消失一轮再出现）
    snap = {"generated": t, "hosts": hosts, "colors": colors, "notices": notices, "lan": lan, "wake": wake,
            "host_notices": per_host,                 # 各主机这一轮的掉线、恢复（serve.Overview.once 另推给能用这台的人，#167）
            # 各项目的挪法建议（worker 的 offers 汇的；项目看板据此告警）；不在内网时沿用上一轮的，回来后不重复叫醒
            "rebalance": RB.suggest(hosts, old.get("rebalance"), t) if lan["ok"] else old.get("rebalance") or {},
            "event_kinds": EVENT_KINDS, "event_cats": EVENT_CATS,
            "fleet": fleet, "disk": disk, "occ": occupancy(hosts, projects, t - KEEP_S), "sched": sched_view(hosts, projects, t, sw, people),
            "regs": _regs(hosts, {k: v for k, v in by.items() if k in hcfg}, got, old, t, fleet_cfg),   # 登记的全局视图：项目「管理」抽屉的主机与队列
            # 各项目的摘要：页面要的字段只在 serve.Overview.proj_info 一处定，这里照带；running 只给 _to_worker 写 eta.json，不进快照（#139）
            "projects": [{k: v for k, v in p.items() if k != "running"} for p in projects],
            "archived": archived_rows(),     # 已归档的项目（projects.json，新的在前）：侧栏「已归档」与它的抽屉
            "hostcheck": HC.view(t),                  # 看板服务所在机的体检（#166）：总览的「看板服务」小卡；不是 WSL 时 None
            "server": LS.view(t)}                     # 服务租约（#163）：这台是主还是备用；备用时不写快照，看板服务发页面时现填
    save_json(sd / "snapshot.json", snap)
    write_page("overview.html", snap, sd / "index.html")
    return snap


WAIT_WINDOW_S = 86400


def sched_view(hosts: list[dict], projects: list[dict], t: float, sw: dict | None = None, people: dict | None = None) -> dict | None:
    """工作台的调度卡：生效的设置与各项目的用量取最新一份 worker 状态的 share（各 worker 都按所有主机算，缺省值只在 worker）；
    各项目算在谁名下（people：fleet.project_people，缺省现算；#23 份额按人）：person 是名字、pk 是 sched.json 里的人员键（同一个 pk 的合起来算一份），
    看板上没有的项目两者都为 None（worker 里它们合起来算一份）；puse 是这人名下各项目的用量合计，取完整的 share.use（含已归档、卡上不列的项目），
    与 worker 的 person_use 同一口径（worker 不 import 本包，各一份）；use_sum 是 share.use 的总和（页面各占比的分母）；
    各项目此刻占几张卡（各主机 share.run 加起来）、排队几个（推演里还没开始的）、在等几个（有排位的）、
    近 1 天平均等多久（放行时记的 wait，按开始时刻取窗口：跑完的取本机存的用量记录 _keep_usage，在跑的取状态）、
    优先、暂停：null 或 {until（None 为无期限）, hosts}：项目级的（sw，主机清单 [sched.focus]、[sched.pause]，缺省现读）hosts 为 None，
    项目级的暂停另带 by（谁下的，config.pause_by；没记的为 None，当管理员下的，#16）；
    没有项目级的、只在几台单独下的（worker 状态 overrides 里不带 global 的）hosts 列出这几台，until 取最晚的；暂停另带 by：{主机: 谁下的}（不算 once，没记的为 None，#27）。
    项目取看板上的，另加此刻在跑、在等的别的项目名；没有 worker 状态带 share 时为 None。"""
    sts = [h["status"] for h in hosts if (h.get("status") or {}).get("share")]
    if not sts:
        return None
    sh = max(sts, key=lambda s: s.get("t", 0))["share"]
    allst = [h.get("status") or {} for h in hosts]
    ids = [p["id"] for p in projects]
    for st in allst:
        for j in st.get("running", []) + st.get("waiting", []):
            if j.get("project") and j.get("name") and j["project"] not in ids:
                ids.append(j["project"])
    recent = [(r.get("p"), r.get("wait")) for h in hosts for r in load_rows(usage_file(h["id"])) if r["s"] >= t - WAIT_WINDOW_S]
    recent += [(r.get("project"), r.get("wait")) for st in allst for r in st.get("running") or [] if (r.get("t0") or 0) >= t - WAIT_WINDOW_S]
    sw = sched(t) if sw is None else sw
    by = pause_by()
    people = F.project_people(ids) if people is None else people
    def flag(kind, pid):
        if pid in (sw.get(kind) or {}):
            return {"until": sw[kind][pid], "hosts": None, **({"by": by.get(pid)} if kind == "pause" else {})}
        on = [(h["id"], o) for h in hosts for o in (h.get("status") or {}).get("overrides") or []
              if o["kind"] == kind and o.get("target") == pid and not o.get("global")]
        if not on:
            return None
        return {"until": max((o.get("until") for _, o in on), key=lambda u: u or float("inf")), "hosts": sorted({h for h, _ in on}),
                **({"by": {h: o.get("by") for h, o in on if not o.get("once")}} if kind == "pause" else {})}
    keys = {p: F.share_key(n) for p, n in people.items() if n}
    puse = {}
    for q, v in sh["use"].items():
        puse[keys.get(q)] = puse.get(keys.get(q), 0.0) + v
    out = []
    for pid in ids:
        waits = [w for p, w in recent if p == pid and w is not None]
        who, pk = people.get(pid), keys.get(pid)
        out.append({"id": pid, "person": who, "pk": pk, "puse": round(puse.get(pk, 0.0), 3), "w": sh["weight"].get(pid, 1), "use": sh["use"].get(pid, 0.0),
                    "cards": round(sum(r[1] for st in allst for r in (st.get("share") or {}).get("run") or [] if r[0] == pid), 2),
                    "queued": sum(1 for st in allst for ln in (st.get("plan") or {}).get("lanes") or [] if ln.get("project") == pid
                                  for j in ln["jobs"] if not j.get("run")),
                    "waiting": sum(1 for st in allst for w in st.get("waiting") or [] if w.get("project") == pid and w.get("pos")),
                    "wait_avg": round(sum(waits) / len(waits), 1) if waits else None, "wait_n": len(waits),
                    "focus": flag("focus", pid), "paused": flag("pause", pid)})
    return {"cap_min": sh["wait_cap_min"], "hl_h": sh["half_life_h"], "use_sum": round(sum(sh["use"].values()), 3), "projects": out}


def platform_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status < 500
    except OSError:
        return False


def health(hosts: list[dict], cfg: dict, t: float, probe=platform_ok, per_host: dict | None = None) -> list[str]:
    """连通状态与租期：给每台主机填 e["health"] 与 e["lease_end"]，掉线、恢复、换了容器、租期将到都记进本机 events.jsonl。
    返回要弹系统通知的消息。per_host 给了的，掉线、恢复的消息另按主机记进去（{主机: [消息]}：另推给主机清单里这台 users 列着的人，#167）。判定在 health.py（link_step、container_step、lease_due），与组内服务器、项目页共用。

    连不上持续 health.DOWN_S 秒才算掉线，再按租期与平台能否打开判断原因；本机不在内网（e["lan_off"]）时不计连不上，租期照常提醒。
    正在做平台操作（fleetops.busy：续租、换人、关机）的主机连不上是预期的，不算掉线。
    租期（主机清单 lease_days）从容器启动时间算：
    这类平台到期后要停止再克隆作业，新容器的启动时间就是新租期的起点。"""
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    st = load_json(sd / "health.json", {})
    out, evs = [], []
    busy = FO.busy()
    for e in hosts:
        c, hs = cfg.get(e["id"], {}), st.setdefault(e["id"], {"fails": 0, "down": None, "warned": []})
        if e.get("error") and e["id"] in busy:                       # 正在续租（或换人、关机）：连不上是预期的，不算掉线
            e["health"] = {"state": "renew", "fails": 0, "since": None, "why": f"正在{FO.OP_NAMES[busy[e['id']]]}"}
            continue
        started = ((e.get("status") or {}).get("info") or {}).get("started")
        label = e["label"]
        step = link_step(hs, None if e.get("lan_off") else not e.get("error"), t)
        if e.get("lan_off"):
            pass
        elif e.get("error"):
            if step == "down":
                end = lease_end(hs.get("started"), c.get("lease_days"))
                if end and t >= end - 3600:
                    why = "租期到了，需在平台续租（停止后克隆）"
                elif c.get("platform") and not probe(c["platform"]):
                    why = "平台网页也打不开，多半是平台出了问题"
                elif c.get("platform"):
                    why = "平台能打开但这台连不上：作业可能被平台停掉了（平台异常或被误停），需在平台克隆重开"
                else:
                    why = "连不上"
                hs["down"] = why
                msg = f"{label} 掉线（{fmt_time(hs['since'], date=False)} 起连不上）：{why}"
                evs.append({"host": e["id"], "kind": "host_down", "msg": msg})
                out.append(msg)
                if per_host is not None:
                    per_host.setdefault(e["id"], []).append(msg)
        else:
            if step == "up":
                msg = f"{label} 恢复连接（离线 {round((t - hs['since']) / 60)} 分钟）"
                evs.append({"host": e["id"], "kind": "host_up", "msg": msg})
                out.append(msg)
                if per_host is not None:
                    per_host.setdefault(e["id"], []).append(msg)
            ev = container_step(hs, started, label)
            if ev:
                evs.append({"host": e["id"], **ev})
        e["health"] = {"state": "lan", "fails": 0, "since": None, "why": None} if e.get("lan_off") else \
            {"state": "down" if hs["down"] else "suspect" if hs["fails"] else "ok", "fails": hs["fails"],
             "since": hs.get("since") if hs["fails"] else None, "why": hs["down"]}
        end = lease_end(hs.get("started"), c.get("lease_days"))
        if end:
            e["lease_end"] = end
            if lease_due([(hs, end)], t):
                msg = (f"{label} 租期约剩 {(end - t) / 3600:.0f} 小时（{fmt_time(end)} 到期），"
                       "可在看板主机页点「预备续租」，等这格跑完再续")
                evs.append({"host": e["id"], "kind": "lease", "msg": msg})
                out.append(msg)
        miss = {w["name"]: w["why"] for w in ((e.get("status") or {}).get("waiting") or []) if w.get("miss")}
        for n, why in miss.items():               # 声明的本地依赖缺了：任务挡在队首不放行，通知一次
            if n not in hs.setdefault("miss", []):
                msg = f"{label} 的 {n} {why}"
                evs.append({"host": e["id"], "kind": "missing", "msg": msg})
                out.append(msg)
        if e.get("status") is not None:
            hs["miss"] = list(miss)
    log_events(evs, t)
    save_json(sd / "health.json", {k: {x: v[x] for x in HEALTH_KEYS if x in v} for k, v in st.items()})   # 只存还在用的键，不再认识的自然消失
    return out


HEALTH_KEYS = ("fails", "down", "since", "warned", "started", "miss")
RELAUNCH_GAP = 600


def relaunch(snap: dict, start=None) -> list[str]:
    """主机清单里标了 worker = "auto" 的主机：能连上而 worker 不在（多半是容器重启），就拉起 worker（10 分钟内最多一次）。
    手动 labmon worker stop 过的不拉，status.json 读不出的也不拉（worker 多半还在）。拉起记在本机 events.jsonl（输出放 detail），
    工作台的事件里能看到；返回的消息带上输出，调用方记进服务日志。start(主机) 返回 (退出码, 输出)。"""
    if start is None:
        start = lambda hid: run_out("start", hid, [])
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    last = load_json(sd / "relaunch.json", {})
    out = []
    for h in snap["hosts"]:
        if not h.get("auto") or h.get("error") or h.get("status_error") or h.get("stopped") or (h.get("status") and h.get("worker_up")):
            continue
        if time.time() - last.get(h["id"], 0) < RELAUNCH_GAP:
            continue
        last[h["id"]] = time.time()
        rc, text = start(h["id"])
        msg = (f"{h['label']}：worker 不在" + (f"（状态停在 {fmt_time(h['status']['t'])}）" if h.get("status") else "")
               + ("，已重新拉起" if rc == 0 else f"，拉起失败（退出码 {rc}）"))
        log_events([{"kind": "relaunch", "host": h["id"], "msg": msg, "detail": text.strip()[-400:]}])
        out.append(msg + (f"\n{text.strip()}" if text.strip() else ""))
    save_json(sd / "relaunch.json", last)
    return out


LOG_KEEP_S = 30 * 86400
LOG_SKIP = {"start", "grant", "done", "end", "failed"}   # 这几类项目快照里已有（任务起止），不重复存


def _keep_log(sd: Path, hid: str, events: list[dict], t: float) -> None:
    """把 worker 事件（只取最近 100 条）续写进本机 hostlog/<主机>.jsonl，存 30 天：
    worker 重启只留 2000 行、换容器会丢，项目页时间线要靠这里解释空档（drain、等待原因、队列结束等）。"""
    f = sd / "hostlog" / f"{hid}.jsonl"
    old = f.read_text().splitlines() if f.exists() else []
    seen = set(old[-400:])
    new = [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in events if e.get("kind") not in LOG_SKIP and e.get("t")]
    new = [l for l in dict.fromkeys(new) if l not in seen]
    if not new:
        return
    f.parent.mkdir(exist_ok=True)
    if len(old) > 3000:                               # 偶尔整理一次：去掉 30 天前的；写到一半的坏行（服务被杀时）一并丢掉
        keep = [l for l in old + new for r in parse_rows(l) if (r.get("t") or 0) >= t - LOG_KEEP_S]
        write_atomic(f, "\n".join(keep) + "\n")
    else:
        with open(f, "a") as fh:
            fh.write("\n".join(new) + "\n")


USAGE_KEEP_S = 30 * 86400


def _keep_usage(sd: Path, hid: str, rows: list[dict], t: float, idx: dict) -> None:
    """worker 的用量记录（任务结束时一行）续写进本机 usage/<主机>.jsonl，存 30 天，只留时间线与调度卡要的几项：
    worker 那边的 usage.jsonl 每次只取最后 60 行，时间线画已跑的占用要更长的。按 开始|任务|目录|清单 去重（同目录的两条清单可以有同名任务）。起止 s、t 是 epoch 秒（worker 行的 t0、t1）。
    idx：这台此刻各卡 uuid → 序号；用的卡除了 uuid（g）也存下序号（gi），换容器后 uuid 全变了还能按序号画。"""
    f = usage_file(hid)
    old = load_rows(f)
    seen = {(r["s"], r["n"], r.get("root"), r.get("queue")) for r in old[-500:]}
    for r in rows:
        s0, e = r.get("t0"), r.get("t1")
        if s0 is None or e is None or (s0, r.get("name"), r.get("root"), r.get("queue")) in seen or e < t - USAGE_KEEP_S:
            continue
        st = "stop" if r.get("requeued") or r.get("void") else "done" if r.get("rc") == "0" or (r.get("rc") is None and r.get("kind") == "slot") else "failed"
        g = r.get("gpus") or []
        append_row(f, {"t": e, "s": s0, "n": r.get("name"), "p": r.get("project"), "root": r.get("root"), "queue": r.get("queue"),
                       "g": g, "gi": [idx[u] for u in g if u in idx], "st": st, "wait": r.get("wait"),
                       "u": r.get("gpu_util")}, USAGE_KEEP_S, t)   # 所用卡的平均利用率（worker 的 util_mean）：项目近 24 小时的平均利用率（collect.gpu_mine）
        seen.add((s0, r.get("name"), r.get("root"), r.get("queue")))


def occupancy(hosts: list[dict], projects: list[dict], since: float, skip: str | None = None) -> dict:
    """各主机各卡被谁占着（时间线用）：已跑的取本机存的用量记录（_keep_usage），在跑与排队的取 worker 推演（status.plan）。
    不用卡的任务（占位、纯 CPU）不画。skip：不给这个项目的（项目页只看同机别的项目）。
    返回 {主机: [{p 项目, n 任务, lane 车道名, t0, t1, g [卡序号], st done/failed/stop/run/plan, after}]}；
    车道名取登记它的项目看板定的（projects 的 lanes），单元申请没有。"""
    names = {(ln.get("host"), ln.get("root"), ln.get("queue")): ln.get("label") for p in projects for ln in p.get("lanes") or []}
    out = {}
    for h in hosts:
        st = h.get("status") or {}
        idx = {g["uuid"]: g["i"] for g in (st.get("info") or {}).get("gpus") or []}
        rows = []
        for r in load_rows(usage_file(h["id"])):
            # 卡按 uuid 对到此刻的序号；换过容器的（uuid 全不在了）用记下时的序号 gi
            g = [idx[u] for u in r.get("g") or [] if u in idx] or r.get("gi")
            if g and r["t"] >= since and r.get("p") != skip:
                rows.append({"p": r.get("p"), "n": r["n"], "lane": names.get((h["id"], r.get("root"), r.get("queue"))), "t0": r["s"], "t1": r["t"], "g": g, "st": r["st"]})
        for ln in (st.get("plan") or {}).get("lanes", []):
            if ln.get("project") == skip:
                continue
            lab = names.get((h["id"], ln.get("root"), ln.get("queue")))
            for j in ln["jobs"]:
                g = [i for i in j.get("gpus") or [] if i is not None]
                if g and j.get("t1"):
                    rows.append({"p": ln.get("project"), "n": j["name"], "lane": lab, "t0": j["t0"], "t1": j["t1"], "g": g, "st": "run" if j.get("run") else "plan", **({"after": j["after"]} if j.get("after") else {})})
        out[h["id"]] = rows
    return out
