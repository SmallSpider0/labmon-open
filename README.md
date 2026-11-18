# labmon

本机各科研项目在实验室 GPU 服务器上跑实验的统一工具，四块：

- **项目看板**（`/p/<id>/`）：本机定时经 ssh 在服务器上跑只读探针，汇成统一快照，渲染成一张数据内联的页面；一个项目一份 `monitor.toml`，服务器上不用装任何东西。
- **工作台**（`/`）：各主机的资源、每个任务的用量、跨项目的放行顺序与推演、租期与续租、记录。
- **服务器端 worker**（`labmon_worker.py`，单文件、只用 Python 3.8 标准库）：每台主机一个，统一调度这台上各项目的队列：准入、公平份额、独占、临时指令、保活。
- **组内服务器管理**：组里别人的服务器只读轮探，平台续租、开关机、换人，借来用与归还，共享盘按人统计。

看板服务可常驻在 Linux（含 WSL2，systemd 用户单元）或 macOS（launchd）。命令行通过看板接口工作；远端部署、HTTPS、公网反代及冷备见 [部署文档](docs/deploy.md)。仓库中的主机、人员、地址和项目均为示例。

本项目采用 [MIT 许可证](LICENSE)，Copyright (c) 2026 SmallSpider0。

## 文档

| 要找什么 | 看哪里 |
|---|---|
| 一页图文介绍：labmon 是什么、各部分怎么配合、看板上各块的意思、告警该怎么办，关键算法的示意图（只概括，以下面各文档为准） | [docs/intro.html](docs/intro.html)；维护规则见 [AGENTS.md「项目介绍页」](AGENTS.md#项目介绍页) |
| 接入一个项目、排队、暂停、看告警、取结果（给实验会话的操作规程） | [skills/lab-experiments/SKILL.md](skills/lab-experiments/SKILL.md) |
| 看板服务与命令、工作台各块、项目页各块、告警清单、掉线与租期、预备续租、借用与归还 | [docs/workbench.md](docs/workbench.md)（告警清单在 [这里](docs/workbench.md#告警)） |
| worker：命令、清单写法、资源提示、占位、依赖、独占、准入与公平份额、预计时长、推演、临时指令、单元申请 | [docs/worker.md](docs/worker.md) |
| 跨项目调度的设计与依据、分期、改动记录 | [docs/scheduler.md](docs/scheduler.md) |
| 组内服务器：配置、探测与归类、保活守护、平台操作（核对、续租、开关机、换人）、借用、共享盘 | [docs/fleet.md](docs/fleet.md) |
| 快照与状态文件的字段 | [docs/schema.md](docs/schema.md) |
| 看板的登录、账号与令牌、两个入口、各路由要的权限与账号接口 | [docs/api.md](docs/api.md) |
| 看板服务所在机：装服务、平台密码、部署（远端模式）、体检、两个入口的证书、TX 公网入口与看门狗、计划维护、主与备用、冷备与恢复 | [docs/deploy.md](docs/deploy.md) |
| SCM 平台的接口记录 | [docs/platform_scm.md](docs/platform_scm.md) |
| 配置逐项说明 | [examples/monitor.toml](examples/monitor.toml)、[examples/hosts.toml](examples/hosts.toml) |
| 改 labmon 本身的规则（怎么开发：Issue 与 PR、子 agent 分工、审查；一个概念只有一处实现、怎么测、怎么部署） | [AGENTS.md](AGENTS.md) |
| 设计新功能、改行为时逐维度对照的影响清单与设计说明模板 | [docs/interactions.md](docs/interactions.md) |
| 开发流程一页图：从 Issue 到部署、分工与强度档、实验会话怎么报问题（只概括，以 AGENTS.md 为准） | [docs/workflow.html](docs/workflow.html)；维护规则见 [AGENTS.md「流程总览」](AGENTS.md#流程总览) |

## 安装

首次自建看板：克隆本仓库，在 Python 3.11 或更新环境执行 `python -m pip install -e .`，复制并修改 `examples/hosts.toml`，运行 `labmon user person <人员名>`、`labmon user add <账号> --person <人员名> --admin`，再运行 `labmon service install`。主机地址、共享目录和可选平台接口须按部署环境填写；通用 SSH 调度无需配置 SCM。

本机需要 Python ≥ 3.11（用到内置的 `tomllib`），没有第三方依赖。服务器端只需要系统自带的 `python3`（≥ 3.8）。

在一台只用命令行的新机器上，先登录看板。没有账号时请维护者开账号，拿设置码在登录页设口令。浏览器使用内网 HTTPS 入口时要信任看板的 CA 证书，使用 TX 公网入口时还要导入维护者签发的客户端证书。

1. 打开「我的账号 → 命令行」，下载 `labmon-install.sh`，然后运行：

   ```bash
   sh ~/Downloads/labmon-install.sh
   ```

   脚本用下载包离线创建命令行的虚拟环境，并为下载时用的入口生成 `~/.config/labmon/client.toml`。已有配置时另存为 `client.toml.new`。内网包附带 CA 时，脚本会保存证书并填好 `ca`。内网使用私有 CA 而包里未附时，按脚本提示向看板管理员领取 CA 公钥，保存后启用该入口的 `ca`，再登录。从公网下载时，按 [docs/deploy.md「给新用户签客户端证书」](docs/deploy.md#给新用户签客户端证书) 填好 `cert` 和 `key`。需要同时配两个入口时，照 [client.toml 的写法](docs/api.md#连哪个看板clienttoml) 把内网写在前面。
2. 跑 `labmon login`，输入看板账号和口令，给这台机器创建一个 `project` 令牌。随后用 `labmon host list` 检查能否连接。普通用户的列表由管理员给这人开放的主机决定。
3. 看到版本提示时跑 `labmon update`，从看板取新版。

安装与更新会一并装好配套 skill（见下面「配套 skill」）。

安装默认提供 `~/.local/bin/labmon`。如果那里已有其他安装，脚本会提示新命令的固定路径，默认是 `~/.local/share/labmon/bin/labmon`，也可移开旧命令后重新运行安装脚本。各版本的虚拟环境默认在 `~/.local/share/labmon/versions/` 下（设置 `XDG_DATA_HOME` 时改用它）。更新后可以等旧版的 `labmon wait` 等进程退出，再手动清理固定入口已不再指向的旧环境。

维护者要在本机运行看板服务或开发时，可在仓库里运行 `python3 -m pip install -e .`。

**配套 skill**：[skills/lab-experiments](skills/lab-experiments/SKILL.md) 是给编码智能体的统一操作规程。

- **用安装脚本装的**：skill 随命令行一起装，一起更新。包里带着 skill，以及它链接到的 `README.md`、`docs/*.md` 和 `examples/`。固定入口 `~/.local/share/labmon/skills/lab-experiments`（设了 `XDG_DATA_HOME` 时在它下面）指向当前版本里的那份，随命令一起切换。
  有 `~/.claude` 的，安装时建 `~/.claude/skills/lab-experiments`；有 `~/.agents` 或 `~/.codex` 的，建 `~/.agents/skills/lab-experiments`。已有 `~/.codex/skills/lab-experiments` 的，只认那一个。这些入口都指向固定入口，只建一次。
  已有同名目录、或软链指向别处（比如仓库）的，不覆盖，只提示一句；要改用随命令行更新的那份，先把它移开，再跑 `labmon update`。
  `labmon wait` 换版本时，如果两版之间 skill 有改动，会叫醒会话一次（`skill_updated`），说改了哪几节。
- **从仓库直接跑的**：用软链安装，仓库更新即生效：

```bash
# 按使用的客户端选择；同一份skill，无需复制内容
mkdir -p ~/.agents/skills    # Codex
ln -s /path/to/labmon/skills/lab-experiments ~/.agents/skills/lab-experiments
mkdir -p ~/.claude/skills    # Claude Code
ln -s /path/to/labmon/skills/lab-experiments ~/.claude/skills/lab-experiments
```

已有同名目录或链接时，先检查它指向哪里，不覆盖用户的修改。当前安装如果已经从 `~/.codex/skills` 发现了这个技能，就继续用那个入口，避免重复安装。新会话确认技能列表中出现 `lab-experiments`，Codex 可用 `$lab-experiments` 显式调用；未发现时先直接读取上述 `SKILL.md` 并检查安装目录。持续监听方式见 [sessions.md](skills/lab-experiments/sessions.md)。

## 用法

主机写在用户级主机清单 `~/.config/labmon/hosts.toml`（各项目共用，示例见 [examples/hosts.toml](examples/hosts.toml)；环境变量 `LABMON_HOSTS` 可改路径）。
项目里放一份 `monitor.toml`（逐项说明见 [examples/monitor.toml](examples/monitor.toml)）；项目页的车道按 `labmon worker add --project <id>` 的登记生成，`[[queues]]` 可选（写了的 `host` 直接写主机 id，登记过的以登记为准）；项目特有的主机设置写在 `[[hosts]] id = "…"` 下，与主机清单合并：标量字段由项目覆盖，`mounts` 按 `path`、`checks` 按 `id` 合并（同 id 的检查字段覆盖、`detail` 拼接）。也可以照旧把主机写全在 `monitor.toml` 里。

```bash
labmon update                           # 从看板下载当前版本并更新命令行
labmon login                            # 命令行经看板接口做事（#162）：输口令换一个 project 令牌（--admin：1 小时的 admin 令牌）；连哪个看板写在 ~/.config/labmon/client.toml（docs/api.md「命令行」）
labmon check   -c monitor.toml          # 看板校验配置，打印各主机的探针规格
labmon collect -c monitor.toml          # 让看板立即采集这个项目一次，打出结果与链接
labmon render  -c monitor.toml          # 看板那台机器上：只用上次的 snapshot.json 重新渲染页面（改页面时用）
labmon project push -c monitor.toml     # 登记到看板上（第一次推就是登记）/ 推上改动；看板上改过的先 labmon project pull；改了 [[hosts]]、[[queues]] 的加 --admin（docs/workbench.md「项目登记与配置」）
labmon service install                  # 装上常驻的看板服务（macOS launchd、Linux systemd）：缺省 http://127.0.0.1:8765/（dashboard-host 上是 --port 8443，docs/deploy.md），项目页 /p/<id>/；要登录，第一次先 labmon user person <人员名>，再 labmon user add <账号> --person <人员名> --admin
labmon wait    -c monitor.toml          # 每轮先与看板同步配置，再问看板等告警（长轮询）：出现新告警或采集停了即退出（退出码 1），用来唤醒会话
labmon wait    -c monitor.toml --job 任务   # 同时等任务跑完（问看板、不连服务器）：都有结果时退出，全成功 0、其余 4

labmon worker start 29                 # 服务器端 worker：部署 main 上已提交的版本并拉起（已在跑则无缝替换；start、stop、limit 要 admin 令牌）
labmon worker add 29 ROOT JOBS --project <id>   # 登记一条队列；之后改清单即可
labmon worker add pool ROOT JOBS --project <id>  # 交给算力池（池成员；入池在「我的账号 → 我的服务器」，docs/fleet.md「算力池」）
labmon worker status 29 | fit 29 任务名… | limit 29 heavy=2 | rm 29 ROOT JOBS | stop 29   # 看板项目「管理」里能按主机把清单换到别的主机、不用一台，新主机页能接上上一台的
labmon pause <项目> [--until 11-10] [--now] [--host 29] | resume | focus <项目> --until … | unfocus | drain 29 | undrain 29   # 临时指令（项目的缺省所有主机；--until：11-10、11-10 18:30、明天 9:00、+30m、+3h）
labmon project init 29 [项目]           # 按共享盘目录规范建目录
labmon project archive|restore <项目 id>  # 项目做完了收起（要求暂时没实验；移除登记、撤权重与指令、看板不再采集，不删数据）/ 加回

labmon host list | return <id> [--now] | restore <id>    # 主机清单：在用 / 借来的 / 已归还，共用共享盘的几台
labmon borrow <组内服务器> [--until "明天 18:30"] [--for a1]   # 借来用（约定精确到分；要 admin 令牌）；不给参数列出在借的
labmon fleet [status <id>] | probe <id> | boot [id] | import <id> <Json 文件>   # 组内服务器（看板那台机器上）
labmon notify test [文字]               # 照 ~/.config/labmon/notify.toml 试推一条到手机（Bark 或 ntfy）
labmon server maint 30m | 18:30 | off   # 看板服务的计划维护：期间 TX 的看门狗与体检不推（最长 1 天）
labmon server lease [--take | --release]   # 服务租约：这台看板服务是主还是备用；强制拿到；释放（docs/deploy.md「主与备用」）
labmon backup pull --to 看板地址 [--ca 证书] | schedule --to … | list | make   # 冷备：笔记本每天经 API 取一份、留 14 份（不含秘密）
labmon server restore <备份>              # 从冷备恢复成这台的看板服务（原来的状态挪到旁边）
labmon secret set|rm platform [--user 账号]   # 在看板服务那台机器上放（或删）平台密码：macOS 钥匙串，Linux 600 权限的文件（docs/deploy.md）
```

`labmon --help` 列出全部子命令；各命令的含义与规则见上面的文档索引。

## 结构

```
labmon/__main__.py      命令行入口：collect / render / check / serve、wait（读快照等告警与全局消息）、service、host、worker、project、fleet，
                        临时指令 pause / resume / focus / unfocus / drain / undrain，borrow
labmon/config.py        读取并补全 monitor.toml 与用户级主机清单（含调度设置 sched）；时刻的格式化与解析（fmt_time / parse_time）；
                        改主机清单（HOSTS_LOCK 下读 → 改 → 校验 → 写）；缺省端口 PORT、worker 存活判定 STALE_S
labmon/state.py         本机状态目录与状态文件的底层读写：JSON（load_json / save_json，write_atomic 整文件覆盖）、JSONL（parse_rows / load_rows / append_row）、全局快照 snapshot()；状态根目录 state_root 与状态目录 state_dir
labmon/records.py       记录：事件、平台操作、配置改动的写入（log_events）与按条件读取（records）；事件种类表 EVENT_KINDS / EVENT_CATS
labmon/health.py        连通与租期的判定，工作台主机、组内服务器与项目页共用：掉线与恢复 link_step、换容器 container_step、到期时刻 lease_end、提醒档 lease_due
labmon/lan.py           连服务器的 ssh（共用长连接）与“本机在不在内网”；按主机并行探测 gather
labmon/probe.py         项目页的探针（在被监控主机上运行，只用标准库，兼容 3.8）：GPU、cgroup、磁盘、自定义检查、队列原始数据
labmon/collect.py       项目页：跑探针 → 统一快照（hosts / lanes / jobs / groups / alerts / history）→ 渲染 page.html
labmon/page.html        项目页的单文件页面模板，只读快照
labmon/overview.py      工作台的数据：读各主机 worker 的状态、连通与租期（health）、调度卡 sched_view、各卡被谁占着 occupancy；
                        写给各主机 worker 的 eta.json、sched.json 与 returned.json；渲染 overview.html
labmon/overview.html    工作台的单文件页面：总览、主机页、项目页入口、记录、组内服务器
labmon/common.js        两个页面共用的脚本（转义、悬停提示、本机存的设置、时刻与时长的文字、借用的说明与小标签、主机短名与车道名、时间线引擎与占用条），渲染时内联进页面
labmon/serve.py         看板服务：各项目、工作台、组内服务器的采集线程与 HTTP（路由全表见文件头部注释）；内网与公网两个入口，路由表 ROUTES 定每条要的权限（docs/api.md）；
                        只认 JSON 类型与同源的 POST、校验 Host（防跨站，docs/workbench.md「防跨站请求」）
labmon/users.py         看板的账号、会话与令牌：口令哈希、登录限速、作用域、普通用户看到的快照与记录（docs/api.md）
labmon/deploy.py        看板服务收部署（快进只放 main 的 checkout、按改动等平台操作做完再重启）、在跑哪个提交；部署脚本与它共用的判断（is_doc、日志 Traceback）
labmon/service.py       用 launchd（macOS）或 systemd（Linux）托管 serve（service install）；服务日志的一行 log；系统通知 notify（macOS 弹窗、推到手机）
labmon/push.py          推到手机（Bark、ntfy）：读 notify.toml、推一条；只用标准库，TX 上的看门狗拷它用
labmon/maint.py         看板服务的计划维护（labmon server maint）：/healthz、体检与总览小卡读它
labmon/projects.py      项目的登记（状态根的 projects/<id>/：配置、done_when 的文件、输出目录；推 put_config、取 get_config、页面上改 set_config）与归档、恢复
labmon/projsync.py      项目目录里的 monitor.toml 与看板上那份同步：labmon project push|pull、labmon wait 每轮的 sync
labmon/client.py        命令行经看板接口做事：入口回退、令牌、登录与更新
labmon/dist.py          从 main 的已提交文件生成客户端 wheel 与安装脚本
labmon/api.py           命令行那几条命令（借卡、归还、临时指令、worker、project init、check）在看板服务这边的做法，回 {rc, out, err}
labmon/waits.py         labmon wait 在看板这边：新告警与全局消息、按名字认任务、长轮询 /api/wait
labmon/lease.py         服务租约：每个 worker_home 下一份，只有主动 worker 的输入、平台与组内服务器、发通知；备用只读租约（labmon server lease）
labmon/backup.py        冷备与恢复：打包状态根与主机清单（不含秘密），笔记本经 API 取、留 14 份，labmon server restore 恢复
labmon/hostcheck.py     看板服务跑在 WSL 里时体检所在的 Windows 机（待装更新、待重启、开机时长、内存、隧道），到时推提醒；总览的「看板服务」小卡（labmon hostcheck）
labmon/labmon_worker.py 服务器端的队列执行器（见 docs/worker.md）
labmon/workerctl.py     在看板服务那台上部署、拉起、停止 worker，登记队列，下临时指令（每条命令一次 ssh，复用看板的连接）
labmon/renew.py         自用主机的预备续租，以及续租后拉起 worker、放行；运行中任务的预计结束 eta_of（续租与借用的截止判断共用）
labmon/rebalance.py     多主机负载均衡的提示：把各主机 worker 算的 offers 汇成每个项目一条挪法建议（rebalance: 告警），不改清单与放行
labmon/borrow.py        借用组内服务器：借来拉起 worker、按对方历史用量推荐给这人留的量、约定时间准入、跑完自动归还；给 labmon wait 的全局消息
labmon/pool.py          算力池：入池开关（我的服务器）、池成员、池清单撤回时在哪撤；池清单经 worker add pool 登记（docs/fleet.md「算力池」）
labmon/fleet.py         组内服务器的探测、归类、保活守护（fleet_guard.sh）与状态文件
labmon/fleetview.py     组内服务器页面的数据（概览、详情曲线、按人）与命令行 labmon fleet status
labmon/fleetops.py      平台操作：核对、续租（自用主机与组内服务器共用）、开保活、关机、换人、开机、借来用的后台执行与进度
labmon/fleetboot.py     开机参数：人员名单、资源池与模板作业、端口；自用主机当成组内的一台 mine
labmon/platform.py      SCM 平台的接口（登录、作业列表、克隆、停止、提交）
labmon/disk.py          共享盘：取回 worker 的扫描索引，按人汇总用量、增量、冷数据，快满与暴涨告警；GPU 负载按归属表归人 whose
labmon/fleet_probe.sh   组内服务器的只读探针（bash，经标准输入送去）
labmon/fleet_guard.sh   在组内服务器上开、停、重开看板登记的保活；用户空转保活经配置脚本核实归属后换成看板保活
tools/issue_watch.py    值守会话的常驻等待：本仓库出现新 Issue 才退出并打印（见 AGENTS.md「谁做什么、子 agent 怎么派」）
tools/tx/               TX 上的看门狗 labmon-watch.py（探 /healthz，异常 15 分钟推到手机）与它的 systemd 单元、nginx、sshd、fail2ban 模板（装法见 docs/deploy.md）
tools/wsl/              dashboard-host 的 WSL 里到 TX 的反向隧道单元 labmon-tunnel.service
tools/deploy.py         部署到看板并核对（本机模式 pull；远端模式经 POST /api/admin/deploy 传 git bundle，docs/deploy.md）：等平台操作做完、pull、重启前再等一次、按改动重启与拉起 worker、核对日志、worker 与页面（页面用 tests/pages/smoke.js；见 AGENTS.md「部署到本机看板」）
```

页面和告警只认统一快照（字段见 [docs/schema.md](docs/schema.md)），不认识具体项目。

**新增一种队列**（如 pueue、task-spooler、Slurm）分两步：

1. 在 `probe.py` 写 `queue_<type>(spec) -> dict`（在主机上取原始数据），登记到 `QUEUE_ADAPTERS`。
2. 在 `collect.py` 写 `parse_<type>(raw, t) -> (lane, jobs)`，把原始数据转成统一的任务结构，登记到 `QUEUE_PARSERS`。

ETA、时间线、计划项和告警都不用改。

**现有队列类型 `gpu_queue`**：文件标记式队列，labmon worker 与原来的 `gpu_queue.sh` 都按它写状态：清单每行 `名称|命令`，行内 `est_min=N` 是估时；同名 `.env` 里的 `QUEUE_STATE` / `QUEUE_LOGS` 给出状态目录和日志目录；状态目录里有 `lock`、`current`、`<名称>.done|.failed` 和 `queue.log`；日志里有 `PROGRESS i/N` 形式的进度行。
正则可在配置里用 `est_re`、`progress_re`、`lock_match` 改；它们只影响看板怎么读清单与日志，worker 认资源提示的规则写死（见 [docs/worker.md](docs/worker.md#资源提示)）。

## 测试

```bash
LABMON_STATE=$(mktemp -d) LABMON_NO_NOTIFY=1 LABMON_NO_KEYCHAIN=1 LABMON_LAN=off python -m unittest discover -s tests -v    # 离线
```

测试的结构见 [docs/workbench.md「测试」](docs/workbench.md#测试)，改代码的规则见 [AGENTS.md](AGENTS.md)；PR 上由 CI 在 Linux 上跑同样的测试。
