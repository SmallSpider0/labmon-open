#!/usr/bin/env python3
"""deploy：把 main 上这期间的提交部署到看板服务并核对（AGENTS.md「部署到看板」第 1–5、7 步，Issue #75、#164）。

    python3.11 -I tools/deploy.py [--wait 分钟] [--to https://常驻机:端口/ [--ca CA 证书]]

只在主 checkout（仓库目录本身，不在 worktree）的 main 上跑，由部署 agent 跑（急修时是当前会话）。两种模式：
- 本机模式（看板服务就在这台，搬家前）：pull 主 checkout 本身，launchctl / systemctl 重启，读本机日志。
- 远端模式（--to，或 ~/.config/labmon/deploy.toml 里写了 url，可选 ca；#164）：看板服务在常驻机上。确认本地 main 与 origin/main 一致，
  按服务端 checkout 现在的提交打增量 git bundle，经 POST /api/admin/deploy 传过去（服务端快进、要重启的自己等平台操作做完再重启），
  等 GET /api/me 报出新提交；拉起 worker 经 POST /api/worker/start；平台操作与日志 Traceback 读 GET /api/admin/deploy。
环境里有测试用的 LABMON_STATE、LABMON_HOSTS、LABMON_LAN 时不动手（TEST_ENV）：平台操作在不在做按状态目录判断，不能被带偏。
看板要登录（#160）：带部署令牌（deploy 作用域，管理员的；环境变量 LABMON_TOKEN 优先，否则 TOKEN_FILE），没有令牌、看板不认它时不动手。
1. 平台操作在做就每 15 秒再看，最多等 --wait 分钟（缺省 30），等不到就停、不更新；
2. 更新代码（本机 git pull --ff-only；远端上传 bundle），列出带上去的提交；
3. 改的文件不全在文档、测试、工具里（labmon.deploy.is_doc）就重启服务（本机：重启前再等一次平台操作做完，再 kickstart；远端由服务端做），
   等服务答得上、报出新提交；
4. POST /collect?fresh 两次（fresh：不受节流，等请求之后开始的那一轮做完）；
5. 改了 worker（labmon_worker.py 或 labmon/worker/）的，各台没 stop 过、连得上的主机拉起 worker，再采集；
   第 4、5 步的最后一次采集后，快照不晚于采集请求、拉起成了的主机状态不晚于拉起的（新 worker 还没写出第一份状态），
   每 SETTLE_POLL_S 秒再采一次，最多等 SETTLE_S 秒（#70），等不到照样往下核对、由核对报没过；
7. 核对：没有进行中的平台操作；服务日志重启以来（没重启就是最后一次「服务启动」以来）没有 Traceback；
   各主机 worker 在写状态（worker_up、age 不超过 AGE_OK 秒，拉起过的状态晚于拉起时刻）；
   看板上的工作台与项目页下载下来，在 node 里用 tests/pages 的桩按路由渲染，不报错、不漏 NaN/undefined（tests/pages/smoke.js）。
第 6 步（核对 HTML 文档）与改了页面时在 Claude 桌面应用的内置浏览器里核对改动、关掉本任务开的标签仍由人做，结论里点名。
结论打到标准输出，不超过 15 行；过程打到标准错误。退出码：0 全部通过；1 部署做了、有核对没过；2 没动手就停了。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path

PYTHON = "python3.11"      # 部署用的解释器（与看板服务同一个，要 3.11）


def need_python(version=None) -> None:
    """解释器不对（python3 往往是 3.8）就打一行中文提示、退出码 2，不往下导入。"""
    v = tuple(version or sys.version_info[:2])
    if v < (3, 11):
        print(f"解释器不对（当前 {v[0]}.{v[1]}，要 3.11）：用 {PYTHON} -I tools/deploy.py 跑", file=sys.stderr)
        raise SystemExit(2)


need_python()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tomllib  # noqa: E402
from labmon import client, config, service, state  # noqa: E402
from labmon import deploy as D  # noqa: E402

ARTIFACTS = {"docs/workflow.html": "流程总览", "docs/intro.html": "项目介绍页"}      # 改了要核对 HTML 文档（AGENTS.md 的这两节有链接）
POLL_S = 15            # 等平台操作做完时多久看一次
UP_S = 60              # 重启后最多等多久服务答得上（远端另加服务端的 2 秒延迟）
AGE_OK = 90            # worker 状态多旧还算在写（worker 每 10 秒写一次）
SETTLE_S = 120         # 采集、拉起之后最多等这么久，等出晚于采集请求的快照、晚于拉起的 worker 状态（#70）
SETTLE_POLL_S = 10     # 其间多久再采一次（worker 每 10 秒写一次状态）
MAX_LINES = 15         # 结论最多几行（带上去的提交多了只列几个）
SMOKE = ROOT / "tests" / "pages" / "smoke.js"
TEST_ENV = ("LABMON_STATE", "LABMON_HOSTS", "LABMON_LAN")   # 测试用的变量：带着它们跑，平台操作会从临时目录判断、拉 worker 按假清单，一律拒绝
TOKEN_FILE = Path("~/.config/labmon/deploy.token").expanduser()     # 部署令牌（#160）：labmon user token <名字> --scope deploy --out 这里
REMOTE_FILE = Path("~/.config/labmon/deploy.toml").expanduser()     # 远端模式（#164）：url = "https://常驻机:端口/"，可选 ca = "CA 证书路径"
NEED_TOKEN = ("没有部署令牌：在看板服务那台机器上 labmon user token <你的名字> --scope deploy --name 部署 --out deploy.token，放到本机 ~/.config/labmon/deploy.token"
              "（还没有看板账号先 labmon user add <名字> --admin，再到登录页用设置码设口令）")
TARGET: dict = {"url": None, "ca": None}            # 远端模式的看板地址与 CA（main 按参数与 REMOTE_FILE 填；url 为 None 是本机模式）

_now = time.time       # 测试打桩
SKEW = [0.0]           # 远端模式：服务端时钟 − 本机时钟（_skew 按回话的 Date 头记）
SKEW_PAD = 1.0
SKEW_RTT = 1.0         # 只用这么快回来的请求校准时钟差（立即采集、上传要等服务端做完，Date 头是做完时的）
PENDING_FILE = state.state_root() / "deploy_pending.json"     # 改了 worker、没拉起成的主机，下次部署补上
_sleep = time.sleep
LOG = service.LOG


class Stop(Exception):
    """没动手就停（退出码 2）。"""


def say(msg: str) -> None:
    print("… " + msg, file=sys.stderr, flush=True)


def remote() -> bool:
    return bool(TARGET["url"])


def remote_conf(path: Path = REMOTE_FILE) -> dict:
    """~/.config/labmon/deploy.toml：{url, ca}；没有这个文件是 {}（本机模式）。"""
    try:
        with open(path, "rb") as f:
            d = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Stop(f"{path} 读不了：{e}") from None
    return {"url": d.get("url"), "ca": d.get("ca")}


# ---------- 外部动作（测试打桩） ----------
def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=300)
    if r.returncode:
        last = (r.stderr or r.stdout).strip().splitlines()
        raise Stop(f"git {' '.join(args)} 失败：{last[-1] if last else r.returncode}")
    return r.stdout.strip()


def parse_worktree_branches(porcelain_output: str) -> set[str]:
    """从 git worktree list --porcelain 输出中提取所有 worktree 正在检出的分支名。"""
    active = set()
    for line in porcelain_output.splitlines():
        line = line.strip()
        if line.startswith("branch refs/heads/"):
            active.add(line[len("branch refs/heads/"):])
    return active


def parse_merged_branches(merged_output: str) -> set[str]:
    """从 git branch --merged <base> 输出中提取分支名。"""
    branches = set()
    for line in merged_output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line[0] in ("*", "+"):
            line = line[1:].strip()
        if line:
            branches.add(line)
    return branches


def parse_gone_branches(branch_vv_output: str) -> set[str]:
    """从 git branch -vv 输出中提取远端跟踪分支已失效（: gone] 或 : gone, ...）的分支名。"""
    branches = set()
    for line in branch_vv_output.splitlines():
        line = line.strip()
        if not line or ": gone" not in line:
            continue
        # 匹配 [remote/branch: gone] 或 [remote/branch: gone, ahead 1]
        if not re.search(r"\[[^\]]*:\s*gone(?:,\s*[^\]]+)?\]", line):
            continue
        cleaned = line.lstrip("*+ ")
        parts = cleaned.split()
        if parts:
            branches.add(parts[0])
    return branches


def filter_cleanup_branches(
    gone_branches: set[str],
    merged_branches: set[str],
    active_branches: set[str],
    head_branch: str | None = None,
    keep_branches: set[str] | None = None,
) -> list[str]:
    """计算可安全删除的本地分支列表：排除 main/master、当前 HEAD、所有活跃 worktree 正在使用的分支。"""
    keep = {"main", "master"}
    if head_branch:
        keep.add(head_branch)
    if keep_branches:
        keep.update(keep_branches)
    keep.update(active_branches)
    candidates = (gone_branches | merged_branches) - keep
    return sorted(candidates)


def auto_clean_branches() -> int:
    """部署后顺手清理已合并/已失效本地分支与过期 worktree 引用；异常只打一行提示、绝不阻断部署。"""
    try:
        git("fetch", "--prune", "origin")
        git("worktree", "prune")
        porcelain = git("worktree", "list", "--porcelain")
        active = parse_worktree_branches(porcelain)
        head_br = git("rev-parse", "--abbrev-ref", "HEAD")
        merged_out = git("branch", "--merged", f"origin/{D.BRANCH}")
        merged = parse_merged_branches(merged_out)
        branch_vv = git("branch", "-vv")
        gone = parse_gone_branches(branch_vv)
        to_del = filter_cleanup_branches(gone, merged, active, head_branch=head_br)
        deleted = 0
        for br in to_del:
            try:
                git("branch", "-D", br)
                deleted += 1
            except Exception as e:
                say(f"清理分支 {br} 跳过：{e}")
        if deleted:
            say(f"自动清理了 {deleted} 个已合并/已失效本地分支")
        return deleted
    except Exception as e:
        say(f"自动清理分支跳过（不影响部署）：{e}")
        return 0


def token() -> str | None:
    """部署令牌：环境变量 LABMON_TOKEN 优先，否则 TOKEN_FILE；都没有返回 None。原文不打印。"""
    t = os.environ.get("LABMON_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    return t or None


def http(method: str, path: str, timeout: float = 30, body: dict | None = None) -> tuple[int, bytes]:
    """看板服务的一个请求（本机模式 127.0.0.1:PORT，远端模式 TARGET 的 url）：(状态码, 内容)；连不上是 (0, b"")。
    带部署令牌（Authorization: Bearer，#160）；POST 带 JSON 类型与 JSON 请求体（缺省 {}；看板只认 application/json 的 POST，#158）。"""
    post = method == "POST"
    hd = {"Content-Type": "application/json"} if post else {}
    if (tok := token()):
        hd["Authorization"] = f"Bearer {tok}"
    if remote():
        base, ctx = TARGET["url"].rstrip("/"), (config.pinned(str(Path(TARGET["ca"]).expanduser())) if TARGET["ca"] else None)
    else:
        try:
            base, ctx = config.lan_url(config.PORT)          # 内网入口配了证书时走 HTTPS、只信那张证书（主机清单的 [server]，#165）
        except config.ConfigError:
            return 0, b""
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body or {}).encode() if post else None, headers=hd)
    t0 = _now()
    try:
        with client.opener(ctx).open(req, timeout=timeout) as r:          # 直连、不走环境代理（#195）
            _skew(r.headers.get("Date"), t0)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        _skew(e.headers.get("Date"), t0)
        return e.code, e.read()
    except (OSError, ValueError):
        return 0, b""


def _skew(date: str | None, t0: float) -> None:
    """远端模式：按回话的 Date 头记下服务端时钟比本机快多少（Date 只到秒，取那一秒的中点、本机取请求前后的中点）。"""
    if not remote() or not date or _now() - t0 > SKEW_RTT:
        return
    try:
        SKEW[0] = parsedate_to_datetime(date).timestamp() + 0.5 - (t0 + _now()) / 2
    except (TypeError, ValueError):
        pass


def clock() -> float:
    """与看板快照、worker 状态比先后用的“现在”：本机模式就是本机时钟；远端模式换到服务端的时钟，再往前让 SKEW_PAD 秒（Date 只到秒）。"""
    return _now() + SKEW[0] - (SKEW_PAD if remote() else 0)


def server_status() -> dict | None:
    """远端：GET /api/admin/deploy（在跑的提交 commit、checkout 的 head、started、busy、tracebacks）；读不到是 None。"""
    code, body = http("GET", "/api/admin/deploy")
    try:
        return json.loads(body) if code == 200 else None
    except ValueError:
        return None


def busy() -> dict:
    """正在做平台操作的服务器：本机 fleetops.busy（看板没起来也能判断），远端读服务端的。"""
    if remote():
        st = server_status()
        if st is None:
            raise Stop(f"读不到 {TARGET['url']} 的部署状态（GET /api/admin/deploy）")
        return st.get("busy") or {}
    from labmon import fleetops
    return fleetops.busy()


def kickstart() -> None:
    """本机模式重启看板服务（launchd 或 systemd，service.MANAGER）。"""
    if service.MANAGER == "launchd":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{service.LABEL}"], capture_output=True, timeout=60)
    else:
        subprocess.run(["systemctl", "--user", "restart", service.UNIT], capture_output=True, timeout=60)


def worker_start(hid: str) -> tuple[int, str]:
    """拉起一台的 worker：POST /api/worker/start（本机与远端模式同一条，#162；服务端推它 checkout 里 main 上已提交的版本）。"""
    code, body = http("POST", "/api/worker/start", timeout=600, body={"host": hid})
    try:
        d = json.loads(body)
    except ValueError:
        d = {}
    return (d.get("rc", -1), (d.get("out") or "") + (d.get("err") or "")) if code == 200 else (-1, d.get("error") or f"HTTP {code or '连不上'}")


def make_bundle(base: str | None) -> bytes:
    """main 打成 git bundle：base 是服务端 checkout 现在的提交，本地有它就只打 base..main，没有打整份。"""
    rng = f"{base}..{D.BRANCH}" if base and subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{base}^{{commit}}"],
                                                           capture_output=True).returncode == 0 else D.BRANCH
    with tempfile.TemporaryDirectory(prefix="labmon-bundle-") as td:
        f = Path(td) / "main.bundle"
        git("bundle", "create", str(f), rng)
        return f.read_bytes()


def smoke(site: Path) -> list | None:
    """页面冒烟检查的结果；没有 node 返回 None。"""
    node = shutil.which("node")
    if not node:
        return None
    r = subprocess.run([node, str(SMOKE), str(site)], capture_output=True, text=True, timeout=300)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return [{"w": "smoke.js", "ok": False, "d": (r.stderr.strip().splitlines() or ["没有输出"])[-1][:200]}]


# ---------- 判断 ----------
def wait_idle(wait_min: float) -> str:
    """平台操作在做就每 POLL_S 秒再看，最多等 wait_min 分钟；做完了返回空串，等不到返回在做的是什么。"""
    t0, said = _now(), False
    while (b := busy()):
        what = "、".join(f"{k} {v}" for k, v in b.items())
        if _now() - t0 >= wait_min * 60:
            return what
        if not said:
            say(f"等平台操作做完：{what}")
            said = True
        _sleep(POLL_S)
    return ""


def state_after(snap: dict, h: dict, t: float) -> bool:
    """这台 worker 的状态时刻（按 generated − age 换到看板时钟）不早于 t。"""
    return snap.get("generated", 0) - h["age"] >= t


def fresh(snap: dict | None, mark: float | None, started: dict) -> bool:
    """快照不早于 mark（采集请求前的时刻）、started 里的各台在写状态且状态不早于拉起：核对读得到这次部署之后的样子。"""
    if snap is None or mark is not None and snap.get("generated", 0) < mark:
        return False
    hs = {h["id"]: h for h in snap.get("hosts") or []}
    return all((h := hs.get(hid)) and h.get("worker_up") and h.get("age") is not None and h["age"] <= AGE_OK and state_after(snap, h, t)
               for hid, t in started.items())


def settle(mark: float | None, started: dict) -> dict | None:
    """带 fresh 采集，直到 fresh(...) 成立；不成立每 SETTLE_POLL_S 秒再采，最多 SETTLE_S 秒，返回最后一份快照（没采到是 None）。
    拉起 worker 只等到新进程起来，它写出第一份状态要再过几秒；采集超时（504）时看板上还是旧快照（#70）。"""
    t0, said = _now(), False
    while True:
        snap = collect()
        if fresh(snap, mark, started) or _now() - t0 >= SETTLE_S:
            return snap
        if not said:
            say("等新的快照与 worker 状态" + (f"（{'、'.join(started)}）" if started else ""))
            said = True
        _sleep(SETTLE_POLL_S)


def host_check(snap: dict, started: dict) -> tuple[bool, str]:
    """各主机 worker 在不在写状态：一行文字。stop 过的不查；拉起过的状态时刻（按 generated − age 换到本机时钟）要晚于拉起时刻。"""
    good, parts = True, []
    for h in snap.get("hosts") or []:
        hid = h["id"]
        if h.get("stopped"):
            parts.append(f"{hid} stop 过、不查")
            continue
        if not h.get("worker_up") or h.get("age") is None or h["age"] > AGE_OK:
            good = False
            why = h.get("error") or h.get("status_error") or ("没有状态" if not h.get("status") else f"状态停在 {config.fmt_dur(h.get('age') or 0)} 前")
            parts.append(f"{hid} 不在（{why}）")
            continue
        ok = hid not in started or state_after(snap, h, started[hid])
        good &= ok
        parts.append(f"{hid} 在写状态（{round(h['age'])} 秒前" + ("" if hid not in started else "，晚于拉起" if ok else "，早于拉起：新 worker 没写状态") + "）")
    return good, "；".join(parts) or "没有主机"


def page_check(snap: dict) -> tuple[bool | None, str]:
    """把看板上的工作台与各项目页下载到临时目录（布局同看板服务），交给 smoke.js 渲染。"""
    with tempfile.TemporaryDirectory(prefix="labmon-deploy-") as tmp:
        site = Path(tmp)
        want = {"index.html": "/", "overview.json": "/overview.json", "records.json": "/records.json?cats"}
        for p in snap.get("projects") or []:
            for f, u in (("index.html", ""), ("snapshot.json", "snapshot.json"), ("events.json", "events.json")):
                want[f"p/{p['id']}/{f}"] = f"/p/{p['id']}/{u}"
        for f, u in want.items():
            code, body = http("GET", u)
            if code != 200:
                return False, f"取不到 {u}（{code or '连不上'}）"
            (site / f).parent.mkdir(parents=True, exist_ok=True)
            (site / f).write_bytes(body)
        res = smoke(site)
    if res is None:
        return None, "没有 node，跳过"
    bad = [x for x in res if not x.get("ok")]
    if bad:
        return False, f"{len(bad)}/{len(res)} 处不过：" + "；".join(f"{x['w']}：{x.get('d')}" for x in bad[:3])
    return True, f"{len(res)} 处（工作台各路由、项目页）渲染不报错、不漏 NaN/undefined"


def collect() -> dict | None:
    try:
        code, body = http("POST", "/collect?fresh", timeout=600)
    except IncompleteRead:
        say("采集响应未收全，再试一次")
        try:
            code, body = http("POST", "/collect?fresh", timeout=600)
        except IncompleteRead:
            say("采集响应再次未收全")
            return None
    try:
        return json.loads(body) if code == 200 else None
    except ValueError:
        return None


def wait_up(off: int) -> bool:
    """本机：重启后日志里出现新的「服务启动」、/api/me 答得上。"""
    t0 = _now()
    while _now() - t0 < UP_S:
        if "服务启动：" in D.log_text(off, LOG) and http("GET", "/api/me", timeout=5)[0] == 200:
            return True
        _sleep(2)
    return False


def wait_new(new: str, started: float | None, wait_min: float) -> bool:
    """远端：等 /api/me 报出新提交、新的启动时刻（服务端先等平台操作做完才重启：它在做时最多再等 wait_min 分钟）。"""
    t0, extra = _now(), 0.0
    while _now() - t0 < UP_S + extra:
        code, body = http("GET", "/api/me", timeout=5)
        try:
            me = json.loads(body) if code == 200 else {}
        except ValueError:
            me = {}
        if me.get("commit") == new and me.get("started") != started:
            return True
        if code == 200 and me.get("started") == started:                 # 旧进程还在：多半在等平台操作做完
            st = server_status() or {}
            if st.get("busy"):
                extra = min(wait_min * 60, _now() - t0 + UP_S)
        _sleep(2)
    return False


# ---------- 主流程 ----------
def _update_local(wait_min: float, checks: list) -> dict:
    """本机：pull，要重启的重启前再等一次平台操作做完、kickstart、等服务起来。"""
    old = git("rev-parse", "--short", "HEAD")
    say("git pull --ff-only")
    git("pull", "--ff-only")
    auto_clean_branches()
    new = git("rev-parse", "--short", "HEAD")
    commits = git("log", "--oneline", "--no-decorate", f"{old}..{new}").splitlines() if old != new else []
    files = git("diff", "--name-only", old, new).splitlines() if commits else []
    u = {"old": old, "new": new, "commits": commits, "files": files, "restart": any(not D.is_doc(f) for f in files), "held": "", "off": None, "up": True}
    if u["restart"] and (held := wait_idle(wait_min)):      # pull 那几秒里可能有人发起了平台操作：kickstart -k 会把它杀在半路
        checks.append((False, f"平台操作还在做（{held}），等了 {wait_min:g} 分钟，pull 了但没重启：做完后 labmon service restart，再照手工第 4、5 步做"))
        u.update(restart=False, held=held)
    if u["restart"]:
        u["off"] = LOG.stat().st_size if LOG.exists() else 0
        say("重启看板服务")
        kickstart()
        u["up"] = wait_up(u["off"])
        if not u["up"]:
            checks.append((False, f"看板服务重启后 {UP_S} 秒没起来（看 {LOG}）"))
    return u


def pending() -> list[str]:
    """上次部署改了 worker、却没拉起成的主机（同一个看板；"*" 是全部）。"""
    d = state.load_json(PENDING_FILE, {})
    return d.get("hosts") or [] if d.get("target") == (TARGET["url"] or "local") else []


def set_pending(hosts: list[str]) -> None:
    if hosts:
        state.save_json(PENDING_FILE, {"target": TARGET["url"] or "local", "hosts": sorted(set(hosts))})
    else:
        PENDING_FILE.unlink(missing_ok=True)


def code_behind(run: str, new: str) -> bool:
    """服务端在跑的代码（run）比 new 少了要重启才生效的改动（上次快进后没重启成）；只差文档、测试、工具的不算。本地没有 run 时按落后算。"""
    if not run or run == new:
        return False
    try:
        return any(not D.is_doc(f) for f in git("diff", "--name-only", run, new).splitlines())
    except Stop:
        return True


def _update_remote(wait_min: float, checks: list) -> dict:
    """远端：本地 main 与 origin/main 一致 → 按服务端 checkout 的提交打 bundle 上传 → 要重启的等服务端报出新提交。"""
    say("git pull --ff-only")
    git("pull", "--ff-only")
    auto_clean_branches()
    new = git("rev-parse", D.BRANCH)
    if new != git("rev-parse", f"origin/{D.BRANCH}"):
        raise Stop(f"本地 {D.BRANCH} 与 origin/{D.BRANCH} 不一致（有没推的提交？）：只部署推到 GitHub 的 main")
    st = server_status()
    if st is None:
        raise Stop(f"读不到 {TARGET['url']} 的部署状态（GET /api/admin/deploy）")
    old, run = st.get("head") or "", st.get("commit") or ""
    u = {"old": old[:7], "new": new[:7], "commits": [], "files": [], "restart": False, "held": "", "off": None, "up": True}
    if old == new and not code_behind(run, new):
        return u
    base = old if old != new else run       # checkout 已是新的、在跑的还是旧的（上次重启没成）：照样传，服务端按在跑的代码补上重启
    say(f"打包上传 {base[:7] or '（整份）'}..{new[:7]}")
    code, body = http("POST", "/api/admin/deploy", timeout=300, body={"bundle": base64.b64encode(make_bundle(base or None)).decode()})
    try:
        r = json.loads(body)
    except ValueError:
        r = {}
    if code != 200:
        raise Stop(f"服务端没收这次部署（{code or '连不上'}）：{r.get('error') or '—'}")
    u.update(old=(r.get("old") or u["old"])[:7], commits=r.get("commits") or [], files=r.get("files") or [], restart=bool(r.get("restart")))
    if u["restart"]:
        say("等服务端重启")
        u["up"] = wait_new(new, st.get("started"), wait_min)
        if not u["up"]:
            checks.append((False, f"服务端 {UP_S} 秒没报出新提交 {new[:7]}（平台操作在做时它会等做完再重启；看那台的服务日志）"))
    return u


def deploy(wait_min: float) -> tuple[int, list[str]]:
    out: list[str] = []
    checks: list[tuple[bool | None, str]] = []
    # 0. 前提
    env = [k for k in TEST_ENV if k in os.environ]
    if env:
        raise Stop(f"环境里有测试用的 {'、'.join(env)}：平台操作在不在做会从它指的目录判断，unset 后再跑")
    gd, cd = (Path(ROOT, x).resolve() for x in git("rev-parse", "--git-dir", "--git-common-dir").splitlines())
    if gd != cd:
        raise Stop("这是 worktree：部署只在主 checkout（仓库目录本身）里做")
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise Stop("主 checkout 不在 main 上")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise Stop("主 checkout 有没提交的改动（它只放 main）")
    if not token():
        raise Stop(NEED_TOKEN)
    code = http("GET", "/api/me", timeout=10)[0]
    if code in (401, 403):        # 看板起着、却不认这个令牌（吊销了、写错了）：核对必然不过，先别动
        raise Stop(f"看板不认部署令牌（{code}）：在「我的账号」里看它还在不在，或重建（{NEED_TOKEN.split('：', 1)[1]}）")
    if remote() and code != 200:
        raise Stop(f"连不上 {TARGET['url']}（{code or '连不上'}）：看内网、地址与 CA 证书")
    # 1. 平台操作
    if (what := wait_idle(wait_min)):
        raise Stop(f"平台操作还在做（{what}），等了 {wait_min:g} 分钟，没更新代码")
    # 2–3. 更新代码、重启
    u = (_update_remote if remote() else _update_local)(wait_min, checks)
    files, restart = u["files"], u["restart"]
    wk_changed = D.has_worker(files)
    wk = wk_changed and restart
    pend = set(pending()) - ({"*"} if wk else set())          # 上次改了 worker、却没拉起成的主机：这次补上（看板与提交都对上了也照样补）
    if wk_changed:                            # 先记下全部要补：没重启（平台操作在做）、采集没完成、拉起断在哪一步，下次跑都补；拉起完换成没成的那几台
        set_pending(["*"])
    started: dict[str, float] = {}
    mark = clock() if restart or pend else None
    # 4–5. 采集、拉起 worker
    if (restart and u["up"]) or pend:
        say("采集两次")
        collect()
        snap = settle(mark, {})
        if snap is None:
            checks.append((False, "采集没完成（POST /collect 没回快照）"))
        if (wk or pend) and snap:
            bad, left = [], []
            for h in snap.get("hosts") or []:
                hid = h["id"]
                if h.get("stopped") or not (h.get("auto") or h.get("status")) or not (wk or hid in pend or "*" in pend):
                    continue
                if h.get("lan_off") or h.get("error"):
                    bad.append(f"{hid} 连不上、worker 没拉起（{h.get('error') or '不在内网'}）")
                    left.append(hid)
                    continue
                say(f"拉起 {hid} 的 worker")
                started[hid] = clock()
                rc, text = worker_start(hid)
                if rc:
                    bad.append(f"{hid} 拉起失败（退出码 {rc}：{(text.strip().splitlines() or [''])[-1][:100]}）")
                    left.append(hid)
            if bad:
                checks.append((False, "；".join(bad) + "（下次跑部署脚本补上）"))
            set_pending(left)
            mark = clock()
            settle(mark, {hid: t for hid, t in started.items() if hid not in left})
    n_doc = sum(map(D.is_doc, files))
    out.append("重启看板：" + ("没重启（平台操作在做）" if u["held"] else "是" if restart else "否") + (f"（改了 {len(files) - n_doc} 个代码文件）" if restart or u["held"] else f"（只改了文档、测试或工具，{len(files)} 个文件）" if files else "（没有新提交）")
               + "｜worker：" + ("改了，拉起 " + ("、".join(started) or "没有主机") if wk else "补拉起上次没成的 " + ("、".join(started) or "（都连不上）") if pend
                          else "改了，没拉起" if wk_changed else "没改，不拉起"))
    # 7. 核对
    st = server_status() if remote() else None
    b = (st or {}).get("busy") if remote() else busy()
    checks.insert(0, (not b, "没有进行中的平台操作" if not b else f"平台操作在做：{b}"))
    if remote() and st is None:
        checks.append((False, "读不到服务端的部署状态，日志没核对"))
    else:
        tb = st.get("tracebacks") or [] if remote() else D.tracebacks(D.log_text(u["off"], LOG))
        checks.append((not tb, ("服务日志重启以来" if restart else "服务日志最后一次启动以来") + ("没有 Traceback" if not tb else f"有 {len(tb)} 个 Traceback：" + "；".join(tb[:2]))))
    code, body = http("GET", "/overview.json")
    try:
        snap = json.loads(body) if code == 200 else None
    except ValueError:
        snap = None
    if snap is None:
        checks.append((False, f"读不到 /overview.json（{code or '连不上'}）"))
    else:
        if mark is not None and snap.get("generated", 0) < mark:
            checks.append((False, f"快照时刻 {config.fmt_time(snap.get('generated'), sec=True)} 早于采集，不是新的"))
        good, text = host_check(snap, started)
        checks.append((good, "worker：" + text))
        checks.append(page_check(snap))
    out += [("✓ " if ok else "– " if ok is None else "✗ ") + t for ok, t in checks]
    todo = [f"核对 HTML 文档 {f}（说明见 AGENTS.md「{s}」）" for f, s in ARTIFACTS.items() if f in files]
    pages = [f for f in files if D.is_page(f)]
    if pages:
        todo.append(f"在内置浏览器里核对改动处（{'、'.join(pages)}；电脑没信任 labmon CA 打不开就写明没看），用完关掉本任务开的标签并记录清理结果")
    if todo:
        out.append("要你手做：" + "；".join(todo))
    fail = [t for ok, t in checks if ok is False]
    out.append("结论：核对全部通过。" if not fail else f"结论：{len(fail)} 项核对没过，不报完成：查原因或回退。")
    return (1 if fail else 0), head(u["old"], u["new"], u["commits"], MAX_LINES - len(out)) + out


def head(old: str, new: str, commits: list[str], room: int) -> list[str]:
    """结论开头的带上去的提交：总共不超过 room 行（至少 1 行），剩几行列几个，多的写“另 N 个”（这行也算在 room 里；
    只剩一两行时并进标题行）。"""
    if not commits:
        return [f"main 没有新提交（{new}），只核对"]
    title = f"部署 {old} → {new}（{len(commits)} 个提交，出了问题可按 PR 单独 git revert）"
    if len(commits) <= room - 1:
        return [title + "："] + ["  " + c[:80] for c in commits]
    n = room - 2 if room >= 3 else max(0, room - 1)
    rest = f"另 {len(commits) - n} 个（git log --oneline {old}..{new}）"
    lines = ["  " + c[:80] for c in commits[:n]]
    return [title + "：", *lines, "  …" + rest] if room >= 3 else [f"{title}，{rest}" + ("：" if n else ""), *lines]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=f"{PYTHON} -I tools/deploy.py", description=__doc__.splitlines()[0],
                                 epilog=f"必须用 {PYTHON} 跑（3.11）；python3 往往是 3.8，会直接退出（退出码 2）。")
    ap.add_argument("--wait", type=float, default=30, help="平台操作在做时最多等几分钟（缺省 30）")
    ap.add_argument("--to", help=f"远端模式：常驻机看板的内网入口地址（缺省取 {REMOTE_FILE} 的 url；都没有就是本机模式）")
    ap.add_argument("--ca", help="远端模式：内网入口证书的 CA（缺省取 deploy.toml 的 ca）")
    a = ap.parse_args(argv)
    try:
        conf = {} if a.to else remote_conf()
        TARGET.update(url=a.to or conf.get("url"), ca=a.ca or conf.get("ca"))
        rc, out = deploy(a.wait)
    except Stop as e:
        rc, out = 2, [f"没部署：{e}"]
    print("\n".join(out), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
