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

        # worker_env 下发 LABMON_NO_KA=1 且不含 KA_BIN
        env_str = W.worker_env(sa)
        self.assertIn("LABMON_NO_KA=1", env_str)
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
        """单台退出切回仅监控停 worker 并撤出受管列表；重接删 manage 键并立即拉起 worker。"""
        stopped, started = [], []
        res_off = F.set_member_manage(
            "srv-alice", False, "alice",
            stop_fn=lambda hid: (stopped.append(hid) or (0, "已停")),
        )
        self.assertFalse(res_off["on"])
        self.assertTrue(res_off["manage"]["opt_out"])
        self.assertEqual(stopped, ["srv-alice"])
        self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})
        self.assertIn("manage = false", Path(os.environ["LABMON_HOSTS"]).read_text())

        # 重新接入调度
        res_on = F.set_member_manage(
            "srv-alice", True, "alice",
            start_fn=lambda hid: (started.append(hid) or (0, "worker：运行中")),
        )
        self.assertTrue(res_on["on"])
        self.assertTrue(res_on["manage"]["on"])
        self.assertEqual(started, ["srv-alice"])
        self.assertIn("srv-alice", {h["id"] for h in C.active_hosts()})
        self.assertNotIn("manage =", Path(os.environ["LABMON_HOSTS"]).read_text())

        # 未开账号的 CHARLIE 拒绝开启
        with self.assertRaises(C.ConfigError):
            F.set_member_manage("srv-charlie", True, "admin")
        # 地址与 [[hosts]] 冲突的拒绝切换
        with self.assertRaises(C.ConfigError):
            F.set_member_manage("srv-dup-addr", False, "admin")

    def test_relaunch_and_container_restart_follow(self):
        """主服务自动拉起 member_auto worker；容器重启自动清 relaunch 冷却跟随拉起；未开账号绝不触发。"""
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

    def test_holder_swap_borrow_and_user_remove(self):
        """换人清 manage、借用互斥与归还恢复、删账号停 worker 并撤出。"""
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

        # 换回 ALICE 后测试借用互斥
        C.set_fleet_holder("srv-alice", "ALICE")
        C.set_fleet_keys({"borrow_since": C.fmt_time(time.time(), year=True), "borrow_by": "ADMIN"}, "借用", sid="srv-alice")
        hs = {h["id"]: h for h in C.active_hosts()}
        self.assertTrue(hs["srv-alice"].get("borrow"))
        self.assertFalse(hs["srv-alice"].get("member_auto"))
        with self.assertRaises(C.ConfigError):
            F.set_member_manage("srv-alice", False, "alice")

        # 借用归还后恢复为 member_auto，且清理 stopped 与 relaunch 冷却
        save_json(state_dir() / "relaunch.json", {"srv-alice": time.time()})
        ssh_cmds = []
        B.finish(
            "srv-alice",
            ssh=lambda s, cmd: (ssh_cmds.append(cmd) or (0, "0\n", "")),
            ka=lambda s, cfg, by: ("ok", "started"),
            probe=lambda sid: (None, None),
            adopt=lambda s, cfg, since, t, mine: ("no", ""),
        )
        hs = {h["id"]: h for h in C.active_hosts()}
        self.assertTrue(hs["srv-alice"].get("member_auto"))
        self.assertNotIn("srv-alice", load_json(state_dir() / "relaunch.json", {}))
        self.assertTrue(any("hosts/srv-alice/stopped" in cmd for cmd in ssh_cmds))

        # 删账号：触发停 worker 回调并撤出受管列表
        stopped_hosts = []
        U.remove("alice", "admin", stop_fn=lambda h: stopped_hosts.append(h["id"]))
        self.assertEqual(stopped_hosts, ["srv-alice"])
        self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})

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
            with mock.patch.object(W, "run_out", return_value=(0, "ok")):
                # 1. 非持有人 bob 越权切 srv-alice → 403
                op_bob = bearer_opener(self.bob_tok)
                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_bob)
                self.assertEqual(code, 403)

                # 2. 持有人 alice 本人可切换退出与重接 → 200
                op_alice = bearer_opener(self.alice_tok)
                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_alice)
                self.assertEqual(code, 200)
                self.assertFalse(data["on"])
                self.assertNotIn("srv-alice", {h["id"] for h in C.active_hosts()})

                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": True}, op_alice)
                self.assertEqual(code, 200)
                self.assertTrue(data["on"])
                self.assertIn("srv-alice", {h["id"] for h in C.active_hosts()})

                # 3. 管理员从公网无 public_ops 许可 → 403；开启 public_ops 后公网允许；内网管理员 → 200
                op_adm = bearer_opener(self.adm_tok)
                code, _ = _post(f"{pub_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_adm)
                self.assertEqual(code, 403)
                U.set_public_ops("admin", True, "admin")
                code, data = _post(f"{pub_url}/api/fleet/manage", {"id": "srv-alice", "on": False}, op_adm)
                self.assertEqual(code, 200)
                self.assertFalse(data["on"])
                code, data = _post(f"{lan_url}/api/fleet/manage", {"id": "srv-alice", "on": True}, op_adm)
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


if __name__ == "__main__":
    unittest.main()
