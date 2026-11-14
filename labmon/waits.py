"""labmon wait 在看板服务这边的判定（#162：以前在命令行进程里读本机文件，现在由 POST /api/wait 在服务端做，逻辑不变）。

- wait_check：读一次项目快照与全局快照的 wake，返回新告警；“报过没有”记在项目输出目录的 .labmon/wait_state.json（同一项目几个会话共用这一份）。
- JobWatch：labmon wait --job 等某些任务跑完（#135）；内部状态（开始等的时刻、见过它在跑、确认了几份快照……）由 state() 交给客户端、restore() 带回，
  服务端不记会话。
- wait：长轮询（#157 方案 C）：有新告警、或等的任务都有了结果就回，否则最多挂 HOLD_MAX 秒；挂着时只在相关文件变了、或离上次判过 POLL_S 秒时再判。
  同时挂着的封顶 SLOTS 个（多了抛 Busy，接口回 503、客户端退避）。请求号 rid：同一个号 RID_KEEP_S 秒内再来回上次那份（回话丢在半路时客户端带同一个号重试，
  已记成报过的告警不会漏）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import poolqueues as PQ
from . import projects as PJ
from .collect import _qpath
from .config import ConfigError, fmt_dur, fmt_time, stale_limit
from .overview import USAGE_KEEP_S
from .state import load_json, load_rows, save_json, snapshot, state_dir

JOB_CONFIRM = 2       # wait --job：没见过它在跑、排队就已结束、找不到、不会再跑的，要连着这么多份开始等之后写出的快照都这样才报（#135）
JOB_BAD_RC = 4        # wait --job 都有了结果、但不全是成功（失败、作废、找不到、不会再跑）的退出码；1 是新告警，2、3 已被参数错、配置错占着
HOLD_MAX = 25         # 长轮询一次最多挂这么久（秒）：小于 TX 上 nginx 的 proxy_read_timeout（60 秒）
POLL_S = 30           # 挂着时文件没变也至少这么久再判一次（collector_stale 按时间判，同以前每 30 秒读一次）
SLOTS = 32            # 同时挂着的长轮询最多几个：每个占一个处理线程
RID_KEEP_S = 120      # 请求号记多久


def _new_alerts(sf: Path, current: dict) -> list[str]:
    """与上次已报的告警比，返回新出现的 key 并记账；条件消失的告警清掉，再次出现时会再报。"""
    reported = set(load_json(sf, {}).get("reported", [])) & set(current)
    new = [k for k in current if k not in reported]
    save_json(sf, {"reported": sorted(reported | set(new)), "t": time.time()})
    return new


def _wakes(pid: str, hosts: set | None, overview: Path | None = None) -> dict:
    """全局快照 wake 里给这个项目的 {key: msg}。projects：只报给这些项目（None 为都报）；host：只报给项目快照里有这台主机的项目（要还、已还、对方在用），
    hosts 为 None 时只取不带 host 的。overview：测试换一份全局快照用。"""
    return {w["key"]: w["msg"] for w in ((load_json(overview, None) if overview else snapshot()) or {}).get("wake") or []
            if (w.get("projects") is None or pid in w["projects"]) and (not w.get("host") or w["host"] in (hosts or ()))}


def check_told(cfg: dict, sf: Path, overview: Path | None = None) -> list[str]:
    """labmon check 末尾列的：项目已归档的那句，与 wait 还没报过的页面上的改动（wake 里 page: 开头的，records.tell 写的，#121）。
    列出的记进 wait 的已报列表（只加不删，别的告警的记账不动），之后 wait 不重报。"""
    gone = PJ.archived_msg(cfg["id"])
    st = load_json(sf, {})
    seen = set(st.get("reported", []))
    new = {k: m for k, m in _wakes(cfg["id"], None, overview).items() if k.startswith("page:") and k not in seen}
    if new:
        save_json(sf, {**st, "reported": sorted(seen | set(new)), "t": time.time()})
    return ([gone] if gone else []) + list(new.values())


def collector_stale(cfg: dict, snap: dict | None, out: Path) -> str | None:
    """项目的采集停了的那句（快照超过 max(3×interval, stale_min) + 1 分钟没更新（config.stale_limit），或没有快照），没停为 None。
    wait 报本项目的 collector_stale、wait --job 报被等的别的项目的 collector_stale:<项目> 都经它。"""
    age = time.time() - snap["generated"] if snap else None
    if age is not None and age <= stale_limit(cfg["interval"], cfg["alerts"]["stale_min"]):
        return None
    return (f"采集已停 {age / 60:.0f} 分钟：看板服务可能没在跑（labmon service status）"
            if age is not None else f"{out / 'snapshot.json'} 不存在：看板服务没在跑？")


def wait_check(cfg: dict, out: Path, sf: Path, overview: Path | None = None, extra: dict | None = None) -> dict:
    """读一次快照，返回新告警 {key: msg}。快照太旧（采集停了）也算一条告警。
    全局快照的 wake（借到卡、到了约定时间、对方在用、已归还、独占任务作废）里与本项目有关的也当告警报。overview：测试换一份全局快照用。
    extra：另外要一起报的 {key: msg}（wait --job 的被等项目采集停了 JobWatch.stale；客户端同步配置的冲突、推不上），同样只报一次。"""
    pid = cfg["id"]
    gone = PJ.archived_msg(pid)
    if gone:                                          # 已归档：每次都报、不记账（会话再跑 wait 也立刻知道），不报 collector_stale
        return {"archived": gone}
    snap = load_json(out / "snapshot.json", None)
    current = {a["key"]: a["msg"] for a in (snap or {}).get("alerts", [])}
    hosts = {h["id"] for h in (snap or {}).get("hosts", [])}     # 本项目的主机（与 serve.proj_events 同一口径）：登记所在的都算，车道跑完收起了也算（#117）
    current.update(_wakes(pid, hosts, overview))
    current.update(extra or {})
    stale = collector_stale(cfg, snap, out)
    if stale:
        current["collector_stale"] = stale
    return {k: current[k] for k in _new_alerts(sf, current)}


def job_key(name: str, pid: str) -> tuple[str, str]:
    """--job 的名字 → (项目, 任务)：与清单 after= 同一套写法（worker 的 dep_keys；worker 不 import 本包，各一份），不带 项目/ 前缀的是本项目。"""
    p, n = name.split("/", 1) if "/" in name else (pid, name)
    return p, n


def job_state(snap: dict | None, name: str, rows: list[tuple[str, dict]]) -> dict:
    """一个任务此刻的状态（labmon wait --job）：snap 是它所在项目的快照（None 为没有），rows 是工作台存的用量记录里
    这个项目这个任务的行 [(主机, 行)]（state.usage_file，30 天；车道收起后快照里没有它时才看）。返回 {st, text}：
    - running / queued：快照 jobs[] 里还会跑的（几条车道都有同名的：在跑的优先，再取排队里不卡住的；与 worker 的依赖判定“还会跑的盖过以前的结果”一致）；
    - blocked：排着、但不会再跑：所在车道队首的依赖等不来（车道的 dep 是 failed / missing / cycle，原因取 worker 的 waiting），或所在主机已归还、清单没搬走；
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
        if ln.get("dep") in ("failed", "missing", "cycle"):
            return f"不会再跑：{where(ln)} 卡在依赖上：{ln.get('waiting')}"
        if ln.get("returned"):
            return f"不会再跑：{where(ln)} 所在主机已归还，清单没搬走（换到在用主机：labmon worker add）"
        return None
    run = next((j for j in js if j["status"] == "running"), None)
    if run:
        eta = f"，预计 {fmt_time(run['eta_ts'])} 跑完" if run.get("eta_ts") else ""
        return {"st": "running", "text": f"在 {where(lanes.get(run['lane'], {'id': run['lane']}))} 上跑{eta}"}
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
        pool_data = PQ.read()
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
                files = sorted((state_dir() / "usage").glob("*.jsonl"))
                rows = [(f.stem, r) for f in files for r in load_rows(f) if (r.get("t") or 0) >= time.time() - USAGE_KEEP_S]   # 文件只在追加时修剪
            st = cur[k] = job_state(snap, k[1], [(h, r) for h, r in rows or () if (r.get("p"), r.get("n")) == k])
            pool_st = PQ.wait_status(*k, pool_data)
            if pool_st and (pool_st["st"] in ("running", "queued") or st["st"] not in ("running", "queued")):
                if pool_st["st"] in ("done", "failed"):
                    self.final[k] = pool_st
                else:
                    cur[k] = pool_st
                    self.live.add(k)
                    self.streak.pop(k, None)
                continue
            if st["st"] in ("running", "queued"):
                self.live.add(k)
                self.streak.pop(k, None)
                continue
            if k in self.live and st["st"] in ("done", "failed"):
                self.final[k] = st
                continue
            # 新登记还没有进入项目采集时，空快照不是任务不存在的证据。
            # 只挡 missing；真实任务状态和已明确不存在的项目仍按原规则处理。
            lanes = {_qpath(ln.get("root"), ln.get("queue")) for ln in (snap or {}).get("lanes", [])}
            if st["st"] == "missing" and any(q["state"] in PQ.ACTIVE and
                    _qpath(q.get("root"), q.get("jobs")) not in lanes for q in PQ.project_rows(k[0], pool_data)):
                self.streak.pop(k, None)
                cur[k] = dict(st, text="等待项目采集确认新登记的算力池清单")
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


_SLOTS = threading.BoundedSemaphore(SLOTS)
_RIDS: dict[str, tuple[float, dict]] = {}      # 请求号 → (回话的时刻, 回话)
_RID_LOCK = threading.Lock()


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _sig(out: Path, w: JobWatch | None) -> tuple:
    """判告警要读的文件的修改时刻：项目快照、全局快照（wake、页面上的改动）、projects.json（归档）、被等的别的项目的快照。变了才再判。"""
    others = sorted({k[0] for k in w.keys if k[0] != w.pid}) if w else []
    return tuple(_mtime(p) for p in [out / "snapshot.json", state_dir() / "snapshot.json", state_dir() / "projects.json"]
                 + ([PQ.state_file()] if w else []) + [PJ.out_dir(p) / "snapshot.json" for p in others if PJ.ID_RE.fullmatch(p)])


def wait(pid: str, names: list[str] | None = None, watch: dict | None = None, hold: float = 0, extra: dict | None = None,
         rid: str | None = None, overview: Path | None = None) -> dict:
    """POST /api/wait（#162）：判一次，有新告警或等的任务都有结果就回，否则挂着（最多 hold 秒，不超过 HOLD_MAX）等相关文件变了再判。
    回 {new: {key: msg}, jobs: [{name, st, text, final}]（按等的顺序）, watch: JobWatch 的状态（客户端下次带回）, done: 等的任务是不是都有了结果}。
    项目没登记抛 projects.Refused(404)；挂着的满了抛 Busy。rid 见模块说明。"""
    now = time.time()
    with _RID_LOCK:
        for r in [r for r, (t, _) in _RIDS.items() if t < now - RID_KEEP_S]:
            del _RIDS[r]
        if rid and rid in _RIDS:
            return _RIDS[rid][1]
    if not PJ.ID_RE.fullmatch(pid) or pid not in PJ.registered():
        raise PJ.Refused(404, f"看板上没有项目 {pid}：在项目目录里 labmon project push 登记")
    if not _SLOTS.acquire(blocking=False):
        raise Busy(f"看板上同时挂着的 labmon wait 已有 {SLOTS} 个，稍后再来")
    try:
        r = _wait(pid, names or [], watch, max(0.0, min(float(hold or 0), HOLD_MAX)), extra or {}, overview)
    finally:
        _SLOTS.release()
    if rid:
        with _RID_LOCK:
            _RIDS[rid] = (time.time(), r)
    return r


def _wait(pid: str, names: list[str], watch: dict | None, hold: float, extra: dict, overview: Path | None) -> dict:
    cfg, out = PJ.load_cfg(pid), PJ.out_dir(pid)
    sf = out / ".labmon" / "wait_state.json"
    w = JobWatch(cfg, out, names, overview).restore(watch) if names else None
    end, sig, last, nap = time.time() + hold, None, 0.0, threading.Event()
    while True:
        now = time.time()
        s = _sig(out, w)
        if s != sig or now - last >= POLL_S or now >= end:
            sig, last = s, now
            st = w.check() if w else {}
            new = wait_check(cfg, out, sf, overview, {**(w.stale if w else {}), **extra})
            if w:
                new = {k: m for k, m in new.items() if k not in w.void_keys}     # 等的任务的 void: 只在任务那段写
            done = bool(st) and all(x["final"] for x in st.values())
            if new or done or now >= end:
                return {"new": new, "jobs": [{"name": w.name(k), **x} for k, x in st.items()] if w else [],
                        "watch": w.state() if w else None, "done": done}
        nap.wait(min(1.0, max(0.0, end - now)))
