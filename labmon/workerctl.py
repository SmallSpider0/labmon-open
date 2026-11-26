"""本机操作服务器上的 labmon worker：部署、拉起、停、看状态、登记队列。每条命令一次 ssh（复用看板的连接）。

    labmon worker start  29                       部署 main 上已提交的 labmon_worker.py 并拉起（已在跑则无缝替换，任务不受影响）
    labmon worker stop   29                       停 worker（运行中的任务继续跑）
    labmon worker status 29                       登记的队列、运行中与等待中的任务
    labmon worker add    29 ROOT JOBS --project a1 [--gpus 0] [--stop term]
                                                   登记一条队列（ROOT 是服务器上的绝对路径，JOBS 相对 ROOT）
    labmon worker rm     29 ROOT JOBS              移除登记（已归还的主机经与它共用 worker_home 的在用主机代办，见 via）
    labmon worker limit  29 [heavy=2 …]           同 tag 同时运行数的上限（heavy= 删掉）
    labmon pause demo-b [--host 29] [--until 11-10] [--now]   临时指令：暂停、恢复、优先、drain（项目的不带 --host 记在主机清单、所有主机生效；
    labmon resume demo-b | focus a1 [--until 11-17] | unfocus a1 | drain 29 [--until …] | undrain 29     带 --host 的经 override_each 只下给那几台）
    labmon project init  29 [项目名]              按共享盘目录规范建目录（给了项目名连 projects/<项目名>/ 一起建），列出可共用的环境、数据集与权重

远端命令的输出由 run_out / override_each 收回来返回，这里不打印：命令行入口（__main__）打印出来，看板服务、借用与续租记进带时刻的日志或事件。
共享盘根目录是主机的 data_root（缺省 /data/labmon），规范见 data_root_readme.md（部署为其中的 README.md）；
传到服务器上的这两个文件都取 main 上已提交的版本（committed），工作区里改了一半的、分支上还没合并的都不会被拉起、借用、续租带上去。
worker 装在 worker_home（缺省 <data_root>/labmon，共享盘上一份供各主机共用）。都可在 ~/.config/labmon/hosts.toml 的 [[hosts]] 里改。
主机写了 shared（与其它主机共用的目录）时，start 让 worker 限速轮流统计其中第一个的按人用量（同一时刻只有一台在扫）；借来的主机不扫。
借来的组内服务器（borrow.py）也在这里当主机用（config.user_hosts），id 用组内服务器的。
"""
from __future__ import annotations

import base64
import io
import json
import re
import shlex
import subprocess
import tarfile
import time
from pathlib import Path

from . import lan as L
from . import lease as LS
from .config import POOL, ConfigError, active_hosts, all_hosts, home_host, host_defaults, hosts_file, pool_home, user_hosts

from .worker import queue_stop_path

HERE = Path(__file__).resolve().parent


def host_conf(hid: str) -> dict:
    hosts = {str(h.get("id")): h for h in user_hosts()}
    if hid not in hosts:
        raise ConfigError(f"{hosts_file()} 里没有主机 {hid}")
    h = host_defaults(hosts[hid])
    if h.get("returned"):
        raise ConfigError(f"主机 {hid} 已归还（{h['returned']}）；" + ("再借用 labmon borrow " if h.get("borrow") else "再借到时先 labmon host restore ") + hid)
    return h


def via(hid: str, home: str | None = None) -> dict:
    """在哪台上操作这台目录下的登记（worker rm）：在用的就是它自己；已归还的、主机清单里已没有的（home 是它的 worker_home，
    全局快照 regs 里那一项的 home），经一台与它共用 worker_home 的在用主机代办（config.home_host，与 overview.collect 每轮代读登记挑的是同一台），
    命令照样带 --host hid：改的是共享盘上我们自己 worker_home 里那台的登记文件，不连已归还的那台（借来还了的是别人的服务器）。找不到能代办的报 ConfigError。
    算力池（hid 是 config.POOL，#24）没有 home 时取 config.pool_home：worker add|rm pool 都经这里。"""
    act = active_hosts()
    if any(str(h["id"]) == hid for h in act):
        return host_conf(hid)
    if hid == POOL and home is None:                 # 算力池（#24）：登记在 pool_home 下，同样经共用这个 worker_home 的在用主机代办
        home = pool_home()
        if home is None:
            raise ConfigError("主机清单里没有自用主机：算力池的登记没有地方放（池登记放在自用主机的 worker 目录下）")
    if home is None:
        home = next((h["worker_home"] for h in all_hosts() if str(h["id"]) == hid), None)
    o = home_host(home, act) if home else None
    if o is None:
        raise ConfigError(f"{hid} 已不在用，又没有与它共用 worker 目录（{home or '不知道在哪'}）的在用主机：它的登记现在动不了")
    return host_conf(str(o["id"]))


DEPLOY_REF = "main"     # 部署到服务器上的只取这个分支：在分支的 worktree 里跑 labmon 也不会把没合并的版本推上去
START_CHECK_S = 3       # 拉起 worker 后隔多久看它还在不在（启动即崩的报“拉起失败”）


def committed(name: str) -> str:
    """本包里一个文件在 main 上已提交的版本：部署到服务器上的只取它，改了要先合进 main（本地的 main，合并后先 pull）。
    路径相对本包目录（./），仓库根在哪都一样。"""
    return subprocess.run(["git", "-C", str(HERE), "show", f"{DEPLOY_REF}:./{name}"], capture_output=True, text=True, check=True).stdout


def committed_tree(subdir: str) -> list[str]:
    """本包里一个子目录在 main 上已提交的所有 .py 文件路径（相对本包目录，按字母序）。目录在 DEPLOY_REF 上还不存在时返回空表。"""
    r = subprocess.run(["git", "-C", str(HERE), "ls-tree", "-r", "--name-only", DEPLOY_REF, "--", subdir], capture_output=True, text=True)
    if r.returncode != 0 or not isinstance(r.stdout, str):
        return []
    return sorted(p for p in r.stdout.splitlines() if p.endswith(".py"))


def worker_bundle() -> tuple[str, list[str]]:
    """把 main 上已提交的 labmon_worker.py、worker/*.py 与 ka.sh 打成 tar.gz 再 base64，经一条 ssh 的 stdin 送出。
    返回 (base64 文字, worker/*.py 的文件名列表)；全部文件内容仍只经 committed() 读取。"""
    now = int(time.time())
    pkg = committed_tree("worker")
    items = [("labmon_worker.py", 0o755), ("ka.sh", 0o755)] + [(p, 0o644) for p in pkg]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, mode in items:
            data = committed(name).encode("utf-8")
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.mtime = now
            tf.addfile(ti, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode("ascii"), [p.split("/", 1)[1] for p in pkg]


def ssh(h: dict, remote: str, stdin: str = "", timeout: float | None = None) -> tuple[int, str]:
    """在主机上跑一条命令（stdin 是喂给它的文字），返回 (退出码, 输出)；输出里 stderr 接在 stdout 后面（ssh 连不上的原因也在里面）。"""
    cmd = L.ssh_argv(h["ssh"], h["port"], known_hosts=h.get("known_hosts")) + [remote] if h.get("ssh") else ["bash", "-c", remote]     # 没有 ssh：本机（测试用）
    r = (L.ssh_run if h.get("ssh") else subprocess.run)(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def worker_env(h: dict) -> str:
    """把主机的部署路径（保活脚本 ka_bin）交给 worker；取值按 shell 参数转义。"""
    pairs = (("KA_BIN", h.get("ka_bin")),)
    return " ".join("%s=%s" % (name, shlex.quote(str(value))) for name, value in pairs if value)


def project_init(hid: str, name: str | None) -> tuple[int, str]:
    """一条连接：建规范目录（和 projects/<名称>/），更新根目录 README，列出现有的共享环境、数据集与权重。返回 (退出码, 输出)。"""
    h = host_conf(hid)
    root = h["data_root"].rstrip("/")
    proj = f"projects/{shlex.quote(name)}" if name else "projects"
    remote = (f"cd {shlex.quote(root)} && mkdir -p envs datasets weights archive {proj} && "
              f"cat > README.md.new && mv README.md.new README.md && "
              f"for d in envs datasets weights; do echo \"$d/：$(ls $d | tr '\\n' ' ')\"; done"
              + (f" && echo {shlex.quote(f'已建 {root}/projects/{name}/')}" if name else ""))
    return ssh(h, remote, committed("data_root_readme.md"))


def override_each(action: str, kind: str, target: str | None, hosts: list[str], until: float | None = None, now: bool = False,
                  at: float | None = None, keep: dict | None = None, once: bool = False, by: str | None = None, expect_by: str | None = None) -> list[dict]:
    """在各主机的 worker 目录写只给这几台的临时指令（overrides.json），worker 下一轮生效。项目级的暂停、优先（所有主机）不经这里，
    记在主机清单（config.set_sched_ovr）。keep：guest 指令给对方留的量（borrow.reserve_for）；once：pause --now 停完就删（暂停由全局的那条挡）。
    by：谁下的（看板账号名，记进那条的 by，#27）；expect_by：worker 在锁里核对那台已有的同目标指令是这个账号下的（或没有），不是就不改、退出码 2
    （普通用户经看板写按主机的暂停时传，判断在 users.need_pause，这里只核快照之后有没有变）。
    返回每台的结果 [{"host", "rc", "out"}]（out 是 worker 打印的“生效中：…”，或 ssh 连不上的原因）。备用的看板服务上不写（#163）。"""
    LS.need("临时指令")
    out = []
    for hid in hosts:
        h = host_conf(hid)
        w = f"env {worker_env(h)} {shlex.quote(h['python'])} {shlex.quote(h['worker_home'] + '/labmon_worker.py')}"
        args = ["override", action, kind] + ([target] if target else [])
        extra = (f" --until {until}" if until else "") + (" --now" if now else "") + (f" --at {at}" if at else "") \
            + (f" --keep {shlex.quote(json.dumps(keep))}" if keep else "") + (" --once" if once else "") \
            + (f" --by {shlex.quote(by)}" if by else "") + (f" --expect-by {shlex.quote(expect_by)}" if expect_by is not None else "")
        rc, text = ssh(h, f"{w} {' '.join(shlex.quote(x) for x in args)}{extra} --host {shlex.quote(hid)}")
        out.append({"host": hid, "rc": rc, "out": text})
    return out


def _clear_borrow_cmd(path: str) -> str:
    """在远端用 Python 3.8+ 清掉 overrides.json 里遗留的借用指令（guest、target=borrow 的 deadline），
    与 worker/common.py 共用 overrides.json.lock 互斥锁与原子替换；文件不存在时直接返回 ok 0。"""
    code = (
        "import fcntl, json, os, sys\n"
        "p = sys.argv[1]\n"
        "if not os.path.exists(p):\n"
        "    print('ok 0')\n"
        "    sys.exit(0)\n"
        "with open(p + '.lock', 'w') as lk:\n"
        "    try:\n"
        "        fcntl.flock(lk, fcntl.LOCK_EX)\n"
        "    except OSError:\n"
        "        pass\n"
        "    if not os.path.exists(p):\n"
        "        print('ok 0')\n"
        "        sys.exit(0)\n"
        "    raw = open(p).read().strip()\n"
        "    cur = json.loads(raw) if raw else []\n"
        "    keep = [o for o in cur if not (o.get('kind') == 'guest' or (o.get('kind') == 'deadline' and o.get('target') == 'borrow'))]\n"
        "    n = len(cur) - len(keep)\n"
        "    if n:\n"
        "        tmp = '%s.tmp.%d' % (p, os.getpid())\n"
        "        with open(tmp, 'w') as f:\n"
        "            f.write(json.dumps(keep, ensure_ascii=False))\n"
        "        os.replace(tmp, p)\n"
        "    print('ok %d' % n)\n"
    )
    return f"{shlex.quote(code)} {shlex.quote(path)}"


def clear_overrides(hid: str, *, home: str | None = None, borrow_only: bool = False, skip=(),
                    ssh_fn=None, target: dict | None = None) -> tuple[bool, str]:
    """清理共享盘上 hosts/<hid>/overrides.json：借用归还连不上目标机时删整份（borrow_only=False）；
    换人或开机接成自用主机时只清遗留借用指令 guest 与 target=borrow 的 deadline（borrow_only=True，保留其它主机级指令）。
    target 给了就在该主机上直接执行；否则经 config.home_host 从在用主机（排除 skip）里依次挑共用 worker_home 的代办，
    连不上（rc == 255）顺延试下一台。返回 (是否清理成功, 说明或原因)，不抛错阻塞归还与开机。"""
    LS.need("清理临时指令")
    run = ssh_fn or ssh
    skip_ids = {str(x) for x in skip}
    if target is not None:
        h = host_defaults(dict(target))
        home = home or h["worker_home"]
        cands = [h]
    else:
        act = [h for h in active_hosts() if str(h["id"]) not in skip_ids]
        if home is None:
            home = next((h["worker_home"] for h in all_hosts() if str(h["id"]) == str(hid)), None)
        if not home:
            return False, f"不知道 {hid} 的 worker 目录"
        cands, rest = [], list(act)
        me = next((h for h in rest if str(h["id"]) == str(hid) and home_host(home, [h])), None)
        if me is not None:
            cands.append(me)
            rest.remove(me)
        while (o := home_host(home, rest)) is not None:
            cands.append(o)
            rest.remove(o)
    if not cands:
        return False, f"没有与 {hid} 共用 worker 目录（{home}）的在用主机"
    ovr = f"{home.rstrip('/')}/hosts/{hid}/overrides.json"
    last_err = ""
    for c in cands:
        cmd = f"{shlex.quote(c['python'])} -c {_clear_borrow_cmd(ovr)}" if borrow_only else f"rm -f {shlex.quote(ovr)}"
        rc, out = run(c, cmd)
        if rc == 0:
            if borrow_only:
                m = re.search(r"ok (\d+)", out or "")
                n = int(m.group(1)) if m else 0
                return True, f"已清 {n} 条遗留借用指令" if n else "无遗留借用指令"
            return True, f"经 {c['id']} 删掉 overrides.json" if str(c["id"]) != str(hid) else "已删 overrides.json"
        last_err = f"{c['id']}：{(out or '').strip() or f'退出码 {rc}'}"
        if rc != 255:
            break
    return False, last_err or "没有可用的代办主机"


CHANGE = ("start", "stop", "add", "rm", "limit")     # 改 worker 的子命令（备用时不做）


def run_out(sub: str, hid: str, args: list[str], gpus: str | None = None, home: str | None = None, **opts) -> tuple[int, str]:
    """worker 的部署拉起、停、看状态、登记队列：返回 (退出码, 输出)。add 登记到这台（别的主机上同一条清单的登记搬过来，页面上的「搬到…」就是它）；
    rm 对已归还的主机经共用 worker_home 的在用主机代办（via；home 同 via）；算力池（pool）的 add、rm 都经 via 代办。备用的看板服务上只许看（status、fit），改 worker 的到主那台做（#163）。"""
    if sub == "add":
        try:
            queue_stop_path(opts.get("stop"))
        except ValueError as e:
            return 2, str(e)
    if sub in CHANGE:
        LS.need(f"worker {sub}")
    h = via(hid, home) if sub == "rm" or hid == POOL else host_conf(hid)
    home, py = h["worker_home"], h["python"]
    w = f"env {worker_env(h)} {shlex.quote(py)} {shlex.quote(home + '/labmon_worker.py')}"
    hq = shlex.quote(hid)
    if sub == "start":
        guest = h.get("borrow") and not h["borrow"].get("swap")                  # 直接借来的：不在对方的容器里扫共享盘
        sh = f" --shared {shlex.quote(h['shared'][0])}" if h["shared"] and not guest else ""      # 共享盘用量由这台的 worker 统计
        # 一条连接：把 main 上已提交的 labmon_worker.py、worker/*.py 与停启保活的 ka.sh 打成 tar.gz 经标准输入送出，
        # 先在 worker_home 下建临时目录解包（失败则清理退出、不破坏现有文件），再替换 worker/*.py、清掉已删模块与旧 __pycache__、替换 ka.sh 与入口脚本，
        # 最后后台拉起（--replace 让旧 worker 退出、由新进程接管任务）。后台那句单独写、不接在 cd 或 && 后面，否则 ssh 会挂到进程结束。
        # START_CHECK_S 秒后看它还在不在：启动即崩的（输出接的是 /dev/null）带上 worker.log 末尾、退出码 3，调用方报“拉起失败”而不是“已拉起”
        log = shlex.quote(f"{home.rstrip('/')}/hosts/{hid}/worker.log")
        hq_home = shlex.quote(home)
        b64, pkg_names = worker_bundle()
        if pkg_names:
            keep = " " + " ".join(pkg_names) + " "
            sync_pkg = (
                f"mkdir -p {hq_home}/worker && "
                f'for f in "$d"/worker/*.py; do [ -f "$f" ] || continue; mv "$f" {hq_home}/worker/ || exit 1; done && '
                f'for f in {hq_home}/worker/*.py; do [ -f "$f" ] || continue; case {shlex.quote(keep)} in *" $(basename "$f") "*) ;; *) rm -f "$f";; esac; done && '
                f"rm -rf {hq_home}/worker/__pycache__ && "
            )
        else:
            sync_pkg = ""
        remote = (
            f"mkdir -p {hq_home} && d=$(mktemp -d {hq_home}/.deploy.XXXXXX) || exit 1\n"
            f'if ! (base64 -d > "$d/.pkg.tgz" && tar -xzf "$d/.pkg.tgz" -C "$d" && {sync_pkg}'
            f'mv "$d/ka.sh" {shlex.quote(home + "/ka.sh")} && '
            f'mv "$d/labmon_worker.py" {shlex.quote(home + "/labmon_worker.py")}); then rm -rf "$d"; exit 1; fi\n'
            f'rm -rf "$d"\n'
            f"setsid nohup {w} run --host {hq} --replace{sh} > /dev/null 2>&1 < /dev/null &\n"
            f"sleep {START_CHECK_S}; o=$({w} ls --host {hq}); echo \"$o\"\n"
            f"case \"$o\" in worker：运行中*) ;; *) echo '---- worker.log 末尾'; tail -n 12 {log}; exit 3;; esac"
        )
        rc, out = ssh(h, remote, b64)
        dirty = subprocess.run(["git", "-C", str(HERE), "diff", "--quiet", DEPLOY_REF, "--", "labmon_worker.py", "worker", "ka.sh"]).returncode
        return rc, (f"（工作区的 labmon_worker.py、worker/ 或 ka.sh 与 {DEPLOY_REF} 上的不同，部署的是 {DEPLOY_REF} 上已提交的版本）\n" if dirty else "") + out
    extra = " ".join(shlex.quote(a) for a in args) + (f" --gpus {shlex.quote(gpus)}" if gpus else "")
    extra += " --idle" if opts.pop("idle", False) else ""
    extra += " --list" if opts.pop("list", False) else ""    # fit --list（#35 B）：位置参数是 ROOT JOBS，hosts 是看哪几台
    extra += " --pool" if opts.pop("pool", False) else ""    # add：算力池放过去的（#79），登记里记 pool
    extra += "".join(f" --{k.replace('_', '-')} {shlex.quote(v)}" for k, v in opts.items() if v)
    rc, out = ssh(h, f"{w} {'ls' if sub == 'status' else sub} {extra} --host {hq}")   # 位置参数要在 --host 前
    if opts.get("expect_project") and rc != 0 and "unrecognized arguments: --expect-project" in out:
        return rc, "这台主机的 worker 还不支持按项目核对登记，请让管理员更新 worker 后再试"
    return rc, out
