"""算力池清理与基础设施（#212 PR 1）离线测试：
- 旧借用推广型算力池字段（lend / lend_hours / borrow_pool）残留在 hosts.toml 时明确报错提示移除；
- 旧路由 POST /api/pool/lend、/api/pool/reclaim 已彻底移除，不再登记在 serve.ROUTES；
- users.need_holder 对组内服务器与自用主机的本人持有人鉴权（#211 /fleet/<id>.json 复用）；
- 算力池基础设施保留：保留 id `pool`、pool.key、pool.member（PR 1 仅管理员）、pool.locate、worker rm pool 经 locate。
不连服务器：ssh 打桩，主机清单与状态目录都在临时目录。

    python -m unittest tests.test_pool -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import make_token  # noqa: E402
from labmon import api as A  # noqa: E402
from labmon import config as CF  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import fleetview as FV  # noqa: E402
from labmon import pool as P  # noqa: E402
from labmon import serve as S  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon import workerctl as W  # noqa: E402

HOSTS = """[[hosts]]
id = "here"
data_root = "/data/me"
platform_holder = "AA"
platform_job = "AA"
platform_pool = "P"

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
        for name in ("boss", "AA", "BB"):
            make_token(name, role="admin" if name == "boss" else "user", scopes=("read",))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, extra: str = ""):
        (self.d / "hosts.toml").write_text(HOSTS.format(extra=extra))


class LegacyPoolRemovedTest(Base):
    def test_legacy_pool_fields_rejected(self):
        """残留旧借用型算力池字段（lend / lend_hours / borrow_pool）时，config.user_hosts 与 fleet.conf 均给出清晰报错提示移除。"""
        for extra, field in (
            ("lend = true\n", "lend"),
            ('lend_hours = "22:00-09:00"\n', "lend_hours"),
            ("borrow_pool = true\n", "borrow_pool"),
        ):
            self.write(extra)
            for fn in (F.conf, CF.user_hosts, CF.active_hosts):
                with self.assertRaises(CF.ConfigError) as e:
                    fn()
                self.assertIn(field, str(e.exception))
                self.assertIn("移除", str(e.exception))
        # 自用主机 [[hosts]] 残留 lend 同样在两边拦截
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('id = "here"\n', 'id = "here"\nlend = true\n'))
        for fn in (F.conf, CF.user_hosts):
            with self.assertRaises(CF.ConfigError) as e:
                fn()
            self.assertIn("lend", str(e.exception))

    def test_legacy_pool_borrow_never_reaches_overview_or_relaunch(self):
        """升级时若残留未归还的 borrow_since + borrow_pool=true，overview.collect 在读取主机清单即拒绝，绝不放进 active_hosts 或触发 relaunch。"""
        from labmon import overview as O
        self.write('borrow_since = "2026-11-20 10:00"\nborrow_pool = true\n')
        with self.assertRaises(CF.ConfigError) as e:
            O.collect([])
        self.assertIn("borrow_pool", str(e.exception))

    def test_legacy_pool_hours_bw_filtered_from_holders(self):
        """历史 hours/*.jsonl 若仍含 30 天内的 bw["算力池"]，人员页统计不把“算力池”当成真人加入 holders，同时保留原持有人的借出小时数。"""
        import json
        t = 1_700_000_000.0
        h0 = int(t // 3600 * 3600) - 3600
        hdir = F.state_dir() / "hours"
        hdir.mkdir(parents=True, exist_ok=True)
        row = {"h": h0, "holder": "AA", "n": 4, "sh": 0, "uj": 80, "vj": 4000, "cr": 4, "mr": 8, "nr": 4,
               "c": {"job": 4}, "bw": {CF.POOL_LABEL: {"n": 4, "uj": 80, "vj": 4000, "cr": 4, "mr": 8, "nr": 4, "c": {"job": 4}}}}
        (hdir / "a.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n")
        by = FV._by_person(t - 30 * 86400)
        self.assertNotIn(CF.POOL_LABEL, by)
        self.assertIn("AA", by)
        v = FV.view([], t)
        names = [x["holder"] for x in v["holders"]]
        self.assertNotIn(CF.POOL_LABEL, names)
        aa = next(x for x in v["holders"] if x["holder"] == "AA")
        self.assertEqual(aa["lent"], [{"id": "a", "to": CF.POOL_LABEL, "h": 1.0}])

    def test_pool_reserved_id_rejected(self):
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('id = "b"', 'id = "pool"'))
        with self.assertRaises(CF.ConfigError):
            F.conf()
        (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('id = "here"', 'id = "pool"'))
        with self.assertRaises(CF.ConfigError):
            CF.user_hosts()

    def test_legacy_routes_and_fields_removed(self):
        """旧入池/收回路由已从 serve.ROUTES 移除，不再放行普通用户；fleetview 与 users.me 不再包含旧 lend / servers 字段。"""
        for path in ("/api/pool/lend", "/api/pool/reclaim"):
            self.assertFalse(any(r.path.fullmatch(path) for r in S.ROUTES), f"旧路由仍存在：{path}")
        self.assertNotIn("holder", S.OWN)
        v = FV.view([], 1700000000.0)
        for s in v["servers"]:
            self.assertNotIn("lend", s)
        self.assertNotIn("servers", U.me(who("AA"), "lan"))
        self.assertFalse(hasattr(P, "set_lend"))
        self.assertFalse(hasattr(P, "reclaim"))
        self.assertFalse(hasattr(P, "tick"))
        self.assertFalse(hasattr(P, "summary"))
        self.assertFalse(hasattr(P, "need_holder"))

    def test_need_holder_in_users(self):
        """users.need_holder 支持组内服务器与自用主机的持有人鉴权，他人或不存在的服务器一律拒绝（403）。"""
        self.assertEqual(U.need_holder(who("AA"), "a")["id"], "a")
        self.assertEqual(U.need_holder(who("AA"), "here")["id"], "here")
        for u, sid in ((who("BB"), "a"), (who("BB"), "here"), (who("AA"), "b"), (who("AA"), "nope"), (who("nobody"), "a")):
            with self.assertRaises(U.Denied) as e:
                U.need_holder(u, sid)
            self.assertEqual(e.exception.code, 403)


class PoolInfraTest(Base):
    def test_member_and_may_host(self):
        """PR 1 清理旧入池成员判定后，普通用户不是 pool 成员，管理员为真。"""
        self.assertFalse(P.member(who("AA")))
        self.assertFalse(U.may_host(who("AA"), CF.POOL))
        self.assertTrue(P.member(who("boss", "admin")))
        self.assertTrue(U.may_host(who("boss", "admin"), CF.POOL))

    def test_locate_and_worker_rm(self):
        """池待放与带 pool 标记的登记可被 pool.locate 定位；普通主机登记不被误认。"""
        snap = {"regs": [
            {"host": "pool", "state": "pool", "queues": [{"root": "/r/x", "jobs": "jobs/x.txt", "project": "p"}]},
            {"host": "here", "state": "active", "queues": [
                {"root": "/r/y", "jobs": "jobs/y.txt", "project": "p", "pool": True},
                {"root": "/r/z", "jobs": "jobs/z.txt", "project": "p"},
            ]},
        ]}
        self.assertEqual(P.locate("/r/x/", "jobs/x.txt", snap), "pool")
        self.assertEqual(P.locate("/r/y", "jobs/y.txt", snap), "here")
        self.assertEqual(P.locate("/r/z", "jobs/z.txt", snap), "pool")
        self.assertEqual(P.locate("/r/none", "jobs/n.txt", snap), "pool")
        self.assertEqual(W.via("pool")["id"], "here")
        got = []
        with mock.patch.object(W, "run_out", side_effect=lambda *a, **k: got.append(a) or (0, "ok\n")), \
                mock.patch.object(P, "locate", return_value="here"):
            r = A.worker("rm", "pool", ["/r/y", "jobs/y.txt"])
        self.assertEqual(r["rc"], 0)
        self.assertEqual(got[0][:2], ("rm", "here"))


if __name__ == "__main__":
    unittest.main()
