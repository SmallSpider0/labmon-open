# 共享盘目录（labmon 管理）

本目录是主机清单中配置的 `data_root`；只在获授权的目录内写入。临时文件、缓存、中间产物放容器 `/tmp`。

```
labmon/              服务器端队列执行器（labmon_worker.py 与 worker/）及各主机的队列登记、状态、日志（labmon worker …）
envs/<名称>/         共享 Python 环境，按版本命名，如 py311-torch2.4-cu124；建好后不改，要新版本就另建一个新名字
datasets/<名称>/     共享数据集（cifar10、cifar100、cifar10c…），各项目只读引用，不另存副本
weights/<名称>/      共享预训练权重与 torch / HuggingFace 缓存（TORCH_HOME、HF_HOME 指到这里）
projects/<项目>/     各项目自己的目录：代码、结果、日志；项目只写这里
archive/<日期>/      压缩归档，以及删除前的清单（删什么、多大、谁确认的）
```

- 新项目：本机 `labmon project init <主机> <项目>` 建 `projects/<项目>/` 并列出现有环境和数据集。先挑现成的，
  都不合适才在 `envs/` 新建，新建的也供以后共用。
- 删除或压缩前先列出精确目标和大小，经用户确认后执行，清单留在 `archive/<日期>/`。
