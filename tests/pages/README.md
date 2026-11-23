# 页面测试与离线预览

改了页面时怎么测，规则见 [AGENTS.md「怎么测」](../../AGENTS.md#怎么测)；这里写具体做法。

- `tests.test_pages` 会渲染工作台、项目页与登录页、编译脚本，再在 node 的 DOM 桩（`dom.js`）里逐个路由渲染并断言（不漏 NaN/undefined、恶意字符串都转义、各处回归）。
  快照在 `snapshots.py`，加了字段或页面分支就补进去，断言在 `check.js`。部署脚本的冒烟检查 `smoke.js` 与它共用 `dom.js` 的 `leak`。

## 离线预览

```bash
python3.11 tests/pages/preview.py --port 8799 [--kind normal|evil|sparse] [--as admin|public|user] [--prefix /pfx]   # --prefix 照反代挂在路径前缀下（#165）
```

- **打开**：缺省参数的预览在 `.claude/launch.json` 里登记为 `labmon-preview`，`preview_start` 按名字起服务并开一个标签。它读的是会话所在目录的 `launch.json`、在那份 checkout 里起：
  Agent 工具派出的 worktree 子 agent 这样起的是主 checkout 的 main，看不到自己的改动；要带 `--kind`、`--as`、`--prefix` 时也不能按名字起。
  这两种情况在自己的 worktree 里用 Bash 后台跑上面那条命令，再 `preview_start` 传 `url: http://localhost:8799/` 开标签。
- **窄屏**：`resize_window` 的 `mobile` 预设（375 宽）后重新 `navigate` 一次，看完用 `desktop` 预设复原。
- **控制台**：`read_console_messages`（`onlyErrors` 看报错）；读文字用 `get_page_text`、`read_page`，看版面才截图（`computer` 的 `screenshot`）。
- **身份**：`--as` 定“我是谁”（缺省从内网入口来的管理员；`public` 是从公网入口来的管理员，`user` 是普通用户），改了按身份收起的按钮、账号自助与人员管理（`#/me`、`#/fleet/people`）或登录页（`/login`）的三种都看。
- **它做什么**：把 `snapshots.py` 的快照经 `collect.write_page` 渲染到临时目录（退出时删掉：Ctrl-C、kill（SIGTERM）、关掉终端（SIGHUP）都行，`kill -9` 会留下 `/tmp/labmon-preview-*`），起一个只读的静态服务，目录布局照看板服务。
  不 ssh、不连平台、不发通知，不碰真看板（dashboard-host 上的主、笔记本上的冷备）与 `~/.local/state/labmon`。页面定时取的 `/overview.json`、`/records.json`、
  项目页的 `snapshot.json`、`events.json` 都放了同一份快照；组内服务器详情（`/fleet/<id>.json` 等）返回 404、所有 POST（立即采集、平台操作）返回 501，
  控制台里只该有这两类，别的报错都是页面的问题。看真数据的效果只在部署后由部署 agent 在真看板上看。
- 更多参数（`--kind` 的全部取值等）见 `preview.py` 开头的说明。
- 用完按 [AGENTS.md「怎么测」](../../AGENTS.md#怎么测)的清理规则关掉自己开的标签、停掉预览服务。
