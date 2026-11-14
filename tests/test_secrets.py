"""平台密码与登录令牌的存放（#164）：Linux 上是 600 权限的文件（LABMON_SECRETS 指到临时目录），macOS 上是钥匙串（打桩，不真调 security）。

    python -m unittest tests.test_secrets -v
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from labmon import platform as P  # noqa: E402


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "secrets"
        self.env = mock.patch.dict(os.environ, {"LABMON_SECRETS": str(self.dir)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_set_get_mode(self):
        """存成 <目录>/<种类>/<账号>，文件 600、目录 700；取回原文；删掉后没有。"""
        self.assertIsNone(P.secret("platform", "demo-tag"))
        P.set_secret("platform", "demo-tag", "口令 1")
        f = self.dir / "platform" / "demo-tag"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(f.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode), 0o700)
        self.assertEqual(P.secret("platform", "demo-tag"), "口令 1")
        self.assertEqual(P.password("demo-tag"), "口令 1")
        P.del_secret("platform", "demo-tag")
        self.assertIsNone(P.secret("platform", "demo-tag"))

    def test_missing_password_says_how(self):
        with self.assertRaises(P.PlatformError) as c:
            P.password("demo-tag")
        self.assertIn("labmon secret set platform", str(c.exception))

    def test_bad_names(self):
        for kind, user in (("other", "demo-tag"), ("platform", "../x"), ("platform", ".hidden"), ("platform", "")):
            with self.assertRaises(P.PlatformError):
                P.secret_file(kind, user)

    def test_session_round_trip(self):
        """登录令牌存成秘密（与密码同一处）：重启后读回；换了平台地址作废并删掉。"""
        cli = P.Client("http://scm.x")
        cli.token, cli.user_id = "a.eyJleHAiOiA5OTk5OTk5OTk5fQ.b", 7
        P.save_session(cli, "demo-tag", 1000)
        self.assertEqual(stat.S_IMODE((self.dir / "platform-token" / "demo-tag").stat().st_mode), 0o600)
        got = P.load_session("http://scm.x/", "demo-tag", now=2000)
        self.assertEqual((got[0].token, got[0].user_id, got[1]), (cli.token, 7, 1000))
        self.assertIsNone(P.load_session("http://other", "demo-tag", now=2000))
        self.assertIsNone(P.secret("platform-token", "demo-tag"))

    def test_prompt_twice(self):
        """labmon secret set：输两遍，对上了才存；不一样、没输都不存。"""
        answers = iter(["a", "b"])
        with self.assertRaises(P.PlatformError):
            P.prompt_secret("platform", "demo-tag", ask=lambda _: next(answers))
        self.assertIsNone(P.secret("platform", "demo-tag"))
        with self.assertRaises(P.PlatformError):
            P.prompt_secret("platform", "demo-tag", ask=lambda _: "")
        # 口令用路径（临时目录名）里不会出现的字样：曾用 "pw"，碰上 /tmp/tmpw… 就误判（#189）
        msg = P.prompt_secret("platform", "demo-tag", ask=lambda _: "口令-xyz")
        self.assertEqual(P.secret("platform", "demo-tag"), "口令-xyz")
        self.assertIn("600", msg)
        self.assertNotIn("口令-xyz", msg)

    def test_cli_set_and_rm(self):
        """labmon secret set|rm platform --user：存、删（删时连登录令牌一起删），原文不打印。"""
        from labmon import __main__ as M
        out = io.StringIO()
        with mock.patch.object(P.getpass, "getpass", return_value="秘密的口令"), redirect_stdout(out):
            self.assertEqual(M.main(["secret", "set", "platform", "--user", "demo-tag"]), 0)
        self.assertEqual(P.secret("platform", "demo-tag"), "秘密的口令")
        self.assertNotIn("秘密的口令", out.getvalue())
        P.set_secret("platform-token", "demo-tag", "{}")
        with redirect_stdout(out):
            self.assertEqual(M.main(["secret", "rm", "platform", "--user", "demo-tag"]), 0)
        self.assertIsNone(P.secret("platform", "demo-tag"))
        self.assertIsNone(P.secret("platform-token", "demo-tag"))


class OffTest(unittest.TestCase):
    def test_no_keychain_touches_nothing(self):
        """LABMON_NO_KEYCHAIN（测试）且没给 LABMON_SECRETS：钥匙串与 ~/.config/labmon/secrets 都不碰。"""
        env = {k: v for k, v in os.environ.items() if k != "LABMON_SECRETS"} | {"LABMON_NO_KEYCHAIN": "1"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(P.subprocess, "run") as run, \
                mock.patch.object(P, "write_atomic") as wa:
            self.assertIsNone(P.secret("platform", "demo-tag"))
            P.set_secret("platform", "demo-tag", "x")
            P.del_secret("platform", "demo-tag")
            with self.assertRaises(P.PlatformError):
                P.prompt_secret("platform", "demo-tag", ask=lambda _: "x")
        run.assert_not_called()
        wa.assert_not_called()


class KeychainTest(unittest.TestCase):
    def test_keychain_service_names(self):
        """macOS：沿用已有的钥匙串条目（服务名 labmon-platform、labmon-platform-token）；放密码交给 security 自己提示输入（-w 不带值）。"""
        env = {k: v for k, v in os.environ.items() if k not in ("LABMON_SECRETS", "LABMON_NO_KEYCHAIN")}
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "pw\n", "")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(P.sys, "platform", "darwin"), \
                mock.patch.object(P.subprocess, "run", fake):
            self.assertEqual(P.secret("platform", "demo-tag"), "pw")
            P.prompt_secret("platform", "demo-tag")
        self.assertEqual(calls[0], ["security", "find-generic-password", "-s", "labmon-platform", "-a", "demo-tag", "-w"])
        self.assertEqual(calls[1], ["security", "add-generic-password", "-U", "-s", "labmon-platform", "-a", "demo-tag", "-w"])


if __name__ == "__main__":
    unittest.main()
