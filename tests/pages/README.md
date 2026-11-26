# 页面测试与离线预览

改了页面时怎么测，规则见 [AGENTS.md「怎么测」](../../AGENTS.md#怎么测)；这里写具体做法。

- `tests.test_pages` 会渲染工作台、项目页与登录页、编译脚本，再在 node 的 DOM 桩（`dom.js`）里逐个路由渲染并断言（不漏 NaN/undefined、恶意字符串都转义、各处回归）。
  快照在 `snapshots.py`，加了字段或页面分支就补进去，断言在 `check.js`。部署脚本的冒烟检查 `smoke.js` 与它共用 `dom.js` 的 `leak`。

## 离线预览

```bash
python3.11 tests/pages/preview.py --port 8799 [--kind normal|evil|sparse] [--as admin|public|user] [--prefix /pfx]   # --prefix 照反代挂在路径前缀下（#165）
```

- **打开与验收（按客户端并列）**：
  - **Antigravity**：使用 `browser` 子代理（`Model: "flash"`）核验离线预览页面与控制台报错；Swarm worker 若环境无内置浏览器工具，可回退本机无头浏览器脚本核对，或交总控调用 `browser` 子代理核验。
  - **Claude 桌面应用**：缺省参数在 `.claude/launch.json` 登记为 `labmon-preview`，`preview_start` 按名字起服务并开一个标签。worktree 子 agent 或需带 `--kind`、`--as`、`--prefix` 时，在自己的 worktree 里后台跑上面那条命令，再 `preview_start` 传 `url: http://localhost:8799/` 开标签。
  - **Codex / CLI 等无原生内置浏览器环境**：在 worktree 启动 `preview.py` 后，使用无头脚本（如 headless Playwright / curl 桩）抓取核对，或交总控核验。
- **窄屏**：按 375 宽（如 mobile 预设或无头浏览器窗口预设）核对响应式布局，窄屏贴底与抽屉显示正常。
- **控制台**：确认无异常报错（控制台日志里只该有 404 与 501 两类预期返回，别的报错都是页面的问题）；页面文本与版面结构正常。
- **身份**：`--as` 定“我是谁”（缺省从内网入口来的管理员；`public` 是从公网入口来的管理员，`user` 是普通用户），改了按身份收起的按钮、账号自助与人员管理（`#/me`、`#/fleet/people`）或登录页（`/login`）的三种都看。
- **它做什么**：把 `snapshots.py` 的快照经 `collect.write_page` 渲染到临时目录（退出时删掉：Ctrl-C、kill（SIGTERM）、关掉终端（SIGHUP）都行，`kill -9` 会留下 `/tmp/labmon-preview-*`），起一个只读的静态服务，目录布局照看板服务。
  不 ssh、不连平台、不发通知，不碰真看板（dashboard-host 上的主、笔记本上的冷备）与 `~/.local/state/labmon`。页面定时取的 `/overview.json`、`/records.json`、
  项目页的 `snapshot.json`、`events.json` 都放了同一份快照；组内服务器详情（`/fleet/<id>.json` 等）返回 404、所有 POST（立即采集、平台操作）返回 501，
  控制台里只该有这两类，别的报错都是页面的问题。看真数据的效果只在部署后由部署 agent 在真看板上看。
- 更多参数（`--kind` 的全部取值等）见 `preview.py` 开头的说明。
- 用完按 [AGENTS.md「怎么测」](../../AGENTS.md#怎么测)的清理规则关掉自己开的标签、停掉预览服务。
