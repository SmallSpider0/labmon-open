"""Issue #114 阶段 1（PR 1）离线测试：
- 主机分类（platform_kind=scm|other|none、share=pool|borrow|private、lease_until、data_root/local_dir/水位缺省值）与旧配置兼容；
- config.home_key 域隔离（多台独立机默认 worker_home 撞名隔离、home_host/home_peers/_guest_tpl、lease.homes、overview.sched_view、
  overview.check_project_idle、serve.reg_move、disk._df/_ours、pool._self_bar、borrow.begin、renew.check、overview.health）；
- workerctl.worker_env 透传与 project_init 独立机骨架、worker add 路径边界防污染校验；
- 多用户零私钥上传 SSH 密钥隔离底座（LABMON_SECRETS/ssh_keys 0700/0600、生成/轮换/删除、lan.ssh_argv/host_ssh_argv 强制 IdentitiesOnly=yes 与按机 ControlPath/known_hosts、
  私钥绝不进冷备包/api.hosts/日志）。
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import fake_host, fake_status, make_token  # noqa: E402
from labmon import api as A  # noqa: E402
from labmon import backup as BK  # noqa: E402
from labmon import borrow as B  # noqa: E402
from labmon import config as C  # noqa: E402
from labmon import disk as DK  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import health as H  # noqa: E402
from labmon import lan as L  # noqa: E402
from labmon import lease as LS  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import platform as P  # noqa: E402
from labmon import renew as R  # noqa: E402
from labmon import serve as SV  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon import worker as WMOD  # noqa: E402
from labmon import workerctl as W  # noqa: E402


class HostClassificationTest(unittest.TestCase):
    def test_defaults_and_backward_compat(self):
        # 1) 旧 SCM 集群主机零改写
        scm = C.host_defaults({"id": "29", "ssh": "root@198.51.100.29", "platform_job": "ALICE"})
        self.assertEqual((scm["platform_kind"], scm["share"], scm["standalone"], scm["ssh_key"]), ("scm", "pool", False, "system"))
        self.assertEqual((scm["data_root"], scm["worker_home"]), ("/data/labmon", "/data/labmon/labmon"))
        self.assertNotIn("local_dir", scm)

        # 2) 旧集群测试主机（无 platform_job 也无 shared，但未标 standalone/platform_kind）：保持非独立兼容旧用法
        legacy = C.host_defaults({"id": "a", "data_root": "/tmp/alice"})
        self.assertEqual((legacy["platform_kind"], legacy["standalone"], legacy["share"]), ("none", False, "private"))
        self.assertEqual(legacy["worker_home"], "/tmp/alice/labmon")

        # 3) 外平台租用主机：写 platform_kind="other" 或写 lease_until 自动判为独立主机、默认 private 与独立工作根
        other = C.host_defaults({"id": "rent1", "ssh": "u@203.0.113.10", "lease_until": "2026-12-20 18:00"})
        self.assertEqual((other["platform_kind"], other["share"], other["standalone"]), ("other", "private", True))
        self.assertEqual(other["data_root"], C.STANDALONE_DATA_ROOT)
        self.assertEqual(other["worker_home"], C.STANDALONE_DATA_ROOT + "/labmon")
        self.assertEqual(other["local_dir"], C.STANDALONE_DATA_ROOT + "/.tmp")
        self.assertEqual((other["disk_warn_gb"], other["disk_stop_gb"]), (20, 10))

        # 4) 自有独立主机（Mac / WSL2）：显式 platform_kind="none"
        mac = C.host_defaults({"id": "mac1", "ssh": "", "platform_kind": "none"})
        self.assertEqual((mac["platform_kind"], mac["share"], mac["standalone"]), ("none", "private", True))
        self.assertEqual(mac["data_root"], C.STANDALONE_DATA_ROOT)

        # 5) 显式声明 shared 或 standalone=False 的外平台主机不算独立机
        shared_other = C.host_defaults({"id": "rent2", "ssh": "u@203.0.113.11", "platform_kind": "other", "shared": ["/mnt/nfs"]})
        self.assertFalse(shared_other["standalone"])
        self.assertEqual(shared_other["share"], "private")

        # 6) 外部机归属 owner 自动拥有主机访问权（非回环时）
        owned = C.host_defaults({"id": "u1", "ssh": "u@203.0.113.20", "owner": "Amy", "ssh_key": "managed"})
        self.assertTrue(owned["standalone"])
        self.assertEqual(C.host_users(owned), ["amy"])
        owned_local = C.host_defaults({"id": "u2", "ssh": "u@127.0.0.1", "owner": "Amy"})
        self.assertEqual(C.host_users(owned_local), [])

    def test_validation_errors(self):
        for bad in (
            {"id": "x", "platform_kind": "aws"},
            {"id": "x", "share": "public"},
            {"id": "x", "standalone": "yes"},
            {"id": "x", "ssh_key": "uploaded"},
            {"id": "x", "lease_until": "not-a-date"},
            {"id": "x", "disk_warn_gb": -1},
            {"id": "x", "disk_stop_gb": True},
            {"id": "x", "disk_quota_gb": 0},
            {"id": "x", "cpu_quota": -2},
            {"id": "x", "allow_outside_root": "true"},
            {"id": "x", "owner": "   "},
        ):
            with self.assertRaises(C.ConfigError, msg=str(bad)):
                C.host_defaults(bad)

    def test_shell_path(self):
        self.assertEqual(C.shell_path("~"), '"$HOME"')
        self.assertEqual(C.shell_path("~/.local/share/labmon/work"), '"$HOME"/.local/share/labmon/work')
        self.assertEqual(C.shell_path("~/my dir/$x"), '"$HOME"/\'my dir/$x\'')
        self.assertEqual(C.shell_path("/data/labmon"), "/data/labmon")


class DomainIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.hf = self.d / "hosts.toml"
        self.env = mock.patch.dict(os.environ, {
            "LABMON_HOSTS": str(self.hf),
            "LABMON_STATE": str(self.d / "state"),
            "LABMON_SECRETS": str(self.d / "secrets"),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_home_key_prevents_standalone_collision(self):
        # 两台独立机共用完全相同的默认 worker_home 字符串，与共享集群机严格隔离
        self.hf.write_text(
            '[[hosts]]\nid = "c1"\nssh = "root@198.51.100.1"\nplatform_job = "A"\ndata_root = "/data/labmon"\n\n'
            '[[hosts]]\nid = "c2"\nssh = "root@198.51.100.2"\nplatform_job = "B"\ndata_root = "/data/labmon"\n\n'
            '[[hosts]]\nid = "ext1"\nssh = "u@203.0.113.1"\nplatform_kind = "other"\ndata_root = "/data/labmon"\n\n'
            '[[hosts]]\nid = "ext2"\nssh = "u@203.0.113.2"\nplatform_kind = "none"\ndata_root = "/data/labmon"\n'
        )
        act = C.active_hosts()
        by = {h["id"]: h for h in act}
        self.assertEqual(C.home_key(by["c1"]), C.home_key(by["c2"]))
        self.assertNotEqual(C.home_key(by["ext1"]), C.home_key(by["ext2"]))
        self.assertNotEqual(C.home_key(by["ext1"]), C.home_key(by["c1"]))
        # home_peers：集群两台互认，两台独立机只认自己
        self.assertEqual(C.home_peers("c1", act), ["c1", "c2"])
        self.assertEqual(C.home_peers("ext1", act), ["ext1"])
        self.assertEqual(C.home_peers("ext2", act), ["ext2"])
        # 字符串 home_host 只匹配非独立主机，不误选独立机
        self.assertEqual(C.home_host("/data/labmon/labmon", [by["ext1"], by["ext2"]]), None)
        self.assertEqual(C.home_host("/data/labmon/labmon", [by["ext1"], by["c1"]])["id"], "c1")
        # _guest_tpl 跳过独立主机
        self.assertEqual(C._guest_tpl([by["ext1"], by["ext2"]]), {})
        self.assertEqual(C._guest_tpl([by["ext1"], by["c2"]])["id"], "c2")

    def test_lease_homes_excludes_standalone_when_shared_exists(self):
        c1 = C.host_defaults({"id": "c1", "ssh": "root@198.51.100.1", "platform_job": "A", "data_root": "/data/labmon"})
        e1 = C.host_defaults({"id": "e1", "ssh": "u@203.0.113.1", "platform_kind": "other"})
        e2 = C.host_defaults({"id": "e2", "ssh": "u@203.0.113.2", "platform_kind": "none"})
        # 有共享盘主机时，仅以共享盘仲裁
        self.assertEqual(list(LS.homes([c1, e1, e2])), ["/data/labmon/labmon"])
        # 全是独立机时，各自独立成键不撞名
        only = LS.homes([e1, e2])
        self.assertEqual(len(only), 2)
        self.assertEqual({g[0]["id"] for g in only.values()}, {"e1", "e2"})

    def test_collect_does_not_write_standalone_put_when_shared_lease_lost(self):
        """共享盘与独立机并存：旧主本机仍标 primary=True，但共享盘租约已被另一台接管时，
        collect 须先确认共享盘租约，绝不能在降为备用前先向独立主机下发 sched.json 等输入。"""
        wh_shared = self.d / "shared_wh"
        wh_ext = self.d / "ext_wh"
        (wh_shared / "hosts" / "c1").mkdir(parents=True)
        (wh_ext / "hosts" / "e1").mkdir(parents=True)
        self.hf.write_text(
            f'[[hosts]]\nid = "c1"\nplatform_job = "A"\nworker_home = "{wh_shared}"\npython = "{sys.executable}"\n\n'
            f'[[hosts]]\nid = "e1"\nplatform_kind = "none"\nworker_home = "{wh_ext}"\npython = "{sys.executable}"\n'
        )
        # 本机曾是主（primary=True），但共享盘租约当前在另一台活跃实例手里
        LS.set_marker(True, "测试原主")
        now = int(os.stat(wh_shared).st_mtime) + 10
        (wh_shared / LS.FILE).write_text(json.dumps({"holder": "other-primary-1", "renew": now, "ttl": LS.TTL}))
        O.reset_sent()
        res = O.collect([], use_ssh=False)
        self.assertIsNone(res)
        self.assertFalse(LS.primary())
        # 独立主机目录下不得被旧主写入任何输入文件
        self.assertFalse((wh_ext / "hosts" / "e1" / "sched.json").exists())
        self.assertFalse((wh_ext / "hosts" / "e1" / "eta.json").exists())
        self.assertFalse((wh_ext / "hosts" / "e1" / "returned.json").exists())

    def test_sched_view_prefers_cluster_over_newer_standalone(self):
        t = 1_790_000_000.0
        sh_cluster = {"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {}, "use": {"p1": 12.5, "p2": 3.0}, "run": []}
        sh_ext = {"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {}, "use": {"p1": 0.2}, "run": []}
        h_cluster = fake_host("c1", status=fake_status(t=t - 10, share=sh_cluster), standalone=False)
        # 独立主机时钟更新更晚（t + 5），不应覆盖全局调度卡
        h_ext = fake_host("e1", status=fake_status(t=t + 5, share=sh_ext), standalone=True)
        sv = O.sched_view([h_cluster, h_ext], [{"id": "p1"}, {"id": "p2"}], t, sw={}, people={"p1": "A", "p2": "B"})
        self.assertEqual(sv["use_sum"], 15.5)
        # 只有独立主机时回退使用独立主机
        sv_only = O.sched_view([h_ext], [{"id": "p1"}], t, sw={}, people={"p1": "A"})
        self.assertEqual(sv_only["use_sum"], 0.2)

    def test_reg_move_blocks_cross_domain_and_standalone(self):
        self.hf.write_text(
            '[[hosts]]\nid = "c1"\nssh = "root@198.51.100.1"\nplatform_job = "A"\ndata_root = "/data/a"\n\n'
            '[[hosts]]\nid = "c2"\nssh = "root@198.51.100.2"\nplatform_job = "B"\ndata_root = "/data/b"\n\n'
            '[[hosts]]\nid = "e1"\nssh = "u@203.0.113.1"\nplatform_kind = "other"\ndata_root = "/data/a"\n\n'
            '[[hosts]]\nid = "e2"\nssh = "u@203.0.113.2"\nplatform_kind = "none"\ndata_root = "/data/a"\n'
        )
        item = lambda frm: [{"host": frm, "root": "/data/a/projects/p1", "jobs": "q.txt", "project": "p1"}]  # noqa: E731
        with mock.patch.object(W, "run_out") as ro:
            for frm, to in (("e1", "e2"), ("e1", "c1"), ("c1", "e1"), ("c1", "c2")):
                with self.assertRaises(C.ConfigError, msg=f"{frm}->{to}"):
                    SV.reg_move(to, item(frm))
            ro.assert_not_called()

    def test_check_project_idle_returned_standalone_does_not_block_forever(self):
        self.hf.write_text(
            '[[hosts]]\nid = "ext1"\nssh = "u@203.0.113.1"\nplatform_kind = "other"\nreturned = "2026-11-20 12:00"\n'
        )
        cfg = {"hosts": [C.host_defaults({"id": "ext1", "ssh": "u@203.0.113.1", "platform_kind": "other", "returned": "2026-11-20 12:00"})]}
        # 已归还且最后快照无在跑任务与未移除登记：直接放行，不因无代办机永久阻断
        O.check_project_idle("p1", {"regs": []}, cfg, {"hosts": [{"id": "ext1"}], "lanes": []}, {"hosts": [], "regs": []})
        # 若最后快照仍有在跑任务：阻断
        glob_busy = {"hosts": [{"id": "ext1", "status": {"running": [{"project": "p1", "name": "j1"}], "external": []}}], "regs": []}
        with self.assertRaises(C.ConfigError) as e:
            O.check_project_idle("p1", {"regs": []}, cfg, {"hosts": [{"id": "ext1"}], "lanes": []}, glob_busy)
        self.assertIn("在跑任务", str(e.exception))
        # 若最后快照仍有未移除登记：阻断
        glob_reg = {"hosts": [], "regs": [{"host": "ext1", "home": C.STANDALONE_DATA_ROOT + "/labmon", "standalone": True, "queues": [{"project": "p1"}]}]}
        with self.assertRaises(C.ConfigError) as e2:
            O.check_project_idle("p1", {"regs": []}, cfg, {"hosts": [{"id": "ext1"}], "lanes": []}, glob_reg)
        self.assertIn("队列登记", str(e2.exception))

    def test_disk_excludes_standalone_from_shared_df_and_ours(self):
        self.hf.write_text(
            '[[hosts]]\nid = "c1"\nssh = "root@198.51.100.1"\nplatform_job = "A"\ndata_root = "/data/shared/me"\n\n'
            '[[hosts]]\nid = "e1"\nssh = "u@203.0.113.1"\nplatform_kind = "none"\ndata_root = "/home/u/work"\n'
        )
        t = 1_790_000_000.0
        hc = fake_host("c1", standalone=False, status=fake_status(t=t - 10, info={"disk": [{"path": "/data/shared", "total": 1000.0, "free": 400.0}], "gpus": []}))
        # 独立机时间更新更晚，但绝不能覆盖共享盘 _df
        he = fake_host("e1", standalone=True, status=fake_status(t=t + 10, info={"disk": [{"path": "/home/u/work", "total": 100.0, "free": 10.0}], "gpus": []}))
        df = DK._df([hc, he], {})
        self.assertAlmostEqual(df["total"] / C.GIB, 1000.0, places=2)
        self.assertIsNone(DK._df([he], {}))
        ours = DK._ours([he], {}, {}, {}, {}, t, {"burst_gb": 30, "bigwrite_gb": 5, "df": df})
        self.assertIsNone(ours)

    def test_lease_until_health_and_renew_skip_non_scm(self):
        t0 = C.parse_time("2026-12-01 12:00")
        end_abs = C.parse_time("2026-12-01 20:00")
        self.assertEqual(H.lease_end(t0 - 86400, 7, "2026-12-01 20:00"), end_abs)
        self.assertEqual(H.lease_end(t0, 2, None), t0 + 2 * 86400)

        # overview.health：other 平台用 lease_until 提醒，文案不含 SCM「预备续租」
        other_cfg = {"ext1": C.host_defaults({"id": "ext1", "ssh": "u@203.0.113.1", "platform_kind": "other", "lease_until": "2026-12-01 20:00"})}
        e = fake_host("ext1", label="ext1", status=fake_status(t=t0), platform_kind="other", standalone=True)
        msgs = O.health([e], other_cfg, t0)
        self.assertEqual(e["lease_end"], end_abs)
        self.assertEqual(len(msgs), 1)
        self.assertIn("外平台手动续租", msgs[0])
        self.assertNotIn("预备续租", msgs[0])
        # 同一期进入 6 小时档再提醒一次，之后不重复提醒
        msgs_6h = O.health([e], other_cfg, end_abs - 3 * 3600)
        self.assertEqual(len(msgs_6h), 1)
        self.assertEqual(O.health([e], other_cfg, end_abs - 2 * 3600), [])
        # 外平台原地续租（容器未重启、started 不变，只延后 lease_until 一周）：新一期的 24h / 6h 提醒必须重置并再次发出
        end_next = C.parse_time("2026-12-08 20:00")
        other_cfg_next = {"ext1": C.host_defaults({"id": "ext1", "ssh": "u@203.0.113.1", "platform_kind": "other", "lease_until": "2026-12-08 20:00"})}
        msgs_next_24 = O.health([e], other_cfg_next, end_next - 18 * 3600)
        self.assertEqual(len(msgs_next_24), 1)
        self.assertEqual(e["lease_end"], end_next)
        msgs_next_6 = O.health([e], other_cfg_next, end_next - 3 * 3600)
        self.assertEqual(len(msgs_next_6), 1)

        # renew.check：非 SCM（other/none）即使配了 platform 与临近 lease_end 也不触发自动预备
        called = []
        bad_other = C.host_defaults({"id": "ext1", "ssh": "u@203.0.113.1", "platform_kind": "other", "platform": "http://x/", "lease_until": "2026-12-01 20:00"})
        e["lease_end"] = t0 + 5 * 3600
        R.check([e], {"ext1": bad_other}, t0, reserve_fn=lambda *a, **kw: called.append(a) or "ok")
        self.assertEqual(called, [])
        self.assertIsNone(e["auto_res"])

    def test_borrow_respects_share_classification(self):
        self.hf.write_text(
            '[fleet]\ninterval = 600\n\n'
            '[[fleet.servers]]\nid = "s_priv"\nholder = "AA"\nssh = "root@h"\nport = 1\npool = "P"\nshare = "private"\n\n'
            '[[fleet.servers]]\nid = "s_bor"\nholder = "AA"\nssh = "root@h"\nport = 2\npool = "P"\nshare = "borrow"\n'
        )
        make_token("AA", role="user", scopes=("read",))
        with self.assertRaises(C.ConfigError) as e1:
            B.begin("s_priv", account="AA")
        self.assertIn("private", str(e1.exception))
        # 非法 share 值应被 fleet.conf 拒绝
        self.hf.write_text('[fleet]\ninterval = 600\n\n[[fleet.servers]]\nid = "s_bad"\nholder = "AA"\nssh = "root@h"\nport = 1\npool = "P"\nshare = "invalid"\n')
        with self.assertRaises(C.ConfigError) as e2:
            F.conf()
        self.assertIn("share", str(e2.exception))


class WorkerPathAndInitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.hf = self.d / "hosts.toml"
        self.env = mock.patch.dict(os.environ, {
            "LABMON_HOSTS": str(self.hf),
            "LABMON_STATE": str(self.d / "state"),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_worker_env_and_standalone_project_init(self):
        dr = self.d / "work"
        self.hf.write_text(f'[[hosts]]\nid = "mac1"\nplatform_kind = "none"\ndata_root = "{dr}"\n')
        h = W.host_conf("mac1")
        env_args = shlex.split(W.worker_env(h))
        self.assertIn(f"LABMON_DATA_ROOT={dr}", env_args)
        self.assertIn(f"LABMON_LOCAL={dr}/.tmp", env_args)
        self.assertIn("LABMON_STANDALONE=1", env_args)

        readme = (Path(W.__file__).resolve().parent / "data_root_standalone_readme.md").read_text()
        with mock.patch.object(W, "committed", return_value=readme):
            rc, out = W.project_init("mac1", "demo")
        self.assertEqual(rc, 0, out)
        for sub in ("labmon", "envs", "datasets", "weights", "archive", ".tmp",
                    "projects/demo/code", "projects/demo/queues", "projects/demo/runs"):
            self.assertTrue((dr / sub).is_dir(), sub)
        self.assertIn("独立主机本地工作目录", (dr / "README.md").read_text())

    def test_standalone_add_blocks_outside_projects_root_and_symlink_escape(self):
        dr = self.d / "work"
        outside = self.d / "dev-repo"
        outside.mkdir(parents=True)
        (outside / "q.txt").write_text("job1 | echo ok\n")
        inside = dr / "projects" / "demo"
        inside.mkdir(parents=True)
        (inside / "q.txt").write_text("job1 | echo ok\n")
        # 符号链接逃逸到原开发仓库
        escape = dr / "projects" / "link_out"
        escape.symlink_to(outside)

        # 1) worker.common.queue_root_ok 直接校验（含符号链接越界）
        self.assertEqual(WMOD.queue_root_ok(str(inside), str(dr)), str(inside.resolve()))
        for bad in (str(outside), str(dr / "projects"), str(escape)):
            with self.assertRaises(ValueError, msg=bad):
                WMOD.queue_root_ok(bad, str(dr))

        # 2) workerctl.run_out("add") 看板侧拦截（不连 ssh）
        self.hf.write_text(f'[[hosts]]\nid = "mac1"\nplatform_kind = "none"\ndata_root = "{dr}"\n')
        with mock.patch.object(W, "ssh") as ssh_fn:
            rc, out = W.run_out("add", "mac1", [str(outside), "q.txt"], project="demo")
        self.assertEqual(rc, 2)
        self.assertIn("projects/<项目>", out)
        ssh_fn.assert_not_called()

        # 3) worker/cli.py 远端 realpath 拦截符号链接越界
        wh = dr / "labmon"
        wh.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "LABMON_HOME": str(wh), "LABMON_DATA_ROOT": str(dr), "LABMON_STANDALONE": "1"}
        r_bad = subprocess.run([sys.executable, str(Path(W.__file__).resolve().parent / "labmon_worker.py"),
                                "add", str(escape), "q.txt", "--project", "demo", "--host", "mac1"],
                               env=env, capture_output=True, text=True)
        self.assertEqual(r_bad.returncode, 2)
        self.assertIn("allow_outside_root", r_bad.stderr)
        # 合法目录通过
        r_ok = subprocess.run([sys.executable, str(Path(W.__file__).resolve().parent / "labmon_worker.py"),
                               "add", str(inside), "q.txt", "--project", "demo", "--host", "mac1"],
                              env=env, capture_output=True, text=True)
        self.assertEqual(r_ok.returncode, 0, r_ok.stderr)
        # 允许 allow_outside_root = true 时放行
        r_allow = subprocess.run([sys.executable, str(Path(W.__file__).resolve().parent / "labmon_worker.py"),
                                  "add", str(outside), "q.txt", "--project", "demo", "--host", "mac1"],
                                 env={**env, "LABMON_ALLOW_OUTSIDE_ROOT": "1"}, capture_output=True, text=True)
        self.assertEqual(r_allow.returncode, 0, r_allow.stderr)

    def test_via_refuses_returned_standalone_host(self):
        self.hf.write_text(
            '[[hosts]]\nid = "c1"\nplatform_job = "A"\ndata_root = "/data/labmon"\n\n'
            '[[hosts]]\nid = "e1"\nplatform_kind = "other"\ndata_root = "/data/labmon"\nreturned = "2026-11-20 10:00"\n\n'
            '[[hosts]]\nid = "e2"\nplatform_kind = "other"\ndata_root = "/data/labmon"\n'
        )
        # 无论是否显式传 home（页面 reg_rm 会传 x.home），都绝不能借同路径的集群主机 c1 代办
        for home in (None, "/data/labmon/labmon"):
            with self.assertRaises(C.ConfigError, msg=str(home)) as e:
                W.via("e1", home=home)
            self.assertIn("独立主机", str(e.exception))


class SshKeyIsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.hf = self.d / "hosts.toml"
        self.secrets = self.d / "secrets"
        self.env = mock.patch.dict(os.environ, {
            "LABMON_HOSTS": str(self.hf),
            "LABMON_STATE": str(self.d / "state"),
            "LABMON_SECRETS": str(self.secrets),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_key_lifecycle_permissions_and_isolation(self):
        # 生成专用 Ed25519 密钥对：目录 0700、私钥 0600，仅返回公钥/指纹/授权命令
        info = P.ensure_ssh_key("Alice", "ext-1")
        self.assertTrue(info["pubkey"].startswith("ssh-ed25519 "))
        self.assertIn("labmon:alice:ext-1", info["pubkey"])
        self.assertTrue(info["fingerprint"] and info["fingerprint"].startswith("SHA256:"))
        self.assertIn("authorized_keys", info["auth_cmd"])
        self.assertNotIn("PRIVATE KEY", json.dumps(info))

        priv = P.ssh_key_path("Alice", "ext-1")
        self.assertEqual(stat.S_IMODE(priv.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(priv.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(priv.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o700)

        # 不传 rotate 幂等复用；rotate=True 换新密钥
        self.assertEqual(P.ensure_ssh_key("Alice", "ext-1")["fingerprint"], info["fingerprint"])
        info2 = P.ensure_ssh_key("Alice", "ext-1", rotate=True)
        self.assertNotEqual(info2["fingerprint"], info["fingerprint"])

        # 非法段拦截路径穿越
        for bad_own, bad_hid in (("../evil", "h1"), ("alice", "../h1"), ("alice", ".hidden"), ("alice", "a/b")):
            with self.assertRaises(P.PlatformError):
                P.ssh_key_path(bad_own, bad_hid)

        # 删除彻底清理私钥与公钥
        self.assertTrue(P.del_ssh_key("Alice", "ext-1"))
        self.assertFalse(priv.exists())
        self.assertIsNone(P.ssh_key_info("Alice", "ext-1"))

    def test_ssh_argv_and_host_ssh_argv_identities_only(self):
        P.ensure_ssh_key("alice", "ext-1")
        P.ensure_ssh_key("bob", "ext-2")
        h_ext = C.host_defaults({"id": "ext-1", "ssh": "u@203.0.113.1", "port": 2222, "owner": "alice", "ssh_key": "managed"})
        h_ext2 = C.host_defaults({"id": "ext-2", "ssh": "u@203.0.113.1", "port": 2222, "owner": "bob", "ssh_key": "managed"})
        h_missing = C.host_defaults({"id": "ext-miss", "ssh": "u@203.0.113.9", "port": 2222, "owner": "alice", "ssh_key": "managed"})
        h_scm = C.host_defaults({"id": "29", "ssh": "root@198.51.100.29", "port": 15155, "platform_job": "ME"})

        argv1 = L.host_ssh_argv(h_ext)
        argv2 = L.host_ssh_argv(h_ext2)
        argv_scm = L.host_ssh_argv(h_scm)

        # 托管密钥强制 -F /dev/null、IdentityAgent=none、-i <key>、IdentitiesOnly=yes，且 ControlPath 与 known_hosts 按机隔离
        self.assertIn("-F", argv1)
        self.assertIn("/dev/null", argv1)
        self.assertIn("IdentityAgent=none", argv1)
        self.assertIn("-i", argv1)
        self.assertIn(str(P.ssh_key_path("alice", "ext-1")), argv1)
        self.assertIn("IdentitiesOnly=yes", argv1)
        self.assertIn(f"UserKnownHostsFile={L.ext_known_hosts('ext-1')}", argv1)
        ctl1 = next(x for x in argv1 if x.startswith("ControlPath="))
        ctl2 = next(x for x in argv2 if x.startswith("ControlPath="))
        ctl_scm = next(x for x in argv_scm if x.startswith("ControlPath="))
        self.assertNotEqual(ctl1, ctl2)
        self.assertNotEqual(ctl1, ctl_scm)
        self.assertEqual(ctl_scm, f"ControlPath={L.CTL}")
        # 旧 SCM 集群主机保持原参数不变，不强加 IdentitiesOnly
        self.assertNotIn("IdentitiesOnly=yes", argv_scm)
        self.assertNotIn("IdentityAgent=none", argv_scm)

        # 托管私钥缺失时必须拒绝连接（抛 ConfigError），overview.fetch / workerctl.ssh 均不启动 ssh 进程，避免回退宿主机密钥
        with self.assertRaises(C.ConfigError) as e_miss:
            L.host_ssh_argv(h_missing)
        self.assertIn("缺少托管 SSH 私钥", str(e_miss.exception))
        with mock.patch.object(L, "ssh_run") as run_mock:
            raw, err = O.fetch(h_missing)
            self.assertIsNone(raw)
            self.assertIn("缺少托管 SSH 私钥", err)
            rc, out = W.ssh(h_missing, "true")
            self.assertEqual(rc, 255)
            self.assertIn("缺少托管 SSH 私钥", out)
            run_mock.assert_not_called()

        # close_host_ssh 在私钥已删后仍可发 -O exit 并清理独立 known_hosts
        kh1 = L.ext_known_hosts("ext-1")
        kh1.write_text("203.0.113.1 ssh-ed25519 AAAA\n")
        P.del_ssh_key("alice", "ext-1")
        with mock.patch.object(L.subprocess, "run") as run:
            L.close_host_ssh(h_ext, drop_known_hosts=True)
        run.assert_called_once()
        self.assertIn("-O", run.call_args.args[0])
        self.assertIn("exit", run.call_args.args[0])
        self.assertNotIn("-i", run.call_args.args[0])
        self.assertFalse(kh1.exists())

    def test_private_key_never_enters_backup_or_api_hosts(self):
        info = P.ensure_ssh_key("alice", "ext-1")
        self.hf.write_text(
            '[[hosts]]\nid = "ext-1"\nssh = "u@203.0.113.1"\nplatform_kind = "other"\n'
            'owner = "alice"\nssh_key = "managed"\nlease_until = "2026-12-20 18:00"\n'
        )
        # api.hosts 只含逻辑模式与指纹，不含私钥路径或原文
        got = A.hosts()["hosts"][0]
        self.assertEqual(got["ssh_key_mode"], "managed")
        self.assertEqual(got["ssh_key_fp"], info["fingerprint"])
        self.assertEqual((got["platform_kind"], got["share"], got["standalone"]), ("other", "private", True))
        raw_api = json.dumps(got, ensure_ascii=False)
        self.assertNotIn("PRIVATE KEY", raw_api)
        self.assertNotIn(str(P.ssh_key_path("alice", "ext-1")), raw_api)

        # backup.make 冷备包绝不含任何私钥文件或私钥文本
        tar_path = BK.make(str(self.d / "bak.tar.gz"))
        with tarfile.open(tar_path, "r:gz") as tf:
            names = tf.getnames()
            self.assertFalse(any("ssh_keys" in n for n in names), names)
            for m in tf.getmembers():
                if m.isfile():
                    body = tf.extractfile(m).read()
                    self.assertNotIn(b"OPENSSH PRIVATE KEY", body, m.name)

    def test_no_keychain_without_secrets_dir_touches_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "LABMON_SECRETS"} | {"LABMON_NO_KEYCHAIN": "1"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(P.ssh_key_info("alice", "ext-1"))
            self.assertFalse(P.del_ssh_key("alice", "ext-1"))
            with self.assertRaises(P.PlatformError):
                P.ensure_ssh_key("alice", "ext-1")


if __name__ == "__main__":
    unittest.main()
