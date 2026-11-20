<!-- 本仓库公开：只提交合成复现或脱敏摘要。生产身份、地址、配置、原始日志/截图、私有报告编号及关联留在受权看板或私有维护记录。 -->
<!-- 规则见 AGENTS.md「怎么开发」「审查要点」。一个工作单元一个 PR：一个 Issue，或第 2 档的一批（最多 4 个、合计约 300 行，每个 Issue 一个提交）。 -->

<!-- 每个号各写一个关键字（GitHub 只认紧跟在关键字后面的那个号），例：Closes #66, closes #72 -->
Closes #

**档位**：第 _ 档（2 小改 / 3 标准 / 4 核心）；合并后：进下一批部署 / 当场部署（严重 bug，写原因）

**一并审的急修提交**（动到同一块代码的第 0 档提交，有才写）：

**设计说明**：<Issue 评论链接>（第 2 档是“根因 + 改法”评论；一批的每个 Issue 各一条）

**改了什么**（取代了什么旧做法，同一次提交删掉了）

**按推荐先做的决定**（除“必须等”之外的主观与设计问题：措辞、缺省值、阈值、估计公式、准入策略、页面布局与细节、数据契约；维护者合并前后随时能改。没有写“无”）
- 

**影响清单**（docs/interactions.md；涉及的写一句怎么处理，不涉及的写“无关”加一句理由。一批的按 Issue 分节：每个 Issue 一个 `#### #N` 小标题，下面各抄一份这 14 行）
- [ ] 多主机：
- [ ] 同机多项目：
- [ ] 多卡：
- [ ] 借来的主机：
- [ ] 临时指令与截止：
- [ ] 生命周期：
- [ ] 主机与项目的进出：
- [ ] 任务种类：
- [ ] 真放行与推演：
- [ ] 借用现有数据：
- [ ] 数据契约：
- [ ] 呈现与告警（会唤醒谁、会不会误报或重复）：
- [ ] 实验会话侧：
- [ ] 硬约束：

**测试**（写结果；没跑的写原因）
- [ ] `python -m unittest discover -s tests`（带 `LABMON_NO_NOTIFY=1 LABMON_NO_KEYCHAIN=1 LABMON_LAN=off`，`LABMON_STATE` 指到临时目录）
- [ ] `flake8 --select=F --max-line-length=200 labmon tools`
- [ ] 新用例在旧代码上会失败
- [ ] 跨主机的逻辑：多主机用例（`tests/fixtures.Hosts`）
- [ ] 改了页面：`tests/pages` 补了快照与断言，在内置浏览器里用离线预览（`tests/pages/preview.py`，见 AGENTS.md「怎么测」）逐个路由核对，`read_console_messages` 没有报错；看过桌面与窄屏（`resize_window` 的 mobile，375 宽）两档：写明路由、结果及本任务开的标签与预览服务是否已清理（`gh` 传不了图片，不把截图提交进分支；只允许提交合成离线预览截图，提交前核对画面不含私人信息）

**唯一实现表**
- [ ] 对照 AGENTS.md 的表搜过，没有第二份；新增或扩展了表里哪一项：

**文档**
- [ ] 文档在本 PR 里一起改了（不另开 PR 补）；行为变了的地方已同步、旧说法已删：README.md / docs/workbench.md / docs/worker.md / docs/fleet.md / docs/schema.md / docs/scheduler.md（设计与依据）/ skills/lab-experiments/SKILL.md / AGENTS.md / docs/workflow.html（改了工作流时）
- [ ] 改了用户看得到的功能或算法：已同步 docs/intro.html（说法、示意图、数字；合并后由落到 main 的 agent 核对 HTML 文档，见 AGENTS.md「项目介绍页」）；没改的写“无关”
