# 在各客户端（Antigravity、Codex 与 Claude）中持续等实验

共用 [SKILL.md](SKILL.md) 的流程与规则。各客户端进程等待差异如下：

## Antigravity

1. 在项目目录执行 `labmon wait -c monitor.toml`（等指定任务加 `--job 名称`）。使用 `run_command` 工具运行，设置 `WaitMsBeforeAsync` 转为后台任务，保存任务 ID。
2. 命令在后台运行时，系统会在输出到达或进程退出时响应式唤醒（Reactive Wakeup），无需轮询；收到完成通知后读取输出与退出码处理。

## Codex

1. 在项目目录执行 `labmon wait -c monitor.toml`（等指定任务加 `--job 名称`）。使用 `exec_command`，保留返回的 `session_id`。
2. 命令运行期间，使用 `write_stdin` 向同一 session 发送空字符串等待输出（使用允许的较长等待时间，如 30–60 秒），长轮询同一进程，不新建进程、不 ssh 查状态。
3. 若被 `functions.exec` 包装：外层返回 cell ID 先用 `functions.wait` 取结果；内部命令返回 session ID 后再用 `write_stdin`。两种 ID 不混用。

## Claude Code

有后台命令与通知能力时，将 `labmon wait` 作为后台任务启动并保存任务 ID；收到完成通知后读取输出与退出码处理。

## 退出后做什么

- **退出码 1：新告警。** 逐条阅读输出。若包含 `archived` 说明项目已归档，结束监听即可。其余一般告警**先重新挂上同一 wait 命令（保留 `--job`），再逐条按 [alerts.md](alerts.md) 处理**（遇 `skill_updated` 则重读提示的章节）。
- **退出码 0 / 4：所等任务结束。** 0 为全部成功，4 为失败或异常退出。检查日志与输出，任务等待到此结束。
- **退出码 3：配置、登录或冲突。** 提示已有其他会话持有 wait 时，本会话不再挂 wait（要看状态用 `labmon status`）；确认需接管时才使用 `labmon wait -c monitor.toml --take`。
- **退出码 5：客户端需更新。** 请用户执行 `labmon update`，更新后重新挂起等待。

> **严禁管道过滤**：不得将 `labmon wait`（含带 `--job`）输出通过管道重定向过滤（如 `2>&1 | grep ...`）。在无 `pipefail` 的环境中，管道命令退出码会掩盖真实退出码，且提前退出可能触发 SIGPIPE 异常中断。读取输出应直接读 stdout/stderr 或重定向到文件。

- 一个项目仅由一个会话挂 `labmon wait`。
- 断网时 wait 会自动退避重试，连接恢复后报 `server_back`；失联满 1 小时报 `server_unreachable` 退出，此时通知用户并重新挂起。

## 压缩、续接与交接

在交接记录中记录项目目录、等待命令（含 `--job` 参数）、未处理事件及任务 ID。续接时先核对后台进程有效性，避免双重监听；通过 `labmon status` 和 `labmon check` 补充查看交接期间的状态变更。
