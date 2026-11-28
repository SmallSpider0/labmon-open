# 临时算力（Google Colab 无头 CLI 与 Google Drive 规范）

[SKILL.md](SKILL.md)「临时算力」的展开指南。面向 Google Colab 等**无 SSH 入口、无实验室共享盘、最长连续运行约 24 小时且可能随时被回收**的临时 GPU / TPU 节点。

**职责分工（硬边界）**：
- **labmon 只做轻量监控与按用户隔离的个人额度展示**：接收 `labmon_ephemeral.py` 的心跳、画统一时间线、发 `labmon wait` 唤醒、展示本人开启的 CU 摘要。
- **绝不**把个人 Google OAuth 凭据交给看板、**绝不**让看板替用户抢卡、**绝不**把 labmon 当代码同步或文件传输服务。抢卡、挂载 Drive、环境与数据准备、断连续跑与停机收尾全部由实验会话按本指南完成。

---

## 1. 无头透明工作流（首选：`google-colab-cli`）

优先使用官方 `google-colab-cli`（`colab` 命令）在终端无头操作 Colab，**浏览器 Notebook 仅作兜底**。完整闭环按以下 6 步执行：

### 步骤 1：分流决策 + 查余额并顺手推个人摘要
1. 先看实验室卡况（`labmon status` / `labmon host list`）：仅当实验室 GPU 排队拥堵、需要 A100/L4/TPU 突发算力或短跑消融时才分流到 Colab；长周期多卡训练仍优先实验室主机。
2. 查个人 Colab 余额与活跃实例，并一键同步到看板（**仅本人可见**，绝不向看板上传 Google OAuth 凭据）：
   ```bash
   labmon colab sync           # 本机调用 colab usage 解析并推送到 POST /api/ephemeral/colab
   labmon colab sync --enable  # 同步并顺手开启总览「我的 Colab」个人摘要
   labmon colab sync --dry-run # 仅打印解析结果，不推送
   ```
   若本机未装或未登录 `google-colab-cli`，命令会返回非零并打印安装/登录指引（`colab login`）。也可在看板「我的账号 → 概览 → 我的 Colab」直接复制按本人项目生成的接入命令并查看在线实例。

### 步骤 2：抢卡退避与阶梯降级（防风控）
1. 按任务显存需求选目标规格，优先按性价比阶梯申请（例如 `A100` → `L4` / `G4` → `T4`，或 TPU `v6e-1`）。
2. **退避与降级规则**：
   - 目标规格无库存（quota / unavailable）时，**最多退避重试 2 次**（如间隔 30s、60s），切勿高频死循环刷 API 触发 Google 风控；
   - 2 次仍无货则按阶梯降级到下一档可用规格，或回退实验室队列；
   - 网络瞬断（TLS/连接重置）最多重试 1–2 次即报错交回决策。

### 步骤 3：多实例并发隔离（`--config` + `-s`）
同时开多台 Colab 跑不同 seed / 消融时，务必做到**三层隔离**：
1. **CLI 会话隔离**：每个并发实例使用独立的 `--config <dir>` 与独立会话标识 `-s <session>`（避免复用同一个本地配置状态互相顶掉连接）。
2. **看板与任务隔离**：不同实例分配不同 `--host-id`（如 `colab-l4-01`、`colab-l4-02`，不与 `hosts.toml` 重名，单项目最多 5 台同时在线）与不同 `--job`（如 `exp1_s0`、`exp1_s1`）。
3. **产物目录隔离**：每个任务独占 `--artifact-dir` 子目录（`projects/<proj>/runs/<job_id>/`）；若误在两台实例跑了同一 `--job`，看板会立即返回 `warnings: ["duplicate_job:<job_id>"]` 并触发 `ephemeral_dup:` 告警——**立刻停掉多余实例**。

### 步骤 4：挂载 Google Drive（`colab drivemount` 单次确认）
1. 新实例启动后执行 `colab drivemount`（首次在当前账号/会话下按提示完成一次浏览器授权确认；后续同会话内可直接挂载到 `/content/drive/MyDrive`）。
2. **禁止裸写未挂载路径**：启动训练前由 `labmon_ephemeral.py --drive-root /content/drive/MyDrive/labmon` 自动校验挂载点确为非根独立文件系统，未挂载直接退出码 2 拒绝启动，防止 checkpoint 悄悄写进 `/content` 容器盘在回收后丢失。

### 步骤 5：启动训练（自动套 `labmon_ephemeral.py run --unassign` + 本地 `labmon wait`）
1. 在远端实例用 Python 标准库获取零依赖客户端（Python 3.8+ 纯标准库；若公网入口使用自签名 CA，加 `--ca <ca.pem>` 或设 `LABMON_CA`）：
   ```bash
   python3 -c '
   import os, urllib.request
   req = urllib.request.Request(
       os.environ["LABMON_URL"].rstrip("/") + "/api/client/ephemeral.py",
       headers={"Authorization": "Bearer " + os.environ["LABMON_TOKEN"]},
   )
   with urllib.request.urlopen(req, timeout=15) as r, open("/content/labmon_ephemeral.py", "wb") as f:
       f.write(r.read())
   '
   ```
2. 按需把本次运行所需的最小代码包传到 `/content/work/src`（**不要把 git 仓库放进 Google Drive**），并把只读数据压缩包从 Drive 解压到热盘 `/content/work/data`。
3. 统一用 `labmon_ephemeral.py run --unassign` 包装训练命令：
   ```bash
   python3 /content/labmon_ephemeral.py run \
     --url "$LABMON_URL" \
     --project proj-a \
     --host-id colab-l4-01 \
     --job exp1_s0 \
     --est 90 \
     --drive-root /content/drive/MyDrive/labmon \
     --artifact-dir /content/drive/MyDrive/labmon/projects/proj-a/runs/exp1_s0 \
     --unassign \
     -- python3 /content/work/src/train.py \
          --data /content/work/data \
          --out /content/drive/MyDrive/labmon/projects/proj-a/runs/exp1_s0 \
          --resume
   ```
   - 自动上报 `started_at`（看板显示 `已跑 Xh / 24h`）、`accel`（兼容 GPU / TPU / 纯 CPU）、`PROGRESS k/N` 进度与 `os.statvfs` 磁盘余量；
   - 收到 `SIGTERM` 透传子进程并等待 `--grace`（默认 15s）回写最后一次 checkpoint；
   - `--unassign` 在子进程结束并上报终态心跳后自动尝试 `google.colab.runtime.unassign()`，防止异常退出后实例空转烧 CU。
4. 本地实验会话照常挂 `labmon wait --project proj-a --job exp1_s0` 等待完成或断连唤醒。

### 步骤 6：断连续跑、网页远程安全停机与强制 `colab stop` 收尾
1. **断连 / 被回收后续跑**：超过 180s 无心跳看板自动标 `down`、把残留 `running` 结转 `failed` 并触发 `ephemeral_down:`（含 `artifact_dir`）唤醒 `labmon wait`。开新实例、挂载 Drive、核对 `<artifact_dir>/ckpt.pt` 完整性后，用相同 `--job` / `--artifact-dir` 和 `--resume` 续跑即可。
2. **网页远程安全停机止损**：在看板主机详情页或「我的账号 → 概览 → 我的 Colab」点「停止实例」（或调用 `POST /api/ephemeral/<host_id>/stop`，仅本人有写权限的项目可用），看板记录 `want_stop` 并在下次心跳（≤30s）返回 `action: "stop"`。`labmon_ephemeral.py run` 收到后会向子进程发 `SIGTERM`、等 `--grace` 回写 checkpoint、上报终态并**自动尝试 `google.colab.runtime.unassign()`** 释放实例止损（即使未传 `--unassign`；Notebook `track()` 模式也可在循环里检查 `run.stop_requested` 提前保存并退出）。
3. **强制收尾**：实验跑完或提前中止后，**必须确认执行 `colab stop`（对应 `-s` / `--config`）** 并复核 `colab usage` 活跃数归零，避免 `--unassign` 在极端断网下未生效而持续空烧 CU；顺手执行 `labmon colab sync` 同步最新余额给看板。

---

## 2. Google Drive 共享盘目录规范（类比实验室 `/root/data1`）

Colab 本地盘 `/content` 读写快但回收即清零；Google Drive `/content/drive/MyDrive` 持久但小文件随机 I/O 慢、易触发 FUSE 缓存延迟。**严格区分两层存储**：

- **热盘（`/content/work/`，容器本地盘）**：代码工作目录、解压后的小文件数据集、中间缓存、高频训练日志（`/tmp/labmon_<job>.log`）。
- **冷盘（`/content/drive/MyDrive/labmon/`，Google Drive 准共享盘）**：按「常驻共享层 + 项目隔离层」组织：

```text
/content/drive/MyDrive/labmon/
├── datasets/                          # [常驻共享层] 只读压缩数据集（如 imagenet_sub.tar.gz），启动时解压到 /content/work/data/
├── weights/                           # [常驻共享层] 只读预训练权重（如 llama3-8b.safetensors）
├── envs/                              # [常驻共享层] 可复用的 wheel 离线包或轻量环境脚本（非解压后的海量 venv）
├── cache/                             # [常驻共享层] HuggingFace / Torch 大文件只读缓存（单文件为主）
└── projects/
    └── <project_id>/
        └── runs/
            └── <job_id>/              # [项目隔离层] 每个任务独占目录（上报为 --artifact-dir）
                ├── ckpt.pt            # 原子替换（先写 .tmp 再 os.replace）的最新 checkpoint
                ├── metrics.jsonl      # 追加指标日志
                └── DONE               # 最终完成标记
```

### 严禁放进 Google Drive 的内容
- **严禁放代码仓库（`.git/`）**：海量碎文件会拖垮 Drive FUSE 同步，且易产生冲突副本；代码按需打包/拉取到 `/content/work/src/`。
- **严禁放临时小文件、高频日志与 TensorBoard 碎事件文件**：写到 `/content/work/` 或 `/tmp/`，阶段性汇总再落盘 Drive。
- **严禁把海量碎图数据集直接解压在 Drive 里**：必须以单个 `.tar` / `.tar.gz` / `.parquet` / WebDataset 存在 `datasets/`，实例启动后解压到 `/content/work/data/` 读取。
- **严禁存放令牌、私钥、OAuth 凭据明文**：`LABMON_TOKEN` 只经环境变量或 Colab Secrets 注入，绝不落盘 Drive。
- **严禁对 Google Drive 跑 `du` / `find` 全盘扫描**：看板只用 `os.statvfs` 读总余量，剩余低于 1 GiB 会标红并触发 `ephemeral_disk_low:` 告警，收到后定向清理旧 `runs/` 目录。

---

## 3. 原子回写 checkpoint 模板

```python
import os, pathlib, torch

def save_ckpt_atomic(state, path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(state, tmp)
    os.replace(tmp, path)          # 同挂载点原子替换，避免断线留下半截坏文件
```

---

## 4. 兜底方案：浏览器 Notebook 接入

仅在 `google-colab-cli` 不可用或需要交互式调试时退回浏览器 Notebook：

1. 在 Colab 左侧钥匙图标（Secrets）添加 `LABMON_URL` 与 `LABMON_TOKEN`（project 令牌），打开 Notebook 访问开关；**绝不硬编码进单元格**。
2. 挂载 Drive 并下载客户端：
   ```python
   import os, urllib.request
   from google.colab import drive, userdata

   drive.mount("/content/drive", force_remount=False)
   os.environ["LABMON_URL"] = userdata.get("LABMON_URL")
   os.environ["LABMON_TOKEN"] = userdata.get("LABMON_TOKEN")

   req = urllib.request.Request(
       os.environ["LABMON_URL"].rstrip("/") + "/api/client/ephemeral.py",
       headers={"Authorization": "Bearer " + os.environ["LABMON_TOKEN"]},
   )
   with urllib.request.urlopen(req, timeout=15) as r, open("/content/labmon_ephemeral.py", "wb") as f:
       f.write(r.read())
   ```
3. 使用上下文管理器 `track()`（同样支持 `unassign=True`、`ca=...`）：
   ```python
   import sys; sys.path.insert(0, "/content")
   import labmon_ephemeral as LE

   with LE.track(
       project="proj-a",
       host_id="colab-t4-01",
       job="exp1_s0",
       est_min=90,
       drive_root="/content/drive/MyDrive/labmon",
       artifact_dir="/content/drive/MyDrive/labmon/projects/proj-a/runs/exp1_s0",
       unassign=True,
   ) as run:
       for step in range(1, 101):
           loss = train_one_step()
           run.progress(step, 100, line=f"loss={loss:.4f}")
   ```
