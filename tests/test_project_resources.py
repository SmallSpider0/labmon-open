"""项目资源归属、幂等提交与共享清单通道交接。"""
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from labmon import project_resources as R, poolqueues as Q, users, projects
from tests.fixtures import worker_module


class Resources(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.dict(os.environ, {"LABMON_STATE": tmp}))
        self.owner = users.Ident("alice", "user", frozenset({"project"}), "token", "")
        self.admin = users.Ident("admin", "admin", frozenset({"project"}), "token", "")
        stack.enter_context(patch.object(R.projects, "project_ids", return_value=["p"]))
        stack.enter_context(patch.object(R.projects, "owner", return_value="alice"))
        stack.enter_context(patch.object(Q, "pool_identity", return_value=self.owner))
        stack.enter_context(patch.object(R.poolview.fleet, "conf", return_value={}))
        stack.enter_context(patch.object(R.poolview.fleet, "account_person", return_value=("Alice", None)))
        self.hosts = [{"id": "mine", "platform_holder": "Alice", "worker_home": "/shared/worker", "data_root": "/shared", "ssh": "gpu.example", "users": ["alice"]},
                      {"id": "other", "platform_holder": "Bob"}, {"id": "borrowed", "platform_holder": "Alice", "borrow": {"until": time.time()+100}}]
        stack.enter_context(patch.object(R.config, "active_hosts", return_value=self.hosts))
        stack.enter_context(patch.object(users, "user_hosts", return_value=self.hosts))
        self.held = stack.enter_context(patch.object(R.pool_supply, "held", return_value=False))
        stack.enter_context(patch.object(Q, "inspect", return_value=("/shared/projects/p", ["a"*64, None])))
        self.run = stack.enter_context(patch.object(R.workerctl, "run_out", return_value=(0, "已登记")))

    def test_admin_uses_project_owner_resources_not_own_access(self):
        rows, error = R.private_hosts(self.owner)
        self.assertIsNone(error)
        self.assertEqual([h["id"] for h in rows], ["mine"])
        with self.assertRaisesRegex(users.Denied, "不是项目主人"):
            R.submit(self.admin, "p", {"destination": "other", "jobs": "jobs.txt", "request_id": "same", "root": "/shared/projects/p"})
        self.run.assert_not_called()

    def test_supply_blocks_private_and_retry_is_idempotent(self):
        data = {"destination": "mine", "jobs": "jobs.txt", "request_id": "same", "root": "/shared/projects/p"}
        self.held.return_value = True
        with self.assertRaisesRegex(users.Denied, "供给算力池"):
            R.submit(self.admin, "p", data)
        self.held.return_value = False
        first = R.submit(self.admin, "p", data)
        self.assertEqual(R.submit(self.admin, "p", data), first)
        self.assertEqual(self.run.call_count, 2)  # 专属登记沿用 worker add 的 root/jobs 幂等，不另建账本。
        self.assertEqual(Q.read().get("routes", {}), {})

    def test_private_submission_keeps_host_permissions_and_local_boundary(self):
        for ssh, allowed in ((None, ["alice"]), ("localhost", ["alice"]), ("gpu.example", [])):
            with self.subTest(ssh=ssh, allowed=allowed):
                self.hosts[0].update(ssh=ssh, users=allowed)
                self.assertFalse(R.private_hosts(self.owner)[0][0]["available"])
                with patch.object(users, "need_project"):
                    with self.assertRaisesRegex(users.Denied, "没开给你用"):
                        R.submit(self.owner, "p", {"destination": "mine", "jobs": "jobs.txt", "request_id": "same", "root": "/shared/projects/p"})
                self.run.assert_not_called()

    def test_admin_pool_submission_records_actual_project_owner(self):
        with patch.object(Q, "_submit", return_value={"id": "q"}) as submit:
            R.submit(self.admin, "p", {"destination": "pool", "jobs": "jobs.txt", "request_id": "same"})
        self.assertEqual(submit.call_args.args[0].name, "alice")

    def test_resource_view_deduplicates_source_registration_without_exposing_root(self):
        row = {"id": "q", "project": "p", "account": "alice", "root": "/shared/projects/p", "jobs": "jobs.txt", "state": "queued", "t": 1}
        with patch.object(Q, "read", return_value={"queues": {"q": row}}) as read, \
             patch.object(R, "snapshot", return_value={"regs": [{"host": "mine", "queues": [row]}]}):
            result = R.view(self.admin, "p")
        self.assertEqual(len(result["queues"]), 1)
        self.assertEqual(result["queues"][0]["destination"], "pool")
        self.assertNotIn("root", result["queues"][0])
        read.assert_called_once()

    def test_membership_requires_personal_server_but_not_pool_exclusion_or_online(self):
        trial = {"members": ["alice"]}
        self.held.return_value = True
        self.assertIsNone(R.poolview.member_reason(self.owner, trial))
        with patch.object(R.config, "active_hosts", return_value=[]):
            self.assertIn("个人服务器", R.poolview.member_reason(self.owner, trial))
            with patch.object(R.poolview.fleet, "conf", return_value={"servers": [{"id": "ad", "holder": "Alice", "state": "offline"}]}):
                self.assertIsNone(R.poolview.member_reason(self.owner, trial))
        with patch.object(R.config, "active_hosts", return_value=[self.hosts[2]]):
            self.assertIn("个人服务器", R.poolview.member_reason(self.owner, trial))
        with patch.object(R.config, "active_hosts", return_value=[]):
            self.assertIn("个人服务器", R.poolview.member_reason(self.admin, trial))


class QueueRoute(unittest.TestCase):
    def test_pending_pool_queue_blocks_ordinary_registration_and_preserves_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = worker_module(Path(tmp))
            w.DATA_ROOT = tmp
            root = Path(tmp).resolve() / "projects" / "p"
            root.mkdir(parents=True)
            (root / "jobs.txt").write_text("done_task|echo done\nnext_task|echo next\n")
            queue = w.load_queue({"root": str(root), "jobs": "jobs.txt", "project": "p", "_slug": "q"})
            Path(queue["state"]).mkdir(parents=True)
            Path(queue["state"], "done_task.done").touch()
            spec = {"root": str(root), "jobs": "jobs.txt", "project": "p", "queue_id": "a"*32, "action": "claim", "source_host": "a"}
            source = Path(w.Paths("a").dir)
            source.mkdir(parents=True)
            (source / "status.json").write_text('{"pool_ready_protocol":1}')
            result = w.pool_queue_route(spec)
            registered, _ = w.registry(w.Paths("a").queues)
            self.assertEqual(registered[0]["pool"], "a"*32)
            self.assertEqual(result["prior_finished"], {"done_task": "done"})
            self.assertEqual(w.main(["add", str(root), "jobs.txt", "--host", "a", "--project", "p"]), 2)
            w.pool_queue_route({**spec, "action": "release", "finished": {"next_task": "done"}})
            self.assertTrue(Path(queue["state"], "next_task.done").exists())
            self.assertEqual(w.main(["add", str(root), "jobs.txt", "--host", "a", "--project", "p"]), 0)

    def test_deleted_or_renamed_manifest_releases_only_after_no_active_work(self):
        for rename in (False, True):
            with self.subTest(rename=rename), tempfile.TemporaryDirectory() as tmp:
                w = worker_module(Path(tmp)); w.DATA_ROOT = tmp
                root = Path(tmp).resolve() / "projects/p"; root.mkdir(parents=True)
                manifest = root / "jobs.txt"; manifest.write_text("train|true\n")
                source = Path(w.Paths("a").dir); source.mkdir(parents=True)
                (source / "status.json").write_text('{"pool_ready_protocol":1}')
                spec = {"root": str(root), "jobs": "jobs.txt", "project": "p", "queue_id": "a"*32, "action": "claim", "source_host": "a"}
                w.pool_queue_route(spec)
                queue = w.registry(str(source / "queues"))[0][0]
                if rename:
                    manifest.rename(root / "renamed.txt")
                else:
                    manifest.unlink()
                marker = Path(w.pool_registration(str(root), "jobs.txt"))
                marker.write_text("in-flight")
                release = dict(spec, action="release")
                with self.assertRaisesRegex(ValueError, "在途"):
                    w.pool_queue_route(release)
                self.assertTrue(Path(str(marker)+".route").exists())
                marker.unlink()
                Path(queue["state"]).mkdir(parents=True)
                current = Path(queue["state"], "current"); current.write_text("train 123")
                with self.assertRaisesRegex(ValueError, "不能切换"):
                    w.pool_queue_route(release)
                current.unlink()
                self.assertTrue(w.pool_queue_route(release)["released"])
                self.assertFalse(Path(str(marker)+".route").exists())
                self.assertEqual(w.registry(str(source / "queues"))[0], [])

    def test_pool_registration_reuses_cross_host_state_directory_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = worker_module(Path(tmp)); w.DATA_ROOT = tmp
            root = Path(tmp).resolve() / "projects/p"; root.mkdir(parents=True)
            for name in ("one", "two"):
                (root / (name + ".txt")).write_text("train|true\n")
            for host in ("a", "b"):
                source = Path(w.Paths(host).dir); source.mkdir(parents=True)
                (source / "status.json").write_text('{"pool_ready_protocol":1}')
            def claim(jobs, identity, host):
                return w.pool_queue_route({"root": str(root), "jobs": jobs, "project": "p", "queue_id": identity * 32,
                                           "action": "claim", "source_host": host})
            claim("one.txt", "a", "a")
            with self.assertRaisesRegex(ValueError, "状态目录.*主机 a"):
                claim("two.txt", "b", "b")
            self.assertFalse(Path(w.pool_registration(str(root), "two.txt") + ".route").exists())
            self.assertEqual(w.registry(w.Paths("b").queues)[0], [])
            (root / "two.env").write_text("QUEUE_STATE=results/two\n")
            claim("two.txt", "b", "b")
            (root / "results/_queue").mkdir(parents=True)
            (root / "results/_queue/train.done").touch()
            second = w.registry(w.Paths("b").queues)[0][0]
            self.assertEqual(w.next_job(second)["name"], "train")

    def test_running_queue_refuses_route_switch_without_deleting_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = worker_module(Path(tmp)); w.DATA_ROOT = tmp
            root = Path(tmp).resolve() / "projects" / "p"; root.mkdir(parents=True)
            (root / "jobs.txt").write_text("train|echo train\n")
            queue = w.load_queue({"root": str(root), "jobs": "jobs.txt", "project": "p", "_slug": "q"})
            Path(queue["state"]).mkdir(parents=True)
            Path(queue["state"], "current").write_text("train 123")
            with self.assertRaisesRegex(ValueError, "不能切换"):
                w.pool_queue_route({"root": str(root), "jobs": "jobs.txt", "project": "p", "queue_id": "a"*32, "action": "claim", "source_host": "a"})
            self.assertFalse(Path(w.pool_registration(str(root), "jobs.txt") + ".route").exists())


class ProjectLockOrder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, LABMON_STATE=tmp.name)
        env.start()
        self.addCleanup(env.stop)
        projects.put_config('p', 'id="p"\n', owner='alice')

    def test_configuration_and_archive_do_not_invert_host_registry_order(self):
        import threading
        from contextlib import contextmanager
        from labmon import config
        class BoundedLock:
            def __init__(self):
                self.lock = threading.RLock()
            def __enter__(self):
                if not self.lock.acquire(timeout=1):
                    raise TimeoutError('锁顺序形成循环等待')
                return self
            def __exit__(self, *args):
                self.lock.release()
        for action in ('config', 'archive'):
            with self.subTest(action=action):
                hosts, reg = BoundedLock(), BoundedLock()
                entered, errors = threading.Event(), []
                original = projects._reg_locked
                @contextmanager
                def registry():
                    with original():
                        if threading.current_thread().name == 'project-edit':
                            entered.set()
                        yield
                def weight(*args):
                    with hosts:
                        pass
                def edit():
                    try:
                        if action == 'config':
                            projects.set_config('p', {'title': 'new'})
                        else:
                            projects.archive('p', None, {})
                    except Exception as e:
                        errors.append(e)
                with patch.object(config, 'HOSTS_LOCK', hosts), patch.object(projects, 'HOSTS_LOCK', hosts, create=True), patch.object(projects, '_REG', reg), \
                     patch.object(projects, '_reg_locked', registry), patch.object(projects, 'archive_block', return_value=None), \
                     patch.object(projects, 'set_weight', side_effect=weight), patch.object(projects, 'set_sched_ovr'):
                    with hosts:
                        thread = threading.Thread(target=edit, name='project-edit')
                        thread.start()
                        entered.wait(.15)
                        try:
                            projects.set_owner('p', 'alice', 'test')
                        except Exception as e:
                            errors.append(e)
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
