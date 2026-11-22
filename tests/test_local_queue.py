"""离线端到端测试：在临时目录里搭一个假的 gpu_queue 队列，用本机主机（不经 ssh）跑一次完整采集。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import dead_pid, kill_procs, make_queue, queue_procs, write_config  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon.config import ConfigError, load, parse_time  # noqa: E402


class LocalQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state")})   # 输出目录在状态根的登记目录下（#161），每个用例一份
        self.env.start()
        self.lock, self.cur = queue_procs()

    def tearDown(self):
        kill_procs(self.lock, self.cur)
        self.env.stop()
        self.tmp.cleanup()

    def run_collect(self, lock_pid, host_extra=""):
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, lock_pid, self.cur.pid)
        cfg = load(write_config(self.d, root, out, host_extra))
        return C.collect(cfg), out

    def test_worker_durations(self):
        # 预计时长由 worker 的 duration 算、排队任务的预计开始与结束由 worker 推演，经探针带到看板；计划项列出各类任务的 实际/估时
        wh = self.d / "wh"
        st = wh / "hosts" / "here" / "status.json"
        st.parent.mkdir(parents=True)
        now, root = time.time(), str(self.d / "proj")
        st.write_text(json.dumps({"t": now, "waiting": [], "queues": [{"root": root, "jobs": "jobs/q.txt", "dur": {
            "b_next": {"min": 12.0, "src": "同一清单近 3 个 实际/估时 ×0.60", "k": 0.6, "cls": "b_next"}}}],
            "plan": {"t": now, "lanes": [{"root": root, "queue": "jobs/q.txt", "project": "out", "kind": "queue", "stuck": "推演到 7 天后还没轮到",
                                          "jobs": [{"name": "a_run", "t0": now - 600, "t1": now + 600, "gpus": [0], "run": True},
                                                   {"name": "b_next", "t0": now + 1200, "t1": now + 1920, "gpus": [0], "after": [["other", "x1"]]}]}]}}))
        snap, _ = self.run_collect(self.lock.pid, f'worker_home = "{wh}"')
        jobs = {j["id"]: j for j in snap["jobs"]}
        self.assertEqual((jobs["b_next"]["dur_min"], jobs["a_run"]["dur_min"], jobs["a_run"]["dur_src"]), (12.0, 30.0, "清单估时"))
        self.assertAlmostEqual(jobs["b_next"]["plan_start_ts"], now + 1200, delta=1)
        self.assertAlmostEqual(jobs["b_next"]["eta_ts"], now + 1920, delta=1)
        self.assertEqual(jobs["b_next"]["after"], [["other", "x1"]])               # 等别的项目的 x1 跑完才轮到
        self.assertNotIn("plan_start_ts", jobs["b_noest"])                          # 推演里没排上的不写预计时间
        lane = snap["lanes"][0]
        self.assertEqual((lane["eta_ts"], lane["stuck"]), (None, "推演到 7 天后还没轮到"))
        g = {x["id"]: x for x in snap["groups"]}
        self.assertEqual(g["B"]["ratios"], [{"cls": "b_next", "k": 0.6, "src": "同一清单近 3 个 实际/估时 ×0.60", "open": True}])
        self.assertEqual(g["A"]["ratios"], [])

    def test_queued_aligned_to_running(self):
        # 同一车道上排队任务的预计时间与这一轮按进度算的在跑任务预计结束对齐（#64）：推演用的是看板上一轮写的 eta.json（整分钟取整），
        # 接着在跑任务开始的跟着它挪（可前可后），不是接着的（等资源）不提前、只在会重叠时往后挪；后面接着的一起挪，占位任务的结束跟着它等的任务
        wh = self.d / "wh"
        st = wh / "hosts" / "here" / "status.json"
        st.parent.mkdir(parents=True)
        now, root = time.time(), self.d / "proj"
        make_queue(root, self.lock.pid, self.cur.pid)
        with open(root / "jobs" / "q.txt", "a") as f:
            f.write("c_hold|sleep 1   # est_min=60 after=b_next\n")
        cfg = load(write_config(self.d, root, self.d / "out", f'worker_home = "{wh}"'))

        def snap(run_t1, nxt, hold=None, deps=(("out", "b_next"),)):
            jobs = [{"name": "a_run", "t0": now - 600, "t1": run_t1, "gpus": [0], "run": True}, dict(nxt, name="b_next", gpus=[0])]
            if hold:
                jobs.append(dict(hold, name="c_hold", gpus=[], deps=[list(d) for d in deps]))
            st.write_text(json.dumps({"t": now, "waiting": [], "queues": [{"root": str(root), "jobs": "jobs/q.txt", "dur": {}}],
                "plan": {"t": now, "lanes": [{"root": str(root), "queue": "jobs/q.txt", "project": "out", "kind": "queue", "stuck": None, "jobs": jobs}]}}))
            return {j["id"]: j for j in C.collect(cfg)["jobs"]}

        js = snap(now + 600, {"t0": now + 600, "t1": now + 1800}, {"t0": now + 1800, "t1": now + 2000})
        eta = js["a_run"]["eta_ts"]
        self.assertAlmostEqual(eta, now + 1200, delta=5)                           # 按估时剩 20 分（进度刚开始）
        b, c = js["b_next"], js["c_hold"]
        self.assertAlmostEqual(b["plan_start_ts"], eta, delta=1)                   # 推演以为 10 分钟后就跑完：往后挪，不再和在跑的重叠
        self.assertAlmostEqual(b["eta_ts"], eta + 1200, delta=1)
        self.assertAlmostEqual(c["plan_start_ts"], b["eta_ts"], delta=1)          # 接着 b_next 的一起挪
        self.assertAlmostEqual(c["eta_ts"], c["plan_start_ts"], delta=1)           # 占位任务等的 b_next 晚了：它跟着放行，不加自己的时长
        b = snap(now + 1800, {"t0": now + 1800, "t1": now + 3000})["b_next"]       # 推演以为还要半小时：往前拉，不留假空档
        self.assertAlmostEqual(b["plan_start_ts"], eta, delta=1)
        self.assertAlmostEqual(b["eta_ts"], eta + 1200, delta=1)
        js = snap(now + 1800, {"t0": now + 1800, "t1": now + 3000}, {"t0": now + 3000, "t1": now + 3000})   # #86：往前拉时占位条不留推演的旧结束
        c = js["c_hold"]
        self.assertAlmostEqual(c["plan_start_ts"], eta + 1200, delta=1)
        self.assertAlmostEqual(c["eta_ts"], c["plan_start_ts"], delta=1)           # 放行就在开始那一刻：跟着往前，不凭空变长
        c = snap(now + 1800, {"t0": now + 1800, "t1": now + 3000}, {"t0": now + 3000, "t1": now + 4000}, (("out", "b_next"), ("other", "x1")))["c_hold"]
        self.assertAlmostEqual(c["eta_ts"], now + 4000, delta=1)                    # 还等别的项目的：不知道那边这一轮的结束，仍取推演的
        c = snap(now + 1800, {"t0": now + 1800, "t1": now + 3000}, {"t0": now + 3000, "t1": now + 3000}, (("out", "b_next"), ("other", "x1")))["c_hold"]
        self.assertAlmostEqual(c["eta_ts"], now + 3000, delta=1)                    # 推演里同时满足（t1 == t0）也一样：x1 不一定跟着提前（Codex 审查）
        c = snap(now + 1800, {"t0": now + 1800, "t1": now + 3000}, {"t0": now + 3000, "t1": now + 4500}, (("out", "b_next"), ("out", "a_done")))["c_hold"]
        self.assertAlmostEqual(c["eta_ts"], now + 4500, delta=1)                    # 等的 a_done 这条车道上跑完了、可能在别处重跑（还会跑的盖过以前的结果）：仍取推演的（Codex 审查）
        b = snap(now + 1800, {"t0": now + 2400, "t1": now + 3600, "after": [["other", "x1"]]})["b_next"]   # 等别的项目腾卡：不提前
        self.assertEqual((round(b["plan_start_ts"] - now), round(b["eta_ts"] - now), b["after"]), (2400, 3600, [["other", "x1"]]))
        b = snap(now + 600, {"t0": now + 900, "t1": now + 2100})["b_next"]         # 不是接着的、但会重叠：挪到在跑的结束
        self.assertAlmostEqual(b["plan_start_ts"], eta, delta=1)
        self.assertAlmostEqual(b["eta_ts"], eta + 1200, delta=1)

    def test_deps_from_plan(self):
        # 写了依赖（after=）的带上推演里的 deps；占位任务的预计结束取 worker 推演的（它等的任务预计结束里最晚的），时长来源写等谁，
        # 排队的不随它自己开始晚了往后挪；普通任务只带 deps，时长照旧；队首卡在依赖上（等的失败了）出告警
        wh = self.d / "wh"
        st = wh / "hosts" / "here" / "status.json"
        st.parent.mkdir(parents=True)
        now, root = time.time(), self.d / "proj"
        make_queue(root, self.lock.pid, self.cur.pid)
        for f in (root / "jobs" / "q.txt", root / "state" / "current", root / "state" / "queue.log"):
            f.write_text(f.read_text().replace("a_run", "w_hold"))
        (root / "logs" / "w_hold.log").write_text("等数据\n")
        with open(root / "jobs" / "q.txt", "a") as f:
            f.write("c_hold|sleep 1   # est_min=60 after=b_next\nd_run|true   # est_min=10 after=b_next afterany=other/x1\n")
        ha = [["out", "b_next"], ["other", "x1"]]
        wt = [{"kind": "queue", "root": str(root), "queue": "jobs/q.txt", "project": "out", "name": "b_next", "why": "找不到等的 zz（任务名写错了）", "dep": "missing"}]
        st.write_text(json.dumps({"t": now, "waiting": wt, "queues": [{"root": str(root), "jobs": "jobs/q.txt", "dur": {}}],
            "plan": {"t": now, "lanes": [{"root": str(root), "queue": "jobs/q.txt", "project": "out", "kind": "queue", "stuck": None, "jobs": [
                {"name": "w_hold", "t0": now - 600, "t1": now + 3600, "gpus": [], "run": True, "deps": ha},
                {"name": "b_next", "t0": now + 3600, "t1": now + 4800, "gpus": [0]},
                {"name": "c_hold", "t0": now + 4800, "t1": now + 7200, "gpus": [], "deps": [["out", "b_next"]]},
                {"name": "d_run", "t0": now + 7200, "t1": now + 7800, "gpus": [0], "deps": [["out", "b_next"], ["other", "x1", "any"]]}]}]}}))
        cfg = load(write_config(self.d, root, self.d / "out", f'worker_home = "{wh}"'))
        snap = C.collect(cfg)
        jobs = {j["id"]: j for j in snap["jobs"]}
        w, c, d = jobs["w_hold"], jobs["c_hold"], jobs["d_run"]
        self.assertEqual((w["status"], w["hold"], w["deps"]), ("running", True, ha))
        self.assertAlmostEqual(w["eta_ts"], now + 3600, delta=2)
        self.assertEqual(w["eta_source"], "等 b_next、other 的 x1 跑完")
        self.assertEqual((w["dur_src"], w["dur_min"]), ("等 b_next、other 的 x1 跑完", 70.0))
        self.assertAlmostEqual(c["plan_start_ts"], now + 4800, delta=2)
        self.assertAlmostEqual(c["eta_ts"], now + 4800, delta=2)                  # 等的 b_next 在本车道：取它这一轮的结束（#86），不取推演的 t1
        self.assertEqual(c["dur_src"], "等 b_next 跑完")
        self.assertEqual((d["deps"], d["dur_min"], d["dur_src"]), ([["out", "b_next"], ["other", "x1", "any"]], 10, "清单估时"))
        self.assertEqual(snap["lanes"][0]["dep"], "missing")
        al = [a["msg"] for a in snap["alerts"] if a["key"].startswith("dep:")]
        self.assertEqual(len(al), 1)
        self.assertIn("卡在依赖上：找不到等的 zz", al[0])
        for dep, alert in (("cycle", True), ("wait", False)):                       # 成环也告警（wait 以外的都是等不来）；还在等的不报
            st.write_text(st.read_text().replace('"dep": "%s"' % ("missing" if dep == "cycle" else "cycle"), '"dep": "%s"' % dep))
            snap = C.collect(cfg)
            self.assertEqual((snap["lanes"][0]["dep"], any(a["key"].startswith("dep:") for a in snap["alerts"])), (dep, alert))
        # 队首因 CPU 被挡：worker 满 10 分钟才写 cpu_wait，看板只认这个字段，出一条 cpu:<车道> 告警（只报给这条车道的项目）
        why = "CPU 不够：要 4 核（提示值），按记账只剩 1.0 核（配额 8）"
        for cw, n in ((None, 0), (720, 1)):
            data = json.loads(st.read_text())
            data["waiting"][0].update(dep=None, why=why, cpu_wait=cw)
            st.write_text(json.dumps(data))
            snap = C.collect(cfg)
            al = [a["msg"] for a in snap["alerts"] if a["key"].startswith("cpu:")]
            self.assertEqual((snap["lanes"][0]["cpu_wait"], len(al)), (cw, n))
        self.assertIn("因 CPU 被挡 12 分钟：" + why, al[0])

    def test_end_to_end(self):
        snap, out = self.run_collect(self.lock.pid)
        self.assertTrue(snap["hosts"][0]["ok"], snap["hosts"][0]["error"])
        lane = snap["lanes"][0]
        self.assertTrue(lane["alive"])
        self.assertEqual(lane["label"], "车道 1 训练方")             # 车道名按主机编号，label 是用途
        self.assertEqual((lane["total"], lane["done"], lane["unfinished"]), (4, 1, 3))
        jobs = {j["id"]: j for j in snap["jobs"]}
        self.assertEqual([jobs[k]["status"] for k in ("a_done", "a_run", "b_next", "b_noest")],
                         ["done", "running", "queued", "queued"])
        self.assertAlmostEqual(jobs["a_done"]["actual_min"], 18.3, delta=0.2)
        run = jobs["a_run"]
        self.assertEqual(run["progress"], [5, 10])
        self.assertEqual(run["eta_source"], "估时（进度刚开始）")     # 只有一个采样：分不清开头做没做活，按估时 30 分
        self.assertAlmostEqual(run["remaining_min"], 20, delta=0.5)
        self.assertEqual(run["last_line"], "step 5 done")                 # 跳过回溯里缩进的行与逐字重复的提示
        self.assertNotIn("plan_start_ts", jobs["b_next"])               # 不归 worker 管的队列没有推演：排队任务不排预计时间
        self.assertIsNone(snap["all_done_ts"])
        g = {x["id"]: x for x in snap["groups"]}
        self.assertEqual((g["A"]["status"], g["A"]["done"], g["A"]["running"]), ("运行中", 1, 1))
        self.assertEqual((g["B"]["status"], g["B"]["queued"]), ("排队中", 2))
        self.assertEqual(snap["alerts"], [])
        html = (out / "index.html").read_text()
        self.assertIn("<title>测试监控</title>", html)
        self.assertNotIn("/*__DATA__*/null", html)
        self.assertEqual(json.loads((out / "snapshot.json").read_text())["schema"], C.SCHEMA)
        self.assertEqual(snap["attempts"], [])                            # 工作台还没存这台的用量记录

    def test_attempts_from_usage(self):
        # 失败的尝试取工作台存的用量记录（state.usage_file，worker 跑完的任务一行）：按主机、目录、清单对到车道；
        # 别的项目、别的清单、跑成功或停下重排的、30 天前的都不算
        old = os.environ.get("LABMON_STATE")
        os.environ["LABMON_STATE"] = str(self.d / "state")
        try:
            from labmon.state import usage_file
            now, root = time.time(), str(self.d / "proj")
            row = lambda n, st, p="out", q="jobs/q.txt", t=now - 3600: {"t": t, "s": t - 600, "n": n, "p": p, "root": root, "queue": q, "g": ["GPU-x"], "st": st}  # noqa: E731
            f = usage_file("here")
            f.parent.mkdir(parents=True)
            f.write_text("".join(json.dumps(r) + "\n" for r in [
                row("a_run", "failed"), row("a_run", "failed", t=now - 7200), row("a_done", "done"), row("b_next", "stop"),
                row("x", "failed", p="other"), row("y", "failed", q="jobs/z.txt"), row("z", "failed", t=now - 40 * 86400)]))
            snap, _ = self.run_collect(self.lock.pid)
            self.assertEqual([(a["name"], a["lane"], a["t1"] - a["t0"]) for a in snap["attempts"]], [("a_run", "q", 600), ("a_run", "q", 600)])
            self.assertEqual(snap["attempts"][1]["t1"], now - 7200)
        finally:
            if old is None:
                os.environ.pop("LABMON_STATE", None)
            else:
                os.environ["LABMON_STATE"] = old

    def test_history_and_markers(self):
        snap, _ = self.run_collect(self.lock.pid)
        self.assertEqual(snap["markers"][0]["label"], "截稿")
        self.assertEqual(dt.datetime.fromtimestamp(snap["markers"][0]["ts"]).strftime("%m-%d %H:%M"), "01-02 23:59")
        host = [r for r in snap["history"] if r["host"] == "here"][-1]
        self.assertIn("/", host["disk"])                                  # 每个挂载点的余量进历史
        self.assertEqual({r["host"] for r in snap["history"]}, {"here"})      # 「本机」一栏删了（#164）：历史里不再有 __local__ 行
        self.assertNotIn("local", snap)

    def test_log_only_current_attempt(self):
        """日志只看最后一行分隔（worker 每次开跑写的）之后的：上一次的进度不当这一次的；还没输出的（不出声的占位任务）没有最后一行，
        11-07 前的旧写法分隔行也认。"""
        root = self.d / "proj"
        make_queue(root, self.lock.pid, self.cur.pid)
        (root / "logs" / "a_run.log").write_text("PROGRESS 9/10\n===== 2026-11-07 13:18:34 开始（可能是续跑；labmon worker，GPU 无）=====\n")
        run = {j["id"]: j for j in C.collect(load(write_config(self.d, root, self.d / "out")))["jobs"]}["a_run"]
        self.assertIsNone(run.get("last_line"))
        self.assertIsNone(run.get("progress"))

    def test_recent_speed(self):
        """两次采集之间进度前进 3 步、相隔 400 秒：ETA 改用近期速度，并给出速度曲线。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, self.lock.pid, self.cur.pid)
        cfg_path = write_config(self.d, root, out)
        log = root / "logs" / "a_run.log"
        now = time.time()
        os.utime(log, (now - 400, now - 400))
        # queue.log 最后一行写的是 10 分钟前开跑：修改时间对上它，不让探针当成盘的时钟快 10 分钟去校正日志时间
        # （#140 起剩余从最后一个进度采样起算，校正后的采样早于现在 10 分钟，剩余就成了 0）
        os.utime(root / "state" / "queue.log", (now - 600, now - 600))
        C.collect(load(cfg_path))
        log.write_text("PROGRESS 8/10 loss=0.2\n")
        os.utime(log, (now, now))
        run = {j["id"]: j for j in C.collect(load(cfg_path))["jobs"]}["a_run"]
        self.assertEqual(run["eta_source"], "近期速度")
        self.assertAlmostEqual(run["rate_per_min"], 0.45, delta=0.01)
        self.assertAlmostEqual(run["remaining_min"], 2 / 0.45, delta=0.1)
        self.assertEqual(len(run["speed"]), 1)
        self.assertAlmostEqual(run["speed"][0][1], 0.45, delta=0.01)


    def test_no_gpu_from_plan(self):
        """车道的 no_gpu（#73）：worker 推演里写着不用卡（gpus 为 []）的未完成任务；推演里没有的（b_noest）照旧当要卡。"""
        wh = self.d / "wh"
        st = wh / "hosts" / "here" / "status.json"
        st.parent.mkdir(parents=True)
        now, root = time.time(), str(self.d / "proj")
        st.write_text(json.dumps({"t": now, "waiting": [], "queues": [{"root": root, "jobs": "jobs/q.txt"}],
            "plan": {"t": now, "lanes": [{"root": root, "queue": "jobs/q.txt", "project": "out", "kind": "queue", "stuck": None,
                                          "jobs": [{"name": "a_run", "t0": now - 600, "t1": now + 600, "gpus": [], "run": True},
                                                   {"name": "b_next", "t0": now + 600, "t1": now + 1800, "gpus": [0]}]}]}}))
        snap, _ = self.run_collect(self.lock.pid, f'worker_home = "{wh}"')
        lane = snap["lanes"][0]
        self.assertEqual((lane["unfinished"], lane["no_gpu"]), (3, 1))

    def test_dead_lock_alerts(self):
        pid = dead_pid()
        snap, _ = self.run_collect(pid)
        self.assertFalse(snap["lanes"][0]["alive"])
        self.assertIn(f"lock_dead:q:{pid}", [a["key"] for a in snap["alerts"]])    # 带锁里的 PID（#129）

    def test_worker_waiting(self):
        """worker 在管、空着等显存的队列（跑完上一个任务就删了锁）：显示 worker 说的等待原因，不报 lock_dead；worker 状态过期了照报。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        cfg = write_config(self.d, root, out)
        cfg.write_text(cfg.read_text().replace('label = "本机"', f'label = "本机"\nworker_home = "{self.d}/wh"'))
        sf = self.d / "wh" / "hosts" / "here" / "status.json"
        sf.parent.mkdir(parents=True)
        why = "没有可用显存够 23 GiB 的卡"

        def status(age):
            sf.write_text(json.dumps({"t": time.time() - age, "queues": [{"root": str(root) + "/", "jobs": "jobs/q.txt"}],
                                      "waiting": [{"queue": "jobs/q.txt", "root": str(root), "name": "b_next", "why": why}]}))
            return C.collect(load(cfg))
        snap = status(5)
        lane = snap["lanes"][0]
        self.assertEqual((lane["alive"], lane["managed"], lane["waiting"]), (False, True, why))
        self.assertFalse(any(a["key"].startswith("lock_dead:") for a in snap["alerts"]))
        snap = status(600)
        self.assertFalse(snap["lanes"][0]["managed"])
        self.assertTrue(any(a["key"].startswith("lock_dead:q:") for a in snap["alerts"]))

    def test_held_lane_is_paused(self):
        """worker 说被临时指令挡住（本项目暂停、drain）的空闲车道按暂停算；drain 另报一条，告诉会话整台主机不放新任务。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        cfg = write_config(self.d, root, out)
        cfg.write_text(cfg.read_text().replace('label = "本机"', f'label = "本机"\nworker_home = "{self.d}/wh"'))
        sf = self.d / "wh" / "hosts" / "here" / "status.json"
        sf.parent.mkdir(parents=True)
        (root / "state" / "current").unlink()                # 车道上没有在跑的

        def status(held, why):
            sf.write_text(json.dumps({"t": time.time(), "queues": [{"root": str(root), "jobs": "jobs/q.txt"}],
                                      "waiting": [{"queue": "jobs/q.txt", "root": str(root), "name": "b_next", "why": why, "held": held}]}))
            return C.collect(load(cfg))
        snap = status("pause", "项目 p 暂停中（无期限）")
        self.assertEqual(snap["lanes"][0]["paused"], "项目 p 暂停中（无期限）")
        self.assertNotIn("drain:here", [a["key"] for a in snap["alerts"]])
        snap = status("drain", "主机 drain 中（无期限）")
        self.assertIn("drain:here", [a["key"] for a in snap["alerts"]])
        snap = status(None, "没有可用显存")
        self.assertIsNone(snap["lanes"][0].get("paused"))

    def test_throttle_alert(self):
        """容器 CPU 持续被配额限流（worker 写的 throttle，#39）：本项目有超核的任务才报 throttle:<主机>:<起点>，给两边看；
        会话那份写按 $LABMON_CPUS 设线程数，页面上给用户的只说现象；只有别的项目超核、还没持续满、没在限流都不报。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        cfg = write_config(self.d, root, out)
        cfg.write_text(cfg.read_text().replace('label = "本机"', f'label = "本机"\nworker_home = "{self.d}/wh"'))
        sf = self.d / "wh" / "hosts" / "here" / "status.json"
        sf.parent.mkdir(parents=True)
        sf.write_text(json.dumps({"t": time.time(), "queues": [{"root": str(root), "jobs": "jobs/q.txt"}], "waiting": []}))
        over = [{"project": "out", "name": "a_run", "cores": 6.0, "give": 2}, {"project": "other", "name": "x", "cores": 9.0, "give": 1}]

        def alerts(th):
            with mock.patch.object(C, "snapshot", lambda: {"hosts": [{"id": "here", "status": {"throttle": th}}]}):
                return {a["key"]: a for a in C.collect(load(cfg))["alerts"] if a["key"].startswith("throttle:")}
        th = {"since": 1000, "for": 900, "thr": 48.5, "alert": True, "over": over, "other": 0.3}
        al = alerts(th)
        self.assertEqual(list(al), ["throttle:here:1000"])
        a = al["throttle:here:1000"]
        self.assertEqual(a["to"], "both")
        self.assertIn("本机：容器 CPU 被配额限流 48.5%（已持续 15 分钟），本项目超核的任务：a_run 实测 6 核（给 2 核）", a["msg"])
        self.assertNotIn("x 实测", a["msg"])                                     # 别的项目的不写进来
        self.assertIn("$LABMON_CPUS", a["msg"])
        self.assertNotIn("LABMON_CPUS", a["page"])                               # 给用户的只说现象
        self.assertEqual(alerts(dict(th, over=over[1:])), {})                    # 只有别的项目超核：不叫醒本项目
        self.assertEqual(alerts(dict(th, alert=False)), {})                      # 还没持续满
        self.assertEqual(alerts(None), {})

    def test_cpu_fit_alert(self):
        """开跑核数给少、给多（worker 写的运行中任务 cpu_fit，#46）：本项目的清单任务才报，只给实验会话（agent）；
        key 按类（worker 给的 cls，#91），给少的已到配额（sug 为 None）不报，别的项目、单元、旧 worker 没这个字段的都不报。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        cfg = write_config(self.d, root, out)
        cfg.write_text(cfg.read_text().replace('label = "本机"', f'label = "本机"\nworker_home = "{self.d}/wh"'))
        sf = self.d / "wh" / "hosts" / "here" / "status.json"
        sf.parent.mkdir(parents=True)
        sf.write_text(json.dumps({"t": time.time(), "queues": [{"root": str(root), "jobs": "jobs/q.txt"}], "waiting": []}))
        low = {"how": "low", "avg": 0.98, "peak": 1.0, "give": 1, "sug": 2, "by": "name", "cls": "a_run"}
        high = {"how": "high", "avg": 0.9, "peak": 1.2, "give": 5, "sug": 2, "by": "name", "cls": "a_run"}

        def alerts(*running):
            with mock.patch.object(C, "snapshot", lambda: {"hosts": [{"id": "here", "status": {"running": list(running)}}]}):
                return {a["key"]: a for a in C.collect(load(cfg))["alerts"] if a["key"].startswith("cpu_")}
        run = lambda f, **kw: dict({"kind": "queue", "project": "out", "name": "a_run", "cpus": None, "cpu_fit": f,   # noqa: E731
                                    "root": str(root) + "/", "queue": "jobs/q.txt"}, **kw)
        al = alerts(run(low))
        self.assertEqual(list(al), ["cpu_low:name:a_run"])
        a = al["cpu_low:name:a_run"]
        self.assertEqual(a["to"], "agent")
        self.assertIn("任务 a_run（本机 车道 1 训练方）近 10 分钟平均用了 1.0 核，给的 1 核基本用满", a["msg"])
        self.assertIn("写 cpus=2 试试（cpus= 优先于历史", a["msg"])
        # #112：worker 按配额、同机别的任务与同类条数封顶过（cpu_budget），告警里写出来
        rm = {"per": 3, "cap": 7, "other": 1.0, "out": 0.5, "same": 2, "kept": False}
        a = alerts(run(dict(low, sug=3, room=rm)))["cpu_low:name:a_run"]
        self.assertIn("写 cpus=3 试试（这台配额 7 核，同类 2 条同时在跑，别的任务记了 1 核，labmon 之外占 0.5 核，每条最多 3 核，再写大会超配额；", a["msg"])
        al = alerts(run(high, cpus=5.0))
        self.assertEqual(list(al), ["cpu_high:name:a_run"])
        self.assertIn("清单写了 cpus=5（给 5 核），跑到现在 1 分钟峰值只有 1.2 核", al["cpu_high:name:a_run"]["msg"])
        self.assertIn("cpus=2", al["cpu_high:name:a_run"]["msg"])
        self.assertIn("自动按实测记账", al["cpu_high:name:a_run"]["msg"])
        # 同类（tag）的几个任务合成一条：key 不带任务名，消息列出都有谁、峰值范围，建议取大
        tag = lambda n, pk, sug: run(dict(high, peak=pk, sug=sug, by="tag", cls="sm"), name=n, cpus=4.0)   # noqa: E731
        al = alerts(tag("a_s1", 1.0, 1), tag("a_s2", 1.2, 2))
        self.assertEqual(list(al), ["cpu_high:tag:sm"])
        self.assertIn("同 tag sm 的 2 个任务（本机 车道 1 训练方：a_s1、a_s2）清单写了 cpus=4（给 5 核），跑到现在 1 分钟峰值只有 1.0–1.2 核", al["cpu_high:tag:sm"]["msg"])
        self.assertIn("cpus=2", al["cpu_high:tag:sm"]["msg"])
        self.assertEqual(list(alerts(tag("a_s1", 1.0, 1), run(low, name="b_s1"))), ["cpu_high:tag:sm", "cpu_low:name:a_run"])   # 给少、给多各报各的
        # #99（Codex 审 #97）：tag 恰好等于另一类去种子号后的名字（tag=train 的 prep_s1、无 tag 的 train_s1）：不同类，各报一条、key 带来源
        both = alerts(run(dict(low, by="tag", cls="train"), name="prep_s1"), run(dict(low, cls="train"), name="train_s1"))
        self.assertEqual(sorted(both), ["cpu_low:name:train", "cpu_low:tag:train"])
        self.assertIn("任务 prep_s1（", both["cpu_low:tag:train"]["msg"])
        self.assertIn("任务 train_s1（", both["cpu_low:name:train"]["msg"])
        self.assertEqual(alerts(run(dict(low, sug=None))), {})                    # 已给到配额：写大也给不了
        self.assertEqual(alerts(run(low, project="other")), {})                   # 别的项目的
        self.assertEqual(alerts(run(low, kind="slot")), {})                       # 单元没有清单行可改
        self.assertEqual(alerts(run(None)), {})
        self.assertEqual(alerts(run(low, root=str(self.d / "other"))), {})        # 同名任务在本项目别的清单上（不是这条车道）：不算到 q 头上（Codex 审 #65）
        self.assertEqual(alerts({"kind": "queue", "project": "out", "name": "a_run", "root": str(root), "queue": "jobs/q.txt"}), {})   # 旧 worker 没写这个字段
        old = {k: v for k, v in low.items() if k not in ("by", "cls")}
        self.assertEqual(list(alerts(run(old, name="a_s1"), run(old, name="a_s2"))), ["cpu_low:job:a_s1", "cpu_low:job:a_s2"])   # 还没升级的 worker 不给 cls：按任务名各一条

    def test_cpu_low_per_host_sug(self):
        """#112：同类在两台上都给少了，两台的配额不同：建议值按主机分写、各写配额，不取最大值（照最大值写在配额小的那台上会超）。"""
        cfg = {"id": "out", "alerts": {"gpu_idle_min": 30, "gpu_idle_util": 5.0},
               "hosts": [{"id": "a", "checks": []}, {"id": "b", "checks": []}]}
        lane = lambda h, n: {"id": f"{h}{n}", "host": h, "label": f"车道 {n}", "root": "/r", "queue": "jobs/q.txt"}   # noqa: E731
        snap = {"hosts": [{"id": "a", "ok": True, "label": "甲"}, {"id": "b", "ok": True, "label": "乙"}],
                "lanes": [lane("a", 1), lane("b", 1)], "jobs": [], "alerts": []}
        fit = lambda sug, cap, same: {"how": "low", "avg": 1.9, "peak": 2.0, "give": 2, "sug": sug, "by": "tag", "cls": "sm",   # noqa: E731
                                      "room": {"per": cap // same, "cap": cap, "other": 0.0, "out": 0.0, "same": same, "kept": False}}
        run = lambda n, f: {"kind": "queue", "project": "out", "name": n, "cpus": 2.0, "cpu_fit": f, "root": "/r", "queue": "jobs/q.txt"}   # noqa: E731
        C._host_alerts(cfg, snap, {}, [], {"hosts": [{"id": "a", "status": {"running": [run("a_s1", fit(3, 7, 2)), run("a_s2", fit(3, 7, 2))]}},
                                                     {"id": "b", "status": {"running": [run("a_s3", fit(4, 16, 1))]}}]}, time.time())
        msg = {x["key"]: x["msg"] for x in snap["alerts"]}["cpu_low:tag:sm"]
        self.assertIn("按主机写 甲 上 cpus=3、乙 上 cpus=4 试试（甲 配额 7 核，同类 2 条同时在跑，每条最多 3 核；乙 配额 16 核，每条最多 16 核，再写大会超配额；", msg)

    def test_cpu_fit_alert_merged_across_hosts(self):
        """#91：同一类分布在两台主机上的任务合成一条 cpu_high:，wait 只叫醒一次；同类后面的任务接着被判出来（key 不变）6 小时内不再叫醒（#88）。"""
        from labmon.waits import _new_alerts
        cfg = {"id": "out", "alerts": {"gpu_idle_min": 30, "gpu_idle_util": 5.0},
               "hosts": [{"id": "a", "checks": []}, {"id": "b", "checks": []}]}
        lane = lambda h, n: {"id": f"{h}{n}", "host": h, "label": f"车道 {n}", "root": "/r", "queue": "jobs/q.txt"}   # noqa: E731
        snap = {"hosts": [{"id": "a", "ok": True, "label": "甲"}, {"id": "b", "ok": True, "label": "乙"}],
                "lanes": [lane("a", 1), lane("b", 1)], "jobs": []}
        fit = lambda pk: {"how": "high", "avg": 0.9, "peak": pk, "give": 4, "sug": 2, "by": "tag", "cls": "sm"}   # noqa: E731
        run = lambda n, pk: {"kind": "queue", "project": "out", "name": n, "cpus": 4.0, "cpu_fit": fit(pk), "root": "/r", "queue": "jobs/q.txt"}   # noqa: E731

        def alerts(a, b):
            snap["alerts"] = []
            C._host_alerts(cfg, snap, {}, [], {"hosts": [{"id": "a", "status": {"running": a}}, {"id": "b", "status": {"running": b}}]}, time.time())
            return {x["key"]: x["msg"] for x in snap["alerts"]}
        al = alerts([run("a_s1", 1.0), run("a_s2", 1.1)], [run("a_s3", 1.2)])
        self.assertEqual(list(al), ["cpu_high:tag:sm"])
        self.assertIn("同 tag sm 的 3 个任务（甲 车道 1：a_s1、a_s2；乙 车道 1：a_s3）", al["cpu_high:tag:sm"])
        sf = self.d / "wait_state.json"
        t = time.time()
        self.assertEqual(_new_alerts(sf, al, t), ["cpu_high:tag:sm"])
        self.assertEqual(_new_alerts(sf, alerts([run("a_s1", 1.0)], []), t + 300), [])          # 有的跑完了：同一条，不再叫醒
        self.assertEqual(_new_alerts(sf, alerts([], []), t + 600), [])                          # 都跑完了
        self.assertEqual(_new_alerts(sf, alerts([], [run("a_s4", 1.0)]), t + 1200), [])         # 同类下一个又判出来：6 小时内不叫醒
        # 无 tag、去种子号后恰好也叫 sm 的另一类（#99）：key 不同，第一次照样叫醒，不被 tag sm 的去冗余压掉
        other = dict(run("sm_s1", 1.0), cpu_fit=dict(fit(1.0), by="name"))
        self.assertEqual(_new_alerts(sf, alerts([], [other]), t + 1500), ["cpu_high:name:sm"])

    def test_borrow_cut_alert(self):
        """借来的主机上队首被约定归还挡住（borrow.check 在全局快照里给的 late 非空）：报 borrow_cut:<车道>，写主机全名、车道名与还剩几个任务；
        自用主机被续租截止挡住的（没有 borrow）不报；约定延长、跑得完了（不再 held）就消失；归还后交给 returned_unfinished，不再报它。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        cfg = write_config(self.d, root, out)
        cfg.write_text(cfg.read_text().replace('label = "本机"', f'label = "本机（借 EVA）"\nworker_home = "{self.d}/wh"'))
        sf = self.d / "wh" / "hosts" / "here" / "status.json"
        sf.parent.mkdir(parents=True)
        (root / "state" / "current").unlink()                # 车道上没有在跑的
        why = "约定归还 11-08 18:00 前跑不完（估 183 分钟）"
        late = [{"project": "out", "root": str(root), "queue": "jobs/q.txt", "name": "b_next", "why": why, "n": 3}]

        def status(held, borrow, returned=None):
            sf.write_text(json.dumps({"t": time.time(), "queues": [{"root": str(root), "jobs": "jobs/q.txt"}],
                                      "waiting": [{"queue": "jobs/q.txt", "root": str(root), "name": "b_next", "why": why, "held": held}]}))
            glob = {"hosts": [{"id": "here", "borrow": borrow}] if borrow else []}
            c = load(cfg)
            if returned:
                c["hosts"][0]["returned"] = returned
            with mock.patch.object(C, "snapshot", lambda: glob):
                return C.collect(c)
        do = "换到别的在用主机（labmon worker add）；截止取的是 EVA 的租期 11-08 19:00 到期，提前 1 小时，延长约定推不迟它"   # borrow.late_do 给的
        say = "截止取的是 EVA 的租期 11-08 19:00 到期，提前 1 小时，延长约定推不迟它，由实验会话换到别的在用主机"         # late_do(user=True) 给的
        snap = status("deadline", {"holder": "EVA", "late": late, "late_do": do, "late_say": say})
        al = {a["key"]: a["msg"] for a in snap["alerts"]}
        self.assertIn("borrow_cut:q:here", al)
        self.assertIn("本机（借 EVA） 车道 1 训练方 还有 3 个任务不会在这台放行", al["borrow_cut:q:here"])
        self.assertIn(why, al["borrow_cut:q:here"])
        self.assertTrue(al["borrow_cut:q:here"].endswith("；" + do))                 # 怎么办的那句只由 borrow.late_do 生成
        # 两边都要（#142）：会话那份写操作，页面上给用户的写人话（late_say）
        bc = next(a for a in snap["alerts"] if a["key"] == "borrow_cut:q:here")
        self.assertEqual(bc["to"], "both")
        self.assertEqual(C.user_alerts(snap["alerts"]), [al["borrow_cut:q:here"][:-len(do)] + say])
        self.assertEqual(snap["lanes"][0]["paused"], why)                       # 照旧按暂停算，不报空转
        self.assertNotIn("borrow_cut:q:here", [a["key"] for a in status("deadline", None)["alerts"]])                   # 续租截止
        self.assertNotIn("borrow_cut:q:here", [a["key"] for a in status("deadline", {"holder": "EVA", "late": []})["alerts"]])   # 续租截止更早
        self.assertNotIn("borrow_cut:q:here", [a["key"] for a in status(None, {"holder": "EVA", "late": late})["alerts"]])  # 跑得完了
        ret = status("deadline", {"holder": "EVA", "late": late}, returned=parse_time("2026-11-08 18:00"))["alerts"]
        keys = [a["key"] for a in ret]
        self.assertNotIn("borrow_cut:q:here", keys)
        self.assertIn(f"returned_unfinished:q:here:{int(parse_time('2026-11-08 18:00'))}", keys)   # 带主机与归还时刻（#129）
        ru = next(a for a in ret if a["key"].startswith("returned_unfinished:q:here:"))       # 两边都要：用户在「管理」里换主机，会话 worker add
        self.assertEqual(ru["to"], "both")
        self.assertIn("labmon worker add", ru["msg"])
        self.assertIn("「管理」", ru["page"])
        self.assertNotIn("labmon", ru["page"])

    def woke(self, sf, snap, t, kind):
        """这一轮 labmon wait 会叫醒哪几条（waits._new_alerts，只看 kind 开头的告警）。"""
        from labmon.waits import _new_alerts
        return [k.split(":")[0] for k in _new_alerts(sf, {a["key"]: a["msg"] for a in snap["alerts"] if a["key"].startswith(kind)}, t)]

    def test_rewake_new_instance(self):
        """#129：dep、drain、returned_unfinished、borrow_cut 的 key 带“这一次”：同一处 6 小时内第二件事叫醒（旧 key 只带车道或主机，被当成
        上一件的抖动压掉）；同一件事消失又出现（读失败一轮、门槛来回）key 不变，仍不叫醒。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        cfg = write_config(self.d, root, out)
        cfg.write_text(cfg.read_text().replace('label = "本机"', f'label = "本机（借 EVA）"\nworker_home = "{self.d}/wh"'))
        st = self.d / "wh" / "hosts" / "here" / "status.json"
        st.parent.mkdir(parents=True)
        (root / "state" / "current").unlink()
        why = "约定归还 11-08 18:00 前跑不完（估 183 分钟）"
        late = [{"project": "out", "root": str(root), "queue": "jobs/q.txt", "name": "b_next", "why": why, "n": 3}]

        def snap(head="b_next", dep=None, held=None, ovr=(), borrow=None, returned=None):
            st.write_text(json.dumps({"t": time.time(), "queues": [{"root": str(root), "jobs": "jobs/q.txt"}], "overrides": list(ovr),
                                      "waiting": [{"queue": "jobs/q.txt", "root": str(root), "name": head, "why": "等不来" if dep else why,
                                                   "dep": dep, "held": held}]}))
            c = load(cfg)
            if returned:
                c["hosts"][0]["returned"] = returned
            with mock.patch.object(C, "snapshot", lambda: {"hosts": [{"id": "here", "borrow": borrow}] if borrow else []}):
                return C.collect(c)
        t0 = time.time()
        steps = {   # 每种：[(这一轮的快照参数, 叫醒吗)]：第一件、消失、同一件又出现（抖动）、6 小时内的第二件
            "dep:": [({"dep": "failed"}, True), ({}, False), ({"dep": "failed"}, False), ({"head": "b_noest", "dep": "missing"}, True)],
            "drain:": [({"held": "drain", "ovr": [{"kind": "drain", "target": "here", "t": t0 - 60}]}, True), ({}, False),
                       ({"held": "drain", "ovr": [{"kind": "drain", "target": "here", "t": t0 - 60}]}, False),
                       ({"held": "drain", "ovr": [{"kind": "drain", "target": "here", "t": t0 + 600}]}, True)],
            "returned_unfinished:": [({"returned": t0 - 3600}, True), ({}, False), ({"returned": t0 - 3600}, False), ({"returned": t0 + 600}, True)],
            "borrow_cut:": [({"held": "deadline", "borrow": {"holder": "EVA", "since": t0 - 7200, "late": late}}, True), ({}, False),
                            ({"held": "deadline", "borrow": {"holder": "EVA", "since": t0 - 7200, "late": late}}, False),
                            ({"held": "deadline", "borrow": {"holder": "EVA", "since": t0 + 300, "late": late}}, True)],
        }
        for kind, seq in steps.items():
            sf = self.d / f"w-{kind[:-1]}.json"
            with self.subTest(kind=kind):
                for i, (kw, want) in enumerate(seq):
                    self.assertEqual(self.woke(sf, snap(**kw), t0 + 600 * i, kind), [kind[:-1]] if want else [], (kind, i))

    def test_rewake_lock_dead(self):
        """#129：lock_dead 的 key 带锁里的 PID：拉起后又挂（PID 变了）6 小时内也叫醒；同一个 PID 消失又出现不叫醒。"""
        sf, t0 = self.d / "w.json", time.time()
        p1, p2 = dead_pid(), dead_pid()
        root = self.d / "proj"
        make_queue(root, p1, self.cur.pid)
        cfg = load(write_config(self.d, root, PJ.out_dir("out")))
        for i, (pid, want) in enumerate(((p1, True), (self.lock.pid, False), (p1, False), (p2, True))):
            (root / "state" / "lock").write_text(str(pid))
            snap = C.collect(cfg)
            self.assertEqual(self.woke(sf, snap, t0 + 600 * i, "lock_dead:"), ["lock_dead"] if want else [], i)

    def test_rebalance_alert(self):
        """挪法建议（全局快照的 rebalance，rebalance.suggest 算的）：项目看板出 rebalance:<项目> 告警（叫醒 labmon wait，
        建议一直在时只叫醒一次），快照带上建议给页面；挪的是借用约定挡住的清单（cut）时这句写进那条 borrow_cut（key 加 :rebalance：
        先报过不带建议的，有了挪法再叫醒一次），不再报 rebalance；被挡的清单没配进项目车道（borrow_cut 不出）时照报 rebalance；
        挪不了的（只有 why）不报。"""
        from labmon.waits import wait_check
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        cfg = write_config(self.d, root, out)
        cfg.write_text(cfg.read_text().replace('label = "本机"', f'label = "本机（借 EVA）"\nworker_home = "{self.d}/wh"'))
        sf = self.d / "wh" / "hosts" / "here" / "status.json"
        sf.parent.mkdir(parents=True)
        (root / "state" / "current").unlink()
        why = "约定归还 11-08 18:00 前跑不完（估 183 分钟）"
        late = [{"project": "out", "root": str(root), "queue": "jobs/q.txt", "name": "b_next", "why": why, "n": 3}]
        text = "把 本机（借 EVA） 上 jobs/q.txt 的 b_next、b_noest 挪到 t2（这台还没有本项目同一种环境的清单：新建一条并 labmon worker add）：整批预计 11-09 02:00 跑完"
        rb = {"key": "rebalance:out", "target": "t2", "jobs": [], "from": None, "to": 0, "cut": [], "text": text}

        def status(r, held=None, borrow=None):
            sf.write_text(json.dumps({"t": time.time(), "queues": [{"root": str(root), "jobs": "jobs/q.txt"}],
                                      "waiting": [{"queue": "jobs/q.txt", "root": str(root), "name": "b_next", "why": why, "held": held}]}))
            glob = {"hosts": [{"id": "here", "borrow": borrow}] if borrow else [], "rebalance": {"out": r} if r else {}}
            with mock.patch.object(C, "snapshot", lambda: glob):
                return C.collect(load(cfg))
        snap = status(rb)
        al = {a["key"]: a["msg"] for a in snap["alerts"]}
        self.assertEqual(al.get("rebalance:out"), text)
        self.assertEqual(snap["rebalance"], rb)
        self.assertEqual(C.user_alerts(snap["alerts"]), [])                   # 整句只给实验会话：页面上不列、不算告警数（#40）
        self.assertEqual([a["to"] for a in snap["alerts"]], ["agent"])
        w = lambda: wait_check(load(cfg), out, self.d / "w.json", self.d / "none.json")
        self.assertIn("rebalance:out", w())
        status(rb)
        self.assertNotIn("rebalance:out", w())                                 # 同一建议：不再叫醒
        # 约定挡住了、别的主机的 offers 还没到：borrow_cut 不带建议；有了挪法后写进 borrow_cut（换 key 再叫醒一次），rebalance 不报
        do, say = "换到别的在用主机（labmon worker add），或请用户延长约定", "可在主机页「改约定」延长，或由实验会话换到别的在用主机"
        bw = {"holder": "EVA", "late": late, "late_do": do, "late_say": say}
        snap = status(None, "deadline", bw)
        self.assertTrue({a["key"]: a["msg"] for a in snap["alerts"]}["borrow_cut:q:here"].endswith("；" + do))
        self.assertIn("borrow_cut:q:here", w())
        snap = status(dict(rb, cut=[f"here|{root}|jobs/q.txt"]), "deadline", bw)
        al = {a["key"]: a["msg"] for a in snap["alerts"]}
        self.assertNotIn("rebalance:out", al)
        self.assertTrue(al["borrow_cut:q:here:rebalance"].endswith("；" + do + "。可以这样挪：" + text))
        self.assertEqual([m[-len(say) - 1:] for m in C.user_alerts(snap["alerts"])], ["；" + say])   # 页面上的那条不带挪法，写给用户的那句
        self.assertEqual(sorted(w()), ["borrow_cut:q:here:rebalance"])
        # 被挡的清单没配进本项目的车道（borrow_cut 不出）：照报 rebalance
        snap = status(dict(rb, cut=[f"here|{root}/other|jobs/q.txt"]), "deadline", bw)
        al = {a["key"]: a["msg"] for a in snap["alerts"]}
        self.assertEqual(al.get("rebalance:out"), text)
        # 只有原因（挪不了）：页面上写，不告警
        snap = status({"why": ["t2 上本项目没活，接不了 x：一直排不上，放不下（要的比卡的显存还大）"], "from": None})
        self.assertFalse([a for a in snap["alerts"] if a["key"].startswith("rebalance:")])
        self.assertEqual(snap["rebalance"]["why"][0][:2], "t2")

    def test_hold_insert_and_killed(self):
        """插在运行中任务之前的暂停任务：不计入计划项；被 kill 的任务标被终止并提示删 .failed。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, self.lock.pid, self.cur.pid)
        jl = root / "jobs" / "q.txt"
        jl.write_text("x_hold|sleep 1   # est_min=60\n" + jl.read_text() + "k_job|echo\n")
        (root / "state" / "k_job.failed").write_text("")
        with open(root / "state" / "queue.log", "a") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} 开始 k_job\n{dt.datetime.now():%Y-%m-%d %H:%M:%S} 失败 k_job（退出码 143，见 x）\n")
        snap = C.collect(load(write_config(self.d, root, out)))
        jobs = {j["id"]: j for j in snap["jobs"]}
        hold = jobs["x_hold"]
        self.assertTrue(hold["hold"])
        self.assertIsNone(hold["group"])
        self.assertTrue(snap["lanes"][0]["has_hold"])
        self.assertEqual(jobs["k_job"]["killed"], 143)
        fa = next(a for a in snap["alerts"] if a["key"] == "failed:k_job")
        msg = fa["msg"]
        self.assertIn("被终止", msg)
        self.assertIn("k_job.failed", msg)
        self.assertEqual(fa["to"], "agent")                                    # 重跑失败的是实验会话的事：页面上不算告警数（#142）
        self.assertNotIn(msg, C.user_alerts(snap["alerts"]))
        self.assertNotIn("x_", {g["id"] for g in snap["groups"]})

    def test_paused_marker(self):
        """队列停了但状态目录里有 paused：显示已暂停，不报 lock_dead，也不计入全部完成时间。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, dead_pid(), self.cur.pid)
        (root / "state" / "paused").write_text("给 E5 腾卡")
        snap = C.collect(load(write_config(self.d, root, out)))
        lane = snap["lanes"][0]
        self.assertEqual(lane["paused"], "给 E5 腾卡")
        self.assertIsNone(lane["eta_ts"])
        self.assertIsNone(snap["all_done_ts"])
        self.assertFalse(any(a["key"].startswith("lock_dead:") for a in snap["alerts"]))

    def test_noprogress_alert(self):
        """#68：在跑的任务超了估时、已跑满 30 分钟、这一次的日志里一直没有进度行：报 noprogress（写明车道与已跑多久）；
        本机不在内网时数据是离开前的，不报。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, self.lock.pid, self.cur.pid)
        start = dt.datetime.fromtimestamp(time.time() - 2 * 3600).strftime("%Y-%m-%d %H:%M:%S")
        (root / "state" / "current").write_text(f"a_run {self.cur.pid} {start}")
        (root / "state" / "queue.log").write_text(f"{start} 开始 a_run\n")
        (root / "logs" / "a_run.log").write_text("PROGRESS 9/10\n===== 2027-01-01 00:00:00 labmon 开始（GPU 0）=====\n加载数据\n")
        cfg = load(write_config(self.d, root, out))
        al = {a["key"]: a["msg"] for a in C.collect(cfg)["alerts"]}
        k = f"noprogress:a_run:{int(parse_time(start))}"                           # 带这一次的开跑时刻（#129）
        self.assertIn(k, al)
        self.assertIn("（本机 车道 1 训练方）", al[k])
        self.assertIn("已跑 2.0 小时", al[k])
        real = C.L.gather
        with mock.patch.object(C.L, "gather", lambda *a, **k: ({"ok": False, "since": time.time()}, real(*a, **k)[1])):
            self.assertNotIn(k, [a["key"] for a in C.collect(cfg)["alerts"]])
        # #86：在内网、只是这一台探针失败，车道用上一次的数据：同样不报
        fail = lambda *a, **k: (lambda r: (r[0], {h: (None, "探针失败") for h in r[1]}))(real(*a, **k))     # noqa: E731
        with mock.patch.object(C.L, "gather", fail):
            snap = C.collect(cfg)
        self.assertTrue(snap["lanes"][0]["stale"])
        self.assertNotIn(k, [a["key"] for a in snap["alerts"]])

    def test_not_started_no_alert(self):
        """预先登记、从没启动过的队列（状态目录里什么都没有）：显示未启动，不报 lock_dead。"""
        root, out = self.d / "proj", PJ.out_dir("out")
        make_queue(root, self.lock.pid, self.cur.pid)
        import shutil
        shutil.rmtree(root / "state")
        snap = C.collect(load(write_config(self.d, root, out)))
        lane = snap["lanes"][0]
        self.assertEqual((lane["started"], lane["alive"], lane["unfinished"]), (False, False, 4))
        self.assertFalse(any(a["key"].startswith("lock_dead:") for a in snap["alerts"]))


class DiscoverTest(unittest.TestCase):
    """jobs 写通配符：每个清单一条车道；早已跑完的旧清单隐藏；不属于计划项的任务按前缀自动归组。"""

    def test_discover_and_auto_groups(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            root = d / "proj"
            lock, cur = queue_procs()
            try:
                make_queue(root, lock.pid, lock.pid)
                (root / "jobs" / "new.txt").write_text("c_one|echo   # est_min=5\nc_two|echo   # est_min=5\n")
                (root / "jobs" / "new.env").write_text("QUEUE_STATE=state_new\nQUEUE_LOGS=logs\n")
                (root / "state_new").mkdir()
                (root / "state_new" / "queue.log").write_text("")             # 刚启动过（队列进程已不在）
                (root / "jobs" / "copy.txt").write_text("y_never|echo\n")     # 从没启动过：不显示
                (root / "jobs" / "old.txt").write_text("z_old|echo   # est_min=5\n")
                (root / "jobs" / "old.env").write_text("QUEUE_STATE=state_old\nQUEUE_LOGS=logs\n")
                (root / "state_old").mkdir()
                (root / "state_old" / "z_old.done").write_text("")
                old = time.time() - 3 * 86400
                for f in (root / "state_old" / "z_old.done", root / "jobs" / "old.txt"):
                    os.utime(f, (old, old))
                cfg = d / "m.toml"
                cfg.write_text(f"""
out_dir = "{d / 'out'}"
[[hosts]]
id = "here"
[[queues]]
id = "all"
root = "{root}"
jobs = "jobs/*.txt"
[[groups]]
id = "A"
prefix = "a_"
""")
                snap = C.collect(load(cfg))
            finally:
                kill_procs(lock, cur)
            lanes = {ln["id"]: ln for ln in snap["lanes"]}
            self.assertEqual(sorted(lanes), ["all:new", "all:q"])                 # old 久无活动、copy 从没启动，都隐藏
            self.assertEqual((lanes["all:new"]["label"], lanes["all:q"]["label"]), ("车道 1", "车道 2"))   # 隐藏的不占号
            self.assertEqual(lanes["all:new"]["total"], 2)
            jobs = {j["id"]: j for j in snap["jobs"]}
            self.assertEqual((jobs["a_run"]["group"], jobs["b_next"]["group"], jobs["c_one"]["group"]), ("A", "b_", "c_"))
            g = {x["id"]: x for x in snap["groups"]}
            self.assertEqual([x["id"] for x in snap["groups"]], ["A", "c_", "b_"])                  # 自动归组按首次出现排序
            self.assertEqual((g["c_"]["label"], g["c_"]["auto"], g["c_"]["queued"]), ("c_*", True, 2))
            self.assertNotEqual(g["b_"]["color"], g["c_"]["color"])
            self.assertEqual((lanes["all:new"]["root"], lanes["all:new"]["queue"]), (str(root), "jobs/new.txt"))   # 页面按它认本车道的事件

    def test_skip_explicit_on_other_host(self):
        """共享盘：另一台主机显式登记的清单，本机自动发现时跳过。"""
        from labmon import probe as P
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "jobs").mkdir()
            for n in ("a", "b"):
                (Path(d) / "jobs" / f"{n}.txt").write_text("x|y\n")
            qs = [{"id": "auto", "root": d, "jobs": "jobs/*.txt"}]
            got = [q["id"] for q in P.expand_queues(qs, [str(Path(d) / "jobs" / "a.txt")])]
            self.assertEqual(got, ["auto:b"])


class SuspendTest(unittest.TestCase):
    def test_suspend_time_excluded(self):
        """连续两次采集都挂起：间隔记为挂起时长；恢复后不再累计；换了一次运行重新计。"""
        susp, j = {}, {"start_ts": 1000.0, "suspended": True}
        C._suspend_track(susp, "q/x", j, 2000)
        C._suspend_track(susp, "q/x", j, 2120)
        j["suspended"] = False
        sr = C._suspend_track(susp, "q/x", j, 2240)
        self.assertEqual(sr["s"], 120)
        j.update(suspended=True, start_ts=3000.0)
        self.assertEqual(C._suspend_track(susp, "q/x", j, 3100)["s"], 0)
        j.update(start_ts=4000.0, log_mtime=4500.0)               # 刚发现挂起：从日志最后更新算起
        self.assertEqual(C._suspend_track(susp, "q/x", j, 5000)["s"], 500)


class NoProgressTest(unittest.TestCase):
    """#68：noprogress 只报“超了折算估时、已跑满 NOPROG_MIN_S、这一次一直没见过进度行”的运行中任务。"""
    T = 100000.0

    def setUp(self):
        self.work = {"prog": {}, "segs": {}, "multi": set(), "susp": {}, "ratios": {}}

    def keys(self, el_min, t=None, **kw):
        t = t or self.T
        kw2 = {k: kw.pop(k) for k in ("fresh", "silent") if k in kw}
        j = {"id": "x", "lane": "q", "status": "running", "start_ts": kw.pop("start", self.T - el_min * 60), "est_min": 60.0,
             "dur": None, "plan": None, "hold": False, "group": None, "log_mtime": t - 30, **kw}
        alerts = []
        C._job_times(j, "q", "p", {"log_silent_min": kw2.get("silent", 0)}, self.work, alerts, t, "29 车道 1", fresh=kw2.get("fresh", True))
        return [a["key"].rsplit(":", 1)[0] if a["key"].endswith(f":{int(j['start_ts'])}") else a["key"] for a in alerts]   # 去掉 key 末尾的开跑时刻（#129）

    def test_key_has_run_start(self):
        """#129：key 带这一次的开跑时刻，重跑后又卡住是新的一件；开跑时刻只是这一轮的现在（日志与 current 都没有）时不带，免得每轮变。"""
        j = {"id": "x", "lane": "q", "status": "running", "start_ts": self.T - 7200, "est_min": 60.0, "dur": None, "plan": None,
             "hold": False, "group": None, "log_mtime": self.T - 3600}
        alerts = []
        C._job_times(dict(j), "q", "p", {"log_silent_min": 10}, self.work, alerts, self.T, "29 车道 1")
        self.assertEqual([a["key"] for a in alerts], [f"noprogress:x:{int(self.T - 7200)}", f"log_silent:x:{int(self.T - 7200)}"])
        alerts = []
        C._job_times(dict(j, start_ts=self.T + 7200), "q", "p", {"log_silent_min": 10}, self.work, alerts, self.T + 7200, "29 车道 1")
        self.assertEqual([a["key"] for a in alerts], ["log_silent:x"])

    def test_rewake_after_rerun(self):
        """#129：重跑（开跑时刻变了）后又卡住是新的一件，6 小时内也叫醒；同一次运行这一轮读失败没报、下一轮又报，不叫醒。"""
        from labmon.waits import _new_alerts
        sf = Path(tempfile.mkdtemp()) / "w.json"

        def woke(start, t, fresh=True):
            j = {"id": "x", "lane": "q", "status": "running", "start_ts": start, "est_min": 60.0, "dur": None, "plan": None,
                 "hold": False, "group": None, "log_mtime": t - 3600}
            alerts = []
            C._job_times(j, "q", "p", {"log_silent_min": 10}, self.work, alerts, t, "29 车道 1", fresh=fresh)
            return sorted(k.split(":")[0] for k in _new_alerts(sf, {a["key"]: a["msg"] for a in alerts}, t))
        T = self.T
        self.assertEqual(woke(T - 7200, T), ["log_silent", "noprogress"])
        self.assertEqual(woke(T - 7200, T + 300, fresh=False), [])
        self.assertEqual(woke(T - 7200, T + 600), [])                      # 同一次：不叫醒
        self.assertEqual(woke(T + 900, T + 900 + 7200), ["log_silent", "noprogress"])   # 重跑后又卡住

    def test_reported(self):
        self.assertEqual(self.keys(120), ["noprogress:x"])
        self.assertEqual(self.keys(120, dur={"min": 150.0, "src": "同类"}), [])       # 折算估时还没超
        self.assertEqual(self.keys(120, dur={"min": 90.0, "src": "同类"}), ["noprogress:x"])

    def test_not_reported(self):
        self.assertEqual(self.keys(120, progress=[3, 10]), [])                         # 有进度行
        self.assertEqual(self.keys(20, est_min=5.0), [])                               # 短任务：不到 30 分钟
        self.assertEqual(self.keys(120, est_min=None), [])                             # 无估时
        self.assertEqual(self.keys(120, hold=True), [])                                # 占位任务
        self.assertEqual(self.keys(120, suspended=True), [])                           # 挂起

    def test_stale_host_data(self):
        """#86：这台探针这一轮失败、车道用上一次的数据时，已用时间与日志静默照常长：noprogress、log_silent 都不报。"""
        self.assertEqual(self.keys(120, silent=10, log_mtime=self.T - 3600), ["noprogress:x", "log_silent:x"])
        self.assertEqual(self.keys(120, silent=10, log_mtime=self.T - 3600, fresh=False), [])

    def test_seen_this_run(self):
        """这一次见过进度行、后来滚出日志末尾读不到了：不报；重跑（开始时刻变了）重新算。"""
        start = self.T - 50 * 60
        self.assertEqual(self.keys(0, t=self.T, start=start, progress=[1, 10]), [])
        self.assertEqual(self.keys(0, t=self.T + 3600, start=start), [])
        self.assertEqual(self.keys(0, t=self.T + C.PROG_KEEP + 3600, start=start), [])        # 跑满 PROG_KEEP、采样已修剪：只靠 seen 压住
        self.assertEqual(self.work["prog"].get("q/x"), [])
        self.assertEqual(self.keys(0, t=self.T + C.PROG_KEEP + 3 * 3600, start=self.T + C.PROG_KEEP + 3600), ["noprogress:x"])


class ProgressTest(unittest.TestCase):
    def test_reset_on_new_segment(self):
        prog, segs = {}, {}
        j = {"lane": "q", "id": "x", "progress": [5, 10], "log_mtime": 100, "start_ts": 50}
        C._progress_samples(prog, segs, j, 100)
        j.update(progress=[2, 20], log_mtime=200)                       # 总步数变了：换了一段
        ss, seg = C._progress_samples(prog, segs, j, 200)
        self.assertEqual(ss, [[200, 2, 20]])
        self.assertEqual((seg["t0"], seg["i0"], seg["k"]), (200, 2, 2))
        j.update(progress=[2, 20], log_mtime=300)                       # 没前进：不重复记
        self.assertEqual(len(C._progress_samples(prog, segs, j, 300)[0]), 1)
        self.assertIsNone(C._recent_rate(prog["q/x"], 300))
        j.update(start_ts=400, progress=[1, 10], log_mtime=450)         # 重跑：第一段从任务开始算
        ss, seg = C._progress_samples(prog, segs, j, 450)
        self.assertEqual((ss, seg["t0"], seg["k"]), ([[450, 1, 10]], 400, 1))

    def test_multi_segment_eta(self):
        """照 A1 x1e2e_discrete 的结构：训练 49 步（18 s/步）→ 核对审计 20 分钟不打进度 → 另一组训练 49 步（6 s/步）
        → 再审计 10 分钟。真实总时长约 50 分钟，预计时长 50 分钟。每 2 分钟采集一次，看预计结束时刻是否稳定。"""
        phases = [("train", 49, 18), ("audit", 0, 1200), ("train", 49, 6), ("audit", 0, 600)]
        timeline, t0 = [], 0.0                       # (时刻, 进度) 日志里最近一次 PROGRESS 及其写入时刻
        for kind, n, step in phases:
            if kind == "train":
                for i in range(1, n + 1):
                    timeline.append((t0 + i * step, [i, n]))
                t0 += n * step
            else:
                t0 += step
        end = t0
        for known in (False, True):                  # 第一次跑（还不知道分段）与同组已见过分段之后
            self.check_run(timeline, end, known)

    def check_run(self, timeline, end, known):
        prog, segs, ends = {}, {}, []
        for t in range(120, int(end), 120):
            last = [x for x in timeline if x[0] <= t]
            if not last:
                continue
            ts, pr = last[-1]
            j = {"lane": "q", "id": "x1", "progress": pr, "log_mtime": ts, "start_ts": 0.0}
            ss, seg = C._progress_samples(prog, segs, j, t)
            multi = known or seg["k"] > 1 or pr[0] >= pr[1]
            r = C._segment_eta(pr, ss, seg, float(t), 50.0, multi, float(t))
            self.assertIsNotNone(r, t)
            ends.append((t, t + r[0], r[1]))
        first_seg = 49 * 18
        for t, e, src in ends:
            self.assertLess(e, 1.35 * end, (known, t, e / 60, src))   # 不再跳到两倍多（原来换段时约 129 分钟）
            if known or t > first_seg:                                 # 第一次跑的第一段只能看到这一段
                self.assertGreater(e, 0.8 * end, (known, t, e / 60, src))
        self.assertTrue(any("不打进度" in src for _, _, src in ends))

    def test_progress_shapes(self):
        """Issue #14：第一段按进度外推的预计结束，几种打进度的形态下既不过晚也不过早（真实结束的 0.8～1.35 倍）。
        每 2 分钟采集一次；估时取真实时长的 0.86（照现场：估 139.4 分、实际约 162 分）。形态：
        - 构造带较长准备阶段的任务：开头约 102 分钟不打进度，之后每 400 步约 68 秒（约 355 步/分），共 21050 步
          （原来按任务开始外推，第一批进度把预计结束推到 31～88 小时后）；
        - 同上，但开头先打一行 PROGRESS 0/21050 再静默；
        - 没有开头静默、进度行比采集周期稀：每轮 10 分钟共 12 轮、每轮 45 分钟共 8 轮；
        - 开头静默 102.5 分钟后每 35 分钟一行，共 8 行。"""
        shapes = {"静默后密集": (6150.0, 400 * 60 / 355, 400, 21050, False),
                  "先打 0/N 再静默": (6150.0, 400 * 60 / 355, 400, 21050, True),
                  "每轮 10 分": (0.0, 600.0, 1, 12, False),
                  "每轮 45 分": (0.0, 2700.0, 1, 8, False),
                  "静默后每 35 分": (6150.0, 2100.0, 1, 8, False)}
        for name, (quiet, step, per, n, zero) in shapes.items():
            end = quiet + n / per * step
            prog, segs, ends = {}, {}, []
            for t in range(120, int(end), 120):
                k = int((t - quiet) // step) if t > quiet else 0
                j = {"lane": "q", "id": "x", "start_ts": 0.0, "log_mtime": quiet + k * step if k else (1.0 if zero else t - 30.0)}
                if k or zero:
                    j["progress"] = [per * k, n]
                ss, seg = C._progress_samples(prog, segs, j, t)
                r = C._segment_eta(j.get("progress"), ss, seg, float(t), 0.86 * end / 60, False, float(t))
                if k:
                    ends.append((t, t + r[0], r[1]))
            with self.subTest(name):
                self.assertGreater(len(ends), 3)
                for t, e, src in ends:
                    self.assertLess(e, 1.35 * end, (t / 60, e / 60, src))
                    self.assertGreater(e, 0.8 * end, (t / 60, e / 60, src))

    def test_progress_overrun_or_no_estimate(self):
        """Issue #17：第一段只有一个步数 > 0 的采样、又已超估时或没有估时，剩余按 overrun_left（已用的 20%、至少 10 分钟，
        与 worker 的 run_end 同一口径），不再按已用时间外推（开头静默 102 分、估时 100 分时原来这一轮预计 5473 分）。
        有了第二个采样就按平均速度，回到真实结束的 0.8～1.35 倍。每 2 分钟采集一次；估时取第一行进度时刻的 0.9（已超）或没有。"""
        shapes = {"静默后密集": (6150.0, 400 * 60 / 355, 400, 21050, False),
                  "先打 0/N 再静默": (6150.0, 400 * 60 / 355, 400, 21050, True),
                  "静默后每 35 分": (6150.0, 2100.0, 1, 8, False),
                  "每轮 45 分": (0.0, 2700.0, 1, 8, False)}
        for name, (quiet, step, per, n, zero) in shapes.items():
            end = quiet + n / per * step
            for floor in (0.9 * (quiet + step) / 60, None):
                prog, segs, single, later = {}, {}, [], []
                for t in range(120, int(end), 120):
                    k = int((t - quiet) // step) if t > quiet else 0
                    j = {"lane": "q", "id": "x", "start_ts": 0.0, "log_mtime": quiet + k * step if k else (1.0 if zero else t - 30.0)}
                    if k or zero:
                        j["progress"] = [per * k, n]
                    ss, seg = C._progress_samples(prog, segs, j, t)
                    r = C._segment_eta(j.get("progress"), ss, seg, float(t), floor, False, float(t))
                    if k:
                        (single if k == 1 else later).append((t, t + r[0], r[1]))
                with self.subTest(name, floor=floor):
                    self.assertTrue(single and len(later) > 3)
                    for t, e, src in single:                              # 只有一个采样：剩余 max(10 分钟, 已用的 20%)，不出尖峰
                        self.assertEqual((src, e), ("剩余按已用的 20%（进度刚开始）", t + max(600.0, 0.2 * t)), t / 60)
                        self.assertLess(e, 1.35 * end, (t / 60, e / 60))
                    for t, e, src in later:
                        self.assertLess(e, 1.35 * end, (t / 60, e / 60, src))
                        self.assertGreater(e, 0.8 * end, (t / 60, e / 60, src))

    def test_overrun_without_progress_not_now(self):
        """#138：估 60 分钟、已跑 90 分钟，进度停了（或一直没有进度行）：剩余按 overrun_left（已用的 20%、至少 10 分钟，同 worker 的 run_end），
        不写 0。以前预计结束就是这一轮的现在，车道对齐后下一条的预计开始也是现在，每轮往后滑。"""
        t, el = 10000.0, 5400.0
        ss = [[t - el + 600, 10, 100], [t - el + 900, 20, 100]]                  # 进度最后一次是 75 分钟前：停了
        self.assertEqual(C._segment_eta([20, 100], ss, None, el, 60.0, False, t), (0.2 * el, "已超估时", None))
        self.assertEqual(C._segment_eta([20, 100], ss, None, 3000.0, 60.0, False, t)[:2], (600.0, "估时（这段不打进度）"))   # 没超：估时 − 已用
        work = {"ratios": {}, "susp": {}, "prog": {}, "segs": {}, "multi": set()}
        al = {"log_silent_min": 0}
        run = {"id": "a", "lane": "q", "group": None, "status": "running", "hold": False, "est_min": 60, "start_ts": t - el,
               "dur": {"min": 60.0, "src": "清单估时"}, "plan": {"t0": t - 3600, "t1": t}}
        nxt = {"id": "b", "lane": "q", "group": None, "status": "queued", "hold": False, "est_min": 30, "dur": None, "plan": {"t0": t, "t1": t + 1800}}
        for j in (run, nxt):
            C._job_times(j, "q", "p", al, work, [], t, "q")
        self.assertEqual((run["eta_source"], run["eta_ts"], round(run["overrun_min"])), ("已超估时", t + 0.2 * el, 30))
        C._queued_times([run, nxt], {"a": {"t0": t - 3600, "t1": t}, "b": {"t0": t, "t1": t + 1800}}, t, "p")
        self.assertEqual((nxt["plan_start_ts"], nxt["eta_ts"]), (t + 0.2 * el, t + 0.2 * el + 1800))   # 接着它的那条跟着挪，不是“现在”

    def test_progress_stalled_no_estimate(self):
        """Issue #17：第一段“停了一阵”（打了一阵进度后进入不打进度的阶段，如中途评估）、又没有估时，平均速度从第一个步数 > 0 的
        采样算起，不从任务开始算（开头静默不算进去）；只有一个这样的采样时按 overrun_left。
        形态：开头静默 102.5 分，之后约 355 步/分打到 8000 步，评估 30 分钟不打进度，再接着打到 21050 步。"""
        quiet, step, n, stop, pause = 6150.0, 400 * 60 / 355, 21050, 20, 1800.0   # stop：评估前打到第几行（每行 400 步）
        last = quiet + stop * step                                        # 评估前最后一行进度
        end = last + pause + (n / 400 - stop) * step
        for zero in (False, True):
            prog, segs, ends = {}, {}, []
            for t in range(120, int(end), 120):
                if t <= last:
                    k = int((t - quiet) // step) if t > quiet else 0
                    ts = quiet + k * step
                else:
                    k = stop + (int((t - last - pause) // step) if t > last + pause else 0)
                    ts = last + pause + (k - stop) * step if k > stop else last
                j = {"lane": "q", "id": "x", "start_ts": 0.0, "log_mtime": ts if k else (1.0 if zero else t - 30.0)}
                if k or zero:
                    j["progress"] = [400 * k, n]
                ss, seg = C._progress_samples(prog, segs, j, t)
                r = C._segment_eta(j.get("progress"), ss, seg, float(t), None, False, float(t))
                if k:
                    ends.append((t, t + r[0], r[1]))
            with self.subTest(zero=zero):
                self.assertGreater(sum(src == "进度（停了一阵）" for _, _, src in ends), 5)
                for t, e, src in ends[1:]:                                # 第一轮只有一个采样（上一个用例测）
                    self.assertLess(e, 1.35 * end, (t / 60, e / 60, src))
                    self.assertGreater(e, 0.8 * end, (t / 60, e / 60, src))
        # 先打 0/N、再只打了一行就停了一阵：只有一个步数 > 0 的采样，按 overrun_left
        prog, segs = {}, {}
        for t, i, mt in ((120, 0, 1.0), (6240, 400, 6200.0), (7200, 400, 6200.0)):
            ss, seg = C._progress_samples(prog, segs, {"lane": "q", "id": "x", "start_ts": 0.0, "log_mtime": mt, "progress": [i, n]}, t)
        self.assertEqual(C._segment_eta([400, n], ss, seg, 7200.0, None, False, 7200.0), (1440.0, "剩余按已用的 20%（进度刚开始）", None))

    def test_progress_pruned(self):
        """日志 PROG_KEEP（6 小时）以上没更新、采样修剪空了、进度还在：交给估时等后备（返回 None）。
        后续段原来在 samples[-1] 处抛 IndexError（整轮采集挂掉）；第一段不再写成“进度刚开始”（PR #18 审查建议 2、3）。"""
        t = 100000.0
        for seg0, pr in (({"run": 0.0, "t0": 1000.0, "i0": 5, "k": 2}, [50, 100]), ({"run": 0.0, "t0": 0.0, "i0": 0, "k": 1}, [50, 100])):
            for floor in (None, 100.0, 3000.0):
                prog, segs = {"q/x": [[1000.0, 5, 100]]}, {"q/x": dict(seg0)}
                ss, seg = C._progress_samples(prog, segs, {"lane": "q", "id": "x", "start_ts": 0.0, "log_mtime": t - 7 * 3600, "progress": pr}, t)
                with self.subTest(k=seg0["k"], floor=floor):
                    self.assertEqual(ss, [])
                    self.assertIsNone(C._segment_eta(pr, ss, seg, t, floor, False, t))

    def test_speed_eta_anchored_at_last_sample(self):
        """#140：日志每 10 分钟一行进度，两行之间的几轮采集预计结束不变（剩余从最后一个采样起算，不是从这一轮起算）。"""
        ss = [[600.0 * k, 10 * k, 1000] for k in range(1, 6)]          # 每 600 秒 10 步：近期速度与平均速度都是 1/60 步/秒
        seg = {"t0": 600.0, "i0": 10, "k": 1}
        end = 3000.0 + 950 * 60
        for t in (3000.0, 3200.0, 3500.0):
            with self.subTest(t=t):
                r = C._segment_eta([50, 1000], ss, seg, t, None, False, t)
                self.assertEqual(r[1], "近期速度")
                self.assertAlmostEqual(t + r[0], end, delta=1)
        # 近 30 分钟里只剩最后一个采样（没有近期速度）、还没算停了一阵时按平均速度，同样从最后一个采样起算
        for t in (4300.0, 4500.0, 4800.0):
            with self.subTest(t=t):
                r = C._segment_eta([50, 1000], ss, seg, t, None, False, t)
                self.assertEqual(r[1], "进度")
                self.assertAlmostEqual(t + r[0], end, delta=1)

    def test_speed_eta_passed_not_zero(self):
        """#140 与 #138 同一口径：外推的结束已过、任务还在跑（没到停了一阵）时剩余不写 0，剩下的步数按这个速度从现在起算。"""
        ss = [[60.0 * k, 10 * k, 100] for k in range(1, 10)]           # 每分钟 10 步，最后一次 90/100 在 540 秒
        seg = {"t0": 60.0, "i0": 10, "k": 1}
        r = C._segment_eta([90, 100], ss, seg, 1000.0, None, False, 1000.0)   # 外推 600 秒跑完，此刻 1000 秒仍在跑
        self.assertEqual(r[1], "近期速度")
        self.assertAlmostEqual(r[0], 60.0, delta=1)

    def test_speed_eta_skips_suspended_after_sample(self):
        """#140 审查：最后一个采样之后挂起了 5 分钟，这段不算在跑，预计结束跟着往后挪 5 分钟（剩余不因挂起变少）。"""
        ss = [[600.0 * k, 10 * k, 100] for k in range(1, 3)]            # 每 10 分钟 10 步，此刻 20/100
        seg = {"t0": 600.0, "i0": 10, "k": 1}
        r0 = C._segment_eta([20, 100], ss, seg, 1200.0, None, False, 1200.0)
        r = C._segment_eta([20, 100], ss, seg, 1500.0, None, False, 1500.0, paused=300.0)
        self.assertAlmostEqual(r0[0], 80 * 60, delta=1)
        self.assertAlmostEqual(r[0], 80 * 60, delta=1)
        # 经 _progress_samples 记下的 sus0：新采样那一轮的累计挂起，之后又挂起的才扣
        prog, segs, j = {}, {}, {"lane": "q", "id": "x", "start_ts": 0.0, "log_mtime": 600.0, "progress": [10, 100]}
        C._progress_samples(prog, segs, j, 600.0, 30.0)
        _, sg = C._progress_samples(prog, segs, dict(j, log_mtime=1200.0, progress=[20, 100]), 1200.0, 30.0)
        self.assertEqual(sg["sus0"], 30.0)
        _, sg = C._progress_samples(prog, segs, dict(j, log_mtime=1200.0, progress=[20, 100]), 1500.0, 330.0)
        self.assertEqual(sg["sus0"], 30.0)

    def test_single_segment_keeps_progress(self):
        # 单段任务按进度估得比估时早：仍用进度
        ss = [[100.0 * k, k, 100] for k in range(1, 41)]
        r = C._segment_eta([40, 100], ss, {"t0": 0.0, "i0": 0, "k": 1}, 4000.0, 100.0, False, 4000.0)
        self.assertEqual(r[1], "近期速度")
        self.assertAlmostEqual(r[0], 6000.0, delta=1)


class ParseGpuQueueTest(unittest.TestCase):
    """探针给的原始队列数据 → 车道与任务：done / failed（被信号终止的记 killed）、在跑（进度、挂起、开始时刻）、排队（推演）；
    锁活着时 paused 不算；worker 在管的车道带等待原因与挡住它的指令。"""

    T = 1_800_000_000

    def raw(self, **kw):
        t = self.T
        r = {"jobs": [{"name": n, "est_min": e} for n, e in (("d", 10), ("f", 5), ("k", 5), ("r", 30), ("q", 20), ("n", None))],
             "marks": {"d.done": t - 100, "f.failed": t - 50, "k.failed": t - 40}, "state_dir": "/s", "worker": None, "lock_alive": True, "lock_pid": 7,
             "paused": "有意暂停", "current": {"name": "r", "pid": 8, "t0": parse_time("2027-01-01 10:00:00"), "alive": True, "stopped": False,
                                              "progress_line": "PROGRESS 3/12", "last_line": "step 3", "log_mtime": t - 5},
             "log_tail": ["2027-01-01 09:00:00 开始 d", "2027-01-01 09:10:00 完成 d（耗时 10 分）", "2027-01-01 09:20:00 开始 f",
                          "2027-01-01 09:21:00 失败 f（退出码 1）", "2027-01-01 09:30:00 开始 k", "2027-01-01 09:31:00 失败 k（退出码 143）",
                          "2027-01-01 10:00:00 开始 r"]}
        r.update(kw)
        return r

    def test_statuses(self):
        t = self.T
        lane, jobs = C.parse_gpu_queue(self.raw(), t)
        j = {x["id"]: x for x in jobs}
        self.assertEqual([x["status"] for x in jobs], ["done", "failed", "failed", "running", "queued", "queued"])
        self.assertEqual((j["d"]["start_ts"], j["d"]["end_ts"]), (parse_time("2027-01-01 09:00:00"), parse_time("2027-01-01 09:10:00")))
        self.assertEqual((j["f"].get("killed"), j["k"]["killed"]), (None, 143))
        self.assertEqual((j["r"]["start_ts"], j["r"]["progress"], j["r"]["last_line"], j["r"]["log_mtime"]),
                         (parse_time("2027-01-01 10:00:00"), [3, 12], "step 3", t - 5))
        self.assertNotIn("suspended", j["r"])
        self.assertEqual((j["q"]["est_min"], j["q"]["dur"], j["q"]["plan"], j["n"]["est_min"]), (20, None, None, None))
        self.assertEqual(lane, {"alive": True, "pid": 7, "state_dir": "/s", "logs_dir": None, "paused": None, "managed": False, "waiting": None, "held": None, "held_t": None, "dep": None, "head": None, "stuck": None, "cpu_wait": None})

    def test_worker_and_edge_cases(self):
        t = self.T
        wk = {"wait": "没有可用显存", "held": "drain", "dur": {"q": {"min": 12.0, "src": "同类"}}, "plan": {"q": {"t0": t + 60, "t1": t + 800, "gpus": [0]}}, "stuck": None}
        cur = {"name": "r", "pid": 8, "t0": None, "alive": True, "stopped": True, "progress_line": "PROGRESS 0/0", "last_line": None, "log_mtime": None}
        lane, jobs = C.parse_gpu_queue(self.raw(worker=wk, lock_alive=False, current=cur, log_tail=[], marks={"d.done": t - 100}), t)
        j = {x["id"]: x for x in jobs}
        self.assertEqual((lane["alive"], lane["paused"], lane["managed"], lane["waiting"], lane["held"]), (False, "有意暂停", True, "没有可用显存", "drain"))
        self.assertEqual((j["d"]["status"], j["d"]["end_ts"], j["d"]["start_ts"]), ("done", t - 100, None))     # 日志里没有：按标记文件的时间
        self.assertEqual((j["r"]["status"], j["r"]["start_ts"], j["r"]["suspended"], j["r"].get("progress")), ("running", t, True, None))   # 0/0 不算进度
        self.assertEqual((j["q"]["dur"], j["q"]["plan"]), ({"min": 12.0, "src": "同类"}, {"t0": t + 60, "t1": t + 800, "gpus": [0]}))
        _, jobs = C.parse_gpu_queue(self.raw(current=dict(cur, t0=t - 300), log_tail=[]), t)
        self.assertEqual({x["id"]: x for x in jobs}["r"]["start_ts"], t - 300)                              # 日志里没有：取探针从 current 换好的 epoch
        _, jobs = C.parse_gpu_queue(self.raw(current=dict(cur, alive=False)), t)
        self.assertEqual({x["id"]: x["status"] for x in jobs}["r"], "queued")                                 # 进程不在：当前任务只算排队


class LaneSpecsTest(unittest.TestCase):
    """配置里的队列 → 车道规格：同机照配置顺序叫“车道 N”、接用途；通配符队列按探针发现的清单展开，没启动过或跑完久无活动的隐藏
    （worker 在管不单独算活着，#117），隐藏的不占号；已归还的主机上发现的清单不按锁算活着。"""

    def test_names_and_discover(self):
        t = 1_800_000_000
        hosts = [{"id": "h1", "returned": None}, {"id": "h2", "returned": None}, {"id": "h3", "returned": "11-01"}]
        q = lambda i, host, **kw: {"id": i, "host": host, "type": "gpu_queue", "discover": False, "hide_done_hours": 24, **kw}   # noqa: E731
        cfg = {"hosts": hosts, "queues": [q("q1", "h1", label="训练"), q("q2", "h2"), q("all", "h1", discover=True), q("far", "h3", discover=True, read_via=None)]}
        raw = {"h1": {"queues": {"all:new": {"lock_alive": False, "worker": None, "activity": t - 1000},
                                 "all:old": {"lock_alive": False, "worker": None, "activity": t - 2 * 86400},
                                 "all:never": {"lock_alive": False, "worker": None, "activity": None},
                                 "all:managed": {"lock_alive": False, "worker": {"wait": "等"}, "activity": None, "jobs": [{"name": "x"}]},
                                 "all:mdone": {"lock_alive": False, "worker": {"wait": None}, "activity": t - 2 * 86400, "jobs": [{"name": "x"}], "marks": {"x.done": t - 2 * 86400}},
                                 "all:live": {"lock_alive": True, "worker": None, "activity": t - 3 * 86400},
                                 "other:x": {"lock_alive": True, "worker": None, "activity": t}}},
               "h3": {"queues": {"far:a": {"lock_alive": True, "worker": None, "activity": t - 3 * 86400}, "far:b": {"lock_alive": True, "worker": None, "activity": t}}}}
        out, last, hid = C._lane_specs(cfg, raw, t)
        self.assertEqual((last, hid), (t - 2 * 86400, []))          # 收起的里最晚的一次活动（_no_work 用）；自动发现的收起不进 quiet                       # 收起的里最晚的一次活动（_no_work 用）
        self.assertEqual([(x["id"], x["host"], x["name"]) for x in out],
                         [("q1", "h1", "车道 1 训练"), ("q2", "h2", "车道 1"), ("all:live", "h1", "车道 2"), ("all:managed", "h1", "车道 3"), ("all:new", "h1", "车道 4"),
                          ("far:b", "h3", "车道 1")])
        self.assertEqual(C._lane_specs({"hosts": hosts, "queues": [q("all", "h1", discover=True)]}, {}, t), ([], 0, []))    # 探针没给：一条也没有
        # 只在登记里有的空清单从没启动过：收起车道的最后活动与收起判定同一口径，取登记时刻（Codex 审查 #119：不然“暂时没实验”的起点每轮刷新成现在）
        reg = {"hosts": hosts, "queues": [q("r/jobs/r.txt", "h1", reg=True, reg_t=t - 30 * 3600, root="/d/r", jobs="jobs/r.txt"),
                                          q("r/jobs/s.txt", "h1", reg=True, reg_t=t - 3600, root="/d/r", jobs="jobs/s.txt")]}
        raw = {"h1": {"queues": {"r/jobs/r.txt": {"lock_alive": False, "worker": {"wait": None}, "activity": None, "jobs": []},
                                 "r/jobs/s.txt": {"lock_alive": False, "worker": {"wait": None}, "activity": None, "jobs": []}}}}
        out, last, hid = C._lane_specs(reg, raw, t)
        self.assertEqual(([x["id"] for x in out], last), (["r/jobs/s.txt"], t - 30 * 3600))     # 刚登记的空清单留着
        self.assertEqual(hid, [{"host": "h1", "root": "/d/r", "queue": "jobs/r.txt", "last": t - 30 * 3600, "idle": False}])   # 收起的登记车道进快照的 quiet：管理抽屉不列（#121）
        # idle（#47）：登记这一轮还在（reg_live）、空了 IDLE_REG_S 以上才算；登记没了、按 REG_KEEP_S 留着的不算（已经 worker rm 了）
        for live, age, idle in ((True, 3 * 86400, True), (True, 30 * 3600, False), (False, 3 * 86400, False)):
            reg["queues"][0].update(reg_live=live, reg_t=t - age)
            self.assertEqual(C._lane_specs(reg, raw, t)[2][0]["idle"], idle, (live, age))


class ConfigTest(unittest.TestCase):
    def test_markers(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.toml"
            p.write_text('[[markers]]\ndate = 2026-11-17\n[[markers]]\ndate = "2026-11-10 09:30"\nlabel = "注册"\n')
            ms = load(p)["markers"]
            self.assertEqual((ms[0]["label"], ms[1]["label"]), ("2026-11-17", "注册"))
            self.assertEqual(dt.datetime.fromtimestamp(ms[1]["ts"]).strftime("%H:%M"), "09:30")
            p.write_text('[[markers]]\ndate = "11/17"\n')
            with self.assertRaises(ConfigError):
                load(p)

    def test_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.toml"
            p.write_text('title = "x"\n')
            self.assertEqual(load(p)["hosts"], [])           # [[queues]] 可选（车道按 worker 登记，#94）：都没写时不补本机
            p.write_text('title = "x"\n[[queues]]\nid = "q"\nroot = "/r"\njobs = "j.txt"\n')
            cfg = load(p)                                     # 不写主机的队列缺省在本机
            self.assertEqual((cfg["hosts"][0]["id"], cfg["queues"][0]["host"]), ("local", "local"))
            self.assertIsNone(cfg["hosts"][0]["ssh"])


if __name__ == "__main__":
    unittest.main()
