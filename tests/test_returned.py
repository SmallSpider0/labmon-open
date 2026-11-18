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
        self.assertEqual(keys, ["returned_unfinished:q"])   # 不报队列进程不在、连接失败或保活不在

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


    def test_registration_overrides_returned_host(self):
        # monitor.toml 写着 host = 已还的 gone，清单已登记到 here：车道在 here 上、id 与用途沿用 monitor.toml 的，不报 returned_unfinished（旧代码会报）
        q = f'[[queues]]\nid = "q"\nlabel = "训练方"\nhost = "gone"\nroot = "{self.root}"\njobs = "jobs/q.txt"\n'
        self.regs(("here", "active", [("p1", str(self.root), "jobs/q.txt")]))
        snap = C.collect(self.cfg(q))
        self.assertEqual([(ln["id"], ln["host"], ln["label"], ln.get("returned")) for ln in snap["lanes"]], [("q", "here", "车道 1 训练方", None)])
        self.assertNotIn("returned_unfinished:q", [a["key"] for a in snap["alerts"]])
        self.assertEqual({j["id"]: j["status"] for j in snap["jobs"]}["a_run"], "running")
        # 登记还在已还的 gone 上（没搬走）：车道在 gone 上，经 here 代读（项目里没有 here，从主机清单补一台能代读的），照报没跑完
        self.regs(("gone", "returned", [("p1", str(self.root), "jobs/q.txt")]), ("here", "active", []))
        snap = C.collect(self.cfg(q))
        self.assertEqual([(ln["id"], ln["host"], ln["read_via"]) for ln in snap["lanes"]], [("q", "gone", "here")])
        self.assertIn("returned_unfinished:q", [a["key"] for a in snap["alerts"]])

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
        # #117：只在登记里有的车道跑完、一天没活动就收起，
        # 与自动发现同一条规则；还有排队的、刚登记的、一天内跑完的、monitor.toml 写明的留着。旧代码全显示
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
        self.assertEqual([(ln["id"], ln["host"], ln["label"]) for ln in snap["lanes"]],
                         [("kept", "here", "车道 1"), ("fresh/jobs/fresh.txt", "here", "车道 2"), ("gpend/jobs/gpend.txt", "gone", "车道 1"),
                          ("proj/jobs/q.txt", "here", "车道 3"), ("recent/jobs/recent.txt", "here", "车道 4")])     # 收起的不占号
        self.assertIn("returned_unfinished:gpend/jobs/gpend.txt", [a["key"] for a in snap["alerts"]])
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

    def test_idle_registrations_hint_worker_rm(self):
        # #47：只在登记里有的车道空了 2 天以上，给实验会话一条 idle_regs:（只给会话），列出清单与 worker rm 的写法；只提示、不删登记。
        # 收起了但没满 2 天的、monitor.toml 写明的、还有待办的不列；同一批只叫醒一次，出现没报过的才换 key，会话删掉一部分不再叫醒。旧代码没有这条
        from labmon.state import save_json, state_dir
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{self.d / "data"}"\nshared = ["{self.d}"]\n')
        old, old2, recent, kept = (self.done_list("old", 3 * 86400), self.done_list("old2", 4 * 86400), self.done_list("recent", 30 * 3600),
                                   self.done_list("kept", 5 * 86400))
        now = time.time()
        q = lambda r, **kw: {"root": str(r), "jobs": f"jobs/{r.name}.txt", "project": "p1", "stop": None, "gpus": None, "added_t": now - 9 * 86400, **kw}   # noqa: E731

        def regs(*rs):
            save_json(state_dir() / "snapshot.json", {"regs": [{"host": "here", "state": "active", "stale": False, "t": 0,
                                                               "queues": [q(self.root, jobs="jobs/q.txt")] + [q(r, empty=True) for r in rs]}]})
        regs(old, old2, recent, kept)
        cfg = self.cfg(f'[[queues]]\nid = "kept"\nhost = "here"\nroot = "{kept}"\njobs = "jobs/kept.txt"\n')
        snap = C.collect(cfg)
        idle = [a for a in snap["alerts"] if a["key"].startswith("idle_regs:")]
        self.assertEqual([(a["key"], a["to"]) for a in idle], [("idle_regs:p1:1", "agent")])
        msg = idle[0]["msg"]
        for r in (old, old2):
            self.assertIn(f"{r}：jobs/{r.name}.txt", msg)
        self.assertIn("here 上 ", msg)
        self.assertIn("labmon worker rm <主机> <目录> <清单>", msg)
        for r in (recent, kept, self.root):                     # 收起了但没满 2 天、monitor.toml 写明的、还有待办的
            self.assertNotIn(str(r) + "：", msg)
        self.assertEqual(sorted((x["queue"], x["idle"]) for x in snap["quiet"]), [("jobs/old.txt", True), ("jobs/old2.txt", True), ("jobs/recent.txt", False)])
        self.assertNotIn("idle_regs:p1:1", [k for k, _ in C.user_alerts(snap["alerts"], keys=True)])   # 只给会话：不算告警数、不进红框
        self.assertTrue(all(d.exists() for d in (old, old2)))  # 只提示，不动清单
        # 再采一轮：同一批，key 不变（labmon wait 不再叫醒）
        self.assertIn("idle_regs:p1:1", [a["key"] for a in C.collect(cfg)["alerts"]])
        # recent 也空满 2 天了：出现没报过的，换 key 再叫醒一次，列全部
        for f in (recent / "state").iterdir():
            os.utime(f, (now - 3 * 86400, now - 3 * 86400))
        snap = C.collect(cfg)
        a = next(a for a in snap["alerts"] if a["key"].startswith("idle_regs:"))
        self.assertEqual(a["key"], "idle_regs:p1:2")
        self.assertIn(f"{recent}：jobs/recent.txt", a["msg"])
        # 会话删了 old、old2，留着 recent（长期清单）：key 不变，不再叫醒
        regs(recent, kept)
        snap = C.collect(cfg)
        a = next(a for a in snap["alerts"] if a["key"].startswith("idle_regs:"))
        self.assertEqual(a["key"], "idle_regs:p1:2")
        self.assertNotIn(f"{old}：", a["msg"])                  # 删了的不再列（车道按 REG_KEEP_S 还留着）
        # 都删了：告警消失
        regs(kept)
        self.assertFalse([a for a in C.collect(cfg)["alerts"] if a["key"].startswith("idle_regs:")])

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
