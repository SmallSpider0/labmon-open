"""算力池执行契约的隔离夹具。启动、停止和核对均由受信夹具提供。"""
from dataclasses import replace
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, mock

from labmon.pool_execution import Coordinator, Grant, Node, Refused


class ExecutionTest(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ledger.sqlite"
        self.now = 100
        self.clock = lambda: self.now
        self.keys = {"node": "launcher-key"}
        self.co = Coordinator(self.path, self.keys, enabled=True, clock=self.clock)
        self.node = Node("node", "host", "boot1", frozenset({"gpu0", "gpu1"}), 8, 32, 99, True, True, True)
        self.grant = Grant("a", "amy", "p", "q", "fp", "node", "host", "boot1", ("gpu0", "gpu1"), 4, 8, 120)
        self.backend = mock.Mock()
        self.backend.matches.return_value = True
        self.backend.cleaned.return_value = False

    def launch(self, node=None, key="launcher-key"):
        self.co.launch("a", node or self.node, key, self.backend)

    def test_default_shadow_and_unverified_node(self):
        shadow = Coordinator(self.path, self.keys, clock=self.clock)
        with self.assertRaisesRegex(Refused, "只试算"):
            shadow.reserve(self.grant, self.node)
        for node in (replace(self.node, isolation_verified=False), replace(self.node, accepting=False),
                     replace(self.node, observed_at=10), replace(self.node, dedicated=False),
                     replace(self.node, devices=frozenset({"gpu0"}))):
            with self.assertRaises(Refused):
                self.co.reserve(self.grant, node)

    def test_forgery_stale_generation_and_queue(self):
        self.co.reserve(self.grant, self.node)
        with self.assertRaisesRegex(Refused, "无权"):
            self.launch(key="fake")
        with self.assertRaisesRegex(Refused, "代际"):
            self.launch(replace(self.node, generation="boot2"))
        self.now = 120
        with self.assertRaisesRegex(Refused, "承诺已过期"):
            self.launch()
        self.now = 100
        self.backend.matches.return_value = False
        with self.assertRaisesRegex(Refused, "队首已变"):
            self.launch()
        self.backend.start.assert_not_called()

    def test_atomic_claim_and_no_replay(self):
        self.co.reserve(self.grant, self.node)

        def claim(_):
            try:
                self.launch()
            except Refused:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(claim, range(2))), [False, True])
        self.backend.start.assert_called_once()
        with self.assertRaisesRegex(Refused, "GPU 仍被占用"):
            self.co.reserve(replace(self.grant, attempt_id="b", job="q2"), self.node)

    def test_one_queue_head_cannot_claim_multiple_nodes(self):
        self.co.reserve(self.grant, self.node)
        other = replace(self.node, node="other", physical_host="other-host", devices=frozenset({"gpu2"}))
        duplicate = replace(self.grant, attempt_id="b", node=other.node, physical_host=other.physical_host,
                            devices=("gpu2",))
        with self.assertRaisesRegex(Refused, "队首已有"):
            self.co.reserve(duplicate, other)

    def test_coordinator_unavailable_does_not_start(self):
        self.co.reserve(self.grant, self.node)
        with mock.patch("labmon.pool_execution.sqlite3.connect", side_effect=OSError("账本不可达")):
            with self.assertRaises(OSError):
                self.launch()
        self.backend.start.assert_not_called()
        self.launch()
        self.backend.start.assert_called_once()

    def test_unknown_retains_resources_until_verified_cleanup(self):
        self.co.reserve(self.grant, self.node)
        self.backend.start.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            self.launch()
        self.co = Coordinator(self.path, self.keys, enabled=True, clock=self.clock)
        with self.assertRaisesRegex(Refused, "已领取"):
            self.launch()
        with self.assertRaisesRegex(Refused, "本账号"):
            self.co.request_stop("a", "bob")
        self.co.request_stop("a", "amy")
        with self.assertRaisesRegex(Refused, "尚未核实清理"):
            self.co.reconcile("a", self.node, "launcher-key", self.backend)
        with self.assertRaisesRegex(Refused, "GPU 仍被占用"):
            self.co.reserve(replace(self.grant, attempt_id="b", job="q2"), self.node)
        self.backend.cleaned.return_value = True
        with self.assertRaisesRegex(Refused, "代际"):
            self.co.reconcile("a", replace(self.node, generation="boot2"), "launcher-key", self.backend)
        self.co.reconcile("a", self.node, "launcher-key", self.backend)
        self.co.reconcile("a", self.node, "launcher-key", self.backend)
        self.co.reserve(replace(self.grant, attempt_id="b", job="q2"), self.node)
        with self.assertRaises(Refused):
            self.co.reserve(self.grant, self.node)
