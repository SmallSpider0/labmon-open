"""项目的归档与恢复（#97，labmon/projects.py）：前提、多主机移除登记（已归还的经共用 worker_home 的在用主机代办）、撤权重与指令、
有一处失败就停、看板服务里摘下与加回、命令行与 wait。主机都没有 ssh（同一段命令在本机跑），worker 是本仓库的 labmon_worker.py。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import NO_HOSTS, board, copy_worker, fake_status, login, post_req, register, running_job, waiting_job, write_config  # noqa: E402

from labmon import config as K  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon.config import ConfigError  # noqa: E402
from labmon.records import records  # noqa: E402


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name).resolve()
        self.wh = d / "alice" / "labmon"                           # 三台共用的 worker_home（共享盘）
        self.wh.mkdir(parents=True)
        copy_worker(self.wh)
        self.hf = d / "hosts.toml"
        self.hosts_toml()
        self.env = mock.patch.dict(os.environ, {"LABMON_HOSTS": str(self.hf), "LABMON_STATE": str(d / "state"), "LABMON_LAN": "on"})
        self.env.start()
        self.cfg = write_config(d, d / "q1", d / "p1")           # id 取 out_dir 的目录名：p1
        register(self.cfg)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def hosts_toml(self, b_python: str | None = None, sched: str = '[sched.weight]\np1 = 2\np2 = 3\n\n[sched.pause]\np1 = true\n'):
        py = sys.executable
        self.hf.write_text(f'[[hosts]]\nid = "a"\ndata_root = "{self.d / "alice"}"\npython = "{py}"\n\n'
                           f'[[hosts]]\nid = "b"\ndata_root = "{self.d / "alice"}"\npython = "{b_python or py}"\n\n'
                           f'[[hosts]]\nid = "c"\ndata_root = "{self.d / "alice"}"\npython = "{py}"\nreturned = "2026-11-09 10:00"\n\n' + sched)

    def reg(self, host: str, name: str, project: str) -> dict:
        q = self.wh / "hosts" / host / "queues"
        q.mkdir(parents=True, exist_ok=True)
        (q / f"{name}.json").write_text(json.dumps({"root": str(self.d / name), "jobs": "jobs/q.txt", "project": project}))
        return {"root": str(self.d / name), "jobs": "jobs/q.txt", "project": project}

    def regs_left(self) -> dict:
        return {h.name: sorted(f.stem for f in (h / "queues").glob("*.json")) for h in sorted((self.wh / "hosts").iterdir()) if (h / "queues").is_dir()}

    def glob(self, hosts=None, regs=None, age=0.0) -> dict:
        now = time.time()
        return {"generated": now - age, "hosts": hosts if hosts is not None else [{"id": "a", "status": fake_status()}, {"id": "b", "status": fake_status()}],
                "regs": regs or []}

    def psnap(self, **kw) -> dict:
        return {"generated": time.time(), "title": "项目一", "no_work_since": time.time() - 100, "lanes": [{"unfinished": 0}], **kw}

    def test_block_reasons(self):
        # 前提同 collect._no_work：在跑、在等（单元也算）、清单没跑完、看板没说暂时没实验、快照旧了，各写原因；都没有才能归档
        g = self.glob(hosts=[{"id": "a", "status": fake_status(running=[running_job("r1", "p1")], waiting=[waiting_job("w1", "p1"), waiting_job("w2", "p2")])}])
        self.assertEqual(PJ.archive_block("p1", self.psnap(), g), "还有实验：1 个任务在跑，1 个在等")
        self.assertIsNone(PJ.archive_block("p2x", self.psnap(), g))
        self.assertEqual(PJ.archive_block("p1", self.psnap(lanes=[{"unfinished": 3}, {"unfinished": None}]), self.glob()), "还有实验：清单里还有 3 个没跑完")
        self.assertIn("还没说暂时没实验", PJ.archive_block("p1", self.psnap(no_work_since=None), self.glob()))
        self.assertIn("没更新", PJ.archive_block("p1", self.psnap(generated=time.time() - 3600), self.glob()))
        self.assertIsNone(PJ.archive_block("p1", self.psnap(generated=time.time() - 3600), self.glob(), interval=3600))   # 采集间隔长的项目按 3 个间隔算
        self.assertIn("工作台的数据", PJ.archive_block("p1", self.psnap(), self.glob(age=3600)))
        self.assertIn("还没采集过", PJ.archive_block("p1", None, self.glob()))
        self.assertIsNone(PJ.archive_block("p1", self.psnap(), self.glob()))
        with self.assertRaises(ConfigError) as e:
            PJ.archive("p1", self.psnap(no_work_since=None), self.glob())
        self.assertIn("p1 现在不能归档", str(e.exception))
        self.assertEqual(PJ.archived(), {})

    def test_user_archive_keeps_admin_schedule(self):
        """普通用户归档自己的项目（#16）：只撤自己下的暂停；管理员定的权重、优先与别人下的暂停留着，恢复后照旧生效，归档绕不过去。管理员归档照旧全撤。"""
        from labmon import users as U
        amy = U.Ident("amy", "user", frozenset({"project"}), "token", "k")
        K.set_sched_ovr("focus", "p1", True)
        K.set_sched_ovr("pause", "p1", True, by="boss")
        PJ.archive("p1", self.psnap(), self.glob(), who=amy)
        sw = K.sched()
        self.assertEqual((sw["weight"]["p1"], sw["pause"], sw["focus"]), (2, {"p1": None}, {"p1": None}))
        self.assertEqual(K.pause_by(), {"p1": "boss"})
        PJ.restore("p1")
        K.set_sched_ovr("pause", "p1", True, by="amy")
        PJ.archive("p1", self.psnap(), self.glob(), who=amy)
        sw = K.sched()
        self.assertEqual((sw["weight"].get("p1"), "pause" in sw, K.pause_by()), (2, False, {}))   # 自己下的暂停撤了，权重与优先留着
        PJ.restore("p1")
        PJ.archive("p1", self.psnap(), self.glob())
        self.assertEqual(K.sched(), {"weight": {"p2": 3}})

    def test_user_archive_keeps_admin_host_overrides(self):
        """普通用户归档（#27）：只给某几台下的暂停也只撤他自己下的（各台按那台的 by 分开判断）；管理员在 b 上下的暂停、a 上的优先留着。
        快照晚了、那台上其实已换成管理员的：worker 在锁里核（--expect-by），不撤、归档报没做完。管理员归档全撤。"""
        from labmon import users as U
        amy = U.Ident("amy", "user", frozenset({"project"}), "token", "k")
        ova, ovb = (self.wh / "hosts" / h / "overrides.json" for h in "ab")
        for f in (ova, ovb):
            f.parent.mkdir(parents=True, exist_ok=True)
        ova.write_text(json.dumps([{"kind": "pause", "target": "p1", "by": "Amy"}, {"kind": "focus", "target": "p1", "by": "boss"}]))
        ovb.write_text(json.dumps([{"kind": "pause", "target": "p1", "by": "boss"}]))
        hosts = [{"id": "a", "status": fake_status(overrides=json.loads(ova.read_text()))},
                 {"id": "b", "status": fake_status(overrides=json.loads(ovb.read_text()))}]
        g = self.glob(hosts=hosts)
        msg = PJ.archive("p1", self.psnap(), g, who=amy)
        self.assertEqual([(o["kind"], o.get("by")) for o in json.loads(ova.read_text())], [("focus", "boss")])
        self.assertEqual(json.loads(ovb.read_text()), [{"kind": "pause", "target": "p1", "by": "boss"}])
        self.assertIn("a 上的优先", msg)
        self.assertIn("b 上的暂停", msg)
        PJ.restore("p1")
        ova.write_text(json.dumps([{"kind": "pause", "target": "p1", "by": "boss"}]))      # 快照里还是 amy 的，那台已换成管理员的
        g = self.glob(hosts=[{"id": "a", "status": fake_status(overrides=[{"kind": "pause", "target": "p1", "by": "amy"}])}])
        with self.assertRaises(ConfigError) as x:
            PJ.archive("p1", self.psnap(), g, who=amy)
        self.assertIn("没做完", str(x.exception))
        self.assertEqual(len(json.loads(ova.read_text())), 1)
        self.assertNotIn("p1", PJ.archived())
        PJ.archive("p1", self.psnap(), self.glob(hosts=hosts))                                 # 管理员：全撤
        self.assertEqual((json.loads(ova.read_text()), json.loads(ovb.read_text())), ([], []))

    def test_archive_multi_host(self):
        # 两台在用 + 一台已归还共用 worker_home：三处 p1 的登记都移除（已归还的 c 经 a 代办，不连 c）；p2 的不动；
        # 主机清单里已没有、又没有共用 worker_home 的在用主机的 z 跳过并写明；按主机下的暂停只撤 p1 的；[sched] 里 p1 的权重与暂停撤掉
        r = {k: self.reg(h, k, p) for h, k, p in [("a", "q1", "p1"), ("a", "q2", "p2"), ("b", "q3", "p1"), ("c", "q4", "p1")]}
        ov = self.wh / "hosts" / "a" / "overrides.json"
        ov.write_text(json.dumps([{"kind": "pause", "target": "p1", "until": None, "t": 1}, {"kind": "pause", "target": "p2", "until": None, "t": 1}]))
        hosts = [{"id": "a", "status": fake_status(overrides=[{"kind": "pause", "target": "p1"}, {"kind": "pause", "target": "p2"},
                                                             {"kind": "pause", "target": "p1", "global": True}])},
                 {"id": "b", "status": fake_status(overrides=[{"kind": "focus", "target": "p2"}])}]
        regs = [{"host": "a", "home": str(self.wh), "state": "active", "queues": [r["q1"], r["q2"]]},
                {"host": "b", "home": str(self.wh), "state": "active", "queues": [r["q3"]]},
                {"host": "c", "home": str(self.wh), "state": "returned", "queues": [r["q4"]]},
                {"host": "z", "home": "/elsewhere/labmon", "state": "unlisted", "queues": [{"root": "/x", "jobs": "j.txt", "project": "p1"}]},
                {"host": "w", "home": str(self.wh), "state": "active", "queues": None}]
        msg = PJ.archive("p1", self.psnap(), self.glob(hosts=hosts, regs=regs))
        self.assertIn("归档 p1（项目一）：移除了 3 条登记", msg)
        self.assertIn("z /x/j.txt（z 已不在用", msg)
        self.assertIn("w（登记没读到）", msg)
        self.assertEqual(self.regs_left(), {"a": ["q2"], "b": [], "c": []})
        self.assertEqual([(o["kind"], o["target"]) for o in json.loads(ov.read_text())], [("pause", "p2")])
        self.assertEqual(K.sched(), {"weight": {"p2": 3}})
        e = PJ.archived()["p1"]
        self.assertEqual((e["title"], [(x["host"], Path(x["root"]).name) for x in e["regs"]]),
                         ("项目一", [("a", "q1"), ("b", "q3"), ("c", "q4")]))
        rec = [x for x in records(cat="config") if x.get("kind") == "project" and x["msg"].startswith("归档")]
        self.assertEqual(len(rec), 1)
        self.assertIn("移除 a ", rec[0]["detail"])
        self.assertTrue(any(x.get("kind") == "override" and x.get("act") == "rm" and x.get("target") == "p1" for x in records()))    # 全局暂停的撤销照记，时间线区间闭合
        with self.assertRaises(ConfigError) as x:
            PJ.archive("p1", self.psnap(), self.glob(hosts=hosts, regs=regs))
        self.assertIn("已经归档了", str(x.exception))

    def test_one_failure_stops(self):
        # b 上的 worker rm 失败：a 的照样移除了，但调度不撤、不记归档；报回已移除的与失败的。b 好了再归档一次接着做（a 上的回“没登记过”，无害）
        bad = self.d / "badpy"
        bad.write_text("#!/bin/sh\necho 'worker 目录写不了' >&2\nexit 1\n")
        bad.chmod(0o755)
        self.hosts_toml(b_python=str(bad))
        r = {k: self.reg(h, k, "p1") for h, k in [("a", "q1"), ("b", "q3")]}
        regs = [{"host": "a", "home": str(self.wh), "state": "active", "queues": [r["q1"]]}, {"host": "b", "home": str(self.wh), "state": "active", "queues": [r["q3"]]}]
        with self.assertRaises(ConfigError) as e:
            PJ.archive("p1", self.psnap(), self.glob(regs=regs))
        self.assertIn("归档 p1 没做完", str(e.exception))
        self.assertIn("worker 目录写不了", str(e.exception))
        self.assertIn("已移除 1 条登记", str(e.exception))
        self.assertEqual((self.regs_left(), PJ.archived(), K.sched()["weight"]), ({"a": [], "b": ["q3"]}, {}, {"p1": 2, "p2": 3}))
        self.hosts_toml()
        self.assertIn("移除了 2 条登记", PJ.archive("p1", self.psnap(), self.glob(regs=regs)))
        self.assertEqual(self.regs_left(), {"a": [], "b": []})

    def test_offline(self):
        # 不在内网：要移除登记时拒绝，什么都不动；没有登记、没有按主机指令的照样能归档（只改本机文件）
        r = self.reg("a", "q1", "p1")
        with mock.patch.dict(os.environ, {"LABMON_LAN": "off"}):
            with self.assertRaises(ConfigError) as e:
                PJ.archive("p1", self.psnap(), self.glob(regs=[{"host": "a", "home": str(self.wh), "state": "active", "queues": [r]}]))
            self.assertIn("本机不在内网", str(e.exception))
            self.assertEqual(self.regs_left(), {"a": ["q1"]})
            self.assertIn("没有要移除的登记", PJ.archive("p2", self.psnap(), self.glob()))

    def test_worker_rm_returned_host_via_active(self):
        # labmon worker rm 对已归还的主机经共用 worker_home 的在用主机代办（以前 host_conf 直接拒绝）；add 照旧只认在用的
        from labmon import workerctl as W
        self.reg("c", "q4", "p1")
        self.assertEqual(W.via("c")["id"], "a")
        rc, out = W.run_out("rm", "c", [str(self.d / "q4"), "jobs/q.txt"])
        self.assertEqual((rc, self.regs_left()), (0, {"c": []}), out)
        with self.assertRaises(ConfigError):
            W.run_out("add", "c", [str(self.d / "q4"), "jobs/q.txt"], project="p1")
        with self.assertRaises(ConfigError) as e:
            W.via("z", "/elsewhere/labmon")
        self.assertIn("没有与它共用 worker 目录", str(e.exception))

    def test_permanent_delete_checks_worker_and_preserves_other_data(self):
        from labmon import overview
        from labmon.records import log_events
        from labmon.state import save_json, state_dir, usage_file
        self.hosts_toml(sched="")
        shared = self.d / "experiment.txt"
        shared.write_text("实验文件")
        PJ.put_config("p2", 'id = "p2"\n')
        PJ._save({"p1": {"id": "p1", "t": 10, "regs": [{"host": "c"}]}, "p2": {"id": "p2", "t": 20}})
        usage_file("a").parent.mkdir(parents=True)
        usage_file("a").write_text("用量历史")
        log_events([{"kind": "project", "project": "p1", "msg": "旧审计"}])
        save_json(state_dir() / "snapshot.json", {"generated": 1, "hosts": [
            {"id": "a", "status": fake_status(running=[running_job("t1", "p1")])}]})
        self.reg("b", "q2", "p2")
        self.reg("c", "q1", "p1")
        status = self.wh / "hosts" / "a" / "status.json"
        old_status = self.wh / "hosts" / "c" / "status.json"
        save_json(old_status, {"waiting": [{"project": "p1"}]})
        save_json(status, fake_status())
        delete = lambda: PJ.delete("p1", "10", "admin", lambda: None)  # noqa: E731
        with self.assertRaisesRegex(ConfigError, "队列登记"):
            delete()
        (old_status.parent / "queues" / "q1.json").unlink()
        save_json(status, fake_status(external=[{"project": "p1"}]))
        with self.assertRaisesRegex(ConfigError, "在跑或等待"):
            delete()
        save_json(status, fake_status())
        overrides = status.with_name("overrides.json")
        save_json(overrides, [{"kind": "pause", "target": "p1"}])
        with self.assertRaisesRegex(ConfigError, "暂停或优先"):
            delete()
        overrides.unlink()
        with mock.patch.object(overview, "_project_worker_state", return_value=(None, "坏状态")):
            with self.assertRaisesRegex(ConfigError, "未能核实"):
                delete()
        with mock.patch.object(PJ.shutil, "rmtree", side_effect=OSError("模拟 IO 失败")):
            with self.assertRaisesRegex(ConfigError, "仍保留"):
                delete()
        self.assertIn("p1", PJ.archived())
        ver = PJ.get_config("p1")["ver"]
        self.assertIn("永久删除", delete())
        self.assertEqual(set(PJ.archived()), {"p2"})
        self.assertEqual(PJ.registered(), ["p2"])
        self.assertFalse(PJ.out_dir("p1").parent.exists())
        self.assertEqual(shared.read_text(), "实验文件")
        self.assertEqual(usage_file("a").read_text(), "用量历史")
        self.assertEqual(self.regs_left()["b"], ["q2"])
        audit = [(r["msg"], r.get("who")) for r in records() if r.get("project") == "p1"]
        self.assertIn(("旧审计", None), audit)
        self.assertIn(("永久删除了看板项目 p1", "admin"), audit)
        with self.assertRaises(PJ.Refused) as ex:
            PJ.put_config("p1", self.cfg.read_text(), base=ver)
        self.assertEqual(ex.exception.code, 412)
        self.assertTrue(PJ.put_config("p1", self.cfg.read_text())["created"])

    def test_delete_checks_project_worker_home_override(self):
        from labmon.state import save_json
        self.hosts_toml(sched="")
        custom = self.d / "custom-worker"
        status = custom / "hosts" / "a" / "status.json"
        save_json(status, fake_status(running=[running_job("t1", "p1")]))
        current = PJ.get_config("p1")
        text = f'id = "p1"\n[[hosts]]\nid = "a"\nworker_home = "{custom}"\n'
        PJ.put_config("p1", text, base=current["ver"])
        PJ._save({"p1": {"id": "p1", "t": 10}})
        with self.assertRaisesRegex(ConfigError, "在跑或等待"):
            PJ.delete("p1", "10", "admin", lambda: None)
        save_json(status, fake_status())
        PJ.delete("p1", "10", "admin", lambda: None)
        self.assertFalse(PJ.cfg_file("p1").exists())

    def test_permanent_delete_rejects_symlink(self):
        self.hosts_toml(sched="")
        PJ._save({"p1": {"id": "p1", "t": 10}})
        project = PJ.cfg_file("p1").parent
        moved = self.d / "target"
        project.rename(moved)
        project.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "路径"):
            PJ.delete("p1", "10", "admin", lambda: None)
        self.assertTrue((moved / "monitor.toml").is_file())

    def test_restore_and_ids(self):
        # 恢复：从 projects.json 去掉、记一条；看板上的配置没了时拒绝；project_ids 是登记了的减去已归档的，unknown_project 说“已归档”
        from labmon import api as A
        from labmon import waits as M
        PJ.archive("p1", self.psnap(), self.glob())
        (self.d / "o").mkdir()
        register(write_config(self.d / "o", self.d / "q9", self.d / "p9"))
        self.assertEqual((PJ.registered(), PJ.project_ids()), (["p1", "p9"], ["p9"]))
        self.assertIn("已归档：先 labmon project restore p1", A.unknown_project("p1"))
        self.assertEqual(M.wait_check(load_cfg(self.cfg), self.d / "p1", self.d / "w.json")["archived"][:9], "项目 p1 已于 ")
        self.assertIn("用命令行归档", M.wait_check(load_cfg(self.cfg), self.d / "p1", self.d / "w.json")["archived"])    # 记着谁做的（#121）
        self.assertEqual(M.check_told(load_cfg(self.cfg), self.d / "w.json")[:1], [PJ.archived_msg("p1")])          # labmon check 也列出
        self.assertIn("archived", M.wait_check(load_cfg(self.cfg), self.d / "p1", self.d / "w.json"))      # 每次都报，不记成已报
        PJ.cfg_file("p1").rename(self.d / "gone.toml")
        with self.assertRaises(ConfigError) as e:
            PJ.restore("p1")
        self.assertIn("看板上没有 p1 的配置", str(e.exception))
        (self.d / "gone.toml").rename(PJ.cfg_file("p1"))
        self.assertIn("恢复 p1（项目一", PJ.restore("p1"))
        self.assertEqual((PJ.archived(), PJ.project_ids()), ({}, ["p1", "p9"]))
        with self.assertRaises(ConfigError):
            PJ.restore("p1")
        self.assertEqual(len([x for x in records(cat="config") if x.get("kind") == "project"]), 4)     # 两次登记、归档、恢复

    def test_cross_process_lock(self):
        # Codex #118：两个进程同时改 projects.json（这里是两个命令行各恢复一个项目），读 → 改 → 写要跨进程互斥，不然后写的把先删的那条又写回去
        import subprocess
        from labmon.state import save_json, state_dir
        for k in ("pa", "pb"):
            PJ.cfg_file(k).parent.mkdir(parents=True)
            PJ.cfg_file(k).write_text(f'id = "{k}"\n')
        save_json(state_dir() / "projects.json", {"archived": {k: {"id": k, "t": 1} for k in ("pa", "pb")}})
        code = ("import sys, time; sys.path.insert(0, sys.argv[2]); from labmon import projects as PJ; s = PJ._save\n"
                "PJ._save = lambda a: (time.sleep(0.5), s(a))\nprint(PJ.restore(sys.argv[1]))")
        env = {**os.environ, "LABMON_NO_NOTIFY": "1", "LABMON_NO_KEYCHAIN": "1"}
        root = str(Path(__file__).resolve().parent.parent)
        ps = [subprocess.Popen([sys.executable, "-c", code, k, root], env=env, stdout=subprocess.PIPE, text=True) for k in ("pa", "pb")]
        outs = [x.communicate(timeout=30)[0] for x in ps]
        self.assertTrue(all("恢复" in o for o in outs), outs)
        self.assertEqual(PJ.archived(), {})

    def test_cli(self):
        # labmon project archive|restore <id>：经看板接口（POST /project/archive|restore，#162），按登记找项目，快照取登记的输出目录；记着是谁经命令行做的
        from labmon import __main__ as M
        from labmon.state import save_json, state_dir
        save_json(PJ.out_dir("p1") / "snapshot.json", self.psnap())
        save_json(state_dir() / "snapshot.json", self.glob())
        board(self).sync()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(M.main(["project", "archive", "nope"]), 3)
            self.assertIn("没有项目 nope", err.getvalue())
            self.assertEqual(M.main(["project", "archive", "p1"]), 0)
        self.assertIn("p1", PJ.archived())
        self.assertIn("经命令行（boss）归档", PJ.archived_msg("p1"))
        self.assertEqual(M.main(["project", "archive", "p1"]), 3)
        self.assertEqual(M.main(["project", "restore", "p1"]), 0)
        self.assertEqual(PJ.archived(), {})


def load_cfg(p):
    return K.load(p)


class ServeSyncTest(unittest.TestCase):
    """看板服务里的摘下与加回：启动时跳过已归档的；运行中 sync 停掉它的采集线程、从项目表拿掉，恢复后加回、起线程；页面路由。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"LABMON_HOSTS": NO_HOSTS, "LABMON_STATE": str(d / "state"), "LABMON_LAN": "on"})
        self.env.start()
        (d / "q1" / "jobs").mkdir(parents=True)
        (d / "q1" / "jobs" / "q.txt").write_text("")
        self.cfg = write_config(d, d / "q1", d / "p1")
        register(self.cfg)

    def tearDown(self):
        if hasattr(self, "srv"):
            self.srv.shutdown()
            self.srv.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def test_startup_skip_and_sync(self):
        from labmon import serve as SV
        from labmon.state import save_json
        save_json(self.d / "state" / "overview" / "projects.json", {"archived": {"p1": {"id": "p1", "title": "测试", "t": time.time()}}})
        srv = SV.Server(("127.0.0.1", 0), interval=3600)      # 不起 serve_forever：tearDown 的 shutdown 会一直等
        self.addCleanup(srv.server_close)
        self.assertEqual(srv.sync(), ["项目 p1 已归档，不采集（labmon project restore p1 加回）"])     # 启动时那一次：按登记加载、跳过已归档的
        self.assertEqual((srv.projects, srv.held, srv.sync()), ({}, {"p1"}, []))
        PJ.restore("p1")
        self.assertEqual(srv.sync(), ["p1：已恢复，接着采集"])
        p = srv.projects["p1"]
        self.assertEqual(([x.id for x in srv.overview.projects], srv.held, p.interval), (["p1"], set(), 3600))
        with p.cond:
            self.assertTrue(p.cond.wait_for(lambda: p.runs > 0, 60))
        t = next(x for x in threading.enumerate() if x.name == "collect-p1")
        save_json(self.d / "state" / "overview" / "projects.json", {"archived": {"p1": {"id": "p1", "t": time.time()}}})
        self.assertEqual(srv.sync(), ["p1：已归档，停止采集"])
        t.join(10)
        self.assertFalse(t.is_alive())                                  # 采集线程退出了
        self.assertEqual((srv.projects, srv.overview.projects, srv.held), ({}, [], {"p1"}))

    def test_sync_new_and_bad(self):
        # #161：命令行新登记的下一次对齐就加上、起线程；配置加载不了的只跳过它、记一条，配置文件没改不重复报，改好了再加上
        from labmon import serve as SV
        srv = SV.Server(("127.0.0.1", 0), interval=3600)
        self.addCleanup(srv.server_close)
        bad = PJ.cfg_file("bad")
        bad.parent.mkdir(parents=True)
        bad.write_text('id = "bad"\nalerts = 5\n')
        msgs = srv.sync()
        self.assertEqual(sorted(m[:22] for m in msgs), ["p1：开始采集", "项目 bad 的配置没加载，这个项目不采集："])
        self.assertEqual(srv.sync(), [])
        self.assertTrue(any("项目 bad 的配置没加载" in (x.get("msg") or "") for x in records()))
        bad.write_text('id = "bad"\n')
        os.utime(bad, (time.time() + 2, time.time() + 2))
        self.assertEqual(srv.sync(), ["bad：开始采集"])
        self.assertEqual(sorted(srv.projects), ["bad", "p1"])
        ts = [x for x in threading.enumerate() if x.name in ("collect-p1", "collect-bad")]
        for p in srv.projects.values():                                 # 收掉线程，等它们写完再删临时目录
            p.stop = True
            p.wake.set()
        for t in ts:
            t.join(30)

    def test_restore_while_old_round_running(self):
        # Codex #118：归档时那一轮采集还没做完就恢复，接着用原来的线程，不另起第二个（两个线程会读改写同一个输出目录）
        from labmon import serve as SV
        from labmon.state import save_json
        p = SV.Project("p1", interval=3600)
        go, inside = threading.Event(), threading.Event()
        p.once = lambda: (inside.set(), go.wait(30))
        srv = SV.Server(("127.0.0.1", 0), [p], interval=3600)
        self.addCleanup(srv.server_close)
        self.addCleanup(go.set)
        p.start("collect-p1")
        self.assertTrue(inside.wait(10))                                # 停在这一轮里
        save_json(self.d / "state" / "overview" / "projects.json", {"archived": {"p1": {"id": "p1", "t": time.time()}}})
        self.assertEqual(srv.sync(), ["p1：已归档，停止采集"])
        PJ.restore("p1")
        self.assertEqual(srv.sync(), ["p1：已恢复，接着采集"])
        self.assertIs(srv.projects["p1"], p)                            # 原来那个，没新建
        self.assertEqual(sum(t.name == "collect-p1" for t in threading.enumerate()), 1)
        inside.clear()
        go.set()
        with p.cond:
            self.assertTrue(p.cond.wait_for(lambda: p.runs >= 1, 10))
        self.assertTrue(p.alive)                                        # 没退出，下一轮照常
        t = next(x for x in threading.enumerate() if x.name == "collect-p1")
        p.stop = True                                                   # 收掉线程，免得后面的用例按名字找到它
        p.wake.set()
        t.join(10)
        self.assertFalse(t.is_alive())

    def test_delete_refuses_collector_that_has_not_exited(self):
        from labmon import serve as SV
        p = SV.Project("p1", interval=3600)
        srv = SV.Server(("127.0.0.1", 0), [p])
        self.addCleanup(srv.server_close)
        inside, go = threading.Event(), threading.Event()
        thread = threading.Thread(target=p.loop)
        p.once = lambda: (inside.set(), go.wait(10))
        p.alive = True
        thread.start()
        try:
            self.assertTrue(inside.wait(5))
            PJ._save({"p1": {"id": "p1", "t": 10}})
            srv.sync()
            with self.assertRaisesRegex(ConfigError, "正在收尾"):
                srv.delete_project("p1", "10", "admin")
            self.assertTrue(PJ.cfg_file("p1").is_file())
        finally:
            go.set()
            thread.join(10)
        srv.delete_project("p1", "10", "admin")
        self.assertFalse(PJ.cfg_file("p1").exists())
        self.assertNotIn("p1", srv.leaving)

    def test_delete_route_permissions_and_immediate_listing(self):
        from fixtures import bearer_opener, make_token
        from labmon import serve as SV
        from labmon.state import save_json, state_dir
        PJ.put_config("owner", 'id = "owner"\n', owner="member")
        PJ._save({"p1": {"id": "p1", "t": 10}, "owner": {"id": "owner", "t": 20}})
        save_json(state_dir() / "snapshot.json", {"generated": time.time(), "archived": PJ.archived_rows()})
        srv = self.srv = SV.Server(("127.0.0.1", 0))
        srv.sync()
        boss = login(self)
        member = make_token(name="member", role="user", scopes=("read", "project"))
        cli = make_token(name="boss", scopes=("project",))
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        def delete(token, pid, revision):
            req = post_req(f"{base}/project/delete", json.dumps({"project": pid, "revision": revision}).encode())
            return bearer_opener(token).open(req)

        for token, pid, revision, code in [(member, "owner", "20", 403), (cli, "p1", "10", 403),
                                           (boss, "../p1", "10", 400), (boss, "p1", None, 400),
                                           (boss, "p1", "9", 409), (boss, "other", "10", 409)]:
            with self.assertRaises(urllib.error.HTTPError) as ex:
                delete(token, pid, revision)
            self.assertEqual(ex.exception.code, code)
        with delete(boss, "p1", "10") as response:
            self.assertEqual(response.status, 200)
        with bearer_opener(boss).open(f"{base}/overview.json") as response:
            self.assertEqual([e["id"] for e in json.load(response)["archived"]], ["owner"])

    def test_stale_move_and_worker_add_after_last_project_deleted(self):
        from labmon import api, serve as SV
        PJ._save({"p1": {"id": "p1", "t": 10}})
        PJ.delete("p1", "10", "admin", lambda: None)
        old = [{"root": str(self.d), "jobs": "q.txt", "project": "p1"}]
        with mock.patch.object(SV.W, "run_out", return_value=(0, "已登记")) as worker:
            with self.assertRaisesRegex(ConfigError, "没有项目 p1"):
                SV.reg_move("a", old)
            self.assertNotEqual(api.worker("add", "a", [str(self.d), "q.txt"], project="p1")["rc"], 0)
        worker.assert_not_called()
        with mock.patch.object(SV.W, "override_each") as override:
            self.assertNotEqual(api.override("pause", "p1", ["a"], None, False)["rc"], 0)
            override.assert_not_called()

    def test_routes(self):
        # POST /project/archive：项目不在服务里 404；还有实验 409（原因同 archive_block）；不认识的动作 404；proj_info 带上 archive_block
        from labmon import serve as SV
        from labmon.state import save_json, state_dir
        save_json(state_dir() / "snapshot.json", {"generated": time.time(), "hosts": []})
        p = SV.Project("p1", interval=3600)
        p.snapshot = lambda: {"generated": time.time(), "no_work_since": None, "lanes": [{"unfinished": 2}]}
        srv = self.srv = SV.Server(("127.0.0.1", 0), [p])
        login(self)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        post = lambda act, b: urllib.request.urlopen(post_req(f"{base}/project/{act}", json.dumps(b).encode()))  # noqa: E731
        for act, body, code, want in [("archive", {"project": "nope"}, 404, "没有项目 nope"), ("archive", {"project": "p1"}, 409, "清单里还有 2 个没跑完"),
                                      ("restore", {"project": "p1"}, 409, "没有归档的项目 p1"), ("boom", {"project": "p1"}, 404, "不认识的动作")]:
            with self.assertRaises(urllib.error.HTTPError) as e:
                post(act, body)
            self.assertEqual(e.exception.code, code)
            self.assertIn(want, json.loads(e.exception.read())["error"])
        self.assertIn("清单里还有 2 个没跑完", srv.overview.proj_info()[0]["archive_block"])


if __name__ == "__main__":
    unittest.main()
