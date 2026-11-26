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
from fixtures import no_git_env  # noqa: E402
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
                # ka.sh 同一条连接写进去（#105）：内容换成固定的一句，不依赖本地 main 上有没有它（PR 合并前 CI 的 main 还没有）
                with mock.patch.object(workerctl, "committed", lambda name: sent.append((name, real(name) if name == "labmon_worker.py" else "#!/bin/sh\n# 假 ka.sh\n"))
                                       or sent[-1][1]), \
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
            sent = dict(sent)
            self.assertIn("ka.sh", sent)
            self.assertIn("labmon_worker.py", sent)
            self.assertEqual((d / "alice" / "labmon" / "labmon_worker.py").read_text(), sent["labmon_worker.py"])
            self.assertEqual((d / "alice" / "labmon" / "ka.sh").read_text(), sent["ka.sh"])   # 停启保活的脚本与 worker 一起写到 worker_home
            for rel, body in sent.items():
                if rel.startswith("worker/"):
                    self.assertEqual((d / "alice" / "labmon" / rel).read_text(), body)

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


class FitListTest(unittest.TestCase):
    def test_fit_list_passes_flag_and_hosts(self):
        # fit --list（#35 B）：--list 是不带值的开关，--hosts 逗号分隔；位置参数在 --host 前
        with mock.patch.object(workerctl, "ssh", return_value=(0, "")) as ssh, mock.patch.object(workerctl, "host_conf",
                return_value={"worker_home": "/worker", "python": "python3"}):
            workerctl.run_out("fit", "a", ["/root", "jobs/q.txt"], project="p", list=True, hosts="a,b")
        self.assertIn("fit /root jobs/q.txt --list --project p --hosts a,b --host a", ssh.call_args.args[1])


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
                self.assertEqual(calls[-1], (("add", "focus", "a1", ["b"], None, False), {"by": "boss", "expect_by": None}))   # #27：带 --host 的记谁下的
                self.assertNotIn("focus", K.sched())
                self.assertIn("[a] 生效中：…\n[b] 生效中：…\n", out.getvalue())
                with contextlib.redirect_stderr(io.StringIO()) as err:              # drain 要 admin 令牌：实验会话的 project 令牌做不了
                    self.assertEqual(M.main(["drain", "a"]), 3)
                self.assertIn("admin", err.getvalue())
                self.assertIn("labmon login --admin", err.getvalue())


class CommittedTest(unittest.TestCase):
    def test_takes_main_not_branch(self):
        """在分支的 worktree 里跑：部署取 main 上已提交的版本，不取分支上提交了的、也不取工作区改了的；路径相对包目录，不相对仓库根。"""
        no_git_env(self)
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

    def test_start_deploys_worker_package_from_main_and_prunes_stale(self):
        """#168：worker start 单条连接只打包部署 main 上已提交的 labmon_worker.py、worker/*.py 与 ka.sh；
        忽略分支提交与未提交改动，清理远端已删的旧模块与 __pycache__，脏工作区对 worker/*.py 有提示；解包失败不破坏已有文件。"""
        no_git_env(self)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            repo = d / "repo"
            pkg = repo / "labmon"
            (pkg / "worker").mkdir(parents=True)
            git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],   # noqa: E731
                                            capture_output=True, text=True, check=True)
            git("init", "-q", "-b", "main")
            (pkg / "labmon_worker.py").write_text("#!/usr/bin/env python3\n# main 入口\n")
            (pkg / "ka.sh").write_text("#!/bin/sh\n# main ka\n")
            (pkg / "worker" / "__init__.py").write_text("# main init\n")
            (pkg / "worker" / "sched.py").write_text("# main sched\n")
            git("add", "-A")
            git("commit", "-q", "-m", "main")
            git("checkout", "-q", "-b", "feat")
            (pkg / "worker" / "sched.py").write_text("# 分支 sched\n")
            (pkg / "worker" / "extra.py").write_text("# 分支新增\n")
            git("add", "-A")
            git("commit", "-q", "-m", "feat")
            (pkg / "worker" / "sched.py").write_text("# 未提交 sched\n")

            wh = d / "alice" / "labmon"
            (wh / "worker" / "__pycache__").mkdir(parents=True)
            (wh / "worker" / "obsolete.py").write_text("# 旧模块\n")
            (wh / "worker" / "__pycache__" / "sched.cpython-38.pyc").write_bytes(b"stale")
            py = d / "py"
            py.write_text('#!/bin/sh\n[ "$2" = ls ] && echo "worker：运行中 PID 1"\nexit 0\n')
            py.chmod(0o755)
            h = {"id": "here", "worker_home": str(wh), "python": str(py), "shared": []}
            ssh_calls = []
            real_ssh = workerctl.ssh
            with mock.patch.object(workerctl, "HERE", pkg), \
                    mock.patch.object(workerctl, "host_conf", return_value=h), \
                    mock.patch.object(workerctl.LS, "need"), \
                    mock.patch.object(workerctl, "START_CHECK_S", 0.05), \
                    mock.patch.object(workerctl, "ssh", lambda hh, cmd, stdin="", timeout=None: (ssh_calls.append(cmd), real_ssh(hh, cmd, stdin, timeout))[1]):
                rc, out = workerctl.run_out("start", "here", [])
            self.assertEqual(rc, 0, out)
            self.assertEqual(len(ssh_calls), 1)
            self.assertIn("工作区的 labmon_worker.py、worker/ 或 ka.sh 与 main 上的不同", out)
            self.assertEqual((wh / "labmon_worker.py").read_text(), "#!/usr/bin/env python3\n# main 入口\n")
            self.assertEqual((wh / "ka.sh").read_text(), "#!/bin/sh\n# main ka\n")
            self.assertEqual(sorted(p.name for p in (wh / "worker").glob("*.py")), ["__init__.py", "sched.py"])
            self.assertEqual((wh / "worker" / "sched.py").read_text(), "# main sched\n")
            self.assertFalse((wh / "worker" / "obsolete.py").exists())
            self.assertFalse((wh / "worker" / "__pycache__").exists())
            self.assertEqual(list(wh.glob(".deploy.*")), [])

            # 解包失败（传输截断/坏数据）：不覆盖已有文件，不留临时目录
            with mock.patch.object(workerctl, "HERE", pkg), \
                    mock.patch.object(workerctl, "host_conf", return_value=h), \
                    mock.patch.object(workerctl.LS, "need"), \
                    mock.patch.object(workerctl, "worker_bundle", return_value=("not-valid-gzip", ["__init__.py", "sched.py"])):
                rc_bad, _ = workerctl.run_out("start", "here", [])
            self.assertNotEqual(rc_bad, 0)
            self.assertEqual((wh / "worker" / "sched.py").read_text(), "# main sched\n")
            self.assertEqual(list(wh.glob(".deploy.*")), [])


if __name__ == "__main__":
    unittest.main()
