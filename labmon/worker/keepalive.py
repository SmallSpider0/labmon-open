"""worker 保活停启、看门狗（watchdogs / freeze / thaw）与借来主机（guest）逻辑。"""
import datetime
import glob
import json
import os
import re
import signal
import subprocess

from . import common as _C
from . import measure as _M

def ka_log_time(ts):
    """ka.sh 日志行开头的时刻（date -Iseconds，带时区）换成 epoch 秒；不带时区的按本机时区；认不出为 None。"""
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


# ---- 保活看门狗：会把 worker 停掉的 GPU 保活又拉起来的进程（借来的主机上暂停，还卡时恢复）----
KA_REF = re.compile(r"(?i)keep_?alive\w*\.(?:py|sh)\b|\bka\.sh\b")   # 提到保活程序：train_keep_alive.py、keepalive_guard.sh、ka.sh 这类文件名


LOOP_RE = re.compile(r"\bwhile\b|\buntil\b|\bsleep\b|\bwatch\b|schedule\.every")  # 在循环里、隔一会儿再看


GPU_RE = re.compile(r"nvidia-smi|pynvml|gpustat|nvitop")                   # 盯着 GPU


INTERP = re.compile(r"^(?:ba|da|z|k)?sh$|^python[\d.]*$")                  # 只看 shell 与 python 跑的脚本（读得到内容）


WATCH_SKIP = ("ka.sh", "gpu_queue.sh", "labmon_worker.py", "fleet_guard.sh")   # labmon 自己的：保活停启入口、旧队列、worker、保活守护


WATCH_EVERY = 600        # 借来的主机多久找一遍看门狗


SURE_WAIT = "；确定的：要卡时它把保活拉起来就暂停，还卡时恢复"   # 找到、还没暂停的确定的看门狗（frozen.json 的 seen）后面接的说明（#31）


LOCK_WAIT_S = 60         # 看门狗一直持着平时不持的锁这么久：记一次事件（照样不暂停，#38）


FIGHT_S = 3600           # 保活被重开的次数按这么久算（状态的 kafight、看板告警）


KA_STUCK_S = 120         # 要卡时停保活连着失败这么久（且至少两次）算 worker 停不了它：ka_stuck 事件、状态的 keepalive.stuck（#102）


KA_LOG_LAG = (-5, 30)    # ka.sh 记 RESTORED 的时刻比保活进程启动晚多少秒（它起来后最多等 20 秒才记；前后各留余量）：ka_caller 按它认这台的那条


CRON = ("/etc/crontab", "/etc/cron.d", "/var/spool/cron")   # cron 在跑时看这些定时任务里有没有拉起保活的（暂停不了，只提醒）


def head(path, n=65536):
    """文件的前 n 字节（文字）；读不到为空串。"""
    try:
        with open(path, "rb") as f:
            return f.read(n).decode(errors="replace")
    except OSError:
        return ""


def proc_args(pid):
    """进程的参数列表与工作目录（读 PROC）；读不到为 ([], "")。"""
    try:
        with open(os.path.join(_C.PROC, str(pid), "cmdline"), "rb") as f:
            args = [x.decode(errors="replace") for x in f.read().split(b"\0")]
    except OSError:
        return [], ""
    while args and not args[-1]:
        args.pop()
    try:
        cwd = os.readlink(os.path.join(_C.PROC, str(pid), "cwd"))
    except OSError:
        cwd = ""
    return args, cwd


def refs(text, *dirs):
    """文字里提到的 .sh / .py 脚本的内容（写了绝对路径的按路径，否则在 dirs 里按文件名找），各取前 64 KB，最多 8 个：
    看门狗常是一个循环脚本去调另一个拉起保活的脚本。"""
    out, seen = [], set()
    for d0, name in re.findall(r"((?:/[\w.-]+)*/)?([\w.-]+\.(?:sh|py))\b", text):
        if name in seen or len(seen) >= 8:
            continue
        seen.add(name)
        for path in [d0 + name] * bool(d0) + [os.path.join(d, name) for d in dirs if d]:
            if os.path.isfile(path):
                out.append(head(path))
                break
    return "\n".join(out)


def script_arg(args, cwd):
    """shell 或 python 进程跑的脚本：(脚本路径（相对路径按工作目录 cwd 还原、normpath）、"-c"、"-s" 或 "-m 模块", 下一个参数的位置)；
    不是这两种解释器、交互 shell 与 REPL 为 None。看门狗（script_of）与 ka.sh 认保活（ka_owned）共用。"""
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
    return os.path.normpath(os.path.join(cwd, args[i])), i + 1


def script_of(args, cwd, pid=None):
    """shell 或 python 进程在跑什么：(脚本路径、-c、-s 或 -m 模块, 文字)。文字是命令行、脚本内容（前 64 KB）和它直接调到的脚本（refs）；
    不是这两种解释器、交互 shell 与 REPL 为 None。脚本文件已被删掉的，bash 还开着它（fd 255），给了 pid 就从那里读。"""
    sa = script_arg(args, cwd)
    if not sa:
        return None
    what, i = sa
    line = " ".join(args)
    if what == "-c":
        return "-c", line + "\n" + refs(args[i] if i < len(args) else "", cwd)
    if what == "-s" or what.startswith("-m "):
        return what, line
    py = os.path.basename(args[0]).startswith("python")
    text = head(what) or (head(os.path.join(_C.PROC, str(pid), "fd", "255")) if pid and not py else "")
    return what, line + "\n" + text + "\n" + refs(text, os.path.dirname(what), cwd)


def ka_owned(args, cwd, ka_full):
    """ka.sh 认得、停得了的保活（与 ka.sh 的 ka_procs 同一条规则，#29、#105；两份：worker 不 import 本包，tests/test_ka.py 核对结论一致）：python 跑的，命令行里写着 ka_full（AGRI_KA_DIR/AGRI_KA_SCRIPT）这个完整路径，
    或跑的脚本（相对路径按进程工作目录还原）解开符号链接后就是它：先 cd 进保活目录、用 train_keep_alive.py、./train_keep_alive.py 起的也算。
    cwd 读不到（进程没了）的相对路径认不出。"""
    if not args or not re.match(r"python[\d.]*$", os.path.basename(args[0])):
        return False
    if ka_full in " ".join(args):
        return True
    sa = script_arg(args, cwd or "")
    return bool(sa and sa[0].startswith("/") and os.path.realpath(sa[0]) == os.path.realpath(ka_full))


def ours(pid):
    """labmon 自己起的（保活守护在后台盯着的 bash 带 ST=/tmp/labmon-guard.state，worker 调 ka.sh 带 KA_CALLER=labmon-worker:…）。"""
    return "labmon-guard" in (_C.proc_env(pid, "ST") or "") or (_C.proc_env(pid, "KA_CALLER") or "").startswith("labmon")


def wd_text(ws):
    """看门狗列成一句：PID 123（bash keepalive_final_check.sh：拉起了保活）、…"""
    return "、".join("PID %d（%s：%s）" % (w["pid"], w["cmd"] if len(w["cmd"]) <= 80 else w["cmd"][:77] + "…", w["why"]) for w in ws)


def ka_lines(st):
    """status.json 里这台的 GPU 保活说成几句（#61；`labmon worker status` 与看板的 `labmon status` 共用）：在跑的（几点起、
    nvidia-smi 里的 PID 与卡、占多少显存；要卡时 worker 停不了它的照实写，keepalive.stuck）、停着的（worker 为要卡的任务停的）、没在跑又不拉起的原因、借来的主机上 ka.sh 停不了的、
    近 1 小时被拉起几次。归属取状态里 gpu_procs 的 owner 与 ka（sample 经 gpu_owners、ka_owned 认的），这里不另判；PID 写宿主机的（nvidia-smi 列的那个）。"""
    idx = {g["uuid"]: g["i"] for g in (st.get("info") or {}).get("gpus") or [] if "uuid" in g}
    ka = st.get("keepalive") or {}

    def procs(gs):
        by = {}
        for g in gs:
            by.setdefault(g.get("hpid"), []).append(g)
        return "；".join("PID %s%s，卡 %s 占 %.1f GiB" % (hp, "（容器内 %s）" % v[0]["pid"] if v[0].get("pid") not in (None, hp) else "",
                                                       ",".join(str(idx.get(g.get("uuid"), "?")) for g in v), sum(g.get("mb") or 0 for g in v) / 1024)
                        for hp, v in by.items())
    out = []
    mine = [g for g in st.get("gpu_procs") or [] if g.get("owner") == "keepalive"]
    if mine:
        t0 = min((g["t0"] for g in mine if g.get("t0")), default=None)
        stuck = ka.get("stuck")              # worker 要卡时停不了它（Worker.ka_note，#102）：不说“自动停”
        out.append("GPU 保活：在跑%s：%s%s" % ("（%s 起）" % _C.now_str(t0)[5:16] if t0 else "", procs(mine),
                                         "；worker 要卡时停它没停成（%s，%s 起 %d 次），和任务同卡分算力、占着显存" % (stuck["why"], _C.now_str(stuck["t0"])[5:16], stuck["n"])
                                         if stuck else "；有任务要卡时 worker 自动停" if ka.get("ctl") else "；这台没配 ka.sh，worker 不停它" if ka.get("ctl") is False else ""))
    elif st.get("ka_stop") is not None:
        out.append("GPU 保活：停着（%s有任务要卡；卡空下来后 worker 再拉起）" % ("worker %s 停的，" % _C.now_str(st["ka_stop"])[5:16] if st["ka_stop"] else ""))
    elif ka.get("why"):
        out.append("GPU 保活：没在跑，没有任务要卡，但%s，不拉起" % ka["why"])
    elif ka.get("ctl"):
        out.append("GPU 保活：没在跑（卡连着空两轮后 worker 拉起）")
    ext = [g for g in st.get("gpu_procs") or [] if g.get("ka")]
    if ext:
        out.append("对方的保活（ka.sh 停不了，这几张卡不放）：" + procs(ext))
    if st.get("kafight"):
        out.append("GPU 保活 %d 小时内被拉起 %d 次（最近 %s）：%s" % (FIGHT_S // 3600, st["kafight"]["n"], _C.now_str(st["kafight"]["t"])[5:16], st["kafight"]["who"]))
    return out


def watchdogs(procs, skip):
    """找会把保活又拉起来的进程（看门狗）。procs 是 scan_procs 的结果，skip 是不算的 PID（labmon 的任务、worker 自己、用着 GPU 的）。
    确定的（sure）：保活的父进程（脚本）、保活的上级进程或同一会话里提到保活程序的，以及提到保活程序又在循环里的；
    可能的（maybe）：只提到保活程序、没见循环的，保活更上级的进程，循环盯着 GPU 的，提到保活程序的 1 号进程（暂停不了），
    cron 在跑时定时任务里提到保活程序的（暂停不了），以及够得上确定、但下面在跑程序的（python 或 skip 里用着 GPU 的：
    对方的实验队列跑完一批再拉保活，暂停它会耽误对方后面的实验）。只看 shell 与 python 跑的脚本，交互 shell 与二进制程序不算。
    1 号进程不算保活的上级：ka.sh 拉起的保活脱离后父进程就成了它（平台容器的 1 号是 sh -c "… && sleep infinity"）。
    返回 (sure, maybe)，每项 {pid, start, cmd, cwd, why}。"""
    info = {pid: proc_args(pid) for pid in procs}
    keep = [pid for pid, (args, _) in info.items()
            if args and re.match(r"python[\d.]*$", os.path.basename(args[0])) and any("keep_alive" in a for a in args[1:])]
    up, ses, kids = {}, {}, {}             # 保活的上级进程（PID → 第几级）、与保活同一会话的、各进程的子进程
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)

    def running(pid):
        """pid 下面在跑的程序（python 或 skip 里的）：第一个的 PID，没有为 None。保活连同它的整棵子树不算
        （DataLoader 用 spawn 起的子进程命令行不带 keep_alive，不跳过整棵会把拉起保活的看门狗降成可能的）。"""
        todo = list(kids.get(pid, []))
        while todo:
            x = todo.pop()
            if x in keep:
                continue
            a = info.get(x, ([], ""))[0]
            if x in skip or a and re.match(r"python[\d.]*$", os.path.basename(a[0])):
                return x
            todo += kids.get(x, [])
        return None
    for k in keep:
        x, n = procs[k]["ppid"], 1
        while x in procs and x != 1 and n <= 20:
            up.setdefault(x, (n, k))
            x, n = procs[x]["ppid"], n + 1
        if procs[k]["sid"] != k:
            ses.update({pid: k for pid, p in procs.items() if p["sid"] == procs[k]["sid"]})
    sure, maybe = [], []
    for pid in sorted(procs):
        if pid in skip or pid in keep:
            continue
        args, cwd = info[pid]
        sc = script_of(args, cwd, pid)
        if not sc or os.path.basename(sc[0]) in WATCH_SKIP or any(n in " ".join(args) for n in ("labmon_worker.py", "fleet_guard.sh")):
            continue
        ref, loop = bool(KA_REF.search(sc[1])), bool(LOOP_RE.search(sc[1]))
        n, k = up.get(pid, (0, None))
        why = ("拉起了保活（PID %d 的父进程）" % k if n == 1 and not sc[0].startswith("-m") else
               "是保活（PID %d）的上级进程，提到保活程序" % k if n and ref else
               "与保活（PID %d）在同一会话里，提到保活程序" % ses[pid] if pid in ses and ref else
               "提到保活程序，在循环里" if ref and loop else None)
        maybe_why = ("是保活（PID %d）的上级进程" % k if n else "提到保活程序，没见循环" if ref else
                     "在循环里盯着 GPU" if loop and GPU_RE.search(sc[1]) else None)
        if not (why or maybe_why) or ours(pid):
            continue
        w = {"pid": pid, "start": _C.pstart(pid), "cmd": " ".join(args)[:200], "cwd": cwd}
        r = running(pid) if why else None
        if pid == 1:
            maybe.append(dict(w, why="容器的 1 号进程，" + (why or maybe_why) + "（暂停不了）"))
        elif r:
            maybe.append(dict(w, why="%s，但下面在跑 PID %d（%s），暂停会耽误对方，没动" % (why, r, " ".join(info[r][0])[:60])))
        else:
            (sure if why else maybe).append(dict(w, why=why or maybe_why))
    cron = [pid for pid, p in procs.items() if p["comm"] in ("cron", "crond", "supercronic")]
    if cron:
        for path in CRON:
            for f in [path] if os.path.isfile(path) else sorted(glob.glob(os.path.join(path, "*")) + glob.glob(os.path.join(path, "*", "*"))):
                for ln in head(f).splitlines():
                    if KA_REF.search(ln) and not ln.lstrip().startswith("#"):
                        maybe.append({"pid": cron[0], "start": _C.pstart(cron[0]), "cmd": ln.strip()[:200], "cwd": f,
                                      "why": "cron 定时任务（%s）提到保活程序（暂停不了）" % f})
    return sure, maybe


def held_locks(pid):
    """进程此刻持着的文件锁（读 PROC/locks，#38）：{"主:次:inode": 文件路径（读不到为 None）}。FLOCK、POSIX 锁按持有者 PID 认；
    OFD 锁不属于单个进程（PID 写 -1），按它开着的文件（PROC/<pid>/fd）认；在等的（"->"）与租约不算。读不到 PROC/locks 为 None。
    PID 是读这个 /proc 的 PID 命名空间里的（Linux 4.9 起按命名空间过滤），worker 与看门狗在同一个容器里，对得上。"""
    try:
        with open(os.path.join(_C.PROC, "locks")) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    fds, d = {}, os.path.join(_C.PROC, str(pid), "fd")
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for n in names:
        try:
            st = os.stat(os.path.join(d, n))
            fds["%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)] = os.readlink(os.path.join(d, n))
        except OSError:
            continue
    out = {}
    for ln in lines:
        x = ln.split()
        if len(x) < 6 or x[1] not in ("FLOCK", "POSIX", "OFDLCK"):
            continue
        if x[4] == str(pid) or x[4] == "-1" and x[5] in fds:
            out[x[5]] = fds.get(x[5])
    return out


def lock_text(locks):
    """锁住的文件列成一句（有路径写路径，没有写 inode）。"""
    return "、".join(sorted(v or "inode " + k for k, v in locks.items()))


def lock_base(seen):
    """frozen.json 的 seen 里记过平时持的锁的看门狗：{(pid, start): 那一项}（locks、locks_t 见 Worker.note_seen）。"""
    return {(w["pid"], w["start"]): w for w in seen if "locks" in w}


def load_frozen(p):
    """frozen.json：{"frozen": 暂停着的看门狗, "seen": 找到了、没动的（可能的、下面在跑程序的，或确定的但还没把保活拉起来过：why 接 SURE_WAIT）}；
    确定的带 locks：它平时一直持着的文件锁（held_locks 的键，每次找到与上次的取交集，Worker.note_seen）；第一份是保活刚被拉起时记的
    另带 locks_t（那一刻）。暂停着的不带（停着拿不了新锁，不再看）。没有或写坏了当作都空。"""
    try:
        d = json.loads(_C.read(p.frozen, "{}") or "{}")
    except ValueError:
        d = {}
    return {"frozen": d.get("frozen") or [], "seen": d.get("seen") or []}


def freeze(p, ws=(), seen=None):
    """暂停看门狗（SIGSTOP）并记进 frozen.json，还卡时 thaw 恢复。保活真被拉起来时才调（ka_restarted）；
    已暂停过的再发一次（被人恢复后又拉起了保活），不重复记。返回这次停住的。seen 给了就换掉找到了、没动的那份（已暂停的不再列）。"""
    with _C.flock(p.frozen):
        d = load_frozen(p)
        old = {(w["pid"], w["start"]) for w in d["frozen"]}
        done = []
        for w in ws:
            try:
                os.kill(w["pid"], signal.SIGSTOP)
            except OSError:
                continue
            done.append(w)
            if (w["pid"], w["start"]) not in old:
                d["frozen"].append(dict(w, t=round(_C.time.time())))
                old.add((w["pid"], w["start"]))
        if seen is not None:
            d["seen"] = [w for w in seen if (w["pid"], w["start"]) not in old]
        _C.write(p.frozen, json.dumps(d, ensure_ascii=False))
    return done


def thaw(p):
    """恢复暂停的看门狗（SIGCONT；PID 已被复用的不碰），清空 frozen.json。返回一句话，没暂停过的为空串。"""
    with _C.flock(p.frozen):
        back, gone = [], []
        for w in load_frozen(p)["frozen"]:
            ok = w["start"] is not None and _C.pstart(w["pid"]) == w["start"]
            try:
                if ok:
                    os.kill(w["pid"], signal.SIGCONT)
            except OSError:
                ok = False
            (back if ok else gone).append(w)
        if os.path.exists(p.frozen):
            os.remove(p.frozen)
    return "；".join(x for x in ["看门狗：恢复了 " + wd_text(back) if back else "",
                                ("看门狗：" if not back else "") + "已不在 " + "、".join("PID %d" % w["pid"] for w in gone) if gone else ""] if x)


def gpu_owners(apps, procs, owner, ka_full=None):
    """GPU 进程的归属（gp）：nvidia-smi 给的宿主机 PID 经 /proc/<pid>/sched 对回容器 PID（scan_procs 的 hpid），再看它属于哪个任务
    （owner {pid: 任务}）、是不是保活（命令行里有 keep_alive），都不是的算 labmon 之外的。
    ka_full（借来的主机上给）：ka.sh 认的保活脚本。ka.sh 认不出（ka_owned：不是 python 跑它，相对路径按工作目录还原后也不是它）的保活
    worker 停不了（other，ka 为真）：那张卡不放；它整棵进程树不算对方的用量（sample 里扣，#37）。"""
    by_hpid = {p["hpid"]: pid for pid, p in procs.items() if p["hpid"]}
    gp = []
    for hp, mb, uu in apps:
        cp = by_hpid.get(hp)
        j = owner.get(cp)
        args, cwd = proc_args(cp) if not j and cp is not None else ([], "")
        keep = any("keep_alive" in a for a in args)
        ext = keep and ka_full is not None and not ka_owned(args, cwd, ka_full)
        gp.append({"hpid": hp, "pid": cp, "mb": mb, "uuid": uu, "comm": procs[cp]["comm"] if cp in procs else None,
                   "owner": "job" if j else "keepalive" if keep and not ext else "other", "ka": ext, "job": j["name"] if j else None, "_j": j})
    return gp


class KeepaliveMixin:

    # ---- 保活 ----
    def ka(self, action):
        """跑 ka.sh 的一个动作，返回 CompletedProcess（stdout 是文字：running 打印保活的进程组）。stop 另收下标准错误（停不下来时
        写进原因，ka_note，#102）；别的动作不收：start 起的保活进程会继承管道，run 就等不到头。"""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}   # 保活用 py38，不能继承项目的补装包
        env["KA_CALLER"] = "labmon-worker:" + self.host
        env["KA_ENV"] = _C.KA_ENV
        return subprocess.run(["bash", _C.KA_SH, action], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE if action == "stop" else subprocess.DEVNULL, text=True)

    def ka_stop(self):
        """ka.sh stop：停了返回 None，没停成返回原因（退出码与它的报错末行）。"""
        r = self.ka("stop")
        tail = ((r.stderr or "").strip().splitlines() or [""])[-1][:200]
        return None if r.returncode == 0 else "ka.sh stop 退出码 %d%s" % (r.returncode, "：" + tail if tail else "")

    def ka_note(self, why):
        """停保活的结果（keepalive 调，#102）：why 为 None 是停下了或保活已不在，否则是没停成的原因。连着没停成满 KA_STUCK_S、
        至少两次算停不了：记一条 ka_stuck 事件（哪个保活、占多少、要谁处理），状态的 keepalive.stuck 给 ka_lines、看板告警、fit 与推演
        照实说、照它一直占着算。worker 照旧每轮再停；任务之间的空档不清（同一段只记一次），停下了、保活没了才清，只写 worker.log。"""
        f, now = self.ka_fail, _C.time.time()
        if why is None:
            if f:
                self.wlog("GPU 保活停下了（之前从 %s 起 %d 次没停成）" % (_C.now_str(f["t0"])[5:16], f["n"]))
            self.ka_fail = None
            return
        if not f or f["why"] != why:
            self.wlog("停 GPU 保活没停成：%s" % why)
        f = self.ka_fail = dict(f or {"t0": now, "n": 0, "said": False}, why=why)
        f["n"] += 1
        if not f["said"] and f["n"] >= 2 and now - f["t0"] >= KA_STUCK_S:
            f["said"] = True
            ka = [g for g in self.gp if g["owner"] == "keepalive"]
            idx = {g["uuid"]: g["i"] for g in self.info.get("gpus") or [] if "uuid" in g}
            which = "、".join("PID %d，卡 %s 占 %.1f GiB" % (g["hpid"], idx.get(g["uuid"], "?"), g["mb"] / 1024) for g in ka) or "保活"
            self.event("ka_stuck", "要卡时停 GPU 保活没停成（%s，从 %s 起 %d 次）：保活（%s）和任务同卡分算力、占着显存，放卡与推演都照它一直占着算，"
                       "worker 每轮再停；%s" % (why, _C.now_str(f["t0"])[5:16], f["n"], which,
                                             "请对方停掉它或查 ka.sh" if self.guest() else "请查 ka.sh"))

    def ka_stuck(self):
        """worker 要卡时停不了的保活（ka_note 判的，记过事件的那段）：{t0, n, why}，没有为 None。状态的 keepalive.stuck。"""
        f = self.ka_fail
        return {k: f[k] for k in ("t0", "n", "why")} if f and f["said"] else None

    def ka_groups(self):
        """保活在跑时为 {进程组: 组长的启动时刻（epoch 秒，读不到为 None）}（ka.sh running 一行一个进程组；ka.sh 用 setsid 起保活，
        组长就是保活进程），没在跑为 None。ka_caller 按它认 events.log 里哪条 RESTORED 是这台的。"""
        r = self.ka("running")
        groups = {int(g): _C.started_at(int(g)) for g in r.stdout.split() if g.isdigit()} if r.returncode == 0 else None
        m = self.ka_mine
        if m and not (groups and m["g"] in groups and _C.pstart(m["g"]) == m["born"]):
            self.ka_mine = None               # 自己开的那个没了（停了、被别人 pkill 了、换了容器）：不再记着
        return groups

    def ka_mark(self):
        """ka.sh start 退出 0 之后：此刻只有一个保活进程组、events.log 里它的 RESTORED 是这台 worker 记的（ka_caller，调用者 labmon-worker:<主机>），
        才记成自己开的（ka_mine：进程组与组长出生时钟）。退出 0 也可能是别人刚开了一个（start 见已在跑就不起），所以不凭退出码认。"""
        groups = self.ka_groups()
        if groups and len(groups) == 1 and self.ka_caller(groups) == "labmon-worker:" + self.host:
            g = next(iter(groups))
            born = _C.pstart(g)
            if born is not None:
                self.ka_mine = {"g": g, "born": born, "t": round(groups[g] or _C.time.time())}

    def keepalive(self, need_gpu):
        if not self.ka_reg:
            return
        pidf = os.path.join(self.ka_reg, "pid")
        if need_gpu:
            if _C.read(pidf) != str(os.getpid()):
                os.makedirs(self.ka_reg, exist_ok=True)
                _C.write(pidf, str(os.getpid()))
            groups = self.ka_groups()
            if groups is not None:           # 别的队列退出时会无条件 ka.sh start，看门狗也会拉起它：这里每轮都看
                if self.ka_off and self.ka_restarted(groups):
                    self.ka_idle, self.ka_why = 0, None
                    return                   # 看门狗正持着平时不持的锁：这一轮不停保活（停了它会再拉一次、又持锁），下一轮再看
                why = self.ka_stop()
                if why is None:
                    self.wlog("停 GPU 保活")
                self.ka_off, self.ka_stop_t = why is None, _C.time.time()
                self.ka_note(why)
            else:
                if self.ka_off and self.ka_wait:
                    self.ka_gone()
                self.ka_off = True
                # ka.sh 说没在跑、GPU 上却有它管的保活（ka_owned）：同样停不了。刚停下的那一分钟里 nvidia-smi 可能还列着正在退出的，不算
                mine = ["PID %d" % g["hpid"] for g in self.gp if g["owner"] == "keepalive"] if _C.time.time() - self.ka_stop_t > 60 else []
                self.ka_note("ka.sh running 说没在跑，GPU 上却有它管的保活（%s）" % "、".join(mine) if mine else None)
            self.ka_idle, self.ka_why = 0, None
            return
        self.ka_off, self.ka_wait = False, None
        if os.path.exists(pidf):
            os.remove(pidf)
        if _C.time.time() - self.ka_checked < _C.KA_CHECK_S:
            return
        self.ka_checked = _C.time.time()
        if self.ka_groups() is not None:
            busy = [_C.gp_label(g) for g in self.gp if g["owner"] != "keepalive"]
            if busy:                          # 有了别的 GPU 进程（借来的主机上多半是对方回来用）：保活让路，同保活守护；空下来再按下面的规则开
                why = self.ka_stop()
                self.wlog("GPU 上有了 %s，停保活让路" % "、".join(busy) + ("" if why is None else "，没停成（%s）" % why))
                self.ka_note(why)
            self.ka_idle, self.ka_why = 0, None
            return
        if self.ka_fail and not any(g["owner"] == "keepalive" for g in self.gp):
            self.ka_note(None)                # 停不了的保活空闲时没了（有人停了）：清掉，免得一直误报、下一段失败沿用旧的起点（#102）
        others = self.ka_others()
        apps = _M.gpu_apps()
        why = ("别的队列登记在跑（%s）" % ", ".join(others) if others else
               "还有进程在用 GPU（PID %s）" % ", ".join(apps) if apps else None)
        if why:
            if why != self.ka_why:
                self.wlog("没有任务要卡，但%s，不拉起保活" % why)
            self.ka_idle, self.ka_why = 0, why
            return
        self.ka_idle += 1                     # 连续两次都空才拉起，避开别的队列两个任务之间的空档
        if self.ka_idle >= 2:
            rc = self.ka("start").returncode
            self.wlog("拉起 GPU 保活" + ("" if rc == 0 else "失败（退出码 %d）" % rc))
            if rc == 0:
                self.ka_mark()
            self.ka_idle, self.ka_why = 0, None

    def ka_others(self):
        """别的登记着在跑的队列（旧 gpu_queue.sh 等，退出时会无条件 ka.sh start）。"""
        return [os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(os.path.dirname(self.ka_reg), "*", "pid"))
                if _C.read_pid(f) != os.getpid() and _C.alive(_C.read_pid(f), ("gpu_queue", "labmon_worker"))]

    def watch_scan(self):
        """在这台上找看门狗（watchdogs）：跳过 labmon 的任务、worker 自己和用着 GPU 的（保活除外）。"""
        skip = self.mine | {os.getpid()} | {g["pid"] for g in self.gp if g["pid"] and g["owner"] != "keepalive"}
        return watchdogs(self.procs or _M.scan_procs(), skip)

    def ka_caller(self, groups):
        """这台此刻在跑的保活（groups：ka_groups 给的 {进程组: 组长的启动时刻}）是谁经 ka.sh 拉起的：events.log 里 ka.sh 记的 RESTORED 的调用者
        （没写 KA_CALLER 的是 ?）；不是经 ka.sh 拉起的为 None。events.log 在共享盘上、各容器的 ka.sh 都写这一份、行里不带主机（#123），
        所以只认这三条都对得上的：进程组是 groups 里的；时刻（带时区）不早于上次停保活；组长的启动时刻读得到时，时刻落在它之后
        KA_LOG_LAG 以内（ka.sh 起来后最多等 20 秒才记，不同容器的 PID 很小、常撞号）。有几条取最近的。"""
        try:
            with open(self.ka_ev, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 8192))
                lines = f.read().decode(errors="replace").splitlines()
        except (OSError, TypeError):
            return None
        for ln in reversed(lines):
            ts, _, rest = ln.partition(" ")
            m = re.match(r"RESTORED 进程组 (\d+)", rest)
            t = ka_log_time(ts) if m else None
            if t is None or t < self.ka_stop_t - 1 or int(m.group(1)) not in groups:
                continue
            t0 = groups[int(m.group(1))]
            if t0 is None or t0 + KA_LOG_LAG[0] <= t <= t0 + KA_LOG_LAG[1]:
                return rest.rpartition("调用者 ")[2].strip() or "?"
        return None

    def ka_restarted(self, groups):
        """要卡的这段里保活停过又被拉起来了（groups：此刻在跑的保活，ka_groups 给的）：找是谁（ka.sh 记的调用者、看门狗），记事件；借来的主机上这时才暂停确定的看门狗
        （平时只列出来，见 watch）；在跑的独占任务的队列日志里记一笔（同卡上保活的那段也记在它用量记录的 with_gpu 里）。
        借来的主机上确定的看门狗正持着平时不持的文件锁（lock_busy）时这一轮不暂停、返回 True，keepalive 也不停保活：
        它多半正在拉起保活、持着对方 run/start 共用的锁，暂停了锁就一直占到还卡（#38）。等它放了再暂停，事件只记一次。"""
        if self.ka_others():
            self.ka_wait = None
            return False                          # 旧 gpu_queue.sh 退出时会无条件拉起，不算打架
        now = _C.time.time()
        sure, maybe = self.watch_scan()
        waited = None
        if self.guest():
            busy, due = self.lock_busy(sure, maybe, now)
            if busy:
                text = "；".join("PID %d %s" % (w["pid"], "还没记过它平时持的锁" if x is None else "正持着 " + lock_text(x) if x else
                                                "平时持的锁是保活刚被拉起时第一次记的，等它放下一把或满 %d 秒" % LOCK_WAIT_S) for w, x in busy)
                if not self.ka_wait:
                    self.ka_wait = {"t0": now, "said": False, "groups": groups}   # 推迟中保活自己没了（ka_gone）时按这份认调用者
                    self.wlog("GPU 保活被拉起，看门狗 %s：这一轮先不暂停、不停保活，下一轮再看" % text)
                elif not self.ka_wait["said"] and now - self.ka_wait["t0"] >= LOCK_WAIT_S:
                    self.ka_wait["said"] = True
                    self.event("watchdog", "看门狗一直持着平时不持的锁已 %d 秒（%s）：不暂停它（暂停会把对方的锁一直占到还卡），"
                               "保活停不下来、和任务同卡，每轮照旧再看" % (now - self.ka_wait["t0"], text))
                return True
            if self.ka_wait:
                waited = now - self.ka_wait["t0"]
        self.ka_wait = None
        frozen = freeze(self.p, sure, maybe) if self.guest() else []
        caller = self.ka_caller(groups)
        self.ka_fought(now, "；".join(x for x in [
            "ka.sh 记的调用者 %s" % caller if caller else "不是经 ka.sh 拉起的",
            "暂停了看门狗 %s，还卡时恢复" % wd_text(frozen) if frozen else "看门狗多半是 %s" % wd_text(sure) if sure else "",
            "" if not frozen or waited is None else
            "推迟 %d 秒：%s 平时持的锁是保活刚被拉起时第一次记的，满 1 分钟没少，到点暂停" % (waited, "、".join("PID %d" % w["pid"] for w in due)) if due else
            "等它放了平时不持的锁才暂停（推迟 %d 秒）" % waited,
            "可能有关：%s" % wd_text(maybe) if maybe else "", "没找到拉起它的进程" if not (sure or maybe) else ""] if x))
        return False

    def ka_gone(self):
        """推迟暂停看门狗（ka_wait）时保活自己没了（例如同卡显存不够、CUDA OOM）：推迟到此为止，这次被拉起照样记一次；看门狗没暂停。"""
        now, caller = _C.time.time(), self.ka_caller(self.ka_wait["groups"])
        waited, self.ka_wait = now - self.ka_wait["t0"], None
        self.ka_fought(now, "；".join(x for x in ["ka.sh 记的调用者 %s" % caller if caller else "不是经 ka.sh 拉起的",
                                                   "等看门狗放了平时不持的锁时（推迟 %d 秒）保活自己退了，看门狗没暂停" % waited] if x), "它自己退了")

    def ka_fought(self, now, who, how="已停"):
        """保活被拉起这一次记下来：近 FIGHT_S 秒的次数、状态的 kafight、在跑的独占任务的队列日志、“保活被拉起”事件。how：保活后来怎样了。"""
        self.ka_hits = [t for t in self.ka_hits if t > now - FIGHT_S] + [now]
        self.fight = {"t": round(now), "who": who}
        for j in self.running.values():
            if j.get("excl") and j.get("q"):
                self.qlog(j["q"], "%s 运行中 GPU 保活被拉起过一次（%s），%s；这段的计时可能受了影响" % (j["name"], who, how))
        self.event("ka_fight", "GPU 保活在任务运行中被拉起（%d 小时内第 %d 次），%s；%s" % (FIGHT_S // 3600, len(self.ka_hits), how, who))

    def lock_busy(self, sure, maybe, t):
        """确定的看门狗里这一轮还不能暂停的 busy：[(看门狗, x)]。x 为多出来的锁 {键: 路径}：此刻持着平时不持的文件锁；
        None：还没记过它平时持的锁（上次找过之后才出现）；{}：平时持的锁是要卡这段里第一次记的（locks_t，那时它可能正拉回保活、持着锁），
        这次没比那份少、又不到 LOCK_WAIT_S。平时持的锁是 frozen.json 里 seen 的 locks（note_seen 记，同一进程每次与上次取交集；
        BOB 的 watchdog 一生持着自己的单例锁，所以不能“持锁就不暂停”）。此刻一把锁都不持的、已暂停的（停着拿不了新锁，freeze 对它只是
        再发一次 SIGSTOP）不看；读不到 PROC/locks 的不算（照旧暂停）。有要等的就顺手把这次读到的记进 seen（没记过的记下第一份，带上 t）。
        另返回 due：放过的里头因 locks_t 满 LOCK_WAIT_S、到点放过的（这次推迟的期间那份还在起作用），事件里不写成“等它放了锁”。"""
        d = load_frozen(self.p)
        base, held = lock_base(d["seen"]), {(w["pid"], w["start"]) for w in d["frozen"]}
        now = {w["pid"]: held_locks(w["pid"]) for w in sure}
        t0 = self.ka_wait["t0"] if self.ka_wait else t
        busy, due = [], []
        for w in sure:
            cur, prev = now[w["pid"]], base.get((w["pid"], w["start"]))
            if not cur or (w["pid"], w["start"]) in held:
                continue
            if prev is None:
                busy.append((w, None))
                continue
            extra = {k: v for k, v in cur.items() if k not in prev["locks"]}
            same = not extra and len(cur) == len(prev["locks"]) and prev.get("locks_t")
            if extra or same and t - prev["locks_t"] < LOCK_WAIT_S:
                busy.append((w, extra))
            elif same and prev["locks_t"] + LOCK_WAIT_S > t0:
                due.append(w)
        if busy:
            self.note_seen(sure, maybe, now, t)
        return busy, due

    def note_seen(self, sure, maybe, now=None, t=None):
        """找到的看门狗记进 frozen.json 的 seen（已暂停的不再列）：确定的 why 接 SURE_WAIT，带上它平时持的锁 locks（这次读到的与上次记的取交集，
        只出现过一次的筛掉；读不到 PROC/locks 的沿用上次的）。now 是已读好的 {pid: held_locks}。t 给了是要卡这段里（保活被拉起时的 lock_busy，
        或保活已被 worker 停着时的 watch：看门狗这时可能正拉回保活、持着锁）：这时才第一次记的带上 locks_t = t（lock_busy 据此等它放下一把），之后沿用。"""
        now = now or {w["pid"]: held_locks(w["pid"]) for w in sure}
        base = lock_base(load_frozen(self.p)["seen"])
        seen = []
        for w in sure:
            cur, prev = now.get(w["pid"]), base.get((w["pid"], w["start"]))
            x = dict(w, why=w["why"] + SURE_WAIT)
            if prev is not None:
                x["locks"] = prev["locks"] if cur is None else sorted(set(cur) & set(prev["locks"]))
                if prev.get("locks_t"):
                    x["locks_t"] = prev["locks_t"]
            elif cur is not None:
                x["locks"] = sorted(cur)
                if t is not None:
                    x["locks_t"] = round(t, 3)
            seen.append(x)
        freeze(self.p, seen=seen + maybe)

    def watch(self, now):
        """借来的主机：每 WATCH_EVERY 秒找一遍看门狗（第一轮就是借来时），只列出来（frozen.json 的 seen），新的各记一次事件。
        不预先暂停：组里的 ka-watchdog.sh 认 labmon 的登记、不会来拉；真把保活拉起来了 ka_restarted 才暂停确定的。"""
        if not self.guest() or now - self.watch_t < WATCH_EVERY:
            return
        self.watch_t = now
        sure, maybe = self.watch_scan()
        self.note_seen(sure, maybe, t=_C.time.time() if self.ka_off else None)
        fresh = [w for w in load_frozen(self.p)["seen"] if (w["pid"], w["start"], w["cmd"]) not in self.watch_seen]
        self.watch_seen |= {(w["pid"], w["start"], w["cmd"]) for w in fresh}
        if fresh:
            self.event("watchdog", "找到保活看门狗，没动（把保活拉起来了才暂停确定的）：%s" % wd_text(fresh))

    # ---- 借来的主机（guest 指令）：给对方留的量、对方超出预留、快撞上限 ----
    def guest(self):
        return next((o for o in self.ovr if o["kind"] == "guest"), None)

    def keep(self):
        """给对方留的量 {vram: 每张对方的卡 GiB, ram: GiB, cpu: 核}；不是借来的、或指令没带（旧指令）为 None。"""
        return (self.guest() or {}).get("keep")

    def away(self):
        """对方说好不用到几点（guest 指令的 keep.away，epoch 秒）：还在这段里、这次没作废（end_away）时返回那个时刻，否则 None。"""
        a = (self.keep() or {}).get("away")
        if self.away_end and self.away_end["until"] != a:     # 看板重下了另一次说好不用（改约定时重新勾）：上次的作废不算
            self.away_end = None
        return a if a and _C.time.time() < a and not self.away_end else None

    def held(self):
        """要不要预先给对方留（准入时扣掉对方还没用上的预留）：借来时对方在轻用（keep.hold），且不在对方说好不用的时段里。"""
        k = self.keep()
        return bool(k) and k.get("hold", True) and not self.away()

    def end_away(self, t, why):
        """对方说好不用、却回来了（有了 GPU 进程、内核杀了 labmon 之外的进程）：这次说好不用作废，立刻按普通借卡规则；
        状态的 guest.away.end 告诉看板，看板清掉主机清单的 borrow_away、重下指令、提醒一次。"""
        a = self.away()
        if a:
            self.away_end = {"until": a, "t": round(t), "why": why}
            self.event("guest", "对方说好 %s 前不用，现在回来了（%s）：恢复按预留借卡" % (_C.now_str(a)[5:16], why), what=[why])

    def away_state(self):
        """状态的 guest.away：{until: 说好不用到几点, end: 作废的时刻（没作废为 None）, why}；指令没带 away 为 None。"""
        self.away()                                          # 指令换了的先清掉上次的作废
        a = (self.keep() or {}).get("away")
        if not a:
            return None
        e = self.away_end if self.away_end and self.away_end["until"] == a else {}
        return {"until": a, "end": e.get("t"), "why": e.get("why")}

    def keep_left(self, i, uuid):
        """这张卡给对方留着、对方还没用上的显存（MB）。对方的卡：单卡就是它，多卡是 0 号卡与对方有进程的卡；别的卡不留。
        借来时对方空着的（keep.hold 为假）与对方说好不用的时段里（away）不预先留。"""
        k, other = self.keep(), self.other["vram"].get(uuid, 0.0)
        if not k or not self.held() or (len(self.info.get("gpus") or []) > 1 and i != 0 and not other):
            return 0.0
        return max(0.0, k.get("vram", 0.0) * 1024 - other)

    def guest_scan(self, t):
        """借来的主机：对方哪几项超出预留（各卡显存、内存、CPU），变了记事件。超出的一项回到预留以内后 GUEST_QUIET_S 内仍算着
        （back_last），那部分不放新任务。借来时对方空着的（keep.hold 为假）与指令没带 keep 的（旧指令）：对方在哪张卡上有进程就算那张卡超出。
        卡上有 ka.sh 停不了的保活（对方另起的）也算那张卡超出。
        对方说好不用的时段里（away）：不判内存、CPU（此刻占着的照算在可用量外，见 mem_room、cpu_room），之前的超出也不再挡；
        这次说好不用开始时对方已有的 GPU 进程照算在可用量外、不算超出（变大了也不算回来）。之后新出现的对方 GPU 进程：那张卡当轮就不放新任务，
        连着两轮都在才算对方回来了（end_away），这一轮起按普通规则；新起的 ka.sh 停不了的保活不算回来，只挡它那张卡（#37）。勾上前已记的超出（back_last 里早于这次开始的，卡也算）这一轮起不挡。
        基线在这次第一轮读到 GPU 表时取；到点、作废、指令不带 away 时清掉，下次重新取（约定时间相同也一样）。worker 重启后以重启时对方已有的进程为准。"""
        if not self.guest():
            self.back, self.back_last, self.tight, self.cpu_hot, self.away_base, self.owner = {}, {}, None, 0, None, []
            return
        k, o, over = self.keep(), self.other, {}
        new = set()                                    # 说好不用开始以后新出现对方 GPU 进程的卡
        a = self.away()
        if a and not self.away_base and self.info.get("gpus"):   # 这次说好不用的第一轮：对方此刻已有的（轻用时开着的 Jupyter 之类）不算回来；没读到 GPU 表的那轮不取
            self.away_base = {"until": a, "t0": t, "pids": {p for v in o["gid"].values() for p in v}, "n": 0}
        if a and self.away_base:                       # ka.sh 停不了的保活不算回来（只占位、不算对方干活，维护者 11-09 定，#37）：那张卡照样不放（下面 ka 那条）
            new = {u for u, v in o["gid"].items() if v - self.away_base["pids"] - set(o["ka"].get(u) or ())}
            self.away_base["n"] = self.away_base["n"] + 1 if new else 0
            if self.away_base["n"] >= 2:               # 连着两轮才算回来：归属只看一次采样会误判（任务刚结束 nvidia-smi 还列着、宿主 PID 对不上）
                self.end_away(t, "卡 %s 上有了对方的 GPU 进程" % ",".join(str(g["i"]) for g in self.info.get("gpus") or [] if g["uuid"] in new))
        away = self.away()
        if not away:                                   # 到点、作废、指令不带 away：下次说好不用（哪怕约定时间相同）重新取基线、计数从 0 起
            self.away_base = None
        lim = k.get("vram", 0.0) * 1024 if self.held() else 0.0
        for g in self.info.get("gpus") or []:
            mb, ka = o["vram"].get(g["uuid"], 0.0), o["ka"].get(g["uuid"])
            if away and g["uuid"] not in new and not ka:   # 说好不用：开始时已有的对方进程照算在可用量外（card_free 扣它），不算超出
                continue
            if ka or (mb and mb > lim):                # ka.sh 停不了的保活：占着算力、worker 停不了，那张卡不放新任务
                over[g["uuid"]] = "卡 %d：对方用 %.1f GiB 显存" % (g["i"], mb / 1024) + (
                    "，其中保活 PID %s 不归 ka.sh 管、停不了，要停请对方停" % "、".join(map(str, ka)) if ka else
                    "，预留 %g" % k["vram"] if lim else "，说好不用、这是新出现的" if away else "，借来时对方空着")
        if k and not away and o["ram"] is not None and o["ram"] > k.get("ram", 0.0):
            over["ram"] = "内存：对方用 %.1f GiB，预留 %g" % (o["ram"], k["ram"])
        # cpu_hot 是“对方 CPU 连着几轮超出预留”这件事本身，说好不用期间照样累计，只是不判超出；作废那一轮起已连着两轮的立刻算超出
        self.cpu_hot = self.cpu_hot + 1 if k and o["cpu"] is not None and o["cpu"] > k.get("cpu", 0.0) else 0
        if self.cpu_hot >= 2 and not away:             # 连着两轮才算：刚停的保活那一段的核数会算到对方头上（ka_cores），一轮的尖峰不算
            over["cpu"] = "CPU：对方用 %.1f 核，预留 %g" % (o["cpu"], k["cpu"])
        for key in over:
            self.back_last[key] = t
        self.back_last = {key: v for key, v in self.back_last.items() if key in over or t - v < _C.GUEST_QUIET_S}
        if away and self.away_base:                    # 说好不用：开始以前的超出（卡、内存、CPU）不再挡（guest_block 与快照的 owner 都看 back_last），这一轮起就放行
            self.back_last = {key: v for key, v in self.back_last.items() if key in over or v > self.away_base["t0"]}
        # 主人回来（#25）：超出的几项里去掉只因 ka.sh 停不了的保活超出的卡（那张卡上对方的进程都是它：保活只占位、不算主人干活，#37）
        gid = o.get("gid") or {}
        self.owner = [v for key, v in over.items() if key in ("ram", "cpu") or not (gid.get(key) and gid[key] <= set(o["ka"].get(key) or ()))]
        if (k or {}).get("pool"):                      # 算力池只在 GPU 上除保活外没有进程时借：之后卡上有了对方的 GPU 进程（停不了的保活除外）就算回来，预留以内也算
            self.owner += ["卡 %d：有了对方的 GPU 进程（PID %s）" % (g["i"], "、".join(map(str, sorted(gid[g["uuid"]] - set(o["ka"].get(g["uuid"]) or ())))))
                           for g in self.info.get("gpus") or [] if g["uuid"] not in over and gid.get(g["uuid"], set()) - set(o["ka"].get(g["uuid"]) or ())]
        if bool(over) != bool(self.back):
            self.event("guest", "借来的主机上对方用得比预留多（%s）：这部分不再放新任务" % "；".join(over.values()) if over else
                       "对方说好 %s 前不用：不再按预留挡，照常放行" % _C.now_str(away)[5:16] if away else
                       "对方回到预留以内，%d 分钟后恢复放行" % (_C.GUEST_QUIET_S / 60), what=list(over.values()))
        self.back = over

    def guest_block(self):
        """借来的主机上整台不放新任务的原因（对方的内存或 CPU 超出预留、或回到预留以内还不到 GUEST_QUIET_S；
        算力池借来的（guest 指令的 keep.pool，#25）主人回来了（owner）也整台不放：看板同一轮要还）；没有为 None。"""
        if (self.keep() or {}).get("pool") and self.owner:
            return "算力池借来的主机：主人回来了（%s），整台不放新任务，在跑的跑完就还" % "；".join(self.owner[:3])
        hit = [k for k in ("ram", "cpu") if k in self.back_last]
        if not hit:
            return None
        return "借来的主机：" + "；".join(self.back.get(k) or "对方的%s %d 分钟前超出预留" % (
            {"ram": "内存", "cpu": "CPU"}[k], (_C.time.time() - self.back_last[k]) / 60) for k in hit) + "，暂不放行"

    def guest_tight(self, t):
        """借来的主机快撞上限：对方超出预留，且那张卡空闲显存不到 TIGHT_FREE_MB、或容器内存到 TIGHT_MEM。不停正在跑的
        （这一格跑完，停了白跑；要不要停由用户线下和对方商量），超出的部分本来就不放新任务；变了记事件，状态里的 guest.tight 给看板提醒。"""
        why = None
        for g in self.info.get("gpus") or []:
            if g["uuid"] in self.back and g["free"] < _C.TIGHT_FREE_MB:
                why = "%s，这张卡只剩 %.0f MB" % (self.back[g["uuid"]], g["free"])
        m = self.info.get("mem") or {}
        if not why and "ram" in self.back and m.get("limit") and m["anon"] / m["limit"] >= _C.TIGHT_MEM:
            why = "%s，容器内存（不算页缓存）%.0f / %.0f GiB" % (self.back["ram"], m["anon"], m["limit"])
        if why and not self.tight:
            self.event("guest", "借来的主机快撞上限（%s）：正在跑的照常跑完，不放新任务" % why, what=[why])
        self.tight = why

    def squeezed_by(self, j, oom):
        """借来的主机上失败的清单任务是不是被对方挤掉的（oom 是 oom_of 认出的这一次 OOM）：容器内存撞上限被内核杀掉——我们的 oom_score_adj 最高，
        先杀的是我们；或 CUDA OOM 且对方在它的卡上用过显存。是就返回说明；被挤掉的不算我们的 OOM，不抬同类的估计（finish）。"""
        if not self.guest():
            return None
        if oom == "ram":
            self.oom_ours += 1
            return "容器内存撞上限，被内核杀掉"
        if oom == "vram" and j.get("owner_gpu"):
            return "对方在同一张卡上，显存不够（CUDA out of memory）"
        return None

    def oom_check(self, t):
        """容器 oom_kill 计数涨了、又不是我们的任务被杀（这一轮 finish 里 squeezed_by 认下的）：内核杀了 labmon 之外的进程，
        借来的主机上多半是对方的，记下时刻 hit（看板据此提醒、调高预留）、作废对方说好不用（end_away），留 VOID_KEEP_S。
        上一轮还有我们的任务进程（self.mine，sample 末尾才换成这一轮的）或有任务在跑时不算：我们的 oom_score_adj 是 1000（GUEST_ADJ），
        内核一定先杀我们的，squeezed_by 认不出的（DataLoader 子进程、某个 rank 被杀，任务以退出码 1 退出或接着跑）也是我们的。"""
        n = (self.info.get("mem") or {}).get("oom_kill")
        if n is not None and self.oom_n is not None:
            k = int(n - self.oom_n) - self.oom_ours
            if k > 0 and self.guest() and (self.mine or self.running):
                self.event("guest", "容器内存撞上限，内核杀了进程 %d 次：我们的任务在跑、最先被杀，不算对方的" % k)
            elif k > 0 and self.guest():
                self.hit = round(t)
                self.event("owner_oom", "容器内存撞上限，内核杀了 labmon 之外的进程 %d 次（多半是对方的）" % k)
                self.end_away(t, "内核杀了 labmon 之外的进程")
        if self.hit and t - self.hit > _C.VOID_KEEP_S:
            self.hit = None
        self.oom_n, self.oom_ours = n, 0
