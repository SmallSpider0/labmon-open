"""看板服务所在机的体检（#166，#157 的 U9）：服务跑在 Windows 机的 WSL2 里时，看 Windows 待装更新、待重启、开机时长、内存与隧道，到时推一条提醒。

    labmon hostcheck        跑一轮（含待装更新的搜索），打印各项与会提醒哪些；不推送。不是 WSL 时打印一句、退出码 1

- 只在 WSL 里启用（enabled：有 WSL interop）。笔记本、普通 Linux、CI 上什么都不做：不起线程、不调 powershell.exe，快照里 hostcheck 为 null。
- labmon serve 里一个线程（loop）每 TICK 秒一轮（tick）：
  Windows 侧经 interop 调 powershell.exe（全路径，run_ps）读开机时刻、可用内存、待重启、暂停更新到期、更新方式（read_win）；
  待装更新每 UPD_EVERY 秒读一次（read_updates）：SYSTEM 计划任务写的 wu.json 新就用它，否则自己跑 tools/win/labmon-wu.ps1 做离线搜索；
  WSL 侧读 /proc 的 MemAvailable、oom_kill 计数、本进程 RSS 与隧道单元的状态（read_wsl）。
- “持续多久”都从 labmon 第一次看到起算，记在状态目录的 hostcheck.json（服务重启不丢）。
- 提醒（judge）：每件事一个稳定的 key；推送（due）只在白天 DAY、每个本地日历日最多一次，计划维护期间（maint_until）不推。
  推送走 service.notify，另记一条记录（事件种类 hostcheck）。页面上的「看板服务」小卡读 view()（全局快照的 hostcheck）。
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import subprocess
import time
import traceback
from pathlib import Path

from . import lease as LS
from . import maint as MT
from .config import fmt_dur, fmt_time
from .records import log_events
from .service import log, notify
from .state import load_json, save_json, state_dir

INTEROP = (Path("/proc/sys/fs/binfmt_misc/WSLInterop"), Path("/proc/sys/fs/binfmt_misc/WSLInterop-late"))   # 开了 systemd 的新版 WSL 注册的是后者
PS = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"     # 全路径：wsl.conf 设了 appendWindowsPath=false
PS_CWD = "/mnt/c"                    # 在 Linux 目录下起 Windows 程序会报“不支持 UNC 路径”
WU_SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "win" / "labmon-wu.ps1"   # 离线搜索待装更新：自己跑与计划任务跑的是同一个文件
WU_TASK = Path("/mnt/c/ProgramData/labmon/wu.json")                       # 退路：SYSTEM 计划任务写的结果（tools/win/install-wu-task.ps1）
MEMINFO, VMSTAT, SELF = Path("/proc/meminfo"), Path("/proc/vmstat"), Path("/proc/self/status")
TUNNEL_UNIT = "labmon-tunnel.service"                                     # 到 TX 的反向隧道（systemd 用户单元，模板 tools/wsl/labmon-tunnel.service，#165）

TICK = 120                           # 每轮相隔（秒）
UPD_EVERY = 6 * 3600                 # 待装更新多久读一次
TASK_FRESH_S = 12 * 3600             # 计划任务每 6 小时写一次 wu.json；12 小时内写过算新
DAY = (9, 22)                        # 白天：9:00 起、22:00 前才推送
# 门槛（#157 问题评论“按推荐先做”的）；页面的说明也取这份
LIM = {"uptime_d": 30, "reboot_s": 86400, "sec_s": 3 * 86400, "any_s": 14 * 86400, "pause_s": 3 * 86400,
       "win_free_mb": 2048, "wsl_frac": 0.10, "low_s": 600, "rss_mb": 1024, "tunnel_s": 900, "oom_s": 86400, "err_s": 86400}
AU_TEXT = {2: "下载前通知", 3: "自动下载、通知安装", 4: "自动安装", 5: "由本地管理员选", 7: "自动下载、通知安装与重启"}
AU_MANUAL = (2, 3, 7)                # 这几种都要人点了才装

_HEAD = "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding $false\n"
WIN_PS = r"""$cim = $null
try {
  try {
    $os = Get-CimInstance Win32_OperatingSystem
    $boot = ([DateTimeOffset]$os.LastBootUpTime).ToUnixTimeSeconds()
    $free = [math]::Round($os.FreePhysicalMemory / 1024)
    $total = [math]::Round($os.TotalVisibleMemorySize / 1024)
  } catch {
    # 经 ssh 登录（网络登录的令牌）时标准用户读 WMI 被拒（#193）：开机时刻取 GetTickCount64，内存取 GlobalMemoryStatusEx，都不经 WMI
    $cim = [string]$_.Exception.Message
    Add-Type -Namespace LabmonHC -Name K -MemberDefinition '[DllImport("kernel32.dll")] public static extern ulong GetTickCount64();'
    Add-Type -AssemblyName Microsoft.VisualBasic
    $ci = New-Object Microsoft.VisualBasic.Devices.ComputerInfo
    $boot = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long][math]::Floor([LabmonHC.K]::GetTickCount64() / 1000)
    $free = [math]::Round($ci.AvailablePhysicalMemory / 1MB)
    $total = [math]::Round($ci.TotalPhysicalMemory / 1MB)
  }
  $ux = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings' -ErrorAction SilentlyContinue
  $au = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -ErrorAction SilentlyContinue
  $pause = $null
  if ($ux.PauseUpdatesExpiryTime) {
    try { $pause = [DateTimeOffset]::Parse([string]$ux.PauseUpdatesExpiryTime, [Globalization.CultureInfo]::InvariantCulture).ToUnixTimeSeconds() } catch {}
  }
  [ordered]@{
    name = $env:COMPUTERNAME
    boot = $boot
    free_mb = $free
    total_mb = $total
    reboot_wu = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    reboot_cbs = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
    pause_until = $pause
    au_options = $au.AUOptions
    no_auto = $au.NoAutoUpdate
  } | ConvertTo-Json -Compress
} catch {
  $m = [string]$_.Exception.Message
  if ($cim) { $m = "WMI：${cim}；退路：${m}" }
  @{ error = $m } | ConvertTo-Json -Compress
}"""


def enabled() -> bool:
    """在 WSL 里（有 interop）才启用；测试夹具设的 LABMON_NO_HOSTCHECK 关掉（在 WSL 里跑测试也不真调 powershell.exe）。"""
    return not os.environ.get("LABMON_NO_HOSTCHECK") and any(p.exists() for p in INTEROP)


def _file() -> Path:
    return state_dir() / "hostcheck.json"


# ---------------- 读 ----------------
def run_ps(script: str, timeout: int = 60) -> dict:
    """经 interop 跑一段 PowerShell，读回它打到标准输出的最后一行 JSON（脚本自己出错也回 {"error"}）；调不起、超时、读不出也回 {"error"}。"""
    enc = base64.b64encode((_HEAD + script).encode("utf-16-le")).decode()
    try:
        r = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                           capture_output=True, timeout=timeout, cwd=PS_CWD, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": f"powershell.exe {timeout} 秒没跑完"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"调不起 powershell.exe：{e}"}
    out = r.stdout.decode("utf-8", "replace").replace("﻿", "").strip()
    try:
        d = json.loads(out.splitlines()[-1]) if out else None
    except ValueError:
        d = None
    if isinstance(d, dict):
        return d
    err = r.stderr.decode("utf-8", "replace").strip() or out
    return {"error": f"powershell.exe 退出码 {r.returncode}：{err[-200:] or '没有输出'}"}


def read_win() -> dict:
    """Windows 侧每轮读的（WIN_PS）；出错回 {"error"}。"""
    return run_ps(WIN_PS, timeout=60)


def _upd_list(x) -> list[dict]:
    """PowerShell 5.1 偶尔把只有一项的数组写成对象：统一成列表，只留认得的字段。"""
    xs = x if isinstance(x, list) else [x] if isinstance(x, dict) else []
    return [{"id": str(u.get("id") or u.get("title") or "?"), "title": str(u.get("title") or ""), "kb": str(u.get("kb") or ""),
             "sec": bool(u.get("sec")), "driver": bool(u.get("driver"))} for u in xs if isinstance(u, dict)]


def read_updates(t: float) -> dict:
    """待装更新：{"list", "src"（task 计划任务写的 / search 自己搜的）, "at"（结果是何时的）, "err"}；读不出时 list 为 None。
    计划任务的 wu.json 新（TASK_FRESH_S 内）就用它；否则自己跑 labmon-wu.ps1（服务账号做不了离线搜索时会报权限错，那就要装计划任务）。"""
    task = load_json(WU_TASK, None)
    if isinstance(task, dict) and t - (task.get("t") or 0) < TASK_FRESH_S and not task.get("error") and task.get("updates") is not None:
        return {"list": _upd_list(task["updates"]), "src": "task", "at": task["t"], "err": None}
    try:
        script = WU_SCRIPT.read_text(encoding="utf-8-sig")
    except OSError as e:
        return {"list": None, "src": "search", "at": t, "err": f"找不到离线搜索的脚本 {WU_SCRIPT}：{e}"}
    d = run_ps("& {\n" + script + "\n}", timeout=180)                       # 包成脚本块：脚本开头的 param() 才合法
    if not d.get("error") and d.get("updates") is not None:
        return {"list": _upd_list(d["updates"]), "src": "search", "at": t, "err": None}
    err = d.get("error") or "离线搜索没读出待装更新"
    if isinstance(task, dict):
        err += f"；计划任务那份{'也出错：' + str(task['error']) if task.get('error') else '是 ' + fmt_time(task.get('t') or 0) + ' 的，已过期'}"
    return {"list": None, "src": "search", "at": t, "err": err}


def _kv(path: Path) -> dict[str, int]:
    """/proc 下“名字[:] 数字”一行一项的文件。"""
    out = {}
    for line in path.read_text().splitlines():
        m = re.match(r"(\w+):?\s+(\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def unit_state(unit: str) -> tuple[str, str | None]:
    """systemd 用户单元的状态：active、down（装了但不在 active）、none（没装）、unknown（读不了，第二项是原因）。"""
    try:
        r = subprocess.run(["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return "unknown", str(e)[:200]
    kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if r.returncode or not kv:
        return "unknown", (r.stderr.strip() or f"systemctl 退出码 {r.returncode}")[:200]
    if kv.get("LoadState") == "not-found":
        return "none", None
    return ("active" if kv.get("ActiveState") == "active" else "down"), None


def read_wsl() -> dict:
    """WSL 侧：可用内存与上限（MiB）、oom_kill 计数、本进程（labmon serve）的 RSS（MiB）、隧道单元的状态；读不了的项为 None，原因放 err。"""
    out, errs = {"avail_mb": None, "total_mb": None, "oom_kill": None, "rss_mb": None}, []
    try:
        m = _kv(MEMINFO)
        out["avail_mb"], out["total_mb"] = m["MemAvailable"] / 1024, m["MemTotal"] / 1024
    except (OSError, KeyError) as e:
        errs.append(f"meminfo：{e}")
    try:
        out["oom_kill"] = _kv(VMSTAT)["oom_kill"]
    except (OSError, KeyError) as e:
        errs.append(f"vmstat：{e}")
    try:
        out["rss_mb"] = _kv(SELF)["VmRSS"] / 1024
    except (OSError, KeyError) as e:
        errs.append(f"status：{e}")
    out["tunnel"], out["tunnel_err"] = unit_state(TUNNEL_UNIT)
    out["err"] = "；".join(errs) or None
    return out


# ---------------- 记与判 ----------------
def _since(st: dict, key: str, on: bool, t: float) -> None:
    """记“从何时起”：成立时没记过就记 t，不成立就删掉。"""
    s = st.setdefault("since", {})
    if on:
        s.setdefault(key, t)
    else:
        s.pop(key, None)


def sample(st: dict, t: float, win: dict, wsl: dict, upd: dict | None) -> None:
    """把这一轮读到的并进状态：各项“从何时起”、OOM 计数、各条更新第一次看到的时刻。读不了的项沿用上一次的值、另记原因。"""
    st["t"] = t
    err = st.setdefault("err", {})
    if win.get("error"):
        err["win"] = str(win["error"])[:300]
    else:
        err.pop("win", None)
        p = win.get("pause_until")                                   # PowerShell 已换成 epoch 秒（Python 侧不解析时刻文字）；不是数当没暂停
        st["win"] = {**{k: win.get(k) for k in ("name", "boot", "free_mb", "total_mb", "au_options", "no_auto")},
                     "pause_until": p if isinstance(p, (int, float)) and not isinstance(p, bool) else None}
        _since(st, "reboot", bool(win.get("reboot_wu") or win.get("reboot_cbs")), t)
        _since(st, "win_mem", win.get("free_mb") is not None and win["free_mb"] < LIM["win_free_mb"], t)
    st["wsl"] = {k: wsl.get(k) for k in ("avail_mb", "total_mb", "rss_mb", "tunnel")}
    err["wsl"] = wsl.get("err") or wsl.get("tunnel_err")
    a, tot = wsl.get("avail_mb"), wsl.get("total_mb")
    _since(st, "wsl_mem", a is not None and bool(tot) and a < LIM["wsl_frac"] * tot, t)
    _since(st, "tunnel", wsl.get("tunnel") == "down", t)
    n, oom = wsl.get("oom_kill"), st.setdefault("oom", {"n": None, "hits": []})
    if n is not None:
        if oom["n"] is not None and n > oom["n"]:          # 计数变小是 WSL 重启过，不算新的
            oom["hits"].append([t, n - oom["n"]])
        oom["n"] = n
    oom["hits"] = [h for h in oom["hits"] if t - h[0] < LIM["oom_s"]]
    if upd is not None:
        old = st.get("upd") or {}
        if upd["list"] is None:                            # 这次没读出：照列上一次的，另写原因
            st["upd"] = {**old, "checked": t, "err": upd["err"]}
        else:
            seen = old.get("seen") or {}
            st["upd"] = {"checked": t, "at": upd["at"], "src": upd["src"], "err": None, "list": upd["list"],
                         "seen": {u["id"]: seen.get(u["id"], t) for u in upd["list"]}}
    _since(st, "win_err", bool(err.get("win") or (st.get("upd") or {}).get("err")), t)


def _gib(mb: float) -> str:
    return f"{mb / 1024:.1f} GiB"


def judge(st: dict, t: float) -> dict[str, str]:
    """此刻成立的提醒：{key: 给用户的一句话}。只看状态（sample 记好的），门槛见 LIM。"""
    w, wl, s, out = st.get("win") or {}, st.get("wsl") or {}, st.get("since") or {}, {}
    who = w.get("name") or "看板服务所在机"
    u = st.get("upd") or {}
    old = [(x, t - u["seen"].get(x["id"], t)) for x in u.get("list") or []]
    hot = [(x, a) for x, a in old if a >= (LIM["sec_s"] if x["sec"] else LIM["any_s"])]
    if hot:
        ns = sum(1 for x in u["list"] if x["sec"])
        out["updates"] = (f"{who} 有 {len(u['list'])} 个 Windows 更新没装" + (f"（安全更新 {ns} 个）" if ns else "")
                          + f"，最久的已挂 {fmt_dur(max(a for _, a in hot))}：找个空档手动装，装完按提示重启")
    if "reboot" in s and t - s["reboot"] > LIM["reboot_s"]:
        out["reboot"] = f"{who} 装完更新等重启已 {fmt_dur(t - s['reboot'])}：找个空档重启（重启后看板服务自己起来）"
    if w.get("boot") and t - w["boot"] > LIM["uptime_d"] * 86400:
        out["uptime"] = f"{who} 已连续开机 {fmt_dur(t - w['boot'])}：找个空档装更新、重启一次"
    p = w.get("pause_until")
    if p and t < p <= t + LIM["pause_s"]:
        out["pause"] = (f"{who} 的“暂停更新” {fmt_time(p)} 到期（还剩 {fmt_dur(p - t)}）：到期后 Windows 会自动装更新并在夜里重启，"
                        "先把更新改成通知后手动装，或再延长暂停")
    if "win_mem" in s and t - s["win_mem"] >= LIM["low_s"]:
        out["win_mem"] = f"{who} 的 Windows 可用内存只剩 {_gib(w['free_mb'])}（已持续 {fmt_dur(t - s['win_mem'])}）：看看谁占的"
    if "wsl_mem" in s and t - s["wsl_mem"] >= LIM["low_s"]:
        out["wsl_mem"] = f"{who} 的 WSL 可用内存只剩 {_gib(wl['avail_mb'])} / {_gib(wl['total_mb'])}（已持续 {fmt_dur(t - s['wsl_mem'])}）：看看 WSL 里谁占的"
    hits = (st.get("oom") or {}).get("hits") or []
    if hits:
        out["oom"] = f"{who} 的 WSL 里近 24 小时有 {sum(h[1] for h in hits)} 次 OOM kill（最近一次 {fmt_time(hits[-1][0])}）：内存上限可能给小了"
    if (wl.get("rss_mb") or 0) > LIM["rss_mb"]:
        out["rss"] = f"看板服务进程占内存 {_gib(wl['rss_mb'])}，像在泄漏：重启服务（systemctl --user restart labmon）"
    if "tunnel" in s and t - s["tunnel"] > LIM["tunnel_s"]:
        out["tunnel"] = f"到公网入口的隧道（{TUNNEL_UNIT}）已断 {fmt_dur(t - s['tunnel'])}：手机与校外打不开看板"
    if "win_err" in s and t - s["win_err"] > LIM["err_s"]:
        e = (st.get("err") or {}).get("win") or u.get("err") or "原因不明"
        out["win_err"] = f"{who} 读不了 Windows 状态已 {fmt_dur(t - s['win_err'])}：{e}"
    return out


def daytime(t: float) -> bool:
    return DAY[0] <= time.localtime(t).tm_hour < DAY[1]


def due(st: dict, warn: dict[str, str], t: float, maint_until: float | None = None) -> list[str]:
    """该推的：白天、不在计划维护期间、这个 key 今天（本地日历日）还没推过。推了的记进 st["sent"]。"""
    if not daytime(t) or (maint_until and t < maint_until):
        return []
    day, sent = time.strftime("%Y-%m-%d", time.localtime(t)), st.setdefault("sent", {})
    out = []
    for k, m in warn.items():
        if sent.get(k) != day:
            sent[k] = day
            out.append(m)
    return out


def tick(t: float | None = None, maint_until: float | None = None, push: bool = True, force_updates: bool = False,
         serve: bool = True) -> list[str]:
    """一轮：读、记、判，返回该推的提醒（调用方 notify）；推了的记一条记录。push=False（命令行）只记状态、不算已推。
    serve=False（命令行）：本进程的 RSS 不是看板服务的，沿用服务上一轮量的。
    maint_until：计划维护到何时（labmon server maint，maint.until；loop 每轮传），这之前不推。不是 WSL 时什么都不做。"""
    if not enabled():
        return []
    t = time.time() if t is None else t
    _file().parent.mkdir(parents=True, exist_ok=True)
    with open(_file().with_suffix(".lock"), "w") as lk:           # 读 → 改 → 写整段互斥：命令行与服务里的线程各跑一轮时，后写的不抹掉先记的“今天推过”
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load_json(_file(), {}) or {}
        win = read_win()
        upd = read_updates(t) if force_updates or t - ((st.get("upd") or {}).get("checked") or 0) >= UPD_EVERY else None
        wsl = read_wsl()
        if not serve:
            wsl["rss_mb"] = (st.get("wsl") or {}).get("rss_mb")
        sample(st, t, win, wsl, upd)
        st["warn"] = judge(st, t)
        msgs = due(st, st["warn"], t, maint_until) if push else []
        save_json(_file(), st)
    if msgs:
        log_events([{"kind": "hostcheck", "msg": m} for m in msgs], t)
    return msgs


def loop() -> None:
    """labmon serve 里的体检线程（serve 只在 enabled 时起它）。"""
    while True:
        try:
            msgs = tick(maint_until=MT.until(), push=LS.primary())   # 备用（#163）只记状态：主那台推，两台都推就重了
            notify(msgs)
            for m in msgs:
                log("体检：" + m)
        except Exception:  # noqa: BLE001 — 体检出错不影响看板
            log(f"体检出错\n{traceback.format_exc()}")
        time.sleep(TICK)


# ---------------- 给页面与命令行 ----------------
def view(t: float | None = None) -> dict | None:
    """全局快照的 hostcheck（工作台「看板服务」小卡）；不是 WSL 时 None（页面不出卡）。内存是 MiB，时刻是 epoch 秒。
    role：主（primary）/ 备用（standby），取服务租约（lease.view，#163）；没跑过租约是 None。warn：此刻成立的提醒（与推送同一句话，不受白天与一天一次的限制）。
    maint：计划维护到何时（maint.until；不在维护为 None），期间不推。"""
    if not enabled():
        return None
    st = load_json(_file(), {}) or {}
    w, wl, s, u, oom = st.get("win") or {}, st.get("wsl") or {}, st.get("since") or {}, st.get("upd") or {}, st.get("oom") or {}
    au, no = w.get("au_options"), w.get("no_auto")
    seen = u.get("seen") or {}
    ul = None if u.get("list") is None else sorted(({**x, "since": seen.get(x["id"])} for x in u["list"]),
                                                    key=lambda x: (not x["sec"], x["since"] or 0))
    hits = oom.get("hits") or []
    return {"t": st.get("t"), "name": w.get("name"), "role": (LS.view(t) or {}).get("role"), "maint": MT.until(t),
            "boot": w.get("boot"), "reboot": s.get("reboot"), "pause_until": w.get("pause_until"),
            "au": None if not w else {"manual": no == 1 or au in AU_MANUAL,
                                      "text": "不自动更新" if no == 1 else AU_TEXT.get(au, f"AUOptions={au}") if au is not None else "自动安装（Windows 缺省）"},
            "updates": {"list": ul, "src": u.get("src"), "at": u.get("at"), "checked": u.get("checked"), "err": u.get("err")},
            "win_mem": {"free": w.get("free_mb"), "total": w.get("total_mb"), "since": s.get("win_mem")},
            "wsl_mem": {"avail": wl.get("avail_mb"), "total": wl.get("total_mb"), "since": s.get("wsl_mem")},
            "oom": {"n": sum(h[1] for h in hits), "last": hits[-1][0] if hits else None},
            "rss": wl.get("rss_mb"),
            "tunnel": {"state": wl.get("tunnel"), "since": s.get("tunnel"), "unit": TUNNEL_UNIT},
            "err": {k: v for k, v in (st.get("err") or {}).items() if v},
            "warn": st.get("warn") or {}, "lim": LIM, "day": list(DAY), "tick": TICK, "upd_every": UPD_EVERY}


def cli(t: float | None = None) -> int:
    """labmon hostcheck：跑一轮（含待装更新的搜索）、打印，不推送。"""
    if not enabled():
        print("[labmon] 这里不是 WSL（没有 interop）：体检只在 WSL 里的看板服务上启用")
        return 1
    t = time.time() if t is None else t
    tick(t, push=False, force_updates=True, serve=False)
    v = view(t)
    na = lambda x, f: "读不了" if x is None else f(x)  # noqa: E731
    u = v["updates"]
    ul = u["list"]
    if ul is None:
        ut = "读不了"
    else:
        ut = f"{len(ul)} 个（安全更新 {sum(1 for x in ul if x['sec'])} 个），" + {"task": "来自计划任务", "search": "离线搜索"}.get(u["src"], "")
        ut += f" {fmt_time(u['at'])}" if u["at"] else ""
    lines = [f"[labmon] {v['name'] or '看板服务所在机'} 体检（{fmt_time(t, sec=True)}）",
             f"  开机        {na(v['boot'], lambda b: f'{fmt_time(b)}，已 {fmt_dur(t - b)}')}",
             f"  待重启      {'要重启，' + fmt_time(v['reboot']) + ' 起' if v['reboot'] else '不用'}",
             f"  待装更新    {ut}" + (f"；出错：{u['err']}" if u["err"] else "")]
    lines += [f"              · {'[安全] ' if x['sec'] else ''}{x['title']}" for x in (u["list"] or [])[:20]]
    au = v["au"] or {}
    lines += [f"  更新方式    {au.get('text', '读不了')}" + (f"；暂停到 {fmt_time(v['pause_until'])}" if v["pause_until"] else ""),
              f"  Windows 内存 可用 {na(v['win_mem']['free'], _gib)} / {na(v['win_mem']['total'], _gib)}",
              f"  WSL 内存    可用 {na(v['wsl_mem']['avail'], _gib)} / {na(v['wsl_mem']['total'], _gib)}；近 24 小时 OOM kill {v['oom']['n']} 次",
              f"  服务进程    {na(v['rss'], lambda r: f'{r:.0f} MiB')}（看板服务上一轮量的）",
              f"  隧道        {({'active': '在连', 'down': '断开', 'none': '没装', 'unknown': '读不了'}).get(v['tunnel']['state'], '—')}（{TUNNEL_UNIT}）"]
    if v["maint"]:
        lines.append(f"  计划维护    到 {fmt_time(v['maint'])}，期间不推（labmon server maint off 提前结束）")
    lines += [f"  出错：{k} {e}" for k, e in v["err"].items()]
    lines += ["会提醒（白天 %d–%d 点推送，每件事一天一次）：" % tuple(DAY)] + [f"  · {m}" for m in v["warn"].values()] if v["warn"] else ["没有要提醒的"]
    for line in lines:
        print(line)
    return 0
