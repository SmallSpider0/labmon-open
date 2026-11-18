"""算力池 B（#24）的离线测试：入池开关（[[fleet.servers]] 的 lend、lend_hours）、成员、池清单的登记与撤回走哪台、全局快照里的池登记。
不连服务器：ssh 打桩，主机清单与状态目录都在临时目录。

    python -m unittest tests.test_pool -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import fake_host, make_token  # noqa: E402
from labmon import api as A  # noqa: E402
from labmon import borrow as B  # noqa: E402
from labmon import config as CF  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import overview as OV  # noqa: E402
from labmon import pool as P  # noqa: E402
from labmon import serve as S  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon import workerctl as W  # noqa: E402

HOSTS = """[[hosts]]
id = "here"
data_root = "/data/me"

[fleet]
interval = 600

[[fleet.servers]]
id = "a"
holder = "AA"
ssh = "root@h"
port = 1
pool = "P"
{extra}
[[fleet.servers]]
id = "b"
holder = "BB"
ssh = "root@h"
port = 2
pool = "Q"
"""


def who(name: str, role: str = "user") -> U.Ident:
    return U.Ident(name, role, frozenset({"read", "project"}), "token", "k")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"LABMON_HOSTS": str(self.d / "hosts.toml"), "LABMON_STATE": str(self.d / "state")})
        self.env.start()
        self.write()
        for name in ("boss", "AA", "BB"):                                    # 持有人按账号绑着的组内人员认（#19：只认账号库的绑定）
            make_token(name, role="admin" if name == "boss" else "user", scopes=("read",))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, extra: str = ""):
        (self.d / "hosts.toml").write_text(HOSTS.format(extra=extra))

    def server(self, sid: str) -> dict:
        return next(s for s in F.conf()["servers"] if s["id"] == sid)


class LendConfTest(Base):
    def test_span(self):
        self.assertEqual(F.lend_span("22:00-09:00"), (22 * 60, 9 * 60))
        self.assertEqual(F.lend_span(" 9:30 - 24:00 "), (9 * 60 + 30, 24 * 60))
        for bad in ("25:00-09:00", "22:00-24:30", "22-09", "", None, 2200):
            self.assertIsNone(F.lend_span(bad), bad)

    def test_open_across_midnight(self):
        day = time.mktime((2026, 11, 17, 0, 0, 0, 0, 0, -1))
        s = {"lend": True, "lend_hours": "22:00-09:00"}
        self.assertEqual(F.lend_open(s, day + 23 * 3600), (True, day + 33 * 3600))        # 23 点：时段里，次日 9 点结束
        self.assertEqual(F.lend_open(s, day + 3 * 3600), (True, day + 9 * 3600))
        self.assertEqual(F.lend_open(s, day + 12 * 3600), (False, day + 22 * 3600))       # 中午：22 点开始
        self.assertEqual(F.lend_open({"lend": True}, day), (True, None))                  # 不写时段：全天
        self.assertEqual(F.lend_open({"lend": True, "lend_hours": "08:00-08:00"}, day), (True, None))
        self.assertEqual(F.lend_open({"lend_hours": "22:00-09:00"}, day), (False, None))  # 没开入池

    def test_conf_validates(self):
        self.write('lend = true\nlend_hours = "22:00-09:00"\n')
        self.assertEqual((self.server("a")["lend"], self.server("a")["lend_hours"]), (True, "22:00-09:00"))
        for extra, says in (('lend = "yes"\n', "lend"), ('lend = true\nlend_hours = "late"\n', "lend_hours")):
            self.write(extra)
            with self.assertRaises(CF.ConfigError) as e:
                F.conf()
            self.assertIn(says, str(e.exception))
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('id = "b"', 'id = "pool"'))
        with self.assertRaises(CF.ConfigError):
            F.conf()
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('id = "here"', 'id = "pool"'))
        with self.assertRaises(CF.ConfigError):
            CF.user_hosts()

    def test_holder_change_drops_lend(self):
        # 换人：入池是原持有人同意的，新持有人没同意过，lend 与 lend_hours 一并去掉；没换人（同名重写）留着
        self.write('lend = true\nlend_hours = "22:00-09:00"\n')
        CF.set_fleet_holder("a", "AA")
        self.assertTrue(self.server("a").get("lend"))
        CF.set_fleet_holder("a", "CC")
        s = self.server("a")
        self.assertEqual((s["holder"], s.get("lend"), s.get("lend_hours")), ("CC", None, None))


class LendSetTest(Base):
    def test_holder_sets_own(self):
        say = P.set_lend("a", True, "22:00-09:00", who("AA"), False, False)
        self.assertIn("入池，每天 22:00-09:00", say)
        self.assertEqual((self.server("a")["lend"], self.server("a")["lend_hours"]), (True, "22:00-09:00"))
        P.set_lend("a", True, "", who("AA"), False, False)                   # 清空时段：全天
        self.assertIsNone(self.server("a").get("lend_hours"))
        P.set_lend("a", False, None, who("AA"), False, False)
        self.assertIsNone(self.server("a").get("lend"))

    def test_permissions(self):
        with self.assertRaises(U.Denied) as e:                               # 别人的服务器
            P.set_lend("a", True, None, who("BB"), False, False)
        self.assertEqual(e.exception.code, 403)
        with self.assertRaises(U.Denied) as e:                               # 管理员代开：要能做平台操作
            P.set_lend("a", True, None, who("boss", "admin"), False, True)
        self.assertEqual(e.exception.code, 403)
        with self.assertRaises(U.Denied) as e:                               # 管理员代开：要勾「已和这人说好」
            P.set_lend("a", True, None, who("boss", "admin"), True, False)
        self.assertEqual(e.exception.code, 409)
        with self.assertRaises(U.Denied) as e:
            P.set_lend("a", True, "nope", who("AA"), False, False)
        self.assertEqual(e.exception.code, 400)
        with self.assertRaises(U.Denied) as e:
            P.set_lend("zz", True, None, who("AA"), False, False)
        self.assertEqual(e.exception.code, 404)
        self.assertIsNone(self.server("a").get("lend"))
        self.assertIn("boss 代开", P.set_lend("a", True, None, who("boss", "admin"), True, True))
        P.set_lend("a", False, None, who("boss", "admin"), True, False)      # 代关不用勾
        self.assertIsNone(self.server("a").get("lend"))

    def test_lend_race_with_holder_change(self):
        # Codex #50 P1：旧持有人入池与管理员换人并发，不能把 lend 写回新持有人名下。换人线程在 set_lend 读完配置后才起，
        # 配置锁盖住读到写的全过程时它只能等 set_lend 写完再清 lend；锁只盖写入时它先换完、lend=True 被写回
        import threading
        t = []
        real = F.conf

        def conf(*a, **k):
            r = real(*a, **k)
            if not t:
                th = threading.Thread(target=CF.set_fleet_holder, args=("a", "BB"))
                t.append(th)
                th.start()
                th.join(0.5)                # 有锁：卡在锁上、等 set_lend 写完；没锁：这里就换完了
            return r
        with mock.patch.object(P.F, "conf", conf):
            P.set_lend("a", True, None, who("AA"), False, False)
        t[0].join(5)
        s = self.server("a")
        self.assertEqual((s["holder"], s.get("lend")), ("BB", None))

    def test_need_holder(self):
        self.assertEqual(P.need_holder(who("AA"), "a")["id"], "a")
        with self.assertRaises(U.Denied):
            P.need_holder(who("BB"), "a")

    def test_member_and_me(self):
        self.assertFalse(P.member(who("AA")))
        self.assertFalse(U.may_host(who("AA"), CF.POOL))
        P.set_lend("a", True, "22:00-09:00", who("AA"), False, False)
        self.assertTrue(U.may_host(who("AA"), CF.POOL))
        self.assertFalse(U.may_host(who("BB"), CF.POOL))
        self.assertTrue(U.may_host(who("boss", "admin"), CF.POOL))
        mine, = P.my_servers(who("AA"))
        self.assertEqual((mine["id"], mine["holder"], mine["lend"]["on"], mine["lend"]["hours"]), ("a", "AA", True, "22:00-09:00"))
        self.assertTrue(mine["lend"]["say"].startswith("入池，"))
        self.assertEqual(P.my_servers(who("nobody")), [])

    def test_route(self):
        r = S.route_of("POST", "/api/pool/lend")
        self.assertEqual((r.scope, r.own), ("project", "holder"))


class PoolRegTest(Base):
    def test_add_and_rm_via_home_host(self):
        # worker add|rm pool：经与池同一个 worker_home 的在用主机（here）代办，命令带 --host pool
        sent = []
        with mock.patch.object(W, "ssh", lambda h, cmd, *a, **k: sent.append((h["id"], cmd)) or (0, "ok")), \
                mock.patch.object(W.LS, "need"), mock.patch.object(W, "committed", return_value="w"):
            W.run_out("add", CF.POOL, ["/r", "jobs/q.txt"], project="pa")
            W.run_out("rm", CF.POOL, ["/r", "jobs/q.txt"])
        self.assertEqual([h for h, _ in sent], ["here", "here"])
        self.assertTrue(all("--host pool" in c for _, c in sent))
        self.assertEqual(CF.pool_home(), W.host_conf("here")["worker_home"])

    def test_explicit_worker_home(self):
        # Codex #50 P2：模板主机显式配了 worker_home，池与借来的主机都沿用它，不按 data_root 另推
        self.write()
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('data_root = "/data/me"', 'data_root = "/data/me"\nworker_home = "/data/custom-w"'))
        self.assertEqual(CF.pool_home(), "/data/custom-w")
        hosts = [dict(h) for h in CF._read_toml(CF.hosts_file())["hosts"]]
        self.assertEqual(CF.host_defaults(CF.guest_host(self.server("a"), hosts))["worker_home"], "/data/custom-w")
        self.assertEqual(W.via(CF.POOL)["id"], "here")

    def test_no_self_host(self):
        (self.d / "hosts.toml").write_text("")
        self.assertIsNone(CF.pool_home())
        with self.assertRaises(CF.ConfigError):
            W.via(CF.POOL)

    def test_cli_only_add_rm(self):
        r = A.worker("status", CF.POOL, [])
        self.assertEqual(r["rc"], 2)
        self.assertIn("算力池只能", r["usage"])

    def test_locate(self):
        snap = {"hosts": [{"id": "a", "borrow": {"pool": True}}],
                "regs": [{"host": "pool", "queues": [{"root": "/r/x", "jobs": "jobs/x.txt"}]},
                         {"host": "a", "queues": [{"root": "/r/y/", "jobs": "jobs/y.txt"}]},
                         {"host": "here", "queues": [{"root": "/r/z", "jobs": "jobs/z.txt"}]}]}
        self.assertEqual(P.locate("/r/x", "jobs/x.txt", snap), "pool")
        self.assertEqual(P.locate("/r/y", "jobs/y.txt", snap), "a")         # 已放到借给池的主机上：在那台撤
        self.assertEqual(P.locate("/r/z", "jobs/z.txt", snap), "pool")      # 自用主机上同一条清单不算池里的
        with mock.patch.object(A.P, "locate", return_value="a") as loc, mock.patch.object(A.W, "run_out", return_value=(0, "")) as ro:
            A.worker("rm", CF.POOL, ["/r/y", "jobs/y.txt"])
        loc.assert_called_once_with("/r/y", "jobs/y.txt")
        self.assertEqual(ro.call_args.args[:2], ("rm", "a"))

    def test_regs_pool_entry(self):
        # 代读读回的登记文件：hosts/pool/queues/ 下的进全局快照 regs，state 为 pool、排在已归还的前面；added_t 取登记文件的修改时刻
        reg = '{"root": "/r/x", "jobs": "jobs/x.txt", "project": "pa", "stop": "term", "gpus": null, "added": "2026-11-17 10:00"}'
        text = f"{OV.REG} pool\n{OV.REG} pool k1.json 1763344800\n{reg}\n{OV.REG} gone\n{OV.REG} gone k2.json\n{reg}\n"
        regs = OV._parse_regs(text)
        self.assertEqual(regs["pool"][0]["added_t"], 1763344800)
        self.assertIsNone(regs["gone"][0]["added_t"])
        h = {"id": "here", "worker_home": "/data/me/labmon", "regs": ["here"]}
        raw = (OV.SEP + "\n") * 7 + text
        out = OV._regs([], {"here": h}, {"here": (raw, None)}, {}, 100.0)
        self.assertEqual([(x["host"], x["state"], x["label"]) for x in out][:1], [("pool", "pool", "算力池")])
        self.assertEqual(out[0]["queues"][0]["project"], "pa")


# ---------- C：自动借还与收回（#25） ----------
NOW = time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1))
GPU = {"total": 46 * 1024}


class TickBase(Base):
    """pool.tick 的离线用例：主机清单里 a（AA）开了入池；组内探测的 state.json、borrow.json 直接写；fit、搬、借、还都打桩。"""

    def setUp(self):
        super().setUp()
        self.write('lend = true\n')
        self.moves, self.fits, self.starts, self.rets, self.fins = [], [], [], [], []
        self.fit_res = lambda it, hs: {"head": "x", "need": {"ngpu": 1, "vram": 20.0, "ram": 4.0, "cpus": 2}, "dur": 60,
                                       "hosts": {h: {"ok": True, "why": "可以"} for h in hs}}
        self.probe("a")
        self.probe("b")

    def probe(self, sid, started=NOW - 86400, work=(), last=NOW - 60, ngpu=2, **kw):
        sts = F._load_state()
        sts[sid] = {"started": started, "last": last, "idle_since": NOW - 7200,
                    "sample": {"gpus": [dict(GPU) for _ in range(ngpu)], "work": list(work), "vscode": False, "terms": 0, "share": True, "started": started}, **kw}
        F._save_state(sts)

    def mem(self, sid, **kw):
        P._mem(sid, **kw)

    def lent(self, sid="a", status=None, **borrow):
        """借给池的主机项：主机清单写 borrow_pool（guest_host 给 borrow.pool），快照里这台的 borrow 与 worker 状态。"""
        CF.set_fleet_keys({"borrow_since": CF.fmt_time(NOW - 3600, year=True), "borrow_pool": True}, "测试", sid=sid)
        return fake_host(sid, status if status is not None else {}, borrow=dict({"pool": True, "holder": "AA", "since": NOW - 60}, **borrow))

    def pool_list(self, root="/r/x", jobs="jobs/x.txt", project="pa", added=NOW - 600):
        return {"root": root, "jobs": jobs, "project": project, "stop": "term", "added_t": added}

    def snap(self, hosts=(), pool=(), regs=None, heads=None, use=None):
        here = fake_host("here", {"pool_heads": heads if heads is not None else {P.key(q["root"], q["jobs"]): {"head": "x", "n": 1, "dep": None, "held": None}
                                                                                  for q in pool},
                                  "share": {"use": use or {}}})
        rg = [{"host": "pool", "state": "pool", "queues": list(pool)}] + list(regs or [])
        return {"hosts": [here, *hosts], "regs": rg}

    def tick(self, snap, t=NOW, sw=None, **kw):
        def fit(it, hs):
            self.fits.append((it["key"], tuple(hs)))
            return self.fit_res(it, hs)
        return P.tick(snap, sw or {}, t, move=lambda to, items, pool="": self.moves.append((to, [i["jobs"] for i in items], pool)),
                      fit=fit, start=lambda *a, **k: self.starts.append((a, k)), ret=lambda sid, now, run: self.rets.append(sid),
                      fin=lambda sid, gone=False: self.fins.append((sid, gone)) or "还了", **kw)

    def setup_tick(self):
        P._FIT.clear()
        self.addCleanup(P._FIT.clear)


class PoolPlaceTest(TickBase):
    def setUp(self):
        super().setUp()
        self.setup_tick()

    def test_place_on_lent_host(self):
        # 池里待放的清单，借着的 a 空得出来：问一次 fit、放过去；快照的 pool 记它从何时起待放
        e = self.lent("a", {"room": {"gpus": {"GPU-a": 40000.0}}})
        out = self.tick(self.snap([e], [self.pool_list()]))
        self.assertEqual(self.moves, [("a", ["jobs/x.txt"], "")])
        self.assertEqual(self.fits, [("/r/x|jobs/x.txt", ("a",))])
        self.assertEqual(out["lists"]["/r/x|jobs/x.txt"]["since"], NOW - 600)
        self.assertEqual(self.starts, [])

    def test_moved_list_in_this_rounds_regs(self):
        # #78：放过去的清单，这一轮写出的快照 regs 里就在 a 上、池里没了（旧代码：池里还有、a 上没有，项目车道要再等一轮全局采集）；
        # 项目车道（collect.reg_queues）与 locate 跟着认 a
        from labmon import collect as C
        e = self.lent("a", {"room": {"gpus": {"GPU-a": 40000.0}}})
        snap = self.snap([e], [self.pool_list()], regs=[{"host": "a", "state": "active", "queues": []}])
        self.tick(snap)
        rg = {x["host"]: x for x in snap["regs"]}
        self.assertEqual(rg["pool"]["queues"], [])
        self.assertEqual([(q["project"], q["jobs"], q["stop"], q["added_t"]) for q in rg["a"]["queues"]], [("pa", "jobs/x.txt", "term", round(NOW))])
        cfg = {"id": "pa", "queues": []}
        self.assertEqual([q["host"] for q in C.reg_queues(cfg, snap["regs"], {}, NOW)[0]], ["a"])
        self.assertEqual(P.locate("/r/x", "jobs/x.txt", snap), "a")

    def test_moved_back_to_empty_pool_in_regs(self):
        # #78：要还的主机上剩的清单回池里，池里原来是空的（regs 里没有 pool 那一项）：补一项，排在在用主机之后
        q = self.pool_list()
        self.mem("a", started=NOW - 86400)
        self.probe("a", started=NOW - 300)                                 # 换了容器：剩的登记回池里
        e = self.lent("a", None)
        snap = self.snap([e], [], regs=[{"host": "a", "state": "active", "queues": [q]}, {"host": "gone", "state": "returned", "queues": []}])
        snap["regs"] = [x for x in snap["regs"] if x["host"] != "pool"]
        self.tick(snap)
        self.assertEqual([(x["host"], x["state"]) for x in snap["regs"]], [("a", "active"), ("pool", "pool"), ("gone", "returned")])
        self.assertEqual((snap["regs"][0]["queues"], [y["jobs"] for y in snap["regs"][1]["queues"]]), ([], ["jobs/x.txt"]))

    def test_fit_budget_reaches_unasked_lists(self):
        # 前面的清单一直放不下、缓存过期后每轮都要重问：没问过的清单先拿预算，后面的不会永远轮不到（Codex 审查 #75）
        e = self.lent("a")
        lists = [self.pool_list("/r/%d" % i, project="p%d" % i, added=NOW - 600 + i) for i in range(P.POOL_FIT_MAX + 2)]
        self.fit_res = lambda it, hs: {"need": {"ngpu": 1, "vram": 20.0}, "hosts": {h: {"ok": False, "why": "满"} for h in hs}}
        with mock.patch.object(P.FO, "busy", return_value={"zz": "renew"}):
            self.tick(self.snap([e], lists))
            asked = {k for k, _ in self.fits}
            self.assertEqual(len(asked), P.POOL_FIT_MAX)
            self.tick(self.snap([e], lists), t=NOW + P.POOL_RETRY_S + 1)
        self.assertEqual({k for k, _ in self.fits}, {P.key(q["root"], q["jobs"]) for q in lists})

    def test_order_throttle_and_retry(self):
        # 先后照 fair_key（这人的用量少的先）；每台每轮只放一条；fit 每轮最多 POOL_FIT_MAX 次、同一（清单, 候选）POOL_RETRY_S 内不重判
        e = self.lent("a")
        lists = [self.pool_list("/r/%d" % i, project="p%d" % i, added=NOW - 600 + i) for i in range(6)]
        use = {"p%d" % i: 10.0 - i for i in range(6)}                   # p5 用得最少
        self.fit_res = lambda it, hs: {"need": {"ngpu": 1, "vram": 20.0}, "hosts": {h: {"ok": it["project"] == "p5"} for h in hs}}
        with mock.patch.object(P.FO, "busy", return_value={"zz": "renew"}):     # 操作格忙：这一轮不借
            out = self.tick(self.snap([e], lists, use=use))
        self.assertEqual(self.moves, [("a", ["jobs/x.txt"], "")])
        self.assertEqual(self.fits[0][0], "/r/5|jobs/x.txt")
        self.assertEqual(len(self.fits), P.POOL_FIT_MAX)                 # 放了 p5 以后 a 这一轮已用过：别的没有候选主机，照样问（要 need 才知道借哪台）
        self.moves.clear()
        self.fit_res = lambda it, hs: {"need": {"ngpu": 1, "vram": 20.0}, "hosts": {h: {"ok": False, "why": "卡不够"} for h in hs}}
        self.fits.clear()
        with mock.patch.object(P.FO, "busy", return_value={"zz": "renew"}):
            out = self.tick(self.snap([e], lists[:5], use=use), t=NOW + 1)
        self.assertEqual(len(self.fits), P.POOL_FIT_MAX)
        self.assertEqual(self.moves, [])
        self.assertIn("这一轮问得够多了", out["lists"]["/r/2|jobs/x.txt"]["why"])    # 没问过的 p1、p0 先拿预算，问过的 p2 排到下一轮
        self.assertIn("池里借着的服务器都放不下（a：卡不够）", out["lists"]["/r/4|jobs/x.txt"]["why"])
        n = len(self.fits)
        with mock.patch.object(P.FO, "busy", return_value={"zz": "renew"}):
            self.tick(self.snap([e], lists[:5], use=use), t=NOW + 60)
        self.assertEqual(len(self.fits) - n, 1)                           # 问过的 4 条 5 分钟内不重判，只问了没问过的那条
        with mock.patch.object(P.FO, "busy", return_value={"zz": "renew"}):
            self.tick(self.snap([e], lists[:5], use=use), t=NOW + P.POOL_RETRY_S + 2)
        self.assertEqual(len(self.fits) - n - 1, P.POOL_FIT_MAX)

    def test_waiting_reasons(self):
        # 不放的原因：队首在等依赖、清单暂停、项目暂停、没有新鲜的 worker 状态
        q = self.pool_list()
        k = P.key(q["root"], q["jobs"])
        out = self.tick(self.snap([], [q], heads={k: {"head": "x", "n": 1, "dep": ["wait", "等 pb/y 跑完"], "held": None}}))
        self.assertEqual(out["lists"][k]["why"], "队首在等依赖：等 pb/y 跑完")
        out = self.tick(self.snap([], [q]), sw={"pause": {"pa": None}})
        self.assertEqual(out["lists"][k]["why"], "项目暂停中")
        out = self.tick(self.snap([], [q], heads={}))
        self.assertEqual(out["lists"][k]["why"], "清单里没有还要跑的行")
        snap = self.snap([], [q])
        snap["hosts"][0]["worker_up"] = False
        self.assertEqual(self.tick(snap)["lists"][k]["why"], "还没有在跑的 worker 读到池里的清单")
        self.assertEqual((self.moves, self.starts), ([], []))

    def test_standby_does_nothing(self):
        with mock.patch.object(P.LS, "primary", return_value=False):
            self.assertEqual(self.tick(self.snap([self.lent("a")], [self.pool_list()])), {})
        self.assertEqual((self.moves, self.fits, self.starts), ([], [], []))

    def test_waited_too_long_moves_on(self):
        # 放过去的清单队首在 a 上因资源等满 POOL_WAIT_S：当放不下，换到别的池主机 b；等依赖的、被指令挡住的不挪
        q = self.pool_list()
        wa = {"kind": "queue", "root": q["root"], "queue": q["jobs"], "project": "pa", "name": "x", "since": NOW - P.POOL_WAIT_S - 5}
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="lend = true\n") + "lend = true\n")      # b 也入池
        a, b = self.lent("a", {"waiting": [wa]}), self.lent("b")
        regs = [{"host": "a", "state": "active", "queues": [dict(q)]}]
        self.tick(self.snap([a, b], [], regs=regs))
        self.assertEqual(self.moves[0][0], "b")
        self.assertIn("在 a 上等了", self.moves[0][2])
        self.moves.clear()
        P._FIT.clear()
        a = self.lent("a", {"waiting": [dict(wa, dep="wait")]})
        self.tick(self.snap([a, b], [], regs=regs))
        self.assertEqual(self.moves, [])


class PoolHomeTest(TickBase):
    def test_heads_only_from_pool_home(self):
        # 主机清单里有两个 worker_home：池清单的队首只认池登记那个 HOME 下各台的状态（别的 HOME 的 worker 读不到池目录，写的是空的），
        # 那边的状态更新得晚也不把池清单当成“没有还要跑的行”（Codex 审查 #75）
        self.setup_tick()
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="lend = true\n").replace(
            '[fleet]', '[[hosts]]\nid = "far"\ndata_root = "/data/far"\nworker_home = "/data/far/.labmon"\n\n[fleet]', 1))
        sn = self.snap([], [self.pool_list()])
        far = fake_host("far", {"pool_heads": {}, "share": {"use": {}}})
        far["status"]["t"] = sn["hosts"][0]["status"]["t"] + 60
        sn["hosts"].append(far)
        out = self.tick(sn)
        self.assertNotIn("清单里没有还要跑的行", out["lists"]["/r/x|jobs/x.txt"].get("why") or "")
        self.assertEqual(len(self.starts), 1)


class PoolBorrowTest(TickBase):
    def setUp(self):
        super().setUp()
        self.setup_tick()
        self.need = {"ngpu": 1, "vram": 20.0}

    def pick(self, t=NOW, busy=()):
        return P.want_borrow(self.need, F.conf(), F._load_state(), P.load_json(P.state_dir() / "borrow.json", {}), t, busy)

    def hours(self, a, b):
        hm = lambda x: time.strftime("%H:%M", time.localtime(x))        # noqa: E731
        self.write('lend = true\nlend_hours = "%s-%s"\n' % (hm(a), hm(b)))

    def test_probe_before_borrow_is_not_new_box(self):
        # 组内探测早于这次借用、记的是上一个容器：不算换了容器（借来时 begin 现场探到的才是这次的），不还（Codex 审查 #75）
        self.probe("a", started=NOW - 86400, last=NOW - 540)
        self.mem("a", started=NOW - 600, since=NOW - 60)
        e = self.lent("a", {"running": [{"name": "j", "hold": False}]})
        out = self.tick(self.snap([e], []))
        self.assertEqual((self.fins, out["gone"]), ([], []))
        self.probe("a", started=NOW - 30, last=NOW)                      # 借来之后探到的新容器：换了，直接还
        out = self.tick(self.snap([e], []), t=NOW + 1)
        self.assertEqual(out["gone"], ["a"])

    def test_borrows_when_nothing_fits(self):
        # 池里有清单、没有借着的主机：按 fit 给的 need 挑一台入池的（a），fleetops.start("borrow", pool=True)；一轮最多借一台
        out = self.tick(self.snap([], [self.pool_list(), self.pool_list("/r/y")]))
        self.assertEqual(len(self.starts), 1)
        (args, kw), = self.starts
        self.assertEqual((args[0], kw["ids"], kw["pool"]), ("borrow", ["a"], True))
        self.assertIn("正在借 AA 的 a", out["lists"]["/r/x|jobs/x.txt"]["why"])
        m = P.load_json(P.state_dir() / "borrow.json", {})["a"]["pool"]
        self.assertEqual(m["want"], {"ngpu": 1, "vram": 20.0})
        with mock.patch.object(P.FO, "busy", return_value={"zz": "renew"}):     # 操作格忙：不借
            self.tick(self.snap([], [self.pool_list()]), t=NOW + P.POOL_RETRY_S + 1)
        self.assertEqual(len(self.starts), 1)

    def test_history_estimate_capped_to_whole_card(self):
        # 按历史估的显存超过整卡时 worker 按整卡算（card_need）：48.4 GiB 的历史估计照样借 46 GiB 的空卡；清单写的提示值照旧要装得下（Codex 审查 #75）
        self.need = {"ngpu": 1, "vram": 48.4, "src": "同名近 5 次峰值"}
        self.assertEqual(self.pick()[0], "a")
        self.need = {"ngpu": 1, "vram": 48.4, "src": "提示值"}
        self.assertIsNone(self.pick()[0])
        self.assertIn("卡不够", self.pick()[2])

    def test_skip_server_with_leftover_lists(self):
        # 以前手动借过、目录里还留着别的清单登记的：池借来一拉起 worker 就会接着跑它们，不借（Codex 审查 #75）
        left = {"host": "a", "state": "returned", "queues": [{"root": "/r/old", "jobs": "jobs/old.txt", "project": "px", "stop": "boundary"}]}
        out = self.tick(self.snap([], [self.pool_list()], regs=[left]))
        self.assertEqual(self.starts, [])
        self.assertIn("目录里还留着别的清单", out["lists"]["/r/x|jobs/x.txt"]["why"])
        self.tick(self.snap([], [self.pool_list()], regs=[dict(left, queues=[])]), t=NOW + P.POOL_RETRY_S + 1)   # 登记都撤了：照借
        self.assertEqual(len(self.starts), 1)

    def test_each_condition(self):
        self.assertEqual(self.pick()[0], "a")
        for setup, why in (
                (lambda: self.write('lend = true\nlend_hours = "00:00-01:00"\n'), "不在入池时段"),
                (lambda: self.mem("a", rest=round(NOW + 60)), "休息中"),
                (lambda: self.mem("a", fail=round(NOW - 60)), "刚借不成"),
                (lambda: self.probe("a", last=NOW - 3 * 600), "没探到"),
                (lambda: self.probe("a", work=[{"kind": "job", "label": "python train.py"}]), "GPU 上有主人的进程"),
                (lambda: self.probe("a", last_job=NOW - 600), "主人刚用过"),
                (lambda: self.probe("a", ngpu=0), "卡不够"),
                (lambda: self.mem("a", nofit=[[1, 20.0, round(NOW + 3600)]]), "借过放不下"),
                (lambda: CF.set_fleet_keys({"borrow_since": CF.fmt_time(NOW, year=True)}, "测试", sid="a"), "借着")):
            setup()
            sid, _, miss = self.pick()
            self.assertIsNone(sid, why)
            self.assertIn(why, miss)
            self.write('lend = true\n')                            # 复原
            self.probe("a")
            B._update(lambda st: st.pop("a", None))
            CF.set_fleet_keys({"borrow_since": None}, "测试", sid="a")
        self.assertEqual(self.pick(busy={"a": "renew"})[2], "在做平台操作 1 台")
        self.need = {"ngpu": 1, "vram": 46.0, "src": "提示值"}         # 单卡 46 GiB：清单写的 46 + 1 放不下
        self.assertIn("卡不够", self.pick()[2])
        self.need = {"ngpu": 1, "vram": 20.0}
        self.hours(NOW - 600, NOW + 3600)                                 # 时段一小时后结束：离截止不到 2 小时
        self.assertIn("离截止不到 2 小时", self.pick()[2])
        self.hours(NOW - 600, NOW + 5 * 3600)
        sid, until, _ = self.pick()
        self.assertEqual((sid, until), ("a", NOW + 5 * 3600))

    def test_failed_backs_off(self):
        with mock.patch.object(P, "notify") as nt:
            P._failed("a", "连不上")
            P._failed("a", "连不上")
        self.assertEqual(nt.call_count, 1)                                  # 每台每天通知管理员一次
        self.assertIn("刚借不成", self.pick(t=time.time())[2])


class PoolReturnTest(TickBase):
    def setUp(self):
        super().setUp()
        self.setup_tick()

    def test_owner_back_and_rest(self):
        # R1：主人回来（worker 状态的 guest.owner）就要还、记休息期；这台上还会跑的清单回池里
        q = self.pool_list()
        e = self.lent("a", {"guest": {"owner": ["卡 0：对方用 3.0 GiB 显存"]}, "todo": [{"root": q["root"], "queue": q["jobs"], "jobs": ["x", "y"]}]})
        out = self.tick(self.snap([e], [], regs=[{"host": "a", "state": "active", "queues": [q]}]))
        self.assertEqual(self.rets, ["a"])
        m = P.load_json(P.state_dir() / "borrow.json", {})["a"]["pool"]
        self.assertEqual((m["rest"], m["rest_why"]), (round(NOW + P.POOL_REST_S), "主人回来过"))
        self.assertEqual(self.moves, [("pool", ["jobs/x.txt"], "a 要还了")])
        self.assertTrue(any("AA 回来了" in r["msg"] for r in self.events()), self.events())
        self.assertEqual(out["gone"], [])

    def events(self):
        from labmon.records import records
        return [r for r in records(cat="borrow") if r.get("kind") == "pool"]

    def test_lend_off_hours_idle(self):
        # R3：关了入池（记休息期）、出了时段（不记）；R4：连着 POOL_IDLE_S 没有池任务在跑；借来后一个也没跑起来的记 nofit
        e = self.lent("a", {"running": []})
        self.write("")
        self.tick(self.snap([e]))
        self.assertEqual(self.rets, ["a"])
        self.assertEqual(P.load_json(P.state_dir() / "borrow.json", {})["a"]["pool"]["rest_why"], "关了入池")
        self.rets.clear()
        B._update(lambda st: st.pop("a", None))
        self.write('lend = true\nlend_hours = "00:00-00:30"\n')
        self.tick(self.snap([self.lent("a", {"running": []})]))
        self.assertEqual(self.rets, ["a"])
        self.assertNotIn("rest", (P.load_json(P.state_dir() / "borrow.json", {}).get("a") or {}).get("pool") or {})
        self.rets.clear()
        self.write('lend = true\n')
        self.mem("a", want={"ngpu": 2, "vram": 30.0})
        self.tick(self.snap([self.lent("a", {"running": []}, since=NOW - 60)]))   # 借来不到 15 分钟：不还
        self.assertEqual(self.rets, [])
        self.tick(self.snap([self.lent("a", {"running": []}, since=NOW - P.POOL_IDLE_S - 1)]))
        self.assertEqual(self.rets, ["a"])
        nf, = P.load_json(P.state_dir() / "borrow.json", {})["a"]["pool"]["nofit"]
        self.assertEqual(nf[:2], [2, 30.0])
        self.rets.clear()
        B._update(lambda st: st.pop("a", None))
        from fixtures import running_job
        self.tick(self.snap([self.lent("a", {"running": [running_job("x", "pa")]}, since=NOW - 7200)]))    # 有池任务在跑：不还
        self.assertEqual(self.rets, [])

    def test_container_changed(self):
        # R5：组内探测的容器启动时刻与借来时记的不同：直接 finish(gone=True)，不走要还；这台目录里剩的登记回池里；relaunch 不在新容器上拉起
        q = self.pool_list()
        self.mem("a", started=NOW - 86400)
        self.probe("a", started=NOW - 300)
        e = self.lent("a", None)
        out = self.tick(self.snap([e], [], regs=[{"host": "a", "state": "active", "queues": [q]}]))
        self.assertEqual((self.fins, self.rets, out["gone"]), ([("a", True)], [], ["a"]))
        self.assertEqual(self.moves, [("pool", ["jobs/x.txt"], "a 已还")])
        self.assertFalse(P.relaunch_ok(dict(e, status={"t": NOW - 600})))
        self.probe("a", started=NOW - 86400 + 5, last=NOW)                  # 还是借来时那个容器、探在 worker 停写之后：worker 自己没了，可以拉起
        self.assertTrue(P.relaunch_ok(dict(e, status={"t": NOW - 600})))
        self.probe("a", started=NOW - 86400, last=NOW - 900)                # 探测还没赶上：先不拉
        self.assertFalse(P.relaunch_ok(dict(e, status={"t": NOW - 600})))

    def test_relaunch_skips_pool_host(self):
        self.mem("a", started=NOW - 86400)
        self.probe("a", started=NOW - 300)
        h = dict(self.lent("a", None), auto=True, status={"t": NOW - 600})
        started = []
        OV.relaunch({"hosts": [h]}, start=lambda hid: started.append(hid) or (0, ""))
        self.assertEqual(started, [])

    def test_reclaim(self):
        # 收回：持有人本人，或能做平台操作的管理员；按停法停下（request_return now）、清单回池里、记休息期
        q = self.pool_list()
        self.lent("a")
        snap = self.snap([fake_host("a", {"running": []})], [], regs=[{"host": "a", "state": "active", "queues": [q]}])
        moves = []
        mv = lambda to, items, pool="": moves.append((to, pool))      # noqa: E731
        with self.assertRaises(U.Denied) as e:
            P.reclaim("a", who("BB"), False, mv, snap)
        self.assertEqual(e.exception.code, 403)
        with self.assertRaises(U.Denied) as e:
            P.reclaim("a", who("boss", "admin"), False, mv, snap)
        self.assertEqual(e.exception.code, 403)
        with self.assertRaises(U.Denied) as e:
            P.reclaim("b", who("BB"), False, mv, snap)
        self.assertEqual(e.exception.code, 409)
        with mock.patch.object(B, "request_return", return_value="要还了") as rr:
            say = P.reclaim("a", who("AA"), False, mv, snap)
        self.assertEqual(rr.call_args.args[:2], ("a", True))
        self.assertEqual(moves, [("pool", "AA 收回了 a")])
        self.assertIn("1 条清单回池里", say)
        self.assertEqual(P.load_json(P.state_dir() / "borrow.json", {})["a"]["pool"]["rest_why"], "收回过")
        with mock.patch.object(B, "request_return", return_value="要还了"):
            self.assertIn("boss 代收", P.reclaim("a", who("boss", "admin"), True, mv, snap))

    def test_lend_view_says(self):
        s = self.server("a")
        self.assertEqual(P.lend_view(s, NOW, {})["say"], "入池，全天")
        self.mem("a", rest=round(NOW + 3600), rest_why="主人回来过")
        v = P.lend_view(s, NOW)
        self.assertEqual((v["rest"], v["say"]), (round(NOW + 3600), "入池，休息到 %s（主人回来过）" % CF.fmt_time(NOW + 3600, date=False)))
        self.lent("a")
        v = P.lend_view(self.server("a"), NOW)
        self.assertEqual((v["lent"], v["say"]), (True, "算力池借着"))
        B._update(lambda st: st["a"].update(returning=round(NOW)))
        self.assertEqual(P.lend_view(self.server("a"), NOW)["say"], "算力池借着，要还了（在跑的跑完就还）")


class PoolGuardTest(Base):
    """手动借与池借互斥、按人统计不把「算力池」当人、借着期间 last_job 不刷新、路由与权限、拒往池借的主机上直接加清单。"""

    def setUp(self):
        super().setUp()
        self.write('lend = true\n')

    def test_manual_borrow_refused_on_pool(self):
        CF.set_fleet_keys({"borrow_since": CF.fmt_time(NOW, year=True), "borrow_pool": True}, "测试", sid="a")
        with mock.patch.object(B.LS, "need"), self.assertRaises(CF.ConfigError) as e:
            B.begin("a", None, account="BB")
        self.assertIn("正借给算力池", str(e.exception))

    def test_borrower_and_last_job(self):
        CF.set_fleet_keys({"borrow_since": CF.fmt_time(NOW, year=True), "borrow_pool": True}, "测试", sid="a")
        s = self.server("a")
        self.assertEqual(F.borrower(s, F.conf()), CF.POOL_LABEL)
        h = CF.guest_host(s, [{"id": "here", "data_root": "/data/me"}])
        self.assertTrue(h["borrow"]["pool"])
        self.assertIn("（池 · AA）", h["label"])

    def test_route_and_refusals(self):
        r = S.route_of("POST", "/api/pool/reclaim")
        self.assertEqual((r.scope, r.own), ("project", "holder"))
        with mock.patch.object(A.P, "lent_ids", return_value={"a"}):
            out = A.worker("add", "a", ["/r", "jobs/q.txt"], project="pa")
        self.assertEqual(out["rc"], 2)
        self.assertIn("算力池借来的", out["usage"])
        self.assertEqual(P.lent_ids([{"id": "a", "borrow": {"pool": True}}, {"id": "b", "borrow": {}}, {"id": "c", "borrow": {"pool": True}, "returned": "x"}]), {"a"})


class CollectHookTest(Base):
    def test_tick_result_in_snapshot_and_errors_kept_out(self):
        # overview.collect 在快照写盘前跑 pool_tick、结果进快照的 pool；它出错只记服务日志，沿用上一轮的 pool，快照照写
        seen = []
        snap = OV.collect([], use_ssh=False, pool_tick=lambda sn, sw: seen.append(sorted(sn)) or {"lists": {"k": {"why": "x"}}, "gone": []})
        self.assertEqual(snap["pool"]["lists"]["k"]["why"], "x")
        self.assertIn("regs", seen[0])

        def boom(sn, sw):
            raise RuntimeError("坏了")
        with mock.patch.object(OV, "log") as lg:
            snap = OV.collect([], use_ssh=False, pool_tick=boom)
        self.assertEqual(snap["pool"]["lists"]["k"]["why"], "x")
        self.assertIn("算力池这一轮出错", lg.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
