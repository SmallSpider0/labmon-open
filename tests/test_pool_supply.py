"""算力池受控试点的放行、续查及现有 worker 进程生命周期。"""
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from labmon import pool_supply as S
from labmon.config import ConfigError
from tests.fixtures import worker_module


class HostConfiguration(unittest.TestCase):
    def test_group_supply_keeps_template_runtime_paths(self):
        """组内供给走真实 guest_host 和字段投影，不能丢掉保活或试点解释器。"""
        group = {"id": "group", "holder": "Bob", "ssh": "user@node.example", "port": 22}
        own = {"id": "own", "data_root": "/shared/alice", "worker_home": "/shared/alice/labmon",
               "python": "/usr/bin/python3", "ka_bin": "/shared/keep alive", "pool_python": "/shared/torch/bin/python"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LABMON_STATE": temp}), \
                patch.object(S.fleet, "conf", return_value={"servers": [group]}), \
                patch.object(S.config, "active_hosts", return_value=[own]), \
                patch.object(S.fleet, "borrowed", return_value=False), \
                patch.object(S.fleet, "off_servers", return_value={}), \
                patch.object(S.renew, "state", return_value={"res": {}}), \
                patch.object(S.fleetops, "busy", return_value={}), patch.object(S, "snapshot", return_value={}):
            target, regular, _ = S.host_of("group", "Bob", checking=True)
        self.assertFalse(regular)
        for key in ("data_root", "worker_home", "python", "ka_bin", "pool_python"):
            self.assertEqual(target[key], own[key], key)


class Supply(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.dict(os.environ, {"LABMON_STATE": tmp}))
        self.until = time.time() + 900
        self.generation = time.time() - 1000
        stack.enter_context(patch.object(S.lease, "need"))
        stack.enter_context(patch.object(S, "host_of", side_effect=lambda h, p, **kw: ({"id": h}, h == "a", self.until + 10)))
        stack.enter_context(patch.object(S, "account_project", side_effect=lambda p, a: a))
        self.mock_remote = stack.enter_context(patch.object(S, "remote", side_effect=self.remote))
        self.lost = set()

    def remote(self, host, regular, device, action, **opts):
        node = host["id"]
        if action == "pool-probe":
            return {"node": node, "physical_host": None, "model": "fixture", "generation": self.generation, "clock": time.time(),
                    "state": "ready", "cpu_cores": 4, "ram_gib": 12, "ram_limit_gib": 16,
                    "cards": [{"uuid": device, "vram_mb": 40000, "free_mb": 40000, "idle": True}]}
        if action == "pool-trial" and node in self.lost:
            raise ConfigError("离线夹具模拟丢失回执")
        return {"finished": True, "success": True,
                "cuda": {"device": device, "start": 100 + (node == "b"), "end": 125, "steps": 6}}

    def approve(self, host, agree=True):
        return S.supply({"host": host, "device": "GPU-" + host * 36, "holder": host.upper(), "account": host,
                         "project": "test-" + host, "until": self.until, "agreed": agree}, "admin")

    def test_two_nodes_single_card_each_and_no_fake_dual_card(self):
        for host in "ab":
            self.approve(host)
            self.assertTrue(S.held(host))
        out = S.run(["a", "b"], "admin")
        self.assertEqual({r["host"] for r in out["attempts"]}, {"a", "b"})
        self.assertEqual({r["project"] for r in out["attempts"]}, {"test-a", "test-b"})
        self.assertTrue(all(r["success"] for r in out["attempts"]))
        self.assertEqual(out["overlap_s"], 24)
        self.assertTrue(out["same_node"])
        attempts = [x for x in self.mock_remote.call_args_list if x.args[3] == "pool-trial"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len({x.kwargs["attempt"] for x in attempts}), 2)
        self.assertFalse(any(S.held(host) for host in "ab"))

    def test_no_consent_revoke_and_lost_receipt(self):
        with self.assertRaises(ConfigError):
            self.approve("a", agree=False)
        self.approve("a")
        self.approve("b")
        self.assertEqual(S.revoke("b", "admin"), {"awaiting_cleanup": False})
        with self.assertRaises(ConfigError):
            S.run(["a", "b"], "admin")
        self.approve("b")
        self.lost.add("b")
        out = S.run(["a", "b"], "admin")
        self.assertFalse(out["attempts"][1]["finished"])
        self.assertTrue(S.held("b"))
        self.assertEqual(S.revoke("b", "admin"), {"awaiting_cleanup": True})
        self.assertTrue(S.reconcile("b", "admin")["finished"])
        self.assertFalse(S.held("b"))


class SupplyGate(unittest.TestCase):
    def test_gate_survives_worker_restart_and_blocks_queue_and_slot(self):
        with tempfile.TemporaryDirectory() as d:
            w = worker_module(Path(d))
            gate = {"host": "a", "supply_id": "a" * 32, "action": "close", "regular": False}
            self.assertTrue(w.pool_gate(gate)["closed"])
            worker = w.Worker("a")
            worker.ovr = []
            self.assertEqual(worker.override_hit({"project": "p"})[0], "pool")
            for kind in ("queue", "slot"):
                with patch.object(worker, "start") as start, patch.object(worker, "grant") as grant:
                    why, resource = worker.launch({"name": "job", "kind": kind}, [])
                    self.assertIn("算力池", why)
                    start.assert_not_called()
                    grant.assert_not_called()
            with self.assertRaisesRegex(ValueError, "供给编号"):
                w.pool_gate({**gate, "action": "open", "supply_id": "b" * 32})
            self.assertTrue(w.pool_gate({**gate, "action": "open"})["released"])
            self.assertIsNone(worker.override_hit({"project": "p"}))

    def test_ready_supply_does_not_repeat_remote_gate_or_probe(self):
        from labmon.state import save_json
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"LABMON_STATE": d}), patch.object(S.lease, "primary", return_value=True):
            save_json(S.fleet.state_file(S.FILE), {"a": {"daily": True, "stage": "ready", "until": time.time() + 300}})
            with patch.object(S.workerctl, "pool_remote") as remote, patch.object(S, "remote") as probe:
                S.reconcile_daily()
                remote.assert_not_called()
                probe.assert_not_called()

    def test_old_worker_cannot_close_gate(self):
        with tempfile.TemporaryDirectory() as d:
            w = worker_module(Path(d))
            p = w.Paths("a")
            Path(p.dir).mkdir(parents=True)
            Path(p.pid).write_text("123")
            Path(p.status).write_text(json.dumps({"pid": 123, "t": time.time(), "pool_trial_gate": True}))
            with patch.object(w, "alive", return_value=True), self.assertRaisesRegex(ValueError, "升级"):
                w.pool_gate({"host": "a", "supply_id": "a" * 32, "action": "close", "regular": True})
            self.assertIsNone(w.pool_supply_hold("a"))

    def test_expired_gate_stays_held_until_remote_release(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"LABMON_STATE": d}):
            from labmon.state import save_json
            row = {"host": "a", "holder": "A", "daily": True, "until": 1,
                   "devices": ["GPU-fixture"], "supply_id": "a" * 32, "stage": "ready"}
            save_json(S.fleet.state_file(S.FILE), {"a": row})
            self.assertTrue(S.held("a"))
            with patch.object(S, "host_of", return_value=({"id": "a"}, False, 100)), \
                    patch.object(S.workerctl, "pool_remote", side_effect=ConfigError("掉线")):
                S.reconcile_daily()
            self.assertTrue(S.held("a"))
            with patch.object(S, "host_of", return_value=({"id": "a"}, False, 100)), \
                    patch.object(S.workerctl, "pool_remote", return_value={"released": True}):
                S.reconcile_daily()
            self.assertFalse(S.held("a"))


class Execution(unittest.TestCase):
    def test_fixed_child_uses_worker_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            w = worker_module(Path(d))
            device = "GPU-fixture"
            attempt = "a" * 24
            now = time.time()
            old_home, old_local, old_data, old_umask = w.HOME, w.LOCAL, w.DATA_ROOT, os.umask(0o022)
            try:
                gpu = {"i": 0, "uuid": device, "name": "fixture", "total": 40000, "used": 0, "free": 40000, "util": 0}
                real_info = w.host_info
                def host_info(gpus, data):
                    return {**real_info(gpus, data), "cpu_quota": 4,
                            "mem": {"limit": 16, "used": 0, "anon": 0, "oom_kill": 0, "failcnt": 0}}
                child = ("import json, os, sys, time\n"
                         "assert 'LABMON_POOL_TEST_SECRET' not in os.environ\n"
                         "assert os.environ['CUDA_VISIBLE_DEVICES'] == sys.argv[1]\n"
                         "print(json.dumps({'start': time.time(), 'end': time.time(), 'steps': 1, 'device': sys.argv[1]}))\n")
                with patch.object(w, "POOL_PYTHON", sys.executable), patch.object(w, "POOL_SMOKE", child), \
                        patch.object(w, "gpu_table", return_value=[gpu]), patch.object(w, "host_info", side_effect=host_info), \
                        patch.object(w, "started_at", return_value=now), patch.object(w, "pool_gpu_apps", return_value=set()), \
                        patch.dict(os.environ, {"LABMON_POOL_TEST_SECRET": "fixture"}):
                    result = w.pool_trial("a", device, False, "test-a", attempt, now, now, now + 105)
            finally:
                w.HOME, w.LOCAL, w.DATA_ROOT = old_home, old_local, old_data
                os.umask(old_umask)
            self.assertTrue(result["success"], result)
            root = Path(old_local) / "pool-trials" / attempt
            self.assertEqual(result, json.loads((root / "receipt.json").read_text()))
            self.assertFalse(list(root.rglob("current")))
            self.assertEqual(len(list(root.rglob("*.done"))), 1)
            with patch.object(w, "started_at", return_value=now):
                self.assertEqual(w.pool_status("a", attempt, now, now + 105), result)
                with self.assertRaises(FileExistsError):
                    w.pool_trial("a", device, False, "test-a", attempt, now, now, now + 105)

    def test_probe_inspects_overrides_without_consuming_stop_request(self):
        with tempfile.TemporaryDirectory() as d:
            w = worker_module(Path(d))
            worker = w.Worker("a")
            now = time.time()
            self.assertEqual(worker.p.dir, w.Paths("a").dir)
            Path(worker.p.dir).mkdir(parents=True)
            Path(worker.p.pid).write_text("123")
            Path(worker.p.status).write_text(json.dumps({"pid": 123, "t": now, "pool_trial_gate": True}))
            pause = {"kind": "pause", "target": "test-a", "now": True}
            Path(worker.p.overrides).write_text(json.dumps([pause]))
            with patch.object(w, "alive", return_value=True):
                with self.assertRaisesRegex(ValueError, "试点项目已暂停"):
                    w.pool_probe("a", "GPU-fixture", True, "test-a")
            self.assertEqual(json.loads(Path(worker.p.overrides).read_text()), [pause])

    def test_probe_credits_only_confirmed_keepalive_memory(self):
        with tempfile.TemporaryDirectory() as d:
            w = worker_module(Path(d))
            device = "GPU-fixture"
            gpu = {"i": 0, "uuid": device, "name": "fixture", "total": 40000, "used": 39000, "free": 1000, "util": 0}
            info = {"cpu_quota": 4, "mem": {"limit": 16, "anon": 1}}
            apps = [(43, 38000, device)]
            with patch.object(w, "gpu_table", return_value=[gpu]), patch.object(w, "host_info", return_value=info), \
                    patch.object(w, "started_at", return_value=1000), patch.object(w, "POOL_PYTHON", sys.executable), \
                    patch.object(w, "pool_gpu_apps", return_value=apps), patch.object(w, "pool_only_keepalive", return_value=True):
                result = w.pool_probe("a", device, False)
                self.assertEqual(result["cards"][0]["free_mb"], 39000)

    def _trial_with_stopped_keepalive(self, *, foreign=False, restore_error=False, launch_error=None, stale=False, reconcile=False):
        """ka.sh 先报停成功，驱动仍短暂返回上一个保活的 GPU PID。"""
        with tempfile.TemporaryDirectory() as d:
            w = worker_module(Path(d))
            base = Path(d)
            device = "GPU-fixture"
            attempt = "b" * 24
            now = time.time()
            old_home, old_local, old_data, old_umask = w.HOME, w.LOCAL, w.DATA_ROOT, os.umask(0o022)
            gpu = {"i": 0, "uuid": device, "name": "fixture", "total": 40000, "used": 0, "free": 40000, "util": 0}
            original = [(42, 100, device)]
            new = [(84, 100, device)]
            # 前两次用于锁内探测和识别保活；旧驱动条目退出后为空。
            seen = new if foreign else original
            remainder = seen if foreign or stale else []
            polls = iter([original, original, seen, remainder])
            proc = {42: {"ppid": 1, "hpid": 42}}
            real_info, worker_init = w.host_info, w.Worker.__init__
            ka_reg = base / "ka"
            ka_reg.mkdir()
            stopped, restored = [], []

            def init(worker, *args, **kw):
                worker_init(worker, *args, **kw)
                worker.ka_reg = str(ka_reg)

            def host_info(gpus, data):
                return {**real_info(gpus, data), "cpu_quota": 4,
                        "mem": {"limit": 16, "used": 0, "anon": 0, "oom_kill": 0, "failcnt": 0}}

            def gpu_apps():
                return next(polls, remainder)

            def ka_groups(worker):
                return {42: now} if not stopped or restored else None

            def keepalive(worker, need_gpu):
                assert need_gpu
                stopped.append(True)

            def ka(worker, action):
                assert action == "start"
                if restore_error:
                    raise OSError("模拟保活命令失败")
                restored.append(True)
                return type("Result", (), {"returncode": 0})()

            child = ("import json, sys, time\n"
                     "print(json.dumps({'start': time.time(), 'end': time.time(), 'steps': 1, 'device': sys.argv[1]}))\n")
            start = w.Worker.start
            def launch(worker, *args, **kw):
                if launch_error:
                    raise launch_error
                return start(worker, *args, **kw)
            try:
                with patch.object(w, "POOL_DRAIN_S", 0 if stale else w.POOL_DRAIN_S), \
                        patch.object(w.Worker, "start", launch), \
                        patch.object(w, "POOL_PYTHON", sys.executable), patch.object(w, "POOL_SMOKE", child), \
                        patch.object(w, "gpu_table", return_value=[gpu]), patch.object(w, "host_info", side_effect=host_info), \
                        patch.object(w, "started_at", return_value=now), patch.object(w, "pool_gpu_apps", side_effect=gpu_apps), \
                        patch.object(w, "scan_procs", return_value=proc), patch.object(w.Worker, "__init__", init), \
                        patch.object(w.Worker, "ka_groups", ka_groups), patch.object(w.Worker, "keepalive", keepalive), \
                        patch.object(w.Worker, "ka", ka):
                    result = w.pool_trial("a", device, False, "test-a", attempt, now, now, now + 105)
                    if reconcile:
                        self.assertFalse(result["finished"], result)
                        w.HOME, w.LOCAL, w.DATA_ROOT = old_home, old_local, old_data
                        remainder = []
                        with patch.object(w.time, "time", return_value=now + 111):
                            result = w.pool_status("a", attempt, now, now + 105)
            finally:
                w.HOME, w.LOCAL, w.DATA_ROOT = old_home, old_local, old_data
                os.umask(old_umask)
            root = Path(old_local) / "pool-trials" / attempt
            name = "receipt.json" if result["finished"] else "pending.json"
            self.assertEqual(result, json.loads((root / name).read_text()))
            self.assertEqual(stopped, [True])
            return result, restored, bool(list(root.rglob("*.done")))

    def test_stale_keepalive_pid_drains_before_launch(self):
        result, restored, done = self._trial_with_stopped_keepalive()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["keepalive"], "restored")
        self.assertTrue(restored and done)

    def test_new_gpu_pid_refuses_without_starting_and_preserves_reason(self):
        result, restored, done = self._trial_with_stopped_keepalive(foreign=True)
        self.assertFalse(result["finished"], result)
        self.assertFalse(result["success"])
        self.assertIn("出现其他 GPU 进程", result["reason"])
        self.assertEqual(result["keepalive"], "busy")
        self.assertFalse(restored or done)


    def test_status_retries_original_keepalive_after_gpu_becomes_free(self):
        result, restored, done = self._trial_with_stopped_keepalive(foreign=True, reconcile=True)
        self.assertTrue(result["finished"], result)
        self.assertIn("出现其他 GPU 进程", result["reason"])
        self.assertEqual(result["keepalive"], "restored")
        self.assertTrue(restored)
        self.assertFalse(done)

    def test_keepalive_restore_failure_does_not_replace_timeout_reason(self):
        result, restored, done = self._trial_with_stopped_keepalive(stale=True)
        self.assertFalse(result["finished"])
        self.assertIn("未在时限内释放 GPU", result["reason"])
        self.assertEqual(result["keepalive"], "busy")
        self.assertFalse(restored or done)
        result, restored, done = self._trial_with_stopped_keepalive(restore_error=True)
        self.assertFalse(result["finished"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["keepalive"], "failed")
        self.assertFalse(restored)
        self.assertTrue(done and result["cuda"])

    def test_uncertain_shell_start_keeps_pending_reason(self):
        result, restored, done = self._trial_with_stopped_keepalive(launch_error=OSError("模拟 shell 启动边界中断"))
        self.assertFalse(result["finished"])
        self.assertIn("shell 启动边界中断", result["reason"])
        self.assertIsNone(result["keepalive"])
        self.assertFalse(restored or done)
