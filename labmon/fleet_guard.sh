# labmon 组内服务器的开保活与让路：经 ssh 标准输入送到服务器上跑（bash -s -- 动作 …），只碰保活，别的进程一概不动。
#
#   start <ka 命令> <盯几秒> <谁开的（一个词）>
#       确认保活进程组已占 GPU 才报 ok。已有保活进程不重开；GPU 上还有别的进程：不开（busy）。
#       否则 <ka 命令> start，登记进 /tmp/labmon-guard.state（一行：start 登记时刻 进程组 盯到几点 谁开的 进程出生时钟），再在后台盯这么久：
#       每 20 秒看一次 GPU，出现不属于这个保活进程组的进程（实验或这人自己开的保活）就 <ka 命令> stop 让出，保活被别人停了也退出。
#       按进程组认自己：保活本身在 GPU 上占几个进程（多卡）都不算别人。
#   repair <ka 命令> <盯几秒> <谁开的> <容器出生时钟> <登记时刻或 manual> <进程组> <进程出生时钟> <卡数>
#       在同一次锁定内核对状态、全部 GPU 均未活动且没有别的 GPU 进程，再重开本工具登记的保活，或停掉配置脚本能确认的用户保活并开启看板保活。
#       停下了、新的却没开成回 gap（GPU 上此刻没有保活，看板下次探测直接再开）；没停成仍是 fail。
#   stop <ka 命令> <为什么>
#       只停 labmon 开的那一个保活（进程组对得上才停）；别人开的、已经不在的不动。
#   adopt <ka 命令> <盯几秒> <谁开的> <ka.env> <调用者> <借来时刻> <归还时刻> [<进程组> <出生时钟>]
#       归还借来的服务器时（borrow.finish）与还掉以后补登记（保活守护的 adopt，#102）：此刻卡上的保活是借用期间 labmon 的 worker 开的，
#       就不动进程、登记成看板的保活（同 start 的登记与盯着让路）。在锁里核对：ka.sh running 只列出一个进程组、组长出生时钟读得到、
#       组长启动时刻在借用期间（前后各留 60 秒）、GPU 上除它以外没有进程，且认得出是 worker 开的——给了进程组与出生时钟（worker 状态的
#       keepalive.mine）就要两样都相等；没给就看 ka.env 的 AGRI_ROOT/keepalive/events.log：这个进程组的 RESTORED、调用者是 <调用者>、
#       时刻在组长启动后 −5～30 秒内（同 worker 的 KA_LOG_LAG），行里带出生时钟的还要相等。
#       ok adopt 进程组：登记了；ok registered 进程组：本来就是看板登记的；no …：没有保活、有几个、不是借用期间起来的（对方的）；
#       unsure …：借用期间起来的，却认不出是 worker 开的（不登记）；busy …：GPU 上有别的进程或查不到；fail …：ka.sh 出错。
#   kacheck <ka 命令> <GPU 进程…>
#       只读（不拿锁、不写文件，借用前 borrow.begin 用）：逐个看这些认作保活的 GPU 进程（容器 PID）所在的进程组在不在 ka.sh running 列出的里面。
#       ok：都在（已经没了的不算）；noka PID…：不在的（ka.sh 停不了它们）；fail …：ka.sh 本身出错（退出码；原因在标准错误）。
#
# ka 命令是主机清单 [fleet] 的 keepalive_cmd（ka.sh：容器内加锁，不会起两个；python 跑它的保活脚本才算，命令行写完整路径，
# 或相对路径按进程工作目录还原后是它，#29）。
# 最后一行是结果：ok … / busy … / fail … / unmanaged … / gap … / no … / unsure …；探针（fleet_probe.sh）读 /tmp/labmon-guard.state 看保活是不是 labmon 开的。
# 进程身份只比出生时钟（/proc/<pid>/stat 第 22 项，开机以来的时钟数，proc_birth），不换算成墙上时钟。
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
    [ "$(date +%s)" -lt "$1" ] || exit 0
    kill -0 "$2" 2>/dev/null || exit 0
    gpu=$(gpu_procs "$2") || continue    # 查 GPU 失败时没有证据说有别的进程
    if [ "${gpu#* }" -gt 0 ]; then
      watch_yield "$2" "$3" "$4" 9>>"$ST.lock"
      exit 0
    fi
  done
}

running_groups() {   # ka.sh running 的读法（与 worker 的 Worker.ka_groups 同一口径：退出码 0 是保活在跑，非 0 是没在跑），打出列出的进程组。
  # 返回 0 有；1 没有在跑的（ka.sh running 退出 1）；201 在跑却没列出进程组（认不了归属，不当作没在跑）；
  # 200 另有认不出的行（strict）；其余是 ka.sh 自己的退出码（同样不是在跑的证据，kacheck 报成出错）。
  # ka.sh 的标准错误照传，认不出的那一行也写到标准错误（kacheck 带回原因，同一次调用，#29）
  local ids rc
  ids=$(bash -c "$1 running" 9>&-); rc=$?
  [ "$rc" = 0 ] || { [ "$rc" = 1 ] && return 1; return "$rc"; }
  if [ "${2:-}" = strict ]; then
    printf '%s\n' "$ids" | LC_ALL=C awk '
      BEGIN { for (i = 128; i < 256; i++) ord[sprintf("%c", i)] = i }
      function trunc(s, m,    p, c, b, n) {
        if (length(s) <= m) return s
        s = substr(s, 1, m); p = m; b = 0
        while (p > 0) { c = substr(s, p, 1); b = (c in ord) ? ord[c] : 0; if (b < 128 || b >= 192) break; p-- }
        if (p > 0) { n = (b >= 240 && b <= 244 ? 4 : b >= 224 && b < 240 ? 3 : b >= 194 && b < 224 ? 2 : 0); if (n > 0 && m - p + 1 < n) s = substr(s, 1, p - 1) }
        return s
      }
      NF && $0 !~ /^[1-9][0-9]*$/ {if (!bad) print "认不出的一行：" trunc($0, 200) > "/dev/stderr"; bad=1}
      /^[1-9][0-9]*$/ {print; seen=1} END {exit (bad ? 2 : seen ? 0 : 3)}' || return $(( $? == 2 ? 200 : 201 ))
  else
    printf '%s\n' "$ids" | grep -E '^[1-9][0-9]*$' || return 201
  fi
}

register() {   # ka 命令、盯几秒、谁开的、进程组、出生时钟：写登记（看板开的保活），再在后台盯着让路（start 与 adopt 共用）
  local now
  now=$(date +%s)
  echo "start $now $4 $(( now + $2 )) ${3//[[:space:]]/_} $5" > "$ST"
  if [ "$2" -gt 0 ]; then       # 后台盯着：不继承本会话的输入输出，ssh 才能马上返回
    ST="$ST" PROC="$PROC" setsid bash -c "$(declare -f gpu_procs running_groups guard_owner watch_yield watch); watch \"\$@\"" _ \
      "$(( now + $2 ))" "$4" "$1" "$now" </dev/null >/dev/null 2>&1 9>&- &
  fi
}

start() {
  local groups gpu g now until born rc
  groups=$(running_groups "$1"); rc=$?
  if [ "$rc" = 201 ]; then echo "fail 保活在跑，但 ka.sh 没列出进程组，无法确认归属"; return; fi
  if [ "$rc" = 0 ]; then
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
  groups=$(running_groups "$1"); rc=$?
  if [ "$rc" = 201 ]; then echo "fail 开了保活，但 ka.sh 没列出进程组，无法确认归属"; return; fi
  [ "$rc" = 0 ] || { echo "fail 保活没起来"; return; }
  if [[ "$groups" == *$'\n'* ]]; then echo "fail 开保活后找到多个进程组，无法确认归属"; return; fi
  g=$groups
  born=$(proc_birth "$g") || born=-          # 读不到出生时钟的登记认不出身份：照样让路、停，只是不自动重开
  register "$1" "$2" "$3" "$g" "$born"
  now=$(date +%s)
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

repair() {  # ka 命令、盯几秒、谁开的、容器出生时钟、登记时刻或 manual、进程组、进程出生时钟、卡数
  local gpu groups result k t g rest
  [[ "$4" =~ ^[0-9]+$ && "$6" =~ ^[1-9][0-9]*$ && "$7" =~ ^[0-9]+$ && "$8" =~ ^[1-9][0-9]*$ ]] &&
    [[ "$5" = manual || "$5" =~ ^[0-9]+$ ]] || { echo "fail 保活身份无效"; return; }
  gpu_zero "$8" || { echo "busy 无法确认全部 GPU 仍无活动"; return; }
  gpu=$(gpu_procs "$6") || { echo "busy 查不到 GPU 进程"; return; }
  [ "${gpu#* }" = 0 ] || { echo "busy 复查时 GPU 上有了别的进程"; return; }
  if [ "$5" = manual ]; then
    [ "${gpu% *}" -gt 0 ] || { echo "busy 自行开启的保活已不占用 GPU"; return; }
    if ! groups=$(running_groups "$1" strict) || [ "$groups" != "$6" ]; then
      echo "unmanaged 配置的保活脚本无法确认归属"
      return
    fi
    if { read -r k t g rest < "$ST"; } 2>/dev/null && [ "$k" = start ] && [ "$g" = "$6" ]; then
      # 两次探测之间可能有人通过看板启动了同一组，不抢在新登记之后停。
      if [[ "$t" =~ ^[0-9]+$ ]] && ! grep -Eq '^(stop|yield) ' "$ST"; then
        echo "busy 看板已登记这一保活"
        return
      fi
    fi
  elif ! guard_owner "$1" "$6" "$5"; then
    echo "busy 保活归属或容器已变"
    return
  fi
  if [ "$(proc_birth 1)" != "$4" ] || [ "$(proc_birth "$6")" != "$7" ]; then
    echo "busy 保活归属或容器已变"
    return
  fi
  bash -c "$1 stop" 9>&- >/dev/null 2>&1 || { echo "fail 空转的保活停不下来"; return; }
  [ "$5" = manual ] || echo "stop $(date +%s) 保活空转" >> "$ST"
  result=$(start "$1" "$2" "$3")
  case "$result" in
    "ok already "*) if [ "$5" = manual ]; then echo "fail 原保活又被拉起，未开启看板保活"; else printf '%s\n' "$result"; fi ;;
    "ok "*|"busy "*) printf '%s\n' "$result" ;;
    *) echo "gap ${result#fail }" ;;      # 旧的已停、新的没开成：GPU 上此刻没有保活
  esac
}

ka_logged() {  # events.log 进程组 出生时钟 启动时刻 调用者：这个进程组的 RESTORED 是不是这个调用者记的（时刻在启动后 −5～30 秒内；带出生时钟的要相等）
  local ts rest t b
  [ -r "$1" ] || return 1
  while IFS= read -r ts && IFS= read -r rest; do
    [[ "$rest" == *"调用者 $5" ]] || continue
    b=$(printf '%s\n' "$rest" | sed -n 's/^RESTORED 进程组 [0-9]* 出生 \([0-9]*\)；.*/\1/p')
    [ -z "$b" ] || [ "$b" = "$3" ] || continue
    t=$(date -d "$ts" +%s 2>/dev/null) || continue
    [ "$t" -ge $(( $4 - 5 )) ] && [ "$t" -le $(( $4 + 30 )) ] && return 0
  done < <(tail -n 2000 "$1" | awk -v g="$2" '$2 == "RESTORED" && $3 == "进程组" && ($4 == g "；调用者" || $4 == g) {print $1; s = $0; sub(/^[^ ]+ /, "", s); print s}')
  return 1
}

adopt() {  # ka 命令、盯几秒、谁开的、ka.env、调用者、借来时刻、归还时刻、[进程组、出生时钟]
  local groups rc g born t0 bt tck gpu ev
  [[ "$6" =~ ^[0-9]+$ && "$7" =~ ^[0-9]+$ ]] || { echo "fail 借用时段无效"; return; }
  groups=$(running_groups "$1" strict); rc=$?
  case "$rc" in
    0) ;;
    1) echo "no 没有保活在跑"; return ;;
    *) echo "fail ka.sh running 出错或输出认不出（退出码 $rc）"; return ;;
  esac
  [[ "$groups" != *$'\n'* ]] || { echo "no 有几个保活进程组，认不准"; return; }
  g=$groups
  born=$(proc_birth "$g") || { echo "no 读不到保活的出生时钟"; return; }
  if guard_owner "$1" "$g"; then echo "ok registered $g"; return; fi
  bt=$(awk '$1 == "btime" {print $2}' "$PROC/stat" 2>/dev/null); tck=$(getconf CLK_TCK 2>/dev/null)
  [[ "$bt" =~ ^[0-9]+$ && "$tck" =~ ^[1-9][0-9]*$ ]] || { echo "no 读不到开机时刻"; return; }
  t0=$(( bt + born / tck ))
  if [ "$t0" -lt $(( $6 - 60 )) ] || [ "$t0" -gt $(( $7 + 60 )) ]; then echo "no 保活不是借用期间起来的"; return; fi
  if [ -n "${8:-}" ]; then
    [ "$8" = "$g" ] && [ "${9:-}" = "$born" ] || { echo "unsure 卡上的保活不是 worker 记下的那个"; return; }
  else
    ev=$( . "$4" >/dev/null 2>&1; printf '%s' "${AGRI_ROOT:-}")
    [ -n "$ev" ] || { echo "unsure 读不到 $4 的 AGRI_ROOT，查不了保活是谁开的"; return; }
    ka_logged "$ev/keepalive/events.log" "$g" "$born" "$t0" "$5" || { echo "unsure ka.sh 的事件日志里没有 $5 拉起它的记录"; return; }
  fi
  gpu=$(gpu_procs "$g") || { echo "busy 查不到 GPU 进程"; return; }
  [ "${gpu#* }" = 0 ] || { echo "busy GPU 上还有别的进程"; return; }
  [ "${gpu% *}" -gt 0 ] || { echo "busy 保活还没占上 GPU"; return; }
  register "$1" "$2" "$3" "$g" "$born"
  echo "ok adopt $g"
}

kacheck() {  # ka 命令、认作保活的 GPU 进程（容器 PID）…
  local groups rc pid row g bad=""
  groups=$(running_groups "$1" strict); rc=$?
  case "$rc" in               # 没有在跑的保活（返回 1）不是出错；出错的原因 ka.sh 已写到标准错误，借用的提示带上它
    0|1) ;;
    200) echo "fail ka.sh running 的输出认不出"; return ;;
    201) echo "fail 保活在跑，但 ka.sh 没列出进程组，无法确认归属"; return ;;
    *) echo "fail ka.sh running 出错（退出码 $rc）"; return ;;
  esac
  for pid in "${@:2}"; do
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    { read -r row < "$PROC/$pid/stat"; } 2>/dev/null || continue     # 已经没了
    row=${row##*) }; read -ra row <<< "$row"
    g=${row[2]:-}
    { [ -n "$g" ] && printf '%s\n' "$groups" | grep -qx -- "$g"; } || bad="$bad $pid"
  done
  if [ -n "$bad" ]; then echo "noka$bad"; else echo ok; fi
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
    adopt) adopt "${@:2}" ;;
    *) echo "fail 不认识的动作 $1" ;;
  esac
}
if [ "${1:-}" = kacheck ]; then kacheck "${@:2}"; else control "$@" 9>>"$ST.lock"; fi
