"""worker 命令行入口（slot_main / main）。"""
import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys

from . import common as _C
from . import disk as _D
from . import pure as _P
from . import queue as _Q
from . import measure as _M
from . import keepalive as _K
from . import deps as _DP
from . import sched as _S
from . import core as _W
from . import fit as _FT

def slot_main(argv):
    """单元申请：阻塞到 worker 放行，设好 CUDA_VISIBLE_DEVICES 后 exec 原命令（进程号不变，退出码就是原命令的）。"""
    ap = argparse.ArgumentParser(prog="labmon_worker.py slot")
    ap.add_argument("--host", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--name", required=True, help="单元名（同类单元的名字前缀相同，估计值按它找历史）")
    ap.add_argument("--ram", type=float, default=0.0, help="内存 GiB（有历史时按历史峰值估）")
    ap.add_argument("--vram", type=float, default=0.0, help="每张卡的显存 GiB")
    ap.add_argument("--gpus", type=int, default=1)
    ap.add_argument("--tag")
    ap.add_argument("--exclusive", choices=["gpu", "host"])
    ap.add_argument("--cpus", type=float, help="要几个核（CPU 记账优先按它，没写按同类历史、再没有按 1 核；放行后导出成 LABMON_CPUS）")
    ap.add_argument("--io", choices=["heavy", "light"], help="读写重不重（只记录，准入不看）")
    ap.add_argument("--est-min", type=float, help="估时（分钟）：没有历史时续租截止、约定归还前按它判断跑不跑得完")
    ap.add_argument("--stop", default="term", help="暂停时怎么停：term（SIGTERM，缺省）/ boundary / file:<路径>")
    ap.add_argument("--log", help="单元日志路径（工作台展开时看末尾）")
    ap.add_argument("--timeout", type=float, default=0, help="最多等多少秒（0 为一直等）；超时退出码 75")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("缺少要运行的命令：slot … -- <命令>")
    os.makedirs(_C.SLOTS, exist_ok=True)
    rid = "%d-%d" % (os.getpid(), int(_C.time.time()))
    base = os.path.join(_C.SLOTS, rid)

    def bye(*_):
        for ext in (".req", ".why", ".ok"):
            if os.path.exists(base + ext):
                os.remove(base + ext)
        sys.exit(143)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    t0 = _C.time.time()
    _C.write(base + ".req", json.dumps({"pid": os.getpid(), "project": a.project, "name": a.name, "ram": a.ram, "vram": a.vram,
                                     "gpus": a.gpus, "tag": a.tag, "excl": a.exclusive, "stop": a.stop, "log": a.log, "est_min": a.est_min,
                                     "cpus": a.cpus, "io": a.io,
                                     "cmd": " ".join(cmd), "t": t0}, ensure_ascii=False))
    wpid, last = _C.Paths(a.host).pid, t0 - 58
    while True:
        ok = _C.read(base + ".ok")
        if ok:
            ok = json.loads(ok)
            _M.accel_env(os.environ, ok.get("gpus", []), accel=ok.get("accel"))
            _M.cpu_env(os.environ, ok.get("cpus") or max(1, _M.job_cpus(_C.CPU_DEFAULT if a.cpus is None else a.cpus)))   # 旧 worker 的 .ok 不带 cpus：按 --cpus 或缺省
            if ok.get("adj"):                     # 借来的主机：撞容器内存上限时内核先杀我们的
                try:
                    with open("/proc/self/oom_score_adj", "w") as f:
                        f.write(str(ok["adj"]))
                except OSError:
                    pass
            if ok.get("nice"):                    # GPU 主机上的纯 CPU 单元：降优先级，保障 GPU 进程时间片（#225）
                try:
                    os.nice(int(ok["nice"]))
                except (AttributeError, OSError, ValueError):
                    pass
            os.environ["LABMON_SLOT"] = rid
            os.environ["LABMON_SCRATCH"], os.environ["LABMON_CACHE"] = _Q.scratch_dir(a.project, a.name), _Q.cache_dir(a.project)
            for d in (os.environ["LABMON_SCRATCH"], os.environ["LABMON_CACHE"]):
                os.makedirs(d, exist_ok=True)
            sys.stdout.flush()
            sys.stderr.flush()
            os.execvp(cmd[0], cmd)
        if _C.time.time() - last >= 60:
            up = _C.alive(_C.read_pid(wpid), ("labmon_worker",))
            print("[labmon slot] %s 等待 %d 秒：%s" % (a.name, _C.time.time() - t0,
                  (_C.read(base + ".why") or "排队中") if up else "worker 不在（labmon worker start %s）" % a.host), file=sys.stderr)
            last = _C.time.time()
        if a.timeout and _C.time.time() - t0 > a.timeout:
            for ext in (".req", ".why"):
                if os.path.exists(base + ext):
                    os.remove(base + ext)
            print("[labmon slot] %s 等了 %d 秒仍未放行，退出" % (a.name, a.timeout), file=sys.stderr)
            return 75
        _C.time.sleep(1)


def _fmt_bytes(b):
    if b >= _C.GIB:
        return "%.2f GiB" % (b / _C.GIB)
    if b >= _C.MIB:
        return "%.1f MiB" % (b / _C.MIB)
    return "%d B" % b


def _job_alive(pid):
    """检查任务自身进程或进程组是否仍在运行（与 worker 是否退出无关，#114）：
    Worker.run() 退出时故意保留运行中任务，因此仅凭 worker PID 不在绝不能认定任务已结束。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    for fn in (os.kill, os.killpg):
        try:
            fn(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            continue
    return False


def _running_scratches(p):
    """实时读取运行中的单格临时集合 {(proj_seg, job_seg)}（合并 status.json 与 active_scratch.json）：
    - status.json 损毁时返回 None（调用方跳过 jobs/ 清理，绝不误删）；
    - worker 活着时信任 status.running + external；worker 退出时按各任务自身进程/进程组是否存活判定；
    - 同步合并 active_scratch.json 中仍存活的进程，覆盖 Worker.start() 刚起进程尚未 write_status 的窗口。"""
    running = set()
    raw_st = _C.read(p.status, "")
    if raw_st:
        try:
            st = json.loads(raw_st)
        except ValueError:
            return None
        w_alive = _C.alive(_C.read_pid(p.pid), ("labmon_worker",))
        for j in (st.get("running") or []) + (st.get("external") or []):
            if not (isinstance(j, dict) and j.get("project") and j.get("name")):
                continue
            jpid = j.get("pid")
            if w_alive or jpid is None or _job_alive(jpid):
                running.add((_C.NAME_RE.sub("_", j["project"]), _C.NAME_RE.sub("_", j["name"])))
    raw_act = _C.read(p.active_scratch, "")
    if raw_act:
        try:
            act = json.loads(raw_act)
        except ValueError:
            return None
        if isinstance(act, dict):
            for k, jpid in act.items():
                if "/" in str(k) and _job_alive(jpid):
                    a, b = str(k).split("/", 1)
                    running.add((a, b))
    return running


def clean_local(p, project=None, cache=False, scratch=False, yes=False):
    """清理本机自管临时目录（<LABMON_LOCAL>/{cache,jobs}）：
    - 绝不触碰 projects/、datasets/、weights/、envs/ 或任何 <LABMON_LOCAL> 之外的路径（经 realpath 严格校验且不跟随符号链接）；
    - 默认仅预览（dry-run），显式传 yes=True 才执行删除；
    - 清理 jobs/ 单格临时目录时与 Worker.start/grant 共用 scratch_lock：扫描在锁外执行，删除每个单格目录前在锁内重新核实任务存活并原子移出后锁外删除，彻底避免扫描期间新起任务被误删。"""
    do_cache = cache or (not cache and not scratch)
    do_scratch = scratch or (not cache and not scratch)
    base = os.path.realpath(_C.LOCAL)
    running = set()
    if do_scratch:
        got = _running_scratches(p)
        if got is None:
            print("状态文件 %s 无法解析，为避免误删运行中任务的临时目录已跳过 jobs/ 清理" % p.status)
            do_scratch = False
        else:
            running = got
    targets = []
    skipped = []

    def _safe_child(path):
        if os.path.islink(path):
            return None
        rp = os.path.realpath(path)
        try:
            if rp == base or os.path.commonpath((base, rp)) != base:
                return None
        except ValueError:
            return None
        return rp

    proj_seg = _C.NAME_RE.sub("_", project) if project else None
    if do_cache:
        cdir = os.path.join(base, "cache")
        if os.path.isdir(cdir) and not os.path.islink(cdir):
            names = [proj_seg] if proj_seg else sorted(os.listdir(cdir))
            for nm in names:
                tpath = os.path.join(cdir, nm)
                rp = _safe_child(tpath)
                if rp and os.path.exists(rp):
                    sz = _D.scan_unit(rp, _C.time.time(), _D.Pace(_D.LOCAL_IOPS))["b"] if os.path.isdir(rp) else os.lstat(rp).st_size
                    targets.append(("cache", nm, rp, sz, None))
    if do_scratch:
        jdir = os.path.join(base, "jobs")
        if os.path.isdir(jdir) and not os.path.islink(jdir):
            pnames = [proj_seg] if proj_seg else sorted(os.listdir(jdir))
            for pnm in pnames:
                pdir = os.path.join(jdir, pnm)
                prp = _safe_child(pdir)
                if not (prp and os.path.isdir(prp)):
                    continue
                for jnm in sorted(os.listdir(prp)):
                    if jnm.startswith(".labmon_del_"):
                        continue
                    if (pnm, jnm) in running:
                        skipped.append(os.path.join(prp, jnm))
                        continue
                    jpath = os.path.join(prp, jnm)
                    jrp = _safe_child(jpath)
                    if jrp and os.path.exists(jrp):
                        sz = _D.scan_unit(jrp, _C.time.time(), _D.Pace(_D.LOCAL_IOPS))["b"] if os.path.isdir(jrp) else os.lstat(jrp).st_size
                        targets.append(("scratch", "%s/%s" % (pnm, jnm), jrp, sz, (pnm, jnm)))
    if not targets:
        print("没有可清理的自管缓存或已结束单格临时目录（目录：%s）" % base)
        if skipped:
            print("  已跳过 %d 个在跑任务的单格临时目录" % len(skipped))
        return 0
    tot = sum(x[3] for x in targets)
    kind_zh = {"cache": "缓存", "scratch": "单格临时"}
    if not yes:
        print("可清理 %d 项，共 %s（目录：%s；预览模式未删除，加 --yes 确认清理；绝不触碰 projects/ 产物）：" % (len(targets), _fmt_bytes(tot), base))
        for kind, label, rp, sz, _ in targets:
            print("  [%s] %s  %s  (%s)" % (kind_zh[kind], label, _fmt_bytes(sz), rp))
        if skipped:
            print("  已跳过 %d 个在跑任务的单格临时目录" % len(skipped))
        return 0
    freed, n = 0, 0
    for idx, (kind, label, rp, sz, skey) in enumerate(targets):
        try:
            del_path = rp
            if kind == "scratch" and skey is not None:
                # 在 scratch_lock 锁内重新核实任务状态，防扫描到删除期间 Worker.start/grant 新启任务被误删
                with _C.flock(p.scratch_lock):
                    cur_run = _running_scratches(p)
                    if cur_run is None or skey in cur_run:
                        skipped.append(rp)
                        continue
                    if not os.path.exists(rp):
                        continue
                    staged = os.path.join(os.path.dirname(rp), ".labmon_del_%d_%d" % (os.getpid(), idx))
                    os.rename(rp, staged)
                    del_path = staged
            if os.path.isdir(del_path) and not os.path.islink(del_path):
                shutil.rmtree(del_path)
            else:
                os.unlink(del_path)
            freed += sz
            n += 1
            print("  已清理 [%s] %s（%s）" % (kind_zh[kind], label, _fmt_bytes(sz)))
        except OSError as e:
            print("  清理失败 %s：%s" % (rp, e), file=sys.stderr)
    os.makedirs(p.dir, exist_ok=True)
    with _C.flock(p.events):
        _C.append_jsonl(p.events, {"t": round(_C.time.time()), "kind": "worker",
                                   "msg": "清理自管缓存/临时目录：%d 项，释放 %s%s" % (n, _fmt_bytes(freed), "（项目 %s）" % project if project else ""),
                                   **({"project": project} if project else {})})
    print("已清理 %d 项，共释放 %s（未触碰 projects/ 产物）" % (n, _fmt_bytes(freed)))
    if skipped:
        print("  已跳过 %d 个在跑任务的单格临时目录" % len(skipped))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["slot"]:
        return slot_main(argv[1:])
    ap = argparse.ArgumentParser(prog="labmon_worker.py")
    ap.add_argument("cmd", choices=["run", "add", "rm", "ls", "stop", "limit", "override", "fit", "thaw", "clean"])
    ap.add_argument("args", nargs="*", help="add / rm：ROOT JOBS；limit：tag=N…；override：add|rm KIND [目标] 或 ls；fit：任务名…（--list 时 ROOT JOBS）")
    ap.add_argument("--host", required=True)
    ap.add_argument("--idle", action="store_true", help="add：仅在清单空闲时更换资源")
    ap.add_argument("--gpus", help="add：只用这些卡（nvidia-smi 序号，逗号分隔）")
    ap.add_argument("--project", help="add：看板的项目 id（必写）；fit：按这个项目的历史估（缺省取同名任务最近一次的项目）；clean：仅清理该项目")
    ap.add_argument("--expect-project", help="add / rm：在登记锁内核对这条清单所属的项目")
    ap.add_argument("--pool", nargs="?", const=_P.POOL, default=None, help="add：算力池放过来的（登记里记池 id，用于区分池放的清单）")
    ap.add_argument("--expect-from", help="add：在登记锁内核对这条清单此刻还登记在这台（算力池搬清单用，#25：会话已撤回、已搬走的不再重新登记）")
    ap.add_argument("--stop", help="add：暂停时怎么停：boundary（缺省，等当前任务跑完）/ term / file:<相对 ROOT 的路径>")
    ap.add_argument("--until", type=float, help="override add：到期时刻（epoch 秒）")
    ap.add_argument("--now", action="store_true", help="override add pause：立即按停法停下正在跑的")
    ap.add_argument("--once", action="store_true", help="override add pause --now：停完就删掉这条（暂停由看板写来的全局那条挡，不在这台另留）")
    ap.add_argument("--at", type=float, help="override add deadline：截止时刻（epoch 秒），之前跑不完的任务不放行（目标 borrow 为约定归还）")
    ap.add_argument("--by", help="override add：谁下的（看板的账号名，记进这条的 by；看板据它判断谁能撤，#27）")
    ap.add_argument("--expect-by", help="override add / rm：在锁里核对这台已有的同种同目标指令（不算 once）是这个账号下的，或没有；"
                                        "是别人下的或没记是谁的就不改（看板按 users.need_pause 定了才传，#27）")
    ap.add_argument("--keep", type=json.loads, help='override add guest：给对方留的量，JSON {"vram": 每张对方的卡 GiB, "ram": GiB, "cpu": 核, "hold": 预先留出, "away": 对方说好不用到几点（epoch 秒，那以前不留）}')
    ap.add_argument("--list", action="store_true", help="fit：位置参数是 ROOT JOBS，看这条清单还会跑的行能在几台上再开几条车道")
    ap.add_argument("--hosts", help="fit --list：看哪几台（逗号分隔；看板按共用 worker 目录的在用主机给，缺省只看这台）")
    ap.add_argument("--json", action="store_true", help="fit --list：只看队首，打一行 JSON（算力池往哪台放用，#25）")
    ap.add_argument("--replace", action="store_true", help="run：已有 worker 时让它退出后接管（升级用）")
    ap.add_argument("--daemon", action="store_true", help="run：以独立会话在后台启动 worker 后立即返回（不依赖 Linux setsid，macOS/Linux 通用）")
    ap.add_argument("--shared", help="run：组里共用的那块盘的挂载点（如 /root/data1），给了就参与统计它的用量")
    ap.add_argument("--standalone", action="store_true", help="run：以独立主机模式运行（不共享 worker_home，扫 <data_root> 工作盘）")
    ap.add_argument("--cache", action="store_true", help="clean：清理 <LOCAL>/cache")
    ap.add_argument("--scratch", action="store_true", help="clean：清理 <LOCAL>/jobs（已结束任务）")
    ap.add_argument("--dry-run", action="store_true", help="clean：仅预览可清理目录与大小（缺省行为）")
    ap.add_argument("--yes", action="store_true", help="clean：确认执行清理（否则仅预览）")
    a = ap.parse_args(argv)
    p = _C.Paths(a.host)
    if a.cmd == "run":
        if a.daemon:
            child_argv = [x for x in argv if x != "--daemon"]
            entry = ([sys.executable, os.path.abspath(sys.argv[0])]
                     if sys.argv and os.path.isfile(sys.argv[0]) else [sys.executable, "-m", "labmon.worker"])
            subprocess.Popen(entry + child_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
            return 0
        return _W.Worker(a.host, a.shared, standalone=True if a.standalone else None).run(a.replace)
    if a.cmd == "clean":
        return clean_local(p, project=a.project, cache=a.cache, scratch=a.scratch, yes=a.yes and not a.dry_run)
    if a.cmd == "fit":
        if a.list:
            if len(a.args) != 2:
                ap.error("fit --list 需要 ROOT JOBS")
            return _FT.fit_list(p, a.args[0], a.args[1], a.project, [h for h in (a.hosts or "").split(",") if h], a.json)
        if not a.args:
            ap.error("fit 需要任务名")
        return _FT.fit(p, a.args, a.project)
    if a.cmd in ("add", "rm"):
        if len(a.args) != 2:
            ap.error("add / rm 需要 ROOT JOBS")
        root = os.path.abspath(a.args[0])
        if a.cmd == "add":
            try:
                _C.queue_stop_path(a.stop, root)
                if os.environ.get("LABMON_STANDALONE") == "1" and os.environ.get("LABMON_ALLOW_OUTSIDE_ROOT") != "1":
                    _C.queue_root_ok(root, os.environ.get("LABMON_DATA_ROOT") or _C.DATA_ROOT)
            except (OSError, ValueError) as e:
                print(str(e), file=sys.stderr)
                return 2
        # 核对项目、查状态目录、登记与搬走，整段经同一把登记锁。
        with _Q.reg_lock(required=bool(a.expect_project or a.expect_from)):
            dirs = _Q.reg_dirs()
            old_regs = _Q.matching_regs(p.queues, root, a.args[1], by_file=a.cmd == "rm")
            if a.cmd == "add" and a.expect_from and not (dirs.get(a.expect_from) and _Q.reg_files(dirs[a.expect_from], root, a.args[1])):
                print("这条清单已不在 %s 上（撤回或搬走了）：不搬" % a.expect_from, file=sys.stderr)
                return 2
            if a.expect_project:
                matches = list(old_regs)
                if a.cmd == "add":
                    for d in dirs.values():
                        matches.extend(_Q.matching_regs(d, root, a.args[1], aliases=True))
                if any(r.get("project") != a.expect_project for _, r in matches) or (a.cmd == "add" and a.project != a.expect_project):
                    print("登记归属已变：请刷新后重试，或找管理员", file=sys.stderr)
                    return 2
            old = [f for f, _ in old_regs]
            if a.cmd == "rm":
                if a.expect_project and not old:
                    print("这条登记已不存在：请刷新后重试", file=sys.stderr)
                    return 2
                for f, reg in old_regs:
                    os.remove(f)
                    print("已移除 %s %s" % (reg.get("root"), reg.get("jobs")))
                if not old:
                    print("没登记过 %s/%s" % (root, a.args[1]))
                return 0
            if not a.project:
                print("add 必须写 --project <看板项目 id>（monitor.toml 的 id）", file=sys.stderr)
                return 2
            if not os.path.exists(os.path.join(root, a.args[1])):
                print("清单不存在：%s/%s" % (root, a.args[1]), file=sys.stderr)
                return 2
            if _P.is_pool_id(a.host):
                bad = _Q.pool_refuse(os.path.join(root, a.args[1]), a.stop, a.gpus)
                if bad:
                    print(bad, file=sys.stderr)
                    return 2
            guest = next((o for o in json.loads(_C.read(p.overrides, "[]") or "[]") if o.get("kind") == "guest" and o.get("target")), None)
            if guest and a.project not in guest["target"].split(","):
                print("主机 %s 是借来的，只给 %s 用（这条队列的项目是 %s）" % (a.host, "、".join(guest["target"].split(",")), a.project), file=sys.stderr)
                return 2
            os.makedirs(p.queues, exist_ok=True)
            f = old[0] if old else os.path.join(p.queues, _Q.qslug(root, a.args[1]) + ".json")
            if a.expect_project and not old and os.path.lexists(f):
                print("登记文件名已被占用：请找管理员", file=sys.stderr)
                return 2
            reg = {"root": root, "jobs": a.args[1], "added": _C.now_str(), "project": a.project}
            if a.gpus:
                reg["gpus"] = [int(x) for x in a.gpus.split(",")]
            if a.stop:
                reg["stop"] = a.stop
            if a.pool and not _P.is_pool_id(a.host):
                reg["pool"] = a.pool
            # 两条队列共用状态目录：current、lock、完成标记会互相覆盖。各主机共用同一块盘，别的主机上登记的也算
            st = _Q.load_queue(dict(reg, _slug="new"))["state"]
            if a.idle and os.path.exists(os.path.join(st, "current")):
                print("清单正在运行或结果待核对，不能更换资源", file=sys.stderr)
                return 2
            conflict = _Q.queue_state_conflict(_Q.load_queue(dict(reg, _slug="new")), a.host, dirs)
            if conflict:
                print(conflict, file=sys.stderr)
                return 2
            for x in old[1:]:
                os.remove(x)
            _C.write(f, json.dumps(reg, ensure_ascii=False))
            # 同一条清单只登记在一台（#95）：别的主机（含已归还的、主机清单里已没有的，那边的 worker 删不了）上的登记搬过来，否则两台轮流放这条清单。
            # 那台手上正在跑的照样跑完，这台等它跑完再接着放（requests 里 run_elsewhere 挡着）。
            # 只删比这次登记旧的（共享盘修改时间，一样新按主机 id，同 Worker.registry 的认法）：两个会话同时登记到两台时只留晚的那份，不会互相删光
            mine, moving, newer = (os.stat(f).st_mtime, a.host), {}, []
            for h, d in dirs.items():
                for x in (_Q.reg_files(d, root, a.args[1]) if h != a.host else []):
                    try:
                        later = (os.stat(x).st_mtime, h) > mine
                    except OSError:
                        continue
                    if later:
                        newer.append(h)
                    else:
                        moving.setdefault(h, []).append(x)
            if newer:
                os.remove(f)
                print("主机 %s 上刚登记了这条清单（比这次晚），以那边为准，没有登记到 %s" % ("、".join(newer), a.host), file=sys.stderr)
                return 2
            moved = sorted(moving)
            try:
                gone = set(json.loads(_C.read(p.returned, "{}") or "{}").get("hosts") or [])
            except (ValueError, AttributeError, TypeError):
                gone = set()
            away = [(c[0], h) for c in _Q.read_current(st) for h in [_Q.run_elsewhere(a.host, {"state": st, "root": root, "jobs": a.args[1]}, c, gone)] if h]
            busy = away[0][1] if away else None
            for h in moved:
                for x in moving[h]:
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(x)
                ev = os.path.join(os.path.dirname(dirs[h]), "events.jsonl")
                with _C.flock(ev):
                    _C.append_jsonl(ev, {"t": round(_C.time.time()), "kind": "worker", "msg": "登记搬到主机 %s：%s/%s，这台不再跑它" % (a.host, root, a.args[1]),
                                      "project": a.project, "root": root, "queue": a.args[1]})
            if moved:
                os.makedirs(p.dir, exist_ok=True)
                with _C.flock(p.events):
                    _C.append_jsonl(p.events, {"t": round(_C.time.time()), "kind": "worker", "msg": "登记从主机 %s 搬来：%s/%s" % ("、".join(moved), root, a.args[1]),
                                            "project": a.project, "root": root, "queue": a.args[1]})
        print("已登记 %s/%s → 主机 %s" % (root, a.args[1], a.host)
              + ("（从主机 %s 搬过来，那边不再跑它%s）" % ("、".join(moved), "；主机 %s 上正在跑的 %s 跑完后这台接着跑" % (busy, "、".join(n for n, _ in away)) if busy else "") if moved else ""))
        return 0
    os.makedirs(p.dir, exist_ok=True)
    if a.cmd == "thaw":                           # 还卡时（borrow.finish）恢复暂停的看门狗
        print(_K.thaw(p) or "没有暂停的看门狗")
        return 0
    if a.cmd == "limit":
        lim = json.loads(_C.read(p.limits, "{}") or "{}")
        tags = lim.setdefault("tags", {})
        for kv_ in a.args:
            k, _, v = kv_.partition("=")
            if v.strip():
                tags[k] = int(v)
            else:
                tags.pop(k, None)
        if a.args:
            _C.write(p.limits, json.dumps(lim, ensure_ascii=False))
        print("tag 限额：" + ("，".join("%s=%d" % kv_ for kv_ in sorted(tags.items())) or "无"))
        return 0
    if a.cmd == "override":
        if a.args[:1] in (["add"], ["rm"]):
            if len(a.args) < 2 or a.args[1] not in ("pause", "focus", "drain", "deadline", "guest"):
                ap.error("override add|rm pause|focus|drain|deadline|guest [目标]")
            kind, target = a.args[1], (a.args[2] if len(a.args) > 2 else None)
            with _C.flock(p.overrides):              # worker 同时在改（到期清理、pause --now 记 applied）
                cur = json.loads(_C.read(p.overrides, "[]") or "[]")
                # 看板按快照核过谁能动（users.need_pause）；快照可能晚一轮，这里在锁里再核一次现状（同 --expect-project）
                if a.expect_by is not None and not a.once and any(
                        o["kind"] == kind and o.get("target") == target and not o.get("once")
                        and (o.get("by") or "").lower() != a.expect_by.lower() for o in cur):
                    print("这台上 %s 的%s不是你下的（管理员下的，或没记是谁）：刷新后再试，或找管理员"
                          % (target or "", {"pause": "暂停", "focus": "优先"}.get(kind, "指令")), file=sys.stderr)
                    return 2
                # 同种同目标的换成新的；“只停一次”（once）与这台单独下的各管各的：全局的 pause --now 不替掉这台已有的暂停（Codex 审查）
                ovr = [o for o in cur
                       if not (o["kind"] == kind and o.get("target") == target and (a.args[0] == "rm" or bool(o.get("once")) == a.once))]
                if a.args[0] == "add":
                    ovr.append({"kind": kind, "target": target, "until": a.until, "now": a.now, "t": _C.time.time(),
                                **({"at": a.at} if a.at else {}), **({"keep": a.keep} if a.keep else {}), **({"once": True} if a.once else {}),
                                **({"by": a.by} if a.by else {})})
                _C.write(p.overrides, json.dumps(ovr, ensure_ascii=False))
            if a.once:                            # 只停一次：暂停的记录由看板记（全局的那条），停下的任务 worker 各记一条 stop
                print("下一轮按停法停下 %s 正在跑的" % target)
                return 0
            if a.args[0] == "add":
                msg, kw = "临时指令：" + _C.ovr_text(ovr[-1]), _C.ovr_fields(ovr[-1], "add")
            else:
                msg, kw = "撤销临时指令：%s %s" % (kind, target or ""), _C.ovr_fields({"kind": kind, "target": target}, "rm")
            with _C.flock(p.events):
                _C.append_jsonl(p.events, dict(kw, t=round(_C.time.time()), kind="override", msg=msg))
        else:
            ovr = json.loads(_C.read(p.overrides, "[]") or "[]")
        ovr = [o for o in ovr if not o.get("once")] + _C.sched_ovr(p.sched)
        print("生效中：" + ("；".join(_C.ovr_text(o) for o in ovr) or "无"))
        return 0
    pid = _C.read_pid(p.pid)
    up = _C.alive(pid, ("labmon_worker",))
    if a.cmd == "stop":
        _C.write(p.stopped, "manual:" + _C.now_str())   # 手动停的标记（#236）：本机看板见到它就不自动拉起
        if up:
            os.kill(pid, signal.SIGTERM)
            print("已通知 worker（PID %d）退出；运行中的任务继续跑" % pid)
        else:
            print("worker 没在运行")
        return 0
    st = json.loads(_C.read(p.status, "{}") or "{}")
    print("worker：" + ("运行中 PID %d" % pid if up else "未运行") + ("（状态 %s）" % _C.now_str(st["t"]) if st.get("t") else ""))
    if (st.get("gate") or {}).get("on"):
        print("  内存闸门关闭中：停止放行")
    if (st.get("disk_gate") or {}).get("on"):
        print("  " + (st["disk_gate"].get("why") or "磁盘闸门关闭中：暂停放新任务"))
    if (st.get("info") or {}).get("wsl") or _M.is_wsl():
        mnt_w = _P.wsl_mnt_warn(_C.DATA_ROOT)
        if mnt_w:
            print("  警告：" + mnt_w)
    for ln in _M.unacct_lines(st):
        print("  " + ln)
    for o in st.get("overrides", []):
        print("  临时指令 " + _C.ovr_text(o))
    wd = _K.load_frozen(p)                           # 直接读 frozen.json：刚拉起、状态还没写时也看得到
    if wd["frozen"]:
        print("  暂停着的保活看门狗（还卡时恢复）：" + _K.wd_text(wd["frozen"]))
    if wd["seen"]:
        print("  找到的保活看门狗（没动）：" + _K.wd_text(wd["seen"]))
    for ln in _K.ka_lines(st):
        print("  " + ln)
    if _S.cpu_line(st):
        print("  " + _S.cpu_line(st))
    th = st.get("throttle")
    if th:
        print("  CPU 被配额限流 %s%%（%s 起，%d 分钟%s）%s" % (th.get("thr"), _C.now_str(th["since"])[5:16], th["for"] // 60, "，已告警" if th["alert"] else "",
                                                    "；超核：" + "、".join("%s/%s %.1f 核（给 %d）" % (o["project"], o["name"], o["cores"], o["give"]) for o in th["over"]) if th["over"] else ""))
    if (st.get("limits") or {}).get("tags"):
        print("  tag 限额 " + "，".join("%s=%d" % kv_ for kv_ in sorted(st["limits"]["tags"].items())))
    qs = st.get("queues", [])
    awake, asleep = [q for q in qs if not q.get("sleeping")], [q for q in qs if q.get("sleeping")]
    days = _P.IDLE_REG_S // 86400
    for q in awake + (asleep if len(asleep) <= 3 else []):
        print("  队列 %s %s/%s%s%s" % (q.get("project"), q["root"], q["jobs"], "（卡 %s）" % q["gpus"] if q.get("gpus") else "",
                                     "（已休眠：空满 %d 天，清单变动自动唤醒）" % days if q.get("sleeping")
                                     else "（空着：清单里没有待办，不占卡）" if q.get("empty") else ""))
    if len(asleep) > 3:
        by_p = {}
        for q in asleep:
            by_p[q.get("project") or "?"] = by_p.get(q.get("project") or "?", 0) + 1
        print("  另有 %d 条已休眠的登记（空满 %d 天，清单变动自动唤醒：%s）" % (
            len(asleep), days, "、".join("%s %d 条" % kv for kv in sorted(by_p.items()))))
    for j in st.get("running", []):
        gang = "，成组 %s（与 %s 一起开跑）" % (j["gang"], "、".join("%s（%s）" % (w[1], w[0]) for w in j.get("gang_with") or []) or "—") if j.get("gang") else ""
        cores = (j.get("acct") or {}).get("cpu")
        print("  运行 %s：%s（PID %d，%s 起，GPU %s%s%s）%s" % (j.get("queue") or "单元 " + str(j.get("project")), j["name"], j["pid"], _C.now_str(j["t0"]),
                                                         ",".join(j["gpus"]) or "无", "，CPU 记 %g 核" % cores if cores else "", gang, _S.shrunk_text(j.get("est"))))
    for w in st.get("waiting", []):               # 排位与预计开始取状态里的 pos、start，与看板的等待表一致；被临时指令挡住的没有排位
        pos, start = w.get("pos"), w.get("start")
        rank = "（排第 %d%s，预计开始 %s）" % (pos["n"], "，屏障" if pos.get("barrier") else "",
                                          ("马上" if start <= st.get("t", 0) + 60 else _C.now_str(start)[5:16]) if start else "—") if pos else ""
        print("  等待 %s%s：%s%s%s" % (w.get("queue") or "单元 " + str(w.get("project")), " 的 " + w["name"] if w["name"] else "", w["why"], rank,
                                     _S.shrunk_text(w.get("est"))))
    for ln in (st.get("plan") or {}).get("lanes", []):     # 推演里还没开始、写了依赖的：等谁、预计何时开始
        for j in ln.get("jobs", []):
            if j.get("deps") and not j.get("run"):
                ok, done = ([tuple(d[:2]) + (0,) for d in j["deps"] if (len(d) > 2) == a] for a in (False, True))
                what = "、".join(x % _DP.dep_names(k, ln.get("project")) for x, k in (("%s 跑成功", ok), ("%s 跑完", done)) if k)
                print("  依赖 %s 的 %s：等 %s，预计 %s 开始" % (ln.get("queue") or "单元", j["name"], what, _C.now_str(j["t0"])[5:16]))
    return 0
