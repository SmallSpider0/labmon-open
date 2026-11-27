"""推模式轻量临时主机（Google Colab、Kaggle 等无固定 SSH 与实验室共享盘的算力节点，#213）。

状态存放在 <状态目录>/ephemeral/<host_id>.json，绝不写入 hosts.toml，也不写入 labmon/disk.py。
- 入口：POST /api/ephemeral/push（serve.ROUTES：project 作用域、归属校验、128 KiB 请求体上限、主备租约）。
- 生命周期：以服务端时钟与 last_seen 判定 online → down（重启宽限取 max(last_seen, deploy.RUNNING["started"])）；
  心跳超时或退出时若仍有 running 任务，自动结转为 failed 并经 overview._keep_usage 写入 usage/<host_id>.jsonl；
  总览离线满 1 小时（无丢失任务）自动隐去卡片，离线满 7 天自动清理状态文件与曲线文件（usage 保留 30 天）。
- 协同与隔离：检测同项目多在线实例同时 running 相同 job_id 返回 warnings 并产 ephemeral_dup: 告警；
  云盘剩余低于 CLOUD_LOW_GB（1 GiB）标红并产项目级会话告警 ephemeral_disk_low:。
"""
from __future__ import annotations

import fcntl
import math
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import deploy as DP
from . import lease as LS
from . import projects as PJ
from .config import GIB, POOL, all_hosts, pick_color
from .records import log_events
from .state import load_json, save_json, state_dir
from .worker.pure import overrun_left

EPHEMERAL_QUEUE = "ephemeral"
HOST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
RESERVED_HOSTS = frozenset({POOL, "__local__", "all"})

TTL_DEFAULT = 180
TTL_MIN = 90
TTL_MAX = 600
NEXT_HEARTBEAT_S = 30
PUSH_MAX_BYTES = 128 * 1024
MIN_PUSH_GAP_S = 5.0
MAX_ACTIVE_PER_PROJECT = 5
STORAGE_MAX = 4
GPUS_MAX = 16
JOBS_MAX = 128
LABEL_MAX = 60
LAST_LINE_MAX = 200
ARTIFACT_DIR_MAX = 256
CLOUD_LOW_GB = 1.0
OVERVIEW_HIDE_S = 3600
OVERVIEW_LOST_HIDE_S = 24 * 3600
PURGE_S = 7 * 86400

JOB_STATUSES = frozenset({"running", "done", "failed", "queued"})
STORAGE_KINDS = frozenset({"cloud", "local"})

_LOCK = threading.Lock()


class EphemeralError(Exception):
    """临时主机上报校验或状态异常，带 HTTP 状态码。"""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _dir() -> Path:
    return state_dir() / "ephemeral"


def _file(hid: str) -> Path:
    return _dir() / f"{hid}.json"


def _root_for(hid: str) -> str:
    return f"ephemeral/{hid}"


@contextmanager
def _locked():
    """ephemeral 状态目录读改写的进程内与跨进程互斥锁。"""
    with _LOCK:
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            yield


def _clean_text(raw, label: str, limit: int, *, required: bool = False, truncate: bool = False) -> str | None:
    if raw is None:
        if required:
            raise EphemeralError(400, f"缺少 {label}")
        return None
    if not isinstance(raw, str):
        raise EphemeralError(400, f"{label} 须为字符串")
    if "\x00" in raw or any(0xD800 <= ord(c) <= 0xDFFF for c in raw):
        raise EphemeralError(400, f"{label} 含非法字符")
    text = " ".join(raw.replace("\r", "\n").splitlines()).strip()
    text = "".join(c for c in text if ord(c) >= 32)
    if required and not text:
        raise EphemeralError(400, f"{label} 不能为空")
    if len(text) > limit:
        if truncate:
            return text[:limit]
        raise EphemeralError(400, f"{label} 长度不能超过 {limit} 字符")
    return text or None


def _num(val, label: str, *, lo: float | None = None, hi: float | None = None, allow_none: bool = True) -> float | None:
    if val is None:
        if allow_none:
            return None
        raise EphemeralError(400, f"缺少 {label}")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise EphemeralError(400, f"{label} 须为数字")
    x = float(val)
    if not math.isfinite(x):
        raise EphemeralError(400, f"{label} 须为有限数字")
    if lo is not None and x < lo:
        raise EphemeralError(400, f"{label} 不能小于 {lo}")
    if hi is not None and x > hi:
        raise EphemeralError(400, f"{label} 不能大于 {hi}")
    return x


def _norm_gpus(raw, hid: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > GPUS_MAX:
        raise EphemeralError(400, f"gpus 须为不超过 {GPUS_MAX} 项的列表")
    out: list[dict] = []
    seen: set[int] = set()
    for pos, g in enumerate(raw):
        if not isinstance(g, dict):
            raise EphemeralError(400, "gpus 列表项须为对象")
        idx_raw = g.get("index", g.get("i", pos))
        if isinstance(idx_raw, bool) or not isinstance(idx_raw, int) or idx_raw < 0 or idx_raw >= 64:
            raise EphemeralError(400, "GPU index 须为非负整数")
        if idx_raw in seen:
            continue
        seen.add(idx_raw)
        uuid = _clean_text(g.get("uuid"), "GPU uuid", 96) or f"ephemeral-{hid}-gpu-{idx_raw}"
        name = _clean_text(g.get("name"), "GPU name", 96, truncate=True) or "GPU"
        util = _num(g.get("util"), "GPU util", lo=0.0, hi=100.0)
        mem_used = _num(g.get("mem_used", g.get("used")), "GPU mem_used", lo=0.0)
        mem_total = _num(g.get("mem_total", g.get("total")), "GPU mem_total", lo=0.0)
        temp = _num(g.get("temp"), "GPU temp", lo=-50.0, hi=200.0)
        power = _num(g.get("power"), "GPU power", lo=0.0)
        power_limit = _num(g.get("power_limit"), "GPU power_limit", lo=0.0)
        out.append({
            "index": idx_raw, "uuid": uuid, "name": name, "util": util,
            "mem_used": mem_used or 0.0, "mem_total": mem_total or 0.0,
            "temp": temp, "power": power, "power_limit": power_limit,
        })
    return out


def _norm_mem(raw) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EphemeralError(400, "mem 须为对象")
    used = _num(raw.get("used_gb"), "mem.used_gb", lo=0.0)
    limit = _num(raw.get("limit_gb"), "mem.limit_gb", lo=0.0)
    if used is None and limit is None:
        return None
    return {"used_gb": round(used or 0.0, 2), "limit_gb": round(limit or 0.0, 2)}


def _norm_storage(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EphemeralError(400, "storage 须为列表")
    if len(raw) > STORAGE_MAX:
        raise EphemeralError(400, f"storage 最多上报 {STORAGE_MAX} 个挂载点")
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise EphemeralError(400, "storage 列表项须为对象")
        kind = item.get("kind")
        if kind not in STORAGE_KINDS:
            raise EphemeralError(400, "storage[].kind 只接受 cloud 或 local")
        path = _clean_text(item.get("path"), "storage[].path", 256, required=True)
        if path in seen:
            continue
        seen.add(path)
        label = _clean_text(item.get("label"), "storage[].label", LABEL_MAX, truncate=True) or path
        total_gb = _num(item.get("total_gb"), "storage[].total_gb", lo=0.0, allow_none=False)
        avail_gb = _num(item.get("avail_gb"), "storage[].avail_gb", lo=0.0, allow_none=False)
        out.append({
            "kind": kind, "label": label, "path": path,
            "total_gb": round(total_gb, 2), "avail_gb": round(avail_gb, 2),
        })
    return out


def _cal_ts(val, offset: float, t: float) -> float | None:
    x = _num(val, "时间戳")
    if x is None:
        return None
    cal = x + offset
    return min(cal, t + 5.0)


def _norm_jobs(raw, prev_jobs: dict[str, dict], offset: float, t: float) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise EphemeralError(400, "jobs 须为列表")
    if len(raw) > JOBS_MAX:
        raise EphemeralError(400, f"jobs 最多上报 {JOBS_MAX} 项")
    out: list[dict] = []
    seen: set[str] = set()
    for j in raw:
        if not isinstance(j, dict):
            raise EphemeralError(400, "jobs 列表项须为对象")
        jid = _clean_text(j.get("id"), "jobs[].id", 128, required=True)
        if jid in seen:
            raise EphemeralError(400, f"jobs[] 里任务 id 重复：{jid}")
        seen.add(jid)
        st = j.get("status")
        if st not in JOB_STATUSES:
            raise EphemeralError(400, f"任务 {jid} 的 status 须为 running/done/failed/queued")
        gpus_raw = j.get("gpus")
        if gpus_raw is None:
            gpus: list[int] = []
        elif isinstance(gpus_raw, list) and all(not isinstance(x, bool) and isinstance(x, int) and 0 <= x < 64 for x in gpus_raw):
            gpus = list(dict.fromkeys(gpus_raw))
        else:
            raise EphemeralError(400, f"任务 {jid} 的 gpus 须为非负整数列表")
        prev = prev_jobs.get(jid) or {}
        start_ts = _cal_ts(j.get("start_ts"), offset, t)
        if start_ts is None and j.get("elapsed_s") is not None:
            el = _num(j.get("elapsed_s"), "elapsed_s", lo=0.0)
            start_ts = t - el
        if start_ts is None:
            start_ts = prev.get("start_ts")
        end_ts = _cal_ts(j.get("end_ts"), offset, t)
        if st == "running":
            if start_ts is None:
                start_ts = t
            end_ts = None
        elif st in ("done", "failed"):
            if end_ts is None:
                end_ts = prev.get("end_ts") or t
            if start_ts is None:
                start_ts = end_ts
            if start_ts > end_ts:
                start_ts = end_ts
        rc = j.get("rc")
        if rc is not None and (isinstance(rc, bool) or not isinstance(rc, int)):
            raise EphemeralError(400, f"任务 {jid} 的 rc 须为整数或 null")
        est_min = _num(j.get("est_min"), "est_min", lo=0.0, hi=100800.0)
        prog_raw = j.get("progress")
        progress = None
        if prog_raw is not None:
            if (isinstance(prog_raw, list) and len(prog_raw) == 2
                    and all(not isinstance(x, bool) and isinstance(x, int) for x in prog_raw)
                    and prog_raw[0] >= 0 and prog_raw[1] > 0):
                progress = [prog_raw[0], prog_raw[1]]
            else:
                raise EphemeralError(400, f"任务 {jid} 的 progress 须为 [已完成步数, 总步数]")
        last_line = _clean_text(j.get("last_line"), "last_line", LAST_LINE_MAX, truncate=True)
        log_mtime = _cal_ts(j.get("log_mtime"), offset, t)
        artifact_dir = _clean_text(j.get("artifact_dir"), "artifact_dir", ARTIFACT_DIR_MAX)
        if artifact_dir is None:
            artifact_dir = prev.get("artifact_dir")
        out.append({
            "id": jid, "status": st, "gpus": gpus,
            "start_ts": round(start_ts, 3) if start_ts is not None else None,
            "end_ts": round(end_ts, 3) if end_ts is not None else None,
            "rc": rc, "est_min": est_min, "progress": progress,
            "last_line": last_line,
            "log_mtime": round(log_mtime, 3) if log_mtime is not None else None,
            "artifact_dir": artifact_dir,
        })
    return out


def _gpu_idx_map(rec: dict) -> tuple[dict[str, int], dict[int, str], dict[int, float]]:
    """从记录中取出 uuid→index、index→uuid、index→util 映射。"""
    hid = rec["host_id"]
    u2i, i2u, i2util = {}, {}, {}
    for g in rec.get("gpus") or []:
        idx = g["index"]
        uuid = g.get("uuid") or f"ephemeral-{hid}-gpu-{idx}"
        u2i[uuid] = idx
        i2u[idx] = uuid
        if g.get("util") is not None:
            i2util[idx] = float(g["util"])
    return u2i, i2u, i2util


def _record_usage(rec: dict, jobs: list[dict], t: float) -> None:
    """将终态任务写入工作台 usage/<host_id>.jsonl，完全复用 overview._keep_usage。"""
    from . import overview as O

    hid, pid = rec["host_id"], rec["project"]
    u2i, i2u, i2util = _gpu_idx_map(rec)
    root = _root_for(hid)
    rows: list[dict] = []
    for j in jobs:
        if j["status"] not in ("done", "failed"):
            continue
        s0 = j.get("start_ts") or j.get("end_ts") or t
        e1 = j.get("end_ts") or t
        if e1 < s0:
            s0 = e1
        g_uuids = []
        for gi in j.get("gpus") or []:
            u = i2u.get(gi) or f"ephemeral-{hid}-gpu-{gi}"
            u2i.setdefault(u, gi)
            g_uuids.append(u)
        utils = [i2util[gi] for gi in j.get("gpus") or [] if gi in i2util]
        rc_str = "0" if j["status"] == "done" else str(j["rc"] if j.get("rc") is not None else 1)
        rows.append({
            "t0": round(s0), "t1": round(e1), "name": j["id"], "project": pid,
            "root": root, "queue": EPHEMERAL_QUEUE, "gpus": g_uuids, "rc": rc_str,
            "wait": 0.0, "gpu_util": round(sum(utils) / len(utils), 1) if utils else None,
        })
    if rows:
        O._keep_usage(state_dir(), hid, rows, t, u2i)


def _load_all_unlocked() -> list[dict]:
    d = _dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(d.glob("*.json")):
        rec = load_json(f, None)
        if isinstance(rec, dict) and rec.get("host_id") and rec.get("project"):
            out.append(rec)
    return out


def _expire_unlocked(t: float) -> list[dict]:
    """在持有锁的情况下推进状态机：TTL 超时结转、满 7 天自动清理（保留 usage/<host_id>.jsonl）。"""
    started = DP.RUNNING.get("started") or 0
    kept: list[dict] = []
    for rec in _load_all_unlocked():
        hid = rec["host_id"]
        if rec.get("state") == "online":
            last_seen = float(rec.get("last_seen") or 0)
            ttl_s = float(rec.get("ttl_s") or TTL_DEFAULT)
            ref = max(last_seen, float(started)) if started else last_seen
            if t - ref > ttl_s:
                rec = dict(rec)
                rec["state"] = "down"
                rec["down_at"] = round(t, 3)
                running = [j for j in rec.get("jobs") or [] if j.get("status") == "running"]
                if running:
                    rec["down_reason"] = "心跳超时（临时主机可能已失联或被回收）"
                    lost = []
                    new_jobs = []
                    for j in rec.get("jobs") or []:
                        if j.get("status") == "running":
                            fj = dict(j, status="failed", end_ts=round(t, 3), rc=-1,
                                      fail_reason="临时主机心跳超时（可能已失联或被回收）")
                            new_jobs.append(fj)
                            lost.append({"id": fj["id"], "artifact_dir": fj.get("artifact_dir")})
                        else:
                            new_jobs.append(j)
                    rec["jobs"] = new_jobs
                    rec["lost_jobs"] = lost
                    _record_usage(rec, [j for j in new_jobs if j["id"] in {x["id"] for x in lost}], t)
                    names = "、".join(x["id"] for x in lost)
                    log_events([{
                        "kind": "host_down", "host": hid, "project": rec["project"],
                        "msg": f"临时主机 {rec.get('label') or hid} 心跳超时离线，中断任务：{names}",
                    }], t)
                else:
                    rec["down_reason"] = None
                    rec["lost_jobs"] = []
                save_json(_file(hid), rec)
        if rec.get("state") == "down":
            down_t = float(rec.get("down_at") or rec.get("last_seen") or 0)
            if t - down_t >= PURGE_S:
                for p in (_file(hid), state_dir() / "series" / f"{hid}.jsonl"):
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass
                continue
        kept.append(rec)
    return kept


def tick(t: float | None = None) -> list[dict]:
    """推进所有临时主机状态机（TTL 超时结转 + 7 天清理），返回仍保留的临时主机记录。"""
    t = time.time() if t is None else float(t)
    with _locked():
        return _expire_unlocked(t)


def _dup_running_unlocked(recs: list[dict], pid: str, cur_iid: str, cur_jobs: list[dict]) -> list[str]:
    """检测同一项目其他在线实例是否正在跑相同 job_id。"""
    running_ids = {j["id"] for j in cur_jobs if j.get("status") == "running"}
    if not running_ids:
        return []
    others: set[str] = set()
    for r in recs:
        if r.get("project") != pid or r.get("state") != "online" or r.get("instance_id") == cur_iid:
            continue
        for j in r.get("jobs") or []:
            if j.get("status") == "running":
                others.add(j["id"])
    return sorted(running_ids & others)


def duplicate_jobs(pid: str, t: float | None = None) -> dict[str, list[dict]]:
    """返回项目 pid 下被 >= 2 个在线临时实例同时 running 的任务：{job_id: [{host_id, label, instance_id, artifact_dir}]}。"""
    t = time.time() if t is None else float(t)
    by_job: dict[str, list[dict]] = {}
    for r in tick(t):
        if r.get("project") != pid or r.get("state") != "online":
            continue
        for j in r.get("jobs") or []:
            if j.get("status") == "running":
                by_job.setdefault(j["id"], []).append({
                    "host_id": r["host_id"], "label": r.get("label") or r["host_id"],
                    "instance_id": r["instance_id"], "artifact_dir": j.get("artifact_dir"),
                })
    return {jid: xs for jid, xs in by_job.items() if len({x["instance_id"] for x in xs}) > 1}


def push(body: dict, *, who: str | None = None, who_role: str = "admin", t: float | None = None) -> tuple[dict, bool]:
    """处理 POST /api/ephemeral/push 心跳上报。返回 (响应体含 project 键, 是否触发立即刷新)。"""
    LS.need("上报临时主机状态")
    t = time.time() if t is None else float(t)
    if not isinstance(body, dict):
        raise EphemeralError(400, "请求体须为 JSON 对象")

    pid = _clean_text(body.get("project"), "project", 64, required=True)
    try:
        PJ.require(pid)
    except PJ.Refused as e:
        raise EphemeralError(e.code, e.msg) from e
    if pid in PJ.archived():
        raise EphemeralError(409, f"项目 {pid} 已归档，不能上报临时主机状态")

    hid = _clean_text(body.get("host_id"), "host_id", 64, required=True)
    if not HOST_ID_RE.fullmatch(hid) or hid.lower() in RESERVED_HOSTS:
        raise EphemeralError(400, f"host_id {hid!r} 不合法：仅允许字母、数字及 _ . -，字母或数字开头，最长 64 字符且不能用保留名")
    if any(str(h.get("id")) == hid for h in all_hosts()):
        raise EphemeralError(409, f"host_id {hid} 与主机清单 hosts.toml 中的已有主机重名，请换一个临时主机标识")

    iid = _clean_text(body.get("instance_id"), "instance_id", 64, required=True)
    if not INSTANCE_ID_RE.fullmatch(iid):
        raise EphemeralError(400, f"instance_id {iid!r} 不合法")

    label = _clean_text(body.get("label"), "label", LABEL_MAX, truncate=True) or hid
    ttl_raw = _num(body.get("ttl_s"), "ttl_s")
    ttl_s = int(max(TTL_MIN, min(TTL_MAX, round(ttl_raw)))) if ttl_raw is not None else TTL_DEFAULT

    closing = body.get("closing", False)
    if not isinstance(closing, bool):
        raise EphemeralError(400, "closing 须为布尔值")

    client_t = _num(body.get("client_t"), "client_t", lo=0.0)
    offset = (t - client_t) if client_t is not None else 0.0

    gpus = _norm_gpus(body.get("gpus"), hid)
    cpu_pct = _num(body.get("cpu_pct"), "cpu_pct", lo=0.0, hi=100.0)
    cpu_count = _num(body.get("cpu_count"), "cpu_count", lo=1.0, hi=1024.0)
    mem = _norm_mem(body.get("mem"))
    storage = _norm_storage(body.get("storage"))

    with _locked():
        recs = _expire_unlocked(t)
        old = next((r for r in recs if r["host_id"] == hid), None)

        # 防止跨项目抢占 host_id
        if old and old.get("project") != pid:
            if old.get("state") == "online":
                raise EphemeralError(409, f"临时主机 {hid} 正归属项目 {old['project']} 在线使用，不能重名上报")
            if who_role != "admin":
                from . import users as U
                old_owner = U.owner_of(old["project"])
                if old_owner != who:
                    raise EphemeralError(403, f"临时主机 {hid} 曾归属项目 {old['project']}，不能复用他人项目的临时主机名")
            old = None

        # 单项目活跃临时主机配额（5 台）
        active_same = [r for r in recs if r.get("project") == pid and r.get("state") == "online" and r["host_id"] != hid]
        if not closing and len(active_same) >= MAX_ACTIVE_PER_PROJECT:
            raise EphemeralError(429, f"项目 {pid} 活跃临时主机已达上限 {MAX_ACTIVE_PER_PROJECT} 台，请等待旧实例退出或离线后再接入")

        same_inst = bool(old and old.get("instance_id") == iid)
        prev_jobs = {j["id"]: j for j in (old.get("jobs") or [])} if same_inst else {}
        jobs = _norm_jobs(body.get("jobs"), prev_jobs, offset, t)

        # 检查任务状态是否发生终态跃迁（终态跃迁与 closing 放行节流，避免短任务结束被 429 挡掉）
        terminal_changed = any(
            j["status"] in ("done", "failed") and (prev_jobs.get(j["id"]) or {}).get("status") != j["status"]
            for j in jobs
        )
        if same_inst and old.get("state") == "online" and not closing and not terminal_changed:
            if t - float(old.get("last_push_t") or 0) < MIN_PUSH_GAP_S:
                raise EphemeralError(429, f"上报过于频繁：单台临时主机最短间隔 {int(MIN_PUSH_GAP_S)} 秒")

        # 同名 host_id 换新 instance_id：先结转旧实例残留 running 任务为 failed
        wake = old is None or not same_inst or closing or terminal_changed
        if old and not same_inst:
            old_running = [j for j in old.get("jobs") or [] if j.get("status") == "running"]
            if old_running:
                finalized = [dict(j, status="failed", end_ts=round(t, 3), rc=-1,
                                  fail_reason="同名临时主机已被新实例接替") for j in old_running]
                _record_usage(old, finalized, t)

        # 超时转 down 后同一 instance_id 重连：允许在未 closing 时恢复 online 并清除断连告警
        reconnected = same_inst and old.get("state") == "down" and not closing
        if reconnected:
            wake = True
            log_events([{"kind": "host_up", "host": hid, "project": pid, "msg": f"临时主机 {label}（{iid}）恢复心跳"}], t)

        down_at = None
        down_reason = None
        lost_jobs: list[dict] = []
        state = "online"
        if closing:
            state = "down"
            down_at = round(t, 3)
            still_running = [j for j in jobs if j["status"] == "running"]
            if still_running:
                down_reason = "临时主机退出时仍有任务未完成"
                new_jobs = []
                for j in jobs:
                    if j["status"] == "running":
                        fj = dict(j, status="failed", end_ts=round(t, 3), rc=-1, fail_reason=down_reason)
                        new_jobs.append(fj)
                        lost_jobs.append({"id": fj["id"], "artifact_dir": fj.get("artifact_dir")})
                    else:
                        new_jobs.append(j)
                jobs = new_jobs
        else:
            # 普通运行中任务若与上次不同（新开任务），也立即触发一次刷新
            if not same_inst or {j["id"] for j in jobs if j["status"] == "running"} != {
                jid for jid, pj in prev_jobs.items() if pj.get("status") == "running"
            }:
                wake = True

        rec = {
            "host_id": hid,
            "project": pid,
            "instance_id": iid,
            "label": label,
            "ttl_s": ttl_s,
            "state": state,
            "closing": closing,
            "down_at": down_at,
            "down_reason": down_reason,
            "lost_jobs": lost_jobs,
            "first_seen": old.get("first_seen", round(t, 3)) if same_inst else round(t, 3),
            "last_seen": round(t, 3),
            "last_push_t": round(t, 3),
            "client_offset": round(offset, 3),
            "gpus": gpus,
            "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
            "cpu_count": int(cpu_count) if cpu_count is not None else None,
            "mem": mem,
            "storage": storage,
            "jobs": jobs,
            "pushed_by": who,
        }
        save_json(_file(hid), rec)
        _record_usage(rec, jobs, t)

        dups = _dup_running_unlocked([r for r in recs if r["host_id"] != hid], pid, iid, jobs) if state == "online" else []
        warnings = [f"duplicate_job:{jid}" for jid in dups]

    return {
        "ok": True,
        "server_t": round(t, 3),
        "host_id": hid,
        "next_s": NEXT_HEARTBEAT_S,
        "warnings": warnings,
        "project": pid,
    }, wake


def _job_eta(j: dict, prog_map: dict, pid: str, t: float) -> float:
    """为总览时间线计算临时主机上运行中任务的预计结束时刻。"""
    pj = prog_map.get((pid, j["id"]))
    if pj and pj.get("eta_ts"):
        return float(pj["eta_ts"])
    s0 = float(j.get("start_ts") or t)
    est = j.get("est_min")
    if est is not None and est > 0:
        el = max(0.0, t - s0)
        return s0 + est * 60.0 if est * 60.0 >= el else t + overrun_left(el)
    return max(t + 600.0, s0 + 600.0)


def overview_hosts(projects: list[dict], prog: dict, colors: dict, t: float) -> list[dict]:
    """为 overview.collect 构造临时主机条目（含同构精简 status、plan.lanes、series），直接复用 overview.occupancy。"""
    from . import overview as O

    proj_ids = {p["id"] for p in projects}
    out: list[dict] = []
    for rec in tick(t):
        hid, pid = rec["host_id"], rec["project"]
        online = rec["state"] == "online"
        if not online:
            down_t = float(rec.get("down_at") or rec.get("last_seen") or 0)
            limit = OVERVIEW_LOST_HIDE_S if rec.get("lost_jobs") else OVERVIEW_HIDE_S
            if t - down_t >= limit:
                continue
        pick_color(colors, pid)
        gpus = rec.get("gpus") or []
        _, i2u, _ = _gpu_idx_map(rec)
        mem = rec.get("mem")
        cpu_pct = rec.get("cpu_pct")
        nproc = rec.get("cpu_count")
        cpu_used = round(cpu_pct / 100.0 * (nproc or 1), 2) if cpu_pct is not None else None
        info = {
            "gpus": [{
                "i": g["index"], "uuid": g["uuid"], "name": g["name"],
                "total": g["mem_total"], "used": g["mem_used"],
                "free": max(0.0, g["mem_total"] - g["mem_used"]), "util": g["util"],
            } for g in gpus],
            "nproc": nproc,
            "mem": {"used": mem["used_gb"], "limit": mem["limit_gb"], "anon": mem["used_gb"], "src": "ephemeral"} if mem else None,
            "cpu_quota": float(nproc) if nproc else None,
            "cpu_used": cpu_used,
            "started": rec.get("first_seen"),
            "press": {"thr": None, "psi": None, "nfs": None, "disk": None},
        }
        root = _root_for(hid)
        running_rows = []
        plan_jobs = []
        done_rows = []
        for j in rec.get("jobs") or []:
            if j["status"] == "running":
                eta_ts = _job_eta(j, prog, pid, t)
                pj = prog.get((pid, j["id"]))
                g_uuids = [i2u.get(gi) or f"ephemeral-{hid}-gpu-{gi}" for gi in j.get("gpus") or []]
                vram_sum = sum(g["mem_used"] for g in gpus if g["index"] in (j.get("gpus") or []))
                running_rows.append({
                    "kind": "ephemeral", "root": root, "queue": EPHEMERAL_QUEUE, "project": pid,
                    "name": j["id"], "pid": 0, "gpus": g_uuids, "tag": None, "hold": False,
                    "stop": None, "cmd": None, "est": None, "acct": None, "log": None,
                    "tail": [j["last_line"]] if j.get("last_line") else [],
                    "est_min": j.get("est_min"), "excl": None, "wait": 0.0,
                    "link": pid if pid in proj_ids else None,
                    "t0": j.get("start_ts") or rec["last_seen"],
                    "artifact_dir": j.get("artifact_dir"),
                    "use": {
                        "t": rec["last_seen"], "nproc": 1,
                        "ram": mem["used_gb"] if mem else None, "ram_peak": mem["used_gb"] if mem else None,
                        "vram": vram_sum if g_uuids else 0.0, "vram_peak": vram_sum if g_uuids else 0.0,
                        "cores": cpu_used, "with_host": [], "with_gpu": [],
                    },
                    **({"prog": {k: v for k, v in pj.items() if k != "id"}} if pj else {}),
                })
                plan_jobs.append({
                    "name": j["id"], "t0": round(j.get("start_ts") or rec["last_seen"]),
                    "t1": round(eta_ts), "gpus": list(j.get("gpus") or []), "run": True,
                })
            elif j["status"] in ("done", "failed"):
                s0 = j.get("start_ts") or j.get("end_ts") or rec["last_seen"]
                e1 = j.get("end_ts") or rec["last_seen"]
                done_rows.append({
                    "min": round(max(0.0, e1 - s0) / 60.0, 1), "kind": "ephemeral",
                    "root": root, "queue": EPHEMERAL_QUEUE, "project": pid, "name": j["id"],
                    "rc": "0" if j["status"] == "done" else str(j["rc"] if j.get("rc") is not None else 1),
                    "gpus": [i2u.get(gi) or f"ephemeral-{hid}-gpu-{gi}" for gi in j.get("gpus") or []],
                    "link": pid if pid in proj_ids else None,
                    "t0": round(s0), "t1": round(e1),
                    "artifact_dir": j.get("artifact_dir"),
                    "with_host": [], "with_gpu": [],
                })
        st = {
            "t": rec["last_seen"], "pid": 0, "host": hid, "ephemeral": True,
            "info": info, "gpu_procs": [], "overrides": [], "waiting": [], "external": [],
            "running": running_rows,
            "queues": [{"root": root, "jobs": EPHEMERAL_QUEUE, "project": pid, "empty": not running_rows, "ephemeral": True}],
            "plan": {"t": rec["last_seen"], "lanes": [{"root": root, "queue": EPHEMERAL_QUEUE, "project": pid, "kind": "ephemeral",
                                                       "stuck": None, "stuck_dep": None, "jobs": plan_jobs}]} if plan_jobs else {"t": rec["last_seen"], "lanes": []},
            "gpu_busy": {g["uuid"]: {"now": g["util"], "m10": g["util"], "h1": None, "cov": 0} for g in gpus if g.get("util") is not None},
            "share": None, "disk": None, "storage": None, "keepalive": None, "kafight": None, "watchdogs": None, "throttle": None,
        }
        entry = {
            "id": hid, "label": rec.get("label") or hid, "ephemeral": True,
            "project": pid, "instance_id": rec["instance_id"],
            "error": None if online else (rec.get("down_reason") or "临时主机已离线"),
            "status_error": None, "status": st, "log": [], "done": done_rows[-60:],
            "lan_off": False, "auto": False, "platform": None, "borrow": None,
            "clock": round(t), "stopped": None, "age": round(max(0.0, t - rec["last_seen"]), 1),
            "worker_up": online,
            "storage": rec.get("storage") or [],
            "health": {"state": "ok" if online else "down", "fails": 0 if online else 1,
                       "since": None if online else rec.get("down_at"), "why": rec.get("down_reason")},
        }
        if online:
            rows = O._series({"id": hid}, st, t)
            entry["series"] = O._thin(rows)
            if gpus:
                entry["busy"] = O.gpu_busy(st, rows)
        out.append(entry)
    return out


def project_hosts(pid: str, t: float | None = None, hide_s: float = 24 * 3600) -> list[dict]:
    """返回项目 pid 应注入采集的临时主机记录（在线的，或离线未超过 hide_s 的）。"""
    t = time.time() if t is None else float(t)
    out: list[dict] = []
    for rec in tick(t):
        if rec.get("project") != pid:
            continue
        if rec["state"] == "online":
            out.append(rec)
        else:
            down_t = float(rec.get("down_at") or rec.get("last_seen") or 0)
            if t - down_t <= hide_s:
                out.append(rec)
    return out


def as_probe(hid: str, t: float | None = None) -> tuple[dict | None, str | None]:
    """将临时主机状态转换为与 probe.py 同构的原始探针字典，供 collect.build 复用。"""
    t = time.time() if t is None else float(t)
    recs = tick(t)
    rec = next((r for r in recs if r["host_id"] == hid), None)
    if rec is None:
        return None, "临时主机状态已清理"
    online = rec["state"] == "online"
    mem = rec.get("mem")
    cgroup = {}
    if mem:
        used_b = int(mem["used_gb"] * GIB)
        lim_b = int(mem["limit_gb"] * GIB)
        cgroup = {"mem_rss": used_b, "mem_used": used_b, "mem_limit": lim_b}
    storage = rec.get("storage") or []
    mounts = {
        s["path"]: {
            "total": int(s["total_gb"] * GIB),
            "avail": int(s["avail_gb"] * GIB),
            "used": int(max(0.0, s["total_gb"] - s["avail_gb"]) * GIB),
        } for s in storage
    }
    probe = {
        "t": rec["last_seen"],
        "ephemeral": True,
        "online": online,
        "instance_id": rec["instance_id"],
        "down_at": rec.get("down_at"),
        "down_reason": rec.get("down_reason"),
        "lost_jobs": rec.get("lost_jobs") or [],
        "gpu": rec.get("gpus") or [],
        "gpu_procs": [],
        "cgroup": cgroup,
        "cpu_pct": rec.get("cpu_pct"),
        "cpu_count": rec.get("cpu_count"),
        "storage": storage,
        "mounts": mounts,
        "checks": [],
        "queues": {
            f"ephemeral:{hid}": {
                "ephemeral": True,
                "online": online,
                "instance_id": rec["instance_id"],
                "activity": rec["last_seen"],
                "jobs": rec.get("jobs") or [],
            }
        },
    }
    return probe, (None if online else (rec.get("down_reason") or "临时主机已离线"))


def is_ephemeral(hid: str) -> bool:
    """判断 host_id 是否为已落盘的临时主机。"""
    return bool(hid and HOST_ID_RE.fullmatch(hid) and _file(hid).is_file())


def online_for_project(pid: str, t: float | None = None) -> list[dict]:
    """返回项目 pid 当前在线的临时主机（归档前核对用）。"""
    t = time.time() if t is None else float(t)
    return [r for r in tick(t) if r.get("project") == pid and r.get("state") == "online"]


def running_for_project(pid: str, t: float | None = None) -> list[dict]:
    """返回项目 pid 当前在线且有 running 任务的临时主机（永久删除前 check_project_idle 核对用）。"""
    return [r for r in online_for_project(pid, t) if any(j.get("status") == "running" for j in r.get("jobs") or [])]


def remove_project(pid: str) -> None:
    """项目永久删除时清理该项目的已离线临时主机状态文件（不动 usage 历史）。"""
    with _locked():
        for rec in _load_all_unlocked():
            if rec.get("project") == pid and rec.get("state") != "online":
                hid = rec["host_id"]
                for p in (_file(hid), state_dir() / "series" / f"{hid}.jsonl"):
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass
