"""labmon worker 包：服务器端统一的队列执行器，只用 Python 3.8 标准库，不 import labmon 其余模块。

对外统一导出全部公共函数、常量与类；对本包模块对象写入属性（如测试替换 time、PROC、host_info 等）时
由 _Facade 同步转发到归属子模块，确保各子模块看到的始终是同一份值。
"""
import sys
import types

from . import pure
from . import common
from . import queue
from . import disk
from . import measure
from . import keepalive
from . import deps
from . import sched
from . import forecast
from . import offers
from . import core
from . import fit
from . import cli

_EXPORTS = (
    (pure, ['GIB', 'GROUP_RE', 'HOLD_RE', 'IDLE_REG_S', 'INTERP', 'OVER_FRAC', 'OVER_MIN_S', 'POOL', 'POOL_ID_RE', 'POOL_PREFIX', 'STALE_S', 'WSL_MNT_RE', 'file_sig', 'is_hold', 'is_pool_id', 'is_wsl_mnt', 'job_key', 'list_path', 'mem_unevictable', 'other_load', 'other_load_text', 'overrun_left', 'parse_jsonl', 'person_use', 'pool_host_id', 'pool_id_of', 'proc_tree', 'same_list', 'script_arg', 'wsl_mnt_warn']),
    (common, ['BASE_DIR', 'BUSY_WIN_S', 'CG', 'CONC_MAX', 'CPU_ALERT_CODES', 'CPU_ALERT_S', 'CPU_BOUND_FRAC', 'CPU_CODES', 'CPU_DEFAULT', 'CPU_ONLY_NICE', 'CPU_OVER_MIN', 'CPU_OVER_RATIO', 'CPU_SHRINK_N', 'CPU_SPARE_FRAC', 'CPU_SPARE_MIN', 'CPU_WARM_S', 'CUDA_OOM', 'CUT_EST_MARGIN', 'DATA_ROOT', 'DEP_MISS_S', 'DEP_STALE_S', 'DISK_HYST_GB', 'DISK_STOP_GB', 'DISK_WARN_GB', 'DUR_MARGIN', 'FAILCNT_WIN_S', 'FORECAST_EVERY', 'FORECAST_S', 'GANG_FRESH_S', 'GANG_JOIN_S', 'GANG_KEEP_S', 'GANG_LATE_S', 'GANG_SOON_S', 'GATE', 'GPU_CPU_RESERVE_DIV', 'GPU_CPU_RESERVE_PER_CARD', 'GUEST_ADJ', 'GUEST_QUIET_S', 'GUEST_RAM_HOT', 'GUEST_RAM_HYST_FRAC', 'GUEST_RAM_HYST_MIN', 'HALF_LIFE_S', 'HIST_N', 'HOME', 'IDLE_CORES', 'IO_BASE_N', 'IO_MIN_OPS', 'IO_SLOW_MS', 'IO_WHAT', 'IO_YIELD_MAX_S', 'KA_BIN', 'KA_CHECK_S', 'KA_ENV', 'KA_SH', 'LOCAL', 'MACOS_RESERVE_CPUS', 'MIB', 'MISS_NOTE', 'MOVE_LEAD_S', 'MOVE_MAX', 'MPS_MEM_FRAC', 'MPS_MEM_RESERVE_GIB', 'NAME_RE', 'NEAR_TOL', 'ONCE_HOLD_S', 'OOM_SAY', 'OUTLIER_DROP_FRAC', 'OUTLIER_DROP_N', 'PAR_MAX', 'PAR_RE', 'PAR_REFILL_S', 'PAR_STEP_S', 'PEAK_WIN', 'PEER_EVERY', 'POLICY', 'PRESS_EVERY', 'PRESS_KEEP_S', 'PRESS_WIN', 'PROC', 'Paths', 'RATIO_N', 'RATIO_OK', 'ROOM_MAX', 'ROOTFS', 'SLOTS', 'SLOW_RATIO', 'SQUEEZE_MAX', 'START_RE', 'STATUS_STALE_S', 'THREAD_VARS', 'THR_COOL', 'THR_HOLD_S', 'THR_HOT', 'TICK', 'TIGHT_FREE_MB', 'TIGHT_MEM', 'UNACCT_BUSY_MB', 'UNACCT_CLEAR_MB', 'UNACCT_UTIL_COOL', 'UNACCT_UTIL_HOT', 'UNACCT_VRAM_MB', 'UNDER_WIN_S', 'UTIL_FULL', 'UTIL_LOW', 'UTIL_WIN_S', 'VOID_CPU', 'VOID_KEEP_S', 'WAIT_CAP_S', 'alive', 'append_jsonl', 'base_name', 'cmdline', 'deadline_name', 'flock', 'fsig', 'gp_label', 'hist_key', 'idle_job', 'intruders', 'lane_key', 'load_hist', 'load_jsonl', 'log_tail', 'now_str', 'ovr_fields', 'ovr_text', 'parse_boottime', 'parse_lstart', 'posix_fallback_ok', 'proc_env', 'project_dir', 'project_of', 'pstart', 'queue_root_ok', 'queue_stop_path', 'read', 'read_pid', 'sched_ovr', 'started_at', 'write', 'write_queue_stop']),
    (queue, ['GUARD_RE', 'LOCAL_EVERY', 'body', 'cache_dir', 'check_needs', 'drop_current', 'env_of', 'env_path', 'expand', 'first_rows', 'hints', 'job_cmd', 'job_of', 'list_jobs', 'list_par', 'load_queue', 'local_deps', 'masters', 'matching_regs', 'new_req', 'next_jobs', 'parse_jobs', 'pool_refuse', 'qslug', 'queue_req', 'queue_state_conflict', 'read_current', 'reg_added', 'reg_dirs', 'reg_files', 'reg_lock', 'registry', 'regs', 'rows_left', 'run_elsewhere', 'run_key', 'scratch_dir', 'stage']),
    (disk, ['Disk', 'LOCAL_DRIFT', 'LOCAL_IOPS', 'LOCAL_SKIP', 'LOCAL_TOP', 'LocalDisk', 'Pace', 'SCAN_AGES', 'SCAN_COST', 'SCAN_DRIFT', 'SCAN_HOT_S', 'SCAN_IOPS', 'SCAN_LEASE_S', 'SCAN_LIST_S', 'SCAN_MAX_S', 'SCAN_SAVE_S', 'SCAN_SPLIT_S', 'WorkDisk', 'dot', 'scan_unit', 'scan_units']),
    (measure, ['accel_env', 'accel_kind', 'card_conc', 'cg', 'cpu_cap', 'cpu_env', 'cpu_over', 'cpu_quota', 'cpu_since', 'darwin_cpu_quota', 'darwin_mem_info', 'disk_counts', 'gpu_apps', 'gpu_procs', 'gpu_table', 'host_cpu_ticks', 'host_info', 'ints', 'io_base', 'is_apple_silicon', 'is_darwin', 'is_wsl', 'job_cpus', 'job_procs', 'job_sus', 'kv', 'local_dev', 'mem_of', 'mps_gpu', 'nfs_counts', 'nfs_slow', 'num', 'oom_kills', 'oom_of', 'parse_ps_procs', 'parse_ps_time', 'parse_vm_stat', 'press_rates', 'press_raw', 'proc_io', 'psi', 'query_gpus', 'rootfs_info', 'scan_procs', 'smi', 'sysctl_val', 'thr_step', 'unacct_lines', 'unacct_vram', 'win_mean', 'MeasureMixin']),
    (keepalive, ['CRON', 'FIGHT_S', 'GPU_RE', 'KA_LOG_LAG', 'KA_REF', 'KA_STUCK_S', 'LOCK_WAIT_S', 'LOOP_RE', 'SURE_WAIT', 'WATCH_EVERY', 'WATCH_SKIP', 'freeze', 'gpu_owners', 'guest_ram_hi', 'head', 'held_locks', 'ka_lines', 'ka_log_time', 'ka_owned', 'load_frozen', 'lock_base', 'lock_text', 'ours', 'proc_args', 'refs', 'script_of', 'thaw', 'watchdogs', 'wd_text', 'KeepaliveMixin']),
    (deps, ['dep_end', 'dep_keys', 'dep_names', 'gang_bad', 'gang_cur', 'gang_file', 'gang_last', 'gang_peer', 'gang_until', 'DepsMixin']),
    (sched, ['WHY', '_place', 'acct_job', 'admit', 'borrowable', 'card_free', 'card_need', 'card_share', 'conc_key', 'cpu_budget', 'cpu_cls', 'cpu_fit', 'cpu_free', 'cpu_free_for', 'cpu_from_peak', 'cpu_give', 'cpu_held', 'cpu_higher_peak', 'cpu_hist_rows', 'cpu_limit', 'cpu_line', 'cpu_need', 'cpu_only_nice', 'cpu_room_for', 'cpu_shrink', 'cpu_threads', 'cpu_warm', 'cpus_export', 'cut_min', 'duration', 'estimate', 'fair_key', 'fits_before', 'gpu_cpu_flex', 'gpu_cpu_reserve', 'gpu_dyn_reserve', 'gpu_slack', 'gpus_of', 'hold_until', 'job_ngpu', 'job_thread_cap', 'near', 'need_mb', 'nofit', 'peak_rows', 'place', 'ram_free', 'ram_margin', 'ram_need', 'ran_ok', 'ratio_dur', 'rec_conc', 'run_tot', 'same_dur', 'shrunk_text', 'snap_ngpus', 'tag_full', 'thread_cap', 'trusted', 'usage', 'why_of', 'why_text', 'with_cut', 'SchedMixin']),
    (forecast, ['sim_snapshot', 'simulate', 'ForecastMixin']),
    (offers, ['OffersMixin']),
    (core, ['Worker']),
    (fit, ['FitHost', 'fit', 'fit_deps', 'fit_eta', 'fit_head', 'fit_list', 'fit_snapshot', 'fit_status', 'known_lists', 'list_row']),
    (cli, ['clean_local', 'main', 'slot_main']),
)

_OWNER = {"time": common}
__all__ = []
time = common.time

for _mod, _names in _EXPORTS:
    for _name in _names:
        _OWNER[_name] = _mod
        globals()[_name] = getattr(_mod, _name)
        __all__.append(_name)


class _Facade(types.ModuleType):
    """将包级属性覆盖/恢复同步到归属子模块（支持测试打桩与 Clock 注入）。"""

    def __setattr__(self, name, value):
        owner = self.__dict__.get("_OWNER", {}).get(name)
        if owner is not None:
            setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        owner = self.__dict__.get("_OWNER", {}).get(name)
        if owner is not None and hasattr(owner, name):
            delattr(owner, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _Facade
