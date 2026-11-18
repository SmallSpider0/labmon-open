"""看板 ↔ worker 的契约测试：起真的 worker 在临时目录里跑出 status.json、usage.jsonl、events.jsonl，再让看板按真实路径读它们，
走完 项目看板（probe.worker_view → collect.build）→ 工作台（overview.fetch → overview.collect，含 sched_view、occupancy）→ 渲染两页 的整条链。

断言页面要用的字段都在、类型对（running[].t0、done[].t0 / t1、waiting[].pos / start、sched.projects 各键、plan.lanes），
项目 id 一路是 worker 写的 project，给页面的快照里没有文字时刻（start / end）。字段改名只改了一边、页面永远走空分支、死字段堆积，
这类问题以后在这里失败。

    python -m unittest tests.test_contract -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import WORKER, gate, register, release, worker_env  # noqa: E402
os.environ["LABMON_LAN"] = "on"                       # 本机主机不经 ssh：别判成不在内网

from labmon import collect as C  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import probe as P  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon.config import STALE_S, load  # noqa: E402
from labmon.state import load_rows, state_dir  # noqa: E402
from labmon.serve import Overview, Project  # noqa: E402

NUM = (int, float)
TEXT_TIME = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d(:\d\d)?$")


def text_times(x, path="") -> list[str]:
    """数据里所有长得像 YYYY-MM-DD HH:MM[:SS] 的字符串的位置（看板只认 epoch 秒，这种字段不该再有）。"""
    if isinstance(x, dict):
        return [p for k, v in x.items() for p in text_times(v, f"{path}.{k}")]
    if isinstance(x, list):
        return [p for i, v in enumerate(x) for p in text_times(v, f"{path}[{i}]")]
    return [path] if isinstance(x, str) and TEXT_TIME.match(x) else []


class ContractTest(unittest.TestCase):
    """两个项目各一条队列：pa 的 a0（不用卡）跑完、a1 等卡，pb 的 b1 占着卡在跑；卡只放得下一个 30 GiB 的任务。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = worker_env(self.d)
        self.home = self.d / "home" / "hosts" / "t"
        (self.d / "hosts.toml").write_text(f'[[hosts]]\nid = "t"\nworker_home = "{self.d / "home"}"\n')
        self.old = {k: os.environ.get(k) for k in ("LABMON_HOSTS", "LABMON_STATE")}
        os.environ.update(LABMON_HOSTS=str(self.d / "hosts.toml"), LABMON_STATE=str(self.d / "state"))
        O._SENT.clear()
        # 第一轮 a0（不用卡）与 b1 一起放行，a0 立刻跑完；第二轮 a1 要的显存 b1 占着：等。看板读的那几秒 worker 被 SIGSTOP 冻住、状态不变，
        # b1 停在闸门上一直跑着（不靠它睡得久：CI 慢时会在看板读完之前跑完）
        self.roots = {"pa": self.queue("pa", ["a0|true   # gpus=0 est_min=1", "a1|sleep 0.5   # vram=30 est_min=1"]),
                      "pb": self.queue("pb", [f"b1|{gate(self.d)}   # vram=30 est_min=1"])}
        self.cfgs = {pid: self.project(pid) for pid in self.roots}
        self.worker = subprocess.Popen([sys.executable, str(WORKER), "run", "--host", "t", "--replace"], env=self.env)

    def tearDown(self):
        try:
            os.kill(self.worker.pid, signal.SIGCONT)
        except OSError:
            pass
        self.worker.terminate()
        self.worker.wait()
        for k, v in self.old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def queue(self, pid, lines):
        root = self.d / pid
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("\n".join(lines) + "\n")
        (root / "jobs" / "q.env").write_text("QUEUE_STATE=state\nQUEUE_LOGS=logs\n")
        r = subprocess.run([sys.executable, str(WORKER), "add", str(root), "jobs/q.txt", "--project", pid, "--host", "t"], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return root

    def project(self, pid):
        """项目 pid 的 monitor.toml：主机 t（本机、不经 ssh；worker_home 从用户级主机清单合并进来）、一条队列；登记到看板上（#161）。"""
        cfg = self.d / pid / "monitor.toml"
        cfg.write_text(f'id = "{pid}"\ntitle = "项目 {pid}"\n[[hosts]]\nid = "t"\n'
                       f'[[queues]]\nid = "q"\nroot = "{self.roots[pid]}"\njobs = "jobs/q.txt"\n')
        register(cfg)
        return cfg

    def status(self):
        try:
            return json.loads((self.home / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def until(self, cond, timeout=20):
        t = time.time()
        while time.time() - t < timeout:
            if cond():
                return
            time.sleep(0.1)
        self.fail("等待超时")

    def chain(self):
        """看板读一遍：项目看板各采集一次 → 工作台采集（含 sched_view、occupancy）；返回 (各项目快照, proj_info, 工作台快照)。"""
        snaps = {pid: C.collect(load(cfg)) for pid, cfg in self.cfgs.items()}
        info = Overview([Project(pid) for pid in self.cfgs]).proj_info()
        return snaps, info, O.collect(info)

    def check_js(self, html: Path):
        """抽出页面里的 <script>，node --check 能过（数据内联后仍是合法脚本）。"""
        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node")
        for i, body in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html.read_text(), re.S)):
            f = html.with_name(f"{html.stem}_{i}.js")
            f.write_text(body)
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_fields_flow_through(self):
        ready = lambda s: s.get("running") and s.get("waiting") and (self.home / "usage.jsonl").exists()   # noqa: E731
        self.until(lambda: ready(self.status()))
        os.kill(self.worker.pid, signal.SIGSTOP)              # 冻住 worker：读的这几秒状态不变（任务进程照跑）
        try:
            st0 = self.status()
            snaps, info, snap = self.chain()
        finally:
            os.kill(self.worker.pid, signal.SIGCONT)
        run, wait = st0["running"][0], st0["waiting"][0]
        self.assertEqual({run["project"], wait["project"]}, {"pa", "pb"})

        # ---- 工作台快照里这台主机：worker 写的字段原样到页面，项目 id 就是登记的 ----
        h = snap["hosts"][0]
        self.assertEqual((h["id"], h["worker_up"], h["status_error"], h["error"]), ("t", True, None, None))
        self.assertIsInstance(h["clock"], int)
        st = h["status"]
        j = st["running"][0]
        self.assertEqual((j["name"], j["project"], j["link"]), (run["name"], run["project"], run["project"]))   # 看板上有这个项目：链到项目页
        self.assertIsInstance(j["t0"], NUM)
        self.assertLess(time.time() - j["t0"], 60)
        self.assertNotIn("start", j)
        self.assertEqual(j["gpus"], ["GPU-aaa"])
        self.assertEqual((st["kafight"], st["watchdogs"]), (None, {"frozen": [], "seen": []}))           # 保活没被拉起、不是借来的
        self.assertIn("throttle", st)                                                                    # CPU 限流（#39）：测试里读不到 cpu.stat，没在限流为 None
        self.assertIsNone(st["throttle"])
        self.assertIsInstance(st["ka_stop"], NUM)                                                        # 有任务要卡：worker 停着保活，停它的时刻（重启时找回，#124）
        for k in ("pid", "cmd", "tail", "log", "use", "est", "hold", "kind", "queue", "root", "cpus", "io", "io_sus"):   # 任务卡展开要用的
            self.assertIn(k, j)
        self.assertLessEqual({"cpus", "cpu_src"}, set(j["est"]))                                       # CPU 估计与记账（主机页 CPU 一行、任务详情）
        self.assertIsInstance(j["acct"]["cpu"], NUM)
        self.assertLessEqual({"ram", "margin", "gpus", "cpu", "cpu_cap", "other_cpu", "owner"}, set(st["room"]))   # 测试里读不到配额：cpu、cpu_cap 为 None；owner 不放的卡、other_cpu（fit 用）
        w = st["waiting"][0]
        self.assertEqual((w["name"], w["project"], w["link"], w["held"], w["dep"]), (wait["name"], wait["project"], wait["project"], None, None))
        self.assertEqual(w["pos"]["n"], 1)                                                              # 排位：等待表“排第几”
        self.assertIsInstance(w["start"], NUM)                                                            # 推演里预计开始（epoch 秒）
        self.assertGreaterEqual(w["start"], j["t0"])
        self.assertIsInstance(w["since"], NUM)
        self.assertIn("显存", w["why"])
        self.assertIsNone(w["cpu_wait"])                                                                 # 没被 CPU 挡：不告警
        self.assertLessEqual({"cpus", "cpu_src"}, set(w["est"]))
        d = h["done"]
        self.assertEqual([(r["name"], r["project"], r["link"]) for r in d], [("a0", "pa", "pa")])
        self.assertTrue(isinstance(d[0]["t0"], int) and isinstance(d[0]["t1"], int) and d[0]["t0"] <= d[0]["t1"])
        self.assertFalse({"start", "end"} & set(d[0]))
        self.assertEqual((d[0]["rc"], d[0]["gpus"], d[0]["wait"]), ("0", [], 0.0))
        self.assertLessEqual({"cores_peak", "rd_peak", "wr_peak", "rd_b", "wr_b", "cpus", "io"}, set(d[0]))      # CPU 与读写的测量、清单提示（期 1 只记录）
        self.assertIsNone(d[0]["oom"])                                                                    # 失败是不是 OOM（#62）：跑成功的为 None
        self.assertEqual(set(st["info"]["press"]), {"thr", "psi", "nfs", "disk"})                         # 主机压力：读不到的项为 None（测试里没有假 /proc）
        self.assertIsNone(st["info"]["rootfs"])                                                           # 根盘开机以来的新写入（#149）：测试里根盘指到不存在的目录
        self.assertEqual(set(st["storage"]), {"t", "local", "own", "wrote", "projects"})                  # 本地盘（LocalDisk.summary）：测不准时只扫 labmon 目录，wrote 为 None
        self.assertIsNone(st["storage"]["wrote"])
        self.assertEqual(text_times(st) + text_times(d), [])                                              # 状态里没有文字时刻
        # 队列与推演：lanes 带 root / queue / project，jobs 的 t0 / t1 是 epoch 秒、gpus 是卡序号；等着的写明等谁
        self.assertEqual({q["project"] for q in st["queues"]}, {"pa", "pb"})
        for q in st["queues"]:
            self.assertEqual(set(q), {"root", "jobs", "gpus", "project", "stop", "empty", "added_t", "dur"})
            self.assertIsInstance(q["added_t"], int)                                                      # 登记时刻（epoch 秒）：看板在同一清单登记在两台时取晚的（#94）
        lanes = {ln["project"]: ln for ln in st["plan"]["lanes"]}
        self.assertEqual(set(lanes), {"pa", "pb"})
        for ln in lanes.values():
            self.assertLessEqual({"root", "queue", "project", "kind", "jobs", "stuck", "stuck_dep"}, set(ln))
            self.assertIsNone(ln["stuck_dep"])                                                            # 停在依赖上的那一行（#183）；都排上了为 null
            for x in ln["jobs"]:
                self.assertIsInstance(x["t0"], int)
                self.assertTrue(x["t1"] is None or isinstance(x["t1"], int))
                self.assertEqual(x["gpus"], [0])
        pr, pw = lanes[run["project"]]["jobs"][0], lanes[wait["project"]]["jobs"][0]
        self.assertEqual((pr["name"], pr["run"]), (run["name"], True))
        self.assertEqual((pw["name"], pw["after"]), (wait["name"], [[run["project"], run["name"]]]))
        self.assertAlmostEqual(pw["t0"], w["start"], delta=1)                                             # 等待表的预计开始取自推演
        self.assertEqual(set(st["share"]), {"wait_cap_min", "half_life_h", "weight", "use", "run"})
        self.assertEqual(st["share"]["run"][0][0], run["project"])
        for ln in st["todo"]:                                                                             # 还会跑的（list_state）：别的主机按它认依赖、offers 按它取各清单还没跑的行
            self.assertEqual(set(ln), {"project", "root", "queue", "jobs", "deps", "held", "gang"})   # gang：成组的行 {任务: [组名, 人数]}（#180）
        self.assertEqual({(ln["project"], ln["queue"]) for ln in st["todo"]}, {(q["project"], q["jobs"]) for q in st["queues"]})
        self.assertEqual(st["offers"], [])                                                                # 只有一台：没有别处的任务可接（offers）
        self.assertEqual(snap["rebalance"], {})                                                           # 也就没有挪法建议

        # ---- 调度卡：各项目一行、键齐全 ----
        S = snap["sched"]
        self.assertIsInstance(S["cap_min"], NUM)
        self.assertIsInstance(S["hl_h"], NUM)
        self.assertIsInstance(S["use_sum"], NUM)
        sp = {p["id"]: p for p in S["projects"]}
        self.assertEqual(set(sp), {"pa", "pb"})
        for p in sp.values():
            self.assertEqual(set(p), {"id", "person", "pk", "puse", "w", "use", "cards", "queued", "waiting", "wait_avg", "wait_n", "focus", "paused"})
        self.assertEqual((sp[wait["project"]]["waiting"], sp[wait["project"]]["queued"]), (1, 1))
        self.assertGreater(sp[run["project"]]["cards"], 0)
        self.assertEqual((sp["pa"]["wait_n"], sp["pa"]["wait_avg"]), (1, 0.0))                             # a0 一来就放行：等待 0 分钟

        # ---- 占用（时间线）：在跑与排队的来自推演，车道名是项目看板定的 ----
        occ = {x["n"]: x for x in snap["occ"]["t"]}
        self.assertEqual(set(occ), {run["name"], wait["name"]})
        for x in occ.values():
            self.assertLessEqual({"p", "n", "lane", "t0", "t1", "g", "st"}, set(x))
            self.assertEqual((x["lane"], x["g"]), ("车道 1", [0]))
        self.assertEqual((occ[run["name"]]["st"], occ[wait["name"]]["st"], occ[wait["name"]]["after"]), ("run", "plan", [[run["project"], run["name"]]]))
        self.assertEqual(text_times(snap["occ"]) + text_times(S), [])

        # ---- 项目看板：worker_view 认出它在管的队列，快照里排队任务的预计时间只取推演 ----
        key = lambda pid: os.path.normpath(str(self.roots[pid] / "jobs" / "q.txt"))   # noqa: E731
        wv = P.worker_view(str(self.home / "status.json"), STALE_S)
        self.assertEqual(set(wv), {key("pa"), key("pb")})
        self.assertEqual((wv[key(wait["project"])]["wait"], wv[key(wait["project"])]["held"], wv[key(wait["project"])]["dep"]), (wait["why"], None, None))
        self.assertIsNone(wv[key(run["project"])]["wait"])
        self.assertEqual(wv[key(wait["project"])]["plan"][wait["name"]]["after"], [[run["project"], run["name"]]])
        self.assertTrue(wv[key(run["project"])]["plan"][run["name"]]["run"])
        pa, pb = snaps["pa"], snaps["pb"]
        self.assertEqual((pa["id"], pb["id"]), ("pa", "pb"))
        jobs = {j["id"]: j for s in snaps.values() for j in s["jobs"]}
        self.assertEqual({k: v["status"] for k, v in jobs.items()}, {"a0": "done", run["name"]: "running", wait["name"]: "queued"})
        self.assertTrue(isinstance(jobs["a0"]["end_ts"], NUM) and isinstance(jobs["a0"]["actual_min"], NUM))
        self.assertTrue(isinstance(jobs[run["name"]]["start_ts"], NUM) and isinstance(jobs[run["name"]]["eta_ts"], NUM))
        self.assertAlmostEqual(jobs[wait["name"]]["plan_start_ts"], w["start"], delta=1)
        self.assertEqual(jobs[wait["name"]]["after"], [[run["project"], run["name"]]])
        self.assertEqual((jobs[wait["name"]]["dur_min"], jobs[wait["name"]]["dur_src"]), (1.0, "清单估时"))
        lane = {s["id"]: s["lanes"][0] for s in snaps.values()}
        self.assertEqual((lane[wait["project"]]["managed"], lane[wait["project"]]["waiting"], lane[wait["project"]]["label"]), (True, wait["why"], "车道 1"))
        self.assertTrue(lane[run["project"]]["alive"])
        self.assertEqual((lane["pa"]["root"], lane["pa"]["queue"]), (str(self.roots["pa"]), "jobs/q.txt"))   # 工作台按它给 worker 的任务标车道名
        self.assertEqual([a["key"] for s in snaps.values() for a in s["alerts"]], [])
        # proj_info 把项目看板算的预计结束带给工作台，工作台再写进 worker 的 eta.json（键是 项目|任务，worker 的 load_eta 按它认）
        pi = {p["id"]: p for p in info}
        self.assertEqual(pi["pa"]["lanes"], [{"host": "t", "root": str(self.roots["pa"]), "queue": "jobs/q.txt", "label": "车道 1",
                                               "unfinished": 1, "returned": None, "run": 0}])   # 项目「管理」抽屉要的未完成、在跑几个（#93）
        # 登记的全局视图（#93）：在用主机取 worker 状态里的 queues，项目 id 是 worker add --project 写的
        self.assertEqual([(x["host"], x["state"], sorted((q["project"], q["jobs"]) for q in x["queues"])) for x in snap["regs"]],
                         [("t", "active", [("pa", "jobs/q.txt"), ("pb", "jobs/q.txt")])])
        self.assertEqual([r["id"] for r in pi[run["project"]]["running"]], [run["name"]])
        eta = json.loads((self.home / "eta.json").read_text())
        self.assertEqual(list(eta["jobs"]), [f"{run['project']}|{run['name']}"])
        self.assertAlmostEqual(eta["jobs"][f"{run['project']}|{run['name']}"], jobs[run["name"]]["eta_ts"], delta=60)
        sj = json.loads((self.home / "sched.json").read_text())
        self.assertEqual(sj, {"owner": {k: sj["owner"]["pa"] for k in ("pa", "pb")}})   # 没有 [sched]；两个项目都归管理员、算一个人（#23）
        # 挪法建议的告警（rebalance:）只给实验会话：项目快照里有，工作台的项目摘要经 C.page_alerts 不计数、不列（#40）
        glob, text = C.snapshot(), "把 t 上 jobs/q.txt 的 a1 挪到 t2：整批预计提前 2.0 小时"
        glob["rebalance"] = {"pa": {"key": "rebalance:pa", "target": "t2", "jobs": [], "from": None, "to": 0, "cut": [], "text": text}}
        with mock.patch.object(C, "snapshot", lambda: glob):
            self.assertEqual([(a["key"], a["msg"]) for a in C.collect(load(self.cfgs["pa"]))["alerts"]], [("rebalance:pa", text)])
        pi = {p["id"]: p for p in Overview([Project(pid) for pid in self.cfgs]).proj_info()}
        self.assertEqual((pi["pa"]["alerts"], pi["pa"]["alert_msgs"]), (0, []))

        # ---- 两个页面渲染出来脚本仍合法，数据按 epoch 内联 ----
        ov = state_dir() / "index.html"
        self.assertIn('"t0":', ov.read_text())
        self.assertNotIn('"start": "20', ov.read_text())
        self.check_js(ov)
        self.check_js(PJ.out_dir("pa") / "index.html")

        # ---- 都跑完以后：done 三行、占用条按本机存的用量记录画（带卡序号 gi），没有在跑的 ----
        release(self.d)
        self.until(lambda: all((self.roots[p] / "state" / f"{n}.done").exists() for p, n in (("pa", "a1"), ("pb", "b1"))) and not self.status().get("running"), 40)
        snaps, info, snap = self.chain()
        h = snap["hosts"][0]
        self.assertEqual(sorted((r["name"], r["project"]) for r in h["done"]), [("a0", "pa"), ("a1", "pa"), ("b1", "pb")])
        for r in h["done"]:
            self.assertTrue(isinstance(r["t0"], int) and isinstance(r["t1"], int) and not {"start", "end"} & set(r))
        self.assertEqual((h["status"]["running"], h["status"]["waiting"]), ([], []))
        self.assertEqual({q["empty"] for q in h["status"]["queues"]}, {True})
        rows = load_rows(state_dir() / "usage" / "t.jsonl")
        self.assertEqual([(r["n"], r["p"], r["gi"], r["st"]) for r in rows], [("a0", "pa", [], "done"), (run["name"], run["project"], [0], "done"), (wait["name"], wait["project"], [0], "done")])
        occ = {x["n"]: x for x in snap["occ"]["t"]}
        self.assertEqual({(x["st"], x["lane"]) for x in occ.values()}, {("done", "车道 1")})
        self.assertEqual(set(occ), {run["name"], wait["name"]})                                            # a0 不用卡：不画
        self.assertEqual(snap["sched"]["projects"] and {p["id"]: p["cards"] for p in snap["sched"]["projects"]}, {"pa": 0, "pb": 0})
        self.assertEqual({j["status"] for s in snaps.values() for j in s["jobs"]}, {"done"})
        self.assertEqual(text_times(h["status"]) + text_times(h["done"]), [])
        # 项目“暂时没实验”看的是工作台上一轮快照里 worker 的 running / waiting：工作台采过空的这一轮后，项目下一次采集才标
        self.assertEqual([s["no_work_since"] for s in snaps.values()], [None, None])
        self.assertTrue(all(C.collect(load(cfg))["no_work_since"] for cfg in self.cfgs.values()))


if __name__ == "__main__":
    unittest.main()
