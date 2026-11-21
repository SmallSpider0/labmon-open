"""成组起跑（清单行尾 gang=组名:人数，#180）的离线测试：写法、准入核心 place 的留资源与回填、推演一起排，
以及两台 worker 共享一个 HOME（fixtures.Hosts）时先留后一起开跑、一个失败另一个跟着停、没跟上停下重排、截止按组里最长的。

    python -m unittest tests.test_gang -v
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import Hosts, gate, load_worker, release  # noqa: E402


class GangPureTest(unittest.TestCase):
    """不起 worker：写法解析、place 的留资源与回填、两组互相留着时的让法、推演里一起开跑。"""

    NOW = 1_000_000.0

    def setUp(self):
        self.m = load_worker({})

    def test_hints(self):
        h = self.m.hints("python train.py --gang=3   # gpus=2 gang=ddp_a:2 after=prep")
        self.assertEqual((h["gang"], h["gn"], h["deps"]), ("ddp_a", 2, ["prep"]))       # 命令自己的 --gang= 不算，只认 # 之后
        self.assertEqual((self.m.hints("x   # gang=g")["gang"], self.m.hints("x   # gang=g")["gn"]), ("g", None))   # 没写人数：gn 为 None（gang_block 报）
        self.assertEqual((self.m.hints("x --gang=g:2")["gang"]), None)
        j = self.m.job_of("hold_wait", "true   # gang=g:2")                              # 占位任务不成组
        self.assertEqual((j["gang"], j["gn"]), (None, None))

    # ---- place：成员放得下时先留资源，不开跑 ----
    def card(self, i, total=40000.0):
        return {"i": i, "uuid": "GPU-" + "ab"[i], "total": total, "free": total, "keep": 0.0, "other": 0.0, "ka": False}

    def ran(self, name, gpus, vram_mb, t1, project="p"):
        return {"key": name, "project": project, "name": name, "t0": self.NOW - 60, "t1": t1, "gpus": gpus, "vram": {u: vram_mb for u in gpus},
                "ram": 1.0, "cpu": 1.0, "excl": None, "tag": None, "hold": False, "ngpu": len(gpus), "use": {}, "cards": 1.0}

    def snap(self, cards, jobs=(), gangs=None):
        return {"cards": cards, "limit": 100.0, "room": 90.0, "margin": 8.0, "cpu_cap": None, "cpu_room": None, "jobs": list(jobs), "ext": [], "gp": [],
                "cpu": None, "holds": {}, "tags": {}, "owner": set(), "who": "labmon 之外的进程", "kept": False, "cut": None, "brief": False,
                "gholds": {}, "gangs": gangs or {}, "gfirst": {}}

    def job(self, name, project="p", **kw):
        return dict({"key": name, "project": project, "name": name, "ngpu": 1, "vram": 10.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值", "excl": None,
                     "tag": None, "cpus": 1, "cpu_src": "缺省 1 核", "dur": 10.0, "hold": False, "cards": 0.25, "allowed": None, "gang": None}, **kw)

    def test_hold_and_backfill(self):
        # a1 放得下、同组别的主机上的成员预计 30 分钟后才能放行：a1 留下卡（不开跑），原因写明；之后 10 分钟（×1.2）跑得完的 s 照样用这份资源（回填），
        # 要 40 分钟的 l 不行；组里有成员说不出何时能放行（at 为 None）的不留
        gk = ("p", "g")
        S = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], 20000.0, None)], {gk: {"at": self.NOW + 1800, "local": {}}})
        cards, why, res = self.m.place(self.job("a1", gang="g"), S, self.NOW)
        self.assertEqual((cards, res), (None, False))
        self.assertEqual(why, "成组 g：资源已留好，等同组的一起开跑（预计 %s）；这之前跑得完的任务照样用这份资源" % self.m.now_str(self.NOW + 1800)[5:16])
        h = S["gholds"]["a1"]
        self.assertEqual(([c["uuid"] for c in h["cards"]], h["until"], h["t"], S["gfirst"]), (["GPU-a"], self.NOW + 1800, self.NOW, {("a1", "a1"): self.NOW}))
        self.assertEqual([c["uuid"] for c in self.m.place(self.job("s", vram=12.0), S, self.NOW)[0]], ["GPU-a"])        # 回填：20 GiB 空着、a1 留了 10
        self.assertEqual(self.m.place(self.job("l", vram=12.0, dur=40.0), S, self.NOW)[0], None)                         # 跑不完：扣 a1 留的
        cards, why, res = self.m.place(self.job("a1", gang="g"), S, self.NOW + 60)                                    # 下一轮再留：从何时留着的不变
        self.assertEqual(S["gholds"]["a1"]["t"], self.NOW)
        S["gangs"][gk]["at"] = None
        cards, why, res = self.m.place(self.job("a1", gang="g"), S, self.NOW + 120)
        self.assertEqual((cards, res, S["gholds"]), (None, False, {}))
        self.assertEqual(why, "成组 g：放得下，但同组有成员说不出何时能放行，先不留资源")

    def test_two_groups_yield_by_name(self):
        # 两组各留了一个成员、正挡着对方组的另一个（两台主机上互相卡住）：按 (项目, 组名) 排后面的组让出，两台判法一样
        gangs = {("p", "g1"): {"at": self.NOW + 600, "local": {}}, ("p", "g2"): {"at": self.NOW + 600, "local": {}}}
        S = self.snap([self.card(0)], gangs=gangs)
        self.assertIsNone(self.m.place(self.job("y", gang="g2", vram=30.0), S, self.NOW)[0])
        self.assertEqual(set(S["gholds"]), {"y"})
        self.assertIsNone(self.m.place(self.job("x", gang="g1", vram=30.0), S, self.NOW)[0])             # g1 在前：g2 留的让给它
        self.assertEqual(set(S["gholds"]), {"x"})
        self.assertIsNone(self.m.place(self.job("y", gang="g2", vram=30.0), S, self.NOW)[0])             # g2 再来抢不走
        self.assertEqual(set(S["gholds"]), {"x"})

    # ---- 推演：同组的一起开跑 ----
    def q(self, key, jobs, project="p"):
        return {"key": key, "kind": "queue", "project": project, "allowed": None, "since": self.NOW, "jobs": jobs}

    def spec(self, name, vram, dur, **kw):
        return dict({"name": name, "ngpu": 1, "vram": vram, "src": "提示值", "ram": 1.0, "ram_src": "提示值", "excl": None, "tag": None, "dur": dur, "hold": False,
                     "cpus": 1, "cpu_src": "缺省 1 核", "cards": vram / 40, "gang": None}, **kw)

    def test_simulate_together(self):
        # 卡上 r 还要跑 60 分钟、占 30 GiB：a1（5 GiB）此刻放得下、b1（20 GiB）要等 r 跑完；同组的 a1 留着资源等 b1，两个都在 60 分钟时开跑。
        # 留着的这段里 30 分钟的 c1 回填（×1.2 也在 60 分钟前跑完），90 分钟的 d1 不行，到 60 分钟才排上
        snap = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], 30000.0, self.NOW + 3600, "pr")])
        gangs = {("p", "g"): {"at": self.NOW, "local": {("a", "a1"): self.NOW, ("b", "b1"): self.NOW + 3600}}}
        first = {}
        out, stuck = self.m.simulate(self.NOW, snap, [self.q("a", [self.spec("a1", 5.0, 30, gang="g")]), self.q("b", [self.spec("b1", 20.0, 30, gang="g")]),
                                                      self.q("c", [self.spec("c1", 5.0, 30)], "pc"), self.q("d", [self.spec("d1", 5.0, 90)], "pd")],
                                     set(), gangs=gangs, first=first)
        t = {e["name"]: ((e["t0"] - self.NOW) / 60, e.get("gang")) for e in out}
        self.assertEqual(t, {"a1": (60.0, "g"), "b1": (60.0, "g"), "c1": (0.0, None), "d1": (60.0, None)})
        self.assertEqual((first[("a", "a1")], first[("b", "b1")]), (self.NOW, self.NOW + 3600))       # 各自最早何时放得下：写进 gang.json 给同伴
        self.assertEqual(stuck, {})

    def test_simulate_members_hold_together(self):
        # 同组的成员要同时占着资源，彼此不算回填：卡 40 GiB、r 占 15 GiB 跑到第 30 分钟，a1、b1 各要 15 GiB。b1 的预计沿用上一遍的 60 分钟，
        # a1 留到那时；b1 10 分钟跑得完也不能当回填用 a1 留的（两个加 r 放不下），不能都排在此刻、挤在一张卡上，要等 r 跑完一起开跑（Codex 审查）
        snap = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], 15360.0, self.NOW + 1800, "pr")])
        gangs = {("p", "g"): {"at": self.NOW, "local": {("a", "a1"): self.NOW, ("b", "b1"): self.NOW + 3600}}}
        out, stuck = self.m.simulate(self.NOW, snap, [self.q("a", [self.spec("a1", 15.0, 10, gang="g")]), self.q("b", [self.spec("b1", 15.0, 10, gang="g")])],
                                     set(), gangs=gangs)
        self.assertEqual({e["name"]: (e["t0"] - self.NOW) / 60 for e in out}, {"a1": 30.0, "b1": 30.0})
        self.assertEqual(stuck, {})
        S = self.snap([self.card(0)], [self.ran("r", ["GPU-a"], 15360.0, self.NOW + 1800, "pr")], {("p", "g"): {"at": self.NOW, "local": {("a", "a1"): self.NOW, ("b", "b1"): self.NOW + 3600}}})
        self.assertIsNone(self.m.place(self.job("a1", key="a", gang="g", vram=15.0), S, self.NOW)[0])
        self.assertIsNone(self.m.place(self.job("b1", key="b", gang="g", vram=15.0), S, self.NOW)[0])            # 真放行同一个判定：b1 不当回填
        self.assertEqual(set(S["gholds"]), {"a"})

    def test_simulate_hold_counts_toward_share(self):
        # 两张卡：p 的两组 g1（A）、g2（B）此刻都能一起开跑，q 的 D 也要一张卡；p 用量 0、q 0.5 卡·时。A 留下资源就记上它的用量（同放行：留着就占着），
        # 第二张卡按 用量÷权重 轮到 D，不是 p 的 B（Codex 审查）
        gangs = {("p", "g1"): {"at": self.NOW, "local": {("a", "A"): self.NOW}}, ("p", "g2"): {"at": self.NOW, "local": {("b", "B"): self.NOW}}}
        out, stuck = self.m.simulate(self.NOW, self.snap([self.card(0), self.card(1)]),
                                     [self.q("a", [self.spec("A", 30.0, 60, gang="g1")]), self.q("b", [self.spec("B", 30.0, 60, gang="g2")]),
                                      self.q("d", [self.spec("D", 30.0, 60)], "q")], set(), use={"q": 0.5}, gangs=gangs)
        self.assertEqual({e["name"]: (e["t0"] - self.NOW) / 60 for e in out}, {"A": 0.0, "D": 0.0, "B": 60.0})

    def test_simulate_unknown_peer_is_stuck_on_dep(self):
        # 别的主机上的成员说不出何时能放行（at 为 None）：本机的成员排不上，记成停在依赖上（on_dep，offers 不当“原处跑不了”，#183），原因写明是成组的
        gangs = {("p", "g"): {"at": None, "local": {("a", "a1"): self.NOW}}}
        on_dep = {}
        out, stuck = self.m.simulate(self.NOW, self.snap([self.card(0)]), [self.q("a", [self.spec("a1", 5.0, 30, gang="g"), self.spec("a2", 5.0, 30)])],
                                     set(), on_dep=on_dep, gangs=gangs)
        self.assertEqual(out, [])
        self.assertEqual(stuck, {"a": "a1 是成组 g 的，同组还排不上（凑不齐、成环，或别的主机上的成员说不出何时能放行）"})
        self.assertEqual(on_dep, {"a": "a1"})


class GangHostsTest(unittest.TestCase):
    """两台 worker（t、o）共享一个 HOME（fixtures.Hosts）：各自清单里一个成员，经共享盘上的 status.json（todo）与 gang.json 认组、一起开跑、一起停。
    任务停在闸门上（fixtures.gate），看完再放行；不要卡（gpus=0），不碰本机 GPU。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.hosts = Hosts(self.d)
        self.m = self.hosts.m
        self.t, self.o = self.hosts.worker("t"), self.hosts.worker("o")

    def tearDown(self):
        for w in (self.t, self.o):
            for j in w.running.values():
                try:
                    os.killpg(j["pid"], signal.SIGKILL)
                except OSError:
                    pass
        self.tmp.cleanup()

    def tick(self, w, qs, first=False):
        """一轮：先让它重读别的主机的状态（不等 PEER_EVERY）。"""
        w.peer_t = 0
        w.tick(qs, first)

    def lists(self, a, b, stop="term"):
        qa, = self.hosts.queues({"a": a}, "pa")
        qb, = self.hosts.queues({"b": b}, "pa")
        qa["stop"] = qb["stop"] = stop
        return [qa], [qb]

    def waiting(self, w):
        st = json.loads(Path(w.p.status).read_text())
        return {x["name"]: (x.get("dep"), x["why"]) for x in st["waiting"]}

    def events(self, w, kind):
        f = Path(w.p.events)
        return [e for e in (json.loads(l) for l in f.read_text().splitlines()) if e["kind"] == kind] if f.exists() else []

    def ended(self, w, j, limit=10.0):
        t = time.time()
        while w.alive_job(j) and time.time() - t < limit:
            time.sleep(0.05)
        self.assertFalse(w.alive_job(j))

    def start_both(self, qa, qb):
        """t 先认出 A（凑不齐，先等）→ o 认出 B、留好资源 → t 见 B 就绪，留好就开跑 A → o 见 A 开跑，跟上开跑 B。"""
        self.tick(self.t, qa, True)
        self.assertEqual(self.t.running, {})
        dep, why = self.waiting(self.t)["A"]
        self.assertEqual(dep, "wait")
        self.assertIn("凑不齐：要 2 个，只见到 1 个（A）", why)
        self.tick(self.o, qb, True)
        self.assertEqual(self.o.running, {})
        self.assertTrue(self.waiting(self.o)["B"][1].startswith("成组 ddp：资源已留好，等同组的一起开跑（预计 "))
        rec, = json.loads(Path(self.o.p.gang).read_text())["members"]
        self.assertEqual((rec["name"], rec["state"], rec["n"], rec["dur"]), ("B", "ready", 2, 20.0))
        self.assertFalse(self.o.gang_moved())                           # 主循环拍与拍之间：o 留着资源，盯着 A 的状态
        self.hosts.advance(5)
        self.tick(self.t, qa)
        a, = self.t.running.values()
        self.assertEqual((a["name"], a["gang_with"]), ("A", [["o", "B", 0]]))
        self.assertTrue(self.o.gang_moved())                            # A 开跑了：o 提前进下一轮跟上
        self.hosts.advance(5)
        self.tick(self.o, qb)
        b, = self.o.running.values()
        self.assertEqual((b["name"], b["gang_with"]), ("B", [["t", "A", 0]]))
        self.assertLessEqual(abs(b["t0"] - a["t0"]), self.m.GANG_JOIN_S)
        st = json.loads(Path(self.o.p.status).read_text())["running"][0]
        self.assertEqual((st["gang"], st["gn"], st["gang_with"], st["gang_stop"]), ("ddp", 2, [["t", "A", 0]], None))
        return a, b

    def test_start_together_and_fail_together(self):
        # 一起开跑；A 失败（退出码 3）：o 见 A 失败，按登记的停法（term）停 B，两边都记失败（.failed、用量行不算重排），事件写明
        qa, qb = self.lists(["A|%s; exit 3   # gpus=0 gang=ddp:2 est_min=10" % gate(self.d, "a")],
                            ["B|%s   # gpus=0 gang=ddp:2 est_min=20" % gate(self.d, "b")])
        a, b = self.start_both(qa, qb)
        release(self.d, "a")
        self.ended(self.t, a)
        self.tick(self.t, qa)
        self.assertTrue(Path(qa[0]["state"], "A.failed").exists())
        self.assertEqual([(e["name"], e["state"]) for e in json.loads(Path(self.t.p.gang).read_text())["members"]], [("A", "fail")])
        self.tick(self.o, qb)                                            # 见同伴失败：发 SIGTERM
        self.assertEqual(b["gang_stop"], {"kind": "fail", "why": "同组的 A（t） 失败了", "sent": True})
        self.assertEqual([e["msg"] for e in self.events(self.o, "gang")], ["成组：同组的 A（t） 失败了，按 term 停下 B，记失败"])
        self.ended(self.o, b)
        self.tick(self.o, qb)
        self.assertTrue(Path(qb[0]["state"], "B.failed").exists())
        self.assertFalse(Path(qb[0]["state"], "B.done").exists())
        fail, = self.events(self.o, "failed")
        self.assertTrue(fail["msg"].startswith("失败 B（同组的 A（t） 失败了，已按停法停下"), fail["msg"])
        row = json.loads(Path(self.o.p.usage).read_text().splitlines()[-1])
        self.assertEqual((row["name"], row["requeued"], row["void"], row["gang"]), ("B", False, None, "ddp"))
        self.assertEqual(self.o.voided, [])                              # 不是停下重排，不告诉 wait “作废”
        self.assertIn("fail", {e["state"] for e in json.loads(Path(self.o.p.gang).read_text())["members"]})

    def test_stopped_exit0_counts_failed_elsewhere(self):
        # A 失败；B 的停法是写停止请求文件，B 见到后正常退出（退出码 0）：B 记失败，用量行也不能写 rc "0"，
        # 否则 t 上 after=B 的 C 只看用量行会当 B 跑成功、放行（Codex 审查）
        stop = self.d / "b" / "stop_b"
        qa, qb = self.lists(["A|%s; exit 3   # gpus=0 gang=ddp:2 est_min=10" % gate(self.d, "a"), "C|true   # gpus=0 after=B"],
                            ["B|while [ ! -e '%s' ]; do sleep 0.05; done; exit 0   # gpus=0 gang=ddp:2 est_min=20" % stop])
        qb[0]["stop"] = "file:stop_b"
        a, b = self.start_both(qa, qb)
        release(self.d, "a")
        self.ended(self.t, a)
        self.tick(self.t, qa)
        self.tick(self.o, qb)
        self.assertEqual(b["gang_stop"]["kind"], "fail")
        self.ended(self.o, b)
        self.tick(self.o, qb)
        self.assertTrue(Path(qb[0]["state"], "B.failed").exists())
        row = json.loads(Path(self.o.p.usage).read_text().splitlines()[-1])
        self.assertEqual((row["name"], row["requeued"], row["rc"]), ("B", False, None))
        self.assertIn("（退出码 0）", self.events(self.o, "failed")[0]["msg"])   # 事件照实写退出码
        self.hosts.advance(5)
        self.tick(self.t, qa)
        self.assertEqual(self.t.running, {})
        self.assertEqual(self.t.dep_idx.get(("pa", "B")), "failed")
        self.assertIsNotNone(self.waiting(self.t)["C"][0])

    def test_quick_restart_is_a_new_round(self):
        # 一起开跑后 B 很快停下重排（如 pause --now 又马上恢复），A 跟着停；90 秒内两边又一起开跑：上一次的 stop 记录不算这一次，
        # 新的一次不被它停下，也不因“这边已结束”跟不上（Codex 审查）
        qa, qb = self.lists(["A|%s   # gpus=0 gang=ddp:2 est_min=10" % gate(self.d, "a")],
                            ["B|%s   # gpus=0 gang=ddp:2 est_min=20" % gate(self.d, "b")])
        a, b = self.start_both(qa, qb)
        t00 = min(a["t0"], b["t0"])
        self.o.gang_stop(b, "stop", "测试")
        self.ended(self.o, b)
        self.hosts.advance(3)
        self.tick(self.o, qb)
        self.tick(self.t, qa)
        self.assertEqual(a["gang_stop"]["kind"], "stop")
        self.ended(self.t, a)
        seen = []
        for _ in range(8):
            self.hosts.advance(3)
            self.tick(self.t, qa)
            self.tick(self.o, qb)
            seen.append((self.waiting(self.t), self.waiting(self.o), [j["name"] for j in self.t.running.values()], [j["name"] for j in self.o.running.values()]))
            if self.t.running and self.o.running:
                break
        self.assertTrue(self.t.running and self.o.running, seen)
        a2, = self.t.running.values()
        b2, = self.o.running.values()
        self.assertLess(max(a2["t0"], b2["t0"]) - t00, self.m.GANG_JOIN_S)   # 新的一次与上一次相差不到 GANG_JOIN_S：只比开跑时刻分不出
        self.hosts.advance(3)
        self.tick(self.t, qa)
        self.tick(self.o, qb)
        self.assertIsNone(a2.get("gang_stop"))
        self.assertIsNone(b2.get("gang_stop"))
        self.assertEqual((self.t.running, self.o.running), ({qa[0]["key"]: a2}, {qb[0]["key"]: b2}))

    def test_backfill_under_hold_not_ready(self):
        # A 在 t 上为预计 60 分钟后才放得下的 B（o 上 X 占着 tag heavy=1）留着资源；t 上 5 分钟的 C 回填用了这份（heavy=1），A 此刻开不了跑：
        # gang.json 不能写 ready，否则 X 提前结束时 B 见 A 就绪就先开跑，A 被 C 挡着跟不上（Codex 审查）
        for w in (self.t, self.o):
            Path(w.p.limits).write_text(json.dumps({"tags": {"heavy": 1}}))
        qa, qb = self.lists(["A|true   # gpus=0 gang=ddp:2 est_min=10 tag=heavy"], ["B|true   # gpus=0 gang=ddp:2 est_min=10 tag=heavy"])
        qc, qx = self.hosts.queues({"c": ["C|%s   # gpus=0 est_min=5 tag=heavy" % gate(self.d, "c")],
                                    "x": ["X|%s   # gpus=0 est_min=60 tag=heavy" % gate(self.d, "x")]}, "pc")
        self.tick(self.o, [qx] + qb, True)                               # X 开跑；B 凑不齐，推演里 X 跑完才放得下
        self.assertEqual([j["name"] for j in self.o.running.values()], ["X"])
        self.tick(self.t, qa, True)                                      # A 为 B 留着资源（留到 B 预计放得下的时刻）
        self.assertEqual([(e["name"], e["state"]) for e in json.loads(Path(self.t.p.gang).read_text())["members"]], [("A", "ready")])
        self.hosts.advance(5)
        self.tick(self.t, qa + [qc])                                     # C 赶在那之前跑得完，回填
        self.assertEqual([j["name"] for j in self.t.running.values()], ["C"])
        rec, = json.loads(Path(self.t.p.gang).read_text())["members"]
        self.assertEqual(rec["state"], "wait")
        self.assertGreater(rec["eta"], self.hosts.now())
        release(self.d, "x")                                              # X 提前结束：B 放得下了，但 A 还被 C 挡着，B 只留着、不先跑
        x, = self.o.running.values()
        self.ended(self.o, x)
        self.hosts.advance(5)
        self.tick(self.o, [qx] + qb)
        self.assertEqual(self.o.running, {})
        self.assertTrue(self.waiting(self.o)["B"][1].startswith("成组 ddp：资源已留好"))

    def test_barrier_drops_old_hold(self):
        # A 在 t 上为 B 留着 60 GiB 内存（B 等 o 上的 X 跑完，tag heavy=1）；排在前面（优先）的 C 也要 60 GiB，被这份预留挡住（主机 100 GiB），
        # 等满上限成了屏障，A 回填不下、让位：这一轮没重新留，旧预留就该放掉（gang.json 不再写 ready），下一轮 C 开得了跑。
        # gang_recheck 不能把让位的旧预留又刷成这一轮的，否则 C 一直被挡、B 就绪后 A 还先开跑（Codex 审查）
        Path(self.o.p.limits).write_text(json.dumps({"tags": {"heavy": 1}}))
        Path(self.t.p.sched).write_text(json.dumps({"wait_cap_min": 10}))
        real = self.m.host_info
        info = mock.patch.object(self.m, "host_info", lambda *a, **k: dict(real(*a, **k), mem={"limit": 100.0, "used": 5.0, "anon": 5.0}))
        info.start()
        self.addCleanup(info.stop)
        qa, qb = self.lists(["A|true   # gpus=0 gang=ddp:2 est_min=10 ram=60"], ["B|true   # gpus=0 gang=ddp:2 est_min=10 tag=heavy"])
        qc, qx = self.hosts.queues({"c": ["C|%s   # gpus=0 est_min=120 ram=60" % gate(self.d, "c")],
                                    "x": ["X|%s   # gpus=0 est_min=60 tag=heavy" % gate(self.d, "x")]}, "pc")
        self.tick(self.o, [qx] + qb, True)                               # X 开跑；B 凑不齐，推演里 X 跑完才放得下
        self.tick(self.t, qa, True)                                      # A 为 B 留着资源
        self.assertIn("A", {h["name"] for h in self.t.gang_hold.values()})
        Path(self.t.p.overrides).write_text(json.dumps([{"kind": "focus", "target": "pc"}]))
        self.hosts.advance(60)
        self.tick(self.t, qa + [qc])                                     # C 排在前面，被 A 的预留挡住（120 分钟，回填不下）；A 照留
        self.assertEqual(self.t.running, {})
        self.assertTrue(self.waiting(self.t)["C"][1].startswith("内存不够"), self.waiting(self.t)["C"])
        self.hosts.advance(11 * 60)
        self.tick(self.o, [qx] + qb)                                     # o 照常写 gang.json（B 还在等 X）：A 不是因为同伴没消息才放掉
        self.tick(self.t, qa + [qc])                                     # C 等满 10 分钟成了屏障，A 让位：旧预留放掉
        self.assertTrue(self.waiting(self.t)["A"][1].startswith("让位："), self.waiting(self.t)["A"])
        self.assertEqual(self.t.gang_hold, {})
        self.assertEqual([(e["name"], e["state"]) for e in json.loads(Path(self.t.p.gang).read_text())["members"]], [("A", "wait")])
        self.hosts.advance(60)
        self.tick(self.t, qa + [qc])                                     # 没了预留，屏障 C 开跑
        self.assertEqual([j["name"] for j in self.t.running.values()], ["C"])

    def test_early_fail_fails_unstarted_member(self):
        # A、B 都留好了，t 先开跑 A，A 在 B 跟上前就失败收尾：A 不在 todo 里了，B 不能退回“凑不齐”一直停着（清单后面的、afterany=B 的都卡住），
        # 同一次开跑的整组记失败，B 没开跑也记失败（Codex 审查）
        qa, qb = self.lists(["A|exit 3   # gpus=0 gang=ddp:2 est_min=10"], ["B|true   # gpus=0 gang=ddp:2 est_min=20", "B2|true   # gpus=0"])
        self.tick(self.t, qa, True)
        self.tick(self.o, qb, True)
        self.hosts.advance(5)
        self.tick(self.t, qa)
        a, = self.t.running.values()
        self.ended(self.t, a)
        self.hosts.advance(5)
        self.tick(self.t, qa)
        self.assertTrue(Path(qa[0]["state"], "A.failed").exists())
        self.hosts.advance(5)
        self.tick(self.o, qb)
        self.assertTrue(Path(qb[0]["state"], "B.failed").exists())
        self.assertNotIn("B", {j["name"] for j in self.o.running.values()})
        fail, = self.events(self.o, "failed")
        self.assertEqual(fail["msg"], "失败 B（同组的 A（t） 开跑后失败了，这边还没跟上：同一次开跑的整组记失败，没开跑）")
        row = json.loads(Path(self.o.p.usage).read_text().splitlines()[-1])
        self.assertEqual((row["name"], row["rc"], row["requeued"], row["min"], row["gang"]), ("B", None, False, 0.0, "ddp"))
        self.assertIn(("B", "fail"), [(e["name"], e["state"]) for e in json.loads(Path(self.o.p.gang).read_text())["members"]])
        self.hosts.advance(5)
        self.tick(self.o, qb)                                            # 清单接着往下走
        self.assertEqual([j["name"] for j in self.o.running.values()], ["B2"])

    def test_early_fail_survives_restart(self):
        # 同上，但 A 失败收尾后、B 跟上之前 o 的 worker 重启：A 已不在 t 的 todo 里，近来见过的同伴在哪台（gang_known）要从 o 自己的 gang.json 读回，
        # 否则读不到 t 的 fail 记录，B 一直停在“凑不齐”（Codex 审查）
        qa, qb = self.lists(["A|exit 3   # gpus=0 gang=ddp:2 est_min=10"], ["B|true   # gpus=0 gang=ddp:2 est_min=20", "B2|true   # gpus=0"])
        self.tick(self.t, qa, True)
        self.tick(self.o, qb, True)
        self.hosts.advance(5)
        self.tick(self.t, qa)
        a, = self.t.running.values()
        self.ended(self.t, a)
        self.hosts.advance(5)
        self.tick(self.t, qa)
        self.assertTrue(Path(qa[0]["state"], "A.failed").exists())
        self.o = o2 = self.hosts.worker("o")
        self.hosts.advance(5)
        self.tick(o2, qb, True)
        self.assertTrue(Path(qb[0]["state"], "B.failed").exists())
        self.hosts.advance(5)
        self.tick(o2, qb)
        self.assertEqual([j["name"] for j in o2.running.values()], ["B2"])

    def test_void_member_fails_both(self):
        # 成组的 B 被作废（独占被打扰）：不重排、记失败（用量行不带 void），t 见它失败，按停法停 A、也记失败
        qa, qb = self.lists(["A|%s   # gpus=0 gang=ddp:2 est_min=10" % gate(self.d, "a")],
                            ["B|%s   # gpus=0 gang=ddp:2 est_min=20" % gate(self.d, "b")])
        a, b = self.start_both(qa, qb)
        self.o.void_job(b, "同机有别的进程")
        self.assertEqual(self.events(self.o, "void")[0]["msg"], "作废 B：同机有别的进程，结果不能用；成组的任务记失败，同组的跟着停下")
        self.ended(self.o, b)
        self.tick(self.o, qb)
        self.assertTrue(Path(qb[0]["state"], "B.failed").exists())
        row = json.loads(Path(self.o.p.usage).read_text().splitlines()[-1])
        self.assertEqual((row["name"], row["requeued"], row["void"]), ("B", False, None))
        self.assertEqual(self.o.voided, [])
        self.tick(self.t, qa)
        self.assertEqual(a["gang_stop"]["kind"], "fail")
        self.ended(self.t, a)
        self.tick(self.t, qa)
        self.assertTrue(Path(qa[0]["state"], "A.failed").exists())

    def test_restart_keeps_gang(self):
        # 两边都在跑时 t 的 worker 重启（升级接管）：接管的 A 带着同伴，结束的记录从 gang.json 读回；B 停下重排后 A 照样跟着停
        qa, qb = self.lists(["A|%s   # gpus=0 gang=ddp:2 est_min=10" % gate(self.d, "a")],
                            ["B|%s   # gpus=0 gang=ddp:2 est_min=20" % gate(self.d, "b")])
        a, b = self.start_both(qa, qb)
        self.t = t2 = self.hosts.worker("t")
        self.tick(t2, qa, True)
        a2, = t2.running.values()
        self.assertEqual((a2["name"], a2["pid"], a2["gang"], a2["gang_with"]), ("A", a["pid"], "ddp", [["o", "B", 0]]))
        self.o.gang_stop(b, "stop", "测试")                              # B 按停法停下、重新排队
        self.ended(self.o, b)
        self.tick(self.o, qb)
        self.assertEqual([(e["name"], e["state"]) for e in json.loads(Path(self.o.p.gang).read_text())["members"] if e["state"] != "wait"], [("B", "stop")])
        self.tick(t2, qa)
        self.assertEqual(a2["gang_stop"], {"kind": "stop", "why": "同组的 B（o） 停下重排了", "sent": True})

    def test_peer_not_following_requeues(self):
        # A 开跑了，o 上 B 的清单随后被暂停、没跟上：开跑满 GANG_LATE_S 时 t 按停法停 A、重新排队（不记失败、记成“跟着成组的同伴停下”）
        qa, qb = self.lists(["A|%s   # gpus=0 gang=ddp:2 est_min=10" % gate(self.d, "a")],
                            ["B|%s   # gpus=0 gang=ddp:2 est_min=20" % gate(self.d, "b")])
        self.tick(self.t, qa, True)
        self.tick(self.o, qb, True)
        self.hosts.advance(5)
        self.tick(self.t, qa)
        a, = self.t.running.values()
        Path(qb[0]["state"], "paused").write_text("")
        self.tick(self.o, qb)
        self.assertEqual(self.o.running, {})
        self.hosts.advance(self.m.GANG_LATE_S - 30)
        self.tick(self.t, qa)
        self.assertIsNone(a.get("gang_stop"))
        self.hosts.advance(60)
        self.tick(self.t, qa)
        self.assertEqual(a["gang_stop"]["kind"], "stop")
        self.assertIn("同组的 B（o） 还没跟着开跑", a["gang_stop"]["why"])
        self.ended(self.t, a)
        self.tick(self.t, qa)
        self.assertFalse(Path(qa[0]["state"], "A.failed").exists())
        self.assertFalse(Path(qa[0]["state"], "A.done").exists())
        v, = self.t.voided
        self.assertEqual((v["name"], v["how"]), ("A", "gang"))
        row = json.loads(Path(self.t.p.usage).read_text().splitlines()[-1])
        self.assertEqual((row["name"], row["requeued"]), ("A", True))
        self.assertNotIn("A", {j["name"] for j in self.t.running.values()})   # 重新排队：等 B 恢复后再一起开跑

    def test_deadline_by_longest_member(self):
        # 截止（续租、约定归还）前的准入按组里最长的成员算：A 自己 10 分钟，同组的 B 在 o 上要 50 分钟；截止在 30 分钟后，A 不放（估 60 分钟）
        qa, qb = self.lists(["A|true   # gpus=0 gang=ddp:2 est_min=10"], ["B|true   # gpus=0 gang=ddp:2 est_min=50"])
        self.tick(self.o, qb, True)
        Path(self.t.p.overrides).write_text(json.dumps([{"kind": "deadline", "at": self.hosts.now() + 1800}]))
        self.tick(self.t, qa, True)
        self.assertEqual(self.t.running, {})
        why = self.waiting(self.t)["A"][1]
        self.assertTrue(why.endswith("前跑不完（估 60 分钟 = 50 × 余量 1.2；成组 ddp 里最长的成员）"), why)
        r = {"key": qa[0]["key"], "kind": "queue", "name": "A", "project": "pa", "gang": "ddp"}
        self.assertEqual((self.t.req_dur(r), self.t.req_dur(dict(r, gang=None))), (50.0, 10.0))

    def test_count_mismatch_and_cycle(self):
        # 没写人数：马上报；写了 3 个只有 2 个：先当在等，满 DEP_MISS_S 才报找不到；同一条清单里两个成员：后一个排在前一个后面，成环
        qa, qb = self.lists(["A|true   # gpus=0 gang=x"], ["B|true   # gpus=0 gang=y:3"])
        self.tick(self.t, qa, True)
        self.assertEqual(self.waiting(self.t)["A"], ("missing", "成组 x 没写人数：写成 gang=x:人数（几个成员一起开跑）后下一轮生效"))
        Path(qa[0]["jobs_path"]).write_text("A|true   # gpus=0 gang=y:3\n")
        self.tick(self.o, qb, True)
        self.tick(self.t, qa)
        self.assertEqual(self.waiting(self.t)["A"][0], "wait")
        self.hosts.advance(self.m.DEP_MISS_S)
        self.tick(self.t, qa)
        dep, why = self.waiting(self.t)["A"]
        self.assertEqual(dep, "missing")
        self.assertEqual(why, "成组 y 凑不齐：要 3 个，只见到 2 个（A、B（o））：改清单后下一轮生效")
        Path(qa[0]["jobs_path"]).write_text("A|true   # gpus=0 gang=z:2\nA2|true   # gpus=0 gang=z:2\n")
        self.tick(self.t, qa)
        dep, why = self.waiting(self.t)["A"]
        self.assertEqual((dep, why), ("cycle", "一起开跑成环，谁也轮不到：A 与 A2 成组（要一起开跑），A2 排在 A 后面。同组的成员放在不同的清单里、互不 after=，改后下一轮生效"))
        self.assertEqual(self.t.running, {})


if __name__ == "__main__":
    unittest.main()
