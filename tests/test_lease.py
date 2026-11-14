"""服务租约（lease.py）与冷备（backup.py），#163。

两个看板服务实例 = 两个 LABMON_STATE，共用一个 worker_home（临时目录，本机 bash 跑租约脚本，不连 ssh）。

    python -m unittest tests.test_lease -v
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402, F401 — 设测试环境
from fixtures import login, make_token, post_req  # noqa: E402
from labmon import backup as BK  # noqa: E402
from labmon import config  # noqa: E402
from labmon import fleetops as FO  # noqa: E402
from labmon import lease as LS  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import platform as PF  # noqa: E402
from labmon import serve as SV  # noqa: E402
from labmon import service as S  # noqa: E402
from labmon import workerctl as W  # noqa: E402
from labmon.config import ConfigError  # noqa: E402
from labmon.records import records  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.wh = self.d / "wh"
        (self.wh / "hosts" / "h1").mkdir(parents=True)
        self.hosts = self.d / "hosts.toml"
        self.hosts.write_text(f'[[hosts]]\nid = "h1"\nssh = "root@192.0.2.9"\nworker_home = "{self.wh}"\npython = "{sys.executable}"\n')
        self.env = mock.patch.dict(os.environ, {"LABMON_HOSTS": str(self.hosts)})
        self.env.start()
        self.addCleanup(self.env.stop)
        ah = mock.patch.object(LS, "active_hosts", lambda: [self.h()])     # 备用时的 tick、释放、--take 读的在用主机：本机跑（主机清单里的带 ssh）
        ah.start()
        self.addCleanup(ah.stop)
        for n, i in (("a", "a-1"), ("b", "b-1")):              # 两台的身份写死，比大小可预期（实际是机器名 + 随机串）
            (self.d / n).mkdir()
            (self.d / n / "server.json").write_text(json.dumps({"id": i}))
        self.use("a")

    def tearDown(self):
        self.tmp.cleanup()

    def use(self, n: str) -> None:
        os.environ["LABMON_STATE"] = str(self.d / n)
        O.reset_sent()

    def h(self, wh: Path | None = None) -> dict:
        return {"id": "h1", "worker_home": str(wh or self.wh), "python": sys.executable, "ssh": None, "port": 22}

    def lease_file(self, wh: Path | None = None) -> dict:
        return json.loads(((wh or self.wh) / LS.FILE).read_text())

    def set_lease(self, holder: str, age: float, wh: Path | None = None) -> None:
        ((wh or self.wh) / LS.FILE).write_text(json.dumps({"holder": holder, "renew": int(time.time() - age), "ttl": LS.TTL}))


class RemoteTest(Base):
    def test_modes(self):
        # 远端脚本：没人持有就拿；别人在续的不动；只放自己的；坏文件不自动抢、--take 才写；过了 ttl 的能拿
        r = LS._run(self.h(), "renew", "a-1")
        self.assertEqual((r["lease"]["holder"], r["bad"]), ("a-1", None))
        r = LS._run(self.h(), "renew", "b-1")
        self.assertEqual(r["lease"]["holder"], "a-1")
        LS._run(self.h(), "release", "b-1")
        self.assertEqual(self.lease_file()["holder"], "a-1")
        LS._run(self.h(), "release", "a-1")
        self.assertEqual(self.lease_file()["holder"], "")
        self.assertEqual(LS._run(self.h(), "renew", "b-1")["lease"]["holder"], "b-1")
        self.set_lease("a-1", LS.TTL + 5)                         # a 被杀、没释放：过了 ttl
        self.assertEqual(LS._run(self.h(), "renew", "b-1")["lease"]["holder"], "b-1")
        (self.wh / LS.FILE).write_text("{oops")
        r = LS._run(self.h(), "renew", "a-1")
        self.assertTrue(r["bad"])
        self.assertEqual((self.wh / LS.FILE).read_text(), "{oops")
        self.assertEqual(LS._run(self.h(), "take", "a-1")["lease"]["holder"], "a-1")

    def test_clock_from_shared_disk(self):
        # 同一个 worker_home 下几台的时钟不一样（Codex #188）：一台快了 700 秒，读到别人刚续的租约也不算过期；时刻取共享盘的
        LS._run(self.h(), "renew", "a-1")
        fast = "import time as _t\n_o = _t.time\n_t.time = lambda: _o() + 700\n" + LS.REMOTE_PY
        r = subprocess.run([sys.executable, "-c", fast, str(self.wh / LS.FILE), "b-1", "renew", str(LS.TTL)], capture_output=True, text=True)
        self.assertEqual((r.returncode, LS.parse(r.stdout)["lease"]["holder"]), (3, "a-1"))
        self.assertLess(abs(LS.parse(r.stdout)["now"] - time.time()), 60)

    def test_fetch_carries_lease_and_gates_put(self):
        # 续约夹在 fetch 那条命令里：拿到了才写 put；别人持有时一个文件都不写
        h = dict(self.h(), put={"sched.json": {"weight": {"p": 2}}}, lease="renew")
        raw, err = O.fetch(h)
        self.assertEqual((err, O.lease_part(raw)["lease"]["holder"]), (None, "a-1"))
        self.assertTrue((self.wh / "hosts" / "h1" / "sched.json").exists())
        (self.wh / "hosts" / "h1" / "sched.json").unlink()
        self.use("b")
        raw, err = O.fetch(dict(h, put={"sched.json": {"weight": {"p": 3}}}))
        self.assertEqual(O.lease_part(raw)["lease"]["holder"], "a-1")
        self.assertFalse((self.wh / "hosts" / "h1" / "sched.json").exists())
        self.assertEqual(O._SENT, {})


class TwoServersTest(Base):
    def test_only_primary_collects(self):
        # 两个看板服务：先跑的成为主、写快照与 worker 的输入；另一台读出租约在别人手里，不写快照、不写 worker 的文件
        snap = O.collect([], use_ssh=False)
        self.assertEqual((snap["server"]["role"], LS.flag()), ("primary", True))
        self.assertTrue((self.wh / "hosts" / "h1" / "sched.json").exists())
        (self.wh / "hosts" / "h1" / "sched.json").unlink()
        self.use("b")
        self.assertIsNone(O.collect([], use_ssh=False))
        self.assertFalse((self.d / "b" / "overview" / "snapshot.json").exists())
        self.assertFalse((self.wh / "hosts" / "h1" / "sched.json").exists())
        self.assertEqual((LS.flag(), LS.view()["holder"]), (False, "a-1"))
        self.assertIn("主是 a-1", LS.text())
        self.assertEqual([e["kind"] for e in records()], ["server"])          # 记一条“转为备用”
        # a 停服务时释放：b 下一轮就接管，不用等 ttl
        self.use("a")
        LS.release()
        self.use("b")
        self.assertEqual(LS.tick(), "primary")
        self.use("a")
        self.assertIsNone(O.collect([], use_ssh=False))                        # a 再起来：租约在 b 手里，a 转为备用
        self.assertFalse(LS.primary())
        # b 被杀、没释放：过了 ttl 才轮到 a
        self.set_lease("b-1", 60)
        self.assertEqual(LS.tick(), "standby")
        self.set_lease("b-1", LS.TTL + 5)
        self.assertEqual(LS.tick(), "primary")

    def test_standby_round_does_nothing(self):
        # 备用的一轮：只读租约，不采集、不通知、不拉起 worker；拿到了接着采这一轮
        self.set_lease("a-1", 0)
        self.use("b")
        self.assertEqual(LS.tick(), "standby")
        ov = SV.Overview([])
        with mock.patch.object(SV, "notify") as nt, mock.patch.object(O, "relaunch") as rl, mock.patch.object(O, "collect", wraps=O.collect) as co:
            ov.once()
            self.assertEqual((nt.called, rl.called, co.called), (False, False, False))
            self.assertFalse(LS.primary())
            self.set_lease("a-1", LS.TTL + 5)
            ov.once()
            self.assertEqual((co.called, nt.called), (True, True))
        self.assertTrue(LS.primary())
        self.assertTrue((self.d / "b" / "overview" / "snapshot.json").exists())

    def test_unreadable_keeps_marker(self):
        # 读不到租约（主机都连不上、不在内网）：照本机标记，不因为超过 ttl 没续上就降为备用（只有一台主机、它停机时还要能续租）
        self.assertEqual(LS.decide({str(self.wh): []}, [str(self.wh)]), "primary")
        LS.set_marker(False, "测试")
        self.assertEqual(LS.decide({str(self.wh): [None]}, [str(self.wh)]), "standby")
        self.assertEqual(LS.op("renew", [dict(self.h(), ssh="root@192.0.2.9")]), {str(self.wh): None})   # LABMON_LAN=off：远程的不连

    def test_split_homes_larger_id_releases(self):
        # 两个 worker_home 分在两台手里：id 大的放掉手上的，id 小的下一轮两边都拿到
        wh2 = self.d / "wh2"
        hs = [self.h(), dict(self.h(wh2), id="h2")]
        self.set_lease("a-1", 0)
        wh2.mkdir()
        self.set_lease("b-1", 0, wh2)
        self.assertEqual(LS.tick(hs), "standby")                              # a 的 id 小：不放
        self.assertEqual(self.lease_file()["holder"], "a-1")
        self.use("b")
        self.assertEqual(LS.tick(hs), "standby")
        self.assertEqual(self.lease_file(wh2)["holder"], "")
        self.use("a")
        self.assertEqual(LS.tick(hs), "primary")

    def test_bad_file_not_taken(self):
        # 租约文件写坏了：两边都不自动抢，转为备用并推一条（force）；--take 写成自己
        (self.wh / LS.FILE).write_text("garbage")
        with mock.patch.object(S, "notify") as nt:
            self.assertEqual(LS.tick(), "standby")
        self.assertEqual(nt.call_args.kwargs, {"force": True})
        self.assertIn("写坏了", LS.text())
        self.assertEqual((self.wh / LS.FILE).read_text(), "garbage")
        with contextlib.redirect_stdout(io.StringIO()):
            LS.cli(["--take"])
        self.assertEqual((self.lease_file()["holder"], LS.primary()), ("a-1", True))


class GatesTest(Base):
    def setUp(self):
        super().setUp()
        self.set_lease("b-1", 0)
        LS.decide({str(self.wh): [LS._run(self.h(), "read", "a-1")]}, [str(self.wh)])
        self.assertFalse(LS.primary())

    def test_actions_refused(self):
        # 备用时：平台操作、改 worker、临时指令都不做；只看的照做
        with self.assertRaises(PF.PlatformError) as e:
            FO.start("sync", {"servers": []})
        self.assertIn("主是 b-1", str(e.exception))
        for sub in W.CHANGE:
            with self.assertRaises(ConfigError):
                W.run_out(sub, "h1", ["/r", "jobs/q.txt"])
        with self.assertRaises(ConfigError):
            W.override_each("add", "pause", "p", ["h1"])
        with mock.patch.object(W, "ssh", return_value=(0, "ok")):
            self.assertEqual(W.run_out("status", "h1", []), (0, "ok"))

    def test_notify_quiet(self):
        # 备用不发系统通知（主那台发）；租约自己的那条 force 照发
        with mock.patch.dict(os.environ), mock.patch.object(S, "_popup") as pop, mock.patch.object(S, "push_conf", return_value=None):
            os.environ.pop("LABMON_NO_NOTIFY", None)
            S.notify(["x 掉线"])
            self.assertFalse(pop.called)
            S.notify(["转为备用"], force=True)
            self.assertTrue(pop.called)

    def test_http(self):
        # 看板服务：写请求回 409 说主是谁；登录、账号、部署照收；/healthz 的 primary 是 false；页面数据带此刻的主备
        srv = SV.Server(("127.0.0.1", 0), [])
        login(self)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        self.assertEqual(json.loads(urllib.request.urlopen(base + "/healthz").read())["primary"], False)
        for path in ("/collect", "/sched/pause", "/fleet/op/sync", "/api/worker/start"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(post_req(base + path))
            self.assertEqual(e.exception.code, 409, path)
            self.assertIn("主是 b-1", json.loads(e.exception.read())["error"])
        put = urllib.request.Request(base + "/api/project/p1/config", data=json.dumps({"text": "id = 'p1'\n"}).encode(), method="PUT",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:           # 推项目配置（#161）同样到主那台
            urllib.request.urlopen(put)
        self.assertEqual(e.exception.code, 409)
        self.assertFalse((self.d / "a" / "projects" / "p1").exists())
        out = json.loads(urllib.request.urlopen(post_req(base + "/api/logout")).read())
        self.assertEqual(out["msg"], "已退出")
        self.assertTrue(SV.standby_ok("/api/admin/deploy") and SV.standby_ok("/api/users/add") and not SV.standby_ok("/api/worker/start"))
        (self.d / "a" / "overview").mkdir(parents=True, exist_ok=True)
        (self.d / "a" / "overview" / "snapshot.json").write_text(json.dumps({"generated": 1, "hosts": [], "server": {"role": "primary"}}))
        snap = json.loads(urllib.request.urlopen(base + "/overview.json").read())
        self.assertEqual((snap["server"]["role"], snap["server"]["holder"]), ("standby", "b-1"))
        self.assertEqual(json.loads((self.d / "a" / "overview" / "snapshot.json").read_text())["server"], {"role": "primary"})   # 文件不动

    def test_serve_settles_before_threads(self):
        # 起服务时先读一次租约定主备，再起项目、工作台、组内服务器的线程（Codex #188：恢复出的那台凭标记先当主，第一轮读租约之前组内线程已经在探）
        order = []
        with mock.patch.object(LS, "tick", side_effect=lambda *a, **k: order.append("tick") or "standby"), \
                mock.patch.object(SV.Overview, "start", lambda self, name: order.append(name)), \
                mock.patch.object(SV.Fleet, "loop", lambda self: order.append("fleet")), \
                mock.patch.object(SV.Server, "sync", lambda self: order.append("sync") or []), \
                mock.patch.object(SV.Server, "serve_forever", side_effect=KeyboardInterrupt), \
                mock.patch.object(SV.Overview, "halt", return_value=True), \
                mock.patch.object(SV.signal, "signal"), mock.patch.object(SV, "rotate_log"), mock.patch.object(SV.DP, "mark_start"):
            self.assertEqual(SV.serve(0), 0)
        self.assertEqual(order[:2], ["tick", "sync"])                          # 项目的采集线程在 sync 里起（#161）
        self.assertIn("overview", order)

    def test_fleet_probe_refused(self):
        # 组内服务器的探测与保活守护：看板线程、命令行 labmon fleet probe 都经 fleet.tick，备用时它什么都不做（Codex #188）
        from labmon import fleet as F
        from labmon import fleetview as FV
        cfg = {"servers": [{"id": "s1"}]}
        with mock.patch.object(F, "_tick") as tk, mock.patch.object(F, "conf", return_value=cfg), contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(FV, "view", return_value={"servers": []}):
            self.assertEqual(F.tick(cfg=cfg, fetch_fn=lambda *a: None), [])
            FV.show("s1", probe=True)
        self.assertFalse(tk.called)
        self.assertIn("没探", out.getvalue())

    def test_project_collect_refused(self):
        # 项目采集：命令行 labmon collect 经看板接口（#162），备用的看板服务回 409（standby），命令行照实打出、退出 3，不探、不写快照（Codex #188）
        from labmon import __main__ as M
        from labmon import collect as C
        from fixtures import board
        cfg_file = self.d / "monitor.toml"
        cfg_file.write_text('id = "p1"\n[[hosts]]\nid = "h1"\nssh = ""\n')
        board(self)
        with mock.patch.object(C, "run_probe") as probe, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(M.main(["collect", "-c", str(cfg_file)]), 3)
        self.assertFalse(probe.called)
        self.assertIn("主是 b-1", err.getvalue())

    def test_cli_reads_refused(self):
        # 命令行读的接口（主机列表、项目配置）在备用上同样回 409 standby：client.toml 里几个地址时接着找主那台，不拿备用上旧的登记（#162）
        from labmon import client
        from fixtures import board
        board(self)
        for path in ("/api/hosts", "/api/project/p1/config"):
            with self.assertRaises(client.Failed) as e:
                client.call("GET", path)
            self.assertEqual((e.exception.code, e.exception.data.get("standby")), (409, True), path)
            self.assertIn("主是 b-1", e.exception.msg)

    def test_service_stop_marks_standby(self):
        # labmon service stop：本机标记置为备用（以后读不到租约时不自认是主）
        LS.set_marker(True, "测试")
        with mock.patch.object(S, "_launchctl"), mock.patch.object(S, "PLIST", self.d / "x.plist"), contextlib.redirect_stdout(io.StringIO()):
            S.stop()
        self.assertFalse(LS.primary())


class BackupTest(Base):
    def setUp(self):
        super().setUp()
        self.root = self.d / "a"
        self.tok = make_token("boss", scopes=("backup",))
        (self.root / "overview").mkdir(exist_ok=True)
        (self.root / "overview" / "snapshot.json").write_text('{"generated": 1}')
        (self.root / "overview" / "x.lock").write_text("")
        (self.root / "labmon.log").write_text("log")
        (self.root / "projects.json").write_text("{}")

    def members(self, data: bytes) -> dict[str, bytes]:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            return {m.name: tf.extractfile(m).read() for m in tf.getmembers() if m.isfile()}

    def test_make_has_no_secrets(self):
        # 包里有状态根与主机清单；没有本机身份、日志、锁；令牌原文、口令不在任何一个文件里（账号库只有哈希）
        name, data = BK.make_bytes()
        m = self.members(data)
        self.assertRegex(name, BK.NAME_RE)
        self.assertIn("state/overview/snapshot.json", m)
        self.assertIn("state/users.json", m)
        self.assertEqual(m["hosts.toml"], self.hosts.read_bytes())
        self.assertFalse({"state/server.json", "state/labmon.log", "state/overview/x.lock"} & set(m))
        self.assertFalse(any(self.tok.encode() in v for v in m.values()))
        man = json.loads(m["manifest.json"])
        self.assertEqual((man["id"], man["files"]), ("a-1", len([k for k in m if k.startswith("state/")])))

    def test_make_while_file_replaced(self):
        # 打包时服务把状态文件整体替换了（save_json）：包里是替换前或替换后的一整份，不是旧的头配新的内容（Codex #188）
        f = self.root / "overview" / "snapshot.json"
        real = tarfile.TarFile.gettarinfo

        def swap(tf, name=None, arcname=None, fileobj=None):
            ti = real(tf, name, arcname, fileobj)
            if arcname == "state/overview/snapshot.json":
                tmp = f.with_name("x.new")
                tmp.write_text(json.dumps({"generated": 2, "pad": "x" * 500}))
                os.replace(tmp, f)
            return ti
        with mock.patch.object(tarfile.TarFile, "gettarinfo", swap):
            m = self.members(BK.make_bytes()[1])
        self.assertEqual(json.loads(m["state/overview/snapshot.json"]), {"generated": 1})
        self.assertIn("manifest.json", m)

    def test_restore_moves_old_aside_and_keeps_id(self):
        # 恢复：旧的挪到旁边、保留这台身份；账号库照旧 600（umask 022 下也是，Codex #188）；以备用起来，读到租约空了才接管（Codex #188：
        # 原来的主还在时，恢复出的那台不能凭标记先当主、在第一次读租约之前就起采集与组内服务器线程）
        out = BK.make(str(self.d))
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)                   # 包里有账号库的哈希：只给自己读
        self.use("b")
        (self.d / "b" / "old.txt").write_text("b 原来的")
        with mock.patch.object(S, "installed", return_value=True):
            with self.assertRaises(ConfigError):
                BK.restore(str(out))
        old = os.umask(0o022)
        try:
            with mock.patch.object(S, "installed", return_value=False):
                msg = BK.restore(str(out), t=time.time())
        finally:
            os.umask(old)
        self.assertEqual((self.d / "b" / "users.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("这台是 b-1", msg)
        self.assertEqual(json.loads((self.d / "b" / "overview" / "snapshot.json").read_text()), {"generated": 1})
        self.assertFalse((self.d / "b" / "old.txt").exists())
        aside = [p for p in self.d.iterdir() if p.name.startswith("b.before-restore-")]
        self.assertEqual(len(aside), 1)
        self.assertTrue((aside[0] / "old.txt").exists())
        self.assertTrue(any(p.name.startswith("hosts.toml.before-restore-") for p in self.d.iterdir()))
        self.assertEqual((LS.ident(), LS.primary()), ("b-1", False))
        self.set_lease("a-1", 0)                                              # 原来的主还在续：照旧备用
        self.assertEqual(LS.tick(), "standby")
        LS._run(self.h(), "release", "a-1")                                   # 它停了：下一轮接管
        self.assertEqual(LS.tick(), "primary")
        with self.assertRaises(ConfigError):
            BK.check(self.hosts)
        with mock.patch.object(S, "install", return_value=0) as ins:        # 接着装服务：项目登记在状态根里（#161），不用再给 -c
            self.assertEqual(BK.install_service(8765, None), 0)
        ins.assert_called_once_with(8765, None, None)

    SRV_A = '# 打包那台的内网入口\n[server]\nlan_bind = "0.0.0.0"\ntls_cert = "tls/a.crt"\ntls_key = "tls/a.key"\n'
    SRV_B = '# 这台的内网入口\n[server]   # dashboard-host\nlan_bind = "0.0.0.0"\ntls_cert = "tls/b.crt"\ntls_key = "tls/b.key"\nlan_hosts = ["b.lab:8443"]\n'

    def test_keep_section(self):
        # 纯函数：new 里的那一段换成 old 里的（连同紧贴表头上方的注释）；old 没有就删、new 没有就接在末尾；别的段不动
        h2 = '[[hosts]]\nid = "h2"\n'
        new = f'top = 1\n\n{self.SRV_A}\n{h2}'
        got = config.keep_section(new, f'{self.SRV_B}\n[[hosts]]\nid = "old"\n')
        self.assertEqual(tomllib.loads(got)["server"]["tls_cert"], "tls/b.crt")
        self.assertEqual(tomllib.loads(got)["hosts"], [{"id": "h2"}])
        self.assertIn("# 这台的内网入口", got)
        self.assertNotIn("打包那台", got)
        self.assertNotIn("server", tomllib.loads(config.keep_section(new, h2)))
        self.assertTrue(config.keep_section(new, "").startswith("top = 1\n"))
        self.assertEqual(tomllib.loads(config.keep_section(h2, self.SRV_B))["server"]["lan_hosts"], ["b.lab:8443"])
        self.assertEqual(config.keep_section(h2, ""), h2)
        with self.assertRaises(ConfigError):
            config.keep_section(new, "[server]\nlan_bind = \n")

    def test_restore_keeps_own_server(self):
        # 恢复时主机清单的 [server] 是这台自己的（#197）：备份里的换成这台原来的；这台原来没有（或没有主机清单）就不带备份里的；其余段照备份
        base = self.hosts.read_text()
        h2 = '\n[[hosts]]\nid = "h2"\nssh = "root@192.0.2.8"\n'
        cases = [("两边都有", self.SRV_A, self.SRV_B, "tls/b.crt"), ("备份里没有", None, self.SRV_B, "tls/b.crt"),
                 ("这台没有", self.SRV_A, "", None), ("这台没有主机清单", self.SRV_A, None, None)]
        for i, (name, at_a, at_b, want) in enumerate(cases):
            with self.subTest(name):
                self.use("a")
                self.hosts.write_text(base + (f"\n{at_a}" if at_a else "") + h2)
                out = BK.make(str(self.d / f"bk{i}"))
                self.use("b")
                if at_b is None:
                    self.hosts.unlink()
                else:
                    self.hosts.write_text(at_b + '\n[[hosts]]\nid = "old"\n')
                with mock.patch.object(S, "installed", return_value=False):
                    msg = BK.restore(str(out), t=time.time() + 10 * i)
                got = tomllib.loads(self.hosts.read_text())
                self.assertEqual([h["id"] for h in got["hosts"]], ["h1", "h2"])
                self.assertEqual((got.get("server") or {}).get("tls_cert"), want)
                self.assertIn("[server]", msg)
                self.assertEqual(config.server()["tls_cert"], str(self.d / want) if want else None)

    def test_restore_bad_local_hosts_moves_nothing(self):
        # 这台原来的主机清单 [server] 写坏了、合不出能解析的：动任何东西之前停下
        out = BK.make(str(self.d))
        self.use("b")
        (self.d / "b" / "old.txt").write_text("b 原来的")
        self.hosts.write_text("[server]\nlan_bind = \n")
        with mock.patch.object(S, "installed", return_value=False), self.assertRaises(ConfigError):
            BK.restore(str(out))
        self.assertTrue((self.d / "b" / "old.txt").exists())
        self.assertEqual(self.hosts.read_text(), "[server]\nlan_bind = \n")
        self.assertFalse([p for p in self.d.iterdir() if "before-restore" in p.name])

    def test_pull_keeps_n_and_skips_unreachable(self):
        srv = SV.Server(("127.0.0.1", 0), [])
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        dest = self.d / "bk"
        dest.mkdir()
        for i in range(3):                                         # 更早的几份
            (dest / f"labmon-backup-2026010{i}-000000.tar.gz").write_bytes(b"x")
        with mock.patch.dict(os.environ, {"LABMON_TOKEN": self.tok}):
            rc, msg = BK.pull(url, d=dest, keep=2)
            self.assertEqual(rc, 0, msg)
            self.assertIn("取到", msg)
            self.assertEqual(len(BK.listing(dest)), 2)
            self.assertEqual(BK.listing(dest)[-1].stat().st_mode & 0o777, 0o600)
            self.assertIn("state/overview/snapshot.json", self.members(BK.listing(dest)[-1].read_bytes()))
            self.assertEqual(BK.pull("http://127.0.0.1:9", d=dest)[0], 0)          # 连不上：跳过
        with mock.patch.dict(os.environ, {"LABMON_TOKEN": "bad"}):
            self.assertEqual(BK.pull(url, d=dest)[0], 3)
        rd = make_token("reader", scopes=("read",))
        with mock.patch.dict(os.environ, {"LABMON_TOKEN": rd}):
            self.assertEqual(BK.pull(url, d=dest)[0], 3)                         # 只读令牌取不了备份


if __name__ == "__main__":
    unittest.main()
