#!/usr/bin/env python3
"""labmon worker：服务器端统一的队列执行器，取代各项目自带的 gpu_queue.sh。每台主机跑一个。

    python3 labmon_worker.py run   --host 29                     常驻（本机用 labmon worker start 29 拉起）
    python3 labmon_worker.py add   --host 29 ROOT JOBS [--gpus 0]  登记一条队列（ROOT 绝对路径，JOBS 相对 ROOT）
    python3 labmon_worker.py rm    --host 29 ROOT JOBS
    python3 labmon_worker.py ls    --host 29                     登记的队列、运行中与等待中的任务
    python3 labmon_worker.py fit   --host 29 NAME…               开并行车道前：与真放行同一份准入，放不放得下、卡吃不满是不是 CPU 受限
    python3 labmon_worker.py fit   --host 29 ROOT JOBS --list --project P [--hosts 29,30]   一条清单还会跑的行此刻能在这几台上再开几条车道
    python3 labmon_worker.py stop  --host 29                     停 worker；运行中的任务不受影响，下次启动时接管
    python3 labmon_worker.py thaw  --host 29                     恢复借来时暂停的保活看门狗（还卡时 borrow.finish 调）

清单与状态文件沿用 gpu_queue.sh 的约定，看板不用改：
- 清单每行 `名称|命令`，# 开头为注释；每启动一个任务前重读，插队、追加就改清单（整体替换文件）。
  行尾可写 `# est_min=30 gpus=1 vram=20 exclusive=gpu`：估时、要几张卡（0 为不用 GPU；hold 占位任务一律不占卡，不用写）、
  每张卡要多少空闲显存、是否独占（gpu 独占所用的卡，host 独占整台主机；开销评估、计时测量用）。
  任务依赖写在行尾：`# after=任务1,项目/任务2` 等这些任务跑成功才开始，`afterany=` 结束就行（成功失败都算）；
  被等的可以在别的队列、别的主机、别的项目（按任务名认，见 dep_state）。等的失败了、找不到时不放行，等待原因写明并出告警。
- 同名 .env 在 ROOT 下用 bash 读入（set -a，赋值即导出），可设 PY、QUEUE_STATE、QUEUE_LOGS 等；
- 状态目录：lock（worker PID）、current、<名称>.done / .failed、queue.log；paused 存在时不启动新任务。

与 gpu_queue.sh 的不同：
- 一台主机上的多条队列由同一个 worker 调度，各队列内部仍一次一个任务；不同队列在 GPU 空闲显存够时并行。
- 放行顺序（admit）与准入（place：能不能放、选哪几张卡、独占留卡与回填、给对方的预留）只有一份，真放行按实时采样拼的资源快照（snapshot）调它，
  推演（simulate）在模拟的时间轴上维护同样的快照调它。顺序（fair_key）：先比人，同一人的几个项目的衰减 GPU 用量（卡·时，各主机一起算，
  在跑的按预计时长先记一笔）合起来算一份，小的先；同一人的项目之间 focus 的先，再比用量 ÷ 权重，小的先；项目内先来先跑（#23）。
  按这个顺序第一个卡在资源上又等满 wait_cap 的成为屏障，
  排在它后面的只能回填（预计在它放行前跑完的才放）。
  半衰期、wait_cap 与各项目权重由看板按用户级主机清单的 [sched] 写进 sched.json，没写的用缺省；项目属于哪个人（owner，看板按项目主人给的人员键）也随它来；
  项目级的暂停与优先（[sched.pause]、[sched.focus]）
  也随它来，当成同名的临时指令（sched_ovr，所有主机生效）。
  选卡看 nvidia-smi 的空闲显存（别人占着的卡自然排除），用 CUDA_VISIBLE_DEVICES=<UUID> 固定到选中的卡。
  显存、内存按 max(估计, 实测) 记账，CPU 开跑满 10 分钟后估计那份按近 10 分钟窗口核数回收（#65）：内存上限扣 labmon 之外收不回的内存（mem.anon，页缓存不算），CPU 上限是配额向下取整，
  估计显存、内存逐项取同 tag › 同名的峰值 ×1.1 › 清单的 vram=、ram= › 同队列的峰值 ×1.1（estimate），
  估计核数取清单的 cpus=、否则同类峰值 ×1.1、都没有按 1 核；请求一直因 CPU 被挡满 10 分钟才在等待项写 cpu_wait 给看板告警。
- 任务在自己的会话里运行，退出码写进 <名称>.rc：worker 重启（升级、被 kill）不影响任务，新 worker 接管后照常记完成。
  服务器重启后任务与 worker 都没了，看板拉起 worker（主机清单标了 worker = "auto" 的自动拉，否则手动）后中断的任务从头重跑（任务内部断点续跑由各实验自己负责）。
- GPU 保活由 worker 统一停启：有 GPU 任务在跑或在等卡时停保活，并在 $AGRI_ROOT/queues/labmon-<主机>/pid 登记；
  都没有时删登记，并像看门狗一样每约 30 秒看一次：连续两次本机没有别的队列登记在跑、GPU 上也没有任何进程，才 ka.sh start
  （worker 刚启动、服务器重启后也一样，不依赖 keepalive-demo 的看门狗）。
  保活在跑时 GPU 上有了别的进程就停掉让路（开的时候本来就要 GPU 上没有进程）。
  要卡的这段里保活停过又被拉起来，记“保活被拉起”事件（ka.sh 记的调用者、找到的看门狗）。借来的主机上一借来就找一遍看门狗、之后每 10 分钟一遍，
  找到的只列出来（frozen.json 的 seen）；真把保活拉起来了，才 SIGSTOP 暂停确定的（保活的父进程、提到保活程序又在循环里的脚本等，见 watchdogs）、
  记进 frozen.json，还卡时 thaw 恢复。下面在跑程序的（对方的实验队列）不暂停。它正持着平时不持的文件锁（如拉起保活时持着对方 run/start 共用的锁）时
  这一轮不暂停、也不停保活，等它放了再暂停（held_locks，#38）。借来的主机上 ka.sh 停不了的保活：那张卡不放，内存与核数不算对方的用量、另从可用量里扣（#37）。
- 独占任务轮到时不再往它要的卡（或整台主机）放别的任务（预计在腾空前跑完的除外），等腾空后启动；它运行期间那张卡（主机）也不放别的任务。
  挡着的是 labmon 之外的进程（借来的主机上多半是对方）时不留卡、不挡别的任务，等它们用完；独占卡也扣给对方的预留，多卡先挑没给对方留的。
  运行中连着两轮（约 20 秒）同卡（整机独占：同机，含 labmon 之外的 CPU 占用超过 1 核）有别的进程，结果就不能用了：
  不管停法直接 SIGTERM，记“作废”，清单任务重新排队（单元由申请方重新申请）。放行前与作废看的是同一个判定（intruders）。
- 借来的主机（guest 指令）：只给指令里的项目用；指令带着给对方留的量 keep（显存 GiB、内存 GiB、CPU 核，看板按对方轻用时的历史用量推荐）。
  借来时对方在轻用的（keep.hold）准入时从对方的卡（单卡就是它；多卡是 0 号卡与对方有进程的卡）与容器内存里扣掉对方还没用上的预留，CPU 扣 max(对方实测, keep.cpu)；
  借来时对方空着的不扣（没人一起跑），对方回来用时有了这人 GPU 进程的卡就算超出。
  对方说好不用到某个时刻的（keep.away，用户勾的；看板取约定时间）：到那时以前不预先留、不判对方的内存与 CPU 超出（此刻占着的照算在可用量外），
  对方有了新的 GPU 进程（连着两轮）或内核杀了 labmon 之外的进程（我们没有任务在跑时）就当对方回来了：这次作废，立刻按普通规则（状态的 guest.away.end，看板清掉、提醒）。
  对方超出预留的那部分（那张卡、内存、CPU）不放新任务，回到预留以内 10 分钟后恢复；正在跑的不停（这一格跑完，停了白跑），
  对方超出预留且某卡快满、或容器内存到 95% 时只记“快撞上限”，看板提醒用户线下和对方商量。
  我们的任务 oom_score_adj 设 1000：真撞上容器内存上限，内核先杀我们的。被挤掉（期间容器 OOM 杀过进程，或 CUDA OOM 且对方在同卡）
  的失败不记失败、重新排队，同一任务最多两次；内核杀了 labmon 之外的进程时记下（看板据此提醒、调高预留）。
- 每轮现读主机容量（各卡显存与利用率、容器内存上限、工作集与收不回的内存、CPU 配额、磁盘、容器启动时间）与压力（CPU 限流、共享盘每次读写的
  执行时间、本地盘忙碌，有 PSI 时带上；每 5 分钟记一行进 press.jsonl，共享盘延迟的基线 io_base 按它算），并记每个任务的
  内存（会话内进程的 PSS 之和）、核数、读写（/proc/<pid>/io）与显存（nvidia-smi 给的是宿主机 PID，经 /proc/<pid>/sched 对回容器 PID），
  写进 status.json；任务结束时把峰值与运行期间有没有别的任务同卡、同机写一行到 usage.jsonl。读写的量准入不看；
  共享盘慢（超过基线且每秒不少于 IO_MIN_OPS 次）时共享盘扫描与拷本地依赖让路（最多推迟 IO_YIELD_MAX_S），
  在跑的任务已跑过同类历史 1.5 倍又遇上共享盘慢时只记一条“读写拥塞嫌疑”事件。
- 开跑时导出 LABMON_CPUS（开跑时给这个任务记账的核数，之后回收记账不改它），项目拿它设线程数；任务环境里没设的
  OMP_NUM_THREADS、MKL_NUM_THREADS、OPENBLAS_NUM_THREADS 缺省同它（cpu_env；清单行与 .env 里设了的不盖）。
- 任务要跑多久（duration）：同类跑过的实际耗时、实际/估时 比值折算 est_min（各主机的 usage.jsonl 一起看）；各清单每个任务的预计时长
  写进 status.json 给看板排预计时间，截止前的准入在它上面再乘 DUR_MARGIN。

只用 Python 3.8 标准库（服务器镜像自带 3.8）；不依赖 labmon 包，单文件部署到共享盘。
"""
import argparse
import contextlib
import datetime
import fcntl
import glob
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import threading
import time
import traceback
from pathlib import PurePosixPath

HOME = os.environ.get("LABMON_HOME") or os.path.dirname(os.path.abspath(__file__))
TICK = float(os.environ.get("LABMON_TICK", "10"))
KA_CHECK_S = 3 * TICK    # 空闲时多久看一次要不要拉起保活
KA_BIN = os.environ.get("KA_BIN", os.path.join(HOME, "keepalive", "bin"))
PROC = os.environ.get("LABMON_PROC", "/proc")          # 只用于统计用量；测试时指向假目录
CG = os.environ.get("LABMON_CGROUP", "/sys/fs/cgroup")
NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
HOLD_RE = re.compile(r"(?i)(?:^|[_.\-\d])(?:hold|pause)(?:$|[_.\-\d])")   # 只在 is_hold 里用
GIB = 2 ** 30
DATA_ROOT = os.path.dirname(HOME.rstrip("/"))          # 共享盘根（worker 装在 <data_root>/labmon）
LOCAL = os.environ.get("LABMON_LOCAL", "/tmp/labmon")   # 容器本地盘上 labmon 管的目录：单格临时、项目缓存、单元申请
ROOTFS = os.environ.get("LABMON_ROOTFS", "/")           # 容器根盘（续租、容器重启会清空）：开机以来的新写入按它算、扫它（LocalDisk）；测试指到假目录
SLOTS = os.environ.get("LABMON_SLOTS", os.path.join(LOCAL, "slots"))   # 单元申请的交换目录：容器本地盘，不走 NFS
GATE = 0.9               # 容器里收不回的内存（mem.anon，不算页缓存）超过上限的这个比例就停止放行
WAIT_CAP_S = 1800        # 缺省：请求卡在资源上等了这么久，排在它后面的就不能再越过它，只能回填（预计在它放行前跑完的才放）；sched.json 的 wait_cap_min 可改
HALF_LIFE_S = 86400      # 缺省：项目之间比的 GPU 用量（卡·时）按这个半衰期衰减；sched.json 的 half_life_h 可改
ONCE_HOLD_S = 1800       # 不带 --host 的 pause --now 下的“只停一次”：等全局暂停随 sched.json 送到才删，最多挡这么久（看板约 1 分钟一轮）
POLICY = {"cap": WAIT_CAP_S, "hl": HALF_LIFE_S, "w": {}, "own": {}}   # 调度设置的缺省（policy 读 sched.json 覆盖）：w 为各项目权重，不写为 1；own 为项目 → 人员键，没写的项目合起来算一个人
POOL = "pool"            # 算力池的保留主机 id（#24，看板那边 config.POOL）：池里待放的清单登记在 hosts/pool/queues/，没有 worker；peers 读它认依赖
PEER_EVERY = 60          # 别的主机在跑的任务（各自状态里的 share.run，公平份额要算上）多久重读一次
STATUS_STALE_S = 180     # status.json 这么久没更新（按里面的 t）就算那台 worker 不在：别的主机（peers）与本机（fit）同一口径，与看板的 config.STALE_S 一样
DEP_STALE_S = 86400      # 别的主机 worker 不在（状态没更新、也没 worker stop）这么久以内，它清单里没跑完的照样算还会跑：续租、重启时等它的不误报找不到
DEP_MISS_S = 120         # 依赖等的任务哪里都找不到要持续这么久才报找不到（#56）：两边清单同时加行时，被等的那台还没认出、或这台读别的主机状态的缓存（PEER_EVERY）还是旧的
MISS_NOTE = "还没在哪台主机的清单里见到它，刚加的行要等那台 worker 认出；%d 分钟后仍没有就报找不到" % (DEP_MISS_S // 60)   # 时限内的等待原因（dep_block）
# 成组（清单行尾 gang=组名:人数，#180）：几台主机上的成员都留好资源再一起开跑、一个失败或停下其余的跟着停。各台只经共享盘上各自的 gang.json 交换状态
GANG_FRESH_S = 60        # 同伴写的“资源已留好”（gang.json 的 ready，按文件里那台自己写的时刻）这么久以内才算此刻就绪：都就绪才一起开跑
GANG_JOIN_S = 90         # 同组成员开跑时刻相差这么久以内算同一次开跑：同伴已开跑这么久以内、这边留好了就跟上；一起停、结果都按同一次认
GANG_LATE_S = 180        # 开跑这么久同伴还没跟着开跑（它留的资源刚被占了、清单刚被暂停、worker 没了）：按停法停下、重新排队
GANG_SOON_S = 120        # 成员只在组里预计这么久内就能一起开跑时才算要卡（停保活）；等得久的保活照开
GANG_KEEP_S = 86400      # gang.json 里结束的记录留这么久：同伴按它认同一次开跑的结果（worker 重启时读回）
GUEST_QUIET_S = 600      # 借来的主机上对方的用量回到预留以内后，这么久内那部分（那张卡、内存、CPU）仍不放新任务（对方可能接着跑下一个）
TIGHT_FREE_MB = 1024     # 借来的主机：对方超出预留、某卡空闲显存不到这么多时算快撞上限（提醒，不停正在跑的）
TIGHT_MEM = 0.95         # 借来的主机：对方超出预留、容器里收不回的内存（不算页缓存）到上限的这个比例时算快撞上限
SQUEEZE_MAX = 2          # 借来的主机上同一任务被对方挤掉（OOM）后重新排队的次数上限，再被挤掉就记失败
GUEST_ADJ = 1000         # 借来的主机上我们任务的 oom_score_adj：撞容器内存上限时内核先杀我们的，不杀对方的
CUDA_OOM = re.compile(r"CUDA out of memory|OutOfMemoryError|CUDA error: out of memory|CUBLAS_STATUS_ALLOC_FAILED")
VOID_CPU = 1.0           # 整机独占时 labmon 之外的进程占这么多核算打扰
IDLE_CORES = 0.2         # 不用卡、平均不到这么多核的任务（如等单元放行的监督进程）：整机独占不等它，也不算打扰
VOID_KEEP_S = 86400      # 作废记录在状态里留这么久（看板据此告诉实验会话）
HIST_N = 5               # 估计值看同类任务最近几次
RATIO_N = 20             # 项目的 实际/估时 比值看最近这么多个跑成功的任务
DUR_MARGIN = 1.2         # 截止前（续租、约定归还）的准入：预计时长再乘这么多（估时偏一点是常事，跑不完这一格就白跑）
OVER_FRAC, OVER_MIN_S = 0.2, 600   # 剩余时长说不准时（超了预计；看板那边还有进度刚开始、没估时或已超估时）按已跑的这么多比例估、至少这么多秒（overrun_left）
UTIL_WIN_S = 600         # 各卡利用率取近这么久的平均（状态的 util10，fit 列出来看；吃满程度 gpu_busy 的 m10 同一个长度）
BUSY_WIN_S = 3600        # 各卡「吃满程度」（只有保活时按 0）留近这么久的样本：状态 gpu_busy 的 h1（页面主数字，#51）
UTIL_LOW = 50            # 卡近 UTIL_WIN_S 的利用率低于这个 %，且上面在跑的被 CPU 卡住时，先加核、不在这张卡上开新车道（gpu_slack，#35、#37）
CPU_BOUND_FRAC = 0.9     # 在跑任务近 CPU_WARM_S 各 1 分钟窗口核数的平均不少于给它的核数的这么多，算被 CPU 卡住、可能给少了（cpu_fit 的 low）
CPU_SPARE_FRAC, CPU_SPARE_MIN = 0.5, 1.0   # 写了 cpus= 的在跑任务整段 1 分钟峰值不到给的核数的这么多、且少至少这么多核，算给多了（cpu_fit 的 high，#46）
UTIL_FULL = 90           # 卡近 UTIL_WIN_S 的利用率不低于这个 % 算吃满：offers 的同机多开车道不往它上面放（Worker.local_cards，#43；同卡 1 条车道常只到六七成，3–4 条才近满）
CONC_MAX = 12            # 按同卡并行数挑同类记录（duration 的 k）时封顶到这么多（推演里查表、缓存用）
FORECAST_S = 7 * 86400   # 推演（simulate）往后排多远
FORECAST_EVERY = 120     # 推演的输入没变时多久重算一次（在跑的超了预计，后面的跟着往后挪）
MOVE_MAX = 5             # 能接别处的任务（offers）：每个项目至多挑几行（从别的主机排得最晚的清单队尾往前取）
MOVE_LEAD_S = 600        # 原处预计这么久内就开跑的不挑：会话剪行时它可能正好放行，两边都跑
MOVE_TRAP_MAX = 20       # 原处跑不了的清单（约定、续租截止前跑不完，drain 着…）一次至多挑几行：挪完才推得出整批何时跑完，MOVE_MAX 常常不够
PEAK_WIN = 60            # 任务的核数、读写峰值按这么长的窗口算（1 分钟峰值；窗口满了结一次，沿用每轮的采样）
PRESS_WIN = 60           # 主机的 CPU 限流、共享盘延迟、本地盘忙碌，“此刻”取近这么久的计数差
PRESS_EVERY = 300        # 多久往 press.jsonl 记一行这段时间的聚合值（共享盘延迟基线的历史）
PRESS_KEEP_S = 14 * 86400   # press.jsonl 留多久，也是基线看的范围
THR_HOT = 25.0           # 近 1 分钟被配额限流的周期占比（%）不低于它，算这台在限流（thr_step；状态的 throttle，#39）
THR_COOL = 10.0          # 限流中降到它以下才算缓过来（滞回，免得在门槛上来回跳）
THR_HOLD_S = 600         # 限流持续满这么久，状态的 throttle 标 alert（看板据此告警）
CPU_OVER_RATIO, CPU_OVER_MIN = 1.5, 1.0   # 任务 1 分钟窗口核数 ≥ 给的核数（LABMON_CPUS）× 这么多倍、且多出至少这么多核，算超核（cpu_over）
IO_SLOW_MS = 20          # 共享盘每次读写的执行时间（ms）：基线不低于它，历史不够时就用它（#4 维护者定）
IO_BASE_N = 288          # 基线至少要这么多个有读写的 5 分钟样本（约一天），不够时取 IO_SLOW_MS
IO_MIN_OPS = 2           # 共享盘每秒读写不到这么多次时不判慢、也不进基线：几个 worker 每 10 秒写一次状态（合计不到 1 次/秒）的延迟不决定慢不慢
IO_YIELD_MAX_S = 1800    # 共享盘慢时扫描与拷本地依赖推迟，同一件事连着推迟满这么久就照做一次（不能永远不扫、不拷）
IO_WHAT = {"scan": "共享盘扫描", "lscan": "本地盘扫描"}   # io_yield 推迟的事（键）在 worker.log 里的说法；别的键是拷本地依赖 (请求 key, 任务)
SLOW_RATIO = 1.5         # 运行中任务已跑过同类历史预计时长的这么多倍、同时共享盘慢：记一条“读写拥塞嫌疑”（只记录）
CPU_DEFAULT = 1          # 没写 cpus=、也没有同类历史时一个任务按几个核记账（#4 调研推荐、维护者定）
CPU_ALERT_S = 600        # 请求一直因 CPU 被挡满这么久，等待项才写 cpu_wait（看板据此告警）
CPU_CODES = ("cpu", "cpu_est", "cpu_other", "nofit_cpu")   # 算“因 CPU 被挡”的原因代码（WHY 的键）：记账不够、记账比实测多挡住的、labmon 之外占着、要的比配额还多
CPU_ALERT_CODES = ("cpu_est", "cpu_other", "nofit_cpu")   # 其中要告警的（cpu_wait）：核真被我们自己的任务用着的（cpu）是正常排队，不报（#65 维护者定）
CPU_WARM_S = 600         # 运行中任务开跑（接管后从开始量起）满这么久，CPU 记账从估计改按近这么久的 1 分钟峰值（回收开头加载阶段之后没用上的核，#65）
MIB = 2 ** 20


def now_str(t=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def read(p, default=None):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return default


def write(p, s):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(s)
    os.replace(tmp, p)


def queue_stop_path(mode, root=None):
    """校验清单停法。HTTP 入口只校验语法，worker 在远端另核实际路径。"""
    if mode in (None, "", "boundary", "term"):
        return None
    if isinstance(mode, str) and mode.startswith("file:") and "\x00" not in mode:
        path = PurePosixPath(mode[5:])
        if path.parts and not path.is_absolute() and ".." not in path.parts:
            if root is None:
                return path
            base = os.path.realpath(root)
            parent = os.path.realpath(os.path.join(base, str(path.parent)))
            target = os.path.realpath(os.path.join(parent, path.name))
            if target != base and all(os.path.commonpath((base, p)) == base for p in (parent, target)):
                # 原有原子替换语义：最终一项是符号链接时，替换链接本身。
                return os.path.join(parent, path.name)
    raise ValueError("stop 要用 boundary、term，或 file: 加清单目录内的相对路径")


def write_queue_stop(root, mode, text):
    """沿 ROOT 逐层打开解析后的目录，替换时不跟随符号链接。"""
    target = queue_stop_path(mode, root)
    folders, name = os.path.split(target)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as stack:
        directory = os.open("/", flags)
        stack.callback(os.close, directory)
        for part in filter(None, folders.split("/")):
            directory = os.open(part, flags, dir_fd=directory)
            stack.callback(os.close, directory)
        tmp = ".labmon-stop-" + os.urandom(12).hex()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=directory)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            os.unlink(tmp, dir_fd=directory)
            raise


def read_pid(p):
    """lock、worker.pid、保活登记里的 PID；没有文件或写坏了（不是数字）当作没有，返回 0。"""
    s = read(p, "") or ""
    return int(s) if s.isdigit() else 0


@contextlib.contextmanager
def flock(path, *, required=False):
    """同一文件读改写的互斥（锁在旁边的 .lock 上，write 是整体替换、锁不能加在文件自己上）：
    overrides.json 被命令行 override add/rm 与 worker（到期清理、pause --now 记 applied）同时改，events.jsonl 启动时截断与随时追加。"""
    with open(path + ".lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
        except OSError:                           # 共享盘没起 lockd 时锁不上：已有操作沿用原口径
            if required:
                raise
        yield


def overrun_left(el):
    """剩余时长说不准时的估计（秒）：已跑 el 秒的 OVER_FRAC、至少 OVER_MIN_S，不当“马上结束”（否则后面任务的预计开始永远是“现在”，每轮往后滑）。
    run_end 用；看板 collect.overrun_left 同一口径（进度刚开始、没估时或已超估时），worker 不 import 本包，各一份，改一处要改另一处。"""
    return max(OVER_MIN_S, OVER_FRAC * el)


def is_hold(name):
    """占位任务（等条件再放行；不参与独占，不进时长与等待统计）：名字里按词认 hold / pause——开头、结尾，或被 _ . - 或数字隔开，
    hold、hold_x、x_hold2、pause-1 算，threshold、household、unpaused 不算。看板那边是 config.HOLD_RE，同一条规则。"""
    return bool(HOLD_RE.search(name or ""))


def cmdline(pid):
    """进程命令行；不存在或已成僵尸返回 None。没有 /proc 时（本机测试）退回 ps。"""
    if not os.path.isdir("/proc"):
        r = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,args="], capture_output=True, text=True).stdout.strip()
        return None if not r or r.startswith("Z") else r.split(None, 1)[-1]
    try:
        with open("/proc/%d/stat" % pid) as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return None
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, IndexError):
        return None


def alive(pid, match):
    c = cmdline(pid) if pid else None
    return c is not None and any(m in c for m in match)


def proc_env(pid, key):
    """进程环境变量里的一项（接管时从 CUDA_VISIBLE_DEVICES 找回所用的卡）；读不到或没有为 None。"""
    try:
        with open(os.path.join(PROC, str(pid), "environ"), "rb") as f:
            env = dict(x.split(b"=", 1) for x in f.read().split(b"\0") if b"=" in x)
    except OSError:
        return None
    v = env.get(key.encode())
    return v.decode(errors="replace") if v is not None else None


class Paths:
    def __init__(self, host):
        self.host = host
        self.dir = os.path.join(HOME, "hosts", host)
        self.queues = os.path.join(self.dir, "queues")
        self.pid = os.path.join(self.dir, "worker.pid")
        self.log = os.path.join(self.dir, "worker.log")
        self.status = os.path.join(self.dir, "status.json")
        self.usage = os.path.join(self.dir, "usage.jsonl")
        self.events = os.path.join(self.dir, "events.jsonl")      # 放行、跑完、中断、闸门、临时指令等结构化事件
        self.limits = os.path.join(self.dir, "limits.json")       # {"tags": {"heavy": 2}}
        self.overrides = os.path.join(self.dir, "overrides.json")  # 临时指令：pause / focus / drain / deadline / guest
        self.eta = os.path.join(self.dir, "eta.json")              # 看板写来的运行中任务预计结束（按日志进度算的），推演用
        self.sched = os.path.join(self.dir, "sched.json")          # 看板写来的调度设置（用户级主机清单的 [sched]）：policy 读
        self.returned = os.path.join(self.dir, "returned.json")    # 看板写来的已归还主机（含平台上关了机的自用主机）：peers 读，offers 不让它们挡
        self.stopped = os.path.join(self.dir, "stopped")          # 手动停的标记：本机看板见到它就不自动拉起
        self.frozen = os.path.join(self.dir, "frozen.json")        # 借来的主机上暂停着的保活看门狗（还卡时 thaw 恢复）与可能的看门狗
        self.press = os.path.join(self.dir, "press.jsonl")         # 主机的 CPU 与 IO 压力每 5 分钟一行（Worker.press 写，留 14 天；共享盘延迟的基线 io_base 按它算）
        self.gang = os.path.join(self.dir, "gang.json")            # 本机成组成员此刻怎样（gang_publish 写，同组别的主机读；建了不删）


def qslug(root, jobs):
    """登记文件名：目录末级名 + 清单路径 + 完整目录的短哈希（两个项目的子目录同名、清单路径也相同时不撞）。"""
    return NAME_RE.sub("_", os.path.basename(root.rstrip("/")) + "__" + jobs)[:110] + "__" + hashlib.sha1(root.encode()).hexdigest()[:6]


def regs(qdir):
    """一台主机登记目录（Paths.queues）下的登记：[(文件, 登记内容（带 _slug）或读坏了时的异常)]，按登记文件名（qslug）的顺序，不是登记先后。
    登记文件只在这里读：reg_files、registry（Worker.registry、worker add 查状态目录、fit 找清单行）都经它。"""
    out = []
    for f in sorted(glob.glob(os.path.join(qdir, "*.json"))):
        try:
            with open(f) as fh:
                reg = json.load(fh)
            reg["_slug"] = os.path.basename(f)[:-5]
            out.append((f, reg))
        except (OSError, ValueError, TypeError, AttributeError) as e:
            out.append((f, e))
    return out


def registry(qdir):
    """登记的队列（load_queue 载入，带登记文件 file）与坏了的登记 [(文件, 异常)]（不是 JSON、缺 root / jobs / project）。"""
    qs, bad = [], []
    for f, reg in regs(qdir):
        try:
            if isinstance(reg, Exception):
                raise reg
            qs.append(dict(load_queue(reg), file=f))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            bad.append((f, e))
    return qs, bad


def matching_regs(qdir, root, jobs, *, aliases=False):
    """一台主机的登记目录 qdir 里登记过这条队列（同一目录、同一清单）的文件：按内容认，11-06 前不带哈希的旧文件名也认得；缺 project 的旧登记也认（rm 删得掉）。"""
    def same(reg):
        r, j = reg.get("root"), reg.get("jobs")
        if os.path.normpath(r or "") == os.path.normpath(root) and j == jobs:
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
        return int(time.mktime(time.strptime(reg.get("added") or "", "%Y-%m-%d %H:%M:%S")))
    except (ValueError, TypeError):
        return None


def reg_dirs():
    """各主机的登记目录 {主机: Paths.queues}（共享盘上同一个 HOME，含已归还的、主机清单里已没有的）：worker add 搬登记、查共用状态目录用（#95）。"""
    return {os.path.basename(os.path.dirname(d)): d for d in sorted(glob.glob(os.path.join(HOME, "hosts", "*", "queues")))}


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
    hosts = os.path.join(HOME, "hosts")
    os.makedirs(hosts, exist_ok=True)
    return flock(os.path.join(hosts, "registry"), required=required)


def env_path(root, jobs):
    """一条清单的 .env 路径（清单名 .txt 换成 .env；load_queue 与 offers 比两条清单的环境共用。看板探针 probe.py 不 import 本文件，另有一份）。"""
    return os.path.join(root, re.sub(r"\.txt$", ".env", jobs))


def load_queue(reg):
    """登记文件 → 队列：清单路径、.env、状态与日志目录（解析方式与看板探针相同）。"""
    root, jobs = reg["root"], reg["jobs"]
    envf = env_path(root, jobs)
    env_txt = read(envf, "") or ""

    def env(key, default):
        m = re.search(r"^%s=(\S+)" % key, env_txt, re.M)
        return os.path.join(root, (m.group(1) if m else default).strip("\"'"))

    return {"key": reg["_slug"], "root": root, "jobs": jobs, "jobs_path": os.path.join(root, jobs),
            "env": envf if os.path.exists(envf) else None, "gpus": reg.get("gpus"),
            # 项目 id 就是登记时写的 --project（add 必写）
            "project": reg["project"], "stop": reg.get("stop") or "boundary",
            "added_t": reg_added(reg),            # 登记时刻（epoch 秒）：状态里带出去，看板在同一清单登记在两台在用主机上时取晚的（collect.reg_queues）
            "state": env("QUEUE_STATE", "results/_queue"), "logs": env("QUEUE_LOGS", "results/_logs")}


def hints(cmd):
    """清单行尾（或单元申请）的资源提示：gpus= vram=（每张卡 GiB）ram=（GiB）tag= exclusive= est_min=，
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
    return {"ngpu": int(float(g.group(1))) if g else 1, "vram": float(v.group(1)) if v else 0.0, "ram": float(r.group(1)) if r else 0.0,
            "tag": t.group(1) if t else None, "excl": x.group(1) if x else None, "est_min": float(e.group(1)) if e else None,
            "cpus": float(c.group(1)) if c else None, "io": io.group(1) if io else None,
            "needs": [p for v in re.findall(r"\bneeds=(\S+)", tail) for p in v.split(",") if p],
            "deps": [a for v in re.findall(r"(?<![\w-])after=(\S+)", tail) for a in v.split(",") if a],
            "deps_any": [a for v in re.findall(r"(?<![\w-])afterany=(\S+)", tail) for a in v.split(",") if a],
            "gang": gg.group(1) if gg else None, "gn": int(gg.group(2)) if gg and gg.group(2) else None}


def rows_left(state, rows, cur):
    """清单里还会跑的行（list_state 写 todo、forecast 推演共用）：返回 (状态目录里的标记，读不了为 None,
    [(任务, 资源提示)] 去掉标了 .done、.failed 的与在跑的 cur)。还没跑过的清单没有状态目录：标记为 None，行都算还会跑。"""
    try:
        ms = set(os.listdir(state))
    except OSError:
        ms = None
    m = ms or ()
    return ms, [(n, h) for n, h in rows if n != cur and n + ".done" not in m and n + ".failed" not in m]


def first_rows(rows):
    """[(任务, X)] → {任务: X}：同名的取第一条（worker 跑的是第一条：next_job 取第一个没标完的，它跑完后同名的后一条因名字已标完永远不跑；
    job_cmd 同理）。按名字取清单行的资源提示都经它（proj_lanes、queue_durs）。"""
    out = {}
    for n, x in rows:
        out.setdefault(n, x)
    return out


def list_jobs(q):
    """清单里的任务 [(名称, 命令)]：跳过空行、# 注释与没有 | 的行。"""
    return parse_jobs(read(q["jobs_path"], "") or "")


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
    if is_hold(name):                             # 占位任务只等条件、不算：不独占、不要卡（漏写 gpus=0 时也不占卡、不为它停保活），也不成组
        h["excl"], h["ngpu"], h["gang"], h["gn"] = None, 0, None, None
    return dict(h, name=name, cmd=cmd.strip())


def next_job(q):
    """清单里第一个没有 .done / .failed 的任务。"""
    for name, cmd in list_jobs(q):
        if os.path.exists(os.path.join(q["state"], name + ".done")) or os.path.exists(os.path.join(q["state"], name + ".failed")):
            continue
        return job_of(name, cmd)
    return None


def new_req(kind, key, project, name, **kw):
    """一个等待放行的请求（清单队首 kind=queue，或一次单元申请 kind=slot）：tick、slot_reqs 与测试都从这里拼，字段只在这一处列全。
    资源提示（ngpu、vram、ram、tag、excl、est_min、cpus、io、needs、deps、deps_any、gang、gn）的缺省同 hints；清单任务另带 q、root、queue，单元另带 id、pid、t（申请时刻）。
    est、held、stall、since、dur、charge 按主机现状算，由 Worker.prep 补。单元申请不成组（slot_reqs 不带 gang）。"""
    r = {"key": key, "kind": kind, "project": project, "name": name, "ngpu": 1, "vram": 0.0, "ram": 0.0, "tag": None, "excl": None,
         "est_min": None, "cpus": None, "io": None, "needs": [], "deps": [], "deps_any": [], "gang": None, "gn": None, "cmd": "", "q": None, "root": None, "queue": None}
    r.update(kw)
    return r


def queue_req(q, job):
    """清单队列 q 里的任务 job（job_of）→ 请求：真放行（Worker.requests）与 fit 共用。"""
    return new_req("queue", q["key"], q["project"], q=q, root=q["root"], queue=q["jobs"], **job)


def read_current(state):
    """状态目录的 current（`名称 PID 开始日期 开始时刻 [放行的主机]`；看板探针 probe.py 另读，放行的主机它也认，#95）：(名称, PID, 开始的 epoch 秒, 放行的主机)；没有为 None。
    放行的主机 11-10 起 worker 写（#88），旧 gpu_queue.sh 与之前的 worker 不写，为 None。PID 写坏了为 0；没写时刻的（很旧的文件）算现在。
    接管（adopt）、别的主机在不在管它（run_elsewhere）与旁观旧 gpu_queue.sh 的任务（ext_jobs）共用。"""
    cur = read(os.path.join(state, "current"))
    if not cur:
        return None
    p = cur.split()
    try:
        t0 = time.mktime(time.strptime(" ".join(p[2:4]), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        t0 = time.time()
    return p[0], int(p[1]) if len(p) > 1 and p[1].isdigit() else 0, t0, p[4] if len(p) > 4 else None


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
    for f in sorted(glob.glob(os.path.join(HOME, "hosts", "*", "status.json"))):
        h = os.path.basename(os.path.dirname(f))
        if h == host or h in gone:
            continue
        try:
            st = json.loads(read(f, "{}") or "{}")
            if any(j.get("kind") == "queue" and j.get("name") == name and j.get("queue") == q["jobs"]
                   and os.path.normpath(j.get("root") or "") == os.path.normpath(q["root"]) for j in st.get("running") or []):
                return h
        except (ValueError, TypeError, AttributeError):
            continue
    return None


GUARD_RE = re.compile(r"(?:\[\[?|\btest)\s+-[efs]\s+['\"]?([^\s'\"\]]+)")
LOCAL_EVERY = 300        # 多久扫一次待跑任务声明的本地依赖
# 容器根盘开机以来的新写入（续租、关机、换人会清空，#149）：LocalDisk 按共享盘扫描同一套调度扫容器自己的根盘，只进开机后动过的目录（见 scan_unit 的 prune）
LOCAL_IOPS = 1000        # 本地盘每秒最多这么多次列目录、stat（NVMe 上 ionice 多半不起作用，同样自己数）
LOCAL_DRIFT = 64 * 2 ** 20   # labmon 之外的新写入（根盘已用 − labmon 自己的）比上次对账变了这么多，最近一天有写入的单位提前重扫（看板扫组内服务器用同一个数：fleet.WROTE_DRIFT）
LOCAL_SKIP = ("proc", "sys", "dev", "run")   # 根盘下不扫的（各自挂载的伪文件系统、运行时文件）；别的挂载点（共享盘、/dev/shm）按设备号不进
LOCAL_TOP = 8            # 写进状态的新写入目录取前这么多个，其余合成一项
# 共享盘（组里各服务器挂的同一块 NFS）的用量：拿着扫描租约的一台 worker 扫全盘（见 Disk）。
# NFS 上 ionice 不管用，只能自己数元数据操作限速；总量与剩余直接读 statvfs，明细按冷热轮流扫。
SCAN_IOPS = 200          # 每秒最多这么多次列目录、stat
SCAN_HOT_S = 3600        # 上次扫出变化的单位隔这么久可再扫
SCAN_MAX_S = 7 * 86400   # 一直没变的单位间隔翻倍，最长这么久
SCAN_COST = 50           # 同一单位两次至少隔上次耗时的这么多倍
SCAN_SPLIT_S = 1200      # 一个单位扫超过这么久就停下，拆成它的各子目录分别扫
SCAN_LIST_S = 600        # 多久列一次一、二级目录：看新建、删除与目录自身的修改时间
SCAN_DRIFT = 10 * 2 ** 30   # df 已用比上次对账变了这么多，就把最近一天有修改的单位提前扫
SCAN_LEASE_S = 900       # 别的主机这么久没续扫描租约，本机接着扫
SCAN_SAVE_S = 10         # 一件接一件做的时候索引最多这么久写一次（整份重写；手上没活了立刻写）
SCAN_AGES = (7, 30, 90, 365)   # 按修改时间分龄的界（天）


def scratch_dir(project, name):
    """单格临时目录 $LABMON_SCRATCH：只给这一格用，跑成功后删。"""
    return os.path.join(LOCAL, "jobs", NAME_RE.sub("_", project or "_"), NAME_RE.sub("_", name))


def cache_dir(project):
    """项目的本地缓存目录 $LABMON_CACHE：跨格用、能重算的中间文件；续租、容器重启会清空。"""
    return os.path.join(LOCAL, "cache", NAME_RE.sub("_", project or "_"))


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
        for line in (read(q["jobs_path"], "") or "").splitlines():
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


def project_dir(root):
    """队列所属项目在共享盘上的目录：projects/<项目>/ 或第一级目录。"""
    rel = os.path.relpath(root, DATA_ROOT) if root.startswith(DATA_ROOT.rstrip("/") + "/") else None
    parts = [x for x in (rel or "").split("/") if x]
    if not parts:
        return None
    return os.path.join(DATA_ROOT, *parts[:2]) if parts[0] == "projects" and len(parts) > 1 else os.path.join(DATA_ROOT, parts[0])


def project_of(root):
    """队列目录属于哪个项目：共享盘根下 projects/<项目>/ 或第一级目录名（与工作台的归属一致）。"""
    rel = os.path.relpath(root, DATA_ROOT) if root.startswith(DATA_ROOT.rstrip("/") + "/") else root
    parts = [x for x in rel.split("/") if x]
    return parts[1] if parts[:1] == ["projects"] and len(parts) > 1 else (parts[0] if parts else root)


def idle_job(j):
    """不用卡、此刻几乎不占 CPU 的任务（如登记成清单任务、等单元放行的监督进程）；刚起来还没量到核数的不算。"""
    c = (j.get("use") or {}).get("cores")
    return j.get("ngpu", 1) == 0 and not j.get("gpus") and c is not None and c < IDLE_CORES


def gp_label(g):
    """GPU 进程在原因与记录里的叫法：任务名、GPU 保活，或 PID（命令名）。g 是 sample 认过归属的一条。"""
    return g["job"] or ("GPU 保活" if g["owner"] == "keepalive" else "对方的保活 PID %d（ka.sh 停不了）" % g["hpid"] if g["ka"] else
                        "PID %d（%s）" % (g["hpid"], g["comm"] or "labmon 之外"))


def intruders(scope, cards, jobs, gp, cpu, me=None):
    """独占任务会被谁打扰（scope 为 gpu 看 cards 这几张卡，host 看整台主机），分两类返回：
    labmon 的（别的任务、GPU 保活：会跑完或由 worker 停下，等腾空时值得留卡）与 labmon 之外的（借来的主机上多半是对方，留卡也不会走）。
    放行前（等腾空）与运行中（作废）共用：同卡（整机：同机）别的任务与 GPU 进程，整机另算 labmon 之外的进程占 VOID_CPU 核以上（cpu）。
    gp 是 sample 认过归属的 GPU 进程，me 是这个任务自己。"""
    on = set(cards)
    ours = {o["name"] for o in jobs if o is not me and not is_hold(o["name"]) and not idle_job(o)
            and (scope == "host" or on & set(o["gpus"]))}
    theirs = set()
    for g in gp:
        if (me is None or g["_j"] is not me) and (scope == "host" or g["uuid"] in on):
            (theirs if g["owner"] == "other" else ours).add(gp_label(g))
    if scope == "host" and cpu is not None and cpu > VOID_CPU:
        theirs.add("labmon 之外的进程占 %.1f 核" % cpu)
    return sorted(ours), sorted(theirs)


def base_name(name):
    """同类任务：去掉末尾的种子、重复编号（x4_s146_seed0 → x4_s146）。"""
    return re.sub(r"([_-](s|seed|rep)\d+)+$", "", name)


def pstart(pid):
    """进程启动时刻（时钟滴答）：认 PID 有没有被复用；没有 /proc 时为 None。"""
    try:
        with open(os.path.join(PROC, str(pid), "stat")) as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def started_at(pid):
    """进程启动时刻（epoch 秒）= 开机时刻（/proc/stat 的 btime）+ pstart / 每秒滴答数；读不到为 None。
    容器启动时刻（host_info 取 PID 1）、保活进程什么时候起来的（ka_groups，认 ka.sh 日志里这台的 RESTORED）共用。"""
    t = pstart(pid)
    if t is None:
        return None
    try:
        btime = int(next(ln.split()[1] for ln in (read(os.path.join(PROC, "stat"), "") or "").splitlines() if ln.startswith("btime")))
    except (StopIteration, IndexError, ValueError):
        return None
    return btime + t / os.sysconf("SC_CLK_TCK")


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
        with open(os.path.join(PROC, str(pid), "cmdline"), "rb") as f:
            args = [x.decode(errors="replace") for x in f.read().split(b"\0")]
    except OSError:
        return [], ""
    while args and not args[-1]:
        args.pop()
    try:
        cwd = os.readlink(os.path.join(PROC, str(pid), "cwd"))
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
    text = head(what) or (head(os.path.join(PROC, str(pid), "fd", "255")) if pid and not py else "")
    return what, line + "\n" + text + "\n" + refs(text, os.path.dirname(what), cwd)


def ka_owned(args, cwd, ka_full):
    """ka.sh 认得、停得了的保活（与共享盘上 ka.sh 的 pgids() 同一条规则，#29）：python 跑的，命令行里写着 ka_full（AGRI_KA_DIR/AGRI_KA_SCRIPT）这个完整路径，
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
    return "labmon-guard" in (proc_env(pid, "ST") or "") or (proc_env(pid, "KA_CALLER") or "").startswith("labmon")


def wd_text(ws):
    """看门狗列成一句：PID 123（bash keepalive_final_check.sh：拉起了保活）、…"""
    return "、".join("PID %d（%s：%s）" % (w["pid"], w["cmd"] if len(w["cmd"]) <= 80 else w["cmd"][:77] + "…", w["why"]) for w in ws)


def ka_lines(st):
    """status.json 里这台的 GPU 保活说成几句（#61；`labmon worker status` 与看板的 `labmon status` 共用）：在跑的（几点起、
    nvidia-smi 里的 PID 与卡、占多少显存）、停着的（worker 为要卡的任务停的）、没在跑又不拉起的原因、借来的主机上 ka.sh 停不了的、
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
        out.append("GPU 保活：在跑%s：%s%s" % ("（%s 起）" % now_str(t0)[5:16] if t0 else "", procs(mine),
                                         "；有任务要卡时 worker 自动停" if ka.get("ctl") else "；这台没配 ka.sh，worker 不停它" if ka.get("ctl") is False else ""))
    elif st.get("ka_stop") is not None:
        out.append("GPU 保活：停着（%s有任务要卡；卡空下来后 worker 再拉起）" % ("worker %s 停的，" % now_str(st["ka_stop"])[5:16] if st["ka_stop"] else ""))
    elif ka.get("why"):
        out.append("GPU 保活：没在跑，没有任务要卡，但%s，不拉起" % ka["why"])
    elif ka.get("ctl"):
        out.append("GPU 保活：没在跑（卡连着空两轮后 worker 拉起）")
    ext = [g for g in st.get("gpu_procs") or [] if g.get("ka")]
    if ext:
        out.append("对方的保活（ka.sh 停不了，这几张卡不放）：" + procs(ext))
    if st.get("kafight"):
        out.append("GPU 保活 %d 小时内被拉起 %d 次（最近 %s）：%s" % (FIGHT_S // 3600, st["kafight"]["n"], now_str(st["kafight"]["t"])[5:16], st["kafight"]["who"]))
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
        w = {"pid": pid, "start": pstart(pid), "cmd": " ".join(args)[:200], "cwd": cwd}
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
                        maybe.append({"pid": cron[0], "start": pstart(cron[0]), "cmd": ln.strip()[:200], "cwd": f,
                                      "why": "cron 定时任务（%s）提到保活程序（暂停不了）" % f})
    return sure, maybe


def held_locks(pid):
    """进程此刻持着的文件锁（读 PROC/locks，#38）：{"主:次:inode": 文件路径（读不到为 None）}。FLOCK、POSIX 锁按持有者 PID 认；
    OFD 锁不属于单个进程（PID 写 -1），按它开着的文件（PROC/<pid>/fd）认；在等的（"->"）与租约不算。读不到 PROC/locks 为 None。
    PID 是读这个 /proc 的 PID 命名空间里的（Linux 4.9 起按命名空间过滤），worker 与看门狗在同一个容器里，对得上。"""
    try:
        with open(os.path.join(PROC, "locks")) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    fds, d = {}, os.path.join(PROC, str(pid), "fd")
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
        d = json.loads(read(p.frozen, "{}") or "{}")
    except ValueError:
        d = {}
    return {"frozen": d.get("frozen") or [], "seen": d.get("seen") or []}


def freeze(p, ws=(), seen=None):
    """暂停看门狗（SIGSTOP）并记进 frozen.json，还卡时 thaw 恢复。保活真被拉起来时才调（ka_restarted）；
    已暂停过的再发一次（被人恢复后又拉起了保活），不重复记。返回这次停住的。seen 给了就换掉找到了、没动的那份（已暂停的不再列）。"""
    with flock(p.frozen):
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
                d["frozen"].append(dict(w, t=round(time.time())))
                old.add((w["pid"], w["start"]))
        if seen is not None:
            d["seen"] = [w for w in seen if (w["pid"], w["start"]) not in old]
        write(p.frozen, json.dumps(d, ensure_ascii=False))
    return done


def thaw(p):
    """恢复暂停的看门狗（SIGCONT；PID 已被复用的不碰），清空 frozen.json。返回一句话，没暂停过的为空串。"""
    with flock(p.frozen):
        back, gone = [], []
        for w in load_frozen(p)["frozen"]:
            ok = w["start"] is not None and pstart(w["pid"]) == w["start"]
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


class Pace:
    """限速：数元数据操作，快过每秒 iops 次就睡；这一件超过 limit 秒抛 TimeoutError（拆开再扫，按这一件自己的用时与次数）。
    rate：同一个扫描者接连几件共用的计数（Disk.rate，#153）：一件接一件做时合起来也守每秒 iops 次（每件各算各的话，不满 20 次的小件从不睡）；
    开始一件时实际已慢于 iops（闲过）就从头算，闲着的时间不攒成一阵猛扫。"""

    def __init__(self, iops, limit=None, rate=None):
        self.iops, self.limit, self.k, self.t = iops, limit, 0, time.time()
        r = self.r = {} if rate is None else rate
        if not r or r["t0"] + r["n"] / iops < self.t:
            r.update(t0=self.t, n=0)

    def __call__(self):
        r = self.r
        r["n"] += 1
        self.k += 1
        if self.limit and self.k % 20 == 0 and time.time() - self.t > self.limit:
            raise TimeoutError
        if r["n"] % 20 == 0:
            ahead = r["n"] / self.iops - (time.time() - r["t0"])
            if ahead > 0:
                time.sleep(ahead)


def dot(d):
    """目录 d 本层的文件这个扫描单位的键：<目录>/.（根目录是 /.）。"""
    return d.rstrip("/") + "/."


def scan_units(root, split, pace, prune=None, skip=()):
    """扫描单位 {路径: 目录自身的修改时刻}：一级目录下的各子目录（拆过的再往下一层），以及根、各一级目录（与拆过的目录）本层的文件（键为 <目录>/.）。
    prune（容器根盘，LocalDisk 给开机时刻）：ctime 早于它的目录整棵不要（overlay 上开机以后动过的目录都已复制到上层、ctime 晚于开机，
    早于开机的只在镜像层里，下面没有新写入）；不跨文件系统（共享盘、/proc 等挂载点不进），skip 里的也不进。"""
    out, base = {}, root.rstrip("/") or "/"
    dev = os.stat(base).st_dev if prune is not None else None

    def walk(d, depth):
        pace()
        try:
            es = list(os.scandir(d))
            out[dot(d)] = os.stat(d).st_mtime
        except OSError:
            return
        for e in es:
            if not e.is_dir(follow_symlinks=False):
                continue
            pace()
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if prune is not None and (e.path in skip or st.st_dev != dev or st.st_ctime < prune):
                continue
            if depth == 0 or e.path in split:
                walk(e.path, depth + 1)
            else:
                out[e.path] = st.st_mtime
    walk(base, 0)
    return out


def scan_unit(path, now, pace, prune=None, since=None):
    """扫一个单位：占用（按块，单位内硬链接只算一次，与 du 一致）、文件数、最新修改、按修改时间分龄的占用、
    直接子项里最大的 10 个 [名字, 占用, 最新修改]。<目录>/. 只看本层的文件。
    prune、since（容器根盘，LocalDisk 给）：ctime 早于 prune 的目录不进（同 scan_units）、不跨文件系统；只数 ctime 不早于 since 的文件
    （开机以来的新写入；借来的主机上 since 是借来的时刻，prune 仍是开机时刻：只按借来时刻剪会漏掉旧目录里新建的文件），
    目录本身的块不算，“最新修改”取 ctime（写进这个容器的时刻）。"""
    flat = path.endswith("/.")
    b = n = 0
    m, age, top, tm, seen = 0.0, [0] * (len(SCAN_AGES) + 1), {}, {}, set()
    first = (path[:-2] or "/") if flat else path
    dev = os.stat(first).st_dev if prune is not None else None
    stack = [(first, None)]
    while stack:
        d, head = stack.pop()
        pace()
        try:
            es = list(os.scandir(d))
        except OSError:
            continue
        for e in es:
            pace()
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            isdir = stat.S_ISDIR(st.st_mode)
            if isdir and (flat or prune is not None and (st.st_dev != dev or st.st_ctime < prune)):
                continue
            if not isdir and st.st_nlink > 1:
                if (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
            h = head or e.name
            if isdir:
                if since is None:
                    b += st.st_blocks * 512
                    top[h] = top.get(h, 0) + st.st_blocks * 512
                stack.append((e.path, h))
                continue
            if since is not None and st.st_ctime < since:
                continue
            sz, t = st.st_blocks * 512, st.st_mtime if since is None else st.st_ctime
            b += sz
            n += 1
            m = max(m, t)
            if not flat:
                top[h] = top.get(h, 0) + sz
                tm[h] = max(tm.get(h, 0), t)
            age[sum(1 for x in SCAN_AGES if now - st.st_mtime > x * 86400)] += sz
    return {"b": b, "n": n, "m": round(m) or None, "age": age,
            "top": sorted(([k, v, round(tm.get(k, 0)) or None] for k, v in top.items()), key=lambda kv: -kv[1])[:10]}


class Disk:
    """共享盘用量。各主机挂的是同一块盘，只有拿着租约（<HOME>/disk/lease）的那台扫，别的主机只续看。
    扫描在后台线程里一次做一件（列单位或扫一个单位），调度在主线程：每拍的 scan_local，拍与拍之间做完一件就接着下一件（Worker.scan_chain，#153）。
    没扫过的先扫；上次有变化的隔 SCAN_HOT_S，没变的间隔翻倍到 SCAN_MAX_S；目录自身修改时刻比上次扫新、有任务跑完（hint）、df 已用变了很多的提前。
    共享盘慢时不开始新的一件（step 的 defer）。限速按扫描者算（rate：接连几件合起来守 iops）；耗时由扫描线程自己量。
    索引 <HOME>/disk/index.json：各单位的占用、文件数、最新修改、分龄、最大子项、扫完的时刻与耗时，以及上一次的占用（看板算增速）；
    接连做的时候最多 SCAN_SAVE_S 写一次。
    容器根盘的新写入（LocalDisk）用同一套调度，只换根、租约、索引的位置、限速与“变了很多”的算法。"""
    key = "scan"             # io_yield 推迟这件事用的键
    iops = SCAN_IOPS

    def __init__(self, root, host):
        self.root, self.host = root.rstrip("/"), host
        self.dir = os.path.join(HOME, "disk")
        self.ixf = os.path.join(self.dir, "index.json")
        self.ix, self.job, self.mine, self.lease_t, self.wait = None, None, False, 0.0, None   # wait：共享盘慢、推迟中的说明
        self.rate, self.saved, self.dirty = {}, 0.0, False   # 接连几件共用的限速计数（Pace）；索引上次写盘的时刻、有没有没写的
        self.roots, self.lsplit = set(), set()   # 登记了清单的项目根（want）；这次列目录用的拆开的目录（列完并进索引的 split）

    def lease(self, now):
        """每分钟看一次租约：别的主机 SCAN_LEASE_S 内续过就不扫；否则（或本来就是本机）续上。"""
        if now - self.lease_t < 60:
            return self.mine
        self.lease_t = now
        f = os.path.join(self.dir, "lease")
        who = (read(f, "") or "").split()
        mine = not who or who[0] == self.host or len(who) < 2 or now - float(who[1]) > SCAN_LEASE_S
        if mine:
            os.makedirs(self.dir, exist_ok=True)
            write(f, "%s %d" % (self.host, now))
            if not self.mine:                   # 刚接手：读上一个扫描者留下的索引
                self.load()
        self.mine = mine
        return mine

    def load(self):
        try:
            self.ix = json.loads(read(self.ixf, "") or "{}")
        except ValueError:
            self.ix = {}

    def want(self, roots):
        """登记了清单的项目根（各台状态的 storage.projects，Worker.scan_local 合起来给）：下一次列目录时把它们连同在二级以下的各级父目录
        一起拆开（与扫得太慢拆开的同一个 split），项目根下的一级目录各成一个单位、按项目分开统计（#53）。只记下来，列目录时才用。"""
        self.roots = set(roots)

    def forced(self):
        """want 的项目根要拆开的目录：根下第二级起到项目根本身（第一级本来就进）。不在这块盘上的不管。"""
        out = set()
        for r in self.roots:
            rel = os.path.relpath(os.path.normpath(r), self.root)
            parts = [x for x in rel.split("/") if x and x != "."]
            if parts and parts[0] != "..":
                out.update(os.path.join(self.root, *parts[:k]) for k in range(2, len(parts) + 1))
        return out

    def hint(self, d):
        """有任务在 d 下跑完：d 里的单位尽快重扫（仍守 SCAN_COST）。"""
        for u, r in ((self.ix or {}).get("units") or {}).items():
            if u == d or u.startswith(d.rstrip("/") + "/"):
                r["due"] = min(r.get("due", 0), r.get("t", 0) + SCAN_COST * r.get("s", 0))

    def lister(self, split, pace):
        return scan_units(self.root, split, pace)

    def scanner(self, u, now, pace):
        return scan_unit(u, now, pace)

    def step(self, now, defer=lambda key, now: None):
        """调度一步：拿着租约、上一件做完了，就收下它的结果、开始下一件（列目录或扫一个单位）。defer（Worker.io_yield）给出推迟的说明时
        这一步先不开始（共享盘慢时让路，推迟有上限，见 IO_YIELD_MAX_S），说明记在 yield（写进状态的 disk）。
        索引有没写的：手上没活了立刻写，接连做的时候隔满 SCAN_SAVE_S 才写。"""
        if not self.lease(now):
            return
        ix = self.ix
        ix.setdefault("units", {})
        ix.setdefault("split", [])
        if self.job and not self.job[0].is_alive():
            self.done()
        if not self.job:
            self.pick(now, defer)
        if self.dirty and (not self.job or now - self.saved >= SCAN_SAVE_S):
            self.save(now)

    def pick(self, now, defer):
        """挑下一件：先列目录（到点了），再扫到期的单位里最早到期的那个。"""
        ix = self.ix
        due = [(r.get("due", 0), u) for u, r in ix["units"].items() if r.get("due", 0) <= now]
        split = set(ix["split"]) | self.forced()
        # 到点了列目录；有新的项目根要拆开时提前列（列失败的隔一分钟再试）
        listing = now - ix.get("listed", 0) >= (60 if split - set(ix["split"]) else SCAN_LIST_S)
        self.wait = (listing or due) and defer(self.key, now) or None
        if self.wait:
            return
        if listing:
            self.lsplit = split
            self.start(now, "list", lambda pace: self.lister(split, pace), None)
            return
        if due:
            u = min(due)[1]
            self.start(now, u, lambda pace: self.scanner(u, now, pace), None if u.endswith("/.") else SCAN_SPLIT_S)

    def start(self, now, what, fn, limit):
        """在后台线程里做一件；扫描本身用了多久由线程自己量（out 的 s），不按主线程哪一拍收的结果算（#153）。"""
        out = {}

        def run():
            t = time.monotonic()
            try:
                out["r"] = fn(Pace(self.iops, limit, self.rate))
            except TimeoutError:
                out["split"] = True
            except Exception as e:  # noqa: BLE001
                out["err"] = repr(e)
            finally:
                out["s"] = time.monotonic() - t
        th = threading.Thread(target=run, daemon=True)
        self.job = (th, what, now, out)
        th.start()

    slack = SCAN_DRIFT       # 对账的量（acct_of）变了这么多才提前扫

    def acct_of(self):
        """对账看的量：共享盘是 df 已用。"""
        return (self.ix.get("df") or {}).get("used")

    def drift(self, now):
        """列完单位后对账：对账的量比上次对账变了 slack 以上（大文件追加、原地写入：目录修改时刻看不出），把最近一天有修改的单位提前扫。"""
        ix = self.ix
        try:
            s = os.statvfs(self.root)
            ix["df"] = {"total": s.f_blocks * s.f_frsize, "free": s.f_bavail * s.f_frsize,
                        "used": (s.f_blocks - s.f_bfree) * s.f_frsize, "t": round(now)}
        except OSError:
            pass
        used = self.acct_of()
        if used is not None and abs(used - ix.get("acct", used)) > self.slack:
            for r in ix["units"].values():
                if r.get("m") and now - r["m"] < 86400:
                    r["due"] = min(r.get("due", 0), r["t"] + SCAN_COST * r.get("s", 0))
            ix["acct"] = used
        ix.setdefault("acct", used)

    def done(self):
        """收下做完的那件：时刻都从它扫完的时刻（开始时刻 + 线程量的耗时）算。"""
        _, what, t0, out = self.job
        self.job = None
        s = out.get("s", 0.0)
        now = t0 + s
        ix, units = self.ix, self.ix["units"]
        if what == "list":
            ix["listed"] = round(now)
            got = out.get("r") or {}
            if got:
                ix["split"] = sorted(set(ix["split"]) | self.lsplit)   # 新拆开的与列出的子单位同一次写进索引：看板按它认“新拆开的”（disk.absorb）
                for u in list(units):
                    if u not in got:
                        units.pop(u)
                for u, dm in got.items():
                    r = units.setdefault(u, {"due": 0})
                    if r.get("t") and dm > r["t"]:     # 目录里增删过东西
                        r["due"] = min(r.get("due", 0), r["t"] + SCAN_COST * r.get("s", 0))
            self.drift(now)
        elif out.get("split"):
            ix["split"] = sorted(set(ix["split"]) | {what})
            units.pop(what, None)
            ix["listed"] = 0                          # 下一轮就列出它的子目录
        else:
            r = units.get(what, {})
            if "r" in out:
                new = out["r"]
                changed = r.get("b") is None or abs(new["b"] - r["b"]) > 2 ** 20 or new["n"] != r.get("n")
                gap = SCAN_HOT_S if changed else min(SCAN_MAX_S, 2 * r.get("gap", SCAN_HOT_S))
                units[what] = dict(new, t=round(now), s=round(s, 1), pb=r.get("b"), pt=r.get("t"), gap=gap,
                                   due=round(now + max(gap, SCAN_COST * s)))
            else:                                     # 出错：一天后再试
                units[what] = dict(r, err=out.get("err"), due=round(now + 86400))
        self.dirty = True

    def save(self, now):
        ix = self.ix
        ix.update(root=self.root, host=self.host, t=round(now))
        os.makedirs(os.path.dirname(self.ixf), exist_ok=True)
        write(self.ixf, json.dumps(ix, ensure_ascii=False, separators=(",", ":")))
        self.saved, self.dirty = now, False

    def brief(self):
        """写进 status.json 的一行：是不是本机在扫、正在扫什么、共享盘慢推迟中的说明（yield）。"""
        return {"root": self.root, "mine": self.mine, "now": self.job[1] if self.job else None, "since": round(self.job[2]) if self.job else None,
                "yield": self.wait}


class LocalDisk(Disk):
    """容器根盘开机以来的新写入（续租、关机、换人会清空，#149），与共享盘的 Disk 同一套调度：一次做一件、Pace 限速、变了的一小时后再扫、
    没变的间隔翻倍、太慢的拆开、任务跑完提前（hint）、共享盘慢时也让路（键 lscan）。不同的只有：每台扫自己的、不要租约；索引在容器本地
    LOCAL/disk.json（续租即清）；只进开机以后动过的目录（scan_units、scan_unit 的 prune：overlay 上就是开机以来写过东西的那些）；
    “变了很多”看 labmon 之外的新写入（根盘已用 − labmon 目录的占用）比上次对账变了 LOCAL_DRIFT（共享盘是 df 已用变了 SCAN_DRIFT），
    变了同样把最近一天有写入的单位提前重扫。
    labmon 自己的目录（LOCAL）拆到 cache/<项目>、jobs/<项目> 一级：同一次扫描给出各项目缓存与单格临时（summary）。
    根盘不是 overlay、或不知道开机时刻（info.rootfs 测不准）时只扫 LOCAL；借来的主机上 since 是借来的时刻（只数借用期间的新写入，给归还确认）。"""
    key = "lscan"
    iops = LOCAL_IOPS
    slack = LOCAL_DRIFT

    def __init__(self, host):
        Disk.__init__(self, ROOTFS, host)
        self.ixf = os.path.join(LOCAL, "disk.json")
        self.mine, self.cfg = True, None

    def setup(self, prune, since, borrow=False):
        """这一轮的扫法：prune 为 None（测不准）只扫 LOCAL；否则扫整个根盘、只数 since 以后的。扫法变了（换了容器、开始或结束借用）整份索引作废。
        borrow：借来的主机，since 是 guest 指令的时刻；借用中指令重下（预留变了）时刻会往后挪，这时沿用这次借用里最早的那个，不作废索引。"""
        if self.ix is None:
            self.load()
        old = (self.ix.get("cfg") or {}).get("since") or 0
        if borrow and prune is not None and since is not None and (self.ix.get("cfg") or {}).get("prune") == prune and prune < old < since:
            since = old
        cfg = {"root": (ROOTFS.rstrip("/") or "/") if prune is not None else LOCAL.rstrip("/"), "prune": prune, "since": since}
        if self.ix.get("cfg") != cfg:
            if self.job:                              # 换扫法时正在扫的那件作废（线程跑完自己结束，结果不要）
                self.job = None
            self.ix = {"cfg": cfg}
        self.cfg, self.root = cfg, cfg["root"]

    def lease(self, now):
        return self.cfg is not None

    def splits(self, split):
        """labmon 目录拆到各项目一级：LOCAL 与它在根下的各级上层、cache、jobs（不拆的话 /tmp/labmon 整个是一个单位）。"""
        L, top, out = LOCAL.rstrip("/"), self.root.rstrip("/"), set(split)
        for d in (L, os.path.join(L, "cache"), os.path.join(L, "jobs")):
            while d != self.root and d.startswith(top + "/"):
                out.add(d)
                d = os.path.dirname(d)
        return out

    def lister(self, split, pace):
        c = self.cfg
        skip = {os.path.join(c["root"], x) for x in LOCAL_SKIP} if c["prune"] is not None else ()
        return scan_units(c["root"], self.splits(split), pace, c["prune"], skip)

    def scanner(self, u, now, pace):
        return scan_unit(u, now, pace, self.cfg["prune"], self.cfg["since"])

    def own(self, u):
        """这个单位是 labmon 自己的（LOCAL 下）。"""
        L = LOCAL.rstrip("/")
        return u == L or u.startswith(L + "/")

    def acct_of(self):
        """对账看 labmon 之外的新写入：根盘已用 − labmon 目录的占用（只扫 labmon 目录时不对账）。"""
        used = (self.ix.get("df") or {}).get("used")
        if self.cfg["prune"] is None or used is None:
            return None
        return used - sum(r.get("b") or 0 for u, r in self.ix["units"].items() if self.own(u))

    def summary(self):
        """写进状态 storage 的：local 各项目的缓存与单格临时、own labmon 目录的总占用（字节）、wrote labmon 之外的新写入（只扫 LOCAL 时为 None）。
        wrote：since 起算的时刻、b 字节、n 文件数、top 前 LOCAL_TOP 个目录 [路径, 字节, 最近写入]（单位的直接子项；本层的文件写成 <目录>/.；
        子项不止 10 个的，其余合在单位自己的路径上）、more [其余几处, 字节]、pending 还没扫的单位数、t 最近一次扫完一个单位的时刻。"""
        ix, L = self.ix or {}, LOCAL.rstrip("/")
        units = ix.get("units") or {}
        local, own, rows, pend, b, n = {}, 0, [], 0, 0, 0
        for u, r in units.items():
            if r.get("b") is None:
                pend += 1
                continue
            if self.own(u):
                own += r["b"]
                rel = os.path.relpath(u, L).split("/")             # 扫得太慢拆开的（cache/<项目>/models 等更深的单位）按前两段归到所属项目
                if len(rel) >= 2 and rel[0] in ("cache", "jobs") and rel[1] != ".":
                    local.setdefault(rel[1], {"cache": 0, "scratch": 0})["cache" if rel[0] == "cache" else "scratch"] += r["b"]
                continue
            b, n = b + r["b"], n + r["n"]
            if u.endswith("/."):
                rows.append([u, r["b"], r.get("m")])
                continue
            tops = [x for x in r.get("top") or [] if x[1]]
            rows += [[u.rstrip("/") + "/" + x[0], x[1], x[2] if len(x) > 2 else None] for x in tops]
            rest = r["b"] - sum(x[1] for x in tops)
            if rest > 0:
                rows.append([u, rest, r.get("m")])
        rows = sorted((x for x in rows if x[1] > 0), key=lambda x: -x[1])
        cfg = ix.get("cfg") or {}
        wrote = {"since": cfg.get("since"), "b": b, "n": n, "top": rows[:LOCAL_TOP], "more": [len(rows[LOCAL_TOP:]), sum(x[1] for x in rows[LOCAL_TOP:])],
                 "pending": pend, "t": max((r.get("t") or 0 for r in units.values()), default=0) or None} if cfg.get("prune") is not None else None
        return {"t": ix.get("t"), "local": local, "own": own if units else None, "wrote": wrote}


def fsig(path):
    """文件变没变的签名 (inode, 修改时刻纳秒, 大小)，按文件缓存读出内容的都拿它比；读不到抛 OSError。只比 st_mtime 时同一个时间戳粒度里
    连写两次第二次就漏读（共享盘上别的主机刚续写的用量记录要等下次再写才补上，#31）。看板那边是 state.file_sig，口径相同。"""
    st = os.stat(path)
    return st.st_ino, st.st_mtime_ns, st.st_size


def load_jsonl(path):
    """JSONL 的全部行（用量记录、事件、主机压力历史共用；写坏的行跳过）。"""
    out = []
    for l in (read(path, "") or "").splitlines():
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def append_jsonl(path, row, keep_s=None):
    """JSONL 续写一行（用量记录、事件、主机压力历史共用；要互斥的调用方自己加 flock）。
    keep_s：按行里的 t 修剪，第一行早于保留期一天以上时整份重写一次（不是每次追加都重写）。"""
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if keep_s:
        now = time.time()
        try:
            with open(path) as f:
                first = json.loads(f.readline()).get("t") or 0
        except (OSError, ValueError, AttributeError):
            first = 0
        if first < now - keep_s - 86400:
            write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in load_jsonl(path) if (r.get("t") or 0) >= now - keep_s))


def load_hist(cache):
    """各主机的 usage.jsonl（共享盘上同一个 HOME）：资源与时长都看所有主机的同类记录，借来的主机一上来就有历史。
    cache 为 {文件: (fsig, 记录)}，文件没变不重读；有变化返回按结束时刻排好的全部记录（记上主机），没变返回 None。
    几秒内就结束、没量到用量的（ram_peak 为空）也留着：依赖要看到这次的结果（别的主机上起不来就失败的），资源估计（estimate）自己跳过它们。"""
    changed = False
    for f in glob.glob(os.path.join(HOME, "hosts", "*", "usage.jsonl")):
        try:
            m = fsig(f)
        except OSError:
            continue
        if cache.get(f, (None,))[0] != m:
            host = os.path.basename(os.path.dirname(f))
            cache[f] = (m, [dict(h, host=h.get("host") or host) for h in load_jsonl(f)])
            changed = True
    return sorted((h for _, rows in cache.values() for h in rows), key=lambda h: h.get("t1") or 0) if changed else None


def job_cmd(q, name, rows=None):
    """清单里叫 name 的那一行的命令（按名字找清单行只在这里：接管与旁观的任务、offers 查本地依赖、fit 找清单行）：取第一条、跳过注释行，
    没有为 None。rows 是已经读好的 list_jobs(q)（fit 一次调用里只读一遍），不给就现读。"""
    return next((c.strip() for n, c in (list_jobs(q) if rows is None else rows) if n == name), None)


# 任务日志里每次开跑的分隔行（launch 写「===== 时刻 labmon 开始（GPU …）=====」；11-07 前写的是「… 开始（可能是续跑；…）=====」，也认）。
# 探针 probe.py 不 import 本文件，另有一份同样的
START_RE = re.compile(r"===== .* 开始（")


OOM_SAY = {"vram": "CUDA 显存不够（OOM）", "ram": "容器内存撞上限被杀（OOM）"}   # 失败事件与队列日志句尾的说法（finish）


def oom_of(j, rc):
    """一次失败的清单任务是不是 OOM（#62）：finish 记进用量行的 oom（estimate 据此抬高同类的估计），squeezed_by 据此判借来的主机上是不是被对方挤掉。
    - "ram"：运行期间容器撞内存上限、内核杀过进程（oom_kill 计数涨了；3.10 内核的 cgroup v1 没有这个计数，退回到 memory.failcnt 涨了），
      且这个任务是被 SIGKILL 的（退出码 137；bash 一起被杀时没留退出码）；
    - "vram"：这一次的日志末尾 40 行（原文，不折叠）是 CUDA OOM；
    - 都不是为 None。单元申请没有日志、不知道退出码，不认。子进程被杀、主进程以退出码 1 退出的（DataLoader worker）认不出。"""
    if j.get("kind") == "slot":
        return None
    n, fc = oom_kills(), num(cg("memory/memory.failcnt"))
    killed = (n > j["oom0"] if n is not None and j.get("oom0") is not None else
              fc is not None and j.get("fc0") is not None and fc > j["fc0"])
    if killed and rc in ("137", None):
        return "ram"
    if CUDA_OOM.search("\n".join(log_tail(os.path.join(j["q"]["logs"], j["name"] + ".log"), 40, fold=False))):
        return "vram"
    return None


def log_tail(path, n=12, fold=True):
    """这一次运行的日志最后几行：读末尾 32 KB（截断的开头半行丢掉），进度条的回车也当换行。日志是追加写的，每次开跑先写一行分隔（START_RE），
    只取最后一行分隔之后的，上一次的输出（失败的回溯、OOM）不算进这一次。
    fold：这一次里逐字出现过的行算套话（库每次加载模型都打印的提示之类），连着的几行折成一行「…（N 行与前面重复，略）」；
    数字在变的行（进度、loss）每行都不一样，不受影响。OOM 判断要原文，传 fold=False。探针 probe.py 取最后一行与进度时同一口径。"""
    try:
        with open(path, "rb") as f:
            size = os.path.getsize(path)
            f.seek(max(0, size - 32768))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    ls = [l.rstrip() for l in re.split(r"[\r\n]+", text)[1 if size > 32768 else 0:] if l.strip()]
    k = max((i for i, l in enumerate(ls) if START_RE.match(l)), default=-1)
    ls = ls[k + 1:]
    if fold:
        seen, out = set(), []
        for l in ls:
            if l not in seen:
                seen.add(l)
                out.append(l)
            elif out and isinstance(out[-1], int):
                out[-1] += 1
            else:
                out.append(1)
        ls = ["…（%d 行与前面重复，略）" % x if isinstance(x, int) else x for x in out]
    return [l[:300] for l in ls[-n:]]


def smi(query, fields, *, strict=False):
    try:
        out = subprocess.run(["nvidia-smi", "--query-%s=%s" % (query, fields), "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8 if strict else 30, check=strict).stdout
    except (OSError, subprocess.TimeoutExpired):
        if strict:
            raise
        return []
    n = fields.count(",") + 1
    lines = out.strip().splitlines()
    if strict and any(l.count(",") != n - 1 for l in lines):
        raise ValueError("GPU 查询返回了无法识别的行")
    return [[c.strip() for c in l.split(",")] for l in lines if l.count(",") == n - 1]


def num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def gpu_table():
    """各卡：序号、UUID、型号、总/已用/空闲显存（MB）、利用率（%）。
    测试时用 LABMON_GPUS="uuid:free_mb[:total_mb[:util]],…" 代替 nvidia-smi。"""
    fake = os.environ.get("LABMON_GPUS")
    if fake is not None:
        out = []
        for i, x in enumerate(x for x in fake.split(",") if x):
            p = x.split(":") + [None, None]
            free, total = float(p[1]), num(p[2], float(p[1]))
            out.append({"i": i, "uuid": p[0], "name": "fake", "total": total, "used": total - free, "free": free,
                        "util": num(p[3], 0.0)})
        return out
    return [{"i": int(r[0]), "uuid": r[1], "name": r[2], "total": num(r[3], 0.0), "used": num(r[4], 0.0),
             "free": num(r[5], 0.0), "util": num(r[6])}
            for r in smi("gpu", "index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu")]


def query_gpus():
    """[(序号, UUID, 空闲 MB)]。"""
    return [(g["i"], g["uuid"], g["free"]) for g in gpu_table()]


def gpu_procs(*, strict=False):
    """GPU 上的计算进程（任何人的）：[(宿主机 PID, 显存 MB, UUID)]。
    测试时用 LABMON_GPU_APPS="pid[:mb[:uuid]],…" 代替 nvidia-smi。"""
    fake = None if strict else os.environ.get("LABMON_GPU_APPS")
    if fake is not None:
        return [(int(p[0]), num(p[1], 0.0), p[2]) for p in (x.split(":") + [None, None] for x in fake.split(",") if x)]
    rows = smi("compute-apps", "pid,used_memory,gpu_uuid", strict=strict)
    if strict and any(not pid.isdigit() or num(mem, -1) < 0 or not math.isfinite(num(mem, -1)) or not gpu for pid, mem, gpu in rows):
        raise ValueError("GPU 进程列表无法识别")
    return [(int(r[0]), num(r[1], 0.0), r[2]) for r in rows if r[0].isdigit()]


def gpu_apps():
    """GPU 上在跑的计算进程 PID（任何人的）。"""
    return [str(p) for p, _, _ in gpu_procs()]


# ---- 用量统计（只读 /proc 与 cgroup；读不到就留空，不影响调度）----
def scan_procs():
    """容器里的进程：{pid: {comm, ppid, sid, ticks（含已回收子进程的 CPU 时间）, hpid（宿主机 PID）}}。没有 /proc 时为空。"""
    out = {}
    try:
        names = os.listdir(PROC)
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(os.path.join(PROC, n, "stat")) as f:
                st = f.read()
            f = st.rsplit(")", 1)[1].split()
            p = {"comm": st[st.find("(") + 1:st.rfind(")")], "ppid": int(f[1]), "sid": int(f[3]), "ticks": sum(int(x) for x in f[11:15])}
        except (OSError, IndexError, ValueError):
            continue
        try:
            with open(os.path.join(PROC, n, "sched")) as fh:   # 3.10 内核在容器里也写宿主机 PID：nvidia-smi 给的就是它
                m = re.search(r"\((\d+), #threads", fh.readline())
            p["hpid"] = int(m.group(1)) if m else None
        except OSError:
            p["hpid"] = None
        out[int(n)] = p
    return out


def mem_of(pid):
    """进程占的内存（字节）：PSS（共享页按份摊，fork 出的 DataLoader 不会重复计），读不到时用 RSS。"""
    for name in ("smaps_rollup", "smaps"):
        try:
            with open(os.path.join(PROC, str(pid), name)) as f:
                return sum(int(l.split()[1]) for l in f if l.startswith("Pss:")) * 1024
        except (OSError, ValueError, IndexError):
            continue
    try:
        with open(os.path.join(PROC, str(pid), "statm")) as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def cg(*names):
    for n in names:
        v = read(os.path.join(CG, n))
        if v is not None:
            return v
    return None


def kv(text):
    return {p[0]: p[1] for p in (l.split() for l in (text or "").splitlines()) if len(p) >= 2}


def oom_kills():
    """容器里内核因撞内存上限杀进程的累计次数（cgroup v2 memory.events，v1 memory.oom_control）；读不到为 None。"""
    return num(kv(cg("memory.events", "memory/memory.oom_control")).get("oom_kill"))


def proc_io(pid):
    """进程累计读写的字节 (rchar, wchar)：/proc/<pid>/io，含已回收的子进程（内核在回收时并进父进程）；读不到（刚退出、没权限）为 (0, 0)。
    rchar、wchar 是经 read/write 系统调用的量，共享盘（NFS）与命中页缓存的都算，不只是落到本地盘的。"""
    try:
        d = kv(read(os.path.join(PROC, str(pid), "io"), "").replace(":", " "))
        return int(d.get("rchar", 0)), int(d.get("wchar", 0))
    except (TypeError, ValueError, AttributeError):
        return 0, 0


def ints(xs):
    try:
        return [int(x) for x in xs]
    except ValueError:
        return None


def nfs_counts(path):
    """/proc/self/mountstats 里 path 所在的 NFS 挂载（挂载点是 path 或它最近的上级）：{addr: clientaddr（同一物理机的几个容器相同）,
    ops: READ+WRITE 累计次数, ms: 两项累计执行时间（ms，含客户端排队）, bytes: 读写的字节}；不是 NFS 或读不到为 None。
    计数是宿主机挂载的，同一物理机的几个容器共用一份，只能看差值。"""
    best, cur = None, None
    for line in (read(os.path.join(PROC, "self", "mountstats"), "") or "").splitlines():
        if line.startswith("device "):
            m, cur = re.match(r"device \S+ mounted on (\S+) with fstype nfs", line), None
            mp = m.group(1).rstrip("/") or "/" if m else None
            if mp and (path == mp or path.startswith(mp.rstrip("/") + "/")) and (best is None or len(mp) > len(best[0])):
                cur = {"addr": None, "ops": 0, "ms": 0, "bytes": 0}
                best = (mp, cur)
            continue
        p = line.split()
        if cur is None or not p:
            continue
        if p[0] == "opts:":
            a = re.search(r"clientaddr=([^,\s]+)", line)
            cur["addr"] = a.group(1) if a else None
        elif p[0] in ("READ:", "WRITE:") and len(p) >= 9 and ints(p[1:9]):
            n = ints(p[1:9])                  # 次数 传输 超时 发出字节 收到字节 排队 往返 执行（后三项累计 ms）
            cur["ops"] += n[0]
            cur["ms"] += n[7]
            cur["bytes"] += n[4] if p[0] == "READ:" else n[3]
    return best[1] if best else None


def local_dev(path="/tmp"):
    """本地盘（容器的 /tmp）背后的块设备号「主:次」：cgroup 里本容器读写过最多的那块（v1 blkio、v2 io.stat）；
    读不到时用 mountinfo 里 path 所在挂载的设备号（overlay 的是假设备 0:xx，不算）。没有为 None。"""
    tot = {}
    for line in (cg("blkio/blkio.throttle.io_service_bytes", "blkio/blkio.io_service_bytes") or "").splitlines():
        p = line.split()
        if len(p) == 3 and p[1] in ("Read", "Write") and p[2].isdigit():
            tot[p[0]] = tot.get(p[0], 0) + int(p[2])
    for line in (cg("io.stat") or "").splitlines():
        p = line.split()
        d = dict(x.split("=", 1) for x in p[1:] if "=" in x)
        if p and ints([d.get("rbytes", 0), d.get("wbytes", 0)]):
            tot[p[0]] = tot.get(p[0], 0) + sum(ints([d.get("rbytes", 0), d.get("wbytes", 0)]))
    if tot and max(tot.values()) > 0:
        return max(tot, key=tot.get)
    best = None
    for line in (read(os.path.join(PROC, "self", "mountinfo"), "") or "").splitlines():
        p = line.split()
        if len(p) > 4 and (path == p[4] or path.startswith(p[4].rstrip("/") + "/")) and (best is None or len(p[4]) >= len(best[1])):
            best = (p[2], p[4])
    return best[0] if best and not best[0].startswith("0:") else None


def disk_counts(dev):
    """/proc/diskstats 里设备号 dev 那块盘：{name, ios: 读写完成次数, wait: 读写累计用时 ms, busy: 有读写在做的累计 ms}；没有为 None。
    容器里没有 lxcfs 时这是宿主机的计数，同一物理机的几个容器共用这块盘。"""
    for line in (read(os.path.join(PROC, "diskstats"), "") or "").splitlines():
        p = line.split()
        if len(p) >= 14 and "%s:%s" % (p[0], p[1]) == dev and ints(p[3:14]):
            n = ints(p[3:14])                 # 读次数 合并 扇区 读 ms 写次数 合并 扇区 写 ms 在做 忙 ms 加权 ms
            return {"name": p[2], "ios": n[0] + n[4], "wait": n[3] + n[7], "busy": n[9]}
    return None


def psi():
    """/proc/pressure 的 some avg60（%：近 60 秒里至少有一个任务在等这项资源的时间比例）{cpu, io, memory}；内核没有（4.20 之前，如 3.10）为 None。"""
    out = {}
    for k in ("cpu", "io", "memory"):
        m = re.search(r"^some .*avg60=([\d.]+)", read(os.path.join(PROC, "pressure", k), "") or "", re.M)
        if m:
            out[k] = float(m.group(1))
    return out or None


def press_raw(dev, t):
    """主机压力的累计计数（每轮读一次，两份求差见 press_rates）：CPU 被限流的周期（cgroup cpu.stat）、共享盘读写（nfs_counts）、
    本地盘（disk_counts）；PSI 本身就是均值，直接带上。读不到的项为 None。"""
    st = kv(cg("cpu/cpu.stat", "cpu,cpuacct/cpu.stat", "cpu.stat"))
    thr = ints([st.get("nr_periods", "x"), st.get("nr_throttled", "x")])
    return {"t": t, "thr": thr, "nfs": nfs_counts(DATA_ROOT), "disk": disk_counts(dev) if dev else None, "psi": psi()}


def press_rates(a, b):
    """两份计数（press_raw）之间的平均：thr 被限流的周期 %，共享盘 ms（READ+WRITE 每次平均执行时间，这段没有读写为 None）、
    ops 每秒次数、mib 每秒 MiB，本地盘 busy 忙碌 %、await 每次平均用时 ms。计数变小（容器重启、计数归零、换了设备）的项为 None。"""
    out, dt = dict.fromkeys(("thr", "ms", "ops", "mib", "busy", "await")), b["t"] - a["t"]
    if dt <= 0:
        return out
    if a["thr"] and b["thr"]:
        dp, dn = b["thr"][0] - a["thr"][0], b["thr"][1] - a["thr"][1]
        if dp > 0 and dn >= 0:
            out["thr"] = round(100 * dn / dp, 1)
    x, y = a["nfs"], b["nfs"]
    if x and y and all(y[k] >= x[k] for k in ("ops", "ms", "bytes")):
        do = y["ops"] - x["ops"]
        out.update(ops=round(do / dt, 1), mib=round((y["bytes"] - x["bytes"]) / dt / MIB, 2), ms=round((y["ms"] - x["ms"]) / do, 2) if do else None)
    x, y = a["disk"], b["disk"]
    if x and y and x["name"] == y["name"] and all(y[k] >= x[k] for k in ("ios", "wait", "busy")):
        di = y["ios"] - x["ios"]
        out.update(busy=round(min(100.0, (y["busy"] - x["busy"]) / dt / 10), 1), **{"await": round((y["wait"] - x["wait"]) / di, 2) if di else None})
    return out


def io_base(rows, now):
    """共享盘慢的基线（ms，每次读写的执行时间）：近 PRESS_KEEP_S（14 天）press.jsonl 里有读写（每秒至少 IO_MIN_OPS 次）的 5 分钟样本的 95 分位，
    且不低于 IO_SLOW_MS；样本不到 IO_BASE_N 个时取 IO_SLOW_MS。返回 (基线, 样本数)。判“共享盘慢”（nfs_slow）用它（#4 维护者定）。"""
    xs = sorted(r["ms"] for r in rows if r.get("ms") is not None and (r.get("ops") or 0) >= IO_MIN_OPS and (r.get("t") or 0) >= now - PRESS_KEEP_S)
    if len(xs) < IO_BASE_N:
        return float(IO_SLOW_MS), len(xs)
    return max(float(IO_SLOW_MS), xs[math.ceil(0.95 * len(xs)) - 1]), len(xs)


def nfs_slow(nfs):
    """共享盘此刻慢不慢（状态的 info.press.nfs）：近 1 分钟每次读写的执行时间超过本机基线（io_base），且每秒至少 IO_MIN_OPS 次读写
    （几次小写的延迟不算）。扫描与拷本地依赖让路（Worker.io_yield）、读写拥塞嫌疑（Worker.io_suspect）共用。"""
    return bool(nfs and nfs.get("ms") is not None and (nfs.get("ops") or 0) >= IO_MIN_OPS and nfs["ms"] > nfs["base"])


THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")


def cpu_env(env, n):
    """开跑时给任务的核数进环境（清单任务 Worker.start 与单元 slot_main 共用）：导出 LABMON_CPUS，THREAD_VARS 没设（或为空）的缺省设成它（#38）。
    已设的不盖：worker 与申请方自己环境里的在这里留着；清单的 .env 与清单行前缀的赋值在这之后才生效（bash -c 里 source、eval），本来就盖过缺省。"""
    env["LABMON_CPUS"] = str(n)
    for k in THREAD_VARS:
        if not env.get(k):
            env[k] = str(n)
    return env


def thr_step(since, thr, t):
    """这一段 CPU 限流从何时起（Worker.press 每轮调，状态的 throttle 与 fit 都按它）：没在限流时 thr（近 1 分钟被限流的周期 %）≥ THR_HOT 从 t 起算；
    限流中降到 THR_COOL 以下才结束（None）。thr 为 None（刚起、计数归零）时沿用原状态。"""
    if thr is None:
        return since
    if since is None:
        return t if thr >= THR_HOT else None
    return None if thr < THR_COOL else since


def cpu_over(u, give, t):
    """运行中任务超核没有：此刻的核数（use 里最近一个 1 分钟窗口 pks，没有或过了两个窗口取每轮的 cores）≥ 给的核数 give × CPU_OVER_RATIO
    且多出至少 CPU_OVER_MIN 核时返回这个核数，否则 None。限流时指出是谁（Worker.throttle）用它。"""
    pk = (u.get("pks") or [None])[-1]
    c = pk[1] if pk and t - pk[0] <= 2 * PEAK_WIN else u.get("cores")
    return c if c is not None and c >= give * CPU_OVER_RATIO and c - give >= CPU_OVER_MIN else None


def job_cpus(v):
    """核数取整（CPU 记账与开跑时导出的 LABMON_CPUS 同一个口径）：四舍五入、至少 1；写明 0 的是 0（不记账，导出时仍给 1）。配额另按向下取整（cpu_cap）。"""
    return 0 if v == 0 else max(1, int(v + 0.5))


def mem_unevictable(use, active_file, inactive_file):
    """容器里收不回的内存（字节）：cgroup 用量 − 文件页（活跃 + 不活跃页缓存）。v1 是 memory.usage_in_bytes − total_active_file − total_inactive_file，
    v2 是 memory.current − active_file − inactive_file。含匿名页、shmem（/dev/shm、tmpfs、SysV 共享内存：没有 swap 时收不回，挂在匿名页的 LRU 上，
    不在 *_file 里）与记到 cgroup 的内核内存。不取 rss + shmem：3.10 内核的 v1 没有 total_shmem，只剩 rss 会把 shmem 当成能回收的页缓存。
    组内服务器的探针（fleet_probe.sh 只送字段，fleet.parse 算）是同一个口径的另一份：探针在没有 Python 的镜像里跑，worker 不 import 本包。"""
    return max(0, use - active_file - inactive_file)


def cpu_cap(quota):
    """记账用的 CPU 上限：容器配额向下取整（7.5 核按 7）；没设上限为 None（不记 CPU）。"""
    return max(1, int(quota)) if quota else None


def cpu_quota():
    """容器的 CPU 配额（核数，cgroup v1 / v2；没设上限或读不到为 None）。"""
    q, per = cg("cpu/cpu.cfs_quota_us"), cg("cpu/cpu.cfs_period_us")
    if q is None and cg("cpu.max"):
        q, per = (cg("cpu.max").split() + ["100000"])[:2]
    return int(q) / int(per) if q and q.lstrip("-").isdigit() and int(q) > 0 and per and per.isdigit() else None


def host_info(gpus, data_root):
    """主机现状：容器内存（cgroup v1 / v2，读不到用 /proc/meminfo）、CPU 配额与累计用时、磁盘、容器启动时间、各卡。
    内存的 used 是工作集（用量 − 不活跃页缓存，只显示）；anon 是收不回的（用量 − 文件页，mem_unevictable），放行（mem_room）、内存闸门、
    借来的主机上算对方用量与快撞上限都用它：我们的任务读数据留下的活跃页缓存也在工作集里，内存紧时内核先回收它，不能算成别人的内存。"""
    h = {"gpus": gpus, "nproc": os.cpu_count()}
    lim, use = cg("memory/memory.limit_in_bytes", "memory.max"), cg("memory/memory.usage_in_bytes", "memory.current")
    if lim and lim.isdigit() and int(lim) < 2 ** 60 and use and use.isdigit():
        st = kv(cg("memory/memory.stat", "memory.stat"))
        stat = lambda k: int(st.get("total_" + k, st.get(k, 0)))      # noqa: E731 — v1 取含子组的 total_ 项，v2 只有不带前缀的
        h["mem"] = {"limit": int(lim) / GIB, "used": (int(use) - stat("inactive_file")) / GIB,
                    "anon": mem_unevictable(int(use), stat("active_file"), stat("inactive_file")) / GIB, "src": "cgroup",
                    "peak": num(cg("memory/memory.max_usage_in_bytes", "memory.peak"), 0) / GIB or None,
                    "failcnt": num(cg("memory/memory.failcnt")), "oom_kill": oom_kills()}
    else:
        mi = {k.rstrip(":"): int(v) * 1024 for k, v in kv(read(os.path.join(PROC, "meminfo"))).items() if v.isdigit()}
        if mi.get("MemTotal"):
            h["mem"] = {"limit": mi["MemTotal"] / GIB, "used": (mi["MemTotal"] - mi.get("MemAvailable", 0)) / GIB,
                        "anon": (mi.get("AnonPages", 0) + mi.get("Shmem", 0)) / GIB, "src": "meminfo"}
    h["cpu_quota"] = cpu_quota()
    usage = cg("cpuacct/cpuacct.usage", "cpu,cpuacct/cpuacct.usage")
    if usage is None and cg("cpu.stat"):
        usage = str(int(kv(cg("cpu.stat")).get("usage_usec", 0)) * 1000)
    h["cpu_ns"] = int(usage) if usage and usage.isdigit() else None
    h["disk"] = []
    try:
        s = os.statvfs(data_root)
        h["disk"].append({"path": data_root, "free": s.f_bavail * s.f_frsize / GIB, "total": s.f_blocks * s.f_frsize / GIB})
    except OSError:
        pass
    h["started"] = started_at(1)          # 容器启动时间 = PID 1 的启动时刻（服务器"重启"多半只是重启容器）
    h["rootfs"] = rootfs_info()
    return h


def rootfs_info():
    """容器根盘（ROOTFS）开机以来的新写入，续租、关机、换人会清空（#149）。单位 GiB：used 已用、total 总量（容器的配额）、fs 文件系统类型。
    ok：根盘是 overlay、总量比宿主机的盘（/etc/hosts 由宿主机绑定挂进来，statvfs 它得到的是宿主盘）小一成以上，即有项目配额：这时已用只算这个容器
    上层写的东西，就是续租会清掉的量；否则（不是 overlay、看不出宿主盘、没配额）测不准，LocalDisk 也不扫整个根盘。
    shm：/dev/shm 是容器私有的（挂载来源是 shm）时它的已用（续租同样清掉）；与宿主共用的 tmpfs不算这台的。读不到根盘为 None。"""
    mounts = {}
    for ln in (read(os.path.join(PROC, "mounts"), "") or "").splitlines():
        f = ln.split()
        if len(f) >= 3:
            mounts[f[1]] = (f[0], f[2])
    try:
        s = os.statvfs(ROOTFS)
    except OSError:
        return None
    total, used = s.f_blocks * s.f_frsize, (s.f_blocks - s.f_bfree) * s.f_frsize
    try:
        e = os.statvfs(os.path.join(ROOTFS, "etc/hosts"))
        host = e.f_blocks * e.f_frsize
    except OSError:
        host = None
    fs = mounts.get("/", (None, None))[1]
    out = {"used": round(used / GIB, 3), "total": round(total / GIB, 3), "fs": fs, "ok": fs == "overlay" and bool(host) and total < 0.9 * host}
    if mounts.get("/dev/shm", ("",))[0] == "shm":
        try:
            d = os.statvfs(os.path.join(ROOTFS, "dev/shm"))
            out["shm"] = round((d.f_blocks - d.f_bfree) * d.f_frsize / GIB, 3)
        except OSError:
            pass
    return out


def proc_tree(procs, roots):
    """roots 里的进程与它们的子孙（scan_procs 的进程表里有的；不在表里的根不算）。单元的进程、保活的进程树（sample 扣保活的内存）共用。"""
    kids = {}
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)
    out, todo = [], [pid for pid in roots if pid in procs]
    while todo:
        x = todo.pop()
        if x not in out:
            out.append(x)
            todo += kids.get(x, [])
    return out


def cpu_since(ticks, prev):
    """一棵进程树这一段用掉的 CPU 秒：本轮各进程累计（ticks，含已回收子进程的）之和比上一轮之和的增量，负数按 0。
    子进程被回收时它一生的 CPU 并进父进程，按整棵树求和不重不漏；回收者不在树里、整棵树没了时差值为负，按 0。
    任务的用量（measure）与保活进程树的核数（Worker.ka_cores）共用。prev 为 None（上一轮没量过）时是 0。"""
    return max(0.0, (ticks - prev) / os.sysconf("SC_CLK_TCK")) if prev is not None else 0.0


def job_procs(procs, jobs):
    """各任务此刻的进程 {id(任务): [pid]}：单元（slot）按进程树认（申请方 exec 成原命令，它和它的子进程）；
    清单任务按会话认，它起的单元（如登记成清单任务的监督进程）归单元，不重复记。"""
    mine = {id(j): proc_tree(procs, [j["pid"]]) for j in jobs if j.get("kind") == "slot"}
    in_slot = {pid for v in mine.values() for pid in v}
    for j in jobs:
        if j.get("kind") != "slot":
            sid = procs[j["pid"]]["sid"] if j["pid"] in procs else j["pid"]
            mine[id(j)] = [pid for pid, p in procs.items() if p["sid"] == sid and pid not in in_slot]
    return mine


def win_mean(w, t, span, n=3):
    """利用率窗口 w [(时刻, 值)] 里近 span 秒的样本平均（一位小数）；不到 n 个样本为 None。
    「在用时多高」（util10）与「吃满程度」（gpu_busy）两种口径共用（Worker.util_window 记样本，#51）。"""
    v = [x[1] for x in w if t - x[0] <= span]
    return round(sum(v) / len(v), 1) if len(v) >= n else None


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


class Worker:
    def __init__(self, host, shared=None):
        self.host = host
        self.p = Paths(host)
        self.disk = Disk(shared, host) if shared else None   # 共享盘用量（只有拿着租约的那台在扫）
        self.ldisk = LocalDisk(host)          # 容器根盘开机以来的新写入与 labmon 目录的用量（每台扫自己的，#149）
        self.running = {}     # 请求 key（队列 key 或 slot:<id>）→ 运行中的任务或单元
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
        self.durk = {}                                        # (项目, 任务, 清单估时, 同卡任务数 k) → duration 的结果（dur_k；queue_durs 重算时清掉）
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
        self.elsewhere = {}                   # 别的主机放行、这台不动的清单任务 {队列: (主机, 任务)}（run_elsewhere）：变了才记事件
        self.gone = set()                     # 看板说已归还的主机（returned.json，peers 一起读）：worker 不在时 offers 不把它的清单当说不准
        self.peer_st = {}                     # 别的在用主机的状态 {主机: status.json}（没过期、没 worker stop 过的；peers 一起读，offers 用）
        self.olists = {}                      # 别的主机上清单与 .env 的 {路径: (fsig, [(任务, 资源提示)] 或 .env 内容)}（list_of、list_env 缓存）
        self.offer, self.offer_t = [], 0.0    # 能接别处的任务（offers，写进状态给看板汇总挪法建议）与算出它的时刻
        self.bad_reg = set()                  # 坏了的登记文件（registry 跳过，只记一次事件）
        self.peer_regs = {}                   # 别的在用主机（peer_st）登记的清单 {(目录, 清单): [(登记文件修改时间, 主机)]}（peers 一起读，registry 认同一条清单归谁，#95）
        self.yielded = {}                     # 本机登记着、但别的在用主机登记得更晚的清单 {队列 key: 那台主机}（registry 不交给 tick，变了才记事件）
        self.stagers = {}                     # (请求 key, 任务) → 后台拷本地依赖的线程与结果（stage_bg）
        self.pwin, self.plast = [], None      # 主机压力的计数（press_raw）：近 PRESS_WIN 秒的几份、上次记 press.jsonl 时的那份
        self.pfail = False                    # press.jsonl 上次写失败了（连续失败只记一次日志）
        self.pdev, self.pdev_t = None, -PRESS_EVERY   # 本地盘的设备号（local_dev）与上次找它的时刻（没找到时隔 PRESS_EVERY 再找）
        self.slow = None                      # 共享盘此刻慢（nfs_slow）时为 info.press.nfs，否则 None（sample 定）
        self.thr_since = None                 # 这一段 CPU 限流从何时起（thr_step；没在限流为 None，load_prev 找回）
        self.defer = {}                       # 共享盘慢时推迟的事（扫描 "scan"、拷依赖 (请求 key, 任务)）→ 开始推迟的时刻（io_yield）
        self.cpu_block = {}                   # 请求 key → (任务, 开始一直因 CPU 被挡的时刻)：满 CPU_ALERT_S 才在等待项写 cpu_wait
        self.peer_box = []                    # 同一物理机（共享盘 clientaddr 相同）的别的主机在跑的任务 [(主机, 项目, 任务)]（peers 一起读）
        self.prows = [r for r in load_jsonl(self.p.press) if (r.get("t") or 0) >= time.time() - PRESS_KEEP_S]   # 本机近 14 天的压力历史（io_base）
        self.ka_off, self.ka_stop_t, self.ka_hits, self.fight = False, 0.0, [], None   # 要卡的这段里保活已停着、上次停的时刻、近 FIGHT_S 秒被重开的时刻、最近一次被重开（load_prev 找回，#124）
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
        env = subprocess.run(["bash", "-c", '. "$1/ka.env" 2>/dev/null; echo "${AGRI_ROOT:-}"; echo "${AGRI_KA_SCRIPT:+${AGRI_KA_DIR:-}/$AGRI_KA_SCRIPT}"', "_", KA_BIN],
                             capture_output=True, text=True).stdout.split("\n") if os.path.exists(os.path.join(KA_BIN, "ka.sh")) else []
        keepalive_demo = env[0].strip() if env else ""
        self.ka_full = (env[1].strip() if len(env) > 1 else "") or None   # ka.sh 认的保活：python 跑这个脚本（完整路径）；别的保活它停不了
        if keepalive_demo:
            self.ka_reg = os.path.join(keepalive_demo, "queues", "labmon-" + host)
        self.ka_ev = os.path.join(keepalive_demo, "keepalive", "events.log") if keepalive_demo else None   # ka.sh 记的停启与调用者

    # ---- 日志与事件 ----
    def wlog(self, msg):
        with open(self.p.log, "a") as f:
            f.write("%s %s\n" % (now_str(), msg))

    def qlog(self, q, msg):
        with open(os.path.join(q["state"], "queue.log"), "a") as f:
            f.write("%s %s\n" % (now_str(), msg))

    def event(self, kind, msg, **kw):
        """写 worker.log，同时记一条结构化事件（工作台的“最近事件”）。"""
        self.wlog(msg)
        with flock(self.p.events):
            append_jsonl(self.p.events, dict(kw, t=round(time.time()), kind=kind, msg=msg))

    # ---- 保活 ----
    def ka(self, action):
        """跑 ka.sh 的一个动作，返回 CompletedProcess（stdout 是文字：running 打印保活的进程组）。"""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}   # 保活用 py38，不能继承项目的补装包
        env["KA_CALLER"] = "labmon-worker:" + self.host
        return subprocess.run(["bash", os.path.join(KA_BIN, "ka.sh"), action], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def ka_groups(self):
        """保活在跑时为 {进程组: 组长的启动时刻（epoch 秒，读不到为 None）}（ka.sh running 一行一个进程组；ka.sh 用 setsid 起保活，
        组长就是保活进程），没在跑为 None。ka_caller 按它认 events.log 里哪条 RESTORED 是这台的。"""
        r = self.ka("running")
        return {int(g): started_at(int(g)) for g in r.stdout.split() if g.isdigit()} if r.returncode == 0 else None

    def keepalive(self, need_gpu):
        if not self.ka_reg:
            return
        pidf = os.path.join(self.ka_reg, "pid")
        if need_gpu:
            if read(pidf) != str(os.getpid()):
                os.makedirs(self.ka_reg, exist_ok=True)
                write(pidf, str(os.getpid()))
            groups = self.ka_groups()
            if groups is not None:           # 别的队列退出时会无条件 ka.sh start，看门狗也会拉起它：这里每轮都看
                if self.ka_off and self.ka_restarted(groups):
                    self.ka_idle, self.ka_why = 0, None
                    return                   # 看门狗正持着平时不持的锁：这一轮不停保活（停了它会再拉一次、又持锁），下一轮再看
                rc = self.ka("stop").returncode
                self.wlog("停 GPU 保活" + ("" if rc == 0 else "失败（退出码 %d）" % rc))
                self.ka_off, self.ka_stop_t = rc == 0, time.time()
            else:
                if self.ka_off and self.ka_wait:
                    self.ka_gone()
                self.ka_off = True
            self.ka_idle, self.ka_why = 0, None
            return
        self.ka_off, self.ka_wait = False, None
        if os.path.exists(pidf):
            os.remove(pidf)
        if time.time() - self.ka_checked < KA_CHECK_S:
            return
        self.ka_checked = time.time()
        if self.ka_groups() is not None:
            busy = [gp_label(g) for g in self.gp if g["owner"] != "keepalive"]
            if busy:                          # 有了别的 GPU 进程（借来的主机上多半是对方回来用）：保活让路，同保活守护；空下来再按下面的规则开
                rc = self.ka("stop").returncode
                self.wlog("GPU 上有了 %s，停保活让路" % "、".join(busy) + ("" if rc == 0 else "，没停成（退出码 %d）" % rc))
            self.ka_idle, self.ka_why = 0, None
            return
        others = self.ka_others()
        apps = gpu_apps()
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
            self.ka_idle, self.ka_why = 0, None

    def ka_others(self):
        """别的登记着在跑的队列（旧 gpu_queue.sh 等，退出时会无条件 ka.sh start）。"""
        return [os.path.basename(os.path.dirname(f)) for f in glob.glob(os.path.join(os.path.dirname(self.ka_reg), "*", "pid"))
                if read_pid(f) != os.getpid() and alive(read_pid(f), ("gpu_queue", "labmon_worker"))]

    def watch_scan(self):
        """在这台上找看门狗（watchdogs）：跳过 labmon 的任务、worker 自己和用着 GPU 的（保活除外）。"""
        skip = self.mine | {os.getpid()} | {g["pid"] for g in self.gp if g["pid"] and g["owner"] != "keepalive"}
        return watchdogs(self.procs or scan_procs(), skip)

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
        now = time.time()
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
        now, caller = time.time(), self.ka_caller(self.ka_wait["groups"])
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
        self.note_seen(sure, maybe, t=time.time() if self.ka_off else None)
        fresh = [w for w in load_frozen(self.p)["seen"] if (w["pid"], w["start"], w["cmd"]) not in self.watch_seen]
        self.watch_seen |= {(w["pid"], w["start"], w["cmd"]) for w in fresh}
        if fresh:
            self.event("watchdog", "找到保活看门狗，没动（把保活拉起来了才暂停确定的）：%s" % wd_text(fresh))

    # ---- 估计与记账 ----
    def acct(self, j):
        """运行中任务的记账：max(估计值, 实测)（CPU 开跑满 10 分钟后见下）。内存 GiB；显存按卡，MB；CPU 核（估计是 job_cpus 取整过的，实测取此刻的核数；占位任务不记）。
        CPU 另有回收（#65）：从开始量窗口核数（use 的 pk0）起满 CPU_WARM_S 后，估计那一份换成 min(开跑时给的核数, max(1, 近 CPU_WARM_S 内 1 分钟窗口核数的最大值))，
        再与此刻实测取大：估计取的是同类的 1 分钟峰值，多半出在开头加载阶段，之后整段按它占着，核记了账却没人用。内存、显存是硬资源（超了 OOM），不回收；
        CPU 超用只是被限流变慢。写了 cpus=0 的给 0 核，回收后仍是 0。"""
        e, u = j.get("est") or {"ram": 0.0, "vram": 0.0}, j.get("use") or {}
        vb = u.get("vram_by") or {}
        give = float(cpu_give(e.get("cpus", CPU_DEFAULT), cpu_cap(self.info.get("cpu_quota"))))
        if cpu_warm(u):
            give = min(give, max(1.0, max(x[1] for x in u["pks"])))
        return {"ram": max(e["ram"], u.get("ram") or 0.0),
                "vram": {g: max(e["vram"] * 1024, vb.get(g, 0.0)) for g in j["gpus"]},
                "cpu": 0.0 if is_hold(j["name"]) else max(give, u.get("cores") or 0.0)}

    def cpus_for(self, j):
        """开跑时导出的 LABMON_CPUS：估计核数封顶到配额（cpu_give，与运行中记账开头 10 分钟的估计同一个数），至少 1（写 cpus=0 的不记账，也给 1）。"""
        return cpus_export(j["est"]["cpus"], cpu_cap(self.info.get("cpu_quota")))

    def mem_room(self):
        """labmon 的任务全停时容器内存还能放多少（GiB）：上限 − labmon 之外收不回的内存 − 借来的主机上给对方留着、对方还没用上的部分。
        labmon 之外 = 收不回的内存（mem.anon，不算页缓存）− 我们任务的实测：读数据留下的页缓存内核会先回收，不算别人的（#4 维护者定）。
        读不到上限时为 None。在跑任务的记账由快照（snapshot）里的 jobs 扣（ram_free）。"""
        m = self.info.get("mem")
        if not m or not m.get("limit"):
            return None
        obs = sum((j.get("use") or {}).get("ram") or 0.0 for j in self.running.values())
        k = self.keep() or {}
        left = max(0.0, k["ram"] - (self.other["ram"] or 0.0)) if k.get("ram") and self.held() else 0.0
        return m["limit"] - max(0.0, m["anon"] - obs) - left

    def cpu_room(self):
        """labmon 的任务全停时还能给多少核（与 mem_room 同构）：记账上限（配额向下取整）− labmon 之外的 − 给对方留的 − ka.sh 停不了的保活。
        labmon 之外的按快照的 cpu（连着两轮占 1 核以上才算，零碎的 shell、一轮尖峰不算）；借来的主机上取 max(对方实测, keep.cpu)
        （对方还没用上的预留也扣；借来时对方空着的不预先留）。对方另起、ka.sh 停不了的保活不算对方的用量（不和预留取 max），
        它这一段占的核（other.ka_cpu）另扣，不看连着几轮（#37）。旧 gpu_queue.sh 的任务不记账，此刻用的核也算 labmon 之外。
        读不到配额为 None（不记 CPU）。在跑任务的记账由快照的 jobs 扣（cpu_free）。"""
        cap = cpu_cap(self.info.get("cpu_quota"))
        if cap is None:
            return None
        o = (self.other["cpu"] or 0.0) if self.cpu_n >= 2 else 0.0
        k = self.keep() or {}
        ext = sum((j.get("use") or {}).get("cores") or 0.0 for j in self.ext.values())   # 旧 gpu_queue.sh 的任务不记账，此刻用的核算 labmon 之外（同 mem_room）
        return cap - (max(o, k["cpu"]) if k.get("cpu") and self.held() else o) - self.other.get("ka_cpu", 0.0) - ext

    def snapshot(self, limits, gpus=None, gp=()):
        """资源快照：准入核心 place 的输入（字段见 place 的说明），真放行每放一个就重拼一份（前面放行的已进 running），
        推演（forecast、shadow）拿它当起点。gpus 是 query_gpus 给的各卡空闲（停保活之后查的），不给时用 sample 读到的；
        gp 是 gpu_now 给的此刻 GPU 进程（有独占请求时才查）。holds 就是 excl_hold 本身：place 在里面留卡、清卡；gholds 同理是成组留的资源 gang_hold。"""
        now = time.time()
        m = self.info.get("mem") or {}
        info = {g["uuid"]: g for g in self.info.get("gpus") or []}
        if gpus is None:
            gpus = [(g["i"], g["uuid"], g["free"]) for g in info.values()]
        seen, jobs = {}, []                   # seen：我们在跑任务在各卡上实测的显存。nvidia-smi 的空闲已经扣了它，记账在 jobs 里再扣，卡的 free 先加回去
        for j in self.running.values():
            a, vb = self.acct(j), (j.get("use") or {}).get("vram_by") or {}
            for g in j["gpus"]:
                seen[g] = seen.get(g, 0.0) + vb.get(g, 0.0)
            jobs.append({"key": j["key"], "project": j.get("project"), "name": j["name"], "t0": j["t0"], "t1": self.run_end(j, now),
                         "gpus": j["gpus"], "vram": a["vram"], "ram": a["ram"], "cpu": a["cpu"], "excl": j.get("excl"), "tag": j.get("tag"),
                         "hold": is_hold(j["name"]), "ngpu": j.get("ngpu", 1), "use": j.get("use") or {}, "cards": self.job_cards(j), "gang": j.get("gang")})
        cards = [{"i": i, "uuid": u, "total": (info.get(u) or {}).get("total") or 0.0, "keep": self.keep_left(i, u),
                  "other": self.other["vram"].get(u, 0.0), "free": f - self.keep_left(i, u) + seen.get(u, 0.0), "ka": bool(self.other["ka"].get(u))}
                 for i, u, f in gpus]
        return {"cards": cards, "limit": m.get("limit"), "room": self.mem_room(), "margin": ram_margin(m["limit"]) if m.get("limit") else None,
                "cpu_cap": cpu_cap(self.info.get("cpu_quota")), "cpu_room": self.cpu_room(),
                "jobs": jobs, "ext": list(self.ext.values()), "gp": list(gp), "cpu": self.other["cpu"] if self.cpu_n >= 2 else None,
                "holds": self.excl_hold, "tags": limits.get("tags") or {}, "owner": {u for u in self.back_last if u not in ("ram", "cpu")},
                "who": "对方" if self.guest() else "labmon 之外的进程", "kept": self.held(), "cut": None, "brief": False,
                # 成组（#180）：gholds 就是 gang_hold 本身（place 在里面留、放、让）；gangs 组里何时能一起开跑（gang_sync 的 gsim）；gfirst 这一轮最早放得下的时刻（推演里用）
                "gholds": self.gang_hold, "gangs": self.gsim, "gfirst": {}}

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
        return a if a and time.time() < a and not self.away_end else None

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
            self.event("guest", "对方说好 %s 前不用，现在回来了（%s）：恢复按预留借卡" % (now_str(a)[5:16], why), what=[why])

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
            self.back, self.back_last, self.tight, self.cpu_hot, self.away_base = {}, {}, None, 0, None
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
        self.back_last = {key: v for key, v in self.back_last.items() if key in over or t - v < GUEST_QUIET_S}
        if away and self.away_base:                    # 说好不用：开始以前的超出（卡、内存、CPU）不再挡（guest_block 与快照的 owner 都看 back_last），这一轮起就放行
            self.back_last = {key: v for key, v in self.back_last.items() if key in over or v > self.away_base["t0"]}
        if bool(over) != bool(self.back):
            self.event("guest", "借来的主机上对方用得比预留多（%s）：这部分不再放新任务" % "；".join(over.values()) if over else
                       "对方说好 %s 前不用：不再按预留挡，照常放行" % now_str(away)[5:16] if away else
                       "对方回到预留以内，%d 分钟后恢复放行" % (GUEST_QUIET_S / 60), what=list(over.values()))
        self.back = over

    def guest_block(self):
        """借来的主机上整台不放新任务的原因（对方的内存或 CPU 超出预留、或回到预留以内还不到 GUEST_QUIET_S）；没有为 None。"""
        hit = [k for k in ("ram", "cpu") if k in self.back_last]
        if not hit:
            return None
        return "借来的主机：" + "；".join(self.back.get(k) or "对方的%s %d 分钟前超出预留" % (
            {"ram": "内存", "cpu": "CPU"}[k], (time.time() - self.back_last[k]) / 60) for k in hit) + "，暂不放行"

    def guest_tight(self, t):
        """借来的主机快撞上限：对方超出预留，且那张卡空闲显存不到 TIGHT_FREE_MB、或容器内存到 TIGHT_MEM。不停正在跑的
        （这一格跑完，停了白跑；要不要停由用户线下和对方商量），超出的部分本来就不放新任务；变了记事件，状态里的 guest.tight 给看板提醒。"""
        why = None
        for g in self.info.get("gpus") or []:
            if g["uuid"] in self.back and g["free"] < TIGHT_FREE_MB:
                why = "%s，这张卡只剩 %.0f MB" % (self.back[g["uuid"]], g["free"])
        m = self.info.get("mem") or {}
        if not why and "ram" in self.back and m.get("limit") and m["anon"] / m["limit"] >= TIGHT_MEM:
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
        if self.hit and t - self.hit > VOID_KEEP_S:
            self.hit = None
        self.oom_n, self.oom_ours = n, 0

    # ---- 临时指令 ----
    def load_overrides(self):
        """读 overrides.json，去掉过期的（并记事件）；pause --now 的按各自的停法停下这个项目正在跑的任务，只做一次（记 applied；
        带 once 的是命令行不带 --host 的 pause --now：停完照样当暂停挡着，等看板写来的 sched.json 里有了这个项目的全局暂停才删（全局的接着挡），
        最多留 ONCE_HOLD_S（看板一直没写来、或全局的还没送到就被 resume 了）。停完立刻删的话，sched.json 还没到，被停的任务下一轮就又放行了）。读改写在文件锁（flock）里：命令行 override add/rm 同时写也不丢。
        返回这些再接上看板写来的项目级暂停、优先（sched_ovr，所有主机生效）。"""
        with flock(self.p.overrides):
            try:
                with open(self.p.overrides) as f:
                    ovr = json.load(f)
            except (OSError, ValueError):
                ovr = []
            keep = [o for o in ovr if not o.get("until") or o["until"] > time.time()]
            global_ovr = sched_ovr(self.p.sched)
            for o in ovr:
                if o not in keep:
                    self.event("override", "临时指令到期：%s" % ovr_text(o), **ovr_fields(o, "expire"))
            now = [o for o in keep if o["kind"] == "pause" and o.get("now") and not o.get("applied")]
            for o in now:
                for j in self.running.values():
                    if j.get("project") == o.get("target") and not is_hold(j["name"]):
                        self.stop_job(j)
                o["applied"] = True
            gp = {o["target"] for o in global_ovr if o["kind"] == "pause"}
            keep = [o for o in keep if not (o.get("once") and o.get("applied") and (o.get("target") in gp or time.time() - (o.get("t") or 0) > ONCE_HOLD_S))]
            if len(keep) != len(ovr) or now:
                write(self.p.overrides, json.dumps(keep, ensure_ascii=False))
        return keep + global_ovr

    def model(self):
        """这台的 GPU 型号（时长只认同型号的记录）；看不到卡为 None。"""
        return ((self.info.get("gpus") or [{}])[0]).get("name")

    def req_dur(self, r, k=None):
        """请求或运行中任务的预计时长（分钟，duration；估不出为 None）：清单任务取 queue_durs 算好的（按 k_now），单元现算（k 为 1）；
        给了 k（同卡并行数，运行中任务自己的 k_of）时按它挑同类记录，清单任务经 dur_k（与推演同一个缓存）。
        写了依赖的占位任务从开始（还没开始的按现在）到 dep_end 给的放行时刻，ends 用上一次推演汇的。
        成组的成员（不给 k 时）取组里各成员里最长的（gang_dur）：截止前跑不完、回填都按整组算（#180）。
        截止前的准入与回填（fits_before 再乘 DUR_MARGIN）、“开跑先记一笔”与运行中任务的预计结束（run_end，传 k）用。"""
        if k is None and r.get("gang") and r.get("kind", "queue") == "queue":
            return self.gang_dur(r)
        if r.get("kind", "queue") != "queue":
            d = duration(self.hist, r, self.model(), 1 if k is None else k)
        elif k is None:
            d = self.durs.get(r["key"], {}).get(r["name"])
        else:
            d = self.dur_k(r.get("project"), r["name"], r.get("est_min"))(k)
        d = d and d["min"]
        if is_hold(r["name"]) and r.get("project") and dep_keys(r):
            t0 = r.get("t0") or time.time()
            e = dep_end(dep_keys(r), t0, self.ends, self.dead)
            d = None if e is None else (e - t0) / 60
        return d

    def list_state(self, queues, reqs=()):
        """本机各清单此刻的状态（依赖按它认，也写进状态的 todo 给别的主机）：标完的 self.marks {(项目, 任务): done|failed}
        （几条清单说法不一的记 "?"，dep_state 看用量记录的最后一次）；还会跑的 self.todo，每条清单一项
        {project, root, queue, jobs [在跑的在前、其余按清单顺序], deps {任务: [[项目, 任务]…]}（只列写了依赖的）, gang {任务: [组名, 人数]}（只列成组的，
        在跑的也列）, held}，在跑、在等的单元与登记已撤的在跑任务各一项（root、queue 为 null）。别的主机按它认谁还会跑（dep_state）、组里有谁（gang_sync），
        offers 按它取别的主机上各清单还没跑的行。本机的成组成员另记进 self.gang_local（gang_sync、gang_publish 用）。
        暂停、drain 的清单照样列（任务还会跑），held 写 paused（状态目录有 paused）、pause 或 drain（项目暂停优先于主机 drain：用户有意停的，
        offers 不当成可挪的活，#25），等它的任务的等待原因据此写清。"""
        marks, todo, seen, gl = {}, [], set(), {}
        for q in queues:
            c = self.qlists.get(q["key"])
            if not c:
                continue
            seen.add(q["key"])
            j = self.running.get(q["key"]) or {}
            cur = j.get("name")
            ms, left = rows_left(q["state"], c[1], cur)
            ms = ms or set()
            ln = {"project": q["project"], "root": q["root"], "queue": q["jobs"], "jobs": [cur] if cur else [], "deps": {}, "gang": {},
                  "held": "paused" if "paused" in ms else (self.override_hit({"project": q["project"]}, ("pause",))
                                                           or self.override_hit({"project": q["project"]}, ("drain",)) or [None])[0]}
            for n, _ in c[1]:
                v = "done" if n + ".done" in ms else "failed" if n + ".failed" in ms else None
                if v:
                    marks[(q["project"], n)] = v if marks.get((q["project"], n), v) == v else "?"
            for n, h in ([(cur, j)] if cur else []) + left:
                if n != cur:
                    ln["jobs"].append(n)
                    d = dep_keys(dict(h, project=q["project"]))
                    if d:
                        ln["deps"][n] = [[p, x] for p, x, _ in d]
                if h.get("gang") and not is_hold(n):
                    ln["gang"][n] = [h["gang"], h.get("gn")]
                    gl.setdefault((q["project"], n), {"key": q["key"], "gang": h["gang"], "n": h.get("gn"), "root": q["root"], "queue": q["jobs"]})
            if ln["jobs"]:
                todo.append(ln)
        extra = [j for j in self.running.values() if j["key"] not in seen] + [r for r in reqs if r["kind"] == "slot" and r["key"] not in self.running]
        todo += [{"project": j.get("project"), "root": None, "queue": None, "jobs": [j["name"]], "deps": {},
                  "gang": {j["name"]: [j["gang"], j.get("gn")]} if j.get("gang") else {}, "held": None} for j in extra]
        self.marks, self.todo, self.gang_local = marks, todo, gl

    def dep_state(self, queues, now, reqs=()):
        """各任务此刻的状态 {(项目, 任务): pending|done|failed}（self.dep_idx，跑失败的另记 self.dead），依赖的放行判定（dep_block）与推演都按它。依次：
        - pending：还会跑的：本机清单里没标完的、在跑的（list_state），别的主机状态里的 todo（peers；那台 worker 不在、又没 worker stop 的
          DEP_STALE_S 内照样算，续租、重启时不误报找不到）。它盖过以前的结果，只有那台写状态之后才记下的用量记录（已跑完）盖过它；
        - 本机清单里有这一行的按标记：.done 成功、.failed 失败（几条清单说法不一的看用量记录的最后一次，没有记录算失败）；
        - 其余看各主机用量记录里的最后一次（作废、被挤掉重排的不算）。
        哪里都没有的不在表里（dep_block 满 DEP_MISS_S 才报找不到，起算时刻 self.dep_miss 只留这一轮还有人等、仍找不到的）。被等的任务按 项目 + 任务名 认，不看它在哪条清单、哪台主机。
        还会跑的在哪记进 self.dep_where（等待原因写清所在清单暂停、那台 worker 不在），各清单项记进 self.dep_lanes（dep_cycle 找环）。
        本机清单的状态每轮都算（list_state，写进状态给别的主机）；本机与别的主机都没有任务写依赖、也没有成组的任务时其余不算
        （别的主机上写了依赖的行可能被挪到这台，offers 的假设推演按这里的状态判它的依赖；成组的找环也要 dep_lanes，见 gang_block）。"""
        self.list_state(queues, reqs)
        self.peers(now)
        if (not any(ln["deps"] or ln["gang"] for ln in self.todo) and not any(dep_keys(j) for j in self.running.values())
                and not any(ln.get("deps") or ln.get("gang") for _, _, _, ln in self.peer_todo)):
            self.dep_idx, self.dead, self.dep_where, self.dep_lanes, self.dep_miss = {}, set(), {}, [], {}
            return
        last = {}
        for h in self.hist:                       # 按结束时刻排好的：后面的盖前面的
            if not h.get("requeued") and not h.get("void"):
                last[(h.get("project"), h.get("name"))] = ("done" if h.get("rc") == "0" else "failed", h.get("t1") or 0)
        idx = {k: v for k, (v, _) in last.items()}
        idx.update((k, v if v != "?" else idx.get(k, "failed")) for k, v in self.marks.items())
        where, lanes = {}, [(None, 0.0, now, ln) for ln in self.todo] + self.peer_todo
        for host, age, t, ln in lanes:
            for n in ln["jobs"]:
                k = (ln["project"], n)
                if host is not None and last.get(k, ("", 0))[1] > t:    # 那台写状态之后才跑完的：用量记录说了算
                    continue
                if k not in where or age < where[k][1]:                 # 几处都有的取本机的、再取状态最新的
                    where[k] = (host, age, ln.get("held"))
        idx.update((k, "pending") for k in where)
        self.dep_idx, self.dead, self.dep_where, self.dep_lanes = idx, {k for k, v in idx.items() if v == "failed"}, where, lanes
        want = {(p, x) for ln in self.todo for ds in ln["deps"].values() for p, x in ds} | {k[:2] for r in reqs for k in dep_keys(r)}
        self.dep_miss = {k: t for k, t in self.dep_miss.items() if k in want and k not in idx}

    def dep_block(self, r, now):
        """请求写的依赖（dep_keys）还没都满足：返回 (状态, 原因)，状态 wait（等的还在排、在跑）、failed（after= 等的跑失败了）、
        missing（等的哪里都找不到，持续满 DEP_MISS_S：起算时刻记在 self.dep_miss，时限内算 wait、原因写 MISS_NOTE）、cycle（依赖成环，谁也轮不到：dep_cycle）；
        都满足或没写为 None。挡住期间同被临时指令挡住：不参与排队、不算等待时间、不为它停保活；推演里照样排（排在依赖满足的时刻）。
        wait 以外的在状态里标出（等待项的 dep），看板出告警。"""
        st = [(k, self.dep_idx.get(k[:2])) for k in dep_keys(r)]
        bad = [k for k, v in st if v == "failed" and not k[2]]
        new = [k for k, v in st if v is None and now - self.dep_miss.setdefault(k[:2], now) < DEP_MISS_S]
        miss = [k for k, v in st if v is None and k not in new]
        pend = [k for k, v in st if v == "pending"]
        if bad:
            return "failed", "等的 %s 跑失败了，不会放行：修好重跑（删掉它的 .failed）后接着等；不等了就删掉这一行的 after=" % dep_names(bad, r["project"])
        if miss:
            return "missing", "找不到等的 %s（任务名写错了，或它所在的清单没登记）：改对 after= 后下一轮生效" % dep_names(miss, r["project"])
        ring = pend and self.dep_cycle(r, [k[:2] for k in pend])
        if ring:
            return "cycle", "依赖成环，谁也轮不到：%s。改清单顺序或 after= 后下一轮生效" % ring
        why = []
        if pend:
            e = dep_end([k for k, _ in st], now, self.ends, self.dead)
            note = "；".join(x for x in (self.dep_note(k[:2], r["project"]) for k in pend) if x)
            why.append("等 %s 跑完%s%s" % (dep_names(pend, r["project"]), "（预计 %s）" % now_str(e)[5:16] if e else "", "；" + note if note else ""))
        if new:
            why.append("等 %s（%s）" % (dep_names(new, r["project"]), MISS_NOTE))
        return ("wait", "；".join(why)) if why else None

    def dep_note(self, k, project):
        """还会跑、眼下却不会动的被等任务的说明（等待原因里写）：所在清单暂停、drain，或在 worker 不在的主机上；照常在排的为 None。"""
        host, age, held = self.dep_where.get(k, (None, 0, None))
        why = [{"paused": "所在清单暂停中", "pause": "所在项目暂停中", "drain": "所在主机 drain 中"}.get(held, held)] if held else []
        if host == POOL:
            why.append("在算力池里等着放到某台")
        elif host is not None and age > STATUS_STALE_S:
            why.append("在 %s 上，那台 worker 已 %d 分钟没更新" % (host, age / 60))
        return "%s %s" % (dep_names([k + (False,)], project), "，".join(why)) if why else None

    def dep_next(self):
        """依赖图里一个任务 (项目, 任务) 在等谁：返回 nxt(k) → [(等的任务, "等" 写的依赖里还会跑的 | "排" 同一条清单里排在它前面的 | "组" 与它成组的)]。
        清单项取 dep_lanes（本机的与别的主机 todo 里的）。dep_cycle（成环）与 dep_reaches（offers 插行防成环）共用。"""
        pos, grp = {}, {}
        for i, (_, _, _, ln) in enumerate(self.dep_lanes):
            for j, n in enumerate(ln["jobs"]):
                pos.setdefault((ln["project"], n), []).append((i, j))
            for n, (g, _) in (ln.get("gang") or {}).items():
                grp.setdefault((ln["project"], g), set()).add((ln["project"], n))
        mate = {k: ks for ks in grp.values() for k in ks}

        def nxt(k):                               # k 在等的：写的依赖里还会跑的、同一条清单里排在它前面的、与它成组的
            for i, j in pos.get(k, []):
                ln = self.dep_lanes[i][3]
                yield from (((p, x), "等") for p, x in (ln.get("deps") or {}).get(k[1], []) if (p, x) in self.dep_where)
                yield from (((ln["project"], x), "排") for x in ln["jobs"][:j])
            yield from ((x, "组") for x in sorted(mate.get(k, ())) if x != k)
        return nxt

    def dep_reaches(self, starts, goal):
        """从 starts 里的任务沿“等”“排”“组”走（dep_next），能不能走到 goal 里的任务：offers 把挪来的行插在 goal 那几行前面之前问它，
        走得到就是挪来的行（间接）在等它们，插在前面会成环（#43，Codex 审 #63）。"""
        nxt, todo = self.dep_next(), list(starts)
        seen = set(todo)
        while todo:
            k = todo.pop()
            if k in goal:
                return True
            for x, _ in nxt(k):
                if x not in seen:
                    seen.add(x)
                    todo.append(x)
        return False

    def dep_cycle(self, r, pend, mates=()):
        """依赖成环（清单顺序也算依赖：一行要等它前面的都跑完）：从 r 等的还会跑的任务 pend（与 r 成组的 mates）出发，沿“等谁”（写的依赖）、
        “排在谁后面”与“和谁成组”（gang=：要一起开跑，等于等同伴能开跑）找回 r 自己，返回最短的一圈的文字（“B 等 A，A 排在 B 后面”），没有为 None。
        只由“成组”连起来的一圈不算（几个成员本来就互相等着一起开跑）：圈里至少要有一条“等”或“排”（等谁跑完）。
        清单项取 dep_lanes（本机的与别的主机 todo 里的；在跑的排最前、不等谁）。dep_block 与 gang_block 共用。"""
        me = (r["project"], r["name"])
        name = lambda k: k[1] if k[0] == r["project"] else "%s/%s" % k
        if me in pend:
            return "%s 等它自己" % name(me)
        nxt = self.dep_next()
        # 广度优先，状态是 (任务, 走过的路上有没有“等”“排”)：找到的一圈最短
        prev, todo, end = {}, [], None
        for k, how in [(k, "等") for k in pend] + [(k, "组") for k in mates]:
            s = (k, how != "组")
            if s not in prev:
                prev[s] = ((me, False), how)
                todo.append(s)
        while todo and not end:
            s = todo.pop(0)
            for x, how in nxt(s[0]):
                hard = s[1] or how != "组"
                if x == me:
                    if hard:
                        end = (s, how)
                        break
                    continue
                if (x, hard) not in prev:
                    prev[(x, hard)] = (s, how)
                    todo.append((x, hard))
        if not end:
            return None
        steps, s = [(end[0][0], end[1], me)], end[0]
        while s != (me, False):
            p, how = prev[s]
            steps.append((p[0], how, s[0]))
            s = p
        say = {"等": "%s 等 %s", "排": "%s 排在 %s 后面", "组": "%s 与 %s 成组（要一起开跑）"}
        return "，".join(say[how] % (name(a), name(b)) for a, how, b in reversed(steps))

    # ---- 成组（清单行尾 gang=组名:人数，#180）：几个成员都留好资源再一起开跑，一个失败或停下其余的跟着停 ----
    # 各台只经共享盘交换：成员按各自状态里的 todo 认（list_state），此刻怎样看各自的 gang.json（gang_publish 写）。
    # 一轮里：gang_sync（认组、读同伴、gang_watch 停该停的）→ prep 里 gang_block（不能排队的原因）→ admit 里 place 留资源 → gang_commit 一起放行 → 推演 → gang_publish。
    def gang_sync(self, now):
        """每轮在 dep_state 之后、prep 之前：本机有成员的组里有谁、各在哪台、此刻怎样，建 self.gctx {(项目, 组名): {mem {(项目, 任务): {host（本机为 None）,
        age 那台状态已多久没更新, n 写的人数, when 预计何时能一起开跑, now 此刻就能, rec 那台 gang.json 里它的记录}}, at 别的主机上的成员里最晚的 when
        （有一个说不出为 None）, now 别的主机上的都此刻就能, why 说不出的原因}} 与给 place、推演的 self.gsim {(项目, 组名): {at, local {(请求 key, 任务): 预计}}}
        （本机成员的预计先取上次推演的 gang_eta，prep 把这一轮在队首、没被挡住的改成现在）。成员按 todo 认（本机与别的主机的，同 dep_state 的 peer_todo），
        一处同时有几份的取本机的、再取状态最新的。同组别的主机的 gang.json 每轮直接读（固定路径、不到 1 KB；读不到、写到一半的沿用上次的）。
        最后看本机在跑的成员要不要跟着同伴停下（gang_watch）。"""
        mem = {}
        for host, age, _, ln in [(None, 0.0, now, ln) for ln in self.todo] + self.peer_todo:
            for n, (g, gn) in (ln.get("gang") or {}).items():
                ms = mem.setdefault((ln.get("project"), g), {})
                old = ms.get((ln.get("project"), n))
                if old is None or (old["host"] is not None and (host is None or age < old["age"])):
                    ms[(ln.get("project"), n)] = {"host": host, "age": age, "n": gn}
        mine = [gk for gk, ms in sorted(mem.items()) if any(v["host"] is None for v in ms.values())]
        # 近 GANG_LATE_S 内见过的同组别的主机上的成员在哪台（它跑完、失败后就不在 todo 里了，gang_doomed 还要读它的结果）
        self.gang_known.update({(gk, k): (v["host"], now) for gk in mine for k, v in mem[gk].items() if v["host"]})
        self.gang_known = {x: hv for x, hv in self.gang_known.items() if now - hv[1] <= GANG_LATE_S}
        want = {v["host"] for gk in mine for v in mem[gk].values() if v["host"]} \
            | {w[0] for j in self.running.values() for w in j.get("gang_with") or [] if w[0] != self.host} \
            | {h for (gk, _), (h, _) in self.gang_known.items() if gk in mine}
        self.gpeer = {h: gang_file(h) or self.gpeer.get(h) for h in want}
        self.gctx, self.gsim = {}, {}
        for gk in mine:
            ctx = self.gctx[gk] = {"mem": mem[gk], "at": now, "now": True, "why": [], "bad": gang_bad(mem[gk])}
            for k, v in sorted(mem[gk].items()):
                if v["host"] is None:
                    continue
                v["when"], v["now"], why = self.gang_when(k, v, now)
                ctx["now"] = ctx["now"] and v["now"]
                if v["when"] is None:
                    ctx["at"] = None
                    ctx["why"].append(why)
                elif ctx["at"] is not None:
                    ctx["at"] = max(ctx["at"], v["when"])
            if ctx["bad"]:                        # 人数对不上：谁也不留、推演里同组的都不排
                ctx["at"], ctx["now"] = None, False
            loc = {(self.gang_local[k]["key"], k[1]) for k, v in mem[gk].items() if v["host"] is None and k in self.gang_local}
            self.gsim[gk] = {"at": ctx["at"], "local": {x: self.gang_eta.get(x) for x in loc}}
        self.gang_miss = {gk: t for gk, t in self.gang_miss.items() if gk in self.gctx}
        self.gang_watch(now)
        self.gang_doom = self.gang_lost(mine, now)

    def gang_lost(self, mine, now):
        """本机还没开跑的成员里，同一次开跑的同伴已经失败了的（先开跑的那个在这边跟上之前就失败收尾）：{(项目, 任务): 原因}，gang_doomed 记失败。
        认法同 gang_peer：同伴近 GANG_LATE_S 内的 fail 记录里，它开跑时记下的同伴（with）有这一员、记下的“最近一次结束”就是这一员此刻的（之后没跑过）。
        同伴失败后就不在 todo 里了，所以按近来见过的（gang_known）找它在哪台；本机的同伴看 gang_ends。"""
        run = {(j.get("project"), j["name"]) for j in self.running.values()}
        out = {}
        for gk in mine:
            recs = [(e["name"], e) for e in self.gang_ends if (e["project"], e["gang"]) == gk] + [
                ("%s（%s）" % (m[1], h), e) for (g, m), (h, _) in sorted(self.gang_known.items()) if g == gk
                for e in (self.gpeer.get(h) or {}).get("members") or [] if (e.get("project"), e.get("name")) == m]
            for k, m in sorted(self.gang_local.items()):
                if (k[0], m["gang"]) != gk or k in run:
                    continue
                last = gang_last(self.gang_ends, *k)
                for who, e in recs:
                    seen = {(w[0], w[1]): w[2] for w in e.get("with") or [] if len(w) > 2}
                    if e.get("state") == "fail" and now - (e.get("t1") or 0) <= GANG_LATE_S and seen.get((self.host, k[1])) == last:
                        out[k] = "同组的 %s 开跑后失败了，这边还没跟上：同一次开跑的整组记失败，没开跑" % who
                        break
        return out

    def gang_doomed(self, reqs, now):
        """gang_lost 找出的成员（还在队首等着）记失败：.failed、failed 事件、gang.json 的 fail、一行 0 分钟的用量行（rc 为 null：别的主机的依赖按它认失败），
        从这一轮的请求里去掉（留着的资源随之放掉），清单接着往下走。返回剩下的请求。"""
        out = []
        for r in reqs:
            why = r.get("gang") and r["kind"] == "queue" and self.gang_doom.get((r["project"], r["name"]))
            if not why:
                out.append(r)
                continue
            q = r["q"]
            open(os.path.join(q["state"], r["name"] + ".failed"), "w").close()
            self.qlog(q, "失败 %s（%s）" % (r["name"], why))
            self.event("failed", "失败 %s（%s）" % (r["name"], why), name=r["name"], project=r["project"])
            j = dict(r, t0=now, gpus=[], use={}, est=None, excl=None, wait=None, void=None)
            self.gang_end(j, "fail")
            with flock(self.p.usage):
                append_jsonl(self.p.usage, self.usage_rec(j, None, None, False))
        return out

    def gang_when(self, k, v, now):
        """别的主机上的成员 k（gang_sync 的 mem 项 v）此刻怎样：(预计何时能一起开跑：已留好资源的、刚开跑的为现在；说不出为 None,
        是不是此刻就能一起开跑（留好资源、那台 GANG_FRESH_S 内写的，或开跑不到 GANG_JOIN_S）, 说不出时的原因)。按那台 gang.json 里它的记录认，记进 v["rec"]。"""
        who = "%s（%s）" % (k[1], v["host"])
        if v["age"] > STATUS_STALE_S:
            return None, False, "%s 那台 worker 已 %d 分钟没更新" % (who, v["age"] / 60)
        g = self.gpeer.get(v["host"]) or {}
        rec = v["rec"] = gang_cur(g.get("members"), k)
        age = now - (g.get("t") or 0)
        if rec is None or age > STATUS_STALE_S:
            return None, False, "%s 还没给出就绪状态（那台 worker 若是旧版就不认 gang=，会单独放行它）" % who
        if rec["state"] == "ready":
            return now, age <= GANG_FRESH_S, None
        if rec["state"] == "run":
            t0 = rec.get("t0") or 0
            # 本机同组的成员在这一次开跑里已经结束（停下重排、失败）：不再跟上这一次，等它也停下后一起重来。
            # 这一次的结束 = 开跑时刻相差 GANG_JOIN_S 以内、又晚于同伴开跑时见过的那次（它 run 记录的 with：[主机, 任务, gang_last]）
            seen = {(w[0], w[1]): w[2] for w in rec.get("with") or [] if len(w) > 2}
            if any(e["project"] == k[0] and e["gang"] == rec.get("gang") and abs(e["t0"] - t0) <= GANG_JOIN_S
                   and e["t1"] > seen.get((self.host, e["name"]), 0) for e in self.gang_ends):
                return None, False, "%s 这一次开跑里同组这边的已结束，等它也停下后一起重来" % who
            if now - t0 <= GANG_JOIN_S:
                return now, True, None
            return None, False, "%s 已开跑 %d 分钟（没等到这边），会停下重排" % (who, (now - t0) / 60)
        if rec.get("eta") is None:
            return None, False, "%s %s" % (who, rec.get("why") or "推演里排不上，不知道何时能放行")
        return max(now, rec["eta"]), False, None

    def gang_block(self, r, now):
        """成组的成员此刻还不能参加排队的原因（prep 在依赖之后看，记进 r["dep"]，同依赖没满足：不排位、不算等待、不为它停保活）：(状态, 原因)，状态同 dep_block：
        missing 没写人数、凑不齐、人数写得不一样（后两种持续满 DEP_MISS_S，起算时刻 self.gang_miss：两边清单同时加行时那台还没认出）、cycle 一起开跑成环（dep_cycle）、
        wait 组里别的主机上有成员说不出何时能放行（不留资源）。能排队为 None。组里本机别的成员不在这里挡：它们这一轮在不在队首、放不放得下由 place 与 gang_commit 看。"""
        g = r.get("gang")
        if not g or r["kind"] != "queue":
            return None
        gk = (r["project"], g)
        if not r.get("gn"):
            return "missing", "成组 %s 没写人数：写成 gang=%s:人数（几个成员一起开跑）后下一轮生效" % (g, g)
        ctx = self.gctx.get(gk) or {"mem": {}, "why": [], "bad": "凑不齐：还没认出同组的成员"}
        ms, bad = ctx["mem"], ctx["bad"]
        if bad:
            t0 = self.gang_miss.setdefault(gk, now)
            if now - t0 < DEP_MISS_S:
                return "wait", "成组 %s %s；刚加的行要等那台 worker 认出，%d 分钟后仍这样就报找不到" % (g, bad, DEP_MISS_S // 60)
            return "missing", "成组 %s %s：改清单后下一轮生效" % (g, bad)
        self.gang_miss.pop(gk, None)
        ring = self.dep_cycle(r, [], [k for k in ms if k != (r["project"], r["name"])])
        if ring:
            return "cycle", "一起开跑成环，谁也轮不到：%s。同组的成员放在不同的清单里、互不 after=，改后下一轮生效" % ring
        if ctx["why"]:
            return "wait", "成组 %s：%s；先不留资源" % (g, "；".join(ctx["why"]))
        return None

    def gang_dur(self, r):
        """成组成员 r 截止前的准入与回填用的时长（分钟）：组里各成员预计时长里最长的（本机的取 queue_durs 算好的，别的主机的取它 gang.json 里的 dur）；
        有一个不知道为 None（约定归还照放、续租截止不放，同 override_hit）。req_dur 不给 k 时经它；运行中任务的预计结束按自己的（run_end 传 k）。"""
        own = (self.durs.get(r["key"], {}).get(r["name"]) or {}).get("min")
        ctx = self.gctx.get((r["project"], r["gang"]))
        ds = [own]
        for k, v in sorted((ctx or {}).get("mem", {}).items()):
            if k == (r["project"], r["name"]):
                continue
            m = self.gang_local.get(k) if v["host"] is None else None
            ds.append((self.durs.get(m["key"], {}).get(k[1]) or {}).get("min") if m else (v.get("rec") or {}).get("dur"))
        return None if None in ds else max(ds)

    def gang_soon(self, r, now):
        """成组成员这一轮算不算要卡（gpu_state 停不停保活）：组里预计 GANG_SOON_S 内就能一起开跑（gang_until）。不成组的都算。"""
        if not r.get("gang"):
            return True
        u = gang_until({"gholds": self.gang_hold, "gangs": self.gsim}, r, now)
        return u is not None and u <= now + GANG_SOON_S

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
                place(h["x"], self.snapshot(limits, gpus, apps), time.time())

    def gang_commit(self, reqs, waiting, now):
        """admit 之后：本机成员都是这一轮的队首、都在这一轮留好了资源（place 记的 gang_hold），别的主机上的成员都此刻就能（gang_when 的 now），
        就按留着的卡一起放行（launch；同伴记进 gang_with，接管时找回），从等待表里去掉。这一轮没重新留的（让位给屏障、不再是队首）已由 gang_recheck 放掉。
        推演里同一条规则在 simulate。先看到全员就绪的那台先跑，另一台看到它开跑（GANG_JOIN_S 内）就跟上；跟不上的由 gang_watch 停下重排。"""
        done = set()
        for gk, ctx in self.gctx.items():
            loc = sorted((r for r in reqs if r.get("gang") and (r["project"], r["gang"]) == gk), key=lambda r: r["key"])
            hs = [self.gang_hold.get(r["key"]) for r in loc]
            if (not loc or not ctx["now"] or len(loc) != sum(v["host"] is None for v in ctx["mem"].values())
                    or any(h is None or h["name"] != r["name"] for h, r in zip(hs, loc))):
                continue
            for r, h in zip(loc, hs):
                del self.gang_hold[r["key"]]
                # 同伴 [主机, 任务, 它最近一次结束的时刻（gang_last：那之后的结束才算这一次的，gang_peer、gang_when 认同一次用）]
                r["gang_with"] = [[v["host"] or self.host, k[1], gang_last(self.gang_ends if v["host"] is None else (self.gpeer.get(v["host"]) or {}).get("members"), *k)]
                                  for k, v in sorted(ctx["mem"].items()) if k != (r["project"], r["name"])]
                got = self.launch(r, h["cards"])
                if got is None:
                    done.add(r["key"])
                else:
                    self.wlog("成组 %s：%s 没放成（%s）" % (gk[1], r["name"], got[0]))
        waiting[:] = [w for w in waiting if w["key"] not in done]

    def gang_watch(self, now):
        """本机在跑的成员按同伴在这一次开跑里怎样（gang_peer：开跑时记下的 gang_with）决定要不要停下（gang_sync 末尾调）：同伴失败（含被作废）→ 按停法停下、记失败；
        同伴停下重排（暂停、被挤掉、容器重启中断、没跟上）或所在主机已归还 → 按停法停下、重新排队；开跑满 GANG_LATE_S 同伴还没跟着开跑 → 同样停下重排。
        同伴跑成功了的不管。已在停的不再看。停法见 gang_stop。"""
        recs = self.gang_recs(now)
        for j in list(self.running.values()):
            if not j.get("gang") or j.get("gang_stop"):
                continue
            for h, n, seen in j.get("gang_with") or []:
                who = n if h == self.host else "%s（%s）" % (n, h)
                st = gang_peer(recs if h == self.host else (self.gpeer.get(h) or {}).get("members"), j.get("project"), n, j["t0"], seen)
                if st == "fail":
                    self.gang_stop(j, "fail", "同组的 %s 失败了" % who)
                elif st == "stop" or (st == "run" and h in self.gone):
                    self.gang_stop(j, "stop", "同组的 %s %s" % (who, "停下重排了" if st == "stop" else "所在的主机已归还"))
                elif st is None and now - j["t0"] > GANG_LATE_S:
                    self.gang_stop(j, "stop", "开跑 %d 分钟了同组的 %s 还没跟着开跑" % ((now - j["t0"]) / 60, who))
                else:
                    continue
                break

    def gang_stop(self, j, kind, why):
        """跟着同伴停下一个成员：kind 是 fail（结束后记失败）或 stop（重新排队）。停法照这条清单登记的：term 发 SIGTERM、file 写停止请求文件；
        boundary 停不下，只记一条，等它自己跑完（跑成功记完成，否则照 kind）。结果由 finish 按 j["gang_stop"] 定（接管时找回）。"""
        mode = j.get("stop") or "boundary"
        j["gang_stop"] = {"kind": kind, "why": why, "sent": mode != "boundary"}
        if mode == "boundary":
            self.event("gang", "成组：%s；%s 登记的停法是 boundary，只能等它自己跑完（成组的清单请登记 --stop term）" % (why, j["name"]),
                       name=j["name"], project=j.get("project"), why=why)
            return
        if not self.send_stop(j, mode):
            j["gang_stop"]["sent"] = False
            self.event("gang", "成组：%s；未能向 %s 发停止请求" % (why, j["name"]),
                       name=j["name"], project=j.get("project"), why=why)
            return
        j["stopping"] = True
        self.event("gang", "成组：%s，按 %s 停下 %s，%s" % (why, mode, j["name"], "记失败" if kind == "fail" else "重新排队"),
                   name=j["name"], project=j.get("project"), why=why)

    def gang_end(self, j, state):
        """记下本机一个成员这一次开跑的结果（done、fail、stop）：写进 gang.json 留 GANG_KEEP_S，同伴按开跑时刻认同一次（gang_peer）。"""
        now = time.time()
        self.gang_ends = [e for e in self.gang_ends if now - e["t1"] < GANG_KEEP_S] + [
            {"project": j.get("project"), "gang": j["gang"], "n": j.get("gn"), "name": j["name"], "state": state, "t0": round(j["t0"]), "t1": round(now),
             "with": j.get("gang_with")}]

    def gang_recs(self, now, waiting=()):
        """本机成员此刻怎样（gang.json 的 members；gang_watch 看本机的同伴也用它）：在跑的 run（开跑时刻 t0，一起开跑的同伴 with 即 gang_with）、留好资源的 ready（从何时 t）、
        其余 wait（推演里最早何时放得下 eta，说不出为 None；它在队首被挡着的原因 why），各带 n、自己的预计时长 dur（分钟），再接上结束的（gang_end，留 GANG_KEEP_S）。"""
        ws, out, seen = {w["key"]: w for w in waiting}, [], set()
        for j in self.running.values():
            if j.get("gang"):
                seen.add((j.get("project"), j["name"]))
                out.append({"project": j.get("project"), "gang": j["gang"], "n": j.get("gn"), "name": j["name"], "state": "run", "t0": round(j["t0"]),
                            "with": j.get("gang_with"), "dur": (self.durs.get(j["key"], {}).get(j["name"]) or {}).get("min")})
        for k, m in sorted(self.gang_local.items()):
            if k in seen:
                continue
            h, w = self.gang_hold.get(m["key"]), ws.get(m["key"])
            e = {"project": k[0], "gang": m["gang"], "n": m["n"], "name": k[1], "dur": (self.durs.get(m["key"], {}).get(k[1]) or {}).get("min")}
            if h and h["name"] == k[1]:
                e.update(state="ready", t=round(h["t"]))
            else:
                eta = self.gang_eta.get((m["key"], k[1]))
                e.update(state="wait", eta=eta and round(eta), why=w["why"] if w and w.get("name") in (k[1], None) else None)
            out.append(e)
        return out + [e for e in self.gang_ends if now - e["t1"] < GANG_KEEP_S]

    def gang_publish(self, waiting, now):
        """写 gang.json（推演之后，每轮）：{t 写的时刻, host, members（gang_recs）, known 近来见过的同组别的主机上的成员 [[项目, 组名, 任务, 主机, 何时见到]]
        （gang_known；重启后 load_prev 读回：同伴失败后不在 todo 里了，不记着它在哪台就读不到它的结果，gang_lost 认不出）}。本机没有成员也没有近一天结束的时不写，之前写过的再写一次空的（文件建了不删：
        共享盘上新建的文件别的主机可能要等目录缓存过期才看得到，同伴只在 todo 里见到成员之后才读它）。"""
        recs = self.gang_recs(now, waiting)
        if recs or self.gang_pub:
            known = [[gk[0], gk[1], k[1], h, round(t)] for (gk, k), (h, t) in sorted(self.gang_known.items())]
            write(self.p.gang, json.dumps({"t": round(now, 1), "host": self.host, "members": recs, "known": known}, ensure_ascii=False))
        self.gang_pub = recs

    def gang_poll(self):
        """主循环拍与拍之间（约 2 秒一次）：本机有留好资源的成员时重读同伴的 gang.json，状态变了（就绪、开跑、停下）返回 True，提前进下一轮，
        几台一起开跑相差几秒。没有留着的不读。"""
        if not self.gang_hold:
            return False
        sig = []
        for gk in sorted({h["gang"] for h in self.gang_hold.values()}):
            for k, v in sorted(((self.gctx.get(gk) or {}).get("mem") or {}).items()):
                if v["host"]:
                    rec = gang_cur((gang_file(v["host"]) or {}).get("members"), k)
                    sig.append((v["host"], k, rec and rec["state"]))
        changed = self.gang_sig is not None and sig != self.gang_sig
        self.gang_sig = sig
        return changed

    def reload_hist(self):
        h = load_hist(self.hist_cache)
        if h is not None:
            self.hist, self.hist_v = h, self.hist_v + 1

    def queue_durs(self, queues):
        """各队列清单里每个任务的预计时长（duration，写进状态）与资源估计（estimate），推演（forecast）与截止前的准入用；
        清单、历史与型号都没变就沿用上次的。运行中任务的依赖（deps、deps_any）也按此刻的清单行更新：占位任务跑着时补写的 after= 下一轮就生效
        （它是开跑时从清单行读的，不更新就要等 worker 重启接管）。"""
        lists = {}
        for q in queues:
            try:
                m = fsig(q["jobs_path"])
            except OSError:
                continue
            c = self.qlists.get(q["key"])
            lists[q["key"]] = c if c and c[0] == m else (m, [(n, hints(cmd)) for n, cmd in list_jobs(q)])
        self.qlists = lists
        for k, j in self.running.items():
            h = first_rows(lists[k][1]).get(j["name"]) if k in lists else None
            if h:
                j["deps"], j["deps_any"] = h["deps"], h["deps_any"]
        kn, runs = self.k_now(), self.run_tot()
        key = (self.hist_v, self.model(), tuple(sorted((k, c[0]) for k, c in lists.items())), kn, tuple(sorted((k, tuple(v)) for k, v in runs.items())))
        if key == self.durs_key:
            return
        self.durs_key = key
        self.durs, self.qest, self.durk, memo = {}, {}, {}, {}
        for q in queues:
            out = self.durs[q["key"]] = {}
            est = self.qest[q["key"]] = {}
            for n, h in lists.get(q["key"], (0, []))[1]:
                d = self.dur_k(q["project"], n, h["est_min"])(kn)
                if d:
                    out[n] = dict(d, min=round(d["min"], 1))
                k = (q["key"], h["tag"], base_name(n), h["ram"], h["vram"], h["cpus"])      # 同类任务的估计一样，清单长时省得每个都扫一遍历史
                if k not in memo:
                    memo[k] = estimate(self.hist, dict(h, name=n, project=q["project"], root=q["root"], queue=q["jobs"]))
                est[n] = memo[k]

    def k_now(self):
        """此刻放一个新任务会遇到几条同卡的 labmon 任务（含自己）：最空的那张卡上在跑的（占位任务不算）+ 1；没有卡为 1。
        清单任务写进状态的预计时长、截止前的准入（req_dur）按它挑同类记录（duration 的 k，#67；准入在选卡前判截止，按最空的卡）。"""
        cards = [g["uuid"] for g in self.info.get("gpus") or []]
        return min((1 + sum(1 for j in self.running.values() if not is_hold(j["name"]) and u in (j.get("gpus") or [])) for u in cards), default=1)

    def k_of(self, j):
        """运行中任务自己的同卡并行数（含自己）：measure 记的 use.conc（按时间加权，与用量行的 conc 同口径，duration 挑记录比的就是它）；
        还没量到时按此刻它那几张卡上的 labmon 任务数（占位任务不算，多卡取平均）；不用卡的为 1。
        运行中任务的预计结束（run_end）与读写拥塞嫌疑（io_suspect）按它挑同类记录，不按给新任务的 k_now（#86）。"""
        c = (j.get("use") or {}).get("conc")
        on = j.get("gpus") or []
        if c or not on:
            return c or 1
        return card_conc(j, on, self.running.values())

    def run_tot(self):
        """在跑任务按日志进度估的总时长（模块级 run_tot，fit 也用）。"""
        return run_tot(self.running.values(), self.eta, self.eta_t)

    def dur_k(self, project, name, est_min):
        """清单里一个任务在同卡有 k 条 labmon 任务（含自己）时的预计时长（duration 的结果；估不出为 None）：返回 k → 结果 的函数，
        推演按开跑时那几张卡上的任务数调它（job_spec 的 durk）。按 (任务, k) 缓存，清单、历史、在跑的变了（queue_durs 的 durs_key）就清掉。"""
        run = [m for n, m in self.run_tot().get((project, base_name(name)), []) if n != name]

        def f(k):
            k = min(CONC_MAX, max(1, int(round(k))))
            c = self.durk.get((project, name, est_min, k), 0)
            if c == 0:
                c = self.durk[(project, name, est_min, k)] = duration(self.hist, {"name": name, "project": project, "est_min": est_min}, self.model(), k, run)
            return c
        return f

    def load_eta(self):
        """看板写来的运行中任务预计结束 {"t": 写的时刻, "jobs": {"项目|任务": 时刻}}（项目看板按日志进度算的，比时长估计准）；文件变了才重读。"""
        try:
            m = fsig(self.p.eta)
        except OSError:
            self.eta, self.eta_t, self.eta_m = {}, 0, None
            return
        if m != self.eta_m:
            try:
                d = json.loads(read(self.p.eta, "{}") or "{}")
            except ValueError:
                d = {}
            self.eta, self.eta_t, self.eta_m = d.get("jobs") or {}, d.get("t") or 0, m

    def run_end(self, j, now):
        """运行中任务的预计结束：看板写来的（任务开始后写的才算这一次）、否则 开始 + 预计时长；超了预计的剩余按 overrun_left 估（已跑的 20%、至少 10 分钟），
        不当“马上结束”。时长不明的（没估时也没有同类历史）为 None。
        占位任务不看看板写来的（它没有日志进度，看板的就是推演的回声）；写了 after= 的等的都已结束时算现在（它下一次检查就放行）。"""
        hold = is_hold(j["name"])
        e = None if hold else self.eta.get("%s|%s" % (j.get("project"), j["name"]))
        d = self.req_dur(j, self.k_of(j))
        end = e if e and self.eta_t >= j["t0"] else j["t0"] + d * 60 if d is not None else None
        if end is None or end > now:
            return end
        return now if hold and dep_keys(j) else now + overrun_left(now - j["t0"])

    # ---- 调度设置与公平份额 ----
    def policy(self):
        """调度设置：看板写来的 sched.json（用户级主机清单的 [sched]），没写的用缺省（POLICY）。返回 {cap 秒, hl 秒, w {项目: 权重}, own {项目: 人员键}}。
        own 是看板按项目主人写的（owner，#23；人员名的短哈希，不是名字）：同一个键的几个项目在 fair_key 里合起来算一份。"""
        try:
            d = json.loads(read(self.p.sched, "{}") or "{}")
        except ValueError:
            d = {}
        own = d.get("owner") if isinstance(d.get("owner"), dict) else {}
        return {"cap": float(d["wait_cap_min"]) * 60 if d.get("wait_cap_min") else POLICY["cap"],
                "hl": float(d["half_life_h"]) * 3600 if d.get("half_life_h") else POLICY["hl"],
                "w": {str(k): float(v) for k, v in (d.get("weight") or {}).items()},
                "own": {str(k): str(v) for k, v in own.items() if v}}

    def cards(self, ngpu, vram_gib, excl):
        """这台主机上一个任务占几张卡（card_share；卡的总显存取这台最小的那张）。"""
        gs = self.info.get("gpus") or []
        return card_share(ngpu, vram_gib, excl, min((g.get("total") or 0) for g in gs) / 1024 if gs else None, len(gs))

    def job_cards(self, j):
        """运行中任务占几张卡（cards）：按所用的卡数、显存估计值与独占算。份额、快照与用量记录共用。"""
        return self.cards(len(j["gpus"]), (j.get("est") or {}).get("vram") or 0.0, j.get("excl"))

    def share(self, now, pol):
        """各项目衰减后的 GPU 用量（卡·时，usage）：各主机跑完的（usage.jsonl）、别的主机在跑的（peers）与本机在跑的（own_runs，按预计结束先记一笔）。"""
        if self.hrows_v != self.hist_v:
            self.hrows_v, self.hrows = self.hist_v, []
            for h in self.hist:
                # 只认记了 project 的行（看板的项目 id）：没记的旧行按目录名猜出来的（如 project-a）与项目 id 对不上，
                # 份额里会冒出幽灵项目，所以不计（只留给估时用，hist_key）；半衰期 12 小时，它们很快就不起作用
                n, p = h.get("cards"), h.get("project")
                if n and p and h.get("t1") and h["t1"] > now - 30 * 86400:
                    self.hrows.append((p, n, h["t0"], h["t1"]))
        return usage(self.hrows + self.peers(now) + self.own_runs(now), now, pol["hl"])

    def own_runs(self, now):
        """本机在跑的任务 [(项目, 卡数, 开始, 预计结束)]：算用量，也写进状态的 share.run 给别的主机。"""
        return [(j.get("project"), round(self.job_cards(j), 3), round(j["t0"]), round(self.run_end(j, now) or now))
                for j in self.running.values() if not is_hold(j["name"])]

    def peers(self, now):
        """别的主机在跑的任务 [(项目, 卡数, 开始, 预计结束)]：各自状态里的 share.run（共享盘上同一个 HOME，PEER_EVERY 秒读一次；
        STATUS_STALE_S 秒没更新的状态不算，那台 worker 不在）。同时记下各自推演（plan，含在跑的 run 条）里每个任务的预计结束
        （peer_end）与还会跑的任务（状态里的 todo，见 list_state；peer_todo）：依赖等别的主机上的任务时查（dep_state、forecast）。
        todo 在那台 worker 不在时（状态没更新、又没有 worker stop 留下的 stopped）DEP_STALE_S 内照样算：续租、重启后那些任务还会跑；
        worker stop 过的（含借用归还）不算。另记下与本机在同一物理机上（状态里共享盘的 clientaddr 相同）的别的主机在跑的任务（peer_box，
        读写拥塞嫌疑的事件里列出）：29、29-15158、29-15159 这类同一物理机的几个容器读到的共享盘计数是同一份，按 addr 归到一起、不相加。
        没过期、没 worker stop 过的状态整份留在 peer_st（offers 读别的主机的清单、推演与临时指令），这些主机登记的清单记进 peer_regs（registry 认同一条清单归谁）。
        一并读看板写来的 returned.json（主机清单里已归还的，平台上关了机的自用主机也标成归还）进 gone：只给 offers 用，依赖照旧按 peer_todo。
        有没有更新看状态里那台 worker 自己写的时刻 t，不看文件修改时间：修改时间是共享盘服务器的时钟，比各容器慢 3 分多钟，看上去总超过 STATUS_STALE_S。"""
        if now - self.peer_t >= PEER_EVERY:
            self.peer_t, self.peer, self.peer_end, self.peer_todo, self.peer_box, self.peer_st = now, [], {}, [], [], {}
            try:
                self.gone = set(json.loads(read(self.p.returned, "{}") or "{}").get("hosts") or [])
            except (ValueError, AttributeError, TypeError):
                self.gone = set()
            addr = ((self.info.get("press") or {}).get("nfs") or {}).get("addr")
            for f in glob.glob(os.path.join(HOME, "hosts", "*", "status.json")):
                try:
                    if f == self.p.status:
                        continue
                    st = json.loads(read(f, "{}") or "{}")
                    d, age = os.path.dirname(f), now - (st.get("t") or 0)
                    if age <= DEP_STALE_S and not os.path.exists(os.path.join(d, "stopped")):
                        self.peer_todo += [(os.path.basename(d), age, st["t"], ln) for ln in st.get("todo") or []]
                    if age > STATUS_STALE_S:
                        continue
                    if not os.path.exists(os.path.join(d, "stopped")):
                        self.peer_st[os.path.basename(d)] = st
                    self.peer += [tuple(x) for x in (st.get("share") or {}).get("run") or []]
                    self.peer_end.update(((ln.get("project"), j["name"]), j.get("t1"))
                                         for ln in (st.get("plan") or {}).get("lanes") or [] for j in ln.get("jobs") or [])
                    if addr and (((st.get("info") or {}).get("press") or {}).get("nfs") or {}).get("addr") == addr:
                        self.peer_box += [(st.get("host") or os.path.basename(d), j.get("project"), j["name"]) for j in st.get("running") or [] if not j.get("hold")]
                except (OSError, ValueError, TypeError, AttributeError, KeyError):
                    continue
            self.peer_todo += self.pool_todo(now)
            self.peer_regs = {}
            for h in self.peer_st:
                for o in registry(os.path.join(HOME, "hosts", h, "queues"))[0]:   # 只认那台自己也会跑的：坏了的登记（缺 project 等）那台跳过，这里也不算
                    try:
                        self.peer_regs.setdefault((os.path.normpath(o["root"]), o["jobs"]), []).append((os.stat(o["file"]).st_mtime, h))
                    except OSError:                   # 刚被删掉的
                        continue
        return self.peer

    def pool_todo(self, now):
        """算力池里待放的清单还会跑的行（#24）：peers 一起读，按别的主机的 todo 的样子 [("pool", 0, now, 清单项)] 记进 peer_todo，
        别处 after= 等它时算还会跑（不报找不到，dep_note 写在池里等）。池目录没有 worker、没有状态，按登记与清单的状态目录现读（清单按修改时间缓存）。"""
        out = []
        for q in registry(os.path.join(HOME, "hosts", POOL, "queues"))[0]:
            rows = self.list_of(q["jobs_path"])
            if rows is None:
                continue
            ms, left = rows_left(q["state"], rows, None)
            if not left:
                continue
            ln = {"project": q["project"], "root": q["root"], "queue": q["jobs"], "jobs": [n for n, _ in left], "gang": {},
                  "deps": {n: [[p, x] for p, x, _ in d] for n, h in left for d in [dep_keys(dict(h, project=q["project"]))] if d},
                  "held": "paused" if ms and "paused" in ms else None}
            out.append((POOL, 0.0, now, ln))
        return out

    # ---- 推演 ----
    def job_spec(self, n, h, est, dur, durk=None):
        """一个任务在准入核心 place 眼里的样子（真放行经 as_job，推演里清单上的每个任务都这么拼）：h 资源提示（ngpu、excl、tag），
        est 资源估计（estimate：vram 每张卡 GiB 与来源 src、ram GiB 与来源 ram_src，cpus 核、cpu_src 来源），dur 预计时长（分钟，不知道为 None）。占位任务不独占、不占卡、不记 CPU，
        依赖（deps、deps_any）带着：推演里等它们满足才排，占位任务的结束取 dep_end。
        durk（推演给）：开跑时卡上有 k 条 labmon 任务（含自己）时的预计时长 durk(k)（duration 的结果，Worker.dur_k）；不给时用 dur。
        成组的带 gang（组名，place 按它留资源）：dur 是组里最长的（截止与回填按整组），durk 是自己的（推演里开跑后何时结束）。"""
        hold = is_hold(n)
        return {"name": n, "ngpu": h["ngpu"], "vram": est["vram"], "src": est["src"], "ram": est["ram"], "ram_src": est["ram_src"], "excl": None if hold else h["excl"],
                "cpus": 0 if hold else est["cpus"], "cpu_src": est["cpu_src"],
                "tag": h["tag"], "dur": dur, "durk": durk, "hold": hold, "deps": h["deps"], "deps_any": h["deps_any"], "gang": None if hold else h.get("gang"),
                "cards": 0.0 if hold else self.cards(h["ngpu"], est["vram"], h["excl"])}

    def as_job(self, r):
        """补全过的请求（prep）→ place 认的任务：job_spec 加 key、project 与能用的卡（清单登记的 --gpus）。"""
        return dict(self.job_spec(r["name"], r, r["est"], r["dur"]), key=r["key"], project=r["project"],
                    allowed=r["q"]["gpus"] if r["kind"] == "queue" else None)

    def cutoff(self):
        """推演里的截止：最早的 deadline 指令 (时刻, 没估时的放不放)；没有为 None。真放行里截止由 override_hit 挡。"""
        dl = sorted((o["at"], o.get("target") == "borrow") for o in self.ovr if o["kind"] == "deadline" and o.get("at"))
        return dl[0] if dl else None

    def shadow(self, r, now, pol, focus, limits):
        """屏障（等满 wait_cap 还卡在资源上的请求）预计最早何时放行：只排它一个，在跑的按预计结束腾出资源（simulate）；推不出为 None。
        成组的按它自己最早何时放得下算（不等同伴）。"""
        q = {"key": r["key"], "kind": r["kind"], "project": r["project"], "since": r["since"],
             "allowed": r["q"]["gpus"] if r["kind"] == "queue" else None, "jobs": [dict(self.as_job(r), gang=None)]}
        out, _ = simulate(now, self.snapshot(limits), [q], focus, self.cutoff(), pol=pol, ends=self.ends, dead=self.dead)
        return out[0]["t0"] if out else None

    def forecast(self, queues, reqs, limits, now, pol):
        """推演（simulate）这台主机往后怎么排，写进状态的 plan：项目看板的预计开始与完成、工作台与项目页的时间线都用它。
        被持久的临时指令挡住的队列（暂停、drain、续租截止前跑不完、只给别人用）不推演，看板照旧标暂停；借来的主机上对方此刻超出预留的照常推演
        （真放行挡、推演不模拟，同 simulate；override_hit 的 lasting）；有停不了的保活的卡推演里同样不放（sim_snapshot，#45）。
        输入（在跑的、各队首、清单、历史、临时指令、看板写来的预计结束、调度设置）变了就重算，没变也每 FORECAST_EVERY 秒重算一次。
        依赖查的各任务预计结束（dep_end 的 ends，存进 self.ends 给 req_dur 与等待原因）在这里汇：已跑完的（dep_state 认的）为 0，
        再依次盖上别的主机 plan 里的（peers）、本机在跑的与推演出的。被等的任务可能排在等它的任务后面才推演到，有写了依赖的任务时推两遍，
        第二遍代入第一遍的结果（在跑的占位任务也按它重算预计结束）。"""
        self.load_eta()
        wait = [r for r in reqs if r["key"] not in self.running]
        key = (tuple(sorted((r["key"], r["name"], bool(r["held"])) for r in wait)), tuple(sorted((k, j["name"]) for k, j in self.running.items())),
               tuple(q["key"] for q in queues),
               self.durs_key, json.dumps(self.ovr, sort_keys=True), self.eta_m, json.dumps(pol, sort_keys=True), frozenset(self.dep_idx.items()),
               json.dumps(self.todo),         # 还会跑的行（删了 .failed 重跑时清单与队首都没变）：plan 与写进状态的 todo 同一轮对得上（#25）
               frozenset(self.other["ka"]),   # 借来的主机上有停不了的保活的卡（推演不放它，#45）：出现、消失当轮重推
               # 成组：别的主机上的成员预计何时能一起开跑（此刻就能的记 0，不随时钟变）、本机成员在不在队首
               tuple((gk, g["at"] and (0 if g["at"] <= now else round(g["at"] / 60)), tuple(sorted((k, n, e and e <= now) for (k, n), e in g["local"].items())))
                     for gk, g in sorted(self.gsim.items())))
        if key == self.plan_key and now - self.plan["t"] < FORECAST_EVERY:
            return
        self.plan_key = key
        # 只排除被持久的挡法挡住的队列：对方此刻超出预留（guest_block）真放行挡、推演不模拟，照常排（#25）
        held, base = {r["key"] for r in wait if r["held"] and self.override_hit(r, lasting=True)}, self.snapshot(limits)
        qs, info = [], {}
        heads = {r["key"]: r for r in wait}
        ends = {k: 0 for k, v in self.dep_idx.items() if v == "done"}
        for q in queues:
            k = q["key"]
            if k in held or (k not in self.running and k not in heads) or k not in self.qlists:
                continue
            cur = (self.running.get(k) or {}).get("name")
            marks, pend = rows_left(q["state"], self.qlists[k][1], cur)
            if marks is None:
                continue
            # 正在跑的队列：下一个会不会被临时指令挡住（截止前跑不完由推演按时刻判断）
            hit = cur and pend and self.override_hit(dict(pend[0][1], q=q, key=k, kind="queue", name=pend[0][0], project=q["project"]), lasting=True)
            if hit and hit[0] != "deadline":
                pend = []
            jobs = [self.job_spec(n, h, self.qest.get(k, {}).get(n) or estimate(self.hist, dict(h, name=n, project=q["project"])),
                                  self.req_dur({"key": k, "name": n, "project": q["project"], "gang": None if is_hold(n) else h["gang"]}),
                                  self.dur_k(q["project"], n, h["est_min"])) for n, h in pend]
            qs.append({"key": k, "kind": "queue", "project": q["project"], "allowed": q["gpus"],
                       "since": heads[k]["since"] if k in heads else now, "jobs": jobs})
            info[k] = {"root": q["root"], "queue": q["jobs"], "project": q["project"], "kind": "queue"}
        for r in wait:
            if r["kind"] == "slot" and r["key"] not in held:
                qs.append({"key": r["key"], "kind": "slot", "project": r["project"], "allowed": None, "since": r["since"],
                           "jobs": [self.as_job(r)]})
        for j in self.running.values():
            info.setdefault(j["key"], {"root": None, "queue": None, "project": j.get("project"), "kind": j.get("kind", "queue")})
        for r in wait:
            info.setdefault(r["key"], {"root": None, "queue": None, "project": r["project"], "kind": r["kind"]})
        focus = {o.get("target") for o in self.ovr if o["kind"] == "focus"}
        use = usage(self.hrows + self.peer, now, pol["hl"])
        waits = {(q["key"], x["name"]): dep_keys(dict(x, project=q["project"])) for q in qs for x in q["jobs"]}     # 写了依赖的任务
        waits.update({(k, j["name"]): dep_keys(j) for k, j in self.running.items()})
        waits = {k: v for k, v in waits.items() if v}
        ends.update((k, v) for k, v in self.peer_end.items() if self.dep_idx.get(k) == "pending")   # 那台写推演之后已跑完的不再取它的预计
        gangs = self.gsim
        for i in range(2):
            ends.update(((y["project"], y["name"]), y["t1"]) for y in base["jobs"])
            on_dep = {}
            first = {}                            # 成组成员最早何时放得下（place 记）：写进 gang.json 给同伴，只看本机，几台互相引用也不会越推越晚
            out, stuck = simulate(now, base, qs, focus, self.cutoff(), pol=pol, use=use, ends=ends, dead=self.dead, on_dep=on_dep,
                                  gangs=gangs, first=first)
            ends.update(((e["project"], e["name"]), e["t1"]) for e in out)
            self.ends = ends
            # 本机成员还不知道何时放得下的（上一轮推演没有它、不在队首）：第二遍按这一遍里它最早何时放得下算，同组的才留得住
            unk = any(e is None for g in gangs.values() for e in g["local"].values())
            if i or not (waits or unk):
                break
            gangs = {gk: dict(g, local={x: first.get(x) if e is None else e for x, e in g["local"].items()}) for gk, g in gangs.items()}
            base = self.snapshot(limits)
        self.plan = {"t": now, "lanes": self.plan_lanes(base["jobs"], out, stuck, info, waits, on_dep)}
        self.starts = {(e["key"], e["name"]): round(e["t0"]) for e in out}
        self.gang_eta = first
        try:                                      # 只是提示：出错不拖垮放行与推演；沿用上一次的（最多 STATUS_STALE_S），免得建议消失一轮再出现（#25）
            self.offer, self.offer_t = self.offers(qs, base, now, pol, focus, use, info, {(r.get("root"), r.get("queue")) for r in wait if r["held"]}), now
        except Exception as e:  # noqa: BLE001
            if now - self.offer_t > STATUS_STALE_S:
                self.offer = []
            if self.noted.get("_offers") != str(e):
                self.wlog("算能接别处的任务出错：%r" % e)
                self.noted["_offers"] = str(e)

    def plan_lanes(self, run, out, stuck, info, waits, on_dep=None):
        """推演结果（simulate 的排上的与排不上的，加开始时在跑的 run）→ 状态里 plan 的车道 [{root, queue, project, kind, jobs, stuck, stuck_dep}]。
        forecast 写状态、offers 读假设推演里本机各车道何时跑完都经它。info：请求 key → {root, queue, project, kind}；waits：写了依赖的任务；
        on_dep：simulate 记的停在依赖上的队列 {key: 那一行}，写成车道的 stuck_dep（别的主机的 offers 据此不当“原处跑不了”，#183）。"""
        idx = {g["uuid"]: g["i"] for g in self.info.get("gpus") or []}
        lanes = {}
        for e in [dict(x, run=True) for x in run] + out:
            ln = lanes.setdefault(e["key"], dict(info[e["key"]], jobs=[], stuck=None, stuck_dep=None))
            x = {"name": e["name"], "t0": round(e["t0"]), "t1": e["t1"] and round(e["t1"]), "gpus": [idx.get(u) for u in e["gpus"]],
                 **({"run": True} if e.get("run") else {"after": e["after"]} if e.get("after") else {}),
                 **({"gang": e["gang"]} if e.get("gang") else {})}     # 成组的组名：同组的在各台的推演里一起开跑
            wf = waits.get((e["key"], e["name"]))
            if wf:
                x["deps"] = [[p, n] + (["any"] if a else []) for p, n, a in wf]     # 它等谁（看板画依赖连线、写“等 X、Y 跑完”）
            ln["jobs"].append(x)
        for k, why in stuck.items():
            ln = lanes.setdefault(k, dict(info[k], jobs=[], stuck=None, stuck_dep=None))
            ln["stuck"], ln["stuck_dep"] = why, (on_dep or {}).get(k)
        return list(lanes.values())

    # ---- 能接别处的任务（负载均衡的提示，Issue #20 第一档：只提示，不改清单、不改放行）----
    def list_of(self, path):
        """一条清单的 [(任务, 资源提示)]，按修改时间缓存（offers 取各行的资源提示；还会跑哪几行看 todo）；读不到为 None。"""
        try:
            m = fsig(path)
        except OSError:
            return None
        c = self.olists.get(path)
        if not c or c[0] != m:
            c = self.olists[path] = (m, [(n, hints(cmd)) for n, cmd in list_jobs({"jobs_path": path})])
        return c[1]

    def list_env(self, root, queue):
        """一条清单的环境：(目录, .env 去掉 QUEUE_STATE、QUEUE_LOGS 两行与空行、各行去掉首尾空白后的内容；没有 .env 或什么都不剩为 None)，.env 按修改时间缓存。任务在清单的目录下跑、
        读这条清单的 .env，offers 只把一批挪来的行取自同一种环境、只接到同一种环境的清单后面（#25 D）。QUEUE_STATE、QUEUE_LOGS 是 labmon
        给这条队列记账的位置，同目录的几条清单按规矩各写各的（两条队列不许共用状态目录），不算任务的环境。"""
        path = env_path(root, queue)
        try:
            m = fsig(path)
        except OSError:
            return os.path.normpath(root), None
        c = self.olists.get(path)
        if not c or c[0] != m:
            c = self.olists[path] = (m, "\n".join(x.strip() for x in (read(path, "") or "").splitlines()
                                                   if x.strip() and not re.match(r"\s*(export\s+)?QUEUE_(STATE|LOGS)=", x)) or None)
        return os.path.normpath(root), c[1]

    def proj_lanes(self, host, todo, plan, running, held=()):
        """一台主机上各清单还没跑完的部分与预计何时跑完（offers 用）：还会跑的行取那台状态里的 todo（list_state；本机取 self.todo），
        预计时刻取那台的推演（本机取这一次或假设的推演）。[{host, root, queue, project, pend [(任务, 资源提示, 推演的开始, 结束)], run（在跑那个的
        预计结束；没有在跑的为 0）, end, state, why, unread}]。
        - end：这条清单预计何时跑完（在跑的与没跑完的预计结束里最晚的；有一个不知道、或 state 是 unknown 就是 None）；
        - state：ok 照常在排；trapped 有没跑完的在推演里排不上（plan 写了 stuck：放不下、时长不明、截止前跑不完…），或被持久的临时指令挡住
          （约定归还、续租截止、drain、只给别的项目用：held 里的，或 todo 的 held 是 drain），在原处跑不了，可以挪（维护者 2026-11-08 定：续租截止
          挡住的也算）；unknown 推演里没有、又说不出为什么（读不了状态目录、队列没出请求），或推演停在依赖上（plan 的 stuck_dep：等的任务跑不成或
          那台说不出何时跑完、前面的占位任务不知道何时放行；挪走的行带着同样的 after=，不是原处跑不了，#183）：不知道原处何时放行，不挪（why 写原因），
          整批推不出（#25）。对方此刻超出预留的清单照常推演（forecast），不在这两种里；
        - 资源提示为 None：清单文件里没有这一行（刚剪过）或读不到清单（unread），不挪。同名的行取第一条（first_rows）。
        清单暂停（paused）、项目在那台暂停（pause）的不列：用户有意停的，不当成要均衡的活，也不算进整批何时跑完（同项目看板的全部完成）。
        running：(目录, 清单) → 在跑的任务名；held：等待项被临时指令挡住的 (目录, 清单)（那台状态的 waiting[].held；本机取 forecast 的等待请求）。"""
        pl = {(ln.get("root"), ln.get("queue")): ln for ln in plan.get("lanes") or [] if ln.get("kind") == "queue"}
        out = []
        for ln in todo:
            if not ln.get("queue") or ln.get("held") in ("paused", "pause"):
                continue
            key = (ln["root"], ln["queue"])
            cur = running.get(key)
            rows = self.list_of(os.path.join(ln["root"], ln["queue"]))
            hs = first_rows(rows or [])
            times = {j["name"]: j for j in (pl.get(key) or {}).get("jobs") or []}
            pend = [(n, hs.get(n), (times.get(n) or {}).get("t0"), (times.get(n) or {}).get("t1")) for n in ln["jobs"] if n != cur]
            if not pend and not cur:
                continue
            run = (times.get(cur) or {}).get("t1") if cur else 0
            ts, lp, ok = [run] + [x[3] for x in pend], pl.get(key) or {}, all(x[0] in times for x in pend)
            dep = not ok and lp.get("stuck_dep")
            state = ("ok" if ok else "unknown" if dep else
                     "trapped" if key in held or ln.get("held") == "drain" or lp.get("stuck") else "unknown")
            why = ("%s 上 %s：%s，说不出原处何时放行" % (host, ln["queue"], lp["stuck"]) if dep else
                   "%s 上 %s 推演里没有（读不了状态目录、队列没出请求之类），不知道原处何时放行" % (host, ln["queue"]) if state == "unknown" else None)
            out.append({"host": host, "root": ln["root"], "queue": ln["queue"], "project": ln["project"], "pend": pend, "run": run,
                        "end": None if None in ts or state == "unknown" else max(ts), "state": state, "unread": rows is None, "why": why})
        return out

    def offers(self, qs, base, now, pol, focus, use, info, held=()):
        """这台能接别的主机上哪些任务、接来后几点跑完（写进状态的 offers，看板汇总成挪法建议；labmon 不改清单、不改放行）。
        每个项目（别的在用主机上有没跑完的清单的）一项。挪哪些：从别的主机上这个项目排得最晚的清单（跑不了的算最晚）取队尾还没开跑的一行，
        那条清单的完工随之提前到它前一行的预计结束，再取此刻最晚的，至多 MOVE_MAX 行（原处跑不了的至多 MOVE_TRAP_MAX 行）；队尾是占位任务、
        原处 MOVE_LEAD_S 内就开跑、needs= 缺了又补不了、有别处的任务 after= 等着它、清单文件里找不到、与前面挑的不是同一种环境（目录与 .env）的
        就到此为止（只从尾巴取：清单里没写 after= 的先后 labmon 看不出来）；这个项目有清单原处何时放行说不出（proj_lanes 的 unknown，
        那台 worker 不在：状态过了 STATUS_STALE_S、没 worker stop、DEP_STALE_S 内）的一行也不挑，整批推不出（#25）。
        几点跑完：把前 k 行（按原清单的先后）加进这台的推演，走同一份 simulate（本机别的项目、公平份额、约定截止、依赖都算在里面；
        依赖的状态取 dep_state，别的主机上写了依赖的行也算）：这台已有这个项目、与挪来的行同一种环境（list_env）的清单就接在预计最早跑完的那条后面
        （不建议同机多开车道），没有就当新的一条。这个项目在这台不能用（drain、只给别的项目用、暂停；对方此刻超出预留不算，几小时后的事推演不模拟它）
        或这台在预备续租的只写原因。别的主机的状态过期、worker stop 过（含已归还）的不读（peers）。held：本机等待请求被临时指令挡住的 (目录, 清单)。
        每项 {project, lane 接在本机哪条清单后面（[目录, 清单]，None 要新建）, free 本机这个项目原推演里何时跑完（没有为现在）,
        ends {"主机|目录|清单": 别的主机上这个项目各清单预计何时跑完（不知道为 None）}, jobs [{host, root, queue, name, there 原处预计结束,
        rest 挪走它之后原处那条清单何时跑完, end 挪来前几行时它们在这台何时都跑完, own 这时本机这个项目自己的清单何时跑完}], why 再往下挪不了的原因
        （只放原因本身）, stop 挪到这台排不上的那一行（why 是推演的原因；与具体某行无关的为 None）}：原因句由看板拼（rebalance.suggest，#25 B）。"""
        mine = {(j["q"]["root"], j["q"]["jobs"]): j["name"] for j in self.running.values() if j.get("q")}
        own = self.proj_lanes(self.host, self.todo, self.plan, mine, held)
        others, waiters = [], {}
        for ln in self.todo:
            for n, ds in ln["deps"].items():
                for k in ds:
                    waiters.setdefault(tuple(k), set()).add((ln["project"], n))
        for h, st in sorted(self.peer_st.items()):
            run = {(j.get("root"), j.get("queue")): j["name"] for j in st.get("running") or []}
            hd = {(w.get("root"), w.get("queue")) for w in st.get("waiting") or [] if w.get("held")}
            others += self.proj_lanes(h, st.get("todo") or [], st.get("plan") or {}, run, hd)
            for ln in st.get("todo") or []:
                for n, ds in (ln.get("deps") or {}).items():
                    for k in ds:
                        waiters.setdefault(tuple(k[:2]), set()).add((ln.get("project"), n))
        for h, age, _, ln in self.peer_todo:      # 那台 worker 不在（续租、重启）：它的清单还会跑、看不到何时跑完，整批推不出（#25 第 8 条）
            # 算力池里待放的（#24）不算 worker 不在：放到哪台由看板定，不挡挪法建议
            if h not in self.peer_st and h not in self.gone and h != POOL and ln.get("queue") and ln.get("jobs") and ln.get("held") not in ("paused", "pause"):
                others.append({"host": h, "root": ln["root"], "queue": ln["queue"], "project": ln["project"], "pend": [], "run": 0, "end": None,
                               "state": "unknown", "unread": False,
                               "why": "%s 的 worker 不在（状态 %d 分钟没更新），那里的 %s 不知道何时跑完" % (h, age / 60, ln["queue"])})
        renew = [o for o in self.ovr if o["kind"] == "deadline" and o.get("target") != "borrow"]
        cx = {"qs": qs, "base": base, "now": now, "pol": pol, "focus": focus, "use": use, "info": info, "mine": mine, "held": held}
        out = []
        for p in sorted({ln["project"] for ln in others if ln["pend"]}):
            lns = [ln for ln in others if ln["project"] == p]
            mine_p = [ln for ln in own if ln["project"] == p]
            free = None if None in [ln["end"] for ln in mine_p] else max([now] + [ln["end"] for ln in mine_p])
            o = {"project": p, "lane": None, "free": free and round(free), "jobs": [], "why": None, "stop": None,
                 "ends": {"%s|%s|%s" % (ln["host"], ln["root"], ln["queue"]): ln["end"] and round(ln["end"]) for ln in lns}}
            out.append(o)
            hit = self.override_hit({"project": p, "name": ""}, ("drain", "guest", "pause"), lasting=True)
            if hit or renew or free is None:
                o["why"] = (hit[1] if hit else "这台在预备续租（%s），不接别处的任务" % ovr_text(renew[0]) if renew
                            else "这台上本项目的清单推演不出何时跑完")
                continue
            cands, o["why"] = self.move_cands(lns, free, now, waiters)
            # 接在本机这个项目、与挪来的行同一种环境（目录与 .env）的清单里预计最早跑完的那条后面（推演里在排的才算）；没有就当新的一条
            ends = {(ln["root"], ln["queue"]): ln["end"] for ln in mine_p}
            env = cands and self.list_env(cands[0]["root"], cands[0]["queue"])
            tgt = sorted((q for q in qs if q["project"] == p and q["kind"] == "queue"
                          and self.list_env(info[q["key"]]["root"], info[q["key"]]["queue"]) == env),
                         key=lambda q: ends.get((info[q["key"]]["root"], info[q["key"]]["queue"])) or now)[:1]
            o["lane"] = [info[tgt[0]["key"]]["root"], info[tgt[0]["key"]]["queue"]] if tgt and cands else None
            # 接在后面的清单里有写了 after= 的行：挪来的插在它前面（它要等别的任务，清单顺序也算依赖，接在它后面会被它堵住，#43）
            o["before"] = next((x["name"] for x in tgt[0]["jobs"] if x["deps"] or x["deps_any"]), None) if o["lane"] else None
            if o["before"]:                          # 挪来的有（间接）等它或它后面哪一行的：插在前面就成环（清单顺序也算依赖），接在末尾（Codex 审 #63）
                tj = [x["name"] for x in tgt[0]["jobs"]]
                if self.dep_reaches([k[:2] for c in cands for k in dep_keys(dict(c["h"], project=p))], {(p, n) for n in tj[tj.index(o["before"]):]}):
                    o["before"] = None
            for k in range(1, len(cands) + 1):
                got = self.try_move(p, cands[:k], tgt[0]["key"] if tgt else None, cx, before=o["before"])
                if isinstance(got, dict):
                    o.update(got)
                    break
                o["jobs"].append(dict({x: v for x, v in cands[k - 1].items() if x not in ("h", "i", "lane")}, end=got[0], own=got[1],
                                      dep=bool(cands[k - 1]["h"]["deps"] or cands[k - 1]["h"]["deps_any"])))
        out += self.local_offers(own, others, now, waiters, renew, cx)
        return out

    def local_offers(self, own, others, now, waiters, renew, cx):
        """同机多开一条车道（#43）：本机这个项目排得最晚的清单队尾几行剪到这台新开的一条车道，整批能提前多少。挑行（move_cands：从队尾往前、
        占位、成组、有人 after= 等着、MOVE_LEAD_S 内开跑、缺本地文件的都停）、推演（try_move，同一份 simulate）、看板那头的门槛与挑法都同跨主机的，
        只多两条：卡要有真余量（local_cards：限流、卡都吃满了、利用率低的卡上在跑的被 CPU 卡住都不出），估计只有提示值的 GPU 行不挪
        （没有实测，同卡并行怕 OOM，同 fit）。每项同 offers 的形状，另带 local: true；lane 恒为 None（新开一条）；ends 是别的主机上这个项目各清单的，
        free 是本机这个项目原推演里何时跑完；jobs 的 rest 是挪完后本机原清单何时跑完（新推演里，同卡变慢也算进去）。不出的不列（原因不叫醒、页面上也不写：fit 会说）。"""
        out = []
        if renew:
            return out
        for p in sorted({ln["project"] for ln in own if ln["pend"]}):
            if self.override_hit({"project": p, "name": ""}, ("drain", "guest", "pause"), lasting=True):
                continue
            lns = [ln for ln in own if ln["project"] == p and ln["state"] != "trapped"]
            mine_p = [ln for ln in own if ln["project"] == p]
            free = None if None in [ln["end"] for ln in mine_p] else max([now] + [ln["end"] for ln in mine_p])
            cards, why = self.local_cards()
            if why:
                continue
            # 原推演何时跑完推不出（free 为 None）时算不了收益，只看是不是因为行没有估时（那样的清单推演里排不上，state 是 trapped，也一起看）
            cands, why = self.move_cands(lns if free is not None else mine_p, now, now, waiters)
            for i, c in enumerate(cands):           # 只有提示值的 GPU 行不挪：没有实测，同卡并行怕 OOM（同 fit）
                e = estimate(self.hist, dict(c["h"], name=c["name"], project=p, root=c["root"], queue=c["queue"]))
                if c["h"]["ngpu"] and e["src"] == "提示值":
                    cands, why = cands[:i], "%s 的显存只有清单提示值、还没有实测，不与别的任务同卡" % c["name"]
                    break
            # gpus：新车道只许用的卡（推演就按它排）；不是全部卡时建议的 worker add 带 --gpus，否则真放行可能放到吃满的卡上（Codex 审 #63）
            o = {"project": p, "local": True, "lane": None, "free": free and round(free), "jobs": [], "why": why, "stop": None,
                 "gpus": cards if cards and len(cards) < len(self.info.get("gpus") or []) else None,
                 "ends": {"%s|%s|%s" % (ln["host"], ln["root"], ln["queue"]): ln["end"] and round(ln["end"]) for ln in others if ln["project"] == p}}
            if free is None:                         # 只在是因为要挪的行没有估时才留一条 nodur（看板出 :est，让会话写 est_min；Codex 审 #63）
                nd = next((c["name"] for c in cands if (self.dur_k(p, c["name"], c["h"]["est_min"])(self.k_now()) or {}).get("min") is None), None)
                if nd:
                    out.append(dict(o, stop=nd, why=None, nodur=True))
                continue
            for k in range(1, len(cands) + 1):
                got = self.try_move(p, cands[:k], None, cx, local=cards)
                if isinstance(got, dict):
                    o.update(got)
                    break
                o["jobs"].append(dict({x: v for x, v in cands[k - 1].items() if x not in ("h", "i", "lane")}, end=got[0], own=got[1],
                                      dep=bool(cands[k - 1]["h"]["deps"] or cands[k - 1]["h"]["deps_any"]), rest=got[2].get((cands[k - 1]["root"], cands[k - 1]["queue"]), round(now))))   # 整条剪空、也没在跑的：现在就跑完了
            if o["jobs"] or o["stop"]:
                out.append(o)
        return out

    def move_cands(self, lns, free, now, waiters):
        """offers 的候选：别的主机上这个项目的清单 lns 里，按“取此刻排得最晚的那条清单的队尾”挑出的几行（至多 MOVE_MAX，
        从原处跑不了的清单挑时至多 MOVE_TRAP_MAX），与挑不下去的原因。各候选带 host、root、queue、name、there（原处预计结束，跑不了为 None）、
        rest（挪走它后那条清单何时跑完）、h（资源提示）、i（在原清单里排第几）、lane（第几条清单）：前 k 行加进推演时按 (lane, i) 排，
        同一条清单里的照原先后。排得最晚的已不晚于本机这个项目何时跑完（free）的不再挑：挪过来只会更晚。
        waiters：{(项目, 任务): {写了 after= 等它的 (项目, 任务)}}（本机与别的主机的 todo）；有不在这批里的等着它就不挪：挪走后等它的何时开始会变。
        有清单原处何时放行说不出（state unknown）的一行也不挑：整批推不出，挪了也算不出提前多少。一批只取同一种环境（list_env：目录与 .env）的行。"""
        unk = next((ln for ln in lns if ln["state"] == "unknown"), None)
        if unk:
            return [], unk["why"]
        end = {i: ln["end"] for i, ln in enumerate(lns)}
        left = {i: list(ln["pend"]) for i, ln in enumerate(lns)}
        out = []
        while True:
            xs = [i for i in left if left[i]]
            if not xs:
                break
            i = max(xs, key=lambda i: math.inf if end[i] is None else end[i])
            if end[i] is not None and end[i] <= free:
                break
            ln, (n, h, t0, t1) = lns[i], left[i][-1]
            if len(out) >= (MOVE_MAX if end[i] is not None else MOVE_TRAP_MAX):
                if end[i] is None:
                    return out, "%s 上 %s 在原处跑不了的超过 %d 行，一次只挑这么多" % (ln["host"], ln["queue"], MOVE_TRAP_MAX)
                break
            if out and self.list_env(ln["root"], ln["queue"]) != self.list_env(out[0]["root"], out[0]["queue"]):
                return out, "再往前是 %s 上另一个目录或环境（.env）的清单 %s，一次只挪一种" % (ln["host"], ln["queue"])
            if is_hold(n):
                return out, "%s 上 %s 的队尾 %s 是占位任务，不挪" % (ln["host"], ln["queue"], n)
            if h is None:
                return out, ("这台读不到 %s 上的 %s，不挪" % (ln["host"], os.path.join(ln["root"], ln["queue"])) if ln["unread"]
                             else "%s 不在 %s 里（刚剪过？），不挪" % (n, os.path.join(ln["root"], ln["queue"])))
            if h.get("gang"):
                return out, "%s 上 %s 的队尾 %s 是成组的（gang=%s），挪了要连同组的一起看，不挪" % (ln["host"], ln["queue"], n, h["gang"])
            if t0 is not None and t0 - now < MOVE_LEAD_S:
                return out, "%s 上的 %s %d 分钟内就开跑，不挪" % (ln["host"], n, MOVE_LEAD_S / 60)
            ws = waiters.get((ln["project"], n), set()) - {(ln["project"], x["name"]) for x in out}
            if ws:
                return out, "%s 有 %s 写了 after= 等着它，不挪" % (n, "、".join(sorted(x for _, x in ws))[:200])
            if h["needs"]:
                q = load_queue({"_slug": qslug(ln["root"], ln["queue"]), "root": ln["root"], "jobs": ln["queue"], "project": ln["project"]})
                job = dict(h, name=n, project=ln["project"], cmd=job_cmd(q, n) or "")
                miss = [c["pat"] for c in check_needs(job, self.qenv(q)) if not c["files"] and not c["heal"]]
                if miss:
                    return out, "%s 缺本地文件：%s（这台上没有，也补不了）" % (n, "、".join(miss[:3]))
            left[i].pop()
            end[i] = left[i][-1][3] if left[i] else ln["run"]
            out.append({"host": ln["host"], "root": ln["root"], "queue": ln["queue"], "name": n, "there": t1 and round(t1),
                        "rest": end[i] and round(end[i]), "h": h, "i": len(left[i]), "lane": i})
        return out, None

    def try_move(self, p, cs, tgt, cx, local=False, before=None):
        """假设把项目 p 的候选 cs 挪到这台推演一遍（同一份 simulate，本机的快照、截止、公平份额与依赖都照真推演）：
        返回 (它们都跑完的时刻, 这时本机这个项目自己的清单何时都跑完, 本机原清单何时跑完 {(目录, 清单): 时刻}（同机挪时才有）)；
        有排不上的返回 {stop 那一行, why 推演里的原因, nodur 是不是因为那一行没有估时（看板据此提醒会话写 est_min，#43）}。
        tgt：接在后面的本机清单的 key（None：当新的一条）；cx：forecast 这一遍的输入（qs、base、info…）。
        挪来的几行按在那条车道里的位置认（接在原有的行后面，车道按顺序放），不按名字：同项目几条清单可能有同名的行。
        local（同机多开车道，local_offers）：候选是本机清单的队尾，从原队列里拿掉（原样搬过去，估计与时长不另算）、放进只许用这几张卡的新车道
        （local 是卡号列表；None 不限）；队尾与候选对不上（清单刚变过）就不挪。
        挪来的几行里没写 after=/afterany= 的排在写了的前面（清单顺序也算依赖：独立的行排在等别的任务的行后面会被它堵住，#43）；
        接在本机清单后面时插在那条清单里第一条写了依赖的行 before 前面（offers 定，文字里写明）。"""
        jobs, qs, cut = [], [], {}
        if local is not False:
            keys = {(v["root"], v["queue"]): k for k, v in cx["info"].items() if v.get("kind") == "queue"}
            for c in cs:
                cut.setdefault(keys.get((c["root"], c["queue"])), []).append(c["name"])
        for q in cx["qs"]:
            ns = cut.get(q["key"])
            if ns:
                if [x["name"] for x in q["jobs"][-len(ns):]] != ns[::-1]:
                    return {"stop": ns[0], "why": "%s 的队尾刚变过" % cx["info"][q["key"]]["queue"], "nodur": False}
                jobs, q = q["jobs"][-len(ns):] + jobs, dict(q, jobs=q["jobs"][:-len(ns)])
            qs.append(q)
        if None in cut:
            return {"stop": cut[None][0], "why": "推演里没有它所在的清单", "nodur": False}
        for c in sorted(cs, key=lambda c: (c["lane"], c["i"])) if local is False else ():
            f = self.dur_k(p, c["name"], c["h"]["est_min"])         # 与 forecast 拼本机清单行同口径：dur 取此刻的 k，durk 给推演按开跑时同卡的条数取
            jobs.append(self.job_spec(c["name"], c["h"], estimate(self.hist, dict(c["h"], name=c["name"], project=p, root=c["root"], queue=c["queue"])),
                                      (f(self.k_now()) or {}).get("min"), f))
        jobs = [x for x in jobs if not (x["deps"] or x["deps_any"])] + [x for x in jobs if x["deps"] or x["deps_any"]]
        key, n0 = tgt or "offer:" + p, 0
        for i, q in enumerate(qs):
            if q["key"] == key:
                n0 = next((n for n, x in enumerate(q["jobs"]) if x["name"] == before), len(q["jobs"]))
                qs[i] = dict(q, jobs=q["jobs"][:n0] + jobs + q["jobs"][n0:])
        if not tgt:
            qs.append({"key": key, "kind": "queue", "project": p, "allowed": None if local is False else local, "since": cx["now"], "jobs": jobs})
        out, stuck = simulate(cx["now"], cx["base"], qs, cx["focus"], self.cutoff(), pol=cx["pol"], use=cx["use"], ends=self.ends, dead=self.dead, gangs=self.gsim)
        moved = [e for e in out if e["key"] == key][n0:n0 + len(jobs)]
        bad = next((i for i, e in enumerate(moved) if e["t1"] is None), len(moved) if len(moved) < len(jobs) else None)
        if bad is not None:
            return {"stop": jobs[bad]["name"], "why": stuck.get(key) or "挪来后不知道何时跑完", "nodur": jobs[bad]["dur"] is None}
        ids = {id(e) for e in moved}
        lanes = self.plan_lanes(cx["base"]["jobs"], [e for e in out if e["key"] in cx["info"] and id(e) not in ids], {}, cx["info"], {})
        gone = {(cx["info"][k]["root"], cx["info"][k]["queue"]): set(ns) for k, ns in cut.items()}
        todo = [dict(ln, jobs=[n for n in ln["jobs"] if n not in gone.get((ln["root"], ln["queue"]), ())]) for ln in self.todo if ln["project"] == p]
        own = self.proj_lanes(self.host, todo, {"lanes": lanes}, cx["mine"], cx["held"])
        ends = [ln["end"] for ln in own]
        return (round(max(e["t1"] for e in moved)), None if None in ends else round(max([cx["now"]] + ends)),
                {(ln["root"], ln["queue"]): ln["end"] and round(ln["end"]) for ln in own if (ln["root"], ln["queue"]) in gone})

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
                return "drain", "主机 drain 中（%s）" % ovr_text(o)
            if o["kind"] == "guest":
                ps = [x for x in (o.get("target") or "").split(",") if x]
                if ps and r["project"] not in ps:
                    return "guest", "借来的主机只给 %s 用" % "、".join(ps)
                why = None if lasting else self.guest_block()
                if why:
                    return "guest", why
            if o["kind"] == "deadline" and o.get("at") and not is_hold(r.get("name")):
                d, lab, at = self.req_dur(r), deadline_name(o), now_str(o["at"])[5:16]
                if time.time() >= o["at"]:
                    return "deadline", "%s %s 已到，不再放行" % (lab, at)
                if d is None:
                    if o.get("target") == "borrow":   # 约定归还到点不停正在跑的，没估时的照常放（续租会重启容器，不放）
                        continue
                    return "deadline", "%s %s 前：没有估时，不放行" % (lab, at)
                if not fits_before(time.time(), d, o["at"]):
                    return "deadline", "%s %s 前跑不完（估 %.0f 分钟）" % (lab, at, d * DUR_MARGIN)
            if o["kind"] == "pause" and o.get("target") == r["project"]:
                return "pause", "项目 %s 暂停中（%s）" % (r["project"], ovr_text(o))
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
                text = "labmon：%s 停下 %s\n" % (now_str(), j.get("project"))
                if j.get("kind") == "slot":
                    path = mode[5:] if os.path.isabs(mode[5:]) else os.path.join("/", mode[5:])
                    write(path, text)
                else:
                    root = (j.get("q") or {}).get("root")
                    if not root:
                        raise ValueError("清单缺少 ROOT，无法写停止请求文件")
                    write_queue_stop(root, mode, text)
            else:
                if j.get("kind") != "slot":
                    queue_stop_path(mode)
                (os.killpg if j.get("kind") != "slot" else os.kill)(j["pid"], signal.SIGTERM)
        except (OSError, ValueError) as e:
            self.wlog("停 %s 失败：%s" % (j["name"], e))
            return False
        return True

    def note_stop(self, j, why, how):
        """记下被停下重排的任务（how：void 独占被打扰作废、squeeze 被对方挤掉、gang 跟着同组的停下），留 VOID_KEEP_S；看板据此告诉实验会话。"""
        t = time.time()
        self.voided = [v for v in self.voided if v["t"] > t - VOID_KEEP_S] + [
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
        r["wait"] = None if is_hold(r["name"]) or not r.get("since") else round((time.time() - r["since"]) / 60, 1)
        return self.start(r["q"], r, gpus) if r["kind"] == "queue" else self.grant(r, gpus)

    def start(self, q, job, gpus):
        """开跑清单里的一个任务；返回 None。开跑前再核一次归属：这一轮收请求之后登记被 worker add 搬到别的主机或被移除（#95），
        或别的主机已放了这条清单（current 已在，#88），这次不放，返回 (等待原因, False)，下一轮按新的登记与 current 重来。
        核归属、起进程、写 current 在登记归属的锁里（reg_lock，与 worker add 同一把）。"""
        st, lg = q["state"], q["logs"]
        with reg_lock():                          # 核归属到写下 current 一气做完：搬登记（worker add）插不进来（Codex 审查 #120）
            if q.get("file") and not os.path.exists(q["file"]):
                return "这条清单的登记刚搬到别的主机或被移除，这次不放", False
            if os.path.exists(os.path.join(st, "current")):   # 收请求之后别的主机已放了这条清单（两台都登记着，#88）：这次不放
                return "这条清单的状态目录里已有 current（别的主机刚放行），这次不放", False
            if q["key"] not in self.active:
                write(os.path.join(st, "lock"), str(os.getpid()))
                self.qlog(q, "队列启动 PID=%d 清单=%s（labmon worker）" % (os.getpid(), q["jobs"]))
                self.active.add(q["key"])
            rcf = os.path.join(st, job["name"] + ".rc")
            if os.path.exists(rcf):
                os.remove(rcf)
            sd, cd = scratch_dir(q["project"], job["name"]), cache_dir(q["project"])
            for d in (sd, cd):
                os.makedirs(d, exist_ok=True)
            env = dict(os.environ, PYTHONUNBUFFERED="1", QUEUE_ROOT=q["root"], LABMON_SCRATCH=sd, LABMON_CACHE=cd)
            env.pop("PYTHONPATH", None)
            env["CUDA_VISIBLE_DEVICES"] = ",".join(c["uuid"] for c in gpus) if job["ngpu"] > 0 else ""
            cpu_env(env, self.cpus_for(job))     # 给它记账的核数：项目拿它设线程数、DataLoader 进程数（nproc 是宿主机的）；没设的 BLAS/OpenMP 线程数缺省同它
            # 命令放子 shell 里跑：清单行里的 exit 不会跳过写退出码。借来的主机上先调高自己的 oom_score_adj（子进程继承）
            script = ("echo %d > /proc/self/oom_score_adj 2>/dev/null; " % GUEST_ADJ if self.guest() else "") \
                + 'set -a; [ -n "$1" ] && . "$1"; set +a; ( eval "$2" ); echo $? > "$3"'
            args = ["bash", "-c", script, "labmon-job:" + job["name"], q["env"] or "", job["cmd"], rcf]
            with open(os.path.join(lg, job["name"] + ".log"), "a") as out:
                out.write("===== %s labmon 开始（GPU %s）=====\n" % (now_str(), ",".join(str(c["i"]) for c in gpus) or "无"))   # 分隔行，见 START_RE
                out.flush()
                pr = subprocess.Popen(args,
                                      cwd=q["root"], env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      start_new_session=True)
            t0 = time.time()
            write(os.path.join(st, "current"), "%s %d %s %s" % (job["name"], pr.pid, now_str(t0), self.host))   # 放行的主机：别的主机据此不动它（run_elsewhere）
        gtxt = "，".join("GPU %d %s" % (c["i"], c["uuid"]) for c in gpus)
        self.qlog(q, "开始 %s" % job["name"] + ("（%s）" % gtxt if gtxt else ""))
        self.running[q["key"]] = dict(job, kind="queue", key=q["key"], pid=pr.pid, t0=t0, q=q, gpus=[c["uuid"] for c in gpus], use={},
                                      project=q["project"], stop=q["stop"], oom0=oom_kills(), fc0=num(cg("memory/memory.failcnt")))
        self.event("start", "开始 %s%s%s" % (job["name"], "（%s）" % gtxt if gtxt else "",
                                           "，独占%s" % {"gpu": "卡", "host": "整机"}[job["excl"]] if job.get("excl") else ""),
                   name=job["name"], project=q["project"], queue=q["jobs"])
        return None

    def grant(self, r, gpus):
        """放行一次单元申请：写 .ok（含选中的卡），申请方据此设 CUDA_VISIBLE_DEVICES 后 exec 原命令。"""
        base = os.path.join(SLOTS, r["id"])
        write(base + ".ok", json.dumps({"gpus": [c["uuid"] for c in gpus], "t": time.time(), "cpus": self.cpus_for(r),
                                        **({"adj": GUEST_ADJ} if self.guest() else {})}))
        if os.path.exists(base + ".why"):
            os.remove(base + ".why")
        self.running[r["key"]] = dict(r, t0=time.time(), gpus=[c["uuid"] for c in gpus], use={}, pstart=pstart(r["pid"]))
        self.event("grant", "放行 %s 的单元 %s（%s）" % (r["project"], r["name"], "，".join("GPU %d" % c["i"] for c in gpus) or "不用 GPU"),
                   name=r["name"], project=r["project"])

    def alive_job(self, j):
        if j.get("kind") == "slot":
            return cmdline(j["pid"]) is not None and (j.get("pstart") is None or pstart(j["pid"]) == j["pstart"])
        return alive(j["pid"], ("labmon-job:" + j["name"],))

    def finish(self, key):
        """收尾一个结束了的任务：单元清掉申请文件；清单任务按结果记 .done / .failed 或重新排队，记事件与用量行（requeued 按实际有没有重排）。
        清单任务依次：独占被打扰作废的重排；被对方挤掉的重排（至多 SQUEEZE_MAX 次）；跟着同组停下的（gang_stop）照它的 kind：stop 重排、fail 记失败；
        暂停时停下的重排；退出码 0 记完成；其余记失败。成组的成员被作废、跟着失败的同伴停下的都记失败（#180）；跑成功的（含 boundary 停法没停成、自己跑完的）照记完成。
        成组成员的结果记进 gang.json（gang_end），同伴按它决定跟不跟着停。
        记失败而进程自己退出码为 0 的（成组的跟着失败的同伴停下、被作废，收到停止请求后正常退出），用量行的 rc 记 null：
        依赖（dep_state）、估时与看板都只认 rc "0" 为跑成功，别的主机按用量行认它失败。"""
        j = self.running.pop(key)
        name, rc, oom, sq, requeue, failed = j["name"], None, None, None, False, False
        mins = round((time.time() - j["t0"]) / 60, 1)
        if j.get("kind") == "slot":
            requeue = bool(j.get("stopping"))     # 作废停下的单元由申请方重新申请
            for ext in (".req", ".ok", ".why"):
                f = os.path.join(SLOTS, j["id"] + ext)
                if os.path.exists(f):
                    os.remove(f)
            self.event("end", "单元结束 %s（%s，%.0f 分钟）" % (name, j["project"], mins), name=name, project=j["project"])
        else:
            q = j["q"]
            rcf = os.path.join(q["state"], name + ".rc")
            rc = read(rcf)
            oom = oom_of(j, rc) if rc != "0" and not j.get("stopping") else None   # 停下的、作废的不认
            sq = self.squeezed_by(j, oom)
            if sq:
                self.squeezed[name] = self.squeezed.get(name, 0) + 1
            gs = j.get("gang_stop")
            if gs and not gs.get("sent") and rc == "0":   # boundary 停法没停成、自己跑成功了：照记完成
                gs = None
            if j.get("void") and not j.get("gang"):   # 独占被打扰作废的：重新排队（事件在作废时已记）；成组的记失败（下面）
                requeue = True
                self.qlog(q, "作废 %s（%s），重新排队" % (name, j["void"]))
            elif sq and self.squeezed[name] <= SQUEEZE_MAX:   # 被对方挤掉的：不记失败，重新排队
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
                shutil.rmtree(scratch_dir(j["project"], name), ignore_errors=True)   # 失败的留着查
                self.ldisk.hint(os.path.dirname(scratch_dir(j["project"], name)))      # 本地盘上这个项目的单格临时少了一份
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
                        + ("；%s，已重排 %d 次，不再重排" % (sq, SQUEEZE_MAX) if sq else "；%s，同类下次估计抬高" % OOM_SAY[oom] if oom else "")
                self.qlog(q, "失败 %s（%s）" % (name, why))
                self.event("failed", "失败 %s（%s）" % (name, why), name=name, project=j["project"])
            if j.get("gang"):                     # 同伴按它跟着停或接着跑
                self.gang_end(j, "stop" if requeue else "fail" if failed else "done")
            for f in (rcf, os.path.join(q["state"], "current")):
                if os.path.exists(f):
                    os.remove(f)
        # 记失败而进程退出码为 0 的（成组的跟着同伴停下、被作废）记 null：各处只认 "0" 为跑成功（#180）；被对方挤掉的不算 OOM
        with flock(self.p.usage):
            append_jsonl(self.p.usage, self.usage_rec(j, None if failed and rc == "0" else rc, None if sq else oom, requeue))   # 下一轮 reload_hist 读回来（各主机的记录都在共享盘上）

    def usage_rec(self, j, rc, oom, requeue):
        """一个结束了的任务的用量行（usage.jsonl 的一行，finish 与没开跑就跟着记失败的成组成员 gang_fail_unstarted 共用）：
        rc 退出码（字符串；记失败而退出码为 0 的由调用方传 None）、oom 失败是不是 OOM、requeue 有没有重新排队。"""
        u = j.get("use") or {}
        return {"t0": round(j["t0"]), "t1": round(time.time()), "min": round((time.time() - j["t0"]) / 60, 1), "kind": j.get("kind", "queue"),   # 起止时刻（epoch 秒）
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
            self.voided = [v for v in old.get("voided") or [] if v.get("t", 0) > time.time() - VOID_KEEP_S]
            kf = old.get("kafight") or {}         # 保活被拉起的告警撑满 FIGHT_S，不因重启提前消失（#124）；两次读以后一次（旧 worker 最终的状态）为准
            hits = [t for t in kf.get("hits") or [] if t > time.time() - FIGHT_S]
            self.ka_hits, self.fight = (hits, {"t": kf.get("t"), "who": kf.get("who")}) if hits else ([], None)
            fresh = time.time() - (old.get("t") or 0) <= STATUS_STALE_S
            if fresh:                             # 停得不久（升级、重启接管）：找不到的起算时刻沿用；停久了别处的清单可能早变了，从头计时
                self.dep_miss = {(p, n): t for p, n, t in old.get("dep_miss") or []}
            # 要卡时停着的保活：重启那段里被拉起，接管后第一轮照样记“保活被拉起”（#124）；停久了那段里怎样说不准，不沿用
            self.ka_off, self.ka_stop_t = (True, old["ka_stop"]) if fresh and old.get("ka_stop") else (False, 0.0)
            self.thr_since = (old.get("throttle") or {}).get("since") if fresh else None   # 限流的起算时刻：升级、重启不从头计时
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.prev, self.voided = {}, []
        # 成组成员近一天的结果（gang.json 里结束了的）：重启后照样告诉同伴（#180）
        g = gang_file(self.host) or {}
        self.gang_ends = [e for e in g.get("members") or [] if isinstance(e, dict) and e.get("state") in ("done", "fail", "stop")
                          and time.time() - (e.get("t1") or 0) < GANG_KEEP_S]
        self.gang_pub = g.get("members") or None
        self.gang_known = {((p, gn), (p, n)): (h, t) for p, gn, n, h, t in (k for k in g.get("known") or [] if isinstance(k, list) and len(k) == 5)
                           if time.time() - (t or 0) <= GANG_LATE_S}

    def adopt(self, q):
        """接管上一个 worker 留下的任务（别的主机放行的不接管，requests 先过 run_elsewhere）：还在跑的继续盯；跑完了的按 .rc 记账；进程没了又没有 .rc 的（服务器重启）重跑。"""
        cur = read_current(q["state"])
        if not cur:
            return
        name, pid, t0, who = cur
        local = not who or who == self.host       # 别的主机放的（那台已归还，run_elsewhere 才放过来）：PID 是另一个容器的，这里同号的进程不是它
        old = self.prev.get((q["root"], q["jobs"], name, pid), {})
        cmd = job_cmd(q, name) or ""
        gpus = old.get("gpus")
        if gpus is None:                          # 上一份状态里没有它（旧 worker 没写过状态）：从进程的 CUDA_VISIBLE_DEVICES 找回，只认此刻看得到的卡
            known = {g["uuid"] for g in gpu_table()}
            gpus = [u for u in (proc_env(pid, "CUDA_VISIBLE_DEVICES") or "").split(",") if u in known] if local else []
        job = dict(hints(cmd), name=name, cmd=cmd, kind="queue", key=q["key"], pid=pid, t0=t0, q=q, project=q["project"], stop=q["stop"],
                   gpus=gpus, use=old.get("use") or {}, oom0=old.get("oom0"), fc0=old.get("fc0"), owner_gpu=old.get("owner_gpu"), io_sus=old.get("io_sus"),
                   cpu_fit=old.get("cpu_fit"), gang_with=old.get("gang_with"), gang_stop=old.get("gang_stop"))
        job["excl"] = old.get("excl", job["excl"])
        if job["gang_stop"] and job["gang_stop"].get("sent"):   # 停止请求已发：收尾照原重排规则记
            job["stopping"] = True
        job["est"] = old.get("est") or estimate(self.hist, dict(job, root=q["root"], queue=q["jobs"]))
        if local and alive(pid, ("labmon-job:" + name,)):
            self.running[q["key"]] = job
            self.active.add(q["key"])
            write(os.path.join(q["state"], "lock"), str(os.getpid()))   # lock 改指本进程，看板才认得队列还在
            self.wlog("接管运行中的 %s（PID %d）" % (name, pid))
        elif os.path.exists(os.path.join(q["state"], name + ".rc")):
            self.running[q["key"]] = job
            self.finish(q["key"])
        elif local and alive(pid, ("gpu_queue",)) or alive(read_pid(os.path.join(q["state"], "lock")), ("gpu_queue",)):
            pass                                  # 旧 gpu_queue.sh 还在管这条队列
        else:
            os.remove(os.path.join(q["state"], "current"))
            if job.get("gang"):                   # 同伴按它停下重排
                self.gang_end(job, "stop")
            self.qlog(q, "中断 %s（进程已不在，未留退出码：多半是服务器重启），重新排队" % name)
            self.event("interrupt", "中断 %s（进程已不在，多半是服务器重启），重新排队" % name, name=name, project=q["project"])

    def rekey(self, q):
        """这条清单换了登记文件名、手上在跑的还记在旧名下时（11-06 前不带目录哈希的登记搬走再搬回来，搬回来的用 qslug，#95）：
        沿用那一项、改记到新名下，不再按新名接管一次（同一个任务盯两份，收尾时一份成功一份失败）。状态目录一条清单一个，按它认。"""
        st = os.path.normpath(q["state"])
        old = next((k for k, j in self.running.items() if k != q["key"] and j.get("q") and os.path.normpath(j["q"]["state"]) == st), None)
        if old and q["key"] not in self.running:
            self.running[q["key"]] = dict(self.running.pop(old), key=q["key"], q=q)
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
            q = load_queue({"_slug": qslug(root, jobs), "root": root, "jobs": jobs, "project": j["project"], "stop": j.get("stop")})
            cur = read_current(q["state"])
            if cur and cur[:2] == (j["name"], j["pid"]) and cur[3] in (None, self.host):
                self.adopt(q)

    # ---- 单元申请 ----
    def slot_reqs(self):
        """容器本地 SLOTS 目录里的申请：已放行的接管（worker 重启后），申请方没了的清掉，其余是待放行的请求。"""
        out = []
        for f in sorted(glob.glob(os.path.join(SLOTS, "*.req"))):
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
            req = new_req("slot", key, r.get("project") or "?", r.get("name") or rid, id=rid, pid=pid, ngpu=int(r.get("gpus", 1)),
                          vram=float(r.get("vram") or 0), ram=float(r.get("ram") or 0), tag=r.get("tag"), excl=r.get("excl"),
                          stop=r.get("stop") or "term", cmd=r.get("cmd", ""), log=r.get("log"), t=r.get("t") or time.time(), est_min=r.get("est_min"),
                          cpus=r.get("cpus"), io=r.get("io"))
            if cmdline(pid) is None:              # 申请方已经没了（含放行后跑完、崩溃）
                for ext in (".req", ".ok", ".why"):
                    if os.path.exists(base + ext):
                        os.remove(base + ext)
                continue
            ok = read(base + ".ok")
            if ok:                                # 上一个 worker 放行过：接着盯
                old = self.prev.get((None, None, req["name"], pid), {})
                try:
                    gpus = json.loads(ok).get("gpus", [])
                except ValueError:
                    gpus = []
                self.running[key] = dict(req, t0=os.path.getmtime(base + ".ok"), gpus=gpus, use=old.get("use") or {},
                                         est=old.get("est") or estimate(self.hist, req), pstart=pstart(pid), io_sus=old.get("io_sus"),
                                         cpu_fit=old.get("cpu_fit"))
                continue
            out.append(req)
        return out

    # ---- 用量 ----
    def sample(self, queues):
        """主机现状与各任务用量：内存（任务进程 PSS 之和）、核数、读写、显存（按卡），以及运行期间同卡、同机还有谁。
        步骤：读主机（host_info、压力 press）→ 认旧 gpu_queue.sh 的任务（ext_jobs）→ 各任务的进程（job_procs）→ GPU 进程归属（gpu_owners）
        → 各卡利用率窗口（util_window）→ 逐任务量用量（measure）→ labmon 之外的用量、作废、借来主机的几项判定 → 内存闸门事件（mem_events）。"""
        t = time.time()
        procs, gpus, apps = scan_procs(), gpu_table(), gpu_procs()
        h = host_info(gpus, DATA_ROOT)
        h["press"] = self.press(t)
        self.slow = h["press"]["nfs"] if (h["press"]["nfs"] or {}).get("slow") else None
        if h["cpu_ns"] is not None:
            if self.cpu_prev and t > self.cpu_prev[0]:
                h["cpu_used"] = round(max(0.0, (h["cpu_ns"] - self.cpu_prev[1]) / 1e9 / (t - self.cpu_prev[0])), 2)
            self.cpu_prev = (t, h["cpu_ns"])
        self.ext = self.ext_jobs(queues)
        jobs = list(self.running.values()) + list(self.ext.values())
        mine = job_procs(procs, jobs)
        gp = gpu_owners(apps, procs, {pid: j for j in jobs for pid in mine[id(j)]}, self.ka_full if self.guest() else None)
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
        roots.update({p: v[1] for p, v in self.ka_roots.items() if p not in seen and p in procs and v[0] is not None and pstart(p) == v[0]})
        self.ka_roots = {p: (pstart(p), x) for p, x in roots.items() if p in procs}
        ka_pids = proc_tree(procs, roots)
        ka_ram = sum(mem_of(p) for p in ka_pids) / GIB
        ka_cpu = self.ka_cores(procs, ka_pids, t, "all")
        # 停不了的那几棵另算核数（cpu_room 另扣）：各棵保活树（按根的 PID 与启动时刻）分开记累计，归类只决定加进哪一份。
        # 改类那一轮照常按这一段求差（合成一份记时，刚归进来的树会把启动以来的 CPU 一起算进来）；一棵退出不丢别的几棵这一段的核数；
        # 只看 /proc 的累计，不掺 labmon 之外的总量（那是容器用量减任务，有测量误差、会为负），各棵都不小于 0。新冒出来的树第一轮为 0（ka_cores）
        # 按启动时刻遍历（祖先一定在子孙前面）：嵌套的保活根（父、子都有 GPU 上下文）里父进程漏认一轮、补回来排在后面时，子树也不挪到子的键上
        own, ka_x = set(), 0.0
        for p, x in sorted(roots.items(), key=lambda r: ((self.ka_roots.get(r[0]) or (0,))[0] or 0, r[0])):
            tree = [q for q in proc_tree(procs, [p]) if q not in own]
            own.update(tree)
            c = self.ka_cores(procs, tree, t, (p, self.ka_roots[p][0])) if p in self.ka_roots else 0.0
            ka_x += c if x else 0.0
        live = {(p, v[0]) for p, v in self.ka_roots.items()}
        self.ka_ticks = {k: v for k, v in self.ka_ticks.items() if k == "all" or k in live}
        self.cpu_n = self.cpu_n + 1 if extra is not None and extra - ka_cpu > VOID_CPU else 0   # 保活不算 labmon 之外的零碎占用
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
        self.gprocs = [dict({k: v for k, v in g.items() if k != "_j"}, **({"t0": round(started_at(g["pid"]) or 0) or None}
                                                                         if g["pid"] and (g["owner"] == "keepalive" or g["ka"]) else {})) for g in gp]
        self.mem_events(m)

    def press(self, t):
        """主机的 CPU 与 IO 压力（状态的 info.press）：近 PRESS_WIN 秒的计数差（press_rates）、PSI、共享盘延迟的基线（io_base）。
        每 PRESS_EVERY 秒往 press.jsonl 记一行这段时间的聚合值（共享盘上本机的状态目录，容器重启不丢，留 14 天）。
        worker 刚启动的第一轮没有上一份计数，速率为空。准入与推演不看它；共享盘慢（nfs 的 slow）时让路与读写拥塞嫌疑用它。"""
        if self.pdev is None and t - self.pdev_t >= PRESS_EVERY:
            self.pdev, self.pdev_t = local_dev(), t
        raw = press_raw(self.pdev, t)
        self.pwin.append(raw)
        while len(self.pwin) > 2 and t - self.pwin[1]["t"] >= PRESS_WIN:   # 起点留最近一份满 PRESS_WIN 秒的
            self.pwin.pop(0)
        if self.plast is None:
            self.plast = raw
        elif t - self.plast["t"] >= PRESS_EVERY:
            row = dict(press_rates(self.plast, raw), t=round(t))
            self.prows = [r for r in self.prows if r["t"] >= t - PRESS_KEEP_S] + [row]
            self.plast = raw
            try:              # 写不进只少一行历史（基线照用内存里的），不拖垮这一轮采样；下个 PRESS_EVERY 再试，连续失败只记一次日志
                append_jsonl(self.p.press, row, PRESS_KEEP_S)
                self.pfail = False
            except OSError as e:
                if not self.pfail:
                    self.wlog("press.jsonl 写不进（%s）：压力历史先只留在内存，每 %d 秒再试" % (e, PRESS_EVERY))
                self.pfail = True
        r = press_rates(self.pwin[0], raw) if len(self.pwin) > 1 else dict.fromkeys(("thr", "ms", "ops", "mib", "busy", "await"))
        self.thr_since = thr_step(self.thr_since, r["thr"], t)
        base, n = io_base(self.prows, t)
        nf = {"path": DATA_ROOT, "addr": raw["nfs"]["addr"], "ms": r["ms"], "ops": r["ops"], "mib": r["mib"], "base": base, "n": n,
              "need": IO_BASE_N, "floor": IO_SLOW_MS, "min_ops": IO_MIN_OPS} if raw["nfs"] else None
        if nf:
            nf["slow"] = nfs_slow(nf)
        return {"thr": r["thr"], "psi": raw["psi"], "nfs": nf,
                "disk": {"dev": raw["disk"]["name"], "busy": r["busy"], "await": r["await"]} if raw["disk"] else None}

    def cpu_hints(self, cap, util):
        """运行中任务开跑核数给少、给多（cpu_fit，#46）：同一次运行一旦判出来就留到跑完（j 的 cpu_fit，写进状态、接管时找回）。
        测量在门槛两侧来回时不反复出现、消失：看板的 cpu_low:、cpu_high: 只在条件出现时叫醒一次，消失再出现会再叫醒（waits._new_alerts）。
        fit 开车道（gpu_slack）要的是此刻的样子，照旧现算，不用这份。"""
        for j in self.running.values():
            if not j.get("cpu_fit"):
                j["cpu_fit"] = cpu_fit(j, cap, util)

    def throttle(self, now):
        """状态的 throttle（#39）：这一段 CPU 限流（thr_step）的起点 since、已持续 for 秒、此刻的 thr、持续满 THR_HOLD_S 的 alert，
        超核的任务 over [{project, name, cores, give}]（cpu_over；give 是开跑时导出的 LABMON_CPUS，按核数从多到少；占位任务不算），
        labmon 之外此刻占的核 other（不含保活，含旧 gpu_queue.sh 的任务，同 cpu_room；算不出为 None）。没在限流为 None。看板的告警与 fit 都只读它，门槛只在这里。"""
        if self.thr_since is None:
            return None
        over = []
        for j in self.running.values():
            if is_hold(j["name"]) or not j.get("est"):
                continue
            give = self.cpus_for(j)
            c = cpu_over(j.get("use") or {}, give, now)
            if c is not None:
                over.append({"project": j.get("project"), "name": j["name"], "cores": round(c, 1), "give": give})
        oc = self.other["cpu"]
        if oc is not None:                    # 旧 gpu_queue.sh 的任务不记账，此刻用的核算 labmon 之外（同 cpu_room）
            oc += sum((j.get("use") or {}).get("cores") or 0.0 for j in self.ext.values())
        return {"since": round(self.thr_since), "for": round(now - self.thr_since), "thr": (self.info.get("press") or {}).get("thr"),
                "alert": now - self.thr_since >= THR_HOLD_S, "over": sorted(over, key=lambda o: -o["cores"]),
                "other": None if oc is None else round(oc, 1)}

    def io_yield(self, key, now):
        """共享盘慢（nfs_slow）时推迟一件读写共享盘的事（扫描 "scan"、本地盘扫描 "lscan"、拷本地依赖 (请求 key, 任务)）：返回推迟的说明；不慢、或这件事已连着推迟满
        IO_YIELD_MAX_S 时返回 None（满了就照做这一次、计时清零：一直慢时每 IO_YIELD_MAX_S 做一件，不会永远不扫、不拷）。开始推迟时记一次 worker.log。"""
        nf = self.slow
        if not nf:
            self.defer.pop(key, None)
            return None
        t0 = self.defer.setdefault(key, now)
        if now - t0 >= IO_YIELD_MAX_S:
            self.defer.pop(key)
            self.wlog("共享盘仍慢，已推迟满 %d 分钟，照做一次%s" % (IO_YIELD_MAX_S / 60, IO_WHAT.get(key) or "拷 %s 的本地依赖" % key[1]))
            return None
        why = "共享盘慢（此刻 %.0f ms/次，本机基线 %.0f）" % (nf["ms"], nf["base"])
        if t0 == now:
            self.wlog("%s：推迟%s" % (why, IO_WHAT.get(key) or "拷 %s 的本地依赖" % key[1]))
        return why

    def io_suspect(self, now):
        """读写拥塞嫌疑（只记录、不拦，#4 维护者定）：在跑的任务已跑过同类历史预计时长（duration 的 same）的 SLOW_RATIO 倍，
        同时共享盘慢（nfs_slow），记一条 io_suspect 事件；同一任务只记一次（io_sus，写进状态、接管时找回）。不改准入与推演。
        事件里列出同一物理机上在跑的别的任务（本机的与 peers 读的 peer_box）；共享盘的延迟与次数只用本机这一份，不把同机几个容器的同一份计数相加。"""
        nf = self.slow
        if not nf:
            return
        for j in list(self.running.values()):
            if j.get("io_sus") or is_hold(j["name"]):
                continue
            d = duration(self.hist, {"name": j["name"], "project": j.get("project"), "est_min": j.get("est_min"), "kind": j.get("kind", "queue")}, self.model(),
                         self.k_of(j))          # 按它自己的同卡并行数比：挤着跑的不和单跑的记录比（#86）
            if not d or not d["same"] or now - j["t0"] < SLOW_RATIO * d["min"] * 60:
                continue
            j["io_sus"] = round(now)
            box = [x["name"] for x in self.running.values() if x is not j and not is_hold(x["name"])] + ["%s 的 %s" % (h, n) for h, _, n in self.peer_box]
            self.event("io_suspect", "读写拥塞嫌疑：%s 已跑 %.0f 分钟，同类历史约 %.0f 分钟（%s）；共享盘此刻 %.0f ms/次（本机基线 %.0f），%.1f 次/秒；同一物理机上还在跑：%s" % (
                j["name"], (now - j["t0"]) / 60, d["min"], d["src"], nf["ms"], nf["base"], nf["ops"], "、".join(box[:6]) or "没有别的任务"),
                name=j["name"], project=j.get("project"), ms=nf["ms"], base=nf["base"], box=box[:12])

    def ext_jobs(self, queues):
        """旧 gpu_queue.sh 正在跑的任务（状态目录的 current 指着、lock 是 gpu_queue 的）：只统计用量，不管；沿用上一轮的记录（用量累计在里面）。"""
        ext = {}
        for q in queues:
            cur = None if q["key"] in self.running else read_current(q["state"])
            if not cur or not cur[1] or not alive(read_pid(os.path.join(q["state"], "lock")), ("gpu_queue",)):
                continue
            k = q["key"] + ":" + cur[0]
            ext[k] = self.ext.get(k) or {"name": cur[0], "pid": cur[1], "t0": cur[2], "q": q, "gpus": [], "use": {}, "ext": True,
                                         "project": q["project"], "excl": None, "cmd": job_cmd(q, cur[0]) or ""}
        return ext

    def util10(self, t):
        """各卡近 UTIL_WIN_S 的「在用时多高」{UUID: %}（不算保活在的那几轮；样本少于 3 个的不列，win_mean）：状态的 util10（fit 读它）、同机多开车道的门槛（local_cards）共用。"""
        return {k: round(v) for k, w in self.uwin.items() for v in [win_mean(w, t, UTIL_WIN_S)] if v is not None}

    def local_cards(self):
        """offers 的同机多开车道能往哪几张卡放（#43）：(卡号列表, None)，或 (None, 不出的原因)。与 fit 同一条规则（gpu_slack）：容器在限流不出；
        近 10 分钟利用率不低于 UTIL_FULL 的卡（吃满了）、利用率低于 UTIL_LOW 而上面在跑的被 CPU 卡住的卡（先加核）不放；还没有利用率样本的卡照放。"""
        now = time.time()
        u10, gs = self.util10(now), self.info.get("gpus") or []
        cards = [{"i": g["i"], "uuid": g["uuid"], "util": u10.get(g["uuid"])} for g in gs]
        sl = gpu_slack(cards, list(self.running.values()), self.throttle(now), cpu_cap(self.info.get("cpu_quota")))
        if sl["throttle"] is not None:
            return None, sl["throttle"] + "：先减线程，不在这台多开车道"
        ok = [c["i"] for c in cards if (c["util"] is None or c["util"] < UTIL_FULL) and c["uuid"] not in sl["bound"]]
        if ok or not cards:
            return ok or None, None
        if sl["bound"]:
            return None, "利用率低的卡上在跑的任务跑满了给它的核：先给它们加核（cpus=），不在这台多开车道"
        return None, "各卡近 10 分钟利用率都不低于 %d%%，不在这台多开车道" % UTIL_FULL

    def util_window(self, gpus, gp, t):
        """各卡利用率的两个窗口（#51），都按 win_mean 取平均：
        uwin「在用时多高」近 UTIL_WIN_S 秒，卡上有保活的那几轮不记（它只是占着卡；util10，fit 列出来看）；
        bwin「吃满程度」近 BUSY_WIN_S 秒，卡上只有保活（ka.sh 管的与停不了的）的那几轮记 0（gpu_busy，页面上的利用率）。"""
        ka_on = {g["uuid"] for g in gp if g["owner"] == "keepalive"}
        work = {g["uuid"] for g in gp if g["owner"] != "keepalive" and not g.get("ka")}
        only_ka = {g["uuid"] for g in gp if g["owner"] == "keepalive" or g.get("ka")} - work
        self.bka = {}
        for g in gpus:
            w = self.uwin.setdefault(g["uuid"], [])
            b = self.bwin.setdefault(g["uuid"], [])
            if g.get("util") is not None:
                if g["uuid"] not in ka_on:
                    w.append((t, g["util"]))
                b.append((t, 0 if g["uuid"] in only_ka else g["util"]))
                if g["uuid"] in only_ka:
                    self.bka[g["uuid"]] = g["util"]
            w[:] = [x for x in w if t - x[0] <= UTIL_WIN_S]
            b[:] = [x for x in b if t - x[0] <= BUSY_WIN_S]

    def busy_view(self, t):
        """状态的 gpu_busy：各卡「吃满程度」{now 此刻, m10 近 10 分钟, h1 近 1 小时, cov 1 小时窗口实际覆盖的秒数（worker 刚起时不满）,
        ka 此刻只有保活时保活顶出来的原值}。样本不够 3 个的平均为 None。"""
        out = {}
        for k, b in self.bwin.items():
            if not b:
                continue
            out[k] = {"now": b[-1][1], "m10": win_mean(b, t, UTIL_WIN_S), "h1": win_mean(b, t, BUSY_WIN_S), "cov": round(t - b[0][0]),
                      **({"ka": self.bka[k]} if k in self.bka else {})}
        return out

    def measure(self, j, pids, procs, gp, util, ovram, jobs, t):
        """量一个任务这一轮的用量（写进 j["use"]）：内存（PSS 之和）与峰值、核数与累计 CPU、读写（进程树 /proc/<pid>/io 之和：此刻 MiB/s、
        累计字节 rd_b / wr_b）、核数与读写的 1 分钟峰值（PEAK_WIN 秒一个窗口）、近 CPU_WARM_S 内各 1 分钟窗口的核数 pks [[窗口结束时刻, 核数]]
        与从何时起量它的 pk0（CPU 记账的回收按它们，cpu_warm、Worker.acct）、同卡并行数 conc、显存（按卡）与峰值、所用卡的平均利用率，
        以及运行期间同卡、同机还有谁（with_gpu、with_host）、所用的卡上有没有过 labmon 之外的进程（owner_gpu）。返回此刻所用的卡。
        CPU（cpu_since）与读写都是累计计数求差：子进程被回收时并进父进程，求和不重不漏；回收者不在树里时那一轮差值为负，按 0 算。"""
        u = j.setdefault("use", {})
        ram = sum(mem_of(p) for p in pids) / GIB
        ticks = sum(procs[p]["ticks"] for p in pids)
        io = [proc_io(p) for p in pids]
        rd, wr = sum(x[0] for x in io), sum(x[1] for x in io)
        vr = [g for g in gp if g["_j"] is j]
        vram, vb = sum(g["mb"] for g in vr), {}
        for g in vr:
            vb[g["uuid"]] = vb.get(g["uuid"], 0.0) + g["mb"]
        if j.get("ext") and vr:
            j["gpus"] = sorted(vb)
        on = set(j["gpus"]) | set(vb)
        if any(ovram.get(x) for x in on):
            j["owner_gpu"] = True             # 运行期间所用的卡上有过 labmon 之外的进程（借来的主机上据此判断 CUDA OOM 是不是被挤的）
        if u.get("t") and t > u["t"]:
            dc = cpu_since(ticks, u.get("ticks"))
            u["cores"] = round(dc / (t - u["t"]), 2)
            u["cpu_s"], u["wall_s"] = u.get("cpu_s", 0) + dc, u.get("wall_s", 0) + t - u["t"]
            drd, dwr = max(0, rd - u.get("rchar", rd)), max(0, wr - u.get("wchar", wr))
            u["rd"], u["wr"] = round(drd / (t - u["t"]) / MIB, 2), round(dwr / (t - u["t"]) / MIB, 2)
            u["rd_b"], u["wr_b"] = u.get("rd_b", 0) + drd, u.get("wr_b", 0) + dwr
            pw = u.get("pw")                  # 峰值窗口的起点 [时刻, 累计 CPU 秒, 累计读, 累计写]
            if pw and t - pw[0] >= PEAK_WIN:
                w = t - pw[0]
                for k, v in (("cores_peak", (u["cpu_s"] - pw[1]) / w), ("rd_peak", (u["rd_b"] - pw[2]) / w / MIB), ("wr_peak", (u["wr_b"] - pw[3]) / w / MIB)):
                    u[k] = round(max(v, u.get(k) or 0), 2)
                # 近 CPU_WARM_S 内各 1 分钟窗口的核数 [[窗口结束时刻, 核数]]：CPU 记账的回收（Worker.acct）按它
                u["pks"] = [x for x in u.get("pks") or [] if t - x[0] < CPU_WARM_S] + [[round(t), round((u["cpu_s"] - pw[1]) / w, 2)]]
            if not pw or t - pw[0] >= PEAK_WIN:
                u["pw"] = [t, u["cpu_s"], u["rd_b"], u["wr_b"]]
            if on:                            # 同卡平均几条 labmon 任务（含自己，按时间加权；多卡取各卡平均）：duration 按它挑同类记录（#67）
                k = card_conc(j, on, jobs)
                u["conc_s"], u["conc_w"] = u.get("conc_s", 0) + k * (t - u["t"]), u.get("conc_w", 0) + t - u["t"]
                u["conc"] = round(u["conc_s"] / u["conc_w"], 2)
            ut = [util[g] for g in on if util.get(g) is not None]
            if ut:                            # 所用卡的平均利用率（同卡有别人时是合计，记账时据 with_gpu 区分）
                u["util_s"], u["util_wall"] = u.get("util_s", 0) + sum(ut) / len(ut) * (t - u["t"]), u.get("util_wall", 0) + t - u["t"]
                u["util_mean"] = round(u["util_s"] / u["util_wall"])
        u.setdefault("pk0", round(t))         # 从何时起量窗口核数（pks）：开跑的就是开跑时，接管旧 worker 的从接管起，满 CPU_WARM_S 才回收
        u.update(t=t, ticks=ticks, rchar=rd, wchar=wr, nproc=len(pids), ram=round(ram, 2), ram_peak=round(max(ram, u.get("ram_peak") or 0), 2),
                 vram=vram, vram_peak=max(vram, u.get("vram_peak") or 0), vram_by=vb)
        card, host = (sum(intruders(s, on, jobs, gp, None, j), []) for s in ("gpu", "host"))
        u["with_host"] = sorted(set(u.get("with_host", [])) | set(host))[:8]
        u["with_gpu"] = sorted(set(u.get("with_gpu", [])) | set(card))[:8]
        return on

    def mem_events(self, m):
        """内存闸门（收不回的内存到上限的 GATE；页缓存内核会先回收，不算）与撞上限次数：变化时记事件。"""
        if not m.get("limit"):
            return
        on = m["anon"] / m["limit"] >= GATE
        if on != self.gate:
            self.event("gate", "内存闸门%s：收不回的内存 %.1f / %.1f GiB" % ("关闭，停止放行" if on else "打开，恢复放行", m["anon"], m["limit"]))
            self.gate = on
        fc = m.get("failcnt")
        if fc is not None and self.failcnt is not None and fc > self.failcnt:
            self.event("failcnt", "容器内存撞上限 %d 次（累计 %d）" % (fc - self.failcnt, fc))
        self.failcnt = fc

    def gpu_now(self):
        """停保活之后此刻的 GPU 进程（放行独占任务前看）：归属沿用这一轮 sample 认的，之后才冒出来的算 labmon 之外的。"""
        known = {g["hpid"]: g for g in self.gp}
        return [known.get(hp) or {"hpid": hp, "pid": None, "mb": mb, "uuid": uu, "comm": None, "owner": "other", "job": None, "_j": None}
                for hp, mb, uu in gpu_procs()]

    def ka_cores(self, procs, pids, t, tree="all"):
        """保活进程树这一段占的核数：整棵树的累计 CPU 之和求差（cpu_since，同 measure）÷ 间隔，第一轮为 0。
        tree 分开记上一轮的累计："all" 所有保活合起来（从 labmon 之外的里扣，没有保活的轮次按 0 记）；(pid, 启动时刻) 是一棵保活树（sample 按它求停不了的那几棵）。
        保活上一轮没认出来、这一轮才进树时，它启动以来的 CPU 一起扣掉（多扣、偏宽），正好抵掉上一轮算到对方头上的那份，不会连着两轮判超出；
        刚停的保活这一段用掉的核没法扣（进程已不在），算到 labmon 之外，借来的主机上由 guest_scan 连着两轮才判 CPU 超出挡住这一轮尖峰。"""
        ticks = sum(procs[p]["ticks"] for p in pids)
        prev, self.ka_ticks[tree] = self.ka_ticks.get(tree), (t, ticks)
        return cpu_since(ticks, prev[1]) / (t - prev[0]) if prev and t > prev[0] else 0.0

    def other_cpu(self, jobs, h, t):
        """labmon 之外的进程此刻占的核数：容器 CPU 用量 − 各任务 − worker 自己；算不出为 None。"""
        c = os.times()
        own = None
        if self.own_cpu and t > self.own_cpu[0]:
            own = (c.user + c.system - self.own_cpu[1]) / (t - self.own_cpu[0])
        self.own_cpu = (t, c.user + c.system)
        cores = [(o.get("use") or {}).get("cores") for o in jobs]
        return h["cpu_used"] - sum(cores) - own if h.get("cpu_used") is not None and own is not None and None not in cores else None

    def void_check(self, jobs, cards_on, gp, extra):
        """独占任务运行中被打扰（intruders：同卡、整机独占时同机有别的，或 labmon 之外占了 VOID_CPU 核以上）连着两轮就作废。
        cards_on：各任务所用的卡；extra：other_cpu。借来的主机上正推迟暂停看门狗（ka_wait，还没满 LOCK_WAIT_S，#38）时 GPU 保活不算：
        推迟一般只多一两轮，sample 里先于 keepalive 跑，第一轮只计 1 次、第二轮起豁免；满 LOCK_WAIT_S 还停不下来的照旧算。"""
        if self.ka_wait and time.time() - self.ka_wait["t0"] < LOCK_WAIT_S:
            gp = [g for g in gp if g["owner"] != "keepalive"]
        for j in list(self.running.values()):
            if not j.get("excl") or j.get("stopping") or is_hold(j["name"]) or id(j) not in cards_on:
                continue
            hit = sum(intruders(j["excl"], cards_on[id(j)], jobs, gp, extra, j), [])
            j["void_n"] = j.get("void_n", 0) + 1 if hit else 0
            if j["void_n"] >= 2:
                self.void_job(j, "独占%s期间有 %s" % ({"gpu": "卡", "host": "整机"}[j["excl"]], "、".join(hit[:3])))

    # ---- 主循环 ----
    def registry(self):
        """这一轮交给 tick 的登记队列。坏了的登记文件（不是 JSON、缺 root / jobs）跳过并记一次事件，不让一个坏文件拖垮整个 worker。
        同一条清单只归一台（#95）：别的在用主机（peers 的 peer_st：worker 在、没 stop 过）也登记着它、登记文件比本机的新（共享盘上的修改时间，
        各台同一个时钟；一样新按主机 id 大的），这台不碰它（不接管、不放行，状态里不列，看板的车道与管理抽屉随之只在那台），变了才记一次事件。
        worker add 会把别处的登记搬走，两台都登记着的只有旧版留下的或两处同时登记的；那台不在了（归还、worker stop、状态过期）这台照常跑。
        这台手上已在跑的照样盯到跑完（running 不靠登记）。第一轮也先读一次别的主机（peers）再定。"""
        out, bad = registry(self.p.queues)
        for f, e in bad:
            if f not in self.bad_reg:
                self.bad_reg.add(f)
                self.event("worker", "登记文件坏了，跳过这条队列：%s（%r）" % (f, e))
        self.peers(time.time())
        keep, yielded = [], {}
        for q in out:
            try:
                mine = (os.stat(q["file"]).st_mtime, self.host)
            except OSError:                       # 这一轮刚被 rm、被 worker add 搬走
                continue
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
        return keep

    def publish(self, j):
        """跑成功了：needs= 写了来源、共享盘上还没有主副本的本地文件，拷上去当主副本（以后续租、换主机就拷，不用重算）。不删主副本。"""
        if not j.get("needs"):
            return
        try:
            pub = [m for c in check_needs(j, self.qenv(j["q"])) for m in c["publish"]]
            if pub:
                n, b = stage([(f, m) for m, f in pub])
                self.event("publish", "%s：%d 个本地依赖写成共享盘主副本（%.0f MB）" % (j["name"], n, b / 1e6), name=j["name"], project=j.get("project"))
        except OSError as x:
            self.wlog("写共享盘主副本失败（%s）：%s" % (j["name"], x))

    def missing(self, r):
        """缺本地文件又补不了（needs= 声明的，没有来源、命令也不会自己重算）：返回原因；没有为 None。"""
        if r["kind"] == "queue" and r.get("needs"):
            miss = [c["pat"] for c in check_needs(r, self.qenv(r["q"])) if not c["files"] and not c["heal"]]
            if miss:
                return "缺本地文件：%s（续租、容器重启会清空本地盘；补好文件，或在命令里先 [ -f 文件 ] || 重新生成）" % "、".join(miss[:3])
        return None

    def stalled(self, r, S):
        """与此刻 GPU 占用无关、等也等不来的原因：缺本地文件又补不了、labmon 的任务全停也放不下（nofit）、tag 到上限（tag_full）；没有为 None。
        prep 算一遍记在 r["stall"]，tick 据此不为这类请求停保活（否则卡空着、保活也不开）；place 里按放行时的快照再算。"""
        x = self.as_job(r)
        return self.missing(r) or nofit(x, S) or tag_full(x, S)

    def prep(self, r, now, limits):
        """把一个请求（new_req）按主机现状补全，admit、place 与推演才认：资源估计 est、挡住它的临时指令 held、没满足的依赖 dep、预计时长 dur（分钟）、
        等不来的原因 stall（按此刻的快照）、开始等的时刻 since 与放行时先记的卡·时 charge。返回 r。
        等了多久（屏障、launch 记的 wait）只算没被临时指令、依赖挡住的时间：挡住期间不算，解除后从那时重新起算
        （否则 pause 半小时一 resume，队首立刻满 wait_cap 成屏障，别的项目全让位）；单元从申请时刻起算。"""
        old = self.wsince.get(r["key"])
        r["est"] = estimate(self.hist, r)
        r["held"] = self.override_hit(r)
        # 依赖没满足（dep_block）、成组的还不能排队（gang_block）：同被临时指令挡住
        r["dep"] = None if r["held"] else self.dep_block(r, now) or self.gang_block(r, now)
        g = r.get("gang") and self.gsim.get((r["project"], r["gang"]))
        if g and r["dep"] and r["dep"][0] == "cycle":
            g["at"] = None                        # 成环：推演里同组的都不排（停在同伴上，同依赖；凑不齐、说不出何时的 gang_sync 已置空）
        elif g and not r["held"] and not r["dep"] and (r["key"], r["name"]) in g["local"]:
            g["local"][(r["key"], r["name"])] = now   # 这一轮在队首、能排队：组里别的成员留资源按它此刻就能算（gang_until）
        r["dur"] = self.req_dur(r)
        r["stall"] = self.stalled(r, self.snapshot(limits))
        fresh = not old or old[0] != r["name"] or old[2]            # 第一次见、换了任务，或上一轮被挡着
        r["since"] = now if r["held"] or r["dep"] else (r["t"] if r["kind"] == "slot" and not old else now) if fresh else old[1]
        r["charge"] = self.cards(r["ngpu"], r["est"]["vram"], r["excl"]) * (r["dur"] or 0.0) / 60
        return r

    def stage_bg(self, r, cp):
        """本地缺、共享盘有主副本的依赖在后台线程里拷（大文件要很久，不拖住这一轮 tick）：没在拷就起线程（共享盘慢时先推迟，io_yield）；
        拷完收结果（记事件，失败返回原因、下一轮重拷）。返回这一轮的等待原因，拷好了为 None。"""
        key, out = (r["key"], r["name"]), {}
        s = self.stagers.get(key)
        if s is None:
            slow = self.io_yield(key, time.time())
            if slow:
                return "%s，推迟拷 %d 个本地依赖（最多推迟 %d 分钟）" % (slow, len(cp), IO_YIELD_MAX_S / 60)

            def run():
                try:
                    out["r"] = stage(cp)
                except OSError as x:
                    out["err"] = str(x)
            th = threading.Thread(target=run, daemon=True)
            s = self.stagers[key] = {"th": th, "t0": time.time(), "n": len(cp), "out": out}
            th.start()
        if s["th"].is_alive():
            return "正在从共享盘拷 %d 个本地依赖（已 %.0f 秒）" % (s["n"], time.time() - s["t0"])
        self.stagers.pop(key)
        if "err" in s["out"]:
            return "从共享盘拷本地依赖失败：%s" % s["out"]["err"]
        n, b = s["out"]["r"]
        self.event("stage", "%s：从共享盘拷了 %d 个本地依赖（%.0f MB，%.0f 秒）" % (r["name"], n, b / 1e6, time.time() - s["t0"]),
                   name=r["name"], project=r.get("project"))
        return None

    def place(self, r, S, launch=None):
        """真放行：按快照 S 调准入核心 place；能放就放（launch）并返回 None，否则返回 (等待原因, 是否卡在资源上)。
        核心之外只有真放行要做的：缺本地文件（miss）、在看资源前拷本地依赖与内存闸门（pre）。原因代码（快照的 code）记进 r["code"]，
        note_waits 据此算一直因 CPU 被挡了多久。"""
        x = dict(self.as_job(r), miss=self.missing(r))
        r["miss"] = x["miss"] is not None            # 缺本地文件又补不了：等待项带着它（状态里的 miss），看板按字段认，不按文字

        def pre():
            if r["kind"] == "queue" and r.get("needs"):
                cp = [m for c in check_needs(r, self.qenv(r["q"])) for m in c["copy"]]
                why = (cp or (r["key"], r["name"]) in self.stagers) and self.stage_bg(r, cp)   # 拷完的那一轮 cp 已空，也要收结果
                if why:
                    return why
            if self.gate:
                m = self.info["mem"]
                return "内存闸门：收不回的内存 %.0f / %.0f GiB 超过 %d%%，暂停放行" % (m["anon"], m["limit"], GATE * 100)
            return None
        cards, why, res = place(x, S, time.time(), pre)
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
            self.watch(time.time())               # 借来的主机：找看门狗（在停保活之前，看得到是谁拉起的）
        except Exception as e:  # noqa: BLE001 — 统计出错不能拖垮调度
            if self.noted.get("_sample") != str(e):
                self.wlog("用量统计出错：%r" % e)
                self.noted["_sample"] = str(e)
        self.queue_durs(queues)                   # 截止前的准入（req_dur）也要用各清单的估时
        now = time.time()
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
        for r, why, pos in admit([r for r in reqs if not r["held"] and not r["dep"]], focus, pol, use, now, lambda r: self.place(r, self.snapshot(limits, gpus, apps)),
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
        """一轮开头：收尾不在了的任务（finish）、重读用量记录、停法以现在的登记为准、读临时指令，返回 (limits, 调度设置)。"""
        for k, j in list(self.running.items()):
            if not self.alive_job(j):
                self.finish(k)
        self.reload_hist()                        # 本机刚跑完的与别的主机新记的
        stops = {q["key"]: q["stop"] for q in queues}
        for k, j in self.running.items():        # 停法以现在的登记为准：重新登记 --stop 对正在跑的也生效（以前沿用放行时的，pause --now 停不下）
            j["stop"] = stops.get(k, j.get("stop"))
        self.ovr = self.load_overrides()
        try:
            limits = json.loads(read(self.p.limits, "{}") or "{}")
        except ValueError:
            limits = {}
        return limits, self.policy()

    def requests(self, queues, first):
        """各队列这一轮的请求：目录不在、旧 gpu_queue.sh 还在跑、已暂停的直接进等待表；清单跑空的标 empty（项目暂时没实验）并收掉 lock；
        其余取队首拼请求（new_req），加上单元申请（slot_reqs）。状态目录里有不是本机在盯的 current 时：别的主机放行的（run_elsewhere）不动、进等待表，
        其余接管（adopt：第一轮是上一个 worker 留下的；之后是那台被看板标成已归还以后留下的）。返回 (等待表, 请求)。"""
        waiting, reqs = [], []
        self.peers(time.time())                   # 第一轮也要知道看板说哪些主机已归还（gone）
        elsewhere = {}
        if first:
            self.adopt_left(queues)
        for q in queues:
            if not os.path.isdir(q["root"]):
                waiting.append({"key": q["key"], "q": q, "name": None, "why": "目录不存在：" + q["root"]})
                continue
            os.makedirs(q["state"], exist_ok=True)
            os.makedirs(q["logs"], exist_ok=True)
            self.rekey(q)
            cur = None if q["key"] in self.running else read_current(q["state"])
            other = cur and run_elsewhere(self.host, q, cur, self.gone)
            if other:
                elsewhere[q["key"]] = (other, cur[0])
                if self.elsewhere.get(q["key"]) != (other, cur[0]):
                    self.event("worker", "清单 %s/%s 的 %s 是主机 %s 放行的，这台不接管、不重跑，等那台收尾" % (q["root"], q["jobs"], cur[0], other),
                               project=q["project"], root=q["root"], queue=q["jobs"], name=cur[0])
                waiting.append({"key": q["key"], "q": q, "name": None,
                                "why": "主机 %s 放行的 %s 还没收尾（这条清单也登记在那台），等那台跑完；那台已不在、任务确定没了，删掉状态目录里的 current 这台就接着跑" % (other, cur[0])})
                continue
            if cur:
                self.adopt(q)
            if q["key"] in self.running:
                continue
            lock = read_pid(os.path.join(q["state"], "lock"))
            if alive(lock, ("gpu_queue",)):
                waiting.append({"key": q["key"], "q": q, "name": None, "why": "旧 gpu_queue.sh（PID %d）还在跑这条队列" % lock})
                continue
            if os.path.exists(os.path.join(q["state"], "paused")):
                waiting.append({"key": q["key"], "q": q, "name": None, "why": "已暂停（状态目录有 paused）"})
                continue
            job = next_job(q)
            if job is None:
                q["empty"] = True                 # 空着：清单里没有待办（项目暂时没实验），状态里标出来，别的项目不当它在抢卡
                if q["key"] in self.active:
                    self.qlog(q, "队列结束（清单中已无待办任务）")
                    self.event("qend", "队列结束：%s 清单中已无待办任务" % q["jobs"], project=q["project"], root=q["root"], queue=q["jobs"])
                    self.active.discard(q["key"])
                    if read(os.path.join(q["state"], "lock")) == str(os.getpid()):
                        os.remove(os.path.join(q["state"], "lock"))
                continue
            reqs.append(queue_req(q, job))
        self.elsewhere = elsewhere
        return waiting, reqs + self.slot_reqs()

    def gpu_state(self, reqs, now=None):
        """放行前的 GPU 现状：先按需要停保活，再查各卡空闲（有要卡的请求时）与此刻的 GPU 进程（有独占请求时）；清掉不再等的独占留卡与成组留的资源。
        被临时指令挡住的（暂停的项目、drain、续租截止、只给别人用）、依赖没满足的与等不来的（stall：放不下、缺文件、tag 到上限）不会放行，
        不为它们停保活，否则卡空着、保活也不开；成组的成员组里 GANG_SOON_S 内不能一起开跑的（gang_soon）同样不停；要看显存的请求照旧先停保活再看。
        独占任务的留卡与成组留的资源只给这一轮真去排队的：不再等了、被临时指令或依赖挡住的清掉（否则它的项目暂停期间那几张卡别人也用不上，
        留卡记的腾空时刻一过连回填都放不进）。返回 (各卡空闲, GPU 进程)。"""
        now = time.time() if now is None else now
        need_gpu = (any(j.get("ngpu", 1) > 0 or j.get("excl") for j in self.running.values())
                    or any((r["ngpu"] > 0 or r["excl"]) and not r["held"] and not r.get("dep") and not r["stall"] and self.gang_soon(r, now) for r in reqs))
        self.keepalive(need_gpu)                  # 先停保活再看显存，否则保活占的显存会让任务以为没卡
        gpus = query_gpus() if any(r["ngpu"] > 0 for r in reqs) else []
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
            cat = "cpu" if w.get("code") in CPU_CODES else re.sub(r"[\d.]+", "#", w["why"])
            if self.noted.get(w["key"]) != cat:
                q = w.get("q") or {}
                bad = (w.get("dep") or [None])[0] not in (None, "wait")          # 依赖等不来（失败、找不到、成环）：另记一类，记录页标红
                self.event("dep" if bad else "wait", "等待 %s%s：%s" % (q.get("jobs") or "单元", " 的 " + w["name"] if w["name"] else "", w["why"]),
                           name=w["name"], project=w.get("project") or q.get("project"), root=w.get("root") or q.get("root"),
                           queue=q.get("jobs"), why=w["why"])
                self.noted[w["key"]] = cat
                if w.get("kind") == "slot":
                    write(os.path.join(SLOTS, w["id"] + ".why"), w["why"])
        keys = {w["key"] for w in waiting}
        self.noted = {k: v for k, v in self.noted.items() if k in keys or k.startswith("_")}
        for w in waiting:                         # 同一个任务一直在等就沿用开始等的时刻
            old = self.wsince.get(w["key"])
            w.setdefault("since", old[1] if old and old[0] == w["name"] else now)
        self.wsince = {w["key"]: (w["name"], w["since"], bool(w.get("held") or w.get("dep"))) for w in waiting}
        old = self.cpu_block
        self.cpu_block = {w["key"]: (w["name"], old[w["key"]][1] if old.get(w["key"], (None,))[0] == w["name"] else now)
                          for w in waiting if w.get("code") in CPU_ALERT_CODES}
        for w in waiting:
            b = self.cpu_block.get(w["key"])
            w["cpu_wait"] = round(now - b[1]) if b and now - b[1] >= CPU_ALERT_S else None
        live = {(r["key"], r["name"]) for r in reqs}
        self.stagers = {k: v for k, v in self.stagers.items() if v["th"].is_alive() or k in live}
        self.defer = {k: v for k, v in self.defer.items() if k in ("scan", "lscan") or k in live}

    def scan_local(self, queues, now):
        """每 LOCAL_EVERY 秒扫一次待跑任务的本地依赖（续租前看会不会丢）；本地盘（LocalDisk）与拿着租约时的共享盘（Disk）各走一步。
        本地盘的扫法：根盘测得准（info.rootfs 的 ok）且知道开机时刻时扫整个根盘、只进开机以后动过的目录；借来的主机上只数借来以后的
        （guest 指令的时刻；借用中指令重下时刻会变，取这次借用里最早的那个）。测不准时只扫 labmon 目录。"""
        if now - self.local_t >= LOCAL_EVERY:
            self.local_t = now
            try:
                # 只留 40 行：补不回来的排前面，几个项目的任务多了也不会把要紧的挤掉
                self.local = {"t": now, "jobs": sorted(local_deps(queues, DATA_ROOT, self.qenv), key=lambda r: r["heal"])[:40]}
            except Exception as e:  # noqa: BLE001 — 统计出错不能拖垮调度
                self.wlog("统计本地依赖出错：%r" % e)
        def local():
            d = self.ldisk
            st, g = self.info.get("started"), self.guest()
            prune = round(st) if st and (self.info.get("rootfs") or {}).get("ok") else None
            d.setup(prune, max(prune, round(g.get("t") or prune)) if g and prune else prune, bool(g))
            d.step(now, self.io_yield)
            self.storage = dict(d.summary(), projects={p: q["project"] for q in queues for p in [project_dir(q["root"])] if p})
        self.disk_do("本地盘", local)
        if self.disk:
            def shared():
                # 项目根取本机登记的与别的主机状态里的（peers 读好的 peer_st，同一个 worker_home 才同一个扫描者）合起来（#53）
                self.disk.want({p for q in queues for p in [project_dir(q["root"])] if p}
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
            if k not in self.hinted and project_dir(h["root"]):
                self.disk.hint(project_dir(h["root"]))
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
        self.cpu_hints(S["cpu_cap"], u10)
        hits = [round(t) for t in self.ka_hits if t > now - FIGHT_S]   # 近 FIGHT_S 秒保活被拉起的时刻（kafight，重启后 load_prev 找回）
        write(self.p.status, json.dumps({
            "t": time.time(), "pid": os.getpid(), "host": self.host, "info": self.info, "gpu_procs": self.gprocs,
            "gate": {"on": self.gate, "at": GATE}, "limits": limits, "overrides": self.ovr, "local": self.local, "storage": self.storage,
            "voided": self.voided,
            "kafight": dict(self.fight, n=len(hits), hits=hits) if hits else None,
            "throttle": self.throttle(now),       # 这一段 CPU 限流与超核的任务（Worker.throttle）；没在限流为 None
            "ka_stop": self.ka_stop_t if self.ka_off else None,   # 要卡的这段里 worker 停着保活：停它的时刻（接管时找回，load_prev）
            "keepalive": {"ctl": bool(self.ka_reg), "why": self.ka_why},   # worker 管不管这台的保活（有 ka.sh）、没任务要卡却不拉起的原因（ka_lines 用，#61）
            "watchdogs": load_frozen(self.p),
            "guest": {"keep": self.keep(), "other": {"vram": self.other["vram"], "ram": self.other["ram"] and round(self.other["ram"], 2),
                                                     "cpu": self.other["cpu"] and round(self.other["cpu"], 2)},
                      "back": list(self.back.values()), "quiet": sorted(set(self.back_last) - set(self.back)), "tight": self.tight,
                      "hit": self.hit, "away": self.away_state(),
                      "block": self.guest_block()} if self.guest() else None,        # 整台不放新任务的原因（guest_block）：fit 照它挡整台
            "util10": u10,                        # 「在用时多高」（fit 用）
            "gpu_busy": self.busy_view(now),      # 「吃满程度」（只有保活时按 0；页面上的利用率，#51）
            "disk": self.disk.brief() if self.disk else None,
            # 按记账还能放多少：内存 GiB、各卡 MB（整卡任务记账超过卡时写 0）、CPU 核（cpu_cap 是记账上限：配额向下取整；读不到配额都为 None）
            "room": {"ram": ram_free(S), "margin": S["margin"], "gpus": {u: max(0.0, v) for u, v in card_free(S).items()},
                     "cpu": cpu_free(S), "cpu_cap": S["cpu_cap"], "other_cpu": S["cpu"],   # other_cpu：认得准的 labmon 之外的核数（fit 判整机独占，同 intruders）
                     "keep": {c["uuid"]: round(c["keep"]) for c in S["cards"] if c["keep"]},   # 各卡给对方留着的 MB（keep_left）：fit 的整卡封顶 card_need 照它
                     "owner": sorted(S["owner"])},        # 不放的卡（对方超出预留或刚回落、有停不了的保活）：fit 的快照照它（fit_snapshot）
            "queues": [{"root": q["root"], "jobs": q["jobs"], "gpus": q["gpus"], "project": q["project"], "stop": q["stop"],
                        "empty": q.get("empty", False), "added_t": q.get("added_t"), "dur": self.durs.get(q["key"], {})} for q in queues],
            "plan": self.plan,                    # 推演（forecast）：各队列与单元每个任务的预计开始、结束、用哪几张卡
            "todo": self.todo,                    # 还会跑的任务（list_state）：别的主机的依赖按它认谁还会跑、排在谁后面
            "dep_miss": [[p, n, int(t)] for (p, n), t in sorted(self.dep_miss.items())],   # 依赖等的、哪里都找不到的任务与起算时刻（dep_block；接管时找回，取整往前不往后）
            "offers": self.offer,                 # 能接别的主机上哪些任务、接来后几点跑完（offers）：看板汇总成挪法建议
            # 公平份额：生效的设置、这一轮开始时各项目衰减后的 GPU 用量（卡·时）、本机在跑的（别的主机读它算进用量）
            "share": {"wait_cap_min": pol["cap"] / 60, "half_life_h": pol["hl"] / 3600, "weight": pol["w"],
                      "use": {k: round(v, 3) for k, v in use.items()}, "run": self.own_runs(now)},
            "running": [dict(self.job_view(j), t0=j["t0"], vram=j.get("vram"), excl=j.get("excl"), est_min=j.get("est_min"),
                             oom0=j.get("oom0"), fc0=j.get("fc0"), owner_gpu=j.get("owner_gpu"), wait=j.get("wait"), cpus=j.get("cpus"), io=j.get("io"),
                             io_sus=j.get("io_sus"),   # 记过读写拥塞嫌疑的时刻（io_suspect；接管时找回，同一任务不重复记）
                             cpu_fit=j.get("cpu_fit"),   # 这一次运行判过的开跑核数给少、给多（cpu_hints，#46）：看板据此出只给实验会话的 cpu_low:、cpu_high:
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
                         "start": self.starts.get((w["key"], w["name"]))}   # 推演（plan）里它预计何时开始；推演里排不上的没有
                        for w in waiting]}, ensure_ascii=False, indent=1))

    def qenv(self, q):
        """队列的变量（读 .env 要起 bash，按修改时间缓存）。"""
        try:
            mt = fsig(q["env"]) if q["env"] else None
        except OSError:
            mt = None
        c = self.envc.get(q["key"])
        if not c or c[0] != mt:
            c = self.envc[q["key"]] = (mt, env_of(q))
        return c[1]

    def job_view(self, j):
        """运行中任务写进 status.json 的公共部分：命令与日志末尾供页面展开看，估计值与记账供核对准入。"""
        q = j.get("q")
        lg = os.path.join(q["logs"], j["name"] + ".log") if q else j.get("log")
        a = self.acct(j) if not j.get("ext") else None
        return {"kind": j.get("kind", "queue"), "queue": q["jobs"] if q else None, "root": q["root"] if q else None,
                "project": j.get("project"), "name": j["name"], "pid": j["pid"], "gpus": j["gpus"], "tag": j.get("tag"),
                "hold": is_hold(j["name"]),
                "stop": j.get("stop"), "use": j.get("use", {}), "cmd": j.get("cmd", ""), "est": j.get("est"),
                "acct": {"ram": round(a["ram"], 2), "vram": round(sum(a["vram"].values())), "cpu": round(a["cpu"], 2)} if a else None,
                "log": (os.path.relpath(lg, q["root"]) if q else lg) if lg else None, "tail": log_tail(lg) if lg else []}

    def run(self, replace=False):
        os.makedirs(self.p.queues, exist_ok=True)
        old = read_pid(self.p.pid)
        if replace and old != os.getpid() and alive(old, ("labmon_worker",)):
            os.kill(old, signal.SIGTERM)          # 升级：旧 worker 这一轮 tick 结束就退、任务不动，由本进程接管
            while alive(old, ("labmon_worker",)):   # 一直等到它退出：任何时候只能有一个 worker 在放行（本进程由 workerctl 放在后台，等多久都不卡命令行）
                time.sleep(0.5)
        if old != os.getpid() and alive(old, ("labmon_worker",)):
            print("worker 已在运行（PID %d）" % old)
            return 0
        write(self.p.pid, str(os.getpid()))
        self.load_prev()                          # 旧 worker 退出前最后一轮可能又放了任务、写了状态：按它最终的状态接管
        if os.path.exists(self.p.stopped):
            os.remove(self.p.stopped)
        with flock(self.p.events):
            ev = (read(self.p.events, "") or "").splitlines()
            if len(ev) > 4000:                    # 事件只留最近的
                write(self.p.events, "\n".join(ev[-2000:]) + "\n")
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        signal.signal(signal.SIGINT, lambda *_: stop.append(1))
        info = self.info or host_info([], DATA_ROOT)
        self.event("worker", "worker 启动 PID=%d 主机=%s（容器 %s 启动）" % (os.getpid(), self.host, now_str(info["started"]) if info.get("started") else "?"))
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
            for i in range(int(TICK * 2)):
                if stop:
                    break
                time.sleep(0.5)
                self.scan_chain(time.time())     # 扫描做完一件就接着下一件，不等下一拍
                if i % 4 == 3 and (self.new_slot() or self.gang_moved()):   # 单元申请、成组的同伴就绪或开跑约 2 秒内处理
                    break
        self.wlog("worker 退出（运行中的任务继续跑，下次启动时接管）")
        if read(self.p.pid) == str(os.getpid()):
            os.remove(self.p.pid)
        return 0

    def gang_moved(self):
        """gang_poll 出错不拖垮主循环：记一次 worker.log，当没变。"""
        try:
            return self.gang_poll()
        except Exception as e:  # noqa: BLE001
            if self.noted.get("_gang") != str(e):
                self.wlog("读成组同伴的状态出错：%r" % e)
                self.noted["_gang"] = str(e)
            return False

    def new_slot(self):
        ids = {os.path.basename(f)[:-4] for f in glob.glob(os.path.join(SLOTS, "*.req"))}
        new = ids - self.seen
        self.seen = ids
        return bool(new)


def card_share(ngpu, vram, excl, card, ncards):
    """一个任务占几张卡（公平份额按它记 GPU 用量）：独占卡算整卡，独占整机算这台全部的卡，同卡并行的按显存估计值（GiB）
    占卡总显存（card，GiB；不知道时算整卡）的比例；不用卡的为 0。"""
    if excl == "host":
        return float(max(ngpu, ncards))
    if not ngpu:
        return 0.0
    return float(ngpu) if excl == "gpu" or not card else ngpu * min(1.0, vram / card)


def usage(rows, t, hl):
    """各项目衰减后的 GPU 用量（卡·时）：rows [(项目, 卡数, 开始, 结束)]，t 之前跑的部分按半衰期 hl（秒）衰减；
    结束在 t 之后的（在跑的、刚放行的）把预计还要跑的整段照记（开跑先记一笔，免得几台主机同时偏向同一个项目）；结束为 None 的只记已跑的。"""
    k, out = math.log(2) / hl, {}
    for p, n, a, b in rows:
        b = t if b is None else b
        e = min(b, t)
        past = (math.exp(-k * (t - e)) - math.exp(-k * (t - a))) / k if e > a else 0.0
        out[p] = out.get(p, 0.0) + n * (past + max(0.0, b - max(a, t))) / 3600
    return out


def fits_before(t, dur_min, until):
    """回填与截止：从 t 起跑、预计 dur_min 分钟（再乘 DUR_MARGIN）能在 until 前跑完。不知道时长或 until 的不算。"""
    return until is not None and dur_min is not None and t + dur_min * DUR_MARGIN * 60 <= until


def gang_file(host):
    """主机 host 的 gang.json（成组成员此刻怎样，Worker.gang_publish 写）；读不到、写到一半的为 None。"""
    try:
        d = json.loads(read(os.path.join(HOME, "hosts", host, "gang.json"), "") or "")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def gang_bad(ms):
    """一组成员（Worker.gang_sync 的 mem 项 {(项目, 任务): {host, n…}}）人数对不上的说法：各行写的人数不一样、没写人数、凑不齐、多了；对得上为 None。"""
    ns = {v["n"] for v in ms.values()}
    n = next(iter(ns)) if len(ns) == 1 else None
    names = "、".join(sorted(x if v["host"] is None else "%s（%s）" % (x, v["host"]) for (_, x), v in ms.items()))
    return ("人数写得不一样（%s）" % "、".join(sorted(str(x) if x else "没写" for x in ns)) if len(ns) > 1
            else "没写人数（%s）" % names if not n
            else "凑不齐：要 %d 个，只见到 %d 个（%s）" % (n, len(ms), names) if len(ms) < n
            else "多了：要 %d 个，见到 %d 个（%s）；同一个组名同时只能有这么多行没跑完，几组就写几个组名" % (n, len(ms), names) if len(ms) > n
            else None)


def gang_cur(recs, k):
    """gang.json 的 members 里成员 k=(项目, 任务) 此刻的记录（wait、ready、run）；没有为 None。"""
    return next((e for e in recs or [] if (e.get("project"), e.get("name")) == k and e.get("state") in ("wait", "ready", "run")), None)


def gang_last(recs, project, name):
    """成员 (project, name) 最近一次结束的时刻（gang.json 里 done、fail、stop 记录的 t1，按它所在那台的时钟）；没有为 0。
    开跑时记进 gang_with 的第三项：同一次开跑按“谁开跑时已经看到了谁的哪次结束”认，不比两台的时刻（停下重排后几秒内又一起开跑也分得清，不怕时钟偏差）。"""
    return max((e.get("t1") or 0 for e in recs or [] if e.get("project") == project and e.get("name") == name
                and e.get("state") in ("done", "fail", "stop")), default=0)


def gang_peer(recs, project, name, t0, seen):
    """同伴在本机这一次开跑里怎样：fail、stop、done、run（几条都对得上时按这个先后）；还没跟着开跑为 None。
    算这一次的：开跑时刻与 t0 相差 GANG_JOIN_S 以内的 run，和本机开跑时没见过的结束（t1 晚于 seen，即开跑时记下的 gang_last；之前的结束是上一次的）。
    Worker.gang_watch 用：recs 是同伴那台 gang.json 的 members（本机的同伴取 gang_recs）。"""
    got = {e["state"] for e in recs or [] if e.get("project") == project and e.get("name") == name
           and (abs((e.get("t0") or 0) - t0) <= GANG_JOIN_S if e.get("state") == "run" else
                e.get("state") in ("done", "fail", "stop") and (e.get("t1") or 0) > seen)}
    return next((s for s in ("fail", "stop", "done", "run") if s in got), None)


def gang_until(S, x, t):
    """成组成员 x 留的资源要留到何时（组里预计何时能一起开跑）：别的主机上的成员里最晚的（S["gangs"] 的 at），本机别的成员已留好的（S["gholds"]）算 t、
    没留好的取它的预计（local：上次推演里最早何时放得下，这一轮在队首、没被挡住的为现在）；有一个说不出为 None（不留）。place 与 Worker.gang_soon 用。"""
    g = (S.get("gangs") or {}).get((x["project"], x["gang"]))
    if not g or g["at"] is None:
        return None
    ts, gh = [t, g["at"]], S.get("gholds") or {}
    for (k, n), eta in g["local"].items():
        if (k, n) != (x["key"], x["name"]):
            h = gh.get(k)
            ts.append(t if h and h["name"] == n else eta)
    return None if None in ts else max(ts)


def acct_job(x, cs, t, t1, cap):
    """开跑的（推演里）或留着资源的（成组成员，place）任务在快照 jobs 里的记账：显存按估计值记在选中的卡 cs 上（MB）、内存（GiB）、核数（cpu_give 封顶到 cap）。
    t、t1 是开始与预计结束（不知道为 None）。simulate 的 launch 与 place 的成组留资源共用。"""
    return {"key": x["key"], "name": x["name"], "project": x["project"], "t0": t, "t1": t1, "gpus": [c["uuid"] for c in cs],
            "vram": {c["uuid"]: x["vram"] * 1024 for c in cs}, "ram": x["ram"], "cpu": float(cpu_give(x["cpus"], cap)),
            "excl": x["excl"], "tag": x["tag"], "hold": x["hold"], "ngpu": x["ngpu"], "use": {}, "cards": x["cards"]}


def dep_keys(r):
    """一行的依赖（hints 的 deps、deps_any，可带 项目/ 前缀）→ [(项目, 任务, 失败也算)]：不带前缀的是这一行自己的项目（r["project"]）。"""
    k = lambda a: tuple(a.split("/", 1)) if "/" in a else (r["project"], a)
    return [k(a) + (False,) for a in r.get("deps") or []] + [k(a) + (True,) for a in r.get("deps_any") or []]


def dep_end(keys, t0, ends, dead=()):
    """依赖（dep_keys）预计何时都满足：等的任务预计结束里最晚的，不早于 t0。推演（simulate：何时能开始，占位任务何时放行）与
    运行中、排队的占位任务的预计时长（Worker.req_dur，run_end 经它）都调这一处；看板取推演里的结果，不另算。
    ends {(项目, 任务): 预计结束}（forecast 汇的）：已跑完的为 0，排上了但不知道何时结束的为 None；dead 是跑失败了的（afterany 的算已结束）。
    有一项满足不了或不知道（after= 的失败了、哪里都找不到、时长不明）为 None。"""
    xs = [t0]
    for p, n, anyok in keys:
        if (p, n) in dead and anyok:
            continue
        x = None if (p, n) in dead else ends.get((p, n))
        if x is None:
            return None
        xs.append(x)
    return max(xs)


def dep_names(keys, project):
    """依赖的文字：本项目的写任务名，别的项目写 项目/任务。"""
    return "、".join(n if p == project else "%s/%s" % (p, n) for p, n, _ in keys)


def need_mb(vram_gib):
    """一张卡上要有这么多空闲显存（MB）才放：估计值另留 1 GiB。准入、推演、放不下的判断与 fit 共用这一处。"""
    return (vram_gib + 1) * 1024


def card_need(x, c):
    """请求 x 在卡 c 上要有多少空闲显存（MB）才放（place、nofit 用）：need_mb；按历史估的（不是清单写的提示值）超过这张卡腾空后能给的
    （总显存 − 给对方留的 − 1 GiB，1 GiB 盖过驱动保留的几百 MB），按整卡算：卡上没有别的任务就放，不因为 ×1.1 的余量永远排不上
    （一次 OOM 的峰值就是整张卡）；配置没改的会再 OOM 一次、记失败。运行中的记账照估计值（整卡以上），这张卡上不再放别的。"""
    n = need_mb(x["vram"])
    return n if x["src"] == "提示值" or not c["total"] else min(n, c["total"] - c["keep"] - 1024)


def ram_need(x, S):
    """请求 x 放行要记多少内存（GiB）与原因里写的来源（place、nofit、fit 用；与 card_need、cpu_need 同理）：estimate 给的 ram；
    按历史估的（不是清单写的 ram=，含 OOM 后的下限）超过 labmon 全停时能给的减去余量（room − margin）时按这整份算：labmon 别的任务都没在跑就放，
    不因为翻倍后比容器还大永远“放不下”（#62）；来源里写明估计与封顶后的量。运行中的记账照估计值，跑着时不再放别的。读不到上限、整份不到 0 时不封顶。"""
    n, src = x["ram"], x["ram_src"]
    if src == "提示值" or S["room"] is None:
        return n, src
    m = S["room"] - S["margin"]
    return (n, src) if n <= m or m <= 0 else (m, "%s；估计 %.1f GiB，封顶为 %.1f GiB（labmon 全停时能给的减去余量）" % (src, n, m))


def ram_margin(limit_gib):
    """放行时容器内存另留的余量（GiB）：上限的 5%，至少 8。"""
    return max(8.0, 0.05 * limit_gib)


def card_free(S):
    """快照里各卡此刻还能给新任务的显存 {UUID: MB}：卡的 free 减去在跑任务的记账。"""
    out = {c["uuid"]: c["free"] for c in S["cards"]}
    for j in S["jobs"]:
        for u, mb in j["vram"].items():
            if u in out:
                out[u] -= mb
    return out


def ram_free(S):
    """快照里容器内存还能给新任务多少（GiB）：labmon 任务全停时的余量减去在跑任务的记账；读不到上限为 None。"""
    return None if S["room"] is None else S["room"] - sum(j["ram"] for j in S["jobs"])


def cpu_free(S):
    """快照里还能给新任务多少核（同 ram_free）：labmon 任务全停时还能给的（cpu_room）减去在跑任务的记账；读不到配额为 None（不记 CPU）。"""
    return None if S["cpu_room"] is None else S["cpu_room"] - sum(j["cpu"] for j in S["jobs"])


def cpu_need(x, S):
    """请求 x 放行要几个核与原因里写的来源（place、nofit、fit 用）：estimate 给的 cpus；按历史估的（不是 cpus= 写的）超过 labmon 全停时能给的
    （cpu_room，已扣 labmon 之外的与给对方留的），按这整份算，来源里写明估计与封顶后的核数（同 card_need：一次吃满配额的峰值 ×1.1 比配额还大，
    不封顶就永远排不上；没有别的 labmon 任务时照放）。写了 cpus=0 的（与占位任务）是 0：不记账，CPU 再紧也不挡它。"""
    n, src = x["cpus"], x["cpu_src"]
    if src == "提示值" or S["cpu_room"] is None:
        return n, src
    m = max(1, int(S["cpu_room"] + 1e-6))
    return (n, src) if n <= m else (m, "%s；估计 %d 核，封顶为 %d 核（labmon 全停时能给的）" % (src, n, m))


def cpu_held(S, cores, n):
    """因记账不够放不下时：原因代码与“核被谁记着”的说明（place、fit 用）。返回 (code, held 文字)。
    还没回收的在跑任务（开跑不满 CPU_WARM_S，记的是估计）记账比此刻实测多出的核（多 0.5 核以上的才列）加回去就够 n 核的是 cpu_est
    （记账挡住、实际空着，满 10 分钟会回收，要告警），否则 cpu（核真被用着，正常排队）。已回收的记账就是近期窗口核数的峰值，
    此刻在两次冒尖之间低于它也不算“记得多”（#86）。推演里开跑的任务没有实测，原因只要短的、也不告警，口径不影响。
    S 要 jobs[{cpu 记账核数, use}]、cpu_cap、cpu_room、kept。"""
    run = [j for j in S["jobs"] if j["cpu"] > 0]
    over = [j for j in run if (j.get("use") or {}).get("cores") is not None and not cpu_warm(j["use"])
            and j["cpu"] - j["use"]["cores"] >= 0.5]   # 还没量到核数的（刚开跑、推演里的）与已回收的不算
    spare = sum(j["cpu"] - j["use"]["cores"] for j in over)
    out = S["cpu_cap"] - S["cpu_room"]
    held = "：在跑 %d 个记 %g 核" % (len(run), round(sum(j["cpu"] for j in run), 1)) if run else ""
    if over:
        held += "，其中 %d 个记账比实测多（实测共 %.1f 核）" % (len(over), sum(j["use"]["cores"] for j in over))
    if out >= 0.1:
        held += "%slabmon 之外%s占 %.1f" % ("；" if held else "：", "与给对方的预留" if S["kept"] else "", out)
    return ("cpu_est" if over and cores + spare >= n - 1e-6 else "cpu"), held


def cpu_warm(u):
    """运行中任务的 CPU 记账已按窗口核数回收（#65）：从开始量窗口核数（use 的 pk0）起满 CPU_WARM_S、量到了 pks。
    Worker.acct（回收）与 cpu_held（只把没回收的算“记账比实测多”）共用这一个判断。"""
    return bool(u.get("pks") and u.get("t") and u.get("pk0") and u["t"] - u["pk0"] >= CPU_WARM_S)


def cpus_export(cpus, cap):
    """开跑时导出的 LABMON_CPUS（给这个任务的核数）：cpu_give 再至少 1。Worker.cpus_for 与判开跑核数给多给少（cpu_fit）共用。"""
    return max(1, cpu_give(cpus, cap))


def cpu_give(cpus, cap):
    """开跑后给任务的核数：估计（estimate 的 cpus）封顶到记账上限（cpu_cap，配额向下取整；读不到配额不封顶）。运行中的记账（Worker.acct、
    推演里开跑的）与导出的 LABMON_CPUS（再至少 1）都取它：历史是各主机一起看的，在核多的主机上量到的峰值可能比这台的配额还大，
    照原样导出等于又让任务按宿主机的核数开进程。"""
    return cpus if cap is None else min(cpus, cap)


def hold_until(S, cards, t):
    """为独占任务留着的卡（cards 为 None 是整台）预计何时腾空：上面 labmon 任务的预计结束取最晚的；这之前跑得完的任务可以回填。
    有一个时长不明的（t1 为 None）就不知道（None），不回填。占位任务与不用卡、不占 CPU 的任务不算。"""
    ts = [j["t1"] for j in S["jobs"] if not j["hold"] and not idle_job(j) and (cards is None or set(j["gpus"]) & set(cards))]
    return None if None in ts else max(ts, default=t)


# 放不了的原因：同一个判定两种说法。真放行写全（给看板的等待表与事件），推演只要一句短的（plan 里 stuck 的“一直排不上，…”）。
WHY = {
    "nofit_ram": ("放不下（要的比容器内存还大）",
                  "放不下：要内存 %(ram).0f GiB（%(src)s，另留 %(margin).0f），这台容器上限 %(limit).0f GiB%(taken)s，labmon 的任务全停也不够；换内存大的主机或拆小"),
    "nofit_gpu_count": ("放不下（GPU 卡数不足：要 %(ngpu)d 张，只有 %(count)d 张）",
                        "放不下：要 %(ngpu)d 张 GPU，%(available)s；%(advice)s或减少用卡数"),
    "nofit_vram_count": ("放不下（显存够的卡数不足：要 %(ngpu)d 张，只有 %(count)d 张）",
                         "放不下：要 %(ngpu)d 张 GPU，每张要显存 %(vram).0f GiB（%(src)s，另留 1 GiB），只有 %(count)d 张满足；换有足够显存和卡数的主机或拆小"),
    "nofit_vram": ("放不下（要的比卡的显存还大）",
                   "放不下：要每张卡显存 %(vram).0f GiB（%(src)s，另留 1 GiB）× %(ngpu)d 张，这台的卡最大 %(max).0f GiB；换显存大的主机或拆小"),
    "cut": ("截止前跑不完", "截止前跑不完"),
    "tag": ("tag %(tag)s 已到上限", "tag %(tag)s 已有 %(n)d 个在跑（上限 %(lim)d）"),
    "xhost_run": ("等独占整机的任务", "独占整机的 %(name)s 在跑"),
    "xhost_hold": ("等独占整机的任务", "主机留给等腾空的独占任务（%(note)s）"),
    "xhost_theirs": ("独占整机：等%(who)s用完", "独占整机：%(who)s在用（%(names)s），等用完；不为它留主机，别的任务照常放"),
    "xhost_wait": ("独占整机：等主机腾空", "独占整机：等主机腾空（还在跑：%(names)s）"),
    "ram": ("内存不够", "内存不够：要 %(ram).0f GiB（%(src)s，另留 %(margin).0f），按记账只剩 %(left).0f GiB"),
    "nofit_cpu": ("放不下（要的核数比容器配额还多）",
                  "放不下：要 %(cpus)d 核（%(src)s），这台容器配额只有 %(cap)d 核；换配额大的主机或少开进程"),
    "cpu_other": ("CPU 不够（labmon 之外占着）",
                  "CPU 不够：要 %(cpus)d 核（%(src)s），配额 %(cap)d 核里 labmon 之外与给对方的预留此刻占 %(busy).1f 核，labmon 的任务全停也不够；"
                  "先等它们退下，一直占着再换主机或少开进程"),
    "cpu": ("CPU 不够", "CPU 不够：要 %(cpus)d 核（%(src)s），按记账只剩 %(left).1f 核（配额 %(cap)d%(held)s）"),
    "cpu_est": ("CPU 不够（记账比实测多）", "CPU 不够：要 %(cpus)d 核（%(src)s），按记账只剩 %(left).1f 核（配额 %(cap)d%(held)s）；"
                "实际空着的够，是在跑的记账比实测多，开跑满 %(warm)d 分钟后按实测回收"),
    "xgpu_none": ("独占：没有可用的卡", "独占：没有可用的卡"),
    "xgpu_theirs": ("独占：等%(who)s用完", "独占：%(who)s在用卡（%(names)s），等用完；不为它留卡，别的任务照常放"),
    "xgpu_nofit": ("独占：没有放得下的卡", "独占：没有放得下的卡（要显存 %(vram).0f GiB，另留 1 GiB%(kept)s）"),
    "xgpu_wait": ("独占：等卡腾空", "独占：等卡 %(idx)s 腾空（还在跑：%(names)s）"),
    "no_gpu": ("看不到 GPU（nvidia-smi 无输出）", "看不到 GPU（nvidia-smi 无输出）"),
    "gang_hold": ("成组 %(gang)s：已留好资源，等一起开跑",
                  "成组 %(gang)s：资源已留好，等同组的一起开跑（预计 %(at)s）；这之前跑得完的任务照样用这份资源"),
    "gang_unk": ("成组 %(gang)s：同组有成员不知道何时能放行", "成组 %(gang)s：放得下，但同组有成员说不出何时能放行，先不留资源"),
    "vram": ("显存不够%(ka_s)s", "没有可用显存够 %(vram).0f GiB 的卡（%(src)s，另留 1 GiB%(kept)s）%(held)s%(ka)s%(owner)s"),
}


def why_text(code, brief, **kw):
    return WHY[code][0] % kw if brief else WHY[code][1] % kw


def why_of(S, code, **kw):
    """place 里给出一个原因：按快照要短的还是全的拼文字（why_text），同时把原因代码记进快照的 code
    （Worker.place 据此认“因 CPU 被挡”，不按文字认；每次 place 开头清掉，缺文件、pre 这类不经它的为 None）。"""
    S["code"] = code
    return why_text(code, S["brief"], **kw)


def nofit(x, S):
    """labmon 的任务全停也放不下（估计值加余量超过容器内存、卡数或总显存不够、或要的核数超过全停时能给的）：返回原因。
    这类请求每轮照样再试，但不算“卡在资源上”，不会成为不让越过的那一个，免得整台主机的所有项目都停住。"""
    ram, rsrc = ram_need(x, S)
    if S["room"] is not None and ram + S["margin"] > S["room"]:
        return why_of(S, "nofit_ram", ram=ram, src=rsrc, margin=S["margin"], limit=S["limit"],
                      taken="，labmon 之外与给对方的预留占 %.0f" % (S["limit"] - S["room"]) if S["limit"] - S["room"] >= 1 else "")
    if x["ngpu"] > 0:
        cards = [c for c in S["cards"] if c["total"]]
        cs = [c for c in cards if x["allowed"] is None or c["i"] in x["allowed"]]
        if cs:
            ngpu = x["ngpu"]
            if len(cs) < ngpu:
                if len(cards) < ngpu:
                    count, available, advice = len(cards), f"这台只检测到 {len(cards)} 张", "换有足够卡数的主机"
                else:
                    count = len(cs)
                    available = f"这台检测到 {len(cards)} 张，清单限制后只有 {count} 张可选"
                    advice = "调整清单的选卡范围"
                return why_of(S, "nofit_gpu_count", ngpu=ngpu, count=count, available=available, advice=advice)
            enough = sum(c["total"] >= card_need(x, c) for c in cs)
            if enough < ngpu:
                if enough:
                    return why_of(S, "nofit_vram_count", vram=x["vram"], src=x["src"], ngpu=ngpu, count=enough)
                return why_of(S, "nofit_vram", vram=x["vram"], src=x["src"], ngpu=ngpu, max=max(c["total"] for c in cs) / 1024)
    n, src = cpu_need(x, S) if S["cpu_room"] is not None else (0, None)
    if n > 0 and n > S["cpu_room"] + 1e-6:      # 要的比配额还多才是真放不下；没超配额、只是 labmon 之外与给对方的预留占着的另说（cpu_other：等它们退下）
        return why_of(S, "nofit_cpu" if n > S["cpu_cap"] else "cpu_other", cpus=n, src=src, cap=S["cpu_cap"], busy=S["cpu_cap"] - S["cpu_room"])
    return None


def tag_full(x, S):
    """同 tag 在跑的已到上限（limits.json 的 tags；占位任务不算）：返回原因。"""
    lim = S["tags"].get(x["tag"] or "")
    n = sum(j["tag"] == x["tag"] for j in S["jobs"] if not j["hold"])
    if lim is not None and n >= lim:
        return why_of(S, "tag", tag=x["tag"], n=n, lim=lim)
    return None


def place(x, S, t, pre=None):
    """放不放一个请求 x：真放行（Worker.place）与推演（simulate）都调这一个，返回同 _place。在准入核心 _place 外面只多成组（#180）这一层：
    - 成组成员（x 的 gang，S["gangs"] 里有它的组）留着的资源（S["gholds"]：真放行就是 Worker.gang_hold，跨轮留着）当在跑的任务扣（acct_job 的记账），
      x 预计在留到的时刻（until）之前跑得完的不扣（回填，同独占留卡）；同组别的成员留的总扣：它们要同时占着，彼此不算回填；
    - 成员自己放得下时不开跑，留下这份资源（选中的卡与记账、留到组里预计一起开跑的时刻 gang_until），返回 (None, 原因, False)：不算卡在资源上、不成屏障；
      推演里 S["gfirst"] 记下它最早何时放得下（Worker.gang_eta，写进 gang.json 给同伴）。组里有成员说不出何时能放行（until 为 None）的不留；放不下的放掉。
      一起开跑由 Worker.gang_commit（真放行）与 simulate（推演）在一轮 admit 之后看；
    - 两组在两台主机上互相留着对方要的资源时（各自先留了一个成员），按 (项目, 组名) 的先后，排在后面的组留的让给前面的组，两台判法一样，不会互相卡死。"""
    gh, gk = S.setdefault("gholds", {}), (x["project"], x["gang"]) if x.get("gang") else None
    mine = gh.get(x["key"])
    held = [h for k, h in gh.items() if k != x["key"] and (h["gang"] == gk or not fits_before(t, x["dur"], h["until"]))]
    until = gang_until(S, x, t) if gk else None

    def try_(hs):
        S2 = dict(S, jobs=S["jobs"] + [h["job"] for h in hs]) if hs else S
        got = _place(x, S2, t, pre)
        S["code"] = S2["code"]
        return got
    cs, why, res = try_(held)
    low = [h for h in held if gk and until is not None and h["gang"] != gk and h["gang"] > gk]
    if cs is None and res and low:
        got = try_([h for h in held if h not in low])
        if got[0] is not None:
            for h in low:
                gh.pop(h["key"], None)
            cs, why, res = got
    if not gk:
        return cs, why, res
    if cs is None:
        gh.pop(x["key"], None)
        return cs, why, res
    S.setdefault("gfirst", {}).setdefault((x["key"], x["name"]), t)
    if until is None:
        gh.pop(x["key"], None)
        return None, why_of(S, "gang_unk", gang=x["gang"]), False
    gh[x["key"]] = {"key": x["key"], "gang": gk, "name": x["name"], "x": x, "cards": cs, "until": until, "seen": t,
                    "t": mine["t"] if mine and mine["name"] == x["name"] else t,
                    "job": acct_job(x, cs, t, until + x["dur"] * 60 if x["dur"] is not None else None, S["cpu_cap"])}
    return None, why_of(S, "gang_hold", gang=x["gang"], at=now_str(until)[5:16]), False


def _place(x, S, t, pre=None):
    """准入核心：放不放一个请求 x（Worker.as_job / job_spec 的形状）：按资源快照 S 定，规则只有这一份，只经 place（外面一层是成组）调。
    返回 (给哪几张卡, 原因, 是否卡在资源上)：放行时卡是快照里的卡（不用卡为 []）、原因 None；不放时卡为 None。
    S（Worker.snapshot 从实时采样拼，simulate 在模拟的时间轴上维护；字段见 Worker.snapshot）：
    - cards [{i, uuid, total, free, keep, other, ka}]：total 总显存（放不下看它），keep 给对方留着、对方还没用上的，free 扣掉 keep 与
      labmon 之外的占用、还没扣在跑任务记账的空闲（记账在 jobs 里，card_free 再扣）；单位 MB；ka：这一轮卡上有借来主机的对方另起、ka.sh 停不了的保活
      （真放行经 guest_scan 记进 owner，推演由 sim_snapshot 照它挡，#45）；
    - room / margin / limit：labmon 任务全停时容器能放多少、另留的余量、上限（GiB；读不到上限都为 None；labmon 之外按收不回的内存算，不算页缓存）；
    - cpu_cap / cpu_room：CPU 记账上限（配额向下取整）、labmon 任务全停时还能给多少核（已扣 labmon 之外的与给对方留的；读不到配额都为 None，不记 CPU）；
    - jobs：labmon 在跑的 [{key, project, name, t0, t1（预计结束，不知道为 None）, gpus, vram {UUID: MB 记账}, ram（GiB 记账）, cpu（核，记账）, excl, tag, hold, ngpu, use, cards}]；
      ext：旧 gpu_queue.sh 的任务，只算打扰、不记账（用量已在 nvidia-smi 与容器用量里）；gp：GPU 进程（sample 认过归属）；cpu：labmon 之外占的核；
    - holds {请求 key: {cards: 留着的卡（整台为 ["*"]）, until: 预计腾空}}：独占任务轮到时在这里留卡，放行或不再等时清掉（真放行就是 excl_hold）；
    - tags：tag 上限；owner：借来的主机上对方超出预留（或刚回落）、有停不了的保活的卡，不放；who / kept：原因里怎么称呼对方、有没有扣给对方的预留；
    - cut：(截止时刻, 没估时的放不放)，截止前跑不完的不放；真放行里截止由 override_hit 在排队前挡住，这里 None；brief：原因只要短的；
    - code：place 写回的这一次的原因代码（WHY 的键，why_of 记；放行或缺文件、pre 时为 None）。
    pre：真放行在看资源前另要做的（拷本地依赖、内存闸门），返回原因或 None；推演不模拟这些，不传。
    独占任务轮到时先留住要腾空的卡（整机独占留住整台），记下预计何时腾空（hold_until）：别的任务预计在那之前跑完的照放（回填）；
    因为别的原因放不了（缺文件、放不下、截止、tag 上限、pre）时不留。
    挡着的是 labmon 之外的进程（借来的主机上多半是对方）时不留、也不算卡在资源上：留着它们也不会走，只会把别的任务一起堵住。"""
    w = lambda code, **kw: why_of(S, code, **kw)
    S["code"] = None
    if x["hold"] and not x["ngpu"]:
        return [], None, False
    if x["ngpu"] and any(card_need(x, c) < need_mb(x["vram"]) for c in S["cards"]):
        x = dict(x, src=x["src"] + "，超过整卡按整卡算")          # 原因里写明
    cut = S["cut"]
    why = (x.get("miss") or nofit(x, S)
           or (w("cut") if cut and (t >= cut[0] or (x["dur"] is None and not cut[1]) or (x["dur"] is not None and not fits_before(t, x["dur"], cut[0])))
               else None)
           or tag_full(x, S) or (pre() if pre else None))
    if why:                                   # 缺文件、放不下、截止前跑不完、tag 到上限、在拷本地依赖、内存闸门：走不到留卡这一步，留着的也放掉
        S["holds"].pop(x["key"], None)
        return None, why, False
    jobs = [j for j in S["jobs"] if not j["hold"]]
    rsv = {k: v for k, v in S["holds"].items() if k != x["key"] and not fits_before(t, x["dur"], v["until"])}   # 回填不了的留卡
    xh = next((j["name"] for j in jobs if j["excl"] == "host"), None)
    if xh:
        return None, w("xhost_run", name=xh), True
    until = [v["until"] for v in rsv.values() if v["cards"] == ["*"]]
    if until:
        return None, w("xhost_hold", note="预计 %s 腾空，这之前跑得完的才放" % now_str(max(until))[5:16] if None not in until
                       else "上面有任务不知道要跑多久，不回填"), True
    held = {u for v in rsv.values() for u in v["cards"] if u != "*"}
    blocked = held | S["owner"] | {u for j in jobs if j["excl"] == "gpu" for u in j["gpus"]}
    mine = [c for c in S["cards"] if (x["allowed"] is None or c["i"] in x["allowed"]) and c["uuid"] not in blocked]
    all_jobs = S["jobs"] + S["ext"]
    if x["excl"] == "host":
        ours, theirs = intruders("host", (), all_jobs, S["gp"], S["cpu"])
        if theirs:
            S["holds"].pop(x["key"], None)
            return None, w("xhost_theirs", who=S["who"], names="、".join(theirs[:3])), False
        if ours:
            S["holds"][x["key"]] = {"cards": ["*"], "until": hold_until(S, None, t)}
            return None, w("xhost_wait", names="、".join(ours)), True
        S["holds"].pop(x["key"], None)
    room_gib = ram_free(S)
    ram, rsrc = ram_need(x, S)
    if room_gib is not None and ram + S["margin"] > room_gib:
        return None, w("ram", ram=ram, src=rsrc, margin=S["margin"], left=max(0.0, room_gib)), True
    cores = cpu_free(S)
    n, src = cpu_need(x, S) if cores is not None else (0, None)
    if n > 0 and n > cores + 1e-6:            # 记账可能超过可放的（实测比估计多、labmon 之外的变多），这时 cpus=0 的照放
        code, held = cpu_held(S, cores, n)
        return None, w(code, held=held, cpus=n, src=src, left=max(0.0, cores), cap=S["cpu_cap"], warm=CPU_WARM_S // 60), True
    kept = "，已扣给对方的预留" if S["kept"] else ""
    if x["excl"] == "gpu" and x["ngpu"] > 0:
        if not mine:
            return None, w("xgpu_none"), True
        free = card_free(S)                                  # MB
        occ = {c["uuid"]: intruders("gpu", [c["uuid"]], all_jobs, S["gp"], None) for c in mine}
        # 候选：没有 labmon 之外的进程、扣掉给对方的预留也放得下；上面只有我们的任务时留着等腾空
        cand = [c for c in mine if not occ[c["uuid"]][1] and c["total"] - c["keep"] >= card_need(x, c)]
        keep = [u for u in (S["holds"].get(x["key"]) or {}).get("cards", []) if u in {c["uuid"] for c in cand}]
        empty = sorted((c for c in cand if not occ[c["uuid"]][0] and free[c["uuid"]] >= card_need(x, c)),
                       key=lambda c: (c["uuid"] not in keep, c["keep"], -free[c["uuid"]]))   # 先挑上一轮留的；多卡时先挑没给对方留的
        if len(empty) >= x["ngpu"]:
            S["holds"].pop(x["key"], None)
            return empty[:x["ngpu"]], None, False
        if len(cand) < x["ngpu"]:
            S["holds"].pop(x["key"], None)
            theirs = sorted({n for c in mine for n in occ[c["uuid"]][1]})
            return None, (w("xgpu_theirs", who=S["who"], names="、".join(theirs[:3])) if theirs else w("xgpu_nofit", vram=x["vram"], kept=kept)), False
        if len(keep) < x["ngpu"]:             # 留下上面任务最少、最快能腾空的卡
            keep = [c["uuid"] for c in sorted(cand, key=lambda c: (len(occ[c["uuid"]][0]), c["keep"]))[:x["ngpu"]]]
        S["holds"][x["key"]] = {"cards": keep, "until": hold_until(S, keep, t)}
        return None, w("xgpu_wait", idx=",".join(str(c["i"]) for c in S["cards"] if c["uuid"] in keep),
                       names="、".join(sorted({n for u in keep for n in occ[u][0]})) or "刚启动的任务"), True
    if x["ngpu"] == 0:
        return [], None, False
    free = card_free(S)                                      # MB
    ok = sorted((c for c in mine if free[c["uuid"]] >= card_need(x, c)), key=lambda c: -free[c["uuid"]])
    if len(ok) >= x["ngpu"]:
        return ok[:x["ngpu"]], None, False
    if not S["cards"]:                        # 不是资源等待，不成屏障：不然 30 分钟后连不用卡的任务也让位
        return None, w("no_gpu"), False
    ka = ",".join(str(c["i"]) for c in S["cards"] if c["uuid"] in S["owner"] and c["ka"])        # 停不了的保活不是对方在用，分开说
    owner = ",".join(str(c["i"]) for c in S["cards"] if c["uuid"] in S["owner"] and not c["ka"])
    return None, w("vram", vram=x["vram"], src=x["src"], kept=kept, held="；有卡留给独占任务" if blocked - S["owner"] else "",
                   ka="；卡 %s 上有对方另起、ka.sh 停不了的保活，不放（要停请对方停）" % ka if ka else "", ka_s="（卡 %s 有停不了的保活）" % ka if ka else "",
                   owner="；对方在用卡 %s（超出预留或刚回落，%d 分钟内不放）" % (owner, GUEST_QUIET_S / 60) if owner else ""), True


def person_use(pol, use):
    """各人员键（pol["own"]）名下几个项目的用量之和 {人员键: 卡·时}（fair_key 的第一项）；没写人员键的项目合在键 None 下。"""
    own, out = pol.get("own") or {}, {}
    for q, v in use.items():
        out[own.get(q)] = out.get(own.get(q), 0.0) + v
    return out


def fair_key(r, focus, pol, use, tot=None):
    """放行的先后（admit 逐个比；真放行与推演同一份，#23）：
    (这人的用量, 项目不在 focus 里, 项目的用量 ÷ 权重, 开始等的时刻)，小的先。
    - 这人的用量：pol["own"] 里同一个人员键的各项目的 use（衰减后的卡·时）之和。所以先比人，一人一份。看板给它上的每个项目都写人员键；
      没写的（看板上没有的项目、旧看板没写 owner）合起来算一份；
    - 同一人的项目之间：focus 的先（维护者 2026-11-17 定：优先只在同一人的项目之间排前，不越过别人），再比用量 ÷ 权重（pol["w"] 里没写的为 1）；
    - 项目内先来先跑。所有项目属于同一人（或都没写人员键）时第一项都相同，顺序与按项目比时（#23 之前）一样。
    tot：person_use(pol, use) 先算好的（admit 一次挑选里各请求共用），不给时现算。"""
    p = r["project"]
    mine = (person_use(pol, use) if tot is None else tot).get((pol.get("own") or {}).get(p), 0.0)
    return (mine, p not in focus, use.get(p, 0.0) / (pol["w"].get(p) or 1.0), r["since"])


def admit(reqs, focus, pol, use, now, place, shadow=lambda r: None):
    """一轮放行的规则（tick 真放行与 simulate 推演共用，规则只在这一处）。每次取排在最前的一个（fair_key）：先比人（同一人的几个项目的用量合起来）；
    同一人的项目之间 focus 的先，再比衰减后的 GPU 用量 ÷ 权重（use，卡·时；pol["w"] 里没写的为 1），小的先，放行一个就记上它预计的卡·时（r["charge"]），
    成组成员留下资源（place 的原因代码 gang_hold，记在 r["code"]）同样记上：留着就占着，后面的按记上之后的排；
    项目内先来先跑。独占任务不另排前面，轮到它时才留卡（place 里）。等得久不另排前面（否则单卡跑长任务时轮到的总是等了一整个任务的那个，
    权重不起作用），只防被越过：place(r) 放行返回 None，否则返回 (原因, 是否卡在资源上)；按这个顺序第一个卡在资源上又等满 pol["cap"] 的
    成为屏障，shadow(r) 给它预计最早能放行的时刻，排在它后面的（占位任务除外）只有预计在那之前跑完的才放（回填，fits_before），其余让位。
    返回没放行的 [(请求, 原因, 排位)]：排位 {n 在没放行的里排第几, puse 轮到它时这人（项目主人）的用量, focus, ratio 轮到它时项目的 用量÷权重, barrier 是屏障,
    behind 被哪个屏障挡着 [项目, 任务]}，看板的等待表据此逐条解释。"""
    use, left, barrier, until, out = dict(use), list(reqs), None, False, []
    while left:
        tot = person_use(pol, use)
        key = lambda r: fair_key(r, focus, pol, use, tot)
        r = left.pop(min(range(len(left)), key=lambda i: key(left[i])))
        k = key(r)
        pos = {"n": len(out) + 1, "puse": round(k[0], 3), "focus": not k[1], "ratio": round(k[2], 3)}
        if barrier and not (r["kind"] == "queue" and is_hold(r["name"])):
            if until is False:
                until = shadow(barrier)            # 后面有请求才推（推演里只排屏障一个时不再往下推）
            if not fits_before(now, r.get("dur"), until):
                out.append((r, "让位：%s 的 %s 已等 %d 分钟（等了 %d 分钟以上的不再被越过%s）" % (
                    barrier["project"], barrier["name"], (now - barrier["since"]) / 60, pol["cap"] / 60,
                    "；它预计 %s 能放行，这之前跑得完的才放" % now_str(until)[5:16] if until else ""),
                    dict(pos, behind=[barrier["project"], barrier["name"]])))
                continue
        res = place(r)
        if res is None or r.get("code") == "gang_hold":
            use[r["project"]] = use.get(r["project"], 0.0) + (r.get("charge") or 0.0)
        if res is None:
            continue
        if res[1] and now - r["since"] >= pol["cap"] and not barrier:
            barrier, pos["barrier"] = r, True
        out.append((r, res[0], pos))
    return out


def sim_snapshot(S, cut, gangs=None, first=None):
    """推演用的快照：从真放行的快照（Worker.snapshot）换成推演口径。与真放行不同、且本来就该不同的只有这几处：
    - 卡的上限与空闲都按 总显存 − labmon 之外的占用 − 给对方留的 算：推演不模拟对方、外来进程何时退出，当它们一直占着；
      keep 已扣进去，不再单算（多卡时“先挑没给对方留的”在推演里不起作用）；内存的 room 与 CPU 的 cpu_room 同理，照搬此刻的（labmon 之外的当常量）；
    - 不放的卡（owner）只留有停不了的保活的（卡的 ka）：它不会自己走，真放行一直不放，推演也不放（#45）；对方超出预留、刚回落是此刻才有的，不模拟（#25）。
      对自己的输出再换一次不变（shadow 在推演里再调 simulate）；
    - 没有旧 gpu_queue.sh 的任务、GPU 进程与外来 CPU 占用（它们是此刻的实况，不在时间轴上）；独占留卡与成组留的资源从空的开始，各自推各自的；
      成组的组（gangs，同 Worker.gsim；不给为没有）照真放行的给，成员最早何时放得下记进 first（gfirst，place 写）；
    - 截止（cut）按时刻在时间轴上判断（真放行由 override_hit 在排队前挡住）；原因只要短的。"""
    return dict(S, cards=[dict(c, total=cap, free=cap, keep=0.0, other=0.0) for c in S["cards"] for cap in [c["total"] - c["other"] - c["keep"]]],
                jobs=[dict(j) for j in S["jobs"]], ext=[], gp=[], cpu=None, holds={}, owner={c["uuid"] for c in S["cards"] if c["ka"]}, cut=cut, brief=True,
                gholds={}, gangs=gangs or {}, gfirst={} if first is None else first)


def simulate(now, snap, queues, focus, cut=None, horizon=None, pol=None, use=None, ends=None, dead=(), on_dep=None, gangs=None, first=None):
    """推演：从 now 起按 admit 的规则一轮轮放行（place，与真放行同一份），在跑的到预计结束时腾出资源、队列接着取下一个，直到排完或超出 horizon（秒）。
    只模拟显存（各卡按估计值记账；借来的主机上有停不了的保活的卡不放，同真放行）、内存、CPU（按估计核数记账）、独占（留卡与回填）、tag 上限、截止前的准入与公平份额；
    缺本地文件、拷本地依赖（含共享盘慢时推迟）、内存闸门、对方超出预留这类此刻才有的原因不模拟。
    - snap：资源快照（Worker.snapshot；sim_snapshot 换成推演口径）；
    - queues：[{key, kind, project, allowed（能用的卡序号，None 不限）, since（队首开始等的时刻）, jobs [job_spec…]}]，jobs 不含正在跑的；
    - cut：(截止时刻, 没估时的放不放)：续租截止、约定归还前跑不完的不放（同 override_hit 的 deadline）；
    - pol：调度设置（缺省 POLICY）；use：开始时各项目的 GPU 用量（卡·时，不含在跑的），往后按半衰期衰减，再加上推演里跑的；
    - ends、dead：依赖查的各任务预计结束 {(项目, 任务): 时刻} 与跑失败了的（dep_end；forecast 汇的）。写了依赖的任务等依赖都满足才排，
      满足不了的（等的失败了、不知道何时跑完）那条队列停在它这里；占位任务排上后结束取 dep_end（依赖满足它就放行）；
    - gangs：成组的组（Worker.gsim）：成员放得下时先留资源（place），本机的成员都留好了、别的主机上的成员预计都到点（at）就一起开跑，
      还没到点的到点再看（同真放行的 gang_commit）；别的主机上有成员说不出何时能放行（at 为 None）的不留、排不上。first：成员最早何时放得下（{(请求 key, 任务): 时刻}，place 写）。
    时长不明的任务（没估时也没有同类历史；占位任务等的条件不知道何时满足）排上后不知道何时结束（t1 为 None）：它那条队列停在它这里，
    要等它腾出资源的任务也排不上（不能当它 0 分钟就跑完，否则后面的全挤在同一刻）。
    - on_dep（forecast 给，dict）：排不上的队列里停在依赖上的记进去 {队列 key: 那一行的任务名}：写了依赖的行依赖满足不了或不知道何时满足，
      或前面的占位任务不知道何时放行（它的结束就是依赖何时满足），或成组的行同组还排不上（凑不齐、成环、别的主机上的成员说不出何时能放行：
      等的是同伴，挪走的行带着同样的 gang=，同 after=）。offers 据此不把这条清单当“原处跑不了”（#183、#180）。
    返回 (排上的 [{key, name, project, t0, t1, gpus, after（等到谁跑完才轮到它 [[项目, 任务]]）, gang（成组的组名）}], 排不上的 {队列 key: 原因})。"""
    horizon, pol, use0, ends, gangs = FORECAST_S if horizon is None else horizon, pol or POLICY, use or {}, dict(ends or {}), gangs or {}
    S = sim_snapshot(snap, cut, gangs, first)
    out, freed, last, blocked = [], [], {}, {}
    gstuck = {}             # 队列 key → 停在它这里的成组的行（同组还排不上，place 的 gang_unk）：同依赖，记进 on_dep
    stuck_res = {}          # 队列 key → 最后一次是不是卡在资源上（等时长不明的任务腾资源时据此解释）
    ptr, since = {q["key"]: 0 for q in queues}, {q["key"]: q["since"] for q in queues}
    rows = [(y["project"], y["cards"], y["t0"], y["t1"]) for y in S["jobs"]]     # 推演里跑过的（含开始时在跑的）：算用量
    t = now

    def used():
        d = 0.5 ** ((t - now) / pol["hl"])
        u = {p: v * d for p, v in use0.items()}
        for p, v in usage(rows, t, pol["hl"]).items():
            u[p] = u.get(p, 0.0) + v
        return u

    def shadow(r):          # 屏障最早何时放行：从此刻的在跑的起，只排它一个（成组的按它自己最早何时放得下算）
        q = {"key": r["key"], "kind": r["kind"], "project": r["project"], "allowed": r["allowed"], "since": r["since"], "jobs": [dict(r, gang=None)]}
        o, _ = simulate(t, S, [q], focus, cut, now + horizon - t, pol, ends=ends, dead=dead)
        return o[0]["t0"] if o else None

    def launch(r, cs):
        d = r["dur"]
        if r.get("durk") and cs and not r["hold"]:    # 按开跑时那几张卡上的 labmon 任务数（含自己）取时长（#67）
            k = sum(1 + sum(1 for y in S["jobs"] if not y["hold"] and c["uuid"] in y["gpus"]) for c in cs) / len(cs)
            d = (r["durk"](k) or {}).get("min")
        e = {"key": r["key"], "name": r["name"], "project": r["project"], "t0": t, "gpus": [c["uuid"] for c in cs],
             "t1": dep_end(dep_keys(r), t, ends, dead) if r["hold"] and dep_keys(r) else t + d * 60 if d is not None else None,
             "after": [f for f in freed][:3] if t > r["since"] else [], **({"gang": r["gang"]} if r.get("gang") else {})}
        ends[(r["project"], r["name"])] = e["t1"]       # 同一遍里排在它后面、等它的任务就能看到
        S["jobs"].append(dict(e, **acct_job(r, cs, t, e["t1"], S["cpu_cap"])))
        rows.append((r["project"], r["cards"], t, e["t1"]))
        out.append(e)
        ptr[r["key"]] += 1

    def place_(r):
        cs, why, res = place(r, S, t)
        r["code"] = S["code"]                     # admit 按它认成组留下的（同 Worker.place）
        gstuck.pop(r["key"], None)
        if cs is not None:
            return launch(r, cs)
        stuck_res[r["key"]] = res
        if S["code"] == "gang_unk":
            gstuck[r["key"]] = r
        return why, res

    while True:
        busy, wake, reqs = {y["key"] for y in S["jobs"]}, [], []
        for q in queues:
            if q["key"] in busy or ptr[q["key"]] >= len(q["jobs"]):
                continue
            r = dict(q["jobs"][ptr[q["key"]]], key=q["key"], kind=q["kind"], project=q["project"], since=since[q["key"]], allowed=q["allowed"])
            r["charge"] = r["cards"] * (r["dur"] or 0.0) / 60
            d = dep_end(dep_keys(r), t, ends, dead) if dep_keys(r) else None
            blocked.pop(q["key"], None)
            if dep_keys(r) and d is None:         # 依赖满足不了或不知道何时满足：这条队列停在它这里
                blocked[q["key"]] = r
            elif d and d > t:                     # 依赖还没满足：不参加这一轮，到时再看
                wake.append(d)
            else:
                if d:                             # 等依赖的时间不算排队（同真放行：依赖满足后才开始等）
                    since[q["key"]] = r["since"] = max(r["since"], d)
                reqs.append(r)
        last.update({r["key"]: why for r, why, _ in admit(reqs, focus, pol, used(), t, place_, shadow)})
        # 成组：这一轮没重新留的（让位给屏障）放掉；本机的成员都留好了、别的主机上的预计都到点（at）了就一起开跑，没到点的到点再看（同 Worker.gang_commit）
        S["gholds"] = {k: h for k, h in S["gholds"].items() if h["seen"] >= t}
        for gk, g in sorted(gangs.items()):
            loc = sorted(g["local"])
            hs = [S["gholds"].get(k) for k, _ in loc]
            if not loc or any(h is None or h["name"] != n for h, (_, n) in zip(hs, loc)):
                continue
            if g["at"] <= t:
                for h in hs:
                    del S["gholds"][h["key"]]
                    launch(h["x"], h["cards"])
            else:
                wake.append(g["at"])
        nxt = [y["t1"] for y in S["jobs"] if y["t1"] is not None] + wake
        if not nxt or min(nxt) > now + horizon:
            break
        t = max(t, min(nxt))
        done = [y for y in S["jobs"] if y["t1"] is not None and y["t1"] <= t]
        S["jobs"] = [y for y in S["jobs"] if not (y["t1"] is not None and y["t1"] <= t)]
        freed = [[y["project"], y["name"]] for y in done if not y["hold"]]
        for y in done:
            if y["key"] in since:
                since[y["key"]] = t
    never = {y["key"]: y for y in S["jobs"] if y["t1"] is None}       # 排上了但不知道何时结束的：它那条队列的后面排不上
    unknown = "、".join(y["name"] for y in never.values() if not y["hold"])
    stuck = {}
    for q in queues:
        if ptr[q["key"]] >= len(q["jobs"]):
            continue
        y, b, g = never.get(q["key"]), blocked.get(q["key"]), gstuck.get(q["key"])
        if on_dep is not None and (y["hold"] if y else b or g):
            on_dep[q["key"]] = (y or b or g)["name"]
        stuck[q["key"]] = ("前面的占位任务不知道何时放行" if y["hold"] else "前面的 %s 不知道要跑多久（没估时也没有同类历史）" % y["name"]) if y \
            else "%s 等的 %s 跑不成或不知道何时跑完" % (b["name"], dep_names(dep_keys(b), b["project"])) if b \
            else "%s 是成组 %s 的，同组还排不上（凑不齐、成环，或别的主机上的成员说不出何时能放行）" % (g["name"], g["gang"]) if g \
            else "等 %s 腾出资源，它不知道要跑多久" % unknown if unknown and stuck_res.get(q["key"]) else \
            "推演到 %d 天后还没轮到" % (horizon / 86400) if nxt else "一直排不上，" + last.get(q["key"], "原因不明")
    return out, stuck


def estimate(hist, r):
    """资源估计：显存、内存取同类任务最近几次峰值的最大值 × 1.1，及单跑（同卡没有别的任务）时的平均 GPU 利用率。只看量到了用量的记录。worker 准入、推演与 fit 都用它。
    显存、内存逐项依次看（#61 维护者定）：同 tag、去掉种子编号后同名的历史（两项一起取自先找到的那一档）› 清单写的提示值（vram=、ram=，> 0 才算写了）
    › 同一条队列的历史（同一份清单里可以是完全不同的任务，最弱，不盖过明写的提示值）› 0。来源：显存 src、内存 ram_src；util 跟着显存的来源走
    （显存用的是提示值时为 None，不拿别的任务的利用率）。
    CPU（cpus 整数核，job_cpus 取整；cpu_src 来源）：清单的 cpus= 优先（来源“提示值”），否则同类（同 tag › 同名 › 同队列）近几次 1 分钟峰值核数（cores_peak，
    期 1 起才记，旧行当没有历史）的最大值 × 1.1，都没有按 CPU_DEFAULT（#4 维护者定：cpus= 优先于历史，历史优先于缺省）。
    OOM 后抬高（#62 维护者定）：用量行标了 oom 的（finish 经 oom_of 记）不进历史（它的峰值是被当时可用的量截住的）；同 tag、同名两档里，
    各取这一档最近一次跑成功之后的 OOM 记录，给 OOM 的那一项（vram 或 ram）一个下限 max(那次放行时的估计 est × 2, 那次的峰值 × 1.1)，几条取大；
    这一项的估计 = max(上面按顺序取的值, 下限)，提示值写小了的同样抬高。连续 OOM 连续翻倍（每条的 est 已含上一次的下限），封顶照 card_need、ram_need；
    这一档有一次新的跑成功（退出码 0；跑了 5 分钟后被停下、别的错误退出的不算）就回到按实测估。同队列一档的 OOM 不算（同一份清单里可以是完全不同的任务）。来源是下限时 util 为 None。"""
    keys = ([("tag", r["project"], r["tag"])] if r.get("tag") else []) + [("name", r["project"], base_name(r["name"]))]
    if r.get("queue"):
        keys.append(("queue", r["root"], r["queue"]))
    ok = lambda h: not h.get("oom") and (h.get("rc") == "0" or (h.get("min") or 0) >= 5)     # noqa: E731 — 跑成功的，或跑了 5 分钟以上（峰值已经出来了）、不是 OOM 的
    label = {"tag": "同 tag", "name": "同名", "queue": "同队列"}
    peak = None                                   # 显存、内存取自的那一档历史：(档, 记录)
    floor = {"vram": (0.0, None), "ram": (0.0, None)}   # OOM 后的下限：(GiB, 来源)
    cpu = (job_cpus(r["cpus"]), "提示值") if r.get("cpus") is not None else None
    for k in keys:
        rows = [h for h in hist if hist_key(h, k[0]) == k]   # hist 按结束时刻排好（load_hist）
        same = [h for h in rows if ok(h)]
        hs = [h for h in same if h.get("ram_peak") is not None][-HIST_N:]
        if hs and peak is None:
            peak = (k[0], hs)
        cs = [h["cores_peak"] for h in same if h.get("cores_peak") is not None][-HIST_N:]
        if cs and cpu is None:
            cpu = (cpu_from_peak(max(cs)), "%s 近 %d 次峰值 ×1.1" % (label[k[0]], len(cs)))
        if k[0] == "queue":
            continue
        last = max((i for i, h in enumerate(rows) if h.get("rc") == "0"), default=-1)   # 只认真跑成功的：跑了 5 分钟后被停下、别的错误退出的不算资源够了
        for h in rows[last + 1:]:
            f = h.get("oom")
            if f not in floor:
                continue
            e = (h.get("est") or {}).get(f) or 0.0
            pk = (h.get("vram_peak") or 0) / 1024 / max(1, len(h.get("gpus") or [1])) if f == "vram" else h.get("ram_peak") or 0
            for v, s in ((2 * e, "放行时估 %.1f GiB ×2" % e), (1.1 * pk, "OOM 时峰值 %.1f GiB ×1.1" % pk)):
                if round(v, 2) > floor[f][0]:
                    floor[f] = (round(v, 2), "%s 上次 OOM：%s" % (label[k[0]], s))
    cpu = cpu or (CPU_DEFAULT, "缺省 %d 核" % CPU_DEFAULT)
    if peak:
        kind, hs = peak
        src = "%s 近 %d 次峰值 ×1.1" % (label[kind], len(hs))
        solo = [h["gpu_util"] for h in hs if h.get("gpu_util") is not None and not [x for x in h.get("with_gpu") or [] if x != "GPU 保活"]]
        hv = {"ram": round(1.1 * max(h["ram_peak"] or 0 for h in hs), 2),
              "vram": round(1.1 * max((h.get("vram_peak") or 0) / 1024 / max(1, len(h.get("gpus") or [1])) for h in hs), 2),
              "util": round(sum(solo) / len(solo)) if solo else None}
    out = {}
    for f, s in (("vram", "src"), ("ram", "ram_src")):
        if peak and (peak[0] != "queue" or not r[f]):
            out[f], out[s] = hv[f], src
        else:
            out[f], out[s] = r[f], "提示值"
        if floor[f][0] > out[f]:
            out[f], out[s] = floor[f]
    out["util"] = hv["util"] if peak and out["src"] == src else None
    return dict(out, cpus=cpu[0], cpu_src=cpu[1])


def rec_conc(h):
    """一条用量记录跑的时候同卡平均有几条 labmon 任务（含自己，measure 记的 conc，#67）。
    11-09 前的行没有 conc：同卡没有别的任务（with_gpu 只有保活）的当 1，其余不知道（None）。这条回退 11-23 后可删（那时同类近几次都有 conc）。"""
    if h.get("conc") is not None:
        return h["conc"]
    return None if [x for x in h.get("with_gpu") or [] if x != "GPU 保活"] else 1.0


def near(hs, k, n):
    """按同卡并行数挑记录（duration 用）：知道并行数的里，与 k 的差不超过最接近的那条再多 1 的，取最新的 n 条；
    都不知道并行数（11-09 前的、挤着跑的旧行）时取最新的 n 条。保持原来的先后。"""
    known = [(h, rec_conc(h)) for h in hs if rec_conc(h) is not None]
    if not known:
        return hs[-n:]
    d = min(abs(c - k) for _, c in known)
    return [h for h, c in known if abs(c - k) <= d + 1.0][-n:]


def card_conc(j, on, jobs):
    """此刻任务 j 所用的卡 on（非空）上平均几条 labmon 任务（含自己；jobs 里的占位任务不算，按对象去重，多卡取各卡平均）。
    measure 按它累计按时间加权的 conc（用量行同名字段，duration 挑记录比的就是它），Worker.k_of 在还没量到 conc 时按它回退（#67、#86）。"""
    return sum(len({id(o) for o in jobs if not is_hold(o["name"]) and g in (o.get("gpus") or [])} | {id(j)}) for g in on) / len(on)


def run_tot(running, eta, eta_t):
    """在跑任务按日志进度估的总时长 {(项目, 同类名): [(任务, 分钟)]}：看板写来的预计结束（eta.json 的 jobs，写的时刻 eta_t 在任务开始之后才算）− 开始。
    同类还没有跑完的时，排队的按它估（duration 的 run，#67）。占位任务不算（它没有日志进度）。Worker.run_tot 与 fit（读状态里的 running）共用。"""
    out = {}
    for j in running:
        e = None if is_hold(j["name"]) else eta.get("%s|%s" % (j.get("project"), j["name"]))
        if e and eta_t >= j["t0"] and e > j["t0"]:
            out.setdefault((j.get("project"), base_name(j["name"])), []).append((j["name"], round((e - j["t0"]) / 60)))
    return out


def duration(hist, r, model=None, k=1, run=None):
    """任务要跑多久（分钟，中间值）：看板的预计时间与截止前的准入（再乘 DUR_MARGIN）共用这一处。依次看：
    - 同类（项目 + 去掉种子编号的名字）跑成功过的：这一个与记录都有估时就按同类 实际/估时 的中位数折算，否则取同类实际用时的中位数；
    - 同类还没有跑完的、有在跑的：run 给的各个在跑同类按日志进度估的总时长（看板写来的 eta.json）的中位数；
    - 本项目最近 RATIO_N 个的 Σ实际/Σ估时（按时长加权，几分钟的探针不占分量；至少 2 个，截到 [1/3, 3]）折算 est_min；
    - 清单的 est_min。
    只认跑完的（停下重排的只跑了一段）、同型号 GPU 的记录（没记型号的旧记录都认）。同类与项目比值的记录都按同卡并行数挑（near）：
    k 是这个任务预计会遇到的同卡 labmon 任务数（含自己），取并行数与它最接近的那几条——几条任务分时共用一张卡时，有的类几乎不拖慢、有的拖慢好几倍，
    单跑的记录估不了挤着跑的（#67：27 上的小任务并行 4 条与 6 条都是 20 分钟，sm_vit 并行 4 条要 300 分钟）。
    占位任务等的是条件、不是算力：就按清单估时，不看同类、不折算，它的时长也不进项目比值（写了依赖的另由 dep_end 算）。
    返回 {"min", "src", "k"（实际/估时，不知道为 None）, "cls"（同类名）, "same"（是不是同类历史给的：读写拥塞嫌疑只认它）}；没有估时也没有同类记录为 None。"""
    cls, proj, e = base_name(r["name"]), r["project"], r.get("est_min")
    if is_hold(r["name"]):
        return {"min": e, "src": "清单估时", "k": None, "cls": cls, "same": False} if e else None
    ok = [h for h in hist if h.get("min") and (h.get("rc") == "0" or (h.get("rc") is None and h.get("kind") == "slot"))
          and not h.get("requeued") and hist_key(h, "name")[1] == proj and (not model or not h.get("gpu") or h["gpu"] == model)]
    same = near([h for h in ok if base_name(h.get("name") or "") == cls][-RATIO_N:], k, HIST_N)
    if same:
        cs = [c for c in map(rec_conc, same) if c is not None]
        par = "（同卡并行约 %g 条）" % round(statistics.median(cs), 1) if cs and max(cs) > 1.05 else ""
        ks = [h["min"] / h["est_min"] for h in same if h.get("est_min")]
        if e and ks:
            q = statistics.median(ks)
            return {"min": e * q, "src": "同类近 %d 次%s 实际/估时 ×%.2f" % (len(ks), par, q), "k": round(q, 3), "cls": cls, "same": True}
        m = statistics.median(h["min"] for h in same)
        return {"min": m, "src": "同类近 %d 次%s实际用时" % (len(same), par), "k": round(m / e, 3) if e else None, "cls": cls, "same": True}
    if run:
        m = statistics.median(run)
        return {"min": m, "src": "同类在跑的 %d 个按进度估" % len(run), "k": round(m / e, 3) if e else None, "cls": cls, "same": False}
    if not e:
        return None
    pr = near([h for h in ok if h.get("est_min") and not is_hold(h.get("name"))][-2 * RATIO_N:], k, RATIO_N)
    if len(pr) >= 2:
        q = min(3.0, max(1 / 3, sum(h["min"] for h in pr) / sum(h["est_min"] for h in pr)))
        return {"min": e * q, "src": "本项目近 %d 个 实际/估时 ×%.2f（按时长加权）" % (len(pr), q), "k": round(q, 3), "cls": cls, "same": False}
    return {"min": e, "src": "清单估时", "k": None, "cls": cls, "same": False}


def known_lists(p):
    """fit 用：登记过的清单 [(主机, 队列, 清单行 list_jobs)]：本机（p）的在前，别的主机（共享 HOME 下的 hosts/*/queues）按主机名排在后面，
    各按登记文件名的顺序。一次 fit 调用只读一遍。"""
    mine = os.path.normpath(p.queues)
    dirs = [mine] + sorted(d for d in glob.glob(os.path.join(HOME, "hosts", "*", "queues")) if os.path.normpath(d) != mine)
    return [(os.path.basename(os.path.dirname(d)), q, list_jobs(q)) for d in dirs for q in registry(d)[0]]


def list_row(lists, name, project=None):
    """known_lists 里叫 name 的那一行，取第一条；写了 project 的只认这个项目的清单。返回 (主机, 队列, 命令)，找不到为 None。"""
    for host, q, rows in lists:
        cmd = None if project and q["project"] != project else job_cmd(q, name, rows)
        if cmd is not None:
            return host, q, cmd
    return None


def cpu_from_peak(peak):
    """按 1 分钟峰值核数估几个核：峰值 ×1.1 取整（job_cpus）、至少 1。estimate 按同类历史估、cpu_fit 给“写小”的建议值共用。"""
    return max(1, job_cpus(1.1 * peak))


def cpu_fit(j, cap, util):
    """在跑的任务 j（状态 running 的形状：name、gpus、est、use）开跑时给的核数是给少了还是给多了（#46；#37 的“被 CPU 卡住”也是它）。
    从开始量窗口核数起满 CPU_WARM_S（cpu_warm）才判；给的核数 give 是开跑时导出的 LABMON_CPUS（cpus_export）；util 各卡近 UTIL_WIN_S 的利用率 {uuid: %}。
    - low（给少了、被 CPU 卡住）：近 CPU_WARM_S 各 1 分钟窗口核数（use.pks）的平均不少于 give × CPU_BOUND_FRAC，且所用的卡里有利用率低于 UTIL_LOW 的
      （卡吃饱了加核也没用；不用卡的不判，单线程脚本本来就只用 1 核）。sug 建议写的 cpus= 是 give 的 2 倍封顶到记账上限 cap；已给到 cap 为 None。
    - high（给多了）：估计来自清单的 cpus=（est.cpu_src 是“提示值”；按历史估的会往实测靠，不判），跑到现在的 1 分钟峰值 use.cores_peak
      不超过 give × CPU_SPARE_FRAC、且少至少 CPU_SPARE_MIN 核（看整段峰值：开头加载阶段用过的核不算多给）。sug 按峰值估（cpu_from_peak，同 estimate）。
    返回 {how, avg 近 10 分钟平均核数, peak 整段峰值, give, sug}，都不是为 None。占位任务、没有估计的不判。
    gpu_slack（fit 开车道）只取 low；状态里运行中任务的 cpu_fit 是它，看板据此出只给实验会话的 cpu_low:、cpu_high: 告警。"""
    u, e = j.get("use") or {}, j.get("est")
    if is_hold(j.get("name")) or not e or not cpu_warm(u):
        return None
    give = cpus_export(e.get("cpus", CPU_DEFAULT), cap)
    avg = round(sum(x[1] for x in u["pks"]) / len(u["pks"]), 2)
    peak = u.get("cores_peak") or max(x[1] for x in u["pks"])
    out = {"avg": avg, "peak": peak, "give": give}
    if avg >= give * CPU_BOUND_FRAC and any((util.get(g) is not None and util[g] < UTIL_LOW) for g in j.get("gpus") or []):
        return dict(out, how="low", sug=None if cap is not None and give >= cap else min(2 * give, cap or 2 * give))
    if e.get("cpu_src") == "提示值" and e.get("cpus") and peak <= give * CPU_SPARE_FRAC and give - peak >= CPU_SPARE_MIN:
        return dict(out, how="high", sug=cpu_from_peak(peak))
    return None


def gpu_slack(cards, jobs, th, cap):
    """卡吃不满是不是真有余量（fit 开车道、offers 同机多开车道共用一条规则，#35、#37）：
    cards [{uuid, util 近 10 分钟利用率 %（样本不够为 None）}]；jobs 在跑的（状态 running 的形状：name、gpus、est、use）；
    th 是状态的 throttle（Worker.throttle：这一段限流，门槛 THR_HOT、THR_COOL 只在 thr_step；没在限流为 None）；cap 记账上限（cpu_cap）。
    返回 {throttle: 限流时的那句说明、否则 None（这台先减线程，不开新车道）,
    bound: {卡 uuid: [(任务, 平均核数, 给的核数)]}：利用率低于 UTIL_LOW 的卡上被 CPU 卡住的在跑任务（cpu_fit 的 low；先给它们加核，不在这张卡上开新车道）}。"""
    util = {c["uuid"]: c.get("util") for c in cards}
    low = {u for u, v in util.items() if v is not None and v < UTIL_LOW}
    bound = {}
    for j in jobs:
        b = cpu_fit(j, cap, util)
        for u in j.get("gpus") or []:
            if b and b["how"] == "low" and u in low:
                bound.setdefault(u, []).append((j["name"], b["avg"], b["give"]))
    thr = None if th is None else th.get("thr")
    return {"throttle": None if th is None else "容器 CPU 在限流（%s，已持续 %d 分钟）" % (
        "近 1 分钟 %.0f%% 的周期被限流" % thr if thr is not None else "近 1 分钟被限流的周期过了门槛", th.get("for", 0) // 60), "bound": bound}


def fit_snapshot(st):
    """fit 用：一台主机的 status.json → 准入核心 place 认的资源快照（字段见 _place；与 Worker.snapshot 同形）。
    卡的空闲取状态的 room.gpus（已扣在跑任务的记账与给对方留的），加回保活占的（放任务前 worker 先停保活），给对方留的照 room.keep（整卡封顶要扣）；在跑的只记内存与核（显存已扣在卡上），
    room、cpu_room 是 labmon 全停时能给的（room 加回在跑的记账：ram_need、cpu_need 按它封顶）；tag 上限照 limits；对方超出预留与有停不了保活的卡
    照 room.owner 不放（旧 worker 没写为空）。独占留卡、成组留的资源不在状态里，不模拟。"""
    room, info = st.get("room") or {}, st.get("info") or {}
    ka = {}
    for g in st.get("gpu_procs") or []:
        if g.get("owner") == "keepalive":
            ka[g["uuid"]] = ka.get(g["uuid"], 0.0) + g["mb"]
    acct = lambda j, k: (j.get("acct") or {}).get(k) or 0.0
    jobs = [{"key": "run:%d" % i, "project": j.get("project"), "name": j["name"], "t0": j.get("t0"), "t1": None, "gpus": j.get("gpus") or [],
             "vram": {}, "ram": acct(j, "ram"), "cpu": acct(j, "cpu"), "excl": j.get("excl"), "tag": j.get("tag"), "hold": bool(j.get("hold")),
             "ngpu": len(j.get("gpus") or []), "use": j.get("use") or {}, "cards": 0.0} for i, j in enumerate(st.get("running") or [])]
    by = {j["name"]: j for j in jobs}
    gp = [dict(g, _j=by.get(g.get("job")) if g.get("owner") == "job" else None) for g in st.get("gpu_procs") or []   # 独占的判断（intruders）要看卡上有谁；
          if g.get("owner") != "keepalive"]                     # worker 停得了的保活放任务前先停（卡的空闲已加回），停不了的是 other、照算
    cards = [{"i": g["i"], "uuid": g["uuid"], "total": g.get("total") or 0.0, "keep": (room.get("keep") or {}).get(g["uuid"], 0.0), "other": 0.0, "ka": False,
              "free": (room.get("gpus") or {}).get(g["uuid"], g.get("free") or 0.0) + ka.get(g["uuid"], 0.0)} for g in info.get("gpus") or []]
    return {"cards": cards, "limit": (info.get("mem") or {}).get("limit"),
            "room": None if room.get("ram") is None else room["ram"] + sum(j["ram"] for j in jobs), "margin": room.get("margin") or 0,
            "cpu_cap": room.get("cpu_cap"), "cpu_room": None if room.get("cpu") is None else room["cpu"] + sum(j["cpu"] for j in jobs),
            "jobs": jobs, "ext": st.get("external") or [], "gp": gp, "cpu": room.get("other_cpu"), "holds": {}, "tags": (st.get("limits") or {}).get("tags") or {},
            "owner": set(room.get("owner") or []), "who": "对方" if st.get("guest") else "labmon 之外的进程",
            "kept": bool((st.get("guest") or {}).get("keep")), "cut": None, "brief": False, "gholds": {}, "gangs": {}, "gfirst": {}}


class FitHost:
    """fit 眼里的一台主机：它的状态（status.json）拼成的快照（fit_snapshot），这次 fit 假设开的车道按 acct_job 记进快照再判下一行（与推演同一做法）。
    请求拼法与临时指令借 Worker 的 job_spec、override_hit（只填它们要的 info、ovr、host），不另写。"""

    def __init__(self, host, st, hist, eta):
        self.host, self.st, self.hist, self.eta = host, st, hist, eta
        self.w = object.__new__(Worker)
        self.w.host, self.w.info, self.w.ovr = host, st.get("info") or {}, st.get("overrides") or []
        self.w.req_dur = lambda r: r.get("dur")       # override_hit 判截止前跑不跑得完要的时长：fit 拼的请求（job_spec）里已有
        self.S = fit_snapshot(st)
        u10 = st.get("util10") or {}
        self.cards = [dict(c, util=u10.get(c["uuid"])) for c in self.S["cards"]]
        self.slack = gpu_slack(self.cards, st.get("running") or [], st.get("throttle"), self.S["cpu_cap"])
        self.lanes, self.say, self.why = [], [], None     # 这次假设开的 [(任务, 卡号)] 与各行的说明；最近一行没开成的原因
        self.solo, self.solo_host = {}, None              # 这次分给只有提示值的行的卡 {uuid: 任务}（之后哪一行都不与它同卡）；不用卡的那种占了整台：任务名

    def card_lines(self):
        """各卡此刻的样子（可用显存、利用率、在跑谁、CPU 受限）与限流：fit 先列出来。"""
        free, out = card_free(self.S), []
        for c in self.cards:
            jobs = [j["name"] for j in self.S["jobs"] if c["uuid"] in j["gpus"]]
            b = self.slack["bound"].get(c["uuid"])
            out.append("卡 %d：可用 %.1f GiB，近 10 分钟利用率 %s%s%s" % (
                c["i"], max(0.0, free[c["uuid"]]) / 1024, "%d%%" % c["util"] if c["util"] is not None else "—（样本不够）",
                "（在跑 %s）" % "、".join(jobs) if jobs else "（空着）",
                "；%s 跑满了给的核（CPU 受限）" % "、".join("%s %.1f/%d 核" % x for x in b) if b else ""))
        if self.slack["throttle"] is not None:
            out.append(self.slack["throttle"])
        return out

    def req(self, q, n, cmd, project):
        """清单 q 里叫 n 的一行（命令 cmd）在这台上的样子：(请求 r, 资源估计 e, place 认的 x)。q 是别的主机上登记的，--gpus 限卡只在那台上算。"""
        r = queue_req(q, job_of(n, cmd))
        if project:
            r["project"] = project
        e = estimate(self.hist, r)
        # 时长同真放行（queue_durs）：按此刻放一个新任务会遇到的同卡并行数（最空的那张卡，同 k_now；这次假设开的也算）与在跑同类的进度挑记录
        kn = min((1 + sum(1 for j in self.S["jobs"] if not j["hold"] and c["uuid"] in j["gpus"]) for c in self.S["cards"]), default=1)
        d = duration(self.hist, r, self.model(), kn, self.runs(r))
        x = dict(self.w.job_spec(n, r, e, d and d["min"]), key="fit:%s" % n, project=r["project"],
                 allowed=q["gpus"] if q.get("_host") == self.host else None)
        return r, e, x

    def model(self):
        return ((self.w.info.get("gpus") or [{}])[0]).get("name")

    def runs(self, r):
        """这台在跑的同类（项目 + 去掉种子编号的名字）按进度估的总长（模块级 run_tot，与 worker 同口径；看板写来的 eta.json），不含它自己。"""
        return [m for y, m in run_tot(self.st.get("running") or [], self.eta.get("jobs") or {}, self.eta.get("t") or 0).get((r["project"], base_name(r["name"])), [])
                if y != r["name"]]

    def blocked(self, project):
        """这台此刻不开新车道的整台原因：持久的临时指令挡住这个项目（drain、只给别的项目用、暂停，override_hit 的 lasting）、
        借来的主机上对方的内存或 CPU 超出预留、刚回落（真放行的 guest_block，状态的 guest.block）、容器在限流；没有为 None。"""
        hit = self.w.override_hit({"project": project, "name": ""}, ("drain", "guest", "pause"), lasting=True)
        if hit:
            return hit[1]
        block = (self.st.get("guest") or {}).get("block")       # 只有内存、CPU 超出（或刚回落）挡整台；某张卡超出的经 room.owner 只挡那张
        if block:
            return block + " → 这台不开新车道"
        if self.slack["throttle"] is not None:
            return self.slack["throttle"] + " → 不开新车道：先减线程（按 LABMON_CPUS 设线程数与 DataLoader 进程数）"
        return None

    def open(self, r, e, x, t, row=True):
        """为这一行再开一条车道放不放得下：放得下返回说明并把它记进快照（acct_job）；放不下返回 (None, 原因)。
        判断就是 place；fit 只多两条：估计只有提示值的（没有实测）只放空卡、不与别的任务同卡（不用卡的只在这台没有 labmon 任务时开；
        这次 fit 先分了它，后面的行也不与它同卡、同台：solo）；
        利用率低的卡上在跑的任务被 CPU 卡住时不在那张卡上开（gpu_slack）。"""
        why = self.blocked(x["project"])
        hit = None if why else self.w.override_hit(x, ("deadline",), lasting=True)    # 续租、约定归还的截止前跑不完的不放（同真放行，按这一行的预计时长）
        if why or hit:
            return None, why or hit[1]
        if self.solo_host:
            return None, "这次已把没有实测的 %s 分到这台（只在这台没有别的 labmon 任务时开） → 不再与它同开" % self.solo_host
        S, gpu = self.S, x["ngpu"] > 0
        hint = e["src" if gpu else "ram_src"] == "提示值"
        # 有别的任务的卡：labmon 在跑的、旧 gpu_queue.sh 的、卡上 labmon 之外的进程（保活不算：放任务前 worker 先停它）
        busy = ({u for j in S["jobs"] if not j["hold"] for u in j["gpus"]} | {u for j in S["ext"] for u in j.get("gpus") or []}
                | {g["uuid"] for g in self.st.get("gpu_procs") or [] if g.get("owner") != "keepalive"})
        free = card_free(S)
        full = place(dict(x), dict(S, holds={}), t)
        if gpu:
            sel = [c["i"] for c in S["cards"] if (x["allowed"] is None or c["i"] in x["allowed"])
                   and c["uuid"] not in self.slack["bound"] and c["uuid"] not in self.solo and not (hint and c["uuid"] in busy)]
            cs, why, _ = place(dict(x, allowed=sel), dict(S, holds={}), t)
            solo = [self.solo[u] for u in (c["uuid"] for c in (full[0] or [])) if u in self.solo]
            if cs is None and solo and not hint:
                return None, "这次已把没有实测的 %s 分到卡 %s → 不与它同卡" % ("、".join(solo), ",".join(str(c["i"]) for c in full[0] if c["uuid"] in self.solo))
        elif hint and any(not j["hold"] for j in S["jobs"]):
            cs, why = None, None
        else:
            cs, why = full[0], full[1]
        if cs is None:
            if full[0] is None:
                return None, full[1]
            c = full[0][0] if full[0] else None
            b = self.slack["bound"].get(c["uuid"]) if c else None
            if b and not hint:
                left = cpu_free(S)
                return None, "卡 %d 近 10 分钟利用率 %d%%，低是因为在跑的 %s跑满了给它的核 → 不在这张卡上开新车道：先给它那条车道加核（清单行 cpus=）%s" % (
                    c["i"], next(x_["util"] for x_ in self.cards if x_["uuid"] == c["uuid"]),
                    "、".join("%s（近 10 分钟平均 %.1f 核，给了 %d 核）" % y for y in b),
                    "；这台还能给 %.0f 核" % left if left is not None and left >= 1 else "；这台已没有余核，瓶颈在 CPU")
            k, v = ("vram", r["vram"]) if gpu else ("ram", r["ram"])
            return None, "%s，%s → 不开新车道（%s），排在已有车道后面%s" % (
                ("没有同 tag、同名任务的实测" if row else "没有同类任务的历史") if v else
                "没有同类任务的历史%s" % ("（按 tag、去掉种子编号后的名字、同一条队列找）" if row else ""),
                "清单写了 %s=%g，还没有实测%s" % (k, v, "（同一条清单里别的任务的历史不用）" if row else "") if v else "估不出%s" % ("显存" if gpu else "内存"),
                "没有实测的只放空卡、不与别的任务同卡：卡上都有任务在跑" if gpu else "没有实测的只在这台没有 labmon 任务在跑时开",
                "；名字各异的同类任务写同一个 tag= 就认得出" if row and not r["tag"] else "")
        rneed, rsrc = ram_need(x, S)
        need, csrc = cpu_need(x, S)
        if gpu:
            c = cs[0]
            k = sum(c["uuid"] in j["gpus"] for j in S["jobs"] if not j["hold"]) + 1     # 与 worker 同口径：这台的型号、在跑同类按进度估的（eta.json）
            d = duration(self.hist, r, self.model(), k, self.runs(r))
            util = next(y["util"] for y in self.cards if y["uuid"] == c["uuid"])
            say = "卡 %s 可用 %.1f GiB，要 %.1f GiB（%s，含另留的 1 GiB）；近 10 分钟利用率 %s%s%s" % (
                ",".join(str(y["i"]) for y in cs), free[c["uuid"]] / 1024, need_mb(x["vram"]) / 1024, x["src"], "%d%%" % util if util is not None else "—",
                "；它单跑时利用率约 %d%%" % e["util"] if e["util"] is not None else "", "；同卡 %d 条时约要 %.0f 分钟（%s）" % (k, d["min"], d["src"]) if d else "")
        else:
            say = "不用 GPU，内存要 %.0f GiB（%s）、%d 核（%s）" % (rneed, rsrc, need, csrc)
        S["jobs"].append(acct_job(x, cs, t, None, S["cpu_cap"]))
        self.lanes.append((r["name"], [y["i"] for y in cs]))
        if hint:                                          # 没有实测的不与别的同卡（同一次 fit 后面分的也算）
            self.solo.update({y["uuid"]: r["name"] for y in cs})
            self.solo_host = None if gpu else r["name"]
        return say, None


def fit_status(p, host, now):
    """fit 读一台的 status.json：(状态, 原因)；读不到或超过 STATUS_STALE_S 没更新的状态为 None、原因写明。"""
    try:
        st = json.loads(read(p.status, "{}") or "{}")
    except ValueError:
        st = {}
    if not st.get("t"):
        return None, "%s 的 worker 没写过状态" % host
    if now - st["t"] > STATUS_STALE_S:
        return None, "%s 的 worker 没在跑（状态 %d 分钟没更新）" % (host, (now - st["t"]) / 60)
    return st, None


def fit_eta(p):
    try:
        return json.loads(read(p.eta, "{}") or "{}")
    except ValueError:
        return {}


def fit(p, names, project=None):
    """要不要为这些任务在这台上开并行车道（labmon worker fit 主机 任务…）：每行与真放行同一个请求、同一份准入 place（FitHost.open），
    判可以的按 acct_job 记进快照再判下一个。卡的利用率只列出来、不据此劝阻（#67：到 100% 的卡上小任务并行 4 条与 6 条每条一样快）；
    利用率低的卡上在跑的被 CPU 卡住、容器在限流时说先加核、先减线程（gpu_slack，#37）。可以开的写同类在那张卡上并行 k 条时约要多久。
    请求按登记过的清单里那一行拼（known_lists → list_row → job_of → queue_req；tag、同队列、cpus=、gpus= 照清单行，项目取登记的）；
    清单里找不到的只按名字找同类（项目缺省取同名任务最近一次的）。"""
    now = time.time()
    st, why = fit_status(p, p.host, now)
    if st is None:
        print("%s：先 labmon worker start，再判断" % why)
        return 2
    hist = load_hist({}) or []
    fh = FitHost(p.host, st, hist, fit_eta(p))
    if not fh.cards:
        print("看不到 GPU")
        return 2
    for x in fh.card_lines():
        print(x)
    lists = known_lists(p)
    for n in names:
        row = list_row(lists, n, project)
        if row:
            host, q, cmd = row
            where = "（清单：%s 上 %s/%s）" % (host, os.path.basename(q["root"].rstrip("/")), q["jobs"])
            r, e, x = fh.req(dict(q, _host=host), n, cmd, None)
        else:
            proj = project or next((hist_key(h, "name")[1] for h in reversed(hist)
                                    if base_name(h.get("name") or "") == base_name(n) and (h.get("project") or h.get("root"))), None)
            where = "（清单里没找到这一行，按名字估；先把它追加到已登记的清单（比如占着卡的那条末尾）再问）"
            r = new_req("queue", None, proj, n)
            e = estimate(hist, r)
            d = duration(hist, r, fh.model())
            x = dict(fh.w.job_spec(n, r, e, d and d["min"]), key="fit:%s" % n, project=proj, allowed=None)
        say, why = fh.open(r, e, x, now, bool(row))
        print("%s：%s%s" % (n, "可以开车道并行：" + say if say else why if "→" in why else why + " → 排在已有车道后面", where))
    return 0


def fit_list(p, root, jobs, project, hosts):
    """一条清单里还会跑、互不依赖的行此刻能在几台主机上再开几条车道（labmon worker fit 主机 ROOT JOBS --list --project P，#35 B）：
    行取清单文件里没标完（.done、.failed）、不在哪台跑着的（rows_left）；写了 after=/afterany=、gang=、占位任务的单列、不分。
    主机是 hosts（看板按共用 worker 目录的在用主机给，config.home_peers；不给只看这台），在这台上读它们的 status.json（共享 HOME，不多连 ssh），
    状态过期的写明没算。按清单顺序逐行，每行先在这次分到车道最少的那台上试（一样少的按 hosts 的顺序），判断同单行的 fit（FitHost.open）；
    哪台都放不下的到此为止，其余排在后面。只回答此刻放得下多少：怎么拆清单、开不开由实验会话定。"""
    now = time.time()
    root = os.path.abspath(root)
    path = os.path.normpath(os.path.join(root, jobs))
    lists = known_lists(p)
    reg = next(((h, q) for h, q, _ in lists if os.path.normpath(os.path.join(q["root"], q["jobs"])) == path), None)
    if reg:
        q = dict(reg[1], _host=reg[0])
        if project and q["project"] != project:
            print("%s 登记在 %s 上，属于项目 %s，不是 %s" % (path, reg[0], q["project"], project))
            return 2
    else:
        if not project:
            print("这条清单还没登记：写 --project <看板项目 id>")
            return 2
        q = load_queue({"_slug": qslug(root, jobs), "root": root, "jobs": jobs, "project": project})
    rows = list_jobs(q)
    if not rows and not os.path.exists(q["jobs_path"]):
        print("读不到清单 %s" % path)
        return 2
    hist, fhs, skip = load_hist({}) or [], [], []
    running = set()
    for h in hosts or [p.host]:
        hp = Paths(h)
        st, why = fit_status(hp, h, now)
        if st is None:
            skip.append(why)
            continue
        running |= {j["name"] for j in st.get("running") or [] if os.path.normpath(os.path.join(j.get("root") or "", j.get("queue") or "")) == path}
        fh = FitHost(h, st, hist, fit_eta(hp))
        hit = fh.w.override_hit({"project": q["project"], "name": ""}, ("drain", "guest", "pause"), lasting=True)
        if hit or not fh.cards:
            skip.append("%s：%s" % (h, hit[1] if hit else "看不到 GPU"))
        else:
            fhs.append(fh)                        # 在限流的照样列出来（各行都写先减线程）
    cur = read_current(q["state"])                # 状态目录里在跑的：放它的主机状态过期了（worker 停了、崩了）任务照样可能在跑，不能再分
    if cur:
        running.add(cur[0])
    _, pend = rows_left(q["state"], rows, None)
    pend = first_rows(pend)
    pend = [(n, c) for n, c in pend.items() if n not in running]
    apart, todo = [], []
    for n, c in pend:
        h = job_of(n, c)
        what = "占位" if is_hold(n) else "成组 gang=%s" % h["gang"] if h["gang"] else "after=" if h["deps"] or h["deps_any"] else None
        (apart if what else todo).append((n, c, what))
    print("清单 %s（%s，项目 %s）：还会跑 %d 行%s" % (path, "登记在 %s 上" % reg[0] if reg else "还没登记", q["project"], len(pend),
                                             "，在跑 %s" % "、".join(sorted(running)) if running else ""))
    if apart:
        print("不分的 %d 行（要等别的任务或成组开跑，留在原处）：%s" % (len(apart), "、".join("%s（%s）" % (n, w) for n, _, w in apart)))
    if skip:
        print("没算：" + "；".join(skip))
    if not fhs:
        print("没有能算的主机")
        return 2
    for fh in fhs:
        fh.pre = fh.card_lines()                  # 卡的样子是开车道之前的
    left = list(todo)
    while left:
        n, c, _ = left[0]
        done = False
        for fh in sorted(fhs, key=lambda f: (len(f.lanes), fhs.index(f))):
            r, e, x = fh.req(q, n, c, None)
            say, why = fh.open(r, e, x, now)
            if say:
                fh.say.append("  %s ← 卡 %s：%s" % (n, ",".join(map(str, fh.lanes[-1][1])), say) if x["ngpu"] else "  %s（不用 GPU）：%s" % (n, say))
                done = True
                break
            fh.why = "%s：%s" % (n, why)
        if not done:
            break
        left.pop(0)
    for fh in fhs:
        print("== %s ==" % fh.host)
        for x in fh.pre:
            print("  " + x)
        if fh.lanes:
            print("  可再开 %d 条车道：" % len(fh.lanes))
            for x in fh.say:
                print("  " + x)
        if fh.why:
            print("  %s不再开：%s" % ("" if fh.lanes else "一条也", fh.why))
    got = sum(len(fh.lanes) for fh in fhs)
    if got:
        print("合计可再开 %d 条车道。照这个在各台新建清单、把分到的行剪过去再 labmon worker add（lists.md「开并行车道」）；%s" % (
            got, "其余 %d 行（%s…）此刻哪台都放不下：留在原清单，或接在新清单后面，让各条车道的行数相近" % (len(left), left[0][0]) if left else "没有剩下的行"))
    else:
        print("此刻哪台都不再开车道：%s" % ("这些行排在已有车道后面" if todo else "没有能分的行"))
    return 0


def hist_key(h, kind):
    """同类记录的键（estimate、duration 用）。没记 project 的旧行按目录名猜，只供估时；份额（share）不认它们。"""
    proj = h.get("project") or (project_of(h["root"]) if h.get("root") else None)
    if kind == "tag":
        return ("tag", proj, h.get("tag"))
    if kind == "name":
        return ("name", proj, base_name(h.get("name") or ""))
    return ("queue", h.get("root"), h.get("queue"))


def ovr_fields(o, act):
    """临时指令事件的结构化字段（时间线按它画 drain / pause / 续租截止的区间）：act 为 add / rm / expire。"""
    return {"act": act, "okind": o["kind"], "target": o.get("target"), "until": o.get("until"), "at": o.get("at")}


def deadline_name(o):
    """截止指令的名字：续租的（无目标）、借用约定归还的（目标 borrow）。"""
    return "约定归还" if o.get("target") == "borrow" else "续租截止"


def sched_ovr(path, now=None):
    """看板写来的 sched.json 里项目级的暂停、优先（主机清单的 [sched.pause]、[sched.focus]，所有主机生效，#96）→ 同名的临时指令，带 global；
    到期的跳过（看板没开、没来得及重写时照样到期）。准入、推演、offers 与状态的 overrides 都按 self.ovr 认，和这台单独下的一样。"""
    try:
        d = json.loads(read(path, "{}") or "{}")
    except ValueError:
        d = {}
    now = time.time() if now is None else now
    return [{"kind": k, "target": str(p), "until": u, "global": True}
            for k in ("pause", "focus") for p, u in sorted((d.get(k) or {}).items()) if not u or u > now]


def ovr_text(o):
    if o["kind"] == "deadline":
        return "%s %s：之前跑不完的不放行" % (deadline_name(o), now_str(o.get("at") or 0)[5:16])
    if o["kind"] == "guest":
        k = o.get("keep")
        return "借来的主机：只给 %s 用，%s" % ("、".join((o.get("target") or "").split(",")) if o.get("target") else "自己的项目",
                                         "对方说好 %s 前不用：不给对方留余量，对方有了新的 GPU 进程就恢复按预留" % now_str(k["away"])[5:16]
                                         if k and k.get("away") else
                                         "给对方留显存 %(vram).0f GiB、内存 %(ram).0f GiB、CPU %(cpu).0f 核，对方超出的部分不放新任务" % k
                                         if k and k.get("hold", True) else "对方有 GPU 进程的卡不放新任务"
                                         + ("，内存超过 %(ram).0f GiB、CPU 超过 %(cpu).0f 核整台不放" % k if k else ""))
    what = {"pause": "暂停 %s", "focus": "优先 %s", "drain": "drain 主机"}[o["kind"]]
    return (what % o.get("target") if "%s" in what else what) + ("，到 %s" % now_str(o["until"])[5:16] if o.get("until") else "，无期限") \
        + ("，立即停" if o.get("now") else "") + ("，所有主机" if o.get("global") else "")


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
    os.makedirs(SLOTS, exist_ok=True)
    rid = "%d-%d" % (os.getpid(), int(time.time()))
    base = os.path.join(SLOTS, rid)

    def bye(*_):
        for ext in (".req", ".why", ".ok"):
            if os.path.exists(base + ext):
                os.remove(base + ext)
        sys.exit(143)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    t0 = time.time()
    write(base + ".req", json.dumps({"pid": os.getpid(), "project": a.project, "name": a.name, "ram": a.ram, "vram": a.vram,
                                     "gpus": a.gpus, "tag": a.tag, "excl": a.exclusive, "stop": a.stop, "log": a.log, "est_min": a.est_min,
                                     "cpus": a.cpus, "io": a.io,
                                     "cmd": " ".join(cmd), "t": t0}, ensure_ascii=False))
    wpid, last = Paths(a.host).pid, t0 - 58
    while True:
        ok = read(base + ".ok")
        if ok:
            ok = json.loads(ok)
            os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(ok.get("gpus", []))
            cpu_env(os.environ, ok.get("cpus") or max(1, job_cpus(CPU_DEFAULT if a.cpus is None else a.cpus)))   # 旧 worker 的 .ok 不带 cpus：按 --cpus 或缺省
            if ok.get("adj"):                     # 借来的主机：撞容器内存上限时内核先杀我们的
                try:
                    with open("/proc/self/oom_score_adj", "w") as f:
                        f.write(str(ok["adj"]))
                except OSError:
                    pass
            os.environ["LABMON_SLOT"] = rid
            os.environ["LABMON_SCRATCH"], os.environ["LABMON_CACHE"] = scratch_dir(a.project, a.name), cache_dir(a.project)
            for d in (os.environ["LABMON_SCRATCH"], os.environ["LABMON_CACHE"]):
                os.makedirs(d, exist_ok=True)
            sys.stdout.flush()
            sys.stderr.flush()
            os.execvp(cmd[0], cmd)
        if time.time() - last >= 60:
            up = alive(read_pid(wpid), ("labmon_worker",))
            print("[labmon slot] %s 等待 %d 秒：%s" % (a.name, time.time() - t0,
                  (read(base + ".why") or "排队中") if up else "worker 不在（labmon worker start %s）" % a.host), file=sys.stderr)
            last = time.time()
        if a.timeout and time.time() - t0 > a.timeout:
            for ext in (".req", ".why"):
                if os.path.exists(base + ext):
                    os.remove(base + ext)
            print("[labmon slot] %s 等了 %d 秒仍未放行，退出" % (a.name, a.timeout), file=sys.stderr)
            return 75
        time.sleep(1)


def pool_refuse(path, stop, gpus):
    """worker add --host pool（交给算力池，#24）不收的清单：返回原因；收为 None。池里的清单会放到借来的服务器上跑，主人回来或收回时要停下重排：
    停法不能是 boundary（等当前任务跑完，收回停不下来），不能限卡（--gpus：各台卡号不同），不能有成组（gang=：借来的随时要还，跨台成组太脆）与占位任务。"""
    if stop == "boundary":
        return "交给算力池的清单不能用 --stop boundary：收回时要停得下来，用 --stop term（缺省）或 --stop file:<相对 ROOT 的路径>"
    if gpus:
        return "交给算力池的清单不能写 --gpus：放到哪台、用哪几张卡由看板定"
    rows = parse_jobs(read(path, "") or "")
    gang = [n for n, c in rows if hints(c).get("gang")]
    hold = [n for n, _ in rows if is_hold(n)]
    if gang:
        return "交给算力池的清单不能有成组的行（gang=）：%s。成组的放到自己的主机上跑" % "、".join(gang[:3])
    if hold:
        return "交给算力池的清单不能有占位任务：%s。要等别的任务就写 after=" % "、".join(hold[:3])
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["slot"]:
        return slot_main(argv[1:])
    ap = argparse.ArgumentParser(prog="labmon_worker.py")
    ap.add_argument("cmd", choices=["run", "add", "rm", "ls", "stop", "limit", "override", "fit", "thaw"])
    ap.add_argument("args", nargs="*", help="add / rm：ROOT JOBS；limit：tag=N…；override：add|rm KIND [目标] 或 ls；fit：任务名…（--list 时 ROOT JOBS）")
    ap.add_argument("--host", required=True)
    ap.add_argument("--idle", action="store_true", help="add：仅在清单空闲时更换资源")
    ap.add_argument("--gpus", help="add：只用这些卡（nvidia-smi 序号，逗号分隔）")
    ap.add_argument("--project", help="add：看板的项目 id（必写）；fit：按这个项目的历史估（缺省取同名任务最近一次的项目）")
    ap.add_argument("--expect-project", help="add / rm：在登记锁内核对这条清单所属的项目")
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
    ap.add_argument("--replace", action="store_true", help="run：已有 worker 时让它退出后接管（升级用）")
    ap.add_argument("--shared", help="run：组里共用的那块盘的挂载点（如 /root/data1），给了就参与统计它的用量")
    a = ap.parse_args(argv)
    p = Paths(a.host)
    if a.cmd == "run":
        return Worker(a.host, a.shared).run(a.replace)
    if a.cmd == "fit":
        if a.list:
            if len(a.args) != 2:
                ap.error("fit --list 需要 ROOT JOBS")
            return fit_list(p, a.args[0], a.args[1], a.project, [h for h in (a.hosts or "").split(",") if h])
        if not a.args:
            ap.error("fit 需要任务名")
        return fit(p, a.args, a.project)
    if a.cmd in ("add", "rm"):
        if len(a.args) != 2:
            ap.error("add / rm 需要 ROOT JOBS")
        root = os.path.abspath(a.args[0])
        if a.cmd == "add":
            try:
                queue_stop_path(a.stop, root)
            except (OSError, ValueError) as e:
                print(str(e), file=sys.stderr)
                return 2
        # 核对项目、查状态目录、登记与搬走，整段经同一把登记锁。
        with reg_lock(required=bool(a.expect_project)):
            dirs = reg_dirs()
            old_regs = matching_regs(p.queues, root, a.args[1])
            if a.expect_project:
                matches = list(old_regs)
                if a.cmd == "add":
                    for d in dirs.values():
                        matches.extend(matching_regs(d, root, a.args[1], aliases=True))
                if any(r.get("project") != a.expect_project for _, r in matches) or (a.cmd == "add" and a.project != a.expect_project):
                    print("登记归属已变：请刷新后重试，或找管理员", file=sys.stderr)
                    return 2
            old = [f for f, _ in old_regs]
            if a.cmd == "rm":
                if a.expect_project and not old:
                    print("这条登记已不存在：请刷新后重试", file=sys.stderr)
                    return 2
                for f in old:
                    os.remove(f)
                print("已移除 %s/%s" % (root, a.args[1]) if old else "没登记过 %s/%s" % (root, a.args[1]))
                return 0
            if not a.project:
                print("add 必须写 --project <看板项目 id>（monitor.toml 的 id）", file=sys.stderr)
                return 2
            if not os.path.exists(os.path.join(root, a.args[1])):
                print("清单不存在：%s/%s" % (root, a.args[1]), file=sys.stderr)
                return 2
            if a.host == POOL:
                bad = pool_refuse(os.path.join(root, a.args[1]), a.stop, a.gpus)
                if bad:
                    print(bad, file=sys.stderr)
                    return 2
                a.stop = a.stop or "term"         # 池里的清单收回时要停得下来：缺省按 term 停
            guest = next((o for o in json.loads(read(p.overrides, "[]") or "[]") if o.get("kind") == "guest" and o.get("target")), None)
            if guest and a.project not in guest["target"].split(","):
                print("主机 %s 是借来的，只给 %s 用（这条队列的项目是 %s）" % (a.host, "、".join(guest["target"].split(",")), a.project), file=sys.stderr)
                return 2
            os.makedirs(p.queues, exist_ok=True)
            f = old[0] if old else os.path.join(p.queues, qslug(root, a.args[1]) + ".json")
            if a.expect_project and not old and os.path.lexists(f):
                print("登记文件名已被占用：请找管理员", file=sys.stderr)
                return 2
            reg = {"root": root, "jobs": a.args[1], "added": now_str(), "project": a.project}
            if a.gpus:
                reg["gpus"] = [int(x) for x in a.gpus.split(",")]
            if a.stop:
                reg["stop"] = a.stop
            # 两条队列共用状态目录：current、lock、完成标记会互相覆盖。各主机共用同一块盘，别的主机上登记的也算
            st = load_queue(dict(reg, _slug="new"))["state"]
            if a.idle and os.path.exists(os.path.join(st, "current")):
                print("清单正在运行或结果待核对，不能更换资源", file=sys.stderr)
                return 2
            conflict = queue_state_conflict(load_queue(dict(reg, _slug="new")), a.host, dirs)
            if conflict:
                print(conflict, file=sys.stderr)
                return 2
            for x in old[1:]:
                os.remove(x)
            write(f, json.dumps(reg, ensure_ascii=False))
            # 同一条清单只登记在一台（#95）：别的主机（含已归还的、主机清单里已没有的，那边的 worker 删不了）上的登记搬过来，否则两台轮流放这条清单。
            # 那台手上正在跑的照样跑完，这台等它跑完再接着放（requests 里 run_elsewhere 挡着）。
            # 只删比这次登记旧的（共享盘修改时间，一样新按主机 id，同 Worker.registry 的认法）：两个会话同时登记到两台时只留晚的那份，不会互相删光
            mine, moving, newer = (os.stat(f).st_mtime, a.host), {}, []
            for h, d in dirs.items():
                for x in (reg_files(d, root, a.args[1]) if h != a.host else []):
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
                gone = set(json.loads(read(p.returned, "{}") or "{}").get("hosts") or [])
            except (ValueError, AttributeError, TypeError):
                gone = set()
            cur = read_current(st)
            busy = cur and run_elsewhere(a.host, {"state": st, "root": root, "jobs": a.args[1]}, cur, gone)
            for h in moved:
                for x in moving[h]:
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(x)
                ev = os.path.join(os.path.dirname(dirs[h]), "events.jsonl")
                with flock(ev):
                    append_jsonl(ev, {"t": round(time.time()), "kind": "worker", "msg": "登记搬到主机 %s：%s/%s，这台不再跑它" % (a.host, root, a.args[1]),
                                      "project": a.project, "root": root, "queue": a.args[1]})
            if moved:
                os.makedirs(p.dir, exist_ok=True)
                with flock(p.events):
                    append_jsonl(p.events, {"t": round(time.time()), "kind": "worker", "msg": "登记从主机 %s 搬来：%s/%s" % ("、".join(moved), root, a.args[1]),
                                            "project": a.project, "root": root, "queue": a.args[1]})
        print("已登记 %s/%s → 主机 %s" % (root, a.args[1], a.host)
              + ("（从主机 %s 搬过来，那边不再跑它%s）" % ("、".join(moved), "；主机 %s 上正在跑的 %s 跑完后这台接着跑" % (busy, cur[0]) if busy else "") if moved else ""))
        return 0
    os.makedirs(p.dir, exist_ok=True)
    if a.cmd == "thaw":                           # 还卡时（borrow.finish）恢复暂停的看门狗
        print(thaw(p) or "没有暂停的看门狗")
        return 0
    if a.cmd == "limit":
        lim = json.loads(read(p.limits, "{}") or "{}")
        tags = lim.setdefault("tags", {})
        for kv_ in a.args:
            k, _, v = kv_.partition("=")
            if v.strip():
                tags[k] = int(v)
            else:
                tags.pop(k, None)
        if a.args:
            write(p.limits, json.dumps(lim, ensure_ascii=False))
        print("tag 限额：" + ("，".join("%s=%d" % kv_ for kv_ in sorted(tags.items())) or "无"))
        return 0
    if a.cmd == "override":
        if a.args[:1] in (["add"], ["rm"]):
            if len(a.args) < 2 or a.args[1] not in ("pause", "focus", "drain", "deadline", "guest"):
                ap.error("override add|rm pause|focus|drain|deadline|guest [目标]")
            kind, target = a.args[1], (a.args[2] if len(a.args) > 2 else None)
            with flock(p.overrides):              # worker 同时在改（到期清理、pause --now 记 applied）
                cur = json.loads(read(p.overrides, "[]") or "[]")
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
                    ovr.append({"kind": kind, "target": target, "until": a.until, "now": a.now, "t": time.time(),
                                **({"at": a.at} if a.at else {}), **({"keep": a.keep} if a.keep else {}), **({"once": True} if a.once else {}),
                                **({"by": a.by} if a.by else {})})
                write(p.overrides, json.dumps(ovr, ensure_ascii=False))
            if a.once:                            # 只停一次：暂停的记录由看板记（全局的那条），停下的任务 worker 各记一条 stop
                print("下一轮按停法停下 %s 正在跑的" % target)
                return 0
            if a.args[0] == "add":
                msg, kw = "临时指令：" + ovr_text(ovr[-1]), ovr_fields(ovr[-1], "add")
            else:
                msg, kw = "撤销临时指令：%s %s" % (kind, target or ""), ovr_fields({"kind": kind, "target": target}, "rm")
            with flock(p.events):
                append_jsonl(p.events, dict(kw, t=round(time.time()), kind="override", msg=msg))
        else:
            ovr = json.loads(read(p.overrides, "[]") or "[]")
        ovr = [o for o in ovr if not o.get("once")] + sched_ovr(p.sched)
        print("生效中：" + ("；".join(ovr_text(o) for o in ovr) or "无"))
        return 0
    pid = read_pid(p.pid)
    up = alive(pid, ("labmon_worker",))
    if a.cmd == "stop":
        write(p.stopped, now_str())               # 本机看板见到它就不自动拉起
        if up:
            os.kill(pid, signal.SIGTERM)
            print("已通知 worker（PID %d）退出；运行中的任务继续跑" % pid)
        else:
            print("worker 没在运行")
        return 0
    st = json.loads(read(p.status, "{}") or "{}")
    print("worker：" + ("运行中 PID %d" % pid if up else "未运行") + ("（状态 %s）" % now_str(st["t"]) if st.get("t") else ""))
    if (st.get("gate") or {}).get("on"):
        print("  内存闸门关闭中：停止放行")
    for o in st.get("overrides", []):
        print("  临时指令 " + ovr_text(o))
    wd = load_frozen(p)                           # 直接读 frozen.json：刚拉起、状态还没写时也看得到
    if wd["frozen"]:
        print("  暂停着的保活看门狗（还卡时恢复）：" + wd_text(wd["frozen"]))
    if wd["seen"]:
        print("  找到的保活看门狗（没动）：" + wd_text(wd["seen"]))
    for ln in ka_lines(st):
        print("  " + ln)
    th = st.get("throttle")
    if th:
        print("  CPU 被配额限流 %s%%（%s 起，%d 分钟%s）%s" % (th.get("thr"), now_str(th["since"])[5:16], th["for"] // 60, "，已告警" if th["alert"] else "",
                                                    "；超核：" + "、".join("%s/%s %.1f 核（给 %d）" % (o["project"], o["name"], o["cores"], o["give"]) for o in th["over"]) if th["over"] else ""))
    if (st.get("limits") or {}).get("tags"):
        print("  tag 限额 " + "，".join("%s=%d" % kv_ for kv_ in sorted(st["limits"]["tags"].items())))
    for q in st.get("queues", []):
        print("  队列 %s %s/%s%s%s" % (q.get("project"), q["root"], q["jobs"], "（卡 %s）" % q["gpus"] if q.get("gpus") else "",
                                     "（空着：清单里没有待办，不占卡）" if q.get("empty") else ""))
    for j in st.get("running", []):
        gang = "，成组 %s（与 %s 一起开跑）" % (j["gang"], "、".join("%s（%s）" % (w[1], w[0]) for w in j.get("gang_with") or []) or "—") if j.get("gang") else ""
        print("  运行 %s：%s（PID %d，%s 起，GPU %s%s）" % (j.get("queue") or "单元 " + str(j.get("project")), j["name"], j["pid"], now_str(j["t0"]),
                                                     ",".join(j["gpus"]) or "无", gang))
    for w in st.get("waiting", []):               # 排位与预计开始取状态里的 pos、start，与看板的等待表一致；被临时指令挡住的没有排位
        pos, start = w.get("pos"), w.get("start")
        rank = "（排第 %d%s，预计开始 %s）" % (pos["n"], "，屏障" if pos.get("barrier") else "",
                                          ("马上" if start <= st.get("t", 0) + 60 else now_str(start)[5:16]) if start else "—") if pos else ""
        print("  等待 %s%s：%s%s" % (w.get("queue") or "单元 " + str(w.get("project")), " 的 " + w["name"] if w["name"] else "", w["why"], rank))
    for ln in (st.get("plan") or {}).get("lanes", []):     # 推演里还没开始、写了依赖的：等谁、预计何时开始
        for j in ln.get("jobs", []):
            if j.get("deps") and not j.get("run"):
                ok, done = ([tuple(d[:2]) + (0,) for d in j["deps"] if (len(d) > 2) == a] for a in (False, True))
                what = "、".join(x % dep_names(k, ln.get("project")) for x, k in (("%s 跑成功", ok), ("%s 跑完", done)) if k)
                print("  依赖 %s 的 %s：等 %s，预计 %s 开始" % (ln.get("queue") or "单元", j["name"], what, now_str(j["t0"])[5:16]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
