"""用两个受信执行端替身串起现有登记、试算、排他领取及回执。"""
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier
from unittest.mock import patch

from labmon import fleet, pool_dispatch as D, poolqueues as Q, projects, users, waits
from labmon.config import ConfigError
from labmon.state import save_json, state_dir
from tests.fixtures import make_token


class FakeNodes:
    def __init__(self):
        self.generation = 1000
        self.started = {}
        self.results = {}
        self.barrier = None
        self.cards_wanted = 1
        self.edited = set()

    def inventory(self):
        now = time.time()
        return [dict(node=node, physical_host=node, model="fixture", generation=self.generation, state="ready",
                     cpu_cores=4, ram_gib=12, ram_limit_gib=16, observed_at=now, deadline=now + 900,
                     cards=[dict(uuid=f"GPU-{node}", vram_mb=40000, free_mb=40000, idle=True)]) for node in ("a", "b")]

    def head(self, row):
        if any(row["id"] == q and self.results.get(a, {}).get("phase") == "done" for a, (q, _) in self.started.items()):
            return {"fingerprint": row["fingerprint"], "task_name": None}
        return {"task_name": "train", "fingerprint": ["modified", None] if row["id"] in self.edited else row["fingerprint"],
                "eligible_nodes": ["a", "b"], "gpus": self.cards_wanted, "vram_gib": 2, "ram_gib": 2, "cpu_cores": 1, "duration_min": 1}

    def claim(self, row, attempt):
        return Q.claim(row["id"], attempt["id"], expected=row, dispatch=attempt)

    def launch(self, row, attempt):
        self.started[attempt["id"]] = (row["id"], attempt["node"])
        if self.barrier is not None:
            self.barrier.wait(timeout=4)
        return self.status(row, attempt)

    def status(self, row, attempt):
        if attempt["id"] not in self.started:
            raise ConfigError("尚未收到执行端回执")
        return self.results.get(attempt["id"], {"phase": "running", "generation": self.generation})


class Dispatch(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.dict(os.environ, {"LABMON_STATE": tmp}))
        stack.enter_context(patch.object(D.lease, "primary", return_value=True))
        stack.enter_context(patch.object(Q, "route_remote", return_value={"released": True, "claimed": True}))
        stack.enter_context(patch.object(Q.poolview, "personal_servers", return_value=([{"id": "fixture"}], None)))
        save_json(state_dir() / "pool-shadow.json", {"members": ["alice", "bob"]})
        self.nodes = FakeNodes()
        self.who = {}
        self.rows = {}
        for account in ("alice", "bob"):
            make_token(account, role="user", scopes=("project",))
            pid = "test-" + account
            projects.put_config(pid, f'id="{pid}"\n', owner=account)
            self.who[account] = users.Ident(account, "user", frozenset({"project"}), "pool", "")
            root = f"/data/projects/{pid}"
            ws = dict(root=root, canonical=root, host="a", account=account)
            row = self.rows[account] = dict(ws, id=account[0]*32, project=pid, jobs="pilot.txt",
                                           fingerprint=["0"*64, None], state="queued", t=time.time(), request_id=account)

            def put(data):
                data.setdefault("workspaces", {})[pid] = ws
                data.setdefault("queues", {})[account] = row
            fleet.edit_state(Q.FILE, put)

    def test_claim_imports_existing_completion_before_opening_old_queue(self):
        row = self.rows["alice"]
        with patch.object(Q, "route_remote", return_value={"claimed": True, "prior_finished": {"train": "done"}}):
            with self.assertRaisesRegex(users.Denied, "已有完成标记"):
                Q.claim(row["id"], "new-attempt", dispatch={"task_name": "train", "devices": ["GPU-a"]})
        saved = Q.find(Q.read(), row["id"])
        self.assertEqual(saved["state"], "queued")
        self.assertEqual(saved["prior_finished"], {"train": "done"})
        self.assertNotIn("attempt_id", saved)

    def test_personal_server_loss_blocks_claim_before_remote_handoff(self):
        with patch.object(Q.poolview, "personal_servers", return_value=([], "尚无个人服务器")), patch.object(Q, "route_remote") as remote:
            with self.assertRaisesRegex(users.Denied, "个人服务器"):
                Q.claim(self.rows["alice"]["id"], "new-attempt")
            remote.assert_not_called()

    def test_admin_can_release_queue_and_owner_can_stop_after_losing_resource(self):
        admin = users.Ident("boss", "admin", frozenset({"project"}), "token", "")
        released = Q.withdraw(admin, self.rows["bob"]["id"])
        self.assertEqual(released["state"], "withdrawn")
        attempt = {"id": "new-attempt", "phase": "running", "holder": "Alice", "task_name": "train", "devices": ["GPU-a"]}
        Q.claim(self.rows["alice"]["id"], attempt["id"], dispatch=attempt)
        with patch.object(Q.poolview, "personal_servers", return_value=([], "尚无个人服务器")):
            stopped = D.stop(self.who["alice"], self.rows["alice"]["id"], attempt["id"])
        self.assertEqual(stopped["dispatch"]["phase"], "stopping")

    def test_confirmed_queue_claim_does_not_repeat_remote_handoff(self):
        row = self.rows["alice"]
        fleet.edit_state(Q.FILE, lambda data: Q.find(data, row["id"]).update(prior_finished={}))
        with patch.object(Q, "route_remote") as remote:
            Q.claim(row["id"], "new-attempt")
            remote.assert_not_called()

    def dispatch(self, account):
        listing = Q.listing(self.who[account])
        self.assertFalse(listing["dispatch_enabled"])
        return listing["queues"][0].get("dispatch")

    def test_two_nodes_parallel_then_wait_and_reconcile_without_relaunch(self):
        D.tick(self.nodes)
        self.assertFalse(self.nodes.started)
        self.nodes.barrier = Barrier(2)
        D.tick(self.nodes, enabled=True)
        self.assertEqual({node for _, node in self.nodes.started.values()}, {"a", "b"})
        self.assertEqual(len(self.nodes.started), 2)
        for person in self.who:
            dispatch = self.dispatch(person)
            self.assertEqual(dispatch["phase"], "running")
            self.assertEqual(dispatch["wait_job"], f"test-{person}/train")
            self.assertNotIn("devices", dispatch)
        wait = waits.JobWatch(projects.load_cfg("test-alice"), projects.out_dir("test-alice"), ["test-alice/train"])
        self.assertEqual(wait.check()[("test-alice", "train")]["st"], "running")
        for attempt_id in self.nodes.started:
            self.nodes.results[attempt_id] = {"phase": "done", "generation": 1000, "cleaned": True}
        D.tick(self.nodes)
        self.assertEqual(self.dispatch("alice")["phase"], "done")
        self.assertEqual(self.dispatch("bob")["phase"], "done")
        self.assertTrue(wait.check()[("test-alice", "train")]["final"])
        D.tick(self.nodes, enabled=True)
        self.assertEqual(len(self.nodes.started), 2)

    def test_paused_dispatch_refreshes_new_supply_and_finished_assignments(self):
        from labmon import pool_supply
        save_json(fleet.state_file(pool_supply.FILE), {
            n: dict(host=n, daily=True, until=time.time()+900, devices=['GPU-'+n]) for n in ('a', 'b')})
        backend = D.Backend()
        with patch.object(pool_supply, 'reconcile_daily'), \
                patch.object(backend, 'inventory', side_effect=self.nodes.inventory), \
                patch.object(backend, 'head') as head, patch.object(backend, 'claim') as claim, \
                patch.object(backend, 'status', return_value={'phase': 'done', 'generation': 1000, 'cleaned': True}):
            D.tick(backend, enabled=False)
            self.assertEqual(D.live_view()['capacity']['free'], 2)
            self.assertEqual({n['node'] for n in D.live_view()['nodes']}, {'a', 'b'})
            D.tick(self.nodes, enabled=True)
            D.tick(backend, enabled=False)
            self.assertEqual(D.live_view()['capacity']['free'], 2)
            self.assertTrue(all(n['state'] == 'ready' for n in D.live_view()['nodes']))
            self.assertEqual(self.dispatch('alice')['phase'], 'done')
            head.assert_not_called()
            claim.assert_not_called()

    def test_same_node_and_changed_content(self):
        self.nodes.cards_wanted = 2
        self.nodes.edited.add(self.rows["bob"]["id"])
        D.tick(self.nodes, enabled=True)
        self.assertFalse(self.nodes.started)
        self.assertEqual(self.dispatch("alice")["wait_reason"]["code"], "same_node")
        self.assertEqual(self.dispatch("bob")["wait_reason"]["code"], "ineligible")

    def test_withdrawn_queue_keeps_history_without_stale_wait_projection(self):
        self.nodes.cards_wanted = 2
        D.tick(self.nodes, enabled=True)
        before = self.dispatch("alice")
        self.assertEqual((before["phase"], before["wait_reason"]["code"]), ("waiting", "same_node"))
        self.assertEqual(before["wait_job"], "test-alice/train")

        who, identity = self.who["alice"], self.rows["alice"]["id"]
        result = Q.withdraw(who, identity)
        self.assertEqual((result["id"], result["state"], result["can_withdraw"]), (identity, "withdrawn", False))
        self.assertNotIn("dispatch", result)
        self.assertEqual(Q.withdraw(who, identity), result)
        self.assertEqual(Q.listing(who)["queues"], [result])
        self.assertEqual(Q.find(Q.read(), identity)["dispatch"]["phase"], "waiting")
        self.assertEqual(self.dispatch("bob")["phase"], "waiting")

    def test_stale_cleanup_keeps_claim_and_second_claim_conflicts(self):
        D.tick(self.nodes, enabled=True)
        a = Q.read()["queues"]["alice"]["dispatch"]
        self.nodes.results[a["id"]] = {"phase": "done", "generation": 1001, "cleaned": True}
        D.tick(self.nodes, enabled=True)
        self.assertEqual(self.dispatch("alice")["phase"], "unknown")
        self.assertEqual(len(self.nodes.started), 2)
        with self.assertRaises(users.Denied):
            Q.withdraw(self.who["alice"], self.rows["alice"]["id"])
        self.nodes.results[a["id"]] = {"phase": "failed", "generation": 1000, "cleaned": True}
        D.tick(self.nodes)
        self.assertEqual(self.dispatch("alice")["phase"], "failed")

    def test_verified_head_uses_worker_preparation_without_remote_read(self):
        row, now = self.rows["alice"], time.time()
        request = dict(queue_id=row["id"], project=row["project"], root=row["root"], jobs=row["jobs"],
                       ready=True, task_name="train", gpus=2, vram_gib=2, ram_gib=4, cpu_cores=4, duration_min=10)
        board = {"generated": now, "hosts": [{"id": "a", "clock": now, "status": {
            "t": now, "pool_ready_protocol": 1, "pool_requests": [request]}}]}
        with patch.object(D, "snapshot", return_value=board), patch.object(Q, "inspect") as remote:
            head = D.verified_head(row)
            self.assertEqual((head["task_name"], head["gpus"], head["cpu_cores"]), ("train", 2, 4))
            self.assertNotIn("cmd", head)
            request.update(ready=False, reason="等前置任务跑完")
            self.assertEqual(D.verified_head(row)["wait_reason"]["text"], "等前置任务跑完")
            request.update(ready=True)
            row["dispatch"] = {"ended_at": now + 1}
            self.assertIn("更新任务结果", D.verified_head(row)["wait_reason"]["text"])
            board["generated"] = now - 10000
            self.assertEqual(D.verified_head(row)["wait_reason"]["code"], "offline")
            remote.assert_not_called()

    def test_wait_rounds_keep_history_barrier_until_source_observes_finish(self):
        row, now = self.rows["alice"], time.time() - 5
        request = dict(queue_id=row["id"], project=row["project"], root=row["root"], jobs=row["jobs"],
                       ready=True, task_name="train", gpus=1, vram_gib=2, ram_gib=2, cpu_cores=1, duration_min=1)
        board = {"generated": now, "hosts": [{"id": "a", "clock": now, "status": {
            "t": now, "pool_ready_protocol": 1, "pool_requests": [request]}}]}
        terminal = {"id": "finished", "phase": "done", "ended_at": now + 1, "node": "a", "devices": ["GPU-a"],
                    "cpu_cores": 1, "ram_gib": 2, "reserved_at": now - 60, "started_at": now - 60}
        fleet.edit_state(Q.FILE, lambda data: Q.find(data, row["id"]).update(dispatch=terminal, history={"finished": terminal}))
        def head(q):
            result = D.verified_head(q)
            return dict(result, eligible_nodes=["a", "b"])
        with patch.object(D, "snapshot", return_value=board), patch.object(self.nodes, "head", side_effect=head):
            for _ in range(3):
                D.tick(self.nodes, enabled=True)
            self.assertFalse(self.nodes.started)
            self.assertEqual(Q.find(Q.read(), row["id"])["state"], "queued")
            self.assertIn("更新任务结果", self.dispatch("alice")["wait_reason"]["text"])
            board["generated"] = now + 2
            request["task_name"] = "next"
            D.tick(self.nodes, enabled=True)
        self.assertEqual(len(self.nodes.started), 1)
        self.assertEqual(self.dispatch("alice")["task_name"], "next")

    def test_worker_finished_failure_leaves_next_item_to_source_worker(self):
        row = self.rows["alice"]
        attempt = dict(id="new-attempt", phase="running", task_name="train", devices=["GPU-a"], generation=7, reserved_at=time.time())
        Q.claim(row["id"], attempt["id"], dispatch=attempt)
        Q.dispatch_receipt(row["id"], attempt["id"], {"phase": "failed", "generation": 7, "cleaned": True, "task_finished": True})
        self.assertEqual(Q.find(Q.read(), row["id"])["state"], "queued")

    def test_migration_retries_partial_handoff_and_refuses_old_inflight(self):
        calls = []
        def remote(row, action):
            calls.append(row["id"])
            if row["account"] == "bob":
                raise users.Denied(409, "模拟连接中断")
            return {"claimed": True, "prior_finished": {"old": "done"}}
        with patch.object(Q, "route_remote", side_effect=remote):
            with self.assertRaisesRegex(users.Denied, "中断"):
                Q.migrate_routes()
        self.assertEqual(Q.find(Q.read(), self.rows["alice"]["id"])["route_protocol"], 1)
        self.assertEqual(Q.find(Q.read(), self.rows["bob"]["id"])["state"], "preparing")
        with patch.object(Q, "route_remote", return_value={"claimed": True}) as retry:
            Q.migrate_routes()
            self.assertEqual(retry.call_count, 1)
            Q.migrate_routes()
            self.assertEqual(retry.call_count, 1)
        def old(data):
            row = Q.find(data, self.rows["bob"]["id"])
            row.pop("route_protocol")
            row["dispatch"] = {"phase": "unknown"}
        fleet.edit_state(Q.FILE, old)
        with patch.object(Q, "route_remote") as remote:
            with self.assertRaisesRegex(users.Denied, "在途"):
                Q.migrate_routes()
            remote.assert_not_called()

    def test_source_queue_in_project_snapshot_is_not_a_duplicate_running_task(self):
        row = self.rows["alice"]
        save_json(projects.out_dir(row["project"]) / "snapshot.json", {"jobs": [{"id": "train", "status": "queued"}]})
        D.tick(self.nodes, enabled=True)
        self.assertTrue(self.nodes.started)

    def test_inventory_uses_collected_supply_and_same_clock_deadline_check(self):
        now = time.time()
        node = dict(self.nodes.inventory()[0], clock=now)
        supply = {"host": "a", "daily": True, "stage": "ready", "until": now + 800, "generation": 1000}
        save_json(fleet.state_file(D.pool_supply.FILE), {"a": supply})
        board = {"pool_observed": {"a": {"node": node, "observed": now, "lease_end": now + 700}}}
        with patch.object(D, "snapshot", return_value=board), patch.object(D.pool_supply, "probe") as remote:
            rows = D.Backend().inventory()
            self.assertEqual(rows[0]["deadline"], now + 700)
            self.assertEqual(rows[0]["node"], "a")
            node["clock"] = now - 1000
            self.assertEqual(D.Backend().inventory(), [])
            remote.assert_not_called()

    def test_withdraw_and_claim_one_wins(self):
        row = self.rows["alice"]
        barrier = Barrier(2)

        def race(act):
            barrier.wait(timeout=4)
            try:
                act()
            except users.Denied:
                return False
            return True

        with ThreadPoolExecutor(2) as workers:
            claims = workers.submit(race, lambda: Q.claim(row["id"], "attempt", expected=row))
            withdrawals = workers.submit(race, lambda: Q.withdraw(self.who["alice"], row["id"]))
            self.assertNotEqual(claims.result(), withdrawals.result())

    def test_member_can_stop_only_own_current_attempt(self):
        D.tick(self.nodes, enabled=True)
        def enable_stop(data):
            for row in data["queues"].values():
                row["dispatch"]["holder"] = "授权人"
        fleet.edit_state(Q.FILE, enable_stop)
        own = self.rows["alice"]["id"]
        attempt_id = self.dispatch("alice")["attempt_id"]
        with self.assertRaises(users.Denied):
            D.stop(self.who["bob"], own, attempt_id)
        with self.assertRaises(users.Denied):
            D.stop(self.who["alice"], own, "0"*32)
        self.assertTrue(self.dispatch("alice")["can_stop"])
        self.assertEqual(D.stop(self.who["alice"], own, attempt_id)["dispatch"]["phase"], "stopping")
        attempt = Q.find(Q.read(), own)["dispatch"]
        Q.dispatch_receipt(own, attempt["id"], {"phase": "running", "generation": 1000})
        self.assertEqual(self.dispatch("alice")["phase"], "stopping")

    def test_bounded_remote_clock_and_new_regular_job_take_precedence(self):
        with patch.object(self.nodes, "launch", return_value={"phase": "starting", "generation": 1000}):
            D.tick(self.nodes, enabled=True)
        row = Q.find(Q.read(), self.rows["alice"]["id"])
        a = row["dispatch"]
        result = {"phase": "done", "generation": 1000, "cleaned": True, "started_at": a["reserved_at"] - 4}
        Q.dispatch_receipt(row["id"], a["id"], result)
        attempt = D.account_attempt(row, Q.find(Q.read(), row["id"])["history"][a["id"]])
        D.pool.attempts_of([attempt], time.time())
        D.tick(self.nodes, enabled=True)
        done = Q.find(Q.read(), row["id"])["history"][a["id"]]
        self.assertEqual(done["started_at"], a["reserved_at"])
        wait = waits.JobWatch(projects.load_cfg("test-alice"), projects.out_dir("test-alice"), ["test-alice/train"])
        with patch.object(wait, "_snap", return_value=({"jobs": [{"id": "train", "status": "running", "lane": "one"}]}, None)):
            self.assertEqual(wait.check()[("test-alice", "train")]["st"], "running")

    def test_priority_without_expiry(self):
        nodes = self.nodes.inventory()[:1]
        with patch.object(self.nodes, "inventory", return_value=nodes), patch.object(
            D.config, "sched", return_value={"focus": {"test-bob": None}}
        ):
            D.tick(self.nodes, enabled=True)
        self.assertEqual({row for row, _ in self.nodes.started.values()}, {self.rows["bob"]["id"]})

    def test_verified_execution_node_takes_two_cards_without_claiming_physical_identity(self):
        nodes = self.nodes.inventory()[:1]
        nodes[0].update(physical_host=None, same_node_verified=True)
        nodes[0]["cards"].append(dict(uuid="GPU-second", vram_mb=40000, free_mb=40000, idle=True))
        self.nodes.cards_wanted = 2
        with patch.object(self.nodes, "inventory", return_value=nodes):
            D.tick(self.nodes, enabled=True)
        self.assertEqual(len(self.nodes.started), 1)
        active = next(row["dispatch"] for row in Q.read()["queues"].values() if row["state"] == "claimed")
        self.assertEqual(set(active["devices"]), {"GPU-a", "GPU-second"})



class RemoteDispatch(unittest.TestCase):
    def test_changed_fingerprint_does_not_start_any_process(self):
        from pathlib import Path
        from labmon import labmon_worker as worker

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            base = Path(tmp)
            root = base / "projects" / "example"
            root.mkdir(parents=True)
            (root / "jobs.txt").write_text("train|true # est_min=2\n")
            path = base / "attempt"
            path.mkdir()
            stack.enter_context(patch.object(worker, "HOME", str(base)))
            stack.enter_context(patch.object(worker, "LOCAL", str(base)))
            stack.enter_context(patch.object(worker, "DATA_ROOT", str(base)))
            stack.enter_context(patch.dict(os.environ, {"LABMON_DATA_ROOT": str(base)}))
            stack.enter_context(patch.object(worker, "Worker"))
            stack.enter_context(patch.object(worker, "pool_gpu_apps", return_value=[]))
            stack.enter_context(patch.object(worker, "pool_probe", return_value={"generation": 7}))
            args = dict(id="1"*32, node="a", root=str(root.resolve()), jobs="jobs.txt", task_name="train", regular=True,
                        project="p", generation=7, start_before=time.time() + 25, until=time.time() + 30, devices=["GPU-a"], fingerprint=["0"*64, None])
            worker.pool_daily_launch(str(path), args, None)
            self.assertEqual(worker.json.loads((path / "receipt.json").read_text())["phase"], "failed")
            self.assertFalse((path / "child").exists())
            self.assertFalse(worker.Worker.return_value.start.called)

    def test_dispatch_uses_original_environment_and_queue_state(self):
        self.run_original()

    def test_preparation_stop_and_supply_expiry_never_start_child(self):
        for mode in ("stop", "stop-wait", "expire"):
            with self.subTest(mode=mode):
                self.run_original(mode)

    def test_same_node_source_observes_pool_child_without_second_finish(self):
        self.run_original("same-node")

    def test_dispatch_reuses_original_learned_duration(self):
        self.run_original("learned")

    def test_pause_arriving_during_preparation_waits_for_resume(self):
        self.run_original("pause-resume")

    def test_supervisor_exit_before_finish_recovers_real_child_result(self):
        self.run_original("supervisor-exit")

    def test_supervisor_recovery_finishes_original_cross_node_queue_once(self):
        from pathlib import Path
        from labmon import labmon_worker as worker
        for mode in ("done", "failed", "interrupted", "stopped", "wrong-identity", "alive", "changed-env", "bad-current"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                base = Path(tmp).resolve(); root = base / "projects/p"; root.mkdir(parents=True)
                (root / "jobs.txt").write_text("train|true\nnext|true # after=train\n")
                for name in ("HOME", "LOCAL", "DATA_ROOT"):
                    stack.enter_context(patch.object(worker, name, str(base)))
                stack.enter_context(patch.object(worker, "pool_gpu_apps", return_value=[]))
                stack.enter_context(patch.object(worker, "gpu_table", return_value=[]))
                stack.enter_context(patch.object(worker, "alive", return_value=False))
                stack.enter_context(patch.object(worker, "pool_group_alive", return_value=mode == "alive"))
                source = Path(worker.Paths("source").dir); source.mkdir(parents=True)
                (source / "status.json").write_text('{"pool_ready_protocol":1}')
                worker.pool_queue_route({"root": str(root), "jobs": "jobs.txt", "project": "p", "queue_id": "a"*32,
                                         "action": "claim", "source_host": "source"})
                queue = worker.registry(str(source / "queues"))[0][0]
                state = Path(queue["state"]); state.mkdir(parents=True)
                started = time.time() - 20
                (state / "current").write_text("train 999999 %s target" % worker.now_str(started))
                if mode != "interrupted":
                    (state / "train.rc").write_text("1" if mode == "failed" else "0")
                path = base / "attempt"; path.mkdir(); (path / "child").write_text("999999")
                attempt = dict(id="1"*32, node="target", root=str(root), jobs="jobs.txt", project="p", task_name="train",
                               queue_id="a"*32, generation=7, start_before=time.time()-1, until=time.time()+100, devices=["GPU-a"])
                marker = Path(worker.pool_registration(str(root), "jobs.txt")); marker.write_text("other" if mode == "wrong-identity" else attempt["id"])
                worker.pool_daily_receipt(str(path), attempt, "running", started_at=started, queue_state=queue["state"], queue_logs=queue["logs"])
                if mode == "changed-env":
                    (root / "jobs.env").write_text("QUEUE_STATE=results/new-state\n")
                if mode == "bad-current":
                    (state / "current").write_text("broken")
                if mode == "stopped":
                    (path / "stop").write_text(attempt["id"])
                # 使用实际批次采集生成的载荷，覆盖观测与即时控制身份一致。
                row = dict(id="a"*32, root=str(root), jobs="jobs.txt", project="p", dispatch=dict(attempt, phase="running"))
                for key in ("queue_id", "root", "jobs", "project"):
                    row["dispatch"].pop(key)
                with patch.object(D, "load_json", return_value={"target": {"daily": True, "host": "target", "holder": "Alice", "devices": ["GPU-a"]}}), \
                     patch.object(Q, "read", return_value={"queues": {row["id"]: row}}), \
                     patch.object(D.pool_supply, "host_of", return_value=({"id": "target"}, True, time.time()+100)):
                    observed = D.observation_hosts()["target"]["pool_observe"]["attempts"][0]
                self.assertEqual(observed, D.attempt_spec(row, row["dispatch"], True))
                result = worker.pool_daily_status(str(path), observed)
                if mode in ("wrong-identity", "alive", "bad-current"):
                    self.assertEqual(result["phase"], "unknown")
                    self.assertTrue((state / "current").exists())
                    self.assertTrue(marker.exists())
                    continue
                self.assertTrue(result["cleaned"])
                self.assertEqual(result["phase"], "done" if mode in ("done", "changed-env") else "stopped" if mode == "stopped" else "failed")
                self.assertFalse((state / "current").exists())
                self.assertFalse((state / "train.rc").exists())
                self.assertEqual((state / "train.done").exists(), mode in ("done", "changed-env"))
                self.assertEqual((state / "train.failed").exists(), mode == "failed")
                self.assertEqual(result["task_finished"], mode in ("done", "changed-env", "failed"))
                self.assertFalse(marker.exists())
                self.assertEqual(result["requeued"], mode == "interrupted")
                self.assert_receipt_reaches_project(attempt, result, "completed" if mode == "stopped" else "queued")
                self.assertEqual(worker.pool_daily_status(str(path), attempt), result)
                history = [r for r in worker.load_hist({}) or [] if r["host"] == "target"]
                self.assertEqual(len(history), 0 if mode == "interrupted" else 1)
                if mode == "done":
                    self.assertEqual(worker.next_job(queue)["name"], "next")
                worker.pool_queue_route({"root": str(root), "jobs": "jobs.txt", "project": "p", "queue_id": "a"*32, "action": "release"})

    def assert_receipt_reaches_project(self, attempt, result, expected):
        # 用真实执行端回执进入账本及项目读模型，避免只验证本地标记。
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LABMON_STATE=tmp):
            row = dict(id=attempt["queue_id"], project=attempt["project"], jobs=attempt["jobs"], root=attempt["root"],
                       account="alice", state="claimed", t=time.time(),
                       dispatch=dict(attempt, phase="running", reserved_at=time.time() - 60))
            save_json(Q.state_file(), {"queues": {row["id"]: row}})
            Q.dispatch_receipt(row["id"], attempt["id"], result)
            Q.dispatch_receipt(row["id"], attempt["id"], result)
            saved = Q.find(Q.read(), row["id"])
            self.assertEqual(saved["state"], expected)
            self.assertEqual(len(saved["history"]), 1)
            viewed = Q.project_view(row["project"])["attempts"][0]
            self.assertEqual(viewed["log_tail"], result["log_tail"])
            if result.get("requeued"):
                self.assertTrue(viewed["requeued"])
                with patch.object(projects, "owner", return_value="alice"):
                    self.assertEqual(Q.wait_status(row["project"], attempt["task_name"], Q.read())["st"], "queued")

    def run_original(self, mode="run"):
        from pathlib import Path
        from labmon import labmon_worker as worker

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            base = Path(tmp).resolve()
            root = base / "projects" / "example"
            root.mkdir(parents=True)
            (root / "jobs.txt").write_text("train|echo visible-training-log; printf '%s' \"$FLAG:$CUDA_VISIBLE_DEVICES\" > proof; "
                                          "printf '%s\\n' \"$LABMON_CACHE\" \"$HOME\" \"${LABMON_TEST_PRIVATE-unset}\" > paths # est_min=2\n")
            (root / "jobs.env").write_text("FLAG=checked\nCUDA_VISIBLE_DEVICES=unexpected\n")
            if mode == "learned":
                (root / "jobs.txt").write_text((root / "jobs.txt").read_text().replace(" # est_min=2", ""))
                prior = base / "hosts/previous/usage.jsonl"
                prior.parent.mkdir(parents=True)
                prior.write_text(worker.json.dumps({"project": "p", "name": "train", "min": 2, "rc": "0", "t1": 1, "conc": 1}) + "\n")
            path = base / "attempt"
            path.mkdir()
            for name in ("HOME", "LOCAL", "DATA_ROOT"):
                stack.enter_context(patch.object(worker, name, str(base)))
            stack.enter_context(patch.dict(os.environ, {"LABMON_DATA_ROOT": str(base)}))
            stack.enter_context(patch.dict(os.environ, {"LABMON_TEST_PRIVATE": "must-not-leak"}))
            stack.enter_context(patch.object(worker.Worker, "sample"))
            stack.enter_context(patch.object(worker.Worker, "snapshot", return_value={"code": None}))
            stack.enter_context(patch.object(worker, "pool_gpu_apps", return_value=[]))
            stack.enter_context(patch.object(worker, "pool_probe", return_value={"generation": 7}))
            stack.enter_context(patch.object(worker, "started_at", return_value=7))
            place_calls = []
            def placement(*args, **kwargs):
                place_calls.append(1)
                if mode == "pause-resume":
                    # Worker.place 使用执行目录，但指令沿原目标主机目录送达。
                    sched = base / "hosts/a/sched.json"
                    if len(place_calls) == 1:
                        sched.write_text('{"pause":{"p":0}}')
                        return None, "等待准备", False
                    if len(place_calls) == 3:
                        self.assertFalse((root / "proof").exists())
                        sched.write_text('{}')
                return (None, "等待准备", False) if mode == "stop-wait" else ([{"i": 0, "uuid": "GPU-a"}], None, False)
            stack.enter_context(patch.object(worker, "place", side_effect=placement))
            observer = worker.Worker("a")
            original_start = worker.Worker.start
            def swap_file(w, *args, **kwargs):
                if mode == "learned":
                    self.assertEqual(args[1]["dur"], 2)
                (root / "jobs.env").write_text("FLAG=changed\n")
                child = original_start(w, *args, **kwargs)
                return child
            stack.enter_context(patch.object(worker.Worker, "start", swap_file))
            original_receipt = worker.pool_daily_receipt
            def receipt(path, attempt, phase, **kwargs):
                if mode == "same-node" and phase == "running":
                    with patch.object(worker, "HOME", str(base)), patch.object(worker, "LOCAL", str(base)):
                        waiting, reqs = observer.requests([source_queue], False)
                    self.assertEqual(observer.running, {})
                    self.assertEqual(reqs, [])
                    self.assertIn("算力池", waiting[0]["why"])
                return original_receipt(path, attempt, phase, **kwargs)
            stack.enter_context(patch.object(worker, "pool_daily_receipt", side_effect=receipt))
            _, digest, _ = worker.pool_checked_files(str(root.parent), str(root), "jobs.txt")
            args = dict(id="1"*32, node="a", root=str(root), jobs="jobs.txt", task_name="train", regular=True,
                        project="p", generation=7, queue_id="a"*32, start_before=time.time() + 25, until=time.time() + 180, devices=["GPU-a"], fingerprint=digest)
            if mode == "expire":
                args["until"] = time.time() - 1
            source_host = "a" if mode == "same-node" else "source"
            source = Path(worker.Paths(source_host).dir)
            source.mkdir(parents=True)
            (source / "status.json").write_text('{"pool_ready_protocol":1}')
            worker.pool_queue_route({"root": str(root), "jobs": "jobs.txt", "project": "p", "queue_id": "a"*32,
                                     "action": "claim", "source_host": source_host})
            source_queue = worker.registry(str(source / "queues"))[0][0]
            # 已接单持锁后，原 worker 的准备可以跨过45秒接单窗口。
            def prepared(*_):
                args["start_before"] = time.time() - 1
                if mode.startswith("stop"):
                    (path / "stop").write_text(args["id"])
            with patch.object(worker.Worker, "sample", side_effect=prepared), worker.pool_device_lock() as lock:
                if mode == "supervisor-exit":
                    with patch.object(worker.Worker, "finish", side_effect=SystemExit("模拟监督进程退出")), self.assertRaises(SystemExit):
                        worker.pool_daily_launch(str(path), args, lock)
                else:
                    worker.pool_daily_launch(str(path), args, lock)
            if mode == "supervisor-exit":
                self.assertTrue(Path(source_queue["state"], "current").exists())
                recovered = worker.pool_daily_status(str(path), args)
                self.assertEqual(recovered["phase"], "done")
                self.assertTrue(recovered["cleaned"])
                self.assertEqual(worker.pool_daily_status(str(path), args), recovered)
            if mode == "pause-resume":
                self.assertGreaterEqual(len(place_calls), 3)
            if mode not in ("run", "learned", "same-node", "supervisor-exit", "pause-resume"):
                self.assertFalse((root / "proof").exists())
                receipt = worker.json.loads((path / "receipt.json").read_text())
                self.assertEqual(receipt["phase"], "stopped" if mode.startswith("stop") else "failed")
                self.assertTrue(receipt["cleaned"])
                self.assertFalse(receipt["task_finished"])
                self.assertFalse((path / "child").exists())
                return
            self.assertTrue((root / "proof").exists(), worker.read(str(path / "dispatch.log")))
            self.assertEqual((root / "proof").read_text(), "changed:GPU-a")
            self.assertTrue((base / "cache/p").is_dir())
            self.assertFalse((path / "cache/p").exists())
            self.assertEqual((root / "paths").read_text().splitlines(), [str(base / "cache/p"), str(path), "unset"])
            self.assertTrue((root / "results/_queue/train.done").exists())
            if mode == "same-node":
                observer.requests([source_queue], False)
            self.assertFalse((root / "results/_queue/train.failed").exists())
            self.assertTrue((root / "results/_logs/train.log").exists())
            actual_receipt = worker.json.loads((path / "receipt.json").read_text())
            self.assertEqual(actual_receipt["phase"], "done")
            self.assertIn("visible-training-log", "\n".join(actual_receipt["log_tail"]))
            self.assert_receipt_reaches_project(args, actual_receipt, "queued")
            self.assertFalse(Path(worker.pool_registration(str(root), "jobs.txt")).exists())
            history = [h for h in worker.load_hist({}) if h["host"] == "a"]
            self.assertEqual([(h["host"], h["project"], h["name"], h["rc"]) for h in history], [("a", "p", "train", "0")])
            self.assertFalse((path / "hosts/a/usage.jsonl").exists())
