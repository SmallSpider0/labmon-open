"""用户级主机清单（~/.config/labmon/hosts.toml）与项目配置的合并。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402, F401 — 设测试环境
from labmon import config as K  # noqa: E402
from labmon import state as ST  # noqa: E402
from labmon.config import ConfigError, load, sources  # noqa: E402

USER = """
[[hosts]]
id = "g1"
label = "g1（A6000）"
ssh = "root@192.0.2.1"
port = 2200
mounts = [{ path = "/data", label = "共享盘", min_gb = 10 }, "/tmp"]

[[hosts.checks]]
id = "keepalive"
label = "保活"
cmd = "true"
detail = ["tail -n 1 ka.log"]
when_idle = "通用提示"

[[hosts]]
id = "g2"
ssh = "root@192.0.2.2"
"""


class HostsTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name).resolve()
        (self.d / "hosts.toml").write_text(USER)
        self.old = os.environ.get("LABMON_HOSTS")
        os.environ["LABMON_HOSTS"] = str(self.d / "hosts.toml")
        self.old_state = os.environ.get("LABMON_STATE")
        os.environ["LABMON_STATE"] = str(self.d / "state")              # 改主机清单会记进记录：别写进真的状态目录

    def tearDown(self):
        if self.old is None:
            os.environ.pop("LABMON_HOSTS", None)
        else:
            os.environ["LABMON_HOSTS"] = self.old
        if self.old_state is None:
            os.environ.pop("LABMON_STATE", None)
        else:
            os.environ["LABMON_STATE"] = self.old_state
        self.td.cleanup()

    def cfg(self, text: str) -> dict:
        p = self.d / "monitor.toml"
        p.write_text(text)
        return load(p)

    def test_returned_absolute(self):
        """returned 落盘的是绝对时刻：命令行 host return --at 认相对写法（18:40、明天 9:00），写进清单前换成带年份的；
        手写成相对写法的读清单时照旧报认不出，不会每天读成当天（PR #10 审查）。"""
        import contextlib
        import datetime as dt
        import io
        from labmon.__main__ import main
        fixtures.board(self, scopes=("admin",))                                       # 经看板接口（#162）：时刻在命令行这边解析，看板那边写清单
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["host", "return", "g1", "--at", "今天 00:00"]), 0)
        self.assertIn("主机 g1 标为已归还", out.getvalue())
        today = dt.date.today().strftime("%Y-%m-%d")
        self.assertIn(f'returned = "{today} 00:00"', (self.d / "hosts.toml").read_text())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["host", "return", "g2", "--at", "乱写"]), 3)                    # 认不出：不写进清单
        self.assertNotIn('returned = "乱写"', (self.d / "hosts.toml").read_text())
        with self.assertRaises(ConfigError):
            self.cfg('[[hosts]]\nid = "x"\nreturned = "18:40"\n[[queues]]\nid = "q"\nhost = "x"\n')

    def test_project_extends_user_host(self):
        c = self.cfg("""
[[hosts]]
id = "g1"
mounts = [{ path = "/tmp", min_gb = 50 }]
[[hosts.checks]]
id = "keepalive"
detail = ["echo 本项目登记"]
when_idle = "本项目提示"

[[queues]]
id = "q"
host = "g1"
""")
        h, = c["hosts"]
        self.assertEqual((h["ssh"], h["port"], h["label"]), ("root@192.0.2.1", 2200, "g1（A6000）"))
        self.assertEqual([(m["path"], m.get("min_gb")) for m in h["mounts"]], [("/data", 10), ("/tmp", 50)])
        k, = h["checks"]
        self.assertEqual((k["label"], k["cmd"], k["when_idle"]), ("保活", "true", "本项目提示"))
        self.assertEqual(k["detail"], ["tail -n 1 ka.log", "echo 本项目登记"])
        self.assertEqual(sources(c), [str(self.d / "monitor.toml"), str(self.d / "hosts.toml")])

    def test_queue_pulls_unlisted_host(self):
        c = self.cfg('[[queues]]\nid = "q"\nhost = "g2"\n')
        self.assertEqual([h["id"] for h in c["hosts"]], ["g2"])
        self.assertEqual(c["hosts"][0]["ssh"], "root@192.0.2.2")

    def test_unknown_host(self):
        with self.assertRaises(ConfigError):
            self.cfg('[[queues]]\nid = "q"\nhost = "nope"\n')

    def test_project_only_host_still_works(self):
        c = self.cfg('[[hosts]]\nid = "x"\nssh = "u@h"\n[[queues]]\nid = "q"\nhost = "x"\n')
        self.assertEqual(c["hosts"][0]["ssh"], "u@h")

    def test_numeric_queue_host(self):
        c = self.cfg('[[queues]]\nid = "q"\nhost = 1\n[[hosts]]\nid = 1\nssh = "u@one"\n')     # host 与 id 写成 TOML 整数也按 "1" 认
        self.assertEqual((c["hosts"][0]["id"], c["queues"][0]["host"]), ("1", "1"))
        (self.d / "hosts.toml").write_text(USER + '\n[[hosts]]\nid = "7"\nssh = "u@seven"\n')
        c = self.cfg('[[queues]]\nid = "q"\nhost = 7\n')                                    # 用户级清单里的主机同样
        self.assertEqual([h["ssh"] for h in c["hosts"]], ["u@seven"])

    def test_set_keys_keeps_comments(self):
        # 只改块顶层的那一行：值换了、行尾注释留着；没有的键加在 id 行后面；None 删行；子表（[[hosts.checks]]）里的同名键不动
        block = ['id = "b"   # 借用\n', 'ssh = "u@b"   # 地址\n', 'port = 22   # 端口\n', 'auto = false\n', "\n", "[[hosts.checks]]\n", 'id = "k"\n', 'port = 1\n']
        out = K._set_keys(list(block), {"port": 2200, "ssh": "u@c", "auto": True, "label": "B 机", "shared": ["/a", "/b"], "nope": None})
        self.assertEqual(out, ['id = "b"   # 借用\n', 'shared = ["/a", "/b"]\n', 'label = "B 机"\n', 'ssh = "u@c"   # 地址\n', 'port = 2200   # 端口\n',
                               'auto = true\n', "\n", "[[hosts.checks]]\n", 'id = "k"\n', 'port = 1\n'])
        self.assertEqual(K._set_keys(list(out), {}), out)
        self.assertEqual(K._set_keys(list(out), {"port": None, "label": None})[:4], ['id = "b"   # 借用\n', 'shared = ["/a", "/b"]\n', 'ssh = "u@c"   # 地址\n', 'auto = true\n'])
        self.assertEqual(K._set_keys(["x = 1\n"], {"y": "2"}), ['y = "2"\n', "x = 1\n"])                 # 没有 id 行：加在最前面
        self.assertEqual(K._set_keys(['shared = ["/a"]   # 共用\n'], {"shared": ["/b"]}), ['shared = ["/b"]   # 共用\n'])
        self.assertEqual(K._set_keys(["n = 1.5   # 数\n"], {"n": 2}), ["n = 2   # 数\n"])

    def test_set_keys_inline_table_message(self):
        # 主机清单里写成内联表、多行数组的值这里不改：说人话，不是 tomllib 的原话
        hf = self.d / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "g1"\nmounts = [\n  "/data",\n]\n[fleet]\nguard = { idle_min = 30 }\n')
        with self.assertRaises(ConfigError) as e:
            K.set_fleet_keys({"guard": "x"}, "试")
        self.assertIn("guard 写成了内联表或多行数组", str(e.exception))
        with self.assertRaises(ConfigError) as e:
            K.set_fleet_keys({"mounts": ["/x"]}, "试", host="g1")
        self.assertIn("mounts 写成了内联表或多行数组", str(e.exception))
        hf.write_text('[sched]\nweight = { a1 = 2 }\n')
        with self.assertRaises(ConfigError) as e:
            K.set_weight("b", 2)
        self.assertIn("改成 [sched.weight] 段", str(e.exception))
        self.assertEqual(hf.read_text(), '[sched]\nweight = { a1 = 2 }\n')                   # 没写入

    def test_sched_pause_focus_global(self):
        # #96：项目级的暂停、优先记在主机清单 [sched.pause] / [sched.focus]（所有主机，含之后加入的），随 sched.json 下发；
        # 增、撤、到期各记一条 override 记录（没有 host，项目页时间线据此画区间）；到期的不下发，expire_sched 删掉那行
        import json
        import time
        hf = self.d / "hosts.toml"
        now = time.time()
        self.assertIn("所有主机", K.set_sched_ovr("pause", "demo-b", True, now + 3600))
        K.set_sched_ovr("focus", "a.b", True)                                          # 无期限；不是裸键的 id 加引号
        K.set_weight("demo-b", 2)
        text = hf.read_text()
        self.assertIn(f'[sched.pause]\ndemo-b = "{K.fmt_time(now + 3600, year=True)}"\n', text)
        self.assertIn('[sched.focus]\n"a.b" = true\n', text)
        self.assertTrue(text.startswith(USER))                                          # 别的行不动
        u = K.parse_time(K.fmt_time(now + 3600, year=True))
        self.assertEqual(K.sched(), {"weight": {"demo-b": 2}, "pause": {"demo-b": u}, "focus": {"a.b": None}})
        self.assertEqual(K.sched(now + 7200), {"weight": {"demo-b": 2}, "focus": {"a.b": None}})     # 到期的不下发
        with self.assertRaises(ConfigError):                                            # 已经过去的到期拒绝，提示与命令行同一句
            K.set_sched_ovr("pause", "x", True, now - 60)
        self.assertIn("本来就没有", K.set_sched_ovr("focus", "x", False))
        self.assertEqual(K.expire_sched(now), [])
        self.assertEqual(K.expire_sched(now + 7200), [f"暂停 demo-b，到 {K.fmt_time(u)}"])
        self.assertNotIn("demo-b = ", hf.read_text().split("[sched.weight]")[0])            # 那行删了，权重不动
        self.assertIn("撤销 a.b 的优先", K.set_sched_ovr("focus", "a.b", False))
        ev = [json.loads(l) for l in (self.d / "state" / "overview" / "events.jsonl").read_text().splitlines()]
        ov = [(e["kind"], e.get("act"), e.get("okind"), e.get("target"), e.get("host")) for e in ev if e.get("okind")]
        self.assertEqual(ov, [("override", "add", "pause", "demo-b", None), ("override", "add", "focus", "a.b", None),
                              ("override", "expire", "pause", "demo-b", None), ("override", "rm", "focus", "a.b", None)])
        hf.write_text('[sched]\npause = { a1 = true, b1 = "2026-01-01 00:00" }\n')
        with self.assertRaises(ConfigError) as e:                                       # 内联表：说人话，不写
            K.set_sched_ovr("pause", "b", True)
        self.assertIn("改成 [sched.pause] 段", str(e.exception))
        from labmon import overview as O                                               # 到期的删不掉（内联表）不挡这一轮采集（Codex 审查）
        with mock.patch.object(O, "log") as log:
            O.collect([], use_ssh=False)
            O.collect([], use_ssh=False)
        self.assertEqual(log.call_count, 1)                                             # 同一句只记一次
        self.assertEqual(K.sched()["pause"], {"a1": None})                             # 过期的照样不下发

    def test_concurrent_edits_and_appends(self):
        # 几个线程同时改主机清单（页面线程、工作台线程、组内操作线程）：读 → 改 → 写整段互斥，谁的改动都不丢；
        # 同时续写同一个 JSONL 并触发修剪也不丢行
        import threading
        hf = self.d / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "g1"\n')
        n = 12
        ts = [threading.Thread(target=K.set_weight, args=(f"p{i}", i + 2)) for i in range(n)]
        ts += [threading.Thread(target=K.set_fleet_keys, args=({f"k{i}": i}, "试"), kwargs={"host": "g1"}) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(K.sched()["weight"], {f"p{i}": i + 2 for i in range(n)})
        self.assertEqual({k: v for k, v in K.user_hosts()[0].items() if k.startswith("k")}, {f"k{i}": i for i in range(n)})
        self.assertEqual([x.name for x in hf.parent.iterdir() if x.name.endswith(".tmp")], [])      # 原子写：临时文件改名后不留
        f = self.d / "rows.jsonl"
        ts = [threading.Thread(target=lambda i=i: [ST.append_row(f, {"t": 1000 + i, "i": i, "j": j}, keep=5000, t=1000 + i, slack=0) for j in range(50)]) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(ST.load_rows(f)), 400)

    def test_edits_exclusive_across_processes(self):
        # 本机命令行（另一个进程）与看板服务同时改主机清单：读 → 改 → 写整段跨进程互斥（#18）。另一个进程读完、写之前停 1.5 秒，
        # 这边这时也改：只有线程锁时这边读到旧文本、先写，那边随后写回自己的那份，这边的改动就丢了
        import subprocess
        import time
        hf = self.d / "hosts.toml"
        hf.write_text('[[hosts]]\nid = "g1"\n')
        code = ("import sys, time\nfrom labmon import config as K\norig = K._write_checked\n"
                "def slow(*a, **k):\n    print('ready', flush=True)\n    time.sleep(1.5)\n    return orig(*a, **k)\n"
                "K._write_checked = slow\nK.set_weight('child', 2)\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        root = str(Path(__file__).resolve().parent.parent)
        p = subprocess.Popen([sys.executable, "-c", code], cwd=root, env=dict(env, PYTHONPATH=root), stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "ready")
            t0 = time.monotonic()
            K.set_weight("parent", 3)
            self.assertGreater(time.monotonic() - t0, 0.5)                 # 等那边写完才读
        finally:
            p.stdout.close()
            self.assertEqual(p.wait(timeout=30), 0)
        self.assertEqual(K.sched()["weight"], {"child": 2, "parent": 3})
        self.assertEqual(K.HOSTS_LOCK._depth, 0)                          # 最外层退出时放掉了文件锁
        with K.HOSTS_LOCK, K.HOSTS_LOCK:                                   # 同一线程可重入（_hosts_edit 的函数互相调用）
            K.set_weight("again", 4)
        self.assertEqual(K.sched()["weight"]["again"], 4)

    def test_no_user_file(self):
        os.environ["LABMON_HOSTS"] = str(self.d / "missing.toml")
        c = self.cfg('[[queues]]\nid = "q"\n')
        self.assertEqual([h["id"] for h in c["hosts"]], ["local"])
        self.assertNotIn("hosts_file", c)


class TimeTest(unittest.TestCase):
    def test_fmt_and_parse(self):
        import datetime as dt
        t = dt.datetime(2026, 11, 7, 9, 5, 3).timestamp()
        self.assertEqual((K.fmt_time(t), K.fmt_time(t, year=True), K.fmt_time(t, sec=True), K.fmt_time(t, date=False), K.fmt_time(t, year=True, sec=True)),
                         ("11-07 09:05", "2026-11-07 09:05", "11-07 09:05:03", "09:05", "2026-11-07 09:05:03"))
        self.assertEqual(K.parse_time("2026-11-07 09:05:03"), t)
        self.assertEqual(K.parse_time("2026-11-07T09:05"), t - 3)
        self.assertEqual(K.parse_time(dt.datetime(2026, 11, 7, 9, 5)), t - 3)                  # TOML 原生日期时间 str() 出来的
        self.assertEqual(K.parse_time("2026-11-07"), dt.datetime(2026, 11, 7, 23, 59, 59).timestamp())   # 只写日期：当天结束
        self.assertIsNone(K.parse_time("明天下午"))
        # rel=True（只有命令行的 parse_until 传）：按 t 那天算的今天 / 明天 / 后天 HH:MM、只写 HH:MM（今天），精确到分（Issue #9）；跨月、跨年照常进位
        nov8 = dt.datetime(2026, 11, 8, 15, 20).timestamp()
        self.assertEqual(K.parse_time("今天 18:30", nov8, rel=True), dt.datetime(2026, 11, 8, 18, 30).timestamp())
        self.assertEqual(K.parse_time("明天 9:05", nov8, rel=True), dt.datetime(2026, 11, 9, 9, 5).timestamp())
        self.assertEqual(K.parse_time("后天23:59", nov8, rel=True), dt.datetime(2026, 11, 10, 23, 59).timestamp())
        self.assertEqual(K.parse_time("21:45", nov8, rel=True), dt.datetime(2026, 11, 8, 21, 45).timestamp())
        self.assertEqual(K.parse_time("明天 00:10", dt.datetime(2026, 12, 31, 22).timestamp(), rel=True), dt.datetime(2027, 1, 1, 0, 10).timestamp())
        self.assertIsNone(K.parse_time("明天 24:00", nov8, rel=True))
        self.assertIsNone(K.parse_time("今天 18:61", nov8, rel=True))
        # 缺省（主机清单、记录、returned）不认相对写法：落盘的文字每天读出来都不一样，照旧报认不出
        for s in ("18:30", "明天 18:30", "今天 9:00"):
            self.assertIsNone(K.parse_time(s, nov8), s)
        self.assertEqual(K._until("明天 18:30"), {"until": None, "until_bad": "明天 18:30"})
        self.assertIsNone(K.parse_time(None))
        # 不带年份：记录不晚于 t 一天（跨年的算去年），命令行写的到期不早于 t（过了算明年）
        jan5 = dt.datetime(2027, 1, 5).timestamp()
        self.assertEqual(K.parse_time("12-31 23:00", jan5), dt.datetime(2026, 12, 31, 23).timestamp())
        self.assertEqual(K.parse_time("01-02 03:04", jan5), dt.datetime(2027, 1, 2, 3, 4).timestamp())
        self.assertEqual(K.parse_time("01-02 03:04", jan5, future=True), dt.datetime(2027, 1, 2, 3, 4).timestamp())   # 过去不到半年：照写，调用方报已经过去
        self.assertEqual(K.parse_time("01-05", dt.datetime(2026, 12, 28, 15).timestamp(), future=True),
                         dt.datetime(2027, 1, 5, 23, 59, 59).timestamp())                   # 跨年：12 月底写 01-05 算明年
        self.assertEqual(K.parse_time("11-08 09:00", dt.datetime(2026, 11, 8, 15).timestamp(), future=True),
                         dt.datetime(2026, 11, 8, 9).timestamp())                            # 今天早些时候：不变成明年（Issue #9）
        self.assertEqual(K.parse_time("01-06", jan5, future=True), dt.datetime(2027, 1, 6, 23, 59, 59).timestamp())
        self.assertEqual(K.parse_time("2026-11-01", jan5, future=True), dt.datetime(2026, 11, 1, 23, 59, 59).timestamp())   # 写全年份照写的算

    def test_check_future(self):
        """时刻要在将来（借用的约定、优先与命令行的到期共用一处）：过去的与正好现在的抛 ConfigError，文案按参数。"""
        now = 1_800_000_000.0
        K.check_future(None, t=now)
        K.check_future(now + 60, t=now)
        for v in (now, now - 60):
            with self.assertRaises(K.ConfigError) as c:
                K.check_future(v, "约定时间", t=now)
            self.assertEqual(str(c.exception), f"约定时间 {K.fmt_time(v)} 已经过去了：换一个更晚的时刻")
        with self.assertRaises(K.ConfigError) as c:
            K.check_future(now - 60, t=now)
        self.assertTrue(str(c.exception).startswith("到期时间 "))


if __name__ == "__main__":
    unittest.main()
