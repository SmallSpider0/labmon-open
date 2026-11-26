"""组内服务器：组里别人在用的服务器。平时只读探测（不进调度、不起 worker），看实际利用率，供分配参考；
在上面动手的只有两件：保活守护（fleet_guard.sh，登记记在容器的 /tmp/labmon-guard.state）开、停、重开看板登记的保活，
用户自行开启的保活持续空转、且配置的保活脚本能核实唯一进程组时停掉它换成看板保活（#10）；
以及用户说好后借来用（borrow.py：拉起 worker、临时指令写在共享盘的 worker 目录，还时清掉）。

主机清单（~/.config/labmon/hosts.toml）的 [fleet] 段：

    [fleet]
    interval = 600                  # 每台多久探一次（秒）。看板每分钟最多连一台，轮流来
    lease_days = 7                  # 平台作业每期天数：从容器启动时间算到期，到期前 24、6 小时提醒（相近到期的合成一条）
    private_pools = ["pool-private"]   # 组里专用的资源池
    private_max = 4                 # 专用池一共几台
    public_max = 2                  # 公用池全组最多开几台（多开会被警告甚至强制关机）
    keepalive = ["keep_alive"]      # GPU 进程自己的命令行含这些字样就算保活（只看它自己，不看父进程：有人用 keepalive_guard.sh 包着真实验）
    keepalive_cmd = "KA_CALLER=labmon-fleet bash /root/data1/alice/keepalive-demo/bin/ka.sh"   # 开、停保活的命令（后接 start / stop / running）
    guard_idle_min = 60             # 保活守护：GPU 连着空闲（没有实验也没有保活）这么久就开保活，免得服务器被平台收回
    guard_watch_min = 5             # 开了保活后在服务器上盯这么久：有实验上来 20 秒内让出；之后靠探测发现、再停掉
    share = {vram = 0.3, util = 30, cpu = 0.3, mem = 0.3}   # 除保活外的实际占用都低于这些：算“可共享”（还能给别人并行用）

    [[fleet.servers]]               # 每台一条；id 跟着这台走（续租、换人都不变），holder 是现在的使用人
    id = "29-15158"
    holder = "CAROL"
    # job = "CAROL-2"                 # 平台作业名，缺省同 holder（看板换人时同名作业已在跑才加后缀并写这一行）
    ssh = "root@198.51.100.29"
    port = 15158
    pool = "pool-private"
    # guard = true                  # 保活守护开不开；不写的公用池开、专用池关（页面上「保活守护」里改）
    # lend = true                   # 入池（#24）：持有人同意空着时给算力池用（页面上「我的账号 → 我的服务器」或抽屉的「入池设置」里改；换人时去掉）
    # lend_hours = "22:00-09:00"    # 入池的时段：每天一段，可跨零点；不写是全天

    [[fleet.rules]]                 # 只写例外（可以一条都没有）：正则匹配 GPU 进程与各级父进程的命令行、工作目录
    match = "/root/demo_eval/"
    kind = "job"                    # job：给它起个好认的名字；keepalive：把它认作保活
    label = "demo_eval 评测"

归类：GPU 进程自己的命令行含 keepalive 里的字样就是保活，其余一律算实验（有人在用），名字取命令简称；规则只管例外。
页面上可以把一组负载点成“这是保活”，它的脚本就加进 keepalive。每组实验按路径归人（disk.whose，与「共享盘」同一张归属表），
对不上的算这台的使用人。“未识别”只剩一种：显卡报出的进程在容器里找不到（同一宿主机上别的容器在用这张卡）。

借用（borrow.py）：说好后把一台借来给自己的项目跑，块里写 borrow_since 等；借用中它也是调度主机，照样探测，保活守护不管它。

保活守护（guard_state、guard_act）：开了守护的服务器，GPU 连着空闲满 guard_idle_min 分钟，探完就开保活（ka_start，经 fleet_guard.sh：
开前当场再查一遍，开了以后在服务器上盯 guard_watch_min 分钟，有实验上来就让出）；盯的时间过了，探测发现 labmon 开的保活旁边又有了
别的负载，就停掉它（ka_stop）。看板登记的保活持续空转就重开（身份只比登记时记下的进程出生时钟）；发现自行开启的保活持续空转时，
只有配置脚本能明确核实归属才接管。重开时停了旧的、新的没开成（gap），下次探测直接再开一次。看板上手动「开保活」与续租后开保活也走 ka_start。

主机清单里写了 platform_job 的自用主机（worker 在管的）也列进来，状态取 worker 的 GPU 进程归属，算配额、不另外探；
使用记录（hours）每轮采集按 worker 状态记一次，口径与探针相同（from_worker）。

每台每次一条 ssh（复用连接，accept-new 记在状态目录的 known_hosts）跑 fleet_probe.sh：GPU 各卡与 5 秒利用率、
GPU 进程（宿主机 PID 经 /proc/<pid>/sched 对回容器 PID）与其父进程链、工作目录、容器的内存与 CPU。
状态目录 overview/fleet/：
    state.json         各台：上次探测、连通、容器启动时间、已发的租期提醒、当前小时的累计、最近一次的样本
    series/<id>.jsonl  每次探测一行（留 7 天；借用中的带 bw，算给对方的预留时不取）；自用主机每 interval 一行（record_mine）
    hours/<id>.jsonl   每小时一行（留 1 年）：各状态次数、可共享次数、实际用量之和、当时的使用人；借用中的那部分另记在 bw[借的人]
                       （同样的计数，算进行的总数）。自用主机也有一份（record_mine，样本取 worker 状态：from_worker）
    mine.json          自用主机当前小时的累计与上次写 series 的时刻（与 state.json 分开：_tick 会删掉不在 [[fleet.servers]] 里的键）
掉线、恢复、换容器（续租或重启）、新出现的未识别进程、租期提醒、保活守护开保活与让出记进看板的记录（records.log_events）。
掉线、换容器、租期提醒的判定与看板主机共用（health.link_step、container_step、lease_due）。
    known_hosts        这些服务器的 ssh 主机密钥（续租、开机换了容器后删掉旧的）
这里只管探测、归类、保活守护与状态文件；给页面算的数据在 fleetview.py，平台操作在 fleetops.py，自用主机当成一台在 fleetboot.mine。
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

from . import lan as L
from . import lease as LS
from .config import GIB, POOL, POOL_LABEL, ConfigError, _read_toml, all_hosts, fmt_time, guest_host, host_defaults, hosts_file, parse_time, set_fleet_keys
from .health import container_step, lease_due, link_step
from .health import lease_end as _lease_end
from .records import log_events
from .state import append_row, load_json, save_json, state_dir as _state_dir
from .worker.pure import mem_unevictable, proc_tree, script_arg

HERE = Path(__file__).resolve().parent
PROBE = HERE / "fleet_probe.sh"
GUARD = HERE / "fleet_guard.sh"
GUARD_RETRY_S = 1800            # 保活守护没开成或尝试过重开：隔这么久再试
GUARD_ZERO_S = 600              # 至少两次有效探测、跨这么久一直无 GPU 活动才重开
TICK = 60                       # 看板每轮间隔；连不上的服务器隔这么久再试一次（掉线判定与看板主机同一规则：health.link_step）
MERGE_S = 2 * 3600              # 租期提醒：相差这么久以内到期的合成一条
SERIES_KEEP = 7 * 86400
HOURS_KEEP = 366 * 86400
# 本地盘开机以来的新写入（续租、关机、换人会清空，#149）：组内服务器、自用主机与借来的同一个口径（local_writes）
LOCAL_ACK = 1.0                 # GiB：labmon 之外的新写入（加容器私有的 /dev/shm）到这么多，续租、关机、换人要勾「已确认」（fleetops._risk）
WROTE_DRIFT = 64 * 2 ** 20      # 字节：组内服务器根盘已用比上次扫路径时变了这么多才再扫（同 worker 的 LOCAL_DRIFT）
SCAN_S = 60                     # 探针扫路径最多跑这么久，超时这次算没扫完
SCAN_GAP = 3600                 # 两次扫路径至少隔这么久；没扫完的间隔翻倍，最长一天
SCAN_TOP = 8                    # 存下的目录取前这么多个，其余合成一项（同 worker 的 LOCAL_TOP）
STATES = ("job", "unknown", "keepalive", "idle", "down")     # 同一小时里取占多数的；一样多时按这个顺序
NAMES = {"job": "实验", "unknown": "未识别", "keepalive": "只有保活", "idle": "空闲", "down": "掉线", "off": "已关机"}
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "tmux", "screen", "nohup", "setsid", "timeout", "sudo", "su", "login",
          "sshd:", "sshd", "sleep", "tini", "dumb-init", "env", "stdbuf", "nice", "ionice", "xargs", "parallel"}


def state_dir() -> Path:
    return _state_dir() / "fleet"


def state_file(name: str) -> Path:
    """组内服务器状态目录里的文件：boot.json（开机参数）、op.json（当前操作）、people.json、pools.json 等。"""
    return state_dir() / name


_EDIT_LOCK = threading.Lock()


def edit_state(name: str, fn):
    """读改写状态目录里的一个 JSON 文件（op.json、boot.json、people.json 等；平台操作线程、HTTP 线程都会改）：
    fn(字典) 原地改，返回值原样返回。进程内用 _EDIT_LOCK，本机命令行登记人员与看板并行时也用文件锁。"""
    path = state_file(name)
    with _EDIT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            d = load_json(path, {})
            out = fn(d)
            save_json(path, d)
            return out


# ---------- 配置 ----------
def conf(path: Path | None = None) -> dict | None:
    """读主机清单的 [fleet]；没有就返回 None。"""
    hf = path or hosts_file()
    if not hf.exists():
        return None
    d = _read_toml(hf)
    f = d.get("fleet")
    if not f:
        return None
    c = {"interval": 600, "lease_days": 7, "private_pools": [], "private_max": None, "public_max": None,
         "keepalive": ["keep_alive"], "keepalive_cmd": None, "guard_idle_min": 60, "guard_watch_min": 5, **{k: v for k, v in f.items() if k not in ("servers", "rules", "share")}}
    c["share"] = {"vram": 0.3, "util": 30, "cpu": 0.3, "mem": 0.3, **(f.get("share") or {})}
    c["servers"] = []
    for s in f.get("servers", []):
        miss = [k for k in ("id", "holder", "ssh", "port", "pool") if not s.get(k)]
        if miss:
            raise ConfigError(f"{hf} 的 [[fleet.servers]] 缺 {'、'.join(miss)}：{s}")
        s = {**s, "id": str(s["id"]), "holder": str(s["holder"])}
        if s["id"] == POOL:
            raise ConfigError(f"{hf} 的 [[fleet.servers]] 不能叫 {POOL}：它是算力池的保留 id")
        _lend_check(hf, s)
        s["job"] = str(s.get("job") or s["holder"])          # 平台作业名：缺省就是名字，同名作业已在跑时加了后缀
        s.setdefault("label", s["holder"])
        c["servers"].append(s)
    for h in d.get("hosts") or []:          # 自用主机也能入池（#79）：同样的两项、同一条校验
        _lend_check(hf, {**h, "id": str(h.get("id"))})
    if len({s["id"] for s in c["servers"]}) != len(c["servers"]):
        raise ConfigError(f"{hf} 的 [[fleet.servers]] id 重复")
    c["rules"] = []
    for r in f.get("rules", []):
        if not r.get("match") or r.get("kind") not in ("job", "keepalive"):
            raise ConfigError(f"{hf} 的 [[fleet.rules]] 需要 match（正则）与 kind = \"job\" 或 \"keepalive\"：{r}")
        try:
            c["rules"].append({**r, "re": re.compile(r["match"])})
        except re.error as e:
            raise ConfigError(f"[[fleet.rules]] 的正则写错了：{r['match']}（{e}）") from None
    return c


def _lend_check(hf, s: dict) -> None:
    """入池的两项（lend、lend_hours）写得对不对：组内服务器与自用主机（#79）共用。"""
    if "lend" in s and not isinstance(s["lend"], bool):
        raise ConfigError(f"{hf} 里 {s['id']} 的 lend 要写 true 或 false")
    if s.get("lend_hours") is not None and lend_span(s["lend_hours"]) is None:
        raise ConfigError(f"{hf} 里 {s['id']} 的 lend_hours「{s['lend_hours']}」认不出：写成 \"22:00-09:00\" 这样（每天一段，可跨零点）")


LEND_RE = re.compile(r"\s*(\d{1,2}):(\d\d)\s*-\s*(\d{1,2}):(\d\d)\s*")


def lend_span(v) -> tuple[int, int] | None:
    """入池的时段（[[fleet.servers]] 的 lend_hours，#24）"HH:MM-HH:MM" → (起, 止) 一天里的分钟；可跨零点（起 > 止），起止相同算全天。
    认不出为 None（fleet.conf 校验、页面提交都经它）。"""
    m = LEND_RE.fullmatch(v) if isinstance(v, str) else None
    if not m:
        return None
    a, b, c, d = (int(x) for x in m.groups())
    if a > 23 or c > 24 or b > 59 or d > 59 or (c == 24 and d):
        return None
    return a * 60 + b, c * 60 + d


def lend_open(s: dict, t: float) -> tuple[bool, float | None]:
    """这台此刻在不在入池时段里与下一次变化的时刻（epoch 秒，按看板本机时间算；全天的为 None）：(在不在, 何时变)。
    没开入池的为 (False, None)。「我的服务器」的状态与 C 的自动借（时段结束就是借用的截止）共用。"""
    if not s.get("lend"):
        return False, None
    sp = lend_span(s.get("lend_hours")) if s.get("lend_hours") else None
    if not sp or sp[0] == sp[1] % 1440:
        return True, None
    lt = time.localtime(t)
    now = lt.tm_hour * 60 + lt.tm_min
    a, b = sp
    inside = a <= now < b if a < b else now >= a or now < b
    nxt = b if inside else a
    day0 = t - (now * 60 + lt.tm_sec)
    at = day0 + nxt * 60
    return inside, at if at > t else at + 86400


def borrowed(s: dict) -> bool:
    """这一台组内服务器正借着（[[fleet.servers]] 写了 borrow_since、还没写 borrow_returned）。借用、保活守护、平台操作、曲线都按它认。"""
    return bool(s.get("borrow_since")) and not s.get("borrow_returned")


def borrower(s: dict, cfg: dict | None) -> str | None:
    """这一台（这次或上一次）借给了谁：算力池自动借的（borrow_pool，#25）是「算力池」（POOL_LABEL，按人统计不把它当一个人），
    其余是借的人（borrow_by；只有 #70 上线前的借用没有它）。"""
    if not s.get("borrow_since"):
        return None
    return POOL_LABEL if s.get("borrow_pool") else s.get("borrow_by") or legacy_person(cfg)


def legacy_person(cfg: dict | None) -> str | None:
    """旧清单中没有归属字段的主机和借用沿用原人名。旧清单补齐后可删。"""
    if (cfg or {}).get("me"):
        return str(cfg["me"])
    job = next((h["platform_job"] for h in all_hosts() if h.get("platform_job") and not h.get("platform_holder")), None)
    return re.sub(r"-\d+$", "", str(job)) if job else None


def host_holder(host: dict, cfg: dict | None) -> str | None:
    """自用主机的组内人员归属。"""
    return host.get("platform_holder") or legacy_person(cfg)


def registered_people() -> dict:
    """读取已登记的组内人员名册。"""
    return load_json(state_file("people.json"), {})


def known_people(cfg: dict) -> set[str]:
    """组内名单及当前主机归属中的人员名。"""
    servers = cfg.get("servers") or []
    names = set(registered_people())
    names.update(s["holder"] for s in servers)
    names.update(borrower(s, cfg) for s in servers)
    names.update(host_holder(h, cfg) for h in all_hosts() if h.get("platform_job"))
    return {name for name in names if name}


ADMIN_PERSON = "管理员"        # 没有主人的项目（归管理员）在认不出管理员是谁（legacy_person 为空）时算在这个名下


def share_key(person: str) -> str:
    """人员名在 sched.json 的 owner 里的写法（#23）：名字的短哈希，共享盘上不写名字；worker 只拿它分组，看板自己知道是谁。"""
    return hashlib.sha256(person.encode()).hexdigest()[:12]


def project_people(pids, cfg: dict | None = None) -> dict[str, str]:
    """各项目算在谁名下（按人的公平份额，#23；同一人的几个项目合起来算一份）：项目主人（users.owner_of）绑着的组内人员（users.people_of）；
    没关联人员的账号按账号自己算一人（「账号 名字」）；没有主人的（归管理员，#167 之前的项目、主人账号已删的）取 legacy_person，再没有就是 ADMIN_PERSON。"""
    from .users import owner_of, people_of                 # 与 users 互相用到，放在函数里 import
    cfg = conf() if cfg is None else cfg
    owners = {p: owner_of(p) for p in pids}
    names = people_of() if any(owners.values()) else {}
    admin = legacy_person(cfg) or ADMIN_PERSON
    return {p: (names.get(a, (None, None))[0] or f"账号 {a}") if a else admin for p, a in owners.items()}


# ---------- 探测 ----------
def _ssh_cmd(s: dict, *extra: str) -> list[str]:
    """连这一台的 ssh：复用看板的长连接，第一次连自动接受主机密钥。记在哪份文件只在这里定（#109）：服务器项带 known_hosts
    （自用主机，fleetboot.mine 给：主机清单写的或 ~/.ssh/known_hosts，与拉起 worker、实验会话裸 ssh 同一份）就记在那里，
    组内服务器与借来的记在状态目录。续租后等 ssh 的那次连接建起长连接，之后 worker 与看板复用它时不再核对密钥，所以必须记对文件。"""
    kh = s.get("known_hosts") or state_dir() / "known_hosts"
    if not s.get("known_hosts"):
        kh.parent.mkdir(parents=True, exist_ok=True)
    return L.ssh_argv(s["ssh"], s["port"], *extra, known_hosts=kh)


def ssh(s: dict, remote: str, input: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    """在这一台上跑一条命令，返回 (退出码, 标准输出, 标准错误)；ssh 本身连不上时退出码是 255。"""
    try:
        r = L.ssh_run(_ssh_cmd(s) + [remote], input=input, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 255, "", "ssh 超时"
    return r.returncode, r.stdout, r.stderr


def forget(s: dict) -> None:
    """续租、开机换了容器：关掉连旧容器的复用连接，删掉记下的旧主机密钥（新容器的密钥若不同，accept-new 会拒连）。
    只删这一台 host:port 的条目（22 端口的不带方括号），两份都删：状态目录的 known_hosts（组内服务器探测、等 ssh 用）；
    自用主机自己那份（s["known_hosts"]，fleetboot.mine 给：主机清单写的或 ~/.ssh/known_hosts）。新密钥由之后等 ssh 的那次连接记回
    _ssh_cmd 定的那份（#109）。"""
    subprocess.run(_ssh_cmd(s, "-O", "exit"), capture_output=True, timeout=20)
    host, port = s["ssh"].rsplit("@", 1)[-1], int(s["port"])
    for f in dict.fromkeys([str(state_dir() / "known_hosts"), *([str(s["known_hosts"])] if s.get("known_hosts") else [])]):
        subprocess.run(["ssh-keygen", "-R", f"[{host}]:{port}" if port != 22 else host, "-f", f], capture_output=True, timeout=20)


def fetch(s: dict, scan: float | None = None, timeout: int = 60) -> tuple[str | None, str | None]:
    """一条 ssh 跑探针，返回 (原文, 错误)。认作保活的字样（[fleet] keepalive）随命令传过去：探针只读这些保活进程树的 smaps。
    scan：顺带扫根盘这个时刻（开机）以后新写的路径（scan_due 定），探针最多多跑 SCAN_S 秒。"""
    env = f"LABMON_SCAN={int(scan)} LABMON_SCAN_S={SCAN_S} " if scan else ""
    try:
        rc, out, err = ssh(s, env + "bash -s -- " + shlex.join((conf() or {}).get("keepalive", [])), PROBE.read_text(), timeout + (SCAN_S + 15 if scan else 0))
    except UnicodeDecodeError as e:
        # 不替换探针中的路径、进程命令等结构字段；整份拒收，沿原失败记录推进轮询。
        return None, f"探针输出不是有效 UTF-8（字节位置 {e.start}–{e.end}），本次未采纳"
    if "@@ info" not in out:
        if "IDENTIFICATION HAS CHANGED" in err or "Host key verification failed" in err:
            return None, "主机密钥变了（多半在网页上续租或重启过）：详情页点「开保活」会先清掉旧密钥再连"
        return None, (err.strip().splitlines() or [f"ssh 失败（退出码 {rc}）"])[-1]
    return out, None


def _secs(s: str) -> int:
    """ps 的 time 列：[DD-]HH:MM:SS → 秒。"""
    d, _, hms = s.rpartition("-")
    parts = [int(x) for x in hms.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return (int(d) if d else 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse(raw: str) -> dict:
    """探针输出 → 样本：info、各卡、利用率采样、CPU、GPU 进程、PID 对照、进程表。"""
    sec, cur = {}, None
    for line in raw.splitlines():
        if line.startswith("@@ "):
            cur = sec.setdefault(line[3:].strip(), [])
        elif cur is not None and line.strip():
            cur.append(line)
    info = dict((l.split(None, 1) + [""])[:2] for l in sec.get("info", []))
    smp = {"clock": _num(info.get("clock")), "nproc": _num(info.get("nproc"))}
    bt, tck, p1 = _num(info.get("btime")), _num(info.get("tck")), _num(info.get("p1"))
    smp["started"] = round(bt + p1 / tck) if bt and tck and p1 is not None else None
    smp["btime"], smp["tck"] = bt, tck              # 进程出生时钟换成墙上时刻（analyze 的 ka_group）
    lim, use, ina, act = (_num(info.get(k)) for k in ("memlim", "memuse", "inact", "actf"))
    # 收不回的内存 = 用量 − 文件页（与 worker 共用 pure.mem_unevictable）
    smp["mem"] = {"limit": lim / GIB, "used": (use - (ina or 0)) / GIB,
                  "anon": mem_unevictable(use, act, ina) / GIB if ina is not None and act is not None else None} \
        if lim and use and lim < 2 ** 60 else None
    q = (info.get("cpuq") or "").split() or (info.get("cpumax") or "").split()
    smp["cpu_quota"] = round(int(q[0]) / int(q[1]), 2) if len(q) == 2 and q[0].isdigit() and q[1].isdigit() and int(q[0]) > 0 else None
    rt, rf, sh = ((info.get(k) or "").split() for k in ("root", "rootfs", "shm"))
    smp["root"] = {"total": int(rt[0]) * 1024 / GIB, "used": int(rt[1]) * 1024 / GIB} if len(rt) == 2 and rt[0].isdigit() and rt[1].isdigit() else None
    if smp["root"]:
        # 根盘是 overlay、比宿主机的盘（/etc/hosts 所在的）小一成以上（有配额）：已用就是开机以来写的；否则测不准（同 worker 的 rootfs_info）
        host = int(rf[1]) * 1024 / GIB if len(rf) == 2 and rf[1].isdigit() else None
        smp["root"].update(fs=rf[0] if rf else None, ok=bool(rf and rf[0] == "overlay" and host and smp["root"]["total"] < 0.9 * host))
        if len(sh) == 2 and sh[0] == "shm" and sh[1].isdigit():   # 容器私有的 /dev/shm（与宿主共用的 tmpfs 不算这台的）
            smp["root"]["shm"] = int(sh[1]) * 1024 / GIB
    smp["scan"] = _scan(sec["scan"]) if "scan" in sec else None
    cpu = (sec.get("cpu") or [""])[0].split()
    smp["cpu"] = round((int(cpu[2]) - int(cpu[0])) / (int(cpu[3]) - int(cpu[1])), 2) \
        if len(cpu) == 4 and all(x.isdigit() for x in cpu) and int(cpu[3]) > int(cpu[1]) else None
    samples, invalid_util = {}, set()
    for l in sec.get("util", []):
        p = [x.strip() for x in l.split(",")]
        if len(p) == 2 and p[0].isdigit():
            util = _num(p[1])
            if util is None:
                invalid_util.add(int(p[0]))
            else:
                samples.setdefault(int(p[0]), []).append(util)
    smp["gpus"] = []
    for l in sec.get("gpu", []):
        p = [x.strip() for x in l.split(",")]
        if len(p) == 6 and p[0].isdigit():
            ss = samples.get(int(p[0])) or []
            smp["gpus"].append({"i": int(p[0]), "uuid": p[1], "name": p[2], "used": _num(p[3], 0), "total": _num(p[4], 0),
                                "util": round(sum(ss) / len(ss), 1) if ss else _num(p[5]), "samples": ss,
                                "zero": int(p[0]) not in invalid_util and len(ss) >= 2 and all(x == 0 for x in ss)})
    smp["apps"] = []
    for l in sec.get("apps", []):
        p = [x.strip() for x in l.split(",")]
        if len(p) >= 2 and p[0].isdigit():
            smp["apps"].append({"hpid": int(p[0]), "mb": _num(p[1], 0), "uuid": p[2] if len(p) > 2 else None})
    smp["map"] = {}
    for l in sec.get("map", []):
        p = l.split(None, 2)
        if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
            smp["map"][int(p[1])] = (int(p[0]), p[2] if len(p) > 2 else None)
    smp["ps"] = {}
    for l in sec.get("ps", []):
        p = l.split(None, 6)
        if len(p) == 7 and p[0].isdigit() and p[1].isdigit():
            try:
                smp["ps"][int(p[0])] = {"ppid": int(p[1]), "etime": int(p[2]), "cpu_s": _secs(p[3]), "pcpu": _num(p[4], 0),
                                        "rss": int(p[5]) * 1024, "args": p[6]}
            except ValueError:
                continue
    smp["pss"] = {int(p[0]): int(p[1]) * 1024 for p in (l.split() for l in sec.get("pss", [])) if len(p) == 2 and p[0].isdigit() and p[1].isdigit()}
    smp["ka_groups"] = {int(p[0]): [int(p[1]), int(p[2])] for p in (l.split() for l in sec.get("ka_groups", []))
                        if len(p) == 3 and all(x.isdigit() for x in p)}
    smp["p1"] = int(p1) if smp["started"] is not None else None
    smp["guard"] = None              # labmon 开的保活（fleet_guard.sh 登记的：start 登记时刻 进程组 盯到几点 谁开的 进程出生时钟）
    born = None
    for l in sec.get("guard", []):
        p = l.split()
        if p[0] == "start" and len(p) >= 4 and all(x.isdigit() for x in p[1:4]):
            smp["guard"] = {"t": int(p[1]), "pgid": int(p[2]), "until": int(p[3]), "by": p[4] if len(p) > 4 else None}
            born = int(p[5]) if len(p) > 5 and p[5].isdigit() else None
        elif smp["guard"] and p[0] in ("yield", "stop") and len(p) >= 2 and p[1].isdigit():
            smp["guard"]["end"] = {"t": int(p[1]), "how": p[0], "why": " ".join(p[2:]) or None}
        elif smp["guard"] and p[0] == "proc" and len(p) == 3 and all(x.isdigit() for x in p[1:]):
            g = smp["guard"]
            pid, birth = map(int, p[1:])
            # 身份只比出生时钟（开机以来的时钟数）：与登记时记下的对得上才是那个进程，PID 复用、时钟跳变都不影响。
            # 登记里没有出生时钟的（11-17 以前的脚本写的）认不出身份：照样让路、停，只是不自动重开
            if pid == g["pgid"] and born is not None and birth == born and p1 is not None:
                g["identity"] = [int(p1), g["t"], pid, birth]
    return smp


def _scan(lines: list[str]) -> dict:
    """探针 scan 段 → {b, n, keys, top [[路径, 字节, 最近写入]], partial, err}（字节；没有 GNU find 的 err 为 nofind）。"""
    out = {"b": 0, "n": 0, "keys": 0, "top": [], "partial": False, "err": None}
    for l in lines:
        p = l.split(None, 3)
        if p[0] == "nofind":
            out["err"] = "nofind"
        elif p[0] == "total" and len(p) == 4 and all(x.isdigit() for x in p[1:]):
            out.update(b=int(p[1]) * 1024, n=int(p[2]), keys=int(p[3]))
        elif p[0] == "rc":
            out["partial"] = p[1:] == ["124"]
        elif len(p) == 4 and p[0].isdigit() and p[1].isdigit() and p[2].isdigit():
            out["top"].append([p[3], int(p[0]) * 1024, int(p[2]) or None])
    return out


def local_writes(root: dict | None, scan: dict | None, own: float | None = None) -> dict | None:
    """本地盘开机以来的新写入（续租、关机、换人会清空，#149）：组内服务器（探针的 root 与扫出的 wrote）、自用主机与借来的（worker 的 info.rootfs
    与 storage）同一个口径，页面、平台操作的确认、提醒都取它。root：{used, total, fs, ok, shm}（GiB）；scan：扫出的路径（字节：worker 的
    storage.wrote 或组内服务器的 wrote）；own：labmon 目录的占用（字节，组内服务器没有）。
    返回（GiB）：used 根盘已用（ok 时就是开机以来写的）、total、own、user = used − own（labmon 之外的）、shm、ok（测得准）、
    risk（测得准且 user + shm 到 LOCAL_ACK：续租、关机、换人要勾「已确认」）、scan（since 起算的时刻、b、n、top [[路径, GiB, 最近写入]]、
    more [其余几处, GiB]、t 扫的时刻、partial 没扫完、pending 还没扫的单位数、err）。没有根盘的量为 None。"""
    if not root or root.get("used") is None:
        return None
    own_g = (own or 0) / GIB
    user, shm, ok = max(0.0, root["used"] - own_g), root.get("shm") or 0.0, bool(root.get("ok"))
    out = {"used": round(root["used"], 3), "total": root.get("total") and round(root["total"], 1), "own": round(own_g, 3) if own is not None else None,
           "user": round(user, 3), "shm": round(shm, 3) or None, "ok": ok, "risk": ok and user + shm >= LOCAL_ACK, "scan": None}
    if scan:
        g = lambda b: round((b or 0) / GIB, 3)  # noqa: E731
        out["scan"] = {"since": scan.get("since"), "t": scan.get("t"), "b": g(scan.get("b")), "n": scan.get("n"),
                       "top": [[x[0], g(x[1]), x[2] if len(x) > 2 else None] for x in scan.get("top") or []],
                       "more": [(scan.get("more") or [0, 0])[0], g((scan.get("more") or [0, 0])[1])],
                       "partial": bool(scan.get("partial")), "pending": scan.get("pending") or 0, "err": scan.get("err")}
    return out


def lw_text(lw: dict | None, verb: str = "续租") -> str:
    """local_writes 过了门槛（risk）时的一句话：写了多少、大头在哪、verb（续租、关机、换人）会清空。平台操作的确认（fleetops._risk）与
    预备续租的提醒（renew.check）共用；页面上是 lwText（overview.html）。没过门槛为空串。"""
    if not lw or not lw.get("risk"):
        return ""
    top = "、".join((p[:-2] or "/") + "（本层）" if p.endswith("/.") else p for p, *_ in ((lw.get("scan") or {}).get("top") or [])[:3])
    return (f"本地盘开机以来 labmon 之外写了 {lw['user'] + (lw.get('shm') or 0):.1f} GiB" + (f"（{top}）" if top else "")
            + f"，{verb}会清空，要留的先挪到共享盘")


def server_lw(st: dict) -> dict | None:
    """组内服务器的 local_writes：总量取最近一次探测的，路径取扫出的（换了容器的旧结果不要）。组内页（fleetview）与平台操作的确认（fleetops._probe）共用。"""
    a, w = st.get("sample") or {}, st.get("wrote")
    return local_writes(a.get("root"), w if w and w.get("started") == a.get("started") else None) if a else None


def scan_due(s: dict, st: dict, t: float, off: bool = False) -> float | None:
    """这一次探测要不要顺带扫路径（维护者 11-10 允许扫别人的，#149），要就返回开机时刻（探针只数它以后新写的）。
    只扫根盘测得准（overlay + 配额）、知道开机时刻、没在借用（扫出来的混着我们的）、没关机的；对方在跑实验照样扫（维护者 11-10 定，#155：开销实测不到 1 秒）；
    这个容器还没扫过（已用不到 WROTE_DRIFT 的不扫），或根盘已用比上次扫时变了 WROTE_DRIFT；两次至少隔 SCAN_GAP，没扫完的间隔翻倍、最长一天；
    扫不了的（没有 GNU find）这个容器不再扫。"""
    a = st.get("sample") or {}
    r, started = a.get("root") or {}, a.get("started")
    if off or borrowed(s) or not r.get("ok") or not started:
        return None
    w, used = st.get("wrote") or {}, r["used"] * GIB
    if w.get("started") != started:
        return started if used >= WROTE_DRIFT else None
    if w.get("err") or t - w["t"] < min(86400, SCAN_GAP * 2 ** (w.get("fails") or 0)):
        return None
    return started if w.get("partial") or abs(used - w["used"]) > WROTE_DRIFT else None


def _wrote(sc: dict, a: dict, old: dict | None, t: float) -> dict:
    """探针扫出的路径存进这台的状态（wrote，字节）：前 SCAN_TOP 个目录，其余合成 more；没扫完的记 partial、fails 加一（scan_due 的间隔翻倍）。"""
    top = sc["top"][:SCAN_TOP]
    old = old if old and old.get("started") == a["started"] else {}
    return {"t": round(t), "started": a["started"], "since": a["started"], "used": round(a["root"]["used"] * GIB), "b": sc["b"], "n": sc["n"], "top": top,
            "more": [max(0, sc["keys"] - len(top)), max(0, sc["b"] - sum(x[1] for x in top))], "partial": sc["partial"], "err": sc["err"],
            "fails": (old.get("fails") or 0) + 1 if sc["partial"] else 0}


# ---------- 归类 ----------
def _exe(args: str) -> str:
    return os.path.basename((args.split() or [""])[0]).lstrip("-")


def _shell(args: str) -> bool:
    return _exe(args) in SHELLS or ".vscode-server" in args or args.startswith("sshd:")


def _spawn(args: str) -> bool:
    return "multiprocessing" in args and " -c " in f" {args} "


def _script(args: str) -> tuple[str | None, list[str]]:
    """命令行跑的是什么（照命令行里的写法）与其后的参数：python 取模块或脚本，shell 取脚本，别的取程序本身。
    python -c、光一个 python 或 shell 返回 None。"""
    t = args.split()
    exe, rest = _exe(args), t[1:]
    if re.fullmatch(r"python[\d.]*", exe):
        i = 0
        while i < len(rest) and rest[i].startswith("-") and rest[i] not in ("-m", "-c"):
            i += 1
        if i < len(rest) and rest[i] == "-m" and i + 1 < len(rest):
            return rest[i + 1], rest[i + 2:]
        return (rest[i], rest[i + 1:]) if i < len(rest) and rest[i] != "-c" else (None, [])
    if exe in SHELLS:
        return (rest[0], rest[1:]) if rest and not rest[0].startswith("-") else (None, [])
    return (t[0], rest) if t else (None, [])


def _abs_script(args: str, cwd: str | None) -> str | None:
    """python 跑的脚本写的是相对路径时，按进程工作目录还原的完整路径（只做 normpath：看板这边解不开对方的符号链接）；
    不是 python、-m 或 -c、没有脚本、已是绝对路径、不知道工作目录的为 None。选项跳过与路径还原复用 pure.script_arg（与 worker 共用）。
    探针 fleet_probe.sh 的 script_abs 是同一条规则（探针在未必有 Python 的镜像里跑）。"""
    t = args.split()
    if not cwd or not t or not re.fullmatch(r"python[\d.]*", _exe(args)):
        return None
    sa = script_arg(t, cwd)
    if not sa or sa[0] == "-c" or sa[0].startswith("-m "):
        return None
    raw = t[sa[1] - 1]
    return None if raw == "-" or raw.startswith("/") else sa[0]


def brief(args: str) -> str:
    """命令行的简称：python 取模块或脚本名，再带上 --config 的文件名。"""
    if not args.split():
        return ""
    tok, more = _script(args)
    if not tok:
        exe = _exe(args)
        return "python -c …" if re.fullmatch(r"python[\d.]*", exe) and " -c " in f" {args} " else exe
    name = os.path.basename(tok).lstrip("-")
    cfg = next((os.path.basename(more[k + 1]) for k in range(len(more) - 1) if more[k] in ("--config", "--cfg", "--config-file")), None)
    return (name + (f" · {cfg}" if cfg else ""))[:90]


def _chain(ps: dict, pid: int | None) -> list[int]:
    out = []
    while pid in ps and pid not in out and pid > 1:
        out.append(pid)
        pid = ps[pid]["ppid"]
    return out


def classify(own: str, text: str, cfg: dict, alt: str | None = None) -> tuple[str, str | None, str | None]:
    """(类别, 规则给的名字, 命中的规则)。own 是 GPU 进程自己的命令行，text 再加上父进程链与目录。alt：相对路径的脚本按工作目录还原的完整路径
    （_abs_script）：规则与保活字样另拿它比一遍（原命令行照旧比，带锚点的规则不受影响），相对路径起的与写完整路径起的同一脚本归类一样（#29）。
    顺序：规则命中自己的命令行 → 自己的命令行含保活字样 → 规则命中父进程链或目录 → 其余都算实验（有人在用）。
    保活字样排在“规则按父进程命中”之前：实验守护脚本（如 demo_eval 的 keepalive_guard.sh）拉起的保活，父进程带着项目路径，
    按父进程算会把纯保活误判成实验。"""
    mine = [own] + ([alt] if alt else [])
    for r in cfg["rules"]:
        if any(r["re"].search(x) for x in mine):
            return r["kind"], r.get("label"), r["match"]
    if any(k in x for k in cfg["keepalive"] for x in mine):
        return "keepalive", None, None
    for r in cfg["rules"]:
        if r["re"].search(text):
            return r["kind"], r.get("label"), r["match"]
    return "job", None, None


def mark_keepalive(sid: str, pid: int) -> str:
    """页面上把这台的一组负载认作保活：它的 GPU 进程跑的脚本加进主机清单的 [fleet] keepalive，再立刻探一次才生效。返回加进去的字样。
    照命令行里的写法；python 跑的相对路径按这组的工作目录写成完整路径（_abs_script）：以后写完整路径、相对路径起的同一脚本都认（#29）。"""
    w = next((w for w in ((_load_state().get(sid) or {}).get("sample") or {}).get("work", []) if w.get("pid") == pid), None)
    if not w or not w.get("chain"):
        raise ConfigError(f"{sid} 上已经没有这组负载，刷新再看")
    word = _abs_script(w["chain"][-1], w.get("cwd")) or _script(w["chain"][-1])[0]
    if not word:
        raise ConfigError("认不出它跑的脚本，请在主机清单里加一条 [[fleet.rules]]（match 写正则，kind = \"keepalive\"）")
    cfg = conf()
    if word not in cfg["keepalive"]:
        who = " ".join(filter(None, [next((x["holder"] for x in cfg["servers"] if x["id"] == sid), None), sid]))
        set_fleet_keys({"keepalive": [*cfg["keepalive"], word]}, f"把 {who} 上的 {word} 认作保活")
    return word


def analyze(smp: dict, cfg: dict) -> dict:
    """把 GPU 进程按“最上层的非 shell 父进程”归成一组组负载并归类，算出这台的状态与除保活外的实际用量。"""
    ps, groups, ka_gpu = smp["ps"], {}, set()
    for a in smp["apps"]:
        pid, cwd = smp["map"].get(a["hpid"], (None, None))
        if pid is None and a["hpid"] in ps:              # 新内核在容器里直接给容器 PID
            pid = a["hpid"]
        ch = _chain(ps, pid)
        if not ch:
            g = groups.setdefault(("h", a["hpid"]), {"kind": "unknown", "label": f"宿主机 PID {a['hpid']}（容器里找不到）",
                                                     "rule": None, "top": None, "mb": 0, "n": 0, "names": [], "cwd": None, "chain": [], "gpids": []})
            g["mb"] += a["mb"]
            g["n"] += 1
            continue
        top = next((p for p in reversed(ch) if not _shell(ps[p]["args"])), ch[0])
        name = next((p for p in ch if not _spawn(ps[p]["args"])), ch[0])
        kind, label, rule = classify(ps[pid]["args"], "\n".join(ps[p]["args"] for p in ch) + "\n" + (cwd or ""), cfg, _abs_script(ps[pid]["args"], cwd))
        if kind == "keepalive":
            ka_gpu.add(pid)
        g = groups.setdefault(top, {"kind": kind, "label": label, "rule": rule, "top": top, "mb": 0, "n": 0, "names": [], "cwd": cwd,
                                    "chain": [ps[p]["args"][:300] for p in reversed(ch)], "gpids": []})
        g["kind"] = min(g["kind"], kind, key=STATES.index)       # 一组里有实验就算实验
        g["mb"] += a["mb"]
        g["n"] += 1
        if pid not in g["gpids"]:                                # 这组的 GPU 进程（容器 PID）：借用前逐个核对保活是不是 ka.sh 管的（borrow.begin）
            g["gpids"].append(pid)
        nm = brief(ps[name]["args"])
        if nm not in g["names"]:
            g["names"].append(nm)
    work = []
    for g in groups.values():
        t = ps.get(g["top"]) if g["top"] else None
        work.append({**g, "label": g["label"] or (brief(t["args"]) if t else g["label"]), "args": t["args"] if t else None,
                     "etime": t["etime"] if t else None, "cpu_s": t["cpu_s"] if t else None, "pid": g["top"] if t else None,
                     "mb": round(g["mb"])})
    work.sort(key=lambda w: (STATES.index(w["kind"]), -w["mb"]))
    # 平均值可能四舍五入到零。每张卡都要有至少两条明确的零样本。
    gs = smp["gpus"]
    zero_cards = len(gs) if all(card.get("zero") for card in gs) else 0
    g = smp.get("guard")
    if g:                                                # labmon 开的那个保活还在不在（进程组长就是保活进程）
        g = {**g, "running": not g.get("end") and any(k in (ps.get(g["pgid"]) or {}).get("args", "") for k in cfg["keepalive"])}
        for w in work:
            w["ours"] = g["running"] and w["kind"] == "keepalive" and w["pid"] == g["pgid"]
        if g["running"] and g.get("identity") and all(w.get("ours") for w in work) and zero_cards:
            g["zero_cards"] = zero_cards
    manual_guard = None
    if not (g or {}).get("running") and zero_cards and work and all(w["kind"] == "keepalive" for w in work):
        p1 = smp.get("p1")
        by_pid = smp.get("ka_groups") or {}
        groups = [by_pid.get(pid) for pid in sorted(ka_gpu)]
        target = None
        if p1 is not None and groups and all(group and group == groups[0] and group[0] > 1 and group[1] >= p1 for group in groups):
            target = [p1, "manual", *groups[0], zero_cards]
        signature = target or ["manual", p1, zero_cards, [[pid, by_pid.get(pid)] for pid in sorted(ka_gpu)]]
        manual_guard = {"identity": signature, "target": target}
    # GPU 上只有保活、都在同一个进程组、不是看板登记的：那个组与组长启动时刻（还掉以后补登记 labmon 借用期间开的保活，guard_state 的 adopt，#102）
    ka_group = None
    if work and all(w["kind"] == "keepalive" and not w.get("ours") for w in work) and smp.get("btime") and smp.get("tck"):
        grp = {tuple(smp.get("ka_groups", {}).get(pid) or ()) for pid in ka_gpu}
        one = next(iter(grp)) if len(grp) == 1 else ()
        if len(one) == 2:
            ka_group = {"pgid": one[0], "birth": one[1], "t": round(smp["btime"] + one[1] / smp["tck"])}
    kinds = {w["kind"] for w in work}
    st = next((k for k in STATES if k in kinds), "idle")
    # 保活：GPU 上认作保活的进程整棵进程树（同 worker 的 proc_tree）。内存扣各进程 PSS（探针的 pss 段；按规则认作保活、探针没读的按 RSS），
    # 同 worker 扣保活的口径（#29：RSS 会把父子共享的页重复相加，扣多了）；CPU 扣 ps 的 %CPU（进程整个生命期的平均，worker 算的是这一段）
    ka = set(proc_tree(ps, ka_gpu))
    ka_cpu = sum(ps[p]["pcpu"] for p in ka) / 100
    ka_mem = sum(smp.get("pss", {}).get(p, ps[p]["rss"]) for p in ka) / GIB
    vt = sum(g["total"] for g in smp["gpus"]) or None
    vr = sum(w["mb"] for w in work if w["kind"] != "keepalive")
    util = round(sum(g["util"] or 0 for g in smp["gpus"]) / len(smp["gpus"]), 1) if smp["gpus"] else None
    ur = util if vr else 0
    cr = max(0.0, smp["cpu"] - ka_cpu) if smp["cpu"] is not None else None
    m = smp["mem"]
    mr = max(0.0, m["used"] - ka_mem) if m else None
    ma = max(0.0, m["anon"] - ka_mem) if m and m["anon"] is not None else None     # 不算页缓存的：给对方留多少按它算（borrow.reserve_for）
    # 可共享的内存也按不算页缓存的（活跃页缓存收得回，读过大文件不算在用；与借用的预留同口径，11-10 急修）；探针没送 anon 时退回含页缓存的
    share = shareable(vr, vt, ur, cr, smp["cpu_quota"], mr if ma is None else ma, m["limit"] if m else None, cfg)
    vscode = any(".vscode-server" in x["args"] for x in ps.values())
    terms = sum(1 for x in ps.values() if re.match(r"sshd: \S+@pts/", x["args"]))
    return {"state": st, "work": work, "util": util, "util_real": ur, "vram": sum(g["used"] for g in smp["gpus"]), "vram_total": vt,
            "vram_real": vr, "vram_ka": sum(w["mb"] for w in work if w["kind"] == "keepalive"), "cpu": smp["cpu"], "cpu_real": cr,
            "cpu_quota": smp["cpu_quota"], "nproc": smp["nproc"], "mem": m, "mem_real": mr, "mem_anon": ma, "share": bool(share),
            "gpus": [{k: g[k] for k in ("i", "uuid", "name", "used", "total", "util")} for g in smp["gpus"]],
            "started": smp["started"], "root": smp["root"], "vscode": vscode, "terms": terms, "guard": g, "manual_guard": manual_guard, "ka_group": ka_group, "mem_ka": ka_mem if m else None,
            "scan": smp.get("scan")}


def shareable(vr, vt, ur, cr, cq, mr, ml, cfg: dict) -> bool:
    """可共享（还能给别人并行用）：除保活外的显存、利用率、CPU、内存都低于 [fleet] share 的阈值。探针（analyze）与自用主机（from_worker）共用。
    mr 是收不回的内存（不算页缓存：探针传 mem_anon，自用主机传 worker 的 mem.anon，即 mem_unevictable；没有时退回含页缓存的）。"""
    s = cfg["share"]
    return bool(vt and vr / vt < s["vram"] and (ur or 0) < s["util"]
                and (cr is None or not cq or cr / cq < s["cpu"])
                and (mr is None or not ml or mr / ml < s["mem"]))


def worker_fresh(e: dict | None) -> bool:
    """工作台快照里这台主机的 worker 状态新不新鲜（本机在内网、没掉线、有状态且未过 STALE_S）：
    自用主机（fleetboot.mine）与借用中的组内服务器（fleetview.view）共用同一门槛（#174）。"""
    return bool(e and e.get("status") and (e.get("health") or {}).get("state") != "down"
                and e.get("worker_up") and not e.get("lan_off"))


def from_worker(st: dict, cfg: dict | None, holder: str | None = None, *, job_owner: str | None = None) -> dict:
    """自用主机与借用中组内服务器的 worker 状态换成与 analyze 同形的样本（使用记录与组内页用），口径与探针一致：状态只看 GPU 进程，按 STATES 的先后取；
    labmon 的任务与容器里找得到的 labmon 之外的 GPU 进程都算实验（探针：找得到、不是保活的一律算实验），对不回容器的（pid 为空）算未识别，
    命令行含 keep_alive 的算保活（worker 认的字样，同 [fleet] keepalive 的缺省；worker 不 import 本包，另加的字样与规则它不认）。
    只有 CPU 的任务、占位任务不占 GPU，不算实验。CPU 与内存取整个容器的（worker 状态里没有保活进程树单独的量：mem_ka 为 None），只用来判可共享。
    work：GPU 进程归成一组组负载（worker 的任务按任务名一组，保活一组，labmon 之外的每个进程一组），不带 pid（页面上不出「这是保活」：
    worker 认保活不看 [fleet] keepalive）。job_owner：借用中 labmon 任务的兜底归属（路径对不上共享盘时算借用方，对方进程仍归 holder）。
    root：worker 量的容器根盘（info.rootfs：已用、总量、测不测得准、容器私有的 /dev/shm，与探针同形）。
    fleetboot.mine 给平台操作用的 state（有任务在跑就算 job、labmon 之外的一律未识别）是另一回事，不用这个。"""
    info, gp = st.get("info") or {}, st.get("gpu_procs") or []
    is_ka = lambda p: p.get("owner") == "keepalive" or bool(p.get("ka"))  # noqa: E731
    kind = lambda p: "keepalive" if is_ka(p) else "job" if p.get("owner") == "job" or p.get("pid") is not None else "unknown"  # noqa: E731
    kinds = {kind(p) for p in gp}
    gs = info.get("gpus") or []
    vt = sum(g.get("total") or 0 for g in gs) or None
    vr = sum(p.get("mb") or 0 for p in gp if not is_ka(p))
    util = round(sum(g.get("util") or 0 for g in gs) / len(gs), 1) if gs else None
    ur = util if vr else 0
    m = info.get("mem") or {}
    cr, mr = info.get("cpu_used"), m.get("used")
    mu = m.get("anon") if m.get("anon") is not None else mr      # 可共享按不算页缓存的（同 analyze，11-10 急修）
    runs, groups = {j.get("name"): j for j in st.get("running") or []}, {}
    for p in gp:
        k = kind(p)
        pk = p.get("hpid") if p.get("hpid") is not None else p.get("pid")
        key, label = (("job", p.get("job")), p.get("job")) if p.get("owner") == "job" else (("ka",), "保活") if k == "keepalive" \
            else (("p", pk), f"labmon 之外：{p.get('comm') or '进程'}") if k == "job" else (("p", pk), f"宿主机 PID {p.get('hpid')}（容器里找不到）")
        j = runs.get(p.get("job")) if p.get("owner") == "job" else None
        who = (job_owner or holder) if p.get("owner") == "job" else holder
        g = groups.setdefault(key, {"kind": k, "label": label, "mb": 0, "n": 0, "names": [], "cwd": j and j.get("root"),
                                    "etime": round(st["t"] - j["t0"]) if j and j.get("t0") and st.get("t") else None, **({"owner": who} if k == "job" else {})})
        g["mb"] += p.get("mb") or 0
        g["n"] += 1
    work = sorted(({**g, "mb": round(g["mb"])} for g in groups.values()), key=lambda w: (STATES.index(w["kind"]), -w["mb"]))
    return {"state": next((k for k in STATES if k in kinds), "idle"), "work": work, "util": util, "util_real": ur, "vram": sum(g.get("used") or 0 for g in gs),
            "vram_total": vt, "vram_real": vr, "vram_ka": sum(p.get("mb") or 0 for p in gp if is_ka(p)),
            "cpu": cr, "cpu_real": cr, "cpu_quota": info.get("cpu_quota"), "nproc": info.get("nproc"), "mem": m or None, "mem_real": mr, "mem_ka": None,
            "share": bool(cfg) and shareable(vr, vt, ur, cr, info.get("cpu_quota"), mu, m.get("limit"), cfg),
            # 各卡带上 worker 的吃满程度（gpu_busy：只有保活时按 0 的近 10 分钟、1 小时平均，#51）：组内页卡片与抽屉上的利用率按它（页面 fUtil、fCardU）
            "gpus": [{**{k: g.get(k) for k in ("i", "uuid", "name", "used", "total", "util")}, "busy": (st.get("gpu_busy") or {}).get(g.get("uuid"))} for g in gs],
            "root": info.get("rootfs"), "started": info.get("started")}


# ---------- 记录 ----------
def _event(kind: str, sid: str, msg: str, t: float, who: str | None = None, **extra) -> None:
    log_events([{"kind": kind, "host": sid, "who": who, "msg": msg, **extra}], t)


HOUR_NUMS = ("n", "sh", "uj", "vj", "cr", "mr", "nr")     # 小时行（与 bw 里每人一份）的计数；c 是各状态次数


def _add(acc: dict, a: dict | None) -> None:
    for k in HOUR_NUMS:
        acc.setdefault(k, 0)
    k = a["state"] if a else "down"
    acc["n"] += 1
    c = acc.setdefault("c", {})
    c[k] = c.get(k, 0) + 1
    if a:
        acc["sh"] += a["share"]
        if k == "job":
            acc["uj"] += a["util_real"] or 0
            acc["vj"] += a["vram_real"] or 0
        if a["cpu_real"] is not None and a["mem_real"] is not None:
            acc["cr"] += a["cpu_real"]
            acc["mr"] += a["mem_real"]
            acc["nr"] += 1


def _hour(st: dict, s: dict, a: dict | None, t: float, by: str | None = None) -> None:
    """计入当前小时；进了新的一小时就把上一小时写进 hours/<id>.jsonl。a 为 None 是掉线。
    by：借用中，这次样本另计进 acc["bw"][by]（行的总数照旧是整台的；按人统计时这部分算给借的人，见 fleetview._parts）。"""
    h = int(t // 3600 * 3600)
    acc = st.get("acc")
    if acc and acc["h"] != h:
        append_row(state_dir() / "hours" / f"{s['id']}.jsonl", acc, HOURS_KEEP, t)
        acc = None
    if not acc:
        acc = {"h": h, "holder": s["holder"], "c": {}}
    acc["holder"] = s["holder"]
    _add(acc, a)
    if by:
        _add(acc.setdefault("bw", {}).setdefault(by, {}), a)
    st["acc"] = acc


def record_mine(xs: list[dict], t: float, cfg: dict) -> None:
    """自用主机记进使用记录（每轮全局采集后调）：xs 是 fleetboot.mine 给了快照的各行。掉线记掉线；没有新鲜样本的（本机不在内网、
    worker 状态过期或停了）不记，等同探针的“没探到”。当前小时的累计在 mine.json（_tick 会删掉 state.json 里不在 [[fleet.servers]] 的键）。
    逐次样本 series/<id>.jsonl 与探针同形（_series_row），每台每 interval 最多一行（与探针同样的密度；上次写的时刻记在 series_t），
    组内页抽屉的曲线读它；小时行照旧每轮记。"""
    xs = [x for x in xs if x.get("sample") or x.get("state") == "down"]
    if not xs:
        return
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(state_dir() / "lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        sts = load_json(state_file("mine.json"), {})
        for x in xs:
            st, a = sts.setdefault(x["id"], {}), None if x.get("state") == "down" else x["sample"]
            _hour(st, x, a, t)
            if t - st.get("series_t", 0) >= cfg["interval"]:
                append_row(state_dir() / "series" / f"{x['id']}.jsonl", _series_row(a, t), SERIES_KEEP, t)
                st["series_t"] = round(t)
        save_json(state_file("mine.json"), sts)


def accs() -> dict:
    """各台当前小时还没写进 hours 的累计：{id: acc}（组内服务器在 state.json，自用主机在 mine.json）。"""
    return {k: v["acc"] for d in (load_json(state_file("mine.json"), {}), _load_state()) for k, v in d.items() if v.get("acc")}


def _brief_work(a: dict, but_ours: bool = False) -> str:
    """GPU 上在跑什么（给记录、提示看）：缺省不算保活；but_ours 时只去掉 labmon 开的那个保活（这人自己的保活也算）。"""
    w = [x for x in a.get("work", []) if (not x.get("ours") if but_ours else x["kind"] != "keepalive")]
    return "、".join(f"{x['label']}" for x in w[:2]) + (f" 等 {len(w)} 组" if len(w) > 2 else "")


def record(s: dict, raw: str | None, err: str | None, cfg: dict, st: dict, t: float) -> list[str]:
    """一次探测的结果：更新这台的状态、写曲线与小时累计、记事件。返回要弹通知的消息（只有掉线与恢复）。"""
    out, prev = [], st.get("last")
    st["last"] = t
    a = None
    if raw is not None:
        try:
            a = analyze(parse(raw), cfg)
        except Exception as x:  # noqa: BLE001 — 解析出错当作这次没探到，不影响别的服务器
            err = f"探针输出解析不了：{x}"
    sc = a.pop("scan", None) if a else None
    if sc and a["started"] and a.get("root"):      # 顺带扫了路径（scan_due）：存下，不进样本
        st["wrote"] = _wrote(sc, a, st.get("wrote"), t)
    label = f"{s['holder']}（{s['id']}）"
    if a is None:
        st["error"] = err
        st["idle_since"] = None
        if link_step(st, False, t) == "down" and not s.get("off"):     # 关机的服务器连不上是应该的
            end = lease_end(st, cfg)
            why = "租期到了，需在平台续租" if end and t >= end - 3600 else f"连不上（{err}）"
            st["down"] = why
            msg = f"组内服务器 {label} 掉线（{fmt_time(st['since'], date=False)} 起）：{why}"
            _event("fleet_down", s["id"], msg, t, s["holder"])
            out.append(msg)
    else:
        if link_step(st, True, t) == "up":
            msg = f"组内服务器 {label} 恢复连接（离线 {round((t - st['since']) / 60)} 分钟）"
            _event("fleet_up", s["id"], msg, t, s["holder"])
            out.append(msg)
        if a["started"] and st.get("started") != a["started"]:
            st.pop("guard_repair_at", None)
        ev = container_step(st, a["started"], label)
        if ev:
            log_events([{"host": s["id"], "who": s["holder"], **ev}], t)
        st.update(error=None, since=None)
        seen = st.setdefault("seen", [])
        for w in a["work"]:
            if w["kind"] == "unknown" and w["label"] not in seen:     # 未识别的只记事件，不弹通知
                seen.append(w["label"])
                _event("fleet_unknown", s["id"], f"{label} 有未识别的 GPU 进程：{w['label']}（{w['mb'] / 1024:.1f} GiB）", t, s["holder"])
        st["seen"] = seen[-50:]
        if a["state"] == "job" and not borrowed(s):    # 借着期间 GPU 上的是我们的任务：last_job 只记主人的（算力池按它判主人空闲了多久，#25）
            st["last_job"] = t
        # 连着空闲从哪次探测起（保活守护按它计时）；两次探测隔得太久（看板停过、离开内网）就从这次重新算
        keep = a["state"] == "idle" and prev and t - prev <= 2.5 * cfg["interval"]
        st["idle_since"] = (st.get("idle_since") if keep else None) or round(t) if a["state"] == "idle" else None
        g = a.get("guard") or {}
        if (g.get("end") or {}).get("how") == "yield" and st.get("guard_logged") != g["end"]["t"]:     # 服务器上盯着的那段时间里让出的
            st["guard_logged"] = g["end"]["t"]
            _event("guard_yield", s["id"], f"{label} 上有了 {_brief_work(a, True) or '别的 GPU 进程'}，{g.get('by') or '看板'}开的保活已让出", t, s["holder"])
        st["sample"] = {**a, "t": round(t)}
    _guard_track(s, st, cfg, t, a, prev)
    if a is not None or st.get("down"):              # 连不上但还没判掉线的那次不计，免得网络抖一下就记成掉线
        _hour(st, s, a, t, borrower(s, cfg) if borrowed(s) else None)
        append_row(state_dir() / "series" / f"{s['id']}.jsonl", _series_row(a, t, borrowed(s)), SERIES_KEEP, t)
    return out


def _series_row(a: dict | None, t: float, bw: bool = False) -> dict:
    """series/<id>.jsonl 的一行（探针与自用主机共用）：a 为 None 是掉线。bw：借用中，用量里有我们的（算给对方的预留时不取）。"""
    row = {"t": round(t), "st": a["state"] if a else "down", **({"bw": 1} if bw else {})}
    if a:
        r2 = lambda v: round(v, 2) if v is not None else None  # noqa: E731
        m = a.get("mem")
        row.update(u=a["util"], ur=a["util_real"], v=a["vram"], vr=a["vram_real"], vt=a["vram_total"], c=a["cpu"], cr=a["cpu_real"],
                   cq=a["cpu_quota"], m=r2(m["used"]) if m else None, ml=r2(m["limit"]) if m else None, mr=r2(a["mem_real"]),
                   ma=r2(a.get("mem_anon")), sh=a["share"], mk=r2(a.get("mem_ka")))
    return row


# ---------- 保活守护 ----------
def guard_on(s: dict, cfg: dict) -> bool:
    """这一台开没开保活守护：清单里写了 guard 就照它，没写的公用池开、专用池关。"""
    return bool(s["guard"]) if "guard" in s else s["pool"] not in cfg["private_pools"]


def _guard_stall(a: dict) -> dict | None:
    """这一轮样本里的空转身份（采样计时与决策共用一份）：看板登记的保活、所有卡都为零（zero_cards），或用户自行开启的（manual_guard）。"""
    g = a.get("guard") or {}
    if g.get("zero_cards"):
        identity = [*g["identity"], g["zero_cards"]]
        # 服务器上盯着的那段（until 之前）归远端看门狗管，过了才由探测重开
        return {"identity": identity, "target": identity, "at": g["until"]}
    return a.get("manual_guard")


def _guard_track(s: dict, st: dict, cfg: dict, t: float, a: dict | None, prev: float | None) -> None:
    """保存这一轮连续零利用率的起点。新容器、进程、卡数、缺样或任何活动都打断旧证据。
    停了没开成的缺口（guard_gap）只在见到肯定的变化时清：换了容器、GPU 上有了负载、借用中、关了守护；没探到的那次不动它。"""
    gap = st.get("guard_gap")
    if gap and (s.get("off") or borrowed(s) or not guard_on(s, cfg) or a and (a["state"] != "idle" or a["started"] != gap.get("container"))):
        st.pop("guard_gap")
    stall = _guard_stall(a or {})
    if not stall or s.get("off") or borrowed(s) or not guard_on(s, cfg):
        st.pop("guard_zero", None)
        return
    z = st.get("guard_zero") or {}
    identity = stall["identity"]
    if z.get("identity") != identity or z.get("container") != a["started"] or prev is None or not 0 <= t - prev <= 2.5 * cfg["interval"]:
        st["guard_zero"] = {"identity": identity, "container": a["started"], "since": t}


def _guard_repair(st: dict, stall: dict, t: float, managed: bool) -> dict | None:
    """空转的那一次判定（看板登记的与用户自行开启的共用）：同一保活的连续有效样本满 GUARD_ZERO_S 才作数，不到返回 None。
    作数时返回同一种形状 {act, status, fail, at, since, identity, why} 里用得上的几项：act 是 repair（重开）、warn（认不了归属，只提醒）
    或没有（退避中）；status、fail 是上次没成的那条。尝试过重开隔 GUARD_RETRY_S 再试，停了没开成（guard_gap）的不等。
    managed：这台配了保活脚本（只对用户自行开启的有意义：看板登记的本来就是经它开的）。"""
    z = st.get("guard_zero")
    if not z or z.get("identity") != stall["identity"] or t < stall.get("at", 0) or t - z["since"] < GUARD_ZERO_S:
        return None
    warning, target = z.get("warning"), stall["target"]
    shown = warning or st.get("guard_fail")
    out = {"status": "fail", "fail": shown} if shown else {}
    if not target or not managed:
        return out if warning else {"act": "warn", "why": "无法确认唯一的保活进程组" if managed else "未配置这台的保活脚本"}
    at = 0 if st.get("guard_gap") else st.get("guard_repair_at", t - GUARD_RETRY_S) + GUARD_RETRY_S
    if t < at:
        return {**out, "at": at} if out else None
    return {**out, "act": "repair", "since": z["since"], "identity": target}


def guard_state(s: dict, st: dict, cfg: dict, t: float, busy: bool = False) -> dict:
    """保活守护的判定（唯一一处）：这一台现在怎样、要不要动手。页面的状态与 guard_act 都取它。
    status：ours（labmon 开的保活在跑）、wait（GPU 连着空闲，计时中）、fail（上次没开成，隔一阵再试）、
    busy（有人在用，或在做平台操作）、off（没开守护）、down（掉线、关机、上次没探到）、borrowed（借来在用：保活由 worker 停启，守护不管）、
    worker（自用主机，fleetboot.mine 的行：保活一直由 worker 停启，守护从来不管它；#58 以前它的行没有 guard，页面当作没探到）。
    since：GPU 连着空闲从几点起（没开守护的也给，页面写现状）。
    act：yield（服务器上盯的时间已过，有了别的负载；关了守护也照样让）；repair（保活持续空转）、warn（自行开启的保活空转但无法核实）；
    start（空闲满 guard_idle_min 分钟，或上次重开停了旧的、新的没开成：gap，下次探测直接开）。
    按这个顺序判一次：让路 → 关守护、平台操作 → 空转 → 停了没开成 → 空闲计时。"""
    if s.get("mine"):
        return {"on": False, "status": "worker"}
    a, on = st.get("sample") or {}, guard_on(s, cfg)
    if borrowed(s):
        return {"on": on, "status": "borrowed"}
    if s.get("off") or st.get("down") or st.get("error") or not a:
        return {"on": on, "status": "down"}
    g, work = a.get("guard") or {}, a.get("work") or []
    reg = {"on": on, "status": "ours", "t": g["t"], "by": g.get("by"), "until": g["until"]} if g.get("running") else None
    ours = reg if any(w.get("ours") for w in work) else None          # 看板开的保活占着 GPU
    if reg and t >= g["until"] and not busy and any(not w.get("ours") for w in work):
        return {**(ours or {"on": on, "status": "busy"}), "act": "yield", "what": _brief_work(a, True)}
    ad = None if busy else _adopt_due(s, st, a)
    if ad:                                            # 关了守护也认：保活是 labmon 借用期间开的，登记了才有人替对方让路（#102）
        return {"on": on, "status": "busy", **ad}
    if not on:
        return ours or {"on": on, "status": "off", "since": st.get("idle_since")}
    if busy:
        return ours or {"on": on, "status": "busy", "why": "在做平台操作"}
    stall = _guard_stall(a)
    rep = _guard_repair(st, stall, t, stall is not a.get("manual_guard") or bool(ka_cmd(s, cfg))) if stall else None
    if rep:
        return {**(reg or {"on": on, "status": "busy"}), **rep}
    if ours:
        return ours
    if reg and not work and st.get("guard_zero"):     # 自己登记的进程还在、没占 GPU：等齐样本后重开，免得普通 start 反复收到“已经在跑”
        return {"on": on, "status": "busy", "why": "保活进程尚未占用 GPU"}
    gap = st.get("guard_gap")
    if gap and not reg and a.get("state") == "idle":
        return {"on": on, "status": "fail", "fail": st.get("guard_fail") or gap, "since": gap["t"], "at": gap["t"], "act": "start"}
    since, fail = st.get("idle_since"), st.get("guard_fail")
    if a.get("state") != "idle" or not since:
        return {"on": on, "status": "busy"}
    out = {"on": on, "since": since, "at": since + cfg["guard_idle_min"] * 60}
    if fail and t - fail["t"] < GUARD_RETRY_S:
        return {**out, "status": "fail", "fail": fail, "at": fail["t"] + GUARD_RETRY_S}
    return {**out, "status": "wait", **({"act": "start"} if t >= out["at"] else {})}


def _adopt_due(s: dict, st: dict, a: dict) -> dict | None:
    """还掉以后补登记（#102）：这台借过又还了（borrow_returned），GPU 上只有一个保活进程组、看板没登记、它的组长在上次借用期间
    （前后各留 60 秒）起来，这个身份（进程组、出生时钟）还没试过（st 的 guard_adopt）。是就给 adopt 动作：fleet_guard.sh 在现场按 ka.sh 的
    事件日志核对是不是这台的 worker 拉起的，认得出才登记成看板的保活。不是借用期间起来的（对方自己的）不动。"""
    kg = a.get("ka_group")
    since, back = parse_time(s.get("borrow_since")), parse_time(s.get("borrow_returned"))
    if not (kg and since and back) or not since - 60 <= kg["t"] <= back + 60:
        return None
    key = [kg["pgid"], kg["birth"]]
    if (st.get("guard_adopt") or {}).get("key") == key:
        return None
    return {"act": "adopt", "identity": key, "span": [round(since), round(back)]}


def ka_env(s: dict) -> str:
    """一台组内服务器上保活的配置 ka.env 在哪（借来时 worker 用的那份：主机模板的 ka_bin，没写是 worker_home/keepalive/bin，同 worker 的 KA_BIN）。"""
    hf = hosts_file()
    h = host_defaults(guest_host(s, _read_toml(hf).get("hosts", []) if hf.exists() else []))
    return (h.get("ka_bin") or h["worker_home"].rstrip("/") + "/keepalive/bin").rstrip("/") + "/ka.env"


def ka_adopt(s: dict, cfg: dict, since: float, until: float, mine: dict | None = None, ssh_fn=None) -> tuple[str, str]:
    """把卡上借用期间这台 worker 开的保活登记成看板的保活（fleet_guard.sh 的 adopt：不动进程，现场核对身份；borrow.finish 与补登记共用，#102）。
    mine：worker 状态的 keepalive.mine（{g, born}），有就按它认，没有就按 ka.sh 的事件日志认。返回 (ok | no | unsure | busy | fail | ssh, 说明)。"""
    ident = [mine["g"], mine["born"]] if mine and mine.get("g") and mine.get("born") is not None else []
    return _guard_run(s, cfg, ["adopt", int(cfg["guard_watch_min"] * 60), "借用归还", ka_env(s), "labmon-worker:" + str(s["id"]),
                               round(since), round(until), *ident], ssh_fn)


def ka_cmd(s: dict, cfg: dict) -> str | None:
    """这一台开、停保活的命令（唯一一处）：服务器自己写的优先，否则 [fleet] 段的；都没写是 None。"""
    return s.get("keepalive_cmd") or cfg.get("keepalive_cmd")


def _guard_run(s: dict, cfg: dict, args: list, ssh_fn=None) -> tuple[str, str]:
    """在这一台上跑 fleet_guard.sh（一条 ssh，脚本经标准输入送去）。返回 (ok | busy | fail | unmanaged | gap | ssh, 说明)：
    gap 是重开时停了旧的、新的没开成，ssh 是连不上。"""
    cmd = ka_cmd(s, cfg)
    if not cmd:
        return "fail", "[fleet] 没写 keepalive_cmd"
    rc, out, err = (ssh_fn or ssh)(s, "bash -s -- " + shlex.join([args[0], cmd, *map(str, args[1:])]), input=GUARD.read_text(), timeout=120)
    res, note = ((out.strip().splitlines() or [""])[-1].split(None, 1) + ["", ""])[:2]
    if res in ("ok", "busy", "fail", "unmanaged", "gap", "no", "unsure"):
        return res, note
    tail = (err or out).strip().splitlines()
    return ("ssh" if rc == 255 else "fail"), tail[-1] if tail else f"退出码 {rc}"


def ka_start(s: dict, cfg: dict, by: str, ssh_fn=None) -> tuple[str, str]:
    """在这一台上开保活（守护、手动、续租后都走这里）：已在跑不重开（ok already）、GPU 上还有进程不开（busy N）、
    开了（ok start 进程组）以后在服务器上盯 guard_watch_min 分钟，有实验上来就让出。by：谁开的，记在服务器上。"""
    return _guard_run(s, cfg, ["start", int(cfg["guard_watch_min"] * 60), by], ssh_fn)


def ka_stop(s: dict, cfg: dict, why: str, ssh_fn=None) -> tuple[str, str]:
    """停掉 labmon 在这一台上开的那个保活（进程组对得上才停）：ok stopped / ok gone（已经不在）/ ok notours（不是我们开的）。"""
    return _guard_run(s, cfg, ["stop", why], ssh_fn)


def guard_act(s: dict, st: dict, cfg: dict, t: float, busy: bool, ssh_fn=None) -> tuple[str | None, list[str]]:
    """探完一台后按 guard_state 开、重开或让出。返回 (动作, 要弹的通知：只有没开成，连着失败只弹一次；停了没开成的每次都弹)。"""
    g = guard_state(s, st, cfg, t, busy)
    label = f"{s['holder']}（{s['id']}）"
    act = g.get("act")

    def warn(why):
        zero = st["guard_zero"]
        if zero.get("warning"):
            return None, []
        msg = f"{label} 自行开启的保活持续空转，{why}，请核对保活脚本"
        zero["warning"] = {"msg": msg}
        _event("guard_fail", s["id"], msg, t, s["holder"])
        return None, [msg]

    if act == "warn":
        return warn(g["why"])
    if act == "adopt":
        res, note = ka_adopt(s, cfg, *g["span"], ssh_fn=ssh_fn)
        if res in ("busy", "ssh"):                   # GPU 上有别的进程、查不到、连不上：下次探测再看
            return None, []
        st["guard_adopt"] = {"key": g["identity"], "t": round(t), "res": res}   # 同一个身份只试一次
        if res == "ok":
            if note.startswith("adopt"):
                _event("guard_adopt", s["id"], f"{label} 卡上的保活是借用期间 labmon 的 worker 开的，已登记成看板的保活"
                       f"（盯 {cfg['guard_watch_min']} 分钟，之后靠探测：有实验上来就让出）", t, s["holder"])
            return "adopt", []
        if res == "no":                              # 没有保活了、有几个、不是借用期间起来的：就是对方的，不动
            return None, []
        msg = f"{label} 卡上的保活是借用期间起来的，{note}：没登记成看板的保活，{s['holder']} 的实验上来时没人替这人停它，请核对"
        _event("guard_fail", s["id"], msg, t, s["holder"])
        return None, [msg]
    if act in ("start", "repair"):
        repair = act == "repair"

        def delay(why):
            key = [act, g["since"]]
            deferred = st.get("guard_delay") or {}
            if deferred.get("key") != key:
                deferred = st["guard_delay"] = {"key": key, "reasons": []}
            if why not in deferred["reasons"]:
                deferred["reasons"].append(why)
                _event("guard_delay", s["id"], f"{label} 延后{'重开' if repair else '开'}保活：{why}，下次探测再看", t, s["holder"])
            return None, []

        manual = repair and g["identity"][1] == "manual"
        if repair:
            res, note = _guard_run(s, cfg, ["repair", int(cfg["guard_watch_min"] * 60), "守护", *g["identity"]], ssh_fn)
            if res != "busy":
                st["guard_repair_at"] = round(t)
        else:
            res, note = ka_start(s, cfg, "守护", ssh_fn)
        if res == "busy":
            return delay(note if repair and not note.isdecimal() else "启动前复查时 GPU 上又有了进程")
        gap = st.pop("guard_gap", None)            # 有它的这次是“停了没开成”之后的那一次快速再试：真试了就只给这一次
        if res == "unmanaged":
            return warn(note)
        st.pop("guard_delay", None)
        if res == "ok":
            st.pop("guard_fail", None)
            if note.startswith("start"):
                minutes = round((t - g["since"]) / 60)
                if gap:
                    msg = f"{label} 上次停了空转的保活、新的没开起来，守护这次{'重开' if repair else '补开'}了保活"
                elif manual:
                    msg = f"{label} 连着 {minutes} 分钟没测到 GPU 活动，守护已停掉自行开启的空转保活并开启看板保活"
                elif repair:
                    msg = f"{label} 连着 {minutes} 分钟没测到 GPU 活动，守护重开了空转的保活"
                else:
                    msg = f"{label} 的 GPU 连着空了 {minutes} 分钟，守护开了保活"
                _event("guard_restart" if repair or gap else "guard_start", s["id"],
                       f"{msg}（盯 {cfg['guard_watch_min']} 分钟，有实验上来就让出）", t, s["holder"])
                return "start", []
            return "already", []                   # 探完才出现的保活也要刷新样本，不另记一条启动
        if res == "gap" and not gap:               # 旧的停了、新的没开成：GPU 上此刻没有保活，下次探测直接再开（不等空闲计时与退避）
            why = f"停掉了{'自行开启的' if manual else ''}空转保活，新的没开起来：{note}"
            st["guard_gap"] = {"t": round(t), "msg": why, "container": (st.get("sample") or {}).get("started")}
            st["guard_fail"] = {"t": round(t), "msg": why}
            msg = f"保活守护在 {label} 上{why}"
            _event("guard_fail", s["id"], msg + "（下次探测直接再开）", t, s["holder"])
            return "fail", [msg]
        first = "guard_fail" not in st
        st["guard_fail"] = {"t": round(t), "msg": note}
        msg = f"保活守护没能给 {label} {'重开' if repair else '开'}保活：{note}"
        _event("guard_fail", s["id"], msg + f"（{GUARD_RETRY_S // 60} 分钟后再试）", t, s["holder"])
        return "fail", [msg] if first else []
    if g.get("act") == "yield":
        res, note = ka_stop(s, cfg, g["what"], ssh_fn)
        if res == "ok" and note == "stopped":
            _event("guard_yield", s["id"], f"{label} 上有了 {g['what']}，停掉了{g.get('by') or '看板'}开的保活", t, s["holder"])
            return "yield", []
        if res != "ok":
            _event("guard_fail", s["id"], f"{label} 上有了 {g['what']}，没能停掉{g.get('by') or '看板'}开的保活：{note}", t, s["holder"])
    return None, []


def guard_conf(cfg: dict | None) -> dict:
    """页面上「保活守护」要的设置：空闲多久开、开后盯多久（每台开没开在各台的 guard_state 里）。"""
    if not cfg:
        return {}
    return {"idle_min": cfg["guard_idle_min"], "watch_min": cfg["guard_watch_min"]}


def set_guard(sid: str | None = None, on: bool | None = None, idle_min=None, watch_min=None) -> None:
    """页面上改保活守护：某一台开或关（写进它的 [[fleet.servers]] 块），或空闲多久开、开后盯多久（写进 [fleet]）。改动进记录。"""
    cfg = conf() or {}
    if sid is not None:
        s = next((x for x in cfg.get("servers", []) if x["id"] == sid), None)
        if not s:
            raise ConfigError(f"组内服务器里没有 {sid}")
        set_fleet_keys({"guard": bool(on)}, f"{'打开' if on else '关掉'} {s['holder']}（{sid}）的保活守护", sid=sid)
        return
    keys = {}
    for k, v, lo, hi, name in (("guard_idle_min", idle_min, cfg.get("interval", 600) // 60, 24 * 60, "空闲多久开"),
                               ("guard_watch_min", watch_min, 0, 60, "开后盯多久")):
        if v is None:
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ConfigError(f"{name}要写整数分钟") from None
        if not lo <= v <= hi:
            raise ConfigError(f"{name}要在 {lo}–{hi} 分钟之间")
        keys[k] = v
    if keys:
        set_fleet_keys(keys, "改保活守护的时间")


def lease_end(st: dict, cfg: dict) -> float | None:
    return _lease_end(st.get("started"), cfg.get("lease_days"))


def lease_notices(cfg: dict, sts: dict, t: float, off=()) -> list[str]:
    """组内服务器的租期提醒（判定在 health.lease_due，与看板主机共用：到期前 24、6 小时各一次，已进 6 小时内的不再报 24；
    同一轮里相近到期（MERGE_S 内）的合成一条）。关机的服务器不提醒。"""
    srv = [s for s in cfg["servers"] if s["id"] not in off]
    ends = [(sts.get(s["id"]) or {}, lease_end(sts.get(s["id"]) or {}, cfg)) for s in srv]
    out = []
    for grp in lease_due(ends, t, MERGE_S):
        msg = "组内服务器租期将到：" + "、".join(f"{srv[i]['holder']} 约剩 {(ends[i][1] - t) / 3600:.0f} 小时（{fmt_time(ends[i][1])}）" for i in grp)
        for i in grp:
            _event("lease", srv[i]["id"], msg, t, srv[i]["holder"])
        out.append(msg)
    return out


def _load_state() -> dict:
    return load_json(state_dir() / "state.json", {})


def _save_state(sts: dict) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    save_json(state_dir() / "state.json", sts)


def tick(t: float | None = None, fetch_fn=None, cfg: dict | None = None, force: str | None = None, ssh_fn=None, busy=()) -> list[str]:
    """看板每分钟调一次：最多探一台（到期最久的；刚连不上的隔一分钟再试，确认掉线后回到正常间隔），探完按保活守护开保活或让出，
    再看租期提醒。返回要弹的通知。
    force：立即探这一台（页面上点的）。busy：正在做平台操作的服务器（fleetops.busy()，调用方给）：不探（停机那几分钟不算掉线），
    守护也不动它。fetch_fn、ssh_fn：测试替换用（只换了 fetch_fn 时不动手）。
    这台看板服务是备用（服务租约，#163）时什么都不做：看板线程、命令行 labmon fleet probe、平台操作后的探测都经这里。"""
    if not LS.primary():
        return []
    cfg = cfg if cfg is not None else conf()
    if not cfg or not cfg["servers"]:
        return []
    probe = fetch_fn is not None or L.check(t)["ok"]     # 本机不在内网：不探（不记连不上、不报掉线），租期照常提醒
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(state_dir() / "lock", "w") as lk:      # 看板线程与命令行 labmon fleet probe 不同时改状态
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _tick(time.time() if t is None else t, fetch_fn or fetch, cfg, force, probe, fetch_fn is None,
                     ssh_fn or (ssh if fetch_fn is None else None), busy)


def off_servers() -> dict:
    """在看板上关了机的服务器：{id: {"t", "holder"}}（记在 boot.json，开机时去掉）。"""
    return {k: e["off"] for k, e in load_json(state_file("boot.json"), {}).items() if e.get("off")}


def _tick(t: float, fetch_fn, cfg: dict, force: str | None, probe: bool = True, lan: bool = False, ssh_fn=None, busy=()) -> list[str]:
    sts = _load_state()
    for k in [k for k in sts if k not in {s["id"] for s in cfg["servers"]}]:
        sts.pop(k)                                   # 清单里删掉的服务器
    out = []

    def due(s):
        st = sts.get(s["id"]) or {}
        gap = TICK if st.get("fails") and not st.get("down") else cfg["interval"]
        return t - st.get("last", 0) - gap
    off = off_servers()                              # 关了机的服务器不探
    pick = next((s for s in cfg["servers"] if s["id"] == force), None) if force else \
        max((s for s in cfg["servers"] if s["id"] not in busy and s["id"] not in off and due(s) >= -1), key=due, default=None)
    if pick and probe:
        scan = None if force else scan_due(pick, sts.get(pick["id"]) or {}, t, pick["id"] in off)   # 点的、平台操作要的立即探测不顺带扫（要快）
        raw, err = fetch_fn(pick, scan) if scan else fetch_fn(pick)
        if raw is not None or not lan or L.check(t)["ok"]:        # 连不上时先看是不是刚离开内网：是就不记
            st = sts.setdefault(pick["id"], {})
            out += record({**pick, "off": pick["id"] in off}, raw, err, cfg, st, t)
            if ssh_fn and raw is not None:
                act, more = guard_act(pick, st, cfg, t, pick["id"] in busy, ssh_fn)
                out += more
                if act in ("start", "already", "adopt"):   # 已确认保活占 GPU、登记了：再探一次，页面马上看到
                    raw, err = fetch_fn(pick)
                    out += record(pick, raw, err, cfg, st, t + 1)
    out += lease_notices(cfg, sts, t, off)
    _save_state(sts)
    return out
