# labmon 组内服务器的开保活与让路：经 ssh 标准输入送到服务器上跑（bash -s -- 动作 …），只碰保活，别的进程一概不动。
#
#   start <ka 命令> <盯几秒> <谁开的>
#       确认保活进程组已占 GPU 才报 ok。已有保活进程不重开；GPU 上还有别的进程：不开（busy）。
#       否则 <ka 命令> start，记进 /tmp/labmon-guard.state，再在后台盯这么久：
#       每 20 秒看一次 GPU，出现不属于这个保活进程组的进程（实验或这人自己开的保活）就 <ka 命令> stop 让出，保活被别人停了也退出。
#       按进程组认自己：保活本身在 GPU 上占几个进程（多卡）都不算别人。
#   repair <ka 命令> <盯几秒> <谁开的> <容器出生时钟> <登记时刻> <进程组> <进程出生时钟> <卡数>
#       在同一次锁定内核对状态、全部 GPU 均未活动且没有别的 GPU 进程，再重开本工具登记的保活。
#   stop <ka 命令> <为什么>
#       只停 labmon 开的那一个保活（进程组对得上才停）；别人开的、已经不在的不动。
#
# ka 命令是主机清单 [fleet] 的 keepalive_cmd（ka.sh：容器内加锁，按脚本的完整路径认保活，不会起两个）。
# 最后一行是结果：ok … / busy … / fail …；探针（fleet_probe.sh）读 /tmp/labmon-guard.state 看保活是不是 labmon 开的。
ST=${GUARD_STATE:-/tmp/labmon-guard.state}
PROC=${GUARD_PROC:-/proc}

gpu_procs() {   # 输出本组与其他 GPU 进程数；对不回容器 PID 的进程也算其他
  local g=$1 raw pid d l h s n=0 own=0
  local mapped=()
  raw=$(timeout -k 1 5 nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null) || return 1
  # 查询成功但返回未知格式时也不当作空闲。
  raw=$(printf '%s\n' "$raw" | awk 'NF && $0 !~ /^[[:space:]]*[1-9][0-9]*[[:space:]]*$/ {
      if ($0 !~ /^[[:space:]]*No running processes found[[:space:]]*$/) bad=1; next
    } NF {print $1} END {exit bad}') || return 1
  raw=$(printf '%s\n' "$raw" | sort -u)
  if [ -n "$g" ] && [ -n "$raw" ]; then
    for d in "$PROC"/[0-9]*; do
      { read -r l < "$d/sched"; } 2>/dev/null || continue
      h=${l##*(}; h=${h%%,*}
      [[ "$h" =~ ^[1-9][0-9]*$ ]] && mapped[$h]=$d
    done
  fi
  for pid in $raw; do
    # 优先用 sched 的宿主 PID 映射；找不到才按容器 PID 查，同 fleet.analyze。
    d=${mapped[$pid]:-"$PROC/$pid"}
    s=$(cat "$d/stat" 2>/dev/null); s=${s##*) }; set -- $s    # 去掉进程名后：状态、父进程、进程组
    if [ -n "$g" ] && [ "${3:-}" = "$g" ]; then own=$((own + 1)); else n=$((n + 1)); fi
  done
  printf '%s %s\n' "$own" "$n"
}

guard_owner() {  # $1 ka 命令 $2 进程组 $3 登记时刻（可选）
  local state k t g rest
  state=$(cat "$ST" 2>/dev/null) || return 1
  [[ "$state" != *$'\n'* ]] || return 1
  read -r k t g rest <<< "$state"
  [ "$k" = start ] && [ "$g" = "$2" ] && { [ -z "${3:-}" ] || [ "$t" = "$3" ]; } &&
    kill -0 "$g" 2>/dev/null && [ "$(running_groups "$1")" = "$g" ]
}

watch_yield() {  # $1 进程组 $2 ka 命令 $3 登记时刻
  local gpu
  flock -w 15 9 || return
  guard_owner "$2" "$1" "$3" || return
  gpu=$(gpu_procs "$1") || return
  if [ "${gpu#* }" -gt 0 ]; then
    bash -c "$2 stop" 9>&- >/dev/null 2>&1 && echo "yield $(date +%s)" >> "$ST"
  fi
}

watch() {   # $1 盯到几点（epoch）$2 保活进程组 $3 ka 命令 $4 登记时刻
  local gpu
  while [ "$(date +%s)" -lt "$1" ]; do
    sleep "${GUARD_POLL:-20}"
    kill -0 "$2" 2>/dev/null || exit 0
    gpu=$(gpu_procs "$2") || continue    # 查 GPU 失败时没有证据说有别的进程
    if [ "${gpu#* }" -gt 0 ]; then
      watch_yield "$2" "$3" "$4" 9>>"$ST.lock"
      exit 0
    fi
  done
}

running_groups() {   # ka.sh 每行一个进程组号，空输出或命令失败都不是已运行的证据
  local ids
  ids=$(bash -c "$1 running" 9>&- 2>/dev/null) || return 1
  printf '%s\n' "$ids" | grep -E '^[1-9][0-9]*$'
}

start() {
  local groups gpu g now until
  if groups=$(running_groups "$1"); then
    for g in $groups; do
      gpu=$(gpu_procs "$g") || { echo "fail 查不到 GPU 进程"; return; }
      if [ "${gpu% *}" -gt 0 ]; then echo "ok already $g"; return; fi
    done
    echo "fail 保活进程已存在，尚未看到它占用 GPU"
    return
  fi
  gpu=$(gpu_procs "") || { echo "fail 查不到 GPU 进程"; return; }
  if [ "${gpu#* }" -gt 0 ]; then echo "busy ${gpu#* }"; return; fi
  bash -c "$1 start" 9>&- >/dev/null 2>&1 || { echo "fail 保活没起来"; return; }
  groups=$(running_groups "$1") || { echo "fail 保活没起来"; return; }
  if [[ "$groups" == *$'\n'* ]]; then echo "fail 开保活后找到多个进程组，无法确认归属"; return; fi
  g=$groups
  now=$(date +%s)
  echo "start $now $g $(( now + $2 )) $3" > "$ST"
  if [ "$2" -gt 0 ]; then       # 后台盯着：不继承本会话的输入输出，ssh 才能马上返回
    ST="$ST" PROC="$PROC" setsid bash -c "$(declare -f gpu_procs running_groups guard_owner watch_yield watch); watch \"\$@\"" _ \
      "$(( now + $2 ))" "$g" "$1" "$now" </dev/null >/dev/null 2>&1 9>&- &
  fi
  until=$(( now + ${GUARD_READY_S:-10} ))
  while :; do
    gpu=$(gpu_procs "$g") || { echo "fail 查不到 GPU 进程"; return; }
    if [ "${gpu% *}" -gt 0 ]; then echo "ok start $g"; return; fi
    [ "$(date +%s)" -lt "$until" ] || break
    sleep 1
  done
  echo "fail 保活进程已启动，尚未看到它占用 GPU"
}

stop() {
  local k g
  read -r k _ g _ < "$ST" 2>/dev/null
  { [ "$k" = start ] && kill -0 "$g" 2>/dev/null; } || { echo "ok gone"; return; }
  guard_owner "$1" "$g" || { echo "ok notours"; return; }
  if bash -c "$1 stop" 9>&- >/dev/null 2>&1; then echo "stop $(date +%s) $2" >> "$ST"; echo "ok stopped"; else echo "fail 保活停不下来"; fi
}

proc_birth() {
  local row pid=$1
  { read -r row < "$PROC/$pid/stat"; } 2>/dev/null || return 1
  row=${row##*) }; set -- $row
  [ "$pid" = 1 ] || [ "${3:-}" = "$pid" ] || return 1
  [[ "${20:-}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${20}"
}

gpu_zero() {
  local raw rc=0
  raw=$(timeout -k 1 3 nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader,nounits -lms 500 2>/dev/null) || rc=$?
  [ "$rc" = 0 ] || [ "$rc" = 124 ] || return 1
  printf '%s\n' "$raw" | awk -F, -v expected="$1" '
    NF {
      if (NF != 2 || $1 !~ /^[[:space:]]*[0-9]+[[:space:]]*$/ || $2 !~ /^[[:space:]]*0[[:space:]]*$/) bad=1
      else count[$1 + 0]++
    }
    END {
      for (i in count) { n++; if (count[i] < 2) bad=1 }
      exit (bad || n != expected)
    }'
}

repair() {  # ka 命令、盯几秒、谁开的、容器出生时钟、登记时刻、进程组、进程出生时钟、卡数
  local gpu
  [[ "$4" =~ ^[0-9]+$ && "$5" =~ ^[0-9]+$ && "$6" =~ ^[1-9][0-9]*$ && "$7" =~ ^[0-9]+$ && "$8" =~ ^[1-9][0-9]*$ ]] ||
    { echo "fail 保活身份无效"; return; }
  gpu_zero "$8" || { echo "busy 无法确认全部 GPU 仍无活动"; return; }
  gpu=$(gpu_procs "$6") || { echo "busy 查不到 GPU 进程"; return; }
  [ "${gpu#* }" = 0 ] || { echo "busy 复查时 GPU 上有了别的进程"; return; }
  if [ "$(proc_birth 1)" != "$4" ] || [ "$(proc_birth "$6")" != "$7" ] || ! guard_owner "$1" "$6" "$5"; then
    echo "busy 保活归属或容器已变"
    return
  fi
  bash -c "$1 stop" 9>&- >/dev/null 2>&1 || { echo "fail 空转的保活停不下来"; return; }
  echo "stop $(date +%s) 保活空转" >> "$ST"
  start "$1" "$2" "$3"
}

control() {
  if ! flock -w 15 9; then
    if [ "$1" = repair ]; then echo "busy 保活正在执行另一项操作"; else echo "fail 保活正在执行另一项操作"; fi
    return
  fi
  case "$1" in
    start) start "$2" "${3:-0}" "${4:-看板}" ;;
    stop) stop "$2" "${3:-}" ;;
    repair) repair "${@:2}" ;;
    *) echo "fail 不认识的动作 $1" ;;
  esac
}
control "$@" 9>>"$ST.lock"
