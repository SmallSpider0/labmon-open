# 清单写法细则

[SKILL.md](SKILL.md)「写清单要点」的展开说明。详细机制见 labmon 仓库 [docs/worker.md](../../docs/worker.md)。

## 命令

```bash
labmon worker start 29                                   # 部署并拉起 worker（已在跑则无缝替换；需管理员令牌，交由用户操作）
labmon worker add 29 /root/data1/alice/projects/<项目>/<目录> jobs/<名>.txt --project <id> [--stop term]   # --project 必填
labmon worker status 29                                  # 查看队列状态、运行中与排队任务详情
labmon worker fit 29 <目录> <清单> --list --project <id>   # 评估该清单在共用 worker 目录的各主机上可新开几条车道
labmon worker fit 29 <任务名>…                            # 单独评估具体任务在本主机是否放得下
labmon worker limit 29 heavy=2                           # 限制同 tag 任务并发上限（整机共用；项目独占 tag 需带项目名）
labmon worker rm 29 <目录> <清单> / labmon worker stop 29
labmon worker add pool <目录> <清单> --project <id>    # 提交给算力池；撤回用 worker rm pool <目录> <清单>
```

- 统一使用 labmon worker 管理队列，不自建旧式脚本。
- **改清单即插队与追加**：整体替换清单文件（写入临时文件后 `mv` 覆盖），worker 在每个任务启动前重读。
- **一条清单只归一台**：将已有清单 `worker add` 到新主机等于**从原主机搬走**。需多机并行时，创建新清单文件（同目录，复制 `.env` 并配置独立的 `QUEUE_STATE` 与 `QUEUE_LOGS`）后登记。
- 失败任务留 `<状态目录>/<名称>.failed` 并跳过，修复后删除该 `.failed` 文件即自动重跑。

## 文件放哪

NFS 共享盘（如 `/root/data1`）数据持久；容器本地盘在续租或平台重启时会被清空。

| 类别 | 放哪 | 要求 |
|---|---|---|
| 结果、日志；不可重算或重算代价高（>30分钟）的文件 | 共享盘 `projects/<项目>/` | 规范控制体积 |
| 跨任务使用的可重算中间文件 | `$LABMON_CACHE`（本地缓存） | 行尾声明 `needs=`；命令中编写 `[ -f 文件 ] \|\| 生成命令;` |
| 需跨机复用且重算代价中等（>10分钟、几GB以内）的文件 | `$LABMON_CACHE`，且在共享盘留主副本 | 声明 `needs=本地<主副本`（本地缺失时 worker 自动从共享盘拉取） |
| 单任务运行中使用的临时文件 | `$LABMON_SCRATCH`（单格临时目录） | 任务成功后 worker 自动清理 |

“共享盘各机可读”仅适用于代码、数据集与最终结果。可重算的缓存放本地；下游用完的中间产物在清单中编写收尾任务删除（行尾 `after=<下游任务>`），或通过 `labmon disk` 定期清理。

## 清单行

- 格式为 `名称|命令`，`#` 开头为注释；资源提示必须写在行尾 ` # ` 之后：
  `x4_s20|{ [ -f $W/w0.pt ] || $PY cap.py --out $W/w0.pt; } && $PY e4.py --w0 $W/w0.pt   # est_min=216 vram=20 ram=30 tag=x4 needs=$W/w0.pt`
- 提示键：
  - `gpus=`：显卡数（默认 1，`gpus=0` 不用卡；纯 CPU 任务推荐写 `gpus=0`，未写 `gpus=` 但显式写 `vram=0` 也会自动按不用卡处理）。
  - `vram=`：每张卡显存（GiB）。
  - `ram=`：内存（GiB）。
  - `est_min=`：单卡预估时长（分钟）。
  - `tag=`：任务类型标识。
  - `cpus=`：核数提示（优先于历史记录）。
  - `needs=`：本地缓存依赖路径（`needs=本地` 或 `needs=本地<主副本`）。
  - `after=` / `afterany=`：任务依赖关系（逗号分隔；跨清单/跨项目写 `项目/任务名`）。
  - `gang=组名:人数`：成组协同启动（用于多机 DDP）。
  - `exclusive=gpu|host`：独占模式。
  - `io=heavy|light`：IO 类型（仅记录）。
- 同名 `.env` 文件声明 `PY`、`QUEUE_STATE`、`QUEUE_LOGS` 与 `PYTHONPATH`（worker 启动会清空外层 PYTHONPATH）；不同清单的 `QUEUE_STATE` 必须隔离。

## 进度行（必须）

每个任务均须输出规范进度行 `PROGRESS k/N`（至少每 epoch 或阶段一行，flush 到 stdout）。多阶段任务采用一个贯穿全程、只增不减的累计进度（例如 `PROGRESS 120/196 [阶段二]`），不得在各阶段中从 1 重新计数。

## 估计：会话自行设定

- `est_min`：按独占单卡耗时评估，不要人为虚报或压低（worker 会参考同类任务实际耗时自动折算）。修改 `est_min` 直接在清单修改即可，无需更换任务名或 tag。
- `vram=` / `ram=`：新任务初次给出估值；已有运行记录后 worker 优先按实测放行。
- **OOM 自动抬高估计与重排**：发生 OOM 时 worker 自动抬高同类任务估计值封顶，但不自动重排该任务；确认非程序错误后删除相应 `.failed` 文件即可重排，成功后恢复实测值，无需改 `vram=` 或换 tag。
- `cpus=`：
  - 容器配额计算以 `labmon worker status <主机>` 的 CPU 配额与可用核为准，不看宿主机 `nproc`。
  - 多线程或自定义 DataLoader 的任务首次运行务必声明 `cpus=`（建议取配额 ÷ 计划并发车道数）。若未设置且无历史，worker 默认仅按 1 核记账。
  - 任务代码应读取环境变量 `$LABMON_CPUS` 来设置线程数和 worker 进程数；worker 默认会将 `OMP_NUM_THREADS` 等设为 `$LABMON_CPUS`。
  - 跑满 3 次实测峰值不到一半时 worker 会自动收敛记账，不必逐行手工修改。

## 排到哪台、开不开并行

- **建立长期清单**：每台主机按资源类型（GPU 槽、重显存槽、纯 CPU 槽）建立固定的长期清单文件（位于项目共享盘 `queues/` 下，工作目录固定在项目主目录），后续新任务均追加到相应清单末尾。跑空满 2 天的登记 worker 会自动休眠，追加新任务自动唤醒，无需重新 `worker add`；确定废弃的清单执行 `labmon worker rm` 摘除。
- **同机多任务并发（`# labmon: par=N`）**：同一份清单中互不依赖的任务，在清单头部添加 `# labmon: par=N`，worker 会在该主机自动并发执行至多 N 行，跑完一行补一行，无需拆分成多份清单。
- **跨机评估与分配（`fit --list`）**：
  1. 新批次任务先汇总在一份清单中，执行 `labmon worker fit <任一主机> <目录> <清单> --list --project <id>`。
  2. 系统按清单顺序模拟准入，输出各共用目录主机当前可新增的车道数及推荐分配。
  3. 对当前主机：调大清单的 `par=N`（`N` 取 fit 说能新增的车道数加上已在跑的行数）。对其他主机：分配到该主机空闲的长期清单中，或新建长期清单登记 `worker add`。
  4. 遇到借来新机、主机仅剩保活、或同机资源释放时，主动重跑 `fit --list` 扩展车道。
- **核对瓶颈**：卡吃不满且 fit 提示跑满分配核数时，在清单行调大 `cpus=` 加核；若提示容器被限流，则在代码或环境变量中减少线程与进程数。

## 依赖

- 在依赖任务行尾添加 `after=前置任务`（前置成功才放行）或 `afterany=前置任务`（前置跑完即放行）。前置可位于其他清单或主机。
- 被依赖任务必须排在依赖任务前面或不同清单中，不得在同一清单形成死锁。
- 禁止在 GPU 命令内部通过脚本 sleep 轮询前置条件（会导致无谓占卡）；应拆分为独立的依赖任务或纯 CPU（`gpus=0`）任务。

## 成组（多机 DDP 一起开跑）

- 多机分布式任务各 rank 分别在各自所在主机的清单中声明，行尾标注完全相同的 `gang=组名:人数`（例如 `gang=ddp_a:2`），worker 会在所有成员均留足资源时同步放行。
- 各成员必须分别放在**不同**的清单中；清单登记时必须指定 `--stop term`。
- rendezvous（如 `MASTER_ADDR` 与初始化文件）由项目脚本自行建立；若某成员失败则其余成员会被终止并均记 `.failed`（修好后需清理各成员 `.failed` 标记），而暂停或未跟上开跑则自动停下重排。

## 占位任务（等待外部条件）

- 等待人工确认或外部数据时，在清单中插入名称按词含 `hold` 或 `pause` 的占位任务（如 `hold_x`、`pause-1` 算，`holddata` 或 `threshold` 不算），行尾注明预估等待时长 `est_min=`。
- 占位任务不占用 GPU 与独占锁，但会按顺序阻挡本车道后续任务。恢复时满足退出条件让其正常退出即可，禁止直接 kill。

## 单元申请

由外部监督进程动态拉起的短任务，可直接通过 slot 命令向 worker 申请资源后执行：
`python3 /root/data1/alice/labmon/labmon_worker.py slot --host 29 --project demo-b --name <名> --ram 30 --vram 4 [--tag heavy] [--est-min 20] -- <命令>`（详见 [docs/worker.md](../../docs/worker.md#单元申请)）。
