"""看板服务所在机的体检（labmon/hostcheck.py，#166）：powershell.exe 与 /proc、systemctl 一律打桩，不在本机或 CI 上真调。

各阈值的边界、只在白天发与一天一次、计划维护期间不推、更新搜索每 6 小时一次与计划任务 JSON 的退路、不是 WSL 时整块不启用。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import fixtures  # noqa: F401  测试环境（不弹通知、LABMON_NO_HOSTCHECK）

from labmon import hostcheck as HC
from labmon.records import EVENT_KINDS, records

DAY_S = 86400


def at(day: int, hh: int, mm: int = 0, ss: int = 0) -> float:
    """2026-11-<day> hh:mm:ss 本地时刻（白天 9–22 点的判定按本地时间）。"""
    return time.mktime((2026, 11, day, hh, mm, ss, 0, 0, -1))


T0 = at(10, 12)


def win(**kw) -> dict:
    d = {"name": "DASHBOARD-HOST", "boot": T0 - 5 * DAY_S, "free_mb": 20000, "total_mb": 32000, "reboot_wu": False, "reboot_cbs": False,
         "pause_until": "", "au_options": 3, "no_auto": None}
    d.update(kw)
    return d


def wsl(**kw) -> dict:
    d = {"avail_mb": 3000.0, "total_mb": 4000.0, "oom_kill": 0, "rss_mb": 80.0, "tunnel": "active", "tunnel_err": None, "err": None}
    d.update(kw)
    return d


def upd(*items) -> dict:
    return {"updates": list(items)}


def item(uid: str, sec: bool = False, title: str | None = None) -> dict:
    return {"id": uid, "title": title or f"更新 {uid}", "kb": "5050505", "sec": sec, "driver": False}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"LABMON_STATE": str(d / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LABMON_NO_HOSTCHECK", None)
        interop = d / "WSLInterop-late"
        interop.write_text("enabled\n")
        self.patch("INTEROP", (d / "WSLInterop", interop))
        self.patch("WU_TASK", d / "ProgramData" / "labmon" / "wu.json")
        self.win, self.wsl, self.upd = win(), wsl(), upd()
        self.ps_calls = []

        def fake_ps(script, timeout=60):
            self.ps_calls.append(script)
            if "Microsoft.Update.Session" in script:
                return self.upd
            return self.win
        self.patch("run_ps", fake_ps)
        self.patch("read_wsl", lambda: dict(self.wsl))

    def patch(self, name, value):
        p = mock.patch.object(HC, name, value)
        p.start()
        self.addCleanup(p.stop)

    def tick(self, t, **kw):
        return HC.tick(t, **kw)

    def warn(self, t, **kw):
        """跑一轮（push=False：不推、不记已发），返回这一轮成立的提醒 key。"""
        HC.tick(t, push=False, **kw)
        return set(HC.view(t)["warn"])


class EnableTest(Base):
    def test_not_wsl_does_nothing(self):
        """没有 WSL interop（笔记本、普通 Linux、CI）：不启用，不调 powershell.exe，快照里没有这张卡。"""
        d = Path(self.tmp.name)
        self.patch("INTEROP", (d / "nope1", d / "nope2"))
        self.patch("run_ps", mock.Mock(side_effect=AssertionError("不该调 powershell.exe")))
        self.patch("read_wsl", mock.Mock(side_effect=AssertionError("不该读 WSL")))
        self.assertFalse(HC.enabled())
        self.assertEqual(HC.tick(T0), [])
        self.assertIsNone(HC.view(T0))
        self.assertFalse((HC._file()).exists())

    def test_wsl_interop_either_name(self):
        d = Path(self.tmp.name)
        self.assertTrue(HC.enabled())                        # 只有 WSLInterop-late（开了 systemd 的新版 WSL）
        self.patch("INTEROP", (d / "WSLInterop-late", d / "nope"))
        self.assertTrue(HC.enabled())

    def test_env_switch_off(self):
        """测试夹具设的 LABMON_NO_HOSTCHECK：在 WSL 里跑测试也不真调。"""
        with mock.patch.dict(os.environ, {"LABMON_NO_HOSTCHECK": "1"}):
            self.assertFalse(HC.enabled())
            self.assertIsNone(HC.view(T0))

    def test_fixtures_turn_it_off(self):
        self.assertIn('os.environ["LABMON_NO_HOSTCHECK"] = "1"', Path(fixtures.__file__).read_text(encoding="utf-8"))


class ThresholdTest(Base):
    def test_uptime_over_30_days(self):
        self.win = win(boot=T0 - 30 * DAY_S)
        self.assertNotIn("uptime", self.warn(T0))
        self.win = win(boot=T0 - 30 * DAY_S - 60)
        self.assertIn("uptime", self.warn(T0))

    def test_reboot_pending_over_a_day(self):
        self.win = win(reboot_cbs=True)
        self.assertNotIn("reboot", self.warn(T0))
        self.assertNotIn("reboot", self.warn(T0 + DAY_S))                  # 从第一次看到起算，刚好 1 天不算
        self.assertIn("reboot", self.warn(T0 + DAY_S + 120))
        self.win = win(reboot_cbs=False)
        self.assertNotIn("reboot", self.warn(T0 + DAY_S + 240))            # 重启过了：清掉
        self.win = win(reboot_wu=True)
        self.assertNotIn("reboot", self.warn(T0 + DAY_S + 360))            # 再出现从头算
        self.assertEqual(HC.view(T0 + DAY_S + 360)["reboot"], T0 + DAY_S + 360)

    def test_security_update_three_days(self):
        self.upd = upd(item("s1", sec=True))
        self.assertNotIn("updates", self.warn(T0))
        self.assertNotIn("updates", self.warn(T0 + 3 * DAY_S - 60))
        self.assertIn("updates", self.warn(T0 + 3 * DAY_S))

    def test_any_update_fourteen_days(self):
        self.upd = upd(item("d1"))
        self.assertNotIn("updates", self.warn(T0))
        self.assertNotIn("updates", self.warn(T0 + 14 * DAY_S - 60))
        self.assertIn("updates", self.warn(T0 + 14 * DAY_S))
        msg = HC.view(T0 + 14 * DAY_S)["warn"]["updates"]
        self.assertIn("DASHBOARD-HOST", msg)
        self.assertIn("1 个", msg)

    def test_update_clock_per_update(self):
        """新出现的更新从它自己第一次被看到起算；装掉的不再算。"""
        self.upd = upd(item("old", sec=True))
        self.warn(T0)
        self.upd = upd(item("new", sec=True))
        self.assertNotIn("updates", self.warn(T0 + 3 * DAY_S, force_updates=True))   # old 装了，new 才刚看到

    def test_pause_expiring_within_three_days(self):
        """暂停到期由 PowerShell 换成 epoch 秒送来（Python 侧不解析时刻文字，见 AGENTS.md 唯一实现表的“时刻的文字与解析”）。"""
        self.win = win(pause_until=T0 + 3 * DAY_S + 60)
        self.assertNotIn("pause", self.warn(T0))
        self.win = win(pause_until=T0 + 3 * DAY_S)
        self.assertIn("pause", self.warn(T0))
        self.assertEqual(HC.view(T0)["pause_until"], T0 + 3 * DAY_S)
        self.win = win(pause_until=T0 - 60)
        self.assertNotIn("pause", self.warn(T0))                           # 已经过了：不再是“快到期”
        self.win = win(pause_until="2026-11-12T01:00:00Z")                 # 不是数就当没暂停
        self.assertNotIn("pause", self.warn(T0))
        self.assertIsNone(HC.view(T0)["pause_until"])
        self.assertIn("ToUnixTimeSeconds", HC.WIN_PS.split("pause_until", 1)[0])

    def test_windows_memory_low_ten_minutes(self):
        self.win = win(free_mb=2047)
        self.assertNotIn("win_mem", self.warn(T0))
        self.assertNotIn("win_mem", self.warn(T0 + 599))
        self.assertIn("win_mem", self.warn(T0 + 600))
        self.win = win(free_mb=2048)
        self.assertNotIn("win_mem", self.warn(T0 + 720))                   # 回到 2 GiB：清掉
        self.win = win(free_mb=100)
        self.assertNotIn("win_mem", self.warn(T0 + 840))                   # 再低从头算

    def test_wsl_memory_low_ten_minutes(self):
        self.wsl = wsl(avail_mb=399.0, total_mb=4000.0)
        self.assertNotIn("wsl_mem", self.warn(T0))
        self.assertIn("wsl_mem", self.warn(T0 + 600))
        self.wsl = wsl(avail_mb=400.0, total_mb=4000.0)
        self.assertNotIn("wsl_mem", self.warn(T0 + 720))

    def test_oom_kill_within_a_day(self):
        self.wsl = wsl(oom_kill=3)
        self.assertNotIn("oom", self.warn(T0))                             # 第一次读到的计数是基线
        self.wsl = wsl(oom_kill=5)
        self.assertIn("oom", self.warn(T0 + 120))
        self.assertEqual(HC.view(T0 + 120)["oom"], {"n": 2, "last": T0 + 120})
        self.assertIn("oom", self.warn(T0 + 120 + DAY_S - 1))
        self.assertNotIn("oom", self.warn(T0 + 120 + DAY_S))
        self.wsl = wsl(oom_kill=0)                                         # WSL 重启过、计数归零：不算新的
        self.assertNotIn("oom", self.warn(T0 + 2 * DAY_S))

    def test_serve_rss_over_1g(self):
        self.wsl = wsl(rss_mb=1024.0)
        self.assertNotIn("rss", self.warn(T0))
        self.wsl = wsl(rss_mb=1025.0)
        self.assertIn("rss", self.warn(T0))

    def test_cli_keeps_serve_rss(self):
        """命令行（serve=False）读到的 RSS 是它自己的：不覆盖看板服务上一轮量的，也不清掉服务的 rss 提醒。"""
        self.wsl = wsl(rss_mb=1500.0)
        self.warn(T0)
        self.wsl = wsl(rss_mb=40.0)
        self.assertIn("rss", self.warn(T0 + 60, serve=False))
        self.assertEqual(HC.view(T0 + 60)["rss"], 1500.0)

    def test_tunnel_down_fifteen_minutes(self):
        self.wsl = wsl(tunnel="down")
        self.assertNotIn("tunnel", self.warn(T0))
        self.assertNotIn("tunnel", self.warn(T0 + 900))
        self.assertIn("tunnel", self.warn(T0 + 901))
        self.wsl = wsl(tunnel="active")
        self.assertNotIn("tunnel", self.warn(T0 + 1000))

    def test_tunnel_not_installed_never_warns(self):
        """隧道单元还没装（U8 #165 之前）：算“没装”，不报。"""
        self.wsl = wsl(tunnel="none")
        self.warn(T0)
        self.assertNotIn("tunnel", self.warn(T0 + 10 * DAY_S))
        self.assertEqual(HC.view(T0)["tunnel"]["state"], "none")

    def test_windows_unreadable_over_a_day(self):
        self.win = {"error": "拒绝访问"}
        self.assertNotIn("win_err", self.warn(T0))
        self.assertNotIn("win_err", self.warn(T0 + DAY_S))
        self.assertIn("win_err", self.warn(T0 + DAY_S + 1))
        self.assertIn("拒绝访问", HC.view(T0 + DAY_S + 1)["warn"]["win_err"])
        self.win = win()
        self.assertNotIn("win_err", self.warn(T0 + DAY_S + 120))

    def test_wmi_denied_has_fallback(self):
        # #193：服务账号经 ssh（网络登录）跑 labmon hostcheck 时 Get-CimInstance 拒绝访问；开机与内存要有不经 WMI 的退路，不整份作废
        cim, rest = HC.WIN_PS.split("Get-CimInstance", 1)
        after = rest.split("} catch {", 1)[1]
        self.assertEqual(cim.count("try {"), 2)                             # CIM 自己一个 try，在外层 try 里
        fallback = after.split("$ux =", 1)[0]
        for k in ("GetTickCount64", "Microsoft.VisualBasic.Devices.ComputerInfo", "$boot =", "$free =", "$total ="):
            self.assertIn(k, fallback)
        self.assertNotIn("Get-Cim", fallback)
        self.assertNotIn("Get-Wmi", fallback)
        self.assertIn("boot = $boot", HC.WIN_PS)

    def test_au_manual_or_not(self):
        self.warn(T0)
        self.assertTrue(HC.view(T0)["au"]["manual"])
        self.win = win(au_options=None)
        self.warn(T0 + 120)
        self.assertFalse(HC.view(T0 + 120)["au"]["manual"])                # 没有组策略：Windows 缺省自动装


class SendTest(Base):
    def setUp(self):
        super().setUp()
        self.win = win(boot=at(1, 9) - 40 * DAY_S)                         # 开机 40 天：一直成立

    def test_daytime_once_a_day(self):
        self.assertEqual(self.tick(at(10, 8, 59)), [])                     # 夜里不发
        m = self.tick(at(10, 9, 0))
        self.assertEqual(len(m), 1)
        self.assertIn("开机", m[0])
        self.assertEqual(self.tick(at(10, 15)), [])                        # 同一天不再发
        self.assertEqual(self.tick(at(10, 23)), [])
        self.assertEqual(self.tick(at(11, 7)), [])
        self.assertEqual(len(self.tick(at(11, 21, 59))), 1)                # 第二天白天再发一次
        self.assertEqual(self.tick(at(12, 22, 0)), [])                     # 22 点起算夜里

    def test_sent_ones_go_to_records(self):
        self.tick(at(10, 10))
        rows = records(cat="board")
        self.assertEqual([r["kind"] for r in rows], ["hostcheck"])
        self.assertIn("hostcheck", EVENT_KINDS)

    def test_maintenance_window_suppresses(self):
        """计划维护（U8 的 maint）期间只记状态、不推；卡上照样标出来。"""
        self.assertEqual(self.tick(at(10, 10), maint_until=at(10, 11)), [])
        self.assertIn("uptime", HC.view(at(10, 10))["warn"])
        self.assertEqual(len(self.tick(at(10, 11))), 1)

    def test_push_false_does_not_mark_sent(self):
        HC.tick(at(10, 10), push=False)
        self.assertEqual(len(self.tick(at(10, 10, 2))), 1)

    def test_concurrent_cli_does_not_erase_sent(self):
        """命令行（labmon hostcheck）与服务里的线程同时跑一轮：读 → 改 → 写整段互斥，命令行不会把后台刚记的“今天推过”抹掉。"""
        entered, go, fake = threading.Event(), threading.Event(), HC.run_ps

        def slow_ps(script, timeout=60):
            if threading.current_thread().name == "cli":
                entered.set()
                go.wait(5)
            return fake(script, timeout)
        self.patch("run_ps", slow_ps)
        sent = []
        cli = threading.Thread(target=HC.tick, args=(at(10, 10),), kwargs={"push": False}, name="cli")
        bg = threading.Thread(target=lambda: sent.extend(HC.tick(at(10, 10, 1))), name="bg")
        cli.start()
        self.assertTrue(entered.wait(5))
        bg.start()
        bg.join(0.5)
        go.set()
        cli.join(5)
        bg.join(5)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.tick(at(10, 15)), [])                        # 同一天不再推


class UpdateSourceTest(Base):
    def test_search_every_six_hours(self):
        self.upd = upd(item("a"))
        self.tick(T0)
        n = lambda: sum("Microsoft.Update.Session" in s for s in self.ps_calls)  # noqa: E731
        self.assertEqual(n(), 1)
        self.tick(T0 + 6 * 3600 - 1)
        self.assertEqual(n(), 1)
        self.tick(T0 + 6 * 3600)
        self.assertEqual(n(), 2)
        self.assertEqual(HC.view(T0)["updates"]["src"], "search")

    def test_task_json_used_when_fresh(self):
        """SYSTEM 计划任务写的 wu.json 12 小时内写过：直接用它，不自己搜。"""
        HC.WU_TASK.parent.mkdir(parents=True)
        HC.WU_TASK.write_text(json.dumps({"t": T0 - 11 * 3600, "updates": [item("t1", sec=True)], "error": None}))
        self.tick(T0)
        self.assertFalse(any("Microsoft.Update.Session" in s for s in self.ps_calls))
        u = HC.view(T0)["updates"]
        self.assertEqual((u["src"], u["at"], [x["id"] for x in u["list"]]), ("task", T0 - 11 * 3600, ["t1"]))

    def test_stale_task_json_falls_back_and_reports(self):
        HC.WU_TASK.parent.mkdir(parents=True)
        HC.WU_TASK.write_text(json.dumps({"t": T0 - 13 * 3600, "updates": [], "error": None}))
        self.upd = {"error": "0x80070005 拒绝访问"}
        self.tick(T0)
        self.assertTrue(any("Microsoft.Update.Session" in s for s in self.ps_calls))
        u = HC.view(T0)["updates"]
        self.assertIsNone(u["list"])
        self.assertIn("拒绝访问", u["err"])
        self.assertIn("计划任务", u["err"])
        self.assertIn("win_err", self.warn(T0 + DAY_S + 1, force_updates=True))

    def test_search_failure_keeps_last_list(self):
        self.upd = upd(item("a", sec=True))
        self.tick(T0)
        self.upd = {"error": "超时"}
        self.tick(T0 + 6 * 3600)
        u = HC.view(T0 + 6 * 3600)["updates"]
        self.assertEqual([x["id"] for x in u["list"]], ["a"])              # 上一次读到的照列，另写这次出错
        self.assertEqual(u["err"], "超时")

    def test_single_update_object_normalized(self):
        """PowerShell 5.1 偶尔把只有一项的数组写成对象：照样认。"""
        self.upd = {"updates": item("solo")}
        self.tick(T0)
        self.assertEqual([x["id"] for x in HC.view(T0)["updates"]["list"]], ["solo"])

    def test_wu_script_is_shipped(self):
        """离线搜索与计划任务跑的是同一个脚本（tools/win/labmon-wu.ps1）。"""
        text = HC.WU_SCRIPT.read_text(encoding="utf-8-sig")
        self.assertIn("Microsoft.Update.Session", text)
        self.assertIn("$s.Online = $false", text)

    def test_install_script_refuses_untrusted_dir(self):
        """装计划任务的脚本不沿用普通用户先建的 C:\\ProgramData\\labmon：所有者隐含 WRITE_DAC，能改回权限、换掉 SYSTEM 要跑的脚本。
        这里只核对脚本写法（Mac 与 CI 上没有 PowerShell）：建目录不带 -Force、核对所有者与重解析点、显式设所有者、里面的东西逐个核对。"""
        text = (HC.WU_SCRIPT.parent / "install-wu-task.ps1").read_text(encoding="utf-8-sig")
        self.assertNotIn("New-Item -ItemType Directory -Force", text)
        for s in ("GetOwner", "SetOwner", "ReparsePoint", "Get-ChildItem"):
            self.assertIn(s, text)
        # 已有文件自己的显式 ACE 优先于继承的：逐个重置成只继承目录的，再核对没有显式 ACE 才注册任务
        self.assertIn("/reset", text)
        self.assertIn("IsInherited", text)
        self.assertLess(text.index("IsInherited"), text.index("Register-ScheduledTask"))

    def test_ps_scripts_have_bom(self):
        """两个脚本里有中文，Windows PowerShell 5.1 以 -File 跑不带 BOM 的脚本时按 ANSI（中文系统是 GBK）读，字会乱、引号会错位：都带 UTF-8 BOM。"""
        for f in ("labmon-wu.ps1", "install-wu-task.ps1"):
            self.assertTrue((HC.WU_SCRIPT.parent / f).read_bytes().startswith(b"\xef\xbb\xbf"), f)


class ReadTest(unittest.TestCase):
    def test_run_ps_full_path_encoded(self):
        """powershell.exe 用全路径（appendWindowsPath=false），脚本经 -EncodedCommand（UTF-16LE base64），cwd 在 /mnt/c；输出带 BOM 也认。"""
        seen = {}

        def fake_run(args, **kw):
            seen["args"], seen["kw"] = args, kw
            return subprocess.CompletedProcess(args, 0, "﻿{\"name\": \"DASHBOARD-HOST\", \"free_mb\": 1}\r\n".encode(), b"")
        with mock.patch.object(HC.subprocess, "run", fake_run):
            d = HC.run_ps("Write-Output 1", timeout=5)
        self.assertEqual(d, {"name": "DASHBOARD-HOST", "free_mb": 1})
        a = seen["args"]
        self.assertEqual(a[0], "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        self.assertIn("-NoProfile", a)
        script = base64.b64decode(a[a.index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertTrue(script.endswith("Write-Output 1"))
        self.assertIn("UTF8Encoding", script)
        self.assertEqual(seen["kw"]["cwd"], "/mnt/c")
        self.assertEqual(seen["kw"]["timeout"], 5)

    def test_run_ps_errors(self):
        bad = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, b"", "拒绝".encode())  # noqa: E731
        with mock.patch.object(HC.subprocess, "run", bad):
            self.assertIn("拒绝", HC.run_ps("x")["error"])
        with mock.patch.object(HC.subprocess, "run", mock.Mock(side_effect=FileNotFoundError("no"))):
            self.assertIn("powershell.exe", HC.run_ps("x")["error"])
        with mock.patch.object(HC.subprocess, "run", mock.Mock(side_effect=subprocess.TimeoutExpired("ps", 5))):
            self.assertIn("error", HC.run_ps("x"))

    def test_read_wsl_parses_proc(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "meminfo").write_text("MemTotal:        4022148 kB\nMemFree:  100 kB\nMemAvailable:    3145728 kB\n")
            (d / "vmstat").write_text("nr_free_pages 1\noom_kill 7\n")
            (d / "status").write_text("Name:\tpython3\nVmRSS:\t   73728 kB\n")
            with mock.patch.multiple(HC, MEMINFO=d / "meminfo", VMSTAT=d / "vmstat", SELF=d / "status"), \
                    mock.patch.object(HC, "unit_state", lambda unit: ("none", None)):
                w = HC.read_wsl()
        self.assertEqual((w["avail_mb"], round(w["total_mb"], 1), w["oom_kill"], w["rss_mb"], w["tunnel"]), (3072.0, 3927.9, 7, 72.0, "none"))

    def test_unit_state(self):
        def run(out, rc=0, err=""):
            return lambda *a, **k: subprocess.CompletedProcess(a[0], rc, out, err)
        with mock.patch.object(HC.subprocess, "run", run("LoadState=loaded\nActiveState=active\n")):
            self.assertEqual(HC.unit_state("x.service"), ("active", None))
        with mock.patch.object(HC.subprocess, "run", run("LoadState=loaded\nActiveState=activating\n")):
            self.assertEqual(HC.unit_state("x.service")[0], "down")
        with mock.patch.object(HC.subprocess, "run", run("LoadState=not-found\nActiveState=inactive\n")):
            self.assertEqual(HC.unit_state("x.service"), ("none", None))
        with mock.patch.object(HC.subprocess, "run", run("", 1, "Failed to connect to bus")):
            st, err = HC.unit_state("x.service")
            self.assertEqual(st, "unknown")
            self.assertIn("bus", err)


class SnapshotTest(Base):
    def test_overview_snapshot_carries_view(self):
        from labmon import overview as O
        self.tick(T0)
        snap = O.collect([], use_ssh=False)
        self.assertEqual(snap["hostcheck"]["name"], "DASHBOARD-HOST")
        with mock.patch.dict(os.environ, {"LABMON_NO_HOSTCHECK": "1"}):
            self.assertIsNone(O.collect([], use_ssh=False)["hostcheck"])

    def test_view_shape(self):
        self.tick(T0)
        v = HC.view(T0)
        for k in ("t", "name", "role", "boot", "reboot", "pause_until", "au", "updates", "win_mem", "wsl_mem", "oom", "rss", "tunnel", "err", "warn", "lim", "day"):
            self.assertIn(k, v)
        self.assertIsNone(v["role"])                                       # 没跑过服务租约（#163）：主 / 备用说不出
        self.assertEqual(v["lim"]["win_free_mb"], 2048)

    def test_before_first_tick(self):
        v = HC.view(T0)
        self.assertIsNone(v["t"])
        self.assertEqual(v["warn"], {})


class CliTest(Base):
    def test_cli_prints_and_does_not_push(self):
        self.win = win(boot=T0 - 40 * DAY_S)
        self.upd = upd(item("k1", sec=True, title="2026-11 安全更新"))
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))), \
                mock.patch.object(HC, "notify", mock.Mock(side_effect=AssertionError("不推送"))):
            self.assertEqual(HC.cli(at(10, 10)), 0)
        text = "\n".join(out)
        self.assertIn("DASHBOARD-HOST", text)
        self.assertIn("2026-11 安全更新", text)
        self.assertIn("会提醒", text)
        self.assertEqual(len(self.tick(at(10, 11))), 1)                    # 命令行跑过不算已发

    def test_cli_not_wsl(self):
        d = Path(self.tmp.name)
        self.patch("INTEROP", (d / "x", d / "y"))
        with mock.patch("builtins.print"):
            self.assertEqual(HC.cli(), 1)


if __name__ == "__main__":
    unittest.main()
