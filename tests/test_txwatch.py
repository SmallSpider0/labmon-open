"""TX 看门狗（tools/tx/labmon-watch.py，#165）：假的 /healthz、假的推送、拨动的时钟。不连 TX、dashboard-host 与推送服务。

    python -m unittest tests.test_txwatch -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("labmon_watch", ROOT / "tools" / "tx" / "labmon-watch.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

T0 = 1_800_000_000


def ok(t, **kw):
    return {"ok": True, "primary": None, "last_collect": t - 60, "maint_until": None, **kw}


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "state.json")
        self.sent, self.fail = [], False

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, msg, title):
        if self.fail:
            raise OSError("连不上 api.day.app")
        self.sent.append((title, msg))

    def run_at(self, t, h):
        """在时刻 t 跑一轮，/healthz 回 h（None 为连不上）。"""
        with contextlib.redirect_stdout(io.StringIO()):
            W.main(["--url", "http://127.0.0.1:9/healthz", "--state", self.state, "--name", "DASHBOARD-HOST 看板"], now=t, fetch=lambda url: h, send=self.send)
        return json.loads(Path(self.state).read_text())

    def test_down_15_min_then_recover(self):
        self.run_at(T0, ok(T0))
        for m in (0, 5, 10):                                                         # 连不上满 15 分钟才推
            self.run_at(T0 + 60 + m * 60, None)
        self.assertEqual(self.sent, [])
        self.run_at(T0 + 60 + 15 * 60, None)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "DASHBOARD-HOST 看板")
        self.assertIn("连不上", self.sent[0][1])
        self.run_at(T0 + 60 + 20 * 60, {**ok(T0 + 60 + 20 * 60), "primary": False})   # 中途换了种类：不重推
        self.run_at(T0 + 60 + 25 * 60, None)
        self.assertEqual(len(self.sent), 1)
        st = self.run_at(T0 + 60 + 30 * 60, ok(T0 + 60 + 30 * 60))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("已恢复", self.sent[1][1])
        self.assertEqual((st["bad"], st["alerted"]), (None, False))
        self.run_at(T0 + 60 + 35 * 60, ok(T0 + 60 + 35 * 60))
        self.assertEqual(len(self.sent), 2)

    def test_short_blip_not_pushed(self):
        self.run_at(T0, None)
        self.run_at(T0 + 600, None)
        self.run_at(T0 + 900 - 1, ok(T0 + 900))
        self.run_at(T0 + 3600, ok(T0 + 3600))
        self.assertEqual(self.sent, [])

    def test_stale_counts_from_last_collect(self):
        h = ok(T0, last_collect=T0 - 10 * 60)                                         # 10 分钟前采集的：还不算停
        self.run_at(T0, h)
        self.run_at(T0 + 6 * 60, h)                                                   # 16 分钟没采集：算停了，从最后一次采集起已满 15 分钟
        self.assertEqual(len(self.sent), 1)
        self.assertIn("采集停了", self.sent[0][1])
        self.run_at(T0 + 7 * 60, ok(T0 + 7 * 60, last_collect=None))                  # 换成从没采集过：不重推
        self.assertEqual(len(self.sent), 1)

    def test_primary_null_is_fine_false_is_bad(self):
        for m in range(0, 30, 5):
            self.run_at(T0 + m * 60, ok(T0 + m * 60))
        self.assertEqual(self.sent, [])
        for m in range(30, 50, 5):
            self.run_at(T0 + m * 60, ok(T0 + m * 60, primary=False))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("不是主", self.sent[0][1])

    def test_maint_silences_then_counts_from_end(self):
        end = T0 + 3600
        self.run_at(T0, ok(T0, maint_until=end))                                     # 开了维护，接着服务停了
        for m in range(5, 60, 5):
            self.run_at(T0 + m * 60, None)
        self.assertEqual(self.sent, [])
        self.run_at(end + 10 * 60, None)                                              # 维护结束 10 分钟：还不推
        self.assertEqual(self.sent, [])
        self.run_at(end + 15 * 60, None)                                              # 维护结束起满 15 分钟
        self.assertEqual(len(self.sent), 1)

    def test_maint_floor_kept_after_expiry(self):
        """维护到期后服务回的 maint_until 已是 null：照旧从维护结束起再算 15 分钟，不在结束后第一次探到就推（Codex 审查 #182）。"""
        end = T0 + 3600
        self.run_at(T0, ok(T0, maint_until=end))
        self.run_at(end + 60, ok(end + 60, last_collect=T0))                          # 服务回来了、采集还没恢复（上次采集远早于维护结束）
        self.assertEqual(self.sent, [])
        self.run_at(end + 10 * 60, ok(end + 10 * 60, last_collect=T0))
        self.assertEqual(self.sent, [])
        self.run_at(end + 15 * 60, ok(end + 15 * 60, last_collect=T0))
        self.assertEqual(len(self.sent), 1)

    def test_maint_cancelled_counts_from_cancel(self):
        self.run_at(T0, ok(T0, maint_until=T0 + 7200))
        self.run_at(T0 + 600, None)                                                   # 维护中停了
        self.run_at(T0 + 1200, ok(T0 + 1200, last_collect=T0 - 3600))                 # 提前结束（服务回 null）：从这一刻起算
        self.run_at(T0 + 1200 + 14 * 60, ok(T0 + 1200 + 14 * 60, last_collect=T0 - 3600))
        self.assertEqual(self.sent, [])
        self.run_at(T0 + 1200 + 15 * 60, ok(T0 + 1200 + 15 * 60, last_collect=T0 - 3600))
        self.assertEqual(len(self.sent), 1)

    def test_push_fail_retries_next_round(self):
        self.run_at(T0, None)
        self.fail = True
        st = self.run_at(T0 + 15 * 60, None)
        self.assertEqual((self.sent, st.get("alerted")), ([], None))                      # 推失败不算推过
        self.fail = False
        self.run_at(T0 + 20 * 60, None)
        self.assertEqual(len(self.sent), 1)
        self.fail = True
        self.run_at(T0 + 25 * 60, ok(T0 + 25 * 60))                                   # 恢复那条推失败：下一轮再推
        self.fail = False
        self.run_at(T0 + 30 * 60, ok(T0 + 30 * 60))
        self.assertEqual(len(self.sent), 2)
        self.assertIn("已恢复", self.sent[1][1])

    def test_probe_shape(self):
        self.assertIsNone(W.probe("http://127.0.0.1:9/healthz"))                     # 连不上
        self.assertEqual(W.judge({"ok": True, "primary": None, "last_collect": None, "maint_until": None}, T0)[0], "stale")

    def test_templates_agree(self):
        """模板之间对得上：隧道单元的名字是体检看的那个；各模板只用约定的占位，端口不写死（dashboard-host 上 8765 被别的程序占着，#168）。"""
        sys.path.insert(0, str(ROOT))
        from labmon import hostcheck as HC
        self.assertTrue((ROOT / "tools" / "wsl" / HC.TUNNEL_UNIT).exists())
        for f in ["tools/wsl/labmon-tunnel.service", *(str(p.relative_to(ROOT)) for p in (ROOT / "tools" / "tx").iterdir() if p.suffix in (".service", ".timer", ".conf"))]:
            s = (ROOT / f).read_text()
            self.assertNotRegex(s, r":8765\b", f)
        self.assertIn("127.0.0.1:LABMON_PORT/healthz", (ROOT / "tools/tx/labmon-watch.service").read_text())
        self.assertIn("-R 127.0.0.1:LABMON_PORT:127.0.0.1:LABMON_PORT", (ROOT / "tools/wsl/labmon-tunnel.service").read_text())

    def test_push_lib_found_in_repo(self):
        self.assertEqual(W._default_push(), ROOT / "labmon" / "push.py")
        P = W._push_mod(W._default_push())
        self.assertTrue(callable(P.one) and callable(P.load))


if __name__ == "__main__":
    unittest.main()
