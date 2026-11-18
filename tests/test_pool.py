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
from labmon import api as A  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
