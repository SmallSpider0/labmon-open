"""Issue #224：已开账号的组员名下服务器自动接入本人调度、单台退出与安全护栏测试。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import bearer_opener, load_worker, no_git_env, no_host, post_req  # noqa: E402

os.environ["LABMON_LAN"] = "on"

from labmon import borrow as B  # noqa: E402
from labmon import config as C  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import fleetboot as FB  # noqa: E402
from labmon import fleetview as FV  # noqa: E402
from labmon import lease as LS  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import serve as S  # noqa: E402
from labmon import users as U  # noqa: E402
from labmon import workerctl as W  # noqa: E402
from labmon.state import load_json, save_json, state_dir  # noqa: E402


HOSTS_TOML = """
[[hosts]]
id = "fixed-a"
label = "固定机 A"
ssh = "root@192.0.2.1"
port = 15101
python = "python3"
data_root = "/data/labmon"
worker_home = "/data/labmon/labmon"
ka_bin = "/data/labmon/keepalive/bin"
shared = ["/data/share"]
worker = "auto"
platform_job = "ADMIN"
platform_pool = "pool-private"
platform_holder = "ADMIN"

[pools.shared]
label = "共有池"
hosts = ["fixed-a"]
members = ["bob"]

[fleet]
interval = 600
lease_days = 7
private_pools = ["pool-private"]

[[fleet.servers]]
id = "srv-alice"
holder = "ALICE"
ssh = "root@192.0.2.10"
port = 15110
pool = "pool-public"

[[fleet.servers]]
id = "srv-charlie"
holder = "CHARLIE"
ssh = "root@192.0.2.11"
port = 15111
pool = "pool-public"

[[fleet.servers]]
id = "srv-dup-addr"
holder = "ALICE"
ssh = "root@192.0.2.1"
port = 15101
pool = "pool-private"
"""


class MemberAutoOnboardTest(unittest.TestCase):
    """自动接入、单台退出/重接、容器重启、删账号、换人、借还、权限与安全护栏。"""

    def setUp(self):
        no_git_env()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_env = {k: os.environ.get(k) for k in ("LABMON_STATE", "LABMON_HOSTS", "LABMON_LAN")}
        os.environ["LABMON_STATE"] = str(self.root / "state")
        os.environ["LABMON_HOSTS"] = str(self.root / "hosts.toml")
        os.environ["LABMON_LAN"] = "on"
        Path(os.environ["LABMON_HOSTS"]).write_text(HOSTS_TOML.strip() + "\n")
        # 登记组内人员：ALICE、BOB、CHARLIE、ADMIN
        F.edit_state("people.json", lambda d: d.update({
            "ADMIN": {}, "ALICE": {}, "BOB": {}, "CHARLIE": {},
        }))
        # 仅给 ADMIN、alice（绑 ALICE）、bob（绑 BOB）开通账号；CHARLIE 未开账号
        U.add("admin", "admin", "cli", person="ADMIN")
        self.adm_tok = U.token_add("admin", "t-adm", ["admin"])[0]
        U.add("alice", "user", "admin", person="ALICE")
        self.alice_tok = U.token_add("alice", "t-alice", ["read", "project"])[0]
        U.add("bob", "user", "admin", person="BOB")
        self.bob_tok = U.token_add("bob", "t-bob", ["read", "project"])[0]
        # 标记本机为主服务
        LS.set_marker(True, "测试主")

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_auto_onboard_and_readonly_for_unregistered(self):
        """已开账号自动接入本人调度，未开账号严格只读，与已有 [[hosts]] 地址冲突的不覆盖。"""
        hs = {h["id"]: h for h in C.active_hosts()}
        self.assertIn("fixed-a", hs)
        self.assertIn("srv-alice", hs)
        # 未开账号的 CHARLIE 绝不接入
        self.assertNotIn("srv-charlie", hs)
        # 与 fixed-a 同 ssh:port 的 srv-dup-addr 尊重 [[hosts]] 不重复生成
        self.assertNotIn("srv-dup-addr", hs)

        sa = hs["srv-alice"]
        self.assertTrue(sa.get("member_auto"))
        self.assertEqual(sa.get("users"), ["alice"])
        self.assertEqual(sa.get("worker"), "auto")
        self.assertEqual(sa.get("share"), "private")
        self.assertEqual(sa.get("worker_home"), "/data/labmon/labmon")
        self.assertEqual(sa.get("lease_days"), 7)
        # 安全护栏：不继承 ka_bin，不伪装成 [[hosts]] 自用机
        self.assertNotIn("ka_bin", sa)
        self.assertFalse(sa.get("platform_job"))
        self.assertFalse(sa.get("platform_pool"))

        # worker_env 下发 LABMON_NO_KA=1、LABMON_RESTRICT_PROJECTS=1 且不含 KA_BIN
        env_str = W.worker_env(sa)
        self.assertIn("LABMON_NO_KA=1", env_str)
        self.assertIn("LABMON_RESTRICT_PROJECTS=1", env_str)
        self.assertNotIn("KA_BIN=", env_str)

        # 权限：仅持有人 alice 和管理员可用，bob 不可用；未自动进入共有池
        alice_id = U.Ident("alice", "user", frozenset({"read", "project"}), "token", "k1")
        bob_id = U.Ident("bob", "user", frozenset({"read", "project"}), "token", "k2")
        self.assertTrue(U.may_host(alice_id, "srv-alice"))
        self.assertFalse(U.may_host(bob_id, "srv-alice"))
        self.assertIn("srv-alice", U.access(alice_id).allowed)
        self.assertNotIn("srv-alice", U.access(bob_id).allowed)
        self.assertNotIn("srv-alice", C.pools()["shared"]["hosts"])

        # fleetboot.mine / users.listing / is_external_host / lease.homes 不误混入 member_auto
        self.assertEqual([x["id"] for x in FB.mine(F.conf())], ["fixed-a"])
        lst = U.listing()
        self.assertEqual([h["id"] for h in lst["hosts"]], ["fixed-a"])
        u_alice = next(u for u in lst["users"] if u["name"] == "alice")
        self.assertEqual(u_alice["hosts"], [])
        self.assertEqual(u_alice["member_hosts"], ["srv-alice"])
        # 管理员给 alice 分配 [[hosts]] 不会因 member_auto 报错
        self.assertIn("fixed-a", C.set_host_users("alice", ["fixed-a"], "admin"))
        self.assertFalse(U.is_external_host(sa))
        homes = LS.homes(C.active_hosts())
        self.assertEqual([h["id"] for h in homes["/data/labmon/labmon"]], ["fixed-a"])

    def test_opt_out_and_rejoin_lifecycle(self):
        """单台退出切回仅监控停 worker 并撤出受管列表；重接删 manage 键、清冷却，但绝不在开关请求内推送/替换 worker 代码。"""
        stopped = []
        res_off = F.set_member_manage(
            "srv-alice", False, "alice",
            stop_fn=lambda hid: (stopped.append(hid) or (0, "已停")),
        )
        self.assertFalse(res_off["on"])
        self.assertTrue(res_off["manage"]["opt_out"])
        self.assertEqual(stopped, ["srv-alice"])
        self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})
        self.assertIn("manage = false", Path(os.environ["LABMON_HOSTS"]).read_text())

        # 重新接入调度：绝不调用 W.run_out("start", ...) 推送代码，仅改状态、清 relaunch 冷却并清远端 stopped 交由后台主服务拉起
        save_json(state_dir() / "relaunch.json", {"srv-alice": {"t": time.time(), "rc": 3, "err": "上次失败"}})
        cleared = []
        with mock.patch.object(W, "run_out", side_effect=AssertionError("切开关绝不该调用 run_out 推送代码")):
            res_on = F.set_member_manage(
                "srv-alice", True, "alice",
                clear_stopped_fn=lambda hid: (cleared.append(hid) or (True, "ok")),
            )
        self.assertTrue(res_on["on"])
        self.assertTrue(res_on["manage"]["on"])
        self.assertEqual(cleared, ["srv-alice"])
        self.assertIn("srv-alice", {h["id"] for h in C.active_hosts()})
        self.assertNotIn("manage =", Path(os.environ["LABMON_HOSTS"]).read_text())
        self.assertNotIn("srv-alice", load_json(state_dir() / "relaunch.json", {}))

        # 未开账号的 CHARLIE 拒绝开启
        with self.assertRaises(C.ConfigError):
            F.set_member_manage("srv-charlie", True, "admin")
        # 地址与 [[hosts]] 冲突的拒绝切换
        with self.assertRaises(C.ConfigError):
            F.set_member_manage("srv-dup-addr", False, "admin")

    def test_relaunch_and_container_restart_follow(self):
        """主服务自动拉起 member_auto worker（默认 deploy=if_missing 不覆盖已有代码）；auto:* 停机可恢复，手动 stop 仍不自动拉起；容器重启清冷却跟随拉起。"""
        snap = {
            "hosts": [
                {"id": "srv-alice", "label": "10（ALICE）", "auto": True, "member_auto": True,
                 "error": None, "status_error": None, "stopped": None, "status": None, "worker_up": False},
            ]
        }
        calls = []
        msgs = O.relaunch(snap, start=lambda hid: (calls.append(hid) or (0, "worker：运行中")))
        self.assertEqual(calls, ["srv-alice"])
        self.assertTrue(any("已重新拉起" in m for m in msgs))

        # 冷却期内不重复拉起
        calls.clear()
        O.relaunch(snap, start=lambda hid: (calls.append(hid) or (0, "worker：运行中")))
        self.assertEqual(calls, [])

        # 容器重启（overview.health container_step 检测到 started 变化）自动清冷却，立刻跟随拉起
        hcfg = {h["id"]: h for h in C.active_hosts()}
        now = time.time()
        e1 = {"id": "srv-alice", "label": "10（ALICE）", "error": None, "lan_off": False,
              "status": {"info": {"started": now - 3600}}}
        O.health([e1], hcfg, now)
        e2 = {"id": "srv-alice", "label": "10（ALICE）", "error": None, "lan_off": False,
              "status": {"info": {"started": now - 10}}}
        O.health([e2], hcfg, now + 60)
        O.relaunch(snap, start=lambda hid: (calls.append(hid) or (0, "worker：运行中")))
        self.assertEqual(calls, ["srv-alice"])

        # 历史借用归还/旧版停机留下的纯时间戳 stopped 不再锁死 member_auto（但对普通自用主机仍阻断兼容旧版 worker stop）
        O.clear_relaunch("srv-alice")
        calls.clear()
        snap_legacy = {"hosts": [
            {**snap["hosts"][0], "stopped": "2026-11-27 06:31:36"},
            {"id": "fixed-a", "label": "固定机 A", "auto": True, "member_auto": False,
             "error": None, "status_error": None, "stopped": "2026-11-27 06:31:36", "status": None, "worker_up": False},
        ]}
        O.relaunch(snap_legacy, start=lambda hid: (calls.append(hid) or (0, "worker：运行中")))
        self.assertEqual(calls, ["srv-alice"])

        # 显式手动 labmon worker stop 写下的 manual:<ts> 仍阻止 member_auto 自动拉起
        O.clear_relaunch("srv-alice")
        calls.clear()
        snap_manual = {"hosts": [{**snap["hosts"][0], "stopped": "manual:2026-11-27 12:00:00"}]}
        O.relaunch(snap_manual, start=lambda hid: (calls.append(hid) or (0, "worker：运行中")))
        self.assertEqual(calls, [])

        # 拉起失败（非 0 退出码）时，relaunch.json 记录失败原因，member_manage_state / fleetview.view 精确反馈而非死写“等待自动拉起”
        O.clear_relaunch("srv-alice")
        O.relaunch(snap, start=lambda hid: (3, "worker 未运行\n---- worker.log 末尾\nPermissionError: /data"))
        v_fail = FV.view(hosts=[{**snap["hosts"][0], "stopped": None}])
        m_fail = next(s["manage"] for s in v_fail["servers"] if s["id"] == "srv-alice")
        self.assertEqual(m_fail["state"], "relaunch_failed")
        self.assertIn("退出码 3", m_fail["why"])
        self.assertIn("PermissionError", m_fail["why"])

        # 手动停用（manual:）或 SSH 连不上时，manage.why 与 relaunch_state 一致反馈真实阻断原因
        v_man = FV.view(hosts=[{**snap["hosts"][0], "stopped": "manual:2026-11-27 12:00:00"}])
        m_man = next(s["manage"] for s in v_man["servers"] if s["id"] == "srv-alice")
        self.assertEqual(m_man["state"], "manual_stopped")
        self.assertTrue(m_man["blocked"])
        self.assertIn("手动停用", m_man["why"])
        self.assertNotIn("等待自动拉起", m_man["why"])

        v_err = FV.view(hosts=[{**snap["hosts"][0], "error": "ssh 超时"}])
        m_err = next(s["manage"] for s in v_err["servers"] if s["id"] == "srv-alice")
        self.assertEqual(m_err["state"], "ssh_error")
        self.assertTrue(m_err["blocked"])
        self.assertIn("ssh 超时", m_err["why"])

        # 生命周期停机（auto:account_removed / auto:monitor / auto:borrow_return）在重新接入后允许自动拉起
        O.clear_relaunch("srv-alice")
        calls.clear()
        snap_auto = {"hosts": [{**snap["hosts"][0], "stopped": "auto:account_removed 2026-11-27 12:00:00"}]}
        O.relaunch(snap_auto, start=lambda hid: (calls.append(hid) or (0, "worker：运行中")))
        self.assertEqual(calls, ["srv-alice"])

        # 默认 start 路径对 member_auto 走 deploy="if_missing"，不覆盖共享盘已有代码、不 --replace
        O.clear_relaunch("srv-alice")
        with mock.patch.object(O, "run_out", return_value=(0, "worker：运行中")) as m_run:
            O.relaunch(snap_auto)
        m_run.assert_called_once_with("start", "srv-alice", [], deploy="if_missing")

        # 实测 workerctl.run_out("start", ..., deploy="if_missing") 远端脚本在已有 labmon_worker.py 时不解包、不 --replace
        captured = {}
        def _fake_ssh(h, remote, stdin="", timeout=None):
            captured["remote"] = remote
            return 0, "worker：运行中 PID 123"
        with mock.patch.object(W, "ssh", side_effect=_fake_ssh), \
             mock.patch.object(W, "worker_bundle", return_value=("AA==", ["core.py"])):
            rc, _ = W.run_out("start", "srv-alice", [], deploy="if_missing")
        self.assertEqual(rc, 0)
        self.assertIn("if [ ! -f ", captured["remote"])
        self.assertNotIn("--replace", captured["remote"])

    def test_holder_swap_borrow_and_user_remove(self):
        """换人清 manage、借用互斥与归还隔离借用方遗留队列、删账号写 auto:account_removed 且重开账号可自动恢复。"""
        # 1. 设 manage = false 后换人给 BOB（有账号）：清 manage 后 BOB 自动接入
        F.set_member_manage("srv-alice", False, "alice", stop_fn=lambda hid: (0, "ok"))
        self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})
        C.set_fleet_holder("srv-alice", "BOB")
        hs = {h["id"]: h for h in C.active_hosts()}
        self.assertIn("srv-alice", hs)
        self.assertEqual(hs["srv-alice"]["users"], ["bob"])

        # 换人给未开账号的 CHARLIE：立刻撤出受管列表保持只读
        C.set_fleet_holder("srv-alice", "CHARLIE")
        self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})

        # 换回 ALICE，准备本地假共享盘验证借用归还时借用方队列被清理、持有人原有队列保留
        C.set_fleet_holder("srv-alice", "ALICE")
        from labmon import projects as PJ
        for pid, own in (("p-alice", "alice"), ("p-admin", "admin")):
            pdir = PJ.root() / pid
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "monitor.toml").write_text(f'id = "{pid}"\ntitle = "{pid}"\n')
            save_json(pdir / "meta.json", {"owner": own, "since": 1})
        fake_home = self.root / "shared_home"
        qdir = fake_home / "hosts" / "srv-alice" / "queues"
        qdir.mkdir(parents=True)
        (qdir / "borrower.json").write_text(json.dumps({"root": "/tmp/b", "jobs": "jobs.txt", "project": "p-admin"}))
        (qdir / "holder.json").write_text(json.dumps({"root": "/tmp/a", "jobs": "jobs.txt", "project": "p-alice"}))
        (fake_home / "hosts" / "srv-alice" / "sched.json").write_text(json.dumps({"weight": {"p-admin": 2}}))
        # 更新 hosts.toml 的 worker_home 指向 fake_home 以便本地执行清理命令
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text(hf.read_text().replace('worker_home = "/data/labmon/labmon"', f'worker_home = "{fake_home}"'))

        C.set_fleet_keys({"borrow_since": C.fmt_time(time.time(), year=True), "borrow_by": "ADMIN"}, "借用", sid="srv-alice")
        hs = {h["id"]: h for h in C.active_hosts()}
        self.assertTrue(hs["srv-alice"].get("borrow"))
        self.assertFalse(hs["srv-alice"].get("member_auto"))
        with self.assertRaises(C.ConfigError):
            F.set_member_manage("srv-alice", False, "alice")

        # 借用在线归还：实际跑远程清理子句，验证借用方队列与旧 sched.json 被删、持有人队列保留、stopped 写成 auto:borrow_return
        import subprocess
        save_json(state_dir() / "relaunch.json", {"srv-alice": time.time()})
        ssh_cmds = []
        def _local_ssh(s, cmd):
            ssh_cmds.append(cmd)
            if "qclean=ok:" in cmd:
                r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
                return r.returncode, r.stdout, r.stderr
            return 0, "0\n", ""

        B.finish(
            "srv-alice",
            ssh=_local_ssh,
            ka=lambda s, cfg, by: ("ok", "started"),
            probe=lambda sid: (None, None),
            adopt=lambda s, cfg, since, t, mine: ("no", ""),
        )
        hs = {h["id"]: h for h in C.active_hosts()}
        self.assertTrue(hs["srv-alice"].get("member_auto"))
        self.assertNotIn("srv-alice", load_json(state_dir() / "relaunch.json", {}))
        self.assertFalse((qdir / "borrower.json").exists())
        self.assertTrue((qdir / "holder.json").exists())
        self.assertFalse((fake_home / "hosts" / "srv-alice" / "sched.json").exists())
        stopped_txt = (fake_home / "hosts" / "srv-alice" / "stopped").read_text().strip()
        self.assertTrue(stopped_txt.startswith("auto:borrow_return "), stopped_txt)

        # 删账号（在线 worker）：写入 auto:account_removed 标记并撤出受管列表；重新开通同人账号后清冷却且 relaunch 可自动恢复
        save_json(state_dir() / "snapshot.json", {"hosts": [{"id": "srv-alice", "worker_up": True}]})
        save_json(state_dir() / "relaunch.json", {"srv-alice": time.time()})
        stop_cmds = []
        def _local_stop_ssh(h, cmd, stdin="", timeout=None):
            stop_cmds.append(cmd)
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
            return r.returncode, r.stdout + r.stderr
        with mock.patch.object(W, "ssh", side_effect=_local_stop_ssh):
            U.remove("alice", "admin")
        self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})
        self.assertTrue(stop_cmds)
        stopped_after_rm = (fake_home / "hosts" / "srv-alice" / "stopped").read_text().strip()
        self.assertTrue(stopped_after_rm.startswith("auto:account_removed "), stopped_after_rm)

        # 重新开通同人账号：清除 relaunch 冷却，下一轮 relaunch 识别 auto:account_removed 并自动恢复拉起
        U.add("alice", "user", "admin", person="ALICE")
        self.assertIn("srv-alice", {h["id"] for h in C.active_hosts()})
        self.assertNotIn("srv-alice", load_json(state_dir() / "relaunch.json", {}))
        restarted = []
        O.relaunch(
            {"hosts": [{"id": "srv-alice", "label": "10（ALICE）", "auto": True, "member_auto": True,
                        "error": None, "status_error": None, "stopped": stopped_after_rm, "status": None, "worker_up": False}]},
            start=lambda hid: (restarted.append(hid) or (0, "worker：运行中")),
        )
        self.assertEqual(restarted, ["srv-alice"])

    def test_fleetview_and_user_self_fleet_projection(self):
        """fleetview.view 与普通用户 _self_fleet 正确透出 manage 状态。"""
        v = FV.view(hosts=[{"id": "srv-alice", "worker_up": True, "age": 5, "status": {"t": time.time()}}])
        by_id = {s["id"]: s for s in v["servers"]}
        self.assertTrue(by_id["srv-alice"]["manage"]["on"])
        self.assertTrue(by_id["srv-alice"]["manage"]["can_toggle"])
        self.assertFalse(by_id["srv-charlie"]["manage"]["on"])
        self.assertFalse(by_id["srv-charlie"]["manage"]["can_toggle"])
        self.assertTrue(by_id["srv-dup-addr"]["manage"]["hosted"])

        alice_id = U.Ident("alice", "user", frozenset({"read", "project"}), "token", "k1")
        snap = {"generated": time.time(), "lan": {"ok": True}, "hosts": [], "projects": [], "fleet": v}
        uv = U.view(alice_id, snap)
        self.assertEqual([s["id"] for s in uv["fleet"]["servers"]], ["srv-alice", "srv-dup-addr"])
        self.assertTrue(uv["fleet"]["servers"][0]["manage"]["on"])

    def test_http_api_auth_and_standby(self):
        """POST /api/fleet/manage 权限控制：本人允许、他人越权 403、管理员公网无许可 403、备用 409。"""
        srv = S.Server(("127.0.0.1", 0))
        pub = S.Listener(("127.0.0.1", 0), srv)
        t1 = threading.Thread(target=srv.serve_forever, daemon=True)
        t2 = threading.Thread(target=pub.serve_forever, daemon=True)
        t1.start()
        t2.start()
        lan_url = f"http://127.0.0.1:{srv.server_address[1]}"
        pub_url = f"http://127.0.0.1:{pub.server_address[1]}"
        def _post(url: str, body: dict, opener) -> tuple[int, dict]:
            import urllib.error
            try:
                with opener.open(post_req(url, json.dumps(body).encode("utf-8"))) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))

        try:
            with mock.patch.object(W, "stop_managed_worker", return_value=(0, "ok")) as m_stop, \
                 mock.patch.object(W, "clear_stopped", return_value=(True, "ok")) as m_clear, \
                 mock.patch.object(W, "run_out", side_effect=AssertionError("POST /api/fleet/manage 绝不能调用 run_out 推送代码")):
                # 1. 非持有人 bob 越权切 srv-alice → 403
                op_bob = bearer_opener(self.bob_tok)
                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_bob)
                self.assertEqual(code, 403)

                # 2. 持有人 alice 本人可切换退出与重接 → 200（且重接调用 clear_stopped、绝不触发 run_out 代码部署）
                op_alice = bearer_opener(self.alice_tok)
                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_alice)
                self.assertEqual(code, 200)
                self.assertFalse(data["on"])
                self.assertEqual(m_stop.call_count, 1)
                self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})

                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": True}, op_alice)
                self.assertEqual(code, 200)
                self.assertTrue(data["on"])
                self.assertEqual(m_clear.call_count, 1)
                self.assertIn("srv-alice", {h["id"] for h in C.active_hosts()})

                # 3. 管理员从公网无 public_ops 许可 → 403；开启 public_ops 后公网允许（仍不触发代码部署）；内网管理员 → 200
                op_adm = bearer_opener(self.adm_tok)
                code, _ = _post(f"{pub_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_adm)
                self.assertEqual(code, 403)
                U.set_public_ops("admin", True, "admin")
                code, data = _post(f"{pub_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_adm)
                self.assertEqual(code, 200)
                self.assertFalse(data["on"])
                code, data = _post(f"{pub_url}/api/fleet/manage", {"id": "srv-alice", "on": True}, op_adm)
                self.assertEqual(code, 200)
                self.assertTrue(data["on"])

                # 4. 备用服务 → 409 standby
                LS.set_marker(False, "测试备用")
                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_alice)
                self.assertEqual(code, 409)
                self.assertTrue(data.get("standby"))
        finally:
            srv.shutdown()
            pub.shutdown()
            srv.server_close()
            pub.server_close()


class WorkerNoKeepaliveGuardTest(unittest.TestCase):
    """安全护栏：LABMON_NO_KA=1 时不动用户自开保活，已有实验与用户保活识别为占用不强行抢卡。"""

    def test_no_ka_never_touches_ka_sh_and_marks_all_external_as_other(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ka_bin = root / "ka_bin"
            home.mkdir()
            ka_bin.mkdir()
            # 构造假 ka.env 与 ka.sh（正常模式会识别并启停，LABMON_NO_KA=1 绝不能触碰）
            (ka_bin / "ka.env").write_text(f"AGRI_ROOT={root}/demo\nAGRI_KA_DIR={root}/demo\nAGRI_KA_SCRIPT=keep_alive.py\n")
            (home / "ka.sh").write_text("#!/bin/sh\nexit 99\n")
            os.chmod(home / "ka.sh", 0o755)

            old_no_ka = os.environ.get("LABMON_NO_KA")
            os.environ["LABMON_NO_KA"] = "1"
            try:
                wm = load_worker(dict(no_host(root), LABMON_HOME=str(home), KA_BIN=str(ka_bin),
                                      LABMON_SLOTS=str(root / "slots"), LABMON_NO_KA="1"))
                w = wm.Worker("srv-alice")
                self.assertTrue(w.no_ka)
                self.assertIsNone(w.ka_reg)
                self.assertIsNone(w.ka_ev)
                self.assertIsNone(w.ka_full)
                self.assertIn("不接管保活", w.ka_why or "")
                # 无论 need_gpu True/False，keepalive() 绝不调用 ka()
                with mock.patch.object(w, "ka", side_effect=AssertionError("绝不该调用 ka.sh")):
                    w.keepalive(True)
                    w.keepalive(False)

                # gpu_owners(..., no_ka=True)：用户已有实验与用户自开 keep_alive 均标为 owner="other", ka=False
                procs = {
                    100: {"hpid": 1000, "comm": "python"},
                    200: {"hpid": 2000, "comm": "python"},
                }
                apps = [
                    (1000, 12000.0, "GPU-0"),
                    (2000, 4000.0, "GPU-1"),
                ]
                with mock.patch.object(wm.keepalive, "proc_args", side_effect=lambda pid: (
                    (["python", "train.py"], "/root/work") if pid == 100 else (["python", "/root/keep_alive.py"], "/root")
                )):
                    gp = wm.gpu_owners(apps, procs, {}, None, no_ka=True)
                self.assertEqual([g["owner"] for g in gp], ["other", "other"])
                self.assertEqual([g["ka"] for g in gp], [False, False])

                # 准入 place()：独占卡请求面对用户已有实验或用户自开保活均识别为被占用、不抢卡
                S_snap = {
                    "cards": [
                        {"i": 0, "uuid": "GPU-0", "total": 24000.0, "free": 12000.0, "keep": 0.0, "other": 12000.0, "ka": False},
                        {"i": 1, "uuid": "GPU-1", "total": 24000.0, "free": 20000.0, "keep": 0.0, "other": 4000.0, "ka": False},
                    ],
                    "ngpus": 2, "limit": 64.0, "room": 50.0, "margin": 8.0, "cpu_cap": 8, "cpu_room": 8.0,
                    "jobs": [], "ext": [], "gp": gp, "cpu": 0.5, "holds": {}, "tags": {}, "owner": set(),
                    "who": "labmon 之外的进程", "kept": False, "cut": None, "brief": False,
                    "gholds": {}, "gangs": {}, "gfirst": {},
                }
                req = {
                    "key": "q1", "lane": "q1", "project": "p1", "name": "j1", "ngpu": 1, "vram": 4.0, "ram": 4.0,
                    "cpus": 2, "src": "提示值", "ram_src": "提示值", "cpu_src": "提示值",
                    "excl": "gpu", "tag": None, "hold": False, "allowed": None, "dur": 10.0, "cards": None,
                }
                cards, why, _ = wm.place(req, S_snap, time.time())
                self.assertIsNone(cards)
                self.assertIn("在用卡", why or "")
            finally:
                if old_no_ka is None:
                    os.environ.pop("LABMON_NO_KA", None)
                else:
                    os.environ["LABMON_NO_KA"] = old_no_ka

    def test_worker_restrict_projects_blocks_foreign_queues_and_slots(self):
        """Worker 侧硬隔离：LABMON_RESTRICT_PROJECTS=1 在未收到 sched.json 前 fail-closed，收到 allowed_projects 后仅放行持有人项目、跳过借用方遗留队列并拦截越权 slot。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            qdir = home / "hosts" / "srv-alice" / "queues"
            qdir.mkdir(parents=True)
            # 准备两条真实清单：借用方 p-admin 与持有人 p-alice
            for nm in ("a", "b"):
                d = root / nm
                d.mkdir()
                (d / "jobs.txt").write_text("j1|echo ok\n")
            (qdir / "borrower.json").write_text(json.dumps({"root": str(root / "b"), "jobs": "jobs.txt", "project": "p-admin"}))
            (qdir / "holder.json").write_text(json.dumps({"root": str(root / "a"), "jobs": "jobs.txt", "project": "p-alice"}))

            old_rp = os.environ.get("LABMON_RESTRICT_PROJECTS")
            os.environ["LABMON_RESTRICT_PROJECTS"] = "1"
            try:
                wm = load_worker(dict(no_host(root), LABMON_HOME=str(home),
                                      LABMON_SLOTS=str(root / "slots"), LABMON_RESTRICT_PROJECTS="1"))
                w = wm.Worker("srv-alice")
                # 1. 尚无 sched.json：fail-closed，任何遗留队列与 slot 均不放行
                self.assertEqual(w.allowed_projects(), set())
                self.assertEqual(w.registry(), [])
                hit = w.override_hit({"project": "p-admin", "name": "s1", "kind": "slot"})
                self.assertIsNotNone(hit)
                self.assertEqual(hit[0], "owner")

                # 2. 看板下发 allowed_projects=["p-alice"]：仅加载 p-alice 队列，彻底跳过 p-admin 遗留队列与 slot
                (home / "hosts" / "srv-alice" / "sched.json").write_text(json.dumps({"allowed_projects": ["p-alice"]}))
                qs = w.registry()
                self.assertEqual([q["project"] for q in qs], ["p-alice"])
                self.assertIsNone(w.override_hit({"project": "p-alice", "name": "s2", "kind": "slot"}))
                self.assertEqual(w.override_hit({"project": "p-admin", "name": "s1", "kind": "slot"})[0], "owner")

                # 3. worker.offers() 同样硬过滤非本人项目，绝不为他人项目生成挪法建议或 why（#236）
                peer_dir = home / "hosts" / "fixed-a"
                peer_dir.mkdir(parents=True)
                now = time.time()
                (peer_dir / "status.json").write_text(json.dumps({
                    "t": now, "running": [], "waiting": [], "queues": [],
                    "todo": [{"project": "p-admin", "root": str(root / "b"), "queue": "jobs.txt", "jobs": ["j1"]}],
                    "plan": {"lanes": [{"kind": "queue", "project": "p-admin", "root": str(root / "b"), "queue": "jobs.txt",
                                        "jobs": [{"name": "j1", "t0": now + 100, "t1": now + 7200}]}]},
                }))
                w.peers(now)
                offs = w.offers([], {"cards": [], "ngpus": 0, "limit": 64.0, "room": 50.0, "margin": 8.0, "cpu_cap": 8, "cpu_room": 8.0,
                                     "jobs": [], "ext": [], "gp": [], "cpu": 0.0, "holds": {}, "tags": {}, "owner": set(),
                                     "who": "labmon 之外的进程", "kept": False, "cut": None, "brief": False,
                                     "gholds": {}, "gangs": {}, "gfirst": {}}, now, w.policy(), set(), {}, {})
                self.assertEqual([o["project"] for o in offs], [])
            finally:
                if old_rp is None:
                    os.environ.pop("LABMON_RESTRICT_PROJECTS", None)
                else:
                    os.environ["LABMON_RESTRICT_PROJECTS"] = old_rp


class MemberAutoIsolationAndStoppedTest(unittest.TestCase):
    """Issue #236：彻底隔离他人 member_auto 主机（视图/HTTP/API/CLI/rebalance/assign/reg_move）及历史 stopped 清理回归测试。"""

    def setUp(self):
        no_git_env()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_env = {k: os.environ.get(k) for k in ("LABMON_STATE", "LABMON_HOSTS", "LABMON_LAN")}
        os.environ["LABMON_STATE"] = str(self.root / "state")
        os.environ["LABMON_HOSTS"] = str(self.root / "hosts.toml")
        os.environ["LABMON_LAN"] = "on"
        self.fake_home = self.root / "shared_home"
        self.fake_home.mkdir()
        extra_admin_srv = """
[[fleet.servers]]
id = "srv-admin-ma"
holder = "ADMIN"
ssh = "root@192.0.2.12"
port = 15112
pool = "pool-public"
"""
        Path(os.environ["LABMON_HOSTS"]).write_text(
            HOSTS_TOML.strip().replace('worker_home = "/data/labmon/labmon"', f'worker_home = "{self.fake_home}"')
            + "\n" + extra_admin_srv.strip() + "\n"
        )
        F.edit_state("people.json", lambda d: d.update({"ADMIN": {}, "ALICE": {}, "BOB": {}}))
        U.add("admin", "admin", "cli", person="ADMIN")
        self.adm_tok = U.token_add("admin", "t-adm", ["admin"])[0]
        U.add("alice", "user", "admin", person="ALICE")
        self.alice_tok = U.token_add("alice", "t-alice", ["read", "project"])[0]
        U.add("bob", "user", "admin", person="BOB")
        LS.set_marker(True, "测试主")
        from labmon import projects as PJ
        for pid, own in (("p-admin", "admin"), ("p-alice", "alice")):
            pdir = PJ.root() / pid
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "monitor.toml").write_text(f'id = "{pid}"\ntitle = "{pid}"\n')
            save_json(pdir / "meta.json", {"owner": own, "since": 1})
        self.admin_id = U.Ident("admin", "admin", frozenset({"admin", "read", "project"}), "token", "k-adm")
        self.alice_id = U.Ident("alice", "user", frozenset({"read", "project"}), "token", "k-alice")

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_admin_isolation_across_config_view_api_assign_reg_move_rebalance(self):
        """管理员默认隔离他人 member_auto（srv-alice），保留管理员本人 member_auto（srv-admin-ma）与自用机（fixed-a）；组员本人照常可见可用。"""
        from labmon import api as A
        from labmon import rebalance as RB

        # 1. config 唯一判定：member_owner / is_other_member_auto / own_hosts / home_host 优先非 member_auto
        hs = {h["id"]: h for h in C.active_hosts()}
        self.assertEqual(C.member_owner(hs["srv-alice"]), "alice")
        self.assertEqual(C.member_owner(hs["srv-admin-ma"]), "admin")
        self.assertIsNone(C.member_owner(hs["fixed-a"]))
        self.assertTrue(C.is_other_member_auto(hs["srv-alice"], self.admin_id))
        self.assertFalse(C.is_other_member_auto(hs["srv-admin-ma"], self.admin_id))
        self.assertFalse(C.is_other_member_auto(hs["srv-alice"], self.alice_id))
        self.assertTrue(C.is_other_member_auto(hs["srv-admin-ma"], None))
        self.assertEqual([h["id"] for h in C.own_hosts(self.admin_id)], ["fixed-a", "srv-admin-ma"])
        self.assertEqual([h["id"] for h in C.own_hosts(None)], ["fixed-a"])
        # home_host 即使把 member_auto 排前面，也优先选非 member_auto 代办
        self.assertEqual(C.home_host(str(self.fake_home), [hs["srv-alice"], hs["fixed-a"]])["id"], "fixed-a")

        # 2. users.view：管理员快照 hosts/occ/regs 排除 srv-alice，保留 fixed-a/srv-admin-ma 及全量 fleet；alice 本人仍看到 srv-alice
        snap = {
            "generated": time.time(), "lan": {"ok": True},
            "hosts": [
                {"id": "fixed-a", "label": "固定机 A", "member_auto": False, "member_account": None, "worker_up": True, "status": {"t": time.time()}},
                {"id": "srv-alice", "label": "10（ALICE）", "member_auto": True, "member_account": "alice", "worker_up": True, "status": {"t": time.time()}},
                {"id": "srv-admin-ma", "label": "12（ADMIN）", "member_auto": True, "member_account": "admin", "worker_up": True, "status": {"t": time.time()}},
            ],
            "occ": {"fixed-a": [{"n": "j1"}], "srv-alice": [{"n": "ja"}], "srv-admin-ma": [{"n": "jm"}]},
            "regs": [
                {"host": "fixed-a", "state": "active", "queues": []},
                {"host": "srv-alice", "state": "active", "queues": []},
                {"host": "srv-admin-ma", "state": "active", "queues": []},
            ],
            "projects": [{"id": "p-admin"}, {"id": "p-alice"}],
            "fleet": FV.view(hosts=[{"id": "srv-alice", "worker_up": True, "status": {"t": time.time()}}]),
        }
        av = U.view(self.admin_id, snap)
        self.assertEqual([h["id"] for h in av["hosts"]], ["fixed-a", "srv-admin-ma"])
        self.assertEqual(sorted(av["occ"]), ["fixed-a", "srv-admin-ma"])
        self.assertEqual([r["host"] for r in av["regs"]], ["fixed-a", "srv-admin-ma"])
        self.assertIn("srv-alice", {s["id"] for s in av["fleet"]["servers"]})

        uv = U.view(self.alice_id, snap)
        self.assertEqual([h["id"] for h in uv["hosts"]], ["srv-alice"])
        self.assertEqual(sorted(uv["occ"]), ["srv-alice"])

        # 3. api.hosts / home_text / fit --list / worker add / override / host_assign / reg_move
        ah = A.hosts(who=self.admin_id)
        self.assertEqual([h["id"] for h in ah["hosts"]], ["fixed-a", "srv-admin-ma"])
        self.assertNotIn("srv-alice", next(h["home_with"] for h in ah["hosts"] if h["id"] == "fixed-a"))
        self.assertNotIn("srv-alice", A.home_text("fixed-a", who=self.admin_id))

        with mock.patch.object(W, "run_out", return_value=(0, "ok")) as m_fit:
            A.worker("fit", "fixed-a", ["/tmp", "jobs.txt"], project="p-admin", lst=True, who=self.admin_id)
        self.assertNotIn("srv-alice", m_fit.call_args.kwargs["hosts"])
        self.assertIn("srv-admin-ma", m_fit.call_args.kwargs["hosts"])

        # 管理员不能往他人 member_auto 执行 worker add / host_assign / reg_move
        r_add = A.worker("add", "srv-alice", ["/tmp", "jobs.txt"], project="p-admin", who=self.admin_id)
        self.assertEqual(r_add["rc"], 2)
        self.assertIn("个人服务器", r_add["err"])
        with self.assertRaises(C.ConfigError):
            S.host_assign("srv-alice", "p-admin", who=self.admin_id)
        with self.assertRaises(C.ConfigError):
            S.reg_move("srv-alice", [{"root": "/tmp", "jobs": "jobs.txt", "project": "p-admin", "host": "fixed-a"}], who=self.admin_id)

        # 组员本人给自己的 srv-alice 执行 host_assign 照常放行
        ok_msg = S.host_assign("srv-alice", "p-alice", who=self.alice_id)
        self.assertIn("已通知 p-alice", ok_msg)

        # 管理员不带 --host 的 pause --now 不连他人 member_auto
        with mock.patch.object(W, "override_each", return_value=[]) as m_ovr:
            A.override("pause", "p-admin", None, None, True, by="admin", who=self.admin_id)
        self.assertNotIn("srv-alice", m_ovr.call_args.args[3])

        # 4. rebalance.suggest 过滤他人 member_auto 的 offers，绝不把 srv-alice 推荐给 p-admin
        now = time.time()
        rb_hosts = [
            {"id": "srv-alice", "label": "10（ALICE）", "member_auto": True, "member_account": "alice", "worker_up": True,
             "lan_off": False, "stopped": None, "status": {"offers": [
                 {"project": "p-admin", "lane": None, "free": now, "ends": {"fixed-a|/r|q": now + 7200},
                  "jobs": [{"host": "fixed-a", "root": "/r", "queue": "q", "name": "j1", "there": now + 7200, "rest": now + 100, "end": now + 600, "own": now}],
                  "why": None, "stop": None, "idle": True},
                 {"project": "p-alice", "lane": None, "free": now, "ends": {"fixed-a|/r|q2": now + 7200},
                  "jobs": [{"host": "fixed-a", "root": "/r", "queue": "q2", "name": "ja", "there": now + 7200, "rest": now + 100, "end": now + 600, "own": now}],
                  "why": None, "stop": None, "idle": True},
             ]}},
        ]
        sug = RB.suggest(rb_hosts, {}, now)
        self.assertNotIn("p-admin", sug)
        self.assertIn("p-alice", sug)

    def test_http_endpoints_isolate_other_member_auto_and_clear_stopped_on_manage(self):
        """HTTP GET /、/overview.json、/api/hosts、POST /collect 对管理员排除他人 member_auto；workerctl stop 写 manual: 且 clear_stopped 实际删文件。"""
        import subprocess
        from labmon import collect as CO

        # 构造真实磁盘快照与 index.html（含 srv-alice）
        now = time.time()
        snap = {
            "generated": now, "lan": {"ok": True}, "notices": [], "wake": [], "event_kinds": {}, "event_cats": [],
            "hosts": [
                {"id": "fixed-a", "label": "固定机 A", "member_auto": False, "member_account": None, "worker_up": True, "status": {"t": now, "running": [], "waiting": [], "external": []}},
                {"id": "srv-alice", "label": "10（ALICE）", "member_auto": True, "member_account": "alice", "worker_up": True, "status": {"t": now, "running": [], "waiting": [], "external": []}},
            ],
            "occ": {"fixed-a": [], "srv-alice": []},
            "regs": [{"host": "fixed-a", "state": "active", "queues": []}, {"host": "srv-alice", "state": "active", "queues": []}],
            "projects": [], "archived": [], "colors": {}, "rebalance": {}, "fleet": FV.view(), "disk": {}, "pool": None, "sched": None, "server": {"role": "primary"},
        }
        save_json(state_dir() / "snapshot.json", snap)
        CO.write_page("overview.html", snap, state_dir() / "index.html")

        srv = S.Server(("127.0.0.1", 0))
        t1 = threading.Thread(target=srv.serve_forever, daemon=True)
        t1.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        op_adm = bearer_opener(self.adm_tok)
        op_alice = bearer_opener(self.alice_tok)
        try:
            with op_adm.open(f"{base}/overview.json") as r:
                d_adm = json.loads(r.read().decode("utf-8"))
            self.assertEqual([h["id"] for h in d_adm["hosts"]], ["fixed-a"])

            with op_adm.open(f"{base}/") as r:
                html_adm = r.read().decode("utf-8")
            self.assertNotIn('"id": "srv-alice", "label": "10（ALICE）"', html_adm)

            with op_adm.open(f"{base}/api/hosts") as r:
                h_adm = json.loads(r.read().decode("utf-8"))
            self.assertNotIn("srv-alice", [h["id"] for h in h_adm["hosts"]])
            self.assertIn("srv-admin-ma", [h["id"] for h in h_adm["hosts"]])

            with op_alice.open(f"{base}/api/hosts") as r:
                h_alice = json.loads(r.read().decode("utf-8"))
            self.assertEqual([h["id"] for h in h_alice["hosts"]], ["srv-alice"])

            with mock.patch.object(srv.overview, "collect_now", return_value=True):
                with op_adm.open(post_req(f"{base}/collect", b"{}")) as r:
                    d_col = json.loads(r.read().decode("utf-8"))
            self.assertEqual([h["id"] for h in d_col["hosts"]], ["fixed-a"])
        finally:
            srv.shutdown()
            srv.server_close()

        # 实测 workerctl.run_out("stop", ...) 写 manual:<ts> 且 workerctl.clear_stopped 删掉它
        sf = self.fake_home / "hosts" / "srv-alice" / "stopped"
        def _local_run(h, cmd, stdin="", timeout=None):
            # 跳过真实 labmon_worker.py stop 调用，测试后续写入 manual: 与 rm -f 子句
            patched = cmd.replace("stop  --host 'srv-alice'", "true").replace("stop --host 'srv-alice'", "true")
            if "labmon_worker.py" in patched:
                patched = "true; " + patched.split("; ", 1)[1]
            r = subprocess.run(["bash", "-c", patched], capture_output=True, text=True)
            return r.returncode, r.stdout + r.stderr
        with mock.patch.object(W, "ssh", side_effect=_local_run):
            rc, _ = W.run_out("stop", "srv-alice", [])
        self.assertEqual(rc, 0)
        self.assertTrue(sf.read_text().strip().startswith("manual:"))
        self.assertTrue(C.stopped_blocks_relaunch(sf.read_text().strip(), member_auto=True))

        ok, _ = W.clear_stopped("srv-alice", ssh_fn=lambda h, cmd: (
            subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).returncode, ""
        ))
        self.assertTrue(ok)
        self.assertFalse(sf.exists())


if __name__ == "__main__":
    unittest.main()
