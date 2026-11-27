# 独立主机本地工作目录（labmon 管理）

本目录是主机清单中配置的独立主机隔离工作根目录 `data_root`（本地盘持久保存，无集群共享盘）。
请勿直接把日常开发仓库登记给 `labmon worker add`，所有实验运行代码副本、队列清单、日志与产物统一放在本目录下：

```
labmon/              worker 代码、状态、日志与队列登记（worker_home）
envs/<名称>/         本机共用 Python 环境
datasets/<名称>/     只读共享数据集（各项目只读引用；本机 Mac 可建软链接指向已有数据，不重复拷贝）
weights/<名称>/      共用预训练权重与缓存（TORCH_HOME / HF_HOME）
projects/<项目>/     项目独立运行根（worker add 的合法边界）
├── code/            隔离运行代码副本（与日常开发仓物理隔离）
├── queues/          清单与 .env（QUEUE_STATE / QUEUE_LOGS 落在此处）
└── runs/            运行产物、日志与 checkpoint（按批次分子目录便于人工回收）
.tmp/                labmon 管理的本地临时目录（LABMON_LOCAL：缓存与单格临时）
archive/<日期>/      归档记录
```

- 新项目：`labmon project init <主机> <项目>` 建立 `projects/<项目>/{code,queues,runs}` 并列出现有环境、数据集与权重。
- labmon 绝不自动删除 `projects/` 下的运行产物与日志；请定期经 `labmon disk` 查看余量并人工清理旧目录。
