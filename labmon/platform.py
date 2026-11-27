"""SCM 平台（思腾合力）客户端：无头续租与恢复用。接口记录见 docs/platform_scm.md。

- 验证码由用户在看板上输入，这里只取回来给页面显示，不自动填。
- 平台密码与登录令牌是“秘密”（secret，#164）：macOS 放钥匙串，Linux 放 600 权限的文件（~/.config/labmon/secrets/）。
  密码由维护者在看板服务那台机器上放一次：labmon secret set platform（不回显、不进命令行参数）。
  登录时取出后按页面同样的方式 RSA 加密（PKCS#1 v1.5，公钥取自平台前端 js/app.*.js），只发给平台；原文不打印、不写日志。
- 共用账号下有别人的作业：动手前核对作业名、资源池、端口（与主机清单一致）才会停止或提交。
- 别让平台当成脚本封号：请求头与网页一致（浏览器 UA、Origin、Referer），一次续租只登录一次，
  步骤之间像人点按钮一样隔几秒，等待时 20 秒查一次列表，出错不自动重试。
"""
from __future__ import annotations

import base64
import getpass
import http.cookiejar
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import fmt_time, host_ssh_key
from .state import write_atomic

SECRETS = {"platform": "labmon-platform", "platform-token": "labmon-platform-token"}   # 种类 → 钥匙串的服务名（已有条目接着用）
SECRET_NAMES = {"platform": "平台密码", "platform-token": "平台登录令牌"}
SSH_SEG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")   # ssh_keys/<owner>/<host_id> 的合法目录与文件名段
TOKEN_TTL = 48 * 3600                            # 登录令牌（JWT）的有效期：存成秘密，看板服务重启后接着用，免得再输验证码
PORTS = (10000, 20000)                           # 平台前端对主机端口的校验范围
NAME_RE = re.compile(r"^[0-9A-Za-z_~-]{1,32}$")   # 平台前端对作业名的校验（^[0-9a-zA-Z-_\\~]*$，去掉反斜杠，限长）
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
PAGE = "/algorithmDeveloped/jobManagement"       # 作业管理页：网页发请求时的 Referer


class PlatformError(Exception):
    pass


class AuthError(PlatformError):
    """令牌失效（code -2）：同一账号只保留一个登录，网页上有人登录就会把这里顶下线。"""


class Refused(PlatformError):
    """平台明确拒绝了这次请求（code 不是 0）：请求到了、没生效，不同于网络出错时不知道生效没有。"""


# ---------- RSA（只用标准库） ----------
def _der(b: bytes, i: int) -> tuple[int, int, int]:
    """读一个 DER 元素：返回 (tag, 内容起点, 内容终点)。"""
    tag, n = b[i], b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    return tag, i, i + n


def rsa_public_key(b64: str) -> tuple[int, int]:
    """SubjectPublicKeyInfo（MIIB… 的 base64）→ (n, e)。"""
    b = base64.b64decode(b64)
    _, s, _ = _der(b, 0)                 # SEQUENCE
    _, _, e1 = _der(b, s)                # AlgorithmIdentifier
    _, s2, e2 = _der(b, e1)              # BIT STRING，首字节是未用位数
    k = b[s2 + 1:e2]
    _, s3, _ = _der(k, 0)                # RSAPublicKey SEQUENCE
    _, a, ae = _der(k, s3)
    _, c, ce = _der(k, ae)
    return int.from_bytes(k[a:ae], "big"), int.from_bytes(k[c:ce], "big")


def rsa_encrypt(key: tuple[int, int], text: str) -> str:
    """PKCS#1 v1.5 加密，结果 base64，与 JSEncrypt.encrypt 相同。"""
    n, e = key
    k = (n.bit_length() + 7) // 8
    m = text.encode()
    if len(m) > k - 11:
        raise PlatformError("密码太长，RSA 加密不了")
    ps = b""
    while len(ps) < k - 3 - len(m):
        ps += bytes(x for x in secrets.token_bytes(k) if x)
    em = b"\x00\x02" + ps[:k - 3 - len(m)] + b"\x00" + m
    return base64.b64encode(pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")).decode()


# ---------- 秘密（平台密码、平台登录令牌；#164） ----------
def _file_store() -> bool:
    """存在文件里（Linux；给了 LABMON_SECRETS 的不论平台）还是 macOS 钥匙串。"""
    return bool(os.environ.get("LABMON_SECRETS")) or sys.platform != "darwin"


def _off() -> bool:
    """测试用 LABMON_NO_KEYCHAIN：不碰真的秘密存放处（钥匙串、~/.config/labmon/secrets）；给了 LABMON_SECRETS（临时目录）的照常用它。"""
    return bool(os.environ.get("LABMON_NO_KEYCHAIN")) and not os.environ.get("LABMON_SECRETS")


def secrets_root() -> Path:
    """秘密文件根目录（缺省 ~/.config/labmon/secrets，LABMON_SECRETS 改；不在 state_root 下，冷备天然不含秘密）。"""
    return Path(os.environ.get("LABMON_SECRETS") or "~/.config/labmon/secrets").expanduser()


def secret_file(kind: str, user: str) -> Path:
    """文件后端的一份秘密：<目录>/<种类>/<账号>，目录 700、文件 600（目录缺省 ~/.config/labmon/secrets，LABMON_SECRETS 改）。"""
    if kind not in SECRETS or not user or "/" in user or user.startswith("."):
        raise PlatformError(f"秘密的种类或账号不对：{kind} {user!r}")
    return secrets_root() / kind / user


def secret(kind: str, user: str) -> str | None:
    """取一份秘密（kind 是 SECRETS 的键）：macOS 钥匙串，Linux 600 权限的文件；没有返回 None。原文不打印、不写日志。"""
    if _off():
        return None
    if _file_store():
        try:
            return secret_file(kind, user).read_text().rstrip("\n") or None
        except FileNotFoundError:
            return None
    r = subprocess.run(["security", "find-generic-password", "-s", SECRETS[kind], "-a", user, "-w"], capture_output=True, text=True)
    return r.stdout.rstrip("\n") or None if r.returncode == 0 else None


def set_secret(kind: str, user: str, value: str) -> None:
    """存一份秘密（覆盖旧的）。文件后端先建 700 的目录，文件一建出来就是 600（state.write_atomic 的 mode）。"""
    if _off():
        return
    if _file_store():
        f = secret_file(kind, user)
        f.parent.mkdir(parents=True, exist_ok=True)
        for d in (f.parent.parent, f.parent):
            os.chmod(d, 0o700)
        write_atomic(f, value, mode=0o600)
        return
    subprocess.run(["security", "add-generic-password", "-U", "-s", SECRETS[kind], "-a", user, "-w", value], capture_output=True, text=True)


def del_secret(kind: str, user: str) -> None:
    if _off():
        return
    if _file_store():
        secret_file(kind, user).unlink(missing_ok=True)
        return
    subprocess.run(["security", "delete-generic-password", "-s", SECRETS[kind], "-a", user], capture_output=True, text=True)


def password(user: str) -> str:
    """平台密码（平台操作登录时取，只发给平台）；没放过就报怎么放。"""
    pw = secret("platform", user)
    if not pw:
        raise PlatformError(f"没有平台账号 {user} 的密码：在看板服务这台机器上跑 labmon secret set platform 放一次后再试")
    return pw


def prompt_secret(kind: str, user: str, ask=None) -> str:
    """labmon secret set：在终端输入一份秘密存起来，不回显、不进命令行参数与日志；返回存在哪的一句话。
    钥匙串交给 security 自己提示输入（-w 不带值）；文件后端用 getpass 输两遍、对上了才写。"""
    name = SECRET_NAMES[kind]
    if _off():
        raise PlatformError("设着 LABMON_NO_KEYCHAIN（测试用），不碰真的秘密存放处")
    if not _file_store():
        r = subprocess.run(["security", "add-generic-password", "-U", "-s", SECRETS[kind], "-a", user, "-w"])
        if r.returncode:
            raise PlatformError(f"security 没存成（退出码 {r.returncode}）")
        return f"{name}（{user}）存进了钥匙串（服务名 {SECRETS[kind]}）"
    ask = ask or getpass.getpass
    v = ask(f"{name}（{user}）：")
    if not v:
        raise PlatformError("没输入，什么都没存")
    if ask("再输一遍：") != v:
        raise PlatformError("两遍不一样，什么都没存")
    set_secret(kind, user, v)
    return f"{name}（{user}）存进了 {secret_file(kind, user)}（600 权限）"


# ---------- 外部机专用 SSH 密钥对（零私钥上传，#114） ----------
def _ssh_seg(val: str, what: str) -> str:
    s = str(val or "").strip()
    if not SSH_SEG_RE.fullmatch(s) or ".." in s:
        raise PlatformError(f"{what}格式不对：{val!r}")
    return s


def ssh_key_owner(owner: str | None) -> str:
    """外部机密钥目录的归属段：普通用户取小写账号名，管理员/未指定归属取 _admin。"""
    s = str(owner or "").strip().lower()
    return _ssh_seg(s, "密钥归属账号") if s else "_admin"


def ssh_key_path(owner: str | None, host_id: str) -> Path:
    """外部机专用 Ed25519 私钥路径：<LABMON_SECRETS>/ssh_keys/<owner>/<host_id>（目录 0700，私钥 0600，绝不进状态目录与冷备）。"""
    return secrets_root() / "ssh_keys" / ssh_key_owner(owner) / _ssh_seg(host_id, "主机 id")


def ssh_auth_cmd(pubkey: str) -> str:
    """用户在目标机粘贴一次即可授权看板专用公钥的一行命令。"""
    q = shlex.quote(pubkey.strip())
    return f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '%s\\n' {q} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"


def _ssh_fingerprint(pub_path: Path) -> str | None:
    r = subprocess.run(["ssh-keygen", "-lf", str(pub_path)], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None
    parts = r.stdout.strip().split()
    return parts[1] if len(parts) >= 2 else (r.stdout.strip() or None)


def ssh_key_info(owner: str | None, host_id: str) -> dict | None:
    """读取已有外部机专用公钥与 SHA256 指纹（只读公钥，绝不返回私钥内容或路径）；不存在返回 None。"""
    if _off():
        return None
    priv = ssh_key_path(owner, host_id)
    pub = priv.with_name(priv.name + ".pub")
    if not (priv.is_file() and pub.is_file()):
        return None
    pubkey = pub.read_text().strip()
    if not pubkey:
        return None
    return {"pubkey": pubkey, "fingerprint": _ssh_fingerprint(pub), "auth_cmd": ssh_auth_cmd(pubkey)}


def ensure_ssh_key(owner: str | None, host_id: str, *, rotate: bool = False) -> dict:
    """在隔离秘密目录下按 (owner, host_id) 生成或读取专用 Ed25519 密钥对（目录 0700、私钥 0600）。
    零私钥上传：私钥永不离开看板机、不回显、不进日志与冷备；返回 {pubkey, fingerprint, auth_cmd}。"""
    if _off():
        raise PlatformError("设着 LABMON_NO_KEYCHAIN（测试用），不碰真的秘密存放处")
    priv = ssh_key_path(owner, host_id)
    pub = priv.with_name(priv.name + ".pub")
    base = secrets_root()
    ssh_dir = base / "ssh_keys"
    owner_dir = priv.parent
    owner_dir.mkdir(parents=True, exist_ok=True)
    for d in (base, ssh_dir, owner_dir):
        os.chmod(d, 0o700)
    if not rotate and priv.is_file() and pub.is_file():
        os.chmod(priv, 0o600)
        info = ssh_key_info(owner, host_id)
        if info:
            return info
    own_seg = ssh_key_owner(owner)
    hid_seg = _ssh_seg(host_id, "主机 id")
    tmp = owner_dir / f".{hid_seg}.{secrets.token_hex(4)}.tmp"
    tmp_pub = tmp.with_name(tmp.name + ".pub")
    try:
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"labmon:{own_seg}:{hid_seg}", "-f", str(tmp), "-q"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise PlatformError(f"生成 SSH 密钥失败：{(r.stderr or r.stdout).strip() or f'退出码 {r.returncode}'}")
        os.chmod(tmp, 0o600)
        os.chmod(tmp_pub, 0o644)
        os.replace(tmp, priv)
        os.replace(tmp_pub, pub)
    finally:
        tmp.unlink(missing_ok=True)
        tmp_pub.unlink(missing_ok=True)
    info = ssh_key_info(owner, host_id)
    if not info:
        raise PlatformError("生成了 SSH 密钥但读不到公钥")
    return info


def del_ssh_key(owner: str | None, host_id: str) -> bool:
    """销毁外部机专用 SSH 密钥对（私钥与公钥一并 unlink，空目录顺手清理）。"""
    if _off():
        return False
    priv = ssh_key_path(owner, host_id)
    pub = priv.with_name(priv.name + ".pub")
    removed = False
    for f in (priv, pub):
        if f.exists() or f.is_symlink():
            f.unlink(missing_ok=True)
            removed = True
    try:
        if priv.parent.is_dir() and not any(priv.parent.iterdir()):
            priv.parent.rmdir()
    except OSError:
        pass
    return removed


def ssh_identity(h: dict | None) -> str | None:
    """主机配置 → 传给 lan.ssh_argv 的专用私钥路径（唯一实现）：
    仅当 ssh_key == "managed" 时返回路径（即使文件尚未生成也返回路径，配合 IdentitiesOnly=yes 绝不回退试看板宿主 ~/.ssh/ 里的维护者个人密钥）。"""
    if not h or host_ssh_key(h) != "managed":
        return None
    return str(ssh_key_path(h.get("owner"), str(h["id"])))


# ---------- 接口 ----------
def token_exp(token: str | None, at: float | None = None) -> float | None:
    """JWT 的到期时刻（payload 的 exp）；解不出来就按登录时刻加 48 小时。"""
    try:
        seg = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))).get("exp")
        if exp:
            return float(exp)
    except (AttributeError, IndexError, ValueError, TypeError):
        pass
    return at + TOKEN_TTL if at else None


def save_session(cli: "Client", user: str, at: float) -> None:
    """登录令牌存成秘密（与密码同一处），看板服务重启后接着用；同一账号只保留一个登录，失效了照旧要重新输验证码。"""
    set_secret("platform-token", user, json.dumps({"base": cli.base, "token": cli.token, "user_id": cli.user_id, "at": round(at)}))


def load_session(base: str, user: str, now: float | None = None) -> tuple["Client", float] | None:
    """存着的还没到期的令牌 → (客户端, 登录时刻)；没有、过期或不是这个平台的返回 None。"""
    try:
        d = json.loads(secret("platform-token", user) or "")
    except ValueError:
        return None
    exp = token_exp(d.get("token"), d.get("at"))
    if d.get("base") != base.rstrip("/") or not d.get("token") or not exp or exp - 600 < (time.time() if now is None else now):
        drop_session(user)
        return None
    cli = Client(base)
    cli.token, cli.user_id = d["token"], d.get("user_id")
    return cli, float(d.get("at") or 0)


def drop_session(user: str) -> None:
    del_secret("platform-token", user)


class Client:
    def __init__(self, base: str, timeout: float = 30):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        self.user_id = None
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.http.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-CN,zh;q=0.9")]

    def _get(self, url: str) -> str:
        with self.http.open(url, timeout=self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    def call(self, method: str, path: str, body=None):
        hdr = {"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json, text/plain, */*",
               "Origin": self.base, "Referer": self.base + (PAGE if self.token else "/login")}
        if self.token:
            hdr["Authorization"] = self.token
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.base + "/api" + path, data=data, method=method, headers=hdr)
        try:
            with self.http.open(req, timeout=self.timeout) as r:
                d = json.loads(r.read())
        except OSError as e:
            raise PlatformError(f"{path} 请求失败：{e}") from None
        except ValueError:
            raise PlatformError(f"{path} 返回的不是 JSON") from None
        if str(d.get("code")) == "-2":
            raise AuthError(f"平台登录已失效（{d.get('msg') or '令牌失效'}）：可能有人在网页上登录了这个账号，重新输验证码再试")
        if str(d.get("code")) != "0":
            raise Refused(f"{path}：{d.get('msg') or d.get('code')}")
        return d.get("data")

    def public_key(self) -> tuple[int, int]:
        html = self._get(self.base + "/")
        m = re.search(r'src="?(/?js/app[.\w]*\.js)', html)
        if not m:
            raise PlatformError("平台首页里找不到 js/app.*.js")
        js = self._get(self.base + "/" + m.group(1).lstrip("/"))
        k = re.search(r"MI[A-Za-z0-9+/]{100,}={0,2}", js)        # SubjectPublicKeyInfo 的 base64
        if not k:
            raise PlatformError("平台前端里找不到 RSA 公钥")
        return rsa_public_key(k.group(0))

    def captcha(self) -> str:
        d = self.call("GET", "/user/captchaImage")
        code = d.get("verifyCode") if isinstance(d, dict) else d
        if not code:
            raise PlatformError("没取到验证码")
        return str(code)

    def login(self, user: str, password: str, code: str) -> None:
        d = self.call("POST", "/user/login", {"loginName": user, "passWord": rsa_encrypt(self.public_key(), password),
                                              "verifyCode": code, "time": fmt_time(time.time(), year=True, sec=True)})
        self.token = (d or {}).get("token")
        self.user_id = (d or {}).get("userId")
        if not self.token:
            raise PlatformError("登录返回里没有令牌")

    def jobs(self, task_name: str = "") -> list[dict]:
        """作业列表第一页（50 条）；task_name 非空时按作业名查，像在网页上搜名字。"""
        d = self.call("POST", "/workManage/list", {"adminFlag": 1, "pageReq": {"pageNum": 1, "pageSize": 50}, "poolName": "", "taskName": task_name})
        return (d or {}).get("workManageVOList") or []

    def roles(self, jid) -> list[dict]:
        return (self.call("GET", f"/workManage/getWorkTaskInfo/{jid}") or {}).get("workTaskRoleVOList") or []

    def stop(self, jid) -> None:
        self.call("POST", f"/workManage/stop?id={jid}")

    def clone(self, jid) -> dict:
        return self.call("POST", f"/workManage/clone?id={jid}")

    def submit(self, clone: dict) -> None:
        self.call("POST", "/submitWork/addWorkTask", submit_body(clone))

    # 提交向导里填下拉框用的只读接口（字段取自平台前端，见 docs/platform_scm.md）
    def pools(self) -> list[dict]:
        """各资源池（每行 poolId、poolName、host、specificationId、specificationName、quantityAvailable）。"""
        return self.call("GET", "/submitWork/getResourcePool/1") or []

    def specs(self, pool_id) -> list[dict]:
        """池里的规格：specificationId、specificationName、quantityAvailable、specificationAmount、isSchedule、queueNum。"""
        return self.call("GET", f"/submitWork/getResourceSpecification/{urllib.parse.quote(str(pool_id))}") or []

    def volumes(self) -> list[dict]:
        """账号能挂的存储卷：fullPath、volumeName。"""
        if not self.user_id:
            raise PlatformError("登录信息里没有 userId，读不了存储卷：重新登录一次")
        return self.call("POST", f"/dataManage/volumeByUserList/{self.user_id}") or []

    def image_repos(self, size: int = 50) -> list[dict]:
        """镜像仓库（id、depositoryName）第一页。"""
        return (self.call("POST", "/imageManage/queryArtifactsAndTags", {"page": {"pageNum": 1, "pageSize": size}}) or {}).get("rows") or []

    def image_tags(self, repo_id, size: int = 50) -> list[dict]:
        """一个仓库的标签（fullPath、tagName）第一页。"""
        body = {"repositoriesId": repo_id, "page": {"pageNum": 1, "pageSize": size}}
        return (self.call("POST", "/imageManage/queryArtifactsAndTags", body) or {}).get("rows") or []


def submit_body(c: dict) -> dict:
    """克隆结果 → 提交向导最后一步发出的请求体（字段对应见 docs/platform_scm.md）。"""
    body = {k: v for k, v in c.items() if k not in ("minFail", "minSuccess", "cloneStorageVolumeVOList", "cloneTaskRoleVOList")}
    body["storageVolumeAddDTOList"] = c.get("cloneStorageVolumeVOList") or []
    roles = []
    for r in c.get("cloneTaskRoleVOList") or []:
        roles.append({**{k: v for k, v in r.items() if k != "cloneTaskHostPortDTOList"},
                      "poolId": c.get("poolId"), "taskHostPortAddDTOList": r.get("cloneTaskHostPortDTOList") or []})
    body["taskRoleAddDTOList"] = roles
    return body


def clone_port_ok(c: dict, port) -> bool:
    return any(str(p.get("containerPort")) == "22" and str(p.get("hostPort")) == str(port)
               for r in c.get("cloneTaskRoleVOList") or [] for p in r.get("cloneTaskHostPortDTOList") or [])


def roles_port_ok(roles: list[dict], port) -> bool:
    def pm(r):
        m = r.get("portMap") or {}
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except ValueError:
                m = {}
        return m
    return any(str(pm(r).get("22")) == str(port) for r in roles)
