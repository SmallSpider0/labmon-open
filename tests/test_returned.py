"""已归还主机、超估时显示与共享盘时钟偏差的离线测试。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import fake_status, kill_procs, make_queue, queue_procs, running_job, waiting_job, write_config  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon.config import load, parse_time, set_returned  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.lock, self.cur = queue_procs()
        self.old = os.environ.get("LABMON_HOSTS")
        os.environ["LABMON_HOSTS"] = str(self.d / "hosts.toml")
        self.old_state = os.environ.get("LABMON_STATE")
        os.environ["LABMON_STATE"] = str(self.d / "state")              # 改主机清单会记进记录：别写进真的状态目录

    def tearDown(self):
        kill_procs(self.lock, self.cur)
        if self.old is None:
            os.environ.pop("LABMON_HOSTS", None)
        else:
            os.environ["LABMON_HOSTS"] = self.old
        if self.old_state is None:
            os.environ.pop("LABMON_STATE", None)
        else:
            os.environ["LABMON_STATE"] = self.old_state
        self.tmp.cleanup()


class ReturnedTest(Base):
    def setup_hosts(self, shared: bool):
        sh = f'shared = ["{self.d}"]\n' if shared else ""
        (self.d / "hosts.toml").write_text(f"""
[[hosts]]
id = "here"
{sh}
[[hosts]]
id = "gone"
label = "借来的机器"
ssh = "nobody@invalid.example"
returned = "2026-11-04 18:40"
{sh}
[[hosts.checks]]
id = "keepalive"
cmd = "false"
when_idle = "保活不在"
""")
        root = self.d / "proj"
        make_queue(root, self.lock.pid, self.cur.pid)
        (self.d / "monitor.toml").write_text(f"""
out_dir = "{self.d / 'out'}"
[[hosts]]
id = "here"
[[queues]]
id = "q"
host = "gone"
root = "{root}"
jobs = "jobs/q.txt"
""")
        return load(self.d / "monitor.toml")

    def test_read_via_shared_disk(self):
        cfg = self.setup_hosts(shared=True)
        self.assertEqual(cfg["queues"][0]["read_via"], "here")
        snap = C.collect(cfg)
        gone = next(h for h in snap["hosts"] if h["id"] == "gone")
        self.assertEqual((gone["returned"], gone["error"]), (parse_time("2026-11-04 18:40"), "已归还"))   # 没有去连；归还时刻是 epoch 秒
        lane = snap["lanes"][0]
        self.assertEqual((lane["returned"], lane["read_via"], lane["alive"]), (parse_time("2026-11-04 18:40"), "here", False))
        jobs = {j["id"]: j["status"] for j in snap["jobs"]}
        self.assertEqual(jobs["a_done"], "done")
        self.assertEqual(jobs["a_run"], "queued")          # 进程在别的机器上，这里不当它在跑
        keys = [a["key"] for a in snap["alerts"]]
        self.assertEqual(keys, [f"returned_unfinished:q:gone:{int(parse_time('2026-11-04 18:40'))}"])   # 不报队列进程不在、连接失败或保活不在；key 带主机与归还时刻（#129）

    def test_no_shared_disk(self):
        cfg = self.setup_hosts(shared=False)
        self.assertIsNone(cfg["queues"][0]["read_via"])
        snap = C.collect(cfg)
        lane = snap["lanes"][0]
        self.assertTrue(lane["missing"])                    # 没有归还前的数据可显示
        self.assertEqual(snap["alerts"], [])

    def test_set_returned(self):
        hf = self.d / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "a"\nssh = "u@a"\n\n[[hosts]]\nid = "b"   # 借用\nssh = "u@b"\n\n'
                      '[[hosts.checks]]\nid = "k"\ncmd = "true"\n')
        set_returned("b", "2026-11-04 18:40")
        set_returned("b", "2026-11-05 09:00")                # 再标一次只改时间
        hs = {h["id"]: h for h in tomllib.loads(hf.read_text())["hosts"]}
        self.assertEqual(hs["b"]["returned"], "2026-11-05 09:00")
        self.assertNotIn("returned", hs["a"])
        self.assertEqual(hs["b"]["checks"][0]["id"], "k")
        self.assertEqual(hf.read_text().count("returned ="), 1)  # 只有一行，不重复写
        set_returned("b", None)
        self.assertNotIn("returned", tomllib.loads(hf.read_text())["hosts"][1])
        with self.assertRaises(Exception):
            set_returned("nope", "2026-11-04")


class OverrunAndSkewTest(Base):
    def setUp(self):
        super().setUp()
        self.root = self.d / "proj"
        make_queue(self.root, self.lock.pid, self.cur.pid)

    def collect(self):
        return C.collect(load(write_config(self.d, self.root, self.d / "out")))

    def test_overrun(self):
        jf = self.root / "jobs" / "q.txt"
        jf.write_text(jf.read_text().replace("a_run|echo b   # est_min=30", "a_run|echo b   # est_min=4"))
        (self.root / "logs" / "a_run.log").write_text("没有进度行\n")
        snap = self.collect()
        run = next(j for j in snap["jobs"] if j["id"] == "a_run")
        self.assertEqual(run["eta_source"], "已超估时")
        self.assertAlmostEqual(run["overrun_min"], 6, delta=0.5)   # 已跑 10 分、估时 4 分
        self.assertTrue(snap["lanes"][0]["overrun"])
        # #138：超了估时又没进度，剩余按 overrun_left（已用的 20%、至少 10 分钟，同 worker 的 run_end），不写 0（以前 eta_ts 就是这一轮的现在）
        self.assertAlmostEqual(run["remaining_min"], 10, delta=0.01)
        self.assertAlmostEqual(run["eta_ts"], snap["generated"] + 600, delta=1)

    def test_fs_skew_corrected(self):
        st = self.root / "state"
        now = time.time()
        last = (st / "queue.log").read_text().strip().splitlines()[-1][:19]
        t_last = parse_time(last)
        os.utime(st / "queue.log", (t_last + 180, t_last + 180))         # 盘的时钟快 3 分钟
        os.utime(self.root / "logs" / "a_run.log", (now + 180, now + 180))
        snap = self.collect()
        self.assertAlmostEqual(snap["lanes"][0]["fs_skew"], 180, delta=1)
        run = next(j for j in snap["jobs"] if j["id"] == "a_run")
        self.assertAlmostEqual(run["log_mtime"], now, delta=5)

    def test_done_mark_yields_to_queue(self):
        # 计划项的 done_when 已勾选，但组里还有任务在跑：以队列为准，不显示“完成”
        cfg = write_config(self.d, self.root, self.d / "out")
        (self.d / "tasks.md").write_text("- [x] A 已勾选\n")
        cfg.write_text(cfg.read_text().replace('prefix = "a_"', f'prefix = "a_"\ndone_when = {{ file = "{self.d / "tasks.md"}", pattern = "^- \\\\[x\\\\] A" }}'))
        gs = {g["id"]: g["status"] for g in C.collect(load(cfg))["groups"]}
        self.assertEqual(gs["A"], "运行中")


if __name__ == "__main__":
    unittest.main()


class AudienceTest(unittest.TestCase):
    """告警给谁看（#142）：受众随告警一起定（collect.alert，写错直接报错），页面上显眼列的只取给用户的（collect.user_alerts）。"""

    def test_alert_needs_audience(self):
        with self.assertRaises(ValueError):
            C.alert("x:1", "page", "m")
        al = [C.alert("a:1", "agent", "会话做"), C.alert("b:1", "user", "用户看"), C.alert("c:1", "both", "会话那份", "用户那份")]
        self.assertEqual(C.user_alerts(al), ["用户看", "用户那份"])
        self.assertEqual(al[1], {"key": "b:1", "to": "user", "msg": "用户看"})


class IdleCheckTest(Base):
    """保活不在、本项目没有车道在跑：卡上有别的进程时不报空闲关机风险。"""

    def snap(self, gpu_procs: list, disk_gb: float | None = None):
        """disk_gb：给 h 配一块 /data（告警线 50 GiB），探针报它还剩这么多。"""
        (self.d / "hosts.toml").write_text("")
        (self.d / "monitor.toml").write_text(f"""
out_dir = "{self.d / 'out'}"
[[hosts]]
id = "h"
{'mounts = [{ path = "/data", label = "数据盘", min_gb = 50 }]' if disk_gb is not None else ""}
[[hosts.checks]]
id = "keepalive"
cmd = "false"
when_idle = "保活不在"
""")
        cfg = load(self.d / "monitor.toml")
        probe = {"t": time.time(), "gpu": [{"util": 90, "mem_used": 21000}], "gpu_procs": gpu_procs,
                 "checks": [{"id": "keepalive", "state": "fail", "out": ""}]}
        if disk_gb is not None:
            probe["mounts"] = {"/data": {"total": 100 * 2 ** 30, "avail": disk_gb * 2 ** 30, "used": (100 - disk_gb) * 2 ** 30}}
        return C.build(cfg, {"h": (probe, None)}, self.d / "out")

    def test_disk_low_both(self):
        """磁盘余量低两边都要（#142）：页面上给用户“只剩多少”，给会话的那份写先清什么、删什么前先问用户。"""
        self.worker_status(waiting=[waiting_job("u", "out")])
        snap = self.snap([{"pid": 123, "mem": 21000.0, "cmd": ""}], disk_gb=12.5)
        a = next(x for x in snap["alerts"] if x["key"] == "disk_low:h:/data")
        self.assertEqual(a["to"], "both")
        self.assertEqual(a["page"], "h 数据盘 只剩 12.5 GiB")
        self.assertTrue(a["msg"].startswith(a["page"] + "：先清本项目能重算的中间文件"), a["msg"])
        self.assertIn("先问用户", a["msg"])
        self.assertEqual(C.user_alerts(snap["alerts"]), [a["page"]])

    def test_other_project_on_gpu(self):
        snap = self.snap([{"pid": 123, "mem": 21000.0, "cmd": ""}])
        self.assertEqual([a["key"] for a in snap["alerts"]], [])

    def worker_status(self, **st):
        """工作台快照里这台主机的 worker 状态（同机别的项目、本项目在等的单元从这里看）。"""
        (self.d / "state" / "overview").mkdir(parents=True, exist_ok=True)
        (self.d / "state" / "overview" / "snapshot.json").write_text(json.dumps({"hosts": [{"id": "h", "status": fake_status(**st)}]}))

    def age_idle(self):
        st = self.d / "out" / ".labmon" / "state.json"
        d = json.loads(st.read_text())
        d["hosts"]["h"]["idle_since"] -= C.IDLE_GRACE_S
        st.write_text(json.dumps(d))

    def test_really_idle(self):
        self.worker_status(waiting=[waiting_job("u", "out")])                      # 本项目有事在等，卡却空着
        self.assertEqual(self.snap([])["alerts"], [])                          # 刚空下来：可能是任务之间、拉起保活前的空档
        self.age_idle()
        snap = self.snap([])
        self.assertEqual([a["key"] for a in snap["alerts"]], ["idle:h:keepalive"])
        self.assertIsNone(snap["no_work_since"])
        a = snap["alerts"][0]                                                   # 两边都要（#142）：用户看到可能被回收，会话那份写先查什么
        self.assertEqual(a["to"], "both")
        self.assertIn("labmon worker start h", a["msg"])
        self.assertNotIn("labmon", a["page"])
        self.assertEqual(C.user_alerts(snap["alerts"]), [a["page"]])

    def test_no_work_no_host_alerts(self):
        # 本项目暂时没实验：主机的事不归它报（主机仍由工作台和在用它的项目盯着）
        self.snap([])
        self.age_idle()
        snap = self.snap([])
        self.assertEqual(snap["alerts"], [])
        self.assertIsNotNone(snap["no_work_since"])

    def test_other_project_job_starting(self):
        self.worker_status(running=[running_job("x", "other")])                    # 同机别的项目的任务刚起来，还没占卡
        self.snap([])
        st = self.d / "out" / ".labmon" / "state.json"
        self.assertIsNone(json.loads(st.read_text())["hosts"]["h"]["idle_since"])


class WaitGoneTest(IdleCheckTest):
    """实验会话的 labmon wait 断了（#141）：项目还有活、wait_state.json 的 t 满 WAIT_GONE_S 没动，出一条只给用户的 wait_gone:。"""

    def setUp(self):
        super().setUp()
        from labmon import deploy as DP
        self.running = dict(DP.RUNNING)
        DP.RUNNING["started"] = round(time.time()) - 86400        # 看板服务一天前起来的
        self.addCleanup(lambda: DP.RUNNING.update(self.running))

    def waited(self, ago: float):
        from labmon import waits as WT
        sf = WT.state_file(self.d / "out")
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({"reported": [], "last": {}, "t": time.time() - ago, "wait_t": time.time() - ago}))
        return time.time() - ago

    def gone(self, snap):
        return [a for a in snap["alerts"] if a["key"].startswith("wait_gone:")]

    def test_threshold(self):
        from labmon import waits as WT
        self.worker_status(waiting=[waiting_job("u", "out")])                       # 本项目还有任务在等
        self.waited(WT.WAIT_GONE_S - 60)
        self.assertEqual(self.gone(self.snap([])), [])                          # 门槛前不报
        last = self.waited(WT.WAIT_GONE_S + 60)
        a = self.gone(self.snap([]))
        self.assertEqual([(x["key"], x["to"]) for x in a], [(f"wait_gone:{round(last)}", "user")])
        self.assertIn("labmon wait", a[0]["msg"])
        self.assertEqual(C.user_alerts(a), [a[0]["msg"]])                       # 页面上显眼列
        self.waited(5)                                                          # 再来等一次就消失
        self.assertEqual(self.gone(self.snap([])), [])

    def test_dup(self):
        # 别的会话想挂 wait 被拦下（#142，waits._claim 记的 refused）：1 小时内、正在等的会话还在时给用户一条 wait_dup
        from labmon import waits as WT
        sf = WT.state_file(self.d / "out")
        sf.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        sf.write_text(json.dumps({"t": now, "wait_t": now, "holder": {"s": "A", "a": "amy", "t0": now - 600, "t": now}, "refused": {"a": "bob", "t": now - 60}}))
        a = [x for x in self.snap([])["alerts"] if x["key"].startswith("wait_dup:")]
        self.assertEqual([x["to"] for x in a], ["user"])
        self.assertIn("账号 bob", a[0]["msg"])
        self.assertIn("--take", a[0]["msg"])
        sf.write_text(json.dumps({"t": now, "wait_t": now, "holder": {"s": "A", "t0": now, "t": now}, "refused": {"a": "bob", "t": now - WT.DUP_SHOW_S - 1}}))
        self.assertFalse([x for x in self.snap([])["alerts"] if x["key"].startswith("wait_dup:")])     # 过了 1 小时

    def test_quiet_cases(self):
        from labmon import deploy as DP
        from labmon import waits as WT
        self.waited(WT.WAIT_GONE_S + 60)
        self.assertEqual(self.gone(self.snap([])), [])                          # 暂时没实验：不报
        self.worker_status(waiting=[waiting_job("u", "out")])
        DP.RUNNING["started"] = round(time.time()) - 60                         # 看板刚重启：从启动时刻起算
        self.assertEqual(self.gone(self.snap([])), [])
        DP.RUNNING["started"] = None                                            # 不在看板服务里（labmon collect 命令行）：不判
        self.assertEqual(self.gone(self.snap([])), [])
        DP.RUNNING["started"] = round(time.time()) - 86400
        WT.state_file(self.d / "out").unlink()                                  # 从没挂过 wait 的项目：不报
        self.assertEqual(self.gone(self.snap([])), [])
        sf = WT.state_file(self.d / "out")
        sf.write_text(json.dumps({"reported": ["page:x"], "last": {}, "t": time.time() - WT.WAIT_GONE_S - 60}))
        self.assertEqual(self.gone(self.snap([])), [])                          # 只跑过 labmon check（记账的 t，不是 wait 来过）：不报


class ProbeDownTest(Base):
    """项目页的掉线判定与工作台同一处（health.link_step）：探针连不上要连续至少 2 次、持续 DOWN_S 秒才报 ssh_fail；
    本机不在内网那轮不计；连上了就清掉。"""

    def build(self, ok: bool, lan: dict | None = None) -> dict:
        (self.d / "hosts.toml").write_text("")
        (self.d / "monitor.toml").write_text(f'out_dir = "{self.d / "out"}"\n[[hosts]]\nid = "h"\nssh = "root@192.0.2.9"\n')
        (self.d / "state" / "overview").mkdir(parents=True, exist_ok=True)        # 本项目有事在等：不算“暂时没实验”，主机的事归它报
        (self.d / "state" / "overview" / "snapshot.json").write_text(json.dumps({"hosts": [{"id": "h", "status": fake_status(waiting=[waiting_job("u", "out")])}]}))
        probe =({"t": time.time(), "gpu": [], "gpu_procs": [], "checks": []}, None) if ok else (None, "本机不在内网" if lan else "探针超时")
        return C.build(load(self.d / "monitor.toml"), {"h": probe}, self.d / "out", lan)

    def state(self) -> dict:
        """这台的连通状态（health.link_step 记在 .labmon/state.json 里的 fails / since / down）。"""
        return json.loads((self.d / "out" / ".labmon" / "state.json").read_text())["hosts"]["h"]

    def age(self, s: float) -> None:
        st = self.d / "out" / ".labmon" / "state.json"
        d = json.loads(st.read_text())
        d["hosts"]["h"]["since"] -= s
        st.write_text(json.dumps(d))

    def test_down_after_two_fails_and_down_s(self):
        from labmon.health import DOWN_S
        self.build(True)
        h = self.build(False)["hosts"][0]
        self.assertEqual((h["ok"], self.state()["fails"], h["error"]), (False, 1, "探针超时"))   # 抖一下：不报
        self.assertEqual(self.build(False)["alerts"], [])                                       # 第 2 次但没到 DOWN_S
        self.age(DOWN_S)
        snap = self.build(False)
        self.assertEqual([(a["key"], a["to"]) for a in snap["alerts"]], [("ssh_fail:h", "user")])   # 连不上只有用户能处理（#142）
        self.assertIn("探针连续失败 3 次", snap["alerts"][0]["msg"])
        self.assertEqual(self.state()["fails"], 3)
        self.assertEqual(self.build(False)["alerts"][0]["key"], "ssh_fail:h")                   # 一直连不上：告警一直在
        snap = self.build(True)
        self.assertEqual(snap["alerts"], [])                                                    # 连上：清掉
        st = self.state()
        self.assertEqual((st["fails"], st["down"]), (0, None))

    def test_offline_round_not_counted(self):
        self.build(True)
        self.build(False)
        off = {"ok": False, "since": time.time(), "checked": time.time()}
        h = self.build(False, lan=off)["hosts"][0]
        self.assertEqual((h["ok"], self.state()["fails"], h["error"]), (False, 0, "本机不在内网"))   # 离开内网前那一次连不上不算数
        self.build(False, lan=off)
        self.assertEqual(self.state()["fails"], 0)
        self.build(False)
        self.assertEqual(self.state()["fails"], 1)                                             # 回来后重新计


class RegLanesTest(Base):
    """项目的车道按 worker 登记生成（#94）：monitor.toml 的 [[queues]] 可选，写了的主机以登记为准；登记读自全局快照的 regs（overview._regs）。"""

    def setUp(self):
        super().setUp()
        self.root = self.d / "proj"
        make_queue(self.root, self.lock.pid, self.cur.pid)
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\nshared = ["{self.d}"]\n\n[[hosts]]\nid = "gone"\nssh = "nobody@invalid.example"\n'
                                           f'returned = "2026-11-04 18:40"\nshared = ["{self.d}"]\n')

    def regs(self, *xs):
        """写全局快照（工作台上一轮的）的 regs：xs 是 (主机, state, [(项目, root, jobs)])。"""
        from labmon.state import save_json, state_dir
        save_json(state_dir() / "snapshot.json", {"regs": [{"host": h, "label": h, "state": st, "stale": False, "t": 0,
                                                            "queues": None if qs is None else [{"root": r, "jobs": j, "project": p, "stop": None, "gpus": None} for p, r, j in qs]}
                                                           for h, st, qs in xs]})

    def cfg(self, queues: str = "") -> dict:
        (self.d / "monitor.toml").write_text(f'id = "p1"\n{queues}')
        return load(self.d / "monitor.toml")

    def test_no_queues_lane_from_registration(self):
        # monitor.toml 不写 [[queues]]、也不写 [[hosts]]：登记在 here 的清单出现在项目页，主机从用户级主机清单取；别的项目的、主机清单里没有的不算
        self.regs(("here", "active", [("p1", str(self.root) + "/", "jobs/q.txt"), ("p9", "/r/x", "jobs/x.txt")]),
                  ("old", "unlisted", [("p1", "/r/old", "jobs/o.txt")]))
        cfg = self.cfg()
        self.assertEqual((cfg["hosts"], cfg["queues"]), ([], []))
        snap = C.collect(cfg)
        self.assertEqual([h["id"] for h in snap["hosts"]], ["here"])
        self.assertEqual([(ln["id"], ln["host"], ln["label"], ln["root"], ln["queue"]) for ln in snap["lanes"]],
                         [("proj/jobs/q.txt", "here", "车道 1", str(self.root) + "/", "jobs/q.txt")])
        self.assertEqual({j["id"]: j["status"] for j in snap["jobs"]}["a_run"], "running")
        self.assertEqual(snap["alerts"], [])

    def test_pool_lane(self):
        # #24：交给算力池的清单（regs 里 host 为 pool）：项目页上一张「算力池」小卡与一条池车道，等着放到某台；
        # 代读它的 here 不是本项目的主机，不进项目快照；池里没有 worker 管，不报队列进程不在
        self.regs(("pool", "pool", [("p1", str(self.root), "jobs/q.txt")]), ("here", "active", []))
        snap = C.collect(self.cfg())
        self.assertEqual([(h["id"], h["label"], h.get("pool")) for h in snap["hosts"]], [("pool", "算力池", True)])
        ln, = snap["lanes"]
        self.assertEqual((ln["host"], ln.get("pool"), ln["alive"], ln["waiting"]), ("pool", True, False, C.POOL_WAIT))
        self.assertIn("a_done", {j["id"] for j in snap["jobs"]})          # 清单状态经 here 代读到了
        self.assertEqual([a["key"] for a in snap["alerts"]], [])


    def test_pool_lane_running_elsewhere(self):
        # #78：清单刚从借来的主机搬回池里，那台手上的那一格还在跑（current 里放行的主机是 a）：池车道上它算在跑，不当成排队（旧代码 readonly 一律不算在跑）；
        # current 没写放行的主机的认不出是谁的，不查这台的 PID
        st = self.root / "state"
        cur = (st / "current").read_text().split()
        (st / "current").write_text(" ".join(cur[:4] + ["a"]))
        self.regs(("pool", "pool", [("p1", str(self.root), "jobs/q.txt")]), ("here", "active", []))
        snap = C.collect(self.cfg())
        self.assertEqual({j["id"]: j["status"] for j in snap["jobs"]}["a_run"], "running")
        (st / "current").write_text(" ".join(cur[:4]))
        self.assertEqual({j["id"]: j["status"] for j in C.collect(self.cfg())["jobs"]}["a_run"], "queued")

    def test_pool_placed_lane_follows(self):
        # #78：池清单被 pool.tick 放到池借来的 a 上之后，这一轮写出的全局快照就让项目车道跟到 a：项目「主机」有 a、在跑的进 jobs[]、wait --job 认得出；
        # 旧代码 regs 还是放之前读的，车道要再等一轮全局采集，期间停在池里、在跑的那一格当成排队。a 还掉、清单回池里以后，跑完的照样在、带开始与结束
        from unittest import mock
        from fixtures import fake_host
        from labmon import pool as PO
        from labmon import waits as WT
        from labmon.config import fmt_time
        from labmon.state import append_row, save_json, state_dir, usage_file
        hf = self.d / "hosts.toml"
        base = hf.read_text() + ('\n[fleet]\ninterval = 600\n\n[[fleet.servers]]\nid = "a"\nholder = "AA"\nssh = "root@h"\nport = 1\npool = "P"\nlend = true\n'
                                 f'borrow_since = "{fmt_time(time.time() - 3600, year=True)}"\nborrow_pool = true\n')
        hf.write_text(base)
        st = self.root / "state"
        cur = (st / "current").read_text().split()
        (st / "current").write_text(" ".join(cur[:4] + ["a"]))      # a 上的 worker 放行的
        k = PO.key(str(self.root), "jobs/q.txt")
        q = {"root": str(self.root), "jobs": "jobs/q.txt", "project": "p1", "stop": "term", "gpus": None}
        here = fake_host("here", {"pool_heads": {k: {"head": "a_run", "n": 3, "dep": None, "held": None}}})
        a = fake_host("a", {"room": {"gpus": {"GPU-a": 40000.0}}}, borrow={"pool": True, "holder": "AA", "since": time.time() - 60})
        glob = {"hosts": [here, a], "regs": [{"host": "here", "state": "active", "queues": []}, {"host": "a", "state": "active", "queues": []},
                                             {"host": "pool", "state": "pool", "queues": [dict(q, added_t=time.time() - 300)]}]}
        moves = []
        fit = lambda it, hs: {"need": {"ngpu": 1, "vram": 1.0}, "hosts": {h: {"ok": True} for h in hs}}   # noqa: E731
        PO._FIT.clear()
        self.addCleanup(PO._FIT.clear)
        PO.tick(glob, {}, move=lambda to, items, pool="": moves.append(to), fit=fit, start=lambda *x, **y: None, ret=lambda *x: None, fin=lambda *x, **y: "")
        self.assertEqual(moves, ["a"])
        save_json(state_dir() / "snapshot.json", glob)
        local = C.run_probe
        with mock.patch.object(C, "run_probe", lambda cfg, h: local(cfg, dict(h, ssh=None))), \
                mock.patch.dict(os.environ, {"LABMON_LAN": "on"}):           # 不连 a：探针在本机跑，a 的目录就是这里
            snap = C.collect(self.cfg())
        self.assertIn("a", [h["id"] for h in snap["hosts"]])
        ln, = snap["lanes"]
        self.assertEqual((ln["host"], ln.get("pool")), ("a", None))
        self.assertEqual({j["id"]: j["status"] for j in snap["jobs"]}["a_run"], "running")
        self.assertEqual(WT.job_state(snap, "a_run", [])["st"], "running")
        self.assertIn("在 root@h（池 · AA） 车道 1 上跑", WT.job_state(snap, "a_run", [])["text"])
        # 还掉：a 目录里留着的登记（跑完的那一格已收尾）由 pool.tick 搬回池里；跑完的 a_done 照样在、时间线有开始与结束
        hf.write_text(base + 'borrow_returned = "2026-11-18 10:00"\n')
        (st / "current").unlink()
        glob = {"hosts": [here], "regs": [{"host": "here", "state": "active", "queues": []}, {"host": "a", "state": "returned", "queues": [q]}]}
        moves.clear()
        PO.tick(glob, {}, move=lambda to, items, pool="": moves.append(to), fit=fit, start=lambda *x, **y: None, ret=lambda *x: None, fin=lambda *x, **y: "")
        self.assertEqual(moves, ["pool"])
        save_json(state_dir() / "snapshot.json", glob)
        # #99：工作台存的 a 的用量记录里有这条清单跑过的（先失败一次、再跑成）：回池后跑完的那格带着 a、失败的尝试也带着 a，项目页画在 a 下面
        # （旧代码按车道主机 pool 找用量记录，什么也找不到，跑完的那格挂在不画的池车道上，从时间线消失）
        now, row = time.time(), {"p": "p1", "root": str(self.root), "queue": "jobs/q.txt", "n": "a_done", "g": []}
        for r in (dict(row, s=now - 3000, t=now - 2900, st="failed"), dict(row, s=now - 2800, t=now - 2000, st="done"), dict(row, n="x", s=now - 2800, t=now - 2000, st="done")):
            append_row(usage_file("a"), r, 86400, now)
        snap = C.collect(self.cfg())
        ln, = snap["lanes"]
        self.assertEqual((ln["host"], ln.get("pool")), ("pool", True))
        done = next(j for j in snap["jobs"] if j["id"] == "a_done")
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["start_ts"] and done["end_ts"])
        self.assertEqual(done.get("host"), "a")
        self.assertEqual([(x["name"], x.get("host")) for x in snap["attempts"]], [("a_done", "a")])
        self.assertNotIn("host", next(j for j in snap["jobs"] if j["status"] == "queued"))     # 还没放的不带主机，不进时间线
        self.assertEqual(WT.job_state(snap, "a_done", [])["st"], "done")

    def test_pool_returned_old_list_alerts(self):
        # #88 E：算力池借过的主机还掉后，池的清单（带停法，pool._ours）由 pool.tick 搬回池里、不叫会话；借给池之前就登记在上面的旧清单（停法没写）池不收，
        # 照报 returned_unfinished 叫会话自己搬（旧代码对池借过的主机一律不报，会话收不到）
        from labmon.config import fmt_time
        from labmon.state import save_json, state_dir
        hf = self.d / "hosts.toml"
        hf.write_text(hf.read_text() + ('\n[fleet]\ninterval = 600\n\n[[fleet.servers]]\nid = "a"\nholder = "AA"\nssh = "root@h"\nport = 1\npool = "P"\nlend = true\n'
                                        f'borrow_since = "{fmt_time(time.time() - 3600, year=True)}"\nborrow_pool = true\nborrow_returned = "2026-11-18 10:00"\n'))
        for stop, want in ((None, True), ("boundary", True), ("term", False)):
            q = {"root": str(self.root), "jobs": "jobs/q.txt", "project": "p1", "stop": stop, "gpus": None}
            save_json(state_dir() / "snapshot.json", {"regs": [{"host": "here", "state": "active", "queues": []}, {"host": "a", "state": "returned", "queues": [q]}]})
            snap = C.collect(self.cfg())
            self.assertEqual([ln["host"] for ln in snap["lanes"]], ["a"], stop)
            self.assertEqual(any(a["key"].startswith("returned_unfinished:") for a in snap["alerts"]), want, stop)

    def test_pool_lane_why_and_wait(self):
        # #25：池车道的等待原因接上 pool.tick 写的为什么还没放；待放满 POOL_TELL_S 给实验会话一条 pool_wait:（只给会话、key 带开始等的时刻）
        import time
        from labmon import pool as PO
        from labmon.state import load_json, save_json, state_dir
        self.regs(("pool", "pool", [("p1", str(self.root), "jobs/q.txt")]), ("here", "active", []))
        k = PO.key(str(self.root), "jobs/q.txt")
        since = round(time.time() - 600)
        snap = load_json(state_dir() / "snapshot.json", {})
        snap["pool"] = {"lists": {k: {"project": "p1", "since": since, "why": "池里没有借着、空得出来的服务器"}}}
        save_json(state_dir() / "snapshot.json", snap)
        out = C.collect(self.cfg())
        ln, = out["lanes"]
        self.assertEqual(ln["waiting"], C.POOL_WAIT + "：池里没有借着、空得出来的服务器")
        self.assertEqual(ln["pool_since"], since)          # 页面上写已等多久（#89）
        self.assertEqual(out["alerts"], [])
        snap["pool"]["lists"][k]["since"] = since - PO.POOL_TELL_S
        save_json(state_dir() / "snapshot.json", snap)
        out = C.collect(self.cfg())
        a, = out["alerts"]
        # #130：一条清单一条（key 带项目与车道，不带起点）；清单回池重新计时（since 变了）key 不变，不再叫醒
        lid = ln["id"]
        self.assertEqual((a["key"], a["to"]), (f"pool_wait:p1:{lid}", "agent"))
        self.assertIn("jobs/q.txt 在算力池里等了", a["msg"])
        snap["pool"]["lists"][k]["since"] = since - 2 * PO.POOL_TELL_S
        save_json(state_dir() / "snapshot.json", snap)
        self.assertEqual([a["key"] for a in C.collect(self.cfg())["alerts"]], [f"pool_wait:p1:{lid}"])
        snap["pool"]["lists"][k].update(nofit=True, why="池里借着的服务器都放不下（a：卡不够）；没有能借的入池服务器（借着 1 台）")
        save_json(state_dir() / "snapshot.json", snap)
        a, = C.collect(self.cfg())["alerts"]                # 放不下、也借不到：换一个 key，另报一次
        self.assertEqual(a["key"], f"pool_wait:p1:{lid}:nofit")
        self.assertIn("池里借着的服务器都放不下", a["msg"])
        snap["pool"]["lists"][k].pop("nofit")
        snap["pool"]["lists"][k].update(why=PO.POOL_DONE, done=True)    # #86：池里跑完了的不算在等
        save_json(state_dir() / "snapshot.json", snap)
        out = C.collect(self.cfg())
        self.assertIsNone(out["lanes"][0]["waiting"])

    def test_pool_wait_each_list(self):
        # #130：池里两条清单先后等满 POOL_TELL_S：第二条另报一次（旧代码 key 只带项目，第一条报过以后第二条、转成放不下都不再叫醒）；
        # 叫没叫醒经 waits._new_alerts 认：清单放出去又回池里重新等满（REWAKE_S 内）、在等着与放不下之间来回，都不再叫醒
        import time
        from labmon import pool as PO
        from labmon import waits as WT
        from labmon.state import load_json, save_json, state_dir
        (self.root / "jobs" / "r.txt").write_text("c_one|echo e\n")
        (self.root / "jobs" / "r.env").write_text("QUEUE_STATE=state2\nQUEUE_LOGS=logs2\n")
        (self.root / "state2").mkdir()
        (self.root / "logs2").mkdir()
        self.regs(("pool", "pool", [("p1", str(self.root), "jobs/q.txt"), ("p1", str(self.root), "jobs/r.txt")]), ("here", "active", []))
        kq, kr = PO.key(str(self.root), "jobs/q.txt"), PO.key(str(self.root), "jobs/r.txt")
        now = time.time()
        snap = load_json(state_dir() / "snapshot.json", {})
        lists = {kq: {"project": "p1", "since": now - PO.POOL_TELL_S - 60, "why": "正在借 AA 的 a"},
                 kr: {"project": "p1", "since": now - 600, "why": "正在借 AA 的 a"}}
        sf, t = self.d / "wait_state.json", now

        def wake(**upd):
            nonlocal t
            for kk, v in upd.items():
                lists[kk].update(v)
                [lists[kk].pop(x) for x in [x for x, y in v.items() if y is None]]
            snap["pool"] = {"lists": lists}
            save_json(state_dir() / "snapshot.json", snap)
            out = C.collect(self.cfg())
            t += 300
            return out["alerts"], WT._new_alerts(sf, {a["key"]: a["msg"] for a in out["alerts"]}, t)

        al, new = wake()
        self.assertEqual(len(new), 1, al)
        self.assertTrue(new[0].startswith("pool_wait:p1:") and new[0].endswith("q.txt"), new)
        al, new = wake(**{kr: {"since": now - PO.POOL_TELL_S - 60}})       # 第二条也等满：另报
        self.assertEqual(len(al), 2)
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0].endswith("r.txt"), new)
        al, new = wake(**{kr: {"nofit": True}})                              # r 放不下、也借不到：换 key，另报一次
        self.assertEqual(new, [new[0]])
        self.assertTrue(new[0].endswith("r.txt:nofit"), new)
        self.assertEqual(wake(**{kr: {"nofit": None}})[1], [])               # 来回不再叫醒
        self.assertEqual(wake(**{kr: {"nofit": True}})[1], [])
        al, new = wake(**{kq: {"since": now - 60}})                          # q 放出去又回池里重新计时：没等满，不报
        self.assertEqual(new, [])
        self.assertEqual(wake(**{kq: {"since": now - PO.POOL_TELL_S - 60}})[1], [])   # 再等满（REWAKE_S 内）：不再叫醒

    def test_registration_overrides_returned_host(self):
        # monitor.toml 写着 host = 已还的 gone，清单已登记到 here：车道在 here 上、id 与用途沿用 monitor.toml 的，不报 returned_unfinished（旧代码会报）
        q = f'[[queues]]\nid = "q"\nlabel = "训练方"\nhost = "gone"\nroot = "{self.root}"\njobs = "jobs/q.txt"\n'
        self.regs(("here", "active", [("p1", str(self.root), "jobs/q.txt")]))
        snap = C.collect(self.cfg(q))
        self.assertEqual([(ln["id"], ln["host"], ln["label"], ln.get("returned")) for ln in snap["lanes"]], [("q", "here", "车道 1 训练方", None)])
        self.assertFalse(any(a["key"].startswith("returned_unfinished:") for a in snap["alerts"]))
        self.assertEqual({j["id"]: j["status"] for j in snap["jobs"]}["a_run"], "running")
        # 登记还在已还的 gone 上（没搬走）：车道在 gone 上，经 here 代读（项目里没有 here，从主机清单补一台能代读的），照报没跑完
        self.regs(("gone", "returned", [("p1", str(self.root), "jobs/q.txt")]), ("here", "active", []))
        snap = C.collect(self.cfg(q))
        self.assertEqual([(ln["id"], ln["host"], ln["read_via"]) for ln in snap["lanes"]], [("q", "gone", "here")])
        self.assertTrue(any(a["key"].startswith("returned_unfinished:q:gone:") for a in snap["alerts"]))

    def done_list(self, name: str, age: float, jobs: int = 2, done: bool = True) -> Path:
        """一份清单 <d>/<name>/jobs/<name>.txt：jobs 个任务，done 时都已跑完（完成标记与队列日志是 age 秒前的），否则从没启动过。"""
        r = self.d / name
        (r / "jobs").mkdir(parents=True)
        (r / "jobs" / f"{name}.txt").write_text("".join(f"{name}_{i}|echo {i}\n" for i in range(jobs)))
        (r / "jobs" / f"{name}.env").write_text("QUEUE_STATE=state\nQUEUE_LOGS=logs\n")
        (r / "state").mkdir()
        (r / "logs").mkdir()
        if done:
            for f in [r / "state" / f"{name}_{i}.done" for i in range(jobs)] + [r / "state" / "queue.log"]:
                f.write_text("")
                os.utime(f, (time.time() - age, time.time() - age))
        return r

    def test_finished_registered_lanes_fold(self):
        # #117、#164：0 任务空清单不出车道；已归还无未完成无失败、已摘登记无未完成、已休眠立即收起；
        # 在用主机 24 小时内全成功跑完的非 monitor.toml 车道不占“车道 N”编号（标 done_lane、叫“已跑完”）；
        # monitor.toml 写明的一直留着。
        from labmon.state import save_json, state_dir
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{self.d / "data"}"\nshared = ["{self.d}"]\n\n[[hosts]]\nid = "gone"\n'
                                           f'ssh = "nobody@invalid.example"\nreturned = "2026-11-04 18:40"\nshared = ["{self.d}"]\n')
        old, recent, gold = self.done_list("old", 2 * 86400), self.done_list("recent", 3600), self.done_list("gold", 3 * 86400)
        fresh, gpend, gempty = self.done_list("fresh", 0, jobs=0, done=False), self.done_list("gpend", 0, done=False), self.done_list("gempty", 0, jobs=0, done=False)
        kept = self.done_list("kept", 5 * 86400)
        now = time.time()
        q = lambda r, **kw: {"root": str(r), "jobs": f"jobs/{r.name}.txt", "project": "p1", "stop": None, "gpus": None, **kw}   # noqa: E731
        here = [q(self.root, jobs="jobs/q.txt", added_t=now - 9 * 86400), q(old, added_t=now - 9 * 86400, empty=True),
                q(recent, added_t=now - 9 * 86400, empty=True), q(fresh, added_t=now - 60), q(kept)]
        # worker 在管 here 上的清单（状态里有，跑完的也有）：#94 以前也按“在”算，一直显示
        wst = self.d / "data" / "labmon" / "hosts" / "here" / "status.json"
        wst.parent.mkdir(parents=True)
        wst.write_text(json.dumps(fake_status(queues=[{"root": x["root"], "jobs": x["jobs"], "project": "p1"} for x in here])))
        gone = [q(gold, added="2026-11-01 10:00"), q(gpend, added="2026-11-01 10:00"), q(gempty, added="2026-11-01 10:00")]
        save_json(state_dir() / "snapshot.json", {"regs": [{"host": "here", "state": "active", "stale": False, "t": 0, "queues": here},
                                                           {"host": "gone", "state": "returned", "stale": False, "t": 0, "queues": gone}]})
        cfg = self.cfg(f'[[queues]]\nid = "kept"\nhost = "here"\nroot = "{kept}"\njobs = "jobs/kept.txt"\n')
        snap = C.collect(cfg)
        self.assertEqual([(ln["id"], ln["host"], ln["label"], bool(ln.get("done_lane"))) for ln in snap["lanes"]],
                         [("kept", "here", "车道 1", False), ("gpend/jobs/gpend.txt", "gone", "车道 1", False),
                          ("proj/jobs/q.txt", "here", "车道 2", False), ("recent/jobs/recent.txt", "here", "已跑完", True)])     # 0 任务空清单与已归还全跑完立即收起，24h 内全跑完不占号
        self.assertTrue(any(a["key"].startswith("returned_unfinished:gpend/jobs/gpend.txt:gone:") for a in snap["alerts"]))
        # 收起的车道所在的主机还是项目的主机：labmon wait 的要还、已还照样报给它
        self.assertEqual([h["id"] for h in snap["hosts"]], ["here", "gone"])
        # 都跑完以后：车道收起，“暂时没实验”的起点是最后一次活动，不是现在
        (self.root / "state" / "lock").unlink()
        (self.root / "state" / "current").unlink()
        for n in ("b_next", "b_noest", "a_run"):
            (self.root / "state" / f"{n}.done").write_text("")
        for f in (self.root / "state").iterdir():
            os.utime(f, (now - 3 * 86400, now - 3 * 86400))
        for r in (recent, fresh):
            for f in (r / "state").iterdir():
                os.utime(f, (now - 2 * 86400, now - 2 * 86400))
        here[2:4] = [q(recent, added_t=now - 9 * 86400, empty=True), q(fresh, added_t=now - 2 * 86400)]
        save_json(state_dir() / "snapshot.json", {"regs": [{"host": "here", "state": "active", "stale": False, "t": 0, "queues": here}]})
        (gpend / "jobs" / "gpend.txt").write_text("")
        snap = C.collect(cfg)
        self.assertEqual([ln["id"] for ln in snap["lanes"]], ["kept"])        # gone 上的登记没了：留 24 小时，但跑完久了照样收起
        self.assertAlmostEqual(snap["no_work_since"], now - 2 * 86400, delta=5)

    def test_sleeping_and_returned_done_shortcut_and_no_idle_regs(self):
        # #164：1) 彻底删除 idle_regs: 提示并清掉 state.json 里的旧 idle_regs 字段；
        # 2) worker 休眠队列探针短路（不读 .env/状态目录/清单）并立即收起、在 quiet 里标 sleeping=True；
        # 3) 已归还主机全跑完清单记下 done_sig 后探针短路，往清单追加新行后自动重新全量读并报 returned_unfinished。
        from labmon.state import load_json, save_json, state_dir
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{self.d / "data"}"\nshared = ["{self.d}"]\n\n[[hosts]]\nid = "gone"\n'
                                           f'ssh = "nobody@invalid.example"\nreturned = "2026-11-04 18:40"\nshared = ["{self.d}"]\n')
        slp, gold, kept = self.done_list("slp", 3 * 86400), self.done_list("gold", 3600), self.done_list("kept", 5 * 86400)
        now = time.time()
        q = lambda r, **kw: {"root": str(r), "jobs": f"jobs/{r.name}.txt", "project": "p1", "stop": None, "gpus": None, "added_t": now - 9 * 86400, **kw}   # noqa: E731
        wst = self.d / "data" / "labmon" / "hosts" / "here" / "status.json"
        wst.parent.mkdir(parents=True)
        wst.write_text(json.dumps(fake_status(queues=[
            {"root": str(slp), "jobs": "jobs/slp.txt", "project": "p1", "empty": True, "sleeping": True, "empty_since": int(now - 3 * 86400)},
            {"root": str(kept), "jobs": "jobs/kept.txt", "project": "p1", "empty": True, "sleeping": False},
        ])))
        save_json(state_dir() / "snapshot.json", {"regs": [
            {"host": "here", "state": "active", "stale": False, "t": 0, "queues": [q(slp, empty=True, sleeping=True), q(kept, empty=True)]},
            {"host": "gone", "state": "returned", "stale": False, "t": 0, "queues": [q(gold, added="2026-11-01 10:00")]},
        ]})
        cfg = self.cfg(f'[[queues]]\nid = "kept"\nhost = "here"\nroot = "{kept}"\njobs = "jobs/kept.txt"\n')
        sd = PJ.out_dir("p1") / ".labmon"
        sd.mkdir(parents=True, exist_ok=True)
        save_json(sd / "state.json", {"idle_regs": {"gen": 2, "keys": ["old"]}})    # 旧状态里的 idle_regs 字段
        # 把 slp 的 .env 和状态目录改成坏内容：探针因 worker.sleeping 短路不会读它们
        (slp / "jobs" / "slp.env").unlink()
        (slp / "jobs" / "slp.txt").write_text("new_unseen|echo 1\n")                 # 假装状态还没醒，探针只信 worker.sleeping 不读清单
        snap = C.collect(cfg)
        self.assertFalse([a for a in snap["alerts"] if a["key"].startswith("idle_regs:")])
        self.assertNotIn("idle_regs", load_json(sd / "state.json", {}))
        self.assertEqual([ln["id"] for ln in snap["lanes"]], ["kept"])              # slp 和 gold 都立即收起
        quiet = {(x["host"], x["queue"]): x["sleeping"] for x in snap["quiet"]}
        self.assertEqual(quiet, {("here", "jobs/slp.txt"): True, ("gone", "jobs/gold.txt"): False})
        # gold 已确认全跑完：reg_lanes.json 记下了 done_sig，下一轮探针走 readonly_done 短路（不读 .env/状态目录）
        mem = load_json(sd / "reg_lanes.json", {})
        gk = C.list_path(str(gold), "jobs/gold.txt")
        self.assertTrue(mem[gk].get("done_sig"))
        (gold / "jobs" / "gold.env").unlink()                                       # 删掉 .env，如果走全量读会找不到 state/gold_*.done 从而误判 queued
        snap2 = C.collect(cfg)
        self.assertEqual([ln["id"] for ln in snap2["lanes"]], ["kept"])
        self.assertFalse(any(a["key"].startswith("returned_unfinished:") for a in snap2["alerts"]))
        # 往已归还清单追加一行新任务（清掉 .env 恢复后）：file_sig 变了，探针不再短路，立即重新浮现并报 returned_unfinished
        (gold / "jobs" / "gold.env").write_text("QUEUE_STATE=state\nQUEUE_LOGS=logs\n")
        with (gold / "jobs" / "gold.txt").open("a") as f:
            f.write("gold_extra|echo extra\n")
        snap3 = C.collect(cfg)
        self.assertIn("gold/jobs/gold.txt", [ln["id"] for ln in snap3["lanes"]])
        self.assertTrue(any(a["key"].startswith("returned_unfinished:gold/jobs/gold.txt:gone:") for a in snap3["alerts"]))
        self.assertNotIn("done_sig", load_json(sd / "reg_lanes.json", {})[gk])
        # api.status 对超过 2 条（或有活跃车道时）的 done_lane 自动折成一条汇总
        from labmon import api as A
        fake = {"id": "p1", "title": "测试", "generated": now, "hosts": [{"id": "here", "ok": True, "gpus": []}],
                "lanes": [{"id": "act", "host": "here", "label": "车道 1", "alive": True, "done": 0, "total": 1, "unfinished": 1},
                          {"id": "d1", "host": "here", "label": "已跑完", "done_lane": True, "done": 1, "total": 1},
                          {"id": "d2", "host": "here", "label": "已跑完", "done_lane": True, "done": 2, "total": 2}],
                "jobs": [{"id": "j1", "lane": "act", "status": "running"}],
                "groups": [], "alerts": []}
        save_json(PJ.out_dir("p1") / "snapshot.json", fake)
        st_txt = A.status("p1", glob={}, now=now)["out"]
        self.assertIn("另有 2 条已跑完的车道（共 3 个任务，不占车道编号）", st_txt)

    def test_explicit_monitor_lane_keeps_results_when_sleeping(self):
        # 主审意见 #4：monitor.toml 显式配置的车道即使 worker 已休眠（sleeping=True），
        # 因为该车道按约定始终显示，探针不能短路成空列表，需保留完成统计（2/2）与失败告警（failed:）。
        from labmon.state import save_json, state_dir
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{self.d / "data"}"\nshared = ["{self.d}"]\n')
        kept = self.done_list("kept", 3 * 86400)
        (kept / "state" / "kept_1.done").unlink()
        (kept / "state" / "kept_1.failed").write_text("")
        now = time.time()
        wst = self.d / "data" / "labmon" / "hosts" / "here" / "status.json"
        wst.parent.mkdir(parents=True)
        wst.write_text(json.dumps(fake_status(queues=[
            {"root": str(kept), "jobs": "jobs/kept.txt", "project": "p1", "empty": True, "sleeping": True, "empty_since": int(now - 3 * 86400)},
        ])))
        save_json(state_dir() / "snapshot.json", {"regs": [
            {"host": "here", "state": "active", "stale": False, "t": 0,
             "queues": [{"root": str(kept), "jobs": "jobs/kept.txt", "project": "p1", "stop": None, "gpus": None, "empty": True, "sleeping": True, "added_t": now - 9 * 86400}]},
        ]})
        cfg = self.cfg(f'[[queues]]\nid = "kept"\nhost = "here"\nroot = "{kept}"\njobs = "jobs/kept.txt"\n')
        snap = C.collect(cfg)
        (ln,) = snap["lanes"]
        self.assertEqual((ln["id"], ln["total"], ln["done"], ln["failed"]), ("kept", 2, 1, 1))
        self.assertTrue(any(a["key"] == "failed:kept_1" for a in snap["alerts"]))

    def test_returned_read_error_not_cached_as_done_sig(self):
        # 第二轮主审意见 #2：已归还主机代读清单时，stat 成功但正文读取临时失败，不得缓存 done_sig；
        # 恢复可读后应立即识别未完成任务并报 returned_unfinished。
        from labmon import probe as P
        from labmon.state import load_json, save_json, state_dir
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{self.d / "data"}"\nshared = ["{self.d}"]\n\n[[hosts]]\nid = "gone"\n'
                                           f'ssh = "nobody@invalid.example"\nreturned = "2026-11-04 18:40"\nshared = ["{self.d}"]\n')
        gpend = self.done_list("gpend", 0, jobs=1, done=False)
        jpath = str(gpend / "jobs" / "gpend.txt")
        save_json(state_dir() / "snapshot.json", {"regs": [
            {"host": "here", "state": "active", "stale": False, "t": 0, "queues": []},
            {"host": "gone", "state": "returned", "stale": False, "t": 0,
             "queues": [{"root": str(gpend), "jobs": "jobs/gpend.txt", "project": "p1", "stop": None, "gpus": None, "added": "2026-11-01 10:00"}]},
        ]})
        cfg = self.cfg()
        sd = PJ.out_dir("p1") / ".labmon"
        # 模拟一次探针读正文失败（不经子进程，直接调 P.queue_gpu_queue 测探针输出 + C.build 端到端）
        real_read = P.read
        with unittest.mock.patch.object(P, "read", side_effect=lambda p, default=None: default if str(p) == jpath else real_read(p, default)):
            raw_err = P.queue_gpu_queue({"root": str(gpend), "jobs": "jobs/gpend.txt", "readonly": True})
        self.assertNotIn("jobs_sig", raw_err)
        # 即使拿该次探针结果跑一轮 collect，也不写 done_sig；下一轮恢复可读立即报 returned_unfinished
        with unittest.mock.patch.object(C, "run_probe", return_value=({"t": time.time(), "queues": {"gpend/jobs/gpend.txt": raw_err}}, None)):
            C.collect(cfg)
        gk = C.list_path(str(gpend), "jobs/gpend.txt")
        self.assertNotIn("done_sig", (load_json(sd / "reg_lanes.json", {}).get(gk) or {}))
        snap2 = C.collect(cfg)
        self.assertIn("gpend/jobs/gpend.txt", [ln["id"] for ln in snap2["lanes"]])
        self.assertTrue(any(a["key"].startswith("returned_unfinished:gpend/jobs/gpend.txt:gone:") for a in snap2["alerts"]))

    def test_stale_sleeping_status_does_not_hide_live_queued_jobs(self):
        # 第二轮主审意见 #3：worker 休眠后停止更新状态（status.json 过期），用户往清单追加了新任务：
        # 探针现场读到新排队任务（worker=None），不得被旧全局快照里的 reg_sleeping=True 盖掉导致车道与队列不在告警消失。
        from labmon.state import save_json, state_dir
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{self.d / "data"}"\nshared = ["{self.d}"]\n')
        slp = self.done_list("slp", 3 * 86400, jobs=1, done=True)
        with (slp / "jobs" / "slp.txt").open("a") as f:
            f.write("slp_new|echo new\n")
        now = time.time()
        wst = self.d / "data" / "labmon" / "hosts" / "here" / "status.json"
        wst.parent.mkdir(parents=True)
        # status.json 已经过期（早于 STALE_S），所以 probe.worker_view 忽略它，现场读清单
        wst.write_text(json.dumps(fake_status(t=now - 3600, queues=[
            {"root": str(slp), "jobs": "jobs/slp.txt", "project": "p1", "empty": True, "sleeping": True, "empty_since": int(now - 3 * 86400)},
        ])))
        save_json(state_dir() / "snapshot.json", {"regs": [
            {"host": "here", "state": "active", "stale": True, "t": now - 3600,
             "queues": [{"root": str(slp), "jobs": "jobs/slp.txt", "project": "p1", "stop": None, "gpus": None, "empty": True, "sleeping": True, "added_t": now - 9 * 86400}]},
        ]})
        snap = C.collect(self.cfg())
        self.assertEqual([ln["id"] for ln in snap["lanes"]], ["slp/jobs/slp.txt"])
        self.assertEqual({j["id"]: j["status"] for j in snap["jobs"]}["slp_new"], "queued")
        self.assertTrue(any(a["key"].startswith("lock_dead:") for a in snap["alerts"]))

    def test_wait_targets_hosts_with_lanes(self):
        # labmon wait 的主机定向按项目快照的主机（登记带来的主机也算），不看 monitor.toml
        from labmon.waits import wait_check
        from labmon.state import save_json
        self.regs(("here", "active", [("p1", str(self.root), "jobs/q.txt")]))
        cfg = self.cfg()
        C.collect(cfg)
        ov = self.d / "ov.json"
        save_json(ov, {"wake": [{"key": "borrow_due:here", "projects": None, "host": "here", "msg": "要还"},
                                {"key": "borrow_due:x", "projects": None, "host": "x", "msg": "别的主机"}]})
        self.assertEqual(wait_check(cfg, PJ.out_dir("p1"), self.d / "w.json", ov), {"borrow_due:here": "要还"})

    def test_reg_queues_order_keep_and_duplicates(self):
        cfg = self.cfg(f'[[queues]]\nid = "q"\nhost = "here"\nroot = "{self.root}"\njobs = "jobs/q.txt"\n'
                       f'[[queues]]\nid = "auto"\nhost = "here"\nroot = "{self.d}/p*"\njobs = "jobs/*.txt"\n')
        R = lambda h, st, qs: {"host": h, "state": st, "queues": None if qs is None else [dict(project="p1", root=r, jobs=j, **kw) for r, j, kw in qs]}   # noqa: E731
        t = 1_800_000_000
        hx = hashlib.sha1(b"/b/x").hexdigest()[:6]
        # 只在登记里有的排在 monitor.toml 的后面；同时见到的按清单路径；目录末级名与清单撞了的接目录的短哈希；同一清单两台都登记取在用的；没读到的主机不算
        regs = [R("gone", "returned", [("/a/x", "jobs/b.txt", {"added": "2026-11-10 10:00"})]),
                R("here", "active", [("/a/x/", "jobs/b.txt", {}), ("/b/x", "jobs/b.txt", {}), ("/a/y", "jobs/a.txt", {})]),
                R("nobody", "active", None)]
        qs, mem = C.reg_queues(cfg, regs, {}, t)
        self.assertEqual([(q["id"], q["host"]) for q in qs],
                         [("q", "here"), ("auto", "here"), ("x/jobs/b.txt", "here"), ("y/jobs/a.txt", "here"), ("x/jobs/b.txt@" + hx, "here")])
        # 后来新登记的接在最后，前面的编号不动；登记没了的按上次见到的主机留 24 小时，之后收起
        qs, mem = C.reg_queues(cfg, [R("here", "active", [("/a/x", "jobs/b.txt", {}), ("/b/x", "jobs/b.txt", {}), ("/0/new", "jobs/n.txt", {})])], mem, t + 60)
        self.assertEqual([q["id"] for q in qs][2:], ["x/jobs/b.txt", "y/jobs/a.txt", "x/jobs/b.txt@" + hx, "new/jobs/n.txt"])
        qs, mem = C.reg_queues(cfg, [], mem, t + 30 + C.REG_KEEP_S)             # y 是 t 见到的：过了 24 小时；别的是 t + 60 见到的：还留着
        self.assertEqual([q["id"] for q in qs], ["q", "auto", "x/jobs/b.txt", "x/jobs/b.txt@" + hx, "new/jobs/n.txt"])
        qs, mem = C.reg_queues(cfg, None, mem, t + 120 + 2 * C.REG_KEEP_S)        # 全局快照里没有 regs：只剩 monitor.toml 的
        self.assertEqual(([q["id"] for q in qs], mem), (["q", "auto"], {}))
        # 撞名后带哈希的那条，先来的那条没了以后也不改名：车道 id 记在 mem 里沿用（进度采样、挂起累计按 车道/任务 记；Codex 审查 #114）
        two = [R("here", "active", [("/a/x", "jobs/b.txt", {}), ("/b/x", "jobs/b.txt", {})])]
        qs, mem = C.reg_queues(cfg, two, {}, t)
        self.assertEqual([q["id"] for q in qs][2:], ["x/jobs/b.txt", "x/jobs/b.txt@" + hx])
        qs, mem = C.reg_queues(cfg, [R("here", "active", [("/b/x", "jobs/b.txt", {})])], mem, t + 60)
        qs, mem = C.reg_queues(cfg, [R("here", "active", [("/b/x", "jobs/b.txt", {})])], mem, t + 120 + C.REG_KEEP_S)
        self.assertEqual([q["id"] for q in qs][2:], ["x/jobs/b.txt@" + hx])
        # monitor.toml 写了的清单：登记换了主机跟着走，登记没了 24 小时后回到 monitor.toml 写的主机
        qs, mem = C.reg_queues(cfg, [R("gone", "returned", [(str(self.root), "jobs/q.txt", {})])], {}, t)
        self.assertEqual(qs[0]["host"], "gone")
        self.assertEqual(C.reg_queues(cfg, [], mem, t + 10)[0][0]["host"], "gone")
        self.assertEqual(C.reg_queues(cfg, [], mem, t + C.REG_KEEP_S + 10)[0][0]["host"], "here")
        # 登记过的清单进探针的 explicit_jobs：通配符在别的主机下发现同一清单时跳过它，不会两台各显示一次；登记带来的主机补进项目
        e = C.lanes_cfg(cfg, {"regs": [R("gone", "returned", [(f"{self.d}/p2", "jobs/z.txt", {})])]}, self.d / "out" / ".labmon", t)
        self.assertIn(f"{self.d}/p2/jobs/z.txt", C.probe_spec(e, e["hosts"][0])["explicit_jobs"])
        self.assertEqual([h["id"] for h in e["hosts"]], ["here", "gone"])
        self.assertEqual(next(q for q in e["queues"] if q["id"] == "p2/jobs/z.txt")["read_via"], "here")
