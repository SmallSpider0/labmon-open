"""命令行走看板接口（#162）：client.call 的几个地址依次试、退避、会改东西的不在半路重试、备用的接着试下一个；
令牌只在 Authorization 头里；project 令牌做不了借卡、整台主机的操作；labmon wait 的长轮询不漏接连发生的事（含回话丢了按请求号重给）、
连不上看板超过门槛以 server_unreachable 退出。

    python -m unittest tests.test_client -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import NO_HOSTS, board, register  # noqa: E402

from labmon import __main__ as M  # noqa: E402
from labmon import backup  # noqa: E402
from labmon import client  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import waits as W  # noqa: E402
from labmon.state import save_json  # noqa: E402


def closed_port() -> int:
    """一个现在没人听的端口（连上去被拒：请求没送到）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state"), "LABMON_HOSTS": NO_HOSTS, "LABMON_LAN": "on",
                                                "LABMON_CLIENT": str(self.d / "client.toml"), "LABMON_TOKEN": "t0k"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def servers(self, *urls):
        (self.d / "client.toml").write_text("".join(f'[[servers]]\nurl = "{u}"\n' for u in urls))

    def fake(self, replies):
        """起一个假看板：第 i 个请求按 replies[i]（最后一个重复用）回 (状态码, JSON[, "short"])；None 是收了请求不回、直接断开。返回 (地址, 收到的请求)。"""
        hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                hits.append((self.command, self.path, dict(self.headers), self.rfile.read(n) if n else b""))
                r = replies[min(len(hits), len(replies)) - 1]
                if r is None:
                    self.close_connection = True
                    return
                data = json.dumps(r[1]).encode()
                self.send_response(r[0])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data) + (100 if r[2:] == ("short",) else 0)))     # short：说好的回话体没发完就断开
                self.close_connection = r[2:] == ("short",)
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = do_PUT = _do
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}", hits


class CallTest(_Base):
    def test_report_and_watcher_cli(self):
        base, hits = self.fake([(201, {"rc": 0, "out": "已提交报告"}), (200, {"reports": []})])
        self.servers(base)
        watcher = self.d / "reports.token"
        watcher.write_text("watcher-token\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(["report", "异常", "--evidence", "日志", "--project", "p", "--host", "h"]), 0)
            self.assertEqual(M.main(["report", "--list", "--token-file", str(watcher)]), 0)
        self.assertEqual((hits[0][0], hits[0][1], hits[0][2]["Authorization"]), ("POST", "/api/report", "Bearer t0k"))
        self.assertEqual(json.loads(hits[0][3]), {"title": "异常", "evidence": "日志", "project": "p", "host": "h", "occurred": None})
        self.assertEqual((hits[1][0], hits[1][1], hits[1][2]["Authorization"]), ("GET", "/api/reports", "Bearer watcher-token"))

    def test_fallback_and_backoff(self):
        # 第一个地址连不上、第二个先 503 两次：做两遍也没事的 GET 依次试、退避 1、2 秒后拿到
        url, hits = self.fake([(503, {"error": "忙"}), (503, {}), (200, {"ok": 1})])
        self.servers(f"http://127.0.0.1:{closed_port()}", url)
        slept = []
        with mock.patch.object(client.time, "sleep", slept.append):
            self.assertEqual(client.call("GET", "/x", patience=30), {"ok": 1})
        self.assertEqual((slept, len(hits)), ([1.0, 2.0], 3))
        self.assertEqual(hits[0][2]["Authorization"], "Bearer t0k")              # 令牌只在头里
        self.assertEqual(hits[0][1], "/x")
        with mock.patch.object(client.time, "sleep", slept.append), self.assertRaises(client.Unreachable) as e:
            self.servers(f"http://127.0.0.1:{closed_port()}")
            client.call("GET", "/x", patience=0)                               # patience 0：各试一遍就算
        self.assertIn("连不上看板服务（试过 http://127.0.0.1:", str(e.exception))

    def test_write_not_retried_after_sent(self):
        # 会改东西的：503（没进到处理）重试；发出去之后回话断了不重试，照实报不知道做成没有
        url, hits = self.fake([(503, {}), (200, {"rc": 0})])
        self.servers(url)
        with mock.patch.object(client.time, "sleep", lambda s: None):
            self.assertEqual(client.call("POST", "/api/x", {"a": 1}), {"rc": 0})
        self.assertEqual([h[3] for h in hits], [b'{"a": 1}'] * 2)
        url, hits = self.fake([None, (200, {"rc": 0})])
        self.servers(url)
        with mock.patch.object(client.time, "sleep", lambda s: None), self.assertRaises(client.Unreachable) as e:
            client.call("POST", "/api/x", {"a": 1})
        self.assertTrue(e.exception.lost)
        self.assertIn("不知道做成没有", str(e.exception))
        self.assertEqual(len(hits), 1)

    def test_error_body_cut(self):
        # 回了 503 的头、回话体没收全就断了：当 503 退避重试（不带着 IncompleteRead 逃出 call，wait 照样按门槛报 server_unreachable）
        url, hits = self.fake([(503, {"error": "忙"}, "short"), (200, {"ok": 1})])
        self.servers(url)
        with mock.patch.object(client.time, "sleep", lambda s: None):
            self.assertEqual(client.call("GET", "/x"), {"ok": 1})
        self.assertEqual(len(hits), 2)

    def test_standby_and_errors(self):
        # 备用的看板服务（#163）回 409 standby：接着试下一个；都是备用当场报 409、不退避；别的 4xx 抛 Failed 带原因
        sb, _ = self.fake([(409, {"error": "这台是备用：主是 b-1", "standby": True})])
        ok, hits = self.fake([(200, {"rc": 0})])
        self.servers(sb, ok)
        self.assertEqual(client.call("POST", "/api/x"), {"rc": 0})
        self.servers(sb)
        with mock.patch.object(client.time, "sleep", lambda s: self.fail("不该退避")), self.assertRaises(client.Failed) as e:
            client.call("POST", "/api/x")
        self.assertEqual((e.exception.code, e.exception.msg), (409, "这台是备用：主是 b-1"))
        bad, _ = self.fake([(403, {"error": "这个令牌没有「管理员」权限"})])
        self.servers(bad)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(client.run("POST", "/api/borrow", {}, admin=True), 3)
        self.assertIn("这个令牌没有「管理员」权限；这类事要管理员在内网入口做", err.getvalue())

    def test_admin_401_says_login_admin(self):
        # 要管理员的命令 401（admin.token 过期、吊销）：那句指到 labmon login --admin，不是只更新 client.token 的两条路（#191，PR #192 审查）
        os.environ.pop("LABMON_TOKEN")
        (self.d / "admin.token").write_text("old\n")
        url, _ = self.fake([(401, {"error": "令牌无效、已过期或已吊销"})])
        self.servers(url)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(client.run("POST", "/api/borrow", {}, admin=True), 3)
        self.assertIn("令牌无效、已过期或已吊销；这条命令要管理员令牌", err.getvalue())
        self.assertIn(f"labmon login --admin（1 小时的 admin 令牌，写到 {self.d / 'admin.token'}）", err.getvalue())
        self.assertNotIn("--scope project", err.getvalue())

    def test_env_proxy_ignored(self):
        # 环境里配了代理（Claude 应用里 HTTPS_PROXY 指向 Clash，NO_PROXY 没有看板的地址，#195）：连看板一律直连，代理一个请求也收不到。
        # 命令行（call、login）、部署脚本的远端模式、冷备的 pull 都经 client.opener
        proxy, via = self.fake([(502, {"error": "代理断了"})])
        url, hits = self.fake([(200, {"ok": 1, "token": "t1", "name": "命令行", "scopes": ["project"]})])
        self.servers(url)
        for k in ("no_proxy", "NO_PROXY"):
            os.environ.pop(k, None)
        os.environ.update({k: proxy for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")})
        self.assertEqual(client.call("GET", "/x", patience=0)["ok"], 1)
        with mock.patch("getpass.getpass", return_value="pw"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(client.login(name="u"), 0)
        spec = importlib.util.spec_from_file_location("deploy_195", Path(__file__).resolve().parents[1] / "tools" / "deploy.py")
        DP = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(DP)
        with mock.patch.dict(DP.TARGET, {"url": url, "ca": None}):
            self.assertEqual(DP.http("GET", "/y")[0], 200)
        with mock.patch.dict(os.environ, {"LABMON_BACKUPS": str(self.d / "bk")}):
            backup.pull(url)                                                      # 回的不是备份包：只看请求去了哪
        self.assertEqual(via, [])
        self.assertEqual([h[1] for h in hits], ["/x", "/api/login", "/api/tokens/add", "/api/logout", "/y", "/api/admin/backup"])

    def test_token_files(self):
        # 没有 LABMON_TOKEN：client.token；要管理员的先 admin.token
        os.environ.pop("LABMON_TOKEN")
        self.servers("http://127.0.0.1:1")
        (self.d / "client.token").write_text("proj\n")
        self.assertEqual((client.token(), client.token(admin=True)), ("proj", "proj"))
        (self.d / "admin.token").write_text("adm\n")
        self.assertEqual((client.token(), client.token(admin=True)), ("proj", "adm"))
        self.assertEqual(client.urls("p1"), ["http://127.0.0.1:1/p/p1/"])


class BoardTest(_Base):
    """经临时看板服务（fixtures.board）。"""

    def setUp(self):
        super().setUp()
        (self.d / "proj").mkdir()
        self.cfg = self.d / "proj" / "monitor.toml"
        self.cfg.write_text('id = "p1"\n')
        register(self.cfg)
        self.snap([])

    def snap(self, alerts):
        save_json(PJ.out_dir("p1") / "snapshot.json", {"id": "p1", "generated": time.time(), "alerts": alerts, "hosts": [], "lanes": [], "jobs": []})

    def test_project_token_cannot_borrow_or_touch_hosts(self):
        board(self)
        for argv in (["borrow", "s1", "--until", "+1h"], ["drain", "h1"], ["worker", "stop", "h1"], ["host", "return", "h1"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(M.main(argv), 3, argv)
            self.assertIn("labmon login --admin", err.getvalue(), argv)

    def test_long_poll_events_and_rid(self):
        # 看板那边挂着：快照里出了告警就回（不等满）；回话丢了带同一个请求号再来，回上次那份（已记成报过的不漏）；接着来的下一件照样报
        board(self)
        a1, a2 = {"key": "a1", "msg": "一"}, {"key": "a2", "msg": "二"}
        threading.Timer(0.5, self.snap, [[a1]]).start()
        t0 = time.time()
        r = client.call("POST", "/api/wait", {"project": "p1", "hold": 10, "rid": "r1"}, timeout=30, idem=True)
        self.assertEqual(r["new"], {"a1": "一"})
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(client.call("POST", "/api/wait", {"project": "p1", "hold": 0, "rid": "r1"}, idem=True)["new"], {"a1": "一"})
        self.assertEqual(client.call("POST", "/api/wait", {"project": "p1", "hold": 0, "rid": "r2"}, idem=True)["new"], {})
        threading.Timer(0.5, self.snap, [[a1, a2]]).start()
        self.assertEqual(client.call("POST", "/api/wait", {"project": "p1", "hold": 10, "rid": "r3"}, timeout=30, idem=True)["new"], {"a2": "二"})
        with self.assertRaises(client.Failed) as e:
            client.call("POST", "/api/wait", {"project": "nope", "hold": 0}, idem=True)
        self.assertEqual(e.exception.code, 404)

    def test_one_reply_per_request(self):
        # 命令行的接口回一次就完：不在 200 之后再补一个 404（原始 socket 收全部字节，只有一个状态行）
        board(self)
        port = int(client.servers()[0]["url"].rsplit(":", 1)[1])
        body = json.dumps({"project": "p1", "hold": 0}).encode()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(b"POST /api/wait HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n"
                      b"Content-Length: %d\r\nConnection: close\r\n\r\n" % (port, os.environ["LABMON_TOKEN"].encode(), len(body)) + body)
            raw = b""
            while True:
                b = s.recv(65536)
                if not b:
                    break
                raw += b
        self.assertRegex(raw[:20], rb"^HTTP/1\.[01] 200 ")
        self.assertEqual(raw.count(b"HTTP/1."), 1, raw)

    def test_wait_busy(self):
        # 同时挂着的满了：503，客户端退避
        with mock.patch.object(W, "_SLOTS", threading.BoundedSemaphore(1)):
            W._SLOTS.acquire()
            with self.assertRaises(W.Busy):
                W.wait("p1")

    def test_wait_first_round_config_error(self):
        # 第一轮推上去就被看板拒（配置不对，400）：同以前读配置出错，退出 3
        board(self)
        self.cfg.write_text('id = "p1"\nalerts = 5\n')
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(M.cmd_wait(str(self.cfg)), 3)
        self.assertIn("配置错误：", err.getvalue())

    def test_no_token_says_what_to_do(self):
        # 命令行没有令牌（或令牌不认）：看板回 401，wait 退出 3，那句话写明交给用户、跑哪条（#191；以前只有「要先登录」）
        board(self)
        os.environ.pop("LABMON_TOKEN")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(M.cmd_wait(str(self.cfg)), 3)
        msg = err.getvalue()
        self.assertIn("要先登录；命令行要一个令牌，这步交给用户做", msg)
        self.assertIn("labmon login", msg)
        self.assertIn(f"labmon user token <看板账号> --scope project --out {client.token_file()}", msg)
        os.environ["LABMON_TOKEN"] = "bad"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(M.main(["check", "-c", str(self.cfg)]), 3)
        self.assertIn("令牌无效、已过期或已吊销；环境变量 LABMON_TOKEN 里的令牌看板不认", err.getvalue())

    def test_wait_server_unreachable(self):
        # 连不上看板：退避重试；连续超过门槛（config.stale_limit，测试换成 0.5 秒）以 server_unreachable 告警退出 1
        self.servers(f"http://127.0.0.1:{closed_port()}")
        out, slept = io.StringIO(), []
        real = time.sleep
        with mock.patch.object(M, "stale_limit", lambda *a: 0.5), mock.patch.object(M.time, "sleep", lambda s: (slept.append(s), real(0.3))), \
                contextlib.redirect_stdout(out):
            self.assertEqual(M.cmd_wait(str(self.cfg)), 1)
        self.assertIn("[server_unreachable] 连不上看板服务已", out.getvalue())
        self.assertEqual(slept[:2], [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
