"""算力池影子入口的多节点回放。"""
import json
import subprocess
import sys
import unittest
from unittest import mock

from labmon import pool


NOW = 10_000


def node(n, devices, physical=None, **changes):
    return {
        "node": n, "physical_host": physical if physical is not None else n, "model": "A6000",
        "state": "ready", "observed_at": NOW, "cpu_cores": 8, "ram_gib": 64, "ram_limit_gib": 64,
        "cards": [{"uuid": u, "vram_mb": 48 * 1024, "free_mb": 47 * 1024, "idle": True} for u in devices],
        **changes,
    }


def job(name, account, *nodes, gpus=1, since=NOW - 10, **changes):
    return {
        "job": name, "account": account, "project": name, "gpus": gpus, "since": since,
        "vram_gib": 20, "ram_gib": 8, "cpu_cores": 2,
        "eligible_nodes": {n["node"]: [c["uuid"] for c in n["cards"]] for n in nodes},
        **changes,
    }


def attempt(name, j, n, cards, **changes):
    return {"attempt_id": name, "job": j, "account": "a", "project": j, "node": n, "gpus": cards, "ram_gib": 8, "cpu_cores": 2, **changes}


def frame(nodes, jobs, *attempts):
    return dict(now=NOW, nodes=nodes, jobs=jobs, attempts=list(attempts))


class PoolTest(unittest.TestCase):
    def test_first_user_uses_both_then_new_user_gets_next_card(self):
        a, b = node("a", ["GPU-a"]), node("b", ["GPU-b"])
        x, y = job("x", "a", a, b), job("y", "a", a, b, since=NOW - 9)
        before = pool.plan(frame([a, b], [x, y]))
        self.assertEqual([(r["job"], r["node"]) for r in before["assignments"]], [("x", "a"), ("y", "b")])
        z = job("z", "b", a, b, since=NOW - 1)
        after = pool.plan(frame([a, b], [y, z], attempt("old", "x", "a", ["GPU-a"], t0=NOW - 300)))
        self.assertEqual([(r["job"], r["node"]) for r in after["assignments"]], [("z", "b")])
        self.assertEqual(after["waiting"][0]["reason"], "busy")
        self.assertEqual(after["usage"], {"a": {"active_cards": 1, "card_seconds": 300}})

    def test_two_nodes_are_not_a_two_card_node(self):
        for physical in ("machine-a", "machine-b"):
            with self.subTest(physical=physical):
                a = node("container-a", ["GPU-a"], physical="machine-a")
                b = node("container-b", ["GPU-b"], physical=physical)
                both = job("ddp", "a", a, b, gpus=2, since=NOW - 20)
                result = pool.plan(frame([a, b], [both, job("single", "b", a, b)]))
                self.assertEqual(result["capacity"], {"total": 2, "free": 2, "max_same_node_free": 1})
                self.assertEqual([r["job"] for r in result["assignments"]], ["single"])
                self.assertEqual(result["waiting"][0]["reason"], "same_node")
                self.assertIn("需要 2 张同机空闲卡", result["waiting"][0]["text"])

    def test_same_execution_node_gets_all_cards_together(self):
        a = node("a", ["GPU-a", "GPU-b"])
        both, single = job("ddp", "a", a, gpus=2, since=NOW - 20), job("single", "b", a)
        out = pool.plan(frame([a], [both, single]))
        self.assertEqual(out["capacity"]["max_same_node_free"], 2)
        self.assertEqual(out["assignments"][0]["devices"], ["GPU-a", "GPU-b"])
        self.assertEqual(out["waiting"][0]["reason"], "busy")
        a["physical_host"] = None
        out = pool.plan(frame([a], [both, single]))
        self.assertEqual(out["assignments"][0]["job"], "single")
        self.assertEqual(out["capacity"]["max_same_node_free"], 1)

    def test_accounting_is_idempotent_for_duplicate_and_late_receipts(self):
        a = node("a", ["GPU-a", "GPU-b"])
        reserved = attempt("same", "first", "a", ["GPU-a"], reserved_at=NOW - 100)
        running = {**reserved, "t0": NOW - 80}
        done = {**running, "t1": NOW - 30}
        still_reserved = attempt("next", "next", "a", ["GPU-b"], reserved_at=NOW - 5)
        out = pool.plan(frame([a], [], running, done, reserved, done, still_reserved))
        self.assertEqual(out["usage"]["a"], {"active_cards": 1, "card_seconds": 50})
        self.assertEqual(out["capacity"]["free"], 1)
        with self.assertRaises(ValueError):
            pool.plan(frame([a], [], done, {**done, "account": "other"}))
        with self.assertRaises(ValueError):
            pool.plan(frame([a], [], running, attempt("other", "other", "a", ["GPU-a"], t0=NOW - 5)))

    def test_recent_start_still_deducts_cpu_budget(self):
        a = node("a", ["GPU-a", "GPU-b"], observed_at=NOW - 2, cpu_cores=3)
        a_job = job("x", "a", a, eligible_nodes={"a": ["GPU-b"]})
        record = attempt("old", "old", "a", ["GPU-a"], t0=NOW - 1)
        out = pool.plan(frame([a], [a_job], record))
        self.assertEqual(out["waiting"][0]["reason"], "cpu")
        self.assertEqual(pool.plan(frame([a], [a_job], {**record, "t0": NOW - 10}))["assignments"][0]["devices"], ["GPU-b"])

    def test_ineligible_stale_paused_and_borrowed_capacity(self):
        for changes, reason in (
            ({"observed_at": NOW - 1000}, "offline"),
            ({"state": "paused"}, "paused"),
            ({"state": "borrowed"}, "ineligible"),
        ):
            with self.subTest(changes=changes):
                a = node("a", ["GPU-a"], **changes)
                out = pool.plan(frame([a], [job("x", "a", a)]))
                self.assertEqual(out["capacity"]["free"], 0)
                self.assertEqual(out["waiting"][0]["reason"], reason)
        a = node("a", ["GPU-a"])
        b = node("b", ["GPU-a"])
        with self.assertRaises(ValueError):
            pool.plan(frame([a, b], []))

    def test_placement_respects_visibility_cpu_ram_deadline_and_focus(self):
        a = node("a", ["GPU-a", "GPU-b"], cpu_cores=3, ram_gib=32)
        cases = (
            (job("x", "a", a, vram_gib=48), "vram"),
            (job("x", "a", a, ram_gib=30), "ram"),
            (job("x", "a", a, cpu_cores=4), "cpu"),
            (job("x", "a", a, deadline=NOW + 300), "cut"),
            (job("x", "a", a, eligible_nodes={}), "ineligible"),
        )
        for req, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(pool.plan(frame([a], [req]))["waiting"][0]["reason"], reason)
        oldest = job("oldest", "a", a, since=NOW - 60)
        focused = job("focused", "b", a, focus_until=NOW + 1)
        later = job("later", "b", a)
        out = pool.plan(frame([a], [oldest, focused, later]))
        self.assertEqual([r["job"] for r in out["assignments"]], ["focused"])
        self.assertEqual({r["reason"] for r in out["waiting"]}, {"cpu"})

    def test_two_empty_cards_with_only_one_large_enough_wait_for_vram(self):
        a = node("a", ["GPU-a", "GPU-b"])
        a["cards"][1].update(vram_mb=46 * 1024, free_mb=45 * 1024)
        r = pool.plan(frame([a], [job("x", "a", a, gpus=2, vram_gib=46)]))
        self.assertEqual(r["waiting"][0]["reason"], "vram")
        self.assertIn("显存", r["waiting"][0]["text"])

    def test_resource_wait_messages_come_from_worker(self):
        a = node("a", ["GPU-a"])
        text = "worker 的准入文案"
        for worker_reason, req in (
            ("nofit_vram", job("x", "a", a, vram_gib=48)),
            ("cut", job("x", "a", a, deadline=NOW + 300)),
        ):
            with self.subTest(reason=worker_reason), mock.patch.dict(pool.W.WHY, {worker_reason: (text, text)}):
                self.assertEqual(pool.plan(frame([a], [req]))["waiting"][0]["text"], text)

    def test_intro_explains_trial_and_same_node_requirement(self):
        from pathlib import Path
        intro = (Path(__file__).resolve().parents[1] / "docs" / "intro.html").read_text()
        self.assertIn('id="a-pool-shadow"', intro)
        self.assertIn("需要 2 张同机空闲卡", intro)
        self.assertIn("只输出试算结果", intro)

    def test_offline_cli(self):
        a = node("a", ["GPU-a"])
        out = subprocess.run([sys.executable, "-m", "labmon.pool"], input=json.dumps(frame([a], [job("x", "a", a)])),
                             text=True, capture_output=True, check=True)
        result = json.loads(out.stdout)
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["assignments"][0]["devices"], ["GPU-a"])
