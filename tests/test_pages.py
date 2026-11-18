"""两个页面（overview.html、page.html）的渲染测试：用 collect.write_page 渲染，抽出脚本编译，再在 node 里装进最小 DOM 桩按路由渲染并断言。

快照由 tests/pages/snapshots.py 构造（正常、恶意字符串、字段大量缺省），DOM 桩在 tests/pages/dom.js，断言在 tests/pages/check.js：
node 只起一个进程，所有断言跑完后把结果以 JSON 回给这里，按分组落到各个测试方法（subTest 是每条断言）。
没有 node 时整组跳过。
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "tests")
os.environ.setdefault("LABMON_NO_NOTIFY", "1")
os.environ.setdefault("LABMON_NO_KEYCHAIN", "1")
os.environ.setdefault("LABMON_LAN", "on")
os.environ.setdefault("LABMON_HOSTS", os.path.join(HERE, "_no_user_hosts.toml"))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from labmon import collect as C  # noqa: E402
from pages import preview  # noqa: E402
from pages import snapshots as S  # noqa: E402

NODE = shutil.which("node")
SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


class PagesTest(unittest.TestCase):
    """渲染一次、node 跑一次，结果按分组分到各测试方法。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest("没有 node，页面的行为测试跳过")
        t0 = time.time()
        cls.tmp = tempfile.TemporaryDirectory()
        w = cls.tmp.name
        # 用恶意快照渲染：数据里带 </script> 与 <!--，页面必须仍只有一个 <script>；脚本本身与数据无关（第一行 let D 由桩替换）
        C.write_page("overview.html", S.overview_evil(), Path(w, "overview.html"), title="调度总览")
        C.write_page("page.html", S.project_evil(), Path(w, "page.html"), title="题" + S.BAD)
        cls.html = {}
        for name in ("overview", "page"):
            with open(os.path.join(w, name + ".html"), encoding="utf-8") as f:
                cls.html[name] = f.read()
            m = SCRIPT.findall(cls.html[name])
            if len(m) != 1:
                raise AssertionError(f"{name}.html 渲染后有 {len(m)} 个 <script>，应是 1 个")
            with open(os.path.join(w, name + ".js"), "w", encoding="utf-8") as f:
                f.write(m[0])
        with open(os.path.join(w, "login.html"), "w", encoding="utf-8") as f:          # 登录页（#160）：数据由看板服务填，这里只要脚本
            f.write(C.render_page("login.html", {"empty": False, "entry": "lan", "entry_name": "内网入口", "next": "", "pw_min": 8, "lock_min": 15, "fails": 5}))
        with open(os.path.join(w, "login.html"), encoding="utf-8") as f:
            m = SCRIPT.findall(f.read())
        with open(os.path.join(w, "login.js"), "w", encoding="utf-8") as f:
            f.write(m[0])
        for name, fn in {**S.ALL, **S.EXTRA}.items():
            with open(os.path.join(w, name + ".json"), "w", encoding="utf-8") as f:
                json.dump(fn(), f, ensure_ascii=False)
        r = subprocess.run([NODE, os.path.join(HERE, "pages", "check.js"), w], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"check.js 跑挂了（返回 {r.returncode}）：\n{r.stderr[-3000:]}")
        cls.results = json.loads(r.stdout)
        cls.elapsed = time.time() - t0

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "tmp", None):
            cls.tmp.cleanup()

    def _group(self, g):
        rows = [x for x in self.results if x["g"] == g]
        self.assertTrue(rows, f"check.js 没有分组 {g} 的结果")
        for x in rows:
            with self.subTest(x["n"]):
                self.assertTrue(x["ok"], x.get("d"))

    def _one(self, g, n):
        x = next((x for x in self.results if x["g"] == g and x["n"] == n), None)
        self.assertIsNotNone(x, f"check.js 没有 {g} / {n}")
        self.assertTrue(x["ok"], x.get("d"))

    # ---- 渲染（Python 侧） ----
    def test_write_page_escapes_data_and_title(self):
        """数据里的 < 转成 \\u003c、标题转义：恶意字符串进不了页面结构。"""
        for name, h in self.html.items():                                   # 只有一个 <script>：check.js 的 syntax 组数过
            self.assertNotIn("<i id=x>", h, name)
            self.assertNotIn("<!--<script>", h, name)
            self.assertNotIn("/*__DATA__*/null", h, name)
            self.assertNotIn("/*__COMMON__*/", h, name)
        self.assertTrue("<title>题&lt;i id=x&gt;" in self.html["page"], "项目页标题没转义")   # 工作台的 <title> 是固定的，write_page 只换项目页的

    def test_snapshots_have_no_real_names(self):
        """构造的快照里只许假名：没有真实网段、真人名字、真作业名。"""
        blob = json.dumps([fn() for fn in {**S.ALL, **S.EXTRA}.values()], ensure_ascii=False)
        self.assertNotRegex(blob, r"10\.12\.\d+\.\d+")
        for s in ("ME", "OWNER", "srv-a", "srv-b", "proj-a", "proj-b"):
            self.assertIn(s, blob)

    def test_posts_go_through_post_json(self):
        """页面上的写请求都经 common.js 的 postJSON（带 JSON 类型；看板只认 application/json 的 POST，漏了的那个按钮会被拒，#158）：
        模板里不另写 POST 的 fetch，也不用 XMLHttpRequest、sendBeacon 发请求。表单（登录页、账号详情，#160）只用来接回车与让口令管理认出来：
        提交由页面拦下改经 postJSON；都写 method="post"、不写 action，脚本没跑起来时浏览器自己提交也是表单类型的 POST（看板回 415），口令不会进地址栏。"""
        for f in ("overview.html", "page.html", "login.html", "common.js"):
            with open(os.path.join(ROOT, "labmon", f), encoding="utf-8") as fh:
                s = fh.read()
            self.assertEqual(len(re.findall(r"""method\s*:\s*["'`]post""", s, re.I)), int(f == "common.js"), f)
            self.assertNotRegex(s, r"XMLHttpRequest|sendBeacon", f)
            for m in re.finditer(r"<form\b[^>]*>", s):
                self.assertIn('method="post"', m[0], f)
                self.assertNotIn("action=", m[0], f)
            self.assertEqual(len(re.findall(r"<form\b", s)), {"overview.html": 2, "login.html": 1}.get(f, 0), f)
            if f == "common.js":
                self.assertRegex(s, r'const postJSON = \(url, body = \{\}, how\) => api\(url, \{method: "POST", headers: \{"Content-Type": "application/json"\}')
            # 请求都经 api（连不上时说人话、按需重试，#177）：模板里不另写 fetch；“看板服务连不上”的说法只在 common.js 的 netText
            self.assertEqual(len(re.findall(r"\bfetch\(", s)), int(f == "common.js"), f)
            self.assertEqual(s.count("看板服务连不上"), int(f == "common.js"), f)

    def test_urls_relative(self):
        """页面上的请求、链接与 iframe 都写相对地址（#165）：TX 的 nginx 把公网入口挂在 /<前缀>/ 下，以 / 开头的会跑出前缀。
        node 里的「路径前缀」那组按 /pfx/ 实际跑一遍；这里把模板里的写法全扫一遍，免得新加的按钮漏掉。"""
        for f in ("overview.html", "page.html", "login.html", "common.js"):
            with open(os.path.join(ROOT, "labmon", f), encoding="utf-8") as fh:
                s = fh.read()
            bad = re.findall(r"""\b(?:api|postJSON|fetch)\(\s*["'`]/|\b(?:href|src|action)\s*=\s*\\?["'`]/(?!/)|\.src\s*=\s*["'`]/|location(?:\.href)?\s*=\s*["'`]/""", s)
            self.assertEqual(bad, [], f)

    # ---- node 里的断言，按分组 ----
    def test_syntax(self):
        """抽出的脚本能编译（与 node --check 同一件事）、页面只有一个 <script>、k5 样式还在。"""
        self._group("syntax")

    def test_overview(self):
        """工作台：所有路由与记录卡渲染不报错、不漏 NaN/undefined；第 1、2 批修复的回归断言。"""
        self._group("overview")

    def test_escape(self):
        """恶意字符串：两个页面所有路由、抽屉、提示、日志末尾、时间线都转义。"""
        self._group("escape")

    def test_sparse(self):
        """字段大量缺省：worker 没部署、worker 停着、不在内网、组内与共享盘读不了、项目页探针失败与已归还。"""
        self._group("sparse")

    def test_project(self):
        """项目页：各块、GiB、KPI、任务明细的时刻与时长、hostBands、ovrOf、cached、刷新不冲掉选区、时间线范围。"""
        self._group("project")

    def test_compact(self):
        """大量小规模并行（Issue #69）：计划项多时完成的全收起，在跑的多时运行中按计划项分组、一个任务一行，可切回详细。"""
        self._group("compact")

    def test_gsq(self):
        """项目页「已完成 N 项」收起行的小方块（Issue #115）：每格自己的悬停、按跑完时刻排且与展开列表同序、多于上限只画最近的、点一格定位、触屏先看悬停。"""
        self._group("gsq")

    def test_timeline(self):
        """时间线的近景按现在前后的任务定宽（Issue #49）：高并行短任务放大、长尾不拉宽、暂时没实验取 24 小时（#85）、滞回、拖过不重算、窄屏。"""
        self._group("timeline")

    def test_manage(self):
        """项目的「管理」抽屉（Issue #93）：路由与顶栏按钮、主机与队列按登记分组（已归还、没读到、只显示、下一轮采集后显示）、调度可改（#121）、项目配置可在页面上改（#122）、转义与缺省。"""
        self._group("manage")

    def test_archive(self):
        """项目的归档与恢复（Issue #97）：管理抽屉的「归档」节（按 archive_block 置灰写原因、确认弹窗）、侧栏「已归档 N 个」与已归档的抽屉（恢复弹窗、回执）、转义。"""
        self._group("archive")

    def test_reg(self):
        """搬、移除队列登记与接上原来的项目（Issue #95）：主机页「队列」与抽屉的按钮、能搬去的主机、弹窗、接上卡的排序与默认勾选、转义、file:// 没有按钮。"""
        self._group("reg")

    def test_auth(self):
        """看板账号（Issue #160）：普通用户看不到组内服务器与写按钮、公网入口的管理员看不到平台操作按钮、「我的账号」「管理 → 账号」、会话失效回登录页、登录页。"""
        self._group("auth")

    def test_common(self):
        """第 4 批：悬停提示外壳、租期文字、主机短名、车道名、本机存储只在 common.js 一份（源码上两页同一行，行为上各一条）。"""
        self._group("common")

    def test_zz_report_elapsed(self):
        """整组用时（渲染 + node 一个进程）控制在几秒内。"""
        self.assertLess(self.elapsed, 20, f"test_pages 用了 {self.elapsed:.1f} 秒")


class PreviewTest(unittest.TestCase):
    """离线预览（tests/pages/preview.py）：快照改名后它不会悄悄坏掉；退出时删掉自己的临时目录。不需要 node。"""

    def test_build_writes_pages_and_json(self):
        for kind in preview.KINDS:
            with self.subTest(kind), tempfile.TemporaryDirectory() as d:
                ids = preview.build(Path(d), kind)
                self.assertTrue(ids)
                for f in ("index.html", "overview.json", "records.json"):
                    self.assertTrue(Path(d, f).is_file(), f)
                for i in ids:
                    for f in ("index.html", "snapshot.json", "events.json"):
                        self.assertTrue(Path(d, "p", i, f).is_file(), f"p/{i}/{f}")

    def test_build_as_accounts(self):
        """--as（#160）：ME 照看板服务填进两页，/api/me 与登录页写出来；管理员另有 /api/users，普通用户的快照没有组内服务器。"""
        for who in preview.AS:
            with self.subTest(who), tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LABMON_STATE": d + "/st"}):
                out = Path(d, "site")
                ids = preview.build(out, "normal", preview.accounts(who))
                me = json.loads(Path(out, "api", "me.json").read_text())
                self.assertEqual((me["name"], me["entry"], me["ops"]), {"admin": ("boss", "lan", True), "public": ("boss", "public", False), "user": ("amy", "lan", False), "empty": ("carol", "lan", False)}[who])
                self.assertEqual(Path(out, "api", "users.json").exists(), who in ("admin", "public"))
                self.assertIn("用设置码设口令", Path(out, "login.html").read_text())
                for f in [Path(out, "index.html")] + [Path(out, "p", i, "index.html") for i in ids]:
                    self.assertNotIn("/*__ME__*/null", f.read_text(), f)
                self.assertEqual(json.loads(Path(out, "overview.json").read_text())["fleet"] is None, who in ("user", "empty"))
                if who in ("user", "empty"):
                    self.assertEqual(ids, ["proj-a"] if who == "user" else [])
                self.assertNotIn("假口令", json.dumps(me, ensure_ascii=False))

    def test_main_removes_tmp_dir(self):
        """Ctrl-C 正常退出、端口被占给一句中文提示退出，临时目录都删掉；SIGTERM、SIGHUP 都接住；环境变量只在 main 里改。"""
        made = []
        real = tempfile.mkdtemp

        def mkdtemp(**kw):
            made.append(real(**kw))
            return made[-1]

        for err in (KeyboardInterrupt, OSError):
            srv = mock.Mock()
            srv.return_value.serve_forever.side_effect = KeyboardInterrupt
            if err is OSError:
                srv.side_effect = OSError("Address already in use")
            with self.subTest(err.__name__), mock.patch.dict(os.environ), mock.patch.object(sys, "argv", ["preview.py", "--port", "8798"]), \
                    mock.patch.object(preview.tempfile, "mkdtemp", mkdtemp), mock.patch.object(preview, "ThreadingHTTPServer", srv), \
                    mock.patch.object(preview.signal, "signal") as sig, redirect_stdout(StringIO()):
                if err is OSError:
                    with self.assertRaises(SystemExit) as cm:
                        preview.main()
                    self.assertIn("端口 8798 用不了", str(cm.exception.code))
                else:
                    preview.main()
                self.assertEqual({c.args[0] for c in sig.call_args_list}, {signal.SIGTERM, signal.SIGHUP})
                self.assertTrue(Path(made[-1]).name.startswith("labmon-preview-"))
                self.assertFalse(os.path.exists(made[-1]), "临时目录没删")


if __name__ == "__main__":
    unittest.main()
