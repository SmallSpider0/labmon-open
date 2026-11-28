"""Issue #114 阶段 2（PR 2）专项测试：
1. 独立机历史转送 hist_shared.json（经 overview.fetch stdin 避 ARG_MAX、仅喂 estimate/duration、严格隔离 share/dep_state/disk_ends/_idle_start、普通用户外部机不泄露他人历史）；
2. 独立机跨机语义显式关闭（peers 短路、dep_block/gang_block 与 api.check 提示）；
3. 本地工作盘防撑爆与空间管理（disk_gate 低水位/软配额/滞回恢复/不杀在跑任务、WorkDisk 仅扫 data_root 且 follow_symlinks=False、host_work_disk 14 天可回收候选、api.disk 区分共享盘与本地工作盘）；
4. labmon worker clean / clean_local（默认 dry-run、仅删 LOCAL/{cache,jobs}、跳过在跑任务 scratch、绝不动 projects/ 产物、普通用户需指定本人 --project）；
5. 外部主机双入口（exthost + REST API /api/hosts/external* + CLI labmon host add/edit/test/key/rm）：零私钥上传、专用 Ed25519 密钥生成/轮换/删机销毁、防 SSRF/选项注入/撞机接管、普通用户上限 3 台与可见性隔离、备用拒写。
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

from fixtures import add_account, bearer_opener, make_token, worker_module  # noqa: E402
from labmon import api as A  # noqa: E402
from labmon import config as CF  # noqa: E402
from labmon import disk as DK  # noqa: E402
from labmon import exthost as EH  # noqa: E402
from labmon import lan as L  # noqa: E402
from labmon import lease as LS  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import platform as PF  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import serve as SV  # noqa: E402
from labmon import state as ST  # noqa: E402
from labmon import users as U  # noqa: E402


def _http_json(opener, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request
    data = json.dumps(body or {}).encode() if method in ("POST", "PUT", "DELETE") else None
    hd = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=hd, method=method)
    try:
        with opener.open(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, json.loads(raw) if raw else {}


class StandaloneHistAndCrossHostTest(unittest.TestCase):
    """独立机历史转送、语义隔离、跨机依赖/成组阻断与 overview.fetch stdin 下发。"""

    def test_hist_shared_feeds_estimate_and_isolates_share_deps_disk(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            data_root = d / "work"
            home = d / "home"
            local = data_root / ".tmp"
            m = worker_module(d, LABMON_LOCAL=str(local), LABMON_DATA_ROOT=str(data_root), LABMON_STANDALONE="1")
            w = m.Worker("ext-1", standalone=True)
            now = 1_760_000_000.0
            # 下发共享历史（来自集群主机 srv-a）
            shared_rows = [
                {"host": "srv-a", "project": "proj-a", "name": "train_s1", "tag": "t1", "gpu": "RTX 4090",
                 "min": 45.0, "est_min": 30.0, "vram_peak": 12288.0, "ram_peak": 16.0, "cores_peak": 4.0,
                 "give": 4, "rc": "0", "kind": "queue", "t0": now - 7200, "t1": now - 4500,
                 "gpus": ["GPU-0"], "ngpu": 1, "conc": 1.0, "root": "/data/proj-a", "queue": "jobs/q.txt"},
                {"host": "srv-a", "project": "proj-other", "name": "other_s1", "tag": None, "gpu": "RTX 4090",
                 "min": 120.0, "est_min": 120.0, "vram_peak": 8192.0, "ram_peak": 8.0, "cores_peak": 2.0,
                 "give": 2, "rc": "0", "kind": "queue", "t0": now - 3600, "t1": now - 600,
                 "gpus": ["GPU-0"], "ngpu": 1, "conc": 1.0, "root": "/data/proj-other", "queue": "jobs/o.txt"},
            ]
            Path(w.p.hist_shared).parent.mkdir(parents=True, exist_ok=True)
            Path(w.p.hist_shared).write_text(json.dumps({"generated": now, "rows": shared_rows}), encoding="utf-8")
            # 本地再记一条记录验证去重与本地数据正常参与
            m.append_jsonl(w.p.usage, {
                "host": "ext-1", "project": "proj-a", "name": "local_done", "rc": "0", "kind": "queue",
                "t0": now - 1000, "t1": now - 400, "min": 10.0, "vram_peak": 2048.0, "ram_peak": 4.0,
                "cores_peak": 2.0, "gpus": ["GPU-1"], "root": str(data_root / "projects/proj-a"), "queue": "q.txt",
            })
            w.reload_hist()
            self.assertEqual(len([r for r in w.hist if r.get("shared_hist")]), 2)
            # 1. estimate 复用共享历史
            req_a = {"project": "proj-a", "name": "train_s2", "tag": "t1", "root": str(data_root / "projects/proj-a"),
                     "queue": "q.txt", "est_min": 30.0, "vram": 0, "ram": 0, "cpus": 1}
            est = m.estimate(w.hist, req_a)
            self.assertAlmostEqual(est["vram"], 12.0 * 1.1, delta=0.5)
            self.assertAlmostEqual(est["ram"], 16.0 * 1.1, delta=0.5)
            # 2. duration 复用共享历史
            dur = m.duration(w.hist, req_a, model="RTX 4090")
            self.assertIsNotNone(dur)
            self.assertGreater(dur["min"], 35.0)
            # 3. share 严格跳过 shared_hist（proj-other 绝不能计入独立机公平份额）
            sh = w.share(now, w.policy())
            self.assertNotIn("proj-other", sh)
            # 4. dep_state 严格跳过 shared_hist（不能把远端 srv-a 的 train_s1 当作本机已完成依赖）
            w.running = {"r1": {"key": "r1", "project": "proj-a", "name": "waiter", "deps": ["train_s1"], "deps_any": []}}
            w.dep_state([], now)
            self.assertNotIn(("proj-a", "train_s1"), w.dep_idx)
            self.assertEqual(w.dep_idx.get(("proj-a", "local_done")), "done")
            w.running = {}
            # 5. disk_ends 严格跳过 shared_hist（不给远端任务触发 hint）
            w.wdisk.ix = {"units": {}}
            w.disk_ends(now)
            self.assertTrue(all(k[2] != "train_s1" for k in w.hinted))
            # 6. peers 在独立模式短路不扫描同 worker_home 别机目录
            peer_dir = home / "hosts" / "fake-peer"
            peer_dir.mkdir(parents=True)
            (peer_dir / "status.json").write_text(json.dumps({"t": now, "running": [], "queues": []}))
            self.assertEqual(w.peers(now), [])

    def test_standalone_dep_gang_block_and_api_check_hint(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            data_root = d / "work"
            local = data_root / ".tmp"
            m = worker_module(d, LABMON_LOCAL=str(local), LABMON_DATA_ROOT=str(data_root), LABMON_STANDALONE="1")
            w = m.Worker("ext-1", standalone=True)
            now = time.time()
            w.dep_state([], now)
            req_dep = {"key": "k1", "kind": "queue", "project": "proj-a", "name": "step2", "deps": ["remote_job"], "deps_any": []}
            # 超过 DEP_MISS_S 判定 missing 并提示独立主机不共享 worker_home
            w.dep_block(req_dep, now)
            st, why = w.dep_block(req_dep, now + m.DEP_MISS_S + 10)
            self.assertEqual(st, "missing")
            self.assertIn("独立主机", why)
            req_gang = {"key": "k1", "kind": "queue", "project": "proj-a", "name": "r0", "gang": "ddp", "gn": 2}
            g_st, g_why = w.gang_block(req_gang, now)
            self.assertEqual(g_st, "wait")
            self.assertIn("独立主机不支持跨主机成组", g_why)
            g_st2, g_why2 = w.gang_block(req_gang, now + m.DEP_MISS_S + 10)
            self.assertEqual(g_st2, "missing")
            self.assertIn("独立主机不支持跨主机成组", g_why2)

    def test_overview_hist_payload_privacy_and_fetch_stdin_put(self):
        """看板裁剪 hist_shared.json：普通用户归属主机仅下发其名下项目历史；fetch put 走 stdin 支持超大载荷。"""
        with tempfile.TemporaryDirectory() as td:
            st_root = Path(td) / "state"
            hosts_f = Path(td) / "hosts.toml"
            hosts_f.write_text(
                '[[hosts]]\nid = "srv-a"\nssh = "u@192.0.2.10"\n\n'
                '[[hosts]]\nid = "ext-amy"\nssh = "u@203.0.113.10"\nplatform_kind = "other"\nstandalone = true\nowner = "amy"\nusers = ["amy"]\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"LABMON_STATE": str(st_root), "LABMON_HOSTS": str(hosts_f), "LABMON_NO_NOTIFY": "1", "LABMON_NO_KEYCHAIN": "1"}):
                add_account("boss", "admin", "init")
                add_account("amy", "user", "boss")
                add_account("bob", "user", "boss")
                PJ.put_config("proj-a", 'id = "proj-a"\ntitle = "A"\n', {}, None, owner="amy", by="test", ops=True)
                PJ.put_config("proj-b", 'id = "proj-b"\ntitle = "B"\n', {}, None, owner="bob", by="test", ops=True)
                now = time.time()
                # 经 _keep_usage 真实路径落盘用量记录
                done_rows = [
                    {"t0": now - 200, "t1": now - 100, "project": "proj-a", "name": "a1", "min": 10.0, "est_min": 8.0, "rc": "0",
                     "vram_peak": 4096.0, "ram_peak": 8.0, "cores_peak": 2.0, "gpus": ["GPU-0"], "gpu": "RTX 4090", "tag": "t1"},
                    {"t0": now - 180, "t1": now - 90, "project": "proj-b", "name": "b1", "min": 20.0, "est_min": 20.0, "rc": "0",
                     "vram_peak": 8192.0, "ram_peak": 12.0, "cores_peak": 4.0, "gpus": ["GPU-0"], "gpu": "RTX 4090"},
                ]
                O._keep_usage(ST.state_dir(), "srv-a", done_rows, now, {"GPU-0": 0})
                h_amy = next(h for h in CF.all_hosts() if h["id"] == "ext-amy")
                payload = O._hist_payload(h_amy, [{"id": "proj-a"}, {"id": "proj-b"}], now)
                self.assertIsNotNone(payload)
                projs = {r["project"] for r in payload["rows"]}
                self.assertEqual(projs, {"proj-a"})
                self.assertEqual(payload["rows"][0]["gpu"], "RTX 4090")
                self.assertNotIn("proj-b", projs)
                # 回归：_to_worker 下发给普通用户外部机的 eta.json 与 sched.json 也按归属过滤，绝不泄露 proj-b 的运行任务与预计结束
                projs_meta = [
                    {"id": "proj-a", "running": [{"id": "a_run", "eta_ts": now + 600, "eta_source": "progress"}]},
                    {"id": "proj-b", "running": [{"id": "secret_job", "eta_ts": now + 900, "eta_source": "progress"}]},
                ]
                _, for_h = O._to_worker(projs_meta, now, {"weight": {"proj-a": 1.0, "proj-b": 2.0}, "owner": {"proj-a": "ha", "proj-b": "hb"}}, {})
                put_amy = for_h(h_amy)
                self.assertIn("proj-a|a_run", put_amy["eta.json"]["jobs"])
                self.assertNotIn("proj-b|secret_job", put_amy["eta.json"]["jobs"])
                self.assertNotIn("proj-b|secret_job", put_amy["eta.json"]["prog"])
                self.assertNotIn("proj-b", put_amy["sched.json"]["weight"])
                self.assertNotIn("proj-b", put_amy["sched.json"]["owner"])

                # 测试 overview.fetch 的 put 走 stdin 落盘（本机执行，大载荷 > 200 KB 不撞 ARG_MAX）
                wh = Path(td) / "wh"
                (wh / "hosts" / "loc1").mkdir(parents=True, exist_ok=True)
                big_rows = [{"host": "srv-a", "project": "proj-a", "name": f"j_{i}", "min": 10.0, "pad": "x" * 800} for i in range(300)]
                big_obj = {"t": round(now), "rows": big_rows}
                self.assertGreater(len(json.dumps(big_obj)), 200_000)
                local_h = CF.host_defaults({"id": "loc1", "ssh": "", "worker_home": str(wh), "standalone": True})
                local_h["put"] = {"hist_shared.json": big_obj, "sched.json": {"t": round(now), "standalone": True}}
                raw, err = O.fetch(local_h)
                self.assertIsNone(err)
                self.assertIsNotNone(raw)
                written = json.loads((wh / "hosts" / "loc1" / "hist_shared.json").read_text(encoding="utf-8"))
                self.assertEqual(len(written["rows"]), 300)
                self.assertTrue(json.loads((wh / "hosts" / "loc1" / "sched.json").read_text(encoding="utf-8"))["standalone"])

    def test_overview_hist_payload_keeps_variants_under_same_tag(self):
        """#282：看板裁剪 hist_shared.json 按 (project, tag, base_name, gpu) 计数，同 tag 下高频重变体不挤出轻变体历史。"""
        with tempfile.TemporaryDirectory() as td:
            st_root = Path(td) / "state"
            hosts_f = Path(td) / "hosts.toml"
            hosts_f.write_text(
                '[[hosts]]\nid = "srv-a"\nssh = "u@192.0.2.10"\n\n'
                '[[hosts]]\nid = "ext-1"\nssh = "u@203.0.113.10"\nplatform_kind = "other"\nstandalone = true\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"LABMON_STATE": str(st_root), "LABMON_HOSTS": str(hosts_f), "LABMON_NO_NOTIFY": "1", "LABMON_NO_KEYCHAIN": "1"}):
                PJ.put_config("proj-a", 'id = "proj-a"\ntitle = "A"\n', {}, None, by="test", ops=True)
                now = time.time()
                done_rows = [
                    {"t0": now - 5000, "t1": now - 4900, "project": "proj-a", "name": "exp_light_s0", "min": 10.0, "rc": "0",
                     "vram_peak": 6144.0, "ram_peak": 14.0, "cores_peak": 2.0, "gpus": ["GPU-0"], "gpu": "A100", "tag": "batch_a"},
                ] + [
                    {"t0": now - 4000 + i * 100, "t1": now - 3950 + i * 100, "project": "proj-a", "name": f"exp_heavy_s{i}", "min": 10.0, "rc": "0",
                     "vram_peak": 32768.0, "ram_peak": 56.0, "cores_peak": 10.0, "gpus": ["GPU-0"], "gpu": "A100", "tag": "batch_a"}
                    for i in range(25)
                ]
                O._keep_usage(ST.state_dir(), "srv-a", done_rows, now, {"GPU-0": 0})
                h_ext = next(h for h in CF.all_hosts() if h["id"] == "ext-1")
                payload = O._hist_payload(h_ext, [{"id": "proj-a"}], now)
                self.assertIsNotNone(payload)
                names = [r["name"] for r in payload["rows"]]
                self.assertIn("exp_light_s0", names)
                self.assertEqual(sum(1 for n in names if n.startswith("exp_heavy_")), 20)


class DiskGateWorkDiskCleanTest(unittest.TestCase):
    """本地工作盘 disk_gate、WorkDisk、host_work_disk 可回收、api.disk 与 clean_local 测试。"""

    def test_disk_gate_hysteresis_and_place_blocking_without_killing_running(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            data_root = d / "work"
            local = data_root / ".tmp"
            m = worker_module(d, LABMON_LOCAL=str(local), LABMON_DATA_ROOT=str(data_root), LABMON_STANDALONE="1")
            w = m.Worker("ext-1", standalone=True)
            Path(w.p.log).parent.mkdir(parents=True, exist_ok=True)
            pol = {"standalone": True, "disk_warn_gb": 20.0, "disk_stop_gb": 10.0, "disk_quota_gb": 100.0}
            # 1. 剩 9 GiB (< 10 GiB) 触发闸门
            info_low = {"disk": [{"path": str(data_root), "free": 9.0, "total": 200.0, "kind": "local"}]}
            w.disk_events(info_low, pol)
            self.assertTrue(w.disk_gate["on"])
            # 2. 剩 11 GiB（在 10..12 GiB 滞回带内）保持开启，不抖动
            info_mid = {"disk": [{"path": str(data_root), "free": 11.0, "total": 200.0, "kind": "local"}]}
            w.disk_events(info_mid, pol)
            self.assertTrue(w.disk_gate["on"])
            # 3. 剩 12.5 GiB（>= 10 + 2 GiB）恢复放行
            info_ok = {"disk": [{"path": str(data_root), "free": 12.5, "total": 200.0, "kind": "local"}]}
            w.disk_events(info_ok, pol)
            self.assertFalse(w.disk_gate["on"])
            # 4. 软配额触发（工作目录 105 GiB >= 100 GiB）与滞回恢复（降到 97 GiB <= 98 GiB）
            w.storage = {"work": {"b": int(105 * m.GIB)}}
            w.disk_events(info_ok, pol)
            self.assertTrue(w.disk_gate["on"])
            self.assertIn("配额", w.disk_gate["why"])
            w.storage = {"work": {"b": int(97 * m.GIB)}}
            w.disk_events(info_ok, pol)
            self.assertFalse(w.disk_gate["on"])

            # 4b. 小配额（<= 2 GiB，如 1.0 GiB）滞回仍保留可达恢复水位（0.8 GiB）
            pol_small = {"standalone": True, "disk_warn_gb": 20.0, "disk_stop_gb": 0.0, "disk_quota_gb": 1.0}
            w.storage = {"work": {"b": int(1.2 * m.GIB)}}
            w.disk_events(info_ok, pol_small)
            self.assertTrue(w.disk_gate["on"])
            self.assertEqual(w.disk_gate["quota_recover_gb"], 0.8)
            w.storage = {"work": {"b": int(0.9 * m.GIB)}}
            w.disk_events(info_ok, pol_small)
            self.assertTrue(w.disk_gate["on"])
            w.storage = {"work": {"b": int(0.5 * m.GIB)}}
            w.disk_events(info_ok, pol_small)
            self.assertFalse(w.disk_gate["on"])

            # 5. place() 的 pre() 在闸门开启时拦截新任务，但不终止在跑任务
            # 顺便验证 cpu_quota 配置经 apply_cpu_quota 接入 worker 有效容量（无 cgroup 限额与大于配额均受限）
            info_nocg = {"nproc": 16, "cpu_quota": None}
            w.apply_cpu_quota(info_nocg, {"cpu_quota": 2.0})
            self.assertEqual(info_nocg["cpu_quota"], 2.0)
            info_bigcg = {"nproc": 16, "cpu_quota": 8.0}
            w.apply_cpu_quota(info_bigcg, {"cpu_quota": 2.0})
            self.assertEqual(info_bigcg["cpu_quota"], 2.0)
            w.info = {"nproc": 16, "cpu_quota": 2.0, "mem": {"limit": 64.0, "anon": 1.0, "used": 1.0}, "gpus": []}
            snap_cq = w.snapshot({})
            self.assertEqual(snap_cq["cpu_cap"], 2)
            # 回归：启动环境含 LABMON_CPU_QUOTA=2，看板下发 sched.json 显式清除 cpu_quota: null 后不再回退旧值
            Path(w.p.sched).parent.mkdir(parents=True, exist_ok=True)
            Path(w.p.sched).write_text(json.dumps({"standalone": True, "cpu_quota": None}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"LABMON_CPU_QUOTA": "2"}):
                self.assertNotIn("cpu_quota", w.policy())
            w.disk_gate = {"on": True, "why": "本地盘空间不足：剩 8.0 GiB", "free_gb": 8.0, "stop_gb": 10.0, "recover_gb": 12.0}
            S = {"now": time.time(), "cards": [], "jobs": [{"name": "already_running", "project": "p1", "gpus": [], "vram": {}, "ram": 1.0, "cpu": 1.0, "excl": None, "tag": None, "hold": False}],
                 "room": 64.0, "margin": 8.0, "limit": 64.0, "cpu_room": 8.0, "cpu_cap": 8, "tags": {}, "holds": {}, "owner": set(), "who": "对方", "kept": False,
                 "cut": None, "brief": False, "ext": [], "gp": [], "cpu": 0.0, "gangs": {}, "gholds": {}}
            req = {"key": "k1", "lane": "k1", "kind": "queue", "project": "p1", "name": "new_job",
                   "ngpu": 0, "excl": None, "tag": None, "hold": False, "allowed": None, "gpus": None, "dur": 10,
                   "deps": [], "deps_any": [], "q": {"key": "k1", "par": 1, "gpus": None},
                   "est": {"vram": 0, "src": "提示值", "ram": 1, "ram_src": "提示值", "cpus": 1, "cpu_src": "提示值"}}
            got = w.place(req, S)
            self.assertIsNotNone(got)
            self.assertIn("本地盘空间不足", got[0])
            self.assertEqual(len(S["jobs"]), 1)
            # 回归：磁盘闸门开启时，带 needs= 依赖拷贝的任务也必须先被闸门拦截，绝不启动 stage_bg 拷贝写爆盘
            src_f = d / "master.bin"
            src_f.write_bytes(b"m" * 1024)
            dst_f = local / "cache" / "p1" / "master.bin"
            req_needs = dict(req, needs=[f"{dst_f}<{src_f}"], cmd="echo hi",
                             q={"key": "k1", "par": 1, "gpus": None, "root": str(data_root / "projects/p1"), "project": "p1", "env": None})
            with mock.patch.object(w, "stage_bg", side_effect=AssertionError("disk_gate 开启时不该启动 stage_bg")):
                got_n = w.place(req_needs, S)
            self.assertIsNotNone(got_n)
            self.assertIn("本地盘空间不足", got_n[0])
            self.assertFalse(dst_f.exists())

    def test_workdisk_no_symlink_escape_and_reclaimable_and_clean_local(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            outside = d / "outside_huge"
            outside.mkdir()
            (outside / "big.bin").write_bytes(b"0" * (2 * 1024 * 1024))

            data_root = d / "work"
            local = data_root / ".tmp"
            proj_a = data_root / "projects" / "proj-a"
            proj_old = data_root / "projects" / "proj-old"
            proj_a.mkdir(parents=True)
            proj_old.mkdir(parents=True)
            (proj_a / "result.bin").write_bytes(b"a" * 4096)
            (proj_a / "link_out").symlink_to(outside)  # 软链指向 data_root 外的大目录
            (proj_old / "ckpt.bin").write_bytes(b"b" * 4096)

            # 创建 .tmp/{cache,jobs}
            cache_a = local / "cache" / "proj-a"
            cache_b = local / "cache" / "proj-b"
            job_done = local / "jobs" / "proj-a" / "done_job"
            job_run = local / "jobs" / "proj-a" / "running_job"
            for p in (cache_a, cache_b, job_done, job_run):
                p.mkdir(parents=True)
                (p / "tmp.dat").write_bytes(b"x" * 2048)

            m = worker_module(d, LABMON_LOCAL=str(local), LABMON_DATA_ROOT=str(data_root), LABMON_STANDALONE="1")
            # 1. WorkDisk 不跟随软链，绝不把 outside 2 MiB 计入 proj-a
            wd = m.WorkDisk("ext-1", str(data_root))
            now = time.time()
            pace = m.Pace(1e9)
            units = wd.lister({str(data_root / "projects")}, pace)
            self.assertIn(str(proj_a), units)
            b_pa = wd.scanner(str(proj_a), now, pace)["b"]
            self.assertLess(b_pa, 100_000)
            # 1b. WorkDisk.summary(proj_map) 按登记项目 ID 归属（目录名与 --project 不一致时不归到虚构目录名）
            wd.ix = {
                "t": now,
                "units": {
                    str(data_root / "projects/."): {"b": 4096, "m": now - 20 * 86400, "t": now},
                    str(data_root / "projects/folder-name/code"): {"b": 3 * m.GIB, "m": now - 20 * 86400, "t": now},
                },
            }
            wk_mapped, _ = wd.summary({str(data_root / "projects/folder-name/code"): "proj-a"})
            self.assertIn("proj-a", wk_mapped["projects"])
            self.assertNotIn("folder-name", wk_mapped["projects"])
            self.assertEqual(wk_mapped["projects"]["proj-a"]["b"], 3 * m.GIB)
            # projects/. 平层单位绝不能覆盖 projects/folder-name/code，避免整个 projects/ 被标成可回收
            self.assertEqual(wk_mapped["projects"]["proj-a"]["dir"], "projects/folder-name/code")
            # host_work_disk 生成可回收路径必须使用真实目录 dir，绝不拿项目 ID 反推成 projects/proj-a
            e_mapped = {
                "id": "ext-1", "label": "ext-1", "standalone": True, "data_root": str(data_root),
                "status": {
                    "info": {"disk": [{"path": str(data_root), "free": 50.0, "total": 200.0, "kind": "local"}]},
                    "queues": [], "running": [],
                    "storage": {"work": wk_mapped, "local": {}, "projects": {str(data_root / "projects/folder-name/code"): "proj-a"}},
                },
            }
            hw_mapped = DK.host_work_disk(e_mapped, CF.host_defaults({"id": "ext-1", "standalone": True, "data_root": str(data_root)}), now)
            self.assertEqual(hw_mapped["reclaimable"][0]["u"], "projects/folder-name/code")
            self.assertEqual(hw_mapped["reclaimable"][0]["path"], str(data_root / "projects/folder-name/code"))

            # 2. host_work_disk 计算 14 天闲置可回收候选 & project_storage / api.disk 区分共享盘与本地工作盘
            old_t = now - 20 * 86400
            e = {
                "id": "ext-1",
                "label": "ext-1",
                "standalone": True,
                "data_root": str(data_root),
                "status": {
                    "info": {"disk": [{"path": str(data_root), "free": 15.0, "total": 200.0, "kind": "local"}]},
                    "disk_gate": {"on": False},
                    "queues": [{"project": "proj-a", "empty": False}],
                    "running": [],
                    "storage": {
                        "work": {
                            "root": str(data_root), "b": 5 * DK.GIB, "t": now, "pending": 0,
                            "projects": {
                                "proj-a": {"b": 2 * DK.GIB, "d24": 0, "m": now - 60, "top": []},
                                "proj-old": {"b": 3 * DK.GIB, "d24": 0, "m": old_t, "top": []},
                            },
                            "common": {"envs": 0, "datasets": 0, "weights": 0, "labmon": 0, "archive": 0, "tmp": 512 * 1024**2, "other": 0},
                        },
                        "local": {"proj-a": {"cache": 512 * 1024**2, "scratch": 0}, "proj-old": {"cache": 512 * 1024**2, "scratch": 0}},
                    },
                },
            }
            hcfg = CF.host_defaults({"id": "ext-1", "standalone": True, "data_root": str(data_root), "disk_warn_gb": 20, "disk_stop_gb": 10})
            hw = DK.host_work_disk(e, hcfg, now)
            self.assertIsNotNone(hw)
            self.assertLess(hw["free"] / DK.GIB, hw["warn_gb"])
            self.assertEqual([x["project"] for x in hw["reclaimable"]], ["proj-old"])
            self.assertTrue(hw["projects"]["proj-old"]["reclaimable"])
            self.assertFalse(hw["projects"]["proj-a"]["reclaimable"])

            # 经真实 DK.project_storage 生成 psnap，验证 api.disk 对纯独立机项目不报“共享盘的总量与剩余还没读到”
            e["work_disk"] = hw
            psg = DK.project_storage("proj-old", snap={"generated": now, "hosts": [e], "disk": {}})
            self.assertIn("ext-1", psg["standalone"])
            psnap = {"storage": psg, "hosts": [{"id": "ext-1", "label": "ext-1", "standalone": True}]}
            res = A.disk("proj-old", psnap=psnap)
            self.assertNotIn("共享盘的总量与剩余还没读到", res["out"])
            self.assertIn("独立主机本地工作盘", res["out"])
            self.assertIn("可回收", res["out"])
            self.assertIn("labmon worker clean ext-1", res["out"])

            # 3. clean_local：默认 dry-run 不删；--yes 仅删指定项目且跳过运行中 scratch，绝不删 projects/
            p_obj = m.Paths("ext-1")
            Path(p_obj.status).parent.mkdir(parents=True, exist_ok=True)
            Path(p_obj.pid).write_text(str(os.getpid()), encoding="utf-8")
            Path(p_obj.status).write_text(json.dumps({
                "running": [{"project": "proj-a", "name": "running_job"}],
            }), encoding="utf-8")
            import contextlib
            import io
            with mock.patch.object(m.clean_local.__globals__["_C"], "alive", return_value=True):
                buf1 = io.StringIO()
                with contextlib.redirect_stdout(buf1):
                    rc_dry = m.clean_local(p_obj, project="proj-a", yes=False)
                self.assertEqual(rc_dry, 0)
                self.assertIn("预览模式未删除", buf1.getvalue())
                self.assertIn("已跳过 1 个在跑任务", buf1.getvalue())
                self.assertTrue(cache_a.exists())
                self.assertTrue(job_done.exists())

                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    rc_yes = m.clean_local(p_obj, project="proj-a", yes=True)
                self.assertEqual(rc_yes, 0)
                self.assertIn("已清理 2 项", buf2.getvalue())
                self.assertFalse(cache_a.exists())
                self.assertFalse(job_done.exists())
                self.assertTrue(job_run.exists())       # 运行中 scratch 受保护
                self.assertTrue(cache_b.exists())       # 别的项目缓存不受影响
                self.assertTrue((proj_a / "result.bin").exists())  # projects/ 产物绝对不动

            # 3b. worker 已停止（pid 文件删除）但任务自身进程仍在跑时，clean --scratch --yes 仍须保护该任务目录
            Path(p_obj.pid).unlink(missing_ok=True)
            job_dead = local / "jobs" / "proj-a" / "dead_job"
            job_dead.mkdir(parents=True, exist_ok=True)
            (job_dead / "tmp.dat").write_bytes(b"z" * 1024)
            Path(p_obj.status).write_text(json.dumps({
                "running": [
                    {"project": "proj-a", "name": "running_job", "pid": os.getpid()},
                    {"project": "proj-a", "name": "dead_job", "pid": 999_999_999},
                ],
            }), encoding="utf-8")
            with mock.patch.object(m.clean_local.__globals__["_C"], "alive", return_value=False):
                buf3 = io.StringIO()
                with contextlib.redirect_stdout(buf3):
                    rc_stop = m.clean_local(p_obj, project="proj-a", scratch=True, yes=True)
                self.assertEqual(rc_stop, 0)
                self.assertTrue(job_run.exists())       # worker 停止后，仍在跑的任务 scratch 绝不被误删
                self.assertFalse(job_dead.exists())     # 已退出任务的 scratch 正常清理

            # 3c. 回归：扫描阶段到删除阶段之间任务被新启动（写入 active_scratch.json）→ 锁内复核必须跳过、绝不误删
            job_racy = local / "jobs" / "proj-a" / "racy_job"
            job_racy.mkdir(parents=True, exist_ok=True)
            (job_racy / "live.bin").write_bytes(b"r" * 1024)
            Path(p_obj.status).write_text(json.dumps({"running": []}), encoding="utf-8")
            Path(p_obj.active_scratch).unlink(missing_ok=True)
            orig_scan = m.clean_local.__globals__["_D"].scan_unit

            def _scan_and_launch(*args, **kwargs):
                res = orig_scan(*args, **kwargs)
                # 模拟扫描期间 Worker.start 放行了 racy_job（写入 active_scratch.json）
                Path(p_obj.active_scratch).write_text(json.dumps({"proj-a/racy_job": os.getpid()}), encoding="utf-8")
                return res

            with mock.patch.object(m.clean_local.__globals__["_D"], "scan_unit", side_effect=_scan_and_launch):
                buf4 = io.StringIO()
                with contextlib.redirect_stdout(buf4):
                    rc_racy = m.clean_local(p_obj, project="proj-a", scratch=True, yes=True)
                self.assertEqual(rc_racy, 0)
                self.assertTrue(job_racy.exists())      # 扫描期间新起任务的 scratch 必须受锁内复核保护
                self.assertTrue((job_racy / "live.bin").exists())


class ExternalHostDualEntrySecurityTest(unittest.TestCase):
    """外部主机 REST API + CLI 双入口、零私钥上传与多用户安全防线。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.state = self.root / "state"
        self.secrets = self.root / "secrets"
        self.hosts_f = self.root / "hosts.toml"
        self.hosts_f.write_text(
            '[[hosts]]\nid = "scm-1"\nssh = "root@192.0.2.10"\nport = 10022\nplatform_job = "j1"\n\n'
            '[fleet]\nplatform = "http://192.0.2.10"\n\n'
            '[[fleet.servers]]\nid = "fl-1"\nholder = "alice"\nssh = "root@192.0.2.20"\nport = 10023\npool = "pub"\n',
            encoding="utf-8",
        )
        self.env = mock.patch.dict(os.environ, {
            "LABMON_STATE": str(self.state),
            "LABMON_SECRETS": str(self.secrets),
            "LABMON_HOSTS": str(self.hosts_f),
            "LABMON_NO_NOTIFY": "1",
            "LABMON_NO_KEYCHAIN": "1",
            "LABMON_LAN": "off",
        })
        self.env.start()
        LS.set_marker(True, "test")
        add_account("boss", "admin", "init")
        add_account("amy", "user", "boss")
        add_account("bob", "user", "boss")
        self.boss = U.Ident("boss", "admin", frozenset({"read", "project", "admin"}), "session", "k0")
        self.amy = U.Ident("amy", "user", frozenset({"read", "project"}), "session", "k1")
        self.bob = U.Ident("bob", "user", frozenset({"read", "project"}), "session", "k2")

    def tearDown(self):
        self.env.stop()
        self.td.cleanup()

    def test_security_redlines_and_quota_and_isolation(self):
        # 1. 拒绝携带任何私钥上传字段
        with self.assertRaises(U.Denied) as cm:
            EH.add_external({"id": "ext-bad", "ssh": "u@203.0.113.10", "private_key": "-----BEGIN"}, self.amy)
        self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(U.Denied) as cm:
            EH.add_external({"id": "ext-bad2", "ssh": "u@203.0.113.10", "ssh_key": "uploaded"}, self.amy)
        self.assertEqual(cm.exception.code, 400)

        # 2. 普通用户禁 ssh=""、禁 ssh_key="system"、禁 share!="private"
        for payload in (
            {"id": "ext-loc", "ssh": ""},
            {"id": "ext-sys", "ssh": "u@203.0.113.10", "ssh_key": "system"},
            {"id": "ext-sh", "ssh": "u@203.0.113.10", "share": "pool"},
        ):
            with self.assertRaises(U.Denied) as cm:
                EH.add_external(payload, self.amy)
            self.assertEqual(cm.exception.code, 403)

        # 3. 普通用户防 SSRF / 选项注入 / 撞机接管
        for bad_ssh, port, want_code in (
            ("-oProxyCommand=id", 22, 400),
            ("u@localhost", 22, 403),
            ("u@127.0.0.1", 22, 403),
            ("u@[::1]", 22, 403),
            ("u@169.254.169.254", 22, 403),
            ("u@0.0.0.0", 22, 403),
            ("u@192.0.2.10", 10022, 409),   # 撞现有 SCM 主机
            ("u@192.0.2.20", 10023, 409),   # 撞组内服务器
        ):
            with self.assertRaises(U.Denied, msg=f"{bad_ssh}:{port}") as cm:
                EH.add_external({"id": "ext-ssrf", "ssh": bad_ssh, "port": port}, self.amy)
            self.assertEqual(cm.exception.code, want_code)

        # 4. 普通用户正常添加（生成专用 Ed25519 公钥，不回显私钥）、最多 3 台上限
        r1 = EH.add_external({"id": "ext-1", "ssh": "u@203.0.113.11", "port": 2201, "platform_kind": "other"}, self.amy)
        self.assertTrue(r1["pubkey"].startswith("ssh-ed25519 "))
        self.assertIn("authorized_keys", r1["auth_cmd"])
        self.assertNotIn("PRIVATE KEY", json.dumps(r1))
        priv = PF.ssh_key_path("amy", "ext-1")
        self.assertTrue(priv.is_file())
        self.assertEqual(priv.stat().st_mode & 0o777, 0o600)

        EH.add_external({"id": "ext-2", "ssh": "u@203.0.113.12"}, self.amy)
        EH.add_external({"id": "ext-3", "ssh": "u@203.0.113.13"}, self.amy)
        with self.assertRaises(U.Denied) as cm:
            EH.add_external({"id": "ext-4", "ssh": "u@203.0.113.14"}, self.amy)
        self.assertEqual(cm.exception.code, 409)

        # 5. 他人（bob）完全不可见且不可改删测
        self.assertNotIn("ext-1", U.access(self.bob, {}).allowed)
        self.assertIn("ext-1", U.access(self.amy, {}).allowed)
        for fn in (
            lambda: EH.edit_external("ext-1", {"label": "hack"}, self.bob),
            lambda: EH.remove_external("ext-1", self.bob),
            lambda: EH.key_external("ext-1", self.bob),
            lambda: EH.test_external("ext-1", self.bob),
        ):
            with self.assertRaises(U.Denied) as cm:
                fn()
            self.assertEqual(cm.exception.code, 403)

        # 6. 轮换密钥生成新公钥并调 close_host_ssh
        old_pub = r1["pubkey"]
        with mock.patch.object(L, "close_host_ssh") as m_close:
            rk = EH.key_external("ext-1", self.amy, rotate=True)
            self.assertNotEqual(rk["pubkey"], old_pub)
            self.assertTrue(rk["rotated"])
            m_close.assert_called_once()

        # 7. 编辑修改端口触发 close_host_ssh(drop_known_hosts=True)
        with mock.patch.object(L, "close_host_ssh") as m_close:
            EH.edit_external("ext-1", {"port": 2202, "disk_warn_gb": 25, "disk_stop_gb": 12}, self.amy)
            m_close.assert_called_once()
            self.assertTrue(m_close.call_args.kwargs.get("drop_known_hosts"))

        # 8. 非 force 删除在主机仍有 waiting / todo 任务时必须返回 409 拒绝
        ST.save_json(ST.state_dir() / "snapshot.json", {
            "hosts": [{"id": "ext-1", "status": {"running": [], "waiting": [{"project": "p1", "name": "j1"}], "todo": [{"project": "p1", "jobs": ["j1", "j2"]}]}}],
        })
        with self.assertRaises(U.Denied) as cm:
            EH.remove_external("ext-1", self.amy, force=False)
        self.assertEqual(cm.exception.code, 409)
        self.assertIn("待跑任务", str(cm.exception))
        # 清空状态后正常删除并同步销毁私钥与 close_host_ssh
        ST.save_json(ST.state_dir() / "snapshot.json", {"hosts": []})
        with mock.patch.object(L, "close_host_ssh") as m_close:
            rm = EH.remove_external("ext-1", self.amy)
            self.assertTrue(rm["key_removed"])
            self.assertFalse(priv.exists())
            m_close.assert_called_once()
            self.assertTrue(m_close.call_args.kwargs.get("drop_known_hosts"))

    def test_external_test_ssh_cmd_shell_quote_and_api_hosts_editable_fields(self):
        """回归测试：SSH test_external 多行脚本用 shlex.quote 在远端 bash 中可执行；GET /api/hosts 完整返回 python/cpu_quota/mps_vram_gb。"""
        import subprocess
        EH.add_external({
            "id": "ext-py", "ssh": "u@203.0.113.88", "python": sys.executable,
            "data_root": str(self.root / "dr"), "cpu_quota": 6, "mps_vram_gb": 24,
        }, self.boss)
        # 1. api.hosts() 完整透出 python / cpu_quota / mps_vram_gb
        got = next(h for h in A.hosts()["hosts"] if h["id"] == "ext-py")
        self.assertEqual(got["python"], sys.executable)
        self.assertEqual(got["cpu_quota"], 6)
        self.assertEqual(got["mps_vram_gb"], 24)
        # 2. SSH test_external 构造的 remote_cmd 在真实 bash -c 下可执行且输出合法 JSON（无 SyntaxError）
        captured = []

        def fake_ssh_run(argv, **kwargs):
            captured.append(argv[-1])
            return subprocess.run(["bash", "-c", argv[-1]], capture_output=True, text=True, timeout=15)

        with mock.patch.object(L, "ssh_run", side_effect=fake_ssh_run):
            res = EH.test_external("ext-py", self.boss)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(captured), 1)
        self.assertTrue(res["info"]["writable"])

        # 3. 回归：管理员创建归属普通用户的 ssh_key="system" 主机，该用户省略 ssh_key 编辑修改目标时必须被 403 拒绝（不能继承 system 绕过）
        EH.add_external({
            "id": "ext-sys-amy", "ssh": "u@203.0.113.89", "ssh_key": "system", "owner": "amy",
        }, self.boss)
        with self.assertRaises(U.Denied) as cm:
            EH.edit_external("ext-sys-amy", {"ssh": "u@203.0.113.90"}, self.amy)
        self.assertEqual(cm.exception.code, 403)
        # 4. 回归：overview._to_worker 与 workerctl.worker_env 均下发 cpu_quota 到独立主机，清空时显式传 null
        from labmon import workerctl as WC
        h_py = next(h for h in CF.all_hosts() if h["id"] == "ext-py")
        _, for_host = O._to_worker([], time.time(), {}, {})
        self.assertEqual(for_host(h_py)["sched.json"].get("cpu_quota"), 6)
        self.assertIn("LABMON_CPU_QUOTA=6", WC.worker_env(h_py))
        # 清空 cpu_quota 后 sched.json 显式包含 cpu_quota: None
        EH.edit_external("ext-py", {"cpu_quota": None}, self.boss)
        h_py2 = next(h for h in CF.all_hosts() if h["id"] == "ext-py")
        _, for_host2 = O._to_worker([], time.time(), {}, {})
        self.assertIn("cpu_quota", for_host2(h_py2)["sched.json"])
        self.assertIsNone(for_host2(h_py2)["sched.json"]["cpu_quota"])

        # 5. 回归：管理员清空外部主机 owner="" 后归属归零且原用户失去可见性
        EH.add_external({"id": "ext-own", "ssh": "u@203.0.113.91", "owner": "amy"}, self.boss)
        self.assertIn("ext-own", U.access(self.amy, {}).allowed)
        r_clr = EH.edit_external("ext-own", {"owner": ""}, self.boss)
        self.assertIsNone(r_clr["host"]["owner"])
        self.assertNotIn("ext-own", U.access(self.amy, {}).allowed)

        # 6. 回归：切换外部主机 data_root 后 overview._SENT 缓存失效，新目录能完整收到 sched.json
        O._SENT[("ext-py", "sched.json")] = {"standalone": True}
        EH.edit_external("ext-py", {"data_root": str(self.root / "dr_new")}, self.boss)
        self.assertNotIn(("ext-py", "sched.json"), O._SENT)

    def test_http_routes_and_standby_and_worker_clean_auth(self):
        """走真实 HTTP Server 验证 /api/hosts/external* 与 /api/worker/clean 路由、鉴权与备用拒写。"""
        srv = SV.Server(("127.0.0.1", 0), interval=3600)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        import threading
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            tok_amy = make_token("amy", "user", ("read", "project"))
            tok_bob = make_token("bob", "user", ("read", "project"))
            tok_adm = make_token("boss", "admin", ("read", "project", "admin"))
            op_amy = bearer_opener(tok_amy)
            op_bob = bearer_opener(tok_bob)
            op_adm = bearer_opener(tok_adm)

            # amy 经 POST /api/hosts/external 添加外部机
            code, res = _http_json(op_amy, "POST", f"{base}/api/hosts/external", {"id": "ext-http", "ssh": "u@203.0.113.55"})
            self.assertEqual(code, 201, res)
            self.assertTrue(res["pubkey"].startswith("ssh-ed25519 "))

            # GET /api/hosts：amy 看得到 ext-http，bob 看不到
            _, amy_hosts = _http_json(op_amy, "GET", f"{base}/api/hosts")
            _, bob_hosts = _http_json(op_bob, "GET", f"{base}/api/hosts")
            self.assertIn("ext-http", [h["id"] for h in amy_hosts["hosts"]])
            self.assertNotIn("ext-http", [h["id"] for h in bob_hosts["hosts"]])

            # POST /api/worker/clean：普通用户不带 --project 被 403 拒绝
            code, res = _http_json(op_amy, "POST", f"{base}/api/worker/clean", {"host": "ext-http", "args": [], "dry_run": True})
            self.assertEqual(code, 403, res)

            # PUT /api/hosts/external/ext-http 修改显示名
            code, res = _http_json(op_amy, "PUT", f"{base}/api/hosts/external/ext-http", {"label": "我的外租机"})
            self.assertEqual(code, 200, res)
            self.assertEqual(res["host"]["label"], "我的外租机")

            # 备用模式下 PUT / DELETE / POST 外部机均返回 409 standby
            with mock.patch.object(LS, "primary", return_value=False):
                code, res = _http_json(op_adm, "POST", f"{base}/api/hosts/external", {"id": "ext-sb", "ssh": "u@203.0.113.66"})
                self.assertEqual(code, 409)
                self.assertTrue(res.get("standby"))

            # DELETE /api/hosts/external/ext-http 删除并毁钥
            code, res = _http_json(op_amy, "DELETE", f"{base}/api/hosts/external/ext-http", {"force": False})
            self.assertEqual(code, 200, res)
            self.assertTrue(res["key_removed"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
