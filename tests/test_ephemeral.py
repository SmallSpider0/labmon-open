"""推模式临时主机（Google Colab 等）与云盘共享存储监控测试（#213）。"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib import error as urlerr
from urllib import request as urlreq

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
import fixtures as FX  # noqa: E402

from labmon import api as API  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import deploy as DP  # noqa: E402
from labmon import disk as DK  # noqa: E402
from labmon import ephemeral as EP  # noqa: E402
from labmon import labmon_ephemeral as LE  # noqa: E402
from labmon import lease as LS  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import state as ST  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon import waits as W  # noqa: E402


def _reg_project(pid: str = "proj-a", owner: str = "boss", title: str = "测试项目") -> None:
    """在当前 LABMON_STATE 下登记一个无 SSH 主机的项目。"""
    toml = f'id = "{pid}"\ntitle = "{title}"\n'
    PJ.put_config(pid, toml, {}, None, owner=owner)


def _sample_payload(
    *,
    host_id: str = "colab-t4-01",
    label: str = "Colab T4 #1",
    instance_id: str = "inst-1",
    project: str = "proj-a",
    ttl_s: int = 180,
    client_t: float | None = None,
    closing: bool = False,
    jobs: list[dict] | None = None,
    storage: list[dict] | None = None,
) -> dict:
    return {
        "host_id": host_id,
        "label": label,
        "instance_id": instance_id,
        "project": project,
        "ttl_s": ttl_s,
        "client_t": client_t,
        "closing": closing,
        "gpus": [{"index": 0, "name": "Tesla T4", "mem_used": 6144.0, "mem_total": 15360.0, "util": 85.0}],
        "cpu_pct": 45.0,
        "cpu_count": 2,
        "mem": {"used_gb": 4.2, "limit_gb": 12.7},
        "storage": storage if storage is not None else [
            {"path": "/content/drive/MyDrive", "label": "Google Drive", "kind": "cloud", "total_gb": 15.0, "avail_gb": 10.5},
            {"path": "/content", "label": "Colab 本地盘", "kind": "local", "total_gb": 78.0, "avail_gb": 50.0},
        ],
        "jobs": jobs if jobs is not None else [
            {
                "id": "colab_s1",
                "status": "running",
                "gpus": [0],
                "start_ts": (client_t or time.time()) - 300,
                "est_min": 60.0,
                "progress": [10, 100],
                "last_line": "PROGRESS 10/100 loss=0.5",
                "artifact_dir": "/content/drive/MyDrive/labmon/proj-a/runs/colab_s1",
            }
        ],
    }


class EphemeralUnitTest(unittest.TestCase):
    """核心状态机、校验、TTL 结转、防撞、低余量告警与隔离。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hosts_file = Path(self.tmp.name) / "hosts.toml"
        self.hosts_file.write_text('[[hosts]]\nid = "srv-a"\nssh = "root@192.0.2.1"\n')
        env = mock.patch.dict(os.environ, {
            "LABMON_STATE": str(Path(self.tmp.name) / "state"),
            "LABMON_HOSTS": str(self.hosts_file),
            "LABMON_NO_NOTIFY": "1",
            "LABMON_NO_KEYCHAIN": "1",
            "LABMON_LAN": "off",
        })
        env.start()
        self.addCleanup(env.stop)
        p_started = mock.patch.dict(DP.RUNNING, {"started": 0})
        p_started.start()
        self.addCleanup(p_started.stop)
        _reg_project("proj-a", "boss", "项目 A")
        _reg_project("proj-b", "bob", "项目 B")

    def test_push_validation_clamp_and_clock_calibration(self):
        t0 = 1_800_000_000.0
        # 客户端比服务端快 1 小时，服务端应自动校准时间戳并钳制 ttl_s
        body = _sample_payload(ttl_s=30, client_t=t0 + 3600.0)
        body["jobs"][0]["start_ts"] = t0 + 3600.0 - 120.0
        res, wake = EP.push(body, who="boss", t=t0)
        self.assertTrue(res["ok"])
        self.assertTrue(wake)
        self.assertEqual(res["warnings"], [])
        rec = ST.load_json(EP._file("colab-t4-01"), {})
        self.assertEqual(rec["ttl_s"], EP.TTL_MIN)            # 30 -> 钳制到 90
        self.assertAlmostEqual(rec["jobs"][0]["start_ts"], t0 - 120.0, places=1)

        # ttl_s 上限钳制 600
        res2, _ = EP.push(_sample_payload(ttl_s=9999, client_t=t0 + 10), who="boss", t=t0 + 10)
        self.assertTrue(res2["ok"])
        self.assertEqual(ST.load_json(EP._file("colab-t4-01"), {})["ttl_s"], EP.TTL_MAX)

        # storage 超过 4 项报错 400
        bad_sto = _sample_payload(storage=[{"path": f"/p{i}", "kind": "local", "total_gb": 10, "avail_gb": 5} for i in range(5)])
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(bad_sto, who="boss", t=t0 + 20)
        self.assertEqual(cm.exception.code, 400)

        # artifact_dir 超过 256 字符报错 400
        bad_art = _sample_payload()
        bad_art["jobs"][0]["artifact_dir"] = "/" + "a" * 260
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(bad_art, who="boss", t=t0 + 20)
        self.assertEqual(cm.exception.code, 400)

        # 与 hosts.toml 重名拒绝 409
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(_sample_payload(host_id="srv-a"), who="boss", t=t0 + 20)
        self.assertEqual(cm.exception.code, 409)

        # 跨项目抢占在线 host_id 拒绝 409
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(_sample_payload(host_id="colab-t4-01", project="proj-b"), who="boss", t=t0 + 20)
        self.assertEqual(cm.exception.code, 409)

        # 已归档项目拒绝 409
        ST.save_json(ST.state_dir() / "projects.json", {"archived": {"proj-b": {"id": "proj-b", "t": t0}}})
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(_sample_payload(host_id="colab-b-01", project="proj-b"), who="boss", t=t0 + 20)
        self.assertEqual(cm.exception.code, 409)

    def test_rate_limit_and_active_quota(self):
        t0 = 1_800_000_000.0
        EP.push(_sample_payload(host_id="c1", instance_id="i1"), who="boss", t=t0)
        # 5 秒内再次发普通心跳应被 429 节流
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(_sample_payload(host_id="c1", instance_id="i1"), who="boss", t=t0 + 2.0)
        self.assertEqual(cm.exception.code, 429)

        # 任务终态跃迁（running -> done）在 5 秒内也应放行
        done_body = _sample_payload(host_id="c1", instance_id="i1")
        done_body["jobs"][0].update(status="done", end_ts=t0 + 2.5, rc=0)
        res, wake = EP.push(done_body, who="boss", t=t0 + 2.5)
        self.assertTrue(res["ok"])
        self.assertTrue(wake)

        # closing=True 在 5 秒内同样放行
        close_body = _sample_payload(host_id="c1", instance_id="i1", closing=True, jobs=[])
        res_c, _ = EP.push(close_body, who="boss", t=t0 + 3.0)
        self.assertTrue(res_c["ok"])
        self.assertEqual(ST.load_json(EP._file("c1"), {})["state"], "down")

        # 单项目最多 5 台同时在线：开 5 台成功，第 6 台回 429
        for k in range(1, 6):
            EP.push(_sample_payload(host_id=f"q-{k}", instance_id=f"qi-{k}", jobs=[]), who="boss", t=t0 + 10)
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(_sample_payload(host_id="q-6", instance_id="qi-6", jobs=[]), who="boss", t=t0 + 10)
        self.assertEqual(cm.exception.code, 429)

    def test_ttl_timeout_settle_grace_reconnect_and_purge(self):
        t0 = 1_800_000_000.0
        EP.push(_sample_payload(host_id="colab-1", instance_id="inst-a", ttl_s=180, client_t=t0), who="boss", t=t0)

        # 看板刚重启宽限期：即使距 last_seen 超过 180s，但距 started 不足 180s 不应误判 down
        with mock.patch.dict(DP.RUNNING, {"started": t0 + 150.0}):
            recs = EP.tick(t=t0 + 250.0)
            self.assertEqual(recs[0]["state"], "online")

        # 超过 TTL 无重启宽限：自动标 down、残留 running 结转 failed 并写 usage/<host>.jsonl
        recs = EP.tick(t=t0 + 200.0)
        self.assertEqual(recs[0]["state"], "down")
        self.assertEqual(recs[0]["jobs"][0]["status"], "failed")
        self.assertEqual(recs[0]["jobs"][0]["rc"], -1)
        self.assertEqual([x["id"] for x in recs[0]["lost_jobs"]], ["colab_s1"])
        usage = ST.load_rows(ST.usage_file("colab-1"))
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["n"], "colab_s1")
        self.assertEqual(usage[0]["st"], "failed")

        # 同一 instance_id 重连：恢复 online，清 down_reason
        EP.push(_sample_payload(host_id="colab-1", instance_id="inst-a", client_t=t0 + 210.0), who="boss", t=t0 + 210.0)
        self.assertEqual(ST.load_json(EP._file("colab-1"), {})["state"], "online")

        # 同名 host_id 换新 instance_id：旧实例残留 running 被自动结转进 usage
        body_new = _sample_payload(host_id="colab-1", instance_id="inst-b", client_t=t0 + 230.0, jobs=[])
        EP.push(body_new, who="boss", t=t0 + 230.0)
        rec = ST.load_json(EP._file("colab-1"), {})
        self.assertEqual(rec["instance_id"], "inst-b")

        # 优雅退出后：离线超过 1 小时总览隐藏卡片；满 7 天清理状态与 series 但保留 usage
        EP.push(_sample_payload(host_id="colab-1", instance_id="inst-b", closing=True, jobs=[]), who="boss", t=t0 + 240.0)
        series_f = ST.state_dir() / "series" / "colab-1.jsonl"
        series_f.parent.mkdir(parents=True, exist_ok=True)
        series_f.write_text('{"t": 1}\n')
        self.assertEqual(EP.overview_hosts([], {}, {}, t=t0 + 240.0 + EP.OVERVIEW_HIDE_S + 10), [])
        self.assertTrue(EP._file("colab-1").exists())
        EP.tick(t=t0 + 240.0 + EP.PURGE_S + 10)
        self.assertFalse(EP._file("colab-1").exists())
        self.assertFalse(series_f.exists())
        self.assertTrue(ST.usage_file("colab-1").exists())

    def test_duplicate_job_drive_low_disk_isolation_and_collect_wait_status(self):
        t0 = time.time() - 200.0
        # 两台在线实例同时跑同一个 colab_s1，且第二台云盘剩 0.5 GiB（< 1 GiB）
        r1, _ = EP.push(_sample_payload(host_id="colab-1", instance_id="i1", client_t=t0), who="boss", t=t0)
        self.assertEqual(r1["warnings"], [])
        low_sto = [
            {"path": "/content/drive/MyDrive", "label": "Google Drive", "kind": "cloud", "total_gb": 15.0, "avail_gb": 0.5},
            {"path": "/content", "label": "Colab 本地盘", "kind": "local", "total_gb": 78.0, "avail_gb": 0.3},
        ]
        r2, _ = EP.push(_sample_payload(host_id="colab-2", label="Colab #2", instance_id="i2", client_t=t0, storage=low_sto), who="boss", t=t0)
        self.assertEqual(r2["warnings"], ["duplicate_job:colab_s1"])

        # 第三台心跳超时离线（带残留任务）
        lost_job = [{"id": "colab_lost", "status": "running", "gpus": [0], "start_ts": t0 - 600, "artifact_dir": "/content/drive/MyDrive/labmon/proj-a/runs/colab_lost"}]
        EP.push(_sample_payload(host_id="colab-3", label="Colab #3", instance_id="i3", ttl_s=180, client_t=t0 - 500, jobs=lost_job), who="boss", t=t0 - 500)
        EP.tick(t=t0)

        # 运行项目采集（无任何 SSH 主机）
        cfg = PJ.load_cfg("proj-a")
        with mock.patch.object(C.time, "time", return_value=t0), mock.patch.object(EP.time, "time", return_value=t0):
            snap = C.collect(cfg)
        hids = {h["id"] for h in snap["hosts"]}
        self.assertEqual(hids, {"colab-1", "colab-2", "colab-3"})
        # 校验三类告警都准确生成且带正确受众
        by_key = {a["key"]: a for a in snap["alerts"]}
        self.assertIn("ephemeral_dup:proj-a:colab_s1", by_key)
        self.assertEqual(by_key["ephemeral_dup:proj-a:colab_s1"]["to"], "agent")
        self.assertIn("ephemeral_disk_low:colab-2:/content/drive/MyDrive", by_key)
        self.assertEqual(by_key["ephemeral_disk_low:colab-2:/content/drive/MyDrive"]["to"], "agent")
        # 本地盘低于 1 GiB 不应触发云盘告警
        self.assertNotIn("ephemeral_disk_low:colab-2:/content", by_key)
        self.assertIn("ephemeral_down:colab-3:i3", by_key)
        self.assertEqual(by_key["ephemeral_down:colab-3:i3"]["to"], "both")
        self.assertIn("/content/drive/MyDrive/labmon/proj-a/runs/colab_lost", by_key["ephemeral_down:colab-3:i3"]["msg"])

        # 再次推送进度更新，验证 collect._segment_eta 按进度算速度
        b1_next = _sample_payload(host_id="colab-1", instance_id="i1", client_t=t0 + 120)
        b1_next["jobs"][0]["progress"] = [30, 100]
        EP.push(b1_next, who="boss", t=t0 + 120)
        with mock.patch.object(C.time, "time", return_value=t0 + 120), mock.patch.object(EP.time, "time", return_value=t0 + 120):
            snap2 = C.collect(cfg)
        j1 = next(j for j in snap2["jobs"] if j["id"] == "colab_s1" and j["lane"] == "ephemeral:colab-1")
        self.assertEqual(j1["progress"], [30, 100])
        self.assertEqual(j1["artifact_dir"], "/content/drive/MyDrive/labmon/proj-a/runs/colab_s1")
        self.assertIsNotNone(j1.get("eta_ts"))

        # 验证 labmon/disk.py 完全不被临时主机污染
        ehs_chk = EP.overview_hosts([{"id": "proj-a"}], {}, {}, t=t0 + 120)
        dk_res, dk_notes = DK.update(ehs_chk, t0 + 120)
        self.assertEqual(dk_notes, [])
        self.assertNotIn("colab-1", json.dumps(dk_res or {}, ensure_ascii=False))
        self.assertNotIn("Google Drive", json.dumps(dk_res or {}, ensure_ascii=False))
        ps_dk = DK.project_storage("proj-a", {"disk": dk_res, "hosts": ehs_chk})
        self.assertNotIn("colab-1", json.dumps(ps_dk or {}, ensure_ascii=False))
        self.assertNotIn("Google Drive", json.dumps(ps_dk or {}, ensure_ascii=False))

        # 验证 waits.JobWatch 对已离线临时主机上结转失败的任务不因 stale 卡住，且提示 artifact_dir 不提示 .failed
        res_lost = W.job_state(snap2, "colab_lost", [])
        self.assertEqual(res_lost["st"], "failed")
        self.assertIn("/content/drive/MyDrive/labmon/proj-a/runs/colab_lost", res_lost["text"])
        self.assertNotIn(".failed", res_lost["text"])
        jw = W.JobWatch(cfg, PJ.out_dir("proj-a"), ["colab_lost"])
        jw.t0 = t0 + 100
        chk = None
        for step in range(W.JOB_CONFIRM):
            with mock.patch.object(C.time, "time", return_value=t0 + 121 + step), mock.patch.object(EP.time, "time", return_value=t0 + 121 + step):
                C.collect(cfg)
            chk = jw.check()
        self.assertTrue(chk[("proj-a", "colab_lost")]["final"])
        self.assertEqual(chk[("proj-a", "colab_lost")]["st"], "failed")

        # 验证 api.status 与 api.disk 输出临时主机独立分区
        with mock.patch.object(EP.time, "time", return_value=t0 + 120):
            st_out = API.status("proj-a", now=t0 + 120)["out"]
            dk_out = API.disk("proj-a", snap2)["out"]
        self.assertIn("[临时]", st_out)
        self.assertIn("Google Drive 剩 0.5/15.0 GiB [余量低]", st_out)
        self.assertIn("产物：/content/drive/MyDrive/labmon/proj-a/runs/colab_s1", st_out)
        self.assertIn("临时主机存储（云盘与实例本地盘，不计入实验室共享盘）", dk_out)
        self.assertIn("Google Drive", dk_out)

        # 验证归档与删除闸门：有在线临时主机时不可归档、有在线 running 任务时不可删除
        with mock.patch.object(EP.time, "time", return_value=t0 + 120):
            self.assertIsNotNone(PJ.archive_block("proj-a", snap2, None))
            with self.assertRaises(Exception) as cm:
                O.check_project_idle("proj-a", {}, cfg, snap2, {})
            self.assertIn("临时主机", str(cm.exception))

        # 普通用户隔离：bob 看不到 proj-a 的临时主机，amy（proj-a 主人）能看到
        FX.make_token("bob", role="user", scopes=("project",))
        FX.make_token("amy", role="user", scopes=("project",))
        ehs = EP.overview_hosts([{"id": "proj-a"}, {"id": "proj-b"}], {}, {}, t=t0 + 120)
        ov_snap = {"hosts": ehs, "projects": [{"id": "proj-a", "owner": "amy"}, {"id": "proj-b", "owner": "bob"}]}
        bob_ident = U.Ident("bob", "user", frozenset({"read", "project"}), "token", "k")
        bob_acc = U.access(bob_ident, ov_snap)
        self.assertNotIn("colab-1", bob_acc.hosts)
        amy_ident = U.Ident("amy", "user", frozenset({"read", "project"}), "token", "k")
        PJ.set_owner("proj-a", "amy", "boss")
        amy_acc = U.access(amy_ident, ov_snap)
        self.assertIn("colab-1", amy_acc.hosts)

        # 优雅退出全部在线临时主机后，归档、删除放行且 delete 清理离线状态文件
        for hid, iid in (("colab-1", "i1"), ("colab-2", "i2")):
            EP.push(_sample_payload(host_id=hid, instance_id=iid, closing=True, jobs=[]), who="boss", t=t0 + 130)
        with mock.patch.object(EP.time, "time", return_value=t0 + 130):
            self.assertIsNone(O.check_project_idle("proj-a", {}, cfg, snap2, {}))
        EP.remove_project("proj-a")
        self.assertFalse(EP._file("colab-1").exists())
        self.assertFalse(EP._file("colab-2").exists())
        self.assertFalse(EP._file("colab-3").exists())

    def test_codex_review_regressions(self):
        """覆盖 PR #220 审查发现的 5 项服务端/采集边界（大小写冲突、重连修正 usage、终态重试幂等、乱序非 closing 忽略、离网保留临时日志告警）。"""
        t0 = time.time() - 600.0
        # 1) 大小写不敏感标识冲突：与 hosts.toml 重名（SRV-A）或与另一项目在线主机（Colab-A vs colab-a）均拒绝且不覆盖
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(_sample_payload(host_id="SRV-A"), who="boss", t=t0)
        self.assertEqual(cm.exception.code, 409)
        EP.push(_sample_payload(host_id="Colab-A", instance_id="ia", project="proj-a", client_t=t0), who="boss", t=t0)
        with self.assertRaises(EP.EphemeralError) as cm:
            EP.push(_sample_payload(host_id="colab-a", instance_id="ib", project="proj-b", client_t=t0), who="bob", who_role="user", t=t0)
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(ST.load_json(EP._file("Colab-A"), {})["project"], "proj-a")

        # 2) TTL 超时写入 failed usage 后，同一 instance_id 重连并最终 done → usage 应被修正为唯一 done 行
        b_run = _sample_payload(host_id="colab-rec", instance_id="inst-r", ttl_s=180, client_t=t0)
        b_run["jobs"][0]["start_ts"] = t0 - 100.0
        EP.push(b_run, who="boss", t=t0)
        EP.tick(t=t0 + 200.0)
        u_after_timeout = ST.load_rows(ST.usage_file("colab-rec"))
        self.assertEqual(len(u_after_timeout), 1)
        self.assertEqual(u_after_timeout[0]["st"], "failed")
        # 重连继续运行：应撤销超时误记的 failed 行
        b_cont = _sample_payload(host_id="colab-rec", instance_id="inst-r", ttl_s=180, client_t=t0 + 210.0)
        b_cont["jobs"][0]["start_ts"] = t0 - 100.0
        EP.push(b_cont, who="boss", t=t0 + 210.0)
        self.assertEqual(ST.load_rows(ST.usage_file("colab-rec")), [])
        # 最终成功完成：写入唯一 done 行
        b_done = _sample_payload(host_id="colab-rec", instance_id="inst-r", ttl_s=180, client_t=t0 + 250.0, closing=True)
        b_done["jobs"][0].update(status="done", start_ts=t0 - 100.0, end_ts=t0 + 250.0, rc=0)
        EP.push(b_done, who="boss", t=t0 + 250.0)
        u_final = ST.load_rows(ST.usage_file("colab-rec"))
        self.assertEqual(len(u_final), 1)
        self.assertEqual(u_final[0]["st"], "done")

        # 3) 终态 closing 重试（相同 payload 晚 2 秒重发）应保持时间戳稳定、不产生重复 usage 行
        EP.push(b_done, who="boss", t=t0 + 252.0)
        u_retry = ST.load_rows(ST.usage_file("colab-rec"))
        self.assertEqual(len(u_retry), 1)
        self.assertEqual(u_retry[0]["s"], u_final[0]["s"])
        self.assertEqual(u_retry[0]["t"], u_final[0]["t"])

        # 4) 已 closing=True 关闭的同实例收到乱序迟到的非 closing 心跳，不应被翻活为 online
        EP.push(b_cont, who="boss", t=t0 + 255.0)
        self.assertEqual(ST.load_json(EP._file("colab-rec"), {})["state"], "down")

        # 5) 离开实验室内网（lan["ok"]=False）时，仍保留在线临时主机的 log_silent 告警
        PJ.put_config("proj-off", 'id = "proj-off"\ntitle = "离网测试"\n[alerts]\nlog_silent_min = 1\n', {}, None, owner="boss")
        b_sil = _sample_payload(host_id="colab-sil", instance_id="is", project="proj-off", client_t=t0 + 300.0)
        b_sil["jobs"][0]["start_ts"] = t0 + 100.0
        b_sil["jobs"][0]["log_mtime"] = t0 + 120.0
        EP.push(b_sil, who="boss", t=t0 + 300.0)
        cfg_off = PJ.load_cfg("proj-off")
        with mock.patch.object(C.time, "time", return_value=t0 + 300.0), \
                mock.patch.object(EP.time, "time", return_value=t0 + 300.0), \
                mock.patch.object(C.L, "gather", return_value=({"ok": False, "since": t0, "checked": t0 + 300.0}, {})):
            snap_off = C.collect(cfg_off)
        self.assertTrue(any(a["key"].startswith("log_silent:colab_s1") for a in snap_off["alerts"]), snap_off["alerts"])


class EphemeralServerApiTest(unittest.TestCase):
    """通过真实 HTTP 看板服务验证 POST /api/ephemeral/push 与 GET /api/client/ephemeral.py 的鉴权与限制。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hosts_file = Path(self.tmp.name) / "hosts.toml"
        self.hosts_file.write_text("")
        env = mock.patch.dict(os.environ, {
            "LABMON_STATE": str(Path(self.tmp.name) / "state"),
            "LABMON_HOSTS": str(self.hosts_file),
            "LABMON_NO_NOTIFY": "1",
            "LABMON_NO_KEYCHAIN": "1",
            "LABMON_LAN": "off",
        })
        env.start()
        self.addCleanup(env.stop)
        self.srv = FX.board(self, scopes=("project",), name="boss", role="admin")
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.boss_tok = os.environ["LABMON_TOKEN"]
        # 再建一个普通用户 amy（有 proj-a）和只读令牌
        self.amy_tok = FX.make_token("amy", role="user", scopes=("project",))
        self.read_tok = FX.make_token("reader", role="user", scopes=("read",))
        _reg_project("proj-a", "amy", "Amy 项目")
        _reg_project("proj-b", "boss", "Boss 项目")
        self.srv.sync()

    def _req(self, path: str, body: dict | bytes | None = None, token: str | None = None, method: str = "POST"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if method == "POST":
            headers["Content-Type"] = "application/json"
            data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body or {}).encode("utf-8")
        req = urlreq.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlreq.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urlerr.HTTPError as e:
            return e.code, e.read()

    def test_auth_size_standby_and_client_download(self):
        body = _sample_payload(project="proj-a")
        # 未登录 401
        code, _ = self._req("/api/ephemeral/push", body, token=None)
        self.assertEqual(code, 401)
        # read 令牌 403
        code, _ = self._req("/api/ephemeral/push", body, token=self.read_tok)
        self.assertEqual(code, 403)
        # 普通用户 amy 向他人项目 proj-b 上报 403
        code, _ = self._req("/api/ephemeral/push", _sample_payload(project="proj-b"), token=self.amy_tok)
        self.assertEqual(code, 403)
        # 普通用户 amy 向自己项目 proj-a 上报 200
        code, raw = self._req("/api/ephemeral/push", body, token=self.amy_tok)
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(raw)["ok"])
        # 超过 128 KiB 拒绝 413
        big = b'{"pad":"' + b"x" * (130 * 1024) + b'"}'
        code, _ = self._req("/api/ephemeral/push", big, token=self.amy_tok)
        self.assertEqual(code, 413)
        # 备用看板拒绝 409 standby
        with mock.patch.object(LS, "primary", return_value=False), mock.patch.object(LS, "text", return_value="备用"):
            code, raw = self._req("/api/ephemeral/push", _sample_payload(host_id="colab-sb", project="proj-a"), token=self.amy_tok)
            self.assertEqual(code, 409)
            self.assertTrue(json.loads(raw).get("standby"))
        # GET /api/client/ephemeral.py：无令牌 401，read 令牌 200 返回客户端源码
        code, _ = self._req("/api/client/ephemeral.py", method="GET", token=None)
        self.assertEqual(code, 401)
        code, raw = self._req("/api/client/ephemeral.py", method="GET", token=self.read_tok)
        self.assertEqual(code, 200)
        self.assertIn(b"def track(", raw)

    def test_colab_http_api_auth_isolation_and_standby(self):
        """#281：GET/POST /api/ephemeral/colab 与 /api/me 用户隔离、只读拒写、8 KiB 上限与备用拒写。"""
        # 未登录 401
        code, _ = self._req("/api/ephemeral/colab", {"enabled": True}, token=None)
        self.assertEqual(code, 401)
        # read 令牌 403
        code, _ = self._req("/api/ephemeral/colab", {"enabled": True}, token=self.read_tok)
        self.assertEqual(code, 403)
        # 超 8 KiB 回 413
        code, _ = self._req("/api/ephemeral/colab", {"pad": "x" * 9000}, token=self.amy_tok)
        self.assertEqual(code, 413)
        # amy 开启并上报
        code, raw = self._req(
            "/api/ephemeral/colab",
            {"enabled": True, "balance": 42.5, "rate": 1.5, "active": 1, "expire_at": "2026-12-20"},
            token=self.amy_tok,
        )
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["colab"]["balance"], 42.5)
        # GET /api/ephemeral/colab 与 /api/me 按用户严格隔离（管理员也看不到 amy 的个人 Colab 额度）
        code, raw_a = self._req("/api/ephemeral/colab", method="GET", token=self.amy_tok)
        self.assertEqual((code, json.loads(raw_a)["balance"]), (200, 42.5))
        code, raw_adm = self._req("/api/ephemeral/colab", method="GET", token=self.boss_tok)
        self.assertEqual((code, json.loads(raw_adm)["balance"], json.loads(raw_adm)["enabled"]), (200, None, False))
        code, raw_me = self._req("/api/me", method="GET", token=self.boss_tok)
        self.assertEqual((code, json.loads(raw_me)["colab"]["enabled"]), (200, False))
        # 备用看板拒绝写入
        with mock.patch.object(LS, "primary", return_value=False), mock.patch.object(LS, "text", return_value="备用"):
            code, raw_sb = self._req("/api/ephemeral/colab", {"balance": 10.0}, token=self.amy_tok)
            self.assertEqual(code, 409)
            self.assertTrue(json.loads(raw_sb).get("standby"))


class EphemeralClientTest(unittest.TestCase):
    """零依赖客户端 labmon_ephemeral.py 单元测试（挂载防呆、statvfs、run 包装与 track()）。"""

    def test_check_drive_mount_and_collect_storage(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "MyDrive")
            # 目录不存在报错
            with self.assertRaises(RuntimeError):
                LE.check_drive_mount(p)
            os.makedirs(p)
            # 未挂载（同级目录且非独立挂载点）报错
            with mock.patch.object(os.path, "ismount", return_value=False):
                with self.assertRaises(RuntimeError) as cm:
                    LE.check_drive_mount(p)
                self.assertIn("未检测到挂载点", str(cm.exception))
            # 是挂载点且与 / 不同文件系统时通过
            st_drive = mock.Mock(st_dev=200)
            st_local = mock.Mock(st_dev=100)
            with mock.patch.object(os.path, "ismount", return_value=True), \
                    mock.patch.object(os, "stat", side_effect=lambda x: st_drive if x == p else st_local):
                LE.check_drive_mount(p)

            # collect_storage 仅调用 os.statvfs，上限 4 项
            fake_vfs = mock.Mock(f_frsize=4096, f_blocks=10 * 1024 * 256, f_bavail=4 * 1024 * 256)
            sub_dirs = []
            for i in range(5):
                sd = os.path.join(d, f"loc{i}")
                os.makedirs(sd)
                sub_dirs.append(sd)
            with mock.patch.object(os, "statvfs", return_value=fake_vfs) as mv:
                items = LE.collect_storage(drive_root=p, extra_local=sub_dirs)
                self.assertEqual(len(items), 4)
                self.assertEqual(items[0]["kind"], "cloud")
                self.assertAlmostEqual(items[0]["total_gb"], 10.0, places=1)
                self.assertAlmostEqual(items[0]["avail_gb"], 4.0, places=1)
                self.assertGreaterEqual(mv.call_count, 1)

    def test_track_context_manager_ok_and_error(self):
        sent: list[dict] = []

        def fake_post(url, token, body, timeout=15.0):
            sent.append(json.loads(json.dumps(body)))
            return 200, {"ok": True, "warnings": []}

        with mock.patch.object(LE, "collect_gpus", return_value=[]):
            with LE.track(
                project="proj-a", host_id="colab-nb", job="nb_s1",
                url="http://127.0.0.1:9", token="lm_test",
                artifact_dir="/drv/runs/nb_s1", interval=999, poster=fake_post,
            ) as run:
                run.progress(5, 10, line="loss=0.1")
            self.assertGreaterEqual(len(sent), 2)
            self.assertEqual(sent[0]["jobs"][0]["status"], "running")
            self.assertEqual(sent[-1]["jobs"][0]["status"], "done")
            self.assertTrue(sent[-1]["closing"])
            self.assertEqual(sent[-1]["jobs"][0]["artifact_dir"], "/drv/runs/nb_s1")

            sent.clear()
            with self.assertRaises(ValueError):
                with LE.track(project="proj-a", host_id="colab-nb", job="nb_s2",
                              url="http://127.0.0.1:9", token="lm_test", interval=999, poster=fake_post):
                    raise ValueError("boom")
            self.assertEqual(sent[-1]["jobs"][0]["status"], "failed")
            self.assertEqual(sent[-1]["jobs"][0]["rc"], 1)

    def test_run_command_wrapped_progress_and_sigterm(self):
        sent: list[dict] = []

        def fake_post(url, token, body, timeout=15.0):
            sent.append(json.loads(json.dumps(body)))
            return 200, {"ok": True, "warnings": []}

        with mock.patch.object(LE, "collect_gpus", return_value=[]):
            tr = LE.EphemeralTracker(
                url="http://127.0.0.1:9", token="lm_test", project="proj-a",
                host_id="colab-cli", job="cli_s1", interval=999,
                artifact_dir="/drv/runs/cli_s1", poster=fake_post,
            )
            rc = LE.run_command_wrapped(
                [sys.executable, "-c", "print('PROGRESS 4/8', flush=True); print('done', flush=True)"],
                tr,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(sent[-1]["jobs"][0]["status"], "done")
            self.assertEqual(sent[-1]["jobs"][0]["progress"], [4, 8])
            self.assertTrue(sent[-1]["closing"])

            # 1) 忽略 SIGTERM 且不关闭 stdout 的子进程：必须在 grace_s 到期时强杀退出并上报终态
            sent.clear()
            tr2 = LE.EphemeralTracker(
                url="http://127.0.0.1:9", token="lm_test", project="proj-a",
                host_id="colab-sig", job="sig_s1", interval=999, poster=fake_post,
            )
            t_start = time.monotonic()

            def _send_term():
                time.sleep(0.15)
                os.kill(os.getpid(), signal.SIGTERM)

            th = threading.Thread(target=_send_term, daemon=True)
            th.start()
            rc2 = LE.run_command_wrapped(
                [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"],
                tr2,
                grace_s=0.15,
            )
            elapsed = time.monotonic() - t_start
            self.assertLess(elapsed, 3.0, f"忽略 SIGTERM 的子进程未在宽限期被强杀：elapsed={elapsed:.2f}s")
            self.assertNotEqual(rc2, 0)
            self.assertTrue(sent[-1]["closing"])
            self.assertEqual(sent[-1]["jobs"][0]["status"], "failed")

    def test_finish_waits_for_inflight_heartbeat(self):
        """在途普通心跳尚未返回时调用 finish()，必须等其结束后再发送 closing=True，绝不并发乱序。"""
        order: list[tuple[bool, str]] = []
        entered = threading.Event()
        release = threading.Event()

        def slow_post(url, token, body, timeout=15.0):
            closing = bool(body.get("closing"))
            if not closing and len(order) == 1:
                entered.set()
                release.wait(timeout=2.0)
            order.append((closing, body["jobs"][0]["status"]))
            return 200, {"ok": True, "warnings": []}

        with mock.patch.object(LE, "collect_gpus", return_value=[]):
            tr = LE.EphemeralTracker(
                url="http://127.0.0.1:9", token="lm_test", project="proj-a",
                host_id="colab-ser", job="ser_s1", interval=5.0, poster=slow_post,
            )
            tr.interval = 0.05
            tr.start()
            self.assertTrue(entered.wait(timeout=2.0), "后台普通心跳未进入在途状态")
            fin_done = threading.Event()

            def _do_finish():
                tr.finish(ok=True, rc=0)
                fin_done.set()

            ft = threading.Thread(target=_do_finish)
            ft.start()
            time.sleep(0.1)
            self.assertFalse(fin_done.is_set(), "在途普通心跳未返回前 finish() 不应抢发 closing=True")
            release.set()
            ft.join(timeout=2.0)
            self.assertTrue(fin_done.is_set())
            self.assertEqual(order[-1], (True, "done"))
            self.assertEqual(order[-2], (False, "running"))

    def test_ca_unassign_and_tpu_cpu_detection(self):
        """#281：--ca/LABMON_CA、--unassign 终态释放、TPU/CPU 规格识别与 started_at/accel/cu_rate 上报。"""
        with tempfile.TemporaryDirectory() as td:
            ca_file = Path(td) / "ca.pem"
            ca_file.write_text("dummy-ca")
            self.assertEqual(LE.resolve_ca(str(ca_file)), str(ca_file))
            with mock.patch.dict(os.environ, {"LABMON_CA": str(ca_file)}):
                self.assertEqual(LE.resolve_ca(None), str(ca_file))
            with self.assertRaises(RuntimeError):
                LE.resolve_ca(str(Path(td) / "missing.pem"))

            # post_push 带 ca 时创建带 cafile 的 SSL 上下文
            with mock.patch.object(LE.ssl, "create_default_context", return_value="CTX") as mk_ctx, \
                    mock.patch.object(LE.urllib.request, "urlopen") as uopen:
                resp = mock.MagicMock()
                resp.status = 200
                resp.read.return_value = b'{"ok": true}'
                uopen.return_value.__enter__.return_value = resp
                code, body = LE.post_push("https://board.example", "tok", {"a": 1}, ca=str(ca_file))
                self.assertEqual(code, 200)
                mk_ctx.assert_called_once_with(cafile=str(ca_file))
                self.assertEqual(uopen.call_args.kwargs.get("context"), "CTX")

        # TPU 与纯 CPU 规格识别
        self.assertEqual(LE.detect_accel([{"name": "NVIDIA A100-SXM4-40GB"}]), "A100")
        self.assertEqual(LE.detect_accel([{"name": "Tesla T4"}]), "T4")
        self.assertEqual(LE.detect_accel([], env={"TPU_ACCELERATOR_TYPE": "v6e-1"}), "TPU v6e-1")
        self.assertEqual(LE.detect_accel([], env={"COLAB_TPU_ADDR": "10.0.0.2:8470"}), "TPU")
        self.assertEqual(LE.detect_accel([], env={}), "CPU")
        hid_tpu = LE.default_host_id([], accel="TPU v6e-1")
        self.assertTrue(hid_tpu.startswith("colab-tpu-v6e-1-"))
        self.assertIn("TPU v6e-1", LE.default_label(hid_tpu, [], accel="TPU v6e-1"))
        hid_cpu = LE.default_host_id([], accel="CPU")
        self.assertTrue(hid_cpu.startswith("colab-cpu-"))

        # --unassign 在 finish() 发送终态心跳之后才调用 unassign_runtime()
        seq: list[str] = []

        def fake_post(url, token, body, timeout=15.0, ca=None):
            seq.append("push:" + ("close" if body.get("closing") else "open"))
            return 200, {"ok": True, "warnings": []}

        with mock.patch.object(LE, "collect_gpus", return_value=[]), \
                mock.patch.object(LE, "unassign_runtime", side_effect=lambda: seq.append("unassign") or True):
            tr = LE.EphemeralTracker(
                url="http://127.0.0.1:9", token="lm_test", project="proj-a",
                job="tpu_job", interval=60.0, unassign=True,
                started_at=1700000000.0, accel="TPU v6e-1", cu_rate=2.4, poster=fake_post,
            )
            payload = tr.build_payload()
            self.assertEqual(payload["started_at"], 1700000000.0)
            self.assertEqual(payload["accel"], "TPU v6e-1")
            self.assertEqual(payload["cu_rate"], 2.4)
            tr.start()
            tr.finish(ok=True, rc=0)
            self.assertEqual(seq, ["push:open", "push:close", "unassign"])


class EphemeralColabV2Test(unittest.TestCase):
    """#281：个人 Colab 状态隔离存储、API 鉴权、started_at/accel/cu_rate 透传与删账号清理。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.env = mock.patch.dict(os.environ, {
            "LABMON_STATE": str(self.root / "state"),
            "LABMON_HOSTS": str(self.root / "hosts.toml"),
            "LABMON_NO_NOTIFY": "1",
            "LABMON_NO_KEYCHAIN": "1",
            "LABMON_LAN": "on",
        })
        self.env.start()
        (self.root / "hosts.toml").write_text("")
        LS.set_marker("primary", "test")

    def tearDown(self):
        self.env.stop()
        self.td.cleanup()

    def test_user_colab_storage_isolation_and_validation(self):
        # 默认关闭
        self.assertEqual(EP.get_colab("alice"), {
            "enabled": False, "balance": None, "rate": None, "active": None, "updated": None, "expire_at": None,
        })
        self.assertIsNone(EP.colab_view("alice"))

        # 增量保存：先由 CLI 推余额（不自动开启），再由前端开启并设到期日，两边互不覆盖
        s1 = EP.save_colab("alice", {"balance": 88.5, "rate": 1.96, "active": 1}, t=1700001000.0)
        self.assertFalse(s1["enabled"])
        self.assertEqual(s1["balance"], 88.5)
        self.assertEqual(s1["updated"], 1700001000.0)
        self.assertIsNone(EP.colab_view("alice"))

        s2 = EP.save_colab("alice", {"enabled": True, "expire_at": "2026-12-15"}, t=1700001050.0)
        self.assertTrue(s2["enabled"])
        self.assertEqual(s2["balance"], 88.5)
        self.assertEqual(s2["expire_at"], "2026-12-15")
        self.assertEqual(s2["updated"], 1700001000.0)
        self.assertIsNotNone(EP.colab_view("alice"))

        # 接受 Unix 时间戳作为 expire_at，拒绝非法日期
        s3 = EP.save_colab("alice", {"expire_at": 1796083200}, t=1700001100.0)
        self.assertRegex(s3["expire_at"], r"^\d{4}-\d{2}-\d{2}$")
        with self.assertRaises(EP.EphemeralError):
            EP.save_colab("alice", {"expire_at": "2026-02-31"})
        with self.assertRaises(EP.EphemeralError):
            EP.save_colab("alice", {"enabled": "yes"})

        # 绝不写 hosts.toml
        self.assertEqual((self.root / "hosts.toml").read_text(), "")

        # 按用户严格隔离：users.view(alice) 见 alice；users.view(admin) 与 users.view(bob) 绝不见 alice 的 my_colab
        base_snap = {"generated": 1700001200.0, "lan": True, "hosts": [], "projects": [], "archived": [],
                     "rebalance": {}, "colors": {}, "regs": [], "occ": {}, "event_kinds": {}}
        alice_id = U.Ident("alice", "user", frozenset({"read", "project"}), "session", "ka")
        bob_id = U.Ident("bob", "user", frozenset({"read", "project"}), "session", "kb")
        admin_id = U.Ident("admin", "admin", frozenset(U.SCOPES), "session", "kadm")

        va = U.view(alice_id, base_snap)
        vb = U.view(bob_id, base_snap)
        vadm = U.view(admin_id, base_snap)
        self.assertEqual(va["my_colab"]["balance"], 88.5)
        self.assertIsNone(vb.get("my_colab"))
        self.assertIsNone(vadm.get("my_colab"))
        self.assertNotIn("my_colab", base_snap, "users.view 不得原地修改共享 snap")

        # 删除账号同步清理个人 Colab 状态文件
        EP.remove_user_colab("alice")
        self.assertIsNone(EP.colab_view("alice"))
        self.assertFalse(EP.get_colab("alice")["enabled"])

    def test_push_started_at_accel_cu_rate_passthrough(self):
        cfg_toml = 'id = "proj-v2"\ntitle = "V2"\n'
        PJ.put_config("proj-v2", cfg_toml, by="admin")
        t0 = 1793800000.0
        EP.push({
            "project": "proj-v2", "host_id": "colab-a100-01", "instance_id": "inst-a1",
            "label": "Colab A100", "started_at": t0 - 7200.0, "accel": "A100", "cu_rate": 11.77,
            "client_t": t0, "gpus": [{"index": 0, "name": "NVIDIA A100", "mem_used": 4096, "mem_total": 40960, "util": 60}],
            "jobs": [{"id": "j1", "status": "running", "gpus": [0]}],
        }, who="admin", t=t0)
        # 同 instance_id 第二次心跳不传 accel/cu_rate 时应保留已落盘值且锚定 started_at
        EP.push({
            "project": "proj-v2", "host_id": "colab-a100-01", "instance_id": "inst-a1",
            "started_at": t0 - 100.0, "client_t": t0 + 30.0,
            "gpus": [{"index": 0, "name": "NVIDIA A100", "mem_used": 4096, "mem_total": 40960, "util": 65}],
            "jobs": [{"id": "j1", "status": "running", "gpus": [0]}],
        }, who="admin", t=t0 + 30.0)
        ohs = EP.overview_hosts([{"id": "proj-v2"}], {}, {}, t0 + 35.0)
        self.assertEqual(len(ohs), 1)
        self.assertEqual(ohs[0]["accel"], "A100")
        self.assertEqual(ohs[0]["cu_rate"], 11.77)
        self.assertAlmostEqual(ohs[0]["started_at"], t0 - 7200.0, delta=1.0)
        probe, err = EP.as_probe("colab-a100-01", t0 + 35.0)
        self.assertIsNone(err)
        self.assertEqual(probe["accel"], "A100")
        self.assertEqual(probe["cu_rate"], 11.77)
        self.assertAlmostEqual(probe["started_at"], t0 - 7200.0, delta=1.0)


if __name__ == "__main__":
    unittest.main()
