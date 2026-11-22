"""labmon wait --job（#135）：等某个任务跑完。判定在看板那边（waits.py：读项目快照、全局快照与工作台存的用量记录，不连服务器）；
命令行经 POST /api/wait 问（#162），CmdWaitJobTest 起一个临时看板服务。

    python -m unittest tests.test_wait_job -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import NO_HOSTS, board, kill_procs, make_queue, queue_procs, register, write_config  # noqa: E402

from labmon import __main__ as M  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import waits as W  # noqa: E402
from labmon.config import load  # noqa: E402
from labmon.state import append_row, save_json, state_dir, usage_file  # noqa: E402


def lane(lid, host, **kw):
    return {"id": lid, "label": f"车道 {lid}", "host": host, "state_dir": f"/r/{lid}/state", "logs_dir": f"/r/{lid}/logs", **kw}


def job(name, lid, status, **kw):
    return {"id": name, "lane": lid, "status": status, **kw}


def psnap(jobs, lanes, gen=None, **kw):
    return {"id": "p1", "generated": gen or time.time(), "alerts": [], "hosts": [{"id": "29", "label": "29（A6000）"}, {"id": "27", "label": "27"}],
            "lanes": lanes, "jobs": jobs, **kw}


class JobStateTest(unittest.TestCase):
    """job_state：按名字认快照里的任务（几条车道、几台主机都有同名的时还会跑的优先），不会再跑的认定，结果与位置一句话，快照里没有的看用量记录。"""

    def test_states(self):
        lanes = [lane("a", "29"), lane("b", "27", dep="failed", waiting="等的 x 跑失败了"), lane("c", "27", returned=1793776000)]
        now = time.time()
        js = W.job_state
        # 在跑
        s = js(psnap([job("x", "a", "running", eta_ts=now + 3600)], lanes), "x", [])
        self.assertEqual(s["st"], "running")
        self.assertIn("29（A6000） 车道 a", s["text"])
        # 一台跑完了、另一台的清单里还排着：还会跑的优先（worker 依赖判定同一口径）
        s = js(psnap([job("x", "a", "done", end_ts=now - 60), job("x", "c", "queued"), job("x", "a", "queued", plan_start_ts=now + 600)], lanes), "x", [])
        self.assertEqual(s["st"], "queued")
        self.assertIn("车道 a 排队，预计", s["text"])
        # 排着但卡在依赖上、或主机已归还：不会再跑
        s = js(psnap([job("y", "b", "queued")], lanes), "y", [])
        self.assertEqual(s["st"], "blocked")
        self.assertIn("卡在依赖上：等的 x 跑失败了", s["text"])
        s = js(psnap([job("y", "c", "queued")], lanes), "y", [])
        self.assertEqual(s["st"], "blocked")
        self.assertIn("已归还", s["text"])
        # 跑完：取最晚结束的那条，带日志位置；失败的写要删的 .failed
        s = js(psnap([job("z", "a", "failed", end_ts=now - 7200), job("z", "b", "done", end_ts=now - 60, actual_min=90)], lanes), "z", [])
        self.assertEqual(s["st"], "done")
        self.assertIn("用时 1.5 小时", s["text"])
        self.assertIn("日志 27:/r/b/logs/z.log", s["text"])
        s = js(psnap([job("z", "a", "failed", end_ts=now - 60, killed=137)], lanes), "z", [])
        self.assertEqual(s["st"], "failed")
        self.assertIn("被信号终止（退出码 137）", s["text"])
        self.assertIn("要重跑删掉 /r/a/state/z.failed", s["text"])
        # 快照里没有（车道收起了）：用量记录里最后一次，停下重排的不算
        rows = [("29", {"n": "w", "p": "p1", "st": "failed", "s": now - 9000, "t": now - 8000, "root": "/r", "queue": "q.txt"}),
                ("27", {"n": "w", "p": "p1", "st": "done", "s": now - 4000, "t": now - 3000, "root": "/r", "queue": "q.txt"}),
                ("27", {"n": "w", "p": "p1", "st": "stop", "s": now - 100, "t": now - 50, "root": "/r", "queue": "q.txt"})]
        s = js(psnap([], lanes), "w", rows)
        self.assertEqual(s["st"], "done")
        self.assertIn("27 上 /r 的 q.txt", s["text"])
        self.assertEqual(js(psnap([], lanes), "v", [])["st"], "missing")
        self.assertEqual(js(None, "v", [])["st"], "missing")

    def test_job_key(self):
        self.assertEqual(W.job_key("x", "p1"), ("p1", "x"))
        self.assertEqual(W.job_key("p2/x", "p1"), ("p2", "x"))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state"), "LABMON_HOSTS": NO_HOSTS, "LABMON_LAN": "on"})
        self.env.start()
        (self.d / "m.toml").write_text('id = "p1"\n')
        self.cfg, self.out = load(self.d / "m.toml"), self.d / "p1"
        self.out.mkdir()
        self.ov = self.d / "ov.json"
        save_json(self.ov, {"wake": []})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def put(self, jobs, gen=None, **kw):
        save_json(self.out / "snapshot.json", psnap(jobs, [lane("a", "29")], gen, **kw))



class JobWatchTest(_Base):
    def test_seen_running_then_done_is_immediate(self):
        self.put([job("x", "a", "running")])
        w = W.JobWatch(self.cfg, self.out, ["x"], self.ov)
        self.assertEqual(w.check()[("p1", "x")]["final"], False)
        self.put([job("x", "a", "done", end_ts=time.time())])
        st = w.check()[("p1", "x")]
        self.assertEqual((st["st"], st["final"]), ("done", True))

    def test_already_ended_or_missing_needs_two_fresh_snapshots(self):
        # 一开始就是跑完、找不到的：开始等之前写的快照不算，再要连着两份开始等之后写的才报（刚加的行、刚删掉 .done 的不被旧快照骗）
        self.put([job("x", "a", "done", end_ts=time.time() - 60)], gen=time.time() - 30)
        w = W.JobWatch(self.cfg, self.out, ["x", "nope"], self.ov)
        st = w.check()
        self.assertEqual([(s["st"], s["final"]) for s in st.values()], [("done", False), ("missing", False)])
        self.assertTrue(st[("p1", "x")]["text"].startswith("还在确认"))
        self.put([job("x", "a", "done", end_ts=time.time() - 60)], gen=time.time() + 1)
        self.assertEqual([s["final"] for s in w.check().values()], [False, False])
        self.assertEqual([s["final"] for s in w.check().values()], [False, False])      # 同一份快照读两遍只算一份
        self.put([job("x", "a", "done", end_ts=time.time() - 60)], gen=time.time() + 2, lan={"ok": False})
        self.assertEqual([s["final"] for s in w.check().values()], [False, False])      # 本机不在内网：数据是离开前的，不算
        self.put([job("x", "a", "done", end_ts=time.time() - 60)], gen=time.time() + 3)
        self.assertEqual([(s["st"], s["final"]) for s in w.check().values()], [("done", True), ("missing", True)])

    def test_stale_lane_not_evidence(self):
        # 主机探针失败、车道沿用上一次的数据（stale）：快照照样写新的 generated，但那份不算确认（Codex 审 #138）
        self.put([job("x", "a", "done", end_ts=time.time() - 60)], gen=time.time() - 30)
        w = W.JobWatch(self.cfg, self.out, ["x"], self.ov)
        w.check()
        for i in (1, 2, 3):
            save_json(self.out / "snapshot.json", psnap([job("x", "a", "done", end_ts=time.time() - 60)], [lane("a", "29", stale=True)], time.time() + i))
            self.assertFalse(w.check()[("p1", "x")]["final"])

    def test_old_usage_rows_ignored(self):
        # 用量记录兜底只看近 30 天（文件只在追加时修剪，没有新任务的主机上会留着更老的行）
        now = time.time()
        f = usage_file("29")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"t": now - 40 * 86400, "s": now - 41 * 86400, "n": "old", "p": "p1", "root": "/r", "queue": "q.txt", "st": "done"}) + "\n")
        self.put([], gen=now + 1)
        w = W.JobWatch(self.cfg, self.out, ["old"], self.ov)
        self.assertEqual(w.check()[("p1", "old")]["st"], "missing")

    def test_other_project_collector_stale(self):
        # 等别的项目的任务：那个项目的采集停了（快照太旧、没有快照）要报 collector_stale:<项目>，不无限等（Codex 审 #138）
        self.put([])
        other = self.d / "o"
        other.mkdir()
        register(write_config(other, self.d / "q9", self.d / "p9"))               # 别的项目按看板上的登记找，快照在登记的输出目录里（#161）
        save_json(PJ.out_dir("p9") / "snapshot.json", psnap([job("y", "a", "running")], [lane("a", "29")], time.time() - 3600) | {"id": "p9"})
        w = W.JobWatch(self.cfg, self.out, ["p9/y"], self.ov)
        w.check()
        self.assertIn("collector_stale:p9", w.stale)
        self.assertIn("p9", W.wait_check(self.cfg, self.out, self.d / "w.json", self.ov, w.stale)["collector_stale:p9"])

    def test_new_line_appears_before_confirmed(self):
        # 刚往清单里加的行：上一份快照里没有，下一轮出现了就接着等，不报找不到
        self.put([], gen=time.time() - 30)
        w = W.JobWatch(self.cfg, self.out, ["x"], self.ov)
        self.assertEqual(w.check()[("p1", "x")]["st"], "missing")
        self.put([job("x", "a", "queued")], gen=time.time() + 1)
        st = w.check()[("p1", "x")]
        self.assertEqual((st["st"], st["final"]), ("queued", False))

    def test_voided_and_cross_project(self):
        # 作废：开始等之后新出现的 void:（overview._voided 带 job）才算；别的项目读它自己的快照；不在看板上的项目当场报
        self.put([job("x", "a", "running")])
        old = O._voided([{"id": "29", "label": "29", "status": {"voided": [{"t": 1, "name": "x", "project": "p1", "why": "旧的", "how": "void"}]}}], 2)
        unrelated = {"key": "void:private", "job": ["private", "secret"], "msg": "别人项目"}
        save_json(self.ov, {"wake": [*old, unrelated]})
        other = self.d / "o"
        other.mkdir()
        register(write_config(other, self.d / "q9", self.d / "p9"))
        save_json(PJ.out_dir("p9") / "snapshot.json", psnap([job("y", "a", "done", end_ts=time.time())], [lane("a", "29")]) | {"id": "p9"})
        w = W.JobWatch(self.cfg, self.out, ["x", "p9/y", "zz/q"], self.ov)
        st = w.check()
        self.assertEqual(st[("p1", "x")]["st"], "running")                    # 开始等之前的作废不算
        self.assertEqual(w.state()["voids0"], [old[0]["key"]])
        self.assertEqual(st[("p9", "y")]["st"], "done")
        self.assertEqual((st[("zz", "q")]["st"], st[("zz", "q")]["final"]), ("missing", True))
        self.assertEqual(st[("zz", "q")]["text"], "看板上没有项目 zz")
        new = O._voided([{"id": "29", "label": "29（A6000）", "status": {"voided": [{"t": time.time(), "name": "x", "project": "p1", "why": "卡上来了别的进程", "how": "void"}]}}], time.time())
        save_json(self.ov, {"wake": old + new})
        st = w.check()[("p1", "x")]
        self.assertEqual((st["st"], st["final"]), ("voided", True))
        self.assertIn("卡上来了别的进程", st["text"])
        self.assertEqual(w.void_keys, {new[0]["key"]})


class CmdWaitJobTest(_Base):
    """cmd_wait 带 jobs（经临时看板服务的 /api/wait，#162）：告警照常唤醒（退出 1，另列还在等的任务）；任务都有结果时退出，全成功 0、其余 4；
    等的任务的 void: 只在任务那段写；等的任务的确认状态（watch）每轮带回看板。"""

    def setUp(self):
        super().setUp()
        register(self.d / "m.toml")
        self.out = PJ.out_dir("p1")
        self.out.mkdir(parents=True, exist_ok=True)
        board(self)

    def run_wait(self, jobs, steps):
        """steps：每轮问下来没有消息、命令行 sleep 时依次调一个，改快照或全局快照。看板那边不挂（HOLD_MAX 换成 0）。"""
        it = iter(steps)
        buf = io.StringIO()
        cli_time = mock.Mock(wraps=time)
        cli_time.sleep.side_effect = lambda s: next(it)()
        with mock.patch.object(M, "HOLD_MAX", 0), mock.patch.object(M, "time", cli_time), contextlib.redirect_stdout(buf):
            rc = M.cmd_wait(str(self.d / "m.toml"), jobs)
        return rc, buf.getvalue()

    def test_update_resumes_watch_without_replaying_first_poll(self):
        watch = {"t0": 12, "live": ["p1/x"]}
        commit = "b" * 40
        config = str(self.d / "m.toml")
        bin_path = self.d / commit / "bin" / "labmon"
        polls = [{"watch": watch, "commit": commit, "client_status": "outdated", "jobs": [{"name": "x", "text": "运行中"}]},
                 {"done": True, "jobs": [{"name": "x", "st": "done", "text": "完成"}]}]
        with mock.patch.object(M, "_sync", return_value={"act": "same"}), mock.patch.object(M.client, "call", side_effect=polls) as http, \
             mock.patch.object(M.dist, "installed", return_value=True), mock.patch.object(M.client, "install_update", return_value=(commit, M.dist.Installed(bin_path, bin_path, None, []))), \
             mock.patch.object(M.os, "execve", side_effect=InterruptedError) as execute, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(M.cmd_wait(config, ["x"]), 0)
            _, args, env = execute.call_args.args
            self.assertEqual(args[1:], ["wait", "-c", config, "--job=x"])
            self.assertEqual(http.call_args.args[2]["watch"], watch)
            self.assertNotEqual(http.call_args_list[0].args[2]["rid"], http.call_args.args[2]["rid"])
            with mock.patch.dict(os.environ, {M.WAIT_RESUME: env[M.WAIT_RESUME]}):
                self.assertEqual(M._wait_resume(config, ["x"])["watch"], watch)
                self.assertNotIn(M.WAIT_RESUME, os.environ)

    def test_skill_change_wakes_once_after_update(self):
        # #57：wait 换了版本时，上一版把自己那份 skill 带给新进程；新进程比出改了哪几节就当告警叫醒一次（退出 1），没改就接着等
        commit, config = "b" * 40, str(self.d / "m.toml")
        old, new = self.d / "old", self.d / "new"
        for pkg, text in ((old, "# 实验\n## 跑和盯\n挂 wait。\n"), (new, "# 实验\n## 跑和盯\n挂 wait，换版本后重读。\n")):
            (pkg / "skills/lab-experiments").mkdir(parents=True)
            (pkg / "skills/lab-experiments/SKILL.md").write_text(text)
        bin_path = self.d / commit / "bin" / "labmon"
        polls = [{"commit": commit, "client_status": "outdated"}, {"done": True, "jobs": []}]
        with mock.patch.object(M, "_sync", return_value={"act": "same"}), mock.patch.object(M.client, "call", side_effect=polls), \
             mock.patch.object(M.dist, "HERE", old), mock.patch.object(M.dist, "version", return_value="a" * 40), \
             mock.patch.object(M.dist, "installed", return_value=True), mock.patch.object(M.client, "install_update", return_value=(commit, M.dist.Installed(bin_path, bin_path, None, []))), \
             mock.patch.object(M.os, "execve", side_effect=InterruptedError) as execute, mock.patch.object(M.time, "sleep"), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            M.cmd_wait(config)                         # execve 打桩成失败：接着等到下一轮结束
        env = execute.call_args.args[2]
        self.assertEqual(json.loads(env[M.WAIT_RESUME])["skill"], [str(old / "skills/lab-experiments"), "a" * 40])
        for here, want_rc in ((new, 1), (old, 0)):
            polls = [{"done": True, "jobs": []}]
            with mock.patch.dict(os.environ, {M.WAIT_RESUME: env[M.WAIT_RESUME]}), mock.patch.object(M.dist, "HERE", here), \
                 mock.patch.object(M.dist, "version", return_value=commit), mock.patch.object(M, "_sync", return_value={"act": "same"}), \
                 mock.patch.object(M.client, "call", side_effect=polls) as http, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(M.cmd_wait(config), want_rc)
            if want_rc == 1:
                http.assert_not_called()               # 先叫醒会话：会话重挂 wait 后再问看板
                self.assertIn("[skill_updated] skill 已更新（aaaaaaa → bbbbbbb）：改了 SKILL.md「跑和盯」；先重挂 labmon wait", out.getvalue())
            else:
                self.assertNotIn("skill_updated", out.getvalue())

    def test_does_not_update_before_reporting_alert(self):
        poll = {"new": {"gpu_idle": "GPU 空着"}, "client_status": "outdated", "commit": "b" * 40}
        with mock.patch.object(M, "_sync", return_value={"act": "same"}), mock.patch.object(M.client, "call", return_value=poll), \
             mock.patch.object(M.dist, "installed", return_value=True), mock.patch.object(M, "_wait_update") as update, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.cmd_wait(str(self.d / "m.toml")), 1)
        update.assert_not_called()
        self.assertIn("[gpu_idle] GPU 空着", out.getvalue())

    def test_outdated_exit_after_sync_or_wait_refusal(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
             mock.patch.object(M.dist, "installed", return_value=False), mock.patch.object(M, "_wait_update") as update:
            for sync in ({"act": "error", "code": 426, "msg": "跑 labmon update"}, {"act": "same"}):
                with mock.patch.object(M, "_sync", return_value=sync), \
                     mock.patch.object(M.client, "call", side_effect=M.client.Failed(426, "跑 labmon update")):
                    self.assertEqual(M.cmd_wait(str(self.d / "m.toml")), 5)
        update.assert_not_called()                     # 不是看板安装的客户端：照旧退出，交给 labmon update
        self.assertEqual(err.getvalue().count("[client_outdated]"), 2)

    def test_outdated_refusal_tries_update_once(self):
        # #17：低于最低版本（426）时，看板安装的客户端也先自动更新一次（426 带在跑的提交就按它核对下载的版本），装不成再退出 5
        commit = "b" * 40
        cases = [({"act": "error", "code": 426, "msg": "跑 labmon update"}, None, None),
                 ({"act": "same"}, M.client.Failed(426, "跑 labmon update", {"commit": commit}), commit)]
        for sync, refusal, want in cases:
            with mock.patch.object(M, "_sync", return_value=sync), mock.patch.object(M.client, "call", side_effect=refusal), \
                 mock.patch.object(M.dist, "installed", return_value=True), mock.patch.object(M, "_wait_update", return_value="装不上") as update, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(M.cmd_wait(str(self.d / "m.toml")), 5)
            self.assertEqual(update.call_count, 1)
            self.assertEqual(update.call_args.args[0], want)
            self.assertIn("装不上", err.getvalue())
            self.assertIn("[client_outdated]", err.getvalue())

    def test_unknown_version_only_hints_once(self):
        # #17：看板认不出这个客户端的版本（unknown）：不拦、不自动换包，stderr 提示一次
        polls = [{"client_status": "unknown", "commit": "b" * 40, "jobs": [{"name": "x", "text": "运行中"}]}] * 2 + \
                [{"done": True, "client_status": "unknown", "jobs": [{"name": "x", "st": "done", "text": "完成"}]}]
        with mock.patch.object(M, "_sync", return_value={"act": "same"}), mock.patch.object(M.client, "call", side_effect=polls), \
             mock.patch.object(M.dist, "installed", return_value=True), mock.patch.object(M, "_wait_update") as update, \
             mock.patch.object(M.time, "sleep"), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(M.cmd_wait(str(self.d / "m.toml"), ["x"]), 0)
        update.assert_not_called()
        self.assertEqual(err.getvalue().count("认不出这个命令行的版本"), 1)

    def test_confirm_state_carried_across_requests(self):
        # 开始等之前就跑完的：要两份开始等之后写的快照；“见过几份”在命令行带回的 watch 里，看板不记会话（#162）
        self.put([job("x", "a", "done", end_ts=time.time() - 60)], gen=time.time() - 30)
        gens = iter([time.time() + 1, time.time() + 2])
        rc, text = self.run_wait(["x"], [lambda: self.put([job("x", "a", "done", end_ts=time.time() - 60)], gen=next(gens))] * 2)
        self.assertEqual(rc, 0)
        self.assertIn("  - x：还在确认", text)
        self.assertIn("等的任务都有结果了：\n  - x：成功", text)

    def test_done_ok_and_failed(self):
        self.put([job("x", "a", "running"), job("y", "a", "queued")])
        rc, text = self.run_wait(["x", "y"], [lambda: self.put([job("x", "a", "done", end_ts=time.time()), job("y", "a", "running")]),
                                          lambda: self.put([job("x", "a", "done", end_ts=time.time()), job("y", "a", "done", end_ts=time.time())])])
        self.assertEqual(rc, 0)
        self.assertIn("同时等 2 个任务跑完", text)
        self.assertIn("等的任务都有结果了", text)
        self.assertIn("  - y：成功", text)
        self.put([job("x", "a", "running")])
        rc, text = self.run_wait(["x"], [lambda: self.put([job("x", "a", "failed", end_ts=time.time())])])
        self.assertEqual(rc, M.JOB_BAD_RC)
        self.assertIn("  - x：失败", text)

    def test_alert_first_lists_jobs(self):
        self.put([job("x", "a", "running")], alerts=[{"key": "gpu_idle:29", "msg": "GPU 空着"}])
        rc, text = self.run_wait(["x"], [])
        self.assertEqual(rc, 1)
        self.assertIn("[gpu_idle:29] GPU 空着", text)
        self.assertIn("等的任务：\n  - x：在 29（A6000） 车道 a 上跑", text)

    def test_alert_and_done_same_round_returns_1(self):
        # 同一轮既有新告警、任务也都跑完：照旧退出 1（告警已记成报过，退出码不能让人以为不用处理）
        self.put([job("x", "a", "running")])
        rc, text = self.run_wait(["x"], [lambda: self.put([job("x", "a", "done", end_ts=time.time())], alerts=[{"key": "failed:z", "msg": "z 失败"}])])
        self.assertEqual(rc, 1)
        self.assertIn("[failed:z] z 失败", text)
        self.assertIn("等的任务都有结果了", text)

    def test_void_of_watched_job_not_listed_as_alert(self):
        self.put([job("x", "a", "running"), job("y", "a", "queued")])
        new = O._voided([{"id": "29", "label": "29", "status": {"voided": [{"t": time.time(), "name": "x", "project": "p1", "why": "被打扰", "how": "void"}]}}], time.time())

        def void():
            save_json(state_dir() / "snapshot.json", {"wake": new})
        rc, text = self.run_wait(["x", "y"], [void, lambda: self.put([job("x", "a", "queued"), job("y", "a", "done", end_ts=time.time())])])
        self.assertEqual(rc, M.JOB_BAD_RC)
        self.assertNotIn("新告警", text)                                         # 作废只在任务那段写一次，也不先单独唤醒
        self.assertIn("  - x：作废：", text)

    def test_session_and_take(self):
        # 每轮带会话标识（#142）；--take 只在第一轮带上；别的会话在等（409）时以 3 退出、打出看板的说法
        polls = [{"new": {}}, {"new": {"a": "一"}}]
        with mock.patch.object(M, "_sync", return_value={"act": "same"}), mock.patch.object(M.client, "call", side_effect=polls) as http, \
             mock.patch.object(M.client, "session_id", return_value="s1"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.cmd_wait(str(self.d / "m.toml"), take=True), 1)
        self.assertEqual([(c.args[2]["session"], c.args[2]["take"]) for c in http.call_args_list], [("s1", True), ("s1", False)])
        err = io.StringIO()
        with mock.patch.object(M, "_sync", return_value={"act": "same"}), \
             mock.patch.object(M.client, "call", side_effect=M.client.Failed(409, "项目 p1 已有另一个会话在挂 labmon wait")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(M.cmd_wait(str(self.d / "m.toml")), 3)
        self.assertIn("已有另一个会话在挂 labmon wait", err.getvalue())
        got = []
        with mock.patch.object(M, "cmd_wait", lambda cfg_path, jobs=None, take=False: got.append(take) or 0):
            M.main(["wait", "-c", str(self.d / "m.toml"), "--take"])
        self.assertEqual(got, [True])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            M.main(["check", "-c", str(self.d / "m.toml"), "--take"])

    def test_cli(self):
        # --job 可重复、也认逗号分隔（同 after=）；只用于 wait
        got = []
        with mock.patch.object(M, "cmd_wait", lambda cfg_path, jobs=None, take=False: got.append(jobs) or 0):
            M.main(["wait", "-c", str(self.d / "m.toml"), "--job", "x, y", "--job", "p9/z"])
        self.assertEqual(got, [["x", "y", "p9/z"]])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            M.main(["check", "-c", str(self.d / "m.toml"), "--job", "x"])


class CollectLogsDirTest(unittest.TestCase):
    """探针把清单 .env 的 QUEUE_LOGS 解析出的日志目录带到车道的 logs_dir；真采集一轮后 wait --job 的结果写出日志位置。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state"), "LABMON_HOSTS": NO_HOSTS, "LABMON_LAN": "on"})
        self.env.start()
        self.lock, self.cur = queue_procs()

    def tearDown(self):
        kill_procs(self.lock, self.cur)
        self.env.stop()
        self.tmp.cleanup()

    def test_logs_dir_and_result(self):
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, self.lock.pid, self.cur.pid)
        cfg = load(write_config(self.d, root, out))
        snap = C.collect(cfg)
        self.assertEqual(snap["lanes"][0]["logs_dir"], str(root / "logs"))
        w = W.JobWatch(cfg, out, ["a_run", "a_done"], self.d / "none.json")
        self.assertEqual([s["st"] for s in w.check().values()], ["running", "done"])
        (root / "state" / "a_run.done").write_text("")
        (root / "state" / "current").unlink()
        C.collect(cfg)
        st = w.check()
        self.assertEqual((st[("out", "a_run")]["st"], st[("out", "a_run")]["final"]), ("done", True))
        self.assertIn(f"日志 here:{root}/logs/a_run.log", st[("out", "a_run")]["text"])
        self.assertFalse(st[("out", "a_done")]["final"])                         # 开始等之前就跑完的：再要一份新快照
        C.collect(cfg)
        self.assertTrue(w.check()[("out", "a_done")]["final"])

    def test_usage_fallback(self):
        # 车道收起后快照里没有：看工作台存的用量记录
        (self.d / "m.toml").write_text('id = "p1"\n')
        cfg, out = load(self.d / "m.toml"), self.d / "p1"
        out.mkdir()
        now = time.time()
        save_json(out / "snapshot.json", psnap([], [], now + 1))
        append_row(usage_file("29"), {"t": now - 60, "s": now - 3600, "n": "old", "p": "p1", "root": "/r", "queue": "q.txt", "st": "done"}, 86400 * 30, now)
        w = W.JobWatch(cfg, out, ["old"], self.d / "none.json")
        st = w.check()[("p1", "old")]
        self.assertEqual(st["st"], "done")
        self.assertIn("29 上 /r 的 q.txt", st["text"])


if __name__ == "__main__":
    unittest.main()
