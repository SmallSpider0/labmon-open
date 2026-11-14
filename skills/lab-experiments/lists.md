# 清单写法细则

[SKILL.md](SKILL.md)「写清单要点」的展开。`docs/…` 指 labmon 仓库 `/path/to/labmon` 下的文件，按「」里的节名找；规则全文在那里，这里只写实验会话要做的。

## 命令

```bash
labmon worker start 29                                   # 部署并拉起（已在跑则无缝替换，任务不受影响）
labmon worker add 29 /root/data1/alice/projects/<项目>/<目录> jobs/<名>.txt --project <id> [--stop term]   # --project 必写，是 monitor.toml 的 id
labmon worker status 29                                  # 登记的队列（清单跑空的标“空着”）、运行中、等待中（在等什么、排第几、预计开始）
labmon worker fit 29 <任务名>…                            # 放不放得下、开不开并行车道（见下）
labmon worker limit 29 heavy=2                           # 同 tag 同时运行数上限（整台主机共用；只想限本项目就用带项目名的 tag）
labmon worker rm 29 <目录> <清单> / labmon worker stop 29
```

- 队列一律用 labmon worker（每台主机一个），不再新建旧的 `gpu_queue.sh`。登记一次即可，之后**插队、追加就改清单**（整体替换：写临时文件再 `mv`），worker 每启动一个任务前重读。
- **一条清单只归一台**：同一条清单 `worker add` 到另一台，就是从原来那台搬走（原来那台手上在跑的跑完为止）。要两台同时跑，新建一份清单（同目录，`.env` 照抄但 `QUEUE_STATE`、`QUEUE_LOGS` 写这份自己的），或从原清单剪行过去，再 `worker add`（`docs/worker.md`「清单与状态」）。
- 失败或被终止的任务留 `<状态目录>/<名称>.failed` 并跳过，修好后删掉它即重跑。

## 文件放哪

`/root/data1` 是各台共享的 NFS，只有它持久；容器本地盘在续租（每 7 天）、平台重启时清空。写清单时就定好每类文件放哪：

| 类别 | 放哪 | 要求 |
|---|---|---|
| 结果、日志；重算很贵（约 30 分钟以上）或重算不了的 | 共享盘 `projects/<项目>/` | 控制体积 |
| 跨格要用、能重算的中间文件 | `$LABMON_CACHE`（项目的本地缓存） | 行尾 `needs=` 声明；命令里先 `[ -f 文件 ] \|\| 重新生成;` |
| 其中跨主机复用、重算约 10 分钟以上、几 GB 以内的 | 同上，另在 `projects/<项目>/cache/` 留主副本 | `needs=本地<主副本`：本地缺了 worker 先从共享盘拷 |
| 只在这一格里用的临时文件 | `$LABMON_SCRATCH`（单格临时目录） | 跑成功后 worker 删掉 |

两个目录由 worker 开跑时设进环境变量，`.env` 里也能用（如 `W=$LABMON_CACHE/w0`）。不要在 `/tmp` 下自建目录放跨格文件：worker 只认 `needs=`。
worker 只认命令里对同一路径的 `[ -f … ] ||` 算能自己补；脚本里“共享盘上已有记录就跳过”的补采在新主机上不会执行，这类记录要写到本地（`docs/worker.md`「本地目录与本地依赖」）。

## 清单行

- 每行 `名称|命令`，`#` 开头是注释；资源提示写在行尾 ` #` 之后（不然原样传给程序）：
  `x4_s20|{ [ -f $W/w0.pt ] || $PY cap.py --out $W/w0.pt; } && $PY e4.py --w0 $W/w0.pt   # est_min=216 vram=20 ram=30 tag=x4 needs=$W/w0.pt`
- 提示键：`gpus=`（缺省 1，0 不用卡）、`vram=` 每张卡 GiB、`ram=` GiB、`est_min=`、`tag=`、`exclusive=gpu|host`（开销评估、计时测量用）、`needs=`、`after=`/`afterany=`、`gang=组名:人数`（成组，见下）、`cpus=`、`io=heavy|light`（只记录）。全表 `docs/worker.md`「资源提示」「独占」。
- 同名 `.env` 设 `PY`、`QUEUE_STATE`、`QUEUE_LOGS` 与任务要的 `PYTHONPATH`（worker 清掉它）；两条清单的状态目录必须不同（`docs/worker.md`「清单与状态」）。

## 进度行（必须）

每个任务都打 `PROGRESS k/N`，写脚本时就加、登记前逐个确认：至少每个 epoch 或阶段一行，一个阶段十分钟以上的在阶段里每几分钟一行，打到 stdout 并 flush。
没有它看板只能按估时猜：超了估时看不出是慢还是卡住，预计结束、准入与推演都跟着不准（会报 `noprogress:`）。
多阶段脚本打**一个贯穿全程、只增不减**的计数（各阶段步数相加为 N，标签写阶段名，如 `PROGRESS 120/196 [审计]`），不要每阶段从 1 重数（`docs/worker.md`「进度行」）。

## 估计：会话自己定，不问用户

- `est_min` 按单独占一张卡时估，不为抢卡加量：worker 按同类实际耗时折算（项目页计划项下写着 实际/估时，偏得多就改写法）。估时写准了短任务才插得进空档，续租截止、约定归还前也按它判断跑不跑得完。
  预计时长只认去掉种子编号后同名的同类、不看 tag：名字各不相同的一组扫描要写 `est_min`，不写就是“时长不明”，推演排不出后面的（`docs/worker.md`「预计时长」）。
- `vram=`、`ram=`：新类型的任务第一次跑写上，跑过一次就按实测（同 tag 或去种子号同名的；同一清单里别的任务的历史不盖过提示值）。
  OOM 后 worker 自己抬高估计、封顶到整卡或整个容器，跑成功一次回到按实测：不用改 `vram=`、拆清单、改名或换 tag（`docs/worker.md`「准入」）。
- `cpus=`：放行按它记 CPU 账，核不够就等。只在没有同类历史、又确知要多核时写；跑过一次就有峰值历史，写多了白占别人的核。
  CPU 与内存按容器配额算，不按 `nproc`/`free`（29 上 `nproc` 是 144，配额只有 17）：线程数、DataLoader 进程数、`OMP_NUM_THREADS` 按开跑时导出的 `LABMON_CPUS` 设（worker 不替你设）。
  按 `LABMON_CPUS` 设并行数的任务**第一次要写 `cpus=`**：历史只会照着给它的核数长。
- 名字各不相同的同类任务写同一个 `tag=`，fit 与准入才认得出是同类。

## 排到哪台、开不开并行

- **先逐台查放不放得下**：把这批任务追加到某台已登记、占着卡、还有没跑完的行的那条清单末尾（fit 只认已登记清单里的行，各台的都找；排在后面不会马上放行），
  再对每台在用主机 `labmon worker fit <主机> <任务名>…`。只有大卡放得下的集中到那台，其余优先排到别的主机，让各台预计跑完的时刻相近。
  各台何时跑完看推演（项目页时间线、`worker status` 的预计时间，同机别的项目与公平份额都算在里面），不拿 `est_min` 心算加总。排得不匀时看板报 `rebalance:`（[alerts.md](alerts.md)）。
- **同一张卡上的串行写进一份清单**，按顺序排；不要一条任务一份清单再用占位任务串起来。
- **开并行车道前先 fit**：说“可以开车道并行”才把这几行挪进新清单（两份都整体替换）并 `worker add`；说显存、内存、CPU 不够或没有实测的，留在同机占着卡的那条清单末尾。
  卡利用率到 100% 不拦（不等于算满），句中写同类并行时约要多久。不要为了让 fit 找得到先 `worker add` 新清单：一登记就按准入放行，fit 拦不住了（`docs/worker.md`「准入」的开并行车道）。

## 依赖

A 跑完再跑 B，写在 B 的行尾：`after=A`（A 跑成功才放行）、`afterany=A`（跑完就行），几个用逗号分开，别的项目的写 `项目/任务名`；A 可以在别的清单、别的主机上。
例：`eval_s42|$PY eval.py --seed 42   # est_min=30 after=train_s42`；`plot|$PY plot.py   # gpus=0 est_min=5 after=eval_s42,eval_s43`。

- B 没满足时它那条清单停在这一行（不排位、不占卡）。被等的 A 要排在 B 前面或在别的清单，排在 B 后面就永远轮不到（报 `dep:` 成环）。
- 不用占位任务轮询别的任务的 `.done`；旧清单里还有这种占位任务的，命令里 `.failed` 也放行的写 `afterany=`，不是 `after=`。
- 不在 GPU 任务的命令里先等条件再跑（`bash wait.sh && $PY …`）：等的时候照样占卡、记份额。等的部分拆成占位任务或 `gpus=0` 的任务，GPU 任务写 `after=` 它（`docs/worker.md`「依赖」）。

## 成组（多机 DDP 一起开跑）

几个进程要一起开跑、一个没了其余的也跑不下去（多机 DDP 的各 rank）：每个成员各写一行，行尾写同一个 `gang=组名:人数`，worker 等它们都留好资源才一起放行，开跑一般相差几秒。
例：29 的清单里 `ddp_r0|$PY -m torch.distributed.run --node_rank 0 … # gpus=4 est_min=600 gang=ddp_a:2`，借来那台的另一条清单里 `ddp_r1|… --node_rank 1 … # gpus=4 est_min=600 gang=ddp_a:2`。

- 成员**放在不同的清单里**（同一条清单里一前一后就成环，报 `dep:`），互相不写 `after=`；整组前面要等的写在每个成员上。人数必写；同一个组名同时只能有这么多行没跑完，几组写几个组名。
- 成组的清单登记 `--stop term`：一个成员失败或被作废，其余的按登记的停法停下、都记失败（`.failed`，还没跟上开跑的也记）；`boundary` 停不下，只能等它自己跑完。
  停下重排的（暂停、被挤掉、容器重启）与开跑 3 分钟同伴还没跟上的，其余的跟着停下重排，`labmon wait` 收到“跟着成组的同伴停下”，不用管，下一次整组一起重来。
- labmon 只管一起放行，不传地址：rendezvous（`MASTER_ADDR`、共享盘上的 init 文件）由项目自己做，超时照样留着。续租、约定归还前跑不跑得完按组里最长的成员判断（`docs/worker.md`「成组」）。

## 占位任务（等 labmon 看不到的条件）

外部数据到位、人工确认这类条件：在清单里插一个占位任务，名字里按词含 `hold` 或 `pause`（`hold_x`、`pause-1` 算，`threshold` 不算），命令是等某个标记删掉后退出。
它不要卡、不独占，但这条车道后面的都等它。写上 `est_min`（大概停多久），后面的才有预计时间。
`pause --now` 不停它；要放掉就满足它等的条件，条件不会再来了也先满足条件让它退出、再从清单删掉这一行，不要 kill（会记失败）。等别的任务跑完不用它，写依赖（`docs/worker.md`「资源提示」占位任务一段）。

## 单元申请

自带监督进程的项目（如 示例项目）不用清单，每个单元启动前申请：命令前加
`python3 /root/data1/alice/labmon/labmon_worker.py slot --host 29 --project demo-b --name <单元名> --ram 30 --vram 4 [--tag heavy] [--est-min 20] -- <原命令>`（`docs/worker.md`「单元申请」）。
