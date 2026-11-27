"""组内服务器的平台操作：看板服务里跑，只做本机页面上人点的操作（平台账号是全组共用的，见 docs/fleet.md）。

开机参数 boot.json（状态目录 overview/fleet/）：每台的作业名、资源池、镜像、端口映射、配置（CPU、内存、硬盘、GPU）、IP、
平台上的作业 id 与创建时间，以及平台“克隆”返回的整份配置（启动命令、挂载卷、规格 id 等，以后开机直接用它提交）。
来源有两个：与平台核对时读回的，或网页上作业详情里“Json 文件”复制下来导入的（labmon fleet import）。

与平台核对（sync）：登录（验证码人输）→ 读作业列表 → 逐台（含自用主机）核对作业名、资源池、端口 22→port → 补克隆配置 → 汇总。
汇总写进 platform.json：平台上运行中的作业、没登记的作业、对不上的地方（含公用池超全组上限；各池台数页面由 fleetview 按登记现算，不存）。

续租（renew，可一次选几台，只登录一次）：登录 → 探测并核对（先各探一次，在跑实验或有未识别进程而页面上没确认的就停；
平台上找到作业、克隆配置；有一台不对就哪台都不动）→ 逐台：停止旧作业 → 提交新作业、等运行 → 等 ssh、开保活。
作业已被平台停掉的跳过停止，克隆同池最近一个同名作业重开（公用池先看全组上限）。
开保活（ka）：不碰平台，只 ssh 上去经 fleet.ka_start 开保活（与保活守护同一个脚本：GPU 上有别的进程不开，开后盯着让路）；续租停在最后一步时用它补。
关机（off）、换人（swap）、开新的（open）走同一条流程（run_swap），开机参数按“人 × 池 × 端口”拼（见 fleetboot.py）：
先核对（探旧服务器、在平台上找到旧作业、拼好新作业的配置、挑端口、数配额、逐项核对规格、挂载卷、镜像），有一项不对就什么都不动；
停旧作业 → 停下就立即提交新作业（这段每几秒查一次，空档十几秒）→ 等运行 →
改主机清单（换人改 holder，开新的加一块；换给自己就把这台移成自用主机，自用主机换给别人就标成已归还）与开机参数
→ 等 ssh、开保活 → 给出连接信息。作业名缺省是使用人的名字，同名作业已在跑就加后缀（见 fleetboot.job_name）。
换人沿用原来那台的端口；开新的挑空闲端口；开已关机的服务器先试它原来的端口。关机后 boot.json 记下 off，之后不探、不提醒租期、不算配额。
停旧作业、提交新作业这两步续租与换人、开机共用（stop_job、submit_job）：进度记在 op.json 这一台的 stop_sent / stopped / submit_sent / up，
平台明确没收提交的（Refused）把 submit_sent 清掉；出错时页面上这一台此刻的状态由 _note 按这几个标记写。
平台给的作业创建时间（createTime，"2026-11-05 10:00:00"）：比较按 config.parse_time 换成时刻，文案按 _created 写；
存进 boot.json、platform.json 的 created 照平台原文（页面上 fcr 自己截）。

平台会话：同一账号只保留一个登录，看板登录会把网页上的人顶下线，网页上有人登录也会让看板的令牌失效。
令牌存在钥匙串（与密码同一处），看板服务重启后接着用，48 小时到期；失效（-2）就删掉、停下，要求重新输验证码。不后台轮询。
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import threading
import time

from . import config as C
from . import deploy as DP
from . import fleet as F
from . import fleetboot as FB
from . import lease as LS
from . import platform as P
from . import renew as R
from .config import active_hosts, fmt_time, parse_time
from .health import lease_end
from .records import log_events
from .service import log, notify
from .state import load_json, save_json, snapshot
from .users import person_of
from .workerctl import clear_overrides

LOCK = threading.Lock()
CAPTCHA_GAP = 10                 # 换验证码至少隔这么多秒（像人点“换一张”）
SESS: dict = {"cli": None, "at": None, "user": None, "pending": None, "captcha_at": 0.0, "tried": False}   # pending：(tok, 取了验证码的会话)；读写都在 LOCK 里
SYNC_STEPS = ["登录", "读作业列表", "核对各台", "补克隆配置", "读资源池与镜像", "汇总"]
RENEW_EACH = ["停止旧作业", "提交新作业、等运行", "等 ssh、开保活"]
TAIL_MINE = "等 ssh、拉起 worker"            # 自用主机续租的最后一步：保活由 worker 管
KINDS = ("sync", "renew", "ka", "off", "swap", "move", "open")  # 页面组内面板里的操作；借来用（borrow）从这台的「借用」里发起，同样在后台跑、显示进度
OP_NAMES = {"sync": "与平台核对", "renew": "续租", "ka": "开保活", "off": "关机", "swap": "换人", "move": "换节点", "open": "开机", "borrow": "借来用"}


def boot() -> dict:
    return load_json(F.state_file("boot.json"), {})


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _live(st: dict) -> bool:
    """这个操作还在做：记下的进程在；是本进程的话，那个线程（按名字 fleet-<kind> 找）也还在。
    线程在收尾里再出错退出了（没来得及把 active 清掉），不算在做，否则 op.json 永远“正在操作”。"""
    if not _alive(st.get("pid")):
        return False
    if int(st["pid"]) != os.getpid():
        return True
    return any(t.name == f"fleet-{st.get('kind')}" and t.is_alive() for t in threading.enumerate())


def op() -> dict:
    """当前（或上一次）操作的进度。做到一半看板服务重启了、或线程没了，就标成停在那一步。"""
    st = load_json(F.state_file("op.json"), {})
    if st.get("active") and not _live(st):
        why, cut = "看板服务中途重启（或操作线程出错退出），操作停在这一步；平台与服务器此刻的状态要到平台上看", []
        def f(d):
            if d.get("active") and not _live(d):
                for x in d.get("steps") or []:
                    if x["state"] == "run":
                        x.update(state="err", note=why)
                        cut.append(x["name"])
                d.update(active=False, error=why)
                cut.append(None)
            return d
        st = F.edit_state("op.json", f)
        if cut:
            _op_event("op_fail", st, f"停在「{cut[0] or '?'}」：{why}")
    return st


def busy() -> dict[str, str]:
    """正在做平台操作的服务器（含自用主机）：{id: 操作}，没在做就空（op() 已把线程没了的标成停下）。
    探测与保活守护（跳过它）、借用（不借）、掉线判定（连不上是预期的）、自动预备续租（不预备）都问这一个。"""
    o = op()
    return {i: o["kind"] for i in o.get("ids") or []} if o.get("active") else {}


def _created(j: dict) -> str:
    """平台作业的创建时间给人看：createTime 文字 → 时刻 → 与别处一致的 MM-DD HH:MM。"""
    return fmt_time(parse_time(j["createTime"]))


def _op_event(ev: str, st: dict, tail: str) -> None:
    """平台操作的开始、完成、停下记进记录：动到的服务器写进 host / hosts，使用人写进 who。"""
    if st.get("kind") == "borrow" and ev != "op_fail":   # 借来用的结果由 borrow.begin 记（kind borrow），这里只记停下
        return
    ids, what = st.get("ids") or [], st.get("what")
    name = OP_NAMES.get(st.get("kind"), st.get("kind"))
    msg = f"开始{name}{f'：{what}' if what else ''}" if ev == "op_start" else f"{name}{f'（{what}）' if what else ''}{tail}"
    log_events([{"kind": ev, "host": ids[0] if len(ids) == 1 else None, "hosts": ids if len(ids) > 1 else None,
                   "who": st.get("person"), "msg": msg}])


def platform_state() -> dict:
    return load_json(F.state_file("platform.json"), {})


def _restore() -> None:
    """看板服务刚起来：钥匙串里有还没到期的令牌就接着用（每个进程只试一次）。"""
    if SESS["cli"] is not None or SESS["tried"]:
        return
    SESS["tried"] = True
    try:
        url, user = platform_conf(F.conf() or {})
    except (P.PlatformError, C.ConfigError):
        return
    got = P.load_session(url, user)
    if got:
        SESS.update(cli=got[0], at=got[1], user=user)


def session() -> dict:
    _restore()
    return {"logged_in": SESS["cli"] is not None, "at": SESS["at"],
            "until": P.token_exp(SESS["cli"].token, SESS["at"]) if SESS["cli"] is not None else None}


def adopt(cli: P.Client, user: str) -> None:
    """别处（自用主机续租）刚登录了平台：组内操作也用这个会话（同一账号只保留一个登录，旧令牌已被顶掉）。"""
    SESS.update(cli=cli, at=round(time.time()), user=user, tried=True)
    P.save_session(cli, user, SESS["at"])
    log_events([{"kind": "login", "msg": f"登录平台（{user}，自用主机续租时）"}])


# ---------- 网页上的 Json 文件 ----------
def parse_jobjson(text: str) -> dict:
    """作业详情“Json 文件”的文本 → dict。它不是合法 JSON：数组元素前带 "0": 序号，还有多余的逗号。"""
    out, stack, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            k = j + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if stack and stack[-1] == "[" and k < n and text[k] == ":":
                i = k + 1                                 # 数组里的 "0": 序号，去掉
                continue
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c in "[{":
            stack.append(c)
        elif c in "]}" and stack:
            stack.pop()
        out.append(c)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def from_detail(d: dict) -> dict:
    """作业详情（Json 文件，或 getWorkTaskInfo 的返回）→ 开机参数里的字段。"""
    r = (d.get("workTaskRoleVOList") or [{}])[0]
    pm = r.get("portMap") or {}
    if isinstance(pm, str):
        try:
            pm = json.loads(pm)
        except ValueError:
            pm = {}
    out = {"image": r.get("imageName"), "ip": r.get("ipAddress"), "ports": {str(k): str(v) for k, v in pm.items()},
           "cpu": r.get("cpuCoreNumber"), "memory": r.get("memory"), "disk": r.get("hardDisk"), "gpu": r.get("gpuTotal"),
           "container": r.get("containerId"), "role_id": r.get("id"), "pool": d.get("poolName"), "created": d.get("createTime"),
           "env": d.get("envMap") or None, "gpu_uuids": d.get("uuidList") or None}
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def import_json(sid: str, text: str, cfg: dict | None = None, t: float | None = None) -> dict:
    """把网页上复制的 Json 文件存成这一台的开机参数（核对端口与资源池，对不上就不存）。"""
    cfg = cfg if cfg is not None else F.conf()
    s = next((x for x in (cfg or {}).get("servers", []) if x["id"] == sid), None)
    if s is None:
        raise P.PlatformError(f"组内服务器里没有 {sid}")
    d = from_detail(parse_jobjson(text))
    if d.get("ports", {}).get("22") != str(s["port"]):
        raise P.PlatformError(f"Json 里的端口映射是 {d.get('ports')}，不是 22→{s['port']}，不像这一台的")
    if d.get("pool") and d["pool"] != s["pool"]:
        raise P.PlatformError(f"Json 里的资源池是 {d['pool']}，主机清单写的是 {s['pool']}")
    t = time.time() if t is None else t

    def f(b):
        e = b.setdefault(sid, {})
        e.update(d, holder=s["holder"], job=s["holder"], src="网页 Json 文件", t=round(t))
        return e
    return F.edit_state("boot.json", f)


# ---------- 平台会话 ----------
def platform_conf(cfg: dict) -> tuple[str, str]:
    """平台网址与账号：[fleet] 的 platform、platform_user，没写就用自用主机的（在用的优先；自用主机都关了取已归还的，开回来要登录）。"""
    url, user = cfg.get("platform"), cfg.get("platform_user")
    for h in C.all_hosts():
        url, user = url or h.get("platform"), user or h.get("platform_user")
    if not url or not user:
        raise P.PlatformError("[fleet] 或自用主机里要写 platform 与 platform_user")
    return url, user


def captcha(cfg: dict, client=None) -> dict:
    """取一张验证码给页面显示：{"code": 验证码, "tok": 这张的标识}。会话留着，提交时用同一个（平台按 Cookie 认验证码）；
    提交时带回 tok，两个页面先后取过的，先取的那张已作废，不拿它去登录。"""
    if op().get("active"):
        raise P.PlatformError("正在操作平台，等它做完")
    with LOCK:
        if time.time() - SESS["captcha_at"] < CAPTCHA_GAP:
            raise P.PlatformError(f"换得太快了，隔 {CAPTCHA_GAP} 秒再换")
        SESS["captcha_at"] = time.time()
    cli = client or P.Client(platform_conf(cfg)[0])
    code, tok = cli.captcha(), secrets.token_hex(8)
    with LOCK:
        SESS["pending"] = (tok, cli)
    return {"code": code, "tok": tok}


def logout(why: str = "退出平台登录") -> None:
    user, was = SESS.get("user"), SESS.get("cli") is not None
    with LOCK:
        SESS.update(cli=None, at=None, pending=None, tried=True)
    if user:
        P.drop_session(user)
    if was:
        log_events([{"kind": "login", "msg": why}])


# ---------- 后台操作（一次只做一件） ----------
def steps_of(kind: str, cfg: dict, ids: list[str], person: str | None = None) -> list[str]:
    sl = {x["id"]: x for x in FB.slots(cfg)}
    names = {i: x["label"] if x.get("mine") else x["holder"] for i, x in sl.items()}     # 自用主机按主机名称叫
    tail = {i: TAIL_MINE if x.get("mine") else RENEW_EACH[-1] for i, x in sl.items()}
    if kind == "renew":
        return ["登录", "探测并核对"] + [f"{names[i]}：{n}" for i in ids for n in RENEW_EACH[:-1] + [tail[i]]]
    if kind == "ka":
        return [f"{names[i]}：{tail[i]}" for i in ids]
    if kind == "borrow":
        from .borrow import BEGIN_STEPS           # borrow 在上层 import 本模块（busy、_probe），这里只能延迟 import
        return list(BEGIN_STEPS)
    if kind in ("off", "swap", "move", "open"):
        old, new = (names[ids[0]] if ids else None), person or (names[ids[0]] if ids else None)
        out = ["登录", "核对"]
        if kind != "open":
            out += [f"{old}：停止作业"]
        if kind != "off":
            out += [f"{new}：提交新作业、等运行", f"{new}：等 ssh、开保活"]
        return out
    return SYNC_STEPS


def _check_start(kind: str, cfg: dict, ids: list[str], kw: dict) -> None:
    """开始前的本地检查（不连平台）：选的服务器、人、池、规格。续租、开保活、关机、换人、换节点也可以选自用主机。"""
    known, off = {x["id"] for x in (cfg["servers"] if kind == "open" else FB.slots(cfg))}, F.off_servers()
    if kind == "sync":
        return
    if any(i not in known for i in ids) or (not ids and kind != "open"):
        raise P.PlatformError(f"要选组内服务器里有的服务器：{ids}")
    if kind != "borrow":                              # 借来在用的：续租、关机、换人、换节点、开保活都会停掉自己的 worker 与 /tmp/labmon，先还
        lent = [s["id"] for s in cfg["servers"] if s["id"] in ids and F.borrowed(s)]
        if lent:
            raise P.PlatformError(f"{'、'.join(lent)} 正借来在用：先在它的「借用」里还掉（若已被平台停掉连不上，先点「与平台核对」，确认作业不在运行后会自动归还），再{OP_NAMES[kind]}")
    if kind in ("off", "swap", "move", "open") and len(ids) > 1:
        raise P.PlatformError("关机、换人、换节点、开机一次一台")
    if kind == "open":
        if ids and ids[0] not in off:
            raise P.PlatformError(f"{ids[0]} 没关机：要换人用「换人」")
        if not ids and kw.get("pool") not in FB.pool_names(cfg):
            raise P.PlatformError(f"要选看板认得的资源池：{kw.get('pool')}")
    elif any(i in off for i in ids):
        raise P.PlatformError(f"{'、'.join(i for i in ids if i in off)} 已关机：要用就开机")
    if kind in ("swap", "move", "open"):
        kw["spec"] = str(kw.get("spec") or "").strip() or None
    if kind == "move":
        if not kw["spec"]:
            raise P.PlatformError("换节点要选目标规格")
        holder = next((x["holder"] for x in FB.slots(cfg) if x["id"] == ids[0]), None)
        p = str(kw.get("person") or "").strip() or None
        if p and holder and p != holder:
            raise P.PlatformError("换节点不换使用人：要同时换人请用「换人」")
        kw.pop("person", None)
    if kind in ("swap", "open"):
        kw["person"] = str(kw.get("person") or "").strip() or None
        if not kw["person"] and not ids:
            raise P.PlatformError("开新的要选人")
        if kw["person"] and not P.NAME_RE.match(kw["person"]):
            raise P.PlatformError(f"使用人的名字也是平台作业名：只能用字母、数字、_ ~ -，最多 32 个：{kw['person']}")


def start(kind: str, cfg: dict, code: str | None = None, ids: list[str] | None = None, tok: str | None = None, *, account: str | None = None, **kw) -> None:
    """开始一次操作。code：新输的验证码（要登录），tok 是取它时给的标识（页面带；不带的照旧用最近取的那张）；
    不给 code 就沿用已登录的会话。ids：要动的服务器（关机、换人、开机一次一台）。
    开保活不碰平台，不用登录。换人、开新的的 kw：person（使用人）、pool（开新的的池）。"""
    if kind not in (*KINDS, "borrow"):
        raise P.PlatformError(f"没有这种操作：{kind}")
    if not LS.primary():                          # 备用的看板服务不做平台操作、不登录平台（#163；同一账号只保留一个登录）
        raise P.PlatformError(f"{OP_NAMES.get(kind, kind)}没做：{LS.text()}")
    ids = list(dict.fromkeys(ids or []))
    _check_start(kind, cfg, ids, kw)
    _restore()
    cli = None
    if kind in ("ka", "borrow"):                  # 不碰平台，不用登录
        code = None
    elif code:
        with LOCK:
            pending = SESS.get("pending")
        if pending is None:
            raise P.PlatformError("先取验证码")
        if tok and tok != pending[0]:
            raise P.PlatformError("验证码在别的页面刷新过了，这张已作废：重新取一张")
        cli = pending[1]
    elif SESS["cli"] is not None:
        cli = SESS["cli"]
    else:
        raise P.PlatformError("看板还没登录平台：先输验证码")
    steps = steps_of(kind, cfg, ids, kw.get("person"))
    sl = {x["id"]: x for x in [*cfg["servers"], *FB.slots(cfg)]}
    what = "、".join(f"{sl[i]['holder']} {i}" if i in sl else i for i in ids) or kw.get("pool")
    spec_label = None
    if kind in ("swap", "open") and kw.get("person"):
        what = f"{what} → {kw['person']}"
    elif kind == "move" and kw.get("spec"):
        pl = sl[ids[0]]["pool"] if ids and ids[0] in sl else None
        spec_label = FB.spec_name(FB.pool_specs(pl), kw["spec"]) if pl else kw["spec"]
        what = f"{what} → {spec_label}"

    fn = {"sync": run_sync, "renew": run_renew, "ka": run_ka, "off": run_swap, "swap": run_swap, "move": run_swap, "open": run_swap, "borrow": run_borrow}[kind]
    if kind != "sync":
        kw["ids"] = ids
    if fn is run_swap:
        kw.update(kind=kind, account=account)
    if fn is run_borrow:
        kw["account"] = account
    th = threading.Thread(target=fn, args=(cfg, cli, code), kwargs=kw, name=f"fleet-{kind}", daemon=True)
    who = kw.get("person") or (sl[ids[0]]["holder"] if kind == "move" and ids and ids[0] in sl else None)

    def init(st):
        if st.get("active") and _live(st):
            return "正在操作，等它做完"
        if DP.restarting():                           # 与部署后安排重启在同一把锁里判定（deploy._claim）：重启会把操作杀在半路
            return "看板服务马上要重启（刚部署了新代码），等它起来再做"
        st.clear()
        st.update(kind=kind, t0=round(time.time()), active=True, error=None, pid=os.getpid(), ids=ids, what=what,
                  person=who, pool=kw.get("pool"), spec=kw.get("spec"), spec_name=spec_label,
                  servers={i: {} for i in ids}, steps=[{"name": n, "state": "wait", "note": None} for n in steps])
        th.start()                                    # 写进 op.json 的同时拉起线程：op() 按线程判活，中间不留空档
        return ""
    if (why := F.edit_state("op.json", init)):
        raise P.PlatformError(why)
    _op_event("op_start", op(), "")
    if code:
        with LOCK:
            SESS["pending"] = None


def dismiss() -> None:
    """收起上一次操作的结果；线程已经没了的（op() 会先标成停下）也能收。"""
    op()
    F.edit_state("op.json", lambda st: st.clear() if not st.get("active") else None)


class _Run:
    """一次操作的进度：每步写进 op.json 供页面看；出错停在那一步，不自动重试。"""

    def __init__(self, pace: float):
        self.i = 0
        self.pace = pace

    def human(self):
        if self.pace:
            time.sleep(self.pace * (0.7 + random.random()))

    def mark(self, s: str, note: str | None = None, **flags):
        def f(st):
            if self.i < len(st["steps"]):
                st["steps"][self.i].update(state=s, note=note)
            st.update(flags)
        F.edit_state("op.json", f)

    def step(self, note: str | None = None, **flags):
        self.mark("ok", note, **flags)
        self.i += 1
        self.mark("run")

    def fail(self, x: Exception, server: str | None = None):
        """停在这一步；server 是这几台此刻状态的说明（写给人看，也进记录）。"""
        if isinstance(x, P.AuthError):
            logout(f"平台登录失效，已退出：{x}")
        self.mark("err", str(x))
        st = F.edit_state("op.json", lambda st: st.update(active=False, error=str(x), server=server) or st)
        steps = st.get("steps") or []
        _op_event("op_fail", st, f"停在「{steps[self.i]['name'] if self.i < len(steps) else '?'}」：{x}{f'。{server}' if server else ''}")

    def done(self, note: str | None = None):
        self.mark("ok", note)
        _op_event("op_done", F.edit_state("op.json", lambda st: st.update(active=False, done=round(time.time())) or st), f"完成{f'：{note}' if note else ''}")


def login(r: _Run, cli: P.Client, code: str | None, user: str, password) -> None:
    r.mark("run")
    if code:
        cli.login(user, password(user), code)
        SESS.update(cli=cli, at=round(time.time()), user=user, tried=True)
        P.save_session(cli, user, SESS["at"])
        log_events([{"kind": "login", "msg": f"登录平台（{user}）"}])
        r.step(user)
    else:
        r.step(f"沿用 {fmt_time(SESS['at'], date=False)} 的登录")


def _running(j: dict) -> bool:
    return int(j.get("runningCount") or 0) >= 1


SYNC_GRACE_S = 300               # 平台核对时刻允许早于 link_step 首次连不上时刻这么多秒（作业刚停用户就点核对、看板随后首次探失败）


def job_stopped(sid: str, *, after: float | None = None, down_since: float | None = None, plat: dict | None = None) -> tuple[bool, float | None]:
    """最近一次与平台核对（platform.json）是否确认 sid 的作业不在运行（不含端口映射不对）。
    after：至少在这个时刻之后核对过（借用取 borrow_since）；down_since：本次掉线首次连不上的时刻（核对不早于它前 SYNC_GRACE_S 秒）。
    返回 (是否确认不在运行, 核对时刻)。"""
    plat = platform_state() if plat is None else plat
    pt = plat.get("t")
    if not pt or sid not in (plat.get("stopped") or ()):
        return False, None
    if after is not None and pt < after:
        return False, None
    if down_since is not None and pt < down_since - SYNC_GRACE_S:
        return False, None
    return True, pt


def _clear_stopped(sid: str) -> None:
    """这台新作业已起：从上次平台核对的 stopped 里去掉，免得旧核对残留误导。"""
    if F.state_file("platform.json").exists():
        F.edit_state("platform.json", lambda d: sid in (d.get("stopped") or ()) and d.update(stopped=[x for x in d["stopped"] if x != sid]))


def _find_job(cli: P.Client, jobs: list[dict], s: dict, human) -> tuple[dict | None, list[dict], str | None, bool]:
    """平台上这一台运行中的作业：作业名（s["job"]）、资源池对、端口 22→port。返回 (作业, 角色, 对不上的原因, 作业是否确实不在运行)。
    第一页里没有就按名字查一次（像在网页上搜名字）。"""
    def cands(js):
        return [j for j in js if j.get("taskName") == s["job"] and j.get("poolName") == s["pool"] and _running(j)]
    cs = cands(jobs)
    if not cs:
        human()
        cs = cands(cli.jobs(s["job"]))
    if not cs:
        return None, [], f"平台上没有运行中的 {s['job']}（{s['pool']}）", True
    for j in cs:
        human()
        roles = cli.roles(j["id"])
        if P.roles_port_ok(roles, s["port"]):
            return j, roles, None, False
    return None, [], f"运行中的 {s['job']} 端口映射都不是 22→{s['port']}", False


def find_job(cli: P.Client, jobs: list[dict], s: dict, human) -> tuple[dict | None, list[dict], str | None]:
    """平台上这一台运行中的作业：作业名（s["job"]）、资源池对、端口 22→port。返回 (作业, 角色, 对不上的原因)。"""
    return _find_job(cli, jobs, s, human)[:3]


def reconcile(cfg: dict, sl: list[dict], running: list[dict], found: dict[str, tuple[dict, dict]], old: dict, own: dict) -> dict:
    """与平台核对的比对（纯函数，不连平台）。running：平台上运行中的作业；found：各台（sl 里的）找到的作业与详情 {id: (作业, from_detail 的字段)}；
    old：核对前本机的开机参数（boot.json）；own：自用主机的平台作业名 → 主机 id。
    返回 changes [(id, 规格变了的说明)]（镜像、CPU、内存、硬盘、IP 与上次存的不同）、rows（平台上运行中的作业，带登记到的 sid，
    自用主机写“自用 <id>”，没登记的 None）、pools（各池运行中台数）、public（公用池台数）、issues（没登记的作业、公用池超全组上限）。"""
    changes = []
    for s in sl:
        if s["id"] not in found:
            continue
        d, o = found[s["id"]][1], old.get(s["id"]) or {}
        for k, name in (("image", "镜像"), ("cpu", "CPU"), ("memory", "内存"), ("disk", "硬盘"), ("ip", "IP")):
            if o.get(k) is not None and d.get(k) is not None and o[k] != d[k]:
                changes.append((s["id"], f"{s['holder']}（{s['id']}）的{name}由 {o[k]} 变为 {d[k]}"))
    mine = {s["id"] for s in sl if s.get("mine")}
    by_id = {j.get("id"): (f"自用 {sid}" if sid in mine else sid) for sid, (j, _) in found.items()}
    rows, pools, issues = [], {}, []
    for j in running:
        sid = by_id.get(j.get("id")) or (f"自用 {own[j.get('taskName')]}" if j.get("taskName") in own else None)
        rows.append({"id": j.get("id"), "name": j.get("taskName"), "pool": j.get("poolName"), "created": j.get("createTime"),
                     "left": j.get("unusedTime"), "sid": sid})
        pools[j.get("poolName")] = pools.get(j.get("poolName"), 0) + 1
        if sid is None:
            issues.append(f"平台上运行中、看板没登记：{j.get('taskName')}（{j.get('poolName')}，{_created(j)} 创建）")
    pub = sum(n for p, n in pools.items() if p not in cfg["private_pools"])
    if cfg.get("public_max") is not None and pub > cfg["public_max"]:
        issues.append(f"公用池运行中 {pub} 个，超过全组上限 {cfg['public_max']}")
    return {"changes": changes, "rows": rows, "pools": pools, "public": pub, "issues": issues}


def run_sync(cfg: dict, cli: P.Client, code: str | None, password=P.password, pace: float = 3, t: float | None = None) -> None:
    r = _Run(pace)
    try:
        url, user = platform_conf(cfg)
        login(r, cli, code, user, password)
        r.human()
        jobs = cli.jobs()
        running = [j for j in jobs if _running(j)]
        r.step(f"第一页 {len(jobs)} 条，运行中 {len(running)} 个")
        t = time.time() if t is None else t
        issues, found, stopped = [], {}, []
        old, sl = boot(), FB.slots(cfg)                 # 组内服务器加自用主机（自用的克隆配置给“我”开新的时用）
        for s in sl:
            j, roles, why, is_stopped = _find_job(cli, jobs, s, r.human)
            if j is None:
                issues.append(f"{s['holder']}（{s['id']}）：{why}")
                if is_stopped:
                    stopped.append(s["id"])
                continue
            d = from_detail({"workTaskRoleVOList": roles, "poolName": j.get("poolName"), "createTime": j.get("createTime")})
            found[s["id"]] = (j, d)
            if j not in running:
                running.append(j)

            def upd(b, s=s, j=j, d=d):
                e = b.setdefault(s["id"], {})
                e.update(d, holder=s["holder"], job=j.get("taskName"), job_id=j.get("id"), src="平台核对", t=round(t))
            F.edit_state("boot.json", upd)
        r.step(f"对上 {len(found)} / {len(sl)} 台")
        cloned = 0
        for s in sl:
            j, e = (found.get(s["id"]) or (None,))[0], boot().get(s["id"]) or {}
            if j is None or (e.get("clone") and e.get("clone_job") == j.get("id")):
                continue
            r.human()
            c = cli.clone(j["id"])
            if (c or {}).get("taskName") != s["job"] or not P.clone_port_ok(c, s["port"]):
                issues.append(f"{s['holder']}（{s['id']}）：克隆出的配置对不上（作业名 {(c or {}).get('taskName')}，端口要 22→{s['port']}），没存")
                continue
            F.edit_state("boot.json", lambda b, s=s, c=c, j=j: b[s["id"]].update(clone=c, clone_job=j.get("id"), clone_t=round(t)))
            cloned += 1
        r.step(f"新存 {cloned} 份" if cloned else "都已有，没再克隆")
        try:
            info = FB.read_pools(cli, cfg, r.human, t)
            r.step(f"{len(info['pools'])} 个池、{len(info['volumes'])} 个存储卷、{sum(len(v) for v in info['images'].values())} 个镜像标签")
        except P.AuthError:
            raise
        except Exception as x:  # noqa: BLE001 — 只读的附带信息，没读成不耽误核对
            issues.append(f"读资源池与镜像没成功：{x}")
            r.step(f"没读成：{x}")
        own = {h.get("platform_job"): h["id"] for h in active_hosts() if h.get("platform_job")}
        rec = reconcile(cfg, sl, running, found, old, own)
        issues += rec["issues"]
        res = {"t": round(t), "running": rec["rows"], "stopped": stopped, "issues": issues, "changes": [m for _, m in rec["changes"]]}
        F.state_dir().mkdir(parents=True, exist_ok=True)
        save_json(F.state_file("platform.json"), res)
        for sid, m in rec["changes"]:
            F._event("boot", sid, m, t)
        r.done(f"{len(issues)} 处对不上" if issues else "都对得上")
    except Exception as x:  # noqa: BLE001 — 停在出错的那一步，页面显示原因
        r.fail(x)


# ---------- 续租、开保活 ----------
def _until(fn, what: str, wait_s: float, poll: float):
    t0 = time.time()
    while True:
        v = fn()
        if v:
            return v
        if time.time() - t0 >= wait_s:
            raise P.PlatformError(f"等了 {wait_s / 60:.0f} 分钟还没{what}")
        time.sleep(poll)


def _srv(sid: str, **flags) -> None:
    F.edit_state("op.json", lambda st: st.setdefault("servers", {}).setdefault(sid, {}).update(flags))


def _probe(sid: str) -> tuple[str | None, str]:
    """现在探一次这一台（与看板共用状态与锁），返回 (状态, 在跑什么)；连不上算 down。"""
    notify(F.tick(force=sid, busy=busy()))
    st = F._load_state().get(sid) or {}
    if st.get("fails"):
        return "down", st.get("error") or "连不上"
    smp = st.get("sample") or {}
    return smp.get("state"), F._brief_work(smp) if smp.get("work") else ""


def _probe_mine(hid: str) -> tuple[str | None, str]:
    """自用主机此刻的状态：取看板最近一次采集（worker 的任务与 GPU 进程归属），不另外 ssh。
    快照里没有这台、或 worker 没状态：状态 None（没探到，与组内服务器从没探过一样，要勾「已说好」才动）。"""
    snap = snapshot()
    row = next((x for x in FB.mine(None, snap.get("hosts") or []) if x["id"] == hid), None)
    if row is None:
        return None, "没探到"
    return row["state"], "、".join(w["label"] for w in row["work"] if w.get("label"))


def _lw(s: dict) -> dict | None:
    """这一台本地盘开机以来的新写入（fleet.local_writes，#149）：组内服务器取最近一次探测（_probe 刚探过）与扫出的路径，
    自用主机取看板最近一次采集（与 _probe_mine 同一份快照）。"""
    if s.get("mine"):
        return next((e.get("lw") for e in snapshot().get("hosts") or [] if e.get("id") == s["id"]), None)
    return F.server_lw(F._load_state().get(s["id"]) or {})


def _risk(s: dict, st: str | None, what: str, lw: dict | None = None, kind: str = "renew") -> str | None:
    """这一台此刻为什么不能直接动（续租、关机、换人要先勾「已说好」）；None 是可以动。
    自用主机上 worker 有任务在跑的那一支只有续租走到：关机、换人在 _need_ack 里先一步拒绝。
    lw：本地盘开机以来的新写入（local_writes）：labmon 之外写的到 LOCAL_ACK（risk）时同样要勾（续租、关机、换人都会清空本地盘，#149）。"""
    if st == "job":
        return f"worker 还有任务在跑（{what}）" if s.get("mine") else f"在跑实验（{what}）"
    if st == "unknown":
        return f"有 labmon 之外的 GPU 进程（{what}）" if s.get("mine") else f"有未识别的 GPU 进程（{what}）"
    if st is None:
        return "没探到这台此刻在跑什么"
    return F.lw_text(lw, OP_NAMES.get(kind, kind)) or None


def _need_ack(kind: str, s: dict, st: str | None, what: str, ack, tail: str, lw: dict | None = None) -> None:
    """续租、关机、换人开始前：这一台此刻能不能动。在跑实验、有未识别的 GPU 进程、没探到的、本地盘写了不少（lw 的 risk）的，要页面上勾过「已说好」（ack 里有它）。
    自用主机上 worker 还有任务在跑：续租勾「可以中断」就行；关机、换人不行，先在主机页暂停队列（不中断任务）。"""
    if kind != "renew" and s.get("mine") and st == "job":
        raise P.PlatformError(f"你的 {s['id']} 上 worker 还有任务在跑（{what}）：先在主机页暂停队列；{tail}")
    why = _risk(s, st, what, lw, kind)
    if why and s["id"] not in ack:
        raise P.PlatformError(f"{s['label'] if s.get('mine') else s['holder']}（{s['id']}）现在{why}，选的时候没确认；{tail}")


def find_old(cli: P.Client, s: dict, human) -> tuple[dict | None, str | None, bool]:
    """续租要换掉的作业，返回 (作业, 对不上的原因, 已被平台停掉)。按名字查一次：运行中的找端口 22→port 的那个；
    没有运行中的（平台到期停掉了）就取同池最近一个同名作业，克隆重开（端口在克隆配置上核对）。"""
    js = [j for j in cli.jobs(s["job"]) if j.get("taskName") == s["job"] and j.get("poolName") == s["pool"]]
    run = [j for j in js if _running(j)]
    if run:
        j, _, why = find_job(cli, run, s, human)
        return j, why, False
    if not js:
        return None, f"平台上没有 {s['job']}（{s['pool']}）的作业", False
    return max(js, key=lambda j: parse_time(j["createTime"])), None, True


def stop_job(r: _Run, cli: P.Client, key: str, j: dict, name: str, wait_s: float, poll: float) -> None:
    """停掉平台上的作业 j、等它停下（每 poll 秒按名字查一次列表，最多 wait_s 秒）。续租与换人、关机共用。
    进度记进 op.json 这一台（key）：stop_sent（停止请求已发）→ stopped（确认停下）。"""
    r.human()
    _srv(key, stop_sent=True)
    cli.stop(j["id"])
    _until(lambda: not any(x.get("id") == j["id"] and _running(x) for x in cli.jobs(name)), "停下", wait_s, poll)
    _srv(key, stopped=True)


def submit_job(r: _Run, cli: P.Client, key: str, c: dict, name: str, pool: str, wait_s: float, poll: float) -> dict:
    """提交克隆配置 c、等新作业运行：先记下平台上同名作业的 id，提交后每 poll 秒查一次，新出现的同名、同池、运行中的就是它。
    续租与换人、开机共用。进度：submit_sent（已提交）→ up（已运行，带新作业 id）。
    平台明确没收的（Refused）把 submit_sent 清掉再抛：什么都没提交，再点一次会克隆重开。返回平台上新作业那一行。"""
    before = {x.get("id") for x in cli.jobs(name)}
    _srv(key, submit_sent=True)
    try:
        cli.submit(c)
    except P.Refused:
        _srv(key, submit_sent=False)
        raise
    new = _until(lambda: next((x for x in cli.jobs(name) if x.get("id") not in before and x.get("taskName") == name
                               and x.get("poolName") == pool and _running(x)), None), "运行", wait_s, poll)
    _srv(key, up=True, job=new.get("id"))
    return new


def _note(kind: str, old: dict | None, name: str | None, x: dict) -> str:
    """出错时这一台此刻的状态（给页面与通知），按 op.json 里记的进度标记 x 写。kind：renew / off / swap / open；
    old：停掉的那台（续租里就是它自己；开新的没有）；name：新作业的使用人（自用主机按主机名称叫）。"""
    again = "续租" if kind == "renew" else "开机"
    if x.get("up"):
        if kind == "renew" and old.get("mine"):
            what, fix = "worker", "点「继续」（只等 ssh、拉起 worker，不碰平台）"
        else:
            what, fix = "保活", "等能连上后在它的详情页点「开保活」"
        return (f"{name} 的新作业已在平台上运行，只是 ssh 或 {what}没弄好：{fix}；不要再{again}，"
                + ("否则会把新作业又停掉。" if kind == "renew" else "免得重复开。"))
    if x.get("submit_sent"):
        return f"{name} 的新作业已提交，还没等到运行：到平台上看它的状态；不要再{again}，免得重复开。"
    if x.get("stopped"):
        if kind == "renew":
            return f"{name} 的旧作业已停、新作业没提交成，这台现在不可用：再点一次续租会克隆重开。"
        if old.get("mine"):
            return (f"你的自用主机 {old['id']} 的作业已停、已标成归还"
                    + (f"；{name} 的新作业还没提交：要接着开就在这个池点「+ 开新的」。" if kind in ("swap", "move") else "。"))
        return (f"{old['holder']} 的作业已停，这台已标成关机"
                + ("；新作业还没提交：要接着开就在这台的详情页点「开机」。" if kind in ("swap", "move") else "。"))
    if x.get("stop_sent"):
        return f"{old['label'] if old.get('mine') else old['holder']} 的停止请求已发出，还没确认停下：到平台上看它的状态。"
    return "平台上什么都没动。"


def public_running(cli: P.Client, cfg: dict) -> int:
    return sum(1 for j in cli.jobs() if _running(j) and j.get("poolName") not in cfg["private_pools"])


def keepalive(s: dict, cfg: dict, ssh_fn, poll: float, wait_s: float) -> str:
    """等这一台 ssh 连得上，开保活（fleet.ka_start：已在跑不重开，GPU 上还有别的进程不开，开了以后盯一阵、有实验上来就让出）。
    每次尝试只连一次：连不上隔 poll 秒再试；连上了但保活没开成就报错，不重试。"""
    if not F.ka_cmd(s, cfg):
        _until(lambda: ssh_fn(s, "true", timeout=60)[0] != 255, "连上", wait_s, poll)
        return "ssh 已连上；[fleet] 没写 keepalive_cmd，没开保活"
    res, note = _until(lambda: (r := F.ka_start(s, cfg, "看板", ssh_fn))[0] != "ssh" and r, "连上", wait_s, poll)
    if res == "busy":
        raise P.PlatformError(f"ssh 连上了，GPU 上还有 {note} 个进程，没开保活，免得和它抢 GPU")
    if res != "ok":
        raise P.PlatformError(f"ssh 连上了，{note or '保活没起来'}")
    return "ssh 已连上，保活已在跑" if note.startswith("already") else "ssh 已连上，保活已开"


def mine_up(s: dict, ssh_fn, poll: float, wait_s: float, start_worker=None, ovr=None) -> str:
    """自用主机续租后：等 ssh 连得上（每次只连一次，连不上隔 poll 秒再试），再交给 renew.after 挡住或放行、拉起 worker。"""
    _until(lambda: ssh_fn(s, "true", timeout=60)[0] != 255, "连上", wait_s, poll)
    h = next((x for x in active_hosts() if x["id"] == s["id"]), {})
    end = lease_end(parse_time((boot().get(s["id"]) or {}).get("created")), h.get("lease_days"))   # 没存开机参数的没有租期
    kw = {k: v for k, v in (("start_worker", start_worker), ("ovr", ovr)) if v}
    return R.after(s["id"], s.get("label") or s["id"], end, **kw)


def run_renew(cfg: dict, cli: P.Client, code: str | None, ids: list[str], ack=(), password=P.password,
              pace: float = 3, poll: float = 20, wait_s: float = 900, ssh_poll: float = 30, ssh_wait_s: float = 900,
              probe=_probe, probe_mine=_probe_mine, ssh_fn=None, forget_fn=None, start_worker=None, ovr=None, t: float | None = None) -> None:
    """后台线程：批量续租，组内服务器与自用主机同一条流程。ack：页面上确认过“会中断在跑的实验”的服务器。
    自用主机的最后一步是等 ssh、拉起 worker、解除预备（不开保活：保活归 worker 管）。出错停在那一步，不往下做，也不自动重试。"""
    ssh_fn = ssh_fn or F.ssh
    forget_fn = forget_fn or F.forget
    srv = {x["id"]: x for x in FB.slots(cfg)}
    r = _Run(pace)
    cur, done = None, []
    try:
        url, user = platform_conf(cfg)
        login(r, cli, code, user, password)
        plan, notes = [], []
        for sid in ids:
            s = srv[sid]
            st, what = (probe_mine if s.get("mine") else probe)(sid)
            _need_ack("renew", s, st, what, ack, "哪台都没动", _lw(s))
            r.human()
            j, why, stopped = find_old(cli, s, r.human)
            if j is None:
                raise P.PlatformError(f"{s['holder']}（{sid}）：{why}；哪台都没动")
            r.human()
            c = cli.clone(j["id"])
            if (c or {}).get("taskName") != s["job"] or not P.clone_port_ok(c, s["port"]):
                raise P.PlatformError(f"{s['holder']}（{sid}）克隆出的配置对不上（作业名 {(c or {}).get('taskName')}，"
                                      f"端口要 22→{s['port']}）；哪台都没动")
            plan.append((s, j, c, stopped))
            notes.append(f"{s['holder']}：" + ("已被平台停掉，克隆重开" if stopped else
                                                {"down": "连不上", None: "没探到"}.get(st, F.NAMES.get(st, st))))
        r.step("；".join(notes))
        for s, j, c, stopped in plan:
            cur, sid, name = s, s["id"], s["job"]
            if s.get("mine"):
                R.begin(sid)                         # 停旧作业前记下本地文件依赖，续租后据此决定放不放行
            if stopped:
                if s["pool"] not in cfg["private_pools"] and cfg.get("public_max") is not None:
                    r.human()
                    n = public_running(cli, cfg)
                    if n >= cfg["public_max"]:
                        raise P.PlatformError(f"公用池已开 {n} 台（全组上限 {cfg['public_max']}），再开 {name} 会超")
                _srv(sid, stopped=True)
                r.step("作业已被平台停掉，跳过")
            else:
                stop_job(r, cli, sid, j, name, wait_s, poll)
                r.step(f"{_created(j)} 创建的已停")
            r.human()
            new = submit_job(r, cli, sid, c, name, s["pool"], wait_s, poll)
            t1 = time.time() if t is None else t
            F.edit_state("boot.json", lambda b, s=s, new=new: b.setdefault(s["id"], {}).update(
                holder=s["holder"], job=s["job"], job_id=new.get("id"), created=new.get("createTime"), clone=c,
                clone_job=new.get("id"), src="续租", t=round(t1)))
            _clear_stopped(sid)
            F._event("fleet_renew", sid, f"{s['holder']}（{sid}）续租：新作业 {_created(new)} 创建", t1, s["holder"])
            r.step(f"{_created(new)} 创建")
            forget_fn(s)
            if s.get("mine"):
                note = mine_up(s, ssh_fn, ssh_poll, ssh_wait_s, start_worker, ovr)
            else:
                note = keepalive(s, cfg, ssh_fn, ssh_poll, ssh_wait_s)
                probe(sid)
                from . import overview as O
                O.clear_relaunch(sid)
            _srv(sid, ka=True)
            done.append(s["label"] if s.get("mine") else s["holder"])
            if s is plan[-1][0]:
                r.done(note)
            else:
                r.step(note)
        cur = None
        notify([f"续租完成：{'、'.join(done)}"])
    except Exception as x:  # noqa: BLE001 — 停在出错的那一步，页面写明这一台此刻的状态
        note = _note("renew", cur, (cur["label"] if cur.get("mine") else cur["holder"]) if cur else None,
                     op().get("servers", {}).get(cur["id"], {}) if cur else {})
        rest = [srv[i]["holder"] for i in ids if srv[i]["holder"] not in done and srv[i].get("label") not in done
                and (cur is None or i != cur["id"])]
        if done:
            note += f"已续完：{'、'.join(done)}。"
        if cur and rest:
            note += f"还没动：{'、'.join(rest)}。"
        steps = op().get("steps") or []
        r.fail(x, note)
        notify([f"续租停在「{steps[r.i]['name'] if r.i < len(steps) else '?'}」：{x}。{note}"])


def run_ka(cfg: dict, cli, code, ids: list[str], ssh_poll: float = 30, ssh_wait_s: float = 300,
           probe=_probe, ssh_fn=None, forget_fn=None, start_worker=None, ovr=None) -> None:
    """后台线程：给选的服务器开保活，不碰平台。上次探测报主机密钥变了的，先清掉旧密钥；先探一次，在跑实验或有未识别进程的不开。
    自用主机（续租停在最后一步时的「继续」）：等 ssh、拉起 worker、解除预备。"""
    ssh_fn = ssh_fn or F.ssh
    forget_fn = forget_fn or F.forget
    srv = {x["id"]: x for x in FB.slots(cfg)}
    sts = F._load_state()
    r = _Run(0)
    try:
        r.mark("run")
        for k, sid in enumerate(ids):
            s = srv[sid]
            if s.get("mine"):
                note = mine_up(s, ssh_fn, ssh_poll, ssh_wait_s, start_worker, ovr)
            else:
                if "主机密钥" in str((sts.get(sid) or {}).get("error")):
                    forget_fn(s)
                st, what = probe(sid)
                if st in ("job", "unknown"):
                    raise P.PlatformError(f"{s['holder']}（{sid}）现在{F.NAMES[st]}（{what}），不开保活，免得和它抢 GPU")
                note = keepalive(s, cfg, ssh_fn, ssh_poll, ssh_wait_s)
                probe(sid)
            _srv(sid, ka=True)
            if k == len(ids) - 1:
                r.done(note)
            else:
                r.step(note)
    except Exception as x:  # noqa: BLE001
        r.fail(x)


def run_borrow(cfg: dict, cli, code, ids: list[str], until: float | None = None, projects=None, begin=None, light: bool = False,
               away: bool = False, account: str | None = None) -> None:
    """后台线程：借来用（borrow.begin），每做完一步写进 op.json，页面上和别的操作一样显示进度；借不了就停在那一步。"""
    from . import borrow as B                     # borrow 在上层 import 本模块，这里只能延迟 import
    r = _Run(0)
    try:
        r.mark("run")
        r.done((begin or B.begin)(ids[0], until, projects, step=r.step, light=light, away=away, account=account))
    except Exception as x:  # noqa: BLE001
        r.fail(x)


def _quota(jobs: list[dict], cfg: dict, pool: str) -> tuple[int, int | None, bool]:
    """平台上与这个池同类（专用或公用）的运行中作业数、上限、是否专用。"""
    priv = FB.is_private(cfg, pool)
    n = sum(1 for j in jobs if _running(j) and FB.is_private(cfg, j.get("poolName") or "") == priv)
    return n, cfg.get("private_max") if priv else cfg.get("public_max"), priv


def _unlisted_ports(cli: P.Client, cfg: dict, jobs: list[dict], human, note=lambda m: None) -> list[tuple[str, int]]:
    """平台上运行中、看板没登记的作业占的端口（各取一次详情，每次隔几秒）：挑端口时避开。note(文字)：进度写给页面看。"""
    known = {e.get("job_id") for e in boot().values()}
    own = {h.get("platform_job") for h in active_hosts() if h.get("platform_job")}
    todo = [j for j in jobs if _running(j) and j.get("id") not in known and j.get("taskName") not in own]
    out = []
    for k, j in enumerate(todo, 1):
        note(f"读第 {k}/{len(todo)} 个未登记作业（{j.get('taskName')}）的端口")
        human()
        for role in cli.roles(j["id"]):
            d = from_detail({"workTaskRoleVOList": [role]})
            node = FB.ext_host(d.get("ip"), cfg)
            out += [(node, int(v)) for v in (d.get("ports") or {}).values() if node and str(v).isdigit()]
    return out


def _old_job(r: _Run, cli: P.Client, kind: str, old: dict, ack, probe, probe_mine) -> tuple[dict | None, list[dict], str | None]:
    """关机、换人：核对要停掉的那台。先探一次（按 _need_ack 的规则，在跑、未识别、没探到的要勾过确认），再在平台上找到它运行中的作业。
    返回 (作业, 角色, 探到的状态)。关机时连不上、平台上也没有它运行中的作业（多半已被平台停掉）：作业 None，只在看板上标关机。"""
    st, what = (probe_mine if old.get("mine") else probe)(old["id"])
    _need_ack(kind, old, st, what, ack, "什么都没动", _lw(old))
    r.human()
    run = [j for j in cli.jobs(old["job"]) if j.get("taskName") == old["job"] and j.get("poolName") == old["pool"] and _running(j)]
    if not run and kind == "off" and st == "down" and not old.get("mine"):
        return None, [], st
    if not run:
        raise P.PlatformError(f"平台上没有运行中的 {old['job']}（{old['pool']}）；什么都没动"
                              + ("：连不上的服务器先点「关机」标为已关机，再开机" if st == "down" and kind in ("swap", "move") else ""))
    oj, roles, why = find_job(cli, run, old, r.human)
    if oj is None:
        raise P.PlatformError(f"{why}；什么都没动")
    return oj, roles, st


def _new_job(r: _Run, cli: P.Client, cfg: dict, old: dict | None, oj: dict | None, prev: dict | None, pool: str | None, name: str, tcp,
             *, kind: str = "swap", spec: str | None = None) -> dict:
    """换人、换节点、开机：拼好要提交的新作业并逐项核对（不提交）。模板：换人/换节点克隆旧作业，开机用这个池的模板作业；
    数配额、定作业名（同名在跑就加后缀）、挑端口（接替的那台的端口优先，没登记的运行中作业占的也避开）、核对规格、挂载卷、镜像。
    返回 {job 作业名, pool, port, c 克隆配置, nodes 池的节点, spec, spec_name, note 核对步骤的说明}；有一项不对就抛错，什么都没动。"""
    pl = prev["pool"] if prev else pool
    if old:
        r.human()
        base = cli.clone(oj["id"])
        if not base or not P.clone_port_ok(base, old["port"]):
            raise P.PlatformError(f"克隆出的配置端口不是 22→{old['port']}（作业 {oj['id']}）；什么都没动")
    else:
        base = FB.ref(pl, prev["id"] if prev else None)[1]       # 重开已关机的服务器：用它自己的克隆配置
        if base is None:
            raise P.PlatformError(f"开机参数里没有 {pl} 的克隆配置做模板：先与平台核对；什么都没动")
    orig_spec = FB.summary(base)["spec"]
    spec = (spec or "").strip() or None
    if kind == "move":
        if not spec:
            raise P.PlatformError("换节点要选目标规格；什么都没动")
        if orig_spec and spec == orig_spec:
            raise P.PlatformError("目标规格与当前规格相同：原地重启请用「续租」；什么都没动")
    target_spec = spec or orig_spec
    pp = FB.people(cfg).get(name) or {}
    r.human()
    jobs = cli.jobs()
    n, lim, priv = _quota(jobs, cfg, pl)
    if lim is not None and n - (1 if old else 0) + 1 > lim:
        raise P.PlatformError(f"{'专用' if priv else '公用'}池运行中 {n} 台（上限 {lim}），再开会超；什么都没动")
    job = FB.job_name(name, FB.taken_jobs(cfg, old["job"] if old else None,
                                          [j.get("taskName") for j in jobs if _running(j) and (not oj or j.get("id") != oj.get("id"))]))
    nodes = FB.pool_nodes(cfg, pl)
    used = FB.used_ports(cfg, _unlisted_ports(cli, cfg, jobs, r.human, lambda m: r.mark("run", m)))
    port, why = FB.pick_port(cfg, nodes, used, int(prev["port"]) if prev else FB.back_port(cfg, pl, name), [(old["ssh"], old["port"])] if old else [], tcp,
                             what="原端口" if prev else "上次的端口")
    img, cmd, _ = FB.settings_of(pp)
    c = FB.compose(base, job, img, cmd, port, spec=target_spec)
    same_spec = bool(orig_spec and target_spec == orig_spec)
    checks = FB.check(cli, c, r.human, freed=bool(old and same_spec))
    bad = [x for x in checks if x["ok"] is False]
    if bad:
        raise P.PlatformError("；".join(f"{x['item']}：{x['note']}" for x in bad) + "；什么都没动")
    sp_note = next((x["note"] for x in checks if x["item"] == "规格"), None)
    sp_label = sp_note.split("，", 1)[0] if sp_note and "，" in sp_note else FB.spec_name(FB.pool_specs(pl), target_spec)
    return {"job": job, "pool": pl, "port": port, "c": c, "nodes": nodes, "spec": target_spec, "spec_name": sp_label,
            "note": f"开 {name}" + (f"（作业名 {job}）" if job != name else "") + f"：{FB.summary(c)['image']}"
            + (f"，规格 {sp_label}" if sp_label else "") + f"，端口 {port}（{why}）"}


def _mark_off(hosts: dict, old: dict, oj: dict | None, t1: float) -> None:
    """旧作业已停（或平台上已没有它）：自用主机在主机清单里标成已归还（看板不再连它、不起 worker，之后可在别处开回来）；
    组内服务器在 boot.json 记 off（之后不探、不提醒租期、不算配额）。记 fleet_off。"""
    sid = old["id"]
    # 上次核对存的运行中作业里删掉它：预览（taken_jobs）不再当它的名字占着。停了的按作业 id；平台上已没有它（oj 为空）的，
    # _old_job 刚读的列表里这个池已没有这个名字在运行，存着的同名同池的行都是过时的
    gone = (lambda x: x.get("id") == oj.get("id")) if oj else (lambda x: x.get("name") == old["job"] and x.get("pool") == old["pool"])
    F.edit_state("platform.json", lambda d: d.get("running") is not None and d.update(running=[x for x in d["running"] if not gone(x)]))
    if old.get("mine"):
        hosts["return_mine"](sid)
    else:
        F.edit_state("boot.json", lambda b: b.setdefault(sid, {}).update(off={"t": round(t1), "holder": old["holder"]},
                                                                     **({"job_id": oj.get("id"), "job": old["job"]} if oj else {})))
    F._event("fleet_off", sid, f"{old['holder']}（{sid}）关机：" + (f"{_created(oj)} 创建的作业已停" if oj else "平台上已没有它的作业，只在看板上标为已关机")
             + ("，自用主机已标成归还" if old.get("mine") else ""), t1, old["holder"])


def _adopt(cli: P.Client, cfg: dict, kind: str, prev: dict | None, roles: list[dict], plan: dict, nj: dict, name: str, hosts: dict, t1: float, to_me: bool) -> tuple[dict, dict]:
    """新作业已在运行：读它的详情定下 ssh 目标与端口、这台的 id，改主机清单（换人改 holder，开新的加一块；换给自己就把这台移成自用主机），
    写开机参数，记 fleet_boot，连接信息写进 op.json。返回 (新服务器, 连接信息)。"""
    pl = plan["pool"]
    d = from_detail({"workTaskRoleVOList": cli.roles(nj["id"]), "poolName": pl, "createTime": nj.get("createTime")})
    prev_ip = None                                 # 接替的那台原来的内网 IP：新作业在同一台上就照用它的 ssh 目标
    if prev:
        prev_ip = (roles[0].get("ipAddress") if roles else None) or (boot().get(prev["id"]) or {}).get("ip")
    node = FB.ext_host(d.get("ip"), cfg) or (prev["ssh"] if prev and d.get("ip") and d.get("ip") == prev_ip else None) \
        or (plan["nodes"][0] if len(plan["nodes"]) == 1 and (not d.get("ip") or not prev_ip or d.get("ip") == prev_ip) else None)
    if not node:
        raise P.PlatformError(f"新作业在 {d.get('ip')} 上，不知道本机怎么连它：在主机清单里登记这一台")
    nport = int((d.get("ports") or {}).get("22") or plan["port"])
    nsid = next((x["id"] for x in cfg["servers"] if x["ssh"] == node and int(x["port"]) == nport), None) or FB.sid_of(node, nport)
    fleet_ids, job = {x["id"] for x in cfg["servers"]}, plan["job"]
    if to_me:                                      # 换给自己 / 自用主机换节点：组内服务器里这台去掉，主机清单加一台自用主机
        if prev and not prev.get("mine") and prev["id"] in fleet_ids:
            hosts["remove"](prev["id"])
        if nsid in fleet_ids and (not prev or nsid != prev["id"]):
            hosts["remove"](nsid)
        nsid = hosts["add_mine"](nsid, job, node, nport, pl, f"{nsid}（自用）", name)
    elif prev and prev["id"] == nsid:
        if (prev["holder"], prev["job"]) != (name, job):
            hosts["holder"](nsid, name, job)
    else:
        if nsid in fleet_ids:
            hosts["holder"](nsid, name, job)
        else:
            hosts["add"](nsid, name, node, nport, pl, job)
        if prev and not prev.get("mine"):
            hosts["remove"](prev["id"])
    new = {"id": nsid, "holder": name, "job": job, "ssh": node, "port": nport, "pool": pl}

    def upd(b):
        if prev and prev["id"] != nsid:
            b.pop(prev["id"], None)
        x = b.setdefault(nsid, {})
        x.pop("off", None)
        x.update(d, holder=name, job=job, job_id=nj.get("id"), created=nj.get("createTime"), clone=plan["c"], clone_job=nj.get("id"),
                 clone_t=round(t1), src={"swap": "换人", "move": "换节点", "open": "开机"}[kind], t=round(t1))
    F.edit_state("boot.json", upd)
    _clear_stopped(nsid)
    if kind == "move":
        was = f"，原节点 {prev['id']}" if prev and prev["id"] != nsid else ""
        msg = f"{name}（{nsid}）换节点：新作业 {_created(nj)} 创建{was}"
    else:
        was = f"，接替 {prev['holder']}（{prev['id']}）" if prev and (prev["holder"] != name or prev["id"] != nsid) else ""
        msg = f"{name}（{nsid}）开机：新作业 {_created(nj)} 创建{was}"
    F._event("fleet_boot", nsid, msg + (f"，作业名 {job}" if job != name else "") + ("，接成自用主机" if to_me else ""), t1, name)
    conn = {"sid": nsid, "holder": name, "cmd": f"ssh -p {nport} {node}", "image": FB.summary(plan["c"])["image"],
            "spec": plan.get("spec_name") or FB.summary(plan["c"])["spec"], "mine": to_me}
    F.edit_state("op.json", lambda st: st.update(conn=conn))
    return new, conn


def _mine_host_spec(hid: str, tgt: dict) -> dict:
    """接成自用主机后的主机配置（含 worker_home 与 python）：优先从主机清单读，测试打桩未改清单时照模板补齐。"""
    found = next((h for h in C.active_hosts() if str(h["id"]) == hid), None)
    if found is not None:
        return found
    tpl = C._guest_tpl(C.all_hosts())
    return C.host_defaults({**{k: tpl[k] for k in ("python", "data_root", "worker_home", "ka_bin") if k in tpl}, **tgt})


def run_swap(cfg: dict, cli: P.Client, code: str | None, ids=(), kind: str = "swap", person: str | None = None,
             pool: str | None = None, spec: str | None = None, ack=(), password=P.password,
             pace: float = 3, poll: float = 20, fast: float = 6, wait_s: float = 900, ssh_poll: float = 30, ssh_wait_s: float = 900,
             probe=_probe, probe_mine=_probe_mine, ssh_fn=None, forget_fn=None, tcp=None, hosts=None, t: float | None = None,
             account: str | None = None, clear_ovr=None) -> None:
    """后台线程：关机（off）、换人（swap：同一个池，停旧作业后立刻开新的，沿用端口，可选换规格）、
    换节点（move：同人同池，停旧作业后立刻按新规格开新的，端口按现有规则挑）、开机（open：重开一台已关机的，或在一个池开新的，可选规格）。
    登录 → 核对（_old_job：探旧服务器、在平台上找到旧作业；_new_job：拼好新作业并逐项核对），有一项不对就什么都不动 →
    停旧作业（stop_job，停旧到提交新之间每 fast 秒查一次，空档只有十几秒；停下就记关机或归还，_mark_off）→ 关机到此为止；
    提交新作业、等运行（submit_job）→ 登记新服务器（_adopt）→ 等 ssh、开保活 → 给出连接命令。出错停在那一步，不自动重试。
    旧服务器可以是自用主机：worker 有任务在跑就拒绝（先在主机页暂停队列），停了作业后标成已归还；
    新的使用人是自己时（或自用主机换节点），开好的服务器接成自用主机（也先开保活，worker 由看板下一轮采集拉起），并清掉同 id 遗留的借用指令（guest、target=borrow 的 deadline）。
    关机时连不上、平台上也没有它运行中的作业：只在看板上标为已关机。
    hosts：改主机清单的函数（holder、add、remove 管组内服务器，add_mine、return_mine 管自用主机），测试替换用。"""
    ssh_fn, forget_fn, tcp = ssh_fn or F.ssh, forget_fn or F.forget, tcp or FB.tcp_open
    ovr_run = (lambda h, cmd: ssh_fn(h, cmd)[:2]) if ssh_fn is not F.ssh else None
    ovr_fn = clear_ovr or clear_overrides
    hosts = hosts or {"holder": C.set_fleet_holder, "add": C.add_fleet_server, "remove": C.remove_fleet_server,
                      "add_mine": lambda *a: C.add_mine_host(*a)[1],
                      "return_mine": lambda hid: C.set_returned(hid, fmt_time(time.time(), year=True))}
    srv = {x["id"]: x for x in FB.slots(cfg)}
    sid0 = ids[0] if ids else None
    old = srv[sid0] if sid0 and kind in ("off", "swap", "move") else None      # 要停掉的服务器
    prev = srv[sid0] if sid0 else None                                         # 新作业接替的服务器（换人/换节点：旧服务器；开机：已关机的服务器）
    key = sid0 or "new"                                                        # op.json 里这一台进度的键
    name = old["holder"] if kind == "move" and old else (person or (prev or {}).get("holder"))
    r = _Run(pace)
    try:
        url, user = platform_conf(cfg)
        login(r, cli, code, user, password)
        notes, oj, roles, plan = [], None, [], None
        if old:
            oj, roles, st = _old_job(r, cli, kind, old, ack, probe, probe_mine)
            if oj is None:
                r.step(f"连不上，平台上也没有运行中的 {old['job']}（{old['pool']}）")
                _mark_off(hosts, old, None, time.time() if t is None else t)
                r.done("平台上已没有它的作业，只在看板上标为已关机")
                return
            notes.append(f"{old['holder']}：{_created(oj)} 创建，现在{F.NAMES.get(st, '没探到')}")
        to_me = False
        if kind != "off":
            if kind == "move":
                to_me = bool(old.get("mine"))
            else:
                actor, error = person_of(account)
                if error:
                    raise P.PlatformError(error)
                to_me = bool(name and actor and name.casefold() == actor.casefold())
                if to_me:
                    name = actor
            plan = _new_job(r, cli, cfg, old, oj, prev, pool, name, tcp, kind=kind, spec=spec)
            notes.append(plan["note"])
        r.step("；".join(notes))
        if old:
            stop_job(r, cli, key, oj, old["job"], wait_s, fast)
            _mark_off(hosts, old, oj, time.time() if t is None else t)
            if kind == "off":
                r.done("已停；自用主机已标成归还，看板不再连它" if old.get("mine") else "已停；这台不再探，也不提醒租期")
                notify([f"{old['holder']}（{sid0}）关机完成"])
                return
            r.step(f"{_created(oj)} 创建的已停")
        else:
            r.human()
        nj = submit_job(r, cli, key, plan["c"], plan["job"], plan["pool"], wait_s, poll)
        r.human()
        new, conn = _adopt(cli, cfg, kind, prev, roles, plan, nj, name, hosts, time.time() if t is None else t, to_me)
        r.step(f"{_created(nj)} 创建，{new['id']}" + ("（自用主机）" if conn["mine"] else ""))
        # 接成自用主机的用 fleetboot.mine 里那一项（与续租同口径）：它带着拉起 worker 用的那份 known_hosts。
        # forget 删掉里面这个 host:port 的旧条目（同一 ssh 与端口开回来时旧容器的密钥还在，accept-new 会拒连），
        # 等 ssh 的那次连接把新密钥记回那份（#109：记进状态目录的话，worker 复用长连接不核对，实验会话裸 ssh 被拒）
        tgt = next((s for s in FB.mine(cfg) if s["id"] == new["id"]), new) if conn["mine"] else new
        ovr_cleared, ovr_msg, mine_h = False, "", None
        if conn["mine"]:
            mine_h = _mine_host_spec(new["id"], tgt)
            ovr_cleared, ovr_msg = ovr_fn(new["id"], home=mine_h["worker_home"], borrow_only=True, skip={new["id"]}, ssh_fn=ovr_run)
        forget_fn(tgt)
        note = keepalive(tgt, cfg, ssh_fn, ssh_poll, ssh_wait_s)
        _srv(key, ka=True)
        if conn["mine"]:
            if not ovr_cleared:
                ovr_cleared, ovr_msg = ovr_fn(new["id"], home=mine_h["worker_home"], borrow_only=True, target=mine_h, ssh_fn=ovr_run)
            if ovr_cleared and ovr_msg.startswith("已清"):
                note += f"；{ovr_msg}"
                log(f"接成自用主机 {new['id']}：{ovr_msg}")
            elif not ovr_cleared:
                log(f"接成自用主机 {new['id']}：未能清理遗留借用指令：{ovr_msg}")
            note += "；看板下一轮采集时拉起 worker"
        else:
            probe(new["id"])
            from . import overview as O
            O.clear_relaunch(new["id"])
        r.done(note)
        notify([f"{name}（{new['id']}）{'换节点' if kind == 'move' else '开机'}完成：{conn['cmd']}"])
    except Exception as x:  # noqa: BLE001 — 停在出错的那一步，页面写明这一台此刻的状态
        note = _note(kind, old, name, op().get("servers", {}).get(key, {}))
        r.fail(x, note)
        notify([f"{name or key} {OP_NAMES[kind]}停下：{x}"])


# ---------- 给页面与命令行 ----------
def view(cfg: dict | None = None) -> dict:
    """组内服务器页要的：开机参数（克隆配置只给摘要）、上次核对的结果、当前操作的进度、会话状态，
    以及开机向导要的人、池（模板作业、上次读回的规格与节点）。"""
    b = {}
    for sid, e in boot().items():
        c = e.get("clone") or {}
        role = (c.get("cloneTaskRoleVOList") or [{}])[0]
        b[sid] = {**{k: v for k, v in e.items() if k != "clone"}, "has_clone": bool(c), "spec": role.get("specificationId"),
                  "run_cmd": role.get("runCmd"), "volumes": [f"{v.get('storagePath')} → {v.get('mountPath')}（{v.get('perm')}）"
                                                             for v in c.get("cloneStorageVolumeVOList") or []]}
    try:
        cfg = cfg if cfg is not None else F.conf()
    except C.ConfigError:
        cfg = None
    info = FB.pools_info()
    pools = {}
    for name in FB.pool_names(cfg) if cfg else []:
        p, (_, base, rsid) = (info.get("pools") or {}).get(name) or {}, FB.ref(name)
        pools[name] = {"private": FB.is_private(cfg, name), "ref": rsid, "spec": FB.summary(base)["spec"] if base else None, "hosts": p.get("hosts") or [],
                       "specs": p.get("specs") or [], "nodes": FB.pool_nodes(cfg, name)}
    return {"t": round(time.time(), 1), "boot": b, "platform": platform_state() or None, "op": op() or None, "session": session(), "names": OP_NAMES,
            "guard": F.guard_conf(cfg),
            "people": FB.people(cfg) if cfg else {}, "defaults": FB.defaults(), "pools": pools, "pools_t": info.get("t"), "volumes": info.get("volumes"),
            "port_range": list((cfg or {}).get("port_range") or FB.PORT_RANGE)}


def show_boot(sid: str | None = None) -> int:
    b = boot()
    if not b:
        print("[labmon] 还没有开机参数：在看板组内服务器页点「与平台核对」，或 labmon fleet import <id> <Json 文件>")
        return 1
    for k, e in sorted(b.items()):
        if sid and k != sid:
            continue
        c = e.get("clone") or {}
        role = (c.get("cloneTaskRoleVOList") or [{}])[0]
        print(f"{e.get('holder', '?'):<5} {k:<12} {e.get('pool', '—')}  镜像 {e.get('image', '—')}  端口 {e.get('ports', {})}"
              f"  {e.get('cpu', '—')} 核 / {e.get('memory', '—')} GB / {e.get('disk', '—')} GB / {e.get('gpu', '—')} 卡"
              f"  作业 {fmt_time(parse_time(e['created'])) if e.get('created') else '—'} 创建  来源 {e.get('src')} {fmt_time(e.get('t', 0))}")
        if sid:
            print(json.dumps({k2: v for k2, v in e.items() if k2 != "clone"}, ensure_ascii=False, indent=2))
            if c:
                print("克隆配置（开机时提交用）：")
                print(json.dumps(c, ensure_ascii=False, indent=2))
        elif role.get("runCmd"):
            print(f"      启动命令 {role['runCmd']}")
    return 0
