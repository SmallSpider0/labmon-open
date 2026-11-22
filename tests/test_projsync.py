"""项目登记与配置推拉的离线测试（#161）：projects.put_config / get_config / set_config、projsync、labmon wait 的同步、
GET/PUT /api/project/<id>/config、从 launchd 的 -c 迁来（projects.adopt、service.install）。
projsync 经看板接口取、推（#162）：_Base 起一个临时看板服务（fixtures.board），令牌是管理员 boss 的 admin 令牌（能推 [[hosts]]）。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures import NO_HOSTS, bearer_opener, board, make_token  # noqa: E402
from labmon import __main__ as M  # noqa: E402
from labmon import collect as C  # noqa: E402
from labmon import projects as PJ  # noqa: E402
from labmon import projsync as PS  # noqa: E402
from labmon import service as S  # noqa: E402
from labmon.config import done_key  # noqa: E402
from labmon.state import load_rows, save_json, state_dir  # noqa: E402

CFG = r"""id = "pz"
title = "推拉测试"
[[groups]]
id = "A"
prefix = "a_"
[groups.done_when]
file = "notes/done.md"
pattern = '^- \[x\] A'
"""


class _Base(unittest.TestCase):
    """项目目录 d/proj：monitor.toml（计划项 A 的 done_when 指 notes/done.md）；状态根在 d/state。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(self.d / "state"), "LABMON_HOSTS": NO_HOSTS, "LABMON_LAN": "off"})
        self.env.start()
        self.proj = self.d / "proj"
        (self.proj / "notes").mkdir(parents=True)
        (self.proj / "notes" / "done.md").write_text("- [ ] A\n")
        self.cfg = self.proj / "monitor.toml"
        self.cfg.write_text(CFG)
        self.board = board(self, scopes=("admin",))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def srv_text(self) -> str:
        return PJ.get_config("pz")["text"]

    def refused(self, fn, *a, **kw) -> PJ.Refused:
        with self.assertRaises(PJ.Refused) as e:
            fn(*a, **kw)
        return e.exception


class RegistryTest(_Base):
    def test_versions_and_refusals(self):
        # 第一次推就是登记；已登记的没带版本 428、版本对不上 412（都带上看板上现在的版本）；对上了才写；id 不对、配置不对 400，看板上那份不动
        r = PJ.put_config("pz", CFG)
        self.assertEqual((r["created"], r["changed"], r["archived"], r["ver"]), (True, [], False, PJ.ver_of(CFG)))
        self.assertEqual((PJ.registered(), PJ.project_ids(), PJ.out_dir("pz").is_dir()), (["pz"], ["pz"], True))
        self.assertEqual((PJ.meta("pz")["by"], PJ.meta("pz")["owner"]), ("用命令行", None))
        new = CFG.replace("推拉测试", "改了")
        e = self.refused(PJ.put_config, "pz", new)
        self.assertEqual((e.code, e.ver), (428, r["ver"]))
        e = self.refused(PJ.put_config, "pz", new, base="0" * 16)
        self.assertEqual((e.code, e.ver), (412, r["ver"]))
        self.assertIn("先 labmon project pull", e.msg)
        self.assertEqual(self.srv_text(), CFG)
        r2 = PJ.put_config("pz", new, base=r["ver"])
        self.assertEqual((r2["created"], r2["changed"], r2["ver"], self.srv_text()), (False, ["配置"], PJ.ver_of(new), new))
        self.assertEqual(PJ.put_config("pz", new, base=r2["ver"])["changed"], [])
        for pid, text, why in [("../x", new, "项目 id"), ("other", new, "与要推的项目 other 不一样"),
                               ("pz", 'id = "pz"\nalerts = 5\n', "配置读不了"), ("pz", 'id = "pz"\n[[groups]]\nid = "A"\nprefix = "a_"\n[groups.done_when]\nfile = "x"\n', "done_when 需要")]:
            e = self.refused(PJ.put_config, pid, text, base=r2["ver"])
            self.assertEqual(e.code, 400, pid)
            self.assertIn(why, e.msg)
        self.assertEqual((PJ.registered(), self.srv_text()), (["pz"], new))
        for pid in ("nope", "..", "../state"):
            self.assertEqual(self.refused(PJ.get_config, pid).code, 404)
        got = PJ.get_config("pz")
        self.assertEqual((got["ver"], got["archived"], got["files"]), (r2["ver"], False, {}))

    def test_archived(self):
        # 已登记又已归档的推不了（409）；归档了、看板上却没有配置的照样能推进来，推完 restore
        r = PJ.put_config("pz", CFG)
        save_json(state_dir() / "projects.json", {"archived": {"pz": {"id": "pz", "t": time.time()}, "pq": {"id": "pq", "t": time.time()}}})
        self.assertEqual(self.refused(PJ.put_config, "pz", CFG, base=r["ver"]).code, 409)
        self.assertTrue(PJ.get_config("pz")["archived"])
        self.assertEqual(PS.status(self.cfg)["state"], "archived")
        q = self.d / "q" / "monitor.toml"
        q.parent.mkdir()
        q.write_text('id = "pq"\n')
        self.assertEqual(PS.push(q), "登记了项目 pq：它已归档，看板不采；要接着用 labmon project restore pq")
        self.assertIn("恢复 pq", PJ.restore("pq"))
        self.assertEqual(PJ.project_ids(), ["pq"])

    def test_done_when_pushed_and_read_from_registry(self):
        # done_when 的文件随推上传；看板只读登记目录里的那份：本地勾了不推不算，wait 的同步把它推上去后才算
        self.assertEqual(PS.status(self.cfg)["state"], "unregistered")
        self.assertEqual(PS.push(self.cfg), "登记了项目 pz：看板服务最迟下一轮全局采集时开始采它")
        f = PJ.files_dir("pz") / "notes" / "done.md"
        self.assertEqual((f.read_text(), PJ.load_cfg("pz")["groups"][0]["done_when"]["file"]), ("- [ ] A\n", str(f)))
        status = lambda: {g["id"]: g["status"] for g in C.collect(PJ.load_cfg("pz"), use_ssh=False)["groups"]}["A"]   # noqa: E731
        self.assertEqual(status(), "未排队")
        (self.proj / "notes" / "done.md").write_text("- [x] A\n")
        self.assertEqual(status(), "未排队")
        self.assertEqual(PS.status(self.cfg)["state"], "files")
        self.assertEqual(PS.sync(self.cfg), {"act": "push", "id": "pz", "msg": "推上去了 pz：更新了 notes/done.md", "key": None})
        self.assertEqual(status(), "完成")
        self.assertEqual(PS.status(self.cfg)["state"], "same")
        (self.proj / "notes" / "done.md").unlink()                         # 本地删了：看板上的也删（读不到就是没勾）
        self.assertEqual(PS.sync(self.cfg)["msg"], "推上去了 pz：删了 notes/done.md（本地没有）")
        self.assertEqual((f.exists(), status()), (False, "未排队"))

    def test_done_when_outside_project_and_limits(self):
        # 绝对路径、~ 开头的放到 files/_abs/<哈希>-<文件名>，不写出 files/；改成别的文件后旧的删掉；超过 1 MiB 的拒绝
        flag = self.d / "elsewhere" / "flag.txt"
        flag.parent.mkdir()
        flag.write_text("ok\n")
        self.cfg.write_text(CFG.replace('"notes/done.md"', f'"{flag}"').replace(r"'^- \[x\] A'", '"ok"'))
        PS.push(self.cfg)
        key = done_key(str(flag))
        self.assertTrue(key.startswith("_abs/") and key.endswith("-flag.txt"), key)
        self.assertEqual(done_key("~/x/../y.md")[:5], "_abs/")
        self.assertEqual(PJ.load_cfg("pz")["groups"][0]["done_when"]["file"], str(PJ.files_dir("pz") / key))
        self.assertEqual(PJ.get_config("pz")["files"], {key: PJ.ver_of("ok\n")})
        g = C.collect(PJ.load_cfg("pz"), use_ssh=False)["groups"][0]
        self.assertEqual((g["status"], g["done_when"]["file"]), ("完成", "flag.txt"))         # 页面提示勾哪里写原来的文件名，不写登记目录里带哈希的
        self.cfg.write_text(CFG)
        self.assertEqual(PS.sync(self.cfg)["msg"], f"推上去了 pz：配置、删了不再用的 {key}、更新了 notes/done.md")
        self.assertEqual(PJ.get_config("pz")["files"], {"notes/done.md": PJ.ver_of("- [ ] A\n")})
        e = self.refused(PJ.put_config, "pz", CFG, {"notes/done.md": "x" * (PJ.FILE_MAX + 1)}, base=PJ.get_config("pz")["ver"])
        self.assertEqual(e.code, 400)

    def test_page_edit_then_pull(self):
        # 页面上改的是看板上那份；项目目录里没改过：wait 的同步把它拉回来（不留 .mine），再同步就一样了
        PS.push(self.cfg)
        self.assertEqual(PJ.set_config("pz", {"title": "页面上改的"}), ["标题 推拉测试 → 页面上改的"])
        self.assertEqual(PS.status(self.cfg)["state"], "pull")
        r = PS.sync(self.cfg)
        self.assertEqual((r["act"], r["key"]), ("pull", None))
        self.assertIn("已拉回", r["msg"])
        self.assertEqual(self.cfg.read_text(), self.srv_text())
        self.assertIn('title = "页面上改的"', self.cfg.read_text())
        self.assertEqual(PS.sync(self.cfg)["act"], "same")
        self.assertFalse((self.proj / ".labmon" / "monitor.toml.mine").exists())
        self.assertEqual((self.proj / ".labmon" / ".gitignore").read_text(), "*\n")       # 项目仓库不多出要提交的东西
        self.assertEqual(json.loads((self.proj / ".labmon" / "monitor.toml.sync.json").read_text())["ver"], PJ.ver_of(self.cfg.read_text()))

    def test_both_changed_not_overwritten(self):
        # 两边都改了：同步谁也不动、报一条（同一对版本同一个键）；push 412；pull 拉回来、本地那份存成 .mine；再改再同步就推上去
        PS.push(self.cfg)
        PJ.set_config("pz", {"title": "页面"})
        srv = self.srv_text()
        mine = CFG.replace("推拉测试", "本地")
        self.cfg.write_text(mine)
        r = PS.sync(self.cfg)
        self.assertEqual(r["act"], "conflict")
        self.assertEqual(r["key"], f"sync_conflict:{PJ.ver_of(mine)}:{PJ.ver_of(srv)}")
        self.assertIn("labmon project pull", r["msg"])
        self.assertEqual((self.cfg.read_text(), self.srv_text()), (mine, srv))
        self.assertEqual(PS.sync(self.cfg)["key"], r["key"])
        self.assertEqual(self.refused(PS.push, self.cfg).code, 412)
        self.assertEqual(self.srv_text(), srv)
        self.assertIn("monitor.toml.mine", PS.pull(self.cfg))
        self.assertEqual(((self.proj / ".labmon" / "monitor.toml.mine").read_text(), self.cfg.read_text()), (mine, srv))
        self.cfg.write_text(srv.replace('"页面"', '"合好了"'))
        self.assertEqual(PS.sync(self.cfg)["act"], "push")
        self.assertIn('"合好了"', self.srv_text())

    def test_done_when_path_moves(self):
        # done_when 的文件从 notes/done.md 换成 notes（目录变文件）再换回去（文件变目录）：先让开旧的再写；两个文件一个是另一个的上级目录的 400
        PS.push(self.cfg)
        flat = CFG.replace('file = "notes/done.md"', 'file = "notes"')
        r = PJ.put_config("pz", flat, {"notes": "- [x] A\n"}, base=PJ.ver_of(CFG))
        self.assertIn("更新了 notes", r["changed"])
        self.assertEqual(PJ.get_config("pz")["files"], {"notes": PJ.ver_of("- [x] A\n")})
        PJ.put_config("pz", CFG, {"notes/done.md": "- [ ] A\n"}, base=PJ.ver_of(flat))
        self.assertEqual(PJ.get_config("pz")["files"], {"notes/done.md": PJ.ver_of("- [ ] A\n")})
        both = CFG + '[[groups]]\nid = "B"\nprefix = "b_"\n[groups.done_when]\nfile = "notes"\npattern = "B"\n'
        e = self.refused(PJ.put_config, "pz", both, {"notes": "B", "notes/done.md": "A"}, base=PJ.ver_of(CFG))
        self.assertEqual(e.code, 400)
        self.assertIn("上级目录", e.msg)
        self.assertEqual(self.srv_text(), CFG)

    def test_done_when_tmp_names(self):
        # 两个计划项的 done_when 是 flags/done.tmp 与 flags/done：原子写的临时文件不占另一个的名字，两个都留着
        two = CFG.replace('"notes/done.md"', '"flags/done.tmp"') + '[[groups]]\nid = "B"\nprefix = "b_"\n[groups.done_when]\nfile = "flags/done"\npattern = "B"\n'
        PJ.put_config("pz", two, {"flags/done.tmp": "A", "flags/done": "B"})
        self.assertEqual(PJ.get_config("pz")["files"], {"flags/done.tmp": PJ.ver_of("A"), "flags/done": PJ.ver_of("B")})

    def test_guarded_needs_ops(self):
        # 没有平台操作权限（ops=False，接口按 users.can_ops 给）的推：改了 [[hosts]]、[[queues]] 的 403（第一次推就带着的也算），别的照常推
        hosts = '[[hosts]]\nid = "h"\nreturned = "2026-01-01 00:00"\n[[hosts.checks]]\nid = "c"\ncmd = "true"\n'
        e = self.refused(PJ.put_config, "pz", CFG + hosts, ops=False)
        self.assertEqual(e.code, 403)
        self.assertEqual(PJ.registered(), [])
        PJ.put_config("pz", CFG + hosts)                                    # 命令行、迁移在看板那台机器上：算有
        v = PJ.ver_of(CFG + hosts)
        t2 = CFG.replace("推拉测试", "只改标题") + "\n# 注释\n" + hosts
        self.assertEqual(PJ.put_config("pz", t2, base=v, ops=False)["changed"], ["配置"])
        for bad in (t2.replace('cmd = "true"', 'cmd = "touch x"'), t2 + '[[queues]]\nid = "q"\nroot = "/r"\njobs = "j.txt"\n', CFG):
            self.assertEqual(self.refused(PJ.put_config, "pz", bad, base=PJ.ver_of(t2), ops=False).code, 403)
        self.assertEqual(self.srv_text(), t2)

    def late_save(self, text: str):
        """取看板上那份时（本地已经读过）项目目录里又存了一版 text：只存一次。"""
        get = PS._get

        def fn(pid, *a):
            r = get(pid, *a)
            if self.cfg.read_text() != text:
                self.cfg.write_text(text)
            return r
        return mock.patch.object(PS, "_get", fn)

    def test_saved_while_syncing(self):
        # 同步读过本地、拉回覆盖之前本地又存了一版（看板上也改了）：不覆盖刚存的，照新的那份报冲突；pull 把刚存的那份留成 .mine
        PS.push(self.cfg)
        PJ.set_config("pz", {"title": "页面"})
        srv, mine = self.srv_text(), CFG.replace("推拉测试", "刚存的")
        with self.late_save(mine):
            r = PS.sync(self.cfg)
        self.assertEqual(r["act"], "conflict")
        self.assertEqual(r["key"], f"sync_conflict:{PJ.ver_of(mine)}:{PJ.ver_of(srv)}")
        self.assertEqual((self.cfg.read_text(), self.srv_text()), (mine, srv))
        self.cfg.write_text(CFG)
        mine2 = CFG.replace("推拉测试", "又存的")
        with self.late_save(mine2):
            self.assertIn("monitor.toml.mine", PS.pull(self.cfg))
        self.assertEqual(((self.proj / ".labmon" / "monitor.toml.mine").read_text(), self.cfg.read_text()), (mine2, srv))

    def test_two_configs_in_one_dir(self):
        # 同一目录里两个项目的配置（a.toml、b.toml）：同步记录按配置文件各记各的，推了 b 不抹掉 a 的；pull 存的 .mine 也按文件名分开
        a, b = self.proj / "a.toml", self.proj / "b.toml"
        a.write_text(CFG.replace('id = "pz"', 'id = "pa"'))
        b.write_text(CFG.replace('id = "pz"', 'id = "pb"'))
        PS.push(a)
        PS.push(b)
        self.assertEqual((PS.base_of(a, "pa"), PS.base_of(b, "pb")), (PJ.ver_of(a.read_text()), PJ.ver_of(b.read_text())))
        PJ.set_config("pa", {"title": "页面"})
        self.assertEqual(PS.status(a)["state"], "pull")
        mine = a.read_text().replace("推拉测试", "本地")
        a.write_text(mine)
        self.assertIn("a.toml.mine", PS.pull(a))
        self.assertEqual((self.proj / ".labmon" / "a.toml.mine").read_text(), mine)
        self.assertEqual(PS.status(b)["state"], "same")

    def test_two_configs_mark_at_once(self):
        """同目录两个配置同时同步（两个 wait）：一个读了记录、还没写时另一个写完，两份基线都要留着。"""
        a, b = self.proj / "a.toml", self.proj / "b.toml"
        real, nested = PS.save_json, []

        def save(f, data):
            if not nested:                      # a 的 mark 读完、写之前，b 的 mark 整个做完
                nested.append(1)
                PS.mark(b, "pb", "vb")
            real(f, data)
        with mock.patch.object(PS, "save_json", save):
            PS.mark(a, "pa", "va")
        self.assertEqual((PS.base_of(a, "pa"), PS.base_of(b, "pb")), ("va", "vb"))

    def test_copy_without_sync_record(self):
        # 别处的一份（没有同步记录）：与看板上一样就记下版本；不一样算冲突，push 回 428
        PS.push(self.cfg)
        c2 = self.d / "copy" / "monitor.toml"
        c2.parent.mkdir()
        c2.write_text(CFG.replace("推拉测试", "另一份"))
        self.assertEqual(PS.status(c2)["state"], "conflict")
        self.assertEqual(self.refused(PS.push, c2).code, 428)
        c2.write_text(CFG)
        (c2.parent / "notes").mkdir()
        (c2.parent / "notes" / "done.md").write_text("- [ ] A\n")
        self.assertEqual(PS.sync(c2)["act"], "same")
        self.assertEqual(PS.base_of(c2, "pz"), PJ.ver_of(CFG))

    def test_cli(self):
        # labmon project push|pull；labmon check 写出同步状态与不再生效的键
        out = io.StringIO()
        self.cfg.write_text(CFG.replace("[[groups]]", 'out_dir = "~/x"\n[local]\nenabled = false\n[[groups]]', 1))
        with contextlib.redirect_stdout(out):
            self.assertEqual(M.main(["project", "push", "-c", str(self.cfg)]), 0)
            self.assertEqual(M.main(["check", "-c", str(self.cfg)]), 0)
        self.assertIn("登记了项目 pz", out.getvalue())
        self.assertIn("与看板上的 pz 配置一样", out.getvalue())
        self.assertIn("out_dir、[local] 不再生效", out.getvalue())
        PJ.set_config("pz", {"title": "页面"})
        self.cfg.write_text(self.cfg.read_text().replace("推拉测试", "本地"))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(M.main(["project", "push", "-c", str(self.cfg)]), 3)
        self.assertIn("先 labmon project pull", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(["project", "pull", "-c", str(self.cfg)]), 0)
        self.assertEqual(self.cfg.read_text(), self.srv_text())

    def test_push_admin(self):
        # 改了 [[hosts]] 的推：client.token 是 project 令牌时 403，提示 labmon login --admin 后 push --admin；--admin 用 admin.token 推上去
        d = Path(os.environ["LABMON_CLIENT"]).parent
        os.environ.pop("LABMON_TOKEN")                                      # board 的 mock.patch.dict 收尾时还原
        (d / "client.token").write_text(make_token("u", "admin", ("project",)) + "\n")
        (d / "admin.token").write_text(make_token("boss", "admin", ("admin",)) + "\n")
        self.cfg.write_text(CFG + '[[hosts]]\nid = "h"\nreturned = "2026-01-01 00:00"\n[[hosts.checks]]\nid = "c"\ncmd = "true"\n')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(M.main(["project", "push", "-c", str(self.cfg)]), 3)
        self.assertIn("labmon login --admin 后 labmon project push --admin", err.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(M.main(["project", "push", "--admin", "-c", str(self.cfg)]), 0)
        self.assertIn("登记了项目 pz", out.getvalue())


class WaitSyncTest(_Base):
    """labmon wait 每轮先同步：推了、拉了各打一行（拉了按新配置接着等）；两边都改了当告警报一次；看板上没登记的退出 3。"""

    class Stop(Exception):
        pass

    def setUp(self):
        super().setUp()
        save_json(PJ.out_dir("pz") / "snapshot.json", {"id": "pz", "generated": time.time() + 3600, "alerts": [], "hosts": []})

    def run_wait(self, steps):
        it = iter(steps)
        out, err = io.StringIO(), io.StringIO()

        def sleep(_):
            next(it, self.stop)()
        with mock.patch.object(M, "HOLD_MAX", 0), mock.patch.object(M.time, "sleep", sleep), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = M.cmd_wait(str(self.cfg))
            except self.Stop:
                rc = None
        return rc, out.getvalue(), err.getvalue()

    def stop(self):
        raise self.Stop

    def test_unregistered_exits_3(self):
        rc, _, err = self.run_wait([])
        self.assertEqual(rc, 3)
        self.assertIn("labmon project push", err)

    def test_rounds(self):
        PS.push(self.cfg)
        steps = [lambda: self.cfg.write_text(CFG.replace("推拉测试", "本地改")),
                 lambda: PJ.set_config("pz", {"title": "页面改"}),
                 lambda: (PJ.set_config("pz", {"title": "页面又改"}), self.cfg.write_text(self.cfg.read_text().replace("页面改", "本地又改")))]
        rc, out, _ = self.run_wait(steps)
        self.assertEqual(rc, 1)
        self.assertIn("推上去了 pz：配置", out)
        self.assertIn("看板上改了 pz 的 monitor.toml（页面上改的），已拉回", out)
        self.assertIn("[sync_conflict:", out)
        self.assertIn("本地又改", self.cfg.read_text())
        self.assertIn("页面又改", self.srv_text())
        rc, out, _ = self.run_wait([])                                      # 同一对版本不再报：一直等到被叫停
        self.assertIsNone(rc)
        self.assertNotIn("sync_conflict", out)

    def test_bad_types_are_sync_errors(self):
        # wait 跑着时本地存成 TOML 对、类型不对的（alerts = 5）：当一条推不上报，不让 wait 带着 Traceback 退出
        PS.push(self.cfg)
        self.cfg.write_text(CFG.replace("[[groups]]", "alerts = 5\n[[groups]]", 1))
        r = M._sync(str(self.cfg))
        self.assertEqual(r["act"], "error")
        self.assertTrue(r["key"].startswith("sync_error:"))
        self.assertIn("读不了", r["msg"])
        self.cfg.write_text('id = "pz"\ngroups = 1\n')     # 本地只做 TOML 解析的那份也不因类型不对抛别的异常：照样是推不上
        r = M._sync(str(self.cfg))
        self.assertEqual(r["act"], "error")
        self.assertTrue(r["key"].startswith("sync_error:"))

    def test_unreachable_limit_follows_push(self):
        # wait 跑着时本地改了采集间隔、推上去了：连不上看板的门槛（提示与 server_back）跟着变，同拉回来的
        PS.push(self.cfg)
        seen, real = [], M.stale_limit

        def spy(*a):
            seen.append(a)
            return real(*a)
        with mock.patch.object(M, "stale_limit", spy):
            self.run_wait([lambda: self.cfg.write_text(CFG.replace("[[groups]]", "interval = 30\n[[groups]]", 1))])
        self.assertEqual([x[0] for x in seen], [120, 30])

    def test_new_cfg_after_push_and_pull(self):
        # 推上去、拉回来之后都按新配置判告警（采集间隔、过期门槛跟着变）：看板那边每次按登记的那份判（waits.wait 读 projects.load_cfg）
        from labmon import waits as W
        PS.push(self.cfg)
        seen, real = [], W.wait_check

        def spy(cfg, *a, **kw):
            seen.append(cfg["title"])
            return real(cfg, *a, **kw)
        steps = [lambda: self.cfg.write_text(CFG.replace("推拉测试", "本地改")), lambda: PJ.set_config("pz", {"title": "页面改"})]
        with mock.patch.object(W, "wait_check", spy):
            self.run_wait(steps)
        self.assertEqual(seen, ["推拉测试", "本地改", "页面改"])


class ApiTest(_Base):
    """GET/PUT /api/project/<id>/config：读要 read、推要 project；ETag / If-Match 带版本；推上来就加进看板服务。"""

    def setUp(self):
        super().setUp()
        from labmon import serve as SV
        self.srv = SV.Server(("127.0.0.1", 0), interval=3600)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/api/project/pz/config"
        self.rw, self.ro = make_token("boss", "admin", ("project",)), make_token("boss", "admin", ("read",))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        ts = [t for t in threading.enumerate() if t.name.startswith("collect-")]
        for p in self.srv.projects.values():
            p.stop = True
            p.wake.set()
        for t in ts:
            t.join(30)
        super().tearDown()

    def call(self, method, tok, body=None, url=None, ctype="application/json", **hdr):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url or self.url, data=data, method=method, headers={"Content-Type": ctype, **hdr})
        try:
            with bearer_opener(tok).open(req, timeout=30) as r:
                return r.status, json.loads(r.read()), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers.get("ETag")

    def test_put_get(self):
        files = {"notes/done.md": "- [ ] A\n"}
        code, body, _ = self.call("PUT", self.ro, {"text": CFG, "files": files})
        self.assertEqual(code, 403)
        self.assertEqual(PJ.registered(), [])
        code, body, tag = self.call("PUT", self.rw, {"text": CFG, "files": files})
        self.assertEqual((code, body["created"], body["msg"], tag), (200, True, "登记了项目 pz", f'"{PJ.ver_of(CFG)}"'))
        self.assertIn("pz", self.srv.projects)                              # 推上来当场加进看板服务、开始采
        self.assertEqual(PJ.meta("pz")["owner"], "boss")
        code, body, tag = self.call("GET", self.ro)
        self.assertEqual((code, body["text"], body["files"], tag), (200, CFG, {"notes/done.md": PJ.ver_of("- [ ] A\n")}, f'"{PJ.ver_of(CFG)}"'))
        new = CFG.replace("推拉测试", "接口改")
        code, body, _ = self.call("PUT", self.rw, {"text": new, "files": files})
        self.assertEqual((code, body["ver"]), (428, PJ.ver_of(CFG)))
        code, body, _ = self.call("PUT", self.rw, {"text": new, "files": files}, **{"If-Match": '"0000000000000000"'})
        self.assertEqual((code, body["ver"]), (412, PJ.ver_of(CFG)))
        code, body, tag = self.call("PUT", self.rw, {"text": new, "files": files}, **{"If-Match": f'"{PJ.ver_of(CFG)}"'})
        self.assertEqual((code, body["changed"], body["msg"], tag), (200, ["配置"], "推上来了 pz：配置", f'"{PJ.ver_of(new)}"'))
        self.assertEqual(self.srv_text(), new)
        code, body, _ = self.call("PUT", self.rw, {"text": new}, ctype="text/plain", **{"If-Match": f'"{PJ.ver_of(new)}"'})
        self.assertEqual(code, 415)                                         # 写请求只认 JSON 类型（防表单跨站提交，#158）
        code, body, _ = self.call("PUT", self.rw, {"files": files}, **{"If-Match": f'"{PJ.ver_of(new)}"'})
        self.assertEqual(code, 400)
        code, body, _ = self.call("PUT", self.rw, {"text": 'id = "zz"\n'}, **{"If-Match": f'"{PJ.ver_of(new)}"'})
        self.assertEqual(code, 400)
        code, body, _ = self.call("GET", self.ro, url=self.url.replace("/pz/", "/nope/"))
        self.assertEqual(code, 404)

    def test_guarded_over_api(self):
        # 改 [[hosts]]（连主机、跑检查）要平台操作的权限：project 令牌、公网入口上的 admin 令牌都 403，内网入口的 admin 令牌能推
        from labmon import serve as SV
        pub = SV.Listener(("127.0.0.1", 0), self.srv)
        threading.Thread(target=pub.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(pub.server_close)
        self.addCleanup(pub.shutdown)
        purl = self.url.replace(str(self.srv.server_address[1]), str(pub.server_address[1]))
        text = CFG + '[[hosts]]\nid = "h"\nreturned = "2026-01-01 00:00"\n[[hosts.checks]]\nid = "c"\ncmd = "true"\n'
        boss = make_token("boss", "admin", ("admin",))
        for tok, url in ((self.rw, None), (boss, purl)):
            code, body, _ = self.call("PUT", tok, {"text": text}, url=url)
            self.assertEqual(code, 403, url)
            self.assertIn("[[hosts]]", body["error"])
        self.assertEqual(PJ.registered(), [])
        code, body, _ = self.call("PUT", boss, {"text": text})
        self.assertEqual((code, body["created"]), (200, True))


class AdoptTest(_Base):
    """可删（#161 迁移）：以前用 -c 加进看板服务的项目登记进来。"""

    def old_out(self) -> Path:
        old = self.d / "oldout"
        (old / ".labmon").mkdir(parents=True)
        (old / "index.html").write_text("页面")
        now = time.time()
        (old / ".labmon" / "history.jsonl").write_text(json.dumps({"t": now, "host": "__local__", "ok": True}) + "\n"
                                                       + json.dumps({"t": now, "host": "here", "ok": True}) + "\n")
        self.cfg.write_text(CFG.replace("[[groups]]", f'out_dir = "{old}"\n[[groups]]', 1))
        return old

    def test_adopt(self):
        old = self.old_out()
        msg = PJ.adopt(str(self.cfg))
        self.assertIn(f"输出目录从 {old} 搬到 {PJ.out_dir('pz')}（2 项）", msg)
        self.assertEqual(((PJ.out_dir("pz") / "index.html").read_text(), old.exists()), ("页面", False))
        self.assertEqual([r["host"] for r in load_rows(PJ.out_dir("pz") / ".labmon" / "history.jsonl")], ["here"])
        self.assertEqual((PJ.files_dir("pz") / "notes" / "done.md").read_text(), "- [ ] A\n")
        self.assertEqual((PJ.meta("pz")["by"], PS.status(self.cfg)["state"]), ("看板升级时从 launchd 的 -c 迁来", "same"))
        told = load_rows(state_dir() / "tell.jsonl")
        self.assertEqual((told[0]["project"], told[0]["what"]), ("pz", "upgrade"))
        self.assertIn("看板升级（#161）", told[0]["msg"])
        self.assertIsNone(PJ.adopt(str(self.cfg)))                         # 已登记的不动

    def test_adopt_only_moves_labmon_output(self):
        # out_dir 就是项目目录（或混着别的东西）：只搬 labmon 写的（页面、快照、.labmon/），源码、.git、monitor.toml 留在原处
        self.cfg.write_text(CFG.replace("[[groups]]", 'out_dir = "."\n[[groups]]', 1))
        (self.proj / "train.py").write_text("print(1)\n")
        (self.proj / ".git").mkdir()
        (self.proj / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (self.proj / "index.html").write_text("页面")
        (self.proj / "snapshot.json").write_text("{}")
        (self.proj / ".labmon").mkdir()
        (self.proj / ".labmon" / "state.json").write_text("{}")
        self.assertIn("（3 项）", PJ.adopt(str(self.cfg)))
        self.assertEqual(sorted(x.name for x in self.proj.iterdir()), [".git", ".labmon", "monitor.toml", "notes", "train.py"])
        self.assertEqual(sorted(x.name for x in (self.proj / ".labmon").iterdir()), [".gitignore", "monitor.toml.sync.json"])
        self.assertEqual(((PJ.out_dir("pz") / "index.html").read_text(), (PJ.out_dir("pz") / ".labmon" / "state.json").is_file()), ("页面", True))
        self.assertEqual(PS.status(self.cfg)["state"], "same")

    def test_service_install(self):
        # labmon service install：plist 里旧的 -c 先登记进来，再重写 plist 去掉 -c（端口等照旧）；登记不进来的不动 plist、退出 3
        self.old_out()
        pl = self.d / "x.plist"
        old = ["py", "-m", "labmon", "serve", "-c", str(self.cfg), "--port", "8799"]
        pl.write_bytes(plistlib.dumps({"ProgramArguments": old}))
        calls = []
        fake = lambda *a, check=False: calls.append(a) or subprocess.CompletedProcess(a, 0, "", "")   # noqa: E731
        with mock.patch.object(S, "MANAGER", "launchd"), mock.patch.object(S, "PLIST", pl), mock.patch.object(S, "LOG", self.d / "x.log"), \
                mock.patch.object(S, "_launchctl", fake):
            bad = self.d / "bad.toml"
            bad.write_text("alerts = [")
            pl.write_bytes(plistlib.dumps({"ProgramArguments": old + ["-c", str(bad)]}))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(S.install(None), 3)
            self.assertIn("没登记进来，服务没动", err.getvalue())
            self.assertEqual((plistlib.loads(pl.read_bytes())["ProgramArguments"][-1], calls), (str(bad), []))
            pl.write_bytes(plistlib.dumps({"ProgramArguments": old}))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(S.install(None), 0)
        args = plistlib.loads(pl.read_bytes())["ProgramArguments"]
        self.assertEqual(("-c" in args, args[args.index("--port") + 1]), (False, "8799"))
        self.assertEqual(PJ.registered(), ["pz"])
        self.assertIn("1 个项目", out.getvalue())


if __name__ == "__main__":
    unittest.main()
