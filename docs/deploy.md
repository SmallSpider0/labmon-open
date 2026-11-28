# 部署到常驻机

本文使用 `dashboard-host` 和 `TX` 表示常驻看板主机与反向代理主机，均为示例名称。按自己的主机与网络配置部署。

## 装看板服务（Linux：systemd）

`labmon service` 在 Linux 上写 systemd 用户单元 `~/.config/systemd/user/labmon.service`（#164；macOS 上照旧是 launchd），命令与参数和 macOS 一样：

- 单元：`Restart=always`、`RestartSec=30`（挂了 30 秒后拉起），标准输出与错误追加到服务日志 `<状态根>/labmon.log`（缺省 `~/.local/state/labmon/labmon.log`，要 systemd ≥ 240），`WantedBy=default.target`。
- 装时 `daemon-reload`、`enable`、`restart`；`labmon service stop` 是 `disable --now` 再删单元。服务参数（端口、公网端口、采集间隔）就存在单元的 `ExecStart=` 里，`labmon service status` 读回来；项目不在参数里，看板服务按登记加载（#161，[workbench.md「项目登记与配置」](workbench.md#项目登记与配置)）。
- 开机自启要这个账号开了 linger（没人登录时用户的 systemd 也起）：`service install` 先看、没开就试 `loginctl enable-linger`，没权限时打一句让维护者跑 `sudo loginctl enable-linger <账号>`。

端口不用缺省的 8765（为什么、用哪个见下面[「看板服务的两个入口」](#看板服务的两个入口server)）。项目在各自目录里 `labmon project push` 登记（#161；笔记本上的项目推到这台的看板，要等 #162 让命令行改走接口）：

```bash
labmon service install --port 8443 --public-port <没人占的端口>
labmon service status            # 状态、登记的项目、入口、日志位置；没开 linger 的会提示
```

## 平台密码

平台密码与平台登录令牌是“秘密”（`platform.secret`）：macOS 存钥匙串（服务名 `labmon-platform`、`labmon-platform-token`，已有的条目接着用），Linux 存 600 权限的文件 `~/.config/labmon/secrets/<种类>/<账号>`（目录 700）。

密码只由维护者在看板服务那台机器上亲手放一次，输入不回显、不进命令行参数：

```bash
labmon secret set platform [--user 账号]   # 账号缺省取主机清单里的平台账号；输两遍
labmon secret rm platform [--user 账号]    # 删掉（连同存着的平台登录令牌）
```

代码只经 `platform.password` 取（平台操作登录时，RSA 加密后只发给平台），不打印、不写日志；没放过时平台操作停在登录那一步，说清要跑这条命令。

## 部署

部署脚本 `tools/deploy.py` 在笔记本的主 checkout 里跑，两种模式（[AGENTS.md「部署到本机看板」](../AGENTS.md#部署到本机看板)）：

- **本机模式**（缺省，搬家前一直用它；现在只在主回退到笔记本时用：没有 `deploy.toml`、不给 `--to`）：看板服务就在这台，pull 主 checkout 本身、重启服务、读本机日志。
- **远端模式**：写了 `~/.config/labmon/deploy.toml` 或给了 `--to` 时。脚本不 ssh：确认本地 main 与 `origin/main` 一致，按服务端 checkout 现在的提交打增量 git bundle（服务端的提交本地没有就打整份），经 `POST /api/admin/deploy` 传过去；服务端校验、快进，改了代码的等平台操作做完后自己重启（[api.md「部署接口」](api.md#部署接口)）。脚本等 `GET /api/me` 报出新提交，采集两次（响应体未收全时，该次采集重试一次），改了 worker 的逐台 `POST /api/worker/start`，核对与本机模式同一套（平台操作与日志 Traceback 读 `GET /api/admin/deploy`）。

两种模式核对前都先等结果落地（#70）：最后一次采集后，快照早于采集请求（采集超时）、或拉起成了的主机状态还早于拉起（新 worker 还没写出第一份），就每 10 秒再带 `fresh` 采一次，最多等 2 分钟（`SETTLE_S`）；等不到照样核对，由核对报没过。

全部核对通过且本次有 `labmon/` 或 `skills/` 改动时（纯文档、测试、工具变动不打扰），脚本会自动调用 `POST /api/deploy/tell` 经 `records.tell` 向所有未归档的活跃项目发一条简短部署提示（含目标 commit、最多 5 条提交摘要、是否重启看板/更新 worker/更新 skill 或 CLI），让挂着 `labmon wait` 的实验会话及时知晓；支持 `--no-notify` 关闭或 `--tell <项目>` 定向通知，通知失败仅打警告、不影响已成功的部署退出码（#275）。

```toml
# ~/.config/labmon/deploy.toml（笔记本上；迁移那天写，#171）
url = "https://<dashboard-host 内网 IP>:8443/"
ca = "~/.config/labmon/ca.crt"     # 内网入口证书的 CA（下面「看板服务的两个入口」那个小 CA 的 ca.crt）：只信它，不看主机名
```

部署令牌照旧是 `~/.config/labmon/deploy.token`（看板服务那台上的管理员建的 `deploy` 令牌）。

常驻机上那份 checkout 只放 main、不 pull GitHub（它没有 GitHub 的凭据）：第一次从笔记本带过去的 bundle 克隆，之后只收部署脚本传来的 bundle。

```bash
# 笔记本：
git -C <主 checkout> bundle create /tmp/labmon-main.bundle main
# 拷到常驻机后：
git clone -b main /tmp/labmon-main.bundle ~/labmon && git -C ~/labmon remote remove origin
```

### 手工部署（脚本跑不了时）

部署脚本本身出错、跑不了时，部署 agent 照这里手工走七步（谁做、怎么派见 [AGENTS.md「部署到本机看板」](../AGENTS.md#部署到本机看板)）。脚本做的是同一件事（第 1–5、7 步），判断（哪些文件不用重启、核对的门槛）照这里写的。
看板要登录（#160）：下面的 `curl` 都带部署令牌（`-H "Authorization: Bearer $(cat ~/.config/labmon/deploy.token)"`）。

看板服务那份 checkout 前进本身就是部署的一部分（远端模式是 dashboard-host 上的 `~/labmon` 快进：它只放 main、不连 GitHub，只收部署传来的 bundle；本机模式是主 checkout 的 `git pull`）：
一落地，后台采集下一轮就用新模板出页面（`collect.write_page` 每次从磁盘读），续租收尾、借用、看板自动拉起与 `POST /api/worker/start` 推上去的也都是新 worker（`workerctl.committed` 读那份 checkout 的 main）。
所以前进之前先确认没有平台操作，前进之后紧接着重启，中间不停（远端模式下服务端收 bundle 时自己查、自己等）。下面的命令先设好这两个（本机模式括号里写了不同处）：

```bash
URL=https://<dashboard-host 内网 IP>:8443   # deploy.toml 的 url，不带末尾的 /（本机模式：http://127.0.0.1:8765）
C=(--cacert ~/.config/labmon/ca.crt -H "Authorization: Bearer $(cat ~/.config/labmon/deploy.token)")   # 本机模式不要 --cacert 那一项
```

1. 确认没有进行中的平台操作：`curl -s "${C[@]}" "$URL/api/admin/deploy"` 的 `busy` 为空（本机模式：`fleetops.busy()`，或 `"$URL/fleet/op.json"` 的 `op.active`），有就等它结束再更新代码。
2. 更新代码：主 checkout 里 `git pull --ff-only`，确认 `main` 与 `origin/main` 一致（只部署推到 GitHub 的 main）；再从第 1 步回话的 `head`（dashboard-host 那份 checkout 现在的提交）到 main 打 bundle 传上去。
   回话的 `commits`、`files`、`restart` 是这次带上去的提交、改了哪些文件、要不要重启；不部署时回 409 与原因（不是快进、它那份 checkout 有改动、平台操作在做）。本机模式只做 `git pull --ff-only`。
   ```bash
   git bundle create /tmp/labmon-deploy.bundle <head>..main
   python3 -c 'import base64,json,sys; print(json.dumps({"bundle": base64.b64encode(open(sys.argv[1],"rb").read()).decode()}))' /tmp/labmon-deploy.bundle > /tmp/labmon-deploy.json
   curl -s "${C[@]}" -X POST -H 'Content-Type: application/json' --data-binary @/tmp/labmon-deploy.json "$URL/api/admin/deploy"
   ```
3. 重启服务。只动了文档、测试或工具时不重启：改的文件全在 `*.md`、`docs/`、`skills/`、`.github/`、`tests/`、`tools/`、`.claude/` 里才算（`deploy.is_doc`；看板服务与 worker 都不 import 这些；`labmon/*.html`、`labmon/*.js` 与 `labmon/overview_js/*.js` 是页面模板，不算）。
   这时第 3、4 步跳过，第 1、6、7 步照做（核对清单里“快照时刻晚于第 4、5 步”这条不用看），在部署报告里写明。
   远端模式由服务端自己重启（等平台操作做完再交给 systemd）：等 `curl -s "${C[@]}" "$URL/api/me"` 的 `commit` 是新提交、`started` 变了。本机模式：`git diff --stat ORIG_HEAD HEAD` 看改了哪些文件，要重启的 `labmon service restart`。
4. `curl -s "${C[@]}" -X POST -H 'Content-Type: application/json' -d '{}' "$URL/collect?fresh"` 两次（看板只认带 JSON 类型的 POST，#158；要登录，#160）。页面在采集时才按模板重新生成；带 `fresh` 才不被 10 秒的节流挡掉，等请求之后开始的那一轮做完。
5. 改了 worker（`labmon/labmon_worker.py` 或 `labmon/worker/`，`deploy.has_worker`）的，各在用主机（没 `worker stop` 过、连得上的）`LABMON_TOKEN=$(cat ~/.config/labmon/deploy.token) labmon worker start <主机>`（单条 ssh 打包部署入口与 `worker/` 包并接管任务、不中断；命令行按 `~/.config/labmon/client.toml` 连看板、经 `POST /api/worker/start`，#162），然后再 `POST /collect?fresh` 一次，下面的核对才读得到拉起之后的状态。
   部署的是看板那份 checkout 里 main 上已提交的版本（`workerctl.committed` / `worker_bundle`），分支上没合并的、工作区里没提交的都不会带上去。
6. 这次带上去的提交改了 `docs/workflow.html` 或 `docs/intro.html` 的，按[流程总览](../AGENTS.md#流程总览)、[项目介绍页](../AGENTS.md#项目介绍页)核对随源码分发的 HTML 文档（脚本不做，结论里点名）。
7. 按下面的清单核对。

### 部署后的核对

部署后逐项核对（脚本与手工同一套），有一项不过就不报完成，查原因或回退：

- 没有进行中的平台操作（照第 1 步看）；服务日志重启以来（没重启就是最后一次「服务启动」以来）没有 Traceback：远端模式看 `GET /api/admin/deploy` 的 `tracebacks`（日志在 dashboard-host 的 `<状态根>/labmon.log`），本机模式看 `~/Library/Logs/labmon.log`。
- 各主机的 worker 在写状态：按 `overview.json` 看，不只看拉起命令的输出（曾经远端最后一句的退出码恒为 0，worker 启动即崩也报“已拉起”，5497dd9）。
  ```bash
  curl -s "${C[@]}" "$URL/overview.json" | python3 -c 'import json,sys,time; d=json.load(sys.stdin); hm=lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else "—"; print("快照", hm(d.get("generated"))); [print(h["id"], h.get("worker_up"), round(h.get("age") or -1), hm((h.get("status") or {}).get("t")), h.get("stopped") or "", h.get("error") or "") for h in d["hosts"]]'
  ```
  快照时刻晚于第 4、5 步；在用的主机 `worker_up` 为真、`age`（距状态里的 `t`）不超过 90 秒；`worker stop` 过、已归还的除外。
  跑了 `worker start` 的，打印出的 `t` 要晚于拉起的时刻。
- 页面渲染不报错：脚本把看板上的工作台（`/`、`overview.json`、`records.json`）与各项目页下载下来，用 `tests/pages/smoke.js` 在 node 里装进 `tests/pages` 的 DOM 桩，
  按路由渲染（`#/`、每台主机、`#/log`、`#/fleet` 与它的选项卡、每台组内服务器的抽屉、每个项目页），不抛异常、文字里不漏 NaN/undefined/Infinity
  （任务日志原文里本来就有的不算，看结论附的上下文判断）。手工时在内置浏览器里打开 `#/`、`#/host/<id>`、`#/p/<id>`、`#/fleet`，`read_console_messages` 没有报错。
- 改了页面模板（`labmon/*.html`、`labmon/*.js` 与 `labmon/overview_js/*.js`，`deploy.is_page`）的，另在内置浏览器里打开真看板核对改动处，按[清理规则](../AGENTS.md#怎么测)关掉自己开的标签，并在部署报告里写明结果。没改页面的批不用开浏览器。
  内网入口用的是 labmon 自签 CA 签的证书：桌面应用所在的电脑要先信任这个 CA（`~/.config/labmon/ca.crt`），否则内置浏览器打不开；没信任时在部署报告里写明没看，以部署脚本的冒烟检查（上一条）为准。

## 看板服务所在机的体检

体检本身怎么判、页面上怎么显示见 [workbench.md「看板服务所在机的体检」](workbench.md#看板服务所在机的体检)（#166）。服务跑在 WSL 里就自动启用，不用配置。装好服务后由维护者在 dashboard-host 上核对：

1. 以服务账号在 WSL 里跑 `labmon hostcheck`，看“待装更新”一行：
   - 写着“N 个（安全更新 M 个），离线搜索 …”：服务账号能做离线搜索，不用做第 2 步；
   - 写着“读不了”、出错是拒绝访问一类：服务账号做不了离线搜索，做第 2 步。
   - “开机”“Windows 内存”读不了的，多半是 interop 没开或 `powershell.exe` 不在缺省位置（`/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe`），原因写在“出错”一行。
2. （只在第 1 步读不出更新时）在 Windows 上用“以管理员身份运行”的 PowerShell 跑仓库里的 `tools/win/install-wu-task.ps1`（与 `labmon-wu.ps1` 放在同一目录）：
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File install-wu-task.ps1
   ```
   它把 `labmon-wu.ps1` 拷到 `C:\ProgramData\labmon\`（只许 SYSTEM 与管理员改、普通用户只读），建计划任务 `labmon-wu`（SYSTEM，开机时与每 6 小时，写 `C:\ProgramData\labmon\wu.json`）并马上跑一次。
   那个目录已经在、却不是 SYSTEM 或管理员建的（或里面有别人的文件、子目录、链接）时它报错停下：普通用户先建的目录，所有者能把权限改回来、换掉 SYSTEM 要跑的脚本。照提示看清里面是什么、删掉再跑。
   一两分钟后再跑 `labmon hostcheck`，“待装更新”一行写“来自计划任务”。改了 `labmon-wu.ps1` 后再跑一次安装脚本就是更新；卸载是 `Unregister-ScheduledTask labmon-wu` 并删掉那个目录。
3. 把 Windows 更新改成通知后手动装（#168 第 1 步）以后，卡上“更新方式”一格写“手动装”；还是“自动装”（标黄）说明组策略没生效。
4. 隧道单元 `labmon-tunnel.service` 装好（[下文](#tx公网入口与看门狗)）之前，“隧道”一格写“没装”，不报。

装更新、重启都在白天做的话会收到体检提醒（如隧道断了 15 分钟以上）：先开[计划维护](#计划维护)。

## 向普通用户开放主机

可选的 `[server].min_client_commit` 设定最低客户端版本。默认不开启。启用时填完整的 40 位小写提交号，且必须是看板运行提交或其祖先，重启后生效。先在人员详情的「账号与权限」确认客户端已更新，再抬高门槛。看板仍提供登录和下载更新入口。

普通用户通过远端 SSH 主机登记任务。升级后，管理员在开放的主机上用 `labmon worker start <主机>` 更新 worker，再开放普通用户的登记操作。旧 worker 无法核对登记归属，普通用户的登记增删、搬动和归档会被拒绝。

## 看板服务的两个入口（`[server]`）

内网入口绑哪、HTTPS 证书、两个入口各认哪些 `Host` 写在用户级主机清单的 `[server]` 段，写法与校验见 [api.md「`[server]` 段」](api.md#server-段)（#165）。
端口只在命令行给（`labmon service install --port P --public-port Q`）：dashboard-host 上 Windows 的 `127.0.0.1:8765` 被别的程序占着、WSL 是 mirrored 网络绑不上，内网入口用 8443（防火墙只放入站 TCP 8443、来源 192.0.2.0/24），公网入口另挑一个没人占的端口、只绑回环（#168）。
下文的 `LABMON_PORT` 都指公网入口的端口。

```toml
[server]
lan_bind = "0.0.0.0"
tls_cert = "tls/dashboard-host.crt"
tls_key  = "tls/dashboard-host.key"
ca_cert  = "tls/ca.crt"            # dashboard-host 的私有 CA 公钥，内网客户端安装包随附
lan_hosts = ["<dashboard-host 内网 IP>:8443"]
public_hosts = ["<TX 公网 IP>"]
```

内网入口的证书与下面 TX 的客户端证书用同一个小 CA 签（维护者的机器上做，CA 私钥不离开那台）：

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 -subj "/CN=labmon CA" -keyout ca.key -out ca.crt
# dashboard-host 内网入口的服务器证书（IP 写进 subjectAltName，浏览器按它核对）
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=dashboard-host" -keyout dashboard-host.key -out dashboard-host.csr
openssl x509 -req -in dashboard-host.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -out dashboard-host.crt -extfile <(printf "subjectAltName=IP:<dashboard-host 内网 IP>\nextendedKeyUsage=serverAuth")
# 每台要从公网用的设备一张客户端证书（p12 带口令，导进手机与浏览器）
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=<设备名>" -keyout dev.key -out dev.csr
openssl x509 -req -in dev.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -out dev.crt -extfile <(printf "extendedKeyUsage=clientAuth")
openssl pkcs12 -export -inkey dev.key -in dev.crt -certfile ca.crt -out dev.p12
```

`ca.crt` 装进各设备的信任列表（内网入口的 HTTPS 才不报警）并拷到 TX 的 `/etc/nginx/labmon-ca.crt`；`dashboard-host.crt`、`dashboard-host.key` 放到 dashboard-host 上 `[server]` 指的位置（私钥 600、属服务账号）。

dashboard-host 还需将同一份 `ca.crt` 公钥复制到 `[server].ca_cert` 指定的位置，确保看板服务用户可读。相对路径以运行用户的主机清单目录为准。安装下载接口每次请求都读取该项，只补这项及其文件无需重启。CA 私钥仍只留在签发机器上。

核对时从生产内网「我的账号 → 命令行」重新下载安装脚本，在干净的用户目录运行。确认生成的 `client.toml` 已填 `ca`，对应文件与现有 CA 公钥一致，再用这次安装的命令运行 `labmon login` 和 `labmon host list`。可以先用未登录的 `labmon host list` 验证能收到看板的登录提示，随后使用获授权的验收账号验证成功。浏览器信任与命令行信任分别核对。

### 给新用户签客户端证书

管理员在保存 CA 的机器上按上面的客户端命令为每台设备签发 `clientAuth` 证书，CN 可用看板账号和设备名（例如 `alice-laptop`）。通过可信渠道交付带口令的 `p12`，供浏览器或手机导入。命令行从 TX 访问时，`client.toml` 对应入口的 `cert` 和 `key` 填这台设备的 PEM 证书和私钥路径。私钥权限设为 600。

要撤销某张证书，若 CA 维护了签发数据库，用原 CA 配置执行 `openssl ca -config <CA配置> -revoke <客户端证书.pem>`，再用 `openssl ca -config <CA配置> -gencrl -out labmon-ca.crl` 生成吊销清单。把清单放到 TX，在对应 nginx 的 `server` 块加 `ssl_crl /etc/nginx/labmon-ca.crl;`，通过 `nginx -t` 后 reload。若原先只用上面的 `openssl x509 -req` 签发且没有 CA 数据库，可更换 CA，并为仍获准的设备和内网入口重新签发证书。

## TX：公网入口与看门狗

公网访问走 `https://<TX>/<前缀>/` → TX 的 nginx（mTLS、限速）→ TX 本机 `127.0.0.1:LABMON_PORT` → 反向隧道 → dashboard-host 公网入口 `127.0.0.1:LABMON_PORT`。
模板都在仓库里，装的时候把占位 sed 换掉：`LABMON_PREFIX`（随机前缀，`openssl rand -hex 12`）、`LABMON_PORT`、`LABMON_TUNNEL_USER`（TX 上的隧道账号）、`TX_HOST`。
隧道两头必须是同一个端口：看门狗探 `/healthz` 带的 `Host` 是 `127.0.0.1:<TX 那头的端口>`，公网入口只认自己的端口。

1. **隧道账号**（TX）：`useradd -m -s /usr/sbin/nologin LABMON_TUNNEL_USER`，不设口令；`tools/tx/sshd-labmon.conf` 放进 `/etc/ssh/sshd_config.d/labmon.conf`，`authorized_keys` 照它注释里那行写（`restrict,port-forwarding,permitlisten=…`），`sshd -t` 后 reload。
2. **隧道单元**（dashboard-host 的 WSL，服务账号）：`ssh-keygen -t ed25519 -N '' -f ~/.ssh/labmon-tunnel`，公钥给第 1 步；`ssh-keyscan TX_HOST > ~/.ssh/labmon-tunnel.known_hosts` 后核对指纹；
   `tools/wsl/labmon-tunnel.service` 拷到 `~/.config/systemd/user/`，`systemctl --user enable --now labmon-tunnel`（要 `loginctl enable-linger` 才开机即起）。名字不要改：体检按它看隧道断没断（总览「看板服务」小卡的“隧道”一格）。
3. **nginx**（TX）：`tools/tx/nginx-labmon.conf` 分三段放：http 级的两个 `limit_req_zone`；现有 443 `server` 块里加 `ssl_client_certificate`、`ssl_verify_client optional` 两行（nginx 只认 server 级）；同一个 `server` 块 `include` 两个 `location`（限速返回码 `limit_req_status 429` 写在 labmon 的 `location` 里，不放 http 级：那会改到同一台上别的站）。
   `nginx -t` 后 reload。浏览器开 `https://<TX>/<前缀>`：没有客户端证书 403，有证书跳到登录页。`[server]` 的 `public_hosts` 写 TX 的 IP 或域名（nginx 原样传 `Host`）。
4. **fail2ban**（TX）：`tools/tx/fail2ban-labmon.conf` 拆成 filter 与 jail 两个文件（注释里写着放哪），日志路径按 TX 的 nginx 改（jail 里的 `backend = auto` 不能删：Debian/Ubuntu 缺省读 journal）；10 分钟里同一 IP 被拒 10 次封 1 小时。
5. **看门狗**（TX，要 Python 3.11 起，TX 自带 3.12）：
   ```bash
   install -d /opt/labmon-watch /etc/labmon-watch
   install -m 644 tools/tx/labmon-watch.py labmon/push.py /opt/labmon-watch/
   install -m 600 notify.toml /etc/labmon-watch/notify.toml       # 与 dashboard-host 上 ~/.config/labmon/notify.toml 同一种写法（Bark 或 ntfy）
   sed 's/LABMON_PORT/<端口>/' tools/tx/labmon-watch.service > /etc/systemd/system/labmon-watch.service
   install -m 644 tools/tx/labmon-watch.timer /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now labmon-watch.timer
   systemctl start labmon-watch.service && journalctl -u labmon-watch -n 5     # 立即探一次：应写“正常”
   ```
   每 5 分钟探一次 `/healthz`：连不上、`primary` 为 `false`、最后一次采集超过 15 分钟，持续 15 分钟推一条（哪一种、从几点起），恢复再推一条；异常中途换了种类不重推；推送失败下一轮再推。
   这台还是备用时（`/healthz` 的 `primary` 为 `false`）先不启用定时器：上面 `enable --now` 那句先不跑（手动探一次照样可以，写的是“不是主”），否则 15 分钟后就推“不是主”；当上主那天再 `systemctl enable --now labmon-watch.timer`（#170）。
   改了仓库里的 `labmon-watch.py` 或 `push.py`，重拷这两个文件即可（下一轮就用新的）。
6. **核对**：dashboard-host 上 `systemctl --user stop labmon-tunnel`，15 分钟后手机收到“连不上”，再 start 收到“已恢复”；先开计划维护（下节）、等看门狗探到再停，就不推。

## 在看板主机上手工操作

平常部署走 `tools/deploy.py` 的远端模式，不用 ssh（[部署](#部署)）。ssh 只用于部署接口做不了的维护，比如挪走状态目录里的旧文件。

- dashboard-host 是组里共用的一台 Windows 机。看板服务跑在专用 Windows 账号（`<服务账号>`）自己的 WSL 里：systemd 用户单元 `labmon`，状态根是这个 WSL 里的 `~/.local/state/labmon`，代码在 `~/labmon`。
- WSL 每个 Windows 账号各有一套。用别的账号 ssh 进去再执行 `wsl`，进的是那个账号自己的 WSL：看不到看板服务，那套 WSL 没在跑的话还会被这次连接拉起来。要进服务的 WSL，必须以 `<服务账号>` 的身份 ssh 登录。
- `<服务账号>` 不是管理员，公钥放在 `C:\Users\<服务账号>\.ssh\authorized_keys`；`<管理员账号>` 的公钥放在 `C:\ProgramData\ssh\administrators_authorized_keys`。
- Windows 的 ssh 外壳是 cmd，引号很难写对，输出还是 GBK。远程跑 bash 时把脚本从标准输入喂进去：`ssh <服务账号>@dashboard-host 'wsl -e bash -s' < 脚本.sh`；跑 PowerShell 用 `powershell -NoProfile -EncodedCommand <UTF-16LE 的 base64>`。
- 排查端口时，mirrored 网络下 WSL 里监听的 8443 在 Windows 的 `netstat` 与 `Get-NetTCPConnection` 里都看不到；Windows 回环上的 8765 是别的程序占着的（见[「看板服务的两个入口」](#看板服务的两个入口server)），别把它当成看板。探活用 `/healthz`。
- 要改状态目录里的文件，先挪进状态根外面带日期的备份目录，不直接删，也不留在状态根里：冷备会把状态根整个打包（[冷备与恢复](#冷备与恢复)）。

## 计划维护

装更新、重启 dashboard-host、改隧道之前：`labmon server maint 30m`（或 `2h`、`18:30`、`明天 9:00`；最长 1 天，到期自动结束；`labmon server maint off` 提前结束，不给参数看现在）。
**开了以后等 5 分钟再停服务、断隧道或重启**：TX 的看门狗每 5 分钟探一次 `/healthz` 才知道在维护；在 TX 上 `journalctl -u labmon-watch -n 1` 写着“计划维护到 …（不报）”就是知道了。
期间体检不推、TX 的看门狗不报；维护结束后还异常的，看门狗从结束起再算 15 分钟（[workbench.md「看板服务所在机的体检」](workbench.md#看板服务所在机的体检)）。

## 主与备用（服务租约）

同一时间只能有一个看板服务动 worker 的输入、平台与组内服务器（#163，`labmon/lease.py`）。每个 `worker_home` 下有一份服务租约 `labmon_server.json`：`{holder 持有的看板服务, renew 最后续约, ttl 600 秒}`，时刻按共享盘的时钟（改锁文件的修改时刻再读回：同一个 `worker_home` 下几台的时钟未必一样）。

- **续约**夹在工作台每轮采集的那条 ssh 里（`overview.fetch`）：先在 `flock` 里读改写租约，这一刻是自己的才写 `eta.json`、`sched.json`、`returned.json`。没人持有、释放了、或过了 ttl 没续的就拿下。
- **主**：每个 `worker_home` 的租约都是自己的。**备用**：有一个在别人手里，或写坏了。读不到的（主机都连不上、本机不在内网）照本机标记（`server.json` 的 `primary`），不因为超过 ttl 没续上就降为备用：只有一台主机、它租期到了停机时，降成备用就续不了租。
- 备用时什么都不动，每轮只读一次租约、能拿就拿：不采集（不写快照）、不探组内服务器、不做平台操作（不登录平台）、不改 worker（`worker start|stop|add|rm|limit`、临时指令、借用）、不发通知、不推体检提醒；这台上的命令行同样（`labmon collect`、`labmon fleet probe`、改 worker 与临时指令的命令都说一句主是谁、不做）；看板上除登录、账号与部署外的写请求（POST、推项目配置的 PUT）回 409，说主是谁、最晚何时能接管。页面顶上一条“备用”横幅，`/healthz` 的 `primary` 为 `false`（TX 的看门狗按它报“不是主”）。
- 两台各拿了几个 `worker_home`（分开了）：id 大的放掉手上的，id 小的下一轮全拿到。
- **停服务时释放**：`labmon service stop|restart`、systemd 停服务、`kill` 都发 SIGTERM，服务等这一轮采集做完、把自己的租约清空再退出，备用的下一轮（约 1 分钟）就接管；被硬杀、断电的要等 ttl（10 分钟）。`labmon service stop` 另把本机标记置为备用。有备用待着时重启主（部署、`service restart`），重启那几秒里备用正好读到就会接走，这时原来的主起来后以备用待着；只跑一个看板服务时没有这回事。
- 租约文件写坏了：两边都不自动抢，备用推一条通知。确认没有别的看板服务在跑后，在要当主的那台上 `labmon server lease --take`。

```bash
labmon server lease              # 这台是主还是备用；各 worker_home 的租约谁持有、最后续约
labmon server lease --take       # 强制拿到（写坏了、或确认另一台已经停了）；本机标记置为主
labmon server lease --release    # 释放（是这台的才清空）；本机标记不动
```

## 冷备与恢复

笔记本只当冷备（#163，`labmon/backup.py`）：每天经 API 取一份看板服务那台的状态，留 14 份，需要时一条命令在笔记本（或新机器）上恢复成看板服务。

- **包里有**：状态根下的全部（快照、记录、用量、组内服务器的状态、账号库 `users.json`、`projects.json` 等）、主机清单 `hosts.toml`，与 `manifest.json`（何时、哪台、哪个提交、服务里的项目配置路径）。
- 备份文件与笔记本上的备份目录只给自己读（600、700）：里面有账号库的哈希。
- **不含秘密**：平台密码与登录令牌、推送配置 `notify.toml` 与 `notify.d/`、ssh 私钥、证书私钥都不在状态根里，不进包；账号库只有口令与令牌的哈希。本机身份 `server.json`、服务日志、锁与临时文件也不进。每台机器的秘密各自放一份（[平台密码](#平台密码)）。
- **取**：经内网入口取（`GET /api/admin/backup` 只在内网入口，公网入口回 404，#17）。看板服务那台的管理员建一个 `backup` 令牌，拷到笔记本：

```bash
# 看板服务那台：
labmon user token <管理员名字> --scope backup --out /tmp/backup.token
# 笔记本（令牌放 ~/.config/labmon/backup.token，600 权限；LABMON_TOKEN 优先）：
labmon backup pull --to https://<dashboard-host 内网 IP>:8443/ --ca ~/.config/labmon/ca.crt     # 取一份试试；连不上就跳过、退出码 0
labmon backup schedule --to https://<dashboard-host 内网 IP>:8443/ --ca ~/.config/labmon/ca.crt  # 每天 12:30 取一次（--at 改；睡着错过的醒来补）；--off 去掉
labmon backup list                                                                    # 存着的几份（~/Library/Application Support/labmon/backups/）
labmon backup make --out <目录>                                                        # 在看板服务那台本机直接打一份
```

- **恢复**：先停这台的服务（装着的拒绝）。状态根与主机清单原来的整个挪到旁边 `<原名>.before-restore-<时刻>`（不删），解出备份（文件照包里的权限，账号库仍是 600），`server.json` 保留这台原来的身份、本机标记置为备用；主机清单照备份，只有 `[server]` 段（[两个入口](#看板服务的两个入口server)：绑哪、证书、认哪些 `Host`，每台各一份）留这台原来的：这台原来没有就不带备份里的（那是打包那台的入口，证书路径指着那台的文件），这台原来的写坏了、合不出能解析的就在挪动任何东西之前停下（#197）；接着装上服务（同 `labmon service install`：项目的登记与配置在状态根的 `projects/` 下，随备份一起回来）。服务起来先读一次租约、定了主备才起各线程：原来的主还在续约，这台就以备用待着，它停了（释放或过 ttl）才接管；读不到租约（没有在用主机、不在内网）时一直是备用，确认原来那台已经停了再 `labmon server lease --take`。

```bash
labmon server restore ~/Library/Application\ Support/labmon/backups/labmon-backup-<时刻>.tar.gz --port 8765 [--public-port Q]   # 恢复完接着装服务
```

- **交接**（把主换到另一台）：新的那台恢复最近一份备份、装服务（备用待着）→ 旧的那台 `labmon service stop`（释放租约）→ 新的下一轮接管。回退同样：在旧的那台再恢复、装服务，停新的那台（两头的 `[server]` 各留各的，不用手工改主机清单）。

## 公版部署路径

主机清单中的 `data_root` 指定获授权的共享目录，缺省 `/data/labmon`；`worker_home` 缺省为其下 `labmon/`。已有安装升级前，应显式写入实际使用的 `data_root` 与 `worker_home`，避免依赖曾经的站点默认值。

可选 `ka_bin` 指定现有 `ka.env`（保活的配置）所在目录；它经 `KA_BIN` 传给 worker，借来的主机继承既有主机模板中的这一项。未配置时 worker 查找自身目录下的 `keepalive/bin`；没有 `ka.env` 时 worker 不管保活，也不会替部署方创建保活。停启保活的 `ka.sh` 由 labmon 随 worker 发到 `worker_home`（[fleet.md「ka.sh」](fleet.md#kash)，#105），`ka_bin` 里原有的那份不再由 worker 调用。既有 `ka.env` 的 `AGRI_ROOT`、`AGRI_KA_DIR`、`AGRI_KA_SCRIPT` 接口仍受支持。

仓库等待默认指向公开仓库，可用 `python3.11 -I tools/issue_watch.py --repo owner/name wait` 覆盖。各仓库的已见编号分别放在 `<状态根>/issue_watch/<owner>--<name>.json`，不会把旧库的同号 Issue 当成已见；新空库可直接启动等待。
