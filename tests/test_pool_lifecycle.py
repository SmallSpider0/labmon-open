"""池账本接入项目生命周期与真实时间区间。"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from labmon import poolqueues as Q, projects, fleet
from labmon.state import save_json
from labmon.config import ConfigError


class Lifecycle(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, LABMON_STATE=tmp.name)
        env.start()
        self.addCleanup(env.stop)
        projects.put_config('p', 'id="p"\n', owner='alice')
        self.a = dict(id='attempt', task_name='train', phase='running', node='a', devices=['GPU-a'], reserved_at=10, started_at=12, observed_at=20)
        self.row = dict(id='queue', project='p', account='alice', root='/data/p', jobs='jobs.txt', state='claimed', t=1, dispatch=self.a)
        self.data = {'queues': {'q': self.row}}
        save_json(Q.state_file(), self.data)

    def test_wait_missing_requires_current_pool_registration_in_snapshot(self):
        from labmon.waits import JobWatch
        self.row.update(state='queued', dispatch={})
        save_json(Q.state_file(), self.data)
        out = projects.out_dir('p')
        watcher = JobWatch({'id': 'p'}, out, ['typo'])
        t = watcher.t0
        def check(i, lanes):
            save_json(out / 'snapshot.json', {'generated': t + i, 'jobs': [], 'lanes': lanes})
            return watcher.check()[('p', 'typo')]
        self.assertFalse(check(1, [])['final'])
        self.assertFalse(check(2, [])['final'])
        lane = dict(id='a', root='/data/p', queue='jobs.txt')
        self.assertFalse(check(3, [lane])['final'])
        result = check(4, [lane])
        self.assertTrue(result['final'])
        self.assertEqual(result['st'], 'missing')

    def test_wait_recognizes_existing_queue_path_aliases(self):
        from labmon.waits import JobWatch
        self.row.update(state='queued', dispatch={})
        save_json(Q.state_file(), self.data)
        out = projects.out_dir('p')
        for root, queue in [('/data/p', './jobs.txt'), ('/data', '/data/p/jobs.txt'), ('/data', 'p/jobs.txt')]:
            with self.subTest(root=root, queue=queue):
                watcher = JobWatch({'id': 'p'}, out, ['typo'])
                for i in (1, 2):
                    save_json(out / 'snapshot.json', {'generated': watcher.t0 + i, 'jobs': [],
                        'lanes': [dict(id='a', root=root, queue=queue)]})
                    result = watcher.check()[('p', 'typo')]
                self.assertTrue(result['final'])
                self.assertEqual(result['st'], 'missing')

    def test_wait_withdrawn_registration_does_not_hide_invalid_task(self):
        from labmon.waits import JobWatch
        self.row.update(state='withdrawn', dispatch={})
        save_json(Q.state_file(), self.data)
        out = projects.out_dir('p')
        watch = JobWatch({'id': 'p'}, out, ['typo'])
        for i in (1, 2):
            save_json(out / 'snapshot.json', {'generated': watch.t0 + i, 'jobs': [], 'lanes': []})
            result = watch.check()[('p', 'typo')]
        self.assertTrue(result['final'])
        self.assertEqual(result['st'], 'missing')

    def test_wait_receipt_still_completes_before_project_snapshot(self):
        from labmon.waits import JobWatch
        self.row['dispatch'].update(phase='done', finished_at=25, ended_at=30)
        self.row['state'] = 'completed'
        save_json(Q.state_file(), self.data)
        result = JobWatch({'id': 'p'}, projects.out_dir('p'), ['train']).check()[('p', 'train')]
        self.assertTrue(result['final'])
        self.assertEqual(result['st'], 'done')

    def test_active_blocks_archive_and_owner_even_with_empty_snapshots(self):
        self.assertIn('算力池', projects.archive_block('p', None, None))
        with self.assertRaises(ConfigError):
            projects.set_owner('p', 'bob', 'test')
        self.assertEqual(projects.owner('p'), 'alice')

    def test_project_record_actions_follow_explicit_project_write_scope(self):
        self.row['state'] = 'queued'
        self.assertFalse(Q.project_view('p', self.data)['queues'][0]['can_withdraw'])
        self.assertTrue(Q.project_view('p', self.data, writable=True)['queues'][0]['can_withdraw'])

    def test_long_preparation_stays_active_without_fabricated_run(self):
        self.a.update(phase='starting', started_at=None, observed_at=1000)
        view = Q.project_view('p', self.data, now=1000)
        self.assertTrue(view['active'])
        self.assertEqual(Q.occupancy(self.data, now=1000), {})
        self.assertIsNotNone(Q.active_reason('p', self.data))
        self.a.update(phase='stopped', ended_at=1100)
        self.row['state'] = 'completed'
        self.assertFalse(Q.project_view('p', self.data, now=1100)['active'])
        self.assertEqual(Q.occupancy(self.data, now=1100), {})

    def test_same_node_worker_usage_and_pool_receipt_show_one_actual_run(self):
        from labmon import overview
        self.a.update(phase='done', finished_at=25, ended_at=30)
        self.row['history'] = {'attempt': dict(self.a)}
        save_json(Q.state_file(), self.data)
        regular = dict(p='p', n='train', root='/data/p', queue='jobs.txt', s=12, t=25,
                       g=['GPU-a'], gi=[0], st='done')
        host = dict(id='a', status={'info': {'gpus': [{'uuid': 'GPU-a', 'i': 0}]}})
        with patch.object(overview, 'load_rows', return_value=[regular]):
            rows = overview.occupancy([host], [], 0)['a']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['t0'], rows[0]['t1'], rows[0]['attempt_id']), (12, 25, 'attempt'))
        self.assertEqual(len(Q.project_view('p', self.data)['attempts']), 1)

    def test_waiting_projection_preserves_completed_attempt_and_observation(self):
        self.a.update(phase='done', finished_at=25, ended_at=30)
        self.row.update(state='queued', history={'attempt': dict(self.a)})
        save_json(Q.state_file(), self.data)
        Q.dispatch_wait('queue', 'train', 'offline', '等待来源更新')
        data = Q.read()
        view = Q.project_view('p', data, now=1000)
        self.assertEqual(view['queues'][0]['dispatch']['phase'], 'waiting')
        self.assertEqual(len(view['attempts']), 1)
        self.assertEqual(view['attempts'][0]['ended_at'], 30)
        self.assertEqual(Q.occupancy(data, now=1000)['a'][0]['t1'], 25)

    def test_unknown_retains_project_activity(self):
        self.row['state'] = 'completed'
        self.a['phase'] = 'unknown'
        self.assertTrue(Q.project_view('p', self.data)['active'])
        self.assertIsNotNone(Q.active_reason('p', self.data))

    def test_current_and_history_deduplicated_and_no_future_eta(self):
        self.row['history'] = {'attempt': dict(self.a)}
        self.assertEqual(len(Q.project_view('p', self.data)['attempts']), 1)
        item = Q.occupancy(self.data, now=100)['a'][0]
        self.assertEqual((item['t0'], item['t1'], item['end_kind']), (12, 20, 'observed'))
        self.assertEqual(Q.occupancy(self.data, skip='p'), {})
        self.a.update(phase='done', finished_at=25, ended_at=30)
        item = Q.occupancy(self.data, now=100)['a'][0]
        self.assertEqual((item['t1'], item['end_kind']), (25, 'finished'))

    def test_legacy_cleanup_end_is_not_training_end(self):
        self.a.update(phase='done', ended_at=30)
        self.row['state'] = 'completed'
        self.assertIsNone(Q.active_reason('p', self.data))
        self.assertEqual(Q.occupancy(self.data, now=100)['a'][0]['end_kind'], 'cleaned')

    def test_pool_failure_wakes_session_and_new_submission_clears_old_failure(self):
        from labmon.collect import _pool
        self.a['phase'] = 'failed'
        self.row['state'] = 'completed'
        save_json(Q.state_file(), self.data)
        snap = {'alerts': []}
        _pool(snap, 'p', 100)
        self.assertEqual(snap['alerts'][0]['to'], 'agent')
        self.assertIn('失败', snap['alerts'][0]['msg'])
        self.data['queues']['retry'] = dict(self.row, id='retry', t=2, state='queued', dispatch={})
        save_json(Q.state_file(), self.data)
        snap = {'alerts': []}
        _pool(snap, 'p', 100)
        self.assertEqual(snap['alerts'], [])
        self.assertTrue(snap['pool']['active'])

    def test_terminal_first_receipt_retains_actual_training_end(self):
        self.a.pop('started_at')
        self.a['generation'] = 1
        save_json(Q.state_file(), self.data)
        with patch.object(Q.time, 'time', return_value=100):
            Q.dispatch_receipt('queue', 'attempt', {'phase': 'done', 'generation': 1,
                'started_at': 12, 'finished_at': 25, 'cleaned': True, 'log_tail': ['finished']})
        a = Q.find(Q.read(), 'queue')['dispatch']
        self.assertEqual((a['started_at'], a['finished_at'], a['ended_at']), (12, 25, 100))

    def test_unreachable_receipt_does_not_extend_observed_runtime(self):
        self.a['generation'] = 1
        save_json(Q.state_file(), self.data)
        with patch.object(Q.time, 'time', return_value=100):
            Q.dispatch_receipt('queue', 'attempt', {'phase': 'unknown'})
        item = Q.occupancy(now=100)['a'][0]
        self.assertEqual(item['t1'], 20)
        self.assertEqual(item['phase'], 'unknown')

    def test_pool_occupancy_preserves_callers_host_boundary(self):
        from labmon import overview
        self.assertEqual(overview.occupancy([], [], 0), {})
        self.assertEqual(overview.occupancy([{'id': 'other'}], [], 0), {'other': []})
        self.assertEqual(len(overview.occupancy([{'id': 'a'}], [], 0)['a']), 1)

    def test_stale_running_receipt_preserves_actual_observation(self):
        self.a['generation'] = 1
        save_json(Q.state_file(), self.data)
        with patch.object(Q.time, 'time', return_value=100):
            Q.dispatch_receipt('queue', 'attempt', {'phase': 'running', 'generation': 1, 'observed_at': 20})
        self.assertEqual(Q.occupancy(now=100)['a'][0]['t1'], 20)

    def test_unknown_to_failure_wakes_default_listener_again(self):
        from labmon.collect import _pool
        from labmon.waits import _new_alerts
        sf = projects.out_dir('p')/'wait-state.json'
        for phase in ('unknown', 'failed'):
            self.a['phase'] = phase
            save_json(Q.state_file(), self.data)
            snap = {'alerts': []}
            _pool(snap, 'p', 100)
            current = {a['key']: a['msg'] for a in snap['alerts']}
            self.assertEqual(len(_new_alerts(sf, current)), 1, phase)
            self.assertEqual(_new_alerts(sf, current), [])

    def test_configuration_and_archive_do_not_invert_host_registry_order(self):
        save_json(Q.state_file(), {})  # 本用例只核锁顺序；池路由释放由实际归档回归覆盖。
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

class ReceiptLogs(unittest.TestCase):
    def test_current_receipt_uses_original_queue_log_directory(self):
        from tests.fixtures import load_worker
        m = load_worker({})
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            root = Path(tmp)
            (root / 'jobs.env').write_text('QUEUE_LOGS=custom_logs\n')
            (root / 'custom_logs').mkdir()
            (root / 'custom_logs/train.log').write_text('actual training log\n')
            local = root / 'attempt'
            local.mkdir()
            (local / 'train.log').write_text('wrong local log\n')
            a = dict(id='a', generation=1, root=tmp, jobs='jobs.txt', project='p', task_name='train')
            row = m.pool_daily_receipt(str(local), a, 'running')
            self.assertIn('actual training log', row['log_tail'])
            old = m.pool_daily_receipt(str(local), dict(id='old', generation=1, task_name='train'), 'done')
            self.assertIn('wrong local log', old['log_tail'])
