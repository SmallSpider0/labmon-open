"""worker 主类 Worker：整合各职责 Mixin、临时指令、任务启停接管、单元申请与主循环。"""
import glob
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import traceback

from . import common as _C
from . import pure as _P
from . import queue as _Q
from . import disk as _D
from . import measure as _M
from . import keepalive as _K
from . import deps as _DP
from . import sched as _S
from . import forecast as _FC
from . import offers as _OF

class Worker(_K.KeepaliveMixin, _DP.DepsMixin, _S.SchedMixin, _FC.ForecastMixin, _OF.OffersMixin, _M.MeasureMixin):
    def __init__(self, host, shared=None):
        self.host = host
        self.p = _C.Paths(host)
        self.disk = _D.Disk(shared, host) if shared else None   # 共享盘用量（只有拿着租约的那台在扫）
        self.ldisk = _D.LocalDisk(host)          # 容器根盘开机以来的新写入与 labmon 目录的用量（每台扫自己的，#149）
        self.running = {}     # 请求 key（run_key：串行清单是队列 key、并行清单是 队列 key|任务名；单元是 slot:<id>）→ 运行中的任务或单元
        self.par_t = {}       # 队列 key → 这条清单上次往上加行的时刻（并行清单两次加行至少隔 PAR_STEP_S，par_gate；补位不算，#176）
        self.par_hi = {}      # 队列 key → (近来同时跑到过的行数, 最后一次跑着这么多行的时刻)：补到它以内是补位（par_level、par_note，#176）
        self.active = set()   # 本次已写 lock、记过“队列启动”的队列
        self.noted = {}       # 请求 key → 上次写进 worker.log 的等待原因（只在变化时记）
        self.ext = {}         # 旧 gpu_queue.sh 正在跑的任务（只统计用量，不管）
        self.excl_hold = {}   # 请求 key → {cards: 为等腾空的独占任务留着的卡（UUID 列表；整机独占为 ["*"]），until: 预计腾空的时刻（hold_until）}
        self.wsince = {}      # 请求 key → (在等的任务, 开始等的时刻, 上一轮是不是被临时指令挡着)
        self.info, self.gprocs, self.gp, self.cpu_prev = {}, [], [], None   # gp：这一轮 sample 认过归属的 GPU 进程（带任务引用）
        self.cpu_n = 0                        # labmon 之外的进程连着几轮占 VOID_CPU 核以上（整机独占放行前看，一轮的尖峰不算）
        self.local, self.local_t = None, 0   # 待跑任务声明的本地依赖（续租前看会不会丢）
        self.envc = {}                        # 队列 key → (.env 的 fsig, 变量)
        self.storage = None                   # 本地盘：各项目的缓存与单格临时、labmon 之外的新写入（LocalDisk.summary）；共享盘上各项目目录（看板按它把共享盘用量分到项目）
        self.gate, self.failcnt, self.ovr, self.seen = False, None, [], set()
        self.uwin = {}                        # 卡 UUID → 近 UTIL_WIN_S 秒的 (时刻, 利用率)：「在用时多高」，保活在的那几轮不记（util10，fit 用）
        self.bwin = {}                        # 卡 UUID → 近 BUSY_WIN_S 秒的 (时刻, 利用率)：「吃满程度」，只有保活的那几轮记 0（gpu_busy，页面用，#51）
        self.bka = {}                         # 卡 UUID → 这一轮只有保活时保活顶出来的原值（gpu_busy 的 ka）
        self.own_cpu = None                   # worker 自己的 CPU 用时（算 labmon 之外的 CPU 占用）
        self.other = {"vram": {}, "ram": None, "cpu": None, "ka": {}, "gid": {}, "ka_cpu": 0.0}   # labmon 之外的用量：各卡显存 MB、内存 GiB（收不回的，不算页缓存）、CPU 核
        # （内存与 CPU 不含任何保活的进程树：借来的主机上就是对方干活的用量，与算预留的组内探针同口径，#37）、各卡上 ka.sh 停不了的保活（宿主机 PID，只在借来的主机上认）、
        # 各卡上 labmon 之外的 GPU 进程（宿主机 PID）、ka.sh 停不了的保活进程树占的核（cpu_room 另扣；只在 worker 内存里，不写进状态）
        self.back, self.back_last = {}, {}    # 借来的主机：对方此刻超出预留的几项（卡 UUID、ram、cpu → 说明），各项最近一次超出的时刻
        self.owner = []                       # 其中算主人回来的（去掉只因 ka.sh 停不了的保活超出的卡，#25）：状态的 guest.owner，算力池借来的整台不放
        self.cpu_hot = 0                      # 借来的主机：对方的 CPU 连着几轮超出预留（两轮才算超出，一轮的尖峰不算）
        self.ka_ticks = {}                    # 上一轮保活进程树的 CPU 累计之和 {树: (时刻, ticks)}（ka_cores；"all" 所有保活合起来，(pid, 启动时刻) 各棵保活树分开记）
        self.ka_roots = {}                    # 上一轮认作保活的 GPU 进程 {pid: (启动时刻, 是不是 ka.sh 停不了的)}：这一轮没认出来（nvidia-smi 超时等）但还在的照样算保活
        self.tight = None                     # 借来的主机快撞上限的说明（guest_tight）
        self.away_end = None                  # 借来的主机上对方说好不用、却回来了：{until: 作废的是到几点的那次, t: 何时, why}（end_away）
        self.away_base = None                 # 这次说好不用：开始时刻、开始时对方已有的 GPU 进程、之后新进程连着出现的轮数 {until, t0, pids, n}（guest_scan）
        self.oom_n, self.oom_ours, self.hit = None, 0, None   # 容器 oom_kill 计数（上一轮）、其间被杀的我们的任务数、最近一次杀了别人进程的时刻
        self.squeezed = {}                    # 任务名 → 借来的主机上被对方挤掉、重新排队的次数
        self.hist_cache, self.hist, self.hist_v = {}, [], 0   # 各主机的用量记录（load_hist），hist_v 每次有变化加一
        self.hinted, self.hint_v, self.hint_ix = set(), -1, None   # 已按跑完提前重扫过的用量行（disk_ends）、那时的 hist_v 与索引
        self.reload_hist()
        self.qlists, self.durs, self.durs_key = {}, {}, None   # 各清单的 (fsig, [(名称, 资源提示)])、预计时长（queue_durs）
        self.durk = {}                                        # (项目, 任务, 清单估时, tag, 清单 root, queue, 同卡任务数 k) → duration 的结果（dur_k；queue_durs 重算时清掉）
        self.qest = {}                        # 队列 key → {任务: 资源估计（estimate）}（queue_durs 与预计时长一起算）
        self.plan, self.plan_key = {"t": 0, "lanes": []}, None   # 推演结果（forecast）与算它时的输入
        self.starts = {}                      # (请求 key, 任务) → 推演里预计何时开始（forecast 算，写进等待列表）
        self.ends = {}                        # (项目, 任务) → 预计结束（forecast 汇的，依赖查它，见 dep_end）
        self.dep_idx, self.dead = {}, set()   # 各任务的状态 {(项目, 任务): pending|done|failed}（dep_state）、其中跑失败的
        self.marks, self.todo = {}, []        # 本机各清单标完的 {(项目, 任务): done|failed} 与还会跑的（list_state；todo 写进状态给别的主机）
        self.dep_where, self.dep_lanes = {}, []   # 还会跑的任务在哪 {(项目, 任务): (主机（本机为 None）, 状态已多久没更新, 清单暂停的种类)}；清单项（dep_cycle 找环）
        self.dep_miss = {}                    # 依赖等的、哪里都找不到的任务 {(项目, 任务): 起算时刻}：满 DEP_MISS_S 才报找不到（dep_block；写进状态，接管时找回）
        self.eta, self.eta_t, self.eta_m = {}, 0, None            # 看板写来的运行中任务预计结束（load_eta）
        self.hrows, self.hrows_v = [], -1     # 跑完的任务 [(项目, 卡数, 开始, 结束)]（share 按 hist 换算，hist 变了才重算）
        self.peer, self.peer_t = [], 0.0      # 别的主机在跑的任务（peers）
        self.peer_end = {}                    # 别的主机 plan 里各任务的预计结束 {(项目, 任务): 时刻}（peers 一起读）
        self.peer_todo = []                   # 别的主机还会跑的任务 [(主机, 状态已多久没更新, 状态时刻, 清单项)]（各自状态里的 todo，peers 一起读）
        self.pool_heads = []                  # 算力池里待放的各清单的队首（pool_todo 读，pool_view 判依赖，#25）
        self.elsewhere = {}                   # 别的主机放行、这台不动的清单任务 {队列: (主机, 任务)}（run_elsewhere）：变了才记事件
        self.gone = set()                     # 看板说已归还的主机（returned.json，peers 一起读）：worker 不在时 offers 不把它的清单当说不准
        self.peer_st = {}                     # 别的在用主机的状态 {主机: status.json}（没过期、没 worker stop 过的；peers 一起读，offers 用）
        self.olists = {}                      # 别的主机上清单与 .env 的 {路径: (fsig, [(任务, 资源提示)] 或 .env 内容)}（list_of、list_env 缓存）
        self.offer, self.offer_t = [], 0.0    # 能接别处的任务（offers，写进状态给看板汇总挪法建议）与算出它的时刻
        self.bad_reg = set()                  # 坏了的登记文件（registry 跳过，只记一次事件）
        self.reg_cache = {}                   # 本机登记文件 {路径: (fsig, st_mtime, reg 或 Exception)}：未变时不重读 JSON
        self.reg_idle = {}                    # 空置计时与休眠态 {队列 key: {since, sig, sleep}}（#164；重启经 load_prev 找回）
        self.sleep_marks = {}                 # 休眠队列的标记快照 {队列 key: {任务名: done|failed}}（转休眠时记，list_state 零 I/O 合入）
        self.pool_reg_cache = {}              # 算力池登记文件缓存 {路径: (fsig, st_mtime, reg)}（pool_todo 用）
        self.pool_idle = {}                   # 算力池待放清单的空置计时与休眠态 {队列 key: {since, sig, sleep}}（#164）
        self.opars = {}                       # 算力池清单并行数缓存 {路径: (fsig, par)}（pool_todo 用）
        self.peer_regs = {}                   # 别的在用主机（peer_st）登记的清单 {(目录, 清单): [(登记文件修改时间, 主机)]}（peers 一起读，registry 认同一条清单归谁，#95）
        self.yielded = {}                     # 本机登记着、但别的在用主机登记得更晚的清单 {队列 key: 那台主机}（registry 不交给 tick，变了才记事件）
        self.stagers = {}                     # (请求 key, 任务) → 后台拷本地依赖的线程与结果（stage_bg）
        self.pwin, self.plast = [], None      # 主机压力的计数（press_raw）：近 PRESS_WIN 秒的几份、上次记 press.jsonl 时的那份
        self.pfail = False                    # press.jsonl 上次写失败了（连续失败只记一次日志）
        self.pdev, self.pdev_t = None, -_C.PRESS_EVERY   # 本地盘的设备号（local_dev）与上次找它的时刻（没找到时隔 PRESS_EVERY 再找）
        self.slow = None                      # 共享盘此刻慢（nfs_slow）时为 info.press.nfs，否则 None（sample 定）
        self.thr_since = None                 # 这一段 CPU 限流从何时起（thr_step；没在限流为 None，load_prev 找回）
        self.defer = {}                       # 共享盘慢时推迟的事（扫描 "scan"、拷依赖 (请求 key, 任务)）→ 开始推迟的时刻（io_yield）
        self.cpu_block = {}                   # 请求 key → (任务, 开始一直因 CPU 被挡的时刻)：满 CPU_ALERT_S 才在等待项写 cpu_wait
        self.peer_box = []                    # 同一物理机（共享盘 clientaddr 相同）的别的主机在跑的任务 [(主机, 项目, 任务)]（peers 一起读）
        self.prows = [r for r in _C.load_jsonl(self.p.press) if (r.get("t") or 0) >= _C.time.time() - _C.PRESS_KEEP_S]   # 本机近 14 天的压力历史（io_base）
        self.ka_off, self.ka_stop_t, self.ka_hits, self.fight = False, 0.0, [], None   # 要卡的这段里保活已停着、上次停的时刻、近 FIGHT_S 秒被重开的时刻、最近一次被重开（load_prev 找回，#124）
        self.ka_mine = None                   # worker 自己用 ka.sh start 拉起、此刻还在的保活：{g 进程组, born 组长出生时钟, t 启动时刻}（ka_mark；状态的 keepalive.mine，归还时据此登记成看板保活，#102）
        self.ka_fail = None                   # 停保活没停成：{t0 起始, n 次数, why 原因, said 记过事件}，停下了或保活没了清掉（ka_note；load_prev 找回，#102）
        # 成组（gang=，#180）：
        self.gang_local = {}                  # 本机清单里还会跑的成员 {(项目, 任务): {key, gang, n, root, queue}}（list_state，含在跑的）
        self.gctx, self.gsim = {}, {}         # 本机有成员的组此刻怎样（gang_sync 每轮建）与给 place、推演的那份 {(项目, 组名): {at, local}}
        self.gang_hold = {}                   # 请求 key → 成员留着的资源 {key, gang, name, x, cards, until, t, seen, job}（place 记，gang_commit 一起放行）
        self.gang_eta = {}                    # (请求 key, 任务) → 推演里这个成员最早何时放得下（forecast；写进 gang.json 给同伴）
        self.gpeer = {}                       # 主机 → 那台的 gang.json（gang_sync 读；读不到、写到一半的沿用上次的）
        self.gang_ends = []                   # 本机成员跑完、停下的记录（gang.json 里留 GANG_KEEP_S；load_prev 读回）
        self.gang_miss = {}                   # (项目, 组名) → 凑不齐的起算时刻（gang_block：满 DEP_MISS_S 才报找不到）
        self.gang_known = {}                  # ((项目, 组名), (项目, 任务)) → (别的主机上的成员在哪台, 最近何时见到)：留 GANG_LATE_S（gang_lost；写进 gang.json 的 known，load_prev 读回）
        self.gang_doom = {}                   # (项目, 任务) → 本机没开跑、同一次开跑的同伴已失败的成员的原因（gang_lost 找、gang_doomed 记失败）
        self.gang_pub, self.gang_sig = None, None   # 上次写进 gang.json 的成员（没有成员后再写一次空的就停）、gang_poll 上次看到的同伴状态
        self.load_prev()
        self.ka_reg = None
        self.ka_checked, self.ka_idle, self.ka_why = 0.0, 0, None
        self.ka_wait = None                   # 保活被拉起、看门狗正持着平时不持的锁：{t0: 开始等的时刻, said: 满 LOCK_WAIT_S 记过事件}（#38）
        self.procs, self.mine = {}, set()     # 这一轮的进程表（scan_procs）与 labmon 任务的进程：找看门狗用
        self.watch_t, self.watch_seen = 0.0, set()   # 借来的主机上次找看门狗的时刻、记过事件的可能的看门狗
        # 保活由 worker 管的前提：有 ka.env（这台的保活配置）与随 worker 发来的 ka.sh（#105）；缺一样就当这台没配保活（keepalive.ctl 为假）
        env = subprocess.run(["bash", "-c", '. "$1" 2>/dev/null; echo "${AGRI_ROOT:-}"; echo "${AGRI_KA_SCRIPT:+${AGRI_KA_DIR:-}/$AGRI_KA_SCRIPT}"', "_", _C.KA_ENV],
                             capture_output=True, text=True).stdout.split("\n") if os.path.exists(_C.KA_ENV) and os.path.exists(_C.KA_SH) else []
        keepalive_demo = env[0].strip() if env else ""
        self.ka_full = (env[1].strip() if len(env) > 1 else "") or None   # ka.sh 认的保活：python 跑这个脚本（完整路径）；别的保活它停不了
        if keepalive_demo:
            self.ka_reg = os.path.join(keepalive_demo, "queues", "labmon-" + host)
        self.ka_ev = os.path.join(keepalive_demo, "keepalive", "events.log") if keepalive_demo else None   # ka.sh 记的停启与调用者

    # ---- 日志与事件 ----
    def wlog(self, msg):
        with open(self.p.log, "a") as f:
            f.write("%s %s\n" % (_C.now_str(), msg))

    def qlog(self, q, msg):
        with open(os.path.join(q["state"], "queue.log"), "a") as f:
            f.write("%s %s\n" % (_C.now_str(), msg))

    def event(self, kind, msg, **kw):
        """写 worker.log，同时记一条结构化事件（工作台的“最近事件”）。"""
        self.wlog(msg)
        with _C.flock(self.p.events):
            _C.append_jsonl(self.p.events, dict(kw, t=round(_C.time.time()), kind=kind, msg=msg))

    # ---- 临时指令 ----
    def load_overrides(self):
        """读 overrides.json，去掉过期的（并记事件）；pause --now 的按各自的停法停下这个项目正在跑的任务，只做一次（记 applied；
        带 once 的是命令行不带 --host 的 pause --now：停完照样当暂停挡着，等看板写来的 sched.json 里有了这个项目的全局暂停才删（全局的接着挡），
        最多留 ONCE_HOLD_S（看板一直没写来、或全局的还没送到就被 resume 了）。停完立刻删的话，sched.json 还没到，被停的任务下一轮就又放行了）。读改写在文件锁（flock）里：命令行 override add/rm 同时写也不丢。
        返回这些再接上看板写来的项目级暂停、优先（sched_ovr，所有主机生效）。"""
        with _C.flock(self.p.overrides):
            try:
                with open(self.p.overrides) as f:
                    ovr = json.load(f)
            except (OSError, ValueError):
                ovr = []
            keep = [o for o in ovr if not o.get("until") or o["until"] > _C.time.time()]
            global_ovr = _C.sched_ovr(self.p.sched)
            for o in ovr:
                if o not in keep:
                    self.event("override", "临时指令到期：%s" % _C.ovr_text(o), **_C.ovr_fields(o, "expire"))
            now = [o for o in keep if o["kind"] == "pause" and o.get("now") and not o.get("applied")]
            for o in now:
                for j in self.running.values():
                    if j.get("project") == o.get("target") and not _C.is_hold(j["name"]):
                        self.stop_job(j)
                o["applied"] = True
            gp = {o["target"] for o in global_ovr if o["kind"] == "pause"}
            keep = [o for o in keep if not (o.get("once") and o.get("applied") and (o.get("target") in gp or _C.time.time() - (o.get("t") or 0) > _C.ONCE_HOLD_S))]
            if len(keep) != len(ovr) or now:
                _C.write(self.p.overrides, json.dumps(keep, ensure_ascii=False))
        return keep + global_ovr

    def gang_recheck(self, limits, gpus, apps, now):
        """admit 之后、gang_commit 之前：先放掉这一轮没重新留的（place 没在这一轮记过：让位给屏障、不再是队首），它们不再复核，
        否则复核一走 place 就把让位的旧预留刷成这一轮的，屏障一直被它挡着、同伴就绪后它还先开跑。
        再看这一轮留下的：在成员留资源之后放行的（回填：预计在留到的时刻之前跑完）可能用了它留着的资源，此刻它就开不了跑。
        按放行后的快照对它们再走一遍 place：还放得下的照留（就绪），放不下的放掉（gang.json 写 wait 与推演里何时放得下），
        免得同伴见它“已留好”先开跑、它跟不上。"""
        self.gang_hold = {k: h for k, h in self.gang_hold.items() if h["seen"] >= now}
        for k in sorted(self.gang_hold):
            h = self.gang_hold.get(k)              # 两组互让时可能已被前一个放掉
            if h:
                _S.place(h["x"], self.snapshot(limits, gpus, apps), _C.time.time())

    def override_hit(self, r, kinds=None, lasting=False):
        """挡住这个请求的临时指令：(指令种类, 原因)，没有为 None。看板据种类把车道标成暂停（drain 另报给同机各项目）。
        kinds 只看这几种（list_state 标清单暂停只看 pause、drain）。截止（续租、约定归还）不挡占位任务：它不占资源，
        续租、归还打断它也只是重开后接着等（推演里同样不挡，place 先放占位任务再看截止）。
        lasting：只看持久的挡法（截止、drain、pause、借来的主机只给别的项目用），跳过对方此刻超出预留（guest_block）：
        推演（forecast 排除哪些队列）与 offers（几小时后接不接得了）用它，此刻才有的原因推演不模拟（#25）；真放行看全部。"""
        for o in self.ovr:
            if kinds and o["kind"] not in kinds:
                continue
            if o["kind"] == "drain":
                return "drain", "主机 drain 中（%s）" % _C.ovr_text(o)
            if o["kind"] == "guest":
                ps = [x for x in (o.get("target") or "").split(",") if x]
                if ps and r["project"] not in ps:
                    return "guest", "借来的主机只给 %s 用" % "、".join(ps)
                why = None if lasting else self.guest_block()
                if why:
                    return "guest", why
            if o["kind"] == "deadline" and o.get("at") and not _C.is_hold(r.get("name")):
                (d, why), lab, at = self.req_dur(r, src=True, cut=True), _C.deadline_name(o), _C.now_str(o["at"])[5:16]
                if _C.time.time() >= o["at"]:
                    return "deadline", "%s %s 已到，不再放行" % (lab, at)
                if d is None:
                    if o.get("target") == "borrow":   # 约定归还到点不停正在跑的，没估时的照常放（续租会重启容器，不放）
                        continue
                    return "deadline", "%s %s 前：没有估时，不放行" % (lab, at)
                if not _S.fits_before(_C.time.time(), d, o["at"], 1):
                    return "deadline", "%s %s 前跑不完（估 %.0f 分钟 = %s）" % (lab, at, d, why)
            if o["kind"] == "pause" and o.get("target") == r["project"]:
                return "pause", "项目 %s 暂停中（%s）" % (r["project"], _C.ovr_text(o))
        return None

    def stop_job(self, j):
        """按停法停下一个任务：boundary 等它跑完；term 发 SIGTERM；file:<路径> 写项目约定的停止请求文件。停下后重新排队，不记失败。"""
        mode = j.get("stop") or "boundary"
        if mode == "boundary":
            self.event("stop", "暂停：%s 按 boundary 等它跑完（这条队列登记的停法；要马上停就重新登记 --stop term 再 pause --now）" % j["name"],
                       name=j["name"], project=j.get("project"))
            return
        if not self.send_stop(j, mode):
            self.event("stop", "暂停：未能按 %s 停下 %s，原因见 worker 日志" % (mode, j["name"]),
                       name=j["name"], project=j.get("project"))
            return
        j["stopping"] = True
        self.event("stop", "暂停：按 %s 停下 %s" % (mode, j["name"]), name=j["name"], project=j.get("project"))

    def send_stop(self, j, mode):
        """发停止请求：term 发 SIGTERM（清单任务发给整个会话的进程组）；file:<路径> 写项目约定的停止请求文件。"""
        try:
            if isinstance(mode, str) and mode.startswith("file:"):
                text = "labmon：%s 停下 %s\n" % (_C.now_str(), j.get("project"))
                if j.get("kind") == "slot":
                    path = mode[5:] if os.path.isabs(mode[5:]) else os.path.join("/", mode[5:])
                    _C.write(path, text)
                else:
                    root = (j.get("q") or {}).get("root")
                    if not root:
                        raise ValueError("清单缺少 ROOT，无法写停止请求文件")
                    _C.write_queue_stop(root, mode, text)
            else:
                if j.get("kind") != "slot":
                    _C.queue_stop_path(mode)
                (os.killpg if j.get("kind") != "slot" else os.kill)(j["pid"], signal.SIGTERM)
        except (OSError, ValueError) as e:
            self.wlog("停 %s 失败：%s" % (j["name"], e))
            return False
        return True

    def note_stop(self, j, why, how):
        """记下被停下重排的任务（how：void 独占被打扰作废、squeeze 被对方挤掉、gang 跟着同组的停下），留 VOID_KEEP_S；看板据此告诉实验会话。"""
        t = _C.time.time()
        self.voided = [v for v in self.voided if v["t"] > t - _C.VOID_KEEP_S] + [
            {"t": round(t), "name": j["name"], "project": j.get("project"), "kind": j.get("kind", "queue"), "why": why, "how": how}]

    def void_job(self, j, why):
        """独占任务被打扰：结果不能用了，不管停法直接 SIGTERM；清单任务停下后重新排队，单元由申请方重新申请。
        成组的成员被作废记失败、不重排（finish），同组的跟着停下、也记失败（gang_watch）。"""
        j["stopping"], j["void"] = True, why
        self.send_stop(j, "term")
        if not j.get("gang"):
            self.note_stop(j, why, "void")
        self.event("void", "作废 %s：%s，结果不能用；%s" % (j["name"], why, "单元已停下" if j.get("kind") == "slot" else
                                                         "成组的任务记失败，同组的跟着停下" if j.get("gang") else "已停下、重新排队"),
                   name=j["name"], project=j.get("project"), why=why)

    # ---- 任务 ----
    def launch(self, r, gpus):
        """放行：gpus 是快照里选中的卡（place 给的）。从排到队首（单元：申请）到放行等了多久（分钟）记进运行中任务与用量记录，
        看板的调度卡算各项目平均等多久；占位任务不算。"""
        r["wait"] = None if _C.is_hold(r["name"]) or not r.get("since") else round((_C.time.time() - r["since"]) / 60, 1)
        return self.start(r["q"], r, gpus) if r["kind"] == "queue" else self.grant(r, gpus)

    def start(self, q, job, gpus):
        """开跑清单里的一个任务；返回 None。开跑前再核一次归属：这一轮收请求之后登记被 worker add 搬到别的主机或被移除（#95），
        或别的主机已放了这条清单（current 已在，#88），这次不放，返回 (等待原因, False)，下一轮按新的登记与 current 重来。
        核归属、起进程、写 current 在登记归属的锁里（reg_lock，与 worker add 同一把）。"""
        st, lg = q["state"], q["logs"]
        with _Q.reg_lock():                          # 核归属到写下 current 一气做完：搬登记（worker add）插不进来（Codex 审查 #120）
            if q.get("file") and not os.path.exists(q["file"]):
                return "这条清单的登记刚搬到别的主机或被移除，这次不放", False
            mine = {(j["name"], j.get("pid")) for j in self.lane(q["key"])}
            if any(c[:2] not in mine for c in _Q.read_current(st)):   # 收请求之后别的主机已放了这条清单（两台都登记着，#88）：这次不放
                return "这条清单的状态目录里已有别的主机放行的 current，这次不放", False
            if q["key"] not in self.active:
                _C.write(os.path.join(st, "lock"), str(os.getpid()))
                self.qlog(q, "队列启动 PID=%d 清单=%s（labmon worker）" % (os.getpid(), q["jobs"]))
                self.active.add(q["key"])
            rcf = os.path.join(st, job["name"] + ".rc")
            if os.path.exists(rcf):
                os.remove(rcf)
            sd, cd = _Q.scratch_dir(q["project"], job["name"]), _Q.cache_dir(q["project"])
            for d in (sd, cd):
                os.makedirs(d, exist_ok=True)
            env = dict(os.environ, PYTHONUNBUFFERED="1", QUEUE_ROOT=q["root"], LABMON_SCRATCH=sd, LABMON_CACHE=cd)
            env.pop("PYTHONPATH", None)
            env["CUDA_VISIBLE_DEVICES"] = ",".join(c["uuid"] for c in gpus) if job["ngpu"] > 0 else ""
            _M.cpu_env(env, self.cpus_for(job))     # 给它记账的核数：项目拿它设线程数、DataLoader 进程数（nproc 是宿主机的）；没设的 BLAS/OpenMP 线程数缺省同它
            # 命令放子 shell 里跑：清单行里的 exit 不会跳过写退出码。借来的主机上先调高自己的 oom_score_adj（子进程继承）
            script = ("echo %d > /proc/self/oom_score_adj 2>/dev/null; " % _C.GUEST_ADJ if self.guest() else "") \
                + 'set -a; [ -n "$1" ] && . "$1"; set +a; ( eval "$2" ); echo $? > "$3"'
            args = ["bash", "-c", script, "labmon-job:" + job["name"], q["env"] or "", job["cmd"], rcf]
            with open(os.path.join(lg, job["name"] + ".log"), "a") as out:
                out.write("===== %s labmon 开始（GPU %s）=====\n" % (_C.now_str(), ",".join(str(c["i"]) for c in gpus) or "无"))   # 分隔行，见 START_RE
                out.flush()
                pr = subprocess.Popen(args,
                                      cwd=q["root"], env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      start_new_session=True)
            t0 = _C.time.time()
            # 一行一个在跑的任务（并行清单有几行，#147）；放行的主机：别的主机据此不动它（run_elsewhere）
            cf = os.path.join(st, "current")
            _C.write(cf, "".join(l + "\n" for l in (_C.read(cf) or "").splitlines() if l.split()) + "%s %d %s %s\n" % (job["name"], pr.pid, _C.now_str(t0), self.host))
            self.par_note(q["key"], t0, 1)            # 往上加行的：加行门槛的间隔从这次开跑算；补位的不算（par_gate，#176）
        gtxt = "，".join("GPU %d %s" % (c["i"], c["uuid"]) for c in gpus)
        self.qlog(q, "开始 %s" % job["name"] + ("（%s）" % gtxt if gtxt else ""))
        k = _Q.run_key(q, job["name"])
        self.running[k] = dict(job, kind="queue", key=k, pid=pr.pid, t0=t0, q=q, gpus=[c["uuid"] for c in gpus], use={},
                                      project=q["project"], stop=q["stop"], oom0=_M.oom_kills(), fc0=_M.num(_M.cg("memory/memory.failcnt")))
        self.event("start", "开始 %s%s%s" % (job["name"], "（%s）" % gtxt if gtxt else "",
                                           "，独占%s" % {"gpu": "卡", "host": "整机"}[job["excl"]] if job.get("excl") else ""),
                   name=job["name"], project=q["project"], queue=q["jobs"])
        return None

    def grant(self, r, gpus):
        """放行一次单元申请：写 .ok（含选中的卡），申请方据此设 CUDA_VISIBLE_DEVICES 后 exec 原命令。"""
        base = os.path.join(_C.SLOTS, r["id"])
        _C.write(base + ".ok", json.dumps({"gpus": [c["uuid"] for c in gpus], "t": _C.time.time(), "cpus": self.cpus_for(r),
                                        **({"adj": _C.GUEST_ADJ} if self.guest() else {})}))
        if os.path.exists(base + ".why"):
            os.remove(base + ".why")
        self.running[r["key"]] = dict(r, t0=_C.time.time(), gpus=[c["uuid"] for c in gpus], use={}, pstart=_C.pstart(r["pid"]))
        self.event("grant", "放行 %s 的单元 %s（%s）" % (r["project"], r["name"], "，".join("GPU %d" % c["i"] for c in gpus) or "不用 GPU"),
                   name=r["name"], project=r["project"])

    def lane(self, k):
        """这条清单（队列 key）此刻在跑的任务（并行清单有几个，#147），按开跑先后。"""
        return sorted((j for j in self.running.values() if _C.lane_key(j) == k), key=lambda j: j["t0"])

    def par_level(self, k, now):
        """这条清单（队列 key）近 PAR_REFILL_S 同时跑到过的行数（#176）：在跑的少于它时再放一行是补位（跑完空出的格），不是往上加；过期了为 0。"""
        n, t = self.par_hi.get(k, (0, 0))
        return n if now - t <= _C.PAR_REFILL_S else 0

    def par_note(self, k, now, add=0):
        """记下这条清单此刻同时跑着几行（#176）：不少于 par_level 的刷新它（达到过、这时还跑着这么多）。add=1 是 launch 刚放的那行（还没进 running）：
        超过 par_level 的是往上加，加行门槛的间隔（par_t）从它算；补位不动 par_t。一轮开头收尾之前（settle）、接管一行（adopt）也调，重启后在跑的行数就是达到过的。"""
        n, lv = len(self.lane(k)) + add, self.par_level(k, now)
        if add and n > lv:
            self.par_t[k] = now
        if n and n >= lv:
            self.par_hi[k] = (n, now)

    def par_gate(self, r, now):
        """并行清单加行的门槛（#147）：这条清单已有行在跑时，再加一行要过它（第一行不过，同以前）。返回 (能放的卡号或 None 不限, 不加的原因或 None)。
        与 fit、offers 同机多开车道同一条规则（local_cards：容器在限流不加；近 10 分钟利用率不低于 UTIL_FULL 的卡、利用率低而上面在跑的被 CPU 卡住的卡不放），
        不用卡的行只看限流；要卡的另外同一条清单两次往上加行至少隔 PAR_STEP_S（利用率窗口来得及反映新加的那行）。
        补位（在跑的少于 par_level，#176）不等间隔、不因卡吃满不补：那个行数刚跑过、门槛没否掉它；只在有没吃满的卡时放在它们上面（同推演的 allowed）。
        容器在限流时补位也不补，记的行数降到此刻在跑的。真放行在 place 的 pre 里调；推演按此刻的结论当常量（forecast）。"""
        k = r["q"]["key"] if r["kind"] == "queue" else None
        n = len(self.lane(k)) if k else 0
        if not n:
            return None, None
        cards, why = self.local_cards()
        hot = why and self.throttle(now)
        if hot:                                   # 限流：补位也不补，记的行数降到此刻在跑的，之后往上加照间隔与利用率来
            self.par_hi[k] = (n, now)
            return None, why.replace("不在这台多开车道", "不再给这条清单加行")
        if r["ngpu"] <= 0:                        # 不用卡的行：只看限流（CPU 记账另由 place 管），不等利用率
            return None, None
        if n < self.par_level(k, now):            # 补位：有这条清单能用的、没吃满的卡就只放它们，没有（卡都吃满）不限（同推演的 allowed 与 gpar）
            allowed = r["q"].get("gpus")
            return (cards if cards and [i for i in cards if allowed is None or i in allowed] else None), None
        left = _C.PAR_STEP_S - (now - self.par_t.get(k, 0))
        if left > 0:
            return None, "这条清单刚加过一行，%d 秒后看卡的利用率再决定加不加（并行 par=%d）" % (math.ceil(left), r["q"].get("par") or 1)
        return cards, why and why.replace("不在这台多开车道", "不再给这条清单加行")

    def alive_job(self, j):
        if j.get("kind") == "slot":
            return _C.cmdline(j["pid"]) is not None and (j.get("pstart") is None or _C.pstart(j["pid"]) == j["pstart"])
        return _C.alive(j["pid"], ("labmon-job:" + j["name"],))

    def finish(self, key):
        """收尾一个结束了的任务：单元清掉申请文件；清单任务按结果记 .done / .failed 或重新排队，记事件与用量行（requeued 按实际有没有重排）。
        清单任务依次：独占被打扰作废的重排；被对方挤掉的重排（至多 SQUEEZE_MAX 次）；跟着同组停下的（gang_stop）照它的 kind：stop 重排、fail 记失败；
        暂停时停下的重排；退出码 0 记完成；其余记失败。成组的成员被作废、跟着失败的同伴停下的都记失败（#180）；跑成功的（含 boundary 停法没停成、自己跑完的）照记完成。
        成组成员的结果记进 gang.json（gang_end），同伴按它决定跟不跟着停。
        记失败而进程自己退出码为 0 的（成组的跟着失败的同伴停下、被作废，收到停止请求后正常退出），用量行的 rc 记 null：
        依赖（dep_state）、估时与看板都只认 rc "0" 为跑成功，别的主机按用量行认它失败。"""
        j = self.running.pop(key)
        name, rc, oom, sq, requeue, failed = j["name"], None, None, None, False, False
        mins = round((_C.time.time() - j["t0"]) / 60, 1)
        if j.get("kind") == "slot":
            requeue = bool(j.get("stopping"))     # 作废停下的单元由申请方重新申请
            for ext in (".req", ".ok", ".why"):
                f = os.path.join(_C.SLOTS, j["id"] + ext)
                if os.path.exists(f):
                    os.remove(f)
            self.event("end", "单元结束 %s（%s，%.0f 分钟）" % (name, j["project"], mins), name=name, project=j["project"])
        else:
            q = j["q"]
            rcf = os.path.join(q["state"], name + ".rc")
            rc = _C.read(rcf)
            oom = _M.oom_of(j, rc) if rc != "0" and not j.get("stopping") else None   # 停下的、作废的不认
            sq = self.squeezed_by(j, oom)
            if sq:
                self.squeezed[name] = self.squeezed.get(name, 0) + 1
            gs = j.get("gang_stop")
            if gs and not gs.get("sent") and rc == "0":   # boundary 停法没停成、自己跑成功了：照记完成
                gs = None
            if j.get("void") and not j.get("gang"):   # 独占被打扰作废的：重新排队（事件在作废时已记）；成组的记失败（下面）
                requeue = True
                self.qlog(q, "作废 %s（%s），重新排队" % (name, j["void"]))
            elif sq and self.squeezed[name] <= _C.SQUEEZE_MAX:   # 被对方挤掉的：不记失败，重新排队
                requeue, j["squeeze"] = True, sq
                self.note_stop(j, sq, "squeeze")
                self.qlog(q, "被挤掉 %s（%s，退出码 %s），重新排队（第 %d 次）" % (name, sq, rc or "未知", self.squeezed[name]))
                self.event("squeeze", "被对方挤掉：%s（%s），重新排队（第 %d 次）" % (name, sq, self.squeezed[name]),
                           name=name, project=j["project"], why=sq)
            elif gs and gs["kind"] != "fail":     # 跟着同组的停下重排的（事件在 gang_stop 时已记）
                requeue = True
                self.note_stop(j, gs["why"], "gang")
                self.qlog(q, "停下 %s（成组：%s，退出码 %s），重新排队" % (name, gs["why"], rc or "未知"))
            elif j.get("stopping") and not gs and not j.get("void"):   # 暂停时停下的：重新排队，恢复后重跑（续跑由项目自己负责）
                requeue = True
                self.qlog(q, "停下 %s（暂停，退出码 %s），恢复后重跑" % (name, rc or "未知"))
                self.event("stop", "已停下 %s，恢复后重跑" % name, name=name, project=j["project"])
            elif rc == "0" and not gs and not j.get("void"):
                open(os.path.join(q["state"], name + ".done"), "w").close()
                self.publish(j)
                shutil.rmtree(_Q.scratch_dir(j["project"], name), ignore_errors=True)   # 失败的留着查
                self.ldisk.hint(os.path.dirname(_Q.scratch_dir(j["project"], name)))      # 本地盘上这个项目的单格临时少了一份
                self.qlog(q, "完成 %s（%d 分钟）" % (name, int(mins)))
                self.event("done", "完成 %s（%d 分钟）" % (name, int(mins)), name=name, project=j["project"])
            else:
                failed = True
                open(os.path.join(q["state"], name + ".failed"), "w").close()
                log = "见 %s/%s.log" % (os.path.relpath(q["logs"], q["root"]), name)
                if gs:                            # 同组的失败了，按停法停下
                    why = "%s，已按停法停下（退出码 %s），%s" % (gs["why"], rc or "未知", log)
                elif j.get("void"):               # 成组的被作废：同伴跟着停下，两边都记失败
                    why = "作废：%s，成组的任务记失败，%s" % (j["void"], log)
                else:
                    why = "退出码 %s，%s" % (rc or "未知：进程被终止", log) \
                        + ("；%s，已重排 %d 次，不再重排" % (sq, _C.SQUEEZE_MAX) if sq else "；%s，同类下次估计抬高" % _C.OOM_SAY[oom] if oom else "")
                self.qlog(q, "失败 %s（%s）" % (name, why))
                self.event("failed", "失败 %s（%s）" % (name, why), name=name, project=j["project"])
            if j.get("gang"):                     # 同伴按它跟着停或接着跑
                self.gang_end(j, "stop" if requeue else "fail" if failed else "done")
            if os.path.exists(rcf):
                os.remove(rcf)
            _Q.drop_current(q["state"], name, j["pid"])
        # 记失败而进程退出码为 0 的（成组的跟着同伴停下、被作废）记 null：各处只认 "0" 为跑成功（#180）；被对方挤掉的不算 OOM
        with _C.flock(self.p.usage):
            _C.append_jsonl(self.p.usage, self.usage_rec(j, None if failed and rc == "0" else rc, None if sq else oom, requeue))   # 下一轮 reload_hist 读回来（各主机的记录都在共享盘上）

    def usage_rec(self, j, rc, oom, requeue):
        """一个结束了的任务的用量行（usage.jsonl 的一行，finish 与没开跑就跟着记失败的成组成员 gang_fail_unstarted 共用）：
        rc 退出码（字符串；记失败而退出码为 0 的由调用方传 None）、oom 失败是不是 OOM、requeue 有没有重新排队。"""
        u = j.get("use") or {}
        return {"t0": round(j["t0"]), "t1": round(_C.time.time()), "min": round((_C.time.time() - j["t0"]) / 60, 1), "kind": j.get("kind", "queue"),   # 起止时刻（epoch 秒）
               "root": j["q"]["root"] if j.get("q") else None, "queue": j["q"]["jobs"] if j.get("q") else None,
               "project": j.get("project"), "name": j["name"], "tag": j.get("tag"), "rc": rc, "gpus": j["gpus"],
               "vram": j.get("vram"), "est": j.get("est"), "excl": j.get("excl"),
               # 失败是不是 OOM（oom_of："vram" / "ram"，不是为 None；被对方挤掉的不算）：estimate 不把它的峰值当历史，按 est 与峰值抬高同类的估计（#62）
               "oom": oom,
               "ram_peak": u.get("ram_peak"), "vram_peak": u.get("vram_peak"), "gpu_util": u.get("util_mean"),
               "cores_mean": round(u["cpu_s"] / u["wall_s"], 2) if u.get("wall_s") else None,
               "conc": u.get("conc"),                  # 同卡平均几条 labmon 任务（含自己；不用卡的为 None）：duration 按它挑同类记录（#67）
               # 核数与读写的 1 分钟峰值（MiB/s）、累计读写字节（rchar / wchar，含共享盘与命中页缓存的），清单的 cpus=、io= 提示（cores_peak 是同类 CPU 估计的历史）
               "cores_peak": u.get("cores_peak"), "rd_peak": u.get("rd_peak"), "wr_peak": u.get("wr_peak"), "rd_b": u.get("rd_b"), "wr_b": u.get("wr_b"),
               "cpus": j.get("cpus"), "io": j.get("io"),
               # 那次开跑导出的 LABMON_CPUS（cpus_for：按这台配额封顶后的；没开跑的为 None）：cpu_shrink 按它认峰值是不是在这么多线程下跑出来的（#99）
               "give": self.cpus_for(j) if j.get("est") else None,
               # 运行期间同卡、同机有没有别的任务或进程：计时结果据此核对是否真的独占
               # void 只记重排了的作废（成组的作废记失败，不带 void：依赖与看板据此认失败）
               "with_gpu": u.get("with_gpu", []), "with_host": u.get("with_host", []), "void": j.get("void") if requeue else None,
               # 估时与 GPU 型号给 duration 算 实际/估时；停下重排的（暂停、作废、被挤掉）只跑了一段，不进时长
               "est_min": j.get("est_min"), "gpu": self.model() if j["gpus"] else None,
               "cards": round(self.job_cards(j), 3),   # 公平份额按它记用量
               "wait": j.get("wait"),                 # 放行前等了多久（分钟，launch 记的；接管来的没有）
               "gang": j.get("gang"),                 # 成组的组名（#180；同组的另几条按 项目 + 组名 认）
               "requeued": requeue}

    def load_prev(self):
        """读上一个 worker 的状态：接管任务时找回所用的卡、估计值与用量峰值（搬走的清单上手里的任务只靠它找，adopt_left），
        以及近一天的作废记录。构造时读一次；run --replace 等旧 worker 跑完最后一轮退出后再读一次，它最后一轮放的也接得上（Codex 审查 #120）。"""
        try:
            with open(self.p.status) as f:
                old = json.load(f)
            self.prev = {(j.get("root"), j.get("queue"), j["name"], j["pid"]): j for j in old.get("running", [])}
            self.voided = [v for v in old.get("voided") or [] if v.get("t", 0) > _C.time.time() - _C.VOID_KEEP_S]
            kf = old.get("kafight") or {}         # 保活被拉起的告警撑满 FIGHT_S，不因重启提前消失（#124）；两次读以后一次（旧 worker 最终的状态）为准
            hits = [t for t in kf.get("hits") or [] if t > _C.time.time() - _K.FIGHT_S]
            self.ka_hits, self.fight = (hits, {"t": kf.get("t"), "who": kf.get("who")}) if hits else ([], None)
            fresh = _C.time.time() - (old.get("t") or 0) <= _C.STATUS_STALE_S
            if fresh:                             # 停得不久（升级、重启接管）：找不到的起算时刻沿用；停久了别处的清单可能早变了，从头计时
                self.dep_miss = {(p, n): t for p, n, t in old.get("dep_miss") or []}
            # 要卡时停着的保活：重启那段里被拉起，接管后第一轮照样记“保活被拉起”（#124）；停久了那段里怎样说不准，不沿用
            self.ka_off, self.ka_stop_t = (True, old["ka_stop"]) if fresh and old.get("ka_stop") else (False, 0.0)
            ks = (old.get("keepalive") or {}).get("stuck") if fresh else None   # 停不了的保活：接着算，不重复记事件（#102）
            self.ka_fail = dict(ks, said=True) if ks else None
            km = (old.get("keepalive") or {}).get("mine")   # 自己开的保活：停多久都沿用，按出生时钟认（ka_groups 对不上就清掉，换了容器不会认错）
            self.ka_mine = km if isinstance(km, dict) and {"g", "born"} <= set(km) else None
            self.thr_since = (old.get("throttle") or {}).get("since") if fresh else None   # 限流的起算时刻：升级、重启不从头计时
            # 跑空登记的空置起点与休眠态（#164）：重启照常沿用，不重置 2 天倒计时；记着休眠时的清单与登记 file_sig，停机期间变动也会醒来
            ri = old.get("reg_idle") if isinstance(old.get("reg_idle"), dict) else {}
            self.reg_idle = {str(k): {"since": float(v["since"]),
                                      "sig": tuple(v["sig"]) if isinstance(v.get("sig"), list) and len(v["sig"]) == 3 else None,
                                      "rsig": tuple(v["rsig"]) if isinstance(v.get("rsig"), list) and len(v["rsig"]) == 3 else None,
                                      "sleep": bool(v.get("sleep"))}
                             for k, v in ri.items() if isinstance(v, dict) and isinstance(v.get("since"), (int, float))}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.prev, self.voided = {}, []
        # 成组成员近一天的结果（gang.json 里结束了的）：重启后照样告诉同伴（#180）
        g = _DP.gang_file(self.host) or {}
        self.gang_ends = [e for e in g.get("members") or [] if isinstance(e, dict) and e.get("state") in ("done", "fail", "stop")
                          and _C.time.time() - (e.get("t1") or 0) < _C.GANG_KEEP_S]
        self.gang_pub = g.get("members") or None
        self.gang_known = {((p, gn), (p, n)): (h, t) for p, gn, n, h, t in (k for k in g.get("known") or [] if isinstance(k, list) and len(k) == 5)
                           if _C.time.time() - (t or 0) <= _C.GANG_LATE_S}

    def adopt(self, q, cur):
        """接管上一个 worker 留下的一个任务（current 的一行 cur，read_current；并行清单逐行调，#147；别的主机放行的不接管，requests 先过 run_elsewhere）：
        还在跑的继续盯；跑完了的按 .rc 记账；进程没了又没有 .rc 的（服务器重启）重跑。"""
        name, pid, t0, who = cur
        local = not who or who == self.host       # 别的主机放的（那台已归还，run_elsewhere 才放过来）：PID 是另一个容器的，这里同号的进程不是它
        old = self.prev.get((q["root"], q["jobs"], name, pid), {})
        cmd = _Q.job_cmd(q, name) or ""
        gpus = old.get("gpus")
        if gpus is None:                          # 上一份状态里没有它（旧 worker 没写过状态）：从进程的 CUDA_VISIBLE_DEVICES 找回，只认此刻看得到的卡
            known = {g["uuid"] for g in _M.gpu_table()}
            gpus = [u for u in (_C.proc_env(pid, "CUDA_VISIBLE_DEVICES") or "").split(",") if u in known] if local else []
        job = dict(_Q.hints(cmd), name=name, cmd=cmd, kind="queue", pid=pid, t0=t0, q=q, project=q["project"], stop=q["stop"],
                   gpus=gpus, use=old.get("use") or {}, oom0=old.get("oom0"), fc0=old.get("fc0"), owner_gpu=old.get("owner_gpu"), io_sus=old.get("io_sus"),
                   cpu_fit=old.get("cpu_fit"), gang_with=old.get("gang_with"), gang_stop=old.get("gang_stop"))
        job["excl"] = old.get("excl", job["excl"])
        if job["gang_stop"] and job["gang_stop"].get("sent"):   # 停止请求已发：收尾照原重排规则记
            job["stopping"] = True
        job["est"] = old.get("est") or _S.estimate(self.hist, dict(job, root=q["root"], queue=q["jobs"]))
        k = job["key"] = self.free_key(q, name)
        if local and _C.alive(pid, ("labmon-job:" + name,)):
            self.running[k] = job
            self.active.add(q["key"])
            self.par_note(q["key"], _C.time.time())  # 重启接管：上一个 worker 同时跑着的行数算达到过的，跑完补回不等间隔（#176）
            _C.write(os.path.join(q["state"], "lock"), str(os.getpid()))   # lock 改指本进程，看板才认得队列还在
            self.wlog("接管运行中的 %s（PID %d）" % (name, pid))
        elif os.path.exists(os.path.join(q["state"], name + ".rc")):
            self.running[k] = job
            self.finish(k)
        elif local and _C.alive(pid, ("gpu_queue",)) or _C.alive(_C.read_pid(os.path.join(q["state"], "lock")), ("gpu_queue",)):
            pass                                  # 旧 gpu_queue.sh 还在管这条队列
        else:
            _Q.drop_current(q["state"], name, pid)
            if job.get("gang"):                   # 同伴按它停下重排
                self.gang_end(job, "stop")
            self.qlog(q, "中断 %s（进程已不在，未留退出码：多半是服务器重启），重新排队" % name)
            self.event("interrupt", "中断 %s（进程已不在，多半是服务器重启），重新排队" % name, name=name, project=q["project"])

    def free_key(self, q, name):
        """接管、改记名下时给在跑的 name 用的 key：照 run_key；那个 key 已被这条清单另一个在跑的占着（运行中把 par 调小、删掉了声明，
        几行还在跑，#147）时用逐行的 `队列 key|任务名`，几行各自跟到收尾，不互相盖掉。"""
        k = _Q.run_key(q, name)
        return k if k not in self.running or self.running[k]["name"] == name else q["key"] + "|" + name

    def rekey(self, q):
        """这条清单换了登记文件名、手上在跑的还记在旧名下时（11-06 前不带目录哈希的登记搬走再搬回来，搬回来的用 qslug，#95）：
        沿用那一项、改记到新名下，不再按新名接管一次（同一个任务盯两份，收尾时一份成功一份失败）。状态目录一条清单一个，按它认。"""
        st = os.path.normpath(q["state"])
        for k, j in list(self.running.items()):
            if j.get("q") and j["q"]["key"] != q["key"] and os.path.normpath(j["q"]["state"]) == st:
                old = j["q"]["key"]
                nk = self.free_key(q, j["name"])
                if nk not in self.running:
                    self.running[nk] = dict(self.running.pop(k), key=nk, q=q)
                if old in self.active:
                    self.active.discard(old)
                    self.active.add(q["key"])

    def adopt_left(self, queues):
        """登记已搬走（worker add 到别的主机，#95）、上一个 worker 手上还在跑的任务：照样接管、盯到跑完收尾（.done、用量记录），
        登记着它的那台这期间按 current 里放行的主机等它（run_elsewhere）；进程没了的照旧记中断，由登记着它的那台重跑。
        只认 current 还是上一份状态里的那个任务（名称与 PID 对得上、是这台放的）：那台已经接着跑了的不动。第一轮在接管登记着的队列之前调。"""
        mine = {(os.path.normpath(q["root"]), q["jobs"]) for q in queues}
        for j in list(self.prev.values()):
            root, jobs = j.get("root"), j.get("queue")
            if j.get("kind") != "queue" or not root or not jobs or not j.get("project") or (os.path.normpath(root), jobs) in mine:
                continue
            q = _Q.load_queue({"_slug": _Q.qslug(root, jobs), "root": root, "jobs": jobs, "project": j["project"], "stop": j.get("stop")})
            _Q.list_jobs(q)                          # 并行数（q["par"]）：key 照它定
            for cur in _Q.read_current(q["state"]):
                if cur[:2] == (j["name"], j["pid"]) and cur[3] in (None, self.host):
                    self.adopt(q, cur)

    # ---- 单元申请 ----
    def slot_reqs(self):
        """容器本地 SLOTS 目录里的申请：已放行的接管（worker 重启后），申请方没了的清掉，其余是待放行的请求。"""
        out = []
        for f in sorted(glob.glob(os.path.join(_C.SLOTS, "*.req"))):
            rid = os.path.basename(f)[:-4]
            key, base = "slot:" + rid, f[:-4]
            if key in self.running:
                continue
            try:
                with open(f) as fh:
                    r = json.load(fh)
            except (OSError, ValueError):
                continue
            pid = int(r.get("pid") or 0)
            req = _Q.new_req("slot", key, r.get("project") or "?", r.get("name") or rid, id=rid, pid=pid, ngpu=int(r.get("gpus", 1)),
                          vram=float(r.get("vram") or 0), ram=float(r.get("ram") or 0), tag=r.get("tag"), excl=r.get("excl"),
                          stop=r.get("stop") or "term", cmd=r.get("cmd", ""), log=r.get("log"), t=r.get("t") or _C.time.time(), est_min=r.get("est_min"),
                          cpus=r.get("cpus"), io=r.get("io"))
            if _C.cmdline(pid) is None:              # 申请方已经没了（含放行后跑完、崩溃）
                for ext in (".req", ".ok", ".why"):
                    if os.path.exists(base + ext):
                        os.remove(base + ext)
                continue
            ok = _C.read(base + ".ok")
            if ok:                                # 上一个 worker 放行过：接着盯
                old = self.prev.get((None, None, req["name"], pid), {})
                try:
                    gpus = json.loads(ok).get("gpus", [])
                except ValueError:
                    gpus = []
                self.running[key] = dict(req, t0=os.path.getmtime(base + ".ok"), gpus=gpus, use=old.get("use") or {},
                                         est=old.get("est") or _S.estimate(self.hist, req), pstart=_C.pstart(pid), io_sus=old.get("io_sus"),
                                         cpu_fit=old.get("cpu_fit"))
                continue
            out.append(req)
        return out

    # ---- 用量 ----
    def sample(self, queues):
        """主机现状与各任务用量：内存（任务进程 PSS 之和）、核数、读写、显存（按卡），以及运行期间同卡、同机还有谁。
        步骤：读主机（host_info、压力 press）→ 认旧 gpu_queue.sh 的任务（ext_jobs）→ 各任务的进程（job_procs）→ GPU 进程归属（gpu_owners）
        → 各卡利用率窗口（util_window）→ 逐任务量用量（measure）→ labmon 之外的用量、作废、借来主机的几项判定 → 内存闸门事件（mem_events）。"""
        t = _C.time.time()
        procs, gpus, apps = _M.scan_procs(), _M.gpu_table(), _M.gpu_procs()
        h = _M.host_info(gpus, _C.DATA_ROOT)
        h["press"] = self.press(t)
        self.slow = h["press"]["nfs"] if (h["press"]["nfs"] or {}).get("slow") else None
        if h["cpu_ns"] is not None:
            if self.cpu_prev and t > self.cpu_prev[0]:
                h["cpu_used"] = round(max(0.0, (h["cpu_ns"] - self.cpu_prev[1]) / 1e9 / (t - self.cpu_prev[0])), 2)
            self.cpu_prev = (t, h["cpu_ns"])
        self.ext = self.ext_jobs(queues)
        jobs = list(self.running.values()) + list(self.ext.values())
        mine = _M.job_procs(procs, jobs)
        gp = _K.gpu_owners(apps, procs, {pid: j for j in jobs for pid in mine[id(j)]}, self.ka_full if self.guest() else None)
        self.util_window(gpus, gp, t)
        util = {g["uuid"]: g.get("util") for g in gpus}
        ovram, ogid = {}, {}                      # labmon 之外的进程各卡占的显存（MB）、各卡上它们的宿主机 PID
        for g in gp:
            if g["owner"] == "other":
                ovram[g["uuid"]] = ovram.get(g["uuid"], 0.0) + g["mb"]
                ogid.setdefault(g["uuid"], set()).add(g["hpid"])
        cards_on = {}                             # 各任务此刻所用的卡（独占任务据此看同卡、同机有没有别的，作废）
        for j in jobs:
            if mine[id(j)]:
                cards_on[id(j)] = self.measure(j, mine[id(j)], procs, gp, util, ovram, jobs, t)
        self.info = h
        extra = self.other_cpu(jobs, h, t)
        # 保活整棵进程树（DataLoader 等子进程也持着内存、占着核，只算 GPU 进程本身会把它们算到对方头上，#29）：
        # 内存扣各进程 PSS 之和（组内探针 fleet.analyze 同一口径），CPU 扣这一段里的核数。根是这一轮认作保活的 GPU 进程：ka.sh 管的（owner keepalive），
        # 和借来的主机上对方另起、ka.sh 停不了的（ka 为真，#37：保活只占位、不算对方干活，算预留的探针也扣它）；
        # 加上上一轮认过、这一轮没出现在 nvidia-smi 里但还在（PID 与启动时刻都对得上）的：刚拉起还没建上下文、nvidia-smi 超时都会漏认一轮，
        # 是不是停不了的沿用上一轮。这一轮 nvidia-smi 里有的按这一轮的归属，不沿用（漏读一轮 guest 指令时停不了的被认作 ka.sh 管的，读到后归回停不了的）
        seen = {g["pid"] for g in gp if g["pid"]}
        roots = {g["pid"]: g["ka"] for g in gp if (g["owner"] == "keepalive" or g["ka"]) and g["pid"]}
        roots.update({p: v[1] for p, v in self.ka_roots.items() if p not in seen and p in procs and v[0] is not None and _C.pstart(p) == v[0]})
        self.ka_roots = {p: (_C.pstart(p), x) for p, x in roots.items() if p in procs}
        ka_pids = _M.proc_tree(procs, roots)
        ka_ram = sum(_M.mem_of(p) for p in ka_pids) / _C.GIB
        ka_cpu = self.ka_cores(procs, ka_pids, t, "all")
        # 停不了的那几棵另算核数（cpu_room 另扣）：各棵保活树（按根的 PID 与启动时刻）分开记累计，归类只决定加进哪一份。
        # 改类那一轮照常按这一段求差（合成一份记时，刚归进来的树会把启动以来的 CPU 一起算进来）；一棵退出不丢别的几棵这一段的核数；
        # 只看 /proc 的累计，不掺 labmon 之外的总量（那是容器用量减任务，有测量误差、会为负），各棵都不小于 0。新冒出来的树第一轮为 0（ka_cores）
        # 按启动时刻遍历（祖先一定在子孙前面）：嵌套的保活根（父、子都有 GPU 上下文）里父进程漏认一轮、补回来排在后面时，子树也不挪到子的键上
        own, ka_x = set(), 0.0
        for p, x in sorted(roots.items(), key=lambda r: ((self.ka_roots.get(r[0]) or (0,))[0] or 0, r[0])):
            tree = [q for q in _M.proc_tree(procs, [p]) if q not in own]
            own.update(tree)
            c = self.ka_cores(procs, tree, t, (p, self.ka_roots[p][0])) if p in self.ka_roots else 0.0
            ka_x += c if x else 0.0
        live = {(p, v[0]) for p, v in self.ka_roots.items()}
        self.ka_ticks = {k: v for k, v in self.ka_ticks.items() if k == "all" or k in live}
        self.cpu_n = self.cpu_n + 1 if extra is not None and extra - ka_cpu > _C.VOID_CPU else 0   # 保活不算 labmon 之外的零碎占用
        m = h.get("mem") or {}
        ka_ext = {}
        for g in gp:
            if g["ka"]:
                ka_ext.setdefault(g["uuid"], []).append(g["hpid"])
        self.other = {"vram": ovram, "cpu": None if extra is None else max(0.0, extra - ka_cpu), "ka": ka_ext, "gid": ogid, "ka_cpu": ka_x,
                      "ram": max(0.0, m["anon"] - sum((j.get("use") or {}).get("ram") or 0.0 for j in jobs) - ka_ram) if m else None}
        self.void_check(jobs, cards_on, gp, extra)
        self.guest_scan(t)
        self.oom_check(t)
        self.guest_tight(t)
        self.gp, self.procs, self.mine = gp, procs, {pid for v in mine.values() for pid in v}
        # 保活另记启动时刻（epoch 秒，状态的 gpu_procs[].t0：worker status 写“几点起”，#61）
        self.gprocs = [dict({k: v for k, v in g.items() if k != "_j"}, **({"t0": round(_C.started_at(g["pid"]) or 0) or None}
                                                                         if g["pid"] and (g["owner"] == "keepalive" or g["ka"]) else {})) for g in gp]
        self.mem_events(m)

    def void_check(self, jobs, cards_on, gp, extra):
        """独占任务运行中被打扰（intruders：同卡、整机独占时同机有别的，或 labmon 之外占了 VOID_CPU 核以上）连着两轮就作废。
        cards_on：各任务所用的卡；extra：other_cpu。借来的主机上正推迟暂停看门狗（ka_wait，还没满 LOCK_WAIT_S，#38）时 GPU 保活不算：
        推迟一般只多一两轮，sample 里先于 keepalive 跑，第一轮只计 1 次、第二轮起豁免；满 LOCK_WAIT_S 还停不下来的照旧算。"""
        if self.ka_wait and _C.time.time() - self.ka_wait["t0"] < _K.LOCK_WAIT_S:
            gp = [g for g in gp if g["owner"] != "keepalive"]
        for j in list(self.running.values()):
            if not j.get("excl") or j.get("stopping") or _C.is_hold(j["name"]) or id(j) not in cards_on:
                continue
            hit = sum(_C.intruders(j["excl"], cards_on[id(j)], jobs, gp, extra, j), [])
            j["void_n"] = j.get("void_n", 0) + 1 if hit else 0
            if j["void_n"] >= 2:
                self.void_job(j, "独占%s期间有 %s" % ({"gpu": "卡", "host": "整机"}[j["excl"]], "、".join(hit[:3])))

    # ---- 主循环 ----
    def registry(self):
        """这一轮交给 tick 的登记队列。坏了的登记文件（不是 JSON、缺 root / jobs）跳过并记一次事件，不让一个坏文件拖垮整个 worker。
        同一条清单只归一台（#95）：别的在用主机（peers 的 peer_st：worker 在、没 stop 过）也登记着它、登记文件比本机的新（共享盘上的修改时间，
        各台同一个时钟；一样新按主机 id 大的），这台不碰它（不接管、不放行，状态里不列，看板的车道与管理抽屉随之只在那台），变了才记一次事件。
        worker add 会把别处的登记搬走，两台都登记着的只有旧版留下的或两处同时登记的；那台不在了（归还、worker stop、状态过期）这台照常跑。
        这台手上已在跑的照样盯到跑完（running 不靠登记）。第一轮也先读一次别的主机（peers）再定。
        跑空满 IDLE_REG_S（2 天，#164）转休眠的登记：每轮只对清单文件做一次 file_sig（复用登记文件的 stat 判重登记），
        不读 .env、状态目录与标记；清单文件或登记文件一变立即醒来照常排。"""
        out, bad = [], []
        prev_q = {(os.path.normpath(j.get("root") or ""), j.get("queue")) for j in getattr(self, "prev", {}).values() if j.get("kind") == "queue"}
        for f, reg, rsig, mtime, changed in _Q.regs(self.p.queues, cache=self.reg_cache, with_meta=True):
            if isinstance(reg, Exception):
                bad.append((f, reg))
                continue
            try:
                key, root, jobs = reg["_slug"], reg["root"], reg["jobs"]
                _ = reg["project"]
            except (KeyError, TypeError, AttributeError) as e:
                bad.append((f, e))
                continue
            idle = self.reg_idle.get(key)
            if changed or (idle and idle.get("rsig") and idle["rsig"] != rsig):   # 重新 worker add：立即唤醒
                self.reg_idle.pop(key, None)
                self.sleep_marks.pop(key, None)
                idle = None
            try:
                if idle and idle.get("sleep") and not self.lane(key) and (os.path.normpath(root), jobs) not in prev_q:
                    jsig = _P.file_sig(os.path.join(root, jobs))
                    if jsig == idle.get("sig"):
                        q = dict(_Q.load_queue(reg, read_env=False), file=f, _mtime=mtime, _rsig=rsig, _reg=reg,
                                 empty=True, sleeping=True, empty_since=round(idle["since"]), par=1)
                    else:
                        self.reg_idle.pop(key, None)
                        self.sleep_marks.pop(key, None)
                        self.wlog("清单 %s/%s 文件有变动，从休眠唤醒" % (root, jobs))
                        q = dict(_Q.load_queue(reg), file=f, _mtime=mtime, _rsig=rsig, _reg=reg)
                else:
                    q = dict(_Q.load_queue(reg), file=f, _mtime=mtime, _rsig=rsig, _reg=reg)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                bad.append((f, e))
                continue
            out.append(q)
        for f, e in bad:
            if f not in self.bad_reg:
                self.bad_reg.add(f)
                self.event("worker", "登记文件坏了，跳过这条队列：%s（%r）" % (f, e))
        self.peers(_C.time.time())
        keep, yielded = [], {}
        for q in out:
            mine = (q["_mtime"], self.host)
            win = max(self.peer_regs.get((os.path.normpath(q["root"]), q["jobs"]), []) + [mine])
            if win == mine:
                keep.append(q)
                continue
            yielded[q["key"]] = win[1]
            if self.yielded.get(q["key"]) != win[1]:
                self.event("worker", "清单 %s/%s 也登记在主机 %s、那边登记得更晚：这台不跑它（要在这台跑就再 labmon worker add 一次）"
                           % (q["root"], q["jobs"], win[1]), project=q["project"], root=q["root"], queue=q["jobs"])
        for k in set(self.yielded) - set(yielded):
            q = next((q for q in keep if q["key"] == k), None)
            if q:
                self.event("worker", "清单 %s/%s 不再被主机 %s 的登记挡着，这台接着跑" % (q["root"], q["jobs"], self.yielded[k]),
                           project=q["project"], root=q["root"], queue=q["jobs"])
        self.yielded = yielded
        live = {q["key"] for q in keep}
        self.reg_idle = {k: v for k, v in self.reg_idle.items() if k in live}
        self.sleep_marks = {k: v for k, v in self.sleep_marks.items() if k in live}
        return keep

    def publish(self, j):
        """跑成功了：needs= 写了来源、共享盘上还没有主副本的本地文件，拷上去当主副本（以后续租、换主机就拷，不用重算）。不删主副本。"""
        if not j.get("needs"):
            return
        try:
            pub = [m for c in _Q.check_needs(j, self.qenv(j["q"])) for m in c["publish"]]
            if pub:
                n, b = _Q.stage([(f, m) for m, f in pub])
                self.event("publish", "%s：%d 个本地依赖写成共享盘主副本（%.0f MB）" % (j["name"], n, b / 1e6), name=j["name"], project=j.get("project"))
        except OSError as x:
            self.wlog("写共享盘主副本失败（%s）：%s" % (j["name"], x))

    def missing(self, r):
        """缺本地文件又补不了（needs= 声明的，没有来源、命令也不会自己重算）：返回原因；没有为 None。"""
        if r["kind"] == "queue" and r.get("needs"):
            miss = [c["pat"] for c in _Q.check_needs(r, self.qenv(r["q"])) if not c["files"] and not c["heal"]]
            if miss:
                return "缺本地文件：%s（续租、容器重启会清空本地盘；补好文件，或在命令里先 [ -f 文件 ] || 重新生成）" % "、".join(miss[:3])
        return None

    def stalled(self, r, S):
        """与此刻 GPU 占用无关、等也等不来的原因：缺本地文件又补不了、labmon 的任务全停也放不下（nofit）、tag 到上限（tag_full）；没有为 None。
        prep 算一遍记在 r["stall"]，tick 据此不为这类请求停保活（否则卡空着、保活也不开）；place 里按放行时的快照再算。"""
        x = self.as_job(r)
        return self.missing(r) or _S.nofit(x, S) or _S.tag_full(x, S)

    def prep(self, r, now, limits):
        """把一个请求（new_req）按主机现状补全，admit、place 与推演才认：资源估计 est、挡住它的临时指令 held、没满足的依赖 dep、预计时长 dur（分钟）、
        等不来的原因 stall（按此刻的快照）、开始等的时刻 since 与放行时先记的卡·时 charge。返回 r。
        等了多久（屏障、launch 记的 wait）只算没被临时指令、依赖挡住的时间：挡住期间不算，解除后从那时重新起算
        （否则 pause 半小时一 resume，队首立刻满 wait_cap 成屏障，别的项目全让位）；单元从申请时刻起算。"""
        old = self.wsince.get(r["key"])
        r["est"] = _S.estimate(self.hist, r)
        r["held"] = self.override_hit(r)
        # 依赖没满足（dep_block）、成组的还不能排队（gang_block）：同被临时指令挡住
        r["dep"] = None if r["held"] else self.dep_block(r, now) or self.gang_block(r, now)
        g = r.get("gang") and self.gsim.get((r["project"], r["gang"]))
        if g and r["dep"] and r["dep"][0] == "cycle":
            g["at"] = None                        # 成环：推演里同组的都不排（停在同伴上，同依赖；凑不齐、说不出何时的 gang_sync 已置空）
        elif g and not r["held"] and not r["dep"] and (r["key"], r["name"]) in g["local"]:
            g["local"][(r["key"], r["name"])] = now   # 这一轮在队首、能排队：组里别的成员留资源按它此刻就能算（gang_until）
        r["dur"], r["cut"] = self.req_dur(r), self.req_dur(r, cut=True)
        r["stall"] = self.stalled(r, self.snapshot(limits))
        fresh = not old or old[0] != r["name"] or old[2]            # 第一次见、换了任务，或上一轮被挡着
        r["since"] = now if r["held"] or r["dep"] else (r["t"] if r["kind"] == "slot" and not old else now) if fresh else old[1]
        r["charge"] = self.cards(r["ngpu"], r["est"]["vram"], r["excl"]) * (r["dur"] or 0.0) / 60
        return r

    def stage_bg(self, r, cp):
        """本地缺、共享盘有主副本的依赖在后台线程里拷（大文件要很久，不拖住这一轮 tick）：没在拷就起线程（共享盘慢时先推迟，io_yield）；
        拷完收结果（记事件，失败返回原因、下一轮重拷）。别的请求正在拷同一个目标文件时不另起（同写一个 .part），等它拷完下一轮再看。
        返回这一轮的等待原因，拷好了为 None。"""
        key, out = (r["key"], r["name"]), {}
        s = self.stagers.get(key)
        if s is None:
            dst = {b for _, b in cp}
            busy = sorted(b for k2, o in self.stagers.items() if k2 != key and o["th"].is_alive() for b in o.get("dst") or () if b in dst)
            if busy:                              # 别的任务（并行清单的另一行、别的清单）正在拷同一个文件：同写一个 .part，等它拷完（#147）
                return "另一个任务正在拷同一个本地依赖（%s），等它拷完" % "、".join(os.path.basename(b) for b in busy[:3])
            slow = self.io_yield(key, _C.time.time())
            if slow:
                return "%s，推迟拷 %d 个本地依赖（最多推迟 %d 分钟）" % (slow, len(cp), _C.IO_YIELD_MAX_S / 60)

            def run():
                try:
                    out["r"] = _Q.stage(cp)
                except OSError as x:
                    out["err"] = str(x)
            th = threading.Thread(target=run, daemon=True)
            s = self.stagers[key] = {"th": th, "t0": _C.time.time(), "n": len(cp), "out": out, "dst": dst}
            th.start()
        if s["th"].is_alive():
            return "正在从共享盘拷 %d 个本地依赖（已 %.0f 秒）" % (s["n"], _C.time.time() - s["t0"])
        self.stagers.pop(key)
        if "err" in s["out"]:
            return "从共享盘拷本地依赖失败：%s" % s["out"]["err"]
        n, b = s["out"]["r"]
        self.event("stage", "%s：从共享盘拷了 %d 个本地依赖（%.0f MB，%.0f 秒）" % (r["name"], n, b / 1e6, _C.time.time() - s["t0"]),
                   name=r["name"], project=r.get("project"))
        return None

    def place(self, r, S, launch=None):
        """真放行：按快照 S 调准入核心 place；能放就放（launch）并返回 None，否则返回 (等待原因, 是否卡在资源上)。
        核心之外只有真放行要做的：缺本地文件（miss）、在看资源前拷本地依赖与内存闸门（pre），以及并行清单加行的门槛（par_gate，#147：只缩小能用的卡、不加就写原因）。原因代码（快照的 code）记进 r["code"]，
        note_waits 据此算一直因 CPU 被挡了多久。"""
        x = dict(self.as_job(r), miss=self.missing(r))
        r["miss"] = x["miss"] is not None            # 缺本地文件又补不了：等待项带着它（状态里的 miss），看板按字段认，不按文字
        ok, gate = self.par_gate(r, _C.time.time())      # 并行清单加行（已有行在跑）：只放在没吃满的卡上，限流、刚加过时不加（#147）
        if ok is not None:
            x["allowed"] = [i for i in ok if x["allowed"] is None or i in x["allowed"]]

        def pre():
            if gate:
                return gate
            if r["kind"] == "queue" and r.get("needs"):
                cp = [m for c in _Q.check_needs(r, self.qenv(r["q"])) for m in c["copy"]]
                why = (cp or (r["key"], r["name"]) in self.stagers) and self.stage_bg(r, cp)   # 拷完的那一轮 cp 已空，也要收结果
                if why:
                    return why
            if self.gate:
                m = self.info["mem"]
                return "内存闸门：收不回的内存 %.0f / %.0f GiB 超过 %d%%，暂停放行" % (m["anon"], m["limit"], _C.GATE * 100)
            return None
        cards, why, res = _S.place(x, S, _C.time.time(), pre)
        r["code"] = S["code"]
        if cards is None:
            return why, res
        return (launch or self.launch)(r, cards)

    def tick(self, queues, first):
        """一轮：收尾跑完的、读指令与设置 → 收集各队列队首与单元申请 → 采样、补全请求 → 保活与 GPU 现状 → 按 admit 放行 → 推演 → 记等待 → 扫本地与共享盘 → 写状态。"""
        limits, pol = self.settle(queues)
        waiting, reqs = self.requests(queues, first)
        try:
            self.sample(queues)                   # 先读主机现状：准入要用当前的内存与各任务实测
            self.watch(_C.time.time())               # 借来的主机：找看门狗（在停保活之前，看得到是谁拉起的）
        except Exception as e:  # noqa: BLE001 — 统计出错不能拖垮调度
            if self.noted.get("_sample") != str(e):
                self.wlog("用量统计出错：%r" % e)
                self.noted["_sample"] = str(e)
        self.queue_durs(queues)                   # 截止前的准入（req_dur）也要用各清单的估时
        now = _C.time.time()
        self.dep_state(queues, now, reqs)         # 本机各清单的状态（写进状态的 todo）与依赖等的任务各自的状态（prep 的 dep_block 与推演用）
        self.gang_sync(now)                       # 成组：认组、读同伴的 gang.json，在跑的成员该停的跟着停（gang_watch）
        reqs = self.gang_doomed(reqs, now)        # 同一次开跑的同伴在这边跟上之前就失败了：没开跑的也记失败
        for r in reqs:
            self.prep(r, now, limits)
        gpus, apps = self.gpu_state(reqs, now)
        focus = {o.get("target") for o in self.ovr if o["kind"] == "focus"}
        use = self.share(now, pol)
        self.io_suspect(now)                      # 只记录：已跑过同类历史 1.5 倍、共享盘又慢的任务记一条事件（用 share 里 peers 读的同机任务）
        waiting += [dict(r, why=(r["held"] or r["dep"])[1]) for r in reqs if r["held"] or r["dep"]]     # 被临时指令、依赖挡住的不参与排队（没有排位）
        for r, why, pos in _S.admit([r for r in reqs if not r["held"] and not r["dep"]], focus, pol, use, now, lambda r: self.place(r, self.snapshot(limits, gpus, apps)),
                                 lambda r: self.shadow(r, now, pol, focus, limits)):
            waiting.append(dict(r, why=why, pos=pos))
        self.gang_recheck(limits, gpus, apps, now)   # 成组：放掉这一轮没重新留的；这一轮回填的可能用了留着的资源，留着的成员按放行后的快照再看一遍
        self.gang_commit(reqs, waiting, now)      # 成组：本机成员都留好、别的主机的都此刻就能，一起放行
        self.forecast(queues, reqs, limits, now, pol)
        self.gang_publish(waiting, now)           # 推演之后：本机成员此刻怎样（eta 取这一轮推演的）写进 gang.json
        self.note_waits(waiting, reqs, now)
        self.scan_local(queues, now)
        self.write_status(queues, waiting, limits, pol, use, now)

    def settle(self, queues):
        """一轮开头：记下并行清单此刻跑着几行（par_note，收尾之前）、收尾不在了的任务（finish）、重读用量记录、停法以现在的登记为准、读临时指令，返回 (limits, 调度设置)。"""
        now = _C.time.time()
        for k in {_C.lane_key(j) for j in self.running.values() if j.get("kind") == "queue"}:
            self.par_note(k, now)
        for k, j in list(self.running.items()):
            if not self.alive_job(j):
                self.finish(k)
        self.reload_hist()                        # 本机刚跑完的与别的主机新记的
        stops = {q["key"]: q["stop"] for q in queues}
        for j in self.running.values():          # 停法以现在的登记为准：重新登记 --stop 对正在跑的也生效（以前沿用放行时的，pause --now 停不下）
            j["stop"] = stops.get(_C.lane_key(j), j.get("stop"))
        self.ovr = self.load_overrides()
        try:
            limits = json.loads(_C.read(self.p.limits, "{}") or "{}")
        except ValueError:
            limits = {}
        return limits, self.policy()

    def _idle_start(self, q, now, was_active=False):
        """首次见到空队列时推算空置起点（#164）：本轮刚跑完的从现在起算；重启或升级后首次见到的取登记时刻（added_t）与用量历史最晚结束（t1）
        的较晚者（均用容器时钟，不拿文件 mtime 比），都没有则从现在起算。"""
        if was_active:
            return now
        t_add = q.get("added_t") or 0
        rn = os.path.normpath(q["root"])
        t_hist = max((h.get("t1") or 0 for h in self.hist if h.get("queue") == q["jobs"] and os.path.normpath(h.get("root") or "") == rn), default=0)
        since = max(t_add, t_hist)
        return min(now, since) if since else now

    @staticmethod
    def _mark_snapshot(state, rows):
        """把状态目录里这条清单的 .done/.failed 标记拍成快照（转休眠时记一份，之后不读状态目录）。"""
        try:
            ms = set(os.listdir(state))
        except OSError:
            return {}
        out = {}
        for n, _ in rows:
            v = "done" if n + ".done" in ms else "failed" if n + ".failed" in ms else None
            if v:
                out[n] = v
        return out

    def requests(self, queues, first):
        """各队列这一轮的请求：休眠中的跳过；目录不在、旧 gpu_queue.sh 还在跑、已暂停的直接进等待表；清单跑空的标 empty（项目暂时没实验）、收掉 lock，
        空满 IDLE_REG_S（2 天，#164）转休眠；其余取队首拼请求（new_req），加上单元申请（slot_reqs）。状态目录里有不是本机在盯的 current 时：
        别的主机放行的（run_elsewhere）不动、进等待表，其余接管（adopt：第一轮是上一个 worker 留下的；之后是那台被看板标成已归还以后留下的）。返回 (等待表, 请求)。"""
        waiting, reqs = [], []
        now = _C.time.time()
        self.peers(now)                              # 第一轮也要知道看板说哪些主机已归还（gone）
        elsewhere = {}
        if first:
            self.adopt_left(queues)
        for q in queues:
            if q.get("sleeping"):                    # 已休眠：每轮只由 registry 用 file_sig 查清单文件，不再读状态目录与标记
                continue
            if not os.path.isdir(q["root"]):
                self.reg_idle.pop(q["key"], None)
                self.sleep_marks.pop(q["key"], None)
                waiting.append({"key": q["key"], "q": q, "name": None, "why": "目录不存在：" + q["root"]})
                continue
            os.makedirs(q["state"], exist_ok=True)
            os.makedirs(q["logs"], exist_ok=True)
            jsig0 = _P.file_sig(q["jobs_path"])      # 读清单前的签名：进休眠前复核一致，避免读后追加的新行被旧判定吞掉（#164）
            rows = _Q.list_jobs(q)                   # 清单（每轮重读）与并行数 q["par"]：换名、接管来的任务 key 照它定（run_key）
            self.rekey(q)
            mine = {(j["name"], j.get("pid")) for j in self.lane(q["key"])}
            curs = [c for c in _Q.read_current(q["state"]) if c[:2] not in mine]   # 不是本机在盯的行
            cur, other = next(((c, h) for c in curs for h in [_Q.run_elsewhere(self.host, q, c, self.gone)] if h), (None, None))
            if other:
                self.reg_idle.pop(q["key"], None)
                self.sleep_marks.pop(q["key"], None)
                elsewhere[q["key"]] = (other, cur[0])
                if self.elsewhere.get(q["key"]) != (other, cur[0]):
                    self.event("worker", "清单 %s/%s 的 %s 是主机 %s 放行的，这台不接管、不重跑，等那台收尾" % (q["root"], q["jobs"], cur[0], other),
                               project=q["project"], root=q["root"], queue=q["jobs"], name=cur[0])
                waiting.append({"key": q["key"], "q": q, "name": None, "elsewhere": [other, cur[0]],
                                "why": "主机 %s 放行的 %s 还没收尾（这条清单也登记在那台），等那台跑完；那台已不在、任务确定没了，删掉状态目录里的 current 这台就接着跑" % (other, cur[0])})
                continue
            if curs:
                for c in curs:
                    self.adopt(q, c)
            run = self.lane(q["key"])
            if not run:
                lock = _C.read_pid(os.path.join(q["state"], "lock"))
                if _C.alive(lock, ("gpu_queue",)):
                    self.reg_idle.pop(q["key"], None)
                    self.sleep_marks.pop(q["key"], None)
                    waiting.append({"key": q["key"], "q": q, "name": None, "why": "旧 gpu_queue.sh（PID %d）还在跑这条队列" % lock})
                    continue
            if os.path.exists(os.path.join(q["state"], "paused")):
                self.reg_idle.pop(q["key"], None)
                self.sleep_marks.pop(q["key"], None)
                if not run:
                    waiting.append({"key": q["key"], "q": q, "name": None, "why": "已暂停（状态目录有 paused）"})
                continue
            jobs = _Q.next_jobs(q, {j["name"] for j in run}, rows)   # 窗口：串行清单在跑时为空，同以前
            if run:
                self.reg_idle.pop(q["key"], None)
                self.sleep_marks.pop(q["key"], None)
                reqs += [_Q.queue_req(q, job) for job in jobs]
                continue
            if not jobs:
                q["empty"] = True                 # 空着：清单里没有待办（项目暂时没实验），状态里标出来，别的项目不当它在抢卡
                was_active = q["key"] in self.active
                if was_active:
                    self.qlog(q, "队列结束（清单中已无待办任务）")
                    self.event("qend", "队列结束：%s 清单中已无待办任务" % q["jobs"], project=q["project"], root=q["root"], queue=q["jobs"])
                    self.active.discard(q["key"])
                    if _C.read(os.path.join(q["state"], "lock")) == str(os.getpid()):
                        os.remove(os.path.join(q["state"], "lock"))
                ent = self.reg_idle.get(q["key"])
                if ent is None:
                    ent = self.reg_idle[q["key"]] = {"since": self._idle_start(q, now, was_active), "sig": None, "rsig": q.get("_rsig"), "sleep": False}
                else:
                    ent["rsig"] = q.get("_rsig")
                q["empty_since"] = round(ent["since"])
                if now - ent["since"] >= _P.IDLE_REG_S and jsig0 is not None and _P.file_sig(q["jobs_path"]) == jsig0:
                    ent["sig"] = jsig0
                    ent["sleep"] = True
                    q["sleeping"] = True
                    self.sleep_marks[q["key"]] = self._mark_snapshot(q["state"], rows)
                    self.wlog("清单 %s/%s 空满 %d 天，转为休眠（仅监听清单文件变动）" % (q["root"], q["jobs"], _P.IDLE_REG_S // 86400))
                continue
            self.reg_idle.pop(q["key"], None)
            self.sleep_marks.pop(q["key"], None)
            reqs += [_Q.queue_req(q, job) for job in jobs]
        self.elsewhere = elsewhere
        return waiting, reqs + self.slot_reqs()

    def gpu_state(self, reqs, now=None):
        """放行前的 GPU 现状：先按需要停保活，再查各卡空闲（有要卡的请求时）与此刻的 GPU 进程（有独占请求时）；清掉不再等的独占留卡与成组留的资源。
        被临时指令挡住的（暂停的项目、drain、续租截止、只给别人用）、依赖没满足的与等不来的（stall：放不下、缺文件、tag 到上限）不会放行，
        不为它们停保活，否则卡空着、保活也不开；成组的成员组里 GANG_SOON_S 内不能一起开跑的（gang_soon）同样不停；要看显存的请求照旧先停保活再看。
        独占任务的留卡与成组留的资源只给这一轮真去排队的：不再等了、被临时指令或依赖挡住的清掉（否则它的项目暂停期间那几张卡别人也用不上，
        留卡记的腾空时刻一过连回填都放不进）。返回 (各卡空闲, GPU 进程)。"""
        now = _C.time.time() if now is None else now
        need_gpu = (any(j.get("ngpu", 1) > 0 or j.get("excl") for j in self.running.values())
                    or any((r["ngpu"] > 0 or r["excl"]) and not r["held"] and not r.get("dep") and not r["stall"] and self.gang_soon(r, now) for r in reqs))
        self.keepalive(need_gpu)                  # 先停保活再看显存，否则保活占的显存会让任务以为没卡
        gpus = _M.query_gpus() if any(r["ngpu"] > 0 for r in reqs) else []
        apps = self.gpu_now() if any(r["excl"] for r in reqs) else []
        live = [r for r in reqs if not r["held"] and not r.get("dep")]
        excl = {r["key"] for r in live if r["excl"]}
        self.excl_hold = {k: v for k, v in self.excl_hold.items() if k in excl}
        names = {r["key"]: r["name"] for r in live if r.get("gang")}
        self.gang_hold = {k: h for k, h in self.gang_hold.items() if names.get(k) == h["name"]}
        return gpus, apps

    def note_waits(self, waiting, reqs, now):
        """等待的记录：原因变了记事件（依赖等不来的记 dep，其余 wait；单元另写 .why 给申请方；因 CPU 被挡的几种原因算一类，来回换不再记），沿用开始等的时刻（wsince），收掉没人等的拷贝线程与推迟记录。
        一直因 CPU 被挡、而且该告警（place 的原因代码 CPU_ALERT_CODES：核真被我们自己的任务用着的 cpu 是正常排队，不算）从何时起记在 cpu_block：
        满 CPU_ALERT_S 才在等待项写 cpu_wait（秒），看板据此告警；
        中间换了原因、换了任务就重新计时。"""
        for w in waiting:                         # 原因里的数字（已等几分钟、剩几 GiB）每轮都变，按去掉数字后的类别去重
            # 因 CPU 被挡的算一类：可放核数随 labmon 之外的占用抖动，原因会在“按记账只剩”“labmon 之外占着”、带不带“封顶为”之间来回换
            cat = "cpu" if w.get("code") in _C.CPU_CODES else re.sub(r"[\d.]+", "#", w["why"])
            if self.noted.get(w["key"]) != cat:
                q = w.get("q") or {}
                bad = (w.get("dep") or [None])[0] not in (None, "wait")          # 依赖等不来（失败、找不到、成环）：另记一类，记录页标红
                self.event("dep" if bad else "wait", "等待 %s%s：%s" % (q.get("jobs") or "单元", " 的 " + w["name"] if w["name"] else "", w["why"]),
                           name=w["name"], project=w.get("project") or q.get("project"), root=w.get("root") or q.get("root"),
                           queue=q.get("jobs"), why=w["why"])
                self.noted[w["key"]] = cat
                if w.get("kind") == "slot":
                    _C.write(os.path.join(_C.SLOTS, w["id"] + ".why"), w["why"])
        keys = {w["key"] for w in waiting}
        self.noted = {k: v for k, v in self.noted.items() if k in keys or k.startswith("_")}
        for w in waiting:                         # 同一个任务一直在等就沿用开始等的时刻
            old = self.wsince.get(w["key"])
            w.setdefault("since", old[1] if old and old[0] == w["name"] else now)
        self.wsince = {w["key"]: (w["name"], w["since"], bool(w.get("held") or w.get("dep"))) for w in waiting}
        old = self.cpu_block
        self.cpu_block = {w["key"]: (w["name"], old[w["key"]][1] if old.get(w["key"], (None,))[0] == w["name"] else now)
                          for w in waiting if w.get("code") in _C.CPU_ALERT_CODES}
        for w in waiting:
            b = self.cpu_block.get(w["key"])
            w["cpu_wait"] = round(now - b[1]) if b and now - b[1] >= _C.CPU_ALERT_S else None
        live = {(r["key"], r["name"]) for r in reqs}
        self.stagers = {k: v for k, v in self.stagers.items() if v["th"].is_alive() or k in live}
        self.defer = {k: v for k, v in self.defer.items() if k in ("scan", "lscan") or k in live}

    def scan_local(self, queues, now):
        """每 LOCAL_EVERY 秒扫一次待跑任务的本地依赖（续租前看会不会丢）；本地盘（LocalDisk）与拿着租约时的共享盘（Disk）各走一步。
        本地盘的扫法：根盘测得准（info.rootfs 的 ok）且知道开机时刻时扫整个根盘、只进开机以后动过的目录；借来的主机上只数借来以后的
        （guest 指令的时刻；借用中指令重下时刻会变，取这次借用里最早的那个）。测不准时只扫 labmon 目录。"""
        if now - self.local_t >= _Q.LOCAL_EVERY:
            self.local_t = now
            try:
                # 只留 40 行：补不回来的排前面，几个项目的任务多了也不会把要紧的挤掉；休眠队列无待办，跳过不扫
                self.local = {"t": now, "jobs": sorted(_Q.local_deps([q for q in queues if not q.get("sleeping")], _C.DATA_ROOT, self.qenv), key=lambda r: r["heal"])[:40]}
            except Exception as e:  # noqa: BLE001 — 统计出错不能拖垮调度
                self.wlog("统计本地依赖出错：%r" % e)
        def local():
            d = self.ldisk
            st, g = self.info.get("started"), self.guest()
            prune = round(st) if st and (self.info.get("rootfs") or {}).get("ok") else None
            # 借来的时刻往下取整（#70）：四舍五入会把起点往后挪最多半秒，借来后半秒内写的文件（文件时刻按内核的粗时钟，还可能早几毫秒）就漏数了
            d.setup(prune, max(prune, int(g.get("t") or prune)) if g and prune else prune, bool(g))
            d.step(now, self.io_yield)
            self.storage = dict(d.summary(), projects={p: q["project"] for q in queues for p in [_C.project_dir(q["root"])] if p})
        self.disk_do("本地盘", local)
        if self.disk:
            def shared():
                # 项目根取本机登记的与别的主机状态里的（peers 读好的 peer_st，同一个 worker_home 才同一个扫描者）合起来（#53）
                self.disk.want({p for q in queues for p in [_C.project_dir(q["root"])] if p}
                               | {d for st in self.peer_st.values() for d in ((st.get("storage") or {}).get("projects") or {})})
                self.disk.step(now, self.io_yield)
                self.disk_ends(now)
            self.disk_do("共享盘", shared)

    def disk_ends(self, now):
        """有清单任务跑完（各主机的 usage.jsonl，load_hist 读的；成败都算）：它的项目目录下的单位尽快重扫（Disk.hint）。
        看板拿跑完那次重扫与开跑前的扫描之差算任务写入量（disk.project_storage 的 jobs，#53）。只有拿着租约的那台有索引，hint 才起作用；
        只认近一小时结束的（worker 重启后不把一天的旧记录都重扫一遍），已提前过的不再提前。没拿着租约时不记“提前过”，接手（读进新索引）后重新看近一小时的。"""
        d = self.disk
        if not (d.mine and d.ix is not None):
            return
        if self.hint_ix is not d.ix:
            self.hint_ix, self.hinted, self.hint_v = d.ix, set(), -1
        if self.hint_v == self.hist_v:
            return
        self.hint_v = self.hist_v
        cur = {(h.get("host"), h.get("root"), h.get("name"), h.get("t0")): h for h in self.hist
               if h.get("root") and (h.get("t1") or 0) >= now - 3600}
        for k, h in cur.items():
            if k not in self.hinted and _C.project_dir(h["root"]):
                self.disk.hint(_C.project_dir(h["root"]))
        self.hinted = set(cur)

    def scan_chain(self, now):
        """主循环拍与拍之间（每 0.5 秒）调：哪个扫描者的线程做完了一件，就马上收结果、开下一件，不等下一拍（#153）。
        开下一件照样经 step 的 defer（共享盘慢时让路）；限速按扫描者合起来算（Pace 的 rate）。"""
        for name, d in (("本地盘", self.ldisk), ("共享盘", self.disk)):
            if d and d.job and not d.job[0].is_alive():
                self.disk_do(name, lambda: d.step(now, self.io_yield))

    def disk_do(self, name, fn):
        """跑一步共享盘或本地盘的统计：出错不能拖垮调度，同一个错只记一次 worker.log。"""
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            if self.noted.get("_du" + name) != str(e):
                self.wlog("统计%s出错：%r" % (name, e))
                self.noted["_du" + name] = str(e)

    def write_status(self, queues, waiting, limits, pol, use, now):
        """写 status.json（看板探针读；字段是看板 ↔ worker 的契约，tests/test_contract.py 守着）。"""
        S = self.snapshot(limits)
        u10 = self.util10(now)                # 各卡「在用时多高」：fit、cpu_fit 与同机多开车道（local_cards）用
        self.cpu_hints(S, u10)
        hits = [round(t) for t in self.ka_hits if t > now - _K.FIGHT_S]   # 近 FIGHT_S 秒保活被拉起的时刻（kafight，重启后 load_prev 找回）
        _C.write(self.p.status, json.dumps({
            "t": _C.time.time(), "pid": os.getpid(), "host": self.host, "info": self.info, "gpu_procs": self.gprocs,
            "gate": {"on": self.gate, "at": _C.GATE}, "limits": limits, "overrides": self.ovr, "local": self.local, "storage": self.storage,
            "voided": self.voided,
            "kafight": dict(self.fight, n=len(hits), hits=hits) if hits else None,
            "throttle": self.throttle(now),       # 这一段 CPU 限流与超核的任务（Worker.throttle）；没在限流为 None
            "ka_stop": self.ka_stop_t if self.ka_off else None,   # 要卡的这段里 worker 停着保活：停它的时刻（接管时找回，load_prev）
            "keepalive": {"ctl": bool(self.ka_reg), "why": self.ka_why,   # worker 管不管这台的保活（有 ka.sh）、没任务要卡却不拉起的原因（ka_lines 用，#61）
                          "stuck": self.ka_stuck(),                           # 要卡时停不了它（#102）：ka_lines、看板告警、fit 照它说、照它占着算
                          "mine": self.ka_mine},                              # worker 自己开的、此刻还在的保活（#102）：归还时 borrow.finish 据此登记成看板保活
            "watchdogs": _K.load_frozen(self.p),
            "guest": {"keep": self.keep(), "other": {"vram": self.other["vram"], "ram": self.other["ram"] and round(self.other["ram"], 2),
                                                     "cpu": self.other["cpu"] and round(self.other["cpu"], 2)},
                      "back": list(self.back.values()), "quiet": sorted(set(self.back_last) - set(self.back)), "tight": self.tight,
                      "hit": self.hit, "away": self.away_state(),
                      "owner": self.owner,      # 其中算主人回来的（#25：不含只因停不了的保活超出的卡）；算力池借来的看板据此要还
                      "block": self.guest_block()} if self.guest() else None,        # 整台不放新任务的原因（guest_block）：fit 照它挡整台
            "util10": u10,                        # 「在用时多高」（fit 用）
            "gpu_busy": self.busy_view(now),      # 「吃满程度」（只有保活时按 0；页面上的利用率，#51）
            "disk": self.disk.brief() if self.disk else None,
            # 按记账还能放多少：内存 GiB、各卡 MB（整卡任务记账超过卡时写 0）、CPU 核（cpu_cap 是记账上限：配额向下取整；读不到配额都为 None）
            "room": {"ram": _S.ram_free(S), "margin": S["margin"], "gpus": {u: max(0.0, v) for u, v in _S.card_free(S).items()},
                     "cpu": _S.cpu_free(S), "cpu_cap": S["cpu_cap"], "other_cpu": S["cpu"],   # other_cpu：认得准的 labmon 之外的核数（fit 判整机独占，同 intruders）
                     "keep": {c["uuid"]: round(c["keep"]) for c in S["cards"] if c["keep"]},   # 各卡给对方留着的 MB（keep_left）：fit 的整卡封顶 card_need 照它
                     "owner": sorted(S["owner"])},        # 不放的卡（对方超出预留或刚回落、有停不了的保活）：fit 的快照照它（fit_snapshot）
            "queues": [{"root": q["root"], "jobs": q["jobs"], "gpus": q["gpus"], "project": q["project"], "stop": q["stop"], **({"pool": True} if q.get("pool") else {}),
                        "empty": q.get("empty", False), "sleeping": bool(q.get("sleeping")), "empty_since": q.get("empty_since"),
                        "added_t": q.get("added_t"), "dur": self.durs.get(q["key"], {}),
                        "par": q.get("par") or 1} for q in queues],   # 清单声明的并行数（list_par，#147）：看板车道写“并行 k/N”
            "reg_idle": {k: {"since": round(v["since"]), "sig": list(v["sig"]) if v.get("sig") else None,
                             "rsig": list(v["rsig"]) if v.get("rsig") else None, "sleep": bool(v.get("sleep"))}
                         for k, v in sorted(self.reg_idle.items())},   # 跑空登记的空置起点与休眠签名（#164；重启接管时找回）
            "plan": self.plan,                    # 推演（forecast）：各队列与单元每个任务的预计开始、结束、用哪几张卡
            "todo": self.todo,                    # 还会跑的任务（list_state）：别的主机的依赖按它认谁还会跑、排在谁后面
            "pool_heads": self.pool_view(now),    # 算力池里待放的各清单还要跑什么、队首依赖满足没有（#25）：看板据此放、借
            "pool_t": round(self.peer_t),          # pool_heads 是何时读的池目录（peers 每 PEER_EVERY 秒一次）：看板逐条取读得最晚的那台（#86）
            "dep_miss": [[p, n, int(t)] for (p, n), t in sorted(self.dep_miss.items())],   # 依赖等的、哪里都找不到的任务与起算时刻（dep_block；接管时找回，取整往前不往后）
            "offers": self.offer,                 # 能接别的主机上哪些任务、接来后几点跑完（offers）：看板汇总成挪法建议
            # 公平份额：生效的设置、这一轮开始时各项目衰减后的 GPU 用量（卡·时）、本机在跑的（别的主机读它算进用量）
            "share": {"wait_cap_min": pol["cap"] / 60, "half_life_h": pol["hl"] / 3600, "weight": pol["w"],
                      "use": {k: round(v, 3) for k, v in use.items()}, "run": self.own_runs(now)},
            "running": [dict(self.job_view(j), t0=j["t0"], vram=j.get("vram"), excl=j.get("excl"), est_min=j.get("est_min"),
                             oom0=j.get("oom0"), fc0=j.get("fc0"), owner_gpu=j.get("owner_gpu"), wait=j.get("wait"), cpus=j.get("cpus"), io=j.get("io"),
                             io_sus=j.get("io_sus"),   # 记过读写拥塞嫌疑的时刻（io_suspect；接管时找回，同一任务不重复记）
                             # 这一次运行判过的开跑核数给少、给多（cpu_hints，#46）：看板据此出只给实验会话的 cpu_low:、cpu_high:，按 cls 合并同类（#91）
                             cpu_fit=dict(j["cpu_fit"], **_S.cpu_cls(j)) if j.get("cpu_fit") else None,
                             # 成组（#180）：组名、人数、一起开跑的同伴 [[主机, 任务]]、跟着同伴停下的 {kind, why, sent}（接管时找回）
                             gang=j.get("gang"), gn=j.get("gn"), gang_with=j.get("gang_with"), gang_stop=j.get("gang_stop"))
                        for j in self.running.values()],
            "external": [dict(self.job_view(e), t0=e["t0"]) for e in self.ext.values()],   # 开始时刻同 running[]：epoch 秒 t0
            "waiting": [{"kind": w.get("kind", "queue"), "queue": w["q"]["jobs"] if w.get("q") else None, "root": w["q"]["root"] if w.get("q") else None,
                         "project": w.get("project") or (w["q"]["project"] if w.get("q") else None), "name": w["name"],
                         "excl": w.get("excl"), "est": w.get("est"), "why": w["why"], "since": w["since"], "cpus": w.get("cpus"), "io": w.get("io"),
                         "cpu_wait": w.get("cpu_wait"),          # 一直因 CPU 被挡满 CPU_ALERT_S 后写已挡了多少秒（note_waits），看板据此告警；没满为 None
                         "miss": bool(w.get("miss")),            # 缺本地文件又补不了（Worker.place 定的）：看板的提醒与主机页按它认
                         "held": (w.get("held") or [None])[0],   # 挡住它的临时指令种类（drain、pause、deadline、guest）：看板据此标暂停
                         "dep": (w.get("dep") or [None])[0],     # 依赖没满足（dep_block）：wait 在等；failed 等的跑失败了、missing 找不到、cycle 成环，这三种看板出告警
                         "pos": w.get("pos"),                     # 排位（admit）：等待表的“排第几”与解释
                         "elsewhere": w.get("elsewhere"),         # 整条清单在等别的主机放行的那一格收尾（run_elsewhere）：[主机, 任务]；算力池据此把它放回池里（#80）
                         "start": self.starts.get((w["key"], w["name"]))}   # 推演（plan）里它预计何时开始；推演里排不上的没有
                        for w in waiting]}, ensure_ascii=False, indent=1))

    def qenv(self, q):
        """队列的变量（读 .env 要起 bash，按修改时间缓存）。"""
        try:
            mt = _C.fsig(q["env"]) if q["env"] else None
        except OSError:
            mt = None
        c = self.envc.get(q["key"])
        if not c or c[0] != mt:
            c = self.envc[q["key"]] = (mt, _Q.env_of(q))
        return c[1]

    def job_view(self, j):
        """运行中任务写进 status.json 的公共部分：命令与日志末尾供页面展开看，估计值与记账供核对准入。"""
        q = j.get("q")
        lg = os.path.join(q["logs"], j["name"] + ".log") if q else j.get("log")
        a = self.acct(j) if not j.get("ext") else None
        return {"kind": j.get("kind", "queue"), "queue": q["jobs"] if q else None, "root": q["root"] if q else None,
                "project": j.get("project"), "name": j["name"], "pid": j["pid"], "gpus": j["gpus"], "tag": j.get("tag"),
                "hold": _C.is_hold(j["name"]),
                "stop": j.get("stop"), "use": j.get("use", {}), "cmd": j.get("cmd", ""), "est": j.get("est"),
                "acct": {"ram": round(a["ram"], 2), "vram": round(sum(a["vram"].values())), "cpu": round(a["cpu"], 2)} if a else None,
                "log": (os.path.relpath(lg, q["root"]) if q else lg) if lg else None, "tail": _C.log_tail(lg) if lg else []}

    def run(self, replace=False):
        os.makedirs(self.p.queues, exist_ok=True)
        old = _C.read_pid(self.p.pid)
        if replace and old != os.getpid() and _C.alive(old, ("labmon_worker",)):
            os.kill(old, signal.SIGTERM)          # 升级：旧 worker 这一轮 tick 结束就退、任务不动，由本进程接管
            while _C.alive(old, ("labmon_worker",)):   # 一直等到它退出：任何时候只能有一个 worker 在放行（本进程由 workerctl 放在后台，等多久都不卡命令行）
                _C.time.sleep(0.5)
        if old != os.getpid() and _C.alive(old, ("labmon_worker",)):
            print("worker 已在运行（PID %d）" % old)
            return 0
        _C.write(self.p.pid, str(os.getpid()))
        self.load_prev()                          # 旧 worker 退出前最后一轮可能又放了任务、写了状态：按它最终的状态接管
        if os.path.exists(self.p.stopped):
            os.remove(self.p.stopped)
        with _C.flock(self.p.events):
            ev = (_C.read(self.p.events, "") or "").splitlines()
            if len(ev) > 4000:                    # 事件只留最近的
                _C.write(self.p.events, "\n".join(ev[-2000:]) + "\n")
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        info = self.info or _M.host_info([], _C.DATA_ROOT)
        self.event("worker", "worker 启动 PID=%d 主机=%s（容器 %s 启动）" % (os.getpid(), self.host, _C.now_str(info["started"]) if info.get("started") else "?"))
        first = True
        while not stop:
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:   # 收掉已退出的子进程，免得留僵尸
                    pass
            except ChildProcessError:
                pass
            try:
                self.tick(self.registry(), first)
            except Exception:                     # 输出接的是 /dev/null：崩之前把出错位置记进 worker.log（主机页的 worker 日志），看板再拉起
                self.wlog("worker 出错退出（运行中的任务继续跑，下次启动时接管）：\n" + traceback.format_exc().rstrip())
                raise
            first = False
            for i in range(int(_C.TICK * 2)):
                if stop:
                    break
                _C.time.sleep(0.5)
                self.scan_chain(_C.time.time())     # 扫描做完一件就接着下一件，不等下一拍
                if i % 4 == 3 and (self.new_slot() or self.gang_moved()):   # 单元申请、成组的同伴就绪或开跑约 2 秒内处理
                    break
        self.wlog("worker 退出（运行中的任务继续跑，下次启动时接管）")
        if _C.read(self.p.pid) == str(os.getpid()):
            os.remove(self.p.pid)
        return 0

    def new_slot(self):
        ids = {os.path.basename(f)[:-4] for f in glob.glob(os.path.join(_C.SLOTS, "*.req"))}
        new = ids - self.seen
        self.seen = ids
        return bool(new)
