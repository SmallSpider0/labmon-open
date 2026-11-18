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
from labmon import disk as DK  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import serve as SV  # noqa: E402
from labmon.config import GIB  # noqa: E402
from labmon.records import log_events  # noqa: E402
from labmon.state import save_json, state_dir  # noqa: E402

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
                       {"id": "h3", "label": "h3", "returned": now - 86400}],
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
        g = {"generated": self.now, "hosts": [{"id": "h1", "status": {"running": [{"project": "p1", "name": "train_s1", "use": {"cores": 3.25}},
                                                                                  {"project": "other", "name": "train_s1", "use": {"cores": 9}}]}}]}
        g.update(kw)
        save_json(state_dir() / "snapshot.json", g)

    def test_status_prints_project_overview(self):
        board(self)
        self.glob()
        rc, out, err = run(["status", "-c", str(self.cfg)])
        self.assertEqual(rc, 0, err)
        for s in ("示例（p1）快照", "（2 分钟前）", "预计全部跑完", "h1（A6000）：正常；卡 0 97% 30.0/45 GiB", "h2：连不上：连不上", "h3：已归还",
                  "车道 1（h1，jobs/a.txt）：2/5 跑完，失败 1；预计", "车道 1（h2，jobs/b.txt）：0/3 跑完", "推演排不上：放不下：要 60 GiB 显存",
                  "train_s1（车道 1（h1））：进度 50/200，已跑 30 分钟，预计", "（近期速度）", "CPU 3.2 核", "日志 1 分钟前：PROGRESS 50/200 loss=0.3",
                  "排队（2 个）", "train_s2（车道 1（h1））：预计", "big（车道 1（h2））：推演不出何时开始",
                  "train_s0（车道 1（h1））", "退出码 137", "日志 /data/p1/_logs/train_s0.log",
                  "E1 训练：运行中，2/5 跑完，1 在跑，1 排队，失败 1", "train 实际/估时 ×0.80",
                  "挪法建议：可以把 h1 上 jobs/a.txt 的 train_s2 挪到 h4", "告警（2 条）", "[failed:train_s0] train_s0 失败", "[ssh_fail:h2] h2 连不上（要用户处理）"):
            self.assertIn(s, out)
        self.assertNotIn("看板没在采集", out)
        rc, out, _ = run(["status", "p1", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["rebalance"]["key"], "rebalance:p1")

    def test_status_stale_and_missing_snapshot(self):
        board(self)
        self.snap(generated=self.now - 3600, rebalance={"why": ["h4 放不下"]})
        rc, out, _ = run(["status", "p1"])
        self.assertIn("看板没在采集，告诉用户", out)
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
        self.glob(disk={"df": {"free": 500 * GIB, "total": 2000 * GIB}, "eta": 5.25, "eta7": None, "warn_gb": 30,
                        "ours": {"base": "/data/alice", "dirs": [{"u": root, "name": "projects/p1", "b": 120 * GIB, "t": self.now - 600, "p": "p1"},
                                                                {"u": "/data/alice/projects/q", "b": 9 * GIB, "t": self.now, "p": "q"}]}},
                  hosts=[{"id": "h1", "status": {"storage": {"local": {"p1": {"cache": 12 * GIB, "scratch": GIB // 2}}}}}])
        rc, out, err = run(["disk", "-c", str(self.cfg)])
        self.assertEqual(rc, 0, err)
        for s in ("共享盘剩 500.0 GiB / 共 2000.0 GiB（25%）；按近 24 小时的速度约 5.25 天满", f"{root}  120.0 GiB", "合计 120.0 GiB",
                  "h1（A6000）：本项目缓存 12.0 GiB，单格临时 0.5 GiB；容器 /tmp 剩 24.9 GiB", "h2：本项目没有缓存"):
            self.assertIn(s, out)
        self.assertNotIn("projects/q", out)
        self.assertNotIn("h3", out)
        # #53 的细分：项目根下一级目录、24 小时与 7 天增量、最大的几项（假数据：字段名等 #53 定）
        fake = {**DK.project_storage("p1"), "dirs": {f"{root}/cache": 80 * GIB, f"{root}/out": 40 * GIB},
                "d24": {f"{root}/cache": 33 * GIB}, "d7": {f"{root}/cache": 40 * GIB, f"{root}/out": -GIB},
                "top": {f"{root}/cache": [[f"{root}/cache/feat", 70 * GIB]]}}
        with mock.patch.object(DK, "project_storage", lambda pid, snap=None: fake):
            rc, out, _ = run(["disk", "p1"])
            js = json.loads(run(["disk", "p1", "--json"])[1])
        for s in (f"{root}/cache  80.0 GiB（近 24 小时 +33.0 GiB，近 7 天 +40.0 GiB）", f"{root}/out  40.0 GiB（近 7 天 −1.0 GiB）",
                  f"{root}/cache/feat  70.0 GiB", "合计 120.0 GiB，近 24 小时 +33.0 GiB"):
            self.assertIn(s, out)
        self.assertLess(out.index("/cache  "), out.index("/out  "))
        self.assertEqual(js["d24"], {f"{root}/cache": 33 * GIB})

    def test_disk_nothing_scanned(self):
        board(self)
        self.glob()
        rc, out, _ = run(["disk", "p1"])
        self.assertEqual(rc, 0)
        self.assertIn("共享盘的总量与剩余还没读到", out)
        self.assertIn("共享盘还没扫到本项目的目录", out)

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
        for bad in ("curl", "#/log", "snapshot.json", "主机页", "组内服务器页", "记录页", "项目页「", "labmon fleet", "labmon service"):
            self.assertFalse(bad in text, f"skill 里还有 {bad}")
        for need in ("labmon status", "labmon disk", "labmon records", "$LABMON_CACHE"):
            self.assertTrue(need in text, f"skill 里没有 {need}")


if __name__ == "__main__":
    unittest.main()
