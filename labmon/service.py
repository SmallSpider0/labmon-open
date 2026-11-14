"""把 serve 交给系统的服务管理托管：登录（Linux 上开机，靠 linger）后自启、挂了自动拉起，不依赖任何终端或会话。
macOS 用 launchd（~/Library/LaunchAgents/com.labmon.serve.plist），Linux 用 systemd 用户单元（~/.config/systemd/user/labmon.service，#164）；
服务参数（端口、公网端口、采集间隔）就存在这份托管配置里，命令一样。项目不写在服务参数里：看板服务按登记加载
（labmon project push 登记、archive 收起，projects.py，#161）。

    labmon service install [--port 8765] [--public-port P] [--interval 秒]
                                                                  装上（或改参数后重装）服务并重启；--public-port 另起公网入口（只绑 127.0.0.1，给隧道接；写 0 去掉），
                                                                  --interval 改采集间隔，都不写则保持原样
    labmon service status                                         服务状态、项目与访问地址
    labmon service restart                                        重启服务（升级 labmon 代码后用）
    labmon service stop                                           停掉服务并删除托管配置

一台机器一个服务，多个项目共用（各有自己的 /p/<id>/）。日志（LOG）在 macOS 上是 ~/Library/Logs/labmon.log，Linux 上是 <状态根>/labmon.log，
超过 1 MB 挪成 labmon.log.1（只留一份旧的）。
系统通知也在这里（notify）：macOS 弹窗；~/.config/labmon/notify.toml 配了的同时推到手机（Bark 或 ntfy，#159，docs/workbench.md「系统通知与推送」）。
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import push as P
from .config import PORT, ConfigError, fmt_time, server
from .state import state_root, write_atomic

MANAGER = "launchd" if sys.platform == "darwin" else "systemd"    # 谁托管看板服务（测试打桩）
LABEL = "com.labmon.serve"
PLIST = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
UNIT = "labmon.service"
UNIT_FILE = Path(f"~/.config/systemd/user/{UNIT}").expanduser()
LOG = Path("~/Library/Logs/labmon.log").expanduser() if MANAGER == "launchd" else state_root() / "labmon.log"
LOG_MAX = 1 << 20
_ROT = [0.0]


def log(msg: str) -> None:
    """服务日志的一行（带时刻；launchd 把 stdout 接在 labmon.log 上）：看板服务、借用与续租给 worker 下指令的回话都经它。"""
    if time.time() - _ROT[0] > 3600:                 # 一小时看一次日志要不要轮转
        _ROT[0] = time.time()
        rotate_log()
    print(f"[labmon] {fmt_time(time.time(), sec=True)} {msg}", flush=True)


def rotate_log() -> None:
    """日志超过 LOG_MAX 就复制成 labmon.log.1、原文件清空接着写。launchd 把 stdout、stderr 接在这个文件上，
    先给它们加上追加方式，清空后才会从头写，不留一段空洞。"""
    try:
        if LOG.stat().st_size <= LOG_MAX:
            return
        for fd in (1, 2):
            fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_APPEND)
        shutil.copyfile(LOG, LOG.with_name(LOG.name + ".1"))
        os.truncate(LOG, 0)
    except OSError:
        pass


def notify(msgs: list[str], force: bool = False, to: list[str] | None = None) -> threading.Thread | None:
    """系统通知：只收给用户看的文字（掉线与恢复、租期、预备续租、借用、共享盘、平台操作结果；只给实验会话的项目告警与 wake 不进来，#142）。
    to 为空是给管理员的（现有的提醒都是）：macOS 上弹窗，推到 notify.toml 配的与各管理员自己填的推送（#167，同一个目标只推一次）；
    to 给了几个账号（#167：主机掉线恢复给能用它的人、普通用户项目的告警给主人），只推到他们各自在「我的账号」填的，不弹窗。
    推送另起一个线程逐条推，不等它：失败只记服务日志、不重试，不挡采集。返回推送线程（没推就是 None）。
    设了 LABMON_NO_NOTIFY（测试）就什么都不做，配置也不读。
    这台看板服务是备用（服务租约，#163）时不发：主那台照样发，两台都发就重了；force 是租约自己的那条（“这台成了备用”）。"""
    if not msgs:
        return None
    from . import lease                                 # lease 在 decide 里延迟 import 本模块，这里同样延迟
    if not force and not lease.primary():
        return None
    if os.environ.get("LABMON_NO_NOTIFY"):
        return None
    confs = []
    if to is None:
        _popup(msgs)
        try:
            confs.append(push_conf())
        except ConfigError as e:
            log(f"推送没发：{e}")
        from . import users                             # users 经 projects 间接 import 了 lease 等，延迟 import
        names = users.admins()
    else:
        names = list(to)
    for n in dict.fromkeys(x.lower() for x in names):
        try:
            confs.append(user_push(n))
        except ConfigError as e:
            log(f"推送没发给 {n}：{e}")
    seen, uniq = set(), []
    for c in confs:
        k = c and P.identity(c)
        if c and k not in seen:
            seen.add(k)
            uniq.append(c)
    if not uniq:
        return None
    th = threading.Thread(target=_push_all, args=(uniq, list(msgs)), name="labmon-push")   # 不设 daemon：命令行进程等它推完再退出
    th.start()
    return th


def _popup(msgs: list[str]) -> None:
    """弹 macOS 系统通知（不在 macOS 上什么都不做）。"""
    if os.uname().sysname != "Darwin":
        return
    for m in msgs:
        script = f'display notification {json.dumps(m, ensure_ascii=False)} with title "labmon" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


# ---------------- 推到手机（#159；推送本身在 push.py，TX 的看门狗共用） ----------------
def notify_file() -> Path:
    """推送配置（维护者手写，600 权限，不进仓库与备份）；LABMON_NOTIFY_CONF 改路径（测试夹具指到不存在的文件）。"""
    return Path(os.environ.get("LABMON_NOTIFY_CONF", "~/.config/labmon/notify.toml")).expanduser()


def push_conf() -> dict | None:
    """读推送配置（push.load）：没有文件、没写 push 返回 None（只弹窗）；写错抛 ConfigError。每次发都重读，改了不用重启服务。"""
    try:
        return P.load(notify_file())
    except P.ConfError as e:
        raise ConfigError(str(e)) from None


def _push_all(confs: list[dict], msgs: list[str]) -> None:
    """推送线程：逐个目标逐条推，一条失败记一行服务日志、不重试，接着推下一条。"""
    for conf in confs:
        for m in msgs:
            try:
                P.one(conf, m)
            except P.ERRORS as e:
                error = P.personal_error(e) if conf.get("personal") else P.err(e)
                log(f"推送失败（{P.KINDS[conf['push']]}）：{error}；消息：{m[:80]}")


# 每人自己的推送（#167）：「我的账号」里填，存在 notify.toml 旁边的 notify.d/<账号>.toml（600，写法同 notify.toml，不进仓库与备份）
def user_push_file(name: str) -> Path:
    return notify_file().with_name("notify.d") / f"{name.lower()}.toml"


def user_push(name: str) -> dict | None:
    """这人填的推送（push.load）：没填返回 None；写坏了抛 ConfigError。"""
    try:
        c = P.load(user_push_file(name))
        return _personal_push(c) if c else None
    except P.ConfError as e:
        raise ConfigError(str(e)) from None


def _personal_push(conf: dict) -> dict:
    try:
        trusted = push_conf()
    except ConfigError:
        trusted = None
    return P.personal(conf, trusted)


def push_view(name: str) -> dict:
    """「我的账号」的推送一节：{set 填了没有, target 打码的目标（push.target）, kind, server, encrypted / token 有没有, error 写坏了的原因}；密钥不出。"""
    try:
        c = user_push(name)
    except ConfigError as e:
        return {"set": False, "error": str(e)}
    if not c:
        return {"set": False}
    return {"set": True, "target": P.target(c), "kind": c["push"], "server": c["server"],
            "encrypted": bool(c.get("encrypt_key")), "token": bool(c.get("token"))}


def set_user_push(name: str, form: dict | None) -> str:
    """存、删这人的推送（POST /api/push）。form 同 notify.toml 解析出的表（{push, bark: {key, server?, encrypt_key?}} 或 {push, ntfy: {topic, server?, token?}}）；
    为空或 push 为空是删掉。先照 push.parse 校验，不过抛 ConfigError、不写。"""
    f = user_push_file(name)
    if not form or form.get("push") in (None, ""):
        f.unlink(missing_ok=True)
        return "删掉了你的推送：看板不再推给你"
    try:
        c = _personal_push(P.parse(form, "推送"))
    except P.ConfError as e:
        raise ConfigError(str(e)) from None
    f.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(f, P.dump(c), mode=0o600)
    return f"存好了：推到 {P.target(c)}；点「试推」看手机收不收得到"


def user_push_test(name: str) -> str:
    """POST /api/push/test：同步推一条试推给这人自己（不弹窗）；没填、写错、推不出去抛 ConfigError。"""
    if os.environ.get("LABMON_NO_NOTIFY"):
        raise ConfigError("看板设着 LABMON_NO_NOTIFY，推送都不发")
    c = user_push(name)
    if not c:
        raise ConfigError("还没填推送：先在上面填好、保存")
    try:
        P.one(c, f"labmon 试推：收到这条说明推送配好了（{fmt_time(time.time(), sec=True)}）")
    except P.ERRORS as e:
        raise ConfigError(f"没发出去：{P.personal_error(e)}") from None
    return f"已推到 {P.target(c)}，看手机有没有收到"


def notify_test(text: str | None = None) -> int:
    """labmon notify test：照配置同步发一条试推（也弹窗），打印后端、目标与结果。0 发出去了，1 没配推送或没发出去，3 配置写错。"""
    path = notify_file()
    if os.environ.get("LABMON_NO_NOTIFY"):
        print("[labmon] 设着 LABMON_NO_NOTIFY，弹窗与推送都不发")
        return 1
    try:
        conf = push_conf()
    except ConfigError as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 3
    if conf is None:
        print(f"[labmon] 没配推送：在 {path} 写 push = \"bark\" 或 \"ntfy\" 和对应的一节（写法见 docs/workbench.md「系统通知与推送」）")
        return 1
    if path.stat().st_mode & 0o077:
        print(f"[labmon] {path} 别人也能读，里面有推送密钥：chmod 600 {path}")
    msg = text or f"labmon 试推：收到这条说明手机推送配好了（{fmt_time(time.time(), sec=True)}）"
    _popup([msg])
    print(f"[labmon] 推到 {P.target(conf)}")
    try:
        P.one(conf, msg)
    except P.ERRORS as e:
        print(f"[labmon] 没发出去：{P.err(e)}", file=sys.stderr)
        return 1
    print("[labmon] 已发出，看手机有没有收到")
    return 0


def _entries(st: dict) -> str:
    """两个入口的地址（命令行打印）：内网入口按主机清单的 [server]（绑哪、有没有证书，#165），写错了照缺省写、serve 起不来时日志里有原因。"""
    try:
        conf = server()
    except ConfigError:
        conf = {"lan_bind": "127.0.0.1", "tls_cert": None}
    lan = f"{'https' if conf['tls_cert'] else 'http'}://{conf['lan_bind']}:{st['port']}/"
    return f"内网入口 {lan}" + (f"，公网入口 127.0.0.1:{st['public_port']}（给隧道接）" if st.get("public_port") else "")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def installed() -> bool:
    """装了服务没有（托管配置在不在）。"""
    return (PLIST if MANAGER == "launchd" else UNIT_FILE).exists()


def _args() -> list[str]:
    """托管配置里存的命令行（plist 的 ProgramArguments、单元的 ExecStart=）；没装是空的。"""
    if not installed():
        return []
    if MANAGER == "launchd":
        return plistlib.loads(PLIST.read_bytes()).get("ProgramArguments", [])
    line = next((x for x in UNIT_FILE.read_text().splitlines() if x.startswith("ExecStart=")), "ExecStart=")
    return [a.replace("%%", "%").replace("$$", "$") for a in shlex.split(line[len("ExecStart="):])]


def _read() -> dict:
    """读出现有服务的参数：port、public_port（公网入口，None 为没开）、interval（None 为按各项目配置）；
    legacy 是以前托管配置里的 -c（可删：#161 迁移用，部署后跑过一次 labmon service install 就没有了，连同 install 里迁移的几行一起删）。"""
    st = {"legacy": [], "port": PORT, "public_port": None, "interval": None}
    args = _args()
    for i, a in enumerate(args):
        if a == "-c":
            st["legacy"].append(args[i + 1])
        elif a == "--port":
            st["port"] = int(args[i + 1])
        elif a == "--interval":
            st["interval"] = int(args[i + 1])
        elif a == "--public-port":
            st["public_port"] = int(args[i + 1])
    return st


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _sd_arg(a: str) -> str:
    """ExecStart= 里的一个参数：有空白的加双引号，% 与 $ 写两遍（systemd 的说明符与变量展开）。
    引号与反斜杠不支持（解释器路径、配置路径、数字里没有），遇到就停下说清楚。"""
    if any(c in a for c in "\"'\\"):
        raise SystemExit(f"[labmon] 参数里有引号或反斜杠，写不进 systemd 单元：{a}")
    a = a.replace("%", "%%").replace("$", "$$")
    return f'"{a}"' if not a or any(c.isspace() for c in a) else a


def unit_text(args: list[str]) -> str:
    """systemd 用户单元（#164）：挂了 30 秒后拉起（同 launchd 的 ThrottleInterval），输出追加到 LOG（append: 要 systemd ≥ 240）；
    开机自启靠 linger。停服务时 systemd 发 SIGTERM，serve 等这一轮采集做完、释放服务租约（#163，lease.release 最多 RELEASE_S 秒，
    远小于缺省 90 秒的 TimeoutStopSec）；不用 ExecStop：那时服务还在采集，释放后这一轮又续上。"""
    return ("[Unit]\nDescription=labmon 看板服务\nAfter=network-online.target\n\n[Service]\n"
            f"ExecStart={' '.join(map(_sd_arg, args))}\nWorkingDirectory={Path.home()}\n"
            "Environment=PYTHONUNBUFFERED=1\nEnvironment=PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n"
            f"Restart=always\nRestartSec=30\nStandardOutput=append:{LOG}\nStandardError=append:{LOG}\n\n"
            "[Install]\nWantedBy=default.target\n")


def linger_hint() -> str | None:
    """Linux：这个账号没开 linger（没人登录时用户的 systemd 不起，服务就不在）就先试着自己开；开不了返回给维护者做的那句。"""
    user = os.environ.get("USER") or str(os.getuid())
    try:
        r = subprocess.run(["loginctl", "show-user", user, "--property=Linger"], capture_output=True, text=True, timeout=10)
        if r.stdout.strip() == "Linger=yes":
            return None
        if subprocess.run(["loginctl", "enable-linger", user], capture_output=True, text=True, timeout=10).returncode == 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return f"这个账号没开 linger：没人登录时服务起不来。请维护者跑一次 sudo loginctl enable-linger {user}"


def _write_and_restart(st: dict) -> None:
    args = [sys.executable, "-u", "-m", "labmon", "serve", "--port", str(st["port"])]
    if st.get("interval"):
        args += ["--interval", str(st["interval"])]
    if st.get("public_port"):
        args += ["--public-port", str(st["public_port"])]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if MANAGER == "systemd":
        UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        UNIT_FILE.write_text(unit_text(args))
        for step in (("daemon-reload",), ("enable", UNIT), ("restart", UNIT)):
            r = _systemctl(*step)
            if r.returncode:
                raise SystemExit(f"[labmon] systemctl --user {' '.join(step)} 失败：{r.stderr.strip()}（单元 {UNIT_FILE}）")
        if (hint := linger_hint()):
            print(f"[labmon] {hint}")
        return
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(Path.home()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,                       # 反复崩溃时至少隔 30 秒再拉起
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
    }))
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    # bootout 返回时旧进程可能还没退完，紧接着 bootstrap 会报 “5: Input/output error”，服务就停在未加载：稍等重试
    for i in range(10):
        r = _launchctl("bootstrap", _domain(), str(PLIST))
        if r.returncode == 0:
            return
        time.sleep(1 + i)
    raise SystemExit(f"[labmon] launchctl bootstrap 失败：{r.stderr.strip()}（服务未加载，"
                     f"可手动 launchctl bootstrap {_domain()} {PLIST}）")


def install(port: int | None, interval: int | None = None, public_port: int | None = None) -> int:
    """装上服务（或改端口、公网入口、采集间隔后重装）并重启。项目不在参数里：看板服务按登记加载（projects.py）。"""
    from .projects import Refused, adopt, project_ids
    st = _read()
    for c in st.pop("legacy"):                        # 可删（#161 迁移）：以前托管配置里 -c 的项目先登记进来，再去掉 -c，免得重装后一个项目都没有
        try:
            note = adopt(c)
        except (ConfigError, OSError, Refused) as e:
            print(f"[labmon] {c} 没登记进来，服务没动（修好后再跑一次）：{e}", file=sys.stderr)
            return 3
        if note:
            print(f"[labmon] {note}")
    if port:
        st["port"] = port
    if public_port is not None:
        st["public_port"] = public_port or None
    if interval:
        st["interval"] = interval
    _write_and_restart(st)
    print(f"[labmon] 服务已（重新）启动：{len(project_ids())} 个项目，{_entries(st)}；日志 {LOG}")
    return 0


def restart() -> int:
    r = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}") if MANAGER == "launchd" else _systemctl("restart", UNIT)
    if r.returncode != 0:
        print(f"[labmon] 重启失败：{r.stderr.strip() or '服务没装？'}")
        return 1
    print("[labmon] 服务已重启")
    return 0


def restart_later(delay: int = 2) -> None:
    """过 delay 秒由服务管理重启看板服务（部署接口用，#164）。服务自己调 systemctl restart，停自己时会把调用者一起停掉，
    所以 Linux 上交给 systemd-run 起的临时单元；macOS 起一个脱离本进程组的 sleep + launchctl kickstart -k（launchd 停服务只杀它的进程组）。
    失败抛 OSError。"""
    if MANAGER == "systemd":
        r = subprocess.run(["systemd-run", "--user", f"--on-active={delay}", "--collect", f"--unit=labmon-restart-{int(time.time())}",
                            "systemctl", "--user", "restart", UNIT], capture_output=True, text=True)
        if r.returncode:
            raise OSError(f"systemd-run 失败：{r.stderr.strip()[:200]}")
        return
    subprocess.Popen(["/bin/sh", "-c", f"sleep {delay}; launchctl kickstart -k {_domain()}/{LABEL}"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop() -> int:
    """停服务、删服务管理的配置。服务停时自己释放服务租约（serve 的 SIGTERM）；本机标记置为备用（#163）：
    停下的这台以后再起、又读不到租约时不自认是主（读得到时照租约定）。"""
    from . import lease
    if MANAGER == "launchd":
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        PLIST.unlink(missing_ok=True)
    else:
        _systemctl("disable", "--now", UNIT)
        UNIT_FILE.unlink(missing_ok=True)
        _systemctl("daemon-reload")
    if lease.known():
        lease.set_marker(False, "labmon service stop")
    print(f"[labmon] 服务已停止，{MANAGER} 的配置已删除")
    return 0


def _state() -> tuple[str, str, str, bool]:
    """服务管理眼里的状态：(状态, PID, 上次退出码, 在跑)。"""
    if MANAGER == "launchd":
        info = {}
        for line in _launchctl("print", f"{_domain()}/{LABEL}").stdout.splitlines():
            k, _, v = line.strip().partition(" = ")
            if k in ("state", "pid", "last exit code") and k not in info:
                info[k] = v
        return info.get("state", "未加载"), info.get("pid", "—"), info.get("last exit code", "—"), info.get("state") == "running"
    out = _systemctl("show", UNIT, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "ExecMainStatus").stdout
    info = dict(x.partition("=")[::2] for x in out.splitlines())
    act = info.get("ActiveState") or "未加载"
    return f"{act}（{info.get('SubState') or '—'}）", info.get("MainPID") or "—", info.get("ExecMainStatus") or "—", act == "active"


def status() -> int:
    if not installed():
        print("[labmon] 没有安装服务（labmon service install）")
        return 1
    st = _read()
    state, pid, code, up = _state()
    print(f"[labmon] 状态：{state}  PID {pid}  上次退出码 {code}（{MANAGER}）")
    print(f"  {_entries(st)}" + (f"，采集间隔 {st['interval']} 秒" if st["interval"] else "") + f"；日志 {LOG}")
    if MANAGER == "systemd" and (hint := linger_hint()):
        print(f"  {hint}")
    from .projects import archived, cfg_file, registered
    gone = archived()
    for pid in registered():
        print(f"  项目 {pid}：{cfg_file(pid)}" + ("（已归档：看板不采集，labmon project restore 加回）" if pid in gone else ""))
    if st["legacy"]:                                  # 可删（#161 迁移）
        print("  托管配置里还带着以前的 -c：跑一次 labmon service install 去掉（那几个项目看板服务启动时已登记进来）")
    print(f"  本机打开 http://127.0.0.1:{st['port']}/（要登录；还没有账号先 labmon user person <人员名>，再 labmon user add <账号> --person <人员名> --admin）")
    return 0 if up else 1
