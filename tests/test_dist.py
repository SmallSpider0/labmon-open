"""看板客户端分发与安装的离线测试。"""
import io
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from labmon import dist


class ClientDistTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.package = self.repo / "labmon"
        self.package.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "测试")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
        source = dist.HERE
        for p in source.glob("*.py"):
            shutil.copy(p, self.package / p.name)
        self.commit = self.save("初版")
        loc = patch.object(dist, "HERE", self.package)
        loc.start()
        self.addCleanup(loc.stop)
        cache = patch.object(dist, "_CACHE", {})
        cache.start()
        self.addCleanup(cache.stop)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def save(self, msg):
        self.git("add", ".")
        self.git("commit", "-qm", msg)
        return self.git("rev-parse", "HEAD")

    def test_builds_running_commit_even_after_main_moves(self):
        (self.package / "later.txt").write_text("新提交")
        later = self.save("更新")
        (self.package / "uncommitted.txt").write_text("未提交")
        name, data, commit = dist.wheel(self.commit)
        self.assertEqual(self.commit, commit)
        self.assertTrue(dist.wheel_name_ok(name, commit))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertNotIn("labmon/later.txt", z.namelist())
            self.assertNotIn("labmon/uncommitted.txt", z.namelist())
            self.assertIn(commit.encode(), z.read("labmon/_build.py"))
        self.assertNotEqual(data, dist.wheel(later)[1])
        self.git("switch", "-qc", "dev")
        (self.package / "dev.txt").touch()
        branch = self.save("开发版")
        with self.assertRaises(dist.Unavailable):
            dist.wheel(branch)

    def test_client_status_uses_history(self):
        (self.package / "middle.txt").touch()
        minimum = self.save("最低版")
        (self.package / "latest.txt").touch()
        current = self.save("当前版")
        self.assertEqual(dist.client_status(self.commit, current, minimum), "below_minimum")
        self.assertEqual(dist.client_status(minimum, current, minimum), "outdated")
        self.assertEqual(dist.client_status(current, current, minimum), "current")
        self.assertEqual(dist.client_status(self.commit, current, None), "outdated")
        self.assertEqual(dist.client_status("0" * 40, current, minimum), "unknown")

    def test_install_update_and_failed_install(self):
        home = self.root / "home with spaces"
        config = home / ".config/labmon"
        config.mkdir(parents=True)
        (config / "client.toml").write_text("# 保留\n")
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / "data"), PYTHON=sys.executable)
        fixed = home / "data/labmon/bin/labmon"
        old = home / ".local/bin/labmon"
        old.parent.mkdir(parents=True)
        old.write_text("旧安装")
        name, script = dist.install_sh("https://tx.example/labmon/", b"private", True, self.commit)
        f = self.root / name
        f.write_text(script)
        subprocess.run(["sh", str(f)], env=env, capture_output=True, check=True)
        self.assertEqual("# 保留\n", (config / "client.toml").read_text())
        new_config = (config / "client.toml.new").read_text()
        self.assertIn('url = "https://tx.example/labmon"', new_config)
        self.assertIn("# cert =", new_config)
        self.assertNotIn("ca =", new_config)
        self.assertEqual("旧安装", old.read_text())
        self.assertTrue(fixed.is_symlink())
        original = fixed.resolve()

        (self.package / "next.txt").touch()
        next_commit = self.save("下一版")
        wheel_name, wheel, _ = dist.wheel(next_commit)
        wheel_file = self.root / wheel_name
        wheel_file.write_bytes("无效包".encode())
        requests = []
        response = {"data": wheel_file.read_bytes(), "redirect": True}

        class Board(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("X-Labmon-Client"), self.headers.get("Authorization")))
                if response["redirect"]:
                    self.send_response(302)
                    self.send_header("Location", "/unexpected")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Disposition", f'attachment; filename="{wheel_name}"')
                self.send_header("X-Labmon-Commit", next_commit)
                self.end_headers()
                self.wfile.write(response["data"])

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Board) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                local_config = config / "test.toml"
                local_config.write_text(f'[[servers]]\nurl = "http://127.0.0.1:{server.server_port}"\n')
                env.update(LABMON_CLIENT=str(local_config), LABMON_TOKEN="test-token")
                def update():
                    return subprocess.run([str(fixed), "update"], env=env, capture_output=True, text=True)

                refused = update()
                self.assertEqual(3, refused.returncode, refused.stderr)
                self.assertEqual([("/api/client/wheel", self.commit, "Bearer test-token")], requests)
                response["redirect"] = False
                failed = update()
                self.assertEqual(3, failed.returncode, failed.stderr)
                self.assertEqual(original, fixed.resolve())
                response["data"] = wheel
                succeeded = update()
                self.assertEqual(0, succeeded.returncode, succeeded.stderr)
            finally:
                server.shutdown()
                thread.join()
        self.assertIn(next_commit, str(fixed.resolve()))
        self.assertTrue(original.is_file())

    def test_install_address_and_secret(self):
        for base in ("https://user@example.com/", "https://example.com/a/../b", "https://example.com/?a=b",
                     "https://example.com:99999", "https://example.com:0", "https://example.com/\""):
            with self.subTest(base=base):
                self.assertIsNone(dist.parse_base(base))
        self.assertEqual(("https", "dashboard-host:8443"), dist.parse_base("https://dashboard-host:8443/"))
        self.assertEqual(("https", "[2001:db8::1]:443"), dist.parse_base("https://[2001:db8::1]:443/path/"))
        with self.assertRaises(dist.Unavailable):
            dist.install_sh("https://dashboard-host:8443/", b"-----BEGIN PRIVATE KEY-----", False, self.commit)

    @unittest.skipUnless(shutil.which("openssl"), "要 openssl 生成测试 CA 和入口证书")
    def test_lan_install_first_connection(self):
        def openssl(*args):
            subprocess.run(["openssl", *args], cwd=self.root, check=True, capture_output=True)

        for ca in ("ca", "other"):
            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", f"/CN={ca}", "-keyout", f"{ca}.key", "-out", f"{ca}.crt",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
                    "-addext", "subjectKeyIdentifier=hash")
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                "-keyout", "board.key", "-out", "board.csr")
        (self.root / "board.ext").write_text(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
            "authorityKeyIdentifier=keyid:always\nsubjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
        openssl("x509", "-req", "-in", "board.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
                "-CAcreateserial", "-days", "1", "-extfile", "board.ext", "-out", "board.crt")

        requests = []

        class Board(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                authorized = self.headers.get("Authorization") == "Bearer test-token"
                self.send_response(200 if authorized else 401)
                self.end_headers()
                if authorized:
                    self.wfile.write(b'{"hosts": []}')

            def log_message(self, *_args):
                pass

        home = self.root / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / "data"), PYTHON=sys.executable)
        env.pop("LABMON_TOKEN", None)
        env.pop("LABMON_CLIENT", None)
        config = home / ".config/labmon/client.toml"
        cli = home / ".local/bin/labmon"
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.root / "board.crt", self.root / "board.key")
        with ThreadingHTTPServer(("127.0.0.1", 0), Board) as server:
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"https://127.0.0.1:{server.server_port}/"

                def install(pem):
                    name, script = dist.install_sh(url, pem, False, self.commit)
                    installer = self.root / name
                    installer.write_text(script)
                    return subprocess.run(["sh", str(installer)], env=env, capture_output=True, text=True, check=True).stdout

                def connect():
                    return subprocess.run([str(cli), "host", "list"], env=env, capture_output=True, text=True, timeout=25)

                def strict_connect():
                    python = cli.resolve().with_name("python")
                    command = """import ssl
from labmon.__main__ import main
default_context = ssl.create_default_context
def strict_context(*args, **kwargs):
    ctx = default_context(*args, **kwargs)
    ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    return ctx
ssl.create_default_context = strict_context
raise SystemExit(main(["host", "list"]))
"""
                    return subprocess.run([str(python), "-Ic", command], env=env, capture_output=True, text=True, timeout=25)

                hint = install(None)
                self.assertIn("未附 CA", hint)
                self.assertIn("向看板管理员领取", hint)
                self.assertIn(str(config), hint)
                self.assertNotIn("ca", tomllib.loads(config.read_text())["servers"][0])
                failed = connect()
                self.assertEqual(2, failed.returncode, failed.stderr)
                self.assertIn("CERTIFICATE_VERIFY_FAILED", failed.stderr)
                self.assertEqual([], requests)

                hint = install(None)
                self.assertIn(str(config.with_suffix(".toml.new")), hint)
                self.assertIn("合并", hint)
                config.unlink()
                pem = (self.root / "ca.crt").read_bytes()
                self.assertNotIn("未附 CA", install(pem))
                cert = tomllib.loads(config.read_text())["servers"][0]["ca"]
                saved = home / cert.removeprefix("~/")
                self.assertEqual(pem, saved.read_bytes())
                unauthenticated = connect()
                self.assertEqual(3, unauthenticated.returncode, unauthenticated.stderr)
                self.assertIn("labmon login", unauthenticated.stderr)
                self.assertEqual(["/api/hosts"], requests)
                config.with_name("client.token").write_text("test-token\n")
                authenticated = strict_connect()
                self.assertEqual(0, authenticated.returncode, authenticated.stderr)
                self.assertEqual(["/api/hosts"] * 2, requests)

                saved.write_bytes((self.root / "other.crt").read_bytes())
                failed = connect()
                self.assertEqual(2, failed.returncode, failed.stderr)
                self.assertIn("CERTIFICATE_VERIFY_FAILED", failed.stderr)
                self.assertEqual(["/api/hosts"] * 2, requests)
            finally:
                server.shutdown()
                thread.join()
