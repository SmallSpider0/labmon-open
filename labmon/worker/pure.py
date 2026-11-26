"""看板与 worker 共用的纯常量与纯函数（只用 Python 3.8 标准库，不 import 本包其余模块）。

随 labmon/worker/ 一起单连接打包发到服务器，看板侧直接 `from .worker.pure import ...` 复用，
消除“worker 不 import 本包，各一份、口径相同”的重复实现。
"""
import json
import os
import posixpath
import re
import time

# ---------- 共用常量 ----------
GIB = 2 ** 30                  # 容量单位（字节 → GiB）
STALE_S = 180                  # status.json 这么久没更新（按里面的 t）就算那台 worker 不在
IDLE_REG_S = 2 * 86400         # 跑空的登记满这么久自动休眠（只看清单 file_sig，不再读状态目录与 .env；#164）
POOL = "pool"                  # 算力池的保留主机 id：池里待放的清单登记在 hosts/pool/queues/

# 占位任务（清单里插的“等条件再放行”的 hold / pause）：按词认，不占卡、不独占、不进估时比值与等待统计
HOLD_RE = re.compile(r"(?i)(?:^|[_.\-\d])(?:hold|pause)(?:$|[_.\-\d])")

# 名字前缀（第一个 _ 或 - 及之前）：看板自动归组（config.AUTO_GROUP_RE）与 worker 借同前缀估时比值同一条正则
GROUP_RE = re.compile(r"([^_-]+)[_-]")

# 剩余时长说不准时（超了预计；看板还有进度刚开始、没估时或已超估时）按已跑的这么多比例估、至少这么多秒
OVER_FRAC, OVER_MIN_S = 0.2, 600

# 只看 shell 与 python 跑的脚本（script_arg 与看门狗、组内保活归类共用）
INTERP = re.compile(r"^(?:ba|da|z|k)?sh$|^python[\d.]*$")


# ---------- 文件签名与 JSONL ----------
def file_sig(path, *, strict=False):
    """文件变没变的签名 (inode, 修改时刻纳秒, 大小)。按文件缓存读出内容的都拿它比：
    只比 st_mtime 时同一时间戳粒度里连写两次会漏读第二次（#31）。
    strict=False 时读不到返回 None（看板侧）；strict=True 时抛 OSError（worker 侧 fsig）。"""
    try:
        st = os.stat(path)
    except OSError:
        if strict:
            raise
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


def parse_jsonl(text):
    """JSONL 文本逐行解析，写坏的行（如写到一半被打断）跳过。"""
    out = []
    for line in (text or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ---------- 清单与任务名 ----------
def list_path(root, jobs):
    """认清单：目录 + 清单路径按 POSIX 规则规整后拼起来。"""
    return posixpath.normpath(posixpath.join(str(root or ""), str(jobs or "")))


def same_list(q, root, jobs):
    """登记 q（dict，含 root/jobs）是不是这条清单（ROOT、JOBS）：拼起来规整后是同一个清单文件就算
    （./q.txt、a/../q.txt 与 q.txt 是同一条，#18；ROOT/JOBS 从哪里断开都一样，#77）。"""
    return bool(q.get("root") and q.get("jobs")) and list_path(q["root"], q["jobs"]) == list_path(root, jobs)


def is_hold(name):
    """占位任务认定：名字里按词认 hold / pause——开头、结尾，或被 _ . - 或数字隔开。"""
    return bool(HOLD_RE.search(name or ""))


def job_key(name, project):
    """任务名（可带 项目/ 前缀）→ (项目, 任务)；不带前缀的是 project。
    清单 after= / afterany=（worker 的 dep_keys）与看板 labmon wait --job 共用。"""
    if "/" in name:
        p, n = name.split("/", 1)
        return p, n
    return project, name


# ---------- 调度与资源计算 ----------
def overrun_left(el):
    """剩余时长说不准时的估计（秒）：已跑 el 秒的 OVER_FRAC、至少 OVER_MIN_S，不当“马上结束”。"""
    return max(OVER_MIN_S, OVER_FRAC * el)


def person_use(pol, use):
    """各人员键（pol["own"]）名下几个项目的用量之和 {人员键: 卡·时}；没写人员键的项目合在键 None 下。
    worker 放行顺序（fair_key）、算力池（pool.py）与看板调度卡（overview.sched_view）共用。"""
    own, out = pol.get("own") or {}, {}
    for q, v in use.items():
        out[own.get(q)] = out.get(own.get(q), 0.0) + v
    return out


def mem_unevictable(use, active_file, inactive_file):
    """容器里收不回的内存（字节）：cgroup 用量 − 文件页（活跃 + 不活跃页缓存）。
    v1 是 memory.usage_in_bytes − total_active_file − total_inactive_file，
    v2 是 memory.current − active_file − inactive_file。含匿名页、shmem 与记到 cgroup 的内核内存。
    worker 的 host_info 与看板组内服务器解析（fleet.parse）共用。"""
    return max(0, use - active_file - inactive_file)


def proc_tree(procs, roots, *, pgids=(), exclude=()):
    """roots 里的进程与它们的子孙（procs 进程表里有的；不在表里的根不算）。
    pgids 给了时，procs 里 pgid 在 pgids 里的进程也并作起点一起展开其子孙；
    exclude 里的进程及其子孙（遇到 roots 中不在 exclude 里的进程时停住，不误伤嵌套子任务）不纳入。
    worker 任务与保活进程树、看板组内服务器保活进程树（fleet.analyze）共用。"""
    kids = {}
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)
    excl = set()
    if exclude:
        excl_in = set(exclude)
        keep = set(roots) - excl_in
        etodo = list(excl_in)
        while etodo:
            x = etodo.pop()
            if x not in excl and x not in keep:
                excl.add(x)
                etodo += kids.get(x, [])
    pg = set(pgids) if pgids else set()
    todo = [pid for pid in roots if pid in procs and pid not in excl]
    if pg:
        todo += [pid for pid, p in procs.items() if p.get("pgid") in pg and pid not in excl]
    out, seen = [], set()
    while todo:
        x = todo.pop()
        if x not in seen and x not in excl:
            seen.add(x)
            out.append(x)
            todo += kids.get(x, [])
    return out


def script_arg(args, cwd):
    """shell 或 python 进程跑的脚本：(脚本路径（相对路径按工作目录 cwd 还原、normpath）、"-c"、"-s" 或 "-m 模块", 下一个参数的位置)；
    不是这两种解释器、交互 shell 与 REPL 为 None。
    worker 的看门狗（script_of）与 ka.sh 认保活（ka_owned）、看板组内服务器（fleet._abs_script）共用。"""
    if not args or not INTERP.match(os.path.basename(args[0])):
        return None
    py = os.path.basename(args[0]).startswith("python")
    takes = ("-W", "-X") if py else ("-o", "-O")       # 带一个参数的选项
    i = 1
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("--") and a.endswith("c"):     # -c、bash -lc、python -uc：代码在下一个参数
            return "-c", i + 1
        if py and a == "-m":
            return "-m " + (args[i + 1] if i + 1 < len(args) else ""), i + 2
        if not py and a == "-s":                           # bash -s：脚本从标准输入来，只看得到命令行
            return "-s", i + 1
        i += 2 if a in takes else 1
    if i >= len(args):
        return None
    return posixpath.normpath(posixpath.join(cwd, args[i])), i + 1


# ---------- 他项聚合负载（#197：fit --list 与 labmon status 共用，绝不暴露他人项目名/任务名） ----------
def other_load(st, project):
    """统计一台主机 status.json 上除 project 外其他项目的聚合负载：
    返回 {"running": 在跑数（不含占位）, "queued": 排队数（不含占位、不含暂停清单）, "until": 预计最晚忙到时刻（epoch 秒或 None）}。
    只读已有 status.json 的 running、todo、waiting、plan，不暴露他人项目名或任务名。"""
    if not isinstance(st, dict):
        return {"running": 0, "queued": 0, "until": None}
    run_n = 0
    run_lane, run_pn = {}, {}
    for j in st.get("running") or []:
        if not isinstance(j, dict):
            continue
        p, n = j.get("project"), j.get("name")
        if p == project or j.get("hold") or is_hold(n):
            continue
        run_n += 1
        lk = (p, j.get("root"), j.get("queue"), n)
        run_lane[lk] = run_lane.get(lk, 0) + 1
        pk = (p, n)
        run_pn[pk] = run_pn.get(pk, 0) + 1
    q_todo = 0
    rem_lane, rem_pn = dict(run_lane), dict(run_pn)
    for item in st.get("todo") or []:
        if not isinstance(item, dict):
            continue
        p = item.get("project")
        if p == project or item.get("held") in ("paused", "pause"):
            continue
        root, queue = item.get("root"), item.get("queue")
        for n in item.get("jobs") or []:
            if is_hold(n):
                continue
            lk = (p, root, queue, n)
            pk = (p, n)
            if rem_lane.get(lk, 0) > 0:
                rem_lane[lk] -= 1
                if rem_pn.get(pk, 0) > 0:
                    rem_pn[pk] -= 1
                continue
            if root is not None and rem_lane.get((p, None, None, n), 0) > 0:
                rem_lane[(p, None, None, n)] -= 1
                if rem_pn.get(pk, 0) > 0:
                    rem_pn[pk] -= 1
                continue
            if root is None and rem_pn.get(pk, 0) > 0:
                rem_pn[pk] -= 1
                continue
            q_todo += 1
    q_plan, until = 0, None
    for ln in (st.get("plan") or {}).get("lanes") or []:
        if not isinstance(ln, dict) or ln.get("project") == project:
            continue
        for j in ln.get("jobs") or []:
            if not isinstance(j, dict) or is_hold(j.get("name")):
                continue
            if not j.get("run"):
                q_plan += 1
            t1 = j.get("t1")
            if isinstance(t1, (int, float)) and not isinstance(t1, bool) and t1 > 0:
                until = float(t1) if until is None or float(t1) > until else until
    q_wait = 0
    for w in st.get("waiting") or []:
        if not isinstance(w, dict):
            continue
        if w.get("project") == project or w.get("held") in ("paused", "pause") or is_hold(w.get("name")):
            continue
        q_wait += 1
    return {"running": run_n, "queued": max(q_todo, q_plan, q_wait), "until": until}


def other_load_text(load, now=None, show_until=False):
    """把 other_load 的结果格式化成极短聚合提示（无他项负载时返回空串）。
    例：'他项 2 跑/8 排'；show_until=True 且有未来预计结束时：'他项 2 跑/8 排，忙至 23:40'（跨天附月-日）。"""
    if not isinstance(load, dict):
        return ""
    r, q = int(load.get("running") or 0), int(load.get("queued") or 0)
    if r <= 0 and q <= 0:
        return ""
    parts = []
    if r > 0:
        parts.append("%d 跑" % r)
    if q > 0:
        parts.append("%d 排" % q)
    s = "他项 " + "/".join(parts)
    until = load.get("until")
    if show_until and isinstance(until, (int, float)) and not isinstance(until, bool) and until > 0:
        if now is None or until > now:
            lt = time.localtime(until)
            same_day = now is not None and lt[:3] == time.localtime(now)[:3]
            s += "，忙至 " + time.strftime("%H:%M" if same_day else "%m-%d %H:%M", lt)
    return s
