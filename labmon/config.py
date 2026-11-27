"""读取并补全 monitor.toml 与用户级主机清单（~/.config/labmon/hosts.toml）。所有缺省值集中在这里，collect.py 只读补全后的字典。

配置示例与逐项说明见仓库的 examples/monitor.toml。
"""
from __future__ import annotations

import difflib
import fcntl
import hashlib
import functools
import ipaddress
import ssl
import json
import os
import re
import shlex
import threading
import time
import tomllib
from pathlib import Path, PurePosixPath

from .records import log_events
from .state import state_root, write_atomic
from .worker.pure import GIB, GROUP_RE as AUTO_GROUP_RE, HOLD_RE, POOL, STALE_S  # noqa: F401 — 与 worker 共用唯一实现

# 计划项（分组）、项目、共享盘上各人的配色顺序；配置里写 color 可覆盖
PALETTE = ["#7F77DD", "#1D9E75", "#D85A30", "#378ADD", "#D4537E", "#BA7517", "#639922", "#888780"]
PORT = 8765                    # 看板服务的缺省端口（labmon serve / service install --port）
POOL_LABEL = "算力池"
PLATFORM_KINDS = ("scm", "other", "none")          # 主机平台分类：SCM 集群平台 / 外部平台租用 / 自有机器
SHARE_MODES = ("pool", "borrow", "private")        # 主机共享分类：可入算力池 / 仅手动借用 / 仅自用
SSH_KEY_MODES = ("managed", "system")              # 主机 SSH 密钥模式：看板托管专用密钥 / 宿主机默认密钥
STANDALONE_DATA_ROOT = "~/.local/share/labmon/work"  # 独立主机缺省隔离工作根目录
DISK_WARN_GB = 20                                  # 独立主机本地盘预警线（GiB）
DISK_STOP_GB = 10                                  # 独立主机本地盘准入停止线（GiB，0 关闭）


def pick_color(colors: dict, key: str, taken=()) -> str:
    """给 key 定一个颜色并记进 colors（调用方存盘），以后不随排序变：先挑调色板里还没人用的，用完了按个数轮。"""
    if key not in colors:
        used = set(colors.values()) | set(taken)
        free = [c for c in PALETTE if c not in used]
        colors[key] = free[0] if free else PALETTE[len(colors) % len(PALETTE)]
    return colors[key]


def fmt_time(t: float, year: bool = False, sec: bool = False, date: bool = True) -> str:
    """时刻（epoch 秒）→ 本机时区的文字。页面文案、记录、通知、主机清单与命令行里的时刻都经它，缺省 MM-DD HH:MM；
    year 带年份（写进文件再读回来的用它，parse_time 不用猜年份），sec 带秒，date=False 只要 HH:MM。"""
    return time.strftime(("%Y-" if year else "") + ("%m-%d " if date else "") + "%H:%M" + (":%S" if sec else ""), time.localtime(t))


def fmt_dur(s: float) -> str:
    """时长（秒）→ 文字，与页面上的 dur（common.js）同一口径：1 分钟内写秒、90 分钟内写分钟、两天内写小时、再长写天。看板 Python 侧的文案都经它。"""
    return f"{round(s)} 秒" if s < 60 else f"{round(s / 60)} 分钟" if s < 5400 else f"{s / 3600:.1f} 小时" if s < 48 * 3600 else f"{s / 86400:.1f} 天"


_TIME_RE = re.compile(r"(?:(\d{4})-)?(\d\d)-(\d\d)(?:[ T](\d\d):(\d\d)(?::(\d\d))?)?")
_DAY_RE = re.compile(r"(今天|明天|后天)?\s*(\d{1,2}):(\d\d)")
_DAYS = {None: 0, "今天": 0, "明天": 1, "后天": 2}
NEXT_YEAR_S = 182 * 86400        # 不带年份的到期早于现在超过这么久才算明年（12 月底写 01-05）；近半年内的当作写错，由调用方报“已经过去”


def parse_time(s, t: float | None = None, future: bool = False, rel: bool = False) -> float | None:
    """文字 → 时刻（epoch 秒，本机时区），认不出返回 None。认 YYYY-MM-DD[ HH:MM[:SS]] 与不带年份的 MM-DD[ HH:MM]
    （日期与时刻之间写 T 也行；TOML 原生日期时间 str() 出来的也认，后面多出的时区不看）；只写日期的取当天结束（23:59:59）。
    rel：也认按 t 那天算的「今天 / 明天 / 后天 HH:MM」与只写「HH:MM」（今天）。只有命令行（__main__.parse_until、host return --at）传，
    读主机清单、记录等落盘文字时不认：那样的原文每天读出来都不一样（约定永远到不了），照旧报“认不出”。
    不带年份的按 t（缺省现在）定年份：记录（缺省）不晚于 t 一天，再晚就算去年；future（命令行写的到期）早于 t 半年以上（NEXT_YEAR_S）才算明年，
    近半年内已经过去的照写（调用方报“已经过去”，免得今天下午写成上午的时刻变成明年）。"""
    s = str(s or "").strip()
    t = time.time() if t is None else t
    m = rel and _DAY_RE.fullmatch(s)
    if m:
        hh, mm = int(m[2]), int(m[3])
        if hh > 23 or mm > 59:
            return None
        lt = time.localtime(t)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + _DAYS[m[1]], hh, mm, 0, 0, 0, -1))
    m = _TIME_RE.match(s)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    clock = (int(hh), int(mm), int(ss or 0)) if hh else (23, 59, 59)
    at = lambda yr: time.mktime((yr, int(mo), int(d)) + clock + (0, 0, -1))   # noqa: E731
    year = int(y) if y else time.localtime(t).tm_year
    v = at(year)
    if not y and future and v < t - NEXT_YEAR_S:
        v = at(year + 1)
    elif not y and not future and v > t + 86400:
        v = at(year - 1)
    return v


def check_future(v: float | None, what: str = "到期时间", t: float | None = None) -> None:
    """时刻要在将来，已经过去的（含正好现在）抛 ConfigError；None（不约定、无期限）放过。借用的约定（borrow.begin、set_swap、serve 开始借用前）、
    优先的到期（serve._sched）与命令行的到期（__main__.parse_until 转成退出）都经它，what 是提示里的说法。"""
    if v is not None and v <= (time.time() if t is None else t):
        raise ConfigError(f"{what} {fmt_time(v)} 已经过去了：换一个更晚的时刻")


def shell_path(p: str) -> str:
    """把路径安全拼进 POSIX shell 命令：支持以 ~ 或 ~/ 开头的远端用户家目录展开，其余字符全走 shlex.quote。"""
    s = str(p)
    if s == "~":
        return '"$HOME"'
    if s.startswith("~/"):
        return '"$HOME"/' + shlex.quote(s[2:])
    return shlex.quote(s)


def host_platform(h: dict | None) -> str:
    """主机的平台分类（唯一实现）：scm（SCM 平台）/ other（外平台租用，只做租期提醒不做平台操作）/ none（自有机器）。
    显式写了 platform_kind 时校验合法值；未写时按已有字段自动推断，确保旧 hosts.toml 零改写。"""
    h = h or {}
    v = h.get("platform_kind")
    if v is not None:
        if not isinstance(v, str) or v not in PLATFORM_KINDS:
            raise ConfigError(f"主机 {h.get('id', '?')} 的 platform_kind 只能写 {' / '.join(PLATFORM_KINDS)}，写的是 {v!r}")
        return v
    if any(h.get(k) for k in ("platform_job", "platform", "platform_pool", "platform_holder")):
        return "scm"
    if h.get("lease_until") or h.get("lease_days"):
        return "other"
    return "none"


def is_standalone(h: dict | None) -> bool:
    """这台主机是不是独立主机（不共享集群 worker_home，唯一实现）。
    显式写了 standalone 时直接按它；未写时：声明了 shared 共享盘的不是独立机，否则显式标了 platform_kind=other|none、
    写了外平台 lease_until（且无 SCM 字段）、归属 owner 或托管密钥 ssh_key=managed 的判为独立机；旧式集群配置缺省 False。"""
    if not h:
        return False
    if "standalone" in h and h["standalone"] is not None:
        if not isinstance(h["standalone"], bool):
            raise ConfigError(f"主机 {h.get('id', '?')} 的 standalone 要写 true 或 false，写的是 {h['standalone']!r}")
        return h["standalone"]
    if h.get("shared"):
        return False
    pk_raw = h.get("platform_kind")
    if pk_raw is not None:
        return host_platform(h) != "scm"
    if h.get("lease_until") and not any(h.get(k) for k in ("platform_job", "platform", "platform_pool", "platform_holder")):
        return True
    if h.get("owner") or h.get("ssh_key") == "managed":
        return True
    return False


def host_share(h: dict | None) -> str:
    """主机的共享分类（唯一实现）：pool（可入池）/ borrow（仅手动借用）/ private（仅自用）。
    缺省：SCM 集群共享盘且允许普通用户（share_host）的为 pool，其余（独立主机、外平台、自有机、本机回环）强制缺省 private。"""
    h = h or {}
    v = h.get("share")
    if v is not None:
        if not isinstance(v, str) or v not in SHARE_MODES:
            raise ConfigError(f"主机 {h.get('id', '?')} 的 share 只能写 {' / '.join(SHARE_MODES)}，写的是 {v!r}")
        return v
    return "pool" if host_platform(h) == "scm" and not is_standalone(h) and share_host(h) else "private"


def host_ssh_key(h: dict | None) -> str:
    """主机的 SSH 密钥模式（唯一实现）：managed（看板 LABMON_SECRETS/ssh_keys 托管专用密钥）或 system（宿主机默认密钥，缺省）。"""
    h = h or {}
    v = h.get("ssh_key")
    if v is None:
        return "system"
    if not isinstance(v, str) or v not in SSH_KEY_MODES:
        raise ConfigError(f"主机 {h.get('id', '?')} 的 ssh_key 只能写 {' / '.join(SSH_KEY_MODES)}，写的是 {v!r}")
    return v


def home_key(h: dict) -> tuple:
    """主机的 worker_home 共享域唯一键（唯一实现）：共享盘主机按路径共用 ("shared", worker_home)；
    独立主机按主机隔离 ("host", id, worker_home)，防止多台独立机默认路径撞名串扰。"""
    wh = str(h["worker_home"]).rstrip("/")
    return ("host", str(h["id"]), wh) if is_standalone(h) else ("shared", wh)


def _check_num(hid: str, key: str, v, *, positive: bool = False) -> None:
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (v <= 0 if positive else v < 0):
        want = "正数" if positive else "非负数"
        raise ConfigError(f"主机 {hid} 的 {key} 要写{want}，写的是 {v!r}")


def host_defaults(h: dict) -> dict:
    """一台主机的缺省值（项目配置的 [[hosts]] 与用户级主机清单共用）：端口、Python、分类与共享域、
    工作根目录（data_root：集群主机缺省 /data/labmon，独立主机缺省 ~/.local/share/labmon/work）、
    与其它主机共用的目录（shared）、worker 目录、本地临时目录（local_dir）、水位与名称。"""
    hid = str(h["id"])
    pk = host_platform(h)
    standalone = is_standalone(h)
    sh = host_share(h)
    sk = host_ssh_key(h)
    raw_until = h.get("lease_until")
    if raw_until is not None and raw_until != "":
        if parse_time(str(raw_until)) is None:
            raise ConfigError(f"主机 {hid} 的 lease_until「{raw_until}」认不出：写成 2026-12-01 18:00 这样")
    for k, pos in (("disk_warn_gb", False), ("disk_stop_gb", False), ("disk_quota_gb", True), ("cpu_quota", True), ("mps_vram_gb", True)):
        _check_num(hid, k, h.get(k), positive=pos)
    for k in ("allow_outside_root", "allow_shared_gpu"):
        if k in h and h[k] is not None and not isinstance(h[k], bool):
            raise ConfigError(f"主机 {hid} 的 {k} 要写 true 或 false，写的是 {h[k]!r}")
    if "owner" in h and h["owner"] is not None and (not isinstance(h["owner"], str) or not h["owner"].strip()):
        raise ConfigError(f"主机 {hid} 的 owner 要写非空账号名")
    default_root = STANDALONE_DATA_ROOT if standalone else "/data/labmon"
    h = {"port": 22, "python": "python3", "data_root": default_root, **h}
    h["id"] = hid
    h["platform_kind"] = pk
    h["standalone"] = standalone
    h["share"] = sh
    h["ssh_key"] = sk
    h.setdefault("label", h["id"])
    h.setdefault("worker_home", h["data_root"].rstrip("/") + "/labmon")
    if standalone:
        h.setdefault("local_dir", h["data_root"].rstrip("/") + "/.tmp")
        h.setdefault("disk_warn_gb", DISK_WARN_GB)
        h.setdefault("disk_stop_gb", DISK_STOP_GB)
    # 与其它主机共用的目录（如 NFS 的 /root/data1）：归还后经别的主机读队列状态；第一个由 worker 按人统计用量（组内服务器的「共享盘」）
    h.setdefault("shared", [])
    return h


class _HostsLock:
    """改配置文件（用户级主机清单、项目的 monitor.toml）的“读 → 改 → 校验 → 写”整段互斥：线程之间（HTTP、工作台、组内操作线程）一把 RLock，
    进程之间（看板服务与本机命令行，#18）状态根 hosts.lock 上的 flock。可重入：同一线程只在最外层拿、放文件锁（_depth 只由持着 RLock 的线程改）。
    锁文件放状态根、不放主机清单旁边：测试的 LABMON_HOSTS 指着仓库里不存在的文件；冷备跳过 *.lock。"""

    def __init__(self):
        self._r = threading.RLock()
        self._depth = 0
        self._fh = None

    def __enter__(self):
        self._r.acquire()
        if self._depth == 0:
            try:
                state_root().mkdir(parents=True, exist_ok=True)
                fh = open(state_root() / "hosts.lock", "a")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                except BaseException:
                    fh.close()
                    raise
            except BaseException:
                self._r.release()
                raise
            self._fh = fh
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            fh, self._fh = self._fh, None
            fh.close()                                   # 关了就放掉 flock
        self._r.release()


HOSTS_LOCK = _HostsLock()


def _hosts_edit(fn):
    """装饰改配置文件的函数（主机清单，以及页面上改项目配置的 set_project）：整个函数（含读文件）都在 HOSTS_LOCK 里，
    两个线程不会各读一份旧文本、后写的覆盖先写的。"""
    @functools.wraps(fn)
    def wrapped(*a, **kw):
        with HOSTS_LOCK:
            return fn(*a, **kw)
    return wrapped


class ConfigError(ValueError):
    """配置不对。field：页面上改项目配置时出错的那一项（set_project 填 PROJ_KEYS、ALERT_KEYS 的键或 groups、markers），POST /project/config 回给页面标到那一行。"""

    def __init__(self, msg: str = "", field: str | None = None):
        super().__init__(msg)
        self.field = field


def _expand(p: str, base: Path) -> str:
    p = os.path.expanduser(p)
    return str(p if os.path.isabs(p) else (base / p))


def hosts_file() -> Path:
    """用户级主机清单：各项目共用的 SSH、挂载点、保活检查等写在这里，项目配置只按 id 引用。"""
    return Path(os.environ.get("LABMON_HOSTS", "~/.config/labmon/hosts.toml")).expanduser()


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"读不了配置 {path}：{e}") from e


LEGACY_POOL_KEYS = ("lend", "lend_hours", "borrow_pool")


def check_legacy_pool(hf: Path, item: dict) -> None:
    """旧借用型算力池字段校验唯一实现（#212 已删除）：user_hosts() 与 fleet.conf() 共用，
    确保残留 lend / lend_hours / borrow_pool 的主机绝不会进入调度、采集或自动拉起流程。"""
    bad = [k for k in LEGACY_POOL_KEYS if k in item]
    if bad:
        raise ConfigError(f"{hf} 里 {item.get('id')} 含有已删除的旧借用型算力池字段（{'、'.join(bad)}），请从主机清单移除")


def user_hosts() -> list[dict]:
    """用户级主机清单里的主机（还没补缺省值；已归还的也在，带 returned）：[[hosts]]，加上借过的组内服务器（guest_host）。
    借用中由看板拉起 worker，还了（borrow_returned）就和归还的自用主机一样不再连接。
    borrow 字段：{holder, since, until, for, light（借来时对方在轻用）, away（对方说好不用到几点）}（since、until、away 是 epoch 秒；手写的 borrow_until 认不出时 until 为 None、原文在 until_bad，
    borrow.check 提醒一次）；自用主机写了 borrow_until 的（换给自己借来的）也有，holder 取 borrow_from，swap 为真。"""
    hf = hosts_file()
    if not hf.exists():
        return []
    d = _read_toml(hf)
    hosts = [dict(h) for h in d.get("hosts", [])]
    if any(str(h.get("id")) == POOL for h in hosts):
        raise ConfigError(f"{hf} 的 [[hosts]] 不能叫 {POOL}：它是算力池的保留 id")
    for h in hosts:
        check_legacy_pool(hf, h)
        if h.get("borrow_until"):
            h["borrow"] = {"holder": h.get("borrow_from"), "since": None, **_until(h["borrow_until"]), "for": None, "swap": True}
    servers = (d.get("fleet") or {}).get("servers", [])
    for s in servers:
        check_legacy_pool(hf, s)
    return hosts + [guest_host(s, hosts) for s in servers if s.get("borrow_since")]


def _until(v) -> dict:
    """主机清单里约定还的时刻 borrow_until → {until: epoch 秒, until_bad: 认不出的原文}（labmon 自己写的带年份，手写的可能写错）。"""
    t = parse_time(v)
    return {"until": t, "until_bad": None if t or not v else str(v)}


def active_hosts() -> list[dict]:
    """在用的主机（自用的与借用中的），已补缺省值：all_hosts 去掉已归还的。"""
    return [h for h in all_hosts() if not h.get("returned")]


def all_hosts() -> list[dict]:
    """主机清单里的全部主机（已补缺省值），在用的在前、已归还的在后。自用主机全关了时，平台账号（fleetops.platform_conf）、
    旧主机的使用人（fleet.legacy_person）与按人沿用的端口（fleetboot.back_port）仍会读取已归还的主机。"""
    return sorted((host_defaults(h) for h in user_hosts()), key=lambda h: bool(h.get("returned")))


def home_host(home: str | tuple, hosts: list[dict] | None = None) -> dict | None:
    """一个 worker_home 共享域由哪台在用主机代读、代办：照主机清单顺序第一台匹配域键的在用主机（hosts 缺省 active_hosts）。
    home 可以是 home_key(h) 返回的元组，或共享盘 worker_home 路径字符串（仅匹配非独立主机，防独立主机默认路径撞名）。
    工作台读已归还主机留下的登记、移除已归还主机上的登记（workerctl.run_out 的 rm）都经它：没有就是 None。"""
    return next((h for h in (active_hosts() if hosts is None else hosts) if _same_home(h, home)), None)


def _same_home(h: dict, home: str | tuple) -> bool:
    """主机 h 是不是属于共享域 home（经 home_key 统一判定）：home 为元组时精确比对 home_key(h)；
    home 为字符串时只匹配非独立主机且 worker_home 路径相同（末尾 / 不算）。"""
    if isinstance(home, tuple):
        return home_key(h) == home
    return not is_standalone(h) and h["worker_home"].rstrip("/") == home.rstrip("/")


def home_peers(hid: str, hosts: list[dict] | None = None) -> list[str]:
    """与 hid 共用 worker_home 共享域的在用主机 id（含 hid 自己，照主机清单顺序；hosts 缺省 active_hosts，含借来的）。
    独立主机（is_standalone）自成一域，只返回自己。hid 不在用时为 []。"""
    act = active_hosts() if hosts is None else hosts
    me = next((h for h in act if str(h["id"]) == str(hid)), None)
    if not me:
        return []
    key = home_key(me)
    return [str(h["id"]) for h in act if home_key(h) == key]


def share_host(h: dict | None) -> bool:
    """普通用户的清单由主机上的系统账号执行。看板本机和回环 SSH 只给管理员。"""
    ssh = str((h or {}).get("ssh") or "").strip().rsplit("@", 1)[-1].lower().strip("[]").rstrip(".")
    return bool(ssh and not ssh.startswith("127.") and ssh not in {"localhost", "::1", "0.0.0.0", "::"})


def host_users(h: dict | None) -> list[str]:
    """主机清单里一台主机允许哪些普通用户用（主机项的 users = [...]，#167；外部机 owner 天然包含；管理员不用列）：账号名，小写去重；没写、写错类型的是空（只有管理员）。"""
    if not share_host(h):
        return []
    v = (h or {}).get("users")
    out = [str(x).strip().lower() for x in v if str(x).strip()] if isinstance(v, list) else []
    owner = (h or {}).get("owner")
    if isinstance(owner, str) and owner.strip():
        out.append(owner.strip().lower())
    return list(dict.fromkeys(out))


@_hosts_edit
def set_host_users(name: str, hosts: list[str], by: str) -> str:
    """「组内服务器 → 人员」里改一个普通用户能用哪些主机（#167）：主机清单各 [[hosts]] 块的 users 里加上或去掉 name，只改动到的那几块的这一行
    （去空了删掉这一行）。hosts 里不在主机清单的报错。返回说明。"""
    p, low = hosts_file(), name.strip().lower()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    bl = _blocks(lines, "[[hosts]]")
    known = {b for _, _, b in bl if b}
    bad = sorted(set(hosts) - known)
    if bad:
        raise ConfigError(f"主机清单里没有 {'、'.join(bad)}")
    raw = {str(h.get("id")): h for h in (_read_toml(p).get("hosts") or [])} if p.exists() else {}
    local = sorted(hid for hid in hosts if not share_host(raw.get(hid)))
    if local:
        raise ConfigError(f"看板本机不能开给普通用户：{'、'.join(local)}")
    add, rm = [], []
    for s, end, bid in reversed(bl):
        if not bid:
            continue
        cur = [str(x) for x in raw.get(bid, {}).get("users") or []] if isinstance(raw.get(bid, {}).get("users"), list) else []
        has = low in [x.strip().lower() for x in cur]
        want = bid in hosts
        if has == want:
            continue
        new = cur + [name] if want else [x for x in cur if x.strip().lower() != low]
        (add if want else rm).append(bid)
        lines = lines[:s + 1] + _set_keys(lines[s + 1:end], {"users": new or None}) + lines[end:]
    if not add and not rm:
        return f"{name} 能用的主机没有变"
    what = "；".join(x for x in (f"能用 {'、'.join(sorted(add))}" if add else "", f"不再能用 {'、'.join(sorted(rm))}" if rm else "") if x)
    _write_checked(p, "".join(lines), f"{by} 改了 {name} 能用的主机：{what}")
    return f"{name} {what}"


HOST_RE = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9.\-]+)(?::(\d{1,5}))?")    # Host 头与 [server] 里的“主机[:端口]”：名字或 IP（IPv6 带方括号），可带端口


def host_pair(h: str, dport: int) -> tuple[str, int] | None:
    """Host 头或 [server] 里写的“主机[:端口]” → (主机, 端口)：不分大小写，没写端口的按 dport（浏览器访问缺省端口时不带）；写法不对为 None。"""
    m = HOST_RE.fullmatch((h or "").strip().lower())
    return (m[1].strip("[]"), int(m[2] or dport)) if m else None


CLIENT_HEADER = "X-Labmon-Client"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SERVER_KEYS = ("lan_bind", "tls_cert", "tls_key", "lan_hosts", "public_hosts", "ca_cert", "min_client_commit")


def server() -> dict:
    """看板服务的两个入口（主机清单的 [server]，#165）：内网入口绑哪（lan_bind，缺省 127.0.0.1）、它的 HTTPS 证书（tls_cert、tls_key，PEM；相对路径按主机清单所在目录）、
    两个入口各另认哪些 Host（lan_hosts、public_hosts：浏览器地址栏里的“主机[:端口]”，公网入口的是反代传进来的）；
    ca_cert：签这些证书的小 CA 的公钥证书（不是私钥），看板发客户端时随包附上（#167，dist.py），没写不附。没有这一段时与以前一样：只绑本机、明文、只认 127.0.0.1 与 localhost。
    写错抛 ConfigError：不认识的键（拼错的安全设置不能悄悄不生效）、证书只给了一半、内网入口绑到非本机地址却没配证书（口令不在内网上明文传）。
    min_client_commit：可选的最低客户端提交号（启动时再核对提交历史，#199）。
    端口不在这里：只在命令行（--port、--public-port），由 labmon service 写进托管配置。入口设置在启动时读取，改了要重启服务；安装下载每次请求都读取 ca_cert。"""
    hf = hosts_file()
    d = (_read_toml(hf).get("server") or {}) if hf.exists() else {}
    if not isinstance(d, dict):
        raise ConfigError(f"{hf} 的 [server] 要是一节")
    bad = sorted(set(d) - set(SERVER_KEYS))
    if bad:
        raise ConfigError(f"{hf} 的 [server] 不认识 {'、'.join(bad)}（能写的：{'、'.join(SERVER_KEYS)}）")
    out = {"lan_bind": str(d.get("lan_bind") or "127.0.0.1").strip(), "tls_cert": None, "tls_key": None, "ca_cert": None}
    minimum = d.get("min_client_commit")
    if minimum is not None and (not isinstance(minimum, str) or not COMMIT_RE.fullmatch(minimum)):
        raise ConfigError(f"{hf} 的 [server] min_client_commit 要写完整的 40 位小写提交号")
    out["min_client_commit"] = minimum
    for k in ("tls_cert", "tls_key", "ca_cert"):
        if d.get(k):
            out[k] = _expand(str(d[k]), hf.parent)
    if bool(out["tls_cert"]) != bool(out["tls_key"]):
        raise ConfigError(f"{hf} 的 [server] tls_cert 与 tls_key 要一起写")
    for k in ("lan_hosts", "public_hosts"):
        v = d.get(k) or []
        if not isinstance(v, list) or not all(isinstance(x, str) and host_pair(x, 443) for x in v):
            raise ConfigError(f"{hf} 的 [server] {k} 要写成 [\"主机\", \"主机:端口\"]（浏览器地址栏里的主机与端口），写的是 {v!r}")
        out[k] = [x.strip().lower() for x in v]
    try:
        ipaddress.IPv4Address(out["lan_bind"])
    except ValueError:
        raise ConfigError(f"{hf} 的 [server] lan_bind 要写一个 IPv4 地址（本机网卡的，或 0.0.0.0 绑所有网卡），写的是 {out['lan_bind']!r}") from None
    if not _loopback(out["lan_bind"]) and not out["tls_cert"]:
        raise ConfigError(f"{hf} 的 [server] lan_bind = {out['lan_bind']!r} 不是本机地址：要配 tls_cert、tls_key（内网上不明文传口令与会话）")
    return out


def lan_url(port: int, conf: dict | None = None) -> tuple[str, "ssl.SSLContext | None"]:
    """本机命令行与脚本（tools/deploy.py）怎么连内网入口（#165）：(根地址, TLS 上下文或 None)。绑 0.0.0.0 时经 127.0.0.1，否则经绑的地址；
    有证书时只信任 [server] 的 tls_cert 这一张（钉住证书，不看主机名：经 127.0.0.1 连时证书上没有这个名字）。"""
    conf = server() if conf is None else conf
    addr = "127.0.0.1" if conf["lan_bind"] == "0.0.0.0" else conf["lan_bind"]
    if not conf["tls_cert"]:
        return f"http://{addr}:{port}", None
    return f"https://{addr}:{port}", pinned(conf["tls_cert"])


def pinned(cert: str) -> ssl.SSLContext:
    """只信任 cert 这一张证书的 TLS 上下文（钉住证书，不看主机名）：本机连内网入口（lan_url）与部署脚本远端模式连常驻机（deploy.toml 的 ca）共用。"""
    ctx = ssl.create_default_context(cafile=cert)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.check_hostname = False
    return ctx


def _loopback(addr: str) -> bool:
    return ipaddress.IPv4Address(addr).is_loopback


def sched(t: float | None = None) -> dict:
    """调度设置：用户级主机清单的 [sched]（不放项目的 monitor.toml：项目不能自己给自己提优先级）。看板原样写给各主机的 worker（sched.json），
    缺省值在 worker：half_life_h（项目之间比的 GPU 用量的半衰期，小时）、wait_cap_min（卡在资源上等这么久就不再被越过，只许回填）、
    [sched.weight] 各项目的权重（不写为 1）。不是正数的值不下发。
    [sched.pause]、[sched.focus] 是项目级的暂停与优先（所有主机，含之后加入的，#96）：一个项目一行，值是到期时刻（labmon 写的带年份）或 true（无期限），
    下发成 {项目: 到期 epoch 秒 | None}；已到期的（t 缺省现在）与认不出的不下发。"""
    hf = hosts_file()
    d = (_read_toml(hf).get("sched") or {}) if hf.exists() else {}
    ok = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
    w = {str(k): v for k, v in (d.get("weight") or {}).items() if ok(v)}
    out = {**{k: d[k] for k in ("half_life_h", "wait_cap_min") if ok(d.get(k))}, **({"weight": w} if w else {})}
    for kind in SCHED_OVR:
        o = {p: u for p, u in _sched_ovr(d, kind) if u is not False and (u is None or u > (time.time() if t is None else t))}
        if o:
            out[kind] = o
    return out


SCHED_OVR = {"pause": "暂停", "focus": "优先"}      # 项目级的临时指令：记在主机清单 [sched.<kind>]，所有主机生效


def _sched_ovr(d: dict, kind: str) -> list[tuple[str, float | None | bool]]:
    """[sched] 里 [sched.<kind>] 各行：(项目, 到期 epoch 秒；true 为 None；认不出的为 False)。"""
    v = d.get(kind)
    return [(str(p), None if u is True else parse_time(u) or False) for p, u in (v.items() if isinstance(v, dict) else [])]


def _sched_rows(lines: list[str], sec: str, key: str, val) -> list[str] | None:
    """主机清单 [sched.<sec>] 里 key 这一行改成 val（None 删掉；没有这一段就加在文件末尾）后的各行；没变返回 None。"""
    b = _blocks(lines, f"[sched.{sec}]")
    if not b and any(re.match(r"\s*%s\s*=" % sec, l) for s, end, _ in _blocks(lines, "[sched]") for l in lines[s + 1:end]):
        raise ConfigError(f"{hosts_file()} 里 [sched] 的 {sec} 写成了内联表，改成 [sched.{sec}] 段（每个项目一行）再在页面上改")
    if b:
        s, end, _ = b[0]
        block = _set_keys(lines[s + 1:end], {key: val})
        return None if block == lines[s + 1:end] else lines[:s + 1] + block + lines[end:]
    if val is None:
        return None
    v = _set_keys([], {key: val})[0]
    return ("".join(lines) + ("\n" if lines and not lines[-1].endswith("\n") else "") + f"\n[sched.{sec}]\n{v}").splitlines(keepends=True)


def _sched_set(sec: str, project: str, val, why: str, ev: dict | None = None, also: dict | None = None) -> bool:
    """改主机清单 [sched.<sec>] 里这个项目的一行（val 为 None 删掉；没有这一段就加在文件末尾）。权重、项目级暂停与优先共用。
    also：同一次写入里一起改的别的段 {段: 值}（暂停是谁下的 [sched.pause_by]，#16）。
    sec 那行改了返回 True；要删的本来就没有返回 False（also 的行变了照样写）。ev：记录里替换的字段（暂停、优先记成 override 种类，时间线据此画区间）。"""
    key = project if re.fullmatch(r"[A-Za-z0-9_-]+", project) else json.dumps(project, ensure_ascii=False)
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    main, cur = False, lines
    for i, (s, v) in enumerate([(sec, val), *(also or {}).items()]):
        new = _sched_rows(cur, s, key, v)
        if new is not None:
            cur, main = new, main or i == 0
    if cur is not lines:
        _write_checked(p, "".join(cur), why, ev and {"host": None, "hosts": None, **ev})    # 项目级的指令不属于哪台（改动的行挨着哪台的块不算）
    return main


@_hosts_edit
def set_weight(project: str, w: float | None) -> str:
    """页面上改项目的调度权重：只改 [sched.weight] 里这一行；w 为 None 或 1 删掉这一行（没写的就是 1）。
    改完看板下一轮采集写进各主机的 sched.json，worker 再下一轮生效。返回说明。"""
    if w is not None and not (isinstance(w, (int, float)) and not isinstance(w, bool) and 0 < w <= 1000):
        raise ConfigError("权重要写 0 到 1000 之间的正数")
    w = None if w is None or w == 1 else int(w) if float(w).is_integer() else round(float(w), 3)
    msg = f"把 {project} 的调度权重改成 {w or 1}"
    if not _sched_set("weight", project, w, msg):
        return f"{project} 的权重本来就是 1"
    return msg + "：worker 约 1 分钟内生效"


def sched_ovr_text(kind: str, project: str, until: float | None) -> str:
    """项目级暂停、优先的一句话（记录、命令行与页面的回执）：暂停 X，到 MM-DD HH:MM / 无期限。"""
    return f"{SCHED_OVR[kind]} {project}，" + (f"到 {fmt_time(until)}" if until else "无期限")


def pause_by() -> dict[str, str]:
    """项目级暂停是谁下的（#16）：主机清单 [sched.pause_by]，{项目: 账号名}；看板随 [sched.pause] 一起写、一起删，worker 不读。
    没有这一行的暂停（手写的、#16 之前下的）当管理员下的：普通用户撤不掉（users.need_pause）。"""
    hf = hosts_file()
    v = ((_read_toml(hf).get("sched") or {}) if hf.exists() else {}).get("pause_by")
    return {str(p): str(b) for p, b in v.items() if isinstance(b, str)} if isinstance(v, dict) else {}


@_hosts_edit
def set_sched_ovr(kind: str, project: str, on: bool, until: float | None = None, by: str | None = None) -> str:
    """项目级的暂停（kind pause）、优先（focus）：加（on，until 为到期，None 无期限；已经过去的拒绝）或撤，写主机清单 [sched.<kind>] 里这一行。
    暂停另在同一次写入里记 by（谁下的，[sched.pause_by]；撤的时候一起删，#16）。
    看板下一轮随 sched.json 写给各在用主机，之后加入的主机也有（#96）；命令行不带 --host 的 pause / focus 与页面调度卡都经它。返回说明。"""
    if kind not in SCHED_OVR:
        raise ConfigError(f"不认识的项目级指令 {kind}")
    who = {"pause_by": by if on else None} if kind == "pause" else None
    if on:
        check_future(until)
        what = sched_ovr_text(kind, project, until)
        _sched_set(kind, project, fmt_time(until, year=True) if until else True, f"临时指令：{what}（所有主机）",
                   {"kind": "override", "act": "add", "okind": kind, "target": project, "until": round(until) if until else None}, who)
        return f"{what}：所有主机（含之后加入的）约 1 分钟内生效"
    if not _sched_set(kind, project, None, f"撤销临时指令：{SCHED_OVR[kind]} {project}（所有主机）",
                      {"kind": "override", "act": "rm", "okind": kind, "target": project}, who):
        return f"{project} 本来就没有{SCHED_OVR[kind]}（所有主机的）"
    return f"撤销 {project} 的{SCHED_OVR[kind]}：所有主机约 1 分钟内生效"


@_hosts_edit
def expire_sched(t: float | None = None) -> list[str]:
    """删掉主机清单里已到期的项目级暂停、优先（[sched.pause]、[sched.focus] 的行），各记一条到期记录；看板每轮采集调一次。
    worker 自己也跳过过期的（看板没开时照样到期），这里只是清掉那行、记一笔。认不出的手写值不动（sched 不下发）。返回删了的说明。"""
    t = time.time() if t is None else t
    hf = hosts_file()
    d = (_read_toml(hf).get("sched") or {}) if hf.exists() else {}
    out = []
    for kind in SCHED_OVR:
        for p, u in _sched_ovr(d, kind):
            if u and u <= t:
                what = f"{SCHED_OVR[kind]} {p}，到 {fmt_time(u)}"
                _sched_set(kind, p, None, f"临时指令到期：{what}（所有主机）",
                           {"kind": "override", "act": "expire", "okind": kind, "target": p, "until": round(u)}, {"pause_by": None} if kind == "pause" else None)
                out.append(what)
    return out


def _guest_tpl(hosts: list[dict]) -> dict:
    """借来的主机与算力池照抄哪台自用主机（同一套镜像与共享盘）：跳过独立主机，取第一台在用、不是换来借的；都不在用取第一台非独立主机；没有为 {}。hosts：[[hosts]] 各块。"""
    cand = [h for h in hosts if not is_standalone(h)]
    return next((h for h in cand if not h.get("returned") and not h.get("borrow")), cand[0] if cand else {})


def pool_home(hosts: list[dict] | None = None) -> str | None:
    """算力池登记所在的 worker_home（#24）：池里的清单将来放到借来的组内服务器上跑，那些主机的 worker_home 照抄模板主机（guest_host），
    池登记就放在同一个 worker_home 下（hosts/pool/queues/）。hosts 缺省读主机清单的 [[hosts]]；没有自用主机为 None。"""
    if hosts is None:
        hf = hosts_file()
        hosts = [dict(h) for h in _read_toml(hf).get("hosts", [])] if hf.exists() else []
    tpl = _guest_tpl(hosts)
    return host_defaults({"id": POOL, **{k: tpl[k] for k in ("data_root", "worker_home") if k in tpl}})["worker_home"] if tpl else None


def guest_host(s: dict, hosts: list[dict]) -> dict:
    """借来的组内服务器当调度主机用时的样子：id 用组内服务器的，照抄一台在用自用主机（_guest_tpl）的 python、data_root、worker_home（显式配的；与 pool_home 同一个目录选择）、shared、mounts、checks
    （同一套镜像与共享盘），ssh 主机密钥沿用组内服务器的 known_hosts。hosts：[[hosts]] 各块。"""
    from .fleet import state_file                 # fleet 在上层 import 本模块，这里只能延迟 import
    tpl = _guest_tpl(hosts)
    node = str(s.get("ssh", "")).rsplit(".", 1)[-1] or str(s["id"])
    return {**{k: tpl[k] for k in ("python", "data_root", "worker_home", "ka_bin", "shared", "mounts", "checks") if k in tpl},
            "id": str(s["id"]), "label": f"{node}（借 {s.get('holder')}）", "ssh": s.get("ssh"), "port": s.get("port", 22),
            "known_hosts": str(state_file("known_hosts")), "worker": "auto", "returned": s.get("borrow_returned"),
            "borrow": {"holder": str(s.get("holder")), "since": parse_time(s.get("borrow_since")), **_until(s.get("borrow_until")),
                       "for": s.get("borrow_for") or None, "light": bool(s.get("borrow_light")),
                       "away": parse_time(s.get("borrow_away"))}}


def _merge_items(base: list, extra: list, key: str) -> list:
    """按 key 合并两组条目：同 key 的字段由项目覆盖，detail 列表拼接；其余按先后追加。"""
    out = [dict(b) if isinstance(b, dict) else {key: b} for b in base]
    idx = {b.get(key): i for i, b in enumerate(out)}
    for e in extra:
        e = e if isinstance(e, dict) else {key: e}
        i = idx.get(e.get(key))
        if i is None:
            idx[e.get(key)] = len(out)
            out.append(dict(e))
            continue
        merged = {**out[i], **e}
        if isinstance(out[i].get("detail"), list) and isinstance(e.get("detail"), list):
            merged["detail"] = out[i]["detail"] + [d for d in e["detail"] if d not in out[i]["detail"]]
        out[i] = merged
    return out


def _merge_host(base: dict, proj: dict) -> dict:
    h = {**base, **proj}
    for k, key in (("mounts", "path"), ("checks", "id")):
        if k in base or k in proj:
            h[k] = _merge_items(base.get(k, []), proj.get(k, []), key)
    return h


def _resolve_hosts(cfg: dict) -> None:
    """项目的 [[hosts]] 与用户级主机清单合并；队列引用了但项目没列的主机直接取用户级的。"""
    hf = hosts_file()
    user = {}
    if hf.exists():
        for h in user_hosts():
            if "id" not in h:
                raise ConfigError(f"{hf} 里的 [[hosts]] 缺 id")
            user[str(h["id"])] = h
        cfg["hosts_file"] = str(hf)
    hosts = []
    for h in cfg.get("hosts", []):
        if "id" not in h:
            raise ConfigError("[[hosts]] 缺 id")
        hosts.append(_merge_host(user.get(str(h["id"]), {}), h))
    listed = {h["id"] for h in hosts}
    for q in cfg.get("queues", []):
        if q.get("host") is not None:
            q["host"] = str(q["host"])             # 写成 host = 29（TOML 整数）的也按 "29" 认
        hid = q.get("host")
        if hid is not None and hid not in listed and hid in user:
            hosts.append(_merge_host(user[hid], {}))
            listed.add(hid)
    cfg["hosts"] = hosts


def _reader(q: dict, hosts: list[dict]) -> str | None:
    """队列所在主机已归还时，找一台在用的、与它共用队列目录的主机代读队列状态（只读文件，不看进程）。
    算力池（h 的 pool，#24）：池里待放的清单由与池同一个 worker_home 的在用主机代读（home_host）。"""
    h = next(x for x in hosts if x["id"] == q["host"])
    if h.get("pool"):
        o = home_host(h["worker_home"], [x for x in hosts if not x["returned"] and not x.get("pool")])
        return o["id"] if o else None
    if not h["returned"] or is_standalone(h):
        return None
    root = str(q.get("root", "")).split("*")[0]
    for p in h["shared"]:
        p = p.rstrip("/") + "/"
        if root.startswith(p):
            for o in hosts:
                if not o["returned"] and not is_standalone(o) and o["id"] != h["id"] and p in [s.rstrip("/") + "/" for s in o["shared"]]:
                    return o["id"]
    return None


@_hosts_edit
def set_returned(hid: str, when: str | None) -> Path:
    """在用户级主机清单里给主机写上（when 为 None 时去掉）returned = "…"，只改这一行，其余原样保留。"""
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    for s, end, bid in _blocks(lines, "[[hosts]]"):
        if bid != hid:
            continue
        block = _set_keys(lines[s + 1:end], {"returned": None})
        if when is not None:
            at = next(i for i, l in enumerate(block) if re.match(r"\s*id\s*=", l)) + 1
            block.insert(at, f'returned = "{when}"   # 已归还：不再连接（labmon host restore {hid} 恢复）\n')
        return _write_checked(p, "".join(lines[:s + 1] + block + lines[end:]), f"{hid} 标为已归还" if when is not None else f"{hid} 恢复使用")
    raise ConfigError(f"{p} 里没有主机 {hid}（只能标记用户级主机清单里的主机）")


@_hosts_edit
def set_fleet_holder(sid: str, holder: str, job: str | None = None) -> Path:
    """在用户级主机清单 [[fleet.servers]] 里把这一台的 holder 改掉（开机换人时），只改这一行，行尾注释与其余原样保留；
    job：平台作业名与名字不同时（同名作业已在跑，加了后缀）写一行 job，相同就去掉。"""
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    for s, end, bid in _blocks(lines, "[[fleet.servers]]"):
        if bid != sid:
            continue
        k = next((i for i in range(s + 1, end) if re.match(r"\s*holder\s*=", lines[i])), None)
        if k is None:
            raise ConfigError(f"{p} 里 {sid} 那一台没有 holder 行")
        m = re.match(r"""(\s*holder\s*=\s*)("[^"]*"|'[^']*')(.*)$""", lines[k].rstrip("\n"))
        if not m:
            raise ConfigError(f"{p} 里 {sid} 的 holder 行看不懂：{lines[k].strip()}")
        lines[k] = f'{m.group(1)}"{holder}"{m.group(3)}\n'
        block = [l for l in lines[s + 1:end] if not re.match(r"\s*job\s*=", l)]
        if job and job != holder:
            block.insert(next(i for i, l in enumerate(block) if re.match(r"\s*holder\s*=", l)) + 1, f'job = "{job}"   # 平台作业名（同名作业已在跑，加了后缀）\n')
        return _write_checked(p, "".join(lines[:s + 1] + block + lines[end:]), f"{sid} 的使用人改为 {holder}")
    raise ConfigError(f"{p} 的 [[fleet.servers]] 里没有 {sid}")


def _blocks(lines: list[str], head: str) -> list[tuple[int, int, str | None]]:
    """head（如 [[hosts]]、[[fleet.servers]]、[fleet]、[[groups]]）各块，连同它自己的子表（[[hosts.checks]]、[groups.done_when] 等）：(起始行, 结束行, id)。
    块结束于下一个不是本表子表的 [ 开头的行，块尾的空行与整行注释不算本块。表头行尾的注释（[[markers]]   # 截稿日）照样认（Codex 审查 #128）。"""
    name = head.strip("[]")
    subs = (f"[{name}.", f"[[{name}.")
    out = []
    for s in [i for i, l in enumerate(lines) if re.sub(r"\s*#.*$", "", l.strip()) == head]:
        end = next((i for i in range(s + 1, len(lines)) if lines[i].lstrip().startswith("[") and not lines[i].lstrip().startswith(subs)), len(lines))
        while end > s + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        m = next((re.match(r"""\s*id\s*=\s*["']?([^"'\s#]+)""", l) for l in lines[s + 1:end] if re.match(r"\s*id\s*=", l)), None)
        out.append((s, end, m.group(1) if m else None))
    return out


def keep_section(new: str, old: str, head: str = "[server]") -> str:
    """主机清单里每台各一份的段（[server]，#197）：new（如备份里的）里 head 那一段换成 old（这台原来的）里的，
    old 里没有就删掉 new 里的，new 里没有就接在末尾；连同紧贴在表头上方的整行注释一起换。其余原样。合出来解析不了抛 ConfigError。"""
    def cut(text: str) -> tuple[list[str], int, int] | None:
        lines = text.splitlines(keepends=True)
        b = _blocks(lines, head)
        if not b:
            return None
        s, end, _ = b[0]
        while s > 0 and lines[s - 1].lstrip().startswith("#"):
            s -= 1
        return lines, s, end
    n, o = cut(new), cut(old)
    mine = "".join(o[0][o[1]:o[2]]) if o else ""
    if mine and not mine.endswith("\n"):
        mine += "\n"
    if n:
        lines, s, end = n
        if not mine:                                 # 连同前面的一个空行删掉
            s -= 1 if s and not lines[s - 1].strip() else 0
        out = "".join(lines[:s]) + mine + "".join(lines[end:])
    else:
        out = new
        if mine:
            out += ("\n" if out and not out.endswith("\n") else "") + ("\n" if out.strip() else "") + mine
    try:
        tomllib.loads(out)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"合上这台原来的 {head} 后主机清单解析不了：{e}") from e
    return out


def _write_checked(p: Path, text: str, why: str | None = None, ev: dict | None = None, check=None) -> Path:
    """改完先校验再写；改了哪些行记进记录：why 是正文（为什么改），改动的行放 detail，动到的主机或服务器放 host / hosts；
    ev 替换记录里的字段（缺省 kind 为 config；项目级暂停、优先记成 override 种类，带 act、okind、target、until）。
    check(text)：缺省只看 TOML 解析得了；项目配置传整套 load 的校验（set_project），抛 ConfigError 就不写。"""
    try:
        (check or tomllib.loads)(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"改完的 {p} 解析不了，未写入：{e}") from e
    except ConfigError as e:
        raise ConfigError(f"改完的 {p} 校验不过，未写入：{e}") from e
    old = p.read_text() if p.exists() else ""
    write_atomic(p, text)
    note, ids = _diff_note(old, text)
    if note:
        log_events([{"kind": "config", "host": ids[0] if len(ids) == 1 else None, "hosts": ids if len(ids) > 1 else None,
                     "msg": why or f"改了 {p.name}", "detail": note, **(ev or {})}])
    return p


def _diff_note(old: str, new: str) -> tuple[str, list[str]]:
    """配置文件改了哪些行（给记录看）：每处写所在块的 id（或段名）与旧行 → 新行，空行与注释不算；另返回动到的块 id（不含段名）。"""
    a, b, out, ids = old.splitlines(), new.splitlines(), [], []
    def where(lines):
        for l in reversed(lines):
            m = re.match(r"""\s*id\s*=\s*["']?([^"'\s#]+)""", l) or re.match(r"\s*(\[\[?[^\]]+\]\]?)", l)
            if m:
                return m.group(1)
        return None
    def txt(ls):
        return " / ".join(x.strip() for x in ls if x.strip() and not x.strip().startswith("#"))[:160]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        x, y = txt(a[i1:i2]), txt(b[j1:j2])
        if op == "equal" or not (x or y):
            continue
        w = where(b[:j1] if j1 else a[:i1])
        out.append(f"{w + ' ' if w else ''}{f'{x} → {y}' if x and y else f'加了 {y}' if y else f'删了 {x}'}")
        if w and not w.startswith("[") and w not in ids:
            ids.append(w)
    return "；".join(out)[:400], ids


@_hosts_edit
def add_fleet_server(sid: str, holder: str, ssh: str, port: int, pool: str, job: str | None = None) -> Path:
    """看板开了新服务器：在用户级主机清单最后一个 [[fleet.servers]] 块后面加一块（没有就加在文件末尾），其余原样保留。"""
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    blocks = _blocks(lines, "[[fleet.servers]]")
    if any(b[2] == sid for b in blocks):
        raise ConfigError(f"{p} 的 [[fleet.servers]] 里已有 {sid}")
    new = [f'[[fleet.servers]]\nid = "{sid}"\nholder = "{holder}"   # 看板开新的时加的\n' + (f'job = "{job}"\n' if job and job != holder else "") + f'ssh = "{ssh}"\nport = {int(port)}\npool = "{pool}"\n']
    at = blocks[-1][1] if blocks else len(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    before = ["\n"] if at and lines[at - 1].strip() else []
    after = ["\n"] if at < len(lines) and lines[at].strip() else []
    return _write_checked(p, "".join(lines[:at] + before + new + after + lines[at:]), f"加了组内服务器 {holder}（{sid}）")


@_hosts_edit
def remove_fleet_server(sid: str) -> Path:
    """在用户级主机清单里删掉这一台的 [[fleet.servers]] 块（连同它前面紧挨着的空行），其余原样保留。"""
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    for s, end, bid in _blocks(lines, "[[fleet.servers]]"):
        if bid == sid:
            while s > 0 and not lines[s - 1].strip():
                s -= 1
            return _write_checked(p, "".join(lines[:s] + lines[end:]), f"从清单里删了组内服务器 {sid}")
    raise ConfigError(f"{p} 的 [[fleet.servers]] 里没有 {sid}")


TOML_DT = r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?"   # 原生日期时间（日期与时刻之间可以是空格）


def _toml_val(v) -> str | None:
    """一个值写成 TOML（_set_keys 与 _append_blocks 共用）：布尔、数、单行数组；字符串按 JSON 转义写成基本字符串；None 为 None。"""
    return None if v is None else str(v).lower() if isinstance(v, bool) else str(v) if isinstance(v, (int, float)) else \
        "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]" if isinstance(v, list) else json.dumps(str(v), ensure_ascii=False)


def _set_keys(block: list[str], keys: dict, path: Path | None = None) -> list[str]:
    """块的顶层（第一个子表之前）逐个改键：值为 None 删掉这一行；没有就加在 id 行后面。行尾注释保留。
    对整份文件调就是改文件顶层的键（第一个表头之前）；path 只用于报错（缺省主机清单）。字符串按 JSON 转义写成 TOML 基本字符串。"""
    top = next((i for i, l in enumerate(block) if l.lstrip().startswith("[")), len(block))
    head, rest = block[:top], block[top:]
    for k, v in keys.items():
        i = next((n for n, l in enumerate(head) if re.match(r"\s*%s\s*=" % re.escape(k), l)), None)
        val = _toml_val(v)
        if i is None:
            if val is not None:
                at = next((n for n, l in enumerate(head) if re.match(r"\s*id\s*=", l)), -1) + 1
                head.insert(at, f"{k} = {val}\n")
        elif val is None:
            head.pop(i)
        else:
            if re.match(r"\s*%s\s*=\s*(\{|\[[^\]]*$)" % re.escape(k), head[i]):      # 内联表、跨行的数组：这里只改单行的值
                raise ConfigError(f"{path or hosts_file()} 里 {k} 写成了内联表或多行数组，改成单行再试：{head[i].strip()}")
            # 旧值按整个 TOML 值认：基本字符串里的转义（\" 等）不算结尾，中间有空格的原生日期时间整个算（Codex 审查 #128）
            m = re.match(r"""(\s*%s\s*=\s*)("(?:[^"\\]|\\.)*"|'[^']*'|\[[^\]]*\]|%s|[^\s#]+)(.*)$""" % (re.escape(k), TOML_DT), head[i].rstrip("\n"))
            head[i] = f"{m.group(1)}{val}{m.group(3)}\n"
    return head + rest


@_hosts_edit
def set_fleet_keys(keys: dict, why: str | None = None, sid: str | None = None, host: str | None = None) -> Path:
    """改用户级主机清单 [fleet] 段的顶层键（如页面上认作保活时的 keepalive），sid 给了就改这一台的 [[fleet.servers]] 块
    （如保活守护的开关 guard、借用），host 给了就改这台自用主机的 [[hosts]] 块（如约定归还）；只动这几行，why 进记录。"""
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    b = [x for x in _blocks(lines, "[[fleet.servers]]") if x[2] == sid] if sid else \
        [x for x in _blocks(lines, "[[hosts]]") if x[2] == host] if host else _blocks(lines, "[fleet]")
    if not b:
        raise ConfigError(f"{p} 里没有 {f'组内服务器 {sid}' if sid else f'主机 {host}' if host else '[fleet] 段'}")
    s, end, _ = b[0]
    return _write_checked(p, "".join(lines[:s + 1] + _set_keys(lines[s + 1:end], keys) + lines[end:]), why)


@_hosts_edit
def add_mine_host(hid: str, job: str, ssh: str, port: int, pool: str, label: str | None = None,
                  holder: str | None = None) -> tuple[Path, str]:
    """看板把一台换给自己：主机清单里有同一 ssh 与端口、已归还的自用主机就恢复它，否则照抄一台自用主机
    （写了 platform_job 的，连同 worker、保活检查等）加一块，改 id、label、ssh、port、platform_job、platform_pool。返回 (文件, 主机 id)。"""
    from .fleet import host_holder, legacy_person                # fleet 在上层 import 本模块
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    raw = _read_toml(p) if p.exists() else {}
    hosts = raw.get("hosts", [])
    fleet = raw.get("fleet")
    keys = {"returned": None, "platform_holder": holder, "platform_job": job, "platform_pool": pool, "borrow_until": None, "borrow_from": None}   # 上次借来时的约定不带过来
    for s, end, bid in _blocks(lines, "[[hosts]]"):
        h = next((x for x in hosts if str(x.get("id")) == bid), {})
        if h.get("ssh") == ssh and int(h.get("port") or 22) == int(port):
            if not h.get("returned"):
                raise ConfigError(f"{p} 里已有在用的主机 {bid}（{ssh}:{port}）")
            if holder is not None and host_holder(h, fleet) != holder:
                keys["users"] = None                                     # 换了人：开给谁用是原来那人定的
            updated = lines[:s + 1] + _set_keys(lines[s + 1:end], keys) + lines[end:]
            # 首次给旧主机写显式归属时，先把旧清单推断的人名固化。与主机改动同一次覆盖写。
            if holder is not None and not h.get("platform_holder") and fleet is not None and not fleet.get("me"):
                if original := legacy_person(fleet):
                    start, end, _ = _blocks(updated, "[fleet]")[0]
                    updated[start + 1:end] = _set_keys(updated[start + 1:end], {"me": original})
            return _write_checked(p, "".join(updated), f"{bid} 换给自己，恢复成自用主机"), bid
    if any(str(x.get("id")) == hid for x in hosts):
        raise ConfigError(f"{p} 里已有主机 {hid}")
    tpl = sorted((b for b in _blocks(lines, "[[hosts]]") if any(str(x.get("id")) == b[2] and x.get("platform_job") for x in hosts)),
                 key=lambda b: bool(next(x for x in hosts if str(x.get("id")) == b[2]).get("returned")))
    if not tpl:
        raise ConfigError(f"{p} 里没有自用主机（写了 platform_job 的 [[hosts]]）可照抄")
    s, end, _ = tpl[0]
    template = next(x for x in hosts if str(x.get("id")) == tpl[0][2])
    if holder is not None and host_holder(template, fleet) != holder:
        keys["users"] = None
    block = _set_keys(lines[s + 1:end], {"id": hid, "label": label or hid, "ssh": ssh, "port": int(port), **keys})
    block = [l for l in block if not re.match(r"\s*#", l)]               # 照抄的整行注释是原主机的，不带过来
    new = ["\n", f"# {label or hid}：看板换给自己时加的（照抄 {tpl[0][2]}）\n", "[[hosts]]\n"] + block
    at = max(e for _, e, _ in _blocks(lines, "[[hosts]]"))
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return _write_checked(p, "".join(lines[:at] + new + (["\n"] if at < len(lines) and lines[at].strip() else []) + lines[at:]), f"{hid} 换给自己，加成自用主机"), hid


# 页面上能改的项目配置（#122）：顶层键与 [alerts] 的阈值，(说法, 单位, 下限, 上限, 缺省)；整数的写成整数。
# 缺省值 load 照它补，取值范围 set_project 照它查；页面经 serve.cfg_info 的 spec 取这两张表（#133），不另抄一份
PROJ_KEYS = {"title": ("标题", "", None, None, "实验监控"), "interval": ("采集间隔", "秒", 30, 3600, 120), "refresh": ("页面刷新", "秒", 10, 3600, 60),
             "hide_done_hours": ("车道收起", "小时", 1, 720, 24), "auto_groups": ("自动归组", "", None, None, True)}
ALERT_KEYS = {"gpu_idle_util": ("GPU 空闲线", "%", 0, 100, 5.0), "gpu_idle_min": ("GPU 空闲多久告警", "分钟", 1, 1440, 15),
              "stale_min": ("数据多久算过期", "分钟", 1, 1440, 5), "log_silent_min": ("日志多久没更新告警", "分钟", 0, 1440, 0)}
INT_KEYS = {"interval", "refresh", "gpu_idle_min", "stale_min", "log_silent_min"}
STALE_RUNS, STALE_PAD_S = 3, 60        # 数据过期至少等这么多次采集，再加这么多秒余量（stale_limit）


def stale_limit(interval: int, stale_min: int) -> int:
    """项目快照多久没更新算过期（秒）：max(3×采集间隔, stale_min 分钟) + 1 分钟。labmon wait 的 collector_stale、项目页顶栏标红、
    「项目配置」那一行的说法都按它（项目快照的 thresholds.stale_s、serve.cfg_info 的 stale）；interval 取 monitor.toml 的。"""
    return max(STALE_RUNS * interval, stale_min * 60) + STALE_PAD_S
TITLE_MAX, LABEL_MAX = 60, 40          # 标题、计划项与里程碑名字的最多字数
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
# 自动归组（auto_groups）：不属于任何 [[groups]] 的任务按名字前缀（第一个 _ 或 - 及之前，如 x5_）成组，组 id 就是这个前缀。
# collect._group_of 按它归组；set_project 把自动组写成 [[groups]] 时按它认 id（#150）


def cfg_spec() -> dict:
    """页面上能改的项的说法、单位、范围、是否整数、缺省（serve.cfg_info 的 spec，页面按它画档位、当场校验）。"""
    spec = {k: {"name": n, "unit": u, "lo": lo, "hi": hi, "int": k in INT_KEYS, "dft": d} for k, (n, u, lo, hi, d) in {**PROJ_KEYS, **ALERT_KEYS}.items()}
    spec["title"]["max"] = TITLE_MAX
    spec["label"] = {"name": "名字", "max": LABEL_MAX}
    return spec


def _proj_val(k: str, v, spec: tuple):
    """页面提交的一项换成要写的值，不对抛 ConfigError。"""
    name, unit, lo, hi = spec[:4]
    if k == "title":
        v = str(v or "").strip()
        if not v or len(v) > TITLE_MAX:
            raise ConfigError(f"标题不能空，最多 {TITLE_MAX} 个字", k)
        return v
    if k == "auto_groups":
        if not isinstance(v, bool):
            raise ConfigError("自动归组只能开或关", k)
        return v
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}要填数：{v!r}", k) from None
    if not lo <= x <= hi:
        raise ConfigError(f"{name}要在 {lo}–{hi} {unit}之间", k)
    if k in INT_KEYS:
        if x != int(x):
            raise ConfigError(f"{name}要填整数", k)
        return int(x)
    return int(x) if x == int(x) else x


def _append_blocks(lines: list[str], head: str, rows: list[dict]) -> list[str]:
    """在最后一个 head 块后面（没有就在文件末尾）接上新块，每块一个空行隔开，键按 rows 里的先后写（值的写法同 _set_keys）。"""
    if not rows:
        return lines
    bl = _blocks(lines, head)
    at = bl[-1][1] if bl else len(lines)
    new = [x for kv in rows for x in ["\n", head + "\n"] + [f"{k} = {_toml_val(v)}\n" for k, v in kv.items()]]
    if not bl and (not lines or not lines[-1].strip()):
        new = new[1:]
    return lines[:at] + new + lines[at:]


def _move_groups(lines: list[str], order: list[str], colors: dict) -> list[str]:
    """[[groups]] 各块按 order（计划项 id）重排：第 n 个块的位置放新顺序的第 n 块，块之间的空行、注释与别的段不动。
    没写 color 的补上此刻的颜色，免得缺省按顺序取的调色板跟着顺序变。order 要正好是现有的这些 id。"""
    bl = _blocks(lines, "[[groups]]")
    if sorted(order) != sorted(b[2] for b in bl if b[2]):
        raise ConfigError("计划项对不上 monitor.toml 里的 [[groups]]（刚被改过？刷新页面再试）", "groups")
    if order == [b[2] for b in bl]:
        return lines
    segs = {}
    for s, end, gid in bl:
        block = lines[s:end]
        if not any(re.match(r"\s*color\s*=", l) for l in block[1:]) and colors.get(gid):
            block = block[:1] + _set_keys(block[1:], {"color": colors[gid]})
        segs[gid] = block
    # 块按位置一一换成新顺序的块：块之间原有的空行、注释、别的段都留在原处
    out, at = [], 0
    for (s, end, _), gid in zip(bl, order):
        out += lines[at:s] + segs[gid]
        at = end
    return out + lines[at:]


@_hosts_edit
def set_project(path: str | Path, changes: dict, why: str | None = None, ev: dict | None = None) -> list[str]:
    """页面上改项目的 monitor.toml（#122）：只改相关的行、保留注释，改完按 load 整套校验再原子写入，记一条配置改动。
    changes 可有 PROJ_KEYS 里的顶层键、alerts {ALERT_KEYS 里的}、groups [{id, label, color（可选，#rrggbb）}]（按新顺序；带 auto: true 与 color 的是自动归出的组，
    写成新的 [[groups]]：id 与 prefix 都是它的前缀，#150）、markers [{date, label}]（整组换掉）。
    出错抛的 ConfigError 带 field（出错的那一项），页面标到那一行。
    返回逐项说明（没变的不算）；什么都没变返回 []、不写。id、out_dir、[[hosts]]、[[queues]] 与写明计划项的归组规则不在这里改。"""
    p = Path(path).expanduser().resolve()
    old = p.read_text()
    cfg = load(p, old)
    lines, said = old.splitlines(keepends=True), []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    bad = [k for k in changes if k not in PROJ_KEYS and k not in ("alerts", "groups", "markers")]
    if bad:
        raise ConfigError(f"页面上不能改 {'、'.join(bad)}", bad[0])
    top = {}
    for k in [k for k in PROJ_KEYS if k in changes]:                  # 说明按固定顺序，不随页面交上来的先后
        v = _proj_val(k, changes[k], PROJ_KEYS[k])
        if v != cfg.get(k):
            top[k] = v
            name, unit = PROJ_KEYS[k][:2]
            f = lambda x: ("开" if x else "关") if k == "auto_groups" else f"{x}{(' ' + unit) if unit else ''}"   # noqa: E731
            said.append(f"{name} {f(cfg.get(k))} → {f(v)}")
    if top:
        lines = _set_keys(lines, top, p)
    al = {}
    ch = changes.get("alerts") or {}
    bad = [k for k in ch if k not in ALERT_KEYS]
    if bad:
        raise ConfigError(f"页面上不能改告警的 {'、'.join(bad)}", bad[0])
    for k in [k for k in ALERT_KEYS if k in ch]:
        v = _proj_val(k, ch[k], ALERT_KEYS[k])
        if v != cfg["alerts"].get(k):
            al[k] = v
            name, unit = ALERT_KEYS[k][:2]
            said.append(f"{name} {cfg['alerts'].get(k)} → {v} {unit}")
    if al:
        b = _blocks(lines, "[alerts]")
        if b:
            s, end, _ = b[0]
            lines = lines[:s + 1] + _set_keys(lines[s + 1:end], al, p) + lines[end:]
        elif any(re.match(r"\s*alerts\s*=", l) for l in lines):
            raise ConfigError(f"{p} 里 alerts 写成了内联表，改成 [alerts] 段再在页面上改", next(iter(al)))
        else:
            lines += ["\n", "[alerts]\n"] + _set_keys([], al, p)
    if "groups" in changes:
        gs = [(str(g.get("id")), str(g.get("label") or "").strip(), g.get("color"), bool(g.get("auto"))) for g in changes["groups"] or []]
        now, add = {g["id"]: g for g in cfg["groups"]}, []
        for gid, label, color, auto in gs:
            if not label or len(label) > LABEL_MAX:
                raise ConfigError(f"计划项的名字不能空，最多 {LABEL_MAX} 个字", "groups")
            if color is not None and not COLOR_RE.fullmatch(str(color)):
                raise ConfigError(f"计划项的颜色要写成 #rrggbb：{color!r}", "groups")
            if auto:                 # 自动归出的组写成 [[groups]]（#150）：id 即前缀，钉住此刻的颜色
                if gid in now or any(a["id"] == gid for a in add):
                    raise ConfigError(f"计划项 {gid} 已经写在 monitor.toml 里了（刚被改过？刷新页面再试）", "groups")
                if not AUTO_GROUP_RE.fullmatch(gid) or color is None:
                    raise ConfigError(f"{gid!r} 不是自动归出的计划项（要带前缀与颜色）", "groups")
                add.append({"id": gid, "label": label, "prefix": gid, "color": str(color)})
                continue
            if gid not in now:
                raise ConfigError(f"monitor.toml 里没有计划项 {gid}（刚被改过？刷新页面再试）", "groups")
            kv = {"label": label} if label != now[gid]["label"] else {}
            if color is not None and str(color).lower() != str(now[gid]["color"]).lower():
                kv["color"] = str(color)
            if kv:
                s, end, _ = next(b for b in _blocks(lines, "[[groups]]") if b[2] == gid)
                lines = lines[:s + 1] + _set_keys(lines[s + 1:end], kv, p) + lines[end:]
                if "label" in kv:
                    said.append(f"计划项 {gid} 改名 {now[gid]['label']} → {label}")
                if "color" in kv:
                    said.append(f"计划项 {label} 换色 {now[gid]['color']} → {kv['color']}")
        order = [gid for gid, _, _, _ in gs]
        # [[groups]] 的先后也是归组匹配的先后：写明组要是用 prefix、又以新组的前缀开头，新组排在它前面就会抢走它的任务（match 的判断不了，同写明组之间换顺序）
        pos = {gid: i for i, gid in enumerate(order)}
        for a in add:
            hit = next((g for g in cfg["groups"] if str(g.get("prefix", "")).startswith(a["id"]) and pos.get(g["id"], -1) > pos[a["id"]]), None)
            if hit:
                raise ConfigError(f"自动组 {a['label']} 排在 {hit['label']} 前面会把它的任务（前缀 {hit['prefix']}）归过来：放到它后面再保存", "groups")
        if add:
            lines = _append_blocks(lines, "[[groups]]", add)
            said += [f"自动归出的计划项 {a['label']} 写成 [[groups]]（prefix = {a['prefix']}，颜色 {a['color']}）" for a in add]
        if order != [g["id"] for g in cfg["groups"]] + [a["id"] for a in add]:
            lines = _move_groups(lines, order, {g["id"]: g["color"] for g in cfg["groups"]})
            said.append("计划项顺序改成 " + "、".join(label for _, label, _, _ in gs))
    if "markers" in changes:
        ms = []
        for m in changes["markers"] or []:
            d, label = str(m.get("date") or "").strip().replace("T", " "), str(m.get("label") or "").strip()
            if parse_time(d) is None:
                raise ConfigError(f"里程碑日期格式不对：{d or '没填'}（YYYY-MM-DD 或 YYYY-MM-DD HH:MM）", "markers")
            if len(label) > LABEL_MAX:
                raise ConfigError(f"里程碑名字最多 {LABEL_MAX} 个字", "markers")
            ms.append((d, label or d))
        was = [(m["date"], m["label"]) for m in cfg["markers"]]
        if ms != was:
            lines = _set_markers(lines, ms)
            said.append("里程碑改成 " + ("、".join(f"{d} {lb}" if lb != d else d for d, lb in ms) if ms else "没有"))
    if not said:
        return []
    _write_checked(p, "".join(lines), why or f"看板页面上改了 {cfg['id']} 的 monitor.toml：" + "；".join(said),
                   {"host": None, "hosts": None, "project": cfg["id"], **(ev or {})}, check=lambda t: load(p, t))
    return said


def _set_markers(lines: list[str], ms: list[tuple[str, str]]) -> list[str]:
    """[[markers]] 整组换成 ms：已有的块逐个改 date、label（多的删掉，连同前面紧挨着的空行），不够的接在最后一块后面（没有就加在文件末尾）。"""
    bl = _blocks(lines, "[[markers]]")
    for s, end, _ in reversed(bl[len(ms):]):             # 先删多的（从后往前），再改留下的：改块会插行，删的不能按改前的行号（Codex 审查 #128）
        while s > 0 and not lines[s - 1].strip():
            s -= 1
        lines = lines[:s] + lines[end:]
    bl = _blocks(lines, "[[markers]]")
    for (s, end, _), (d, label) in reversed(list(zip(bl, ms))):
        lines = lines[:s + 1] + _set_keys(lines[s + 1:end], {"date": d, "label": label}) + lines[end:]
    return _append_blocks(lines, "[[markers]]", [{"date": d, "label": label} for d, label in ms[len(bl):]])


def _norm_host(h: dict) -> dict:
    """项目里的一台主机补全（load 与 with_queues 共用）：缺省值、归还时刻、挂载点、自定义检查。"""
    h = host_defaults(h)
    h.setdefault("ssh", None)            # 缺省即本机：直接用本机 Python 跑探针
    # 已归还（借来的机器还回去了）：不再连接、不报这台机器的告警；值为归还时刻（epoch 秒，清单里写的文字经 parse_time），页面据此标注
    raw = h.get("returned")
    h["returned"] = parse_time(raw) if raw else None
    if raw and h["returned"] is None:        # 认不出就报错，不然这台会被当成在用、又去连
        raise ConfigError(f"主机 {h['id']} 的 returned「{raw}」认不出：写成 2026-11-04 18:40 这样")
    h["mounts"] = [m if isinstance(m, dict) else {"path": m} for m in h.get("mounts", [])]
    for m in h["mounts"]:
        m.setdefault("label", m["path"])
    for c in h.setdefault("checks", []):
        if "id" not in c or "cmd" not in c:
            raise ConfigError(f"主机 {h['id']} 的 [[hosts.checks]] 需要 id 与 cmd")
        c.setdefault("label", c["id"])
        c.setdefault("detail", [])
    return h


def _norm_queue(q: dict, hosts: list[dict], hide: float = 24) -> dict:
    """一条队列补全（load 与 with_queues 共用）：自动发现、缺省值、所在主机、已归还时经哪台代读（_reader）。
    hide：项目顶层的 hide_done_hours（队列自己没写时用它，缺省 24）。"""
    if "id" not in q:
        raise ConfigError("[[queues]] 缺 id")
    q = dict(q)
    # root 或 jobs 含通配符：自动发现，每个匹配的清单一条车道。车道名由采集时按主机编号（collect._lane_specs），
    # label 只是可选的用途（实验之外的角色，如“训练方”），不写实验名
    q["discover"] = any(c in str(q.get("root", "")) + str(q.get("jobs", "")) for c in "*?[")
    q.setdefault("hide_done_hours", hide)    # 不是 monitor.toml 写明的车道：没有在跑与排队的、这么久没有活动就收起（collect._quiet）
    q.setdefault("type", "gpu_queue")
    q.setdefault("host", hosts[0]["id"])      # 项目里写的 host 已在 _resolve_hosts 转成字符串
    if q["host"] not in [h["id"] for h in hosts]:
        raise ConfigError(f"队列 {q['id']} 的 host={q['host']} 既不在 [[hosts]] 里，也不在 {hosts_file()} 里")
    q["read_via"] = _reader(q, hosts)
    return q


def with_queues(cfg: dict, queues: list[dict]) -> dict:
    """项目配置换一组队列（collect.reg_queues 按 worker 登记拼的），返回新配置、不改原来的：队列用到、项目里还没有的主机
    从用户级主机清单取（同 _resolve_hosts），已在的主机不动；队列在已归还的主机上、项目里又没有能代读它的在用主机时，
    也从清单里补一台（_reader 的规则）；队列补全同 load（主机换了，代读的主机跟着重算）。用户级主机清单里也没有的主机，它的队列丢掉。
    算力池里的登记（host 为 POOL，#24）：补一台合成主机 {id: pool, pool: true}（不探测），代读它的在用主机项目里没有时补进来、标 reader_only。"""
    hosts, ids = list(cfg["hosts"]), {h["id"] for h in cfg["hosts"]}
    pool: list = []                                   # 用户级主机清单里、项目还没有的主机（补全过），要用时才读

    def more() -> list:
        if not pool:
            pool.extend(_norm_host(_merge_host(h, {})) for h in user_hosts() if "id" in h and str(h["id"]) not in ids)
        return [h for h in pool if h["id"] not in ids]

    def add(hid: str) -> None:
        h = next((x for x in more() if x["id"] == hid), None)
        if h:
            hosts.append(h)
            ids.add(hid)
    for q in queues:
        if q.get("host") is not None and q["host"] not in ids:
            add(q["host"])
    home = pool_home() if any(q.get("host") == POOL for q in queues) and POOL not in ids else None
    if home:                                          # 算力池（#24）：一台不探测的合成主机，池车道由同 worker_home 的在用主机只读代读
        hosts.append(_norm_host({"id": POOL, "label": POOL_LABEL, "pool": True, "worker_home": home}))
        ids.add(POOL)
        if _reader({"host": POOL}, hosts) is None and (r := home_host(home, [h for h in more() if not h["returned"]])):
            hosts.append(dict(r, reader_only=True))   # 不是本项目的主机，只替池代读：不进项目快照（collect._hosts）
            ids.add(r["id"])
    for q in queues:
        if q.get("host") in ids and next(h for h in hosts if h["id"] == q["host"])["returned"] and _reader(q, hosts) is None:
            r = _reader(q, hosts + more())
            if r:
                add(r)
    return dict(cfg, hosts=hosts, queues=[_norm_queue(q, hosts, cfg["hide_done_hours"]) for q in queues if q.get("host") is None or q["host"] in ids])


def sources(cfg: dict) -> list[str]:
    """决定这份配置内容的文件：项目配置，以及用到的用户级主机清单（服务据此判断要不要重读）。"""
    return [cfg["config_path"]] + ([cfg["hosts_file"]] if cfg.get("hosts_file") else [])


def done_key(src: str) -> str:
    """done_when 指的文件在登记目录 files/ 下的位置（#161）：monitor.toml 里写的相对路径原样用；绝对路径、~ 开头、带 .. 的换成 _abs/<哈希>-<文件名>，不会写出 files/。"""
    p = PurePosixPath(src)
    if src.startswith("~") or p.is_absolute() or ".." in p.parts or not p.parts:
        return f"_abs/{hashlib.sha1(src.encode()).hexdigest()[:10]}-{p.name or 'file'}"
    return str(p)


def done_files(cfg: dict) -> list[str]:
    """配置里 done_when 用到的文件（monitor.toml 里写的原文，去重保序）：随 labmon project push 上传的就是这些。"""
    return list(dict.fromkeys(g["done_when"]["src"] for g in cfg.get("groups", []) if g.get("done_when")))


def _proj_id(cfg: dict, base: Path) -> str:
    """项目 id：看板地址 /p/<id>/、登记目录名，也是 worker 的项目名（worker add --project、pause、focus）；没写时沿用以前的缺省：out_dir 的目录名，再没有是 default。"""
    od = cfg.get("out_dir")
    return str(cfg.get("id") or (Path(_expand(str(od), base)).name if od else "default"))


def brief(path: str | Path, text: str | None = None) -> dict:
    """命令行在项目目录里读本地这份 monitor.toml（#162）：只做 TOML 解析，取项目 id、采集间隔、stale_min（wait 连不上看板多久提示一次、连回来报 server_back，
    与 collector_stale 同一门槛）与 done_when 的文件 {monitor.toml 里写的路径: 本地路径}。不做整套校验：那要用户级主机清单，只在看板服务那边（load）；
    推上去由服务端校验。语法不对抛 ConfigError；类型不对的取缺省、不抛（id 照样 str 一下）。"""
    path = Path(path).expanduser().resolve()
    try:
        d = _read_toml(path) if text is None else tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"读不了配置 {path}：{e}") from e
    al = d.get("alerts") if isinstance(d.get("alerts"), dict) else {}
    num = lambda v, k: v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else k[4]   # noqa: E731
    done = {}
    for g in d.get("groups") if isinstance(d.get("groups"), list) else []:     # 类型不对的不在这里抛：推上去由看板校验（400，wait 当一条推不上报）
        dw = g.get("done_when") if isinstance(g, dict) else None
        if isinstance(dw, dict) and dw.get("file"):
            done[str(dw["file"])] = _expand(str(dw["file"]), path.parent)
    return {"id": _proj_id(d, path.parent), "interval": num(d.get("interval"), PROJ_KEYS["interval"]),
            "stale_min": num(al.get("stale_min"), ALERT_KEYS["stale_min"]), "done": done}


def load(path: str | Path, text: str | None = None, files: Path | None = None) -> dict:
    """读项目配置并补全缺省值、校验（不对抛 ConfigError）。text 给了就按它、不读文件（页面上改配置、推上来的配置先校验文本）。
    files：看板上登记的项目（projects.load_cfg）给 files/ 目录，done_when 的文件取那里的（done_key）；不给按配置文件所在目录找（项目目录里的工作副本）。
    out_dir 与 [local] 不再生效（#161：输出目录固定在登记目录的 out/；「本机」一栏 #164 删了）：读到了从配置里拿掉、记进 ignored，labmon check 提示。"""
    path = Path(path).expanduser().resolve()
    if text is None:
        cfg = _read_toml(path)
    else:
        try:
            cfg = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"读不了配置 {path}：{e}") from e
    base = path.parent
    cfg["config_path"] = str(path)
    cfg["base_dir"] = str(base)
    _resolve_hosts(cfg)
    for k in ("title", "interval", "refresh", "hide_done_hours", "auto_groups"):   # 缺省值都在 PROJ_KEYS、ALERT_KEYS（页面经 cfg_info 的 spec 取同一份）
        cfg.setdefault(k, PROJ_KEYS[k][4])
    cfg["ignored"] = [k for k in ("out_dir", "local") if k in cfg]
    cfg["id"] = _proj_id(cfg, base)
    cfg.pop("out_dir", None)
    cfg.pop("local", None)
    cfg.setdefault("history_hours", 24)      # interval 采集间隔（秒）、refresh 页面自刷新间隔（秒）；hide_done_hours：不是 monitor.toml 写明的车道跑完这么久没动就收起，[[queues]] 里单条写的优先

    hosts = cfg["hosts"]
    queues = cfg.setdefault("queues", [])
    if not hosts and queues:                 # 不写主机的队列缺省在本机；[[queues]] 可选（车道按 worker 登记生成，collect.reg_queues），都没写时不补
        hosts.append({"id": "local"})
    hosts[:] = [_norm_host(h) for h in hosts]
    host_ids = [h["id"] for h in hosts]
    if len(set(host_ids)) != len(host_ids):
        raise ConfigError("主机 id 重复")
    queues[:] = [_norm_queue(q, hosts, cfg["hide_done_hours"]) for q in queues]
    if len({q["id"] for q in queues}) != len(queues):
        raise ConfigError("队列 id 重复")

    # auto_groups：不属于任何计划项的任务按名字前缀（第一个 _ 或 - 之前）自动归组
    for i, g in enumerate(cfg.setdefault("groups", [])):
        if "id" not in g or not ("prefix" in g or "match" in g):
            raise ConfigError("[[groups]] 需要 id，以及 prefix 或 match（正则）之一")
        g.setdefault("label", g["id"])
        g.setdefault("color", PALETTE[i % len(PALETTE)])
        dw = g.get("done_when")
        if dw:
            if "file" not in dw or "pattern" not in dw:
                raise ConfigError(f"计划项 {g['id']} 的 done_when 需要 file 与 pattern")
            dw["src"] = str(dw["file"])                   # monitor.toml 里写的原文（推的时候按它找本地文件、定 files/ 下的位置）
            dw["file"] = str(files / done_key(dw["src"])) if files is not None else _expand(dw["src"], base)

    # 里程碑（截稿日等）：画在时间线上，顶部指标块显示离下一个里程碑的余量。只写日期时取当天结束（23:59:59）
    for m in cfg.setdefault("markers", []):
        if "date" not in m:
            raise ConfigError("[[markers]] 需要 date（YYYY-MM-DD 或 YYYY-MM-DD HH:MM）")
        d = str(m["date"]).replace("T", " ")          # 也接受 TOML 原生日期 / 日期时间
        m["ts"] = parse_time(d)
        if m["ts"] is None:
            raise ConfigError(f"里程碑日期格式不对：{m['date']}")
        m["date"] = d
        m.setdefault("label", d)

    al = cfg.setdefault("alerts", {})
    for k, spec in ALERT_KEYS.items():       # log_silent_min >0 时：运行中任务的日志这么久没更新就告警
        al.setdefault(k, spec[4])
    return cfg
