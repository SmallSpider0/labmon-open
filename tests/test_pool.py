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
        self.assertIn("等 prep 跑完", res["lists"]["/data/lab/pa|jobs/dep.txt"]["why"])
        self.assertIn("h2", res["lists"]["/data/lab/pa|jobs/ew.txt"]["why"])
        big = res["lists"]["/data/lab/pa|jobs/big.txt"]
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


if __name__ == "__main__":
    unittest.main()
