"""worker 基础常量、环境变量路径、文件与锁、JSONL 与通用辅助函数（Python 3.8 标准库）。"""
import contextlib
import fcntl
import glob
import json
import os
import re
import subprocess
import time
from pathlib import PurePosixPath

from . import pure as _P
from .pure import (  # noqa: F401 — 经 common 与包顶层兼容导出
    GIB,
    GROUP_RE,
    HOLD_RE,
    IDLE_REG_S,
    OVER_FRAC,
    OVER_MIN_S,
    POOL,
    STALE_S as STATUS_STALE_S,
    is_hold,
    overrun_left,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser(os.environ.get("LABMON_HOME") or BASE_DIR)


TICK = float(os.environ.get("LABMON_TICK", "10"))


KA_CHECK_S = 3 * TICK    # 空闲时多久看一次要不要拉起保活


KA_BIN = os.environ.get("KA_BIN", os.path.join(HOME, "keepalive", "bin"))   # 保活的配置 ka.env 所在目录（主机清单的 ka_bin）


KA_ENV = os.path.join(KA_BIN, "ka.env")


KA_SH = os.environ.get("KA_SH") or os.path.join(BASE_DIR, "ka.sh")   # 停启保活的脚本：与 worker 一起发来的那份（#105）


PROC = os.environ.get("LABMON_PROC", "/proc")          # 只用于统计用量；测试时指向假目录


CG = os.environ.get("LABMON_CGROUP", "/sys/fs/cgroup")


NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


DATA_ROOT = os.path.expanduser(os.environ.get("LABMON_DATA_ROOT") or os.path.dirname(HOME.rstrip("/")))  # 工作根目录（优先 LABMON_DATA_ROOT，兼容旧反推）


LOCAL = os.path.expanduser(os.environ.get("LABMON_LOCAL") or "/tmp/labmon")   # 本地临时目录：单格临时、项目缓存、单元申请


ROOTFS = os.environ.get("LABMON_ROOTFS", "/")           # 容器根盘（续租、容器重启会清空）：开机以来的新写入按它算、扫它（LocalDisk）；测试指到假目录


SLOTS = os.environ.get("LABMON_SLOTS", os.path.join(LOCAL, "slots"))   # 单元申请的交换目录：容器本地盘，不走 NFS


GATE = 0.9               # 容器里收不回的内存（mem.anon，不算页缓存）超过上限的这个比例就停止放行


FAILCNT_WIN_S = 300      # 高压期间容器 memory.failcnt 事件的聚合折叠时间窗（秒）：每窗最多记一条带累计增量的摘要（#262）


DISK_WARN_GB = 20.0      # 独立主机本地盘预警线（GiB）


DISK_STOP_GB = 10.0      # 独立主机本地盘准入停止线（GiB，0 关闭）


DISK_HYST_GB = 2.0       # 磁盘闸门滞回恢复带宽（GiB）：余量回升至 stop+2 且占用降至 quota-2 才自动解除


UNACCT_VRAM_MB = 512.0   # 未归因显存差额扣减门槛（MB，#114）：>= 512 MB 才扣进 other["vram"]，过滤驱动/桌面合成器底噪


UNACCT_BUSY_MB = 1536.0  # 外部活跃占卡显存触发门槛（MB，1.5 GiB，#114）：达到即视为外部活跃占卡，默认整卡暂停放新任务


UNACCT_CLEAR_MB = 1024.0 # 外部活跃占卡显存滞回解除门槛（MB，1.0 GiB，#114）：降至其下且利用率冷却才解除，防边缘抖动


UNACCT_UTIL_HOT = 15.0   # 无可见 GPU 进程时外部活跃占卡利用率触发门槛（%，#114）


UNACCT_UTIL_COOL = 5.0   # 外部活跃占卡利用率滞回解除门槛（%，#114）


MACOS_RESERVE_CPUS = 2   # macOS 桌面默认保留核数（#114）：避免实验把笔记本 CPU 吃死，可经 cpu_quota 覆盖


MPS_MEM_FRAC = 0.65      # Apple Silicon MPS 逻辑卡默认显存上限占统一内存比例（#114）


MPS_MEM_RESERVE_GIB = 4.0  # Apple Silicon MPS 逻辑卡默认至少为 macOS 系统保留的统一内存（GiB，#114）


WAIT_CAP_S = 1800        # 缺省：请求卡在资源上等了这么久，排在它后面的就不能再越过它，只能回填（预计在它放行前跑完的才放）；sched.json 的 wait_cap_min 可改


HALF_LIFE_S = 86400      # 缺省：项目之间比的 GPU 用量（卡·时）按这个半衰期衰减；sched.json 的 half_life_h 可改


ONCE_HOLD_S = 1800       # 不带 --host 的 pause --now 下的“只停一次”：等全局暂停随 sched.json 送到才删，最多挡这么久（看板约 1 分钟一轮）


POLICY = {"cap": WAIT_CAP_S, "hl": HALF_LIFE_S, "w": {}, "own": {}}   # 调度设置的缺省（policy 读 sched.json 覆盖）：w 为各项目权重，不写为 1；own 为项目 → 人员键，没写的项目合起来算一个人


PEER_EVERY = 60          # 别的主机在跑的任务（各自状态里的 share.run，公平份额要算上）多久重读一次


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


GUEST_RAM_HOT = 2        # 借来的主机：对方内存连着几轮超出滞回上限才算超出预留（与 cpu_hot >= 2 对称，挡住单轮抖动）


GUEST_RAM_HYST_MIN = 0.5 # 借来的主机：对方内存超出预留判定的最小滞回缓冲（GiB）


GUEST_RAM_HYST_FRAC = 0.1  # 借来的主机：对方内存超出预留判定的相对滞回比例（相对 keep["ram"]）


TIGHT_FREE_MB = 1024     # 借来的主机：对方超出预留、某卡空闲显存不到这么多时算快撞上限（提醒，不停正在跑的）


TIGHT_MEM = 0.95         # 借来的主机：对方超出预留、容器里收不回的内存（不算页缓存）到上限的这个比例时算快撞上限


SQUEEZE_MAX = 2          # 借来的主机上同一任务被对方挤掉（OOM）后重新排队的次数上限，再被挤掉就记失败


GUEST_ADJ = 1000         # 借来的主机上我们任务的 oom_score_adj：撞容器内存上限时内核先杀我们的，不杀对方的


CUDA_OOM = re.compile(r"CUDA out of memory|OutOfMemoryError|CUDA error: out of memory|CUBLAS_STATUS_ALLOC_FAILED|MPS backend out of memory")


VOID_CPU = 1.0           # 整机独占时 labmon 之外的进程占这么多核算打扰


IDLE_CORES = 0.2         # 不用卡、平均不到这么多核的任务（如等单元放行的监督进程）：整机独占不等它，也不算打扰


VOID_KEEP_S = 86400      # 作废记录在状态里留这么久（看板据此告诉实验会话）


HIST_N = 5               # 估计值看同类任务最近几次


HIST_RESET_KEEP_S = 90 * 86400   # 历史峰值水印（hist_reset.json，#300）陈旧规则保留期（90 天）


OUTLIER_DROP_N = 3       # 内存/显存连续至少这么多次跑成功（ran_ok）且峰值均低于窗口最大值的 OUTLIER_DROP_FRAC 时，淘汰旧离群高点（peak_rows，#294）


OUTLIER_DROP_FRAC = 0.5  # 连续瘦身离群淘汰的比例门槛（严格低于窗口全量最大值的 50%，#294）


RATIO_N = 20             # 借来的 实际/估时 比值（同 tag、同前缀、同一清单、本项目）各看最近这么多个跑成功的任务


RATIO_OK = (1 / 3, 3.0)  # 实际/估时 在这个范围里才算当时的估时可信：才拿它的比值折算改过的估时（#134：一次写错的 est_min=3 把比值抬到 ×11）；借给别的名字时不在里面的另看 borrowable（#139）


DUR_MARGIN = 1.2         # 回填与截止前（续租、约定归还）的准入：预计时长再乘这么多（估时偏一点是常事，跑不完这一格就白跑）；截止另与同类最长的一次取大（with_cut）


CUT_EST_MARGIN = 1.5     # 截止前的准入：时长只来自清单估时或别的型号的记录时乘这么多（更没把握，#139）


UTIL_WIN_S = 600         # 各卡利用率取近这么久的平均（状态的 util10，fit 列出来看；吃满程度 gpu_busy 的 m10 同一个长度）


BUSY_WIN_S = 3600        # 各卡「吃满程度」（只有保活时按 0）留近这么久的样本：状态 gpu_busy 的 h1（页面主数字，#51）


UTIL_LOW = 50            # 卡近 UTIL_WIN_S 的利用率低于这个 %，且上面在跑的被 CPU 卡住时，先加核、不在这张卡上开新车道（gpu_slack，#35、#37）


CPU_BOUND_FRAC = 0.9     # 在跑任务近 CPU_FIT_WARM_S 各 1 分钟窗口核数的平均不少于给它的核数的这么多，算被 CPU 卡住、可能给少了（cpu_fit 的 low）


CPU_SPARE_FRAC, CPU_SPARE_MIN = 0.5, 1.0   # cpu_fit 的 high 告警：写了 cpus= 的在跑任务整段 1 分钟峰值不到给的核数的这么多、且少至少这么多核，算给多了（#46）；cpu_shrink 也复用 CPU_SPARE_MIN（至少小 1 核才收敛，#292）


CPU_SHRINK_N = 1         # 写了 cpus= 的同类至少这么多条有效样本（完赛/跑满 5 分钟历史或满 CPU_LIVE_SHRINK_S 在跑轨迹）且按实测峰值加裕量至少少 CPU_SPARE_MIN 核时，没开跑的连续收敛记账（cpu_shrink，#91、#292）


UTIL_FULL = 90           # 卡近 UTIL_WIN_S 的利用率不低于这个 % 算吃满：offers 的同机多开车道不往它上面放（Worker.local_cards，#43；同卡 1 条车道常只到六七成，3–4 条才近满）


UNDER_WIN_S = 1800       # offers 的 room：能放的卡近这么久的「吃满程度」平均（样本不满这么久为 None），看板据此判这台吃不满、提示多开车道（#145）


ROOM_MAX = 32            # offers 的 room：此刻至多数到能再开这么多条车道（Worker.lane_room）


NEAR_TOL = 0.25          # 按同卡并行数挑记录（near）：与 k 按比例的差 |c − k| / k 在最接近的那条再多这么多以内算同一档（#138：1 条与 2 条并行分开）


CONC_MAX = 12           # 按同卡并行数挑同类记录（duration 的 k）时封顶到这么多（推演里查表、缓存用）


FORECAST_S = 7 * 86400   # 推演（simulate）往后排多远


FORECAST_EVERY = 120     # 推演的输入没变时多久重算一次（在跑的超了预计，后面的跟着往后挪）


MOVE_MAX = 20            # 能接别处的任务（offers）：每个项目一次至多挑几行（从排得最晚的清单队尾往前取；挪来的几行比原处剩下的晚跑完就不再往下挑，


                         # Worker.moved_past）。以前在原处跑得了的只挑 5 行：行短、晚得多时（40 行串行排在同卡别的车道后面，#110）挪满 5 行也不到门槛，一直不建议
MOVE_LEAD_S = 600        # 原处预计这么久内就开跑的不挑：会话剪行时它可能正好放行，两边都跑


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


IO_WHAT = {"scan": "共享盘扫描", "lscan": "本地盘扫描", "wscan": "本地工作盘扫描"}   # io_yield 推迟的事（键）在 worker.log 里的说法；别的键是拷本地依赖 (请求 key, 任务)


SLOW_RATIO = 1.5         # 运行中任务已跑过同类历史预计时长的这么多倍、同时共享盘慢：记一条“读写拥塞嫌疑”（只记录）


CPU_DEFAULT = 1          # 没写 cpus=、也没有同类历史时一个任务按几个核记账（#4 调研推荐、维护者定）


GPU_CPU_RESERVE_PER_CARD = 2   # 有 GPU 的主机每张卡为 GPU 任务预留的保底 CPU 核数（#225）


GPU_CPU_RESERVE_DIV = 4        # GPU 保底 CPU 核数最多占容器配额上限的 1/4（#225）


CPU_ONLY_NICE = 10             # GPU 主机上纯 CPU 任务启动时的 nice 增量（降低 OS 调度优先级，保障 GPU 进程时间片，#225）


CPU_ALERT_S = 600        # 请求一直因 CPU 被挡满这么久，等待项才写 cpu_wait（看板据此告警）


CPU_CODES = ("cpu", "cpu_est", "cpu_other", "nofit_cpu")   # 算“因 CPU 被挡”的原因代码（WHY 的键）：记账不够、记账比实测多挡住的、labmon 之外占着、要的比配额还多


CPU_ALERT_CODES = ("cpu_est", "cpu_other", "nofit_cpu")   # 其中要告警的（cpu_wait）：核真被我们自己的任务用着的（cpu）是正常排队，不报（#65 维护者定）


CPU_WARM_S = 180         # 运行中任务开跑（接管后从开始量起）满这么久，CPU 记账从估计改按近这么久的 1 分钟峰值（回收开头加载阶段之后没用上的核，#65、#292）


CPU_FIT_WARM_S = 600     # cpu_fit 核数给多/给少告警与 gpu_slack 的防误报观察窗（秒，与 UTIL_WIN_S 对齐，#46、#292）


CPU_LIVE_SHRINK_S = CPU_FIT_WARM_S   # 同变体在跑任务作为排队 cpu_shrink 实测峰值轨迹的稳定观察期（秒，#292）


MIB = 2 ** 20


PAR_RE = re.compile(r"^[ \t]*#[ \t]*labmon:.*?(?<![\w-])par=(\d+)", re.M)   # 清单声明的并行数 `# labmon: par=N`（list_par，#147）


PAR_MAX = 32             # 一条清单在一台上最多同时跑这么多行


PAR_STEP_S = 60          # 并行清单已有行在跑时，两次往上加行至少隔这么久：让卡的利用率来得及反映新加的那行（加行门槛 Worker.par_gate）


PAR_REFILL_S = UTIL_WIN_S   # 并行清单近这么久同时跑到过的行数（Worker.par_hi）：跑完一行后补回这个数不等 PAR_STEP_S、不看利用率（#176）


GPAR_TRANSIENT_S = 600   # 推演里瞬态限流（如 CFS 限流）收窄并行窗口（wpar / gpar）的恢复窗（秒）：过了此时刻自动解除，不外推至全队列（#297）


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


def queue_root_ok(root, data_root=None):
    """校验独立主机登记的清单目录 ROOT 位于 <data_root>/projects/<项目>/ 内（经 realpath 防符号链接越界）。"""
    dr = os.path.expanduser(data_root or DATA_ROOT)
    proj = os.path.realpath(os.path.join(dr, "projects"))
    target = os.path.realpath(os.path.expanduser(root))
    try:
        inside = target != proj and os.path.commonpath((proj, target)) == proj
    except ValueError:
        inside = False
    if not inside:
        raise ValueError(
            "独立主机须在 %s/projects/<项目>/ 下运行，避免日志与产物污染原代码仓库"
            "（如确需原地运行，可在 hosts.toml 设 allow_outside_root = true）" % dr.rstrip("/")
        )
    return target


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


def posix_fallback_ok():
    """无 /proc 时是否启用轻量 POSIX（macOS/BSD）采集回退（#114）：
    - LABMON_NO_POSIX=1 强制关闭；
    - LABMON_POSIX_PROBE=1 强制开启（单测显式测回退路径用）；
    - 缺省仅当 PROC 指向真实 "/proc" 且该目录不存在时启用（Linux 环境与指向假目录的测试夹具均不触发）。"""
    if os.environ.get("LABMON_NO_POSIX") == "1":
        return False
    if os.environ.get("LABMON_POSIX_PROBE") == "1":
        return True
    return PROC == "/proc" and not os.path.isdir(PROC)


def cmdline(pid):
    """进程命令行；不存在或已成僵尸返回 None。没有 /proc 时（macOS/BSD 与本机测试）退回 ps。"""
    if not os.path.isdir("/proc"):
        try:
            r = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,args="], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
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
        self.hist_shared = os.path.join(self.dir, "hist_shared.json")  # 看板经 put 转送给独立主机的跨机同类历史（只供 estimate/duration，不进 share/dep_state/disk_ends）
        self.scratch_lock = os.path.join(self.dir, "scratch.lock")     # Worker.start/grant 与 clean_local 互斥锁，防扫描到删除窗口误删新起任务
        self.active_scratch = os.path.join(self.dir, "active_scratch.json")  # 锁内登记的运行中单格临时 {proj/job: pid}


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


def intruders(scope, cards, jobs, gp, cpu, me=None, unacct=None):
    """独占任务会被谁打扰（scope 为 gpu 看 cards 这几张卡，host 看整台主机），分两类返回：
    labmon 的（别的任务、GPU 保活：会跑完或由 worker 停下，等腾空时值得留卡）与 labmon 之外的（借来的主机上多半是对方，留卡也不会走）。
    放行前（等腾空）与运行中（作废）共用：同卡（整机：同机）别的任务与 GPU 进程，未归因外部活跃占卡（unacct，#114），
    整机另算 labmon 之外的进程占 VOID_CPU 核以上（cpu）。
    gp 是 sample 认过归属的 GPU 进程，me 是这个任务自己。"""
    on = set(cards)
    ours = {o["name"] for o in jobs if o is not me and not is_hold(o["name"]) and not idle_job(o)
            and (scope == "host" or on & set(o["gpus"]))}
    theirs = set()
    for g in gp:
        if (me is None or g["_j"] is not me) and (scope == "host" or g["uuid"] in on):
            (theirs if g["owner"] == "other" else ours).add(gp_label(g))
    for uu, u in (unacct or {}).items():
        if u and u.get("busy") and (scope == "host" or uu in on):
            theirs.add(u.get("why") or "外部（如宿主机）占卡")
    if scope == "host" and cpu is not None and cpu > VOID_CPU:
        theirs.add("labmon 之外的进程占 %.1f 核" % cpu)
    return sorted(ours), sorted(theirs)


def base_name(name):
    """同类任务：去掉末尾的种子、重复编号（x4_s146_seed0 → x4_s146）。"""
    return re.sub(r"([_-](s|seed|rep)\d+)+$", "", name)


def parse_lstart(text):
    """解析 `LC_ALL=C ps -p <pid> -o lstart=` 输出（如 `Fri Nov 27 11:51:05 2026`）为整型 epoch 秒；失败返回 None。"""
    s = " ".join((text or "").split())
    if not s:
        return None
    try:
        return int(time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError):
        return None


def parse_boottime(text):
    """解析 macOS/BSD `sysctl -n kern.boottime`（如 `{ sec = 1794381005, usec = 776306 } ...`）为 epoch 秒；失败返回 None。"""
    m = re.search(r"\bsec\s*=\s*(\d+)", text or "")
    if not m:
        return None
    sec = int(m.group(1))
    um = re.search(r"\busec\s*=\s*(\d+)", text or "")
    return sec + (int(um.group(1)) / 1e6 if um else 0.0)


def pstart(pid):
    """进程启动时刻（Linux 为时钟滴答，macOS/BSD 无 /proc 回退为整型 epoch 秒）：认 PID 有没有被复用；读不到为 None。"""
    try:
        with open(os.path.join(PROC, str(pid), "stat")) as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        pass
    if not posix_fallback_ok():
        return None
    try:
        env = dict(os.environ, LC_ALL="C")
        r = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, timeout=5, env=env)
        if r.returncode == 0:
            return parse_lstart(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def started_at(pid):
    """进程启动时刻（epoch 秒）= Linux `/proc/stat` btime + pstart / SC_CLK_TCK；
    macOS/BSD 无 /proc 时：pid == 1 优先读 `sysctl -n kern.boottime`，其余回退 `pstart(pid)`（epoch 秒）；读不到为 None。
    容器/主机启动时刻（host_info 取 PID 1）、保活进程什么时候起来的（ka_groups）共用。"""
    try:
        with open(os.path.join(PROC, str(pid), "stat")) as f:
            t = int(f.read().rsplit(")", 1)[1].split()[19])
        btime = int(next(ln.split()[1] for ln in (read(os.path.join(PROC, "stat"), "") or "").splitlines() if ln.startswith("btime")))
        return btime + t / os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, IndexError, ValueError):
        pass
    if not posix_fallback_ok():
        return None
    if pid == 1:
        try:
            r = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                bt = parse_boottime(r.stdout)
                if bt is not None:
                    return bt
        except (OSError, subprocess.TimeoutExpired):
            pass
    v = pstart(pid)
    return float(v) if v is not None else None


def fsig(path):
    """文件变没变的签名 (inode, 修改时刻纳秒, 大小)；读不到抛 OSError（唯一实现在 pure.file_sig）。"""
    return _P.file_sig(path, strict=True)


def load_jsonl(path):
    """JSONL 的全部行（用量记录、事件、主机压力历史共用；写坏的行跳过，解析唯一实现在 pure.parse_jsonl）。"""
    return _P.parse_jsonl(read(path, ""))


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


def load_hist(cache, host=None, standalone=False):
    """各主机的 usage.jsonl（共享盘上同一个 HOME）：资源与时长都看所有主机的同类记录，借来的主机一上来就有历史。
    独立主机（standalone=True）只读本机 usage.jsonl，并一并读入看板经 put 转送的 hist_shared.json（标 shared_hist=True，同 (host, t0, name) 本机优先去重）。
    shared_hist 仅用于 estimate/duration，share()/dep_state()/disk_ends() 显式跳过它。
    cache 为 {文件: (fsig, 记录)}，文件没变不重读；有变化返回按结束时刻排好的全部记录（记上主机），没变返回 None。
    几秒内就结束、没量到用量的（ram_peak 为空）也留着：依赖要看到这次的结果（别的主机上起不来就失败的），资源估计（estimate）自己跳过它们。"""
    changed = False
    usage_files = [os.path.join(HOME, "hosts", host, "usage.jsonl")] if (standalone and host) else glob.glob(os.path.join(HOME, "hosts", "*", "usage.jsonl"))
    shared_files = [os.path.join(HOME, "hosts", host, "hist_shared.json")] if host else glob.glob(os.path.join(HOME, "hosts", "*", "hist_shared.json"))
    live = set()
    for f in usage_files:
        try:
            m = fsig(f)
        except OSError:
            continue
        live.add(f)
        if cache.get(f, (None,))[0] != m:
            hname = os.path.basename(os.path.dirname(f))
            cache[f] = (m, [dict(h, host=h.get("host") or hname) for h in load_jsonl(f)])
            changed = True
    for sf in shared_files:
        try:
            m = fsig(sf)
        except OSError:
            if sf in cache:
                cache.pop(sf, None)
                changed = True
            continue
        live.add(sf)
        if cache.get(sf, (None,))[0] != m:
            try:
                raw = json.loads(read(sf, "") or "{}")
                rows = raw.get("rows") if isinstance(raw, dict) else []
                rows = [dict(r, shared_hist=True) for r in (rows or []) if isinstance(r, dict)]
            except ValueError:
                rows = []
            cache[sf] = (m, rows)
            changed = True
    for k in list(cache):
        if k not in live and (k.endswith("hist_shared.json") or (standalone and k.endswith("usage.jsonl"))):
            cache.pop(k, None)
            changed = True
    if not changed:
        return None
    local_rows, shared_rows = [], []
    for _, rows in cache.values():
        for h in rows:
            (shared_rows if h.get("shared_hist") else local_rows).append(h)
    seen = {(h.get("host"), h.get("t0"), h.get("name")) for h in local_rows}
    merged = local_rows + [h for h in shared_rows if (h.get("host"), h.get("t0"), h.get("name")) not in seen]
    return sorted(merged, key=lambda h: h.get("t1") or 0)


def hist_reset_path():
    """历史峰值水印文件路径（worker_home 下共享一份，独立主机各存一份，#300）。"""
    return os.path.join(HOME, "hist_reset.json")


def normalize_hist_resets(rules, now=None):
    """规整历史峰值水印规则：校验字段、name 经 base_name 归一化、按 (project, tag, name) 去重留最新 t、清理超期旧规则（HIST_RESET_KEEP_S）。"""
    now = time.time() if now is None else now
    cutoff = now - HIST_RESET_KEEP_S
    best = {}
    for r in rules or ():
        if not isinstance(r, dict):
            continue
        proj = r.get("project")
        if not isinstance(proj, str) or not proj.strip():
            continue
        proj = proj.strip()
        tag = r.get("tag")
        tag = tag.strip() if isinstance(tag, str) and tag.strip() else None
        name = r.get("name")
        name = base_name(name.strip()) if isinstance(name, str) and name.strip() else None
        if not tag and not name:
            continue
        try:
            t = float(r.get("t"))
        except (TypeError, ValueError):
            continue
        if t < cutoff:
            continue
        key = (proj, tag, name)
        if key not in best or t > best[key]:
            best[key] = t
    return [{"project": k[0], "tag": k[1], "name": k[2], "t": best[k]}
            for k in sorted(best, key=lambda x: (x[0], x[1] or "", x[2] or ""))]


def load_hist_resets(cache, host=None, standalone=False, now=None):
    """按 fsig 缓存加载历史峰值水印（hist_reset.json；独立主机一并合并 hist_shared.json 中的 resets，#300）。
    文件未变化返回 None；有变化（含首次加载或文件被删）返回规整后的规则列表。"""
    changed = False
    rf = hist_reset_path()
    files = [(rf, "rules")]
    if standalone and host:
        files.append((os.path.join(HOME, "hosts", host, "hist_shared.json"), "resets"))
    live = set()
    for path, field in files:
        try:
            m = fsig(path)
        except OSError:
            if path in cache:
                cache.pop(path, None)
                changed = True
            continue
        live.add(path)
        if cache.get(path, (None,))[0] != m:
            try:
                raw = json.loads(read(path, "") or "{}")
                rows = raw.get(field) if isinstance(raw, dict) else []
                rows = rows if isinstance(rows, list) else []
            except ValueError:
                rows = []
            cache[path] = (m, rows)
            changed = True
    for k in list(cache):
        if k not in live:
            cache.pop(k, None)
            changed = True
    if not changed:
        return None
    merged = []
    for _, rows in cache.values():
        merged.extend(rows)
    return normalize_hist_resets(merged, now=now)


# 任务日志里每次开跑的分隔行（launch 写「===== 时刻 labmon 开始（GPU …）=====」；11-07 前写的是「… 开始（可能是续跑；…）=====」，也认）。
# 探针 probe.py 不 import 本文件，另有一份同样的
START_RE = re.compile(r"===== .* 开始（")


OOM_SAY = {"vram": "CUDA 显存不够（OOM）", "ram": "容器内存撞上限被杀（OOM）"}   # 失败事件与队列日志句尾的说法（finish）


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


THREAD_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")


def lane_key(j):
    """运行中任务或请求属于哪条车道：清单任务是它的队列 key（并行清单几行同一个，#147），单元是它自己的 key。"""
    return j["q"]["key"] if j.get("q") else j["key"]


def hist_key(h, kind):
    """同类记录的键（estimate、duration 用）。没记 project 的旧行按目录名猜，只供估时；份额（share）不认它们。"""
    proj = h.get("project") or (project_of(h["root"]) if h.get("root") else None)
    if kind == "tag_name":
        return ("tag_name", proj, h.get("tag"), base_name(h.get("name") or ""))
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
