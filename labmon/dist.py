"""客户端分发（#167）：使用者从看板下载命令行，版本与当前看板服务保持一致。

    GET /api/client/install.sh?base=<看板根地址>   一个 sh 脚本，嵌着 wheel、填好地址的 client.toml 与（[server] 配了 ca_cert 时）CA 证书：
                                                  sh labmon-install.sh 一条命令装好（用独立虚拟环境安装，不连 PyPI）
    GET /api/client/wheel                          只要 wheel（labmon update 用）

- wheel 只用 main 上已提交的文件（workerctl.DEPLOY_REF，同部署到服务器上的 worker）：git archive 取本包目录，标准库 zipfile 拼成纯 Python 的 wheel，
  不要 setuptools；版本 <pyproject 的版本>+g<提交前 7 位>，另放 labmon/_build.py（COMMIT 是完整提交号）。按提交缓存在内存里。
- 客户端的版本（version）：装的 wheel 里 _build.COMMIT；从仓库直接跑的是那个 checkout 的 HEAD。命令行每个请求带 X-Labmon-Client: <version>（client.HEADER），
  给 #199（看板认最低客户端版本、wait 自动更新）用；看板的版本是 /api/me 的 commit（在跑的代码）。
"""
from __future__ import annotations

import base64
import fcntl
import functools
import hashlib
import io
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import COMMIT_RE
from .workerctl import DEPLOY_REF

HERE = Path(__file__).resolve().parent
NAME = "labmon"
_CACHE: dict = {}
_LOCK = threading.Lock()


class Unavailable(Exception):
    """取不到 main 上的代码（不是 git checkout、git 出错）：看板回 503 与这句。"""


def installed() -> bool:
    """这份命令行是不是看板发的 wheel 装的（有 _build.py）；仓库里直接跑的不是（更新靠 git pull，不用 labmon update）。"""
    return (HERE / "_build.py").is_file()


@functools.cache
def version() -> str:
    """这份命令行是哪个提交：wheel 装的取 _build.COMMIT；仓库里直接跑的取 HEAD；都没有是 dev。一个进程里只算一次。"""
    try:
        from . import _build      # type: ignore[attr-defined]  # 只在看板发的 wheel 里有
        return str(_build.COMMIT)
    except ImportError:
        pass
    try:
        r = subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "dev"
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "dev"


def short(c: str | None) -> str:
    return (c or "?")[:7]


def _git(*args: str, binary: bool = False, cwd: str | None = None):
    try:
        r = subprocess.run(["git", "-C", cwd or str(HERE), *args], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unavailable(f"读不了代码：{e}") from e
    if r.returncode:
        raise Unavailable(f"git {args[0]} 失败：{r.stderr.decode(errors='replace').strip()[-200:] or r.returncode}")
    return r.stdout if binary else r.stdout.decode().strip()


@functools.lru_cache(maxsize=4)
def history(commit: str) -> frozenset[str]:
    """运行提交的祖先（含自身）。部署更新分支指针时，正在运行的提交历史不变。"""
    if not COMMIT_RE.fullmatch(commit):
        return frozenset()
    return frozenset(_git("rev-list", commit).splitlines())


@functools.lru_cache(maxsize=4)
def supported_history(running: str, minimum: str) -> frozenset[str]:
    """最低版本与从它走到运行版本的提交。每个部署版本对只读一次 Git。"""
    if minimum not in history(running):
        return frozenset()
    return frozenset(_git("rev-list", "--ancestry-path", f"{minimum}..{running}").splitlines()) | {minimum}


def client_status(commit: str | None, running: str | None, minimum: str | None) -> str | None:
    """令牌版本的状态与最低版本门槛。未知或较新的提交不会触发自动换包。"""
    if not commit or not running:
        return None
    try:
        if commit not in history(running):
            return "unknown"
        if minimum and commit not in supported_history(running, minimum):
            return "below_minimum"
        return "current" if commit == running else "outdated"
    except Unavailable:
        return "unknown"


def _dist_version(commit: str) -> str:
    try:
        ver = tomllib.loads(_git("show", f"{commit}:pyproject.toml"))["project"]["version"]
    except (KeyError, tomllib.TOMLDecodeError) as e:
        raise Unavailable("提交里的 pyproject.toml 没有项目版本") from e
    if not isinstance(ver, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", ver):
        raise Unavailable("提交里的项目版本格式不对")
    return f"{ver}+g{commit[:7]}"


def wheel_name_ok(name: str, commit: str) -> bool:
    return bool(COMMIT_RE.fullmatch(commit) and re.fullmatch(
        rf"{NAME}-[0-9]+(?:\.[0-9]+)+\+g{commit[:7]}-py3-none-any\.whl", name
    ))


def _hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def wheel(commit: str | None) -> tuple[str, bytes, str]:
    """从服务启动的同一个已提交版本打包。该提交必须在 main 上。"""
    if not commit or not COMMIT_RE.fullmatch(commit):
        raise Unavailable("看板没有已提交的运行版本")
    with _LOCK:
        if _CACHE.get("commit") == commit:
            return _CACHE["name"], _CACHE["data"], commit
        try:
            _git("merge-base", "--is-ancestor", commit, DEPLOY_REF)
        except Unavailable as e:
            raise Unavailable("看板的运行版本不在 main 上") from e
        prefix = _git("rev-parse", "--show-prefix")
        top = _git("rev-parse", "--show-toplevel")
        tar = _git("archive", "--format=tar", f"{commit}:{prefix}", binary=True, cwd=top)
        ver = _dist_version(commit)
        di = f"{NAME}-{ver}.dist-info"
        files: list[tuple[str, bytes]] = []
        with tarfile.open(fileobj=io.BytesIO(tar)) as tf:
            for m in tf.getmembers():
                if m.isfile() and "__pycache__" not in m.name:
                    files.append((f"{NAME}/{m.name.removeprefix('./')}", tf.extractfile(m).read()))
        files.append((f"{NAME}/_build.py", f'"""看板发的客户端是哪个提交（dist.wheel 写的）。"""\nCOMMIT = "{commit}"\n'.encode()))
        files += [(f"{di}/METADATA", f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {ver}\nSummary: labmon 命令行（看板发的）\nRequires-Python: >=3.11\n".encode()),
                  (f"{di}/WHEEL", b"Wheel-Version: 1.0\nGenerator: labmon.dist\nRoot-Is-Purelib: true\nTag: py3-none-any\n"),
                  (f"{di}/entry_points.txt", f"[console_scripts]\n{NAME} = {NAME}.__main__:main\n".encode())]
        rec = "".join(f"{n},{_hash(d)},{len(d)}\n" for n, d in files) + f"{di}/RECORD,,\n"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n, d in files + [(f"{di}/RECORD", rec.encode())]:
                z.writestr(zipfile.ZipInfo(n, (1980, 1, 1, 0, 0, 0)), d, zipfile.ZIP_DEFLATED)
        name = f"{NAME}-{ver}-py3-none-any.whl"
        _CACHE.update(commit=commit, name=name, data=buf.getvalue())
        return name, _CACHE["data"], commit


BASE_RE = re.compile(r"https?://(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]+)?(?:/[a-zA-Z0-9_.~/-]*)?")
CERT_RE = re.compile(rb"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----")


def parse_base(base: str) -> tuple[str, str] | None:
    """安装配置只接受能写进 TOML 的根地址，具体入口与 Host 由看板核对。"""
    if not BASE_RE.fullmatch(base):
        return None
    try:
        url = urlsplit(base)
        if not url.hostname or url.port == 0 or {".", ".."} & set(url.path.split("/")):
            return None
    except ValueError:
        return None
    return url.scheme, url.netloc


def _ca_name(pem: bytes) -> str:
    """只附可读的公钥证书。文件名按内容定，避免覆盖已有配置正在用的证书。"""
    if CERT_RE.sub(b"", pem).strip() or not CERT_RE.search(pem):
        raise Unavailable("[server] ca_cert 要是 PEM 公钥证书")
    try:
        ssl.create_default_context(cadata=pem.decode("ascii"))
    except (ssl.SSLError, UnicodeError) as e:
        raise Unavailable("[server] ca_cert 读不了 PEM 公钥证书") from e
    return f"labmon-ca-{hashlib.sha256(pem).hexdigest()[:16]}.crt"


def client_toml(base: str, ca_name: str | None, public: bool, t: float) -> str:
    """按下载时用的入口生成 client.toml。"""
    lines = [f"# labmon 命令行连哪个看板（{base} 发的，{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}）。有别的入口可再加一节 [[servers]]，内网写在前面。",
             "[[servers]]", f'url = "{base.rstrip("/")}"']
    if public:
        lines += ['# cert = "~/.config/labmon/ca/<设备名>.crt"   # 公网入口的客户端证书由管理员签发',
                  '# key = "~/.config/labmon/ca/<设备名>.key"']
    elif base.startswith("https://"):
        if ca_name:
            lines.append(f'ca = "~/.config/labmon/ca/{ca_name}"')
        else:
            lines.append('# ca = "~/.config/labmon/ca/ca.crt"   # 私有 CA：向看板管理员领取签发内网入口证书的 CA 公钥后启用')
    return "\n".join(lines) + "\n"


def _heredoc(tag: str, data: bytes) -> str:
    b = base64.b64encode(data).decode()
    return "\n".join(b[i:i + 76] for i in range(0, len(b), 76)) + f"\n{tag}\n"


def install_wheel(wheel_path: Path, commit: str) -> tuple[Path, Path]:
    """把 wheel 装进独立环境，验证通过后原子切换命令。返回固定入口及可选的 PATH 入口。"""
    wheel_path = wheel_path.resolve()
    if not wheel_name_ok(wheel_path.name, commit):
        raise Unavailable("安装包文件名或版本不对")
    home = Path.home()
    root = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share") / NAME
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    fixed = root / "bin" / NAME
    short_path = home / ".local/bin" / NAME
    fixed.parent.mkdir(parents=True, exist_ok=True)
    short_path.parent.mkdir(parents=True, exist_ok=True)
    with (root / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if fixed.exists() and not fixed.is_symlink():
            raise Unavailable(f"命令入口 {fixed} 已被占用，请先移开原文件")
        venv = versions / commit
        ready = venv / "ready"
        python = venv / "bin/python"
        cmd = venv / "bin" / NAME
        if not (ready.is_file() and python.is_file() and cmd.is_file()):
            if venv.exists():
                shutil.rmtree(venv)
            try:
                subprocess.run([sys.executable, "-Im", "venv", str(venv)], check=True)
                subprocess.run([str(python), "-Im", "pip", "--isolated", "install", "--no-deps", "--no-index", str(wheel_path)], check=True)
                actual = subprocess.check_output(
                    [str(python), "-Ic", "from labmon._build import COMMIT; print(COMMIT)"], text=True
                ).strip()
                if actual != commit or not cmd.is_file():
                    raise Unavailable("安装后的客户端版本不对")
                ready.touch()
            except (OSError, subprocess.SubprocessError, Unavailable):
                shutil.rmtree(venv, ignore_errors=True)
                raise
        pending = fixed.with_name(".labmon.next")
        pending.unlink(missing_ok=True)
        pending.symlink_to(cmd)
        pending.replace(fixed)
        try:
            short_path.symlink_to(fixed)
        except FileExistsError:
            pass
    return fixed, short_path


def install_sh(base: str, ca_pem: bytes | None, public: bool, commit: str | None, *, t: float | None = None) -> tuple[str, str]:
    """生成离线安装脚本。命令行装进独立虚拟环境，已有配置另存为 client.toml.new。"""
    if not parse_base(base) or (public and not base.startswith("https://")):
        raise Unavailable("看板的根地址不对")
    t = time.time() if t is None else t
    ca_pem = ca_pem if base.startswith("https://") and not public else None
    ca_name = _ca_name(ca_pem) if ca_pem is not None else None
    needs_ca_help = base.startswith("https://") and not public and ca_name is None
    name, data, commit = wheel(commit)
    toml = client_toml(base, ca_name, public, t)
    dec = "\"$PY\" -c 'import base64,sys; open(sys.argv[1],\"wb\").write(base64.b64decode(sys.stdin.read()))'"
    sh = f"""#!/bin/sh
# labmon 命令行安装脚本：看板 {base} 发的，提交 {short(commit)}。
set -eu
umask 077
PY="${{PYTHON:-python3}}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "[labmon] 需要 Python 3.11 或更新版本。可用 PYTHON=/路径/python3 sh $0 指定。" >&2
  exit 1
fi
D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
{dec} "$D/{name}" <<'LABMON_WHEEL'
{_heredoc("LABMON_WHEEL", data)}cat > "$D/client.toml" <<'LABMON_TOML'
{toml}LABMON_TOML
"""
    if ca_name:
        sh += f"""{dec} "$D/{ca_name}" <<'LABMON_CA'
{_heredoc("LABMON_CA", ca_pem)}"""
    sh += f"""\"$PY\" -I - "$D/{name}" "{commit}" "{ca_name or ''}" <<'LABMON_INSTALL'
import fcntl
import subprocess
import sys
from pathlib import Path

wheel = Path(sys.argv[1])
sys.path.insert(0, str(wheel))
from labmon.dist import Unavailable, install_wheel
from labmon.state import write_atomic

try:
    fixed, path_entry = install_wheel(wheel, sys.argv[2])
    c = Path.home() / ".config/labmon"
    c.mkdir(parents=True, exist_ok=True)
    with (c / ".client-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ca = sys.argv[3]
        if ca:
            write_atomic(c / "ca" / ca, (wheel.parent / ca).read_text(encoding="utf-8"), mode=0o600)
        target = c / "client.toml"
        if target.exists() or target.is_symlink():
            target = c / "client.toml.new"
            print(f"[labmon] 已有 client.toml。这次的配置写在 {{target}}，可对照着合并。")
        write_atomic(target, (wheel.parent / "client.toml").read_text(encoding="utf-8"), mode=0o600)
        if {needs_ca_help!r}:
            print("[labmon] 此内网安装包未附 CA。若内网入口使用私有 CA，请向看板管理员领取签发入口证书的 CA 公钥（ca.crt）。")
            print(f"[labmon] 将公钥保存到 {{c / 'ca' / 'ca.crt'}}，取消 {{target}} 中 ca 那行的注释。")
            if target.name == "client.toml.new":
                print(f"[labmon] 把对应入口的 ca 设置合并到 {{c / 'client.toml'}}，再运行 labmon login。")
except (OSError, subprocess.SubprocessError, Unavailable) as exc:
    sys.exit(f"[labmon] 安装失败：{{exc}}")
if not path_entry.is_symlink() or path_entry.readlink() != fixed:
    print(f"[labmon] {{path_entry}} 已有其他安装。请直接运行 {{fixed}}，或移开旧命令后重新安装。")
else:
    print(f"[labmon] 已安装 {{path_entry}}。若找不到命令，把 {{path_entry.parent}} 加到 PATH 前面。")
LABMON_INSTALL
echo "[labmon] 安装完成（提交 {short(commit)}）。下一步：labmon login。"
"""
    return "labmon-install.sh", sh
