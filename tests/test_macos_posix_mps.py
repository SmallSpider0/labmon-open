"""Issue #114 阶段 4（macOS 轻量 POSIX worker + Apple Silicon MPS 单逻辑卡保守准入 + 桌面默认留 2 核）单测。"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures as FX  # noqa: E402
from labmon import api, collect, overview, worker as W, workerctl  # noqa: E402


@contextlib.contextmanager
def _iso_worker(td):
    """隔离 worker 路径到临时目录（屏蔽宿主 /proc、cgroup 与全局 HOME/LOCAL/SLOTS）。"""
    d = Path(td).resolve()
    env = FX.no_host(d)
    home = str(d / "labmon")
    local = str(d / "local")
    slots = str(d / "local" / "slots")
    with mock.patch.dict(os.environ, {**env, "LABMON_HOME": home, "LABMON_DATA_ROOT": str(d / "work"), "LABMON_LOCAL": local}, clear=False), \
            mock.patch.object(W, "HOME", home), \
            mock.patch.object(W, "DATA_ROOT", str(d / "work")), \
            mock.patch.object(W, "LOCAL", local), \
            mock.patch.object(W, "SLOTS", slots), \
            mock.patch.object(W, "PROC", env["LABMON_PROC"]), \
            mock.patch.object(W, "CG", env["LABMON_CGROUP"]), \
            mock.patch.object(W, "ROOTFS", env["LABMON_ROOTFS"]):
        yield d


SAMPLE_VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            320000.
Pages inactive:                          280000.
Pages speculative:                        12000.
Pages throttled:                              0.
Pages wired down:                        120000.
Pages purgeable:                          15000.
File-backed pages:                       200000.
Anonymous pages:                         400000.
Pages occupied by compressor:             80000.
"""

SAMPLE_PS = """  1     0     1 Ss   20960 377:39.90 /sbin/launchd
 100     1   100 S    4096   0:01.50 /usr/bin/python3
 101   100   100 S   16384   1:02.25 /bin/bash
 102   101   100 T    8192 1-02:03:04 /usr/local/bin/my train
 200     1   200 S    2048   0:00.10 /usr/bin/other
"""


class PosixParseTest(unittest.TestCase):
    def test_parse_ps_time_formats(self):
        self.assertAlmostEqual(W.parse_ps_time("0:01.50"), 1.5, places=3)
        self.assertAlmostEqual(W.parse_ps_time("377:39.90"), 377 * 60 + 39.9, places=2)
        self.assertAlmostEqual(W.parse_ps_time("01:02:03"), 3723.0, places=3)
        self.assertAlmostEqual(W.parse_ps_time("1-02:03:04"), 86400 + 2 * 3600 + 3 * 60 + 4, places=3)
        self.assertEqual(W.parse_ps_time("bad"), 0.0)
        self.assertEqual(W.parse_ps_time(""), 0.0)

    def test_parse_ps_procs_and_job_procs_no_sid_merge(self):
        """macOS ps -o sess= 恒为 0：parse_ps_procs 显式置 sid=None，job_procs 仅按 ppid + pgid 归集且识别 T 挂起态。"""
        procs = W.parse_ps_procs(SAMPLE_PS, clk_tck=100)
        self.assertEqual(set(procs), {1, 100, 101, 102, 200})
        self.assertIsNone(procs[100]["sid"])
        self.assertEqual(procs[100]["comm"], "python3")
        self.assertEqual(procs[101]["rss"], 16384 * 1024)
        self.assertEqual(procs[102]["state"], "T")
        self.assertEqual(procs[102]["comm"], "my train")
        self.assertEqual(procs[101]["ticks"], int(round(62.25 * 100)))
        j1 = {"pid": 100, "kind": "queue"}
        j2 = {"pid": 200, "kind": "queue"}
        mine = W.job_procs(procs, [j1, j2], self_pid=1)
        self.assertEqual(set(mine[id(j1)]), {100, 101, 102})
        self.assertEqual(set(mine[id(j2)]), {200})

    def test_parse_vm_stat_and_fallback_mem_of(self):
        total = 16 * 1024 ** 3
        m = W.parse_vm_stat(SAMPLE_VM_STAT, total)
        self.assertIsNotNone(m)
        self.assertEqual(m["src"], "vm_stat")
        self.assertAlmostEqual(m["limit"], 16.0, places=3)
        expected_anon = (400000 + 120000 + 80000) * 16384 / W.GIB
        self.assertAlmostEqual(m["anon"], expected_anon, places=3)
        self.assertGreaterEqual(m["used"], m["anon"])
        self.assertLessEqual(m["used"], m["limit"])
        self.assertIsNone(W.parse_vm_stat("", total))
        # mem_of 无 /proc 时回退 procs[pid]["rss"]
        procs = W.parse_ps_procs(SAMPLE_PS, clk_tck=100)
        with mock.patch.object(W, "PROC", "/nonexistent_proc_for_test"):
            self.assertEqual(W.mem_of(101, procs), 16384 * 1024)

    def test_parse_boottime_and_lstart_and_pstart_fallback(self):
        bt = W.parse_boottime("{ sec = 1794381005, usec = 776306 } Wed Nov 11 15:10:05 2026")
        self.assertAlmostEqual(bt, 1794381005.776306, places=4)
        self.assertIsNone(W.parse_boottime("invalid"))
        ls = W.parse_lstart("Fri Nov 27 11:51:05 2026")
        self.assertIsInstance(ls, int)
        self.assertGreater(ls, 1700000000)
        self.assertIsNone(W.parse_lstart(""))
        with mock.patch.object(W, "PROC", "/nonexistent_proc_for_test"), \
                mock.patch.dict(os.environ, {"LABMON_POSIX_PROBE": "1"}, clear=False):
            def fake_run(cmd, **kw):
                if cmd[0] == "sysctl":
                    return mock.Mock(returncode=0, stdout="{ sec = 1794381005, usec = 0 }\n")
                return mock.Mock(returncode=0, stdout="Fri Nov 27 11:51:05 2026\n")
            with mock.patch.object(W.common.subprocess, "run", side_effect=fake_run):
                self.assertEqual(W.pstart(12345), ls)
                self.assertAlmostEqual(W.started_at(1), 1794381005.0, places=3)
                self.assertEqual(W.started_at(12345), float(ls))

    def test_posix_fallback_gate_isolation(self):
        """默认测试夹具（假 PROC 目录）绝不触发宿主 POSIX 回退；LABMON_NO_POSIX=1 强制关闭。"""
        with mock.patch.object(W, "PROC", "/tmp/fake_proc_dir"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("LABMON_POSIX_PROBE", None)
                os.environ.pop("LABMON_NO_POSIX", None)
                self.assertFalse(W.posix_fallback_ok())
        with mock.patch.dict(os.environ, {"LABMON_POSIX_PROBE": "1", "LABMON_NO_POSIX": "1"}, clear=False):
            self.assertFalse(W.posix_fallback_ok())


class MpsAndDarwinQuotaTest(unittest.TestCase):
    def test_mps_gpu_conservative_capacity_and_override(self):
        # 8 GiB -> min(5.2, 4.0) = 4.0 GiB
        g8 = W.mps_gpu(mem={"limit": 8.0, "anon": 2.0}, chip="Apple M2")
        self.assertEqual(g8["uuid"], "mps-0")
        self.assertEqual(g8["backend"], "mps")
        self.assertEqual(g8["name"], "Apple M2 (MPS)")
        self.assertIsNone(g8["util"])
        self.assertAlmostEqual(g8["total"], 4.0 * 1024, places=1)
        self.assertAlmostEqual(g8["free"], 4.0 * 1024, places=1)
        # 16 GiB，系统已用 11 GiB -> 上限 min(10.4, 12.0)=10.4 GiB，但剩余仅 5 GiB，free 保守封顶 5 GiB
        g16 = W.mps_gpu(mem={"limit": 16.0, "anon": 11.0}, chip="Apple M4")
        self.assertAlmostEqual(g16["total"], 10.4 * 1024, places=1)
        self.assertAlmostEqual(g16["free"], 5.0 * 1024, places=1)
        self.assertAlmostEqual(g16["used"], 5.4 * 1024, places=1)
        # 64 GiB -> min(41.6, 60.0) = 41.6 GiB
        g64 = W.mps_gpu(mem={"limit": 64.0, "anon": 10.0}, chip="Apple M3 Max")
        self.assertAlmostEqual(g64["total"], 41.6 * 1024, places=1)
        # mps_vram_gb 覆盖，且不能超过物理总内存
        gov = W.mps_gpu(mem={"limit": 16.0, "anon": 4.0}, limit_gb=12.0, chip="Apple M4")
        self.assertAlmostEqual(gov["total"], 12.0 * 1024, places=1)
        gcap = W.mps_gpu(mem={"limit": 16.0, "anon": 4.0}, limit_gb=32.0, chip="Apple M4")
        self.assertAlmostEqual(gcap["total"], 16.0 * 1024, places=1)

    def test_unacct_vram_skips_mps_card(self):
        """Apple Silicon MPS 统一内存绝不误判成 WSL2 外部未归因占卡。"""
        g = W.mps_gpu(mem={"limit": 16.0, "anon": 11.0}, chip="Apple M4")
        self.assertEqual(W.unacct_vram([g], []), {})

    def test_darwin_cpu_quota_and_override(self):
        self.assertEqual(W.darwin_cpu_quota(10), 8.0)
        self.assertEqual(W.darwin_cpu_quota(8), 6.0)
        self.assertEqual(W.darwin_cpu_quota(2), 1.0)
        self.assertEqual(W.darwin_cpu_quota(1), 1.0)
        self.assertIsNone(W.darwin_cpu_quota(0))
        # ram_margin 小内存适配，64 GiB 起与原公式连续衔接
        self.assertAlmostEqual(W.ram_margin(8.0), 1.0)
        self.assertAlmostEqual(W.ram_margin(16.0), 2.0)
        self.assertAlmostEqual(W.ram_margin(32.0), 4.0)
        self.assertAlmostEqual(W.ram_margin(64.0), 8.0)
        self.assertAlmostEqual(W.ram_margin(256.0), 12.8)
        # apply_cpu_quota 允许配置覆盖 macos_default（调高或调低），仍受 nproc 封顶
        with tempfile.TemporaryDirectory() as td, _iso_worker(td):
            w = W.Worker("mac", standalone=True)
            info = {"os": "darwin", "nproc": 10, "cpu_quota": 8.0, "cpu_quota_src": "macos_default"}
            w.apply_cpu_quota(info, {"cpu_quota": 10.0})
            self.assertEqual(info["cpu_quota"], 10.0)
            self.assertEqual(info["cpu_quota_src"], "config")
            info2 = {"os": "darwin", "nproc": 10, "cpu_quota": 8.0, "cpu_quota_src": "macos_default"}
            w.apply_cpu_quota(info2, {"cpu_quota": 4.0})
            self.assertEqual(info2["cpu_quota"], 4.0)
            info3 = {"os": "darwin", "nproc": 10, "cpu_quota": 8.0, "cpu_quota_src": "macos_default"}
            w.apply_cpu_quota(info3, {"cpu_quota": 16.0})
            self.assertEqual(info3["cpu_quota"], 10.0)

    def test_mps_single_card_exclusivity_place_sim_fit_offers(self):
        """MPS 逻辑卡默认单卡互斥：真放行、推演、fit 与 offers 全部一致，纯 CPU 任务可并发放行。"""
        with tempfile.TemporaryDirectory() as td, _iso_worker(td):
            w = W.Worker("mac", standalone=True)
            g = W.mps_gpu(mem={"limit": 24.0, "anon": 4.0}, chip="Apple M4 Pro")
            w.info = {
                "os": "darwin", "nproc": 12, "cpu_quota": 10.0, "cpu_quota_src": "macos_default",
                "mem": {"limit": 24.0, "used": 6.0, "anon": 4.0, "src": "vm_stat"},
                "gpus": [g], "disk": [],
            }
            w.running = {}
            S = w.snapshot({})
            x1 = {"key": "q1", "lane": "q1", "name": "mps_a", "project": "p1", "ngpu": 1, "vram": 2.0, "src": "提示值",
                  "ram": 2.0, "ram_src": "提示值", "cpus": 2, "cpu_src": "提示值", "excl": None, "tag": None, "hold": False,
                  "dur": 10.0, "allowed": None, "cards": 1.0}
            x2 = dict(x1, key="q2", lane="q2", name="mps_b")
            xcpu = dict(x1, key="q3", lane="q3", name="cpu_job", ngpu=0, vram=0.0, cards=0.0)
            x2gpu = dict(x1, key="q4", lane="q4", name="need2", ngpu=2, cards=2.0)

            cs1, why1, res1 = W.place(x1, S, 1000.0)
            self.assertEqual([c["uuid"] for c in cs1], ["mps-0"])
            self.assertIsNone(why1)
            # 模拟 x1 已在跑
            S["jobs"].append(W.acct_job(x1, cs1, 1000.0, 1600.0, S["cpu_cap"], W.snap_ngpus(S)))
            # 第 2 个 MPS 任务即便显存充足也必须因单卡互斥排队（res=True）
            cs2, why2, res2 = W.place(x2, S, 1000.0)
            self.assertIsNone(cs2)
            self.assertEqual(S["code"], "mps_busy")
            self.assertIn("单卡互斥", why2)
            self.assertTrue(res2)
            # 纯 CPU 任务不受 MPS 互斥阻挡
            cs_cpu, why_cpu, _ = W.place(xcpu, S, 1000.0)
            self.assertEqual(cs_cpu, [])
            self.assertIsNone(why_cpu)
            # 请求 2 张卡直接判放不下
            cs_2g, _, res_2g = W.place(x2gpu, S, 1000.0)
            self.assertIsNone(cs_2g)
            self.assertEqual(S["code"], "nofit_gpu_count")
            self.assertFalse(res_2g)

            # 推演：两个 MPS 任务必须串行排开
            S0 = w.snapshot({})
            qs = [
                {"key": "q1", "kind": "queue", "project": "p1", "allowed": None, "since": 1000.0, "jobs": [dict(x1, deps=[], deps_any=[])]},
                {"key": "q2", "kind": "queue", "project": "p1", "allowed": None, "since": 1001.0, "jobs": [dict(x2, deps=[], deps_any=[])]},
            ]
            out, stuck = W.simulate(1000.0, S0, qs, set())
            self.assertEqual(stuck, {})
            self.assertEqual(len(out), 2)
            self.assertEqual(out[0]["t0"], 1000.0)
            self.assertEqual(out[1]["t0"], out[0]["t1"])

            # offers.local_cards 不对 MPS 逻辑卡建议同机多开车道
            cards_ok, why_lc = w.local_cards()
            self.assertIsNone(cards_ok)
            self.assertIn("MPS", why_lc)

            # FitHost.card_lines 标 (MPS) 且不显示“样本不够”
            st = {"t": 1000.0, "info": w.info, "running": [], "external": [], "gpu_procs": [],
                  "room": {"ram": 20.0, "margin": 3.0, "gpus": {"mps-0": g["free"]}, "cpu": 10.0, "cpu_cap": 10, "owner": [], "unacct": []},
                  "limits": {}, "overrides": []}
            fh = W.FitHost("mac", st, [], {})
            lines = "\n".join(fh.card_lines())
            self.assertIn("卡 0 (MPS)", lines)
            self.assertIn("—（MPS）", lines)
            self.assertIn("默认留 2 核给桌面", lines)

    def test_darwin_no_gpu_reason_without_nvidia_smi_error(self):
        """Intel Mac 或禁用 MPS（无 GPU）请求 gpus=1 报 no_gpu_darwin，不误报 nvidia-smi。"""
        S = {"cards": [], "ngpus": 0, "os": "darwin", "limit": 16.0, "room": 12.0, "margin": 2.0,
             "cpu_cap": 6, "cpu_room": 6.0, "jobs": [], "ext": [], "gp": [], "cpu": None,
             "holds": {}, "tags": {}, "owner": set(), "unacct": set(), "unacct_info": {},
             "who": "labmon 之外的进程", "kept": False, "cut": None, "brief": False, "gholds": {}, "gangs": {}, "gfirst": {}}
        x = {"key": "q1", "lane": "q1", "name": "g1", "project": "p1", "ngpu": 1, "vram": 1.0, "src": "提示值",
             "ram": 1.0, "ram_src": "提示值", "cpus": 1, "cpu_src": "提示值", "excl": None, "tag": None, "hold": False,
             "dur": 5.0, "allowed": None, "cards": 1.0}
        cs, why, res = W.place(x, S, 1000.0)
        self.assertIsNone(cs)
        self.assertEqual(S["code"], "no_gpu_darwin")
        self.assertIn("gpus=0", why)
        self.assertNotIn("nvidia-smi", why)
        self.assertFalse(res)

    def test_accel_env_and_mps_oom_and_queue_start_and_slot(self):
        # accel_env 唯一实现
        e_mps = W.accel_env({}, [{"i": 0, "uuid": "mps-0", "backend": "mps"}])
        self.assertEqual((e_mps["CUDA_VISIBLE_DEVICES"], e_mps["LABMON_ACCEL"]), ("", "mps"))
        e_cuda = W.accel_env({}, [{"i": 0, "uuid": "GPU-abc"}])
        self.assertEqual((e_cuda["CUDA_VISIBLE_DEVICES"], e_cuda["LABMON_ACCEL"]), ("GPU-abc", "cuda"))
        e_cpu = W.accel_env({}, [])
        self.assertEqual((e_cpu["CUDA_VISIBLE_DEVICES"], e_cpu["LABMON_ACCEL"]), ("", "cpu"))
        # MPS OOM 匹配
        self.assertIsNotNone(W.CUDA_OOM.search("RuntimeError: MPS backend out of memory (MPS allocated: 9.8 GB)"))

        # 真启动清单任务：校验 CUDA_VISIBLE_DEVICES="" + LABMON_ACCEL=mps + LABMON_CPUS 与日志分隔行
        with tempfile.TemporaryDirectory() as td, _iso_worker(td) as d:
            proj = d / "work" / "projects" / "p1"
            (proj / "state").mkdir(parents=True)
            (proj / "logs").mkdir(parents=True)
            out_env = proj / "got_env.txt"
            (proj / "jobs.txt").write_text(f'j1|echo "C=$CUDA_VISIBLE_DEVICES A=$LABMON_ACCEL N=$LABMON_CPUS" > {out_env}\n')
            w = W.Worker("mac", standalone=True)
            os.makedirs(w.p.queues, exist_ok=True)
            g = W.mps_gpu(mem={"limit": 16.0, "anon": 4.0}, chip="Apple M4")
            w.info = {"os": "darwin", "nproc": 10, "cpu_quota": 8.0, "cpu_quota_src": "macos_default",
                      "mem": {"limit": 16.0, "used": 6.0, "anon": 4.0, "src": "vm_stat"}, "gpus": [g], "disk": []}
            q = {"key": "q1", "root": str(proj), "jobs": "jobs.txt", "jobs_path": str(proj / "jobs.txt"),
                 "state": str(proj / "state"), "logs": str(proj / "logs"), "env": None, "project": "p1", "stop": "term", "gpus": None, "par": 1}
            job = {"name": "j1", "cmd": f'echo "C=$CUDA_VISIBLE_DEVICES A=$LABMON_ACCEL N=$LABMON_CPUS" > {out_env}',
                   "ngpu": 1, "vram": 1.0, "ram": 1.0, "cpus": 12, "excl": None, "tag": None, "est": {"cpus": 12, "ram": 1.0, "vram": 1.0}}
            res = w.start(q, job, [g])
            self.assertIsNone(res)
            deadline = time.time() + 5.0
            while time.time() < deadline and not (proj / "state" / "j1.rc").exists():
                time.sleep(0.05)
            try:
                os.waitpid(w.running[next(iter(w.running))]["pid"], 0)
            except OSError:
                pass
            self.assertTrue(out_env.exists())
            # cpus=12 应被 macOS 默认留 2 核（10-2=8）封顶到 8
            self.assertEqual(out_env.read_text().strip(), "C= A=mps N=8")
            log_txt = (proj / "logs" / "j1.log").read_text()
            self.assertIn("GPU 0 (MPS)", log_txt)
            self.assertIsNotNone(W.START_RE.search(log_txt))

            # 单元 grant 写 .ok 含 accel="mps"，且 slot_main 按 .ok 导出 CUDA_VISIBLE_DEVICES="" 与 LABMON_ACCEL=mps
            slot_req = {"id": "s1", "key": "slot:s1", "project": "p1", "name": "u1", "pid": os.getpid(),
                        "ngpu": 1, "est": {"cpus": 2, "ram": 1.0, "vram": 1.0}}
            os.makedirs(W.SLOTS, exist_ok=True)
            w.grant(slot_req, [g])
            ok_data = json.loads((Path(W.SLOTS) / "s1.ok").read_text())
            self.assertEqual(ok_data["gpus"], ["mps-0"])
            self.assertEqual(ok_data["accel"], "mps")
            slot_env_out = proj / "slot_env.txt"
            script = str(Path(__file__).resolve().parent.parent / "labmon" / "labmon_worker.py")
            senv = dict(os.environ, LABMON_HOME=W.HOME, LABMON_DATA_ROOT=W.DATA_ROOT,
                        LABMON_LOCAL=W.LOCAL, LABMON_SLOTS=W.SLOTS)
            sp = subprocess.Popen(
                [sys.executable, script, "slot", "--host", "mac", "--project", "p1", "--name", "s_mps",
                 "--gpus", "1", "--timeout", "5", "--", "sh", "-c",
                 f'echo "C=$CUDA_VISIBLE_DEVICES A=$LABMON_ACCEL" > {slot_env_out}'],
                env=senv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                req_f = None
                dl = time.time() + 4.0
                while time.time() < dl and not req_f:
                    reqs = list(Path(W.SLOTS).glob("*.req"))
                    if reqs:
                        req_f = reqs[0]
                    else:
                        time.sleep(0.05)
                self.assertIsNotNone(req_f)
                rid = req_f.stem
                w.grant({"id": rid, "key": "slot:" + rid, "project": "p1", "name": "s_mps",
                         "pid": sp.pid, "ngpu": 1, "est": {"cpus": 2, "ram": 1.0, "vram": 1.0}}, [g])
                sp.wait(timeout=5)
                self.assertEqual(sp.returncode, 0)
                self.assertEqual(slot_env_out.read_text().strip(), "C= A=mps")
            finally:
                if sp.poll() is None:
                    sp.kill()
                    sp.wait(timeout=2)
                if sp.stdout:
                    sp.stdout.close()
                if sp.stderr:
                    sp.stderr.close()

            # 无旧状态时 adopt 找回单逻辑 MPS 卡
            w2 = W.Worker("mac", standalone=True)
            w2.prev = {}
            with mock.patch.object(W.measure, "gpu_table", return_value=[g]), \
                    mock.patch.object(W.common, "alive", return_value=True):
                w2.adopt(q, ("j1", 99999, 1000.0, "mac"))
            adopted = next(iter(w2.running.values()))
            self.assertEqual(adopted["gpus"], ["mps-0"])

    def test_sample_cpu_used_from_ps_ticks_diff_and_mps_vram_policy(self):
        with tempfile.TemporaryDirectory() as td, _iso_worker(td):
            w = W.Worker("mac", standalone=True)
            os.makedirs(w.p.queues, exist_ok=True)
            # sched.json 下发 mps_vram_gb 立即生效
            W.write(w.p.sched, json.dumps({"standalone": True, "mps_vram_gb": 11.5}))
            pol = w.policy()
            self.assertEqual(pol["mps_vram_gb"], 11.5)
            self.assertEqual(w._mps_vram_gb, 11.5)
            # cpu_ns is None 时通过两次 scan_procs 差分计算 cpu_used
            p1 = {10: {"comm": "py", "state": "R", "ppid": 1, "pgid": 10, "sid": None, "ticks": 100, "hpid": None, "rss": 1024}}
            p2 = {10: {"comm": "py", "state": "R", "ppid": 1, "pgid": 10, "sid": None, "ticks": 300, "hpid": None, "rss": 1024}}
            t_now = [1000.0]
            with mock.patch.object(W.common.time, "time", side_effect=lambda: t_now[0]), \
                    mock.patch.object(W.measure, "scan_procs", side_effect=[p1, p2]), \
                    mock.patch.object(W.measure, "gpu_table", return_value=[]), \
                    mock.patch.object(W.measure, "gpu_procs", return_value=([], True)), \
                    mock.patch.object(W.measure, "host_info", return_value={"gpus": [], "nproc": 8, "cpu_quota": 6.0, "cpu_ns": None, "mem": {}, "disk": [], "os": "darwin"}):
                w.sample([])
                self.assertNotIn("cpu_used", w.info)
                t_now[0] = 1002.0
                w.sample([])
                hz = os.sysconf("SC_CLK_TCK")
                self.assertAlmostEqual(w.info["cpu_used"], round(200 / hz / 2.0, 2), places=2)

    def test_linux_no_cgroup_reaped_child_not_double_counted(self):
        """回归 Codex P2 #1：Linux 无 cgroup 独立主机上，子进程退出被父进程回收（cutime/cstime 转入父进程）绝不能重复累计为本轮 cpu_used；
        有 /proc/stat 时改走全机非空闲 ticks 差分，无 /proc/stat 时也不对含 cutime/cstime 的 Linux /proc 进程表做 PID 交集求差。"""
        with tempfile.TemporaryDirectory() as td, _iso_worker(td) as d:
            w = W.Worker("lin_standalone", standalone=True)
            os.makedirs(w.p.queues, exist_ok=True)
            hz = os.sysconf("SC_CLK_TCK")
            # 1) 无 /proc/stat 且为 Linux 进程（带整数 sid，ticks 含 cutime/cstime）：子进程 20 退出被父进程 10 回收，不应产生虚假 cpu_used=5.0
            p1 = {10: {"comm": "sh", "state": "S", "ppid": 1, "pgid": 10, "sid": 10, "ticks": 0, "hpid": None},
                  20: {"comm": "py", "state": "S", "ppid": 10, "pgid": 10, "sid": 10, "ticks": 10 * hz, "hpid": None}}
            p2 = {10: {"comm": "sh", "state": "S", "ppid": 1, "pgid": 10, "sid": 10, "ticks": 10 * hz, "hpid": None}}
            t_now = [1000.0]
            with mock.patch.object(W.common.time, "time", side_effect=lambda: t_now[0]), \
                    mock.patch.object(W.measure, "scan_procs", side_effect=[p1, p2]), \
                    mock.patch.object(W.measure, "gpu_table", return_value=[]), \
                    mock.patch.object(W.measure, "gpu_procs", return_value=([], True)), \
                    mock.patch.object(W.measure, "host_info", return_value={"gpus": [], "nproc": 8, "cpu_quota": 8.0, "cpu_ns": None, "mem": {}, "disk": []}):
                w.sample([])
                t_now[0] = 1002.0
                w.sample([])
                self.assertIsNone(w.info.get("cpu_used"))

            # 2) 有 /proc/stat 时走 host_cpu_ticks 全机非空闲计数差分（期间子进程被回收但全机仅新增 1 CPU 秒 -> 0.5 核）
            proc_dir = d / "fake_proc_stat"
            proc_dir.mkdir()
            stat_f = proc_dir / "stat"
            stat_f.write_text(f"cpu  {100 * hz} 0 {20 * hz} 50000 0 0 0 0 0 0\n")
            t_now2 = [2000.0]
            with mock.patch.object(W, "PROC", str(proc_dir)), \
                    mock.patch.object(W.common.time, "time", side_effect=lambda: t_now2[0]), \
                    mock.patch.object(W.measure, "scan_procs", side_effect=[p1, p2]), \
                    mock.patch.object(W.measure, "gpu_table", return_value=[]), \
                    mock.patch.object(W.measure, "gpu_procs", return_value=([], True)), \
                    mock.patch.object(W.measure, "host_info", return_value={"gpus": [], "nproc": 8, "cpu_quota": 8.0, "cpu_ns": None, "mem": {}, "disk": []}):
                w.sample([])
                stat_f.write_text(f"cpu  {101 * hz} 0 {20 * hz} 51000 0 0 0 0 0 0\n")
                t_now2[0] = 2002.0
                w.sample([])
                self.assertAlmostEqual(w.info["cpu_used"], 0.5, places=2)

    def test_mps_unmeasured_vram_keeps_hint_and_does_not_write_zero(self):
        """回归 Codex P2 #2：Apple Silicon MPS 无单进程显存采集时，measure() 与 usage_rec() 将 vram_peak 记为 None（不写 0）；
        estimate() 遇到 vram_peak=None 的 MPS 成功历史时保留清单 vram= 提示值（同时仍可复用该条实测 ram_peak），调小 mps_vram_gb 后正确拦截。"""
        with tempfile.TemporaryDirectory() as td, _iso_worker(td):
            w = W.Worker("mac", standalone=True)
            os.makedirs(w.p.queues, exist_ok=True)
            g = W.mps_gpu(mem={"limit": 16.0, "anon": 4.0}, chip="Apple M4")
            w.info = {"os": "darwin", "nproc": 10, "cpu_quota": 8.0, "mem": {"limit": 16.0, "used": 6.0, "anon": 4.0, "src": "vm_stat"}, "gpus": [g], "disk": []}
            j = {"name": "train_s0", "project": "p1", "pid": 101, "t0": 1000.0, "gpus": ["mps-0"], "ngpu": 1,
                 "vram": 8.0, "ram": 2.0, "use": {}, "est": {"vram": 8.0, "ram": 2.0, "cpus": 2}}
            procs = {101: {"comm": "python3", "state": "R", "ppid": 1, "pgid": 101, "sid": None, "ticks": 50, "hpid": None, "rss": 2 * 1024 ** 3}}
            w.measure(j, [101], procs, [], {"mps-0": None}, {}, [j], 1010.0, mps_cards={"mps-0"})
            self.assertIsNone(j["use"]["vram"])
            self.assertIsNone(j["use"]["vram_peak"])
            self.assertAlmostEqual(j["use"]["ram_peak"], 2.0, places=2)
            rec = w.usage_rec(j, "0", None, False)
            self.assertIsNone(rec["vram_peak"])
            # 下一条带 vram=8 的同名任务经 estimate() 必须保留提示值 8.0（绝不被 0 覆盖），而 ram 正常取实测 2.0 * 1.1 = 2.2
            req = {"name": "train_s1", "project": "p1", "vram": 8.0, "ram": 1.0, "cpus": None, "tag": None, "root": "/r", "queue": "q.txt"}
            est = W.estimate([rec], req)
            self.assertEqual(est["vram"], 8.0)
            self.assertEqual(est["src"], "提示值")
            self.assertAlmostEqual(est["ram"], 2.2, places=2)
            self.assertIn("同名", est["ram_src"])
            # 调小 mps_vram_gb=6 后，vram=8 必须因显存放不下被拦截
            g_small = W.mps_gpu(mem={"limit": 16.0, "anon": 4.0}, limit_gb=6.0, chip="Apple M4")
            w.info["gpus"] = [g_small]
            S = w.snapshot({})
            x = {"key": "q1", "lane": "q1", "name": "train_s1", "project": "p1", "ngpu": 1, "vram": est["vram"], "src": est["src"],
                 "ram": est["ram"], "ram_src": est["ram_src"], "cpus": est["cpus"], "cpu_src": est["cpu_src"],
                 "excl": None, "tag": None, "hold": False, "dur": 10.0, "allowed": None, "cards": 1.0}
            cs, _, res = W.place(x, S, 1020.0)
            self.assertIsNone(cs)
            self.assertEqual(S["code"], "nofit_vram")
            self.assertFalse(res)


class BoardAndWorkerctlMacosTest(unittest.TestCase):
    def test_workerctl_env_and_portable_daemon_start(self):
        h = {"worker_home": "/Users/u/work/labmon", "data_root": "/Users/u/work", "python": "python3",
             "shared": [], "standalone": True, "mps_vram_gb": 10.5, "cpu_quota": 6}
        env_s = workerctl.worker_env(h)
        self.assertIn("LABMON_MPS_VRAM_GB=10.5", env_s)
        self.assertIn("LABMON_CPU_QUOTA=6", env_s)
        with mock.patch.object(workerctl, "host_conf", return_value=h), \
                mock.patch.object(workerctl.LS, "need"), \
                mock.patch.object(workerctl, "committed", return_value="worker"), \
                mock.patch.object(workerctl, "ssh", return_value=(0, "ok")) as ssh, \
                mock.patch.object(workerctl.subprocess, "run", return_value=mock.Mock(returncode=0)):
            workerctl.run_out("start", "mac", [])
        cmd = ssh.call_args.args[1]
        self.assertNotIn("setsid", cmd)
        self.assertIn("--replace --daemon --standalone", cmd)
        self.assertIn("base64 -D", cmd)

    def test_overview_to_worker_sends_mps_vram_gb(self):
        _, for_host = overview._to_worker([], 1000.0, {}, {})
        h = {"id": "mac", "standalone": True, "mps_vram_gb": 9.5, "cpu_quota": 6}
        payload = for_host(h)
        self.assertEqual(payload["sched.json"]["mps_vram_gb"], 9.5)
        self.assertEqual(payload["sched.json"]["cpu_quota"], 6)

    def test_collect_synthesizes_mps_gpu_and_skips_gpu_idle_alert(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            cfg = {"id": "p1", "alerts": {"gpu_idle_min": 20, "gpu_idle_util": 5.0},
                   "hosts": [{"id": "mac", "label": "MacBook", "ssh": None, "returned": None, "mounts": [], "checks": []}]}
            raw = {"t": 2000.0, "gpu": [], "gpu_procs": [], "cgroup": {}, "loadavg": [1.0, 1.0, 1.0], "mounts": {}, "checks": []}
            g_mps = {"i": 0, "uuid": "mps-0", "name": "Apple M4 (MPS)", "backend": "mps", "total": 10649.6, "used": 2048.0, "free": 8601.6, "util": None}
            glob = {"hosts": [{"id": "mac", "worker_up": True, "status": {"info": {"gpus": [g_mps]}, "room": {"unacct": []}}}]}
            snap = {"hosts": [], "alerts": [], "lanes": [{"id": "l1", "host": "mac", "label": "l1", "unfinished": 2, "no_gpu": 0}], "jobs": []}
            hist = [{"t": 2000.0 - 1500, "host": "mac", "ok": True, "util": [None]}, {"t": 2000.0, "host": "mac", "ok": True, "util": [None]}]
            state = {}
            collect._hosts(cfg, {"mac": (raw, None)}, False, glob, snap, state, hist, d, 2000.0)
            self.assertEqual(len(snap["hosts"]), 1)
            gpus = snap["hosts"][0]["gpus"]
            self.assertEqual(len(gpus), 1)
            self.assertEqual(gpus[0]["backend"], "mps")
            self.assertEqual(gpus[0]["name"], "Apple M4 (MPS)")
            # api._card 对 MPS 标 (MPS) 不输出 ?%
            self.assertIn("(MPS)", api._card(gpus[0]))
            self.assertNotIn("?%", api._card(gpus[0]))
            # _host_alerts 绝不误报 gpu_idle
            collect._host_alerts(cfg, snap, state, hist, glob, 2000.0)
            self.assertFalse(any(a["key"].startswith("gpu_idle:") for a in snap["alerts"]))


@unittest.skipUnless(sys.platform == "darwin", "仅在 macOS 上运行原生命令实测")
class RealDarwinIntegrationTest(unittest.TestCase):
    def test_real_darwin_probes_and_daemon_lifecycle(self):
        with mock.patch.object(W, "PROC", "/proc"), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LABMON_NO_POSIX", None)
            os.environ.pop("LABMON_POSIX_PROBE", None)
            self.assertTrue(W.posix_fallback_ok())
            m = W.darwin_mem_info()
            self.assertIsNotNone(m)
            self.assertEqual(m["src"], "vm_stat")
            self.assertGreater(m["limit"], 1.0)
            self.assertGreaterEqual(m["used"], m["anon"])
            procs = W.scan_procs()
            self.assertIn(os.getpid(), procs)
            self.assertIsNone(procs[os.getpid()]["sid"])
            self.assertGreater(W.mem_of(os.getpid(), procs), 0)
            self.assertIsNotNone(W.pstart(os.getpid()))
            self.assertGreater(W.started_at(1), 1500000000)
            if W.is_apple_silicon():
                g = W.mps_gpu(mem=m)
                self.assertIsNotNone(g)
                self.assertEqual(g["backend"], "mps")
                self.assertGreater(g["total"], 0)
                self.assertGreaterEqual(g["free"], 0)
                self.assertLessEqual(g["free"], g["total"])

        # 隔离临时目录实跑 --daemon 启动与 stop 清理（无 setsid）
        with tempfile.TemporaryDirectory(prefix="labmon_mac_test_") as td:
            d = Path(td).resolve()
            env = dict(os.environ, LABMON_HOME=str(d / "labmon"), LABMON_DATA_ROOT=str(d),
                       LABMON_LOCAL=str(d / "local"), LABMON_TICK="1",
                       LABMON_NO_NOTIFY="1", LABMON_NO_KEYCHAIN="1")
            script = str(Path(__file__).resolve().parent.parent / "labmon" / "labmon_worker.py")
            r = subprocess.run([sys.executable, script, "run", "--host", "mac_it", "--standalone", "--daemon"],
                               env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            pid_file = d / "labmon" / "hosts" / "mac_it" / "worker.pid"
            st_file = d / "labmon" / "hosts" / "mac_it" / "status.json"
            deadline = time.time() + 10.0
            try:
                while time.time() < deadline and not st_file.exists():
                    time.sleep(0.2)
                self.assertTrue(pid_file.exists(), "daemon 未写出 worker.pid")
                self.assertTrue(st_file.exists(), "daemon 未写出 status.json")
                st = json.loads(st_file.read_text())
                self.assertEqual(st["info"]["os"], "darwin")
                self.assertEqual(st["info"]["mem"]["src"], "vm_stat")
                self.assertEqual(st["info"]["cpu_quota"], float(max(1, (os.cpu_count() or 1) - 2)))
                if W.is_apple_silicon():
                    self.assertEqual(len(st["info"]["gpus"]), 1)
                    self.assertEqual(st["info"]["gpus"][0]["backend"], "mps")
            finally:
                subprocess.run([sys.executable, script, "stop", "--host", "mac_it"],
                               env=env, capture_output=True, text=True, timeout=10)
                end_t = time.time() + 8.0
                wpid = int(pid_file.read_text().strip()) if pid_file.exists() and pid_file.read_text().strip().isdigit() else 0
                while wpid and time.time() < end_t:
                    try:
                        os.kill(wpid, 0)
                        time.sleep(0.1)
                    except OSError:
                        break
                if wpid:
                    try:
                        os.kill(wpid, 9)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
