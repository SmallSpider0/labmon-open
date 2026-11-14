# 看板服务的账号、令牌与接口

看板服务（`labmon serve`）一律要登录（#160）：浏览器用账号口令换会话，命令行与脚本用令牌。没有“来自本机就放行”。
实现：账号、会话、令牌与作用域在 `labmon/users.py`；每条路由要什么权限只在 `serve.ROUTES` 里写，请求的身份和作用域经 `serve.Handler._allow` → `users.check` 核对，项目、主机与登记的对象权限经 `serve.Handler._own` 核对。
路由的全表（带请求体）在 `labmon/serve.py` 文件头部注释；防跨站的那几样（`Host`、JSON 类型、`Sec-Fetch-Site`）见 [workbench.md「防跨站请求」](workbench.md#防跨站请求)，先于登录查。

## 两个入口

| 入口 | 怎么开 | 绑在哪 | 能做的 |
|---|---|---|---|
| 内网入口 | `--port`（缺省 8765；dashboard-host 上 8765 被别的程序占着，用 8443，#168） | 主机清单 `[server]` 的 `lan_bind`（缺省 `127.0.0.1`）；配了证书就是 HTTPS | 全部 |
| 公网入口 | `--public-port P`（`labmon service install --public-port P`，写 0 去掉） | 只绑 `127.0.0.1`，给隧道接（HTTPS 由 TX 的 nginx 做，挂在随机路径前缀下） | 除平台操作与部署外的全部 |

- 两个入口共用同一份项目、采集线程与账号；各自认的 `Host` 见下面 [`[server]`](#server-段)。端口只在命令行给，哪台机器用哪个端口按那台的情况定。
- **POST 请求体**：通过入口、权限和主备检查后，服务端先读取 JSON 对象再执行操作。空请求体按空对象处理，非法长度或非法 JSON 返回 400。
- **平台操作按账号允许公网，部署仍限内网**：平台路由要求管理员、admin 作用域；公网还需账号开启 public_ops。部署与 worker 代码推送在公网仍回 404。设置入口见下文「管理员公网平台操作」。
- 公网入口经隧道来的请求源地址都是 `127.0.0.1`，所以两边都只认登录，不按来源地址放行。
- 页面、服务端的跳转与链接一律用相对地址（#165）：公网入口挂在反代的 `/<前缀>/` 下照样用，服务不用知道前缀。

### `[server]` 段

用户级主机清单（`~/.config/labmon/hosts.toml`）里可选的一段，`labmon serve` 启动时读一次（`config.server`，#165），改了要重启服务。不写时与以前一样：只绑本机、明文、只认 `127.0.0.1` 与 `localhost`。

```toml
[server]
lan_bind = "0.0.0.0"                      # 内网入口绑哪（IPv4；缺省 127.0.0.1）。不是本机地址时必须配下面两行证书：口令不在内网上明文传
tls_cert = "tls/dashboard-host.crt"                 # 内网入口的 HTTPS 证书与私钥（PEM；相对路径按主机清单所在目录）；TLS 1.2 起
tls_key  = "tls/dashboard-host.key"
ca_cert  = "tls/ca.crt"                   # 可选，随命令行安装脚本附上的 CA 公钥证书
lan_hosts = ["dashboard-host.lab:8443", "192.0.2.2:8443"]   # 内网入口另认的 Host：浏览器地址栏里的主机与端口
public_hosts = ["203.0.113.7"]            # 公网入口另认的 Host：nginx 用 proxy_set_header Host $host 传进来的（TX 的 IP 或域名）
```

- 写错（不认识的键、证书只给了一半、`lan_bind` 不是 IPv4、绑到非本机地址却没证书）或证书读不了、端口占着：服务不起来，日志里一行原因（退出码 2），不退回明文、不只开一半。
- `Host`：每个入口认 `127.0.0.1:<自己的端口>`、`localhost:<自己的端口>` 与这里另认的；不带端口的按缺省端口算（有 TLS 的与公网入口 443，没 TLS 的 80）。
- 内网入口配了证书时，用 `http://` 打到这个端口的只会断开（不跳转、不进日志）；握手限时 10 秒，连上不说话的不挡别人。

### `GET /healthz`

不要登录（两个入口都有），给 TX 的看门狗探活，只回四项、不出任何主机、项目、人员（`serve.health`，#165）：

```json
{"ok": true, "primary": true, "last_collect": 1762770000, "maint_until": null}
```

`ok` 服务在答；`primary` 是不是主（[服务租约](deploy.md#主与备用服务租约)，`lease.flag`：还没跑过租约的是 `null`，看门狗只把 `false` 当异常）；`last_collect` 工作台最后一次采集的时刻（全局快照文件的修改时刻，重启不清零；没采集过为 `null`）；`maint_until` 计划维护到何时（`labmon server maint`，不在维护为 `null`）。时刻是 epoch 秒。

## 账号与角色

| 角色 | 能做的 |
|---|---|
| 管理员 | 全部：项目的写操作、调度权重、组内服务器的一切（数据与平台操作，平台操作按账号允许公网）、账号管理 |
| 普通用户 | 看自己的项目、获准使用的主机及自己项目所在的主机；能管理自己的项目与登记；组内服务器接口 `/fleet/*` 回 403 |

- 账号存在状态根目录下的 `users.json`（权限 600）：口令只存 PBKDF2-SHA256 加盐的哈希（`pbkdf2_sha256$轮数$盐$哈希`，60 万轮，比对用常数时间），设置码、会话、令牌只存 SHA-256。原文不落盘、不打印、不进日志与记录。
- 名字：字母开头，只用字母、数字与 `_ . -`，最多 32 个；不分大小写认。口令至少 8 个字符。
- **设置码**：加人、重置口令时给出（`XXXX-XXXX-XXXX`），24 小时内有效、用一次作废。拿到的人在登录页「用设置码设口令」里设口令，设好就登录。
- **登录限速**：同一名字连着输错 5 次（口令与设置码一起算）锁 15 分钟，锁记在文件里、重启不清；同时发来的几次也算数：验完写之前按文件里最新的状态再查一次锁，已经锁上的对了也不收。不存在的名字同样计数、同样的回话、同样算一次 PBKDF2，试不出哪些名字存在。
- 最后一个管理员撤不掉、删不掉；页面上不能删自己、不能重置自己（改自己的口令在「我的账号」）。
- 每一笔账号改动（加人、改角色、重置、吊销、删除、建令牌、连着输错被锁）记进记录页「配置改动」（事件种类 `account`，只有管理员看得到）。

## 会话（浏览器）

- 登录成功种 Cookie `labmon_s`：32 字节随机，`Path=/; HttpOnly; SameSite=Strict`；经 HTTPS 的入口另加 `Secure`：公网入口、配了证书的内网入口（没 TLS 的内网入口不加：Safari 不在 `http://127.0.0.1` 上存带 `Secure` 的 Cookie）。
  反代加了前缀时由 nginx 的 `proxy_cookie_path` 把 `Path` 改成前缀，Cookie 不发给 TX 上别的服务。
- 30 天，滑动续期：用着就不过期（“最后用”至多每小时落盘一次，同时重发 Cookie）。看板服务重启不掉线。
- 重置口令、删账号时这人的会话立即失效；吊销会话、撤管理员都是下一次请求就生效（每次请求都查文件，按修改时刻缓存）。
- 没登录：页面（`/`、`/p/<id>/`）302 到 `login?next=<原路径>`（相对地址，浏览器带着 `#/…` 跳，登录后回到原处）；JSON 与 `/api/*` 回 401。页面定时取数据拿到 401 就回登录页。

## 令牌（命令行与脚本）

请求头带 `Authorization: Bearer <令牌>`。令牌是 `lm_` 开头的随机串，原文只在建时给一次（页面上显示一次、命令行写进文件），服务端只存 SHA-256。

| 作用域 | 能做的 |
|---|---|
| `read` 只读 | 看页面与数据（GET） |
| `project` 项目 | 项目的写操作：立即采集、调度的优先与暂停、搬与移除登记、项目配置、归档与恢复；含 `read` |
| `deploy` 部署 | 部署脚本：读数据、立即采集（`POST /collect`），管理员的另能部署与拉起 worker（[部署接口](#部署接口)）；含 `read` |
| `backup` 备份 | 备份脚本：读数据，管理员的另能取冷备（`GET /api/admin/backup`，[deploy.md「冷备与恢复」](deploy.md#冷备与恢复)）；含 `read` |
| `admin` 管理员 | 全部，含账号与平台操作；**1 小时过期** |

- **实际权限 = 令牌的作用域 ∩ 角色能做的**：管理员全部，普通用户可建 `read`、`project` 令牌。角色变更后下一次请求重新计算交集，项目操作仍要核对对象。
- 浏览器会话另有一个 `account`：改自己的口令、建与吊销自己的令牌。令牌没有它：令牌不能再造令牌、不能改口令。
- 令牌不过期（`admin` 的除外），在「我的账号」或「组内服务器 → 人员」里吊销。

```bash
curl -s -H "Authorization: Bearer $(cat ~/.config/labmon/deploy.token)" http://127.0.0.1:8765/overview.json
curl -s -X POST -H "Authorization: Bearer $(cat ~/.config/labmon/deploy.token)" -H 'Content-Type: application/json' -d '{}' 'http://127.0.0.1:8765/collect?fresh'
```

## 路由要的权限

照 `serve.ROUTES`（改权限只改它，这张表跟着改）：

| 路由 | 要什么 |
|---|---|
| `GET /login`、`GET /healthz`、`POST /api/login`、`/api/setup`、`/api/logout` | 不要登录 |
| `GET /`、`/overview.json`、`/records.json`、`/p/…`、`/api/me`、`/api/project/<id>/config` | `read` |
| `GET /fleet/…`（组内服务器的数据） | `read`，只给管理员 |
| `POST /fleet/op/…`、`/fleet/probe/…`、`/renew/…`（平台操作） | `admin`，只给管理员；公网须开启账号许可 |
| `POST /sched/weight` | `project`，只给管理员 |
| `POST /sched/…`、`/project/…`、`/reg/…`、`/p/<id>/collect`；`PUT /api/project/<id>/config` | `project` |
| `POST /collect` | `project` 或 `deploy` |
| `GET /api/admin/deploy` | `deploy`，只给管理员 |
| `GET /api/admin/backup`（冷备，回 `.tar.gz`） | `backup`，只给管理员 |
| `POST /api/admin/deploy`、`/api/worker/start` | `deploy`，只给管理员，只在内网入口 |
| `GET /api/hosts`、`GET /api/client/install.sh`、`GET /api/client/wheel` | `read` |
| `POST /project/owner` | `admin`，只给管理员 |
| `POST /api/wait`、`/api/project/<id>/check`、`/api/project/init`、`/api/worker/(add\|rm\|status\|fit)`、`/api/override/(pause\|resume\|focus\|unfocus)`（[命令行](#命令行162)） | `project` |
| `POST /api/worker/(stop\|limit)`、`/api/override/(drain\|undrain)` | `admin`，只给管理员 |
| `POST /api/borrow`、`/api/host/(return\|restore)` | `admin`，只给管理员；公网须开启账号许可 |
| `POST /api/password`、`/api/tokens/add`、`POST /api/revoke`、`/api/push`、`/api/push/test` | 浏览器会话（`account`） |
| `GET /api/users`、`POST /api/users/…` | `admin`，只给管理员 |
| 没登记的路径 | GET 按 `read`，POST、PUT 按 `admin` 且只给管理员（新写路由忘了登记时从严） |

备用的看板服务（[服务租约](deploy.md#主与备用服务租约)）上，除登录、账号（`/api/login|setup|logout|password|tokens/add|revoke`、`/api/users/…`）与部署（`POST /api/admin/deploy`）外的写请求一律回 409，原因写主是谁、何时能接管，带 `"standby": true`（命令行据此接着试 `client.toml` 里的下一个地址，`serve.standby_ok`）；GET 照答（命令行读的 `GET /api/hosts`、`GET /api/project/<id>/config`、`GET /api/client/wheel` 除外：同样回 409 `standby`，备用上的登记是旧的，`serve.CLI_READS`），`/` 与 `/overview.json` 的 `server` 换成此刻的主备。

不过时：没登录或凭据失效 401（页面拿到它就回登录页），没权限 403（改口令时原口令不对也是 403：会话还有效），锁着 429，回 `{"error": "一句人话"}`。

## 普通用户看到的

- `/` 与 `/overview.json`：去掉 `fleet`（组内服务器）、共享盘的按人明细与按人告警（`burst:`、`over:`）、给系统通知的提醒（`notices`）、给实验会话的消息（`wake`）与看板服务所在机的体检（`hostcheck`，总览的「看板服务」小卡，#166）；`/` 现渲染（`collect.render_page`），不发磁盘上带全量数据的 `index.html`（`users.view`）。
- `/records.json`：去掉平台操作、保活、借用、共享盘、账号、看板服务体检这几类，组内服务器的掉线与恢复，以及与组内某人有关（`who`）的（`users.visible`）。
- 工作台只列自己的项目（含已归档的项目）和获准使用或自己的项目有车道的主机。登记视图只列自己的清单。调度卡保留各项目的份额行，按钮和项目链接只在自己的行上；可见主机上别人的任务照常显示。
- 记录再按相关项目或主机过滤。两样都未标记的全局记录仅供管理员查看。

### 普通用户的对象权限

- 第一次推一个新项目时成为项目主人。已登记项目的读取、推送、等待、立即采集、项目级暂停与优先、配置、归档和恢复都要核对主人。没有有效主人账号的项目归管理员。管理员改主人后，下一次请求按新主人核对。
- 主机清单的自用主机项可写 `users = ["账号名"]`，按账号名授权。带主机的调度指令及 `worker status`、`fit`、`project init` 都要通过主机权限核对。只能开放经 SSH 访问的远端主机。本机执行型主机和回环 SSH 主机仅供管理员使用。同一台远端主机上的任务以共享的系统账号运行，只给可以相互信任的人开这台主机。获准的已归还主机可通过代办移除该账号自己项目的登记。
- 增加、搬动登记还要核对项目主人、目标主机及清单是否已登记在别的项目名下。移除登记要能在看板上查到这条登记属于自己的项目。刚登记的清单还未采集到时，等下一轮再试。普通用户增加、移除、搬动登记或归档时，远端 worker 也要核对登记归属。旧版 worker 会拒绝这些操作，请管理员更新。
- 整台主机或全局的操作仍由管理员执行。

页面知道“我是谁”：看板服务发页面时把 `users.me` 的结果填进 `let ME = …`（与 `GET /api/me` 同一份），页面上按它藏按钮（`common.js` 的 `canOps`、`canWrite`、`canWeight`）。

## 账号接口（JSON，都过防跨站检查）

| 请求 | 回 |
|---|---|
| `POST /api/login {"name", "password"}` | 种会话 Cookie，`{"name", "msg"}` |
| `POST /api/setup {"name", "code", "password"}` | 用设置码设口令并登录，同上 |
| `POST /api/logout` | 删掉这个会话、清 Cookie |
| `GET /api/me` | `{"name", "role", "role_name", "scopes", "via", "entry", "entry_name", "admin", "ops", "write", "account", "commit"（在跑的代码是哪个提交，#164）, "started"（服务启动时刻）}`；普通用户另有 `own`（项目 id）和 `hosts`（获准使用的主机 id）；浏览器会话另有 `sessions`、`tokens`、`token_scopes`、`scope_info`、`push`（打码的推送摘要） |
| `POST /api/password {"old", "new"}` | 改口令：别的浏览器都要重新登录，这个不用；令牌不受影响 |
| `POST /api/tokens/add {"name", "scopes": [...]}` | `{"token"（原文，只这一次）, "id", "name", "scopes", "until", "msg", "commit"}` |
| `POST /api/revoke {"id"}` | 吊销自己的一个会话或令牌（`id` 是列表里给的前 12 位） |
| `GET /api/users` | 各账号：角色、口令设了没有、设置码到何时、锁到何时、最后用、会话与令牌（只有 id 与摘要）、项目、能用的主机、推送是否已配置、组内人员对应 `fleet_person` 与对应错误 `fleet_person_error`，以及名册与已匹配的当前人员 `people`、可开通账号的人员 `person_choices`。清单读取失败时返回 `people_error`，已有明确关联仍可管理 |
| `POST /api/users/add {"name", "admin", "person"}` | 必须给出已登记、尚未绑定的人员 `person`，成功时返回一次性设置码 `code` 和提示 `msg`。重复绑定返回 409 |
| `POST /api/users/role {"name", "admin"}` | 设为或撤销管理员 |
| `POST /api/users/reset {"name"}` | `{"code", "msg"}`：原口令与这人的会话立即失效，令牌不动 |
| `POST /api/users/revoke {"name", "id"?}` | 吊销这人的一个会话或令牌；不给 `id` 是全部 |
| `POST /api/users/remove {"name"}` | 删账号，连同它的会话、令牌与推送配置 |
| `POST /api/users/hosts {"name", "hosts": [...]}` | 更新这名普通用户可用的主机 |
| `POST /project/owner {"project", "owner"}` | 换项目主人；`owner` 为空时归管理员 |
| `POST /api/push {"push", "bark" 或 "ntfy"}` | 保存自己的推送，回 `{msg, push}`；`push` 为空时移除。Bark 段填 `key`，可选 `server`、`encrypt_key`；ntfy 段填 `topic`，可选 `server`、`token`；服务地址限于渠道的官方地址或管理员在 `notify.toml` 配置的同渠道地址 |
| `POST /api/push/test {}` | 用自己的推送配置试发一条，回 `{msg}` |

## 算力池试算

`GET /api/pool` 需要 `read` 权限，并须为试点成员或管理员。工作台身份数据的 `pool_access` 控制入口，接口仍独立鉴权。响应含 `mode: "shadow"`、试算快照时刻 `observed`（有数据时）、`capacity`（`total`、`free`、`max_same_node_free`）、`nodes` 和 `jobs`。节点只给试点标识、核实的物理机归属、型号、状态和卡数；实验仅给本人当前有权限的项目、申请卡数和试算结果。服务端在两种入口都过滤他人作业、路径、命令、设备标识和用量明细。

试点负责人在服务状态目录 `overview/pool-shadow.json` 原子写入 `{"members": ["已有账号名"], "snapshot": {...}}`，`snapshot` 使用 [影子调度内核](scheduler.md) 的输入格式。用于页面的节点和物理机标识填写可向试点成员公开的名称。快照按自身时刻回放，页面标注该时刻。成员变动后同步更新名单和输入；不再维护试点时移除文件。未配置时管理员可见空态。此接口只读，页面加入与退出说明指向试点管理员，实验执行沿用现有流程。

管理员可从内网调用 `POST /api/pool/supply`（`host, device, holder, account, project, until, agreed`）、`/api/pool/run`（`hosts` 为两个节点编号）、`/api/pool/revoke` 与 `/api/pool/status`（`host`）。试点固定命令无脚本或工作目录入参。主备和管理员鉴权与其他写入口共用，运行回包含 `attempts`、`same_node`、`overlap_s`。参见 [管理员固定算子试点](pool-execution.md#管理员固定算子试点252)。

### 成员清单登记

管理员在内网用短期 admin 令牌执行 `labmon pool bind <项目> <绝对目录> [--host <读盘主机>]`。目录必须是已登记项目的获准共享工作区，位于读盘主机的 `data_root/projects/` 下。存在多个匹配的在用非借用主机时，须指定一台。工作区绑定只授予该项目清单登记资格。管理员重新绑定时会撤回已失去资格的原成员留下的待派单清单，有已领取的清单时须等尝试结束才能更换绑定。

本人用 project 令牌执行 `labmon pool submit <相对清单.txt> --request-id <本次编号> [--project <项目>]`。项目缺省取当前目录的 monitor.toml。编号由调用方取一个新的稳定值，网络重试沿用。相同账号、编号和请求会返回原登记。更改清单或同名 .env 后，撤回旧登记并换编号重交。用 `labmon pool list` 查看自己的清单和编号，用 `labmon pool withdraw <编号>` 撤回尚未领取的清单。

- `GET /api/pool/queues` 需 read 和试点权限，返回 `queues` 及 `can_submit` 项目 id 数组。队列有 `id, project, jobs, state, t, can_withdraw`。只读令牌没有写操作标记。
- `POST /api/pool/queues` 需 project，正文含 `project, jobs, request_id`；`POST /api/pool/queues/withdraw` 同权限，正文含 `id`。两者回 `{queue: ...}`。管理员绑定使用 `POST /api/pool/workspace`，正文含 `project, root` 与可选的 `host`。
- 状态 `queued` 表示登记后待派单，`withdrawn` 表示已撤回，`claimed` 表示已被受信派单器领取。池执行接入前不会启动。成员看不到工作区绝对路径或内容指纹。

读盘时验证清单与 .env 均在获准工作区，记录字节指纹。真正执行前须在目标节点再次验证，并使用核对过的内容。已有主机登记的清单应先排空移除。服务端按现有主机登记快照预检，派单前仍需在远端登记锁内核对不存在并行主机登记。

### 日常派单与停止

`GET /api/pool/queues` 带 `dispatch_enabled`。登记有尝试时带 `dispatch`，包含 `phase, node, task_name, wait_reason, wait_job, can_stop, attempt_id`。真实节点投影在开启后从 `GET /api/pool` 读取，`mode` 为 `live`。

`POST /api/pool/queues/stop` 需要 project，正文为 `{id, attempt_id}`，仅本人或管理员可请求当前尝试停止。内网管理员用 `POST /api/pool/supply-daily` 批准 `{host, devices, holder, until, agreed}`，用 `POST /api/pool/dispatch` 设置 `{enabled}`。撤回供给仍用 `/api/pool/revoke`。详见 [日常派单](pool-execution.md#成员清单日常派单254)。

## 客户端分发

- `GET /api/client/install.sh?base=<看板根地址>` 下载 `labmon-install.sh`。页面传自己的根地址（含公网路径前缀）；服务端检查协议和当前入口的 Host 白名单。脚本内有 wheel 和入口配置。内网 HTTPS 配有 `[server].ca_cert` 时随附 CA 公钥证书；未附时脚本说明私有 CA 的手动配置方式。
- `GET /api/client/wheel` 返回同版本的 wheel，响应头 `X-Labmon-Commit` 是完整提交号。`labmon update` 用命令行令牌取包并安装。包从看板启动时运行的 main 提交构建，版本带提交号。备用入口回 409，客户端接着试下一个入口。更新先验证新版本，再切换命令入口。
- 已认证的命令行可用 `X-Labmon-Client` 报告完整的 40 位小写 Git 提交号。服务按令牌保存最近一次报告的 `client_commit` 与 `client_seen`，同版本的报告最多每小时落盘一次。账号详情的令牌摘要还带 `client_status`（`current`、`outdated`、`below_minimum`、`unknown`、`inactive` 或空）。
- `[server].min_client_commit` 可选，取运行版本或其祖先的完整提交号。配置后，带版本头的命令行若低于最低版本或版本无法确认，操作前回 426，正文含 `reason: "client_outdated"`、`minimum` 和更新提示。浏览器与没有版本头的旧脚本照常。登录、账号、更新包、部署与备份入口仍可用。主备切换先于最低版本判定。
- `POST /api/wait` 成功时追加 `commit` 与 `client_status`。用看板安装的客户端发现自己在运行版本的祖先链上且可更新时，会在两次轮询间下载、安装并接着等。该轮有新告警或任务已有终态时，先照原规则退出。收到 426 时，支持此契约的 `labmon wait` 以 5 退出，原因 `client_outdated`。
- 接口演进优先新增字段和路由。已有接口的删改留到各客户端都更新后进行，客户端应容忍附加字段。
- 命令行请求带 `X-Labmon-Client`，值为客户端提交号。`labmon login` 和 `labmon check` 发现版本不同时提示运行 `labmon update`。

## 部署接口

部署脚本的远端模式用（#164，[deploy.md「部署」](deploy.md#部署)）：看板服务那份 checkout 只放 main，由它自己快进、自己重启。判断在 `labmon/deploy.py`。

| 请求 | 回 |
|---|---|
| `GET /api/admin/deploy` | `{"commit"（在跑的）, "started", "head"（checkout 现在的）, "busy"（正在做平台操作的服务器）, "tracebacks"（这次启动以来服务日志里每个 Traceback 的异常行）}` |
| `POST /api/admin/deploy {"bundle": git bundle 的 base64}` | `{"old", "new", "commits", "files", "restart", "worker", "msg"}`；不部署回 409 与原因 |
| `POST /api/worker/start {"host"}` | `{"rc", "out", "err"}`：拉起这台的 worker（推上去的是 checkout 里 main 上已提交的版本）；`labmon worker start` 与部署脚本（本机、远端模式）都经它 |

- 收 bundle 前查：checkout 在 main、没有未提交的改动、没有进行中的平台操作、没有另一次部署在做。bundle 先 `git bundle verify`，`git fetch` 后要是快进（checkout 现在的提交在新提交的历史里），再 `merge --ff-only`；服务端的 main 有别处来的提交就拒绝，不覆盖。
- 改的文件不全在文档、测试、工具里（`deploy.is_doc`）就重启：收下后起一个线程，平台操作在做就等它做完（最多 30 分钟，等不到记一行日志、不重启），安排了重启就不再开始新的平台操作（与 `fleetops.start` 在同一把锁里判定，免得被几秒后的重启杀在半路；2 分钟还没重启就不再挡），再交给服务管理（`service.restart_later`：Linux 上 `systemd-run` 起的临时单元，macOS 上脱离进程组的 `launchctl kickstart -k`）。
- 每次部署记一行服务日志（谁发起、快进到哪）。
- 改动按在跑的代码（`RUNNING`）算：上次快进了、重启没成的，再部署时补上重启；部署脚本远端模式比先后（快照、worker 状态）按回话的 `Date` 头换到服务端的时钟。

## 项目配置

看板上的项目配置（#161，登记目录里那份，[workbench.md「项目登记与配置」](workbench.md#项目登记与配置)）。实验会话的 `labmon project push|pull`、`labmon wait` 每轮的同步经这两个接口（`projsync._get`、`_put`，#162）。PUT 与 POST 一样过防跨站检查（要 JSON 类型）。

| 请求 | 回 |
|---|---|
| `GET /api/project/<id>/config` | `{"id", "ver", "text", "files": {位置: 版本}, "archived"}`，头 `ETag: "<ver>"`；没登记 404 |
| `PUT /api/project/<id>/config {"text", "files": {done_when 的路径: 内容或 null}}`，头 `If-Match: "<上次同步的版本>"` | 成功 `{"id", "ver", "created", "changed", "archived", "msg"}`，头 `ETag` |

- `ver` 是 monitor.toml 文本的 SHA-256 前 16 位；`files` 的键是登记目录 `files/` 下的位置（`config.done_key`），值是内容的版本。
- 第一次推就是登记（不用 `If-Match`），`meta.json` 记下推的账号（`owner`）。带 `If-Match` 但项目不存在时返回 412，旧会话的推送不能因此创建项目。已登记的：没带 `If-Match` 回 428，与看板上的版本对不上回 412（先 GET 或 `labmon project pull`），两者都带 `{"error", "ver" 看板上现在的版本}`；
  已登记又已归档的 409；配置按 `config.load` 校验不过、id 与地址里的不同、id 写法不对、done_when 的文件超过 1 MiB 或一个是另一个的上级目录回 400。都不写。
- `[[hosts]]` 与 `[[queues]]`（`projects.GUARDED`：连哪台主机、在主机或本机上跑什么检查与 Python、读主机上哪些文件）经接口改要平台操作的权限：管理员、`admin` 作用域、入口获准（`users.can_ops`），
  不然 403，第一次推就带着这两段的同样算改了；按 TOML 解析后比，只动了注释、空白的不算。别的项（标题、计划项、里程碑、告警……）`project` 作用域就能推。
  这是为了 `project` 令牌（不过期、会放在别的机器上）不能借推配置在看板那台机器或服务器上执行命令。命令行也经接口（#162）：改这两段要先 `labmon login --admin`，再 `labmon project push --admin` 从内网入口推（wait 每轮的同步只用 `client.token`，推不上当一条 `sync_error` 报）。
- `files` 里给 null 的删掉看板上那份，没给的不动；配置里不再用到的删掉。推成了当场对齐看板服务里的项目（新登记的开始采集，改了的下一轮按新配置采）。

### 永久删除归档项目

`POST /project/delete {"project": "<id>", "revision": "<归档版本>"}` 仅管理员可用，令牌须有 `admin` 作用域。确认值取自工作台 `archived` 条目的 `revision`，旧条目取 `t` 的字符串。成功返回 `{"msg"}`；项目未归档、归档版本改变、采集仍在收尾或有活动依赖时返回 409。该操作会写入看板审计记录。

## 命令行 `labmon user`

在跑看板服务的这台机器上直接改 `users.json`，只认看板服务的账号本人（文件属主不是自己就拒绝）。用来建第一个管理员、忘了口令时找回、给脚本建令牌；别的在页面「组内服务器 → 人员」里做。

```bash
labmon user person <人员名>            # 先登记人员，服务器设置可稍后补
labmon user add <账号> --person <人员名> [--admin]  # 开通并绑定，打出设置码；第一个必须 --admin
labmon user bind <账号> <人员名>       # 为未关联的存量账号补关联已登记人员
labmon user reset <名字>               # 忘了口令：打出新的设置码（这人已登录的浏览器都要重新登录，令牌不动）
labmon user remove <名字>              # 删账号（连同会话与令牌；最后一个管理员删不掉）
labmon user list                       # 列出账号
labmon user token <名字> --scope deploy --name 部署 --out ~/.config/labmon/deploy.token   # 建令牌：原文只写进文件（600），不打到终端
```

## 命令行（#162）

会动东西的命令经看板接口由看板服务做：`wait`、`check`、`collect`、`worker`、`pause|resume|focus|unfocus|drain|undrain`、`project init|push|pull|archive|restore`、`borrow`、`host`。
在哪台电脑上跑都一样，不读写本机的状态、不直接写主机清单、不直接 ssh（客户端 `labmon/client.py`，看板那边的做法 `labmon/api.py`、`labmon/waits.py`）。
**输出与退出码与以前在本机做的一样**：看板回 `{"rc", "out", "err"}`，命令行原样打到标准输出、标准错误，按 `rc` 退出（`usage` 是参数错，退出码 2）。
另：连不上看板服务退出码 2（试了 15 秒），看板拒绝（没登录、令牌没这个权限、配置不对、项目没登记）打它说的原因、退出码 3。

只在看板服务那台机器上用、不经接口的（管的是那台机器上的状态与秘密）：`serve`、`service`、`user`、`secret`、`notify test`、`hostcheck`、`server maint|lease|restore`、`backup make`、`fleet`、`render`。

### 连哪个看板：`client.toml`

`~/.config/labmon/client.toml`（环境变量 `LABMON_CLIENT` 可改）。`[[servers]]` 依次试，内网入口写在前面；没有这个文件时连本机的内网入口（`config.lan_url`：看板服务就在这台时不用写）。

```toml
[[servers]]
url = "https://dashboard-host.lab:8443"             # 内网入口
ca = "~/.config/labmon/dashboard-host.crt"          # 内网入口的证书（钉住这一张，同部署脚本远端模式的 ca）

[[servers]]
url = "https://203.0.113.7/labmon/"        # 公网入口：TX 上 nginx 的路径前缀
cert = "~/.config/labmon/tx-client.crt"   # TX 要客户端证书时
key = "~/.config/labmon/tx-client.key"
```

- 做两遍也没事的请求（读、`wait` 的长轮询）连不上（含 502/503/504）就退避重试（1、2、4…最多 15 秒一次）；会改东西的只在请求根本没送到（连接被拒、找不到主机、502/503）时重试，回话断在半路的不重试，打一句“不知道做成没有”。
- 备用的看板服务（[服务租约](deploy.md#主与备用服务租约)）回 409 `standby`：没做，接着试下一个地址；都是备用就照实打出主是谁、退出码 3。
- 一律直连，不走代理：环境变量 `HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY` 与 macOS 系统设置里的代理都不读（`client.opener`，#195）。Claude 应用里 `HTTPS_PROXY` 指向本机的 Clash、`NO_PROXY` 不含 10.x 时，照它走连内网入口握手会被断。部署脚本（远端模式）与 `labmon backup pull` 连看板同样直连。

### 令牌：`labmon login`

```bash
labmon login                 # 问名字与口令（不回显），建一个 project 令牌存进 client.toml 旁边的 client.token（600）
labmon login --admin         # 另建 1 小时的 admin 令牌存进 admin.token（不覆盖 client.token）
labmon url <项目 id>         # 看板上这个项目的链接（每个入口一行）
```

- 用哪个令牌：环境变量 `LABMON_TOKEN` › 要管理员的命令（与 `labmon project push --admin`）先用 `admin.token` › `client.token`。令牌只放在 `Authorization` 头里。
- 看板这台机器上不想输口令的：`labmon user token <名字> --scope project --out ~/.config/labmon/client.token`。
- 没带令牌、令牌不认（看板回 401）：命令行在看板说的原因后面接一句这两条路（交给用户做，路径按 `LABMON_CLIENT` 取；要管理员的命令指到 `labmon login --admin`；设了 `LABMON_TOKEN` 的说换掉或去掉它），退出码 3（`client.login_hint`，#191）。
- **`project` 令牌做不了的**（403，打一句“这类事要管理员在内网入口做（看板页面上，或 `labmon login --admin` 后再跑）”）：`borrow`、`host return|restore`、`drain|undrain`、`worker start|stop|limit`，推改了 `[[hosts]]`、`[[queues]]` 的配置。实验会话的令牌是 `project` 的，这些交给用户。

### 命令行用的接口

| 请求 | 回 |
|---|---|
| `POST /api/wait {"project", "jobs", "watch", "hold", "extra", "rid"}` | `{"new": {键: 文字}, "jobs": [{"name", "st", "text", "final"}], "watch", "done"}`（`waits.wait`） |
| `POST /api/project/<id>/check {"text"}` | `{"rc", "out"（各主机的探针规格）, "err", "after"（同步状态那行之后的几行）}`；配置不对 400 |
| `POST /api/project/init {"host", "name"}` | `{"rc", "out", "err"}`（`api.project_init`） |
| `POST /api/worker/<动作> {"host", "args", "gpus", "project", "stop"}` | `{"rc", "out", "err"}`；`usage` 是参数错（如 `--project` 不是看板上的项目） |
| `POST /api/override/<动作> {"target", "hosts", "until", "now"}` | `{"rc", "out", "err"}`；`until` 是 epoch 秒（相对写法在命令行那台机器上解析） |
| `POST /api/borrow {"id", "until", "for", "from", "away"}` | `{"rc", "out", "err"}`（`api.borrow`） |
| `POST /api/host/(return\|restore) {"id", "at", "now"}` | `{"rc", "out", "err"}`（`api.host`；`at` 是 epoch 秒，清单里写带年份的） |
| `GET /api/hosts` | `{"missing", "hosts": [{"id", "returned", "ssh", "port", "label", "borrow"}]}`：`labmon host list`、`labmon borrow` 列表用 |

`labmon collect` 用 `POST /p/<id>/collect`，`project archive|restore` 用 `POST /project/archive|restore`（命令行做的记成“经命令行（名字）”），`project push|pull` 用上面[项目配置](#项目配置)的两个接口。
改东西的每条记一行服务日志（谁、做了什么、退出码）。

### `labmon wait` 的长轮询

- 每轮先与看板上的配置同步，再 `POST /api/wait`：第一次 `hold=0`（马上列出等的任务此刻的状态），之后 `hold=25`。看板那边有新告警、或等的任务都有了结果就回，
  否则挂着，相关文件（项目快照、全局快照、`projects.json`、被等的别的项目的快照）变了、或离上次判过 30 秒时再判一次，最多挂 25 秒（小于 TX 上 nginx 的 60 秒超时）。
- “报过没有”照旧记在看板上这个项目输出目录的 `.labmon/wait_state.json`（同一项目几个会话共用）；`--job` 的确认状态（见过它在跑、确认了几份快照……）在 `watch` 里，命令行每轮带回，看板不记会话、重启不丢。
- 请求号 `rid`：回话丢在半路时命令行带同一个号重试，看板 2 分钟内回上次那份，已记成报过的告警不会漏。同时挂着的最多 32 个，多了回 503、命令行退避。
- 连不上看板退避重试；连续连不上超过 max(3×采集间隔, `stale_min`) + 1 分钟（`config.stale_limit`，与 `collector_stale` 同一门槛，按本地 monitor.toml）以新告警 `server_unreachable` 退出（退出码 1）。
- 第一轮推配置就被看板拒（配置不对）照以前“配置错误”退出 3；之后各轮推不上当一条 `sync_error:` 报一次。

## 第一次用

1. 在看板服务主机运行 `labmon user person <人员名>`，再运行 `labmon user add <账号> --person <人员名> --admin`，记下设置码。
2. 打开 `http://127.0.0.1:8765/` → 登录页「用设置码设口令」→ 名字、设置码、新口令 → 进工作台（这个浏览器 30 天不用再登录）。
3. 部署脚本（`tools/deploy.py`）经 HTTP 核对看板，要一个 `deploy` 令牌（管理员的）：`labmon user token <你的名字> --scope deploy --name 部署 --out ~/.config/labmon/deploy.token`；环境变量 `LABMON_TOKEN` 优先。没有令牌、看板不认它时脚本不动手（退出码 2）。
4. 在「组内服务器 → 人员」先登记组内人员，再打开此人详情开通账号，把设置码交给这人。

## 问题报告

- `POST /api/report` 需要 `project` 作用域。JSON 字段 `title`（现象，最多 200 字）和 `evidence`（证据，最多 12000 字）必填，`project`、`host`、`occurred`（发生时刻，epoch 秒）可省略。报告人取登录身份，提交时间由看板记录。普通用户只能指定自己的项目和能看见的主机。成功回 201，含 `report_id` 及命令行输出。备用看板回 409 standby。
- 管理员在「记录 → 问题报告」按报告编号查询原文。记录保留一年，随冷备保存。报告里的文字用于核实问题，提交前去掉口令、令牌及其他秘密。
- `GET /api/reports` 需要管理员的 `reports` 作用域，回 `{"reports":[{"report_id":"R-…","reporter":"账号","t":1794551400}]}`。这个作用域只读报告元数据，可长期使用；`admin` 令牌也可访问。备用看板回 409 standby。

值守用已安装的 `labmon`，沿用本机的 `client.toml`。在看板以服务账号为维护者生成独立令牌，例如 `labmon user token <管理员> --scope reports --name 值守 --out ~/.config/labmon/reports.token`，或从「我的账号」生成同作用域的令牌。在值守所在电脑保存为权限 600 的 `~/.config/labmon/reports.token`，然后在仓库主 checkout 运行 `python3 -I tools/issue_watch.py wait --reports-token ~/.config/labmon/reports.token`。可用 `labmon report --list --token-file ~/.config/labmon/reports.token` 核对配置。未传令牌文件的值守只查 GitHub Issue。

### 管理员公网平台操作（#275）

账号详情「公网平台操作」可按管理员开启或关闭，设置本身从内网入口修改（POST `/api/users/public-ops`，`name`、布尔 `enabled`）。本机也可执行 `labmon user public-ops <账号> on|off`。其余账号默认关闭；降为普通用户会清除此许可，再升管理员仍需重新开启。旧会话及令牌下一次请求生效，令牌仍需 admin 作用域。公网获准操作包括续租、探测、开关机、换人、保活、借还；代码部署、worker代码推送和池管理仍沿原入口限制。维护者部署时按生产实际账号启用，不在代码中按名字自动赋权。

### 算力池成员与资源管理

`GET /api/pool` 返回真实资源概览，关闭派单时也不切回试算。`dispatch_enabled` 表示当前是否接新任务，`nodes` 含供给阶段、持有人、设备和截止，`assignments` 为当前占用摘要（`node, phase, project, task, devices`）；成员不能看到他人的项目或任务名称。`waiting` 为可见等待清单队首（`project, task, phase, reason`），只给管理员或仍拥有项目的提交者；已撤回、已结束清单不计。`inventory_fresh` 按 `observed` 与 `config.STALE_S` 判定。节点 `sample` 仅含现有服务器采样的 `t, state, gpus`，GPU 按供给 UUID 筛选并白名单输出 `i, uuid, name, util, used, total`；含保活负载，无进程、命令与路径。组内样本时刻取 `sample.t`；自用样本沿用采集已校正的 `age`，从看板 `generated` 换算采样时刻，避免将 worker 的远端时刻 `last` 与本机直接比较。过期、掉线、收回或无有效利用率时为 null。管理员额外取得已有绑定人员的账号与可供给服务器候选。

`POST /api/pool/member`（内网管理员）接收 `name` 与布尔 `included`。只允许已绑定人员且拥有个人服务器的账号加入；有待位或未收尾尝试时移出返回 409。此操作与清单提交共用事务锁，防止检查后出现新的提交。它不删除人员、账号、实验产物或历史。

### 项目的运行资源（#267）

- `GET /api/project/<id>/resources`：read + 项目权限。返回 `owner`、本人专属 `private[{id,available,reason}]`、`pool{available,reason,root}` 与项目清单目的地。管理员查看的也是项目主人资源。
- `GET /api/project/<id>/pool`：read + 项目权限。返回这个项目的清单与尝试记录；管理员代管读取项目主人的记录，普通用户只能读自己的项目。写作用域决定撤回/停止按钮，不依赖当前账号的池成员列表。
- `POST /api/project/<id>/queues`：project + 项目权限。正文 `{destination, jobs, request_id, root?}`；`destination` 为 `pool` 或本人池外专属主机 id，专属需绝对 `root`。同编号重试保持同一目的地，不自动回退。
- 日常供给新增 `supply_id` 与 `stage: preparing|ready|releasing|released`；`gate_error` 为需要核对的原因。`ready` 前不计可派资源，`released` 前不解除借用互斥。新看板需配合支持 `pool_supply_gate` 的 worker。

本阶段池成员须有个人服务器与既有共享盘；入池或暂时离线不影响拥有资格，借入不算。资源入口仅登记已有清单，不负责文件传输或环境准备，实验会话沿用 Skill 的共享目录与 tar/SSH 约定。清单切换、供给期间排他与升级步骤见 [项目资源与供给闸门](pool-execution.md#项目资源与供给闸门267)。

### 项目池任务记录（#268）

`GET /api/project/<id>/pool` 复用项目读取权限，返回 `queues`、`active` 与按尝试编号去重的 `attempts`。尝试包含真实节点、GPU UUID、领取/启动/训练结束/清理结束/最后观测时刻和最近30行日志。这些时刻是执行回执，不是预计结束；不通过此接口传代码、结果或配置环境。
