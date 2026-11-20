"""本机不在内网（lan.py）：不探、不记连不上、不报掉线，租期照常提醒。

    python -m unittest tests.test_lan -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import KA_PROBE as KA, NO_HOSTS, T0, fake_status, fleet_cfg as cfg  # noqa: E402
os.environ["LABMON_HOSTS"] = NO_HOSTS

from labmon import fleet as F  # noqa: E402
from labmon import lan as L  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon.records import records  # noqa: E402
from labmon.state import state_dir  # noqa: E402


class LanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = self.tmp.name
        L._cache.update(t=0.0, v=None)
        L._ssh.update(ok=0.0, fail=0.0)
        self.env = os.environ.get("LABMON_LAN")

    def tearDown(self):
        os.environ.pop("LABMON_LAN", None) if self.env is None else os.environ.update(LABMON_LAN=self.env)
        L._cache.update(t=0.0, v=None)
        self.tmp.cleanup()

    def test_ssh_guard(self):
        """测试里的 ssh 是 tests/bin 下的假程序（fixtures 放到 PATH 最前面）：漏了桩的用例立刻按连不上返回，不真连、不等连接超时（#60：
        test_until_minutes 曾经真连 198.51.100.27、等满 20 秒）。"""
        t = time.time()
        r = L.ssh_run(L.ssh_argv("root@10.255.255.1", 22) + ["true"], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 255)
        self.assertIn("测试不连 ssh", r.stderr)
        self.assertLess(time.time() - t, 5)

    def test_since_kept(self):
        os.environ["LABMON_LAN"] = "off"
        self.assertEqual(L.check(T0), {"ok": False, "since": T0, "checked": T0})
        self.assertEqual(L.check(T0 + 600)["since"], T0)              # 接着上次离线的起点
        L._cache.update(t=0.0, v=None)
        self.assertEqual(L.state()["since"], T0)                       # 服务重启后从 lan.json 读
        os.environ["LABMON_LAN"] = "on"
        self.assertEqual(L.check(T0 + 1200)["since"], None)
        self.assertEqual([e["msg"] for e in records()], ["本机回到内网（离开了约 20 分钟）"])       # 第一次判定不记

    def test_few_connects(self):
        """ssh 长连接最近连上过就不试连；有连不上才试，先试平台网页，连上了就不碰各台 sshd。"""
        os.environ.pop("LABMON_LAN", None)
        tried, up = [], {("plat", 80)}
        real = (L._reach, L.targets)
        L._reach = lambda hp: tried.append(hp) or hp in up
        L.targets = lambda: ([("plat", 80)], [("a", 22), ("b", 22)])
        try:
            L.note(True, T0)
            self.assertTrue(L.check(T0 + 10)["ok"])
            self.assertTrue(L.check(T0 + 100, force=True)["ok"])
            self.assertEqual(tried, [])
            L.note(False, T0 + 110)                                      # 有一台连不上：试连一次平台
            self.assertTrue(L.check(T0 + 110)["ok"])
            self.assertEqual(tried, [("plat", 80)])
            self.assertTrue(L.check(T0 + 115)["ok"])                     # TTL 内不再试
            self.assertEqual(len(tried), 1)
            up.clear()
            L.note(False, T0 + 200)                                      # 全连不上：平台、各台 sshd 都试过才算离线
            self.assertFalse(L.check(T0 + 200)["ok"])
            self.assertEqual(sorted(tried[1:]), [("a", 22), ("b", 22), ("plat", 80)])
            L.note(True, T0 + 300)                                       # 回到内网：ssh 一连上就算在
            self.assertTrue(L.check(T0 + 300)["ok"])
            self.assertEqual(len(tried), 4)
        finally:
            L._reach, L.targets = real

    def test_fleet_skips_probe(self):
        c, calls = cfg(), []
        real = F.fetch
        F.fetch = lambda s: calls.append(s["id"]) or (None, "连不上")
        try:
            os.environ["LABMON_LAN"] = "off"
            for k in range(5):
                self.assertEqual(F.tick(T0 + k * 600, None, c), [])
            self.assertEqual(calls, [])
            self.assertFalse((F.state_dir() / "series").exists())      # 统计里算没有数据，不是掉线
        finally:
            F.fetch = real
        F.tick(T0 + 3000, lambda s: (KA, None), c)                     # 回到内网：照常探，不报恢复
        self.assertNotIn("fleet_down", [e["kind"] for e in records()])

    def test_fleet_leaving_not_counted(self):
        """探的那一下刚好离开内网：这次连不上不记。"""
        c = cfg()
        os.environ["LABMON_LAN"] = "off"
        F._tick(T0, lambda s: (None, "连不上"), c, None, True, True)
        self.assertFalse(any(v.get("fails") for v in F._load_state().values()))

    def test_health_no_down(self):
        e = lambda **kw: {"id": "a", "label": "A", "status": None, **kw}
        cf = {"a": {"lease_days": 7}}
        self.assertEqual(O.health([e(error="连不上")], cf, T0), [])     # 离开内网前那一次连不上
        for k in range(1, 10):
            h = e(error=L.ERR, lan_off=True)
            self.assertEqual(O.health([h], cf, T0 + k * 60), [])
            self.assertEqual(h["health"]["state"], "lan")
        h = e(error="连不上")
        self.assertEqual(O.health([h], cf, T0 + 700), [])              # 回来后重新计：第一次连不上不报
        self.assertEqual(h["health"]["state"], "suspect")

    def test_gather_parallel(self):
        """各台并行探：一台卡住（超时）不拖慢别的；有远程的连不上再查一次，刚离开内网就都记成 ERR。"""
        import threading
        import time as tm
        os.environ["LABMON_LAN"] = "on"
        seen = set()

        def fetch(h):
            seen.add(threading.get_ident())
            tm.sleep(h["slow"])
            return (None, "超时") if h["slow"] else ("ok", None)
        items = {"a": {"slow": 0.5}, "b": {"slow": 0}, "c": {"slow": 0.5}, "d": {"slow": 0}}
        t0 = tm.monotonic()
        lan, got = L.gather(items, fetch, lambda k: True, T0)
        self.assertLess(tm.monotonic() - t0, 0.9)                        # 串行要 1 秒
        self.assertGreater(len(seen), 1)
        self.assertEqual((lan["ok"], got["a"], got["b"]), (True, (None, "超时"), ("ok", None)))
        os.environ["LABMON_LAN"] = "off"                                 # 探完发现刚离开内网：这轮都不算连不上
        L._cache.update(t=0.0, v=None)
        lan, got = L.gather(items, fetch, lambda k: True, T0 + 100)
        self.assertEqual((lan["ok"], set(got.values())), (False, {(None, L.ERR)}))

    def test_overview_keeps_last(self):
        """全局页：离线时不连 ssh，主机沿用上一次采到的状态，不往曲线里写。"""
        import json
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "h"\nssh = "root@192.0.2.1"\n')
        old = os.environ.get("LABMON_HOSTS")
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            sd = state_dir()
            sd.mkdir(parents=True)
            st = fake_status(t=T0 - 100, running=[{"name": "j"}])
            (sd / "snapshot.json").write_text(json.dumps({"hosts": [{"id": "h", "status": st, "clock": T0 - 90}]}))
            os.environ["LABMON_LAN"] = "off"
            snap = O.collect([], use_ssh=True)
            h = snap["hosts"][0]
            self.assertEqual((h["error"], h["health"]["state"], h["status"]["running"][0]["name"]), (L.ERR, "lan", "j"))
            self.assertEqual((snap["lan"]["ok"], snap["notices"]), (False, []))
            self.assertFalse((sd / "series" / "h.jsonl").exists())
        finally:
            os.environ["LABMON_HOSTS"] = old


    def test_overview_keeps_offers_on_read_failure(self):
        """#25 第 1 条：在内网、这一轮读某台失败（ssh 抖一下）：沿用它上一轮的 offers（主机项的 offers_kept），挪法建议不消失、不重复叫醒。"""
        import json
        import time as tm
        from unittest import mock
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "h"\nssh = "root@192.0.2.1"\n')
        old_env = os.environ.get("LABMON_HOSTS")
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            now = tm.time()
            H = 3600
            job = {"host": "o", "root": "/r", "queue": "q.txt", "name": "x", "there": now + 10 * H, "rest": now + H, "end": now + 2 * H, "own": now}
            off = {"project": "pa", "lane": None, "free": now, "ends": {"o|/r|q.txt": now + 10 * H}, "why": None, "stop": None, "jobs": [job]}
            sd = state_dir()
            sd.mkdir(parents=True)
            st = fake_status(t=now - 70, offers=[off])
            (sd / "snapshot.json").write_text(json.dumps({"generated": now - 60, "rebalance": {"pa": {"key": "rebalance:pa", "target": "h"}},
                                                          "hosts": [{"id": "h", "status": st, "worker_up": True, "age": 10}]}))
            os.environ["LABMON_LAN"] = "on"
            L._cache.update(t=0.0, v=None)
            with mock.patch.object(O, "fetch", return_value=(None, "连不上")):
                snap = O.collect([], use_ssh=True)
            self.assertEqual(snap["hosts"][0]["offers_kept"]["offers"], [off])
            self.assertEqual(snap["rebalance"]["pa"]["key"], "rebalance:pa:h:o/q.txt/x")     # 沿用上一轮的 offers：同一挪法（#109）
        finally:
            os.environ["LABMON_HOSTS"] = old_env
            os.environ["LABMON_LAN"] = "off"
            L._cache.update(t=0.0, v=None)


if __name__ == "__main__":
    unittest.main()
