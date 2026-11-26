"""worker 队列登记、清单解析、current 状态与本地依赖（needs= / stage）。"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess

from . import common as _C
from . import pure as _P

def qslug(root, jobs):
    """登记文件名：目录末级名 + 清单路径 + 完整目录的短哈希（两个项目的子目录同名、清单路径也相同时不撞）。"""
    return _C.NAME_RE.sub("_", os.path.basename(root.rstrip("/")) + "__" + jobs)[:110] + "__" + hashlib.sha1(root.encode()).hexdigest()[:6]


def regs(qdir, *, cache=None, with_meta=False):
    """一台主机登记目录（Paths.queues）下的登记：[(文件, 登记内容（带 _slug）或读坏了时的异常)]，按登记文件名（qslug）的顺序，不是登记先后。
    登记文件只在这里读：reg_files、registry（worker add 查状态目录、fit 找清单行）、Worker.registry、Worker.pool_todo 都经它（#164）。
    cache：可选的 {文件: (rsig, st_mtime, reg)} 缓存；只缓存成功读到的 dict，临时读取失败或坏 JSON 不缓存，下一轮自动重试；已消失的文件一并清理。
    with_meta=True 时返回 [(文件, 登记内容或异常, rsig, st_mtime, changed)]（rsig 为文件签名，changed 表示相较缓存有更新）。"""
    out, seen = [], set()
    for f in sorted(glob.glob(os.path.join(qdir, "*.json"))):
        if cache is not None or with_meta:
            try:
                st = os.stat(f)
            except OSError:                       # 这一轮刚被 rm、被 worker add 搬走
                continue
            seen.add(f)
            rsig, mtime = (st.st_ino, st.st_mtime_ns, st.st_size), st.st_mtime
            cached = cache.get(f) if cache is not None else None
            if cached and cached[0] == rsig:
                reg, changed = cached[2], False
            else:
                changed = cached is not None
                try:
                    with open(f) as fh:
                        reg = json.load(fh)
                    if not isinstance(reg, dict):
                        raise TypeError("登记文件不是 JSON 对象")
                    reg["_slug"] = os.path.basename(f)[:-5]
                except (OSError, ValueError, TypeError, AttributeError) as e:
                    reg = e
                    if cache is not None:
                        cache.pop(f, None)        # 不按文件签名永久缓存临时 I/O 异常或坏文件
                else:
                    if cache is not None:
                        cache[f] = (rsig, mtime, reg)
            out.append((f, reg, rsig, mtime, changed) if with_meta else (f, reg))
        else:
            try:
                with open(f) as fh:
                    reg = json.load(fh)
                if not isinstance(reg, dict):
                    raise TypeError("登记文件不是 JSON 对象")
                reg["_slug"] = os.path.basename(f)[:-5]
                out.append((f, reg))
            except (OSError, ValueError, TypeError, AttributeError) as e:
                out.append((f, e))
    if cache is not None:
        for k in [k for k in cache if k not in seen]:
            cache.pop(k, None)
    return out


def registry(qdir, *, read_env=True):
    """登记的队列（load_queue 载入，带登记文件 file）与坏了的登记 [(文件, 异常)]（不是 JSON、缺 root / jobs / project）。
    read_env=False 时不读 .env、不查其存在（peers 认归属、休眠登记用，省掉共享盘 I/O）。"""
    qs, bad = [], []
    for f, reg in regs(qdir):
        try:
            if isinstance(reg, Exception):
                raise reg
            qs.append(dict(load_queue(reg, read_env=read_env), file=f))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            bad.append((f, e))
    return qs, bad


def matching_regs(qdir, root, jobs, *, aliases=False, by_file=False):
    """一台主机的登记目录 qdir 里登记过这条队列（同一目录、同一清单）的文件：按内容认，11-06 前不带哈希的旧文件名也认得；缺 project 的旧登记也认（rm 删得掉）。
    by_file（rm 用，#77）：ROOT 与 JOBS 拼起来规整后是同一个清单文件就算，不管从哪里断开（status 列的是拼起来的 ROOT/JOBS，看不出登记时在哪断开），
    只比文字、不碰文件，目录已经删了的也认得。aliases：另认经符号链接是同一个文件的（文件要还在）。"""
    def same(reg):
        r, j = reg.get("root"), reg.get("jobs")
        if os.path.normpath(r or "") == os.path.normpath(root) and j == jobs:
            return True
        if by_file and _P.same_list(reg, root, jobs):
            return True
        if aliases and r and j:
            try:
                return os.path.samefile(os.path.join(r, j), os.path.join(root, jobs))
            except OSError:
                pass
        return False

    return [(f, reg) for f, reg in regs(qdir) if isinstance(reg, dict) and same(reg)]


def reg_files(qdir, root, jobs):
    return [f for f, _ in matching_regs(qdir, root, jobs)]


def reg_added(reg):
    """登记时刻：worker add 写的文字（这台容器的时钟，now_str）→ epoch 秒；没写、写坏了为 None。"""
    try:
        return int(_C.time.mktime(_C.time.strptime(reg.get("added") or "", "%Y-%m-%d %H:%M:%S")))
    except (ValueError, TypeError):
        return None


def reg_dirs():
    """各主机的登记目录 {主机: Paths.queues}（共享盘上同一个 HOME，含已归还的、主机清单里已没有的）：worker add 搬登记、查共用状态目录用（#95）。"""
    return {os.path.basename(os.path.dirname(d)): d for d in sorted(glob.glob(os.path.join(_C.HOME, "hosts", "*", "queues")))}


def queue_state_conflict(queue, host, directories):
    """登记锁内检查状态目录唯一（worker add 用）。"""
    for h, directory in directories.items():
        for other in registry(directory)[0]:
            same = os.path.normpath(other["root"]) == os.path.normpath(queue["root"]) and other["jobs"] == queue["jobs"]
            if not same and os.path.normpath(other["state"]) == os.path.normpath(queue["state"]):
                return "状态目录 %s 已被%s %s/%s 用着：在清单旁的 .env 里给这条写不同的 QUEUE_STATE" % (
                    queue["state"], "" if h == host else "主机 %s 上登记的" % h, other["root"], other["jobs"])
    return None


def reg_lock(*, required=False):
    """登记归属的锁（共享盘上同一个 HOME 下的 hosts/registry.lock，各主机共用，#95）：worker add 查、写、比、删登记，与 Worker.start
    核登记还在、没有 current 到写下 current，都在它里面。搬登记与开跑因此不会交错：搬过去的那台要么看得到原主机刚写的 current，要么原主机看得到登记已搬走。"""
    hosts = os.path.join(_C.HOME, "hosts")
    os.makedirs(hosts, exist_ok=True)
    return _C.flock(os.path.join(hosts, "registry"), required=required)


def env_path(root, jobs):
    """一条清单的 .env 路径（清单名 .txt 换成 .env；load_queue 与 offers 比两条清单的环境共用。看板探针 probe.py 不 import 本文件，另有一份）。"""
    return os.path.join(root, re.sub(r"\.txt$", ".env", jobs))


def load_queue(reg, *, read_env=True):
    """登记文件 → 队列：清单路径、.env、状态与日志目录（解析方式与看板探针相同）。
    read_env=False 时跳过读 .env（休眠登记与 peers 认归属用，不碰清单目录里的 .env）。"""
    root, jobs = reg["root"], reg["jobs"]
    project = reg["project"]
    if read_env:
        envf = env_path(root, jobs)
        env_raw = _C.read(envf, None)
        env_ok = env_raw is not None or not os.path.exists(envf)
        env_txt = env_raw or ""

        def env(key, default):
            m = re.search(r"^%s=(\S+)" % key, env_txt, re.M)
            return os.path.join(root, (m.group(1) if m else default).strip("\"'"))

        ev = envf if os.path.exists(envf) else None
        st, lg = env("QUEUE_STATE", "results/_queue"), env("QUEUE_LOGS", "results/_logs")
    else:
        ev, st, lg, env_ok = None, None, None, True
    return {"key": reg["_slug"], "root": root, "jobs": jobs, "jobs_path": os.path.join(root, jobs),
            "env": ev, "_env_ok": env_ok, "gpus": reg.get("gpus"),
            # 项目 id 就是登记时写的 --project（add 必写）
            "project": project, "stop": reg.get("stop") or "boundary",
            "pool": bool(reg.get("pool")),        # 算力池放过来的（#79，worker add --pool）：看板认哪些清单是池放的（自用主机上与主人的清单分开），offers 不叫会话挪它
            "added_t": reg_added(reg),            # 登记时刻（epoch 秒）：状态里带出去，看板在同一清单登记在两台在用主机上时取晚的（collect.reg_queues）
            "state": st, "logs": lg}


def hints(cmd):
    """清单行尾（或单元申请）的资源提示：gpus=（缺省 1；未写 gpus= 且显式写 vram=0 时按 0 不用卡处理）vram=（每张卡 GiB）ram=（GiB）tag= exclusive= est_min=，
    以及 needs=（这格要用的本地文件，逗号分隔，可写变量与通配符；本地<共享盘主副本 的写了来源，见 check_needs）、
    after= / afterany=（依赖：等这些任务跑成功 / 结束才开始，逗号分隔，可带 项目/ 前缀，见 dep_keys；记在 deps、deps_any）。
    cpus=（要几个核：CPU 记账优先按它，没写按同类历史，见 estimate；也是开跑时导出的 LABMON_CPUS，开跑时给的核数）、io=heavy|light（读写重不重：只记录与显示，准入不看）。
    gang=组名:人数（成组：同项目同组名的几行（可在不同主机、不同清单）都留好资源再一起开跑，见 Worker.gang_sync；记在 gang、gn，没写人数 gn 为 None）。
    提示键前面不能是 - 或单词字符：命令自己的参数 --gpus=0、--ram=64 不算（旧清单有不带 # 的行内提示，不能只认 # 之后）；needs=、after=、gang= 只认 # 之后。"""
    f = lambda k: re.search(r"(?<![\w-])%s=(\d+(?:\.\d+)?)" % k, cmd)
    g, v, r, e, c = f("gpus"), f("vram"), f("ram"), f("est_min"), f("cpus")
    t, x = re.search(r"(?<![\w-])tag=([\w.-]+)", cmd), re.search(r"(?<![\w-])exclusive=(gpu|host)\b", cmd)
    io = re.search(r"(?<![\w-])io=(heavy|light)\b", cmd)
    tail = cmd[len(body(cmd)):]
    gg = re.search(r"(?<![\w-])gang=([\w.-]+)(?::(\d+))?", tail)
    vram = float(v.group(1)) if v else 0.0
    ngpu = int(float(g.group(1))) if g else (0 if v and vram == 0.0 else 1)
    return {"ngpu": ngpu, "vram": vram, "ram": float(r.group(1)) if r else 0.0,
            "tag": t.group(1) if t else None, "excl": x.group(1) if x else None, "est_min": float(e.group(1)) if e else None,
            "cpus": float(c.group(1)) if c else None, "io": io.group(1) if io else None,
            "needs": [p for v in re.findall(r"\bneeds=(\S+)", tail) for p in v.split(",") if p],
            "deps": [a for v in re.findall(r"(?<![\w-])after=(\S+)", tail) for a in v.split(",") if a],
            "deps_any": [a for v in re.findall(r"(?<![\w-])afterany=(\S+)", tail) for a in v.split(",") if a],
            "gang": gg.group(1) if gg else None, "gn": int(gg.group(2)) if gg and gg.group(2) else None}


def rows_left(state, rows, cur=()):
    """清单里还会跑的行（list_state 写 todo、forecast 推演共用）：返回 (状态目录里的标记，读不了为 None,
    [(任务, 资源提示)] 去掉标了 .done、.failed 的与在跑的 cur（任务名的集合：并行清单几行在跑，#147）)。还没跑过的清单没有状态目录：标记为 None，行都算还会跑。"""
    try:
        ms = set(os.listdir(state))
    except OSError:
        ms = None
    m, cur = ms or (), set(cur or ())
    return ms, [(n, h) for n, h in rows if n not in cur and n + ".done" not in m and n + ".failed" not in m]


def first_rows(rows):
    """[(任务, X)] → {任务: X}：同名的取第一条（worker 跑的是第一条：next_job 取第一个没标完的，它跑完后同名的后一条因名字已标完永远不跑；
    job_cmd 同理）。按名字取清单行的资源提示都经它（proj_lanes、queue_durs）。"""
    out = {}
    for n, x in rows:
        out.setdefault(n, x)
    return out


def list_jobs(q):
    """清单里的任务 [(名称, 命令)]：跳过空行、# 注释与没有 | 的行。顺带把清单声明的并行数记进 q["par"]（list_par；读不到清单为 1），
    并把正文是否读成功记进 q["_jobs_ok"]（用于休眠判定：临时读取失败不能当成空清单进休眠，#164）。"""
    text = _C.read(q["jobs_path"], None)
    q["_jobs_ok"] = text is not None
    q["par"] = list_par(text or "")
    return parse_jobs(text or "")


def list_par(text):
    """清单声明的并行数（#147）：清单里一行 `# labmon: par=N`（位置不限，取第一处），这条清单在这台上最多同时跑 N 行；没写、写错为 1（串行，同以前）。
    封顶 PAR_MAX。写在清单里，清单搬到哪台（worker add、页面换主机、算力池）都跟着走；旧 worker 当注释，照旧串行。"""
    m = _C.PAR_RE.search(text or "")
    return min(_C.PAR_MAX, max(1, int(m.group(1)))) if m else 1


def run_key(q, name):
    """清单任务的请求 key 与运行中任务的 key（#147）：串行清单（par=1）就是队列 key（同以前，状态、接管都不变），并行清单是 `队列 key|任务名`，
    按 key 记的（等了多久 wsince、独占留卡、成组留的资源、等待原因、推演里的 starts）因此逐行独立。车道身份一律按队列 key（运行中的 j["q"]["key"]、请求的 r["q"]["key"]）认。"""
    return q["key"] if (q.get("par") or 1) <= 1 else q["key"] + "|" + name


def parse_jobs(text):
    out = []
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "|" in line:
            name, cmd = line.split("|", 1)
            out.append((name.strip(), cmd))
    return out


def job_of(name, cmd):
    """清单里的一行 → 任务（资源提示加名字与命令）：next_job 与 fit 共用。"""
    h = hints(cmd)
    if _C.is_hold(name):                             # 占位任务只等条件、不算：不独占、不要卡（漏写 gpus=0 时也不占卡、不为它停保活），也不成组
        h["excl"], h["ngpu"], h["gang"], h["gn"] = None, 0, None, None
    return dict(h, name=name, cmd=cmd.strip())


def next_jobs(q, busy=(), rows=None):
    """清单的窗口（#147）：按清单顺序取没有 .done / .failed、也不在跑（busy：这条清单此刻在跑的任务名）的前 par − 在跑数 行，[任务（job_of）]。
    同名的只认第一条（first_rows 同一口径：同名的后一条不会和它同时跑）。par=1 时就是以前的队首（不在跑时一行，在跑时没有）。
    窗口里等依赖的行照样占一格（同以前队首等依赖就挡住整条清单）。rows 是已经读好的 list_jobs(q)（q["par"] 已记下），不给就现读。"""
    try:
        ms = set(os.listdir(q["state"]))
    except OSError:
        ms = set()
    rows = list_jobs(q) if rows is None else rows
    out, seen, n = [], set(busy), q["par"] - len(set(busy))
    for name, cmd in rows:
        if len(out) >= n:
            break
        if name in seen or name + ".done" in ms or name + ".failed" in ms:
            seen.add(name)
            continue
        seen.add(name)
        out.append(job_of(name, cmd))
    return out


def new_req(kind, key, project, name, **kw):
    """一个等待放行的请求（清单队首 kind=queue，或一次单元申请 kind=slot）：tick、slot_reqs 与测试都从这里拼，字段只在这一处列全。
    资源提示（ngpu、vram、ram、tag、excl、est_min、cpus、io、needs、deps、deps_any、gang、gn）的缺省同 hints；清单任务另带 q、root、queue，单元另带 id、pid、t（申请时刻）。
    est、held、stall、since、dur、charge 按主机现状算，由 Worker.prep 补。单元申请不成组（slot_reqs 不带 gang）。"""
    r = {"key": key, "kind": kind, "project": project, "name": name, "ngpu": 1, "vram": 0.0, "ram": 0.0, "tag": None, "excl": None,
         "est_min": None, "cpus": None, "io": None, "needs": [], "deps": [], "deps_any": [], "gang": None, "gn": None, "cmd": "", "q": None, "root": None, "queue": None}
    r.update(kw)
    return r


def queue_req(q, job):
    """清单队列 q 里的任务 job（job_of）→ 请求：真放行（Worker.requests）与 fit 共用。key 按 run_key（并行清单逐行一个，#147）。"""
    return new_req("queue", run_key(q, job["name"]), q["project"], q=q, root=q["root"], queue=q["jobs"], **job)


def read_current(state):
    """状态目录的 current：一行一个在跑的任务（`名称 PID 开始日期 开始时刻 [放行的主机]`；并行清单有几行，#147；看板探针 probe.py 另读，放行的主机它也认，#95），
    返回 [(名称, PID, 开始的 epoch 秒, 放行的主机)]，没有为 []。旧读法（整个文件 split）读到的正是第一行。
    放行的主机 11-10 起 worker 写（#88），旧 gpu_queue.sh 与之前的 worker 不写，为 None。PID 写坏了为 0；没写时刻的（很旧的文件）算现在。
    接管（adopt）、别的主机在不在管它（run_elsewhere）与旁观旧 gpu_queue.sh 的任务（ext_jobs）共用。"""
    out = []
    for line in (_C.read(os.path.join(state, "current")) or "").splitlines():
        p = line.split()
        if not p:
            continue
        try:
            t0 = _C.time.mktime(_C.time.strptime(" ".join(p[2:4]), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            t0 = _C.time.time()
        out.append((p[0], int(p[1]) if len(p) > 1 and p[1].isdigit() else 0, t0, p[4] if len(p) > 4 else None))
    return out


def drop_current(state, name, pid=None):
    """从 current 里删掉一个任务那一行（收尾、记中断；pid 给了只删 PID 对得上的），删空了删文件。只有放行它的那台写这条清单的 current
    （别的主机见到它不是自己的行就整条清单等，run_elsewhere），所以读改写不用另加锁。"""
    f = os.path.join(state, "current")
    lines = [l for l in (_C.read(f) or "").splitlines() if l.split()]
    keep = [l for l in lines if not (l.split()[0] == name and (pid is None or (l.split() + [""])[1] == str(pid)))]
    if keep == lines:
        return
    if keep:
        _C.write(f, "\n".join(keep) + "\n")
    elif os.path.exists(f):
        os.remove(f)


def run_elsewhere(host, q, cur, gone):
    """状态目录 current 里的任务 cur（read_current）是不是别的主机放行的、这台不该动：是就返回那台主机，否则为 None（#88）。
    同一条清单登记在两台（借来的主机还了再借回、自用主机恢复时旧登记还在），回来那台的 worker 看不到另一个容器里的 PID，
    以前当成服务器重启记中断、删 current、重跑同一个任务。现在：
    - current 写明了放行的主机（11-10 起）：是别的主机、看板又没说它已归还（gone，容器没了）的，一概不动：不接管、不收尾、不放行这条清单，
      等那台的 worker 自己收尾（跑完删掉 current）。那台 worker 不在、状态多久没更新都一样：判断不了就不动，由等待原因与事件告诉人；
    - 旧格式（没写主机）：别的主机（不在 gone）状态里的 running 列着这条清单的这个任务就不动，没列着的照旧当本机留下的。"""
    name, _, _, who = cur
    if who:
        return who if who != host and who not in gone else None
    for f in sorted(glob.glob(os.path.join(_C.HOME, "hosts", "*", "status.json"))):
        h = os.path.basename(os.path.dirname(f))
        if h == host or h in gone:
            continue
        try:
            st = json.loads(_C.read(f, "{}") or "{}")
            if any(j.get("kind") == "queue" and j.get("name") == name and j.get("queue") == q["jobs"]
                   and os.path.normpath(j.get("root") or "") == os.path.normpath(q["root"]) for j in st.get("running") or []):
                return h
        except (ValueError, TypeError, AttributeError):
            continue
    return None


GUARD_RE = re.compile(r"(?:\[\[?|\btest)\s+-[efs]\s+['\"]?([^\s'\"\]]+)")


LOCAL_EVERY = 300        # 多久扫一次待跑任务声明的本地依赖


def scratch_dir(project, name):
    """单格临时目录 $LABMON_SCRATCH：只给这一格用，跑成功后删。"""
    return os.path.join(_C.LOCAL, "jobs", _C.NAME_RE.sub("_", project or "_"), _C.NAME_RE.sub("_", name))


def cache_dir(project):
    """项目的本地缓存目录 $LABMON_CACHE：跨格用、能重算的中间文件；续租、容器重启会清空。"""
    return os.path.join(_C.LOCAL, "cache", _C.NAME_RE.sub("_", project or "_"))


def body(cmd):
    """清单行的命令部分（去掉行尾 # 提示）。"""
    return re.split(r"\s#", cmd, 1)[0]


def expand(s, env):
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), s)


def env_of(q):
    """任务看到的变量（不含单格的 $LABMON_SCRATCH）：标准目录加同名 .env（与启动任务时同样用 bash set -a 读入）。"""
    env = dict(os.environ, QUEUE_ROOT=q["root"], LABMON_CACHE=cache_dir(q["project"]))
    if q["env"]:
        try:
            out = subprocess.run(["bash", "-c", 'set -a; . "$1" >/dev/null 2>&1; set +a; env -0', "_", q["env"]], cwd=q["root"],
                                 env=env, capture_output=True, timeout=20).stdout
            env = dict(x.split("=", 1) for x in out.decode("utf-8", "replace").split("\0") if "=" in x)
        except (OSError, subprocess.SubprocessError):
            pass
    return env


def masters(p, src):
    """有来源的本地依赖（needs=本地<主副本）：[(共享盘主副本, 本地路径)]。来源是目录、或本地路径带通配符时按文件名对应。"""
    if src.endswith("/") or os.path.isdir(src) or glob.has_magic(p):
        d = os.path.dirname(p)
        return [(f, os.path.join(d, os.path.basename(f))) for f in sorted(glob.glob(os.path.join(src, os.path.basename(p)))) if os.path.isfile(f)]
    return [(src, p)]


def check_needs(job, env):
    """清单行尾 needs= 声明的本地依赖：每项展开后的路径、此刻匹配到的文件、缺了怎么补。
    写了来源（needs=$W/w0.pt<$WC/w0.pt，来源可以是目录）的，缺了先从共享盘主副本拷（copy：要拷的 [(主副本, 本地)]），
    主副本还没有时这格跑成功后写回（publish）；没写来源的看命令会不会自己补（先 [ -f 路径 ] || 重新生成）。
    how：copy（从共享盘主副本拷）/ redo（命令自己重算）/ None（补不了）；页面上的文字由看板写。"""
    env = dict(env, LABMON_SCRATCH=scratch_dir(job.get("project"), job["name"]))
    guard = set(GUARD_RE.findall(expand(body(job["cmd"]), env)))
    out = []
    for need in job.get("needs") or []:
        pat, _, src = need.partition("<")
        p = expand(pat, env)
        files = sorted(f for f in glob.glob(p) if os.path.isfile(f))
        ms = masters(p, expand(src, env)) if src else []
        have = [m for m in ms if os.path.isfile(m[0])]
        copy = [m for m in have if not os.path.isfile(m[1])]
        redo = p in guard or bool(files) and all(f in guard for f in files)
        how = "copy" if have and (glob.has_magic(p) or len(have) == len(ms)) else "redo" if redo else None
        out.append({"pat": p, "files": files, "src": expand(src, env) or None, "copy": copy, "how": how, "heal": how is not None,
                    "publish": [(m, f) for m, f in ((os.path.join(expand(src, env), os.path.basename(f)) if len(ms) != 1 or glob.has_magic(p) else ms[0][0], f)
                                                    for f in files) if src and not os.path.isfile(m)]})
    return out


def stage(pairs):
    """把文件拷到位（先写 .part 再改名，拷到一半不会被当成好的）。返回 (个数, 字节)；出错抛 OSError。"""
    n = 0
    for a, b in pairs:
        os.makedirs(os.path.dirname(b) or ".", exist_ok=True)
        shutil.copyfile(a, b + ".part")
        os.replace(b + ".part", b)
        n += os.path.getsize(b)
    return len(pairs), n


def local_deps(queues, data_root, envs):
    """续租会重启容器、清空本地盘：待跑任务（含正在跑的，中断后要重跑）needs= 声明的、此刻在本地盘上的文件。"""
    shared = os.path.realpath(data_root).rstrip("/") + "/"
    rows = []
    for q in queues:
        for line in (_C.read(q["jobs_path"], "") or "").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "|" not in line:
                continue
            name, cmd = (x.strip() for x in line.split("|", 1))
            needs = hints(cmd)["needs"]
            if not needs or any(os.path.exists(os.path.join(q["state"], name + ext)) for ext in (".done", ".failed")):
                continue
            chk = check_needs({"name": name, "cmd": cmd, "needs": needs, "project": q["project"]}, envs(q))
            files = [f for c in chk for f in c["files"] if not os.path.realpath(f).startswith(shared)]
            if files:
                hows = {c["how"] for c in chk}
                rows.append({"project": q["project"], "queue": q["jobs"], "job": name, "files": files,
                             "bytes": sum(os.path.getsize(f) for f in files), "heal": all(c["heal"] for c in chk),
                             "how": None if None in hows else "copy" if hows == {"copy"} else "redo" if hows == {"redo"} else "mixed"})
    return rows


def job_cmd(q, name, rows=None):
    """清单里叫 name 的那一行的命令（按名字找清单行只在这里：接管与旁观的任务、offers 查本地依赖、fit 找清单行）：取第一条、跳过注释行，
    没有为 None。rows 是已经读好的 list_jobs(q)（fit 一次调用里只读一遍），不给就现读。"""
    return next((c.strip() for n, c in (list_jobs(q) if rows is None else rows) if n == name), None)


def pool_refuse(path, stop, gpus):
    """worker add --host pool（交给算力池，#24）不收的清单：返回原因；收为 None。池里的清单会放到借来的服务器上跑，主人回来或收回时要停下重排：
    停法不能是 boundary（等当前任务跑完，收回停不下来），不能限卡（--gpus：各台卡号不同），不能有成组（gang=：借来的随时要还，跨台成组太脆）与占位任务。"""
    if stop == "boundary":
        return "交给算力池的清单不能用 --stop boundary：收回时要停得下来，用 --stop term（缺省）或 --stop file:<相对 ROOT 的路径>"
    if gpus:
        return "交给算力池的清单不能写 --gpus：放到哪台、用哪几张卡由看板定"
    rows = parse_jobs(_C.read(path, "") or "")
    gang = [n for n, c in rows if hints(c).get("gang")]
    hold = [n for n, _ in rows if _C.is_hold(n)]
    if gang:
        return "交给算力池的清单不能有成组的行（gang=）：%s。成组的放到自己的主机上跑" % "、".join(gang[:3])
    if hold:
        return "交给算力池的清单不能有占位任务：%s。要等别的任务就写 after=" % "、".join(hold[:3])
    return None
