"""部署脚本（tools/deploy.py，Issue #75、#164）与服务端收部署（labmon/deploy.py）：git、launchctl、HTTP、子进程与时钟都打桩，不碰 8765、launchctl、ssh；
远端模式起一个端口 0 的真看板服务，服务端 checkout 与本地主 checkout 都是临时 git 仓库。

    python -m unittest tests.test_deploy -v
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("deploy", ROOT / "tools" / "deploy.py")
DP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DP)
sys.path.insert(0, str(ROOT / "tests"))
from fixtures import no_git_env  # noqa: E402


def setUpModule():
    """临时仓库上的 git（sh_git 与被测的部署代码）不认继承来的 GIT_DIR 之类：整个模块都去掉。"""
    unittest.addModuleCleanup(no_git_env().stop)


def real_env():
    """部署时的环境：没有把状态目录、主机清单、内网状态指到别处的测试变量（整套测试带着 LABMON_STATE 跑）。"""
    env = {k: v for k, v in os.environ.items() if k not in DP.TEST_ENV}
    return mock.patch.dict(os.environ, env, clear=True)


class PythonTest(unittest.TestCase):
    def test_wrong_interpreter(self):
        import io
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err), self.assertRaises(SystemExit) as c:
            DP.need_python((3, 8))
        self.assertEqual(c.exception.code, 2)
        self.assertIn("python3.11", err.getvalue())
        DP.need_python((3, 11))

    def test_help_names_interpreter(self):
        import io
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), self.assertRaises(SystemExit):
            DP.main(["--help"])
        self.assertIn("python3.11", out.getvalue())


class Rig:
    """一次部署的假环境：git 的回答、看板服务、日志文件、时钟。calls 记下做过的外部动作。"""

    def __init__(self, tmp: Path, files=(), commits=None, hosts=None, busy=None, up=True):
        self.t = 1_000_000.0
        self.head, self.pulled = "aaa1111", False
        self.files = list(files)
        self.commits = commits if commits is not None else ([f"bbb222{i} 提交 {i}" for i in range(2)] if files else [])
        self.hosts = hosts if hosts is not None else [{"id": "a", "auto": True, "status": {"t": 1}, "worker_up": True, "age": 5}]
        self.busy = list(busy or [])
        self.up = up
        self.calls: list = []
        self.gitdir = (".git", ".git")
        self.branch, self.dirty = "main", ""
        self.log = tmp / "labmon.log"
        self.pending = tmp / "deploy_pending.json"
        self.log.write_text("[labmon] 11-09 10:00:00 服务启动：http://127.0.0.1:8765/ ；项目 a\n")
        self.age_after_start = 3        # 拉起之后的采集里，worker 状态的 age
        self.tok, self.me = "lm_部署令牌", 200   # 部署令牌（#160）与看板认不认它（/api/me 的状态码）

    # ---- 假的外部动作 ----
    def git(self, *a):
        if a[:1] == ("pull",):
            self.calls.append("pull")
            self.pulled = True
            return ""
        if a == ("rev-parse", "--git-dir", "--git-common-dir"):
            return "\n".join(self.gitdir)
        if a == ("rev-parse", "--abbrev-ref", "HEAD"):
            return self.branch
        if a[:1] == ("status",):
            return self.dirty
        if a == ("rev-parse", "--short", "HEAD"):
            return "ccc3333" if self.pulled and self.commits else self.head
        if a[:1] == ("log",):
            return "\n".join(self.commits)
        if a[:1] == ("diff",):
            return "\n".join(self.files)
        raise AssertionError(f"没想到的 git {a}")

    def snap(self):
        hs = []
        for h in self.hosts:
            h = dict(h)
            if h["id"] in self.started:
                h["age"] = self.age_after_start
            hs.append(h)
        return {"generated": self.t, "hosts": hs, "projects": []}

    def http(self, method, path, timeout=30, body=None):
        self.calls.append(f"{method} {path}")
        if path == "/api/me":
            return (self.me, b"{}") if self.up else (0, b"")
        if method == "POST" and path == "/collect?fresh":
            self.t += 5
            return 200, json.dumps(self.snap()).encode()
        if path == "/overview.json":
            return 200, json.dumps(self.snap()).encode()
        raise AssertionError(f"没想到的请求 {method} {path}")

    def kickstart(self):
        self.calls.append("kickstart")
        if self.up:
            with self.log.open("a") as f:
                f.write("[labmon] 11-09 10:05:00 服务启动：http://127.0.0.1:8765/ ；项目 a\n")

    def worker_start(self, hid):
        self.calls.append(f"start {hid}")
        self.t += 2
        return 0, "已拉起\n"

    def get_busy(self):
        return self.busy.pop(0) if self.busy else {}

    def sleep(self, s):
        self.t += s

    def run(self, wait=30):
        self.started = {}
        orig = self.worker_start

        def ws(hid):
            self.started[hid] = self.t
            return orig(hid)
        with real_env(), mock.patch.multiple(DP, git=self.git, http=self.http, kickstart=self.kickstart, worker_start=ws, busy=self.get_busy, token=lambda: self.tok,
                                             _now=lambda: self.t, _sleep=self.sleep, LOG=self.log, say=lambda m: None, PENDING_FILE=self.pending,
                                             page_check=lambda snap: (True, "页面过了")):
            try:
                return DP.deploy(wait)
            except DP.Stop as e:
                return 2, [f"没部署：{e}"]


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def rig(self, **kw):
        return Rig(self.dir, **kw)

    def test_docs_tests_tools_only_no_restart(self):
        """只改了文档、测试、工具：不重启、不采集、不拉 worker，照样核对。"""
        r = self.rig(files=["docs/workbench.md", "AGENTS.md", "tests/test_x.py", "tools/deploy.py", ".github/x.yml", "skills/a/SKILL.md", ".claude/settings.json"])
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("kickstart", r.calls)
        self.assertFalse([c for c in r.calls if "collect" in c or c.startswith("start")], r.calls)
        self.assertTrue(any(l.startswith("重启看板：否") for l in out), out)

    def test_code_change_restarts_and_collects_twice_fresh(self):
        """改了看板代码：重启、等服务起来、带 fresh 采集两次；没改 worker 不拉起。"""
        r = self.rig(files=["labmon/serve.py", "README.md"])
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(r.calls.count("kickstart"), 1)
        self.assertEqual(r.calls.count("POST /collect?fresh"), 2)
        self.assertLess(r.calls.index("pull"), r.calls.index("kickstart"))
        self.assertFalse([c for c in r.calls if c.startswith("start")])
        self.assertFalse([l for l in out if l.startswith("✗")], out)

    def test_worker_change_starts_live_hosts_only(self):
        """改了 worker：没 stop 过、连得上、有 worker 的主机拉起，再采集一次；stop 过的、没 worker 的不拉，连不上的记为不过。"""
        hosts = [{"id": "a", "auto": True, "status": {"t": 1}, "worker_up": True, "age": 5},
                 {"id": "b", "auto": True, "stopped": "11-09 09:00", "status": {"t": 1}, "worker_up": False, "age": 900},
                 {"id": "c", "auto": False, "status": None},
                 {"id": "d", "auto": True, "error": "ssh: connect timed out", "status": {"t": 1}, "worker_up": False, "age": 900}]
        r = self.rig(files=["labmon/labmon_worker.py"], hosts=hosts)
        rc, out = r.run()
        self.assertEqual([c for c in r.calls if c.startswith("start")], ["start a"])
        self.assertEqual(r.calls.count("POST /collect?fresh"), 3)
        self.assertEqual(rc, 1, out)
        text = "\n".join(out)
        self.assertIn("d 连不上、worker 没拉起（ssh: connect timed out）", text)
        self.assertIn("a 在写状态（3 秒前，晚于拉起）", text)
        self.assertIn("b stop 过、不查", text)

    def test_worker_status_older_than_start_fails(self):
        """拉起之后 worker 状态的时刻（generated − age）早于拉起时刻：新 worker 没写状态，不过。"""
        r = self.rig(files=["labmon/labmon_worker.py"])
        r.age_after_start = 60           # 状态停在拉起之前
        rc, out = r.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("早于拉起", "\n".join(out))

    def test_stale_worker_fails(self):
        """worker 不在（age 太大）：不过，写出停了多久。"""
        r = self.rig(hosts=[{"id": "a", "auto": True, "status": {"t": 1}, "worker_up": False, "age": 600}])
        rc, out = r.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("a 不在（状态停在 10 分钟 前）", "\n".join(out))

    def test_waits_for_platform_op_then_pulls(self):
        """平台操作在做：等它做完再 pull。"""
        r = self.rig(files=["labmon/serve.py"], busy=[{"srv": "renew"}, {"srv": "renew"}])
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("pull", r.calls)
        self.assertGreaterEqual(r.t, 1_000_000 + 2 * DP.POLL_S)

    def test_platform_op_never_ends_no_pull(self):
        """平台操作一直在做：等满就停，不 pull、不重启，退出码 2。"""
        r = self.rig(files=["labmon/serve.py"], busy=[{"srv": "renew"}] * 1000)
        rc, out = r.run(wait=1)
        self.assertEqual(rc, 2, out)
        self.assertNotIn("pull", r.calls)
        self.assertNotIn("kickstart", r.calls)
        self.assertIn("平台操作还在做（srv renew）", out[0])

    def test_preconditions(self):
        """worktree 里、不在 main、有没提交的改动：什么都不动，退出码 2。"""
        for attr, val, words in (("gitdir", (".git/worktrees/x", ".git"), "worktree"), ("branch", "deploy-75", "不在 main"),
                                 ("dirty", " M labmon/serve.py", "没提交的改动")):
            r = self.rig(files=["labmon/serve.py"])
            setattr(r, attr, val)
            rc, out = r.run()
            self.assertEqual(rc, 2, attr)
            self.assertIn(words, out[0])
            self.assertEqual([c for c in r.calls if c in ("pull", "kickstart")], [], attr)

    def test_service_not_up_after_restart(self):
        """重启后日志里没有新的「服务启动」、服务也答不上：记为不过，不采集。"""
        r = self.rig(files=["labmon/serve.py"], up=False)
        with mock.patch.object(Rig, "http", lambda self, m, p, timeout=30: (self.calls.append(f"{m} {p}"), (0, b""))[1]):
            rc, out = r.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("没起来", "\n".join(out))
        self.assertNotIn("POST /collect?fresh", r.calls)

    def test_no_new_commits_only_checks(self):
        r = self.rig(files=[])
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("main 没有新提交", out[0])
        self.assertNotIn("kickstart", r.calls)

    def test_summary_at_most_15_lines(self):
        """很多提交、几台主机、改了页面与流程图：结论仍不超过 15 行。"""
        hosts = [{"id": h, "auto": True, "status": {"t": 1}, "worker_up": True, "age": 5} for h in "abc"]
        r = self.rig(files=["labmon/labmon_worker.py", "labmon/overview.html", "labmon/common.js", "docs/workflow.html", "docs/intro.html"],
                     commits=[f"ddd{i:04d} 提交 {i}" for i in range(12)], hosts=hosts)
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertLessEqual(len(out), 15, "\n".join(out))
        text = "\n".join(out)
        self.assertIn("另 6 个", text)
        self.assertIn("核对 HTML 文档 docs/workflow.html", text)
        self.assertIn("在内置浏览器里核对改动处（labmon/overview.html、labmon/common.js；", text)

    def test_summary_at_most_15_lines_when_much_fails(self):
        """核对大多没过（拉起失败、连不上、日志有 Traceback、快照不新、页面不过）：提交让出行数，结论仍不超过 15 行、至少列一个提交。"""
        hosts = [{"id": "a", "auto": True, "status": {"t": 1}, "worker_up": True, "age": 5},
                 {"id": "d", "auto": True, "error": "ssh: x", "status": {"t": 1}, "worker_up": False, "age": 900}]
        r = self.rig(files=["labmon/labmon_worker.py", "labmon/overview.html", "docs/workflow.html"], commits=[f"e{i} 提交" for i in range(20)], hosts=hosts)
        r.worker_start = lambda hid: (r.calls.append(f"start {hid}"), (1, "ssh: 失败\n"))[1]
        r.log.write_text(LogTest.TB)
        with mock.patch.object(DP, "page_check", lambda snap: (False, "1/9 处不过")):
            r.started = {}
            with real_env(), mock.patch.multiple(DP, git=r.git, http=r.http, kickstart=r.kickstart, worker_start=r.worker_start, busy=r.get_busy,
                                                 _now=lambda: r.t, _sleep=r.sleep, LOG=r.log, say=lambda m: None, token=lambda: r.tok, PENDING_FILE=r.pending):
                rc, out = DP.deploy(30)
        self.assertEqual(rc, 1, out)
        self.assertLessEqual(len(out), 15, "\n".join(out))
        self.assertIn("e0 提交", "\n".join(out))
        self.assertIn("个（git log --oneline", "\n".join(out))

    def test_waits_for_platform_op_before_restart(self):
        """pull 那几秒里有人发起了平台操作：重启前再问一次，等它做完再 kickstart（不然会被杀在半路）。"""
        r = self.rig(files=["labmon/serve.py"], busy=[{}, {"srv": "renew"}, {"srv": "renew"}])
        seen = []
        orig = r.kickstart
        r.kickstart = lambda: (seen.append(list(r.busy)), orig())
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(seen, [[]], "kickstart 时平台操作还没做完")
        self.assertLess(r.calls.index("pull"), r.calls.index("kickstart"))
        self.assertGreaterEqual(r.t, 1_000_000 + 2 * DP.POLL_S)

    def test_platform_op_before_restart_never_ends(self):
        """重启前平台操作一直在做：等满不重启、不采集，记为不过（已 pull，退出码 1），说清要手动做什么。"""
        r = self.rig(files=["labmon/serve.py"], busy=[{}] + [{"srv": "renew"}] * 1000)
        rc, out = r.run(wait=1)
        self.assertEqual(rc, 1, out)
        self.assertIn("pull", r.calls)
        self.assertNotIn("kickstart", r.calls)
        self.assertNotIn("POST /collect?fresh", r.calls)
        text = "\n".join(out)
        self.assertIn("没重启", text)
        self.assertIn("平台操作还在做（srv renew）", text)

    def test_refuses_test_env(self):
        """带着测试用的 LABMON_STATE（或 LABMON_HOSTS、LABMON_LAN）跑：平台操作会从临时目录判断，什么都不动，退出码 2。"""
        for k in ("LABMON_STATE", "LABMON_HOSTS", "LABMON_LAN"):
            r = self.rig(files=["labmon/serve.py"])
            buf = []
            # main 会读 ~/.config/labmon/deploy.toml 填 TARGET：笔记本上 11-11 起写着它，不打桩就成了远端模式、还漏给后面的用例（#197）
            with mock.patch.dict(os.environ, {k: "/tmp/x"}), mock.patch.dict(DP.TARGET), \
                    mock.patch.multiple(DP, git=r.git, http=r.http, kickstart=r.kickstart, busy=r.get_busy, _now=lambda: r.t,
                                        _sleep=r.sleep, LOG=r.log, say=lambda m: None, remote_conf=lambda: {}), \
                    mock.patch("builtins.print", lambda *a, **kw: buf.append(a[0])):
                rc = DP.main([])
            self.assertEqual(rc, 2, k)
            self.assertIn(k, buf[0])
            self.assertEqual(r.calls, [], k)

    def test_needs_deploy_token(self):
        """看板要登录（#160）：没有部署令牌、看板不认它时不 pull，退出码 2，说清怎么建；看板没起来（0）或还是旧服务（404）时照常部署。"""
        r = self.rig(files=["labmon/serve.py"])
        r.tok = None
        rc, out = r.run()
        self.assertEqual(rc, 2, out)
        self.assertIn("labmon user token <你的名字> --scope deploy", out[0])
        self.assertNotIn("pull", r.calls)
        for code in (401, 403):
            r = self.rig(files=["labmon/serve.py"])
            r.me = code
            rc, out = r.run()
            self.assertEqual(rc, 2, out)
            self.assertIn(f"看板不认部署令牌（{code}）", out[0])
            self.assertNotIn("pull", r.calls)
        r = self.rig(files=["labmon/serve.py"])
        r.me = 404
        self.assertEqual(r.run()[0], 1)                     # 旧服务（不认 /api/me）：照常 pull、重启；重启后等不到 /api/me 记为没起来
        self.assertIn("pull", r.calls)

    def test_token_file_and_env(self):
        """部署令牌：LABMON_TOKEN 优先，否则 ~/.config/labmon/deploy.token；http 带 Authorization: Bearer。"""
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "deploy.token"
            with mock.patch.object(DP, "TOKEN_FILE", f), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LABMON_TOKEN", None)
                self.assertIsNone(DP.token())
                f.write_text("lm_from_file\n")
                self.assertEqual(DP.token(), "lm_from_file")
                os.environ["LABMON_TOKEN"] = "lm_from_env"
                self.assertEqual(DP.token(), "lm_from_env")

    def test_failed_worker_start_retried_next_run(self):
        """改了 worker、有一台拉起失败（旧 worker 还在写状态）：记下来，下次跑脚本（main 没有新提交）补拉起这台，成了才清掉（Codex 审查）。"""
        hosts = [{"id": "a", "auto": True, "status": {"t": 1}, "worker_up": True, "age": 5},
                 {"id": "b", "auto": True, "status": {"t": 1}, "worker_up": True, "age": 5}]
        r = self.rig(files=["labmon/labmon_worker.py"], hosts=hosts)
        orig = r.worker_start
        r.worker_start = lambda hid: (r.calls.append(f"start {hid}"), (3, "传输失败\n"))[1] if hid == "a" else orig(hid)
        rc, out = r.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("下次跑部署脚本补上", "\n".join(out))
        r = self.rig(files=[], hosts=hosts)
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual([c for c in r.calls if c.startswith("start")], ["start a"])
        self.assertIn("补拉起上次没成的 a", "\n".join(out))
        self.assertFalse(r.pending.exists())
        r = self.rig(files=[], hosts=hosts)
        r.run()
        self.assertFalse([c for c in r.calls if c.startswith("start")], "补上了就不再拉")

    def test_collect_failed_worker_update_kept(self):
        """改了 worker、重启了但采集没完成（504）：一台也没拉起，记下全部要补；下次跑（main 没有新提交）补拉起（Codex 审查）。"""
        r = self.rig(files=["labmon/labmon_worker.py"])
        orig = r.http
        r.http = lambda m, p, timeout=30, body=None: (r.calls.append(f"{m} {p}"), (504, b"{}"))[1] if p == "/collect?fresh" else orig(m, p, timeout, body)
        rc, out = r.run()
        self.assertEqual(rc, 1, out)
        self.assertFalse([c for c in r.calls if c.startswith("start")])
        r = self.rig(files=[])
        rc, out = r.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual([c for c in r.calls if c.startswith("start")], ["start a"])
        self.assertFalse(r.pending.exists())

    def test_skew_only_from_quick_replies(self):
        """时钟差只用很快回来的请求校准：立即采集、上传要等服务端做完，Date 头是做完时的，算进去会以为服务端快了（Codex 审查）。"""
        date = "Tue, 10 Nov 2026 15:00:00 GMT"
        srv = 1794322800.0
        with mock.patch.dict(DP.TARGET, {"url": "http://x/"}), mock.patch.object(DP, "SKEW", [0.0]):
            with mock.patch.object(DP, "_now", lambda: srv + 4.0):
                DP._skew(date, srv)                      # 采集用了 4 秒：不校准
            self.assertEqual(DP.SKEW[0], 0.0)
            with mock.patch.object(DP, "_now", lambda: srv + 0.2):
                DP._skew(date, srv)
            self.assertAlmostEqual(DP.SKEW[0], 0.4, places=3)        # 那一秒的中点 − 请求前后的中点

    def test_age_zero_is_fresh(self):
        """worker 状态刚写（age 恰好为 0）算在写，不算太旧。"""
        good, text = DP.host_check({"generated": 100, "hosts": [{"id": "a", "worker_up": True, "age": 0, "status": {"t": 100}}]}, {})
        self.assertTrue(good, text)
        self.assertIn("a 在写状态（0 秒前）", text)

    def test_head_fits_room(self):
        """剩下的行数很少时，带上去的提交一段也不超过它（“另 N 个”那行算在里面），至少一行。"""
        commits = [f"c{i} 提交" for i in range(20)]
        for room in range(-2, 8):
            lines = DP.head("aaa", "bbb", commits, room)
            self.assertLessEqual(len(lines), max(room, 1), (room, lines))
            self.assertIn("20 个提交", lines[0])
        text = "\n".join(DP.head("aaa", "bbb", commits, 2))
        self.assertIn("c0 提交", text)
        self.assertIn("另 19 个", text)


class HttpTest(unittest.TestCase):
    def test_collect_retries_incomplete_response_once(self):
        """响应体不足 Content-Length 时重试一次，第二次也断了就交给部署核对。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        for replies, expected in (([b"{", b"{}"], {}), ([b"{", b"{"], None)):
            with self.subTest(replies=replies):
                seen = []

                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.rfile.read(int(self.headers["Content-Length"]))
                        body = replies[len(seen)]
                        seen.append(self.path)
                        self.send_response(200)
                        self.send_header("Content-Length", "2")
                        self.end_headers()
                        self.wfile.write(body)

                    def log_message(self, *_args):
                        pass

                with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as srv:
                    worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                    worker.start()
                    try:
                        target = {"url": f"http://127.0.0.1:{srv.server_port}", "ca": None}
                        with mock.patch.dict(DP.TARGET, target), mock.patch.object(DP, "token", return_value=None):
                            self.assertEqual(DP.collect(), expected)
                    finally:
                        srv.shutdown()
                        worker.join()
                self.assertEqual(seen, ["/collect?fresh"] * 2)

    def test_post_passes_serve_gate(self):
        """deploy.http 的 POST 过得了看板的防跨站检查（#158：只认 application/json 的 POST）：起一个没有项目的看板服务（端口 0），不碰 8765。"""
        import threading
        from labmon.serve import Server
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"LABMON_STATE": td}):
            from fixtures import make_token
            os.environ["LABMON_TOKEN"] = make_token(scopes=("deploy",))      # 部署令牌（#160）：deploy.http 带上它
            srv = Server(("127.0.0.1", 0), [])
            threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
            try:
                with mock.patch.object(DP.config, "PORT", srv.server_address[1]), \
                        mock.patch.object(srv.overview, "collect_now", return_value=None) as go:
                    self.assertEqual(DP.http("POST", "/collect?fresh", timeout=10)[0], 504)      # 进了路由（桩回“采集超时”），没被 415 挡掉
                    go.assert_called_once_with(fresh=True)
                    self.assertEqual(DP.http("GET", "/fleet/op.json", timeout=10)[0], 200)
            finally:
                srv.shutdown()
                srv.server_close()


def sh_git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "-c", "init.defaultBranch=main", *args],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def commit(repo: Path, path: str, text: str, msg: str) -> str:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    sh_git(repo, "add", path)
    sh_git(repo, "commit", "-q", "-m", msg)
    return sh_git(repo, "rev-parse", "HEAD")


class RemoteTest(unittest.TestCase):
    """远端模式（#164）：本地主 checkout 打 bundle、经 POST /api/admin/deploy 传给看板服务，服务端快进它那份 checkout、要重启的交给 restart_later。
    看板服务真起（端口 0，内网入口；公网入口另起 Listener），服务端 checkout、本地主 checkout、GitHub 都是临时 git 仓库。"""

    def setUp(self):
        from labmon import deploy as LD
        from labmon import serve as SV
        from fixtures import make_token
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.up, self.cli, self.srv = d / "up.git", d / "cli", d / "srv"
        sh_git(d, "init", "-q", "--bare", str(self.up))
        sh_git(d, "clone", "-q", str(self.up), str(self.cli))
        sh_git(self.cli, "checkout", "-q", "-b", "main")
        self.c1 = commit(self.cli, "labmon/serve.py", "v1\n", "c1")
        sh_git(self.cli, "push", "-q", "origin", "main")
        sh_git(d, "clone", "-q", "-b", "main", str(self.up), str(self.srv))      # 常驻机上的那份（第一次从 bundle 克隆，这里直接克隆）
        self.LD, self.restarts = LD, []
        log = d / "labmon.log"
        log.write_text("[labmon] 服务启动：x\n")

        def restart_later(delay=2):            # 服务管理重启：新进程启动时 mark_start
            self.restarts.append(delay)
            LD.RUNNING.update(commit=LD.head(), started=LD.RUNNING["started"] + 1)

        def collect_now(timeout=None, fresh=False):
            from labmon.state import save_json, state_dir
            save_json(state_dir() / "snapshot.json", {"generated": time.time(), "hosts": [], "projects": []})
            return True
        self.ps = [mock.patch.dict(os.environ, {"LABMON_STATE": str(d / "state")}), mock.patch.dict(LD.RUNNING), mock.patch.dict(LD.RESTART),
                   mock.patch.object(LD, "ROOT", self.srv), mock.patch.object(LD.S, "LOG", log),
                   mock.patch.object(LD.S, "restart_later", restart_later),
                   mock.patch.object(DP, "ROOT", self.cli), mock.patch.object(DP, "TEST_ENV", ()), mock.patch.dict(DP.TARGET),
                   mock.patch.object(DP, "say", lambda m: None), mock.patch.object(DP, "page_check", lambda snap: (True, "页面过了")),
                   mock.patch.object(DP, "SKEW", [0.0]), mock.patch.object(DP, "PENDING_FILE", d / "deploy_pending.json")]
        for p in self.ps:
            p.start()
        LD.mark_start()
        collect_now()                          # 已有一份快照（/overview.json 有得读）
        self.tok = make_token(scopes=("deploy",))
        os.environ["LABMON_TOKEN"] = self.tok
        self.server = SV.Server(("127.0.0.1", 0), [])
        self.pub = SV.Listener(("127.0.0.1", 0), self.server)
        self.ps.append(mock.patch.object(self.server.overview, "collect_now", collect_now))
        self.ps[-1].start()
        for s in (self.server, self.pub):
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        DP.TARGET.update(url=f"http://127.0.0.1:{self.server.server_address[1]}/", ca=None)

    def run_deploy(self):
        try:
            return DP.deploy(1)
        except DP.Stop as e:
            return 2, [f"没部署：{e}"]

    def tearDown(self):
        for s in (self.server, self.pub):
            s.shutdown()
            s.server_close()
        for p in reversed(self.ps):
            p.stop()
        self.tmp.cleanup()

    def test_bundle_fast_forwards_and_restarts(self):
        """两个提交（代码与文档）推到 GitHub 后部署：服务端 checkout 快进到同一个提交、重启一次，/api/me 报出新提交，核对全过。"""
        commit(self.cli, "labmon/serve.py", "v2\n", "c2 改代码")
        c3 = commit(self.cli, "docs/a.md", "x\n", "c3 改文档")
        sh_git(self.cli, "push", "-q", "origin", "main")
        rc, out = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh_git(self.srv, "rev-parse", "HEAD"), c3)
        self.assertEqual(self.restarts, [2])
        text = "\n".join(out)
        self.assertIn(f"部署 {self.c1[:7]} → {c3[:7]}（2 个提交", text)
        self.assertIn("重启看板：是", text)
        self.assertEqual(self.LD.RUNNING["commit"], c3)

    def test_checkout_ahead_of_running_restarts(self):
        """上次快进了、重启没成（restart_later 失败、等平台操作等满）：checkout 已是新的、在跑的还是旧的，再部署要补上重启，不能报“没有新提交”（Codex 审查）。"""
        c2 = commit(self.cli, "labmon/serve.py", "v2\n", "c2 改代码")
        sh_git(self.cli, "push", "-q", "origin", "main")
        sh_git(self.srv, "pull", "-q", "--ff-only")                 # checkout 快进了，服务没重启：RUNNING 还是 c1
        self.assertEqual(self.LD.RUNNING["commit"], self.c1)
        rc, out = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.restarts, [2])
        self.assertEqual(self.LD.RUNNING["commit"], c2)
        text = "\n".join(out)
        self.assertIn(f"部署 {self.c1[:7]} → {c2[:7]}（1 个提交", text)
        self.assertIn("重启看板：是", text)

    def test_docs_only_no_restart_and_up_to_date(self):
        """只改文档：快进、不重启；再部署一次：服务端已是这个提交，只核对。"""
        c2 = commit(self.cli, "docs/a.md", "x\n", "c2 改文档")
        sh_git(self.cli, "push", "-q", "origin", "main")
        rc, out = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sh_git(self.srv, "rev-parse", "HEAD"), c2)
        self.assertEqual(self.restarts, [])
        self.assertIn("重启看板：否", "\n".join(out))
        rc, out = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertIn("main 没有新提交", out[0])

    def test_not_fast_forward_refused(self):
        """服务端的 main 有别处来的提交：bundle 不是快进，服务端 409、checkout 不动，脚本没动手（退出码 2）。"""
        stray = commit(self.srv, "labmon/x.py", "y\n", "常驻机上直接改的")
        commit(self.cli, "labmon/serve.py", "v2\n", "c2")
        sh_git(self.cli, "push", "-q", "origin", "main")
        rc, out = self.run_deploy()
        self.assertEqual(rc, 2, out)
        self.assertIn("不是快进", out[0])
        self.assertEqual(sh_git(self.srv, "rev-parse", "HEAD"), stray)
        self.assertEqual(self.restarts, [])

    def test_local_main_not_pushed_refused(self):
        """本地 main 有没推的提交：只部署推到 GitHub 的 main，不上传。"""
        commit(self.cli, "labmon/serve.py", "v2\n", "c2 没推")
        rc, out = self.run_deploy()
        self.assertEqual(rc, 2, out)
        self.assertIn("不一致", out[0])
        self.assertEqual(sh_git(self.srv, "rev-parse", "HEAD"), self.c1)

    def test_dirty_server_checkout_refused(self):
        (self.srv / "labmon" / "serve.py").write_text("改了没提交\n")
        commit(self.cli, "labmon/serve.py", "v2\n", "c2")
        sh_git(self.cli, "push", "-q", "origin", "main")
        rc, out = self.run_deploy()
        self.assertEqual(rc, 2, out)
        self.assertIn("没提交的改动", out[0])

    def post(self, port, path, tok, body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_route_guards(self):
        """部署只在内网入口（公网入口 404）、只认管理员的 deploy 令牌；坏请求体 409。"""
        from fixtures import make_token
        lan, pub = self.server.server_address[1], self.pub.server_address[1]
        b = {"bundle": base64_of(b"x")}
        self.assertEqual(self.post(pub, "/api/admin/deploy", self.tok, b)[0], 404)
        self.assertEqual(self.post(lan, "/api/admin/deploy", make_token("ann", "user", scopes=("read",)), b)[0], 403)
        self.assertEqual(self.post(lan, "/api/admin/deploy", make_token("bob", scopes=("read",)), b)[0], 403)
        code, d = self.post(lan, "/api/admin/deploy", self.tok, {"bundle": "不是 base64"})
        self.assertEqual(code, 409)
        self.assertIn("base64", d["error"])
        code, d = self.post(lan, "/api/admin/deploy", self.tok, b)
        self.assertEqual(code, 409)                 # 是 base64、不是 bundle：git bundle verify 不过
        self.assertEqual(sh_git(self.srv, "rev-parse", "HEAD"), self.c1)

    def test_worker_start_route(self):
        """部署后拉起 worker 经 POST /api/worker/start（服务端 workerctl.run_out）。"""
        from labmon import serve as SV
        with mock.patch.object(SV.W, "run_out", return_value=(0, "已拉起\n")) as ro:
            self.assertEqual(DP.worker_start("a"), (0, "已拉起\n"))
        ro.assert_called_once_with("start", "a", [], None, project=None, stop=None)
        self.assertEqual(self.post(self.pub.server_address[1], "/api/worker/start", self.tok, {"host": "a"})[0], 404)

    def test_busy_waits_then_restart(self):
        """服务端收部署时平台操作在做：拒绝（409）；收下后才开始的平台操作，重启等它做完（_restart 经 _claim 问）。"""
        with mock.patch.object(self.LD, "busy", return_value={"srv": "renew"}):
            with self.assertRaises(self.LD.Refused):
                self.LD.apply(base64_of(b"x"))
        seq = [{"srv": "renew"}, {"srv": "renew"}, {}]
        slept = []
        with mock.patch.object(self.LD, "_claim", lambda: seq.pop(0)):
            self.LD._restart(sleep=slept.append, now=lambda: 0)
        self.assertEqual(self.restarts, [2])
        self.assertEqual(len(slept), 2)
        self.restarts.clear()
        with mock.patch.object(self.LD, "_claim", return_value={"srv": "renew"}):
            t = [0]
            self.LD._restart(sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0])
        self.assertEqual(self.restarts, [], "等满了还在做：不重启")

    def test_no_platform_op_after_restart_scheduled(self):
        """安排了重启（restart_later 延迟几秒才重启）之后，平台操作不再开始：不然会被重启杀在半路（Codex 审查）。
        判定与 fleetops.start 开新操作在 op.json 的同一把锁里；restart_later 失败就放开。"""
        from labmon import fleetops as FO
        ran = []
        with mock.patch.multiple(FO, _check_start=lambda *a: None, _restore=lambda: None, steps_of=lambda *a: ["开保活"],
                                 run_ka=lambda *a, **kw: ran.append(1), _op_event=lambda *a: None), \
                mock.patch.object(FO.FB, "slots", lambda cfg: []), mock.patch.dict(self.LD.RESTART):
            self.LD._restart(sleep=lambda s: None)
            self.assertEqual(self.restarts, [2])
            with self.assertRaises(FO.P.PlatformError) as c:
                FO.start("ka", {"servers": []}, ids=["s1"])
            self.assertIn("马上要重启", str(c.exception))
            self.assertFalse(FO.op().get("active"))
            with mock.patch.object(self.LD.S, "restart_later", side_effect=OSError("no bus")):
                self.LD._restart(sleep=lambda s: None)
            self.assertFalse(self.LD.restarting())
            FO.start("ka", {"servers": []}, ids=["s1"])          # 重启没安排成：平台操作照常
            for th in [t for t in threading.enumerate() if t.name == "fleet-ka"]:
                th.join(5)
        self.assertEqual(ran, [1])

    def test_client_clock_ahead(self):
        """笔记本时钟比常驻机快一小时：快照与 worker 状态的先后按服务端的时钟（回话的 Date 头）比，不误报“快照早于采集”（Codex 审查）。"""
        commit(self.cli, "labmon/serve.py", "v2\n", "c2 改代码")
        sh_git(self.cli, "push", "-q", "origin", "main")
        with mock.patch.object(DP, "_now", lambda: time.time() + 3600):
            rc, out = self.run_deploy()
        self.assertEqual(rc, 0, out)
        self.assertLess(DP.SKEW[0], -3500)

    def test_remote_conf(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "deploy.toml"
            self.assertEqual(DP.remote_conf(f), {})
            f.write_text('url = "https://dashboard-host:8443/"\nca = "~/.config/labmon/ca.pem"\n')
            self.assertEqual(DP.remote_conf(f), {"url": "https://dashboard-host:8443/", "ca": "~/.config/labmon/ca.pem"})


def base64_of(b: bytes) -> str:
    return base64.b64encode(b).decode()


class LogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "labmon.log"
        self.p = mock.patch.object(DP.service, "LOG", self.log)     # labmon.deploy.log_text 缺省读 service.LOG
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    TB = ("[labmon] 11-09 10:00:01 全局页：采集出错\nTraceback (most recent call last):\n  File \"x.py\", line 1, in f\n    g()\n"
          "KeyError: 'hosts'\n")

    def test_only_after_last_start(self):
        """没重启：只看最后一次「服务启动」之后的；之前的 Traceback 不算。"""
        self.log.write_text(self.TB + "[labmon] 11-09 10:05:00 服务启动：x\n[labmon] 11-09 10:05:10 全局页：好\n")
        self.assertEqual(DP.D.tracebacks(DP.D.log_text(None)), [])
        with self.log.open("a") as f:
            f.write(self.TB)
        self.assertEqual(DP.D.tracebacks(DP.D.log_text(None)), ["KeyError: 'hosts'"])

    def test_since_offset_and_rotation(self):
        """重启：看重启前记下的长度之后的；文件变短（轮转过）时连 .1 一起看。"""
        self.log.write_text("旧的一行\n" + self.TB)
        off = self.log.stat().st_size
        with self.log.open("a") as f:
            f.write("[labmon] 服务启动：x\n")
        self.assertEqual(DP.D.tracebacks(DP.D.log_text(off)), [])
        shutil.copyfile(self.log, self.log.with_name("labmon.log.1"))
        self.log.write_text(self.TB.replace("KeyError: 'hosts'", "ValueError: x"))     # 轮转后从头写，比记下的长度短
        self.assertEqual(DP.D.tracebacks(DP.D.log_text(off)), ["ValueError: x"])

    def test_chained_exception_line(self):
        text = ("Traceback (most recent call last):\n  File \"a\"\nOSError: a\n\nDuring handling of the above exception, another exception occurred:\n\n"
                "Traceback (most recent call last):\n  File \"b\"\nRuntimeError: b\n")
        self.assertEqual(DP.D.tracebacks(text), ["OSError: a", "RuntimeError: b"])


@unittest.skipUnless(shutil.which("node"), "没有 node，页面冒烟检查跳过")
class SmokeTest(unittest.TestCase):
    """tests/pages/smoke.js：对 preview.build 渲染出的目录（布局同看板服务）全过；漏了 NaN、页面脚本出错都抓得到。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.site = Path(cls.tmp.name) / "site"
        env = {"LABMON_STATE": str(Path(cls.tmp.name) / "state"), "LABMON_HOSTS": str(ROOT / "tests" / "_no_user_hosts.toml")}
        sys.path[:0] = [p for p in (str(ROOT), str(ROOT / "tests")) if p not in sys.path]
        with mock.patch.dict(os.environ, env):
            from pages import preview
            cls.ids = preview.build(cls.site, "normal")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_clean_site_passes(self):
        res = DP.smoke(self.site)
        self.assertTrue(res and all(x["ok"] for x in res), [x for x in res or [] if not x["ok"]])
        ws = {x["w"] for x in res}
        self.assertIn("工作台 #/", ws)
        self.assertIn("工作台 #/fleet", ws)
        for i in self.ids:
            self.assertIn("项目页 " + i, ws)

    def test_leak_and_error_caught(self):
        bad = Path(self.tmp.name) / "bad"
        shutil.copytree(self.site, bad)
        ov = json.loads((bad / "overview.json").read_text())
        ov["hosts"][0]["label"] = "NaN"                       # 页面上漏出的 NaN
        (bad / "overview.json").write_text(json.dumps(ov))
        (bad / "p" / self.ids[0] / "index.html").write_text("<script>let D = null;\nthrow new Error('页面脚本出错')</script>")
        res = DP.smoke(bad)
        fails = {x["w"] for x in res if not x["ok"]}
        self.assertIn("工作台 #/", fails)
        self.assertIn("项目页 " + self.ids[0], fails)


if __name__ == "__main__":
    unittest.main()
