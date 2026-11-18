"""workerctl 的离线测试：拉起 worker（没有 ssh 的主机在本机跑同一段远端命令）、部署只取 main 上已提交的版本。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from labmon import workerctl  # noqa: E402


class StartTest(unittest.TestCase):
    def test_crash_on_start_is_failure(self):
        """worker 拉起后 START_CHECK_S 秒内就退出了（ls 说未运行）：退出码非 0，输出带 worker.log 末尾的出错位置。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            py = d / "py"                         # 假的 python：run 直接退出，ls 报未运行
            py.write_text('#!/bin/sh\n[ "$2" = ls ] && echo "worker：未运行（状态 2026-11-06 19:31:09）"\nexit 0\n')
            py.chmod(0o755)
            log = d / "alice" / "labmon" / "hosts" / "here" / "worker.log"
            log.parent.mkdir(parents=True)
            log.write_text("worker 启动 PID=1\nKeyError: 'stop'\n")
            (d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{d / "alice"}"\npython = "{py}"\n')
            old = os.environ.get("LABMON_HOSTS")
            os.environ["LABMON_HOSTS"] = str(d / "hosts.toml")
            # 记下 run_out 当时取到的 main 版本来比，不在拉起等的那几秒后再取一次：别的 agent 在主 checkout 里 pull（本地 main 各 worktree 共用）
            # 正好落在这几秒里，两次取到的就不同（11-08 整套跑时偶发失败一次，怀疑是它）
            sent = []
            real = workerctl.committed
            try:
                with mock.patch.object(workerctl, "committed", lambda name: sent.append((name, real(name))) or sent[-1][1]), \
                        mock.patch.object(workerctl, "START_CHECK_S", 0.3):      # 假 python 的 run 立刻退出，不用等 3 秒
                    rc, out = workerctl.run_out("start", "here", [])
            finally:
                if old is None:
                    os.environ.pop("LABMON_HOSTS")
                else:
                    os.environ["LABMON_HOSTS"] = old
            self.assertEqual(rc, 3)
            self.assertIn("worker：未运行", out)
            self.assertIn("KeyError: 'stop'", out)
            # worker 照样写进去了，是 main 上已提交的版本（工作区里改了一半的不传）
            self.assertEqual([n for n, _ in sent], ["labmon_worker.py"])
            self.assertEqual((d / "alice" / "labmon" / "labmon_worker.py").read_text(), sent[0][1])

    def test_worker_environment_uses_explicit_host_paths(self):
        """路径可含空格或 shell 字符，按 shell 参数转义。"""
        import shlex
        h = {"ka_bin": "/data/me/keep alive/$x"}
        args = shlex.split(workerctl.worker_env(h))
        self.assertEqual(args, ["KA_BIN=" + h["ka_bin"]])
        self.assertEqual(workerctl.worker_env({}), "")

    def test_start_receives_keepalive_path(self):
        h = {"worker_home": "/worker", "python": "python3", "shared": [], "ka_bin": "/shared/ka"}
        with mock.patch.object(workerctl, "host_conf", return_value=h), \
                mock.patch.object(workerctl.LS, "need"), \
                mock.patch.object(workerctl, "committed", return_value="worker"), \
                mock.patch.object(workerctl, "ssh", return_value=(0, "ok")) as ssh, \
                mock.patch.object(workerctl.subprocess, "run", return_value=mock.Mock(returncode=0)):
            workerctl.run_out("start", "demo", [])
        command = ssh.call_args.args[1]
        self.assertIn("setsid nohup env KA_BIN=/shared/ka ", command)

    def test_guest_inherits_explicit_deployment_paths(self):
        from labmon import config
        h = config.guest_host({"id": "guest", "ssh": "user@host", "holder": "Bob"},
                              [{"id": "a", "data_root": "/shared", "ka_bin": "/shared/ka"}])
        self.assertEqual((h["data_root"], h["ka_bin"]), ("/shared", "/shared/ka"))


class AddTest(unittest.TestCase):
    def test_refuses_outside_stop_path_before_ssh(self):
        with mock.patch.object(workerctl, "ssh") as ssh, mock.patch.object(workerctl, "host_conf",
                return_value={"worker_home": "/worker", "python": "python3"}):
            rc, out = workerctl.run_out("add", "test", ["/root", "q.txt"], stop="file:../outside")
        self.assertEqual(rc, 2)
        self.assertIn("清单目录内的相对路径", out)
        ssh.assert_not_called()


class OverrideCliTest(unittest.TestCase):
    def test_project_pause_focus_global_unless_host(self):
        """#96：labmon pause / focus <项目> 不带 --host 只改主机清单（所有主机，含之后加入的），不连服务器；
        带 --host 照旧只下给那几台；不带 --host 的 pause --now 另给各在用主机下“只停一次”（once），resume 一条撤干净。
        #162：经看板接口（POST /api/override/…），看板那边改主机清单、下指令；输出逐行照旧。"""
        import contextlib
        import io
        from fixtures import board
        from labmon import __main__ as M
        from labmon import config as K, projects
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            (d / "hosts.toml").write_text('[[hosts]]\nid = "a"\n\n[[hosts]]\nid = "b"\n')
            env = {"LABMON_HOSTS": str(d / "hosts.toml"), "LABMON_STATE": str(d / "state")}
            calls = []
            fake = lambda *a, **kw: calls.append((a, kw)) or [{"host": h, "rc": 0, "out": "生效中：…"} for h in a[3]]   # noqa: E731
            with mock.patch.dict(os.environ, env), mock.patch.object(workerctl, "override_each", fake), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                for pid in ("a1", "demo-b"):
                    projects.put_config(pid, f'id = "{pid}"\n')
                board(self)
                self.assertEqual(M.main(["pause", "demo-b"]), 0)
                self.assertEqual((K.sched().get("pause"), calls), ({"demo-b": None}, []))
                self.assertEqual(M.main(["pause", "demo-b", "--now", "--until", "+2h"]), 0)
                self.assertEqual(calls, [(("add", "pause", "demo-b", ["a", "b"], None, True), {"once": True})])
                self.assertIsNotNone(K.sched()["pause"]["demo-b"])                    # 到期写进主机清单的那条
                self.assertEqual(M.main(["resume", "demo-b"]), 0)
                self.assertNotIn("pause", K.sched())
                self.assertEqual(len(calls), 1)
                self.assertEqual(M.main(["focus", "a1", "--host", "b"]), 0)
                self.assertEqual(calls[-1], (("add", "focus", "a1", ["b"], None, False), {}))
                self.assertNotIn("focus", K.sched())
                self.assertIn("[a] 生效中：…\n[b] 生效中：…\n", out.getvalue())
                with contextlib.redirect_stderr(io.StringIO()) as err:              # drain 要 admin 令牌：实验会话的 project 令牌做不了
                    self.assertEqual(M.main(["drain", "a"]), 3)
                self.assertIn("admin", err.getvalue())
                self.assertIn("labmon login --admin", err.getvalue())


class CommittedTest(unittest.TestCase):
    def test_takes_main_not_branch(self):
        """在分支的 worktree 里跑：部署取 main 上已提交的版本，不取分支上提交了的、也不取工作区改了的；路径相对包目录，不相对仓库根。"""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            pkg = repo / "labmon"
            pkg.mkdir()
            git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],   # noqa: E731
                                            capture_output=True, text=True, check=True)
            git("init", "-q", "-b", "main")
            (pkg / "labmon_worker.py").write_text("main 上的\n")
            git("add", "-A")
            git("commit", "-q", "-m", "main")
            git("checkout", "-q", "-b", "feat")
            (pkg / "labmon_worker.py").write_text("分支上提交的\n")
            git("commit", "-q", "-am", "feat")
            (pkg / "labmon_worker.py").write_text("工作区改了一半的\n")
            with mock.patch.object(workerctl, "HERE", pkg):
                self.assertEqual(workerctl.committed("labmon_worker.py"), "main 上的\n")


if __name__ == "__main__":
    unittest.main()
