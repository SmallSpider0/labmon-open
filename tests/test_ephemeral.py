"""推模式临时主机（Google Colab 等）与云盘共享存储监控测试（#213）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
