"""labmon worker 的离线测试：起真 worker 的（并行、选卡、接管、保活、与旧 gpu_queue.sh 共存，用假 GPU LABMON_GPUS 和假 ka.sh）
与不起进程、直接调函数的（准入、借来的主机、外来进程与作废、放行顺序、时长与份额、推演）。夹具在 fixtures.py。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import signal
import datetime
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import WORKER, Clock, Hosts, dead_pid, gate, load_worker, release, running_job, usage_row, worker_env, worker_module  # noqa: E402

from labmon import probe as P  # noqa: E402
from labmon import rebalance as RB  # noqa: E402


class WorkerBase(unittest.TestCase):
    """起真 worker 进程的公共部分：临时目录、假保活、登记队列、等条件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = worker_env(self.d)                 # 假保活本来在跑、一张 40 GiB 空卡
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.terminate()
            p.wait()
        self.tmp.cleanup()

    def w(self, *args):
        return subprocess.run([sys.executable, str(WORKER), *args, "--host", "t"], env=self.env,
                              capture_output=True, text=True)

    def start(self):
        p = subprocess.Popen([sys.executable, str(WORKER), "run", "--host", "t", "--replace"], env=self.env)
        self.procs.append(p)
        return p

    def queue(self, name, lines, env="", *opts):
        root = self.d / name
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("\n".join(lines) + "\n")
        (root / "jobs" / "q.env").write_text("QUEUE_STATE=state\nQUEUE_LOGS=logs\n" + env)
        if "--project" not in opts:                                           # add 必写项目 id：缺省用目录名
            opts += ("--project", name)
        self.assertEqual(self.w("add", str(root), "jobs/q.txt", *opts).returncode, 0)
        return root

    def status(self):
        st = self.d / "home" / "hosts" / "t" / "status.json"
        try:
            return json.loads(st.read_text())
        except (OSError, ValueError):
            return {}

    def gate(self, name="go"):
        """任务命令里的闸门：停在这里直到 release(name)（fixtures.gate）。"""
        return gate(self.d, name)

    def release(self, name="go"):
        release(self.d, name)

    def usage(self):
        """本机 usage.jsonl 的各行（还没有为空）。收尾先写 .done 再写用量行：要读用量行就等它，不以 .done 为准。"""
        f = self.d / "home" / "hosts" / "t" / "usage.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def until(self, cond, timeout=15):
        t = time.time()
        while time.time() - t < timeout:
            if cond():
                return True
            time.sleep(0.1)
        self.fail("等待超时")


class WorkerTest(WorkerBase):
    def test_parallel_queues_marks_and_probe(self):
        a = self.queue("a", [f"a1|{self.gate()}; echo $X $CUDA_VISIBLE_DEVICES   # est_min=1", "a2|exit 3"], env="X=从env来\n")
        b = self.queue("b", [f"b1|{self.gate()}   # gpus=0"])
        self.start()
        self.until(lambda: (a / "state" / "current").exists() and (b / "state" / "current").exists())   # 两条队列并行
        lane = P.queue_gpu_queue({"root": str(a), "jobs": "jobs/q.txt"})
        self.assertTrue(lane["lock_alive"])                     # 看板认得 worker 的锁
        self.release()
        self.until(lambda: (a / "state" / "a2.failed").exists() and (b / "state" / "b1.done").exists())
        self.assertTrue((a / "state" / "a1.done").exists())
        self.assertIn("从env来 GPU-aaa", (a / "logs" / "a1.log").read_text())
        self.until(lambda: not (a / "state" / "lock").exists())
        log = (a / "state" / "queue.log").read_text()
        for s in ("队列启动", "开始 a1（GPU 0 GPU-aaa）", "完成 a1（0 分钟）", "失败 a2（退出码 3", "队列结束"):
            self.assertIn(s, log)
        self.until(lambda: (self.d / "keepalive_demo" / "ka_on").exists())   # 跑完恢复保活
        self.assertEqual((self.d / "keepalive_demo" / "ka.log").read_text().split(), ["stop", "start"])

    def test_waits_for_memory(self):
        self.env["LABMON_GPUS"] = "GPU-aaa:40000:80000"           # 卡 80 GiB、空 40 GiB：60 GiB 放得下，只是现在没空
        a = self.queue("a", ["big|true   # vram=60", "small|true"])
        self.start()
        st = self.d / "home" / "hosts" / "t" / "status.json"
        self.until(lambda: st.exists() and json.loads(st.read_text())["waiting"])
        w = json.loads(st.read_text())["waiting"][0]
        self.assertEqual(w["name"], "big")
        self.assertIn("60 GiB", w["why"])
        self.assertFalse((self.d / "keepalive_demo" / "ka_on").exists())   # 等卡期间保活保持停着，不来回停启
        self.assertFalse((a / "state" / "small.done").exists())  # 队列内按顺序，不越过等待中的任务
        self.assertIn("排第 1，预计开始 马上）", self.w("ls").stdout)   # 命令行的等待表也写排位与预计开始，与看板一致
        # 等待原因记成事件（项目页时间线据此解释空档）；原因里的数字每轮在变，按类别只记一次（测试里轮与轮之间不睡，0.6 秒已是许多轮）
        time.sleep(0.6)
        ev = [json.loads(l) for l in (self.d / "home" / "hosts" / "t" / "events.jsonl").read_text().splitlines()]
        wt = [e for e in ev if e["kind"] == "wait"]
        self.assertEqual(len(wt), 1)
        self.assertEqual((wt[0]["name"], wt[0]["queue"], wt[0]["root"]), ("big", "jobs/q.txt", str(a)))
        self.assertIn("60 GiB", wt[0]["why"])

    def test_status_has_cmd_tail_and_wait_since(self):
        # 页面展开任务要看命令与日志末尾；等待列表要写已等多久（同一个任务一直等就不重置）
        self.queue("a", [f"t|echo hello; {self.gate()}   # est_min=5 gpus=0"])
        self.queue("b", ["big|true   # vram=60"])
        self.start()
        st = self.d / "home" / "hosts" / "t" / "status.json"
        load = lambda: json.loads(st.read_text()) if st.exists() else {}
        self.until(lambda: load().get("running") and load()["running"][0]["tail"][-1:] == ["hello"] and load().get("waiting"))
        j = load()["running"][0]
        self.assertIn("echo hello", j["cmd"])
        self.assertEqual((j["est_min"], j["log"]), (5.0, "logs/t.log"))
        self.assertTrue(isinstance(j["t0"], float) and 0 < time.time() - j["t0"] < 60 and "start" not in j)   # 开始时刻只写 epoch 秒
        since = load()["waiting"][0]["since"]
        time.sleep(0.5)
        self.assertEqual(load()["waiting"][0]["since"], since)

    def test_idle_start_brings_up_keepalive(self):
        # 服务器重启后手动拉起 worker：没有队列、GPU 上也没进程，就像看门狗一样拉起保活；GPU 上有进程则不拉
        (self.d / "keepalive_demo" / "ka_on").unlink()
        self.env["LABMON_GPU_APPS"] = "4242"
        p = self.start()
        time.sleep(1.2)                                         # 多于 KA_CHECK_S（3 × LABMON_TICK = 0.9 秒）：至少又看过一次
        self.assertFalse((self.d / "keepalive_demo" / "ka_on").exists())
        p.terminate()
        p.wait()
        self.env["LABMON_GPU_APPS"] = ""
        self.start()
        self.until(lambda: (self.d / "keepalive_demo" / "ka_on").exists())

    def test_restart_adopts_running_job(self):
        a = self.queue("a", [f"long|{self.gate()}"])
        p = self.start()
        self.until(lambda: (a / "state" / "current").exists())
        p.terminate()
        p.wait()
        pid = int((a / "state" / "current").read_text().split()[1])
        os.kill(pid, 0)                                         # worker 退出，任务还在跑
        q = self.start()
        self.until(lambda: (a / "state" / "lock").read_text() == str(q.pid))   # 接管后 lock 指向新 worker
        self.release()
        self.until(lambda: (a / "state" / "long.done").exists())
        self.assertEqual((a / "state" / "queue.log").read_text().count("开始 long"), 1)   # 没有重跑

    def test_replace_waits_for_old_worker(self):
        # --replace 接管：旧 worker 正在一个长 tick 里（拷文件、遍历 /tmp），新进程一直等到它退出再接手，不是 30 秒就放弃
        home = self.d / "home" / "hosts" / "t"
        home.mkdir(parents=True)
        old = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, lambda *a: None); time.sleep(1.5)",
                                "labmon_worker"])
        self.procs.append(old)
        (home / "worker.pid").write_text(str(old.pid))
        t0 = time.time()
        p = self.start()
        self.until(lambda: (home / "worker.pid").read_text() == str(p.pid), timeout=20)
        self.assertGreaterEqual(time.time() - t0, 1.2)
        self.assertIsNotNone(old.poll())

    def test_interrupted_job_requeued(self):
        a = self.queue("a", ["j|true"])
        st = a / "state"
        st.mkdir()
        (st / "current").write_text("j 999999 2026-11-04 10:00:00")   # 服务器重启前在跑、没留退出码
        self.start()
        self.until(lambda: (st / "j.done").exists())
        self.assertIn("中断 j", (st / "queue.log").read_text())

    def test_exclusive_gpu_waits_for_empty_card(self):
        # 独占任务轮到时把卡留住：卡上的任务跑完才启动它，期间后来的、不知道要跑多久的任务也不往这张卡放
        a = self.queue("a", [f"a1|{self.gate('a1')}"])
        self.start()
        self.until(lambda: (a / "state" / "current").exists())
        b = self.queue("b", [f"e|{self.gate('e')}   # exclusive=gpu"])
        self.until(lambda: "等卡" in json.dumps(self.status().get("waiting", []), ensure_ascii=False))
        c = self.queue("c", ["c1|true"])
        self.until(lambda: "c1" in [w["name"] for w in self.status().get("waiting", [])])   # a1 还在跑：c1 也排着，不往留着的卡放
        self.release("a1")
        self.until(lambda: (b / "state" / "current").exists())
        self.assertTrue((a / "state" / "a1.done").exists())
        self.assertFalse((c / "state" / "c1.done").exists())
        self.release("e")
        self.until(lambda: len(self.usage()) == 3)                            # 收尾先写 .done、后写用量行：等用量行齐了再读（3f3ea21 的 CI 在这里偶发失败）
        self.assertTrue((c / "state" / "c1.done").exists())
        recs = self.usage()
        self.assertEqual([(r["name"], r["excl"]) for r in recs], [("a1", None), ("e", "gpu"), ("c1", None)])
        for r in recs:                                                        # 起止时刻只写 epoch 秒（整数），项目 id 就是登记的
            self.assertTrue(isinstance(r["t0"], int) and isinstance(r["t1"], int) and r["t0"] <= r["t1"] <= time.time() + 1)
            self.assertFalse({"start", "end"} & set(r))
        self.assertEqual([r["project"] for r in recs], ["a", "b", "c"])

    def test_deadline_while_queue_runs(self):
        # 队列正在跑、下一个排着时推演要看它会不会被截止指令挡住（11-07 这里漏传队列 key，worker 出错退出）
        a = self.queue("a", ["a1|sleep 1   # est_min=1", "a2|true   # est_min=600"])
        (self.d / "home" / "hosts" / "t").mkdir(parents=True, exist_ok=True)
        (self.d / "home" / "hosts" / "t" / "overrides.json").write_text(json.dumps([{"kind": "deadline", "at": time.time() + 3600}]))
        self.start()
        self.until(lambda: (a / "state" / "a1.done").exists())
        self.until(lambda: "截止" in json.dumps(self.status().get("waiting", []), ensure_ascii=False))
        self.assertNotIn("出错退出", (self.d / "home" / "hosts" / "t" / "worker.log").read_text())

    def test_exclusive_gpu_takes_free_card(self):
        self.env["LABMON_GPUS"] = "GPU-aaa:40000,GPU-bbb:40000"
        a = self.queue("a", [f"a1|{self.gate()}"])
        self.start()
        self.until(lambda: (a / "state" / "current").exists())
        b = self.queue("b", ["e|true   # exclusive=gpu"])
        self.until(lambda: (b / "state" / "e.done").exists())
        self.assertFalse((a / "state" / "a1.done").exists())          # 另一张卡空着，不用等
        self.assertIn("开始 e（GPU 1 GPU-bbb）", (b / "state" / "queue.log").read_text())

    def test_paused_project_leaves_keepalive_on(self):
        # 暂停的项目还有 GPU 任务排着：不会放行，就不为它停保活（以前卡空着、保活也不开，平台可能按空闲关机）
        self.queue("a", ["g|sleep 1"], "", "--project", "p1")
        self.assertIn("暂停 p1", self.w("override", "add", "pause", "p1").stdout)
        self.start()
        self.until(lambda: "暂停中" in json.dumps(self.status().get("waiting", []), ensure_ascii=False))
        time.sleep(0.5)
        self.assertTrue((self.d / "keepalive_demo" / "ka_on").exists())

    def test_hold_obeys_drain(self):
        # 占位任务也守临时指令：drain 下不起（以前绕过所有指令，预备续租等不到“跑完了”）
        a = self.queue("a", ["hold_x|sleep 30   # gpus=0"])
        self.w("override", "add", "drain")
        self.start()
        self.until(lambda: "drain" in json.dumps(self.status().get("waiting", []), ensure_ascii=False))
        self.assertFalse((a / "state" / "current").exists())


class RegistryTest(WorkerBase):
    def test_add_rm_by_content(self):
        # 两个项目的子目录同名、清单路径也相同：各登记一份，不互相覆盖；再 add 同一条沿用原文件；rm 只删自己的
        roots = [self.d / "a1" / "smoke", self.d / "bp" / "smoke"]
        for i, r in enumerate(roots):
            (r / "jobs").mkdir(parents=True)
            (r / "jobs" / "q.txt").write_text("x|true\n")
            (r / "jobs" / "q.env").write_text(f"QUEUE_STATE=state{i}\n")
        qd = self.d / "home" / "hosts" / "t" / "queues"
        for r in roots:
            self.assertEqual(self.w("add", str(r), "jobs/q.txt", "--project", r.parent.name).returncode, 0)
        self.assertEqual(len(list(qd.glob("*.json"))), 2)
        self.assertEqual(self.w("add", str(roots[0]), "jobs/q.txt", "--project", "a1", "--stop", "term").returncode, 0)
        self.assertEqual(len(list(qd.glob("*.json"))), 2)
        self.w("rm", str(roots[0]), "jobs/q.txt")
        self.assertEqual([json.loads(f.read_text())["project"] for f in qd.glob("*.json")], ["bp"])

    def test_invalid_stop_keeps_existing_registration(self):
        root = self.queue("q", ["x|true"])
        (root / "escape").symlink_to(self.d, target_is_directory=True)
        reg = next((self.d / "home" / "hosts" / "t" / "queues").glob("*.json"))
        before = reg.read_bytes()
        for stop in ("file:../outside", "file:/outside", "file:escape/stop"):
            with self.subTest(stop=stop):
                result = self.w("add", str(root), "jobs/q.txt", "--project", "q", "--stop", stop)
                self.assertEqual(result.returncode, 2, result)
                self.assertEqual(reg.read_bytes(), before)
        moved = subprocess.run([sys.executable, str(WORKER), "add", str(root), "jobs/q.txt", "--project", "q",
                                "--stop", "file:../outside", "--host", "u"], env=self.env, capture_output=True, text=True)
        self.assertEqual(moved.returncode, 2, moved)
        self.assertEqual(reg.read_bytes(), before)
        self.assertFalse((self.d / "home" / "hosts" / "u" / "queues").exists())
        result = self.w("add", str(root), "jobs/q.txt", "--project", "q", "--stop", "file:signals/stop")
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual(json.loads(reg.read_text())["stop"], "file:signals/stop")

    def test_add_refuses_shared_state_dir(self):
        # 两条队列的状态目录相同：current、lock、完成标记会互相覆盖，不让登记
        for n in ("a", "b"):
            (self.d / "r" / "jobs").mkdir(parents=True, exist_ok=True)
            (self.d / "r" / "jobs" / f"{n}.txt").write_text("x|true\n")
            (self.d / "r" / "jobs" / f"{n}.env").write_text("QUEUE_STATE=state\n")
        self.assertEqual(self.w("add", str(self.d / "r"), "jobs/a.txt", "--project", "r").returncode, 0)
        r = self.w("add", str(self.d / "r"), "jobs/b.txt", "--project", "r")
        self.assertEqual(r.returncode, 2)
        self.assertIn("状态目录", r.stderr)

    def test_add_requires_project(self):
        # 项目 id 只认登记时写的 --project（就是看板项目 id），不写就不登记
        (self.d / "r" / "jobs").mkdir(parents=True)
        (self.d / "r" / "jobs" / "a.txt").write_text("x|true\n")
        r = self.w("add", str(self.d / "r"), "jobs/a.txt")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--project", r.stderr)
        qd = self.d / "home" / "hosts" / "t" / "queues"
        self.assertEqual(list(qd.glob("*.json")) if qd.exists() else [], [])


class AdmissionTest(WorkerBase):
    """3b 准入：按记账放行、历史估计、tag 限额、单元申请、暂停与停法。"""

    def test_accounting_reserves_estimate(self):
        # 刚启动的任务显存还没占上：按估计值记账，第三个放不下就等
        qs = [self.queue(n, [f"{n}1|{self.gate()}   # vram=15"]) for n in "abc"]
        self.start()
        self.until(lambda: len(self.status().get("running", [])) == 2 and self.status().get("waiting"))
        self.assertIn("没有可用显存够 15 GiB", self.status()["waiting"][0]["why"])
        self.release()
        self.until(lambda: all((q / "state" / f"{q.name}1.done").exists() for q in qs))

    def test_new_tag_hint_beats_queue_history(self):
        # #61：三条车道各有一条旧 tag=sm_train、峰值 6000 MB 的历史，各追加一条新 tag 的 vram=24：同一条队列别的任务的历史不盖过明写的提示值，
        # 40 GiB 的卡按 24+1 只放 1 条（以前按同队列 6.45 GiB 估，三条同时放行，现场 OOM）
        h = self.d / "home" / "hosts" / "t"
        h.mkdir(parents=True)
        now = time.time()
        (h / "usage.jsonl").write_text("".join(json.dumps(usage_row("old_" + n, n, now - 900, now - 600, root=str(self.d / n), tag="sm_train",
                                                                    gpus=["GPU-aaa"], ram_peak=1.0, vram_peak=6000.0)) + "\n" for n in "abc"))
        for n in "abc":
            self.queue(n, [f"vit_{n}|{self.gate()}   # vram=24 ram=12 tag=sm_vit"])
        self.start()
        self.until(lambda: self.status().get("running") and len(self.status().get("waiting") or []) == 2)
        st = self.status()
        self.assertEqual(len(st["running"]), 1)
        self.assertEqual({k: st["running"][0]["est"][k] for k in ("vram", "src", "ram", "ram_src")}, {"vram": 24.0, "src": "提示值", "ram": 12.0, "ram_src": "提示值"})
        self.assertIn("没有可用显存够 24 GiB 的卡（提示值", st["waiting"][0]["why"])

    def test_history_estimate_overrides_hint(self):
        # 同类任务（去掉种子编号同名）历史峰值 37 GiB：提示只写 1 GiB 也按 37×1.1 估；超过这张 40 GiB 卡就按整卡算，卡空着照放，
        # 记账照估计值，跑着时这张卡不再放别的
        h = self.d / "home" / "hosts" / "t"
        h.mkdir(parents=True)
        (h / "usage.jsonl").write_text(json.dumps({"name": "x_s0", "project": "a", "rc": "0", "min": 30, "gpus": ["GPU-aaa"],
                                                   "ram_peak": 1.0, "vram_peak": 38000}) + "\n")
        self.queue("a", [f"x_s1|{self.gate()}   # vram=1"])
        self.start()
        self.until(lambda: self.status().get("running"))
        self.queue("b", ["small|true   # vram=1"])
        self.until(lambda: self.status().get("waiting"))
        st = self.status()
        self.assertEqual([j["name"] for j in st["running"]], ["x_s1"])
        self.assertIn("没有可用显存够 1 GiB 的卡", st["waiting"][0]["why"])
        self.assertEqual(st["room"]["gpus"], {"GPU-aaa": 0.0})

    def test_cuda_oom_raises_next_estimate(self):
        # #62：清单任务打印 CUDA OOM 退出 1：用量行记 oom、放行时的估计；删 .failed 重排后按估计 ×2 = 24 GiB 等着，不再放进 hog 旁边原来的空位
        # （40000 MB 的卡，hog 记 15 GiB，剩 24.1 GiB < 24+1）；hog 跑完后放行，再 OOM 一次，记的估计是 24
        qa = self.queue("a", ["hog|sleep 8   # vram=15"])
        self.start()
        self.until(lambda: self.status().get("running"))
        qb = self.queue("b", ["x_s0|echo 'torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB'; exit 1   # vram=12 tag=t"])
        # 收尾先写 .failed、后写用量行：等用量行（#185），不以 .failed 为准
        def x_rows():
            return [r for r in self.usage() if r["name"] == "x_s0"]
        self.until(x_rows)
        row = x_rows()[-1]
        self.assertEqual((row["oom"], row["est"]["vram"], row["est"]["src"]), ("vram", 12.0, "提示值"))
        self.assertIn("CUDA 显存不够（OOM），同类下次估计抬高", (qb / "state" / "queue.log").read_text())
        (qb / "state" / "x_s0.failed").unlink()
        self.until(lambda: any(w["name"] == "x_s0" for w in self.status().get("waiting") or []))
        w = next(w for w in self.status()["waiting"] if w["name"] == "x_s0")
        self.assertIn("没有可用显存够 24 GiB 的卡（同 tag 上次 OOM：放行时估 12.0 GiB ×2", w["why"])
        self.assertTrue(any(j["name"] == "hog" for j in self.status()["running"]))
        self.until(lambda: len(x_rows()) == 2 and (qa / "state" / "hog.done").exists(), timeout=30)
        self.assertEqual([r["est"]["vram"] for r in x_rows()], [12.0, 24.0])

    def fit_case(self, gpus, peak_mb):
        """卡的状态为 gpus（LABMON_GPUS），同类任务历史显存峰值 peak_mb；一个任务占着卡在跑，问 x_s1 要不要开车道并行。"""
        h = self.d / "home" / "hosts" / "t"
        h.mkdir(parents=True)
        (h / "usage.jsonl").write_text(json.dumps({"name": "x_s0", "project": "a", "rc": "0", "min": 30, "gpus": ["GPU-aaa"],
                                                   "ram_peak": 1.0, "vram_peak": peak_mb, "gpu_util": 60, "with_gpu": []}) + "\n")
        self.env["LABMON_GPUS"] = gpus
        self.queue("a", ["r_s0|sleep 30   # vram=1"])
        self.start()
        self.until(lambda: self.status().get("running") and len((self.status().get("util10") or {})) == 1)
        r = self.w("fit", "x_s1")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def test_fit_vram_short(self):
        # 要 32.2+1 GiB，卡只空 29.3 GiB：不开车道
        self.assertIn("显存不够", self.fit_case("GPU-aaa:30000:48000:30", 30000))

    def test_fit_card_full_not_blocked(self):
        # 显存够、卡的利用率已到 95%：利用率是“有核函数在跑”的时间占比，到 100% 不等于算满，不据此劝阻（#67）；写同类在这张卡上并行 2 条时约要多久
        line = self.fit_case("GPU-aaa:40000:48000:95", 30000)
        self.assertIn("可以开车道并行", line)
        self.assertIn("同卡 2 条时约要 30 分钟（同类近 1 次实际用时）", line)
        self.assertIn("单跑时利用率约 60%", line)                             # 同类单跑时的利用率照写，只是不据此劝阻
        with open(self.d / "home" / "hosts" / "t" / "usage.jsonl", "a") as f:   # 别的型号上跑的同类不算（#86：与 worker 同口径）
            f.write(json.dumps({"name": "x_s2", "project": "a", "rc": "0", "min": 90, "gpus": ["GPU-aaa"], "gpu": "A100",
                                "ram_peak": 1.0, "vram_peak": 30000, "gpu_util": 60, "with_gpu": []}) + "\n")
        self.assertIn("同卡 2 条时约要 30 分钟（同类近 1 次实际用时）", self.w("fit", "x_s1").stdout)

    def test_tag_limit(self):
        self.assertIn("heavy=1", self.w("limit", "heavy=1").stdout)
        a = self.queue("a", [f"a1|{self.gate()}   # gpus=0 tag=heavy"])
        b = self.queue("b", ["b1|true   # gpus=0 tag=heavy"])
        self.start()
        self.until(lambda: "tag heavy" in json.dumps(self.status().get("waiting", []), ensure_ascii=False))
        self.release()
        self.until(lambda: (b / "state" / "b1.done").exists())
        self.assertTrue((a / "state" / "a1.done").exists())

    def test_slot_grant_and_release(self):
        self.start()
        out = self.d / "out"
        r = subprocess.run([sys.executable, str(WORKER), "slot", "--host", "t", "--project", "demo-b", "--name", "u1", "--vram", "1",
                            "--", "bash", "-c", f"echo $CUDA_VISIBLE_DEVICES > {out}; exit 7"], env=self.env, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 7)                       # exec 原命令：退出码原样返回
        self.assertEqual(out.read_text().strip(), "GPU-aaa")
        usage = self.d / "home" / "hosts" / "t" / "usage.jsonl"
        self.until(lambda: usage.exists() and '"kind": "slot"' in usage.read_text())
        self.assertEqual(list((self.d / "slots").iterdir()), [])  # 交换文件清掉

    def test_slot_waits_without_worker(self):
        r = subprocess.run([sys.executable, str(WORKER), "slot", "--host", "t", "--project", "demo-b", "--name", "u1", "--timeout", "2",
                            "--", "true"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 75)
        self.assertIn("worker 不在", r.stderr)

    def test_pause_now_requeues_and_resume_reruns(self):
        a = self.queue("a", ["long|sleep 30"], "", "--stop", "term", "--project", "p1")
        self.start()
        self.until(lambda: (a / "state" / "current").exists())
        self.assertIn("暂停 p1", self.w("override", "add", "pause", "p1", "--now").stdout)
        self.until(lambda: "暂停中" in json.dumps(self.status().get("waiting", []), ensure_ascii=False))
        self.assertFalse((a / "state" / "long.failed").exists())   # 停下的重新排队，不记失败
        self.assertIn("停下 long（暂停", (a / "state" / "queue.log").read_text())
        self.w("override", "rm", "pause", "p1")
        self.until(lambda: (a / "state" / "current").exists())
        ev = (self.d / "home" / "hosts" / "t" / "events.jsonl").read_text()
        for k in ("临时指令：暂停 p1", "暂停：按 term 停下 long", "撤销临时指令"):
            self.assertIn(k, ev)
        ov = [json.loads(l) for l in ev.splitlines() if '"override"' in l]   # 结构化字段：时间线按它画暂停区间
        self.assertEqual([(e["act"], e["okind"], e["target"]) for e in ov], [("add", "pause", "p1"), ("rm", "pause", "p1")])


    def test_override_once_and_global_listing(self):
        # #96：命令行 override add … --once 只写这台的“只停一次”、不记指令事件（暂停由看板记全局的那条）；
        # 列出生效中的指令时带上看板写来的项目级暂停、优先，不列 once
        sd = self.d / "home" / "hosts" / "t"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "sched.json").write_text(json.dumps({"pause": {"p2": None}}))
        self.w("override", "add", "pause", "p1")                                   # 这台单独下的暂停：全局的 pause --now 不替掉它（Codex 审查）
        self.assertIn("下一轮按停法停下 p1", self.w("override", "add", "pause", "p1", "--now", "--once").stdout)
        self.assertEqual([(o["target"], o.get("once")) for o in json.loads((sd / "overrides.json").read_text())], [("p1", None), ("p1", True)])
        self.assertEqual([json.loads(l)["act"] for l in (sd / "events.jsonl").read_text().splitlines()], ["add"])    # once 不记指令事件
        self.assertEqual(self.w("override", "ls").stdout.strip(), "生效中：暂停 p1，无期限；暂停 p2，无期限，所有主机")
        self.w("override", "rm", "pause", "p1")                                    # 撤这台的：once 一起撤
        self.assertEqual(json.loads((sd / "overrides.json").read_text()), [])

    def test_restop_applies_to_running(self):
        # 登记时是 boundary（等跑完）：重新登记 --stop term 后 pause --now，正在跑的也按新停法停下（以前沿用放行时的，停不下）
        a = self.queue("a", ["long|sleep 30"], "", "--project", "p1")
        self.start()
        self.until(lambda: (a / "state" / "current").exists())
        self.w("override", "add", "pause", "p1", "--now")
        ev = self.d / "home" / "hosts" / "t" / "events.jsonl"
        self.until(lambda: "按 boundary 等它跑完" in ev.read_text())
        self.assertEqual(self.w("add", str(a), "jobs/q.txt", "--stop", "term", "--project", "p1").returncode, 0)
        self.w("override", "add", "pause", "p1", "--now")
        self.until(lambda: "停下 long（暂停" in (a / "state" / "queue.log").read_text())
        self.assertFalse((a / "state" / "long.failed").exists())


class EmptyQueueTest(WorkerBase):
    def test_empty_queue_marked(self):
        # 清单跑空了的队列（项目暂时没实验）照样登记着，状态里标“空着”，别的项目不当它在抢卡
        root = self.queue("q", ["a|true"])
        self.start()
        self.until(lambda: (root / "state" / "a.done").exists())
        self.until(lambda: (self.status().get("queues") or [{}])[0].get("empty") is True)
        self.assertIn("空着", self.w("ls").stdout)
        with open(root / "jobs" / "q.txt", "a") as f:                     # 往清单里加任务就接着跑
            f.write("b|true\n")
        self.until(lambda: (root / "state" / "b.done").exists())


class UnitBase(unittest.TestCase):
    """不起进程、直接调 worker 函数的公共部分：临时目录下的多主机夹具（hosts，共享 HOME），worker 模块与本机 t 的 Worker（状态目录已建）。
    别的主机的状态、用量记录与清单都经 hosts 写（fixtures.Hosts）。"""

    def extra(self) -> dict:
        """worker 模块另加的环境变量（假 cgroup 等），子类按需覆盖。"""
        return {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.hosts = Hosts(self.d, **self.extra())
        self.m = self.hosts.m
        self.w = self.hosts.worker("t")

    def tearDown(self):
        self.hosts.close()                        # tick 真放行的任务还在写状态目录时删不掉（#185）
        self.tmp.cleanup()

    def req(self, **kw):
        """一次单元申请（缺省不用卡），与 tick 一样经 new_req 拼、prep 补全（est、held、stall、since、dur、charge）；kw 覆盖字段。"""
        r = self.m.new_req("slot", kw.pop("key", "r"), kw.pop("project", "p"), kw.pop("name", "u"), id="1", pid=1, ngpu=kw.pop("ngpu", 0), t=time.time(), **kw)
        return self.w.prep(r, time.time(), {})

    def place(self, r, gpus=None, apps=()):
        """真放行一个请求：按此刻的快照（gpus 为 query_gpus 形状的各卡空闲，不给用 info 里的；apps 是 GPU 进程）。"""
        return self.w.place(r, self.w.snapshot({}, gpus, apps))

    def cards(self, *free, total=40000.0):
        """主机现状里的几张卡 GPU-a、GPU-b…，各空 free MB；返回 query_gpus 形状的 [(序号, UUID, 空闲)]。"""
        gs = [{"i": i, "uuid": "GPU-" + "abcd"[i], "name": "A", "total": total, "used": total - f, "free": f, "util": 0.0} for i, f in enumerate(free)]
        self.w.info = dict(self.w.info, gpus=gs)
        return [(g["i"], g["uuid"], g["free"]) for g in gs]

    def events(self, kind=None):
        f = Path(self.w.p.events)
        ev = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        return [e for e in ev if kind is None or e["kind"] == kind]


class AdmissionUnitTest(UnitBase):
    """不起进程，直接核对内存记账、闸门、到期与“等了 30 分钟不再被越过”。"""

    def block(self, r):
        """挡住这个请求的临时指令的原因（override_hit 的第二项），没有为 None。"""
        return (self.w.override_hit(r) or (None, None))[1]

    def test_crash_logged(self):
        # 输出接的是 /dev/null：崩之前把出错位置记进 worker.log，主机页看得到
        def boom(*_):
            raise RuntimeError("坏了")
        self.w.tick, self.w.info = boom, {"mem": {}}
        with self.assertRaises(RuntimeError):
            self.w.run()
        log = Path(self.w.p.log).read_text()
        self.assertIn("worker 出错退出", log)
        self.assertIn("RuntimeError: 坏了", log)

    def test_memory_room_counts_estimate(self):
        self.w.info = {"mem": {"limit": 100.0, "used": 30.0, "anon": 30.0}}
        self.w.running["j"] = {"key": "j", "name": "j", "t0": time.time(), "gpus": [], "est": {"ram": 50.0, "vram": 0.0}, "use": {"ram": 10.0}}
        self.assertAlmostEqual(self.w.mem_room(), 80.0)          # labmon 全停时：100 − 其他 20
        self.assertAlmostEqual(self.m.ram_free(self.w.snapshot({})), 30.0)   # 再扣记账 50
        why, res = self.place(self.req(ram=25.0))
        self.assertTrue(res)
        self.assertIn("内存不够", why)
        self.w.gate = True
        self.w.info["mem"]["anon"] = 95.0
        self.assertIn("内存闸门", self.place(self.req())[0])

    def test_page_cache_not_others_memory(self):
        # 读数据留下的页缓存很多（工作集 100 / 122，收不回的只有 10）：不算 labmon 之外的内存，90 GiB 的任务照放；内存闸门也不关
        os.makedirs(self.m.SLOTS, exist_ok=True)
        self.w.info = {"mem": {"limit": 122.0, "used": 100.0, "anon": 10.0}}
        self.assertAlmostEqual(self.w.mem_room(), 112.0)
        self.assertIsNone(self.place(self.req(ram=90.0)))
        self.w.mem_events(self.w.info["mem"])
        self.assertFalse(self.w.gate)
        self.w.mem_events({"limit": 122.0, "used": 121.0, "anon": 115.0})
        self.assertTrue(self.w.gate)
        self.assertIn("收不回的内存 115.0 / 122.0 GiB", self.events("gate")[-1]["msg"])

    def test_cpu_wait_alert_after_10_min(self):
        # 请求一直因记账被挡、实际空着的却够（cpu_est）：满 10 分钟（CPU_ALERT_S）才在等待项写 cpu_wait（秒）；腾出 CPU 放下了、换了原因就清掉、重新计时。
        # 核真被我们自己的任务用着（cpu）是正常排队：不计时、不告警（#65）
        os.makedirs(self.m.SLOTS, exist_ok=True)
        self.w.info = {"mem": {"limit": 100.0, "used": 5.0, "anon": 5.0}, "cpu_quota": 4.5}
        self.w.running["j"] = {"key": "j", "name": "j", "t0": time.time(), "gpus": [], "est": {"ram": 1.0, "vram": 0.0, "cpus": 4}, "use": {"cores": 4.0}}

        def tick(r):
            out = self.place(r)
            w = [dict(r, why=out[0])] if out else []
            self.w.note_waits(w, [r], self.hosts.now())
            return w[0]["cpu_wait"] if w else "放了"
        r = self.req(key="k", name="u", cpus=1)
        self.assertEqual(self.place(r), ("CPU 不够：要 1 核（提示值），按记账只剩 0.0 核（配额 4：在跑 1 个记 4 核）", True))
        self.assertEqual(r["code"], "cpu")
        self.assertIsNone(tick(r))
        self.hosts.advance(self.m.CPU_ALERT_S)
        self.assertIsNone(tick(r))                                                   # 核真被用着：满 10 分钟也不告警
        self.assertEqual(self.w.cpu_block, {})
        self.w.running["j"]["use"] = {"cores": 0.5}                                 # 记 4 核、实测 0.5：实际空着的够，是记账挡住的
        self.assertEqual(self.place(r), ("CPU 不够：要 1 核（提示值），按记账只剩 0.0 核（配额 4：在跑 1 个记 4 核，其中 1 个记账比实测多（实测共 0.5 核））；"
                                         "实际空着的够，是在跑的记账比实测多，开跑满 10 分钟后按实测回收", True))
        self.assertEqual(r["code"], "cpu_est")
        self.assertIsNone(tick(r))
        self.hosts.advance(self.m.CPU_ALERT_S - 1)
        self.assertIsNone(tick(r))
        self.hosts.advance(1)
        self.assertEqual(tick(r), self.m.CPU_ALERT_S)
        big = self.req(key="k", name="u", cpus=9)                                   # 要的比配额还多：放不下（nofit_cpu）也算因 CPU 被挡，接着计时
        self.assertEqual(self.place(big)[1], False)
        self.assertEqual((big["code"], tick(big)), ("nofit_cpu", self.m.CPU_ALERT_S))
        self.w.running["j"]["est"]["cpus"] = 1                                      # 腾出 CPU：这一个放下了，计时清掉
        self.w.running["j"]["use"] = {}
        self.assertEqual(tick(self.req(key="k", name="u", cpus=1)), "放了")
        self.assertEqual(self.w.cpu_block, {})

    def test_cpu_acct_reclaimed_after_warmup(self):
        # #65（27-15151，11-09 13:10–13:56）：同 tag 估计 2 核（开头加载阶段的 1 分钟峰值）、整段只用 1 核的任务一直按 2 核记，
        # 配额 7、给对方留 1、在跑 4 个（1+1+2+2）只剩 0，要 2 核的等了半小时。开跑满 CPU_WARM_S 后按近 CPU_WARM_S 的窗口核数记，腾出 2 核放得下
        os.makedirs(self.m.SLOTS, exist_ok=True)
        now = self.hosts.now()
        self.w.info = {"mem": {"limit": 100.0, "used": 5.0, "anon": 5.0}, "cpu_quota": 6.0}
        warm = self.m.CPU_WARM_S

        def job(k, cpus, pk0):
            use = {"cores": 1.0, "t": now, "pk0": pk0, "pks": [[now - 120, 1.4], [now - 60, 1.0], [now, 1.0]]}
            self.w.running[k] = {"key": k, "name": k, "t0": pk0, "gpus": [], "est": {"ram": 1.0, "vram": 0.0, "cpus": cpus}, "use": use}
        for k in ("a", "b"):
            job(k, 1, now - 3 * warm)
        for k in ("c", "d"):
            job(k, 2, now - warm + 60)                                         # 才跑了 9 分钟：还按估计 2 核记
        self.assertEqual([self.w.acct(self.w.running[k])["cpu"] for k in "abcd"], [1.0, 1.0, 2.0, 2.0])
        r = self.req(key="k", name="u", cpus=2)
        self.assertEqual(self.place(r), ("CPU 不够：要 2 核（提示值），按记账只剩 0.0 核（配额 6：在跑 4 个记 6 核，其中 2 个记账比实测多（实测共 2.0 核））；"
                                         "实际空着的够，是在跑的记账比实测多，开跑满 10 分钟后按实测回收", True))
        self.assertEqual(r["code"], "cpu_est")
        for k in ("c", "d"):
            self.w.running[k]["use"]["pk0"] = now - warm                       # 满 10 分钟：按近 10 分钟窗口核数的最大值 1.4 记
        self.assertEqual([self.w.acct(self.w.running[k])["cpu"] for k in "abcd"], [1.0, 1.0, 1.4, 1.4])
        self.assertIsNone(self.place(self.req(key="k2", name="u2", cpus=1)))  # 6 − 4.8 = 1.2 核：要 1 核的放得下
        c = self.w.running["c"]
        c["use"]["cores"] = 0.3                                                # #86：已回收、此刻在两次冒尖之间：记账就是近期实测的峰值
        r = self.req(key="k", name="u", cpus=1)                                # 刚放的 u2 记 1 核：只剩 0.2
        self.assertEqual(self.place(r), ("CPU 不够：要 1 核（提示值），按记账只剩 0.2 核（配额 6：在跑 5 个记 5.8 核）", True))
        self.assertEqual(r["code"], "cpu")                                     # 核真会被用到：正常排队，不算“记账比实测多”、不告警
        c["use"]["cores"] = 3.0                                                # 后段又冒尖：跟着实测涨回去
        self.assertEqual(self.w.acct(c)["cpu"], 3.0)
        c["use"].update(cores=0.2, pks=[[now, 0.2]])                           # 几乎不用：不低于 1 核
        self.assertEqual(self.w.acct(c)["cpu"], 1.0)
        c["use"]["pks"] = [[now, 5.0]]                                         # 窗口核数比给的还多：不超过开跑时给的核数（此刻实测另算）
        self.assertEqual(self.w.acct(c)["cpu"], 2.0)
        c["est"]["cpus"] = 0                                                   # cpus=0：不记账，回收后仍是 0
        self.assertEqual(self.w.acct(c)["cpu"], 0.2)
        c["est"]["cpus"], c["use"] = 2, {"cores": 1.0, "t": now}               # 接管旧 worker 的、还没量过窗口核数：按估计记
        self.assertEqual(self.w.acct(c)["cpu"], 2.0)

    def test_cpu_reason_flips_logged_once(self):
        # labmon 之外的占用在抖：原因在“按记账只剩”与“labmon 之外…占”之间来回换，都算因 CPU 被挡的一类，只记一条等待事件，计时连着算
        os.makedirs(self.m.SLOTS, exist_ok=True)
        self.w.info = {"mem": {"limit": 100.0, "used": 5.0, "anon": 5.0}, "cpu_quota": 8.0}
        self.w.running["j"] = {"key": "j", "name": "j", "t0": time.time(), "gpus": [], "est": {"ram": 1.0, "vram": 0.0, "cpus": 4}, "use": {"cores": 0.5}}
        self.w.cpu_n = 2
        codes = []
        for other in (0.0, 4.0, 0.0, 4.0):
            self.w.other["cpu"] = other
            r = self.req(key="k", name="u", cpus=5)
            why, _ = self.place(r)
            codes.append(r["code"])
            self.w.note_waits([dict(r, why=why)], [r], self.hosts.now())
            self.hosts.advance(60)
        self.assertEqual(codes, ["cpu_est", "cpu_other", "cpu_est", "cpu_other"])
        self.assertEqual(len(self.events("wait")), 1)
        self.assertAlmostEqual(self.w.cpu_block["k"][1], self.hosts.now() - 240, delta=1)

    def test_fit_cpu_short(self):
        # labmon worker fit 也按 CPU 记账判断，与 place 同一口径：同类峰值 2 核 ×1.1 → 2 核，在跑的记 2 核、只剩 1.5 核：不开车道；
        # 同类峰值 9 核 ×1.1 → 10 核，超过全停时能给的 3.5 核按 3 核算（原因里写明估计与封顶），也不开；只剩的够封顶后的核数就开
        import contextlib
        import io
        now = self.hosts.now()
        self.hosts.usage("t", [usage_row("x_s0", "a", now - 3600, now - 1800, ram_peak=1.0, vram_peak=1000.0, cores_peak=2.0),
                               usage_row("y_s0", "a", now - 3600, now - 1800, ram_peak=1.0, vram_peak=1000.0, cores_peak=9.0)])
        room = {"ram": 50.0, "margin": 8.0, "gpus": {"GPU-a": 40000.0}, "cpu": 1.5, "cpu_cap": 4}
        info = {"gpus": [{"i": 0, "uuid": "GPU-a", "free": 40000.0}], "mem": {}, "disk": [], "started": now - 3600}
        self.hosts.status("t", info=info, room=room, running=[running_job("r", "b", acct={"ram": 1.0, "vram": 0, "cpu": 2.0})])

        def fit(*names):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(self.m.fit(self.w.p, list(names), "a"), 0)
            return out.getvalue()
        self.assertIn("x_s1：CPU 不够：要 2 核（同名 近 1 次峰值 ×1.1），按记账只剩 1.5 核（配额 4：在跑 1 个记 2 核；labmon 之外占 0.5） → 排在已有车道后面", fit("x_s1"))
        self.assertIn("y_s1：CPU 不够：要 3 核（同名 近 1 次峰值 ×1.1；估计 10 核，封顶为 3 核（labmon 全停时能给的）），按记账只剩 1.5 核（配额 4：在跑 1 个记 2 核；", fit("y_s1"))
        # #86：核被谁记着与真放行同一个 cpu_held；在跑的还没回收、此刻只用 0.5 核，加回去就够：句尾同真放行写会回收
        self.hosts.status("t", info=info, room=room, running=[running_job("r", "b", acct={"ram": 1.0, "vram": 0, "cpu": 2.0}, use={"cores": 0.5, "t": now})])
        self.assertIn("其中 1 个记账比实测多（实测共 0.5 核）；labmon 之外占 0.5）；实际空着的够，是在跑的记账比实测多，开跑满 10 分钟后按实测回收 → 排在已有车道后面", fit("x_s1"))
        self.hosts.status("t", info=info, room=dict(room, cpu=3.5), running=[])
        self.assertIn("y_s1：可以开车道并行", fit("y_s1"))

    def fit_out(self, names, project=None, host="t"):
        """在主机 host 上跑 labmon worker fit（状态由 fit_status 写好），返回各任务那一行 {任务: 句子}。"""
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.fit(self.m.Paths(host), list(names), project), 0)
        return {ln.split("：", 1)[0]: ln.split("：", 1)[1] for ln in out.getvalue().splitlines() if not ln.startswith("卡 ")}

    def fit_status(self, host="t", cpu=6.0):
        """一张 44 GiB 空闲的卡、内存与 CPU 都够（cpu 为还能给的核数），卡上有一个任务在跑。"""
        now = self.hosts.now()
        room = {"ram": 50.0, "margin": 8.0, "gpus": {"GPU-a": 45000.0}, "cpu": cpu, "cpu_cap": 8}
        info = {"gpus": [{"i": 0, "uuid": "GPU-a", "free": 45000.0}], "mem": {}, "disk": [], "started": now - 3600}
        self.hosts.status(host, info=info, room=room, running=[running_job("sm1_psm5_pattern", "bp", gpus=["GPU-a"])])

    def test_fit_reads_list_row(self):
        # #43：fit 按清单行估，与真放行同一口径。名字各不相同、写了同一个 tag 的任务按“同 tag”找到历史（历史那行在别的目录、别的清单，
        # 只能靠 tag 认出）；没写 tag、和跑过的在同一条清单的按“同队列”；清单的 cpus= 照算；不写 --project 也从登记取项目
        now = self.hosts.now()
        self.hosts.usage("t", [usage_row("sm1_psm5_honest", "bp", now - 3600, now - 3300, root="/elsewhere", queue="jobs/old.txt", tag="sm_train",
                                         ram_peak=3.0, vram_peak=1500.0, gpu_util=9, cores_peak=1.0),
                               usage_row("sm1_prep", "bp", now - 3300, now - 3000, root=str(self.d / "s1"), queue="jobs/q.txt",
                                         ram_peak=2.0, vram_peak=800.0, cores_peak=1.0)])
        self.hosts.queues({"s1": ["sm1_prep|true", "sm1_psm5_noise|true   # tag=sm_train", "sm1_class_honest|true   # tag=sm_train cpus=7",
                                  "sm1_plot|true"]}, project="bp")
        self.hosts.register("t", "s1", "bp")
        self.fit_status()
        out = self.fit_out(["sm1_psm5_noise", "sm1_plot", "sm1_class_honest"])
        self.assertIn("可以开车道并行", out["sm1_psm5_noise"])
        self.assertIn("同 tag 近 1 次峰值 ×1.1", out["sm1_psm5_noise"])
        self.assertIn("单跑时利用率约 9%", out["sm1_psm5_noise"])
        self.assertIn("清单：t 上 s1/jobs/q.txt", out["sm1_psm5_noise"])
        self.assertIn("同队列 近 1 次峰值 ×1.1", out["sm1_plot"])
        self.assertIn("CPU 不够：要 7 核（提示值）", out["sm1_class_honest"])

    def test_fit_hint_with_only_queue_history(self):
        # #61：同一条清单里只有别的 tag 的历史，这一行写了提示值：按提示值估、没有实测，不开新车道（以前按同队列判“可以开车道”，现场并行 OOM）；
        # 不用卡的看内存的来源；写了 tag、没写提示值的照旧按同队列
        now = self.hosts.now()
        self.hosts.usage("t", [usage_row("sm9_r18", "bp", now - 3600, now - 3300, root=str(self.d / "s4"), tag="sm_train", gpus=["GPU-a"],
                                         ram_peak=1.0, vram_peak=6000.0, cores_peak=1.0)])
        self.hosts.queues({"s4": ["vit_a|true   # vram=24 ram=12 tag=sm_vit", "agg_a|true   # gpus=0 ram=4 tag=sm_agg", "vit_b|true   # tag=sm_vit2"]}, project="bp")
        self.hosts.register("t", "s4", "bp")
        self.fit_status()
        out = self.fit_out(["vit_a", "agg_a", "vit_b"])
        self.assertIn("没有同 tag、同名任务的实测，清单写了 vram=24，还没有实测（同一条清单里别的任务的历史不用） → 不开新车道", out["vit_a"])
        self.assertIn("清单写了 ram=4，还没有实测", out["agg_a"])
        self.assertIn("不开新车道", out["agg_a"])
        self.assertIn("可以开车道并行", out["vit_b"])
        self.assertIn("同队列 近 1 次峰值 ×1.1", out["vit_b"])

    def test_fit_after_oom(self):
        # #62：写了 vram= 的行 OOM 过：来源是 OOM 下限，按历史处理（不再是“只有提示值、不开新车道”），句中写出来源；
        # 不用卡的内存 OOM 后翻倍超过全停时能给的减余量：按整份算（ram_need），没有别的记账时照开
        now = self.hosts.now()
        self.hosts.usage("t", [usage_row("vit_a", "bp", now - 600, now - 540, tag="sm_vit", rc="1", oom="vram", gpus=["GPU-a"], est={"vram": 8.0, "ram": 1.0}),
                               usage_row("agg_a", "bp", now - 600, now - 540, tag="sm_agg", rc="137", oom="ram", est={"vram": 0.0, "ram": 30.0}, ram_peak=29.0)])
        self.hosts.queues({"s5": ["vit_a|true   # vram=8 tag=sm_vit", "agg_a|true   # gpus=0 ram=4 tag=sm_agg"]}, project="bp")
        self.hosts.register("t", "s5", "bp")
        self.fit_status()
        out = self.fit_out(["vit_a", "agg_a"])
        self.assertIn("可以开车道并行：卡 0 可用 43.9 GiB，要 17.0 GiB（同 tag 上次 OOM：放行时估 8.0 GiB ×2，含另留的 1 GiB）", out["vit_a"])
        self.assertIn("可以开车道并行：不用 GPU，内存要 42 GiB（同 tag 上次 OOM：放行时估 30.0 GiB ×2；估计 60.0 GiB，封顶为 42.0 GiB（labmon 全停时能给的减去余量））",
                      out["agg_a"])

    def test_fit_list_row_on_other_host(self):
        # 清单登记在别的主机上（排程前逐台查放不放得下）也找得到，写明用的是哪台的；同名的行本机有就用本机的（本机优先），
        # 写了 --project 的只认这个项目的清单
        now = self.hosts.now()
        self.hosts.usage("u", [usage_row("a_s0", "bp", now - 3600, now - 3300, tag="small", ram_peak=1.0, vram_peak=1000.0)])
        self.hosts.queues({"qu": ["b_new|true   # tag=small"]}, project="bp")
        self.hosts.queues({"qt": ["b_new|true   # tag=other"]}, project="zz")
        self.hosts.register("u", "qu", "bp")
        self.fit_status()
        self.assertIn("清单：u 上 qu/jobs/q.txt", self.fit_out(["b_new"])["b_new"])
        self.assertIn("同 tag", self.fit_out(["b_new"])["b_new"])
        self.hosts.register("t", "qt", "zz")
        line = self.fit_out(["b_new"])["b_new"]
        self.assertIn("清单：t 上 qt/jobs/q.txt", line)
        self.assertIn("没有同类任务的历史", line)
        self.assertNotIn("写同一个 tag=", line)          # 已经写了 tag 的不再劝它写 tag
        self.assertIn("清单：u 上 qu/jobs/q.txt", self.fit_out(["b_new"], "bp")["b_new"])

    def test_fit_without_list_row(self):
        # 清单里找不到的照旧按名字估，提示先追加到已登记的清单再问（不说按 tag 找过）；只有清单提示值、没有同类历史的不开车道，
        # 句子里写明（用卡的说 vram=，不用卡的说 ram=）；没写 tag 的劝它给同类写同一个 tag=；不用卡、有历史的不看显存
        now = self.hosts.now()
        self.hosts.usage("t", [usage_row("d_s0", "bp", now - 3600, now - 3300, ram_peak=2.0, vram_peak=0.0, cores_peak=1.0)])
        self.hosts.queues({"q": ["v_new|true   # vram=6", "c_new|true   # gpus=0 ram=2", "d_s1|true   # gpus=0"]}, project="bp")
        self.hosts.register("t", "q", "bp")
        self.fit_status()
        out = self.fit_out(["zz_new", "v_new", "c_new", "d_s1"], "bp")
        self.assertIn("没有同类任务的历史", out["zz_new"])
        self.assertIn("清单里没找到这一行，按名字估；先把它追加到已登记的清单（比如占着卡的那条末尾）再问", out["zz_new"])
        self.assertNotIn("tag", out["zz_new"])
        self.assertIn("清单写了 vram=6，还没有实测", out["v_new"])
        self.assertIn("不开新车道", out["v_new"])
        self.assertIn("写同一个 tag=", out["v_new"])
        self.assertIn("清单写了 ram=2，还没有实测", out["c_new"])
        self.assertIn("不开新车道", out["c_new"])
        self.assertIn("可以开车道并行：不用 GPU", out["d_s1"])

    def test_fit_same_name_in_two_lists(self):
        # 同一台的两条清单里有同名的行：按登记文件名的顺序取第一条（qb 先登记、qa 后登记，取 qa），写明用的哪条
        self.hosts.queues({"qb": ["dup|true   # tag=b"], "qa": ["dup|true   # tag=a"]}, project="bp")
        self.hosts.register("t", "qb", "bp")
        self.hosts.register("t", "qa", "bp")
        self.fit_status()
        self.assertIn("清单：t 上 qa/jobs/q.txt", self.fit_out(["dup"])["dup"])

    def test_job_cmd_first_row(self):
        # 按名字找清单里的一行只有一处（job_cmd）：取第一条、跳过注释行，找不到为 None
        q, = self.hosts.queues({"q": ["# x|old", "x|echo 1   # tag=a", "x|echo 2"]})
        self.assertEqual(self.m.job_cmd(q, "x"), "echo 1   # tag=a")
        self.assertIsNone(self.m.job_cmd(q, "y"))

    def test_cpu_given_capped_to_quota(self):
        # 历史是各主机一起看的：在 17 核的主机上量到 17 核、估 19 核，到配额 7.5 核的主机上，运行中的记账与导出的 LABMON_CPUS 都封顶到 7
        # （配额向下取整）；读不到配额不封顶；cpus=0 的不记账、导出 1；实测超过时记账照实测
        self.w.info = {"cpu_quota": 7.5}
        j = {"key": "j", "name": "j", "t0": time.time(), "gpus": [], "est": {"ram": 1.0, "vram": 0.0, "cpus": 19}, "use": {}}
        self.assertEqual((self.w.cpus_for(j), self.w.acct(j)["cpu"]), (7, 7.0))
        self.assertEqual(self.w.cpus_for(dict(j, est=dict(j["est"], cpus=0))), 1)
        self.assertEqual(self.w.acct(dict(j, est=dict(j["est"], cpus=0)))["cpu"], 0.0)
        self.assertEqual(self.w.acct(dict(j, est=dict(j["est"], cpus=2), use={"cores": 3.4}))["cpu"], 3.4)
        self.w.info = {}
        self.assertEqual((self.w.cpus_for(j), self.w.acct(j)["cpu"]), (19, 19.0))
        os.makedirs(self.m.SLOTS)
        self.w.info = {"cpu_quota": 7.5}
        self.w.grant(self.req(key="slot:1", cpus=19.0), [])
        self.assertEqual(json.loads(Path(self.m.SLOTS, "1.ok").read_text())["cpus"], 7)

    def test_old_queue_cpu_counted_outside(self):
        # 旧 gpu_queue.sh 的任务不记账：它此刻用的核从全停时能给的里扣（other_cpu 已把它从 labmon 之外减掉，不扣就两头都没算；同内存）
        self.w.info = {"cpu_quota": 8.0}
        self.assertEqual(self.w.cpu_room(), 8)
        self.w.ext = {"k": {"name": "old", "pid": 1, "t0": time.time(), "gpus": [], "use": {"cores": 2.5}, "ext": True}}
        self.assertAlmostEqual(self.w.cpu_room(), 5.5)

    def test_slot_adopt_keeps_io_suspect(self):
        # worker 重启后接管已放行的单元：带回上一份状态里记过的读写拥塞嫌疑（io_sus），同一个单元不再记一次
        os.makedirs(self.m.SLOTS)
        pid = os.getpid()
        Path(self.m.SLOTS, "9.req").write_text(json.dumps({"pid": pid, "project": "p", "name": "u9", "gpus": 0, "t": time.time()}))
        Path(self.m.SLOTS, "9.ok").write_text(json.dumps({"gpus": [], "t": time.time(), "cpus": 1}))
        self.w.prev = {(None, None, "u9", pid): {"name": "u9", "pid": pid, "io_sus": 1234, "use": {}, "est": {"ram": 0.0, "vram": 0.0, "cpus": 1}}}
        self.assertEqual(self.w.slot_reqs(), [])
        self.assertEqual(self.w.running["slot:9"]["io_sus"], 1234)

    def test_estimate_order_per_field(self):
        # #61：显存、内存逐项取 同 tag › 同名 › 清单提示值 › 同队列 › 0；来源 src（显存）、ram_src（内存）；util 跟着显存的来源走
        q = dict(root="/r", queue="jobs/q.txt")
        hist = [usage_row("old", "p", 0, 600, tag="sm_train", gpus=["GPU-a"], ram_peak=1.0, vram_peak=6000.0, gpu_util=50, **q)]
        est = lambda **kw: {k: self.m.estimate(hist, dict(dict({"project": "p", "name": "vit_a", "tag": "sm_vit", "ram": 0.0, "vram": 0.0}, **q), **kw))[k]  # noqa: E731
                            for k in ("vram", "src", "ram", "ram_src", "util")}
        qs = "同队列 近 1 次峰值 ×1.1"
        self.assertEqual(est(vram=24.0, ram=12.0), {"vram": 24.0, "src": "提示值", "ram": 12.0, "ram_src": "提示值", "util": None})
        self.assertEqual(est(vram=24.0), {"vram": 24.0, "src": "提示值", "ram": 1.1, "ram_src": qs, "util": None})
        self.assertEqual(est(ram=12.0), {"vram": 6.45, "src": qs, "ram": 12.0, "ram_src": "提示值", "util": 50})
        self.assertEqual(est(), {"vram": 6.45, "src": qs, "ram": 1.1, "ram_src": qs, "util": 50})          # 写了 tag、没写提示值：照旧看同队列
        self.assertEqual(est(vram=24.0, tag=None, queue=None), {"vram": 24.0, "src": "提示值", "ram": 0.0, "ram_src": "提示值", "util": None})
        # 同 tag、同名的实测盖过提示值，两项一起取自那一档
        hist.append(usage_row("vit_b", "p", 0, 600, tag="sm_vit", gpus=["GPU-a"], ram_peak=10.0, vram_peak=16000.0, root="/o", queue="jobs/o.txt"))
        self.assertEqual(est(vram=24.0, ram=12.0), {"vram": 17.19, "src": "同 tag 近 1 次峰值 ×1.1", "ram": 11.0, "ram_src": "同 tag 近 1 次峰值 ×1.1", "util": None})
        self.assertEqual(est(vram=24.0, ram=12.0, tag=None, name="old")["src"], "同名 近 1 次峰值 ×1.1")

    def test_estimate_after_oom(self):
        # #62：OOM 失败的记录不进峰值历史；同 tag、同名里最近一次跑成功之后的 OOM 给那一项下限 max(放行时估计 ×2, OOM 时峰值 ×1.1)，几条取大；
        # 连续 OOM 连续翻倍；跑成功一次后回到按实测估；同队列的 OOM 不算；提示值写小了也抬高；内存 OOM 只抬内存
        q = dict(root="/r", queue="jobs/q.txt")
        hist = [usage_row("old", "p", 0, 600, tag="sm_train", gpus=["GPU-a"], ram_peak=1.0, vram_peak=6600.0, gpu_util=50, **q)]
        r = dict({"project": "p", "name": "vit_s1", "tag": "sm_vit", "ram": 0.0, "vram": 0.0}, **q)
        est = lambda **kw: {k: self.m.estimate(hist, dict(r, **kw))[k] for k in ("vram", "src", "ram", "ram_src", "util")}  # noqa: E731
        qs = "同队列 近 1 次峰值 ×1.1"
        self.assertEqual(est(), {"vram": 7.09, "src": qs, "ram": 1.1, "ram_src": qs, "util": 50})
        e0 = self.m.estimate(hist, r)
        oom = lambda name, t, e, **kw: usage_row(name, "p", t, t + 60, **dict(dict(q, tag="sm_vit", rc="1", oom="vram", gpus=["GPU-a"], est=e), **kw))  # noqa: E731
        hist.append(oom("vit_s0", 600, e0, ram_peak=2.0))                    # 几十秒就 OOM、没采到显存峰值：按放行时的估计翻倍
        self.assertEqual(est(), {"vram": 14.18, "src": "同 tag 上次 OOM：放行时估 7.1 GiB ×2", "ram": 1.1, "ram_src": qs, "util": None})
        hist.append(oom("vit_s0", 700, self.m.estimate(hist, r), ram_peak=2.0))   # 再 OOM：再翻一倍
        self.assertEqual(est()["vram"], 28.36)
        hist[1:] = [oom("vit_s0", 600, e0, ram_peak=2.0, vram_peak=15360.0)]   # 峰值 15 GiB ×1.1 比估计 ×2 大
        self.assertEqual(est()["vram"], 16.5)
        self.assertEqual(est()["src"], "同 tag 上次 OOM：OOM 时峰值 15.0 GiB ×1.1")
        self.assertEqual(est(tag=None)["src"], "同名 上次 OOM：OOM 时峰值 15.0 GiB ×1.1")   # 没写 tag：同名一档照样认
        self.assertEqual(est(vram=8.0)["vram"], 16.5)                       # 写了提示值也抬高
        self.assertEqual(est(vram=24.0)["src"], "提示值")                     # 提示值本来就够大：不动
        self.assertEqual(est(tag="other", name="plot")["vram"], 7.09)        # 同队列的 OOM 不算
        # OOM 后跑了 6 分钟被暂停停下（或别的错误退出）：不是跑成功，下限照旧（Codex 审查 #103）
        hist[1:] = [oom("vit_s0", 600, e0, ram_peak=2.0),
                    usage_row("vit_s0", "p", 700, 1060, tag="sm_vit", rc="143", requeued=True, gpus=["GPU-a"], ram_peak=2.0, vram_peak=4096.0, **q)]
        self.assertEqual(est()["vram"], 14.18)
        # 跑满 8 分钟才 OOM：峰值是被截住的，只当下限，不当跑成功的峰值（以前按 12 GiB ×1.1 = 13.2 估）
        hist[1:] = [oom("vit_s0", 600, e0, ram_peak=2.0, vram_peak=12288.0, min=8.0)]
        self.assertEqual(est()["vram"], 14.18)
        hist.append(usage_row("vit_s2", "p", 800, 900, tag="sm_vit", gpus=["GPU-a"], ram_peak=3.0, vram_peak=16300.0, **q))   # 跑成功一次：回到按实测
        self.assertEqual(est(), {"vram": 17.51, "src": "同 tag 近 1 次峰值 ×1.1", "ram": 3.3, "ram_src": "同 tag 近 1 次峰值 ×1.1", "util": None})
        hist.append(oom("vit_s3", 1000, self.m.estimate(hist, r), oom="ram", ram_peak=30.0))   # 内存 OOM：只抬内存
        self.assertEqual(est(), {"vram": 17.51, "src": "同 tag 近 1 次峰值 ×1.1", "ram": 33.0, "ram_src": "同 tag 上次 OOM：OOM 时峰值 30.0 GiB ×1.1", "util": None})

    def test_estimate_cpu_order(self):
        # CPU 估计：清单的 cpus= 优先，其次同类近几次 1 分钟峰值核数 ×1.1（期 1 之前的旧行没有 cores_peak，当没有历史），都没有按 1 核
        r = {"project": "pb", "name": "far", "tag": None, "ram": 2.0, "vram": 0.0}
        hist = [dict(usage_row("far", "pb", 0, 600), ram_peak=1.0, host="o")]
        self.assertEqual({k: self.m.estimate(hist, r)[k] for k in ("cpus", "cpu_src")}, {"cpus": 1, "cpu_src": "缺省 1 核"})
        hist.append(dict(usage_row("far", "pb", 0, 600), ram_peak=1.0, cores_peak=3.4, host="o"))
        self.assertEqual({k: self.m.estimate(hist, r)[k] for k in ("cpus", "cpu_src")}, {"cpus": 4, "cpu_src": "同名 近 1 次峰值 ×1.1"})
        self.assertEqual({k: self.m.estimate(hist, dict(r, cpus=2.0))[k] for k in ("cpus", "cpu_src")}, {"cpus": 2, "cpu_src": "提示值"})
        self.assertEqual(self.m.estimate(hist, dict(r, cpus=0))["cpus"], 0)
        self.assertEqual(self.w.prep(self.m.new_req("slot", "s", "pb", "far", id="1", pid=1, ngpu=0, t=time.time()), time.time(), {})["est"]["cpu_src"],
                         "缺省 1 核")                                               # 本机没有这条历史

    def test_exclusive_host_ignores_idle_supervisor(self):
        # 不用卡、几乎不占 CPU 的常驻任务（等单元的监督进程）：整机独占不等它，也不算打扰；占着 CPU 的照样要等
        os.makedirs(self.m.SLOTS, exist_ok=True)
        self.w.info = {"mem": {"limit": 100.0, "used": 5.0, "anon": 5.0}}
        self.w.running["s"] = {"key": "s", "kind": "slot", "project": "p", "name": "sup", "t0": time.time(), "ngpu": 0, "gpus": [],
                               "est": {"ram": 1.0, "vram": 0.0}, "use": {"ram": 1.0, "cores": 2.0}}
        why, res = self.place(self.req(excl="host"))
        self.assertIn("等主机腾空", why)
        self.w.running["s"]["use"]["cores"] = 0.05
        self.w.excl_hold = {}
        self.assertIsNone(self.place(self.req(excl="host")))
        self.assertTrue(self.m.idle_job(self.w.running["s"]))

    def test_hold_ignores_deadline(self):
        # 截止（续租、约定归还）不挡占位任务：它不占资源，打断了也只是重开后接着等；没估时的普通任务照旧挡
        self.w.ovr = [{"kind": "deadline", "at": time.time() + 600}]
        self.assertIsNone(self.block(self.req(name="hold_x")))
        self.assertIn("没有估时", self.block(self.req(name="u")))
        self.w.ovr = [{"kind": "drain"}]
        self.assertIn("drain", self.block(self.req(name="hold_x")))

    def test_exclusive_hold_dropped_when_blocked(self):
        # 独占任务留着卡、它的项目暂停了（或依赖变成没满足）：留卡放掉，别的项目照常用这张卡
        os.makedirs(self.m.SLOTS, exist_ok=True)
        gpus = self.cards(30000)
        self.w.running["k0"] = {"key": "k0", "kind": "slot", "name": "run0", "project": "pc", "t0": time.time() - 60, "gpus": ["GPU-a"], "use": {},
                                "est": {"vram": 5.0, "ram": 1.0}, "excl": None, "tag": None, "ngpu": 1, "est_min": 2, "deps": [], "deps_any": []}
        ex = self.req(key="ex", project="pa", name="ex", ngpu=1, excl="gpu", est_min=60)
        self.assertEqual(self.place(ex, gpus)[1], True)
        self.assertEqual(list(self.w.excl_hold), ["ex"])
        self.w.ovr = [{"kind": "pause", "target": "pa"}]
        self.w.running.clear()
        ex, ok = self.req(key="ex", project="pa", name="ex", ngpu=1, excl="gpu", est_min=60), self.req(key="ok", project="pb", name="ok", ngpu=1, vram=5.0, est_min=600)
        self.assertEqual(ex["held"][0], "pause")
        self.w.gpu_state([ex, ok])
        self.assertEqual(self.w.excl_hold, {})
        self.assertIsNone(self.place(ok, gpus))

    def test_never_fits_memory(self):
        # 估计值加余量超过容器上限：labmon 的任务全停也放不下，原因写明，不算卡在资源上
        os.makedirs(self.m.SLOTS, exist_ok=True)
        self.w.info = {"mem": {"limit": 122.0, "used": 3.0, "anon": 3.0}}
        why, res = self.place(self.req(ram=113.0))
        self.assertIn("放不下", why)
        self.assertFalse(res)
        self.assertIsNone(self.place(self.req(ram=100.0)))

    def test_slot_duration_from_history(self):
        # 单元拿不到退出码（rc 为空）：续租截止前按单元自己的历史判断，不再一律“没有估时”
        self.w.ovr = [{"kind": "deadline", "at": time.time() + 3600}]
        self.w.hist = [{"name": "u", "project": "p", "rc": None, "kind": "slot", "min": 10.0, "ram_peak": 1.0}]
        self.assertIsNone(self.block(self.req()))
        self.w.hist = []
        self.assertIsNone(self.block(self.req(est_min=20)))

    def test_overrides_expire(self):
        Path(self.w.p.overrides).write_text(json.dumps([{"kind": "drain", "until": time.time() - 1}, {"kind": "focus", "target": "a"}]))
        self.assertEqual([o["kind"] for o in self.w.load_overrides()], ["focus"])
        e = json.loads(Path(self.w.p.events).read_text().splitlines()[0])
        self.assertIn("到期", e["msg"])
        self.assertEqual((e["act"], e["okind"]), ("expire", "drain"))

    def test_global_pause_focus_from_sched(self):
        # #96：看板写来的 sched.json 里项目级的暂停、优先当成同名的临时指令（global），接在这台单独下的后面；过期的跳过、不记事件
        now = time.time()
        Path(self.w.p.overrides).write_text(json.dumps([{"kind": "focus", "target": "a"}]))
        Path(self.w.p.sched).write_text(json.dumps({"weight": {"p": 2}, "pause": {"p": None}, "focus": {"q": now + 600, "old": now - 1}}))
        self.w.ovr = self.w.load_overrides()
        self.assertEqual([(o["kind"], o["target"], bool(o.get("global"))) for o in self.w.ovr], [("focus", "a", False), ("pause", "p", True), ("focus", "q", True)])
        self.assertIn("项目 p 暂停中", self.block(self.req(project="p")))
        self.assertIsNone(self.block(self.req(project="x")))
        self.assertEqual(self.m.ovr_text(self.w.ovr[1]), "暂停 p，无期限，所有主机")
        self.assertEqual(self.events("override"), [])
        self.assertEqual(json.loads(Path(self.w.p.overrides).read_text()), [{"kind": "focus", "target": "a"}])   # 全局的不写进这台的文件
        self.assertEqual(self.w.policy()["w"], {"p": 2.0})

    def test_queue_stop_file_stays_inside_root(self):
        root = self.d / "queue"
        (root / "signals").mkdir(parents=True)
        (root / "escape").symlink_to(self.d, target_is_directory=True)
        outside = self.d / "outside"
        outside.write_text("原内容")
        (root / "signals" / "link").symlink_to(outside)
        (root / "signals" / "stop.tmp").symlink_to(outside)
        for path in ("../outside", str(outside), "escape/outside", "signals/link"):
            j = {"name": "job", "project": "p", "q": {"root": str(root)}, "stop": "file:" + path}
            self.w.stop_job(j)
            self.assertFalse(j.get("stopping"))
            self.assertEqual(outside.read_text(), "原内容")
        j = {"name": "job", "project": "p", "q": {"root": str(root)}, "stop": "file:signals/stop"}
        self.w.stop_job(j)
        self.assertTrue(j["stopping"])
        self.assertIn("停下 p", (root / "signals" / "stop").read_text())
        self.assertEqual(outside.read_text(), "原内容")
        target = root / "signals" / "existing"
        target.write_text("保留")
        linked = root / "signals" / "local"
        linked.symlink_to(target)
        self.w.stop_job(dict(j, stop="file:signals/local"))
        self.assertFalse(linked.is_symlink())
        self.assertIn("停下 p", linked.read_text())
        self.assertEqual(target.read_text(), "保留")
        slot = {"name": "单元", "project": "p", "kind": "slot", "stop": "file:" + str(self.d / "slot-stop")}
        self.w.stop_job(slot)
        self.assertTrue(slot["stopping"])
        self.assertTrue((self.d / "slot-stop").is_file())

    def test_pause_once_stops_then_drops(self):
        # #96：不带 --host 的 pause --now 给各台下“只停一次”（once）：按停法停下这个项目正在跑的；全局暂停随 sched.json 送到之前它照样挡着
        # （Codex P1：停完立刻删，被停的任务下一轮就又放行了），送到了才删、由全局的接着挡；一直没送到的最多挡 ONCE_HOLD_S
        now = time.time()
        Path(self.w.p.overrides).write_text(json.dumps([{"kind": "pause", "target": "p", "now": True, "once": True, "t": now}, {"kind": "drain"}]))
        self.w.running = {"k": {"project": "p", "name": "j"}, "k2": {"project": "x", "name": "j2"}}
        with mock.patch.object(self.w, "stop_job") as stop:
            self.w.ovr = self.w.load_overrides()
            self.assertEqual([c.args[0]["name"] for c in stop.call_args_list], ["j"])
            self.assertEqual([(o["kind"], o.get("once")) for o in self.w.ovr], [("pause", True), ("drain", None)])
            self.w.running = {}
            self.assertIn("项目 p 暂停中", self.block(self.req(project="p")))       # 全局的还没到：照样挡
            Path(self.w.p.sched).write_text(json.dumps({"pause": {"p": None}}))
            ovr = self.w.load_overrides()
            self.assertEqual(stop.call_count, 1)                      # 只停一次
            self.assertEqual([(o["kind"], o.get("global")) for o in ovr], [("drain", None), ("pause", True)])
            self.assertEqual(json.loads(Path(self.w.p.overrides).read_text()), [{"kind": "drain"}])
            Path(self.w.p.sched).write_text("{}")                     # 全局的一直没送到（或先被 resume 了）：最多挡 ONCE_HOLD_S
            Path(self.w.p.overrides).write_text(json.dumps([{"kind": "pause", "target": "p", "now": True, "once": True, "applied": True,
                                                              "t": now - self.m.ONCE_HOLD_S - 1}]))
            self.assertEqual(self.w.load_overrides(), [])

    def test_deadline_admission(self):
        # 续租截止：跑得完的放行，跑不完或没估时的不放；预计时长（duration）再乘 1.2
        self.w.ovr = [{"kind": "deadline", "at": time.time() + 3600}]
        self.assertIsNone(self.block(self.req(est_min=30)))             # 30×1.2 = 36 分钟 < 60
        self.assertIn("跑不完", self.block(self.req(est_min=55)))       # 66 分钟 > 60
        self.assertIn("没有估时", self.block(self.req()))
        self.w.hist = [{"name": "u", "project": "p", "rc": "0", "min": 20.0, "ram_peak": 1.0}]
        self.assertIsNone(self.block(self.req(est_min=100)))            # 同类实际 20 分钟 ×1.2 = 24 分钟
        self.assertIn("续租截止", self.m.ovr_text(self.w.ovr[0]))

    def test_long_wait_blocks_later_requests(self):
        # a 等了 31 分钟、卡上显存被占着放不下；b 本来放得下，也要让位（不管 b 是哪个项目、在跑的多少）
        qs = self.hosts.queues({"a": ["big|true   # vram=30"], "b": ["small|sleep 5   # vram=1"]})
        os.environ["LABMON_GPUS"] = "GPU-aaa:20000:40000"
        try:
            self.w.wsince = {"a": ("big", time.time() - 31 * 60, False)}
            self.w.tick(qs, True)
        finally:
            os.environ.pop("LABMON_GPUS")
        why = {w["name"]: w["why"] for w in json.loads(Path(self.w.p.status).read_text())["waiting"]}
        self.assertIn("没有可用显存", why["big"])
        self.assertIn("让位：a 的 big 已等 31 分钟", why["small"])

    def test_forecast_in_status(self):
        # 推演写进状态：a1 在跑（看板写来的预计结束 30 分钟后）；b1 等 a1 跑完腾出显存，after 写着等谁；a2 再等 b1
        qs = self.hosts.queues({"a": ["a1|sleep 30   # vram=20", "a2|true   # vram=20 est_min=10"], "b": ["b1|true   # vram=30 est_min=60"]})
        os.environ["LABMON_GPUS"] = "GPU-aaa:40000:40000"
        try:
            self.w.wsince = {"b": ("b1", time.time() - 60, False)}
            self.w.tick(qs[:1], True)                       # 先只有 a：a1 放行
            now = time.time()
            Path(self.w.p.eta).write_text(json.dumps({"t": now + 1, "jobs": {"a|a1": now + 1800}}))
            os.environ["LABMON_GPUS"] = "GPU-aaa:19000:40000"
            self.w.tick(qs, False)
        finally:
            os.environ.pop("LABMON_GPUS")
            for j in self.w.running.values():
                os.killpg(j["pid"], 9)
        plan = {ln["queue"] and ln["project"]: {j["name"]: j for j in ln["jobs"]} for ln in json.loads(Path(self.w.p.status).read_text())["plan"]["lanes"]}
        self.assertTrue(plan["a"]["a1"]["run"])
        self.assertAlmostEqual(plan["a"]["a1"]["t1"], now + 1800, delta=2)              # 用了看板写来的预计结束
        self.assertAlmostEqual(plan["b"]["b1"]["t0"], now + 1800, delta=2)      # a1 跑完：先等的 b1 先排
        self.assertEqual((plan["b"]["b1"]["gpus"], plan["b"]["b1"]["after"]), ([0], [["a", "a1"]]))
        self.assertAlmostEqual(plan["a"]["a2"]["t0"], now + 1800 + 3600, delta=2)  # b1 占了 31 GiB，a2 放不下，等 b1 跑完
        self.assertEqual(plan["a"]["a2"]["after"], [["b", "b1"]])
        self.assertAlmostEqual(plan["a"]["a2"]["t1"] - plan["a"]["a2"]["t0"], 600, delta=2)

    def test_fair_share_across_hosts(self):
        # 卡上一次只放得下一个：a 在别的主机上正跑着一个（它状态里的 share.run），先放 b；sched.json 给 a 的权重够大时先放 a
        qs = self.hosts.queues({n: [n + "1|sleep 5   # vram=30 est_min=10"] for n in "ab"})      # 有估时：推演才知道 b1 何时跑完
        now = time.time()
        self.hosts.status("o", share={"run": [["a", 1.0, now - 3600, now + 3600]]})
        os.environ["LABMON_GPUS"] = "GPU-aaa:40000:40000"
        try:
            self.w.tick(qs, True)
            st = json.loads(Path(self.w.p.status).read_text())
            self.assertEqual([j["name"] for j in st["running"]], ["b1"])
            self.assertGreater(st["share"]["use"]["a"], 1.0)                  # 已跑的衰减了，预计还要跑的 1 小时照记
            self.assertEqual((st["share"]["wait_cap_min"], st["share"]["half_life_h"], st["share"]["weight"]), (30.0, 24.0, {}))
            self.assertEqual(st["share"]["run"][0][:2], ["b", 0.768])           # 30 GiB 估计值占 39 GiB 卡的比例
            w = st["waiting"][0]                                                  # 排位：等待表逐条解释用
            self.assertEqual((w["name"], w["pos"]["n"], w["pos"]["focus"]), ("a1", 1, False))
            self.assertGreater(w["pos"]["ratio"], 1.0)
            self.assertIsNotNone(w["start"])                                      # 推演里 b1 跑完就轮到它
            self.assertEqual(st["running"][0]["wait"], 0.0)                        # 一来就放行
            Path(self.w.p.sched).write_text(json.dumps({"weight": {"a": 100}, "wait_cap_min": 10}))
            self.assertEqual(self.w.policy(), {"cap": 600.0, "hl": 86400.0, "w": {"a": 100.0}})
        finally:
            os.environ.pop("LABMON_GPUS")
            for j in self.w.running.values():
                os.killpg(j["pid"], 9)

    def test_never_fits_does_not_block(self):
        # a 要的显存超过卡的总量：labmon 的任务全停也放不下，等多久都不挡后面的请求（以前整台主机所有项目都会停住）
        qs = self.hosts.queues({"a": ["big|true   # vram=100"], "b": ["small|sleep 5   # vram=1"]})
        os.environ["LABMON_GPUS"] = "GPU-aaa:40000"
        try:
            self.w.wsince = {"a": ("big", time.time() - 31 * 60, False)}
            self.w.tick(qs, True)
        finally:
            os.environ.pop("LABMON_GPUS")
        st = json.loads(Path(self.w.p.status).read_text())
        why = {w["name"]: w["why"] for w in st["waiting"]}
        self.assertIn("放不下", why["big"])
        self.assertNotIn("small", why)
        self.assertEqual([j["name"] for j in st["running"]], ["small"])

    def kill_running(self):
        for j in self.w.running.values():
            os.killpg(j["pid"], 9)

    def test_log_tail_current_attempt(self):
        # 日志追加写：只取最后一行分隔之后的（上一次的 CUDA OOM 不算进这一次的判断），分隔行本身不算
        lg = self.d / "x.log"
        lg.write_text("CUDA out of memory\n===== 2026-11-07 13:18:34 labmon 开始（GPU 0）=====\nstep 1\n")
        self.assertEqual(self.m.log_tail(str(lg), 40), ["step 1"])
        lg.write_text("old\n===== 2026-11-07 13:18:34 开始（可能是续跑；labmon worker，GPU 无）=====\n")
        self.assertEqual(self.m.log_tail(str(lg)), [])
        lg.write_text("a\nb\n")
        self.assertEqual(self.m.log_tail(str(lg)), ["a", "b"])                                   # 没有分隔行（不经 worker 起的）照旧

    def test_log_tail_fold(self):
        # 逐字出现过的行（库反复打印的提示）连着的折成一行；数字在变的行不受影响；OOM 判断取原文；从中间读起时丢掉开头半行
        lg = self.d / "x.log"
        hint = "Some weights were not initialized\nYou should probably TRAIN this model\n"
        lg.write_text(f"{hint}PROGRESS 1/9\n{hint}{hint}PROGRESS 2/9\n{hint}")
        self.assertEqual(self.m.log_tail(str(lg)), ["Some weights were not initialized", "You should probably TRAIN this model", "PROGRESS 1/9",
                                                    "…（4 行与前面重复，略）", "PROGRESS 2/9", "…（2 行与前面重复，略）"])
        self.assertEqual(self.m.log_tail(str(lg), 40, fold=False)[-3:], ["PROGRESS 2/9"] + hint.split("\n")[:2])
        lg.write_text("x" * 40000 + "半行\nstep 1\n")
        self.assertEqual(self.m.log_tail(str(lg)), ["step 1"])

    def test_hints_ignore_command_args(self):
        # 命令自己的 --gpus=0、--ram=64 不是资源提示（以前 ngpu=0 让任务看不到卡）；不带 # 的行内提示仍认；needs= 只认 # 之后
        h = self.m.hints("python train.py --gpus=0 --ram=64 --vram=8 --tag=x --needs=a   # est_min=30 tag=heavy")
        self.assertEqual((h["ngpu"], h["ram"], h["vram"], h["tag"], h["est_min"], h["needs"]), (1, 0.0, 0.0, "heavy", 30.0, []))
        h = self.m.hints("python x.py gpus=2 vram=10 exclusive=gpu")
        self.assertEqual((h["ngpu"], h["vram"], h["excl"]), (2, 10.0, "gpu"))
        self.assertEqual(self.m.hints("x --exclusive=host # ram=4")["excl"], None)
        # after= / afterany= 只认 # 之后，命令自己的 after=3、--after=x 不算；可写几次、逗号分隔
        h = self.m.hints("python x.py after=3 --after=x   # est_min=5 after=a,pb/b afterany=c after=d")
        self.assertEqual((h["deps"], h["deps_any"]), (["a", "pb/b", "d"], ["c"]))
        self.assertEqual(self.m.hints("python x.py after=3")["deps"], [])

    def test_prep_since_and_request_shape(self):
        # 请求由 new_req 拼、prep 补全：单元从申请时刻起算；被临时指令挡住的起点推到现在；同一个任务一直在等就沿用上一轮的起点，上一轮被挡着的重新起算
        now = time.time()
        r = self.req(ngpu=1, vram=2.0, est_min=10)
        self.assertEqual((r["kind"], r["est"], r["held"], r["stall"], r["dur"]), ("slot", {"vram": 2.0, "src": "提示值", "ram": 0.0, "ram_src": "提示值", "util": None, "cpus": 1, "cpu_src": "缺省 1 核"}, None, None, 10.0))
        self.assertAlmostEqual(r["since"], r["t"], delta=0.01)
        self.assertAlmostEqual(r["charge"], 10 / 60, delta=0.001)                            # 放行先记的卡·时：看不到卡时按整卡 × 10 分钟
        self.w.wsince = {"r": ("u", now - 600, False)}
        self.assertEqual(self.w.prep(self.m.new_req("slot", "r", "p", "u", id="1", pid=1, t=now), now, {})["since"], now - 600)
        self.w.wsince = {"r": ("u", now - 600, True)}
        self.assertEqual(self.w.prep(self.m.new_req("slot", "r", "p", "u", id="1", pid=1, t=now), now, {})["since"], now)
        self.w.ovr = [{"kind": "pause", "target": "p"}]
        self.w.wsince = {"r": ("u", now - 600, False)}
        r = self.w.prep(self.m.new_req("slot", "r", "p", "u", id="1", pid=1, t=now), now, {})
        self.assertEqual((r["held"][0], r["since"]), ("pause", now))
        q = {"key": "k", "project": "p", "root": "/r", "jobs": "q.txt", "gpus": None}
        r = self.m.new_req("queue", "k", "p", q=q, root="/r", queue="q.txt", **self.m.hints("x   # gpus=2 vram=8 tag=h"), name="j", cmd="x")
        self.assertEqual((r["key"], r["kind"], r["ngpu"], r["vram"], r["tag"], r["q"]["jobs"], r["needs"]), ("k", "queue", 2, 8.0, "h", "q.txt", []))

    def test_hold_never_takes_a_card(self):
        # 占位任务漏写 gpus=0：也不要卡、不独占（否则它“占着”一张卡几小时，保活被停，平台可能按 GPU 空闲关机）
        q, = self.hosts.queues({"h": ["hold_x|sleep 30   # exclusive=gpu"]}, "p")
        os.makedirs(q["state"])
        job = self.m.next_job(q)
        self.assertEqual((job["ngpu"], job["excl"]), (0, None))

    def test_hold_names(self):
        # 占位任务按名字里的词认：开头、结尾或被 _ . - 或数字隔开的 hold / pause
        for n in ("hold", "Hold", "hold_x", "x_hold2", "pause-1", "my.pause", "p1_hold_s1", "pause", "2hold"):
            self.assertTrue(self.m.is_hold(n), n)
        for n in ("threshold_sweep", "household_s1", "unpaused", "holder", "withhold", "x", "", None):
            self.assertFalse(self.m.is_hold(n), n)

    def test_pause_time_not_counted_as_wait(self):
        # pa 被暂停期间不算在等：resume 后不会立刻满 wait_cap 成屏障，pb 照常放行（以前暂停半小时一恢复别的项目全让位）
        qs = self.hosts.queues({"pa": ["big|true   # vram=30"], "pb": ["small|sleep 5   # vram=1"]})
        os.environ["LABMON_GPUS"] = "GPU-aaa:20000:40000"           # 卡上只剩 20 GiB：big 放不下、small 放得下
        try:
            Path(self.w.p.overrides).write_text(json.dumps([{"kind": "pause", "target": "pa", "until": None, "t": time.time()}]))
            self.w.wsince = {"pa": ("big", time.time() - 31 * 60, False)}   # 暂停前已等了一阵
            self.w.tick(qs[:1], True)
            self.assertAlmostEqual(self.w.wsince["pa"][1], time.time(), delta=5)   # 暂停中：起点推到现在
            self.w.wsince["pa"] = ("big", time.time() - 31 * 60, True)          # 暂停了 31 分钟
            Path(self.w.p.overrides).write_text("[]")                           # resume
            self.w.tick(qs, False)
            st = json.loads(Path(self.w.p.status).read_text())
            w = {x["name"]: x for x in st["waiting"]}
            self.assertNotIn("small", w)
            self.assertFalse((w["big"].get("pos") or {}).get("barrier"))
            self.assertAlmostEqual(w["big"]["since"], time.time(), delta=5)
            self.assertEqual(st["running"][0]["wait"], 0.0)
        finally:
            os.environ.pop("LABMON_GPUS")
            self.kill_running()

    def test_stalled_requests_leave_keepalive_on(self):
        # 等也等不来的请求（要的比卡大、缺本地文件补不了、tag 到上限）不为它停保活；要看显存的照旧先停保活
        qs = self.hosts.queues({"a": ["big|true   # vram=100"], "b": ["need|true   # needs=/nonexist/w.pt"], "c": ["t1|sleep 5   # gpus=0 tag=h"],
                                "d": ["t2|true   # tag=h"]})
        Path(self.w.p.limits).write_text(json.dumps({"tags": {"h": 1}}))
        rec = []
        self.w.keepalive = lambda need: rec.append(need)
        os.environ["LABMON_GPUS"] = "GPU-aaa:40000"
        try:
            self.w.tick(qs, True)                                   # 第一轮 t2 还可能放行：停保活
            self.w.tick(qs, False)                                  # 第二轮 t1 在跑（gpus=0），t2 被 tag 上限挡住：等不来
            st = json.loads(Path(self.w.p.status).read_text())
            why = {x["name"]: x["why"] for x in st["waiting"]}
            self.assertIn("放不下", why["big"])
            self.assertIn("缺本地文件", why["need"])
            self.assertIn("tag h", why["t2"])
            self.assertEqual(rec, [True, False])
            qs += self.hosts.queues({"e": ["e1|sleep 5   # vram=1"]})
            self.w.tick(qs, False)
            self.assertEqual(rec[-1], True)
        finally:
            os.environ.pop("LABMON_GPUS")
            self.kill_running()

    def test_no_gpu_is_not_barrier(self):
        # 看不到 GPU（nvidia-smi 无输出）不算卡在资源上：等再久也不成屏障，不用卡的任务照常放
        qs = self.hosts.queues({"a": ["g|true   # vram=1"], "b": ["c|sleep 5   # gpus=0"]})
        os.environ["LABMON_GPUS"] = ""
        try:
            self.w.wsince = {"a": ("g", time.time() - 31 * 60, False)}
            self.w.tick(qs, True)
            st = json.loads(Path(self.w.p.status).read_text())
            self.assertEqual([(x["name"], x["why"]) for x in st["waiting"]], [("g", "看不到 GPU（nvidia-smi 无输出）")])
            self.assertFalse(st["waiting"][0]["pos"].get("barrier"))
            self.assertEqual([j["name"] for j in st["running"]], ["c"])
        finally:
            os.environ.pop("LABMON_GPUS")
            self.kill_running()

    def test_bad_files_do_not_crash(self):
        # lock / worker.pid 写坏了当没有；坏的登记文件跳过并只记一次事件，别的队列照常
        bad = self.d / "bad"
        bad.mkdir()
        (bad / "lock").write_text("garbage\n")
        self.assertEqual(self.m.read_pid(str(bad / "lock")), 0)
        self.assertEqual(self.m.read_pid(str(bad / "none")), 0)
        (bad / "pid").write_text(" 42\n")
        self.assertEqual(self.m.read_pid(str(bad / "pid")), 42)
        qd = Path(self.w.p.queues)
        qd.mkdir()
        (qd / "broken.json").write_text("{not json")
        (qd / "noroot.json").write_text(json.dumps({"jobs": "q.txt"}))
        (qd / "ok.json").write_text(json.dumps({"root": str(self.d / "ok"), "jobs": "jobs/q.txt", "project": "ok"}))
        for _ in range(2):
            self.assertEqual([q["key"] for q in self.w.registry()], ["ok"])
        ev = [json.loads(l)["msg"] for l in Path(self.w.p.events).read_text().splitlines()]
        self.assertEqual(len(ev), 2)                                  # 两个坏文件各记一次，第二轮不再记
        self.assertTrue(all(str(qd / f) in "\n".join(ev) for f in ("broken.json", "noroot.json")))

    def test_owner_hit_expires(self):
        # 内核杀了对方进程的记录带时刻，过了 VOID_KEEP_S 不再出现在状态里
        self.w.ovr, self.w.info = [{"kind": "guest", "target": "a1"}], {"mem": {}}
        self.w.hit = time.time() - self.m.VOID_KEEP_S + 60
        self.w.oom_check(time.time())
        self.assertTrue(self.w.hit)
        self.w.hit = time.time() - self.m.VOID_KEEP_S - 1
        self.w.oom_check(time.time())
        self.assertIsNone(self.w.hit)

    def test_squeezed_by_failcnt_without_oom_kill(self):
        # 3.10 内核的 cgroup v1 没有 oom_kill 计数：failcnt 涨了且任务被 SIGKILL（退出码 137）就算被挤掉、重新排队；退出码 1 的不算
        cgm = self.d / "cg" / "memory"
        cgm.mkdir(parents=True)
        (cgm / "memory.oom_control").write_text("oom_kill_disable 0\nunder_oom 0\n")
        (cgm / "memory.failcnt").write_text("7")
        m = worker_module(self.d, LABMON_CGROUP=str(self.d / "cg"))
        self.assertIsNone(m.oom_kills())
        w = m.Worker("t")
        w.ovr = [{"kind": "guest", "target": "a1", "keep": {"vram": 4, "ram": 8, "cpu": 2}}]
        root = self.d / "p"
        q = {"key": "k", "root": str(root), "jobs": "q.txt", "state": str(root / "st"), "logs": str(root / "logs"), "project": "a1"}
        os.makedirs(q["state"])
        os.makedirs(q["logs"])

        def finish(rc, fc0):
            Path(q["state"], "x1.rc").write_text(rc)
            w.running["k"] = {"key": "k", "name": "x1", "kind": "queue", "pid": 5, "gpus": ["GPU-a"], "t0": time.time(), "q": q,
                              "project": "a1", "use": {}, "oom0": None, "fc0": fc0}
            w.finish("k")
        finish("137", 3)
        self.assertEqual([v["how"] for v in w.voided], ["squeeze"])
        self.assertEqual(w.oom_ours, 1)
        self.assertFalse(Path(q["state"], "x1.failed").exists())
        finish("1", 3)                                              # 撞过上限但不是被杀的：是任务自己的错
        self.assertTrue(Path(q["state"], "x1.failed").exists())
        self.assertEqual(len(w.voided), 1)

    def test_adopt_reads_gpus_from_environ(self):
        # 接管时上一份状态里没有这个任务：从进程的 CUDA_VISIBLE_DEVICES 找回所用的卡（只认此刻看得到的）
        job = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "labmon-job:x1"])
        proc = self.d / "proc" / str(job.pid)
        proc.mkdir(parents=True)
        (proc / "environ").write_bytes(b"PATH=/bin\0CUDA_VISIBLE_DEVICES=GPU-aaa,GPU-gone\0")
        m = worker_module(self.d, LABMON_PROC=str(self.d / "proc"))
        root = self.d / "p"
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("x1|true   # vram=1\n")
        q = m.load_queue({"_slug": "k", "root": str(root), "jobs": "jobs/q.txt", "project": "a"})
        os.makedirs(q["state"])
        Path(q["state"], "current").write_text("x1 %d %s" % (job.pid, m.now_str()))
        os.environ["LABMON_GPUS"] = "GPU-aaa:40000"
        try:
            w = m.Worker("t")
            w.adopt(q)
        finally:
            os.environ.pop("LABMON_GPUS")
            job.kill()
            job.wait()
        self.assertEqual(w.running["k"]["gpus"], ["GPU-aaa"])

    def test_external_job_start_is_epoch(self):
        # 旧 gpu_queue.sh 还在管的队列：它 current 文件里的文字时刻读进来就换成 epoch 秒 t0（与 running[] 同名），状态里不再有文字时刻 start
        lock = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "gpu_queue"])
        try:
            root = self.d / "p"
            (root / "jobs").mkdir(parents=True)
            (root / "jobs" / "q.txt").write_text("x1|true\n")
            q = self.m.load_queue({"_slug": "k", "root": str(root), "jobs": "jobs/q.txt", "project": "a"})
            os.makedirs(q["state"])
            t0 = time.time() - 600
            Path(q["state"], "lock").write_text(str(lock.pid))
            Path(q["state"], "current").write_text("x1 %d %s" % (lock.pid, self.m.now_str(t0)))
            self.assertEqual(self.m.read_current(q["state"]), ("x1", lock.pid, float(int(t0)), None))
            os.environ["LABMON_GPUS"] = ""
            try:
                self.w.sample([q])
                self.w.tick([q], False)
            finally:
                os.environ.pop("LABMON_GPUS")
            st = json.loads(Path(self.w.p.status).read_text())
            e = st["external"][0]
            self.assertEqual((e["name"], e["pid"], e["t0"]), ("x1", lock.pid, float(int(t0))))
            self.assertNotIn("start", e)
            self.assertEqual(st["waiting"][0]["why"], "旧 gpu_queue.sh（PID %d）还在跑这条队列" % lock.pid)
        finally:
            lock.kill()
            lock.wait()
        Path(q["state"], "current").write_text("x1 abc")                   # PID 写坏、没写时刻：PID 算 0，时刻算现在
        name, pid, t0, _ = self.m.read_current(q["state"])
        self.assertEqual((name, pid), ("x1", 0))
        self.assertAlmostEqual(t0, time.time(), delta=2)

    def test_run_end_overdue_and_unknown(self):
        # 超了预计的运行中任务：剩余按已跑的 20%（至少 10 分钟）估，不当马上结束；时长不明的为 None，留的卡不知何时腾空、不回填
        now = time.time()
        self.w.info = {"gpus": [{"i": 0, "uuid": "GPU-a", "name": "A", "total": 40000, "free": 40000}]}
        j = lambda name, t0, est=None: {"key": name, "kind": "slot", "name": name, "project": "p", "t0": t0, "est_min": est, "gpus": ["GPU-a"], "use": {"cores": 1}}
        self.assertAlmostEqual(self.w.run_end(j("a", now - 300, 60), now), now + 3300, delta=1)
        self.assertAlmostEqual(self.w.run_end(j("b", now - 600, 1), now), now + 600, delta=1)
        self.assertAlmostEqual(self.w.run_end(j("c", now - 7200, 1), now), now + 1440, delta=1)
        self.assertIsNone(self.w.run_end(j("d", now - 600), now))
        until = lambda cards: self.m.hold_until(self.w.snapshot({}), cards, now)
        self.w.running = {"d": j("d", now - 600)}
        self.assertIsNone(until(["GPU-a"]))
        self.w.running["a"] = j("a", now - 300, 60)
        self.assertIsNone(until(["GPU-a"]))
        del self.w.running["d"]
        self.assertAlmostEqual(until(["GPU-a"]), now + 3300, delta=1)
        self.assertAlmostEqual(until(None), now + 3300, delta=1)                         # 整台主机：看所有在跑的
        self.w.running["h"] = j("hold_x", now - 60)                                     # 占位任务不占卡：留卡何时腾空不看它
        self.assertAlmostEqual(until(["GPU-a"]), now + 3300, delta=1)
        self.assertEqual(until(["GPU-b"]), now)                                          # 卡上没人：现在就空


class GuestTest(UnitBase):
    """借来的主机（guest 指令）：给对方留的量、对方超出预留、整台不放、快撞上限、被挤掉、内核杀了对方的进程。用假 cgroup。"""

    KEEP = {"vram": 8, "ram": 16, "cpu": 4}

    def extra(self):
        return {"LABMON_CGROUP": str(self.d / "cg")}

    def guest(self, keep=KEEP, **kw):
        self.w.ovr = [dict({"kind": "guest", "target": "a1", "t": 0}, **({"keep": keep} if keep else {}), **kw)]

    def oom(self, n):
        """容器的 oom_kill 计数（cgroup v2 memory.events）。"""
        (self.d / "cg").mkdir(exist_ok=True)
        (self.d / "cg" / "memory.events").write_text("low 0\noom_kill %d\n" % n)

    def test_keep_left(self):
        # 单卡就是对方的卡：留 8 GiB 减去对方已用；多卡只留 0 号卡与对方有进程的卡；借来时对方空着（hold 为假）或不是借来的不留
        self.cards(40000)
        self.assertEqual(self.w.keep_left(0, "GPU-a"), 0.0)
        self.guest()
        self.w.other["vram"] = {"GPU-a": 2048.0}
        self.assertEqual(self.w.keep_left(0, "GPU-a"), 8 * 1024 - 2048)
        self.w.other["vram"] = {"GPU-a": 10000.0}
        self.assertEqual(self.w.keep_left(0, "GPU-a"), 0.0)                              # 对方用的已超过预留：没有“还没用上的”
        self.cards(40000, 40000, 40000)
        self.w.other["vram"] = {"GPU-b": 1000.0}
        self.assertEqual([self.w.keep_left(i, u) for i, u in ((0, "GPU-a"), (1, "GPU-b"), (2, "GPU-c"))], [8192.0, 7192.0, 0.0])
        self.guest(dict(self.KEEP, hold=False))
        self.assertEqual(self.w.keep_left(0, "GPU-a"), 0.0)

    def test_guest_scan_and_block(self):
        # 对方某张卡超出预留：只那张卡不放；内存或 CPU 超出：整台不放；回到预留以内后 GUEST_QUIET_S 内仍算着；变化时各记一次事件
        gpus = self.cards(40000, 40000)
        self.guest()
        t = time.time()
        self.w.other = {"vram": {"GPU-b": 9000.0}, "ram": 10.0, "cpu": 2.0, "ka": {}}
        self.w.guest_scan(t)
        self.assertEqual(self.w.back, {"GPU-b": "卡 1：对方用 8.8 GiB 显存，预留 8"})
        self.assertIsNone(self.w.guest_block())                                           # 只超出一张卡：整台照放
        self.assertIn("这部分不再放新任务", self.events("guest")[-1]["msg"])
        self.w.other["ram"] = 20.0
        self.w.guest_scan(t + 1)
        self.assertEqual(sorted(self.w.back), ["GPU-b", "ram"])
        self.assertEqual(self.w.guest_block(), "借来的主机：内存：对方用 20.0 GiB，预留 16，暂不放行")
        self.assertEqual(len(self.events("guest")), 1)                                   # 仍是“超出”：不重复记
        self.w.other = {"vram": {}, "ram": 1.0, "cpu": 0.5, "ka": {}}
        self.w.guest_scan(t + 2)
        self.assertEqual((self.w.back, sorted(self.w.back_last)), ({}, ["GPU-b", "ram"]))  # 回落：仍记着，静默期内不放
        self.assertEqual(self.w.guest_block(), "借来的主机：对方的内存 0 分钟前超出预留，暂不放行")
        self.assertIn("回到预留以内", self.events("guest")[-1]["msg"])
        self.w.guest_scan(t + 2 + self.m.GUEST_QUIET_S)
        self.assertEqual((self.w.back_last, self.w.guest_block()), ({}, None))
        # 放行时那张卡不给：对方在卡 1 上超出（或刚回落），请求 1 张卡时只剩卡 0；两张卡都被对方占着就等，原因写明
        self.w.other["vram"] = {"GPU-b": 9000.0}
        self.w.guest_scan(t + 3 + self.m.GUEST_QUIET_S)
        starts = []
        self.w.launch = lambda r, sel: starts.append([c["uuid"] for c in sel])
        self.assertIsNone(self.place(self.req(project="a1", ngpu=1, vram=1.0), gpus))
        self.assertEqual(starts, [["GPU-a"]])
        self.assertEqual(self.req(ngpu=1, vram=1.0)["held"], ("guest", "借来的主机只给 a1 用"))   # 别的项目被指令挡住，不进排队
        self.w.other["vram"] = {"GPU-a": 9000.0, "GPU-b": 9000.0}
        self.w.guest_scan(t + 4 + self.m.GUEST_QUIET_S)
        why, res = self.place(self.req(project="a1", ngpu=1, vram=1.0), gpus)
        self.assertTrue(res)
        self.assertIn("对方在用卡 0,1（超出预留或刚回落，10 分钟内不放）", why)
        self.assertIn("已扣给对方的预留", why)
        # 借来时对方空着（hold 为假）或旧指令没带 keep：对方有 GPU 进程的卡就算超出
        self.guest(dict(self.KEEP, hold=False))
        self.w.other = {"vram": {"GPU-a": 100.0}, "ram": 1.0, "cpu": 0.5, "ka": {}}
        self.w.guest_scan(t + 5 + self.m.GUEST_QUIET_S)
        self.assertEqual(self.w.back, {"GPU-a": "卡 0：对方用 0.1 GiB 显存，借来时对方空着"})
        self.guest(None)
        self.w.guest_scan(t + 6 + self.m.GUEST_QUIET_S)
        self.assertEqual(list(self.w.back), ["GPU-a"])
        self.w.ovr = []                                                                     # 还了：全清
        self.w.guest_scan(t + 7 + self.m.GUEST_QUIET_S)
        self.assertEqual((self.w.back, self.w.back_last, self.w.tight), ({}, {}, None))

    def test_unstoppable_keepalive_card_in_plan(self):
        # #45：两卡借来的主机，卡 0 上的单元还要 30 分钟，卡 1 上有对方另起、ka.sh 停不了的保活（只占 6 GiB）：真放行不放卡 1，推演也不放，
        # 等着的单元排在 30 分钟后的卡 0（旧推演清空了 owner，排在马上、卡 1）。保活没了：推演当轮重推（不等 FORECAST_EVERY）、排到马上的卡 1；
        # 真放行照旧挡 GUEST_QUIET_S（刚回落是此刻才有的，推演不模拟，#25）
        now = time.time()
        gpus = self.cards(10000, 34000)
        self.guest(dict(self.KEEP, hold=False))
        self.w.running["slot:1"] = {"key": "slot:1", "kind": "slot", "name": "r1", "project": "a1", "t0": now, "est_min": 30, "gpus": ["GPU-a"],
                                    "est": {"ram": 1.0, "vram": 30.0}, "use": {}, "tag": None, "excl": None}
        self.w.other = {"vram": {"GPU-b": 6000.0}, "ram": 1.0, "cpu": 0.5, "ka": {"GPU-b": [5300]}, "gid": {"GPU-b": {5300}}, "ka_cpu": 0.0}
        self.w.guest_scan(now)
        r = self.req(key="slot:2", project="a1", name="u2", ngpu=1, vram=9.0, est_min=10)
        self.w.forecast([], [r], {}, now, self.m.POLICY)
        u2 = next(j for ln in self.w.plan["lanes"] for j in ln["jobs"] if j["name"] == "u2")
        self.assertEqual((u2["t0"], u2["gpus"], u2.get("after")), (round(now + 1800), [0], [["a1", "r1"]]))
        cards, why, res = self.m.place(self.w.as_job(r), self.w.snapshot({}, gpus), now)
        self.assertIsNone(cards)
        self.assertIn("卡 1 上有对方另起、ka.sh 停不了的保活，不放（要停请对方停）", why)
        self.w.other = dict(self.w.other, vram={}, ka={}, gid={})
        self.w.guest_scan(now + 1)
        self.assertIsNotNone(self.m.place(self.w.as_job(r), self.w.snapshot({}, gpus), now + 1)[1])   # 真放行：刚回落，10 分钟内仍不放
        self.w.forecast([], [r], {}, now + 2, self.m.POLICY)
        u2 = next(j for ln in self.w.plan["lanes"] for j in ln["jobs"] if j["name"] == "u2")
        self.assertEqual((self.w.plan["t"], u2["t0"], u2["gpus"]), (now + 2, round(now + 2), [1]))

    def test_guest_cpu_two_rounds_and_keepalive_cores(self):
        """对方的 CPU（#29）：保活进程树这一段的核数从 labmon 之外的里扣掉（ka_cores，上一轮没有的进程不计）；
        超出预留连着两轮才算，刚停的保活留下的一轮尖峰不挡放行。"""
        t = time.time()
        self.guest()
        self.w.other = {"vram": {}, "ram": 1.0, "cpu": 6.0, "ka": {}}
        self.w.guest_scan(t)
        self.assertEqual((self.w.back, self.w.guest_block()), ({}, None))           # 一轮超出：不算
        self.w.other["cpu"] = 1.0
        self.w.guest_scan(t + 10)
        self.w.other["cpu"] = 6.0
        self.w.guest_scan(t + 20)
        self.assertEqual(self.w.back, {})                                           # 中间回落过：重新数
        self.w.guest_scan(t + 30)
        self.assertEqual(self.w.back, {"cpu": "CPU：对方用 6.0 核，预留 4"})
        self.assertEqual(self.w.guest_block(), "借来的主机：CPU：对方用 6.0 核，预留 4，暂不放行")
        procs = {300: {"ppid": 1, "ticks": 0}, 301: {"ppid": 300, "ticks": 0}}
        self.assertEqual(self.w.ka_cores(procs, [300, 301], t), 0.0)              # 第一轮没有区间
        tck = os.sysconf("SC_CLK_TCK")
        procs = {300: {"ppid": 1, "ticks": 10 * tck}, 301: {"ppid": 300, "ticks": 5 * tck}}
        self.assertAlmostEqual(self.w.ka_cores(procs, [300, 301], t + 10), 1.5)   # (10 + 5) 秒 / 10 秒

    def test_cpu_room_ext_keepalive(self):
        """ka.sh 停不了的保活占的核（other.ka_cpu，#37）另从 cpu_room 扣：不和预留取 max（混在对方用量里时会被预留吃掉），
        不看 labmon 之外连着几轮（cpu_n）；借来时对方空着（不预先留）也扣。"""
        self.w.info = {"cpu_quota": 17.0}
        self.guest(dict(self.KEEP, ram=5.3, cpu=1.1))
        self.w.other = {"vram": {}, "ram": 4.2, "cpu": 0.3, "ka": {}, "ka_cpu": 1.7}
        self.w.cpu_n = 0
        self.assertAlmostEqual(self.w.cpu_room(), 17 - 1.1 - 1.7)
        self.w.cpu_n = 2
        self.assertAlmostEqual(self.w.cpu_room(), 17 - 1.1 - 1.7)                 # 对方 0.3 在预留以内：取预留
        self.guest(dict(self.KEEP, cpu=1.1, hold=False))
        self.assertAlmostEqual(self.w.cpu_room(), 17 - 0.3 - 1.7)

    def test_keepalive_cores_tree_sum(self):
        """保活进程树的核数按整棵树的累计求和求差（同 measure，cpu_since）：DataLoader 子进程被主进程回收时，一生的 CPU 并进主进程的 cutime，
        逐个 PID 求差会把它整份算成这一段的、盖掉对方真超出（审查阻塞 1 现象 B）。"""
        tck = os.sysconf("SC_CLK_TCK")
        t = time.time()
        self.w.ka_cores({300: {"ticks": 50 * tck}, 301: {"ticks": 100 * tck}}, [300, 301], t)
        # 这 10 秒：主进程用了 5 秒，301 退出被回收（100 秒并进主进程），新开的 302 用了 1 秒
        self.assertAlmostEqual(self.w.ka_cores({300: {"ticks": 155 * tck}, 302: {"ticks": 1 * tck}}, [300, 302], t + 10), 0.6)

    def test_keepalive_first_seen_no_two_round_block(self):
        """保活有一轮没认出来（刚拉起还没建 CUDA 上下文、nvidia-smi 超时）：那一轮它的核数算到对方头上；下一轮认出来时整棵树启动以来的 CPU
        一起扣掉（多扣、偏宽），不会连着两轮判超出、整台挡 10 分钟（审查阻塞 1 现象 A）。"""
        tck = os.sysconf("SC_CLK_TCK")
        t = time.time()
        self.guest(dict(self.KEEP, cpu=1.1))
        for i, (extra, procs) in enumerate([(0.1, {}), (1.3, {}), (1.8, {300: {"ticks": 30 * tck}, 301: {"ticks": 4 * tck}, 302: {"ticks": 4 * tck}})]):
            ka = self.w.ka_cores(procs, list(procs), t + 10 * i)
            self.w.other = {"vram": {}, "ram": 1.0, "cpu": max(0.0, extra - ka), "ka": {}}   # 同 sample：labmon 之外的减去保活进程树
            self.w.guest_scan(t + 10 * i)
        self.assertEqual((self.w.back, self.w.guest_block()), ({}, None))

    def test_guest_tight(self):
        # 对方超出预留且那张卡只剩不到 1 GiB、或容器内存（不算页缓存）到 95%：记“快撞上限”，只记一次事件
        self.cards(500, 20000)
        self.guest()
        self.w.back = {"GPU-a": "卡 0：对方用 30.0 GiB 显存，预留 8"}
        self.w.guest_tight(time.time())
        self.assertEqual(self.w.tight, "卡 0：对方用 30.0 GiB 显存，预留 8，这张卡只剩 500 MB")
        self.w.guest_tight(time.time())
        self.assertEqual(len(self.events("guest")), 1)
        self.w.back = {"ram": "内存：对方用 20.0 GiB，预留 16"}
        self.w.info["mem"] = {"limit": 100.0, "used": 90.0, "anon": 96.0}
        self.w.guest_tight(time.time())
        self.assertEqual(self.w.tight, "内存：对方用 20.0 GiB，预留 16，容器内存（不算页缓存）96 / 100 GiB")
        self.w.info["mem"]["anon"] = 50.0
        self.w.guest_tight(time.time())
        self.assertIsNone(self.w.tight)
        self.w.back = {"GPU-b": "卡 1：…"}                                                   # 超出的那张卡还有 20 GiB：不算
        self.w.guest_tight(time.time())
        self.assertIsNone(self.w.tight)

    def test_squeezed_by(self):
        # 运行期间 oom_kill 涨了且任务被 SIGKILL（137，或 bash 一起被杀没留退出码）：被挤掉；退出码 1 的不算，再看日志末尾是不是 CUDA OOM 且对方在同卡
        root = self.d / "p"
        q = {"key": "k", "root": str(root), "jobs": "q.txt", "state": str(root / "st"), "logs": str(root / "logs"), "project": "a1"}
        os.makedirs(q["logs"])
        j = lambda **kw: dict({"key": "k", "name": "x1", "kind": "queue", "pid": 5, "gpus": ["GPU-a"], "t0": time.time(), "q": q, "project": "a1",
                               "use": {}, "oom0": 3, "fc0": None}, **kw)
        sq = lambda x, rc: self.w.squeezed_by(x, self.m.oom_of(x, rc))                    # noqa: E731 — finish 里同样先 oom_of 再 squeezed_by
        self.oom(5)
        self.assertIsNone(sq(j(), "137"))                                 # 不是借来的
        self.guest()
        self.assertIsNone(sq(j(kind="slot"), "137"))                      # 单元由申请方自己处理
        self.assertEqual(sq(j(), "137"), "容器内存撞上限，被内核杀掉")
        self.assertEqual(sq(j(), None), "容器内存撞上限，被内核杀掉")
        self.assertEqual(self.w.oom_ours, 2)
        self.assertIsNone(sq(j(), "1"))
        (root / "logs" / "x1.log").write_text("step 3\ntorch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB\n")
        self.assertIsNone(sq(j(), "1"))                                   # 对方没在这张卡上用过：是任务自己要的太多
        self.assertEqual(sq(j(owner_gpu=True), "1"), "对方在同一张卡上，显存不够（CUDA out of memory）")
        self.oom(3)                                                                         # 计数没涨：退出码 137 也不算被挤
        self.assertIsNone(sq(j(), "137"))

    def test_finish_marks_oom(self):
        # #62：失败的清单任务是 OOM 的，用量行记 oom（日志末尾 CUDA OOM 为 vram；容器 oom_kill 涨了且退出码 137 或没留为 ram），失败事件句尾说同类下次抬高；
        # 借来的主机上被对方挤掉的（重排的与挤满次数后记失败的）不记；停下的不认
        root = self.d / "p"
        q = {"key": "k", "root": str(root), "jobs": "q.txt", "state": str(root / "st"), "logs": str(root / "logs"), "project": "a1"}
        os.makedirs(q["state"])
        os.makedirs(q["logs"])
        lg = root / "logs" / "x1.log"

        def finish(rc, log="step 3\n", **kw):
            lg.write_text(log)
            Path(q["state"], "x1.rc").write_text(rc)
            self.w.running["k"] = dict({"key": "k", "name": "x1", "kind": "queue", "pid": 5, "gpus": ["GPU-a"], "t0": time.time(), "q": q, "project": "a1",
                                        "use": {}, "oom0": 3, "fc0": None, "est": {"vram": 7.0, "ram": 1.0}}, **kw)
            self.w.finish("k")
            return json.loads(Path(self.w.p.usage).read_text().splitlines()[-1])
        cuda = "step 3\ntorch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB\n"
        self.oom(3)
        self.assertIsNone(finish("1")["oom"])                                              # 别的失败
        self.assertEqual(finish("1", cuda)["oom"], "vram")
        self.assertIn("失败 x1（退出码 1，见 logs/x1.log；CUDA 显存不够（OOM），同类下次估计抬高）", self.events("failed")[-1]["msg"])
        self.assertIn("CUDA 显存不够（OOM），同类下次估计抬高", (root / "st" / "queue.log").read_text())
        self.assertIsNone(finish("1", cuda, stopping=True)["oom"])                          # 暂停停下的：不认
        self.assertIsNone(finish("0", cuda)["oom"])
        self.oom(5)
        self.assertEqual(finish("137")["oom"], "ram")
        self.assertIn("容器内存撞上限被杀（OOM），同类下次估计抬高", self.events("failed")[-1]["msg"])
        self.assertIsNone(finish("1")["oom"])                                              # 计数涨了但不是被杀的
        self.guest()
        row = finish("137")                                                                 # 借来的主机：被对方挤掉，重排、不记
        self.assertEqual((row["oom"], row["requeued"]), (None, True))
        self.assertEqual(finish("1", cuda)["oom"], "vram")                                  # 对方没在这张卡上：是自己要的太多
        self.assertEqual(finish("1", cuda, owner_gpu=True)["oom"], None)                   # 第 2 次被挤掉
        row = finish("137")                                                                 # 挤满 SQUEEZE_MAX 次后记失败：照样不记
        self.assertEqual((row["oom"], row["requeued"]), (None, False))
        self.assertIn("不再重排", self.events("failed")[-1]["msg"])

    def test_oom_check(self):
        # oom_kill 比上一轮涨的比我们被杀的多：内核杀了 labmon 之外的进程（借来的主机上多半是对方），记时刻 hit 与事件；不是借来的不记
        t = time.time()
        self.w.info = {"mem": {"oom_kill": 3.0}}
        self.w.oom_check(t)                                                                 # 第一轮只记基数
        self.assertEqual((self.w.oom_n, self.w.hit), (3.0, None))
        self.w.info["mem"]["oom_kill"], self.w.oom_ours = 5.0, 1
        self.w.oom_check(t)
        self.assertIsNone(self.w.hit)                                                       # 不是借来的
        self.guest()
        self.w.info["mem"]["oom_kill"], self.w.oom_ours = 8.0, 1
        self.w.oom_check(t + 1)
        self.assertEqual((self.w.hit, self.w.oom_ours), (round(t + 1), 0))
        self.assertIn("杀了 labmon 之外的进程 2 次", self.events("owner_oom")[-1]["msg"])
        self.w.info["mem"]["oom_kill"], self.w.oom_ours = 9.0, 1                           # 只杀了我们的：不算
        self.w.oom_check(t + 2)
        self.assertEqual((self.w.hit, len(self.events("owner_oom"))), (round(t + 1), 1))


class VoidTest(UnitBase):
    """外来进程与作废：独占任务会被谁打扰（intruders），运行中连着两轮被打扰就作废（void_check → void_job），停下重排的记录（note_stop）。"""

    def gp(self, owner, uuid, hpid=7, job=None):
        return {"hpid": hpid, "pid": None, "mb": 100.0, "uuid": uuid, "comm": "python" if owner == "other" else None, "owner": owner,
                "ka": False, "job": job["name"] if job else None, "_j": job}

    def test_intruders(self):
        me = {"name": "e", "gpus": ["A"], "ngpu": 1, "use": {}}
        o = {"name": "o", "gpus": ["A"], "ngpu": 1, "use": {}}
        jobs = [me, o, {"name": "p", "gpus": ["B"], "ngpu": 1, "use": {}}, {"name": "hold_x", "gpus": ["A"], "ngpu": 1, "use": {}},
                {"name": "sup", "gpus": [], "ngpu": 0, "use": {"cores": 0.05}}]           # 占位任务与不占 CPU 的监督进程不算打扰
        gp = [self.gp("other", "A"), self.gp("keepalive", "B", 8), self.gp("job", "A", 9, o)]
        I = self.m.intruders
        self.assertEqual(I("gpu", ["A"], jobs, gp, None, me), (["o"], ["PID 7（python）"]))
        self.assertEqual(I("gpu", ["B"], jobs, gp, None, me), (["GPU 保活", "p"], []))
        self.assertEqual(I("gpu", ["A"], jobs, gp, None), (["e", "o"], ["PID 7（python）"]))   # 放行前（me 为空）：卡上所有任务都算
        self.assertEqual(I("host", (), jobs, gp, 2.5, me), (["GPU 保活", "o", "p"], ["PID 7（python）", "labmon 之外的进程占 2.5 核"]))
        self.assertEqual(I("host", (), jobs, gp, 0.5, me)[1], ["PID 7（python）"])           # 不到 VOID_CPU 核不算
        self.assertEqual(I("gpu", ["C"], jobs, gp, None, me), ([], []))

    def test_void_check_and_note_stop(self):
        # 独占卡的 e 同卡连着两轮有 labmon 之外的进程：第二轮作废（直接 SIGTERM、记 voided 与事件），之后不再重复；一轮的尖峰不算
        stops = []
        self.w.send_stop = lambda j, mode: stops.append((j["name"], mode))
        e = {"key": "e", "kind": "queue", "name": "e", "project": "p", "excl": "gpu", "gpus": ["A"], "pid": 1, "use": {}, "ngpu": 1}
        n = {"key": "n", "kind": "queue", "name": "n", "project": "p", "excl": None, "gpus": ["B"], "pid": 2, "use": {}, "ngpu": 1}
        self.w.running = {"e": e, "n": n}
        jobs, near = [e, n], {id(e): {"A"}, id(n): {"B"}}
        hit = [self.gp("other", "A")]
        self.w.void_check(jobs, near, hit, None)
        self.assertEqual((e["void_n"], stops, self.w.voided), (1, [], []))
        self.w.void_check(jobs, near, [], None)                                            # 这一轮没了：重新数
        self.assertEqual(e["void_n"], 0)
        self.w.void_check(jobs, near, hit, None)
        self.w.void_check(jobs, near, hit, None)
        self.assertEqual((e["stopping"], e["void"], stops), (True, "独占卡期间有 PID 7（python）", [("e", "term")]))
        self.assertEqual([(v["name"], v["how"], v["why"]) for v in self.w.voided], [("e", "void", "独占卡期间有 PID 7（python）")])
        self.assertEqual(self.events("void")[0]["name"], "e")
        self.w.void_check(jobs, near, hit, None)                                           # 已在停：不再作废第二次
        self.assertEqual(len(stops), 1)
        self.assertNotIn("void_n", n)                                                       # 不独占的不看
        # 整机独占：labmon 之外的进程占 VOID_CPU 核以上也算打扰
        h = {"key": "h", "kind": "slot", "name": "h", "project": "p", "excl": "host", "gpus": [], "pid": 3, "use": {}, "ngpu": 0}
        self.w.running = {"h": h}
        for _ in range(2):
            self.w.void_check([h], {id(h): set()}, [], 1.5)
        self.assertEqual((h["void"], stops[-1]), ("独占整机期间有 labmon 之外的进程占 1.5 核", ("h", "term")))
        self.assertIn("单元已停下", self.events("void")[-1]["msg"])
        # note_stop：留 VOID_KEEP_S，过期的清掉；被挤掉的 how 为 squeeze
        self.w.voided[0]["t"] = time.time() - self.m.VOID_KEEP_S - 1
        self.w.note_stop(n, "容器内存撞上限", "squeeze")
        self.assertEqual([(v["name"], v["how"]) for v in self.w.voided], [("h", "void"), ("n", "squeeze")])


class AdmitTest(UnitBase):
    """放行顺序（admit）：focus、用量 ÷ 权重、先来先跑、等满 wait_cap 的屏障与回填、排位；屏障预计何时放行（shadow）。"""

    NOW = 1_000_000.0

    def rq(self, key, project, since, dur=None, charge=0.0, kind="queue", name=None):
        return {"key": key, "kind": kind, "project": project, "name": name or key, "since": self.NOW - since, "dur": dur, "charge": charge}

    def test_order(self):
        order = []
        place = lambda r: order.append(r["key"])                                           # 都放行（返回 None）
        reqs = [self.rq("a1", "a", 100), self.rq("a2", "a", 200), self.rq("b1", "b", 50), self.rq("c1", "c", 10)]
        self.assertEqual(self.m.admit(reqs, {"c"}, self.m.POLICY, {"a": 1.0, "b": 4.0}, self.NOW, place), [])
        self.assertEqual(order, ["c1", "a2", "a1", "b1"])                                   # focus 先；a 用得少先；a 里先来的 a2 先
        order.clear()
        self.m.admit(reqs, set(), dict(self.m.POLICY, w={"b": 8.0}), {"a": 1.0, "b": 4.0}, self.NOW, place)
        self.assertEqual(order, ["c1", "b1", "a2", "a1"])                                   # 没用过的 c 先；b 的 4 ÷ 8 < a 的 1
        order.clear()
        reqs[1]["charge"] = 5.0                                                             # 放行 a2 先记 5 卡·时：a 变成 6，轮到 b1
        self.m.admit(reqs, set(), self.m.POLICY, {"a": 1.0, "b": 4.0}, self.NOW, place)
        self.assertEqual(order, ["c1", "a2", "b1", "a1"])

    def test_barrier_backfill_and_pos(self):
        cap, shadows = self.m.POLICY["cap"], []
        place = lambda r: ("显存不够", True) if r["key"] == "big" else None

        def shadow(r):
            shadows.append(r["key"])
            return self.NOW + cap
        big = self.rq("big", "a", 31 * 60)
        reqs = [big, self.rq("small", "b", 60, dur=10), self.rq("slow", "c", 30, dur=60), self.rq("hold_x", "d", 5)]
        out = self.m.admit(reqs, set(), self.m.POLICY, {}, self.NOW, place, shadow)
        self.assertEqual([(r["key"], pos["n"]) for r, _, pos in out], [("big", 1), ("slow", 2)])     # small 回填（10×1.2 分钟内跑完），占位任务不受屏障管
        self.assertEqual(out[0][2], {"n": 1, "focus": False, "ratio": 0.0, "barrier": True})
        self.assertEqual(out[1][2]["behind"], ["a", "big"])
        self.assertEqual(out[1][1], "让位：a 的 big 已等 31 分钟（等了 30 分钟以上的不再被越过；它预计 %s 能放行，这之前跑得完的才放）"
                         % self.m.now_str(self.NOW + cap)[5:16])
        self.assertEqual(shadows, ["big"])                                                  # 后面有请求才推，且只推一次
        out = self.m.admit([big, self.rq("slow", "c", 30, dur=60)], set(), self.m.POLICY, {}, self.NOW, place, lambda r: None)
        self.assertEqual(out[1][1], "让位：a 的 big 已等 31 分钟（等了 30 分钟以上的不再被越过）")       # 推不出何时放行：一律让位
        out = self.m.admit([big], set(), self.m.POLICY, {}, self.NOW, place, shadow)
        self.assertEqual((len(out), shadows), (1, ["big"]))                                 # 只有屏障自己：不推
        out = self.m.admit([self.rq("big", "a", 100), self.rq("slow", "c", 30, dur=60)], set(), self.m.POLICY, {}, self.NOW, place)
        self.assertEqual([r["key"] for r, _, _ in out], ["big"])                            # 没等满 cap：不成屏障
        self.assertNotIn("barrier", out[0][2])
        place2 = lambda r: ("缺本地文件", False) if r["key"] == "big" else None             # 不是卡在资源上：等再久也不挡人
        out = self.m.admit([big, self.rq("slow", "c", 30, dur=60)], set(), self.m.POLICY, {}, self.NOW, place2)
        self.assertEqual([r["key"] for r, _, _ in out], ["big"])

    def test_fits_before(self):
        f = self.m.fits_before
        self.assertEqual((f(0, 10, 720), f(0, 10, 719), f(0, None, 1000), f(0, 10, None)), (True, False, False, False))

    def test_shadow(self):
        # 屏障 u 要 21 GiB，卡上 r1 占着 30 GiB 还要跑 20 分钟：预计那时放行；要的比卡大则推不出
        now = time.time()
        self.cards(9000)
        self.w.running["r"] = {"key": "r", "kind": "slot", "name": "r1", "project": "a", "t0": now - 600, "est_min": 30, "gpus": ["GPU-a"],
                               "est": {"ram": 1.0, "vram": 30.0}, "use": {}, "tag": None, "excl": None}
        r = self.req(ngpu=1, vram=20.0, est_min=10)
        self.assertAlmostEqual(self.w.shadow(r, now, self.m.POLICY, set(), {}), now + 1200, delta=1)
        self.assertIsNone(self.w.shadow(self.req(ngpu=1, vram=50.0, est_min=10), now, self.m.POLICY, set(), {}))


class ShareTest(UnitBase):
    """公平份额的输入：本机在跑的（own_runs）、别的主机在跑的（peers）、合起来的衰减用量（share）；运行中任务的预计结束取看板写来的（run_end）。"""

    def test_own_runs_and_share(self):
        now = time.time()
        self.cards(40000)
        j = lambda name, project, t0, est_min, ngpu=1, vram=20.0: {"key": name, "kind": "slot", "name": name, "project": project, "t0": t0,
                                                                   "est_min": est_min, "gpus": ["GPU-a"] if ngpu else [], "ngpu": ngpu,
                                                                   "est": {"ram": 1.0, "vram": vram}, "use": {}, "excl": None}
        self.w.running = {"a": j("a", "pa", now - 1800, 60), "h": j("hold_x", "pa", now - 60, None), "b": j("b", "pb", now - 60, 10, ngpu=0)}
        runs = self.w.own_runs(now)
        self.assertEqual([(p, c, t0) for p, c, t0, _ in runs], [("pa", 0.512, round(now - 1800)), ("pb", 0.0, round(now - 60))])   # 20 GiB 占 39 GiB 卡；占位不算
        self.assertEqual(runs[0][3], round(now + 1800))
        use = self.w.share(now, self.m.POLICY)
        self.assertAlmostEqual(use["pa"], 0.512 * (0.5 + 0.5), delta=0.01)                 # 已跑半小时（衰减一点）+ 预计还要跑半小时
        self.assertEqual(use["pb"], 0.0)

    def test_peers(self):
        now = time.time()
        w = lambda h, run, age=0, reread=True: self.hosts.status(h, age=age, reread=reread, share={"run": run})   # noqa: E731
        f = w("o", [["x", 1.0, now - 3600, now + 3600]])
        os.utime(f, (now - 400, now - 400))                                                # 修改时间是共享盘服务器的时钟（慢几分钟）：不看它，看状态里的 t
        w("stale", [["y", 1.0, now - 3600, now + 3600]], age=400)                           # 那台 worker 不在了
        w("t", [["me", 1.0, now, now + 60]])                                               # 本机自己的不算（own_runs 另记）
        self.assertEqual(self.w.peers(now), [("x", 1.0, now - 3600, now + 3600)])
        w("o2", [["z", 2.0, now, now + 60]], reread=False)
        self.assertEqual(len(self.w.peers(now + 10)), 1)                                   # PEER_EVERY 内不重读
        self.assertEqual(len(self.w.peers(now + self.m.PEER_EVERY)), 2)

    def test_run_end_uses_dashboard_eta(self):
        # 看板按日志进度算的预计结束（eta.json）优先，但只认任务开始后写的；没有就 开始 + 预计时长
        now = time.time()
        j = {"key": "s", "kind": "slot", "name": "u", "project": "p", "t0": now - 100, "est_min": 30, "gpus": [], "use": {}}
        Path(self.w.p.eta).write_text(json.dumps({"t": now, "jobs": {"p|u": now + 500}}))
        self.w.load_eta()
        self.assertAlmostEqual(self.w.run_end(j, now), now + 500, delta=1)
        Path(self.w.p.eta).write_text(json.dumps({"t": now - 200, "jobs": {"p|u": now + 500}}))
        os.utime(self.w.p.eta, (now + 1, now + 1))                                          # 文件变了才重读
        self.w.load_eta()
        self.assertAlmostEqual(self.w.run_end(j, now), now - 100 + 1800, delta=1)
        os.remove(self.w.p.eta)
        self.w.load_eta()
        self.assertEqual(self.w.eta, {})
        # 占位任务不看看板写来的（它没有日志进度，看板的就是推演的回声）：按估时
        Path(self.w.p.eta).write_text(json.dumps({"t": now, "jobs": {"p|hold_u": now + 500}}))
        self.w.load_eta()
        self.assertAlmostEqual(self.w.run_end(dict(j, name="hold_u"), now), now - 100 + 1800, delta=1)


class DepsTest(UnitBase):
    """任务依赖（after= 跑成功、afterany= 跑完）：按 项目 + 任务名 认（dep_state），没满足的不放行（dep_block，同被临时指令挡住），
    推演里排在依赖满足的时刻，占位任务排上后结束取 dep_end。"""

    def test_dep_end(self):
        k, e = self.m.dep_keys, self.m.dep_end
        keys = k({"project": "p", "deps": ["x", "q/y"], "deps_any": ["z"]})
        self.assertEqual(keys, [("p", "x", False), ("q", "y", False), ("p", "z", True)])
        ends = {("p", "x"): 500, ("q", "y"): 300, ("p", "z"): 200}
        self.assertEqual(e(keys, 100, ends), 500)                                     # 取最晚的
        self.assertEqual(e(keys, 600, ends), 600)                                     # 都已结束：不早于此刻
        self.assertIsNone(e(keys, 100, {("p", "x"): 500}))                            # 有不知道何时结束的
        self.assertIsNone(e(keys, 100, ends, {("p", "x")}))                           # after= 等的失败了：永远满足不了
        self.assertEqual(e(keys, 100, dict(ends, **{}), {("p", "z")}), 500)           # afterany= 等的失败了也算跑完
        self.assertEqual(self.m.dep_names(keys, "p"), "x、q/y、z")

    def test_dep_block_states(self):
        # a 清单：ok 跑成功、bad 失败、again 失败过又删了 .failed 重跑（还会跑）；b 清单的各行等它们；ghost 哪里都没有
        qs = self.hosts.queues({"a": ["ok|true", "bad|true", "again|true"],
                                "b": ["w1|true   # after=ok", "w2|true   # after=bad", "w3|true   # afterany=bad", "w4|true   # after=again",
                                      "w5|true   # after=ghost", "w6|true   # after=ok,pb/far"]}, "pa")
        os.makedirs(qs[0]["state"])
        for f in ("ok.done", "bad.failed"):
            Path(qs[0]["state"], f).write_text("")
        self.w.hist = [{"project": "pa", "name": "again", "rc": "1", "t1": 1, "ram_peak": 1.0},
                       {"project": "pb", "name": "far", "rc": "0", "t1": 2, "ram_peak": 1.0},
                       {"project": "pb", "name": "far", "rc": "137", "t1": 3, "ram_peak": 1.0, "requeued": True}]   # 被挤掉重排的不算失败
        now = time.time()
        self.w.queue_durs(qs)
        self.w.dep_state(qs, now)
        self.assertEqual({k: v for k, v in self.w.dep_idx.items() if k[1] in ("ok", "bad", "again", "far")},
                         {("pa", "ok"): "done", ("pa", "bad"): "failed", ("pa", "again"): "pending", ("pb", "far"): "done"})
        self.assertEqual(self.w.dead, {("pa", "bad")})
        blk = {n: self.w.dep_block(dict(h, project="pa", name=n), now) for n, h in self.w.qlists[qs[1]["key"]][1]}
        self.assertIsNone(blk["w1"])
        self.assertEqual(blk["w2"][0], "failed")
        self.assertIn("等的 bad 跑失败了", blk["w2"][1])
        self.assertIsNone(blk["w3"])                                                  # afterany= 不管成败
        self.assertEqual(blk["w4"], ("wait", "等 again 跑完"))                       # 还会跑、推演还没算出何时结束
        self.assertEqual(blk["w5"], ("wait", "等 ghost（%s）" % self.m.MISS_NOTE))       # 找不到：满 DEP_MISS_S 才报（#56）
        w5 = dict(self.w.qlists[qs[1]["key"]][1][4][1], project="pa", name="w5")
        self.assertEqual(self.w.dep_block(w5, now + self.m.DEP_MISS_S - 1)[0], "wait")
        self.assertEqual(self.w.dep_block(w5, now + self.m.DEP_MISS_S)[0], "missing")
        self.assertIn("找不到等的 ghost", self.w.dep_block(w5, now + self.m.DEP_MISS_S)[1])
        self.assertIsNone(blk["w6"])
        self.w.ends[("pa", "again")] = now + 3600
        self.assertEqual(self.w.dep_block(dict(self.w.qlists[qs[1]["key"]][1][3][1], project="pa", name="w4"), now),
                         ("wait", "等 again 跑完（预计 %s）" % self.m.now_str(now + 3600)[5:16]))

    def test_tick_holds_back_until_deps_done(self):
        # b1 等 a1：a1 在跑时 b1 不放行、不排位，等待项标 dep；a1 跑完（.done）后下一轮放行。c1 等的不存在：标 missing，记 dep 事件
        qs = self.hosts.queues({"a": ["a1|sleep 1   # gpus=0 est_min=1"], "b": ["b1|sleep 30   # gpus=0 after=a1"], "c": ["c1|true   # gpus=0 after=nope"]}, "pa")
        try:
            self.w.tick(qs, True)
            st = json.loads(Path(self.w.p.status).read_text())
            self.assertEqual({j["name"] for j in self.w.running.values()}, {"a1"})
            w = {x["name"]: x for x in st["waiting"]}
            self.assertEqual((w["b1"]["dep"], w["b1"]["pos"], w["b1"]["why"]), ("wait", None, w["b1"]["why"]))
            self.assertTrue(w["b1"]["why"].startswith("等 a1 跑完"))
            self.assertEqual(w["c1"]["dep"], "wait")                       # 找不到：满 DEP_MISS_S 才报
            self.assertEqual(self.events("dep"), [])
            plan = {j["name"]: j for ln in st["plan"]["lanes"] for j in ln["jobs"]}
            self.assertEqual(plan["b1"]["deps"], [["pa", "a1"]])
            j, t = next(iter(self.w.running.values())), time.time()
            while self.w.alive_job(j) and time.time() - t < 10:
                time.sleep(0.2)
            self.hosts.advance(self.m.DEP_MISS_S)
            self.w.tick(qs, False)                          # 先收尾 a1（.done），同一轮里 b1 的依赖就满足了；c1 满时限报找不到、记 dep 事件
            self.assertIn("b1", {j["name"] for j in self.w.running.values()})
            st = json.loads(Path(self.w.p.status).read_text())
            self.assertEqual({x["name"]: x["dep"] for x in st["waiting"]}["c1"], "missing")
            self.assertEqual([e["name"] for e in self.events("dep")], ["c1"])
            self.assertTrue(Path(qs[0]["state"], "a1.done").exists())
        finally:
            for j in self.w.running.values():
                os.killpg(j["pid"], 9)

    def test_forecast_gates_and_holds(self):
        # 本机：b1、hold_local 都等 a2（a2 排在 a1 后面，第一遍里晚于它们才推演到，等它排上再放）；n1 接在 hold_local 后面。
        # 别的主机：hold_peer 等 pb 在 o 上的 b9（那台 plan 里 5 小时后结束）。已跑完：old_ok 有 rc=0 的用量记录，old_mark 有 .done。
        # hold_ghost 等的哪里都找不到：排不上。运行中的 hold_run 也等 a2：按第二遍算预计结束
        now = time.time()
        self.cards(40000)
        qs = self.hosts.queues({"a": ["a1|true   # vram=20 est_min=60", "old_mark|true", "a2|true   # vram=20 est_min=30"],
                                "h1": ["hold_local|sleep 1   # est_min=900 after=a2", "n1|true   # gpus=0 est_min=10"],
                                "h2": ["hold_peer|sleep 1   # est_min=900 after=pb/b9"],
                                "h3": ["hold_done|sleep 1   # est_min=900 after=old_ok,pa/old_mark"],
                                "h4": ["hold_ghost|sleep 1   # est_min=900 after=ghost"],
                                "h5": ["hold_run|sleep 1   # est_min=900 after=a2"],
                                "h6": ["b1|true   # vram=10 est_min=20 after=a2"]}, "pa")
        os.makedirs(qs[0]["state"])
        Path(qs[0]["state"], "old_mark.done").write_text("")
        self.w.hist = [{"project": "pa", "name": "old_ok", "rc": "0", "t0": now - 7200, "t1": now - 3600, "ram_peak": 1.0}]
        self.hosts.status("o", plan={"t": now, "lanes": [{"project": "pb", "jobs": [{"name": "b9", "t0": now, "t1": now + 5 * 3600}]}]},
                          todo=[{"project": "pb", "jobs": ["b9"], "deps": {}, "held": None}])
        self.w.running["h5"] = {"key": "h5", "kind": "queue", "name": "hold_run", "project": "pa", "t0": now - 600, "deps": [], "deps_any": [],
                                "gpus": [], "use": {}, "est": None, "excl": None, "tag": None}
        _, reqs = self.w.requests(qs, False)
        self.w.queue_durs(qs)
        self.w.dep_state(qs, now)
        self.w.dep_miss[("pa", "ghost")] = now - self.m.DEP_MISS_S       # 找不到已满时限
        for r in reqs:
            self.w.prep(r, now, {})
        dep = {r["name"]: (r["dep"] or [None])[0] for r in reqs}
        self.assertEqual({k: dep[k] for k in ("hold_local", "hold_peer", "hold_done", "hold_ghost", "b1")},
                         {"hold_local": "wait", "hold_peer": "wait", "hold_done": None, "hold_ghost": "missing", "b1": "wait"})
        self.w.forecast(qs, reqs, {}, now, self.m.POLICY)
        plan = {j["name"]: j for ln in self.w.plan["lanes"] for j in ln["jobs"]}
        stuck = {os.path.basename(ln["root"]): ln["stuck"] for ln in self.w.plan["lanes"] if ln.get("stuck")}
        self.assertAlmostEqual(plan["a2"]["t1"], now + 90 * 60, delta=2)
        self.assertAlmostEqual(plan["b1"]["t0"], now + 90 * 60, delta=2)                      # 等依赖满足才排，不按资源提前放
        self.assertEqual(plan["b1"]["deps"], [["pa", "a2"]])
        self.assertAlmostEqual(plan["hold_local"]["t0"], now + 90 * 60, delta=2)
        self.assertAlmostEqual(plan["hold_local"]["t1"], now + 90 * 60, delta=2)              # 依赖满足就放行
        self.assertAlmostEqual(plan["n1"]["t0"], now + 90 * 60, delta=2)
        self.assertAlmostEqual(plan["hold_peer"]["t1"], now + 5 * 3600, delta=2)
        self.assertEqual(plan["hold_peer"]["deps"], [["pb", "b9"]])
        self.assertAlmostEqual(plan["hold_done"]["t1"], now, delta=2)
        self.assertNotIn("hold_ghost", plan)
        self.assertEqual(stuck["h4"], "hold_ghost 等的 ghost 跑不成或不知道何时跑完")
        self.assertTrue(plan["hold_run"]["run"])
        self.assertAlmostEqual(plan["hold_run"]["t1"], now + 90 * 60, delta=2)                  # 运行中的也按第二遍
        self.assertEqual(plan["hold_run"]["deps"], [["pa", "a2"]])
        self.assertNotIn("deps", plan["a2"])
        self.assertAlmostEqual(self.w.run_end(self.w.running["h5"], now), now + 90 * 60, delta=2)
        self.w.ends[("pa", "a2")] = 0                                                              # a2 跑完了、它还没放行：算现在，不按超时往后推
        self.assertEqual(self.w.run_end(self.w.running["h5"], now + 10), now + 10)

    def blocked(self, qs, now=None):
        """各清单队首的 dep_block：{任务: (状态, 原因) 或 None}。"""
        now = now or time.time()
        self.w.queue_durs(qs)
        self.w.dep_state(qs, now)
        return {r["name"]: self.w.dep_block(r, now) for r in self.w.requests(qs, False)[1]}

    def test_list_state(self):
        # 本机清单的状态（写进状态的 todo）：在跑的排最前、其余按清单顺序，标完的不列；只列写了依赖的；暂停的清单照样列、标 held
        qs = self.hosts.queues({"a": ["a0|true", "a1|true", "a2|true   # after=a9,pb/x afterany=a8"], "b": ["b1|true"]}, "pa")
        for q, f in ((qs[0], "a0.done"), (qs[1], "paused")):
            os.makedirs(q["state"], exist_ok=True)
            Path(q["state"], f).write_text("")
        self.w.running[qs[0]["key"]] = {"key": qs[0]["key"], "kind": "queue", "name": "a2", "project": "pa", "t0": time.time(), "deps": [], "deps_any": []}
        self.w.queue_durs(qs)
        self.w.list_state(qs)
        self.assertEqual(self.w.todo, [{"project": "pa", "root": qs[0]["root"], "queue": "jobs/q.txt", "jobs": ["a2", "a1"], "deps": {}, "gang": {}, "held": None},
                                       {"project": "pa", "root": qs[1]["root"], "queue": "jobs/q.txt", "jobs": ["b1"], "deps": {}, "gang": {}, "held": "paused"}])
        self.assertEqual(self.w.marks, {("pa", "a0"): "done"})
        del self.w.running[qs[0]["key"]]
        self.w.list_state(qs)
        self.assertEqual(self.w.todo[0]["deps"], {"a2": [["pa", "a9"], ["pb", "x"], ["pa", "a8"]]})

    def test_peer_todo(self):
        # 别的主机上被等的任务按那台状态里的 todo 认还会跑：排在时长不明的任务后面、所在清单被挡住的都看得到，
        # 以前跑成功过的（删了 .done 重跑）接着等；那台写状态之后才记下的用量记录说了算
        qs = self.hosts.queues({"b": ["B|true   # gpus=0 after=pb/A"]}, "pa")
        now = time.time()
        self.w.hist = [{"project": "pb", "name": "A", "rc": "0", "t1": now - 86400}]
        self.hosts.status("o", todo=[{"project": "pb", "jobs": ["x1", "A"], "deps": {}, "held": None}])
        self.assertEqual(self.blocked(qs)["B"], ("wait", "等 pb/A 跑完"))
        self.w.hist.append({"project": "pb", "name": "A", "rc": "0", "t1": now + 5})
        self.assertIsNone(self.blocked(qs)["B"])
        self.w.hist.pop()
        self.hosts.status("o", todo=[{"project": "pb", "jobs": ["x1", "A"], "deps": {}, "held": "drain"}])     # 所在主机 drain：写清
        self.assertEqual(self.blocked(qs)["B"], ("wait", "等 pb/A 跑完；pb/A 所在主机 drain 中"))
        self.hosts.status("o", todo=[{"project": "pb", "jobs": ["A"], "deps": {}, "held": None}], age=600)     # 那台 worker 不在（续租重启）：照样等
        self.assertEqual(self.blocked(qs)["B"], ("wait", "等 pb/A 跑完；pb/A 在 o 上，那台 worker 已 10 分钟没更新"))
        self.hosts.status("o", todo=[{"project": "pb", "jobs": ["A"], "deps": {}, "held": None}], age=self.m.DEP_STALE_S + 60)
        self.assertIsNone(self.blocked(qs)["B"])                                                  # 不在太久：退回用量记录（跑成功过）
        self.w.hist = []
        t = time.time()
        self.assertEqual(self.blocked(qs, t)["B"][0], "wait")                                     # 找不到：满 DEP_MISS_S 才报
        self.assertEqual(self.blocked(qs, t + self.m.DEP_MISS_S)["B"][0], "missing")
        self.hosts.status("o", todo=[{"project": "pb", "jobs": ["A"], "deps": {}, "held": None}], stopped=True)   # worker stop 过（归还）：不算
        self.assertEqual(self.blocked(qs, t + self.m.DEP_MISS_S)["B"][0], "missing")

    def test_last_outcome(self):
        # 结果看最后一次：本机清单有这一行的按标记（跑成功过、重跑失败了算失败），清单里没有的看用量记录的最后一次；
        # 几条清单说法不一的看用量记录，没有记录算失败；几秒内就失败、没量到用量的记录也读回来
        qs = self.hosts.queues({"a": ["A|true"], "c": ["C|true"], "c2": ["C|true"],
                                "b": ["B1|true   # after=A", "B2|true   # after=pb/far", "B3|true   # after=C"]}, "pa")
        for q, f in ((qs[0], "A.failed"), (qs[1], "C.done"), (qs[2], "C.failed")):
            os.makedirs(q["state"], exist_ok=True)
            Path(q["state"], f).write_text("")
        self.hosts.usage("o", [{"project": "pa", "name": "A", "rc": "0", "t1": 1, "ram_peak": 1.0}, {"project": "pb", "name": "far", "rc": "0", "t1": 2, "ram_peak": 1.0},
                               {"project": "pb", "name": "far", "rc": "1", "t1": 3, "ram_peak": None}])
        self.w.reload_hist()
        self.assertEqual(len(self.w.hist), 3)
        self.assertEqual(self.m.estimate(self.w.hist, {"project": "pb", "name": "far", "tag": None, "ram": 2.0, "vram": 0.0})["src"], "同名 近 1 次峰值 ×1.1")
        self.w.queue_durs(qs)
        self.w.dep_state(qs, time.time())
        self.assertEqual({k[1]: self.w.dep_idx[k] for k in (("pa", "A"), ("pb", "far"), ("pa", "C"))}, {"A": "failed", "far": "failed", "C": "failed"})
        self.w.hist.append({"project": "pa", "name": "C", "rc": "0", "t1": 4})
        self.w.dep_state(qs, time.time())
        self.assertEqual(self.w.dep_idx[("pa", "C")], "done")

    def test_dep_cycle(self):
        # 清单顺序也算依赖：等排在自己后面的、互相等的（跨项目、跨主机）、等自己的都标 cycle，原因写出这一圈；tick 记 dep 事件
        qs = self.hosts.queues({"a": ["B|true   # gpus=0 after=A", "A|true   # gpus=0"], "c": ["C|true   # gpus=0 after=pb/D"],
                                "s": ["S|true   # gpus=0 after=S"], "e": ["E|true   # gpus=0 after=pb/F"]}, "pa")
        qs += self.hosts.queues({"d": ["D|true   # gpus=0 after=pa/C"]}, "pb")
        self.hosts.status("o", todo=[{"project": "pb", "jobs": ["X", "F"], "deps": {"X": [["pa", "E"]]}, "held": None}])
        blk = self.blocked(qs)
        why = "依赖成环，谁也轮不到：%s。改清单顺序或 after= 后下一轮生效"
        self.assertEqual(blk["B"], ("cycle", why % "B 等 A，A 排在 B 后面"))
        self.assertEqual(blk["C"], ("cycle", why % "C 等 pb/D，pb/D 等 C"))
        self.assertEqual(blk["D"], ("cycle", why % "D 等 pa/C，pa/C 等 D"))
        self.assertEqual(blk["S"], ("cycle", why % "S 等它自己"))
        self.assertEqual(blk["E"], ("cycle", why % "E 等 pb/F，pb/F 排在 pb/X 后面，pb/X 等 E"))
        self.w.tick(qs, True)
        st = json.loads(Path(self.w.p.status).read_text())
        self.assertEqual({w["name"]: w["dep"] for w in st["waiting"]}, {n: "cycle" for n in "BCDSE"})
        self.assertEqual(sorted(e["name"] for e in self.events("dep")), list("BCDES"))
        self.assertEqual({k: v for k, v in st["todo"][0].items() if k not in ("root", "queue")}, {"project": "pa", "jobs": ["B", "A"], "deps": {"B": [["pa", "A"]]}, "gang": {}, "held": None})


class CrossHostTest(UnitBase):
    """两台 worker 共享一个 HOME（fixtures.Hosts）：一台 tick 写出的状态与用量记录，另一台按共享盘上的真实路径读；推进时钟看过期。"""

    def waiting(self, w):
        """一台 worker 写出的状态里等待中的任务：{任务: (dep, why)}。"""
        st = json.loads(Path(w.p.status).read_text())
        return {x["name"]: (x.get("dep"), x["why"]) for x in st["waiting"]}

    def test_estimate_queue_history_from_other_host(self):
        # #61：同一条清单的历史记在别的主机的 usage.jsonl 里（load_hist 一起读）：写了提示值的行照样按提示值估，没写的按那台的同队列历史；
        # 别的主机上同 tag 的实测盖过提示值
        now = self.hosts.now()
        self.hosts.usage("o", [usage_row("old", "pa", now - 900, now - 600, root=str(self.d / "s"), tag="sm_train", gpus=["GPU-a"],
                                         ram_peak=1.0, vram_peak=6000.0)])
        q, = self.hosts.queues({"s": ["vit_a|true   # vram=24 ram=12 tag=sm_vit", "plot|true"]}, "pa")
        os.makedirs(q["state"])
        self.w.reload_hist()

        def est(name):
            r = self.w.prep(self.m.queue_req(q, self.m.job_of(name, self.m.job_cmd(q, name))), now, {})
            return r["est"]["vram"], r["est"]["src"], r["est"]["ram"], r["est"]["ram_src"]
        self.assertEqual(est("vit_a"), (24.0, "提示值", 12.0, "提示值"))
        self.assertEqual(est("plot"), (6.45, "同队列 近 1 次峰值 ×1.1", 1.1, "同队列 近 1 次峰值 ×1.1"))
        self.hosts.usage("o", [usage_row("vit_z", "pa", now - 600, now - 300, tag="sm_vit", gpus=["GPU-a"], ram_peak=10.0, vram_peak=16000.0)])
        self.w.reload_hist()
        self.assertEqual(est("vit_a"), (17.19, "同 tag 近 1 次峰值 ×1.1", 11.0, "同 tag 近 1 次峰值 ×1.1"))

    def test_oom_on_other_host_raises_estimate(self):
        # #62：同类在别的主机上 OOM（记在那台的 usage.jsonl）：本机同类按下限估；那台随后跑成功一次（同样记在那台）就回到按实测
        now = self.hosts.now()
        q, = self.hosts.queues({"s": ["vit_a|true   # vram=8 tag=sm_vit"]}, "pa")
        os.makedirs(q["state"])
        self.hosts.usage("o", [usage_row("vit_z", "pa", now - 900, now - 840, tag="sm_vit", rc="1", oom="vram", gpus=["GPU-a"], est={"vram": 8.0, "ram": 0.0})])
        self.w.reload_hist()

        def est():
            r = self.w.prep(self.m.queue_req(q, self.m.job_of("vit_a", self.m.job_cmd(q, "vit_a"))), now, {})
            return r["est"]["vram"], r["est"]["src"]
        self.assertEqual(est(), (16.0, "同 tag 上次 OOM：放行时估 8.0 GiB ×2"))
        self.hosts.usage("o", [usage_row("vit_y", "pa", now - 600, now - 300, tag="sm_vit", gpus=["GPU-a"], ram_peak=2.0, vram_peak=15000.0)])
        self.w.reload_hist()
        self.assertEqual(est(), (16.11, "同 tag 近 1 次峰值 ×1.1"))

    def test_global_pause_reaches_later_host(self):
        # #96：项目级暂停由看板随 sched.json 写给各在用主机：后来加入的 o 拿到同一份就挡住，todo 的 held 写 pause（offers 不当可挪的活）、
        # 等待原因写暂停；t 上撤了（sched.json 不再带）下一轮照常放行。以前逐台下指令，之后加入的主机上没有
        sched = json.dumps({"pause": {"pa": None}})
        Path(self.w.p.sched).write_text(sched)
        o = self.hosts.worker("o")
        Path(o.p.sched).write_text(sched)
        qo = self.hosts.queues({"d": ["D|true   # gpus=0"]}, "pa")
        o.tick(qo, True)
        self.assertEqual(self.waiting(o)["D"][1], "项目 pa 暂停中（暂停 pa，无期限，所有主机）")
        self.assertEqual([ln["held"] for ln in json.loads(Path(o.p.status).read_text())["todo"] if ln["root"]], ["pause"])
        Path(o.p.sched).write_text("{}")
        o.tick(qo, False)
        self.assertNotIn("D", self.waiting(o))

    def test_dep_cycle_across_workers(self):
        # t 上 pa/C 等 pb/D，o 上 pb/D 等 pa/C：各自 tick，读到对方写的 todo 后两边都标成环；
        # o 不再更新（续租重启）时 t 照样认它的 todo，过了 DEP_STALE_S 才不认，C 改报找不到
        o = self.hosts.worker("o")
        qt = self.hosts.queues({"c": ["C|true   # gpus=0 after=pb/D"]}, "pa")
        qo = self.hosts.queues({"d": ["D|true   # gpus=0 after=pa/C"]}, "pb")
        self.w.tick(qt, True)                                           # t 先写：这时还没有 o 的状态，D 还没见到，DEP_MISS_S 内先当在等（#56）
        self.assertEqual(self.waiting(self.w)["C"], ("wait", "等 pb/D（%s）" % self.m.MISS_NOTE))
        o.tick(qo, True)
        why = "依赖成环，谁也轮不到：%s。改清单顺序或 after= 后下一轮生效"
        self.assertEqual(self.waiting(o)["D"], ("cycle", why % "D 等 pa/C，pa/C 等 D"))
        self.hosts.advance(self.m.PEER_EVERY)                           # 到点重读别的主机
        self.w.tick(qt, False)
        self.assertEqual(self.waiting(self.w)["C"], ("cycle", why % "C 等 pb/D，pb/D 等 C"))
        self.hosts.advance(self.m.STATUS_STALE_S + 60)                  # o 不更新了：todo 照样算
        self.w.tick(qt, False)
        self.assertEqual(self.waiting(self.w)["C"][0], "cycle")
        self.hosts.advance(self.m.DEP_STALE_S)                          # 不在太久：不再认它的 todo，也没有用量记录
        self.w.tick(qt, False)
        self.assertEqual(self.waiting(self.w)["C"][0], "wait")          # 从这时起算找不到，满 DEP_MISS_S 才报
        self.hosts.advance(self.m.DEP_MISS_S)
        self.w.tick(qt, False)
        self.assertEqual(self.waiting(self.w)["C"][0], "missing")
        self.assertEqual([e["name"] for e in self.events("dep")], ["C", "C"])     # 成环、找不到：原因的类别变了才记，时限内的在等不记 dep

    def test_dep_released_by_other_worker(self):
        # o 上 pb/A 所在清单暂停：t 上等它的 B 不放行，原因写明；o 恢复后跑完 A（.done 与 o 的用量记录），t 下一轮放行 B
        o = self.hosts.worker("o")
        qt = self.hosts.queues({"b": ["B|sleep 30   # gpus=0 after=pb/A"]}, "pa")
        qa, = self.hosts.queues({"a": ["A|true   # gpus=0 est_min=1"]}, "pb")
        os.makedirs(qa["state"])
        Path(qa["state"], "paused").write_text("")
        try:
            o.tick([qa], True)
            self.w.tick(qt, True)
            self.assertEqual(self.waiting(self.w)["B"], ("wait", "等 pb/A 跑完；pb/A 所在清单暂停中"))
            os.remove(Path(qa["state"], "paused"))
            o.tick([qa], False)
            j, t = next(iter(o.running.values())), time.time()
            while o.alive_job(j) and time.time() - t < 10:
                time.sleep(0.1)
            o.tick([qa], False)                                         # 收尾：A.done，用量记录写进 o 的 usage.jsonl
            self.assertTrue(Path(qa["state"], "A.done").exists())
            self.hosts.advance(self.m.PEER_EVERY)
            self.w.tick(qt, False)
            self.assertEqual({j["name"] for j in self.w.running.values()}, {"B"})
        finally:
            for w in (o, self.w):
                for j in w.running.values():
                    os.killpg(j["pid"], 9)


    def test_dep_on_job_just_started_elsewhere(self):
        # Issue #56：两边清单同时加行，o 先认出自己清单里的 A 并开跑、写进状态的 todo；t 隔几十秒才认出等 A 的 B，
        # 这时 t 读别的主机状态的缓存（peers，PEER_EVERY 秒一次）还是 A 加进来之前的：B 不该报找不到
        o = self.hosts.worker("o")
        qa, = self.hosts.queues({"a": ["# 还没有任务"]}, "pa")
        qz, = self.hosts.queues({"z": ["# 还没有任务"]}, "pa")
        try:
            o.tick([qa], True)
            self.w.tick([qz], True)                                     # t 读了一次 o 的状态：这时 o 还没有 A
            self.hosts.advance(5)
            Path(qa["jobs_path"]).write_text("A|sleep 60   # gpus=0 est_min=30\n")
            qb, = self.hosts.queues({"b": ["B|true   # gpus=0 after=A"]}, "pa")
            o.tick([qa], False)                                         # o 认出 A、开跑，状态的 todo 里有它
            self.assertEqual({j["name"] for j in o.running.values()}, {"A"})
            self.hosts.advance(25)                                      # 没到 PEER_EVERY
            self.w.tick([qz, qb], False)
            self.assertEqual(self.waiting(self.w)["B"][0], "wait")
            self.assertEqual(self.events("dep"), [])
            self.hosts.advance(self.m.PEER_EVERY)                       # 重读后认出 A 在 o 上跑着
            self.w.tick([qz, qb], False)
            self.assertEqual(self.waiting(self.w)["B"][1][:7], "等 A 跑完")
            self.assertEqual(self.w.dep_miss, {})
        finally:
            for j in o.running.values():
                os.killpg(j["pid"], 9)

    def test_dep_seen_before_other_host_reads_its_list(self):
        # 反过来的顺序（#56）：t 先认出等 A 的 B，o 还没认出自己清单里新加的 A：时限内照“依赖没满足”挡着（不排位、不算等待），
        # 不记 dep 事件；o 认出后 t 到点重读就改成等 A 跑完
        o = self.hosts.worker("o")
        qa, = self.hosts.queues({"a": ["# 还没有任务"]}, "pa")
        o.tick([qa], True)
        qb, = self.hosts.queues({"b": ["B|true   # gpus=0 after=A"]}, "pa")
        self.w.tick([qb], True)
        st = json.loads(Path(self.w.p.status).read_text())
        w, = st["waiting"]
        self.assertEqual((w["dep"], w["pos"], w["why"]), ("wait", None, "等 A（%s）" % self.m.MISS_NOTE))
        self.assertEqual(self.w.running, {})
        self.assertEqual(self.events("dep"), [])
        Path(qa["jobs_path"]).write_text("A|true   # gpus=1 est_min=30\n")     # 要卡、o 没卡：在排
        self.hosts.advance(5)
        o.tick([qa], False)
        self.hosts.advance(self.m.PEER_EVERY)
        self.w.tick([qb], False)
        self.assertEqual(self.waiting(self.w)["B"], ("wait", "等 A 跑完"))
        self.hosts.advance(self.m.DEP_MISS_S)                           # 之前的找不到不再算：不会到点报 missing
        self.w.tick([qb], False)
        self.assertEqual(self.waiting(self.w)["B"][0], "wait")
        self.assertEqual(self.events("dep"), [])

    def test_dep_missing_reported_after_grace(self):
        # 名字真写错：满 DEP_MISS_S 才报找不到（状态 dep 为 missing、记 dep 事件，看板据此告警）；
        # 起算时刻写进状态的 dep_miss，worker 重启接管时从上一份状态找回（状态没过期才认），不从头再等
        qb, = self.hosts.queues({"b": ["B|true   # gpus=0 after=nope"]}, "pa")
        t0 = self.hosts.now()
        self.w.tick([qb], True)
        self.assertEqual(self.waiting(self.w)["B"], ("wait", "等 nope（%s）" % self.m.MISS_NOTE))
        (p, n, t), = json.loads(Path(self.w.p.status).read_text())["dep_miss"]
        self.assertEqual((p, n), ("pa", "nope"))
        self.assertTrue(t0 - 1 <= t <= self.hosts.now())                 # tick 里取的时刻，往下取整
        self.hosts.advance(self.m.DEP_MISS_S - 1)
        self.w.tick([qb], False)
        self.assertEqual(self.waiting(self.w)["B"][0], "wait")
        self.assertEqual(self.events("dep"), [])
        w2 = self.hosts.worker("t")                                     # 重启接管：起算时刻沿用
        self.assertEqual(w2.dep_miss, {("pa", "nope"): t})
        self.hosts.advance(1)
        w2.tick([qb], True)
        self.assertEqual(self.waiting(w2)["B"], ("missing", "找不到等的 nope（任务名写错了，或它所在的清单没登记）：改对 after= 后下一轮生效"))
        self.assertEqual([e["name"] for e in self.events("dep")], ["B"])
        self.hosts.advance(self.m.STATUS_STALE_S + 1)                   # 停了太久才重启：从头计时
        self.assertEqual(self.hosts.worker("t").dep_miss, {})
        Path(qb["jobs_path"]).write_text("B|true   # gpus=0\n")          # 删掉 after=：不再记
        w2.tick([qb], False)
        self.assertEqual(json.loads(Path(w2.p.status).read_text())["dep_miss"], [])


class RegDupTest(UnitBase):
    """同一条清单登记在两台（#88）：借来的主机还了再借回、自用主机恢复时，旧登记还在那台。两台共享 HOME（fixtures.Hosts）；
    o 的状态是假的（hosts.status），它容器里的任务 PID 在 t 这边看不到（dead_pid）。回来那台不能把 o 手上的任务当中断、删 current、重跑。"""

    def setUp(self):
        super().setUp()
        self.q, = self.hosts.queues({"s": ["j1|sleep 30   # gpus=0", "j2|sleep 30   # gpus=0"]}, "pa")
        os.makedirs(self.q["state"])
        self.fo, self.ft = self.hosts.register("o", "s", "pa"), self.hosts.register("t", "s", "pa")
        self.newer(self.ft)                       # t 的登记更晚：o 在用时 t 照样跑这条（registry 认更晚的那份，#95）

    def newer(self, f, s=10):
        st = os.stat(f)
        os.utime(f, (st.st_atime, st.st_mtime + s))

    def add(self, host, *extra):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", out):
            rc = self.m.main(["add", self.q["root"], "jobs/q.txt", "--host", host, "--project", "pa", *extra])
        return rc, out.getvalue()

    # ---- 搬登记（#95）----
    def test_add_moves_registration(self):
        # worker add 把别的主机（含已归还的，那边删不掉）上同一条清单的登记搬过来，两边各记一条事件；o 正在跑的跑完后 t 接着跑
        self.ft.unlink()
        self.o_runs(who="o")
        rc, out = self.add("t")
        self.assertEqual(rc, 0, out)
        self.assertIn("从主机 o 搬过来，那边不再跑它；主机 o 上正在跑的 j1 跑完后这台接着跑", out)
        self.assertFalse(self.fo.exists())
        self.assertEqual(len(list((self.hosts.home / "hosts" / "t" / "queues").glob("*.json"))), 1)
        o_ev = [json.loads(l) for l in (self.hosts.home / "hosts" / "o" / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["msg"] for e in o_ev], ["登记搬到主机 t：%s/jobs/q.txt，这台不再跑它" % self.q["root"]])
        self.assertEqual([e["msg"] for e in self.events("worker")], ["登记从主机 o 搬来：%s/jobs/q.txt" % self.q["root"]])
        self.w.tick(self.w.registry(), True)       # 搬来后 o 手上的 j1 还没跑完：t 等它，不重跑
        self.assertEqual(self.w.running, {})
        self.assertEqual(self.events("interrupt"), [])

    def test_guarded_add_and_rm_reject_stale_project(self):
        # 看板采到的主人已过期时，worker 在登记锁内按当前文件核对。
        reg = json.loads(self.fo.read_text())
        self.fo.write_text(json.dumps({**reg, "project": "pb"}))
        self.ft.unlink()
        rc, out = self.add("t", "--expect-project", "pa")
        self.assertEqual(rc, 2, out)
        self.assertIn("登记归属已变", out)
        self.assertFalse(self.ft.exists())
        alias = self.hosts.home / "alias"
        alias.symlink_to(self.q["root"], target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.m.main(["add", str(alias), "jobs/q.txt", "--host", "t", "--project", "pa", "--expect-project", "pa"])
        self.assertEqual(rc, 2, err.getvalue())
        self.assertIn("登记归属已变", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.m.main(["rm", self.q["root"], "jobs/q.txt", "--host", "o", "--expect-project", "pa"])
        self.assertEqual(rc, 2, err.getvalue())
        self.assertTrue(self.fo.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.m.main(["rm", self.q["root"], "jobs/q.txt", "--host", "o", "--expect-project", "pb"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.fo.exists())

    def test_concurrent_add_keeps_later(self):
        # 两个会话同时登记到两台：o 的那份比这次晚，这次让给它（删掉刚写的、退出码 2），不会互相删光
        self.ft.unlink()
        self.newer(self.fo, 100)
        rc, out = self.add("t")
        self.assertEqual(rc, 2)
        self.assertIn("主机 o 上刚登记了这条清单（比这次晚），以那边为准，没有登记到 t", out)
        self.assertTrue(self.fo.exists())
        self.assertEqual(list((self.hosts.home / "hosts" / "t" / "queues").glob("*.json")), [])

    def test_concurrent_add_interleaved_keeps_one(self):
        # Codex 审查 #120：t 上的 add 已判定 o 的登记较旧、还没删时，o 上的 add 插进来（写了更晚的 o、删了 t 的）；
        # 没有锁时 t 接着按过期的比较删掉 o 的新登记，两台都没了。现在整段在共享盘的一把锁里：o 的 add 等 t 做完再比，最后只留 o 的
        import threading
        self.ft.unlink()
        real, th = self.m.read_current, []

        def hook(st):
            if not th:                               # t 的 add 比完、删之前：o 上的 add 插进来（另一个会话，用线程模拟）
                os.utime(next((self.hosts.home / "hosts" / "t" / "queues").glob("*.json")), (time.time() - 5, time.time() - 5))
                th.append(threading.Thread(target=self.m.main, args=(["add", self.q["root"], "jobs/q.txt", "--host", "o", "--project", "pa"],)))
                th[0].start()
                th[0].join(1)
            return real(st)
        with mock.patch.object(self.m, "read_current", hook):
            rc, out = self.add("t")
        th[0].join(10)
        self.assertEqual(rc, 0, out)
        left = {h: [f.name for f in (self.hosts.home / "hosts" / h / "queues").glob("*.json")] for h in ("o", "t")}
        self.assertEqual(sum(len(v) for v in left.values()), 1, left)
        self.assertEqual(left["t"], [], left)

    def test_move_during_launch_not_run_twice(self):
        # Codex 审查 #120：t 正在开跑 j1（核过登记、还没写 current）时，另一个会话把登记搬到 o、o 的 worker 接着 tick：
        # 没有锁时 o 看不到 current 就也放了 j1，两台同跑、t 再覆盖 current。现在 t 核归属到写 current 在登记归属的锁里，
        # 搬登记等 t 写完 current 才做，o 据此等 t 跑完
        import threading
        o, th = self.hosts.worker("o"), []
        self.fo.unlink()
        self.newer(self.ft, -60)                     # t 的登记早于这次搬：搬得动
        real, rc = self.w.qlog, []

        def other():
            rc.append(self.add("o")[0])
            o.tick(o.registry(), True)

        def hook(q, msg):
            if not th:                               # t 第一次放这条清单（“队列启动”）：核过归属、还没起进程
                th.append(threading.Thread(target=other))
                th[0].start()
                th[0].join(1)
            return real(q, msg)
        try:
            with mock.patch.object(self.w, "qlog", hook):
                self.w.tick(self.w.registry(), True)
            th[0].join(10)
            runs = {"t": [j["name"] for j in self.w.running.values()], "o": [j["name"] for j in o.running.values()]}
            self.assertEqual(rc, [0])
            self.assertEqual(runs, {"t": ["j1"], "o": []})
            self.assertEqual(self.m.read_current(self.q["state"])[3], "t")
        finally:
            for j in o.running.values():
                os.killpg(j["pid"], 9)

    def test_probe_trusts_current_from_other_host(self):
        # 清单刚搬到 t、o 还在跑 j1（current 写着 o）：t 上的探针不按 t 的 PID 判（那是 o 容器里的），按在跑算，进度照样读；
        # 放行的就是这台、PID 不在，照旧算没在跑；已归还的代读（readonly）不查
        pid = dead_pid()
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.hosts.now() - 60))
        Path(self.q["state"], "current").write_text(f"j1 {pid} {start} o")
        q = {"root": self.q["root"], "jobs": "jobs/q.txt"}
        self.assertTrue(P.queue_gpu_queue(dict(q, me="t"))["current"]["alive"])
        self.assertFalse(P.queue_gpu_queue(dict(q, me="o"))["current"]["alive"])
        self.assertFalse(P.queue_gpu_queue(dict(q, me="t", readonly=True))["current"]["alive"])

    def test_add_refuses_state_dir_used_on_other_host(self):
        # 别的主机上另一条清单用着同一个状态目录：同样不让登记（共享盘上 current、完成标记会互相覆盖）
        Path(self.q["root"], "jobs", "b.txt").write_text("x|true\n")
        self.hosts.register("o", "s", "pa").write_text(json.dumps({"root": self.q["root"], "jobs": "jobs/b.txt", "project": "pa"}))
        self.ft.unlink()
        rc, out = self.add("t")
        self.assertEqual(rc, 2)
        self.assertIn("已被主机 o 上登记的 %s/jobs/b.txt 用着" % self.q["root"], out)

    def test_older_registration_yields_while_newer_host_in_use(self):
        # o 的登记更晚、o 在用：t 不碰这条清单（不交给 tick，记一次事件）；o worker stop 后 t 接着跑，再记一次
        self.newer(self.fo, 100)
        self.hosts.status("o")
        self.assertEqual(self.w.registry(), [])
        self.assertEqual(self.w.registry(), [])
        ev = [e["msg"] for e in self.events("worker")]
        self.assertEqual(len(ev), 1)
        self.assertIn("也登记在主机 o、那边登记得更晚", ev[0])
        self.hosts.status("o", stopped=True)
        self.w.peer_t = 0
        self.assertEqual([q["root"] for q in self.w.registry()], [self.q["root"]])
        self.assertIn("不再被主机 o 的登记挡着", self.events("worker")[-1]["msg"])

    def test_broken_peer_registration_does_not_win(self):
        # o 上更晚的那份登记坏了（缺 project，o 自己也跳过它）：不算数，t 照常跑，不会两台都不跑
        self.fo.write_text(json.dumps({"root": self.q["root"], "jobs": "jobs/q.txt"}))
        self.newer(self.fo, 100)
        self.hosts.status("o")
        self.assertEqual([q["root"] for q in self.w.registry()], [self.q["root"]])

    def test_moved_away_mid_tick_not_launched(self):
        # t 这一轮已读了登记，放行前 worker add 把登记搬到 o：t 这次不放（start 再核一次登记），不会与 o 各跑一份
        qs = self.w.registry()
        self.ft.unlink()
        self.w.tick(qs, True)
        self.assertEqual(self.w.running, {})
        self.assertFalse(Path(self.q["state"], "current").exists())

    def test_restarted_worker_keeps_job_of_moved_list(self):
        # 清单已搬到 o，t 手上的 j1 还在跑时 t 的 worker 重启（升级）：新 worker 照样接管 j1、盯到跑完，o 据此（current 写着 t）等它，不重跑
        self.ft.unlink()
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "labmon-job:j1"], start_new_session=True)
        try:
            start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.hosts.now() - 60))
            Path(self.q["state"], "current").write_text(f"j1 {p.pid} {start} t")
            self.hosts.status("t", running=[running_job("j1", "pa", root=self.q["root"], queue="jobs/q.txt", pid=p.pid, stop="boundary")])
            w2 = self.hosts.worker("t")
            w2.tick(w2.registry(), True)
            self.assertEqual([j["name"] for j in w2.running.values()], ["j1"])
            self.assertEqual(self.m.run_elsewhere("o", self.q, self.m.read_current(self.q["state"]), set()), "t")
            Path(self.q["state"], "j1.rc").write_text("0\n")
            p.kill()
            p.wait()
            w2.tick(w2.registry(), False)           # 跑完由 t 自己收尾：.done、删 current，o 接着放
            self.assertTrue(Path(self.q["state"], "j1.done").exists())
            self.assertFalse(Path(self.q["state"], "current").exists())
            self.assertEqual(w2.running, {})
        finally:
            p.kill()
            p.wait()

    def test_replace_takes_job_of_last_old_tick(self):
        # Codex 审查 #120：run --replace 时新 worker 构造在先，旧 worker 退出前最后一轮又放了 j1、写了状态，随后登记搬到 o。
        # 新 worker 要按旧 worker 最终的状态接管 j1（只在构造时读的话漏掉它，跑完无人收尾，o 一直被 current 挡着）
        class Stop(Exception):
            pass
        w2 = self.hosts.worker("t")                # 构造时旧 worker 还没放 j1
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "labmon-job:j1"], start_new_session=True)
        sig = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        real = w2.tick

        def once(*a):
            real(*a)
            raise Stop
        try:
            start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.hosts.now() - 60))
            Path(self.q["state"], "current").write_text(f"j1 {p.pid} {start} t")
            self.hosts.status("t", running=[running_job("j1", "pa", root=self.q["root"], queue="jobs/q.txt", pid=p.pid, stop="boundary")])
            self.ft.unlink()                       # 登记搬到 o
            with mock.patch.object(w2, "tick", once), self.assertRaises(Stop):
                w2.run(replace=True)
            self.assertEqual([j["name"] for j in w2.running.values()], ["j1"])
        finally:
            for s, h in sig.items():
                signal.signal(s, h)
            p.kill()
            p.wait()

    def test_legacy_name_moved_back_tracked_once(self):
        # Codex 审查 #120：t 上不带目录哈希的旧名登记（11-06 前，如 s.json）在跑 j1 时搬到 o、又搬回 t（搬回来的用 qslug 命名）：
        # t 沿用手上那一项、改记到新名下，不按新名再接管一次（同一个任务盯两份，收尾时 .done 与 .failed 都有）
        self.fo.unlink()
        self.assertEqual(self.ft.name, "s.json")
        self.w.tick(self.w.registry(), True)
        self.assertEqual([(k, j["name"]) for k, j in self.w.running.items()], [("s", "j1")])
        self.newer(self.ft, -60)
        self.assertEqual(self.add("o")[0], 0)        # 搬到 o：t 手上的 j1 照样跑
        self.w.tick(self.w.registry(), False)
        fo, = Path(self.hosts.home, "hosts", "o", "queues").glob("*.json")
        self.newer(fo, -30)
        self.assertEqual(self.add("t")[0], 0)        # 又搬回 t
        self.w.tick(self.w.registry(), False)
        key = self.m.qslug(self.q["root"], "jobs/q.txt")
        self.assertEqual([(k, j["name"]) for k, j in self.w.running.items()], [(key, "j1")])
        pid = self.w.running[key]["pid"]
        Path(self.q["state"], "j1.rc").write_text("0\n")
        os.killpg(pid, 9)
        time.sleep(0.2)
        self.w.tick(self.w.registry(), False)
        self.assertTrue(Path(self.q["state"], "j1.done").exists())
        self.assertFalse(Path(self.q["state"], "j1.failed").exists())

    def o_runs(self, name="j1", listed=True, who=None, **kw):
        """o 上的 worker 放行了 name：current 写的是 o 容器里的 PID（t 看不到）；who 给了就照现在的 worker 写上放行的主机，否则照旧版只写四项。
        listed：o 的状态里列着它。"""
        pid = dead_pid()
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.hosts.now() - 300))
        Path(self.q["state"], "current").write_text(f"{name} {pid} {start}" + (f" {who}" if who else ""))
        self.hosts.status("o", running=[running_job(name, "pa", root=self.q["root"], queue="jobs/q.txt", pid=pid)] if listed else [], **kw)

    def why(self):
        return [w["why"] for w in json.loads(Path(self.w.p.status).read_text())["waiting"]]

    def test_comeback_does_not_rerun_job_running_elsewhere(self):
        # 旧格式的 current（没写主机）、o 的状态里列着 j1：t 第一轮不当 j1 中断、不删 current、不重跑；o 跑完（j1.done、current 收掉）后 t 接着放 j2
        self.o_runs()
        self.w.tick(self.w.registry(), True)
        self.assertTrue(Path(self.q["state"], "current").exists())
        self.assertEqual(self.w.running, {})
        self.assertEqual(self.events("interrupt"), [])
        self.assertIn("主机 o 放行的 j1 还没收尾", self.why()[0])
        Path(self.q["state"], "j1.done").write_text("")
        Path(self.q["state"], "current").unlink()
        self.hosts.status("o")
        self.w.tick(self.w.registry(), False)
        self.assertEqual([j["name"] for j in self.w.running.values()], ["j2"])

    def test_current_from_other_host_left_alone_until_returned(self):
        # current 写明是 o 放的：o worker stop 过、状态里也没列着、j1 已留下 .rc，t 照样不动（不接管、不收尾、不放行），事件只记一次；
        # 看板把 o 标成已归还（容器没了）以后，t 下一轮才接管，按 .rc 记完成，接着放 j2
        self.o_runs(who="o", listed=False, stopped=True)
        Path(self.q["state"], "j1.rc").write_text("0\n")
        self.w.tick(self.w.registry(), True)
        self.w.tick(self.w.registry(), False)
        self.assertEqual(self.w.running, {})
        self.assertTrue(Path(self.q["state"], "j1.rc").exists())
        self.assertFalse(Path(self.q["state"], "j1.done").exists())
        self.assertEqual([e["name"] for e in self.events("worker") if "放行的" in e["msg"]], ["j1"])
        self.assertIn("删掉状态目录里的 current", self.why()[0])
        Path(self.w.p.returned).write_text(json.dumps({"hosts": ["o"]}))
        self.w.peer_t = 0
        self.w.tick(self.w.registry(), False)
        self.assertTrue(Path(self.q["state"], "j1.done").exists())
        self.assertEqual([j["name"] for j in self.w.running.values()], ["j2"])
        self.assertEqual(self.m.read_current(self.q["state"])[3], "t")      # 现在的 current 写明放行的主机

    def test_leftover_current_from_returned_host_is_interrupt(self):
        # 看板说 o 已归还：它状态里列着的不算在跑，current 是容器没了留下的，照旧记中断、重跑
        self.o_runs(who="o")
        Path(self.w.p.returned).write_text(json.dumps({"hosts": ["o"]}))
        self.w.peer_t = 0
        self.w.tick(self.w.registry(), True)
        self.assertEqual([j["name"] for j in self.w.running.values()], ["j1"])
        self.assertEqual([e["name"] for e in self.events("interrupt")], ["j1"])

    def test_returned_host_pid_not_taken_as_local_process(self):
        # 已归还的 o 放的 j1 留下 .rc=0：这台恰好有同 PID、同名的进程也不当成它接管（别的容器的 PID 在这里没有意义），按 .rc 记完成
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "labmon-job:j1"], start_new_session=True)
        try:
            start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.hosts.now() - 300))
            Path(self.q["state"], "current").write_text(f"j1 {p.pid} {start} o")
            Path(self.q["state"], "j1.rc").write_text("0\n")
            Path(self.w.p.returned).write_text(json.dumps({"hosts": ["o"]}))
            self.w.peer_t = 0
            self.w.tick(self.w.registry(), True)
            self.assertTrue(Path(self.q["state"], "j1.done").exists())
            self.assertEqual([j["name"] for j in self.w.running.values()], ["j2"])
        finally:
            p.kill()
            p.wait()

    def test_own_leftover_current_still_interrupt(self):
        # current 是这台自己放的（容器重启后 PID 没了）：照旧记中断、重跑
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.hosts.now() - 300))
        Path(self.q["state"], "current").write_text(f"j1 {dead_pid()} {start} t")
        self.w.tick(self.w.registry(), True)
        self.assertEqual([e["name"] for e in self.events("interrupt")], ["j1"])
        self.assertEqual([j["name"] for j in self.w.running.values()], ["j1"])

    def test_start_refuses_when_other_host_just_launched(self):
        # 收请求之后、放行之前 o 已放了这条清单（两台都登记着）：t 这次不放，不会两台各跑一份
        start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.hosts.now()))
        r = self.w.prep(self.m.queue_req(self.q, self.m.next_job(self.q)), time.time(), {})
        Path(self.q["state"], "current").write_text(f"j1 {dead_pid()} {start} o")
        self.assertTrue(self.w.start(self.q, r, []))
        self.assertEqual(self.w.running, {})
        self.assertEqual(self.m.read_current(self.q["state"])[3], "o")

class OffersTest(UnitBase):
    """能接别处的任务（offers，Issue #20 第一档）：别的主机 o 上 pa 的清单排得很晚、本机 t 将空时，t 用自己的推演算出能接哪几行、几点跑完，
    看板（rebalance.suggest）汇成一条挪法建议。o 的状态是假的（hosts.status），清单是真文件；t 一张 46 GiB 的空卡。"""

    H = 3600

    def setUp(self):
        super().setUp()
        self.cards(46000.0, total=46000.0)
        self.now = self.hosts.now()

    def origin(self, lines, run=None, step=2, held=None, overrides=(), age=0, stopped=False, host="o", project="pa", name="a", extra_todo=(), in_plan=True, root=None):
        """o 上 project 的一条清单 lines（任务名 a0、a1…）：run 是在跑的那个预计还要几小时（None 为没有在跑的），其余每个 step 小时、
        依次排在推演里；held 为真时 o 的推演里没有这条清单（被临时指令挡住，等待项的 held 写它）；in_plan 为假时推演里也没有它、等待项又没有 held
        （读不了状态目录之类）；root 给了就把清单放在那个已有目录下（jobs/<name>.txt，同一种环境）。返回清单。"""
        if root:
            (self.d / root / "jobs" / (name + ".txt")).write_text("\n".join(lines) + "\n")
            q = self.m.load_queue({"_slug": name, "root": str(self.d / root), "jobs": "jobs/%s.txt" % name, "project": project})
        else:
            q, = self.hosts.queues({name: lines}, project)
        names = [ln.split("|")[0] for ln in lines]
        jobs, t, running, waiting = [], self.now, [], []
        if run is not None:
            t += run * self.H
            jobs.append({"name": names[0], "t0": round(self.now - 600), "t1": round(t), "gpus": [0], "run": True})
            running.append({"root": q["root"], "queue": q["jobs"], "name": names[0], "project": project})
        for n in names[1 if run is not None else 0:]:
            jobs.append({"name": n, "t0": round(t), "t1": round(t + step * self.H), "gpus": [0]})
            t += step * self.H
        if held:
            waiting.append({"kind": "queue", "queue": q["jobs"], "root": q["root"], "project": project, "name": names[len(running)], "why": "挡住了", "held": held})
        lanes = [] if held or not in_plan else [{"root": q["root"], "queue": q["jobs"], "project": project, "kind": "queue", "jobs": jobs, "stuck": None}]
        deps = {}
        for ln in lines:
            n, cmd = ln.split("|", 1)
            d = self.m.dep_keys(dict(self.m.hints(cmd), project=project))
            if d:
                deps[n] = [[x, y] for x, y, _ in d]
        todo = [{"project": project, "root": q["root"], "queue": q["jobs"], "jobs": names, "deps": deps, "held": held if held in ("paused", "pause", "drain") else None}]
        self.hosts.status(host, age=age, stopped=stopped, overrides=list(overrides), running=running, waiting=waiting, plan={"t": self.now, "lanes": lanes},
                          todo=todo + list(extra_todo),
                          queues=[{"root": q["root"], "jobs": q["jobs"], "gpus": None, "project": project, "stop": "boundary", "empty": False, "dur": {}}])
        return q

    def offer(self, queues=(), reqs=()):
        """t 照 tick 的次序算一遍本机清单与依赖的状态（dep_state，内含 peers）再推演（forecast 里算 offers），返回 {项目: offer}。"""
        self.w.peer_t = 0
        self.w.dep_state(list(queues), self.now, list(reqs))
        self.w.peers(self.now)                    # dep_state 里已读过：不再读（这一行只为在旧代码上也读到别的主机）
        self.w.plan_key = None
        self.w.forecast(list(queues), list(reqs), {}, self.now, self.m.POLICY)
        return {o["project"]: o for o in self.w.offer}

    def suggest(self, prev=None, extra=()):
        """看板把 t（与 extra 里别的主机）的 offers 汇成的挪法建议（rebalance.suggest）。"""
        return RB.suggest([{"id": "t", "label": "t", "worker_up": True, "status": {"offers": self.w.offer}}, *extra], prev, self.now)

    def rel(self, t):
        return None if t is None else round((t - self.now) / self.H, 2)

    def test_offer_numbers_and_suggestion(self):
        # o：a0 在跑还要 1 小时，a1–a5 各 2 小时，排到 11 小时后；t 空着。从 o 的队尾往前挑 a5、a4…，挪前 k 行后 t 在 2k 小时后跑完、
        # o 那条清单提前到 11 − 2k 小时；整批最早是挪 3 行：6 小时后（早 5 小时）
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        o = self.offer()["pa"]
        self.assertEqual([j["name"] for j in o["jobs"]], ["a5", "a4", "a3", "a2", "a1"])
        self.assertEqual([self.rel(j["end"]) for j in o["jobs"]], [2, 4, 6, 8, 10])
        self.assertEqual([self.rel(j["rest"]) for j in o["jobs"]], [9, 7, 5, 3, 1])
        self.assertEqual([self.rel(j["there"]) for j in o["jobs"]], [11, 9, 7, 5, 3])
        self.assertEqual((self.rel(o["free"]), o["lane"], o["why"]), (0, None, None))
        self.assertEqual([self.rel(v) for v in o["ends"].values()], [11])
        s = self.suggest()["pa"]
        self.assertEqual((s["target"], [j["name"] for j in s["jobs"]], self.rel(s["from"]), self.rel(s["to"])), ("t", ["a5", "a4", "a3"], 11, 6))
        self.assertEqual((s["key"], s["cut"]), ("rebalance:pa", []))
        self.assertIn("把 o 上 %s/jobs/q.txt 的 a3、a4、a5 挪到 t（这台还没有本项目同一种环境的清单：新建一条（与原清单同目录，.env 照抄原清单的，但 QUEUE_STATE 与 QUEUE_LOGS 写这条自己的；原清单没有 .env 时也新建一个，只写这两行）并 labmon worker add t %s 新清单 --project pa）"
                      % (self.d / "a", self.d / "a"), s["text"])
        self.assertIn("（早 5.0 小时）", s["text"])
        # 真把这 3 行挪过来（t 上新建 pa 的清单）：t 的推演里 a5 何时跑完与建议里的一样（同一份 simulate）
        qb, = self.hosts.queues({"b": ["a%d|true   # est_min=120 vram=20" % i for i in (3, 4, 5)]}, "pa")
        os.makedirs(qb["state"])                                        # tick 里 requests 建的
        self.w.queue_durs([qb])
        r = self.w.prep(self.m.new_req("queue", qb["key"], "pa", q=qb, root=qb["root"], queue=qb["jobs"], **self.m.next_job(qb)), self.now, {})
        self.offer([qb], [r])
        plan = {j["name"]: j for ln in self.w.plan["lanes"] for j in ln["jobs"]}
        self.assertEqual(plan["a5"]["t1"], s["to"])

    def test_borrowed_unstoppable_keepalive_card_cannot_take(self):
        # #45：t 是借来只给 pa 用的，唯一的卡上有对方另起、ka.sh 停不了的保活（只占 6 GiB）：真放行一直不放这张卡，t 的推演也不放，
        # 所以接不了 o 上的行，只写原因（旧推演清空了 owner，按剩下的 40 GiB 报能接）
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(4)], run=1)
        self.w.ovr = [{"kind": "guest", "target": "pa", "t": 0}]
        self.w.other = {"vram": {"GPU-a": 6000.0}, "ram": 1.0, "cpu": 0.5, "ka": {"GPU-a": [5300]}, "gid": {"GPU-a": {5300}}, "ka_cpu": 0.0}
        self.w.guest_scan(self.now)
        o = self.offer()["pa"]
        self.assertEqual((o["jobs"], o["stop"], o["why"]), ([], "a3", "一直排不上，显存不够（卡 0 有停不了的保活）"))
        self.assertEqual(self.suggest()["pa"]["why"], ["t 上本项目没活，接不了 a3：一直排不上，显存不够（卡 0 有停不了的保活）"])

    def test_move_duration_by_card_concurrency(self):
        # #82 审查阻塞：挪来的行与别处的推演同口径，按开跑时同卡的并行数估时。同类记录：单跑 20 分钟、并行 6 条 60 分钟；
        # t 的卡上已有 5 条在跑（挪来的开跑时是第 6 条）：挪来的 m_s9 约 60 分钟跑完，不是按单跑的 20 分钟
        self.hosts.usage("t", [usage_row("m_s1", "pa", self.now - 7200, self.now - 7200 + 1200, conc=1.0, gpus=["GPU-a"]),
                               usage_row("m_s2", "pa", self.now - 3600, self.now - 3600 + 3600, conc=6.0, gpus=["GPU-a"])])
        self.w.reload_hist()
        self.w.running = {"r%d" % i: {"key": "r%d" % i, "name": "y_s%d" % i, "project": "px", "t0": self.now, "gpus": ["GPU-a"],
                                      "est": {"ram": 1.0, "vram": 0.0, "cpus": 1}, "use": {}} for i in range(5)}
        self.origin(["m_s%d|true   # vram=1" % i for i in range(6, 10)], run=1, step=3)
        o = self.offer()["pa"]
        self.assertEqual(self.rel(o["jobs"][0]["end"]), 1.0)

    def test_appends_to_existing_lane(self):
        # t 上已有 pa 的清单（同一目录、还要 1 小时）：挪来的接在它后面（不另开车道），建议写明接在哪条清单后面
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        (self.d / "a" / "jobs" / "r.txt").write_text("b0|true   # est_min=60 vram=20\n")
        qb = self.m.load_queue({"_slug": "r", "root": str(self.d / "a"), "jobs": "jobs/r.txt", "project": "pa"})
        os.makedirs(qb["state"], exist_ok=True)                         # tick 里 requests 建的
        self.w.queue_durs([qb])
        r = self.w.prep(self.m.new_req("queue", qb["key"], "pa", q=qb, root=qb["root"], queue=qb["jobs"], **self.m.next_job(qb)), self.now, {})
        o = self.offer([qb], [r])["pa"]
        self.assertEqual((o["lane"], self.rel(o["free"])), ([qb["root"], "jobs/r.txt"], 1))
        self.assertEqual([self.rel(j["end"]) for j in o["jobs"][:2]], [3, 5])
        self.assertIn("接在 %s/jobs/r.txt 后面" % qb["root"], self.suggest()["pa"]["text"])

    def test_cannot_move_reasons(self):
        # 挪不了的只写原因（页面上），不给建议：显存放不下、依赖不知道何时满足、借来的主机约定前跑不完、借来的主机只给别的项目用；
        # 挪不动的那一行在 stop（与具体某行无关的为 None），看板拼成“t 上本项目没活，接不了 X：原因”（#25 B）
        cases = [
            (["a%d|true   # est_min=120 vram=50" % i for i in range(4)], [], "a3", "一直排不上，放不下（要的比卡的显存还大）"),
            (["a0|true   # est_min=120", "a1|true   # est_min=120", "a2|true   # est_min=120 after=nowhere"], [], "a2", "a2 等的 nowhere 跑不成或不知道何时跑完"),
            (["a%d|true   # est_min=120" % i for i in range(4)], [{"kind": "deadline", "target": "borrow", "at": self.now + 1800}], "a3", "一直排不上，截止前跑不完"),
            (["a%d|true   # est_min=120" % i for i in range(4)], [{"kind": "guest", "target": "pb"}], None, "借来的主机只给 pb 用"),
        ]
        for i, (lines, ovr, stop, why) in enumerate(cases):
            with self.subTest(why=why):
                self.origin(lines, run=1, name="c%d" % i)
                self.w.ovr = ovr
                o = self.offer()["pa"]
                self.assertEqual((o["jobs"], o["stop"], o["why"]), ([], stop, why))
                s = self.suggest()["pa"]
                self.assertNotIn("key", s)
                self.assertEqual(s["why"], ["t 上本项目没活，接不了%s：%s" % (" " + stop if stop else "", why)])

    def test_hold_and_soon_not_moved(self):
        # 队尾是占位任务、原处 10 分钟内就开跑的都不挑
        self.origin(["a0|true   # est_min=120", "a1|true   # est_min=120", "hold_x|true   # gpus=0 est_min=60"], run=1)
        o = self.offer()["pa"]
        self.assertEqual((o["jobs"], o["why"]), ([], "o 上 jobs/q.txt 的队尾 hold_x 是占位任务，不挪"))
        self.origin(["b0|true   # est_min=120", "b1|true   # est_min=120"], run=0.1, host="o", name="b")
        self.assertIn("10 分钟内就开跑", self.offer()["pa"]["why"])

    def test_stale_or_stopped_origin(self):
        # 那台状态过期或 worker stop 过（含已归还）：不当来源
        self.origin(["a%d|true   # est_min=120" % i for i in range(4)], run=1, age=self.m.STATUS_STALE_S + 60)
        self.assertEqual(self.offer(), {})
        self.origin(["b%d|true   # est_min=120" % i for i in range(4)], run=1, stopped=True, name="b")
        self.assertEqual(self.offer(), {})

    def test_same_name_rows(self):
        # 阻塞 1：两台上 pa 的清单队尾都叫 dup，t 是借来的、约定 3 小时后还，只来得及跑一个：挪来的按在车道里的位置认，不按名字（以前 StopIteration，
        # worker 退出、拉起后反复崩）；第二个 dup 写明挪不来的原因
        for h in ("o", "o2"):                                                # 同一目录下的两条清单（同一种环境，一批里能一起挪）
            self.origin(["x|true   # est_min=120", "dup|true   # est_min=120"], run=1, host=h, name=h, root="o" if h == "o2" else None)
        self.w.ovr = [{"kind": "deadline", "target": "borrow", "at": self.now + 3 * self.H}]
        o = self.offer()["pa"]
        self.assertEqual([(j["host"], j["name"]) for j in o["jobs"]], [("o", "dup")])
        self.assertEqual(self.rel(o["jobs"][0]["end"]), 2)
        self.assertTrue(o["stop"] == "dup" and "截止前跑不完" in o["why"], o)

    def test_offers_error_isolated(self):
        # 算 offers 出错只丢掉这一轮的 offers、记一行日志（同一个错只记一次），推演照常写 plan，不让 worker 退出
        self.origin(["a%d|true   # est_min=120" % i for i in range(4)], run=1)
        qb, = self.hosts.queues({"b": ["b0|true   # est_min=60"]}, "pa")
        os.makedirs(qb["state"])
        self.w.queue_durs([qb])
        r = self.w.prep(self.m.new_req("queue", qb["key"], "pa", q=qb, root=qb["root"], queue=qb["jobs"], **self.m.next_job(qb)), self.now, {})
        self.w.offer = [{"project": "old"}]
        logs = []
        with mock.patch.object(self.w, "offers", side_effect=RuntimeError("坏了")), mock.patch.object(self.w, "wlog", logs.append):
            self.offer([qb], [r])
            self.offer([qb], [r])
        self.assertEqual(self.w.offer, [])
        self.assertEqual([j["name"] for ln in self.w.plan["lanes"] for j in ln["jobs"]], ["b0"])
        self.assertEqual(logs, ["算能接别处的任务出错：RuntimeError('坏了')"])

    def test_deps_by_dep_state(self):
        # 阻塞 2：写了 after= 的行按 dep_state 同一口径（别的主机的 todo 与 plan、各主机用量记录）判依赖，接收方本机没有依赖时也一样：
        # 被等的正在 o 上跑（1 小时后完）、早已跑完的照常挪；跑失败的不挪，原因写跑不成
        lines = ["a0|true   # est_min=120", "a1|true   # est_min=120", "a2|true   # est_min=120", "a3|true   # est_min=120 after=%s"]
        self.origin([lines[0], lines[1], lines[2], lines[3] % "a0"], run=1)
        o = self.offer()["pa"]
        self.assertEqual(([j["name"] for j in o["jobs"]][:2], self.rel(o["jobs"][0]["end"])), (["a3", "a2"], 3))      # a3 等 a0（1 小时后完）：接来后 1 小时开跑
        self.hosts.usage("o", [{"project": "pa", "name": "z", "rc": "0", "t0": self.now - 7200, "t1": self.now - 3600, "ram_peak": 1.0}])
        self.w.reload_hist()
        self.origin([lines[0], lines[1], lines[2], lines[3] % "z"], run=1, name="b")
        self.assertEqual(self.offer()["pa"]["jobs"][0]["name"], "a3")
        self.hosts.usage("o", [{"project": "pa", "name": "z", "rc": "1", "t0": self.now - 3000, "t1": self.now - 1800, "ram_peak": 1.0}])
        self.w.reload_hist()
        self.origin([lines[0], lines[1], lines[2], lines[3] % "z"], run=1, name="c")
        o = self.offer()["pa"]
        self.assertEqual(o["jobs"], [])
        self.assertEqual(o["stop"], "a3")
        self.assertIn("z", o["why"])

    def test_waited_on_not_moved(self):
        # 别处的行写了 after= 等着队尾这一行：挪走后它何时开始会变，不挪
        self.origin(["a%d|true   # est_min=120" % i for i in range(4)], run=1,
                    extra_todo=[{"project": "pb", "root": None, "queue": None, "jobs": ["w"], "deps": {"w": [["pa", "a3"]]}, "held": None}])
        o = self.offer()["pa"]
        self.assertEqual((o["jobs"], o["why"]), ([], "a3 有 w 写了 after= 等着它，不挪"))

    def test_trapped_origin_and_renewing_receiver(self):
        # 维护者 11-08 定：原处被续租截止（或 drain）挡住、在原处跑不了的照常能挪，跑不了的清单一次至多挑 MOVE_TRAP_MAX 行（超过 MOVE_MAX）；
        # 接收方自己在预备续租（续租截止）时不接
        ovr = [{"kind": "deadline", "at": self.now + 1800}]
        self.origin(["a%d|true   # est_min=30" % i for i in range(7)], held="deadline", overrides=ovr)
        o = self.offer()["pa"]
        self.assertEqual(len(o["jobs"]), 7)
        self.assertEqual(list(o["ends"].values()), [None])
        self.assertEqual(self.rel(o["jobs"][-1]["end"]), 3.5)
        s = self.suggest()["pa"]
        self.assertEqual((len(s["jobs"]), s["from"], self.rel(s["to"])), (7, None, 3.5))
        self.origin(["a%d|true   # est_min=30" % i for i in range(7)], held="drain", overrides=[{"kind": "drain"}], name="b")
        self.assertEqual(len(self.offer()["pa"]["jobs"]), 7)
        self.w.ovr = [{"kind": "deadline", "at": self.now + 86400}]
        o = self.offer()["pa"]
        self.assertEqual(o["jobs"], [])
        self.assertTrue(o["why"].startswith("这台在预备续租（续租截止"), o["why"])

    def test_threshold_and_hysteresis(self):
        # o：a0 还要 3 小时、a1 50 分钟：挪 a1 早 50 分钟，不到 1 小时不建议；报过同一建议的，降到阈值一半以下才收
        self.origin(["a0|true   # est_min=180", "a1|true   # est_min=50"], run=3, step=50 / 60)
        self.offer()
        self.assertEqual(self.suggest(), {})
        self.assertEqual(self.suggest({"pa": {"key": "rebalance:pa"}})["pa"]["key"], "rebalance:pa")
        # 整批还要 21 小时：挪一行早 1.5 小时，不到剩余的 10%（2.1 小时）不建议；早 3 小时的建议
        job = {"host": "o", "root": "/r", "queue": "q.txt", "name": "x", "there": self.now + 21 * self.H, "end": self.now + 1.5 * self.H, "own": self.now}
        o = {"project": "pa", "lane": None, "free": self.now, "ends": {"o|/r|q.txt": self.now + 21 * self.H}, "why": None,
             "jobs": [dict(job, rest=self.now + 19.5 * self.H)]}
        self.w.offer = [o]
        self.assertEqual(self.suggest(), {})
        self.w.offer = [dict(o, jobs=[dict(job, rest=self.now + 18 * self.H)])]
        self.assertEqual(self.rel(self.suggest()["pa"]["to"]), 18)

    def test_key_stable_across_rounds(self):
        # 阻塞 3：两台一样空的接收方 B、C 各约 2 分钟重算一次、相位不同，谁的完工早每轮都可能换（差一分钟）；原处两条清单的预计结束也在漂、
        # 第一行跟着换。key 只按项目、目标沿用上一轮的（差不到 STICK_S）：连续多轮只叫醒一次（_new_alerts 同 labmon wait）
        from labmon.waits import _new_alerts
        H, now = self.H, self.now

        def off(d, first):
            js = [{"host": "o", "root": "/r", "queue": "jobs/%s.txt" % first, "name": first, "there": now + 10 * H, "rest": now + H,
                   "end": now + 2 * H + d, "own": now}]
            return {"project": "pa", "lane": None, "free": now, "ends": {"o|/r|jobs/%s.txt" % first: now + 10 * H}, "jobs": js, "why": None}
        prev, keys, targets, woke = None, [], [], []
        sf = self.d / "wait.json"
        for i in range(6):
            b, c = (0, 60) if i % 2 else (60, 0)
            first = "x" if i % 3 else "y"
            hosts = [{"id": "B", "label": "B", "worker_up": True, "status": {"offers": [off(b, first)]}},
                     {"id": "C", "label": "C", "worker_up": True, "status": {"offers": [off(c, first)]}}]
            prev = RB.suggest(hosts, prev, now)
            keys.append(prev["pa"]["key"])
            targets.append(prev["pa"]["target"])
            woke += _new_alerts(sf, {prev["pa"]["key"]: prev["pa"]["text"]})
        self.assertEqual((set(keys), set(targets), woke), ({"rebalance:pa"}, {targets[0]}, ["rebalance:pa"]))
        # 另一台明显更好（早 STICK_S 以上）才换过去；key 不变、不再叫醒
        hosts = [{"id": "B", "label": "B", "worker_up": True, "status": {"offers": [off(0, "x")]}},
                 {"id": "C", "label": "C", "worker_up": True, "status": {"offers": [off(0, "x")]}}]
        other = "B" if targets[0] == "C" else "C"
        hosts[0 if other == "B" else 1]["status"]["offers"][0]["jobs"][0]["end"] -= H
        s = RB.suggest(hosts, prev, now)["pa"]
        self.assertEqual((s["target"], _new_alerts(sf, {s["key"]: s["text"]})), (other, []))

    def test_why_when_batch_unknown(self):
        # 整批推不出（原处有跑不了的、这台又接不了）：原因不说“没有本项目的任务”
        hosts = [{"id": "t", "label": "t", "worker_up": True,
                  "status": {"offers": [{"project": "pa", "lane": None, "free": self.now + 3 * self.H, "ends": {"o|/r|jobs/q.txt": None}, "jobs": [], "why": "放不下"}]}}]
        self.assertEqual(RB.suggest(hosts, None, self.now)["pa"]["why"], ["t 接不了：放不下"])

    def test_borrow_cut_lane(self):
        # o 是借来的主机，pa 的清单被约定归还挡住（推演里没有它）：在原处跑不了（there 为 None），整批何时跑完推不出；挪完才推得出，照样建议，
        # 并标出这批来自约定挡住的清单（cut）：由 borrow_cut 告警带上这句，rebalance 不再报
        from labmon import borrow as BW
        ovr = [{"kind": "deadline", "target": "borrow", "at": self.now + 1800}]
        self.origin(["a%d|true   # est_min=60" % i for i in range(3)], held="deadline", overrides=ovr)
        o = self.offer()["pa"]
        self.assertEqual([(j["name"], j["there"]) for j in o["jobs"]], [("a2", None), ("a1", None), ("a0", None)])
        self.assertEqual(list(o["ends"].values()), [None])
        st = json.loads((self.hosts.home / "hosts" / "o" / "status.json").read_text())
        s = self.suggest(extra=[{"id": "o", "label": "o（借 X）", "worker_up": False, "status": st, "borrow": {"late": BW.late_jobs(st)}}])["pa"]
        self.assertEqual(([j["name"] for j in s["jobs"]], s["from"], self.rel(s["to"])), (["a2", "a1", "a0"], None, 3))
        self.assertEqual(s["cut"], ["o|%s|jobs/q.txt" % (self.d / "a")])
        self.assertIn("现在有任务在原处跑不了", s["text"])

    # ---- #25 ----
    def busy_guest(self, w, target="pa"):
        """把 w 当成借来的主机：只给 target 用，对方的内存此刻超出预留（guest_block 为真）。"""
        w.ovr = [{"kind": "guest", "target": target}]
        w.back_last, w.back = {"ram": self.now}, {"ram": "对方的内存超出预留"}

    def test_guest_busy_lane_still_planned(self):
        # #25 A1：借来的主机 o 上对方此刻超出预留（guest_block，真放行照旧挡住）：推演照常排这条清单（此刻才有的原因不模拟），
        # 队首 t0≈现在，t 不建议挪它（以前 o 的推演整条丢掉，t 把整条当“原处跑不了”、连队首一起挪，队首随即在原处放行）
        q, = self.hosts.queues({"a": ["a%d|true   # est_min=120 vram=20" % i for i in range(3)]}, "pa")
        os.makedirs(q["state"])
        o = self.hosts.worker("o")
        o.info = dict(o.info, gpus=[{"i": 0, "uuid": "GPU-o", "name": "A", "total": 46000.0, "used": 0.0, "free": 46000.0, "util": 0.0}])
        self.busy_guest(o)
        o.queue_durs([q])
        r = o.prep(self.m.new_req("queue", q["key"], "pa", q=q, root=q["root"], queue=q["jobs"], **self.m.next_job(q)), self.now, {})
        self.assertEqual(r["held"][0], "guest")
        o.list_state([q], [r])
        o.forecast([q], [r], {}, self.now, self.m.POLICY)
        plan = {j["name"]: j for ln in o.plan["lanes"] for j in ln["jobs"]}
        self.assertEqual(sorted(plan), ["a0", "a1", "a2"])
        self.assertLess(plan["a0"]["t0"] - self.now, 60)
        self.hosts.status("o", plan=o.plan, todo=o.todo, overrides=o.ovr,
                          waiting=[{"kind": "queue", "queue": q["jobs"], "root": q["root"], "project": "pa", "name": "a0", "why": "超出预留", "held": "guest"}])
        off = self.offer()["pa"]
        self.assertNotIn("a0", [j["name"] for j in off["jobs"]])
        self.assertIn("a0 10 分钟内就开跑", off["why"])
        # “只给别的项目用”是持久的挡法：推演照旧不排
        self.busy_guest(o, target="pb")
        o.back_last, o.back = {}, {}
        r = o.prep(self.m.new_req("queue", q["key"], "pa", q=q, root=q["root"], queue=q["jobs"], **self.m.next_job(q)), self.now, {})
        o.plan_key = None
        o.forecast([q], [r], {}, self.now, self.m.POLICY)
        self.assertEqual([j["name"] for ln in o.plan["lanes"] for j in ln["jobs"]], [])

    def test_unknown_lane_not_moved(self):
        # #25 A2：推演里没有、等待项也没有 held 的清单（读不了状态目录、队列没出请求）说不出原处为什么跑不了：不挪、整批推不出
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(4)], in_plan=False)
        o = self.offer()["pa"]
        self.assertEqual((o["jobs"], list(o["ends"].values())), ([], [None]))
        self.assertIn("推演里没有", o["why"])
        self.assertNotIn("key", self.suggest().get("pa", {}))

    def test_stale_origin_batch_unknown(self):
        # #25 第 8 条：o2 的 worker 不在（状态过了 STATUS_STALE_S、没 worker stop，DEP_STALE_S 内）：它的清单整条看不见，整批推不出、不建议
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.origin(["b%d|true   # est_min=120 vram=20" % i for i in range(2)], run=1, host="o2", name="b", age=self.m.STATUS_STALE_S + 60)
        o = self.offer()["pa"]
        self.assertIn(None, list(o["ends"].values()))
        self.assertNotIn("key", self.suggest().get("pa", {}))

    def test_returned_origin_not_unknown(self):
        # #25 非阻塞 1（维护者 11-09 定）：o2 是已归还（host return）或平台已关机的自用主机（看板下发的 returned.json 里有它）：
        # 它没 worker stop、状态在 DEP_STALE_S 内，以前清单记 unknown、一天内整批推不出、不出建议；现在不挡。只是暂时不在的仍然挡（上一条用例）
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.origin(["b%d|true   # est_min=120 vram=20" % i for i in range(2)], run=1, host="o2", name="b", age=self.m.STATUS_STALE_S + 60)
        Path(self.w.p.dir, "returned.json").write_text(json.dumps({"hosts": ["o2"]}))
        o = self.offer()["pa"]
        self.assertNotIn(None, list(o["ends"].values()))
        self.assertTrue(o["jobs"])
        self.assertIn("key", self.suggest()["pa"])
        self.assertIn("o2", [h for h, _, _, _ in self.w.peer_todo])          # 依赖那边照旧：没 stop 过的主机还会跑

    def test_rows_missing_from_list(self):
        # #25 第 2 条：todo 里有、清单文件里没有的行（刚剪过）不挪；清单读不了的也不挪（以前按缺省资源提示当候选）
        q = self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(4)], run=1)
        Path(q["jobs_path"]).write_text("".join("a%d|true   # est_min=120 vram=20\n" % i for i in range(3)))
        o = self.offer()["pa"]
        self.assertEqual(o["jobs"], [])
        self.assertIn("a3 不在", o["why"])
        os.rename(q["jobs_path"], q["jobs_path"] + ".x")
        o = self.offer()["pa"]
        self.assertEqual(o["jobs"], [])
        self.assertIn("读不到", o["why"])

    def test_same_name_first_row(self):
        # #43 复核非阻塞 1：同一清单里同名的行按名字取资源提示时取第一条（worker 跑的是第一条），以前取最后一条（vram=60 放不下 46 GiB 的卡）
        self.origin(["a0|true   # est_min=120 vram=20", "a1|true   # est_min=120 vram=20", "a2|true   # est_min=120 vram=20", "a2|true   # est_min=120 vram=60"], run=1)
        self.assertIn("a2", [j["name"] for j in self.offer()["pa"]["jobs"]])

    def test_pause_beats_drain_in_todo(self):
        # #25 第 4 条：主机 drain 又暂停了本项目：todo 的 held 写 pause（用户有意停的），offers 不列；以前按指令先后写 drain、当成可挪
        q, = self.hosts.queues({"a": ["a0|true   # est_min=120"]}, "pa")
        os.makedirs(q["state"])
        self.w.ovr = [{"kind": "drain"}, {"kind": "pause", "target": "pa"}]
        self.w.queue_durs([q])
        self.w.list_state([q])
        self.assertEqual(self.w.todo[0]["held"], "pause")

    def test_receiver_guest_busy_still_offers(self):
        # #25 第 1 条：接收方 t 是借来的主机、对方此刻超出预留：几小时后接不接得了不看它（推演也不模拟），照常给挪法；
        # 以前 offers 只写原因、建议消失一轮再出现又叫醒一次
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.busy_guest(self.w)
        o = self.offer()["pa"]
        self.assertEqual(len(o["jobs"]), 5)
        self.assertIsNone(o["why"])

    def test_offers_error_keeps_last(self):
        # #25 第 1 条：offers 出错时沿用上一次的（最多 STATUS_STALE_S），免得建议消失一轮再出现；再久就清空
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        good = self.offer()
        self.assertIn("pa", good)
        with mock.patch.object(self.w, "offers", side_effect=RuntimeError("坏了")), mock.patch.object(self.w, "wlog"):
            self.assertEqual(self.offer(), good)
            self.now = self.hosts.advance(self.m.STATUS_STALE_S + 1)
            self.assertEqual(self.offer(), {})

    def test_dep_stuck_origin_not_trapped(self):
        # #183：o 上 pa 的 b1 写了 after=x1，x1 在 t 自己的清单里。o 读到的 t 的状态里 todo 已有 x1、推演里还没有它（两边清单刚改、
        # 对方的推演还没跟上；两台的 after= 交错成链时，一台一轮没算出的依赖时刻会沿着链来回传几轮），o 的推演停在 b1（stuck：等的 x1 不知道何时跑完）。
        # t 自己推得出 x1 何时跑完：以前 t 把 o 那条清单当“原处跑不了”（trapped），挪 b1 过来、原处整批“推不出”，叫醒会话去挪（11-10 23:19 a1）；
        # 现在停在依赖上的清单记为不知道：不挪、整批推不出，原因写依赖（页面上一句，不叫醒）
        now = self.now
        qx, = self.hosts.queues({"x": ["x0|true   # est_min=60 vram=20", "x1|true   # est_min=60 vram=20"]}, "pa")
        qb, = self.hosts.queues({"b": ["b0|true   # est_min=600 vram=20", "b1|true   # est_min=60 vram=20 after=x1"]}, "pa")
        for q in (qx, qb):
            os.makedirs(q["state"])
        self.hosts.status("t", todo=[{"project": "pa", "root": qx["root"], "queue": qx["jobs"], "jobs": ["x0", "x1"], "deps": {}, "held": None}])
        o = self.hosts.worker("o")
        o.info = dict(o.info, gpus=[{"i": 0, "uuid": "GPU-o", "name": "A", "total": 46000.0, "used": 0.0, "free": 46000.0, "util": 0.0}])
        o.queue_durs([qb])
        r = self.m.new_req("queue", qb["key"], "pa", q=qb, root=qb["root"], queue=qb["jobs"], **self.m.next_job(qb))
        o.dep_state([qb], now, [r])
        r = o.prep(r, now, {})
        o.forecast([qb], [r], {}, now, self.m.POLICY)
        ln, = o.plan["lanes"]
        self.assertEqual(([j["name"] for j in ln["jobs"]], ln["stuck"], ln["stuck_dep"]), (["b0"], "b1 等的 x1 跑不成或不知道何时跑完", "b1"))
        self.hosts.status("o", plan=o.plan, todo=o.todo,
                          queues=[{"root": qb["root"], "jobs": qb["jobs"], "gpus": None, "project": "pa", "stop": "boundary", "empty": False, "dur": {}}])
        self.w.queue_durs([qx])
        rt = self.w.prep(self.m.new_req("queue", qx["key"], "pa", q=qx, root=qx["root"], queue=qx["jobs"], **self.m.next_job(qx)), now, {})
        off = self.offer([qx], [rt])["pa"]
        self.assertEqual((off["jobs"], list(off["ends"].values())), ([], [None]))
        self.assertIn("b1 等的 x1 跑不成或不知道何时跑完", off["why"])
        s = self.suggest()["pa"]
        self.assertNotIn("key", s)
        self.assertIn("等的 x1", s["why"][0])

    def test_why_sentence(self):
        # #25 B：原因句由看板拼：“t 上本项目没活，接不了 a3：放不下（…）”；offers 的 why 只放原因、挪不动的那行在 stop
        self.cards(10000.0, total=10000.0)
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(4)], run=1)
        o = self.offer()["pa"]
        self.assertEqual((o["jobs"], o["stop"], o["why"]), ([], "a3", "一直排不上，放不下（要的比卡的显存还大）"))
        self.assertEqual(self.suggest()["pa"]["why"], ["t 上本项目没活，接不了 a3：一直排不上，放不下（要的比卡的显存还大）"])
        o["free"] = self.now + 3 * self.H
        from labmon.config import fmt_time
        self.assertEqual(self.suggest()["pa"]["why"], ["t 上本项目 %s 后没活，接不了 a3：一直排不上，放不下（要的比卡的显存还大）" % fmt_time(o["free"])])

    def test_board_drops_started_rows(self):
        # #25 A3：看板按这一轮读到的来源状态核对：offer 里第一行已在原处开跑（或已不在 todo：剪过、跑完了）就截到它前面
        q = self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.offer()
        st = json.loads((self.hosts.home / "hosts" / "o" / "status.json").read_text())
        src = lambda **kw: {"id": "o", "label": "o", "worker_up": True, "status": dict(st, **kw)}
        self.assertEqual([j["name"] for j in self.suggest(extra=[src()])["pa"]["jobs"]], ["a5", "a4", "a3"])
        run = [{"root": q["root"], "queue": q["jobs"], "name": "a3", "project": "pa"}]
        self.assertEqual([j["name"] for j in self.suggest(extra=[src(running=run)])["pa"]["jobs"]], ["a5", "a4"])
        cut = [dict(st["todo"][0], jobs=[n for n in st["todo"][0]["jobs"] if n != "a5"])]
        self.assertNotIn("key", self.suggest(extra=[src(todo=cut)]).get("pa", {}))
        self.assertEqual(len(self.suggest(extra=[{"id": "o", "label": "o", "error": "连不上", "status": None}])["pa"]["jobs"]), 3)   # 这轮没读到：不过滤

    def test_keep_offers_on_read_failure(self):
        # #25 第 1 条：目标主机这一轮读失败（error、status_error，没 stop）：沿用上一轮的 offers（照 borrow_cut 的先例），key 不消失；
        # 连续几轮都接得上，到那台状态按 STALE_S 过期为止
        from labmon.config import STALE_S
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.offer()
        t0 = self.now
        old = {"generated": t0, "hosts": [{"id": "t", "label": "t", "worker_up": True, "age": 10, "status": {"t": t0 - 10, "offers": self.w.offer}}]}
        prev = RB.suggest(old["hosts"], None, t0)
        for i, err in enumerate(({"error": "连不上"}, {"status_error": "status.json 解析不了"})):
            hosts = [dict({"id": "t", "label": "t", "status": None}, **err)]
            RB.keep_offers(hosts, old, t0 + 60 * (i + 1))
            self.assertIn("offers_kept", hosts[0])
            s = RB.suggest(hosts, prev, t0 + 60 * (i + 1))
            self.assertEqual(s["pa"]["key"], "rebalance:pa")
            old = {"generated": t0 + 60 * (i + 1), "hosts": hosts}
        hosts = [{"id": "t", "label": "t", "error": "连不上", "status": None}]
        RB.keep_offers(hosts, old, t0 + STALE_S + 1)
        self.assertNotIn("offers_kept", hosts[0])
        hosts = [{"id": "t", "label": "t", "error": "连不上", "status": None, "stopped": "x"}]
        RB.keep_offers(hosts, {"generated": t0, "hosts": [dict(old["hosts"][0], offers_kept={"t": t0, "offers": self.w.offer})]}, t0 + 60)
        self.assertNotIn("offers_kept", hosts[0])

    def test_text_new_lane_and_time(self):
        # #25 第 5 条、A4：新建清单那句写出原清单的目录与项目 id，.env 照抄；句尾写推演时刻
        from labmon.config import fmt_time
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.offer()
        s = self.suggest()["pa"]
        self.assertIn("新建一条（与原清单同目录，.env 照抄原清单的，但 QUEUE_STATE 与 QUEUE_LOGS 写这条自己的；原清单没有 .env 时也新建一个，只写这两行）并 labmon worker add t %s 新清单 --project pa" % (self.d / "a"), s["text"])
        self.assertTrue(s["text"].endswith("；按 %s 的推演" % fmt_time(self.now, date=False)), s["text"])

    def test_target_lane_same_root_and_env(self):
        # #25 D：只接到与挪来的行目录相同、.env 一样的清单后面（任务在清单的目录下跑、读那条清单的 .env）；不同目录的当新的一条
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        qb, = self.hosts.queues({"b": ["b0|true   # est_min=60 vram=20"]}, "pa")         # 另一个目录
        os.makedirs(qb["state"])
        (self.d / "a" / "jobs" / "r.txt").write_text("r0|true   # est_min=60 vram=20\n")    # 同目录的另一条清单
        qr = self.m.load_queue({"_slug": "r", "root": str(self.d / "a"), "jobs": "jobs/r.txt", "project": "pa"})
        os.makedirs(qr["state"], exist_ok=True)

        def lane(qs):
            self.w.queue_durs(qs)
            rs = [self.w.prep(self.m.new_req("queue", q["key"], "pa", q=q, root=q["root"], queue=q["jobs"], **self.m.next_job(q)), self.now, {}) for q in qs]
            return self.offer(qs, rs)["pa"]["lane"]
        self.assertIsNone(lane([qb]))
        self.assertEqual(lane([qb, qr]), [str(self.d / "a"), "jobs/r.txt"])
        (self.d / "a" / "jobs" / "r.env").write_text("X=1\n")                                # .env 不一样
        self.assertIsNone(lane([qb, qr]))

    def test_env_ignores_queue_state(self):
        # #25 审查阻塞：同目录的几条清单按规矩靠各自 .env 的 QUEUE_STATE（QUEUE_LOGS）分开状态目录，比“同一种环境”时不看这两行：
        # .env 只差 QUEUE_STATE 的能接上；任务的环境（PY）不同的接不上。以前整份比，正确配置的清单永远接不上
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        (self.d / "a" / "jobs" / "q.env").write_text("PY=python3\nQUEUE_STATE=results/_q\nQUEUE_LOGS=results/_ql\n")
        (self.d / "a" / "jobs" / "r.txt").write_text("r0|true   # est_min=60 vram=20\n")
        (self.d / "a" / "jobs" / "r.env").write_text("PY=python3\nQUEUE_STATE=results/_r\nQUEUE_LOGS=results/_rl\n")
        qr = self.m.load_queue({"_slug": "r", "root": str(self.d / "a"), "jobs": "jobs/r.txt", "project": "pa"})
        os.makedirs(qr["state"], exist_ok=True)

        def lane():
            self.w.queue_durs([qr])
            r = self.w.prep(self.m.new_req("queue", qr["key"], "pa", q=qr, root=qr["root"], queue=qr["jobs"], **self.m.next_job(qr)), self.now, {})
            return self.offer([qr], [r])["pa"]["lane"]
        self.assertEqual(lane(), [str(self.d / "a"), "jobs/r.txt"])
        (self.d / "a" / "jobs" / "r.env").write_text("PY=python3.11\nQUEUE_STATE=results/_r\n")
        self.assertIsNone(lane())

    def test_new_lane_own_state_dir(self):
        # #25 审查阻塞：新建清单那句不能让人整份照抄 .env（照抄后状态目录与原清单相同，两台 worker 互相覆盖 current、lock 与完成标记）：
        # 写明 QUEUE_STATE（与 QUEUE_LOGS）写这条自己的；照这句做出来的新清单状态目录与原清单不同
        q = self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.offer()
        text = self.suggest()["pa"]["text"]
        self.assertIn("这台还没有本项目同一种环境的清单", text)
        self.assertIn(".env 照抄原清单的，但 QUEUE_STATE 与 QUEUE_LOGS 写这条自己的", text)
        (self.d / "a" / "jobs" / "new.txt").write_text("")
        (self.d / "a" / "jobs" / "new.env").write_text("QUEUE_STATE=results/_new\nQUEUE_LOGS=results/_new_logs\n")
        qn = self.m.load_queue({"_slug": "n", "root": str(self.d / "a"), "jobs": "jobs/new.txt", "project": "pa"})
        self.assertNotEqual(qn["state"], q["state"])

    def test_env_missing_vs_only_queue_lines(self):
        # #25 复核阻塞：原清单没有 .env、照新建那句建的清单 .env 只写 QUEUE_STATE、QUEUE_LOGS：去掉这两行（与空行）后什么都不剩，
        # 与没有 .env 同一种环境，下一轮接在它后面（以前 None 对 ""，永远接不上、反复建议新建）；反方向（原清单 .env 只写 QUEUE_STATE、
        # 接收方没有 .env）同样接得上；两条都没有 .env 时也接得上。句子写明原清单没有 .env 时也新建一个、只写这两行
        q = self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(6)], run=1)
        self.offer()
        text = self.suggest()["pa"]["text"]
        jobs = self.d / "a" / "jobs"
        (jobs / "new.txt").write_text("n0|true   # est_min=60 vram=20\n")

        def lane():
            self.w.olists.clear()
            qn = self.m.load_queue({"_slug": "n", "root": str(self.d / "a"), "jobs": "jobs/new.txt", "project": "pa"})
            os.makedirs(qn["state"], exist_ok=True)
            self.w.queue_durs([qn])
            r = self.w.prep(self.m.new_req("queue", qn["key"], "pa", q=qn, root=qn["root"], queue=qn["jobs"], **self.m.next_job(qn)), self.now, {})
            return self.offer([qn], [r])["pa"]["lane"]
        (jobs / "new.env").write_text("QUEUE_STATE=results/_new\n\nQUEUE_LOGS=results/_new_logs\n")
        self.assertEqual(lane(), [str(self.d / "a"), "jobs/new.txt"])           # 原清单没有 .env，新清单只写 QUEUE_*
        (jobs / "new.env").unlink()
        self.assertEqual(lane(), [str(self.d / "a"), "jobs/new.txt"])           # 两条都没有
        (jobs / "q.env").write_text("QUEUE_STATE=results/_q\n")
        self.assertEqual(lane(), [str(self.d / "a"), "jobs/new.txt"])           # 原清单只写 QUEUE_STATE，接收方没有
        self.assertIn("原清单没有 .env 时也新建一个，只写这两行", text)
        self.assertTrue(q["state"])

    def test_plan_redone_after_marks_change(self):
        # #25 审查非阻塞 2：删掉 .failed 重跑（清单没改、队首没变）：推演的输入里有还会跑的行，当轮重推；以前 FORECAST_EVERY 内 plan 少这一行，
        # 别的主机把这条清单判成说不出何时放行、建议消失一两轮再出现
        q, = self.hosts.queues({"a": ["a0|true   # est_min=60", "a1|true   # est_min=60"]}, "pa")
        os.makedirs(q["state"])
        (Path(q["state"]) / "a1.failed").write_text("")
        self.w.queue_durs([q])

        def plan():
            r = self.w.prep(self.m.new_req("queue", q["key"], "pa", q=q, root=q["root"], queue=q["jobs"], **self.m.next_job(q)), self.now, {})
            self.w.list_state([q], [r])
            self.w.forecast([q], [r], {}, self.now, self.m.POLICY)
            return [j["name"] for ln in self.w.plan["lanes"] for j in ln["jobs"]]
        self.assertEqual(plan(), ["a0"])
        os.remove(Path(q["state"]) / "a1.failed")
        self.assertEqual(plan(), ["a0", "a1"])

    def test_candidates_one_root(self):
        # #25 D：一批候选跨了不同目录（或 .env）的清单时，挑到第一个不同的就停
        self.origin(["a%d|true   # est_min=120 vram=20" % i for i in range(5)], run=1)
        self.origin(["b%d|true   # est_min=120 vram=20" % i for i in range(5)], run=1, host="o2", name="b")
        o = self.offer()["pa"]
        self.assertEqual(len({j["root"] for j in o["jobs"]}), 1)
        self.assertIn("另一个目录或环境", o["why"])


class ForecastTest(UnitBase):
    """推演写进状态的 plan（forecast）：运行中的单元是 run 条，等待中的单元排在它后面、after 写着等谁，放不下的写原因；输入没变不重算。"""

    def test_slots_in_plan(self):
        now = time.time()
        self.cards(10000)
        self.w.running["slot:1"] = {"key": "slot:1", "kind": "slot", "name": "r1", "project": "p", "t0": now - 600, "est_min": 30, "gpus": ["GPU-a"],
                                    "est": {"ram": 1.0, "vram": 30.0}, "use": {}, "tag": None, "excl": None}
        reqs = [self.req(key="slot:2", project="q", name="u2", ngpu=1, vram=20.0, est_min=10),
                self.req(key="slot:3", project="q", name="u3", ngpu=1, vram=50.0, est_min=10)]
        self.w.forecast([], reqs, {}, now, self.m.POLICY)
        lanes = {ln["kind"] + ":" + ln["project"] + ":" + (ln["jobs"] or [{"name": ""}])[0]["name"]: ln for ln in self.w.plan["lanes"]}
        run = lanes["slot:p:r1"]["jobs"][0]
        self.assertEqual((run["run"], run["t0"], run["t1"], run["gpus"]), (True, round(now - 600), round(now + 1200), [0]))
        u2 = lanes["slot:q:u2"]["jobs"][0]
        self.assertEqual((u2["t0"], u2["t1"], u2["after"]), (round(now + 1200), round(now + 1800), [["p", "r1"]]))
        self.assertEqual(lanes["slot:q:"]["stuck"], "一直排不上，放不下（要的比卡的显存还大）")
        self.assertEqual(self.w.starts, {("slot:2", "u2"): round(now + 1200)})
        t = self.w.plan["t"]
        self.w.forecast([], reqs, {}, now + 1, self.m.POLICY)                              # 输入没变、不到 FORECAST_EVERY：沿用
        self.assertEqual(self.w.plan["t"], t)
        self.w.forecast([], reqs[:1], {}, now + 2, self.m.POLICY)                          # 等的变了：重算
        self.assertEqual((self.w.plan["t"], len(self.w.plan["lanes"])), (now + 2, 2))


    def test_run_end_by_own_concurrency(self):
        # #86：运行中任务的预计结束（没有 eta.json 时）按它自己的同卡并行数挑同类记录，不按给新任务的 k_now：
        # 卡 0 上 5 条在跑、卡 1 空着（k_now = 1）；同类单跑 20 分钟（conc 1）、并行 6 条 60 分钟（conc 6）
        now = time.time()
        self.cards(40000, 40000)
        self.w.hist = ([usage_row("v_s%d" % i, "p", now - 86400, now - 86400 + 1200, conc=1.0, gpus=["GPU-b"], gpu="A") for i in range(3)]
                       + [usage_row("v_s%d" % i, "p", now - 7200, now - 7200 + 3600, conc=6.0, gpus=["GPU-a"], gpu="A") for i in range(3, 6)])
        for i in range(5):
            self.w.running["k%d" % i] = {"key": "k%d" % i, "kind": "queue", "name": "v_s%d" % (10 + i), "project": "p", "t0": now - 600, "est_min": None,
                                         "gpus": ["GPU-a"], "est": {"ram": 1.0, "vram": 5.0, "cpus": 1}, "use": {}, "tag": None, "excl": None}
        self.assertEqual(self.w.k_now(), 1)
        self.w.durs = {"k0": {"v_s10": {"min": 20.0}}}                      # queue_durs 按 k_now 给排队的算的：单跑的 20 分钟
        j = self.w.running["k0"]
        self.assertEqual(self.w.k_of(j), 5)                                  # 还没量到 conc：按此刻卡 0 上的条数（含自己）
        self.assertAlmostEqual(self.w.run_end(j, now), now - 600 + 3600, delta=1)
        j["use"]["conc"] = 1.2                                               # 量到了：按它（按时间加权，与用量行同口径）
        self.assertAlmostEqual(self.w.run_end(j, now), now - 600 + 1200, delta=1)
        self.assertEqual(self.w.req_dur(j), 20.0)                            # 不给 k 的（等待中的请求、截止前的准入）照旧按 k_now
        # 此刻同卡几条（含自己、占位不算、多卡取平均）只有 card_conc 一处：measure 的 conc 与 k_of 的回退同口径（Codex 审查）
        hold = {"name": "hold_x", "gpus": ["GPU-a"]}
        jobs = list(self.w.running.values()) + [hold]
        self.assertEqual(self.m.card_conc(j, ["GPU-a", "GPU-b"], jobs), 3.0)    # 卡 0 上 5 条、卡 1 上只有自己
        self.assertEqual(self.m.card_conc({"name": "new"}, ["GPU-a"], jobs), 6.0)

    def test_io_does_not_change_admission(self):
        # io= 只记录（期 3 才可能用）：同样的请求带不带 io=，放不放（place）、放行顺序与排位（admit）、推演（forecast 的 plan）都一样
        h = self.m.hints("python x.py   # gpus=1 vram=20 cpus=64 io=heavy")
        self.assertEqual((h["cpus"], h["io"]), (64.0, "heavy"))
        self.assertEqual(dict(h, cpus=None, io=None), self.m.hints("python x.py   # gpus=1 vram=20"))
        now = time.time()
        gpus = self.cards(30000, 10000)
        self.w.running["slot:1"] = {"key": "slot:1", "kind": "slot", "name": "r1", "project": "p", "t0": now - 600, "est_min": 30, "gpus": ["GPU-b"],
                                    "est": {"ram": 1.0, "vram": 30.0}, "use": {}, "tag": None, "excl": None}
        plain = [self.req(key="slot:2", project="q", name="u2", ngpu=1, vram=20.0, est_min=10),
                 self.req(key="slot:3", project="r", name="u3", ngpu=1, vram=29.0, est_min=20),
                 self.req(key="slot:4", project="q", name="u4", ngpu=2, vram=50.0, est_min=10)]

        def place(r):                                                       # 准入核心按此刻的快照判（不真开跑）
            cards, why, res = self.m.place(self.w.as_job(r), self.w.snapshot({}, gpus, ()), now)
            return None if cards is not None else (why, res)

        def outcome(reqs):
            placed = [(place(r), self.m.place(self.w.as_job(r), self.w.snapshot({}, gpus, ()), now)[0]) for r in reqs]
            order = []
            waits = self.m.admit(reqs, set(), self.m.POLICY, {"q": 1.0}, now, lambda r: order.append(r["key"]) or place(r))
            self.w.plan_key = None
            self.w.forecast([], reqs, {}, now, self.m.POLICY)
            return placed, order, [(r["key"], why, pos) for r, why, pos in waits], self.w.plan["lanes"], self.w.starts
        base = outcome(plain)
        self.assertEqual([c is not None for _, c in base[0]], [True, False, False])   # u2 放得下；u3、u4 要等：比较有意义
        self.assertTrue(base[2] and base[4])
        self.assertEqual(base, outcome([dict(r, io="heavy") for r in plain]))
        self.assertEqual(base, outcome([dict(r, io="light") for r in plain]))


class PlaceTest(unittest.TestCase):
    """准入核心 place：真放行与推演同一份规则。手拼资源快照（Worker.snapshot 的形状）直接调它，核对决定、选的卡、独占留卡与原因的两种说法。"""

    NOW = 1_000_000.0

    def setUp(self):
        self.m = load_worker({})

    def card(self, i, total=40000.0, free=None, keep=0.0, other=0.0, ka=False):
        return {"i": i, "uuid": "GPU-" + "abcd"[i], "total": total, "free": total if free is None else free, "keep": keep, "other": other, "ka": ka}

    def ran(self, name, gpus, vram_mb=1024.0, t1=None, **kw):
        """快照里在跑的一个任务（labmon 的）。"""
        return dict({"key": name, "project": "p", "name": name, "t0": self.NOW - 60, "t1": t1, "gpus": gpus, "vram": {u: vram_mb for u in gpus},
                     "ram": 1.0, "cpu": 1.0, "excl": None, "tag": None, "hold": False, "ngpu": len(gpus), "use": {}, "cards": 1.0}, **kw)

    def snap(self, cards, jobs=(), **kw):
        return dict({"cards": cards, "limit": 100.0, "room": 90.0, "margin": 8.0, "cpu_cap": None, "cpu_room": None,
                     "jobs": list(jobs), "ext": [], "gp": [], "cpu": None, "holds": {},
                     "tags": {}, "owner": set(), "who": "labmon 之外的进程", "kept": False, "cut": None, "brief": False}, **kw)

    def job(self, name="x", **kw):
        return dict({"key": name, "project": "p", "name": name, "ngpu": 1, "vram": 10.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值", "excl": None, "tag": None,
                     "cpus": 1, "cpu_src": "缺省 1 核", "dur": 10.0, "hold": False, "cards": 0.25, "allowed": None}, **kw)

    def both(self, x, S, **kw):
        """同一个快照：推演（sim_snapshot，原因短；独占留卡照搬）与真放行（原因写全）各判一次，决定必须一样；返回真放行的结果。
        推演口径里没有的（对方超出预留、刚回落的卡 owner，GPU 进程、外来 CPU）不在这里比；owner 里有停不了的保活的卡（ka）推演照样挡，要比（#45）。"""
        sim = self.m.place(x, dict(self.m.sim_snapshot(S, None), holds=json.loads(json.dumps(S["holds"]))), self.NOW)
        real = self.m.place(x, S, self.NOW, **kw)
        self.assertEqual((real[0] and [c["uuid"] for c in real[0]], real[2]), (sim[0] and [c["uuid"] for c in sim[0]], sim[2]), (real, sim))
        return real

    def test_pick_and_accounting(self):
        # 选可用显存最多、够 估计值 + 1 GiB 的卡；在跑任务按记账扣（卡的 free 已加回它实测的那部分）
        S = self.snap([self.card(0), self.card(1)], [self.ran("r", ["GPU-a"], 30000.0), self.ran("r2", ["GPU-b"], 25000.0)])
        cards, why, res = self.both(self.job(vram=9.0), S)
        self.assertEqual([c["uuid"] for c in cards], ["GPU-b"])
        self.assertEqual(self.m.card_free(S), {"GPU-a": 10000.0, "GPU-b": 15000.0})
        cards, why, res = self.both(self.job(vram=19.0), S)
        self.assertEqual((cards, why, res), (None, "没有可用显存够 19 GiB 的卡（提示值，另留 1 GiB）", True))
        self.assertEqual(self.m.place(self.job(vram=19.0), self.m.sim_snapshot(S, None), self.NOW)[1], "显存不够")
        self.assertEqual(self.both(self.job(ngpu=0), S)[0], [])
        self.assertEqual(self.both(self.job(name="hold_x", hold=True, ngpu=0, excl="gpu"), S)[0], [])
        self.assertEqual(self.both(self.job(vram=9.0, allowed=[0]), S)[1:], ("没有可用显存够 9 GiB 的卡（提示值，另留 1 GiB）", True))

    def test_unstoppable_keepalive_card_same_in_forecast(self):
        # #45：借来的主机上对方另起、ka.sh 停不了的保活所在的卡（快照卡的 ka；真放行经 guest_scan 记进 owner）不会自己走，推演与真放行一样不放；
        # 对方超出预留、刚回落的卡（owner 里没有 ka 的）是此刻才有的，推演照旧不模拟。旧推演清空 owner，把任务排到卡 1 上
        S = self.snap([self.card(0), self.card(1, other=6000.0, ka=True)], [self.ran("r", ["GPU-a"], 30000.0, t1=self.NOW + 1800)],
                      owner={"GPU-b"}, who="对方", kept=True)
        cards, why, res = self.both(self.job(vram=9.0), S)
        self.assertEqual((cards, res), (None, True))
        self.assertEqual(why, "没有可用显存够 9 GiB 的卡（提示值，另留 1 GiB，已扣给对方的预留）；卡 1 上有对方另起、ka.sh 停不了的保活，不放（要停请对方停）")
        sim = self.m.sim_snapshot(S, None)
        self.assertEqual(sim["owner"], {"GPU-b"})
        self.assertEqual(self.m.sim_snapshot(sim, None), sim)                              # shadow 在推演里再换一次：不变
        self.assertEqual(self.m.place(self.job(vram=9.0), sim, self.NOW)[1], "显存不够（卡 1 有停不了的保活）")
        self.assertEqual([c["uuid"] for c in self.both(self.job(vram=5.0), S)[0]], ["GPU-a"])   # 卡 0 放得下的照放
        self.assertEqual([c["uuid"] for c in self.both(self.job(vram=5.0, excl="gpu"), S)[0] or []], [])   # 独占卡：卡 0 上有任务、卡 1 不算候选
        self.assertEqual(self.both(self.job(ngpu=0), S)[0], [])                            # 不用卡的照放
        # 对方在卡 1 上超出预留（没有停不了的保活）：真放行挡、推演不模拟（#25），原因照旧
        S["cards"][1]["ka"] = False
        self.assertEqual(self.m.sim_snapshot(S, None)["owner"], set())
        self.assertIn("对方在用卡 1（超出预留或刚回落，10 分钟内不放）", self.m.place(self.job(vram=9.0), S, self.NOW)[1])
        self.assertEqual([c["uuid"] for c in self.m.place(self.job(vram=9.0), self.m.sim_snapshot(S, None), self.NOW)[0]], ["GPU-b"])
        # 两类都有：分开写
        S = self.snap([self.card(0, ka=True), self.card(1)], owner={"GPU-a", "GPU-b"}, who="对方")
        self.assertEqual(self.m.place(self.job(vram=9.0), S, self.NOW)[1],
                         "没有可用显存够 9 GiB 的卡（提示值，另留 1 GiB）；卡 0 上有对方另起、ka.sh 停不了的保活，不放（要停请对方停）；对方在用卡 1（超出预留或刚回落，10 分钟内不放）")

    def test_never_fits_and_order_of_reasons(self):
        # 等不来的原因按 缺文件 → 放不下（内存、显存）→ 截止 → tag → pre（拷依赖、闸门）→ 资源 的顺序，前面的说了算；都不算卡在资源上
        S = self.snap([self.card(0)], tags={"h": 1})
        self.assertEqual(self.both(self.job(vram=40.0, miss="缺本地文件：a"), S)[1:], ("缺本地文件：a", False))
        self.assertEqual(self.both(self.job(vram=40.0, ram=90.0), S)[1],
                         "放不下：要内存 90 GiB（提示值，另留 8），这台容器上限 100 GiB，labmon 之外与给对方的预留占 10，labmon 的任务全停也不够；换内存大的主机或拆小")
        self.assertEqual(self.both(self.job(vram=40.0), S)[1:], ("放不下：要每张卡显存 40 GiB（提示值，另留 1 GiB）× 1 张，这台的卡最大 39 GiB；换显存大的主机或拆小", False))
        sim = self.m.sim_snapshot(S, (self.NOW + 600, False))
        self.assertEqual(self.m.place(self.job(vram=40.0), sim, self.NOW)[1], "放不下（要的比卡的显存还大）")
        self.assertEqual(self.m.place(self.job(tag="h"), sim, self.NOW)[1:], ("截止前跑不完", False))
        self.assertEqual(self.m.place(self.job(tag="h", dur=None), self.m.sim_snapshot(S, (self.NOW + 600, True)), self.NOW)[0],
                         [self.m.sim_snapshot(S, None)["cards"][0]])
        S["jobs"].append(self.ran("t", [], tag="h"))
        self.assertEqual(self.both(self.job(tag="h"), S)[1:], ("tag h 已有 1 个在跑（上限 1）", False))
        self.assertEqual(self.m.place(self.job(tag="h"), self.m.sim_snapshot(S, None), self.NOW)[1], "tag h 已到上限")
        S["jobs"].append(self.ran("xh", ["GPU-a"], excl="host"))
        self.assertEqual(self.m.place(self.job(), S, self.NOW, pre=lambda: "内存闸门")[1:], ("内存闸门", False))
        self.assertEqual(self.both(self.job(), S)[1:], ("独占整机的 xh 在跑", True))
        self.assertEqual(self.m.place(self.job(), self.m.sim_snapshot(S, None), self.NOW)[1], "等独占整机的任务")

    def test_history_estimate_over_card_takes_whole_card(self):
        # 按历史估的超过卡腾空后能给的（总显存 − 给对方留的 − 1 GiB）：按整卡算，卡上没别的任务就放（独占同理）；
        # 清单写的提示值超过卡照样放不下
        hist = "同名 近 1 次峰值 ×1.1"
        S = self.snap([self.card(0, free=39500.0)])                                      # 空卡：驱动保留几百 MB
        self.assertEqual(self.both(self.job(vram=40.0, src=hist), S)[0][0]["uuid"], "GPU-a")
        self.assertEqual(self.both(self.job(vram=40.0, src=hist, excl="gpu"), S)[0][0]["uuid"], "GPU-a")
        self.assertEqual(self.both(self.job(vram=40.0), S)[2], False)                     # 提示值：放不下，不算卡在资源上
        S["cards"][0]["keep"], S["cards"][0]["free"] = 2000.0, 37500.0                   # 借来的卡给对方留 2 GB：扣掉后整卡照放
        self.assertEqual(self.both(self.job(vram=40.0, src=hist, excl="gpu"), S)[0][0]["uuid"], "GPU-a")
        S = self.snap([self.card(0, free=39500.0)], [self.ran("r", ["GPU-a"], 2048.0)])
        self.assertEqual(self.both(self.job(vram=40.0, src=hist), S)[1:],
                         ("没有可用显存够 40 GiB 的卡（同名 近 1 次峰值 ×1.1，超过整卡按整卡算，另留 1 GiB）", True))
        S = self.snap([self.card(0, free=39500.0)], [self.ran("w", ["GPU-a"], 40.82 * 1024)])   # 整卡任务在跑：记账超过卡，别的放不进来
        self.assertIsNone(self.both(self.job(vram=0.0), S)[0])

    def test_history_ram_over_container_takes_whole(self):
        # #62：按历史估的内存（含 OOM 后翻倍的下限）超过 labmon 全停时能给的减余量：按这整份算，labmon 别的任务都没在跑就放（ram_need）；
        # 有别的任务在跑时等，原因写明估计与封顶；清单写的 ram= 照旧放不下
        hist = "同 tag 上次 OOM：放行时估 60.0 GiB ×2"
        S = self.snap([self.card(0)])                                                     # 容器 100 GiB，全停时能给 90，另留 8
        self.assertEqual(self.both(self.job(ram=120.0, ram_src=hist), S)[0][0]["uuid"], "GPU-a")
        self.assertEqual(self.both(self.job(ram=120.0, ram_src=hist, ngpu=0), S)[0], [])
        self.assertEqual(self.m.ram_need(self.job(ram=120.0, ram_src=hist), S), (82.0, hist + "；估计 120.0 GiB，封顶为 82.0 GiB（labmon 全停时能给的减去余量）"))
        self.assertEqual(self.m.ram_need(self.job(ram=50.0, ram_src=hist), S), (50.0, hist))
        real = self.both(self.job(ram=120.0), S)                                          # 提示值：放不下，不算卡在资源上
        self.assertEqual(real[2], False)
        self.assertIn("放不下：要内存 120 GiB（提示值，另留 8）", real[1])
        S = self.snap([self.card(0)], [self.ran("r", ["GPU-a"])])
        self.assertEqual(self.both(self.job(ram=120.0, ram_src=hist), S)[1:],
                         ("内存不够：要 82 GiB（%s；估计 120.0 GiB，封顶为 82.0 GiB（labmon 全停时能给的减去余量），另留 8），按记账只剩 89 GiB" % hist, True))
        S = self.snap([self.card(0)], [self.ran("w", ["GPU-a"], ram=120.0)])             # 整份的在跑：记账照估计值，别的放不进来
        self.assertIsNone(self.both(self.job(ram=0.0), S)[0])

    def test_exclusive_gpu_holds_and_backfills(self):
        # 独占卡的轮到时留住上面任务最少的卡、记预计腾空；别的任务预计在那之前跑完的回填，否则那张卡不给；放行或不再等时清掉留卡
        S = self.snap([self.card(0), self.card(1)], [self.ran("r1", ["GPU-a"], t1=self.NOW + 1200), self.ran("r2", ["GPU-b"], t1=self.NOW + 600),
                                                     self.ran("r3", ["GPU-b"], t1=self.NOW + 300)])
        x = self.job("e", excl="gpu", dur=30.0)
        self.assertEqual(self.both(x, S)[1:], ("独占：等卡 0 腾空（还在跑：r1）", True))
        self.assertEqual(S["holds"], {"e": {"cards": ["GPU-a"], "until": self.NOW + 1200}})
        self.assertEqual(self.both(self.job("s", dur=5.0), S)[0][0]["uuid"], "GPU-a")          # 20 分钟内跑得完：回填进留着的卡（可用最多的）
        self.assertEqual(self.both(self.job("l", dur=30.0), S)[0][0]["uuid"], "GPU-b")         # 回填不了：只剩没留的卡
        S["cards"][1]["free"] = 5000.0                                                           # 没留的卡也放不下：原因写明有卡留着
        self.assertEqual(self.m.place(self.job("l", dur=30.0), S, self.NOW)[1:], ("没有可用显存够 10 GiB 的卡（提示值，另留 1 GiB）；有卡留给独占任务", True))
        S["jobs"].append(self.ran("r4", ["GPU-a"]))                                             # 上面的任务变了：仍留着原来那张卡
        self.assertEqual(self.both(x, S)[1], "独占：等卡 0 腾空（还在跑：r1、r4）")
        self.assertEqual(S["holds"]["e"]["cards"], ["GPU-a"])
        S["jobs"] = [self.ran("r2", ["GPU-b"], t1=self.NOW + 600)]
        self.assertEqual([c["uuid"] for c in self.both(x, S)[0]], ["GPU-a"])                   # 腾空：放行，留卡清掉
        self.assertEqual(S["holds"], {})
        S["jobs"], S["holds"] = [], {"e": {"cards": ["GPU-b"], "until": None}}                   # 上一轮留的卡空了：先挑它，不挑更空的
        S["cards"][0]["free"], S["cards"][1]["free"] = 40000.0, 20000.0
        self.assertEqual([c["uuid"] for c in self.both(x, S)[0]], ["GPU-b"])
        S["holds"] = {"e": {"cards": ["GPU-b"], "until": None}}
        S["cards"][1]["keep"] = 36000.0                                                          # 留的卡扣掉给对方的预留后放不下了：不再等，留卡清掉
        self.assertEqual(self.m.place(self.job("e", excl="gpu", allowed=[1]), S, self.NOW)[1:], ("独占：没有放得下的卡（要显存 10 GiB，另留 1 GiB）", False))
        self.assertEqual(S["holds"], {})

    def test_blocked_exclusive_drops_hold(self):
        # 独占任务因为别的原因放不了（截止前跑不完、tag 到上限、缺文件……）：走不到留卡这一步，上一轮留的也放掉，推演同样
        S = self.snap([self.card(0)], [self.ran("r1", ["GPU-a"], t1=self.NOW + 1200)], tags={"h": 1})
        S["holds"] = {"e": {"cards": ["GPU-a"], "until": self.NOW + 1200}}
        S["jobs"].append(self.ran("t", [], tag="h"))
        self.assertEqual(self.both(self.job("e", excl="gpu", tag="h"), S)[1:], ("tag h 已有 1 个在跑（上限 1）", False))
        self.assertEqual(S["holds"], {})
        sim = dict(self.m.sim_snapshot(S, (self.NOW + 600, False)), holds={"e": {"cards": ["GPU-a"], "until": None}})
        self.assertEqual(self.m.place(self.job("e", excl="gpu"), sim, self.NOW)[1], "截止前跑不完")
        self.assertEqual(sim["holds"], {})

    def test_exclusive_gpu_blocked_is_resource_wait(self):
        # 卡都被在跑的独占任务（或别人留着）占着：算卡在资源上（等满 wait_cap 成屏障），推演也一样
        S = self.snap([self.card(0)], [self.ran("x", ["GPU-a"], excl="gpu", t1=None)])
        self.assertEqual(self.both(self.job("e", excl="gpu"), S)[1:], ("独占：没有可用的卡", True))
        self.assertEqual(self.both(self.job("n"), S)[1:], ("没有可用显存够 10 GiB 的卡（提示值，另留 1 GiB）；有卡留给独占任务", True))

    def test_exclusive_host(self):
        # 整机独占：有 labmon 的任务在跑就留住整台、记预计腾空；不用卡不占 CPU 的监督进程不算；labmon 之外的在用就不留、不算卡在资源上
        S = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], t1=self.NOW + 600), self.ran("sup", [], ngpu=0, use={"cores": 0.05})])
        x = self.job("h", excl="host", ngpu=0, dur=30.0)
        self.assertEqual(self.both(x, S)[1:], ("独占整机：等主机腾空（还在跑：r）", True))
        self.assertEqual(S["holds"], {"h": {"cards": ["*"], "until": self.NOW + 600}})
        self.assertEqual(self.both(self.job("s", dur=5.0), S)[0][0]["uuid"], "GPU-a")          # 10 分钟内跑得完：回填
        self.assertEqual(self.both(self.job("l", dur=30.0), S)[1:],
                         ("主机留给等腾空的独占任务（预计 %s 腾空，这之前跑得完的才放）" % self.m.now_str(self.NOW + 600)[5:16], True))
        S["jobs"] = [self.ran("sup", [], ngpu=0, use={"cores": 0.05})]
        self.assertEqual((self.both(x, S)[0], S["holds"]), ([], {}))
        S["gp"] = [{"hpid": 7, "pid": None, "mb": 100.0, "uuid": "GPU-a", "comm": "python", "owner": "other", "ka": False, "job": None, "_j": None}]
        self.assertEqual(self.m.place(x, S, self.NOW)[1:], ("独占整机：labmon 之外的进程在用（PID 7（python）），等用完；不为它留主机，别的任务照常放", False))
        S["gp"], S["cpu"] = [], 2.5
        self.assertIn("占 2.5 核", self.m.place(x, S, self.NOW)[1])

    def test_guest_reserve_and_owner(self):
        # 借来的主机：给对方留的从卡的 free 与 total 里扣（多卡先挑没给对方留的）；对方超出预留（或刚回落）的卡不给，原因写明
        S = self.snap([self.card(0, free=30000.0, keep=8000.0, other=2000.0), self.card(1)], kept=True, who="对方", owner={"GPU-b"})
        self.assertEqual(self.m.place(self.job(vram=30.0), S, self.NOW)[1:],
                         ("没有可用显存够 30 GiB 的卡（提示值，另留 1 GiB，已扣给对方的预留）；对方在用卡 1（超出预留或刚回落，10 分钟内不放）", True))
        S["owner"] = set()
        self.assertEqual([c["uuid"] for c in self.both(self.job(vram=30.0), S)[0]], ["GPU-b"])
        self.assertEqual([c["uuid"] for c in self.both(self.job("e", excl="gpu", vram=1.0), S)[0]], ["GPU-b"])      # 独占：先挑没给对方留的
        S["cards"][0]["total"] = 10000.0
        self.assertEqual(self.both(self.job("e", excl="gpu", vram=1.0, allowed=[0]), S)[1],
                         "独占：没有放得下的卡（要显存 1 GiB，另留 1 GiB，已扣给对方的预留）")
        S["gp"] = [{"hpid": 7, "pid": None, "mb": 2000.0, "uuid": "GPU-a", "comm": "python", "owner": "other", "ka": False, "job": None, "_j": None}]
        self.assertEqual(self.m.place(self.job("e", excl="gpu", vram=1.0, allowed=[0]), S, self.NOW)[1:],
                         ("独占：对方在用卡（PID 7（python）），等用完；不为它留卡，别的任务照常放", False))

    def test_cpu_accounting(self):
        # CPU 像内存一样记账：配额 8 核，在跑的记 6 核，只剩 2；放不下的写“CPU 不够”（卡在资源上），推演同一个决定；
        # 要的比全停时能给的还多：放不下（不算卡在资源上）；按历史估的超过配额按整份算；cpus=0 不记；读不到配额不记 CPU
        S = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], cpu=6.0)], cpu_cap=8, cpu_room=8.0)
        self.assertEqual(self.m.cpu_free(S), 2.0)
        self.assertEqual(self.both(self.job(cpus=2), S)[0][0]["uuid"], "GPU-a")
        self.assertEqual(self.both(self.job(cpus=3, cpu_src="提示值"), S)[1:], ("CPU 不够：要 3 核（提示值），按记账只剩 2.0 核（配额 8：在跑 1 个记 6 核）", True))
        self.assertEqual(S["code"], "cpu")
        self.assertEqual(self.m.place(self.job(cpus=3), self.m.sim_snapshot(S, None), self.NOW)[1], "CPU 不够")
        self.assertEqual(self.both(self.job(ngpu=0, cpus=3), S)[1:], ("CPU 不够：要 3 核（缺省 1 核），按记账只剩 2.0 核（配额 8：在跑 1 个记 6 核）", True))   # 不用卡的也要过
        self.assertEqual(self.both(self.job(cpus=0), dict(S, jobs=[self.ran("r", ["GPU-a"], cpu=8.0)]))[0][0]["uuid"], "GPU-a")
        S["cpu_room"] = 6.5                                                                        # labmon 之外占 1.5 核
        self.assertEqual(self.both(self.job(cpus=7, cpu_src="提示值"), S)[1:],                    # 没超配额：labmon 之外占着，等它们退下（不算卡在资源上）
                         ("CPU 不够：要 7 核（提示值），配额 8 核里 labmon 之外与给对方的预留此刻占 1.5 核，labmon 的任务全停也不够；"
                          "先等它们退下，一直占着再换主机或少开进程", False))
        self.assertEqual(S["code"], "cpu_other")
        self.assertEqual(self.m.place(self.job(cpus=7, cpu_src="提示值"), self.m.sim_snapshot(S, None), self.NOW)[1], "CPU 不够（labmon 之外占着）")
        self.assertEqual(self.both(self.job(cpus=9, cpu_src="提示值"), S)[1:],                    # 比配额还多：真放不下
                         ("放不下：要 9 核（提示值），这台容器配额只有 8 核；换配额大的主机或少开进程", False))
        self.assertEqual(S["code"], "nofit_cpu")
        self.assertEqual(self.m.place(self.job(cpus=9, cpu_src="提示值"), self.m.sim_snapshot(S, None), self.NOW)[1], "放不下（要的核数比容器配额还多）")
        hist = self.job(cpus=9, cpu_src="同名 近 1 次峰值 ×1.1")
        self.assertEqual(self.m.cpu_need(hist, S)[0], 6)                                           # 按历史估的：超过全停时能给的 6.5 核按整份，全停时照放
        self.assertEqual(self.both(hist, dict(S, jobs=[]))[0][0]["uuid"], "GPU-a")
        self.assertEqual(self.both(hist, S)[1:], ("CPU 不够：要 6 核（同名 近 1 次峰值 ×1.1；估计 9 核，封顶为 6 核（labmon 全停时能给的）），按记账只剩 0.5 核（配额 8：在跑 1 个记 6 核；labmon 之外占 1.5）", True))
        self.assertEqual(self.both(self.job(cpus=99), self.snap([self.card(0)], [self.ran("r", ["GPU-a"], cpu=6.0)]))[0][0]["uuid"], "GPU-a")

    def test_cpus0_not_blocked_when_overbooked(self):
        # 记账超过可放（实测比估计多、labmon 之外的变多、封顶放进来的）时 cpu_free 是负的：写了 cpus=0 的（不记账）照放，
        # 真放行与推演一致，没有原因代码（不成屏障、不计 CPU 被挡、不告警）；要核的照样“CPU 不够”。全停时能给的也是负的同样照放
        S = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], cpu=4.5), self.ran("r2", [], cpu=3.0)], cpu_cap=7, cpu_room=7.0)
        self.assertEqual(self.m.cpu_free(S), -0.5)
        for x in (self.job(ngpu=0, cpus=0, cpu_src="提示值"), self.job(cpus=0, cpu_src="提示值"), self.job("hold_x", ngpu=1, cpus=0, hold=True)):
            cards, why, res = self.both(x, S)
            self.assertEqual((cards is not None, why, res, S["code"]), (True, None, False, None), x["name"])
        self.assertEqual(self.both(self.job(ngpu=0, cpus=1, cpu_src="提示值"), S)[1:], ("CPU 不够：要 1 核（提示值），按记账只剩 0.0 核（配额 7：在跑 2 个记 7.5 核）", True))
        S = dict(S, cpu_room=-0.5, jobs=[])                                                        # 借来的主机：对方用的超过了配额
        self.assertEqual(self.both(self.job(ngpu=0, cpus=0, cpu_src="提示值"), S)[0], [])
        self.assertIsNone(S["code"])

    def test_gpu_count_and_vram_shortages_have_distinct_reasons(self):
        x = self.job(ngpu=2, vram=2)
        one = self.snap([self.card(0, total=46068)])
        restricted = self.snap([self.card(0), self.card(1)])
        mixed = self.snap([self.card(0), self.card(1, total=2048)])
        for job, S, full, short in (
            (x, one, "放不下：要 2 张 GPU，这台只检测到 1 张；换有足够卡数的主机或减少用卡数", "放不下（GPU 卡数不足：要 2 张，只有 1 张）"),
            (dict(x, allowed=[0]), restricted, "放不下：要 2 张 GPU，这台检测到 2 张，清单限制后只有 1 张可选；调整清单的选卡范围或减少用卡数",
             "放不下（GPU 卡数不足：要 2 张，只有 1 张）"),
            (x, mixed, "放不下：要 2 张 GPU，每张要显存 2 GiB（提示值，另留 1 GiB），只有 1 张满足；换有足够显存和卡数的主机或拆小",
             "放不下（显存够的卡数不足：要 2 张，只有 1 张）"),
        ):
            with self.subTest(full=full):
                self.assertEqual(self.both(job, S), (None, full, False))
                self.assertEqual(self.m.place(job, self.m.sim_snapshot(S, None), self.NOW), (None, short, False))

    def test_memory_and_no_gpu(self):
        S = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], ram=50.0)])
        self.assertEqual(self.m.ram_free(S), 40.0)
        self.assertEqual(self.both(self.job(ram=33.0), S)[1:], ("内存不够：要 33 GiB（提示值，另留 8），按记账只剩 40 GiB", True))
        self.assertEqual(self.m.place(self.job(ram=33.0), self.m.sim_snapshot(S, None), self.NOW)[1], "内存不够")
        self.assertEqual(self.both(self.job(), self.snap([]))[1:], ("看不到 GPU（nvidia-smi 无输出）", False))
        self.assertEqual(self.both(self.job(ngpu=0), self.snap([], room=None, margin=None, limit=None))[0], [])
        for code in self.m.WHY:                                                                    # 两种说法都能拼出来
            kw = {k: 1 for k in ("ram", "margin", "limit", "vram", "ngpu", "max", "n", "lim", "left", "cpus", "cap", "busy", "warm", "count")}
            kw.update({k: "x" for k in ("src", "taken", "tag", "name", "note", "who", "names", "kept", "idx", "held", "owner", "ka", "ka_s", "gang", "at", "available", "advice")})
            self.assertTrue(self.m.why_text(code, True, **kw) and self.m.why_text(code, False, **kw), code)


class SimulateTest(unittest.TestCase):
    """推演（simulate）：同一套放行规则往后排，在跑的按预计结束腾出资源。"""

    def setUp(self):
        self.m = load_worker({})
        self.now = 1_000_000.0

    def job(self, name, vram=19.0, dur=60, **kw):
        """清单上的一个任务（job_spec 的形状）：每张卡要 vram GiB（放行时另留 1 GiB），占卡按 40 GiB 的卡算。"""
        return dict({"name": name, "ngpu": 1, "vram": vram, "src": "提示值", "ram": 1.0, "ram_src": "提示值", "excl": None, "tag": None, "dur": dur, "hold": False,
                     "cpus": 1, "cpu_src": "缺省 1 核", "cards": vram / 40}, **kw)

    def test_duration_by_cards_at_start(self):
        # #67：推演按开跑时那张卡上的 labmon 任务数（含自己）取时长（job_spec 的 durk）：卡上已有 1 条时开跑的按 2 条算，再开的按 3 条算
        durk = lambda k: {"min": 10.0 * k}                                     # noqa: E731
        out, _ = self.run_([(0, "A", 40000.0)], [self.ran("r", "r", 100, 1000.0)],
                           [self.q("a", [self.job("a1", vram=1.0, durk=durk)]), self.q("b", [self.job("b1", vram=1.0, durk=durk)])])
        self.assertEqual(sorted((out["a1"][1], out["b1"][1])), [20.0, 30.0])
        out, _ = self.run_([(0, "A", 40000.0)], [], [self.q("a", [self.job("a1", vram=1.0, dur=7)])])   # 没给 durk：用 dur
        self.assertEqual(out["a1"][1], 7.0)

    def ran(self, key, name, end_min, vram_mb, t0=None):
        """开始时在卡 A 上跑着的任务（快照 jobs 的形状，显存记账 vram_mb），end_min 分钟后结束。"""
        return {"key": key, "project": key, "name": name, "t0": self.now if t0 is None else t0, "t1": self.now + end_min * 60, "gpus": ["A"],
                "vram": {"A": vram_mb}, "ram": 1.0, "cpu": 1.0, "excl": None, "tag": None, "hold": False, "ngpu": 1, "use": {}, "cards": vram_mb / 40000}

    def q(self, key, jobs, since=0, project=None, allowed=None):
        return {"key": key, "kind": "queue", "project": project or key, "allowed": allowed, "since": self.now - since, "jobs": jobs}

    def snap(self, cards, running, tags=None, room=None, cpu_room=None):
        """真放行那样的资源快照（Worker.snapshot 的形状）：cards [(序号, UUID, 总显存 MB)]，推演自己换成推演口径（sim_snapshot）。"""
        return {"cards": [{"i": i, "uuid": u, "total": t, "free": t, "keep": 0.0, "other": 0.0, "ka": False} for i, u, t in cards], "limit": None,
                "room": room, "margin": 8.0, "cpu_cap": None if cpu_room is None else int(cpu_room), "cpu_room": cpu_room, "jobs": running, "ext": [], "gp": [], "cpu": None, "holds": {}, "tags": tags or {}, "owner": set(),
                "who": "labmon 之外的进程", "kept": False, "cut": None, "brief": False}

    def run_(self, cards, running, queues, **kw):
        S = self.snap(cards, running, kw.pop("tags", None), kw.pop("room", None), kw.pop("cpu_room", None))
        out, stuck = self.m.simulate(self.now, S, queues, kw.pop("focus", set()), **kw)
        return {e["name"]: ((e["t0"] - self.now) / 60, e["t1"] and (e["t1"] - self.now) / 60, e["gpus"], e["after"]) for e in out}, stuck

    def test_cross_project_and_order(self):
        # b 的 b1 占着卡还要跑 60 分钟；a 的 a1 等显存，b1 跑完才轮到；b2 比 a2 先等，先排
        run = [self.ran("b", "b1", 60, 30000.0, t0=self.now - 600)]
        out, stuck = self.run_([(0, "A", 40000.0)], run, [self.q("a", [self.job("a1"), self.job("a2", 4.0, 30)], since=100),
                                                          self.q("b", [self.job("b2", 28.0, 120)])])
        self.assertEqual(out["a1"], (60.0, 120.0, ["A"], [["b", "b1"]]))
        self.assertEqual(out["b2"][:2], (120.0, 240.0))
        self.assertEqual(out["a2"][:2], (120.0, 150.0))
        self.assertEqual(stuck, {})

    def test_exclusive_hold_and_backfill(self):
        # 独占卡的任务轮到时留住卡，等 x1 跑完（10 分钟后）；small 5 分钟（×1.2）在那之前跑得完，回填进去；long 要 20 分钟，等独占跑完
        out, _ = self.run_([(0, "A", 40000.0)], [self.ran("x", "x1", 10, 1000.0)],
                           [self.q("e", [self.job("ex", 1.0, 30, excl="gpu")], since=10), self.q("s", [self.job("small", 1.0, 5)], since=5),
                            self.q("l", [self.job("long", 1.0, 20)], since=1)])
        self.assertEqual(out["ex"][:2], (10.0, 40.0))
        self.assertEqual(out["small"][:2], (0.0, 5.0))
        self.assertEqual(out["long"][:2], (40.0, 60.0))

    def test_stuck_and_deadline(self):
        # 放不下的（要的比卡大）一直排不上；没估时的占位任务挡住后面；续租截止前跑不完的不放
        out, stuck = self.run_([(0, "A", 40000.0)], [], [self.q("big", [self.job("huge", 88.0)]),
                                                         self.q("h", [self.job("hold1", 0.0, None, ngpu=0, hold=True), self.job("after_hold")]),
                                                         self.q("d", [self.job("short", dur=30), self.job("long", dur=150)])],
                               cut=(self.now + 3 * 3600, False))
        self.assertEqual(stuck["big"], "一直排不上，放不下（要的比卡的显存还大）")
        self.assertEqual(stuck["h"], "前面的占位任务不知道何时放行")
        self.assertEqual((out["hold1"][0], out["hold1"][1]), (0.0, None))
        self.assertEqual(out["short"][:2], (0.0, 30.0))
        self.assertNotIn("long", out)                         # 30 + 150×1.2 分钟 > 3 小时
        self.assertIn("截止前跑不完", stuck["d"])

    def test_stuck_on_dep_marked(self):
        # #183：排不上的队列里停在依赖上的记进 on_dep（写了依赖、等的不知道何时跑完的那一行；前面的占位任务不知道何时放行的那个占位任务），
        # 别的原因（放不下、时长不明、截止前跑不完）不记：offers 只把后者当“原处跑不了”
        on_dep = {}
        out, stuck = self.run_([(0, "A", 40000.0)], [], [self.q("big", [self.job("huge", 88.0)]),
                                                         self.q("h", [self.job("hold1", 0.0, None, ngpu=0, hold=True), self.job("after_hold")]),
                                                         self.q("w", [self.job("w0", dur=10), self.job("w1", dur=10, deps=["ghost"])]),
                                                         self.q("u", [self.job("u0", 1.0, None), self.job("u1", 1.0)]),
                                                         self.q("d", [self.job("short", 1.0, dur=30), self.job("long", 1.0, dur=150)])],
                               cut=(self.now + 3 * 3600, False), on_dep=on_dep)
        self.assertEqual(set(stuck), {"big", "h", "w", "u", "d"})
        self.assertEqual((stuck["h"], stuck["w"]), ("前面的占位任务不知道何时放行", "w1 等的 ghost 跑不成或不知道何时跑完"))
        self.assertEqual(on_dep, {"h": "hold1", "w": "w1"})

    def test_long_wait_barrier(self):
        # 等了 31 分钟、显存不够的 big 成了屏障，预计 20 分钟后 r1 跑完才放得下：small 10 分钟（×1.2）在那之前跑得完，回填；
        # slow 要 30 分钟，会推迟 big，让位到 big 放行后
        out, _ = self.run_([(0, "A", 40000.0)], [self.ran("r", "r1", 20, 30000.0)],
                           [self.q("big", [self.job("big", 24.0, 10)], since=31 * 60), self.q("small", [self.job("small", 1.0, 10)], since=60),
                            self.q("slow", [self.job("slow", 1.0, 30)], since=30)])
        self.assertEqual(out["big"][:2], (20.0, 30.0))
        self.assertEqual(out["small"][:2], (0.0, 10.0))
        self.assertEqual(out["slow"][:2], (20.0, 50.0))

    def test_unknown_duration_not_zero(self):
        # 时长不明的 a1 排上后不知道何时结束：同队列的 a2 与要等它腾显存的 b1 都排不上、写明原因（以前按 0 分钟算，几个都挤在同一刻）；
        # 放得下的 c1 照排
        out, stuck = self.run_([(0, "A", 40000.0)], [], [self.q("a", [self.job("a1", 28.0, None), self.job("a2", 28.0, None)]),
                                                         self.q("b", [self.job("b1", 28.0, 10)]), self.q("c", [self.job("c1", 4.0, 10)])])
        self.assertEqual(out["a1"][:2], (0.0, None))
        self.assertEqual(out["c1"][:2], (0.0, 10.0))
        self.assertNotIn("b1", out)
        self.assertEqual(stuck, {"a": "前面的 a1 不知道要跑多久（没估时也没有同类历史）", "b": "等 a1 腾出资源，它不知道要跑多久"})

    def test_fair_share_and_weight(self):
        # 一张卡一次只放得下一个：a 近期用得多（5 卡·时），先排 b 的两个；a 权重 10 时按 用量÷权重 比，两边交替
        jobs = lambda p: [self.job(p + "1", 28.0, 60), self.job(p + "2", 28.0, 60)]
        qs = lambda: [self.q("a", jobs("a"), since=100), self.q("b", jobs("b"), since=50)]
        out, _ = self.run_([(0, "A", 40000.0)], [], qs(), use={"a": 5.0})
        self.assertEqual([out[n][0] for n in ("b1", "b2", "a1", "a2")], [0.0, 60.0, 120.0, 180.0])
        out, _ = self.run_([(0, "A", 40000.0)], [], qs(), use={"a": 5.0}, pol={"cap": 1800, "hl": 86400, "w": {"a": 10.0}})
        self.assertEqual([out[n][0] for n in ("b1", "a1", "a2", "b2")], [0.0, 60.0, 120.0, 180.0])

    def test_reservation_matches_real_admission(self):
        # 推演里放行的任务按估计值记显存（与真放行的记账一样，另留的 1 GiB 只在放行时看）：40 GiB 的卡上两个 19 GiB 的一起跑；
        # 以前推演多记了 1 GiB，第二个要等第一个跑完，与真放行不一致
        out, _ = self.run_([(0, "A", 40000.0)], [], [self.q("a", [self.job("a1", 19.0, 60)]), self.q("b", [self.job("b1", 19.0, 10)])])
        self.assertEqual((out["a1"][0], out["b1"][0]), (0.0, 0.0))
        out, _ = self.run_([(0, "A", 40000.0)], [], [self.q("a", [self.job("a1", 20.0, 60)]), self.q("b", [self.job("b1", 19.0, 10)])])
        self.assertEqual((out["a1"][0], out["b1"][0]), (0.0, 60.0))

    def test_cpu_frees_later(self):
        # 推演同样记 CPU：配额 4 核、b1 记 3 核还要跑 30 分钟，要 2 核的 a1 等 b1 跑完；cpus=0 的 z1 不记，马上开始
        run = [dict(self.ran("b", "b1", 30, 1000.0), cpu=3.0)]
        out, stuck = self.run_([(0, "A", 40000.0)], run, [self.q("a", [self.job("a1", 1.0, 10, cpus=2)], since=100),
                                                          self.q("z", [self.job("z1", 1.0, 10, cpus=0)])], cpu_room=4.0)
        self.assertEqual((out["a1"][:2], out["a1"][3]), ((30.0, 40.0), [["b", "b1"]]))
        self.assertEqual(out["z1"][:2], (0.0, 10.0))
        out, stuck = self.run_([(0, "A", 40000.0)], run, [self.q("a", [self.job("a1", 1.0, 10, cpus=5, cpu_src="提示值")])], cpu_room=4.0)
        self.assertEqual(stuck["a"], "一直排不上，放不下（要的核数比容器配额还多）")

    def test_usage_and_card_share(self):
        # 用量：跑完的按半衰期衰减（一个半衰期前跑了 1 小时）；在跑的已跑部分衰减、预计还要跑的照记
        h = 3600.0
        u = self.m.usage([("a", 1.0, self.now - 2 * h, self.now - h), ("b", 0.5, self.now - h / 2, self.now + h / 2)], self.now, h)
        self.assertAlmostEqual(u["a"], 0.25 / math.log(2), places=4)
        self.assertAlmostEqual(u["b"], 0.5 * (1 - 0.5 ** 0.5) / math.log(2) + 0.25, places=4)
        cs = self.m.card_share
        self.assertEqual((cs(1, 12.0, None, 48.0, 2), cs(1, 12.0, "gpu", 48.0, 2), cs(1, 0.0, "host", 48.0, 2), cs(0, 0.0, None, 48.0, 2)),
                         (0.25, 1.0, 2.0, 0.0))


class DurationTest(unittest.TestCase):
    """任务要跑多久（duration）：同类的 实际/估时、项目的加权比值、清单估时；各主机的历史一起看；写进状态给看板。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.m = worker_module(self.d)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def rec(name, mins, est=None, **kw):
        return dict({"name": name, "project": "bp", "rc": "0", "min": mins, "est_min": est, "ram_peak": 1.0, "t1": 1794412800}, **kw)

    def dur(self, hist, name, est=None, **kw):
        return self.m.duration(hist, {"name": name, "project": "bp", "est_min": est}, **kw)

    def test_rules(self):
        # 构造历史估时与实际耗时：bert 估 160 实际约 125，vit 估 400 实际约 245，两个探针估 20、5 实际 1 分钟
        hist = [self.rec("p1_sst2_bert_clean_s42", 122.0, 160), self.rec("p1_sst2_bert_clean_s43", 127.0, 160),
                self.rec("p1_cifar_vit_clean_s42", 247.0, 400), self.rec("p1_cifar_vit_clean_s43", 241.0, 400),
                self.rec("p1_probe_abpc_b128", 0.8, 20), self.rec("p1_probe_abpc_b128_full", 1.0, 5)]
        d = self.dur(hist, "p1_sst2_bert_clean_s44", 160)
        self.assertAlmostEqual(d["min"], 124.5, delta=0.1)                     # 同类 实际/估时 中位数，不被探针与 vit 拉低
        self.assertIn("同类近 2 次 实际/估时", d["src"])
        self.assertEqual((d["cls"], d["same"]), ("p1_sst2_bert_clean", True))                  # same：同类历史给的（读写拥塞嫌疑只认它）
        d = self.dur(hist, "p1_cifar_vit_wu_s42", 420)                         # 新类型：本项目按时长加权的比值
        self.assertFalse(d["same"])
        self.assertAlmostEqual(d["k"], (122 + 127 + 247 + 241 + 0.8 + 1) / (160 * 2 + 400 * 2 + 25), delta=0.001)
        self.assertIn("按时长加权", d["src"])
        self.assertEqual(self.m.duration(hist, {"name": "p1_sst2_bert_clean_s44", "project": "a1", "est_min": 30})["src"], "清单估时")   # 别的项目的记录不看
        self.assertIsNone(self.dur(hist, "p1_new"))                            # 没估时、没同类
        hist.append(self.rec("p1_hold_s42", 30.0, 900))
        self.assertEqual(self.dur(hist, "p1_hold_s43", 900), {"min": 900, "src": "清单估时", "k": None, "cls": "p1_hold", "same": False})   # 占位任务：不看同类、不折算
        self.assertIsNone(self.dur(hist, "p1_hold_s43"))
        self.assertEqual(self.dur(hist, "p1_cifar_vit_clean_s44")["min"], 244.0)   # 没写估时：同类实际用时中位数

    def test_filters(self):
        hist = [self.rec("x_s1", 100.0, 100), self.rec("x_s2", 50.0, 100, gpu="V100"),        # 别的型号不认
                self.rec("x_s3", 200.0, 100, with_gpu=["y_s1"]),                              # 和别的任务同卡、不知道并行几条（旧行）：有知道的就不用
                self.rec("x_s4", 10.0, 100, requeued=True), self.rec("x_s5", 30.0, 100, rc="143")]   # 停下重排、失败的不算
        d = self.dur(hist, "x_s9", 100, model="NVIDIA RTX A6000")
        self.assertEqual((d["min"], d["src"]), (100.0, "同类近 1 次 实际/估时 ×1.00"))

    def test_by_concurrency(self):
        # #67：同类挤着跑与单跑差得多时，按预计的同卡并行数挑记录：k=1 取单跑的，k=6 取并行约 6 条的；旧行没有 conc：同卡没别的任务当 1，其余不知道、排最后
        hist = [self.rec("v_s1", 20.0, 30, conc=1.0), self.rec("v_s2", 21.0, 30, with_gpu=[]),
                self.rec("v_s3", 60.0, 30, conc=6.0), self.rec("v_s4", 64.0, 30, conc=5.4), self.rec("v_s5", 90.0, 30, with_gpu=["w_s1"])]
        self.assertAlmostEqual(self.dur(hist, "v_s9", 30)["min"], 20.5)
        d = self.dur(hist, "v_s9", 30, k=6)
        self.assertEqual((round(d["min"], 6), d["src"]), (62.0, "同类近 2 次（同卡并行约 5.7 条） 实际/估时 ×2.07"))
        self.assertEqual(self.dur(hist, "v_s9", k=6)["src"], "同类近 2 次（同卡并行约 5.7 条）实际用时")
        old = [self.rec("v_s5", 90.0, 30, with_gpu=["w_s1"]), self.rec("v_s6", 80.0, 30, with_gpu=["w_s2"])]   # 都不知道并行数：取最新的几条
        self.assertEqual(self.dur(old, "v_s9", k=3)["min"], 85.0)
        # 项目比值同样按并行数挑：新类在挤着跑的项目里，按挤着跑的比值折算
        d = self.dur(hist + [self.rec("u_s1", 10.0, 30, conc=1.0), self.rec("u_s2", 10.0, 30, conc=1.0)], "new_s1", 90, k=6)
        self.assertEqual(d["k"], round((60 + 64) / 60, 3))

    def test_running_siblings_by_progress(self):
        # #67：新类（sm_vit）没有跑完的同类，清单写 90 分钟；在跑的同类按日志进度估约 300 分钟：排队的按它估，不按清单估时，也不按项目比值
        hist = [self.rec("u_s1", 10.0, 30), self.rec("u_s2", 10.0, 30)]
        d = self.dur(hist, "vit_s43", 90, run=[300, 310])
        self.assertEqual((d["min"], d["src"], d["same"]), (305.0, "同类在跑的 2 个按进度估", False))
        hist.append(self.rec("vit_s41", 280.0, 90))                     # 同类有跑完的：回到同类历史
        self.assertIn("同类近 1 次", self.dur(hist, "vit_s43", 90, run=[300])["src"])
        w = self.m.Worker("t")                                                # Worker：在跑的同类按 eta.json（开跑后写的）算总长，排队的同类按它，在跑的自己不算
        now = time.time()
        w.running = {"a": {"key": "a", "name": "vit_s42", "project": "bp", "t0": now - 9000, "gpus": ["G"]},
                     "b": {"key": "b", "name": "hold_x", "project": "bp", "t0": now - 60, "gpus": []}}
        w.eta, w.eta_t = {"bp|vit_s42": now + 9000, "bp|hold_x": now + 60}, now - 30
        self.assertEqual(w.run_tot(), {("bp", "vit"): [("vit_s42", 300)]})
        root = self.d / "bp"
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("vit_s42|true   # est_min=90\nvit_s44|true   # est_min=90\n")
        q = self.m.load_queue({"_slug": "k", "root": str(root), "jobs": "jobs/q.txt", "project": "bp"})
        w.queue_durs([q])
        self.assertEqual(w.durs["k"]["vit_s44"]["src"], "同类在跑的 1 个按进度估")
        self.assertEqual(w.durs["k"]["vit_s44"]["min"], 300)
        self.assertEqual(w.durs["k"]["vit_s42"]["src"], "清单估时")      # 在跑的自己：不拿自己的进度估自己

    def test_conc_across_hosts(self):
        # #67 多主机：别的主机的用量行带的 conc 一样认。29 上挤着跑（并行 6 条，60 分钟）、借来的主机上单跑（20 分钟）；
        # 本机卡上已有 5 条在跑（k_now = 6）时，排队的按挤着跑的记录估
        for host, name, mins, conc in (("29", "x_s1", 60.0, 6.0), ("borrowed", "x_s2", 20.0, 1.0)):
            f = self.d / "home" / "hosts" / host / "usage.jsonl"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps(self.rec(name, mins, conc=conc)) + "\n")
        w = self.m.Worker("borrowed")
        w.info = {"gpus": [{"i": 0, "uuid": "G"}]}
        w.running = {"r%d" % i: {"key": "r%d" % i, "name": "y_s%d" % i, "project": "bp", "t0": time.time(), "gpus": ["G"]} for i in range(5)}
        self.assertEqual(w.k_now(), 6)
        root = self.d / "bp"
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("x_s3|true\n")
        q = self.m.load_queue({"_slug": "k", "root": str(root), "jobs": "jobs/q.txt", "project": "bp"})
        w.queue_durs([q])
        self.assertEqual(w.durs["k"]["x_s3"]["min"], 60.0)
        w.running = {}
        w.queue_durs([q])                                                     # 卡空了：按单跑的
        self.assertEqual(w.durs["k"]["x_s3"]["min"], 20.0)

    def test_hist_across_hosts_and_status(self):
        for host, name in (("29", "x_s1"), ("borrowed", "x_s2")):
            f = self.d / "home" / "hosts" / host / "usage.jsonl"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps(self.rec(name, 60.0, 100)) + "\n")
        w = self.m.Worker("borrowed")
        self.assertEqual(sorted((h["host"], h["name"]) for h in w.hist), [("29", "x_s1"), ("borrowed", "x_s2")])
        self.assertIsNone(self.m.load_hist(w.hist_cache))                      # 文件没变不重读
        root = self.d / "bp"
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("x_s3|true   # est_min=100\nnoest|true\n")
        q = self.m.load_queue({"_slug": "k", "root": str(root), "jobs": "jobs/q.txt", "project": "bp"})
        w.queue_durs([q])
        self.assertEqual(w.durs["k"], {"x_s3": {"min": 60.0, "src": "同类近 2 次 实际/估时 ×0.60", "k": 0.6, "cls": "x", "same": True}})
        self.assertFalse(self.m.fits_before(0, w.req_dur({"key": "k", "kind": "queue", "name": "x_s3"}), 71 * 60))   # 准入看的：清单算好的 × DUR_MARGIN = 72 分钟
        self.assertTrue(self.m.fits_before(0, w.req_dur({"key": "k", "kind": "queue", "name": "x_s3"}), 72 * 60))

    def test_hist_order_and_share(self):
        # 排序与份额都按 t1；没记 project 的旧行（按目录名猜的）只供估时，不进份额；30 天前的也不进
        now = int(time.time())
        f = self.d / "home" / "hosts" / "t" / "usage.jsonl"
        f.parent.mkdir(parents=True)
        rows = [self.rec("x_s2", 60.0, t0=now - 7200, t1=now - 3600, cards=1.0),
                self.rec("x_s1", 60.0, t0=now - 14400, t1=now - 10800, cards=1.0, project=None, root=str(self.d / "bp")),
                self.rec("x_s0", 60.0, t0=now - 40 * 86400, t1=now - 40 * 86400 + 3600, cards=1.0),
                self.rec("y_s0", 60.0, t0=now - 3600, t1=now - 1800, cards=0.5, project="a1")]
        f.write_text("".join(json.dumps(r) + "\n" for r in rows))
        w = self.m.Worker("t")
        self.assertEqual([h["name"] for h in w.hist], ["x_s0", "x_s1", "x_s2", "y_s0"])
        self.assertEqual(self.m.hist_key(w.hist[1], "name"), ("name", "bp", "x"))       # 估时仍按目录名归项目
        use = w.share(now, {"hl": 12 * 3600, "cap": 0, "w": {}})
        self.assertEqual(sorted(use), ["a1", "bp"])
        self.assertEqual([r[0] for r in w.hrows], ["bp", "a1"])


class UsageTest(unittest.TestCase):
    """用假 /proc、假 cgroup 与假 nvidia-smi 核对主机现状与每个任务的用量归属。"""

    def proc(self, pid, comm, sid, hpid, ticks=0, pss_kb=None, start=0, ppid=1):
        d = self.p / str(pid)
        d.mkdir()
        f = ["S", str(ppid), str(sid), str(sid)] + ["0"] * 7 + [str(ticks), "0", "0", "0"] + ["0"] * 4 + [str(start)]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(f) + " 0 0\n")
        (d / "sched").write_text(f"{comm} ({hpid}, #threads: 1)\n---\n")
        (d / "cmdline").write_text(comm)
        if pss_kb is not None:
            (d / "smaps").write_text(f"Rss: {pss_kb * 2} kB\nPss: {pss_kb} kB\nPss: {pss_kb} kB\n")

    def test_sample_attributes_memory_and_gpu(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.p = d / "proc"
            self.p.mkdir()
            (self.p / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
            self.proc(1, "sh", 1, 44101, start=500)          # 容器的 PID 1：启动于开机后 5 秒（CLK_TCK=100）
            self.proc(100, "bash", 100, 5100)                  # 任务会话：bash 加一个 python
            self.proc(101, "python", 100, 5101, ticks=300, pss_kb=512 * 1024)
            self.proc(200, "python2", 200, 5200)               # 容器里别的进程
            self.proc(300, "python", 300, 5300)
            (self.p / "300" / "cmdline").write_text("python\0train_keep_alive.py")
            self.proc(400, "sup", 400, 5400, pss_kb=50 * 1024)     # 登记成清单任务的监督进程，它起的单元（slot）在同一会话里
            self.proc(401, "unit", 400, 5401, pss_kb=256 * 1024, ppid=400)
            cgm = d / "cg" / "memory"
            cgm.mkdir(parents=True)
            (cgm / "memory.limit_in_bytes").write_text(str(122 * 2 ** 30))
            (cgm / "memory.usage_in_bytes").write_text(str(10 * 2 ** 30))
            (cgm / "memory.stat").write_text(f"total_rss {3 * 2 ** 30}\ntotal_shmem {2 ** 30}\ntotal_active_file {4 * 2 ** 30}\ntotal_inactive_file {2 * 2 ** 30}\n")
            (cgm / "memory.failcnt").write_text("3")
            (d / "cg" / "cpu").mkdir()
            (d / "cg" / "cpu" / "cpu.cfs_quota_us").write_text("1700000")
            (d / "cg" / "cpu" / "cpu.cfs_period_us").write_text("100000")
            m = worker_module(d, LABMON_PROC=str(self.p), LABMON_CGROUP=str(d / "cg"))
            env = {"LABMON_GPUS": "GPU-aaa:20000:48000:77",
                   "LABMON_GPU_APPS": "5101:2048:GPU-aaa,5200:1000:GPU-aaa,5300:6886:GPU-aaa,7777:500:GPU-aaa"}
            os.environ.update(env)
            try:
                w = m.Worker("t")
                q = {"key": "k", "root": "/r", "jobs": "jobs/q.txt", "state": str(d / "st")}
                w.running["k"] = {"name": "j", "pid": 100, "t0": time.time(), "q": q, "gpus": ["GPU-aaa"], "vram": 0.0}
                w.running["s"] = {"name": "sup", "pid": 400, "t0": time.time(), "q": q, "gpus": [], "vram": 0.0}
                w.running["u"] = {"name": "unit", "kind": "slot", "pid": 401, "t0": time.time(), "gpus": [], "vram": 0.0}
                w.sample([])
                w.sample([])                      # 第二轮才有区间：核数与所用卡的平均利用率
            finally:
                for k in env:
                    os.environ.pop(k)
            u = w.running["k"]["use"]
            self.assertEqual((u["ram"], u["vram"], u["nproc"]), (1.0, 2048.0, 2))   # PSS 之和，不重复计 RSS
            self.assertEqual(u["util_mean"], 77)
            self.assertEqual(len(w.uwin["GPU-aaa"]), 0)                         # 保活占着这张卡：利用率不进近 10 分钟的样本
            self.assertEqual(u["with_gpu"], ["GPU 保活", "PID 5200（python2）", "PID 7777（labmon 之外）"])
            owners = {g["hpid"]: (g["owner"], g["pid"]) for g in w.gprocs}
            self.assertEqual(owners, {5101: ("job", 101), 5200: ("other", 200), 5300: ("keepalive", 300), 7777: ("other", None)})
            h = w.info
            self.assertEqual((h["mem"]["limit"], h["mem"]["used"], h["mem"]["anon"], h["mem"]["failcnt"]), (122.0, 8.0, 4.0, 3.0))
            self.assertEqual((w.running["s"]["use"]["ram"], w.running["u"]["use"]["ram"]), (0.1, 0.5))   # 单元的内存只记在单元上
            self.assertAlmostEqual(w.other["ram"], 2.4, 1)  # 收不回的 4 − 我们的 1 + 0.1 + 0.5：活跃页缓存不记到别人头上（按工作集是 7）
            self.assertEqual(h["cpu_quota"], 17.0)
            self.assertEqual(h["started"], 1005.0)
            self.assertEqual(h["gpus"][0]["used"], 28000.0)

    def test_guest_keepalive_tree_is_ours(self):
        """借来的主机（#29）：保活带两个 fork 出来的 DataLoader 子进程，整棵进程树的 PSS 都是我们的，不算对方的内存。
        对方在预留以内就照放；对方真超出预留时照旧整台不放，原因里的预留按原值写（5.3 不写成 5）。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.p = d / "proc"
            self.p.mkdir()
            (self.p / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
            self.proc(1, "sh", 1, 44101)
            self.proc(300, "python", 300, 5300, pss_kb=256 * 1024)               # 保活主进程（GPU 上的那个），PSS 0.5 GiB（smaps 两段各 256 MiB）
            (self.p / "300" / "cmdline").write_text("python\0train_keep_alive.py")
            for c in (301, 302):                                                  # DataLoader 子进程：不在 nvidia-smi 的列表里
                self.proc(c, "python", 300, 0, pss_kb=128 * 1024, ppid=300)
            self.proc(400, "node", 400, 5400, pss_kb=2 * 1024 * 1024)             # 对方的 VS Code
            cgm = d / "cg" / "memory"
            cgm.mkdir(parents=True)
            (cgm / "memory.limit_in_bytes").write_text(str(61 * 2 ** 30))

            def mem(anon_gib):
                (cgm / "memory.usage_in_bytes").write_text(str(int((anon_gib + 13) * 2 ** 30)))
                (cgm / "memory.stat").write_text(f"total_active_file {10 * 2 ** 30}\ntotal_inactive_file {3 * 2 ** 30}\n")
            mem(6.0)                                                              # 收不回的 6 GiB = 对方 5 + 保活整棵树 1
            m = worker_module(d, LABMON_PROC=str(self.p), LABMON_CGROUP=str(d / "cg"))
            env = {"LABMON_GPUS": "GPU-aaa:42000:49140:90", "LABMON_GPU_APPS": "5300:6748:GPU-aaa"}
            os.environ.update(env)
            try:
                w = m.Worker("t")
                os.makedirs(w.p.dir)
                w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
                w.sample([])
                self.assertAlmostEqual(w.other["ram"], 5.0, 2)                    # 只扣主进程会是 5.5，超出 5.3
                self.assertNotIn("ram", w.back)
                self.assertIsNone(w.guest_block())
                mem(7.5)                                                          # 对方真涨到 6.5 GiB：整台不放
                w.sample([])
            finally:
                for k in env:
                    os.environ.pop(k)
            self.assertEqual(w.guest_block(), "借来的主机：内存：对方用 6.5 GiB，预留 5.3，暂不放行")

    def test_sticky_keepalive_yields_to_this_round(self):
        """沿用上一轮的保活根只给这一轮 nvidia-smi 里没有的 PID：对方另起、ka.sh 停不了的保活，漏读一轮 guest 指令时被认作 ka.sh 管的，
        下一轮读到指令、归回停不了的（owner other、ka 为真）。两类的内存都不算对方的（#37），核数归回停不了的那份（cpu_room 另扣）。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.p = d / "proc"
            self.p.mkdir()
            (self.p / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
            self.proc(1, "sh", 1, 44101)
            self.proc(300, "python", 300, 5300, pss_kb=512 * 1024, start=777)     # 对方的保活：PSS 1 GiB
            (self.p / "300" / "cmdline").write_text("python\0/root/carol/train_keep_alive.py")
            cgm = d / "cg" / "memory"
            cgm.mkdir(parents=True)
            (cgm / "memory.limit_in_bytes").write_text(str(61 * 2 ** 30))
            (cgm / "memory.usage_in_bytes").write_text(str(int(19 * 2 ** 30)))      # 收不回的 6
            (cgm / "memory.stat").write_text(f"total_active_file {10 * 2 ** 30}\ntotal_inactive_file {3 * 2 ** 30}\n")
            m = worker_module(d, LABMON_PROC=str(self.p), LABMON_CGROUP=str(d / "cg"))
            os.environ.update({"LABMON_GPUS": "GPU-aaa:42000:49140:90", "LABMON_GPU_APPS": "5300:6748:GPU-aaa"})
            try:
                w = m.Worker("t")
                os.makedirs(w.p.dir)
                w.ka_full = "/root/data1/alice/keepalive-demo/keepalive/train_keep_alive.py"
                w.sample([])                                                          # 这一轮没读到 guest 指令：按保活算
                self.assertAlmostEqual(w.other["ram"], 5.0, 2)
                w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
                w.sample([])
                self.assertEqual([(g["owner"], g["ka"]) for g in w.gp], [("other", True)])
                self.assertEqual(w.ka_roots, {300: (777, True)})                     # 按这一轮的归属，不沿用上一轮的“ka.sh 管的”
                self.assertAlmostEqual(w.other["ram"], 5.0, 2)
            finally:
                for k in ("LABMON_GPUS", "LABMON_GPU_APPS"):
                    os.environ.pop(k, None)

    def test_guest_keepalive_sticks_when_unseen(self):
        """保活这一轮没被认出来（nvidia-smi 超时、刚拉起还没建上下文），但上一轮认出的保活进程还在（PID 与启动时刻都对得上）：照样从对方内存里扣，
        不当轮整台挡 10 分钟（维护者 11-08 定）；进程换了（启动时刻变了）就不再算。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.p = d / "proc"
            self.p.mkdir()
            (self.p / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
            self.proc(1, "sh", 1, 44101)
            self.proc(300, "python", 300, 5300, pss_kb=256 * 1024, start=777)
            (self.p / "300" / "cmdline").write_text("python\0train_keep_alive.py")
            for c in (301, 302):
                self.proc(c, "python", 300, 0, pss_kb=128 * 1024, ppid=300)
            cgm = d / "cg" / "memory"
            cgm.mkdir(parents=True)
            (cgm / "memory.limit_in_bytes").write_text(str(61 * 2 ** 30))
            (cgm / "memory.usage_in_bytes").write_text(str(int(19 * 2 ** 30)))      # 收不回的 6 = 对方 5 + 保活树 1
            (cgm / "memory.stat").write_text(f"total_active_file {10 * 2 ** 30}\ntotal_inactive_file {3 * 2 ** 30}\n")
            m = worker_module(d, LABMON_PROC=str(self.p), LABMON_CGROUP=str(d / "cg"))
            os.environ["LABMON_GPUS"] = "GPU-aaa:42000:49140:90"
            try:
                w = m.Worker("t")
                os.makedirs(w.p.dir)
                w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
                os.environ["LABMON_GPU_APPS"] = "5300:6748:GPU-aaa"
                w.sample([])
                os.environ["LABMON_GPU_APPS"] = ""                                    # 这一轮 nvidia-smi 没给出进程
                w.sample([])
                self.assertAlmostEqual(w.other["ram"], 5.0, 2)
                self.assertIsNone(w.guest_block())
                st = (self.p / "300" / "stat").read_text().split()
                st[21] = "888"                                                        # 同一个 PID 换了进程：不再算保活
                (self.p / "300" / "stat").write_text(" ".join(st) + "\n")
                w.sample([])
                self.assertAlmostEqual(w.other["ram"], 6.0, 2)
            finally:
                for k in ("LABMON_GPUS", "LABMON_GPU_APPS"):
                    os.environ.pop(k, None)

    def ext_host(self, d):
        """两卡借来的主机（#37）：对方在卡 1 上另起了保活（脚本不是 ka.sh 认的那个，停不了），带两个 DataLoader 子进程。
        保活整棵树 PSS 1.5 GiB（主进程 1、子进程各 0.25），收不回的内存 5.7 = 对方别的 4.2 + 保活 1.5；预留内存 5.3、CPU 1.1；配额 17 核。
        返回 (worker 模块, 时钟, 设 /proc 里保活树累计 CPU 秒的函数)。"""
        self.p = d / "proc"
        self.p.mkdir()
        (self.p / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
        self.proc(1, "sh", 1, 44101)
        self.proc(300, "python", 300, 5300, pss_kb=512 * 1024, start=777)
        (self.p / "300" / "cmdline").write_text("python\0/root/carol/train_keep_alive.py")
        for c in (301, 302):
            self.proc(c, "python", 300, 0, pss_kb=128 * 1024, ppid=300, start=778)
        self.proc(400, "node", 400, 5400)                                        # 对方的 VS Code（内存在 cgroup 的收不回里）
        cgm = d / "cg" / "memory"
        cgm.mkdir(parents=True)
        (cgm / "memory.limit_in_bytes").write_text(str(61 * 2 ** 30))
        (cgm / "memory.usage_in_bytes").write_text(str(int((5.7 + 13) * 2 ** 30)))
        (cgm / "memory.stat").write_text(f"total_active_file {10 * 2 ** 30}\ntotal_inactive_file {3 * 2 ** 30}\n")
        (d / "cg" / "cpu").mkdir()
        (d / "cg" / "cpu" / "cpu.cfs_quota_us").write_text("1700000")
        (d / "cg" / "cpu" / "cpu.cfs_period_us").write_text("100000")
        m = worker_module(d, LABMON_PROC=str(self.p), LABMON_CGROUP=str(d / "cg"))
        clock = Clock()
        base = time.time()
        clock.time = lambda: base + clock.offset                                 # 冻结真时间：两轮 sample 的间隔恰好是推进的 15 秒，不被机器卡顿掺进零头（核数按这段时长求）
        m.time = clock
        tck = os.sysconf("SC_CLK_TCK")

        def ticks(sec):                                                          # 保活树这一刻的累计 CPU 秒：主进程 sec，两个子进程各 sec / 4
            for pid, s in ((300, sec), (301, sec / 4), (302, sec / 4)):
                st = (self.p / str(pid) / "stat").read_text().split()
                st[13] = str(int(s * tck))
                (self.p / str(pid) / "stat").write_text(" ".join(st) + "\n")
        return m, clock, ticks

    def test_guest_ext_keepalive_not_counted_as_theirs(self):
        """借来的主机（#37 方案 A）：对方另起、ka.sh 停不了的保活整棵树不算对方的用量（与算预留的组内探针同口径）：
        对方别的用量在预留以内时只有保活所在的卡 1 不放，卡 0 照放；保活的核数另从 cpu_room 扣，内存本来就在收不回的里。
        旧做法把整棵树算成对方的（5.7 GiB、2.0 核），内存与 CPU 都超出预留、两张卡都不放。"""
        with tempfile.TemporaryDirectory() as td:
            m, clock, ticks = self.ext_host(Path(td))
            env = {"LABMON_GPUS": "GPU-aaa:42000:49140:0,GPU-bbb:42000:49140:90", "LABMON_GPU_APPS": "5300:6748:GPU-bbb"}
            os.environ.update(env)
            try:
                w = m.Worker("t")
                os.makedirs(w.p.dir)
                w.ka_full = "/root/data1/alice/keepalive/train_keep_alive.py"
                w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
                extra = iter([None, 2.0, 2.0])                                    # labmon 之外此刻占的核：对方别的 0.3 + 保活树 1.7（第一轮没有区间）
                w.other_cpu = lambda jobs, h, t: next(extra)
                for i in range(3):
                    ticks(17 * i)                                                 # 每 15 秒：主进程 17 秒、子进程各 4.25 秒，整棵树 25.5 秒 = 1.7 核
                    w.sample([])
                    clock.offset += 15
            finally:
                for k in env:
                    os.environ.pop(k)
            self.assertEqual([(g["owner"], g["ka"]) for g in w.gp], [("other", True)])
            self.assertAlmostEqual(w.other["ram"], 4.2, 2)                        # 旧做法 5.7，超出 5.3
            self.assertAlmostEqual(w.other["ka_cpu"], 1.7, 2)                     # 25.5 秒 / 15 秒
            self.assertAlmostEqual(w.other["cpu"], 0.3, 2)                        # 旧做法 2.0，连着两轮超出 1.1
            self.assertEqual(list(w.back), ["GPU-bbb"])
            self.assertIn("保活 PID 5300 不归 ka.sh 管、停不了，要停请对方停", w.back["GPU-bbb"])
            self.assertIsNone(w.guest_block())
            self.assertAlmostEqual(w.cpu_room(), 17 - 1.1 - 1.7, 2)              # 上限 − max(对方 0.3, 预留 1.1) − 停不了的保活 1.7
            self.assertAlmostEqual(w.mem_room(), 61 - 5.7 - (5.3 - 4.2), 2)      # 公式不变：保活的内存在收不回的里，对方没用上的预留照留
            S = w.snapshot({})
            self.assertEqual(S["owner"], {"GPU-bbb"})
            r = w.prep(m.new_req("slot", "r", "a1", "u", id="1", pid=1, vram=1.0, ram=1.0, t=clock.time()), clock.time(), {})
            cards, why, _ = m.place(w.as_job(r), S, clock.time())
            self.assertIsNone(why)
            self.assertEqual([c["uuid"] for c in cards], ["GPU-aaa"])

    def run_ext(self, m, clock, ticks, w, rounds):
        """按轮跑 sample：rounds 是 [(LABMON_GPU_APPS, labmon 之外此刻占的核, 保活树累计 CPU 秒)]，每轮之后时钟推 15 秒。"""
        extra = iter([r[1] for r in rounds])
        w.other_cpu = lambda jobs, h, t: next(extra)
        os.environ["LABMON_GPUS"] = "GPU-aaa:42000:49140:0,GPU-bbb:42000:49140:90"
        try:
            for apps, _, sec in rounds:
                os.environ["LABMON_GPU_APPS"] = apps
                ticks(sec)
                w.sample([])
                clock.offset += 15
        finally:
            for k in ("LABMON_GPUS", "LABMON_GPU_APPS"):
                os.environ.pop(k, None)

    def test_guest_ext_keepalive_cores_switch_no_spike(self):
        """保活刚归进“停不了的”这一类（漏读一轮 guest 指令后读到）：各棵保活树分开记累计（ka_cores 按根），归类只决定加进哪一份，
        改类那一轮照常按这一段求差，不会把它启动以来的 CPU 一起算进来、把 cpu_room 扣成负的。"""
        with tempfile.TemporaryDirectory() as td:
            m, clock, ticks = self.ext_host(Path(td))
            w = m.Worker("t")
            os.makedirs(w.p.dir)
            w.ka_full = "/root/data1/alice/keepalive/train_keep_alive.py"
            self.run_ext(m, clock, ticks, w, [("5300:6748:GPU-bbb", None, 1000)])   # 已经跑了很久的保活；这一轮没读到 guest 指令：算 ka.sh 管的
            self.assertEqual(w.other["ka_cpu"], 0.0)
            w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
            self.run_ext(m, clock, ticks, w, [("5300:6748:GPU-bbb", 2.0, 1017)])
            self.assertAlmostEqual(w.other["ka_cpu"], 1.7, 2)                     # 这一段 25.5 秒 / 15 秒，不是 (1000 + 500 + 25.5) / 15

    def test_ext_cores_never_negative(self):
        """没有停不了的保活（自用主机、没借的）：labmon 之外的测量误差（extra 为负，t 与 cgroup 的 cpu_ns 不在同一刻读）不进另扣的核数，
        cpu_room 不超过记账上限（审查阻塞 1 现象 A）。"""
        with tempfile.TemporaryDirectory() as td:
            m, clock, ticks = self.ext_host(Path(td))
            w = m.Worker("t")
            os.makedirs(w.p.dir)
            self.run_ext(m, clock, ticks, w, [("", None, 0), ("", -0.6, 0), ("", -0.6, 0)])
            self.assertEqual(w.other["ka_cpu"], 0.0)
            self.assertEqual(w.cpu_room(), 17)

    def test_ext_cores_ignore_low_extra(self):
        """停不了的保活稳态 1.7 核，这一轮 labmon 之外的只测到 1.0（测量误差）：另扣的核数按 /proc 里整棵树求差，仍约 1.7，不被压低（审查阻塞 1 现象 B）。"""
        with tempfile.TemporaryDirectory() as td:
            m, clock, ticks = self.ext_host(Path(td))
            w = m.Worker("t")
            os.makedirs(w.p.dir)
            w.ka_full = "/root/data1/alice/keepalive/train_keep_alive.py"
            w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
            self.run_ext(m, clock, ticks, w, [("5300:6748:GPU-bbb", None, 0), ("5300:6748:GPU-bbb", 2.0, 17), ("5300:6748:GPU-bbb", 1.0, 34)])
            self.assertAlmostEqual(w.other["ka_cpu"], 1.7, 2)
            self.assertEqual(w.other["cpu"], 0.0)                                 # 对方的照旧截到 0

    def test_ext_cores_nested_roots_by_start(self):
        """同一棵保活树里父、子都有 GPU 上下文（都认作停不了的保活根），父进程一轮漏认、从 ka_roots 补回来：按启动时刻遍历（祖先在前），
        子树仍归父的那份累计，不挪到从 0 起算的子的键上把启动以来的 CPU 一起算进来（复核非阻塞 1：旧顺序那一轮 17.2 核）。"""
        with tempfile.TemporaryDirectory() as td:
            m, clock, ticks = self.ext_host(Path(td))
            (self.p / "301" / "sched").write_text("python (5301, #threads: 1)\n---\n")
            (self.p / "301" / "cmdline").write_text("python\0/root/carol/train_keep_alive.py")
            w = m.Worker("t")
            os.makedirs(w.p.dir)
            w.ka_full = "/root/data1/alice/keepalive/train_keep_alive.py"
            w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
            both = "5300:6748:GPU-bbb,5301:500:GPU-bbb"
            self.run_ext(m, clock, ticks, w, [(both, None, 1000), (both, 2.0, 1017), ("5301:500:GPU-bbb", 2.0, 1034)])   # 第三轮父进程漏认
            self.assertAlmostEqual(w.other["ka_cpu"], 1.7, 2)

    def test_ext_cores_one_tree_exits(self):
        """两张卡上各有一棵停不了的保活，卡 0 那棵退出的那一轮：剩下那棵的核数照算（按根分开记），不因整份之和变小归 0（审查非阻塞 1）。"""
        with tempfile.TemporaryDirectory() as td:
            m, clock, ticks = self.ext_host(Path(td))
            self.proc(500, "python", 500, 5500, start=900)                         # 卡 0 上另一棵：每 15 秒 15 秒 = 1 核
            (self.p / "500" / "cmdline").write_text("python\0/home/bob/keep_alive.py")
            tck = os.sysconf("SC_CLK_TCK")

            def both(sec):
                ticks(sec)
                if (self.p / "500").exists():
                    st = (self.p / "500" / "stat").read_text().split()
                    st[13] = str(int(sec / 17 * 15 * tck))
                    (self.p / "500" / "stat").write_text(" ".join(st) + "\n")
            w = m.Worker("t")
            os.makedirs(w.p.dir)
            w.ka_full = "/root/data1/alice/keepalive/train_keep_alive.py"
            w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
            apps = "5300:6748:GPU-bbb,5500:6000:GPU-aaa"
            self.run_ext(m, clock, both, w, [(apps, None, 0), (apps, 3.0, 17)])
            self.assertAlmostEqual(w.other["ka_cpu"], 2.7, 2)
            import shutil
            shutil.rmtree(self.p / "500")                                          # 卡 0 那棵退出
            self.run_ext(m, clock, both, w, [("5300:6748:GPU-bbb", 2.0, 34)])
            self.assertAlmostEqual(w.other["ka_cpu"], 1.7, 2)
            self.assertEqual(set(w.ka_ticks), {"all", (300, 777)})                 # 退出的那棵不再记

    def test_guest_ext_keepalive_sticks_when_unseen(self):
        """对方另起的保活这一轮没出现在 nvidia-smi 里（超时、漏认），进程还在（PID 与启动时刻对得上）：照样不算对方的内存，
        核数照样另记（ka_roots 记着它是对方的保活）；那张卡这一轮没认出来，回落后 GUEST_QUIET_S 内照样不放（back_last）。"""
        with tempfile.TemporaryDirectory() as td:
            m, clock, ticks = self.ext_host(Path(td))
            os.environ["LABMON_GPUS"] = "GPU-aaa:42000:49140:0,GPU-bbb:42000:49140:90"
            try:
                w = m.Worker("t")
                os.makedirs(w.p.dir)
                w.ka_full = "/root/data1/alice/keepalive/train_keep_alive.py"
                w.ovr = [{"kind": "guest", "target": "a1", "t": 0, "keep": {"vram": 2.0, "ram": 5.3, "cpu": 1.1, "hold": True}}]
                extra = iter([None, 2.0, 2.0])
                w.other_cpu = lambda jobs, h, t: next(extra)
                for i, apps in enumerate(["5300:6748:GPU-bbb", "5300:6748:GPU-bbb", ""]):   # 第三轮 nvidia-smi 没给出进程
                    os.environ["LABMON_GPU_APPS"] = apps
                    ticks(17 * i)
                    w.sample([])
                    clock.offset += 15
            finally:
                for k in ("LABMON_GPUS", "LABMON_GPU_APPS"):
                    os.environ.pop(k, None)
            self.assertEqual(w.gp, [])
            self.assertAlmostEqual(w.other["ram"], 4.2, 2)
            self.assertAlmostEqual(w.other["ka_cpu"], 1.7, 2)
            self.assertIsNone(w.guest_block())
            self.assertEqual(w.snapshot({})["owner"], {"GPU-bbb"})


class PressTest(UnitBase):
    """CPU 与读写的测量（期 1）：假 /proc 与 cgroup 上的主机压力（限流、共享盘、本地盘、PSI）、基线历史 press.jsonl 与 io_base、
    任务的读写与 1 分钟峰值、用量行新字段、清单的 cpus= / io= 与 LABMON_CPUS。时刻走夹具的时钟（Hosts.advance）。"""

    def extra(self):
        return {"LABMON_PROC": str(self.d / "proc"), "LABMON_CGROUP": str(self.d / "cg")}

    def setUp(self):
        super().setUp()
        self.proc, self.cg = self.d / "proc", self.d / "cg"
        (self.proc / "self").mkdir(parents=True)
        (self.cg / "cpu").mkdir(parents=True)
        (self.cg / "blkio").mkdir()
        (self.cg / "blkio" / "blkio.throttle.io_service_bytes").write_text(
            "8:0 Read 4096\n8:0 Write 0\n259:0 Read 32768\n259:0 Write 35700000000\n8:0 Total 4096\nTotal 35700036864\n")
        (self.proc / "self" / "mountinfo").write_text("21 1 0:45 / / rw - overlay overlay rw\n")

    def counters(self, ops=0, ms=0, rbytes=0, periods=0, throttled=0, ios=0, wait=0, busy=0):
        """写一份累计计数：共享盘挂在 worker 的 DATA_ROOT（夹具 HOME 的上一级）上，READ 与 WRITE 各占一半；本地盘 nvme0n1（259:0）。"""
        root = os.path.dirname(self.m.HOME.rstrip("/"))

        def op(name, n, b, t):
            return f"\t{name}: {n} {n} 0 {b if name == 'WRITE' else 0} {b if name == 'READ' else 0} 0 {t} {t}\n"
        (self.proc / "self" / "mountstats").write_text(
            "device rootfs mounted on / with fstype overlay\n"
            f"device 192.0.2.1:/nfs/users/me mounted on {root} with fstype nfs4 statvers=1.1\n"
            "\topts:\trw,vers=4.1,rsize=1048576,clientaddr=192.0.2.7,local_lock=none\n\tper-op statistics\n"
            "\t        NULL: 0 0 0 0 0 0 0 0\n" + op("READ", ops // 2, rbytes // 2, ms // 2) + op("WRITE", ops - ops // 2, rbytes - rbytes // 2, ms - ms // 2)
            + "\t     GETATTR: 999 999 0 1 1 0 9999 9999\n")
        (self.proc / "diskstats").write_text(f" 259       0 nvme0n1 {ios} 0 0 {wait} 0 0 0 0 0 {busy} 0\n   8       0 sda 9 0 0 9 9 0 0 9 0 9 0\n")
        (self.cg / "cpu" / "cpu.stat").write_text(f"nr_periods {periods}\nnr_throttled {throttled}\nthrottled_time 0\n")

    def test_host_press_rates_history_and_reset(self):
        t0 = self.hosts.now()
        self.counters(ops=1000, ms=8000, periods=100, ios=500, wait=1000)
        p = self.w.press(t0)
        self.assertEqual((p["nfs"]["addr"], p["nfs"]["path"], p["nfs"]["ms"], p["nfs"]["base"], p["nfs"]["n"]),
                         ("192.0.2.7", os.path.dirname(self.m.HOME.rstrip("/")), None, 20.0, 0))
        self.assertEqual((p["disk"]["dev"], p["disk"]["busy"], p["thr"], p["psi"]), ("nvme0n1", None, None, None))   # 第一轮没有上一份计数
        # 一分钟里共享盘 120 次、每次 30 ms、读写 120 MiB；限流 60 / 600 个周期；本地盘 100 次、每次 2 ms、忙 30 秒
        self.counters(ops=1120, ms=8000 + 120 * 30, rbytes=120 * 2 ** 20, periods=700, throttled=60, ios=600, wait=1200, busy=30000)
        p = self.w.press(self.hosts.advance(60))
        self.assertEqual((p["nfs"]["ms"], p["nfs"]["ops"], p["nfs"]["mib"]), (30.0, 2.0, 2.0))
        self.assertTrue(p["nfs"]["slow"])                                   # 超过基线 20 ms、每秒 2 次：算慢
        self.assertEqual((p["thr"], p["disk"]["busy"], p["disk"]["await"]), (10.0, 50.0, 2.0))
        self.assertFalse(os.path.exists(self.w.p.press))                 # 还不到 5 分钟：不记历史
        self.counters(ops=1120, ms=11600, rbytes=120 * 2 ** 20, periods=800, throttled=60, ios=600, wait=1200, busy=30000)
        p = self.w.press(self.hosts.advance(self.m.PRESS_EVERY))
        self.assertIsNone(p["nfs"]["ms"])                                   # 这一分钟没有读写：没有每次的时间，不当 0
        rows = self.m.load_jsonl(self.w.p.press)
        self.assertEqual([(r["ms"], r["ops"], r["thr"], r["busy"]) for r in rows], [(30.0, 0.3, 8.6, 8.3)])   # 5 分钟一行：这段的聚合值
        self.assertEqual(rows[0]["t"], round(self.hosts.now()))
        # 容器重启：cgroup 计数归零（限流那项丢掉）；mountstats 是宿主机的，照常求差
        self.counters(ops=1220, ms=11600 + 100 * 5, rbytes=120 * 2 ** 20, periods=5, ios=700, wait=1300, busy=33000)
        p = self.w.press(self.hosts.advance(10))
        self.assertEqual((p["thr"], p["nfs"]["ms"]), (None, 5.0))
        # 有 PSI 的新内核：带上 some avg60
        (self.proc / "pressure").mkdir()
        (self.proc / "pressure" / "io").write_text("some avg10=1.50 avg60=2.25 avg300=0.10 total=123\nfull avg10=0 avg60=0 avg300=0 total=0\n")
        self.assertEqual(self.w.press(self.hosts.advance(10))["psi"], {"io": 2.25})

    def test_unevictable_memory(self):
        # 收不回的内存 = 用量 − 文件页（活跃 + 不活跃页缓存）。#4 在 29（3.10 内核、cgroup v1）的采样：memory.stat 没有 total_shmem，
        # 用量 36.44、活跃 25.48、不活跃 7.37 GiB → 3.59（与 rss 3.57 吻合）；/dev/shm 里另有 20 GiB 没人映射的：它记在 cache 下、
        # 不在 *_file 里，算收不回的（以前取 total_rss 会当成能回收的页缓存）；v2 同理（memory.current − active_file − inactive_file）
        G = 2 ** 30
        cgm = self.cg / "memory"
        cgm.mkdir()
        (cgm / "memory.limit_in_bytes").write_text(str(122 * G))

        def v1(use, cache, rss, act, inact):
            (cgm / "memory.usage_in_bytes").write_text(str(int(use * G)))
            (cgm / "memory.stat").write_text(f"cache {int(cache * G)}\nrss {int(rss * G)}\nactive_file 1\ninactive_file 1\n"
                                             f"total_cache {int(cache * G)}\ntotal_rss {int(rss * G)}\n"
                                             f"total_active_file {int(act * G)}\ntotal_inactive_file {int(inact * G)}\n")
            return self.m.host_info([], "/tmp")["mem"]
        m = v1(36.44, 32.87, 3.57, 25.48, 7.37)
        self.assertEqual((round(m["anon"], 2), round(m["used"], 2)), (3.59, 29.07))
        m = v1(56.44, 52.87, 3.57, 25.48, 7.37)                              # /dev/shm 多了 20 GiB：cache 与用量涨，文件页不变
        self.assertEqual(round(m["anon"], 2), 23.59)
        for f in cgm.iterdir():
            f.unlink()
        cgm.rmdir()
        (self.cg / "memory.max").write_text(str(122 * G))
        (self.cg / "memory.current").write_text(str(int(30.5 * G)))
        (self.cg / "memory.stat").write_text(f"anon {3 * G}\nfile {int(27 * G)}\nshmem {2 * G}\nactive_file {20 * G}\ninactive_file {5 * G}\n")
        m = self.m.host_info([], "/tmp")["mem"]
        self.assertEqual((m["anon"], m["used"]), (5.5, 25.5))                # 匿名 3 + shmem 2 + 内核 0.5；不活跃页缓存不算工作集

    def test_press_write_failure_keeps_sampling(self):
        # press.jsonl 写不进（这里换成目录）：这一轮的主机现状照常更新、准入用新数据；只记一次日志，5 分钟后再试，恢复后照常追加
        self.counters(ops=1000, ms=8000)
        self.w.sample([])
        os.makedirs(self.w.p.press)
        self.w.info = {"stale": True}
        self.counters(ops=1100, ms=9000)
        self.hosts.advance(self.m.PRESS_EVERY)
        self.w.sample([])
        self.assertNotIn("stale", self.w.info)
        self.assertEqual(self.w.info["press"]["nfs"]["ms"], 10.0)
        self.assertEqual(len(self.w.prows), 1)                              # 基线照常用这一段
        t1 = self.w.plast["t"]
        self.w.sample([])                                                   # 不到 5 分钟：不再试
        self.assertEqual(self.w.plast["t"], t1)
        self.hosts.advance(self.m.PRESS_EVERY)
        self.w.sample([])                                                   # 5 分钟后再试，仍失败：日志只记一次
        self.assertGreater(self.w.plast["t"], t1)
        self.assertEqual(Path(self.w.p.log).read_text().count("press.jsonl 写不进"), 1)
        os.rmdir(self.w.p.press)
        self.hosts.advance(self.m.PRESS_EVERY)
        self.w.sample([])                                                   # 恢复：照常追加
        self.assertEqual(len(self.m.load_jsonl(self.w.p.press)), 1)
        self.assertEqual(len(self.w.prows), 3)

    def test_io_base(self):
        now = self.hosts.now()

        def row(ms, age=0, ops=5.0):
            return {"t": now - age - 1, "ms": ms, "ops": ops}
        n = self.m.IO_BASE_N
        self.assertEqual(self.m.io_base([row(500.0)] * (n - 1), now), (20.0, n - 1))   # 不够一天：取 20 ms
        rows = [row(float(k)) for k in range(1, 301)] + [row(None)] * 50 + [row(900.0, self.m.PRESS_KEEP_S)] * 50
        self.assertEqual(self.m.io_base(rows, now), (285.0, 300))         # 95 分位；没读写的、14 天前的不算
        self.assertEqual(self.m.io_base([row(3.0)] * 400, now), (20.0, 400))   # 平时很快：不低于 20 ms
        self.assertEqual(self.m.io_base([row(900.0, ops=1.0)] * 300 + [row(30.0)] * 300, now), (30.0, 300))   # 每秒不到 2 次的样本不算

    def test_press_history_kept_14_days_and_reloaded(self):
        now = self.hosts.now()
        old = [{"t": now - self.m.PRESS_KEEP_S - 2 * 86400, "ms": 9.0}, {"t": now - 3600, "ms": 4.0}]
        Path(self.w.p.press).write_text("".join(json.dumps(r) + "\n" for r in old))
        w = self.m.Worker("t")                                            # worker 重启：只读回近 14 天的
        self.assertEqual([r["ms"] for r in w.prows], [4.0])
        self.m.append_jsonl(w.p.press, {"t": round(now), "ms": 5.0}, self.m.PRESS_KEEP_S)   # 第一行超出保留期一天以上：整份重写
        self.assertEqual([r["ms"] for r in self.m.load_jsonl(w.p.press)], [4.0, 5.0])
        self.m.append_jsonl(w.p.press, {"t": round(now), "ms": 6.0}, self.m.PRESS_KEEP_S)
        self.assertEqual(len(self.m.load_jsonl(w.p.press)), 3)

    def test_local_dev(self):
        self.assertEqual(self.m.local_dev(), "259:0")                     # cgroup 里本容器读写最多的那块
        (self.cg / "blkio" / "blkio.throttle.io_service_bytes").write_text("Total 0\n")
        self.assertIsNone(self.m.local_dev())                             # /tmp 在 overlay 上（假设备 0:45）：不算
        (self.proc / "self" / "mountinfo").write_text("21 1 0:45 / / rw - overlay overlay rw\n30 21 259:2 / /tmp rw - ext4 /dev/nvme0n1p2 rw\n")
        self.assertEqual(self.m.local_dev(), "259:2")

    def io(self, pid, rchar, wchar=0):
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / "io").write_text(f"rchar: {rchar}\nwchar: {wchar}\nsyscr: 1\nsyscw: 1\nread_bytes: 0\nwrite_bytes: {wchar}\n")

    def test_job_io_peaks_and_usage_row(self):
        # 精确采样间隔只由advance推进，CI文件读写耗时不应改变预期峰值。
        base = self.hosts.now() - self.hosts.clock.offset
        self.enterContext(mock.patch.object(self.hosts.clock, "time", side_effect=lambda: base + self.hosts.clock.offset))
        M, tck = 2 ** 20, os.sysconf("SC_CLK_TCK")
        j = {"key": "s", "name": "u", "kind": "slot", "id": "1", "pid": 100, "t0": self.hosts.now(), "gpus": [], "project": "p", "cpus": 4.0, "io": "heavy"}
        self.w.running["s"] = j

        def step(dt, cpu_s, io):
            for pid, (r, w_) in io.items():
                self.io(pid, r, w_)
            procs = {pid: {"ticks": cpu_s * tck if pid == 100 else 0} for pid in io}
            self.w.measure(j, sorted(io), procs, [], {}, {}, [j], self.hosts.advance(dt))
            return j["use"]
        step(0, 0, {100: (0, 0), 101: (0, 0)})                            # 第一轮：只记基线
        u = step(10, 20, {100: (10 * M, 0), 101: (20 * M, M)})
        self.assertEqual((u["rd"], u["wr"], u["rd_b"], u["cores"]), (3.0, 0.1, 30 * M, 2.0))
        step(30, 80, {100: (60 * M, 0), 101: (120 * M, M)})
        u = step(10, 100, {100: (180 * M, M)})                            # 子进程退出、被会话里的父进程回收：计数并进父进程，合计不变
        self.assertEqual((u["rd"], u["rd_b"]), (0.0, 180 * M))
        u = step(25, 150, {100: (180 * M, M)})                            # 窗口满 60 秒（从第二轮起）：150 MiB / 65 秒 → 峰值
        self.assertEqual((u["rd_peak"], u["cores_peak"]), (2.31, 2.0))
        self.assertEqual((u["pks"], u["pk0"]), ([[round(self.hosts.now()), 2.0]], round(self.hosts.now()) - 75))   # 近 CPU_WARM_S 的窗口核数（记账回收用）
        self.assertNotIn("conc", u)                                           # 不用卡的：不记同卡并行数
        u = step(10, 170, {100: (100 * M, M)})                            # 回收者不在树里：合计变小那一轮按 0 算
        self.assertEqual((u["rd"], u["rd_b"]), (0.0, 180 * M))
        self.w.finish("s")
        rec = self.m.load_jsonl(self.w.p.usage)[-1]
        self.assertEqual((rec["rd_b"], rec["wr_b"], rec["rd_peak"], rec["cores_peak"], rec["cpus"], rec["io"]), (180 * M, M, 2.31, 2.0, 4.0, "heavy"))

    def test_conc_time_weighted(self):
        # #67：同卡平均几条 labmon 任务（含自己，按时间加权；占位任务不算），写进用量行的 conc
        tck = os.sysconf("SC_CLK_TCK")
        j = {"key": "s", "name": "u", "kind": "slot", "id": "1", "pid": 100, "t0": self.hosts.now(), "gpus": ["G"], "project": "p"}
        o = {"key": "o", "name": "o", "pid": 200, "gpus": ["G"]}
        h = {"key": "h", "name": "hold_x", "pid": 300, "gpus": ["G"]}
        self.w.running["s"] = j
        procs = {100: {"ticks": tck}}
        self.io(100, 0)
        self.w.measure(j, [100], procs, [], {}, {}, [j], self.hosts.advance(0))
        self.w.measure(j, [100], procs, [], {}, {}, [j, h], self.hosts.advance(30))     # 30 秒只有自己（占位任务不算）
        self.w.measure(j, [100], procs, [], {}, {}, [j, o], self.hosts.advance(90))     # 90 秒有两条
        self.assertEqual(j["use"]["conc"], 1.75)
        self.w.finish("s")
        self.assertEqual(self.m.load_jsonl(self.w.p.usage)[-1]["conc"], 1.75)

    def test_hints_cpus_io_and_labmon_cpus(self):
        h = self.m.hints("python x.py --cpus=8 io=medium   # cpus=4 io=heavy")
        self.assertEqual((h["cpus"], h["io"]), (4.0, "heavy"))
        h = self.m.hints("python x.py --cpus=8 --io=light")
        self.assertEqual((h["cpus"], h["io"]), (None, None))                 # 命令自己的参数不算
        # 取整口径（记账与 LABMON_CPUS 同一个）：四舍五入至少 1，写明 0 的是 0；配额向下取整
        self.assertEqual([self.m.job_cpus(v) for v in (4.0, 1.4, 1.5, 0.3, 0)], [4, 1, 2, 1, 0])
        self.assertEqual((self.m.cpu_cap(7.5), self.m.cpu_cap(17.0), self.m.cpu_cap(0.5), self.m.cpu_cap(None)), (7, 17, 1, None))
        self.assertIsNone(self.m.cpu_quota())                                # 没设上限
        (self.cg / "cpu" / "cpu.cfs_quota_us").write_text("1700000\n")
        (self.cg / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
        self.assertEqual(self.m.cpu_quota(), 17.0)
        # LABMON_CPUS 是给这个任务记账的核数（estimate）：没写 cpus= 又没历史是缺省 1 核，不再是整份配额；写了 cpus=0 的导出 1
        q, = self.hosts.queues({"c": ["a|echo $LABMON_CPUS > a.out   # gpus=0", "b|echo $LABMON_CPUS > b.out   # gpus=0 cpus=3",
                                      "z|echo $LABMON_CPUS > z.out   # gpus=0 cpus=0"]}, "p")
        for d in (q["state"], q["logs"]):
            os.makedirs(d)
        self.w.info = {"cpu_quota": 17.0}
        for name, cmd in self.m.list_jobs(q):
            r = self.w.prep(self.m.new_req("queue", "c", "p", q=q, root=q["root"], queue=q["jobs"], **dict(self.m.hints(cmd), name=name, cmd=cmd.strip())), time.time(), {})
            self.w.start(q, r, [])
            t = time.time()
            while not Path(q["state"], name + ".rc").exists() and time.time() - t < 10:
                time.sleep(0.05)
            Path(q["state"], "current").unlink()                         # 同 finish 收尾：start 见 current 还在就不放（#88）
        self.assertEqual([Path(q["root"], n + ".out").read_text().strip() for n in "abz"], ["1", "3", "1"])
        os.makedirs(self.m.SLOTS)
        self.w.grant(self.req(key="slot:1", cpus=2.0), [])
        self.assertEqual(json.loads(Path(self.m.SLOTS, "1.ok").read_text())["cpus"], 2)

    def test_peers_read_status_with_press_fields(self):
        # 别的主机写的状态带 info.press、运行中任务带读写，用量记录有新字段也有旧行：本机照常读进份额与历史、写出自己的 info.press
        o = {"thr": 2.0, "psi": None, "nfs": {"path": "/data", "addr": "192.0.2.7", "ms": 4.0, "ops": 1.0, "mib": 0.5, "base": 20.0, "n": 3, "need": 288, "floor": 20},
             "disk": {"dev": "nvme0n1", "busy": 1.0, "await": 0.2}}
        now = self.hosts.now()
        self.hosts.status("o", info={"gpus": [], "mem": {}, "disk": [], "started": now - 3600, "press": o},
                          share={"wait_cap_min": 30, "half_life_h": 24, "weight": {}, "use": {}, "run": [["pb", 1.0, now - 600, now + 600]]},
                          running=[running_job("x", "pb", use={"cores": 1.0, "rd": 3.0, "rd_b": 2 ** 30, "cores_peak": 1.5})])
        old = {k: v for k, v in usage_row("old", "pb", now - 7200, now - 3600, ram_peak=1.0).items()
               if k not in ("cores_peak", "rd_peak", "wr_peak", "rd_b", "wr_b", "cpus", "io")}   # 期 1 之前的 worker 写的：没有这些键
        self.hosts.usage("o", [old,
                               usage_row("new", "pb", now - 3000, now - 600, ram_peak=1.0, cores_peak=1.2, rd_b=2 ** 30, rd_peak=40.0, cpus=2.0, io="light")])
        q = self.hosts.queues({"a": ["a1|sleep 30   # gpus=0"]}, "pa")
        try:
            self.w.tick(q, True)
            st = json.loads(Path(self.w.p.status).read_text())
            self.assertEqual(set(st["info"]["press"]), {"thr", "psi", "nfs", "disk"})
            self.assertEqual(self.w.peer, [("pb", 1.0, now - 600, now + 600)])
            self.assertEqual({h["name"]: h.get("rd_peak") for h in self.w.hist if h["host"] == "o"}, {"old": None, "new": 40.0})
            self.assertEqual({j["name"] for j in self.w.running.values()}, {"a1"})
        finally:
            for j in self.w.running.values():
                os.killpg(j["pid"], 9)


class IoYieldTest(UnitBase):
    """共享盘慢时让路（扫描、拷本地依赖推迟，最多推迟 IO_YIELD_MAX_S）与“读写拥塞嫌疑”事件（只记录、不拦）。时刻走夹具的时钟。"""

    NF = {"path": "/data", "addr": "192.0.2.7", "ms": 80.0, "ops": 5.0, "mib": 3.0, "base": 25.0, "n": 300, "need": 288, "floor": 20, "min_ops": 2}

    def test_nfs_slow(self):
        slow = self.m.nfs_slow
        self.assertTrue(slow(self.NF))
        self.assertFalse(slow(dict(self.NF, ops=1.5)))                     # 次数太少：几次小写不决定慢不慢
        self.assertFalse(slow(dict(self.NF, ms=25.0)))                     # 不超过基线
        self.assertFalse(slow(dict(self.NF, ms=None)))                     # 这一分钟没有读写
        self.assertFalse(slow(None))

    def test_yield_defers_with_cap(self):
        # 慢的时候推迟，同一件连着推迟满 30 分钟就照做一次、计时清零；不慢了立即照做；开始推迟时记一次日志
        self.w.slow = self.NF
        t = self.hosts.now()
        self.assertIn("共享盘慢（此刻 80 ms/次，本机基线 25）", self.w.io_yield("scan", t))
        self.assertTrue(self.w.io_yield("scan", t + self.m.IO_YIELD_MAX_S - 1))
        self.assertIsNone(self.w.io_yield("scan", t + self.m.IO_YIELD_MAX_S))
        self.assertTrue(self.w.io_yield("scan", t + self.m.IO_YIELD_MAX_S + 10))   # 重新计时
        self.w.slow = None
        self.assertIsNone(self.w.io_yield("scan", t + self.m.IO_YIELD_MAX_S + 20))
        self.assertEqual(self.w.defer, {})
        log = Path(self.w.p.log).read_text()
        self.assertEqual((log.count("推迟共享盘扫描"), log.count("已推迟满 30 分钟，照做一次共享盘扫描")), (2, 1))

    def test_disk_scan_yields(self):
        # 共享盘扫描：慢时不开始新的一件（状态里 disk.yield 写着），不慢了照常开始
        root = self.d / "data1"
        (root / "alice").mkdir(parents=True)
        d = self.m.Disk(str(root), "t")
        d.step(self.hosts.now(), lambda key, now: "共享盘慢")
        self.assertEqual((d.job, d.brief()["yield"]), (None, "共享盘慢"))
        d.step(self.hosts.now())
        self.assertEqual((d.job[1], d.brief()["yield"]), ("list", None))
        d.job[0].join()

    def test_stage_deferred(self):
        # 拷本地依赖：慢时不起拷贝线程，等待原因写明推迟；推迟满 30 分钟照拷
        src, dst = self.d / "a.pt", self.d / "local" / "a.pt"
        src.write_bytes(b"x" * 10)
        r = {"key": "k", "name": "u", "project": "p"}
        self.w.slow = self.NF
        why = self.w.stage_bg(r, [(str(src), str(dst))])
        self.assertEqual(why, "共享盘慢（此刻 80 ms/次，本机基线 25），推迟拷 1 个本地依赖（最多推迟 30 分钟）")
        self.assertEqual(self.w.stagers, {})
        self.hosts.advance(self.m.IO_YIELD_MAX_S)
        # 拷贝停在闸门上，看完“正在拷”再放行：只拷 10 字节，不拦着的话线程可能在 is_alive 之前就跑完（#178）
        hold, real = threading.Event(), self.m.stage
        with mock.patch.object(self.m, "stage", lambda pairs: (hold.wait(10), real(pairs))[1]):
            self.assertIn("正在从共享盘拷", self.w.stage_bg(r, [(str(src), str(dst))]))
            hold.set()
            self.w.stagers[("k", "u")]["th"].join()
        self.assertIsNone(self.w.stage_bg(r, []))
        self.assertEqual(dst.read_bytes(), b"x" * 10)

    def suspect_setup(self, age_min):
        now = self.hosts.now()
        self.w.hist = [usage_row("train_s%d" % k, "p", now - 86400, now - 86400 + 600, host="o") for k in range(3)]   # 同类跑 10 分钟
        self.w.running["k"] = {"key": "k", "kind": "queue", "name": "train_s9", "project": "p", "t0": now - age_min * 60, "gpus": [], "est_min": None,
                               "est": {"ram": 1.0, "vram": 0.0, "cpus": 1}, "use": {}}
        self.w.running["h"] = {"key": "h", "kind": "queue", "name": "hold_x", "project": "p", "t0": now - 3600, "gpus": [], "est_min": 5,
                               "est": {"ram": 0.0, "vram": 0.0, "cpus": 0}, "use": {}}
        return now

    def test_io_suspect_once_and_no_admission_change(self):
        # 已跑过同类历史的 1.5 倍、共享盘又慢：记一条 io_suspect，同一任务只记一次；不慢、还没到 1.5 倍、占位任务、没有同类历史的都不记；
        # 不改准入：记事件前后的资源快照一样
        now = self.suspect_setup(14)
        self.w.slow = self.NF
        self.w.io_suspect(now)
        self.assertEqual(self.events("io_suspect"), [])                    # 14 分钟 < 10 × 1.5
        self.hosts.advance(60)
        self.w.slow = None
        self.w.io_suspect(now + 60)
        self.assertEqual(self.events("io_suspect"), [])                    # 共享盘不慢
        freeze = mock.patch.object(self.m.time, "time", return_value=now + 61)   # 快照里超了预计的在跑任务按现在估剩余：两次取同一个现在
        with freeze:
            before = self.w.snapshot({})
        self.w.slow = self.NF
        self.w.io_suspect(now + 61)
        self.w.io_suspect(now + 120)
        ev = self.events("io_suspect")
        self.assertEqual([(e["name"], e["ms"], e["base"]) for e in ev], [("train_s9", 80.0, 25.0)])
        self.assertIn("读写拥塞嫌疑：train_s9 已跑 15 分钟，同类历史约 10 分钟（同类近 3 次实际用时）", ev[0]["msg"])
        self.assertEqual(self.w.running["k"]["io_sus"], round(now + 61))
        self.assertNotIn("io_sus", self.w.running["h"])
        with freeze:
            self.assertEqual(self.w.snapshot({}), before)
        self.w.running["k"].pop("io_sus")
        self.w.hist = []                                                   # 只有清单估时：不是同类历史，不记
        self.w.running["k"]["est_min"] = 5
        self.w.io_suspect(now + 180)
        self.assertEqual(len(self.events("io_suspect")), 1)

    def test_io_suspect_by_own_concurrency(self):
        # #86：挤着跑的任务和并行数相近的同类记录比，不和单跑的比：同类单跑 10 分钟、并行 6 条 40 分钟，它并行约 6 条、跑了 20 分钟不记嫌疑
        now = self.suspect_setup(20)
        self.w.hist = ([usage_row("train_s%d" % k, "p", now - 86400, now - 86400 + 600, host="o", conc=1.0) for k in range(3)]
                       + [usage_row("train_s%d" % k, "p", now - 7200, now - 7200 + 2400, host="o", conc=6.0) for k in range(3, 6)])
        self.w.running["k"]["use"]["conc"] = 6.0
        self.w.slow = self.NF
        self.w.io_suspect(now)
        self.assertEqual(self.events("io_suspect"), [])
        self.w.io_suspect(now + 41 * 60)                                     # 61 分钟 > 40 × 1.5
        ev, = self.events("io_suspect")
        self.assertIn("同类历史约 40 分钟（同类近 3 次（同卡并行约 6 条）实际用时）", ev["msg"])

    def test_io_suspect_lists_same_machine(self):
        # 多主机：同一物理机（共享盘 clientaddr 相同）上别的容器在跑的任务列进事件；别的物理机的、状态过期的、占位任务不列
        now = self.suspect_setup(20)
        same, other = {"nfs": dict(self.NF)}, {"nfs": dict(self.NF, addr="192.0.2.99")}
        self.hosts.status("o", info={"gpus": [], "mem": {}, "disk": [], "started": now - 3600, "press": same},
                          running=[running_job("x1", "pb"), running_job("hold_y", "pb", hold=True)])
        self.hosts.status("q", info={"gpus": [], "mem": {}, "disk": [], "started": now - 3600, "press": other}, running=[running_job("far", "pc")])
        self.hosts.status("s", age=self.m.STATUS_STALE_S + 60, info={"gpus": [], "mem": {}, "disk": [], "started": now - 3600, "press": same},
                          running=[running_job("old", "pd")])
        self.w.info = {"press": same}
        self.w.peers(now)
        self.assertEqual(self.w.peer_box, [("o", "pb", "x1")])
        self.w.slow = self.NF
        self.w.io_suspect(now)
        ev, = self.events("io_suspect")
        self.assertEqual(ev["box"], ["o 的 x1"])
        self.assertIn("同一物理机上还在跑：o 的 x1", ev["msg"])
        self.assertIn("80 ms/次（本机基线 25），5.0 次/秒", ev["msg"])        # 次数只用本机读到的这一份，不按容器数相加


class LocalDiskTest(UnitBase):
    """容器根盘开机以来的新写入（#149）：LocalDisk 在假根盘上扫（只进开机以后动过的目录、跳过 /proc 等、labmon 目录拆到各项目）、
    借来的主机只数借来以后的、labmon 之外的新写入变了 LOCAL_DRIFT 才提前重扫；rootfs_info 的可信判定与 /dev/shm；scan_local 的接线。
    df 用假的 statvfs（根盘 750 GiB 配额、宿主盘 3.5 TiB，已用 self.used）：真的会随跑测试这台机器的写入变。"""

    def extra(self):
        self.root = self.d / "root"
        self.used = 2 * 2 ** 30
        return {"LABMON_ROOTFS": str(self.root), "LABMON_LOCAL": str(self.root / "tmp" / "labmon")}

    def put(self, rel, n):
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * n)

    def mark(self):
        """一个与前后写入的 ctime 分得开的时刻。"""
        time.sleep(0.05)
        t = time.time()
        time.sleep(0.05)
        return t

    def df(self, host=3584):
        def f(path):
            if str(path).endswith("etc/hosts"):
                if host is None:
                    raise OSError("no such file")
                tot = host * 2 ** 30
            else:
                tot = 750 * 2 ** 30
            return SimpleNamespace(f_blocks=tot // 4096, f_bfree=(tot - self.used) // 4096, f_bavail=(tot - self.used) // 4096, f_frsize=4096)
        return f

    def scan(self, d, now):
        """一直走到没有该做的：返回做了的几件（"list" 或单位路径）。"""
        out = []
        with mock.patch.object(os, "statvfs", self.df()):
            for _ in range(200):
                d.step(now)
                if not d.job:
                    return out
                out.append(d.job[1])
                d.job[0].join()
        return out

    def R(self, p):
        return f"{self.root}/{p}"

    def test_scan_new_writes(self):
        self.put("usr/lib/big.so", 400 * 1024)                                    # 镜像里的（开机以前的）
        self.put("etc/old.conf", 4096)
        boot = self.mark()
        for rel, n in (("root/.cache/hf/model.bin", 400 * 1024), ("root/.cache/pip/x.whl", 40 * 1024), ("root/notes.txt", 4096),
                       ("data/out/run1/ckpt.pt", 800 * 1024), ("tmp/other.bin", 4096), ("proc/self/junk", 4096),
                       ("tmp/labmon/cache/proj-a/w.pt", 12 * 1024), ("tmp/labmon/jobs/proj-a/j1/s.bin", 8 * 1024)):
            self.put(rel, n)
        d = self.m.LocalDisk("t")
        d.setup(boot, boot)
        now = self.hosts.now() + 10
        self.scan(d, now)
        R = self.R
        self.assertFalse([u for u in d.ix["units"] if u.startswith((R("usr"), R("etc"), R("proc")))])   # 开机以前的目录整棵不进；/proc 不进
        s = d.summary()
        w = s["wrote"]
        self.assertEqual((w["since"], w["n"], w["pending"], w["more"]), (boot, 5, 0, [0, 0]))       # labmon 目录不算
        self.assertEqual([x[0] for x in w["top"][:3]], [R("data/out/run1"), R("root/.cache/hf"), R("root/.cache/pip")])   # 按前三级汇总、按占用排
        self.assertEqual(sorted(x[0] for x in w["top"][3:]), [R("root/."), R("tmp/.")])              # 一、二级目录本层的文件
        self.assertEqual(w["b"], sum(x[1] for x in w["top"]))
        self.assertTrue(all(x[2] and x[2] >= boot - 1 for x in w["top"]))                             # 最近写入取 ctime
        a = s["local"]["proj-a"]
        self.assertEqual(set(s["local"]), {"proj-a"})                                                    # labmon 目录拆到各项目的缓存与单格临时
        self.assertTrue(a["cache"] >= 12 * 1024 and a["scratch"] >= 8 * 1024)
        self.assertGreaterEqual(s["own"], a["cache"] + a["scratch"])
        self.assertTrue((self.root / "tmp" / "labmon" / "disk.json").exists())                         # 索引在容器本地（续租即清）

        # 对账：labmon 之外的新写入变了不到 LOCAL_DRIFT 只列目录；变了这么多，最近一天有写入的单位提前重扫（大文件追加，目录修改时刻看不出）
        with open(self.root / "data/out/run1/ckpt.pt", "ab") as f:
            f.write(b"y" * 400 * 1024)
        self.used += 2 ** 20
        self.assertEqual(self.scan(d, now + 601), ["list"])
        self.used += 100 * 2 ** 20
        got = self.scan(d, now + 1202)
        self.assertEqual(got[0], "list")
        self.assertIn(R("data/out"), got)
        self.assertGreaterEqual(d.summary()["wrote"]["top"][0][1], 1200 * 1024)

    def test_borrowed_counts_since_borrow(self):
        boot = self.mark()
        self.put("root/.cache/hf/theirs.bin", 400 * 1024)                         # 借来以前对方写的
        since = self.mark()
        self.put("root/.cache/hf/ours.bin", 40 * 1024)                            # 借来以后：在旧的上层目录里新建（只按借来时刻剪枝会漏掉它）
        self.put("work/new/x.bin", 8 * 1024)
        d = self.m.LocalDisk("t")
        d.setup(boot, since, True)
        self.scan(d, self.hosts.now() + 10)
        w = d.summary()["wrote"]
        R = self.R
        self.assertEqual((w["since"], w["n"]), (since, 2))
        self.assertEqual({x[0] for x in w["top"]}, {R("root/.cache/hf"), R("work/new/x.bin")})
        self.assertLess(w["b"], 400 * 1024)                                                             # 对方的那个不算
        n = len(d.ix["units"])
        d.setup(boot, since + 100, True)                                                                # 借用中 guest 指令重下：取这次借用里最早的时刻，索引不作废
        self.assertEqual((d.cfg["since"], len(d.ix["units"])), (since, n))
        d.setup(boot, boot)                                                                             # 还了（或不是借来的）：换扫法，索引作废
        self.assertEqual((d.cfg["since"], d.ix), (boot, {"cfg": d.cfg}))

    def test_split_units_count_to_project(self):
        # labmon 目录里扫得太慢被拆开的单位（cache/proj-a/models、本层的 cache/proj-a/.、jobs/proj-a/j1）照样按前两段算到所属项目（PR #152 Codex 指出）；
        # cache、jobs 本层的文件不归哪个项目，只进 own
        d = self.m.LocalDisk("t")
        d.setup(None, None)
        L = str(self.root / "tmp" / "labmon")
        d.ix["units"] = {f"{L}/cache/proj-a/models": {"b": 300, "n": 2}, f"{L}/cache/proj-a/.": {"b": 20, "n": 1}, f"{L}/jobs/proj-a/j1": {"b": 50, "n": 1},
                         f"{L}/cache/proj-b": {"b": 7, "n": 1}, f"{L}/cache/.": {"b": 5, "n": 1}, f"{L}/.": {"b": 3, "n": 1}}
        s = d.summary()
        self.assertEqual(s["local"], {"proj-a": {"cache": 320, "scratch": 50}, "proj-b": {"cache": 7, "scratch": 0}})
        self.assertEqual(s["own"], 385)

    def test_local_only_when_unsure(self):
        # 根盘测不准（不是 overlay、没配额、不知道开机时刻）：只扫 labmon 目录，给各项目的缓存与单格临时，不给 labmon 之外的新写入
        self.put("root/notes.txt", 4096)
        self.put("tmp/labmon/cache/proj-a/w.pt", 12 * 1024)
        d = self.m.LocalDisk("t")
        d.setup(None, None)
        self.scan(d, self.hosts.now() + 10)
        L = str(self.root / "tmp" / "labmon")
        self.assertTrue(all(u.startswith(L + "/") for u in d.ix["units"]))
        s = d.summary()
        self.assertIsNone(s["wrote"])
        self.assertGreaterEqual(s["local"]["proj-a"]["cache"], 12 * 1024)

    def test_rootfs_info(self):
        proc = self.d / "proc"
        proc.mkdir()
        self.m.PROC = str(proc)
        mounts = proc / "mounts"
        ov = "overlay / overlay rw,relatime,lowerdir=/l,upperdir=/u,workdir=/w 0 0\n"
        self.used = 3 * 2 ** 30
        with mock.patch.object(os, "statvfs", self.df()):
            mounts.write_text(ov + "tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n")                       # 与宿主共用的 /dev/shm（29 就是）：不算这台的
            r = self.m.rootfs_info()
            self.assertEqual((r["fs"], r["ok"], r["used"], r["total"], "shm" in r), ("overlay", True, 3.0, 750.0, False))
            mounts.write_text(ov + "shm /dev/shm tmpfs rw,nosuid,nodev,noexec,relatime,size=65536k 0 0\n")   # 容器私有的：续租同样清掉
            self.assertEqual(self.m.rootfs_info()["shm"], 3.0)
            mounts.write_text("/dev/sda1 / ext4 rw 0 0\n")
            self.assertEqual(self.m.rootfs_info()["ok"], False)                                      # 不是 overlay：测不准
        mounts.write_text(ov)
        with mock.patch.object(os, "statvfs", self.df(host=750)):
            self.assertEqual(self.m.rootfs_info()["ok"], False)                                      # 根盘与宿主盘一样大：没配额，已用不只是这个容器的
        with mock.patch.object(os, "statvfs", self.df(host=None)):
            self.assertEqual(self.m.rootfs_info()["ok"], False)                                      # 看不出宿主盘

    def test_worker_wiring(self):
        # scan_local：根盘测得准、知道开机时刻时扫整个根盘，数开机以来的；借来的主机数借来以后的；测不准时 wrote 为 None
        st = time.time() - 60
        self.put("root/w.bin", 8192)
        w = self.w

        def go():
            now = self.hosts.now() + 10
            with mock.patch.object(os, "statvfs", self.df()):
                for k in range(60):
                    w.scan_local([], now + k)
                    if w.ldisk.job:
                        w.ldisk.job[0].join()
            return w.storage

        w.info = {"started": st, "rootfs": {"ok": True}}
        s = go()
        self.assertEqual((s["wrote"]["since"], s["wrote"]["n"], s["projects"]), (round(st), 1, {}))
        time.sleep(1.1)                                                                                 # scan_local 把时刻取整：前后各留出余量
        g = time.time() - 0.5
        w.ovr = [{"kind": "guest", "target": None, "t": g}]
        self.put("root/ours.bin", 4096)
        s = go()
        self.assertEqual((s["wrote"]["since"], s["wrote"]["n"]), (round(g), 1))                       # 借来以前的 w.bin 不算
        w.ovr = [{"kind": "guest", "target": None, "t": g + 30}]                                          # 指令重下：沿用最早的
        self.assertEqual(go()["wrote"]["since"], round(g))
        w.info = {"started": st, "rootfs": {"ok": False}}
        self.assertIsNone(go()["wrote"])


class ScanTimingTest(UnitBase):
    """扫描的节奏（#153）：耗时按扫描本身记（不是主循环的一拍）、做完一件不等下一拍就接着下一件（Worker.scan_chain）、
    一件接一件时合起来照样守限速、共享盘慢时照样让路、接连做的时候索引不每件都写。共享盘 Disk 与本地盘 LocalDisk 各测一遍。"""

    NF = IoYieldTest.NF

    def extra(self):
        self.root = self.d / "root"
        return {"LABMON_ROOTFS": str(self.root), "LABMON_LOCAL": str(self.root / "tmp" / "labmon")}

    def tree(self):
        """共享盘与本地盘各放几个小目录，返回 (共享盘, 本地盘) 两个扫描者。"""
        data = self.d / "data1"
        for rel in ("alice/a/x.bin", "alice/b/y.bin", "bob/c/z.bin", "top.sh"):
            (data / rel).parent.mkdir(parents=True, exist_ok=True)
            (data / rel).write_bytes(b"x" * 100)
        L = self.root / "tmp" / "labmon"
        for rel in ("cache/proj-a/w.pt", "cache/proj-b/w.pt", "jobs/proj-a/j1/s.bin"):
            (L / rel).parent.mkdir(parents=True, exist_ok=True)
            (L / rel).write_bytes(b"x" * 100)
        ld = self.m.LocalDisk("t")
        ld.setup(None, None)
        return self.m.Disk(str(data), "t"), ld

    def test_cost_is_scan_itself(self):
        # 隔一拍（10 秒）才收结果：记的耗时是扫描本身的，扫完的时刻也不是收结果的那一拍（旧代码两者都按收的时刻，29 上每个单位都记成 10.3 秒）
        for d in self.tree():
            with self.subTest(d=type(d).__name__):
                now = self.hosts.now()
                d.step(now)
                self.assertEqual(d.job[1], "list")
                d.job[0].join()
                d.step(now + 10)
                self.assertLessEqual(d.ix["listed"], round(now) + 1)
                u = d.job[1]
                d.job[0].join()
                d.step(now + 20)
                r = d.ix["units"][u]
                self.assertLess(r["s"], 1)
                self.assertLessEqual(r["t"], round(now + 10) + 1)
                self.assertGreaterEqual(r["due"], r["t"] + self.m.SCAN_HOT_S - 1)
                d.job[0].join()

    def test_pace_across_jobs(self):
        # 一件接一件的小活（每件不满 20 次）合起来也守每秒 iops 次；闲下来再开始时从头算，闲着的时间不攒成一阵猛扫
        rate = {}
        t0 = time.monotonic()
        for _ in range(10):
            p = self.m.Pace(200, None, rate)
            for _ in range(5):
                p()
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)            # 50 次、每秒 200 次：约 0.2 秒（第一批 20 次不等）
        time.sleep(0.5)
        t0 = time.monotonic()
        p = self.m.Pace(200, None, rate)
        for _ in range(40):
            p()
        self.assertGreaterEqual(time.monotonic() - t0, 0.08)            # 闲了 0.5 秒：不攒下 100 次的额度
        p = self.m.Pace(200, 1e-9, dict(rate))                          # 超时拆开按这一件自己的次数：不满 20 次的不超时
        for _ in range(15):
            p()

    def test_chain_without_waiting_for_tick(self):
        # Worker.scan_chain（主循环拍与拍之间每 0.5 秒调）：线程做完一件就收结果、开下一件，不等下一拍；两种扫描都是
        w = self.w
        w.disk, w.ldisk = self.tree()
        now = self.hosts.now()
        for d in (w.disk, w.ldisk):
            d.step(now)
        for _ in range(50):
            jobs = [d.job for d in (w.disk, w.ldisk) if d.job]
            if not jobs:
                break
            for j in jobs:
                j[0].join()
            w.scan_chain(now)
        for d in (w.disk, w.ldisk):
            self.assertIsNone(d.job)
            self.assertTrue(d.ix["units"])
            self.assertTrue(all(r.get("b") is not None for r in d.ix["units"].values()), d.ix["units"])

        # 共享盘慢：收完上一件就不开下一件（yield 写着），不慢了照常开
        d = w.ldisk
        d.hint(d.root)
        d.step(now + 100)
        d.job[0].join()
        w.slow = self.NF
        w.scan_chain(now + 100)
        self.assertIsNone(d.job)
        self.assertIn("共享盘慢", d.brief()["yield"])
        w.slow = None
        d.step(now + 101)
        self.assertIsNotNone(d.job)
        d.job[0].join()

    def test_run_loop_chains(self):
        # 主循环拍与拍之间调 scan_chain
        class Stop(Exception):
            pass
        ticks, calls = [], []

        def tick(*a):
            ticks.append(1)
            if len(ticks) > 1:
                raise Stop

        def chain(now):
            calls.append(now)
            raise Stop
        sig = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        self.w.info = {"mem": {}}
        try:
            with mock.patch.object(self.w, "tick", tick), mock.patch.object(self.w, "scan_chain", chain), self.assertRaises(Stop):
                self.w.run()
        finally:
            for s, h in sig.items():
                signal.signal(s, h)
        self.assertEqual((len(ticks), len(calls)), (1, 1))

    def test_index_saved_when_idle(self):
        # 接连做的时候索引最多 SCAN_SAVE_S 写一次（共享盘上整份重写），这一轮做完立刻写
        d, _ = self.tree()
        now = self.hosts.now()
        d.step(now)
        d.job[0].join()
        d.step(now + 0.1)                       # 收了列目录、开第一个单位：第一次写
        u = d.job[1]
        d.job[0].join()
        d.step(now + 0.2)                       # 收了第一个单位、开下一个：不到 SCAN_SAVE_S 不写
        self.assertIsNotNone(d.job)
        ix = json.loads(Path(d.ixf).read_text())
        self.assertIsNone(ix["units"][u].get("b"))
        for _ in range(50):
            if not d.job:
                break
            d.job[0].join()
            d.step(now + 0.3)
        ix = json.loads(Path(d.ixf).read_text())
        self.assertTrue(all(r.get("b") is not None for r in ix["units"].values()))


class WatchdogTest(unittest.TestCase):
    """保活看门狗：用假 /proc 认谁会把保活又拉起来；暂停、恢复用真的 sleep 进程试。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name).resolve()
        self.p = self.d / "proc"
        self.p.mkdir()
        self.m = load_worker(worker_env(self.d, LABMON_PROC=str(self.p)))
        self.kids = []

    def tearDown(self):
        for k in self.kids:
            k.send_signal(18)                     # SIGCONT：暂停着的也能收尸
            k.kill()
            k.wait()
        self.td.cleanup()

    def proc(self, pid, args, sid=None, ppid=1, comm=None, script=None, cwd=None, env=None, start=1000):
        """一个假进程；script 给了就在 cwd（缺省 d/w<pid>）下写 args[1] 这个脚本。"""
        d = self.p / str(pid)
        d.mkdir()
        comm = comm or os.path.basename(args[0])
        f = ["S", str(ppid), str(sid or pid), str(sid or pid)] + ["0"] * 15 + [str(start)]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(f) + " 0 0\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
        cwd = Path(cwd) if cwd else self.d / f"w{pid}"
        cwd.mkdir(exist_ok=True)
        os.symlink(cwd, d / "cwd")
        if script is not None:
            (cwd / args[1]).write_text(script)
        if env:
            (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()))

    def live(self):
        k = subprocess.Popen(["sleep", "30"])
        self.kids.append(k)
        return k.pid

    def stopped(self, pid):
        return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip().startswith("T")

    def test_watchdogs_sure_and_maybe(self):
        P = "/root/project-demo/evals/train_keep_alive.py"
        self.proc(1, ["/bin/sh", "-c", "/etc/init.d/ssh restart && sleep infinity"])   # 平台容器的 1 号进程
        self.proc(300, ["python3.8", P])                                       # ka.sh 拉起的保活：setsid，脱离后父进程是 1 号（1 号不算看门狗）
        self.proc(400, ["bash", "keepalive_final_check.sh"],                   # demo_eval 那样的：循环里调恢复保活的脚本
                  script="while true; do\n  grep -q 'RUN DONE' q.log && bash keepalive_guard.sh true && exit 0\n  sleep 60\ndone\n")
        self.proc(500, ["bash", "-c", "cd /root/y && python train_keep_alive.py"])   # 前台起保活、等着它的 shell
        self.proc(501, ["python", "/root/y/train_keep_alive.py"], ppid=500, sid=500)
        self.proc(600, ["bash", "loop.sh"], script="while :; do bash restore.sh; sleep 30; done\n")   # 循环调另一个脚本，那个脚本调 ka.sh
        (self.d / "w600" / "restore.sh").write_text("/root/data1/alice/keepalive-demo/bin/ka.sh start\n")
        self.proc(700, ["python3", "check.py"], script="import subprocess\nsubprocess.run(['bash', 'ka.sh', 'start'])\n")
        self.proc(800, ["bash", "gpuwatch.sh"], script="while true; do nvidia-smi >> g.log; sleep 5; done\n")
        self.proc(900, ["python", "train.py"], script="ch = grpc.insecure_channel(a, options=[('grpc.keepalive_time_ms', 1)])\n"
                                                      "while True:\n    time.sleep(1)\n")          # 提到 keepalive 但不是保活程序
        self.proc(1000, ["bash", "-c", "while :; do sleep 20; done; bash ka.sh stop"], env={"ST": "/tmp/labmon-guard.state"})   # 保活守护
        self.proc(1100, ["bash", "/root/data1/alice/keepalive-demo/bin/ka.sh", "start"])
        self.proc(1200, ["bash", "job.sh"], script="while :; do bash ka.sh start; sleep 1; done\n")   # labmon 的任务（skip）
        self.proc(1300, ["-bash"])
        self.proc(1310, ["bash", "/root/demo_eval/exp/keepalive_final_check.sh"])  # 脚本文件已删，bash 还开着它（fd 255）
        (self.p / "1310" / "fd").mkdir()
        (self.p / "1310" / "fd" / "255").write_text("until grep -q 'RUN DONE' q.log; do sleep 60; done\nbash keepalive_guard.sh true\n")
        self.proc(1400, ["cron"])
        self.proc(1500, ["bash", "queue.sh"],                                  # 对方的实验队列：跑完一批拉起保活，此刻在跑实验
                  script="while read c; do python train.py $c; done < cfgs.txt\nbash /root/data1/alice/keepalive-demo/bin/ka.sh start\n")
        self.proc(1501, ["python", "train.py", "a"], ppid=1500, sid=1500)
        cron = self.d / "cron"
        (cron / "crontabs").mkdir(parents=True)
        (cron / "crontabs" / "root").write_text("# */5 * * * * bash /root/keepalive_guard.sh\n*/5 * * * * bash /root/demo_eval/keepalive_guard.sh true\n")
        self.m.CRON = (str(cron),)
        sure, maybe = self.m.watchdogs(self.m.scan_procs(), {1200})
        why = {w["pid"]: w["why"] for w in sure}
        self.assertEqual(why, {400: "提到保活程序，在循环里", 500: "拉起了保活（PID 501 的父进程）", 600: "提到保活程序，在循环里",
                               1310: "提到保活程序，在循环里"})
        self.assertEqual([(w["pid"], w["why"]) for w in maybe],
                         [(700, "提到保活程序，没见循环"), (800, "在循环里盯着 GPU"),
                          (1500, "提到保活程序，在循环里，但下面在跑 PID 1501（python train.py a），暂停会耽误对方，没动"),
                          (1400, "cron 定时任务（%s）提到保活程序（暂停不了）" % (cron / "crontabs" / "root"))])
        self.assertEqual(maybe[3]["cmd"], "*/5 * * * * bash /root/demo_eval/keepalive_guard.sh true")
        self.assertEqual(sure[0]["start"], 1000)

    def test_freeze_then_thaw(self):
        """暂停记进 frozen.json，重复暂停（被恢复后又拉起了保活）再停一次、不重复记；thaw 核对启动时刻后恢复，PID 被复用的不碰。"""
        a, b = self.live(), self.live()
        self.proc(a, ["bash", "wd.sh"], start=111)
        self.proc(b, ["bash", "wd2.sh"], start=222)
        pp = self.m.Paths("t")
        os.makedirs(pp.dir)
        w = [{"pid": a, "start": 111, "cmd": "bash wd.sh", "cwd": "/", "why": "提到保活程序，在循环里"},
             {"pid": b, "start": 222, "cmd": "bash wd2.sh", "cwd": "/", "why": "提到保活程序，在循环里"}]
        self.assertEqual([x["pid"] for x in self.m.freeze(pp, w, [])], [a, b])
        self.assertEqual([x["pid"] for x in self.m.freeze(pp, w[:1])], [a])
        self.assertTrue(self.stopped(a) and self.stopped(b))
        self.assertEqual(len(self.m.load_frozen(pp)["frozen"]), 2)
        (self.p / str(b) / "stat").write_text((self.p / str(b) / "stat").read_text().replace(" 222 ", " 333 "))   # b 的 PID 换了人
        msg = self.m.thaw(pp)
        self.assertEqual(msg, "看门狗：恢复了 PID %d（bash wd.sh：提到保活程序，在循环里）；已不在 PID %d" % (a, b))
        self.assertFalse(self.stopped(a))
        self.assertTrue(self.stopped(b))
        self.assertEqual((self.m.thaw(pp), self.m.load_frozen(pp)), ("", {"frozen": [], "seen": []}))

    def test_restart_while_running_is_fight(self):
        """要卡时保活停过又被拉起：记事件与状态；借来的主机上暂停确定的看门狗，独占任务的队列日志记一笔；空闲下来再要卡不算。"""
        wd = self.live()
        self.proc(wd, ["bash", "keepalive_final_check.sh"], script="while true; do bash keepalive_guard.sh true; sleep 60; done\n")
        self.proc(300, ["python3.8", "/root/x/train_keep_alive.py"])
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        w.procs = self.m.scan_procs()
        w.ovr = [{"kind": "guest", "target": None}]
        qs = self.d / "qs"
        qs.mkdir()
        w.running["k"] = {"name": "x1", "excl": "gpu", "q": {"state": str(qs)}, "pid": 0, "gpus": []}
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)                                                      # 一开始在跑的保活：照常停，不算
        self.assertFalse(ka_on.exists())
        self.assertIsNone(w.fight)
        w.keepalive(True)
        ka_on.write_text("300\n")                                             # 看门狗经 ka.sh 拉起（没写调用者），保活的进程组 300
        (self.d / "keepalive_demo" / "keepalive").mkdir()
        (self.d / "keepalive_demo" / "keepalive" / "events.log").write_text(self.restored(time.time() + 1, 300, "?"))
        w.keepalive(True)
        self.assertFalse(ka_on.exists())
        self.assertTrue(self.stopped(wd))
        self.assertEqual([x["pid"] for x in self.m.load_frozen(w.p)["frozen"]], [wd])
        ev = [json.loads(x) for x in Path(w.p.events).read_text().splitlines()]
        self.assertEqual(ev[-1]["kind"], "ka_fight")
        self.assertIn("1 小时内第 1 次", ev[-1]["msg"])
        self.assertIn("ka.sh 记的调用者 ?", ev[-1]["msg"])
        self.assertIn("暂停了看门狗 PID %d（bash keepalive_final_check.sh：提到保活程序，在循环里），还卡时恢复" % wd, ev[-1]["msg"])
        self.assertIn("x1 运行中 GPU 保活被拉起过一次", (qs / "queue.log").read_text())
        w.keepalive(False)                                                     # 空闲了：之后再要卡时第一次停保活不算被拉起
        ka_on.touch()
        w.keepalive(True)
        self.assertEqual(len(w.ka_hits), 1)

    def restored(self, t, pg, who, tz=8):
        """ka.sh 记的一行 RESTORED（date -Iseconds：带时区）。"""
        z = datetime.timezone(datetime.timedelta(hours=tz))
        return "%s RESTORED 进程组 %d；调用者 %s\n" % (datetime.datetime.fromtimestamp(t, z).isoformat(timespec="seconds"), pg, who)

    def test_caller_only_this_container(self):
        """events.log 在共享盘上、各容器共用、行里不带主机（#123）：只认进程组是这台此刻在跑的保活、时刻在停保活之后、
        又落在保活进程启动后 KA_LOG_LAG 内的那条 RESTORED；别的容器写的（进程组不同、撞号但时刻对不上、写在这台那条之后、别的时区）都不认。"""
        now, tck = time.time(), os.sysconf("SC_CLK_TCK")
        boot = int(now) - 10000
        (self.p / "stat").write_text("cpu  1 2 3 4\nbtime %d\n" % boot)
        self.proc(1, ["/bin/sh"], start=50 * tck)
        self.proc(300, ["python3.8", "/root/x/train_keep_alive.py"], start=int((now - 3 - boot) * tck))   # 3 秒前起来的保活
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        w.running["k"] = {"name": "x1", "pid": 0, "gpus": []}
        ka_on = self.d / "keepalive_demo" / "ka_on"
        ev = self.d / "keepalive_demo" / "keepalive" / "events.log"
        ev.parent.mkdir()
        fights = lambda: [e["msg"] for e in map(json.loads, Path(w.p.events).read_text().splitlines()) if e["kind"] == "ka_fight"]
        w.keepalive(True)                                                      # 停保活
        self.assertFalse(ka_on.exists())
        w.ka_stop_t -= 3600                                                    # 停了 1 小时了
        ka_on.write_text("300\n")                                             # 不经 ka.sh 被拉起：进程组 300
        ev.write_text(self.restored(now - 7200, 300, "before") +              # 停保活之前的
                      self.restored(now - 1800, 300, "clash") +               # 别的容器撞号：进程组也是 300，但比这台的保活早半小时
                      self.restored(now - 1, 96, "other"))                    # 别的容器，进程组 96
        w.keepalive(True)
        self.assertEqual(len(fights()), 1)
        self.assertIn("不是经 ka.sh 拉起的", fights()[0])
        self.assertNotIn("调用者", fights()[0])
        w.ka_stop_t -= 3600
        ka_on.write_text("300\n")                                             # 又被拉起：这回经 ka.sh，拉起后 2 秒记；后面又有别的容器（UTC 时区）写了一条
        with open(ev, "a") as f:
            f.write(self.restored(now - 1, 300, "ours") + self.restored(now, 97, "later", tz=0))
        w.keepalive(True)
        self.assertEqual(len(fights()), 2)
        self.assertIn("ka.sh 记的调用者 ours", fights()[1])
        self.assertAlmostEqual(self.m.started_at(300), now - 3, delta=1)
        self.assertEqual(self.m.host_info([], str(self.d))["started"], boot + 50)   # 容器启动时刻与保活启动时刻同一口径

    def test_idle_log_wording(self):
        """没有任务要卡、GPU 上还有别的进程时不拉起保活，日志不写成“GPU 空闲，但GPU 上还有进程”（自相矛盾）。"""
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        (self.d / "keepalive_demo" / "ka_on").unlink()
        w.ka_others = lambda: []
        self.m.gpu_apps = lambda: ["4242"]
        w.keepalive(False)
        self.assertIn("没有任务要卡，但还有进程在用 GPU（PID 4242），不拉起保活", Path(w.p.log).read_text())

    def restart(self, w, age=0.0):
        """w 写一份状态（write_status，时刻往前推 age 秒），再起一个新 Worker 读它（load_prev，同部署时 --replace 接管）。"""
        run, w.running = w.running, {}                                         # 用例里的假任务不全，写状态时不带
        w.sample([])
        w.write_status([], [], {}, self.m.POLICY, {}, time.time())
        w.running = run
        if age:
            st = json.loads(Path(w.p.status).read_text())
            st["t"] -= age
            Path(w.p.status).write_text(json.dumps(st))
        return self.m.Worker("t")

    def test_kafight_survives_restart(self):
        """kafight 与“要卡时保活停着”写进状态、重启后找回（#124）：告警不因部署重启提前消失，次数接着算、过了 1 小时的筛掉；
        重启那段里被拉起的保活，新 worker 第一轮照样记“保活被拉起”。上一份状态停得太久（STATUS_STALE_S 外）不沿用“停着”。"""
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        w.running["k"] = {"name": "x1", "pid": 0, "gpus": []}
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)                                                      # 停保活
        ka_on.touch()
        w.keepalive(True)                                                      # 被拉起一次
        self.assertEqual(len(w.ka_hits), 1)
        w.ka_hits.insert(0, time.time() - self.m.FIGHT_S - 5)                  # 一次 1 小时前的：不该再算
        w.ka_hits.insert(1, time.time() - 600)
        stop_t = w.ka_stop_t
        w2 = self.restart(w)
        st = json.loads(Path(w.p.status).read_text())
        self.assertEqual((st["kafight"]["n"], len(st["kafight"]["hits"])), (2, 2))
        self.assertAlmostEqual(st["ka_stop"], stop_t, delta=0.01)
        self.assertEqual((len(w2.ka_hits), w2.fight["who"], w2.ka_off), (2, w.fight["who"], True))
        self.assertAlmostEqual(w2.ka_stop_t, stop_t, delta=0.01)
        w2.running["k"] = {"name": "x1", "pid": 0, "gpus": []}
        ka_on.touch()                                                          # 重启那段里被拉起
        w2.keepalive(True)
        self.assertFalse(ka_on.exists())
        ev = [e for e in map(json.loads, Path(w.p.events).read_text().splitlines()) if e["kind"] == "ka_fight"]
        self.assertEqual(len(ev), 2)
        self.assertIn("1 小时内第 3 次", ev[-1]["msg"])
        w3 = self.restart(w2, age=self.m.STATUS_STALE_S + 60)                  # 停得太久：被拉起的次数照样按时刻留，“停着”不沿用
        self.assertEqual((len(w3.ka_hits), w3.ka_off, w3.ka_stop_t), (3, False, 0.0))
        w3.running["k"] = {"name": "x1", "pid": 0, "gpus": []}
        ka_on.touch()
        w3.keepalive(True)                                                     # 当成一开始就在跑的保活：照常停，不记
        self.assertFalse(ka_on.exists())
        self.assertEqual(len([e for e in map(json.loads, Path(w.p.events).read_text().splitlines()) if e["kind"] == "ka_fight"]), 2)
        w3.ka_hits = []                                                        # 都过了 1 小时：状态里没有 kafight，再起来也没有
        w4 = self.restart(w3)
        self.assertEqual((json.loads(Path(w.p.status).read_text())["kafight"], w4.ka_hits, w4.fight), (None, [], None))

    def test_guest_watch_only_lists(self):
        """借来的主机：第一轮就找一遍，只列出来、不暂停（真把保活拉起来了才暂停，见上一条）；同一批不重复记事件。"""
        wd = self.live()
        self.proc(wd, ["bash", "keepalive_final_check.sh"], script="while true; do bash keepalive_guard.sh true; sleep 60; done\n")
        self.proc(700, ["python3", "check.py"], script="import os\nos.system('bash ka.sh start')\n")
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        w.procs = self.m.scan_procs()
        w.watch(time.time())
        self.assertFalse(self.m.load_frozen(w.p)["seen"])                     # 不是借来的：不找
        w.ovr = [{"kind": "guest", "target": None}]
        w.watch(time.time())
        self.assertFalse(self.stopped(wd))
        d = self.m.load_frozen(w.p)
        self.assertEqual((d["frozen"], [(x["pid"], x["why"]) for x in d["seen"]]),
                         ([], [(wd, "提到保活程序，在循环里；确定的：要卡时它把保活拉起来就暂停，还卡时恢复"), (700, "提到保活程序，没见循环")]))
        w.watch(time.time() + self.m.WATCH_EVERY + 1)
        ev = [json.loads(x) for x in Path(w.p.events).read_text().splitlines()]
        self.assertEqual([e["kind"] for e in ev], ["watchdog"])
        self.assertIn("找到保活看门狗，没动（把保活拉起来了才暂停确定的）：PID %d" % wd, ev[0]["msg"])
        self.assertIn("PID 700（python3 check.py：提到保活程序，没见循环）", ev[0]["msg"])
        out = subprocess.run([sys.executable, WORKER, "ls", "--host", "t"], capture_output=True, text=True,
                             env=dict(os.environ, LABMON_HOME=str(self.d / "home"))).stdout
        self.assertIn("找到的保活看门狗（没动）：PID %d" % wd, out)

    def test_owner_keepalive_manager(self):
        """出借方自己的保活管理器（#31，照 BOB 的 keepalive_control.py）：guardian 起 watchdog，watchdog 每 5 秒看登记文件里的 keeper，
        不在就拉起；keeper 与 ka.sh 同一个解释器、同一个脚本。这里是 run / start 这类会退出的命令起的 keeper（start_new_session，起它的命令退出后
        父进程是 1 号；watchdog 自己拉回的见下一条）。按脚本内容 watchdog 是确定的、guardian 下面在跑它是可能的；
        keeper 算 ka.sh 管的保活（worker 停得了）；借来时只列出来，保活被拉起才暂停 watchdog，guardian 不动。"""
        FULL = "/root/data1/alice/project-demo/evals/eval4_end_to_end/train_keep_alive.py"
        ctl = ("KEEPER_SCRIPT = Path('%s')\nWATCH_INTERVAL = 5\n\ndef watchdog():\n    while True:\n        if not keeper_healthy():\n"
               "            start_keeper()\n        time.sleep(WATCH_INTERVAL)\n" % FULL)
        bob, py = self.d / "bob", "/root/data1/bob/envs/torch-demo/bin/python"
        wd = self.live()
        self.proc(60025, [py, "keepalive_control.py", "guardian"], script=ctl, cwd=bob)
        self.proc(wd, [py, "keepalive_control.py", "watchdog"], ppid=60025, script=ctl, cwd=bob)
        self.proc(300, ["/root/miniconda3/envs/py38/bin/python", "-u", FULL])         # keeper：起它的命令已退出，父进程是 1 号
        self.proc(301, ["/root/miniconda3/envs/py38/bin/python", "-u", FULL], ppid=300, sid=300)   # 它的 DataLoader 子进程
        sure, maybe = self.m.watchdogs(self.m.scan_procs(), set())
        self.assertEqual([(w["pid"], w["why"]) for w in sure], [(wd, "提到保活程序，在循环里")])
        self.assertEqual([w["pid"] for w in maybe], [60025])
        self.assertIn("但下面在跑 PID %d" % wd, maybe[0]["why"])
        gp = self.m.gpu_owners([(9300, 6700.0, "GPU-a")], {300: {"hpid": 9300, "comm": "pt_main_thread"}}, {}, FULL)
        self.assertEqual((gp[0]["owner"], gp[0]["ka"]), ("keepalive", False))          # 同脚本：ka.sh 认得、停得了，不另算对方的保活
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        w.procs = self.m.scan_procs()
        w.ovr = [{"kind": "guest", "target": None}]
        w.watch(time.time())
        self.assertFalse(self.stopped(wd))
        seen = {x["pid"]: x["why"] for x in self.m.load_frozen(w.p)["seen"]}
        self.assertEqual(seen[wd], "提到保活程序，在循环里；确定的：要卡时它把保活拉起来就暂停，还卡时恢复")
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)                                                      # 要卡：停 keeper
        w.keepalive(True)
        ka_on.touch()                                                          # watchdog 5 秒内拉回（不经 ka.sh）
        w.keepalive(True)
        self.assertTrue(self.stopped(wd))
        self.assertFalse(ka_on.exists())
        self.assertEqual([x["pid"] for x in self.m.load_frozen(w.p)["frozen"]], [wd])   # guardian 不动

    def test_owner_keepalive_manager_keeper_child(self):
        """watchdog 自己拉回的 keeper 是 watchdog 的子进程（start_new_session 只做 setsid，不改父进程）：watchdog 按“拉起了保活”认成确定的。
        keeper 用 spawn 起的子进程（命令行是 multiprocessing.spawn，不带 keep_alive）是保活自己的子树，不算 watchdog 下面在跑的程序，
        不把 watchdog 降成可能的（#36 审查第 3 条）；guardian 下面在跑 watchdog，仍是可能的。"""
        FULL = "/root/data1/alice/project-demo/evals/eval4_end_to_end/train_keep_alive.py"
        ctl = ("KEEPER_SCRIPT = Path('%s')\nWATCH_INTERVAL = 5\n\ndef watchdog():\n    while True:\n        if not keeper_healthy():\n"
               "            start_keeper()\n        time.sleep(WATCH_INTERVAL)\n" % FULL)
        bob, py, kpy = self.d / "bob", "/root/data1/bob/envs/torch-demo/bin/python", "/root/miniconda3/envs/py38/bin/python"
        wd = self.live()
        self.proc(60025, [py, "keepalive_control.py", "guardian"], script=ctl, cwd=bob)
        self.proc(wd, [py, "keepalive_control.py", "watchdog"], ppid=60025, script=ctl, cwd=bob)
        self.proc(300, [kpy, "-u", FULL], ppid=wd, sid=300)                           # watchdog 拉回的 keeper
        self.proc(301, [kpy, "-c", "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=5, pipe_handle=7)", "--multiprocessing-fork"],
                  ppid=300, sid=300)                                                 # keeper 用 spawn 起的 DataLoader 子进程
        sure, maybe = self.m.watchdogs(self.m.scan_procs(), set())
        self.assertEqual([(w["pid"], w["why"]) for w in sure], [(wd, "拉起了保活（PID 300 的父进程）")])
        self.assertEqual([w["pid"] for w in maybe], [60025])
        self.assertIn("但下面在跑 PID %d" % wd, maybe[0]["why"])

    # ---- #38：暂停看门狗前看它是不是正持着平时不持的锁 ----
    @staticmethod
    def lkey(path):
        """/proc/locks 里这个文件的写法（主:次:inode）。"""
        st = os.stat(path)
        return "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)

    def fds(self, pid, *paths):
        """假进程开着的文件：PROC/<pid>/fd/3、4……指向真文件。"""
        d = self.p / str(pid) / "fd"
        d.mkdir(exist_ok=True)
        for i, f in enumerate(paths, 3):
            f.touch()
            os.symlink(f, d / str(i))

    def locks(self, *rows):
        """写假的 PROC/locks：每行 (类型, PID, 文件)，类型带 "-> " 前缀是在等的。"""
        (self.p / "locks").write_text("".join("%d: %s ADVISORY  WRITE %s %s 0 EOF\n" % (i, t, pid, self.lkey(f)) for i, (t, pid, f) in enumerate(rows, 1)))

    def test_held_locks(self):
        """held_locks：FLOCK、POSIX 按持有者 PID；OFD（PID -1）按它开着的文件；在等的、租约、别人的不算；读不到 /proc/locks 为 None。"""
        a, b, c = (self.d / x for x in ("a.lock", "b.lock", "c.lock"))
        self.proc(4242, ["python", "wd.py"])
        self.fds(4242, a, c)
        b.touch()
        self.assertIsNone(self.m.held_locks(4242))
        self.locks(("FLOCK ", 4242, a), ("-> FLOCK ", 4242, b), ("POSIX ", 999, b), ("OFDLCK", -1, c), ("OFDLCK", -1, b), ("LEASE ", 4242, b))
        self.assertEqual(self.m.held_locks(4242), {self.lkey(a): str(a), self.lkey(c): str(c)})
        self.assertEqual(self.m.held_locks(999), {self.lkey(b): None})       # 读不到它的 fd：只有 inode

    def bob(self):
        """BOB 的结构（同 test_owner_keepalive_manager）：guardian → watchdog（真进程，好看暂不暂停），keeper 与 ka.sh 同脚本；
        watchdog 一生持着自己的单例锁 .keepalive_watchdog.lock，拉起 keeper 时另持 .experiment_keepalive.lock（run/start 共用）。"""
        FULL = "/root/data1/alice/project-demo/evals/eval4_end_to_end/train_keep_alive.py"
        ctl = ("KEEPER_SCRIPT = Path('%s')\nWATCH_INTERVAL = 5\n\ndef watchdog():\n    while True:\n        if not keeper_healthy():\n"
               "            with exclusive():\n                start_keeper()\n        time.sleep(WATCH_INTERVAL)\n" % FULL)
        bob, py = self.d / "bob", "/root/data1/bob/envs/torch-demo/bin/python"
        wd = self.live()
        self.proc(60025, [py, "keepalive_control.py", "guardian"], script=ctl, cwd=bob)
        self.proc(wd, [py, "keepalive_control.py", "watchdog"], ppid=60025, script=ctl, cwd=bob)
        self.proc(300, ["/root/miniconda3/envs/py38/bin/python", "-u", FULL])
        single, run = bob / ".keepalive_watchdog.lock", bob / ".experiment_keepalive.lock"
        self.fds(wd, single, run)
        self.locks(("FLOCK ", wd, single))
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        w.procs = self.m.scan_procs()
        w.ovr = [{"kind": "guest", "target": None}]
        return wd, w, single, run

    def events(self, w):
        return [json.loads(x) for x in Path(w.p.events).read_text().splitlines()] if os.path.exists(w.p.events) else []

    def test_watchdog_holding_lock_not_frozen(self):
        """保活被拉起时 watchdog 正持着平时不持的锁（拉起 keeper 那 8 秒里持着对方 run/start 共用的锁）：这一轮不暂停、不停保活，
        下一轮锁放了再暂停、停保活；“保活被拉起”只记一次，写明推迟了。旧代码第一轮就暂停，锁会一直占到还卡（#38）。"""
        wd, w, single, run = self.bob()
        w.watch(time.time())                                                   # 借来第一轮：记下它平时持的锁（只有单例锁）
        self.assertEqual([x.get("locks") for x in self.m.load_frozen(w.p)["seen"] if x["pid"] == wd], [[self.lkey(single)]])
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)                                                      # 要卡：停 keeper
        w.keepalive(True)
        ka_on.touch()                                                          # watchdog 拉回 keeper，此刻持着 run 锁
        self.locks(("FLOCK ", wd, single), ("FLOCK ", wd, run))
        w.keepalive(True)
        self.assertFalse(self.stopped(wd))
        self.assertTrue(ka_on.exists())                                        # 保活也没停：停了它会再拉一次、又持锁
        self.assertEqual(self.m.load_frozen(w.p)["frozen"], [])
        self.assertIn("PID %d 正持着 %s：这一轮先不暂停、不停保活" % (wd, run), Path(w.p.log).read_text())
        self.assertEqual(([e["kind"] for e in self.events(w)], w.ka_hits), (["watchdog"], []))   # 只有借来第一轮“找到看门狗”那条
        self.assertEqual([x.get("locks") for x in self.m.load_frozen(w.p)["seen"] if x["pid"] == wd], [[self.lkey(single)]])   # 交集：run 锁进不了基线
        self.locks(("FLOCK ", wd, single))                                     # 起稳 keeper、放了锁、在睡
        w.keepalive(True)
        self.assertTrue(self.stopped(wd))
        self.assertFalse(ka_on.exists())
        self.assertEqual([x["pid"] for x in self.m.load_frozen(w.p)["frozen"]], [wd])
        ev = self.events(w)
        self.assertEqual([e["kind"] for e in ev], ["watchdog", "ka_fight"])
        self.assertIn("1 小时内第 1 次", ev[1]["msg"])
        self.assertIn("等它放了平时不持的锁才暂停（推迟", ev[1]["msg"])
        self.assertIsNone(w.ka_wait)

    def test_watchdog_lock_long_hold(self):
        """一直持着平时不持的锁满 LOCK_WAIT_S：照样不暂停（暂停会把对方的锁占到还卡），保活停不下来，记一次“看门狗”事件。"""
        wd, w, single, run = self.bob()
        w.watch(time.time())
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)
        w.keepalive(True)
        ka_on.touch()
        self.locks(("FLOCK ", wd, single), ("FLOCK ", wd, run))
        w.keepalive(True)
        w.ka_wait["t0"] -= self.m.LOCK_WAIT_S + 1                              # 一直持着
        w.keepalive(True)
        w.keepalive(True)
        self.assertFalse(self.stopped(wd))
        self.assertTrue(ka_on.exists())
        ev = [e for e in self.events(w) if e["kind"] == "watchdog" and "找到保活看门狗" not in e["msg"]]
        self.assertEqual(len(ev), 1)
        self.assertIn("看门狗一直持着平时不持的锁", ev[0]["msg"])
        self.assertIn(str(run), ev[0]["msg"])

    def test_void_check_skips_deferred_keepalive(self):
        """运行中的独占任务：只有借来的主机上正推迟暂停看门狗（ka_wait 有值、还没满 LOCK_WAIT_S）时，被拉起的保活不算打扰（#38）；
        别的时候卡上的保活照旧算（连着两轮作废）：要卡这段里自用主机每轮拉回的、推迟满 1 分钟还停不下来的都算，同 main。"""
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        j = {"key": "k", "name": "x1", "excl": "gpu", "gpus": ["GPU-a"], "pid": 0, "q": None}
        w.running["k"] = j
        gp = [{"hpid": 9300, "pid": 300, "mb": 6700.0, "uuid": "GPU-a", "comm": "python", "owner": "keepalive", "ka": False, "job": None, "_j": None}]
        hit = []
        w.void_job = lambda j, why: hit.append(why)
        w.ka_off, w.ka_wait = True, {"t0": time.time(), "said": False}
        for _ in range(3):
            w.void_check([j], {id(j): {"GPU-a"}}, gp, None)
        self.assertEqual(hit, [])
        w.ka_wait["t0"] -= self.m.LOCK_WAIT_S + 1                              # 一直持着锁满 1 分钟：保活停不下来，算打扰
        for _ in range(2):
            w.void_check([j], {id(j): {"GPU-a"}}, gp, None)
        self.assertEqual(hit, ["独占卡期间有 GPU 保活"])
        w.ka_wait, j["void_n"] = None, 0                                       # 要卡这段里、没在推迟（自用主机每轮拉回）：同 main，连着两轮作废
        for _ in range(2):
            w.void_check([j], {id(j): {"GPU-a"}}, gp, None)
        self.assertEqual(len(hit), 2)

    def test_frozen_watchdog_skips_lock_check(self):
        """已暂停的看门狗不参与看锁：它停着拿不了新锁。10 分钟后又找一遍（seen 里不再有它），之后保活被别的原因拉起，一轮就停（审查阻塞 1）。"""
        wd, w, single, run = self.bob()
        w.watch(time.time())
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)
        w.keepalive(True)
        ka_on.touch()
        w.keepalive(True)                                                      # 锁没多：暂停 wd、停保活
        self.assertTrue(self.stopped(wd))
        self.assertFalse(ka_on.exists())
        w.watch(time.time() + self.m.WATCH_EVERY + 1)                          # 又找一遍：seen 里没有 wd 了
        self.assertEqual([x["pid"] for x in self.m.load_frozen(w.p)["seen"] if x["pid"] == wd], [])
        ka_on.touch()                                                          # 出借方 start、别的队列退出时 ka.sh start
        w.keepalive(True)
        self.assertFalse(ka_on.exists())
        self.assertNotIn("还没记过它平时持的锁", Path(w.p.log).read_text())
        self.assertEqual([e["kind"] for e in self.events(w)].count("ka_fight"), 2)

    def test_watchdog_lock_wait_cleared(self):
        """推迟中保活自己没了（同卡 CUDA OOM）：ka_wait 清掉，这次被拉起照样记一次；很久以后再被拉起，推迟从那时算，不一进来就满 1 分钟（审查 3）。
        旧 gpu_queue.sh 还在时返回（不算打架）也清掉。"""
        wd, w, single, run = self.bob()
        w.watch(time.time())
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)
        w.keepalive(True)
        ka_on.touch()
        self.locks(("FLOCK ", wd, single), ("FLOCK ", wd, run))
        w.keepalive(True)                                                      # 推迟
        w.ka_wait["t0"] -= 3000                                                # 推迟了很久
        ka_on.unlink()                                                         # 保活自己退了
        w.keepalive(True)
        self.assertIsNone(w.ka_wait)
        self.assertEqual(len(w.ka_hits), 1)
        ev = [e for e in self.events(w) if e["kind"] == "ka_fight"]
        self.assertEqual(len(ev), 1)
        self.assertIn("保活自己退了", ev[0]["msg"])
        self.assertNotIn("已停", ev[0]["msg"])
        self.assertFalse(self.stopped(wd))
        ka_on.touch()                                                          # 很久以后又被拉起，它又持着锁
        w.keepalive(True)
        self.assertLess(time.time() - w.ka_wait["t0"], 5)
        self.assertEqual([e for e in self.events(w) if "一直持着" in e["msg"]], [])
        w.ka_others = lambda: ["gpu_queue"]                                    # 旧 gpu_queue.sh 退出时拉的：不算打架，也清掉
        w.keepalive(True)
        self.assertIsNone(w.ka_wait)

    def test_watchdog_first_baseline_at_restart(self):
        """没有基线时第一份是在保活刚被拉起时取的（它多半正持着锁）：要等它放下一把再暂停，持锁超过一轮也不暂停在锁里；
        一直不少（第一份其实就是平时的）满 LOCK_WAIT_S 再暂停（审查 4）。"""
        wd, w, single, run = self.bob()
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)
        w.keepalive(True)
        ka_on.touch()
        self.locks(("FLOCK ", wd, single), ("FLOCK ", wd, run))
        w.keepalive(True)                                                      # 没有基线：记下第一份（含 run 锁），推迟
        self.assertIn("PID %d 还没记过它平时持的锁" % wd, Path(w.p.log).read_text())
        w.keepalive(True)                                                      # 还持着（残留 keeper 时最长约 28 秒）：不暂停
        self.assertFalse(self.stopped(wd))
        self.assertTrue(ka_on.exists())
        self.locks(("FLOCK ", wd, single))                                     # 放了 run 锁
        w.keepalive(True)
        self.assertTrue(self.stopped(wd))
        self.assertFalse(ka_on.exists())
        # 第一份就是平时的：一直不少，满 1 分钟再暂停（macOS 上 SIGCONT 之后 ps 仍报 T，这段看 frozen.json）
        Path(w.p.frozen).unlink()
        w.ka_wait = None
        ka_on.touch()
        w.keepalive(True)
        w.keepalive(True)
        self.assertEqual(self.m.load_frozen(w.p)["frozen"], [])
        self.assertTrue(ka_on.exists())
        w.ka_wait["t0"] -= self.m.LOCK_WAIT_S + 1
        d = json.loads(Path(w.p.frozen).read_text())
        for x in d["seen"]:
            if "locks_t" in x:
                x["locks_t"] -= self.m.LOCK_WAIT_S + 1
        Path(w.p.frozen).write_text(json.dumps(d))
        w.keepalive(True)
        self.assertEqual([x["pid"] for x in self.m.load_frozen(w.p)["frozen"]], [wd])
        self.assertFalse(ka_on.exists())
        self.assertEqual([e for e in self.events(w) if "一直持着" in e["msg"]], [])
        ev = [e for e in self.events(w) if e["kind"] == "ka_fight"]
        self.assertIn("等它放了平时不持的锁才暂停", ev[0]["msg"])             # 第一段：放了 run 锁才暂停
        self.assertIn("满 1 分钟没少，到点暂停", ev[1]["msg"])                # 第二段：到点暂停，不是等它放锁
        self.assertNotIn("等它放了", ev[1]["msg"])

    def test_watchdog_no_lock_not_delayed(self):
        """此刻一把锁都不持的看门狗：没有基线也直接暂停，不白等 60 秒（复核 2）。"""
        wd, w, single, run = self.bob()
        self.locks()                                                           # 什么锁都没有
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)
        w.keepalive(True)
        ka_on.touch()
        w.keepalive(True)
        self.assertTrue(self.stopped(wd))
        self.assertFalse(ka_on.exists())
        self.assertIsNone(w.ka_wait)

    def test_watch_baseline_while_keepalive_stopped(self):
        """要卡这段里（保活已被 worker 停着）找到看门狗记的第一份也带 locks_t：它这时可能正拉回保活、持着 run 锁，
        同一轮保活被拉起时不暂停在锁里，等它少一把（复核 1）。借来第一轮（保活还没停）记的不带。"""
        wd, w, single, run = self.bob()
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.keepalive(True)
        w.keepalive(True)                                                      # 要卡：保活已停
        ka_on.touch()
        self.locks(("FLOCK ", wd, single), ("FLOCK ", wd, run))               # watchdog 正拉回 keeper
        w.watch(time.time())                                                   # 这一轮先找看门狗：第一份含 run 锁
        self.assertIn("locks_t", [x for x in self.m.load_frozen(w.p)["seen"] if x["pid"] == wd][0])
        w.keepalive(True)                                                      # 同一轮看到保活被拉起：不暂停
        self.assertFalse(self.stopped(wd))
        self.assertTrue(ka_on.exists())
        self.locks(("FLOCK ", wd, single))
        w.keepalive(True)
        self.assertTrue(self.stopped(wd))
        self.assertFalse(ka_on.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "要真的 Linux /proc/locks（CI 在 Linux 上跑）：核对真内核的格式与 PID、flock 的锁认得出")
    def test_held_locks_real_flock(self):
        """真进程 flock 一个文件：held_locks 认得出（PID 是它、路径对），它退出后锁就没了。"""
        m = self.m
        f = self.d / "x.lock"
        k = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\nf = open(sys.argv[1], 'a')\nfcntl.flock(f, fcntl.LOCK_EX)\n"
                              "print('ok', flush=True)\ntime.sleep(30)\n", str(f)], stdout=subprocess.PIPE, text=True)
        self.kids.append(k)
        self.assertEqual(k.stdout.readline().strip(), "ok")
        k.stdout.close()
        ino = ":%d" % os.stat(f).st_ino          # 只比 inode 与路径：btrfs 子卷的 st_dev 与 /proc/locks 的 s_dev 不同，主:次 对不上
        with mock.patch.object(m, "PROC", "/proc"):                              # 别的用例都用假 /proc（no_host），这条要真的
            got = m.held_locks(k.pid)
            self.assertEqual([(x.endswith(ino), v) for x, v in got.items()], [(True, str(f))])
            self.assertEqual(m.held_locks(os.getpid()), {})
            k.kill()
            k.wait()
            self.assertEqual(m.held_locks(k.pid), {})

    def test_unmanaged_keepalive_is_theirs(self):
        """借来的主机上（给了 ka_full）：命令行里没有 ka.sh 那个脚本的保活 worker 停不了，算对方在用；不给时照旧都算保活。"""
        full = "/root/data1/alice/keepalive-demo/keepalive/train_keep_alive.py"
        self.proc(300, ["python3.8", "/root/carol/train_keep_alive.py"])
        self.proc(301, ["python3.8", full])
        procs = {300: {"hpid": 9300, "comm": "python3.8"}, 301: {"hpid": 9301, "comm": "python3.8"}}
        apps = [(9300, 6000.0, "GPU-a"), (9301, 6000.0, "GPU-a")]
        gp = self.m.gpu_owners(apps, procs, {}, full)
        self.assertEqual([(g["owner"], g["ka"]) for g in gp], [("other", True), ("keepalive", False)])
        self.assertEqual(self.m.gp_label(gp[0]), "对方的保活 PID 9300（ka.sh 停不了）")
        self.assertEqual([g["owner"] for g in self.m.gpu_owners(apps, procs, {})], ["keepalive", "keepalive"])

    def test_relative_keepalive_is_ka(self):
        """相对路径起的同一保活脚本（先 cd 进保活目录、用 train_keep_alive.py、./、.. 起的）也算 ka.sh 的（维护者 11-17 定，#29），
        同 ka.sh 的 pgids()：按进程工作目录还原后比；别的目录里的同名脚本、python -c、不是 python 跑的仍停不了。"""
        ka_dir = self.d / "ka" / "demo"
        ka_dir.mkdir(parents=True)
        full = str(ka_dir / "train_keep_alive.py")
        other = self.d / "carol"
        other.mkdir()
        cases = [(["python3.8", "train_keep_alive.py"], ka_dir, ("keepalive", False)),
                 (["/opt/conda/bin/python", "-u", "./train_keep_alive.py"], ka_dir, ("keepalive", False)),
                 (["python3", "demo/../demo/train_keep_alive.py"], ka_dir.parent, ("keepalive", False)),
                 (["python3.8", "train_keep_alive.py"], other, ("other", True)),
                 (["python3", "-c", "import time", "train_keep_alive.py"], ka_dir, ("other", True)),
                 (["bash", "train_keep_alive.py"], ka_dir, ("other", True))]
        procs, apps = {}, []
        for i, (args, cwd, _) in enumerate(cases):
            self.proc(400 + i, args, cwd=cwd)
            procs[400 + i] = {"hpid": 9400 + i, "comm": "python"}
            apps.append((9400 + i, 6000.0, "GPU-a"))
        gp = self.m.gpu_owners(apps, procs, {}, full)
        self.assertEqual([(g["owner"], g["ka"]) for g in gp], [c[2] for c in cases])
        self.assertFalse(self.m.ka_owned(["python3", "train_keep_alive.py"], "", full))   # 工作目录读不到（进程没了）：认不出

    def test_keepalive_yields(self):
        """空闲时保活在跑、GPU 上有了别的进程（对方回来用）：停保活让路；只有保活时不动。"""
        w = self.m.Worker("t")
        os.makedirs(w.p.dir)
        ka_on = self.d / "keepalive_demo" / "ka_on"
        w.gp = [{"hpid": 9300, "pid": 300, "mb": 6000.0, "uuid": "GPU-a", "comm": "python", "owner": "keepalive", "ka": False, "job": None, "_j": None}]
        w.keepalive(False)
        self.assertTrue(ka_on.exists())
        w.gp.append({"hpid": 9400, "pid": None, "mb": 8000.0, "uuid": "GPU-a", "comm": "python", "owner": "other", "ka": False, "job": None, "_j": None})
        w.ka_checked = 0
        w.keepalive(False)
        self.assertFalse(ka_on.exists())
        self.assertIn("GPU 上有了 PID 9400（python），停保活让路", Path(w.p.log).read_text())


class ProjectInitTest(unittest.TestCase):
    def test_init_builds_layout(self):
        from labmon import workerctl
        with tempfile.TemporaryDirectory() as td:
            d = Path(td).resolve()
            (d / "hosts.toml").write_text(f'[[hosts]]\nid = "here"\ndata_root = "{d / "alice"}"\n')
            (d / "alice" / "envs" / "py311-torch2.4").mkdir(parents=True)
            old = os.environ.get("LABMON_HOSTS")
            os.environ["LABMON_HOSTS"] = str(d / "hosts.toml")
            try:
                self.assertEqual(workerctl.project_init("here", "keepalive_demo")[0], 0)
                rc, out = workerctl.project_init("here", None)
                self.assertEqual(rc, 0)
                self.assertIn("envs/：py311-torch2.4", out)                     # 回输出、不打印（命令行经 /api/project/init 原样打出，#162）
            finally:
                if old is None:
                    os.environ.pop("LABMON_HOSTS")
                else:
                    os.environ["LABMON_HOSTS"] = old
            for p in ("projects/keepalive_demo", "datasets", "weights", "archive", "envs/py311-torch2.4"):
                self.assertTrue((d / "alice" / p).is_dir(), p)
            self.assertTrue((d / "alice" / "README.md").read_text().startswith("# 共享盘目录"))


class LocalDepsTest(WorkerBase):
    """本地盘：标准目录、needs= 声明、放行前检查、续租前列出会丢的文件。"""

    def test_local_deps(self):
        W = load_worker({"LABMON_LOCAL": str(self.d / "local"), "LABMON_HOME": str(self.d / "home")})
        data, cache = self.d / "data1", self.d / "local" / "cache" / "proj" / "w0"
        (data / "proj" / "jobs").mkdir(parents=True)
        (data / "proj" / "st").mkdir()
        cache.mkdir(parents=True)
        for n in ("a.pt", "b.pt", "c.pt"):
            (cache / n).write_bytes(b"x" * 10)
        (data / "proj" / "keep.pt").write_bytes(b"y")
        (data / "proj" / "jobs" / "q.env").write_text(f"W=$LABMON_CACHE/w0\nSH={data}/proj\n")
        (data / "proj" / "jobs" / "q.txt").write_text("\n".join([
            "done1|python x.py --w0 $W/a.pt   # needs=$W/a.pt",
            "fatal|python x.py --w0 $W/a.pt --ref $SH/keep.pt   # est_min=5 needs=$W/a.pt,$SH/keep.pt",
            "heal|{ [ -f $W/b.pt ] || python cap.py --dir $W; } && python x.py --w0 $W/b.pt   # needs=$W/b.pt",
            "glob|python x.py --dir $W   # needs=$W/*.pt",
            "nodecl|python x.py --w0 $W/c.pt",
            "missing|python x.py   # needs=$W/zz.pt",
            "copy|python x.py --w0 $W/a.pt   # needs=$W/a.pt<$SH/m/"]) + "\n")
        (data / "proj" / "m").mkdir()
        (data / "proj" / "m" / "a.pt").write_bytes(b"x" * 10)
        (data / "proj" / "st" / "done1.done").touch()
        q = {"key": "k", "root": str(data / "proj"), "jobs": "jobs/q.txt", "jobs_path": str(data / "proj" / "jobs" / "q.txt"),
             "env": str(data / "proj" / "jobs" / "q.env"), "state": str(data / "proj" / "st"), "project": "proj"}
        rows = {r["job"]: r for r in W.local_deps([q], str(data), W.env_of)}
        self.assertEqual(set(rows), {"fatal", "heal", "glob", "copy"})              # 只认声明；没声明、文件不在的不算
        self.assertEqual((rows["copy"]["heal"], rows["copy"]["how"], rows["heal"]["how"], rows["fatal"]["how"]), (True, "copy", "redo", None))
        c = W.check_needs({"name": "x", "cmd": "true", "needs": [f"{cache}/zz.pt<{data}/proj/m/zz.pt", f"{cache}/*.pt<{data}/proj/m"], "project": "proj"}, {})
        self.assertEqual((c[0]["how"], c[0]["copy"]), (None, []))                       # 主副本也没有：补不了
        self.assertEqual((c[1]["how"], c[1]["copy"]), ("copy", []))                       # 本地已有：不用拷
        self.assertEqual(sorted(m for m, _ in c[1]["publish"]), [f"{data}/proj/m/b.pt", f"{data}/proj/m/c.pt"])   # 主副本没有的跑完写回
        self.assertEqual(rows["fatal"]["files"], [f"{cache}/a.pt"])                  # 共享盘上的不算
        self.assertFalse(rows["fatal"]["heal"])
        self.assertTrue(rows["heal"]["heal"])
        self.assertEqual((len(rows["glob"]["files"]), rows["glob"]["bytes"], rows["glob"]["heal"]), (3, 30, False))
        self.assertEqual(W.hints("x   # gpus=0 needs=$A/b,$C/d needs=e")["needs"], ["$A/b", "$C/d", "e"])
        self.assertEqual(W.hints("python x.py --needs=1")["needs"], [])              # 命令本身里的不算

    def test_shared_disk_scan(self):
        # 共享盘：拿着租约的那台轮流扫二级目录（一次一个）；变了的 1 小时后再扫，没变的间隔翻倍；
        # 目录里增删过东西、有任务跑完的提前；扫得太慢的拆成子目录；别的主机不扫
        root = self.d / "data1"
        W = load_worker({"LABMON_HOME": str(root / "alice" / "labmon"), "LABMON_LOCAL": str(self.d / "local")})
        for p, n in (("alice/a1/x.bin", 5000), ("alice/a1/sub/y.bin", 3000), ("bob/ckpt/z.bin", 9000), ("bob/readme", 10), ("top.sh", 10)):
            f = root / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x" * n)
        for k in range(30):
            (root / "bob" / "big").mkdir(exist_ok=True)
            (root / "bob" / "big" / f"f{k}").write_bytes(b"y" * 100)
        os.link(root / "alice" / "a1" / "x.bin", root / "alice" / "a1" / "sub" / "x2.bin")   # 硬链接只算一次
        d = W.Disk(str(root), "t")
        scanned = []

        def run(now):
            for _ in range(100):
                d.step(now)
                if not d.job:
                    return
                scanned.append(d.job[1])
                d.job[0].join()
        t = time.time() + 10
        old = W.SCAN_SPLIT_S
        W.SCAN_SPLIT_S = 1e-9                                                     # 30 个文件的目录一扫就超时
        try:
            run(t)
        finally:
            W.SCAN_SPLIT_S = old
        u = d.ix["units"]
        r = lambda p: f"{root}/{p}"
        self.assertEqual(scanned[0], "list")
        self.assertEqual(set(u), {r("."), r("bob/."), r("bob/ckpt"), r("bob/big/."), r("alice/."), r("alice/a1"), r("alice/labmon")})
        self.assertEqual(d.ix["split"], [r("bob/big")])                         # 太慢：拆开，本层文件另算一个单位
        self.assertEqual(u[r("bob/big/.")]["n"], 30)
        a1 = u[r("alice/a1")]
        self.assertEqual(a1["n"], 2)
        self.assertEqual([x[0] for x in a1["top"]], ["x.bin", "sub"])            # 直接子项按占用排
        self.assertEqual(u[r("bob/.")]["n"], 1)                                   # 只看本层：readme
        self.assertEqual(a1["age"][0], sum(a1["age"]))                         # 刚写的文件都在“7 天内”
        self.assertLessEqual(sum(a1["age"]), a1["b"])                             # 目录本身的块不分龄
        ix = json.loads((root / "alice" / "labmon" / "disk" / "index.json").read_text())
        self.assertEqual((ix["host"], len(ix["units"])), ("t", 7))              # 放在共享盘上，各主机都能读
        self.assertIn("used", ix["df"])
        scanned.clear()
        run(t + 601)
        self.assertEqual(scanned, ["list"])                                       # 都扫过了：只列目录
        scanned.clear()
        run(t + 3700)
        self.assertEqual(len(scanned), 8)                                         # 第一次扫完都算“变了”：1 小时后各扫一次
        self.assertEqual(u[r("bob/ckpt")]["gap"], 7200)                          # 这次没变：间隔翻倍
        scanned.clear()
        (root / "bob" / "ckpt" / "w.bin").write_bytes(b"z" * 4000000)
        os.utime(root / "bob" / "ckpt", (t + 4400, t + 4400))
        run(t + 4400)
        self.assertEqual(scanned, ["list", r("bob/ckpt")])                       # 目录里新增了文件：提前扫
        self.assertEqual((u[r("bob/ckpt")]["gap"], u[r("bob/ckpt")]["pb"] < u[r("bob/ckpt")]["b"]), (3600, True))
        scanned.clear()
        d.hint(r("alice/a1"))
        run(t + 4410)
        self.assertEqual(scanned, [r("alice/a1")])                                  # 有任务在这个项目目录下跑完
        other = W.Disk(str(root), "u")
        other.step(t + 4420)
        self.assertFalse(other.mine)                                              # 租约在 t 手里：别的主机不扫
        other.lease_t = 0
        other.step(t + 4420 + W.SCAN_LEASE_S + 61)
        self.assertTrue(other.mine)                                               # t 久不续：接着扫，从它留下的索引接着来
        self.assertEqual(len(other.ix["units"]), 7)

    def test_standard_dirs_and_needs_gate(self):
        a = self.queue("a", ["need|true   # gpus=0 needs=$LABMON_CACHE/w0.pt"])
        b = self.queue("b", ["heal|{ [ -f $LABMON_CACHE/x.pt ] || touch $LABMON_CACHE/x.pt; } && echo $LABMON_SCRATCH > $LABMON_CACHE/scr.txt"
                             " && touch $LABMON_SCRATCH/t   # gpus=0 needs=$LABMON_CACHE/x.pt"])
        self.start()
        self.until(lambda: (b / "state" / "heal.done").exists())                     # 能自己补的照常放行
        scr = (self.d / "local" / "cache" / "b" / "scr.txt").read_text().strip()
        self.assertEqual(scr, str(self.d / "local" / "jobs" / "b" / "heal"))
        self.until(lambda: not Path(scr).exists())                                   # 跑成功删单格临时目录（在写 .done 之后删）
        self.until(lambda: any(w["miss"] and "缺本地文件" in w["why"] for w in self.status().get("waiting", [])))
        self.assertFalse((a / "state" / "need.failed").exists())                     # 缺文件：挡在队首，不跑也不记失败
        (self.d / "local" / "cache" / "a").mkdir(parents=True, exist_ok=True)
        (self.d / "local" / "cache" / "a" / "w0.pt").touch()
        self.until(lambda: (a / "state" / "need.done").exists())

    def test_master_copy_then_publish(self):
        # needs=本地<主副本：本地缺、共享盘有就先拷再放行；主副本还没有的，跑成功后写回共享盘
        m = self.d / "data1" / "master"
        m.mkdir(parents=True)
        (m / "w0.pt").write_bytes(b"w" * 100)
        a = self.queue("a", [f"use|cmp $LABMON_CACHE/w0.pt {m}/w0.pt   # gpus=0 needs=$LABMON_CACHE/w0.pt<{m}/",
                             f"make|[ -f $LABMON_CACHE/w1.pt ] || echo v > $LABMON_CACHE/w1.pt   # gpus=0 needs=$LABMON_CACHE/w1.pt<{m}/w1.pt"])
        self.start()
        ev = self.d / "home" / "hosts" / "t" / "events.jsonl"
        kinds = lambda: [json.loads(x)["kind"] for x in ev.read_text().splitlines()] if ev.exists() else []   # noqa: E731
        self.until(lambda: (a / "state" / "make.done").exists() and "publish" in kinds())   # .done 先写，随后才写回主副本、记 publish
        self.assertTrue((a / "state" / "use.done").exists())
        self.assertEqual((m / "w1.pt").read_text(), "v\n")
        self.assertFalse(list(m.glob("*.part")))
        self.assertIn("stage", kinds())


if __name__ == "__main__":
    unittest.main()
