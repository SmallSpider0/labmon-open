"""本机在不在内网：电脑带出去用时，平台与各服务器都连不上，不该当成它们都掉线了。

全局页采集、组内服务器轮探、项目页采集之前调 check()，有 ssh 连不上时再调一次。判断尽量不新开连接（频繁短连接可能被平台限流）：
- 各处 ssh 走 ControlMaster 长连接，每次连上、连不上都用 note() 记下；最近 FRESH 秒内连上过、之后没有连不上，就算在内网，不连网络；
- 否则才试连：先连平台网页端口，连不上再并发连各主机、各组内服务器的 ssh 端口（各 TIMEOUT 秒），
  有一个连得上（或被拒绝，说明网络是通的）就算在内网。在内网时只在有服务器连不上后试连一次，离线时每 TTL 秒一次。
离线时这几处都不连 ssh、不记连不上、不报掉线，统计里这段算没有数据；平台操作与立即探测直接拒绝。
结果几个线程共用；离线的起点写进状态目录 lan.json，服务重启后接着算。
测试可设环境变量 LABMON_LAN=on|off 直接指定。

ssh_argv / ssh_run：各处（全局页、项目页、组内服务器、worker 命令）连服务器的 ssh 都从这里拼、从这里跑，共用一条 ControlMaster 长连接。
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .config import ConfigError, active_hosts, host_ssh_key, is_standalone
from .records import log_events
from .state import load_json, save_json, state_dir

TTL = 20
FRESH = 180
TIMEOUT = 1.5
ERR = "本机不在内网"
_lock = threading.Lock()
_cache: dict = {"t": 0.0, "v": None}
_ssh: dict = {"ok": 0.0, "fail": 0.0}
CTL = os.path.expanduser("~/.ssh/cm-labmon-%C")


def ctl_path(ctl_tag: str | None = None) -> str:
    """SSH ControlMaster 套接字路径：无 ctl_tag 沿用全局 CTL；带 ctl_tag（外部机/专用密钥）按标签哈希隔离，
    防止多用户或换钥复用旧连接，且控制长度不超过 UNIX domain socket 路径上限。"""
    if not ctl_tag:
        return CTL
    tag = hashlib.sha256(str(ctl_tag).encode()).hexdigest()[:12]
    return os.path.expanduser(f"~/.ssh/cm-labmon-{tag}-%C")


def ext_known_hosts(host_id: str) -> Path:
    """外部机/独立机专用的 known_hosts 文件（<state_dir>/known_hosts_ext/<host_id>），不与宿主机 ~/.ssh/known_hosts 混用。"""
    s = str(host_id or "").strip()
    if not s or "/" in s or "\\" in s or s.startswith(".") or ".." in s:
        raise ConfigError(f"主机 id 不合法：{host_id!r}")
    return state_dir() / "known_hosts_ext" / s


def ssh_argv(target: str, port, *extra: str, known_hosts=None, identity: str | None = None, ctl_tag: str | None = None) -> list[str]:
    """连一台服务器的 ssh 命令（不含远端命令）：每个 sshd 只保持一条长连接，15 分钟不用才断，30 秒心跳（断网后约 90 秒失效）。
    主机密钥 accept-new：第一次连（续租后 fleet.forget 删掉旧条目，新容器就算第一次）自动记下，变了照样拒连。
    known_hosts：记在这个文件里（不给就是 ssh 缺省的 ~/.ssh/known_hosts；经 fleet.ssh 连的哪台记哪份由 fleet._ssh_cmd 定）。
    identity：专用私钥路径；传入时强制 -i <identity> -o IdentitiesOnly=yes，禁止回退尝试看板宿主机 ~/.ssh/ 的默认密钥。
    ctl_tag：按主机/归属隔离的 ControlPath 标签（防多用户或换钥复用旧长连接）。"""
    kh = ["-o", f"UserKnownHostsFile={known_hosts}"] if known_hosts else []
    ident = ["-i", str(identity), "-o", "IdentitiesOnly=yes"] if identity else []
    return ["ssh", "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl_path(ctl_tag)}", "-o", "ControlPersist=15m",
            "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "LogLevel=ERROR",
            "-o", "StrictHostKeyChecking=accept-new", *ident, *kh, "-p", str(port), *extra, target]


def host_ssh_argv(h: dict, *extra: str) -> list[str]:
    """按主机配置拼 ssh_argv（唯一实现，overview / workerctl / collect / lease 共用）：
    使用托管密钥（ssh_key=managed）或独立主机时，自动启用按机隔离的 ControlPath 与独立 known_hosts，
    托管密钥额外强制 -i <key> -o IdentitiesOnly=yes；旧集群主机完全沿用原参数。"""
    from .platform import ssh_identity               # platform 在顶层 import config，延迟 import 避免循环
    managed = host_ssh_key(h) == "managed"
    standalone = is_standalone(h)
    identity = ssh_identity(h) if managed else None
    kh = h.get("known_hosts")
    if not kh and (managed or standalone):
        p = ext_known_hosts(str(h["id"]))
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        kh = str(p)
    tag = f"{h.get('owner') or ''}:{h['id']}" if (managed or standalone) else None
    return ssh_argv(h["ssh"], h["port"], *extra, known_hosts=kh, identity=identity, ctl_tag=tag)


def close_host_ssh(h: dict, *, drop_known_hosts: bool = False) -> None:
    """切断一台主机的 ControlMaster 复用连接；drop_known_hosts 为真时同步清理该机独立 known_hosts（绝不触碰宿主机 ~/.ssh/known_hosts）。"""
    if h.get("ssh"):
        try:
            subprocess.run(host_ssh_argv(h, "-O", "exit"), capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if drop_known_hosts and h.get("id") is not None:
        try:
            ext_known_hosts(str(h["id"])).unlink(missing_ok=True)
        except (ConfigError, OSError):
            pass


def ssh_run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """跑一条 ssh（参数同 subprocess.run），顺手记下连上没有（退出码 255 或超时算连不上）。超时照样抛 TimeoutExpired。"""
    try:
        r = subprocess.run(cmd, **kw)
    except subprocess.TimeoutExpired:
        note(False)
        raise
    note(r.returncode != 255)
    return r


def note(ok: bool, t: float | None = None) -> None:
    """一次 ssh 连上了（ok）或连不上（退出码 255、超时）。远端命令本身出错不算。"""
    _ssh["ok" if ok else "fail"] = time.time() if t is None else t


def targets() -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """要试连的 (主机, 端口)：(平台网址, 在用主机与组内服务器的 ssh)。"""
    from . import fleet as F                 # fleet 在上层 import 本模块（ssh_argv），这里只能延迟 import
    web, out = set(), set()
    for h in active_hosts():
        if h.get("ssh"):
            out.add((h["ssh"].split("@")[-1], int(h["port"])))
        if h.get("platform"):
            u = urlparse(h["platform"])
            if u.hostname:
                web.add((u.hostname, u.port or (443 if u.scheme == "https" else 80)))
    try:
        cfg = F.conf()
    except Exception:  # noqa: BLE001 — 主机清单写错时只看在用主机
        cfg = None
    for s in (cfg or {}).get("servers", []):
        if s.get("ssh"):
            out.add((s["ssh"].split("@")[-1], int(s["port"])))
    return sorted(web), sorted(out - web)


def _reach(hp: tuple[str, int]) -> bool:
    try:
        with socket.create_connection(hp, timeout=TIMEOUT):
            return True
    except ConnectionRefusedError:
        return True
    except OSError:
        return False


def _probe() -> bool:
    web, ts = targets()
    if not web and not ts or any(_reach(x) for x in web):          # 平台网页一个一个试；它连上了就不碰各台 sshd
        return True
    with ThreadPoolExecutor(max_workers=max(1, len(ts))) as ex:
        return any(ex.map(_reach, ts))


def check(t: float | None = None, force: bool = False) -> dict:
    """{"ok": 在不在内网, "since": 离线起点, "checked": 这次查的时间}。没有要连的目标就算在。
    force：不用 TTL 内的缓存（平台操作前），最近 ssh 连上过照样不连网络。"""
    t = time.time() if t is None else t
    with _lock:
        env = os.environ.get("LABMON_LAN")
        fresh = env not in ("on", "off") and _ssh["ok"] > _ssh["fail"] and t - _ssh["ok"] < FRESH
        if fresh and _cache["v"] and _cache["v"]["ok"]:
            return _cache["v"]
        if not force and not fresh and _cache["v"] and t - _cache["t"] < TTL and _ssh["fail"] <= _cache["t"]:
            return _cache["v"]
        if env in ("on", "off"):
            ok = env == "on"
        else:
            ok = fresh or _probe()
        f = state_dir() / "lan.json"
        old = load_json(f, {})
        since = None if ok else ((old.get("since") or t) if old.get("ok") is False else t)    # 接着上次离线的起点
        v = {"ok": ok, "since": since and round(since), "checked": round(t)}
        if old.get("ok") != ok or old.get("since") != v["since"]:
            state_dir().mkdir(parents=True, exist_ok=True)
            save_json(f, v)
        if old.get("ok") is not None and old["ok"] != ok:          # 进出内网记一条（第一次判定不记）
            log_events([{"kind": "lan", "msg": "本机离开内网：平台与各服务器连不上，暂停探测" if not ok
                         else f"本机回到内网（离开了约 {max(1, round((t - (old.get('since') or t)) / 60))} 分钟）"}], t)
        _cache.update(t=t, v=v)
        return v


def gather(items: dict, fetch, remote, t: float | None = None) -> tuple[dict, dict]:
    """并行探测 items（{主机: 参数}，一键一台，各台走自己那条 ControlMaster 长连接，不多开连接；一台超时不拖慢别的），
    remote(键) 表示要连远程。本机不在内网时远程的不连，记 (None, ERR)；
    有远程的连不上，再查一次是不是刚离开内网，是就都记成 ERR（不算连不上）。返回 (内网状态, {键: (raw, err)})。"""
    far = [k for k in items if remote(k)]
    lan = check(t) if far else {"ok": True, "since": None}
    with ThreadPoolExecutor(max_workers=max(1, len(items))) as ex:
        got = dict(zip(items, ex.map(lambda k: fetch(items[k]) if lan["ok"] or k not in far else (None, ERR), items)))
    if lan["ok"] and any(got[k][0] is None for k in far) and not check(t)["ok"]:
        lan = state()
        got.update({k: (None, ERR) for k in far})
    return lan, got


def state() -> dict:
    """上次查的结果（不连网络）：页面用。"""
    return _cache["v"] or load_json(state_dir() / "lan.json", {"ok": True, "since": None})
