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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import no_git_env  # noqa: E402
from labmon import dist  # noqa: E402


SKILL_MD = """---
name: lab-experiments
---
# 实验
## 红线
不扫盘。
## 跑和盯
挂 wait。
```sh
# 不是标题
```
"""


class GitEnvTest(unittest.TestCase):
    def test_setup_ignores_inherited_git_env(self):
        """带着 GIT_DIR、GIT_WORK_TREE 跑整套测试（在 worktree 里 export 过）：建临时仓库的 git 不能改到那个仓库。诱饵是临时仓库，不拿真仓库试。"""
        with tempfile.TemporaryDirectory() as td:
            decoy = Path(td) / "decoy"
            decoy.mkdir()
            clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            git = lambda *a: subprocess.run(["git", "-C", str(decoy), "-c", "user.name=d", "-c", "user.email=d@x", *a],  # noqa: E731
                                            env=clean, capture_output=True, text=True, check=True).stdout.strip()
            git("init", "-q", "-b", "main")
            (decoy / "a.txt").write_text("诱饵\n")
            git("add", ".")
            git("commit", "-qm", "诱饵")
            (decoy / "untracked.txt").write_text("没跟踪\n")
            before = ((decoy / ".git/config").read_text(), git("rev-parse", "HEAD"), git("status", "--porcelain"))
            case = ClientDistTest("test_client_status_uses_history")
            with patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git"), "GIT_WORK_TREE": str(decoy)}):
                try:
                    case.setUp()
                    log = subprocess.run(["git", "-C", str(case.repo), "log", "--format=%s"], env=clean, capture_output=True, text=True).stdout
                    self.assertEqual(log.strip(), "初版")
                finally:
                    case.doCleanups()
            self.assertEqual(before, ((decoy / ".git/config").read_text(), git("rev-parse", "HEAD"), git("status", "--porcelain")))


class ClientDistTest(unittest.TestCase):
    def setUp(self):
        no_git_env(self)
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
        self.skill = self.repo / "skills/lab-experiments"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(SKILL_MD)
        (self.repo / "docs/sub").mkdir(parents=True)
        (self.repo / "docs/api.md").write_text("# 接口\n")
        (self.repo / "docs/intro.html").write_text("<p>页面</p>")
        (self.repo / "docs/sub/deep.md").write_text("# 子目录\n")
        (self.repo / "README.md").write_text("# labmon\n")
        (self.repo / "examples").mkdir()
        (self.repo / "examples/monitor.toml").write_text('title = "例子"\n')
        (self.repo / "skills/other").mkdir()
        (self.repo / "skills/other/SKILL.md").write_text("别的 skill\n")
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
            # #57：skill 与它链接到的文档照仓库的相对位置放进包目录（../../docs/… 打得开），别的 skill、网页、子目录不带
            names = set(z.namelist())
            self.assertLessEqual({"labmon/skills/lab-experiments/SKILL.md", "labmon/docs/api.md", "labmon/README.md",
                                  "labmon/examples/monitor.toml"}, names)
            self.assertFalse({"labmon/docs/intro.html", "labmon/docs/sub/deep.md", "labmon/skills/other/SKILL.md"} & names)
            self.assertEqual(SKILL_MD.encode(), z.read("labmon/skills/lab-experiments/SKILL.md"))
            self.assertIn("labmon/skills/lab-experiments/SKILL.md,sha256=", z.read(next(n for n in names if n.endswith("RECORD"))).decode())
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
        (home / ".claude").mkdir()                     # 装了 Claude Code：建 skill 入口
        codex = home / ".agents/skills/lab-experiments"
        codex.parent.mkdir(parents=True)
        codex.symlink_to(self.skill)                   # 指向仓库的软链：不覆盖、只提示
        name, script = dist.install_sh("https://tx.example/labmon/", b"private", True, self.commit)
        f = self.root / name
        f.write_text(script)
        out = subprocess.run(["sh", str(f)], env=env, capture_output=True, check=True, text=True).stdout
        claude = home / ".claude/skills/lab-experiments"
        entry = home / "data/labmon/skills/lab-experiments"
        self.assertEqual(entry, claude.readlink())
        self.assertEqual(SKILL_MD, (claude / "SKILL.md").read_text())
        self.assertTrue(str(entry.resolve()).startswith(str(home / "data/labmon/versions" / self.commit)))
        self.assertTrue((claude / "../../docs/api.md").resolve().is_file())   # SKILL 里的相对链接打得开
        self.assertEqual(self.skill, codex.readlink())
        self.assertIn(f"已把 skill 装到 {claude}", out)
        self.assertIn(f"{codex} 已有（指向 {self.skill}），没改", out)
        self.assertEqual("# 保留\n", (config / "client.toml").read_text())
        new_config = (config / "client.toml.new").read_text()
        self.assertIn('url = "https://tx.example/labmon"', new_config)
        self.assertIn("# cert =", new_config)
        self.assertNotIn("ca =", new_config)
        self.assertEqual("旧安装", old.read_text())
        self.assertTrue(fixed.is_symlink())
        original = fixed.resolve()

        (self.package / "next.txt").touch()
        (self.skill / "SKILL.md").write_text(SKILL_MD.replace("挂 wait。", "挂 wait，换版本后重读。"))
        (self.skill / "alerts.md").write_text("# 告警\n")
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
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
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
                self.assertEqual(SKILL_MD, (claude / "SKILL.md").read_text())    # 没装成：skill 也留在原来那版
                succeeded = update()
                self.assertEqual(0, succeeded.returncode, succeeded.stderr)
                self.assertIn("skill 已更新", succeeded.stdout)
                self.assertIn("改了 SKILL.md「跑和盯」、新增 alerts.md", succeeded.stdout)
                self.assertNotIn("「红线」", succeeded.stdout)
                self.assertIn("换版本后重读", (claude / "SKILL.md").read_text())
            finally:
                server.shutdown()
                thread.join()
        self.assertIn(next_commit, str(fixed.resolve()))
        self.assertIn(next_commit, str(entry.resolve()))
        self.assertEqual(entry, claude.readlink())
        self.assertTrue(original.is_file())

    def test_install_prunes_old_versions(self):
        # 装好新版、切换入口后删掉没有入口指着的旧版本目录（#18）；切换前那版（别的会话里还在跑的旧 wait 要跑完这一轮）与当前进程这版留着
        home, data = self.root / "home", self.root / "data"
        versions = data / "labmon/versions"
        def version(c, ready=True):
            venv = versions / c
            pkg = venv / "lib/python3.11/site-packages/labmon"
            pkg.mkdir(parents=True)
            (venv / "bin").mkdir()
            for f in ("python", "labmon"):
                (venv / "bin" / f).touch()
            if ready:
                (venv / "ready").touch()
            return venv, pkg
        old, mine, prev, new = "1" * 40, "2" * 40, "3" * 40, "4" * 40
        version(old)
        _, pkg = version(mine)
        version(prev)
        version(new)
        version("5" * 40, ready=False)                  # 装到一半断了的
        fixed = data / "labmon/bin/labmon"
        fixed.parent.mkdir(parents=True)
        fixed.symlink_to(versions / prev / "bin/labmon")
        with patch.dict(os.environ, {"HOME": str(home), "XDG_DATA_HOME": str(data)}), patch.object(dist, "HERE", pkg):
            dist.install_wheel(self.root / f"labmon-0.1.0+g{new[:7]}-py3-none-any.whl", new)
        self.assertEqual(versions / new / "bin/labmon", fixed.readlink())
        self.assertEqual(sorted(p.name for p in versions.iterdir()), [mine, prev, new])

    def test_auto_update_backs_off_after_failed_install(self):
        # 装不上（venv、pip 出错，装出来的版本不对）：自动更新（wait）同一个提交过一阵才再试、不先下载（#18；以前每 5 分钟重下一次）；
        # 手动 labmon update 照常试，装成了清掉标记
        from labmon import client
        data = self.root / "data"
        new = "4" * 40
        name = f"labmon-0.1.0+g{new[:7]}-py3-none-any.whl"
        got = []
        def download():
            got.append(1)
            return name, b"wheel", new
        broken = patch.object(dist, "install_wheel", side_effect=dist.Unavailable("安装后的客户端版本不对"))
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}), patch.object(client, "_download_wheel", download), \
                patch.object(dist, "version", lambda: "0" * 40):
            with broken, self.assertRaises(dist.Unavailable):
                client.install_update(new, auto=True)
            self.assertEqual(len(got), 1)
            with self.assertRaises(dist.Unavailable) as e:
                client.install_update(new, auto=True)
            self.assertEqual(len(got), 1)                                     # 没再下载
            self.assertIn("安装后的客户端版本不对", str(e.exception))
            self.assertIn("labmon update", str(e.exception))
            t0 = dist._failed()["t"]
            with broken, patch("labmon.dist.time.time", return_value=t0 + 3700), self.assertRaises(dist.Unavailable):
                client.install_update(new, auto=True)                       # 过了 1 小时再试一次，又没装成：下一次要等 2 小时
            self.assertEqual((len(got), dist._failed()["n"]), (2, 2))
            with patch("labmon.dist.time.time", return_value=t0 + 3700 + 3600), self.assertRaises(dist.Unavailable):
                client.install_update(new, auto=True)
            self.assertEqual(len(got), 2)
            ok = dist.Installed(Path("/x"), Path("/x"), None, [])
            with patch.object(dist, "install_wheel", return_value=ok):
                self.assertEqual(client.install_update(), (new, ok))            # 手动不看标记
            self.assertEqual(len(got), 3)
            self.assertIsNone(dist._failed())

    def test_skill_changes_by_section(self):
        old, new = self.root / "old", self.root / "new"
        for d in (old, new):
            d.mkdir()
        (old / "SKILL.md").write_text(SKILL_MD + "## 结束\n归档。\n")
        (new / "SKILL.md").write_text(SKILL_MD.replace("# 不是标题", "# 代码块里的改动") + "## 新节\n新的。\n")
        (old / "lists.md").write_text("# 清单\n")
        (new / "lists.md").write_text("# 清单\n")
        (old / "gone.md").write_text("# 删掉的\n")
        (new / "alerts.md").write_text("# 告警\n")
        self.assertEqual(["SKILL.md「跑和盯」「新节」（删了「结束」）", "新增 alerts.md", "删了 gone.md"], dist.skill_changes(old, new))
        self.assertEqual([], dist.skill_changes(old, old))
        note = dist.skill_note(["SKILL.md「跑和盯」"], "a" * 40, "b" * 40, "先重挂 labmon wait")
        self.assertIn("skill 已更新（aaaaaaa → bbbbbbb）：改了 SKILL.md「跑和盯」；先重挂 labmon wait，重读", note)

    def test_ensure_skill_only_for_current_version(self):
        # 旧版本的代码换上这一版时没建 skill 入口：这一版的命令补上；固定命令已指着别的版本时不动（skill 与命令不分家）
        home, data = self.root / "home", self.root / "data"
        (home / ".claude").mkdir(parents=True)
        def version(c):
            venv = data / "labmon/versions" / c
            pkg = venv / "lib/python3.11/site-packages/labmon"
            (pkg / "skills/lab-experiments").mkdir(parents=True)
            (pkg / "_build.py").write_text(f'COMMIT = "{c}"\n')
            (pkg / "skills/lab-experiments/SKILL.md").write_text(SKILL_MD)
            (venv / "bin").mkdir()
            (venv / "bin/labmon").touch()
            return venv, pkg
        mine, pkg = version("a" * 40)
        other, _ = version("b" * 40)
        fixed = data / "labmon/bin/labmon"
        fixed.parent.mkdir(parents=True)
        fixed.symlink_to(other / "bin/labmon")
        entry = data / "labmon/skills/lab-experiments"
        with patch.dict(os.environ, {"HOME": str(home), "XDG_DATA_HOME": str(data)}), patch.object(dist, "HERE", pkg):
            self.assertEqual([], dist.ensure_skill())
            self.assertFalse(os.path.lexists(entry))
            fixed.unlink()
            fixed.symlink_to(mine / "bin/labmon")
            self.assertEqual([f"已把 skill 装到 {home / '.claude/skills/lab-experiments'}（指向 {entry}，随命令行更新）"], dist.ensure_skill())
            self.assertEqual(pkg / "skills/lab-experiments", entry.readlink())
            self.assertEqual([], dist.ensure_skill())       # 第二次：都已就位，不再提示
            # 智能体的入口建不了（skills 是个文件）：只提示，固定入口照样切换，不让已切换好的命令被报成装失败
            (entry.parent / "x").mkdir()
            (entry.parent / "x/SKILL.md").write_text(SKILL_MD)
            claude = home / ".claude/skills"
            (claude / "lab-experiments").unlink()
            claude.rmdir()
            claude.write_text("")
            with patch.object(dist, "_agent_entries", lambda: [claude / "lab-experiments"]):
                notes = dist._link_skill(entry.parent / "x")
            self.assertEqual(entry.parent / "x", entry.readlink())
            self.assertEqual(1, len(notes))
            self.assertIn(f"{claude / 'lab-experiments'} 建不了", notes[0])

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
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
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
