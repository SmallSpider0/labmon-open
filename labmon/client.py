"""看板接口的客户端（#162）：命令行里会动东西的命令（wait、check、collect、worker、pause 等、project、borrow、host）都经 call 发到看板服务，
不再读写本机状态、不直接写主机清单、不直接 ssh。只用标准库。

    ~/.config/labmon/client.toml（LABMON_CLIENT 可改）    [[servers]] 依次试：url（内网入口在前；公网入口带 TX 上的路径前缀），可选 ca（内网入口的证书，钉住）、
                                                         cert + key（TX 要的客户端证书）。没有这个文件时连本机的内网入口（config.lan_url：看板服务就在这台时不用写）
    client.token（client.toml 旁边，600）                  命令行的令牌（labmon login 建的 project 令牌）；环境变量 LABMON_TOKEN 优先
    admin.token（同上）                                    labmon login --admin 建的 1 小时 admin 令牌：要管理员的命令（borrow、host、drain、worker start|stop|limit）先用它

重试：做两遍也没事的请求（读、长轮询）连不上（含 502/503/504）就退避重试（1、2、4…最多 15 秒一次），直到 patience 秒；会改东西的只在请求根本没送到
（连接被拒、找不到主机、502/503）时重试，回话断在半路的不重试、照实报“不知道做成没有”。备用的看板服务（#163）回 409 standby：没做，接着试下一个地址。
几个地址都不通抛 Unreachable。
令牌只放在 Authorization 头里，不打印、不进地址。
每个请求带 X-Labmon-Client: <这份命令行的提交>（HEADER，dist.version，#167）：看板据此说“该 labmon update 了”，#199 的最低客户端版本也按它认。
labmon update（update）：从看板取与它同一版本的 wheel 装上（GET /api/client/wheel，dist.py）。
"""
from __future__ import annotations

import getpass
import http.client
import http.cookiejar
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from . import config
from . import dist
from .state import write_atomic

PATIENCE = 15        # 一次性命令连不上最多再试这么久（秒），之后退出码 2
BACKOFF_MAX = 15     # 退避最长一次
NOT_SENT = (502, 503)          # 网关说后端不在、服务说忙：请求没进到看板的处理，会改东西的也能重试
RETRY = (502, 503, 504)        # 做两遍也没事的请求：这几个都重试
ADMIN_HINT = "这类事要管理员在内网入口做（看板页面上，或 labmon login --admin 后再跑）"
HEADER = config.CLIENT_HEADER

CLIENT_OUTDATED = 426
CLIENT_OUTDATED_RC = 5
CLIENT_OUTDATED_REASON = "client_outdated"


def login_hint(admin: bool = False) -> str:
    """看板回 401（没带令牌、令牌不认）时接在原因后面的那句：下一步谁做、跑哪条（#191）。经 call 进 Failed.msg，命令行各处打原因时都带上。
    admin：要管理员的命令（先用 admin.token，1 小时过期），指到 labmon login --admin。"""
    if os.environ.get("LABMON_TOKEN"):
        return "环境变量 LABMON_TOKEN 里的令牌看板不认：换一个有效的，或去掉它改用 " + str(token_file(admin))
    if admin:
        return f"这条命令要管理员令牌，这步交给用户做：用户在这台跑 labmon login --admin（1 小时的 admin 令牌，写到 {token_file(True)}）后再跑"
    return (f"命令行要一个令牌，这步交给用户做（实验会话不输口令、不建账号）：用户在这台跑 labmon login 输看板账号的口令；"
            f"或在看板那台机器上以看板服务的账号跑 labmon user token <看板账号> --scope project --out {token_file()}"
            f"（账号名见 labmon user list；看板不在这台时把这个文件拷到这台同一路径、权限 600）。有了令牌再重跑这条命令")


class Unreachable(Exception):
    """几个地址都连不上（或会改东西的请求回话断在半路：lost 为真，不知道做成没有）。"""

    def __init__(self, msg: str, lost: bool = False):
        super().__init__(msg)
        self.lost = lost


class Failed(Exception):
    """看板回了错（4xx、别的 5xx）：code 是状态码，msg 是它说的原因，data 是回话的 JSON。"""

    def __init__(self, code: int, msg: str, data: dict | None = None):
        super().__init__(msg)
        self.code, self.msg, self.data = code, msg, data or {}


def ca_hint(reasons) -> str:
    """连不上的原因里有证书校验没过（CERTIFICATE_VERIFY_FAILED）时接在后面的那句（#96）：向谁要哪张证书、写到哪。call、login、取更新包共用；别的原因回空串。"""
    if not any("CERTIFICATE_VERIFY_FAILED" in str(r) for r in reasons):
        return ""
    return (f"。证书校验没过：内网入口用私有 CA 时，向看板管理员要签发内网入口证书的 CA 公钥（ca.crt，不是私钥），存到 {conf_file().parent / 'ca' / 'ca.crt'}，"
            f"在 {conf_file()} 那个入口的 [[servers]] 里写 ca = \"~/.config/labmon/ca/ca.crt\"；已写了 ca 的，核对它是不是签发这个入口证书的那张")


def conf_file() -> Path:
    return Path(os.environ.get("LABMON_CLIENT") or "~/.config/labmon/client.toml").expanduser()


def token_file(admin: bool = False) -> Path:
    return conf_file().parent / ("admin.token" if admin else "client.token")


def servers() -> list[dict]:
    """要试的地址 [{url, ctx}]，按 client.toml 的顺序；没有 client.toml 时是本机的内网入口。写错抛 config.ConfigError。"""
    f = conf_file()
    if not f.is_file():
        url, ctx = config.lan_url(config.PORT)
        return [{"url": url, "ctx": ctx}]
    try:
        d = tomllib.loads(f.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise config.ConfigError(f"读不了 {f}：{e}") from e
    out = []
    for s in d.get("servers") or []:
        if not isinstance(s, dict) or not str(s.get("url") or "").startswith(("http://", "https://")):
            raise config.ConfigError(f"{f} 的 [[servers]] 每项要有 url（http:// 或 https:// 开头），写的是 {s!r}")
        ctx = None
        if s["url"].startswith("https://"):
            ctx = config.pinned(str(Path(s["ca"]).expanduser())) if s.get("ca") else ssl.create_default_context()
            if s.get("cert"):
                ctx.load_cert_chain(str(Path(s["cert"]).expanduser()), str(Path(s["key"]).expanduser()) if s.get("key") else None)
        out.append({"url": s["url"].rstrip("/"), "ctx": ctx})
    if not out:
        raise config.ConfigError(f"{f} 里没有 [[servers]]")
    return out


def token(admin: bool = False) -> str | None:
    """这次用哪个令牌：LABMON_TOKEN；要管理员的先 admin.token，再 client.token。"""
    if os.environ.get("LABMON_TOKEN"):
        return os.environ["LABMON_TOKEN"].strip()
    for f in ([token_file(True)] if admin else []) + [token_file()]:
        try:
            t = f.read_text().strip()
        except OSError:
            continue
        if t:
            return t
    return None


def opener(ctx: ssl.SSLContext | None = None, *handlers) -> urllib.request.OpenerDirector:
    """连看板服务的 opener（命令行、tools/deploy.py、冷备 backup pull 共用，#195）：一律直连，不走环境变量（HTTPS_PROXY、HTTP_PROXY、ALL_PROXY）
    与 macOS 系统设置里的代理。看板的地址是内网入口或 TX 上的公网入口，都不该经本机的代理（Claude 应用里 HTTPS_PROXY 指向 Clash、NO_PROXY 没有
    10.x 时，连内网入口握手被代理断掉）。ctx：HTTPS 用的证书上下文（None 按系统的 CA）；handlers 另加的（登录时的 Cookie）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), *([urllib.request.HTTPSHandler(context=ctx)] if ctx else []), *handlers)


def _once(srv: dict, method: str, path: str, body, headers: dict, timeout: float, op=None):
    """发一次：返回 (状态码, JSON 或 None)；请求没送到抛 _Down(sent=False)，发出去之后回话断了、超时了抛 _Down(sent=True)。"""
    hd = {"Accept": "application/json", HEADER: dist.version(), **headers}
    data = None
    if method in ("POST", "PUT"):
        hd["Content-Type"] = "application/json"
        data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode()
    req = urllib.request.Request(srv["url"] + path, data=data, method=method, headers=hd)
    try:
        with (op or opener(srv["ctx"])).open(req, timeout=timeout) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            raw = e.read()
        except (http.client.HTTPException, OSError):    # 错误的回话体没收全：状态码已经拿到，照它处理（502/503 退避重试），只是没有原因那句
            raw = b""
    except urllib.error.URLError as e:                 # 连接、握手、发送时出的错：请求没送到
        raise _Down(False, str(e.reason)) from e
    except (http.client.HTTPException, OSError) as e:  # 发出去之后等回话时断了、超时了
        raise _Down(True, repr(e)) from e
    try:
        js = json.loads(raw) if raw else None
    except ValueError:
        js = None
    return code, js


class _Down(Exception):
    def __init__(self, sent: bool, why: str):
        super().__init__(why)
        self.sent, self.why = sent, why


def call(method: str, path: str, body=None, *, admin: bool = False, timeout: float = 60, patience: float = PATIENCE, idem: bool | None = None,
         headers: dict | None = None, token_path: Path | None = None) -> dict:
    """发到看板接口，回 JSON。几个地址依次试；连不上按模块说明退避重试到 patience 秒（0 是各试一遍就算）。
    idem：做两遍也没事（缺省 GET 是）；4xx 与别的 5xx 抛 Failed，都不通抛 Unreachable。"""
    idem = method == "GET" if idem is None else idem
    hd = dict(headers or {})
    try:
        tok = token(admin) if token_path is None else token_path.read_text().strip()
    except OSError as e:
        raise Failed(0, f"读不了令牌文件 {token_path}：{e.strerror}") from e
    if tok:
        hd["Authorization"] = f"Bearer {tok}"
    try:
        srvs = servers()
    except config.ConfigError as e:
        raise Failed(0, str(e)) from e
    t0, wait, why = time.time(), 1.0, {}
    while True:
        standby = None
        for s in srvs:
            try:
                code, js = _once(s, method, path, body, hd, timeout)
            except _Down as d:
                if d.sent and not idem:
                    raise Unreachable(f"请求发到了 {s['url']}，回话断在半路（{d.why}）：不知道做成没有，看一眼再决定要不要重做", lost=True) from d
                why[s["url"]] = d.why
                continue
            if code == 409 and isinstance(js, dict) and js.get("standby"):     # 备用的看板服务（#163）：没做、照下一个地址试（会改东西的也行）
                why[s["url"]] = f"备用：{js.get('error')}"
                standby = standby or js
                continue
            if code in (RETRY if idem else NOT_SENT):
                why[s["url"]] = f"HTTP {code}" + (f"：{js.get('error')}" if isinstance(js, dict) and js.get("error") else "")
                continue
            if 200 <= code < 300:
                return js if isinstance(js, dict) else {}
            msg = js.get("error") if isinstance(js, dict) and js.get("error") else f"HTTP {code}"
            if code == 401 and token_path is None:
                msg += "；" + login_hint(admin)
            raise Failed(code, msg, js if isinstance(js, dict) else None)
        if standby and len(why) == len(srvs) and all(w.startswith("备用：") for w in why.values()):   # 都连上了、都是备用：再试也一样
            raise Failed(409, standby.get("error") or "看板服务是备用", standby)
        if time.time() - t0 + wait > patience:
            raise Unreachable("连不上看板服务（试过 " + "；".join(f"{u}：{w}" for u, w in why.items()) + "）" + ca_hint(why.values()))
        time.sleep(wait)
        wait = min(wait * 2, BACKOFF_MAX)


def run(method: str, path: str, body=None, *, admin: bool = False, ap=None, timeout: float = 600, as_json: bool = False) -> int:
    """命令行一条命令走接口：回话 {rc, out, err} 原样打出、按 rc 退出；usage 是参数错（ap.error，退出码 2）。
    as_json：标准输出改打回话的 data（只读命令的 --json，#52），err 照打。看板回了错打它的原因、退出 3（要管理员的命令 403 时加一句该怎么办）；连不上退出 2。"""
    try:
        r = call(method, path, body, admin=admin, timeout=timeout)
    except Failed as e:
        print(f"[labmon] {e.msg}" + (f"；{ADMIN_HINT}" if admin and e.code == 403 else ""), file=sys.stderr)
        return 3
    except Unreachable as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 2
    if r.get("usage") and ap is not None:
        ap.error(r["usage"])
    sys.stdout.write(json.dumps(r.get("data"), ensure_ascii=False, indent=1) + "\n" if as_json else r.get("out") or "")
    sys.stdout.flush()
    sys.stderr.write(r.get("err") or "")
    return int(r.get("rc") or 0)


def login(name: str | None = None, admin: bool = False) -> int:
    """labmon login：问名字与口令（不回显、不进参数），在第一个连得上的入口登录、建一个令牌、退出；令牌原文只写进令牌文件（600）。
    缺省建 project 令牌（client.token）；--admin 建 1 小时的 admin 令牌（admin.token，不覆盖 client.token）。"""
    try:
        srvs = servers()
    except config.ConfigError as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 3
    try:
        name = name or input("看板账号：").strip()
        pw = getpass.getpass("口令：")
    except (KeyboardInterrupt, EOFError):
        print("\n[labmon] 取消了", file=sys.stderr)
        return 3
    why = []
    for s in srvs:
        jar = http.cookiejar.CookieJar()
        op = opener(s["ctx"], urllib.request.HTTPCookieProcessor(jar))
        try:
            code, js = _once(s, "POST", "/api/login", {"name": name, "password": pw}, {}, 30, op)
        except _Down as d:
            why.append(f"{s['url']}：{d.why}")
            continue
        if code != 200:
            print(f"[labmon] 登录不上 {s['url']}：{(js or {}).get('error') or f'HTTP {code}'}", file=sys.stderr)
            return 3
        try:
            label = f"命令行（{socket.gethostname()}）" + ("（管理员）" if admin else "")
            code, js = _once(s, "POST", "/api/tokens/add", {"name": label, "scopes": ["admin" if admin else "project"]}, {}, 30, op)
            if code != 200:
                print(f"[labmon] 建令牌没成：{(js or {}).get('error') or f'HTTP {code}'}", file=sys.stderr)
                return 3
            f = token_file(admin)
            write_atomic(f, js["token"] + "\n", mode=0o600)
        except _Down as d:
            print(f"[labmon] 建令牌时连接断了（{d.why}）：再跑一次 labmon login", file=sys.stderr)
            return 2
        try:
            _once(s, "POST", "/api/logout", {}, {}, 5, op)
        except _Down:
            pass
        until = f"，{config.fmt_time(js['until'])} 过期" if js.get("until") else ""
        print(f"[labmon] 已在 {s['url']} 登录 {name}，建了令牌「{js['name']}」（{'、'.join(js['scopes'])}{until}），原文写在 {f}（权限 600）；在看板「我的账号」里能吊销")
        hint = stale_hint(js.get("commit"))
        if hint:
            print(f"[labmon] {hint}")
        return 0
    print("[labmon] 连不上看板服务（试过 " + "；".join(why) + "）" + ca_hint(why), file=sys.stderr)
    return 2


def stale_hint(server_commit: str | None) -> str | None:
    """看板在跑的提交与这份命令行不同时的那句（labmon login、labmon check 打）：看板发的 wheel 装的叫它 labmon update；仓库里直接跑的不提示（维护者自己拉代码）。"""
    mine = dist.version()
    if not server_commit or not dist.installed() or server_commit == mine:
        return None
    return f"看板已更新到 {dist.short(server_commit)}，这份命令行是 {dist.short(mine)}：跑 labmon update 换成同一版本"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_wheel() -> tuple[str, bytes, str]:
    """从首个能用的入口读取更新包。备用入口回 409 standby 时接着试。"""
    try:
        srvs = servers()
    except (config.ConfigError, OSError) as e:
        raise Failed(0, str(e)) from e
    why = []
    tok = token()
    for srv in srvs:
        req = urllib.request.Request(srv["url"] + "/api/client/wheel", headers={HEADER: dist.version()})
        if tok:
            req.add_unredirected_header("Authorization", f"Bearer {tok}")
        try:
            with opener(srv["ctx"], _NoRedirect()).open(req, timeout=60) as r:
                name = r.headers.get_filename() or ""
                commit = r.headers.get("X-Labmon-Commit") or ""
                if not dist.wheel_name_ok(name, commit):
                    raise Failed(0, "看板给的更新包文件名或版本不对")
                return name, r.read(), commit
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise Failed(401, login_hint()) from e
            if e.code not in (409, *RETRY):
                raise Failed(e.code, f"看板拒绝发更新包：HTTP {e.code}") from e
            why.append(f"{srv['url']}：HTTP {e.code}")
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            why.append(f"{srv['url']}：{getattr(e, 'reason', e)}")
    raise Unreachable("取不到客户端（试过 " + "；".join(why) + "）" + ca_hint(why))


def install_update(expected: str | None = None, auto: bool = False) -> tuple[str, dist.Installed | None]:
    """从看板安装更新包。自动更新时下载必须与刚收到的主看板响应同一版本。已是这一版时回 None（labmon update、wait 共用）。
    auto（wait 的自动更新）：这一版最近装不上、还没到再试的时候（dist.failed_wait），不下载、抛 Unavailable（#18）。装不上记下、装成清掉。"""
    if auto and (why := dist.failed_wait(expected)):
        raise dist.Unavailable(why)
    name, data, commit = _download_wheel()
    if expected and commit != expected:
        raise dist.Unavailable("下载的版本与刚收到的看板版本不同，等下一轮再试")
    if commit == dist.version():
        return commit, None
    with tempfile.TemporaryDirectory() as d:
        wheel_path = Path(d) / name
        wheel_path.write_bytes(data)
        try:
            got = dist.install_wheel(wheel_path, commit)
        except (OSError, subprocess.SubprocessError, dist.Unavailable) as e:
            dist.note_failure(commit, e)
            raise
    dist.clear_failure()
    return commit, got


def update() -> int:
    """从看板取更新包。只在新版本安装成功后切换命令。"""
    if not dist.installed():
        print("[labmon] 请先在看板的「我的账号 → 命令行」下载安装脚本。仓库里的版本用 git 更新。", file=sys.stderr)
        return 3
    try:
        commit, got = install_update()
    except Unreachable as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 2
    except Failed as e:
        print(f"[labmon] {e.msg}", file=sys.stderr)
        return 3
    except (OSError, subprocess.SubprocessError, dist.Unavailable) as e:
        print(f"[labmon] 安装更新失败：{e}", file=sys.stderr)
        return 3
    if got is None:
        print(f"[labmon] 已是看板的版本（{dist.short(commit)}）")
        for note in dist.ensure_skill():               # 旧版本换过来的那次没建 skill 入口：补上
            print(f"[labmon] {note}")
        return 0
    print(f"[labmon] 已安装看板的版本 {dist.short(commit)}")
    for note in got.notes:
        print(f"[labmon] {note}")
    mine = dist.skill_of(dist.HERE)
    if got.skill and mine.is_dir() and (changes := dist.skill_changes(mine, got.skill)):
        print(f"[labmon] {dist.skill_note(changes, dist.version(), commit, '在跑的会话请先重挂 labmon wait')}")
    if not got.path_entry.is_symlink() or got.path_entry.readlink() != got.fixed:
        print(f"[labmon] 请使用 {got.fixed}，{got.path_entry} 仍指向原来的安装")
    return 0


def urls(pid: str) -> list[str]:
    """labmon url：看板上这个项目的链接，每个入口一个（client.toml 的顺序）。"""
    return [f"{s['url']}/p/{pid}/" for s in servers()]
