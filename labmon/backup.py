"""冷备与恢复（#163，#157 J 节）：看板服务那台的状态打一个包；笔记本每天经 API 取一份、留 KEEP 份；需要时在一台机器上恢复成看板服务。

    labmon backup make [--out 文件]                      在看板服务这台本机打包（缺省写到当前目录）
    labmon backup pull --to 看板地址 [--ca 证书] [--dir 目录]   经 GET /api/admin/backup 取一份（令牌 ~/.config/labmon/backup.token，LABMON_TOKEN 优先）；
                                                         连不上就跳过、退出码 0（笔记本常不在内网）；留最近 KEEP 份
    labmon backup schedule --to … [--ca …] [--at 12:30]  每天定时 pull 一次（macOS launchd 任务，Linux systemd 定时器）；--off 去掉
    labmon backup list [--dir 目录]                       列出存着的备份
    labmon server restore <备份>                           恢复成这台的看板服务（restore；服务装着的先停）

包里有：状态根（state.state_root）下的全部，去掉 SKIP 那几样；主机清单 hosts.toml；manifest.json（何时、哪台、哪个提交、服务里的项目配置）。
不含秘密：平台密码与登录令牌、推送配置 notify.toml、ssh 私钥、证书私钥都不在状态根里（各在 ~/.config/labmon、钥匙串、~/.ssh），账号库 users.json 只有哈希；
本机身份 server.json（服务租约，lease.py）每台各一份，也不进包；主机清单的 [server] 段（入口绑哪、证书、认哪些 Host）也是每台各一份，恢复时留这台原来的（#197）。状态文件都是整体替换，打包时服务在写也只会读到新的或旧的；JSONL 最后半行读时跳过。
"""
from __future__ import annotations

import io
import json
import os
import plistlib
import re
import shutil
import socket
import ssl
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import deploy as DP
from . import lease as LS
from .client import opener
from .config import ConfigError, fmt_time, hosts_file, keep_section, pinned
from .state import state_root

KEEP = 14                                    # 笔记本上留几份（每天一份，两周）
TIMEOUT = 120
NAME = "labmon-backup-{}.tar.gz"
NAME_RE = re.compile(r"labmon-backup-\d{8}-\d{6}\.tar\.gz")
SKIP = {"server.json"}                       # 本机身份与主备（lease.py），每台各一份
MINE = "[server]"                            # 主机清单里每台各一份的段（入口绑哪、证书、认哪些 Host，#197）：恢复时留这台原来的
TOKEN = Path("~/.config/labmon/backup.token")
LABEL = "com.labmon.backup"
PLIST = Path(f"~/Library/LaunchAgents/{LABEL}.plist").expanduser()
UNIT = "labmon-backup"
UNIT_DIR = Path("~/.config/systemd/user").expanduser()


def _skip(p: Path) -> bool:
    """不进包的：本机身份、服务日志（Linux 上在状态根下）、锁与临时文件、套接字之类不是普通文件的。"""
    n = p.name
    return n in SKIP or n.startswith("labmon.log") or n.endswith((".lock", ".tmp")) or not p.is_file() or p.is_symlink()


def _manifest(t: float, files: int) -> dict:
    return {"t": round(t), "host": socket.gethostname(), "id": LS.ident(), "commit": DP.RUNNING.get("commit") or DP.head(),
            "files": files, "state_root": str(state_root()), "hosts_file": str(hosts_file())}


def _write(fh, t: float) -> int:
    """把包写进 fh：state/… 是状态根，hosts.toml 是主机清单，最后 manifest.json。返回进了几个文件。"""
    root, n = state_root(), 0
    with tarfile.open(fileobj=fh, mode="w:gz") as tf:
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if _skip(p):
                    continue
                n += _add(tf, p, f"state/{p.relative_to(root).as_posix()}")
        hf = hosts_file()
        if hf.is_file():
            _add(tf, hf, "hosts.toml")
        m = json.dumps(_manifest(t, n), ensure_ascii=False, indent=1).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime = len(m), int(t)
        tf.addfile(info, io.BytesIO(m))
    return n


def _create(path: Path, mode: int = 0o600):
    """新建（或截断）一个文件写二进制，一建出来就是 mode（不先以 umask 的缺省权限落盘再改）：备份包里有账号库的哈希，只给自己读。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    os.fchmod(fd, mode)                              # 文件以前就有时 os.open 不改它的权限
    return os.fdopen(fd, "wb")


def _add(tf: tarfile.TarFile, path: Path, arcname: str) -> int:
    """打进一个文件：先打开，大小、权限与内容都取这一个打开的文件（服务在写时 save_json 整体替换：拿到的是替换前或替换后的一整份，
    不会头是旧的、内容是新的）。打包时刚被删掉（修剪、换名）的跳过，返回 0。"""
    try:
        with open(path, "rb") as f:
            ti = tf.gettarinfo(arcname=arcname, fileobj=f)
            tf.addfile(ti, f)
    except OSError:
        return 0
    return 1


def name_of(t: float) -> str:
    return NAME.format(time.strftime("%Y%m%d-%H%M%S", time.localtime(t)))


def make_bytes(t: float | None = None) -> tuple[str, bytes]:
    """GET /api/admin/backup：(文件名, .tar.gz 内容)。"""
    t = time.time() if t is None else t
    buf = io.BytesIO()
    _write(buf, t)
    return name_of(t), buf.getvalue()


def make(out: str | None = None, t: float | None = None) -> Path:
    """labmon backup make：写到 out（目录或文件名；缺省当前目录），先写临时文件再改名。"""
    t = time.time() if t is None else t
    dest = Path(out or ".").expanduser()
    if dest.is_dir():
        dest = dest / name_of(t)
    tmp = dest.with_name(dest.name + ".tmp")
    with _create(tmp) as fh:
        _write(fh, t)
    os.replace(tmp, dest)
    return dest


def check(path: Path) -> dict:
    """是不是一份能用的备份：返回 manifest；不是就报 ConfigError。"""
    try:
        with tarfile.open(path, "r:gz") as tf:
            m = tf.extractfile("manifest.json")
            return json.loads(m.read()) if m else {}
    except (OSError, tarfile.TarError, KeyError, ValueError) as e:
        raise ConfigError(f"{path} 不是 labmon 的备份（{e}）")


# ---------------- 笔记本这边：取、留、定时 ----------------
def backup_dir() -> Path:
    return Path(os.environ.get("LABMON_BACKUPS") or ("~/Library/Application Support/labmon/backups" if sys.platform == "darwin"
                                                     else "~/.local/share/labmon/backups")).expanduser()


def token() -> str:
    raw = os.environ.get("LABMON_TOKEN") or (TOKEN.expanduser().read_text().strip() if TOKEN.expanduser().is_file() else "")
    if not raw:
        raise ConfigError(f"没有令牌：在看板服务那台 labmon user token <名字> --scope backup --out 文件，把文件拷到这台的 {TOKEN}（600 权限）")
    return raw


def listing(d: Path | None = None) -> list[Path]:
    d = d or backup_dir()
    return sorted(p for p in d.glob("labmon-backup-*.tar.gz") if NAME_RE.fullmatch(p.name)) if d.is_dir() else []


def prune(d: Path, keep: int = KEEP) -> list[Path]:
    """只留最近 keep 份（按文件名里的时刻），返回删了的。"""
    old = listing(d)[:-keep] if keep else listing(d)
    for p in old:
        p.unlink(missing_ok=True)
    return old


def pull(to: str, ca: str | None = None, d: Path | None = None, keep: int = KEEP) -> tuple[int, str]:
    """labmon backup pull：经 API 取一份存进 d、修剪到 keep 份。返回 (退出码, 一句话)：0 取到了或连不上（跳过），3 令牌或服务端拒绝、包坏了。"""
    d = d or backup_dir()
    try:
        tok = token()
    except ConfigError as e:
        return 3, str(e)
    url = to.rstrip("/") + "/api/admin/backup"
    ctx = pinned(str(Path(ca).expanduser())) if ca else None
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = d / ".pull.tmp"
    try:
        with opener(ctx).open(req, timeout=TIMEOUT) as r, _create(tmp) as fh:      # 直连、不走环境代理（#195）
            shutil.copyfileobj(r, fh)
            cd = r.headers.get("Content-Disposition") or ""
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or e.reason
        except ValueError:
            why = e.reason
        tmp.unlink(missing_ok=True)
        return 3, f"看板服务拒绝了（{e.code}）：{why}"
    except (urllib.error.URLError, OSError, ssl.SSLError) as e:
        tmp.unlink(missing_ok=True)
        return 0, f"连不上 {to}（{getattr(e, 'reason', e)}），这次跳过"
    m = re.search(r'filename="([^"]+)"', cd)
    name = m.group(1) if m and NAME_RE.fullmatch(m.group(1)) else name_of(time.time())
    try:
        man = check(tmp)
    except ConfigError as e:
        tmp.unlink(missing_ok=True)
        return 3, str(e)
    dest = d / name
    os.replace(tmp, dest)
    gone = prune(d, keep)
    return 0, (f"取到 {dest}（{dest.stat().st_size // 1024} KiB，{man.get('host')} 上 {fmt_time(man.get('t'))} 打的，{man.get('files')} 个文件）"
               + (f"；删了更早的 {len(gone)} 份，留 {keep} 份" if gone else ""))


def _pull_args(to: str, ca: str | None) -> list[str]:
    return [sys.executable, "-m", "labmon", "backup", "pull", "--to", to] + (["--ca", str(Path(ca).expanduser().resolve())] if ca else [])


def schedule(to: str | None, ca: str | None, at: str = "12:30", off: bool = False) -> str:
    """labmon backup schedule：每天 at 定时 pull 一次（错过的醒来后补：launchd 的日历任务、systemd 的 Persistent=true）；off 去掉。"""
    from .service import _launchctl, _domain, _sd_arg, _systemctl
    hh, mm = (int(x) for x in at.split(":"))
    log = Path("~/Library/Logs/labmon-backup.log").expanduser() if sys.platform == "darwin" else backup_dir() / "pull.log"
    if sys.platform == "darwin":
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        if off:
            PLIST.unlink(missing_ok=True)
            return "去掉了每天的备份"
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps({
            "Label": LABEL, "ProgramArguments": _pull_args(to, ca), "WorkingDirectory": str(Path.home()),
            "StartCalendarInterval": {"Hour": hh, "Minute": mm},
            "StandardOutPath": str(log), "StandardErrorPath": str(log),
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"}}))
        r = _launchctl("bootstrap", _domain(), str(PLIST))
        if r.returncode:
            raise ConfigError(f"launchctl bootstrap 失败：{r.stderr.strip()}（{PLIST}）")
        return f"每天 {at} 从 {to} 取一份备份（{PLIST}；日志 {log}）"
    svc, tmr = UNIT_DIR / f"{UNIT}.service", UNIT_DIR / f"{UNIT}.timer"
    if off:
        _systemctl("disable", "--now", f"{UNIT}.timer")
        svc.unlink(missing_ok=True)
        tmr.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        return "去掉了每天的备份"
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    svc.write_text(f"[Unit]\nDescription=labmon 冷备\n\n[Service]\nType=oneshot\nExecStart={' '.join(map(_sd_arg, _pull_args(to, ca)))}\n"
                   f"StandardOutput=append:{log}\nStandardError=append:{log}\n")
    tmr.write_text(f"[Unit]\nDescription=labmon 冷备（每天）\n\n[Timer]\nOnCalendar=*-*-* {hh:02d}:{mm:02d}:00\nPersistent=true\n\n"
                   "[Install]\nWantedBy=timers.target\n")
    for step in (("daemon-reload",), ("enable", "--now", f"{UNIT}.timer")):
        r = _systemctl(*step)
        if r.returncode:
            raise ConfigError(f"systemctl --user {' '.join(step)} 失败：{r.stderr.strip()}")
    return f"每天 {at} 从 {to} 取一份备份（{tmr}；日志 {log}）"


# ---------------- 恢复 ----------------
def restore(archive: str, t: float | None = None) -> str:
    """labmon server restore：把备份恢复成这台的状态根与主机清单。服务装着的拒绝（先 labmon service stop）；
    已有的整个挪到旁边 <原名>.before-restore-<时刻>（不删）；文件照包里的权限建（账号库 600）；server.json 保留这台原来的身份，
    主机清单的 [server] 段留这台原来的（这台没有就不带备份里的，那是打包那台的入口；合不出能解析的就在挪动任何东西之前停下，#197），
    本机标记置为备用（lease.set_marker）：起来后读到租约空了、释放了或过了 ttl 才接管，原来的主还在续约就以备用待着；
    读不到租约（没有在用主机、不在内网）时一直是备用，确认原来那台已经停了再 labmon server lease --take。"""
    from .service import installed
    t = time.time() if t is None else t
    src = Path(archive).expanduser()
    if installed():
        raise ConfigError("这台已经装着看板服务：先 labmon service stop 再恢复（恢复会把现在的状态挪到旁边）")
    man = check(src)
    root, hf = state_root(), hosts_file()
    me = LS.ident()                                  # 先取这台的身份（没有就生成），恢复后写回去
    keep = (root / "server.json").read_bytes() if (root / "server.json").is_file() else None
    mine = hf.read_text() if hf.is_file() else ""
    with tarfile.open(src, "r:gz") as tf:            # 主机清单先合好（这台的 [server] 换进去）：合不出来就在挪动任何东西之前停下
        m = next((m for m in tf.getmembers() if m.isfile() and m.name == "hosts.toml"), None)
        hosts = tf.extractfile(m).read().decode() if m else None
    if hosts is not None:
        hosts = keep_section(hosts, mine, MINE)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t))
    moved = []
    for p in (root, hf):
        if p.exists():
            to = p.with_name(f"{p.name}.before-restore-{stamp}")
            p.rename(to)
            moved.append(str(to))
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(src, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or ".." in Path(m.name).parts or m.name.startswith("/"):
                continue
            if m.name == "hosts.toml":
                dest = hf
            elif m.name.startswith("state/"):
                dest = root / m.name[len("state/"):]
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tf.extractfile(m) as fh, _create(dest, (m.mode & 0o777) or 0o600) as out:
                if dest == hf:
                    out.write(hosts.encode())
                else:
                    shutil.copyfileobj(fh, out)
            os.utime(dest, (m.mtime, m.mtime))
    if keep is not None:
        (root / "server.json").write_bytes(keep)
    LS.set_marker(False, f"从备份恢复（{src.name}，{man.get('host')} 上 {fmt_time(man.get('t'))} 打的）")
    had = hosts is not None and MINE.strip("[]") in tomllib.loads(hosts)
    return (f"恢复了 {src.name}（{man.get('host')} 上 {fmt_time(man.get('t'))} 打的，{man.get('files')} 个文件）；这台是 {me}"
            + (f"；主机清单的 {MINE} 留的是这台原来的" if had else f"；这台原来没有 {MINE}，备份里的没带进来（那是打包那台的入口）")
            + (f"；原来的挪到了 {'、'.join(moved)}" if moved else ""))


def install_service(port: int | None, public_port: int | None) -> int:
    """恢复后装服务：项目的登记与配置在状态根的 projects/ 下（#161），已随备份回来，不用再给 -c。起来后先是备用（restore 置的标记）。"""
    from .service import install               # service 在 notify 里延迟 import lease；这里同样延迟，免得循环
    rc = install(port, None, public_port)
    if rc == 0:
        print("[labmon] 起来后先是备用，读到租约空了才接管；原来那台确定停了、读不到租约时 labmon server lease --take。")
    return rc


def cli(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="labmon backup", description="冷备：看板服务那台打包，笔记本经 API 每天取一份（不含秘密）")
    sub = ap.add_subparsers(dest="act", required=True)
    p = sub.add_parser("make", help="在看板服务这台本机打包")
    p.add_argument("--out", help="写到哪（目录或文件名；缺省当前目录）")
    for name, h in (("pull", "经 GET /api/admin/backup 取一份，留最近 14 份"), ("schedule", "每天定时 pull 一次")):
        p = sub.add_parser(name, help=h)
        p.add_argument("--to", required=name == "pull", help="看板地址，如 https://dashboard.example:8765（内网入口）")
        p.add_argument("--ca", help="只信这张证书（内网入口用 labmon 自己的 CA 签的证书时）")
        if name == "pull":
            p.add_argument("--dir", help=f"存到哪（缺省 {backup_dir()}）")
        else:
            p.add_argument("--at", default="12:30", help="每天几点（HH:MM，缺省 12:30；电脑睡着错过的醒来后补）")
            p.add_argument("--off", action="store_true", help="去掉定时")
    p = sub.add_parser("list", help="列出存着的备份")
    p.add_argument("--dir", help=f"哪个目录（缺省 {backup_dir()}）")
    a = ap.parse_args(argv)
    try:
        if a.act == "make":
            out = make(a.out)
            print(f"[labmon] 备份写到 {out}（{out.stat().st_size // 1024} KiB）")
        elif a.act == "pull":
            rc, msg = pull(a.to, a.ca, Path(a.dir).expanduser() if a.dir else None)
            print(f"[labmon] {fmt_time(time.time())} {msg}", file=sys.stderr if rc else sys.stdout)
            return rc
        elif a.act == "schedule":
            if not a.off and not a.to:
                ap.error("schedule 需要 --to（或 --off）")
            if not a.off and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", a.at):
                ap.error("--at 写成 HH:MM")
            print(f"[labmon] {schedule(a.to, a.ca, a.at, a.off)}")
        else:
            rows = listing(Path(a.dir).expanduser() if a.dir else None)
            for p in rows:
                print(f"  {p}  {p.stat().st_size // 1024} KiB")
            print(f"[labmon] {len(rows)} 份" + ("" if rows else f"（{Path(a.dir).expanduser() if a.dir else backup_dir()} 下没有）"))
    except ConfigError as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 3
    return 0
