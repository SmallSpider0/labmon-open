"""labmon wait 在看板服务这边的判定（#162：以前在命令行进程里读本机文件，现在由 POST /api/wait 在服务端做，逻辑不变）。

- wait_check：读一次项目快照与全局快照的 wake，返回新告警；“报过没有”记在项目输出目录的 .labmon/wait_state.json（同一项目几个会话共用这一份）。
- JobWatch：labmon wait --job 等某些任务跑完（#135）；内部状态（开始等的时刻、见过它在跑、确认了几份快照……）由 state() 交给客户端、restore() 带回，
  服务端不记会话。
- wait：长轮询（#157 方案 C）：有新告警、或等的任务都有了结果就回，否则最多挂 HOLD_MAX 秒；挂着时只在相关文件变了、或离上次判过 POLL_S 秒时再判。
  同时挂着的封顶 SLOTS 个（多了抛 Busy，接口回 503、客户端退避），一个普通账号封顶 ACCOUNT_SLOTS 个（多了抛 AccountBusy，回 429，#18）。请求号 rid：同一个号 RID_KEEP_S 秒内再来回上次那份（回话丢在半路时客户端带同一个号重试，
  已记成报过的告警不会漏）。
- 一个项目只让一个会话等（#142，_claim）：客户端带会话标识，wait_state.json 的 holder 记着此刻等的会话；别的会话来回 409、不领告警，换会话用 --take。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import deploy as DP
from . import projects as PJ
from .config import ConfigError, fmt_dur, fmt_time, stale_limit
from .overview import USAGE_KEEP_S
from .state import file_sig, load_json, load_rows, save_json, snapshot, state_dir, usage_file, usage_hosts
from .worker.pure import job_key  # noqa: F401 — 与 worker 共用唯一实现

JOB_CONFIRM = 2       # wait --job：没见过它在跑、排队就已结束、找不到、不会再跑的，要连着这么多份开始等之后写出的快照都这样才报（#135）
JOB_BAD_RC = 4        # wait --job 都有了结果、但不全是成功（失败、作废、找不到、不会再跑）的退出码；1 是新告警，2、3 已被参数错、配置错占着
HOLD_MAX = 25         # 长轮询一次最多挂这么久（秒）：小于 TX 上 nginx 的 proxy_read_timeout（60 秒）
POLL_S = 30           # 挂着时文件没变也至少这么久再判一次（collector_stale 按时间判，同以前每 30 秒读一次）
SLOTS = 32            # 同时挂着的长轮询最多几个：每个占一个处理线程
ACCOUNT_SLOTS = 8     # 一个普通账号同时最多挂几个（#18）：一个人占不满 SLOTS，别人照样能等；管理员只受 SLOTS 限
RID_KEEP_S = 120      # 请求号记多久
REWAKE_S = 6 * 3600   # 报过的告警消失后这么久内再出现不再叫醒（#88：门槛两侧来回、清单回池重新计时、主机掉线又回来，会话不用再读一遍）；
                      # 同一处的第二件事靠 key 带“这一次”（开跑时刻、PID、归还时刻、借用起点、指令时刻、这一段的起点，#129）分开，不在这里另判
SAME_KIND = ("idle_regs:",)   # key 末段是代数（条数变了加一）的告警：去掉末段比，代数变了不算新告警（#88）
EACH_TIME = ("failed:",)      # 每次出现都是新的一次、不受 REWAKE_S 挡：删了 .failed 重跑又失败，会话要再处理
WAIT_GONE_S = 15 * 60         # 项目还有活、这么久没有 wait 来等：会话的 wait 断了，告诉用户（#141）；等这个项目的会话这么久没来就让出（#142）
DUP_SHOW_S = 3600             # 别的会话被拦下后，这么久内给用户一条 wait_dup（#142）


def _kind(k: str) -> str:
    """比“报没报过”用的 key：SAME_KIND 的去掉末段（代数），其余原样。"""
    return k.rsplit(":", 1)[0] if k.startswith(SAME_KIND) else k


def _new_alerts(sf: Path, current: dict, t: float | None = None) -> list[str]:
    """与已报的告警比，返回新出现的 key 并记账（wait_state.json：reported 是此刻还在、报过的；last 是报过的各 key（按 _kind）最后一次在场的时刻）。
    条件消失的告警从 reported 去掉，REWAKE_S 内再出现（last 里还在）不再报、照样记成报过；过了 REWAKE_S 再出现才再报（#88）。EACH_TIME 的消失再出现照报。"""
    t = time.time() if t is None else t
    st = load_json(sf, {})
    last = {k: v for k, v in (st.get("last") or {}).items() if t - v < REWAKE_S}
    reported = set(st.get("reported", [])) & set(current)
    seen = {_kind(k) for k in reported} | set(last)
    new = [k for k in current if k not in reported and (_kind(k) not in seen or k.startswith(EACH_TIME))]
    keep = reported | set(current)                    # 此刻在场的都算报过：新报的与 REWAKE_S 内再出现的
    last.update({_kind(k): t for k in keep})
    save_json(sf, {**st, "reported": sorted(keep), "last": last, "t": t, "wait_t": t})     # wait_t 只在这里写（wait_check 判一次）：gone_since 看它；holder、refused（_claim）照留
    return new


def state_file(out: Path) -> Path:
    """“报过没有”与最后一次来等的时刻（wait_t；t 是记账的时刻，labmon check 也会改）记在这里：项目输出目录的 .labmon/wait_state.json，同一项目几个会话共用。"""
    return out / ".labmon" / "wait_state.json"


def gone_since(out: Path, t: float | None = None) -> float | None:
    """这个项目的 wait 断了（#141）：返回最后一次来等的时刻，没断为 None。
    wait 挂着时每 POLL_S 秒判一次 wait_check、写一次 wait_state.json 的 wait_t，所以它满 WAIT_GONE_S 没动就是没人在等。
    只认 wait_t：t 是记账的时刻，labmon check 列出页面改动时也会改（check_told），不是 wait 来过的证据。
    从没挂过 wait 的项目（没有这个文件）不算；只在看板服务里判（deploy.RUNNING 的 started：labmon collect 命令行没有），
    看板刚重启时从启动时刻起算，等会话的 wait 连回来。项目有没有活由调用方（collect._wait_gone）看。"""
    t = time.time() if t is None else t
    started = DP.RUNNING.get("started")
    last = load_json(state_file(out), {}).get("wait_t")
    if not started or not isinstance(last, (int, float)) or t - max(last, started) < WAIT_GONE_S:
        return None
    return last


def _claim(pid: str, sf: Path, sess: str | None, who: str | None, take: bool, mine: bool, t: float) -> None:
    """一个项目只让一个会话等（#142）：wait_state.json 的 holder 是此刻等这个项目的会话 {s: 会话标识, a: 账号, t0: 起始, t: 最后一次来等}。
    take 接手时记下被接手的会话（from），它再来时同样挡下、说法是“改由另一个会话等了”，不记 refused。
    来的就是 holder、没有 holder、holder 满 WAIT_GONE_S 没来（与 gone_since 同一门槛：会话不在了）或带了 take（labmon wait --take）：它成为或仍是 holder；
    否则抛 projects.Refused(409)，命令行以 3 退出、不领告警（在 wait_check 之前挡）。mine：这一次请求前几轮已是 holder（被 --take 接手了的说法不同）。
    被拦下的记进 refused {a, t}，项目快照据此给用户一条 wait_dup（dup_refused）。sess 为空（旧客户端、取不出会话标识）不挡也不占。调用方持着登记锁。"""
    if not sess:
        return
    st = load_json(sf, {})
    h = st.get("holder") or {}
    if h.get("s") and h["s"] != sess and t - (h.get("t") or 0) < WAIT_GONE_S and not take:
        who_s = f"账号 {h['a']}，" if h.get("a") else ""
        if mine or h.get("from") == sess:
            raise PJ.Refused(409, f"项目 {pid} 改由另一个会话等了（{who_s}{fmt_time(h['t0'])} 用 labmon wait --take 接手）：本会话别再挂 labmon wait，"
                                  "要看状态用 labmon status")
        save_json(sf, {**st, "refused": {"a": who, "t": t}})
        raise PJ.Refused(409, f"项目 {pid} 已有另一个会话在挂 labmon wait（{who_s}{fmt_time(h['t0'])} 起，最后一次来等 {fmt_time(h['t'])}）。"
                              "一个项目只让一个会话等，告警才不会被分走：本会话别再挂 wait，要看状态用 labmon status。"
                              "确认那边已经停了（例如是本会话续接前挂的），或要改由本会话负责这个项目的实验，用 labmon wait --take 接手（那边下一轮就退出）；"
                              f"那边 {fmt_time(h['t'] + WAIT_GONE_S)} 前没再来等也会自动让出")
    alive = t - (h.get("t") or 0) < WAIT_GONE_S
    same = h.get("s") == sess and alive
    took = {"from": h["s"]} if take and h.get("s") and h["s"] != sess and alive else {}      # 被接手的那个会话再来时说“改由别的会话等了”
    save_json(sf, {**st, "holder": {"s": sess, "a": who, "t0": h["t0"] if same else t, "t": t, **(took or {k: h[k] for k in ("from",) if same and k in h})}})


def dup_refused(out: Path, t: float | None = None) -> dict | None:
    """别的会话想挂 wait 被拦下了（#142，_claim 记的 refused）：DUP_SHOW_S 内、holder 还在时返回 {a: 被拦的账号, t: 时刻}，否则 None。"""
    t = time.time() if t is None else t
    st = load_json(state_file(out), {})
    r, h = st.get("refused") or {}, st.get("holder") or {}
    if not r.get("t") or t - r["t"] >= DUP_SHOW_S or t - (h.get("t") or 0) >= WAIT_GONE_S:
        return None
    return r


def _wakes(pid: str, hosts: set | None, overview: Path | None = None) -> dict:
    """全局快照 wake 里给这个项目的 {key: msg}。projects：只报给这些项目（None 为都报）；host：只报给项目快照里有这台主机的项目（要还、已还、对方在用），
    hosts 为 None 时只取不带 host 的。overview：测试换一份全局快照用。"""
    return {w["key"]: w["msg"] for w in ((load_json(overview, None) if overview else snapshot()) or {}).get("wake") or []
            if (w.get("projects") is None or pid in w["projects"]) and (not w.get("host") or w["host"] in (hosts or ()))}


def check_told(cfg: dict, sf: Path, overview: Path | None = None, *, remember: bool = True) -> list[str]:
    """labmon check 末尾列的：项目已归档的那句，与 wait 还没报过的页面上的改动（wake 里 page: 开头的，records.tell 写的，#121）。
    列出的记进 wait 的已报列表（只加不删，别的告警的记账不动），之后 wait 不重报。"""
    gone = PJ.archived_msg(cfg["id"])
    st = load_json(sf, {})
    seen = set(st.get("reported", []))
    new = {k: m for k, m in _wakes(cfg["id"], None, overview).items() if k.startswith("page:") and k not in seen}
    if new and remember:
        t = time.time()
        save_json(sf, {**st, "reported": sorted(seen | set(new)), "last": {**(st.get("last") or {}), **{k: t for k in new}}, "t": t})
    return ([gone] if gone else []) + list(new.values())


def collector_stale(cfg: dict, snap: dict | None, out: Path) -> str | None:
    """项目的采集停了的那句（快照超过 max(3×interval, stale_min) + 1 分钟没更新（config.stale_limit），或没有快照），没停为 None。
    wait 报本项目的 collector_stale、wait --job 报被等的别的项目的 collector_stale:<项目> 都经它。"""
    age = time.time() - snap["generated"] if snap else None
    if age is not None and age <= stale_limit(cfg["interval"], cfg["alerts"]["stale_min"]):
        return None
    return (f"采集已停 {age / 60:.0f} 分钟：看板服务可能没在跑，告诉用户（看板那台机器上 labmon service status）"
            if age is not None else f"看板上还没有 {cfg['id']} 的快照：看板服务可能没在跑，告诉用户（看板那台机器上 labmon service status）")


def wait_check(cfg: dict, out: Path, sf: Path, overview: Path | None = None, extra: dict | None = None) -> dict:
    """读一次快照，返回新告警 {key: msg}（只报给会话看的：to 为 agent、both 的；报过的消失后 REWAKE_S 内再出现不再报，_new_alerts）。快照太旧（采集停了）也算一条告警。
    全局快照的 wake（借到卡、到了约定时间、对方在用、已归还、独占任务作废）里与本项目有关的也当告警报。overview：测试换一份全局快照用。
    extra：另外要一起报的 {key: msg}（wait --job 的被等项目采集停了 JobWatch.stale；客户端同步配置的冲突、推不上），同样只报一次。"""
    pid = cfg["id"]
    gone = PJ.archived_msg(pid)
    if gone:                                          # 已归档：每次都报、不记账（会话再跑 wait 也立刻知道），不报 collector_stale
        return {"archived": gone}
    snap = load_json(out / "snapshot.json", None)
    current = {a["key"]: a["msg"] for a in (snap or {}).get("alerts", []) if a.get("to") != "user"}   # 只给用户的（ssh_fail:）不叫醒会话，labmon status 照列（#88）
    hosts = {h["id"] for h in (snap or {}).get("hosts", [])}     # 本项目的主机（与 serve.proj_events 同一口径）：登记所在的都算，车道跑完收起了也算（#117）
    current.update(_wakes(pid, hosts, overview))
    current.update(extra or {})
    stale = collector_stale(cfg, snap, out)
    if stale:
        current["collector_stale"] = stale
    return {k: current[k] for k in _new_alerts(sf, current)}


def job_state(snap: dict | None, name: str, rows: list[tuple[str, dict]]) -> dict:
    """一个任务此刻的状态（labmon wait --job）：snap 是它所在项目的快照（None 为没有），rows 是工作台存的用量记录里
    这个项目这个任务的行 [(主机, 行)]（state.usage_file，30 天；车道收起后快照里没有它时才看）。返回 {st, text}：
    - running / queued：快照 jobs[] 里还会跑的（几条车道都有同名的：在跑的优先，再取排队里不卡住的；与 worker 的依赖判定“还会跑的盖过以前的结果”一致）；
    - blocked：排着、但不会再跑：所在车道队首的依赖等不来（车道的 dep 是 failed / missing / cycle，原因取 worker 的 waiting；并行清单只看这一行自己的，车道的 bad），或所在主机已归还、清单没搬走；
    - done / failed：快照里同名的取最晚结束的那条，带日志位置（车道的 logs_dir，探针给的）；快照里没有的取用量记录里最后一次（停下重排的不算）；
    - missing：都没有。
    作废（重新排队）不在这里认：看全局快照 wake 的 void:（JobWatch）。"""
    snap = snap or {}
    lanes = {ln["id"]: ln for ln in snap.get("lanes", [])}
    labels = {h["id"]: h.get("label") or h["id"] for h in snap.get("hosts", [])}
    where = lambda ln: f"{labels.get(ln.get('host'), ln.get('host'))} {ln.get('label') or ln.get('id')}"
    js = [j for j in snap.get("jobs", []) if j["id"] == name]

    def stuck(j):
        ln = lanes.get(j["lane"], {})
        if (ln.get("par") or 1) > 1:         # 并行清单（#147）：只有这一行自己等不来才不会再跑，窗口里别的行照跑
            if name in (ln.get("bad") or {}):
                return f"不会再跑：{where(ln)} 卡在依赖上：{ln['bad'][name]}"
        elif ln.get("dep") in ("failed", "missing", "cycle"):
            return f"不会再跑：{where(ln)} 卡在依赖上：{ln.get('waiting')}"
        if ln.get("returned"):
            return f"不会再跑：{where(ln)} 所在主机已归还，清单没搬走（换到在用主机：labmon worker add）"
        return None
    run = next((j for j in js if j["status"] == "running"), None)
    if run:
        eta = f"，预计 {fmt_time(run['eta_ts'])} 跑完" if run.get("eta_ts") else ""
        ln = lanes.get(run["lane"], {"id": run["lane"]})
        if run.get("host"):                  # 清单搬走前那台放的、在那台跑完这一格（#100）：车道已跟着登记走
            return {"st": "running", "text": f"在 {labels.get(run['host'], run['host'])} 上跑{eta}（清单已搬到 {where(ln)}，这一格在原主机跑完）"}
        return {"st": "running", "text": f"在 {where(ln)} 上跑{eta}"}
    queued = [j for j in js if j["status"] == "queued"]
    if queued:
        j = next((x for x in queued if not stuck(x)), None)
        if j is None:
            return {"st": "blocked", "text": stuck(queued[0])}
        ln = lanes.get(j["lane"], {"id": j["lane"]})
        head = next((x for x in snap.get("jobs", []) if x["lane"] == j["lane"] and x["status"] == "queued"), None) is j
        why = f"（{ln['waiting']}）" if head and ln.get("waiting") else ""
        start = f"，预计 {fmt_time(j['plan_start_ts'])} 开始" if j.get("plan_start_ts") else ""
        return {"st": "queued", "text": f"在 {where(ln)} 排队{start}{why}"}
    ends = [j for j in js if j["status"] in ("done", "failed")]
    if ends:
        j = max(ends, key=lambda x: x.get("end_ts") or 0)
        ln = lanes.get(j["lane"], {"id": j["lane"]})
        took = f"，用时 {fmt_dur(j['actual_min'] * 60)}" if j.get("actual_min") else ""
        at = f"，{fmt_time(j['end_ts'])} 跑完" if j.get("end_ts") else ""
        log = f"；日志 {ln.get('host')}:{ln['logs_dir']}/{name}.log" if ln.get("logs_dir") else ""
        if j["status"] == "done":
            return {"st": "done", "text": f"成功{at}{took}（{where(ln)}）{log}"}
        how = f"被信号终止（退出码 {j['killed']}）" if j.get("killed") else "失败"
        redo = f"；要重跑删掉 {ln.get('state_dir') or '状态目录'}/{name}.failed"
        return {"st": "failed", "text": f"{how}{at}{took}（{where(ln)}）{log}{redo}"}
    rs = [(h, r) for h, r in rows if r.get("st") in ("done", "failed")]
    if rs:
        h, r = max(rs, key=lambda x: x[1].get("t") or 0)
        ok = r["st"] == "done"
        return {"st": r["st"], "text": f"{'成功' if ok else '失败'}，{fmt_time(r['t'])} 跑完，用时 {fmt_dur(r['t'] - r['s'])}"
                                       f"（{h} 上 {r.get('root')} 的 {r.get('queue')}；项目快照里已没有这一行：车道收起了或行删了）"}
    return {"st": "missing", "text": "找不到：项目的清单里没有这一行，近 30 天也没跑过（任务名写错了，或它所在的清单没登记）"}


def _k(k: tuple[str, str]) -> str:
    return f"{k[0]}/{k[1]}"


class JobWatch:
    """labmon wait --job 的一次等待（#135）：每轮 check 读项目快照与全局快照，返回各任务 {(项目, 任务): {st, text, final}}。
    final 的条件（快照是上一轮采的，刚加的行、刚删掉 .done 重排的任务上一份快照里还是“找不到”“已跑完”，#56 同一教训）：
    - 这次等的过程中见过它在跑或排队、之后跑完了（done / failed）：当场算；
    - 其余的结束、找不到、不会再跑：要连着 JOB_CONFIRM 份开始等之后写出的快照都这样（看板那台不在内网的、有车道沿用上一次探针数据（stale）的那份不算：数据不是这一轮的）；
    - 开始等之后全局快照 wake 里新出现了它的 void:（作废、被对方挤掉，已重新排队）：当场算，st 为 voided；
    - 别的项目不在看板上（含已归档）：当场算 missing。
    被等的别的项目采集停了（collector_stale）记进 self.stale {collector_stale:<项目>: 那句}，wait 并进 wait_check 当告警报（本项目的 wait_check 自己报）。
    内部状态经 state() 交给客户端、下一次请求经 restore() 带回（#162：服务端不记会话）。overview：测试换一份全局快照用。"""

    def __init__(self, cfg: dict, out: Path, names: list[str], overview: Path | None = None):
        self.pid, self.out, self.ov, self.t0 = cfg["id"], out, overview, time.time()
        self.keys = list(dict.fromkeys(job_key(n, self.pid) for n in names))
        self.live, self.streak, self.final, self.voids0, self.void_keys, self.stale = set(), {}, {}, None, set(), {}

    def state(self) -> dict:
        """要交给客户端带回的内部状态（JSON）。"""
        return {"t0": self.t0, "live": sorted(_k(k) for k in self.live), "streak": {_k(k): [s, sorted(g)] for k, (s, g) in self.streak.items()},
                "final": {_k(k): v for k, v in self.final.items()}, "voids0": None if self.voids0 is None else sorted(self.voids0),
                "void_keys": sorted(self.void_keys)}

    def restore(self, st: dict | None) -> "JobWatch":
        """接着上一次请求的状态等（st 是上一次 state() 的，客户端原样带回；没有是刚开始等）。"""
        if st:
            key = lambda s: tuple(s.split("/", 1))     # noqa: E731
            self.t0 = float(st.get("t0") or self.t0)
            self.live = {key(s) for s in st.get("live") or []}
            self.streak = {key(s): (v[0], set(v[1])) for s, v in (st.get("streak") or {}).items()}
            self.final = {key(s): v for s, v in (st.get("final") or {}).items()}
            self.voids0 = None if st.get("voids0") is None else set(st["voids0"])
            self.void_keys = set(st.get("void_keys") or [])
        return self

    def name(self, k: tuple[str, str]) -> str:
        return k[1] if k[0] == self.pid else f"{k[0]}/{k[1]}"

    def _snap(self, p: str) -> tuple[dict | None, str | None]:
        """项目 p 的快照，或它不在看板上的那句。本项目取 wait 的 out；别的项目取看板上登记的输出目录（projects.out_dir）。"""
        if p == self.pid:
            return load_json(self.out / "snapshot.json", None), None
        if p in PJ.archived():
            return None, f"项目 {p} 已归档"
        try:
            cfg = PJ.load_cfg(p) if p in PJ.registered() else None
        except ConfigError:
            cfg = None
        if not cfg:
            return None, f"看板上没有项目 {p}"
        out = PJ.out_dir(p)
        snap = load_json(out / "snapshot.json", None)
        stale = collector_stale(cfg, snap, out)
        if stale:
            self.stale[f"collector_stale:{p}"] = f"等的任务所在的项目 {p}：{stale}"
        return snap, None

    def check(self) -> dict:
        glob = (load_json(self.ov, None) if self.ov else snapshot()) or {}
        keys = set(self.keys)
        voids = {w["key"]: w for w in glob.get("wake") or [] if tuple(w.get("job") or ()) in keys}
        if self.voids0 is None:
            self.voids0 = set(voids)
        rows, cur, self.stale = None, {}, {}
        for k in self.keys:
            if k in self.final:
                continue
            v = next((w for key, w in voids.items() if key not in self.voids0 and tuple(w["job"]) == k), None)
            if v:
                self.void_keys.add(v["key"])
                self.final[k] = {"st": "voided", "text": f"作废：{v['msg']}（要接着等就再挂 labmon wait --job {self.name(k)}）"}
                continue
            snap, gone = self._snap(k[0])
            if gone:
                self.final[k] = {"st": "missing", "text": gone}
                continue
            if rows is None and not any(j["id"] == k[1] for j in (snap or {}).get("jobs", [])):
                rows = [(h, r) for h in usage_hosts() for r in load_rows(usage_file(h)) if (r.get("t") or 0) >= time.time() - USAGE_KEEP_S]   # 文件只在追加时修剪
            st = cur[k] = job_state(snap, k[1], [(h, r) for h, r in rows or () if (r.get("p"), r.get("n")) == k])
            if st["st"] in ("running", "queued"):
                self.live.add(k)
                self.streak.pop(k, None)
                continue
            if k in self.live and st["st"] in ("done", "failed"):
                self.final[k] = st
                continue
            gen = (snap or {}).get("generated") or 0
            old = self.streak.get(k)
            seen = old[1] if old and old[0] == st["st"] else set()
            if gen > self.t0 and not (snap or {}).get("lan") and not any(ln.get("stale") for ln in (snap or {}).get("lanes", [])):
                seen = seen | {gen}
            self.streak[k] = (st["st"], seen)
            if len(seen) >= JOB_CONFIRM:
                self.final[k] = st
            else:
                cur[k] = dict(st, text="还在确认（快照里已是这样，等下一轮采集）：" + st["text"])
        return {k: dict(self.final[k], final=True) if k in self.final else dict(cur[k], final=False) for k in self.keys}


class Busy(Exception):
    """同时挂着的长轮询已满（SLOTS）：接口回 503，客户端退避再来。"""


class AccountBusy(Busy):
    """这个账号同时挂着的已有 ACCOUNT_SLOTS 个（#18）：是他自己挂多了，接口回 429，命令行照实报、不退避。"""


_SLOTS = threading.BoundedSemaphore(SLOTS)
_HELD: dict[str, int] = {}                     # 账号 → 此刻挂着几个（只记限了数的普通账号）
_RIDS: dict[str, tuple[float, dict]] = {}      # 请求号 → (回话的时刻, 回话)
_RID_LOCK = threading.Lock()


def _sig(out: Path, w: JobWatch | None) -> tuple:
    """判告警要读的文件的修改时刻：项目快照、全局快照（wake、页面上的改动）、projects.json（归档）、被等的别的项目的快照。变了才再判。"""
    others = sorted({k[0] for k in w.keys if k[0] != w.pid}) if w else []
    return tuple(file_sig(p) for p in [out / "snapshot.json", state_dir() / "snapshot.json", state_dir() / "projects.json"]
                 + [PJ.out_dir(p) / "snapshot.json" for p in others if PJ.ID_RE.fullmatch(p)])


def wait(pid: str, names: list[str] | None = None, watch: dict | None = None, hold: float = 0, extra: dict | None = None,
         rid: str | None = None, overview: Path | None = None, account: str | None = None,
         session: str | None = None, take: bool = False, who: str | None = None) -> dict:
    """POST /api/wait（#162）：判一次，有新告警或等的任务都有结果就回，否则挂着（最多 hold 秒，不超过 HOLD_MAX）等相关文件变了再判。
    回 {new: {key: msg}, jobs: [{name, st, text, final}]（按等的顺序）, watch: JobWatch 的状态（客户端下次带回）, done: 等的任务是不是都有了结果}。
    session：会话标识（client.session_id 的哈希），who：账号名，take：labmon wait --take（_claim：一个项目只让一个会话等，别的会话来抛 Refused(409)）。
    项目没登记抛 projects.Refused(404)；挂着的满了抛 Busy；account（普通账号的名字，管理员不传）同时挂着的已满 ACCOUNT_SLOTS 个抛 AccountBusy。rid 见模块说明。"""
    now = time.time()
    with _RID_LOCK:
        for r in [r for r, (t, _) in _RIDS.items() if t < now - RID_KEEP_S]:
            del _RIDS[r]
        if rid and rid in _RIDS:
            return _RIDS[rid][1]
        if account and _HELD.get(account, 0) >= ACCOUNT_SLOTS:
            raise AccountBusy(f"你这个账号同时挂着的 labmon wait 已有 {ACCOUNT_SLOTS} 个（上限）：先停掉用不着的会话里的 wait，再挂这一个")
        if account:
            _HELD[account] = _HELD.get(account, 0) + 1
    try:
        PJ.require(pid)
        if not _SLOTS.acquire(blocking=False):
            raise Busy(f"看板上同时挂着的 labmon wait 已有 {SLOTS} 个，稍后再来")
        try:
            r = _wait(pid, names or [], watch, max(0.0, min(float(hold or 0), HOLD_MAX)), extra or {}, overview, session, take, who)
        finally:
            _SLOTS.release()
    finally:
        if account:
            with _RID_LOCK:
                _HELD[account] -= 1
                if not _HELD[account]:
                    del _HELD[account]
    if rid:
        with _RID_LOCK:
            _RIDS[rid] = (time.time(), r)
    return r


def _wait(pid: str, names: list[str], watch: dict | None, hold: float, extra: dict, overview: Path | None,
          session: str | None = None, take: bool = False, who: str | None = None) -> dict:
    with PJ._reg_locked():
        PJ.require(pid)
        cfg = PJ.load_cfg(pid)
        registration = PJ.meta(pid)
    out = PJ.out_dir(pid)
    sf = state_file(out)
    w = JobWatch(cfg, out, names, overview).restore(watch) if names else None
    end, sig, last, nap, mine = time.time() + hold, None, 0.0, threading.Event(), False
    while True:
        now = time.time()
        s = _sig(out, w)
        if s != sig or now - last >= POLL_S or now >= end:
            sig, last = s, now
            st = w.check() if w else {}
            with PJ._reg_locked():
                PJ.require(pid)
                if PJ.meta(pid) != registration:
                    raise PJ.Refused(409, f"项目 {pid} 的登记已变化，请重新运行 labmon wait")
                _claim(pid, sf, session, who, take and not mine, mine, now)    # 别的会话在等就挡下，不领告警（#142）
                mine = True
                new = wait_check(cfg, out, sf, overview, {**(w.stale if w else {}), **extra})
            if w:
                new = {k: m for k, m in new.items() if k not in w.void_keys}     # 等的任务的 void: 只在任务那段写
            done = bool(st) and all(x["final"] for x in st.values())
            if new or done or now >= end:
                return {"new": new, "jobs": [{"name": w.name(k), **x} for k, x in st.items()] if w else [],
                        "watch": w.state() if w else None, "done": done}
        nap.wait(min(1.0, max(0.0, end - now)))
