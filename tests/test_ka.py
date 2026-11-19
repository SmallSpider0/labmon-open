"""labmon/ka.sh（停启 GPU 保活，#105）的测试：几种起法下 running、stop、start 的退出码与进程组；认保活与发信号同一套规则；
失败的原因写到标准错误；与 worker 的 ka_owned 结论一致（两份：worker 在服务器上跑、不 import 本包，ka.sh 是 shell）。

用真进程（一个只 sleep 的 python 脚本当保活）、真 ps 与 /proc：只在 Linux 上跑（macOS 没有 /proc、flock、setsid、realpath -m；
CI 在 Linux 上跑）。脚本路径都在临时目录里，ps 扫到的别的进程对不上，不会误停。

    python -m unittest tests.test_ka -v
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import worker_module  # noqa: E402

KA = Path(__file__).resolve().parent.parent / "labmon" / "ka.sh"
# 假保活：起一个子进程（DataLoader 那样 fork 出来，命令行与工作目录同父进程），父子都 sleep；TERM 照常退出
KEEP = """import os, signal, sys, time
if os.environ.get("KA_CHILD") and os.fork() == 0:
    pass
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
while True:
    time.sleep(0.2)
"""


@unittest.skipUnless(sys.platform.startswith("linux"), "ka.sh 读 /proc/<pid>/cwd、用 flock 与 setsid：只在 Linux 上跑")
class KaTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = d = Path(self.td.name).resolve()
        (d / "kad").mkdir()
        (d / "kad" / "train_keep_alive.py").write_text(KEEP)
        (d / "root").mkdir()
        (d / "ka.env").write_text(f'AGRI_ROOT="{d / "root"}"\nAGRI_KA_DIR="{d / "kad"}"\nAGRI_KA_SCRIPT="train_keep_alive.py"\nAGRI_KA_PY="{sys.executable}"\n')
        self.procs = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
        out = self.ka("running").stdout.split()     # ka.sh start 起的
        for g in out:
            try:
                os.killpg(int(g), signal.SIGKILL)
            except OSError:
                pass
        self.td.cleanup()

    def ka(self, action, **env):
        return subprocess.run(["bash", str(KA), action], capture_output=True, text=True, timeout=90,
                              env=dict(os.environ, KA_ENV=str(self.d / "ka.env"), KA_CALLER="test", **env))

    def launch(self, args, cwd, child=False):
        """自己起一个保活（setsid：自成进程组），等它真起来；返回 Popen。"""
        p = subprocess.Popen(args, cwd=cwd, start_new_session=True, env=dict(os.environ, **({"KA_CHILD": "1"} if child else {})))
        self.procs.append(p)
        for _ in range(50):
            if Path(f"/proc/{p.pid}/cmdline").exists() and Path(f"/proc/{p.pid}/cmdline").read_bytes():
                break
            time.sleep(0.05)
        time.sleep(0.2)
        return p

    def gone(self, pid, s=5.0):
        end = time.time() + s
        while time.time() < end:
            try:
                st = Path(f"/proc/{pid}/stat").read_text()
                if st.rsplit(")", 1)[1].split()[0] == "Z":
                    return True
            except OSError:
                return True
            time.sleep(0.05)
        return False

    def ways(self):
        """几种起法：(说明, 命令行, 工作目录)。都跑同一个保活脚本，只有最后一种不是它。"""
        d, py = self.d, sys.executable
        (d / "link").symlink_to(d / "kad")
        (d / "other").mkdir()
        (d / "other" / "train_keep_alive.py").write_text(KEEP)
        return [("完整路径", [py, str(d / "kad" / "train_keep_alive.py")], d, True),
                ("相对路径", [py, "train_keep_alive.py"], d / "kad", True),
                ("./", [py, "./train_keep_alive.py"], d / "kad", True),
                ("..", [py, "../kad/train_keep_alive.py"], d / "root", True),
                ("经符号链接", [py, "train_keep_alive.py"], d / "link", True),
                ("带选项", [py, "-u", "-W", "ignore", "train_keep_alive.py"], d / "kad", True),
                ("同名的别的脚本", [py, "train_keep_alive.py"], d / "other", False)]

    def test_ways_running_stop(self):
        """#102 的根因：相对路径起的保活 running 认得、stop 却一个信号都没发（退出 1）。现在几种起法都认、都停得掉，
        DataLoader 那样 fork 出来的子进程一起退；不是这个脚本的不认、不动。"""
        w = worker_module(self.d)
        ka_full = str(self.d / "kad" / "train_keep_alive.py")
        for name, args, cwd, ours in self.ways():
            with self.subTest(name):
                p = self.launch(args, cwd, child=True)
                r = self.ka("running")
                self.assertEqual((r.returncode, r.stdout.split()), (0, [str(p.pid)]) if ours else (1, []))
                # 与 worker 的 ka_owned 同一个结论（worker 照它判 ka.sh 停不停得了：#29）
                cmd = Path(f"/proc/{p.pid}/cmdline").read_bytes().decode().split("\0")[:-1]
                self.assertEqual(w.ka_owned(cmd, os.readlink(f"/proc/{p.pid}/cwd"), ka_full), ours)
                r = self.ka("stop")
                self.assertEqual(r.returncode, 0, r.stderr)
                if ours:
                    self.assertTrue(self.gone(p.pid), "保活没停掉")
                    self.assertEqual(self.ka("running").returncode, 1)
                    self.assertIn(f"STOPPED 进程组 {p.pid}；调用者 test", (self.d / "root" / "keepalive" / "events.log").read_text())
                else:
                    self.assertFalse(self.gone(p.pid, 0.5), "别的脚本被停了")
                    os.killpg(p.pid, signal.SIGKILL)
                p.wait()

    def test_mixed_group_only_matched(self):
        """同一进程组里还有别的程序（如包着保活的 shell）：只给认出来的保活进程发信号，组里别的进程不动。"""
        sh = subprocess.Popen(["bash", "-c", f'"{sys.executable}" train_keep_alive.py & sleep 30; wait'], cwd=self.d / "kad", start_new_session=True)
        self.procs.append(sh)
        for _ in range(50):
            r = self.ka("running")
            if r.returncode == 0:
                break
            time.sleep(0.1)
        self.assertEqual(r.stdout.split(), [str(sh.pid)])
        self.assertEqual(self.ka("stop").returncode, 0)
        self.assertEqual(self.ka("running").returncode, 1)
        self.assertFalse(self.gone(sh.pid, 0.5), "同组的 shell 被停了")

    def test_start_logs_birth_and_kill_by_name(self):
        """start：起成 0、命令行里带 train_keep_alive.py（组里有人 pkill -f train_keep_alive.py 照样匹配），RESTORED 一行带出生时钟；
        已在跑再 start 不另起。被 pkill 停掉后 running 退出 1（就是没了），stop 退出 0。"""
        r = self.ka("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        g = self.ka("running").stdout.split()
        self.assertEqual(len(g), 1)
        args = Path(f"/proc/{g[0]}/cmdline").read_bytes().decode()
        self.assertIn("train_keep_alive.py", args)
        born = Path(f"/proc/{g[0]}/stat").read_text().rsplit(")", 1)[1].split()[19]
        self.assertIn(f"RESTORED 进程组 {g[0]} 出生 {born}；调用者 test", (self.d / "root" / "keepalive" / "events.log").read_text())
        self.assertEqual(self.ka("start").returncode, 0)
        self.assertEqual(self.ka("running").stdout.split(), g)
        subprocess.run(["pkill", "-f", str(self.d / "kad" / "train_keep_alive.py")])
        self.assertTrue(self.gone(int(g[0])))
        self.assertEqual(self.ka("running").returncode, 1)
        self.assertEqual(self.ka("stop").returncode, 0)

    def test_failures_to_stderr(self):
        """失败的原因写到标准错误，退出码按契约（docs/fleet.md）：拿不到锁 2；启动后没见到进程 1；不认识的动作 2。"""
        root = str(self.d / "root")
        lock = "/tmp/agri-ka-%s.lock" % subprocess.run(["bash", "-c", 'echo -n "$1" | cksum | cut -d" " -f1', "_", root],
                                                       capture_output=True, text=True).stdout.strip()
        self.addCleanup(lambda: os.path.exists(lock) and os.remove(lock))
        with open(lock, "a") as f:                  # 别的调用者正持着锁（flock(1) 与 fcntl.flock 是同一种锁）
            fcntl.flock(f, fcntl.LOCK_EX)
            for action, ev in (("stop", "STOP_FAILED"), ("start", "RESTORE_FAILED")):
                r = self.ka(action, KA_LOCK_S="1")
                self.assertEqual(r.returncode, 2)
                self.assertIn(f"{ev} 拿不到锁", r.stderr)
        (self.d / "ka.env").write_text((self.d / "ka.env").read_text().replace(f'AGRI_KA_PY="{sys.executable}"', 'AGRI_KA_PY="/bin/false"'))
        r = self.ka("start", KA_START_S="2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("RESTORE_FAILED 启动后 2 s 内未见进程", r.stderr)
        self.assertEqual(self.ka("nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()
