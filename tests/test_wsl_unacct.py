"""Issue #114 阶段 3（PR 3：Windows WSL2 适配——通用 unacct_vram 外部占卡与 9p 路径预警）专项测试：
1. unacct_vram 差额识别：底噪（<512 MiB）过滤、1.5 GiB（1536 MiB）显存触发、无可见进程+利用率 >=15% 触发、
   WDDM 单进程显存 [N/A]（mb <= 0）保护、有我方/保活可见进程时不误靠利用率触发、滞回（1024 MiB / 5%）解除与释放自动恢复；
2. 准入与全链路对齐：默认整卡暂停放新任务（ext_gpu、不成屏障不挡纯 CPU 任务、不报异常/不产生告警、不杀已跑非独占任务）、
   多卡隔离、allow_shared_gpu 经 sched.json 免重启切换为按剩余显存共享、真放行 place 与 simulate/forecast/fit/offers 一致、
   独占任务（exclusive=gpu/host）准入拦截与运行中持续 2 轮外部占卡触发 void_check 作废重排、保活自动让路、重启 load_prev 保留滞回；
3. WSL2 /mnt/[a-z]/ 9p 路径预警：is_wsl_mnt / wsl_mnt_warn 唯一实现，覆盖 worker status、labmon check、exthost.test_external；
4. 看板后端集成：workerctl.worker_env、overview._to_worker 下发 allow_shared_gpu、exthost 读写校验、collect._hosts 透传
   unacct_mb/unacct_paused 与 _host_alerts 豁免全卡外部占卡的 gpu_idle 误报、api.status 输出 unacct_lines。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "tests")
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("LABMON_NO_NOTIFY", "1")
os.environ.setdefault("LABMON_NO_KEYCHAIN", "1")
os.environ.setdefault("LABMON_LAN", "off")

from fixtures import add_account, worker_module  # noqa: E402
from labmon import api as A  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import exthost as EH  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon import workerctl as W  # noqa: E402
from labmon.worker.pure import is_wsl_mnt, wsl_mnt_warn  # noqa: E402


def _gpu(i: int, used: float, total: float = 24576.0, util: float = 0.0, uuid: str | None = None) -> dict:
    return {
        "i": i, "uuid": uuid or f"GPU-wsl-{i}", "name": "RTX 4090",
        "used": float(used), "total": float(total), "free": float(total - used), "util": float(util),
    }


class UnacctVramPureTest(unittest.TestCase):
    """unacct_vram 核心计算、WDDM [N/A] 保护、底噪过滤与滞回边界测试。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.m = worker_module(Path(self.td.name))

    def tearDown(self):
        self.td.cleanup()

    def test_thresholds_noise_and_wddm_na(self):
        m = self.m
        # < 512 MiB：按驱动/上下文底噪过滤，不扣减也不标 busy（返回空字典）
        r = m.unacct_vram([_gpu(0, 480, util=0)], [])
        self.assertEqual(r, {})

        # 512 MiB <= 差额 < 1536 MiB（如 1535.9）：扣减可放显存，但不触发整卡暂停
        r = m.unacct_vram([_gpu(0, 1535.9, util=0)], [])
        self.assertAlmostEqual(r["GPU-wsl-0"]["deduct"], 1535.9, places=1)
        self.assertFalse(r["GPU-wsl-0"]["busy"])

        # >= 1536 MiB（1.5 GiB）：触发外部活跃占卡
        r = m.unacct_vram([_gpu(0, 1536.0, util=0)], [])
        self.assertEqual(r["GPU-wsl-0"]["deduct"], 1536.0)
        self.assertTrue(r["GPU-wsl-0"]["busy"])
        self.assertIn("1.5 GiB", r["GPU-wsl-0"]["why"])

        # 有可见进程时按 (used - sum(gp)) 算差额（400 MiB < 512 MiB，过滤掉）
        gp = [{"uuid": "GPU-wsl-0", "hpid": 100, "mb": 8000.0, "owner": "job"}]
        r = m.unacct_vram([_gpu(0, 8400.0, util=80)], gp, running_cards={"GPU-wsl-0"})
        self.assertEqual(r, {})

        # WDDM 场景：可见进程行显存为 [N/A]（gp 里 mb == 0）时，不把这些进程已占显存误归为外部差额
        gp_na = [{"uuid": "GPU-wsl-0", "hpid": 100, "mb": 0.0, "owner": "job"}]
        r = m.unacct_vram([_gpu(0, 10000.0, util=90)], gp_na, running_cards={"GPU-wsl-0"})
        self.assertEqual(r, {})

    def test_util_trigger_and_hysteresis(self):
        m = self.m
        # 无可见进程、无在跑任务：14.9% 不触发，15.0% 触发
        r0 = m.unacct_vram([_gpu(0, 200, util=14.9)], [])
        self.assertEqual(r0, {})
        r1 = m.unacct_vram([_gpu(0, 200, util=15.0)], [])
        self.assertTrue(r1["GPU-wsl-0"]["busy"])
        self.assertIn("利用率", r1["GPU-wsl-0"]["why"])

        # 存在我方在跑任务或可见保活进程时，不因高利用率误触发外部占卡
        r_run = m.unacct_vram([_gpu(0, 200, util=80.0)], [], running_cards={"GPU-wsl-0"})
        self.assertEqual(r_run, {})
        r_ka = m.unacct_vram([_gpu(0, 700, util=95.0)], [{"uuid": "GPU-wsl-0", "hpid": 10, "mb": 700.0, "owner": "keepalive"}])
        self.assertEqual(r_ka, {})

        # 滞回（显存）：1536 触发 → 1200 保持 busy → 1023.0 且 util < 5% 解除（但 1023 >= 512 仍保留 deduct）
        r_hold = m.unacct_vram([_gpu(0, 1200.0, util=0.0)], [], prev_busy={"GPU-wsl-0"})
        self.assertTrue(r_hold["GPU-wsl-0"]["busy"])
        r_still = m.unacct_vram([_gpu(0, 1024.0, util=0.0)], [], prev_busy={"GPU-wsl-0"})
        self.assertTrue(r_still["GPU-wsl-0"]["busy"])
        r_clear = m.unacct_vram([_gpu(0, 1023.0, util=4.9)], [], prev_busy={"GPU-wsl-0"})
        self.assertFalse(r_clear["GPU-wsl-0"]["busy"])
        self.assertEqual(r_clear["GPU-wsl-0"]["deduct"], 1023.0)

        # 滞回（利用率）：15% 触发 → 8% 保持 → 4.9% 解除
        r_uhold = m.unacct_vram([_gpu(0, 200.0, util=8.0)], [], prev_busy={"GPU-wsl-0"})
        self.assertTrue(r_uhold["GPU-wsl-0"]["busy"])
        r_uclear = m.unacct_vram([_gpu(0, 200.0, util=4.9)], [], prev_busy={"GPU-wsl-0"})
        self.assertEqual(r_uclear, {})

        # 释放骤降：从 6000 MiB 骤降到 120 MiB → 下一轮立即解除
        r_rel = m.unacct_vram([_gpu(0, 120.0, util=0.0)], [], prev_busy={"GPU-wsl-0"})
        self.assertEqual(r_rel, {})

    def test_is_wsl_detection_and_mnt_path(self):
        # 唯一实现 is_wsl_mnt / wsl_mnt_warn
        self.assertTrue(is_wsl_mnt("/mnt/c/labmon/work"))
        self.assertTrue(is_wsl_mnt("/mnt/D/exp"))
        self.assertTrue(is_wsl_mnt("/mnt/e"))
        self.assertFalse(is_wsl_mnt("/mnt/data/labmon"))
        self.assertFalse(is_wsl_mnt("/home/u/.local/share/labmon/work"))
        self.assertFalse(is_wsl_mnt("~/.local/share/labmon/work"))
        self.assertIn("ext4", wsl_mnt_warn("/mnt/c/work", "wsl-1"))

        # measure.is_wsl 经 LABMON_PROC 注入检测
        with tempfile.TemporaryDirectory() as pd:
            osrel = Path(pd) / "sys" / "kernel" / "osrelease"
            osrel.parent.mkdir(parents=True)
            osrel.write_text("5.15.153.1-microsoft-standard-WSL2\n")
            m_wsl = worker_module(Path(self.td.name), LABMON_PROC=pd)
            self.assertTrue(m_wsl.is_wsl())
            osrel.write_text("6.8.0-45-generic\n")
            self.assertFalse(m_wsl.is_wsl())


class Wsl2SchedulingE2ETest(unittest.TestCase):
    """端到端 WSL2 外部占卡调度：默认整卡暂停、allow_shared_gpu 切换、多卡隔离、推演/fit/offers、独占与保活让路。"""

    def _mk_worker(self, d: Path, **env):
        data_root = d / "work"
        local = data_root / ".tmp"
        m = worker_module(d, LABMON_LOCAL=str(local), LABMON_DATA_ROOT=str(data_root), LABMON_STANDALONE="1", **env)
        w = m.Worker("wsl-1", standalone=True)
        return m, w, data_root

    def test_default_pause_no_barrier_no_kill_and_release_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 模拟 WSL2 宿主机占了 6 GiB 显存（used=6144, total=24576, 同时我方已跑任务占 2048）
            m, w, dr = self._mk_worker(d)

            now = time.time()
            # 预先放一个非独占在跑任务：验证外部占卡出现后绝不杀它
            w.running["keep-run"] = {
                "key": "keep-run", "kind": "queue", "project": "proj-a", "name": "already_running",
                "root": str(dr / "projects/proj-a/old"), "queue": "jobs.txt", "pid": os.getpid(),
                "gpus": ["GPU-wsl-0"], "ngpu": 1, "exclusive": None, "t0": now - 60,
                "est": {"vram": 2.0, "ram": 1.0, "cpus": 1}, "acct": {"vram": 2048.0, "ram": 1.0, "cpu": 1},
                "use": {}, "stop": None,
            }
            with mock.patch.dict(os.environ, {"LABMON_GPUS": "GPU-wsl-0:18432:24576:25", "LABMON_GPU_APPS": f"{os.getpid()}:2048:GPU-wsl-0"}):
                # 宿主机外占 6144 - 2048 = 4096 MiB >= 1536 MiB → 触发暂停放新任务
                w.sample([])
                self.assertTrue(w.info["gpus"][0]["unacct_busy"])
                self.assertAlmostEqual(w.info["gpus"][0]["unacct_mb"], 4096.0, places=0)
                S = w.snapshot({})
                self.assertIn("GPU-wsl-0", S["unacct"])

                # GPU 任务被暂停放行且不成屏障，等待原因清晰写明外部占卡
                req_gpu = {"key": "k_gpu", "kind": "queue", "project": "proj-a", "name": "gpu_job", "ngpu": 1, "allowed": None,
                           "excl": None, "vram": 4.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值",
                           "cpus": 1, "cpu_src": "缺省 1 核", "tag": None, "hold": False, "dur": 20.0}
                pick, why, bar = m.place(req_gpu, S, now)
                self.assertIsNone(pick)
                self.assertFalse(bar)
                self.assertIn("外部（如宿主机）占卡", why)

                # 纯 CPU 任务不受影响、照常放行
                req_cpu = {"key": "k_cpu", "kind": "queue", "project": "proj-a", "name": "cpu_job", "ngpu": 0, "allowed": None,
                           "excl": None, "vram": 0.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值",
                           "cpus": 1, "cpu_src": "缺省 1 核", "tag": None, "hold": False, "dur": 5.0}
                pick_c, why_c, _ = m.place(req_cpu, S, now)
                self.assertEqual(pick_c, [])
                self.assertIsNone(why_c)

                # 非独占在跑任务未被作废/杀掉
                self.assertIn("keep-run", w.running)
                self.assertFalse(w.running["keep-run"].get("void_n"))

                # 推演与 fit、offers 对齐
                sim_s = m.sim_snapshot(S, None)
                self.assertIn("GPU-wsl-0", sim_s["unacct"])
                spick, swhy, sbar = m.place(req_gpu, sim_s, now)
                self.assertIsNone(spick)
                self.assertFalse(sbar)
                self.assertIn("外部（如宿主机）占卡暂停放行", swhy)

                # 写入 status.json 后检验 room.unacct 与 fit_snapshot / unacct_lines
                w.write_status([], {}, {}, m.POLICY, {}, now)
                st = json.loads(Path(w.p.status).read_text(encoding="utf-8"))
                self.assertEqual(st["room"]["unacct"], ["GPU-wsl-0"])
                self.assertTrue(any("默认整卡暂停放新任务" in ln for ln in m.unacct_lines(st)))

                fs = m.fit_snapshot(st)
                self.assertIn("GPU-wsl-0", fs["unacct"])
                fpick, fwhy, _ = m.place(req_gpu, fs, now)
                self.assertIsNone(fpick)
                self.assertIn("外部（如宿主机）占卡", fwhy)

                ok_cards, owhy = w.local_cards()
                self.assertIsNone(ok_cards)
                self.assertIn("外部（如宿主机）占卡", owhy)

                # 宿主机释放后下一轮自动恢复可放行
                os.environ["LABMON_GPUS"] = "GPU-wsl-0:22528:24576:2"
                w.sample([])
                self.assertFalse(w.info["gpus"][0].get("unacct_busy"))
                S2 = w.snapshot({})
                self.assertEqual(S2["unacct"], set())
                pick2, why2, _ = m.place(req_gpu, S2, now + 10)
                self.assertEqual([c["uuid"] for c in pick2], ["GPU-wsl-0"])
                self.assertIsNone(why2)

    def test_allow_shared_gpu_via_sched_json_and_sim_alignment(self):
        """allow_shared_gpu = true 经 sched.json 免重启即时生效：按剩余显存共享，真放行与推演一致。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 24 GiB 卡，宿主机外占 12 GiB（free=12288, used=12288），无可见进程
            m, w, _ = self._mk_worker(d)
            now = time.time()
            with mock.patch.dict(os.environ, {"LABMON_GPUS": "GPU-wsl-0:12288:24576:20"}):
                w.sample([])
                # 默认暂停
                self.assertFalse(w.allow_shared_gpu())
                S0 = w.snapshot({})
                self.assertIn("GPU-wsl-0", S0["unacct"])

                # 通过 sched.json 开启 allow_shared_gpu（免重启 worker）
                Path(w.p.sched).parent.mkdir(parents=True, exist_ok=True)
                Path(w.p.sched).write_text(json.dumps({"allow_shared_gpu": True}), encoding="utf-8")
                w.policy()
                self.assertTrue(w.allow_shared_gpu())

                S1 = w.snapshot({})
                self.assertEqual(S1["unacct"], set())
                # 6 GiB 任务可放行
                req_small = {"key": "k_s", "kind": "queue", "project": "proj-a", "name": "small", "ngpu": 1, "allowed": None,
                             "excl": None, "vram": 6.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值",
                             "cpus": 1, "cpu_src": "缺省 1 核", "tag": None, "hold": False, "dur": 10.0}
                pick_s, why_s, _ = m.place(req_small, S1, now + 5)
                self.assertEqual([c["uuid"] for c in pick_s], ["GPU-wsl-0"])
                self.assertIsNone(why_s)

                # 16 GiB 任务放不下（可用显存已扣 12 GiB，余 ~11.5 GiB < 16 GiB），真放行与推演均挡住
                req_big = {"key": "k_b", "kind": "queue", "project": "proj-a", "name": "big", "ngpu": 1, "allowed": None,
                           "excl": None, "vram": 16.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值",
                           "cpus": 1, "cpu_src": "缺省 1 核", "tag": None, "hold": False, "dur": 10.0}
                pick_b, why_b, _ = m.place(req_big, S1, now + 5)
                self.assertIsNone(pick_b)
                self.assertIn("显存", why_b)
                sim_s = m.sim_snapshot(S1, None)
                spick_b, swhy_b, _ = m.place(req_big, sim_s, now + 5)
                self.assertIsNone(spick_b)
                self.assertIn("显存", swhy_b)

    def test_multi_gpu_isolation_exclusive_void_keepalive_and_restart_hysteresis(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # 双卡：GPU 0 宿主机外占 6 GiB，GPU 1 空闲
            m, w, dr = self._mk_worker(d)
            now = time.time()
            with mock.patch.dict(os.environ, {"LABMON_GPUS": "GPU-wsl-0:18432:24576:30,GPU-wsl-1:24576:24576:0"}):
                w.sample([])
                S = w.snapshot({})
                self.assertEqual(S["unacct"], {"GPU-wsl-0"})

                # 单卡任务自动落在空闲的 GPU 1
                req1 = {"key": "k1", "kind": "queue", "project": "proj-a", "name": "j1", "ngpu": 1, "allowed": None,
                        "excl": None, "vram": 4.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值",
                        "cpus": 1, "cpu_src": "缺省 1 核", "tag": None, "hold": False, "dur": 10.0}
                pick1, why1, _ = m.place(req1, S, now)
                self.assertEqual([c["uuid"] for c in pick1], ["GPU-wsl-1"])
                self.assertIsNone(why1)

                # 2 卡任务因仅剩 1 张未暂停卡而报 ext_gpu
                req2 = dict(req1, key="k2", name="j2", ngpu=2)
                pick2, why2, _ = m.place(req2, S, now)
                self.assertIsNone(pick2)
                self.assertIn("外部（如宿主机）占卡", why2)

                # 整机独占在有外部占卡时不放行
                req_xh = dict(req1, key="kxh", name="xh", excl="host")
                pick_xh, why_xh, _ = m.place(req_xh, S, now)
                self.assertIsNone(pick_xh)
                self.assertIn("外部", why_xh)

                # 独占任务运行中遇到所在卡外部活跃占卡：连续 2 轮触发 void_check（第 1 轮记 strike，第 2 轮作废）
                ex_j = {
                    "key": "ex1", "kind": "queue", "project": "proj-a", "name": "ex_job",
                    "root": str(dr / "projects/proj-a/ex"), "queue": "jobs.txt", "pid": 999999,
                    "gpus": ["GPU-wsl-0"], "ngpu": 1, "excl": "gpu", "t0": now - 120,
                    "est": {"vram": 2.0, "ram": 1.0, "cpus": 1}, "acct": {"vram": 2048.0, "ram": 1.0, "cpu": 1},
                    "use": {}, "stop": None,
                }
                w.running["ex1"] = ex_j
                cards_on = {id(ex_j): {"GPU-wsl-0"}}
                with mock.patch.object(w, "void_job") as vj:
                    w.void_check([ex_j], cards_on, w.gp, None, unacct=w.other["unacct"])
                    self.assertEqual(ex_j.get("void_n"), 1)
                    vj.assert_not_called()
                    w.void_check([ex_j], cards_on, w.gp, None, unacct=w.other["unacct"])
                    vj.assert_called_once()
                w.running.pop("ex1", None)

                # 保活让路：全卡无任务但存在外部活跃占卡时，保活在跑则停保活，未在跑则不拉起并记 ka_why
                w.ka_reg = str(d / "ka_reg")
                w.ka_checked = 0
                with mock.patch.object(w, "ka_groups", return_value=[123]), mock.patch.object(w, "ka_stop", return_value=None) as ks:
                    w.keepalive(False)
                    ks.assert_called_once()
                w.ka_checked = 0
                w.ka_idle = 5
                with mock.patch.object(w, "ka_groups", return_value=None), mock.patch.object(w, "ka_others", return_value=[]), \
                        mock.patch.object(w, "ka") as ka_start:
                    w.keepalive(False)
                    ka_start.assert_not_called()
                    self.assertIn("外部", w.ka_why or "")

                # 重启接管 load_prev 保留 unacct_busy 滞回状态
                w.write_status([], {}, {}, m.POLICY, {}, now)
                w2 = m.Worker("wsl-1", standalone=True)
                self.assertIn("GPU-wsl-0", w2.other["unacct"])
                # 此时降到 1200 MiB（介于 1024 与 1536 之间），因继承了 prev_busy 仍保持 busy
                os.environ["LABMON_GPUS"] = "GPU-wsl-0:23376:24576:0,GPU-wsl-1:24576:24576:0"
                w2.sample([])
                self.assertTrue(w2.info["gpus"][0]["unacct_busy"])

    def test_gpu_procs_query_failure_does_not_trigger_unacct_or_void(self):
        """回归（Codex 审查）：compute-apps 查询超时/失败时，绝不把我方在跑任务显存误判为外部占卡，也不作废独占实验。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            m, w, dr = self._mk_worker(d)
            now = time.time()
            # 我方正在跑一个独占 GPU 实验，卡总显存已用 6 GiB
            ex_j = {
                "key": "ex1", "kind": "queue", "project": "proj-a", "name": "ex_job",
                "root": str(dr / "projects/proj-a/ex"), "queue": "jobs.txt", "pid": os.getpid(),
                "gpus": ["GPU-wsl-0"], "ngpu": 1, "excl": "gpu", "t0": now - 120,
                "est": {"vram": 6.0, "ram": 1.0, "cpus": 1}, "acct": {"vram": 6144.0, "ram": 1.0, "cpu": 1},
                "use": {}, "stop": None,
            }
            w.running["ex1"] = ex_j
            meas_mod = m.Worker.sample.__globals__["_M"]
            with mock.patch.dict(os.environ, {"LABMON_GPUS": "GPU-wsl-0:18432:24576:80"}):
                os.environ.pop("LABMON_GPU_APPS", None)
                # 连续两轮模拟 nvidia-smi --query-compute-apps 超时（with_ok 返回 False）
                with mock.patch.object(meas_mod, "gpu_procs", return_value=([], False)), \
                        mock.patch.object(w, "void_job") as vj:
                    w.sample([])
                    w.sample([])
                    self.assertFalse(w.info["gpus"][0].get("unacct_busy"))
                    self.assertEqual(w.other["unacct"], {})
                    self.assertFalse(ex_j.get("void_n"))
                    vj.assert_not_called()

            # smi() 本身在 returncode != 0 或超时时返回 ([], False)
            bad_proc = mock.Mock(returncode=1, stdout="", stderr="NVML timeout")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LABMON_GPUS", None)
                os.environ.pop("LABMON_GPU_APPS", None)
                with mock.patch("subprocess.run", return_value=bad_proc):
                    self.assertEqual(m.gpu_procs(with_ok=True), ([], False))

    def test_ext_gpu_takes_precedence_over_ram_and_cpu_barrier(self):
        """回归（Codex 审查）：外部占卡暂停与内存/CPU 紧缺叠加时，仍返回 ext_gpu（bar=False），绝不成屏障挡住纯 CPU 任务。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            m, w, _ = self._mk_worker(d)
            now = time.time()
            with mock.patch.dict(os.environ, {"LABMON_GPUS": "GPU-wsl-0:18432:24576:30"}):
                w.sample([])
                S = w.snapshot({})
                # 容器内存上限 40 GiB / 配额 8 核（1 卡预留 2 核），在跑任务占 30 GiB 与 5 核：
                # GPU 请求（12 GiB / 4 核）不触发 nofit，但同时缺内存与 CPU；纯 CPU 请求（1 GiB / 1 核）资源刚好够
                S["room"], S["margin"], S["limit"] = 40.0, 2.0, 40.0
                S["cpu_cap"], S["cpu_room"] = 8, 8.0
                S["jobs"] = [{"key": "run0", "project": "proj-x", "name": "busy_cpu", "t0": now - 60, "t1": now + 600,
                              "gpus": [], "vram": {}, "ram": 30.0, "cpu": 5.0, "excl": None, "tag": None, "hold": False, "ngpu": 0, "use": {}, "cards": 0.0}]

                req_gpu = {"key": "k_gpu", "kind": "queue", "project": "proj-a", "name": "gpu_big_ram", "ngpu": 1, "allowed": None,
                           "excl": None, "vram": 4.0, "src": "提示值", "ram": 12.0, "ram_src": "提示值",
                           "cpus": 4, "cpu_src": "提示值", "tag": None, "hold": False, "dur": 20.0}
                pick, why, bar = m.place(req_gpu, S, now)
                self.assertIsNone(pick)
                self.assertFalse(bar)
                self.assertEqual(S["code"], "ext_gpu")
                self.assertIn("外部（如宿主机）占卡", why)

                # 等满 wait_cap（>30 分钟）后走 admit()，排在后面的 1 GiB / 1 核纯 CPU 任务绝不被挡住
                req_cpu = {"key": "k_cpu", "kind": "queue", "project": "proj-b", "name": "cpu_small", "ngpu": 0, "allowed": None,
                           "excl": None, "vram": 0.0, "src": "提示值", "ram": 1.0, "ram_src": "提示值",
                           "cpus": 1, "cpu_src": "提示值", "tag": None, "hold": False, "dur": 5.0}
                reqs = [
                    dict(req_gpu, since=now - 3600, charge=0.5),
                    dict(req_cpu, since=now - 60, charge=0.0),
                ]
                launched = []

                def do_place(r):
                    cs, why_, bar_ = m.place(r, S, now)
                    if cs is not None:
                        launched.append(r["name"])
                        return None
                    return why_, bar_

                waits = m.admit(reqs, set(), m.POLICY, {}, now, do_place, shadow=lambda r: None)
                self.assertEqual(launched, ["cpu_small"])
                self.assertEqual(len(waits), 1)
                self.assertEqual(waits[0][0]["name"], "gpu_big_ram")
                self.assertFalse((waits[0][2] or {}).get("barrier"))


class Wsl2BackendIntegrationTest(unittest.TestCase):
    """看板后端集成：worker_env、_to_worker、exthost、api.check/status、collect._hosts 与 gpu_idle 豁免。"""

    def test_worker_env_and_to_worker_allow_shared_gpu(self):
        h_off = {"id": "wsl-1", "standalone": True, "data_root": "~/.local/share/labmon/work", "allow_shared_gpu": False}
        h_on = {**h_off, "allow_shared_gpu": True}
        self.assertNotIn("LABMON_ALLOW_SHARED_GPU", W.worker_env(h_off))
        self.assertIn("LABMON_ALLOW_SHARED_GPU=1", W.worker_env(h_on))

        with tempfile.TemporaryDirectory() as td:
            hosts_f = Path(td) / "hosts.toml"
            hosts_f.write_text('[[hosts]]\nid = "wsl-1"\nssh = "u@203.0.113.20"\nstandalone = true\nallow_shared_gpu = true\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"LABMON_STATE": str(Path(td) / "st"), "LABMON_HOSTS": str(hosts_f)}):
                _, for_host = O._to_worker([], time.time(), {"weight": {}, "pause": {}, "focus": {}, "owner": {}})
                out_on = for_host(h_on)
                out_off = for_host(h_off)
                self.assertTrue(out_on["sched.json"]["allow_shared_gpu"])
                self.assertFalse(out_off["sched.json"]["allow_shared_gpu"])
                # 非独立主机未显式配置时不平白追加 allow_shared_gpu 键
                out_cluster = for_host({"id": "srv-a", "ssh": "u@192.0.2.1"})
                self.assertNotIn("allow_shared_gpu", out_cluster["sched.json"])

    def test_exthost_allow_shared_gpu_and_wsl_mnt_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            st_root = Path(td) / "state"
            sec_root = Path(td) / "secrets"
            hosts_f = Path(td) / "hosts.toml"
            hosts_f.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LABMON_STATE": str(st_root), "LABMON_SECRETS": str(sec_root), "LABMON_HOSTS": str(hosts_f)}):
                add_account("boss", "admin", "init")
                who = U.Ident(name="boss", role="admin", scopes=frozenset({"admin"}), via="session", key="k")
                # 校验非法布尔值被拒
                with self.assertRaises(U.Denied):
                    EH.add_external({"id": "wsl-bad", "ssh": "u@203.0.113.21", "allow_shared_gpu": "yes"}, who)

                # 新增外部机带 allow_shared_gpu = True 及 /mnt/c 路径
                res = EH.add_external({
                    "id": "wsl-home", "label": "WSL2 本机", "ssh": "u@203.0.113.22",
                    "platform_kind": "none", "data_root": "/mnt/c/labmon/work", "allow_shared_gpu": True,
                }, who)
                self.assertTrue(res["host"]["allow_shared_gpu"])

                # api.hosts 透出 allow_shared_gpu
                hl = A.hosts()["hosts"]
                self.assertTrue(next(x for x in hl if x["id"] == "wsl-home")["allow_shared_gpu"])

                # test_external 在 WSL2 + /mnt/c 下返回 9p 性能提醒但不阻断 ok
                fake_info = {"os": "Linux", "release": "5.15-microsoft-standard-WSL2", "arch": "x86_64",
                             "is_wsl": True, "py": "3.11.0", "gpus": ["RTX 4090, 24576 MiB"],
                             "data_root": "/mnt/c/labmon/work", "free_gb": 120.0, "writable": True}
                fake_run = mock.Mock(returncode=0, stdout=json.dumps(fake_info) + "\n", stderr="")
                with mock.patch.object(EH.lan, "ssh_run", return_value=fake_run):
                    tr = EH.test_external("wsl-home", who)
                self.assertTrue(tr["ok"])
                self.assertIn("ext4", tr["msg"])
                self.assertIn("warn", tr)

                # labmon check 对独立机 /mnt/c 发出 9p 警告
                chk = A.check("proj-w", 'id = "proj-w"\ntitle = "W"\n[[hosts]]\nid = "wsl-home"\n')
                self.assertTrue(any("9p" in ln or "ext4" in ln for ln in chk["after"]))

                # edit_external 关闭 allow_shared_gpu
                res2 = EH.edit_external("wsl-home", {"allow_shared_gpu": False}, who)
                self.assertFalse(res2["host"]["allow_shared_gpu"])

    def test_collect_unacct_passthrough_and_gpu_idle_exemption(self):
        """collect._hosts 将 worker 的 unacct_mb/unacct_paused 合并到项目快照，且全卡外部占卡暂停不触发 gpu_idle。"""
        now = time.time()
        cfg = {
            "id": "proj-w", "title": "W",
            "hosts": [{"id": "wsl-1", "label": "WSL2", "ssh": "", "returned": None, "mounts": [], "checks": []}],
            "queues": [],
            "alerts": {"gpu_idle_util": 5.0, "gpu_idle_min": 15, "stale_min": 5, "log_silent_min": 0},
        }
        probe = {"t": now, "gpu": [{"index": 0, "name": "RTX 4090", "util": 0, "mem_used": 6144, "mem_total": 24576}],
                 "gpu_procs": [], "cgroup": {}, "mounts": {}, "checks": []}
        glob_snap = {"hosts": [{
            "id": "wsl-1", "worker_up": True,
            "status": {
                "info": {"gpus": [{"i": 0, "uuid": "GPU-wsl-0", "used": 6144, "total": 24576, "util": 0,
                                   "unacct_mb": 6144.0, "unacct_busy": True, "unacct_why": "外部未归因显存 6.0 GiB（≥ 1.5 GiB）"}]},
                "room": {"gpus": {"GPU-wsl-0": 18432.0}, "unacct": ["GPU-wsl-0"]},
                "running": [], "waiting": [], "keepalive": {},
            },
        }]}
        snap = {"hosts": [], "lanes": [{"id": "q1", "host": "wsl-1", "label": "q1", "unfinished": 2, "no_gpu": 0,
                                        "waiting": "外部（如宿主机）占卡暂停放行"}], "jobs": [], "alerts": []}
        # 连续 20 分钟 util=0 的历史
        hist = [{"t": now - 1200 + k * 60, "host": "wsl-1", "ok": True, "util": [0], "busy": [0]} for k in range(20)]
        state: dict = {}
        with tempfile.TemporaryDirectory() as td:
            C._hosts(cfg, {"wsl-1": (probe, None)}, False, glob_snap, snap, state, hist, Path(td), now)
            hd = snap["hosts"][0]
            self.assertEqual(hd["gpus"][0]["unacct_mb"], 6144.0)
            self.assertTrue(hd["gpus"][0]["unacct_paused"])

            # 外部占卡暂停时绝不报 gpu_idle
            C._host_alerts(cfg, snap, state, hist, glob_snap, now)
            self.assertFalse(any(a["key"].startswith("gpu_idle:") for a in snap["alerts"]))

            # 解除外部占卡后，同样的 20 分钟 util=0 照常报 gpu_idle
            hd["gpus"][0].pop("unacct_paused", None)
            C._host_alerts(cfg, snap, state, hist, glob_snap, now)
            self.assertTrue(any(a["key"].startswith("gpu_idle:") for a in snap["alerts"]))

    def test_api_status_shows_unacct_lines(self):
        with tempfile.TemporaryDirectory() as td:
            st_root = Path(td) / "state"
            hosts_f = Path(td) / "hosts.toml"
            hosts_f.write_text("", encoding="utf-8")
            now = time.time()
            with mock.patch.dict(os.environ, {"LABMON_STATE": str(st_root), "LABMON_HOSTS": str(hosts_f)}):
                out_dir = PJ.out_dir("proj-w")
                out_dir.mkdir(parents=True, exist_ok=True)
                snap = {
                    "id": "proj-w", "title": "W", "generated": now, "thresholds": {"stale_s": 600},
                    "hosts": [{"id": "wsl-1", "label": "WSL2", "ok": True,
                               "gpus": [{"index": 0, "util": 0, "mem_used": 6144, "mem_total": 24576}]}],
                    "lanes": [], "jobs": [], "alerts": [], "groups": [],
                }
                (out_dir / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
                glob = {"hosts": [{
                    "id": "wsl-1", "worker_up": True,
                    "status": {
                        "info": {"gpus": [{"i": 0, "uuid": "GPU-wsl-0", "used": 6144, "total": 24576, "util": 0,
                                           "unacct_mb": 6144.0, "unacct_busy": True, "unacct_why": "外部未归因显存 6.0 GiB（≥ 1.5 GiB）"}]},
                        "room": {"gpus": {"GPU-wsl-0": 18432.0}, "unacct": ["GPU-wsl-0"]},
                        "running": [], "waiting": [], "keepalive": {},
                    },
                }]}
                r = A.status("proj-w", glob=glob, now=now)
                self.assertEqual(r["rc"], 0)
                self.assertIn("默认整卡暂停放新任务", r["out"])


if __name__ == "__main__":
    unittest.main()
