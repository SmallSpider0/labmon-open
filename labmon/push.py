"""推到手机（#159）：读 notify.toml、推一条（Bark 或 ntfy，Bark 可加密）。

只用标准库、不 import 本包：看板服务（service.notify、notify_test）与 TX 上的看门狗（tools/tx/labmon-watch.py，#165）共用这一个文件，
TX 上不装 labmon，只把它拷过去。所以出错抛自己的 ConfError（ValueError），由调用方转成各自的说法。
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT_S = 10
KINDS = {"bark": "Bark", "ntfy": "ntfy"}
_SERVERS = {"bark": "https://api.day.app", "ntfy": "https://ntfy.sh"}
_CIPHERS = {16: "aes-128-cbc", 24: "aes-192-cbc", 32: "aes-256-cbc"}


class ConfError(ValueError):
    """notify.toml 写错、读不了。"""


def load(path: Path) -> dict | None:
    """读推送配置：没有文件、没写 push 返回 None（只弹窗）；写错抛 ConfError。调用方每次发都重读，改了不用重启。
    返回 {push, server, key, encrypt_key}（bark）或 {push, server, topic, token}（ntfy）。"""
    try:
        with open(path, "rb") as f:
            d = tomllib.load(f)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfError(f"{path} 读不了：{e}") from None
    return parse(d, str(path))


def parse(d: dict, src: str = "推送配置") -> dict | None:
    """一份推送配置（notify.toml 解析出的表，或页面上填的同形的表，#167）的校验：写法同 load；src 是报错里说的来源。"""
    kind = d.get("push")
    if kind is None or kind == "":
        return None
    if not isinstance(kind, str) or kind not in KINDS:
        raise ConfError(f"{src} 的 push 只能是 {' 或 '.join(KINDS)}")
    sec = d.get(kind) or {}
    if not isinstance(sec, dict):
        raise ConfError(f"{src} 的 [{kind}] 要是一节")
    need = "key" if kind == "bark" else "topic"
    if not isinstance(sec.get(need), str) or not sec[need].strip():
        raise ConfError(f"{src} 的 [{kind}] 缺 {need}")
    srv = sec.get("server")
    if srv is not None and (not isinstance(srv, str) or not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", srv.strip())):
        raise ConfError(f"{src} 的 [{kind}] server 要是 http(s):// 开头的地址")
    out = {"push": kind, "server": str(srv or _SERVERS[kind]).strip().rstrip("/"), need: sec[need].strip()}
    if kind == "bark":
        ek = sec.get("encrypt_key") or None
        if ek is not None and (not isinstance(ek, str) or len(ek.encode()) not in _CIPHERS):
            raise ConfError(f"{src} 的 [bark] encrypt_key 要 16、24 或 32 位（对应 AES-128、192、256），与 Bark App 里填的一样")
        out["encrypt_key"] = ek
    else:
        tok = sec.get("token") or None
        if tok is not None and not isinstance(tok, str):
            raise ConfError(f"{src} 的 [ntfy] token 要是字符串")
        out["token"] = tok
    return out


def dump(conf: dict) -> str:
    """parse 的结果写回成 notify.toml 的写法（页面上填的存成每人一份，#167）；字符串照 JSON 转义写（TOML 的基本字符串认同样的转义）。"""
    sec = {k: v for k, v in conf.items() if k not in ("push", "personal") and v}
    return f"push = {json.dumps(conf['push'])}\n\n[{conf['push']}]\n" + "".join(f"{k} = {json.dumps(v, ensure_ascii=False)}\n" for k, v in sec.items())


def identity(conf: dict) -> tuple:
    """推送目标的去重键，配置端点不同则各推一次。"""
    key = "key" if conf["push"] == "bark" else "topic"
    return conf["push"], conf["server"], conf[key]


def target(conf: dict) -> str:
    """推到哪（命令行打印用，密钥打码）。"""
    if conf["push"] == "bark":
        k = conf["key"]
        return f"Bark {conf['server']}，设备 key {k[:3]}…{k[-2:] if len(k) > 6 else ''}" + ("，推送加密" if conf.get("encrypt_key") else "，未加密")
    return f"ntfy {conf['server']}，主题 {conf['topic'][:3]}…" + ("，带访问令牌" if conf.get("token") else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def personal(conf: dict, trusted: dict | None) -> dict:
    """页面提交的个人配置只能推给官方服务或运维本机配置的同渠道端点。"""
    kind = conf["push"]
    allowed = {_SERVERS[kind]}
    if trusted and trusted["push"] == kind:
        allowed.add(trusted["server"])
    if conf["server"] not in allowed:
        raise ConfError("个人推送只能使用该渠道的官方地址或管理员配置的地址")
    return {**conf, "personal": True}


def one(conf: dict, msg: str, title: str = "labmon") -> None:
    """推一条（同步，超时 TIMEOUT_S）；失败抛 OSError、ValueError 或 subprocess.SubprocessError，由调用方记下（err 给一句短的）。"""
    if conf["push"] == "bark":
        fields = {"title": title, "body": msg, "group": "labmon"}
        if conf.get("encrypt_key"):           # 推送加密：参数 JSON 用 AES-CBC 加密、base64，以表单 ciphertext + iv 发到 /<设备 key>（Bark 文档）
            iv = secrets.token_hex(8)         # 16 位，每条随机，随请求带上
            ct = _aes_cbc(conf["encrypt_key"], iv, json.dumps(fields, ensure_ascii=False).encode())
            req = urllib.request.Request(f"{conf['server']}/{urllib.parse.quote(conf['key'], safe='')}",
                                         urllib.parse.urlencode({"ciphertext": ct, "iv": iv}).encode(),
                                         {"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        else:
            req = urllib.request.Request(f"{conf['server']}/push", json.dumps({"device_key": conf["key"], **fields}, ensure_ascii=False).encode(),
                                         {"Content-Type": "application/json; charset=utf-8"}, method="POST")
    else:
        hd = {"Content-Type": "application/json; charset=utf-8"} | ({"Authorization": f"Bearer {conf['token']}"} if conf.get("token") else {})
        req = urllib.request.Request(f"{conf['server']}/", json.dumps({"topic": conf["topic"], "title": title, "message": msg}, ensure_ascii=False).encode(),
                                     hd, method="POST")
    open_url = urllib.request.build_opener(_NoRedirect).open if conf.get("personal") else urllib.request.urlopen
    with open_url(req, timeout=TIMEOUT_S) as r:
        body = r.read()
    if conf["push"] == "bark":                # Bark 出错时 HTTP 也可能是 200，看回话里的 code
        try:
            code, why = (lambda j: (j.get("code"), j.get("message")))(json.loads(body))
        except (ValueError, AttributeError):
            return
        if code not in (None, 200):
            if conf.get("personal"):
                raise ValueError("Bark 拒绝了这条推送，请核对设备密钥")
            raise ValueError(f"Bark 回 {code}：{why}")


ERRORS = (OSError, ValueError, subprocess.SubprocessError)       # one() 会抛的


def err(e: Exception) -> str:
    """推送失败的一句短话（日志、命令行用；不带密钥）。"""
    return (f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(getattr(e, "reason", None) or e))[:200]


def personal_error(e: Exception) -> str:
    """个人推送的错误只回状态或固定提示，避免远端回显推送密钥。"""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return "推送服务未接收，请核对配置"


def _aes_cbc(key: str, iv: str, data: bytes) -> str:
    """AES-CBC 加密后 base64（PKCS#7 填充）。标准库没有 AES，用系统的 openssl（macOS、Ubuntu 自带）。"""
    kb = key.encode()
    r = subprocess.run(["openssl", "enc", f"-{_CIPHERS[len(kb)]}", "-K", kb.hex(), "-iv", iv.encode().hex(), "-base64", "-A"],
                       input=data, capture_output=True, timeout=10)
    if r.returncode:
        raise ValueError(f"openssl 加密失败：{r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout.decode().strip()
