"""总控的常驻等待（tools/issue_watch.py，Issue #44）：gh 打桩，不连网。

    python -m unittest tests.test_issue_watch -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_P = Path(__file__).resolve().parent.parent / "tools" / "issue_watch.py"
_spec = importlib.util.spec_from_file_location("issue_watch", _P)
IW = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(IW)


def issue(n, title="t", labels=(), who="someone"):
    return {"number": n, "title": title, "labels": [{"name": l} for l in labels],
            "author": {"login": who}, "createdAt": "2026-11-09T00:00:00Z",
            "url": f"https://github.com/SmallSpider0/labmon-open/issues/{n}"}


class Stop(Exception):
    pass


class WaitTest(unittest.TestCase):
    """gh 每一轮的返回值按 rounds 给（列表或异常）；rounds 用完时 sleep 抛 Stop，表示一直没退出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "issue_watch.json"
        self.t = 1000.0

    def tearDown(self):
        self.tmp.cleanup()

    def run_wait(self, rounds):
        rounds = list(rounds)
        calls = []

        def gh():
            r = rounds.pop(0)
            calls.append(r)
            if callable(r):                    # 这一轮查的同时有人做了别的事（如 mark）
                r = r()
            if isinstance(r, Exception):
                raise r
            return r

        def sleep(s):
            if not rounds:
                raise Stop
            self.t += s

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(IW, "gh_issues", gh), mock.patch.object(IW, "_sleep", sleep), \
                mock.patch.object(IW, "_now", lambda: self.t), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = IW.wait(300, self.path)
            except Stop:
                rc = None
        return rc, out.getvalue(), err.getvalue(), len(calls)

    def test_reports_do_not_wait_for_github_or_repeat(self):
        token = self.path.with_name("reports.token")
        report = {"report_id": "R-" + "f" * 32, "reporter": "reporter", "t": 1000, "detail": "不回显"}
        with mock.patch.object(IW, "gh_issues", side_effect=RuntimeError("EOF")), \
                mock.patch.object(IW, "board_reports", return_value=[report]) as board, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(IW.wait(300, self.path, token), 0)
            self.assertIn(report["report_id"], out.getvalue())
            self.assertNotIn(report["detail"], out.getvalue())
            board.assert_called_once_with(token)
            with mock.patch.object(IW, "_sleep", side_effect=Stop), self.assertRaises(Stop):
                IW.wait(300, self.path, token)
        self.assertEqual(out.getvalue().count(report["report_id"]), 1)

    def test_first_run_reports_nothing(self):
        rc, out, err, n = self.run_wait([[issue(1), issue(2)], [issue(1), issue(2)]])
        self.assertIsNone(rc)                  # 一直等着
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(IW.load_seen(self.path), {1, 2})
        self.assertEqual(n, 2)

    def test_new_issue_exits_and_is_marked_seen(self):
        IW.save_seen(self.path, {1, 2})
        old = [issue(1), issue(2)]
        rc, out, err, n = self.run_wait([old, old, old + [issue(43, "估显存不看 tag", ["from-experiment"], "SmallSpider0")]])
        self.assertEqual(rc, 0)
        self.assertEqual(n, 3)
        self.assertIn("#43 [from-experiment] 估显存不看 tag — SmallSpider0 https://github.com/SmallSpider0/labmon-open/issues/43", out)
        self.assertNotIn("#2", out)
        self.assertEqual(IW.load_seen(self.path), {1, 2, 43})
        rc, out, _, _ = self.run_wait([old + [issue(43)]])     # 再挂上：已报过的不再报
        self.assertIsNone(rc)
        self.assertEqual(out, "")

    def test_gh_errors_retry_silently(self):
        IW.save_seen(self.path, {1})
        rc, out, err, n = self.run_wait([RuntimeError("EOF")] * 5 + [[issue(1)]])
        self.assertIsNone(rc)
        self.assertEqual((out, err), ("", ""))
        self.assertEqual(n, 6)

    def test_long_failure_says_one_line_and_keeps_waiting(self):
        IW.save_seen(self.path, {1})
        rounds = [RuntimeError("网络不通")] * 14 + [[issue(1), issue(5)]]   # 13 × 300 秒 = 65 分钟
        rc, out, err, n = self.run_wait(rounds)
        self.assertEqual(rc, 0)                # 恢复后照常报新的
        self.assertIn("#5", out)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("网络不通", err)

    def test_first_run_empty_repository_reports_first_issue(self):
        """新公开库初始无 Issue，第一条新增 Issue 仍会唤醒等待。"""
        rc, out, err, n = self.run_wait([[], [issue(1)]])
        self.assertEqual(rc, 0)
        self.assertIn("#1", out)
        self.assertEqual(err, "")
        self.assertEqual(IW.load_seen(self.path), {1})
        self.assertEqual(n, 2)

    def test_marked_meanwhile_not_reported_nor_overwritten(self):
        """wait 运行中别人 mark 的号：不报，存已见列表时也不把它冲掉（Issue #50）。"""
        IW.save_seen(self.path, {1})

        def mark(nums, rows):
            def f():
                self.assertEqual(IW.main(["--state", str(self.path), "mark", *map(str, nums)]), 0)
                return rows
            return f

        rounds = [[issue(1)],
                  mark([5], [issue(1)]),                       # 总控开了 #5 并 mark，这一轮 gh 还没看到它
                  [issue(1), issue(5)],                        # 下一轮看到了 #5：不报
                  mark([7], [issue(1), issue(5), issue(6)])]   # 报 #6 这一轮又有人 mark 了 #7：存的时候不冲掉
        rc, out, err, n = self.run_wait(rounds)
        self.assertEqual(rc, 0)
        self.assertEqual(n, 4)
        self.assertIn("#6", out)
        self.assertNotIn("#5", out)
        self.assertEqual(IW.load_seen(self.path), {1, 5, 6, 7})

    def test_closed_issue_is_marked(self):
        IW.save_seen(self.path, {1})
        rc, out, _, _ = self.run_wait([[issue(1), dict(issue(7), state="CLOSED"), dict(issue(8), state="OPEN")]])
        self.assertEqual(rc, 0)
        self.assertIn("#7（已关） [无标签]", out)
        self.assertIn("#8 [无标签]", out)

    def test_corrupt_state_counts_as_first_run(self):
        self.path.write_text("{not json")
        rc, out, _, _ = self.run_wait([[issue(1), issue(2)]])
        self.assertIsNone(rc)
        self.assertEqual(out, "")
        self.assertEqual(IW.load_seen(self.path), {1, 2})
        self.path.write_text(json.dumps({"seen": "x"}))
        self.assertIsNone(IW.load_seen(self.path))


class GhTest(unittest.TestCase):
    def test_gh_failures_raise_runtime_error(self):
        bad = [subprocess.CompletedProcess([], 1, "", "error connecting to api.github.com\n"),
               subprocess.CompletedProcess([], 0, "not json", ""),
               subprocess.TimeoutExpired("gh", 60), FileNotFoundError("gh")]
        for b in bad:
            run = mock.Mock(side_effect=b) if isinstance(b, Exception) else mock.Mock(return_value=b)
            with mock.patch.object(IW.subprocess, "run", run), self.assertRaises(RuntimeError):
                IW.gh_issues()

    def test_gh_ok(self):
        r = subprocess.CompletedProcess([], 0, json.dumps([issue(3)]), "")
        with mock.patch.object(IW.subprocess, "run", return_value=r):
            self.assertEqual([x["number"] for x in IW.gh_issues()], [3])


class StatePathTest(unittest.TestCase):
    def test_state_file_uses_labmon_state_root(self):
        """状态根目录只在 labmon.state 解析一处（AGENTS.md「一个概念只有一处实现」）。"""
        from labmon import state
        with mock.patch.object(state, "state_root", lambda: Path("/x/root")):
            self.assertEqual(IW.state_file(), Path("/x/root/issue_watch/smallspider0--labmon-open.json"))
        with mock.patch.dict("os.environ", {"LABMON_STATE": "/y"}):
            self.assertEqual(state.state_dir(), Path("/y/overview"))
            self.assertEqual(IW.state_file(), Path("/y/issue_watch/smallspider0--labmon-open.json"))

    def test_repositories_never_share_seen_numbers(self):
        with mock.patch.object(IW.state, "state_root", return_value=Path("/state")):
            with mock.patch.object(IW, "REPO", "owner/first"):
                first = IW.state_file()
            with mock.patch.object(IW, "REPO", "owner/second"):
                second = IW.state_file()
        self.assertNotEqual(first, second)


class CliTest(unittest.TestCase):
    def test_seen_and_forget(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "w.json")
            IW.save_seen(Path(p), {1, 2, 3})
            self.assertEqual(IW.main(["--state", p, "forget", "2"]), 0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                IW.main(["--state", p, "seen"])
            self.assertEqual(out.getvalue().strip(), "#1 #3")
            IW.main(["--state", p, "forget", "1", "3"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                IW.main(["--state", p, "seen"])
            self.assertEqual(out.getvalue().strip(), "已见列表是空的")

    def test_mark(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.json"
            IW.save_seen(p, {1, 2})
            self.assertEqual(IW.main(["--state", str(p), "mark", "48", "49", "2"]), 0)
            self.assertEqual(IW.load_seen(p), {1, 2, 48, 49})

    def test_mark_before_first_wait_writes_nothing(self):
        """还没有已见列表时 mark 不写：只写这几个号的话，首次 wait 会把其余现有 Issue 全当新的报。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.json"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(IW.main(["--state", str(p), "mark", "5"]), 0)
            self.assertIn("首次 wait", out.getvalue())
            self.assertFalse(p.exists())


if __name__ == "__main__":
    unittest.main()
