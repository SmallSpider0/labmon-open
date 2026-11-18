# labmon 组内服务器探针：经 ssh 标准输入送到别人的容器里跑（bash -s），只读：
# 不写文件、不碰进程，只读 /proc、cgroup，跑 nvidia-smi 与 ps。各镜像未必有 python，所以用 bash 写，解析在本机（fleet.py）。
# 输出按段：每段以 "@@ 段名" 开头。参数是认作保活的字样（[fleet] keepalive，fleet.fetch 经 bash -s -- 传来）：只读这些保活进程树的 smaps。
# 环境变量 LABMON_SCAN=<开机时刻>（fleet.scan_due 定，维护者 11-10 允许扫别人的，#149）时最后多一段 scan：根盘开机以来新写的文件按前三级目录汇总，
# 只读元数据（find 不读文件内容）、最低 CPU 与 IO 优先级、限时 LABMON_SCAN_S 秒，结果经标准输出带回本机，不在对方盘上写任何东西。
CG=${LABMON_CG:-/sys/fs/cgroup}   # 测试指到假 cgroup；服务器上没设，就是 /sys/fs/cgroup
cgv() { for f in "$@"; do [ -r "$CG/$f" ] && { cat "$CG/$f"; return; }; done; }
# memory.stat 的一项：v1 取含子组的 total_ 项，v2 只有不带前缀的
mstat() { cgv memory/memory.stat memory.stat | awk -v k="$1" '{v[$1] = $2} END {if (("total_" k) in v) print v["total_" k]; else if (k in v) print v[k]}'; }
cpuns() { v=$(cgv cpuacct/cpuacct.usage cpu,cpuacct/cpuacct.usage); [ -n "$v" ] && echo "$v" && return
          awk '$1 == "usage_usec" {printf "%d\n", $2 * 1000}' /sys/fs/cgroup/cpu.stat 2>/dev/null; }
# 进程出生时钟：/proc/<pid>/stat 第 22 项（开机以来的时钟数）；传 leader 时只认进程组长。同 fleet_guard.sh 的 proc_birth（各自经标准输入送去）
birth() {
  local s f
  { read -r s < "/proc/$1/stat"; } 2>/dev/null || return 1
  read -ra f <<< "${s##*) }"
  [ "${2:-}" != leader ] || [ "${f[2]:-}" = "$1" ] || return 1
  [[ "${f[19]:-}" =~ ^[0-9]+$ ]] && echo "${f[19]}"
}
echo "@@ info"
echo "clock $(date +%s)"
echo "btime $(awk '$1 == "btime" {print $2}' /proc/stat)"
echo "tck $(getconf CLK_TCK)"
echo "p1 $(birth 1)"
echo "nproc $(nproc)"
echo "memlim $(cgv memory/memory.limit_in_bytes memory.max)"
echo "memuse $(cgv memory/memory.usage_in_bytes memory.current)"
# 文件页（活跃、不活跃页缓存）：工作集 = 用量 − 不活跃的，收不回的内存 = 用量 − 两者（fleet.parse 算，同 worker 的 mem_unevictable）
echo "inact $(mstat inactive_file)"
echo "actf $(mstat active_file)"
echo "cpuq $(cgv cpu/cpu.cfs_quota_us) $(cgv cpu/cpu.cfs_period_us)"
echo "cpumax $(cgv cpu.max)"
echo "root $(df -Pk / | awk 'NR == 2 {print $2, $3}')"
# 根盘的文件系统类型与宿主机的盘（/etc/hosts 由宿主机绑定挂进来）总量：overlay 且根盘比宿主盘小一成以上（有配额）时，根盘已用就是开机以来写的（fleet.parse）
echo "rootfs $(awk '$2 == "/" {t = $3} END {print t}' /proc/mounts) $(df -Pk /etc/hosts 2>/dev/null | awk 'NR == 2 {print $2}')"
# /dev/shm 的挂载来源与已用（kB）：来源是 shm 的是容器私有的（续租同样清掉），tmpfs 的与宿主共用、不算这台的
echo "shm $(awk '$2 == "/dev/shm" {s = $1} END {print s}' /proc/mounts) $(df -Pk /dev/shm 2>/dev/null | awk 'NR == 2 {print $3}')"
c0=$(cpuns); t0=$(date +%s%N)
echo "@@ gpu"
nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null
echo "@@ util"
timeout 5 nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader,nounits -lms 500 2>/dev/null
c1=$(cpuns); t1=$(date +%s%N)
echo "@@ cpu"
echo "$c0 $t0 $c1 $t1"
APPS=$(nvidia-smi --query-compute-apps=pid,used_memory,gpu_uuid --format=csv,noheader,nounits 2>/dev/null)
echo "@@ apps"
echo "$APPS"
# nvidia-smi 给的是宿主机 PID：经 /proc/<pid>/sched 第一行对回容器里的 PID，顺带记工作目录
HP=" $(echo "$APPS" | cut -d, -f1 | tr -d ' ' | tr '\n' ' ') "
echo "@@ map"
KA=""                            # 命令行里有保活字样的 GPU 进程（容器 PID）
MAPPED=" "
# python 跑的脚本写的是相对路径时，按工作目录还原的完整路径（同 fleet._abs_script：取第一个不以 - 开头的参数，-m、-c 没有脚本；只做 normpath，#29）
script_abs() {
  local d=$1 a exe="" tok="" cwd skip=""
  while IFS= read -r -d '' a; do
    if [ -z "$exe" ]; then exe=${a##*/}; exe=${exe#-}; [[ "$exe" =~ ^python[0-9.]*$ ]] || return; continue; fi
    if [ -n "$skip" ]; then skip=""; continue; fi
    case "$a" in -m|-c|-[!-]*c) return ;; -W|-X) skip=1 ;; -*) ;; *) tok=$a; break ;; esac
  done 2>/dev/null < "$d/cmdline"
  [ -n "$tok" ] && [ "${tok#/}" = "$tok" ] || return
  cwd=$(readlink "$d/cwd" 2>/dev/null) || return
  realpath -m -s -- "$cwd/$tok" 2>/dev/null || printf '%s\n' "$cwd/$tok"
}
ka_pid() {                       # 命令行（后面接上还原的脚本路径，与 fleet.analyze 认保活同一口径）里有保活字样就记下
  local d=$1 c k
  shift
  c=$(tr '\0' ' ' 2>/dev/null < "$d/cmdline") || return
  c="$c $(script_abs "$d")"
  for k in "$@"; do case "$c" in *"$k"*) KA="$KA ${d#/proc/}"; return ;; esac; done
}
for d in /proc/[0-9]*; do
  { read -r l < "$d/sched"; } 2>/dev/null || continue
  h=${l##*(}; h=${h%%,*}
  case "$HP" in *" $h "*)
    echo "${d#/proc/} $h $(readlink "$d/cwd" 2>/dev/null)"
    MAPPED="$MAPPED$h "
    ka_pid "$d" "$@" ;;
  esac
done
for h in $HP; do
  [[ "$h" =~ ^[1-9][0-9]*$ ]] || continue
  case "$MAPPED" in *" $h "*) ;; *)     # 新内核在容器里直接给容器 PID：照样记工作目录
    [ -d "/proc/$h" ] && echo "$h $h $(readlink "/proc/$h/cwd" 2>/dev/null)"
    ka_pid "/proc/$h" "$@"; MAPPED="$MAPPED$h " ;; esac
done
echo "@@ ps"
PS=$(ps -eo pid=,ppid=,etimes=,time=,pcpu=,rss=,args= 2>/dev/null | cut -c1-400)
echo "$PS"
# 保活 GPU 进程与它们的子孙各自的 PSS（kB，共享页按份摊）：本机按它扣保活整棵进程树的内存，同 worker 的 mem_of（smaps_rollup，旧内核没有就逐段加 smaps）。
# 别人的实验进程不读：3.10 内核没有 smaps_rollup，读整份 smaps 要遍历页表、占着对方进程的 mmap_sem
echo "@@ pss"
for p in $(echo "$PS" | awk -v r="$KA" 'BEGIN {n = split(r, a, " "); for (i = 1; i <= n; i++) k[a[i]] = 1}
    {par[$1] = $2; ids[NR] = $1} END {do {c = 0; for (i in ids) if (!(ids[i] in k) && (par[ids[i]] in k)) {k[ids[i]] = 1; c = 1}} while (c); for (p in k) if (p in par) print p}'); do
  f=/proc/$p/smaps_rollup; [ -r "$f" ] || f=/proc/$p/smaps
  awk -v p="$p" '/^Pss:/ {s += $2; n = 1} END {if (n) print p, s}' "$f" 2>/dev/null
done
echo "@@ ka_groups"
for p in $KA; do
  { read -r gs < "/proc/$p/stat"; } 2>/dev/null || continue
  read -ra fields <<< "${gs##*) }"
  gg=${fields[2]:-}
  [[ "$gg" =~ ^[1-9][0-9]*$ ]] && b=$(birth "$gg" leader) && echo "$p $gg $b"
done
echo "@@ guard"                  # labmon 开的保活（fleet_guard.sh 记的）：开的时刻、进程组、盯到几点、谁开的、出生时钟，之后的让出与停止
GUARD=$(cat /tmp/labmon-guard.state 2>/dev/null)
echo "$GUARD"
read -r gk gt gg rest <<< "$GUARD"
if [ "$gk" = start ] && [[ "$gg" =~ ^[1-9][0-9]*$ ]] && b=$(birth "$gg" leader); then
  echo "proc $gg $b"             # 登记的进程组此刻的出生时钟：与登记里的对得上才是同一个进程
fi
if [ -n "$LABMON_SCAN" ]; then
  # 根盘开机以来新写的文件（同 worker 的 LocalDisk：两份，探针在未必有 Python 的镜像里跑）：ctime 不晚于开机的目录整棵不进
  # （overlay 上开机后写过东西的目录都已复制到上层、ctime 晚于开机），不跨文件系统，/proc /sys /dev /run 不进；只数 ctime 晚于开机的文件。
  # 按前三级目录汇总（四级以下的归到前三级，三级的是它自己，一、二级目录本层的文件记成 <目录>/.），输出：total 占用 kB 文件数 目录数，
  # 再按占用取前 30 个“占用 kB 文件数 最近写入 路径”；rc 是 timeout 的退出码（124 = 超时没扫完）。没有 GNU find（busybox 等）写 nofind。
  # 对方在跑实验照样扫（维护者 11-10 定，#155）。
  echo "@@ scan"
  if find --version 2>/dev/null | grep -q GNU; then
    IO=""; command -v ionice >/dev/null 2>&1 && IO="ionice -c3"
    R=${LABMON_SCAN_ROOT:-/}; R=${R%/}         # 测试指到假根盘；服务器上没设，就是 /
    timeout "${LABMON_SCAN_S:-60}" nice -n 19 $IO find "$R/" -xdev -mindepth 1 \( -path "$R/proc" -o -path "$R/sys" -o -path "$R/dev" -o -path "$R/run" \) -prune \
        -o -type d ! -newerct "@$LABMON_SCAN" -prune -o ! -type d -newerct "@$LABMON_SCAN" -printf '%k %C@ %p\n' 2>/dev/null |
      awk -v r="$R" '{p = $0; sub(/^[^ ]+ [^ ]+ /, "", p); p = substr(p, length(r) + 1); n = split(p, a, "/") - 1
            if (n >= 4) k = "/" a[2] "/" a[3] "/" a[4]; else if (n == 3) k = p; else {k = p; sub(/\/[^\/]*$/, "", k); k = k "/."}
            if (!(k in b)) nk++
            b[k] += $1; c[k]++; if ($2 > m[k]) m[k] = $2; tb += $1; tn++}
           END {print "total", tb + 0, tn + 0, nk + 0; for (k in b) print b[k], c[k], int(m[k]), k}' |
      { IFS= read -r l; echo "$l"; sort -rn | head -n 30; }
    echo "rc ${PIPESTATUS[0]}"
  else
    echo "nofind"
  fi
fi
