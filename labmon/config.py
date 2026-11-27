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
from .worker.pure import GIB, GROUP_RE as AUTO_GROUP_RE, HOLD_RE, POOL, POOL_ID_RE, POOL_PREFIX, STALE_S, is_pool_id, pool_host_id, pool_id_of  # noqa: F401 — 与 worker 共用唯一实现

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


def check_fleet_manage(hf: Path, item: dict) -> None:
    """[[fleet.servers]] 的 manage 字段校验（#224）：缺省或不写跟随账号状态自动接入，写时须为布尔值。"""
    if "manage" in item and not isinstance(item["manage"], bool):
        raise ConfigError(f"{hf} 里 {item.get('id')} 的 manage 只能写 true 或 false，写的是 {item['manage']!r}")


def _ssh_port_pair(h: dict) -> tuple[str, int] | None:
    ssh = str(h.get("ssh") or "").strip().lower()
    if not ssh:
        return None
    try:
        port = int(h.get("port") or PORT)
    except (TypeError, ValueError):
        return None
    return ssh, port


def _fleet_conflict_hosts(hosts: list[dict]) -> tuple[set[str], set[tuple[str, int]]]:
    """[[hosts]] 已显式配置的主机 id 集合与在用 (ssh, port) 集合：组内服务器命中任一项时尊重 [[hosts]] 不重复覆盖。"""
    ids = {str(h["id"]) for h in hosts if "id" in h}
    pairs = {p for h in hosts if not h.get("returned") and (p := _ssh_port_pair(h))}
    return ids, pairs


def _member_account(holder: str | None, links: dict[str, tuple[str | None, str | None]]) -> str | None:
    """按 users.people_of() 反查持有人 holder 无误绑定的唯一账号名；未开账号或绑定有误返回 None。"""
    if not holder:
        return None
    for acct, (person, err) in links.items():
        if not err and person == holder:
            return acct
    return None


def member_owner(h: dict | None) -> str | None:
    """自动接入组员服务器（member_auto，#224/#236）的归属账号名（小写）；非 member_auto 返回 None。"""
    if not h or not h.get("member_auto"):
        return None
    acc = h.get("member_account")
    if isinstance(acc, str) and acc.strip():
        return acc.strip().lower()
    us = h.get("users")
    if isinstance(us, list) and us and str(us[0]).strip():
        return str(us[0]).strip().lower()
    return None


def is_other_member_auto(h: dict | None, who=None) -> bool:
    """判断主机 h 是否属于他人（非 who 本人）的自动接入组员服务器（唯一实现，#236）：
    - 非 member_auto 主机一律返回 False；
    - who 为 None 时（不针对特定本人，即全局管理员自用视角），所有 member_auto 均视为他人私有返回 True；
    - who 为账号名或带 .name 的 Ident 时，归属账号与 who 一致（如普通组员本人，或管理员本人恰好持有该服务器）返回 False，其余返回 True。"""
    if not h or not h.get("member_auto"):
        return False
    if who is None:
        return True
    name = (who.name if hasattr(who, "name") else str(who)).strip().lower()
    owner = member_owner(h)
    return not owner or owner != name


def own_hosts(who=None, *, hosts: list[dict] | None = None) -> list[dict]:
    """在用主机中排除他人私有的 member_auto 主机（唯一实现，#236）：管理员总览/CLI 默认主机列表共用。"""
    src = active_hosts() if hosts is None else hosts
    return [h for h in src if not is_other_member_auto(h, who)]


def stopped_blocks_relaunch(stopped: str | None, *, member_auto: bool = False) -> bool:
    """hosts/<id>/stopped 标记是否应阻止看板自动拉起 worker（唯一实现，#236）：
    - 空值或 auto:*（切仅监控 / 删账号 / 借用归还等生命周期停机）：不阻断自动拉起；
    - manual:*（手动 labmon worker stop 写入）：一律阻断自动拉起；
    - 旧版纯时间戳（无 manual:/auto: 前缀）：对普通自用主机保持兼容仍阻断，对 member_auto 不阻断，
      避免历史借用归还/旧版停机留下的纯时间戳永久锁死新接入的组员服务器。"""
    s = str(stopped or "").strip()
    if not s or s.startswith("auto:"):
        return False
    if s.startswith("manual:"):
        return True
    return not member_auto


def relaunch_state(h: dict | None, *, member_auto: bool | None = None, relaunch_rec=None) -> dict:
    """单台主机此刻自动拉起 worker 的唯一状态判定（overview.relaunch 与 config.member_manage_state 共用，#236）。
    返回 {can_relaunch, blocked, code, why}：
    - running：worker 正常运行中；
    - pending：可自动拉起，等待下一次拉起；
    - relaunch_failed：可自动拉起，但最近一次拉起退出码非 0（等待冷却后重试）；
    - manual_stopped / ssh_error / status_error / lan_off / not_auto / ephemeral：当前阻断或不自动拉起。"""
    if h is None:
        return {"can_relaunch": True, "blocked": False, "code": "pending", "why": "等待自动拉起 worker"}
    if h.get("ephemeral"):
        return {"can_relaunch": False, "blocked": False, "code": "ephemeral", "why": "临时主机不自动拉起"}
    if h.get("worker_up") and (h.get("status") or member_auto):
        return {"can_relaunch": False, "blocked": False, "code": "running", "why": "worker 运行中"}
    is_ma = bool(h.get("member_auto")) if member_auto is None else bool(member_auto)
    auto = bool(h.get("auto")) if "auto" in h else is_ma
    if not auto:
        return {"can_relaunch": False, "blocked": True, "code": "not_auto", "why": "未开启自动拉起"}
    if h.get("lan_off"):
        return {"can_relaunch": False, "blocked": True, "code": "lan_off", "why": "看板当前不在内网，暂无法自动拉起 worker"}
    if h.get("error"):
        return {"can_relaunch": False, "blocked": True, "code": "ssh_error", "why": f"主机连不上（{h['error']}），暂无法自动拉起 worker"}
    if h.get("status_error"):
        return {"can_relaunch": False, "blocked": True, "code": "status_error", "why": f"worker 状态文件异常（{h['status_error']}），暂不自动拉起"}
    stopped = h.get("stopped")
    if stopped_blocks_relaunch(stopped, member_auto=is_ma):
        ts_txt = str(stopped).strip()
        if ts_txt.startswith("manual:"):
            ts_txt = ts_txt[len("manual:"):].strip()
        hid = str(h.get("id") or "")
        hint = f"可点击「恢复拉起」或运行 labmon worker start {hid}" if is_ma else f"需运行 labmon worker start {hid} 恢复"
        return {"can_relaunch": False, "blocked": True, "code": "manual_stopped",
                "why": f"已手动停用 worker（{ts_txt}），不自动拉起（{hint}）"}
    if isinstance(relaunch_rec, dict) and relaunch_rec.get("rc"):
        err = str(relaunch_rec.get("err") or "").strip()
        why = f"上次自动拉起失败（退出码 {relaunch_rec['rc']}）" + (f"：{err}" if err else "") + "，10 分钟后重试"
        return {"can_relaunch": True, "blocked": False, "code": "relaunch_failed", "why": why}
    return {"can_relaunch": True, "blocked": False, "code": "pending", "why": "等待自动拉起 worker"}


def member_manage_state(s: dict, hosts: list[dict], *, links: dict | None = None, off: dict | None = None,
                        snap_e: dict | None = None, relaunch_rec=None) -> dict:
    """单台 [[fleet.servers]] 自动接入本人调度的状态唯一判定（#224/#236）：
    user_hosts()、fleetview.view()、fleet.set_member_manage() 共用；开启后的拉起状态统一复用 relaunch_state()。"""
    if links is None:
        from .users import people_of
        links = people_of()
    if off is None:
        from .fleet import off_servers
        off = off_servers()
    sid = str(s.get("id") or "")
    holder = str(s.get("holder") or "")
    acct = _member_account(holder, links)
    ids, pairs = _fleet_conflict_hosts(hosts)
    pair = _ssh_port_pair(s)
    hosted = sid in ids or (pair is not None and pair in pairs)
    is_off = sid in off
    is_borrowed = bool(s.get("borrow_since")) and not s.get("borrow_returned")
    opt_out = s.get("manage") is False
    worker_up = bool((snap_e or {}).get("worker_up"))
    if hosted:
        return {"on": False, "auto": False, "opt_out": False, "hosted": True, "account": acct,
                "can_toggle": False, "worker_up": worker_up, "state": "hosted", "blocked": False,
                "why": "已在主机清单 [[hosts]] 单独配置"}
    if is_borrowed:
        return {"on": False, "auto": False, "opt_out": opt_out, "hosted": False, "account": acct,
                "can_toggle": False, "worker_up": worker_up, "state": "borrowed", "blocked": False,
                "why": "当前处于借用中，归还后按本人设置恢复"}
    if is_off:
        return {"on": False, "auto": False, "opt_out": opt_out, "hosted": False, "account": acct,
                "can_toggle": False, "worker_up": False, "state": "off", "blocked": False, "why": "已关机"}
    if not acct:
        return {"on": False, "auto": False, "opt_out": opt_out, "hosted": False, "account": None,
                "can_toggle": False, "worker_up": False, "state": "no_account", "blocked": False,
                "why": "未开通看板账号，保持只读"}
    if opt_out:
        return {"on": False, "auto": False, "opt_out": True, "hosted": False, "account": acct,
                "can_toggle": True, "worker_up": False, "state": "opt_out", "blocked": False,
                "why": "已切为仅监控（不自动拉起 worker）"}
    rs = relaunch_state(snap_e, member_auto=True, relaunch_rec=relaunch_rec)
    if rs["code"] == "running":
        why = "已接入本人调度（worker 运行中）"
    elif rs["code"] == "pending":
        why = "已接入本人调度（等待自动拉起 worker）"
    else:
        why = f"已接入本人调度，但{rs['why']}"
    return {"on": True, "auto": True, "opt_out": False, "hosted": False, "account": acct,
            "can_toggle": True, "worker_up": worker_up, "state": rs["code"], "blocked": bool(rs["blocked"]),
            "why": why}


def member_host(s: dict, hosts: list[dict], acct: str, fleet_cfg: dict | None = None) -> dict:
    """已开账号的组员名下 [[fleet.servers]] 自动接入本人调度时的受管主机条目（#224/#236）：
    复用模板或 [fleet] 的 SSH/python/data_root/worker_home/shared/mounts/checks，不继承 ka_bin（不碰用户自开保活），
    仅开放给持有人本人（users=[acct]，member_account=acct），普通账号另标 member_restrict（worker 启动时启用项目白名单硬隔离，防遗留队列越权放行），
    不写 platform_job/platform_pool 以免被误当成 [[hosts]] 自用机。"""
    from .fleet import state_file                 # fleet 在上层 import 本模块，这里只能延迟 import
    from .users import account as _account
    tpl = _guest_tpl(hosts)
    fcfg = fleet_cfg or {}
    base = {k: tpl[k] for k in ("python", "data_root", "worker_home", "shared", "mounts", "checks") if k in tpl}
    for k in ("python", "data_root", "worker_home", "shared"):
        if fcfg.get(k):
            base[k] = fcfg[k]
        if s.get(k):
            base[k] = s[k]
    node = str(s.get("ssh", "")).rsplit(".", 1)[-1] or str(s["id"])
    lbl = str(s.get("label") or "").strip()
    holder = str(s.get("holder") or "")
    label = lbl if lbl and lbl != holder else f"{node}（{holder}）" if holder else node
    acc = _account(acct)
    out = {**base,
           "id": str(s["id"]), "label": label, "ssh": s.get("ssh"), "port": s.get("port", PORT),
           "known_hosts": str(state_file("known_hosts")), "worker": "auto",
           "users": [acct], "share": "private", "member_auto": True, "member_holder": holder,
           "member_account": acct, "member_restrict": (acc or {}).get("role") != "admin"}
    if fcfg.get("lease_days") is not None:
        out["lease_days"] = fcfg["lease_days"]
    return out


def user_hosts() -> list[dict]:
    """用户级主机清单里的主机（还没补缺省值；已归还的也在，带 returned）：[[hosts]]，加上借过的组内服务器（guest_host），
    以及已开通看板账号且未显式退出的组员名下服务器（member_host，#224）。
    借用中由看板拉起 worker，还了（borrow_returned）若持有人已开账号且未退出则恢复为 member_host，否则和归还的自用主机一样不再连接。
    borrow 字段：{holder, since, until, for, light（借来时对方在轻用）, away（对方说好不用到几点）}（since、until、away 是 epoch 秒；手写的 borrow_until 认不出时 until 为 None、原文在 until_bad，
    borrow.check 提醒一次）；自用主机写了 borrow_until 的（换给自己借来的）也有，holder 取 borrow_from，swap 为真。"""
    hf = hosts_file()
    if not hf.exists():
        return []
    d = _read_toml(hf)
    hosts = [dict(h) for h in d.get("hosts", [])]
    if any(is_pool_id(str(h.get("id"))) for h in hosts):
        bad = next(str(h.get("id")) for h in hosts if is_pool_id(str(h.get("id"))))
        raise ConfigError(f"{hf} 的 [[hosts]] 不能叫 {bad}：它是算力池的保留 id")
    for h in hosts:
        check_legacy_pool(hf, h)
        if h.get("borrow_until"):
            h["borrow"] = {"holder": h.get("borrow_from"), "since": None, **_until(h["borrow_until"]), "for": None, "swap": True}
    fleet_raw = d.get("fleet") or {}
    servers = fleet_raw.get("servers", [])
    for s in servers:
        check_legacy_pool(hf, s)
        check_fleet_manage(hf, s)
    extra: list[dict] = []
    if servers:
        from .fleet import off_servers
        from .users import people_of
        links, off = people_of(), off_servers()
        for s in servers:
            if s.get("borrow_since") and not s.get("borrow_returned"):
                extra.append(guest_host(s, hosts))
                continue
            ms = member_manage_state(s, hosts, links=links, off=off)
            if ms["on"] and ms["account"]:
                extra.append(member_host(s, hosts, ms["account"], fleet_raw))
            elif s.get("borrow_since"):
                extra.append(guest_host(s, hosts))
    return hosts + extra


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
    """一个 worker_home 共享域由哪台在用主机代读、代办：照主机清单顺序优先挑非 member_auto 的匹配在用主机（hosts 缺省 active_hosts），
    全为 member_auto 时再兜底挑第一台匹配机。
    home 可以是 home_key(h) 返回的元组，或共享盘 worker_home 路径字符串（仅匹配非独立主机，防独立主机默认路径撞名）。
    工作台读已归还主机留下的登记、移除已归还主机上的登记（workerctl.run_out 的 rm）都经它：没有就是 None。"""
    cand = [h for h in (active_hosts() if hosts is None else hosts) if _same_home(h, home)]
    return next((h for h in cand if not h.get("member_auto")), cand[0] if cand else None)


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


def _find_pool_block(lines: list[str], pid: str) -> list[tuple[int, int, str | None]]:
    """在 hosts.toml 各行中定位 [pools.<pid>]（兼容引号写法）块。"""
    return (_blocks(lines, f"[pools.{pid}]")
            or _blocks(lines, f'[pools."{pid}"]')
            or _blocks(lines, f"[pools.'{pid}']"))


@_hosts_edit
def remove_pool_member(name: str, by: str) -> list[str]:
    """删除账号时同步移除主机清单 [pools.*].members 里显式列出的该账号（#212，users.remove 调）：
    不触碰 "*" 通配项；无改动时不写文件。返回移除过该账号的共有池 id 列表。"""
    p, low = hosts_file(), name.strip().lower()
    if not low or not p.exists():
        return []
    raw_pools = _read_toml(p).get("pools")
    if not isinstance(raw_pools, dict) or not raw_pools:
        return []
    lines = p.read_text().splitlines(keepends=True)
    targets: list[tuple[int, int, str, list[str]]] = []
    for pid, cfg in raw_pools.items():
        if not isinstance(cfg, dict) or not isinstance(cfg.get("members"), list):
            continue
        cur = [str(x) for x in cfg["members"]]
        new = [x for x in cur if x.strip().lower() != low]
        if len(new) == len(cur):
            continue
        bl = _find_pool_block(lines, str(pid))
        if not bl:
            continue
        s, end, _ = bl[0]
        targets.append((s, end, str(pid), new))
    if not targets:
        return []
    rm: list[str] = []
    for s, end, pid, new in sorted(targets, key=lambda x: -x[0]):
        lines = lines[:s + 1] + _set_keys(lines[s + 1:end], {"members": new}) + lines[end:]
        rm.append(pid)
    rm.sort()
    _write_checked(p, "".join(lines), f"{by} 删账号 {name} 时移除共有池成员：{'、'.join(rm)}")
    return rm


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


POOL_KEYS = ("label", "hosts", "members", "exclusive")
POOL_RESERVED_IDS = frozenset({"new", "edit", "options", "save", "remove"})


def _check_pools_toml(text: str) -> dict:
    """整份主机清单与共有算力池校验（save_pool / remove_pool 共用）：先过 _check_hosts_toml，再过 pools() 唯一强校验。"""
    d = _check_hosts_toml(text)
    pools(d)
    return d


def _last_pool_end(lines: list[str]) -> int:
    """找到 hosts.toml 中最后一个 [pools.*] 块的结束行；没有时返回文件末尾。"""
    end = None
    for i, l in enumerate(lines):
        head = re.sub(r"\s*#.*$", "", l.strip())
        if head.startswith("[pools.") and head.endswith("]"):
            bl = _blocks(lines, head)
            if bl:
                end = max(end or 0, max(e for _, e, _ in bl))
    return end if end is not None else len(lines)


@_hosts_edit
def save_pool(pid: str, *, label: str | None = None, hosts: list[str], members: list[str] | None = None,
              exclusive: bool = True, create: bool = False, by: str = "看板", snap: dict | None = None) -> dict:
    """管理员新建或编辑共有型算力池 [pools.<id>]（#240，唯一落盘入口）：
    走 HOSTS_LOCK 原子改写 hosts.toml，复用 pools() 同套强校验（合法 id、非保留名、同 worker_home、
    单机单池互斥、禁独立机/借用机/本机入池）并校验显式成员账号存在；编辑已有池且仍有未完成清单时禁止切换到另一共享域。返回 {"pool", "changed", "msg"}。"""
    from .users import account as _account
    pid = str(pid or "").strip()
    if not POOL_ID_RE.fullmatch(pid):
        raise ConfigError("算力池 ID 不合法：须字母开头，仅含字母、数字、_ 或 -，最多 32 位")
    if pid.lower() in POOL_RESERVED_IDS:
        raise ConfigError(f"算力池 ID 不能使用保留名 {pid}")
    if label is not None:
        if not isinstance(label, str):
            raise ConfigError("算力池显示名称要是字符串")
        label = label.strip() or None
        if label and len(label) > LABEL_MAX:
            raise ConfigError(f"算力池显示名称最多 {LABEL_MAX} 个字")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(x, str) and x.strip() for x in hosts):
        raise ConfigError("至少选择一台常驻集群主机加入算力池")
    norm_hosts = list(dict.fromkeys(x.strip() for x in hosts))
    raw_members = [] if members is None else members
    if not isinstance(raw_members, list) or not all(isinstance(x, str) and x.strip() for x in raw_members):
        raise ConfigError("池成员要写成账号名列表（或 \"*\"）")
    norm_members = list(dict.fromkeys(x.strip().lower() for x in raw_members))
    if "*" in norm_members:
        norm_members = ["*"]
    else:
        missing = [m for m in norm_members if not _account(m)]
        if missing:
            raise ConfigError(f"池成员账号不存在：{'、'.join(missing)}（请先在「组内服务器 → 人员」开通账号）")
    if not isinstance(exclusive, bool):
        raise ConfigError("提交模式 exclusive 要是布尔值")
    p = hosts_file()
    raw = _read_toml(p) if p.exists() else {}
    raw_pools = raw.get("pools")
    if raw_pools is not None and not isinstance(raw_pools, dict):
        raise ConfigError(f"{p} 的 [pools] 要写成 [pools.<id>] 表")
    existing = raw_pools or {}
    if create and pid in existing:
        raise ConfigError(f"算力池 {pid} 已存在")
    if not create and pid not in existing:
        raise ConfigError(f"找不到算力池 {pid}")
    raw_hosts = raw.get("hosts") or []
    prev_hosts = set((existing.get(pid) or {}).get("hosts") or []) if not create else set()
    returned_ids = {str(h["id"]) for h in raw_hosts if isinstance(h, dict) and h.get("id") and h.get("returned")}
    bad_ret = [hid for hid in norm_hosts if hid in returned_ids and hid not in prev_hosts]
    if bad_ret:
        raise ConfigError(f"主机 {'、'.join(bad_ret)} 已归还，不能加入共有算力池")
    old_text = p.read_text() if p.exists() else ""
    lines = old_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    kv = {"label": label, "hosts": norm_hosts, "members": norm_members, "exclusive": exclusive}
    if create:
        if any(re.match(r"\s*%s\s*=" % re.escape(pid), l) for s, end, _ in _blocks(lines, "[pools]") for l in lines[s + 1:end]):
            raise ConfigError(f"{p} 里 [pools] 已含 {pid} 内联定义，请先清理或改写成 [pools.{pid}] 段")
        at = _last_pool_end(lines)
        block = [f"[pools.{pid}]\n"] + [f"{k} = {_toml_val(v)}\n" for k, v in kv.items() if v is not None]
        before = ["\n"] if at > 0 and lines[at - 1].strip() else []
        after = ["\n"] if at < len(lines) and lines[at].strip() else []
        new_lines = lines[:at] + before + block + after + lines[at:]
    else:
        bl = _find_pool_block(lines, pid)
        if not bl:
            raise ConfigError(f"{p} 里的算力池 {pid} 写成了内联表，请改写成 [pools.{pid}] 段后再在页面上编辑")
        s, end, _ = bl[0]
        new_lines = lines[:s + 1] + _set_keys(lines[s + 1:end], kv, p) + lines[end:]
    new_text = "".join(new_lines)
    parsed = _check_pools_toml(new_text)
    saved = pools(parsed)[pid]
    if not create:
        try:
            old_pm = pools(raw)
        except ConfigError:
            old_pm = {}
        old_pool = old_pm.get(pid)
        if old_pool and old_pool["worker_home"].rstrip("/") != saved["worker_home"].rstrip("/"):
            from .pool import pool_activity
            act = pool_activity(pid, snap=snap, pool_map=old_pm)
            if act["pending"] or act["placed"]:
                bits = [x for x in (f"{act['pending']} 条待放清单" if act["pending"] else "",
                                    f"{act['placed']} 条已落机清单" if act["placed"] else "") if x]
                raise ConfigError(
                    f"算力池 {pid} 仍有 {' 和 '.join(bits)}，不能切换到另一共享域"
                    f"（{old_pool['worker_home']} → {saved['worker_home']}）；请先等清单跑完或撤回后再切换"
                )
    if not create and new_text == old_text:
        return {"pool": saved, "changed": False, "msg": f"算力池「{saved['label']}」（{saved['host_id']}）配置没有变动"}
    p.parent.mkdir(parents=True, exist_ok=True)
    action = "新建" if create else "修改"
    mode_txt = "独占" if exclusive else "开放"
    mem_txt = "全部用户" if norm_members == ["*"] else ("、".join(norm_members) if norm_members else "仅管理员")
    why = f"{by} {action}了共有算力池「{saved['label']}」（{saved['host_id']}，{mode_txt}，主机：{'、'.join(norm_hosts)}，成员：{mem_txt}）"
    _write_checked(p, new_text, why, {"host": None, "hosts": None}, check=_check_pools_toml)
    removed_hosts = [h for h in (existing.get(pid) or {}).get("hosts", []) if h not in norm_hosts] if not create else []
    extra = f"；移出主机 {'、'.join(removed_hosts)} 上待跑的本池清单将在下一轮自动撤离（不中断在跑任务）" if removed_hosts else ""
    return {"pool": saved, "changed": True, "msg": f"已{action}算力池「{saved['label']}」（{saved['host_id']}）{extra}"}


@_hosts_edit
def remove_pool(pid: str, *, force: bool = False, by: str = "看板", snap: dict | None = None) -> str:
    """管理员删除共有型算力池 [pools.<id>]（#240，唯一落盘入口）：
    默认拦截仍有待放或已落机池清单的算力池；force=True 时移除段落（不中断已在主机上运行的任务）。"""
    from .pool import pool_activity
    pid = str(pid or "").strip()
    if not pid:
        raise ConfigError("请指定要删除的算力池 ID")
    p = hosts_file()
    if not p.exists():
        raise ConfigError(f"找不到算力池 {pid}")
    raw = _read_toml(p)
    raw_pools = raw.get("pools")
    if not isinstance(raw_pools, dict) or pid not in raw_pools:
        raise ConfigError(f"找不到算力池 {pid}")
    try:
        pm = pools(raw)
        pcfg = pm.get(pid)
    except ConfigError:
        pm, pcfg = {}, None
    act = pool_activity(pid, snap=snap, pool_map=pm)
    if (act["pending"] or act["placed"]) and not force:
        bits = [x for x in (f"{act['pending']} 条待放清单" if act["pending"] else "",
                            f"{act['placed']} 条已落机清单" if act["placed"] else "") if x]
        raise ConfigError(f"算力池 {pid} 仍有 {' 和 '.join(bits)}：请先撤回或等待跑完后再删除，或勾选强制删除")
    lines = p.read_text().splitlines(keepends=True)
    bl = _find_pool_block(lines, pid)
    if not bl:
        raise ConfigError(f"{p} 里的算力池 {pid} 写成了内联表，请手动从 hosts.toml 移除")
    s, end, _ = bl[0]
    while s > 0 and not lines[s - 1].strip():
        s -= 1
    sep = ["\n"] if s > 0 and end < len(lines) and lines[end].strip() else []
    new_text = "".join(lines[:s] + sep + lines[end:])
    label = (pcfg or {}).get("label") or pid
    phid = (pcfg or {}).get("host_id") or (POOL if pid == POOL else f"{POOL_PREFIX}{pid}")
    why = f"{by} 删除了共有算力池「{label}」（{phid}）" + ("（强制）" if force and (act["pending"] or act["placed"]) else "")
    _write_checked(p, new_text, why, {"host": None, "hosts": None}, check=_check_pools_toml)
    if act["pending"] and act["placed"]:
        tail = f"（在跑任务不受中断，已落机清单留在原主机继续跑完；待放目录残留清单可用 labmon worker rm {phid} ROOT JOBS 清理）"
    elif act["pending"]:
        tail = f"（待放目录残留清单可用 labmon worker rm {phid} ROOT JOBS 清理）"
    elif act["placed"]:
        tail = "（在跑任务不受中断，已落机清单留在原主机继续跑完）"
    else:
        tail = ""
    return f"已删除算力池「{label}」（{phid}）{tail}"


def pools(data: dict | None = None) -> dict[str, dict]:
    """共有型算力池配置解析与强校验唯一实现（hosts.toml 的 [pools.<id>]，#212，热读取免重启）。
    返回 {pid: {id, host_id, label, hosts, active_hosts, members, exclusive, worker_home}}（保序）。
    强校验：
    - 池 id 匹配 POOL_ID_RE，且不得与任何 [[hosts]] 或 [[fleet.servers]] 的 id 重名；
    - hosts 必选非空，每台须在 [[hosts]] 声明、满足 share_host（非看板本机与回环 SSH）、非独立主机、非借用主机；
    - 同池所有主机必须共用同一个 worker_home（同一共享盘域）；
    - 一台主机至多属于一个共有池（互斥）。"""
    hf = hosts_file()
    if data is None:
        if not hf.exists():
            return {}
        data = _read_toml(hf)
    raw_pools = data.get("pools")
    if raw_pools is None:
        return {}
    if not isinstance(raw_pools, dict):
        raise ConfigError(f"{hf} 的 [pools] 要写成 [pools.<id>] 表")
    raw_hosts = [dict(h) for h in data.get("hosts", [])]
    for h in raw_hosts:
        check_legacy_pool(hf, h)
        if h.get("borrow_until"):
            h["borrow"] = {"holder": h.get("borrow_from"), "swap": True}
    fleet_srvs = (data.get("fleet") or {}).get("servers", []) or []
    for s in fleet_srvs:
        check_legacy_pool(hf, s)
    host_by = {str(h["id"]): host_defaults(h) for h in raw_hosts if "id" in h}
    fleet_ids = {str(s["id"]) for s in fleet_srvs if isinstance(s, dict) and "id" in s}
    taken_ids = set(host_by) | fleet_ids
    seen_host: dict[str, str] = {}
    out: dict[str, dict] = {}
    for pid, sec in raw_pools.items():
        pid = str(pid)
        if not POOL_ID_RE.fullmatch(pid):
            raise ConfigError(f"{hf} 的 [pools.{pid}] 池 id 不合法：须字母开头，仅含字母、数字、_ 或 -，最多 32 位")
        if pid in taken_ids:
            raise ConfigError(f"{hf} 的 [pools.{pid}] 与已有主机或组内服务器 {pid} 重名")
        if not isinstance(sec, dict):
            raise ConfigError(f"{hf} 的 [pools.{pid}] 要是一节表")
        bad = sorted(set(sec) - set(POOL_KEYS))
        if bad:
            raise ConfigError(f"{hf} 的 [pools.{pid}] 不认识 {'、'.join(bad)}（能写的：{'、'.join(POOL_KEYS)}）")
        raw_h = sec.get("hosts")
        if not isinstance(raw_h, list) or not raw_h or not all(isinstance(x, str) and x.strip() for x in raw_h):
            raise ConfigError(f"{hf} 的 [pools.{pid}] 必须写非空 hosts = [\"主机 id\", ...]")
        hids = list(dict.fromkeys(x.strip() for x in raw_h))
        homes: dict[str, tuple] = {}
        active_hids = []
        for hid in hids:
            if hid in fleet_ids and hid not in host_by:
                raise ConfigError(f"{hf} 的 [pools.{pid}] 主机 {hid} 是借用的组内服务器，共有池只能包含常驻 [[hosts]]")
            h = host_by.get(hid)
            if h is None:
                raise ConfigError(f"{hf} 的 [pools.{pid}] 主机 {hid} 不在 [[hosts]] 里")
            if h.get("borrow"):
                raise ConfigError(f"{hf} 的 [pools.{pid}] 主机 {hid} 是借用主机，不能配入共有池")
            if not share_host(h):
                raise ConfigError(f"{hf} 的 [pools.{pid}] 主机 {hid} 是看板本机或回环 SSH，不能配入共有池")
            if is_standalone(h):
                raise ConfigError(f"{hf} 的 [pools.{pid}] 主机 {hid} 是独立主机，不共享 worker_home，不能配入共有池")
            if hid in seen_host:
                raise ConfigError(f"{hf} 的主机 {hid} 同时出现在 [pools.{seen_host[hid]}] 与 [pools.{pid}]：一台主机至多属于一个共有池")
            seen_host[hid] = pid
            homes[hid] = home_key(h)
            if not h.get("returned"):
                active_hids.append(hid)
        uniq_homes = set(homes.values())
        if len(uniq_homes) > 1:
            detail = "；".join(f"{hid}={host_by[hid]['worker_home']}" for hid in hids)
            raise ConfigError(f"{hf} 的 [pools.{pid}] 主机必须共用同一个 worker_home（当前：{detail}）")
        wh = host_by[hids[0]]["worker_home"]
        raw_m = sec.get("members", [])
        if not isinstance(raw_m, list) or not all(isinstance(x, str) and x.strip() for x in raw_m):
            raise ConfigError(f"{hf} 的 [pools.{pid}] members 要写成字符串列表（账号名或 \"*\"）")
        members = list(dict.fromkeys(x.strip().lower() for x in raw_m))
        excl = sec.get("exclusive", True)
        if not isinstance(excl, bool):
            raise ConfigError(f"{hf} 的 [pools.{pid}] exclusive 要写 true 或 false")
        lbl = sec.get("label")
        if lbl is not None and (not isinstance(lbl, str) or not lbl.strip()):
            raise ConfigError(f"{hf} 的 [pools.{pid}] label 要写非空字符串")
        label = lbl.strip() if isinstance(lbl, str) else (POOL_LABEL if pid == POOL else pid)
        out[pid] = {
            "id": pid,
            "host_id": pool_host_id(pid),
            "label": label,
            "hosts": hids,
            "active_hosts": active_hids,
            "members": members,
            "exclusive": excl,
            "worker_home": wh,
        }
    return out


def resolve_pool(hid: str, pool_map: dict[str, dict] | None = None) -> dict | None:
    """将命令行/接口中的池入口（pool 或 pool:<id>）解析为共有池配置 dict；非池入口返回 None。
    全配置仅 1 个池时，默认入口 pool 自动指向该池；多池且未配 [pools.pool] 时报错提示显式写 pool:<id>。"""
    if not isinstance(hid, str):
        return None
    if hid != POOL and not hid.startswith(POOL_PREFIX):
        return None
    pm = pools() if pool_map is None else pool_map
    if hid == POOL:
        if POOL in pm:
            return pm[POOL]
        if len(pm) == 1:
            return next(iter(pm.values()))
        if len(pm) > 1:
            choices = "、".join(p["host_id"] for p in pm.values())
            raise ConfigError(f"当前配置了多个算力池，请明确指定池入口：{choices}")
        return None
    pid = pool_id_of(hid)
    if not pid or pid not in pm:
        known = f"（可用：{'、'.join(p['host_id'] for p in pm.values())}）" if pm else "（hosts.toml 尚未配置 [pools.<id>]）"
        raise ConfigError(f"找不到算力池 {hid}{known}")
    return pm[pid]


def host_pool(hid: str, pool_map: dict[str, dict] | None = None) -> dict | None:
    """一台常驻主机所属的共有池配置 dict；不属于任何共有池返回 None。"""
    pm = pools() if pool_map is None else pool_map
    hid = str(hid)
    return next((p for p in pm.values() if hid in p["hosts"]), None)


def pool_home(pid_or_hid: str | None = None, hosts: list[dict] | None = None) -> str | None:
    """算力池登记所在的 worker_home（#24、#212）：优先取 [pools.<id>] 对应池的 worker_home；
    尚未配置 [pools.*] 或清理已删池的历史待放目录时，回退到自用模板主机的 worker_home。"""
    try:
        pm = pools()
    except ConfigError:
        pm = {}
    if pid_or_hid:
        if pid_or_hid in pm:
            return pm[pid_or_hid]["worker_home"]
        pid = pool_id_of(pid_or_hid)
        if pid and pid in pm:
            return pm[pid]["worker_home"]
        if pid_or_hid == POOL and len(pm) == 1:
            return next(iter(pm.values()))["worker_home"]
    elif pm:
        p = pm.get(POOL) or (next(iter(pm.values())) if len(pm) == 1 else None)
        if p:
            return p["worker_home"]
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
        block = [l for l in lines[s + 1:end] if not re.match(r"\s*(?:job|manage)\s*=", l)]
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


def _check_hosts_toml(text: str) -> dict:
    """整份主机清单校验（add_host / edit_host / remove_host 共用）：TOML 合法、无重复主机 id、每块 [[hosts]] 过 host_defaults。"""
    d = tomllib.loads(text)
    seen = set()
    for h in d.get("hosts") or []:
        if not isinstance(h, dict) or not h.get("id"):
            raise ConfigError("[[hosts]] 块缺少 id")
        hid = str(h["id"])
        if hid in seen:
            raise ConfigError(f"主机清单里有重复的 id：{hid}")
        seen.add(hid)
        host_defaults(h)
    return d


@_hosts_edit
def add_host(entry: dict, why: str) -> Path:
    """在用户级主机清单追加一块 [[hosts]]（外部主机唯一落盘入口），跳过值为 None 的键，全文件过 _check_hosts_toml 后原子写入。"""
    p = hosts_file()
    hid = str(entry.get("id") or "").strip()
    if not hid:
        raise ConfigError("主机 id 不能为空")
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    raw = _read_toml(p) if p.exists() else {}
    if any(str(x.get("id")) == hid for x in raw.get("hosts") or []):
        raise ConfigError(f"主机清单里已有主机 {hid}")
    if any(str(x.get("id")) == hid for x in (raw.get("fleet") or {}).get("servers") or []):
        raise ConfigError(f"主机 id {hid} 与组内服务器重名")
    row = {k: v for k, v in entry.items() if v is not None}
    updated = _append_blocks(lines, "[[hosts]]", [row])
    p.parent.mkdir(parents=True, exist_ok=True)
    return _write_checked(p, "".join(updated), why, {"host": hid, "hosts": None}, check=_check_hosts_toml)


@_hosts_edit
def edit_host(hid: str, updates: dict, why: str) -> Path:
    """修改用户级主机清单里指定 [[hosts]] 块的顶层键（值为 None 删掉该行），保留注释与其他块，过 _check_hosts_toml 后原子写入。"""
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    for s, end, bid in _blocks(lines, "[[hosts]]"):
        if bid != hid:
            continue
        block = _set_keys(lines[s + 1:end], updates, p)
        return _write_checked(p, "".join(lines[:s + 1] + block + lines[end:]), why, {"host": hid, "hosts": None}, check=_check_hosts_toml)
    raise ConfigError(f"主机清单里没有主机 {hid}")


@_hosts_edit
def remove_host(hid: str, why: str) -> Path:
    """从用户级主机清单删掉指定 [[hosts]] 块（连同其上方紧邻空行），过 _check_hosts_toml 后原子写入。"""
    p = hosts_file()
    lines = p.read_text().splitlines(keepends=True) if p.exists() else []
    for s, end, bid in _blocks(lines, "[[hosts]]"):
        if bid != hid:
            continue
        while s > 0 and not lines[s - 1].strip():
            s -= 1
        return _write_checked(p, "".join(lines[:s] + lines[end:]), why, {"host": hid, "hosts": None}, check=_check_hosts_toml)
    raise ConfigError(f"主机清单里没有主机 {hid}")


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
    pm = {}
    if any(is_pool_id(q.get("host")) for q in queues):
        try:
            pm = pools()
        except ConfigError:
            pm = {}
    for phid in list(dict.fromkeys(q["host"] for q in queues if is_pool_id(q.get("host")) and q["host"] not in ids)):
        p = next((x for x in pm.values() if x["host_id"] == phid), None)
        home = p["worker_home"] if p else pool_home(phid)
        if not home:
            continue
        lbl = p["label"] if p else (POOL_LABEL if phid == POOL else phid)
        hosts.append(_norm_host({"id": phid, "label": lbl, "pool": True, "pool_id": p["id"] if p else pool_id_of(phid), "worker_home": home}))
        ids.add(phid)
        if _reader({"host": phid}, hosts) is None and (r := home_host(home, [h for h in more() if not h["returned"]])):
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
