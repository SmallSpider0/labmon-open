"""实验会话的只读命令（#52）：labmon status、disk、records 经看板接口（GET /api/project/<id>/…），看板排版、命令行原样打出，--json 打原始数据；
普通用户只看自己的项目，没登记的退出 3；备用看板让命令行接着找主那台。skill 里不再叫会话看页面或自己 curl。

    python -m unittest tests.test_reads -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import NO_HOSTS, board, make_token, register  # noqa: E402

from labmon import __main__ as M  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import serve as SV  # noqa: E402
from labmon.config import GIB  # noqa: E402
from labmon.records import log_events  # noqa: E402
from labmon.state import load_json, save_json, state_dir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def run(argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = M.main(argv)
    return rc, out.getvalue(), err.getvalue()


class ReadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state"), "LABMON_HOSTS": NO_HOSTS, "LABMON_LAN": "on",
                                                "LABMON_NO_NOTIFY": "1", "LABMON_NO_KEYCHAIN": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        (self.d / "proj").mkdir()
        self.cfg = self.d / "proj" / "monitor.toml"
        self.cfg.write_text('id = "p1"\ntitle = "示例"\n')
        register(self.cfg)
        self.now = time.time()
        self.snap()

    def snap(self, **kw):
        now = self.now
        s = {"id": "p1", "title": "示例", "generated": now - 120, "all_done_ts": now + 7200, "thresholds": {"stale_s": 420},
             "hosts": [{"id": "h1", "label": "h1（A6000）", "ok": True, "gpus": [{"index": 0, "util": 97, "mem_used": 30720, "mem_total": 46068}],
                        "mounts": [{"path": "/tmp", "label": "容器 /tmp", "avail_gb": 24.9}]},
                       {"id": "h2", "label": "h2", "ok": False, "error": "连不上", "last_ok": now - 900},
                       {"id": "h3", "label": "h3", "returned": now - 86400},
                       {"id": "pool", "label": "算力池", "pool": True, "ok": True, "error": None}],
             "lanes": [{"id": "L1", "label": "车道 1", "host": "h1", "queue": "jobs/a.txt", "alive": True, "managed": True, "total": 5, "done": 2,
                        "failed": 1, "unfinished": 2, "eta_ts": now + 7200, "logs_dir": "/data/p1/_logs"},
                       {"id": "L2", "label": "车道 1", "host": "h2", "queue": "jobs/b.txt", "managed": True, "total": 3, "done": 0, "unfinished": 3,
                        "stuck": "放不下：要 60 GiB 显存"}],
             "jobs": [{"id": "train_s1", "lane": "L1", "status": "running", "progress": [50, 200], "elapsed_min": 30, "eta_ts": now + 3600,
                       "eta_source": "近期速度", "last_line": "PROGRESS 50/200 loss=0.3", "log_mtime": now - 60},
                      {"id": "train_s2", "lane": "L1", "status": "queued", "plan_start_ts": now + 3600, "eta_ts": now + 7200},
                      {"id": "big", "lane": "L2", "status": "queued", "plan_start_ts": None},
                      {"id": "train_s0", "lane": "L1", "status": "failed", "end_ts": now - 600, "killed": 137, "last_line": "Killed"}],
             "groups": [{"id": "E1", "label": "E1 训练", "status": "运行中", "total": 5, "done": 2, "failed": 1, "running": 1, "queued": 1,
                         "eta_ts": now + 7200, "ratios": [{"cls": "train", "k": 0.8}]}],
             "rebalance": {"key": "rebalance:p1", "text": "可以把 h1 上 jobs/a.txt 的 train_s2 挪到 h4"},
             "alerts": [{"key": "failed:train_s0", "to": "agent", "msg": "train_s0 失败"}, {"key": "ssh_fail:h2", "to": "user", "msg": "h2 连不上"}]}
        s.update(kw)
        save_json(PJ.out_dir("p1") / "snapshot.json", s)

    def glob(self, **kw):
        g = {"generated": self.now, "hosts": [{"id": "h1", "worker_up": True, "status": {
            "running": [{"project": "p1", "name": "train_s1", "use": {"cores": 3.25}}, {"project": "other", "name": "train_s1", "use": {"cores": 9}}],
            "info": {"gpus": [{"i": 0, "uuid": "GPU-a"}]}, "keepalive": {"ctl": True, "why": None},
            "gpu_procs": [{"hpid": 5300, "pid": 300, "mb": 6886, "uuid": "GPU-a", "owner": "keepalive", "ka": False, "t0": self.now - 600}]}},
            {"id": "h2", "worker_up": False, "status": {"keepalive": {"ctl": True, "why": None}}}]}
        g.update(kw)
        save_json(state_dir() / "snapshot.json", g)

    def test_status_prints_project_overview(self):
        board(self)
        self.glob()
        rc, out, err = run(["status", "-c", str(self.cfg)])
        self.assertEqual(rc, 0, err)
        for s in ("示例（p1）快照", "（2 分钟前）", "预计全部跑完", "h1（A6000）：正常；卡 0 97% 30.0/45 GiB", "h2：连不上：连不上", "h3：已归还", "算力池：交给池、还没放到哪台的清单在这里等",
                  "车道 1（h1，jobs/a.txt）：2/5 跑完，失败 1；预计", "车道 1（h2，jobs/b.txt）：0/3 跑完", "推演排不上：放不下：要 60 GiB 显存",
                  "train_s1（车道 1（h1））：进度 50/200，已跑 30 分钟，预计", "（近期速度）", "CPU 3.2 核", "日志 1 分钟前：PROGRESS 50/200 loss=0.3",
                  "排队（2 个）", "train_s2（车道 1（h1））：预计", "big（车道 1（h2））：推演不出何时开始",
                  "train_s0（车道 1（h1））", "退出码 137", "日志 /data/p1/_logs/train_s0.log",
                  "E1 训练：运行中，2/5 跑完，1 在跑，1 排队，失败 1", "train 实际/估时 ×0.80",
                  "挪法建议：可以把 h1 上 jobs/a.txt 的 train_s2 挪到 h4", "告警（2 条）", "[failed:train_s0] train_s0 失败", "[ssh_fail:h2] h2 连不上（要用户处理）"):
            self.assertIn(s, out)
        self.assertNotIn("看板没在采集", out)
        # 这台的保活（#61）：nvidia-smi 里的 PID 对得上；连不上、worker 不在的主机不报；#197 同时附他项聚合数
        self.assertIn("h1（A6000）：正常；卡 0 97% 30.0/45 GiB；他项 1 跑\n    GPU 保活：在跑（", out)
        self.assertIn("PID 5300（容器内 300），卡 0 占 6.7 GiB；有任务要卡时 worker 自动停", out)
        self.assertEqual(out.count("GPU 保活"), 1)
        # 这台的 CPU 配额与已记的核（#112，同 worker status 的 cpu_line）；worker 不在的主机不报
        self.assertIn("6.7 GiB；有任务要卡时 worker 自动停\n    CPU：容器没设配额，不按核记账\n", out)
        self.assertEqual(out.count("CPU："), 1)
        g = load_json(state_dir() / "snapshot.json", {})
        st = g["hosts"][0]["status"]
        st["info"]["cpu_quota"], st["info"]["nproc"], st["room"] = 7.0, 64, {"cpu": 2.0, "cpu_cap": 7}
        st["running"][0]["acct"], st["running"][1]["acct"] = {"cpu": 3.0}, {"cpu": 2.0}
        save_json(state_dir() / "snapshot.json", g)
        rc, out, _ = run(["status", "p1"])
        self.assertIn("    CPU：配额 7 核（为 GPU 保底留 1 核，纯 CPU 上限 6 核），nproc 报的 64 是宿主机的核数；labmon 在跑 2 个任务记 5 核，还能给新任务 2 核\n", out)
        rc, out, _ = run(["status", "p1", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["rebalance"]["key"], "rebalance:p1")

    def test_status_stale_and_missing_snapshot(self):
        board(self)
        self.snap(generated=self.now - 3600, rebalance={"why": ["h4 放不下"]})
        rc, out, _ = run(["status", "p1"])
        self.assertIn("看板没在采集，告诉用户", out)
        self.assertIn("别的主机接不了本项目的任务：h4 放不下", out)
        self.snap(rebalance={"key": "rebalance:p1:est", "est": True, "text": "有主机有空，但推演算不出", "why": ["h4 放不下"]})
        rc, out, _ = run(["status", "p1"])          # 时长不明（#43）：不当成挪法建议，挪不了的原因照列
        self.assertIn("可能可以提前：有主机有空，但推演算不出", out)
        self.assertNotIn("挪法建议：", out)
        self.assertIn("别的主机接不了本项目的任务：h4 放不下", out)
        (PJ.out_dir("p1") / "snapshot.json").unlink()
        rc, out, _ = run(["status", "p1"])
        self.assertEqual(rc, 0)
        self.assertIn("第一次采集还没完成", out)
        rc, out, _ = run(["status", "p1", "--json"])
        self.assertEqual(out.strip(), "null")

    def test_status_unknown_gpu_fields(self):
        # nvidia-smi 回 N/A 时探针记 None（probe.num）：照样出整份，未知的写“?”
        board(self)
        hosts = [{"id": "h1", "label": "h1", "ok": True, "gpus": [{"index": 0, "util": None, "mem_used": None, "mem_total": None},
                                                                  {"index": 1, "util": 5, "mem_used": 1024, "mem_total": None}]}]
        self.snap(hosts=hosts)
        rc, out, err = run(["status", "p1"])
        self.assertEqual(rc, 0, err)
        self.assertIn("卡 0 ?% ?/? GiB", out)
        self.assertIn("卡 1 5% 1.0/? GiB", out)
        self.assertIn("告警（2 条）", out)

    def test_unregistered_and_not_owner(self):
        board(self)
        rc, _, err = run(["status", "nope"])
        self.assertEqual(rc, 3)
        self.assertIn("看板上没有项目 nope", err)
        rc, _, err = run(["status", "-c", str(self.d / "missing.toml")])
        self.assertEqual(rc, 3)
        self.assertIn("配置错误", err)
        # 普通用户：别人的项目 403（退出 3），自己的能看
        os.environ["LABMON_TOKEN"] = make_token("amy", role="user", scopes=("project",))
        for cmd in ("status", "disk", "records"):
            rc, _, err = run([cmd, "p1"])
            self.assertEqual(rc, 3, cmd)
            self.assertIn("不是你的", err)
        PJ.set_owner("p1", "amy", "测试")
        for cmd in ("status", "disk", "records"):
            self.assertEqual(run([cmd, "p1"])[0], 0, cmd)

    def test_cli_reads_go_to_primary(self):
        # 备用上的快照是旧的：命令行读的这几个也回 409 standby，客户端接着试下一个地址（serve.CLI_READS）
        for what in ("status", "disk", "records"):
            self.assertTrue(SV.CLI_READS.fullmatch(f"/api/project/p1/{what}"))
            r = SV.route_of("GET", f"/api/project/p1/{what}")
            self.assertEqual((r.scope, r.own, r.admin), ("read", "path", False))

    def test_disk(self):
        board(self)
        root = "/data/alice/projects/p1"
        n = self.now
        jobs = [{"name": "a1", "host": "h1", "t0": n - 7200, "t1": n - 3600, "st": "done", "b": 33 * GIB, "top": [["cache", 30 * GIB]],
                 "from": n - 7300, "to": n - 3500, "with": 1, "pending": 0, "wait": False, "big": True, "grp": f"a1:{n - 3600}"},
                {"name": "a2", "host": "h1", "t0": n - 7000, "t1": n - 3400, "st": "done", "b": 33 * GIB, "top": [["cache", 30 * GIB]],
                 "from": n - 7300, "to": n - 3500, "with": 1, "pending": 0, "wait": False, "big": True, "grp": f"a1:{n - 3600}"},
                {"name": "b1", "host": "h1", "t0": n - 900, "t1": n - 300, "st": "failed", "b": -2 * GIB, "top": [],
                 "from": None, "to": None, "with": 0, "pending": 1, "wait": True, "big": False, "grp": f"b1:{n - 300}"}]
        st = {"roots": [root], "b": 120 * GIB, "d24": 33 * GIB, "d7": 39 * GIB, "pending": 2, "burst": True,
              "dirs": [{"path": f"{root}/out", "name": "out", "b": 40 * GIB, "d24": 0, "d7": -GIB, "n": 12, "m": n, "t": n - 600, "top": []},
                       {"path": f"{root}/cache", "name": "cache", "b": 80 * GIB, "d24": 33 * GIB, "d7": 40 * GIB, "n": 1203, "m": n, "t": n - 600,
                        "top": [["feat", 70 * GIB, n]]},
                       {"path": root, "name": ".", "b": GIB // 2, "d24": None, "d7": None, "n": 3, "m": n, "t": n - 600, "top": []}],
              "gone": [{"path": f"{root}/tmp_old", "name": "tmp_old", "d24": 0, "d7": -5 * GIB}], "jobs": jobs,
              "at": n - 600, "free": 500 * GIB, "total": 2000 * GIB, "eta": 5.25, "eta7": None, "warn_gb": 30, "burst_gb": 30, "bigwrite_gb": 5,
              "local": {"h1": {"cache": 12 * GIB, "scratch": GIB // 2}}}
        self.snap(storage=st)
        rc, out, err = run(["disk", "-c", str(self.cfg)])
        self.assertEqual(rc, 0, err)
        for x in ("共享盘剩 500 GiB / 共 2000 GiB（25%）；按近 24 小时的速度约 5.25 天满",
                  f"本项目在共享盘上（{root}）：合计 120 GiB（近 24 小时 +33 GiB，近 7 天 +39 GiB）；近 24 小时涨得快", "还有 2 个目录没扫到",
                  "cache  80 GiB（近 24 小时 +33 GiB，近 7 天 +40 GiB）  1203 个文件", "      feat  70 GiB", "out  40 GiB（近 7 天 −1.0 GiB）",
                  "项目根下的文件  0.5 GiB", "tmp_old（已删）  近 7 天 −5.0 GiB",
                  "a1、a2 跑的这段时间本项目在共享盘上净增 33 GiB，大头 cache +30 GiB；h1 上", "超过提醒线",
                  "b1 跑的这段时间本项目在共享盘上净减 2.0 GiB；h1 上", "还在等重扫",
                  "h1（A6000）：本项目缓存 12 GiB，单格临时 0.5 GiB；容器 /tmp 剩 24.9 GiB", "h2：本项目没有缓存"):
            self.assertIn(x, out)
        self.assertLess(out.index("cache  80"), out.index("out  40"))
        self.assertLess(out.index("b1 跑的"), out.index("a1、a2 跑的"))              # 新结束的在前；同一组扫描的合成一条
        self.assertEqual(out.count("a2"), 1)
        for x in ("h3", "算力池"):
            self.assertNotIn(x, out)
        js = json.loads(run(["disk", "p1", "--json"])[1])
        self.assertEqual(js["jobs"][2]["grp"], f"b1:{n - 300}")

    def test_disk_nothing_scanned(self):
        board(self)
        rc, out, _ = run(["disk", "p1"])                 # 快照里的 storage 还空着（#53 之前的快照没有这一项）
        self.assertEqual(rc, 0)
        self.assertIn("共享盘的总量与剩余还没读到", out)
        self.assertIn("共享盘还没扫到本项目的目录", out)
        (PJ.out_dir("p1") / "snapshot.json").unlink()
        rc, out, _ = run(["disk", "p1"])
        self.assertIn("第一次采集还没完成", out)
        self.assertEqual(run(["disk", "p1", "--json"])[1].strip(), "null")

    def test_disk_me_and_fallback(self):
        """#211：labmon disk --me 与无 monitor.toml 时回退 GET /api/disk，仅展示本人共享盘与总盘概况。"""
        from labmon import fleetboot as FB, users as U
        board(self)
        FB.set_person("OWNER", create=True, roster_only=True)
        FB.set_person("OTHER", create=True, roster_only=True)
        tok = make_token("amy", role="user", scopes=("read",))
        with U._edit() as d:
            d["users"]["amy"]["fleet_person"] = "OWNER"
        os.environ["LABMON_TOKEN"] = tok
        self.glob(disk={
            "root": "/data/share", "at": self.now - 120, "eta": 14.5, "warn_gb": 30,
            "df": {"total": 1000 * GIB, "used": 400 * GIB, "free": 600 * GIB},
            "names": ["OWNER", "OTHER"], "now": "/data/share/other/secret",
            "owners": [
                {"name": "OWNER", "public": False, "b": 80 * GIB, "d24": 4 * GIB, "d7": 9 * GIB, "cold": 12 * GIB,
                 "units": [{"u": "owner/proj", "b": 80 * GIB, "d24": 4 * GIB, "d7": 9 * GIB, "n": 100}]},
                {"name": "OTHER", "public": False, "b": 200 * GIB, "units": [{"u": "other/secret", "b": 200 * GIB}]},
            ],
            "alerts": [{"key": "low", "msg": "剩余不足"}, {"key": "burst:OWNER", "msg": "OWNER 涨得快"}, {"key": "burst:OTHER", "msg": "OTHER 涨得快"}],
        })
        # 1. labmon disk --me（文本 + JSON）
        rc, out, err = run(["disk", "--me"])
        self.assertEqual(rc, 0, err)
        for s in ("共享盘剩 600 GiB / 共 1000 GiB", "本人（OWNER）在共享盘上：合计 80 GiB", "owner/proj  80 GiB", "OWNER 涨得快"):
            self.assertIn(s, out)
        for bad in ("OTHER", "other/secret", "OTHER 涨得快"):
            self.assertNotIn(bad, out)
        rc, out, err = run(["disk", "--me", "--json"])
        self.assertEqual(rc, 0, err)
        js = json.loads(out)
        self.assertEqual([o["name"] for o in js["owners"]], ["OWNER"])
        self.assertNotIn("names", js)
        # 2. --me 与项目 id / -c 互斥
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf), self.assertRaises(SystemExit) as cm:
            M.main(["disk", "p1", "--me"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("不能和项目 id", err_buf.getvalue())
        # 3. 无项目参数且当前目录无 monitor.toml 时自动回退 GET /api/disk
        empty_dir = self.d / "empty_cwd"
        empty_dir.mkdir()
        old_cwd = os.getcwd()
        try:
            os.chdir(empty_dir)
            rc, out, err = run(["disk"])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(rc, 0, err)
        self.assertIn("本人（OWNER）在共享盘上", out)
        # 4. 路由登记与备用节点检查
        self.assertTrue(SV.CLI_READS.fullmatch("/api/disk"))
        r = SV.route_of("GET", "/api/disk")
        self.assertEqual((r.scope, r.own, r.admin), ("read", None, False))
        # 5. 未绑定或无目录人员的降级提示
        os.environ["LABMON_TOKEN"] = make_token("stranger", role="user", scopes=("read",))
        rc, out, err = run(["disk", "--me"])
        self.assertEqual(rc, 0, err)
        self.assertIn("共享盘上暂无归属 stranger 的目录", out)

    def test_records_only_this_project(self):
        board(self)
        t = self.now
        log_events([{"kind": "relaunch", "host": "h1", "msg": "拉起 worker"}], t - 300)
        log_events([{"kind": "void", "host": "h1", "project": "other", "msg": "别的项目的作废"}], t - 200)
        log_events([{"kind": "host_down", "host": "h9", "msg": "别的主机掉线"}], t - 100)
        log_events([{"kind": "override", "okind": "pause", "target": "p1", "msg": "暂停 p1"}], t - 50)
        log_events([{"kind": "override", "okind": "pause", "target": "other", "msg": "暂停 other"}], t - 40)
        log_events([{"kind": "renew", "hosts": ["h2", "h9"], "msg": "续租 h2", "detail": "第一行\n第二行"}], t - 30)
        rc, out, err = run(["records", "-c", str(self.cfg)])
        self.assertEqual(rc, 0, err)
        for s in ("[h1] 拉起：拉起 worker", "[全部主机] 指令：暂停 p1", "[h2、h9] 续租：续租 h2", "      第一行\n      第二行"):
            self.assertIn(s, out)
        for s in ("别的项目的作废", "别的主机掉线", "暂停 other"):
            self.assertNotIn(s, out)
        self.assertLess(out.index("续租 h2"), out.index("拉起 worker"))     # 新的在前
        rc, out, _ = run(["records", "p1", "--host", "h1"])
        self.assertIn("拉起 worker", out)
        self.assertNotIn("暂停 p1", out)
        self.assertNotIn("续租", out)
        rc, out, _ = run(["records", "p1", "-n", "1"])
        self.assertEqual(out.count("\n"), 3)                                # 一条（带两行细节）
        rc, out, _ = run(["records", "p1", "-q", "拉起"])
        self.assertIn("拉起 worker", out)
        self.assertNotIn("续租", out)
        rc, _, err = run(["records", "p1", "--host", "h9"])
        self.assertEqual(rc, 3)
        self.assertIn("不在项目 p1 用的主机里", err)
        rc, out, _ = run(["records", "p1", "--json", "--cat", "queue"])
        self.assertEqual({e["kind"] for e in json.loads(out)}, {"relaunch", "override"})

    def test_skill_uses_commands_not_pages(self):
        # skill 里不再叫会话看页面、自己 curl：要读的都换成命令（#52）。叫用户在页面上做的（权重、归档）可以提。
        text = "".join(p.read_text() for p in sorted((ROOT / "skills" / "lab-experiments").glob("*.md")))
        for bad in ("curl", "#/log", "snapshot.json", "主机页", "组内服务器页", "记录页", "项目页「", "项目页车道", "项目页多", "项目页时间线", "labmon fleet", "labmon service"):
            self.assertFalse(bad in text, f"skill 里还有 {bad}")
        for need in ("labmon status", "labmon disk", "labmon records", "$LABMON_CACHE"):
            self.assertTrue(need in text, f"skill 里没有 {need}")

    def test_lease_end_expose_hosts_collect_and_status(self):
        """#272：GET /api/hosts、collect._hosts 项目快照、labmon host list 与 labmon status 暴露平台租期到期时间，无租期机不显示。"""
        from labmon import api as A
        from labmon import collect as CL
        from labmon.config import fmt_time

        hosts_toml = self.d / "hosts_lease.toml"
        hosts_toml.write_text(
            '[[hosts]]\nid = "h1"\nssh = "root@192.0.2.1"\nlabel = "自用机1"\nlease_days = 7\n'
            '[[hosts]]\nid = "ext1"\nssh = "u@203.0.113.10"\nlabel = "自有外部机"\nplatform_kind = "none"\nstandalone = true\n'
            '[[hosts]]\nid = "ext2"\nssh = "u@203.0.113.11"\nlabel = "外租机"\nplatform_kind = "other"\nstandalone = true\nlease_until = "2026-12-01 12:00"\n'
            '[[hosts]]\nid = "ret1"\nssh = "root@192.0.2.2"\nlabel = "已归还机"\nreturned = "2026-11-01 10:00"\nlease_days = 7\n'
        )
        le1 = round(self.now + 3 * 86400)
        with mock.patch.dict(os.environ, {"LABMON_HOSTS": str(hosts_toml)}):
            save_json(state_dir() / "snapshot.json", {
                "generated": self.now,
                "hosts": [
                    {"id": "h1", "worker_up": True, "lease_end": le1, "status": {"running": [], "info": {"gpus": []}}},
                    {"id": "ext1", "worker_up": True, "status": {"running": [], "info": {"gpus": []}}},
                ],
            })
            # 1. GET /api/hosts 透传动态 lease_end（外租机无快照时回退 lease_until；无租期机与已归还机为 None）
            res = A.hosts()
            by_id = {h["id"]: h for h in res["hosts"]}
            self.assertEqual(by_id["h1"]["lease_end"], le1)
            self.assertIsNone(by_id["ext1"]["lease_end"])
            self.assertIsNotNone(by_id["ext2"]["lease_end"])
            self.assertIsNone(by_id["ret1"]["lease_end"])

            # 2. labmon host list 展示有租期主机的到期时间，无租期外部机与已归还机不显示
            board(self)
            rc, out, err = run(["host", "list"])
            self.assertEqual(rc, 0, err)
            lines = {ln.strip().split()[0]: ln for ln in out.splitlines() if ln.strip()}
            self.assertIn(f"租期到 {fmt_time(le1)}", lines["h1"])
            self.assertIn("租期到 12-01 12:00", lines["ext2"])
            self.assertNotIn("租期到", lines["ext1"])
            self.assertNotIn("租期到", lines["ret1"])

            # 3. collect._hosts 透传 lease_end 到项目快照 snap["hosts"]，无租期机缺省
            cfg = {
                "id": "p1",
                "hosts": [
                    {"id": "h1", "label": "自用机1", "ssh": None, "returned": None, "mounts": [], "checks": []},
                    {"id": "ext1", "label": "自有外部机", "ssh": None, "returned": None, "mounts": [], "checks": []},
                ],
                "queues": [],
            }
            probes = {
                "h1": ({"t": self.now, "gpu": [], "cgroup": {}, "mounts": {}, "checks": {}}, None),
                "ext1": ({"t": self.now, "gpu": [], "cgroup": {}, "mounts": {}, "checks": {}}, None),
            }
            psnap: dict = {"hosts": [], "alerts": []}
            sd = self.d / "sd_lease"
            sd.mkdir()
            CL._hosts(cfg, probes, False, load_json(state_dir() / "snapshot.json", {}), psnap, {}, [], sd, self.now)
            ph = {h["id"]: h for h in psnap["hosts"]}
            self.assertEqual(ph["h1"].get("lease_end"), le1)
            self.assertNotIn("lease_end", ph["ext1"])

            # 4. labmon status 文本与 --json 展示 lease_end（无租期机不显示）
            self.snap(hosts=[
                {"id": "h1", "label": "h1（A6000）", "ok": True, "lease_end": le1,
                 "gpus": [{"index": 0, "util": 50, "mem_used": 10240, "mem_total": 46068}]},
                {"id": "ext1", "label": "自有外部机", "ok": True,
                 "gpus": [{"index": 0, "util": 10, "mem_used": 2048, "mem_total": 24576}]},
            ])
            rc, out, err = run(["status", "p1"])
            self.assertEqual(rc, 0, err)
            h1_line = next(ln for ln in out.splitlines() if "h1（A6000）" in ln)
            ext1_line = next(ln for ln in out.splitlines() if "自有外部机" in ln)
            self.assertIn(f"租期到 {fmt_time(le1)}", h1_line)
            self.assertNotIn("租期到", ext1_line)

            rc, jout, err = run(["status", "p1", "--json"])
            self.assertEqual(rc, 0, err)
            jhosts = {h["id"]: h for h in json.loads(jout)["hosts"]}
            self.assertEqual(jhosts["h1"]["lease_end"], le1)
            self.assertNotIn("lease_end", jhosts["ext1"])


if __name__ == "__main__":
    unittest.main()
