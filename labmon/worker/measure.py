"""worker 主机与进程测量：/proc、cgroup、nvidia-smi、macOS POSIX 回退与 Apple Silicon MPS、压力与限流、用量采样。"""
import math
import os
import re
import shutil
import subprocess
import sys

from . import common as _C
from . import queue as _Q
from .pure import mem_unevictable, proc_tree

_PROC_RSS = {}   # 无 /proc 时 scan_procs 顺带缓存的 {pid: rss_bytes}，供 mem_of 回退读取（不二次起 ps）

def oom_of(j, rc):
    """一次失败的清单任务是不是 OOM（#62）：finish 记进用量行的 oom（estimate 据此抬高同类的估计），squeezed_by 据此判借来的主机上是不是被对方挤掉。
    - "ram"：运行期间容器撞内存上限、内核杀过进程（oom_kill 计数涨了；3.10 内核的 cgroup v1 没有这个计数，退回到 memory.failcnt 涨了），
      且这个任务是被 SIGKILL 的（退出码 137；bash 一起被杀时没留退出码）；
    - "vram"：这一次的日志末尾 40 行（原文，不折叠）是 CUDA OOM；
    - 都不是为 None。单元申请没有日志、不知道退出码，不认。子进程被杀、主进程以退出码 1 退出的（DataLoader worker）认不出。"""
    if j.get("kind") == "slot":
        return None
    n, fc = oom_kills(), num(cg("memory/memory.failcnt"))
    killed = (n > j["oom0"] if n is not None and j.get("oom0") is not None else
              fc is not None and j.get("fc0") is not None and fc > j["fc0"])
    if killed and rc in ("137", None):
        return "ram"
    if _C.CUDA_OOM.search("\n".join(_C.log_tail(os.path.join(j["q"]["logs"], j["name"] + ".log"), 40, fold=False))):
        return "vram"
    return None


def smi(query, fields, *, strict=False, with_ok=False):
    try:
        r = subprocess.run(["nvidia-smi", "--query-%s=%s" % (query, fields), "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=8 if strict else 30, check=strict)
    except (OSError, subprocess.TimeoutExpired):
        if strict:
            raise
        return ([], False) if with_ok else []
    if r.returncode != 0:
        return ([], False) if with_ok else []
    out = r.stdout
    n = fields.count(",") + 1
    lines = out.strip().splitlines()
    bad = any(l.count(",") != n - 1 for l in lines)
    if strict and bad:
        raise ValueError("GPU 查询返回了无法识别的行")
    rows = [[c.strip() for c in l.split(",")] for l in lines if l.count(",") == n - 1]
    return (rows, not bad) if with_ok else rows


def num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def is_darwin():
    """当前环境是否是 macOS（Darwin）；单测可用 LABMON_OS=darwin|linux 覆盖。"""
    ov = os.environ.get("LABMON_OS")
    return ov.lower() == "darwin" if ov else sys.platform == "darwin"


def is_apple_silicon():
    """当前环境是否是 Apple Silicon（macOS + arm64）；单测可用 LABMON_OS / LABMON_ARCH 覆盖。"""
    if not is_darwin():
        return False
    arch = os.environ.get("LABMON_ARCH")
    if not arch:
        try:
            arch = os.uname().machine
        except AttributeError:
            arch = ""
    return (arch or "").lower() in ("arm64", "aarch64")


def sysctl_val(key):
    """读 macOS/BSD `sysctl -n <key>` 单值；失败返回 None。"""
    try:
        r = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_ps_time(s):
    """解析 POSIX/BSD `ps -o time=`（MM:SS.cc、HH:MM:SS、D-HH:MM:SS、MM:SS）为累计 CPU 秒；失败返回 0.0。"""
    s = (s or "").strip()
    if not s:
        return 0.0
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return 0.0
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return days * 86400.0 + int(parts[0]) * 60.0 + float(parts[1])
        if len(parts) == 3:
            return days * 86400.0 + int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
    except ValueError:
        return 0.0
    return 0.0


def parse_ps_procs(text, clk_tck=None):
    """解析 `ps -ax -o pid=,ppid=,pgid=,stat=,rss=,time=,comm=` 输出为与 scan_procs 同形的进程字典（#114）：
    macOS `ps -o sess=` 恒为 0，这里显式置 sid=None，由 job_procs 仅按 ppid + pgid 归集；
    rss（KiB）转字节存入 rss 字段供 mem_of 回退；time 转成与 /proc/<pid>/stat 同口径的 ticks。"""
    if clk_tck is None:
        try:
            clk_tck = os.sysconf("SC_CLK_TCK")
        except (AttributeError, ValueError, OSError):
            clk_tck = 100
    out = {}
    for ln in (text or "").splitlines():
        parts = ln.strip().split(None, 6)
        if len(parts) < 7:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
            rss_kb = int(parts[4])
        except ValueError:
            continue
        st = parts[3][:1] if parts[3] else ""
        ticks = int(round(parse_ps_time(parts[5]) * clk_tck))
        comm = os.path.basename(parts[6].strip()) or parts[6].strip()
        out[pid] = {"comm": comm, "state": st, "ppid": ppid, "pgid": pgid, "sid": None,
                    "ticks": ticks, "hpid": None, "rss": max(0, rss_kb) * 1024}
    return out


def parse_vm_stat(text, total_bytes):
    """解析 macOS `vm_stat` 输出 + 总内存字节（sysctl hw.memsize）→ host_info.mem 口径（#114）：
    - anon（收不回的内存）= (Anonymous pages + Pages wired down + Pages occupied by compressor) × page_size；
    - used（工作集）= max(anon, (Pages active + Pages wired down + Pages occupied by compressor) × page_size)，均封顶到 total_bytes；
    - src 标记为 "vm_stat"。解析失败返回 None。"""
    if not text or not total_bytes or total_bytes <= 0:
        return None
    m = re.search(r"page size of (\d+) bytes", text)
    if m:
        ps = int(m.group(1))
    else:
        try:
            ps = os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, ValueError, OSError):
            ps = 16384
    vals = {}
    for ln in text.splitlines():
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        v = v.strip().rstrip(".")
        if v.isdigit():
            vals[k.strip().lower()] = int(v)
    if not vals:
        return None
    wired = vals.get("pages wired down", 0)
    comp = vals.get("pages occupied by compressor", 0)
    anon_pg = vals.get("anonymous pages", 0)
    active_pg = vals.get("pages active", 0)
    anon_b = min(total_bytes, (anon_pg + wired + comp) * ps)
    used_b = min(total_bytes, max(anon_b, (active_pg + wired + comp) * ps))
    return {"limit": total_bytes / _C.GIB, "used": used_b / _C.GIB, "anon": anon_b / _C.GIB, "src": "vm_stat"}


def darwin_mem_info():
    """读取本机 macOS 统一内存（sysctl hw.memsize + vm_stat）；读不到返回 None。"""
    tot_s = sysctl_val("hw.memsize")
    if not (tot_s and tot_s.isdigit()):
        return None
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        return parse_vm_stat(r.stdout, int(tot_s))
    except (OSError, subprocess.TimeoutExpired):
        return None


def darwin_cpu_quota(nproc):
    """macOS 默认 CPU 配额：留出 MACOS_RESERVE_CPUS（2）核给桌面，至少保留 1 核（#114）。"""
    if not nproc or nproc <= 0:
        return None
    return float(max(1, int(nproc) - _C.MACOS_RESERVE_CPUS))


def mps_gpu(mem=None, limit_gb=None, chip=None):
    """构造 Apple Silicon 单逻辑 MPS 卡（唯一实现，#114）：
    - 保守总容量：配置了 mps_vram_gb > 0 时取 min(mps_vram_gb, 总内存)，否则 min(0.65 × 总内存, max(1, 总内存 − 4 GiB))；
    - 可用显存余量：按统一内存系统余量 avail = max(0, total − anon) 与 cap 取小，不与系统/桌面抢内存；
    - backend="mps", uuid="mps-0", util=None（不采集 Metal 实时利用率）。"""
    m = mem if isinstance(mem, dict) and mem.get("limit") else darwin_mem_info()
    if not m or not m.get("limit"):
        return None
    total_gib = float(m["limit"])
    anon_gib = float(m.get("anon") or 0.0)
    if limit_gb is not None and limit_gb > 0:
        cap_gib = min(float(limit_gb), total_gib)
    else:
        cap_gib = min(_C.MPS_MEM_FRAC * total_gib, max(1.0, total_gib - _C.MPS_MEM_RESERVE_GIB))
    avail_gib = max(0.0, total_gib - anon_gib)
    free_gib = min(cap_gib, avail_gib)
    total_mb = round(cap_gib * 1024.0, 1)
    free_mb = round(free_gib * 1024.0, 1)
    used_mb = round(max(0.0, total_mb - free_mb), 1)
    brand = (chip or sysctl_val("machdep.cpu.brand_string") or "Apple Silicon").strip()
    name = brand if "(MPS)" in brand else "%s (MPS)" % brand
    return {"i": 0, "uuid": "mps-0", "name": name, "backend": "mps",
            "total": total_mb, "used": used_mb, "free": free_mb, "util": None}


def accel_kind(gpus):
    """根据放行的卡列表（卡字典或 UUID 字符串）判定加速后端（唯一实现，#114）："mps" | "cuda" | "cpu"。"""
    if not gpus:
        return "cpu"
    for g in gpus:
        if isinstance(g, dict):
            if g.get("backend") == "mps" or str(g.get("uuid") or "").startswith("mps-"):
                return "mps"
        elif str(g).startswith("mps-"):
            return "mps"
    return "cuda"


def accel_env(env, gpus, accel=None):
    """开跑时导出加速卡环境变量（清单 Worker.start 与单元 slot_main 共用，唯一实现，#114）：
    - Apple Silicon MPS：CUDA_VISIBLE_DEVICES="" + LABMON_ACCEL=mps；
    - CUDA：CUDA_VISIBLE_DEVICES="<uuids>" + LABMON_ACCEL=cuda；
    - 纯 CPU：CUDA_VISIBLE_DEVICES="" + LABMON_ACCEL=cpu。"""
    kind = accel or accel_kind(gpus)
    if kind == "cuda" and gpus:
        uuids = [g["uuid"] if isinstance(g, dict) else str(g) for g in gpus]
        env["CUDA_VISIBLE_DEVICES"] = ",".join(uuids)
        env["LABMON_ACCEL"] = "cuda"
    elif kind == "mps":
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["LABMON_ACCEL"] = "mps"
    else:
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["LABMON_ACCEL"] = "cpu"
    return env


def gpu_table(mps_vram_gb=None, mem=None):
    """各卡：序号、UUID、型号、总/已用/空闲显存（MB）、利用率（%）、可选 backend（如 "mps"）。
    测试时用 LABMON_GPUS="uuid:free_mb[:total_mb[:util[:backend]]],…" 代替 nvidia-smi。
    无 nvidia-smi 且处于 Apple Silicon macOS POSIX 回退模式时，暴露单逻辑 MPS 卡（#114）。"""
    fake = os.environ.get("LABMON_GPUS")
    if fake is not None:
        out = []
        for i, x in enumerate(x for x in fake.split(",") if x):
            p = x.split(":") + [None, None, None]
            free, total = float(p[1]), num(p[2], float(p[1]))
            backend = p[4] or ("mps" if p[0].startswith("mps-") else None)
            u_raw = p[3]
            util = None if (backend == "mps" and (u_raw is None or u_raw == "")) else num(u_raw, 0.0)
            row = {"i": i, "uuid": p[0], "name": "Apple Silicon (MPS)" if backend == "mps" else "fake",
                   "total": total, "used": max(0.0, total - free), "free": free, "util": util}
            if backend:
                row["backend"] = backend
            out.append(row)
        return out
    rows = [{"i": int(r[0]), "uuid": r[1], "name": r[2], "total": num(r[3], 0.0), "used": num(r[4], 0.0),
             "free": num(r[5], 0.0), "util": num(r[6])}
            for r in smi("gpu", "index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu")]
    if rows:
        return rows
    if os.environ.get("LABMON_NO_MPS") != "1" and _C.posix_fallback_ok() and is_apple_silicon():
        g = mps_gpu(mem=mem, limit_gb=mps_vram_gb)
        return [g] if g else []
    return []


def query_gpus(mps_vram_gb=None):
    """[(序号, UUID, 空闲 MB)]。"""
    return [(g["i"], g["uuid"], g["free"]) for g in gpu_table(mps_vram_gb=mps_vram_gb)]


def gpu_procs(*, strict=False, with_ok=False):
    """GPU 上的计算进程（任何人的）：[(宿主机 PID, 显存 MB, UUID)]。
    with_ok=True 时返回 (列表, 查询是否成功)，供 unacct_vram 区分“确实无进程”与“nvidia-smi 超时/失败”。
    测试时用 LABMON_GPU_APPS="pid[:mb[:uuid]],…" 代替 nvidia-smi。"""
    fake = None if strict else os.environ.get("LABMON_GPU_APPS")
    if fake is not None:
        rows = [(int(p[0]), num(p[1], 0.0), p[2]) for p in (x.split(":") + [None, None] for x in fake.split(",") if x)]
        return (rows, True) if with_ok else rows
    if not strict and os.environ.get("LABMON_GPUS") is not None:
        return ([], True) if with_ok else []
    if not strict and _C.posix_fallback_ok() and is_darwin() and not shutil.which("nvidia-smi"):
        return ([], True) if with_ok else []
    if with_ok:
        rows, ok = smi("compute-apps", "pid,used_memory,gpu_uuid", strict=strict, with_ok=True)
    else:
        rows, ok = smi("compute-apps", "pid,used_memory,gpu_uuid", strict=strict), True
    if strict and any(not pid.isdigit() or num(mem, -1) < 0 or not math.isfinite(num(mem, -1)) or not gpu for pid, mem, gpu in rows):
        raise ValueError("GPU 进程列表无法识别")
    out = [(int(r[0]), num(r[1], 0.0), r[2]) for r in rows if r[0].isdigit()]
    return (out, ok) if with_ok else out


def gpu_apps():
    """GPU 上在跑的计算进程 PID（任何人的）。"""
    return [str(p) for p, _, _ in gpu_procs()]


def is_wsl():
    """当前环境是否是 WSL（读 PROC/sys/kernel/osrelease 或 PROC/version；测试可经 LABMON_PROC 注入，#114）。"""
    for rel in ("sys/kernel/osrelease", "version"):
        txt = (_C.read(os.path.join(_C.PROC, rel), "") or "").lower()
        if "microsoft" in txt or "wsl" in txt:
            return True
    return False


def _unacct_why(mb, util, busy):
    parts = []
    if mb >= _C.UNACCT_VRAM_MB:
        parts.append("约 %.1f GiB 显存" % (mb / 1024.0))
    if util is not None and ((busy and mb < _C.UNACCT_BUSY_MB) or util >= _C.UNACCT_UTIL_HOT):
        parts.append("利用率 %.0f%%" % util)
    detail = "、".join(parts) or "显存/算力活跃"
    return "外部（如宿主机）占卡（%s，本环境不可见其进程）" % detail if busy else "外部（如宿主机）占%s" % detail


def unacct_vram(gpus, gp, running_cards=(), prev_busy=()):
    """未归因显存差额识别与外部活跃占卡判定（通用机制，覆盖 WSL2 宿主机占卡与容器 PID/MIG 隔离场景，#114）：
    逐卡计算 raw_mb = max(0, card.used - sum(可见 compute-apps 显存))：
    - 若该卡存在可见进程且驱动未上报单进程显存（mb <= 0，如旧 WDDM 返回 [N/A]），无法可靠求差，不按差额误判；
    - 显存容量扣减（deduct）：raw_mb >= UNACCT_VRAM_MB（512 MB）时记入，过滤驱动与桌面合成器底噪；
    - 外部活跃占卡（busy，带滞回防边缘抖动）：
      * 触发：raw_mb >= UNACCT_BUSY_MB（1536 MB / 1.5 GiB），或该卡无可见 GPU 进程且无我方在跑任务时 util >= UNACCT_UTIL_HOT（15%）；
      * 解除：raw_mb < UNACCT_CLEAR_MB（1024 MB / 1.0 GiB）且（存在可见/我方进程 或 util is None 或 util < UNACCT_UTIL_COOL（5%））；
      外部程序释放显存后下一轮即自动恢复放行。
    返回 {uuid: {"mb", "deduct", "util", "busy", "why"}}（仅包含有扣减或活跃占卡的卡）。"""
    running = set(running_cards or ())
    prev = set(prev_busy or ())
    by_uuid = {}
    for g in gp or ():
        uu = g.get("uuid")
        if uu:
            by_uuid.setdefault(uu, []).append(g)
    out = {}
    for c in gpus or ():
        if c.get("backend") == "mps":             # Apple Silicon MPS 共享统一内存、无独立 compute-apps 列表，跳过未归因显存差额判定
            continue
        uu = c.get("uuid")
        if not uu:
            continue
        card_gp = by_uuid.get(uu) or []
        has_vis = bool(card_gp)
        has_ours = uu in running
        # 驱动对可见进程未上报单进程显存（[N/A] 解析为 0）时无法可靠求差，避免把己方进程误判成外部占卡
        has_unknown_mb = any((g.get("mb") or 0.0) <= 0.0 for g in card_gp)
        used = float(c.get("used") or 0.0)
        vis_mb = sum(float(g.get("mb") or 0.0) for g in card_gp)
        raw_mb = 0.0 if has_unknown_mb else max(0.0, used - vis_mb)
        deduct = round(raw_mb, 1) if raw_mb >= _C.UNACCT_VRAM_MB else 0.0
        util = c.get("util")
        was = uu in prev
        if not was:
            busy = raw_mb >= _C.UNACCT_BUSY_MB or (not has_vis and not has_ours and util is not None and util >= _C.UNACCT_UTIL_HOT)
        else:
            vram_clear = raw_mb < _C.UNACCT_CLEAR_MB
            util_clear = has_vis or has_ours or util is None or util < _C.UNACCT_UTIL_COOL
            busy = not (vram_clear and util_clear)
        if deduct > 0.0 or busy:
            out[uu] = {"mb": round(raw_mb, 1), "deduct": deduct, "util": util, "busy": busy,
                       "why": _unacct_why(raw_mb, util, busy)}
    return out


def unacct_lines(st):
    """status.json 里各卡外部（如宿主机）未归因显存占用说明（labmon worker status 与看板 labmon status 共用，#114）。"""
    info = (st or {}).get("info") or {}
    paused = set(((st or {}).get("room") or {}).get("unacct") or ())
    out = []
    for g in info.get("gpus") or []:
        uu = g.get("uuid")
        mb = g.get("unacct_mb") or 0.0
        busy = bool(g.get("unacct_busy"))
        if not (mb >= _C.UNACCT_VRAM_MB or busy or uu in paused):
            continue
        why = g.get("unacct_why") or ("外部（如宿主机）占约 %.1f GiB 显存" % (mb / 1024.0) if mb >= _C.UNACCT_VRAM_MB else "外部（如宿主机）占卡")
        if uu in paused:
            out.append("GPU %d：%s，默认整卡暂停放新任务（释放后自动恢复）" % (g.get("i", 0), why))
        elif busy:
            out.append("GPU %d：%s（已开启共享放行，按剩余显存调度）" % (g.get("i", 0), why))
        else:
            out.append("GPU %d：%s（已从可用显存与推演中扣除）" % (g.get("i", 0), why))
    return out


# ---- 用量统计（只读 /proc 与 cgroup；macOS 无 /proc 时经 ps/vm_stat/sysctl 回退；读不到就留空，不影响调度）----
def scan_procs():
    """主机/容器里的进程：{pid: {comm, state, ppid, pgid, sid, ticks（含已回收子进程的 CPU 时间）, hpid（宿主机 PID）}}。
    没有 /proc 且启用 POSIX 回退（macOS/BSD）时经 ps 采集并缓存 RSS；否则为空。"""
    out = {}
    try:
        names = os.listdir(_C.PROC)
    except OSError:
        if _C.posix_fallback_ok():
            try:
                r = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,pgid=,stat=,rss=,time=,comm="],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    out = parse_ps_procs(r.stdout)
                    _PROC_RSS.clear()
                    _PROC_RSS.update({pid: p["rss"] for pid, p in out.items() if "rss" in p})
            except (OSError, subprocess.TimeoutExpired):
                pass
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(os.path.join(_C.PROC, n, "stat")) as f:
                st = f.read()
            f = st.rsplit(")", 1)[1].split()
            p = {"comm": st[st.find("(") + 1:st.rfind(")")], "state": f[0], "ppid": int(f[1]), "pgid": int(f[2]), "sid": int(f[3]), "ticks": sum(int(x) for x in f[11:15])}
        except (OSError, IndexError, ValueError):
            continue
        try:
            with open(os.path.join(_C.PROC, n, "sched")) as fh:   # 3.10 内核在容器里也写宿主机 PID：nvidia-smi 给的就是它
                m = re.search(r"\((\d+), #threads", fh.readline())
            p["hpid"] = int(m.group(1)) if m else None
        except OSError:
            p["hpid"] = None
        out[int(n)] = p
    return out


def mem_of(pid, procs=None):
    """进程占的内存（字节）：优先 /proc PSS（共享页按份摊），次选 /proc statm RSS，无 /proc 时回退 ps 采集的 RSS。"""
    for name in ("smaps_rollup", "smaps"):
        try:
            with open(os.path.join(_C.PROC, str(pid), name)) as f:
                return sum(int(l.split()[1]) for l in f if l.startswith("Pss:")) * 1024
        except (OSError, ValueError, IndexError):
            continue
    try:
        with open(os.path.join(_C.PROC, str(pid), "statm")) as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    if procs and pid in procs and "rss" in procs[pid]:
        return int(procs[pid]["rss"])
    return int(_PROC_RSS.get(pid, 0))


def cg(*names):
    for n in names:
        v = _C.read(os.path.join(_C.CG, n))
        if v is not None:
            return v
    return None


def kv(text):
    return {p[0]: p[1] for p in (l.split() for l in (text or "").splitlines()) if len(p) >= 2}


def oom_kills():
    """容器里内核因撞内存上限杀进程的累计次数（cgroup v2 memory.events，v1 memory.oom_control）；读不到为 None。"""
    return num(kv(cg("memory.events", "memory/memory.oom_control")).get("oom_kill"))


def proc_io(pid):
    """进程累计读写的字节 (rchar, wchar)：/proc/<pid>/io，含已回收的子进程（内核在回收时并进父进程）；读不到（刚退出、没权限）为 (0, 0)。
    rchar、wchar 是经 read/write 系统调用的量，共享盘（NFS）与命中页缓存的都算，不只是落到本地盘的。"""
    try:
        d = kv(_C.read(os.path.join(_C.PROC, str(pid), "io"), "").replace(":", " "))
        return int(d.get("rchar", 0)), int(d.get("wchar", 0))
    except (TypeError, ValueError, AttributeError):
        return 0, 0


def ints(xs):
    try:
        return [int(x) for x in xs]
    except ValueError:
        return None


def nfs_counts(path):
    """/proc/self/mountstats 里 path 所在的 NFS 挂载（挂载点是 path 或它最近的上级）：{addr: clientaddr（同一物理机的几个容器相同）,
    ops: READ+WRITE 累计次数, ms: 两项累计执行时间（ms，含客户端排队）, bytes: 读写的字节}；不是 NFS 或读不到为 None。
    计数是宿主机挂载的，同一物理机的几个容器共用一份，只能看差值。"""
    best, cur = None, None
    for line in (_C.read(os.path.join(_C.PROC, "self", "mountstats"), "") or "").splitlines():
        if line.startswith("device "):
            m, cur = re.match(r"device \S+ mounted on (\S+) with fstype nfs", line), None
            mp = m.group(1).rstrip("/") or "/" if m else None
            if mp and (path == mp or path.startswith(mp.rstrip("/") + "/")) and (best is None or len(mp) > len(best[0])):
                cur = {"addr": None, "ops": 0, "ms": 0, "bytes": 0}
                best = (mp, cur)
            continue
        p = line.split()
        if cur is None or not p:
            continue
        if p[0] == "opts:":
            a = re.search(r"clientaddr=([^,\s]+)", line)
            cur["addr"] = a.group(1) if a else None
        elif p[0] in ("READ:", "WRITE:") and len(p) >= 9 and ints(p[1:9]):
            n = ints(p[1:9])                  # 次数 传输 超时 发出字节 收到字节 排队 往返 执行（后三项累计 ms）
            cur["ops"] += n[0]
            cur["ms"] += n[7]
            cur["bytes"] += n[4] if p[0] == "READ:" else n[3]
    return best[1] if best else None


def local_dev(path="/tmp"):
    """本地盘（容器的 /tmp）背后的块设备号「主:次」：cgroup 里本容器读写过最多的那块（v1 blkio、v2 io.stat）；
    读不到时用 mountinfo 里 path 所在挂载的设备号（overlay 的是假设备 0:xx，不算）。没有为 None。"""
    tot = {}
    for line in (cg("blkio/blkio.throttle.io_service_bytes", "blkio/blkio.io_service_bytes") or "").splitlines():
        p = line.split()
        if len(p) == 3 and p[1] in ("Read", "Write") and p[2].isdigit():
            tot[p[0]] = tot.get(p[0], 0) + int(p[2])
    for line in (cg("io.stat") or "").splitlines():
        p = line.split()
        d = dict(x.split("=", 1) for x in p[1:] if "=" in x)
        if p and ints([d.get("rbytes", 0), d.get("wbytes", 0)]):
            tot[p[0]] = tot.get(p[0], 0) + sum(ints([d.get("rbytes", 0), d.get("wbytes", 0)]))
    if tot and max(tot.values()) > 0:
        return max(tot, key=tot.get)
    best = None
    for line in (_C.read(os.path.join(_C.PROC, "self", "mountinfo"), "") or "").splitlines():
        p = line.split()
        if len(p) > 4 and (path == p[4] or path.startswith(p[4].rstrip("/") + "/")) and (best is None or len(p[4]) >= len(best[1])):
            best = (p[2], p[4])
    return best[0] if best and not best[0].startswith("0:") else None


def disk_counts(dev):
    """/proc/diskstats 里设备号 dev 那块盘：{name, ios: 读写完成次数, wait: 读写累计用时 ms, busy: 有读写在做的累计 ms}；没有为 None。
    容器里没有 lxcfs 时这是宿主机的计数，同一物理机的几个容器共用这块盘。"""
    for line in (_C.read(os.path.join(_C.PROC, "diskstats"), "") or "").splitlines():
        p = line.split()
        if len(p) >= 14 and "%s:%s" % (p[0], p[1]) == dev and ints(p[3:14]):
            n = ints(p[3:14])                 # 读次数 合并 扇区 读 ms 写次数 合并 扇区 写 ms 在做 忙 ms 加权 ms
            return {"name": p[2], "ios": n[0] + n[4], "wait": n[3] + n[7], "busy": n[9]}
    return None


def psi():
    """/proc/pressure 的 some avg60（%：近 60 秒里至少有一个任务在等这项资源的时间比例）{cpu, io, memory}；内核没有（4.20 之前，如 3.10）为 None。"""
    out = {}
    for k in ("cpu", "io", "memory"):
        m = re.search(r"^some .*avg60=([\d.]+)", _C.read(os.path.join(_C.PROC, "pressure", k), "") or "", re.M)
        if m:
            out[k] = float(m.group(1))
    return out or None


def press_raw(dev, t):
    """主机压力的累计计数（每轮读一次，两份求差见 press_rates）：CPU 被限流的周期（cgroup cpu.stat）、共享盘读写（nfs_counts）、
    本地盘（disk_counts）；PSI 本身就是均值，直接带上。读不到的项为 None。"""
    st = kv(cg("cpu/cpu.stat", "cpu,cpuacct/cpu.stat", "cpu.stat"))
    thr = ints([st.get("nr_periods", "x"), st.get("nr_throttled", "x")])
    return {"t": t, "thr": thr, "nfs": nfs_counts(_C.DATA_ROOT), "disk": disk_counts(dev) if dev else None, "psi": psi()}


def press_rates(a, b):
    """两份计数（press_raw）之间的平均：thr 被限流的周期 %，共享盘 ms（READ+WRITE 每次平均执行时间，这段没有读写为 None）、
    ops 每秒次数、mib 每秒 MiB，本地盘 busy 忙碌 %、await 每次平均用时 ms。计数变小（容器重启、计数归零、换了设备）的项为 None。"""
    out, dt = dict.fromkeys(("thr", "ms", "ops", "mib", "busy", "await")), b["t"] - a["t"]
    if dt <= 0:
        return out
    if a["thr"] and b["thr"]:
        dp, dn = b["thr"][0] - a["thr"][0], b["thr"][1] - a["thr"][1]
        if dp > 0 and dn >= 0:
            out["thr"] = round(100 * dn / dp, 1)
    x, y = a["nfs"], b["nfs"]
    if x and y and all(y[k] >= x[k] for k in ("ops", "ms", "bytes")):
        do = y["ops"] - x["ops"]
        out.update(ops=round(do / dt, 1), mib=round((y["bytes"] - x["bytes"]) / dt / _C.MIB, 2), ms=round((y["ms"] - x["ms"]) / do, 2) if do else None)
    x, y = a["disk"], b["disk"]
    if x and y and x["name"] == y["name"] and all(y[k] >= x[k] for k in ("ios", "wait", "busy")):
        di = y["ios"] - x["ios"]
        out.update(busy=round(min(100.0, (y["busy"] - x["busy"]) / dt / 10), 1), **{"await": round((y["wait"] - x["wait"]) / di, 2) if di else None})
    return out


def io_base(rows, now):
    """共享盘慢的基线（ms，每次读写的执行时间）：近 PRESS_KEEP_S（14 天）press.jsonl 里有读写（每秒至少 IO_MIN_OPS 次）的 5 分钟样本的 95 分位，
    且不低于 IO_SLOW_MS；样本不到 IO_BASE_N 个时取 IO_SLOW_MS。返回 (基线, 样本数)。判“共享盘慢”（nfs_slow）用它（#4 维护者定）。"""
    xs = sorted(r["ms"] for r in rows if r.get("ms") is not None and (r.get("ops") or 0) >= _C.IO_MIN_OPS and (r.get("t") or 0) >= now - _C.PRESS_KEEP_S)
    if len(xs) < _C.IO_BASE_N:
        return float(_C.IO_SLOW_MS), len(xs)
    return max(float(_C.IO_SLOW_MS), xs[math.ceil(0.95 * len(xs)) - 1]), len(xs)


def nfs_slow(nfs):
    """共享盘此刻慢不慢（状态的 info.press.nfs）：近 1 分钟每次读写的执行时间超过本机基线（io_base），且每秒至少 IO_MIN_OPS 次读写
    （几次小写的延迟不算）。扫描与拷本地依赖让路（Worker.io_yield）、读写拥塞嫌疑（Worker.io_suspect）共用。"""
    return bool(nfs and nfs.get("ms") is not None and (nfs.get("ops") or 0) >= _C.IO_MIN_OPS and nfs["ms"] > nfs["base"])


def cpu_env(env, n):
    """开跑时给任务的核数进环境（清单任务 Worker.start 与单元 slot_main 共用）：导出 LABMON_CPUS，THREAD_VARS 没设（或为空）的缺省设成它（#38）。
    已设的不盖：worker 与申请方自己环境里的在这里留着；清单的 .env 与清单行前缀的赋值在这之后才生效（bash -c 里 source、eval），本来就盖过缺省。"""
    env["LABMON_CPUS"] = str(n)
    for k in _C.THREAD_VARS:
        if not env.get(k):
            env[k] = str(n)
    return env


def thr_step(since, thr, t):
    """这一段 CPU 限流从何时起（Worker.press 每轮调，状态的 throttle 与 fit 都按它）：没在限流时 thr（近 1 分钟被限流的周期 %）≥ THR_HOT 从 t 起算；
    限流中降到 THR_COOL 以下才结束（None）。thr 为 None（刚起、计数归零）时沿用原状态。"""
    if thr is None:
        return since
    if since is None:
        return t if thr >= _C.THR_HOT else None
    return None if thr < _C.THR_COOL else since


def cpu_over(u, give, t):
    """运行中任务超核没有：此刻的核数（use 里最近一个 1 分钟窗口 pks，没有或过了两个窗口取每轮的 cores）≥ 给的核数 give × CPU_OVER_RATIO
    且多出至少 CPU_OVER_MIN 核时返回这个核数，否则 None。限流时指出是谁（Worker.throttle）用它。"""
    pk = (u.get("pks") or [None])[-1]
    c = pk[1] if pk and t - pk[0] <= 2 * _C.PEAK_WIN else u.get("cores")
    return c if c is not None and c >= give * _C.CPU_OVER_RATIO and c - give >= _C.CPU_OVER_MIN else None


def job_cpus(v):
    """核数取整（CPU 记账与开跑时导出的 LABMON_CPUS 同一个口径）：四舍五入、至少 1；写明 0 的是 0（不记账，导出时仍给 1）。配额另按向下取整（cpu_cap）。"""
    return 0 if v == 0 else max(1, int(v + 0.5))


def cpu_cap(quota):
    """记账用的 CPU 上限：容器配额向下取整（7.5 核按 7）；没设上限为 None（不记 CPU）。"""
    return max(1, int(quota)) if quota else None


def cpu_quota():
    """容器的 CPU 配额（核数，cgroup v1 / v2；没设上限或读不到为 None）。"""
    q, per = cg("cpu/cpu.cfs_quota_us"), cg("cpu/cpu.cfs_period_us")
    if q is None and cg("cpu.max"):
        q, per = (cg("cpu.max").split() + ["100000"])[:2]
    return int(q) / int(per) if q and q.lstrip("-").isdigit() and int(q) > 0 and per and per.isdigit() else None


def host_info(gpus, data_root):
    """主机现状：容器内存（cgroup v1 / v2，读不到用 /proc/meminfo）、CPU 配额与累计用时、磁盘、容器启动时间、各卡。
    内存的 used 是工作集（用量 − 不活跃页缓存，只显示）；anon 是收不回的（用量 − 文件页，mem_unevictable），放行（mem_room）、内存闸门、
    借来的主机上算对方用量与快撞上限都用它：我们的任务读数据留下的活跃页缓存也在工作集里，内存紧时内核先回收它，不能算成别人的内存。"""
    h = {"gpus": gpus, "nproc": os.cpu_count()}
    lim, use = cg("memory/memory.limit_in_bytes", "memory.max"), cg("memory/memory.usage_in_bytes", "memory.current")
    if lim and lim.isdigit() and int(lim) < 2 ** 60 and use and use.isdigit():
        st = kv(cg("memory/memory.stat", "memory.stat"))
        stat = lambda k: int(st.get("total_" + k, st.get(k, 0)))      # noqa: E731 — v1 取含子组的 total_ 项，v2 只有不带前缀的
        h["mem"] = {"limit": int(lim) / _C.GIB, "used": (int(use) - stat("inactive_file")) / _C.GIB,
                    "anon": mem_unevictable(int(use), stat("active_file"), stat("inactive_file")) / _C.GIB, "src": "cgroup",
                    "peak": num(cg("memory/memory.max_usage_in_bytes", "memory.peak"), 0) / _C.GIB or None,
                    "failcnt": num(cg("memory/memory.failcnt")), "oom_kill": oom_kills()}
    else:
        mi = {k.rstrip(":"): int(v) * 1024 for k, v in kv(_C.read(os.path.join(_C.PROC, "meminfo"))).items() if v.isdigit()}
        if mi.get("MemTotal"):
            h["mem"] = {"limit": mi["MemTotal"] / _C.GIB, "used": (mi["MemTotal"] - mi.get("MemAvailable", 0)) / _C.GIB,
                        "anon": (mi.get("AnonPages", 0) + mi.get("Shmem", 0)) / _C.GIB, "src": "meminfo"}
        elif _C.posix_fallback_ok() and is_darwin():
            dm = darwin_mem_info()
            if dm:
                h["mem"] = dm
    h["cpu_quota"] = cpu_quota()
    if _C.posix_fallback_ok() and is_darwin():
        h["os"] = "darwin"
        if not h.get("nproc"):
            nc = sysctl_val("hw.ncpu")
            if nc and nc.isdigit():
                h["nproc"] = int(nc)
        if h["cpu_quota"] is None and h.get("nproc"):
            h["cpu_quota"] = darwin_cpu_quota(h["nproc"])
            h["cpu_quota_src"] = "macos_default"
    usage = cg("cpuacct/cpuacct.usage", "cpu,cpuacct/cpuacct.usage")
    if usage is None and cg("cpu.stat"):
        usage = str(int(kv(cg("cpu.stat")).get("usage_usec", 0)) * 1000)
    h["cpu_ns"] = int(usage) if usage and usage.isdigit() else None
    h["disk"] = []
    try:
        s = os.statvfs(data_root)
        h["disk"].append({"path": data_root, "free": s.f_bavail * s.f_frsize / _C.GIB, "total": s.f_blocks * s.f_frsize / _C.GIB,
                          "kind": "local" if os.environ.get("LABMON_STANDALONE") == "1" else "shared"})
    except OSError:
        pass
    h["started"] = _C.started_at(1)          # 容器启动时间 = PID 1 的启动时刻（服务器"重启"多半只是重启容器）
    h["rootfs"] = rootfs_info()
    if is_wsl():
        h["wsl"] = True
    return h


def rootfs_info():
    """容器根盘（ROOTFS）开机以来的新写入，续租、关机、换人会清空（#149）。单位 GiB：used 已用、total 总量（容器的配额）、fs 文件系统类型。
    ok：根盘是 overlay、总量比宿主机的盘（/etc/hosts 由宿主机绑定挂进来，statvfs 它得到的是宿主盘）小一成以上，即有项目配额：这时已用只算这个容器
    上层写的东西，就是续租会清掉的量；否则（不是 overlay、看不出宿主盘、没配额）测不准，LocalDisk 也不扫整个根盘。
    shm：/dev/shm 是容器私有的（挂载来源是 shm）时它的已用（续租同样清掉）；与宿主共用的 tmpfs不算这台的。读不到根盘为 None。"""
    mounts = {}
    for ln in (_C.read(os.path.join(_C.PROC, "mounts"), "") or "").splitlines():
        f = ln.split()
        if len(f) >= 3:
            mounts[f[1]] = (f[0], f[2])
    try:
        s = os.statvfs(_C.ROOTFS)
    except OSError:
        return None
    total, used = s.f_blocks * s.f_frsize, (s.f_blocks - s.f_bfree) * s.f_frsize
    try:
        e = os.statvfs(os.path.join(_C.ROOTFS, "etc/hosts"))
        host = e.f_blocks * e.f_frsize
    except OSError:
        host = None
    fs = mounts.get("/", (None, None))[1]
    out = {"used": round(used / _C.GIB, 3), "total": round(total / _C.GIB, 3), "fs": fs, "ok": fs == "overlay" and bool(host) and total < 0.9 * host}
    if mounts.get("/dev/shm", ("",))[0] == "shm":
        try:
            d = os.statvfs(os.path.join(_C.ROOTFS, "dev/shm"))
            out["shm"] = round((d.f_blocks - d.f_bfree) * d.f_frsize / _C.GIB, 3)
        except OSError:
            pass
    return out


def cpu_since(ticks, prev):
    """一棵进程树这一段用掉的 CPU 秒：本轮各进程累计（ticks，含已回收子进程的）之和比上一轮之和的增量，负数按 0。
    子进程被回收时它一生的 CPU 并进父进程，按整棵树求和不重不漏；回收者不在树里、整棵树没了时差值为负，按 0。
    任务的用量（measure）与保活进程树的核数（Worker.ka_cores）共用。prev 为 None（上一轮没量过）时是 0。"""
    return max(0.0, (ticks - prev) / os.sysconf("SC_CLK_TCK")) if prev is not None else 0.0


def job_procs(procs, jobs, self_pid=None):
    """各任务此刻的进程 {id(任务): [pid]}：统一经 proc_tree 归集，排除 worker 自身子树及其他运行中任务的根树。
    - 单元（slot）先归集：按根进程 PPID 树认；当根拥有独立进程组（pgid == root_pid 且不同于 worker 自身 pgid）时，
      并入同 pgid 进程及其子孙（脱壳/孤儿子进程不漏计；与其他进程共享 pgid 时不按 pgid 合并，避免误拉）。
    - 清单任务（及旧 gpu_queue.sh 的 ext）后归集：按根进程 PPID 树认；根拥有独立会话（sid == root_pid 且不同于 worker 自身 sid）时
      并入同 sid 进程，拥有独立进程组（pgid == root_pid 且不同于 worker 自身 pgid）时并入同 pgid 进程，再统一展开其子孙；
      已归属单元（如登记成清单任务的监督进程所起的单元）或其他任务的进程不重复记。"""
    wpid = os.getpid() if self_pid is None else self_pid
    wp = procs.get(wpid) or {}
    w_pgid, w_sid = wp.get("pgid"), wp.get("sid")
    all_roots = {j["pid"] for j in jobs}
    worker_pids = set(proc_tree(procs, [wpid], exclude=all_roots - {wpid})) | ({wpid} if wpid is not None else set())

    def collect(j, claimed, use_sid):
        rpid = j["pid"]
        excl = worker_pids | claimed | (all_roots - {rpid})
        p = procs.get(rpid)
        roots, pgids = [rpid], ()
        if p is not None:
            if p.get("pgid") == rpid and rpid != w_pgid:
                pgids = (rpid,)
            if use_sid and p.get("sid") == rpid and rpid != w_sid:
                roots += [pid for pid, x in procs.items() if x.get("sid") == rpid]
        else:
            if rpid != w_pgid:
                pgids = (rpid,)
            if use_sid and rpid != w_sid:
                roots += [pid for pid, x in procs.items() if x.get("sid") == rpid]
        return proc_tree(procs, roots, pgids=pgids, exclude=excl)

    mine, claimed = {}, set()
    for is_slot in (True, False):
        for j in jobs:
            if (j.get("kind") == "slot") != is_slot:
                continue
            pids = collect(j, claimed, use_sid=not is_slot)
            mine[id(j)] = pids
            claimed.update(pids)
    return mine


def win_mean(w, t, span, n=3):
    """利用率窗口 w [(时刻, 值)] 里近 span 秒的样本平均（一位小数）；不到 n 个样本为 None。
    「在用时多高」（util10）与「吃满程度」（gpu_busy）两种口径共用（Worker.util_window 记样本，#51）。"""
    v = [x[1] for x in w if t - x[0] <= span]
    return round(sum(v) / len(v), 1) if len(v) >= n else None


def job_sus(j, now):
    """运行中或刚结束的任务累计被 SIGSTOP 挂起的秒数（measure 记在 use.sus_s，夹在 [0, now - t0] 内）。
    Worker.usage_rec、Worker.finish、Worker.run_end 与 run_tot 共用（#149）。"""
    wall = max(0.0, now - j["t0"])
    return min(wall, max(0.0, float((j.get("use") or {}).get("sus_s") or 0.0)))


def card_conc(j, on, jobs):
    """此刻任务 j 所用的卡 on（非空）上平均几条 labmon 任务（含自己；jobs 里的占位任务不算，按对象去重，多卡取各卡平均）。
    measure 按它累计按时间加权的 conc（用量行同名字段，duration 挑记录比的就是它），Worker.k_of 在还没量到 conc 时按它回退（#67、#86）。"""
    return sum(len({id(o) for o in jobs if not _C.is_hold(o["name"]) and g in (o.get("gpus") or [])} | {id(j)}) for g in on) / len(on)


class MeasureMixin:

    def press(self, t):
        """主机的 CPU 与 IO 压力（状态的 info.press）：近 PRESS_WIN 秒的计数差（press_rates）、PSI、共享盘延迟的基线（io_base）。
        每 PRESS_EVERY 秒往 press.jsonl 记一行这段时间的聚合值（共享盘上本机的状态目录，容器重启不丢，留 14 天）。
        worker 刚启动的第一轮没有上一份计数，速率为空。准入与推演不看它；共享盘慢（nfs 的 slow）时让路与读写拥塞嫌疑用它。"""
        if self.pdev is None and t - self.pdev_t >= _C.PRESS_EVERY:
            self.pdev, self.pdev_t = local_dev(), t
        raw = press_raw(self.pdev, t)
        self.pwin.append(raw)
        while len(self.pwin) > 2 and t - self.pwin[1]["t"] >= _C.PRESS_WIN:   # 起点留最近一份满 PRESS_WIN 秒的
            self.pwin.pop(0)
        if self.plast is None:
            self.plast = raw
        elif t - self.plast["t"] >= _C.PRESS_EVERY:
            row = dict(press_rates(self.plast, raw), t=round(t))
            self.prows = [r for r in self.prows if r["t"] >= t - _C.PRESS_KEEP_S] + [row]
            self.plast = raw
            try:              # 写不进只少一行历史（基线照用内存里的），不拖垮这一轮采样；下个 PRESS_EVERY 再试，连续失败只记一次日志
                _C.append_jsonl(self.p.press, row, _C.PRESS_KEEP_S)
                self.pfail = False
            except OSError as e:
                if not self.pfail:
                    self.wlog("press.jsonl 写不进（%s）：压力历史先只留在内存，每 %d 秒再试" % (e, _C.PRESS_EVERY))
                self.pfail = True
        r = press_rates(self.pwin[0], raw) if len(self.pwin) > 1 else dict.fromkeys(("thr", "ms", "ops", "mib", "busy", "await"))
        self.thr_since = thr_step(self.thr_since, r["thr"], t)
        base, n = io_base(self.prows, t)
        nf = {"path": _C.DATA_ROOT, "addr": raw["nfs"]["addr"], "ms": r["ms"], "ops": r["ops"], "mib": r["mib"], "base": base, "n": n,
              "need": _C.IO_BASE_N, "floor": _C.IO_SLOW_MS, "min_ops": _C.IO_MIN_OPS} if raw["nfs"] else None
        if nf:
            nf["slow"] = nfs_slow(nf)
        return {"thr": r["thr"], "psi": raw["psi"], "nfs": nf,
                "disk": {"dev": raw["disk"]["name"], "busy": r["busy"], "await": r["await"]} if raw["disk"] else None}

    def throttle(self, now):
        """状态的 throttle（#39）：这一段 CPU 限流（thr_step）的起点 since、已持续 for 秒、此刻的 thr、持续满 THR_HOLD_S 的 alert，
        超核的任务 over [{project, name, cores, give}]（cpu_over；give 是开跑时导出的 LABMON_CPUS，按核数从多到少；占位任务不算），
        labmon 之外此刻占的核 other（不含保活，含旧 gpu_queue.sh 的任务，同 cpu_room；算不出为 None）。没在限流为 None。看板的告警与 fit 都只读它，门槛只在这里。"""
        if self.thr_since is None:
            return None
        over = []
        for j in self.running.values():
            if _C.is_hold(j["name"]) or not j.get("est"):
                continue
            give = self.cpus_for(j)
            c = cpu_over(j.get("use") or {}, give, now)
            if c is not None:
                over.append({"project": j.get("project"), "name": j["name"], "cores": round(c, 1), "give": give})
        oc = self.other["cpu"]
        if oc is not None:                    # 旧 gpu_queue.sh 的任务不记账，此刻用的核算 labmon 之外（同 cpu_room）
            oc += sum((j.get("use") or {}).get("cores") or 0.0 for j in self.ext.values())
        return {"since": round(self.thr_since), "for": round(now - self.thr_since), "thr": (self.info.get("press") or {}).get("thr"),
                "alert": now - self.thr_since >= _C.THR_HOLD_S, "over": sorted(over, key=lambda o: -o["cores"]),
                "other": None if oc is None else round(oc, 1)}

    def io_yield(self, key, now):
        """共享盘慢（nfs_slow）时推迟一件读写共享盘的事（扫描 "scan"、本地盘扫描 "lscan"、拷本地依赖 (请求 key, 任务)）：返回推迟的说明；不慢、或这件事已连着推迟满
        IO_YIELD_MAX_S 时返回 None（满了就照做这一次、计时清零：一直慢时每 IO_YIELD_MAX_S 做一件，不会永远不扫、不拷）。开始推迟时记一次 worker.log。"""
        nf = self.slow
        if not nf:
            self.defer.pop(key, None)
            return None
        t0 = self.defer.setdefault(key, now)
        if now - t0 >= _C.IO_YIELD_MAX_S:
            self.defer.pop(key)
            self.wlog("共享盘仍慢，已推迟满 %d 分钟，照做一次%s" % (_C.IO_YIELD_MAX_S / 60, _C.IO_WHAT.get(key) or "拷 %s 的本地依赖" % key[1]))
            return None
        why = "共享盘慢（此刻 %.0f ms/次，本机基线 %.0f）" % (nf["ms"], nf["base"])
        if t0 == now:
            self.wlog("%s：推迟%s" % (why, _C.IO_WHAT.get(key) or "拷 %s 的本地依赖" % key[1]))
        return why

    def ext_jobs(self, queues):
        """旧 gpu_queue.sh 正在跑的任务（状态目录的 current 指着、lock 是 gpu_queue 的）：只统计用量，不管；沿用上一轮的记录（用量累计在里面）。"""
        ext = {}
        for q in queues:
            if q.get("sleeping"):
                continue
            cur = None if self.lane(q["key"]) else (_Q.read_current(q["state"]) or [None])[0]   # 旧 gpu_queue.sh 一次只跑一个
            if not cur or not cur[1] or not _C.alive(_C.read_pid(os.path.join(q["state"], "lock")), ("gpu_queue",)):
                continue
            k = q["key"] + ":" + cur[0]
            ext[k] = self.ext.get(k) or {"name": cur[0], "pid": cur[1], "t0": cur[2], "q": q, "gpus": [], "use": {}, "ext": True,
                                         "project": q["project"], "excl": None, "cmd": _Q.job_cmd(q, cur[0]) or ""}
        return ext

    def util10(self, t):
        """各卡近 UTIL_WIN_S 的「在用时多高」{UUID: %}（不算保活在的那几轮；样本少于 3 个的不列，win_mean）：状态的 util10（fit 读它）、同机多开车道的门槛（local_cards）共用。"""
        return {k: round(v) for k, w in self.uwin.items() for v in [win_mean(w, t, _C.UTIL_WIN_S)] if v is not None}

    def util_window(self, gpus, gp, t):
        """各卡利用率的两个窗口（#51），都按 win_mean 取平均：
        uwin「在用时多高」近 UTIL_WIN_S 秒，卡上有保活的那几轮不记（它只是占着卡；util10，fit 列出来看）；
        bwin「吃满程度」近 BUSY_WIN_S 秒，卡上只有保活（ka.sh 管的与停不了的）的那几轮记 0（gpu_busy，页面上的利用率）。"""
        ka_on = {g["uuid"] for g in gp if g["owner"] == "keepalive"}
        work = {g["uuid"] for g in gp if g["owner"] != "keepalive" and not g.get("ka")}
        only_ka = {g["uuid"] for g in gp if g["owner"] == "keepalive" or g.get("ka")} - work
        self.bka = {}
        for g in gpus:
            w = self.uwin.setdefault(g["uuid"], [])
            b = self.bwin.setdefault(g["uuid"], [])
            if g.get("util") is not None:
                if g["uuid"] not in ka_on:
                    w.append((t, g["util"]))
                b.append((t, 0 if g["uuid"] in only_ka else g["util"]))
                if g["uuid"] in only_ka:
                    self.bka[g["uuid"]] = g["util"]
            w[:] = [x for x in w if t - x[0] <= _C.UTIL_WIN_S]
            b[:] = [x for x in b if t - x[0] <= _C.BUSY_WIN_S]

    def busy_view(self, t):
        """状态的 gpu_busy：各卡「吃满程度」{now 此刻, m10 近 10 分钟, h1 近 1 小时, cov 1 小时窗口实际覆盖的秒数（worker 刚起时不满）,
        ka 此刻只有保活时保活顶出来的原值}。样本不够 3 个的平均为 None。"""
        out = {}
        for k, b in self.bwin.items():
            if not b:
                continue
            out[k] = {"now": b[-1][1], "m10": win_mean(b, t, _C.UTIL_WIN_S), "h1": win_mean(b, t, _C.BUSY_WIN_S), "cov": round(t - b[0][0]),
                      **({"ka": self.bka[k]} if k in self.bka else {})}
        return out

    def measure(self, j, pids, procs, gp, util, ovram, jobs, t, unacct=None):
        """量一个任务这一轮的用量（写进 j["use"]）：内存（PSS 之和）与峰值、核数与累计 CPU、读写（进程树 /proc/<pid>/io 之和：此刻 MiB/s、
        累计字节 rd_b / wr_b）、核数与读写的 1 分钟峰值（PEAK_WIN 秒一个窗口）、近 CPU_WARM_S 内各 1 分钟窗口的核数 pks [[窗口结束时刻, 核数]]
        与从何时起量它的 pk0（CPU 记账的回收按它们，cpu_warm、Worker.acct）、同卡并行数 conc、显存（按卡）与峰值、所用卡的平均利用率，
        以及运行期间同卡、同机还有谁（with_gpu、with_host）、所用的卡上有没有过 labmon 之外的进程（owner_gpu）。返回此刻所用的卡。
        CPU（cpu_since）与读写都是累计计数求差：子进程被回收时并进父进程，求和不重不漏；回收者不在树里时那一轮差值为负，按 0 算。"""
        u = j.setdefault("use", {})
        ram = sum(mem_of(p, procs) for p in pids) / _C.GIB
        ticks = sum(procs[p]["ticks"] for p in pids)
        io = [proc_io(p) for p in pids]
        rd, wr = sum(x[0] for x in io), sum(x[1] for x in io)
        vr = [g for g in gp if g["_j"] is j]
        vram, vb = sum(g["mb"] for g in vr), {}
        for g in vr:
            vb[g["uuid"]] = vb.get(g["uuid"], 0.0) + g["mb"]
        if j.get("ext") and vr:
            j["gpus"] = sorted(vb)
        on = set(j["gpus"]) | set(vb)
        if any(ovram.get(x) for x in on):
            j["owner_gpu"] = True             # 运行期间所用的卡上有过 labmon 之外的进程（借来的主机上据此判断 CUDA OOM 是不是被挤的）
        stopped = any(procs[p].get("state") == "T" for p in pids)
        if u.get("t") and t > u["t"]:
            if stopped and u.get("sus_on") and t - u["t"] <= _C.STATUS_STALE_S:
                u["sus_s"] = round(u.get("sus_s", 0.0) + t - u["t"], 1)
            dc = cpu_since(ticks, u.get("ticks"))
            u["cores"] = round(dc / (t - u["t"]), 2)
            u["cpu_s"], u["wall_s"] = u.get("cpu_s", 0) + dc, u.get("wall_s", 0) + t - u["t"]
            drd, dwr = max(0, rd - u.get("rchar", rd)), max(0, wr - u.get("wchar", wr))
            u["rd"], u["wr"] = round(drd / (t - u["t"]) / _C.MIB, 2), round(dwr / (t - u["t"]) / _C.MIB, 2)
            u["rd_b"], u["wr_b"] = u.get("rd_b", 0) + drd, u.get("wr_b", 0) + dwr
            pw = u.get("pw")                  # 峰值窗口的起点 [时刻, 累计 CPU 秒, 累计读, 累计写]
            if pw and t - pw[0] >= _C.PEAK_WIN:
                w = t - pw[0]
                for k, v in (("cores_peak", (u["cpu_s"] - pw[1]) / w), ("rd_peak", (u["rd_b"] - pw[2]) / w / _C.MIB), ("wr_peak", (u["wr_b"] - pw[3]) / w / _C.MIB)):
                    u[k] = round(max(v, u.get(k) or 0), 2)
                # 近 CPU_WARM_S 内各 1 分钟窗口的核数 [[窗口结束时刻, 核数]]：CPU 记账的回收（Worker.acct）按它
                u["pks"] = [x for x in u.get("pks") or [] if t - x[0] < _C.CPU_WARM_S] + [[round(t), round((u["cpu_s"] - pw[1]) / w, 2)]]
            if not pw or t - pw[0] >= _C.PEAK_WIN:
                u["pw"] = [t, u["cpu_s"], u["rd_b"], u["wr_b"]]
            if on:                            # 同卡平均几条 labmon 任务（含自己，按时间加权；多卡取各卡平均）：duration 按它挑同类记录（#67）
                k = card_conc(j, on, jobs)
                u["conc_s"], u["conc_w"] = u.get("conc_s", 0) + k * (t - u["t"]), u.get("conc_w", 0) + t - u["t"]
                u["conc"] = round(u["conc_s"] / u["conc_w"], 2)
            ut = [util[g] for g in on if util.get(g) is not None]
            if ut:                            # 所用卡的平均利用率（同卡有别人时是合计，记账时据 with_gpu 区分）
                u["util_s"], u["util_wall"] = u.get("util_s", 0) + sum(ut) / len(ut) * (t - u["t"]), u.get("util_wall", 0) + t - u["t"]
                u["util_mean"] = round(u["util_s"] / u["util_wall"])
        u.setdefault("pk0", round(t))         # 从何时起量窗口核数（pks）：开跑的就是开跑时，接管旧 worker 的从接管起，满 CPU_WARM_S 才回收
        if stopped:
            u["sus_on"] = True
        else:
            u.pop("sus_on", None)
        u.update(t=t, ticks=ticks, rchar=rd, wchar=wr, nproc=len(pids), ram=round(ram, 2), ram_peak=round(max(ram, u.get("ram_peak") or 0), 2),
                 vram=vram, vram_peak=max(vram, u.get("vram_peak") or 0), vram_by=vb)
        card, host = (sum(_C.intruders(s, on, jobs, gp, None, j, unacct=unacct), []) for s in ("gpu", "host"))
        u["with_host"] = sorted(set(u.get("with_host", [])) | set(host))[:8]
        u["with_gpu"] = sorted(set(u.get("with_gpu", [])) | set(card))[:8]
        return on

    def mem_events(self, m):
        """内存闸门（收不回的内存到上限的 GATE；页缓存内核会先回收，不算）与撞上限次数：变化时记事件。"""
        if not m.get("limit"):
            return
        on = m["anon"] / m["limit"] >= _C.GATE
        if on != self.gate:
            self.event("gate", "内存闸门%s：收不回的内存 %.1f / %.1f GiB" % ("关闭，停止放行" if on else "打开，恢复放行", m["anon"], m["limit"]))
            self.gate = on
        fc = m.get("failcnt")
        if fc is not None and self.failcnt is not None and fc > self.failcnt:
            self.event("failcnt", "容器内存撞上限 %d 次（累计 %d）" % (fc - self.failcnt, fc))
        self.failcnt = fc

    def apply_cpu_quota(self, info, pol):
        """将主机配置的 cpu_quota 软配额接入有效 CPU 容量（#114）：
        - 无 cgroup 限额的独立机按配置值启用 CPU 记账；
        - macOS 桌面默认留核（cpu_quota_src == "macos_default"）允许配置值覆盖（可调大到 nproc 或调小）；
        - 已有 Linux cgroup 硬配额时收紧到配置值（且不超过逻辑核数 nproc）。"""
        cfg = (pol or {}).get("cpu_quota")
        if not (isinstance(info, dict) and cfg is not None and cfg > 0):
            return
        cands = [float(cfg)]
        if info.get("cpu_quota") and info.get("cpu_quota_src") != "macos_default":
            cands.append(float(info["cpu_quota"]))
        if info.get("nproc"):
            cands.append(float(info["nproc"]))
        info["cpu_quota"] = min(cands)
        if info.get("cpu_quota_src") == "macos_default":
            info["cpu_quota_src"] = "config"

    def disk_events(self, info, pol):
        """本地盘低水位/软配额准入闸门（#114，复用内存闸门模式）：
        - 触发：data_root 所在卷 free < disk_stop_gb，或配置了 disk_quota_gb 且已扫描 <data_root> 总占用 > disk_quota_gb；
        - 滞回恢复：剩余空间回升至 disk_stop_gb + DISK_HYST_GB 且占用降至 disk_quota_gb - DISK_HYST_GB 以下自动解除；
        - 不杀在跑任务，仅在 Worker.place() 的 pre() 拦截新任务放行并记 disk_gate 事件。"""
        stop_gb = pol.get("disk_stop_gb")
        quota_gb = pol.get("disk_quota_gb")
        d0 = next(iter((info or {}).get("disk") or []), None)
        free_gb = d0.get("free") if isinstance(d0, dict) else None
        wb = ((self.storage or {}).get("work") or {}).get("b")
        work_gb = (wb / _C.GIB) if wb is not None else None
        was = bool((self.disk_gate or {}).get("on"))
        has_stop = stop_gb is not None and stop_gb > 0
        has_quota = quota_gb is not None and quota_gb > 0
        # 软配额滞回：默认 2 GiB，小配额时按配额的 20% 封顶，保证恢复水位始终 > 0 且可达（避免 <= 2 GiB 时归零无法恢复）
        quota_recover = round(quota_gb - min(_C.DISK_HYST_GB, quota_gb * 0.2), 2) if has_quota else None
        if not (has_stop or has_quota):
            on = False
        elif not was:
            on = (has_stop and free_gb is not None and free_gb < stop_gb) or (has_quota and work_gb is not None and work_gb > quota_gb)
        else:
            free_ok = (not has_stop) or free_gb is None or free_gb >= stop_gb + _C.DISK_HYST_GB
            quota_ok = (not has_quota) or work_gb is None or work_gb <= quota_recover
            on = not (free_ok and quota_ok)
        why = None
        if on:
            bits = []
            if has_stop and free_gb is not None and free_gb < stop_gb + _C.DISK_HYST_GB:
                if free_gb < stop_gb:
                    bits.append("本地盘仅剩 %.1f GiB（低于停止线 %g GiB）" % (free_gb, stop_gb))
                else:
                    bits.append("本地盘剩 %.1f GiB（需回升至 %g GiB 以上解除）" % (free_gb, stop_gb + _C.DISK_HYST_GB))
            if has_quota and work_gb is not None and work_gb > quota_recover:
                if work_gb > quota_gb:
                    bits.append("%s 已占 %.1f GiB 超过配额 %g GiB" % (_C.DATA_ROOT, work_gb, quota_gb))
                else:
                    bits.append("%s 已占 %.1f GiB（需降至 %g GiB 以下解除）" % (_C.DATA_ROOT, work_gb, quota_recover))
            why = "磁盘闸门：" + ("；".join(bits) or "本地盘空间不足") + "，暂停放新任务；请运行 labmon disk 查看并清理缓存或旧运行目录"
        if on != was:
            if on:
                self.event("disk_gate", why)
            else:
                self.event("disk_gate", "磁盘闸门打开，恢复放行（本地盘剩 %s GiB）" % ("%.1f" % free_gb if free_gb is not None else "?"))
        self.disk_gate = {
            "on": on,
            "why": why,
            "free_gb": round(free_gb, 2) if free_gb is not None else None,
            "stop_gb": stop_gb,
            "recover_gb": (stop_gb + _C.DISK_HYST_GB) if has_stop else None,
            "quota_gb": quota_gb,
            "quota_recover_gb": quota_recover,
            "work_gb": round(work_gb, 2) if work_gb is not None else None,
        }

    def gpu_now(self):
        """停保活之后此刻的 GPU 进程（放行独占任务前看）：归属沿用这一轮 sample 认的，之后才冒出来的算 labmon 之外的。"""
        known = {g["hpid"]: g for g in self.gp}
        return [known.get(hp) or {"hpid": hp, "pid": None, "mb": mb, "uuid": uu, "comm": None, "owner": "other", "job": None, "_j": None}
                for hp, mb, uu in gpu_procs()]

    def ka_cores(self, procs, pids, t, tree="all"):
        """保活进程树这一段占的核数：整棵树的累计 CPU 之和求差（cpu_since，同 measure）÷ 间隔，第一轮为 0。
        tree 分开记上一轮的累计："all" 所有保活合起来（从 labmon 之外的里扣，没有保活的轮次按 0 记）；(pid, 启动时刻) 是一棵保活树（sample 按它求停不了的那几棵）。
        保活上一轮没认出来、这一轮才进树时，它启动以来的 CPU 一起扣掉（多扣、偏宽），正好抵掉上一轮算到对方头上的那份，不会连着两轮判超出；
        刚停的保活这一段用掉的核没法扣（进程已不在），算到 labmon 之外，借来的主机上由 guest_scan 连着两轮才判 CPU 超出挡住这一轮尖峰。"""
        ticks = sum(procs[p]["ticks"] for p in pids)
        prev, self.ka_ticks[tree] = self.ka_ticks.get(tree), (t, ticks)
        return cpu_since(ticks, prev[1]) / (t - prev[0]) if prev and t > prev[0] else 0.0

    def other_cpu(self, jobs, h, t):
        """labmon 之外的进程此刻占的核数：容器 CPU 用量 − 各任务 − worker 自己；算不出为 None。"""
        c = os.times()
        own = None
        if self.own_cpu and t > self.own_cpu[0]:
            own = (c.user + c.system - self.own_cpu[1]) / (t - self.own_cpu[0])
        self.own_cpu = (t, c.user + c.system)
        cores = [(o.get("use") or {}).get("cores") for o in jobs]
        return h["cpu_used"] - sum(cores) - own if h.get("cpu_used") is not None and own is not None and None not in cores else None
