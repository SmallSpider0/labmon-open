"""服务托管的离线测试：launchd 与 systemd（#164）都不真调 launchctl、systemctl。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from labmon import service as S  # noqa: E402

_MGR = mock.patch.object(S, "MANAGER", "launchd")      # 下面 RestartTest 按 launchd 测（CI 在 Linux 上缺省是 systemd）


def setUpModule():
    _MGR.start()


def tearDownModule():
    _MGR.stop()


class RestartTest(unittest.TestCase):
    def run_with(self, bootstrap_rcs: list[int]) -> list[list[str]]:
        calls, rcs = [], iter(bootstrap_rcs)

        def fake(*args, check=False):
            calls.append(list(args))
            rc = next(rcs) if args[0] == "bootstrap" else 0
            return subprocess.CompletedProcess(["launchctl", *args], rc, "", "Bootstrap failed: 5: Input/output error")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(S, "PLIST", Path(d) / "x.plist"), mock.patch.object(S, "LOG", Path(d) / "x.log"), \
                mock.patch.object(S, "_launchctl", fake), mock.patch.object(S.time, "sleep"):
            S._write_and_restart({"port": 8765, "lan": False})
        return calls

    def test_retry_after_bootout_race(self):
        calls = self.run_with([5, 5, 0])                 # 旧进程没退完时 bootstrap 先失败两次
        self.assertEqual([c[0] for c in calls], ["bootout", "bootstrap", "bootstrap", "bootstrap"])

    def test_interval_in_plist(self):
        # service install --interval 写进 plist，再读回来；不给就不写
        import plistlib
        calls = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(S, "PLIST", Path(d) / "x.plist"), mock.patch.object(S, "LOG", Path(d) / "x.log"), \
                mock.patch.object(S, "_launchctl", lambda *a, check=False: calls.append(a) or subprocess.CompletedProcess(a, 0, "", "")):
            S._write_and_restart({"port": 8765, "lan": False, "interval": 45})
            args = plistlib.loads((Path(d) / "x.plist").read_bytes())["ProgramArguments"]
            self.assertEqual(args[args.index("--interval") + 1], "45")
            self.assertEqual(S._read()["interval"], 45)
            S._write_and_restart({"port": 8765, "lan": False, "interval": None})
            self.assertNotIn("--interval", plistlib.loads((Path(d) / "x.plist").read_bytes())["ProgramArguments"])

    def test_give_up(self):
        with self.assertRaises(SystemExit):
            self.run_with([5] * 10)


class SystemdTest(unittest.TestCase):
    """Linux 上用 systemd 用户单元托管（#164）：写单元、daemon-reload、enable、restart；参数原样读回；停掉删单元。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.calls = []
        self.rc = {}

        def fake(*args):
            self.calls.append(list(args))
            return subprocess.CompletedProcess(["systemctl", *args], self.rc.get(args[0], 0), "", "坏了")
        self.ps = [mock.patch.object(S, "MANAGER", "systemd"), mock.patch.object(S, "UNIT_FILE", d / "labmon.service"),
                   mock.patch.object(S, "LOG", d / "state" / "labmon.log"), mock.patch.object(S, "_systemctl", fake),
                   mock.patch.object(S, "linger_hint", lambda: None)]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in reversed(self.ps):
            p.stop()
        self.tmp.cleanup()

    def test_unit_round_trip(self):
        """端口不写死：--port 8443 与公网端口写进 ExecStart= 再读回；项目不在参数里（#161）。
        以前的单元带着 -c：带空格、%、$ 的配置路径原样读回成 legacy（可删，#161 迁移）。"""
        S._write_and_restart({"port": 8443, "public_port": 18443, "interval": 45})
        self.assertEqual(self.calls, [["daemon-reload"], ["enable", S.UNIT], ["restart", S.UNIT]])
        text = S.UNIT_FILE.read_text()
        self.assertIn("Restart=always", text)
        self.assertIn(f"StandardOutput=append:{S.LOG}", text)
        self.assertNotIn(" -c ", text)
        self.assertTrue(S.installed())
        self.assertEqual(S._read(), {"legacy": [], "port": 8443, "public_port": 18443, "interval": 45})
        cfgs = ["/home/a b/研究/monitor.toml", "/x/100%/$HOME/m.toml"]
        S.UNIT_FILE.write_text(S.unit_text(["py", "-m", "labmon", "serve", "--port", "8443", "-c", cfgs[0], "-c", cfgs[1]]))
        self.assertIn("100%%/$$HOME", S.UNIT_FILE.read_text())
        self.assertEqual(S._read(), {"legacy": cfgs, "port": 8443, "public_port": None, "interval": None})

    def test_quote_refused(self):
        with self.assertRaises(SystemExit):
            S.unit_text(['/a"b/python', "-m", "labmon", "serve"])

    def test_systemctl_failure_stops(self):
        self.rc["enable"] = 1
        with self.assertRaises(SystemExit) as c:
            S._write_and_restart({"port": 8443})
        self.assertIn("enable", str(c.exception))

    def test_restart_stop_and_later(self):
        S._write_and_restart({"port": 8443})
        self.calls.clear()
        self.assertEqual(S.restart(), 0)
        self.assertEqual(self.calls, [["restart", S.UNIT]])
        with mock.patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            S.restart_later(3)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["systemd-run", "--user", "--on-active=3"])
        self.assertEqual(argv[-4:], ["systemctl", "--user", "restart", S.UNIT])
        with mock.patch.object(S.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "no bus")), self.assertRaises(OSError):
            S.restart_later()
        self.calls.clear()
        S.stop()
        self.assertEqual(self.calls, [["disable", "--now", S.UNIT], ["daemon-reload"]])
        self.assertFalse(S.installed())


class RestartLaterLaunchdTest(unittest.TestCase):
    def test_detached_kickstart(self):
        """macOS：脱离本进程组的 sleep + kickstart -k（launchd 停服务只杀它的进程组）。"""
        with mock.patch.object(S.subprocess, "Popen") as po:
            S.restart_later(2)
        argv, kw = po.call_args
        self.assertIn("sleep 2; launchctl kickstart -k gui/", argv[0][2])
        self.assertTrue(kw["start_new_session"])
