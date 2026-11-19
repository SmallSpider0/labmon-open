"""借用组内服务器：组里别人的服务器空着时，说好后借来给自己的项目跑；到约定时间不再放新任务，跑完自动还回去。

两种借法：
- 直接借（本模块的 begin）：平台作业还是对方的，不动平台；看板在上面拉起 worker（环境与数据在共享盘上，不用部署）。
  [[fleet.servers]] 里写 borrow_since（借来的时刻）、borrow_until（约定还的时刻，可不写）、borrow_for（只给哪些项目用，不写为全部）；
  还了写 borrow_returned。借用中这台同时是调度主机（config.guest_host），id 不变；保活守护不碰它（fleet.guard_state）。
- 换给自己（组内页「换人」换给自己，平台作业换成自己的）：要约定还的时间时 labmon borrow <主机> --until … 写进 [[hosts]] 的
  borrow_until、borrow_from。准入与直接借一样；到时只提醒，换回去还是在组内页「换人」。

约定时间（cutoff）：borrow_until 与对方租期到期前 MARGIN_S 取早的。worker 的 deadline 指令（目标 borrow）按它准入：之前跑不完的不放；
到点不停正在跑的（估时偏一点是常事，停了这一格就白跑了），跑完自动归还；超过 OVER_S 还没跑完提醒一次，可在页面点「现在停」。
「跑完就还」（request_return）在还掉之前可以撤销（cancel_return）；「现在停」不能撤。
对方在用：借来时对方空着、或轻用（fleet.analyze 的 share：除保活外各项用量都低于阈值；有 VS Code、终端或小实验）都能借，轻用的要在页面上另勾
「对方在轻用」（缺省勾上），记 borrow_light。给对方留的量（reserve_for）按对方近 7 天轻用时的用量推荐，随 guest 指令下发（keep）：
借来时对方在轻用的（hold）worker 准入时扣掉它；借来时对方空着的不扣（这段时间没人一起跑），对方回来用时有了这人 GPU 进程的卡算超出。
对方超出预留的部分不放新任务；正在跑的不停（这一格跑完，停了白跑），快撞上限时提醒用户线下和对方商量（要停就点「现在停」）；
真撞上容器内存上限时内核先杀我们的任务，被挤掉的重新排队（worker 的规则）。
借用中对方被挤到一次（我们的任务被 OOM 挤掉、或内核杀了对方的进程），这次借用的预留乘 BUMP_X。
对方说好不用（页面上勾「某某说好约定时间前不用」，Issue #30）：主机清单记 borrow_away（取约定时间），随 guest 指令下发 keep.away；到那时以前 worker
不预先留、不判对方的内存与 CPU 超出，对方有了新的 GPU 进程（连着两轮）或内核杀了 labmon 之外的进程（我们没有任务在跑时）就作废（worker 状态的 guest.away.end），check 清掉 borrow_away、提醒一次。独占任务等对方用完才放行（不留卡、不堵别的任务），被打扰就作废重排（所有主机一样）。
归还（finish）：一条 ssh 停 worker、清容器里 labmon 的本地目录与这台的临时指令；卡上的保活是借用期间 worker 开的（worker 状态的 keepalive.mine，
或 ka.sh 的事件日志），就不动进程、登记成看板的保活（fleet.ka_adopt，#102），照常盯着让路；认不出的：借来时 GPU 上没有保活的（ka 为假）
照旧 ka.sh stop 再按保活守护开，借来时就有对方保活的不碰它、按保活守护开（fleet.ka_start：已在跑不重开）。
随即探一次这一台（页面不必等下一轮探测才看到保活），通知“可以告诉对方了”。

实验会话怎么知道：check 给全局快照写 wake（借到、到了约定时间、对方在用、已归还），labmon wait 把与本项目有关的当告警报出。
提醒记在本机状态目录 overview/borrow.json：{服务器: {"warned": [...], "ka", "returning", "now", "kept", "back", "bump", "hit", "seen", "last"}}
（ka：借来时 GPU 上有没有保活；kept：撤销要还的时刻；back：对方开始超出预留的时刻；bump：这次借用预留的倍数；hit、seen：已算过的被挤到）。
"""
from __future__ import annotations

import fcntl
import shlex
import time

from . import fleet as F
from . import fleetops as FO
from . import lease as LS
from .config import GIB, HOSTS_LOCK, ConfigError, _read_toml, check_future, fmt_time, guest_host, host_defaults, hosts_file, parse_time, set_fleet_keys, user_hosts
from .records import log_events
from .renew import eta_of
from .service import log
from .state import load_json, load_rows, save_json, state_dir
from .users import person_of
from .workerctl import worker_env, host_conf, override_each, run_out

MARGIN_S = 3600                  # 对方租期到期前这么久截止：续租会重启容器，没跑完的白跑
DUE_WARN_S = 2 * 3600            # 约定时间前这么久提醒一次
OVER_S = 3600                    # 过了约定时间这么久还没跑完，提醒一次
DONE_KEEP_S = 86400              # 还了以后这么久内，wait 还报“已归还”（登记在上面没跑完的队列要换主机）
IDLE_S = 1800                    # 借来的主机上没有 labmon 任务在跑、也没有在等的连着这么久，给 labmon wait 一条 borrow_idle（只给会话，#43）
RESERVE_HALF_S = 86400           # 给对方留的量：样本按时间衰减加权的半衰期
RESERVE_Q, RESERVE_X = 0.95, 1.15   # 取加权 95 分位再乘这么多（同 Kubernetes VPA recommender 的做法）
RESERVE_MIN = {"vram": 2.0, "ram": 4.0, "cpu": 1.0}   # 下限：显存 GiB、内存 GiB、CPU 核（VS Code、小实验起步就要这些）
BUMP_X, BUMP_MAX = 1.2, 3.0      # 借用中对方被挤到一次，这次借用的预留乘 BUMP_X，最多 BUMP_MAX 倍


def _update(fn):
    """改 borrow.json（看板线程与命令行共用，加文件锁）。"""
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    with open(sd / "borrow.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load_json(sd / "borrow.json", {})
        out = fn(st)
        save_json(sd / "borrow.json", st)
        return out


def _ovr(action: str, kind: str, hid: str, target: str | None = None, until: float | None = None, now: bool = False,
         at: float | None = None, keep: dict | None = None) -> int:
    """给这台的 worker 加或撤临时指令，返回退出码；worker 的回话（生效中：…）或 ssh 连不上的原因记进服务日志。"""
    r = override_each(action, kind, target, [hid], until, now, at, keep)[0]
    log(f"借用 {hid}：指令 {action} {kind}{' ' + target if target else ''} → {r['out'].strip() or ('完成' if r['rc'] == 0 else '退出码 %d' % r['rc'])}")
    return r["rc"]


def _start(hid: str) -> tuple[int, str]:
    """拉起这台的 worker：返回 (退出码, 输出)，输出进借用事件的 detail。"""
    return run_out("start", hid, [])


def _server(sid: str) -> tuple[dict, dict]:
    cfg = F.conf()
    s = next((x for x in (cfg or {}).get("servers", []) if x["id"] == sid), None)
    if not s:
        raise ConfigError(f"组内服务器里没有 {sid}")
    return cfg, s


def label(s: dict) -> str:
    return f"{s['holder']}（{s['id']}）"


def cutoff(b: dict, lease: float | None) -> tuple[float | None, str | None]:
    """约定时间与依据：borrow_until 与对方租期到期前 MARGIN_S 取早的。b：主机的 borrow 字段（until 是 epoch 秒）。"""
    until, lim = b.get("until"), lease - MARGIN_S if lease else None
    if until and (not lim or until <= lim):
        return until, "约定"
    if lim:
        return lim, f"{b.get('holder')} 的租期 {fmt_time(lease)} 到期，提前 {MARGIN_S // 3600} 小时"
    return None, None


def _projects(v) -> list[str] | None:
    if v is None or v == "":
        return None
    xs = [x.strip() for x in (v.split(",") if isinstance(v, str) else v) if str(x).strip()]
    return xs or None


BEGIN_STEPS = ["探一次：对方空着或轻用", "查共享盘、Python 与保活", "写进主机清单、下指令", "拉起 worker、等它报到"]


def begin(sid: str, until: float | None = None, projects=None, fetch=None, ssh=None, start=None, ovr=_ovr, step=None, light: bool = False,
          away: bool = False, *, account: str | None = None, pool: bool = False) -> str:
    """借来用（已在借的就改约定时间与给哪些项目用）。先探一次：对方要么空着（GPU 上没有进程或只有 ka.sh 管的保活，worker 要卡时会停它；
    认作保活的 GPU 进程逐个核对在不在 ka.sh running 列出的进程组里，相对路径起的同一脚本 ka.sh 也认，#29），
    要么轻用（using：有 VS Code、终端或小实验，且 share 判为轻用）——轻用的要 light（页面上勾「对方在轻用」）；
    共享盘与 Python 在；再写进主机清单、下 guest（带给对方留的量）与截止指令、拉起 worker。返回说明；不能借就抛 ConfigError。
    step(note)：做完 BEGIN_STEPS 的一步就调一次（最后一步不调），看板上的借来用（fleetops.run_borrow）拿它显示进度。
    away：对方说好约定时间前不用（不给对方留余量），要有约定时间；改约定时按它重写（不勾就去掉）。
    pool：算力池自动借（pool.want_borrow 经 fleetops 调，#25）：持有人开了入池就是同意，不记借的人（borrow_by），记 borrow_pool；
    只许 GPU 上除保活外没有进程（VS Code、终端这类轻用照样给主人留预留）；until 是入池时段结束（全天的为 None）。
    borrow.json 这台的 pool 子项记下容器的启动时刻（started：换了容器就还，pool._returns），再借时留着休息期、退避这些记忆。"""
    LS.need("借用")                                  # 备用的看板服务不借（#163）：主那台在管这台的指令与归还
    via_op, step = step is not None, step or (lambda note=None: None)
    check_future(until, "约定时间")
    if away and not until:
        raise ConfigError("说好不用要有个时限：先选约定时间")
    cfg, s = _server(sid)
    projects = _projects(projects)
    keys = {"borrow_until": fmt_time(until, year=True) if until else None, "borrow_for": projects,
            "borrow_away": fmt_time(until, year=True) if away else None}
    what = (f"到 {fmt_time(until)}" if until else "没约定时间") + (f"，只给 {'、'.join(projects)} 用" if projects else "") \
        + (f"，{s['holder']} 说好这段时间不用" if away else "")
    if F.borrowed(s) and (pool or s.get("borrow_pool")):     # 手动借与池借互斥：池借着的由持有人或管理员「收回」，不改约定
        raise ConfigError(f"{label(s)} 正借给算力池：要用就往池里交清单（labmon worker add pool），要还给 {s['holder']} 就点「收回」")
    if F.borrowed(s):
        set_fleet_keys(keys, f"改借用 {label(s)} 的约定：{what}", sid=sid)
        _sync(sid, ovr)
        msg = f"借用 {label(s)} 的约定改为{what}"
        log_events([{"kind": "borrow", "host": sid, "who": s["holder"], "msg": msg}])
        return msg
    person, error = (None, None) if pool else person_of(account)
    if error or (not pool and not person):
        raise ConfigError(error or f"账号 {account} 尚未关联组内人员")
    if sid in F.off_servers():
        raise ConfigError(f"{label(s)} 已关机")
    if sid in FO.busy() and not via_op:            # 看板上的借来用本身就是这台的操作（fleetops.start 已排过别的操作）
        raise ConfigError(f"{label(s)} 正在做平台操作")
    raw, err = (fetch or F.fetch)(s)
    if raw is None:
        raise ConfigError(f"连不上 {label(s)}：{err}")
    a = F.analyze(F.parse(raw), cfg)
    use = using(a)
    if not can_borrow(a):
        raise ConfigError(f"{label(s)} 上 {s['holder']} 在用（{use}），不是轻用，不能借")
    if pool and F._brief_work(a):                   # 算力池只在 GPU 空着或只有保活时借（AGENTS.md「只守真会出事的规则」）
        raise ConfigError(f"{label(s)} 的 GPU 上有 {s['holder']} 的进程（{F._brief_work(a)}）：算力池只在 GPU 空着或只有保活时借")
    if use and not light and not pool:
        raise ConfigError(f"{label(s)} 上 {s['holder']} 在轻用（{use}）：确认和这人说好轻用时也借，再勾「对方在轻用」")
    kp = {**reserve_for(sid), "hold": bool(use), **({"away": round(until)} if away else {})}
    step(f"{s['holder']} 在轻用（{use}）" if use else "只有保活" if a["work"] else "GPU 空着")
    h = host_defaults(guest_host(s, _read_toml(hosts_file()).get("hosts", [])))
    ka = F.ka_cmd(s, cfg)
    kw = {p: w for w in a["work"] if w["kind"] == "keepalive" for p in w.get("gpids") or []}   # 认作保活的 GPU 进程 → 它那组负载
    remote = f"test -d {shlex.quote(h['data_root'])} || echo nodata; test -x {shlex.quote(h['python'])} || echo nopy"
    if ka and kw:      # 同一条 ssh 里逐个核对保活是不是 ka.sh 管的（fleet_guard.sh 的 kacheck，只读）：一个不是就借不了，worker 停不了它
        rc, out, err = (ssh or F.ssh)(s, remote + "; bash -s -- " + shlex.join(["kacheck", ka, *map(str, sorted(kw))]), input=F.GUARD.read_text())
    else:
        rc, out, err = (ssh or F.ssh)(s, remote)
    if rc == 255:
        raise ConfigError(f"连不上 {label(s)}：{err.strip()}")
    lines = out.split()
    miss = ([f"共享盘上没有 {h['data_root']}"] if "nodata" in lines else []) + ([f"没有 {h['python']}"] if "nopy" in lines else [])
    if ka and kw:
        miss += _ka_miss(s, kw, (out.strip().splitlines() or [""])[-1], err)
    if miss:
        raise ConfigError(f"{label(s)} 借不了：{'；'.join(miss)}")
    step()
    with HOSTS_LOCK:
        _, current = _server(sid)
        if F.borrowed(current) or current["holder"] != s["holder"] or current["job"] != s["job"]:
            raise ConfigError(f"{label(s)} 的登记已变，请刷新后再试")
        if pool and not (current.get("lend") and F.lend_open(current, time.time())[0]):   # 探测期间持有人关了入池、改了时段：按最新的配置不借
            raise ConfigError(f"{label(s)} 刚关了入池或不在入池时段里了：算力池不借")
        set_fleet_keys({"borrow_since": fmt_time(time.time(), year=True), "borrow_returned": None, "borrow_by": person, "borrow_light": True if use else None,
                        "borrow_pool": True if pool else None, **keys},
                       f"{'算力池借来' if pool else '借用'} {label(s)}：{what}", sid=sid)
    ka_on = any(w["kind"] == "keepalive" and not w.get("ours") for w in a["work"])   # 借来时对方的保活在：还的时候不停它（labmon 自己开的不算）

    def fresh(st):                                   # 这一次借用重新记；算力池的记忆（休息期、退避、nofit）跨借用留着
        mem = dict((st.get(sid) or {}).get("pool") or {})
        if pool:
            mem.update(started=a.get("started"), since=round(time.time()), idle=None, busy=None)   # busy 只算这一次借用的
        st[sid] = {"warned": [], "ka": ka_on, **({"pool": mem} if mem else {})}
    _update(fresh)
    notes = []
    if _sync(sid, ovr):
        notes.append("下指令失败")
    step(notes[0] if notes else what)
    rc, out = (start or _start)(sid)
    notes.append("worker 已拉起" if rc == 0 else f"worker 拉起失败（退出码 {rc}）")
    msg = f"{'算力池借来' if pool else '借来'} {label(s)}：{what}；{keep_text(kp)}；{'，'.join(notes)}"
    log_events([{"kind": "borrow", "host": sid, "who": s["holder"], "msg": msg, "detail": out.strip()[-400:], **({"pool": True} if pool else {})}])
    return msg


def _ka_miss(s: dict, kw: dict, res: str, err: str) -> list[str]:
    """kacheck 的结果行 → 借不了的原因（都过了为空）：哪个保活（命令行、PID、工作目录）ka.sh 停不了、对方该怎么做；ka.sh 本身出错另一句。"""
    word, _, rest = res.partition(" ")
    if word == "ok":
        return []
    if word == "noka":
        out = []
        for w in {id(kw[int(p)]): kw[int(p)] for p in rest.split() if p.isdigit() and int(p) in kw}.values():
            pids = "、".join(str(p) for p in w["gpids"] if str(p) in rest.split())
            cmd = (w.get("chain") or [w["label"]])[-1]
            out.append(f"GPU 上的保活 {cmd[:120]}（PID {pids}{'，工作目录 ' + w['cwd'] if w.get('cwd') else ''}）不在 ka.sh 管的进程组里，"
                       f"借来后 worker 停不了它、那几张卡也用不上：请 {s['holder']} 先停掉它，要保活改用 ka.sh start 开")
        return out or [f"GPU 上有 ka.sh 停不了的保活（PID {rest}），请 {s['holder']} 先停掉它"]
    tail = (err.strip().splitlines() or [""])[-1][:200]                # ka.sh 的报错（kacheck 照传到标准错误）
    if word == "fail":
        return [f"核对保活时 {rest}{'：' + tail if tail else ''}，请先查 [fleet] keepalive_cmd 与共享盘上的 ka.sh"]
    return [f"核对保活没拿到结果（{tail or '没有回话'}）"]


def using(a: dict) -> str:
    """对方此刻在用什么（fleet.analyze 的结果）：GPU 上的实验、VS Code、终端；空着为空串。"""
    return "、".join(x for x in [F._brief_work(a), "VS Code" if a.get("vscode") else "", f"{a['terms']} 个终端" if a.get("terms") else ""] if x)


def can_borrow(a: dict) -> bool:
    """借得了：对方空着，或只是轻用（fleet.analyze 的 share：除保活外显存、利用率、CPU、内存都低于阈值）。"""
    return not using(a) or bool(a.get("share"))


def keep_text(k: dict) -> str:
    """给对方留的量的一句话（记录、命令行用；页面上同样的话由 keepText 生成）。hold 为假：借来时对方空着，不预先留；
    away：对方说好不用到几点（epoch 秒），那以前不留。"""
    x = f"（被挤到后 ×{k['bump']:g}）" if k.get("bump", 1) > 1 else ""
    if k.get("away"):
        return (f"对方说好 {fmt_time(k['away'])} 前不用：不给对方留余量，对方此刻占着的照算、空着的都能用；"
                "对方有了新的 GPU 进程、或内核杀了 labmon 之外的进程，就当对方回来了，恢复按预留借卡") + x
    if k.get("hold", True):
        return f"给对方留显存 {k['vram']:g} GiB、内存 {k['ram']:g} GiB、CPU {k['cpu']:g} 核" + x
    return f"借来时对方空着，不预先留；对方回来用时，有这人 GPU 进程的卡、内存超过 {k['ram']:g} GiB、CPU 超过 {k['cpu']:g} 核就不放新任务" + x


def _wq(xs: list[tuple[float, float]], q: float) -> float:
    """加权分位数：xs 为 (值, 权重)。"""
    xs, acc = sorted(xs), 0.0
    tot = sum(w for _, w in xs)
    for v, w in xs:
        acc += w
        if acc >= q * tot:
            return v
    return xs[-1][0] if xs else 0.0


_REC: dict = {}


def _ram_sample(r: dict) -> float | None:
    """组内探测的一行里对方的内存（GiB，不算页缓存、除保活）：带 mk 的行 ma 已按保活整棵进程树的 PSS 扣（同 worker 的 other.ram）。
    兼容旧行（两处都只读本机 series，补不出来，到期删）：
    - 不带 mk 的（#29 修复部署前）ma 扣的是保活进程树的 RSS 合计，父子共享的页重复相加、扣多了（27-15152 上多扣约 2 GiB），
      改取不扣保活的总 anon（ma + 当时扣的 m − mr）：把保活也算对方的，偏向多留，11-17 后七天内全是带 mk 的，可删；
    - 不带 ma 的（11-06 前）只有含活跃页缓存的 mr（偏大），11-13 后七天内全是 ma，可删。"""
    if "mk" in r:
        return r.get("ma")
    if r.get("ma") is not None:
        return r["ma"] + max(0.0, (r.get("m") or 0.0) - (r.get("mr") or 0.0))
    return r.get("mr")


def reserve_for(sid: str, t: float | None = None, bump: float = 1.0) -> dict:
    """给对方留的量：组内探测近 7 天里对方空着或轻用（sh）时的样本、对方的实际用量（除保活；内存不算页缓存，保活按整棵进程树的 PSS 扣，
    与 worker 比的 other.ram 同口径，见 _ram_sample），
    按时间衰减加权（半衰期 RESERVE_HALF_S）
    取 RESERVE_Q 分位 ×RESERVE_X，不低于 RESERVE_MIN，再乘 bump。只取轻用时的：借卡只在对方空着或轻用时借，留的是轻用的余量；
    对方回来跑大实验就是超出预留，由 worker 不放新任务。借用中的样本不算（带 bw 的）。
    显存是整台合计，worker 按“对方的卡”留（现在都是单卡）。返回 {vram, ram, cpu, peak: 这些样本里的最大值, n: 样本数, bump}。按小时缓存。"""
    t = time.time() if t is None else t
    key = (sid, int(t // 3600))
    if key not in _REC:
        rows = [r for r in load_rows(F.state_dir() / "series" / f"{sid}.jsonl")
                if r.get("t", 0) >= t - 7 * 86400 and (r.get("sh") or r.get("st") in ("idle", "keepalive")) and not r.get("bw")]
        rec, peak = {}, {}
        for k, f in (("vram", lambda r: r["vr"] / 1024 if r.get("vr") is not None else None), ("ram", _ram_sample), ("cpu", lambda r: r.get("cr"))):
            xs = [(f(r), 0.5 ** ((t - r["t"]) / RESERVE_HALF_S)) for r in rows if f(r) is not None]
            rec[k] = max(RESERVE_MIN[k], _wq(xs, RESERVE_Q) * RESERVE_X)
            peak[k] = round(max((v for v, _ in xs), default=0.0), 1)
        for k in [k for k in _REC if k[0] == sid]:
            del _REC[k]
        _REC[key] = {**rec, "peak": peak, "n": len(rows)}
    r = _REC[key]
    return {**{k: round(r[k] * bump, 1) for k in RESERVE_MIN}, "peak": r["peak"], "n": r["n"], "bump": bump}


def _keep(hid: str, t: float | None = None) -> dict:
    """借来的这台现在给对方留的量（含这次借用被挤到后的倍数）；hold：借来时对方在轻用，要预先留出（空着时借的不留）；
    away：对方说好不用到几点（主机清单的 borrow_away，还没到才带），那以前 worker 不留。
    pool：算力池借来的（#25）：worker 见到主人回来（guest.owner）整台不放新任务。"""
    h = next((x for x in user_hosts() if str(x.get("id")) == hid), None) or {}
    b, now = h.get("borrow") or {}, time.time() if t is None else t
    return {**reserve_for(hid, t, (load_json(state_dir() / "borrow.json", {}).get(hid) or {}).get("bump") or 1.0),
            "hold": bool(b.get("light")), **({"away": round(b["away"])} if b.get("away") and now < b["away"] else {}),
            **({"pool": 1} if b.get("pool") else {})}


def _drift(cur: dict | None, want: dict) -> bool:
    """指令里的预留与该有的差得多（任一项差 10% 以上，或指令没带）：要重下。"""
    return (not cur or cur.get("hold", True) != want["hold"] or cur.get("away") != want.get("away") or bool(cur.get("pool")) != bool(want.get("pool"))
            or any(abs((cur.get(k) or 0) - want[k]) > 0.1 * want[k] for k in RESERVE_MIN))


def _want(b: dict, cut: float | None, keep: dict | None = None) -> dict:
    """借来的主机上该有的临时指令：{(kind, target): {at, keep}}。guest 只在直接借的主机上下，带着给对方留的量。"""
    out = {} if b.get("swap") else {("guest", ",".join(b["for"]) if b.get("for") else None):
                                    {"at": None, "keep": {**{k: keep[k] for k in RESERVE_MIN}, "hold": keep["hold"],
                                                          **({"away": keep["away"]} if keep.get("away") else {}),
                                                          **({"pool": 1} if keep.get("pool") else {})} if keep else None}}
    if cut:
        out[("deadline", "borrow")] = {"at": cut, "keep": None}
    return out


def _guest_cut(hid: str, lease: float | None = None) -> tuple[dict | None, float | None]:
    """借来的主机（用户级主机清单里的样子）与它现在的约定时间；不在借返回 (None, None)。lease：对方租期到期，None 就从组内状态取。"""
    h = next((x for x in user_hosts() if str(x.get("id")) == hid), None)
    if not h or not h.get("borrow"):
        return None, None
    if lease is None and not h["borrow"].get("swap"):
        lease = F.lease_end(F._load_state().get(hid) or {}, F.conf() or {})
    return h, cutoff(h["borrow"], lease)[0]


def _sync(hid: str, ovr=_ovr, have: list | None = None, lease: float | None = None) -> int:
    """按主机清单补齐这台的 guest 与截止指令（缺了、时刻对不上或给对方留的量差得多才下，多了的撤掉）。have：worker 状态里的指令，None 就都下一遍。"""
    h, cut = _guest_cut(hid, lease)
    if not h:
        return 0
    want, rc = _want(h["borrow"], cut, None if h["borrow"].get("swap") else _keep(hid)), 0
    cur = {(o.get("kind"), o.get("target")): o for o in have or []}
    for (kind, target), w in want.items():
        o = cur.get((kind, target))
        if have is None or o is None or abs((o.get("at") or 0) - (w["at"] or 0)) > 60 or (w["keep"] and _drift(o.get("keep"), w["keep"])):
            rc |= ovr("add", kind, hid, target, None, False, w["at"], w["keep"])
    for (kind, target) in cur:                       # 改了给哪些项目用、去掉了约定：旧的撤掉
        if (kind == "guest" or (kind, target) == ("deadline", "borrow")) and (kind, target) not in want:
            rc |= ovr("rm", kind, hid, target)
    return rc


def request_return(sid: str, now: bool = False, running: list[dict] | None = None, ovr=_ovr) -> str:
    """要还：不再放新任务（drain），正在跑的跑完就还（check 里见到跑完了就 finish）。now：正在跑的按各自的停法停下（pause --now）。"""
    _, s = _server(sid)
    if not F.borrowed(s):
        raise ConfigError(f"{label(s)} 没在借")
    rc = ovr("add", "drain", sid)
    for p in sorted({j.get("project") for j in running or [] if j.get("project")}) if now else []:
        rc |= ovr("add", "pause", sid, p, None, True)
    if rc:
        raise ConfigError(f"给 worker 下指令失败（退出码 {rc}）")
    _update(lambda st: st.setdefault(sid, {"warned": []}).update(returning=round(time.time()), now=now))
    msg = f"要还 {label(s)}：不再放新任务，" + ("正在跑的按各自的停法停下" if now else "正在跑的跑完") + "后自动归还"
    log_events([{"kind": "borrow", "act": "return", "now": now, "host": sid, "who": s["holder"], "msg": msg}])   # act、now：项目页时间线按字段认「跑完就还」
    return msg


def cancel_return(sid: str, ovr=_ovr, t: float | None = None) -> str:
    """撤销「跑完就还」：撤掉要还时下的 drain、清掉要还标记，照常放新任务。只在还没还掉、约定时间还没到时行
    （过了约定时间下一轮又会要还，先改约定）；「现在停」的不撤（正在跑的已经停了，撤了也挽回不了）。
    记下撤销的时刻 kept：check 本轮开始时读到的要还比它早，合并时不再写回。"""
    _, s = _server(sid)
    t = t or time.time()
    if not F.borrowed(s):
        raise ConfigError(f"{label(s)} 没在借（已经还了的要再用：labmon borrow {sid}）")
    st = load_json(state_dir() / "borrow.json", {}).get(sid) or {}
    if not st.get("returning"):
        raise ConfigError(f"{label(s)} 没在要还")
    if st.get("now"):
        raise ConfigError("「现在停」的不能撤销：正在跑的已经停了，停完就还")
    cut = _guest_cut(sid)[1]
    if cut and t >= cut:
        raise ConfigError(f"已过约定时间（{fmt_time(cut)}），撤了下一轮又会要还：先改约定到更晚，再撤销")
    if ovr("rm", "drain", sid):
        raise ConfigError("撤 drain 指令失败")
    _update(lambda st: st.setdefault(sid, {"warned": []}).update(returning=None, now=None, kept=round(t)))
    msg = f"撤销要还 {label(s)}：照常放新任务" + (f"，{fmt_time(cut)} 前跑得完的才放行" if cut else "")
    log_events([{"kind": "borrow", "host": sid, "who": s["holder"], "msg": msg}])
    return msg


def wrote_note(w: dict | None, holder: str) -> str | None:
    """借用期间本地盘 labmon 目录之外新写的（worker 的 storage.wrote，借来的主机上只数借来以后的，#149）一句话：归还不删它们（只删 /tmp/labmon），
    留在对方机器上、我们拿不到，对方续租时清掉。归还的结果（finish）用它；不到 1 MiB 的不说。"""
    if not w or (w.get("b") or 0) < 2 ** 20:
        return None
    top = "、".join(x[0] for x in (w.get("top") or [])[:3])
    return (f"借用期间本地盘 labmon 目录之外新写了 {w['b'] / GIB:.1f} GiB（多半是我们的任务写的" + (f"：{top}" if top else "")
            + f"），没动：留在 {holder} 那边，我们拿不到，对方续租时会清掉")


def finish(sid: str, ssh=None, ka=None, wrote: dict | None = None, kill=(), probe=None, gone: bool = False, stuck: dict | None = None,
           mine: dict | None = None, adopt=None) -> str:
    """归还：一条 ssh 停 worker、停掉还挂着的占位任务（kill：它们的进程组）、清掉容器里 labmon 的本地目录与这台的临时指令。主机清单写 borrow_returned。
    卡上的保活是借用期间 worker 开的（mine：worker 状态的 keepalive.mine；没有就按 ka.sh 的事件日志认），不动进程、登记成看板的保活
    （adopt 缺省 fleet.ka_adopt，在现场核对身份，#102）：不停再开，没有空档；认不出的照旧——借来时 GPU 上没有保活的停掉再按保活守护开，
    借来时就有对方保活的不碰、按保活守护开（已在跑不重开；开了就盯着，对方的实验上来就让出）。然后恢复借来时暂停的保活看门狗（worker 的 thaw：
    保活已在，它醒来不会再多拉一个：出借方自己的保活管理器见到不是自己登记的同脚本保活会换成自己的，结果仍是一个），最后探一次这一台（同手动开保活，fleetops._probe）。
    wrote：借用期间本地盘 labmon 目录之外新写的（worker 的 storage.wrote：只列出来，不动，wrote_note）。
    stuck：借用期间 worker 要卡时停不了卡上的保活（worker 状态的 keepalive.stuck，#102）：结果里照实写一句，不只说保活是谁的。
    gone：这台换了容器（算力池借着时主人续租或重启了，pool._returns 的 R5，#25）：旧容器里的 worker、任务、保活、/tmp/labmon 都已不在，
    只 worker stop（按命令行核对 PID，新容器里不会误停别的）、删这台的临时指令；不 kill 旧的进程组号、不停保活、不清 /tmp/labmon、
    不开保活、不恢复看门狗（新容器里的保活由主人与保活守护照现有规则管），只探一次。"""
    cfg, s = _server(sid)
    h = host_conf(sid)
    w = f"env {worker_env(h)} {shlex.quote(h['python'])} {shlex.quote(h['worker_home'] + '/labmon_worker.py')}"
    ka_cmd = F.ka_cmd(s, cfg)
    theirs = bool((load_json(state_dir() / "borrow.json", {}).get(sid) or {}).get("ka"))
    ovr_file = f"rm -f {shlex.quote(h['worker_home'] + '/hosts/' + sid + '/overrides.json')}"
    remote = (f"{w} stop --host {shlex.quote(sid)} >/dev/null 2>&1; {ovr_file}" if gone else
              f"{w} stop --host {shlex.quote(sid)} >/dev/null 2>&1; sleep 2; "
              + "".join(f"kill -TERM -- -{int(p)} 2>/dev/null; " for p in kill)
              + f"du -sm /tmp/labmon 2>/dev/null | cut -f1; rm -rf /tmp/labmon; {ovr_file}")
    rc, out, err = (ssh or F.ssh)(s, remote)
    if rc == 255:
        raise ConfigError(f"连不上 {label(s)}，还没还：{err.strip()}")
    mb = 0 if gone else next((int(x) for x in out.split() if x.isdigit()), 0)
    set_fleet_keys({"borrow_returned": fmt_time(time.time(), year=True)}, f"把 {label(s)} 还给 {s['holder']}", sid=sid)
    since = parse_time(s.get("borrow_since"))
    took = f"（借了 {(time.time() - since) / 3600:.0f} 小时）" if since else ""
    if gone:
        (probe or FO._probe)(sid)
        msg = f"已把 {sid} 还给 {s['holder']}{took}：容器换了（续租或重启），上面的 worker 与任务已随旧容器停下，新容器没动"
        _update(lambda st: st.setdefault(sid, {}).update(returning=None, done=round(time.time())))
        log_events([{"kind": "borrow_return", "host": sid, "who": s["holder"], "msg": msg, **({"pool": True} if s.get("borrow_pool") else {})}])
        return msg
    # 卡上的保活是借用期间 worker 开的：登记成看板的保活，不停再开（#102）；认不出就照旧
    ad, adn = (adopt or F.ka_adopt)(s, cfg, since, time.time(), mine) if ka_cmd and since else ("no", "")
    if ad == "ok":
        res, note = "ok", adn
    else:
        if ka_cmd and not theirs:                    # 借来时 GPU 上没有保活：卡上的多半是 worker 开的，认不出就停掉、按守护重开（原来的做法）
            (ssh or F.ssh)(s, f"({ka_cmd} stop) >/dev/null 2>&1")
        res, note = (ka or F.ka_start)(s, cfg, "借用归还")
    rc, out, err = (ssh or F.ssh)(s, f"{w} thaw --host {shlex.quote(sid)}")
    wd = (next((x for x in out.splitlines() if x.startswith("看门狗：")), None) if rc == 0
          else f"借来时暂停的看门狗没恢复成：{(err or out).strip()[-160:]}")
    (probe or FO._probe)(sid)
    # 保活是谁的（借来时就在、不是 labmon 开的：borrow.json 的 ka）与 worker 停不停得了（worker 状态的 keepalive.stuck）是两回事，各说各的（#102）
    notes = [f"清掉了容器里 labmon 的本地目录（{mb} MB）" if mb else None,
             ("保活是借用期间 worker 开的，已登记成看板的保活（盯着，对方的实验上来就让出）"
              + (f"；借来时 {s['holder']} 的那个借用期间停过" if theirs else "") if ad == "ok" and adn.startswith("adopt") else
              "保活是看板登记的那个，接着盯着让路" if ad == "ok" else
              "保活还在" + (f"（借来时就在、是 {s['holder']} 的，归还不停它）" if theirs else "") if note.startswith("already") else "开了保活（盯着，对方的实验上来就让出）")
             if res == "ok" else "GPU 上已有别的进程，没开保活" if res == "busy" else f"保活没开成：{note}",
             f"卡上的保活是借用期间起来的，{adn}，没登记成看板的保活" if ad == "unsure" and theirs else None,
             f"借用期间 worker 要卡时停不了它（{stuck['why']}，{fmt_time(stuck['t0'])} 起），任务和它同卡分算力" if stuck else None,
             wd, wrote_note(wrote, s["holder"])]
    msg = (f"已把 {sid} 还给 {s['holder']}{took}：" + "；".join(x for x in notes if x)
           + ("。" if s.get("borrow_pool") else f"。可以告诉 {s['holder']} 了"))      # 算力池借的是持有人事先同意的，不用另行告诉
    _update(lambda st: st.setdefault(sid, {}).update(returning=None, done=round(time.time())))
    log_events([{"kind": "borrow_return", "host": sid, "who": s["holder"], "msg": msg, **({"pool": True} if s.get("borrow_pool") else {})}])
    return msg


def set_swap(hid: str, until: float | None, holder: str | None, ovr=_ovr) -> str:
    """换给自己借来的自用主机：记下约定还的时间与还给谁（until 为 None 就去掉约定）。"""
    check_future(until, "约定时间")
    keys = {"borrow_until": fmt_time(until, year=True) if until else None, "borrow_from": holder if until else None}
    msg = f"{hid} 约定 {fmt_time(until)} 换回给 {holder or '原使用人'}" if until else f"去掉 {hid} 的约定归还时间"
    set_fleet_keys(keys, msg, host=hid)
    log_events([{"kind": "borrow", "host": hid, "who": holder, "msg": msg}])
    if not until:                                    # 不约定了：check 不再管这台，截止指令在这里撤
        ovr("rm", "deadline", hid, "borrow")
    return msg


def _jobs(run: list[dict]) -> str:
    return "、".join(j.get("name") + (f"（预计 {fmt_time(eta_of(j), date=False)} 跑完）" if eta_of(j) else "") for j in run[:4]) \
        + (f" 等 {len(run)} 格" if len(run) > 4 else "")


def late_jobs(ws: dict) -> list[dict]:
    """借用约定挡住、这台不会再放的清单（看板的 borrow_cut 告警、borrow_due 提醒里跑不完的任务都取它）：worker 状态里队首被截止挡住
    （等待项 held 为 deadline）的队列，前提是约定归还（目标 borrow 的 deadline 指令）是最早的截止，不然挡住它的可能是续租截止。
    ws：worker 状态。返回 [{project, root, queue, name 队首, why, n 这条清单还没跑的任务数（todo 里这条的，找不到算 1）}]。只读，不改放行规则。"""
    ds = [o for o in ws.get("overrides") or [] if o.get("kind") == "deadline" and o.get("at")]
    cut = min((o["at"] for o in ds if o.get("target") == "borrow"), default=None)
    if cut is None or any(o["at"] < cut for o in ds if o.get("target") != "borrow"):
        return []
    todo = {(ln.get("project"), ln["jobs"][0]): len(ln["jobs"]) for ln in ws.get("todo") or [] if ln.get("jobs")}
    return [{"project": w.get("project"), "root": w.get("root"), "queue": w["queue"], "name": w["name"], "why": w.get("why"),
             "n": todo.get((w.get("project"), w["name"]), 1)} for w in ws.get("waiting") or [] if w.get("held") == "deadline" and w.get("queue")]


def late_do(why: str | None, user: bool = False) -> str:
    """约定截止挡住、这台不会再放的任务该怎么办（看板的 borrow_cut 告警、工作台告警、borrow_due 提醒都用这一句）。
    两种说法（#142）：给实验会话的写操作（labmon wait 的 borrow_cut、wake 的 borrow_due；快照主机的 borrow.late_do），
    user=True 给用户的写人话（页面上的告警、弹给用户的 borrow_due 提醒；快照主机的 borrow.late_say）。
    why：borrow.cutoff 给的依据。截止取的是对方租期时延长约定推不迟它，不提延长。"""
    if user:
        return ("可在主机页「改约定」延长，或由实验会话换到别的在用主机" if why == "约定"
                else f"截止取的是 {why}，延长约定推不迟它，由实验会话换到别的在用主机")
    return "换到别的在用主机（labmon worker add）" + ("，或请用户延长约定" if why == "约定" else f"；截止取的是 {why}，延长约定推不迟它")


def _late_text(late: list[dict], why: str | None, user: bool = False) -> str:
    """borrow_due 提醒附的一句：截止前跑不完、不会在这台放的任务（user：弹给用户的那份，见 late_do）。"""
    if not late:
        return ""
    by = "、".join(f"{x['project']} 的 {x['queue']} {x['n']} 个" for x in late[:3]) + (" 等" if len(late) > 3 else "")
    return f"；排着的 {sum(x['n'] for x in late)} 个任务在约定前跑不完、不会在这台放（{by}）：{late_do(why, user)}"


def _say(st: dict, key: str, kind: str, hid: str, who: str | None, msg: str, evs: list, msgs: list) -> None:
    """一件事只提醒一次（key 记在 warned 里）：进记录、弹通知。"""
    if key in st["warned"]:
        return
    st["warned"].append(key)
    evs.append({"kind": kind, "host": hid, "who": who, "msg": msg})
    msgs.append(msg)


def check(hosts: list[dict], t: float, cfg: dict | None = None, finish_fn=finish, ovr=_ovr) -> tuple[list[str], list[dict]]:
    """每轮全局采集后调用：补齐指令、到点提醒、对方在用时提醒、要还且跑完了就归还；给借来的主机填 e["borrow"]。
    返回 (要弹的通知, wake 条目 [{key, msg, projects}]，projects 为 None 是各项目都报)。
    算力池借的（borrow.pool，#25）：不给实验会话发 borrow*（池清单的去向由 pool 经 records.tell 说），提醒只进记录、不弹通知（还不成的照弹）；
    主人回来、要还与搬走由 pool.tick 管，这里不另提醒“对方在用”。"""
    cfg = cfg if cfg is not None else F.conf() or {}
    sts, fst = load_json(state_dir() / "borrow.json", {}), F._load_state()
    out, wake, evs, by = [], [], [], {e["id"]: e for e in hosts}
    for s in cfg.get("servers", []):                 # 还了的：一天内 wait 还报，登记在上面的队列要换主机（算力池的由 pool 搬回池里）
        rt = parse_time(s.get("borrow_returned"))
        if s.get("borrow_since") and rt and t - rt < DONE_KEEP_S and not s.get("borrow_pool"):
            wake.append({"key": f"borrow_done:{s['id']}", "projects": _projects(s.get("borrow_for")), "host": s["id"],
                         "msg": f"{s['id']} 已于 {fmt_time(rt)} 还给 {s['holder']}：登记在它上面、没跑完的队列要换到别的主机（labmon worker add）"})
    for h in user_hosts():
        b, hid = h.get("borrow"), str(h.get("id"))
        if not b or h.get("returned") or hid not in by:
            continue
        e, swap, pool = by[hid], bool(b.get("swap")), bool(b.get("pool"))
        lease = None if swap else F.lease_end(fst.get(hid) or {}, cfg)
        cut, why = cutoff(b, lease)
        st = sts.setdefault(hid, {})
        st.setdefault("warned", [])
        ws = (e.get("status") or {}) if e.get("worker_up") else {}      # worker 不在时不算“跑完了”
        run = [j for j in ws.get("running", []) if not j.get("hold")]
        late = late_jobs(ws)                         # 截止前跑不完、不会在这台放的（borrow_due 提醒附上，看板的 borrow_cut 告警按它认）
        g = ws.get("guest") or {}                    # worker 量的对方用量、超出预留的几项、内核杀了对方进程的时刻
        back = g.get("back") or ([f"PID {p.get('hpid')}（{p.get('comm') or 'labmon 之外'}）" for p in ws.get("gpu_procs") or [] if p.get("owner") == "other"]
                                 if swap else [])    # 换给自己的没有 guest 指令：别人的 GPU 进程就算
        tight = g.get("tight")                       # 快撞上限：不停正在跑的，提醒用户线下和对方商量
        if ws:
            st["last"] = {"late": late, "back": back, "tight": tight}
        else:                                        # 这一轮没拿到 worker 状态（读这台失败、状态读不出、worker 不在）：沿用上次的，
            late, back, tight = ((st.get("last") or {}).get(k) for k in ("late", "back", "tight"))   # 免得 borrow_cut、borrow_back 消失再出现、labmon wait 重复叫醒
            late, back = late or [], back or []      # worker 不在时项目探针那边车道没有 held，沿用的 late 不会报出 borrow_cut
        who, name, msgs = b.get("holder") or "原使用人", e.get("label", hid), []
        say = lambda key, kind, msg: _say(st, key, kind, hid, b.get("holder"), msg, evs,   # noqa: E731
                                          [] if pool and kind != "borrow_return" else msgs)   # 算力池的只进记录（还不成的照弹）
        if b.get("until_bad"):                       # 手写的 borrow_until 认不出（config._until）：说一次，不然静默当成没约定
            say(f"badts:{b['until_bad']}", "borrow", f"{name} 的 borrow_until「{b['until_bad']}」认不出，当作没约定时间：要写成 "
                f"{fmt_time(t, year=True)} 这样（YYYY-MM-DD HH:MM），改 ~/.config/labmon/hosts.toml 或 labmon borrow {hid} --until …")
        aw = g.get("away") or {}                     # 对方说好不用、却回来了（worker 判的）：清掉主机清单的 borrow_away，下面重下指令时就不带了
        if aw.get("end") and b.get("away") and abs(b["away"] - aw["until"]) < 60 and not swap:   # 作废的是现在这一次（刚重新勾的不清）
            set_fleet_keys({"borrow_away": None}, f"{name}：{who} 说好 {fmt_time(aw['until'])} 前不用，现在回来了，恢复按预留借卡", sid=hid)
            b = {**b, "away": None}
            st["away_back"] = {"t": aw["end"], "msg": f"{who} 说好 {fmt_time(aw['until'])} 前不用，现在回来了（{aw.get('why') or '在用'}）：{hid} 恢复按预留借卡，超出的部分不再放新任务"}
            if back:                                 # 同一件事只提醒一次（下面的“用得比预留多”不再另说）
                st["back"] = round(t)
            say(f"away:{aw['end']}", "borrow_back", f"{name} 上 {who} 说好 {fmt_time(aw['until'])} 前不用，现在回来了（{aw.get('why') or '在用'}）："
                "恢复按预留借卡，超出的部分不再放新任务，" + ("正在跑的照常跑完" if run else "现在没有任务在跑") + f"；和 {who} 商量要不要现在还")
        away_on = bool(b.get("away") and t < b["away"])   # 说好不用还有效：新出现的对方 GPU 进程连着两轮才算回来（worker），一轮的不提醒
        if ws and not st.get("returning"):
            _sync(hid, ovr, ws.get("overrides") or [], lease)
        if cut and cut - DUE_WARN_S <= t < cut:
            say(f"soon:{round(cut)}", "borrow_due", f"{name} 约定 {fmt_time(cut)} 还给 {who}（{why}）：还有 {(cut - t) / 3600:.1f} 小时，"
                + ("到时在组内页把它换回去" if swap else "之后不再放新任务" + (f"，正在跑的 {_jobs(run)}" if run else "")) + _late_text(late, why, user=True))
        if cut and t >= cut:
            if not swap and not st.get("returning"):
                st["returning"] = round(t)
                ovr("add", "drain", hid)
            say(f"due:{round(cut)}", "borrow_due", f"{name} 到了约定时间（{fmt_time(cut)}）：不再放新任务，" + (
                (f"{_jobs(run)} 跑完后" if run else "现在就可以") + f"在组内页「换人」换回给 {who}" if swap else
                f"{_jobs(run)} 跑完就还给 {who}" if run else f"现在还给 {who}") + _late_text(late, why, user=True))
            if run and t >= cut + OVER_S:
                say(f"over:{round(cut)}", "borrow_due", f"{name} 已超过约定时间 {(t - cut) / 3600:.0f} 小时，{_jobs(run)} 还在跑；"
                    + (f"跑完再换回给 {who}" if swap else "要马上还就在看板上点「收回」" if pool else "要马上还就在看板上点「现在停」"))
        if back and not st.get("back") and not away_on and not pool:
            st["back"] = round(t)
            say(f"back:{round(t)}", "borrow_back", f"{name} 上" + (f"有了别人的 GPU 进程（{'、'.join(back[:3])}）：不再放新任务，" if swap else
                f" {who} 用得比预留多（{'；'.join(back[:3])}）：这部分不再放新任务，") + (
                "正在跑的照常跑完" if run else "可以还了") + ("" if swap else f"；和 {who} 商量要不要现在还"))
        elif ws and not back:
            st["back"] = None
        if tight and not st.get("tight") and not pool:
            st["tight"] = round(t)
            say(f"tight:{round(t)}", "borrow_back", f"{name} 快撞上限（{tight}）：正在跑的{(' ' + _jobs(run)) if run else ''}照常跑完、不放新任务；"
                + f"要不要停由你和 {who} 商量，要停就在看板上点「现在停」")
        elif ws and not tight:
            st["tight"] = None
        seen = st.get("seen") or 0                   # 对方被挤到：我们的任务被 OOM 挤掉、内核杀了 labmon 之外的进程。这次借用的预留调高
        hits = [f"{v['name']} {v['why']}" for v in ws.get("voided") or [] if v.get("how") == "squeeze" and v["t"] > seen] \
            + ([f"内核杀了 labmon 之外的进程（多半是 {who} 的）"] if (g.get("hit") or 0) > seen else [])
        if hits and not swap:
            st["seen"] = max([v["t"] for v in ws.get("voided") or [] if v.get("how") == "squeeze"] + [g.get("hit") or 0])
            st["bump"] = min(BUMP_MAX, round((st.get("bump") or 1.0) * BUMP_X ** len(hits), 2))
            say(f"hit:{st['seen']}", "borrow_hit", f"{name} 上挤到了 {who}（{'；'.join(hits[:3])}）：这次借用给 {who} 的预留调到 ×{st['bump']:g}")
        if st.get("returning") and ws and not run and not swap:
            try:
                done = finish_fn(hid, wrote=(ws.get("storage") or {}).get("wrote"), stuck=(ws.get("keepalive") or {}).get("stuck"),
                                 mine=(ws.get("keepalive") or {}).get("mine"),
                                 kill=[j["pid"] for j in ws.get("running", []) if j.get("hold") and j.get("pid")])
                if not pool:                         # 算力池的归还只进记录（finish 记了 borrow_return）
                    msgs.append(done)
                st.update(returning=None, done=round(t))
                out += msgs
                continue
            except ConfigError as x:                 # 多半是连不上：这一次要还只说一次，连上还掉了再报结果
                say(f"nofin:{st['returning']}", "borrow_return", f"{name} 没还成：{x}；连上了会自动再还")
        out += msgs
        ps = b.get("for")
        kp = None if swap else _keep(hid, t)
        e["borrow"] = {**b, "cutoff": cut, "why": why, "returning": st.get("returning"), "now": bool(st.get("returning") and st.get("now")), "back": back, "tight": tight,
                       "overdue": bool(cut and t >= cut and run), "keep": kp, "other": g.get("other"),
                       "late": late, "late_do": late_do(why) if late else None, "late_say": late_do(why, user=True) if late else None}
        if pool:                                     # 算力池的：实验会话不往这台加清单、不用知道它借没借着
            continue
        if st.get("returning") or (cut and t >= cut):           # 要还的原因照实写：到点、用户点了跑完就还、现在停
            due = f"到了约定时间（{fmt_time(cut)}）" if cut and t >= cut else "正在停下，停完就还" if st.get("now") else "要还了（用户点了跑完就还）"
            wake.append({"key": f"borrow_due:{hid}", "projects": ps, "host": hid,
                         "msg": f"{hid} {due}，不再放新任务" + ("" if swap or st.get("now") else "，跑完就还") + "：别再往它上面加任务，给已有清单追加行也不要" + _late_text(late, why)})
        else:
            gpus = (ws.get("info") or {}).get("gpus") or []
            wake.append({"key": f"borrow:{hid}", "projects": ps,
                         "msg": f"借到 {who} 的 {hid}" + (f"（{len(gpus)} 张 {gpus[0].get('name')}）" if gpus else "")
                         + (f"，到 {fmt_time(cut)}：之前跑得完的任务才放行（按预计时长估：同类跑过的实际耗时，或按 实际/估时 折算的 est_min）" if cut else "")
                         + (f"，只给 {'、'.join(ps)} 用" if ps else "")
                         + (f"；{keep_text(kp)}" + ("" if kp.get("away") else "，对方超出的部分不放新任务，正在跑的照常跑完") if kp else "")
                         + f"。登记队列：labmon worker add {hid} ROOT JOBS --project <项目>；独占的等对方用完才跑，被对方打扰会作废重排"})
            if ws:                                   # 借来却空着（只有保活）：没有在跑的（占位任务不算）、也没有在等的，连着 IDLE_S 才报；worker 不在的轮次不动起点
                st["idle"] = None if run or ws.get("waiting") else st.get("idle") or round(t)
            if st.get("idle") and t - st["idle"] >= IDLE_S:      # 维护者 2026-11-18 定：只给实验会话，不推给用户；key 带起点，这一段空着只报一次
                wake.append({"key": f"borrow_idle:{hid}:{st['idle']}", "projects": ps,
                             "msg": f"借来的 {hid} 从 {fmt_time(st['idle'])} 起空着（没有任务在跑、也没有在等的，卡上只有保活）："
                             f"有活就追加到它上面的长期清单或 labmon worker add {hid} ROOT JOBS --project <项目>；用不上了告诉用户，可以早点还给 {who}"})
        if back and not away_on:
            wake.append({"key": f"borrow_back:{hid}", "projects": ps, "host": hid, "msg": f"{who} 在用 {hid}（{'；'.join(back[:3])}）：这部分不再放新任务"})
        ab = st.get("away_back")                     # 说好不用被对方作废：一小时内 wait 都报（back 可能为空：hold 为真且在预留以内、因 OOM 作废）
        if ab and t - ab["t"] < OVER_S:
            wake.append({"key": f"borrow_back:{hid}:away", "projects": ps, "host": hid, "msg": ab["msg"]})
    log_events(evs, t)

    def merge(cur):                                  # 只并本轮改的几项：命令行、页面可能同时在改 returning（要还、撤销）
        for hid, st in sts.items():
            c = cur.setdefault(hid, {})
            c.update(warned=st.get("warned", [])[-20:], back=st.get("back"), tight=st.get("tight"), last=st.get("last"), idle=st.get("idle"), **{k: st[k] for k in ("bump", "seen", "away_back") if st.get(k)})
            if st.get("done"):
                c.update(returning=None, done=st["done"])
            elif st.get("returning") and not c.get("returning") and st["returning"] > (c.get("kept") or 0):   # 本轮中途撤销了的不写回
                c["returning"] = st["returning"]
    _update(merge)
    return out, wake


