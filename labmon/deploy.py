"""部署（#164）：看板服务那份只放 main 的 checkout 收一个 git bundle、快进、按改动重启；本机部署脚本 tools/deploy.py 与服务端共用这里的判断。

- 哪些改动不用重启（is_doc）、服务日志里一次启动以来的 Traceback（log_text、tracebacks）：脚本的本机模式读本机日志，
  远端模式经 GET /api/admin/deploy 读服务端算好的，同一份判断。
- POST /api/admin/deploy {"bundle": base64}（serve.Handler 调 apply）：管理员的 deploy 作用域、只在内网入口（serve.ROUTES）。
  checkout 要在 main、没有未提交的改动；平台操作在做、另一次部署在做时拒绝；bundle 校验后 git fetch，不是快进的拒绝，再 merge --ff-only。
  要重启的起一个线程：等平台操作做完（最多 IDLE_MAX_S）再 service.restart_later，交给服务管理重启。
- 在跑的代码是哪个提交（RUNNING：服务启动时 mark_start 记一次），GET /api/me 与 GET /api/admin/deploy 都报它。
"""
from __future__ import annotations

import base64
import binascii
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import service as S

ROOT = Path(__file__).resolve().parent.parent      # 看板服务跑的那份 checkout
BRANCH = "main"
DOCS = ("docs/", "skills/", ".github/", "tests/", "tools/", ".claude/")    # 加上 *.md：看板服务与 worker 都不 import 它们，改了不用重启
WORKER = "labmon/labmon_worker.py"
IDLE_MAX_S = 1800        # 部署后等平台操作做完再重启，最多等这么久
IDLE_POLL_S = 5
RUNNING: dict = {"commit": None, "started": None}
RESTART: dict = {"at": None}     # 部署后安排了重启的时刻（_claim 记）：这之后平台操作不再开始（restarting）
RESTART_HOLD_S = 120             # 安排了这么久还没重启（服务管理没照做）就不再挡平台操作
_LOCK = threading.Lock()


class Refused(Exception):
    """不部署（回 409）：原因一句话。"""


def is_doc(f: str) -> bool:
    return f.endswith(".md") or f.startswith(DOCS)


def git(*args: str, timeout: float = 120) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        last = (r.stderr or r.stdout).strip().splitlines()
        raise Refused(f"git {args[0]} 失败：{last[-1] if last else r.returncode}")
    return r.stdout.strip()


def head() -> str | None:
    try:
        return git("rev-parse", "HEAD")
    except (Refused, OSError):
        return None


def mark_start() -> None:
    """服务启动时记下在跑的代码（之后 checkout 再快进也不变，直到重启）。"""
    RUNNING.update(commit=head(), started=round(time.time()))


# ---------- 服务日志 ----------
def log_text(off: int | None, log: Path | None = None) -> str:
    """服务日志：off 给了就是那个位置之后的（重启前记下的长度；文件变短说明轮转过，连 .1 一起）；
    没给就是最后一次「服务启动」那一行之后的。log 缺省 service.LOG。"""
    log = log or S.LOG
    rd = lambda p: p.read_bytes() if p.exists() else b""   # noqa: E731
    old, cur = rd(log.with_name(log.name + ".1")), rd(log)
    if off is not None:
        text = old[off:] + cur if len(cur) < off else cur[off:]
        return text.decode(errors="replace")
    text = (old + cur).decode(errors="replace")
    i = text.rfind("服务启动：")
    return text if i < 0 else text[i:].partition("\n")[2]


def tracebacks(text: str) -> list[str]:
    """日志里每个 Traceback 的异常那一行。"""
    lines, out = text.splitlines(), []
    for i, line in enumerate(lines):
        if "Traceback (most recent call last)" not in line:
            continue
        exc = next((m.strip() for m in lines[i + 1:] if m.strip() and not m.startswith((" ", "\t"))
                    and not m.startswith(("Traceback", "During handling", "The above exception"))), "（没找到异常行）")
        out.append(exc[:160])
    return out


# ---------- 服务端 ----------
def busy() -> dict:
    from . import fleetops
    return fleetops.busy()


def status() -> dict:
    """GET /api/admin/deploy：在跑的代码、checkout 现在的提交、启动时刻、正在做的平台操作、这次启动以来日志里的 Traceback。"""
    return {**RUNNING, "head": head(), "busy": busy(), "tracebacks": tracebacks(log_text(None))}


def apply(b64: str) -> dict:
    """收一个 git bundle（base64）快进 checkout 的 main。返回 {old, new, commits, files, restart, worker, msg}；不部署抛 Refused。
    old 是改动的起点：在跑的代码（RUNNING，是新提交的祖先时），否则 checkout 原来的提交；bundle 里就是 checkout 现在的提交也照这样算，
    上次快进后没重启成的这次补上重启。"""
    if not isinstance(b64, str) or not b64:
        raise Refused("请求体要有 bundle（git bundle 的 base64）")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise Refused("bundle 不是 base64") from None
    if not _LOCK.acquire(blocking=False):
        raise Refused("另一次部署正在做")
    try:
        if (b := busy()):
            raise Refused("平台操作在做（" + "、".join(f"{k} {v}" for k, v in b.items()) + "）：做完再部署")
        if not (ROOT / ".git").exists():
            raise Refused(f"看板服务不是从 git checkout 跑的（{ROOT}）")
        if git("rev-parse", "--abbrev-ref", "HEAD") != BRANCH:
            raise Refused(f"看板服务的 checkout 不在 {BRANCH} 上（它只放 {BRANCH}）")
        if git("status", "--porcelain", "--untracked-files=no"):
            raise Refused("看板服务的 checkout 有没提交的改动（它只放 main）")
        old = git("rev-parse", "HEAD")
        with tempfile.TemporaryDirectory(prefix="labmon-deploy-") as td:
            f = Path(td) / "main.bundle"
            f.write_bytes(data)
            git("bundle", "verify", str(f))
            git("fetch", "--no-tags", str(f), BRANCH)
        new = git("rev-parse", "FETCH_HEAD")
        if new != old:
            if not _ancestor(old, new):
                raise Refused(f"不是快进：checkout 的 {old[:7]} 不在 bundle 的 {new[:7]} 的历史里（服务端的 main 有别处来的提交？）")
            git("merge", "--ff-only", new)
        # 改动按在跑的代码算：上次快进了、重启却没成（restart_later 失败、等平台操作等满）时 checkout 已是新的，在跑的还是旧的，这次补上
        run = RUNNING["commit"]
        base = run if run and run != old and _ancestor(run, old) and any(not is_doc(x) for x in git("diff", "--name-only", run, old).splitlines()) else old
        commits = git("log", "--oneline", "--no-decorate", f"{base}..{new}").splitlines() if base != new else []
        files = git("diff", "--name-only", base, new).splitlines() if base != new else []
    finally:
        _LOCK.release()
    restart = any(not is_doc(x) for x in files)
    if restart:
        threading.Thread(target=_restart, name="deploy-restart", daemon=True).start()
    msg = (f"快进到 {new[:7]}（{len(commits)} 个提交）" if new != old else
           f"checkout 已是 {new[:7]}，在跑的是 {base[:7]}（{len(commits)} 个提交没生效）" if base != new else "已是这个提交，没动")
    return {"old": base, "new": new, "commits": commits, "files": files, "restart": restart, "worker": WORKER in files,
            "msg": msg + ("，马上重启" if restart else "，不用重启" if commits else "")}


def _ancestor(a: str, b: str) -> bool:
    try:
        git("merge-base", "--is-ancestor", a, b)
        return True
    except Refused:
        return False


def restarting() -> bool:
    """已安排重启、还没重启：平台操作不再开始（fleetops.start 在 op.json 的锁里问它）。安排后 RESTART_HOLD_S 还没重启的不再挡。"""
    at = RESTART["at"]
    return bool(at) and time.time() - at < RESTART_HOLD_S


def _claim() -> dict:
    """在 op.json 的锁里（与 fleetops.start 开新操作互斥）：平台操作没在做就记下要重启，返回空；在做就返回 {服务器: 操作}。"""
    from . import fleet as F
    from . import fleetops as FO

    def f(st):
        if st.get("active") and FO._live(st):
            return {i: st.get("kind") for i in st.get("ids") or []} or {"—": st.get("kind")}
        RESTART["at"] = time.time()
        return {}
    return F.edit_state("op.json", f)


def _restart(sleep=time.sleep, now=time.time) -> None:
    """部署后重启：平台操作在做就等它做完（最多 IDLE_MAX_S）；没在做就在同一把锁里记下要重启（之后不再开新的平台操作，
    免得被 restart_later 的那几秒延迟后的重启杀在半路），再交给服务管理重启（service.restart_later）。"""
    t0 = now()
    while (b := _claim()) and now() - t0 < IDLE_MAX_S:
        sleep(IDLE_POLL_S)
    if b:
        S.log(f"部署后等平台操作做完等了 {IDLE_MAX_S // 60} 分钟还在做（{b}），没重启：做完后 labmon service restart")
        return
    try:
        S.restart_later()
    except OSError as e:
        RESTART["at"] = None
        S.log(f"部署后重启没成：{e}")
