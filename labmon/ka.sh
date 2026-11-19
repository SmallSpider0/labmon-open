#!/usr/bin/env bash
# GPU 保活程序的停启入口（labmon 发到服务器上的那份：worker start 时与 labmon_worker.py 一起写到 worker_home，只取 main 上已提交的版本，#105）。
#
#   ka.sh running   有则逐行打印保活的进程组号，退出码 0；无则退出码 1
#   ka.sh stop      终止保活并确认已退出：0 停了（或本来就没有）；1 发完 TERM、KILL 还在；2 拿不到锁
#   ka.sh start     若未运行则启动并确认已起来：0 起来了（或本来就在跑）；1 20 秒内没见到进程；2 拿不到锁
#   不认识的动作退出 2。失败的原因写进事件日志，同时写到标准错误（worker 的 ka_stop 带回原因，#102）。
#
# 配置（AGRI_ROOT、AGRI_KA_DIR、AGRI_KA_SCRIPT、AGRI_KA_PY）读 KA_ENV 指的 ka.env，没给时读脚本旁边的 ka.env。
# 识别（ka_procs，认保活与发信号同一个函数）：python 解释器跑 $AGRI_KA_DIR/$AGRI_KA_SCRIPT——命令行里写着完整路径，
# 或第一个脚本参数按进程工作目录还原、解开符号链接后是它（labmon #29）。不看进程名（comm）：PyTorch 会把进程名改成 pt_main_thread。
# 不用 pkill -f：它会匹配到发起命令的 shell 自身。启动照旧是 cd 保活目录后 python 跑完整路径，命令行里带着脚本名
# （组里有人 pkill -f train_keep_alive.py 照样匹配得上，被这样停掉就是没了，下次按规则再开）。
# 锁与事件日志的位置按 AGRI_ROOT 定，与共享盘上原来那份 ka.sh 同一处：两份同时在用时彼此互斥、写同一份日志。
# 所有停启写入 $AGRI_ROOT/keepalive/events.log（共享盘，重启后仍在），格式：时间 事件 详情。
set -uo pipefail
_ENV=${KA_ENV:-"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ka.env"}; [[ -f "$_ENV" ]] && source "$_ENV"
: "${AGRI_ROOT:?}" "${AGRI_KA_DIR:?}" "${AGRI_KA_SCRIPT:?}" "${AGRI_KA_PY:?}"
FULL="$AGRI_KA_DIR/$AGRI_KA_SCRIPT"
PROC=${KA_PROC:-/proc}
EV="$AGRI_ROOT/keepalive/events.log"; mkdir -p "$(dirname "$EV")"
LOCK="/tmp/agri-ka-$(echo -n "$AGRI_ROOT" | cksum | cut -d" " -f1).lock"   # 按根目录区分，测试实例不与正式实例互锁
log() { echo "$(date -Iseconds) $1 ${2:-}" >> "$EV"; }
fail() { log "$1" "$2"; echo "ka.sh: $1 $2" >&2; exit "$3"; }      # 失败：记日志、原因写到标准错误、按契约退出

ka_procs() {   # 保活进程：每行“PID 进程组”。running 与 stop 都只认它列出的（#105）
  local real pid pgid exe rest a tok p skip
  local -a argv
  real=$(realpath -m -- "$FULL" 2>/dev/null) || real=$FULL
  ps -eo pid=,pgid=,args= | while read -r pid pgid exe rest; do
    [[ "${exe##*/}" =~ ^python[0-9.]*$ ]] || continue
    if [[ " $rest " == *"$FULL"* ]]; then echo "$pid $pgid"; continue; fi
    tok="" skip=""
    read -ra argv <<< "$rest"
    for a in "${argv[@]}"; do         # 第一个不以 - 开头的参数是脚本；-W、-X 跳过它的值；-m、-c 没有脚本
      if [ -n "$skip" ]; then skip=""; continue; fi
      case "$a" in -m|-c|-[!-]*c) break ;; -W|-X) skip=1 ;; -*) ;; *) tok=$a; break ;; esac
    done
    [ -n "$tok" ] || continue
    case "$tok" in
      /*) p=$tok ;;
      *) p="$(readlink "$PROC/$pid/cwd" 2>/dev/null)/$tok" || continue ;;
    esac
    [ "$(realpath -m -- "$p" 2>/dev/null)" = "$real" ] && echo "$pid $pgid"
  done
}

pgids() { ka_procs | awk '{print $2}' | sort -un; }

birth() {      # 进程出生时钟：/proc/<pid>/stat 第 22 项（开机以来的时钟数），同 fleet_guard.sh 的 proc_birth
  local s
  { read -r s < "$PROC/$1/stat"; } 2>/dev/null || return 1
  set -- ${s##*) }
  printf '%s\n' "${20:-?}"
}

signal_ka() {   # 组内全是保活进程才整组发信号；否则只发给认出来的进程，不波及同组的其他程序
  local sig=$1 procs g
  procs=$(ka_procs)
  for g in $(printf '%s\n' "$procs" | awk 'NF {print $2}' | sort -un); do
    if ps -eo pid=,pgid= | awk -v g="$g" -v mine="$(printf '%s\n' "$procs" | awk -v g="$g" '$2 == g {print $1}' | tr '\n' ' ')" '
        BEGIN {n = split(mine, a, " "); for (i = 1; i <= n; i++) k[a[i]] = 1}
        $2 == g && !($1 in k) {bad = 1} END {exit bad}'; then
      kill "-$sig" -- "-$g" 2>/dev/null
    else
      for p in $(printf '%s\n' "$procs" | awk -v g="$g" '$2 == g {print $1}'); do kill "-$sig" "$p" 2>/dev/null; done
    fi
  done
}

case "${1:-}" in
  running) g=$(pgids); [[ -n "$g" ]] && { echo "$g"; exit 0; } || exit 1 ;;

  stop)
    exec 9>"$LOCK"; flock -w "${KA_LOCK_S:-60}" 9 || fail STOP_FAILED "拿不到锁" 2
    g=$(pgids); [[ -z "$g" ]] && exit 0
    signal_ka TERM
    for i in $(seq 30); do [[ -z "$(pgids)" ]] && break; sleep 1; done
    if [[ -n "$(pgids)" ]]; then signal_ka KILL; sleep 2; fi
    left=$(pgids)
    [[ -n "$left" ]] && fail STOP_FAILED "进程组 $(echo $left) 仍在" 1
    log STOPPED "进程组 $(echo $g)；调用者 ${KA_CALLER:-?}"; exit 0 ;;

  start)
    exec 9>"$LOCK"; flock -w "${KA_LOCK_S:-60}" 9 || fail RESTORE_FAILED "拿不到锁" 2
    [[ -n "$(pgids)" ]] && exit 0
    ( exec 8>&- 9>&- </dev/null >/dev/null 2>&1
      cd "$AGRI_KA_DIR" || exit 1
      CUDA_VISIBLE_DEVICES=0 PYTHONDONTWRITEBYTECODE=1 exec setsid nohup "$AGRI_KA_PY" "$FULL" ) &
    # ↑ 子 shell 一开始就关闭锁描述符并以 exec 替换为保活进程：否则保活（或等待它的子 shell）会
    #   继承并永久持有 ka.sh 的锁（fd 9）与看门狗的单实例锁（fd 8），之后的 stop 拿不到锁
    g=""
    for i in $(seq "${KA_START_S:-20}"); do g=$(pgids); [[ -n "$g" ]] && break; sleep 1; done
    # 进程组后接组长的出生时钟（#102：归还时 fleet_guard.sh adopt 按它认这个保活是谁开的；worker 的 ka_caller 只取“进程组 N”与“调用者”）
    if [[ -n "$g" ]]; then log RESTORED "进程组 $(echo $g) 出生 $(birth "${g%%$'\n'*}" || echo ?)；调用者 ${KA_CALLER:-?}"; exit 0; fi
    fail RESTORE_FAILED "启动后 ${KA_START_S:-20} s 内未见进程；调用者 ${KA_CALLER:-?}" 1 ;;

  *) sed -n '2,7p' "$0" >&2; exit 2 ;;
esac
