"""服务租约（#163，#157 B 节）：两个看板服务实例同时跑时（冷备恢复了一台、原来那台其实还活着），只有一台（主）动 worker 的输入、平台、组内服务器与通知。

    远端：每个 worker_home 下的 labmon_server.json {"holder": 实例 id, "renew": 那台主机的 epoch 秒, "ttl": 秒}（worker 不读它）
    本机：状态根下的 server.json {"id", "primary"（本机标记）, "role", "since", "t", "homes": {worker_home: 最近一次读到的}}（不进备份：每台机器各一份）

- 续约夹在 overview.fetch 那条 ssh 里（remote_sh）：先读改写租约，再按它的退出码决定写不写给 worker 的文件（put），不加连接。
  规则：holder 是自己、文件不存在、holder 空、或 renew 已过 ttl 才写；写完再读一次确认。文件写坏了不写（bad），要人 labmon server lease --take。
  读改写在旁边 .lock 上 flock（同 worker 的 flock：共享盘没起 lockd 时锁不上照做）；renew 取那台主机的时钟，只拿同一处的时钟比。
- 谁算主（decide）：这一轮读到的各 worker_home 都是自己的 → 主，本机标记置真；读到任一个在别人手里或写坏了 → 备用，标记置假；
  有的读不到（没有在用主机、都连不上、本机不在内网）且没读到别人的 → 照本机标记（容器重启、租期到了停机时主还是主：照样报掉线、能续租）。
- 门 primary()：采集（serve 的 Overview、Project）、组内服务器的探测与保活守护、平台操作（fleetops.start）、改 worker（workerctl）、
  通知（service.notify）、看板的写请求都先问它；server.json 还没有（这台没跑过租约）按主算，保持以前的行为。
- 备用时（tick）：每个 worker_home 挑一台连得上的在用主机读一次、能拿就拿；几个 worker_home 分在两台手里时 id 大的那台把手上的放掉（不会两边同时放、同时抢）。
- 释放：serve 收到 SIGTERM（systemd、launchd 停或重启服务都发它）时 release()；labmon service stop 另把本机标记置假（set_marker）。
"""
from __future__ import annotations

import json
import secrets
import shlex
import socket
import subprocess
import threading
import time

from . import lan as L
from .config import ConfigError, active_hosts, fmt_time, is_standalone, shell_path
from .records import log_events
from .state import load_json, save_json, state_root

TTL = 600                           # 租约多久不续就算没人持有（秒，按共享盘的时钟：远端脚本的 clock）
FILE = "labmon_server.json"
TIMEOUT = 30                        # 备用时读一次、释放、强制拿的 ssh 限时
RELEASE_S = 10                      # 停服务时释放租约最多等多久（systemd 缺省 90 秒、launchd 20 秒后就硬杀）
_LOCK = threading.Lock()

# 服务器上跑的那段（主机的 python，与 worker 同一个，3.8 能跑）：参数 文件 实例id 动作(renew|release|take|read) ttl。
# 打一行 JSON {"lease": 文件内容或 null, "bad": 写坏了的原因或 null, "now": 那台的 epoch 秒}；退出码 0 是“现在是我的”，3 不是。
REMOTE_PY = r'''
import fcntl, json, os, sys, time
p, me, mode, ttl = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
def rd():
    try:
        with open(p) as f:
            s = f.read()
    except FileNotFoundError:
        return None, None
    try:
        d = json.loads(s)
        ok = isinstance(d, dict) and isinstance(d.get("holder"), str) and all(isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool) for k in ("renew", "ttl"))
    except ValueError:
        ok = False
    return (d, None) if ok else (None, "不是 holder、renew、ttl 三项：" + s[:80])
os.makedirs(os.path.dirname(p), exist_ok=True)
lk = open(p + ".lock", "a")
try:
    fcntl.flock(lk, fcntl.LOCK_EX)
except OSError:
    pass
def clock():
    # 共享盘的时钟：同一个 worker_home 下几台的时钟未必一样，renew 与 now 都取它，比较才在同一个基准上。
    # 改锁文件的修改时刻为“现在”（NFS 上由服务器定时刻），再读回来；改不了的退回这台的时钟
    try:
        os.utime(p + ".lock", None)
        return int(os.stat(p + ".lock").st_mtime)
    except OSError:
        return int(time.time())
now = clock()
d, bad = rd()
free = bad is None and (d is None or not d["holder"] or now - d["renew"] > d["ttl"])
w = None
if mode == "take" or (mode == "renew" and (free or (d is not None and d["holder"] == me))):
    w = {"holder": me, "renew": now, "ttl": ttl}
elif mode == "release" and d is not None and d["holder"] == me:
    w = {"holder": "", "renew": now, "ttl": ttl}
if w is not None:
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with open(tmp, "w") as f:
        json.dump(w, f)
    os.replace(tmp, p)
    d, bad = rd()
print(json.dumps({"lease": d, "bad": bad, "now": now}))
sys.exit(0 if d is not None and d["holder"] == me else 3)
'''


# ---------------- 本机的身份与标记 ----------------
def _file():
    return state_root() / "server.json"


def _load() -> dict:
    return load_json(_file(), {}) or {}


def ident() -> str:
    """这台看板服务的实例 id（机器名 + 首次生成的随机串，记在 server.json；恢复备份时保留这台原来的，备份里不带）。"""
    with _LOCK:
        st = _load()
        if not st.get("id"):
            st["id"] = f"{socket.gethostname().split('.')[0][:24] or 'labmon'}-{secrets.token_hex(3)}"
            save_json(_file(), st)
        return st["id"]


def set_marker(on: bool, why: str) -> None:
    """改本机标记（读不到租约时照它）：恢复备份、--take 置真，labmon service stop 置假。记一条。"""
    me = ident()
    with _LOCK:
        st = _load()
        st.update(primary=on, role="primary" if on else "standby", t=round(time.time()))
        save_json(_file(), st)
    log_events([{"kind": "server", "msg": f"这台看板服务（{me}）{'记为主' if on else '记为备用'}：{why}"}])


def known() -> bool:
    """这台跑过租约没有（server.json 里有身份与角色）。"""
    return bool(_load().get("role"))


def primary() -> bool:
    """门：这台此刻是不是主。没跑过租约（server.json 还没有角色）按主算：升级后的第一轮之前、测试与命令行照旧。"""
    return _load().get("role", "primary") == "primary"


def flag() -> bool | None:
    """/healthz 的 primary：是主 True、备用 False；没跑过租约 None（看门狗只把 False 当异常）。"""
    st = _load()
    return st["role"] == "primary" if st.get("role") else None


def view(t: float | None = None) -> dict | None:
    """页面与 /api 用的主备情况（全局快照的 server）：role（primary / standby）、id（这台）、holder（主是谁）、renew（最后续约）、
    until（最晚何时这台能接管）、bad（租约文件写坏了的原因）、home（哪个 worker_home）、say（备用时页面横幅那句，text）；时刻都换成本机时钟。没跑过租约是 None。"""
    st = _load()
    if not st.get("role"):
        return None
    out = {"role": st["role"], "id": st.get("id"), "since": st.get("since"), "holder": None, "renew": None, "until": None, "bad": None, "home": None}
    for home, r in sorted((st.get("homes") or {}).items()):
        if r.get("bad") or (r.get("holder") and r["holder"] != st.get("id")):
            skew = (r.get("at") or 0) - (r.get("now") or 0)          # 本机时钟 − 共享盘的时钟
            out.update(home=home, bad=r.get("bad"), holder=r.get("holder"),
                       renew=r["renew"] + skew if r.get("renew") is not None else None,
                       until=r["renew"] + r.get("ttl", TTL) + skew if r.get("renew") is not None and not r.get("bad") else None)
            break
    out["say"] = text(out)
    return out


def text(v: dict | None = None) -> str:
    """备用时的那句话（页面横幅、写请求的 409、命令行的拒绝同一句）。"""
    v = view() if v is None else v
    if not v or v["role"] != "standby":
        return ""
    if v.get("bad"):
        return (f"这台看板服务是备用：{v['home']} 下的租约文件写坏了（{v['bad']}），两边都不自动抢；"
                "确认没有别的看板服务在跑后，在这台上 labmon server lease --take")
    if v.get("holder"):
        return (f"这台看板服务是备用：主是 {v['holder']}，最后续约 {fmt_time(v['renew'])}；"
                f"它停了的话最晚 {fmt_time(v['until'])} 起这台自动接管。要动东西到主那台做")
    return "这台看板服务是备用（租约不在这台手里）：要动东西到主那台做；确认没有别的看板服务在跑时 labmon server lease --take"


def need(what: str = "这件事") -> None:
    """改东西之前的门：备用时报 ConfigError（命令行打印、看板回 409）。"""
    if not primary():
        raise ConfigError(f"{what}没做：{text()}")


# ---------------- 远端那一段 ----------------
def path(home: str) -> str:
    return home.rstrip("/") + "/" + FILE


def _cmd(h: dict, mode: str, me: str) -> str:
    return f"{shlex.quote(h['python'])} -c {shlex.quote(REMOTE_PY)} {shell_path(path(h['worker_home']))} {shlex.quote(me)} {mode} {TTL}"


def remote_sh(h: dict, mode: str = "renew") -> str:
    """overview.fetch 那条命令开头的一段：跑租约，读回的那行存进 $lease，$lok 为 0 是“现在是我的”（后面写 put 前看它）。"""
    return f"lease=$({_cmd(h, mode, ident())} 2>&1); lok=$?; "


def parse(raw: str | None) -> dict | None:
    """远端那行 → {"lease", "bad", "now"}；没跑成（python 不在、共享盘写不了）是 None（当这一轮读不到）。"""
    for line in reversed((raw or "").strip().splitlines()):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and "now" in r:
            return r
    return None


def _run(h: dict, mode: str, me: str, timeout: float = TIMEOUT) -> dict | None:
    """在一台主机上单独跑一次租约（备用时读、释放、强制拿）：走它的长连接；没有 ssh 的（测试）在本机跑。"""
    remote = _cmd(h, mode, me)
    try:
        cmd = L.host_ssh_argv(h) + [remote] if h.get("ssh") else ["bash", "-c", remote]
    except ConfigError:
        return None
    try:
        r = (L.ssh_run if h.get("ssh") else subprocess.run)(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None
    return parse(r.stdout)


def homes(hosts: list[dict] | None = None) -> dict[str, list[dict]]:
    """参与主备服务租约仲裁的在用主机按共享域分组（照主机清单顺序）：
    存在非独立（共享盘）主机时，仅以共享盘为仲裁点，排除独立主机（避免笔记本休眠或外租机断连导致主看板误降备）；
    全部都是独立主机时按主机隔离键分组，防止多台独立机默认 worker_home 撞名互覆盖。"""
    act = active_hosts() if hosts is None else hosts
    shared = [h for h in act if not is_standalone(h)]
    out: dict = {}
    if shared:
        for h in shared:
            out.setdefault(h["worker_home"].rstrip("/"), []).append(h)
        return out
    for h in act:
        out.setdefault(f"{h['id']}:{h['worker_home'].rstrip('/')}", []).append(h)
    return out


def op(mode: str, hosts: list[dict] | None = None, me: str | None = None, timeout: float = TIMEOUT) -> dict[str, dict | None]:
    """每个 worker_home 挑一台连得上的在用主机跑一次 mode：{worker_home: 读回的（读不到 None）}。本机不在内网时远程的不连。"""
    me = me or ident()
    hs = homes(hosts)
    far = any(h.get("ssh") for g in hs.values() for h in g)
    lan = not far or L.check()["ok"]
    out = {}
    for home, group in hs.items():
        out[home] = None
        for h in group:
            if h.get("ssh") and not lan:
                continue
            r = _run(h, mode, me, timeout)
            if r is not None:
                out[home] = {**r, "at": time.time()}
                break
    return out


# ---------------- 谁算主 ----------------
def _mine(r: dict, me: str) -> bool:
    return bool(r.get("lease")) and r["lease"].get("holder") == me


def decide(readings: dict[str, list[dict]], hs: list[str], t: float | None = None) -> str:
    """按这一轮读到的定主备，记进 server.json；返回 "primary" 或 "standby"。
    readings：{worker_home: [各台读回的]}（同一个 worker_home 下几台都续，哪台读到都算）；hs：这一轮在用主机的 worker_home。
    角色变了记一条；从主变成备用、或发现租约文件写坏了，推一条通知（这时另一台是主，不会重复）。"""
    t = time.time() if t is None else t
    me = ident()
    with _LOCK:
        st = _load()
        was = st.get("role")
        seen = dict(st.get("homes") or {})
        lost, bad = False, None
        got = 0
        for home in hs:
            rs = [r for r in readings.get(home) or [] if r]
            if not rs:
                continue
            got += 1
            pick = next((r for r in rs if r.get("bad")), None) or next((r for r in rs if not _mine(r, me)), None) or rs[-1]
            ls = pick.get("lease") or {}
            seen[home] = {"holder": ls.get("holder"), "renew": ls.get("renew"), "ttl": ls.get("ttl"), "now": pick.get("now"),
                          "at": round(pick.get("at") or t), "bad": pick.get("bad")}
            if pick.get("bad"):
                bad = bad or (home, pick["bad"])
                lost = True
            elif not _mine(pick, me):
                lost = True
        seen = {k: v for k, v in seen.items() if k in hs}
        if lost:
            on = False
        elif hs and got == len(hs):
            on = True
        else:                                       # 有的读不到、读到的都是自己的：照本机标记
            on = st.get("primary", True)
        role = "primary" if on else "standby"
        st.update(id=me, primary=on, role=role, t=round(t), homes=seen, since=st.get("since") if was == role else round(t))
        save_json(_file(), st)
    if was != role and (was or role == "standby"):     # 第一次跑租约就是主（升级后、新装）不算一件事
        v = view(t)
        msg = (f"这台看板服务（{me}）成为主：开始采集、续约" if role == "primary" else text(v))
        log_events([{"kind": "server", "msg": msg}], t)
        from .service import log, notify            # service 在 notify 里延迟 import 本模块，这里同样延迟
        log(msg)
        if role == "standby" and (was == "primary" or bad):
            notify([msg], force=True)
    return role


def tick(hosts: list[dict] | None = None, t: float | None = None) -> str:
    """备用时每轮做的：每个 worker_home 读一次、能拿就拿；几个 worker_home 分在两台手里时 id 大的放掉手上的。返回定下的角色。"""
    me = ident()
    hs = homes(hosts)
    got = op("renew", hosts, me)
    others = [r["lease"]["holder"] for r in got.values() if r and r.get("lease") and r["lease"].get("holder") not in ("", me)]
    if others and any(o < me for o in others) and any(r and _mine(r, me) for r in got.values()):
        rel = op("release", [h for home, g in hs.items() if got.get(home) and _mine(got[home], me) for h in g], me)
        got.update({k: v for k, v in rel.items() if v})
    return decide({home: [r] for home, r in got.items() if r}, list(hs), t)


def release(timeout: float = RELEASE_S) -> dict[str, dict | None]:
    """停服务时：是自己的租约清空 holder（下一台立即能拿），本机标记不动（重启后照旧是主）。"""
    if not known():
        return {}
    return op("release", timeout=timeout)


def take() -> dict[str, dict | None]:
    """labmon server lease --take：各 worker_home 强制写成自己（坏文件、确认没有别的服务在跑时），本机标记置真。"""
    got = op("take")
    set_marker(True, "labmon server lease --take")
    hs = homes()
    decide({home: [r] for home, r in got.items() if r}, list(hs))
    return got


def cli(argv: list[str]) -> int:
    """labmon server lease [--take | --release]：看这台的主备与各 worker_home 的租约；强制拿到；释放。"""
    import argparse
    ap = argparse.ArgumentParser(prog="labmon server lease", description="服务租约：这台看板服务是主还是备用")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--take", action="store_true", help="强制拿到各 worker_home 的租约（租约文件写坏了、或确认没有别的看板服务在跑时）")
    g.add_argument("--release", action="store_true", help="释放（是这台的才清空），本机标记不动")
    a = ap.parse_args(argv)
    if a.take:
        got = take()
    elif a.release:
        got = release(TIMEOUT)
    else:
        got = op("read")
    me = ident()
    for home, r in got.items():
        if r is None:
            print(f"  {home}：读不到（在用主机都连不上，或本机不在内网）")
            continue
        ls = r.get("lease") or {}
        who = "这台" if ls.get("holder") == me else ls.get("holder") or "没人"
        print(f"  {home}：" + (f"写坏了（{r['bad']}）" if r.get("bad") else
                              f"{who}持有，最后续约 {fmt_time(ls['renew'] + r['at'] - r['now'])}（共享盘的时钟 {fmt_time(ls['renew'])}），ttl {ls.get('ttl')} 秒" if ls else "还没有"))
    if not got:
        print("  没有在用主机：读不到租约，照本机标记")
    st = _load()
    print(f"[labmon] 这台是 {me}，" + ({"primary": "主", "standby": "备用"}.get(st.get("role"), "还没跑过租约（按主算）"))
          + f"；本机标记 {'主' if st.get('primary', True) else '备用'}")
    return 0
