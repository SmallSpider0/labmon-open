"""共有型算力池（#212 PR 2）与旧借用池防回归（PR 1）离线测试：
- 旧借用推广型算力池字段（lend / lend_hours / borrow_pool）与旧路由彻底移除；
- `[pools.<id>]` 多池配置强校验（非法 id、重名、非 share_host、独立机、借用机、跨 worker_home、单机双池互斥、默认池解析、exclusive 缺省 true）；
- 权限与隐私隔离（池成员 / 通配 * / 非成员 / 管理员、pool.summary 脱敏、kind=pool 归 sched 分类且按本人项目过滤、exclusive=true 拦截直投单台）；
- 即时落机、全池忙选台、按人公平份额与内存扣减防单人霸占、Work-Stealing 空闲窃取与 10 分钟冷却防抖、异常自动撤离、依赖与 elsewhere 暂留、全池 nofit 告警；
- 多池共存、worker status/fit --list/add/rm 路由、pool._heads 读错不误标 done（#99 PR #93）。
不连服务器：ssh 打桩，主机清单与状态目录都在临时目录。

    python -m unittest tests.test_pool -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import make_token  # noqa: E402
from labmon import api as A  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import config as CF  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import fleetview as FV  # noqa: E402
from labmon import lease as LS  # noqa: E402
from labmon import pool as P  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import records as RC  # noqa: E402
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

SHARED_HOSTS = """[[hosts]]
id = "h1"
ssh = "root@10.0.0.1"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
share_host = true
label = "A100-1"

[[hosts]]
id = "h2"
ssh = "root@10.0.0.2"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
share_host = true
label = "A100-2"

[[hosts]]
id = "h3"
ssh = "root@10.0.0.3"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
share_host = true
label = "4090-1"

[[hosts]]
id = "solo"
ssh = "root@10.0.0.4"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
users = ["alice"]
label = "自用机"

{pools}
"""


def who(name: str, role: str = "user") -> U.Ident:
    return U.Ident(name, role, frozenset({"read", "project"}), "token", "k")


def make_host_entry(hid: str, *, gpus: int = 2, busy_gpus: int = 0, running: list | None = None,
                    waiting: list | None = None, overrides: list | None = None,
                    share: dict | None = None, pool_heads: dict | None = None,
                    worker_up: bool = True, error: str | None = None, t: float = 1_700_000_000.0) -> dict:
    run = list(running or [])
    for i in range(busy_gpus):
        if i >= len(run):
            run.append({"name": f"busy_{i}", "project": "other", "root": "/r/o", "queue": "jobs/o.txt",
                        "gpus": [f"GPU-{hid}-{i}"], "t0": t - 600})
    gs = [{"i": i, "uuid": f"GPU-{hid}-{i}", "name": "A100", "total": 40000.0,
           "used": 20000.0 if i < busy_gpus else 200.0, "free": 20000.0 if i < busy_gpus else 39800.0,
           "util": 90.0 if i < busy_gpus else 0.0} for i in range(gpus)]
    st = {
        "t": t, "pid": 1000, "host": hid,
        "info": {"gpus": gs, "nproc": 32, "cpu_quota": 16.0, "cpu_used": 2.0,
                 "mem": {"limit": 128.0, "used": 10.0, "anon": 4.0}},
        "running": run, "external": [], "waiting": list(waiting or []),
        "overrides": list(overrides or []),
        "share": share or {"use": {}, "weight": {}, "run": [], "half_life_h": 24.0},
        "plan": {"t": t, "lanes": []},
        "pool_heads": pool_heads or {},
    }
    return {"id": hid, "label": hid, "error": error, "status_error": None,
            "status": None if error else st, "worker_up": worker_up and not error,
            "lease_end": t + 86400}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"LABMON_HOSTS": str(self.d / "hosts.toml"), "LABMON_STATE": str(self.d / "state")})
        self.env.start()
        self.write()
        for name in ("boss", "AA", "BB", "alice", "bob", "carol"):
            make_token(name, role="admin" if name == "boss" else "user", scopes=("read", "project"))

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
        for bad in ("pool", "pool:a100"):
            (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('id = "b"', f'id = "{bad}"'))
            with self.assertRaises(CF.ConfigError):
                F.conf()
            (self.d / "hosts.toml").write_text(HOSTS.format(extra="").replace('id = "here"', f'id = "{bad}"'))
            with self.assertRaises(CF.ConfigError):
                CF.user_hosts()

    def test_legacy_routes_removed_and_shared_pool_present(self):
        """旧入池/收回路由已从 serve.ROUTES 移除；PR 2 恢复共有池引擎 pool.tick 与 pool.summary。"""
        for path in ("/api/pool/lend", "/api/pool/reclaim"):
            self.assertFalse(any(r.path.fullmatch(path) for r in S.ROUTES), f"旧路由仍存在：{path}")
        self.assertNotIn("holder", S.OWN)
        v = FV.view([], 1700000000.0)
        for s in v["servers"]:
            self.assertNotIn("lend", s)
        self.assertNotIn("servers", U.me(who("AA"), "lan"))
        self.assertFalse(hasattr(P, "set_lend"))
        self.assertFalse(hasattr(P, "reclaim"))
        self.assertFalse(hasattr(P, "need_holder"))
        self.assertTrue(hasattr(P, "tick"))
        self.assertTrue(hasattr(P, "summary"))

    def test_need_holder_in_users(self):
        """users.need_holder 支持组内服务器与自用主机的持有人鉴权，他人或不存在的服务器一律拒绝（403）。"""
        self.assertEqual(U.need_holder(who("AA"), "a")["id"], "a")
        self.assertEqual(U.need_holder(who("AA"), "here")["id"], "here")
        for u, sid in ((who("BB"), "a"), (who("BB"), "here"), (who("AA"), "b"), (who("AA"), "nope"), (who("nobody"), "a")):
            with self.assertRaises(U.Denied) as e:
                U.need_holder(u, sid)
            self.assertEqual(e.exception.code, 403)


class PoolConfigValidationTest(Base):
    def write_shared(self, pools_toml: str):
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools=pools_toml))

    def test_single_and_multi_pool_resolution(self):
        # 单命名池时，pool 自动指向该池；default 池直接叫 pool
        self.write_shared("""
[pools.a100]
label = "A100 共有池"
hosts = ["h1", "h2"]
members = ["alice", "bob"]
""")
        pm = CF.pools()
        self.assertEqual(list(pm), ["a100"])
        self.assertEqual(pm["a100"]["host_id"], "pool:a100")
        self.assertTrue(pm["a100"]["exclusive"])
        self.assertEqual(CF.resolve_pool("pool")["id"], "a100")
        self.assertEqual(CF.resolve_pool("pool:a100")["id"], "a100")
        self.assertEqual(CF.host_pool("h1")["id"], "a100")
        self.assertIsNone(CF.host_pool("solo"))

        # 多池且无 default 时，pool 必须明确报错提示 pool:<id>
        self.write_shared("""
[pools.a100]
label = "A100 池"
hosts = ["h1", "h2"]
members = ["alice"]

[pools.rtx4090]
label = "4090 池"
hosts = ["h3"]
members = ["bob"]
exclusive = false
""")
        pm = CF.pools()
        self.assertEqual(set(pm), {"a100", "rtx4090"})
        self.assertFalse(pm["rtx4090"]["exclusive"])
        self.assertEqual(CF.resolve_pool("pool:rtx4090")["id"], "rtx4090")
        with self.assertRaises(CF.ConfigError) as e:
            CF.resolve_pool("pool")
        self.assertIn("pool:a100", str(e.exception))
        self.assertIn("pool:rtx4090", str(e.exception))

        # 多池含 [pools.pool] 默认池时，pool 指向默认池
        self.write_shared("""
[pools.pool]
label = "默认共有池"
hosts = ["h1", "h2"]
members = ["*"]

[pools.rtx4090]
label = "4090 池"
hosts = ["h3"]
members = ["bob"]
""")
        self.assertEqual(CF.resolve_pool("pool")["id"], "pool")
        self.assertEqual(CF.resolve_pool("pool")["host_id"], "pool")

    def test_validation_rejects_bad_configs(self):
        cases = [
            # 非法池 id
            ('[pools."Bad!"]\nhosts = ["h1"]\nmembers = ["alice"]\n', "不合法"),
            # 与主机重名
            ('[pools.h1]\nhosts = ["h2"]\nmembers = ["alice"]\n', "重名"),
            # 未知键
            ('[pools.a100]\nhosts = ["h1"]\nmembers = ["alice"]\nextra = 1\n', "不认识"),
            # 本机/回环主机（非 share_host）
            ('[pools.a100]\nhosts = ["local1"]\nmembers = ["alice"]\n', "本机或回环"),
            # 单机双池互斥
            ('[pools.a100]\nhosts = ["h1"]\nmembers = ["alice"]\n[pools.b]\nhosts = ["h1"]\nmembers = ["bob"]\n', "同时出现"),
            # 不存在的主机
            ('[pools.a100]\nhosts = ["nope"]\nmembers = ["alice"]\n', "不在 [[hosts]]"),
        ]
        for toml, expect in cases:
            self.write_shared(toml + '\n[[hosts]]\nid = "local1"\n')
            with self.assertRaises(CF.ConfigError, msg=toml) as e:
                CF.pools()
            self.assertIn(expect, str(e.exception))

        # 独立主机与不同 worker_home 混池
        (self.d / "hosts.toml").write_text("""
[[hosts]]
id = "h1"
ssh = "root@10.0.0.1"
worker_home = "/data/a/labmon"
share_host = true

[[hosts]]
id = "h2"
ssh = "root@10.0.0.2"
worker_home = "/data/b/labmon"
share_host = true

[pools.mix]
hosts = ["h1", "h2"]
members = ["alice"]
""")
        with self.assertRaises(CF.ConfigError) as e:
            CF.pools()
        self.assertIn("worker_home", str(e.exception))

        (self.d / "hosts.toml").write_text("""
[[hosts]]
id = "ind1"
ssh = "root@10.0.0.1"
standalone = true
share_host = true

[pools.s]
hosts = ["ind1"]
members = ["alice"]
""")
        with self.assertRaises(CF.ConfigError) as e:
            CF.pools()
        self.assertIn("独立主机", str(e.exception))

        # 借用机不能加入共有池
        (self.d / "hosts.toml").write_text("""
[[hosts]]
id = "b1"
ssh = "root@10.0.0.1"
share_host = true
[hosts.borrow]
holder = "bob"
since = 1

[pools.s]
hosts = ["b1"]
members = ["alice"]
""")
        with self.assertRaises(CF.ConfigError) as e:
            CF.pools()
        self.assertIn("借用", str(e.exception))


class PoolAuthAndExclusiveTest(Base):
    def setUp(self):
        super().setUp()
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools="""
[pools.a100]
label = "A100 共有池"
hosts = ["h1", "h2"]
members = ["alice"]
exclusive = true

[pools.rtx4090]
label = "4090 开放池"
hosts = ["h3"]
members = ["*"]
exclusive = false
"""))
        PJ.put_config("pa", 'id = "pa"\n', owner="alice")
        PJ.put_config("pb", 'id = "pb"\n', owner="bob")

    def test_membership_and_may_host(self):
        alice, bob, carol, boss = who("alice"), who("bob"), who("carol"), who("boss", "admin")
        self.assertTrue(P.member(alice, "a100"))
        self.assertFalse(P.member(bob, "a100"))
        self.assertTrue(P.member(bob, "rtx4090"))          # 通配 *
        self.assertTrue(P.member(boss, "a100"))
        # 池入口与池内主机只读权限
        self.assertTrue(U.may_host(alice, "pool:a100"))
        self.assertTrue(U.may_host(alice, "h1"))
        self.assertFalse(U.may_host(bob, "pool:a100"))
        self.assertFalse(U.may_host(bob, "h1"))
        self.assertTrue(U.may_host(carol, "pool:rtx4090"))
        self.assertTrue(U.may_host(carol, "h3"))
        # users.me 暴露 pools 与 pool_member
        me_a = U.me(alice, "lan")
        self.assertTrue(me_a["pool_member"])
        self.assertEqual(set(me_a["pools"]), {"pool:a100", "pool:rtx4090"})

    def test_exclusive_blocks_direct_add_move_assign(self):
        # exclusive=true 的 h1：管理员与普通用户均不能直接 worker add h1 / reg_move h1 / host_assign h1
        for caller in (who("boss", "admin"), who("alice")):
            r = A.worker("add", "h1", ["/data/lab/pa", "jobs/q.txt"], project="pa", who=caller)
            self.assertEqual(r["rc"], 2)
            self.assertIn("pool:a100", r["err"])
        with self.assertRaises(CF.ConfigError) as e:
            S.reg_move("h1", [{"host": "solo", "project": "pa", "root": "/data/lab/pa", "jobs": "jobs/q.txt"}])
        self.assertIn("pool:a100", str(e.exception))
        with mock.patch.object(LS, "need"), self.assertRaises(CF.ConfigError) as e:
            S.host_assign("h1", "pa")
        self.assertIn("pool:a100", str(e.exception))

        # 共有池引擎内部传 pool="a100" 允许落机到 h1；开放池 h3（exclusive=false）允许直接 add
        with mock.patch.object(W, "run_out", return_value=(0, "ok\n")) as run:
            S.reg_move("h1", [{"host": "pool:a100", "project": "pa", "root": "/data/lab/pa", "jobs": "jobs/q.txt"}],
                       pool="a100", pool_reason="测试落机")
            self.assertEqual(run.call_args.kwargs.get("pool"), "a100")
            r = A.worker("add", "h3", ["/data/lab/pa", "jobs/q.txt"], project="pa", who=who("alice"))
            self.assertEqual(r["rc"], 0)

    def test_summary_and_records_privacy(self):
        # 仅给 alice 分配 a100，去掉 rtx4090 的通配以测非成员
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools="""
[pools.a100]
label = "A100 共有池"
hosts = ["h1", "h2"]
members = ["alice"]
"""))
        snap_pool = {
            "pools": [{"id": "a100", "host_id": "pool:a100", "label": "A100 共有池", "exclusive": True,
                       "members": ["alice"], "hosts": [{"id": "h1", "online": True}], "online": 1, "total": 2,
                       "gpus": 2, "free_gpus": 1, "running": 1, "queued": 0, "pending": 2, "placed": 1}],
            "lists": {
                "/data/lab/pa|jobs/a.txt": {"pool": "a100", "project": "pa", "root": "/data/lab/pa", "jobs": "jobs/a.txt"},
                "/data/lab/pb|jobs/b.txt": {"pool": "a100", "project": "pb", "root": "/data/lab/pb", "jobs": "jobs/b.txt"},
            },
        }
        # 管理员全量可见（含 members 与全部 lists）
        adm = P.summary(snap_pool, who("boss", "admin"))
        self.assertEqual(len(adm["lists"]), 2)
        self.assertEqual(adm["pools"][0]["members"], ["alice"])
        # 池成员 alice：看不到 members，lists 只留本人 pa
        mem = P.summary(snap_pool, who("alice"))
        self.assertNotIn("members", mem["pools"][0])
        self.assertEqual(list(mem["lists"]), ["/data/lab/pa|jobs/a.txt"])
        # 非池成员 bob：返回 None
        self.assertIsNone(P.summary(snap_pool, who("bob")))

        # kind="pool" 事件归入 sched 分类，且普通池成员仅见自己项目的迁移记录，不因能看池内主机 h1 而泄露他人 pb
        self.assertEqual(RC.cat_of("pool"), "sched")
        ev_a = {"kind": "pool", "hosts": ["h1", "pool:a100"], "project": "pa", "msg": "pa 搬到 h1"}
        ev_b = {"kind": "pool", "hosts": ["h1", "pool:a100"], "project": "pb", "msg": "pb 搬到 h1"}
        vis_alice = U.visible(who("alice"), U.Access({"pa"}, {"solo"}, {"solo", "h1", "h2"}))
        self.assertTrue(vis_alice(ev_a))
        self.assertFalse(vis_alice(ev_b))

    def test_view_filters_cross_pool_heads(self):
        """Codex P1：同 worker_home 两个不同成员池，普通用户经可见主机 status.pool_heads 绝不能读到未加入池或他人项目的待放清单。"""
        k_a = "pool:a100|/data/lab/pa|jobs/secret_a.txt"
        k_b = "pool:rtx4090|/data/lab/pb|jobs/my_b.txt"
        k_b_other = "pool:rtx4090|/data/lab/pa|jobs/other_in_4090.txt"
        ph = {
            k_a: {"pool": "pool:a100", "pool_id": "a100", "head": "secret_a1", "n": 2},
            k_b: {"pool": "pool:rtx4090", "pool_id": "rtx4090", "head": "my_b1", "n": 1},
            k_b_other: {"pool": "pool:rtx4090", "pool_id": "rtx4090", "head": "other_a1", "n": 1},
        }
        snap = {
            "generated": 1_700_000_000.0, "lan": {"ok": True}, "event_kinds": {}, "event_cats": [],
            "hosts": [make_host_entry("h1", pool_heads=ph), make_host_entry("h3", pool_heads=ph)],
            "projects": [{"id": "pa"}, {"id": "pb"}], "archived": [], "rebalance": {}, "colors": {},
            "regs": [
                {"host": "pool:a100", "state": "pool", "queues": [{"root": "/data/lab/pa", "jobs": "jobs/secret_a.txt", "project": "pa"}]},
                {"host": "pool:rtx4090", "state": "pool", "queues": [
                    {"root": "/data/lab/pb", "jobs": "jobs/my_b.txt", "project": "pb"},
                    {"root": "/data/lab/pa", "jobs": "jobs/other_in_4090.txt", "project": "pa"},
                ]},
            ],
            "occ": {}, "pool": None,
        }
        # bob 仅属于 rtx4090（h3），无权访问 a100（h1）与 pa
        v_bob = U.view(who("bob"), snap)
        self.assertEqual([h["id"] for h in v_bob["hosts"]], ["h3"])
        got_ph = v_bob["hosts"][0]["status"]["pool_heads"]
        self.assertNotIn(k_a, got_ph)
        self.assertNotIn(k_b_other, got_ph)
        self.assertIn(k_b, got_ph)

    def test_remove_user_clears_pool_member(self):
        """Codex P1：删除账号时同步清理 [pools.*].members 显式成员；重建同名账号绝不继承旧池权限。"""
        self.assertTrue(P.member(who("alice"), "a100"))
        self.assertTrue(U.may_host(who("alice"), "h1"))
        self.assertTrue(U.may_host(who("alice"), "pool:a100"))
        # 删除 alice 后为新人重新开通同名账号 alice
        with mock.patch.object(U, "_check_bind"):
            U.remove("alice", "boss")
            U.add("alice", "user", "boss", person="NewAlice")
        self.assertNotIn("alice", CF.pools()["a100"]["members"])
        self.assertFalse(P.member(who("alice"), "a100"))
        self.assertFalse(U.may_host(who("alice"), "h1"))
        self.assertFalse(U.may_host(who("alice"), "pool:a100"))
        # 不影响其他池成员 bob
        self.assertTrue(P.member(who("bob"), "rtx4090"))


class PoolSchedulingTest(Base):
    def setUp(self):
        super().setUp()
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools="""
[pools.a100]
label = "A100 共有池"
hosts = ["h1", "h2", "h3"]
members = ["alice", "bob"]
"""))
        PJ.put_config("pa", 'id = "pa"\n', owner="alice")
        PJ.put_config("pb", 'id = "pb"\n', owner="bob")
        self.t = 1_700_000_000.0
        self.sw = {"owner": {"pa": "k-alice", "pb": "k-bob"}, "weight": {}, "focus": {}, "pause": {}}

    def test_immediate_placement_fairness_and_load_ranking(self):
        # 场景：alice 已用 20 卡·时、bob 已用 1 卡·时；三人份清单同时入待放（pa 两条、pb 一条）；h1 有 2 空卡、h2 有 1 空卡、h3 满载
        # 期望：按人公平先给 bob 落最空台 h1，再给 alice 的第一条落 h2，第二条全池忙即时落到预计最快空出机 h3；每台同轮最多落 1 条
        heads = {
            "pool:a100|/data/lab/pa|jobs/a1.txt": {"pool": "pool:a100", "pool_id": "a100", "head": "a1", "n": 2},
            "pool:a100|/data/lab/pa|jobs/a2.txt": {"pool": "pool:a100", "pool_id": "a100", "head": "a2", "n": 2},
            "pool:a100|/data/lab/pb|jobs/b1.txt": {"pool": "pool:a100", "pool_id": "a100", "head": "b1", "n": 2},
        }
        sh = {"use": {"pa": 20.0, "pb": 1.0}, "weight": {}, "run": [], "half_life_h": 24.0}
        hosts = [
            make_host_entry("h1", gpus=2, busy_gpus=0, share=sh, pool_heads=heads, t=self.t),
            make_host_entry("h2", gpus=2, busy_gpus=1, share=sh, pool_heads=heads, t=self.t),
            make_host_entry("h3", gpus=1, busy_gpus=1, share=sh, pool_heads=heads, t=self.t),
        ]
        regs = [{
            "host": "pool:a100", "state": "pool", "queues": [
                {"root": "/data/lab/pa", "jobs": "jobs/a1.txt", "project": "pa"},
                {"root": "/data/lab/pa", "jobs": "jobs/a2.txt", "project": "pa"},
                {"root": "/data/lab/pb", "jobs": "jobs/b1.txt", "project": "pb"},
            ],
        }]
        moves = []

        def fake_fit(pcfg, it, cands):
            table = {
                "h1": {"ok": True, "nofit": False, "free_cards": 2, "free_vram": 80000, "same_proj": False, "load": {"run": 0, "wait": 0, "until": 0}},
                "h2": {"ok": True, "nofit": False, "free_cards": 1, "free_vram": 40000, "same_proj": False, "load": {"run": 1, "wait": 0, "until": self.t + 600}},
                "h3": {"ok": False, "nofit": False, "why": "显存不够", "free_cards": 0, "free_vram": 1000, "same_proj": False,
                       "load": {"run": 1, "wait": 0, "until": self.t + 300}},
            }
            return {"head": it["head"], "need": {"ngpu": 1, "est_min": 120}, "hosts": {h: table[h] for h in cands if h in table}}

        with mock.patch.object(LS, "primary", return_value=True):
            res = P.tick(hosts, regs, self.sw, self.t, move_fn=lambda to, it, pid, rsn: moves.append((to, it["project"], it["jobs"], rsn)), fit_fn=fake_fit)
        self.assertEqual([(m[0], m[1]) for m in moves], [("h1", "pb"), ("h2", "pa"), ("h3", "pa")])
        self.assertEqual(res["pools"][0]["pending"], 0)
        self.assertEqual(res["pools"][0]["placed"], 3)

    def test_work_stealing_and_cooldown(self):
        # h2 上 pa 的池清单正在排队（无在跑任务），此时 h1 空闲 → 立刻空闲窃取搬到 h1
        hosts = [
            make_host_entry("h1", gpus=1, busy_gpus=0, t=self.t),
            make_host_entry("h2", gpus=1, busy_gpus=1,
                            waiting=[{"name": "a1", "project": "pa", "root": "/data/lab/pa", "queue": "jobs/a1.txt",
                                      "since": self.t - 60, "why": "显存不够"}], t=self.t),
        ]
        regs = [{"host": "h2", "state": "active", "queues": [
            {"root": "/data/lab/pa", "jobs": "jobs/a1.txt", "project": "pa", "pool": "a100"},
        ]}]
        moves = []

        def fake_fit(pcfg, it, cands):
            return {"head": "a1", "need": {"ngpu": 1, "est_min": 60}, "hosts": {
                h: {"ok": h == "h1", "nofit": False, "free_cards": 1 if h == "h1" else 0,
                    "free_vram": 40000 if h == "h1" else 0, "same_proj": False, "load": {"run": 0 if h == "h1" else 1, "wait": 0, "until": 0}}
                for h in cands
            }}

        with mock.patch.object(LS, "primary", return_value=True):
            P.tick(hosts, regs, self.sw, self.t, move_fn=lambda to, it, pid, rsn: moves.append((to, it["src"])), fit_fn=fake_fit)
        self.assertEqual(moves, [("h1", "h2")])

        # 冷却期（600 秒）内即使反过来 h2 空闲也不来回乒乓
        moves.clear()
        hosts2 = [
            make_host_entry("h1", gpus=1, busy_gpus=1,
                            waiting=[{"name": "a1", "project": "pa", "root": "/data/lab/pa", "queue": "jobs/a1.txt",
                                      "since": self.t + 60, "why": "显存不够"}], t=self.t + 120),
            make_host_entry("h2", gpus=1, busy_gpus=0, t=self.t + 120),
        ]
        regs2 = [{"host": "h1", "state": "active", "queues": [
            {"root": "/data/lab/pa", "jobs": "jobs/a1.txt", "project": "pa", "pool": "a100"},
        ]}]
        with mock.patch.object(LS, "primary", return_value=True):
            P.tick(hosts2, regs2, self.sw, self.t + 120, move_fn=lambda to, it, pid, rsn: moves.append((to, it["src"])), fit_fn=fake_fit)
        self.assertEqual(moves, [])

    def test_evacuation_on_drain_and_down_10min(self):
        # h1 处于 drain，h2 连续失联：先在 t0 记失联起点（不立刻撤离），满 10 分钟后自动撤离到 h3
        regs = [
            {"host": "h1", "state": "active", "queues": [{"root": "/data/lab/pa", "jobs": "jobs/a1.txt", "project": "pa", "pool": "a100"}]},
            {"host": "h2", "state": "active", "queues": [{"root": "/data/lab/pb", "jobs": "jobs/b1.txt", "project": "pb", "pool": "a100"}]},
        ]
        hosts = [
            make_host_entry("h1", overrides=[{"kind": "drain", "until": None}], t=self.t),
            make_host_entry("h2", worker_up=False, error="ssh timeout", t=self.t),
            make_host_entry("h3", gpus=2, busy_gpus=0, t=self.t),
        ]
        moves = []

        def fake_fit(pcfg, it, cands):
            return {"head": "x", "need": {"ngpu": 1, "est_min": 30},
                    "hosts": {h: {"ok": True, "nofit": False, "free_cards": 2, "free_vram": 80000, "same_proj": False, "load": {"run": 0, "wait": 0, "until": 0}} for h in cands}}

        with mock.patch.object(LS, "primary", return_value=True):
            P.tick(hosts, regs, self.sw, self.t, move_fn=lambda to, it, pid, rsn: moves.append((it["src"], to, rsn)), fit_fn=fake_fit)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][:2], ("h1", "h3"))
        self.assertIn("drain", moves[0][2])

        # 过 601 秒后 h2 仍失联 → 撤离 h2 上的 pb 到 h3
        moves.clear()
        regs_after = [{"host": "h2", "state": "active", "queues": [{"root": "/data/lab/pb", "jobs": "jobs/b1.txt", "project": "pb", "pool": "a100"}]}]
        with mock.patch.object(LS, "primary", return_value=True):
            P.tick(hosts, regs_after, self.sw, self.t + P.POOL_DOWN_S + 1,
                   move_fn=lambda to, it, pid, rsn: moves.append((it["src"], to, rsn)), fit_fn=fake_fit)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][:2], ("h2", "h3"))
        self.assertIn("失联", moves[0][2])

    def test_down_evacuation_resets_across_lan_offline(self):
        """Codex P1：首次失败后离网超过 10 分钟再回内网，不把离网时间算成主机失联；需回网后连续有效探测失联满 10 分钟才撤离。"""
        regs = [{"host": "h2", "state": "active", "queues": [{"root": "/data/lab/pb", "jobs": "jobs/b1.txt", "project": "pb", "pool": "a100"}]}]
        hosts = [
            make_host_entry("h2", worker_up=False, error="ssh timeout", t=self.t),
            make_host_entry("h3", gpus=2, busy_gpus=0, t=self.t),
        ]
        moves = []

        def fake_fit(pcfg, it, cands):
            return {"head": "x", "need": {"ngpu": 1, "est_min": 30},
                    "hosts": {h: {"ok": True, "nofit": False, "free_cards": 2, "free_vram": 80000, "same_proj": False, "load": {"run": 0, "wait": 0, "until": 0}} for h in cands}}

        with mock.patch.object(LS, "primary", return_value=True):
            # 1. t0 内网首次失败：不撤离
            P.tick(hosts, regs, self.sw, self.t, lan_ok=True, move_fn=lambda to, it, pid, rsn: moves.append(to), fit_fn=fake_fit)
            self.assertEqual(moves, [])
            # 2. t0 + 60 离网（lan_ok=False）：重置未判掉线的连续失联计数
            P.tick(hosts, regs, self.sw, self.t + 60, lan_ok=False, move_fn=lambda to, it, pid, rsn: moves.append(to), fit_fn=fake_fit)
            self.assertEqual(moves, [])
            # 3. t0 + 700（离网满 10 分钟后）回内网首次失败：应重新起算，绝不能立刻撤离
            P.tick(hosts, regs, self.sw, self.t + P.POOL_DOWN_S + 100, lan_ok=True, move_fn=lambda to, it, pid, rsn: moves.append(to), fit_fn=fake_fit)
            self.assertEqual(moves, [])
            # 4. 回网后持续失联满 10 分钟（t0 + 700 + 601）再次有效探测失败：此刻才撤离到 h3
            P.tick(hosts, regs, self.sw, self.t + P.POOL_DOWN_S * 2 + 101, lan_ok=True, move_fn=lambda to, it, pid, rsn: moves.append(to), fit_fn=fake_fit)
            self.assertEqual(moves, ["h3"])

    def test_dep_wait_elsewhere_and_nofit_alert(self):
        # 队首等依赖（#98）与 elsewhere 暂留待放不落机；全池 nofit 标记 nofit=True 并由 collect._lane 立即报 pool_wait:...:nofit
        heads = {
            "pool:a100|/data/lab/pa|jobs/dep.txt": {"pool": "pool:a100", "pool_id": "a100", "head": "d1", "n": 1,
                                                    "dep": ["wait", "等 prep 跑完"], "dep_end": self.t + 1800},
            "pool:a100|/data/lab/pa|jobs/ew.txt": {"pool": "pool:a100", "pool_id": "a100", "head": "e2", "n": 1,
                                                   "elsewhere": ["h2", "e1"]},
            "pool:a100|/data/lab/pa|jobs/big.txt": {"pool": "pool:a100", "pool_id": "a100", "head": "big1", "n": 1},
        }
        hosts = [make_host_entry("h1", gpus=1, busy_gpus=0, pool_heads=heads, t=self.t)]
        regs = [{"host": "pool:a100", "state": "pool", "queues": [
            {"root": "/data/lab/pa", "jobs": "jobs/dep.txt", "project": "pa"},
            {"root": "/data/lab/pa", "jobs": "jobs/ew.txt", "project": "pa"},
            {"root": "/data/lab/pa", "jobs": "jobs/big.txt", "project": "pa"},
        ]}]
        moves = []

        def nofit_all(pcfg, it, cands):
            return {"head": it["head"], "need": {"ngpu": 8, "est_min": 60},
                    "hosts": {h: {"ok": False, "nofit": True, "why": "要 8 张卡，只有 1 张"} for h in cands}}

        with mock.patch.object(LS, "primary", return_value=True):
            res = P.tick(hosts, regs, self.sw, self.t, move_fn=lambda *a: moves.append(a), fit_fn=nofit_all)
        self.assertEqual(moves, [])
        self.assertIn("等 prep 跑完", res["lists"][P.key("/data/lab/pa", "jobs/dep.txt", "a100")]["why"])
        self.assertIn("h2", res["lists"][P.key("/data/lab/pa", "jobs/ew.txt", "a100")]["why"])
        big = res["lists"][P.key("/data/lab/pa", "jobs/big.txt", "a100")]
        self.assertTrue(big["nofit"])
        self.assertIn("要 8 张卡", big["why"])

        # collect._lane 收到 nofit=True 立即产出 pool_wait:...:nofit 告警（不用等 POOL_TELL_S）
        qcfg = {"id": "big", "name": "车道 1", "host": "pool:a100", "type": "gpu_queue", "discover": False,
                "root": "/data/lab/pa", "jobs": "jobs/big.txt"}
        h_pool = {"id": "pool:a100", "label": "A100 共有池", "returned": None, "pool": True}
        cfg = {"id": "pa", "groups": [], "auto_groups": False, "alerts": {"log_silent_min": 0}, "hosts": [h_pool]}
        raw_by_host = {"pool:a100": {"t": self.t, "queues": {"big": {
            "jobs": [{"name": "big1", "est_min": 60.0}], "done": [], "failed": [], "current": [], "lock_alive": False,
        }}}}
        snap = {"hosts": [{"id": "pool:a100", "ok": True}], "lanes": [], "jobs": [], "alerts": [], "pool": res["lists"]}
        work = {"running": set(), "hcfg": {"pool:a100": h_pool}, "ratios": {}, "multi": set()}
        C._lane(qcfg, cfg, snap, raw_by_host, work, self.t + 10)
        self.assertTrue(any("pool_wait:" in a["key"] and a["key"].endswith(":nofit") for a in snap["alerts"]), snap["alerts"])

    def test_heads_merge_prefers_readable_and_status_text(self):
        # #99 PR #93：某台读失败带 err，另一台读得到 → 合并取读得到的那份，不误丢也不误标 done
        k = "pool:a100|/data/lab/pa|jobs/q.txt"
        h1 = make_host_entry("h1", pool_heads={k: {"pool": "pool:a100", "pool_id": "a100", "head": None, "n": 0, "err": "清单读不到"}}, t=self.t + 5)
        h2 = make_host_entry("h2", pool_heads={k: {"pool": "pool:a100", "pool_id": "a100", "head": "j1", "n": 2}}, t=self.t)
        all_h = {str(h["id"]): h for h in CF.all_hosts()}
        merged = P._heads([h1, h2], "/data/lab/labmon", all_h)
        self.assertEqual(merged[("pool:a100", "/data/lab/pa|jobs/q.txt")]["head"], "j1")

        # status_text：管理员看全部待放，普通用户脱敏仅看本人项目
        pcfg = CF.resolve_pool("pool:a100")
        snap = {
            "hosts": [h1, h2],
            "pool": {"pools": [], "lists": {
                "/data/lab/pa|jobs/a.txt": {"pool": "a100", "project": "pa", "root": "/data/lab/pa", "jobs": "jobs/a.txt", "head": "a1", "since": self.t - 120, "why": "等空卡"},
                "/data/lab/pb|jobs/b.txt": {"pool": "a100", "project": "pb", "root": "/data/lab/pb", "jobs": "jobs/b.txt", "head": "b1", "since": self.t - 120, "why": "等空卡"},
            }},
            "sched": {"half_life_h": 24.0, "projects": [
                {"id": "pa", "person": "alice", "use": 5.0, "cards": 1.0},
                {"id": "pb", "person": "bob", "use": 8.0, "cards": 1.0},
            ]},
        }
        adm_txt = P.status_text(pcfg, snap, own=False, now=self.t)
        self.assertIn("pa /data/lab/pa/jobs/a.txt", adm_txt)
        self.assertIn("pb /data/lab/pb/jobs/b.txt", adm_txt)
        usr_txt = P.status_text(pcfg, snap, own=True, projects={"pa"}, now=self.t)
        self.assertIn("pa /data/lab/pa/jobs/a.txt", usr_txt)
        self.assertNotIn("pb", usr_txt)

    def test_picked_host_does_not_mark_next_list_nofit(self):
        """Codex P2：异构池大显存机 h1 本轮已落 1 条进入 picked 后，下一条仅评小显存机 h2 返回 nofit，绝不标全池 nofit=True。"""
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools="""
[pools.a100]
label = "异构池"
hosts = ["h1", "h2"]
members = ["alice", "bob"]
"""))
        k1 = "pool:a100|/data/lab/pa|jobs/big1.txt"
        k2 = "pool:a100|/data/lab/pb|jobs/big2.txt"
        heads = {
            k1: {"pool": "pool:a100", "pool_id": "a100", "head": "a1", "n": 1},
            k2: {"pool": "pool:a100", "pool_id": "a100", "head": "b1", "n": 1},
        }
        hosts = [
            make_host_entry("h1", gpus=1, busy_gpus=0, pool_heads=heads, t=self.t),
            make_host_entry("h2", gpus=1, busy_gpus=0, pool_heads=heads, t=self.t),
        ]
        regs = [{"host": "pool:a100", "state": "pool", "queues": [
            {"root": "/data/lab/pa", "jobs": "jobs/big1.txt", "project": "pa"},
            {"root": "/data/lab/pb", "jobs": "jobs/big2.txt", "project": "pb"},
        ]}]
        moves = []

        def fake_fit(pcfg, item, eval_hosts):
            # h1 放得下，h2 显存太小放不下
            res = {}
            for h in eval_hosts:
                if h == "h1":
                    res[h] = {"ok": True, "nofit": False, "load": {"running": 0, "queued": 0, "until": None}, "free_cards": 1, "free_vram": 80000}
                else:
                    res[h] = {"ok": False, "nofit": True, "why": "单卡显存不够（要 60 GiB）"}
            return {"head": "x", "need": {"ngpu": 1}, "dur": 60, "hosts": res}

        out = P.tick(hosts, regs, self.sw, self.t, lan_ok=True,
                     move_fn=lambda to, it, pid, rsn: moves.append((to, it["project"])),
                     fit_fn=fake_fit)
        # 两条清单里第一条落到 h1；第二条只评估了 h2 放不下，不得被标为全池 nofit=True
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0][0], "h1")
        remain = list(out["lists"].values())
        self.assertEqual(len(remain), 1)
        self.assertFalse(remain[0]["nofit"], remain[0])
        self.assertIn("下一轮", remain[0]["why"])

    def test_fit_read_failure_falls_back_and_not_marked_done(self):
        """Codex P2：_default_fit 首台读失败返回非 0 时自动回退下一台健康机；全员读失败不标 done=True 且保留 wait_since。"""
        pcfg = CF.resolve_pool("pool:a100")
        item = {"root": "/data/lab/pa", "jobs": "jobs/q.txt", "project": "pa"}
        tried = []

        def fake_run_out(cmd, via, args, **kw):
            tried.append(via)
            if via == "h1":
                return (2, "读不到清单 /data/lab/pa/jobs/q.txt\n")
            return (0, '{"head": "j1", "need": {"ngpu": 1}, "dur": 30, "hosts": {"h2": {"ok": true}}}\n')

        with mock.patch.object(W, "run_out", side_effect=fake_run_out):
            got = P._default_fit(pcfg, item, ["h1", "h2"])
        self.assertEqual(tried, ["h1", "h2"])
        self.assertIsNotNone(got)
        self.assertEqual(got["head"], "j1")

        # 若所有候选机拟合均返回 None（读失败），tick 绝不误标 done=True，且保留 wait_since
        k = "pool:a100|/data/lab/pa|jobs/q.txt"
        heads = {k: {"pool": "pool:a100", "pool_id": "a100", "head": "j1", "n": 1}}
        hosts = [make_host_entry("h1", gpus=1, busy_gpus=0, pool_heads=heads, t=self.t)]
        regs = [{"host": "pool:a100", "state": "pool", "queues": [{"root": "/data/lab/pa", "jobs": "jobs/q.txt", "project": "pa"}]}]
        out = P.tick(hosts, regs, self.sw, self.t, lan_ok=True, move_fn=lambda *a: None, fit_fn=lambda *a: None)
        lk = P.key("/data/lab/pa", "jobs/q.txt", "a100")
        self.assertFalse(out["lists"][lk].get("done"))
        self.assertIn(lk, P.load_json(P.state_file(), {}).get("wait_since", {}))

    def test_cross_domain_same_path_lists_isolated(self):
        """Codex P2：两个不同 worker_home 的共有池存在同名 ROOT/JOBS 时，状态键带池 id，互不覆盖 lists/wait_since。"""
        (self.d / "hosts.toml").write_text("""
[[hosts]]
id = "h1"
ssh = "u@10.0.0.1"
worker_home = "/data/cluster_a/.labmon"

[[hosts]]
id = "h2"
ssh = "u@10.0.0.2"
worker_home = "/data/cluster_b/.labmon"

[pools.pa_pool]
label = "A 域池"
hosts = ["h1"]
members = ["alice"]

[pools.pb_pool]
label = "B 域池"
hosts = ["h2"]
members = ["bob"]
""")
        k1 = "pool:pa_pool|/projects/demo|jobs.txt"
        k2 = "pool:pb_pool|/projects/demo|jobs.txt"
        hosts = [
            make_host_entry("h1", gpus=1, busy_gpus=0, pool_heads={k1: {"pool": "pool:pa_pool", "pool_id": "pa_pool", "head": "j_a", "n": 1}}, t=self.t),
            make_host_entry("h2", gpus=1, busy_gpus=0, pool_heads={k2: {"pool": "pool:pb_pool", "pool_id": "pb_pool", "head": "j_b", "n": 1}}, t=self.t),
        ]
        regs = [
            {"host": "pool:pa_pool", "state": "pool", "queues": [{"root": "/projects/demo", "jobs": "jobs.txt", "project": "pa"}]},
            {"host": "pool:pb_pool", "state": "pool", "queues": [{"root": "/projects/demo", "jobs": "jobs.txt", "project": "pb"}]},
        ]
        out = P.tick(hosts, regs, self.sw, self.t, lan_ok=True, use_ssh=False)
        lk1 = P.key("/projects/demo", "jobs.txt", "pa_pool")
        lk2 = P.key("/projects/demo", "jobs.txt", "pb_pool")
        self.assertIn(lk1, out["lists"])
        self.assertIn(lk2, out["lists"])
        self.assertEqual(out["lists"][lk1]["pool"], "pa_pool")
        self.assertEqual(out["lists"][lk2]["pool"], "pb_pool")
        self.assertEqual(out["lists"][lk1]["head"], "j_a")
        self.assertEqual(out["lists"][lk2]["head"], "j_b")

    def test_api_fit_fallback_and_reg_move_pool_alias(self):
        """Codex P2：① worker fit pool:<id> --list 首台不可达时自动回退下一台可用池主机；② reg_move('pool', ...) 规范化为实际池入口 pool:a100。"""
        tried = []

        def fake_fit(cmd, via, args, *p, **kw):
            tried.append((cmd, via, kw.get("hosts")))
            if via == "h1":
                return (255, "ssh: connect failed\n")
            return (0, "ok from " + via + "\n")

        with mock.patch.object(W, "run_out", side_effect=fake_fit):
            r = A.worker("fit", "pool:a100", ["/data/lab/pa", "jobs/q.txt"], project="pa", lst=True)
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual([v for _, v, _ in tried][:2], ["h1", "h2"])

        moved_to = []
        with mock.patch.object(W, "run_out", side_effect=lambda cmd, to, *a, **k: moved_to.append(to) or (0, "已登记")):
            S.reg_move("pool", [{"host": "h1", "root": "/data/lab/pa", "jobs": "jobs/q.txt", "project": "pa"}])
        self.assertEqual(moved_to, ["pool:a100"])

    def test_host_view_deadline_single_host_nofit_and_via_fallback(self):
        """Codex 第四轮 P2：① _host_view 投影 deadline 与 lease_end；② 单健康机池 orig_nofit 退回待放；③ workerctl.run_out 对池入口 SSH 255 自动切下一台可达同域代办主机。"""
        # ① _host_view 传递 deadline 与 lease_end
        h_dl = make_host_entry("h1", gpus=1, busy_gpus=0,
                               overrides=[{"kind": "deadline", "target": "renew", "until": self.t + 7200}], t=self.t)
        h_dl["lease_end"] = self.t + 86400
        out0 = P.tick([h_dl], [], self.sw, self.t, lan_ok=True, use_ssh=False)
        hv = next(h for h in out0["pools"][0]["hosts"] if h["id"] == "h1")
        self.assertTrue(hv["deadline"])
        self.assertEqual(hv["lease_end"], self.t + 86400)

        # ② 单健康机池原机已 nofit：退回待放以便下一轮标 nofit=True 并告警
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools="""
[pools.a100]
label = "单机池"
hosts = ["h1"]
members = ["alice"]
"""))
        h1 = make_host_entry("h1", gpus=1, busy_gpus=0,
                             waiting=[{"name": "big1", "project": "pa", "root": "/data/lab/pa", "queue": "jobs/big.txt",
                                       "since": self.t - 60, "why": "单卡显存放不下"}], t=self.t)
        regs = [{"host": "h1", "state": "active", "queues": [
            {"root": "/data/lab/pa", "jobs": "jobs/big.txt", "project": "pa", "pool": "a100"},
        ]}]
        moves = []
        fit_nofit = lambda pcfg, it, hs: {"head": "big1", "need": {"ngpu": 1}, "hosts": {h: {"ok": False, "nofit": True, "why": "显存放不下"} for h in hs}}
        P.tick([h1], regs, self.sw, self.t, lan_ok=True,
               move_fn=lambda to, it, pid, rsn: moves.append((to, it["src"])), fit_fn=fit_nofit)
        self.assertEqual(moves, [("pool:a100", "h1")])

        # ③ workerctl.run_out("add", "pool:a100", ...) 首台 h1 SSH 255 时自动回退到同域可达主机 h2
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools="""
[pools.a100]
label = "双机池"
hosts = ["h1", "h2"]
members = ["alice"]
"""))
        tried = []

        def fake_ssh(hc, cmd, *a, **k):
            tried.append(hc["id"])
            return (255, "ssh: connect failed") if hc["id"] == "h1" else (0, "已登记")

        with mock.patch.object(W, "ssh", side_effect=fake_ssh):
            rc, out = W.run_out("add", "pool:a100", ["/data/lab/pa", "jobs/q.txt"], project="pa")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tried[:2], ["h1", "h2"])

    def test_overview_regs_fallback_when_first_host_down(self):
        """Codex 第五轮 P2：同共享域首台 h1 失联时，overview.collect 当轮立即回退到可达的 h2 补读登记；下轮直接选 h2 读取。"""
        from labmon import overview as OV
        (self.d / "hosts.toml").write_text(SHARED_HOSTS.format(pools="""
[pools.a100]
label = "双机池"
hosts = ["h1", "h2"]
members = ["alice"]
"""))
        import time
        now = time.time()
        k = "pool:a100|/data/lab/pa|jobs/q.txt"
        st_h2 = make_host_entry("h2", gpus=1, busy_gpus=0,
                                share={"wait_cap_min": 180, "half_life_h": 24, "use": {}, "weight": {}},
                                pool_heads={k: {"pool": "pool:a100", "pool_id": "a100", "head": "j1", "n": 1}}, t=now)["status"]
        reg_line = f"{OV.REG} pool:a100 q1.json {int(now)}\n" + json.dumps({"root": "/data/lab/pa", "jobs": "jobs/q.txt", "project": "pa"})
        calls = []

        def fake_fetch(h):
            calls.append((h["id"], h.get("regs") is not None))
            if h["id"] == "h1":
                return (None, "ssh: connect failed")
            reg_part = reg_line if h.get("regs") is not None else ""
            raw = ("\n" + OV.SEP + "\n").join([json.dumps(st_h2), "", "", str(int(now)), "", "", "", reg_part, ""])
            return (raw, None)

        with mock.patch.object(OV, "fetch", side_effect=fake_fetch), \
                mock.patch.object(P, "_default_fit", return_value=None), \
                mock.patch.object(LS, "decide", return_value="primary"), \
                mock.patch.object(LS, "primary", return_value=True), \
                mock.patch.object(OV.L, "gather", side_effect=lambda items, fn, *a: ({"ok": True, "since": None}, {k: fn(v) for k, v in items.items()})):
            snap1 = OV.collect(use_ssh=True)
            self.assertIsNotNone(snap1)
            pool_reg = next((r for r in snap1["regs"] if r["host"] == "pool:a100"), None)
            self.assertIsNotNone(pool_reg, snap1["regs"])
            self.assertEqual(len(pool_reg["queues"]), 1)
            self.assertEqual(snap1["pool"]["pools"][0]["pending"], 1)
            # 第二轮：old_hosts 已知 h1 失败，直接选 h2 读 regs，不再补采
            calls.clear()
            snap2 = OV.collect(use_ssh=True)
            self.assertIsNotNone(snap2)
            self.assertEqual([c for c in calls if c[1]], [("h2", True)])


class PoolInfraTest(Base):
    def test_locate_and_worker_rm(self):
        """池待放与带 pool 标记的登记可被 pool.locate 定位（含命名池）；普通主机登记不被误认。"""
        snap = {"regs": [
            {"host": "pool", "state": "pool", "queues": [{"root": "/r/x", "jobs": "jobs/x.txt", "project": "p"}]},
            {"host": "pool:a100", "state": "pool", "queues": [{"root": "/r/m", "jobs": "jobs/m.txt", "project": "p"}]},
            {"host": "here", "state": "active", "queues": [
                {"root": "/r/y", "jobs": "jobs/y.txt", "project": "p", "pool": "pool"},
                {"root": "/r/n", "jobs": "jobs/n.txt", "project": "p", "pool": "a100"},
                {"root": "/r/z", "jobs": "jobs/z.txt", "project": "p"},
            ]},
        ]}
        self.assertEqual(P.locate("/r/x/", "jobs/x.txt", snap), "pool")
        self.assertEqual(P.locate("/r/y", "jobs/y.txt", snap), "here")
        self.assertEqual(P.locate("/r/m", "jobs/m.txt", snap, pid="a100"), "pool:a100")
        self.assertEqual(P.locate("/r/n", "jobs/n.txt", snap, pid="a100"), "here")
        self.assertEqual(P.locate("/r/z", "jobs/z.txt", snap), "pool")
        self.assertEqual(P.locate("/r/none", "jobs/none.txt", snap), "pool")
        self.assertEqual(W.via("pool")["id"], "here")
        got = []
        with mock.patch.object(W, "run_out", side_effect=lambda *a, **k: got.append(a) or (0, "ok\n")), \
                mock.patch.object(P, "locate", return_value="here"), \
                mock.patch.object(A, "resolve_pool", return_value={"id": "pool", "host_id": "pool", "label": "算力池", "hosts": ["here"], "active_hosts": ["here"]}):
            r = A.worker("rm", "pool", ["/r/y", "jobs/y.txt"])
        self.assertEqual(r["rc"], 0)
        self.assertEqual(got[0][:2], ("rm", "here"))


class PoolGuiManagementTest(Base):
    """Issue #240：管理员 GUI 可视化新建/编辑/删除共有型算力池，含原子写入、强校验与安全红线。"""

    def setUp(self):
        super().setUp()
        self.hosts_toml = """# 保留顶部注释
[[hosts]]
id = "h1"
ssh = "root@10.0.0.1"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
label = "A100-1"

[[hosts]]
id = "h2"
ssh = "root@10.0.0.2"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
label = "A100-2"

[[hosts]]
id = "h3"
ssh = "root@10.0.0.3"
data_root = "/data/other"
worker_home = "/data/other/labmon"
label = "异域机"

[[hosts]]
id = "ind1"
ssh = "root@10.0.0.4"
standalone = true
label = "独立机"

[[hosts]]
id = "local1"
label = "看板本机"

[[hosts]]
id = "ret1"
ssh = "root@10.0.0.5"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
returned = "2026-11-20"
label = "已归还机"

[[hosts]]
id = "swp1"
ssh = "root@10.0.0.6"
data_root = "/data/lab"
worker_home = "/data/lab/labmon"
borrow_until = "2026-12-01 18:00"
borrow_from = "AA"
label = "借用机"

[fleet]
interval = 600

[[fleet.servers]]
id = "fsrv1"
holder = "AA"
ssh = "root@10.0.0.9"
port = 15001
pool = "P"
"""
        (self.d / "hosts.toml").write_text(self.hosts_toml)

    def test_save_edit_remove_pool_atomic_and_events(self):
        """save_pool / remove_pool 原子改写 hosts.toml，保留原注释与其他段，记录 config 事件。"""
        res = CF.save_pool("a100", label="A100 共有池", hosts=["h1", "h2"], members=["alice", "Bob"],
                           exclusive=True, create=True, by="boss")
        self.assertTrue(res["changed"])
        self.assertEqual(res["pool"]["host_id"], "pool:a100")
        self.assertEqual(res["pool"]["members"], ["alice", "bob"])
        text = (self.d / "hosts.toml").read_text()
        self.assertIn("# 保留顶部注释", text)
        self.assertIn("[pools.a100]", text)
        # 无变动编辑短路不重写
        res_same = CF.save_pool("a100", label="A100 共有池", hosts=["h1", "h2"], members=["alice", "bob"],
                                exclusive=True, create=False, by="boss")
        self.assertFalse(res_same["changed"])
        # 编辑改开放模式、通配成员、移出 h2（提示下一轮自动撤离）
        res2 = CF.save_pool("a100", label="A100 开放池", hosts=["h1"], members=["alice", "*"],
                            exclusive=False, create=False, by="boss")
        self.assertTrue(res2["changed"])
        self.assertFalse(res2["pool"]["exclusive"])
        self.assertEqual(res2["pool"]["members"], ["*"])
        self.assertEqual(res2["pool"]["hosts"], ["h1"])
        self.assertIn("h2", res2["msg"])
        self.assertIn("自动撤离", res2["msg"])
        # 支持默认池 [pools.pool] 与命名池共存
        res_def = CF.save_pool("pool", label="默认池", hosts=["h2"], members=[], exclusive=True, create=True, by="boss")
        self.assertEqual(res_def["pool"]["host_id"], "pool")
        self.assertEqual(set(CF.pools()), {"a100", "pool"})
        # 删除两个池
        msg1 = CF.remove_pool("a100", by="boss", snap={"regs": [], "pool": {"lists": {}}})
        self.assertIn("已删除", msg1)
        self.assertEqual(list(CF.pools()), ["pool"])
        CF.remove_pool("pool", by="boss", snap={"regs": [], "pool": {"lists": {}}})
        self.assertEqual(CF.pools(), {})
        self.assertIn("# 保留顶部注释", (self.d / "hosts.toml").read_text())
        evs = RC.records(cat="config")
        self.assertTrue(any("新建了共有算力池" in e["msg"] for e in evs), evs)
        self.assertTrue(any("删除了共有算力池" in e["msg"] for e in evs), evs)

    def test_save_pool_validation_rejects_invalid_inputs(self):
        """save_pool 复用 pools() 强校验并拦截非法 id、保留名、重名、异域、互斥、独立/借用/本机/归还机与未知账号。"""
        CF.save_pool("a100", label="已有池", hosts=["h1"], members=["alice"], create=True, by="boss")
        bad_cases = [
            # 非法 id / 保留名 / 重名 / 已存在 / 不存在
            (dict(pid="Bad!", hosts=["h2"], create=True), "不合法"),
            (dict(pid="new", hosts=["h2"], create=True), "保留名"),
            (dict(pid="edit", hosts=["h2"], create=True), "保留名"),
            (dict(pid="h1", hosts=["h2"], create=True), "重名"),
            (dict(pid="fsrv1", hosts=["h2"], create=True), "重名"),
            (dict(pid="a100", hosts=["h2"], create=True), "已存在"),
            (dict(pid="nope", hosts=["h2"], create=False), "找不到"),
            # 非法主机组合
            (dict(pid="p2", hosts=[], create=True), "至少选择"),
            (dict(pid="p2", hosts=["nohost"], create=True), "不在 [[hosts]]"),
            (dict(pid="p2", hosts=["h1"], create=True), "同时出现"),
            (dict(pid="p2", hosts=["h2", "h3"], create=True), "worker_home"),
            (dict(pid="p2", hosts=["ind1"], create=True), "独立主机"),
            (dict(pid="p2", hosts=["local1"], create=True), "本机或回环"),
            (dict(pid="p2", hosts=["swp1"], create=True), "借用"),
            (dict(pid="p2", hosts=["ret1"], create=True), "已归还"),
            (dict(pid="p2", hosts=["fsrv1"], create=True), "组内服务器"),
            # 成员账号不存在 / 名称超长
            (dict(pid="p2", hosts=["h2"], members=["ghost"], create=True), "账号不存在"),
            (dict(pid="p2", hosts=["h2"], label="x" * 41, create=True), "最多"),
        ]
        before = (self.d / "hosts.toml").read_text()
        for kw, expect in bad_cases:
            with self.assertRaises(CF.ConfigError, msg=str(kw)) as e:
                CF.save_pool(**kw, by="boss")
            self.assertIn(expect, str(e.exception))
        # 所有失败校验均未污染 hosts.toml
        self.assertEqual((self.d / "hosts.toml").read_text(), before)

    def test_remove_pool_guards_active_and_pending_lists(self):
        """有待放或已落机池清单时默认阻止删除并提示条数，force=True 可强制删除；编辑移出主机后下一轮自动撤离。"""
        CF.save_pool("a100", label="A100 池", hosts=["h1", "h2"], members=["alice"], create=True, by="boss")
        snap = {
            "pool": {"lists": {
                "a100|/data/lab/pa|jobs/q1.txt": {
                    "pool": "a100", "root": "/data/lab/pa", "jobs": "jobs/q1.txt", "done": False,
                },
            }},
            "regs": [
                {"host": "pool:a100", "state": "pool", "queues": [
                    {"root": "/data/lab/pa", "jobs": "jobs/q1.txt", "project": "pa"},
                ]},
                {"host": "h2", "state": "active", "queues": [
                    {"root": "/data/lab/pa", "jobs": "jobs/q2.txt", "project": "pa", "pool": "a100"},
                ]},
            ],
        }
        with self.assertRaises(CF.ConfigError) as e:
            CF.remove_pool("a100", force=False, by="boss", snap=snap)
        self.assertIn("1 条待放清单", str(e.exception))
        self.assertIn("1 条已落机清单", str(e.exception))

        # 编辑态移出 h2 后，pool.tick 自动撤离 h2 上已落机的本池清单
        CF.save_pool("a100", label="A100 池", hosts=["h1"], members=["alice"], create=False, by="boss")
        t = 1_700_000_000.0
        h1 = make_host_entry("h1", gpus=1, busy_gpus=0, t=t)
        h2 = make_host_entry("h2", gpus=1, busy_gpus=0,
                             waiting=[{"name": "j2", "project": "pa", "root": "/data/lab/pa", "queue": "jobs/q2.txt", "since": t - 60}], t=t)
        moved = []
        fit_ok = lambda pcfg, it, hs: {"head": "j2", "need": {"ngpu": 1}, "hosts": {h: {"ok": True, "free_cards": 1, "free_vram": 40000, "load": {}} for h in hs}}
        with mock.patch.object(LS, "primary", return_value=True):
            P.tick([h1, h2], snap["regs"], {}, t, lan_ok=True,
                   move_fn=lambda to, it, pid, rsn: moved.append((to, it["src"], rsn)), fit_fn=fit_ok)
        self.assertTrue(any(src == "h2" and to == "h1" and "移出算力池" in rsn for to, src, rsn in moved), moved)

        # force=True 强制删除成功
        msg = CF.remove_pool("a100", force=True, by="boss", snap=snap)
        self.assertIn("已删除", msg)
        self.assertIn("不受中断", msg)
        self.assertEqual(CF.pools(), {})

    def test_manage_data_structure(self):
        """pool.manage_data 准确返回候选主机（含 worker_home / pool_id / 不可入池原因）与普通用户列表（不含管理员）。"""
        CF.save_pool("a100", label="A100 池", hosts=["h1"], members=["alice"], create=True, by="boss")
        data = P.manage_data({"regs": [], "pool": {"lists": {}}})
        by_id = {h["id"]: h for h in data["hosts"]}
        self.assertTrue(by_id["h1"]["eligible"])
        self.assertEqual(by_id["h1"]["pool_id"], "a100")
        self.assertTrue(by_id["h2"]["eligible"])
        self.assertIsNone(by_id["h2"]["pool_id"])
        self.assertEqual(by_id["h3"]["worker_home"], "/data/other/labmon")
        self.assertFalse(by_id["ind1"]["eligible"])
        self.assertIn("独立主机", by_id["ind1"]["why"])
        self.assertFalse(by_id["local1"]["eligible"])
        self.assertFalse(by_id["ret1"]["eligible"])
        self.assertFalse(by_id["swp1"]["eligible"])
        self.assertNotIn("fsrv1", by_id)
        unames = [u["name"] for u in data["users"]]
        self.assertIn("alice", unames)
        self.assertNotIn("boss", unames)

    def test_http_routes_security_lan_user_standby(self):
        """HTTP 接口安全红线：内网管理员读写正常并唤醒采集；普通用户 403；公网（含开启 public_ops 管理员）一律 404；备用机写操作 409。"""
        import threading
        import urllib.error
        import urllib.request
        from fixtures import board

        srv = board(self, scopes=("admin",), name="boss", role="admin")
        pub = S.Listener(("127.0.0.1", 0), srv)
        threading.Thread(target=pub.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(pub.server_close)
        self.addCleanup(pub.shutdown)

        # 开启 boss 的公网平台操作，验证池管理仍严格 lan=True 绝不随 public_ops 开放
        U.set_public_ops("boss", True, "test")
        admin_tok = make_token("boss", role="admin", scopes=("admin",))
        user_tok = make_token("alice", role="user", scopes=("read", "project"))
        lan_base = f"http://127.0.0.1:{srv.server_address[1]}"
        pub_base = f"http://127.0.0.1:{pub.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def call(base: str, method: str, path: str, tok: str, body: dict | None = None):
            data = json.dumps(body).encode() if body is not None else None
            hdr = {"Authorization": f"Bearer {tok}"}
            if method == "POST":
                hdr["Content-Type"] = "application/json"
            req = urllib.request.Request(base + path, data=data, method=method, headers=hdr)
            try:
                with opener.open(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())

        with mock.patch.object(LS, "primary", return_value=True):
            # 1. 内网管理员读取 options、新建、编辑、删除
            st, data = call(lan_base, "GET", "/api/pools/options", admin_tok)
            self.assertEqual(st, 200, data)
            self.assertTrue(any(h["id"] == "h1" for h in data["hosts"]))
            self.assertTrue(any(u["name"] == "alice" for u in data["users"]))

            srv.overview.wake.clear()
            st, out = call(lan_base, "POST", "/api/pools/save", admin_tok,
                           {"id": "a100", "label": "A100 池", "hosts": ["h1", "h2"], "members": ["alice"], "exclusive": True, "create": True})
            self.assertEqual(st, 200, out)
            self.assertTrue(srv.overview.wake.is_set())
            self.assertIn("a100", CF.pools())

            # 2. 普通用户一律 403
            for method, path, body in (
                ("GET", "/api/pools/options", None),
                ("POST", "/api/pools/save", {"id": "p2", "hosts": ["h3"], "members": [], "create": True}),
                ("POST", "/api/pools/remove", {"id": "a100"}),
            ):
                st, err = call(lan_base, method, path, user_tok, body)
                self.assertEqual(st, 403, f"普通用户应 403：{method} {path} → {st} {err}")

            # 3. 公网入口（即使管理员开启了 public_ops）一律 404
            for method, path, body in (
                ("GET", "/api/pools/options", None),
                ("POST", "/api/pools/save", {"id": "p2", "hosts": ["h3"], "members": [], "create": True}),
                ("POST", "/api/pools/remove", {"id": "a100"}),
            ):
                st, err = call(pub_base, method, path, admin_tok, body)
                self.assertEqual(st, 404, f"公网入口应 404：{method} {path} → {st} {err}")

            # 4. 校验失败返回 409
            st, err = call(lan_base, "POST", "/api/pools/save", admin_tok,
                           {"id": "bad_mix", "hosts": ["h1", "h3"], "members": [], "create": True})
            self.assertEqual(st, 409, err)

            # 5. 内网管理员正常删除
            srv.overview.wake.clear()
            st, out = call(lan_base, "POST", "/api/pools/remove", admin_tok, {"id": "a100"})
            self.assertEqual(st, 200, out)
            self.assertTrue(srv.overview.wake.is_set())
            self.assertEqual(CF.pools(), {})

        # 6. 备用机拦截写操作返回 409（含 standby=True）
        with mock.patch.object(LS, "primary", return_value=False):
            st, err = call(lan_base, "POST", "/api/pools/save", admin_tok,
                           {"id": "sb", "hosts": ["h1"], "members": [], "create": True})
            self.assertEqual(st, 409, err)
            self.assertTrue(err.get("standby"))
            st, err = call(lan_base, "POST", "/api/pools/remove", admin_tok, {"id": "sb"})
            self.assertEqual(st, 409, err)
            self.assertTrue(err.get("standby"))

    def test_save_pool_blocks_domain_switch_with_active_lists(self):
        """Codex P2-1：池仍有待放或已落机清单时，拒绝编辑切换到另一 worker_home 共享域；清单清空后才允许切换。"""
        CF.save_pool("a100", label="A100 池", hosts=["h1"], members=["alice"], create=True, by="boss")
        before = (self.d / "hosts.toml").read_text()
        snap_pend = {
            "pool": {"lists": {"a100|/data/lab/pa|jobs/q1.txt": {"pool": "a100", "root": "/data/lab/pa", "jobs": "jobs/q1.txt", "done": False}}},
            "regs": [{"host": "pool:a100", "state": "pool", "home": "/data/lab/labmon", "queues": [{"root": "/data/lab/pa", "jobs": "jobs/q1.txt", "project": "pa"}]}],
        }
        snap_placed = {
            "pool": {"lists": {}},
            "regs": [{"host": "h1", "state": "active", "home": "/data/lab/labmon", "queues": [{"root": "/data/lab/pa", "jobs": "jobs/q2.txt", "project": "pa", "pool": "a100"}]}],
        }
        for label, sn in (("待放", snap_pend), ("已落机", snap_placed)):
            with self.assertRaises(CF.ConfigError, msg=label) as e:
                CF.save_pool("a100", label="A100 池", hosts=["h3"], members=["alice"], create=False, by="boss", snap=sn)
            self.assertIn("共享域", str(e.exception))
            self.assertEqual((self.d / "hosts.toml").read_text(), before)
        # 清单清空后允许切换到新共享域
        res = CF.save_pool("a100", label="A100 池", hosts=["h3"], members=["alice"], create=False, by="boss",
                           snap={"regs": [], "pool": {"lists": {}}})
        self.assertTrue(res["changed"])
        self.assertEqual(res["pool"]["worker_home"], "/data/other/labmon")

    def test_pool_activity_excludes_done_pending_regs(self):
        """Codex 第 2 轮 P2：pool.lists 已确认 done=True 的待放登记（regs 文件本身无 empty/sleeping 字段）不计入待放数，不阻断删池或切域。"""
        CF.save_pool("a100", label="A100 池", hosts=["h1"], members=["alice"], create=True, by="boss")
        snap_done = {
            "pool": {"lists": {
                "a100|/data/lab/pa|jobs/done.txt": {
                    "pool": "a100", "root": "/data/lab/pa", "jobs": "jobs/done.txt", "done": True, "sleeping": False,
                },
            }},
            "regs": [
                {"host": "pool:a100", "state": "pool", "home": "/data/lab/labmon", "queues": [
                    {"root": "/data/lab/pa", "jobs": "jobs/done.txt", "project": "pa"},
                ]},
            ],
        }
        self.assertEqual(P.pool_activity("a100", snap=snap_done), {"pending": 0, "placed": 0})
        # 允许无阻碍切共享域与非强制删除
        res = CF.save_pool("a100", label="A100 池", hosts=["h3"], members=["alice"], create=False, by="boss", snap=snap_done)
        self.assertEqual(res["pool"]["worker_home"], "/data/other/labmon")
        # 尚未被 worker 读到的新登记（不在 pool.lists 中）仍保守计入待放
        snap_unread = {
            "pool": {"lists": snap_done["pool"]["lists"]},
            "regs": [
                {"host": "pool:a100", "state": "pool", "home": "/data/other/labmon", "queues": [
                    {"root": "/data/lab/pa", "jobs": "jobs/done.txt", "project": "pa"},
                    {"root": "/data/other/pa", "jobs": "jobs/new.txt", "project": "pa"},
                ]},
            ],
        }
        self.assertEqual(P.pool_activity("a100", snap=snap_unread), {"pending": 1, "placed": 0})
        # done-only 快照允许非强制删除
        msg = CF.remove_pool("a100", force=False, by="boss", snap=snap_done)
        self.assertIn("已删除", msg)

    def test_worker_rm_deleted_named_pool_pending_list(self):
        """Codex P2-2：强制删除命名池后，仍可经 worker rm pool:<id> 按快照 home 精准撤回待放清单（含普通用户撤自己项目），而未知池名仍报错。"""
        import urllib.error
        import urllib.request
        from fixtures import board
        from labmon import projects as PJ
        from labmon.state import save_json, state_dir

        CF.save_pool("p1", label="默认域池", hosts=["h1"], members=["alice"], create=True, by="boss")
        CF.save_pool("a100", label="他域池", hosts=["h3"], members=["alice"], create=True, by="boss")
        PJ.put_config("pa", 'id = "pa"\n', owner="alice")
        snap = {
            "hosts": [make_host_entry("h1", gpus=1, busy_gpus=0), make_host_entry("h3", gpus=1, busy_gpus=0)],
            "pool": {"lists": {"a100|/data/other/pa|jobs/q1.txt": {"pool": "a100", "root": "/data/other/pa", "jobs": "jobs/q1.txt", "done": False}}},
            "regs": [
                {"host": "pool:a100", "state": "pool", "home": "/data/other/labmon", "queues": [
                    {"root": "/data/other/pa", "jobs": "jobs/q1.txt", "project": "pa"},
                ]},
            ],
        }
        save_json(state_dir() / "snapshot.json", snap)
        msg = CF.remove_pool("a100", force=True, by="boss", snap=snap)
        self.assertIn("labmon worker rm pool:a100", msg)
        self.assertNotIn("a100", CF.pools())

        # ① CLI / api.worker 撤回已删命名池的待放清单：必须携带 /data/other/labmon 并通过 h3 执行，而不是误连 h1
        seen_ssh = []
        with mock.patch.object(LS, "primary", return_value=True), \
                mock.patch.object(W, "ssh", side_effect=lambda h, cmd, *a, **k: seen_ssh.append((h["id"], h["worker_home"], cmd)) or (0, "已移除\n")):
            r = A.worker("rm", "pool:a100", ["/data/other/pa", "jobs/q1.txt"])
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual(seen_ssh[0][0], "h3")
        self.assertEqual(seen_ssh[0][1], "/data/other/labmon")
        self.assertIn("--host pool:a100", seen_ssh[0][2])

        # ② 乱写从未存在且快照无残留的命名池仍报找不到算力池，不误连别的共享域
        r_bad = A.worker("rm", "pool:ghost", ["/data/other/pa", "jobs/q1.txt"])
        self.assertEqual(r_bad["rc"], 3)
        self.assertIn("找不到算力池", r_bad["err"])

        # ③ 普通用户经 HTTP API 撤回已删命名池里自己项目的待放清单：按 need_reg 核准放行
        srv = board(self, scopes=("admin",), name="boss", role="admin")
        save_json(state_dir() / "snapshot.json", snap)
        user_tok = make_token("alice", role="user", scopes=("read", "project"))
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            base + "/api/worker/rm",
            data=json.dumps({"host": "pool:a100", "args": ["/data/other/pa", "jobs/q1.txt"]}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {user_tok}", "Content-Type": "application/json"},
        )
        seen_ssh.clear()
        with mock.patch.object(LS, "primary", return_value=True), \
                mock.patch.object(W, "ssh", side_effect=lambda h, cmd, *a, **k: seen_ssh.append((h["id"], cmd)) or (0, "已移除\n")):
            with opener.open(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode())
        self.assertEqual(body["rc"], 0, body)
        self.assertEqual(seen_ssh[0][0], "h3")

    def test_project_snapshot_host_pool_badge_and_isolation(self):
        """项目快照 hosts[] 透传 pool_id/pool_label（#251）：管理员与池成员可见，非池成员不泄露池名，未入池/临时/已归还主机不设。"""
        from labmon import collect as CL
        from labmon import projects as PJ
        from labmon.state import save_json

        CF.save_pool("a100", label="A100 共有池", hosts=["h1"], members=["alice"], create=True, by="boss")
        for pid, owner in (("p-admin", "boss"), ("p-alice", "alice"), ("p-bob", "bob"), ("p-anon", None)):
            f = PJ.cfg_file(pid)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f'[project]\nid = "{pid}"\n')
            if owner:
                save_json(f.parent / "meta.json", {"owner": owner})

        def run_hosts(pid: str) -> dict[str, dict]:
            cfg = {
                "id": pid,
                "hosts": [
                    {"id": "h1", "label": "h1（A100）", "ssh": None, "returned": None, "mounts": [], "checks": []},
                    {"id": "h2", "label": "h2（未入池）", "ssh": None, "returned": None, "mounts": [], "checks": []},
                    {"id": "eph1", "label": "colab-t4", "ephemeral": True, "returned": None, "mounts": [], "checks": []},
                    {"id": "ret1", "label": "已归还", "ssh": None, "returned": 1700000000, "mounts": [], "checks": []},
                ],
                "queues": [],
            }
            probes = {
                "h1": ({"t": 1700001000, "gpu": [], "cgroup": {}, "mounts": {}, "checks": {}}, None),
                "h2": ({"t": 1700001000, "gpu": [], "cgroup": {}, "mounts": {}, "checks": {}}, None),
                "eph1": ({"online": True, "instance_id": "inst-1", "gpu": [], "cgroup": {}, "storage": []}, None),
                "ret1": (None, None),
            }
            snap: dict = {"hosts": [], "alerts": []}
            sd = Path(self.tmp.name) / f"sd_{pid}"
            sd.mkdir(parents=True, exist_ok=True)
            CL._hosts(cfg, probes, False, {"hosts": []}, snap, {}, [], sd, 1700001000.0)
            return {h["id"]: h for h in snap["hosts"]}

        # 管理员名下项目、无主项目与池成员 alice 项目均透传 pool_id / pool_label
        for pid in ("p-admin", "p-anon", "p-alice"):
            by = run_hosts(pid)
            self.assertEqual(by["h1"].get("pool_id"), "a100", pid)
            self.assertEqual(by["h1"].get("pool_label"), "A100 共有池", pid)
            for other in ("h2", "eph1", "ret1"):
                self.assertNotIn("pool_id", by[other], f"{pid}/{other}")
                self.assertNotIn("pool_label", by[other], f"{pid}/{other}")

        # 非池成员 bob 的项目不泄露池名
        by_bob = run_hosts("p-bob")
        self.assertNotIn("pool_id", by_bob["h1"])
        self.assertNotIn("pool_label", by_bob["h1"])


if __name__ == "__main__":
    unittest.main()
