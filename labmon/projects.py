"""项目的登记、配置推拉（#161）与归档、恢复（#97，#71 单元 ⑤）。

登记（#161）：看板上的项目都在 <状态根>/projects/<id>/ 下，取代以前 launchd plist 的 -c：
    monitor.toml   项目配置，以这份为准（页面上改的改这份；项目目录里的是工作副本，labmon project push|pull、labmon wait 每轮同步，projsync.py）
    files/         done_when 指的文件（随推上传，config.done_key 定位置；采集读这里的）
    out/           输出目录：快照、页面与 .labmon/ 的记忆（monitor.toml 的 out_dir 不再生效）
    meta.json      {since 登记时刻, owner 项目主人（第一次经接口推的账号；为空的归管理员，#167：普通用户只能动自己的项目；管理员能换，set_owner）, by}
第一次推就是登记；版本号是文本的哈希（ver_of），推时带上次同步的版本，对不上回 412（put_config）。看板服务按登记加载项目（serve.Server.sync）。

归档：项目做完了在看板上收起，要时再加回；命令行（labmon project archive|restore）与页面（POST /project/<动作>）同用这里。

- 归档要求项目暂时没实验（archive_block，口径同 collect._no_work）。做的事：移除本项目在各主机上的登记（已归还的经共用 worker_home 的在用主机代办，workerctl.via），
  撤按主机下的暂停、优先，撤主机清单 [sched] 里它的权重、暂停、优先，记进状态目录的 projects.json。不删数据：登记目录（配置、输出）、共享盘、用量记录都留着。
- 看板服务按 projects.json 摘下与加回（serve.Server.sync）：启动时跳过已归档的，运行中每轮全局采集前对齐。
- 恢复只把它从 projects.json 去掉（看板服务加回采集）；登记不自动恢复，要跑由实验会话再 worker add（归档时移除了哪些记在条目的 regs 里，页面列出来）。
- 永久删除由管理员在归档列表操作。只删除看板上的登记目录、归档条目及闲置池工作区绑定，写一条审计。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import threading
import time
import tomllib
import uuid
from contextlib import contextmanager
from pathlib import Path
from stat import S_ISDIR

from . import lan as L
from . import workerctl as W
from .config import HOSTS_LOCK, ConfigError, _expand, _write_checked, done_files, done_key, fmt_dur, fmt_time, load, sched, set_project, set_sched_ovr, set_weight
from .records import log_events, tell
from .state import load_json, load_rows, save_json, snapshot, state_dir, state_root, write_atomic

FRESH_S = 600                 # 项目看板与工作台的快照超过这么久（且超过 3 个采集间隔）没更新，不让归档：说不准还有没有实验
_LOCK = threading.Lock()


def _file():
    return state_dir() / "projects.json"


@contextmanager
def _locked():
    """projects.json 读 → 改 → 写的整段互斥：同一进程里的线程（两个页面同时点）用 _LOCK，看板服务与命令行、两个命令行之间用旁边 .lock 上的 flock。
    不锁的话两边各读一份、各删自己的条目，后写的把先写的那条又写回去。"""
    with _LOCK:
        _file().parent.mkdir(parents=True, exist_ok=True)
        with open(_file().with_suffix(".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            yield


def archived() -> dict:
    """已归档的项目 {id: {id, title, t（归档时刻）, by（谁做的：看板页面上 / 用命令行；#121 之前的没有）, regs（归档时移除的登记 [{host, root, jobs}]）, skipped（没移除成的说明）}}。"""
    d = load_json(_file(), {})
    a = d.get("archived") if isinstance(d, dict) else None
    return a if isinstance(a, dict) else {}


def archived_msg(pid: str) -> str | None:
    """项目已归档时给实验会话的那句（labmon wait 每次都报、labmon check 也列出）：何时、谁归档的，要接着用怎么办；没归档返回 None。"""
    e = archived().get(pid)
    if not e:
        return None
    return (f"项目 {pid} 已于 {fmt_time(e['t'])} {e.get('by') or ''}归档，看板不再采集它，各主机上的登记已移除；"
            f"要接着用先 labmon project restore {pid}，再 labmon worker add 登记清单")


def archived_rows() -> list[dict]:
    """当前归档列表，新归档的在前；工作台页面与快照的顺序一致。"""
    return sorted(archived().values(), key=lambda e: -(e.get("t") or 0))


def _save(a: dict) -> None:
    save_json(_file(), {"archived": a})


# ---------- 登记（#161） ----------
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")   # 项目 id 是看板地址与登记目录名
FILE_MAX = 1 << 20            # done_when 的文件每个最多这么大（字节）
OUT_NAMES = ("index.html", "snapshot.json", ".labmon")   # 可删（#161 迁移）：labmon 在以前的 out_dir 里写的（页面、快照、状态与记忆），迁移只搬这几样
GUARDED = ("hosts", "queues")   # 配置里连主机、在主机或本机上跑命令（[[hosts.checks]]、python_local）、读主机上文件（队列的路径）的部分：
                              # 经接口改它要平台操作的权限（users.can_ops），project 作用域的令牌不能借推配置执行命令
_REG = threading.Lock()


class Refused(Exception):
    """推、取配置不成：code 是 HTTP 状态码（400 配置不对、403 改了 GUARDED 又没有平台操作的权限、404 没登记、409 已归档、412 版本对不上、428 没带版本），msg 给人看，ver 是看板上现在的版本（412、428 时带）。"""

    def __init__(self, code: int, msg: str, ver: str | None = None):
        super().__init__(msg)
        self.code, self.msg, self.ver = code, msg, ver


def root() -> Path:
    return state_root() / "projects"


def _valid(pid: str) -> None:
    if not ID_RE.fullmatch(pid):
        raise Refused(400, f"项目 id {pid!r} 不行：只能用字母、数字与 _ . -，字母或数字开头，最长 64（它是看板地址与登记目录名）")


def cfg_file(pid: str) -> Path:
    return root() / pid / "monitor.toml"


def files_dir(pid: str) -> Path:
    return root() / pid / "files"


def out_dir(pid: str) -> Path:
    """项目的输出目录（快照、页面、.labmon/ 的记忆）：看板服务、labmon collect、labmon wait 都按它找。"""
    return root() / pid / "out"


def ver_of(data: str | bytes) -> str:
    """版本号：内容的 SHA-256 前 16 位（一样的内容版本一样，两边改成同一份不算冲突）。配置文本与 done_when 的文件同一个算法。"""
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()[:16]


@contextmanager
def _reg_locked():
    """登记目录读 → 改 → 写的整段互斥：页面上改配置（看板服务的线程）与命令行推（另一个进程）不会各读一份旧版本、后写的盖掉先写的。
    线程之间 _REG，进程之间 projects/registry.lock 上的 flock。也要锁归档记录时，先锁登记。"""
    with _REG:
        root().mkdir(parents=True, exist_ok=True)
        with open(root() / "registry.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            yield


def meta(pid: str) -> dict:
    return load_json(root() / pid / "meta.json", {})


def owner(pid: str) -> str | None:
    """meta.json 里记的项目主人（#167）；没登记、为空（命令行推的、迁移来的）是 None。判谁是主人只经 users.owner_of（对上现有账号，#19），别处不直接读它。"""
    o = meta(pid).get("owner")
    return str(o) if o else None


def set_owner(pid: str, who: str | None, by: str, *, previous: str | None = None) -> str:
    """管理员换项目主人（#167，项目「管理」抽屉的「主人」；POST /project/owner）：who 为空回到归管理员。记一条配置改动、告诉实验会话一次。"""
    with _reg_locked():
        return _set_owner(pid, who, by, previous=previous)


def _set_owner(pid, who, by, *, previous=None):
    """调用方持有登记锁；账号删除与池领取按同一事务核对。"""
    _valid(pid)
    f = root() / pid / "meta.json"
    if not cfg_file(pid).is_file():
        raise ConfigError(f"看板上没有项目 {pid}")
    m = meta(pid)
    old = m.get("owner") or None
    if previous is not None and (old or "").lower() != previous.lower():
        return ""
    if (old or "").lower() == (who or "").lower():
        return f"{pid} 的主人本来就是 {who or '管理员'}"
    save_json(f, {**m, "owner": who or None})
    msg = f"{by} 把项目 {pid} 的主人从 {old or '管理员'} 换成 {who or '管理员'}"
    log_events([{"kind": "config", "project": pid, "msg": msg}])
    tell(pid, "owner", f"{msg}：之后由 {who or '管理员'} 的账号与令牌管这个项目，别的普通用户的令牌做不了它的写操作")
    return msg


def registered() -> list[str]:
    """登记了的项目 id（有 monitor.toml 的子目录，含已归档的），按登记先后。"""
    if not root().is_dir():
        return []
    ids = [d.name for d in root().iterdir() if (d / "monitor.toml").is_file()]
    return sorted(ids, key=lambda i: (meta(i).get("since") or 0, i))


def project_ids() -> list[str]:
    """看板上的项目 id（登记了、没归档）：worker add --project、pause、focus 的目标要是其中之一，页面只链到这些。"""
    gone = archived()
    return [pid for pid in registered() if pid not in gone]


def load_cfg(pid: str) -> dict:
    """看板上这个项目的配置（done_when 的文件取登记目录 files/ 里的）。"""
    return load(cfg_file(pid), files=files_dir(pid))


def _files(pid: str) -> dict:
    d = files_dir(pid)
    return {str(f.relative_to(d)): ver_of(f.read_bytes()) for f in sorted(d.rglob("*")) if f.is_file()} if d.is_dir() else {}


def require(pid: str) -> None:
    """项目要登记过（含已归档的）：没登记、id 写法不对抛 Refused(404)。取配置、wait、只读命令（labmon status|disk|records）共用这一句。"""
    if not ID_RE.fullmatch(pid or "") or not cfg_file(pid).is_file():
        raise Refused(404, f"看板上没有项目 {pid}：在项目目录里 labmon project push 登记")


def get_config(pid: str) -> dict:
    """看板上的配置：{id, ver, text, files {files/ 下的位置: 版本}, archived}。没登记抛 Refused(404)。"""
    require(pid)
    text = cfg_file(pid).read_text()
    return {"id": pid, "ver": ver_of(text), "text": text, "files": _files(pid), "archived": pid in archived()}


def check_config(pid: str, text: str, files: dict) -> dict:
    """推上来的配置与文件（labmon check 经 api.check 也用它校验项目目录里那份）：校验不过抛 Refused(400)，返回校验好的配置。"""
    _valid(pid)
    try:
        cfg = load(cfg_file(pid), text, files_dir(pid))
    except ConfigError as e:
        raise Refused(400, str(e)) from e
    except Exception as e:  # noqa: BLE001 — 写错类型（如 alerts = 5）load 抛的不是 ConfigError：推上来的同样拒绝，不写进登记目录
        raise Refused(400, f"配置读不了：{e!r}") from e
    if cfg["id"] != pid:
        raise Refused(400, f"配置里的 id 是 {cfg['id']}，与要推的项目 {pid} 不一样（改 id 等于另一个项目）")
    for k, v in files.items():
        if v is not None and (not isinstance(v, str) or len(v.encode()) > FILE_MAX):
            raise Refused(400, f"done_when 的文件 {k} 要是不超过 {FILE_MAX >> 10} KiB 的文本")
    keys = {done_key(s) for s in done_files(cfg)}
    clash = next((k for k in sorted(keys) if any(o.startswith(k + "/") for o in keys)), None)
    if clash:
        raise Refused(400, f"done_when 的文件 {clash} 又是别的 done_when 文件的上级目录：两个放不到一起")
    return cfg


def _guarded(text: str) -> dict:
    """配置里 GUARDED 那几段（按 TOML 解析后比，空白、注释与顺序之外的改动才算改了）；调用方已校验过 text。"""
    d = tomllib.loads(text)
    return {k: d.get(k) for k in GUARDED}


def _store(pid: str, text: str, files: dict, cfg: dict, owner: str | None, by: str) -> dict:
    """写进登记目录（调用方已持 _reg_locked、已校验）：先删掉不再用到的 done_when 文件与空了的目录（路径从 a 换成 a/b 或反过来时让开），
    再照推上来的写用到的（空值删掉、没带的不动）；文本变了才写、记一条配置改动。"""
    p, new = cfg_file(pid), not cfg_file(pid).is_file()
    srcs, d = done_files(cfg), files_dir(pid)
    keep, did = {done_key(x) for x in srcs}, []
    if d.is_dir():
        for f in [f for f in d.rglob("*") if f.is_file() and str(f.relative_to(d)) not in keep]:
            f.unlink()
            did.append(f"删了不再用的 {f.relative_to(d)}")
        for x in sorted((x for x in d.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            if not any(x.iterdir()):
                x.rmdir()
    for src in srcs:
        if src not in files:
            continue
        f = d / done_key(src)
        if files[src] is None:
            if f.exists():
                f.unlink()
                did.append(f"删了 {src}（本地没有）")
        elif not f.is_file() or f.read_text() != files[src]:
            write_atomic(f, files[src])
            did.append(f"更新了 {src}")
    if new:
        write_atomic(p, text)
        save_json(p.parent / "meta.json", {"since": round(time.time()), "owner": owner, "by": by})
        out_dir(pid).mkdir(parents=True, exist_ok=True)
        log_events([{"kind": "project", "msg": f"登记了项目 {pid}（{cfg.get('title') or pid}，{by}）"}])
    elif p.read_text() != text:
        _write_checked(p, text, f"推上来了 {pid} 的 monitor.toml（{by}）", check=lambda t: load(p, t, d))
        did.insert(0, "配置")
    return {"id": pid, "ver": ver_of(text), "created": new, "changed": did}


def put_config(pid: str, text: str, files: dict | None = None, base: str | None = None, owner: str | None = None, by: str = "用命令行",
               ops: bool = True, check_owner=None) -> dict:
    """推配置（labmon project push、labmon wait 的同步、PUT /api/project/<id>/config 同用）。files：done_when 用到的文件 {monitor.toml 里写的路径: 内容（本地没有为 None）}。
    base：上次同步时的版本。还没登记而且没带 base 的就是第一次推；带了 base 但项目不存在返回 412；已登记的 base 要等于现在的版本，没带回 428、对不上回 412（先拉）；已登记又已归档的 409
    （归档了、看板上却没有配置的照样能推进来，推完再 restore）。整套 config.load 校验、id 要与 pid 相同，不过就不写。
    ops：调用方有没有平台操作的权限（接口按 users.can_ops 给；命令行与迁移在看板那台机器上直接调，算有）：没有时改了 GUARDED（含第一次推就带着）回 403。
    返回 {id, ver, created, changed（改了什么，空的是没改）, archived}。"""
    files = files or {}
    _valid(pid)
    with _reg_locked():
        if check_owner is not None:
            check_owner()
        p, gone = cfg_file(pid), pid in archived()
        if p.is_file():
            if gone:
                raise Refused(409, archived_msg(pid) or f"项目 {pid} 已归档")
            cur = ver_of(p.read_text())
            if base is None:
                raise Refused(428, f"看板上已有项目 {pid}：推要带上次同步的版本；项目目录里没有同步记录的，先 labmon project pull 再改、再推", cur)
            if base != cur:
                raise Refused(412, f"看板上 {pid} 的配置在你上次同步之后改过（多半是页面上改的）：先 labmon project pull 拉回来，再改、再推", cur)
        cfg = check_config(pid, text, files)
        if not p.is_file() and base is not None:
            raise Refused(412, f"看板上没有项目 {pid}：要重新登记，请去掉上次同步的版本再推")
        if not ops and _guarded(text) != _guarded(p.read_text() if p.is_file() else ""):
            raise Refused(403, "这次推改了 [[hosts]] 或 [[queues]]（连哪台主机、在主机上跑什么检查、读哪些文件）：经接口改这两段要管理员的 admin 令牌从内网入口推"
                               "（labmon login --admin 后 labmon project push --admin）；别的项照常能推")
        return dict(_store(pid, text, files, cfg, owner, by), archived=gone)


def set_config(pid: str, changes: dict, check_owner=None) -> list[str]:
    """页面上改看板上这份配置（config.set_project）：与推在同一把锁里，谁也不盖掉谁；推的一方随后按版本对不上处理。"""
    # 和删账号同序：先主机配置锁，再项目登记锁。
    with HOSTS_LOCK, _reg_locked():
        if check_owner is not None:
            check_owner()
        if not cfg_file(pid).is_file():
            raise ConfigError(f"看板上没有项目 {pid}")
        return set_project(cfg_file(pid), changes)


def adopt(config: str) -> str | None:
    """可删（#161 迁移，只为当时 plist 里 -c 的项目跑一次）：部署后跑过一次 labmon service install（plist 去掉 -c）就删，连同 serve 的 legacy 参数与 service 里读旧 -c 的那几行。
    把以前用 -c 加进看板服务的项目登记进来：配置与 done_when 的文件推进登记目录、原 out_dir 里 labmon 写的（OUT_NAMES：页面、快照、.labmon/ 的记忆；
    out_dir 是项目目录或混着别的东西时，源码、.git 等原样不动）搬到登记的 out/、
    历史里「本机」一栏的 __local__ 行去掉、项目目录写同步记录（projsync.mark）、告诉那个项目的实验会话。已登记的不动，返回 None；做了返回说明。"""
    from .projsync import mark                         # projsync 在顶层 import 本模块
    c = Path(config).expanduser().resolve()
    text = c.read_text()
    cfg = load(c)
    pid = cfg["id"]
    _valid(pid)
    if cfg_file(pid).is_file():
        return None
    local = {s: Path(_expand(s, c.parent)) for s in done_files(cfg)}
    files = {s: (f.read_text() if f.is_file() else None) for s, f in local.items()}
    with _reg_locked():
        _store(pid, text, files, check_config(pid, text, files), None, "看板升级时从 launchd 的 -c 迁来")
    old = Path(_expand(str(tomllib.loads(text).get("out_dir") or f"~/labmon_out/{pid}"), c.parent))
    moved = []
    if old.is_dir() and old.resolve() != out_dir(pid).resolve():
        for f in [old / n for n in OUT_NAMES if (old / n).exists()]:
            dst = out_dir(pid) / f.name
            if not dst.exists():
                shutil.move(str(f), str(dst))
                moved.append(f.name)
        if not any(old.iterdir()):
            old.rmdir()
    h = out_dir(pid) / ".labmon" / "history.jsonl"
    if h.is_file():
        write_atomic(h, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in load_rows(h) if r.get("host") != "__local__"))
    mark(c, pid, ver_of(text))
    msg = (f"项目 {pid} 登记进看板（{cfg_file(pid).parent}）：配置以看板上那份为准，{c} 是工作副本；"
           f"输出目录从 {old} 搬到 {out_dir(pid)}（{len(moved)} 项）")
    tell(pid, "upgrade", "看板升级（#161）：monitor.toml 以看板上登记的那份为准，项目目录里这份是工作副本。重挂 labmon wait（它每轮同步：本地改了推上去、页面上改了拉回来；"
         "两边都改了会提示先 labmon project pull）；没挂 wait 时改了配置用 labmon project push。接入新项目用 labmon project push，不再用 labmon service add；"
         "out_dir 与 [local] 不再生效，不用改。项目目录多了 .labmon/（自带 .gitignore，不用提交）", src="看板升级")
    log_events([{"kind": "project", "msg": msg}])
    return msg


def archive_block(pid: str, psnap: dict | None, glob: dict | None, interval: float = 60, t: float | None = None) -> str | None:
    """这个项目现在能不能归档：能返回 None，不能返回一句原因（页面上按钮旁写的、命令行与页面拒绝时报的都是它）。
    psnap 是项目看板的快照，glob 是工作台的全局快照。口径同 collect._no_work：清单里没有待办（车道的 unfinished），
    worker 上也没有本项目在跑、在等的（含单元与占位任务），且项目快照说暂时没实验（no_work_since）。"""
    t = time.time() if t is None else t
    if pid in archived():
        return "已经归档了"
    glob, lim = glob or {}, max(FRESH_S, 3 * interval)
    if not psnap or not psnap.get("generated"):
        return "项目看板还没采集过，等第一轮采集完再归档"
    if t - psnap["generated"] > lim:
        return f"项目看板 {fmt_dur(t - psnap['generated'])}没更新，等采集恢复后再归档"
    if not glob.get("generated") or t - glob["generated"] > lim:
        return "工作台的数据" + (f" {fmt_dur(t - glob['generated'])}" if glob.get("generated") else "还") + "没更新，等采集恢复后再归档"
    n = {k: sum(1 for e in glob.get("hosts") or [] for j in (e.get("status") or {}).get(k) or [] if j.get("project") == pid) for k in ("running", "waiting")}
    left = sum(ln.get("unfinished") or 0 for ln in psnap.get("lanes") or [])
    why = [f"{n['running']} 个任务在跑" if n["running"] else "", f"{n['waiting']} 个在等" if n["waiting"] else "", f"清单里还有 {left} 个没跑完" if left else ""]
    if any(why):
        return "还有实验：" + "，".join(x for x in why if x)
    if not psnap.get("no_work_since"):
        return "项目看板还没说暂时没实验，等下一轮采集"
    from . import ephemeral as EP
    if ephs := EP.online_for_project(pid, t):
        return f"还有在线临时主机（{'、'.join(r['host_id'] for r in ephs)}），请等待其退出或离线后再归档"
    return None


def _regs_of(pid: str, glob: dict) -> tuple[list[dict], list[str]]:
    """全局快照 regs 里这个项目的登记 [{host, home, root, jobs}]，以及登记从没读到过的主机（queues 为空值，说不出有没有它的）。"""
    out, unread = [], []
    for x in glob.get("regs") or []:
        if x.get("queues") is None:
            unread.append(x["host"])
            continue
        out += [{"host": x["host"], "home": x.get("home"), "root": q["root"], "jobs": q["jobs"]} for q in x["queues"] if q.get("project") == pid]
    return out, unread


def host_ovr(pid: str, glob: dict) -> list[tuple[str, str, str | None]]:
    """只在某几台单独下的（--host，全局快照里 worker 状态 overrides 中不带 global、once 的）暂停、优先：[(主机, 种类, 谁下的)]，没记是谁的为 None（#27）。
    全局的在主机清单 [sched] 里，另撤。归档撤哪些、users.pause_ok 判按主机的暂停都按它。"""
    return [(e["id"], o["kind"], o.get("by")) for e in glob.get("hosts") or [] for o in (e.get("status") or {}).get("overrides") or []
            if o.get("kind") in ("pause", "focus") and o.get("target") == pid and not o.get("global") and not o.get("once")]


def _last(out: str) -> str:
    return (out.strip().splitlines() or [""])[-1][:160]


def archive(pid: str, psnap: dict | None, glob: dict | None, interval: float = 60, by: str = "用命令行", check_owner=None, own: bool = False,
            who=None) -> str:
    """归档一个项目（见模块说明）。移除登记、撤按主机的指令有一处失败就停在那里：不撤调度、不记归档，报回已移除的与失败的（worker rm 重做无害，再来一次接着做）；
    不停的话调度撤了、看板摘了，那台的登记却还在，那台 worker 往后照样放它的清单。没有能代办的那几台（已归还、又没有共用 worker_home 的在用主机）跳过，写进结果。
    by：谁做的（「看板页面上」「用命令行」），记进条目，labmon wait 报“已归档”时写上（archived_msg）。返回说明；不能归档或没做完报 ConfigError。
    who：看板上谁做的（users.Ident；None 是本机命令行）。普通用户（#16、#27）项目级与只在某几台下的暂停只撤 users.pause_ok 说他能动的，
    管理员定的权重、优先（项目级与按主机的）与别人下的暂停留着（恢复后照旧生效）；按主机撤时另带 expect_by，快照晚了、那台已换人下的 worker 不撤。管理员全撤。"""
    glob = glob or {}
    pause_ok = None
    if who is not None and who.role != "admin":
        from . import users                             # users 在模块级 import 本模块
        pause_ok = lambda h: users.pause_ok(who, pid, h, glob)  # noqa: E731
    with HOSTS_LOCK, _reg_locked(), _locked():
        if check_owner is not None:
            check_owner()
        why = archive_block(pid, psnap, glob, interval)
        if why:
            raise ConfigError(f"{pid} 现在不能归档：{why}")
        regs, unread = _regs_of(pid, glob)
        mine = None if pause_ok is None else who.name
        hovr = host_ovr(pid, glob)
        ovr = [(h, k) for h, k, _ in hovr if pause_ok is None or (k == "pause" and pause_ok(h))]
        left = [f"{h} 上的{'暂停' if k == 'pause' else '优先'}" for h, k, _ in hovr if (h, k) not in ovr]
        if (regs or ovr) and not L.check(force=True)["ok"]:
            raise ConfigError("本机不在内网：移除登记要连服务器，回到内网再归档")
        done, bad, skip = [], [], []
        for r in regs:
            name = f"{r['host']} {r['root'].rstrip('/')}/{r['jobs']}"
            try:
                rc, out = W.run_out("rm", r["host"], [r["root"], r["jobs"]], home=r.get("home"),
                                    **({"expect_project": pid} if own else {}))
            except ConfigError as e:
                skip.append(f"{name}（{e}）")
                continue
            if rc == 0:
                done.append({k: r[k] for k in ("host", "root", "jobs")})
            else:
                bad.append(f"{name}：{_last(out) or '退出码 %d' % rc}")
        for hid, kind in ovr:
            try:
                rs = W.override_each("rm", kind, pid, [hid], expect_by=mine)
            except ConfigError as e:
                bad.append(f"{hid} 撤{'暂停' if kind == 'pause' else '优先'}：{e}")
                continue
            bad += [f"{hid} 撤{'暂停' if kind == 'pause' else '优先'}：{_last(x['out']) or '退出码 %d' % x['rc']}" for x in rs if x["rc"] != 0]
        gone = "、".join(f"{d['host']} {d['root'].rstrip('/')}/{d['jobs']}" for d in done)
        if bad:
            msg = f"归档 {pid} 没做完：" + "；".join(bad) + (f"。已移除 {len(done)} 条登记（{gone}）" if done else "") + "。调度与看板都没动，连上后再归档一次接着做"
            log_events([{"kind": "project", "msg": msg[:400]}])
            raise ConfigError(msg)
        if pause_ok is None:                                # 各自只在有那一行时改、记一条（暂停、优先记成 override 的撤销，项目页时间线的区间照样闭合）
            set_weight(pid, None)
            for kind in ("pause", "focus"):
                set_sched_ovr(kind, pid, False)
        elif pause_ok(None):
            set_sched_ovr("pause", pid, False)
        sw = sched() if pause_ok is not None else {}
        kept = [n for k, n in (("weight", "权重"), ("focus", "优先"), ("pause", "暂停")) if pid in (sw.get(k) or {})] + left
        title = (psnap or {}).get("title") or pid
        a = archived()
        a[pid] = {"id": pid, "title": title, "t": round(time.time()), "revision": uuid.uuid4().hex, "by": by, "regs": done, "skipped": skip + [f"{h}（登记没读到）" for h in unread]}
        _save(a)
        msg = (f"归档 {pid}（{title}）：" + (f"移除了 {len(done)} 条登记" if done else "没有要移除的登记")
               + ("，撤掉了它的权重、暂停与优先" if pause_ok is None else f"，管理员定的{'、'.join(kept)}留着（恢复后照旧生效）" if kept else "") + "，看板不再采集它；"
               "登记目录（配置与输出）、共享盘、用量记录都留着，labmon project restore " + pid + " 可加回")
        log_events([{"kind": "project", "msg": msg, "detail": "；".join(([f"移除 {gone}"] if gone else []) + [f"没动 {x}" for x in a[pid]["skipped"]])[:400]}])
        return msg + ("。没动：" + "；".join(a[pid]["skipped"]) if a[pid]["skipped"] else "")


def restore(pid: str, check_owner=None) -> str:
    """恢复一个归档的项目：从 projects.json 去掉，看板服务下一次对齐时（页面上点的当场）加回采集。登记不自动恢复。
    看板上的配置没了或读不了时拒绝（ConfigError），条目留着。"""
    with _reg_locked(), _locked():
        if check_owner is not None:
            check_owner()
        a = archived()
        e = a.get(pid)
        if not e:
            raise ConfigError(f"没有归档的项目 {pid}")
        if not cfg_file(pid).is_file():
            raise ConfigError(f"看板上没有 {pid} 的配置（{cfg_file(pid)}）：在项目目录里 labmon project push 一次再恢复")
        try:
            load_cfg(pid)
        except ConfigError as x:
            raise ConfigError(f"{pid} 的配置读不了，没恢复：{x}") from x
        del a[pid]
        _save(a)
        msg = f"恢复 {pid}（{e.get('title') or pid}，{fmt_time(e['t'])} 归档的）：看板接着采集它；登记没恢复，要跑先 labmon worker add … --project {pid}"
        log_events([{"kind": "project", "msg": msg}])
        return msg


def delete(pid: str, revision: str, by: str, check_collector) -> str:
    """永久删除看板上的归档项目。持登记与归档锁核对状态；清理失败时保留归档条目供重试。"""
    _valid(pid)
    from . import overview
    with HOSTS_LOCK, _reg_locked(), _locked():
        a = archived()
        entry = a.get(pid)
        if not entry:
            raise ConfigError(f"没有归档的项目 {pid}")
        if revision != str(entry.get("revision") or entry["t"]):
            raise ConfigError("项目的归档记录已变化，请刷新后重新确认")
        check_collector()
        schedule = sched()
        if any(pid in schedule.get(kind, {}) for kind in ("weight", "pause", "focus")):
            raise ConfigError("项目仍有调度权重、暂停或优先设置，请先撤销")
        project = cfg_file(pid).parent
        # 核对目录项的原名，避免大小写不敏感的盘把另一个项目当作目标。
        if any(p.is_symlink() for p in (root(), project)) or (
            project.exists() and (not S_ISDIR(project.lstat().st_mode) or pid not in {p.name for p in root().iterdir()})
        ):
            raise ConfigError("项目登记目录不是看板状态中的实际目录，请先核对路径")
        config = cfg_file(pid)
        if config.is_symlink() or out_dir(pid).is_symlink():
            raise ConfigError("项目配置或输出目录经符号链接，请先核对路径")
        cfg = load_cfg(pid) if config.is_file() else {}
        local = load_json(out_dir(pid) / "snapshot.json", {})
        overview.check_project_idle(pid, entry, cfg, local, snapshot())

        try:
            if project.exists():
                shutil.rmtree(project)
            del a[pid]
            _save(a)
        except OSError as e:
            raise ConfigError("看板清理未完成，归档条目仍保留，请稍后重试") from e
        from . import ephemeral as EP
        EP.remove_project(pid)
        msg = f"永久删除了看板项目 {pid}"
        log_events([{"kind": "project", "project": pid, "who": by, "msg": msg}])
        return msg
