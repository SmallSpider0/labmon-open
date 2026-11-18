"""看板账号、会话与令牌（labmon/users.py，#160）的离线测试：口令只存哈希、连着输错锁定、设置码、作用域与角色取交集、吊销立即生效、最后一个管理员。

    python -m unittest tests.test_users -v
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402,F401  （设好测试环境：不发通知、不碰钥匙串）

from labmon import users as U  # noqa: E402
from labmon.records import records  # noqa: E402

T0 = 1_800_000_000.0


def hdr(token=None, cookie=None) -> dict:
    """请求头：令牌走 Authorization，会话走 Cookie。"""
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if cookie:
        h["Cookie"] = f"{U.COOKIE}={cookie}"
    return h


class UsersBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LABMON_STATE": str(Path(self.tmp.name) / "state"), "LABMON_HOSTS": str(Path(self.tmp.name) / "hosts.toml")})
        self.env.start()
        self.iters = mock.patch.object(U, "ITER", 1000)       # 测试里 PBKDF2 少算几轮
        self.iters.start()
        U._GHOST.clear()

    def tearDown(self):
        self.iters.stop()
        self.env.stop()
        self.tmp.cleanup()

    def admin(self, name="boss", pw="correct horse", t=T0) -> str:
        """建一个设好口令的管理员，返回会话原文。"""
        code = fixtures.add_account(name, "admin", "本机命令行", t=t)
        raw, _ = U.setup(name, code, pw, "lan", t=t)
        return raw


class StoreTest(UsersBase):
    def test_local_bootstrap_requires_roster_then_opens_admin(self):
        import contextlib
        import io
        from labmon.__main__ import cmd_user
        from labmon import fleet as F
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            add = ["add", "boss", "--person", "Owner", "--admin"]
            self.assertEqual(cmd_user(add), 3)
            self.assertIsNone(U.account("boss"))
            self.assertEqual(cmd_user(["person", "Owner"]), 0)
            self.assertIn("Owner", F.registered_people())
            self.assertEqual(cmd_user(add), 0)
        self.assertIn("设置码", out.getvalue())
        self.assertEqual(U.listing(directory=True)["users"][0]["fleet_person"], "Owner")

    def test_account_directory_keeps_registered_people_when_config_is_unreadable(self):
        from labmon import fleetboot as FB
        self.admin()
        FB.set_person("Pending", create=True, roster_only=True)
        Path(os.environ["LABMON_HOSTS"]).write_text("[fleet\n")
        listing = U.listing(directory=True)
        self.assertEqual(listing["people"], ["boss", "Pending"])
        self.assertEqual(listing["users"][0]["fleet_person"], "boss")
        self.assertIn("hosts_error", listing)
        self.assertIn("people_error", listing)
        self.assertNotIn("person_choices", listing)

    def test_only_one_account_can_open_for_registered_person(self):
        import concurrent.futures
        from labmon import fleetboot as FB
        self.admin()
        FB.set_person("Owner", create=True, roster_only=True)

        def add(name):
            try:
                return U.add(name, "user", "boss", person="Owner")
            except U.Denied as e:
                return e.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(add, ("amy", "bob")))
        self.assertEqual(results.count(409), 1)
        self.assertEqual(sum(isinstance(v, str) for v in results), 1)

    def test_no_plaintext_and_mode_600(self):
        code = fixtures.add_account("boss", "admin", "本机命令行", t=T0)
        raw, who = U.setup("boss", code.lower().replace("-", " "), "correct horse", "lan", "Safari", t=T0)   # 设置码不分大小写、不管空格与连字符
        self.assertEqual(who, "boss")
        tok, _ = U.token_add("boss", "部署", ["deploy"], t=T0)
        text = U.path().read_text()
        for secret in ("correct horse", code, code.replace("-", ""), raw, tok):
            self.assertNotIn(secret, text)
        d = json.loads(text)
        self.assertRegex(d["users"]["boss"]["pw"], r"^pbkdf2_sha256\$1000\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+$")
        self.assertIsNone(d["users"]["boss"]["setup"])                   # 用过的设置码作废
        self.assertEqual(stat.S_IMODE(U.path().stat().st_mode), 0o600)
        self.assertTrue(tok.startswith("lm_"))

    def test_pw_hash_salted_and_checked(self):
        a, b = U.hash_pw("same pw 123"), U.hash_pw("same pw 123")
        self.assertNotEqual(a, b)                                          # 随机盐
        self.assertTrue(U._pw_ok(a, "same pw 123"))
        self.assertFalse(U._pw_ok(a, "same pw 124"))
        self.assertFalse(U._pw_ok(None, "anything"))
        self.assertFalse(U._pw_ok("garbage", "anything"))
        self.assertTrue(U._pw_ok(U.hash_pw("x" * 8, it=500), "x" * 8))     # 轮数随哈希存：以后加轮数旧口令照样认

    def test_corrupt_file_means_nobody(self):
        sid = self.admin()
        U.path().write_text("{坏了")
        with self.assertRaises(U.Denied) as e:
            U.check(hdr(cookie=sid), "read")
        self.assertEqual(e.exception.code, 401)
        self.assertFalse(U.any_user())


class LoginTest(UsersBase):
    def test_login_and_wrong_pw(self):
        self.admin()
        raw, who = U.login("BOSS", "correct horse", "public", t=T0 + 5)     # 名字不分大小写
        self.assertEqual(who, "boss")
        w = U.check(hdr(cookie=raw), "admin", admin=True, t=T0 + 6)
        self.assertEqual((w.name, w.role, w.via), ("boss", "admin", "session"))
        self.assertIn(U.ACCOUNT, w.scopes)
        with self.assertRaises(U.Denied) as e:
            U.login("boss", "wrong", "lan", t=T0 + 7)
        self.assertEqual((e.exception.code, e.exception.msg), (401, "名字或口令不对"))

    def test_lock_after_five_failures_then_unlock(self):
        self.admin()
        for i in range(U.FAIL_MAX):
            with self.assertRaises(U.Denied) as e:
                U.login("boss", f"wrong{i}", "lan", t=T0 + 10 + i)
            self.assertEqual(e.exception.code, 401)
        with self.assertRaises(U.Denied) as e:                             # 锁着时对的口令也不行
            U.login("boss", "correct horse", "lan", t=T0 + 20)
        self.assertEqual(e.exception.code, 429)
        self.assertIn("15 分钟", e.exception.msg)
        self.assertTrue(any("连着输错 5 次" in r["msg"] for r in records(cat="config")))
        raw, _ = U.login("boss", "correct horse", "lan", t=T0 + 20 + U.LOCK_S)    # 15 分钟后解锁
        self.assertTrue(raw)
        self.assertNotIn("fails", json.loads(U.path().read_text())["users"]["boss"])   # 登录成功清零

    def test_lock_rechecked_before_write(self):
        # 并发的几次登录都在第一次失败落盘前过了开头的锁检查、排在 PBKDF2 上（PR #176 审查）：写之前按最新的账号状态再查锁，
        # 锁了的对的口令也不收、不清锁；错的不再记次数、不延长锁
        self.admin()
        lock = T0 + 30 + U.LOCK_S
        real = U._pw_ok

        def racing(stored, pw):
            with U._edit() as d:                                           # 验口令的这段时间里，别的请求输错满 5 次、把账号锁上了
                d["users"]["boss"]["lock"] = lock
            return real(stored, pw)
        for pw in ("correct horse", "wrong"):
            with U._edit() as d:
                d["users"]["boss"].pop("lock", None)
            with mock.patch.object(U, "_pw_ok", racing), self.assertRaises(U.Denied) as e:
                U.login("boss", pw, "lan", t=T0 + 30)
            self.assertEqual(e.exception.code, 429, pw)
            d = json.loads(U.path().read_text())
            self.assertEqual(len(d["sessions"]), 1, pw)                    # 只有 admin() 设口令时那一个，没开新会话
            self.assertEqual((d["users"]["boss"].get("lock"), d["users"]["boss"].get("fails")), (lock, None), pw)

    def test_unknown_name_same_answer_and_lock(self):
        self.admin()
        for i in range(U.FAIL_MAX):
            with self.assertRaises(U.Denied) as e:
                U.login("ghost", "whatever", "lan", t=T0 + i)
            self.assertEqual((e.exception.code, e.exception.msg), (401, "名字或口令不对"))
        with self.assertRaises(U.Denied) as e:
            U.login("ghost", "whatever", "lan", t=T0 + 10)
        self.assertEqual(e.exception.code, 429)
        self.assertNotIn("ghost", U.path().read_text())                    # 不存在的名字不落盘
        with mock.patch.object(U, "_pw_ok", wraps=U._pw_ok) as kdf:        # 不存在的名字照样算一次 PBKDF2
            with self.assertRaises(U.Denied):
                U.login("nobody", "whatever", "lan", t=T0)
            self.assertEqual(kdf.call_count, 1)

    def test_setup_code_single_use_and_expiry(self):
        code = fixtures.add_account("boss", "admin", "本机命令行", t=T0)
        with self.assertRaises(U.Denied) as e:
            U.setup("boss", code, "short", "lan", t=T0)
        self.assertEqual(e.exception.code, 400)                            # 口令太短：不算一次输错
        with self.assertRaises(U.Denied) as e:
            U.setup("boss", "AAAA-BBBB-CCCC", "long enough", "lan", t=T0)
        self.assertEqual((e.exception.code, e.exception.msg), (401, "名字或设置码不对"))
        U.setup("boss", code, "long enough", "lan", t=T0 + 1)
        with self.assertRaises(U.Denied):                                  # 用过就作废
            U.setup("boss", code, "another one", "lan", t=T0 + 2)
        code2 = fixtures.add_account("amy", "user", "boss", t=T0)
        with self.assertRaises(U.Denied) as e:
            U.setup("amy", code2, "long enough", "lan", t=T0 + U.SETUP_S + 1)
        self.assertEqual(e.exception.code, 400)
        self.assertIn("过期", e.exception.msg)

    def test_change_pw_logs_out_other_browsers(self):
        a = self.admin()
        b, _ = U.login("boss", "correct horse", "public", t=T0 + 1)
        tok, _ = U.token_add("boss", "读", ["read"], t=T0)
        who = U.check(hdr(cookie=a), U.ACCOUNT, t=T0 + 2)
        with self.assertRaises(U.Denied) as e:
            U.change_pw(who, "wrong", "brand new pw", t=T0 + 3)
        self.assertEqual((e.exception.code, e.exception.msg), (403, "原口令不对"))    # 不是 401：会话还有效，页面不该跳登录页（PR #176 审查）
        self.assertIn("别的 1 个浏览器", U.change_pw(who, "correct horse", "brand new pw", t=T0 + 3))
        U.check(hdr(cookie=a), "read", t=T0 + 4)                            # 这个浏览器留着
        with self.assertRaises(U.Denied):
            U.check(hdr(cookie=b), "read", t=T0 + 4)
        U.check(hdr(token=tok), "read", t=T0 + 4)                           # 令牌不动
        U.login("boss", "brand new pw", "lan", t=T0 + 5)


class ScopeTest(UsersBase):
    def test_public_ops_changes_existing_credentials(self):
        sid = self.admin()
        self.admin("other")
        token, _ = U.token_add("boss", "admin", ["admin"], t=T0)
        narrow, _ = U.token_add("boss", "project", ["project"], t=T0)
        for enabled in (True, False, True):
            U.set_public_ops("boss", enabled, "other")
            for headers in (hdr(cookie=sid), hdr(token=token)):
                who = U.check(headers, "read", t=T0)
                self.assertEqual(U.me(who, "public")["ops"], enabled)
            self.assertFalse(U.can_ops(U.check(hdr(token=narrow), "read", t=T0), "public"))
        U.set_role("boss", "user", "other")
        self.assertFalse(U.can_ops(U.check(hdr(cookie=sid), "read", t=T0), "public"))
        U.set_role("boss", "admin", "other")
        self.assertFalse(U.can_ops(U.check(hdr(cookie=sid), "read", t=T0), "public"))
        with self.assertRaises(U.Denied):
            U.set_public_ops("boss", "yes", "other")

    def test_token_scopes_and_role(self):
        sid = self.admin()
        ro, _ = U.token_add("boss", "只读", ["read"], t=T0)
        dep, _ = U.token_add("boss", "部署", ["deploy"], t=T0)
        pj, _ = U.token_add("boss", "会话", ["project"], t=T0)
        self.assertEqual(U.check(hdr(token=dep), ("project", "deploy"), t=T0).scopes, {"deploy", "read"})
        for tok, need in ((ro, "project"), (dep, "admin"), (pj, "admin"), (ro, U.ACCOUNT)):
            with self.assertRaises(U.Denied) as e:
                U.check(hdr(token=tok), need, t=T0)
            self.assertEqual(e.exception.code, 403)
        self.assertTrue(U.can_ops(U.check(hdr(cookie=sid), "admin", t=T0), "lan"))
        self.assertFalse(U.can_ops(U.check(hdr(cookie=sid), "admin", t=T0), "public"))
        self.assertFalse(U.can_ops(U.check(hdr(token=pj), "read", t=T0), "lan"))     # project 令牌做不了平台操作
        code = fixtures.add_account("amy", "user", "boss", t=T0)
        amy, _ = U.setup("amy", code, "amy password", "lan", t=T0)
        w = U.check(hdr(cookie=amy), "read", t=T0)
        self.assertEqual(w.scopes, {"read", "project", U.ACCOUNT})
        for need, admin in (("admin", False), ("read", True)):
            with self.assertRaises(U.Denied) as e:
                U.check(hdr(cookie=amy), need, admin=admin, t=T0)
            self.assertEqual(e.exception.code, 403)
        project, _ = U.token_add("amy", "实验", ["project"], t=T0)
        self.assertEqual(U.check(hdr(token=project), "project", t=T0).scopes, {"read", "project"})
        with self.assertRaises(U.Denied) as e:
            U.token_add("amy", "x", ["admin"], t=T0)
        self.assertEqual(e.exception.code, 403)
        me = U.me(w, "lan")
        self.assertEqual((me["admin"], me["ops"], me["write"], me["token_scopes"]), (False, False, True, ["read", "project"]))

    def test_admin_token_expires_in_an_hour(self):
        self.admin()
        tok, v = U.token_add("boss", "临时", ["admin"], t=T0)
        self.assertEqual(v["until"], T0 + U.ADMIN_TOKEN_S)
        U.check(hdr(token=tok), "admin", admin=True, t=T0 + 3599)
        with self.assertRaises(U.Denied) as e:
            U.check(hdr(token=tok), "read", t=T0 + 3601)
        self.assertEqual(e.exception.code, 401)

    def test_demoted_admin_loses_admin_at_once(self):
        self.admin()
        code = fixtures.add_account("amy", "admin", "boss", t=T0)
        amy, _ = U.setup("amy", code, "amy password", "lan", t=T0)
        tok, _ = U.token_add("amy", "临时", ["admin"], t=T0)
        U.check(hdr(token=tok), "admin", admin=True, t=T0)
        U.set_role("amy", "user", "boss")
        for h in (hdr(token=tok), hdr(cookie=amy)):
            with self.assertRaises(U.Denied) as e:
                U.check(h, "admin", admin=True, t=T0 + 1)
            self.assertEqual(e.exception.code, 403)
        self.assertEqual(U.check(hdr(token=tok), "read", t=T0 + 1).scopes, {"read", "project"})     # 令牌作用域 ∩ 角色

    def test_bad_auth_header_and_missing(self):
        with self.assertRaises(U.Denied) as e:
            U.check({}, "read")
        self.assertEqual(e.exception.code, 401)
        with self.assertRaises(U.Denied) as e:
            U.check({"Authorization": "Basic abc"}, "read")
        self.assertEqual(e.exception.code, 401)


class AdminTest(UsersBase):
    def test_last_admin_cannot_be_demoted(self):
        self.admin()
        with self.assertRaises(U.Denied) as e:
            U.set_role("boss", "user", "boss")
        self.assertEqual(e.exception.code, 409)
        self.assertIn("最后一个管理员", e.exception.msg)
        fixtures.add_account("amy", "admin", "boss", t=T0)
        self.assertEqual(U.set_role("boss", "user", "amy"), "boss 现在是普通用户")
        with self.assertRaises(U.Denied):
            U.set_role("amy", "user", "amy")

    def test_remove(self):
        """删账号（人不再用看板）：连同会话与令牌立即失效；删不了自己，也删不了最后一个管理员（本机命令行同样）。"""
        self.admin()
        fixtures.add_account("amy", "user", "boss", t=T0)
        tok, _ = U.token_add("amy", "读", ["read"], t=T0)
        U.check(hdr(token=tok), "read", t=T0)
        with self.assertRaises(U.Denied) as e:
            U.remove("boss", "boss")
        self.assertEqual(e.exception.code, 409)
        with self.assertRaises(U.Denied) as e:
            U.remove("boss", "本机命令行")
        self.assertIn("最后一个管理员", e.exception.msg)
        self.assertEqual(U.remove("AMY", "boss"), "删了账号 amy，它的会话与令牌都已失效")
        with self.assertRaises(U.Denied):
            U.check(hdr(token=tok), "read", t=T0)
        self.assertEqual([u["name"] for u in U.listing(t=T0)["users"]], ["boss"])
        self.assertFalse([r for r in U._read()["tokens"].values() if r["user"] == "amy"])
        with self.assertRaises(U.Denied) as e:
            U.remove("amy", "boss")
        self.assertEqual(e.exception.code, 404)

    def test_remove_drops_account_people(self):
        """#17：删账号时连同主机清单 [fleet.account_people] 里这个账号的对应（大小写不敏感）一起删，同名新账号不继承；别的行、别的段不动。"""
        import tomllib
        hosts = Path(os.environ["LABMON_HOSTS"])
        hosts.write_text('[fleet]\nme = "BOSS"   # 旧写法\n\n[fleet.account_people]\nboss = "BOSS"\nAmy = "AMY"   # 这人\n"bob" = "BOB"\n\n[[hosts]]\nid = "a"\nssh = "remote-a"\n')
        self.admin()
        fixtures.add_account("amy", "user", "boss", t=T0)
        U.remove("amy", "boss")
        text = hosts.read_text()
        self.assertEqual(tomllib.loads(text)["fleet"]["account_people"], {"boss": "BOSS", "bob": "BOB"})
        self.assertIn('me = "BOSS"   # 旧写法', text)
        self.assertIn('[[hosts]]\nid = "a"', text)
        fixtures.add_account("bob", "user", "boss", t=T0)
        hosts.write_text('[fleet]\naccount_people = {bob = "BOB"}\n')               # 写成内联表的：不删账号，说清楚怎么改
        with self.assertRaises(U.Denied) as e:
            U.remove("bob", "boss")
        self.assertEqual(e.exception.code, 409)
        self.assertIn("[fleet.account_people]", e.exception.msg)
        self.assertIn("bob", [u["name"] for u in U.listing(t=T0)["users"]])
        hosts.write_text('[fleet]\naccount_people = {boss = "BOSS"}\n')              # 没有这个账号的对应：照常删，主机清单不动
        U.remove("bob", "boss")
        self.assertEqual(hosts.read_text(), '[fleet]\naccount_people = {boss = "BOSS"}\n')

    def test_first_account_must_be_admin(self):
        with self.assertRaises(U.Denied) as e:
            fixtures.add_account("amy", "user", "本机命令行", t=T0)
        self.assertIn("--admin", e.exception.msg)
        for bad in ("", "1abc", "a b", "张三", "x" * 33):
            with self.assertRaises(U.Denied):
                U.add(bad, "admin", "本机命令行", t=T0, person="boss")
        fixtures.add_account("boss", "admin", "本机命令行", t=T0)
        with self.assertRaises(U.Denied) as e:
            U.add("Boss", "user", "boss", t=T0, person="boss")
        self.assertEqual(e.exception.code, 409)

    def test_revoke_takes_effect_immediately(self):
        sid = self.admin()
        tok, v = U.token_add("boss", "读", ["read"], t=T0)
        tok2, _ = U.token_add("boss", "部署", ["deploy"], t=T0)
        U.check(hdr(token=tok), "read", t=T0)
        self.assertIn("1 个令牌", U.revoke("boss", "boss", ids=[v["id"]], t=T0))
        with self.assertRaises(U.Denied) as e:
            U.check(hdr(token=tok), "read", t=T0)
        self.assertEqual(e.exception.code, 401)
        U.check(hdr(token=tok2), "read", t=T0)
        with self.assertRaises(U.Denied):
            U.revoke("boss", "boss", ids=[v["id"]], t=T0)
        msg = U.revoke("boss", "管理员 amy ", t=T0)
        self.assertIn("1 个会话、1 个令牌", msg)
        for h in (hdr(cookie=sid), hdr(token=tok2)):
            with self.assertRaises(U.Denied):
                U.check(h, "read", t=T0)
        self.assertTrue(any("吊销了账号 boss" in r["msg"] for r in records(cat="config")))

    def test_reset_logs_out_and_new_code(self):
        sid = self.admin()
        fixtures.add_account("amy", "user", "boss", t=T0)
        amy, _ = U.setup("amy", U.reset("amy", "boss", t=T0), "amy password", "lan", t=T0)
        tok, _ = U.token_add("amy", "读", ["read"], t=T0)
        code = U.reset("amy", "boss", t=T0 + 1)
        with self.assertRaises(U.Denied):
            U.check(hdr(cookie=amy), "read", t=T0 + 2)
        with self.assertRaises(U.Denied):
            U.login("amy", "amy password", "lan", t=T0 + 2)
        U.check(hdr(token=tok), "read", t=T0 + 2)                          # 令牌不动
        U.setup("amy", code, "new amy pw", "lan", t=T0 + 3)
        with self.assertRaises(U.Denied) as e:                             # 页面上不能重置自己
            U.reset("boss", "boss", t=T0)
        self.assertEqual(e.exception.code, 409)
        U.reset("boss", "本机命令行", self_ok=True, t=T0)
        with self.assertRaises(U.Denied):
            U.check(hdr(cookie=sid), "read", t=T0)

    def test_session_sliding_renewal(self):
        sid = self.admin()
        w = U.check(hdr(cookie=sid), "read", t=T0 + 60)
        self.assertIsNone(w.cookie)                                        # 一小时内不落盘、不重发
        w = U.check(hdr(cookie=sid), "read", t=T0 + U.TOUCH_S + 1)
        self.assertEqual(w.cookie, sid)
        late = T0 + U.SESSION_S + U.TOUCH_S                                # 续过期：从上次用起再算 30 天
        U.check(hdr(cookie=sid), "read", t=late)
        with self.assertRaises(U.Denied):
            U.check(hdr(cookie=sid), "read", t=late + U.SESSION_S + 1)

    def test_listing_has_no_secrets(self):
        sid = self.admin()
        tok, _ = U.token_add("boss", "部署", ["deploy"], t=T0)
        text = json.dumps(U.listing(t=T0))
        self.assertNotIn(tok, text)
        self.assertNotIn(sid, text)
        L = U.listing(t=T0)
        self.assertEqual([(u["name"], u["role"], u["pw"], len(u["sessions"]), len(u["tokens"])) for u in L["users"]], [("boss", "admin", True, 1, 1)])

    def test_local_ok_refuses_other_owner(self):
        self.admin()
        U.local_ok()
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(U.Denied):
                U.local_ok()


class ViewTest(UsersBase):
    def test_user_view_strips_fleet(self):
        self.admin()
        fixtures.add_account("amy", "user", "boss", t=T0)
        amy = U.Ident("amy", "user", frozenset({"read"}), "session", "k")
        boss = U.Ident("boss", "admin", U.ROLE_SCOPES["admin"], "session", "k")
        snap = {"fleet": {"servers": [1]}, "notices": ["x"], "wake": [{"key": "a"}], "hosts": [{"id": "h"}], "hostcheck": {"t": T0},
                "event_cats": [["link", "连接"], ["op", "平台操作"], ["queue", "队列"]],
                "disk": {"df": {"free": 1}, "owners": [{"name": "zz"}], "names": ["zz"], "owners_set": {}, "colors": {"zz": "#000"},
                         "alerts": [{"key": "low"}, {"key": "burst:zz"}, {"key": "over:zz"}]}}
        self.assertIs(U.view(boss, snap), snap)
        acc = U.Access({"p"}, {"h"}, {"h"})
        v = U.view(amy, snap, acc)
        self.assertEqual((v["fleet"], v["notices"], v["wake"], v["hosts"]), (None, [], [], [{"id": "h"}]))
        self.assertIsNone(v["hostcheck"])                                  # 看板服务所在机的体检（#166）也只给管理员
        self.assertEqual(v["disk"], {"df": {"free": 1}, "alerts": [{"key": "low"}]})
        self.assertEqual([c[0] for c in v["event_cats"]], ["link", "queue"])
        see = U.visible(amy, acc)
        self.assertIsNone(U.visible(boss))
        rows = [{"kind": "host_down", "host": "h"}, {"kind": "fleet_down"}, {"kind": "fleet_renew"}, {"kind": "guard_start"}, {"kind": "borrow"},
                {"kind": "disk"}, {"kind": "account"}, {"kind": "hostcheck"}, {"kind": "config", "who": "zz"}, {"kind": "config"}, {"kind": "config", "host": "h"}, {"kind": "done", "project": "p"}]
        self.assertEqual([r["kind"] for r in rows if see(r)], ["host_down", "config", "done"])

    def test_user_view_drops_old_pool_observed(self):
        # #12 删了算力池，但部署前写的 snapshot.json 里还有 pool_observed（各项目的节点观测与尝试回执）；
        # 备用机不重新采集、照读旧快照，普通用户的视图不能把它带出去
        amy = U.Ident("amy", "user", frozenset({"read"}), "session", "k")
        snap = {"hosts": [{"id": "h"}], "pool_observed": {"hosts": {"x": {"tail": "别人的日志"}}}}
        v = U.view(amy, snap, U.Access({"p"}, {"h"}, {"h"}))
        self.assertNotIn("pool_observed", v)

    def test_user_view_is_allowlist(self):
        # #17：普通用户的快照只取列出的字段，新加的字段默认不给；colors 只留自己的项目，server 不给主是谁，disk 不给正在扫的路径、别人的目录
        amy = U.Ident("amy", "user", frozenset({"read"}), "session", "k")
        snap = {"generated": T0, "lan": {"ok": True}, "event_kinds": {"done": "跑完"}, "sched": {"rows": []}, "future_secret": {"x": 1},
                "pool_observed": {"x": 1}, "host_notices": {"h": ["掉线"]}, "cached": True,
                "hosts": [{"id": "h", "status": {"running": [{"project": "q", "cmd": "python train.py"}]}}, {"id": "z"}],
                "projects": [{"id": "p"}, {"id": "q"}], "archived": [{"id": "p"}, {"id": "q"}], "rebalance": {"p": 1, "q": 2},
                "colors": {"p": "#111", "q": "#222", "某人": "#333"},
                "server": {"role": "standby", "id": "srv-main", "holder": "srv-other", "renew": T0, "until": T0, "bad": None, "home": "/h", "say": "主是 srv-other"},
                "disk": {"root": "/data", "df": {"free": 1}, "eta": 3, "hist": [[T0, 1]], "warn_gb": 9, "at": T0, "units": 4, "pending": 1, "yield": None,
                         "scanner": "z", "now": "/data/别人/私有目录", "colors": {"某人": "#000"}, "unscanned": 5, "new_field": 1,
                         "ours": {"base": "/data/mine", "dirs": [{"u": "/data/mine/a", "name": "a", "b": 1, "p": "p"}, {"u": "/data/mine/b", "name": "b", "b": 2, "p": "q"},
                                                                 {"u": "/data/mine/c", "name": "c", "b": 3, "p": None}]},
                         "alerts": [{"key": "low"}, {"key": "soon"}, {"key": "burst:zz"}, {"key": "x"}]}}
        v = U.view(amy, snap, U.Access({"p"}, {"h"}, {"h"}))
        self.assertEqual(set(v), {"generated", "lan", "event_kinds", "event_cats", "sched", "hosts", "projects", "archived", "rebalance", "regs", "occ",
                                  "colors", "server", "disk", "fleet", "notices", "wake", "hostcheck", "access"})
        self.assertEqual(v["hosts"], [snap["hosts"][0]])                     # 可见主机照整项给：别人任务的命令行照常显示（docs/api.md）
        self.assertEqual(v["colors"], {"p": "#111"})
        self.assertEqual(v["server"], {"role": "standby", "say": U.STANDBY_SAY})
        self.assertEqual(set(v["disk"]), {"root", "df", "eta", "hist", "warn_gb", "at", "units", "pending", "yield", "ours", "alerts"})
        self.assertEqual(v["disk"]["ours"], {"dirs": [snap["disk"]["ours"]["dirs"][0]]})
        self.assertEqual(v["disk"]["alerts"], [{"key": "low"}, {"key": "soon"}])
        snap["disk"]["scanner"] = "h"
        self.assertEqual(U.view(amy, snap, U.Access({"p"}, {"h"}, {"h"}))["disk"]["scanner"], "h")   # 扫盘的主机他看得见时才给
        snap["server"] = None
        self.assertIsNone(U.view(amy, snap, U.Access({"p"}, {"h"}, {"h"}))["server"])
        snap["server"] = {"role": "primary", "id": "srv-main", "say": ""}
        self.assertEqual(U.view(amy, snap, U.Access({"p"}, {"h"}, {"h"}))["server"], {"role": "primary", "say": ""})


if __name__ == "__main__":
    unittest.main()
