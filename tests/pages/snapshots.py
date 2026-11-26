"""页面测试用的快照：按真实快照的字段形状构造（overview.json、项目的 snapshot.json 与 events.json），内容全是假名。

三份工作台快照、三份项目快照：
    normal  覆盖面够的一份：自用主机 + 借来的主机、两个项目、运行中 / 等待（排位、预计开始、屏障、被挡住、推演里排不上）/ 跑完的任务、
            推演 plan、调度 sched、组内服务器几台（在跑、只有保活、掉线、关机、空闲）、共享盘、告警、临时指令
    evil    normal 里的项目名、任务名、主机名、人名、日志行、告警、里程碑等都带上 BAD（标签、&、引号、<!--、<script>）
    sparse  字段大量缺省：worker 没部署的、worker 停着（没有 plan / share，列表都空）的、本机不在内网的；组内服务器与共享盘读不了（error 兜底）；
            项目页上探针失败的主机、已归还的主机、缺清单的车道，没有任务、没有计划项
另有一对高并行短任务的（project_dense、overview_dense，在 EXTRA 里，离线预览 --kind dense）：时间线近景定宽用（Issue #49）；
一份大量小规模并行的项目快照（project_many，在 EXTRA 里，离线预览 --kind many）：计划项与运行中的紧凑视图用（Issue #69）。
普通用户看到的工作台快照（overview_user，在 EXTRA 里，离线预览 --as user）：normal 经 users.view 只留可见的主机与项目（#167）。
时刻字段按约定：running[].t0、done[].t0 / t1、项目快照的 *_ts、事件的 started 都是 epoch 秒；generated 取现在，tsNear 的“今天”才成立。
"""
from __future__ import annotations

import copy
import time

from labmon.config import STALE_PAD_S, STALE_RUNS, cfg_spec, stale_limit
from labmon.fleet import local_writes
from labmon import hostcheck as HC
from labmon.overview import EVENT_CATS, EVENT_KINDS, gpu_busy
from labmon.users import Access, Ident, view

BAD = "<i id=x>&\"'<!--<script>alert(1)</script>"
H = 3600.0
NOW = float(int(time.time()))        # 整秒：页面上 ts() 与 node 里算的期望值一致
# 借用的约定还的时刻：一周后 18:37（精确到分，Issue #9）；晚于对方租期截止（NOW + 6 天），截止仍取租期
UNTIL = time.mktime(time.localtime(NOW + 7 * 86400)[:3] + (18, 37, 0, 0, 0, -1))


def _series(n: int, lim: float, gpus: int) -> list[dict]:
    # 前一半是旧行（看板 11-08 前存的，没有 thr、nfs、busy；11-17 前的卡没有第 4 项吃满程度，#51），后一半带主机压力与吃满程度
    return [{"t": NOW - (n - k) * 600, "mem": 2.0 + (k % 7) * 0.5, "anon": 1.0 + (k % 7) * 0.3, "lim": lim, "cpu": 1.0 + (k % 3),
             **({"thr": float(k % 4), "nfs": 0.5 + (k % 6) * 6, "busy": float(k % 10) * 3} if k >= n // 2 else {}),
             "gpu": [[20000 + 100 * (k % 9), 46068, 90.0 - (k % 5)] + ([0.0 if g else 70.0 + k % 9] if k >= n // 2 else []) for g in range(gpus)]} for k in range(n)]


def _dur(name: str, mins: float, cls: str) -> dict:
    return {"min": mins, "src": "同类近 3 次 实际/估时 ×0.90", "k": 0.9, "cls": cls}


def _done(name: str, project: str, rc: str, t0: float, t1: float, root: str, queue: str, **kw) -> dict:
    return {"min": round((t1 - t0) / 60, 1), "kind": "queue", "root": root, "queue": queue, "project": project, "name": name, "tag": None,
            "rc": rc, "gpus": ["GPU-aaaa-0"], "est": {"ram": 1.3, "vram": 10.2, "src": "同队列 近 1 次峰值 ×1.1"},
            "excl": None, "ram_peak": 1.1, "vram_peak": 11374.0, "cores_mean": 1.0, "with_gpu": [], "with_host": [], "link": project,
            "cores_peak": 1.4, "rd_peak": 210.5, "wr_peak": 12.0, "rd_b": 48 * 2 ** 30, "wr_b": 3 * 2 ** 30, "cpus": None, "io": None,
            "t0": t0, "t1": t1, **kw}


def _host_a() -> dict:
    """自用主机 srv-a：两张卡、两条队列、运行中两个（一个有进度，一个占位）、等待五条（各种排位情形）。"""
    root_a, q_a = "/data/me/proj-a/q1", "jobs/q1.txt"
    root_b, q_b = "/data/me/proj-b/q", "jobs/b.txt"
    use = {"t": NOW - 4, "ticks": 2400, "nproc": 3, "ram": 1.29, "ram_peak": 2.1, "vram": 20500.0, "vram_peak": 20500.0,
           "vram_by": {"GPU-aaaa-0": 20500.0}, "with_host": [], "with_gpu": [], "cores": 1.0, "cpu_s": 2419.4, "wall_s": 2416.3,
           "util_s": 30386.4, "util_wall": 2416.3, "util_mean": 13, "rchar": 51539607552, "wchar": 2147483648, "rd": 35.2, "wr": 0.4,
           "rd_b": 51539607552, "wr_b": 2147483648, "rd_peak": 180.3, "wr_peak": 9.8, "cores_peak": 2.6, "pw": [NOW - 30, 2390.0, 51000000000, 2100000000]}
    est = {"ram": 5.8, "vram": 20.9, "util": 59, "src": "同 tag 近 5 次峰值 ×1.1", "ram_src": "同 tag 近 5 次峰值 ×1.1", "cpus": 4, "cpu_src": "提示值"}
    running = [
        {"kind": "queue", "queue": q_a, "root": root_a, "project": "proj-a", "name": "alpha_train_s1", "pid": 2001, "gpus": ["GPU-aaaa-0"],
         "tag": "record", "hold": False, "stop": "boundary", "use": use, "cmd": "python train.py --seed 1", "est": est,
         "acct": {"ram": 5.83, "vram": 21453, "cpu": 4.0}, "io_sus": NOW - 20 * 60, "log": "logs/alpha_train_s1.log", "tail": ["step 40/100", "loss 0.31"], "vram": 42.0,
         "excl": "gpu", "est_min": 300.0, "oom0": None, "owner_gpu": None, "wait": 1.6, "link": "proj-a", "cpus": 4.0, "io": "heavy",
         # 项目看板按进度算的预计结束故意比推演的 t1 晚一小时：总览的「预计全部跑完」只取推演
         "prog": {"progress": [40, 100], "eta_ts": NOW + 9 * H, "eta_source": "近期速度", "rate_per_min": 2.5, "dur_min": 300.0}, "t0": NOW - 2 * H},
        {"kind": "queue", "queue": q_b, "root": root_b, "project": "proj-b", "name": "hold_until_data", "pid": 2003, "gpus": [], "tag": None,
         "hold": True, "stop": "boundary", "use": {"t": NOW - 4, "nproc": 1, "ram": 0.1, "ram_peak": 0.1, "vram": 0, "vram_peak": 0, "with_host": [], "with_gpu": [], "cores": 0.0},
         "cmd": "bash wait_data.sh", "est": None, "acct": None, "log": None, "tail": [], "vram": None, "excl": None, "est_min": 900.0,
         "oom0": None, "owner_gpu": None, "wait": 0.0, "link": "proj-b", "t0": NOW - 30 * 60,
         # 写了 after= 的占位任务：项目看板的预计结束取自推演（等的任务预计结束里最晚的）
         "prog": {"eta_ts": NOW + H, "eta_source": "等 proj-a 的 alpha_train_s1、beta_eval_s7 跑完", "dur_min": 90.0}},
        # 有进度行、还算不出速度（第一段只有一个步数 > 0 的采样）：详情写“暂无速度（进度 i/N）”，不写“没有进度行”（#17）
        {"kind": "queue", "queue": q_a, "root": root_a, "project": "proj-a", "name": "alpha_warm_s3", "pid": 2005, "gpus": ["GPU-aaaa-0"],
         "tag": None, "hold": False, "stop": "boundary", "use": {"t": NOW - 4, "nproc": 1, "ram": 1.2, "ram_peak": 1.4, "vram": 3000.0, "vram_peak": 3100.0,
                                                                "with_host": [], "with_gpu": [], "cores": 0.9},
         "cmd": "python train.py --seed 3", "est": None, "acct": None, "log": "logs/alpha_warm_s3.log", "tail": ["PROGRESS 400/21050"], "vram": 3.0,
         "excl": None, "est_min": 160.0, "oom0": None, "owner_gpu": None, "wait": 0.2, "link": "proj-a", "t0": NOW - 104 * 60,
         "prog": {"progress": [400, 21050], "eta_ts": NOW + 35 * 60, "eta_source": "估时（进度刚开始）", "dur_min": 139.4}}]
    w = lambda name, project, root, queue, why, since, pos, start, held=None, excl=None, miss=False, dep=None, cpu_wait=None: {  # noqa: E731
        "kind": "queue", "queue": queue, "root": root, "project": project, "name": name, "excl": excl,
        "est": {"ram": 5.8, "vram": 20.9, "src": "提示值", "ram_src": "同队列 近 2 次峰值 ×1.1", "cpus": 8, "cpu_src": "同名 近 3 次峰值 ×1.1"} if name else None,
        "why": why, "since": since, "miss": miss, "held": held, "dep": dep, "pos": pos, "start": start, "cpu_wait": cpu_wait}
    waiting = [
        w("alpha_train_s2", "proj-a", root_a, q_a, "显存不够：GPU-aaaa-0 余 24 GiB，要 21 GiB 还得等独占的跑完", NOW - 40 * 60,
          {"n": 1, "focus": True, "ratio": 18.9, "barrier": True}, NOW + 3 * H, excl="gpu"),
        w("alpha_train_s3", "proj-a", root_a, q_a, "等屏障放行", NOW - 10 * 60,
          {"n": 2, "focus": True, "ratio": 18.9, "barrier": False, "behind": ["proj-a", "alpha_train_s2"]}, NOW + 5 * H),
        # 推演里排不上；一直因 CPU 被挡了 15 分钟（worker 满 10 分钟才写 cpu_wait）：工作台出告警
        w("beta_eval_s1", "proj-b", root_b, q_b, "CPU 不够：要 8 核（同名 近 3 次峰值 ×1.1），按记账只剩 2.0 核（配额 17）", NOW - 20 * 60,
          {"n": 3, "focus": False, "ratio": 1.6, "barrier": False}, None, cpu_wait=900),
        w("beta_eval_s2", "proj-b", root_b, q_b, "被临时指令挡住：pause", None, None, None, held="pause"),
        w("beta_eval_s3", "proj-b", root_b, q_b, "等的 beta_prep 跑失败了，不会放行：修好重跑（删掉它的 .failed）后接着等；不等了就删掉这一行的 after=",
          None, None, None, dep="failed"),                                                                                  # 卡在依赖上
        w(None, "proj-b", "/data/me/proj-b/old", "jobs/old.txt", "被旧 gpu_queue 占着", NOW - H, None, None),                   # 整条队列受阻
        w("alpha_need_w0", "proj-a", root_a, q_a, "缺本地文件：/tmp/cache/w0.pt（续租、容器重启会清空本地盘；补好文件，或在命令里先 [ -f 文件 ] || 重新生成）", NOW - 300, None, None, miss=True)]
    plan = {"t": NOW - 60, "lanes": [
        {"root": root_a, "queue": q_a, "project": "proj-a", "kind": "queue", "stuck": None, "stuck_dep": None, "jobs": [
            {"name": "alpha_train_s1", "t0": NOW - 2 * H, "t1": NOW + 8 * H, "gpus": [0], "run": True},
            {"name": "alpha_train_s2", "t0": NOW + 3 * H, "t1": NOW + 5 * H, "gpus": [1]},
            {"name": "alpha_train_s3", "t0": NOW + 5 * H, "t1": NOW + 7 * H, "gpus": [1], "after": [["proj-b", "beta_eval_s7"]]}]},
        {"root": root_b, "queue": q_b, "project": "proj-b", "kind": "queue", "stuck": "推演到 7 天后还没轮到", "stuck_dep": None, "jobs": [
            {"name": "hold_until_data", "t0": NOW - 30 * 60, "t1": NOW + H, "gpus": [], "run": True,
             "deps": [["proj-a", "alpha_train_s1"], ["proj-b", "beta_eval_s7"]]}]}]}
    status = {
        "t": NOW - 4, "pid": 4242, "host": "srv-a",
        "info": {"gpus": [{"i": 0, "uuid": "GPU-aaaa-0", "name": "NVIDIA RTX A6000", "total": 46068.0, "used": 21000.0, "free": 25068.0, "util": 95.0},
                          {"i": 1, "uuid": "GPU-aaaa-1", "name": "NVIDIA RTX A6000", "total": 46068.0, "used": 900.0, "free": 45168.0, "util": 3.0}],
                 "nproc": 144, "mem": {"limit": 122.0, "used": 19.9, "anon": 4.5, "src": "cgroup", "peak": 31.5, "failcnt": 0.0, "oom_kill": None},
                 "cpu_quota": 17.0, "cpu_ns": 201382373909614, "disk": [{"path": "/data/me", "free": 200.6, "total": 1024.0}],
                 "started": NOW - 2 * 86400, "cpu_used": 3.2, "rootfs": {"used": 27.4, "total": 750.0, "fs": "overlay", "ok": True, "shm": 0.3},
                 # 主机压力（Worker.press）：CPU 被限流 38.5%；共享盘此刻比基线慢、样本还不够一天（基线取 20 ms）；本地盘有点忙；3.10 内核没有 PSI
                 "press": {"thr": 38.5, "psi": None,
                           "nfs": {"path": "/data", "addr": "192.0.2.7", "ms": 31.4, "ops": 12.3, "mib": 40.2, "base": 20.0, "n": 120, "need": 288, "floor": 20, "min_ops": 2, "slow": True},
                           "disk": {"dev": "nvme0n1", "busy": 37.5, "await": 0.8}}},
        "gpu_procs": [{"hpid": 1001, "pid": 2001, "mb": 20500.0, "uuid": "GPU-aaaa-0", "comm": "python", "owner": "job", "job": "alpha_train_s1"},
                      {"hpid": 1002, "pid": 2002, "mb": 700.0, "uuid": "GPU-aaaa-1", "comm": "python", "owner": "keepalive"},
                      {"hpid": 1003, "pid": 2004, "mb": 150.0, "uuid": "GPU-aaaa-1", "comm": "jupyter", "owner": "other"}],
        "gate": {"on": False, "at": 0.9}, "limits": {"tags": {"record": 1}},
        "overrides": [{"kind": "pause", "target": "proj-b", "until": None, "at": NOW - 2 * H},                  # 只给这台下的（--host）
                      {"kind": "focus", "target": "proj-a", "until": NOW + 6 * H, "global": True}],            # 项目级的（主机清单 [sched.focus]，#96）
        "local": {"t": NOW - 30, "jobs": [{"project": "proj-a", "job": "alpha_train_s3", "files": ["/tmp/cache/a.pt"], "bytes": 3e9, "heal": True, "how": "copy"},
                                           {"project": "proj-b", "job": "beta_eval_s1", "files": ["/tmp/y/b.bin"], "bytes": 1e8, "heal": False, "how": None}]},
        # 本地盘（#149，worker 的 LocalDisk.summary）：labmon 各项目的缓存与单格临时、labmon 目录的总占用、开机以来 labmon 之外新写的各目录（字节）
        "storage": {"t": NOW - 3000, "local": {"proj-a": {"cache": 1.5e10, "scratch": 2e9}, "proj-b": {"cache": 0, "scratch": 0}}, "own": 1.72e10,
                    "wrote": {"since": NOW - 2 * 86400, "b": 1.02e10, "n": 5210, "pending": 0, "t": NOW - 600, "more": [3, 1.5e8],
                              "top": [["/root/.cache/huggingface/hub", 5.2e9, NOW - 3 * H], ["/root/work/ckpt", 3.1e9, NOW - 1200], ["/opt/conda/pkgs", 1.55e9, NOW - 40 * H],
                                      ["/root/.", 2e8, NOW - 86400]]},
                    "projects": {"/data/me/proj-a": "proj-a", "/data/me/proj-b": "proj-b"}},
        "voided": [{"name": "beta_x", "project": "proj-b", "t": NOW - 9 * H, "why": "独占期间同卡出现别的进程", "how": "void", "kind": "queue"}],
        "kafight": None, "watchdogs": {"frozen": [], "seen": []},
        # CPU 持续被配额限流 25 分钟（Worker.throttle，#39）：alpha_train_s1 给 4 核、1 分钟窗口实测 9.2 核
        "throttle": {"since": NOW - 1504, "for": 1500, "thr": 38.5, "alert": True, "over": [{"project": "proj-a", "name": "alpha_train_s1", "cores": 9.2, "give": 4}], "other": 0.3},
        "guest": None, "util10": {"GPU-aaaa-0": 95, "GPU-aaaa-1": 3},
        # 吃满程度（#51）：卡 1 此刻只有保活（顶到 99%），按 0 算
        "gpu_busy": {"GPU-aaaa-0": {"now": 95.0, "m10": 88.4, "h1": 72.3, "cov": 3600}, "GPU-aaaa-1": {"now": 0, "m10": 0.0, "h1": 4.2, "cov": 3600, "ka": 99.0}}, "disk": {"root": "/data/share", "mine": True, "now": None, "since": NOW - 100,
                                                                              "yield": "共享盘慢（此刻 31 ms/次，本机基线 20）"},
        "room": {"ram": 97.5, "margin": 8.0, "gpus": {"GPU-aaaa-0": 24000.0, "GPU-aaaa-1": 44000.0}, "cpu": 12.5, "cpu_cap": 17},
        "queues": [{"root": root_a, "jobs": q_a, "gpus": None, "project": "proj-a", "stop": "boundary", "empty": False,
                    "dur": {"alpha_train_s2": _dur("alpha_train_s2", 120, "alpha_train"), "alpha_train_s3": _dur("alpha_train_s3", 120, "alpha_train")}},
                   {"root": root_b, "jobs": q_b, "gpus": "0", "project": "proj-b", "stop": "boundary", "empty": True, "dur": {}}],
        "plan": plan,
        "share": {"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {"proj-b": 2}, "use": {"proj-a": 18.9, "proj-b": 3.2}, "run": [["proj-a", 1.0, NOW - 2 * H, NOW + 8 * H]]},
        "running": running, "external": [], "waiting": waiting}
    return {"id": "srv-a", "label": "srv-a（A6000）", "error": None, "status_error": None, "status": status, "lan_off": False, "auto": True,
            "lw": local_writes(status["info"]["rootfs"], status["storage"]["wrote"], status["storage"]["own"]),     # overview._annotate
            "platform": {"job": "ME"}, "clock": int(NOW), "stopped": None, "age": 4.0, "worker_up": True,
            # 自用主机入池（#79，pool.tick 写的 pool.lend_view）：池里的清单直接放上来，此刻跑着 1 条
            "lend": SELF_LEND,
            "health": {"state": "ok", "fails": 0, "since": None, "why": None}, "lease_end": NOW + 5 * 86400, "auto_res": {"at": NOW + 4 * 86400, "off": False}, "renew": None,
            "log": ["2026-11-07 07:37:49 开始 alpha_train_s1（GPU 0 GPU-aaaa-0），独占卡", "2026-11-07 07:40:01 等待 alpha_train_s2：显存不够"],
            "done": [_done("alpha_train_s0", "proj-a", "0", NOW - 5 * H, NOW - 4.5 * H, root_a, q_a, with_host=["beta_old"]),
                     _done("beta_eval_s0", "proj-b", "1", NOW - 7 * H, NOW - 6 * H, root_b, q_b),
                     _done("alpha_hold_0", "proj-a", "0", NOW - 8 * H, NOW - 7.5 * H, root_a, q_a, excl="gpu", with_gpu=["beta_eval_s0"])],
            "series": _series(60, 122.0, 2)}


def _host_b() -> dict:
    """借来的组内服务器 srv-b：一张卡、一个 proj-b 任务在跑（没有进度，预计结束要取推演里的 run 条）、guest 与约定归还的指令。
    OWNER 说好约定时间前不用（borrow_away，#30）：指令的 keep 带 away，worker 状态的 guest.away 还没作废。"""
    root, q = "/data/me/proj-b/q2", "jobs/b2.txt"
    cutoff = NOW + 6 * 86400
    keep = {"vram": 2.0, "ram": 14.5, "cpu": 1.0, "peak": {"vram": 7.5, "ram": 9.2, "cpu": 1.5}, "n": 208, "bump": 1.0, "hold": False, "away": UNTIL}
    borrow = {"holder": "OWNER", "since": NOW - 16 * H, "until": UNTIL, "for": ["proj-b"], "light": True, "away": UNTIL, "cutoff": cutoff,
              "why": "OWNER 的租期到期，提前 1 小时", "returning": None, "now": False, "back": [], "tight": None, "overdue": False, "keep": keep,
              "other": {"vram": {}, "ram": 0.5, "cpu": 0.1}, "swap": None,
              # 约定归还挡住、这台不会再放的清单（borrow.late_jobs，取下面 waiting 里 held 为 deadline 的）：工作台告警
              "late": [{"project": "proj-b", "root": root, "queue": "jobs/b3.txt", "name": "beta_big_s1", "why": "约定归还 11-14 09:00 前跑不完（估 330 分钟）", "n": 2}],
              "late_do": "换到别的在用主机（labmon worker add）；截止取的是 OWNER 的租期到期，提前 1 小时，延长约定推不迟它",   # borrow.late_do
              "late_say": "截止取的是 OWNER 的租期到期，提前 1 小时，延长约定推不迟它，由实验会话换到别的在用主机"}   # late_do(user=True)
    status = {
        "t": NOW - 6, "pid": 9757, "host": "srv-b",
        "info": {"gpus": [{"i": 0, "uuid": "GPU-bbbb-0", "name": "NVIDIA RTX A6000", "total": 49140.0, "used": 6754.0, "free": 42386.0, "util": 90.0}],
                 "nproc": 144, "mem": {"limit": 61.0, "used": 2.2, "anon": 0.8, "src": "cgroup", "peak": 3.0, "failcnt": 0.0, "oom_kill": None},
                 "cpu_quota": 7.0, "cpu_ns": 1, "disk": [], "started": NOW - 86400, "cpu_used": 1.1, "rootfs": {"used": 13.9, "total": 750.0, "fs": "overlay", "ok": True},
                 # 新内核有 PSI；这一分钟共享盘没有读写（ms 为空），基线已按 14 天样本算；没找到本地盘的设备、cgroup 没有限流计数
                 "press": {"thr": None, "psi": {"cpu": 0.4, "io": 2.1, "memory": 0.0},
                           "nfs": {"path": "/data", "addr": None, "ms": None, "ops": 0.0, "mib": 0.0, "base": 26.8, "n": 3900, "need": 288, "floor": 20, "min_ops": 2, "slow": False},
                           "disk": None}},
        "gpu_procs": [{"hpid": 3001, "pid": 3002, "mb": 6700.0, "uuid": "GPU-bbbb-0", "comm": "python", "owner": "job", "job": "beta_eval_s7"}],
        "gate": {"on": False, "at": 0.9}, "limits": {},
        "overrides": [{"kind": "guest", "target": "proj-b", "until": None, "at": NOW - 86400, "keep": keep},
                      {"kind": "deadline", "target": "borrow", "until": cutoff, "at": cutoff},
                      {"kind": "focus", "target": "proj-a", "until": NOW + 6 * H, "global": True}],
        "local": {"t": NOW - 30, "jobs": []},
        "storage": {"t": NOW - 3000, "local": {"proj-b": {"cache": 1.2e10, "scratch": 0}}, "own": 1.2e10, "projects": {"/data/me/proj-b": "proj-b"},
                    "wrote": {"since": NOW - 16 * H, "b": 1.7e9, "n": 12, "pending": 1, "t": NOW - 900, "more": [0, 0], "top": [["/root/beta_out", 1.6e9, NOW - H], ["/tmp/.", 1e8, NOW - 2 * H]]}},
        "voided": [], "guest": {"keep": keep, "other": {"vram": {}, "ram": 0.5, "cpu": 0.1}, "back": [], "quiet": [], "tight": None, "hit": 0,
                  "away": {"until": UNTIL, "end": None, "why": None}},
        "kafight": {"t": NOW - 600, "n": 1, "who": "不是经 ka.sh 拉起的；暂停了看门狗 PID 4242（bash keepalive_final_check.sh：提到保活程序，在循环里），还卡时恢复"},
        "keepalive": {"ctl": True, "why": None, "stuck": {"t0": NOW - 2 * H, "n": 160, "why": "ka.sh stop 退出码 1：另一个调用者正持着锁"}},   # #102：要卡时停不了
        "watchdogs": {"frozen": [{"pid": 4242, "start": 123, "cmd": "bash keepalive_final_check.sh", "cwd": "/root/demo_eval/exp", "why": "提到保活程序，在循环里", "t": NOW - 600}],
                      "seen": [{"pid": 4300, "start": 456, "cmd": "python3 check.py", "cwd": "/root", "why": "提到保活程序，没见循环"}]},
        "util10": {"GPU-bbbb-0": 90}, "gpu_busy": {"GPU-bbbb-0": {"now": 90.0, "m10": 86.0, "h1": 85.5, "cov": 1200}}, "disk": None, "room": {"ram": 40.0, "margin": 8.0, "gpus": {"GPU-bbbb-0": 20000.0}, "cpu": 4.8, "cpu_cap": 7},
        "queues": [{"root": root, "jobs": q, "gpus": None, "project": "proj-b", "stop": "boundary", "empty": False, "dur": {}}],
        "plan": {"t": NOW - 50, "lanes": [{"root": root, "queue": q, "project": "proj-b", "kind": "queue", "stuck": None, "stuck_dep": None, "jobs": [
            {"name": "beta_eval_s7", "t0": NOW - H, "t1": NOW + 2 * H, "gpus": [0], "run": True},
            {"name": "beta_eval_s8", "t0": NOW + 2 * H, "t1": NOW + 4 * H, "gpus": [0]}]}]},
        "share": {"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {"proj-b": 2}, "use": {"proj-a": 18.9, "proj-b": 3.2}, "run": [["proj-b", 1.0, NOW - H, NOW + 2 * H]]},
        "running": [{"kind": "queue", "queue": q, "root": root, "project": "proj-b", "name": "beta_eval_s7", "pid": 3002, "gpus": ["GPU-bbbb-0"], "tag": None,
                     "hold": False, "stop": "boundary", "use": {"t": NOW - 6, "nproc": 2, "ram": 2.3, "ram_peak": 2.5, "vram": 6700.0, "vram_peak": 6900.0,
                                                                  "with_host": [], "with_gpu": ["stray_proc"], "cores": 1.2},
                     "cmd": "python eval.py --seed 7",   # est 没有 ram_src：#61 之前放行、新 worker 接管的任务
                     "est": {"ram": 3.0, "vram": 7.0, "util": 80, "src": "提示值", "cpus": 1, "cpu_src": "缺省 1 核"},
                     "acct": {"ram": 3.0, "vram": 7168, "cpu": 1.2},
                     "log": "logs/beta_eval_s7.log", "tail": ["eval batch 3"], "vram": 7.0, "excl": None, "est_min": 180.0, "oom0": None, "owner_gpu": None,
                     "wait": 0.5, "link": "proj-b", "t0": NOW - H}],
        "external": [], "waiting": [{"kind": "queue", "queue": "jobs/b3.txt", "root": root, "project": "proj-b", "name": "beta_big_s1", "excl": None, "est": None,
                                     "why": "约定归还 11-14 09:00 前跑不完（估 330 分钟）", "since": NOW - 600, "cpus": None, "io": None, "cpu_wait": None,
                                     "miss": False, "held": "deadline", "dep": None, "pos": None, "start": None}]}
    return {"id": "srv-b", "label": "srv-b", "error": None, "status_error": None, "status": status, "lan_off": False, "auto": True, "platform": None,
            "lw": local_writes(status["info"]["rootfs"], status["storage"]["wrote"], status["storage"]["own"]),
            "clock": int(NOW), "stopped": None, "age": 6.0, "worker_up": True, "health": {"state": "ok", "fails": 0, "since": None, "why": None},
            "lease_end": cutoff + H, "renew": None, "borrow": borrow,
            "log": ["2026-11-07 08:50:36 worker 启动 PID=9757 主机=srv-b（容器 2026-11-06 08:58:01 启动）"],
            "done": [_done("beta_eval_s6", "proj-b", "0", NOW - 3 * H, NOW - 1.2 * H, root, q)], "series": _series(30, 61.0, 1)}


def _strip(kind: str) -> list:
    return [[kind, 0.8] if k % 9 else None for k in range(168)]


def _stats(job: float, ka: float) -> dict:
    return {"n": 264, "hours": 46, "frac": {"job": job, "unknown": 0.0, "keepalive": ka, "idle": round(1 - job - ka, 3), "down": 0.0},
            "h": {"job": round(46 * job, 1), "unknown": 0.0, "keepalive": round(46 * ka, 1), "idle": 0.0, "down": 0.0},
            "share": 0.8, "share_h": 37.1, "util_job": 91.2, "vram_job": 19286, "cpu_real": 0.21, "mem_real": 6.3}


def _server(sid: str, holder: str, state: str | None, **kw) -> dict:
    sample = None if state in (None, "off", "down") else {
        "state": state, "work": [] if state == "idle" else [{"kind": "job" if state == "job" else "keepalive", "label": "train.py" if state == "job" else "keep_alive.py",
                                                            "rule": None, "top": 100, "mb": 6748, "n": 1, "names": ["train.py · --lr 0.1", "train.py · --lr 0.2"],
                                                            "cwd": f"/data/share/{holder.lower()}/exp", "chain": ["bash run.sh"], "args": "python train.py", "etime": 48967, "cpu_s": 4.0, "pid": 100,
                                                            **({"owner": holder} if state == "job" else {})}],
        "util": 82.3, "util_real": 82.3 if state == "job" else 0, "vram": 6754.0, "vram_total": 46068.0, "vram_real": 6754.0 if state == "job" else 0,
        "vram_ka": 0 if state == "job" else 6748, "cpu": 1.69, "cpu_real": 1.0, "cpu_quota": 17.0, "nproc": 144.0,
        "mem": {"limit": 122.0, "used": 9.2, "anon": 1.0}, "mem_real": 5.9, "mem_anon": 0.0, "mem_ka": 3.3, "share": state != "job",
        "gpus": [{"i": 0, "uuid": f"GPU-{sid}-0", "name": "NVIDIA RTX A6000", "used": 6754.0, "total": 46068.0, "util": 82.3}],
        "started": NOW - 3 * 86400, "root": {"total": 750.0, "used": kw.pop("root_gb", 0.4), "fs": "overlay", "ok": True}, "vscode": state == "idle", "terms": 1, "guard": None,
        "t": int(NOW - 300)}
    wrote = kw.pop("wrote", None)
    base = {"id": sid, "holder": holder, "job": holder, "label": holder, "ssh": "root@192.0.2.9", "port": 15100 + len(sid), "pool": "pool-public", "public": True,
            "last": NOW - 300, "error": None, "down": None, "fails": 0, "since": None, "started": NOW - 3 * 86400, "lease_end": NOW + 4 * 86400, "off": None,
            "last_job": NOW - 100, "state": state, "sample": sample, "strip": _strip(state or "idle"), "h0": int(NOW // 3600 * 3600) - 167 * 3600, "idle_h": 0,
            "s7": _stats(0.2, 0.7), "s30": _stats(0.3, 0.6), "guard": {"on": False, "status": "off", "since": None}, "borrow": None, "using": "",
            "lend": {"on": False, "hours": None, "open": False, "say": "没入池"},          # 入池（pool.lend_view，#24）
            "reserve": {"vram": 2.0, "ram": 6.6, "cpu": 1.0, "peak": {"vram": 7.5, "ram": 9.2, "cpu": 1.5}, "n": 208, "bump": 1.0}}
    base.update(kw)
    base["lease_cut"] = base["lease_end"] - H if base["lease_end"] else None     # 对方租期截止（borrow.cutoff：到期前 1 小时）
    base["lw"] = local_writes(sample["root"], wrote) if sample else None         # fleet.server_lw
    return base


SELF_LEND = {"on": True, "hours": None, "open": True, "lent": False, "returning": False, "rest": None, "say": "入池，全天；跑着池里 1 条清单",
             "mine": True, "placed": 1, "bar": None, "holder": "ME"}


def _fleet(mine_host: dict) -> dict:
    boot = lambda sid, holder: {  # noqa: E731
        "image": "registry.local/lab/ubuntu:" + holder.lower(), "ip": "192.0.2.9", "ports": {"22": "15158"}, "cpu": 17, "memory": 122, "disk": 750, "gpu": 1,
        "container": "c" * 12, "role_id": "r" * 8, "pool": "pool-public", "created": "2026-11-05 10:36:50", "gpu_uuids": [f"GPU-{sid}-0"], "holder": holder, "job": holder,
        "src": "平台核对", "t": NOW - 86400, "job_id": "j" * 8, "clone_job": "j" * 8, "clone_t": NOW - 2 * 86400, "has_clone": True,
        "run_cmd": "/etc/init.d/ssh restart && sleep infinity", "volumes": ["/nfs/lab → /data/share（rw）"]}
    person = lambda slots: {  # noqa: E731
        "image": "registry.local/lab/ubuntu:base", "run_cmd": "/etc/init.d/ssh restart && sleep infinity", "note": None, "src": "job", "saved": True,
        "t": NOW - 86400, "slots": slots}
    servers = [
        _server("srv-b", "OWNER", "job", lease_end=NOW + 6 * 86400 + H, guard={"on": False, "status": "borrowed"},
                borrow={"since": NOW - 16 * H, "until": UNTIL, "for": ["proj-b"], "by": "ME", "away": UNTIL, "returned": None}, reserve=None,
                strip=[["job", 0.0, ["ME"]] if k >= 152 else ["keepalive", 0.8] for k in range(168)]),
        # 本地盘开机以来写了 6.2 GiB（过门槛）：探针扫出的各目录
        _server("srv-d", "PERSON3", "keepalive", idle_h=15, lease_end=NOW + 20 * H, using="VS Code 远程连着", root_gb=6.2,
                wrote={"since": NOW - 3 * 86400, "t": NOW - 1800, "b": 6.0e9, "n": 812, "partial": False, "pending": 0, "err": None, "more": [5, 2e8],
                       "top": [["/root/data/out/run1", 4.0e9, NOW - 5 * H], ["/root/.vscode-server", 1.2e9, NOW - 2 * H], ["/home/u/.cache/pip", 6e8, NOW - 50 * H]]},
                guard={"on": True, "status": "ours", "since": NOW - 2 * H, "t": NOW - H, "until": NOW + 5 * 60, "by": "看板", "at": NOW - H}),
        _server("srv-e", "PERSON4", "down", down="连不上：timeout", since=NOW - H, last=NOW - H, fails=3, lease_end=NOW + 2 * 86400, strip=[None] * 168,
                guard={"on": True, "status": "fail", "fail": {"msg": "ssh 失败"}, "at": NOW + 600, "since": NOW - H}, reserve=None),
        _server("srv-f", "PERSON5", "off", off={"t": NOW - 86400, "holder": "PERSON5"}, lease_end=None, guard={"on": False, "status": "off", "since": None}, reserve=None, idle_h=0, strip=[None] * 168),
        _server("srv-g", "PERSON6", "idle", pool="pool-private", public=False, guard={"on": True, "status": "wait", "since": NOW - 30 * 60, "at": NOW + 30 * 60},
                lend={"on": True, "hours": "22:00-09:00", "open": False, "say": "入池，22:00 开始"}),
        # 算力池借着的（#25）：持有人开了入池，看板自动借来跑池里的清单（borrow.pool，by 是「算力池」；lend.lent）
        _server("srv-h", "PERSON7", "job", guard={"on": False, "status": "borrowed"}, reserve=None,
                borrow={"since": NOW - 2 * H, "until": NOW + 8 * H, "for": None, "by": "算力池", "away": None, "returned": None, "pool": True},
                lend={"on": True, "hours": None, "open": True, "lent": True, "returning": False, "rest": None, "say": "算力池借着"})]
    # 自用主机（#58）：字段与组内服务器同名，sample 与探针的 analyze 同形（fleet.from_worker 由 worker 状态换出：按任务归组、不带 pid、没有保活进程树单独的量）
    info = mine_host["status"]["info"]
    mine = [{"id": "srv-a", "holder": "ME", "job": "ME", "label": mine_host["label"], "pool": "pool-private", "ssh": "root@192.0.2.1", "port": 15155, "mine": True, "public": False,
             "state": "job", "lease_end": mine_host["lease_end"], "work": [{"kind": "job", "label": "alpha_train_s1", "mb": None}, {"kind": "unknown", "label": "jupyter", "mb": 150.0}],
             "last": mine_host["status"]["t"], "error": None, "down": None, "since": None, "started": info["started"], "guard": {"on": False, "status": "worker"},
             "sample": {"state": "job", "util": 49.0, "util_real": 49.0, "vram": 21900.0, "vram_total": 92136.0, "vram_real": 20650.0, "vram_ka": 700.0,
                        "cpu": 3.2, "cpu_real": 3.2, "cpu_quota": 17.0, "nproc": 144, "mem": info["mem"], "mem_real": 19.9, "mem_ka": None, "share": False,
                        "gpus": [{**{k: g[k] for k in ("i", "uuid", "name", "used", "total", "util")}, "busy": mine_host["status"]["gpu_busy"].get(g["uuid"])} for g in info["gpus"]],
                        "root": info["rootfs"],
                        "started": info["started"],
                        "work": [{"kind": "job", "label": "alpha_train_s1", "mb": 20500, "n": 1, "names": [], "etime": 7200, "cwd": "/data/me/proj-a/q1", "owner": "ME"},
                                 {"kind": "job", "label": "labmon 之外：jupyter", "mb": 150, "n": 1, "names": [], "etime": None, "cwd": None, "owner": "ME"},
                                 {"kind": "keepalive", "label": "保活", "mb": 700, "n": 1, "names": [], "etime": None, "cwd": None}]},
             "strip": _strip("job"), "h0": int(NOW // 3600 * 3600) - 167 * 3600, "idle_h": 0, "s7": _stats(0.9, 0.05), "s30": _stats(0.85, 0.1),
             "lw": mine_host["lw"], "lend": mine_host.get("lend")}]
    holders = [{"holder": h, "servers": [s], **_stats(0.3, 0.6), "borrowed": [], "lent": []}
               for h, s in (("PERSON3", "srv-d"), ("PERSON4", "srv-e"), ("PERSON6", "srv-g"), ("GONE", "srv-z"))]
    # 借卡（#52）：ME 借 OWNER 的 srv-b 16 小时，OWNER 那边这段记成“借出”；ME 的自用主机 srv-a 也有用量
    holders += [{"holder": "ME", "servers": ["srv-a"], **_stats(0.88, 0.1), "hours": 62, "borrowed": [{"id": "srv-b", "from": "OWNER", "h": 16.0}], "lent": []},
                {"holder": "OWNER", "servers": ["srv-b"], **_stats(0.0, 0.65), "borrowed": [], "lent": [{"id": "srv-b", "to": "ME", "h": 16.0}]}]
    holders[-1]["frac"].update(idle=0.0, lent=0.35)
    holders[-1]["h"].update(lent=16.0)
    holders[-1]["util_job"] = None
    return {"ops": {"t": NOW, "boot": {"srv-b": {**boot("srv-b", "OWNER"), "spec": "s2a"}, "srv-d": {**boot("srv-d", "PERSON3"), "spec": "s2a"},
                                       "srv-a": {**boot("srv-a", "ME"), "pool": "pool-private", "spec": "s1"}},
                    "platform": {"t": NOW - 86400, "running": [{"id": "j" * 8, "name": "OWNER", "pool": "pool-public", "created": "2026-11-05 10:36:50", "left": "6d 11h", "sid": "srv-b"}],
                                 "issues": ["PERSON4 的作业在平台上找不到"], "changes": []},
                    "op": None, "session": {"logged_in": True, "at": NOW - 86400, "until": NOW + 86400},
                    "names": {"sync": "与平台核对", "renew": "续租", "ka": "开保活", "off": "关机", "swap": "换人", "move": "换节点", "open": "开机", "borrow": "借来用"},
                    "guard": {"idle_min": 60, "watch_min": 5},
                    "people": {"ME": person(["srv-a"]), "OWNER": person(["srv-b"]), "PERSON3": person(["srv-d"]), "PERSON4": person(["srv-e"]),
                               "PERSON5": person([]), "PERSON6": person(["srv-g"]), "NEWBIE": {**person([]), "src": "default", "note": "下个月来"}},
                    "defaults": {"image": "registry.local/lab/ubuntu:base", "run_cmd": "/etc/init.d/ssh restart && sleep infinity", "saved": True},
                    "pools": {"pool-private": {"private": True, "ref": "srv-a", "spec": "s1", "hosts": [],
                                               "specs": [{"specificationId": "s1", "specificationName": "priv-1GPU", "quantityAvailable": 2, "specificationAmount": 4, "isSchedule": 0, "queueNum": 0}],
                                               "nodes": ["root@192.0.2.1"]},
                              "pool-public": {"private": False, "ref": "srv-b", "spec": "s2a", "hosts": [],
                                              "specs": [{"specificationId": "s2a", "specificationName": "nodeA-1GPU", "quantityAvailable": 2, "specificationAmount": 4, "isSchedule": 0, "queueNum": 0},
                                                        {"specificationId": "s2b", "specificationName": "nodeB-1GPU", "quantityAvailable": 0, "specificationAmount": 4, "isSchedule": 0, "queueNum": 0}],
                                              "nodes": ["root@192.0.2.9", "root@192.0.2.10"]}},
                    "pools_t": NOW - 86400, "volumes": [{"path": "/nfs/lab", "name": "lab"}], "port_range": [15151, 15199]},
            "servers": servers, "mine": mine, "holders": holders,
            "unknown": [{"id": "srv-d", "holder": "PERSON3", "kind": "unknown", "label": "宿主机 PID 777（容器里找不到）", "mb": 5000, "cwd": None, "chain": [], "names": []}],
            "quota": {"private": 2, "private_max": 4, "public": 3, "public_max": 2, "private_pools": ["pool-private"]},
            "interval": 600, "share": {"vram": 0.3, "util": 30, "cpu": 0.3, "mem": 0.3},
            "names": {"job": "实验", "unknown": "未识别", "keepalive": "只有保活", "idle": "空闲", "down": "掉线", "off": "已关机"}}


def _disk() -> dict:
    owner = lambda name, b, **kw: {  # noqa: E731
        "name": name, "person": True, "public": False, "dirs": [name.lower()], "b": b, "n": 150237, "m": NOW - 900, "cold": 2e8, "d24": 7e9, "d7": 8.5e9,
        "t0": NOW - 3600, "pending": 0, "limit": None, "over": False,
        "units": [{"u": f"{name.lower()}/archives", "b": b * 0.4, "n": 167, "m": NOW - 16000, "cold": 0, "d24": 4.6e9, "d7": 5e9, "t": NOW - 2000, "s": 12, "due": NOW + 3600, "top": [["big.tar", 3e10]]},
                  {"u": f"{name.lower()}/codex-remote-batch/snapshots/run_20261107_seed0_lr3e4_bs256_warmup2000_long", "b": b * 0.1, "n": 9, "m": NOW - 5000, "cold": 3e9, "d24": 1e9, "d7": 1e9,
                   "t": NOW - 4000, "s": 3, "due": NOW + 3600, "top": [["step_120000.ckpt", 1e10]]},  # 长目录名：表格不被撑出横向滚动（#108）
                  {"u": f"{name.lower()}/new", "b": None, "n": None, "m": None, "cold": None, "d24": None, "d7": None, "t": None}],
        "spark": [[NOW - 86400 * 3, b * 0.95], [NOW - 86400, b * 0.98], [NOW, b]], **kw}
    return {"root": "/data/share", "scanner": "srv-a", "at": NOW - 60, "df": {"total": 1.1e12, "free": 2.2e11, "used": 8.75e11, "t": NOW},
            "owners": [owner("OWNER", 1.78e11, limit=200), owner("ME", 1.2e11, limit=100, over=True), owner("PERSON3", 5e10),
                       {"name": "公共", "person": False, "public": True, "dirs": ["envs", "tmp"], "b": 3e10, "n": 20, "m": NOW - 5000, "cold": 1e10, "d24": 0, "d7": 0, "t0": NOW - 7000, "pending": 0, "limit": None, "over": False, "units": [], "spark": []},
                       {"name": "stray_dir", "person": False, "public": False, "dirs": ["stray_dir"], "b": 0, "n": 0, "m": None, "cold": 0, "d24": 0, "d7": 0, "t0": None, "pending": 3, "limit": None, "over": False, "units": [], "spark": []}],
            "eta": 11.8, "eta7": 13.1, "units": 339, "pending": 3, "unscanned": 5e10, "d24": 1.9e10, "d7": 2.3e10, "cold": 2.3e11,
            "hist": [[NOW - k * 6 * 3600, 8.4e11 + k * 1e9] for k in range(28, -1, -1)], "warn_gb": 30, "soon_days": 3, "burst_gb": 30,
            "names": ["ME", "OWNER", "PERSON3"], "owners_set": {"stray_dir": "OWNER", "tmp": "公共"}, "now": "/data/share/owner/x",
            "yield": "共享盘慢（此刻 31 ms/次，本机基线 20）",
            "ours": {"base": "/data/share/me", "dirs": [{"u": "/data/share/me/proj-a", "name": "proj-a", "b": 5.7e10, "t": NOW - 3000, "p": "proj-a"},
                                                        {"u": "/data/share/me/envs", "name": "envs", "b": 2e10, "t": NOW - 3000, "p": None},
                                                        {"u": "/data/share/me/old_stuff", "name": "old_stuff", "b": 1e10, "t": NOW - 3000, "p": None}]},
            "colors": {"ME": "#7F77DD", "OWNER": "#1D9E75", "PERSON3": "#D85A30"},
            "alerts": [{"key": "burst:OWNER", "msg": "共享盘：OWNER 24 小时涨了 40 GiB", "notify": True}, {"key": "over:ME", "msg": "ME 超过提醒线 100 GiB", "notify": False}]}


# 挪法建议（rebalance.suggest 给的一项；text 由看板写好，页面原样摆）
REBAL = {"key": "rebalance:proj-a:srv-b:srv-a/jobs/q1.txt/alpha_train_s3", "target": "srv-b", "jobs": [{"host": "srv-a", "queue": "jobs/q1.txt", "name": "alpha_train_s3"}],
         "from": NOW + 7 * H, "to": NOW + 4 * H, "cut": [],
         "text": "把 srv-a 上 jobs/q1.txt 的 alpha_train_s3 挪到 srv-b（这台还没有本项目同一种环境的清单：新建一条（与原清单同目录，.env 照抄原清单的，但 QUEUE_STATE 与 QUEUE_LOGS 写这条自己的；原清单没有 .env 时也新建一个，只写这两行）并 labmon worker add srv-b /data/proj-a 新清单 --project proj-a）：整批预计从 11-11 02:59 提前到 11-10 23:59（早 3.0 小时）；按 20:59 的推演"}


# 登记的全局视图（overview._regs）：在用的取状态里的 queues，已归还的（代读的那台这轮没连上：stale）、主机清单里没有的取登记文件
def _regs(a: dict, b: dict) -> list[dict]:
    st = lambda h: [{k: q[k] for k in ("root", "jobs", "project", "stop", "gpus", "empty", "sleeping") if k in q} for q in h["status"]["queues"]]   # noqa: E731
    # 在用主机带资源池与上一台 prev（同池最近归还的自用主机，接上卡排第一，#95）
    # srv-a 上 proj-b 的 jobs/bd.txt 跑空满 2 天已休眠：项目页已收起（项目摘要的 quiet），管理抽屉数进“跑完”、逐条里不列并标含已休眠（#121、#164）
    return [{"host": "srv-a", "label": a["label"], "state": "active", "returned": None, "home": "/data/me/labmon", "stale": False, "t": NOW - 4,
             "queues": st(a) + [{"root": "/data/me/proj-b/qd", "jobs": "jobs/bd.txt", "project": "proj-b", "stop": None, "gpus": None, "empty": True, "sleeping": True}],
             "pool": "pool-mine", "prev": "srv-old3"},
            {"host": "srv-b", "label": b["label"], "state": "active", "returned": None, "home": "/data/me/labmon", "stale": False, "t": NOW - 6, "pool": None, "prev": None,
             # jobs/b4.txt 刚登记、项目快照里还没有这条车道：抽屉里写“下一轮采集后显示”；proj-b 的 jobs/b3.txt 反过来，monitor.toml 写了、没登记：“只显示”
             "queues": st(b) + [{"root": "/data/me/proj-b/q2/", "jobs": "jobs/b4.txt", "project": "proj-b", "stop": "file:STOP", "gpus": None, "empty": False}]},
            # 交给算力池、还没放到哪台的（#24）：在用的之后、已归还的之前
            {"host": "pool", "label": "算力池", "state": "pool", "returned": None, "home": "/data/me/labmon", "stale": False, "t": NOW,
             "queues": [{"root": "/data/me/proj-a/pq", "jobs": "jobs/p.txt", "project": "proj-a", "stop": "term", "gpus": None, "added": "2026-11-17 09:00",
                         "added_t": NOW - H, "key": "pq__jobs_p.txt__abcdef"}]},
            {"host": "srv-old2", "label": "srv-old2（借 X）", "state": "returned", "returned": NOW - 30 * H, "home": "/data/me/labmon", "stale": True, "t": NOW - 600,
             "queues": [{"root": "/data/me/proj-b/q9", "jobs": "jobs/b9.txt", "project": "proj-b", "stop": "term", "gpus": [0, 1], "added": "2026-11-08 10:00", "key": "q9__jobs_b9.txt__abcdef"}]},
            {"host": "srv-old3", "label": "srv-old3", "state": "returned", "returned": NOW - 50 * H, "home": "/data/me/labmon", "stale": False, "t": NOW,
             "queues": [{"root": "/data/me/proj-b/q8", "jobs": "jobs/b8.txt", "project": "proj-b", "stop": None, "gpus": None, "added": "2026-11-07 10:00", "key": "q8__jobs_b8.txt__abcdef"}]},
            {"host": "old-id", "label": "old-id", "state": "unlisted", "returned": None, "home": "/data/me/labmon", "stale": False, "t": NOW,
             "queues": [{"root": "/data/me/proj-a/z", "jobs": "jobs/z.txt", "project": "proj-a", "stop": None, "gpus": None, "added": "2026-10-30 10:00", "key": "z__jobs_z.txt__abcdef"}]},
            {"host": "srv-x", "label": "srv-x", "state": "unlisted", "returned": None, "home": "/data/me/labmon", "stale": False, "t": NOW, "queues": []}]


# 项目配置的只读摘要（serve.Overview.cfg_info）；stale 是数据过期的实际门槛（iv 是 monitor.toml 的采集间隔，B 用 --interval 定死了、两者不同）
cfg_stale = lambda iv, st: {"s": stale_limit(iv, st), "runs": STALE_RUNS, "pad": STALE_PAD_S, "iv": iv}  # noqa: E731
CFG_A = {"path": "/Users/me/.local/state/labmon/projects/proj-a/monitor.toml", "out_dir": "/Users/me/.local/state/labmon/projects/proj-a/out", "title": "Alpha 实验", "interval": 120, "interval_fixed": False,
         "refresh": 60, "hide_done_hours": 24,
         "groups": [{"id": "E1", "label": "E1 主实验", "color": "#7F77DD"}, {"id": "abl", "label": "消融", "color": "#1D9E75"}], "auto_groups": True,
         "markers": [{"date": "2026-11-07", "label": "开题", "ts": time.mktime((2026, 11, 7, 23, 59, 59, 0, 0, -1))},
                     {"date": "2026-11-13 12:00", "label": "投稿截止", "ts": time.mktime((2026, 11, 13, 12, 0, 0, 0, 0, -1))}],
         "alerts": {"gpu_idle_util": 5.0, "gpu_idle_min": 15, "stale_min": 5, "log_silent_min": 0}, "spec": cfg_spec(), "stale": cfg_stale(120, 5)}
CFG_B = {"path": "/Users/me/.local/state/labmon/projects/proj-b/monitor.toml", "out_dir": "/Users/me/.local/state/labmon/projects/proj-b/out", "title": "Beta 实验", "interval": 300, "interval_fixed": True,
         "refresh": 60, "hide_done_hours": 12, "groups": [], "auto_groups": False, "markers": [],
         "alerts": {"gpu_idle_util": 5.0, "gpu_idle_min": 15, "stale_min": 5, "log_silent_min": 30}, "spec": cfg_spec(), "stale": cfg_stale(60, 5)}
# 项目快照里的计划项进度（serve.proj_info 的 groups、ungrouped）：写明的两组与一组自动归出来的
GROUPS_A = [{"id": "E1", "label": "E1 主实验", "color": "#7F77DD", "auto": False, "total": 6, "done": 3, "failed": 1, "running": 1, "queued": 1},
            {"id": "abl", "label": "消融", "color": "#1D9E75", "auto": False, "total": 2, "done": 0, "failed": 0, "running": 0, "queued": 2},
            {"id": "ext_", "label": "ext_*", "color": "#BA7517", "auto": True, "total": 3, "done": 3, "failed": 0, "running": 0, "queued": 0}]


def overview() -> dict:
    a, b = _host_a(), _host_b()
    for h in (a, b):                                   # 主机项的吃满程度（#51）：worker 的 gpu_busy 加上曲线汇的 24 小时平均，与看板同一个函数
        h["busy"] = gpu_busy(h["status"], h["series"])
    occ_a = [{"p": "proj-a", "n": "alpha_train_s0", "lane": "车道 1", "t0": NOW - 5 * H, "t1": NOW - 4.5 * H, "g": [0], "st": "done"},
             {"p": "proj-b", "n": "beta_eval_s0", "lane": None, "t0": NOW - 7 * H, "t1": NOW - 6 * H, "g": [0], "st": "failed"},
             {"p": "proj-b", "n": "beta_x", "lane": None, "t0": NOW - 9 * H, "t1": NOW - 8 * H, "g": [1], "st": "stop"},
             {"p": "proj-a", "n": "alpha_train_s1", "lane": "车道 1", "t0": NOW - 2 * H, "t1": NOW + 8 * H, "g": [0], "st": "run"},
             {"p": "proj-a", "n": "alpha_train_s2", "lane": "车道 1", "t0": NOW + 3 * H, "t1": NOW + 5 * H, "g": [1], "st": "plan"},
             {"p": "proj-a", "n": "alpha_train_s3", "lane": "车道 1", "t0": NOW + 5 * H, "t1": NOW + 7 * H, "g": [1], "st": "plan", "after": [["proj-b", "beta_eval_s7"]]}]
    occ_b = [{"p": "proj-b", "n": "beta_eval_s7", "lane": "车道 1", "t0": NOW - H, "t1": NOW + 2 * H, "g": [0], "st": "run"},
             {"p": "proj-b", "n": "beta_eval_s8", "lane": "车道 1", "t0": NOW + 2 * H, "t1": NOW + 4 * H, "g": [0], "st": "plan"}]
    return {"generated": NOW, "hosts": [a, b], "colors": {"proj-a": "#7F77DD", "proj-b": "#D85A30"}, "notices": [],
            "lan": {"ok": True, "since": None, "checked": NOW}, "wake": [], "event_kinds": EVENT_KINDS, "event_cats": EVENT_CATS,
            "fleet": _fleet(a), "disk": _disk(), "occ": {"srv-a": occ_a, "srv-b": occ_b}, "regs": _regs(a, b),
            # 算力池（pool.tick，#25）：各待放清单为什么还没放（键「目录|清单」）；在借着的主机上的 since 是这一轮的时刻，没有 why 的是这一轮正放出去
            "pool": {"lists": {
                "/data/me/proj-a/pq|jobs/p.txt": {"project": "proj-a", "root": "/data/me/proj-a/pq", "jobs": "jobs/p.txt", "host": "pool", "since": NOW - H,
                                                  "why": "池里借着的服务器都放不下（srv-h：显存不够）；没有能借的入池服务器（srv-g 不在入池时段）"},
                "/data/me/proj-b/pq2|jobs/p2.txt": {"project": "proj-b", "root": "/data/me/proj-b/pq2", "jobs": "jobs/p2.txt", "host": "srv-h", "since": NOW,
                                                    "why": "池里借着的服务器都放不下（srv-h：内存不够）"},
                "/data/me/proj-a/pq3|jobs/p3.txt": {"project": "proj-a", "root": "/data/me/proj-a/pq3", "jobs": "jobs/p3.txt", "host": "pool", "since": NOW - 5 * 60},
                # 池里跑完了的（#86）：不算在等，抽屉里收在后面
                "/data/me/proj-a/pq4|jobs/cpu.txt": {"project": "proj-a", "root": "/data/me/proj-a/pq4", "jobs": "jobs/cpu.txt", "host": "pool", "since": NOW - 3 * H,
                                                     "why": "清单里的行都跑完了：不用了就 labmon worker rm pool 撤掉，往清单里加了行会接着放", "done": True}},
                "gone": []},
            "rebalance": {"proj-a": dict(REBAL), "proj-b": {"why": ["srv-a 上本项目 11-10 22:59 后没活，接不了：srv-b 上 jobs/b3.txt 的队尾 hold_b 是占位任务，不挪"], "from": NOW + 9 * H}},
            "sched": {"cap_min": 30.0, "hl_h": 24.0, "use_sum": 22.1, "projects": [
                {"id": "proj-a", "person": "amy", "pk": "5a1c0e7d9b2f", "puse": 22.1, "w": 1, "use": 18.9, "cards": 1.0, "queued": 2, "waiting": 2, "wait_avg": 12.5, "wait_n": 3, "focus": {"until": NOW + 6 * H, "hosts": None}, "paused": None},
                {"id": "proj-b", "person": "amy", "pk": "5a1c0e7d9b2f", "puse": 22.1, "w": 2, "use": 3.2, "cards": 1.0, "queued": 1, "waiting": 1, "wait_avg": None, "wait_n": 0, "focus": None, "paused": {"until": None, "hosts": ["srv-a"], "by": {"srv-a": "amy"}}}]},
            "projects": [
                {"id": "proj-a", "title": "Alpha 实验", "owner": "amy", "alerts": 1, "alert_msgs": ["srv-a 数据盘 只剩 12.5 GiB"],
                 "generated": NOW - 120, "no_work_since": None,
                 "lanes": [{"host": "srv-a", "root": "/data/me/proj-a/q1", "queue": "jobs/q1.txt", "label": "车道 1", "unfinished": 4, "run": 2, "returned": None}],
                 "markers": [{"label": "开题", "ts": NOW - 3 * 86400}, {"label": "投稿截止", "ts": NOW + 3 * 86400}], "cfg": CFG_A, "groups": GROUPS_A, "ungrouped": 2,
                 "archive_block": "还有实验：2 个任务在跑，2 个在等，清单里还有 4 个没跑完"},
                {"id": "proj-b", "title": "Beta 实验", "alerts": 0, "alert_msgs": [], "generated": NOW - 60, "no_work_since": None,
                 "lanes": [{"host": "srv-a", "root": "/data/me/proj-b/q", "queue": "jobs/b.txt", "label": "车道 2", "unfinished": 1, "run": 1, "returned": None},
                           {"host": "srv-b", "root": "/data/me/proj-b/q2", "queue": "jobs/b2.txt", "label": "车道 1", "unfinished": 2, "run": 1, "returned": None},
                           {"host": "srv-b", "root": "/data/me/proj-b/q2", "queue": "jobs/b3.txt", "label": "车道 3", "unfinished": 2, "run": 0, "returned": None},
                           {"host": "srv-old2", "root": "/data/me/proj-b/q9", "queue": "jobs/b9.txt", "label": "车道 1", "unfinished": 3, "run": 0, "returned": NOW - 30 * H},
                           {"host": "srv-old3", "root": "/data/me/proj-b/q8", "queue": "jobs/b8.txt", "label": "车道 1", "unfinished": 0, "run": 0, "returned": NOW - 50 * H},
                           {"host": "srv-gone", "root": "/data/me/proj-b/q7", "queue": "jobs/b7.txt", "label": "车道 1", "unfinished": 0, "run": 0, "returned": NOW - 90 * H}],
                 "markers": [{"label": "组会", "ts": NOW + 86400}], "quiet": [{"host": "srv-a", "root": "/data/me/proj-b/qd", "queue": "jobs/bd.txt", "last": NOW - 3 * 86400, "sleeping": True}],
                 "cfg": CFG_B, "groups": [], "ungrouped": 0, "archive_block": None}],
            # 已归档的项目（projects.json，#97）：侧栏「已归档 N 个」与它的抽屉
            "archived": [{"id": "proj-old", "title": "Gamma 旧实验", "t": NOW - 3 * 86400, "revision": "demo-archive-100",
                          "regs": [{"host": "srv-a", "root": "/data/me/gamma/q/", "jobs": "jobs/g.txt"}, {"host": "srv-old2", "root": "/data/me/gamma/q2", "jobs": "jobs/g2.txt"}],
                          "skipped": ["srv-x（登记没读到）"]},
                         {"id": "proj-older", "title": None, "t": NOW - 20 * 86400, "regs": [], "skipped": []}],
            "hostcheck": hostcheck(), "server": {"role": "primary", "id": "dashboard-host-a1b2c3", "since": NOW - 86400, "holder": None, "renew": None,
                                                 "until": None, "bad": None, "home": None, "say": ""}}


def hostcheck() -> dict:
    """看板服务所在机的体检（hostcheck.view，#166）：开机 31 天、安全更新挂了 4 天、暂停更新 2 天后到期三项成立；隧道还没装。
    sparse 没有这个字段（不是 WSL：不出卡）。"""
    return {"t": NOW - 40, "name": "DASHBOARD-HOST", "role": None, "boot": NOW - 31.2 * 86400, "reboot": None, "pause_until": NOW + 2 * 86400 + 5 * H,
            "au": {"manual": True, "text": "自动下载、通知安装"},
            "updates": {"list": [{"id": "u1", "title": "2026-11 Windows 11 累积更新（KB5050505）", "kb": "5050505", "sec": True, "driver": False, "since": NOW - 4 * 86400},
                                 {"id": "u2", "title": "Realtek 网卡驱动", "kb": "", "sec": False, "driver": True, "since": NOW - 9 * 86400}],
                        "src": "search", "at": NOW - 2 * H, "checked": NOW - 2 * H, "err": None},
            "win_mem": {"free": 23900.0, "total": 32694.0, "since": None}, "wsl_mem": {"avail": 3310.5, "total": 3927.9, "since": None},
            "oom": {"n": 0, "last": None}, "rss": 74.2, "tunnel": {"state": "none", "since": None, "unit": HC.TUNNEL_UNIT}, "err": {},
            "warn": {"updates": "DASHBOARD-HOST 有 2 个 Windows 更新没装（安全更新 1 个），最久的已挂 4.0 天：找个空档手动装，装完按提示重启",
                     "uptime": "DASHBOARD-HOST 已连续开机 31.2 天：找个空档装更新、重启一次",
                     "pause": "DASHBOARD-HOST 的“暂停更新” 11-18 01:52 到期（还剩 2.2 天）：到期后 Windows 会自动装更新并在夜里重启，先把更新改成通知后手动装，或再延长暂停"},
            "lim": dict(HC.LIM), "day": list(HC.DAY), "tick": HC.TICK, "upd_every": HC.UPD_EVERY, "maint": None}


def records() -> list[dict]:
    """给 /records.json 的记录（页面经 RECS 与 LOG 取）：平台操作一次（开始、登录、连接、完成合成一事）、同类同机几条合并（×N）、带项目的、带细节的。"""
    r = lambda t, kind, msg, **kw: {"t": t, "kind": kind, "msg": msg, **kw}  # noqa: E731
    rows = [r(NOW - 60, "collect", "采集出错：探针超时", host="srv-a"),
            r(NOW - 5 * 60, "done", "完成 alpha_train_s0（28 分钟）", host="srv-a", project="proj-a"),
            r(NOW - 6 * 60, "start", "开始 alpha_train_s1（GPU 0）", host="srv-a", project="proj-a"),
            r(NOW - 7 * 60, "start", "开始 beta_eval_s7", host="srv-b", project="proj-b"),
            r(NOW - 30 * 60, "op_done", "续租（OWNER srv-b）完成：ssh 已连上，保活已开", host="srv-b"),
            r(NOW - 31 * 60, "fleet_up", "OWNER（srv-b）恢复", host="srv-b"),
            r(NOW - 33 * 60, "fleet_down", "OWNER（srv-b）掉线：续租中", host="srv-b"),
            r(NOW - 34 * 60, "login", "登录平台"),
            r(NOW - 35 * 60, "op_start", "开始续租：OWNER srv-b", hosts=["srv-b"]),
            r(NOW - 2 * H, "config", "借用 OWNER（srv-b）：没约定时间，只给 proj-b 用", host="srv-b", detail="srv-b 加了 borrow_for = [\"proj-b\"]"),
            r(NOW - 3 * H, "guard_start", "PERSON3（srv-d）保活守护开了保活", host="srv-d", who="PERSON3"),
            r(NOW - 26 * H, "host_down", "srv-a（A6000）掉线：连不上", host="srv-a"),
            r(NOW - 25 * H, "host_up", "srv-a（A6000）恢复", host="srv-a"),
            r(NOW - 27 * H, "lease", "组内服务器租期将到：PERSON3 约剩 18 小时", host="srv-d"),
            r(NOW - 28 * H, "disk", "共享盘只剩 25 GiB"),
            r(NOW - 29 * H, "io_suspect", "读写拥塞嫌疑：alpha_train_s1 已跑 95 分钟，同类历史约 60 分钟（同类近 3 次实际用时）；共享盘此刻 80 ms/次（本机基线 25），5.0 次/秒；"
              "同一物理机上还在跑：srv-c 的 x1", host="srv-a", project="proj-a"),
            r(NOW - 50 * H, "unknown_kind", "worker 新加的事件", host="srv-a")]
    return sorted(rows, key=lambda e: -e["t"])


def overview_evil() -> dict:
    d = copy.deepcopy(overview())
    a, b = d["hosts"]
    a["label"] = "主机" + BAD
    st = a["status"]
    st["running"][0].update(name="任务" + BAD, cmd="python " + BAD, tail=["行" + BAD], log="logs/" + BAD)
    st["waiting"][0].update(name="等" + BAD, why="因" + BAD)
    st["waiting"][4]["queue"] = "jobs/" + BAD
    st["plan"]["lanes"][1]["stuck"] = "卡" + BAD
    st["queues"][0]["root"] = "/data/" + BAD
    st["local"]["jobs"][1].update(job="格" + BAD, files=["/tmp/" + BAD])
    st["gpu_procs"][2]["comm"] = BAD
    st["info"]["press"]["nfs"].update(path="/data/" + BAD, addr=BAD)
    st["info"]["press"]["disk"]["dev"] = BAD
    a["lw"]["scan"]["top"][0][0] = "/root/" + BAD
    b["lw"]["scan"]["top"][0][0] = "/root/" + BAD
    st["running"][0].update(io=BAD, cpus=BAD)
    st["throttle"]["over"][0].update(name="任务" + BAD)
    a["log"].append("日志 " + BAD)
    a["done"][0]["name"] = "完" + BAD
    b["borrow"].update(holder="人" + BAD, why="由" + BAD)
    b["borrow"]["late"][0].update(queue="jobs/" + BAD, why="因" + BAD)
    b["borrow"]["late_do"] = "换" + BAD
    b["borrow"]["late_say"] = "说" + BAD
    b["status"]["kafight"]["who"] = "谁" + BAD
    b["status"]["keepalive"]["stuck"]["why"] = "ka.sh stop 退出码 1：" + BAD
    b["status"]["watchdogs"]["frozen"][0]["cmd"] = "bash " + BAD
    b["status"]["watchdogs"]["seen"][0]["why"] = "因" + BAD
    d["projects"].append({"id": "evil", "title": "邪恶" + BAD, "alerts": 1, "alert_msgs": ["告警" + BAD], "generated": NOW, "no_work_since": None,
                          "lanes": [{"host": "主" + BAD, "root": "/d/" + BAD, "queue": "jobs/" + BAD, "label": "道" + BAD, "unfinished": 1, "run": 0, "returned": None}],
                          "markers": [{"label": "碑" + BAD, "ts": NOW + 2 * 86400}],
                          "cfg": {"path": "/x/" + BAD, "out_dir": "/o/" + BAD, "title": "题" + BAD, "interval": BAD, "refresh": 60, "hide_done_hours": BAD,
                                  "groups": [{"id": "g" + BAD, "label": "组" + BAD, "color": BAD}], "auto_groups": True, "markers": [{"date": "日" + BAD, "label": "碑" + BAD}],
                                  "alerts": {"gpu_idle_util": BAD, "gpu_idle_min": 15, "stale_min": 5, "log_silent_min": 1}, "spec": cfg_spec(), "stale": cfg_stale(120, 5)},
                          "groups": [{"id": "g" + BAD, "label": "组" + BAD, "color": BAD, "auto": False, "total": 1, "done": 0, "failed": 0, "running": 1, "queued": 0},
                                     {"id": "auto:" + BAD, "label": "自" + BAD, "color": BAD, "auto": True, "total": 1, "done": 1, "failed": 0, "running": 0, "queued": 0}],
                          "ungrouped": 1, "archive_block": "因" + BAD})
    d["archived"].append({"id": "归" + BAD, "title": "档" + BAD, "t": NOW - H, "regs": [{"host": "主" + BAD, "root": "/r/" + BAD, "jobs": "j/" + BAD}],
                          "skipped": ["没" + BAD]})
    d["regs"].append({"host": "坏" + BAD, "label": "坏" + BAD, "state": "returned", "returned": NOW - H, "home": "/h/" + BAD, "stale": True, "t": NOW - 60,
                      "queues": [{"root": "/r/" + BAD, "jobs": "jobs/" + BAD, "project": "evil", "stop": "file:" + BAD, "gpus": [BAD], "added": "加" + BAD, "key": BAD}]})
    d["regs"][0]["queues"].append({"root": "/data/" + BAD, "jobs": "q/" + BAD, "project": "evil", "stop": BAD, "gpus": None, "empty": False})
    d["colors"]["evil"] = "#123456"
    d["rebalance"]["proj-a"]["text"] = "挪" + BAD
    d["rebalance"]["proj-b"]["why"] = ["因" + BAD]
    d["sched"]["use_sum"] += 1.0
    d["sched"]["projects"].append({"id": "evil", "person": "人" + BAD, "pk": "e" + BAD, "puse": 1.0, "w": 2, "use": 1.0, "cards": 0, "queued": 1, "waiting": 1, "wait_avg": None, "wait_n": 0, "focus": {"until": None, "hosts": None}, "paused": {"until": None, "hosts": None, "by": "人" + BAD}})
    F = d["fleet"]
    F["servers"][1].update(holder="人" + BAD, job="作业" + BAD)
    F["servers"][1]["sample"]["work"][0].update(label="负载" + BAD, names=["名" + BAD], cwd="/d/" + BAD, chain=["链" + BAD])
    F["servers"][2]["down"] = "断" + BAD
    F["servers"][1]["lw"]["scan"]["top"][1][0] = "/root/" + BAD + "/."
    F["mine"][0]["sample"]["work"][0].update(label="任" + BAD, cwd="/d/" + BAD)
    F["mine"][0]["error"] = "错" + BAD
    F["servers"][2]["guard"]["fail"]["msg"] = "败" + BAD
    F["servers"][-2]["lend"].update(say="池" + BAD, hours="22:00-" + BAD)     # 算力池（#76）：入池那一句与时段、池里等着的清单
    d["pool"]["lists"]["坏|" + BAD] = {"project": "evil", "root": "/r/" + BAD, "jobs": "j/" + BAD, "host": "主" + BAD, "since": NOW - H, "why": "因" + BAD}
    F["unknown"][0]["label"] = "未知" + BAD
    me, owner = F["holders"][-2:]
    me["borrowed"][0]["from"] = "主" + BAD
    owner["lent"][0]["to"] = "借" + BAD
    F["ops"]["pools"]["pool-public"]["nodes"] = ["root@node" + BAD]
    F["ops"]["pools"]["pool-public"]["specs"][0]["specificationName"] = "规格" + BAD
    F["ops"]["platform"]["issues"] = ["核对" + BAD]
    F["ops"]["people"]["NEWBIE"]["note"] = "注" + BAD
    F["ops"]["boot"]["srv-b"].update(image="registry/" + BAD, run_cmd="sh " + BAD)
    d["disk"]["owners"][0].update(name="人" + BAD, dirs=["目" + BAD])
    d["disk"]["owners"][0]["units"][0].update(u="单元" + BAD, top=[["顶" + BAD, 1e9]])
    d["disk"]["ours"]["dirs"][1]["name"] = "环境" + BAD
    d["disk"]["alerts"][0]["msg"] = "盘" + BAD
    d["occ"]["srv-a"][0]["n"] = "占" + BAD
    hc = d["hostcheck"]
    hc.update(name="机" + BAD, au={"manual": False, "text": "方式" + BAD}, err={"win": "错" + BAD}, role="standby",
              tunnel={"state": "down", "since": NOW - 40 * 60, "unit": "单元" + BAD})
    hc["updates"].update(err="搜" + BAD, src="task")
    hc["updates"]["list"][0]["title"] = "更新" + BAD
    hc["warn"]["tunnel"] = "隧道" + BAD
    d["server"] = {"role": "standby", "id": "mac-" + BAD, "since": NOW - 600, "holder": "dashboard-host-" + BAD, "renew": NOW - 120, "until": NOW + 480,
                   "bad": None, "home": "/data/" + BAD, "say": "主是 " + BAD}          # 备用（服务租约，#163）：各页顶上的横幅
    return d


def records_evil() -> list[dict]:
    rows = records()
    rows[0]["msg"] = "记" + BAD
    rows[9]["detail"] = "细" + BAD
    return rows


def overview_sparse() -> dict:
    """字段大量缺省：worker 没部署、worker 停着、本机不在内网；组内服务器与共享盘读不了（overview.collect 的兜底）；没有调度数据。"""
    return {"generated": NOW, "colors": {}, "notices": [], "lan": {"ok": False, "since": NOW - 3600, "checked": NOW}, "wake": [],
            "event_kinds": EVENT_KINDS, "event_cats": EVENT_CATS, "occ": {}, "sched": None,
            "fleet": {"error": "组内服务器数据读不了：boom", "servers": [], "mine": []}, "disk": {"error": "共享盘数据读不了：boom"},
            "hosts": [
                {"id": "srv-c", "label": "srv-c", "error": "ssh: connect to host timed out", "status_error": None, "status": None, "log": [], "done": [],
                 "lan_off": False, "auto": True, "platform": None, "health": {"state": "down", "fails": 5, "since": NOW - 20 * 60, "why": "租期已到"}},
                {"id": "srv-old", "label": "srv-old", "error": None, "status_error": None, "lan_off": False, "auto": True, "platform": {"job": "ME"},
                 "status": {"t": NOW - 1000, "pid": 1, "host": "srv-old", "info": {"gpus": [], "mem": {"limit": 61.0, "used": 2.2, "anon": 0.8, "src": "cgroup"}, "started": NOW - 7200},
                            "running": [], "external": [], "waiting": [], "gpu_procs": []},
                 "log": ["2026-11-01 00:00:00 worker 启动"], "done": [], "clock": int(NOW), "stopped": "2026-11-06 10:00", "age": 1000.0, "worker_up": False,
                 "health": {"state": "ok", "fails": 0, "since": None, "why": None}, "lease_end": NOW + 3600,
                 "renew": {"state": "ready", "why": "都跑完了", "auto": True, "deadline": NOW + 1800, "mode": "idle"}},
                {"id": "srv-lan", "label": "srv-lan", "error": "本机不在内网", "status_error": None, "status": None, "log": [], "done": [], "lan_off": True,
                 "auto": False, "platform": None, "health": {"state": "lan", "fails": 0, "since": None, "why": None}},
                {"id": "srv-bad", "label": "srv-bad", "error": None, "status_error": "status.json 解析不了", "status": None, "log": [], "done": [], "lan_off": False,
                 "auto": True, "platform": None, "health": {"state": "ok", "fails": 0, "since": None, "why": None}}],
            "projects": [{"id": "proj-a", "title": None, "alerts": 0, "alert_msgs": [], "generated": None, "no_work_since": NOW - 86400,
                          "lanes": [], "markers": None}]}


# ---------------- 项目页 ----------------
def project() -> dict:
    """proj-a 的项目快照：两台主机（自用两张卡、借来的一张卡），三条车道（在跑的、暂停的、等待且推演不出的），各种状态的任务。"""
    la, lb, lc = "auto:q1/q1", "auto:q2/q2", "b2/jobs/b2.txt"      # lc 只在 worker 登记里有（#94）：id 是「目录末级名/清单」
    jobs = [
        {"id": "alpha_train_s0", "est_min": 40.0, "status": "done", "end_ts": NOW - 4.5 * H, "start_ts": NOW - 5 * H, "lane": la, "hold": False, "group": "alpha_",
         "dur_min": 28.5, "dur_src": "同类近 1 次 实际/估时 ×0.71", "actual_min": 28.5},
        {"id": "alpha_train_s1", "est_min": 300.0, "status": "running", "start_ts": NOW - 2 * H, "last_line": "step 40/100 loss 0.31", "log_mtime": NOW - 30, "lane": la, "hold": False,
         "group": "alpha_", "dur_min": 270.0, "dur_src": "同 tag alpha-train 近 11 个 实际/估时 ×0.90", "elapsed_min": 120.0,
         "progress": [40, 100], "speed": [[NOW - 50 * 60, 2.1], [NOW - 40 * 60, 2.4], [NOW - 30 * 60, 2.6], [NOW - 20 * 60, 2.5]], "rate_per_min": 2.5,
         "eta_source": "近期速度", "remaining_min": 150.0, "eta_ts": NOW + 2.5 * H},
        {"id": "alpha_train_s2", "est_min": 300.0, "status": "queued", "lane": la, "hold": False, "group": "alpha_", "dur_min": 120.0, "dur_src": "同类近 3 次 实际/估时 ×0.90",
         "plan_start_ts": NOW + 3 * H, "eta_ts": NOW + 5 * H},
        {"id": "alpha_train_s3", "est_min": 300.0, "status": "queued", "lane": la, "hold": False, "group": "alpha_", "dur_min": 120.0, "dur_src": "同类近 3 次 实际/估时 ×0.90",
         "plan_start_ts": NOW + 5 * H, "eta_ts": NOW + 7 * H, "after": [["proj-b", "beta_eval_s7"]],
         "deps": [["proj-a", "alpha_train_s1"], ["proj-b", "beta_eval_s7", "any"]]},                    # 依赖：本项目的画连线，别的项目的只写在悬停里
        {"id": "alpha_probe_s9", "est_min": 10.0, "status": "failed", "end_ts": NOW - 6 * H, "start_ts": NOW - 6.1 * H, "lane": la, "hold": False, "group": "probe_",
         "dur_min": 10.0, "dur_src": "清单估时", "actual_min": 6.0, "killed": 137},
        {"id": "hold_until_data", "est_min": 900.0, "status": "running", "start_ts": NOW - 30 * 60, "lane": lb, "hold": True, "group": None, "dur_min": 90.0,
         "dur_src": "等 alpha_train_s1、proj-b 的 beta_eval_s7 跑完", "deps": [["proj-a", "alpha_train_s1"], ["proj-b", "beta_eval_s7"]],
         "elapsed_min": 30.0, "speed": [], "eta_source": "等 alpha_train_s1、proj-b 的 beta_eval_s7 跑完", "remaining_min": 60.0, "eta_ts": NOW + H, "last_line": "waiting for data"},
        {"id": "probe_s1", "est_min": 10.0, "status": "queued", "lane": lb, "hold": False, "group": "probe_", "dur_min": 10.0, "dur_src": "清单估时"},
        {"id": "alpha_eval_s7", "est_min": 180.0, "status": "queued", "lane": lc, "hold": False, "group": "eval_", "dur_min": None, "dur_src": None},
        {"id": "alpha_eval_s6", "est_min": 180.0, "status": "done", "end_ts": NOW - 30 * H, "start_ts": NOW - 32 * H, "lane": lc, "hold": False, "group": "eval_",
         "dur_min": 120.0, "dur_src": "清单估时", "actual_min": 120.0},
        {"id": "alpha_pool_s1", "est_min": 60.0, "status": "queued", "lane": "pq/jobs/p.txt", "hold": False, "group": None, "dur_min": 60.0, "dur_src": "清单估时"},
        # 算力池放到池借来的 srv-h 上的（#78）：车道跟到那台，在跑与跑完的照常进时间线
        {"id": "alpha_pool_s2", "est_min": 30.0, "status": "done", "end_ts": NOW - 40 * 60, "start_ts": NOW - 70 * 60, "lane": "ph/jobs/h.txt", "hold": False, "group": None,
         "dur_min": 30.0, "dur_src": "清单估时", "actual_min": 30.0},
        {"id": "alpha_pool_s3", "est_min": 60.0, "status": "running", "start_ts": NOW - 35 * 60, "lane": "ph/jobs/h.txt", "hold": False, "group": None, "dur_min": 60.0,
         "dur_src": "清单估时", "elapsed_min": 35.0, "speed": [], "eta_source": "清单估时", "remaining_min": 25.0, "eta_ts": NOW + 25 * 60, "last_line": "epoch 3"}]
    lane = lambda id_, label, host, root, queue, **kw: {  # noqa: E731
        "id": id_, "label": label, "host": host, "type": "gpu_queue", "stale": False, "root": root, "queue": queue, "alive": False, "pid": None,
        "state_dir": root + "/results/_queue", "paused": None, "managed": True, "waiting": None, "held": None, "stuck": None, "fs_skew": -184.1, "started": True,
        "holding": False, "has_hold": False, "overrun": False, "total": 0, "done": 0, "failed": 0, "unfinished": 0, "eta_ts": None, **kw}
    lanes = [lane(la, "车道 1", "srv-a", "/data/me/proj-a/q1", "jobs/q1.txt", total=5, done=1, failed=1, unfinished=3, eta_ts=NOW + 7 * H),
             lane(lb, "车道 2", "srv-a", "/data/me/proj-a/q2", "jobs/q2.txt", paused="临时指令 pause", held="pause", holding=True, has_hold=True, total=2, unfinished=2),
             lane(lc, "车道 1", "srv-b", "/data/me/proj-a/b2", "jobs/b2.txt", waiting="等 beta_eval_s7 跑完", stuck="推演到 7 天后还没轮到", total=2, done=1, unfinished=1),
             # 交给算力池、还没放到哪台的（#24，collect._lane 的 pool）：没有 worker 管，等着放到某台
             lane("pq/jobs/p.txt", "车道 1", "pool", "/data/me/proj-a/pq", "jobs/p.txt", pool=True, waiting="在算力池里等着放到某台：池里借着的服务器都放不下（srv-h：显存不够）", pool_since=NOW - H,
                  fs_skew=None, total=1, unfinished=1),
             lane("ph/jobs/h.txt", "车道 1", "srv-h", "/data/me/proj-a/ph", "jobs/h.txt", alive=True, total=2, done=1, unfinished=1, eta_ts=NOW + 25 * 60)]
    gpu = lambda i, used, util: {"index": i, "name": "NVIDIA RTX A6000", "util": util, "mem_used": used, "mem_total": 46068.0, "temp": 51.0, "power": 87.5, "power_limit": 300.0}  # noqa: E731
    hosts = [{"id": "srv-a", "label": "srv-a（A6000）", "ok": True, "error": None, "borrow": None, "gpus": [gpu(0, 21000.0, 95.0), gpu(1, 900.0, 3.0)],
              "gpu_procs": [{"pid": 2001, "mem": 20500.0, "cmd": "python train.py"}], "cgroup": {"cpu_usage_ns": 1, "cpu_quota": 17.0, "mem_used": 2.8e10, "mem_limit": 1.3e11, "mem_rss": 1.1e9},
              "loadavg": [18.8, 15.8, 14.8], "cpu_pct": 6.0,
              "mounts": [{"path": "/tmp", "label": "容器 /tmp", "total_gb": 805.3, "avail_gb": 787.6, "used_pct": 2.2, "min_gb": 50}],
              "checks": [{"id": "keepalive", "label": "GPU 保活在跑", "state": "fail", "out": "", "detail": ["STOPPED 进程组 55336"]}],
              # 吃满程度（工作台那一轮 overview.gpu_busy 算好的，#51）；srv-b 没有：退回探针此刻的值
              "busy": [{"i": 0, "now": 95.0, "m10": 88.4, "h1": 72.3, "cov": 3600, "d1": 64.0, "d1cov": 86000},
                       {"i": 1, "now": 0, "m10": 0.0, "h1": 4.2, "cov": 3600, "ka": 99.0, "d1": 1.5, "d1cov": 86000}]},
             {"id": "srv-b", "label": "srv-b", "ok": True, "error": None, "gpus": [gpu(0, 6754.0, 90.0)], "gpu_procs": [], "cgroup": {"cpu_quota": 7.0, "mem_used": 2e9, "mem_limit": 6.5e10, "mem_rss": 1e9},
              "loadavg": [1.0, 1.0, 1.0], "cpu_pct": 2.0, "mounts": [], "checks": [],
              "borrow": {"holder": "OWNER", "since": NOW - 16 * H, "until": None, "for": ["proj-a"], "light": True, "cutoff": NOW + 6 * 86400, "why": "OWNER 的租期到期，提前 1 小时",
                         "returning": None, "now": False, "back": [], "tight": None, "overdue": False, "keep": {"vram": 2.0, "ram": 14.5, "cpu": 1.0, "bump": 1.0, "hold": False}, "swap": None}},
             # 算力池借来的（#25、#78）：池放过去的清单在这台上跑，项目「主机」里有它
             {"id": "srv-h", "label": "srv-h（池 · PERSON7）", "ok": True, "error": None, "gpus": [gpu(0, 12000.0, 80.0)], "busy": [{"i": 0, "now": 80.0, "m10": 80.0, "h1": 80.0, "cov": 3600, "d1": 40.0, "d1cov": 7200}],
              "gpu_procs": [], "cgroup": {"cpu_quota": 8.0, "mem_used": 3e9, "mem_limit": 6.5e10, "mem_rss": 1e9},
              "loadavg": [1.0, 1.0, 1.0], "cpu_pct": 3.0, "mounts": [], "checks": [],
              "borrow": {"holder": "PERSON7", "since": NOW - 2 * H, "until": NOW + 8 * H, "for": None, "light": False, "cutoff": NOW + 8 * H, "why": "入池时段结束",
                         "returning": None, "now": False, "back": [], "tight": None, "overdue": False, "keep": {"vram": 2.0, "ram": 4.0, "cpu": 1.0, "bump": 1.0, "hold": False}, "swap": None, "pool": True}},
             {"id": "pool", "label": "算力池", "pool": True, "ok": True, "error": None}]        # 算力池（collect._hosts 的 pool，#24）：一张小卡
    hist = []
    for k in range(144):
        t = NOW - (144 - k) * 600
        hist.append({"t": t, "host": "srv-a", "ok": True, "util": [90.0 - k % 7, 3.0], **({"busy": [80.0 - k % 9, 0.0]} if k >= 72 else {}), "mem": [20000.0 + k, 900.0], "cpu_pct": 5.0 + k % 4, "ram": 1.1e9, "disk": {"/tmp": 787.6 - k * 0.01}})
        hist.append({"t": t, "host": "srv-b", "ok": True, "util": [85.0], "mem": [6700.0], "cpu_pct": 2.0, "ram": 1e9})
    groups = [{"id": "alpha_", "label": "alpha_*", "color": "#7F77DD", "status": "运行中", "auto": True, "done_when": None, "total": 4, "done": 1, "failed": 0, "running": 1, "queued": 2,
               "ratios": [{"cls": "alpha_train", "k": 0.9, "src": "同类近 3 次 实际/估时 ×0.90", "open": True}], "eta_ts": NOW + 7 * H},
              {"id": "probe_", "label": "probe_*", "color": "#D85A30", "status": "有失败", "auto": True, "done_when": None, "total": 2, "done": 0, "failed": 1, "running": 0, "queued": 1, "ratios": [], "eta_ts": None},
              {"id": "eval_", "label": "评估", "color": "#1D9E75", "status": "跑完待汇总", "auto": False, "done_when": {"file": "PLAN.md", "pattern": r"\[x\] 评估"}, "total": 2, "done": 1, "failed": 0, "running": 0, "queued": 1,
               "ratios": [{"cls": "alpha_eval", "k": 0.67, "src": "同类近 1 次 实际/估时 ×0.67", "open": False}], "eta_ts": None},
              {"id": "old_", "label": "old_*", "color": "#888780", "status": "完成", "auto": True, "done_when": None, "total": 3, "done": 3, "failed": 0, "running": 0, "queued": 0, "ratios": [], "eta_ts": None}]
    return {"schema": 2, "id": "proj-a", "title": "Alpha 实验", "generated": NOW, "refresh": 60, "interval": 120,
            "hosts": hosts, "lanes": lanes, "jobs": jobs, "groups": groups,
            # 告警给谁看（collect.alert 的 to，#142）：失败、空转、挪法建议只给实验会话（页面上折叠）；借用约定挡住两边都要（page 给用户）
            "alerts": [{"key": "failed:alpha_probe_s9", "to": "agent", "msg": "任务被终止（退出码 137）：alpha_probe_s9（srv-a 车道 1）；要重跑请删除 /data/me/proj-a/q1/results/_queue/alpha_probe_s9.failed"},
                       {"key": "gpu_idle:srv-b:1793770000", "to": "agent", "msg": "srv-b：还有 1 个任务未完成，GPU 利用率已 15 分钟低于 5%（车道 1 等待中：等 beta_eval_s7 跑完）"},
                       {"key": "borrow_cut:b3:srv-b:1793700000", "to": "both", "msg": "srv-b（借 OWNER） 车道 2 还有 2 个任务不会在这台放行：约定归还 11-14 09:00 前跑不完（估 330 分钟）；"
                        "换到别的在用主机（labmon worker add）；截止取的是 OWNER 的租期到期，提前 1 小时，延长约定推不迟它",
                        "page": "srv-b（借 OWNER） 车道 2 还有 2 个任务不会在这台放行：约定归还 11-14 09:00 前跑不完（估 330 分钟）；"
                        "截止取的是 OWNER 的租期到期，提前 1 小时，延长约定推不迟它，由实验会话换到别的在用主机"},
                       {"key": REBAL["key"], "to": "agent", "msg": REBAL["text"]}],          # collect._rebalance 出的：整句只给实验会话（#40）
            "markers": [{"date": "2026-11-13 19:59", "label": "投稿截止", "ts": NOW + 3 * 86400}, {"date": "2026-11-01 10:00", "label": "开题", "ts": NOW - 6 * 86400}],
            "spans": [[NOW - 40 * H, NOW - 30 * H], [NOW - 29 * H, NOW]], "util_hourly": {"srv-a": [[NOW - k * 3600, 80.0 + k % 9] for k in range(30 * 24, 0, -1)], "srv-b": [[NOW - 3600, 85.0]]},
            "history": hist, "all_done_ts": NOW + 7 * H, "gpu_mine": {"util": 63.2, "card_s": 52200, "n": 4},
            "attempts": [{"name": "alpha_probe_s9", "lane": la, "t0": NOW - 6.1 * H, "t1": NOW - 6 * H}, {"name": "alpha_probe_s9", "lane": la, "t0": NOW - 6.3 * H, "t1": NOW - 6.2 * H}],
            "no_work_since": None, "thresholds": {"stale_s": stale_limit(120, 5)}, "rebalance": dict(REBAL),
            # 本项目的存储（disk.project_storage，#53）：项目根下的一级目录（「.」是根下的文件）、删掉的、近来写得多的任务
            "storage": {"roots": ["/data/share/me/proj-a"], "b": 5.7e10, "d24": 3.5e10, "d7": 4.1e10, "pending": 1, "burst": True,
                        "dirs": [{"path": "/data/share/me/proj-a/cache", "name": "cache", "b": 4.2e10, "d24": 3.3e10, "d7": 3.9e10, "n": 120, "m": NOW - 600, "t": NOW - 3000,
                                  "top": [["feats", 4.0e10, NOW - 600]]},
                                 {"path": "/data/share/me/proj-a/out", "name": "out", "b": 1.4e10, "d24": 2.1e9, "d7": 2.1e9, "n": 40, "m": NOW - 900, "t": NOW - 3000, "top": []},
                                 {"path": "/data/share/me/proj-a/.", "name": ".", "b": 1.0e9, "d24": 0, "d7": 0, "n": 3, "m": NOW - 86400, "t": NOW - 3000, "top": []}],
                        "gone": [{"path": "/data/share/me/proj-a/tmp_old", "name": "tmp_old", "d24": 0, "d7": -5e9}],
                        "jobs": [{"name": "alpha_train_s0", "host": "srv-a", "t0": NOW - 5 * H, "t1": NOW - 4.5 * H, "st": "done", "b": 7.4e9,
                                  "top": [["cache", 6.9e9], ["out", 5e8]], "from": NOW - 5.2 * H, "to": NOW - 4.4 * H, "pending": 0, "wait": False, "with": 0, "big": True},
                                 {"name": "alpha_probe_s9", "host": "srv-a", "t0": NOW - 6.1 * H, "t1": NOW - 6 * H, "st": "failed", "b": 2e8,
                                  "top": [["out", 2e8]], "from": None, "to": None, "pending": 1, "wait": True, "with": 0, "big": False}],
                        "at": NOW - 3000, "free": 2.2e11, "total": 1.1e12, "eta": 6.3, "eta7": 9.1, "warn_gb": 30, "burst_gb": 30, "bigwrite_gb": 5,
                        "local": {"srv-a": {"cache": 1.5e10, "scratch": 2e9}, "srv-b": {"cache": 1.2e10, "scratch": 0}}}}


def project_events() -> dict:
    """项目页经 /p/<id>/events.json 取的：主机 30 天事件（含带 started 与不带的 container_new、临时指令事件）、同机别的项目的占用、存储。"""
    e = lambda t, kind, msg, **kw: {"t": t, "kind": kind, "msg": msg, **kw}  # noqa: E731
    ev_a = [e(NOW - 10 * H, "host_down", "srv-a（A6000）掉线：连不上", host="srv-a"),
            e(NOW - 9.5 * H, "host_up", "srv-a（A6000）恢复", host="srv-a"),
            e(NOW - 9.4 * H, "container_new", "srv-a（A6000） 换了新容器（11-07 02:30 启动）", host="srv-a", started=NOW - 9.45 * H),
            e(NOW - 20 * H, "container_new", "srv-a（A6000） 换了新容器（11-06 15:52 启动）", host="srv-a"),            # 旧事件：没有 started，不画
            e(NOW - 8 * H, "renew", "预备续租：不再放行新任务，正在跑的跑完后开保活", host="srv-a"),
            e(NOW - 7.5 * H, "override", "临时指令：暂停 proj-a", host="srv-a", act="add", okind="pause", target="proj-a", until=None, at=NOW - 7.5 * H),
            e(NOW - 7 * H, "override", "撤销临时指令：pause proj-a", host="srv-a", act="rm", okind="pause", target="proj-a"),
            e(NOW - 5 * H, "override", "临时指令：drain 主机", host="srv-a", act="add", okind="drain", target=None, until=NOW - 4 * H, at=NOW - 5 * H),
            e(NOW - 4 * H, "override", "临时指令到期：drain 主机", host="srv-a", act="expire", okind="drain", target=None),
            e(NOW - 5.5 * H, "qend", "队列结束：jobs/q0.txt 清单中已无待办任务", host="srv-a", project="proj-a", queue="jobs/q1.txt", root="/data/me/proj-a/q1"),
            e(NOW - 4.6 * H, "wait", "等待 alpha_train_s1：显存不够", host="srv-a", project="proj-a", queue="jobs/q1.txt", root="/data/me/proj-a/q1", name="alpha_train_s1", why="显存不够"),
            e(NOW - 3 * H, "relaunch", "srv-a：worker 不在，已重新拉起", host="srv-a"),
            e(NOW - 2.5 * H, "void", "作废 beta_x：独占期间同卡出现别的进程", host="srv-a"),
            e(NOW - 1 * H, "interrupt", "中断 alpha_probe_s9", host="srv-a")]
    ev_b = [e(NOW - 86400, "borrow", "借来 OWNER（srv-b）：没约定时间，只给 proj-a 用", host="srv-b", who="OWNER"),
            e(NOW - 86400 + 60, "override", "临时指令：续租截止 borrow", host="srv-b", act="add", okind="deadline", target="borrow", until=NOW + 6 * 86400, at=NOW + 6 * 86400)]
    occ = {"srv-a": [{"p": "proj-b", "n": "beta_eval_s0", "lane": "车道 2", "t0": NOW - 7 * H, "t1": NOW - 6 * H, "g": [0], "st": "failed"},
                     {"p": "proj-b", "n": "hold_until_data", "lane": "车道 2", "t0": NOW - 30 * 60, "t1": NOW + H, "g": [1], "st": "run"}],
           "srv-b": [{"p": "proj-b", "n": "beta_eval_s7", "lane": "车道 1", "t0": NOW - H, "t1": NOW + 2 * H, "g": [0], "st": "run"},
                     {"p": "proj-b", "n": "beta_eval_s8", "lane": "车道 1", "t0": NOW + 2 * H, "t1": NOW + 4 * H, "g": [0], "st": "plan", "after": [["proj-a", "alpha_eval_s7"]]}]}
    return {"colors": {"proj-a": "#7F77DD", "proj-b": "#D85A30"}, "events": {"srv-a": ev_a, "srv-b": ev_b}, "occ": occ}


def project_evil() -> dict:
    d = copy.deepcopy(project())
    d["title"] = "题" + BAD
    d["jobs"][1].update(id="任务" + BAD, last_line="行" + BAD, eta_source="据" + BAD, dur_src="源" + BAD)
    d["jobs"][5].update(last_line="等" + BAD, dur_src="源" + BAD, eta_source="据" + BAD)
    d["groups"][0]["label"] = "组" + BAD
    d["groups"][2]["done_when"]["file"] = "文" + BAD
    d["hosts"][0]["label"] = "主机" + BAD
    d["hosts"][0]["gpu_procs"][0]["cmd"] = "命令" + BAD
    d["hosts"][0]["checks"][0].update(label="检" + BAD, out="出" + BAD)
    d["hosts"][0]["mounts"][0]["label"] = "盘" + BAD
    d["hosts"][1]["borrow"]["holder"] = "人" + BAD
    d["lanes"][0]["label"] = "道" + BAD
    d["lanes"][1]["paused"] = "停" + BAD
    d["lanes"][2].update(waiting="等" + BAD, stuck="卡" + BAD)
    d["alerts"][0]["msg"] = "告警" + BAD
    d["markers"][0]["label"] = "碑" + BAD
    d["attempts"][0]["name"] = "试" + BAD
    d["rebalance"]["text"] = d["alerts"][3]["msg"] = "挪" + BAD
    d["storage"]["dirs"][0]["name"] = "目录" + BAD
    d["storage"]["roots"] = ["/data/share/" + BAD]
    d["storage"]["jobs"][0].update(name="写" + BAD, top=[["顶" + BAD, 6.9e9]])
    return d


def project_longq() -> dict:
    """srv-a 两条车道的清单名都长（gpu_… / cpu_…，区分它们的在末尾），车道 1 近一天跑过四个计划项（#108：左栏放不下时车道名怎么截）。"""
    d = copy.deepcopy(project())
    d["lanes"][0]["queue"], d["lanes"][1]["queue"] = "jobs/gpu_srvx_sweep_part07.txt", "jobs/cpu_srvx_sweep_part08.txt"
    la = d["lanes"][0]["id"]
    d["jobs"] += [{"id": f"{g}x_s{k}", "est_min": 30.0, "status": "done", "start_ts": NOW - (8 + k) * H, "end_ts": NOW - (7.5 + k) * H, "lane": la, "hold": False, "group": g,
                   "dur_min": 30.0, "dur_src": "清单估时", "actual_min": 30.0} for k, g in enumerate(["eval_", "old_"])]
    return d


def project_moved() -> dict:
    """#100：借 OWNER 车道 1（b2 清单）原先登记在 srv-a，搬到 srv-b 时 srv-a 正在跑 alpha_eval_s5：车道按登记在 srv-b，
    这一格仍在 srv-a 上跑完（任务的 host）；还有一格在已不属于本项目的 srv-z 上跑完（主机表里没有它）。"""
    d = copy.deepcopy(project())
    lc = d["lanes"][2]["id"]
    run = {"est_min": 180.0, "status": "running", "lane": lc, "hold": False, "group": "eval_", "dur_min": 180.0, "dur_src": "清单估时", "speed": [],
           "eta_source": "清单估时", "last_line": "eval 3/9"}
    d["jobs"] += [dict(run, id="alpha_eval_s5", host="srv-a", start_ts=NOW - H, elapsed_min=60.0, remaining_min=120.0, eta_ts=NOW + 2 * H),
                  dict(run, id="alpha_eval_s4", host="srv-z", start_ts=NOW - 2 * H, elapsed_min=120.0, remaining_min=60.0, eta_ts=NOW + H)]
    d["lanes"][2].update(total=4, unfinished=3)
    return d


def project_pool_back() -> dict:
    """#99：池车道 pq（还在池里待放 alpha_pool_s1）先前被放到池借来的 srv-y 上跑过，srv-y 还掉、清单回到池里：跑完的 alpha_pool_s0、
    失败的尝试按用量记录带着 srv-y（collect._attempts），srv-y 那一格还在跑完的 alpha_pool_s4 带着 current 里放行的主机（#100）。"""
    d = copy.deepcopy(project())
    lp = "pq/jobs/p.txt"
    d["jobs"] += [{"id": "alpha_pool_s0", "est_min": 60.0, "status": "done", "start_ts": NOW - 5 * H, "end_ts": NOW - 4 * H, "lane": lp, "hold": False, "group": None,
                   "dur_min": 60.0, "dur_src": "清单估时", "actual_min": 60.0, "host": "srv-y"},
                  {"id": "alpha_pool_s4", "est_min": 60.0, "status": "running", "start_ts": NOW - 30 * 60, "lane": lp, "hold": False, "group": None, "dur_min": 60.0,
                   "dur_src": "清单估时", "elapsed_min": 30.0, "speed": [], "eta_source": "清单估时", "remaining_min": 30.0, "eta_ts": NOW + 30 * 60, "last_line": "epoch 1",
                   "host": "srv-y"}]
    d["attempts"].append({"name": "alpha_pool_s0", "lane": lp, "t0": NOW - 5.5 * H, "t1": NOW - 5.4 * H, "host": "srv-y"})
    d["lanes"][3].update(total=3, done=1, unfinished=2)
    return d


def project_overdue() -> dict:
    """借来的 srv-b 已过约定时间（半小时前）、在跑的那格还没跑完（还要 15 分钟）：到点时看板自己下了 drain，跑完就还（#103）。"""
    d = copy.deepcopy(project())
    cut = NOW - 30 * 60
    d["hosts"][1]["borrow"].update(until=cut, cutoff=cut, why="约定的时间", returning=round(cut + 90), overdue=True)
    j = next(j for j in d["jobs"] if j["id"] == "alpha_eval_s7")
    j.update(status="running", start_ts=NOW - 2 * H, elapsed_min=120.0, speed=[], eta_source="清单估时", remaining_min=15.0, eta_ts=NOW + 15 * 60, last_line="eval 9/10")
    return d


def project_events_overdue() -> dict:
    """project_overdue 的事件：约定归还的截止指令（worker 记的 until 为空、at 是约定时刻），到点后那一轮 borrow.check 下的 drain（不记要还的事件）。"""
    d = project_events()
    e = lambda t, kind, msg, **kw: {"t": t, "kind": kind, "msg": msg, "host": "srv-b", **kw}  # noqa: E731
    cut = NOW - 30 * 60
    d["events"]["srv-b"] = [e(NOW - 16 * H, "borrow", "借来 OWNER（srv-b）：约定今天还，只给 proj-a 用", who="OWNER"),
                            e(NOW - 16 * H + 60, "override", "临时指令：续租截止 borrow", act="add", okind="deadline", target="borrow", until=None, at=cut),
                            e(cut + 90, "override", "临时指令：drain 主机", act="add", okind="drain", target=None, until=None, at=cut + 90)]
    return d


def project_events_evil() -> dict:
    d = project_events()
    d["events"]["srv-a"][0]["msg"] = "事" + BAD
    d["events"]["srv-a"][10]["why"] = "因" + BAD
    d["occ"]["srv-a"][0]["n"] = "占" + BAD
    return d


def project_sparse() -> dict:
    """探针失败、没有上次数据的主机，已归还的主机，缺清单的车道（raw 为空），没有任务、没有计划项、没有本机、暂时没实验。"""
    return {"schema": 2, "id": "proj-a", "title": "Alpha 实验", "generated": NOW, "refresh": 60, "interval": 120,
            "hosts": [{"id": "srv-c", "label": "srv-c", "ok": False, "error": "探针超时", "borrow": None, "stale": False, "last_ok": None},
                      {"id": "srv-r", "label": "srv-r", "ok": False, "returned": NOW - 6 * 86400, "error": "已归还"}],
            "lanes": [{"id": "auto:q9/q9", "label": "车道 1", "host": "srv-c", "type": "gpu_queue", "stale": True, "root": "/data/me/proj-a/q9", "queue": "jobs/q9.txt",
                       "missing": True, "alive": False, "total": 0, "done": 0, "unfinished": 0},
                      {"id": "auto:r1/r1", "label": "车道 1", "host": "srv-r", "type": "gpu_queue", "stale": False, "root": "/data/me/proj-a/r1", "queue": "jobs/r1.txt",
                       "returned": NOW - 6 * 86400, "read_via": None, "frozen_at": NOW - 6.1 * 86400, "alive": False, "pid": None, "state_dir": None, "paused": None, "managed": False,
                       "waiting": None, "held": None, "stuck": None, "started": True, "holding": False, "has_hold": False, "overrun": False, "total": 1, "done": 0, "failed": 0,
                       "unfinished": 1, "eta_ts": None}],
            "jobs": [{"id": "r_job", "est_min": None, "status": "queued", "lane": "auto:r1/r1", "hold": False, "group": None, "dur_min": None, "dur_src": None}],
            "groups": [], "alerts": [{"key": "returned_unfinished:auto:r1/r1:srv-r:1793600000", "to": "both", "msg": "srv-r 已归还，车道 1 还有 1 个任务没跑完：把这条清单 labmon worker add 到别的在用主机（车道跟着登记走，monitor.toml 不用改）",
                                  "page": "srv-r 已归还，车道 1 还有 1 个任务没跑完：可在项目的「管理」里换到别的主机，或由实验会话登记到别的主机"}], "markers": [],
            "spans": [], "util_hourly": {}, "history": [], "all_done_ts": None, "attempts": [], "no_work_since": NOW - 86400,
            "thresholds": {"stale_s": stale_limit(120, 5)},
            "rebalance": {"why": ["srv-c 接不了 gamma_big：一直排不上，放不下（要的比卡的显存还大）"], "from": None}}   # 只有挪不了的原因：灰「⇄」


# 高并行、任务短（Issue #49，构造高并发样例）：两台主机 24 条车道，每条近 1 小时跑完几个 5–15 分钟的任务，在跑一个，
# 每隔两条排一个；最晚的预计结束正好在 NOW + 42 分钟（DENSE_END）。旧的近景固定 −3/+12 小时，这批只占视图左边 1/4
DENSE_END = NOW + 42 * 60


def _dense_jobs() -> list[tuple]:
    """[(车道序号, 主机, 卡, 任务名, 状态 done|running|queued, 开始, 结束)]"""
    out = []
    for i in range(24):
        host, g = ("srv-a", i % 2) if i < 12 else ("srv-b", 0)
        t, k = NOW - 60 * 60 + (i % 4) * 60, 0
        while True:
            d = (5 + (i * 7 + k * 3) % 11) * 60
            if t + d > NOW - 120:
                break
            out.append((i, host, g, f"p1_d{i:02d}_s{k}", "done", t, t + d)); t += d; k += 1
        end = NOW + (5 + i % 15) * 60
        out.append((i, host, g, f"p1_d{i:02d}_s{k}", "running", t, end))
        if i % 2 == 0:
            q1 = min(DENSE_END, end + (6 + i % 9) * 60) if i else DENSE_END
            out.append((i, host, g, f"p1_d{i:02d}_s{k + 1}", "queued", end, q1))
    return out


def project_dense() -> dict:
    d = project()
    lane0 = d["lanes"][0]
    d["lanes"], d["jobs"] = [], []
    for i in range(24):
        host = "srv-a" if i < 12 else "srv-b"
        mine = [x for x in _dense_jobs() if x[0] == i]
        d["lanes"].append({**copy.deepcopy(lane0), "id": f"auto:d{i}/d{i}", "label": f"车道 {i % 12 + 1}", "host": host, "root": f"/data/me/proj-a/d{i}",
                           "queue": f"jobs/d{i}.txt", "total": len(mine), "done": sum(x[4] == "done" for x in mine), "failed": 0,
                           "unfinished": sum(x[4] != "done" for x in mine), "eta_ts": max(x[6] for x in mine)})
        for _, _, _, name, st, t0, t1 in mine:
            j = {"id": name, "est_min": 10.0, "status": st, "lane": f"auto:d{i}/d{i}", "hold": False, "group": "alpha_", "dur_min": round((t1 - t0) / 60, 1), "dur_src": "清单估时"}
            j.update({"done": {"start_ts": t0, "end_ts": t1, "actual_min": round((t1 - t0) / 60, 1)},
                      "running": {"start_ts": t0, "eta_ts": t1, "elapsed_min": round((NOW - t0) / 60, 1), "remaining_min": round((t1 - NOW) / 60, 1),
                                  "eta_source": "估时", "speed": [], "last_line": "step"},
                      "queued": {"plan_start_ts": t0, "eta_ts": t1}}[st])
            d["jobs"].append(j)
    d["attempts"], d["all_done_ts"] = [], DENSE_END
    return d


def project_deps() -> dict:
    """依赖多、跨两台主机（#111）：srv-a、srv-b 各 4 条车道。车道 0（srv-a）上 dp_prep → dp_prep2（同车道相连）；其余 7 条车道各一个 dp_tr<i>
    等 dp_prep2（又多写一个等 dp_prep：传递边），后面同车道的 dp_ev<i> 等 dp_tr<i>（同车道）与 dp_prep2（传递边）；车道 0 上的 dp_merge 等全部 dp_ev 与 dp_prep2，
    srv-b 末条车道的 dp_report 等 dp_merge；另有一条与它们无关的 dp_side_a（srv-b）→ dp_side_b（srv-a）。共 39 条 after=，关键的是 prep2→tr 7 条、ev→merge 7 条、
    merge→report、side_a→side_b。"""
    d = project()
    lane0 = d["lanes"][0]
    lid = lambda i: f"auto:dp{i}/dp{i}"  # noqa: E731
    host = lambda i: "srv-a" if i < 4 else "srv-b"  # noqa: E731
    span: dict[str, tuple] = {}
    jobs: list[dict] = []

    def job(name: str, i: int, t0: float, mins: float, after: list[str], group: str) -> None:
        t1 = t0 + mins * 60
        span[name] = (t0, t1)
        st = "done" if t1 <= NOW else "running" if t0 <= NOW else "queued"
        j = {"id": name, "est_min": mins, "status": st, "lane": lid(i), "hold": False, "group": group, "dur_min": mins, "dur_src": "清单估时",
             "deps": [["proj-a", a] for a in after]}
        j.update({"done": {"start_ts": t0, "end_ts": t1, "actual_min": mins},
                  "running": {"start_ts": t0, "eta_ts": t1, "elapsed_min": round((NOW - t0) / 60, 1), "remaining_min": round((t1 - NOW) / 60, 1),
                              "eta_source": "估时", "speed": [], "last_line": "step"},
                  "queued": {"plan_start_ts": t0, "eta_ts": t1}}[st])
        jobs.append(j)

    job("dp_side_a", 5, NOW - 9 * H, 60, [], "probe_")
    job("dp_side_b", 2, NOW - 7.5 * H, 60, ["dp_side_a"], "probe_")
    job("dp_prep", 0, NOW - 6 * H, 60, [], "eval_")
    job("dp_prep2", 0, NOW - 5 * H, 30, ["dp_prep"], "eval_")
    for i in range(1, 8):
        job(f"dp_tr{i}", i, span["dp_prep2"][1] + i * 300, 90 + 20 * i, ["dp_prep2", "dp_prep"], "alpha_")
        job(f"dp_ev{i}", i, span[f"dp_tr{i}"][1], 40, [f"dp_tr{i}", "dp_prep2"], "eval_")
    t = max(span[f"dp_ev{i}"][1] for i in range(1, 8))
    job("dp_merge", 0, t, 45, [f"dp_ev{i}" for i in range(1, 8)] + ["dp_prep2"], "alpha_")
    job("dp_report", 7, span["dp_merge"][1], 20, ["dp_merge"], "eval_")
    lanes = []
    for i in range(8):
        mine = [j for j in jobs if j["lane"] == lid(i)]
        lanes.append({**copy.deepcopy(lane0), "id": lid(i), "label": f"车道 {i % 4 + 1}", "host": host(i), "root": f"/data/me/proj-a/dp{i}", "queue": f"jobs/dp{i}.txt",
                      "total": len(mine), "done": sum(j["status"] == "done" for j in mine), "failed": 0, "unfinished": sum(j["status"] != "done" for j in mine),
                      "eta_ts": max(span[j["id"]][1] for j in mine), "fs_skew": None})
    d.update(lanes=lanes, jobs=jobs, attempts=[], all_done_ts=span["dp_report"][1], alerts=d["alerts"][:1], rebalance=None)
    return d


MANY_COLORS = ["#7F77DD", "#1D9E75", "#D85A30", "#D4537E", "#BA7517", "#639922", "#378ADD", "#888780"]


def project_many() -> dict:
    """大量小规模并行（Issue #69，仿 11-09 14:00 的真项目：30 个计划项、300 多个任务、35 条车道、两台主机同时跑约 10 个）。
    30 个计划项里 26 个今天之内跑完（不到一天，旧规则不收起；跑完的先后与编号、列表顺序都不同，配色 8 色轮着用、有重复），4 个没完：两个在跑、一个排队、一个有失败；
    在跑 10 个（有进度与速度的、超估时的、按估时的）加一个占位任务；两条车道没在跑（一条等待、一条暂停），其余车道都跑完了。"""
    d = project()
    lane0 = d["lanes"][0]
    gid = lambda k: f"p1_v{k:02d}_"  # noqa: E731
    jobs: list[dict] = []
    job = lambda name, k, lane, st, **kw: {"id": name, "est_min": 10.0, "status": st, "lane": f"auto:m{lane}/m{lane}", "hold": False,  # noqa: E731
                                           "group": gid(k) if k is not None else None, "dur_min": 10.0, "dur_src": "清单估时", **kw}
    for k in range(26):                     # 跑完的：每项 11 个，近 8 小时里陆续跑完；跑完的先后与编号错开（第 k 项排第 k*7%26），配色按编号轮、8 色重复（#115）
        for m in range(11):
            t1 = NOW - (26 - k * 7 % 26) * 15 * 60 - m * 60 - 300
            jobs.append(job(f"{gid(k)}s{m}", k, (k * 11 + m) % 35, "done", start_ts=t1 - 600, end_ts=t1, actual_min=10.0))
    for i in range(10):                     # 在跑：v26 六个、v27 四个，车道 0–9
        k, t0, eta = 26 if i < 6 else 27, NOW - (5 + i) * 60, NOW + (4 + 3 * i) * 60
        kw = {"start_ts": t0, "eta_ts": eta, "elapsed_min": (NOW - t0) / 60, "remaining_min": (eta - NOW) / 60, "eta_source": "估时", "speed": [], "last_line": f"epoch {i}"}
        if i % 3 == 0:
            kw.update(progress=[10 + i, 40], eta_source="近期速度", rate_per_min=1.5,
                      speed=[[NOW - 4 * 60, 1.2], [NOW - 3 * 60, 1.4], [NOW - 2 * 60, 1.6], [NOW - 60, 1.5]])
        if i == 7:
            kw.update(eta_ts=NOW + 10 * 60, remaining_min=10.0, overrun_min=6.0, dur_min=8.0)
        jobs.append(job(f"{gid(k)}s{i}", k, i, "running", **kw))
    for i in range(4):                      # 排队：v27 两个、v28 两个，排在在跑的后面
        k = 27 if i < 2 else 28
        jobs.append(job(f"{gid(k)}s{10 + i}", k, i, "queued", plan_start_ts=NOW + (4 + 3 * i) * 60, eta_ts=NOW + (14 + 3 * i) * 60))
    jobs.append(job(f"{gid(29)}s0", 29, 13, "failed", start_ts=NOW - 2 * H, end_ts=NOW - 1.9 * H, actual_min=6.0))
    jobs.append(job(f"{gid(29)}s1", 29, 11, "queued"))
    jobs.append(job("hold_merge", None, 10, "running", hold=True, start_ts=NOW - 20 * 60, elapsed_min=20.0, est_min=60.0, dur_min=60.0, eta_ts=NOW + 40 * 60,
                    remaining_min=40.0, eta_source="暂停（等条件放行，时长按估时猜）", speed=[], last_line="waiting"))
    jobs.append(job(f"{gid(28)}s9", 28, 12, "queued"))
    lanes = []
    for i in range(35):
        mine = [j for j in jobs if j["lane"] == f"auto:m{i}/m{i}"]
        un = sum(j["status"] in ("running", "queued") for j in mine)
        lanes.append({**copy.deepcopy(lane0), "id": f"auto:m{i}/m{i}", "label": f"车道 {i % 20 + 1}", "host": "srv-a" if i < 20 else "srv-b",
                      "root": f"/data/me/proj-a/m{i}", "queue": f"jobs/m{i}.txt", "total": len(mine), "done": sum(j["status"] == "done" for j in mine),
                      "failed": sum(j["status"] == "failed" for j in mine), "unfinished": un, "fs_skew": None,
                      "eta_ts": max([j.get("eta_ts") or 0 for j in mine if j["status"] != "done"], default=0) or None,
                      "holding": i == 10, "has_hold": i == 10,
                      **({"waiting": "显存不够：GPU-aaaa-0 余 3 GiB，要 5 GiB"} if i == 11 else {}), **({"paused": "临时指令 pause", "held": "pause"} if i == 12 else {})})
    groups = []
    for k in range(30):
        mine = [j for j in jobs if j["group"] == gid(k)]
        n = lambda s: sum(j["status"] == s for j in mine)  # noqa: E731
        st = "完成" if k < 26 else "运行中" if k < 28 else "排队中" if k == 28 else "有失败"
        groups.append({"id": gid(k), "label": f"{gid(k)}*", "color": MANY_COLORS[k % len(MANY_COLORS)], "status": st, "auto": True, "done_when": None,
                       "total": len(mine), "done": n("done"), "failed": n("failed"), "running": n("running"), "queued": n("queued"), "ratios": [],
                       "eta_ts": max([j.get("eta_ts") or 0 for j in mine], default=0) or None if k >= 26 else None})
    d.update(lanes=lanes, jobs=jobs, groups=groups, attempts=[], all_done_ts=max(j.get("eta_ts") or 0 for j in jobs), alerts=d["alerts"][:1], rebalance=None)
    return d


def overview_dense() -> dict:
    """overview() 的占用上再加 _dense_jobs：原有的长任务（srv-a 的 alpha_train_s1 到 +8 小时、排队到 +7 小时，srv-b 的 beta_eval_s7/s8 到 +4 小时）
    留着，与任务表对得上；它们正是近景的长尾规则要判的情形。"""
    d = overview()
    st = {"done": "done", "running": "run", "queued": "plan"}
    for h in ("srv-a", "srv-b"):
        d["occ"][h] += [{"p": "proj-a", "n": name, "lane": f"车道 {i % 12 + 1}", "t0": t0, "t1": t1, "g": [g], "st": st[s]}
                        for i, hh, g, name, s, t0, t1 in _dense_jobs() if hh == h]
    return d


ALL = {"overview": overview, "overview_evil": overview_evil, "overview_sparse": overview_sparse,
       "project": project, "project_evil": project_evil, "project_sparse": project_sparse}
def overview_user() -> dict:
    """普通用户只有一台可用主机、一个在用项目和一个已归档项目。"""
    return view(Ident("amy", "user", frozenset({"read", "project"}), "session", "k"), overview(),
                Access({"proj-a", "proj-old"}, {"srv-a"}, {"srv-a"}))


EXTRA = {"overview_user": overview_user, "records": records, "records_evil": records_evil, "events": project_events, "events_evil": project_events_evil,
         "project_overdue": project_overdue, "project_longq": project_longq, "project_moved": project_moved, "project_pool_back": project_pool_back, "events_overdue": project_events_overdue,
         "project_dense": project_dense, "overview_dense": overview_dense, "project_many": project_many,
         "project_deps": project_deps}

