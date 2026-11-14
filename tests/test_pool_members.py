"""成员退出与任务接单共享事务，不能隐藏尚未收尾的任务。"""
import os
import tempfile
import unittest
from unittest.mock import patch

from labmon import fleet, poolqueues, poolview, users
from labmon.state import save_json, state_dir


class Members(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, LABMON_STATE=root))
        self.enterContext(patch('labmon.lease.need'))
        self.enterContext(patch('labmon.users.account', return_value={'name': 'amy', 'role': 'user'}))
        self.enterContext(patch('labmon.fleet.conf', return_value={}))
        self.enterContext(patch('labmon.fleet.account_person', return_value=('AMY', None)))
        self.enterContext(patch('labmon.records.log_events'))
        self.enterContext(patch('labmon.poolview.personal_servers', return_value=([{'id': 'own'}], None), create=True))
        save_json(state_dir() / 'pool-shadow.json', {'members': ['amy'], 'snapshot': {'now': 100}})

    def test_active_prevents_removal(self):
        for row in ({'state': 'queued'}, {'state': 'completed', 'dispatch': {'phase': 'unknown'}}):
            save_json(fleet.state_file(poolqueues.FILE), {'queues': {'a': dict(account='amy', **row)}})
            with self.assertRaises(users.Denied):
                poolview.set_member('amy', False, 'admin')
            self.assertEqual(poolview.trial()['members'], ['amy'])

    def test_finished_member_can_leave_without_erasing_history(self):
        row = {'account': 'amy', 'state': 'completed', 'dispatch': {'phase': 'done'}}
        save_json(fleet.state_file(poolqueues.FILE), {'queues': {'a': row}})
        poolview.set_member('amy', False, 'admin')
        self.assertEqual(poolview.trial(), {'members': [], 'snapshot': {'now': 100}})
        self.assertEqual(poolqueues.read()['queues']['a'], row)

    def test_unbound_account_cannot_join(self):
        with patch('labmon.fleet.account_person', return_value=(None, '未绑定')):
            with self.assertRaises(users.Denied):
                poolview.set_member('amy', True, 'admin')

    def test_no_personal_server_cannot_join_but_can_leave(self):
        with patch('labmon.poolview.personal_servers', return_value=([], '需要个人服务器')):
            with self.assertRaisesRegex(users.Denied, '需要个人服务器'):
                poolview.set_member('amy', True, 'admin')
            poolview.set_member('amy', False, 'admin')
        self.assertEqual(poolview.trial()['members'], [])

    def test_admin_is_not_removable_membership(self):
        with patch('labmon.users.account', return_value={'name': 'amy', 'role': 'admin'}):
            with self.assertRaises(users.Denied):
                poolview.set_member('amy', False, 'admin')


class ResourceStages(unittest.TestCase):
    def test_release_receipt_is_required_before_supply_disappears(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, LABMON_STATE=root))
        from labmon import pool_supply
        stages = ['preparing', 'ready', 'releasing', 'released']
        save_json(fleet.state_file(pool_supply.FILE), {
            stage: {'stage': stage, 'devices': ['GPU-' + stage]} for stage in stages})
        nodes = [dict(node=s, state='ready', total=1, free=1) for s in stages]
        who = users.Ident('amy', 'user', frozenset(), 'session', 'test')
        with patch('labmon.pool_dispatch.live_view', return_value={'nodes': nodes}), \
                patch('labmon.pool_dispatch.enabled', return_value=False):
            result = poolview.live(who)
        self.assertFalse(result['dispatch_enabled'])
        self.assertEqual([n['state'] for n in result['nodes']],
                         ['preparing', 'ready', 'withdrawing', 'withdrawn'])
        self.assertEqual([n['free'] for n in result['nodes']], [0, 1, 0, 0])
        self.assertEqual(result['capacity'], {'total': 3, 'free': 1, 'max_same_node_free': 1})


class ResourceObservation(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, LABMON_STATE=root))
        self.enterContext(patch('time.time', return_value=1000))
        self.enterContext(patch('labmon.pool_dispatch.enabled', return_value=True))
        self.enterContext(patch('labmon.users.owns', side_effect=lambda who, pid: pid == 'mine'))
        self.who = users.Ident('amy', 'user', frozenset(), 'session', 'test')

    def test_waiting_visibility_and_inflight_anonymity(self):
        rows = {}
        for key, account, pid, state, phase in [('mine', 'amy', 'mine', 'queued', 'waiting'),
                ('other', 'bob', 'other', 'queued', 'waiting'), ('lost', 'amy', 'lost', 'queued', 'waiting'),
                ('withdrawn', 'amy', 'mine', 'withdrawn', 'waiting'), ('done', 'amy', 'mine', 'completed', 'done'),
                ('run', 'bob', 'other', 'claimed', 'unknown'), ('prep', 'amy', 'mine', 'preparing', None)]:
            rows[key] = {'state': state, 'account': account, 'project': pid, 'root': '/private',
                         'dispatch': {'phase': phase, 'node': 'a', 'devices': ['GPU-a'], 'task_name': key,
                                      'wait_reason': {'text': key + '-reason'}}}
        save_json(fleet.state_file(poolqueues.FILE), {'queues': rows})
        with patch('labmon.pool_dispatch.live_view', return_value={'nodes': [], 'observed': 999}):
            result = poolview.live(self.who)
        self.assertEqual([r['task'] for r in result['waiting']], ['mine', None])
        self.assertEqual(result['waiting'][0]['reason'], 'mine-reason')
        self.assertEqual(result['waiting'][1]['phase'], 'preparing')
        self.assertEqual(result['assignments'][0]['project'], None)
        self.assertEqual(result['assignments'][0]['task'], None)
        self.assertNotIn('/private', str(result))
        self.assertNotIn('other-reason', str(result))
        self.assertTrue(result['inventory_fresh'])
        with patch('labmon.pool_dispatch.live_view', return_value={'nodes': [], 'observed': 1}):
            self.assertFalse(poolview.live(self.who)['inventory_fresh'])

    def test_supplied_gpu_sample_is_whitelisted_and_fresh(self):
        from labmon import pool_supply
        save_json(fleet.state_file(pool_supply.FILE), {'a': {'stage': 'ready', 'devices': ['GPU-a', 'GPU-missing']}})
        host = {'id': 'a', 'state': 'keepalive', 'sample': {'t': 999, 'state': 'keepalive', 'work': ['/private/command'],
                'gpus': [{'uuid': 'GPU-a', 'i': 2, 'util': 64, 'used': 10, 'total': 100, 'command': 'secret'},
                         {'uuid': 'GPU-b', 'i': 3, 'util': 100}, {'uuid': 'GPU-missing', 'util': None}]}}
        node = dict(node='a', state='ready', total=2, free=2)
        self.enterContext(patch('labmon.pool_dispatch.live_view', side_effect=lambda: {'nodes': [node], 'observed': 999}))
        self.enterContext(patch('labmon.state.snapshot', return_value={'fleet': {'servers': [host]}}))
        result = poolview.live(self.who)
        sample = result['nodes'][0]['sample']
        self.assertEqual([g['uuid'] for g in sample['gpus']], ['GPU-a'])
        self.assertEqual(sample['state'], 'keepalive')
        self.assertNotIn('secret', str(result))
        self.assertNotIn('/private', str(result))
        for changes in ({'state': 'down'}, {'off': True}):
            with patch.dict(host, changes):
                self.assertIsNone(poolview.live(self.who)['nodes'][0]['sample'])
        host['sample']['t'] = 1
        self.assertIsNone(poolview.live(self.who)['nodes'][0]['sample'])

    def test_mine_sample_uses_board_age_across_worker_clock_skew(self):
        from labmon import fleetboot
        host = {'id': 'a', 'platform_job': 'AMY', 'platform_pool': 'pool', 'ssh': 'fixture', 'port': 1}
        self.enterContext(patch('labmon.fleetboot.active_hosts', return_value=[host]))
        self.enterContext(patch('labmon.fleet.host_holder', return_value='AMY'))
        node = {'node': 'a', 'state': 'running', 'devices': ['GPU-a']}
        for skew in (0, 3600, -3600):
            source = {'id': 'a', 'worker_up': True, 'age': 1, 'status': {'t': 998 + skew,
                      'info': {'gpus': [{'uuid': 'GPU-a', 'i': 0, 'util': 64, 'used': 10, 'total': 100}]}}}
            board = {'generated': 999, 'hosts': [source], 'fleet': {'mine': fleetboot.mine(None, [source])}}
            self.assertNotIn('t', board['fleet']['mine'][0]['sample'])
            sample = poolview.gpu_sample(node, board)
            self.assertIsNotNone(sample)
            self.assertEqual((sample['t'], sample['gpus'][0]['util']), (998, 64))
            with patch.dict(board, generated=1):
                self.assertIsNone(poolview.gpu_sample(node, board))
            with patch.dict(board, generated=1002):
                self.assertIsNone(poolview.gpu_sample(node, board))
            for change in ({'worker_up': False, 'age': 1000}, {'lan_off': True}):
                with patch.dict(source, change):
                    board['fleet']['mine'] = fleetboot.mine(None, [source])
                    self.assertIsNone(poolview.gpu_sample(node, board))
