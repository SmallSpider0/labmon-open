"""组内服务器平台操作（fleetops.py、fleetboot.py）的离线测试：本机起一个模拟 SCM 平台，有多台服务器、别人的作业、按名字查、令牌被顶掉、
停止与提交（过几次列表才生效）、资源池与规格、存储卷、镜像仓库。ssh、探测与端口探测用替身，不连任何服务器。

    python -m unittest tests.test_fleetops -v
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import KEY, NO_HOSTS, fake_op, fake_status, fleet_cfg, rsa_decrypt  # noqa: E402
os.environ["LABMON_LAN"] = "on"                       # 假服务器连不上：别判成本机不在内网
os.environ["LABMON_HOSTS"] = NO_HOSTS

from labmon import config as C  # noqa: E402
from labmon import disk as DK  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import fleetboot as FB  # noqa: E402
from labmon import fleetops as FO  # noqa: E402
from labmon import fleetview as FV  # noqa: E402
from labmon.state import save_json  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import platform as P  # noqa: E402
from labmon import renew as R  # noqa: E402
from labmon.records import records  # noqa: E402
from labmon.state import load_rows, save_json, state_dir  # noqa: E402

PRIV, PUB = "node07-专用", "GPUs-A6000"
IP = {PRIV: "192.0.2.29", PUB: "192.0.2.27"}
PID = {PRIV: "p1", PUB: "p2"}                       # 平台上的池 id
REG = "h:8888/grp/"
RUN = "/etc/init.d/ssh restart && sleep infinity"
JSONTEXT = """{
  "workTaskRoleVOList": [
    "0": {
      "id": "r1", "taskRoleName": "taskRole1", "imageName": "reg/ubuntu:aa", "containerId": "c1", "ipAddress": "192.0.2.29",
      "port": "8081", "cpuCoreNumber": 17, "memory": 122, "hardDisk": 750, "gpuTotal": 1, "status": 1, "remark": "容器正在运行",
      "portMap": {
        "22": "15001",
      },
    },
  ],
  "envMap": {
  },
  "createTime": "2026-11-05 10:00:00",
  "poolName": "node07-专用",
  "uuidList": [
    "0": "GPU-x",
  ],
}"""


class FleetPlat:
    """模拟平台：作业有各自的端口与镜像；列表第一页只放前 page 条，按名字查才能找到后面的。"""

    def __init__(self, page=50, lag=1):
        self.codes, self.tokens, self.calls, self.page, self.lag = {}, set(), [], page, lag
        self.pending, self.n_new, self.on = [], 0, {}      # pending：[列表还要查几次, 作业, 改成的 runningCount]；on：接口 → 钩子
        self.fail, self.avail = {}, {}                    # fail：接口 → 报错信息；avail：池 → 空卡数
        self.specs, self.spec_ip = {}, {}                 # specs：池 id → 规格列表（不设时单规格 "s"）；spec_ip：规格 id → 节点 IP
        self.tags = {"grp/ubuntu": ["aa", "bb", "zz", "dd"], "grp/pt": ["1"]}
        self.jobs = [
            {"id": "j1", "taskName": "AA", "poolName": PRIV, "createTime": "2026-11-05 10:00:00", "runningCount": 1, "port": "15001", "image": REG + "ubuntu:aa"},
            {"id": "j0", "taskName": "AA", "poolName": PRIV, "createTime": "2026-10-29 10:00:00", "runningCount": 0, "port": "15001", "image": REG + "ubuntu:aa"},
            {"id": "jz", "taskName": "ZZ", "poolName": PUB, "createTime": "2026-11-04 09:00:00", "runningCount": 1, "port": "15999", "image": REG + "ubuntu:zz"},
            {"id": "jx", "taskName": "ALICE", "poolName": PRIV, "createTime": "2026-11-05 11:00:00", "runningCount": 1, "port": "15155", "image": REG + "pt:1"},
            {"id": "jh", "taskName": "HH", "poolName": PUB, "createTime": "2026-10-20 09:00:00", "runningCount": 0, "port": "15003", "image": REG + "ubuntu:hh"},
            {"id": "j2", "taskName": "BB", "poolName": PUB, "createTime": "2026-11-03 09:00:00", "runningCount": 1, "port": "15002", "image": REG + "ubuntu:bb"},
        ]
        plat = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, b, sid=None):
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                if sid:
                    self.send_header("Set-Cookie", f"sid={sid}; Path=/")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path == "/":
                    return self._out(b"<script src=/js/app.abc.js></script>")
                if u.path == "/js/app.abc.js":
                    return self._out(f'var o="{KEY[0]}";'.encode())
                self._api("GET", u, None)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._api("POST", urlsplit(self.path), json.loads(self.rfile.read(n)) if n else None)

            def _api(self, method, u, body):
                res = plat.api(method, u.path[4:], {k: v[0] for k, v in parse_qs(u.query).items()}, body, self.headers)
                sid = res.pop("_sid", None)
                self._out(json.dumps(res, ensure_ascii=False).encode(), sid)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"

    def kick(self):
        self.tokens.clear()                       # 网页上有人登录：看板的令牌失效

    def api(self, method, path, q, body, hdr):
        self.calls.append((path, q, body))
        if path in self.on:
            self.on[path]()
        if path in self.fail:
            return {"code": "-1", "msg": self.fail[path]}
        ok = lambda data=None: {"code": "0", "data": data}  # noqa: E731
        sid = next((c.split("=", 1)[1] for c in (hdr.get("Cookie") or "").split("; ") if c.startswith("sid=")), None)
        if path == "/user/captchaImage":
            sid = secrets.token_hex(4)
            self.codes[sid] = "q7Wz"
            return {**ok({"verifyCode": "q7Wz"}), "_sid": sid}
        if path == "/user/login":
            if self.codes.get(sid) != body["verifyCode"] or rsa_decrypt(KEY[1], KEY[2], body["passWord"]) != "pw":
                return {"code": "-1", "msg": "验证码错误"}
            tok = secrets.token_hex(8)
            self.tokens.add(tok)
            return ok({"token": tok, "userId": "u1"})
        if hdr.get("Authorization") not in self.tokens:
            return {"code": "-2", "msg": "令牌失效"}
        pub = lambda j: {k: v for k, v in j.items() if k not in ("port", "image", "spec", "ip")}  # noqa: E731
        if path == "/workManage/list":
            for x in self.pending:
                x[0] -= 1
                if x[0] <= 0:
                    x[1]["runningCount"] = x[2]
            self.pending = [x for x in self.pending if x[0] > 0]
            js = [j for j in self.jobs if j["taskName"] == body["taskName"]] if body["taskName"] else self.jobs[:self.page]
            return ok({"workManageVOList": [pub(j) for j in js]})
        if path.startswith("/workManage/getWorkTaskInfo/"):
            j = next(j for j in self.jobs if j["id"] == path.rsplit("/", 1)[1])
            ip = j.get("ip") or self.spec_ip.get(j.get("spec")) or IP[j["poolName"]]
            return ok({"workTaskRoleVOList": [{"id": "r-" + j["id"], "imageName": j["image"], "ipAddress": ip, "portMap": {"22": j["port"]},
                                               "port": "8081", "cpuCoreNumber": 17, "memory": 122, "hardDisk": 750, "gpuTotal": 1, "containerId": "c-" + j["id"]}]})
        if path == "/submitWork/getResourcePool/1":
            rows = []
            for p in IP:
                pid = PID[p]
                if pid in self.specs:
                    for sp in self.specs[pid]:
                        rows.append({"poolId": pid, "poolName": p, "host": self.spec_ip.get(sp["specificationId"], IP[p]),
                                     "specificationId": sp["specificationId"], "specificationName": sp["specificationName"],
                                     "quantityAvailable": sp["quantityAvailable"]})
                else:
                    rows.append({"poolId": pid, "poolName": p, "host": IP[p], "specificationId": "s", "specificationName": "1 卡", "quantityAvailable": 1})
            return ok(rows)
        if path.startswith("/submitWork/getResourceSpecification/"):
            pid = path.rsplit("/", 1)[1]
            if pid in self.specs:
                return ok([{**sp, "poolId": pid} for sp in self.specs[pid]])
            return ok([{"specificationId": "s", "specificationName": "1 卡", "poolId": pid, "quantityAvailable": self.avail.get(pid, 1),
                        "specificationAmount": 4, "isSchedule": 0, "queueNum": 0}])
        if path == "/dataManage/volumeByUserList/u1":
            return ok([{"fullPath": "/nfs/x", "volumeName": "x"}])
        if path == "/imageManage/queryArtifactsAndTags":
            if not body.get("repositoriesId"):
                return ok({"rows": [{"id": k, "depositoryName": k} for k in self.tags], "total": len(self.tags)})
            repo = body["repositoriesId"]
            return ok({"rows": [{"fullPath": f"{REG}{repo.split('/', 1)[1]}:{t}", "tagName": t} for t in self.tags[repo]], "total": 1})
        j = next((j for j in self.jobs if j["id"] == q.get("id")), None)
        if path == "/workManage/stop":
            self.pending.append([self.lag, j, 0])
            return ok()
        if path == "/submitWork/addWorkTask":
            role = body["taskRoleAddDTOList"][0]
            self.n_new += 1
            pool_name = next(k for k, v in PID.items() if v == body["poolId"])
            spec_id = role.get("specificationId") or "s"
            new = {"id": f"n{self.n_new}", "taskName": body["taskName"], "poolName": pool_name, "createTime": f"2026-11-06 0{self.n_new}:00:00",
                   "runningCount": 0, "port": role["taskHostPortAddDTOList"][0]["hostPort"], "image": role["imageName"],
                   "spec": spec_id, "ip": self.spec_ip.get(spec_id, IP[pool_name])}
            self.jobs.insert(0, new)
            self.pending.append([self.lag + 1, new, 1])
            return ok()
        if path == "/workManage/clone":
            return ok({"taskName": j["taskName"], "poolId": PID[j["poolName"]], "machineAmount": 1, "envMap": {}, "minFail": None, "minSuccess": None,
                       "cloneStorageVolumeVOList": [{"mountPath": "/root/data1", "perm": "rw", "storagePath": "/nfs/x"}],
                       "cloneTaskRoleVOList": [{"taskRoleName": "taskRole1", "specificationId": j.get("spec") or "s", "imageName": j["image"],
                                                "runCmd": "/etc/init.d/ssh restart && sleep infinity",
                                                "cloneTaskHostPortDTOList": [{"containerPort": "22", "hostPort": j["port"]}]}]})
        return {"code": "-1", "msg": "未知接口 " + path}


def cfg(url):
    """三台服务器（平台上的作业名同使用人）、指向模拟平台。"""
    return fleet_cfg(private_pools=[PRIV], public_max=1, platform=url, platform_user="u", keepalive_cmd="KA_CALLER=labmon-fleet bash /x/ka.sh",
                     servers=[{"id": "a", "holder": "AA", "job": "AA", "ssh": "root@198.51.100.29", "port": 15001, "pool": PRIV},
                              {"id": "b", "holder": "BB", "job": "BB", "ssh": "root@198.51.100.27", "port": 15002, "pool": PUB},
                              {"id": "c", "holder": "CC", "job": "CC", "ssh": "root@198.51.100.27", "port": 15003, "pool": PUB}])


class Remote:
    """ssh 与探测的替身：down 次内连不上（255），之后开保活（fleet_guard.sh）照 ka 的结果；记下每次调用。"""

    def __init__(self, down=1, ka="ok start 123", states=None):
        self.down, self.ka, self.states = down, ka, states or {}
        self.ssh_calls, self.forgot, self.probed = [], [], []

    def ssh(self, s, remote, input=None, timeout=60):
        self.ssh_calls.append((s["id"], remote))
        if sum(1 for x in self.ssh_calls if x[0] == s["id"]) <= self.down:
            return 255, "", "ssh: connect to host h port 1: Connection refused"
        return 0, self.ka + "\n", ""

    def forget(self, s):
        self.forgot.append(s["id"])

    def probe(self, sid):
        self.probed.append(sid)
        return self.states.get(sid, ("keepalive", ""))


class FleetOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LABMON_STATE"] = self.tmp.name
        hf = Path(self.tmp.name) / "hosts.toml"
        hf.write_text(f'[[hosts]]\nid = "29"\nssh = "root@198.51.100.29"\nport = 15155\nplatform_job = "ALICE"\nplatform_pool = "{PRIV}"\n')
        os.environ["LABMON_HOSTS"] = str(hf)
        FO.logout()
        FO.SESS["captcha_at"] = 0
        self.plat = FleetPlat(page=5)                 # BB 不在第一页，要按名字查
        self.busy = set()                             # 本机连得上的 (节点, 端口)：端口探测的替身
        FB._TCP.clear()                               # 预览里端口试连的缓存：各测试的替身不同

    def tearDown(self):
        os.environ["LABMON_HOSTS"] = NO_HOSTS
        self.tmp.cleanup()

    def me(self, account="alice", person="ALICE") -> str:
        """看板上登录的管理员账号，绑着组内人员 person：平台操作认“我”只认账号库的绑定（#19）。"""
        from fixtures import add_account
        with unittest.mock.patch("labmon.users._log"):
            add_account(account, "admin", person=person)
        return account

    def sync(self, code="q7Wz"):
        c = cfg(self.plat.url)
        if code:
            self.assertEqual(FO.captcha(c)["code"], "q7Wz")
            FO.SESS["captcha_at"] = 0
        FO.start("sync", c, code, password=lambda u: "pw", pace=0)
        for t in threading.enumerate():
            if t.name == "fleet-sync":
                t.join(10)
        return FO.op()

    def test_parse_jobjson(self):
        d = FO.from_detail(FO.parse_jobjson(JSONTEXT))
        self.assertEqual((d["image"], d["ports"], d["cpu"], d["pool"], d["gpu_uuids"]), ("reg/ubuntu:aa", {"22": "15001"}, 17, PRIV, ["GPU-x"]))
        with self.assertRaises(ValueError):
            FO.parse_jobjson(JSONTEXT[:300])
        e = FO.import_json("a", JSONTEXT, cfg(self.plat.url))
        self.assertEqual((e["holder"], e["src"]), ("AA", "网页 Json 文件"))
        with self.assertRaises(P.PlatformError):
            FO.import_json("b", JSONTEXT, cfg(self.plat.url))          # 端口对不上，不存

    def test_sync(self):
        op = self.sync()
        self.assertIsNone(op["error"], op)
        self.assertEqual([s["state"] for s in op["steps"]], ["ok"] * 6)
        b = FO.boot()
        self.assertEqual((b["a"]["job_id"], b["a"]["image"], b["a"]["created"]), ("j1", REG + "ubuntu:aa", "2026-11-05 10:00:00"))
        self.assertEqual(b["b"]["job_id"], "j2")                       # 第一页没有，按名字查到
        self.assertEqual(b["a"]["clone"]["cloneTaskRoleVOList"][0]["runCmd"], "/etc/init.d/ssh restart && sleep infinity")
        self.assertNotIn("c", b)
        pf = FO.platform_state()
        self.assertFalse({"pools", "public", "public_max"} & set(pf), "各池台数没有读者，不存（页面由 fleetview 现算）")
        self.assertTrue(any("CC" in m and "没有运行中" in m for m in pf["issues"]))
        self.assertTrue(any("ZZ" in m and "没登记" in m for m in pf["issues"]))
        self.assertTrue(any("超过全组上限" in m for m in pf["issues"]))
        self.assertFalse(any("ALICE" in m for m in pf["issues"]))          # 自用主机的作业算登记了
        self.assertEqual({r["name"]: r["sid"] for r in pf["running"]}["ALICE"], "自用 29")
        paths = [c[0] for c in self.plat.calls]
        self.assertFalse([p for p in paths if p in ("/workManage/stop", "/submitWork/addWorkTask")])   # 只读
        self.assertEqual(sorted(c[1]["id"] for c in self.plat.calls if c[0] == "/workManage/clone"), ["j1", "j2", "jx"])   # 只克隆登记的
        self.assertEqual((b["29"]["holder"], b["29"]["job"]), ("ALICE", "ALICE"))   # 自用主机也核对、存克隆配置
        info = FB.pools_info()                                          # 顺带读回的池、规格、存储卷、镜像
        self.assertEqual((info["pools"][PUB]["hosts"], info["pools"][PRIV]["specs"][0]["quantityAvailable"]), (["192.0.2.27"], 1))
        self.assertEqual((info["volumes"], sorted(info["images"])), ([{"path": "/nfs/x", "name": "x"}], ["grp/pt", "grp/ubuntu"]))   # 只读人用到的仓库
        # 再核对一次：沿用登录，不再登录、不再克隆；镜像变了记下来
        self.plat.jobs[0]["image"] = REG + "ubuntu:aa2"
        n = len(self.plat.calls)
        op = self.sync(code=None)
        self.assertIsNone(op["error"], op)
        later = [c[0] for c in self.plat.calls[n:]]
        self.assertNotIn("/user/login", later)
        self.assertNotIn("/workManage/clone", later)
        self.assertIn("沿用", op["steps"][0]["note"])
        self.assertTrue(any("镜像" in m and "aa2" in m for m in FO.platform_state()["changes"]))
        self.assertEqual(FO.boot()["a"]["image"], REG + "ubuntu:aa2")

    def test_kicked(self):
        self.sync()
        self.plat.kick()
        op = self.sync(code=None)
        self.assertEqual([s["state"] for s in op["steps"][:2]], ["ok", "err"])
        self.assertIn("重新输验证码", op["error"])
        self.assertFalse(FO.session()["logged_in"])
        r = records(cat="op")
        self.assertEqual(r[0]["kind"], "op_fail")                              # 停下的原因留在记录里
        self.assertIn("与平台核对停在「读作业列表」", r[0]["msg"])
        self.assertIn("平台登录失效", r[1]["msg"])
        with self.assertRaises(P.PlatformError):
            FO.start("sync", cfg(self.plat.url))                       # 没登录：要验证码

    def test_captcha_gap_and_busy(self):
        c = cfg(self.plat.url)
        FO.captcha(c)
        with self.assertRaises(P.PlatformError):
            FO.captcha(c)                                              # 换得太快
        with self.assertRaises(P.PlatformError):
            FO.start("sync", c)                                        # 没登录也没给验证码

    def test_captcha_token(self):
        """两个页面先后取验证码：带着先取那张的 tok 来提交，不拿它去登录（平台只认最后一张）；不带 tok 的照旧。"""
        c = cfg(self.plat.url)
        first = FO.captcha(c)
        FO.SESS["captcha_at"] = 0
        second = FO.captcha(c)
        self.assertNotEqual(first["tok"], second["tok"])
        with self.assertRaises(P.PlatformError) as cm:
            FO.start("sync", c, "q7Wz", tok=first["tok"], password=lambda u: "pw", pace=0)
        self.assertIn("别的页面", str(cm.exception))
        self.assertNotIn("/user/login", [x[0] for x in self.plat.calls])
        FO.start("sync", c, "q7Wz", tok=second["tok"], password=lambda u: "pw", pace=0)
        for t in threading.enumerate():
            if t.name == "fleet-sync":
                t.join(10)
        self.assertIsNone(FO.op()["error"], FO.op())

    def test_op_thread_gone(self):
        """操作线程在收尾里出错退出、没来得及清 active：op() 看线程不在就标成停下，能收起、不挡新操作。"""
        F.state_dir().mkdir(parents=True, exist_ok=True)
        (F.state_dir() / "op.json").write_text(json.dumps({"kind": "renew", "active": True, "pid": os.getpid(), "ids": ["a"],
                                                           "steps": [{"name": "登录", "state": "ok", "note": None}, {"name": "探测并核对", "state": "run", "note": None}]}))
        op = FO.op()
        self.assertFalse(op["active"])
        self.assertEqual(op["steps"][1]["state"], "err")
        FO.dismiss()
        self.assertEqual(FO.op(), {})

    def test_borrowed_refuses_ops(self):
        """借来在用的服务器：续租、关机、换人、开保活都拒绝（会停掉自己的 worker），先还。"""
        c = cfg(self.plat.url)
        c["servers"][1]["borrow_since"] = "2026-11-06 10:00"
        for kind, kw in (("renew", {}), ("off", {}), ("swap", {"person": "DD"}), ("ka", {})):
            with self.assertRaises(P.PlatformError) as cm:
                FO.start(kind, c, ids=["b"], **kw)
            self.assertIn("先在它的「借用」里还掉", str(cm.exception))
            self.assertIn("与平台核对", str(cm.exception))
        c["servers"][1]["borrow_returned"] = "2026-11-07 10:00"
        with self.assertRaises(P.PlatformError) as cm:
            FO.start("renew", c, ids=["b"])                             # 还了：只差登录
        self.assertIn("验证码", str(cm.exception))

    def test_reconcile(self):
        """核对的比对段是纯函数：规格变了、没登记的作业（自用主机的算登记）、各池台数与公用池上限。"""
        c = cfg(self.plat.url)
        sl = [*c["servers"], {"id": "29", "holder": "ALICE", "job": "ALICE", "pool": PRIV, "mine": True}]
        j = lambda i, n, p: {"id": i, "taskName": n, "poolName": p, "createTime": "2026-11-05 10:00:00", "unusedTime": "6d"}  # noqa: E731
        running = [j("j1", "AA", PRIV), j("jz", "ZZ", PUB), j("jx", "ALICE", PRIV), j("j2", "BB", PUB)]
        found = {"a": (running[0], {"image": "i2", "cpu": 17, "ip": "203.0.113.1"}), "b": (running[3], {"image": "i1"})}
        old = {"a": {"image": "i1", "cpu": 17, "ip": None}, "b": {"image": "i1"}}
        r = FO.reconcile(c, sl, running, found, old, {"ALICE": "29"})
        self.assertEqual(r["changes"], [("a", "AA（a）的镜像由 i1 变为 i2")])                # IP 以前没存：不算变
        self.assertEqual([(x["name"], x["sid"]) for x in r["rows"]], [("AA", "a"), ("ZZ", None), ("ALICE", "自用 29"), ("BB", "b")])
        self.assertEqual((r["pools"], r["public"]), ({PRIV: 2, PUB: 2}, 2))
        self.assertEqual(r["issues"], [f"平台上运行中、看板没登记：ZZ（{PUB}，11-05 10:00 创建）", "公用池运行中 2 个，超过全组上限 1"])
        found["29"] = (running[2], {})
        r = FO.reconcile(c, sl, running, found, old, {})
        self.assertEqual(next(x["sid"] for x in r["rows"] if x["name"] == "ALICE"), "自用 29")   # 核对时找到的自用主机也写“自用”
        self.assertEqual(FO.reconcile(dict(c, public_max=None), sl, [], {}, {}, {}), {"changes": [], "rows": [], "pools": {}, "public": 0, "issues": []})

    def test_note(self):
        """出错时这一台此刻的状态只有一处文案（_note）：按 op.json 里记的进度标记，续租与换人、关机、开机各写各的下一步。"""
        a, m = cfg(self.plat.url)["servers"][0], {"id": "29", "label": "29", "holder": "ALICE", "mine": True}
        self.assertEqual(FO._note("renew", a, "AA", {}), "平台上什么都没动。")
        self.assertIn("AA 的停止请求已发出", FO._note("renew", a, "AA", {"stop_sent": True}))
        self.assertIn("再点一次续租会克隆重开", FO._note("renew", a, "AA", {"stop_sent": True, "stopped": True}))
        self.assertIn("不要再续租，免得重复开", FO._note("renew", a, "AA", {"stopped": True, "submit_sent": True}))
        self.assertIn("「开保活」；不要再续租，否则会把新作业又停掉", FO._note("renew", a, "AA", {"submit_sent": True, "up": True}))
        self.assertIn("「继续」", FO._note("renew", m, "29", {"up": True}))
        self.assertIn("29 的停止请求", FO._note("swap", m, "DD", {"stop_sent": True}))             # 自用主机按主机名称叫
        self.assertIn("这台已标成关机；新作业还没提交：要接着开就在这台的详情页点「开机」", FO._note("swap", a, "DD", {"stopped": True}))
        self.assertTrue(FO._note("off", a, "AA", {"stopped": True}).endswith("已标成关机。"))
        self.assertIn("已标成归还；DD 的新作业还没提交：要接着开就在这个池点「+ 开新的」", FO._note("swap", m, "DD", {"stopped": True}))
        self.assertIn("不要再开机，免得重复开", FO._note("open", None, "EE", {"submit_sent": True}))
        self.assertIn("「开保活」；不要再开机", FO._note("swap", a, "DD", {"up": True}))

    def test_unlisted_ports_progress(self):
        """核对步骤里逐个读没登记作业的端口：进度写明读到第几个。"""
        self.sync()
        cli, c, notes = FO.SESS["cli"], cfg(self.plat.url), []
        ports = FO._unlisted_ports(cli, c, cli.jobs(), lambda: None, notes.append)
        self.assertEqual(notes, ["读第 1/1 个未登记作业（ZZ）的端口"])
        self.assertEqual(ports, [("root@198.51.100.27", 15999)])

    def test_view(self):
        self.sync()
        v = FO.view()
        self.assertTrue(v["boot"]["a"]["has_clone"])
        self.assertNotIn("clone", v["boot"]["a"])
        self.assertEqual(v["boot"]["a"]["volumes"], ["/nfs/x → /root/data1（rw）"])
        self.assertTrue(v["session"]["logged_in"])

    def renew(self, ids, remote=None, code="q7Wz", ack=(), kind="renew", **kw):
        c = cfg(self.plat.url)
        if code:
            FO.captcha(c)
            FO.SESS["captcha_at"] = 0
        self.remote = remote or Remote()
        base = {"probe": self.remote.probe}
        if kind != "off":
            base.update(ssh_fn=self.remote.ssh, forget_fn=self.remote.forget, ssh_poll=0.01)
        if kind != "ka":
            base.update(password=lambda u: "pw", pace=0, poll=0.01)
        if kind in ("renew", "off", "swap", "move"):
            base["ack"] = list(ack)
        if kind in ("off", "swap", "move", "open"):
            self.hosts = []
            base.update(fast=0.01, tcp=lambda n, p: (n, p) in self.busy, hosts=self.fake_hosts())
        kw = {**base, **kw}
        FO.start(kind, c, code if kind != "ka" else None, ids=ids, **kw)
        for t in threading.enumerate():
            if t.name == f"fleet-{kind}":
                t.join(10)
        return FO.op()

    def fake_hosts(self):
        """改主机清单的替身：记下调用；add_mine 返回新自用主机的 id。"""
        return {"holder": lambda *a: self.hosts.append(("holder",) + a), "add": lambda *a: self.hosts.append(("add",) + a),
                "remove": lambda sid: self.hosts.append(("remove", sid)), "return_mine": lambda hid: self.hosts.append(("return_mine", hid)),
                "add_mine": lambda *a: self.hosts.append(("add_mine",) + a) or a[0]}

    def paths(self, n=0):
        return [(c[0], (c[1] or {}).get("id") or ((c[2] or {}).get("taskName") if c[0] == "/submitWork/addWorkTask" else None))
                for c in self.plat.calls[n:] if c[0] in ("/workManage/stop", "/submitWork/addWorkTask")]

    def test_renew(self):
        op = self.renew(["a", "b"])
        self.assertIsNone(op["error"], op)
        self.assertEqual([x["state"] for x in op["steps"]], ["ok"] * 8)
        self.assertEqual(op["steps"][2]["name"], "AA：停止旧作业")
        self.assertEqual(self.paths(), [("/workManage/stop", "j1"), ("/submitWork/addWorkTask", "AA"),
                                        ("/workManage/stop", "j2"), ("/submitWork/addWorkTask", "BB")])   # 一台续完再动下一台
        new = {j["taskName"]: j for j in self.plat.jobs if j["id"].startswith("n")}
        self.assertEqual((new["AA"]["port"], new["AA"]["poolName"], new["BB"]["port"], new["BB"]["poolName"]), ("15001", PRIV, "15002", PUB))
        self.assertEqual([j["runningCount"] for j in self.plat.jobs if j["id"] in ("j1", "j2")], [0, 0])
        b = FO.boot()
        self.assertEqual((b["a"]["job_id"], b["a"]["created"], b["a"]["clone_job"], b["a"]["src"]), (new["AA"]["id"], new["AA"]["createTime"], new["AA"]["id"], "续租"))
        self.assertEqual(self.remote.forgot, ["a", "b"])                       # 换了容器，清旧主机密钥
        ka = [r for sid, r in self.remote.ssh_calls if sid == "a"]
        self.assertEqual(len(ka), 2)                                           # 第一次连不上，隔一会再连
        self.assertEqual(ka[-1], "bash -s -- start 'KA_CALLER=labmon-fleet bash /x/ka.sh' 300 '看板'")     # 开了以后盯 5 分钟
        self.assertEqual(self.remote.probed, ["a", "b", "a", "b"])            # 核对前探一次，开完保活再探一次
        self.assertEqual(op["servers"]["b"], {"stop_sent": True, "stopped": True, "submit_sent": True, "up": True, "job": new["BB"]["id"], "ka": True})
        evs = records(cat="op")[::-1]
        self.assertEqual([e["host"] for e in evs if e["kind"] == "fleet_renew"], ["a", "b"])
        self.assertEqual([e["kind"] for e in evs if e["kind"].startswith("op_")][-2:], ["op_start", "op_done"])
        self.assertEqual(evs[-1]["hosts"], ["a", "b"])                         # 一次续两台：记录里两台都能筛到
        self.assertTrue(any(e["msg"].startswith("登录平台") for e in evs if e["kind"] == "login"))
        # 再续一次：沿用登录，换掉的是上次新开的那个
        n = len(self.plat.calls)
        op = self.renew(["a"], code=None)
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.paths(n)[0], ("/workManage/stop", new["AA"]["id"]))
        self.assertNotIn("/user/login", [c[0] for c in self.plat.calls[n:]])

    def test_experiment_needs_ack(self):
        r = Remote(states={"b": ("job", "demo_eval 评测")})
        op = self.renew(["a", "b"], r)
        self.assertEqual([x["state"] for x in op["steps"][:3]], ["ok", "err", "wait"])
        self.assertIn("没确认", op["error"])
        self.assertIn("什么都没动", op["server"])
        self.assertEqual(self.paths(), [])                                     # a 也没动
        FO.SESS["captcha_at"] = 0
        op = self.renew(["b"], Remote(states={"b": ("job", "demo_eval 评测")}), code=None, ack=["b"])
        self.assertIsNone(op["error"], op)

    def test_port_mismatch_touches_nothing(self):
        self.plat.jobs[5]["port"] = "15999"                                    # BB 的端口对不上
        op = self.renew(["a", "b"])
        self.assertEqual(op["steps"][1]["state"], "err")
        self.assertIn("哪台都没动", op["error"])
        self.assertEqual(self.paths(), [])

    def test_stopped_by_platform(self):
        self.plat.jobs[0]["runningCount"] = 0                                  # AA 已被平台停掉：克隆最近一个重开，不停
        op = self.renew(["a"])
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.paths(), [("/submitWork/addWorkTask", "AA")])
        self.assertEqual(op["steps"][2]["note"], "作业已被平台停掉，跳过")
        self.assertIn("j1", [c[1].get("id") for c in self.plat.calls if c[0] == "/workManage/clone"])
        # 公用池：BB 停了，ZZ 还开着，再开 BB 就超全组上限（1）
        self.plat.jobs = [j for j in self.plat.jobs if j["id"] != "n1"]
        next(j for j in self.plat.jobs if j["id"] == "j2")["runningCount"] = 0
        n = len(self.plat.calls)
        op = self.renew(["b"], code=None)
        self.assertIn("会超", op["error"])
        self.assertEqual(self.paths(n), [])

    def test_submit_refused_can_retry(self):
        """停了旧作业、平台明确拒绝提交：不算“已提交”，页面说再点续租会克隆重开（以前写成“不要再续租”）。"""
        self.plat.fail["/submitWork/addWorkTask"] = "资源不足"
        op = self.renew(["a"])
        self.assertIn("资源不足", op["error"])
        self.assertEqual(op["servers"]["a"], {"stop_sent": True, "stopped": True, "submit_sent": False})
        self.assertIn("再点一次续租会克隆重开", op["server"])
        self.assertNotIn("不要再续租", op["server"])
        del self.plat.fail["/submitWork/addWorkTask"]
        n = len(self.plat.calls)
        op = self.renew(["a"], code=None)                                       # 再续：已停的跳过，克隆重开
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.paths(n), [("/submitWork/addWorkTask", "AA")])

    def test_kicked_after_stop(self):
        self.plat.on["/workManage/stop"] = self.plat.kick                     # 停止请求发出后，网页上有人登录
        op = self.renew(["a", "b"])
        self.assertEqual(op["steps"][2]["state"], "err")
        self.assertIn("重新输验证码", op["error"])
        self.assertIn("停止请求已发出", op["server"])
        self.assertIn("还没动：BB", op["server"])
        self.assertFalse(FO.session()["logged_in"])
        self.assertEqual(self.paths(), [("/workManage/stop", "j1")])

    def test_ssh_timeout_then_ka(self):
        op = self.renew(["a", "b"], Remote(down=10 ** 6), ssh_wait_s=0.05)
        self.assertEqual([x["state"] for x in op["steps"]], ["ok"] * 4 + ["err", "wait", "wait", "wait"])
        self.assertIn("还没连上", op["error"])
        self.assertIn("开保活", op["server"])
        self.assertIn("不要再续租", op["server"])
        self.assertIn("还没动：BB", op["server"])
        # 只补开保活：不碰平台、不要验证码
        n = len(self.plat.calls)
        op = self.renew(["a"], Remote(down=0), code=None, kind="ka")
        self.assertIsNone(op["error"], op)
        self.assertEqual(op["steps"][0]["note"], "ssh 已连上，保活已开")
        self.assertEqual(len(self.plat.calls), n)
        self.assertEqual(self.remote.forgot, [])                               # 主机密钥没报错，不清

    def test_ka_failed(self):
        op = self.renew(["a"], Remote(down=0, ka="fail 保活没起来"), kind="ka", code=None)
        self.assertIn("保活没起来", op["error"])
        op = self.renew(["a"], Remote(down=0, states={"a": ("job", "demo_eval 评测")}), kind="ka", code=None)
        self.assertIn("不开保活", op["error"])
        self.assertEqual(self.remote.ssh_calls, [])                            # 在跑实验：没去开

    def mine_kw(self, seq):
        """自用主机续租后那一步的替身：拉起 worker、给 worker 下指令都只记下来。"""
        return {"start_worker": lambda hid: seq.append("start") or (0, "worker 起来了\n"),
                "ovr": lambda action, kind, hid, until=None, at=None: seq.append(f"{action} {kind}") or 0}

    def test_renew_mine(self):
        """自用主机与组内服务器同一条续租：沿用登录；最后一步等 ssh、拉起 worker、撤掉预备（不开保活）。"""
        self.sync()
        R._update(lambda st: st["res"].__setitem__("29", {"state": "ready"}))
        seq, n = [], len(self.plat.calls)
        op = self.renew(["29"], code=None, probe_mine=lambda h: ("keepalive", ""), **self.mine_kw(seq))
        self.assertIsNone(op["error"], op)
        self.assertEqual([x["name"] for x in op["steps"]], ["登录", "探测并核对", "29：停止旧作业", "29：提交新作业、等运行", "29：等 ssh、拉起 worker"])
        self.assertIn("沿用", op["steps"][0]["note"])
        self.assertNotIn("/user/login", [c[0] for c in self.plat.calls[n:]])
        self.assertEqual(self.paths(n), [("/workManage/stop", "jx"), ("/submitWork/addWorkTask", "ALICE")])
        self.assertEqual(seq, ["start", "rm drain", "rm deadline"])
        self.assertEqual(op["steps"][-1]["note"], "worker 已拉起；已恢复放行")
        self.assertEqual([r for sid, r in self.remote.ssh_calls if sid == "29"], ["true", "true"])   # 第一次连不上，隔一会再连
        self.assertEqual(self.remote.probed, [])                             # 自用主机不另外探，看看板最近一次采集
        self.assertNotIn("29", R.state()["res"])
        self.assertEqual(R.state()["renewing"], {})
        self.assertEqual(FO.boot()["29"]["src"], "续租")
        self.assertIn("29 续租完成", [e["msg"] for e in load_rows(state_dir() / "events.jsonl") if e.get("kind") == "renew"][-1])

    def test_renew_mine_needs_ack(self):
        self.sync()
        seq = []
        op = self.renew(["29"], code=None, probe_mine=lambda h: ("job", "r9 第 3 条"), **self.mine_kw(seq))
        self.assertIn("没确认", op["error"])
        self.assertEqual((self.paths(), seq), ([], []))
        op = self.renew(["29"], code=None, ack=["29"], probe_mine=lambda h: ("job", "r9 第 3 条"), **self.mine_kw(seq))
        self.assertIsNone(op["error"], op)
        self.assertEqual(seq, ["start"])                                    # 没预备过：不动 drain

    def test_renew_mine_unprobed_needs_ack(self):
        """自用主机没探到（快照里没它、或 worker 没状态）：按未识别处理，要勾「已说好」才续租。"""
        self.sync()
        seq = []
        op = self.renew(["29"], code=None, probe_mine=lambda h: (None, "没探到"), **self.mine_kw(seq))
        self.assertIn("没探到这台此刻在跑什么", op["error"])
        self.assertEqual((self.paths(), seq), ([], []))
        op = self.renew(["29"], code=None, ack=["29"], probe_mine=lambda h: (None, "没探到"), **self.mine_kw(seq))
        self.assertIsNone(op["error"], op)
        self.assertIn("没探到", op["steps"][1]["note"])

    def test_renew_mine_local_loss_holds(self):
        """续租前扫到有任务要用本地文件（不能自愈）：续租完先挡住再拉 worker，不恢复放行。"""
        self.sync()
        loss = {"fatal": ["x4_s20_seed1"], "heal": [], "dirs": ["/tmp/a1_r9/w0"]}
        R._update(lambda st: (st["res"].__setitem__("29", {"state": "ready"}), st["loss"].__setitem__("29", loss)))
        seq = []
        op = self.renew(["29"], code=None, probe_mine=lambda h: ("idle", ""), **self.mine_kw(seq))
        self.assertIsNone(op["error"], op)
        self.assertEqual(seq, ["add drain", "start"])
        r = R.state()["res"]["29"]
        self.assertEqual(r["state"], "held")
        self.assertIn("x4_s20_seed1", r["why"])
        self.assertIn("保持不放行", op["steps"][-1]["note"])

    def test_renew_mine_ssh_timeout_then_continue(self):
        self.sync()
        seq = []
        op = self.renew(["29"], Remote(down=10 ** 6), code=None, ssh_wait_s=0.05, probe_mine=lambda h: ("idle", ""), **self.mine_kw(seq))
        self.assertEqual(op["steps"][-1]["state"], "err")
        self.assertTrue(op["servers"]["29"]["up"])
        self.assertIn("继续", op["server"])
        self.assertIn("不要再续租", op["server"])
        self.assertEqual(seq, [])
        # 继续：只等 ssh、拉起 worker，不碰平台、不要验证码
        n = len(self.plat.calls)
        op = self.renew(["29"], Remote(down=0), code=None, kind="ka", **self.mine_kw(seq))
        self.assertIsNone(op["error"], op)
        self.assertEqual(op["steps"][0]["name"], "29：等 ssh、拉起 worker")
        self.assertEqual(len(self.plat.calls), n)
        self.assertEqual(seq, ["start"])

    def test_renew_mine_worker_fails(self):
        self.sync()
        op = self.renew(["29"], code=None, probe_mine=lambda h: ("idle", ""), start_worker=lambda hid: (3, "no python\n"), ovr=lambda *a, **k: 0)
        self.assertIn("worker 拉起失败（退出码 3）", op["error"])
        self.assertIn("29", R.state()["renewing"])                         # 还没做完：「继续」时照这次开始前的依赖

    def test_busy_server_not_probed(self):
        """正在做平台操作的服务器只有一种问法（fleetops.busy）：探测跳过它，掉线判定、自动预备、借用都按它。线程没了的不算在做。"""
        c = cfg(self.plat.url)
        seen = []
        with fake_op(["a", "b"]):
            self.assertEqual(FO.busy(), {"a": "renew", "b": "renew"})
            F.tick(t=10 ** 9, fetch_fn=lambda s: (seen.append(s["id"]), (None, "x"))[1], cfg=c, busy=FO.busy())
            self.assertEqual(seen, ["c"])                                      # 正在续租的服务器不探，免得记成掉线
            e = {"id": "a", "label": "A", "error": "ssh 超时", "status": None}
            O.health([e], {"a": {}}, 10 ** 9)                                    # 掉线判定自己问 fleetops.busy
            self.assertEqual((e["health"]["state"], e["health"]["why"]), ("renew", "正在续租"))   # 连不上是预期的，不算掉线
        self.assertEqual(FO.busy(), {})
        F.state_dir().mkdir(parents=True, exist_ok=True)
        (F.state_dir() / "op.json").write_text(json.dumps({"kind": "renew", "active": True, "pid": os.getpid(), "ids": ["a"]}))
        self.assertEqual(FO.busy(), {})                                        # 本进程里没有那个线程：操作已停下

    def test_stale_op(self):
        F.state_dir().mkdir(parents=True, exist_ok=True)
        (F.state_dir() / "op.json").write_text(json.dumps({"kind": "renew", "active": True, "pid": 2 ** 22 + 7, "ids": ["a"],
                                                           "steps": [{"name": "登录", "state": "run", "note": None}]}))
        op = FO.op()
        self.assertFalse(op["active"])
        self.assertIn("中途重启", op["steps"][0]["note"])
        fails = lambda: [e for e in records(cat="op") if e["kind"] == "op_fail"]
        self.assertEqual([e["host"] for e in fails()], ["a"])
        self.assertIn("续租停在「登录」：看板服务中途重启", fails()[0]["msg"])
        FO.op()
        self.assertEqual(len(fails()), 1)                                      # 只记一次
        op = self.renew(["a"], kind="ka", code=None)                           # 不再挡着新操作
        self.assertIsNone(op["error"], op)

    def submits(self, n=0):
        return [c[2] for c in self.plat.calls[n:] if c[0] == "/submitWork/addWorkTask"]

    def test_off(self):
        op = self.renew(["a"], kind="off")
        self.assertIsNone(op["error"], op)
        self.assertEqual([x["name"] for x in op["steps"]], ["登录", "核对", "AA：停止作业"])
        self.assertEqual(self.paths(), [("/workManage/stop", "j1")])
        self.assertEqual(F.off_servers()["a"]["holder"], "AA")
        self.assertEqual(self.hosts, [])                                       # 关机不改主机清单
        seen = []
        F.tick(t=10 ** 9, fetch_fn=lambda s: (seen.append(s["id"]), (None, "x"))[1], cfg=cfg(self.plat.url))
        self.assertNotEqual(seen, ["a"])                                       # 关了机的服务器不轮探
        v = FV.view(cfg=cfg(self.plat.url))
        x = next(x for x in v["servers"] if x["id"] == "a")
        self.assertEqual((x["state"], x["lease_end"]), ("off", None))
        self.assertEqual((v["quota"]["private"], [m["id"] for m in v["mine"]]), (1, ["29"]))   # 关了机的不算配额；自用主机 29 算
        with self.assertRaises(P.PlatformError):
            FO.start("renew", cfg(self.plat.url), ids=["a"])                   # 已关机：不能续租
        with self.assertRaises(P.PlatformError):
            FO.start("swap", cfg(self.plat.url), ids=["a"])                    # 也不能换人，要开机

    def test_off_gone(self):
        """连不上、平台上也没有它运行中的作业：只在看板上标关机；连得上的不这样做。"""
        self.plat.jobs[0]["runningCount"] = 0
        op = self.renew(["a"], Remote(states={"a": ("keepalive", "")}), kind="off")
        self.assertIn("平台上没有运行中的 AA", op["error"])
        self.assertEqual(F.off_servers(), {})
        op = self.renew(["a"], Remote(states={"a": ("down", "连不上")}), kind="swap")
        self.assertIn("先点「关机」", op["error"])
        F.state_dir().mkdir(parents=True, exist_ok=True)
        save_json(F.state_file("platform.json"), {"running": [{"id": "old", "name": "AA", "pool": PRIV}, {"id": "o2", "name": "AA", "pool": PUB}]})
        op = self.renew(["a"], Remote(states={"a": ("down", "连不上")}), kind="off")
        self.assertIsNone(op["error"], op)
        self.assertEqual(op["steps"][-1]["note"], "平台上已没有它的作业，只在看板上标为已关机")
        # #86：上次核对存的同名同池的行是过时的（平台上已没有），删掉，预览不再当 AA 占着；别的池同名的不动
        self.assertEqual([x["id"] for x in FO.platform_state()["running"]], ["o2"])
        self.assertEqual(F.off_servers()["a"]["holder"], "AA")
        self.assertEqual([p for p in self.paths() if p[0] == "/workManage/stop"], [])

    def test_off_needs_ack(self):
        op = self.renew(["a"], Remote(states={"a": ("job", "demo_eval 评测")}), kind="off")
        self.assertIn("没确认", op["error"])
        self.assertIn("什么都没动", op["server"])
        self.assertEqual(self.paths(), [])
        self.assertEqual(F.off_servers(), {})

    def local_disk(self, sid, gib=3.2):
        """这台本地盘开机以来 labmon 之外写了 gib GiB（#149）：组内服务器写进探测状态（样本的根盘与扫出的路径），自用主机写进工作台快照（overview._annotate 算的 lw）。"""
        root, b = {"used": gib, "total": 750.0, "fs": "overlay", "ok": True}, int(gib * 2 ** 30)
        wrote = {"t": 1, "started": 100, "since": 100, "used": b, "b": b, "n": 3, "top": [["/root/ckpt", b, 1]], "more": [0, 0]}
        if sid == "29":
            save_json(state_dir() / "snapshot.json", {"hosts": [{"id": "29", "lw": F.local_writes(root, wrote, 0)}]})
        else:
            F.state_dir().mkdir(parents=True, exist_ok=True)
            F._save_state({sid: {"sample": {"state": "keepalive", "root": root, "started": 100}, "wrote": wrote}})

    def test_local_disk_needs_ack(self):
        """本地盘开机以来 labmon 之外写了 1 GiB 以上（#149）：续租、关机、换人同一个判断，没勾「已确认」就停下、什么都没动，提示里写多少、大头在哪；
        不到 1 GiB 的照常做。"""
        self.sync()
        FB.set_person("DD", image=REG + "ubuntu:dd", run_cmd=RUN, cfg=cfg(self.plat.url))
        self.local_disk("a")
        for kind, verb in (("renew", "续租"), ("off", "关机"), ("swap", "换人")):
            FO.SESS["captcha_at"] = 0
            op = self.renew(["a"], code=None, kind=kind, **({"person": "DD"} if kind == "swap" else {}))
            self.assertIn(f"本地盘开机以来 labmon 之外写了 3.2 GiB（/root/ckpt），{verb}会清空，要留的先挪到共享盘，选的时候没确认", op["error"])
            self.assertEqual(self.paths(), [])
        self.local_disk("a", 0.5)
        op = self.renew(["a"], code=None, kind="off")
        self.assertIsNone(op["error"], op)

    def test_local_disk_ack_renews(self):
        """勾了「已确认」照常续租；自用主机取工作台快照里的 lw，同一个判断。"""
        self.sync()
        self.local_disk("29")
        seq = []
        op = self.renew(["29"], code=None, probe_mine=lambda h: ("keepalive", ""), **self.mine_kw(seq))
        self.assertIn("本地盘开机以来 labmon 之外写了 3.2 GiB", op["error"])
        self.assertEqual((self.paths(), seq), ([], []))
        op = self.renew(["29"], code=None, ack=["29"], probe_mine=lambda h: ("keepalive", ""), **self.mine_kw(seq))
        self.assertIsNone(op["error"], op)

    def test_swap(self):
        """换人：停 AA 后立刻按 DD 存的镜像提交，沿用端口 15001，id 不变，主机清单只改 holder。"""
        self.sync()
        FB.set_person("DD", image=REG + "ubuntu:dd", run_cmd=RUN, cfg=cfg(self.plat.url))
        n = len(self.plat.calls)
        op = self.renew(["a"], code=None, kind="swap", person="DD")
        self.assertIsNone(op["error"], op)
        self.assertEqual([x["name"] for x in op["steps"]], ["登录", "核对", "AA：停止作业", "DD：提交新作业、等运行", "DD：等 ssh、开保活"])
        self.assertIn("端口 15001（沿用原端口）", op["steps"][1]["note"])
        self.assertEqual(self.paths(n), [("/workManage/stop", "j1"), ("/submitWork/addWorkTask", "DD")])
        sub = self.submits(n)[0]
        role = sub["taskRoleAddDTOList"][0]
        self.assertEqual((sub["taskName"], role["imageName"], role["taskHostPortAddDTOList"][0]["hostPort"], role["poolId"]),
                         ("DD", REG + "ubuntu:dd", "15001", "p1"))
        self.assertEqual(self.hosts, [("holder", "a", "DD", "DD")])
        b = FO.boot()["a"]
        self.assertNotIn("off", b)
        self.assertEqual((b["holder"], b["src"], b["ip"]), ("DD", "换人", "192.0.2.29"))
        self.assertEqual(op["conn"]["cmd"], "ssh -p 15001 root@198.51.100.29")
        self.assertEqual(self.remote.forgot, ["a"])
        self.assertEqual(op["servers"]["a"], {"stop_sent": True, "stopped": True, "submit_sent": True, "up": True,
                                              "job": self.plat.jobs[0]["id"], "ka": True})
        kinds = [e["kind"] for e in records()[::-1] if e["kind"] in ("op_start", "fleet_off", "fleet_boot", "op_done")]
        self.assertEqual(kinds[-4:], ["op_start", "fleet_off", "fleet_boot", "op_done"])
        # 停到提交之间：只查了一次“停下了没有”和一次提交前的名单，没有等 20 秒一轮
        calls = [c[0] for c in self.plat.calls[n:]]
        i, j = calls.index("/workManage/stop"), calls.index("/submitWork/addWorkTask")
        self.assertEqual(calls[i + 1:j], ["/workManage/list"] * 2)

    def test_swap_refuses(self):
        op = self.renew(["a"], Remote(states={"a": ("job", "demo_eval 评测")}), kind="swap", person="DD")
        self.assertIn("没确认", op["error"])                                   # 在跑实验：要先说好
        FO.SESS["captcha_at"] = 0
        FB.set_person("DD", image=REG + "ubuntu:nope", run_cmd=RUN, cfg=cfg(self.plat.url))
        op = self.renew(["a"], code=None, kind="swap", person="DD")
        self.assertIn("镜像：grp/ubuntu 里没有标签 nope", op["error"])         # 换人克隆旧作业，不用模板作业；镜像要在仓库里
        self.assertIn("什么都没动", op["error"])
        self.assertEqual(self.paths(), [])
        with self.assertRaises(P.PlatformError):
            FO.start("swap", cfg(self.plat.url), ids=["a"], person="D D")      # 作业名不合法
        with self.assertRaises(P.PlatformError):
            FO.start("open", cfg(self.plat.url), pool="不认得的池", person="EE")

    def test_swap_submit_fails_then_boot(self):
        """停了旧的、新的没提交成：这台标成关机，详情页「开机」接着开（沿用原端口，不再停什么）。"""
        self.sync()
        self.plat.fail["/submitWork/addWorkTask"] = "资源不足"
        op = self.renew(["a"], code=None, kind="swap", person="AA")
        self.assertIn("资源不足", op["error"])
        self.assertIn("已标成关机", op["server"])
        self.assertIn("「开机」", op["server"])
        self.assertEqual(F.off_servers()["a"]["holder"], "AA")
        del self.plat.fail["/submitWork/addWorkTask"]
        n = len(self.plat.calls)
        op = self.renew(["a"], code=None, kind="open", person="EE")
        self.assertIsNone(op["error"], op)
        self.assertEqual([x["name"] for x in op["steps"]], ["登录", "核对", "EE：提交新作业、等运行", "EE：等 ssh、开保活"])
        self.assertEqual(self.paths(n), [("/submitWork/addWorkTask", "EE")])
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["taskHostPortAddDTOList"][0]["hostPort"], "15001")
        self.assertEqual(self.hosts, [("holder", "a", "EE", "EE")])
        self.assertNotIn("a", F.off_servers())

    def test_open_new_slot(self):
        """开新的：公用池按模板作业（BB 的）开，新的人用新的人默认的镜像；端口挑 15151 起的空闲端口（15151 本机连得上，算占着），主机清单加一块。"""
        self.sync()
        FB.set_defaults(REG + "ubuntu:bb", RUN)
        op = self.renew([], code=None, kind="open", person="EE", pool=PUB)
        self.assertIn("会超", op["error"])                                     # 公用池上限 1，已开 2
        FO.SESS["captcha_at"] = 0
        c = cfg(self.plat.url)
        c["public_max"] = 3
        self.busy = {("root@198.51.100.27", 15151)}
        self.plat.jobs[2]["port"] = "15152"                                    # 没登记的 ZZ 占着 15152（要取详情才知道）
        n = len(self.plat.calls)
        FO.start("open", c, None, ids=[], person="EE", pool=PUB, probe=self.remote.probe, ssh_fn=self.remote.ssh, forget_fn=self.remote.forget,
                 ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01, tcp=lambda nd, p: (nd, p) in self.busy,
                 hosts=self.fake_hosts())
        self.hosts = []
        for t in threading.enumerate():
            if t.name == "fleet-open":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertIn("jz", [c[0].rsplit("/", 1)[1] for c in self.plat.calls[n:] if c[0].startswith("/workManage/getWorkTaskInfo/")])
        sub = self.submits(n)[0]
        role = sub["taskRoleAddDTOList"][0]
        self.assertEqual((sub["taskName"], role["imageName"], role["taskHostPortAddDTOList"][0]["hostPort"]), ("EE", REG + "ubuntu:bb", "15153"))
        self.assertEqual(self.hosts, [("add", "27-15153", "EE", "root@198.51.100.27", 15153, PUB, "EE")])
        self.assertEqual((FO.boot()["27-15153"]["holder"], op["conn"]["cmd"]), ("EE", "ssh -p 15153 root@198.51.100.27"))

    def test_open_checks(self):
        """提交前逐项核对：镜像不在仓库、池里没有空卡，都什么都不动。"""
        self.sync()
        c = cfg(self.plat.url)
        FB.set_person("EE", image=REG + "ubuntu:nope", run_cmd=RUN, cfg=c)
        op = self.renew(["c"], code=None, kind="off")                          # c 在平台上没有运行中的作业：关不了
        self.assertIn("没有运行中的 CC", op["error"])
        FO.SESS["captcha_at"] = 0
        self.plat.avail["p1"] = 0
        op = self.renew([], code=None, kind="open", person="EE", pool=PRIV)
        self.assertIn("规格：", op["error"])
        self.assertIn("镜像：grp/ubuntu 里没有标签 nope", op["error"])
        self.assertEqual(self.paths(), [])

    def test_people_and_preview(self):
        """一人一份设置：占着服务器的人读到现有作业就记进名单；新的人按新的人默认；占着服务器的不能删。"""
        self.sync()
        c = cfg(self.plat.url)
        ps = FB.people(c)
        self.assertEqual((ps["AA"]["image"], ps["AA"]["run_cmd"], ps["AA"]["slots"], ps["AA"]["saved"], ps["AA"]["src"]),
                         (REG + "ubuntu:aa", RUN, ["a"], True, "job"))
        FB.set_person("AA", REG + "ubuntu:dd", RUN, "博士生", cfg=c)
        self.assertEqual(records(who="AA")[0]["msg"], f"人员：改了 AA 的设置：镜像 {REG}ubuntu:aa → {REG}ubuntu:dd；备注 空 → 博士生")
        self.assertEqual({k: FB.people(c)["AA"][k] for k in ("image", "note", "src")}, {"image": REG + "ubuntu:dd", "note": "博士生", "src": "manual"})
        self.plat.jobs[0]["image"] = REG + "ubuntu:aa2"                     # 之后作业换了镜像：名单不跟着变
        self.sync()
        self.assertEqual(FB.people(c)["AA"]["image"], REG + "ubuntu:dd")
        with self.assertRaises(P.PlatformError):
            FB.set_person("AA", remove=True, cfg=c)                            # 还占着服务器
        for bad in (("ubuntu", RUN), (REG + "ubuntu:ee", ""), ("", RUN)):     # 镜像没写全、启动命令空、镜像空
            with self.assertRaises(P.PlatformError):
                FB.set_person("EE", *bad, cfg=c)
        self.assertEqual(FB.defaults()["run_cmd"], RUN)                       # 没存过：取各台作业里最常见的
        FB.set_defaults(REG + "ubuntu:bb", RUN)
        self.assertEqual(FB.defaults(), {"image": REG + "ubuntu:bb", "run_cmd": RUN, "saved": True})
        pv = FB.preview(c, "swap", "a", "EE", tcp=lambda n, p: True)            # 换人：原端口马上空出来，不探
        self.assertEqual((pv["port"], pv["image"], pv["run_cmd"], pv["image_src"], pv["new_person"], pv["ref"]),
                         ("15001", REG + "ubuntu:bb", RUN, "新的人默认", True, "a"))
        self.assertTrue(all(x["ok"] for x in pv["items"]), pv["items"])
        FB.set_person("EE", REG + "ubuntu:bb", RUN, cfg=c)                    # 照新的人默认存：来源记 default
        self.assertEqual(FB.people(c)["EE"]["src"], "default")
        FB.set_person("AA", REG + "ubuntu:dd", RUN, "博士", cfg=c)              # 只改备注：来源不变
        self.assertEqual(FB.people(c)["AA"]["src"], "manual")
        FB.set_person("EE", REG + "ubuntu:zz", RUN, cfg=c)
        pv = FB.preview(c, "open", None, "EE", PUB, tcp=lambda n, p: p == 15151)
        self.assertEqual((pv["port"], pv["sid"], pv["image"], pv["image_src"]), ("15152", "27-15152", REG + "ubuntu:zz", "这人的设置"))
        self.assertFalse(next(x for x in pv["items"] if x["item"] == "配额")["ok"])   # 公用池上限 1
        FB.set_person("EE", remove=True, cfg=c)                                # 没占服务器：能删，删了回到其他作业名之外
        self.assertNotIn("EE", FB.people(c))
        v = FO.view(c)
        self.assertEqual((v["pools"][PUB]["ref"], v["pools"][PUB]["nodes"], v["defaults"]["image"]), ("b", ["root@198.51.100.27"], REG + "ubuntu:bb"))

    def test_people_groups(self):
        """名单：我与工作台（存过的、占着服务器的）；平台上没登记的作业名（ZZ、已停的 HH）不列。"""
        self.sync()
        c = cfg(self.plat.url)
        FB.set_person("EE", REG + "ubuntu:aa", RUN, "新来的", cfg=c)
        ps = FB.people(c)
        self.assertEqual(set(ps), {"ALICE", "AA", "BB", "CC", "EE"})
        from labmon import users as U
        self.me()
        self.assertEqual(U.person_of("alice"), ("ALICE", None))
        self.assertEqual(U.person_of("ee"), (None, None))                    # 没开账号：不按同名去猜（#19）
        self.assertNotIn("me", ps["ALICE"])
        self.assertEqual((ps["ALICE"]["slots"], ps["ALICE"]["image"], ps["ALICE"]["src"]), (["29"], REG + "pt:1", "job"))
        self.assertEqual((ps["CC"]["image"], ps["CC"]["saved"]), (None, False))   # 占着服务器、还没读到作业：先列出来
        self.assertEqual(FB.job_name("ALICE", FB.taken_jobs(c)), "ALICE-2")
        self.assertEqual(FB.job_name("ALICE", FB.taken_jobs(c, "ALICE")), "ALICE")
        self.assertEqual(FB.job_name("EE", FB.taken_jobs(c)), "EE")
        pv = FB.preview(c, "swap", "a", "ALICE", tcp=lambda n, p: True, account="alice")
        self.assertEqual((pv["job"], pv["name"], pv["mine"], pv["image"], pv["image_src"]), ("ALICE-2", "ALICE-2", True, REG + "pt:1", "这人的设置"))
        self.assertIn("自用", pv["after"])
        self.assertIn("加了后缀", pv["items"][0]["note"])
        pv = FB.preview(c, "swap", "29", "DD", tcp=lambda n, p: True)          # 自己的服务器换给别人
        self.assertEqual((pv["port"], pv["job"]), ("15155", "DD"))
        self.assertIn("已归还", pv["after"])
        pv = FB.preview(c, "swap", "a", "CC", tcp=lambda n, p: True)            # CC 还没读到作业：镜像那项提示先核对
        self.assertIsNone(next(x for x in pv["items"] if x["item"] == "镜像")["ok"])

    def test_swap_to_me(self):
        """换给自己：作业名加后缀 ALICE-2、用自己作业的镜像，组内服务器里这台去掉，接成自用主机。"""
        self.sync()
        n = len(self.plat.calls)
        c = cfg(self.plat.url)
        self.me()
        self.assertTrue(FB.preview(c, "swap", "a", "ALICE", account="alice")["mine"])
        self.assertFalse(FB.preview(c, "swap", "a", "ALICE", account="another")["mine"])
        op = self.renew(["a"], code=None, kind="swap", person="ALICE", account="alice")
        self.assertIsNone(op["error"], op)
        sub = self.submits(n)[0]
        role = sub["taskRoleAddDTOList"][0]
        self.assertEqual((sub["taskName"], role["imageName"], role["taskHostPortAddDTOList"][0]["hostPort"]), ("ALICE-2", REG + "pt:1", "15001"))
        self.assertEqual(self.hosts, [("remove", "a"), ("add_mine", "a", "ALICE-2", "root@198.51.100.29", 15001, PRIV, "a（自用）", "ALICE")])
        self.assertTrue(op["conn"]["mine"])
        self.assertEqual((FO.boot()["a"]["holder"], FO.boot()["a"]["job"]), ("ALICE", "ALICE-2"))

    def test_sync_records_stopped_and_renew_clears_it(self):
        """#171：与平台核对把无运行中同名同池作业的记入 platform.json['stopped']（端口不对不算不在运行）；
        job_stopped 校验 after 与 down_since - SYNC_GRACE_S；续租新作业起来后从 stopped 移除。"""
        self.plat.jobs[1]["port"] = "19999"                                    # b 在运行但端口对不上：不算 stopped
        op = self.sync()
        self.assertIsNone(op["error"], op)
        pf = FO.platform_state()
        self.assertEqual(pf["stopped"], ["c"])                                 # 只有 c 无运行中作业；a 正常、b 端口不对均不在 stopped
        pt = pf["t"]
        self.assertEqual(FO.job_stopped("c", after=pt - 10, down_since=pt - 5), (True, pt))
        self.assertEqual(FO.job_stopped("c", after=pt - 10, down_since=pt + 120), (True, pt))      # 300s 容错内
        self.assertEqual(FO.job_stopped("c", after=pt + 1, down_since=pt), (False, None))          # 核对早于借来
        self.assertEqual(FO.job_stopped("c", after=pt - 1000, down_since=pt + 400), (False, None)) # 核对早于掉线前 300s
        self.assertEqual(FO.job_stopped("b", after=pt - 10, down_since=pt), (False, None))
        # c 续租重开（克隆已停的旧作业）后从 stopped 移除
        self.plat.jobs.append({"id": "jc0", "taskName": "CC", "poolName": PUB, "createTime": "2026-11-01 09:00:00",
                               "runningCount": 0, "port": "15003", "image": REG + "ubuntu:bb"})
        self.plat.jobs[1]["runningCount"] = 0                                  # 公用池空出名额（public_max=1）
        self.plat.jobs[2]["runningCount"] = 0
        op = self.renew(["c"], code=None)
        self.assertIsNone(op["error"], op)
        self.assertNotIn("c", FO.platform_state().get("stopped") or [])

    def test_swap_to_me_clears_legacy_borrow_overrides(self):
        """#171 评论 #1：换人接成自用主机时清掉同 id 遗留的 guest（含带 target）与 deadline(target=borrow)，保留其他指令；
        无其他共用 worker_home 的在用主机时，在等上新容器 ssh 后直接在自身上清理。"""
        import subprocess
        wh = Path(self.tmp.name) / "wh"
        ovr = wh / "hosts" / "a" / "overrides.json"
        ovr.parent.mkdir(parents=True)
        Path(os.environ["LABMON_HOSTS"]).write_text(
            f'[[hosts]]\nid = "29"\nssh = "root@198.51.100.29"\nport = 15155\npython = "{sys.executable}"\n'
            f'worker_home = "{wh}"\nplatform_job = "ALICE"\nplatform_pool = "{PRIV}"\n'
        )
        self.sync()
        self.me()

        def write_legacy():
            ovr.write_text(json.dumps([
                {"kind": "guest", "target": "a1,b2", "keep": {"vram": 2.0, "ram": 4.0, "cpu": 1.0, "hold": True}},
                {"kind": "deadline", "target": "borrow", "at": 1900000000},
                {"kind": "pause", "target": "p2", "until": 1900000000},
            ], ensure_ascii=False))

        # 1) 经共用 worker_home 的自用主机 29 清理
        write_legacy()
        via = []

        def ssh_exec(s, cmd, input=None, timeout=60):
            if "overrides.json" in cmd:
                via.append(s["id"])
                r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
                return r.returncode, r.stdout, r.stderr
            return 0, "ok start 123\n", ""

        rem = Remote(down=0)
        rem.ssh = ssh_exec
        op = self.renew(["a"], rem, code=None, kind="swap", person="ALICE", account="alice")
        self.assertIsNone(op["error"], op)
        self.assertEqual(via, ["29"])
        self.assertIn("已清 2 条遗留借用指令", op["steps"][-1]["note"])
        self.assertEqual([(o["kind"], o.get("target")) for o in json.loads(ovr.read_text())], [("pause", "p2")])

        # 2) 其他共用主机连不上（255）→ 等新容器 a 的 ssh 连上后直接在 a 上清理
        write_legacy()
        via.clear()
        FO.SESS["captcha_at"] = 0

        def ssh_only_new(s, cmd, input=None, timeout=60):
            if "overrides.json" in cmd:
                via.append(s["id"])
                if s["id"] == "29":
                    return 255, "", "Connection refused"
                r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
                return r.returncode, r.stdout, r.stderr
            return 0, "ok start 123\n", ""

        rem2 = Remote(down=0)
        rem2.ssh = ssh_only_new
        self.plat.jobs[0]["taskName"] = "AA"                                   # 让旧的 a 可再次换人
        self.plat.jobs[0]["runningCount"] = 1
        op2 = self.renew(["a"], rem2, code=None, kind="swap", person="ALICE", account="alice")
        self.assertIsNone(op2["error"], op2)
        self.assertEqual(via, ["29", "a"])
        self.assertIn("已清 2 条遗留借用指令", op2["steps"][-1]["note"])
        self.assertEqual([(o["kind"], o.get("target")) for o in json.loads(ovr.read_text())], [("pause", "p2")])
        self.assertIn("worker", op["steps"][-1]["note"])

    def test_swap_from_me(self):
        """自己的服务器换给别人：worker 有任务在跑就不动；否则停 ALICE，标成归还，组内服务器加一台。"""
        self.sync()
        op = self.renew(["29"], code=None, kind="swap", person="DD", ack=["29"], probe_mine=lambda h: ("job", "r9 第 3 条"))
        self.assertIn("先在主机页暂停队列", op["error"])
        self.assertEqual(self.paths(), [])
        FO.SESS["captcha_at"] = 0
        n = len(self.plat.calls)
        op = self.renew(["29"], code=None, kind="swap", person="DD", probe_mine=lambda h: ("keepalive", ""))
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.paths(n), [("/workManage/stop", "jx"), ("/submitWork/addWorkTask", "DD")])
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["taskHostPortAddDTOList"][0]["hostPort"], "15155")
        self.assertEqual(self.hosts, [("return_mine", "29"), ("add", "29-15155", "DD", "root@198.51.100.29", 15155, PRIV, "DD")])
        self.assertNotIn("29", FO.boot())
        self.assertEqual(FO.boot()["29-15155"]["holder"], "DD")

    def test_off_mine(self):
        self.sync()
        op = self.renew(["29"], code=None, kind="off", probe_mine=lambda h: ("idle", ""))
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.hosts, [("return_mine", "29")])
        self.assertNotIn("29", F.off_servers())

    def test_reopen_for_me_after_off(self):
        """自用主机关了再给自己开新的（11-09）：平台账号只写在已归还的自用主机上也能登录；上次核对存的运行中作业里删掉已停的 ALICE，
        作业名不加后缀；端口沿用已归还那台的 15155；新作业仍接成自用主机。"""
        self.sync()
        op = self.renew(["29"], code=None, kind="off", probe_mine=lambda h: ("idle", ""))
        self.assertIsNone(op["error"], op)
        Path(os.environ["LABMON_HOSTS"]).write_text(
            f'[[hosts]]\nid = "29"\nreturned = "2026-11-09 18:23"\nssh = "root@198.51.100.29"\nport = 15155\n'
            f'platform = "{self.plat.url}"\nplatform_user = "u"\nplatform_job = "ALICE"\nplatform_pool = "{PRIV}"\n')
        c = cfg(self.plat.url)
        del c["platform"], c["platform_user"]
        self.assertEqual((FO.platform_conf(c), F.legacy_person(c)), ((self.plat.url, "u"), "ALICE"))
        self.assertNotIn("ALICE", FB.taken_jobs(c))
        pv = FB.preview(c, "open", None, "ALICE", PRIV, tcp=lambda n, p: False, account=self.me())
        self.assertEqual((pv["job"], pv["port"], pv["mine"]), ("ALICE", "15155", True))
        self.assertIn("沿用上次的端口", next(x for x in pv["items"] if x["item"] == "端口")["note"])
        self.assertIn("恢复已归还的自用主机 29", pv["after"])                  # #86：会恢复那一块，不是“加一台”
        FO.SESS["captcha_at"] = 0
        n = len(self.plat.calls)
        self.hosts = []
        FO.start("open", c, None, ids=[], person="ALICE", pool=PRIV, probe=self.remote.probe, ssh_fn=self.remote.ssh, forget_fn=self.remote.forget,
                 ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01, tcp=lambda nd, p: False, hosts=self.fake_hosts(), account="alice")
        for t in threading.enumerate():
            if t.name == "fleet-open":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        sub = self.submits(n)[0]
        self.assertEqual((sub["taskName"], sub["taskRoleAddDTOList"][0]["taskHostPortAddDTOList"][0]["hostPort"]), ("ALICE", "15155"))
        self.assertEqual(self.hosts[-1][:4], ("add_mine", "29-15155", "ALICE", "root@198.51.100.29"))

    def test_reopen_for_other_reuses_port(self):
        """给别人开新的（11-09）：这人在这个池里上次关掉的那台的端口优先沿用（AA 关掉的 a 在 15001），别的人照常挑最小的空闲端口。"""
        self.sync()
        op = self.renew(["a"], code=None, kind="off", ack=["a"])
        self.assertIsNone(op["error"], op)
        self.assertIn("a", F.off_servers())
        c = cfg(self.plat.url)
        self.assertEqual(FB.back_port(c, PRIV, "AA"), 15001)
        self.assertIsNone(FB.back_port(c, PUB, "AA"))
        pv = FB.preview(c, "open", None, "AA", PRIV, tcp=lambda n, p: False)
        self.assertEqual((pv["job"], pv["port"]), ("AA", "15001"))
        self.assertNotEqual(FB.preview(c, "open", None, "DD", PRIV, tcp=lambda n, p: False)["port"], "15001")
        FB._TCP.clear()
        pv = FB.preview(c, "open", None, "AA", PRIV, tcp=lambda n, p: p == 15001)     # 上次的端口有东西在听：照常往后挑
        self.assertNotIn(pv["port"], ("15001", None))
        self.assertIn("上次的端口 15001 被占，改用 %s" % pv["port"], next(x for x in pv["items"] if x["item"] == "端口")["note"])   # #86：说明里写上次那个

    def test_reopen_for_me_forgets_own_known_hosts(self):
        """给自己开回来、端口沿用（急修 69a0541 的事后审查）：同一 ssh 与端口恢复已归还的那台自用主机，新容器的主机密钥与旧的不同，
        它拉起 worker 用的那份 known_hosts（fleetboot.mine 给，这里写在主机清单里）里旧容器的条目也要删（与续租同口径），
        不然看板拉起 worker 被 accept-new 拒连。主机清单用真的改写（关机标归还、开回来恢复那一块）。"""
        Path(os.environ["LABMON_HOSTS"]).write_text(
            f'[[hosts]]\nid = "29"\nssh = "root@198.51.100.29"\nport = 15155\nknown_hosts = "/x/kh"\n'
            f'platform_job = "ALICE"\nplatform_pool = "{PRIV}"\n')
        self.sync()
        op = self.renew(["29"], code=None, kind="off", probe_mine=lambda h: ("idle", ""), hosts=None)
        self.assertIsNone(op["error"], op)
        self.assertEqual(C.active_hosts(), [])
        got, sshed = [], []

        def ssh_fn(s, remote, input=None, timeout=60):
            sshed.append(s.get("known_hosts"))
            return self.remote.ssh(s, remote, input, timeout)
        FO.SESS["captcha_at"] = 0
        FO.start("open", cfg(self.plat.url), None, ids=[], person="ALICE", pool=PRIV, probe=self.remote.probe, ssh_fn=ssh_fn,
                 forget_fn=lambda s: got.append(dict(s)), ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01,
                 tcp=lambda nd, p: False, account=self.me())
        for t in threading.enumerate():
            if t.name == "fleet-open":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        h = next(x for x in C.active_hosts() if x["id"] == "29")                 # 恢复了已归还的那一块
        self.assertEqual((h["port"], h["platform_job"]), (15155, "ALICE"))
        self.assertEqual([(s["id"], s["ssh"], s["port"], s.get("known_hosts")) for s in got], [("29", "root@198.51.100.29", 15155, "/x/kh")])
        # 等 ssh、开保活也用这一项（#109）：长连接由它建起，新密钥记进拉起 worker 与实验会话裸 ssh 用的那份，不记进状态目录
        self.assertTrue(sshed and set(sshed) == {"/x/kh"}, sshed)

    def test_swap_mine_other_process_with_ack(self):
        """自用主机上有 labmon 之外的 GPU 进程（fleetboot.mine 归 unknown）：换人、关机提示写清是外来进程，勾「已说好」就放行（以前永远做不了）。"""
        self.sync()
        snap = {"hosts": [{"id": "29", "status": fake_status(gpu_procs=[{"owner": "other", "comm": "python", "mb": 900}])}]}
        save_json(state_dir() / "snapshot.json", snap)
        self.assertEqual(FO._probe_mine("29"), ("unknown", "python"))
        op = self.renew(["29"], code=None, kind="swap", person="DD")
        self.assertIn("有 labmon 之外的 GPU 进程（python）", op["error"])
        self.assertEqual(self.paths(), [])
        FO.SESS["captcha_at"] = 0
        n = len(self.plat.calls)
        op = self.renew(["29"], code=None, kind="off", ack=["29"])
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.paths(n), [("/workManage/stop", "jx")])
        self.assertEqual(FO._probe_mine("nope"), (None, "没探到"))

    def test_pick_port_probes_few(self):
        """挑端口：登记里没占的候选才试连，最多试 TCP_MAX 个；预览里的试连结果按 (节点, 端口) 缓存。"""
        c, node = cfg(self.plat.url), "root@198.51.100.27"
        tried = []
        with self.assertRaises(P.PlatformError) as cm:
            FB.pick_port(c, [node], {node: {15151}}, None, [], lambda n, p: tried.append(p) or True)
        self.assertEqual(tried, list(range(15152, 15152 + FB.TCP_MAX)))
        self.assertIn("没再试", str(cm.exception))
        FB._TCP.clear()
        hits = []
        for _ in range(3):
            self.assertTrue(FB.tcp_cached(node, 15151, tcp=lambda n, p: hits.append(p) or True))
        self.assertEqual(hits, [15151])
        self.assertFalse(FB.tcp_cached(node, 15152, time.time() + FB.TCP_TTL + 1, tcp=lambda n, p: False))

    def test_other_admin_gets_own_host(self):
        self.sync()
        c = cfg(self.plat.url)
        FB.set_person("EE", REG + "ubuntu:aa", RUN, cfg=c)
        self.assertTrue(FB.preview(c, "swap", "a", "EE", account=self.me("ee", "EE"))["mine"])
        op = self.renew(["a"], code=None, kind="swap", person="EE", account="ee")
        self.assertIsNone(op["error"], op)
        self.assertTrue(op["conn"]["mine"])
        self.assertEqual(self.hosts[-1][-1], "EE")

    def test_mine_host_keeps_access_only_for_same_holder(self):
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text('[[hosts]]\nid = "old"\nssh = "root@h"\nport = 15151\nplatform_pool = "p"\n'
                      'platform_job = "ALICE"\nusers = ["amy"]\nlend = true\nreturned = "2026-11-09 20:00"\n'
                      '[fleet]\nme = "ALICE"\n')
        c = F.conf()
        self.assertEqual(FB.back_port(c, "p", "ALICE"), 15151)
        self.assertIsNone(FB.back_port(c, "p", "EE"))
        _, new = C.add_mine_host("new", "EE", "root@h", 15152, "p", holder="EE")
        _, old = C.add_mine_host("newer", "EE-2", "root@h", 15151, "p", holder="EE")
        hs = {h["id"]: h for h in C.all_hosts()}
        self.assertEqual(old, "old")
        self.assertEqual({h["holder"] for h in FB.mine(c)}, {"EE"})
        self.assertNotIn("users", hs[new])
        self.assertNotIn("users", hs[old])
        self.assertNotIn("lend", hs[new])                                   # 入池（#79）：开出来的自用主机一律不入池（维护者 11-18 定）
        self.assertNotIn("lend", hs[old])

    def test_reopened_mine_host_not_in_pool(self):
        # 同一人关了又开回同一台（同 ssh 与端口、恢复那一块）：入池同样不带，要入池由持有人再手动开（维护者 11-18 定，#79）
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text('[[hosts]]\nid = "old"\nssh = "root@h"\nport = 15151\nplatform_pool = "p"\nplatform_holder = "ALICE"\n'
                      'platform_job = "ALICE"\nusers = ["amy"]\nlend = true\nlend_hours = "22:00-09:00"\nreturned = "2026-11-09 20:00"\n')
        _, hid = C.add_mine_host("x", "ALICE", "root@h", 15151, "p", holder="ALICE")
        h = next(h for h in C.all_hosts() if h["id"] == hid)
        self.assertEqual((hid, h.get("users")), ("old", ["amy"]))            # 同一人：开给谁用照旧
        self.assertNotIn("lend", h)
        self.assertNotIn("lend_hours", h)

    def test_reopening_last_legacy_host_keeps_old_borrower(self):
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text('[[hosts]]\nid = "old"\nssh = "root@h"\nport = 15151\nplatform_pool = "p"\n'
                      'platform_job = "ALICE"\nreturned = "2026-11-09 20:00"\n'
                      '[fleet]\npublic_max = 2\n[[fleet.servers]]\nid = "s"\nholder = "AA"\n'
                      'ssh = "root@h"\nport = 15152\npool = "p"\nborrow_since = "2026-11-09 10:00"\n')
        c = F.conf()
        self.assertEqual(F.borrower(c["servers"][0], c), "ALICE")
        C.add_mine_host("old", "EE", "root@h", 15151, "p", holder="EE")
        c = F.conf()
        self.assertEqual(c.get("me"), "ALICE")
        self.assertEqual(F.borrower(c["servers"][0], c), "ALICE")

    def test_disk_keeps_returned_hosts_in_people(self):
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text('[[hosts]]\nid = "old"\nssh = "root@h"\nport = 15151\nplatform_pool = "p"\n'
                      'platform_job = "ALICE"\nreturned = "2026-11-09 20:00"\n'
                      '[fleet]\nme = "ALICE"\n')
        self.assertEqual(DK.owner_of("alice", {}, DK._names(F.conf())), "ALICE")

    def test_mine_host_config(self):
        """主机清单：照抄自用主机加一台（不带原主机的整行注释），同 ssh 与端口的已归还主机就恢复它；job 行随换人加、去。"""
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text('[[hosts]]\nid = "29"\nlabel = "29（A6000）"\n# 原主机的说明\nssh = "root@h"\nport = 15155\nworker = "auto"   # 自动拉起\n'
                      'platform_job = "ALICE"\nplatform_pool = "p"\n\n[[hosts.checks]]\nid = "keepalive"\ncmd = "ka running"\n\n'
                      '[fleet]\npublic_max = 2\n\n[[fleet.servers]]\nid = "a"\nholder = "AA"\nssh = "root@h"\nport = 15001\npool = "p"\n')
        _, hid = C.add_mine_host("h-15158", "ALICE-2", "root@h", 15158, "p", "h-15158（自用）")
        hs = {h["id"]: h for h in C._read_toml(hf)["hosts"]}
        self.assertEqual(hid, "h-15158")
        self.assertEqual((hs[hid]["platform_job"], hs[hid]["port"], hs[hid]["worker"], hs[hid]["checks"][0]["id"]), ("ALICE-2", 15158, "auto", "keepalive"))
        self.assertEqual(hf.read_text().count("# 原主机的说明"), 1)
        self.assertEqual(hs["29"]["port"], 15155)
        C.set_returned(hid, "2026-11-05 20:00")
        _, hid2 = C.add_mine_host("other", "ALICE-3", "root@h", 15158, "p")
        hs = {h["id"]: h for h in C._read_toml(hf)["hosts"]}
        self.assertEqual((hid2, hs[hid].get("returned"), hs[hid]["platform_job"]), (hid, None, "ALICE-3"))
        with self.assertRaises(C.ConfigError):
            C.add_mine_host("x", "ALICE-4", "root@h", 15158, "p")                # 在用的主机
        C.set_fleet_holder("a", "ALICE", "ALICE-2")
        self.assertEqual([(x["holder"], x["job"]) for x in F.conf(hf)["servers"]], [("ALICE", "ALICE-2")])
        C.set_fleet_holder("a", "DD")
        self.assertNotIn("\njob =", hf.read_text())

    def test_session_restore(self):
        self.sync()
        tok, uid = FO.SESS["cli"].token, FO.SESS["cli"].user_id
        FO.logout()
        self.assertFalse(FO.session()["logged_in"])
        FO.SESS["tried"] = False
        saved = P.load_session

        def fake(base, user, now=None):
            cli = P.Client(base)
            cli.token, cli.user_id = tok, uid
            return cli, 1
        P.load_session = fake
        try:
            with unittest.mock.patch.object(F, "conf", lambda *a: cfg(self.plat.url)):
                self.assertTrue(FO.session()["logged_in"])                     # 服务重启后从钥匙串接着用
            op = self.sync(code=None)
            self.assertIsNone(op["error"], op)
        finally:
            P.load_session = saved
        self.assertEqual(P.token_exp("x.eyJleHAiOjE3OTM5MDAwMDB9.y"), 1793900000.0)
        self.assertEqual(P.token_exp("bad", 100), 100 + P.TOKEN_TTL)

    def test_set_fleet_holder(self):
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text('[[hosts]]\nid = "29"\nssh = "root@h"\n\n[fleet]\npublic_max = 2\n\n[[fleet.servers]]\nid = "a"\n'
                      'holder = "AA"   # 平台作业名\nssh = "root@h"\nport = 15001\npool = "p"\n\n'
                      '[[fleet.servers]]\nid = "b"\nholder = "BB"\nssh = "root@h"\nport = 15002\npool = "p"\n')
        C.set_fleet_holder("b", "DD")
        t = hf.read_text()
        self.assertIn('holder = "AA"   # 平台作业名', t)
        self.assertIn('holder = "DD"', t)
        self.assertEqual([x["holder"] for x in F.conf(hf)["servers"]], ["AA", "DD"])
        with self.assertRaises(C.ConfigError):
            C.set_fleet_holder("zz", "DD")

    def _multi_spec_pub(self, avail_a=1, avail_b=2):
        """公用池配两个物理节点规格（nodeA→.27，nodeB→.28），BB 的作业在 nodeA 上。"""
        self.plat.specs[PID[PUB]] = [
            {"specificationId": "sA", "specificationName": "nodeA-1GPU", "quantityAvailable": avail_a, "specificationAmount": 4, "isSchedule": 0, "queueNum": 0},
            {"specificationId": "sB", "specificationName": "nodeB-1GPU", "quantityAvailable": avail_b, "specificationAmount": 4, "isSchedule": 0, "queueNum": 0},
        ]
        self.plat.spec_ip = {"sA": "192.0.2.27", "sB": "192.0.2.28"}
        next(j for j in self.plat.jobs if j["id"] == "j2")["spec"] = "sA"
        next(j for j in self.plat.jobs if j["id"] == "jz")["spec"] = "sA"

    def test_open_and_swap_choose_spec(self):
        """开机（开新的、重开已关机）与换人可选规格：缺省沿用原规格，改选后提交带新的 specificationId 并落到新节点。"""
        self._multi_spec_pub(avail_a=1, avail_b=2)
        self.sync()
        c = cfg(self.plat.url)
        c["public_max"] = 4
        v = FO.view(c)
        self.assertEqual(v["boot"]["b"]["spec"], "sA")
        self.assertEqual([x["specificationId"] for x in v["pools"][PUB]["specs"]], ["sA", "sB"])
        FB.set_defaults(REG + "ubuntu:bb", RUN)
        # 预览：默认原规格 sA，可改选 sB
        pv_def = FB.preview(c, "open", None, "EE", PUB, tcp=lambda n, p: False)
        self.assertEqual((pv_def["spec"], pv_def["orig_spec"], pv_def["spec_name"]), ("sA", "sA", "nodeA-1GPU"))
        pv_b = FB.preview(c, "open", None, "EE", PUB, tcp=lambda n, p: False, spec="sB")
        self.assertEqual((pv_b["spec"], pv_b["orig_spec"], pv_b["spec_name"], pv_b["sid"]), ("sB", "sA", "nodeB-1GPU", None))
        self.assertTrue(all(x["ok"] for x in pv_b["items"]), pv_b["items"])
        # 开新的选 sB：提交带 sB，落到 192.0.2.28 → 新 id 28-15151
        self.remote = Remote()
        n = len(self.plat.calls)
        self.hosts = []
        FO.start("open", c, None, ids=[], person="EE", pool=PUB, spec="sB", probe=self.remote.probe, ssh_fn=self.remote.ssh,
                 forget_fn=self.remote.forget, ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01,
                 tcp=lambda nd, p: False, hosts=self.fake_hosts())
        for t in threading.enumerate():
            if t.name == "fleet-open":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["specificationId"], "sB")
        self.assertEqual(self.hosts, [("add", "28-15151", "EE", "root@198.51.100.28", 15151, PUB, "EE")])
        self.assertEqual((op["conn"]["sid"], op["conn"]["cmd"], op["conn"]["spec"]), ("28-15151", "ssh -p 15151 root@198.51.100.28", "nodeB-1GPU"))
        self.assertEqual(FO.view(c)["boot"]["28-15151"]["spec"], "sB")
        # 换人改选 sB：停 b（sA）后按 sB 开给 DD，主机清单新加 28-15002、删旧 b
        FB.set_person("DD", REG + "ubuntu:dd", RUN, cfg=c)
        n = len(self.plat.calls)
        self.hosts = []
        FO.start("swap", c, None, ids=["b"], person="DD", spec="sB", ack=[], probe=self.remote.probe, ssh_fn=self.remote.ssh,
                 forget_fn=self.remote.forget, ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01,
                 tcp=lambda nd, p: False, hosts=self.fake_hosts())
        for t in threading.enumerate():
            if t.name == "fleet-swap":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["specificationId"], "sB")
        self.assertEqual(self.hosts, [("add", "28-15002", "DD", "root@198.51.100.28", 15002, PUB, "DD"), ("remove", "b")])
        self.assertNotIn("b", FO.boot())
        self.assertEqual(FO.view(c)["boot"]["28-15002"]["spec"], "sB")
        # 关机后重开改选 sA：停掉 n2 并用旧 id 重开（这里直接测试对已关机的 b 重开选 sB）
        F.edit_state("boot.json", lambda b: b.update({"c": {**b["28-15002"], "off": {"t": 1, "holder": "CC"}, "holder": "CC"}}))
        n = len(self.plat.calls)
        self.hosts = []
        FO.start("open", c, None, ids=["c"], person="CC", spec="sA", probe=self.remote.probe, ssh_fn=self.remote.ssh,
                 forget_fn=self.remote.forget, ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01,
                 tcp=lambda nd, p: False, hosts=self.fake_hosts())
        for t in threading.enumerate():
            if t.name == "fleet-open":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["specificationId"], "sA")

    def test_spec_unavailable_blocks_and_same_spec_freed(self):
        """目标规格无空位时（开新的、换人改选规格、换节点）预览与提交前均拦截、平台上什么都不动；换人沿用原规格空位 0 仍放行（freed=True）。"""
        self._multi_spec_pub(avail_a=0, avail_b=0)
        self.sync()
        c = cfg(self.plat.url)
        c["public_max"] = 4
        FB.set_person("DD", REG + "ubuntu:dd", RUN, cfg=c)
        # 预览：换人改选无空位的 sB → 规格 ok=False；沿用原规格 sA → freed=True，规格 ok=True；换节点选 sB → ok=False；未知规格 → ok=False
        pv_swap_b = FB.preview(c, "swap", "b", "DD", tcp=lambda n, p: False, spec="sB")
        self.assertFalse(next(x for x in pv_swap_b["items"] if x["item"] == "规格")["ok"])
        pv_swap_a = FB.preview(c, "swap", "b", "DD", tcp=lambda n, p: False)
        self.assertTrue(next(x for x in pv_swap_a["items"] if x["item"] == "规格")["ok"])
        pv_move_b = FB.preview(c, "move", "b", tcp=lambda n, p: False, spec="sB")
        self.assertFalse(next(x for x in pv_move_b["items"] if x["item"] == "规格")["ok"])
        pv_bad = FB.preview(c, "open", None, "DD", PUB, tcp=lambda n, p: False, spec="nope")
        bad_item = next(x for x in pv_bad["items"] if x["item"] == "规格")
        self.assertFalse(bad_item["ok"])
        self.assertIn("池里没有规格 nope", bad_item["note"])
        # 提交前实时核对：swap 改选 sB（空 0）与 move 选 sB（空 0）均拒绝，平台上什么都不动
        self.remote = Remote()
        for kind, kw in (("swap", {"person": "DD", "spec": "sB"}), ("move", {"spec": "sB"})):
            n = len(self.plat.calls)
            self.hosts = []
            FO.start(kind, c, None, ids=["b"], ack=[], probe=self.remote.probe, ssh_fn=self.remote.ssh, forget_fn=self.remote.forget,
                     ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01, tcp=lambda nd, p: False, hosts=self.fake_hosts(), **kw)
            for t in threading.enumerate():
                if t.name == f"fleet-{kind}":
                    t.join(10)
            op = FO.op()
            self.assertIn("规格：nodeB-1GPU", op["error"])
            self.assertIn("什么都没动", op["error"])
            self.assertEqual(self.paths(n), [])
        # swap 沿用原规格 sA（空 0，但停旧释放同规格）：照常放行
        n = len(self.plat.calls)
        self.hosts = []
        FO.start("swap", c, None, ids=["b"], person="DD", ack=[], probe=self.remote.probe, ssh_fn=self.remote.ssh, forget_fn=self.remote.forget,
                 ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01, tcp=lambda nd, p: False, hosts=self.fake_hosts())
        for t in threading.enumerate():
            if t.name == "fleet-swap":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.paths(n), [("/workManage/stop", "j2"), ("/submitWork/addWorkTask", "DD")])

    def test_move_server_changes_id_port_and_drops_lend(self):
        """组内服务器同池换节点：先停后开换规格，主机清单新加删旧、不带原 lend，原端口空闲时沿用、目标节点被占时改挑空闲端口，给新连接命令。"""
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text(
            f'[[hosts]]\nid = "29"\nssh = "root@198.51.100.29"\nport = 15155\nplatform_job = "ALICE"\nplatform_pool = "{PRIV}"\n\n'
            f'[fleet]\nprivate_pools = ["{PRIV}"]\npublic_max = 4\nplatform = "{self.plat.url}"\nplatform_user = "u"\n'
            f'keepalive_cmd = "KA_CALLER=labmon-fleet bash /x/ka.sh"\n\n'
            f'[[fleet.servers]]\nid = "a"\nholder = "AA"\nssh = "root@198.51.100.29"\nport = 15001\npool = "{PRIV}"\n\n'
            f'[[fleet.servers]]\nid = "27-15002"\nholder = "BB"\nssh = "root@198.51.100.27"\nport = 15002\npool = "{PUB}"\nlend = true\nlend_hours = "22:00-09:00"\n'
        )
        self._multi_spec_pub(avail_a=1, avail_b=2)
        c = F.conf()
        FO.captcha(c)
        FO.SESS["captcha_at"] = 0
        FO.start("sync", c, "q7Wz", password=lambda u: "pw", pace=0)
        for t in threading.enumerate():
            if t.name == "fleet-sync":
                t.join(10)
        self.assertIsNone(FO.op()["error"])
        # 第一次换节点：27-15002 → sB（28），原端口 15002 在 28 空闲 → 28-15002，lend 不带过去
        self.remote = Remote()
        n = len(self.plat.calls)
        FO.start("move", F.conf(), None, ids=["27-15002"], spec="sB", ack=[], probe=self.remote.probe, ssh_fn=self.remote.ssh,
                 forget_fn=self.remote.forget, ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01, tcp=lambda nd, p: False)
        for t in threading.enumerate():
            if t.name == "fleet-move":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertEqual([x["name"] for x in op["steps"]], ["登录", "核对", "BB：停止作业", "BB：提交新作业、等运行", "BB：等 ssh、开保活"])
        self.assertEqual(self.paths(n), [("/workManage/stop", "j2"), ("/submitWork/addWorkTask", "BB")])
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["specificationId"], "sB")
        srvs = {s["id"]: s for s in F.conf()["servers"]}
        self.assertNotIn("27-15002", srvs)
        self.assertIn("28-15002", srvs)
        self.assertEqual((srvs["28-15002"]["holder"], srvs["28-15002"]["ssh"], srvs["28-15002"]["port"], srvs["28-15002"].get("lend")),
                         ("BB", "root@198.51.100.28", 15002, None))
        self.assertNotIn("27-15002", FO.boot())
        self.assertEqual((FO.boot()["28-15002"]["src"], FO.view()["boot"]["28-15002"]["spec"]), ("换节点", "sB"))
        self.assertEqual((op["conn"]["sid"], op["conn"]["cmd"], op["conn"]["spec"]), ("28-15002", "ssh -p 15002 root@198.51.100.28", "nodeB-1GPU"))
        evs = records(who="BB")
        self.assertTrue(any(e["kind"] == "fleet_boot" and "换节点" in e["msg"] and "原节点 27-15002" in e["msg"] for e in evs), evs)
        # 第二次换节点：28-15002 → sA（27），但 27 上 15002 被占 → 自动挑 15151，id 变 27-15151
        n = len(self.plat.calls)
        FO.start("move", F.conf(), None, ids=["28-15002"], spec="sA", ack=[], probe=self.remote.probe, ssh_fn=self.remote.ssh,
                 forget_fn=self.remote.forget, ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01,
                 tcp=lambda nd, p: (nd, p) == ("root@198.51.100.27", 15002))
        for t in threading.enumerate():
            if t.name == "fleet-move":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["taskHostPortAddDTOList"][0]["hostPort"], "15151")
        srvs = {s["id"]: s for s in F.conf()["servers"]}
        self.assertNotIn("28-15002", srvs)
        self.assertIn("27-15151", srvs)
        self.assertEqual(op["conn"]["cmd"], "ssh -p 15151 root@198.51.100.27")

    def test_move_mine_host_by_other_admin_keeps_mine_and_drops_lend(self):
        """自用主机换节点（哪怕由另一管理员操作）：仍保持原持有人的自用主机；旧台标归还、新台不自动入池、last_mine 指向旧台；worker 在跑时拒绝。"""
        hf = Path(os.environ["LABMON_HOSTS"])
        hf.write_text(
            f'[[hosts]]\nid = "27-15155"\nssh = "root@198.51.100.27"\nport = 15155\nworker = "auto"\n'
            f'platform_holder = "ALICE"\nplatform_job = "ALICE"\nplatform_pool = "{PUB}"\nlend = true\nlend_hours = "22:00-09:00"\n\n'
            f'[fleet]\nprivate_pools = ["{PRIV}"]\npublic_max = 4\nplatform = "{self.plat.url}"\nplatform_user = "u"\n'
            f'keepalive_cmd = "KA_CALLER=labmon-fleet bash /x/ka.sh"\n\n'
            f'[[fleet.servers]]\nid = "b"\nholder = "BB"\nssh = "root@198.51.100.27"\nport = 15002\npool = "{PUB}"\n'
        )
        next(j for j in self.plat.jobs if j["id"] == "jx").update(poolName=PUB, spec="sA")
        self._multi_spec_pub(avail_a=1, avail_b=2)
        bob = self.me("bob", "BOB")
        c = F.conf()
        FO.captcha(c)
        FO.SESS["captcha_at"] = 0
        FO.start("sync", c, "q7Wz", password=lambda u: "pw", pace=0)
        for t in threading.enumerate():
            if t.name == "fleet-sync":
                t.join(10)
        # 另一管理员 bob 预览 ALICE 的自用主机换节点：mine 仍为 True
        pv = FB.preview(F.conf(), "move", "27-15155", tcp=lambda n, p: False, account=bob, spec="sB")
        self.assertTrue(pv["mine"])
        self.assertEqual((pv["person"], pv["spec"], pv["orig_spec"]), ("ALICE", "sB", "sA"))
        # worker 有任务在跑：即使勾 ack 也拒绝
        self.remote = Remote()
        FO.start("move", F.conf(), None, ids=["27-15155"], spec="sB", ack=["27-15155"], account=bob,
                 probe_mine=lambda h: ("job", "exp1"), ssh_fn=self.remote.ssh, forget_fn=self.remote.forget,
                 ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01, tcp=lambda nd, p: False)
        for t in threading.enumerate():
            if t.name == "fleet-move":
                t.join(10)
        self.assertIn("先在主机页暂停队列", FO.op()["error"])
        # 空闲时由 bob 触发换节点：旧 27-15155 标归还，新 28-15155 仍归 ALICE 为自用主机、不带 lend，last_mine 指向 27-15155
        n = len(self.plat.calls)
        FO.start("move", F.conf(), None, ids=["27-15155"], spec="sB", ack=[], account=bob,
                 probe_mine=lambda h: ("keepalive", ""), ssh_fn=self.remote.ssh, forget_fn=self.remote.forget,
                 ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01, tcp=lambda nd, p: False)
        for t in threading.enumerate():
            if t.name == "fleet-move":
                t.join(10)
        op = FO.op()
        self.assertIsNone(op["error"], op)
        self.assertEqual(self.paths(n), [("/workManage/stop", "jx"), ("/submitWork/addWorkTask", "ALICE")])
        self.assertEqual(self.submits(n)[0]["taskRoleAddDTOList"][0]["specificationId"], "sB")
        hs = {h["id"]: h for h in C.all_hosts()}
        self.assertTrue(hs["27-15155"].get("returned"))
        self.assertFalse(hs["28-15155"].get("returned"))
        self.assertEqual((hs["28-15155"]["platform_holder"], hs["28-15155"]["ssh"], hs["28-15155"]["port"], hs["28-15155"].get("lend")),
                         ("ALICE", "root@198.51.100.28", 15155, None))
        self.assertEqual(FB.last_mine(PUB, skip="28-15155", cfg=F.conf(), holder="ALICE")["id"], "27-15155")
        self.assertTrue(op["conn"]["mine"])
        self.assertEqual(op["conn"]["cmd"], "ssh -p 15155 root@198.51.100.28")

    def test_move_guards_and_errors(self):
        """换节点的各种前置拦截、风险确认、同规格拒绝、提交失败说明，以及未知节点 IP 不误落旧节点。"""
        self._multi_spec_pub(avail_a=1, avail_b=2)
        self.sync()
        c = cfg(self.plat.url)
        c["public_max"] = 4
        # 缺 spec / 换人 / 借用中 / 已关机
        with self.assertRaises(P.PlatformError) as cm:
            FO.start("move", c, ids=["b"])
        self.assertIn("目标规格", str(cm.exception))
        with self.assertRaises(P.PlatformError) as cm:
            FO.start("move", c, ids=["b"], spec="sB", person="DD")
        self.assertIn("不换使用人", str(cm.exception))
        c_bor = cfg(self.plat.url)
        c_bor["servers"][1]["borrow_since"] = "2026-11-06 10:00"
        with self.assertRaises(P.PlatformError) as cm:
            FO.start("move", c_bor, ids=["b"], spec="sB")
        self.assertIn("先在它的「借用」里还掉", str(cm.exception))
        self.assertIn("再换节点", str(cm.exception))
        # 预览：未选规格 / 选了当前规格
        pv0 = FB.preview(c, "move", "b", tcp=lambda n, p: False)
        self.assertFalse(next(x for x in pv0["items"] if x["item"] == "规格")["ok"])
        pv_same = FB.preview(c, "move", "b", tcp=lambda n, p: False, spec="sA")
        self.assertIn("续租", next(x for x in pv_same["items"] if x["item"] == "规格")["note"])
        # 后端选了当前规格：拒绝且什么都不动
        n = len(self.plat.calls)
        op = self.renew(["b"], code=None, kind="move", spec="sA")
        self.assertIn("续租", op["error"])
        self.assertEqual(self.paths(n), [])
        # 在跑实验 / 本地盘写超 1 GiB 没勾确认：拒绝
        n = len(self.plat.calls)
        op = self.renew(["b"], Remote(states={"b": ("job", "train")}), code=None, kind="move", spec="sB")
        self.assertIn("没确认", op["error"])
        self.assertEqual(self.paths(n), [])
        self.local_disk("b")
        n = len(self.plat.calls)
        op = self.renew(["b"], code=None, kind="move", spec="sB")
        self.assertIn("换节点会清空", op["error"])
        self.assertEqual(self.paths(n), [])
        self.local_disk("b", 0.2)
        # 停旧后提交被拒：_note 给续开指引
        self.assertIn("点「开机」", FO._note("move", c["servers"][1], "BB", {"stopped": True}))
        self.assertIn("点「+ 开新的」", FO._note("move", {"id": "29", "label": "29", "holder": "ALICE", "mine": True}, "ALICE", {"stopped": True}))
        # 新容器落在未知网段的不同 IP（ext_host 换算不出）：不误落回旧节点
        Path(os.environ["LABMON_HOSTS"]).write_text("")
        c_odd = fleet_cfg(private_pools=[PRIV], public_max=4, platform=self.plat.url, platform_user="u",
                          servers=[{"id": "b", "holder": "BB", "job": "BB", "ssh": "root@node-a", "port": 15002, "pool": PUB}])
        self.plat.spec_ip["sB"] = "10.99.0.50"
        self.remote = Remote()
        self.hosts = []
        FO.start("move", c_odd, None, ids=["b"], spec="sB", ack=[], probe=self.remote.probe, ssh_fn=self.remote.ssh,
                 forget_fn=self.remote.forget, ssh_poll=0.01, password=lambda u: "pw", pace=0, poll=0.01, fast=0.01,
                 tcp=lambda nd, p: False, hosts=self.fake_hosts())
        for t in threading.enumerate():
            if t.name == "fleet-move":
                t.join(10)
        self.assertIn("不知道本机怎么连它", FO.op()["error"])


if __name__ == "__main__":
    unittest.main()
