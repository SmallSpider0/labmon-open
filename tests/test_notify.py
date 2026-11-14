"""通知推到手机的离线测试（#159）：service.notify 的推送后端（Bark、ntfy）打桩，不连真服务、不弹真通知。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import ROOT  # noqa: E402,F401

from labmon import push as P  # noqa: E402
from labmon import service as S  # noqa: E402
from labmon.config import ConfigError  # noqa: E402
from labmon import lease as LS  # noqa: E402
from labmon.__main__ import main  # noqa: E402

ENC_KEY = "0123456789abcdef"                         # 测试用的 16 位加密密钥（不是谁的真密钥）


class _Resp:
    def __init__(self, body: bytes = b'{"code":200,"message":"success"}', status: int = 200):
        self.body, self.status = body, status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NotifyBase(unittest.TestCase):
    """推送配置写在临时目录；去掉 LABMON_NO_NOTIFY；弹窗与 urlopen 打桩，记下发了什么。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmp.name) / "notify.toml"
        env = {k: v for k, v in os.environ.items() if k != "LABMON_NO_NOTIFY"}
        env["LABMON_NOTIFY_CONF"] = str(self.conf)
        self.popups, self.reqs, self.logs = [], [], []
        self.resp = lambda req, timeout: _Resp()
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(LS, "primary", return_value=True),
                  mock.patch.object(S, "_popup", lambda msgs: self.popups.extend(msgs)),
                  mock.patch.object(S, "log", lambda m: self.logs.append(m)),
                  mock.patch("urllib.request.urlopen", self._urlopen)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def _urlopen(self, req, timeout=None):
        self.reqs.append((req, timeout))
        return self.resp(req, timeout)

    def write(self, text: str, mode: int = 0o600) -> None:
        self.conf.write_text(text)
        os.chmod(self.conf, mode)

    def send(self, msgs):
        th = S.notify(msgs)
        if th is not None:
            th.join(5)
        return th


class NotifyTest(NotifyBase):
    def test_no_config_only_popup(self):
        # 没有 notify.toml：只弹窗（同 #159 之前），不推
        self.assertIsNone(self.send(["a 掉线"]))
        self.assertEqual(self.popups, ["a 掉线"])
        self.assertEqual(self.reqs, [])

    def test_no_notify_env_sends_nothing(self):
        # LABMON_NO_NOTIFY 一并管推送：弹窗、推送都不发
        self.write('push = "bark"\n[bark]\nkey = "K"\n')
        with mock.patch.dict(os.environ, {"LABMON_NO_NOTIFY": "1"}):
            self.assertIsNone(S.notify(["a 掉线"]))
        self.assertEqual((self.popups, self.reqs), ([], []))

    def test_empty_sends_nothing(self):
        self.write('push = "bark"\n[bark]\nkey = "K"\n')
        self.assertIsNone(S.notify([]))
        self.assertEqual((self.popups, self.reqs), ([], []))

    def test_bark_plain(self):
        self.write('push = "bark"\n[bark]\nkey = "DEVKEY"\n')
        self.send(["a 掉线", "b 恢复"])
        self.assertEqual(self.popups, ["a 掉线", "b 恢复"])             # 配了推送照样弹窗
        self.assertEqual(len(self.reqs), 2)                              # 一条消息一次推送
        req, timeout = self.reqs[0]
        self.assertEqual(req.full_url, "https://api.day.app/push")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, P.TIMEOUT_S)
        body = json.loads(req.data)
        self.assertEqual(body, {"device_key": "DEVKEY", "title": "labmon", "body": "a 掉线", "group": "labmon"})
        self.assertEqual(json.loads(self.reqs[1][0].data)["body"], "b 恢复")
        self.assertEqual(self.logs, [])

    def test_bark_server_override(self):
        self.write('push = "bark"\n[bark]\nkey = "K"\nserver = "https://bark.example.org/"\n')
        self.send(["x"])
        self.assertEqual(self.reqs[0][0].full_url, "https://bark.example.org/push")

    def test_bark_encrypted(self):
        # 推送加密：AES-128-CBC 加密参数 JSON，base64 后以表单 ciphertext + iv POST 到 /<设备 key>；用 openssl 解回来核对
        self.write(f'push = "bark"\n[bark]\nkey = "DEVKEY"\nencrypt_key = "{ENC_KEY}"\n')
        self.send(["租期剩 6 小时"])
        req = self.reqs[0][0]
        self.assertEqual(req.full_url, "https://api.day.app/DEVKEY")
        form = urllib.parse.parse_qs(req.data.decode())
        self.assertEqual(set(form), {"ciphertext", "iv"})
        iv = form["iv"][0]
        self.assertEqual(len(iv), 16)
        self.assertNotIn("租期", req.data.decode())
        plain = subprocess.run(["openssl", "enc", "-d", "-aes-128-cbc", "-K", ENC_KEY.encode().hex(), "-iv", iv.encode().hex()],
                               input=base64.b64decode(form["ciphertext"][0]), capture_output=True, check=True).stdout
        self.assertEqual(json.loads(plain), {"title": "labmon", "body": "租期剩 6 小时", "group": "labmon"})
        self.send(["再一条"])                                             # iv 每条随机
        self.assertNotEqual(urllib.parse.parse_qs(self.reqs[1][0].data.decode())["iv"][0], iv)

    def test_ntfy(self):
        self.write('push = "ntfy"\n[ntfy]\ntopic = "labmon-abc"\ntoken = "tk_test"\n')
        self.send(["共享盘快满"])
        req = self.reqs[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/")
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_test")
        self.assertEqual(json.loads(req.data), {"topic": "labmon-abc", "title": "labmon", "message": "共享盘快满"})

    def test_ntfy_self_hosted_no_token(self):
        self.write('push = "ntfy"\n[ntfy]\ntopic = "t"\nserver = "https://ntfy.example.org"\n')
        self.send(["x"])
        req = self.reqs[0][0]
        self.assertEqual(req.full_url, "https://ntfy.example.org/")
        self.assertIsNone(req.get_header("Authorization"))

    def test_slow_push_does_not_block(self):
        # 推送卡住（连不上、超时）：notify 马上返回，采集线程不等；之后失败只记一行日志
        gate = threading.Event()

        def stuck(req, timeout):
            gate.wait(5)
            raise TimeoutError("timed out")
        self.resp = stuck
        self.write('push = "ntfy"\n[ntfy]\ntopic = "t"\n')
        t0 = time.monotonic()
        th = S.notify(["a 掉线"])
        self.assertLess(time.monotonic() - t0, 1)
        self.assertTrue(th.is_alive())
        gate.set()
        th.join(5)
        self.assertEqual(len(self.reqs), 1)                              # 不重试
        self.assertEqual(len(self.logs), 1)
        self.assertIn("推送失败", self.logs[0])

    def test_failure_logged_no_retry(self):
        self.resp = lambda req, timeout: _Resp(b'{"code":400,"message":"failed to get device token"}')
        self.write('push = "bark"\n[bark]\nkey = "SECRETKEY"\n')
        self.send(["a", "b"])
        self.assertEqual(len(self.reqs), 2)                              # 每条一次，不重试
        self.assertEqual(len(self.logs), 2)
        self.assertIn("failed to get device token", self.logs[0])
        self.assertNotIn("SECRETKEY", "".join(self.logs))                 # 日志里不写密钥

    def test_bad_config_still_popup(self):
        # 配错：弹窗照弹、不推，记一行日志说错在哪
        for text, word in (('push = "slack"\n', "push"), ('push = "bark"\n', "key"), ('push = "ntfy"\n[ntfy]\n', "topic"),
                           ('push = "bark"\n[bark]\nkey = "K"\nencrypt_key = "short"\n', "encrypt_key"), ('push = \n', "notify.toml")):
            with self.subTest(text=text):
                self.popups.clear(), self.reqs.clear(), self.logs.clear()
                self.write(text)
                self.assertIsNone(self.send(["a"]))
                self.assertEqual((self.popups, self.reqs), (["a"], []))
                self.assertEqual(len(self.logs), 1)
                self.assertIn(word, self.logs[0])

    def test_no_push_key_only_popup(self):
        # 文件在、没写 push（比如两段都写好了还没选）：只弹窗
        self.write('[bark]\nkey = "K"\n')
        self.assertIsNone(self.send(["a"]))
        self.assertEqual((self.popups, self.reqs, self.logs), (["a"], [], []))


class PersonalNotifyTest(NotifyBase):
    def setUp(self):
        super().setUp()
        opener = mock.Mock()
        opener.open.side_effect = self._urlopen
        p = mock.patch.object(P.urllib.request, "build_opener", return_value=opener)
        self.build_opener = p.start()
        self.opener = opener
        self.addCleanup(p.stop)

    def test_personal_push_rejects_unknown_server_and_malformed_kind(self):
        for form in ({"push": {"bad": "secret"}},
                     {"push": "ntfy", "ntfy": {"topic": "secret", "server": "http://127.0.0.1:9000"}}):
            with self.assertRaises(ConfigError) as cm:
                S.set_user_push("amy", form)
            self.assertNotIn("secret", str(cm.exception))
        self.assertFalse(S.user_push_file("amy").exists())
        self.build_opener.assert_not_called()

    def test_personal_push_is_private_and_targeted(self):
        form = {"push": "ntfy", "ntfy": {"topic": "topicsecret", "token": "token-secret"}}
        S.set_user_push("amy", form)
        S.set_user_push("bob", {"push": "bark", "bark": {"key": "other"}})
        self.assertEqual(S.user_push_file("amy").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("topicsecret", str(S.push_view("amy")))
        self.assertNotIn("token-secret", str(S.push_view("amy")))
        S.notify(["一条消息"], to=["amy"]).join(5)
        self.assertEqual([r.full_url for r, _ in self.reqs], ["https://ntfy.sh/"])
        self.assertEqual(json.loads(self.reqs[0][0].data)["topic"], "topicsecret")
        self.assertEqual(self.popups, [])
        self.assertIsInstance(self.build_opener.call_args.args[0](), P.urllib.request.HTTPRedirectHandler)
        self.assertIsNone(self.build_opener.call_args.args[0]().redirect_request(None, None, None, None, None, None))

    def test_admin_target_is_not_pushed_twice(self):
        from labmon import users as U
        self.write('push = "bark"\n[bark]\nkey = "same"\n')
        S.set_user_push("boss", {"push": "bark", "bark": {"key": "same"}})
        with mock.patch.object(U, "admins", return_value=["boss"]):
            self.send(["一条消息"])
        self.assertEqual(len(self.reqs), 1)


class AudienceTest(NotifyBase):
    """只推给用户的那份（#142）：工作台一轮的 notices 推，只给实验会话的 wake 不推。"""

    def test_overview_once(self):
        from labmon import serve as SV
        self.write('push = "bark"\n[bark]\nkey = "K"\n')
        snap = {"notices": ["srv-a 掉线"], "wake": [{"key": "borrow:x", "msg": "借到卡了：给会话的", "hosts": ["g1"]}]}
        threads = []
        real = S.notify

        def spy(msgs):
            th = real(msgs)
            threads.append(th)
            return th
        with mock.patch.object(SV.O, "collect", lambda projects: snap), mock.patch.object(SV.O, "relaunch", lambda s: []), \
                mock.patch.object(SV, "notify", spy), mock.patch.object(SV, "log", lambda m: None):
            SV.Overview([]).once()
        for th in threads:
            if th is not None:
                th.join(5)
        self.assertEqual([json.loads(r.data)["body"] for r, _ in self.reqs], ["srv-a 掉线"])


class NotifyTestCommand(NotifyBase):
    """labmon notify test：照配置同步发一条试推，打印后端、目标（密钥打码）与结果。"""

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = main(["notify", "test", *args])
        return rc, out.getvalue()

    def test_ok(self):
        self.write('push = "bark"\n[bark]\nkey = "DEVKEY123456"\nencrypt_key = "' + ENC_KEY + '"\n')
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.reqs), 1)
        self.assertIn("Bark", out)
        self.assertIn("已发出", out)
        self.assertNotIn("DEVKEY123456", out)                           # 密钥打码
        self.assertNotIn(ENC_KEY, out)
        self.assertEqual(len(self.popups), 1)

    def test_custom_text(self):
        self.write('push = "ntfy"\n[ntfy]\ntopic = "t"\n')
        rc, out = self.run_cli("你好")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.reqs[0][0].data)["message"], "你好")

    def test_not_configured(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn(str(self.conf), out)
        self.assertEqual(self.reqs, [])

    def test_bad_config(self):
        self.write('push = "bark"\n')
        rc, out = self.run_cli()
        self.assertEqual(rc, 3)
        self.assertIn("key", out)

    def test_send_failed(self):
        def boom(req, timeout):
            raise OSError("connection refused")
        self.resp = boom
        self.write('push = "ntfy"\n[ntfy]\ntopic = "t"\n')
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("connection refused", out)

    def test_loose_permission_warned(self):
        self.write('push = "ntfy"\n[ntfy]\ntopic = "t"\n', mode=0o644)
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("chmod 600", out)

    def test_no_notify_env(self):
        self.write('push = "ntfy"\n[ntfy]\ntopic = "t"\n')
        with mock.patch.dict(os.environ, {"LABMON_NO_NOTIFY": "1"}):
            rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("LABMON_NO_NOTIFY", out)
        self.assertEqual(self.reqs, [])


if __name__ == "__main__":
    unittest.main()
