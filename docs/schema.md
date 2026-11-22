# 快照结构（schema 2）

项目输出目录里的 `snapshot.json`（登记目录 `<状态根>/projects/<id>/out/`，#161，见本页末尾），也内联在 `index.html` 里。时刻一律是 Unix 秒（`*_ts`、`generated`、`t`、`t0` / `t1`、`last_ok`、`frozen_at`、`returned`），
页面用 common.js 的 `ts` 一类函数显示；时长是分钟（`*_min`），容量是 GiB（`*_gb`）。
不再成立或取不到的字段为 `null` 或缺省。改字段含义时把 `collect.SCHEMA` 加一。

```jsonc
{
  "schema": 2, "id": "a1", "title": "A1 实验监控", "generated": 1793776260.1,
  "refresh": 60, "interval": 120,
  "all_done_ts": 1793970791.0,                 // 各车道完成时间的最大值（排不上、不知道时长的车道没有完成时间，不计）；只在项目页快照，工作台的 projects[] 没有它
  "no_work_since": null,                       // 暂时没实验（清单里没有待办、worker 上也没有本项目在跑在等的）的起点：最后一个任务跑完的时刻；
                                               // 这时本项目不报主机的事（ssh_fail、disk_low、idle），往清单里加任务就接着跑
  "rebalance": null,                           // 本项目的挪法建议（全局快照 rebalance 里本项目那一项，结构见“工作台的全局快照”；没有为 null），
                                               // 有 key 时出 rebalance: 告警，页面上 rebalMark 标一个图标（docs/workbench.md「挪法建议」）
  "lan": {"ok": false, "since": 1793776000, "checked": 1793776260},   // 只在本机不在内网时有（lan.check）：这轮没连 ssh，数据是离开前的，
                                               // ssh_fail、log_silent、noprogress、gpu_idle 这几条告警不报；页面顶上据此写说明
  "quiet": [{"host": "29", "root": "/root/data1/alice/projects/a1", "queue": "jobs/old.txt", "last": 1793500000, "idle": true}],
                                               // 这一轮收起的只在登记里有的车道（跑完久了没动，collect._quiet，#117）；last 最后一次活动（epoch 秒，没有为 0）；
                                               // idle：登记还在、空了 2 天以上，进了 idle_regs: 提示（collect._idle_regs，#47）；
                                               // 工作台 projects[] 带上，项目「管理」抽屉的逐条清单不列它们（#121），悬停写其中几条已提示会话

  "hosts": [{
    "id": "29", "label": "29（A6000）", "ok": true, "error": null,   // 掉线按 health.link_step 判（连续失败 ≥ 2 次且持续 150 秒），掉线了才报 ssh_fail 告警
    "stale": false, "last_ok": null,           // 探针失败时显示上次数据：stale=true，last_ok 为其时刻
    "returned": null,                           // 已归还时为归还时刻（主机清单里写的文字由 config 读入时换算）：不再连接，ok=false、error="已归还"，其余字段都没有
    "borrow": null,                             // 借来的主机（全局快照里 borrow.check 算好的）：{holder, since, until, for, light（借来时对方在轻用）, away（对方说好不用到几点，Unix 秒；没勾或已作废为 null）, swap（换给自己借来的）, cutoff（约定时间，Unix 秒）, why,
                                                //   returning, now, back（对方超出预留）, tight（快撞上限）, overdue, keep（给对方留的量；说好不用的时段里带 away，keepText 据此写说好不用）, other（对方的用量 {vram 各卡 MB, ram GiB, cpu 核}：内存与 CPU 不含任何保活的进程树，`ka.sh` 停不了的对方保活也不算，与预留同口径（#37）；显存仍含停不了的保活（它是 labmon 之外的 GPU 进程，那张卡本来就不放），所以抽屉里“对方此刻”的三项口径不一）, late（约定归还挡住、这台不会再放的清单 [{project, root, queue, name, why, n}]，borrow.late_jobs；这一轮没拿到 worker 状态时沿用上一次的，back、tight 同样）, late_do（late 非空时该怎么办的一句，borrow.late_do，否则 null）}；页面上 borrowBadge、borrowText 读它
    "gpus": [{"index": 0, "name": "NVIDIA RTX A6000", "util": 99, "mem_used": 30720, "mem_total": 46068,
              "temp": 84, "power": 284, "power_limit": 300}],          // 显存单位 MiB；util 是探针此刻的值（不到 1 秒，含保活）
    "busy": [{"i": 0, "now": 0, "m10": 12.5, "h1": 40.2, "cov": 3600, "ka": 99, "d1": 55.0, "d1cov": 86400}],
                                                // 各卡的吃满程度（#51），与 gpus 一一对应：全局快照主机项的 busy 照搬（见下文工作台快照）；worker 不在时没有，页面退回 gpus 的 util
    "gpu_procs": [{"pid": 92352, "mem": 21197, "cmd": "…"}],
    "cgroup": {"cpu_usage_ns": 0, "cpu_quota": 17, "mem_used": 0, "mem_limit": 0, "mem_rss": 0},
    "cpu_pct": 12.3, "loadavg": [14.8, 15.4, 15.9],
    "mounts": [{"path": "/tmp", "label": "容器 /tmp", "total_gb": 1024.0, "avail_gb": 24.9, "used_pct": 97.6,
                "min_gb": 10}],                 // 容量与 min_gb 都是 GiB（2^30）；disk_low 按 min_gb 与配置的 max_used_pct 判
    "checks": [{"id": "keepalive", "label": "GPU 保活在跑", "state": "ok|fail|na", "out": "…", "detail": ["…"]}]
  }],                                           // 有交给算力池的清单时另有一项 {"id": "pool", "label": "算力池", "pool": true, "ok", "error"}（#24）：ok 是代读它的在用主机这轮读到了，其余字段都没有

  "lanes": [{                                   // 一条车道 = 一条清单（一次一行；写了 `# labmon: par=N` 的同时至多 N 行，#147）
    "id": "r9_a6000", "label": "车道 2", "host": "29", "type": "gpu_queue",   // 自动发现的车道 id 为 <队列 id>:<清单名>；只在 worker 登记里有的为 <目录末级名>/<清单>（#94）
                                                 // label：同机照配置顺序编号的车道名，配置里写了 label（用途）的接在后面
    "alive": true, "pid": 6341, "missing": false, "stale": false, "started": true,   // started=false：从没启动过，不报 lock_dead
    "paused": null,                              // 队列进程不在且状态目录有 paused 标记时为其内容（原因）；此时 eta_ts 为 null
    "managed": true, "waiting": null,            // worker 在管这条队列（status.json 新鲜且登记了它）：没有锁也不报 lock_dead；waiting 为 worker 说的等待原因
    "held": null,                                // 挡住它的临时指令种类（drain、pause、deadline、guest，worker 给）：没有在跑的任务时按暂停算（paused 写 waiting）
    "held_t": null,                              // 那种指令最晚下的时刻（worker 状态 overrides 的 t，探针取）：drain: 告警的 key 带它（#129）
    "dep": null,                                 // 队首的依赖没满足（worker 给）：wait 在等；failed 等的跑失败了、missing 找不到、cycle 成环，这三种出 dep: 告警；都不报 GPU 空转
    "head": null,                                // 队首等待项的任务名（worker 等待项的 name，探针取）：dep: 告警的 key 带它（#129）
    "stuck": null,                               // worker 推演里排不上的原因（放不下、截止前跑不完等）：此时 eta_ts 为 null
    "cpu_wait": null,                            // 队首一直因 CPU 被挡满 10 分钟后已挡的秒数（worker 的 cpu_wait，阈值只在 worker）：出 cpu: 告警
    "par": 1, "running_n": 1,                    // 清单声明的并行数（worker 状态 queues[] 的 par，不归 worker 管的为 1）与此刻在跑几行（#147）；waiting、held、dep、head 取这条清单的第一条等待项，窗口里有依赖等不来的取它，cpu_wait 取最久的；
    "bad": {},                                   // 依赖等不来的行 {任务: 原因}（并行清单 wait --job 只把这一行判成不会再跑）；并行车道排队任务的预计时刻照搬 worker 推演，不另对齐
    "holding": false, "has_hold": false,         // 正在跑暂停任务 / 未完成任务里有暂停任务
    "state_dir": "/root/…/results/_queue",
    "logs_dir": "/root/…/results/_logs",         // 日志目录（探针按清单 .env 的 QUEUE_LOGS 解析，任务日志是 <logs_dir>/<任务>.log）；labmon wait --job 写结果位置用，#135 前的快照没有
    "returned": null, "read_via": null,          // 所在主机已归还：归还时间；经哪台共用盘的主机代读（算力池的车道也写代读它的在用主机）
    "pool": false,                               // 交给算力池、还没放到哪台的（host 为 pool，#24）：没有 worker 管，alive 为 false、不报 lock_dead，waiting 写在池里等着
    "frozen_at": null,                           // 已归还且没有主机代读时：归还前最后一次采到的时刻
    "fs_skew": null,                             // 队列状态所在盘与主机的时钟差（秒，盘快为正）；探针已据此校正各文件时间
    "root": "/root/data1/alice/project-a/a1_r9", "queue": "jobs/r9_a6000w.txt",   // 队列目录与清单（配置写的或登记的；自动发现的取发现到的）：页面按它认 worker 的等待、队列结束事件
    "overrun": false,                            // 当前任务已超估时（eta_ts 因而偏早）
    "total": 22, "done": 8, "failed": 0, "unfinished": 14,
    "no_gpu": 0,                                 // 未完成里不用卡的：worker 推演里这一条的 gpus 为 []（在跑的没分到卡、排队的推演里不给卡）；gpu_idle 告警不数它们
    "eta_ts": 1793970791.0
  }],

  "jobs": [{
    "id": "x4_s146_seed0", "lane": "r9_a6000", "group": "E4",
    "status": "done|failed|running|queued",
    "host": null,                                // 运行中、且在车道之外的主机上跑时才有：current 里放行的主机（清单搬走后原主机接着跑完手上那一格，#95）；
                                                 // 项目页把这一格画到那台、标「在 X 上跑完」，wait --job 照它说在哪跑（#100）；
                                                 // 交回算力池的车道上跑完、失败的也有：用量记录里这条清单最后一次跑它的那台（collect._attempts，#99）
    "hold": false,                               // 暂停任务（名字按词含 hold / pause，config.HOLD_RE）：group 为 null
    "killed": null,                              // 失败且退出码为 130/137/143 时为退出码（被信号终止）
    "suspended": false,                          // 运行中且任务进程（或其子孙）被 SIGSTOP 挂起（探针读 /proc）
                                                 // elapsed_min、actual_min 都已扣除累计挂起的时长
    "est_min": 216, "dur_min": 170, "dur_src": "同类近 3 次 实际/估时 ×0.79",   // 清单估时、预计时长（worker 的 duration）与依据
    "start_ts": 1793775456, "end_ts": null,      // 完成或失败的任务有 end_ts，并尽量给出 start_ts 与 actual_min
    "actual_min": null, "elapsed_min": 13.9, "remaining_min": 541.7,
    "progress": [50, 2000], "eta_source": "近期速度|进度|进度（停了一阵）|估时（进度刚开始）|剩余按已用的 20%（进度刚开始）|估时（…）|<dur_src>|已超估时|无估时",
    "overrun_min": null,                         // 没有进度行、按估时推算且已超出时为超出的分钟数
    "rate_per_min": 4.2,                         // 运行中且有进度：当前采用的速度（步 / 分钟）
    "speed": [[1793777311, 4.18]],               // 运行中且有进度：近 6 小时相邻两次采样间的速度
    "plan_start_ts": null,                       // 排队任务按 worker 推演的开始时间，与这一轮在跑任务的预计结束对齐、不早于现在（collect._queued_times；不经 worker 的队列没有）
    "after": null,                               // 推演里要等哪些任务腾出资源才轮到它：[[项目, 任务]]
    "deps": null,                                // 清单行写的依赖（after=、afterany=，worker 推演的 plan 带来）：[[项目, 任务]]，afterany 的第三项为 "any"；
                                                 // 暂停任务写了依赖时 eta_ts 取 worker 推演的（依赖都满足的时刻），dur_src、eta_source 写“等 X、Y 跑完”，
                                                 // dur_min 是推演里它从开始到放行的分钟数
    "eta_ts": 1793808791,                        // 运行中或排队任务的推算结束时间
    "last_line": "PROGRESS 50/2000 …", "log_mtime": 1793775964.9    // last_line、progress 只看这一次的输出（日志里最后一行分隔之后），逐字出现过的行跳过，还没输出时为 null
  }],

  "groups": [{"id": "E4", "label": "E4 多种子多步", "color": "#D85A30", "auto": false,   // auto=true：按前缀自动归组，id 即前缀（如 x5_）
              "status": "完成|运行中|排队中|跑完待汇总|有失败|未排队",
              "total": 14, "done": 0, "failed": 0, "running": 1, "queued": 13, "eta_ts": 1793970791,
              "ratios": [{"cls": "x4_s146", "k": 0.79, "src": "同类近 3 次 实际/估时 ×0.79", "open": true}],   // 各类任务的 实际/估时（open：还有没跑完的）
              "done_when": {"file": "tasks.md", "pattern": "^\\s*- \\[x\\] E4"}}],   // 配了 done_when 的：跑完待汇总时页面提示勾哪里

  "alerts": [{"key": "lock_dead:r9_a6000:6341", "to": "agent", "msg": "…"}],   // collect.alert 产出（#142）：key 带“这一次”（#129，见 workbench.md「告警」）；to 给谁看 user / agent / both；
                                               // msg 给实验会话（labmon wait 与命令行）；page 只在 both 时有：给用户的那句人话（collect.user_alerts、页面的 forUser 取它，
                                               // 没有就是 msg）；to=agent 的页面上不计数、只收在折叠的「给实验会话的提示」里

  "history": [{"t": 0, "host": "29", "ok": true, "util": [99], "busy": [12.5], "mem": [30720], "cpu_pct": 12.3, "ram": 0,
               "disk": {"/tmp": 24.9}}],         // 近 24 小时；各挂载点余量（GiB；11-07 前存的按 GB，过了保留期自然没有）；
                                               // util 是探针此刻的值，busy 是各卡近 10 分钟的吃满程度（worker 的 gpu_busy.m10，#51；worker 不在、样本不够时没有）：
                                               // 时间线主机行的浅底、利用率小图、util_hourly 与 gpu_idle 告警按 busy

  "markers": [{"date": "2026-11-17", "label": "截稿", "ts": 1794931199}],   // 只写日期时 ts 取当天结束（23:59:59）

  "spans": [[1793780000, 1793857000]],          // labmon 在采集的区间（断开超过 3 个采集间隔、至少 10 分钟另起一段），存 30 天
  "util_hourly": {"29": [[1793775600, 99.0]]}, // 各主机每小时的平均 GPU 利用率（整点时刻），存 30 天；逐次采样只在 history 里留 24 小时。
                                               // 按 history 的 busy 平均（只有保活时按 0，#51）；11-17 之前的行是探针瞬时值、含保活，存满 30 天自然滚出
  "gpu_mine": {"util": 63.2, "card_s": 52200, "n": 4},   // 本项目近 24 小时的平均 GPU 利用率（collect.gpu_mine，#51）：各任务所用卡的平均利用率（util_mean），
                                               // 按 卡数 × 落在窗口里的秒数 加权；card_s 卡·秒，n 任务数；窗口里没有用卡的任务为 null
  "attempts": [{"name": "x4_s20_seed1", "lane": "r9_a6000", "t0": 1793850748, "t1": 1793850780}],
                                               // 30 天内失败的尝试（任务后来重跑成功的也在）：取工作台存的用量记录 overview/usage/<主机>.jsonl，
                                               // 按项目 id 与车道的 root、queue 对上（collect._attempts）；只有 worker 管的车道有；
                                               // 交回算力池的车道看各台的记录，另带 host：在哪台失败的（#99）

  "thresholds": {"stale_s": 420},               // 页面据此标“N 分钟未更新”：config.stale_limit 算的秒数，与 labmon wait 的 collector_stale 同一门槛（#141）

  "storage": {                                 // 本项目的存储（#53，disk.project_storage，取上一轮全局快照的 disk）：项目页「本项目占用」、labmon disk 读它。容量都是字节，时刻 epoch 秒
    "roots": ["/root/data1/alice/projects/a1"],  // 共享盘上本项目的项目根（各台 worker 按登记的清单记的 storage.projects）
    "b": 61203283968, "d24": 35433480192, "d7": 44023414784,   // 合计占用、近 24 小时与 7 天的增量（含删掉的目录，按扫描记录插值）
    "pending": 0,                              // 项目根下还没扫到的单位数（刚拆开时），合计只算扫过的
    "burst": true,                             // 近 24 小时涨得快（同按人的 burst:：超过 burst_gb 且超过剩余的 10%），出 disk:<项目> 告警
    "dirs": [{"path": "/root/data1/alice/projects/a1/cache", "name": "cache",   // 项目根下的一级目录（扫描单位）：name 相对项目根，「.」是项目根本层的文件，
              "b": 45097156608, "d24": 33285996544, "d7": 41875931136,        // 空串是旧 worker 没拆开时的整个项目目录
              "n": 1203, "m": 1793776000, "t": 1793775000,                      // 文件数、最新修改、扫描时刻
              "top": [["feats", 42949672960, 1793776000]]}],                    // 直接子项里最大的几个 [名字, 占用, 最新修改]（最多 5 个）
    "gone": [{"path": "…/a1/tmp_old", "name": "tmp_old", "d24": 0, "d7": -5368709120}],   // 7 天内删掉的目录（增量为负）
    "jobs": [{"name": "x4_s146_seed0", "host": "29", "t0": 1793770000, "t1": 1793774000, "st": "done",   // 近 7 天结束的清单任务的写入量（近的在前，最多 30 个；
              "b": 7408566272,                 // 只留净增 64 MiB 以上或还在等重扫的）：b 是开跑前最后一次扫描到跑完那次重扫，项目根下各单位之差的和（同期本项目别的任务写的也在内）
              "top": [["cache", 6979321856]],  // 增量最大的几个目录 [name, 增量]（最多 3 个）
              "from": 1793769000, "to": 1793775000,   // 用到的最早、最晚的扫描时刻（没有变化为 null）
              "with": 0,                       // 这段时间本项目还有几个别的任务（跑完的与在跑的）
              "pending": 0, "wait": false,     // 结束后还没重扫的单位数；wait：还在等重扫（满一天就按已扫到的算，不再等）
              "big": true,                     // 算完了且 b 超过 bigwrite_gb：出 bigwrite:<grp> 告警（结束 24 小时内，只给实验会话）
              "grp": "x4_s146_seed0:1793774000"}],   // 用的是同一组扫描（并行的）的几个任务里最早结束的那个「任务:t1」，bigwrite: 按它合成一条
    "at": 1793775000, "free": 236223201280, "total": 1181116006400,   // 共享盘扫描索引的时刻、整块盘的剩余与总量
    "eta": 6.3, "eta7": 9.1,                   // 按近 24 小时、近 7 天已用的增长速度约几天满（不涨为 null）
    "warn_gb": 30, "burst_gb": 30, "bigwrite_gb": 5,   // 告警线（[fleet.disk] 可改）
    "local": {"29": {"cache": 1.5e10, "scratch": 2e9}}  // 各主机本地盘上本项目的缓存与单格临时（worker 状态的 storage.local）
  }
}
```
项目页的 `/p/<id>/events.json` 只给时间线的主机事件、同机别的项目的占用与配色；本项目的存储在项目快照的 `storage`（#53 起）。

字段设计参照了 pueue `status --json` 的 task 与 group：车道对应 group，任务的状态与起止时间对应 task。以后接 pueue 时，适配器只需做字段映射。

## 工作台的全局快照与接口

工作台的快照在状态目录 `overview/snapshot.json`（`overview.collect` 写，页面经 `/overview.json` 取），不进 schema 号。与项目快照不同的几处：

- `hosts[]` 每台在用主机一条：`error` 只表示连不上（进掉线判定）；`status_error` 是连得上但 `status.json` 读不出（半行、坏文件），页面另标，不算掉线，也不触发自动拉起 worker。
  `status` 是 worker 的 `status.json` 原样：`running[]` 的开始时刻是 `t0`（epoch 秒），等待项的 `start` 是推演的预计开始（epoch 秒），
  `queues[]` 的 `par` 是清单声明的并行数（`# labmon: par=N`，没写为 1，#147）：并行清单在 `running[]`、等待项与 `plan.lanes[].jobs` 里同一条清单可有几项（车道仍按 `root`、`queue` 认），状态目录的 `current` 一行一个在跑的；
  等待项的 `miss` 为真表示缺本地文件又补不了（看板的提醒与主机页按它认，不按 `why` 的文字）；`dep` 是依赖没满足（`wait`，以及出提醒的 `failed`、`missing`、`cycle`，见 [worker.md「依赖」](worker.md#依赖)）；
  `dep_miss` 是依赖等的、哪里都找不到的任务与起算时刻 `[[项目, 任务, epoch 秒]]`（满 2 分钟才报找不到，worker 接管时从上一份状态找回，见 [worker.md「依赖」](worker.md#依赖)）；
  `todo` 是这台还会跑的任务（每条清单一项 `{project, root, queue, jobs 在跑的在前、其余按清单顺序, deps {任务: [[项目, 任务]]}, gang {任务: [组名, 人数（没写为 null）]}（成组的行，#180）, held paused|pause|drain|null, par（并行清单才有，#147：依赖成环的判断里窗口内的行不算互相排队）}`，在跑、在等的单元各一项，root、queue 为 null；别的主机的依赖按它认，offers 按它取别的主机各清单还没跑的行）；`local[]` 的 `how` 是 `copy`（有主副本可拷）、`redo`（命令会自己重算）、`mixed` 或 null（补不了），
  `external[]`（旧 `gpu_queue.sh` 正在跑的）的开始时刻同样是 `t0`（worker 读状态目录的 `current` 时就换算，`read_current`；探针 `probe.py` 读 `current` 时同样给 `t0`）；
  `done[]` 是这台 `usage.jsonl` 的尾巴（最近 60 行），起止是 `t0` / `t1`（epoch 秒，整数）。
  `info.press` 是主机的 CPU 与读写压力（[worker.md「CPU 与读写」](worker.md#cpu-与读写)）：`{thr 被限流的周期 %, psi {cpu, io, memory} 或 null,
  nfs {path, addr, ms 每次读写的执行时间, ops 每秒次数, mib MiB/s, base 近 14 天基线 ms, n 样本数, need 够用的样本数, floor 基线下限 ms, min_ops 算慢的次数下限, slow 此刻慢不慢} 或 null, disk {dev, busy %, await ms} 或 null}`，“此刻”是近 1 分钟，读不到的为 null；
  `throttle` 是这一段 CPU 限流（worker 的 `Worker.throttle`，#39；没在限流为 null）：`{since 起点（epoch 秒）, for 已持续秒数, thr 此刻被限流的周期 %, alert 持续满 10 分钟, over [{project, name, cores 1 分钟窗口核数, give 开跑时给的核数}] 超核的任务, other labmon 之外此刻占的核（含旧 `gpu_queue.sh` 的任务，不含保活）或 null}`，门槛见 [worker.md「CPU 与读写」](worker.md#cpu-与读写)；
  `info.mem` 是 `{limit, used 工作集, anon 收不回的内存（用量 − 文件页，含 shmem 与内核占用；放行与内存闸门看它）, src, peak, failcnt, oom_kill}`（GiB）；
  `info.disk` 只有共享盘一项 `[{path, free, total}]`（GiB；11-10 前还有 `/tmp` 一项，#149 起本地盘看 `info.rootfs`）；`info.rootfs` 是容器根盘 `{used, total, fs, ok 测得准（overlay 且有配额）, shm 容器私有的 /dev/shm 已用（与宿主共用的没有这一项）}`（GiB），读不到为 null；
  `storage` 是本地盘扫描（`LocalDisk.summary`，[worker.md「本地目录与本地依赖」](worker.md#本地目录与本地依赖)）：`{t, local {项目: {cache, scratch}}, own labmon 目录的总占用, wrote labmon 之外的新写入 {since 起算时刻（开机，借来的是借来时）, b, n 文件数,
  top [[路径, 字节, 最近写入]] 前 8 处, more [其余几处, 字节], pending 还没扫的单位数, t 最近扫完一个单位的时刻}（测不准时为 null）, projects {项目目录: 项目}}`（字节）；
  主机项另有看板算的 `lw`（`fleet.local_writes`，GiB）：`{used, total, own, user labmon 之外的, shm, ok, risk 续租、关机、换人要先勾确认, scan {since, t, b, n, top [[路径, GiB, 最近写入]], more [几处, GiB], partial, pending, err} 或 null}`；
  GPU 利用率两种口径（#51，worker 每 10 秒一个样本，`win_mean` 取窗口平均）：`util10 {UUID: %}` 是「在用时多高」，近 10 分钟、卡上有保活的那几轮不记（fit 列出来看）；
  `gpu_busy {UUID: {now 此刻, m10 近 10 分钟, h1 近 1 小时, cov 1 小时窗口实际覆盖的秒数（worker 刚起时不满）, ka 此刻只有保活时保活顶出来的原值}}` 是「吃满程度」，卡上只有保活的那几轮记 0（页面上的利用率）；样本不到 3 个的平均为 null。
  主机项另有看板算的 `busy`（`overview.gpu_busy`）：按 `info.gpus` 的顺序每卡一项 `{i, …gpu_busy 那几项, d1 近 24 小时平均, d1cov 它覆盖的秒数}`，`d1` 是 `series` 各行 `m10` 的平均；离线沿用旧状态、worker 不在（`worker_up` 为假）时没有，曲线那一行的 `m10` 也记 null。
  `room` 是按记账还能放多少：`{ram, margin, gpus {UUID: MB}, cpu 核, cpu_cap 记账上限（配额向下取整）, other_cpu, keep, owner}`，读不到配额时 `cpu`、`cpu_cap` 为 null；`other_cpu` 是认得准的 labmon 之外的核数（容器给 2 核以上时才有，否则 null），`keep` 是各卡给对方留着、对方还没用上的显存 `{UUID: MB}`（只列非 0 的），`owner` 是不放的卡（对方超出预留或刚回落、有停不了的保活），`worker fit` 按这几项判整机独占、整卡封顶与卡；借来的主机上 `guest.block` 是整台不放新任务的原因（对方内存或 CPU 超出预留、刚回落：`guest_block`，没有为 null）；
  `running[]` 的 `est` 是资源估计（`estimate`）：`vram`（每卡 GiB）与来源 `src`、`ram`（GiB）与来源 `ram_src`（#61 起 `src` 只指显存；之前放行、被接管的任务没有 `ram_src`，与 `src` 同一来源）、`util`，带 `cpus`（估计核数，即记账的核数）、`cpu_src`（来源）、按实测收小了记账时另有 `cpus_set`（清单写的 `cpus=`，开跑仍按它给，#91），`acct` 带 `cpu`（记账核数 = max(估计封顶到 `cpu_cap`, 实测)，开跑满 10 分钟后估计那份换成近 10 分钟窗口核数的最大值、不超过给的核数、不低于 1（#65），占位任务 0），`io_sus` 是记了“读写拥塞嫌疑”的时刻（没记为 null），`cpu_fit` 是开跑核数给少了、给多了（`{how: low|high, avg, peak, give, sug, by, cls}`，`by`/`cls` 是类：`tag` 与 tag，或 `name` 与去种子号的名字，#91；没有或不满 10 分钟为 null，#46，[worker.md「CPU 与读写」](worker.md#cpu-与读写)）；
  `waiting[]` 的 `est` 同样带 `ram_src`、`cpus`、`cpu_src`，`cpu_wait` 是一直因 CPU 被挡满 10 分钟后已挡的秒数（不到为 null），`elsewhere` 是整条清单在等别的主机放行的那一格收尾时的 `[主机, 任务]`（`run_elsewhere`；没有为 null，算力池据此把它收回池里，#80）；`disk`（共享盘扫描，`mine` 为真的那台在扫）的 `yield` 是共享盘慢、推迟中的说明（没推迟为 null）；
  `running[].use` 另有 `rd`、`wr`（此刻 MiB/s）、`rd_b`、`wr_b`（累计字节）、`cores_peak`、`rd_peak`、`wr_peak`（1 分钟峰值），`conc`（运行期间同卡平均几条 labmon 任务，含自己；不用卡的没有），以及接管时续算用的内部键 `rchar`、`wchar`、`pw`、`conc_s`、`conc_w`、`pks`（近 10 分钟各 1 分钟窗口的 [结束时刻, 核数]，CPU 记账回收用）、`pk0`（从何时起量窗口核数）（看板不读），`running[]` 与 `waiting[]` 带清单的 `cpus`、`io`；
  `done[]` 的这几项同名（`cores_peak`、`rd_peak`、`wr_peak`、`rd_b`、`wr_b`、`cpus`、`io`；11-08 前的行没有，读的地方当没有）；`give` 是那次开跑导出的 `LABMON_CPUS`（按那台配额封顶后的，`cpu_shrink` 按它认，#99；没开跑就记失败的成组成员为 null，之前的行没有）。
  `done[]` 的 `oom` 是失败是不是 OOM：`"vram"`（CUDA 显存）、`"ram"`（容器内存被杀）或 null（跑成功、别的失败、停下重排、借来的主机上被挤掉的；11-10 前的行没有，当作 null），
  资源估计据此抬高同类（[worker.md「准入」](worker.md#准入)，#62）；`est` 是放行时的资源估计（同 `running[].est`）。
  成组（[worker.md「成组」](worker.md#成组)，#180）：`running[]` 带 `gang`（组名，不成组为 null）、`gn`（人数）、`gang_with`（一起开跑的同伴 `[[主机, 任务, 开跑时见过的它最近一次结束 t1（那台的时刻，没有为 0）]]`：之后的结束才算这一次的）、
  `gang_stop`（跟着同伴停下的 `{kind fail 记失败 | stop 重排, why, sent 停止请求发了没有（boundary 停法为假）}`，没有为 null；这几项接管时找回）；
  `done[]` 带 `gang`（11-10 前的行没有）；成组的成员被作废、跟着失败的同伴停下的记失败：`requeued` 为假、`void` 为 null（`requeued` 照实际有没有重排写），进程自己退出码为 0 的 `rc` 记 null（各处只认 `"0"` 为跑成功，别的主机的依赖据此认它失败）；先开跑的同伴在这边跟上之前就失败、这边没开跑也记失败的，另写一行 `min` 0、`gpus` []、`rc` null；
  `voided[]` 的 `how` 另有 `gang`（跟着同伴停下、重新排队）；`plan` 车道的 `jobs[]` 里成组的带 `gang`。
  各台另写 `hosts/<主机>/gang.json`（看板不读，同组的别的主机每轮读）：`{t 写的时刻, host, members [{project, gang, n, name, dur 自己的预计时长（分钟）, cut 截止前的准入用的时长（已含余量，#139；旧 worker 不写，读的按 dur × 1.2）, state,
  wait 时 eta（推演里最早何时放得下，说不出为 null）与 why, ready 时 t（从何时留着）, run 时 t0 与 with（即 gang_with）, done / fail / stop 时 t0、t1 与 with（留一天；没开跑就跟着记失败的 t0 = t1、with 为 null）}], known 近 3 分钟见过的同组别的主机上的成员 [[项目, 组名, 任务, 主机, 何时见到]]（重启后读回）}`（epoch 秒）。
  `kafight` 是近 1 小时要卡时保活又被拉起（`n` 次数、`hits` 每次的 epoch 秒、`t` 最近一次、`who` 是谁拉的；没有为 null），`ka_stop` 是要卡的这段里 worker 停着保活时停它的时刻（epoch 秒；没停着为 null；这两项 worker 重启接管时找回，#124），`keepalive` 是 `{ctl worker 管不管这台的保活（有 ka.sh）, why 没有任务要卡却不拉起的原因（别的队列登记在跑、还有进程在用 GPU，没有为 null）, stuck 要卡时 worker 停不了它（连着没停成满 2 分钟、至少两次：{t0 起始 epoch 秒, n 次数, why 原因}，没有为 null；#102）, mine worker 自己经 ka.sh start 拉起、此刻还在的保活（{g 进程组, born 组长出生时钟（/proc 第 22 项）, t 启动时刻 epoch 秒}，没有为 null；归还时 borrow.finish 据此登记成看板的保活，#102）}`，`gpu_procs[]` 是 nvidia-smi 列的 GPU 进程（`hpid` 宿主机 PID 即 nvidia-smi 里的那个、`pid` 容器内 PID、`mb`、`uuid`、`owner` job|keepalive|other、`ka` 借来的主机上 ka.sh 停不了的保活、`job`；保活的另有 `t0` 启动时刻 epoch 秒），`labmon worker status` 与 `labmon status` 按这几项写保活那几句（`ka_lines`，#61），`watchdogs` 是 `frozen.json`（`frozen` 暂停着的看门狗、`seen` 找到了没动的，
  每项 `pid`、`start`、`cmd`、`cwd`、`why`；`seen` 里确定的另有 `locks`：它平时持着的文件锁，`/proc/locks` 的 `主:次:inode`；第一份是要卡这段里取的（保活被拉起时，或保活已被 worker 停着时找到的）另有 `locks_t`，那一刻的 epoch 秒；`frozen` 里的不带，#38），见 [worker.md「保活」](worker.md#保活)。
  `offers` 是这台能接别的主机上哪些任务（[worker.md「能接别处的任务」](worker.md#能接别处的任务offers)）：每个项目一项（同机的挪法另列，带 `local: true`：多开一条车道的 `lane` 为 null、带 `gpus` 新车道只许用的卡号（不是全部卡时；否则 null），#43；挪到本机另一条早跑完的清单的 `lane` 是那条清单、`gpus` 为 null，#110）`{project, local, lane 接在本机哪条清单后面（[目录, 清单]，null 要新建）, before 插在那条清单里哪一行前面（它写了依赖；null 接在末尾）,
  free 本机这个项目原推演里何时跑完（没有为现在；推不出为 null）, ends {"主机|目录|清单": 别的主机上这个项目各清单预计何时跑完（推不出为 null）},
  jobs [{host, root, queue, name, there 原处预计结束（跑不了为 null）, rest 挪走它之后原处那条清单何时跑完, end 挪来前几行时它们在这台何时都跑完, own 这时本机这个项目自己的清单何时跑完, dep 这一行写了 after=/afterany=}]（按挑的顺序，从队尾往前；同机的 rest 是新推演里本机原清单何时跑完）,
  why 再往下挪不了的原因（只放原因本身，没有为 null）, stop 挪到这台排不上的那一行（why 是推演的原因；与具体某行无关的为 null）, nodur stop 那一行是不是因为没有估时, idle 这台此刻空着（没有 labmon 任务在跑、推演里 10 分钟内也没有要开跑的；挑行跳过挪不动、说不出何时放行的清单，同机那项不带，#62）, room 这台卡有真余量时 {busy 能放的卡近 30 分钟吃满程度 %（窗口没满为 null）, lanes 此刻还能给这个项目再开几条车道}（不出为 null，`Worker.lane_room`，#145）}`；原因句由看板拼（`rebalance.suggest`）。
  `lan_off` 为本机不在内网（这时沿用上一轮的 `status`）；借来的主机带 `borrow`（同上）。
  算力池借来的（#25）：`borrow.pool` 为真（`holder` 照写持有人，`by` 是「算力池」）；换了容器的那一轮另带 `gone: true`（`pool._returns` 标，工作台不重新拉起 worker）。
  自用主机（不是借来、换来的）另带 `lend`（#79，`pool.tick` 写的 `pool.lend_view`：`{on, hours, open, lent, returning, rest, say, mine: true, placed 此刻这台上池放的清单数, bar 不能入池的原因, holder}`；组内页 `fleet.mine[]` 的 `lend` 是同一份）。
  借来的主机的 `status.guest` 另有 `owner`（算力池用：对方超出预留的几项里去掉只因 `ka.sh` 停不了的保活超出的卡，算力池借的再加上有了对方 GPU 进程的卡；非空算主人回来了），各台状态另有 `pool_heads`：
  `{"目录|清单": {head 队首（并行清单取窗口里第一行依赖满足的，#147）, n 还要跑几行, held 清单暂停（"paused" 或 null）, dep 队首的依赖没满足时 [状态, 原因]（worker 的 dep_block）, elsewhere 交给池之前在别的主机放行、还没收尾的那一格 [主机, 任务]（不算在 head、n 里；没有为 null，#80）, err 这台读不到清单文件时的一句（没有不带，head 为 null）}}`，
  算力池里登记着的每条清单（都跑完了的 head 为 null、n 为 0，#86）；`pool_t` 是这份 pool_heads 读池目录的时刻（epoch 秒，worker 每 60 秒读一次）。
  看板逐条合并池那个 worker_home 下各台的（`pool._heads`：没有 err 的优先，再取 `pool_t` 最晚的），哪一份里都没有的算还没有 worker 读到。
  `offers_kept` 只在这一轮读这台失败（`error`、`status_error`，没 `worker stop`）、沿用它上一轮的 offers 时出现：`{t 那份状态写下的时刻（本机时钟 epoch 秒）, offers}`，按 `STALE_S` 过期（`rebalance.keep_offers`）。
- 顶层 `rebalance` 是各项目的挪法建议 `{项目: …}`（`rebalance.suggest`，[workbench.md「挪法建议」](workbench.md#挪法建议)）：有建议的 `{key 告警 key（rebalance:<项目>:<目标>:<原主机>/<清单>/<第一行>，`rebalance.move_key`，#109）, target 挪到哪台, lane, before, local 是不是同机的挪法（lane 为 null 是多开一条车道，否则挪到同一台的那条清单，#110）, gpus 同机多开车道时登记要限的卡（offer 的 gpus）, jobs（offers 的前几项，至多 20 行）,
  from 原来整批何时跑完（推不出为 null）, to 挪后（推不出为 null）, idle 是不是接收方空着、按行比的（这时另带 there 这几行在原处最早何时跑完（说不出为 null）、done 挪过去后何时都跑完，#62）, cut 其中被借用约定挡住的清单 ["主机|目录|清单"]（这句写进 borrow_cut，不另报）, room 有吃不满的主机时 {hosts {主机: 还能开几条}, busy {主机: 吃满程度 %}, lanes 合计, rows 本项目还有几行排队}（句首另写一句；loose 为真的是只因吃不满才收下的，与按行比的一样 key 是 `rebalance:<项目>:room:<主机,…>`，#145）, text 那一句（句尾“按 HH:MM 的推演”）}`；
  只有原因的 `{why [每台一句：“主机 上本项目 X 后没活，接不了 任务：原因”，整批推不出时“主机 接不了 任务：原因”], from}`；有主机有空、只因挪来的那一行没有估时推演算不出的，
  另带 `key`（`rebalance:<项目>:est`）、`est: true` 与 `text`（让会话写 est_min 或 fit --list，#43）；都没有的项目不列。offers 里已不在原处的行这一轮截掉（[workbench.md「挪法建议」](workbench.md#挪法建议)）。
- 顶层 `lan`（`lan.check` 的结果，总是有）、`wake`（给 `labmon wait` 的全局消息 `[{key, msg, projects, host}]`；作废与被挤掉的 `void:` 另带 `job: [项目, 任务]`（`overview._voided`，`labmon wait --job` 按它认等的任务被作废，#135）；页面上的改动 `records.told` 给的 key 是 `page:<what>:<项目>:<毫秒>`、不带 `host`，#121；借来的主机空着半小时以上的 `borrow_idle:<主机>:<起点>`、不带 `host`，#43）、`sched`（调度卡；`use_sum` 是各项目用量（`share.use`）之和；`projects[]` 的 `person` 是项目算在谁名下、`pk` 是 `sched.json` 里的人员键，看板上没有的项目两者为 null，`puse` 是同一个 `pk` 名下各项目的用量合计（取完整的 `share.use`，含已归档的项目；与 worker 的 `person_use` 同一口径），普通用户看到的别人项目 `person` 为 null，#23）、`occ`（各卡被谁占着）、`fleet`、`disk`（共享盘，`disk.update`：其中 `ours` 是自己目录下的各单位 `dirs[]`（`u`、`name`、`b`、`t`、`p` 项目、`d24`、`d7`、`n`、`m`、`top`）与按项目的汇总 `projects{项目: {roots, b, d24, d7, pending, burst, gone, jobs}}`，项目快照的 `storage` 由它来，#53）、`projects`（各项目看板的摘要）。
  `sched.projects[]` 的 `focus`、`paused` 是 null 或 `{until（epoch 秒，null 为无期限）, hosts}`：`hosts` 为 null 是项目级的（主机清单 `[sched.focus]`、`[sched.pause]`，所有主机），否则是只在这几台单独下的（`--host`），`until` 取最晚的（#96）。项目级的暂停另带 `by`：谁下的（主机清单 `[sched.pause_by]`，`config.pause_by`；没记的为 null，当管理员下的，#16），只在几台单独下的暂停的 `by` 是 `{主机: 谁下的}`（不算 `once`，没记的为 null，#27）；普通用户看到的快照只留他自己项目的 `by`。
  `hosts[].status.overrides[]` 里项目级的暂停、优先带 `global: true`（worker 从 `sched.json` 接上的，每台都有一份），`once: true` 的是不带 `--host` 的 `pause --now` 给各台下的“只停一次”：停完照样挡着，全局暂停送到就删（最多 30 分钟）。经看板带 `--host` 下的暂停、优先另带 `by`：谁下的（看板账号名；没有的当管理员下的，#27），普通用户看到的快照里别人项目的指令去掉 `by`。
- 顶层 `pool` 是算力池这一轮（`pool.tick`，#25；不持有服务租约的那台为空）：`{lists {"目录|清单": {project, root, jobs, host 此刻登记在哪（pool 是池里待放）, since 从何时起等（epoch 秒）, why 为什么还没放（放过去了的没有）, done 清单里的行都跑完了（#86：不算在等，只在为真时带）, nofit 这一轮问到了 fit、借着的都放不下，也挑不出能借的入池服务器（#130：`pool_wait:` 据此另报一次，只在为真时带）}}, gone [这一轮换了容器、直接还的主机]}`；
  项目车道的等待原因与 `pool_wait:` 告警取它。
- 顶层 `regs` 是登记的全局视图（`overview._regs`，项目「管理」抽屉用，[workbench.md「项目的管理抽屉」](workbench.md#项目的管理抽屉)）：各主机目录下登记的清单，每台一项
  `{host, label, state active|pool|returned|unlisted（在用、算力池里待放的（host 为 pool，#24）、主机清单里已归还、主机清单里已没有）, returned 归还时刻（epoch 秒，没有为 null）, home 所在 worker_home, t 读到的时刻（在用的是那份状态的 t）,
  stale 这一轮没读到、沿用上一轮的, queues [{root, jobs, project, stop, gpus}] 或 null（在用主机这轮既没状态、也没读到文件，上一轮也没有）}`；
  算力池放过去的登记（`worker add --pool`，登记文件与状态 `queues[]` 里的 `pool`，#79）另带 `pool: true`，别的不带这个键（`overview._pool_mark`）；
  在用主机那项另带 `pool`（`platform_pool`）与 `prev`（上一台：同一个池里最近归还的自用主机的 id，`fleetboot.last_mine`，没有为 null；接上卡排序用，#95）；
  在用主机的 `queues` 取状态里的，另带 `empty` 与 `added_t`（登记时刻，epoch 秒，那台容器的时钟）；其余取登记文件，另带 `added`（登记时刻的文字，那台容器的时钟）、`added_t`（登记文件的修改时刻，epoch 秒，共享盘服务器的时钟；读不到为 null）与 `key`（登记键，文件名去掉 `.json`）。在用的在前（照主机清单顺序），再是算力池、已归还的（归还晚的在前）、主机清单里没有的。
- `projects[]` 是各项目看板的摘要（`serve.Overview.proj_info` 给什么带什么，只去掉写 `eta.json` 用的 `running`）：`id`、`title`、`alerts`、`alert_msgs`、`generated`、`no_work_since`、`markers`、`quiet`（项目快照的，见上）、`groups`（计划项的进度：`[{id, label, color, auto 自动归出来的, total, done, failed, running, queued}]`，取项目快照的）、`ungrouped`（没归进任何组的任务数，占位任务不算）；`lanes[]` 是 `{host, root, queue, label, unfinished, run 在跑几个, returned}`（项目探针没读到的车道 `unfinished`、`run` 为 null）；
  `cfg` 是项目配置的摘要（`serve.Overview.cfg_info`）：`{path 看板上的配置（登记目录里的 monitor.toml）, out_dir 输出目录（登记目录的 out/）, title, interval, interval_fixed（服务启动参数定了采集间隔）, refresh, hide_done_hours, groups [{id, label, color}], auto_groups,
  markers [{date 原文, label, ts load 解析好的时刻（页面按它填日期框）}], alerts {gpu_idle_util, gpu_idle_min, stale_min, log_silent_min}, spec, stale}`（管理抽屉的「项目配置」节按它画控件，#122、#133）；
  `stale` 是数据过期的实际门槛（`config.stale_limit`，#141）：`{s 按已保存的配置算好的秒数, runs 至少等几次采集, pad 再加的余量（秒）, iv 门槛用的采集间隔（monitor.toml 的；服务用 --interval 定死时与 interval 不同）}`；
  `spec` 是能改的项的说法与范围（`config.cfg_spec`）：`{键: {name, unit, lo, hi, int, dft 缺省}}`，另有 `title.max`、`label: {name, max}`（计划项与里程碑名字的最多字数）。
  `POST /project/config` 出错时回 409 `{error, field}`，`field` 是出错的那一项（顶层键、告警键、`groups`、`markers`，说不出的不带）。
  `archive_block` 是能不能归档（`projects.archive_block`）：null 能，否则一句原因（管理抽屉照它置灰、写原因）。
- 顶层 `archived[]` 是已归档的项目（状态目录 `overview/projects.json`，新的在前，[workbench.md「项目的归档、恢复与永久删除」](workbench.md#项目的归档恢复与永久删除)）：
  `{id, title, t 归档时刻（epoch 秒）, revision 本次归档标识（旧条目没有）, by 谁做的（「看板页面上」「用命令行」，#121 之前归档的没有）, regs 归档时移除的登记 [{host, root, jobs}], skipped 没动的说明（没有能代办的在用主机、登记没读到）}`。
  `fleet.holders[]` 是按人的近 30 天统计（`frac`、`h` 里主人借给别人的时段是 `lent`；`borrowed [{id, from, h}]`、`lent [{id, to, h}]`），`fleet.mine[]` 与 `servers[]` 字段同名（`strip`、`s7`、`s30`、`last`、`error`、`down`、`since`、`started`、`public`、`guard`（`status: "worker"`），`sample` 与探针的同形，见 `fleet.from_worker`；另有平台操作口径的 `state`、`work`；
  `lw` 同主机项的：组内服务器取探针的根盘 `sample.root` 与扫出的路径（`fleet.server_lw`），自用主机取主机项的，关机的为 null，[fleet.md「本地盘」](fleet.md#本地盘)），`servers[].lend` 是入池 `{on, hours, open 此刻在不在时段里, say}`（`pool.lend_view`，#24），`servers[].borrow.by` 是借的人（[fleet.md「使用记录」](fleet.md#使用记录)）。
- 顶层 `hostcheck` 是看板服务所在机的体检（`hostcheck.view`，#166，[workbench.md「看板服务所在机的体检」](workbench.md#看板服务所在机的体检)），不是 WSL 时为 null：
  `{t 体检时刻（null 是还没体检过）, name 计算机名, role（primary 主 / standby 备用，取服务租约 lease.view；没跑过租约 null）, boot 开机时刻, reboot 待重启从何时起（不待重启 null）, pause_until 暂停更新到期（没暂停 null）,
  au {manual 是不是通知后手动装, text}, updates {list [{id, title, kb, sec 安全更新, driver, since 第一次看到}]（读不出 null）, src task|search, at 结果是何时的, checked, err},
  win_mem {free, total, since 低于门槛从何时起}, wsl_mem {avail, total, since}（MiB）, oom {n 近 24 小时几次, last}, rss 服务进程 MiB, tunnel {state active|down|none|unknown, since, unit},
  err {win?, wsl?}, warn {key: 提醒原文}, lim 门槛（`hostcheck.LIM`）, day [起, 止) 点, tick, upd_every（秒）, maint 计划维护到何时（`maint.until`，不在维护 null，#165）}`。时刻都是 epoch 秒。
- 顶层 `server` 是服务租约（`lease.view`，#163，[deploy.md「主与备用（服务租约）」](deploy.md#主与备用服务租约)），没跑过租约为 null：
  `{role primary|standby, id 这台看板服务, since 当前角色从何时起, holder 别人持有时是谁, renew 那份租约最后续约, until 最晚何时这台能接管, bad 租约文件写坏了的原因, home 哪个 worker_home, say 备用时页面横幅那句}`（时刻换成本机时钟，epoch 秒）。
  备用时不写快照：`/` 与 `/overview.json` 现填此刻的 `server`（`serve.live_snapshot`）。
- 下面的 `POST` 都要带 `Content-Type: application/json`（没请求体的也带，请求体写 `{}`），否则 415；带了 `Sec-Fetch-Site` 的只认 `same-origin`、`none`，别的 403；
  所有请求的 `Host` 不认的 403（[workbench.md「防跨站请求」](workbench.md#防跨站请求)，#158）。都要登录，各路由要的权限见 [api.md](api.md#路由要的权限)（#160）。
- `POST /collect` 与 `POST /p/<id>/collect`：先读 JSON 对象（也可发空请求体），格式不对回 400。立即采集返回新快照；离上一轮不到 10 秒的不采，返回现有快照并带 `"cached": true`。
- `POST /project/archive|restore`（`project` 作用域）：`{"project"}`；归档、恢复一个项目（`projects.archive`、`restore`），做完当场对齐看板服务里的项目；成功 `{"msg"}`，不能做、没做完 409 `{"error"}`，归档的项目不在服务里 404。
  恢复给那个项目的实验会话一条消息（`records.tell`）。
- `POST /project/delete`（管理员、`admin` 作用域）：`{"project", "revision"}`。确认值取自当前归档条目，旧条目用 `t` 的字符串。成功返回 `{"msg"}`，有活动依赖或归档批次变化返回 409。
- `POST /project/config`（`project` 作用域，#122）：`{"project", "set": {title?, interval?, refresh?, hide_done_hours?, auto_groups?, alerts?: {gpu_idle_util?, gpu_idle_min?, stale_min?, log_silent_min?}, groups?: [{id, label, color?, auto?}]（整组、按新顺序，写明的 id 要正好是现有的；带 `auto: true` 与 `color` 的是自动归出的组，写成新的 `[[groups]]`：`id`、`prefix` 都是它的前缀，#150）, markers?: [{date, label}]（整组换掉）}}`。
  `projects.set_config` 写回看板上的 monitor.toml（登记目录里那份，与推在同一把锁里；`config.set_project` 只动相关行，按 `config.load` 校验后原子写入），当场重读这个项目的配置；改了的记一条 `config` 记录（带 `project`）、给实验会话一条消息（`records.tell` 的 `config`）。
  成功 `{"msg"}`（什么都没变是“没有改动”，不写不发），取值不对、校验不过 409 `{"error"}`，项目不在服务里 404，没给 `set` 400。
- `GET /api/project/<id>/config`（`read`）、`PUT /api/project/<id>/config`（`project`，带 `If-Match`）：取、推看板上的项目配置（#161），见 [api.md「项目配置」](api.md#项目配置)。
- `POST /reg/move|rm`（`project` 作用域，本机不在内网 409，#95，#121 按主机成批）：move `{"to", "items": [{"host", "root", "jobs", "project", "stop"}]}` 逐条在 `to` 上 `worker add`（不带 `--gpus`）；
  rm `{"host", "home", "project", "items": [{"root", "jobs"}]}` 逐条 `worker rm`（已不在用的主机经 `workerctl.via` 代办，`home` 给主机清单里已没有的那台的 worker_home），有一条在这台上有任务在跑就整批拒绝。
  每条记一条 `config` 记录，做成的按项目给实验会话一条消息（`records.tell`）；成功 `{"msg"}`，失败 409 `{"error"}`（中途失败的带已做了几条）。
- `POST /sched/weight|focus|unfocus|pause|resume`（`project` 作用域，`weight`、`focus`、`unfocus` 只给管理员，#16）：都只改主机清单的 `[sched]`（权重、`[sched.focus]`、`[sched.pause]`；暂停同时记谁下的 `[sched.pause_by]`），不连服务器；成功 `{"msg"}`，失败 409 `{"error"}`。
  普通用户只能加、改、撤自己下的暂停：已有管理员（或没记是谁）下的、还没到期的暂停时 403（`users.need_pause`）。
  暂停、恢复给那个项目的实验会话一条消息（`records.tell`；权重、优先不给）。

状态目录里与它相关的文件：

| 文件 | 内容 |
|---|---|
| `overview/usage/<主机>.jsonl` | worker 用量记录的尾巴（30 天，按 开始\|任务\|目录 去重）：`{t 结束, s 开始（都是 epoch 秒，取 worker 行的 t1、t0）, n 任务名, p 项目, root, queue, g 用的卡 UUID, gi 记下时的卡序号（换容器后 UUID 全变了还能按序号画）, st done\|failed\|stop, wait 放行前等了几分钟, u 所用卡的平均利用率（worker 行的 gpu_util；11-17 前的行没有）}`；时间线的已跑占用、调度卡的平均等待、项目的平均利用率（`gpu_mine`）取它 |
| `overview/series/<主机>.jsonl` | 主机曲线（7 天，每分钟一行）：`{t, mem, lim, cpu, thr, nfs, busy, gpu [[已用, 总量, 此刻利用率, 吃满程度 m10]]}`；`thr`、`nfs`、`busy` 取状态的 `info.press`（11-08 前的行没有）；卡的第 4 项是 `gpu_busy` 的 `m10`（#51，11-17 前的行没有，主机页的利用率小曲线与 24 小时平均只用有它的行） |
| `overview/health.json` | 各主机的连通状态，只留 `fails`、`down`、`since`、`warned`、`started`、`miss` 这几个键（`HEALTH_KEYS`），别的键不再写 |
| `overview/events.jsonl` | 记录（一年）：`{t, kind, msg, host?, hosts?, who?, project?, target?, detail?}`；`relaunch`（自动拉起 worker）事件的 `detail` 是命令输出；`container_new`（换了新容器）带 `started`（容器启动时刻，epoch 秒），项目页的重启标记只画带它的 |
| `overview/hostlog/<主机>.jsonl` | worker 事件续存（30 天，任务起止的不存） |
| `overview/projects.json` | 已归档的项目 `{"archived": {id: {id, title, t, revision, by, regs, skipped}}}`（`projects.archive` 写、`restore` 和 `delete` 删；#161 之前归档的条目还带着 `config` 配置文件路径，不再读）；看板服务按它跳过与摘下项目，全局快照的 `archived[]` 取它 |
| `overview/hostcheck.json` | 看板服务所在机的体检状态（`hostcheck.tick` 写，#166）：上一轮读到的 `win`、`wsl`，`since`（各项从何时起）、`upd`（待装更新与各条第一次看到的 `seen`）、`oom`（计数与近 24 小时的增量）、`warn`、`sent`（各 key 推过的本地日期）、`err` |
| `overview/maint.json` | 计划维护（`labmon server maint`，#165）：`{until 到何时（epoch 秒；提前结束写 null）, by 谁开的, t 何时开的}`，到期自然失效；`/healthz` 的 `maint_until`、体检与全局快照的 `hostcheck.maint` 经 `maint.until` 读它 |
| `overview/borrow.json` | 借用的提醒与看板的记忆（`borrow.check` 写，字段见 `labmon/borrow.py` 头部）；算力池借的那台另有 `pool` 子项（#25，`pool._mem` 写，再借时保留）：`{started 借来时的容器启动时刻, since 借来的时刻, busy 最近一次有池任务在跑, rest 休息到几点, rest_why, fail 最近一次借不成, fail_day 那天通知过管理员, nofit [[卡数, 单卡显存, 到期]], want 为哪个形状借的 {ngpu, vram}}` |
| `overview/tell.jsonl` | 页面上的改动给实验会话的消息（7 天，`records.tell` 写）：`{t, what（move、rm、restore、pause、resume、config、borrow（改借用约定，#127）；看板升级时的 upgrade）, project, msg}`（算力池放、搬、回池只记事件、不写这里，#88）；`records.told` 每轮并进全局快照的 `wake`（#121） |

状态根目录的 `server.json`（`lease.py`，#163，不进备份）：`{id 这台看板服务（机器名-随机串）, primary 本机标记（读不到租约时照它）, role, since, t, homes {worker_home: {holder, renew, ttl, now 共享盘此刻的时刻, at 本机读到的时刻, bad}}}`。
每个 `worker_home` 下的 `labmon_server.json` 是服务租约本身：`{holder 持有的看板服务 id（释放后为空串）, renew 最后续约（共享盘时钟的 epoch 秒）, ttl 秒}`，旁边 `labmon_server.json.lock` 是互斥用的锁（它的修改时刻就是读共享盘时钟用的）；worker 不读它们。

看板账号在状态根目录的 `users.json`（不在 `overview/` 下，权限 600；只有哈希，字段见 `labmon/users.py` 头部注释），账号改动记进 `events.jsonl` 的 `account` 类（「配置改动」）。
项目摘要的 `owner` 是项目主人的账号名。普通用户的全局快照另有 `access: {own, hosts}`，用于页面刷新项目与主机权限。普通用户经 `/overview.json` 只拿到自己项目的摘要及可见主机的数据，并去掉 `fleet`、共享盘按人的部分、`notices`、`wake` 与 `hostcheck`（`users.view`，[api.md「普通用户看到的」](api.md#普通用户看到的)）。

worker 在共享盘上本机状态目录 `hosts/<主机>/press.jsonl` 每 5 分钟记一行主机压力：`{t, ms, ops, mib, busy, await, thr}`（共享盘每次读写 ms、每秒次数、MiB/s，本地盘忙 %、每次 ms，CPU 被限流 %；
这 5 分钟没有读写时 `ms` 为 null，计数归零的那项为 null），留 14 天；`io_base` 按它算共享盘慢的基线。看板不读它。

项目看板不再写 `out_dir/.labmon/durations.jsonl`（11-07 起）：失败的尝试取工作台的用量记录，已落盘的旧文件不再读，可以删。

自用的远端 SSH 主机 `[[hosts]]` 可用 `users = ["账号名"]` 保存普通用户的主机授权；缺省为空。`[server].ca_cert` 可选，指向随命令行安装脚本附带的 CA 公钥证书。

个人推送配置在看板服务那台机器的 `~/.config/labmon/notify.d/<账号>.toml`（600，账号名小写，字段与 `notify.toml` 一致），由「我的账号」保存和试推，不进冷备。

## 项目登记（#161）

看板上的项目登记在状态根的 `projects/` 下（`labmon/projects.py`；行为见 [workbench.md「项目登记与配置」](workbench.md#项目登记与配置)）：

| 文件 | 内容 |
|---|---|
| `projects/<id>/monitor.toml` | 项目配置，以这份为准；版本号是文本的 SHA-256 前 16 位（`projects.ver_of`） |
| `projects/<id>/files/<位置>` | done_when 指的文件：monitor.toml 里写的相对路径原样用，绝对路径、`~` 开头、带 `..` 的放 `_abs/<哈希>-<文件名>`（`config.done_key`） |
| `projects/<id>/out/` | 输出目录：`snapshot.json`、`index.html`、`.labmon/`（`state.json`、`history.jsonl`、`reg_lanes.json`、`wait_state.json` 等） |
| `projects/<id>/meta.json` | `{since 登记时刻, owner 项目主人, by 怎么登记的}`。第一次经接口推时取推送的账号。管理员可改主人；为空、账号不存在时由管理员管理。 |
| `projects/registry.lock` | 登记目录读改写的跨进程锁（推与页面上改共用） |

项目目录里另有 `.labmon/<配置文件名>.sync.json`（`projsync.mark` 写；同一目录里可以有几个项目的配置，各一个文件）：`{id, ver 上次同步时的版本, t}`；`.labmon/.gitignore` 只写 `*`；`pull` 时本地没同步的改动存成 `.labmon/<配置文件名>.mine`。


看板账号库的令牌条目可带 `client_commit`（最近报告的完整客户端提交号）和 `client_seen`（该报告的 epoch 秒，整数）。旧令牌缺少这两项时照常使用。见 [客户端接口](api.md)。

### 问题报告

在 `overview/events.jsonl` 中记为 `kind=report`，沿用事件保留期。字段包括唯一 `report_id`、登录账号 `reporter`、提交时间 `t`、现象 `msg`、证据 `detail`，以及可选的项目 `project`、主机 `host`、发生时刻 `occurred`。只有管理员可以读原文。总控本机在 `issue_watch-reports.json` 记已上报的编号。
