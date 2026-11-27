# 临时算力（Google Colab 等无共享盘主机）

[SKILL.md](SKILL.md)「临时算力」的展开指南。面向 Google Colab、Kaggle 等**无 SSH 入口、无实验室共享盘、几小时可能被回收**的临时 GPU 节点。

**职责分工**：labmon 只负责接收临时实例的推模式监控心跳、统一时间线、`labmon wait` 唤醒与存储余量提醒；**绝不承担**代码同步、文件传输或远端队列编排。数据与 checkpoint 的持久化、多实例防撞与断点续跑由实验会话按本指南落实。

---

## 1. 两层存储分工与 Google Drive 目录布局

Colab 本地盘 `/content` 读写快但回收即清零；Google Drive `/content/drive/MyDrive` 持久共享但小文件随机 I/O 慢且容易触发 FUSE 缓存延迟。务必区分两层：

- **热盘（`/content/work/`）**：存放解压后的小文件数据集、中间临时文件、高频日志。
- **冷盘（`CLOUD_ROOT`，位于 Google Drive）**：存放只读压缩数据集、模型权重、每个任务独立的 checkpoint 与最终结果。

推荐统一目录布局：

```text
/content/drive/MyDrive/labmon/<project_id>/
├── datasets/        # 只读压缩包（如 train.tar.gz），启动时解压到 /content/work/data/
├── weights/         # 只读预训练权重
└── runs/
    └── <job_id>/    # 每个任务独占一个目录（上报为 --artifact-dir）
        ├── ckpt.pt          # 原子回写的最新 checkpoint
        ├── metrics.jsonl    # 指标日志
        └── DONE             # 最终完成标记
```

---

## 2. 启动防呆：挂载、本地解压、原子回写（10 行模板）

Colab 偶发 Drive 未挂载，若直接向 `/content/drive/MyDrive/...` 写入，会把 checkpoint 悄悄写进本地容器盘、实例回收后全丢。

1. **挂载并校验**（`labmon_ephemeral.py --drive-root` 也会在启动前自动校验其为真实挂载点且非 `/content` 同一文件系统，未挂载直接退出码 2 拒绝启动）：
   ```python
   from google.colab import drive
   drive.mount("/content/drive", force_remount=False)
   ```
2. **解压到热盘 + 训练 + 原子回写 checkpoint**：
   ```python
   import os, shutil, tarfile, pathlib

   CLOUD_ROOT = pathlib.Path("/content/drive/MyDrive/labmon/proj-a")
   WORK = pathlib.Path("/content/work")
   ART_DIR = CLOUD_ROOT / "runs" / "colab_s1"
   ART_DIR.mkdir(parents=True, exist_ok=True)
   if not (WORK / "data").exists():
       tarfile.open(CLOUD_ROOT / "datasets" / "data.tar.gz").extractall(WORK / "data")

   def save_ckpt_atomic(state, path: pathlib.Path):
       tmp = path.with_suffix(".tmp")
       torch.save(state, tmp)
       os.replace(tmp, path)          # 同挂载点原子替换，避免断线留下半截坏文件
   ```

---

## 3. 获取零依赖客户端并接入看板

零依赖客户端 `labmon_ephemeral.py` 仅使用 Python 3.8+ 标准库，在 Colab 单元中用标准库即可下载：

```python
import os, urllib.request
from google.colab import userdata

os.environ["LABMON_URL"] = userdata.get("LABMON_URL")       # 看板地址
os.environ["LABMON_TOKEN"] = userdata.get("LABMON_TOKEN")   # project 作用域令牌，绝不写进 Notebook 代码

req = urllib.request.Request(
    os.environ["LABMON_URL"].rstrip("/") + "/api/client/ephemeral.py",
    headers={"Authorization": "Bearer " + os.environ["LABMON_TOKEN"]},
)
with urllib.request.urlopen(req, timeout=15) as r, open("/content/labmon_ephemeral.py", "wb") as f:
    f.write(r.read())
```

### 方式 A：CLI 包装命令（推荐，自动捕获 `PROGRESS i/N` 与 `SIGTERM`）

```bash
python3 /content/labmon_ephemeral.py run \
  --project proj-a \
  --host-id colab-t4-01 \
  --label "Colab T4 #1" \
  --job colab_s1 \
  --est 90 \
  --drive-root /content/drive/MyDrive \
  --artifact-dir /content/drive/MyDrive/labmon/proj-a/runs/colab_s1 \
  -- python3 train.py --resume /content/drive/MyDrive/labmon/proj-a/runs/colab_s1/ckpt.pt
```

- 训练脚本照旧向 stdout 输出 `PROGRESS k/N`（flush），客户端会自动解析并上报，项目页自动计算速度与预计结束时间。
- 收到 `SIGTERM`（Colab 即将回收前）会透传给子进程，等待最多 `--grace`（默认 15s）让训练写完最后一次 checkpoint，再上报最终状态退出。

### 方式 B：Notebook 内 `track()` 上下文管理器

```python
import sys; sys.path.insert(0, "/content")
import labmon_ephemeral as LE

with LE.track(
    project="proj-a",
    host_id="colab-t4-01",
    job="colab_s1",
    est_min=90,
    drive_root="/content/drive/MyDrive",
    artifact_dir="/content/drive/MyDrive/labmon/proj-a/runs/colab_s1",
) as run:
    for step in range(1, 101):
        loss = train_one_step()
        run.progress(step, 100, line=f"loss={loss:.4f}")
```

---

## 4. 多 Colab 并发防撞检查表

同时开多个 Colab 跑同一项目时，按以下 4 条防撞：

1. **不同实例取不同 `--host-id`**（如 `colab-t4-01`、`colab-t4-02`），且不要与 `hosts.toml` 里的实验室主机重名；单项目最多 5 台同时在线。
2. **不同实例跑不同 `--job`**（如 `exp1_s0`、`exp1_s1`），且每个任务独占 `--artifact-dir` 子目录（`runs/<job_id>/`）。
3. **禁止多实例写同一个 checkpoint 或汇总文件**；若误在两台实例跑了同一个 `--job`，看板会立即在心跳响应里返回 `warnings: ["duplicate_job:<job_id>"]` 并在项目里产出 `ephemeral_dup:` 告警——**立刻停掉多余那台**。
4. **关注云盘余量**：Google Drive 剩余低于 1 GiB 时看板会标红并产出 `ephemeral_disk_low:` 告警，且 `labmon disk` 会单列临时主机存储区，及时清理旧 `runs/` 目录避免写盘失败。

---

## 5. 断连 / 被回收后续跑

1. 临时实例超过 180 秒无心跳（默认 TTL）时，看板自动把残留 `running` 任务结转为 `failed` 并触发 `ephemeral_down:` 告警（含 `artifact_dir`），唤醒本地挂着的 `labmon wait`。
2. 在新开的 Colab 实例重新挂载 Google Drive，核对 `<artifact_dir>/ckpt.pt` 是否完整。
3. 用相同 `--job` 与 `--artifact-dir`、开启训练脚本的 `--resume` 重新运行即可（可沿用原 `--host-id` 或换新 `--host-id`，旧实例 `instance_id` 会自动结转、不冲突）。
4. 切勿把 `LABMON_TOKEN` 写进 Notebook 单元格或保存到 Google Drive 文本文件中；优先使用 Colab Secrets（左侧钥匙图标）并开启 Notebook 访问权限。
