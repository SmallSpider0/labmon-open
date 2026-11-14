"""公网入口的落地（#165）：[server] 段、Host 允许表、内网入口的 TLS、/healthz、计划维护、服务端写的相对地址。离线、端口 0、不连任何服务器。

    python -m unittest tests.test_public -v
"""
from __future__ import annotations

import http.client
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402
from fixtures import NO_HOSTS, make_token  # noqa: E402
os.environ["LABMON_LAN"] = "off"
os.environ["LABMON_HOSTS"] = NO_HOSTS

from labmon import config as K  # noqa: E402
from labmon import dist as DI  # noqa: E402
from labmon import hostcheck as HC  # noqa: E402
from labmon import maint as MT  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon.__main__ import main as cli  # noqa: E402
from labmon.config import ConfigError  # noqa: E402
from labmon.records import records  # noqa: E402
from labmon.serve import Listener, Server, allowed_hosts, host_ok, root_rel, serve  # noqa: E402
from labmon.state import save_json, state_dir  # noqa: E402


class _Env(unittest.TestCase):
    """每个用例一个临时状态目录与主机清单（hosts.toml 只写这个用例要的 [server]）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state"), "LABMON_HOSTS": str(self.d / "hosts.toml")})
        self.env.start()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def hosts(self, text: str):
        (self.d / "hosts.toml").write_text(text, encoding="utf-8")

    def run_srv(self, s):
        self.servers.append(s)
        threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return s.server_address[1]


class ServerConfTest(_Env):
    def test_defaults_without_section(self):
        self.assertEqual(K.server(), {"lan_bind": "127.0.0.1", "tls_cert": None, "tls_key": None, "ca_cert": None, "min_client_commit": None, "lan_hosts": [], "public_hosts": []})

    def test_reads_and_expands(self):
        self.hosts('[server]\nlan_bind = "0.0.0.0"\ntls_cert = "tls/dashboard-host.crt"\ntls_key = "~/k.pem"\n'
                   'lan_hosts = ["DASHBOARD-HOST.lab:8443", "192.0.2.2:8443"]\npublic_hosts = ["lab.example.org"]\n')
        c = K.server()
        self.assertEqual(c["tls_cert"], str(self.d / "tls" / "dashboard-host.crt"))          # 相对路径按主机清单所在目录
        self.assertEqual(c["tls_key"], str(Path("~/k.pem").expanduser()))
        self.assertEqual((c["lan_bind"], c["lan_hosts"], c["public_hosts"]), ("0.0.0.0", ["dashboard-host.lab:8443", "192.0.2.2:8443"], ["lab.example.org"]))

    def test_rejects(self):
        for text, word in (('min_client_commit = "abc"', "完整的 40 位"),
                           ('lan_bnd = "203.0.113.4"', "不认识"),                       # 拼错的安全设置不能悄悄不生效
                           ('tls_cert = "a.crt"', "一起写"),
                           ('lan_hosts = "dashboard-host.lab"', "lan_hosts"),
                           ('public_hosts = ["https://blocked.example/"]', "public_hosts"),
                           ('lan_bind = "dashboard-host.lab"', "IPv4"),
                           ('lan_bind = "192.0.2.2"', "tls_cert"),                    # 绑到非本机地址却没配证书：口令不在内网上明文传
                           ('lan_bind = "0.0.0.0"', "tls_cert")):
            self.hosts(f"[server]\n{text}\n")
            with self.assertRaises(ConfigError, msg=text) as e:
                K.server()
            self.assertIn(word, str(e.exception), text)
        self.hosts('[server]\nlan_bind = "127.0.0.2"\n')                              # 回环地址不要证书
        self.assertEqual(K.server()["lan_bind"], "127.0.0.2")

    def test_serve_refuses_bad_section(self):
        self.hosts('[server]\nlan_bind = "0.0.0.0"\n')
        lines = []
        with mock.patch("labmon.serve.log", lines.append):
            self.assertEqual(serve(0), 2)                                       # 不退回明文、不只开一半
        self.assertIn("tls_cert", lines[-1])
        self.hosts(f'[server]\ntls_cert = "{self.d}/none.crt"\ntls_key = "{self.d}/none.key"\n')
        with mock.patch("labmon.serve.log", lines.append):
            self.assertEqual(serve(0), 2)                                       # 证书读不了
        self.assertIn("证书", lines[-1])


class HostTest(_Env):
    def test_allowed_and_default_port(self):
        a = allowed_hosts(80, [], 80)                                               # 没 TLS 的 serve --port 80：浏览器省掉端口（Codex 审查 #172）
        self.assertTrue(host_ok("127.0.0.1", 80, a))
        self.assertTrue(host_ok("LOCALHOST:80", 80, a))
        for h in ("evil.example", "[fe80::1]", "", "127.0.0.1:8765", "127.0.0.1.evil.example"):
            self.assertFalse(host_ok(h, 80, a), h)
        a = allowed_hosts(8443, ["dashboard-host.lab:8443", "lab.example.org"], 443)          # 有 TLS 的：不带端口的按 443
        self.assertTrue(host_ok("dashboard-host.lab:8443", 443, a))
        self.assertTrue(host_ok("127.0.0.1:8443", 443, a))
        self.assertTrue(host_ok("lab.example.org", 443, a))
        self.assertTrue(host_ok("lab.example.org:443", 443, a))
        for h in ("dashboard-host.lab", "127.0.0.1", "lab.example.org:8443", "evil.example:8443"):
            self.assertFalse(host_ok(h, 443, a), h)

    def test_bind_address_is_allowed(self):
        """内网入口绑了具体地址时，那个地址加端口本身也认（config.lan_url 经它连，Codex 审查 #182）；绑 0.0.0.0 的不认 0.0.0.0。"""
        for bind, want in (("192.0.2.3", True), ("0.0.0.0", False)):
            srv = Server(("127.0.0.1", 0), [], conf={"lan_bind": bind})
            self.addCleanup(srv.server_close)
            self.assertEqual((bind, srv.server_address[1]) in srv.hosts, want, bind)
        self.assertEqual(K.lan_url(8443, {"lan_bind": "192.0.2.3", "tls_cert": None}), ("http://192.0.2.3:8443", None))
        self.assertEqual(K.lan_url(8443, {"lan_bind": "0.0.0.0", "tls_cert": None})[0], "http://127.0.0.1:8443")

    def test_public_entry_hosts(self):
        board = Server(("127.0.0.1", 0), [])
        self.addCleanup(board.server_close)                                          # 没起 serve_forever：不能 shutdown（会一直等）
        pub = Listener(("127.0.0.1", 0), board, ["lab.example.org"])
        port = self.run_srv(pub)
        for h, code in (("lab.example.org", 200), ("LAB.example.org:443", 200), (f"127.0.0.1:{port}", 200),   # 看门狗经隧道直接探
                        ("lab.example.org:8443", 403), ("evil.example", 403), (f"evil.example:{port}", 403)):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.putrequest("GET", "/healthz", skip_host=True)
            c.putheader("Host", h)
            c.endheaders()
            r = c.getresponse()
            self.assertEqual(r.status, code, h)
            if code == 403:
                self.assertIn("public_hosts", json.loads(r.read())["error"])
            c.close()


class HealthTest(_Env):
    def get(self, port, path="/healthz"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        out = (r.status, r.getheader("Location"), r.read())
        c.close()
        return out

    def test_fields_only_and_no_login(self):
        srv = Server(("127.0.0.1", 0), [])
        lan = self.run_srv(srv)
        wan = self.run_srv(Listener(("127.0.0.1", 0), srv))
        make_token()                                                                 # 有账号了照样不要登录
        for port in (lan, wan):
            code, _, body = self.get(port)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body), {"ok": True, "primary": None, "last_collect": None, "maint_until": None})
        snap = {"generated": time.time(), "hosts": [{"id": "srv-secret"}], "projects": [{"id": "proj-secret"}], "fleet": {"servers": [{"id": "srv-x"}]}}
        save_json(state_dir() / "snapshot.json", snap)
        os.utime(state_dir() / "snapshot.json", (1_800_000_000, 1_800_000_000))      # 最后一次采集：全局快照的修改时刻
        end = MT.start(time.time() + 1800, "测试") and MT.until()
        for port in (lan, wan):
            code, _, body = self.get(port)
            d = json.loads(body)
            self.assertEqual(set(d), {"ok", "primary", "last_collect", "maint_until"})   # 只这四项：不出主机、项目、人员
            self.assertEqual((d["last_collect"], d["maint_until"]), (1_800_000_000, end))
            for s in ("srv-secret", "proj-secret", "srv-x", "测试"):
                self.assertNotIn(s, body.decode())
        self.assertEqual(self.get(lan, "/overview.json")[0], 401)                     # 别的照样要登录

    def test_relative_redirects(self):
        self.assertEqual((root_rel("/"), root_rel("/p/a"), root_rel("/p/a/"), root_rel("/p/a/x.json")), ("./", "../", "../../", "../../"))
        srv = Server(("127.0.0.1", 0), [])
        port = self.run_srv(srv)
        tok = make_token()
        srv.projects = {"a": mock.Mock(id="a")}
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/p/a", headers={"Authorization": f"Bearer {tok}"})
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Location")), (302, "a/"))         # 相对地址：反代在前面加了 /<前缀>/ 也对
        r.read()
        c.request("GET", "/p/nope/", headers={"Authorization": f"Bearer {tok}"})
        r = c.getresponse()
        self.assertEqual(r.status, 404)
        self.assertIn("href='../../'", r.read().decode())
        c.close()


@unittest.skipUnless(subprocess.run(["openssl", "version"], capture_output=True).returncode == 0, "要 openssl 生成自签证书")
class TlsTest(_Env):
    def setUp(self):
        super().setUp()
        self.crt, self.key = self.d / "c.pem", self.d / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(self.key), "-out", str(self.crt), "-days", "1",
                        "-subj", "/CN=localhost"], check=True, capture_output=True)
        self.iters = mock.patch.object(U, "ITER", 1000)
        self.iters.start()

    def tearDown(self):
        self.iters.stop()
        super().tearDown()

    def https(self, port, method, path, body=None, host=None):
        c = http.client.HTTPSConnection("127.0.0.1", port, timeout=10, context=ssl._create_unverified_context())
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Host": host or f"127.0.0.1:{port}", **({"Content-Type": "application/json"} if body is not None else {})})
        r = c.getresponse()
        out = (r.status, r.getheader("Set-Cookie"), r.read())
        c.close()
        return out

    def test_https_secure_cookie_and_stray_connections(self):
        srv = Server(("127.0.0.1", 0), [], conf={"tls_cert": str(self.crt), "tls_key": str(self.key), "lan_hosts": []})
        port = self.run_srv(srv)
        self.assertTrue(srv.secure)
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            idle = socket.create_connection(("127.0.0.1", port), timeout=10)        # 连上不说话的：不挡别人（握手在处理线程里、限时）
            plain = http.client.HTTPConnection("127.0.0.1", port, timeout=10)      # 用 http 打到 HTTPS 端口：断开，不记 traceback
            try:
                plain.request("GET", "/healthz")
                plain.getresponse().read()
            except (OSError, http.client.HTTPException):
                pass
            plain.close()
            self.assertEqual(self.https(port, "GET", "/healthz")[0], 200)
            idle.close()
            time.sleep(0.2)
        code = fixtures.add_account("boss", "admin", "本机命令行")
        st, ck, body = self.https(port, "POST", "/api/setup", {"name": "boss", "code": code, "password": "correct horse"})
        self.assertEqual(st, 200, body)
        self.assertTrue(ck.endswith("; Secure"), ck)                                 # 有 TLS 的内网入口：会话 Cookie 加 Secure
        self.assertEqual(self.https(port, "GET", "/healthz", host="127.0.0.1")[0], 403)     # 不带端口按 443，这个入口不在 443
        # 命令行与部署脚本经 config.lan_url 连：HTTPS、只信 [server] 的那张证书（Codex 审查 #182：原来写死 http://）
        base, ctx = K.lan_url(port, {"lan_bind": "127.0.0.1", "tls_cert": str(self.crt)})
        self.assertTrue(base.startswith("https://127.0.0.1:"))
        with urllib.request.urlopen(base + "/healthz", timeout=10, context=ctx) as r:
            self.assertEqual(json.loads(r.read())["ok"], True)
        other = self.d / "o.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(self.d / "ok.pem"), "-out", str(other), "-days", "1",
                        "-subj", "/CN=localhost"], check=True, capture_output=True)
        with self.assertRaises(urllib.error.URLError):                               # 换了一张证书的不信
            urllib.request.urlopen(base + "/healthz", timeout=10, context=K.lan_url(port, {"lan_bind": "127.0.0.1", "tls_cert": str(other)})[1])
        self.assertNotIn("Traceback", err.getvalue())                                 # 握手失败、断开的不往日志里写 traceback

    def test_install_download_reads_ca_without_restart(self):
        port = self.run_srv(Server(("127.0.0.1", 0), [], conf={
            "tls_cert": str(self.crt), "tls_key": str(self.key), "lan_hosts": []}))
        base = f"https://127.0.0.1:{port}/"
        path = "/api/client/install.sh?" + urllib.parse.urlencode({"base": base})
        tok = make_token(scopes=("read",))
        ctx = K.pinned(str(self.crt))

        def download():
            req = urllib.request.Request(base.rstrip("/") + path, headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                return r.read().decode()

        with mock.patch.object(DI, "wheel", return_value=("labmon.whl", b"wheel", "a" * 40)):
            self.assertNotIn("<<'LABMON_CA'", download())
            self.hosts('[server]\nca_cert = "c.pem"\n')
            installed = download()
            self.assertIn("<<'LABMON_CA'", installed)
            self.assertIn('ca = "~/.config/labmon/ca/labmon-ca-', installed)
            self.hosts('[server]\nca_cert = "missing.pem"\n')
            with self.assertRaises(urllib.error.HTTPError) as error:
                download()
            self.assertEqual(503, error.exception.code)


class MaintTest(_Env):
    def test_start_stop_limits_and_record(self):
        now = time.time()
        self.assertIsNone(MT.until())
        msg = MT.start(now + 1800, "本机命令行", t=now)
        self.assertIn("计划维护到", msg)
        self.assertEqual(MT.until(now), round(now + 1800))
        self.assertIsNone(MT.until(now + 1801))                                      # 到期自然失效
        with self.assertRaises(ConfigError):
            MT.start(now - 1, "x", t=now)
        with self.assertRaises(ConfigError):
            MT.start(now + MT.MAX_S + 3600, "x", t=now)                              # 最长一天
        self.assertIn("已结束", MT.stop("本机命令行", t=now))
        self.assertIsNone(MT.until(now))
        self.assertEqual(MT.stop("x", t=now), "没在计划维护")
        rows = [r for r in records() if r["kind"] == "maint"]
        self.assertEqual(len(rows), 2)
        see = U.visible(U.Ident("amy", "user", frozenset({"read"}), "token", "k"))
        self.assertFalse([r for r in rows if see(r)])                                # 普通用户看不到（管服务器的事）

    def test_cli(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(cli(["server", "maint", "30m"]), 0)
            u = MT.until()
            self.assertTrue(u and abs(u - time.time() - 1800) < 5)
            self.assertEqual(cli(["server", "maint"]), 0)
            self.assertEqual(cli(["server", "maint", "off"]), 0)
            self.assertIsNone(MT.until())
            with mock.patch("sys.stderr"):
                self.assertEqual(cli(["server", "maint", "25h"]), 3)
        self.assertIsNone(MT.until())

    def test_hostcheck_view(self):
        now = time.time()
        MT.start(now + 600, "x", t=now)
        with mock.patch.object(HC, "enabled", return_value=True):
            self.assertEqual(HC.view(now)["maint"], round(now + 600))


if __name__ == "__main__":
    unittest.main()
