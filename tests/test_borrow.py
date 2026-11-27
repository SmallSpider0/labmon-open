"""借用组内服务器的离线测试：借来的主机清单、借来与拒借、约定时间与提醒、自动归还、wait 的全局消息，
以及给对方留的量（按历史推荐、随 guest 指令下发），worker 的 guest 指令（只限项目、扣预留、对方超出预留的部分不放行、快撞上限只提醒、
被挤掉重排）与独占任务被打扰作废。不连 ssh：探测、ssh、拉起 worker、下指令都换成假的。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import functools
import json
import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import Hosts, fake_host, fake_status, make_token, running_job, worker_module  # noqa: E402
from labmon import borrow as B  # noqa: E402
from labmon import config as K  # noqa: E402
from labmon import fleet as F  # noqa: E402
from labmon import fleetops as FO  # noqa: E402
from labmon import overview as O  # noqa: E402
from labmon import platform as PF  # noqa: E402
from labmon import workerctl as W  # noqa: E402
from labmon.waits import wait_check  # noqa: E402
from labmon.config import ConfigError, load, user_hosts  # noqa: E402
from labmon.records import records  # noqa: E402
from labmon.state import save_json  # noqa: E402

HOSTS = """
[[hosts]]
id = "29"
ssh = "root@198.51.100.29"
port = 15155
python = "/usr/bin/python3"
shared = ["/root/data1"]
worker = "auto"

[fleet]
keepalive_cmd = "bash ka.sh"

[[fleet.servers]]
id = "27-15152"
holder = "BOB"
ssh = "root@198.51.100.27"
port = 15152
pool = "p"
"""
SID = "27-15152"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {"LABMON_HOSTS": str(self.d / "hosts.toml"), "LABMON_STATE": str(self.d / "state")})
        self.env.start()
        (self.d / "hosts.toml").write_text(HOSTS)
        for name in ("me", "second", "third"):                              # 借用人按登录账号绑着的组内人员认（#19：只认账号库的绑定）
            make_token(name)
        self.calls, self.keeps = [], []
        B._REC.clear()                                                        # 推荐的预留按小时缓存，各测试的状态目录不同

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def ovr(self, action, kind, hid, target=None, until=None, now=False, at=None, keep=None):
        self.calls.append((action, kind, hid, target, round(at) if at else None))
        if keep:
            self.keeps.append(keep)
        return 0

    def server(self) -> dict:
        return next(s for s in tomllib.loads((self.d / "hosts.toml").read_text())["fleet"]["servers"] if s["id"] == SID)

    def ep(self, key="borrow_since") -> int:
        """wake 的借用 key 带的这一次借用（借来的时刻；已还的取归还时刻，#109）。"""
        return round(K.parse_time(self.server()[key]))

    def begin(self, work=(), until=None, projects=None, ssh_out="", light=False, away=False, account="me", ssh_err="", **a):
        a, self.remote = {"work": list(work), **a}, []
        with mock.patch.object(F, "parse", lambda raw: raw), mock.patch.object(F, "analyze", lambda smp, cfg: a):
            return B.begin(SID, until, projects, fetch=lambda s: ("raw", None), ssh=lambda s, cmd, input=None: self.remote.append((cmd, input)) or (0, ssh_out, ssh_err),
                           start=lambda hid: (0, "ok\n"), ovr=self.ovr, light=light, away=away, account=account)


class BorrowTest(Base):
    def test_guest_host_copies_template(self):
        self.assertEqual([h["id"] for h in user_hosts()], ["29"])             # 没借：只有自用主机
        self.begin(until=time.time() + 3600, projects="a1")
        g = next(h for h in user_hosts() if h["id"] == SID)
        self.assertEqual((g["python"], g["shared"], g["worker"], g["label"]), ("/usr/bin/python3", ["/root/data1"], "auto", "27（借 BOB）"))
        self.assertEqual((g["borrow"]["holder"], g["borrow"]["for"]), ("BOB", ["a1"]))
        self.assertTrue(g["known_hosts"].endswith("fleet/known_hosts"))      # 主机密钥沿用组内服务器的
        self.assertIsNone(g["returned"])

    def test_begin_writes_keys_and_overrides(self):
        until = time.time() + 7200
        msg = self.begin(until=until, projects=["a1", "demo-b"])
        s = self.server()
        self.assertTrue(s["borrow_since"])
        self.assertEqual((s["borrow_until"], s["borrow_for"]), (K.fmt_time(until, year=True), ["a1", "demo-b"]))
        cut = K.parse_time(s["borrow_until"])
        self.assertIn(("add", "guest", SID, "a1,demo-b", None), self.calls)
        self.assertEqual(self.keeps, [{"vram": 2.0, "ram": 4.0, "cpu": 1.0, "hold": False}])  # 没有历史：下限；对方空着时借的不预先留
        self.assertIn("借来时对方空着，不预先留", msg)

        self.assertIn(("add", "deadline", SID, "borrow", round(cut)), self.calls)
        self.assertIn("worker 已拉起", msg)
        self.assertEqual(F.guard_state({**s, "guard": True, "pool": "p"}, {"sample": {"work": []}}, {"private_pools": []}, time.time())["status"], "borrowed")

    def test_borrow_records_authenticated_person(self):
        self.begin(account="second")
        self.assertEqual(self.server()["borrow_by"], "second")
        self.assertEqual(F.borrower(self.server(), F.conf()), "second")
        B.begin(SID, time.time() + 3600, ovr=self.ovr, account="third")
        self.assertEqual(self.server()["borrow_by"], "second")

    def test_retime_tells_projects(self):
        """改约定后经 records.tell 告诉用这台的项目（#127）：借到的那条 wake 的 key 同一次借用里不变，wait 不会再报。
        告诉谁：改前、改后 borrow_for 的并集，有一边不限就是看板上所有项目；不再给用的另说；没变不说；命令行来的 src 写明。"""
        from labmon import api
        from labmon.records import told
        t0 = time.time()
        self.begin(until=t0 + 3600, projects=["a1"])
        self.assertEqual(told(), [])                                          # 借来本身由 borrow.check 的 wake 报，不经 tell
        until = round(t0 + 7200)
        B.begin(SID, until, ["a1", "demo-b"], ovr=self.ovr, account="me")    # 页面上改约定：延长、多给一个项目
        w = told()
        self.assertEqual(sorted(x["projects"][0] for x in w), ["a1", "demo-b"])
        self.assertTrue(all(x["key"].startswith("page:borrow:") for x in w))
        m = next(x["msg"] for x in w if x["projects"] == ["a1"])
        self.assertTrue(m.startswith("看板页面上 "))
        self.assertIn(f"到 {K.fmt_time(until)}", m)
        self.assertIn("之前跑不完的不放行", m)
        B.begin(SID, until, ["a1", "demo-b"], ovr=self.ovr, account="me")    # 同样再提交一次：没变不说
        self.assertEqual(len(told()), 2)
        with mock.patch.object(B.PJ, "project_ids", lambda: ["a1", "demo-b", "c3"]):
            with mock.patch.object(B, "begin", functools.partial(B.begin, ovr=self.ovr)):
                api.borrow(SID, None, "demo-b", None, False, account="me")   # 命令行：不约定、只给 demo-b
        w = told()[2:]
        self.assertEqual(sorted(x["projects"][0] for x in w), ["a1", "demo-b"])
        a1 = next(x["msg"] for x in w if x["projects"] == ["a1"])
        self.assertTrue(a1.startswith("命令行 labmon borrow "))
        self.assertIn("不再放行 a1 的新任务", a1)
        self.assertIn("不再按约定截止", next(x["msg"] for x in w if x["projects"] == ["demo-b"]))
        with mock.patch.object(B.PJ, "project_ids", lambda: ["a1", "demo-b", "c3"]):
            B.begin(SID, None, None, ovr=self.ovr, account="me")              # 改成不限项目：看板上所有项目
        self.assertEqual(sorted(x["projects"][0] for x in told()[4:]), ["a1", "c3", "demo-b"])

    def test_retime_while_returning(self):
        """要还期间改约定（Codex 审 #128）：drain 与要还标记都还在，通知不说“可以接着加任务”，说要还、请用户撤销还；现在停的撤不了。"""
        from labmon.records import told
        self.begin(until=time.time() + 3600, projects=["a1"])
        B.request_return(SID, ovr=self.ovr)
        B.begin(SID, None, ["a1"], ovr=self.ovr, account="me")
        m = told()[-1]["msg"]
        self.assertNotIn("可以接着加任务", m)
        self.assertIn("别往上加任务", m)
        self.assertIn("撤销还", m)
        B.request_return(SID, now=True, ovr=self.ovr)
        B.begin(SID, time.time() + 7200, ["a1"], ovr=self.ovr, account="me")
        m = told()[-1]["msg"]
        self.assertNotIn("撤销还", m)
        self.assertIn("正在停下", m)

    def test_begin_refuses(self):
        train = {"kind": "job", "label": "train", "names": [], "owner": None, "mb": 1000}
        with self.assertRaises(ConfigError) as c:
            self.begin(work=[train])
        self.assertIn("不是轻用，不能借", str(c.exception))
        with self.assertRaises(ConfigError) as c:                            # 轻用：要勾「对方在轻用」
            self.begin(work=[train], share=True, vscode=True)
        self.assertIn("在轻用（train、VS Code）", str(c.exception))
        self.assertNotIn("borrow_since", self.server())

    def test_begin_checks_each_keepalive(self):
        """借用前逐个保活 GPU 进程核对在不在 ka.sh running 的进程组里（fleet_guard.sh 的 kacheck，与查共享盘同一条 ssh，#29）：
        一个不在就借不了，提示写明哪个进程（命令行、PID、工作目录）、为什么停不了、对方该怎么做（原来括号里是空的）；ka.sh 出错另一句。"""
        ours = {"kind": "keepalive", "label": "train_keep_alive.py", "names": [], "mb": 500, "gpids": [9, 10], "cwd": "/root/data1/alice/ka",
                "chain": ["/opt/py/bin/python /root/data1/alice/ka/train_keep_alive.py"]}
        theirs = {"kind": "keepalive", "label": "my_keep_alive.py", "names": [], "mb": 500, "gpids": [21], "cwd": "/root/bob",
                  "chain": ["bash run.sh", "python my_keep_alive.py"]}
        with self.assertRaises(ConfigError) as c:
            self.begin(work=[ours, theirs], ssh_out="noka 21\n")
        msg = str(c.exception)
        self.assertIn("GPU 上的保活 python my_keep_alive.py（PID 21，工作目录 /root/bob）不在 ka.sh 管的进程组里", msg)
        self.assertIn("请 BOB 先停掉它，要保活改用 ka.sh start 开", msg)
        self.assertNotIn("train_keep_alive", msg)
        cmd, script = self.remote[-1]
        self.assertTrue(cmd.endswith("; bash -s -- kacheck 'bash ka.sh' 9 10 21"), cmd)     # 只读动作，脚本经标准输入送去
        self.assertEqual(script, F.GUARD.read_text())
        with self.assertRaises(ConfigError) as c:
            self.begin(work=[ours], ssh_out="fail ka.sh running 出错（退出码 127）\n", ssh_err="bash: ka.sh: No such file or directory\n")
        self.assertIn("核对保活时 ka.sh running 出错（退出码 127）：bash: ka.sh: No such file or directory，请先查 [fleet] keepalive_cmd", str(c.exception))
        with self.assertRaises(ConfigError) as c:
            self.begin(work=[ours], ssh_out="nopy\n")                                       # 脚本没回结果：不当成核对过
        self.assertIn("核对保活没拿到结果", str(c.exception))
        self.assertNotIn("borrow_since", self.server())
        self.assertIn("worker 已拉起", self.begin(work=[ours], ssh_out="ok\n"))

    def test_begin_light(self):
        """对方轻用（VS Code 加一个小实验）时勾了也借：借得了；保活的核对只在有保活时做。"""
        msg = self.begin(work=[{"kind": "job", "label": "small", "names": [], "mb": 900}], share=True, vscode=True, light=True)
        self.assertIn("worker 已拉起", msg)
        self.assertIn("给对方留显存 2 GiB", msg)
        self.assertEqual(self.keeps[-1]["hold"], True)                       # 对方在轻用：预先留出
        self.assertTrue(self.server()["borrow_light"])
        self.assertNotIn("kacheck", self.remote[0][0])
        self.assertTrue(self.server()["borrow_since"])

    def test_begin_away(self):
        """对方说好约定时间前不用（#30）：主机清单记 borrow_away（取约定时间），guest 指令的 keep 带 away；没约定时间不能勾；改约定不勾就去掉。"""
        until = round(time.time() + 7200)
        with self.assertRaises(ConfigError) as c:
            self.begin(away=True, light=True, vscode=True, share=True)
        self.assertIn("先选约定时间", str(c.exception))
        msg = self.begin(until=until, light=True, away=True, vscode=True, share=True)
        self.assertEqual(self.server()["borrow_away"], K.fmt_time(until, year=True))
        away = K.parse_time(self.server()["borrow_away"])
        self.assertEqual((self.keeps[-1]["hold"], self.keeps[-1]["away"]), (True, round(away)))
        self.assertIn("BOB 说好这段时间不用", msg)
        self.assertIn("前不用：不给对方留余量", msg)
        self.assertEqual(next(h for h in user_hosts() if h["id"] == SID)["borrow"]["away"], away)
        self.keeps.clear()
        B.begin(SID, until, None, ovr=self.ovr)                               # 改约定、不勾：去掉，重下的指令不带 away
        self.assertNotIn("borrow_away", self.server())
        self.assertNotIn("away", self.keeps[-1])
        self.assertTrue(B._drift({**self.keeps[-1], "away": round(away)}, self.keeps[-1]))   # 指令里还带着 away 的要重下

    def test_check_away_ended_once(self):
        """worker 判对方回来了（guest.away.end）：check 清掉 borrow_away、重下不带 away 的指令、按 borrow_back 提醒一次。"""
        t = time.time()
        until = round(t + 86400)
        self.begin(until=until, away=True)
        away = K.parse_time(self.server()["borrow_away"])
        ov = [{"kind": "guest", "target": None, "keep": dict(self.KEEP, away=round(away))},
              {"kind": "deadline", "target": "borrow", "at": K.parse_time(self.server()["borrow_until"])}]
        e = self.host_entry(back=["卡 0：对方用 0.5 GiB 显存，借来时对方空着"], overrides=ov)
        e["status"]["guest"]["away"] = {"until": round(away), "end": round(t), "why": "卡 0 上有了对方的 GPU 进程"}
        self.keeps.clear()
        msgs, wake = B.check([e], t, ovr=self.ovr)
        self.assertTrue(any("说好" in m and "前不用，现在回来了（卡 0 上有了对方的 GPU 进程）" in m and "恢复按预留借卡" in m for m in msgs))
        self.assertEqual(len(msgs), 1, msgs)                                  # 同一件事一条：不再另说“用得比预留多”
        self.assertNotIn("borrow_away", self.server())
        self.assertNotIn("away", self.keeps[-1])
        self.assertIn(f"borrow_back:{SID}:{self.ep()}:away:{round(t)}", [w["key"] for w in wake])   # 带这一次借用与作废的时刻（#129）
        self.assertFalse(e["borrow"].get("away"))
        e["status"]["guest"].update(back=[], away=None)                       # 看板重下了不带 away 的指令；对方在预留以内（back 为空）
        msgs, wake = B.check([e], t + 60, ovr=self.ovr)
        self.assertFalse(any("现在回来了" in m for m in msgs))               # 清掉以后不再说
        self.assertIn(f"borrow_back:{SID}:{self.ep()}:away:{round(t)}", [w["key"] for w in wake])  # wait 一小时内照样报（back 为空也报）
        _, wake = B.check([e], t + 3700, ovr=self.ovr)
        self.assertFalse(any(":away:" in w["key"] for w in wake))

    def test_check_away_flicker_and_rechecked(self):
        """说好不用还有效时，新出现的对方 GPU 进程（worker 那张卡当轮不放，连着两轮才作废）不提醒、不报 borrow_back；
        worker 报的作废是上一次的（用户刚在改约定里换了时间重新勾）：不清新勾的 borrow_away（审查非阻塞 5）。"""
        t = time.time()
        until = round(t + 86400)
        self.begin(until=until, away=True)
        away = K.parse_time(self.server()["borrow_away"])
        ov = [{"kind": "guest", "target": None, "keep": dict(self.KEEP, away=round(away))},
              {"kind": "deadline", "target": "borrow", "at": K.parse_time(self.server()["borrow_until"])}]
        e = self.host_entry(back=["卡 0：对方用 0.5 GiB 显存，说好不用、这是新出现的"], overrides=ov)
        e["status"]["guest"]["away"] = {"until": round(away), "end": None, "why": None}
        msgs, wake = B.check([e], t, ovr=self.ovr)
        self.assertEqual(msgs, [])
        self.assertFalse([w for w in wake if w["key"].startswith("borrow_back")])
        e["status"]["guest"]["away"] = {"until": round(away) - 3600, "end": round(t), "why": "卡 0 上有了对方的 GPU 进程"}
        B.check([e], t + 10, ovr=self.ovr)
        self.assertEqual(self.server()["borrow_away"], K.fmt_time(until, year=True))


    def test_reserve_from_history(self):
        """给对方留的量：近 7 天对方轻用时用量的加权 95 分位 ×1.15，不低于下限；内存直接取不算页缓存的 ma；跑大实验时、借用中（bw）的样本不算；被挤到后乘倍数。"""
        t = time.time()
        f = self.d / "state" / "overview" / "fleet" / "series" / f"{SID}.jsonl"
        f.parent.mkdir(parents=True)
        rows = [{"t": t - i * 600, "st": "job", "sh": True, "vr": 6144, "m": 17.0, "mr": 15.8, "ma": 6.0, "mk": 1.2, "cr": 2.0} for i in range(100)]
        rows += [{"t": t - i * 600 - 100, "st": "job", "sh": False, "vr": 40000, "mr": 60.0, "ma": 50.0, "mk": 1.2, "cr": 20.0} for i in range(100)]   # 跑大实验时
        rows += [{"t": t - i * 600 - 300, "st": "idle", "vr": 40000, "mr": 60.0, "ma": 50.0, "mk": 1.2, "cr": 20.0, "bw": 1} for i in range(100)]   # 借用中：有我们的
        rows += [{"t": t - 8 * 86400, "st": "idle", "vr": 40000, "mr": 60.0, "ma": 50.0, "mk": 1.2, "cr": 20.0}]                                  # 7 天前的不算
        f.write_text("".join(json.dumps(r) + "\n" for r in rows))
        k = B.reserve_for(SID, t)
        self.assertEqual((k["vram"], k["ram"], k["cpu"], k["n"]), (6.9, 6.9, 2.3, 100))
        self.assertEqual(k["peak"], {"vram": 6.0, "ram": 6.0, "cpu": 2.0})
        self.assertEqual(B.reserve_for(SID, t, 1.2)["vram"], 8.3)
        self.assertEqual(B.reserve_for("nohist", t)["vram"], B.RESERVE_MIN["vram"])
        # ma 为 None 时不退回含页缓存的 mr，回落到下限
        f.write_text("".join(json.dumps({**r, "ma": None} if r.get("sh") else r) + "\n" for r in rows))
        B._REC.clear()
        k = B.reserve_for(SID, t)
        self.assertEqual((k["ram"], k["peak"]["ram"]), (B.RESERVE_MIN["ram"], 0.0))

    def test_begin_as_op_shows_progress(self):
        """看板上的借来用在后台跑（fleetops.run_borrow）：每步写进 op.json；开始与完成只记借用那一条，不另记平台操作。"""
        from labmon import fleetops as FO
        real, a = B.begin, {"work": [{"kind": "keepalive", "label": "ka", "names": [], "mb": 500}]}

        def fake(sid, until, projects, step=None, light=False, away=False, account=None):
            return real(sid, until, projects, fetch=lambda s: ("raw", None), ssh=lambda s, cmd: (0, "", ""),
                        start=lambda hid: (0, "ok\n"), ovr=self.ovr, step=step, light=light, away=away, account=account)
        with mock.patch.object(F, "parse", lambda raw: raw), mock.patch.object(F, "analyze", lambda smp, cfg: a), \
                mock.patch.object(B, "begin", fake):
            FO.start("borrow", F.conf(), None, ids=[SID], projects=["a1"], account="second")
            for _ in range(100):
                if not FO.op().get("active"):
                    break
                time.sleep(0.05)
        op = FO.op()
        self.assertEqual(self.server()["borrow_by"], "second")
        self.assertFalse(op.get("error"))
        self.assertEqual([x["name"] for x in op["steps"]], B.BEGIN_STEPS)
        self.assertEqual([x["state"] for x in op["steps"]], ["ok"] * 4)
        self.assertEqual(op["steps"][0]["note"], "只有保活")
        self.assertIn("worker 已拉起", op["steps"][-1]["note"])
        kinds = [r["kind"] for r in records()]
        self.assertIn("borrow", kinds)
        self.assertNotIn("op_start", kinds)

    def test_cutoff_takes_earlier(self):
        b = {"holder": "BOB", "until": time.time() + 86400}                 # 主机清单里的 borrow_until 读进来已是 epoch 秒
        lease = time.time() + 3 * 3600
        cut, why = B.cutoff(b, lease)
        self.assertAlmostEqual(cut, lease - B.MARGIN_S)
        self.assertIn("租期", why)
        self.assertEqual(B.cutoff({**b, "until": None}, None), (None, None))

    def test_until_minutes(self):
        """约定精确到分（Issue #9）：主机清单里写 YYYY-MM-DD HH:MM、读回同一分；与对方租期截止按分钟取早；
        提前 2 小时的提醒从那一分起、到点的从约定那一分起。"""
        lt = time.localtime(time.time() + 86400)
        until = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 18, 37, 0, 0, 0, -1))
        self.begin(until=until)
        self.assertEqual(self.server()["borrow_until"], time.strftime("%Y-%m-%d 18:37", lt))
        b = next(h for h in user_hosts() if h["id"] == SID)["borrow"]
        self.assertEqual(b["until"], until)
        self.assertEqual(B.cutoff(b, until + B.MARGIN_S + 60), (until, "约定"))     # 对方租期截止晚 1 分钟：取约定
        self.assertEqual(B.cutoff(b, until + B.MARGIN_S - 60)[0], until - 60)       # 早 1 分钟：取租期截止
        say = lambda t: B.check([self.host_entry()], t, finish_fn=lambda *a, **k: "已还", ovr=self.ovr)[0]   # 到点没任务在跑就归还：换成假的（#60：以前真连 ssh）
        self.assertEqual(say(until - B.DUE_WARN_S - 60), [])
        soon = say(until - B.DUE_WARN_S)
        self.assertTrue(any(f"约定 {K.fmt_time(until)} 还给 BOB" in m and "还有 2.0 小时" in m for m in soon), soon)
        self.assertFalse(any("到了约定时间" in m for m in say(until - 60)))
        self.assertTrue(any(f"到了约定时间（{K.fmt_time(until)}）" in m for m in say(until)))

    def test_past_until_refused(self):
        """已经过去的约定时间：借来用、改约定、换给自己的约定都拒绝，主机清单不动（Issue #9）。"""
        t = time.time()
        with self.assertRaises(ConfigError) as c:
            self.begin(until=t - 60)
        self.assertIn("已经过去", str(c.exception))
        self.assertNotIn("borrow_since", self.server())
        self.begin(until=t + 3600)
        with self.assertRaises(ConfigError):
            self.begin(until=t - 60)
        self.assertEqual(self.server()["borrow_until"], K.fmt_time(t + 3600, year=True))
        with self.assertRaises(ConfigError):
            B.set_swap("29", t - 60, "CAROL", ovr=self.ovr)
        self.assertNotIn("borrow", next(x for x in user_hosts() if x["id"] == "29"))

    def test_old_until_formats(self):
        """旧数据：只写日期的 borrow_until 取当天结束，整点的照读。"""
        from labmon.config import set_fleet_keys
        import datetime as dt
        self.begin(until=time.time() + 3600)
        for raw, want in (("2026-12-01", dt.datetime(2026, 12, 1, 23, 59, 59)), ("2026-12-01 18:00", dt.datetime(2026, 12, 1, 18))):
            set_fleet_keys({"borrow_until": raw}, "手改", sid=SID)
            b = next(h for h in user_hosts() if h["id"] == SID)["borrow"]
            self.assertEqual((b["until"], b["until_bad"]), (want.timestamp(), None))

    def host_entry(self, running=(), back=(), overrides=None, voided=(), hit=None):
        """借来的 27 在工作台快照里的样子：running 是 [(任务名, pid)]，占位任务名里带 hold。"""
        return fake_host(SID, label="27（借 BOB）", status=dict(
            running=[running_job(n, "a1", pid=pid, hold=n.startswith("hold")) for n, pid in running],
            guest={"back": list(back), "hit": hit}, voided=list(voided), overrides=overrides or [], info={"gpus": [{"name": "A6000"}] * 4}))

    KEEP = {"vram": 2.0, "ram": 4.0, "cpu": 1.0, "hold": False}

    def test_idle_borrowed_host_wakes_session(self):
        # #43（维护者 2026-11-18 定只给会话）：借来的主机上没有任务在跑、也没有在等的，连着 IDLE_S 才给 wait 一条 borrow_idle（key 带起点，只报一次）；
        # 有任务在跑就清掉起点
        t = time.time()
        self.begin(projects="a1")
        ov = [{"kind": "guest", "target": "a1", "keep": self.KEEP}]
        fin = lambda hid, **k: "已还"
        keys = lambda wake: [w["key"] for w in wake if w["key"].startswith("borrow_idle:")]
        _, wake = B.check([self.host_entry(overrides=ov)], t, finish_fn=fin, ovr=self.ovr)
        self.assertEqual(keys(wake), [])
        _, wake = B.check([self.host_entry(overrides=ov)], t + B.IDLE_S + 1, finish_fn=fin, ovr=self.ovr)
        w = next(w for w in wake if w["key"].startswith("borrow_idle:"))
        self.assertEqual((w["key"], w["projects"]), (f"borrow_idle:{SID}:{round(t)}", ["a1"]))
        self.assertIn("空着", w["msg"])
        _, wake = B.check([self.host_entry(running=[("x1", 5)], overrides=ov)], t + B.IDLE_S + 2, finish_fn=fin, ovr=self.ovr)
        self.assertEqual(keys(wake), [])
        _, wake = B.check([self.host_entry(overrides=ov)], t + B.IDLE_S + 3, finish_fn=fin, ovr=self.ovr)
        self.assertEqual(keys(wake), [])                                     # 新的起点，还不到 IDLE_S

    def test_check_due_then_return(self):
        t = time.time()
        self.begin(until=t + 600, projects="a1")
        cut = K.parse_time(self.server()["borrow_until"])
        ov = [{"kind": "guest", "target": "a1", "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": cut}]
        done = []
        fin = lambda hid, wrote=None, kill=(), **k: done.append((hid, list(kill))) or "已还"
        e = self.host_entry(running=[("x1", 5)], overrides=ov)
        self.calls.clear()
        msgs, wake = B.check([e], t, finish_fn=fin, ovr=self.ovr)
        self.assertEqual(self.calls, [])                                      # 指令齐了：不再下
        self.assertTrue(any("约定" in m and "还有" in m for m in msgs))      # 提前 2 小时内提醒
        self.assertEqual([w["key"] for w in wake], [f"borrow:{SID}:{self.ep()}"])
        self.assertEqual(wake[0]["projects"], ["a1"])
        self.assertIn("4 张 A6000", wake[0]["msg"])
        msgs, wake = B.check([self.host_entry(running=[("x1", 5)], overrides=ov)], cut + 1, finish_fn=fin, ovr=self.ovr)
        self.assertIn(("add", "drain", SID, None, None), self.calls)        # 到点：不再放新任务，不停正在跑的
        self.assertTrue(any("到了约定时间" in m and "x1 跑完就还" in m for m in msgs))
        self.assertEqual(done, [])
        self.assertTrue(any(w["key"].startswith(f"borrow_due:{SID}:{self.ep()}:") for w in wake))
        msgs, _ = B.check([self.host_entry(running=[("hold_x", 9)], overrides=ov)], cut + 60, finish_fn=fin, ovr=self.ovr)
        self.assertEqual(done, [(SID, [9])])                                  # 只剩占位任务：归还，并停掉占位任务
        self.assertIn("已还", msgs)

    def test_due_lists_late_jobs(self):
        """borrow_due 的提醒（提前 2 小时、到点、wait 的全局消息）附上约定前跑不完、不会在这台放的任务数；late_jobs 也写进 e["borrow"]，
        看板的 borrow_cut 告警按它认。续租截止比约定更早时挡住的可能是续租，不算。"""
        t = time.time()
        self.begin(until=t + 3600, projects="a1")
        cut = K.parse_time(self.server()["borrow_until"])
        ov = [{"kind": "guest", "target": "a1", "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": cut}]
        why = "约定归还 前跑不完（估 183 分钟）"

        def entry(ovr):
            e = self.host_entry(running=[("x1", 5)], overrides=ovr)
            e["status"].update(
                waiting=[{"kind": "queue", "queue": "jobs/p1f.txt", "root": "/r", "project": "a1", "name": "s1", "why": why, "held": "deadline"},
                         {"kind": "queue", "queue": "jobs/other.txt", "root": "/r", "project": "a1", "name": "o1", "why": "没有可用显存", "held": None}],
                todo=[{"project": "a1", "root": "/r", "queue": "jobs/other.txt", "jobs": ["x1", "x2"], "deps": {}, "held": None},
                      {"project": "a1", "root": "/r", "queue": "jobs/p1f.txt", "jobs": ["s1", "s2", "s3", "s4"], "deps": {}, "held": None}])
            return e
        late = B.late_jobs(entry(ov)["status"])
        self.assertEqual([(x["queue"], x["name"], x["n"]) for x in late], [("jobs/p1f.txt", "s1", 4)])
        # 并行清单（#147）：同一条清单有一行在跑、窗口里两行被截止挡住：一条清单一项，n 是还没开跑的行数
        e = entry(ov)
        e["status"]["waiting"].insert(1, dict(e["status"]["waiting"][0], name="s2"))
        e["status"]["running"] = [{"project": "a1", "root": "/r", "queue": "jobs/p1f.txt", "name": "s0"}]
        e["status"]["todo"][1]["jobs"] = ["s0", "s1", "s2", "s3", "s4"]
        self.assertEqual([(x["queue"], x["name"], x["n"]) for x in B.late_jobs(e["status"])], [("jobs/p1f.txt", "s1", 4)])
        self.assertEqual(B.late_jobs(entry(ov + [{"kind": "deadline", "target": None, "at": cut - 600}])["status"]), [])   # 续租截止更早
        self.assertEqual(B.late_jobs(entry([ov[0]])["status"]), [])                                                       # 没有约定归还
        e = entry(ov)
        msgs, _ = B.check([e], t, finish_fn=lambda *a, **k: "已还", ovr=self.ovr)
        self.assertTrue(any("还有" in m and "排着的 4 个任务在约定前跑不完" in m and "a1 的 jobs/p1f.txt 4 个" in m for m in msgs), msgs)
        self.assertEqual(e["borrow"]["late"], late)
        self.assertEqual((e["borrow"]["late_do"], e["borrow"]["late_say"]), (B.late_do("约定"), B.late_do("约定", user=True)))
        msgs, wake = B.check([entry(ov)], cut + 1, finish_fn=lambda *a, **k: "已还", ovr=self.ovr)
        self.assertTrue(any("到了约定时间" in m and "排着的 4 个任务" in m for m in msgs), msgs)
        # 弹给用户的提醒写人话（late_say），给会话的 wake 写操作（late_do），#142
        self.assertFalse([m for m in msgs if "labmon worker add" in m], msgs)
        self.assertTrue(any(m.endswith(B.late_do("约定", user=True)) for m in msgs if "到了约定时间" in m), msgs)
        due = next(w["msg"] for w in wake if w["key"].startswith(f"borrow_due:{SID}:{self.ep()}:"))
        self.assertIn("给已有清单追加行也不要", due)
        self.assertIn("排着的 4 个任务在约定前跑不完", due)
        self.assertTrue(due.endswith(B.late_do("约定")), due)

    def test_late_back_kept_when_fetch_fails(self):
        """工作台某一轮读这台失败（没有 worker 状态）：沿用上一次的 late、back、tight，borrow_cut 的依据与 borrow_back 的 wake 不消失再出现
        （labmon wait 按 key 去重，消失一轮再出现会重复叫醒）；再拿到状态时按新的算，没有了就清掉。"""
        t = time.time()
        self.begin(until=t + 86400, projects="a1")
        cut = K.parse_time(self.server()["borrow_until"])
        ov = [{"kind": "guest", "target": "a1", "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": cut}]
        why = "约定归还 前跑不完（估 183 分钟）"

        def entry(held="deadline", back=("卡 0：对方用 5.0 GiB 显存，预留 2",)):
            e = self.host_entry(back=back, overrides=ov)
            e["status"].update(waiting=[{"kind": "queue", "queue": "jobs/p1f.txt", "root": "/r", "project": "a1", "name": "s1", "why": why, "held": held}],
                               todo=[{"project": "a1", "jobs": ["s1", "s2"], "deps": {}, "held": None}])
            e["status"]["guest"]["tight"] = "卡 0：快满"
            return e
        e = entry()
        _, wake = B.check([e], t, ovr=self.ovr)
        late = e["borrow"]["late"]
        self.assertEqual([(x["queue"], x["n"]) for x in late], [("jobs/p1f.txt", 2)])
        self.assertIn(f"borrow_back:{SID}:{self.ep()}", [w["key"] for w in wake])
        for down in (fake_host(SID, label="27（借 BOB）", error="timeout"),                          # 连不上
                     fake_host(SID, label="27（借 BOB）", status_error="JSON 读不出")):               # 状态读不出
            _, wake = B.check([down], t + 60, ovr=self.ovr)
            self.assertEqual(down["borrow"]["late"], late)
            self.assertEqual(down["borrow"]["late_do"], B.late_do("约定"))
            self.assertEqual(down["borrow"]["back"], ["卡 0：对方用 5.0 GiB 显存，预留 2"])
            self.assertEqual(down["borrow"]["tight"], "卡 0：快满")
            self.assertIn(f"borrow_back:{SID}:{self.ep()}", [w["key"] for w in wake])
        e = entry(held=None, back=())
        e["status"]["guest"].pop("tight")
        _, wake = B.check([e], t + 120, ovr=self.ovr)                         # 拿到状态：跑得完了、对方回落，清掉
        self.assertEqual((e["borrow"]["late"], e["borrow"]["back"], e["borrow"]["tight"], e["borrow"]["late_do"]), ([], [], None, None))
        self.assertNotIn(f"borrow_back:{SID}:{self.ep()}", [w["key"] for w in wake])

    def test_late_after_cutoff_until_returned(self):
        """到了约定时间以后，看板下了 drain，但 deadline 指令在列表里排在 drain 前面（override add 追加到末尾），队首仍是 held=deadline：
        late 照报（borrow_cut 归还前一直报），不是交给 drain:。"""
        t = time.time()
        self.begin(until=t + 3600, projects="a1")
        cut = K.parse_time(self.server()["borrow_until"])
        ov = [{"kind": "guest", "target": "a1", "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": cut}, {"kind": "drain", "target": None}]
        e = self.host_entry(running=[("x1", 5)], overrides=ov)
        e["status"].update(waiting=[{"kind": "queue", "queue": "jobs/p1f.txt", "root": "/r", "project": "a1", "name": "s1",
                                     "why": "约定归还 已到，不再放行", "held": "deadline"}], todo=[{"project": "a1", "jobs": ["s1", "s2", "s3"], "deps": {}, "held": None}])
        B.check([e], cut + 600, finish_fn=lambda *a, **k: "已还", ovr=self.ovr)
        self.assertTrue(e["borrow"]["returning"])
        self.assertEqual([(x["queue"], x["n"]) for x in e["borrow"]["late"]], [("jobs/p1f.txt", 3)])

    def test_late_do_by_cutoff_source(self):
        """截止是约定时间时提示请用户延长约定；取的是对方租期（提前 1 小时）时延长约定推不迟，不这么提，写明截止从哪来。"""
        self.assertIn("或请用户延长约定", B.late_do("约定"))
        lease = time.time() + 86400
        _, why = B.cutoff({"holder": "BOB", "until": lease + 7200}, lease)
        txt = B.late_do(why)
        self.assertNotIn("请用户延长约定", txt)
        self.assertIn("BOB 的租期", txt)
        self.assertIn("labmon worker add", txt)
        self.assertIn(txt, B._late_text([{"project": "a1", "queue": "jobs/q.txt", "n": 2}], why))
        # 给用户的那份（#142）：不写命令，写看板上能做什么、交给实验会话的
        self.assertIn("「改约定」", B.late_do("约定", user=True))
        say = B.late_do(why, user=True)
        self.assertNotIn("labmon", say)
        self.assertNotIn("改约定", say)
        self.assertIn("BOB 的租期", say)
        self.assertIn("实验会话", say)

    def test_rewake_due_and_back(self):
        """#129：borrow_due 的 key 带要还的时刻：点了跑完就还、撤销、再点是新的一次，6 小时内也叫醒；
        borrow_back 的 key 带这一次借用、不带超出的时刻：对方用量在预留线上下来回（back 一轮为空）不重复叫醒。"""
        from labmon.waits import _new_alerts
        t = time.time()
        self.begin(until=t + 86400, projects="a1")
        ov = [{"kind": "guest", "target": "a1", "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": K.parse_time(self.server()["borrow_until"])}]
        fin = lambda *a, **k: self.fail("还有任务在跑，不该还")      # noqa: E731

        def woke(sf, at, kind, **kw):
            _, wake = B.check([self.host_entry(running=[("x1", 5)], overrides=ov, **kw)], at, finish_fn=fin, ovr=self.ovr)
            return [k.split(":")[0] for k in _new_alerts(self.d / sf, {w["key"]: w["msg"] for w in wake if w["key"].startswith(kind)}, at)]
        back = ["卡 0：对方用 5.0 GiB 显存，预留 2"]
        self.assertEqual(woke("b.json", t + 10, "borrow_back:", back=back), ["borrow_back"])
        self.assertEqual(woke("b.json", t + 70, "borrow_back:"), [])
        self.assertEqual(woke("b.json", t + 130, "borrow_back:", back=back), [])           # 同一次借用里来回：不叫醒
        for k, (act, want) in enumerate((("return", True), ("cancel", False), ("return", True))):
            with mock.patch.object(B.time, "time", return_value=t + 200 + 100 * k):
                B.request_return(SID, ovr=self.ovr) if act == "return" else B.cancel_return(SID, ovr=self.ovr)
            self.assertEqual(woke("d.json", t + 250 + 100 * k, "borrow_due:"), ["borrow_due"] if want else [], act)

    def test_rewake_away_voided_again(self):
        """#129：说好不用又被对方作废一次（用户重新勾了“这段时间不用”）是新的一件：key 带作废的时刻，6 小时内也叫醒。"""
        from labmon.waits import _new_alerts
        t = time.time()
        until = round(t + 86400)
        self.begin(until=until, away=True)
        sf = self.d / "w.json"

        def void(at):
            away = K.parse_time(self.server()["borrow_away"])
            ov = [{"kind": "guest", "target": None, "keep": dict(self.KEEP, away=round(away))},
                  {"kind": "deadline", "target": "borrow", "at": K.parse_time(self.server()["borrow_until"])}]
            e = self.host_entry(overrides=ov)
            e["status"]["guest"]["away"] = {"until": round(away), "end": round(at), "why": "卡 0 上有了对方的 GPU 进程"}
            _, wake = B.check([e], at, ovr=self.ovr)
            return [k.split(":")[0] for k in _new_alerts(sf, {w["key"]: w["msg"] for w in wake if ":away:" in w["key"]}, at)]
        self.assertEqual(void(t), ["borrow_back"])
        B.check([self.host_entry()], t + 3700, ovr=self.ovr)
        _new_alerts(sf, {}, t + 3700)                                                    # 一小时后不再报
        B.begin(SID, until, None, away=True, ovr=self.ovr)                               # 又勾了“这段时间不用”
        self.assertEqual(void(t + 4000), ["borrow_back"])

    def test_check_back_once(self):
        t = time.time()
        self.begin(until=t + 86400)
        ov = [{"kind": "guest", "target": None, "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": K.parse_time(self.server()["borrow_until"])}]
        back = ["卡 0：对方用 5.0 GiB 显存，预留 2"]
        self.calls.clear()
        msgs, wake = B.check([self.host_entry(back=back, overrides=ov)], t, ovr=self.ovr)
        self.assertTrue(any("用得比预留多（卡 0" in m for m in msgs))
        self.assertIn(f"borrow_back:{SID}:{self.ep()}", [w["key"] for w in wake])
        msgs, _ = B.check([self.host_entry(back=back, overrides=ov)], t + 60, ovr=self.ovr)
        self.assertFalse(any("用得比预留多" in m for m in msgs))             # 同一段只提醒一次
        self.assertEqual(self.calls, [])                                      # 预留没变：不重下指令

    def test_check_tight_once(self):
        """快撞上限：提醒一次，说正在跑的照常跑完、要停和对方商量。"""
        t = time.time()
        self.begin(until=t + 86400)
        ov = [{"kind": "guest", "target": None, "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": K.parse_time(self.server()["borrow_until"])}]
        e = lambda: dict(self.host_entry(running=[("x1", 5)], back=["卡 0"], overrides=ov),
                         status=fake_status(running=[running_job("x1", "a1", pid=5)], guest={"back": ["卡 0"], "tight": "卡 0：对方用 30 GiB，这张卡只剩 500 MB"},
                                            overrides=ov))
        msgs, _ = B.check([e()], t, ovr=self.ovr)
        self.assertTrue(any("快撞上限" in m and "x1" in m and "照常跑完" in m and "现在停" in m for m in msgs))
        msgs, _ = B.check([e()], t + 60, ovr=self.ovr)
        self.assertFalse(any("快撞上限" in m for m in msgs))

    def test_check_squeeze_bumps_keep(self):
        """借用中对方被挤到（我们的任务被 OOM 挤掉、内核杀了对方的进程）：提醒一次，这次借用的预留调高，下一轮按新的量重下 guest。"""
        t = time.time()
        self.begin(until=t + 86400)
        ov = [{"kind": "guest", "target": None, "keep": self.KEEP}, {"kind": "deadline", "target": "borrow", "at": K.parse_time(self.server()["borrow_until"])}]
        v = [{"t": round(t), "name": "x1", "project": "a1", "kind": "queue", "why": "容器内存撞上限，被内核杀掉", "how": "squeeze"}]
        msgs, _ = B.check([self.host_entry(overrides=ov, voided=v)], t, ovr=self.ovr)
        self.assertTrue(any("挤到了 BOB" in m and "×1.2" in m for m in msgs))
        self.keeps.clear()
        msgs, _ = B.check([self.host_entry(overrides=ov, voided=v)], t + 60, ovr=self.ovr)
        self.assertFalse(any("挤到了" in m for m in msgs))                     # 同一次只算一回
        self.assertEqual(self.keeps, [{"vram": 2.4, "ram": 4.8, "cpu": 1.2, "hold": False}])

    def test_request_return_now_and_finish(self):
        self.begin()
        msg = B.request_return(SID, True, [{"name": "x", "project": "a1"}, {"name": "y", "project": "a1"}], ovr=self.ovr)
        self.assertIn("按各自的停法停下", msg)
        self.assertIn(("add", "pause", SID, "a1", None), self.calls)
        cmds = []
        probed = []
        out = B.finish(SID, ssh=lambda s, cmd: cmds.append(cmd) or (0, "12\n", ""), ka=lambda s, cfg, by: ("ok", "start 1"), adopt=lambda *a: ("no", "没有保活在跑"),
                       
                       wrote={"since": 1, "b": 3 * 2 ** 30, "n": 2, "top": [["/root/res", 2 * 2 ** 30, 1], ["/work/out", 2 ** 30, 1]]}, kill=[42], probe=probed.append)
        self.assertEqual(probed, [SID])                                       # 开完保活随即探一次，页面马上看到
        self.assertIn("kill -TERM -- -42", cmds[0])
        self.assertNotIn("ka.sh stop", cmds[0])
        self.assertIn("(bash ka.sh stop)", cmds[1])                           # 借来时 GPU 空着、认不出是 worker 开的：停掉再按守护开（原来的做法）
        self.assertIn("rm -rf /tmp/labmon", cmds[0])
        self.assertIn("借用期间本地盘 labmon 目录之外新写了 3.0 GiB（多半是我们的任务写的：/root/res、/work/out），没动：留在 BOB 那边", out)   # 归还不删（#149）
        self.assertIn("可以告诉 BOB 了", out)
        self.assertTrue(self.server()["borrow_returned"])
        g = next(h for h in user_hosts() if h["id"] == SID)
        self.assertTrue(g["returned"])                                        # 还了：和归还的自用主机一样不再连接
        with self.assertRaises(ConfigError):
            B.request_return(SID)
        _, wake = B.check([], time.time(), ovr=self.ovr)
        self.assertEqual([w["key"] for w in wake], [f"borrow_done:{SID}:{self.ep('borrow_returned')}"])
        # #109：同一台还了又借（6 小时内），借到、要还、已还都是新的一次：key 带这一次借用，labmon wait 照样叫醒
        sf = self.d / "w109.json"
        from labmon.waits import _new_alerts
        first = {w["key"]: w["msg"] for w in wake}
        self.assertEqual(_new_alerts(sf, first), list(first))
        self.begin(until=time.time() + 600, projects="a1")
        K.set_fleet_keys({"borrow_since": K.fmt_time(time.time() + 120, year=True)}, "测试：第二次借的时刻与第一次不同分钟", sid=SID)
        ov = [{"kind": "guest", "target": "a1", "keep": self.KEEP}]
        _, wake = B.check([self.host_entry(overrides=ov)], time.time(), finish_fn=lambda *a, **k: "已还", ovr=self.ovr)
        again = {w["key"]: w["msg"] for w in wake}
        self.assertEqual(list(again), [f"borrow:{SID}:{self.ep()}"])
        self.assertEqual(_new_alerts(sf, again), list(again))

    def test_finish_gone(self):
        # 换了容器：只 worker stop、删这台的临时指令；不 kill 旧进程组、不停保活、不清 /tmp/labmon、不开保活、不恢复看门狗
        self.begin()
        cmds, probed = [], []
        out = B.finish(SID, ssh=lambda s, cmd: cmds.append(cmd) or (0, "", ""), ka=lambda *a: self.fail("不该开保活"), kill=[42], probe=probed.append, gone=True)
        self.assertEqual(len(cmds), 1)
        self.assertIn("stop --host", cmds[0])
        self.assertIn("overrides.json", cmds[0])
        for no in ("kill", "ka.sh", "rm -rf /tmp/labmon"):
            self.assertNotIn(no, cmds[0])
        self.assertEqual(probed, [SID])
        self.assertIn("容器换了", out)
        self.assertTrue(self.server()["borrow_returned"])

    def test_stopped_down_auto_returns_and_unblocks_renew(self):
        """#171：借用中服务器被平台强停（link_step 判掉线 + 平台核对显示作业不在运行）→ 按 gone='stopped' 自动归还、
        不 ssh 目标机、经共用 worker_home 的在用主机删共享盘 overrides.json、续租不再被挡，未核对/旧核对/离线/端口错均不误还。"""
        import subprocess
        wh = self.d / "shared_home"
        ovr = wh / "hosts" / SID / "overrides.json"
        ovr.parent.mkdir(parents=True)
        ovr.write_text(json.dumps([{"kind": "guest", "target": "a1", "keep": self.KEEP},
                                   {"kind": "deadline", "target": "borrow", "at": time.time() + 3600}]))
        (self.d / "hosts.toml").write_text(
            f'[[hosts]]\nid = "29"\nssh = "root@198.51.100.29"\nport = 15155\npython = "{sys.executable}"\n'
            f'worker_home = "{wh}"\nshared = ["/root/data1"]\nworker = "auto"\n\n'
            '[fleet]\nkeepalive_cmd = "bash ka.sh"\n\n'
            f'[[fleet.servers]]\nid = "{SID}"\nholder = "BOB"\nssh = "root@198.51.100.27"\nport = 15152\npool = "p"\n'
        )
        t0 = round(time.time())
        self.begin(until=t0 + 86400, projects="a1")
        # 借用中续租被挡，且报错写清掉线后的解开办法
        with self.assertRaises(PF.PlatformError) as cm:
            FO.start("renew", F.conf(), ids=[SID])
        self.assertIn("先在它的「借用」里还掉", str(cm.exception))
        self.assertIn("与平台核对", str(cm.exception))

        # 经 overview.health（health.link_step）连续两轮间隔 >= DOWN_S 连不上 → 判掉线
        e = {"id": SID, "label": "27（借 BOB）", "error": "ssh: Connection refused", "status": None, "worker_up": False}
        O.health([e], {SID: {}}, t0 + 10)
        self.assertEqual(e["health"]["state"], "suspect")
        O.health([e], {SID: {}}, t0 + 10 + 160)
        self.assertEqual(e["health"]["state"], "down")
        down_since = e["health"]["since"]

        # 1) 还没核对平台：不归还
        msgs, _ = B.check([e], t0 + 200, finish_fn=lambda *a, **k: self.fail("没核对平台不该归还"), ovr=self.ovr)
        self.assertEqual(msgs, [])

        # 2) 旧核对（早于借来时刻、或早于 down_since - 300）或不在 stopped（如端口配置不对）：不归还
        F.state_dir().mkdir(parents=True, exist_ok=True)
        for bad_plat in ({"t": t0 - 600, "running": [], "stopped": [SID]},
                         {"t": t0 + 200, "running": [], "stopped": []}):
            save_json(F.state_file("platform.json"), bad_plat)
            B.check([e], t0 + 200, finish_fn=lambda *a, **k: self.fail("无效核对不该归还"), ovr=self.ovr)
        # 核对早于 down_since - SYNC_GRACE_S 也不触发
        e_later = dict(e, health={"state": "down", "fails": 3, "since": t0 + 1000, "why": "连不上"})
        save_json(F.state_file("platform.json"), {"t": t0 + 200, "running": [], "stopped": [SID]})
        B.check([e_later], t0 + 1200, finish_fn=lambda *a, **k: self.fail("过旧核对不该归还"), ovr=self.ovr)

        # 3) 本机离线（lan_off）：health.state == "lan"，不误还
        e_lan = dict(e, lan_off=True, health={"state": "lan", "fails": 0, "since": None, "why": None})
        save_json(F.state_file("platform.json"), {"t": down_since + 10, "running": [], "stopped": [SID]})
        B.check([e_lan], down_since + 20, finish_fn=lambda *a, **k: self.fail("离线不该归还"), ovr=self.ovr)

        # 4) 有效平台核对（作业不在运行）+ link_step 已判掉线 → 自动按 gone="stopped" 归还
        via_calls = []

        def fake_peer_ssh(h, cmd):
            via_calls.append((h["id"], cmd))
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
            return r.returncode, r.stdout + r.stderr

        fin = lambda hid, **k: B.finish(hid, ssh=lambda *a: self.fail("作业已停不该 ssh 目标机"),
                                        probe=lambda *a: self.fail("作业已停不该探目标机"),
                                        clear_ovr=lambda i, **ck: W.clear_overrides(i, ssh_fn=fake_peer_ssh, **ck), **k)
        msgs, wake = B.check([e], down_since + 20, finish_fn=fin, ovr=self.ovr)
        self.assertTrue(self.server().get("borrow_returned"))
        self.assertFalse(ovr.exists())                                         # 已通过共用 worker_home 的 29 删掉 overrides.json
        self.assertEqual([h for h, _ in via_calls], ["29"])
        self.assertTrue(any("已掉线且平台核对显示作业不在运行" in m for m in msgs), msgs)
        self.assertIn(f"borrow_done:{SID}:{self.ep('borrow_returned')}", [w["key"] for w in wake])
        # 归还后续租不再被借用挡住（只差登录）
        with self.assertRaises(PF.PlatformError) as cm:
            FO.start("renew", F.conf(), ids=[SID])
        self.assertIn("验证码", str(cm.exception))

    def test_finish_gone_unreachable_falls_back_to_peer_and_no_peer(self):
        """#171 评论 #1：finish(gone=True) 连不上目标机（255）时不抛错卡住，回退经共用 worker_home 的在用主机删 overrides.json；
        没有任何在线代办主机时也照常记归还解死锁，并在结果里写明暂未删成。"""
        self.begin()
        cleared = []
        out = B.finish(SID, ssh=lambda s, cmd: (255, "", "Connection refused"),
                       probe=lambda *a: self.fail("连不上不该探"),
                       clear_ovr=lambda hid, **k: cleared.append((hid, k.get("skip"))) or (True, "经 29 删掉 overrides.json"),
                       gone=True)
        self.assertEqual(cleared, [(SID, {SID})])
        self.assertIn("容器换了", out)
        self.assertTrue(self.server().get("borrow_returned"))

        # 再借一次，所有共用 worker_home 的主机都连不上：仍完成归还，并在文案中注明暂未删成
        self.begin()
        out2 = B.finish(SID, ssh=lambda *a: self.fail("stopped 不该 ssh"),
                        probe=lambda *a: self.fail("stopped 不该探"),
                        clear_ovr=lambda hid, **k: W.clear_overrides(hid, ssh_fn=lambda h, cmd: (255, "Connection refused"), **k),
                        gone="stopped")
        self.assertTrue(self.server().get("borrow_returned"))
        self.assertIn("共享盘上的临时指令暂未删成", out2)

    def test_request_return_and_finish_offline_hint(self):
        """连不上时（rc == 255）request_return 与正常 finish 均提示先点「与平台核对」自动按已停归还。"""
        self.begin()
        with self.assertRaises(ConfigError) as c1:
            B.request_return(SID, ovr=lambda *a, **k: 255)
        self.assertIn("与平台核对", str(c1.exception))
        with self.assertRaises(ConfigError) as c2:
            B.finish(SID, ssh=lambda s, cmd: (255, "", "Connection refused"))
        self.assertIn("与平台核对", str(c2.exception))

    def test_cross_host_clear_overrides_and_adopt_mine(self):
        """多主机夹具（fixtures.Hosts）：共用 worker_home 的两台主机，借用机连不上时经在用主机删 overrides.json；
        残留 guest（带 target）与 deadline(borrow) 时经 borrow_only=True 仅清借用指令、保留 pause，接成自用主机后新 worker 正常放行。"""
        import subprocess
        H = Hosts(self.d / "multi")
        try:
            (self.d / "hosts.toml").write_text(
                f'[[hosts]]\nid = "h1"\nssh = "root@198.51.100.29"\nport = 15155\npython = "{sys.executable}"\n'
                f'worker_home = "{H.home}"\nshared = ["{self.d}"]\nworker = "auto"\n\n'
                '[fleet]\nkeepalive_cmd = "bash ka.sh"\n\n'
                f'[[fleet.servers]]\nid = "h2"\nholder = "BOB"\nssh = "root@198.51.100.27"\nport = 15152\npool = "p"\n'
                'borrow_since = "2026-11-20 10:00"\n'
            )
            ovr = H.home / "hosts" / "h2" / "overrides.json"
            ovr.parent.mkdir(parents=True, exist_ok=True)
            items = [{"kind": "guest", "target": "a1,b2", "keep": {"vram": 2.0, "ram": 4.0, "cpu": 1.0, "hold": True}},
                     {"kind": "deadline", "target": "borrow", "at": round(H.now() + 60)},
                     {"kind": "pause", "target": "other-proj", "until": round(H.now() + 3600)}]
            ovr.write_text(json.dumps(items, ensure_ascii=False))

            def local_run(h, cmd):
                r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
                return r.returncode, r.stdout + r.stderr

            # 1) 换人接成自用主机场景：borrow_only=True 经 h1 仅清 guest（含 target）与 deadline(borrow)，保留 pause
            ok, msg = W.clear_overrides("h2", home=str(H.home), borrow_only=True, skip={"h2"}, ssh_fn=local_run)
            self.assertTrue(ok, msg)
            self.assertIn("已清 2 条", msg)
            left = json.loads(ovr.read_text())
            self.assertEqual([(o["kind"], o.get("target")) for o in left], [("pause", "other-proj")])

            # 新自用主机 h2 的 worker 读 overrides.json，不再被当借用主机（a1 之外的项目也不被 guest 挡住，也不受借用截止挡住）
            w2 = H.worker("h2")
            w2.ovr = w2.load_overrides()
            self.assertFalse(any(o.get("kind") == "guest" or o.get("target") == "borrow" for o in w2.ovr))
            self.assertIsNone(w2.override_hit({"project": "demo-c", "name": "x", "est_min": 600}))
            self.assertEqual(w2.override_hit({"project": "other-proj", "name": "y", "est_min": 5})[0], "pause")

            # 2) 归还连不上场景：borrow_only=False 经 h1 整份删掉 h2 的 overrides.json
            ok2, msg2 = W.clear_overrides("h2", home=str(H.home), borrow_only=False, skip={"h2"}, ssh_fn=local_run)
            self.assertTrue(ok2, msg2)
            self.assertIn("经 h1", msg2)
            self.assertFalse(ovr.exists())
        finally:
            H.close()

    def test_finish_thaws_watchdogs_after_keepalive(self):
        """还卡：保活开了之后再恢复借来时暂停的看门狗（worker thaw），结果写进归还的说明。"""
        self.begin()
        cmds, order = [], []
        def ssh(s, cmd):
            cmds.append(cmd)
            order.append("thaw" if " thaw " in cmd else "stop")
            return (0, "看门狗：恢复了 PID 9（bash wd.sh：提到保活程序，在循环里）\n" if " thaw " in cmd else "", "")
        out = B.finish(SID, ssh=ssh, ka=lambda s, cfg, by: order.append("ka") or ("ok", "start 1"), probe=lambda sid: None,
                       adopt=lambda *a: order.append("adopt") or ("no", "没有保活在跑"))
        self.assertEqual(order, ["stop", "adopt", "stop", "ka", "thaw"])
        self.assertIn(f"thaw --host {SID}", cmds[-1])
        self.assertIn("看门狗：恢复了 PID 9", out)

    def test_finish_keeps_owner_keepalive(self):
        """借来时 GPU 上就有对方的保活（ka.sh 开的）：还的时候不 ka.sh stop，保活还在就不重开。"""
        self.begin(work=[{"kind": "keepalive", "label": "ka", "names": [], "mb": 500}])
        self.assertTrue(json.loads((self.d / "state" / "overview" / "borrow.json").read_text())[SID]["ka"])
        cmds = []
        out = B.finish(SID, ssh=lambda s, cmd: cmds.append(cmd) or (0, "", ""), ka=lambda s, cfg, by: ("ok", "already 123"), probe=lambda sid: None,
                       adopt=lambda *a: ("no", "没有保活在跑"))
        self.assertFalse(any("ka.sh stop" in c for c in cmds))
        self.assertIn("rm -rf /tmp/labmon", cmds[0])
        self.assertIn("保活还在（借来时就在、是 BOB 的，归还不停它）", out)
        self.assertNotIn("停不了", out)

    def test_finish_says_stuck_keepalive(self):
        """#102：借用期间 worker 要卡时停不了卡上的保活（状态的 keepalive.stuck）：归还的结果照实写，不只说“借来时就是对方的”；
        borrow.check 把状态里的 stuck 传给 finish。"""
        self.begin(work=[{"kind": "keepalive", "label": "ka", "names": [], "mb": 500}])
        stuck = {"t0": time.time() - 3600, "n": 80, "why": "ka.sh stop 退出码 1：别的调用者正持着锁"}
        B.request_return(SID, ovr=self.ovr)
        got = []
        e = self.host_entry()
        e["status"]["keepalive"] = {"ctl": True, "why": None, "stuck": stuck}
        B.check([e], time.time(), finish_fn=lambda hid, **k: got.append(k.get("stuck")) or "已还", ovr=self.ovr)
        self.assertEqual(got, [stuck])
        out = B.finish(SID, ssh=lambda s, cmd: (0, "", ""), ka=lambda s, cfg, by: ("ok", "already 123"), probe=lambda sid: None, stuck=stuck,
                       adopt=lambda *a: ("no", "没有保活在跑"))
        self.assertIn("保活还在（借来时就在、是 BOB 的，归还不停它）", out)
        self.assertIn("借用期间 worker 要卡时停不了它（ka.sh stop 退出码 1：别的调用者正持着锁", out)

    def test_own_keepalive_is_not_owners(self):
        """借来时的保活是 labmon 自己开的（上次归还时守护式开的、盯着让路的那个）：不记成对方的，还的时候照常 ka.sh stop 再按守护开。"""
        self.begin(work=[{"kind": "keepalive", "label": "ka", "names": [], "mb": 500, "ours": True}])
        self.assertFalse(json.loads((self.d / "state" / "overview" / "borrow.json").read_text())[SID]["ka"])
        cmds = []
        B.finish(SID, ssh=lambda s, cmd: cmds.append(cmd) or (0, "", ""), ka=lambda s, cfg, by: ("ok", "start 1"), probe=lambda sid: None,
                 adopt=lambda *a: ("unsure", "ka.sh 的事件日志里没有拉起它的记录"))
        self.assertTrue(any("ka.sh stop" in c for c in cmds))

    def test_finish_adopts_worker_keepalive(self):
        """#102：借来时卡上是对方的保活（borrow.json 的 ka 为真），借用期间 worker 要卡时停过、空闲时又经 ka.sh start 开了一个（状态的 keepalive.mine）。
        归还时不再写“借来时就是对方的”，也不停再开：经 fleet_guard.sh 的 adopt 在现场核对身份后登记成看板的保活（不动进程、没有空档）。
        以前照 borrow.json 写“保活还在（借来时就在、是 BOB 的）”，没人登记它，对方回来用卡时没人让路。"""
        self.begin(work=[{"kind": "keepalive", "label": "ka", "names": [], "mb": 500}])
        mine = {"g": 9100, "born": 4363366, "t": round(time.time()) - 600}
        B.request_return(SID, ovr=self.ovr)
        got = []
        e = self.host_entry()
        e["status"]["keepalive"] = {"ctl": True, "why": None, "stuck": None, "mine": mine}
        B.check([e], time.time(), finish_fn=lambda hid, **k: got.append(k.get("mine")) or "已还", ovr=self.ovr)
        self.assertEqual(got, [mine])                                         # borrow.check 把状态里的 mine 传给 finish
        cmds, seen = [], []
        out = B.finish(SID, ssh=lambda s, cmd: cmds.append(cmd) or (0, "", ""), ka=lambda *a: self.fail("登记上了就不该再开保活"),
                       probe=lambda sid: None, mine=mine, adopt=lambda s, cfg, since, until, m: seen.append((since, until, m)) or ("ok", "adopt 9100"))
        self.assertEqual(seen[0][2], mine)
        self.assertEqual(round(seen[0][0]), round(K.parse_time(self.server()["borrow_since"])))   # 借用期间：借来时刻到归还时刻
        self.assertFalse(any("ka.sh stop" in c for c in cmds))               # 不停、不重开
        self.assertIn("保活是借用期间 worker 开的，已登记成看板的保活（盯着，对方的实验上来就让出）；借来时 BOB 的那个借用期间停过", out)
        self.assertNotIn("借来时就在、是 BOB 的", out)

    def test_finish_adopt_no_gap(self):
        """#102：借来时 GPU 空着，卡上的保活是 worker 开的：登记上就不再 ka.sh stop 再开（原来会空十几秒没有保活）。"""
        self.begin()
        cmds = []
        out = B.finish(SID, ssh=lambda s, cmd: cmds.append(cmd) or (0, "", ""), ka=lambda *a: self.fail("不该再开"), probe=lambda sid: None,
                       adopt=lambda *a: ("ok", "adopt 9100"))
        self.assertFalse(any("ka.sh stop" in c for c in cmds))
        self.assertIn("保活是借用期间 worker 开的，已登记成看板的保活", out)
        self.assertNotIn("借来时", out.split("：", 1)[1])

    def test_finish_adopt_unsure_said(self):
        """#102：借来时就有对方的保活，卡上这个是借用期间起来的、却认不出是 worker 开的：不登记、不停，结果里照实说一句。"""
        self.begin(work=[{"kind": "keepalive", "label": "ka", "names": [], "mb": 500}])
        out = B.finish(SID, ssh=lambda s, cmd: (0, "", ""), ka=lambda s, cfg, by: ("ok", "already 9100"), probe=lambda sid: None,
                       adopt=lambda *a: ("unsure", "ka.sh 的事件日志里没有 labmon-worker:27-15152 拉起它的记录"))
        self.assertIn("卡上的保活是借用期间起来的，ka.sh 的事件日志里没有 labmon-worker:27-15152 拉起它的记录，没登记成看板的保活", out)

    def test_finish_failure_said_once(self):
        """要还期间连不上：“没还成”只说一次，不是每轮都弹；连上还掉了再报结果。"""
        t = time.time()
        self.begin(until=t + 3600)
        B.request_return(SID, ovr=self.ovr)
        n = []
        def fin(hid, wrote=None, kill=(), **k):
            n.append(1)
            if len(n) < 3:
                raise ConfigError("连不上 BOB（27-15152），还没还：timeout")
            return "已还"
        msgs, _ = B.check([self.host_entry()], t, finish_fn=fin, ovr=self.ovr)
        self.assertTrue(any("没还成" in m and "连上了会自动再还" in m for m in msgs))
        msgs, _ = B.check([self.host_entry()], t + 60, finish_fn=fin, ovr=self.ovr)
        self.assertEqual(msgs, [])
        msgs, _ = B.check([self.host_entry()], t + 120, finish_fn=fin, ovr=self.ovr)
        self.assertEqual((msgs, len(n)), (["已还"], 3))

    def test_bad_until_said_once(self):
        """手写的 borrow_until 格式不对：提醒一次（写明格式），当作没约定；不再静默。"""
        from labmon.config import set_fleet_keys
        t = time.time()
        self.begin(until=t + 3600)
        set_fleet_keys({"borrow_until": "明天下午"}, "手改", sid=SID)
        msgs, wake = B.check([self.host_entry()], t, ovr=self.ovr)
        self.assertEqual([m for m in msgs if "认不出" in m and "YYYY-MM-DD HH:MM" in m and "明天下午" in m].__len__(), 1)
        self.assertEqual([w["key"] for w in wake], [f"borrow:{SID}:{self.ep()}"])   # 没约定时间
        msgs, _ = B.check([self.host_entry()], t + 60, ovr=self.ovr)
        self.assertFalse(any("认不出" in m for m in msgs))

    def test_due_wake_says_why(self):
        """borrow_due 的说明照实写要还的原因：用户点的跑完就还不说成到了约定时间。"""
        t = time.time()
        self.begin(until=t + 3600)
        B.request_return(SID, ovr=self.ovr)
        run, fin = [("x1", 5)], lambda *a, **k: "已还"
        _, wake = B.check([self.host_entry(running=run)], t, finish_fn=fin, ovr=self.ovr)
        self.assertIn("要还了（用户点了跑完就还）", wake[0]["msg"])
        self.assertNotIn("约定时间", wake[0]["msg"])
        _, wake = B.check([self.host_entry(running=run)], t + 3601, finish_fn=fin, ovr=self.ovr)
        self.assertIn("到了约定时间", wake[0]["msg"])

    def test_cancel_return(self):
        t = time.time()
        self.begin(until=t + 3600)
        state = lambda: json.loads((self.d / "state" / "overview" / "borrow.json").read_text())[SID]
        B.request_return(SID, ovr=self.ovr)
        self.calls.clear()
        self.assertIn("撤销要还", B.cancel_return(SID, ovr=self.ovr))
        self.assertEqual(self.calls, [("rm", "drain", SID, None, None)])    # 只撤要还时下的 drain，guest 与截止指令不动
        self.assertIsNone(state()["returning"])
        with self.assertRaises(ConfigError):
            B.cancel_return(SID, ovr=self.ovr)                               # 没在要还
        B.request_return(SID, ovr=self.ovr)
        with self.assertRaises(ConfigError) as cm:
            B.cancel_return(SID, ovr=self.ovr, t=t + 3700)                  # 过了约定时间：下一轮又会要还，先改约定
        self.assertIn("先改约定", str(cm.exception))
        # 采集这一轮开始时读到了要还，中途页面上撤销了：合并时不写回
        e = self.host_entry(running=[("x1", 5)])
        log, done = B.log_events, []
        def mid(evs, *a):
            if not done:
                done.append(1)
                B.cancel_return(SID, ovr=self.ovr)
            return log(evs, *a)
        with mock.patch.object(B, "log_events", side_effect=mid):
            B.check([e], t + 60, finish_fn=lambda *a, **k: self.fail("不该还"), ovr=self.ovr)
        self.assertTrue(done)
        self.assertIsNone(state()["returning"])
        self.assertTrue(e["borrow"]["returning"])                             # 这一轮的快照还是要还，下一轮才变
        B.request_return(SID, True, [{"name": "x1", "project": "a1"}], ovr=self.ovr)
        with self.assertRaises(ConfigError):
            B.cancel_return(SID, ovr=self.ovr)                               # 现在停的不撤
        B.check([e], t + 120, finish_fn=lambda *a, **k: "已还", ovr=self.ovr)
        self.assertTrue(e["borrow"]["now"])

    def test_swap_host_only_reminds(self):
        t = time.time()
        B.set_swap("29", t + 600, "CAROL", ovr=self.ovr)
        h = next(x for x in user_hosts() if x["id"] == "29")
        self.assertEqual((h["borrow"]["holder"], h["borrow"]["swap"]), ("CAROL", True))
        cut = h["borrow"]["until"]                                          # epoch 秒
        e = fake_host("29", status={})
        self.calls.clear()
        msgs, wake = B.check([e], cut + 1, finish_fn=lambda *a, **k: self.fail("换给自己的不自动还"), ovr=self.ovr)
        self.assertEqual(self.calls, [("add", "deadline", "29", "borrow", round(cut))])   # 只下约定截止，不下 guest、不 drain
        self.assertTrue(any("换回给 CAROL" in m for m in msgs))
        self.assertEqual([w["key"] for w in wake], [f"borrow_due:29:{round(cut)}"])   # 换给自己的没有起点、没有 returning：取约定时刻
        B.set_swap("29", None, None, ovr=self.ovr)
        self.assertIn(("rm", "deadline", "29", "borrow", None), self.calls)
        self.assertNotIn("borrow", next(x for x in user_hosts() if x["id"] == "29"))

    def test_wait_merges_wake(self):
        (self.d / "m.toml").write_text(f'out_dir = "{self.d}/a1"\nid = "a1"\n')
        cfg = load(self.d / "m.toml")
        out = self.d / "a1"
        out.mkdir()
        save_json(out / "snapshot.json", {"generated": time.time(), "alerts": []})
        ov = self.d / "ov.json"
        save_json(ov, {"wake": [{"key": "borrow:x", "projects": ["a1"], "msg": "借到"}, {"key": "borrow:y", "projects": ["demo-b"], "msg": "别的项目"},
                                {"key": "void:29:j:1", "projects": None, "msg": "作废"},
                                {"key": "borrow_due:x", "projects": None, "host": "x", "msg": "要还"}]})   # 只报给在 x 上有车道的项目
        self.assertEqual(wait_check(cfg, out, self.d / "w.json", ov), {"borrow:x": "借到", "void:29:j:1": "作废"})


class GuestWorkerTest(unittest.TestCase):
    """worker 的 guest 指令、约定归还的准入与独占任务被打扰作废（不起任务进程）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name).resolve()
        self.m = worker_module(self.d)
        self.w = self.m.Worker("t")
        os.makedirs(self.w.p.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, **kw):
        return {"key": "r", "kind": "queue", "name": "u", "project": "a1", "ngpu": 1, "excl": None, **kw}

    def prepped(self, **kw):
        """一次单元申请，经 new_req 拼、prep 补全（place 只认补全过的请求）。"""
        r = self.m.new_req("slot", kw.pop("key", "r"), kw.pop("project", "a1"), kw.pop("name", "u"), id="1", pid=1, t=time.time(), **kw)
        return self.w.prep(r, time.time(), {})

    def place(self, r, gpus, apps=()):
        """真放行：按此刻的快照（gpus 为 query_gpus 形状的各卡空闲，apps 为 GPU 进程）。"""
        return self.w.place(r, self.w.snapshot({}, gpus, apps))

    def free(self, gpus):
        """快照里各卡还能给新任务的显存 {UUID: MB}。"""
        return self.m.card_free(self.w.snapshot({}, gpus))

    def block(self, r):
        """挡住这个请求的临时指令的原因（override_hit 的第二项），没有为 None。"""
        return (self.w.override_hit(r) or (None, None))[1]

    KEEP = {"vram": 4, "ram": 8, "cpu": 2}

    def guest(self, free=20000.0, vram=1024.0, ram=3.0):
        self.w.ovr = [{"kind": "guest", "target": "a1", "keep": self.KEEP}]
        self.w.info = {"gpus": [{"i": 0, "uuid": "GPU-a", "free": free}], "mem": {"limit": 64.0, "used": 10.0, "anon": 6.0}}
        self.w.other = {"vram": {"GPU-a": vram} if vram else {}, "ram": ram, "cpu": 0.5, "ka": {}, "gid": {"GPU-a": {900}} if vram else {}}

    def test_guest_blocks(self):
        """对方轻用、在预留以内照常放（扣掉对方还没用上的预留）；内存超出预留整台不放，回落后 10 分钟内仍不放。"""
        self.guest()
        t = time.time()
        self.w.guest_scan(t)
        self.assertEqual(self.w.back, {})
        self.assertIsNone(self.block(self.req()))
        self.assertIn("只给 a1 用", self.block(self.req(project="demo-b")))
        self.assertEqual(self.free([(0, "GPU-a", 20000.0)]), {"GPU-a": 20000.0 - 3072.0})   # 对方用 1、留 4：扣 3 GiB
        self.assertAlmostEqual(self.w.mem_room(), 64 - 6 - 5)               # 收不回的 6（页缓存不算）；对方用 3、留 8：扣 5 GiB
        self.w.other["ram"] = 9.0
        self.w.guest_scan(t)
        self.assertIn("内存：对方用 9.0 GiB", self.block(self.req()))
        self.w.other["ram"] = 3.0
        self.w.guest_scan(t + 60)
        self.assertIn("分钟前超出预留", self.block(self.req()))
        self.w.guest_scan(t + 700)
        self.assertIsNone(self.block(self.req()))
        self.assertIn("给对方留显存 4 GiB", self.m.ovr_text(self.w.ovr[0]))
        self.w.ovr = [{"kind": "guest", "target": None}]                     # 旧指令没带 keep：对方有进程的卡算超出
        self.w.guest_scan(t + 800)
        self.assertIn("GPU-a", self.w.back)
        self.assertEqual(self.free([(0, "GPU-a", 100.0)]), {"GPU-a": 100.0})

    def test_deadline_still_hits_after_drain(self):
        """约定到点后看板追加 drain：deadline 在列表里排在它前面，override_hit 仍按 deadline 挡（已到，不再放行），看板的 borrow_cut 接着报。"""
        self.guest()
        t = time.time()
        self.w.guest_scan(t)
        self.w.ovr += [{"kind": "deadline", "target": "borrow", "at": t - 60}, {"kind": "drain", "target": None}]
        kind, why = self.w.override_hit(self.req())
        self.assertEqual(kind, "deadline")
        self.assertIn("已到，不再放行", why)

    def test_guest_cpu_room(self):
        """借来的主机上 CPU 扣对方的：max(对方实测, 预留 keep.cpu)；hold 为假只扣实测；对方实测要连着两轮超过 1 核才算。"""
        self.guest()
        self.w.info["cpu_quota"] = 16.0
        self.w.cpu_n = 2
        self.assertAlmostEqual(self.w.cpu_room(), 16 - 2)                   # 对方用 0.5、留 2：扣 2
        self.w.other["cpu"] = 5.0
        self.assertAlmostEqual(self.w.cpu_room(), 16 - 5)                   # 超出预留按实测扣
        self.w.cpu_n = 1
        self.assertAlmostEqual(self.w.cpu_room(), 16 - 2)                   # 一轮尖峰不算，仍扣预留
        self.w.ovr[0]["keep"] = dict(self.KEEP, hold=False)
        self.assertAlmostEqual(self.w.cpu_room(), 16)
        self.w.ovr[0]["keep"] = self.KEEP
        S = self.w.snapshot({}, [(0, "GPU-a", 20000.0)])
        self.assertEqual((S["cpu_cap"], S["cpu_room"]), (16, 14.0))
        hist = {"cpus": 16, "cpu_src": "同名 近 1 次峰值 ×1.1"}                  # 按历史估的超过 配额 − 留的，按整份算，不永远排不上
        self.assertEqual(self.m.cpu_need(hist, S), (14, "同名 近 1 次峰值 ×1.1；估计 16 核，封顶为 14 核（labmon 全停时能给的）"))
        self.assertEqual(self.m.cpu_need(dict(hist, cpu_src="提示值"), S), (16, "提示值"))

    def test_guest_unmanaged_keepalive_blocks_card(self):
        """对方另起的、ka.sh 停不了的保活：在预留以内也算那张卡超出（占着算力，worker 停不了），原因里写明要对方停。"""
        self.guest(vram=600.0)
        self.w.other["ka"] = {"GPU-a": [9300]}
        self.w.guest_scan(time.time())
        self.assertIn("卡 0：对方用 0.6 GiB 显存，其中保活 PID 9300 不归 ka.sh 管、停不了，要停请对方停", self.w.back["GPU-a"])

    def test_guest_idle_borrow_holds_nothing(self):
        """借来时对方空着（hold 为假）：不预先留；对方回来、卡上有了这人的进程，那张卡就不放新任务；内存仍按推荐量判断。"""
        self.guest(vram=0)
        self.w.ovr[0]["keep"] = dict(self.KEEP, hold=False)
        t = time.time()
        self.w.guest_scan(t)
        self.assertEqual(self.w.back, {})
        self.assertEqual(self.free([(0, "GPU-a", 20000.0)]), {"GPU-a": 20000.0})
        self.assertAlmostEqual(self.w.mem_room(), 64 - 6)
        self.w.other["vram"] = {"GPU-a": 512.0}
        self.w.guest_scan(t + 10)
        self.assertIn("借来时对方空着", self.w.back["GPU-a"])
        self.assertIsNone(self.w.guest_block())                              # 内存 3 GiB 没超过 8：整台照常
        self.assertIn("对方有 GPU 进程的卡不放新任务", self.m.ovr_text(self.w.ovr[0]))

    def test_guest_away_holds_nothing(self):
        """对方说好不用（keep.away，#30）：不预先留（卡、内存、CPU），对方内存、CPU 超预留也照常放；此刻占着的照算在可用量外。
        对方有了 GPU 进程：那张卡当轮就不放，连着两轮才算回来了，这次作废，内存、CPU 按普通规则判。"""
        t = time.time()
        self.guest(vram=0, ram=9.0)                                           # 常驻进程占着 9 GiB，超过预留 8
        self.w.ovr[0]["keep"] = dict(self.KEEP, hold=True, away=t + 3600)
        self.w.info["cpu_quota"], self.w.cpu_n = 16.0, 2
        self.w.other["cpu"] = 3.0                                             # CPU 也超过预留 2
        self.w.guest_scan(t)
        self.assertEqual(self.w.back, {})
        self.assertIsNone(self.block(self.req()))
        self.assertEqual(self.free([(0, "GPU-a", 20000.0)]), {"GPU-a": 20000.0})
        self.assertAlmostEqual(self.w.mem_room(), 64 - 6)                   # 收不回的 6 照扣，不另留
        self.assertAlmostEqual(self.w.cpu_room(), 16 - 3)                   # 对方实测照扣，不取 max(实测, 预留)
        self.assertIn("前不用：不给对方留余量", self.m.ovr_text(self.w.ovr[0]))
        self.assertEqual(self.w.away_state(), {"until": t + 3600, "end": None, "why": None})
        self.w.other.update(vram={"GPU-a": 512.0}, gid={"GPU-a": {901}})      # 对方回来了
        self.w.guest_scan(t + 10)
        self.assertIn("GPU-a", self.w.back)                                   # 那张卡当轮就不放
        self.assertIsNotNone(self.w.away())                                   # 一轮还不算回来
        self.w.guest_scan(t + 20)
        self.assertIsNone(self.w.away())
        self.assertEqual(self.w.away_state()["why"], "卡 0 上有了对方的 GPU 进程")
        self.assertEqual(self.free([(0, "GPU-a", 20000.0)]), {"GPU-a": 20000.0 - 3584.0})   # 借来时在轻用：重新给对方留卡（留 4、用 0.5）
        self.assertIn("内存：对方用 9.0 GiB", self.block(self.req()))
        self.assertAlmostEqual(self.w.mem_room(), 64 - 6)                   # 对方已超预留：没有没用上的预留可扣
        self.w.ovr[0]["keep"] = dict(self.KEEP, hold=True)                    # 看板重下不带 away 的指令：作废记录清掉
        self.assertIsNone(self.w.away_state())
        self.assertIsNone(self.w.away_end)

    def test_guest_away_expires_and_oom_ends(self):
        """说好不用到点就按普通规则；期间内核杀了 labmon 之外的进程也算对方回来了。"""
        t = time.time()
        self.guest(vram=0, ram=9.0)
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t - 1)
        self.w.guest_scan(t)
        self.assertIn("内存：对方用 9.0 GiB", self.block(self.req()))
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t + 3600)
        self.w.guest_scan(t)
        self.assertIsNone(self.block(self.req()))
        self.w.oom_n = 4
        self.w.info["mem"]["oom_kill"] = 5
        self.w.oom_check(t)
        self.assertEqual(self.w.away_state()["why"], "内核杀了 labmon 之外的进程")
        self.w.guest_scan(t + 10)
        self.assertIn("内存：对方用 9.0 GiB", self.block(self.req()))

    def test_guest_away_existing_gpu_proc_and_flicker(self):
        """说好不用开始时对方卡上已有的小 GPU 进程（轻用时开着的 Jupyter）不算回来、不挡那张卡，照扣在可用量外；
        新出现的对方 GPU 进程只出现一轮（归属误判、任务刚结束 nvidia-smi 还列着）不作废，连着两轮才作废（审查非阻塞 3、4）。"""
        t = time.time()
        self.guest(vram=300.0)
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t + 3600)
        for dt in (0, 10, 20):
            self.w.guest_scan(t + dt)
        self.assertIsNotNone(self.w.away())
        self.assertEqual(self.w.back, {})
        self.assertEqual(self.free([(0, "GPU-a", 20000.0)]), {"GPU-a": 20000.0})   # 快照的 free 已扣对方的 300 MB，不另留
        self.w.other["gid"] = {"GPU-a": {900, 905}}
        self.w.guest_scan(t + 30)
        self.assertIn("GPU-a", self.w.back)
        self.w.other["gid"] = {"GPU-a": {900}}                                 # 下一轮没了：不算回来，计数从头
        self.w.guest_scan(t + 40)
        self.assertIsNotNone(self.w.away())
        self.assertEqual(self.w.back, {})
        self.w.other["gid"] = {"GPU-a": {900, 906}}
        self.w.guest_scan(t + 50)
        self.w.guest_scan(t + 60)
        self.assertEqual(self.w.away_state()["why"], "卡 0 上有了对方的 GPU 进程")

    def test_guest_away_unmanaged_keepalive_not_back(self):
        """说好不用期间对方新起 ka.sh 停不了的保活（如对方的看门狗拉起了脚本不同的保活）：不算对方回来了、不作废这次说好不用，
        只是那张卡不放（维护者 11-09 定，#37 审查非阻塞 4：保活只占位、不算对方干活）。同一张卡上再有别的新进程照样算回来。"""
        t = time.time()
        self.guest(vram=0)
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t + 3600)
        self.w.guest_scan(t)
        self.w.other.update(vram={"GPU-a": 6748.0}, gid={"GPU-a": {9300}}, ka={"GPU-a": [9300]})
        for dt in (10, 20, 30):
            self.w.guest_scan(t + dt)
        self.assertIsNotNone(self.w.away())
        self.assertIsNone(self.w.away_state()["end"])
        self.assertIn("保活 PID 9300 不归 ka.sh 管、停不了，要停请对方停", self.w.back["GPU-a"])
        self.w.other["gid"] = {"GPU-a": {9300, 901}}                          # 对方真回来跑东西
        self.w.guest_scan(t + 40)
        self.w.guest_scan(t + 50)
        self.assertEqual(self.w.away_state()["why"], "卡 0 上有了对方的 GPU 进程")

    def test_guest_away_lifts_card_block_at_once(self):
        """勾上前那张卡已超出（轻用时对方超出显存预留；借来时空着、对方后来开了个小进程）：勾上说好不用同一轮就放这张卡，
        不等 back_last 的 10 分钟（复核阻塞 A）。"""
        t = time.time()
        for hold, vram in ((True, 6144.0), (False, 300.0)):
            self.guest(vram=vram)
            self.w.ovr[0]["keep"] = dict(self.KEEP, hold=hold)
            self.w.back, self.w.back_last, self.w.away_base, self.w.away_end = {}, {}, None, None
            self.w.guest_scan(t)
            self.assertIn("GPU-a", self.w.back)
            self.assertIn("对方在用卡 0", self.place(self.prepped(vram=2.0, ram=1.0), [(0, "GPU-a", 20000.0)])[0])
            self.w.ovr[0]["keep"] = dict(self.KEEP, hold=hold, away=t + 3600)
            self.w.guest_scan(t + 10)
            self.assertEqual((self.w.back, self.w.back_last), ({}, {}))
            self.assertEqual(self.w.snapshot({}, [(0, "GPU-a", 20000.0)])["owner"], set(), hold)   # place 不再因这张卡挡

    def test_guest_away_recheck_same_time(self):
        """作废以后用同一个约定时间重新勾（改约定弹窗缺省就是原约定时间）：重新取基线、计数从 0 起，第一轮不再作废（复核阻塞 B）。"""
        t = time.time()
        self.guest(vram=300.0)
        away = dict(self.KEEP, away=t + 3600)
        self.w.ovr[0]["keep"] = away
        self.w.guest_scan(t)
        self.w.other["gid"] = {"GPU-a": {900, 905}}
        self.w.guest_scan(t + 10)
        self.w.guest_scan(t + 20)
        self.assertEqual(self.w.away_state()["why"], "卡 0 上有了对方的 GPU 进程")
        self.w.ovr[0]["keep"] = dict(self.KEEP)                               # 看板清掉、重下不带 away 的指令
        self.w.guest_scan(t + 30)
        self.w.guest_scan(t + 40)
        self.w.ovr[0]["keep"] = dict(away)                                    # 同一个约定时间重新勾
        ev = []
        self.w.event = lambda kind, msg, **kw: ev.append(msg)
        for dt in (50, 60, 70):
            self.w.guest_scan(t + dt)
        self.assertIsNotNone(self.w.away())
        self.assertFalse(any("现在回来了" in m for m in ev), ev)

    def test_guest_away_base_waits_for_gpu_table(self):
        """说好不用的第一轮没读到 GPU 表（nvidia-smi 失败）：不取基线，等读到了再取，对方原本就有的进程不被当成新的（复核非阻塞 4）。"""
        t = time.time()
        self.guest(vram=300.0)
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t + 3600)
        gpus = self.w.info["gpus"]
        self.w.info["gpus"], self.w.other["gid"] = [], {}
        self.w.guest_scan(t)
        self.w.info["gpus"], self.w.other["gid"] = gpus, {"GPU-a": {900}}
        for dt in (10, 20, 30):
            self.w.guest_scan(t + dt)
        self.assertIsNotNone(self.w.away())

    def test_guest_away_cpu_hot_keeps_counting(self):
        """说好不用期间对方 CPU 超预留照样累计 cpu_hot（只是不判超出）：作废那一轮已连着两轮超出的，立刻算 CPU 超出、整台不放。"""
        t = time.time()
        self.guest(vram=0)
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t + 3600)
        self.w.other["cpu"] = 3.0                                             # 超过预留 2
        for dt in (0, 10):
            self.w.guest_scan(t + dt)
        self.assertEqual(self.w.cpu_hot, 2)
        self.assertIsNone(self.w.guest_block())
        self.w.oom_n, self.w.info["mem"]["oom_kill"] = 4, 5                    # 内核杀了对方的进程：作废
        self.w.oom_check(t + 15)
        self.w.guest_scan(t + 20)
        self.assertIn("CPU：对方用 3.0 核，预留 2", self.w.guest_block())

    def test_guest_away_lifts_block_at_once(self):
        """对方内存超出预留、整台不放时勾上说好不用：这一轮起就放行，不等 back_last 的 GUEST_QUIET_S 过期（审查阻塞 2）；
        也不记“对方回到预留以内，10 分钟后恢复放行”。"""
        t = time.time()
        self.guest(vram=0, ram=9.0)
        self.w.guest_scan(t)
        self.assertIn("内存：对方用 9.0 GiB", self.block(self.req()))
        ev = []
        self.w.event = lambda kind, msg, **kw: ev.append(msg)
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t + 3600)
        for dt in (30, 590):
            self.w.guest_scan(t + dt)
            self.assertIsNone(self.w.guest_block())
            self.assertIsNone(self.block(self.req()))
        self.assertFalse(any("10 分钟后恢复放行" in m for m in ev), ev)

    def test_guest_away_own_oom_is_not_owner_back(self):
        """说好不用期间内核杀的是我们自己的进程（DataLoader 子进程被杀、任务以退出码 1 退出，squeezed_by 不认）：
        上一轮还有我们的任务进程（oom_score_adj 1000，内核先杀我们的），就不算对方回来了，也不记挤到对方（审查阻塞 1）。"""
        t = time.time()
        self.guest(vram=0)
        self.w.ovr[0]["keep"] = dict(self.KEEP, away=t + 3600)
        self.w.running = {"k": {"name": "x1", "kind": "queue", "pid": 11, "gpus": ["GPU-a"], "t0": t - 600, "project": "a1"}}
        self.w.mine = {11, 12}
        self.w.oom_n = 4
        self.w.info["mem"]["oom_kill"] = 5
        self.w.oom_check(t)
        self.assertEqual(self.w.away_state(), {"until": t + 3600, "end": None, "why": None})
        self.assertIsNone(self.w.hit)
        self.w.running, self.w.mine = {}, set()                               # 我们没有进程在跑时撞上限，杀的才是对方的
        self.w.info["mem"]["oom_kill"] = 6
        self.w.oom_check(t + 10)
        self.assertEqual(self.w.away_state()["why"], "内核杀了 labmon 之外的进程")
        self.assertEqual(self.w.hit, round(t + 10))

    def test_guest_card_over_then_tight(self):
        """对方超出预留的卡不放新任务；这张卡快满时只记快撞上限，正在跑的不停（这一格跑完）。"""
        self.guest(free=800.0, vram=6144.0)
        t = time.time()
        self.w.guest_scan(t)
        self.assertIn("卡 0：对方用 6.0 GiB 显存，预留 4", self.w.back["GPU-a"])
        res = self.place(self.prepped(vram=2.0, ram=1.0), [(0, "GPU-a", 20000.0)])
        self.assertIn("对方在用卡 0", res[0])
        sent = []
        self.w.send_stop = lambda j, mode: sent.append((j["name"], mode))
        self.w.running = {"o": {"name": "old", "kind": "queue", "pid": 11, "gpus": ["GPU-a"], "t0": t - 600, "stop": "term", "project": "a1"}}
        self.w.guest_tight(t)
        self.assertIn("这张卡只剩 800 MB", self.w.tight)
        self.assertEqual((sent, self.w.voided), ([], []))
        self.w.info["gpus"][0]["free"] = 5000.0
        self.w.guest_tight(t + 10)
        self.assertIsNone(self.w.tight)
        self.w.other["ram"] = 20.0                                          # 对方内存也超出预留
        self.w.guest_scan(t + 20)
        self.w.info["mem"].update(used=63.0, anon=30.0)                      # 工作集满的是页缓存：不算快撞上限
        self.w.guest_tight(t + 20)
        self.assertIsNone(self.w.tight)
        self.w.info["mem"]["anon"] = 62.0
        self.w.guest_tight(t + 30)
        self.assertIn("容器内存（不算页缓存）62 / 64 GiB", self.w.tight)

    def test_squeezed_requeue(self):
        """借来的主机上被对方挤掉（CUDA OOM 且对方在同卡、或期间容器 OOM 杀过进程）：不记失败、重新排队，同一任务最多两次。"""
        self.guest()
        root = self.d / "p"
        q = {"key": "k", "root": str(root), "jobs": "q.txt", "state": str(root / "st"), "logs": str(root / "logs"), "project": "a1"}
        os.makedirs(q["state"])
        os.makedirs(q["logs"])
        Path(q["logs"], "x1.log").write_text("step 3\ntorch.OutOfMemoryError: CUDA out of memory.\n")
        for n in range(3):
            Path(q["state"], "x1.rc").write_text("1")
            self.w.running["k"] = {"key": "k", "name": "x1", "kind": "queue", "pid": 5, "gpus": ["GPU-a"], "t0": time.time(), "q": q,
                                   "project": "a1", "owner_gpu": True, "use": {}}
            self.w.finish("k")
        self.assertEqual([v["how"] for v in self.w.voided], ["squeeze", "squeeze"])
        self.assertTrue(Path(q["state"], "x1.failed").exists())             # 第三次记失败
        log = Path(q["state"], "queue.log").read_text()
        self.assertIn("被挤掉 x1（对方在同一张卡上", log)
        self.assertIn("不再重排", log)

    def test_owner_oom(self):
        """容器 oom_kill 计数涨了、又不是我们的任务被杀：记下内核杀了对方的进程。"""
        self.guest()
        self.w.oom_n = 4
        self.w.info["mem"]["oom_kill"] = 5
        self.w.oom_check(time.time())
        self.assertTrue(self.w.hit)
        self.w.info["mem"]["oom_kill"], self.w.oom_ours, self.w.hit = 6, 1, None   # 是我们的任务被杀：不算
        self.w.oom_check(time.time())
        self.assertIsNone(self.w.hit)

    def test_borrow_deadline(self):
        self.w.ovr = [{"kind": "deadline", "target": "borrow", "at": time.time() + 3600}]
        self.assertIsNone(self.block(self.req()))                 # 没估时：到点前照常放
        self.w.durs = {"r": {"u": self.m.with_cut({"min": 60, "src": "清单估时"})}}   # 清单的预计时长（queue_durs 算的）
        self.assertIn("约定归还", self.block(self.req()))
        self.w.ovr[0]["at"] = time.time() - 1
        self.assertIn("已到", self.block(self.req()))
        self.assertIn("约定归还", self.m.ovr_text(self.w.ovr[0]))

    def test_add_refuses_other_project(self):
        root = self.d / "demo-b"
        (root / "jobs").mkdir(parents=True)
        (root / "jobs" / "q.txt").write_text("a|true\n")
        Path(self.w.p.overrides).write_text(json.dumps([{"kind": "guest", "target": "a1"}]))
        self.assertEqual(self.m.main(["add", str(root), "jobs/q.txt", "--host", "t", "--project", "demo-b"]), 2)
        self.assertEqual(self.m.main(["add", str(root), "jobs/q.txt", "--host", "t", "--project", "a1"]), 0)

    def test_void_after_two_ticks(self):
        voided = []
        self.w.void_job = lambda j, why: voided.append((j["name"], why))
        ex = {"name": "timing", "excl": "gpu", "gpus": ["GPU-a"], "use": {"cores": 1.0}}
        plain = {"name": "train", "excl": None, "gpus": ["GPU-a"], "use": {"cores": 1.0}}
        self.w.running = {"e": ex, "p": plain}
        near, gp = {id(ex): {"GPU-a"}, id(plain): {"GPU-a"}}, [self.proc("GPU-a", 7)]
        self.w.void_check(list(self.w.running.values()), near, gp, None)
        self.assertEqual(voided, [])                                          # 一轮不算
        self.w.void_check(list(self.w.running.values()), near, gp, None)
        self.assertEqual(len(voided), 1)
        self.assertEqual(voided[0][0], "timing")
        self.assertIn("train、PID 7（python）", voided[0][1])
        ex["void_n"] = 0
        self.w.void_check([ex], {id(ex): {"GPU-a"}}, [], None)               # 打扰没了：从头计
        self.assertEqual(ex["void_n"], 0)
        ex.update(excl="host", gpus=[])
        for _ in range(2):
            self.w.void_check([ex], {id(ex): set()}, [], 1.5)                 # 整机独占：labmon 之外占了 1 核以上也算
        self.assertIn("labmon 之外的进程占 1.5 核", voided[-1][1])

    @staticmethod
    def proc(uuid, hpid=999, comm="python"):
        return {"hpid": hpid, "pid": None, "mb": 1024.0, "uuid": uuid, "comm": comm, "owner": "other", "ka": False, "job": None, "_j": None}

    def excl_req(self, excl="gpu", vram=10.0, **kw):
        return self.prepped(excl=excl, vram=vram, ram=1.0, **kw)

    def lend(self, gpus=(("GPU-a", 0.0),), cpu=0.1):
        """借来的主机：对方轻用（hold），给对方留显存 2 GiB；gpus 为各卡 (UUID, 对方占的显存 MB)，卡 48 GiB。"""
        self.w.ovr = [{"kind": "guest", "target": "a1", "keep": {"vram": 2.0, "ram": 8.0, "cpu": 2.0, "hold": True}}]
        self.w.info = {"gpus": [{"i": i, "uuid": u, "free": 49152.0 - mb, "total": 49152.0} for i, (u, mb) in enumerate(gpus)],
                       "mem": {"limit": 64.0, "used": 10.0, "anon": 6.0}}
        self.w.other = {"vram": {u: mb for u, mb in gpus if mb}, "ram": 3.0, "cpu": cpu, "ka": {}}
        self.w.guest_scan(time.time())
        self.launched = []
        self.w.launch = lambda r, g: self.launched.append((r["name"], [c["i"] for c in g]))
        return [(i, u, 49152.0 - mb) for i, (u, mb) in enumerate(gpus)]

    def test_exclusive_gpu_keeps_owner_reserve(self):
        """独占卡也扣给对方的预留：扣完放不下就不放（以前只看卡上有没有进程，对方回来就没显存）；多卡先挑没给对方留的卡。"""
        gpus = self.lend()
        why, stuck = self.place(self.excl_req(vram=46.0), gpus)
        self.assertIn("没有放得下的卡（要显存 46 GiB，另留 1 GiB，已扣给对方的预留）", why)
        self.assertFalse(stuck)
        self.assertIsNone(self.place(self.excl_req(vram=40.0), gpus))
        gpus = self.lend((("GPU-a", 0.0), ("GPU-b", 0.0)))
        self.assertIsNone(self.place(self.excl_req(), gpus))
        self.assertEqual(self.launched, [("u", [1])])                        # 0 号卡是给对方留的

    def test_exclusive_waits_for_owner_without_holding(self):
        """对方在卡上有进程（预留以内）：独占任务等对方用完，但不留卡、不算卡在资源上，别的任务照常放（以前整台都堵住）。"""
        gpus = self.lend((("GPU-a", 1024.0),))
        apps = [self.proc("GPU-a")]
        why, stuck = self.place(self.excl_req(), gpus, apps)
        self.assertIn("独占：对方在用卡（PID 999（python）），等用完", why)
        self.assertFalse(stuck)
        self.assertEqual(self.w.excl_hold, {})
        self.assertIsNone(self.place(self.prepped(key="n", vram=10.0, ram=1.0), gpus, apps))
        self.w.running = {"j": {"key": "j", "name": "mine", "kind": "queue", "gpus": ["GPU-a"], "use": {}, "t0": time.time()}}
        why, _ = self.place(self.excl_req(), gpus, apps)                     # 我们的任务也在：对方不走，留卡也没用
        self.assertIn("对方在用卡", why)
        self.assertEqual(self.w.excl_hold, {})
        why, stuck = self.place(self.excl_req(), gpus)                       # 只剩我们的任务：留卡等它跑完
        self.assertIn("独占：等卡 0 腾空（还在跑：mine）", why)
        self.assertTrue(stuck)
        self.assertEqual(self.w.excl_hold["r"]["cards"], ["GPU-a"])

    def test_exclusive_host_waits_for_owner_cpu(self):
        """整机独占放行前也看 labmon 之外的 CPU（与作废同一个判定）：连着两轮超过 1 核就等，不留主机；以前照样放行、20 秒后作废，反复空转。"""
        self.lend(cpu=2.0)
        self.w.cpu_n = 1                                                     # 一轮的尖峰（如刚结束的任务）不算
        self.assertIsNone(self.place(self.excl_req("host", ngpu=0), []))
        self.w.cpu_n = 2
        why, stuck = self.place(self.excl_req("host", ngpu=0), [])
        self.assertIn("独占整机：对方在用（labmon 之外的进程占 2.0 核），等用完", why)
        self.assertFalse(stuck)
        self.assertEqual(self.w.excl_hold, {})
        why, _ = self.place(self.excl_req("host", ngpu=0), [], [self.proc("GPU-a")])
        self.assertIn("PID 999（python）", why)


if __name__ == "__main__":
    unittest.main()
