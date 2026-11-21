"""GPU 利用率的两种口径与页面上的平均值（#51）的离线测试。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import fake_status, worker_module  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon.state import append_row, usage_file  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.old = os.environ.get("LABMON_STATE")
        os.environ["LABMON_STATE"] = str(self.d / "state")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("LABMON_STATE", None)
        else:
            os.environ["LABMON_STATE"] = self.old
        self.tmp.cleanup()


class WorkerWindowTest(Tmp):
    """worker 的两个窗口：「在用时多高」（util10，fit 用）保活在的那几轮不记；「吃满程度」（gpu_busy，页面用）只有保活时记 0。"""

    def setUp(self):
        super().setUp()
        self.m = worker_module(self.d)
        self.w = SimpleNamespace(uwin={}, bwin={}, bka={})

    def step(self, t, util, procs):
        gpus = [{"uuid": "GPU-a", "util": util}]
        self.m.Worker.util_window(self.w, gpus, [{"uuid": "GPU-a", **p} for p in procs], t)

    def test_two_calibers(self):
        t0 = 1_000_000.0
        for k in range(6):                         # 前 6 轮在跑任务 80%
            self.step(t0 + 10 * k, 80, [{"owner": "job"}])
        for k in range(6, 12):                     # 后 6 轮只有保活，顶到 98%
            self.step(t0 + 10 * k, 98, [{"owner": "keepalive"}])
        t = t0 + 110
        self.assertEqual(self.m.win_mean(self.w.uwin["GPU-a"], t, self.m.UTIL_WIN_S), 80.0)    # 在用时多高：保活那几轮不算，语义不变
        b = self.m.Worker.busy_view(self.w, t)["GPU-a"]
        self.assertEqual((b["now"], b["m10"], b["h1"], b["cov"], b["ka"]), (0, 40.0, 40.0, 110, 98))
        # 借来的主机上 ka.sh 停不了的保活（owner other、ka 为真）也算只有保活；同卡有任务时照原值
        self.step(t + 10, 70, [{"owner": "other", "ka": True}])
        self.assertEqual(self.m.Worker.busy_view(self.w, t + 10)["GPU-a"]["now"], 0)
        self.step(t + 20, 70, [{"owner": "keepalive"}, {"owner": "job"}])
        b = self.m.Worker.busy_view(self.w, t + 20)["GPU-a"]
        self.assertEqual(b["now"], 70)
        self.assertNotIn("ka", b)

    def test_windows_trim_and_need_samples(self):
        t0 = 1_000_000.0
        self.step(t0, 50, [])
        self.step(t0 + 10, 50, [])
        self.assertIsNone(self.m.Worker.busy_view(self.w, t0 + 10)["GPU-a"]["m10"])             # 不到 3 个样本没有平均
        for k in range(2, 400):
            self.step(t0 + 10 * k, 0 if k > 380 else 100, [])
        t = t0 + 10 * 399
        b = self.m.Worker.busy_view(self.w, t)["GPU-a"]
        self.assertLessEqual(b["cov"], self.m.BUSY_WIN_S)                                       # 1 小时窗口修剪
        self.assertEqual(b["m10"], round(100 * 42 / 61, 1))                                    # 近 10 分钟 61 个样本里 19 个是 0
        self.assertGreater(b["h1"], b["m10"])


class GpuBusyTest(Tmp):
    def test_overview_d1_and_series(self):
        """看板：series 卡元组多记吃满程度 m10；主机项的 busy 带上近 24 小时平均 d1，旧行（没有第 4 项）不算。"""
        st = fake_status(info={"gpus": [{"i": 0, "uuid": "GPU-a", "used": 1, "total": 2, "util": 99}]},
                         gpu_busy={"GPU-a": {"now": 0, "m10": 10.0, "h1": 20.0, "cov": 3600, "ka": 99}})
        t = time.time()
        f = O.state_dir() / "series" / "h.jsonl"
        f.parent.mkdir(parents=True)
        append_row(f, {"t": round(t - 7200), "gpu": [[1, 2, 99]]}, O.KEEP_S, t)                 # 11-17 之前的行
        append_row(f, {"t": round(t - 3600), "gpu": [[1, 2, 99, 30.0]]}, O.KEEP_S, t)
        rows = O._series({"id": "h"}, st, t)
        self.assertEqual(rows[-1]["gpu"], [[1, 2, 99, 10.0]])
        self.assertEqual(O.gpu_busy(st, rows), [{"i": 0, "now": 0, "m10": 10.0, "h1": 20.0, "cov": 3600, "ka": 99, "d1": 20.0, "d1cov": 3600}])


class StaleWorkerTest(Tmp):
    def test_stale_status_no_busy(self):
        """worker 停了、状态还读得到（Codex 在 #54 指出）：停机前的平均值不出 busy、不记进曲线，页面与 gpu_idle 退回探针此刻值。"""
        t = time.time()
        for age, up in ((5, True), (3600, False)):
            st = fake_status(t=t - age, info={"gpus": [{"i": 0, "uuid": "GPU-a", "used": 1, "total": 2, "util": 99}]},
                             gpu_busy={"GPU-a": {"now": 50, "m10": 50.0, "h1": 50.0, "cov": 3600}})
            e = {"id": "h" + str(age), "status": st, "done": [], "lan_off": False}
            O._annotate(e, {"id": e["id"]}, [], {}, {}, t)
            self.assertEqual(e["worker_up"], up)
            self.assertEqual("busy" in e, up)
            self.assertEqual(e["series"][-1]["gpu"][0][3], 50.0 if up else None)


class CollectTest(Tmp):
    def cfg(self):
        return {"id": "p", "alerts": {"gpu_idle_util": 5.0, "gpu_idle_min": 15}, "hosts": [{"id": "h", "checks": []}]}

    def idle(self, recs, lanes=None, jobs=()):
        t = time.time()
        snap = {"hosts": [{"id": "h", "label": "h", "ok": True, "gpus": [{}]}], "lanes": lanes or [{"id": "l", "host": "h", "label": "车道 1", "unfinished": 1}],
                "jobs": list(jobs), "alerts": []}
        hist = [dict(r, t=t - 900 + 120 * k, host="h", ok=True) for k, r in enumerate(recs)]
        C._host_alerts(self.cfg(), snap, {}, hist, {"hosts": []}, t)
        return [a for a in snap["alerts"] if a["key"].startswith("gpu_idle:h:")]

    def test_idle_alert_uses_busy(self):
        """gpu_idle 看近 10 分钟的吃满程度：保活把瞬时值顶到 98% 也报；数据加载的空档瞬时为 0、平均高的不报；没有 worker 数据时看探针值。"""
        a = self.idle([{"util": [98], "busy": [0.0]}] * 8)
        self.assertEqual(len(a), 1)
        self.assertIn("近 10 分钟平均，只有保活时按 0", a[0]["msg"])
        self.assertEqual(self.idle([{"util": [0], "busy": [60.0]}] * 8), [])
        self.assertEqual(len(self.idle([{"util": [0]}] * 8)), 1)

    def test_idle_rewake_by_stretch(self):
        """#129：gpu_idle: 的 key 带这一段空转的起点：门槛两侧短暂来回（不空转不到 gpu_idle_min）仍是同一段，labmon wait 不重复叫醒；
        GPU 真用起来满 gpu_idle_min 再空转是新的一段，6 小时内也叫醒（旧 key 只带主机，被压掉）。"""
        from labmon.waits import _new_alerts
        state, sf, t0 = {}, Path(self.tmp.name) / "w.json", 1_800_000_000.0

        def step(k, busy):                                  # 第 k 轮（每轮 5 分钟）：近 15 分钟的采样都是 busy
            t = t0 + 300 * k
            snap = {"hosts": [{"id": "h", "label": "h", "ok": True, "gpus": [{}]}], "lanes": [{"id": "l", "host": "h", "label": "车道 1", "unfinished": 1}],
                    "jobs": [], "alerts": []}
            hist = [{"t": t - 900 + 120 * i, "host": "h", "ok": True, "busy": [busy]} for i in range(8)]
            C._host_alerts(self.cfg(), snap, state, hist, {"hosts": []}, t)
            return [k.split(":")[0] for k in _new_alerts(sf, {a["key"]: a["msg"] for a in snap["alerts"] if a["key"].startswith("gpu_idle:")}, t)]
        self.assertEqual(step(0, 0.0), ["gpu_idle"])
        self.assertEqual(step(1, 0.0), [])
        self.assertEqual(step(2, 50.0), [])                 # 用了一下（不到 15 分钟）
        self.assertEqual(step(3, 0.0), [])                  # 同一段：不叫醒
        for k in range(4, 8):
            self.assertEqual(step(k, 50.0), [])             # 用了 20 分钟
        self.assertEqual(step(8, 0.0), ["gpu_idle"])        # 新的一段：叫醒

    def test_idle_single_spike_same_stretch(self):
        """Codex 审 #133：滑动窗口里只有一个超线样本（6%，其余 0%）时，窗口条件会假上 gpu_idle_min 分钟，但 GPU 并没真用起来：
        仍是同一段、同一个 key，不重复叫醒（旧做法拿条件的否定计时，第 20 分钟就换了 key）。"""
        from labmon.waits import _new_alerts
        state, sf, t0, hist, woke = {}, Path(self.tmp.name) / "w2.json", 1_800_000_000.0, [], []
        for i in range(-8, 40):                             # 每两分钟一个采样
            t = t0 + 120 * i
            hist.append({"t": t, "host": "h", "ok": True, "busy": [6.0 if i == 1 else 0.0]})
            snap = {"hosts": [{"id": "h", "label": "h", "ok": True, "gpus": [{}]}], "lanes": [{"id": "l", "host": "h", "label": "车道 1", "unfinished": 1}],
                    "jobs": [], "alerts": []}
            C._host_alerts(self.cfg(), snap, state, hist, {"hosts": []}, t)
            woke += _new_alerts(sf, {a["key"]: a["msg"] for a in snap["alerts"] if a["key"].startswith("gpu_idle:")}, t)
        self.assertEqual(len(woke), 1, woke)

    def test_idle_skips_cpu_only(self):
        """不用卡的任务（车道的 no_gpu，#73）不算 gpu_idle 的未完成：只有纯 CPU 任务在跑、卡上只有保活时不报；
        同机还有要卡的任务时照报，只数要卡的、只列那条车道。"""
        rec = [{"util": [98], "busy": [0.0]}] * 8
        cpu = {"id": "c", "host": "h", "label": "车道 1", "unfinished": 1, "no_gpu": 1}
        run = [{"id": "cj", "lane": "c", "status": "running", "hold": False, "log_mtime": time.time() - 60}]
        self.assertEqual(self.idle(rec, [cpu], run), [])
        gpu = {"id": "g", "host": "h", "label": "车道 2", "unfinished": 2, "no_gpu": 0}
        a = self.idle(rec, [cpu, gpu], run + [{"id": "gj", "lane": "g", "status": "running", "hold": False, "log_mtime": time.time() - 60}])
        self.assertEqual(len(a), 1)
        self.assertIn("还有 2 个要用卡的任务未完成", a[0]["msg"])
        self.assertIn("gj 日志", a[0]["msg"])
        self.assertNotIn("cj", a[0]["msg"])

    def test_gpu_mine(self):
        """本项目近 24 小时的平均利用率：跑完的取用量记录的 u、在跑的取 use.util_mean，按 卡数 × 落在窗口里的秒数 加权；别的项目、窗口外、没有 u 的不算。"""
        t = time.time()
        f = usage_file("h")
        f.parent.mkdir(parents=True, exist_ok=True)
        for r in ({"t": t - 3600, "s": t - 7200, "n": "a1", "p": "p", "g": ["a", "b"], "u": 90},     # 2 卡 × 1 小时 90%
                  {"t": t - 90000, "s": t - 93600, "n": "a0", "p": "p", "g": ["a"], "u": 10},        # 窗口外
                  {"t": t - 3600, "s": t - 7200, "n": "q1", "p": "q", "g": ["a"], "u": 10},          # 别的项目
                  {"t": t - 3600, "s": t - 7200, "n": "a2", "p": "p", "g": ["a"]}):                  # 旧行没有 u
            append_row(f, r, O.USAGE_KEEP_S, t)
        glob = {"hosts": [{"id": "h", "status": {"running": [{"name": "r", "project": "p", "gpus": ["c"], "t0": t - 3600, "use": {"util_mean": 30}}]}}]}
        m = C.gpu_mine("p", ["h"], glob, t)
        self.assertEqual((m["util"], m["n"]), (70.0, 2))                                       # (90×2 + 30×1) / 3
        self.assertAlmostEqual(m["card_s"], 3 * 3600, delta=2)
        self.assertIsNone(C.gpu_mine("x", ["h"], glob, t))
        # 刚跑完：用量行已写、状态里还在 running（#54 审查）：按 主机、任务、开始时刻 认同一次，只算一遍
        t0 = round(t - 3600)
        append_row(f, {"t": t - 10, "s": t0, "n": "dup", "p": "p", "g": ["d"], "u": 50}, O.USAGE_KEEP_S, t)
        g2 = {"hosts": [{"id": "h", "status": {"running": [dict(glob["hosts"][0]["status"]["running"][0]), {"name": "dup", "project": "p", "gpus": ["d"], "t0": t0 + 0.4, "use": {"util_mean": 50}}]}}]}
        self.assertEqual(C.gpu_mine("p", ["h"], g2, t)["n"], 3)
        # 同一台、同一秒开跑的同名任务，属于不同清单就是两次运行（#54 审查）：按目录与清单区分，两侧都不并
        f2 = usage_file("k")
        f2.parent.mkdir(parents=True, exist_ok=True)
        s0 = round(t - 3600)
        append_row(f2, {"t": t - 5, "s": s0, "n": "same", "p": "p", "root": "/r", "queue": "A.txt", "g": ["a"], "u": 0}, O.USAGE_KEEP_S, t)
        append_row(f2, {"t": t - 5, "s": s0, "n": "same", "p": "p", "root": "/r", "queue": "B.txt", "g": ["b"], "u": 100}, O.USAGE_KEEP_S, t)
        m = C.gpu_mine("p", ["k"], {"hosts": []}, t)
        self.assertEqual((m["n"], m["util"]), (2, 50.0))
        self.assertAlmostEqual(m["card_s"], 7200, delta=12)
        g3 = {"hosts": [{"id": "k", "status": {"running": [
            {"name": "same", "project": "p", "root": "/r", "queue": q, "gpus": [c], "t0": s0, "use": {"util_mean": u}} for q, c, u in (("C.txt", "c", 0), ("D.txt", "d", 100))]}}]}
        self.assertEqual(C.gpu_mine("p", ["k"], g3, t)["n"], 4)
        # worker 不在、状态停在两天前（#54 审查）：在跑的只算到状态最后写的时刻，落在窗口外就不算
        stale = {"hosts": [{"id": "z", "worker_up": False, "status": {"t": t - 2 * 86400, "running": [
            {"project": "p", "gpus": ["c"], "t0": t - 3 * 86400, "use": {"util_mean": 80}}]}}]}
        self.assertIsNone(C.gpu_mine("p", ["z"], stale, t))

    def test_keep_usage_stores_util(self):
        O._keep_usage(self.d, "h", [{"t0": 1, "t1": time.time(), "name": "a", "project": "p", "rc": "0", "gpus": ["G"], "gpu_util": 77}], time.time(), {"G": 0})
        from labmon.state import load_rows
        self.assertEqual(load_rows(usage_file("h"))[-1]["u"], 77)
        # 同目录不同清单、同一秒开跑的同名任务是两次运行，两行都存（#54 审查）；同一行再送一次仍只存一遍
        now = time.time()
        rows = [{"t0": 5, "t1": now, "name": "s", "project": "p", "root": "/r", "queue": q, "rc": "0", "gpus": ["G"]} for q in ("A.txt", "B.txt")]
        O._keep_usage(self.d, "k2", rows, now, {})
        O._keep_usage(self.d, "k2", rows, now, {})
        self.assertEqual([r["queue"] for r in load_rows(usage_file("k2"))], ["A.txt", "B.txt"])


class FleetMineTest(unittest.TestCase):
    def test_from_worker_carries_busy(self):
        """自用主机的样本各卡带 worker 的吃满程度：组内页卡片与抽屉的利用率按它（页面 fUtil、fCardU）。"""
        st = fake_status(info={"gpus": [{"i": 0, "uuid": "GPU-a", "used": 1, "total": 2, "util": 99}]}, gpu_busy={"GPU-a": {"now": 0, "h1": 12.0}})
        self.assertEqual(F.from_worker(st, None)["gpus"][0]["busy"], {"now": 0, "h1": 12.0})


if __name__ == "__main__":
    unittest.main()
