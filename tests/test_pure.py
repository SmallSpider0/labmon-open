"""看板与 worker 共用纯模块（labmon/worker/pure.py）的离线测试（#168 第 2 步）。

    python -m unittest tests.test_pure -v
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from labmon import collect, config, fleet, overview, pool, state, users, waits, worker
from labmon.worker import pure as P


class PureBehaviorTest(unittest.TestCase):
    def test_file_sig_strict_and_change(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "a.txt"
            self.assertIsNone(P.file_sig(f))
            with self.assertRaises(OSError):
                P.file_sig(f, strict=True)
            f.write_text("one")
            s1 = P.file_sig(f)
            self.assertIsNotNone(s1)
            self.assertEqual(s1, P.file_sig(f, strict=True))
            f.write_text("one-two")
            self.assertNotEqual(s1, P.file_sig(f))

    def test_parse_jsonl_skips_corrupt_lines(self):
        text = '{"a": 1}\nnot-json\n{"b": "中文"}\n{"half":\n'
        self.assertEqual(P.parse_jsonl(text), [{"a": 1}, {"b": "中文"}])
        self.assertEqual(P.parse_jsonl(""), [])
        self.assertEqual(P.parse_jsonl(None), [])

    def test_list_path_and_same_list(self):
        self.assertEqual(P.list_path("/data/proj", "jobs/q.txt"), "/data/proj/jobs/q.txt")
        self.assertEqual(P.list_path("/data/proj/", "./a/../jobs/q.txt"), "/data/proj/jobs/q.txt")
        reg = {"root": "/data/proj", "jobs": "jobs/q.txt"}
        self.assertTrue(P.same_list(reg, "/data/proj", "./jobs/q.txt"))
        self.assertTrue(P.same_list(reg, "/data/proj/jobs", "q.txt"))
        self.assertFalse(P.same_list(reg, "/data/proj", "jobs/other.txt"))
        self.assertFalse(P.same_list({"root": "/data/proj"}, "/data/proj", "jobs/q.txt"))

    def test_is_hold_and_group_re(self):
        for n in ("hold", "hold_1", "x_hold2", "pause-a", "A.PAUSE"):
            self.assertTrue(P.is_hold(n), n)
        for n in ("threshold", "household", "unpaused", "train_s1", "", None):
            self.assertFalse(P.is_hold(n), n)
        self.assertEqual(P.GROUP_RE.match("exp1_s0").group(1), "exp1")
        self.assertEqual(P.GROUP_RE.match("vit-b-1").group(1), "vit")
        self.assertIsNone(P.GROUP_RE.match("plain"))

    def test_job_key(self):
        self.assertEqual(P.job_key("run1", "p1"), ("p1", "run1"))
        self.assertEqual(P.job_key("p2/run1", "p1"), ("p2", "run1"))
        self.assertEqual(P.job_key("p2/sub/run1", "p1"), ("p2", "sub/run1"))

    def test_overrun_left(self):
        self.assertEqual(P.overrun_left(0), P.OVER_MIN_S)
        self.assertEqual(P.overrun_left(1000), P.OVER_MIN_S)
        self.assertAlmostEqual(P.overrun_left(5000), 5000 * P.OVER_FRAC)

    def test_person_use(self):
        pol = {"own": {"a": "alice", "b": "alice", "c": "bob"}}
        use = {"a": 1.5, "b": 2.0, "c": 0.5, "d": 3.0}
        self.assertEqual(P.person_use(pol, use), {"alice": 3.5, "bob": 0.5, None: 3.0})
        self.assertEqual(P.person_use({}, {"a": 2.0}), {None: 2.0})

    def test_mem_unevictable(self):
        self.assertEqual(P.mem_unevictable(100, 30, 20), 50)
        self.assertEqual(P.mem_unevictable(40, 30, 20), 0)

    def test_proc_tree_filters_missing_and_handles_cycles(self):
        procs = {
            1: {"ppid": 0},
            10: {"ppid": 1},
            11: {"ppid": 10},
            12: {"ppid": 11},
            20: {"ppid": 21},
            21: {"ppid": 20},
        }
        self.assertEqual(set(P.proc_tree(procs, [10, 999])), {10, 11, 12})
        self.assertEqual(set(P.proc_tree(procs, [20])), {20, 21})

    def test_script_arg(self):
        self.assertEqual(P.script_arg(["python3", "-W", "ignore", "./d/../a.py", "--x"], "/w"), ("/w/a.py", 4))
        self.assertEqual(P.script_arg(["python", "-uc", "print(1)"], "/w"), ("-c", 2))
        self.assertEqual(P.script_arg(["python", "-m", "pkg.mod"], "/w"), ("-m pkg.mod", 3))
        self.assertEqual(P.script_arg(["python", "--", "a.py"], "/w"), ("/w/a.py", 3))
        self.assertEqual(P.script_arg(["bash", "-o", "errexit", "run.sh"], "/w"), ("/w/run.sh", 4))
        self.assertEqual(P.script_arg(["bash", "-s"], "/w"), ("-s", 2))
        self.assertIsNone(P.script_arg(["python"], "/w"))
        self.assertIsNone(P.script_arg(["/usr/bin/sleep", "10"], "/w"))


class SingleSourceIdentityTest(unittest.TestCase):
    def test_board_and_worker_share_identical_objects(self):
        """看板与 worker 均直接复用 labmon.worker.pure 的唯一实现。"""
        self.assertIs(state.file_sig, P.file_sig)
        self.assertIs(state.parse_rows, P.parse_jsonl)
        self.assertIs(config.GIB, P.GIB)
        self.assertIs(config.STALE_S, P.STALE_S)
        self.assertIs(config.POOL, P.POOL)
        self.assertIs(config.HOLD_RE, P.HOLD_RE)
        self.assertIs(config.AUTO_GROUP_RE, P.GROUP_RE)
        self.assertIs(collect.overrun_left, P.overrun_left)
        self.assertIs(collect.list_path, P.list_path)
        self.assertIs(collect.is_hold, P.is_hold)
        self.assertIs(users.same_list, P.same_list)
        self.assertIs(waits.job_key, P.job_key)
        self.assertIs(overview.person_use, P.person_use)
        self.assertIs(pool.person_use, P.person_use)
        self.assertIs(fleet.mem_unevictable, P.mem_unevictable)
        self.assertIs(fleet.proc_tree, P.proc_tree)
        self.assertIs(fleet.script_arg, P.script_arg)
        self.assertIs(worker.HOLD_RE, P.HOLD_RE)
        self.assertIs(worker.GROUP_RE, P.GROUP_RE)
        self.assertIs(worker.overrun_left, P.overrun_left)
        self.assertIs(worker.mem_unevictable, P.mem_unevictable)
        self.assertIs(worker.proc_tree, P.proc_tree)
        self.assertIs(worker.person_use, P.person_use)
        self.assertIs(worker.script_arg, P.script_arg)
        self.assertEqual(worker.STATUS_STALE_S, P.STALE_S)

    def test_standalone_worker_import_has_no_labmon_dependency(self):
        """模拟服务器上脱离 labmon 包直接导入 labmon_worker 与 worker.pure，不导入 labmon 其余模块。"""
        pkg_dir = Path(__file__).resolve().parent.parent / "labmon"
        code = (
            "import sys; "
            f"sys.path.insert(0, {str(pkg_dir)!r}); "
            "import labmon_worker, worker, worker.pure as p; "
            "assert 'labmon' not in sys.modules; "
            "assert labmon_worker.overrun_left is p.overrun_left; "
            "assert worker.person_use is p.person_use"
        )
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
