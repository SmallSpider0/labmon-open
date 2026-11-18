"""组内服务器（fleet.py）的离线测试：探针输出用合成的样例，不连服务器。

    python -m unittest tests.test_fleet -v
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import GB, GUARD_PROBE as GUARD, KA_PROBE as KA, NO_HOSTS, T0, fake_op, fake_status, fleet_cfg as cfg, login, post_req, probe_out  # noqa: E402
os.environ["LABMON_LAN"] = "on"                       # 假服务器连不上：别判成本机不在内网
os.environ["LABMON_HOSTS"] = NO_HOSTS

from labmon import fleet as F  # noqa: E402
from labmon import fleetboot as FB  # noqa: E402
from labmon import fleetops as FO  # noqa: E402
from labmon import fleetview as FV  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon.config import ConfigError  # noqa: E402
from labmon.health import container_step, lease_due  # noqa: E402
from labmon.records import EVENT_KEEP_S, log_events, records  # noqa: E402
from labmon.state import load_rows, save_json, state_dir  # noqa: E402

class ParseTest(unittest.TestCase):
    def test_parse(self):
        s = F.parse(KA)
        self.assertEqual(s["started"], T0 - 86400)
        self.assertAlmostEqual(s["mem"]["used"], 4.0)
        self.assertAlmostEqual(s["mem"]["anon"], 3.5)
        self.assertEqual(s["mem"]["limit"], 61)
        self.assertEqual(s["cpu_quota"], 7)
        self.assertAlmostEqual(s["cpu"], 1.2)
        self.assertEqual(s["gpus"][0]["util"], 81)
        self.assertEqual(s["map"], {500: (9, "/root/data1/alice/ka")})
        self.assertEqual(s["ps"][9]["cpu_s"], 86400 + 2 * 3600 + 3 * 60 + 4)
        self.assertEqual(s["ps"][9]["rss"], 3 * GB)
        self.assertEqual(F._secs("05:06"), 306)

    def test_probe_script(self):
        subprocess.run(["bash", "-n", str(F.PROBE)], check=True)

    def test_probe_unevictable_memory(self):
        """探针只送用量与文件页，本机算收不回的内存 = 用量 − 文件页（与 worker 的 mem_unevictable 同口径）：3.10 内核的 v1 没有 total_shmem，
        /dev/shm 的 20 GiB 照样算收不回的；v2 同理。真跑 fleet_probe.sh（LABMON_CG 指到假 cgroup，其余段在本机跑空）。"""
        with tempfile.TemporaryDirectory() as td:
            cg = Path(td)
            (cg / "memory").mkdir()
            (cg / "memory" / "memory.limit_in_bytes").write_text(str(122 * GB))
            (cg / "memory" / "memory.usage_in_bytes").write_text(str(int(56.44 * GB)))
            (cg / "memory" / "memory.stat").write_text(f"cache {int(52.87 * GB)}\nrss {int(3.57 * GB)}\nactive_file 1\ninactive_file 1\n"
                                                       f"total_rss {int(3.57 * GB)}\ntotal_active_file {int(25.48 * GB)}\ntotal_inactive_file {int(7.37 * GB)}\n")

            def run():
                out = subprocess.run(["bash", "-s"], input=F.PROBE.read_text(), capture_output=True, text=True, timeout=60,
                                     env=dict(os.environ, LABMON_CG=td)).stdout
                return F.parse(out)["mem"]
            m = run()
            self.assertEqual((round(m["anon"], 2), round(m["used"], 2)), (23.59, 49.07))
            for f in (cg / "memory").iterdir():
                f.unlink()
            (cg / "memory").rmdir()
            (cg / "memory.max").write_text(str(122 * GB))
            (cg / "memory.current").write_text(str(int(30.5 * GB)))
            (cg / "memory.stat").write_text(f"anon {3 * GB}\nshmem {2 * GB}\nactive_file {20 * GB}\ninactive_file {5 * GB}\n")
            m = run()
            self.assertEqual((m["anon"], m["used"]), (5.5, 25.5))

    def test_brief(self):
        self.assertEqual(F.brief("/x/python -u -m pkg.run --config /a/b/c.yaml --seed 1"), "pkg.run · c.yaml")
        self.assertEqual(F.brief("/x/python3.8 /a/train.py"), "train.py")
        self.assertEqual(F.brief("bash /a/run.sh s2"), "run.sh")
        self.assertEqual(F.brief("/x/python -u -c import x"), "python -c …")
        self.assertEqual(F.brief("bash -c 'python a.py'"), "bash")


class ClassifyTest(unittest.TestCase):
    def test_keepalive_only(self):
        a = F.analyze(F.parse(KA), cfg())
        self.assertEqual(a["state"], "keepalive")
        self.assertEqual((a["vram_real"], a["util_real"]), (0, 0))
        self.assertAlmostEqual(a["cpu_real"], 0.2)            # 1.2 核减去保活的 100%
        self.assertAlmostEqual(a["mem_real"], 1.0)            # 4 GiB 工作集减去保活的 3 GiB RSS
        self.assertAlmostEqual(a["mem_anon"], 0.5)            # 收不回的 3.5 GiB 减去保活
        self.assertTrue(a["share"])

    def test_keepalive_tree_pss(self):
        """保活的内存按它整棵进程树的 PSS 扣（#29，同 worker）：fork 出的 DataLoader 子进程与主进程共享的页按 RSS 相加会重复计、扣多了，
        对方的用量被压低。探针的 pss 段读不到的进程按 RSS；对方的进程（VS Code）不扣。"""
        ka = "/root/miniconda3/envs/py38/bin/python -u /root/x/train_keep_alive.py"
        raw = probe_out(apps=[(500, 6748)], maps=[(9, 500, "/root/x")], mem_gb=20.5, inact_gb=3.0, anon_gb=7.25,
                        ps=[(1, 0, "/bin/bash"), (9, 1, ka, 100.0, int(1.25 * 1024 * 1024)), (10, 9, ka, 30.0, int(1.1 * 1024 * 1024)),
                            (11, 9, ka, 30.0, int(1.1 * 1024 * 1024)), (12, 11, "sh -c true", 0.0, 512 * 1024),
                            (50, 1, "/root/.vscode-server/node server-main.js", 1.0, int(4.7 * 1024 * 1024))])
        raw += "@@ pss\n9 %d\n10 %d\n11 %d\n" % (int(0.55 * 1024 * 1024), int(0.3 * 1024 * 1024), int(0.3 * 1024 * 1024))
        a = F.analyze(F.parse(raw), cfg())
        self.assertAlmostEqual(a["mem_anon"], 7.25 - 1.65, 2)                 # 按 RSS 合计扣会只剩 3.3
        self.assertAlmostEqual(a["mem_ka"], 1.15 + 0.5, 2)                    # 12 读不到 PSS：按 RSS 0.5

    def test_guard_wrapper_is_not_keepalive(self):
        a = F.analyze(F.parse(GUARD), cfg())
        w = a["work"][0]
        self.assertEqual((a["state"], w["kind"], w["rule"]), ("job", "job", None))   # 不是保活就算实验，不用规则
        self.assertEqual(w["label"], "run_parallel.py")       # 最上层的非 shell 父进程
        self.assertEqual(w["names"], ["eval.py · a.yaml"])    # multiprocessing 子进程取它的父进程名
        self.assertEqual(w["cwd"], "/root/proj/repo")
        a = F.analyze(F.parse(GUARD), cfg(rules=[{"match": "/root/proj/", "kind": "job", "label": "P 项目"}]))
        self.assertEqual((a["state"], a["work"][0]["label"], a["work"][0]["rule"]), ("job", "P 项目", "/root/proj/"))
        self.assertFalse(a["share"])                          # 利用率 55% 超过阈值
        self.assertEqual(a["vram_real"], 7800)

    def test_guarded_keepalive_beats_parent_rule(self):
        # demo_eval 的守护脚本跑完实验后拉起保活：父进程带 /root/demo_eval/，GPU 上只有保活，不能被项目规则算成实验
        raw = probe_out(apps=[(600, 6748)], maps=[(52, 600, "/root/data1/alice/ka")], util=(85, 85), cores=1.0, mem_gb=6.0,
                        ps=[(1, 0, "/bin/bash"), (50, 1, "bash /root/demo_eval/exp/keepalive_guard.sh bash /root/demo_eval/exp/run/a.sh"),
                            (51, 50, "xargs -0 -a /root/demo_eval/exp/keepalive_environ.bin sh -c exec env -i setsid nohup python train_keep_alive.py"),
                            (52, 51, "/root/miniconda3/envs/py38/bin/python /root/data1/alice/ka/train_keep_alive.py", 100.0, 3 * 1024 * 1024)])
        demo_eval = [{"match": "/root/demo_eval/", "kind": "job", "label": "demo_eval 评测"}]
        a = F.analyze(F.parse(raw), cfg(rules=demo_eval))
        self.assertEqual((a["state"], a["work"][0]["kind"], a["work"][0]["rule"]), ("keepalive", "keepalive", None))
        self.assertEqual((a["vram_real"], a["util_real"]), (0, 0))
        # 规则写的是 GPU 进程自己的命令行，仍以规则为准
        a = F.analyze(F.parse(raw), cfg(rules=[{"match": r"ka/train_keep_alive\.py", "kind": "job", "label": "伪装"}]))
        self.assertEqual(a["state"], "job")

    def test_relative_keepalive(self):
        """相对路径起的脚本按工作目录还原后再比字样与规则（#29）：写完整路径的字样（如页面上认作保活加的）也认得相对路径起的同一脚本，
        别的目录里的同名脚本不认；每组负载带上它的 GPU 进程（gpids，借用前逐个核对）。"""
        def run(args, cwd, words):
            raw = probe_out(apps=[(500, 6748)], maps=[(9, 500, cwd)], ps=[(1, 0, "/bin/bash"), (9, 1, args, 100.0, 1024)])
            return F.analyze(F.parse(raw), cfg(keepalive=words))
        full = ["keep_alive", "/root/x/occupy_gpu.py"]
        for args, cwd in (("/opt/py/bin/python /root/x/occupy_gpu.py", "/"), ("/opt/py/bin/python -u ./occupy_gpu.py --mb 6000", "/root/x"),
                          ("python3 ../x/occupy_gpu.py", "/root/y")):
            a = run(args, cwd, full)
            self.assertEqual((a["state"], a["work"][0]["gpids"]), ("keepalive", [9]), args)
        self.assertEqual(run("python occupy_gpu.py", "/root/y", full)["state"], "job")         # 别的目录里的同名脚本
        self.assertEqual(run("python -c occupy_gpu.py", "/root/x", full)["state"], "job")
        a = run("python ./occupy_gpu.py", "/root/x", ["keep_alive"])
        self.assertEqual(a["state"], "job")
        a = F.analyze(F.parse(probe_out(apps=[(500, 6748)], maps=[(9, 500, "/root/x")], ps=[(1, 0, "/bin/bash"), (9, 1, "python occupy_gpu.py", 100.0, 1024)])),
                      cfg(rules=[{"match": r"/root/x/occupy_gpu\.py", "kind": "keepalive"}]))
        self.assertEqual(a["state"], "keepalive")                                            # 规则同样比还原后的
        a = F.analyze(F.parse(probe_out(apps=[(500, 6748)], maps=[(9, 500, "/root/x")], ps=[(1, 0, "/bin/bash"), (9, 1, "python train_keep_alive.py", 100.0, 1024)])),
                      cfg(rules=[{"match": r"^python train_keep_alive\.py$", "kind": "job"}]))
        self.assertEqual(a["state"], "job")                     # 原命令行上带锚点的规则照旧命中：还原路径只是另一个候选（PR #34 审查）
        self.assertEqual([F._abs_script(x, "/w") for x in ("python a.py", "python -W ignore ./d/../a.py", "python /a.py", "python -m a", "python -uc a.py",
                                                            "bash a.sh", "python")],
                         ["/w/a.py", "/w/a.py", None, None, None, None, None])
        self.assertIsNone(F._abs_script("python a.py", None))

    def test_rule_can_mark_keepalive(self):
        a = F.analyze(F.parse(GUARD), cfg(rules=[{"match": r"run_parallel\.py", "kind": "keepalive"}]))
        self.assertEqual(a["state"], "keepalive")
        self.assertEqual(a["util_real"], 0)

    def test_light_job_is_shareable(self):
        raw = probe_out(apps=[(700, 3000)], maps=[(40, 700, "/w")], util=(10, 12), cores=1.0, mem_gb=10, limit_gb=122, quota=17,
                        ps=[(1, 0, "/bin/bash"), (40, 1, "python small.py", 50.0, 1024 * 1024)])
        a = F.analyze(F.parse(raw), cfg())
        self.assertEqual(a["state"], "job")
        self.assertTrue(a["share"])

    def test_page_cache_not_counted_for_share(self):
        # 读过大文件留下的活跃页缓存收得回，不算“在用”：可共享的内存按不算页缓存的 mem_anon（与借用的预留同口径）。
        # 27-15151（11-10）：VS Code + 终端、GPU 上只有保活，内存用量 29 GiB / 61 GiB 里 28 GiB 是页缓存，被判成不可共享、借不了
        raw = probe_out(apps=[(700, 3000)], maps=[(40, 700, "/w")], util=(10, 12), cores=1.0, mem_gb=40, inact_gb=2, anon_gb=1, limit_gb=61, quota=17,
                        ps=[(1, 0, "/bin/bash"), (40, 1, "python small.py", 50.0, 1024 * 1024)])
        a = F.analyze(F.parse(raw), cfg())
        self.assertGreater(a["mem_real"] / 61, 0.3)        # 含页缓存的已超阈值
        self.assertTrue(a["share"])
        raw = probe_out(apps=[(700, 3000)], maps=[(40, 700, "/w")], util=(10, 12), cores=1.0, mem_gb=40, inact_gb=2, anon_gb=30, limit_gb=61, quota=17,
                        ps=[(1, 0, "/bin/bash"), (40, 1, "python small.py", 50.0, 1024 * 1024)])
        self.assertFalse(F.analyze(F.parse(raw), cfg())["share"])     # 真占着的超了照样不可共享

    def test_idle_and_unmapped(self):
        a = F.analyze(F.parse(probe_out(used=0, util=(0, 0))), cfg())
        self.assertEqual((a["state"], a["work"]), ("idle", []))
        a = F.analyze(F.parse(probe_out(apps=[(999, 500)])), cfg())      # 容器里找不到：唯一的“未识别”
        self.assertEqual(a["state"], "unknown")
        self.assertIn("999", a["work"][0]["label"])


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = self.tmp.name
        self.calls = []
        self.out = {"a": KA, "b": GUARD}

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, s):
        self.calls.append(s["id"])
        r = self.out[s["id"]]
        return (r, None) if r else (None, "Connection refused")

    def test_bad_stderr_is_not_replaced_or_returned_as_probe_data(self):
        def run(cmd, **kw):
            return subprocess.run([sys.executable, '-c',
                "import os; os.write(1, b'@@ info\\n'); os.write(2, b'private-path-\\xff')"], **kw)
        with unittest.mock.patch.object(F.L, 'ssh_run', side_effect=run), \
                unittest.mock.patch.object(F, '_ssh_cmd', return_value=['ssh']):
            raw, error = F.fetch(cfg()['servers'][0])
            self.assertIsNone(raw)
            self.assertIn('UTF-8', error)
            self.assertNotIn('private-path', error)
            with self.assertRaises(UnicodeDecodeError):
                F.ssh(cfg()['servers'][0], 'operation')

    def test_bad_probe_bytes_reject_node_and_advance_polling(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                # 真正触发 subprocess 的文本解码；不启动 SSH、不接触生产。
                return subprocess.run([sys.executable, '-c',
                    "import os; os.write(1, b'@@ info\\ninvalid-path-\\xff')"], **kw)
            return subprocess.CompletedProcess(cmd, 0, GUARD, '')
        with unittest.mock.patch.object(F.L, 'ssh_run', side_effect=run), \
                unittest.mock.patch.object(F, '_ssh_cmd', return_value=['ssh']), \
                unittest.mock.patch.object(F, 'conf', return_value=cfg()):
            F.tick(T0, F.fetch, cfg())
            first = F._load_state()['a']
            self.assertEqual(first['last'], T0)
            self.assertEqual(first['fails'], 1)
            self.assertIn('UTF-8', first['error'])
            F.tick(T0 + 1, F.fetch, cfg())
        self.assertEqual(len(calls), 2)
        self.assertEqual(F._load_state()['b']['last'], T0 + 1)

    def test_round_robin_down_up(self):
        c = cfg()
        self.assertEqual(F.tick(T0, self.fetch, c), [])
        F.tick(T0 + 60, self.fetch, c)
        F.tick(T0 + 120, self.fetch, c)                       # 两台都没到期：不探
        self.assertEqual(self.calls, ["a", "b"])
        self.out["a"] = None
        self.assertEqual(F.tick(T0 + 600, self.fetch, c), [])  # 第一次连不上不报
        self.assertEqual(F.tick(T0 + 660, self.fetch, c), [])  # 隔一分钟再试；与看板主机同一规则，持续约 3 分钟才算掉线
        self.assertEqual(self.calls[-1], "a")
        F.tick(T0 + 720, self.fetch, c)                        # 这一轮轮到 b
        msgs = F.tick(T0 + 780, self.fetch, c)
        self.assertEqual(len(msgs), 1)
        self.assertIn("掉线", msgs[0])
        F.tick(T0 + 1320, self.fetch, c)                       # 已判掉线：回到正常间隔，先探 b
        self.assertEqual(self.calls[-1], "b")
        self.out["a"] = KA
        msgs = F.tick(T0 + 1400, self.fetch, c)
        self.assertEqual(self.calls[-1], "a")
        self.assertTrue(msgs and "恢复" in msgs[0])
        kinds = [e["kind"] for e in records()][::-1]
        self.assertEqual(kinds[-2:], ["fleet_down", "fleet_up"])
        self.assertEqual(records(who="AA")[0]["kind"], "fleet_up")             # 带上当时的使用人
        self.assertNotIn("fleet_unknown", kinds)
        rows = [json.loads(l) for l in (F.state_dir() / "series" / "a.jsonl").read_text().splitlines()]
        self.assertEqual([r["st"] for r in rows], ["keepalive", "down", "keepalive"])   # 第一次连不上不记

    def test_hours_and_view(self):
        c = cfg()
        for k in range(8):                                     # 跨过两个整点
            F.tick(T0 + k * 600, self.fetch, c)
        F.tick(T0 + 8 * 600, self.fetch, c, force="a")
        rows = load_rows(F.state_dir() / "hours" / "a.jsonl")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["holder"], "AA")
        self.assertEqual(rows[0]["c"], {"keepalive": rows[0]["n"]})
        mine = [{"id": "m", "label": "我的", "status": fake_status(gpu_procs=[{"owner": "job", "mb": 100}], running=[{"name": "x"}],
                                                               info={"gpus": [{"used": 100, "total": 1000, "util": 50}]})}]
        v = FV.view(mine, T0 + 8 * 600, c)
        a, b = v["servers"]
        self.assertEqual((a["state"], b["state"]), ("keepalive", "job"))
        self.assertEqual(b["sample"]["work"][0]["owner"], "BB")     # 路径对不上人：算使用人
        self.assertNotIn("owner", a["sample"]["work"][0])            # 保活不归人
        self.assertEqual(v["quota"], {"private": 1, "private_max": 4, "public": 1, "public_max": 2, "private_pools": ["P"]})
        self.assertEqual(len(a["strip"]), 168)
        self.assertEqual(a["strip"][-1][0], "keepalive")
        self.assertEqual(a["s7"]["frac"]["keepalive"], 1.0)
        self.assertEqual(v["unknown"], [])
        self.assertEqual({h["holder"] for h in v["holders"]}, {"AA", "BB"})
        self.assertEqual(FV.detail("a", t=T0 + 8 * 600)["series"][-1]["st"], "keepalive")
        p = FV.person("AA", t=T0 + 8 * 600)                   # 人员详情：逐日（含当前这一小时）与按服务器
        self.assertEqual([s["id"] for s in p["servers"]], ["a"])
        self.assertEqual(p["total"]["frac"]["keepalive"], 1.0)
        self.assertGreaterEqual(len(p["days"]), 30)
        self.assertAlmostEqual(sum(d["h"]["keepalive"] for d in p["days"]), p["total"]["hours"], delta=0.2)
        self.assertEqual(FV.person("ZZ", t=T0 + 8 * 600)["servers"], [])

    def test_borrowed_hours_go_to_borrower(self):
        """借用中的样本另记进小时行的 bw[借的人]（行的总数仍是整台的，服务器卡的逐小时条不变），按人统计时算给借的人：
        主人那份记成“借出”（算进时间占比，不算进利用率），借的人那份带 from；还了以后的样本照旧算主人的（#52）。"""
        c = cfg(me="ME")
        c["servers"][0] = {**c["servers"][0], "borrow_since": "2026-11-01 00:00"}
        self.out["a"] = GUARD                                 # 借用中：在跑（借的人的）实验
        for k in range(6):
            F.tick(T0 + k * 600, self.fetch, c, force="a")
        c["servers"][0]["borrow_returned"] = "2026-11-01 01:00"
        self.out["a"] = KA                                    # 还了：只剩主人的保活
        for k in range(6, 12):
            F.tick(T0 + k * 600, self.fetch, c, force="a")
        F.tick(T0 + 12 * 600, self.fetch, c, force="a")
        t = T0 + 12 * 600
        rows = load_rows(F.state_dir() / "hours" / "a.jsonl") + [F.accs()["a"]]
        self.assertEqual(sum(r["n"] for r in rows), 13)
        self.assertEqual(sum((r.get("bw") or {}).get("ME", {}).get("n", 0) for r in rows), 6)
        self.assertEqual({r["holder"] for r in rows}, {"AA"})
        v = FV.view([], t, c)
        hs = {h["holder"]: h for h in v["holders"]}
        aa, me = hs["AA"], hs["ME"]
        self.assertEqual(aa["frac"]["job"], 0)                # 主人自己没跑实验
        self.assertGreater(aa["frac"]["lent"], 0.3)
        self.assertIsNone(aa["util_job"])                     # 借出那段不算进主人的利用率
        self.assertEqual([(x["id"], x["to"]) for x in aa["lent"]], [("a", "ME")])
        self.assertEqual(me["frac"]["job"], 1.0)
        self.assertEqual([(x["id"], x["from"]) for x in me["borrowed"]], [("a", "AA")])
        self.assertAlmostEqual(me["hours"] + aa["hours"] - aa["h"]["lent"], aa["hours"], delta=0.05)   # 借出的小时数 = 借来的小时数
        self.assertAlmostEqual(aa["h"]["lent"], me["hours"], delta=0.05)
        self.assertEqual(v["servers"][0]["borrow"]["by"], "ME")
        self.assertTrue(any(c_ and len(c_) > 2 and c_[2] == ["ME"] for c_ in v["servers"][0]["strip"]))   # 逐小时条：借出的小时带借的人
        p = FV.person("ME", t=t)
        self.assertEqual([(s["id"], s.get("from")) for s in p["servers"]], [("a", "AA")])
        self.assertEqual(p["total"]["frac"]["job"], 1.0)
        p = FV.person("AA", t=t)
        self.assertEqual([(s["id"], s.get("lent_to")[0]["to"]) for s in p["servers"]], [("a", "ME")])
        self.assertEqual(p["total"]["frac"]["job"], 0)
        self.assertAlmostEqual(sum(d["h"].get("lent", 0) for d in p["days"]), aa["h"]["lent"], delta=0.05)

    def test_stats_by_hour(self):
        """按人统计按小时归一：一人两台，一台每小时 60 次样本（自用主机每分钟一次）、一台 6 次（探针），各一小时，时间占比各一半。"""
        from labmon.state import append_row
        h = int(T0 // 3600 * 3600)
        blank = {"sh": 0, "uj": 0.0, "vj": 0.0, "cr": 0.0, "mr": 0.0, "nr": 0}
        append_row(F.state_dir() / "hours" / "p.jsonl", {"h": h, "holder": "X", "n": 60, "c": {"idle": 60}, **blank}, 10 ** 9, T0)
        append_row(F.state_dir() / "hours" / "q.jsonl", {"h": h, "holder": "X", "n": 6, "c": {"job": 6}, **blank, "uj": 480.0}, 10 ** 9, T0)
        st = FV.person("X", t=T0 + 3600)["total"]
        self.assertEqual((st["hours"], st["frac"]["job"], st["frac"]["idle"], st["util_job"]), (2, 0.5, 0.5, 80.0))

    def test_mine_usage_hours(self):
        """自用主机也有使用记录（#52）：每轮按 worker 状态记一次（fleet.from_worker，与探针同口径），view 给它逐小时条与统计，人员里有这人。
        状态只看 GPU 进程：labmon 的任务与容器里找得到的 labmon 之外的进程算实验，对不回容器的算未识别，只有保活算保活，只有 CPU 的任务算空闲；
        本机不在内网、worker 状态过期不记，掉线记掉线；fleet 的 _tick 不会删掉它的当前小时累计。"""
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "m"\nssh = "root@h"\nport = 1\nplatform_job = "ALICE"\nplatform_pool = "P"\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            c = cfg()
            g = {"info": {"gpus": [{"used": 9000, "total": 46000, "util": 90}], "mem": {"used": 10, "limit": 100}, "cpu_used": 4, "cpu_quota": 16}}
            st = lambda procs, **kw: fake_status(gpu_procs=procs, **{**g, **kw})  # noqa: E731
            fw = lambda procs, **kw: F.from_worker(st(procs, **kw), c)["state"]  # noqa: E731
            self.assertEqual(fw([{"owner": "job", "pid": 5, "mb": 9000}]), "job")
            self.assertEqual(fw([{"owner": "other", "pid": 5, "mb": 9000}]), "job")       # 探针口径：找得到、不是保活的算实验
            self.assertEqual(fw([{"owner": "other", "pid": None, "mb": 9000}]), "unknown")
            self.assertEqual(fw([{"owner": "keepalive", "pid": 5, "mb": 9000}]), "keepalive")
            self.assertEqual(fw([], running=[{"name": "cpu_only"}]), "idle")             # 只有 CPU 的任务不占 GPU
            a = F.from_worker(st([{"owner": "keepalive", "pid": 5, "mb": 6000}, {"owner": "job", "pid": 6, "mb": 3000}]), c)
            self.assertEqual((a["state"], a["vram_real"], a["util_real"], a["vram_total"]), ("job", 3000, 90, 46000))
            self.assertEqual(FB.mine(c, [{"id": "m", "status": st([])}])[0]["sample"], None)     # 没标 worker_up：状态不新鲜
            e = lambda procs, **kw: {"id": "m", "worker_up": True, "lan_off": False, "status": st(procs), **kw}  # noqa: E731
            for k in range(70):                               # 跨过整点；最后 10 轮是只有保活
                F.record_mine(FB.mine(c, [e([{"owner": "job", "pid": 5, "mb": 9000}] if k < 60 else [{"owner": "keepalive", "pid": 5, "mb": 900}])]), T0 + k * 60, c)
            F.record_mine(FB.mine(c, [e([], lan_off=True)]), T0 + 70 * 60, c)                 # 不在内网：不记
            F.record_mine(FB.mine(c, [e([], worker_up=False)]), T0 + 71 * 60, c)              # worker 状态过期：不记
            F.record_mine(FB.mine(c, [e([], health={"state": "down"})]), T0 + 72 * 60, c)     # 掉线：记掉线
            F.tick(T0 + 73 * 60, self.fetch, c)                                            # 组内的 _tick 不删自用主机的累计
            rows = load_rows(F.state_dir() / "hours" / "m.jsonl") + [F.accs()["m"]]
            self.assertEqual(sum(r["n"] for r in rows), 71)
            self.assertEqual(sum(r["c"].get("down", 0) for r in rows), 1)
            self.assertEqual({r["holder"] for r in rows}, {"ALICE"})
            t = T0 + 73 * 60
            v = FV.view([e([{"owner": "job", "pid": 5, "mb": 9000}])], t, c)
            m = v["mine"][0]
            self.assertEqual((len(m["strip"]), m["strip"][-1] is not None), (168, True))
            self.assertEqual(m["s30"]["frac"]["job"], 0.5)          # T0 是整点：头一小时全是实验，第二小时是保活与掉线（按小时算，各占一半）
            self.assertEqual(m["s30"]["util_job"], 90)
            x = next(h for h in v["holders"] if h["holder"] == "ALICE")
            self.assertEqual((x["servers"], x["borrowed"], x["lent"]), (["m"], [], []))
            self.assertEqual(FV.person("ALICE", t=t)["servers"][0]["id"], "m")
        finally:
            os.environ["LABMON_HOSTS"] = NO_HOSTS

    def test_mine_other_process_is_unknown(self):
        """自用主机当成组内的一台（fleetboot.mine）：labmon 之外的 GPU 进程算未识别（勾「已说好」才动），worker 的任务才算实验；
        没状态的是 None（没探到）；不给快照时只有清单里的字段（平台操作用），known_hosts 是拉起 worker 用的那份。"""
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "m"\nssh = "root@h"\nport = 1\nplatform_job = "ALICE-2"\nplatform_pool = "P"\nknown_hosts = "/me/kh"\n'
                      '[[hosts]]\nid = "n"\nssh = "root@h"\nport = 2\n')                     # 没写 platform_job：不算
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            row = lambda st: FB.mine(None, [{"id": "m", "status": st and fake_status(**st)}])[0]
            r = row({"gpu_procs": [{"owner": "other", "comm": "python", "mb": 900}]})
            self.assertEqual((r["state"], r["work"]), ("unknown", [{"kind": "unknown", "label": "python", "mb": 900}]))
            self.assertEqual((r["holder"], r["job"], r["known_hosts"], r["mine"]), ("ALICE", "ALICE-2", "/me/kh", True))
            self.assertEqual(row({"gpu_procs": [{"owner": "other"}], "running": [{"name": "x"}]})["state"], "job")
            self.assertEqual(row({"gpu_procs": [{"owner": "keepalive"}]})["state"], "keepalive")
            self.assertIsNone(row(None)["state"])
            self.assertEqual([(x["id"], x["state"], x["pool"], x["port"]) for x in FB.mine(cfg())], [("m", None, "P", 1)])
            self.assertEqual([x["id"] for x in FB.slots(cfg())], ["a", "b", "m"])
        finally:
            os.environ["LABMON_HOSTS"] = NO_HOSTS

    def test_mine_share_ignores_page_cache(self):
        """自用主机的可共享同样按不算页缓存的内存（worker 的 mem.anon）：工作集 50/61 GiB 多是活跃页缓存时仍可共享（11-10 急修）。"""
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "m"\nssh = "root@h"\nport = 1\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            info = {"gpus": [{"i": 0, "uuid": "G0", "name": "A6000", "used": 6000, "total": 46000, "free": 40000, "util": 0}],
                    "mem": {"used": 50.0, "limit": 61.0, "anon": 1.0, "src": "cgroup"}, "cpu_used": 0.5, "cpu_quota": 17.0, "nproc": 128}
            procs = [{"owner": "keepalive", "pid": 14, "hpid": 104, "mb": 6000, "comm": "python"}]
            self.assertTrue(F.from_worker(fake_status(t=T0, info=info, gpu_procs=procs, running=[]), cfg())["share"])
            info["mem"]["anon"] = 40.0
            self.assertFalse(F.from_worker(fake_status(t=T0, info=info, gpu_procs=procs, running=[]), cfg())["share"])
        finally:
            os.environ.pop("LABMON_HOSTS", None)

    def test_mine_drawer_fields(self):
        """自用主机的抽屉与组内服务器同一套（#58）：from_worker 给的样本与 analyze 同形（work 按任务归组、gpus、mem、CPU 配额、本地盘、保活显存），
        fleetboot.mine 的行带与 view.servers 同名的字段（last、started、down、public、error），view 给保活守护的 worker 状态；
        record_mine 按 interval 节流写组内的逐次样本（与探针同形），曲线接口认它。平台操作口径（state、work）不变。"""
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "m"\nssh = "root@h"\nport = 1\nplatform_job = "ALICE"\nplatform_pool = "P"\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            c = cfg()
            info = {"gpus": [{"i": 0, "uuid": "G0", "name": "A6000", "used": 16000, "total": 46000, "free": 30000, "util": 90}],
                    "mem": {"used": 30.0, "limit": 120.0, "anon": 8.0, "src": "cgroup"}, "cpu_used": 7.0, "cpu_quota": 17.0, "nproc": 128,
                    "disk": [{"path": "/root/data1/alice", "free": 100.0, "total": 1024.0}], "started": T0 - 7200,
                    "rootfs": {"used": 50.0, "total": 750.0, "fs": "overlay", "ok": True}}
            procs = [{"owner": "job", "job": "t0", "pid": 11, "hpid": 101, "mb": 3000, "comm": "python"}, {"owner": "job", "job": "t0", "pid": 12, "hpid": 102, "mb": 1000, "comm": "python"},
                     {"owner": "job", "job": "t1", "pid": 13, "hpid": 103, "mb": 5000, "comm": "python"}, {"owner": "keepalive", "pid": 14, "hpid": 104, "mb": 6000, "comm": "python"},
                     {"owner": "other", "pid": 15, "hpid": 105, "mb": 500, "comm": "jupyter"}, {"owner": "other", "pid": None, "hpid": 106, "mb": 200, "comm": None}]
            running = [{"name": "t0", "project": "p", "t0": T0 - 600, "root": "/root/data1/alice/p"}, {"name": "t1", "project": "p", "t0": T0 - 60, "root": "/root/data1/alice/p"},
                       {"name": "cpu_only", "project": "p", "t0": T0 - 30, "root": "/root/data1/alice/p"}]
            st = fake_status(t=T0, info=info, gpu_procs=procs, running=running)
            a = F.from_worker(st, c, "ALICE")
            w = {x["label"]: x for x in a["work"]}
            self.assertEqual((w["t0"]["kind"], w["t0"]["mb"], w["t0"]["n"], w["t0"]["etime"], w["t0"]["cwd"], w["t0"]["owner"]), ("job", 4000, 2, 600, "/root/data1/alice/p", "ALICE"))
            self.assertEqual((w["t1"]["mb"], w["t1"]["n"]), (5000, 1))
            self.assertEqual([x["kind"] for x in a["work"]], ["job", "job", "job", "unknown", "keepalive"])   # 按 STATES 的先后，同类按显存
            self.assertIn("labmon 之外：jupyter", w)
            self.assertIn("宿主机 PID 106（容器里找不到）", w)                     # 与 analyze 同一写法
            self.assertFalse(any(x.get("pid") for x in a["work"]))               # 不带 pid：页面上不出「这是保活」（worker 不认 [fleet] keepalive）
            self.assertEqual((a["vram_ka"], a["cpu"], a["cpu_quota"], a["nproc"], a["mem"]["limit"], a["mem_ka"], a["root"], a["started"]),
                             (6000, 7.0, 17.0, 128, 120.0, None, {"used": 50.0, "total": 750.0, "fs": "overlay", "ok": True}, T0 - 7200))   # 根盘取 worker 的 info.rootfs（#149）
            self.assertEqual([g["uuid"] for g in a["gpus"]], ["G0"])
            self.assertEqual((a["state"], a["vram_real"], a["util_real"]), ("job", 9700, 90))     # 已有的口径不变（小时计数照旧）
            e = {"id": "m", "worker_up": True, "lan_off": False, "status": st, "lease_end": T0 + 86400,
                 "health": {"state": "ok", "fails": 0, "since": None, "why": None}}
            r = FB.mine(c, [e])[0]
            self.assertEqual((r["last"], r["started"], r["public"], r["down"], r["error"]), (T0, T0 - 7200, False, None, None))
            self.assertFalse(any(k in r for k in ("util", "vram", "vram_total", "vram_real")))      # 利用率、显存只在 sample 里
            self.assertEqual(r["sample"]["work"], a["work"])
            # 平台操作口径不变：只有 CPU 任务也算 job，work 是 worker 的任务名（含只用 CPU 的）加 labmon 之外的进程
            self.assertEqual(r["state"], "job")
            self.assertEqual([x["label"] for x in r["work"]][:3], ["t0", "t1", "cpu_only"])
            cpu = FB.mine(c, [{**e, "status": fake_status(t=T0, info=info, running=[running[2]], gpu_procs=[procs[3]])}])[0]
            self.assertEqual((cpu["state"], cpu["sample"]["state"]), ("job", "keepalive"))
            with unittest.mock.patch.object(FO, "snapshot", lambda: {"hosts": [{**e, "status": fake_status(t=T0, info=info, running=[running[2]], gpu_procs=[procs[3]])}]}):
                self.assertEqual(FO._probe_mine("m"), ("job", "cpu_only"))
            off = FB.mine(c, [{**e, "lan_off": True}])[0]
            self.assertEqual((off["sample"], off["error"]), (None, "本机不在内网"))
            stale = FB.mine(c, [{**e, "worker_up": False, "age": 900}])[0]
            self.assertIsNone(stale["sample"])
            self.assertIn("worker", stale["error"])
            down = FB.mine(c, [{**e, "health": {"state": "down", "fails": 3, "since": T0 - 300, "why": "连不上"}}])[0]
            self.assertEqual((down["state"], down["down"], down["since"]), ("down", "连不上", T0 - 300))
            # 保活守护：自用主机的保活由 worker 停启，守护不管（以前 mine 行没有 guard，页面当作没探到）
            self.assertEqual(F.guard_state(r, {}, c, T0), {"on": False, "status": "worker"})
            v = FV.view([e], T0, c)
            self.assertEqual(v["mine"][0]["guard"]["status"], "worker")
            # 组内的逐次样本：第一轮写一行，同一 interval 里不再写，满 interval 再写；掉线写 down
            for k in range(12):
                F.record_mine(FB.mine(c, [e]), T0 + k * 60, c)
            F.record_mine(FB.mine(c, [{**e, "health": {"state": "down", "fails": 3, "since": T0, "why": "x"}}]), T0 + 1300, c)
            rows = load_rows(F.state_dir() / "series" / "m.jsonl")
            self.assertEqual([r["t"] - T0 for r in rows], [0, 600, 1300])
            self.assertEqual((rows[0]["st"], rows[0]["vr"], rows[0]["vt"], rows[0]["cq"], rows[0]["ml"], rows[0]["mr"], rows[2]["st"]), ("job", 9700, 46000, 17.0, 120.0, 30.0, "down"))
            self.assertNotIn("bw", rows[0])
            self.assertEqual(len(FV.detail("m", t=T0 + 1300)["series"]), 3)
            self.assertTrue(FV.known("m", c) and FV.known("a", c))
            self.assertFalse(FV.known("zz", c))
        finally:
            os.environ["LABMON_HOSTS"] = NO_HOSTS

    def test_forget_and_accept_new(self):
        """换了容器清旧主机密钥：只删这一台 host:port 的条目（22 端口不带方括号），删状态目录的 known_hosts，自用主机再删它拉起 worker 用的那份；
        之后的 ssh 都是 accept-new（新密钥记下，变了照样拒连）。"""
        from labmon import lan as L
        calls = []
        with unittest.mock.patch.object(F.subprocess, "run", lambda cmd, **kw: calls.append(cmd)):
            F.forget({"ssh": "root@h", "port": 22})
            F.forget({"ssh": "root@h", "port": 15001, "known_hosts": "/me/known_hosts", "mine": True})
        kh = str(F.state_dir() / "known_hosts")
        self.assertEqual([c for c in calls if c[0] == "ssh-keygen"],
                         [["ssh-keygen", "-R", "h", "-f", kh], ["ssh-keygen", "-R", "[h]:15001", "-f", kh], ["ssh-keygen", "-R", "[h]:15001", "-f", "/me/known_hosts"]])
        self.assertEqual(calls[0][-3:], ["-O", "exit", "root@h"])
        argv = L.ssh_argv("root@h", 15001)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertFalse(any(a.startswith("UserKnownHostsFile") for a in argv))
        self.assertIn("UserKnownHostsFile=/x/kh", L.ssh_argv("root@h", 22, known_hosts="/x/kh"))

    def test_ssh_records_key_in_hosts_own_file(self):
        """连一台记主机密钥用哪份文件（fleet._ssh_cmd，#109）：服务器项带 known_hosts（自用主机，fleetboot.mine 给：主机清单写的或
        ~/.ssh/known_hosts）就用它，组内服务器与借来的用状态目录那份。以前一律状态目录：续租后等 ssh 的那次连接由它建起长连接，
        worker 与看板复用时不核对密钥，新容器的密钥一直没进 ~/.ssh/known_hosts，实验会话裸 ssh 被拒。"""
        kh = lambda argv: next(a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile="))
        self.assertEqual(kh(F._ssh_cmd({"ssh": "root@h", "port": 1})), str(F.state_dir() / "known_hosts"))
        self.assertEqual(kh(F._ssh_cmd({"ssh": "root@h", "port": 1, "known_hosts": "/me/kh"})), "/me/kh")
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "m"\nssh = "root@h"\nport = 1\nplatform_job = "ALICE"\nplatform_pool = "P"\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            s = next(x for x in FB.slots(cfg()) if x["id"] == "m")
            self.assertEqual(kh(F._ssh_cmd(s)), os.path.expanduser("~/.ssh/known_hosts"))      # 与拉起 worker、实验会话裸 ssh 同一份
            got = []
            with unittest.mock.patch.object(F.L, "ssh_run", lambda cmd, **kw: got.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")):
                F.ssh(s, "true")
            self.assertEqual(kh(got[0]), os.path.expanduser("~/.ssh/known_hosts"))
        finally:
            os.environ["LABMON_HOSTS"] = NO_HOSTS

    def test_lease_notices(self):
        c = cfg()
        F.tick(T0, self.fetch, c)
        F.tick(T0 + 60, self.fetch, c)
        st = F._load_state()
        st["a"]["started"] = T0 - 7 * 86400 + 25 * 3600       # 25 小时后到期
        st["b"]["started"] = T0 - 7 * 86400 + 26 * 3600       # 26 小时后到期：与 a 合成一条
        save_json(F.state_dir() / "state.json", st)
        self.out = {"a": None, "b": None}
        self.assertEqual(F.lease_notices(c, F._load_state(), T0), [])
        msgs = F.tick(T0 + 3700, lambda s: (None, "x"), c)
        self.assertEqual(len(msgs), 1)
        self.assertTrue("AA" in msgs[0] and "BB" in msgs[0])
        self.assertEqual(F.tick(T0 + 3800, lambda s: (None, "x"), c), [])
        msgs = F.tick(T0 + 19 * 3600 + 100, lambda s: (None, "x"), c)
        self.assertTrue(any("租期" in m and "AA" in m for m in msgs))

    def test_lease_due_and_container_step(self):
        """租期提醒的判定与看板主机共用（health.lease_due）：到期前 24、6 小时各一档，已进 6 小时内的不再补 24，相近到期的合成一组；
        换容器（health.container_step）清掉已发的档、给出带启动时刻的事件。"""
        a, b, c = {}, {}, {}
        ends = [(a, T0 + 25 * 3600), (b, T0 + 26 * 3600), (c, T0 + 5 * 3600)]
        self.assertEqual(lease_due(ends, T0, 2 * 3600), [[2]])                   # 只有 c 进了 6 小时
        self.assertEqual((sorted(c["warned"]), a["warned"]), ([6, 24], []))
        self.assertEqual(lease_due(ends, T0 + 3700, 2 * 3600), [[0, 1]])         # a 进了 24 小时，b 相近：合成一组
        self.assertEqual(lease_due(ends, T0 + 3800, 2 * 3600), [])
        self.assertEqual(lease_due([(a, T0 + 25 * 3600)], T0 + 19.5 * 3600), [[0]])   # a 再进 6 小时那档
        self.assertEqual(lease_due([(a, T0 + 25 * 3600)], T0 + 26 * 3600), [])     # 过期了不报
        self.assertIsNone(container_step(a, T0, "A"))                              # 第一次见：只记下
        self.assertEqual(a["started"], T0)
        self.assertIsNone(container_step(a, T0 + 30, "A"))                         # 差 30 秒：同一容器
        ev = container_step(a, T0 + 100, "A")
        self.assertEqual((ev["kind"], ev["started"], a["warned"], a["started"]), ("container_new", T0 + 100, [], T0 + 100))
        self.assertIn("A 换了新容器", ev["msg"])

    def test_lease_first_seen_within_6h(self):
        c = cfg(servers=cfg()["servers"][:1])
        F.tick(T0, self.fetch, c)
        st = F._load_state()
        st["a"]["started"] = T0 - 7 * 86400 + 3 * 3600
        save_json(F.state_dir() / "state.json", st)
        msgs = F.tick(T0 + 60, self.fetch, c)
        self.assertEqual(len([m for m in msgs if "租期" in m]), 1)        # 只报 6 小时那条，不再补 24 小时
        self.assertEqual(sorted(F._load_state()["a"]["warned"]), [6, 24])

    def test_container_new(self):
        c = cfg(servers=cfg()["servers"][:1])
        F.tick(T0, self.fetch, c)
        self.out["a"] = probe_out(apps=[(500, 6748)], maps=[(9, 500, "/k")], started=T0 + 100,
                                  ps=[(9, 1, "python train_keep_alive.py")])
        F.tick(T0 + 700, self.fetch, c)
        self.assertEqual((records()[0]["kind"], records()[0]["started"]), ("container_new", T0 + 100))   # 带容器启动时刻，项目页的里程碑取它
        self.assertEqual(F._load_state()["a"]["started"], T0 + 100)


IDLE = probe_out(used=0, util=(0, 0))
KA_PS = (9, 1, "/root/miniconda3/envs/py38/bin/python /root/data1/alice/ka/train_keep_alive.py", 100.0, 3 * 1024 * 1024)
KA_JOB = probe_out(apps=[(500, 6748), (700, 7800)], maps=[(9, 500, "/root/data1/alice/ka"), (40, 700, "/root/proj")],
                   ps=[(1, 0, "/bin/bash"), KA_PS, (40, 1, "/x/python train.py")])
JOB = probe_out(apps=[(700, 7800)], maps=[(40, 700, "/root/proj")], ps=[(1, 0, "/bin/bash"), (40, 1, "/x/python train.py")])


def guarded(raw, t, until, *more):
    """探针输出后面接上 fleet_guard.sh 记的 /tmp/labmon-guard.state（不带 proc 行：认不出身份，只管让路）。"""
    return raw + f"@@ guard\nstart {t} 9 {until} 守护 100100\n" + "".join(m + "\n" for m in more)


class LocalWritesTest(unittest.TestCase):
    """本地盘开机以来的新写入（#149）：探针的根盘与 /dev/shm 两行、scan 段的解析、要不要顺带扫（scan_due）、扫出的存进状态（record）、
    local_writes 与 lw_text、组内页的 lw；探针的 scan 段在假根盘上真跑（要 GNU find：CI 的 Linux 上有，macOS 自带的 BSD find 没有，跳过）。"""

    ROOTFS = "overlay 3758096384"           # 宿主盘 3.5 TiB：根盘 750 GiB 有配额，测得准
    SCAN = ["total 3145728 120 40", "2097152 100 1799990000 /root/.cache/hf", "1048576 20 1799991000 /root/.", "rc 0"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        s = F.parse(probe_out(root_gb=5, rootfs=self.ROOTFS, shm="shm 2097152", scan=self.SCAN))
        self.assertEqual((s["root"]["fs"], s["root"]["ok"], s["root"]["used"], s["root"]["shm"]), ("overlay", True, 5.0, 2.0))
        sc = s["scan"]
        self.assertEqual((sc["b"], sc["n"], sc["keys"], sc["partial"], sc["err"]), (3 * GB, 120, 40, False, None))
        self.assertEqual(sc["top"], [["/root/.cache/hf", 2 * GB, 1799990000], ["/root/.", GB, 1799991000]])
        self.assertTrue(F.parse(probe_out(rootfs=self.ROOTFS, scan=self.SCAN[:-1] + ["rc 124"]))["scan"]["partial"])   # 超时：没扫完
        self.assertEqual(F.parse(probe_out(rootfs=self.ROOTFS, scan=["nofind"]))["scan"]["err"], "nofind")
        self.assertIsNone(F.parse(probe_out(rootfs=self.ROOTFS))["scan"])
        r = lambda **kw: F.parse(probe_out(**kw))["root"]  # noqa: E731
        self.assertNotIn("shm", r(rootfs=self.ROOTFS, shm="tmpfs 2097152"))           # 与宿主共用的 /dev/shm（29 就是）不算这台的
        self.assertFalse(r(rootfs="ext4 3758096384")["ok"])                           # 不是 overlay：测不准
        self.assertFalse(r(rootfs="overlay 786432000")["ok"])                         # 根盘和宿主盘一样大：没配额
        self.assertFalse(r()["ok"])                                                   # 没有这一行

    def sample(self, state="idle", used=5.0, started=T0 - 86400):
        return dict(F.analyze(F.parse(probe_out(root_gb=used, rootfs=self.ROOTFS, started=started)), cfg()), state=state, t=T0)

    def test_scan_due(self):
        s = cfg()["servers"][0]
        st = {"sample": self.sample()}
        st0 = st["sample"]["started"]
        self.assertEqual(F.scan_due(s, st, T0), st0)                                   # 这个容器还没扫过
        self.assertEqual(F.scan_due(s, {"sample": self.sample("job")}, T0), st0)       # 对方在跑实验照样扫（维护者 11-10 定，#155）
        self.assertIsNone(F.scan_due({**s, "borrow_since": "2026-11-10 10:00"}, st, T0))   # 借用中：扫出来的混着我们的
        self.assertIsNone(F.scan_due(s, st, T0, off=True))
        self.assertIsNone(F.scan_due(s, {"sample": self.sample(used=0.01)}, T0))      # 几乎没写：不扫
        self.assertIsNone(F.scan_due(s, {"sample": dict(self.sample(), root={"used": 5.0, "total": 750.0, "ok": False})}, T0))   # 测不准
        sc = F.parse(probe_out(rootfs=self.ROOTFS, scan=self.SCAN))["scan"]
        st["wrote"] = F._wrote(sc, st["sample"], None, T0)
        self.assertIsNone(F.scan_due(s, st, T0 + 4000))                                # 已用没变：不再扫
        st["sample"] = self.sample(used=5.2)
        self.assertIsNone(F.scan_due(s, st, T0 + 600))                                 # 变了 200 MiB，但离上次不到 1 小时
        self.assertEqual(F.scan_due(s, st, T0 + 3600), st0)
        st["wrote"] = F._wrote(dict(sc, partial=True), st["sample"], st["wrote"], T0)  # 没扫完：间隔翻倍
        self.assertEqual(st["wrote"]["fails"], 1)
        self.assertIsNone(F.scan_due(s, st, T0 + 3600))
        self.assertEqual(F.scan_due(s, st, T0 + 7200), st0)                            # 到点再扫，已用没变也扫
        st["wrote"] = F._wrote(dict(sc, err="nofind"), st["sample"], st["wrote"], T0)
        self.assertIsNone(F.scan_due(s, st, T0 + 2 * 86400))                           # 扫不了（没有 GNU find）：这个容器不再扫
        st["sample"] = self.sample(started=T0 - 600)
        self.assertEqual(F.scan_due(s, st, T0 + 60), T0 - 600)                         # 换了容器（续租、重启）：重新扫

    def test_tick_scans_and_view(self):
        c = cfg(servers=[cfg()["servers"][0]])
        got = []

        def fetch(s, scan=None):
            got.append(scan)
            return probe_out(root_gb=5, rootfs=self.ROOTFS, scan=self.SCAN if scan else None), None
        F.tick(T0, fetch, c)
        F.tick(T0 + 600, fetch, c)
        self.assertEqual(got, [None, T0 - 86400])         # 第一次还没有样本、不知道测不测得准：不扫；之后顺带扫，只数开机以后的
        st = F._load_state()["a"]
        self.assertNotIn("scan", st["sample"])                                        # 扫出来的不进样本
        self.assertEqual((st["wrote"]["b"], st["wrote"]["top"][0][0], st["wrote"]["started"], st["wrote"]["more"]),
                         (3 * GB, "/root/.cache/hf", T0 - 86400, [38, 0]))
        F.tick(T0 + 1200, fetch, c)
        F.tick(T0 + 1300, fetch, c, force="a")
        self.assertEqual(got[2:], [None, None])                                        # 刚扫过不扫；点的立即探测不顺带扫（要快）
        x = FV.view([], T0 + 1300, c)["servers"][0]["lw"]
        self.assertEqual((x["used"], x["own"], x["ok"], x["risk"], x["scan"]["b"], x["scan"]["top"][0]),
                         (5.0, None, True, True, 3.0, ["/root/.cache/hf", 2.0, 1799990000]))
        self.assertEqual(F.lw_text(x, "关机"), "本地盘开机以来 labmon 之外写了 5.0 GiB（/root/.cache/hf、/root（本层）），关机会清空，要留的先挪到共享盘")

    def test_lw_text_same_as_page(self):
        """lw_text 与页面上的 lwText（overview.html）口径相同：同一份快照，tests/pages/check.js 对 JS 断言同一句；一、二级目录本层的文件写成「（本层）」。"""
        from pages import snapshots
        self.assertEqual(F.lw_text(snapshots._host_a()["lw"]),
                         "本地盘开机以来 labmon 之外写了 11.7 GiB（/root/.cache/huggingface/hub、/root/work/ckpt、/opt/conda/pkgs），续租会清空，要留的先挪到共享盘")
        lw = F.local_writes({"used": 2.0, "total": 750.0, "ok": True}, {"b": 2 * GB, "top": [["/.", GB, 1], ["/root/.", GB, 1]]})
        self.assertEqual(F.lw_text(lw, "关机"), "本地盘开机以来 labmon 之外写了 2.0 GiB（/（本层）、/root（本层）），关机会清空，要留的先挪到共享盘")

    def test_local_writes(self):
        root = {"used": 5.0, "total": 750.0, "fs": "overlay", "ok": True, "shm": 0.5}
        lw = F.local_writes(root, {"since": 1, "b": GB, "n": 2, "top": [["/root/a", GB, 9]], "more": [0, 0], "pending": 1}, 4 * GB)
        self.assertEqual((lw["user"], lw["own"], lw["shm"], lw["risk"], lw["scan"]["pending"]), (1.0, 4.0, 0.5, True, 1))   # labmon 的缓存不算，/dev/shm 算
        self.assertFalse(F.local_writes(dict(root, used=4.5, shm=None), None, 4 * GB)["risk"])        # labmon 之外只写了 0.5 GiB
        self.assertFalse(F.local_writes(dict(root, ok=False), None)["risk"])                           # 测不准：不拦
        self.assertEqual(F.lw_text(F.local_writes(dict(root, ok=False), None)), "")
        self.assertIsNone(F.local_writes(None, None))

    @unittest.skipUnless(b"GNU" in subprocess.run(["find", "--version"], capture_output=True).stdout, "要 GNU find（服务器与 CI 的 Linux 上有；macOS 自带的是 BSD find）")
    def test_probe_scan_on_fake_root(self):
        """探针的 scan 段真跑（LABMON_SCAN_ROOT 指到假根盘）：开机以前的目录整棵不进、/proc 不进，按前三级目录汇总，与 worker 的 LocalDisk 同一规则。"""
        with tempfile.TemporaryDirectory() as td:
            r = Path(td)

            def put(rel, n):
                f = r / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"x" * n)
            put("usr/lib/big.so", 400 * 1024)
            time.sleep(1.1)
            boot = int(time.time())
            time.sleep(1.1)
            for rel, n in (("root/.cache/hf/model.bin", 400 * 1024), ("root/.cache/pip/x.whl", 40 * 1024), ("root/notes.txt", 4096),
                           ("data/out/run1/a/b/ckpt.pt", 800 * 1024), ("tmp/other.bin", 4096), ("proc/self/junk", 4096), ("work/new/x.bin", 8192)):
                put(rel, n)
            out = subprocess.run(["bash", "-s"], input=F.PROBE.read_text(), capture_output=True, text=True, timeout=60,
                                 env=dict(os.environ, LABMON_SCAN=str(boot), LABMON_SCAN_ROOT=td)).stdout
            sc = F.parse(out)["scan"]
            self.assertEqual((sc["n"], sc["keys"], sc["partial"], sc["err"]), (6, 6, False, None))
            self.assertEqual([x[0] for x in sc["top"][:3]], ["/data/out/run1", "/root/.cache/hf", "/root/.cache/pip"])
            self.assertEqual({x[0] for x in sc["top"][3:]}, {"/work/new/x.bin", "/root/.", "/tmp/."})
            self.assertTrue(all(x[2] >= boot for x in sc["top"]))

    def probe_scan(self, apps, *ka, pids=(), full=False):
        """真跑探针，扫一个空的假根盘：假 nvidia-smi 的 compute-apps 给 apps（宿主机 PID 一行一个），ka 是认作保活的字样。返回 (scan 段原文, parse 出的 scan)；
        full 时返回 (探针整份输出, parse 出的 scan)。"""
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "bin"
            b.mkdir()
            nv = b / "nvidia-smi"
            nv.write_text('#!/bin/sh\ncase "$*" in *query-compute-apps*) cat "$NV_APPS" ;; esac\n')
            nv.chmod(0o755)
            (Path(td) / "apps").write_text("".join(f"{p}, 100, GPU-x\n" for p in apps))
            (Path(td) / "root").mkdir()
            env = dict(os.environ, LABMON_SCAN=str(int(time.time()) - 60), LABMON_SCAN_ROOT=str(Path(td) / "root"), LABMON_SCAN_S="10",
                       NV_APPS=str(Path(td) / "apps"), PATH=f"{b}{os.pathsep}{os.environ['PATH']}")
            out = subprocess.run(["bash", "-s", "--", *ka], input=F.PROBE.read_text(), capture_output=True, text=True, timeout=60, env=env).stdout
            return out if full else out.split("@@ scan\n", 1)[-1], F.parse(out)["scan"]

    def test_probe_scan_while_job_runs(self):
        """GPU 上有不是保活的进程（对方在跑实验、容器里对不上的宿主机 PID）照样扫（维护者 11-10 定，#155；以前写 busy 不扫）。"""
        for apps in (["999999"], []):
            raw, sc = self.probe_scan(apps, "keep_alive.py")
            self.assertNotIn("busy", raw)
            self.assertIsNotNone(sc)                                                   # 扫了（macOS 的 BSD find 是 nofind，也算扫过）

    @unittest.skipUnless(os.path.isdir("/proc/self") and shutil.which("realpath"), "要真的 /proc（Linux）")
    def test_probe_relative_keepalive(self):
        """探针按同一口径认保活（#29）：相对路径起的保活，字样写完整路径也读它的 PSS、报它的进程组（真跑 fleet_probe.sh 与一个真进程）。"""
        with tempfile.TemporaryDirectory() as td:
            ka = Path(td) / "ka"
            ka.mkdir()
            (ka / "occupy_gpu.py").write_text("import time\ntime.sleep(30)\n")
            p = subprocess.Popen([sys.executable, "./occupy_gpu.py"], cwd=ka, start_new_session=True)
            try:
                time.sleep(0.3)
                raw, _ = self.probe_scan([str(p.pid)], str(Path(os.path.realpath(ka)) / "occupy_gpu.py"), full=True)
            finally:
                p.kill()
                p.wait()
            smp = F.parse(raw)
            self.assertIn(p.pid, smp["pss"])
            self.assertEqual(smp["ka_groups"][p.pid][0], p.pid)


class GuardTest(unittest.TestCase):
    """保活守护：空闲满一小时开保活、没开成隔一阵再试、盯的时间过了由探测让出。只用 b（公用池，缺省开守护）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = self.tmp.name
        self.c = cfg()
        self.c["servers"] = [self.c["servers"][1]]
        self.raw, self.reply, self.calls, self.remote = IDLE, "ok start 9", [], []

        if not shutil.which("flock"):
            # macOS 自带的命令里没有 flock，测试仍在脚本继承的 fd 上拿真实文件锁。
            lock = Path(self.tmp.name) / "flock"
            lock.write_text(f"#!{sys.executable}\nimport fcntl, sys\nfcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX)\n")
            lock.chmod(0o755)

    def shell_env(self, d):
        return {**os.environ, "PATH": f"{d / 'bin'}:{d}:{os.environ['PATH']}", "GUARD_STATE": str(d / "guard.state"),
                "GUARD_PROC": str(d / "proc"), "GUARD_READY_S": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, s):
        self.calls.append(s["id"])
        return self.raw, None

    def ssh(self, s, remote, input=None, timeout=60):
        self.remote.append(remote)
        if self.reply.startswith("ok start"):              # 开成了：之后探到的是守护开的保活
            self.raw = guarded(KA, T0, T0 + 300)
        return 0, self.reply + "\n", ""

    def tick(self, t):
        return F.tick(t, self.fetch, self.c, ssh_fn=self.ssh, busy=FO.busy())

    def guard(self, t):
        return FV.view([], t, self.c)["servers"][0]["guard"]

    def test_start_after_idle_hour(self):
        for k in range(6):
            self.tick(T0 + k * 600)
        self.assertEqual(self.remote, [])
        g = self.guard(T0 + 3000)
        self.assertEqual((g["status"], g["since"], g["at"]), ("wait", T0, T0 + 3600))
        self.tick(T0 + 3600)
        self.assertEqual(len(self.remote), 1)
        self.assertTrue(self.remote[0].startswith("bash -s -- start 'bash /x/ka.sh' 300 "))     # 开了以后盯 5 分钟
        self.assertEqual(self.calls[-2:], ["b", "b"])                                         # 开了再探一次
        self.assertEqual(self.guard(T0 + 3601)["status"], "ours")
        ev = records(cat="keepalive")[0]
        self.assertEqual((ev["kind"], ev["host"], ev["who"]), ("guard_start", "b", "BB"))
        self.assertIn("空了 60 分钟", ev["msg"])
        self.tick(T0 + 4200)                                                                  # 保活在跑：不再开
        self.assertEqual(len(self.remote), 1)

    def test_already_running_refreshes_sample(self):
        def ssh(s, remote, **kwargs):
            self.raw = KA
            return 0, "ok already 9\n", ""

        for k in range(6):
            self.tick(T0 + k * 600)
        F.tick(T0 + 3600, self.fetch, self.c, ssh_fn=ssh, busy=[])
        self.assertEqual(self.calls[-2:], ["b", "b"])
        self.assertEqual(F._load_state()["b"]["sample"]["state"], "keepalive")
        self.assertEqual(records(cat="keepalive"), [])

    def test_off_busy_and_gap(self):
        self.c["servers"][0]["guard"] = False
        for k in range(8):
            self.tick(T0 + k * 600)
        self.assertEqual((self.remote, self.guard(T0 + 4200)["status"]), ([], "off"))
        del self.c["servers"][0]["guard"]
        with fake_op(["b"]):                                                                  # 在做平台操作：不动
            self.tick(T0 + 8 * 600)
            self.assertEqual((self.remote, self.guard(T0 + 4800)["status"]), ([], "busy"))
        self.tick(T0 + 8 * 600 + 1600)                                                        # 隔太久没探：从这次重新计时
        self.assertEqual(self.remote, [])
        self.assertEqual(self.guard(T0 + 6500)["since"], T0 + 8 * 600 + 1600)
        self.c["servers"][0]["pool"] = "P"                                                    # 专用池缺省不开
        self.assertFalse(F.guard_on(self.c["servers"][0], self.c))

    def test_fail_retry_and_notify_once(self):
        self.reply = "fail 保活没起来"
        for k in range(7):
            msgs = self.tick(T0 + k * 600)
        self.assertEqual(msgs, ["保活守护没能给 BB（b） 开保活：保活没起来"])
        g = self.guard(T0 + 4200)
        self.assertEqual((g["status"], g["at"]), ("fail", T0 + 3600 + F.GUARD_RETRY_S))
        self.assertEqual(self.tick(T0 + 4200), [])                                            # 隔 30 分钟才再试
        self.assertEqual(len(self.remote), 1)
        for k in range(1, 4):
            msgs = self.tick(T0 + 4200 + k * 600)
        self.assertEqual((len(self.remote), msgs), (2, []))                                   # 又没开成：不再弹
        self.assertEqual([e["kind"] for e in records(cat="keepalive")], ["guard_fail", "guard_fail"])
        self.reply = "ok start 9"
        for k in range(4, 7):
            self.tick(T0 + 4200 + k * 600)
        self.assertEqual(records(cat="keepalive")[0]["kind"], "guard_start")
        self.assertNotIn("guard_fail", F._load_state()["b"])

    def test_guard_process_without_gpu_keeps_failure_backoff(self):
        self.raw = guarded(probe_out(ps=[(1, 0, "/bin/bash"), KA_PS]), T0, T0 + 300)
        self.reply = "fail 保活进程已存在，尚未看到它占用 GPU"
        for k in range(7):
            self.tick(T0 + k * 600)
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(self.guard(T0 + 3600)["status"], "fail")
        self.tick(T0 + 4200)
        self.assertEqual(len(self.remote), 1)
        self.raw = guarded(KA, T0, T0 + 300)
        self.tick(T0 + 4800)
        self.assertEqual(self.guard(T0 + 4800)["status"], "ours")

    def stalled(self, *, util=(0, 0), started=T0 - 86400, birth=100100, born=100100, marker=True, job=False, second=None):
        """自己开的保活占住少量显存；登记来自手动开、续租和守护时同形。birth 是探针此刻读到的出生时钟，born 是登记时记下的（None：旧登记没有这一列）。"""
        ps = [(1, 0, "/bin/bash"), KA_PS]
        apps, maps = [(500, 384)], [(9, 500, "/x/ka")]
        if job:
            ps.append((40, 1, "/x/python train.py"))
            apps.append((700, 200))
            maps.append((40, 700, "/x/train"))
        raw = probe_out(apps=apps, maps=maps, ps=ps, used=384, util=util, started=started)
        if second is not None:
            raw = raw.replace("@@ util", "1, GPU-y, GPU, 0, 49140, 0\n@@ util")
            raw = raw.replace("@@ cpu", "".join(f"1, {v}\n" for v in second) + "@@ cpu")
        if marker:
            raw += f"@@ guard\nstart {T0 - 1000} 9 {T0 - 700} 看板{'' if born is None else f' {born}'}\nproc 9 {birth}\n"
        return raw

    def test_stalled_keepalive_restarts_and_refreshes(self):
        self.raw = self.stalled(second=(0, 0))
        self.tick(T0)
        F.tick(T0 + 1, self.fetch, self.c, force="b", ssh_fn=self.ssh)  # 紧接的手动探测不满足时长
        self.assertEqual(self.remote, [])
        self.tick(T0 + 601)
        self.assertEqual(self.remote, [f"bash -s -- repair 'bash /x/ka.sh' 300 '守护' 100000 {T0 - 1000} 9 100100 2"])
        self.assertEqual(self.calls[-2:], ["b", "b"])
        self.assertEqual(records(cat="keepalive")[0]["kind"], "guard_restart")
        self.assertEqual(self.guard(T0 + 602)["status"], "ours")
        self.assertNotIn("guard_zero", F._load_state()["b"])

    def manual(self, **kwargs):
        return self.stalled(marker=False, **kwargs) + "@@ ka_groups\n9 9 100100\n"

    def test_manual_stall_replaced_with_board_keepalive(self):
        self.raw = self.manual(second=(0, 0))
        self.tick(T0)
        self.assertEqual(self.remote, [])
        self.assertEqual(self.tick(T0 + 600), [])
        self.assertEqual(self.remote, ["bash -s -- repair 'bash /x/ka.sh' 300 '守护' 100000 manual 9 100100 2"])
        self.assertEqual(self.guard(T0 + 601)["status"], "ours")
        ev = records(cat="keepalive")[0]
        self.assertEqual(ev["kind"], "guard_restart")
        self.assertIn("停掉自行开启的空转保活并开启看板保活", ev["msg"])

    def test_manual_stall_warning_deduplicates_and_clears(self):
        self.raw, self.reply = self.manual(), "unmanaged 配置的保活脚本无法确认归属"
        self.tick(T0)
        msg, = self.tick(T0 + 600)
        self.assertIn("自行开启的保活持续空转", msg)
        self.assertEqual(self.guard(T0 + 600)["fail"]["msg"], msg)
        for k in range(2, 5):
            self.assertEqual(self.tick(T0 + k * 600), [])
        self.assertEqual(len(self.remote), 2)
        self.assertEqual([r["kind"] for r in records(cat="keepalive")], ["guard_fail"])
        self.raw = self.manual(util=(0, 30))
        self.assertEqual(self.tick(T0 + 3000), [])
        self.assertEqual(self.guard(T0 + 3000)["status"], "busy")
        self.assertNotIn("guard_zero", F._load_state()["b"])

    def test_manual_zero_without_identity_warns_without_remote_call(self):
        self.raw = self.stalled(marker=False)
        self.tick(T0)
        msg, = self.tick(T0 + 600)
        self.assertIn("无法确认唯一的保活进程组", msg)
        self.assertEqual(self.guard(T0 + 600)["status"], "fail")
        self.assertEqual(self.tick(T0 + 1200), [])
        self.assertEqual(self.remote, [])

    def test_manual_keepalive_with_activity_extra_load_or_missing_samples_stays_untouched(self):
        for raw in (self.manual(util=(0, 1)), self.manual(job=True), self.manual(second=(0, "N/A", 0))):
            with self.subTest(raw=raw):
                st = {}
                host = self.c["servers"][0]
                F.record(host, self.manual(), None, self.c, st, T0)
                F.record(host, raw, None, self.c, st, T0 + 600)
                self.assertNotIn("guard_zero", st)
                self.assertEqual(F.guard_act(host, st, self.c, T0 + 600, False, self.ssh), (None, []))
        self.assertEqual(self.remote, [])

    def test_manually_running_process_without_gpu_is_reported_when_start_fails(self):
        self.raw = probe_out(ps=[(1, 0, "/bin/bash"), KA_PS])
        self.reply = "fail 保活进程已存在，尚未看到它占用 GPU"
        for k in range(6):
            self.assertEqual(self.tick(T0 + k * 600), [])
        msg, = self.tick(T0 + 3600)
        self.assertIn("尚未看到它占用 GPU", msg)
        self.assertEqual(self.guard(T0 + 3600)["status"], "fail")

    def test_breaks_in_stall_evidence(self):
        for raw in (self.stalled(marker=False), self.stalled(util=(0, "N/A", 0)), self.stalled(job=True),
                    self.stalled(second=(1, 0)), self.stalled(second=()), self.stalled(birth=100500),
                    self.stalled(started=T0 - 72000), self.stalled(birth=10_000_000), self.stalled(born=None)):
            with self.subTest(raw=raw[-85:]):
                st = {}
                s = self.c["servers"][0]
                F.record(s, self.stalled(), None, self.c, st, T0)
                F.record(s, raw, None, self.c, st, T0 + 600)
                self.assertNotEqual(F.guard_state(s, st, self.c, T0 + 600).get("act"), "repair")
        st = {}
        s = self.c["servers"][0]
        F.record(s, self.stalled(), None, self.c, st, T0)
        F.record(s, None, "未连通", self.c, st, T0 + 600)
        F.record(s, self.stalled(), None, self.c, st, T0 + 1200)
        self.assertNotEqual(F.guard_state(s, st, self.c, T0 + 1200).get("act"), "repair")

    def test_stalled_recovery_waits_for_watch_window(self):
        self.raw = self.stalled().replace(f"9 {T0 - 700} 看板", f"9 {T0 + 650} 看板")     # 服务器上还在盯：归远端看门狗管
        self.tick(T0)
        self.tick(T0 + 600)
        self.assertEqual(self.remote, [])
        F.tick(T0 + 650, self.fetch, self.c, force="b", ssh_fn=self.ssh)                # 盯完就重开，不再为升级前的看门狗多等一轮
        self.assertEqual(len(self.remote), 1)

    def test_identity_compares_birth_clock_not_wall_clock(self):
        """登记后墙上时钟前跳：换算出的进程启动时刻晚于登记时刻，但出生时钟对得上，照样认得出、能重开（#26）。"""
        jumped = self.stalled(birth=10_000_000, born=10_000_000)
        a = F.analyze(F.parse(jumped), self.c)
        self.assertEqual(a["guard"]["identity"], [100000, T0 - 1000, 9, 10_000_000])
        self.raw = jumped
        self.tick(T0)
        self.tick(T0 + 600)
        self.assertEqual(self.remote, [f"bash -s -- repair 'bash /x/ka.sh' 300 '守护' 100000 {T0 - 1000} 9 10000000 1"])
        self.assertNotIn("identity", F.analyze(F.parse(self.stalled(born=None)), self.c)["guard"])   # 旧登记没有出生时钟：认不出

    def test_multi_card_with_one_busy_card_is_not_stalled(self):
        """多卡只有一张卡持续非零：看板登记的与自行开启的保活都不算空转，不重开（#26）。"""
        for raw in (self.stalled(second=(5, 7)), self.manual(second=(5, 7))):
            with self.subTest(manual="ka_groups" in raw):
                a = F.analyze(F.parse(raw), self.c)
                self.assertNotIn("zero_cards", a["guard"] or {})
                self.assertIsNone(a["manual_guard"])
                st, s = {}, self.c["servers"][0]
                F.record(s, raw, None, self.c, st, T0)
                F.record(s, raw, None, self.c, st, T0 + 600)
                self.assertNotIn("guard_zero", st)
                self.assertEqual(F.guard_act(s, st, self.c, T0 + 600, False, self.ssh), (None, []))
        self.assertEqual(self.remote, [])

    def test_gap_after_repair_restarts_on_next_probe(self):
        """重开时停了旧的、新的没开成（gap）：下次探测直接开，不等空闲计时与 30 分钟退避（#26）。"""
        self.raw, self.reply = self.stalled(), "gap 保活没起来"
        self.tick(T0)
        msg, = self.tick(T0 + 600)
        self.assertIn("停掉了空转保活，新的没开起来：保活没起来", msg)
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(self.guard(T0 + 601)["status"], "fail")              # 页面：没开成、约几点再试
        self.raw, self.reply = IDLE, "ok start 9"                              # GPU 上没有保活了
        self.assertEqual(self.tick(T0 + 1200), [])
        self.assertEqual(len(self.remote), 2)
        self.assertTrue(self.remote[1].startswith("bash -s -- start 'bash /x/ka.sh' 300 "))
        ev = records(cat="keepalive")[0]
        self.assertEqual(ev["kind"], "guard_restart")
        self.assertIn("上次停了空转的保活、新的没开起来", ev["msg"])
        st = F._load_state()["b"]
        self.assertFalse({"guard_gap", "guard_fail"} & set(st))

    def test_gap_retry_is_once_then_normal_backoff(self):
        self.raw, self.reply = self.stalled(), "gap 保活没起来"
        self.tick(T0)
        self.tick(T0 + 600)
        self.raw, self.reply = IDLE, "fail 保活没起来"
        self.assertEqual(self.tick(T0 + 1200), [])                             # 快速再试又没成：不再弹
        self.assertEqual(len(self.remote), 2)
        self.assertNotIn("guard_gap", F._load_state()["b"])
        for k in range(3, 8):
            self.tick(T0 + k * 600)
        self.assertEqual(len(self.remote), 2)                                  # 回到平常：空闲满 60 分钟、隔 30 分钟才再试
        self.tick(T0 + 8 * 600)
        self.assertEqual(len(self.remote), 3)

    def test_gap_cleared_when_gpu_gets_used_or_container_changes(self):
        for raw in (KA_JOB, probe_out(used=0, util=(0, 0), started=T0 - 3600)):
            with self.subTest(raw=raw[:60]):
                st, s = {}, self.c["servers"][0]
                self.reply = "gap 保活没起来"
                F.record(s, self.stalled(), None, self.c, st, T0)
                F.record(s, self.stalled(), None, self.c, st, T0 + 600)
                F.guard_act(s, st, self.c, T0 + 600, False, self.ssh)
                self.assertIn("guard_gap", st)
                F.record(s, raw, None, self.c, st, T0 + 1200)
                self.assertNotIn("guard_gap", st)
                self.assertNotEqual(F.guard_state(s, st, self.c, T0 + 1200).get("act"), "start")

    def test_borrowed_clears_stall_evidence(self):
        """借用中（新算力池也走 borrow.begin）不攒空转证据，守护不管（#26 第 4 条）。"""
        st, s = {}, dict(self.c["servers"][0])
        F.record(s, self.stalled(), None, self.c, st, T0)
        self.assertIn("guard_zero", st)
        s.update(borrow_since="2027-01-15 08:00", borrow_by="AA")
        F.record(s, self.stalled(), None, self.c, st, T0 + 600)
        self.assertNotIn("guard_zero", st)
        self.assertEqual(F.guard_state(s, st, self.c, T0 + 600)["status"], "borrowed")

    def test_stalled_recovery_backoff_and_new_attempt(self):
        self.raw, self.reply = self.stalled(), "fail 未看到 GPU 进程"
        self.tick(T0)
        self.assertTrue(self.tick(T0 + 600))
        self.assertEqual(self.guard(T0 + 600)["status"], "fail")
        self.tick(T0 + 1200)
        self.tick(T0 + 1800)
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(self.tick(T0 + 2400), [])
        self.assertEqual(len(self.remote), 2)

    def test_start_delay_records_a_reason_without_repeating(self):
        self.reply = "busy 1"
        for k in range(8):
            self.tick(T0 + k * 600)
        self.assertEqual(len(self.remote), 2)
        rows = records(cat="keepalive")
        self.assertEqual([row["kind"] for row in rows], ["guard_delay"])
        self.assertIn("启动前复查时 GPU 上又有了进程", rows[0]["msg"])
        self.reply = "ok start 9"
        self.tick(T0 + 4800)
        self.assertEqual([row["kind"] for row in records(cat="keepalive")], ["guard_start", "guard_delay"])

    def test_yield(self):
        self.raw = guarded(KA, T0, T0 + 300)
        self.tick(T0)
        self.assertEqual(self.guard(T0)["status"], "ours")
        self.raw = guarded(KA_JOB, T0, T0 + 300)
        self.tick(T0 + 200)                                                                   # 服务器上还在盯：探测不动手
        self.assertEqual(self.remote, [])
        self.c["servers"][0]["guard"] = False                                                 # 关了守护也照样让路
        self._yield(T0 + 800)

    def _yield(self, t):
        self.reply = "ok stopped"
        self.tick(t)
        self.assertEqual(self.remote, ["bash -s -- stop 'bash /x/ka.sh' train.py"])
        ev = records(cat="keepalive")[0]
        self.assertEqual(ev["kind"], "guard_yield")
        self.assertIn("有了 train.py，停掉了守护开的保活", ev["msg"])

    def test_watcher_yield_logged_once(self):
        self.raw = guarded(JOB, T0, T0 + 300, f"yield {T0 + 120}")                             # 服务器上盯着时让出了
        self.tick(T0 + 200)
        self.tick(T0 + 800)
        evs = records(cat="keepalive")
        self.assertEqual(len(evs), 1)
        self.assertIn("有了 train.py，守护开的保活已让出", evs[0]["msg"])
        self.assertEqual((self.remote, self.guard(T0 + 800)["status"]), ([], "busy"))

    def test_script_checks_gpu_before_reporting_success(self):
        d = Path(self.tmp.name)
        proc = d / "proc" / "11"
        proc.mkdir(parents=True)
        (proc / "sched").write_text("python (x) (5000, #threads: 1)\n")
        (proc / "stat").write_text("11 (python) S 1 9 9 0\n")
        collision = d / "proc" / "5000"
        collision.mkdir()
        (collision / "stat").write_text("5000 (python) S 1 5000 5000 0\n")
        for name, body in {
            "timeout": 'shift 3; exec "$@"',
            "nvidia-smi": 'cat "$NV"',
            "ka.sh": 'case "$1" in running) cat "$PID";; start) printf "9\\n" > "$PID";; esac',
        }.items():
            (d / name).write_text("#!/bin/bash\n" + body + "\n")
            (d / name).chmod(0o755)
        env = {**self.shell_env(d), "NV": str(d / "nv"), "PID": str(d / "pid")}

        def start():
            r = subprocess.run(["bash", "-s", "--", "start", f"bash {d / 'ka.sh'}", "0"], input=F.GUARD.read_text(),
                               env=env, capture_output=True, text=True, check=True, timeout=15)
            return r.stdout.strip()

        (d / "pid").write_text("9\n")
        self.assertEqual(start(), "fail 查不到 GPU 进程")
        (d / "nv").write_text("")
        self.assertEqual(start(), "fail 保活进程已存在，尚未看到它占用 GPU")
        self.assertFalse((d / "guard.state").exists())
        (d / "nv").write_text("5000\n")
        self.assertEqual(start(), "ok already 9")
        (d / "nv").write_text("11\n")   # 有些驱动在容器里直接返回容器 PID
        self.assertEqual(start(), "ok already 9")
        self.assertFalse((d / "guard.state").exists())
        (d / "pid").write_text(f"{os.getpid()}\n999\n")
        (d / "guard.state").write_text(f"start {T0} {os.getpid()} {T0} 守护\n")
        r = subprocess.run(["bash", "-s", "--", "stop", f"bash {d / 'ka.sh'}"], input=F.GUARD.read_text(),
                           env=env, capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(r.stdout.strip(), "ok notours")
        (d / "pid").unlink()
        (d / "nv").write_text("")
        self.assertEqual(start(), "fail 保活进程已启动，尚未看到它占用 GPU")
        self.assertIn(" 9 ", (d / "guard.state").read_text())

    def test_kacheck(self):
        """借用前的只读核对（#29）：逐个 GPU 进程看进程组在不在 ka.sh running 列出的里面；一个不在就 noka（原来只看退出码，一个认出就放行）；
        没有在跑的保活不是出错（全部 noka）；ka.sh 跑不起来写明退出码与原因（原来吞掉）；不拿锁、不写文件。"""
        d = Path(self.tmp.name)
        for pid, pgid in ((11, 9), (12, 9), (13, 30)):
            (d / "proc" / str(pid)).mkdir(parents=True)
            (d / "proc" / str(pid) / "stat").write_text(f"{pid} (python (x)) S 1 {pgid} {pgid} 0\n")
        (d / "ka.sh").write_text('echo x >> "$CALLS"\ncase "$1" in running) [ -s "$PID" ] || exit 1; cat "$PID" ;; esac\n')
        env = {**self.shell_env(d), "PID": str(d / "pid"), "CALLS": str(d / "calls")}
        self.err = ""

        def run(ka, *pids):
            r = subprocess.run(["bash", "-s", "--", "kacheck", ka, *map(str, pids)], input=F.GUARD.read_text(), env=env,
                               capture_output=True, text=True, timeout=15)
            self.err = r.stderr
            return r.stdout.strip().splitlines()[-1]
        ka = f"bash {d / 'ka.sh'}"
        (d / "pid").write_text("9\n")
        self.assertEqual(run(ka, 11, 12), "ok")
        self.assertEqual(run(ka, 11, 13), "noka 13")                         # 同台另一个对方自己起的保活：不放过
        self.assertEqual(run(ka, 11, 99), "ok")                              # 已经没了的不算
        (d / "pid").write_text("")
        self.assertEqual(run(ka, 11, 13), "noka 11 13")                      # 没有在跑的保活：都不是 ka.sh 的
        self.assertEqual(run(f"bash {d / 'nope.sh'}", 11), "fail ka.sh running 出错（退出码 127）")
        self.assertIn("nope.sh", self.err)                                    # ka.sh 的报错照传到标准错误，借用的提示带上它
        (d / "ka0.sh").write_text("exit 0\n")
        self.assertEqual(run(f"bash {d / 'ka0.sh'}", 11), "fail 保活在跑，但 ka.sh 没列出进程组，无法确认归属")  # 在跑却没列出：认不了，不当没在跑（#26 与 #29 合并后同一读法）
        (d / "pid").write_text("9\noops\n")
        (d / "calls").write_text("")
        self.assertEqual(run(ka, 11), "fail ka.sh running 的输出认不出")      # 输出认不出也不当成核对过
        self.assertIn("oops", self.err)
        self.assertEqual((d / "calls").read_text(), "x\n")                   # 只读一次（经 running_groups），不另跑一遍取原因（PR #34 审查）
        self.assertFalse((d / "guard.state.lock").exists())                  # 只读：不拿锁、不建锁文件

    def test_script(self):
        """fleet_guard.sh 本身：用假的 nvidia-smi（给宿主机 PID）、/proc（sched 对回容器 PID、stat 带进程组）、ka.sh、setsid 跑。"""
        d = Path(self.tmp.name)
        (d / "bin").mkdir()
        for name, body in {"nvidia-smi": 'cat "$NV"', "setsid": 'exec "$@"', "timeout": 'shift 3; exec "$@"'}.items():
            (d / "bin" / name).write_text("#!/bin/bash\n" + body + "\n")
            (d / "bin" / name).chmod(0o755)
        (d / "ka.sh").write_text('P=$KA_PIDF\nup() { [ -s "$P" ] && kill -0 "$(cat "$P")" 2>/dev/null; }\n'
                                 'case $1 in\n running) up && cat "$P" ;;\n start) up || { sleep 60 & echo $! > "$P"; }; printf "11 (python) S 1 %s 0 0\\n" "$(cat "$P")" > "$GUARD_PROC/11/stat"; echo 5000 > "$NV" ;;\n'
                                 ' stop) up && kill "$(cat "$P")"; rm -f "$P" ;;\nesac\n')
        env = {**self.shell_env(d), "NV": str(d / "nv"), "KA_PIDF": str(d / "ka.pid"), "GUARD_POLL": "0.2"}
        ka = f"bash {d / 'ka.sh'}"

        def gpu(*hp):                                    # GPU 上的进程（宿主机 PID，同一进程占两张卡就出现两次）
            (d / "nv").write_text("".join(f"{h}\n" for h in hp))

        def proc(pid, hpid, pgid):                       # 容器里的进程：sched 第一行带宿主机 PID，stat 里第 5 项是进程组
            (d / "proc" / str(pid)).mkdir(parents=True, exist_ok=True)
            (d / "proc" / str(pid) / "sched").write_text(f"python (x) ({hpid}, #threads: 1)\n")
            (d / "proc" / str(pid) / "stat").write_text(f"{pid} (python (x)) S 1 {pgid} {pgid} 0\n")

        def run(*args):
            r = subprocess.run(["bash", "-s", "--", *args], input=F.GUARD.read_text(), env=env, capture_output=True, text=True, timeout=30)
            return r.stdout.strip().splitlines()[-1]
        try:
            gpu(9000)
            self.assertEqual(run("start", ka, "2", "守护"), "busy 1")                          # GPU 上有进程（容器里对不上的也算）：不开
            gpu()
            proc(11, 5000, 0)
            out = run("start", ka, "2", "守护")
            pid = (d / "ka.pid").read_text().strip()
            self.assertEqual(out, f"ok start {pid}")
            self.assertTrue((d / "guard.state").read_text().startswith("start "))
            self.assertIn(f" {pid} ", (d / "guard.state").read_text())
            self.assertEqual(run("start", ka, "2", "守护"), f"ok already {pid}")               # 已在跑：不重开
            proc(12, 5001, pid)
            gpu(5000, 5001, 5001)                                                            # 保活自己占两个进程、一个跨两张卡：不让
            threading.Event().wait(0.8)
            self.assertNotIn("yield", (d / "guard.state").read_text())
            proc(13, 5002, 13)
            gpu(5000, 5001, 5002)                                                            # 有实验上来：盯着的那段时间里让出
            for _ in range(50):
                if "yield" in (d / "guard.state").read_text():
                    break
                threading.Event().wait(0.1)
            self.assertIn("\nyield ", (d / "guard.state").read_text())
            self.assertFalse((d / "ka.pid").exists())
            self.assertEqual(run("stop", ka, "x"), "ok gone")
            gpu()
            run("start", ka, "0", "看板")                                                       # 盯 0 秒：不留后台
            self.assertEqual(run("stop", ka, "train.py"), "ok stopped")
            self.assertIn("\nstop ", (d / "guard.state").read_text())
            gpu()
            run("start", ka, "3", "守护")
            marker = (d / "guard.state").read_text().split()
            marker[1] = str(int(marker[1]) + 1)                         # PID 不变，旧看门狗看到的是另一份登记
            (d / "guard.state").write_text(" ".join(marker) + "\n")
            gpu(5000, 5002)
            threading.Event().wait(0.7)
            self.assertTrue((d / "ka.pid").exists())
            self.assertNotIn("yield", (d / "guard.state").read_text())
            self.assertEqual(run("stop", ka, "测试结束"), "ok stopped")
            subprocess.run(["bash", str(d / "ka.sh"), "start"], env=env)                         # 别人开的保活：不停
            (d / "guard.state").write_text(f"start {T0} {os.getpid()} {T0} 守护\n")
            self.assertEqual(run("stop", ka, "x"), "ok notours")
        finally:
            subprocess.run(["bash", str(d / "ka.sh"), "stop"], env=env)

    def test_repair_script_checks_live_identity_all_cards_and_processes(self):
        d = Path(self.tmp.name)
        pid = os.getpid()  # 只用于脚本里的 kill -0；所有真实开停都由假 ka.sh 写入文件
        for p, birth in ((1, 2000), (pid, 2345), (11, 2400)):
            proc = d / "proc" / str(p)
            proc.mkdir(parents=True, exist_ok=True)
            (proc / "stat").write_text(" ".join([f"{p} (python (x)) S 1 {pid}", *["0"] * 16, str(birth)]) + "\n")
        (d / "proc" / "11" / "sched").write_text("python (x) (5011, #threads: 1)\n")
        for cmd, body in {
            "timeout": 'shift 3; exec "$@"',
            "nvidia-smi": 'case "$*" in *query-gpu*) cat "$NV_UTIL";; *) cat "$NV_APPS";; esac',
            # 没在跑时 running 退出码非 0（同真的 ka.sh）；NOSTART 在时 start 什么也没开起来
            "ka.sh": ('case "$1" in running) [ -s "$PID" ] && cat "$PID";; stop) echo stop >> "$ACTIONS"; : > "$PID"; : > "$NV_APPS";; '
                      'start) echo start >> "$ACTIONS"; [ -e "$NOSTART" ] || { echo "$PID_VALUE" > "$PID"; echo 5011 > "$NV_APPS"; };; esac'),
        }.items():
            (d / cmd).write_text("#!/bin/bash\n" + body + "\n")
            (d / cmd).chmod(0o755)
        env = {**self.shell_env(d), "PID_VALUE": str(pid), **{k: str(d / k) for k in ("PID", "ACTIONS", "NV_UTIL", "NV_APPS", "NOSTART")}}

        def run(p1=2000, marked=T0, birth=2345):
            args = ["repair", f"bash {d / 'ka.sh'}", "0", "守护", str(p1), str(marked), str(pid), str(birth), "2"]
            return subprocess.run(["bash", "-s", "--", *args], input=F.GUARD.read_text(), env=env,
                                  capture_output=True, text=True, check=True, timeout=20).stdout.strip()

        (d / "guard.state").write_text(f"start {T0} {pid} {T0} 看板\n")
        (d / "PID").write_text(f"{pid}\n")
        (d / "NV_APPS").write_text("5011\n")
        for invalid in ("0,0\n0,0\n", "0,0\n1,0\n0,0\n1,N/A\n", "0,0\n1,0\n0,0\n1,30\n"):
            (d / "NV_UTIL").write_text(invalid)
            self.assertTrue(run().startswith("busy "))
        (d / "NV_UTIL").write_text("0,0\n1,0\n0,0\n1,0\n")
        self.assertIn("容器已变", run(p1=2001))
        self.assertIn("容器已变", run(marked=T0 + 1))
        self.assertIn("容器已变", run(birth=2346))
        (d / "NV_APPS").write_text("5011\n99999\n")
        self.assertIn("别的进程", run())
        self.assertFalse((d / "ACTIONS").exists())
        (d / "NV_APPS").write_text("5011\n")
        # 模拟另一项保活操作占锁；重开不能越过它。锁释放后一次完整的停、开。
        args = ["repair", f"bash {d / 'ka.sh'}", "0", "守护", "2000", str(T0), str(pid), "2345", "2"]
        with (d / "guard.state.lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with subprocess.Popen(["bash", "-s", "--", *args], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as racing:
                racing.stdin.write(F.GUARD.read_text())
                racing.stdin.close()
                threading.Event().wait(0.2)
                self.assertIsNone(racing.poll())
                self.assertFalse((d / "ACTIONS").exists())
                fcntl.flock(held, fcntl.LOCK_UN)
                self.assertEqual(racing.wait(timeout=10), 0)
                self.assertEqual(racing.stdout.read().strip(), f"ok start {pid}")
        self.assertEqual((d / "ACTIONS").read_text(), "stop\nstart\n")

        # 自行启动时不靠看板标记。配置命令若返回另一组、多组或格式不明，不能调用 stop。
        self.assertIn("看板已登记", run(marked="manual"))
        (d / "guard.state").unlink()
        (d / "ACTIONS").unlink()
        for ids in (str(pid + 1), f"{pid}\n{pid + 1}", f"{pid}\nunknown"):
            (d / "PID").write_text(ids + "\n")
            response = run(marked="manual")
            self.assertTrue(response.startswith("unmanaged "), (ids, response))
        (d / "PID").write_text(f"{pid}\n")
        (d / "NV_APPS").write_text("")
        self.assertIn("已不占用 GPU", run(marked="manual"))
        (d / "NV_APPS").write_text("5011\n")
        self.assertIn("容器已变", run(marked="manual", birth=2346))
        self.assertFalse((d / "ACTIONS").exists())
        self.assertEqual(run(marked="manual"), f"ok start {pid}")
        self.assertEqual((d / "ACTIONS").read_text(), "stop\nstart\n")
        marker = (d / "guard.state").read_text().split()
        self.assertEqual((marker[0], marker[2], marker[4:]), ("start", str(pid), ["守护", "2345"]))   # 登记里记下出生时钟（#26）

        # 停了旧的、新的没开成：回 gap，与“没停成”的 fail 分开（#26）
        (d / "NOSTART").touch()
        self.assertEqual(run(marked=marker[1]), "gap 保活没起来")
        self.assertEqual((d / "ACTIONS").read_text(), "stop\nstart\n" * 2)
        self.assertIn("\nstop ", (d / "guard.state").read_text())

        # ka.sh running 退出码 0 却没列出进程组：在跑、认不了归属，不当作没在跑去再开一个
        (d / "PID").write_text("unknown\n")
        out = subprocess.run(["bash", "-s", "--", "start", f"bash {d / 'ka.sh'}", "0"], input=F.GUARD.read_text(), env=env,
                             capture_output=True, text=True, check=True, timeout=20).stdout.strip()
        self.assertEqual(out, "fail 保活在跑，但 ka.sh 没列出进程组，无法确认归属")
        self.assertEqual((d / "ACTIONS").read_text(), "stop\nstart\n" * 2)


    def test_set_guard(self):
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text('[fleet]\ninterval = 600\nprivate_pools = ["P"]\n\n[[fleet.servers]]\nid = "a"\nholder = "AA"\nssh = "root@h"\nport = 1\npool = "P"\n\n'
                      '[[fleet.servers]]\nid = "b"\nholder = "BB"\nssh = "root@h"\nport = 2\npool = "Q"\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        try:
            on = lambda c: {x["id"]: F.guard_on(x, c) for x in c["servers"]}
            self.assertEqual(on(F.conf()), {"a": False, "b": True})
            F.set_guard("a", True)
            F.set_guard(idle_min=90, watch_min=3)
            self.assertIn('id = "a"\nguard = true\nholder = "AA"', hf.read_text())
            c = F.conf()
            self.assertEqual((on(c)["a"], F.guard_conf(c)), (True, {"idle_min": 90, "watch_min": 3}))
            with self.assertRaises(ConfigError):
                F.set_guard(idle_min=5)                                                          # 比探测间隔还短
            self.assertEqual([(e["msg"], e.get("host")) for e in records(cat="config")], [("改保活守护的时间", None), ("打开 AA（a）的保活守护", "a")])
            self.assertEqual(records(host="a", q="guard = true")[0]["msg"], "打开 AA（a）的保活守护")           # 按服务器筛得到；细节里的字也搜得到
        finally:
            os.environ["LABMON_HOSTS"] = NO_HOSTS


class OwnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        os.environ["LABMON_STATE"] = str(self.d / "state")

    def tearDown(self):
        os.environ["LABMON_HOSTS"] = NO_HOSTS
        self.tmp.cleanup()

    def test_whose(self):
        from labmon import disk as DK
        hosts = [{"id": "m", "shared": ["/root/data1/"]}]
        who = DK.whose(cfg(), hosts)
        self.assertEqual(who(["/root/data1/bb/exp/code", "/root/data1/aa/venv/bin/python x.py"]), "BB")   # 名字不分大小写，工作目录在先
        self.assertEqual(who([None, "/root/data1/tmp/x", "/root/data1/aa/.venv/bin/python -m p"]), "AA")  # 对不上人的路径跳过
        self.assertIsNone(who(["/root/demo_eval/repo", "python run.py"]))
        DK.set_owner("aa", "公共")
        DK.set_owner("zz", "BB")
        who = DK.whose(cfg(), hosts)
        self.assertEqual(who(["/root/data1/aa/x /root/data1/zz/y"]), "BB")     # 与「共享盘」同一张归属表
        self.assertIsNone(DK.whose(cfg(), [{"id": "m", "shared": []}])(["/root/data1/bb/x"]))

    def test_mark_keepalive(self):
        hf = self.d / "hosts.toml"
        hf.write_text('[fleet]\ninterval = 600\nkeepalive = ["keep_alive"]   # 只看自己的命令行\n\n'
                      '[[fleet.servers]]\nid = "a"\nholder = "AA"\nssh = "root@h"\nport = 1\npool = "P"\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        raw = probe_out(apps=[(500, 6748)], maps=[(9, 500, "/root/x")],
                        ps=[(1, 0, "/bin/bash"), (9, 1, "/opt/py/bin/python /root/x/occupy_gpu.py --mb 6000", 100.0, 1024)])
        a = F.analyze(F.parse(raw), F.conf())
        self.assertEqual(a["state"], "job")
        F._save_state({"a": {"sample": a}})
        self.assertEqual(F.mark_keepalive("a", 9), "/root/x/occupy_gpu.py")
        self.assertIn('keepalive = ["keep_alive", "/root/x/occupy_gpu.py"]   # 只看自己的命令行\n', hf.read_text())
        self.assertEqual(F.analyze(F.parse(raw), F.conf())["state"], "keepalive")
        F.mark_keepalive("a", 9)                                         # 再点一次不重复加
        self.assertEqual(F.conf()["keepalive"], ["keep_alive", "/root/x/occupy_gpu.py"])
        cf = records(cat="config")                                     # 记录里写明为什么改、改了哪行，只记一次
        self.assertEqual(len(cf), 1)
        self.assertEqual(cf[0]["msg"], "把 AA a 上的 /root/x/occupy_gpu.py 认作保活")
        self.assertTrue(cf[0]["detail"].startswith('[fleet] keepalive = ["keep_alive"]'), cf)
        rel = probe_out(apps=[(501, 6748)], maps=[(19, 501, "/root/z")],                  # 相对路径起的：按工作目录写成完整路径（#29）
                        ps=[(1, 0, "/bin/bash"), (19, 1, "/opt/py/bin/python ./hold_gpu.py", 100.0, 1024)])
        F._save_state({"a": {"sample": F.analyze(F.parse(rel), F.conf())}})
        self.assertEqual(F.mark_keepalive("a", 19), "/root/z/hold_gpu.py")
        g = F.analyze(F.parse(GUARD), F.conf())                          # GPU 进程是 python -c：认不出脚本
        F._save_state({"a": {"sample": g}})
        with self.assertRaises(ConfigError):
            F.mark_keepalive("a", g["work"][0]["pid"])
        with self.assertRaises(ConfigError):
            F.mark_keepalive("a", 12345)


class RecordsTest(unittest.TestCase):
    """记录：一个文件、按时间修剪；worker 的 hostlog 一起读；按服务器、人、分类、正文筛；主机清单改了哪些行。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_filters_and_keep(self):
        log_events([{"kind": "fleet_down", "host": "a", "who": "AA", "msg": "AA（a）掉线"}], T0)
        log_events([{"kind": "op_fail", "hosts": ["a", "b"], "msg": "续租停在「登录」：验证码不对", "who": None}], T0 + 10)
        log_events([{"kind": "config", "msg": "人员：改了 BB 的设置：镜像 x → y"}], T0 + 20)
        (state_dir() / "hostlog").mkdir(parents=True)
        (state_dir() / "hostlog" / "h1.jsonl").write_text(json.dumps({"t": T0 + 5, "kind": "wait", "msg": "等显存"}) + "\n")
        self.assertEqual([e["kind"] for e in records()], ["config", "op_fail", "wait", "fleet_down"])
        self.assertNotIn("who", records(cat="op")[0])                      # 空的字段不写
        self.assertEqual([e["kind"] for e in records(host="b")], ["op_fail"])
        self.assertEqual(records(host="h1")[0]["host"], "h1")               # worker 的事件补上主机
        self.assertEqual([e["kind"] for e in records(who="BB")], ["config"])  # 老记录没有 who：按正文里的名字
        self.assertEqual([e["kind"] for e in records(who="AA")], ["fleet_down"])
        self.assertEqual([e["kind"] for e in records(cat="queue")], ["wait"])
        self.assertEqual([e["kind"] for e in records(q="验证码")], ["op_fail"])
        self.assertEqual([e["kind"] for e in records(q="续租 验证码")], ["op_fail"])                  # 几个词都要有
        self.assertEqual(records(q="续租 掉线"), [])
        self.assertEqual([e["kind"] for e in records(before=T0 + 10, limit=1)], ["wait"])
        log_events([{"kind": "lease", "msg": "租期"}], T0 + EVENT_KEEP_S + 2 * 86400)      # 一年以前的整理掉
        self.assertEqual([e["kind"] for e in records()], ["lease", "wait"])

    def test_hosts_diff(self):
        from labmon.config import _diff_note
        old = '[fleet]\nkeepalive = ["keep_alive"]\n\n[[fleet.servers]]\nid = "a"\nholder = "AA"\n# 注释\n'
        new = '[fleet]\nkeepalive = ["keep_alive", "occupy.py"]\n\n[[fleet.servers]]\nid = "a"\nholder = "DD"\n# 注释改了\n'
        self.assertEqual(_diff_note(old, new), ('[fleet] keepalive = ["keep_alive"] → keepalive = ["keep_alive", "occupy.py"]；a holder = "AA" → holder = "DD"', ["a"]))
        self.assertEqual(_diff_note(old, old), ("", []))

    def test_trouble(self):
        from labmon.serve import trouble
        for err in ("Traceback\nValueError: 坏了", "Traceback\nValueError: 又坏了", None, None):
            trouble("工作台", err)
        self.assertEqual([e["msg"] for e in records()][::-1], ["工作台采集出错：ValueError: 坏了", "工作台采集恢复"])


class ConfTest(unittest.TestCase):
    def conf(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        try:
            return F.conf(Path(f.name))
        finally:
            os.unlink(f.name)

    def test_conf(self):
        self.assertIsNone(self.conf("[[hosts]]\nid = \"x\"\n"))
        c = self.conf('[fleet]\npublic_max = 2\nshare = {util = 20}\n[[fleet.servers]]\nid = "s"\nholder = "H"\nssh = "root@h"\nport = 1\npool = "P"\n'
                      '[[fleet.rules]]\nmatch = "/a/"\nkind = "job"\n')
        self.assertEqual((c["public_max"], c["share"]["util"], c["share"]["vram"], c["servers"][0]["label"]), (2, 20, 0.3, "H"))
        with self.assertRaises(ConfigError):
            self.conf('[fleet]\n[[fleet.servers]]\nid = "s"\nholder = "H"\n')
        with self.assertRaises(ConfigError):
            self.conf('[fleet]\n[[fleet.rules]]\nmatch = "("\nkind = "job"\n')
        with self.assertRaises(ConfigError):
            self.conf('[fleet]\n[[fleet.rules]]\nmatch = "a"\nkind = "other"\n')


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        os.environ["LABMON_STATE"] = str(d / "state")
        (d / "hosts.toml").write_text('[fleet]\n[[fleet.servers]]\nid = "s1"\nholder = "H"\nssh = "root@h"\nport = 1\npool = "P"\n')
        os.environ["LABMON_HOSTS"] = str(d / "hosts.toml")
        self.fetch, F.fetch = F.fetch, lambda s, timeout=60: (KA, None)
        from labmon.serve import Server
        self.srv = Server(("127.0.0.1", 0), [])
        login(self)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()   # shutdown 不用等缺省 0.5 秒的轮询
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        F.fetch = self.fetch
        os.environ["LABMON_HOSTS"] = NO_HOSTS
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def test_borrow_past_until(self):
        """借来用：已经过去的约定在 HTTP 层就回 409（后台借用开始之前），不开平台操作（PR #10 审查）。"""
        old, os.environ["LABMON_LAN"] = os.environ.get("LABMON_LAN"), "on"
        try:
            body = json.dumps({"id": "s1", "until": time.time() - 60, "ok": True}).encode()
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(post_req(self.base + "/fleet/op/borrow", body), timeout=10)
            self.assertEqual(e.exception.code, 409)
            self.assertIn("约定时间", json.loads(e.exception.read())["error"])
            self.assertFalse((FO.view().get("op") or {}).get("active"))
        finally:
            os.environ.pop("LABMON_LAN", None) if old is None else os.environ.update(LABMON_LAN=old)

    def test_probe_and_series(self):
        r = urllib.request.urlopen(post_req(self.base + "/fleet/probe/s1"), timeout=30)
        v = json.loads(r.read())
        self.assertEqual(v["servers"][0]["state"], "keepalive")
        r = json.loads(urllib.request.urlopen(self.base + "/fleet/s1.json", timeout=10).read())
        self.assertEqual(len(r["series"]), 1)
        for url, kw in ((self.base + "/fleet/nope.json", {}), (self.base + "/fleet/probe/nope", None)):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(post_req(url) if kw is None else url, timeout=10)
            self.assertEqual(e.exception.code, 404)
        from labmon import fleetops as FO
        FO.logout()
        v = json.loads(urllib.request.urlopen(self.base + "/fleet/op.json", timeout=10).read())
        self.assertEqual((v["boot"], v["session"]["logged_in"]), ({}, False))
        with self.assertRaises(urllib.error.HTTPError) as e:              # 没登录也没给验证码：不动平台
            urllib.request.urlopen(post_req(self.base + "/fleet/op/sync", b"{}"), timeout=10)
        self.assertEqual(e.exception.code, 409)
        snap = O.collect(use_ssh=False)                      # 全局页的快照带上组内服务器（不连 ssh）
        self.assertEqual(snap["fleet"]["servers"][0]["holder"], "H")
        self.assertIn("srvDrawer", (state_dir() / "index.html").read_text())
        # 记录：按分类、服务器筛，limit 之外还有就 more；favicon 回空的
        log_events([{"kind": "fleet_down", "host": "s1", "who": "H", "msg": "s1 掉线"}], T0)
        log_events([{"kind": "config", "msg": "改了主机清单"}, {"kind": "op_start", "hosts": ["s1", "s2"], "msg": "开始续租"}], T0 + 60)
        get = lambda q: json.loads(urllib.request.urlopen(self.base + "/records.json?" + q, timeout=10).read())
        self.assertEqual([e["msg"] for e in get("host=s1")["rows"]], ["开始续租", "s1 掉线"])
        self.assertEqual([e["msg"] for e in get("cat=link")["rows"]], ["s1 掉线"])
        r = get("limit=1")
        self.assertEqual((len(r["rows"]), r["more"]), (1, True))
        self.assertEqual(get(f"before={T0 + 1}")["rows"][0]["msg"], "s1 掉线")
        self.assertEqual(get("cat=link&host=s1&cats=1")["cats"], {"link": 1, "op": 1})     # 选项卡上的条数：同样的筛选、不分类
        self.assertEqual([e["msg"] for e in get("who=H")["rows"]], ["s1 掉线"])
        self.assertEqual([e["msg"] for e in get("q=" + quote("续租 开始"))["rows"]], ["开始续租"])      # 几个词都要有
        self.assertEqual(get("q=" + quote("续租 掉线"))["rows"], [])
        r = get(f"before={T0 + 60}&limit=1")                                                     # 翻页：before 取更早的，再没有就不 more
        self.assertEqual(([e["msg"] for e in r["rows"]], r["more"]), (["s1 掉线"], False))
        self.assertEqual(len(get("limit=5000")["rows"]), 3)                                         # 上限 1000，不报错
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.base + "/records.json?limit=abc", timeout=10)
        self.assertEqual(e.exception.code, 400)
        self.assertEqual(urllib.request.urlopen(self.base + "/favicon.ico", timeout=10).status, 204)


if __name__ == "__main__":
    unittest.main()
