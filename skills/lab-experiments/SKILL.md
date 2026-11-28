---
name: lab-experiments
description: 本机所有科研项目在实验室 GPU 服务器上跑实验的统一做法：用 labmon 接入项目（先一次问清用户）、体检已接入的项目、写任务清单排队、暂停与插队、持续运行 labmon wait 盯看板和告警、等任务跑完、取回结果。凡是要在服务器上跑、续跑、暂停、终止实验，看有哪些卡与主机、GPU 与队列状态、共享盘用量，回答实验何时跑完，或处理 labmon 告警时先读它。不用于已有独立远端管理工具的项目和一般服务器管理。
---

# 实验室服务器实验

工具是 labmon，shell 里直接敲 `labmon`（`~/.config/labmon/client.toml` 指定看板地址）。
细则在同目录：清单写法 [lists.md](lists.md)，告警逐条怎么办 [alerts.md](alerts.md)，客户端进程等待差异 [sessions.md](sessions.md)，Google Colab 等临时算力与云盘存储 [colab.md](colab.md)。项目自己的 `AGENTS.md` 比这里优先。
原则是**快**：不加快照哈希、逐次确认等流程，只守下面会真出事的规则。

## 红线

- **先核对持久盘挂载**：共享盘（如 `/data`）按主机清单的 `data_root` 和 `shared` 使用；容器本地盘重启/续租时重置。项目文件放 `projects/<项目>/`；`envs/`、`datasets/`、`weights/` 共享且只读引用、不另存副本。文件放置规范见 [lists.md](lists.md)「文件放哪」。
- **不扫盘**：禁止跑 du、find 遍历共享盘（抢 NFS IO）。用量统一用 `labmon disk` 查看。
- **ssh 不轮询与打包传输**：严禁定时 ssh 查状态；短时间频繁新建连接会被限流约 10 分钟。多文件一条连接 tar：上传 `tar czf - 文件… | ssh … "tar xzf - -C 目标"`，取回 `ssh … 'cd 目录 && tar cf - 文件' | tar xf -`。经 ssh 起后台进程：先 `cd`，再单独执行 `setsid nohup … > /dev/null 2>&1 < /dev/null &`。
- **不自己 kill 正在跑的任务**：会记成失败；停法见「暂停与让路」。
- **不绕过 labmon 调度与 CPU 记账**：严禁因排队或资源紧张绕过 labmon 直接 ssh/后台起训练或计算进程，也严禁把耗核任务的清单写成 `cpus=0` 逃避 CPU 记账（`cpus=0` 仅限几乎不占核的轻量监督/等待脚本）；带 GPU 的主机会自动为 GPU 预留保底 CPU 核并将纯 CPU 任务自动封顶、降优先级，遇排队异常用 `labmon report` 上报。
- **GPU 保活不碰**：由 worker 与看板管理，严禁自行 `ka.sh start / stop` 或 kill 保活进程。
- **租期、续租、借卡、权重与优先由用户定**：续租由用户在看板操作并输入验证码；实验会话不碰平台、不自行借卡、不改 `[sched]`。
- **服务器重启即容器重启**：标了 `worker = "auto"` 的主机看板自动拉起 worker，中断任务重跑；未标的主机请用户 `labmon worker start <主机>`（需管理员令牌）。

## 这台机器第一次用 labmon

先查 `command -v labmon` 与 `labmon host list`（未配置退 2，未登录退 3），根据结果转告用户，会话不自行装证书或建账号：
- **无 labmon 命令**：请用户从看板「我的账号 → 命令行」下载并运行 `sh ~/Downloads/labmon-install.sh`（Python ≥ 3.11）。
- **连不上看板**：请用户参考 [README「安装」](../../README.md#安装) 与 [docs/deploy.md「看板服务的两个入口」](../../docs/deploy.md#看板服务的两个入口) 核对 `client.toml` 与证书。
- **未登录或令牌无效**：请用户执行 `labmon login`（或联系维护者获取账号）。

## 接入一个项目

**第 0 步：一次问清。** 先查 `labmon host list` 与 `labmon project init <主机>`（无项目名时只建规范目录并列出共享资源）。在**一条消息**中向用户确认：
1. 看板标题与 id：推荐标题为项目名，id 为小写短名。
2. 用哪些主机：推荐全部自用主机；是否借卡由用户定。
3. 截止与里程碑：换算为北京时间（AoE 换算：北京时间 = AoE + 20 小时）。
4. 本机取回路径：推荐项目内按日期新建目录。
5. 调度权重：推荐 1。
6. 共用环境：推荐 `envs/` 下最匹配的环境，均不匹配才在 `envs/` 下新建规范环境。
*显存、内存、时长、依赖、进度行、cpus、tag 等会话自行查定，不问用户。*

**接入步骤**：
1. `labmon project init <主机> <项目>` 创建目录结构。
2. 项目根目录建 `monitor.toml`（参考 `examples/monitor.toml`）：填写 `title`、`id` 与 `[[markers]]` 截止（北京时间）。**不写 `[[queues]]`**（车道由 `worker add` 自动生成）与 `out_dir`。
3. `labmon check -c monitor.toml` 校验，`labmon project push -c monitor.toml` 推送登记。项目页 `/p/<id>/` 供用户查看；会话统一通过命令查数据（[docs/api.md](../../docs/api.md)）。
4. **同步配置**：以看板登记版本为准。`labmon wait` 每轮自动双向同步；若遇 `sync_conflict:`，执行 `labmon project pull -c monitor.toml` 拉回后重新应用本地修改，wait 下一轮自动推送。

## 已接入项目体检

接手老项目时执行体检并以 `labmon check` 验证：
- 删已登记在 worker 里的 `[[queues]]` 与通配车道（趁空闲删，避免重置速度曲线），仅保留需特定 `label` 的车道。
- 删 `monitor.toml` 里过时的主机检查、旧保活段与已归还主机的 `[[hosts]]`。
- 清理项目 `AGENTS.md` 中过时的 `gpu_queue.sh`、轮询 ssh、自管保活等废弃描述，统一指引到本 skill。
- 确认缓存写入规范：数据集与结果放共享盘，可重算缓存写 `$LABMON_CACHE`。
- 确认截止在 `[[markers]]` 且为北京时间；确认各任务打规范进度行。

## 写清单要点

细则与命令见 [lists.md](lists.md)：
- 进度行：脚本必须输出 `PROGRESS k/N`（只增不减，flush 到 stdout）。
- 缓存：可重算缓存放 `$LABMON_CACHE` 并用 `needs=` 声明；中间产物及时清理。
- 长期清单：每台按资源类建立固定的长期清单，新任务追加到末尾，整体替换清单文件。
- 多机与并行：同一台上多任务并行在清单声明 `# labmon: par=N`，不拆清单；跨主机或加车道先跑 `labmon worker fit <主机> <目录> <清单> --list --project <id>` 评估分配；多机同步写 `gang=组名:人数`。
- 依赖：前后关系写 `after=` / `afterany=`，严禁在 GPU 命令里 sleep 轮询。
- OOM 与旧峰值重置：worker 自动抬高估计，不改名、不换 tag、不拆清单；若代码修复内存/显存泄漏大幅瘦身后仍被旧历史尖峰或 OOM 下限卡住队首，用 `labmon worker reset-hist <主机|pool|all> --project <id> [--tag <tag>] [--name <任务名或前缀>]` 显式写入时间戳水印（不删 `usage.jsonl`，不影响公平份额与时长）。

## 跑和盯

1. 同步代码至服务器后，登记清单：`labmon worker add <主机> <目录> <清单> --project <id>`。
2. 按 [sessions.md](sessions.md) 在会话后台挂起 `labmon wait -c monitor.toml`（等指定任务加 `--job <任务名>`）。
   - **严禁管道过滤**：不得写 `labmon wait ... 2>&1 | grep ...`，避免掩盖退出码或产生 SIGPIPE。
   - 收到退出信号后，按 [sessions.md](sessions.md) 分辨退出码：退出 1 为一般告警，**先重挂 wait，再逐条处理** [alerts.md](alerts.md) 中的告警；退出 0/4 为所等任务完成；退出 3 为配置或会话冲突；退出 5 为客户端过期（执行 `labmon update`）。
3. 取回结果：实验完成后按约定的本机新目录 tar 回，不覆盖已有产物。

**状态与数据查看**：
- **综合状态**：`labmon status`（查主机显存、GPU 保活状态、容器 CPU 记账、车道进度与预计结束时间、挪法建议、告警）。
- **推演结束时间**：以 `labmon status` 和 `labmon worker status` 的系统推演为准，不自行拿 `est_min` 外推。
- **主机与卡**：`labmon host list` 查看在用、借用及共用目录主机；`labmon worker status <主机>` 查看具体队列与任务。
- **磁盘用量**：`labmon disk` 查看本项目共享盘、独立主机本地工作盘及可回收目录，以及各台临时缓存占用（查本人共享盘总占用用 `labmon disk --me`），不跑 du。清理自管缓存用 `labmon worker clean <主机> [--project <id>]`（默认 `--dry-run` 预览，加 `--yes` 才删，绝不触碰 `projects/` 产物）。
- **历史与事件**：`labmon records [--host <主机>]` 查看拉起、续租、失败及临时指令记录。

## 独立主机（外租 Linux / WSL2 / 本机 Mac）

独立主机（`standalone = true`，`labmon host list` 标「外部机」）不挂集群共享盘，各机独立使用本地工作根目录 `data_root`（缺省 `~/.local/share/labmon/work`）：

- **接入与密钥红线**：可用 `labmon host add <id> --ssh user@host` / `edit` / `test` / `key` / `rm` 协助用户管理外部主机；**只走零私钥上传路线**（看板生成专用 Ed25519 公钥与一行 `authorized_keys` 授权命令，由用户确认粘贴到目标机）。**严禁读取或上传用户本机 `~/.ssh/id_*` 个人私钥**。
- **防开发仓污染（硬规）**：先跑 `labmon project init <主机> <项目>` 在 `<data_root>/projects/<项目>/` 下建目录，**严禁直接对正在编辑的源码仓执行 `worker add`**（默认拦截 `<data_root>` 之外的清单目录）：
  - **本机 Mac（APFS / Apple Silicon MPS）**：在 `<data_root>/projects/<项目>/code` 用 `git worktree add --detach` 或 APFS 零拷贝 `cp -cR` 隔离运行副本；macOS worker 自动走轻量 POSIX 采集并**默认留 2 核给桌面**（避免把笔记本 CPU 吃死，可在主机配置用 `cpu_quota` 调整）；Apple Silicon 自动暴露单逻辑 MPS 卡（默认单卡互斥、统一内存保守估算，可经 `mps_vram_gb` 覆盖），放行时导出 **`CUDA_VISIBLE_DEVICES=""`** 与 **`LABMON_ACCEL=mps`**（纯 CPU 导出 `LABMON_ACCEL=cpu`，CUDA 导出 `LABMON_ACCEL=cuda`），脚本应优先按 `$LABMON_ACCEL` 选择设备（如 `mps` / `cuda` / `cpu`）；Intel Mac 无 GPU 时仅支持 `gpus=0` 纯 CPU 任务；
  - **大数据集与权重**：统一放 `<data_root>/{datasets,weights}`，项目目录内**只读软链接**引用，不重复拷贝；
  - **WSL2**：`data_root` 必须落在 Linux ext4（如 `~/.local/share/labmon/work`），禁止放 `/mnt/c` 等 Windows 9p 盘（IO 极慢且锁语义不完整；`labmon check` / `worker status` / `host test` 命中 `/mnt/[a-z]/` 会提示风险）；当 Windows 宿主机在用 GPU 时，worker 通过未归因显存差额（`unacct_vram`）识别外部活跃占卡并**默认整卡暂停放新任务**（等待原因写「外部（如宿主机）占卡暂停放行」），**属于正常让路、不是异常**，不杀在跑任务、不报异常也不唤醒 `labmon wait`，宿主机释放后下一轮自动恢复；若用户明确希望按剩余显存共享，可在主机设置开 `allow_shared_gpu = true`（`labmon host edit <id> --allow-shared-gpu`）；
  - **产物与日志**：实验输出统一落 `<data_root>/projects/<项目>/runs/` 与 `logs/`，不写回源码仓。
- **空间防爆与清理**：独立主机默认剩 `20 GiB` 预警、剩 `10 GiB` 触发 `disk_gate` 磁盘闸门（停止放新任务，不杀在跑任务，含 `+2 GiB` 滞回恢复）。查看占用与 14 天闲置可回收目录用 `labmon disk`；清自管缓存与已结束单格临时用 `labmon worker clean <主机> [--project <id>]`（默认预览，显式 `--yes` 才删）；`projects/` 下的旧产物由用户确认后手动删除。
- **跨机语义限制**：独立主机不支持跨主机 `after=` 依赖、`gang=` 多机成组、借用、算力池与跨盘「搬登记」；但看板会自动把同名项目的历史用量裁剪下发（`hist_shared.json`），新机首次即可复用估时与显存/内存估计。

### 暂停与让路

- 临时让路：`labmon pause <项目> [--until …] [--host …]`（`--now` 按登记的停法停下，重新排队不记失败；缺省停法等当前任务跑完，要马上停需先 `worker add … --stop term` 登记）。恢复用 `labmon resume <项目>`。
- 整条队列停：在状态目录建 `paused` 文件（写入原因），删掉即恢复。
- 任务间等待外部条件：在清单中使用规范命名的占位任务（见 [lists.md](lists.md)「占位任务」）。

## 借来的主机

借用及期限由用户在看板决定，会话不自行借卡（[docs/fleet.md「借用」](../../docs/fleet.md#借用)）。
- 借到后 wait 报 `borrow:<id>:<时刻>`。往借来主机登记清单为**搬迁操作**；若需两边同时跑，需建新清单文件登记。
- 约定到期前跑不完的任务不放行，到点不杀正在跑的任务、跑完自动归还。
- 遇 `borrow_due:` 停止追加任务；`borrow_cut:` 等告警处置见 [alerts.md](alerts.md)。
- 对方保活与看门狗由 worker 处理，严禁自行停启对方保活或加锁。

## 交给算力池

用户授权且为共有算力池成员时，统一经池入口提交：`labmon worker add pool <目录> <清单> --project <id>`（多池时用 `pool:<id>`；单池时 `pool` 自动指向该池），由看板按人公平份额即时落机与空闲窃取（[docs/fleet.md「算力池」](../../docs/fleet.md#算力池)）。
- 仅禁止 `--gpus` 与成组起跑 `gang=`（默认 `--stop boundary` 与占位任务 `hold_*` 均可入池）；代码与数据须放在该池共用的共享盘。
- 独占共有池（缺省 `exclusive = true`）禁止直接对池内单台主机执行 `worker add`；可通过 `labmon worker status pool[:<id>]`、`labmon worker fit pool[:<id>] <目录> <清单> --list --project <id>` 与 `labmon host list` 查看池状态。
- 交付后照常挂 `labmon wait`，池内自动落机、窃取与撤离不打扰会话；若全池放不下（`pool_wait:...:nofit`）或等待超 2 小时（`pool_wait:`），再按需转回自用主机。
- 撤回用 `labmon worker rm pool[:<id>] <目录> <清单>`；转回自用主机直接在自用主机 `worker add`。

## 临时算力（Colab 等无共享盘主机）

Google Colab 等无 SSH 入口、无实验室共享盘的短生命周期 GPU / TPU 实例，首选官方 `google-colab-cli` 无头工作流并走推模式监控上报（浏览器 Notebook 退为兜底），详细流程与目录规范见 [colab.md](colab.md)：
- **分流与查余额**：先核实验室队列，需分流时用 `colab usage` 查余量（若本人开启了「我的 Colab」可顺手推 `POST /api/ephemeral/colab`）；抢卡无货最多退避重试 2 次并按阶梯降级（如 A100 → L4/G4），并发实例用独立 `--config` + `-s` 隔离。
- **Google Drive 两层存储与防呆**：热数据与代码放 `/content/work/`，Drive 按常驻共享层（`datasets/`、`weights/`、`envs/`、`cache/`）与项目隔离层（`projects/<proj>/runs/<job>/`，原子 `os.replace` 回写）组织；严禁把 `.git/`、碎图解压目录、高频日志或令牌放进 Drive；`colab drivemount` 后必带 `--drive-root` 与 `--artifact-dir` 启动。
- **监控与防空烧**：从 `GET /api/client/ephemeral.py` 下载 `labmon_ephemeral.py`（自签名 CA 加 `--ca` / `LABMON_CA`），用 `labmon_ephemeral.py run --unassign` 包装训练并挂本地 `labmon wait --job`；任务结束或异常后务必复核 `colab stop` 与活跃实例归零。
- **断连与告警**：遇 `ephemeral_down:` 按提示中的 `artifact_dir` 在新实例 `--resume` 续跑；遇 `ephemeral_dup:` 立刻停掉重复实例；遇 `ephemeral_disk_low:` 定向清理 Drive 旧 `runs/`（严禁对 Drive 跑 `du`/`find`）。

## 结束一个项目

项目结束执行 `labmon project archive <id>` 归档（撤销登记与指令，停止采集，保留数据）。恢复用 `labmon project restore <id>`（[docs/workbench.md「项目的归档与恢复」](../../docs/workbench.md#项目的归档与恢复)）。

## 发现 labmon 的问题

遇到调度、推演、告警或命令异常，直接向看板提交报告：
```bash
labmon report "一句话现象" --host "主机 id" --at "11-13 14:30" --evidence "实际与预期、对实验的影响、日志片段"
```
项目缺省取当前目录 `monitor.toml`（亦可 `--project`）。脱敏后提交，不包含密码令牌；原因推测明确注明。提交后获得编号，留在队列中等待评估，不改动 labmon 代码，不通过改名或换 tag 绕开。
