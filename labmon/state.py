"""本机状态目录与状态文件的底层读写：别的模块都建在它上面，它不 import 本包的其它模块。

    状态根目录：$LABMON_STATE（缺省 ~/.local/state/labmon）（state_root；tools/issue_watch.py 的已见列表直接放这里）
    状态目录：<状态根目录>/overview/（state_dir）
        snapshot.json   工作台最近一次的全局快照（overview.collect 写，各处经 snapshot() 读）
        其余文件见 overview.py、fleet.py、records.py 的说明

- JSON 状态文件：load_json、save_json（原子写入 write_atomic：先写临时文件再改名，读的一方不会读到半截）；
- JSONL（逐行记录）：parse_rows、load_rows、append_row（按保留期修剪；续写的进程内互斥按路径）。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path


def state_root() -> Path:
    """本机状态的根目录；只在这里读 LABMON_STATE。"""
    return Path(os.environ.get("LABMON_STATE", "~/.local/state/labmon")).expanduser()


def state_dir() -> Path:
    return state_root() / "overview"


def usage_file(hid: str) -> Path:
    """一台主机跑完的任务的记录（overview._keep_usage 从 worker 的 usage.jsonl 续写来的，存 30 天）：
    工作台的时间线（occupancy）、调度卡的平均等待（sched_view）与项目页的失败尝试（collect）都读它。"""
    return state_dir() / "usage" / f"{hid}.jsonl"


def load_json(path: Path, default):
    """读状态文件；没有或坏了返回 default。"""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def snapshot() -> dict:
    """工作台的全局快照（overview.collect 上一轮写的）；还没有就是空字典。"""
    return load_json(state_dir() / "snapshot.json", {}) or {}


def write_atomic(path: Path, text: str, mode: int | None = None) -> None:
    """整文件覆盖写（状态文件、JSONL 修剪、主机清单、页面）：先写临时文件再改名，读的一方不会读到半截，
    进程被杀在半途也不会丢整个文件。目录没有就建。mode：临时文件一建出来就是这个权限（账号文件 users.json 写 0o600，
    不先以缺省权限落盘再改）。临时文件名带随机串、以点开头：不占同目录里别的文件的名字（推上来的 done_when 文件可以叫 x.tmp，#161），
    两个线程同时写同一个文件也各用各的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    if mode is None:
        tmp.write_text(text)
    else:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        os.fchmod(fd, mode)                       # umask 会削掉 os.open 给的权限，这里按 mode 定死
        with os.fdopen(fd, "w") as f:
            f.write(text)
    os.replace(tmp, path)


def save_json(path: Path, obj) -> None:
    """写状态文件（原子写入，见 write_atomic）。"""
    write_atomic(path, json.dumps(obj, ensure_ascii=False))


_LOCKS: dict[str, threading.Lock] = {}          # 续写 JSONL 的进程内锁，按路径（看板的几个线程会同时记事件）
_LOCKS_GUARD = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.Lock())


def parse_rows(text: str) -> list[dict]:
    """JSONL 文本逐行解析，坏行跳过（写到一半被打断的那行）。"""
    rows = []
    for l in text.splitlines():
        try:
            rows.append(json.loads(l))
        except ValueError:
            continue
    return rows


def load_rows(path: Path) -> list[dict]:
    return parse_rows(path.read_text()) if path.exists() else []


def append_row(path: Path, row: dict, keep: float, t: float, slack: float = 86400) -> None:
    """续写一行；首行比保留期还早 slack 秒时整理一次，去掉保留期以前的（按 t，小时累计按 h）。
    同一文件的续写与整理在进程内按路径互斥：整理是“读全文 → 修剪 → 覆盖”，不锁的话别的线程同时追加的那行会丢。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path):
        with open(path, "a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(path) as fh:
            r0 = parse_rows(fh.readline())
        t0 = r0[0].get("t", r0[0].get("h")) if r0 else None
        if t0 is None or t0 < t - keep - slack:
            rows = [r for r in load_rows(path) if r.get("t", r.get("h", 0)) >= t - keep]
            write_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
