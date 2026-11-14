#!/usr/bin/env python3
"""issue_watch：值守会话的常驻等待，本仓库出现新 Issue 或看板有新问题报告才退出（Issue #44）。

    python3 -I tools/issue_watch.py wait [--every 300] [--reports-token 文件]   # 后台跑；有新 Issue 时打印每条并退出（退出码 0）
    python3 -I tools/issue_watch.py mark 48 49           # 把这几个号记为已见（自己 gh issue create 开的，紧接着跑）
    python3 -I tools/issue_watch.py seen                 # 列出已见的 Issue 号
    python3 -I tools/issue_watch.py forget 43 44         # 把这几个号标回未见（测试用：下一次 wait 会报它们）

- 每 --every 秒用 `gh issue list --state all` 查一次，与已见列表比；没有新的就接着睡，什么都不输出。
- 新 Issue 每条一行：号、标签、标题、作者、链接，已经关了的标“已关”；打印前先记为已见，下次不再报。
- 已见列表在状态根目录（labmon.state.state_root：$LABMON_STATE，缺省 ~/.local/state/labmon）的 issue_watch/<owner>--<name>.json，
  读写经 labmon.state；没有或写坏了当作首次运行：把现有 Issue 全记为已见、不报，接着等
  （成功返回空列表也初始化，支持刚创建、没有 Issue 的仓库）。
- wait 每轮查之前重读已见列表、与内存里的取并集，存之前再并一次：别人中途 mark 的号它不报、也不会被它覆盖掉
  （Issue #50）。所以 forget 只对下一次启动的 wait 生效，正在跑的那个内存里还记着。
- mark 时还没有已见列表（首次 wait 之前）就什么都不写、退出码 0：首次 wait 会把那时现有的 Issue 全记为已见，
  这几个号自然在内；这时若只写这几个号，首次 wait 会把列表当成已有的，把其余现有 Issue 全当新的报。
- gh 出错（网络、超时、输出不是 JSON）静默重试；连续失败满 FAIL_SAY 秒才往 stderr 打一行，接着等，不退出。
- 报告经 labmon CLI 只读获取，另一份已见列表记报告编号。无令牌时只查 Issue。脚本自身支持 Python 3.8+。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from labmon import state  # noqa: E402
from labmon.state import load_json, save_json  # noqa: E402

REPO = "SmallSpider0/labmon-open"
EVERY = 300          # 缺省多久查一次（秒）
FAIL_SAY = 3600      # gh 连续失败多久才说一句（秒）；说过之后再满这么久才说下一句
GH_TIMEOUT = 60

_now = time.time      # 测试打桩
_sleep = time.sleep


def state_file() -> Path:
    return state.state_root() / "issue_watch" / (REPO.lower().replace("/", "--") + ".json")


def load_seen(path: Path):
    """已见的 Issue 号集合；没有或写坏了返回 None（当作首次运行）。"""
    d = load_json(path, None)
    try:
        return {int(n) for n in d["seen"]}
    except (TypeError, KeyError, ValueError):
        return None


def save_seen(path: Path, seen: set) -> None:
    save_json(path, {"seen": sorted(seen)})


def gh_issues() -> list:
    """本仓库的全部 Issue（不含 PR）；gh 出错抛 RuntimeError。"""
    cmd = ["gh", "issue", "list", "--repo", REPO, "--state", "all", "--limit", "1000",
           "--json", "number,title,state,labels,author,createdAt,url"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(str(e))
    if r.returncode:
        last = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(last[-1] if last else f"退出码 {r.returncode}")
    try:
        rows = json.loads(r.stdout)
        return [x for x in rows if isinstance(x.get("number"), int)]
    except (ValueError, AttributeError) as e:
        raise RuntimeError(f"gh 输出不是 Issue 列表：{e}")


def line(x: dict) -> str:
    labels = ",".join(l.get("name", "") for l in x.get("labels") or []) or "无标签"
    who = (x.get("author") or {}).get("login") or "?"
    url = x.get("url") or f"https://github.com/{REPO}/issues/{x['number']}"
    closed = "（已关）" if x.get("state") == "CLOSED" else ""
    return f"#{x['number']}{closed} [{labels}] {x.get('title', '')} — {who} {url}"


def merge_seen(path: Path, seen):
    """内存里的已见与文件里的取并集（别人中途 mark 进去的号）；两边都没有时仍是 None（首次运行）。"""
    disk = load_seen(path)
    if seen is None or disk is None:
        return disk if seen is None else seen
    return seen | disk


def board_reports(token: Path) -> list:
    """经已安装的 labmon 复用看板连接与认证。只接收固定格式的元数据。"""
    try:
        r = subprocess.run(["labmon", "report", "--list", "--token-file", str(token)],
                           capture_output=True, text=True, timeout=GH_TIMEOUT)
        if r.returncode:
            raise RuntimeError(f"读取报告退出码 {r.returncode}")
        rows = json.loads(r.stdout)["reports"]
        if not isinstance(rows, list) or any(
                not isinstance(e, dict)
                or not re.fullmatch(r"R-[0-9a-f]{32}", str(e.get("report_id")))
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,31}", str(e.get("reporter")))
                or type(e.get("t")) is not int for e in rows):
            raise ValueError
        return rows
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError) as e:
        raise RuntimeError("读取报告失败") from e


def new_issues(path: Path, seen: set | None) -> tuple:
    seen = merge_seen(path, seen)
    issues = gh_issues()
    if seen is None:
        seen = {x["number"] for x in issues}
        save_seen(path, seen)
        return [], seen
    new = sorted((x for x in issues if x["number"] not in seen), key=lambda x: x["number"])
    if new:
        seen = merge_seen(path, seen | {x["number"] for x in new})
        save_seen(path, seen)
    return new, seen


def new_reports(path: Path, token: Path) -> list:
    rows = board_reports(token)
    saved = load_json(path, {})
    ids = saved.get("seen") if isinstance(saved, dict) else None
    seen = {x for x in ids if isinstance(x, str)} if isinstance(ids, list) else set()
    new = [x for x in reversed(rows) if x["report_id"] not in seen]
    if new:
        save_json(path, {"seen": sorted(seen | {x["report_id"] for x in new})})
    return new


def wait(every: float, path: Path, report_token: Path | None = None) -> int:
    seen = None

    def check_issues():
        nonlocal seen
        rows, seen = new_issues(path, seen)
        return rows

    checks = [("gh", check_issues)]
    if report_token:
        checks.append(("看板", lambda: new_reports(path.with_name(path.stem + "-reports.json"), report_token)))
    failed = {}
    while True:
        found = {}
        for source, check in checks:
            try:
                found[source] = check()
            except RuntimeError as e:
                t = _now()
                since = failed.setdefault(source, t)
                if t - since >= FAIL_SAY:
                    print(f"issue_watch：{source} 已连续失败 {round((t - since) / 60)} 分钟，接着重试：{e}",
                          file=sys.stderr, flush=True)
                    failed[source] = t
            else:
                failed.pop(source, None)
        if found.get("gh"):
            print(f"{len(found['gh'])} 个新 Issue：")
            for x in found["gh"]:
                print(line(x))
        if found.get("看板"):
            print(f"{len(found['看板'])} 个新问题报告（到看板「记录」按编号查阅）：")
            for x in found["看板"]:
                print(f"{x['report_id']} — {x['reporter']}，提交时间戳 {x['t']}")
        if any(found.values()):
            return 0
        _sleep(every)


def main(argv=None) -> int:
    global REPO
    ap = argparse.ArgumentParser(description="值守会话的常驻等待：出现新 Issue 才退出")
    ap.add_argument("--repo", default=REPO, help="值守仓库，格式 owner/name；默认公开 labmon 仓库")
    ap.add_argument("--state", type=Path, help="已见列表的文件（缺省按仓库隔离）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wait", help="等到出现新 Issue，打印后退出")
    w.add_argument("--reports-token", type=Path, help="值守读取问题报告的令牌文件")
    w.add_argument("--every", type=float, default=EVERY, help=f"多久查一次（秒，缺省 {EVERY}）")
    m = sub.add_parser("mark", help="把这几个号记为已见（自己开的 Issue，开完紧接着跑）；"
                                    "还没有已见列表时不写、退出码 0：首次 wait 会把现有的全记为已见")
    m.add_argument("nums", type=int, nargs="+")
    sub.add_parser("seen", help="列出已见的 Issue 号")
    f = sub.add_parser("forget", help="把这几个号标回未见（只对下一次启动的 wait 生效）")
    f.add_argument("nums", type=int, nargs="+")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repo):
        ap.error("仓库要写成 owner/name")
    REPO = a.repo
    path = a.state or state_file()
    if a.cmd == "wait":
        return wait(a.every, path, a.reports_token.expanduser() if a.reports_token else None)
    seen = load_seen(path)
    if a.cmd == "seen":
        print(" ".join(f"#{n}" for n in sorted(seen)) if seen else
              "已见列表是空的" if seen is not None else "还没有已见列表（首次 wait 时建）")
        return 0
    if a.cmd == "mark" and seen is None:
        print("还没有已见列表，不用 mark：首次 wait 会把现有 Issue 全记为已见，这几个号也在内")
        return 0
    if seen is None:
        print("还没有已见列表，先跑一次 wait", file=sys.stderr)
        return 1
    save_seen(path, seen | set(a.nums) if a.cmd == "mark" else seen - set(a.nums))
    return 0


if __name__ == "__main__":
    sys.exit(main())
