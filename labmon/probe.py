"""labmon 探针：在被监控的主机上运行（经 ssh 送入标准输入，或本机直接运行），向标准输出打印一行 JSON。

只读：不改队列、不碰任何进程。只用标准库，兼容 Python 3.8（服务器镜像常只有系统自带的 python3）。
参数是一个 base64 编码的 JSON 规格（由 collect.py 按配置生成）：

    {"mounts": ["/data", "/tmp"],
     "checks": [{"id": "keepalive", "if": "test -x ka.sh", "cmd": "ka.sh running", "detail": ["tail -n1 ka.log"]}],
     "queues": [{"id": "q1", "type": "gpu_queue", "root": "/data/proj/code", "jobs": "jobs/q1.txt", ...}]}

新增队列类型：写一个 `queue_<type>(spec) -> dict` 函数并登记到 QUEUE_ADAPTERS，
返回的原始数据由本机 collect.py 里同名类型的解析函数转成统一的任务结构。
"""
import base64
import glob
import json
import os
import re
import subprocess
import sys
import time

OUT = {"t": time.time(), "host": os.uname().nodename, "errors": []}
START_RE = re.compile(r"===== .* 开始（")   # 任务日志里每次开跑的分隔行（worker 的 START_RE，同一条；本文件不 import worker）


def sh(cmd, timeout=20):
    """运行 shell 命令，返回 (退出码, 标准输出)；出错记入 errors。"""
    try:
        r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, universal_newlines=True)
        return r.returncode, r.stdout
    except Exception as e:  # noqa: BLE001
        OUT["errors"].append("%s: %s" % (cmd.split()[0] if cmd.split() else cmd, e))
        return None, ""


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


def num(x):
    return float(x) if re.match(r"^[0-9.]+$", x or "") else None


# ---------------- 硬件 ----------------
def gpus():
    rc, txt = sh("nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
                 "power.draw,power.limit --format=csv,noheader,nounits")
    out = []
    for line in txt.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 8:
            out.append({"index": int(p[0]), "name": p[1], "util": num(p[2]), "mem_used": num(p[3]),
                        "mem_total": num(p[4]), "temp": num(p[5]), "power": num(p[6]), "power_limit": num(p[7])})
    procs = []
    if out:
        _, txt = sh("nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits")
        for line in txt.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) == 2 and p[0].isdigit():
                args = read("/proc/%s/cmdline" % p[0], "") or ""
                procs.append({"pid": int(p[0]), "mem": num(p[1]), "cmd": args.replace("\0", " ")[:200]})
    return out, procs


def cgroup():
    """容器的 CPU 累计时间、配额与内存（cgroup v1 / v2）；不在 Linux 时返回空。"""
    cg = {}
    u = read("/sys/fs/cgroup/cpuacct/cpuacct.usage")
    if u is not None:
        cg["cpu_usage_ns"] = int(u)
        q, per = read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and per and int(q) > 0:
            cg["cpu_quota"] = int(q) / int(per)
        cg["mem_used"] = int(read("/sys/fs/cgroup/memory/memory.usage_in_bytes", "0"))
        lim = int(read("/sys/fs/cgroup/memory/memory.limit_in_bytes", "0"))
        cg["mem_limit"] = lim if lim < 1 << 60 else None
        st = dict(l.split() for l in (read("/sys/fs/cgroup/memory/memory.stat", "") or "").splitlines()
                  if len(l.split()) == 2)
        cg["mem_rss"] = int(st.get("total_rss", st.get("rss", 0)))
    elif os.path.exists("/sys/fs/cgroup/cpu.stat"):
        st = dict(l.split() for l in (read("/sys/fs/cgroup/cpu.stat", "") or "").splitlines() if len(l.split()) == 2)
        if "usage_usec" in st:
            cg["cpu_usage_ns"] = int(st["usage_usec"]) * 1000
        m = (read("/sys/fs/cgroup/cpu.max", "") or "").split()
        if len(m) == 2 and m[0] != "max":
            cg["cpu_quota"] = int(m[0]) / int(m[1])
        cg["mem_used"] = int(read("/sys/fs/cgroup/memory.current", "0") or 0)
        mx = read("/sys/fs/cgroup/memory.max", "max")
        cg["mem_limit"] = int(mx) if mx and mx.isdigit() else None
        st = dict(l.split() for l in (read("/sys/fs/cgroup/memory.stat", "") or "").splitlines() if len(l.split()) == 2)
        cg["mem_rss"] = int(st.get("anon", 0)) or None
    if cg and not cg.get("cpu_quota"):
        cg["cpu_quota"] = float(os.cpu_count() or 1)
    return cg


def mounts(paths):
    out = {}
    for m in paths:
        try:
            s = os.statvfs(m)
        except OSError as e:
            OUT["errors"].append("statvfs %s: %s" % (m, e))
            continue
        out[m] = {"total": s.f_blocks * s.f_frsize, "avail": s.f_bavail * s.f_frsize,
                  "used": (s.f_blocks - s.f_bfree) * s.f_frsize}
    return out


def checks(entries):
    """自定义检查：`if` 不成立记 na；`cmd` 退出码 0 记 ok，否则 fail；`detail` 各取输出最后一行。"""
    out = []
    for c in entries:
        r = {"id": c["id"]}
        if c.get("if") and sh(c["if"])[0] != 0:
            r["state"] = "na"
        else:
            rc, txt = sh(c["cmd"], c.get("timeout", 20))
            r["state"] = "ok" if rc == 0 else "fail"
            r["out"] = (txt.strip().splitlines() or [""])[-1][:300]
            r["detail"] = [((sh(d)[1].strip().splitlines() or [""])[-1])[:300] for d in c.get("detail", [])]
        out.append(r)
    return out


# ---------------- 队列适配器 ----------------
def _tail(path, nbytes=32768):
    """文件末尾 nbytes 字节的文字；从中间读起时丢掉开头那半行。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - nbytes))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return text.split("\n", 1)[-1] if n > nbytes else text


def _tree_stopped(pid):
    """pid 及其子孙进程里是否有被 SIGSTOP 挂起的（/proc/<pid>/stat 状态为 T）。没有 /proc 时返回 None。"""
    if not pid or not os.path.isdir("/proc"):
        return None
    kids, state = {}, {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % d) as f:
                s = f.read()
            rest = s[s.rfind(")") + 2:].split()
            state[int(d)] = rest[0]
            kids.setdefault(int(rest[1]), []).append(int(d))
        except (OSError, IndexError, ValueError):
            pass
    stack, seen = [pid], set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        if state.get(p) == "T":
            return True
        stack.extend(kids.get(p, []))
    return False


def _pid_alive(pid, match=None):
    if not pid or not os.path.exists("/proc/%d" % pid) and not _ps_alive(pid):
        return False
    if match:
        args = read("/proc/%d/cmdline" % pid, None)
        if args is None:
            args = sh("ps -p %d -o args=" % pid)[1]
        return match in (args or "")
    return True


def _ps_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:         # 进程在，但属于别的用户
        return True
    except OSError:
        return False


def _fs_skew(qlog):
    """队列状态所在盘与本机的时钟差（秒，正数表示盘的时间偏快）。queue.log 每行开头是本机 `date` 写的时间，
    文件修改时间却是盘（NFS 服务器）的时间，两者都对应最后一次追加，相减即偏差。差不到 5 秒当作没有。"""
    try:
        mt = os.path.getmtime(qlog)
    except OSError:
        return 0.0
    tail = [l for l in _tail(qlog, 4000).splitlines() if l.strip()]
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ", tail[-1]) if tail else None
    if not m:
        return 0.0
    sk = mt - time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))    # 探针整个文件送到服务器上跑，用不了 config.parse_time
    return sk if 5 < abs(sk) < 86400 else 0.0


def queue_gpu_queue(q):
    """文件标记式队列（labmon worker，或 A1 原来的 scripts/gpu_queue.sh）：

    - 清单 `<root>/<jobs>`：每行 `名称|命令`，# 开头为注释；行内 `est_min=N` 为估时（分钟）；
    - 清单旁同名 .env 里的 QUEUE_STATE / QUEUE_LOGS 给出状态目录与日志目录（相对 root）；
    - 状态目录：lock（队列进程 PID）、current（`名称 子进程PID 开始日期 开始时刻 [放行的主机]`：放行的主机不是这台（q 的 me）时不查 PID、按在跑算）、<名称>.done / .failed、queue.log；
      paused（可选，内容为原因）：有意停掉队列时由人建，labmon 据此显示“已暂停”、不报队列进程不在；
      lock 只在 worker 跑这条队列的任务时才有：空着等资源的队列由 worker_view 认出（q 里带 worker），也不报；
    - 日志目录：<名称>.log，进度行形如 `PROGRESS i/N`。
    """
    root = q["root"]
    envf = os.path.join(root, re.sub(r"\.txt$", ".env", q["jobs"]))
    env_txt = read(envf, "") or ""

    def env(key, default):
        m = re.search(r"^%s=(\S+)" % key, env_txt, re.M)
        return os.path.join(root, (m.group(1) if m else default).strip("\"'"))

    state = env("QUEUE_STATE", q.get("state", "results/_queue"))
    logs = env("QUEUE_LOGS", q.get("logs", "results/_logs"))
    out = {"jobs": [], "marks": {}, "state_dir": state, "logs_dir": logs, "worker": q.get("worker")}   # worker 在管这条队列时为 {"wait": 等待原因或 None}
    skew = _fs_skew(os.path.join(state, "queue.log"))
    out["fs_skew"] = round(skew, 1) if skew else None

    def mtime(f):                                # 文件修改时间换算成本机时钟
        return os.path.getmtime(f) - skew

    ro = q.get("readonly")                       # 队列在已归还的机器上：PID 属于那台机器，在这里查进程没有意义
    lock = read(os.path.join(state, "lock"))
    out["lock_pid"] = int(lock) if lock and lock.isdigit() else None
    out["lock_alive"] = False if ro else any(_pid_alive(out["lock_pid"], m) for m in (
        [q["lock_match"]] if q.get("lock_match") else ["gpu_queue", "labmon_worker"]))
    pf = os.path.join(state, "paused")
    out["paused"] = (read(pf, "") or "有意暂停")[:200] if os.path.exists(pf) else None
    for f in glob.glob(os.path.join(state, "*.done")) + glob.glob(os.path.join(state, "*.failed")):
        out["marks"][os.path.basename(f)] = mtime(f)
    # 最近一次活动（队列日志、锁、当前任务、完成标记的最晚修改时间）；从没启动过为 None
    ts = [mtime(os.path.join(state, f)) for f in ("queue.log", "lock", "current")
          if os.path.exists(os.path.join(state, f))] + list(out["marks"].values())
    out["activity"] = max(ts) if ts else None
    if "discovered" in q and not out["lock_alive"] and not out["worker"] and (
            out["activity"] is None or time.time() - out["activity"] > q.get("hide_done_hours", 24) * 3600):
        out["marks"] = {}
        out["skipped"] = True                    # 自动发现的清单久无活动：只报活动时间，省掉清单与日志
        return out
    cur = read(os.path.join(state, "current"))
    if cur:
        p = cur.split()
        c = {"name": p[0], "pid": int(p[1]) if len(p) > 1 and p[1].isdigit() else None,
             "t0": time.mktime(time.strptime(" ".join(p[2:4]), "%Y-%m-%d %H:%M:%S")) if len(p) >= 4 else None}   # 服务器本地时刻 → epoch
        who = p[4] if len(p) > 4 else None       # 放行的主机（worker 11-10 起写，#88）
        # 别的主机放行的（清单刚搬到这台、那台还在跑，#95）：PID 属于那台容器，这里查不了；照 worker 的 run_elsewhere 按在跑算，进度照样从共享盘的日志读
        away = bool(who and q.get("me") and who != q["me"])
        if q.get("pool"):                        # 算力池里的（#78）：刚从借来的主机搬回池里，那台手上的那一格还在跑；是这台自己放行的才查 PID
            c["alive"] = away or (who == q.get("me") and _pid_alive(c["pid"]))
        else:                                    # 已归还主机的（readonly）都不算在跑
            c["alive"] = False if ro else True if away else _pid_alive(c["pid"])
        c["stopped"] = _tree_stopped(c["pid"]) if c["alive"] and not away else None
        if who:
            c["host"] = who                      # 在哪台跑：清单搬走后原主机接着跑完手上那一格，项目页画到那台（#100）
        lf = os.path.join(logs, p[0] + ".log")
        lines = [l.rstrip() for l in _tail(lf).replace("\r", "\n").splitlines() if l.strip()]
        k = max((i for i, l in enumerate(lines) if START_RE.match(l)), default=-1)
        seen = set()                    # 只看这一次的：上一次的进度与输出不算，分隔行本身也不当最后一行（不出声的占位任务就没有）；
        lines = [l for l in lines[k + 1:] if not (l in seen or seen.add(l))]   # 逐字出现过的行是套话，只留第一次（worker 的 log_tail 同一口径）
        prog_re = re.compile(q.get("progress_re", r"PROGRESS (\d+)/(\d+)"))
        prog = [l for l in lines if prog_re.search(l)]
        c["progress_line"] = prog[-1][-300:] if prog else None
        body = [l for l in lines if not l[:1].isspace() and "Warning" not in l and not l.startswith("warnings.warn")]
        last = (body or lines or [None])[-1]
        c["last_line"] = last[-300:] if last else None
        c["log_mtime"] = mtime(lf) if os.path.exists(lf) else None
        out["current"] = c
    est_re = re.compile(q.get("est_re", r"est_min=(\d+(?:\.\d+)?)"))
    for line in (read(os.path.join(root, q["jobs"]), "") or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "|" not in line:
            continue
        name, cmd = line.split("|", 1)
        m = est_re.search(cmd)
        out["jobs"].append({"name": name.strip(), "est_min": float(m.group(1)) if m else None})
    out["log_tail"] = (read(os.path.join(state, "queue.log"), "") or "").splitlines()[-400:]
    return out


QUEUE_ADAPTERS = {"gpu_queue": queue_gpu_queue}


def _is_glob(q):
    return any(c in q["root"] + q.get("jobs", "") for c in "*?[")


def expand_queues(qs, explicit_jobs=()):
    """root 或 jobs 含通配符的队列在这里展开：每个匹配到的清单一条，id 为 `<队列 id>:<清单名>`。
    与显式登记的清单重复的跳过；root 匹配到多个目录时，清单名前加目录名。"""
    explicit = set(os.path.realpath(os.path.join(q["root"], q.get("jobs", ""))) for q in qs if not _is_glob(q))
    explicit |= set(os.path.realpath(p) for p in explicit_jobs)
    out = []
    for q in qs:
        if not _is_glob(q):
            out.append(q)
            continue
        roots = sorted(r for r in glob.glob(q["root"]) if os.path.isdir(r))
        for r in roots:
            for jf in sorted(glob.glob(os.path.join(r, q.get("jobs", "")))):
                if os.path.realpath(jf) in explicit or not os.path.isfile(jf):
                    continue
                name = os.path.splitext(os.path.basename(jf))[0]
                if len(roots) > 1:
                    name = os.path.basename(r.rstrip("/")) + "/" + name
                sub = dict(q, root=r, jobs=os.path.relpath(jf, r), id="%s:%s" % (q["id"], name))
                sub["discovered"] = {"from": q["id"], "name": name, "mtime": os.path.getmtime(jf)}
                out.append(sub)
    return out


def worker_view(path, stale):
    """worker 的 status.json：worker 在跑（状态 stale 秒内更新过，与工作台判 worker 在不在同一口径）时返回
    {清单路径: {"wait": 等待原因或 None, "held": 挡住它的临时指令种类或 None, "dep": 依赖没满足（wait、failed、missing、cycle）或 None,
    "cpu": 一直因 CPU 被挡满 10 分钟后已挡的秒数（worker 的 cpu_wait）或 None,
    "dur": {任务: 预计时长}, "plan": {任务: {t0, t1, gpus, after, deps}}, "stuck": 推演里排不上的原因或 None}}，即它在管的队列；不在跑返回 {}。
    队列的锁只在 worker 跑这条队列的任务时才有，空着等资源的队列靠这个认。预计时长是 worker 的 duration 算的；
    plan 是 worker 推演（forecast）出的预计开始与结束（同机各项目一起排），看板按它排预计时间。"""
    try:
        st = json.loads(read(path, "") or "{}") if path else {}
    except ValueError:
        return {}
    if not st.get("t") or time.time() - st["t"] >= stale:
        return {}

    def key(root, jobs):
        return os.path.normpath(os.path.join(root or "", jobs or ""))
    out = {key(q.get("root"), q.get("jobs")): {"wait": None, "held": None, "dep": None, "cpu": None, "dur": q.get("dur") or {}, "plan": {}, "stuck": None}
           for q in st.get("queues", [])}
    for ln in (st.get("plan") or {}).get("lanes", []):
        k = key(ln.get("root"), ln.get("queue"))
        if ln.get("root") and k in out:
            out[k].update(plan={j["name"]: j for j in ln["jobs"]}, stuck=ln.get("stuck"))
    for w in st.get("waiting", []):
        k = key(w.get("root"), w.get("queue"))
        if w.get("root") and k in out and out[k]["wait"] is None:
            out[k].update(wait=w.get("why"), held=w.get("held"), dep=w.get("dep"), cpu=w.get("cpu_wait"))
    return out


def main():
    spec = json.loads(base64.b64decode(sys.argv[1]).decode()) if len(sys.argv) > 1 else {}
    OUT["gpu"], OUT["gpu_procs"] = gpus()
    OUT["cgroup"] = cgroup()
    la = read("/proc/loadavg", "")
    OUT["loadavg"] = [float(x) for x in la.split()[:3]] if la else list(os.getloadavg())
    OUT["mounts"] = mounts(spec.get("mounts", []))
    OUT["checks"] = checks(spec.get("checks", []))
    OUT["queues"] = {}
    wv = worker_view(spec.get("worker_status"), spec.get("worker_stale", 180))
    for q in expand_queues(spec.get("queues", []), spec.get("explicit_jobs", [])):
        fn = QUEUE_ADAPTERS.get(q.get("type"))
        if fn is None:
            OUT["errors"].append("未知队列类型 %s" % q.get("type"))
            continue
        k = os.path.normpath(os.path.join(q["root"], q.get("jobs", "")))
        if not q.get("readonly") and k in wv:
            q = dict(q, worker=wv[k])
        q = dict(q, me=spec.get("host"))         # 这台主机的 id：current 里放行的主机不是它时不查 PID
        try:
            OUT["queues"][q["id"]] = fn(q)
            if "discovered" in q:                # 带上实际的目录与清单：页面按它认 worker 事件属于哪条车道
                OUT["queues"][q["id"]]["discovered"] = dict(q["discovered"], root=q["root"], jobs=q["jobs"])
        except Exception as e:  # noqa: BLE001
            OUT["errors"].append("队列 %s: %s" % (q["id"], e))
    print(json.dumps(OUT, ensure_ascii=False))


if __name__ == "__main__":
    main()
