"""预备续租（renew.py）的离线测试与平台登录的 RSA 加密。续租本身与组内服务器同一条流程，测试在 test_fleetops.py（模拟平台）。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import KEY, fake_host, rsa_decrypt, running_job  # noqa: E402
from labmon import platform as P  # noqa: E402
from labmon import renew as R  # noqa: E402


class RsaTest(unittest.TestCase):
    def test_rsa_roundtrip(self):
        key = P.rsa_public_key(KEY[0])
        self.assertEqual(key[0], KEY[1])
        for pw in ("s3cret", "中文密码!@#"):
            self.assertEqual(rsa_decrypt(KEY[1], KEY[2], P.rsa_encrypt(key, pw)), pw)


class ReserveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = self.tmp.name
        self.drains = []

    def tearDown(self):
        os.environ.pop("LABMON_STATE", None)
        self.tmp.cleanup()

    def host(self, running, end):
        """主机 29，租期 end 到期；running 是 [(任务名, 项目看板算的预计结束)]。"""
        return fake_host("29", lease_end=end, status=dict(running=[running_job(n, "a1", prog={"eta_ts": eta}) for n, eta in running]))

    def ovr(self, action, kind, hid, until=None, at=None):
        self.drains.append((action, kind, until, at))
        return 0

    def test_wait_then_ready(self):
        t = time.time()
        end = t + 30 * 3600
        R.reserve("29", end, ovr=self.ovr)
        self.assertEqual(self.drains, [("add", "drain", end + R.EXTRA_S, None)])
        cfg = {"29": {"platform": "http://x/"}}
        e = self.host([("x1", t + 3600)], end)
        self.assertEqual(R.check([e], cfg, t), [])
        self.assertEqual(e["renew"]["state"], "waiting")
        self.assertIn("跑完", e["renew"]["why"])
        e = self.host([], end)
        msgs = R.check([e], cfg, t + 3700)
        self.assertEqual(len(msgs), 1)
        self.assertIn("可以续租了", msgs[0])
        self.assertEqual(R.check([self.host([], end)], cfg, t + 3800), [])          # 只通知一次

    def test_hold_placeholder_does_not_wait(self):
        t = time.time()
        end = t + 30 * 3600
        R.reserve("29", end, ovr=self.ovr)
        e = self.host([("hold_until_ok", None)], end)
        e["status"]["running"][0]["hold"] = True                                    # 暂停占位：等人删标记才退出
        msgs = R.check([e], {"29": {}}, t)
        self.assertEqual(e["renew"]["state"], "ready")
        self.assertEqual(e["renew"]["interrupt"], [])
        self.assertIn("可以续租了", msgs[0])

    def test_late_job_does_not_wait(self):
        t = time.time()
        end = t + 3 * 3600
        R.reserve("29", end, ovr=self.ovr)
        e = self.host([("x1", end - 1800)], end)                                    # 预计跑完时已过截止（到期前 1 小时）
        msgs = R.check([e], {"29": {}}, t)
        self.assertEqual(e["renew"]["state"], "ready")
        self.assertEqual(e["renew"]["interrupt"], ["x1"])
        self.assertIn("中断", msgs[0])

    def test_deadline_passed(self):
        t = time.time()
        end = t + 1800
        R.reserve("29", end, ovr=self.ovr)
        e = self.host([("x1", None)], end)
        R.check([e], {"29": {}}, t)
        self.assertEqual(e["renew"]["state"], "ready")
        self.assertIn("不再等", e["renew"]["why"])

    def test_auto_reserve(self):
        t = time.time()
        cfg = {"29": {"platform": "http://x/"}}
        calls = []
        fn = lambda hid, end, auto=False: calls.append(auto) or R.reserve(hid, end, auto=auto, ovr=self.ovr)  # noqa: E731
        R.check([self.host([("x1", t + 600)], t + 30 * 3600)], cfg, t, reserve_fn=fn, ovr=self.ovr)
        self.assertEqual(calls, [])                                                  # 离到期 30 小时：不自动预备
        msgs = R.check([self.host([("x1", t + 600)], t + 20 * 3600)], cfg, t, reserve_fn=fn, ovr=self.ovr)
        self.assertEqual(calls, [True])
        self.assertIn("自动预备", msgs[0])
        r = R.state()["res"]["29"]
        self.assertTrue(r["auto"])
        self.assertEqual(r["mode"], "deadline")                                      # 自动预备用截止准入，不 drain
        self.assertEqual(self.drains[0][:2], ("add", "deadline"))
        self.assertAlmostEqual(self.drains[0][3], t + 20 * 3600 - R.MARGIN_S, delta=1)
        # 没在跑的、但还有跑得完的在排队：不算可以续租
        e = self.host([], t + 20 * 3600)
        e["status"]["waiting"] = [{"name": "x2", "why": "没有可用显存", "held": None}]
        self.assertEqual(R.check([e], cfg, t, ovr=self.ovr), [])
        # 剩下的都被临时指令挡住（截止、项目暂停；按 worker 状态里的 held 认，不按文字）：可以续租，并下 drain 冻结
        e["status"]["waiting"] = [{"name": "x2", "why": "续租截止 11-12 09:54 前跑不完（估 300 分钟）", "held": "deadline"},
                                  {"name": "x3", "why": "项目 a1 暂停中", "held": "pause"}]
        msgs = R.check([e], cfg, t, ovr=self.ovr)
        self.assertIn("可以续租了", msgs[0])
        self.assertEqual(self.drains[-1][:2], ("add", "drain"))
        self.assertEqual(R.state()["res"]["29"]["mode"], "drain")
        R.cancel("29", ovr=self.ovr)
        R.check([self.host([("x1", t + 600)], t + 5 * 3600)], {"29": {"platform": "http://x/", "lease_reserve_h": 0}}, t, reserve_fn=fn)
        self.assertEqual(calls, [True])

    def test_local_loss_warning(self):
        t = time.time()
        end = t + 30 * 3600
        R.reserve("29", end, ovr=self.ovr)
        e = self.host([], end)
        e["status"]["local"] = {"t": t, "jobs": [
            {"job": "x4_s20_seed1", "files": ["/tmp/a1_r9/w0/a.pt"], "heal": False},
            {"job": "x4_s50_seed1", "files": ["/tmp/a1_r9/w0/b.pt"], "heal": True}]}
        msgs = R.check([e], {"29": {"platform": "http://x/"}}, t)
        self.assertIn("可以续租了", msgs[0])
        self.assertIn("x4_s20_seed1 要用的本地文件（/tmp/a1_r9/w0）会丢", msgs[0])
        self.assertIn("x4_s50_seed1 的本地文件会丢，命令能自己补", msgs[0])
        self.assertEqual(R.state()["loss"]["29"]["fatal"], ["x4_s20_seed1"])
        # 读不到主机状态时（如续租中容器重启）不覆盖续租前记下的依赖
        R.check([dict(e, status=None)], {"29": {"platform": "http://x/"}}, t)
        self.assertEqual(R.state()["loss"]["29"]["fatal"], ["x4_s20_seed1"])

    def test_check_writes_once(self):
        """每轮 renew.json 只读一次；状态没变不写，变了一次写完。"""
        from unittest import mock
        t = time.time()
        end = t + 30 * 3600
        R.reserve("29", end, ovr=self.ovr)
        cfg = {"29": {"platform": "http://x/"}}
        e = self.host([("x1", t + 3600)], end)
        e["status"]["local"] = {"t": t, "jobs": [{"job": "x1", "files": ["/tmp/a/b.pt"], "heal": False}]}
        with mock.patch.object(R, "save_json", wraps=R.save_json) as sv, mock.patch.object(R, "load_json", wraps=R.load_json) as ld:
            R.check([e], cfg, t)                                                     # 第一次：依赖与预备状态都变了
            self.assertEqual((sv.call_count, ld.call_count), (1, 2))                 # 读两次是开头一次、写回时一次
            R.check([self.host([("x1", t + 3600)], end) | {"status": e["status"]}], cfg, t + 60)
            self.assertEqual((sv.call_count, ld.call_count), (1, 3))                 # 没变：只读不写
        self.assertEqual(R.state()["res"]["29"]["state"], "waiting")
        self.assertEqual(R.state()["loss"]["29"]["fatal"], ["x1"])

    def test_cancel(self):
        R.reserve("29", time.time() + 86400, ovr=self.ovr)
        self.assertEqual(R.cancel("29", ovr=self.ovr), "已取消，恢复放行")
        self.assertEqual([d[:2] for d in self.drains[1:]], [("rm", "drain"), ("rm", "deadline")])
        self.assertNotIn("29", R.state()["res"])

    def test_cancel_in_auto_window(self):
        """自动预备时段里取消：这一期不再自动预备；早于这个时段取消的、或续租后的新一期照常自动预备。"""
        t = time.time()
        cfg = {"29": {"platform": "http://x/"}}
        calls = []
        fn = lambda hid, end, auto=False: calls.append(end) or R.reserve(hid, end, auto=auto, ovr=self.ovr)  # noqa: E731
        early = t + 40 * 3600
        R.reserve("29", early, ovr=self.ovr)
        R.cancel("29", early, ovr=self.ovr)                                         # 离到期 40 小时取消：不算
        e = self.host([], early)
        R.check([e], cfg, t, reserve_fn=fn, ovr=self.ovr)
        self.assertEqual(e["auto_res"], {"at": round(early - 24 * 3600), "off": False})
        R.check([self.host([], early)], cfg, early - 20 * 3600, reserve_fn=fn, ovr=self.ovr)
        self.assertEqual(calls, [early])                                             # 到了时段照常自动预备
        R.cancel("29", early, ovr=self.ovr, t=early - 19 * 3600)                    # 时段里取消
        e = self.host([], early)
        R.check([e], cfg, early - 18 * 3600, reserve_fn=fn, ovr=self.ovr)
        self.assertEqual(calls, [early])                                             # 这一期不再预备回来
        self.assertTrue(e["auto_res"]["off"])
        self.assertIsNone(e["renew"])
        new = early + 7 * 86400                                                      # 续租后的新一期
        R.check([self.host([], new)], cfg, new - 20 * 3600, reserve_fn=fn, ovr=self.ovr)
        self.assertEqual(calls, [early, new])
        self.assertNotIn("29", R.state()["kept"])                                   # 再预备时清掉


if __name__ == "__main__":
    unittest.main()
