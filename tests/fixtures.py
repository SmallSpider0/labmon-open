"""测试的公共夹具：一处写，各测试文件从这里 import，测试模块之间不互相 import。

导入它就设好测试环境（不弹通知、不碰钥匙串、不读本机的用户级主机清单、PATH 里的 ssh 换成 tests/bin 下立刻按连不上返回的假程序）。里面有：

- 假的队列进程 ``queue_procs``、``kill_procs``、``dead_pid``；假的文件标记式队列 ``make_queue`` 与项目配置 ``write_config``
- 给测试里起的看板服务发写请求 ``post_req``（带 JSON 类型）；看板要登录（#160）：``make_token`` 建账号与令牌、``login`` 让用例里的 urlopen 都带上、
  ``bearer_opener`` 自己拼 opener
- 命令行走看板接口（#162）：``board`` 起一个临时看板服务（不起采集线程）、写好 client.toml 与令牌，用例里的 labmon 命令都连它；
  ``register`` 直接登记到看板上（不经接口）
- worker 现在写出的形状：``fake_status``（status.json）、``running_job``、``waiting_job``（状态里的任务）、``usage_row``（usage.jsonl 一行）、
  ``fake_host``（工作台快照里一台主机，overview.collect 给的）。字段名照 labmon_worker.py 写状态与用量的地方：
  运行中任务的开始是 epoch 秒 ``t0``，用量行是 ``t0`` / ``t1``，项目 id 是登记时的 ``project``
- 起真 worker：``KA_SH``（假保活脚本）、``worker_env``（临时目录下的环境变量）、``load_worker``（按环境变量载入一份 worker 模块）、
  ``worker_module``（不起进程直接调函数）；都经 ``no_host`` 不读跑测试那台机器的 /proc 与 cgroup
  任务停在闸门上、等用例放行：``gate``（写进任务命令）、``release``（要“任务还在跑时看状态”就用它，不靠任务睡得够久）
- 多主机：``Hosts``（共享一个临时 HOME 的几台 worker、别的主机的假状态与用量记录、建清单、可推进的时钟 ``Clock``）；
  新功能默认用它写一条多主机用例（AGENTS.md「怎么测」）
- 组内服务器探针样例 ``probe_out``、``KA_PROBE``、``GUARD_PROBE``、``T0``、``GB`` 与配置 ``fleet_cfg``；装作正在做平台操作的 ``fake_op``
- 平台登录用的 RSA 测试密钥 ``KEY``、``make_key``、``rsa_decrypt``
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "labmon" / "labmon_worker.py"
NO_HOSTS = str(ROOT / "tests" / "_no_user_hosts.toml")       # 不存在的用户级主机清单：测试不受本机清单影响

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["LABMON_NO_NOTIFY"] = "1"                 # 测试不弹系统通知、不推到手机
os.environ["LABMON_NOTIFY_CONF"] = str(ROOT / "tests" / "_no_notify.toml")   # 不存在的推送配置：测试读不到本机的推送密钥（#159）
os.environ["LABMON_NO_KEYCHAIN"] = "1"                # 测试不碰钥匙串里的平台令牌
os.environ["LABMON_NO_HOSTCHECK"] = "1"               # 测试不调 powershell.exe（在 WSL 里跑测试也不启用体检，#166）
os.environ.setdefault("LABMON_HOSTS", NO_HOSTS)
SSH_GUARD = str(ROOT / "tests" / "bin")              # 假 ssh 放在 PATH 最前面：漏了桩的用例不真连服务器、也不等 20 秒连接超时
if os.environ.get("PATH", "").split(os.pathsep)[0] != SSH_GUARD:
    os.environ["PATH"] = SSH_GUARD + os.pathsep + os.environ.get("PATH", "")

from labmon import service as _service  # noqa: E402
_service.MANAGER = "launchd"     # 托管配置在哪个平台都按 launchd 的读写（各用例把 PLIST 打桩到临时文件）；systemd 的用例自己换 MANAGER 与 UNIT_FILE（#164）


# ---------- 假进程 ----------
def queue_procs() -> tuple[subprocess.Popen, subprocess.Popen]:
    """假的队列进程（命令行含 gpu_queue，探针按它认锁活着）与当前任务进程；用完 kill_procs。"""
    lock = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "gpu_queue"])
    cur = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return lock, cur


def kill_procs(*ps: subprocess.Popen) -> None:
    for p in ps:
        p.kill()
        p.wait()


def dead_pid() -> int:
    """一个已经退出的进程的 PID（锁文件指着它就是“队列进程不在”）。"""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ---------- 假的文件标记式队列与项目配置 ----------
def make_queue(root: Path, lock_pid: int, cur_pid: int) -> None:
    """四个任务的队列：a_done 跑完、a_run 在跑（10 分钟前开始，日志里有进度）、b_next 与 b_noest 排队。"""
    (root / "jobs").mkdir(parents=True)
    (root / "jobs" / "q.txt").write_text(
        "# 注释行\n"
        "a_done|echo a   # est_min=10\n"
        "a_run|echo b   # est_min=30\n"
        "b_next|echo c   # est_min=20\n"
        "b_noest|echo d\n")
    (root / "jobs" / "q.env").write_text("QUEUE_STATE=state\nQUEUE_LOGS=logs\n")
    st, lg = root / "state", root / "logs"
    st.mkdir()
    lg.mkdir()
    (st / "lock").write_text(str(lock_pid))
    start = dt.datetime.fromtimestamp(time.time() - 600).strftime("%Y-%m-%d %H:%M:%S")
    (st / "current").write_text(f"a_run {cur_pid} {start}")
    (st / "a_done.done").write_text("")
    t0 = dt.datetime.fromtimestamp(time.time() - 1800).strftime("%Y-%m-%d %H:%M:%S")
    t1 = dt.datetime.fromtimestamp(time.time() - 700).strftime("%Y-%m-%d %H:%M:%S")
    (st / "queue.log").write_text(f"{t0} 开始 a_done\n{t1} 完成 a_done（耗时 18 分）\n{start} 开始 a_run\n")
    # 日志追加写：上一次跑到 9/10 失败，这一次从分隔行起；库每次加载模型都打印的提示逐字重复
    hint = "You should probably TRAIN this model\n"
    (lg / "a_run.log").write_text("PROGRESS 9/10\nTraceback 上一次\n===== 2027-01-01 00:00:00 labmon 开始（GPU 0）=====\n"
                                  f"加载数据\n{hint}PROGRESS 5/10 loss=0.3\n  File \"x.py\", line 3\nstep 5 done\n{hint}{hint}")


def write_config(d: Path, root: Path, out: Path, host_extra: str = "") -> Path:
    """项目配置：本机主机 here、一条队列 q（训练方）、计划项 A / B、一个里程碑。"""
    cfg = d / "monitor.toml"
    cfg.write_text(f"""
title = "测试监控"
out_dir = "{out}"
[[hosts]]
id = "here"
label = "本机"
{host_extra}
mounts = [{{ path = "/", label = "根", min_gb = 0.001 }}]
[[queues]]
id = "q"
label = "训练方"
root = "{root}"
jobs = "jobs/q.txt"
[[groups]]
id = "A"
prefix = "a_"
[[groups]]
id = "B"
prefix = "b_"
[[markers]]
date = "2099-01-02"
label = "截稿"
""")
    return cfg


def register(cfg: Path) -> str:
    """把项目目录里的配置登记到看板上（#161；登记在 LABMON_STATE 下），返回项目 id：起 serve.Project、按 id 找项目前先登记。
    直接调看板那边的 projects.put_config、再记下同步记录（同 labmon project push 推成后的样子），不起看板服务。"""
    from labmon import projects as PJ
    from labmon import projsync
    from labmon.config import brief
    b = brief(cfg)
    r = PJ.put_config(b["id"], Path(cfg).read_text(), projsync._local(b), projsync.base_of(cfg, b["id"]))
    projsync.mark(cfg, b["id"], r["ver"])
    return b["id"]


def board(case, scopes=("project",), name: str = "boss", role: str = "admin"):
    """起一个临时看板服务（serve.Server，绑 127.0.0.1 的随机端口；不起采集线程：Project.start 换成空的；服务日志不打到标准输出），
    写 client.toml（LABMON_CLIENT）指向它，LABMON_TOKEN 是 name 的令牌（作用域 scopes）。用例里的 labmon 命令（__main__.main、projsync）都连它。
    要在 LABMON_STATE 指到临时目录后用；用例结束时停掉。返回 Server（sync() 按登记加载项目）。"""
    import shutil
    import tempfile
    import threading
    from unittest import mock
    from labmon import serve as SV
    for p in (mock.patch.object(SV.Project, "start", lambda self, name: None), mock.patch.object(SV, "log", lambda msg: None)):
        p.start()
        case.addCleanup(p.stop)
    srv = SV.Server(("127.0.0.1", 0))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()   # shutdown 不用等缺省 0.5 秒的轮询
    case.addCleanup(srv.server_close)
    case.addCleanup(srv.shutdown)
    d = Path(tempfile.mkdtemp(prefix="labmon-client-"))
    case.addCleanup(shutil.rmtree, d, True)
    (d / "client.toml").write_text(f'[[servers]]\nurl = "http://127.0.0.1:{srv.server_address[1]}"\n')
    env = mock.patch.dict(os.environ, {"LABMON_CLIENT": str(d / "client.toml"), "LABMON_TOKEN": make_token(name, role, scopes)})
    env.start()
    case.addCleanup(env.stop)
    return srv


# ---------- 给看板服务的写请求 ----------
def post_req(url: str, data: bytes = b"{}"):
    """POST 到测试里起的看板服务：带 JSON 类型（看板只认 application/json 的 POST，serve.Handler._gate，#158；页面上同样经 common.js 的 postJSON）。"""
    import urllib.request
    return urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})


# ---------- 看板账号（#160）：测试里起的看板服务都要登录 ----------
def add_account(name, role, by="测试", **kw):
    """测试账号与人员登记均走真实创建入口。"""
    from labmon import fleet as F, fleetboot as FB, users as U
    person = kw.pop("person", name)
    if person not in F.registered_people():
        from unittest import mock
        with mock.patch.object(FB, "log_events"):
            FB.set_person(person, create=True, roster_only=True)
    return U.add(name, role, by, person=person, **kw)


def make_legacy_account(name, role):
    """直接写一条旧账号记录，供历史映射测试。"""
    from labmon import users as U
    with U._edit() as d:
        d["users"][name] = {"role": role}
    return U.token_add(name, "旧测试账号", ["admin"])[0]


def make_token(name: str = "boss", role: str = "admin", scopes=("admin",)) -> str:
    """在当前 LABMON_STATE 下建账号（还没有就建；第一个必须是管理员，建普通用户前先补一个 boss）与一个令牌，返回令牌原文。
    不设口令（不算 PBKDF2）：令牌就够测路由。建的时候不记进记录页（不打乱各用例对记录的断言）。"""
    from unittest import mock
    from labmon import users as U
    with mock.patch.object(U, "_log", lambda msg: None):
        have = U._read()["users"]
        if role != "admin" and not any(u.get("role") == "admin" for u in have.values()):
            add_account("boss", "admin")
        if name not in U._read()["users"]:
            add_account(name, role)
        return U.token_add(name, "测试", list(scopes))[0]


def bearer_opener(tok: str, *handlers):
    """带 Authorization: Bearer 的 urllib opener（handlers 另加，如不跟随跳转的）。"""
    import urllib.request

    class Bearer(urllib.request.BaseHandler):
        def http_request(self, req):
            if not req.has_header("Authorization"):
                req.add_unredirected_header("Authorization", f"Bearer {tok}")
            return req
    return urllib.request.build_opener(Bearer(), *handlers)


def login(case, **kw) -> str:
    """让这个用例里的 urllib.request.urlopen 都带令牌（缺省建管理员 boss 的 admin 令牌，见 make_token）；用例结束时卸掉。返回令牌原文。"""
    import urllib.request
    tok = make_token(**kw)
    urllib.request.install_opener(bearer_opener(tok))
    case.addCleanup(urllib.request.install_opener, None)
    return tok


# ---------- worker 写出的形状 ----------
def fake_status(**kw) -> dict:
    """一份 worker 的 status.json（labmon_worker.py 的 write(self.p.status, …) 写的键都在，列表都空）；kw 覆盖。"""
    now = time.time()
    st = {"t": now, "pid": 1, "host": "h", "info": {"gpus": [], "mem": {}, "disk": [], "started": now - 3600,
                                                    "press": {"thr": None, "psi": None, "nfs": None, "disk": None}}, "gpu_procs": [],
          "gate": {"on": False, "at": 0.92}, "limits": {}, "overrides": [], "local": None, "storage": None, "voided": [], "kafight": None, "ka_stop": None, "watchdogs": {"frozen": [], "seen": []}, "guest": None,
          "util10": {}, "disk": None, "room": {"ram": None, "margin": None, "gpus": {}, "cpu": None, "cpu_cap": None, "owner": []}, "queues": [], "plan": {"t": now, "lanes": []}, "todo": [], "dep_miss": [], "offers": [],
          "share": {"wait_cap_min": 30.0, "half_life_h": 24.0, "weight": {}, "use": {}, "run": []},
          "running": [], "external": [], "waiting": []}
    st.update(kw)
    return st


def running_job(name: str, project: str, t0: float | None = None, **kw) -> dict:
    """状态里运行中的一个任务（job_view 加 t0 等）；开始时刻只有 epoch 秒 t0。"""
    j = {"kind": "queue", "queue": "jobs/q.txt", "root": None, "project": project, "name": name, "pid": 1, "gpus": [], "tag": None,
         "hold": False, "stop": None, "use": {}, "cmd": "", "est": None, "acct": None, "log": None, "tail": [],
         "t0": time.time() - 60 if t0 is None else t0, "vram": None, "excl": None, "est_min": None, "oom0": None, "fc0": None,
         "owner_gpu": None, "wait": None, "cpus": None, "io": None}
    j.update(kw)
    return j


def waiting_job(name: str, project: str, why: str = "没有可用显存", **kw) -> dict:
    """状态里等待中的一个任务：pos 是排位（被临时指令挡住的没有），start 是推演里预计开始的 epoch 秒（排不上的没有）。"""
    w = {"kind": "queue", "queue": "jobs/q.txt", "root": None, "project": project, "name": name, "excl": None, "tag": None, "est": None,
         "why": why, "since": time.time(), "held": None, "pos": None, "start": None, "cpus": None, "io": None}
    w.update(kw)
    return w


def usage_row(name: str, project: str, t0: float, t1: float, **kw) -> dict:
    """usage.jsonl 的一行（任务结束时 worker 记的）：起止是 epoch 秒 t0 / t1（整数）。"""
    r = {"t0": round(t0), "t1": round(t1), "min": round((t1 - t0) / 60, 2), "kind": "queue", "root": None, "queue": "jobs/q.txt",
         "project": project, "name": name, "tag": None, "rc": "0", "gpus": [], "vram": None, "ram_hint": None, "est": None, "excl": None,
         "ram_peak": None, "vram_peak": None, "gpu_util": None, "cores_mean": None, "with_gpu": [], "with_host": [], "void": None,
         "est_min": None, "gpu": None, "cards": 0.0, "wait": None, "requeued": False,
         "cores_peak": None, "rd_peak": None, "wr_peak": None, "rd_b": None, "wr_b": None, "cpus": None, "io": None}
    r.update(kw)
    return r


def fake_host(hid: str = "h", status: dict | None = None, **kw) -> dict:
    """工作台快照里的一台主机（overview.collect 给各模块的 e）：status 是 fake_status 的参数，None 为读不到状态。"""
    e = {"id": hid, "label": hid, "error": None, "status_error": None, "status": None if status is None else fake_status(**status),
         "log": [], "done": [], "lan_off": False, "auto": False, "platform": None, "worker_up": status is not None, "age": 0}
    e.update(kw)
    return e


# ---------- 起真 worker ----------
KA_SH = """#!/usr/bin/env bash
# 假保活：状态记在 $AGRI_ROOT/ka_on（内容是 running 打印的进程组，可以空着），动作记在 $AGRI_ROOT/ka.log
. "$(dirname "$0")/ka.env"
case "$1" in
  running) [ -f "$AGRI_ROOT/ka_on" ] && cat "$AGRI_ROOT/ka_on" ;;
  stop) rm -f "$AGRI_ROOT/ka_on"; echo stop >> "$AGRI_ROOT/ka.log" ;;
  start) echo start >> "$AGRI_ROOT/ka.log"; touch "$AGRI_ROOT/ka_on" ;;   # 先记日志再 touch：用例见 ka_on 出现就读日志
esac
"""


def no_host(d: Path) -> dict:
    """worker 读主机现状的三处（/proc 的用量与内存、cgroup 的上限、容器根盘）指到 d 下不存在的目录：测试不读跑测试的这台机器
    （Linux 上 CI 机器的 /proc 与 cgroup 是真的，内存上限只有几 GiB，任务全都放不下；macOS 上本来就没有；根盘不指走的话 LocalDisk 会扫这台的 /）。
    要假数据的用例另给 LABMON_PROC、LABMON_CGROUP、LABMON_ROOTFS。"""
    return {"LABMON_PROC": str(d / "noproc"), "LABMON_CGROUP": str(d / "nocg"), "LABMON_ROOTFS": str(d / "noroot")}


def worker_env(d: Path, **extra: str) -> dict:
    """在临时目录 d 下布置假保活（本来在跑）、假 GPU（一张 40 GiB 空卡）与 worker 的各目录，返回起 worker 用的环境变量。
    worker 的 HOME 是 d/home（状态在 d/home/hosts/<主机>/），本地目录 d/local，单元交换目录 d/slots；不读本机的 /proc 与 cgroup（no_host）。"""
    ka = d / "ka"
    ka.mkdir()
    (ka / "ka.sh").write_text(KA_SH)
    (ka / "ka.env").write_text(f"AGRI_ROOT={d / 'keepalive_demo'}\n")
    (d / "keepalive_demo").mkdir()
    (d / "keepalive_demo" / "ka_on").touch()
    return dict(os.environ, **dict(no_host(d), LABMON_HOME=str(d / "home"), LABMON_TICK="0.3", KA_BIN=str(ka), LABMON_GPUS="GPU-aaa:40000",
                                   LABMON_GPU_APPS="", LABMON_SLOTS=str(d / "slots"), LABMON_LOCAL=str(d / "local"), **extra))


def gate(d: Path, name: str = "go") -> str:
    """任务命令里的闸门（shell 片段）：一直等到用例 ``release(d, name)`` 才往下走；临时目录 d 删了也退出，不留孤儿进程。
    起真 worker 的用例要在“任务还在跑”时看状态（等卡、排位、记账、接管），就让任务停在这里，看完再放行，不靠任务睡得够久（CI 慢时会先跑完）。"""
    g = d / "gates"
    g.mkdir(exist_ok=True)
    return f"while [ -d '{g}' ] && [ ! -e '{g / name}' ]; do sleep 0.05; done"


def release(d: Path, name: str = "go") -> None:
    """放行停在闸门 name 上的任务（见 ``gate``）。"""
    (d / "gates" / name).touch()


def load_worker(env: dict):
    """按给定环境变量（LABMON_PROC 等在导入时读）载入一份独立的 worker 模块。"""
    import importlib.util
    if "LABMON_HOME" in env:                       # 本地目录跟着测试的临时目录走，不写真的 /tmp/labmon
        env = dict({"LABMON_LOCAL": str(Path(env["LABMON_HOME"]) / "local")}, **env)
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("labmon_worker_t", WORKER)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k)
            else:
                os.environ[k] = v


def worker_module(d: Path, **extra: str):
    """不起进程、直接调函数用的 worker 模块：HOME 在 d/home（状态在 d/home/hosts/<主机>/），没有保活脚本（KA_BIN 指到不存在的目录），
    单元交换目录 d/slots，不读本机的 /proc 与 cgroup（no_host）；extra 另加或改环境变量（假的 LABMON_PROC、LABMON_CGROUP 等）。"""
    return load_worker(dict(no_host(d), LABMON_HOME=str(d / "home"), KA_BIN=str(d / "noka"), LABMON_SLOTS=str(d / "slots"), **extra))


# ---------- 多主机 ----------
class Clock:
    """可推进的时钟：顶替一份 worker 模块里的 time（只换这一份，别的模块和测试代码照旧）。time() 是真时间加上推进的量，
    其余函数照真的 time 模块；不推进时与真时间一样，测试里拿 time.time() 比也对得上。"""

    def __init__(self):
        self.offset = 0.0

    def time(self) -> float:
        return time.time() + self.offset

    def __getattr__(self, k):
        return getattr(time, k)


class Hosts:
    """多主机：共享一个临时 HOME（共享盘上的 worker_home，各台在 hosts/<主机>/ 下）的几台 worker，同一个可推进的时钟。
    不起 worker 进程、直接调函数；worker 模块只有一份（各台跑的是同一个 labmon_worker.py），每台是一个 Worker。
    模块级的东西因此也只有一份：本地盘 ``LOCAL``、单元目录 ``SLOTS``、``LABMON_GPUS`` 各台共用，真实环境里每个容器各有一份；
    测本地依赖、按卡的逻辑时别拿它当几台独立的容器。

    - ``worker(host)``：建一台（状态目录已建）；它 ``tick`` 写出的 status.json、usage.jsonl 别的台按真实路径读
    - ``status(host, age=0, stopped=False, reread=True, **fields)``：直接写一台的假状态（别的主机眼里的它）：fake_status 的形状，
      ``t`` 是 age 秒前，fields 覆盖（``todo``、``share``、``plan``…）；``stopped`` 留下 worker stop 的标记（含借用归还），为假时清掉；
      ``reread`` 让各台下一次 ``peers`` 重读（不然 PEER_EVERY 内沿用）
    - ``usage(host, rows)``：在一台的 usage.jsonl 后面接几行（各台 load_hist 一起读）
    - ``queues({名: [清单行]}, project=None)``：建清单并按登记的样子载入，项目缺省同清单名；清单在 HOME 旁边，各台都能登记
    - ``register(host, 名, project=None)``：把 ``queues`` 建的那条清单登记到一台（写 hosts/<主机>/queues/<名>.json，同 worker add）
    - ``advance(s)``：时钟往前推 s 秒；``now()``：现在（各台眼里的）
    """

    def __init__(self, d: Path, **extra: str):
        self.d = d
        self.m = worker_module(d, **extra)
        self.clock = Clock()
        self.m.time = self.clock
        self.home = Path(self.m.HOME)
        self.ws: dict = {}
        self.made: list = []          # 建过的每一台（同一主机重建的旧 Worker 也在），close 收它们放行的任务

    def now(self) -> float:
        return self.clock.time()

    def advance(self, s: float) -> float:
        self.clock.offset += s
        return self.now()

    def worker(self, host: str):
        w = self.ws[host] = self.m.Worker(host)
        self.made.append(w)
        os.makedirs(w.p.dir, exist_ok=True)
        return w

    def status(self, host: str, age: float = 0.0, stopped: bool = False, reread: bool = True, **fields) -> Path:
        d = self.home / "hosts" / host
        d.mkdir(parents=True, exist_ok=True)
        f = d / "status.json"
        f.write_text(json.dumps(fake_status(**dict({"t": self.now() - age, "host": host}, **fields))))
        if stopped:
            (d / "stopped").write_text("")
        else:
            (d / "stopped").unlink(missing_ok=True)
        if reread:
            for w in self.ws.values():
                w.peer_t = 0
        return f

    def usage(self, host: str, rows: list) -> None:
        d = self.home / "hosts" / host
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "usage.jsonl", "a") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)

    def queues(self, spec: dict, project: str | None = None) -> list:
        out = []
        for n, lines in spec.items():
            (self.d / n / "jobs").mkdir(parents=True)
            (self.d / n / "jobs" / "q.txt").write_text("\n".join(lines) + "\n")
            out.append(self.m.load_queue({"_slug": n, "root": str(self.d / n), "jobs": "jobs/q.txt", "project": project or n}))
        return out

    def close(self) -> None:
        """清理临时目录之前调：各台 ``tick`` 真放行的清单任务（bash 子进程，自成一个进程组）还没退出的整组杀掉，并等它们都退出。
        任务收尾会往状态目录写 .rc、往日志续写，不等就删目录会报 “Directory not empty”（#185）。
        只认本进程的子进程（waitpid 认不出的跳过：已被回收，或是用例手写的假 PID），不碰别的进程。"""
        for w in self.made:
            for j in list(w.running.values()):
                pid = j.get("pid") if j.get("kind") == "queue" else None
                if not pid:
                    continue
                try:
                    if os.waitpid(pid, os.WNOHANG) == (0, 0):   # 还在跑
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):   # 刚好退出了（macOS 上组里只剩僵尸时报 EPERM）
                            pass
                        os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def register(self, host: str, name: str, project: str | None = None) -> Path:
        d = self.home / "hosts" / host / "queues"
        d.mkdir(parents=True, exist_ok=True)
        f = d / (name + ".json")
        f.write_text(json.dumps({"root": str(self.d / name), "jobs": "jobs/q.txt", "project": project or name}))
        return f


# ---------- 组内服务器探针样例 ----------
@contextlib.contextmanager
def fake_op(ids: list[str], kind: str = "renew"):
    """装作正在做一个平台操作：op.json 里 active、本进程的 pid，并起一个叫 fleet-<kind> 的线程（fleetops.op 按它判操作还在）；
    退出时停掉线程、清掉 op.json。要在 LABMON_STATE 指到临时目录后用。"""
    import threading
    from labmon import fleet as F
    from labmon.state import save_json
    ev = threading.Event()
    th = threading.Thread(target=ev.wait, name=f"fleet-{kind}", daemon=True)
    th.start()
    F.state_dir().mkdir(parents=True, exist_ok=True)
    save_json(F.state_file("op.json"), {"kind": kind, "active": True, "pid": os.getpid(), "ids": list(ids),
                                        "steps": [{"name": "登录", "state": "run", "note": None}]})
    try:
        yield
    finally:
        ev.set()
        th.join(5)
        save_json(F.state_file("op.json"), {})


GB = 2 ** 30
T0 = 1_800_000_000          # 测试用的“现在”（整点之后 800 秒）


def probe_out(apps=(), ps=(), maps=(), used=7000, util=(90, 90), cores=1.0, mem_gb=12.0, inact_gb=2.0, anon_gb=3.5, limit_gb=61,
              quota=7, started=T0 - 86400, clock=T0, root_gb=1.0, rootfs=None, shm=None, scan=None):
    """拼一份 fleet_probe.sh 的输出。apps: (宿主机 PID, MB)；ps: (pid, ppid, 命令行[, pcpu, rss_kb])；maps: (容器 PID, 宿主机 PID, 目录)。
    根盘（#149）：root_gb 已用（总量 750 GiB）；rootfs "文件系统 宿主盘总量kB"、shm "来源 已用kB" 给了才有那一行（缺省没有：根盘测不准，不扫路径）；
    scan：scan 段的各行（给了才有这一段）。"""
    btime, tck = started - 1000, 100
    out = ["@@ info", f"clock {clock}", f"btime {btime}", f"tck {tck}", f"p1 {1000 * tck}", "nproc 64",
           f"memlim {int(limit_gb * GB)}", f"memuse {int(mem_gb * GB)}", f"inact {int(inact_gb * GB)}", f"actf {int((mem_gb - inact_gb - anon_gb) * GB)}",
           f"cpuq {quota * 100000} 100000", "cpumax ", f"root 786432000 {int(root_gb * 1048576)}",
           *([f"rootfs {rootfs}"] if rootfs else []), *([f"shm {shm}"] if shm else []), "@@ gpu",
           f"0, GPU-x, NVIDIA RTX A6000, {used}, 49140, {util[0]}", "@@ util", *[f"0, {u}" for u in util],
           "@@ cpu", f"0 0 {int(cores * 5e9)} 5000000000", "@@ apps", *[f"{h}, {mb}, GPU-x" for h, mb in apps], "@@ map",
           *[f"{c} {h} {d}" for c, h, d in maps], "@@ ps"]
    for p in ps:
        pid, ppid, args, pcpu, rss = (*p, 0.0, 1024)[:5] if len(p) == 3 else p
        out.append(f"{pid} {ppid} 3600 1-02:03:04 {pcpu} {rss} {args}")
    if scan is not None:
        out += ["@@ guard", "@@ scan", *scan]
    return "\n".join(out) + "\n"


# 只有保活在跑
KA_PROBE = probe_out(apps=[(500, 6748)], maps=[(9, 500, "/root/data1/alice/ka")],
                     ps=[(1, 0, "/bin/bash"), (9, 1, "/root/miniconda3/envs/py38/bin/python /root/data1/alice/ka/train_keep_alive.py", 100.0, 3 * 1024 * 1024)],
                     util=(80, 82), cores=1.2, mem_gb=6.0)
# 真实验外面套着 keepalive_guard.sh（不带下划线，不能算保活）；GPU 进程是 multiprocessing 的子进程
GUARD_PROBE = probe_out(apps=[(700, 7800)], maps=[(40, 700, "/root/proj/repo")], util=(60, 50), cores=1.0, mem_gb=10, limit_gb=122, quota=17,
                        ps=[(1, 0, "/bin/bash"), (10, 1, "bash /root/proj/exp/keepalive_guard.sh bash run.sh"), (11, 10, "bash run.sh"),
                            (20, 11, "/root/proj/venv/bin/python exp/run_parallel.py --workers 2"),
                            (30, 20, "/root/proj/venv/bin/python eval.py --config /root/proj/cfg/a.yaml"),
                            (40, 30, "/root/proj/venv/bin/python -c from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=11) --multiprocessing-fork", 100.0, 2 * 1024 * 1024)])


def fleet_cfg(**kw) -> dict:
    """组内服务器的配置（fleet.conf 读出来的样子）：两台服务器 a（AA，私有池 P）、b（BB，公共池 Q）；rules 给 [{match, kind, label}] 这里编译。"""
    rules = kw.pop("rules", [])
    c = {"interval": 600, "lease_days": 7, "private_pools": ["P"], "private_max": 4, "public_max": 2, "keepalive": ["keep_alive"],
         "keepalive_cmd": "bash /x/ka.sh", "guard_idle_min": 60, "guard_watch_min": 5,
         "share": {"vram": 0.3, "util": 30, "cpu": 0.3, "mem": 0.3},
         "servers": [{"id": "a", "holder": "AA", "label": "AA", "ssh": "root@h", "port": 1, "pool": "P"},
                     {"id": "b", "holder": "BB", "label": "BB", "ssh": "root@h", "port": 2, "pool": "Q"}],
         "rules": [{**r, "re": re.compile(r["match"])} for r in rules]}
    c.update(kw)
    return c


# ---------- 平台登录用的 RSA 测试密钥 ----------
def _prime(bits: int) -> int:
    while True:
        n = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if all(pow(a, n - 1, n) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29)):
            return n


def _der(tag: int, body: bytes) -> bytes:
    n = len(body)
    ln = bytes([n]) if n < 128 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + ln + body


def _int(v: int) -> bytes:
    b = v.to_bytes((v.bit_length() + 8) // 8, "big")
    return _der(0x02, b)


def make_key(bits: int = 1024):
    """(SPKI 的 base64, n, d)：平台网页里的公钥与解密用的私钥。"""
    e = 65537
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            break
    n, d = p * q, pow(e, -1, (p - 1) * (q - 1))
    alg = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    spki = _der(0x30, alg + _der(0x03, b"\x00" + _der(0x30, _int(n) + _int(e))))
    return base64.b64encode(spki).decode(), n, d


def rsa_decrypt(n: int, d: int, b64: str) -> str:
    k = (n.bit_length() + 7) // 8
    em = pow(int.from_bytes(base64.b64decode(b64), "big"), d, n).to_bytes(k, "big")
    assert em[:2] == b"\x00\x02" and 0 not in em[2:10], "PKCS#1 v1.5 填充不对"
    return em[em.index(0, 2) + 1:].decode()


KEY = make_key()
