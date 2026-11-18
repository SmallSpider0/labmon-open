"""看板的账号、会话与令牌（#160）：谁能打开看板、能做什么。请求的鉴权只经 check（serve.Handler._allow 按路由表 serve.ROUTES 调）。

文件：状态根目录下的 users.json（权限 600）。口令只存 PBKDF2 哈希，设置码、会话与令牌只存 SHA-256，原文不落盘、不打印、不进日志：
    {"users": {名字: {"role": "admin"|"user", "pw": "pbkdf2_sha256$轮数$盐$哈希" 或 null（还没设、被重置了）,
                      "setup": {"h": 设置码的 SHA-256, "until": epoch 秒} 或 null, "fails": 连着输错几次, "lock": 锁到何时,
                      "t": 建于, "by": 谁建的}},
     "sessions": {SHA-256: {"user", "t", "seen", "until", "entry", "ua"}},
     "tokens":   {SHA-256: {"user", "name", "scopes", "t", "seen", "until"（admin 令牌 1 小时，其余 null）}}}
读改写整段互斥（_edit：进程内锁 + 旁边 users.lock 上的 flock；看板服务与命令行 labmon user 会同时改）；读按 inode、修改时刻、大小缓存（_read），
每次请求都查、改了就重读，所以吊销、撤管理员下一次请求就生效。

作用域（SCOPES）：read 只读；project 项目的写操作（立即采集、调度、登记、归档、改配置），含 read；deploy 部署核对（读与立即采集），含 read；
backup 备份（#163），含 read；reports 只读问题报告元数据（#200）；admin 全部，1 小时过期。另有只给浏览器会话的 account：改自己的口令、管自己的令牌（令牌不能再造令牌）。
实际权限 = 令牌的作用域（会话取角色的全部）∩ 角色能做的（ROLE_SCOPES：管理员全部，普通用户只读与项目）。
普通用户的项目写操作另按对象核（#167）：只能动自己是主人的项目（owns，projects.owner），只能用主机清单里列着他的主机（may_host，config.host_users），
清单登记按全局快照的 regs 认是谁的（need_reg、need_list）；serve.Handler._own 按路由表的 own 列调这几个。看的也只有自己的（access、view、visible）。
入口（ENTRIES）：lan 内网入口，public 公网入口（管理员可按账号允许平台操作）（只绑回环，给隧道接）。
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
import posixpath
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from . import projects as PJ
from .config import CLIENT_HEADER, COMMIT_RE, ConfigError, host_users, pause_by, sched, set_host_users, share_host, user_hosts
from .records import EVENT_CATS, cat_of, log_events, schedule_project
from .state import snapshot, state_root, write_atomic

CLIENT_RECENT_S = 30 * 86400

ITER = 600_000                 # PBKDF2-SHA256 的轮数：随哈希存，以后加轮数旧口令照样认（测试里调低）
SESSION_S = 30 * 86400         # 会话 30 天，滑动续期
TOUCH_S = 3600                 # 会话、令牌的“最后用”至多每小时落盘一次（会话同时续期、重发 Cookie）
SETUP_S = 86400                # 设置码 24 小时有效，用一次作废
ADMIN_TOKEN_S = 3600           # admin 令牌 1 小时过期
FAIL_MAX, LOCK_S = 5, 900      # 同一名字连着输错 5 次（口令、设置码一起算）锁 15 分钟
PW_MIN = 8
COOKIE = "labmon_s"
SCOPES = {"read": "只读", "project": "项目", "deploy": "部署", "backup": "备份", "reports": "问题报告", "admin": "管理员"}
SCOPE_HELP = {"read": "看页面与数据（GET）", "project": "项目的写操作：立即采集、调度的优先与暂停、搬与移除登记、项目配置、归档与恢复、提交问题报告",
              "deploy": "部署脚本：读数据、立即采集", "backup": "备份脚本：读数据、取冷备（GET /api/admin/backup，#163）", "reports": "值守：只读报告编号、报告人和提交时间", "admin": "全部，含账号与平台操作；1 小时过期"}
IMPLIES = {"read": {"read"}, "project": {"project", "read"}, "deploy": {"deploy", "read"}, "backup": {"backup", "read"}, "reports": {"reports"}, "admin": set(SCOPES)}
ACCOUNT = "account"
ROLES = {"admin": "管理员", "user": "普通用户"}
ROLE_SCOPES = {"admin": frozenset(SCOPES), "user": frozenset({"read", "project"})}
ENTRIES = {"lan": "内网入口", "public": "公网入口"}
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}")
NAME_RULE = "名字要字母开头，只用字母、数字与 _ . -，最多 32 个"
CODE_ABC = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"      # 设置码去掉易混的 0 O 1 I
FLEET_CATS = {"op", "keepalive", "borrow", "disk"}  # 普通用户看不到的记录分类（平台操作、保活、借用、共享盘：都是组内服务器的事）


class Denied(Exception):
    """不让做：code 是 HTTP 状态码（401 没登录或凭据失效、403 没权限或改口令时原口令不对、400/404/409 输入不对或不能做、429 锁着），msg 是给人看的一句。"""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


@dataclass
class Ident:
    """这次请求是谁：name、role、scopes（实际权限，会话另含 account）、via（session / token）、key（会话或令牌的 SHA-256）。
    cookie：会话这次续了期，要把 Cookie 原文重发给这个浏览器（只在内存里）。"""
    name: str
    role: str
    scopes: frozenset
    via: str
    key: str
    cookie: str | None = None


def path() -> Path:
    return state_root() / "users.json"


def _blank() -> dict:
    return {"users": {}, "sessions": {}, "tokens": {}}


def _parse(text: str) -> dict:
    try:
        d = json.loads(text)
    except ValueError:
        return _blank()
    return {k: d[k] if isinstance(d, dict) and isinstance(d.get(k), dict) else {} for k in _blank()}


_LOCK = threading.Lock()
_CACHE: list = [None, _blank()]          # [(inode, 修改时刻, 大小), 数据]：一次换整个列表项，线程间不会读到对不上的一对


def _read() -> dict:
    """当前的账号数据（只读，别改它）：文件没变就用缓存。没有或写坏了是空的：没有账号，谁也进不来（只能在本机 labmon user add 重建）。"""
    p = path()
    try:
        st = p.stat()
        sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        c = _CACHE
        if c[0] != sig:
            c[:] = [sig, _parse(p.read_text())]
        return c[1]
    except OSError:
        return _blank()


@contextmanager
def _edit(*, write=True):
    """users.json 读 → 改 → 写的整段互斥（同 projects._locked）：给出现读的数据，改完原子写回（600）；里面抛错就不写。"""
    with _LOCK:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p.with_suffix(".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                d = _parse(p.read_text())
            except OSError:
                d = _blank()
            yield d
            if write:
                write_atomic(p, json.dumps(d, ensure_ascii=False, indent=1), mode=0o600)


def _h(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


_KDF = threading.Lock()                 # PBKDF2 一次约 0.3 秒，进程内一次只算一个：挡住并发登录把 CPU 吃满


def hash_pw(pw: str, it: int | None = None) -> str:
    """口令的存法：pbkdf2_sha256$轮数$盐$哈希（盐 16 字节随机，Base64）。"""
    it, salt = it or ITER, secrets.token_bytes(16)
    with _KDF:
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return f"pbkdf2_sha256${it}${_b64(salt)}${_b64(dk)}"


def _pw_ok(stored: str | None, pw: str) -> bool:
    """口令对不对：按存着的轮数与盐重算，常数时间比较。没设口令的也算一遍（不让人从耗时看出来）。"""
    try:
        algo, it, salt, want = (stored or "").split("$")
        it, salt, want = int(it), base64.b64decode(salt), base64.b64decode(want)
        assert algo == "pbkdf2_sha256" and it > 0
    except (ValueError, AssertionError):
        it, salt, want = ITER, b"labmon-no-such-pw", b""
    with _KDF:
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return hmac.compare_digest(dk, want)


def _canon(code: str) -> str:
    """设置码不分大小写、不管空格与连字符。"""
    return re.sub(r"[^0-9A-Z]", "", (code or "").upper())


def _new_code(t: float) -> tuple[str, dict]:
    c = "".join(secrets.choice(CODE_ABC) for _ in range(12))
    return f"{c[:4]}-{c[4:8]}-{c[8:]}", {"h": _h(c), "until": round(t + SETUP_S)}


def _check_pw(pw: str) -> None:
    if len(pw or "") < PW_MIN:
        raise Denied(400, f"口令至少 {PW_MIN} 个字符")


def _key(users: dict, name: str) -> str | None:
    """按名字找账号（不分大小写）；没有返回 None。"""
    name = (name or "").strip()
    if name in users:
        return name
    low = [k for k in users if k.lower() == name.lower()]
    return low[0] if len(low) == 1 else None


def _admins(d: dict) -> list[str]:
    return [k for k, u in d["users"].items() if u.get("role") == "admin"]


def admins() -> list[str]:
    """各管理员的名字（service.notify：给管理员的提醒另推到他们自己填的推送，#167）。"""
    return _admins(_read())


def _bindings(users: dict) -> dict:
    return {name: u["fleet_person"] for name, u in users.items() if "fleet_person" in u}


def account_identity() -> tuple[tuple[str, ...], dict]:
    """同一次读取取得账号拼写与明确的人员选择。"""
    users = _read()["users"]
    return tuple(users), _bindings(users)


def account(name: str | None) -> dict | None:
    """有没有这个账号（不分大小写）：{name 账号库里的写法, role}；没有返回 None（换主人、开主机、推给项目主人前查）。"""
    users = _read()["users"]
    k = _key(users, name) if name else None
    return {"name": k, "role": users[k].get("role")} if k else None


def _prune(d: dict, t: float) -> None:
    """改文件时顺手去掉过期的会话与令牌、账号已不在的。"""
    for kind in ("sessions", "tokens"):
        d[kind] = {k: r for k, r in d[kind].items() if r.get("user") in d["users"] and not (r.get("until") and r["until"] < t)}


def _log(msg: str) -> None:
    log_events([{"kind": "account", "msg": msg}])


# ---------- 登录、设置码、改口令：连着输错 FAIL_MAX 次锁 LOCK_S ----------
_GHOST: dict[str, list] = {}            # 不存在的名字：在内存里同样计数（[连着错几次, 锁到何时]），回话与真账号一样，不让人试出哪些名字存在


def _locked(name: str, u: dict | None, t: float) -> None:
    until = (u.get("lock") or 0) if u else (_GHOST.get(name.lower()) or [0, 0])[1]
    if until > t:
        raise Denied(429, f"连着输错 {FAIL_MAX} 次，已锁定，约 {math.ceil((until - t) / 60)} 分钟后再试")


def _attempt(name: str, t: float, verify, apply, fail_msg: str, prep=None, slow: bool = True, fail_code: int = 401):
    """一次要验口令或设置码的尝试：先看锁没锁，再验（verify(账号) 返回对不对，在文件锁外算），
    对了在同一次读改写里清零计数、apply(数据, 名字, prep 的结果)；错了记一次、满 FAIL_MAX 锁 LOCK_S，抛 Denied(fail_code, fail_msg)。
    写之前按文件里最新的账号再查一次锁：并发的几次都在第一次失败落盘前过了开头那次检查、排在 PBKDF2 上，
    别的已经把账号锁上时，对的也不收、不清锁，错的不再记次数。
    验与写之间口令或设置码被别处改了的，按错算（验的是旧的）。slow：验的是口令，不存在的名字照样算一次 PBKDF2，耗时上看不出名字在不在。
    fail_code：登录、设置码错了是 401；已登录的人改口令时原口令错了是 403（会话还有效，页面不该当作掉了登录跳登录页）。"""
    d = _read()
    k = _key(d["users"], name)
    u = d["users"].get(k) if k else None
    _locked(name, u, t)
    ok = bool(u) and verify(u)
    if not u:
        if slow:
            _pw_ok(None, "-")
        if len(_GHOST) > 5000:
            _GHOST.clear()
        g = _GHOST.setdefault(name.lower(), [0, 0])
        g[0] += 1
        if g[0] >= FAIL_MAX:
            _GHOST[name.lower()] = [0, t + LOCK_S]
        raise Denied(fail_code, fail_msg)
    extra = prep() if ok and prep else None
    with _edit() as d:
        cur = d["users"].get(k)
        if cur is None:
            raise Denied(fail_code, fail_msg)
        _locked(name, cur, t)
        if ok and all(cur.get(f) == u.get(f) for f in ("pw", "setup")):
            cur.pop("fails", None)
            cur.pop("lock", None)
            _prune(d, t)
            return apply(d, k, extra)
        cur["fails"] = (cur.get("fails") or 0) + 1
        lock = cur["fails"] >= FAIL_MAX
        if lock:
            cur["fails"], cur["lock"] = 0, round(t + LOCK_S)
    if lock:
        _log(f"账号 {k} 连着输错 {FAIL_MAX} 次，锁 {LOCK_S // 60} 分钟")
    raise Denied(fail_code, fail_msg)


def _session(d: dict, name: str, entry: str, ua: str, t: float) -> str:
    raw = secrets.token_urlsafe(32)
    d["sessions"][_h(raw)] = {"user": name, "t": round(t), "seen": round(t), "until": round(t + SESSION_S), "entry": entry, "ua": (ua or "")[:160]}
    return raw


def login(name: str, pw: str, entry: str, ua: str = "", t: float | None = None) -> tuple[str, str]:
    """用口令登录：返回 (会话原文（放进 Cookie）, 账号名)。"""
    t = time.time() if t is None else t
    return _attempt(name, t, lambda u: _pw_ok(u.get("pw"), pw or ""),
                    lambda d, k, _: (_session(d, k, entry, ua, t), k), "名字或口令不对")


def setup(name: str, code: str, pw: str, entry: str, ua: str = "", t: float | None = None) -> tuple[str, str]:
    """用设置码设口令（新加的账号、被重置的）并登录：返回 (会话原文, 账号名)。设置码用一次就作废。"""
    t = time.time() if t is None else t
    _check_pw(pw)
    c = _canon(code)

    def apply(d, k, new):
        u = d["users"][k]
        if (u.get("setup") or {}).get("until", 0) < t:
            raise Denied(400, "设置码已过期（24 小时有效），请管理员在「账号」里重新生成")
        u.update(pw=new, setup=None)
        _log(f"账号 {k} 用设置码设了口令")
        return _session(d, k, entry, ua, t), k
    return _attempt(name, t, lambda u: bool(u.get("setup")) and hmac.compare_digest(_h(c), u["setup"].get("h") or ""),
                    apply, "名字或设置码不对", prep=lambda: hash_pw(pw), slow=False)


def change_pw(who: Ident, old: str, new: str, t: float | None = None) -> str:
    """改自己的口令（要原口令）：别的浏览器都要重新登录，这个浏览器的会话留着；令牌不动。"""
    t = time.time() if t is None else t
    _check_pw(new)

    def apply(d, k, h):
        d["users"][k]["pw"] = h
        n = len(d["sessions"])
        d["sessions"] = {s: r for s, r in d["sessions"].items() if r["user"] != k or s == who.key}
        return "口令改好了" + (f"；别的 {n - len(d['sessions'])} 个浏览器要重新登录" if n > len(d["sessions"]) else "")
    msg = _attempt(who.name, t, lambda u: _pw_ok(u.get("pw"), old or ""), apply, "原口令不对", prep=lambda: hash_pw(new), fail_code=403)
    _log(f"账号 {who.name} 改了口令")
    return msg


def logout(headers, t: float | None = None) -> None:
    """退出：删掉这个浏览器的会话（没有、已过期的也算退出了）。"""
    raw = cookie_of(headers)
    if raw and _h(raw) in _read()["sessions"]:
        with _edit() as d:
            d["sessions"].pop(_h(raw), None)


# ---------- 每次请求：是谁、能不能做 ----------
def cookie_of(headers) -> str | None:
    c = SimpleCookie()
    try:
        c.load(headers.get("Cookie") or "")
    except CookieError:
        return None
    m = c.get(COOKIE)
    return m.value if m and m.value else None


def ident(headers, t: float | None = None) -> Ident:
    """这次请求是谁：Authorization: Bearer <令牌>，或 Cookie 里的会话。没带、无效、过期、吊销都抛 Denied(401)。
    会话用满 TOUCH_S 续期一次（Ident.cookie 要重发），令牌记最后用。"""
    t = time.time() if t is None else t
    auth = (headers.get("Authorization") or "").strip()
    if auth:
        kind, _, raw = auth.partition(" ")
        if kind.lower() != "bearer" or not raw.strip():
            raise Denied(401, "Authorization 头要写成 Bearer <令牌>")
        via, raw = "token", raw.strip()
    else:
        via, raw = "session", cookie_of(headers)
        if not raw:
            raise Denied(401, "要先登录")
    d, k = _read(), _h(raw)
    rec = d["sessions" if via == "session" else "tokens"].get(k)
    u = d["users"].get(rec.get("user")) if rec else None
    if not u or (rec.get("until") and rec["until"] < t):
        raise Denied(401, "登录已过期或已退出，请重新登录" if via == "session" else "令牌无效、已过期或已吊销")
    role = u.get("role") if u.get("role") in ROLES else "user"
    if via == "session":
        scopes = ROLE_SCOPES[role] | {ACCOUNT}
    else:
        scopes = frozenset(set().union(*(IMPLIES.get(s, set()) for s in rec.get("scopes") or [])) & ROLE_SCOPES[role])
    who = Ident(rec["user"], role, frozenset(scopes), via, k)
    version = headers.get(CLIENT_HEADER, "") if via == "token" else ""
    version = version if COMMIT_RE.fullmatch(version) else None
    touch = t - (rec.get("seen") or 0) >= TOUCH_S
    observe = version and (version != rec.get("client_commit") or t - (rec.get("client_seen") or 0) >= TOUCH_S)
    if touch or observe:
        with _edit() as d:
            r = d["sessions" if via == "session" else "tokens"].get(k)
            if r:
                if t - (r.get("seen") or 0) >= TOUCH_S:
                    r["seen"] = round(t)
                    if via == "session":
                        r["until"] = round(t + SESSION_S)
                        who.cookie = raw
                if version and t >= (r.get("client_seen") or 0) and (
                    version != r.get("client_commit") or t - (r.get("client_seen") or 0) >= TOUCH_S
                ):
                    r.update(client_commit=version, client_seen=round(t))
    return who


def check(headers, need, admin: bool = False, t: float | None = None) -> Ident:
    """看板的唯一鉴权入口（serve.Handler._allow 按路由表调）：need 是要的作用域（字符串，或几个里有一个就行的元组；None 只要登录），
    admin 只给管理员。不过时抛 Denied（401 没登录或凭据失效、403 没权限）。入口的限制由路由表和 can_ops 管。"""
    who = ident(headers, t)
    if admin and who.role != "admin":
        raise Denied(403, "只有管理员能做")
    needs = (need,) if isinstance(need, str) else need or ()
    if needs and not who.scopes & set(needs):
        names = "或".join(f"「{SCOPES.get(n, '账号')}」" for n in needs)
        raise Denied(403, f"这个令牌没有{names}权限" if who.via == "token" else
                     "要在浏览器里登录后做" if needs == (ACCOUNT,) else f"这个账号没有{names}权限")
    return who


def can_ops(who: Ident, entry: str) -> bool:
    """平台操作（续租、关机、换人、开机、借卡、保活守护、探测……）：管理员、admin 作用域；公网另需账号许可。"""
    return who.role == "admin" and "admin" in who.scopes and (entry == "lan" or
        entry == "public" and _read()["users"].get(who.name, {}).get("public_ops") is True)


def _sess_view(k: str, r: dict) -> dict:
    return {"id": k[:12], **{f: r.get(f) for f in ("t", "seen", "until", "entry", "ua")}, "entry_name": ENTRIES.get(r.get("entry"), r.get("entry"))}


def _tok_view(k: str, r: dict) -> dict:
    return {"id": k[:12], **{f: r.get(f) for f in ("name", "scopes", "t", "seen", "until", "client_commit", "client_seen")}}


def scope_info() -> dict:
    """页面上的作用域：{键: [名字, 能做什么]}（「我的账号」建令牌的选项、令牌的小签）。"""
    return {s: [SCOPES[s], SCOPE_HELP[s]] for s in SCOPES}


def me(who: Ident, entry: str) -> dict:
    """GET /api/me 与页面上的 ME（看板服务发页面时填进去）：谁、角色、实际作用域、从哪个入口来、能不能做平台操作（ops）与项目的写操作（write）；
    浏览器会话另给自己的会话与令牌（「我的账号」页）。"""
    d = _read()
    out = {"name": who.name, "role": who.role, "role_name": ROLES[who.role], "scopes": sorted(who.scopes - {ACCOUNT}), "via": who.via,
           "entry": entry, "entry_name": ENTRIES.get(entry, entry), "admin": who.role == "admin",
           "ops": can_ops(who, entry), "write": "project" in who.scopes, "account": ACCOUNT in who.scopes}
    if who.role != "admin":                           # 普通用户：自己的项目与能用的主机（#167；页面上按项目判能不能改，canWrite(pid)）
        acc = access(who)
        out["own"], out["hosts"] = sorted(acc.projects), sorted(acc.allowed)
    if out["account"]:
        out["sessions"] = sorted(({**_sess_view(k, r), "this": k == who.key} for k, r in d["sessions"].items() if r.get("user") == who.name),
                                 key=lambda x: -(x["seen"] or 0))
        out["tokens"] = sorted((_tok_view(k, r) for k, r in d["tokens"].items() if r.get("user") == who.name), key=lambda x: -(x["t"] or 0))
        out["token_scopes"] = [s for s in SCOPES if s in ROLE_SCOPES[who.role]]
        out["scope_info"] = scope_info()
    return out


# ---------- 令牌 ----------
def token_add(user: str, name: str, scopes: list, t: float | None = None) -> tuple[str, dict]:
    """给 user 建一个令牌：返回 (原文, 摘要)。原文只在这里出现一次（页面上显示一次，命令行写进文件），文件里只存哈希。
    作用域只能是这个角色能做的（普通用户可建 read、project）；含 admin 的 1 小时过期。"""
    t = time.time() if t is None else t
    scopes = {str(s) for s in scopes or []}
    sc = [s for s in SCOPES if s in scopes]
    if not sc or len(sc) != len(scopes):
        raise Denied(400, "作用域要从 " + "、".join(SCOPES) + " 里选")
    name = (name or "").strip()[:40] or "令牌"
    raw = "lm_" + secrets.token_urlsafe(32)
    with _edit() as d:
        k = _key(d["users"], user)
        if not k:
            raise Denied(404, f"没有账号 {user}")
        role = d["users"][k].get("role")
        bad = [s for s in sc if s not in ROLE_SCOPES.get(role, ())]
        if bad:
            raise Denied(403, f"{ROLES.get(role, role)}不能建「{'、'.join(bad)}」令牌")
        _prune(d, t)
        rec = {"user": k, "name": name, "scopes": sc, "t": round(t), "seen": None, "until": round(t + ADMIN_TOKEN_S) if "admin" in sc else None}
        d["tokens"][_h(raw)] = rec
    _log(f"账号 {k} 建了令牌「{name}」（{'、'.join(sc)}）")
    return raw, _tok_view(_h(raw), rec)


def revoke(user: str, by: str, ids: list | None = None, kinds: tuple = ("sessions", "tokens"), t: float | None = None) -> str:
    """吊销 user 的会话与令牌：ids 给了只吊销这几个（会话、令牌的 id，页面上列的前 12 位），否则 kinds 里的全部。下一次请求就失效。"""
    t = time.time() if t is None else t
    n = {"sessions": 0, "tokens": 0}
    with _edit() as d:
        k = _key(d["users"], user)
        if not k:
            raise Denied(404, f"没有账号 {user}")
        for kind in kinds:
            keep = {}
            for h, r in d[kind].items():
                if r.get("user") == k and (h[:12] in ids if ids else True):
                    n[kind] += 1
                else:
                    keep[h] = r
            d[kind] = keep
        if ids and not sum(n.values()):
            raise Denied(404, "没有这个会话或令牌（可能已吊销或过期）")
        _prune(d, t)
    what = "、".join(f"{v} 个{'会话' if kind == 'sessions' else '令牌'}" for kind, v in n.items() if v) or "（本来就没有）"
    _log(f"{by} 吊销了账号 {k} 的 {what}")
    return f"吊销了 {k} 的 {what}"


# ---------- 管理员：加人、改角色、重置口令、删人、列表 ----------
def add(name: str, role: str, by: str, t: float | None = None, *, person: str) -> str:
    """给已登记的组内人员开通账号，返回一次性设置码。"""
    if not isinstance(person, str) or not person:
        raise Denied(400, "请先登记组内人员，再开通账号")
    t = time.time() if t is None else t
    name = (name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise Denied(400, NAME_RULE)
    if role not in ROLES:
        raise Denied(400, "角色只有 " + "、".join(ROLES))
    with _edit() as d:
        if any(k.lower() == name.lower() for k in d["users"]):
            raise Denied(409, f"已经有账号 {name}")
        if role != "admin" and not _admins(d):
            raise Denied(409, "还没有管理员：第一个账号必须加 --admin")
        # 新账号不能使已有账号失去唯一的人员对应。
        from . import fleet as F
        try:
            F.check_account_binding(F.conf() or {}, d["users"], _bindings(d["users"]), name, person)
        except ConfigError as e:
            raise Denied(409, str(e)) from e
        code, st = _new_code(t)
        d["users"][name] = {"role": role, "pw": None, "setup": st, "t": round(t), "by": by, "fleet_person": person}
    _log(f"{by} 加了账号 {name}（{ROLES[role]}），关联组内人员 {person}，设置码 24 小时内有效")
    return code


def bind_person(name: str, person: str, by: str) -> str:
    """仅给尚无组内人员的账号补绑定，与新建账号共用写入锁。"""
    from . import fleet as F
    with _edit() as d:
        k = _key(d["users"], name)
        if not k:
            raise Denied(404, f"没有账号 {name}")
        cfg = F.conf() or {}
        bindings = _bindings(d["users"])
        if bindings.get(k) == person:
            return f"{k} 已关联组内人员 {person}"
        F.check_account_binding(cfg, d["users"], bindings, k, person, existing=True)
        d["users"][k]["fleet_person"] = person
    msg = f"{k} 已关联组内人员 {person}"
    _log(f"{by} 把账号 {k} 关联到组内人员 {person}")
    return msg


@contextmanager
def removing_person(person: str, cfg: dict | None):
    """与账号开通串行，防止移除名册时留下孤立账号。"""
    from . import fleet as F
    with _edit(write=False) as d:
        links = F.account_people(cfg or {}, d["users"], _bindings(d["users"]))
        bindings = [v for v in _bindings(d["users"]).values() if isinstance(v, str)]
        bindings += [name for name, _ in links.values() if name]
        if any(name.casefold() == person.casefold() for name in bindings):
            raise Denied(409, f"{person} 已开通账号，请先在「账号与权限」处理账号")
        yield


def set_public_ops(name: str, enabled: bool, by: str) -> str:
    """按账号设置公网平台操作；角色和作用域仍在每次请求时核对。"""
    if not isinstance(enabled, bool):
        raise Denied(400, "请选择开启或关闭")
    with _edit() as d:
        k = _key(d["users"], name)
        if not k:
            raise Denied(404, "账号不存在")
        if d["users"][k].get("role") != "admin":
            raise Denied(400, "只有管理员账号可以开启公网平台操作")
        d["users"][k]["public_ops"] = enabled
    msg = f"{by} {'开启' if enabled else '关闭'}了 {k} 的公网平台操作"
    _log(msg)
    return msg


def set_role(name: str, role: str, by: str) -> str:
    """改角色（设为或撤销管理员）：最后一个管理员撤不掉。撤掉后它的会话与令牌下一次请求就只剩普通用户的权限。"""
    if role not in ROLES:
        raise Denied(400, "角色只有 " + "、".join(ROLES))
    with _edit() as d:
        k = _key(d["users"], name)
        if not k:
            raise Denied(404, f"没有账号 {name}")
        u = d["users"][k]
        if u.get("role") == role:
            return f"{k} 本来就是{ROLES[role]}"
        if u.get("role") == "admin" and _admins(d) == [k]:
            raise Denied(409, f"{k} 是最后一个管理员，撤不掉：先把别人设为管理员")
        u["role"] = role
        if role != "admin":
            u.pop("public_ops", None)
    _log(f"{by} 把账号 {k} {'设为管理员' if role == 'admin' else '改成普通用户'}")
    return f"{k} 现在是{ROLES[role]}"


def reset(name: str, by: str, self_ok: bool = False, t: float | None = None) -> str:
    """重置口令：原口令立即失效、这人已登录的浏览器都要重新登录，令牌不动（要一起收回另吊销）；返回新的设置码（24 小时）。
    页面上管理员不能重置自己（self_ok=False：改自己的口令在「我的账号」）；本机命令行能（忘了口令时的找回办法）。"""
    t = time.time() if t is None else t
    with _edit() as d:
        k = _key(d["users"], name)
        if not k:
            raise Denied(404, f"没有账号 {name}")
        if k == by and not self_ok:
            raise Denied(409, "改自己的口令在「我的账号」里")
        code, st = _new_code(t)
        d["users"][k].update(pw=None, setup=st, fails=0, lock=0)
        d["sessions"] = {s: r for s, r in d["sessions"].items() if r.get("user") != k}
    _log(f"{by} 重置了账号 {k} 的口令，设置码 24 小时内有效")
    return code


def remove(name: str, by: str) -> str:
    """删掉一个账号（这人不再用看板）：连同它的会话与令牌，下一次请求就失效。删不了自己，也删不了最后一个管理员。"""
    from .config import HOSTS_LOCK
    with HOSTS_LOCK, PJ._reg_locked(), _edit() as d:
        k = _key(d["users"], name)
        if not k:
            raise Denied(404, f"没有账号 {name}")
        if k == by:
            raise Denied(409, "不能删自己的账号")
        if d["users"][k].get("role") == "admin" and _admins(d) == [k]:
            raise Denied(409, f"{k} 是最后一个管理员，删不掉：先把别人设为管理员")
        from .service import set_user_push
        from .poolqueues import active_reason
        for pid in PJ.registered():
            if (PJ.owner(pid) or "").casefold() == k.casefold() and (reason := active_reason(pid)):
                raise Denied(409, reason)
        set_host_users(k, [], by)
        for pid in PJ.registered():
            PJ._set_owner(pid, None, by, previous=k)
        set_user_push(k, None)
        del d["users"][k]
        for kind in ("sessions", "tokens"):
            d[kind] = {h: r for h, r in d[kind].items() if r.get("user") != k}
    _log(f"{by} 删了账号 {k}（连同它的会话与令牌）")
    return f"删了账号 {k}，它的会话与令牌都已失效"


def listing(t: float | None = None, *, directory=False) -> dict:
    """人员详情的「账号与权限」（管理员）：各账号的角色、口令设了没有、设置码到何时、锁到何时、最后用，会话与令牌（只有 id 与摘要）。"""
    t = time.time() if t is None else t
    d = _read()
    out, host_error = [], None
    try:
        hs = user_hosts()
    except ConfigError as e:
        if not directory:
            raise
        hs, host_error = [], str(e)
    owners = {pid: (PJ.owner(pid) or "").lower() for pid in PJ.registered()}
    for k, u in sorted(d["users"].items(), key=lambda x: (x[1].get("role") != "admin", x[0].lower())):
        ss = [_sess_view(h, r) for h, r in d["sessions"].items() if r.get("user") == k and not (r.get("until") and r["until"] < t)]
        ts = [_tok_view(h, r) for h, r in d["tokens"].items() if r.get("user") == k and not (r.get("until") and r["until"] < t)]
        setup_until = (u.get("setup") or {}).get("until")
        out.append({"name": k, "role": u.get("role"), "public_ops": u.get("public_ops") is True, "t": u.get("t"), "by": u.get("by"), "pw": bool(u.get("pw")),
                    "setup": setup_until, "lock": u["lock"] if (u.get("lock") or 0) > t else None,
                    "seen": max([x["seen"] or 0 for x in ss + ts] or [0]) or None,
                    "sessions": sorted(ss, key=lambda x: -(x["seen"] or 0)), "tokens": sorted(ts, key=lambda x: -(x["t"] or 0)),
                    # 能用的主机（主机清单里列着他的，#167）、他是主人的项目；管理员的都是“全部”，不列
                    "hosts": [str(h["id"]) for h in hs if k.lower() in host_users(h)] if u.get("role") != "admin" else None,
                    "projects": sorted(p for p, o in owners.items() if o == k.lower())})
    pick = [{"id": str(h["id"]), "label": h.get("label") or str(h["id"]), "returned": bool(h.get("returned"))} for h in hs if "id" in h and share_host(h) and not h.get("borrow")]
    result = {"users": out, "admins": len(_admins(d)), "roles": ROLES, "scope_info": scope_info(), "hosts": pick}
    if directory:
        from . import fleet as F
        roster = set(F.registered_people())
        bindings = _bindings(d["users"])
        if host_error:
            result["hosts_error"] = host_error
        try:
            cfg = F.conf() or {}
            known = F.known_people(cfg)
            links = F.account_people(cfg, d["users"], bindings, names=known)
            result["person_choices"] = F.account_choices(cfg, links, bindings)
            people = roster | ({person for person, _ in links.values()} & known)
        except ConfigError as e:
            result["people_error"] = str(e)
            # 配置坏了时只凭账号库中明确保存的对应维护现有账号。
            links = F.account_people({}, bindings, bindings, names=roster)
            people = roster | {person for person, _ in links.values() if person}
        result["people"] = sorted(people, key=str.casefold)
        for u in out:
            u["fleet_person"], u["fleet_person_error"] = links.get(u["name"], (None, None))
    return result


def any_user() -> bool:
    """有没有账号（登录页据此说怎么在本机建第一个管理员）。"""
    return bool(_read()["users"])


# ---------- 本机命令行（labmon user）：直接改文件，只认看板服务的账号本人 ----------
def local_ok() -> None:
    """命令行改账号前查：users.json（或它所在的状态根目录）属于跑命令的这个系统账号，否则拒绝。"""
    p = path()
    q = p if p.exists() else p.parent
    if q.exists() and q.stat().st_uid != os.getuid():
        raise Denied(403, f"{q} 不属于当前系统账号：只能以看板服务的账号本人改账号")


# ---------- 普通用户：按对象核（#167） ----------
def _is(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.lower() == b.lower()


def owns(who: Ident, pid: str) -> bool:
    """能不能动这个项目：管理员都能；普通用户要是它的主人（projects.owner；为空、账号已删的归管理员）。"""
    if who.role == "admin":
        return True
    if not PJ.ID_RE.fullmatch(pid):
        return False
    o = PJ.owner(pid)
    return _is(o, who.name) and _key(_read()["users"], o) is not None


def may_host(who: Ident, hid: str) -> bool:
    """能不能用这台主机（worker add、status、fit、project init，只给某几台的暂停与优先）：管理员都能；普通用户要主机清单里这台的 users 列着他。"""
    if who.role == "admin":
        return True
    return any(str(h.get("id")) == str(hid) and who.name.lower() in host_users(h) for h in user_hosts())


@dataclass
class Access:
    """普通用户能看、能动的：projects 他是主人的项目（含已归档的），allowed 主机清单里列着他的主机，hosts 是页面上给他看的主机（allowed 加上他的项目有车道的）。"""
    projects: set
    allowed: set
    hosts: set


def access(who: Ident, snap: dict | None = None) -> Access:
    """普通用户的 Access（管理员不用它：调用方先看 role）。snap：全局快照（缺省现读），取他的项目有车道的主机。"""
    me = who.name.lower()
    projects = {pid for pid in PJ.registered() if (PJ.owner(pid) or "").lower() == me} if _key(_read()["users"], who.name) else set()
    allowed = {str(h["id"]) for h in user_hosts() if "id" in h and me in host_users(h)}
    snap = snapshot() if snap is None else snap
    lanes = {str(ln.get("host")) for p in (snap or {}).get("projects") or [] if p.get("id") in projects for ln in p.get("lanes") or [] if ln.get("host")}
    return Access(projects, allowed, allowed | lanes)


def need_project(who: Ident, pid: str, new_ok: bool = False) -> None:
    """要动项目 pid：不是主人抛 Denied(403)。new_ok：还没登记的也放（第一次推就登记，主人是推的人）。"""
    if who.role == "admin" or owns(who, pid) or (new_ok and not PJ.cfg_file(pid).is_file() if PJ.ID_RE.fullmatch(pid or "") else False):
        return
    raise Denied(403, f"项目 {pid} 不是你的：只有它的主人与管理员能动它（要接手找管理员在项目「管理」里换主人）")


def need_host(who: Ident, hid: str) -> None:
    if not may_host(who, hid):
        raise Denied(403, f"主机 {hid} 没开给你用：找管理员在「组内服务器 → 人员」里给你开（主机清单这台的 users）")


def need_pause(who: Ident, pid: str) -> None:
    """要加、改或撤项目 pid 的项目级暂停（主机清单 [sched.pause]，#16）：普通用户只能动自己下的；已有别人下的（管理员、没记是谁的）还没到期的暂停时抛 Denied(403)，
    免得重下一次把管理员的暂停改成自己的再撤。项目是不是他的另由 need_project 核；调用方持 HOSTS_LOCK，判断与写入之间别人改不了。"""
    if who.role == "admin" or pid not in (sched().get("pause") or {}) or _is(pause_by().get(pid), who.name):
        return
    raise Denied(403, f"{pid} 的暂停不是你下的（管理员或之前的主人下的）：恢复、改到期都找管理员")


def _nrm(root: str) -> str:
    return posixpath.normpath(str(root or ""))


def reg_projects(snap: dict, root: str, jobs: str, host: str | None = None, *, normalize_jobs: bool = False) -> set:
    """全局快照的 regs 里这条清单（ROOT、JOBS）登记在哪些项目名下；host 给了只看那一台。"""
    job_path = _nrm if normalize_jobs else lambda path: path
    return {q.get("project") for x in snap.get("regs") or [] if host is None or x.get("host") == host
            for q in x.get("queues") or [] if _nrm(q.get("root")) == _nrm(root) and job_path(q.get("jobs")) == jobs}


def need_list(who: Ident, root: str, jobs: str, pid: str, snap: dict | None = None) -> None:
    """worker add、搬登记（普通用户）：同一条清单已登记在别的项目名下时拒绝（worker add 会把别处的登记搬过来、改成这个项目）。"""
    if who.role == "admin":
        return
    if not posixpath.isabs(root):
        raise Denied(400, "清单目录 ROOT 要用绝对路径")
    other = reg_projects(snapshot() if snap is None else snap, root, jobs) - {pid}
    if other:
        raise Denied(403, f"{_nrm(root)}/{jobs} 已登记在别的项目名下：换一条清单，或找管理员")


def need_reg(who: Ident, hid: str, root: str, jobs: str, pid: str | None = None, snap: dict | None = None) -> str | None:
    """worker rm、移除与搬走登记（普通用户）：看板上 hid 这台的这条登记要属于他的项目（pid 给了还要正是 pid）；看板还没看到的拒绝，下一轮采集后再试。"""
    if who.role == "admin":
        return
    ps = reg_projects(snapshot() if snap is None else snap, root, jobs, hid)
    if not ps:
        raise Denied(403, f"看板上 {hid} 还没有 {_nrm(root)}/{jobs} 这条登记（刚登记的等下一轮采集，约 1 分钟后再试）")
    if len(ps) != 1 or (pid and ps != {pid}) or not all(p and owns(who, p) for p in ps):
        raise Denied(403, f"{hid} 上的 {_nrm(root)}/{jobs} 登记归属不一致或不属于你的项目")
    return next(iter(ps))


# ---------- 普通用户看到的（#160 去掉组内，#167 只留自己的主机与项目） ----------
def _related(e: dict, acc: Access) -> bool:
    """一条记录与他有没有关系：主机（host、hosts）是给他看的主机，或项目（project、projects、暂停与优先的 target）是他的。"""
    hs = [e.get("host")] + list(e.get("hosts") or [])
    ps = [e.get("project"), schedule_project(e)] + list(e.get("projects") or [])
    return any(h in acc.hosts for h in hs if h) or any(p in acc.projects for p in ps if p)


def visible(who: Ident, acc: Access | None = None):
    """给 records(see=…) 的筛选：普通用户看不到平台操作、保活、借用、共享盘、账号这几类，看板服务所在机的体检提醒（hostcheck，#166）、计划维护（maint，#165）与看板主备（server，#163），
    组内服务器的掉线与恢复、与组内某人有关（who）的；剩下的也只留与他的主机或项目有关的（#167，_related）。管理员全看。"""
    if who.role == "admin":
        return None
    acc = acc or access(who)
    return lambda e: not (cat_of(e.get("kind")) in FLEET_CATS or str(e.get("kind") or "").startswith("fleet_")
                          or e.get("kind") in ("account", "hostcheck", "maint", "server", "report") or e.get("who")) and _related(e, acc)


def view(who: Ident, snap: dict, acc: Access | None = None) -> dict:
    """看的人能看到的全局快照：普通用户去掉组内服务器（fleet）、共享盘的按人明细与按人告警、给系统通知的提醒（notices）、
    给实验会话的消息（wake）与看板服务所在机的体检（hostcheck，#166），记录的分类也去掉组内的那几类；
    主机只留给他看的（他能用的、他的项目有车道的），项目、已归档的、挪法建议只留他的，登记只留那些主机上他项目的清单（#167）。
    调度卡各项目的行照留（份额跨项目比；按钮页面上只在他自己的项目上），别人项目的暂停去掉谁下的（by）。管理员原样。"""
    if who.role == "admin" or not snap:
        return snap
    acc = acc or access(who, snap)
    out = dict(snap, fleet=None, notices=[], wake=[], hostcheck=None, host_notices=None,
               event_cats=[c for c in snap.get("event_cats") or EVENT_CATS if c[0] not in FLEET_CATS and c[0] != "report"],
               access={"own": sorted(acc.projects), "hosts": sorted(acc.allowed)})
    out.pop("pool_observed", None)  # 派单器内部库存/回执；成员按项目接口看获准的记录。
    out["hosts"] = [h for h in snap.get("hosts") or [] if h.get("id") in acc.hosts]
    out["projects"] = [p for p in snap.get("projects") or [] if p.get("id") in acc.projects]
    out["archived"] = [a for a in snap.get("archived") or [] if a.get("id") in acc.projects]
    out["rebalance"] = {k: v for k, v in (snap.get("rebalance") or {}).items() if k in acc.projects}
    out["regs"] = [{**x, "queues": None if x.get("queues") is None else [q for q in x["queues"] if q.get("project") in acc.projects]}
                   for x in snap.get("regs") or [] if x.get("host") in acc.hosts or any(q.get("project") in acc.projects for q in x.get("queues") or [])]
    if isinstance(snap.get("occ"), dict):
        out["occ"] = {k: v for k, v in snap["occ"].items() if k in acc.hosts}
    sc = snap.get("sched")
    if isinstance(sc, dict):                          # 调度卡各项目的行照留；别人项目的暂停不给看是谁下的（#16）
        out["sched"] = {**sc, "projects": [p if p.get("id") in acc.projects or "by" not in (p.get("paused") or {})
                                           else {**p, "paused": {k: v for k, v in p["paused"].items() if k != "by"}} for p in sc.get("projects") or []]}
    dk = snap.get("disk")
    if isinstance(dk, dict):
        out["disk"] = {k: v for k, v in dk.items() if k not in ("owners", "names", "owners_set", "colors")}
        out["disk"]["alerts"] = [a for a in dk.get("alerts") or [] if not str(a.get("key") or "").startswith(("burst:", "over:"))]
    return out
