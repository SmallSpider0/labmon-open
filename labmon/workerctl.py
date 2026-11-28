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
from pathlib import Path, PurePosixPath

from . import lan as L
from . import lease as LS
from .config import POOL, ConfigError, active_hosts, all_hosts, home_host, home_key, host_defaults, hosts_file, is_pool_id, is_standalone, pool_home, shell_path, user_hosts
from .state import snapshot

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


def via_candidates(hid: str, home: str | tuple | None = None) -> list[dict]:
    """返回可代办 hid 登记操作的主机候选列表（在线且 worker 运行中的排最前，供 via 与 run_out 连接失败回退共用）：
    - 在用主机返回它自己；
    - 已归还、清单已无或共有池入口（is_pool_id）返回同共享域的所有在用主机（非独立主机），优先按全局快照把在线主机排前。"""
    act = active_hosts()
    if any(str(h["id"]) == hid for h in act):
        return [host_conf(hid)]
    if is_pool_id(hid) and home is None:                 # 共有型算力池（#24、#212）：登记在对应池的 worker_home 下，经共用该目录的在用主机代办
        home = pool_home(hid)
        if home is None:
            raise ConfigError("主机清单里没有可用主机：算力池的登记没有地方放")
    old_h = next((h for h in all_hosts() if str(h["id"]) == str(hid)), None)
    if old_h and is_standalone(old_h):
        raise ConfigError(f"{hid} 是独立主机且已不在用，没有共享盘代办主机：它的登记现在动不了")
    key = home_key(old_h) if old_h else (("shared", home.rstrip("/")) if isinstance(home, str) else home)
    cands, rest = [], list(act)
    if key:
        while (o := home_host(key, rest)) is not None:
            cands.append(host_conf(str(o["id"])))
            rest.remove(o)
    if not cands:
        show = key[-1] if isinstance(key, tuple) else (key or "不知道在哪")
        raise ConfigError(f"{hid} 已不在用，又没有与它共用 worker 目录（{show}）的在用主机：它的登记现在动不了")
    snap_hosts = {str(e.get("id")): e for e in (snapshot() or {}).get("hosts") or []}
    if snap_hosts:
        cands.sort(key=lambda h: 0 if (snap_hosts.get(str(h["id"])) and not snap_hosts[str(h["id"])].get("error")
                                       and snap_hosts[str(h["id"])].get("worker_up")) else 1)
    return cands


def via(hid: str, home: str | tuple | None = None) -> dict:
    """在哪台上操作这台目录下的登记（worker rm 与共有池 add/rm）：返回 via_candidates 的首选主机。"""
    return via_candidates(hid, home)[0]


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
    if h.get("ssh"):
        try:
            cmd = L.host_ssh_argv(h) + [remote]
        except ConfigError as e:
            return 255, str(e)
    else:
        cmd = ["bash", "-c", remote]     # 没有 ssh：本机（测试用）
    r = (L.ssh_run if h.get("ssh") else subprocess.run)(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def worker_env(h: dict) -> str:
    """把主机的部署路径（ka_bin、data_root、local_dir）、独立/自动接入主机标记与外部占卡共享策略交给 worker；取值按 shell 参数安全转义（支持 ~/ 展开）。"""
    pairs = [
        ("KA_BIN", None if h.get("member_auto") else h.get("ka_bin")),
        ("LABMON_DATA_ROOT", h.get("data_root")),
        ("LABMON_LOCAL", h.get("local_dir")),
    ]
    parts = [f"{name}={shell_path(str(value))}" for name, value in pairs if value]
    if h.get("member_auto"):
        parts.append("LABMON_NO_KA=1")
    if h.get("member_restrict"):
        parts.append("LABMON_RESTRICT_PROJECTS=1")
    if is_standalone(h):
        parts.append("LABMON_STANDALONE=1")
        if h.get("allow_outside_root"):
            parts.append("LABMON_ALLOW_OUTSIDE_ROOT=1")
    if h.get("allow_shared_gpu"):
        parts.append("LABMON_ALLOW_SHARED_GPU=1")
    if h.get("cpu_quota") is not None:
        parts.append(f"LABMON_CPU_QUOTA={shlex.quote(str(h['cpu_quota']))}")
    if h.get("mps_vram_gb") is not None:
        parts.append(f"LABMON_MPS_VRAM_GB={shlex.quote(str(h['mps_vram_gb']))}")
    return " ".join(parts)


def project_init(hid: str, name: str | None) -> tuple[int, str]:
    """一条连接：建规范目录（和 projects/<名称>/），更新根目录 README，列出现有的环境、数据集与权重。返回 (退出码, 输出)。
    独立主机（is_standalone）建立 labmon/.tmp 与 projects/<名称>/{code,queues,runs} 隔离分层并写独立机版 README。"""
    h = host_conf(hid)
    root = h["data_root"].rstrip("/")
    rq = shell_path(root)
    if is_standalone(h):
        proj = (f"projects/{shlex.quote(name)}/code projects/{shlex.quote(name)}/queues projects/{shlex.quote(name)}/runs"
                if name else "projects")
        remote = (f"mkdir -p {rq} && cd {rq} && mkdir -p labmon envs datasets weights archive .tmp {proj} && "
                  f"cat > README.md.new && mv README.md.new README.md && "
                  f"for d in envs datasets weights; do echo \"$d/：$(ls $d | tr '\\n' ' ')\"; done"
                  + (f" && echo {shlex.quote(f'已建 {root}/projects/{name}/（含 code/、queues/、runs/）')}" if name else ""))
        return ssh(h, remote, committed("data_root_standalone_readme.md"))
    proj = f"projects/{shlex.quote(name)}" if name else "projects"
    remote = (f"cd {rq} && mkdir -p envs datasets weights archive {proj} && "
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
        w = f"env {worker_env(h)} {shlex.quote(h['python'])} {shell_path(h['worker_home'].rstrip('/') + '/labmon_worker.py')}"
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


def _clear_queues_cmd(home: str, hid: str, *, keep_projects: set[str] | None = None,
                      drop_projects: set[str] | None = None, since: float | None = None,
                      rm_sched: bool = True) -> str:
    """在远端用 Python 3.8+ 于共享盘 reg.lock 锁内清理 hosts/<hid>/queues/*.json 遗留登记（#224）：
    - keep_projects 不为 None（普通持有人或未开账号）：删除 project 不在 keep_projects 或损毁的登记；
    - keep_projects 为 None（管理员持有人）：按 drop_projects / since 删除借用方登记，若两者均未提供则清空全部；
    - rm_sched 为真时一并删除 hosts/<hid>/sched.json，防止新拉起 worker 在下一轮采集前读到旧主人的策略。
    输出 qclean=ok:<清理条数>（不含独立纯数字，避免干扰 du 输出解析）。"""
    spec = {
        "home": home.rstrip("/"),
        "hid": str(hid),
        "keep": sorted(keep_projects) if keep_projects is not None else None,
        "drop": sorted(drop_projects) if drop_projects else None,
        "since": float(since) if since is not None else None,
        "rm_sched": bool(rm_sched),
    }
    code = (
        "import fcntl, glob, json, os, sys\n"
        "cfg = json.loads(sys.argv[1])\n"
        "home, hid = cfg['home'], cfg['hid']\n"
        "hd = os.path.join(home, 'hosts', hid)\n"
        "qd = os.path.join(hd, 'queues')\n"
        "n = 0\n"
        "if os.path.isdir(qd):\n"
        "    lk_path = os.path.join(home, 'reg.lock')\n"
        "    os.makedirs(home, exist_ok=True)\n"
        "    with open(lk_path, 'w') as lk:\n"
        "        try:\n"
        "            fcntl.flock(lk, fcntl.LOCK_EX)\n"
        "        except OSError:\n"
        "            pass\n"
        "        keep = set(cfg['keep']) if cfg['keep'] is not None else None\n"
        "        drop = set(cfg['drop']) if cfg['drop'] is not None else None\n"
        "        since = cfg['since']\n"
        "        for f in glob.glob(os.path.join(qd, '*.json')):\n"
        "            rm = False\n"
        "            try:\n"
        "                reg = json.loads(open(f).read())\n"
        "                proj = reg.get('project') if isinstance(reg, dict) else None\n"
        "            except (OSError, ValueError):\n"
        "                proj = None\n"
        "                rm = True\n"
        "            if not rm:\n"
        "                if keep is not None:\n"
        "                    rm = proj not in keep\n"
        "                elif drop is not None or since is not None:\n"
        "                    if drop and proj in drop:\n"
        "                        rm = True\n"
        "                    elif since is not None:\n"
        "                        try:\n"
        "                            rm = os.stat(f).st_mtime >= since - 5\n"
        "                        except OSError:\n"
        "                            rm = False\n"
        "                else:\n"
        "                    rm = True\n"
        "            if rm:\n"
        "                try:\n"
        "                    os.remove(f)\n"
        "                    n += 1\n"
        "                except OSError:\n"
        "                    pass\n"
        "if cfg['rm_sched']:\n"
        "    sp = os.path.join(hd, 'sched.json')\n"
        "    if os.path.exists(sp):\n"
        "        try:\n"
        "            os.remove(sp)\n"
        "        except OSError:\n"
        "            pass\n"
        "print('qclean=ok:%d' % n)\n"
    )
    return f"{shlex.quote(code)} {shlex.quote(json.dumps(spec, ensure_ascii=False))}"


def stop_managed_worker(h: dict, reason: str, *, ssh_fn=None, timeout: float | None = 15) -> tuple[int, str]:
    """停止组员自动接入服务器（member_auto）的 worker，并写入带 auto:<reason> 前缀的 stopped 标记（#224）：
    - 仅停进程、绝不上传或替换 worker 代码；
    - 与管理员手动 labmon worker stop 留下的 manual:<ts> 标记区分开：重新开通账号或切回接入调度后，
      overview.relaunch 会自动识别 auto:* 并恢复拉起，而手动 stop 仍保持不自动拉起；
    - peer worker 仍见 hosts/<id>/stopped 存在，停机期间不会误当在用 peer。"""
    LS.need(f"停用受管 worker（{reason}）")
    hc = host_defaults(dict(h))
    hid = str(hc["id"])
    we = worker_env(hc)
    w = (f"env {we} " if we else "env ") + f"{shlex.quote(hc['python'])} {shell_path(hc['worker_home'].rstrip('/') + '/labmon_worker.py')}"
    sf = shell_path(f"{hc['worker_home'].rstrip('/')}/hosts/{hid}/stopped")
    hd = shell_path(f"{hc['worker_home'].rstrip('/')}/hosts/{hid}")
    tag = f"auto:{reason}"
    cmd = f"{w} stop --host {shlex.quote(hid)}; rc=$?; mkdir -p {hd} && printf '%s %s\\n' {shlex.quote(tag)} \"$(date '+%F %T')\" > {sf}; exit $rc"
    run = ssh_fn or ssh
    return run(hc, cmd, timeout=timeout) if ssh_fn is None else run(hc, cmd)


def _cleanup_candidates(hid: str, *, home: str | None = None, skip=(), target: dict | None = None) -> tuple[list[dict], str | None, str | None]:
    """清理共享盘或本机 hosts/<hid>/ 下文件时的代办主机候选与 worker_home 唯一选择逻辑（clear_stopped 与 clear_overrides 共用，#236）：
    - target 给了直接返回该主机；
    - 独立主机只允许自身执行（不跨共享域代办）；
    - 共享盘主机优先目标机自身，再经 config.home_host 依次追加同 worker_home 在用主机（排除 skip）。
    返回 (cands, home, err)；无可用候选或未知目录时 cands 为空且 err 给出原因。"""
    if target is not None:
        h = host_defaults(dict(target))
        return [h], (home or h["worker_home"]), None
    skip_ids = {str(x) for x in skip}
    act = [h for h in active_hosts() if str(h["id"]) not in skip_ids]
    known_h = next((h for h in all_hosts() if str(h["id"]) == str(hid)), None)
    if known_h and is_standalone(known_h):
        me = next((h for h in act if str(h["id"]) == str(hid)), None)
        wh = home or known_h["worker_home"]
        cands = [me] if me is not None else []
        return cands, wh, (None if cands else f"没有与 {hid} 共用 worker 目录（{wh}）的在用主机")
    if home is None:
        home = known_h["worker_home"] if known_h else None
    if not home:
        return [], None, f"不知道 {hid} 的 worker 目录"
    cands, rest = [], list(act)
    me = next((h for h in rest if str(h["id"]) == str(hid) and home_host(home, [h])), None)
    if me is not None:
        cands.append(me)
        rest.remove(me)
    while (o := home_host(home, rest)) is not None:
        cands.append(o)
        rest.remove(o)
    if not cands:
        return [], home, f"没有与 {hid} 共用 worker 目录（{home}）的在用主机"
    return cands, home, None


def clear_stopped(hid: str, *, home: str | None = None, skip=(), ssh_fn=None, target: dict | None = None,
                  timeout: float | None = 15) -> tuple[bool, str]:
    """清除远端/共享盘上 hosts/<hid>/stopped 停用标记（#236，切换接入调度/恢复拉起时调用）：
    - 绝不推送或替换 worker 代码，仅执行单条轻量 rm -f；
    - 代办候选统一经 _cleanup_candidates 选择（目标主机优先，连不上时共享盘主机回退同 worker_home 在用主机，独立机严格只连自身）；
    - 返回 (是否清理成功, 说明或原因)，不抛错阻塞开启。"""
    LS.need("清除停用标记")
    run = ssh_fn or ssh
    cands, home, err = _cleanup_candidates(hid, home=home, skip=skip, target=target)
    if err or not home:
        return False, err or f"不知道 {hid} 的 worker 目录"
    sf = shell_path(f"{home.rstrip('/')}/hosts/{hid}/stopped")
    cmd = f"rm -f {sf}"
    last_err = ""
    for cand in cands:
        try:
            rc, out = run(cand, cmd, timeout=timeout) if ssh_fn is None else run(cand, cmd)
        except (OSError, subprocess.TimeoutExpired) as e:
            last_err = f"{cand['id']}：{e}"
            continue
        if rc == 0:
            return True, f"已清除 {hid} 的停用标记"
        last_err = f"{cand['id']}：{((out or '').strip().splitlines() or [f'退出码 {rc}'])[-1][:120]}"
        if rc != 255:
            return False, last_err
    return False, last_err or "无可达主机"


def clear_overrides(hid: str, *, home: str | None = None, borrow_only: bool = False, skip=(),
                    ssh_fn=None, target: dict | None = None, clear_queues: dict | None = None) -> tuple[bool, str]:
    """清理共享盘上 hosts/<hid>/overrides.json：借用归还连不上目标机时删整份（borrow_only=False）；
    换人或开机接成自用主机时只清遗留借用指令 guest 与 target=borrow 的 deadline（borrow_only=True，保留其它主机级指令）。
    clear_queues 非空时在同一条 ssh 中顺带清理 hosts/<hid>/queues 遗留登记与旧 sched.json（#224，一条连接不加额外 ssh）。
    代办候选统一经 _cleanup_candidates 选择，连不上（rc == 255）顺延试下一台。返回 (是否清理成功, 说明或原因)，不抛错阻塞归还与开机。"""
    LS.need("清理临时指令")
    run = ssh_fn or ssh
    cands, home, err = _cleanup_candidates(hid, home=home, skip=skip, target=target)
    if err or not home:
        return False, err or f"不知道 {hid} 的 worker 目录"
    ovr = f"{home.rstrip('/')}/hosts/{hid}/overrides.json"
    q_extra = ""
    if clear_queues is not None:
        q_extra = _clear_queues_cmd(
            home, hid,
            keep_projects=clear_queues.get("keep_projects"),
            drop_projects=clear_queues.get("drop_projects"),
            since=clear_queues.get("since"),
            rm_sched=clear_queues.get("rm_sched", True),
        )
    last_err = ""
    for c in cands:
        cmd = f"{shlex.quote(c['python'])} -c {_clear_borrow_cmd(ovr)}" if borrow_only else f"rm -f {shell_path(ovr)}"
        if q_extra:
            cmd = f"{cmd} && {shlex.quote(c['python'])} -c {q_extra}"
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


def _reset_hist_cmd(home: str, project: str, tag: str | None = None, name: str | None = None) -> str:
    """在远端用 Python 3.8+ 原子更新 <worker_home>/hist_reset.json（#300）：
    在 hist_reset.json.lock 锁内读改写，name 按 base_name 归一化，按 (project, tag, name) 去重只留最新 t（取远端服务器时钟），
    清理超过 90 天的旧规则，原子替换后输出单行 JSON 回执。"""
    spec = {"home": home.rstrip("/"), "project": project, "tag": tag or None, "name": name or None}
    code = (
        "import fcntl, json, os, re, sys, time\n"
        "cfg = json.loads(sys.argv[1])\n"
        "home = os.path.expanduser(cfg['home'])\n"
        "os.makedirs(home, exist_ok=True)\n"
        "p = os.path.join(home, 'hist_reset.json')\n"
        "now = round(time.time(), 3)\n"
        "cutoff = now - 90 * 86400\n"
        "proj = cfg['project'].strip()\n"
        "tag = cfg['tag'].strip() if isinstance(cfg['tag'], str) and cfg['tag'].strip() else None\n"
        "raw_name = cfg['name'].strip() if isinstance(cfg['name'], str) and cfg['name'].strip() else None\n"
        "norm_name = re.sub(r'([_-](s|seed|rep)\\d+)+$', '', raw_name) if raw_name else None\n"
        "with open(p + '.lock', 'w') as lk:\n"
        "    try:\n"
        "        fcntl.flock(lk, fcntl.LOCK_EX)\n"
        "    except OSError:\n"
        "        pass\n"
        "    rules = []\n"
        "    if os.path.exists(p):\n"
        "        try:\n"
        "            d = json.loads(open(p).read() or '{}')\n"
        "            if isinstance(d, dict) and isinstance(d.get('rules'), list):\n"
        "                rules = d['rules']\n"
        "        except (OSError, ValueError):\n"
        "            rules = []\n"
        "    best = {}\n"
        "    for r in rules:\n"
        "        if not isinstance(r, dict):\n"
        "            continue\n"
        "        rp = r.get('project')\n"
        "        if not isinstance(rp, str) or not rp.strip():\n"
        "            continue\n"
        "        rtg = r.get('tag').strip() if isinstance(r.get('tag'), str) and r.get('tag').strip() else None\n"
        "        rnm = re.sub(r'([_-](s|seed|rep)\\d+)+$', '', r.get('name').strip()) if isinstance(r.get('name'), str) and r.get('name').strip() else None\n"
        "        if not rtg and not rnm:\n"
        "            continue\n"
        "        try:\n"
        "            rt = float(r.get('t'))\n"
        "        except (TypeError, ValueError):\n"
        "            continue\n"
        "        if rt < cutoff:\n"
        "            continue\n"
        "        k = (rp.strip(), rtg, rnm)\n"
        "        if k not in best or rt > best[k]:\n"
        "            best[k] = rt\n"
        "    best[(proj, tag, norm_name)] = now\n"
        "    out_rules = [{'project': k[0], 'tag': k[1], 'name': k[2], 't': best[k]}\n"
        "                 for k in sorted(best, key=lambda x: (x[0], x[1] or '', x[2] or ''))]\n"
        "    tmp = '%s.tmp.%d' % (p, os.getpid())\n"
        "    with open(tmp, 'w') as f:\n"
        "        f.write(json.dumps({'rules': out_rules}, ensure_ascii=False))\n"
        "    os.replace(tmp, p)\n"
        "print(json.dumps({'ok': True, 't': now, 'name': norm_name, 'count': len(out_rules)}, ensure_ascii=False))\n"
    )
    return f"{shlex.quote(code)} {shlex.quote(json.dumps(spec, ensure_ascii=False))}"


def reset_hist(hosts: list[dict], project: str, *, tag: str | None = None, name: str | None = None, ssh_fn=None) -> list[dict]:
    """向目标主机所在的 worker_home 原子写入历史峰值水印（#300）：
    - 按 config.home_key 分组去重，同一共享盘只写一次；独立主机各自独立写入；
    - 组内优先挑全局快照里在线且 worker 运行中的主机，SSH 不可达（rc == 255）顺延试下一台；
    - 返回每组结果 [{"hosts": [...], "via": str, "home": str, "rc": int, "t": float|None, "name": str|None, "out": str}]。"""
    LS.need("重置历史峰值水印")
    run = ssh_fn or ssh
    snap_hosts = {str(e.get("id")): e for e in (snapshot() or {}).get("hosts") or []}
    groups: dict[tuple, list[dict]] = {}
    for raw in hosts:
        h = host_defaults(dict(raw))
        key = home_key(h)
        groups.setdefault(key, []).append(h)
    results = []
    for _key, cands in groups.items():
        ids = [str(c["id"]) for c in cands]
        ordered = sorted(
            cands,
            key=lambda h: 0 if (snap_hosts.get(str(h["id"])) and not snap_hosts[str(h["id"])].get("error")
                                and snap_hosts[str(h["id"])].get("worker_up")) else 1,
        )
        home = ordered[0]["worker_home"]
        rc, out, via_id, wt, wname = 255, "", str(ordered[0]["id"]), None, None
        for cand in ordered:
            via_id = str(cand["id"])
            cmd = f"{shlex.quote(cand['python'])} -c {_reset_hist_cmd(cand['worker_home'], project, tag, name)}"
            rc, out = run(cand, cmd)
            if rc == 0:
                for ln in reversed((out or "").splitlines()):
                    ln = ln.strip()
                    if ln.startswith("{"):
                        try:
                            parsed = json.loads(ln)
                            if isinstance(parsed, dict) and parsed.get("ok"):
                                wt = parsed.get("t")
                                wname = parsed.get("name")
                                break
                        except ValueError:
                            pass
                break
            if rc != 255:
                break
        results.append({"hosts": ids, "via": via_id, "home": home, "rc": rc, "t": wt, "name": wname, "out": out})
    return results


def _check_standalone_root(h: dict, root: str) -> str | None:
    """看板侧独立主机路径边界前置校验（与 worker/common.py:queue_root_ok 同口径，防旧 worker 绕过）：
    仅当 data_root 与 root 均为明确路径（或本机无 ssh 可展开）时校验；远端含 ~ 的另由远端 worker 经 realpath 校验。"""
    if not is_standalone(h) or h.get("allow_outside_root"):
        return None
    dr = str(h.get("data_root") or "")
    if not dr:
        return None
    if not h.get("ssh"):
        import os as _os
        dr = _os.path.expanduser(dr)
        rt = _os.path.expanduser(root)
    else:
        if dr.startswith("~") or root.startswith("~"):
            return None
        rt = root
    if ".." in PurePosixPath(rt).parts:
        return (f"独立主机须在 {dr.rstrip('/')}/projects/<项目>/ 下运行，避免日志与产物污染原代码仓库"
                "（如确需原地运行，可在 hosts.toml 设 allow_outside_root = true）")
    proj = PurePosixPath(dr.rstrip("/")) / "projects"
    target = PurePosixPath(rt)
    try:
        rel = target.relative_to(proj)
    except ValueError:
        rel = None
    if not rel or not rel.parts:
        return (f"独立主机须在 {dr.rstrip('/')}/projects/<项目>/ 下运行，避免日志与产物污染原代码仓库"
                "（如确需原地运行，可在 hosts.toml 设 allow_outside_root = true）")
    return None


def run_out(sub: str, hid: str, args: list[str], gpus: str | None = None, home: str | None = None, **opts) -> tuple[int, str]:
    """worker 的部署拉起、停、看状态、登记队列：返回 (退出码, 输出)。add 登记到这台（别的主机上同一条清单的登记搬过来，页面上的「搬到…」就是它）；
    rm 对已归还的主机经共用 worker_home 的在用主机代办（via；home 同 via）；算力池（pool）的 add、rm 都经 via 代办。备用的看板服务上只许看（status、fit），改 worker 的到主那台做（#163）。"""
    if sub == "add":
        try:
            queue_stop_path(opts.get("stop"))
        except ValueError as e:
            return 2, str(e)
    if sub in CHANGE or (sub == "clean" and opts.get("yes")):
        LS.need(f"worker {sub}")
    cands = via_candidates(hid, home) if (sub == "rm" or is_pool_id(hid)) else [host_conf(hid)]
    h = cands[0]
    if sub == "add" and not is_pool_id(hid) and args:
        if bad := _check_standalone_root(h, args[0]):
            return 2, bad
    hq = shlex.quote(hid)
    def _wcmd(hc: dict) -> str:
        we = worker_env(hc)
        return (f"env {we} " if we else "env ") + f"{shlex.quote(hc['python'])} {shell_path(hc['worker_home'].rstrip('/') + '/labmon_worker.py')}"
    if sub == "start":
        deploy = opts.pop("deploy", True)
        home = h["worker_home"]
        w = _wcmd(h)
        guest = (h.get("borrow") and not h["borrow"].get("swap")) or h.get("member_auto")   # 直接借来或自动接入的组员服务器：不在容器里扫全组共享盘
        sh = f" --shared {shlex.quote(h['shared'][0])}" if h["shared"] and not guest and not is_standalone(h) else ""      # 共享盘用量由这台的 worker 统计
        sa = " --standalone" if is_standalone(h) else ""
        # 一条连接：把 main 上已提交的 labmon_worker.py、worker/*.py 与停启保活的 ka.sh 打成 tar.gz 经标准输入送出，
        # 先在 worker_home 下建临时目录解包（失败则清理退出、不破坏现有文件），再替换 worker/*.py、清掉已删模块与旧 __pycache__、替换 ka.sh 与入口脚本，
        # 最后后台拉起（--replace 让旧 worker 退出、由新进程接管任务）。后台那句单独写、不接在 cd 或 && 后面，否则 ssh 会挂到进程结束。
        # deploy="if_missing"（主服务后台自动拉起 member_auto，#224）：远端已有 labmon_worker.py 时绝不覆盖共享盘代码也不 --replace，
        # 仅在首次尚未安装时落地 main 已提交版本，彻底将常规自动拉起与管理员内网代码升级分离。
        # START_CHECK_S 秒后看它还在不在：启动即崩的（输出接的是 /dev/null）带上 worker.log 末尾、退出码 3，调用方报“拉起失败”而不是“已拉起”
        log = shell_path(f"{home.rstrip('/')}/hosts/{hid}/worker.log")
        hq_home = shell_path(home.rstrip("/"))
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
        b64_dec = "(base64 -d < /dev/null >/dev/null 2>&1 && base64 -d || base64 -D)"
        if deploy == "if_missing":
            remote = (
                f"mkdir -p {hq_home}\n"
                f"if [ ! -f {hq_home}/labmon_worker.py ]; then\n"
                f"  d=$(mktemp -d {hq_home}/.deploy.XXXXXX) || exit 1\n"
                f'  if ! ({b64_dec} > "$d/.pkg.tgz" && tar -xzf "$d/.pkg.tgz" -C "$d" && {sync_pkg}'
                f'mv "$d/ka.sh" {hq_home}/ka.sh && '
                f'mv "$d/labmon_worker.py" {hq_home}/labmon_worker.py); then rm -rf "$d"; exit 1; fi\n'
                f'  rm -rf "$d"\n'
                f"else\n"
                f"  cat >/dev/null\n"
                f"fi\n"
                f"o=$({w} ls --host {hq} 2>/dev/null)\n"
                f"case \"$o\" in worker：运行中*) echo \"$o\"; exit 0;; esac\n"
                f"if {w} run --help 2>&1 | grep -q -- --daemon; then\n"
                f"  {w} run --host {hq} --daemon{sh}{sa}\n"
                f"elif command -v setsid >/dev/null 2>&1; then\n"
                f"  setsid nohup {w} run --host {hq}{sh}{sa} > /dev/null 2>&1 < /dev/null &\n"
                f"else\n"
                f"  nohup {w} run --host {hq}{sh}{sa} > /dev/null 2>&1 < /dev/null &\n"
                f"fi\n"
                f"sleep {START_CHECK_S}; o=$({w} ls --host {hq}); echo \"$o\"\n"
                f"case \"$o\" in worker：运行中*) ;; *) echo '---- worker.log 末尾'; tail -n 12 {log}; exit 3;; esac"
            )
        else:
            remote = (
                f"mkdir -p {hq_home} && d=$(mktemp -d {hq_home}/.deploy.XXXXXX) || exit 1\n"
                f'if ! ({b64_dec} > "$d/.pkg.tgz" && tar -xzf "$d/.pkg.tgz" -C "$d" && {sync_pkg}'
                f'mv "$d/ka.sh" {hq_home}/ka.sh && '
                f'mv "$d/labmon_worker.py" {hq_home}/labmon_worker.py); then rm -rf "$d"; exit 1; fi\n'
                f'rm -rf "$d"\n'
                f"{w} run --host {hq} --replace --daemon{sh}{sa}\n"
                f"sleep {START_CHECK_S}; o=$({w} ls --host {hq}); echo \"$o\"\n"
                f"case \"$o\" in worker：运行中*) ;; *) echo '---- worker.log 末尾'; tail -n 12 {log}; exit 3;; esac"
            )
        rc, out = ssh(h, remote, b64)
        dirty = subprocess.run(["git", "-C", str(HERE), "diff", "--quiet", DEPLOY_REF, "--", "labmon_worker.py", "worker", "ka.sh"]).returncode
        return rc, (f"（工作区的 labmon_worker.py、worker/ 或 ka.sh 与 {DEPLOY_REF} 上的不同，部署的是 {DEPLOY_REF} 上已提交的版本）\n" if dirty and deploy != "if_missing" else "") + out
    extra = " ".join(shlex.quote(a) for a in args) + (f" --gpus {shlex.quote(gpus)}" if gpus else "")
    extra += " --idle" if opts.pop("idle", False) else ""
    extra += " --list" if opts.pop("list", False) else ""    # fit --list（#35 B）：位置参数是 ROOT JOBS，hosts 是看哪几台
    extra += " --json" if opts.pop("json", False) else ""    # fit --list --json（#25、#212）：共有池单连接评估队首
    pool_opt = opts.pop("pool", None)
    if pool_opt:                                             # add：共有池落机标记池 id（#212）
        extra += f" --pool {shlex.quote(pool_opt if isinstance(pool_opt, str) else POOL)}"
    extra += " --cache" if opts.pop("cache", False) else ""
    extra += " --scratch" if opts.pop("scratch", False) else ""
    extra += " --dry-run" if opts.pop("dry_run", False) else ""
    extra += " --yes" if opts.pop("yes", False) else ""
    extra += "".join(f" --{k.replace('_', '-')} {shlex.quote(str(v))}" for k, v in opts.items() if v)
    rc, out = 255, ""
    for cand in cands:
        if sub == "stop":
            # 经看板执行手动 labmon worker stop：兼容远端未升级的旧版 worker，停完统一写入 manual:<ts>（#236）
            sf = shell_path(f"{cand['worker_home'].rstrip('/')}/hosts/{hid}/stopped")
            hd = shell_path(f"{cand['worker_home'].rstrip('/')}/hosts/{hid}")
            cmd = f"{_wcmd(cand)} stop {extra} --host {hq}; rc=$?; if [ $rc -eq 0 ]; then mkdir -p {hd} && printf 'manual:%s\\n' \"$(date '+%F %T')\" > {sf}; fi; exit $rc"
        else:
            cmd = f"{_wcmd(cand)} {'ls' if sub == 'status' else sub} {extra} --host {hq}"   # 位置参数要在 --host 前
        rc, out = ssh(cand, cmd)
        if rc != 255:
            break
    if opts.get("expect_project") and rc != 0 and "unrecognized arguments: --expect-project" in out:
        return rc, "这台主机的 worker 还不支持按项目核对登记，请让管理员更新 worker 后再试"
    return rc, out
