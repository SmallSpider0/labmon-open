# 影响清单：改一处要对照的维度

第 3、4 档的改动（新功能、改行为的修复等，分档见 [AGENTS.md「怎么开发」](../AGENTS.md#怎么开发)）在动手前写设计说明，按下面 14 个维度写影响：
第 4 档逐个写；第 3 档只展开涉及的维度，其余一行写“无关：维度号 + 理由”。第 2 档不写设计说明，只写“根因 + 改法”评论，影响清单在 PR 正文里勾。
设计说明写在 Issue 评论里；里面的问题分自己定、按推荐先做、必须等三类，只有“必须等”的等维护者定了再写那部分代码
（[AGENTS.md「事先同意的事与要等维护者的事」](../AGENTS.md#事先同意的事与要等维护者的事)）。审查按同一张清单核对（[AGENTS.md「审查要点」](../AGENTS.md#审查要点)）。
模板在[文末](#设计说明模板)。

每个维度列：要问的问题（用 labmon 自己的概念与函数名）、相关代码、历史上漏过的例子。函数名后面不写文件的是 `labmon_worker.py` 里的，
其余写 `模块.函数`；概念的唯一实现以 [唯一实现表](concepts.md) 为准。

## 1. 多主机

几台主机的 worker 共用共享盘上同一个 `worker_home`，各写 `hosts/<主机>/` 下自己的 `status.json`、`usage.jsonl`，互相读。

- 别的主机要不要知道这件事？要的话写进 `write_status` 的哪个字段，`peers` 读不读得到（`share.run`、`plan` → `peer_end`、`todo` → `peer_todo`）。
- 那台 worker 不在时怎么算：状态过了 `STATUS_STALE_S` 还算不算；续租、重启的一天内（`DEP_STALE_S`）照样算吗；`worker stop` 过（`stopped`，含借用归还）的呢。
- 过期一律看状态里那台自己写的 `t`，不看文件修改时间（时钟偏差见[第 7 维](#7-主机与项目的进出)）。
- 用量记录：各主机的 `usage.jsonl` 由 `load_hist` / `reload_hist` 一起读，新加的字段各台口径一致吗；公平份额（`share`）是不是跨主机算。
- 看板这一侧：`overview.collect` 读各主机状态、`config.STALE_S` 与 worker 同一口径；共享盘扫描只由拿着租约的那台做（`Disk`）。
- 测试：用多主机夹具 `tests/fixtures.Hosts`（共享 HOME 的几台 worker、别的主机的假状态、推进时钟）写一条用例。

相关代码：`peers`、`write_status`、`dep_state`、`load_hist`、`share`、`own_runs`；`overview.collect`、`overview.fetch`。

漏过的：
- 占位任务与 `after=` 依赖最初只看本机队列：别的主机上的被等任务排在时长不明的任务后面、所在清单暂停、那台续租重启时都看不见，
  成环没人报，被挡住的独占任务还占着留卡（023774e 才补上：各台把还会跑的写进 `todo`，别的主机按它认）。
- 别的主机算不算过期曾看 `status.json` 的修改时间，共享盘时钟慢 3.5 分钟，各台互相当对方不在，跨主机的公平份额一直没算上（11-07）。
- “此刻哪里都看不到”不等于不存在：别的主机的状态按 `PEER_EVERY` 缓存，被等的那台也要下一轮 tick 才认出自己清单里的新行。
  两边清单同时加行时，跨主机的 `after=` 曾被马上报成找不到，几十秒后自己恢复，实验会话却已据此挪了行（#56；改成持续满 `DEP_MISS_S` 才报）。

## 2. 同机多项目

- 按项目 id（`cfg["id"]`，即 `worker add --project`）认，不按目录猜；新加的东西是按项目、按清单还是按主机生效。
- 公平份额（`share`、`card_share`，权重 `policy` 来自 `[sched]`）、`focus` 与放行顺序 `admit` 受不受影响；一个项目的状态会不会挡住别的项目。
- 同 tag 的上限整台主机共用、各项目合并计数（`tag_full`）；两条队列不能共用状态目录（`qslug`）。
- 项目暂时没实验（`empty`、`no_work_since`）时这件事还报不报。

相关代码：`admit`、`share`、`policy`、`tag_full`、`qslug`；`config.load`、`api.unknown_project`、`collect.build`。

漏过的：
- 屏障曾排到所有不是 focus 的请求前面，整台主机都停下等它；放不下的请求也成屏障，一等满 30 分钟就一直挡着别的项目（11-06 → 11-07 改成只拦会耽误它的）。
- 项目暂停时它的独占任务留着的卡，别的项目也用不上（023774e）。

## 3. 多卡

- 一台几张卡：这件事按卡算还是按整机算；放在哪张卡上（`place` 里的 `card_free`、`card_need`），几张都放得下时先挑哪张。
- 一个任务几张卡（`gpus=N`）：显存估计与历史（`estimate`、`need_mb`）是每张卡的还是合计；份额按占几张卡算（`card_share`、用量行的 `cards`）。
- 保活看整机：一台只要一张卡开着保活就算保活（平台按整机判空闲）。要卡时停、空闲时开（`keepalive`、`ka`）；只用了部分卡时要不要保活、
  组内服务器上一张卡有保活其余空着算什么（`fleet.classify`、`fleet.guard_state`）。

相关代码：`place`、`card_free`、`card_need`、`need_mb`、`card_share`、`keepalive`、`ka`；`fleet.classify`、`fleet.guard_state`。

漏过的：
- 借来的主机上独占卡只看卡上有没有进程，不扣给对方的预留；多卡时也不先挑没给对方留的卡（f1dfff6）。
- 设计时以为每张卡都要开保活，维护者纠正：一张卡开着整机就算保活（11-06）。

## 4. 借来的主机

- 在借来的主机上（`guest` 指令）还成立吗：给对方留的量（`borrow.reserve_for`、`keep_left`、`guest_block`）、对方回来用时（`squeezed_by`、`oom_check`）。
- 预留（组内探针的历史样本算）与对方此刻的用量（worker 量）是不是同一口径：哪些进程算对方的、哪些扣掉（保活按整棵进程树，`ka.sh` 管的与对方另起的都扣），内存与 CPU 各怎么量。
- 只动自己的：只停自己的 worker、占位任务与 `ka.sh` 保活；看门狗只在保活真被拉起时暂停确定的（`watchdogs`、`freeze`），还时恢复（`thaw`）。
- 归还（`borrow.finish`）后留不留下东西：`/tmp/labmon` 以外不删，临时指令清掉；约定时间（`borrow.cutoff`）前跑不完的不放。
- 借来的主机不扫共享盘（`workerctl.run_out` 不给 `--shared`）。

相关代码：`guest`、`guest_scan`、`keep_left`、`squeezed_by`、`oom_check`、`watchdogs`、`ka`；`borrow.py`、`fleetops.run_borrow`。

漏过的：
- 对方的实验队列（循环跑实验、跑完拉保活）会被判成确定的看门狗、借来第一轮就暂停，对方后面的实验会跟着停（ccae559 盘点时发现，改成保活真被拉起才暂停）；29-15159 借来时把上次归还时 labmon 开的保活记成了对方的，
  还时会留下不让路的保活（ccae559）。
- 暂停看门狗时它可能正持着对方的锁（BOB 的 watchdog 拉起保活时持着对方 `run`/`start` 共用的锁约 8 秒），锁会一直占到还卡、对方自己的实验起不来（#38 改成暂停前看 `/proc/locks`）。
- 对方另起、`ka.sh` 停不了的保活：探针算预留时按字样扣掉，worker 判超出时却算成对方的，多卡借来主机上内存或 CPU 会超出预留、整台不放（#37）。

## 5. 临时指令与截止

- 被 `pause`、`drain`、`deadline`、`guest` 的“只给谁用”挡住的请求（`override_hit` 给的 `held`）：不进 `admit`、不排位、不算等待、不为它停保活，新东西也这样吗。
- 截止：真放行由 `override_hit` 挡、推演由 `cutoff` 挡，两边一致吗；截止前跑不跑得完用 `fits_before` 与 `req_dur`。
- 预备续租要 drain（`renew.reserve`），自用主机每周一次：drain 期间这件事怎样。
- `pause --now` 按登记的停法停（`send_stop`），被停的重新排队、不记失败。

相关代码：`load_overrides`（接上项目级的 `sched_ovr`）、`override_hit`、`cutoff`、`fits_before`、`gpu_state`；`config.set_sched_ovr`（项目级的暂停、优先）、`workerctl.override_each`（只给某几台的）、`renew.reserve`、`borrow.cutoff`。

漏过的：
- 暂停一个项目、预备续租时卡空着保活也不开（11-06）；等待时间算进了被 pause 挡住的那段，一 resume 队首立刻成屏障（11-07）。
- 截止挡住了占位任务，推演里却不挡（023774e）。

## 6. 生命周期

- worker 重启（`--replace`、`labmon worker start`）：新状态只在内存里吗，接管（`adopt`）后还在不在。
- 续租、平台重启就是容器重启：`/tmp` 清空（本地依赖 `needs=`、`scan_local`），PID 全变，容器里的自启无效，由看板拉起（`worker = "auto"`、`renew.after`）；换了容器 `health.container_step`。
- 掉线（`health.link_step`）、状态过期（`STATUS_STALE_S`、看板 `config.STALE_S`）、`worker stop`（`stopped`）时这件事怎么显示、怎么算。
- 看板与服务器上的 worker 版本可以不同：worker 只在 `labmon worker start` 时更新（部署的是 main 上已提交的版本，`workerctl.committed`）。

相关代码：`adopt`、`settle`、`finish`、`scan_local`；`health.py`、`renew.py`、`serve._Collector`、`workerctl.run_out`。

漏过的：
- 被等任务所在主机的 worker 续租重启时，别的主机以前没跑过的误报“找不到”，跑成功过、删了 `.done` 重跑的直接放行用旧结果（023774e 加 `DEP_STALE_S`）。

## 7. 主机与项目的进出

- 主机归还（`labmon host return`，主机清单的 `returned`；借来的由 `borrow.finish` 还）：不再连接、不报它的告警；页面上它最后的状态、
  登记在上面的队列、临时指令、给 wait 的消息怎么收尾；没跑完的怎么报。
- 项目结束或暂时没实验（队列的 `empty`、快照的 `no_work_since`）：还报不报主机的事、还占不占份额与卡、登记要不要注销。
- 本机离开内网（`lan.check`、`lan.gather`；测试用 `LABMON_LAN=off`）：连不上的是本机，不记连不上、不判掉线（`health.link_step` 的 `ok=None`），
  按旧数据算的空闲、日志多久没更新这类不报；回到内网后重新计。
- 时钟偏差：本机、各容器、共享盘服务器三处时钟不同（共享盘慢 3 分多钟）。比时刻只拿同一台写的比：worker 状态里自己写的 `t`，
  看板写给 worker 的时刻按 `overview._CLOCK` 换算，探针看共享盘文件按 `probe._fs_skew` 校正；不拿文件修改时间与本机时钟比。

相关代码：`config.active_hosts`、`borrow.finish`、`collect.build`（`no_work_since`）、`lan.py`、`health.link_step`、`overview._CLOCK`、`probe._fs_skew`。

漏过的：
- 主机归还后面板里没有清理（544691b 加 `returned`）；同一提交才发现共享盘时钟慢约 3 分钟，完成标记与日志时间要校正。
- 项目跑完后两条空队列还登记着，`worker status` 与工作台看不出是空的，别的项目会话以为它在抢卡、故意少分了卡（350da12 加“空着”“暂时没实验”）。
- 别的主机算不算过期看共享盘上的修改时间，各台互相当对方不在（d599f71，见第 1 维）。

## 8. 任务种类

- 占位任务（`is_hold`，名字按词含 hold / pause）：不要卡、不独占、不进时长与等待统计、不算份额（`own_runs` 不记）。
- 独占（`exclusive=gpu|host`）：留卡 `excl_hold`、腾空时刻 `hold_until`、外来进程 `intruders`、运行中作废 `void_check`；被挡住时放掉留卡。
- 单元申请（`slot`，`/tmp/labmon/slots`，`slot_reqs`）：没有清单、没有 `.done`，原因写 `.why`。
- `gpus=0` 的任务、同机并行车道（`fit`、`card_need`）、tag、`needs=` 本地依赖。

相关代码：`is_hold`、`job_spec`、`as_job`、`excl_hold`、`hold_until`、`intruders`、`void_check`、`slot_reqs`、`fit`。

漏过的：
- 占位任务漏写 `gpus=0` 时占着一张卡几小时，保活被停（`test_hold_never_takes_a_card` 守着）。
- 占位任务起初按手写的 `est_min=900` 估放行时刻，被等的任务预测变了也不跟；同日改成按依赖的预计结束，后来 `after=` 成了真依赖（11-07）。
- 被临时指令、依赖挡住的独占任务仍占着留卡（023774e）。

## 9. 真放行与推演

- 新规则只写在 `place(x, S, t, pre)` / `admit` 里一次，推演（`simulate`、`forecast`）走同一份吗；快照真放行用 `snapshot`、推演用 `sim_snapshot`，口径差在哪。
- 推演看得到同样的输入吗：依赖（`dep_end`）、临时指令与截止（`cutoff`）、借来主机的预留、运行中任务的预计结束（`run_end`、看板写来的 `eta.json`）。
- 页面上排队任务的预计时间只取推演的 `plan`，不另算。
- 只在此刻才有的信号（共享盘慢时的让路、读写拥塞嫌疑、对方超出预留）不是准入规则、推演不模拟；它们要是改了放不放，就得写进 `place`，再想推演怎么近似。

相关代码：`place`、`admit`、`shadow`、`fits_before`、`snapshot`、`sim_snapshot`、`simulate`、`forecast`、`run_end`、`dep_end`。

漏过的：
- 截止在真放行里挡占位任务、推演里不挡（023774e）。
- 运行中占位任务的预计结束曾取看板写来的 `eta.json`，那其实是推演自己的回声，被等的任务预测变了它也不跟（11-07）。
- 推演把借来的主机上对方此刻超出预留（`guest_block`，真放行挡、推演本不模拟）挡住的队列整条丢掉，别的主机的 offers 把整条当“原处跑不了”、连队首也建议挪走，队首随即在原处放行（#25 改成推演只排除持久的挡法，`override_hit` 的 `lasting`）。
- 推演把对方超出预留的卡（快照的 `owner`）整个清空，连借来的主机上对方另起、`ka.sh` 停不了的保活所在的卡也一起照排：那张卡真放行一直不放，推演的预计开始偏早，屏障的回填时刻、offers 跟着错（#45 改成 `sim_snapshot` 只去掉此刻才有的，有停不了的保活的卡照挡）。

## 10. 借用现有数据

- 先查 [唯一实现表](concepts.md)：要的数已经有人算了吗（状态字段、快照、用量记录、`renew.eta_of`、`estimate`、`duration`）。
- 不够用就扩展那一处（加参数、拆出共用部分），不复制一份改改；新做法取代旧做法时同一次提交删掉旧的。
- 新增一个会被多处用到的概念，补进那张表。

漏过的：自用主机续租与组内服务器续租各写了一套；掉线判定两份、规则不同；JSONL 读写八份；租期文字十来种写法（都已合并，见 AGENTS.md）。

## 11. 数据契约

- worker 状态（`write_status`，`tests/test_contract.py` 守着）、项目快照（`collect.SCHEMA`，[schema.md](schema.md)）、用量行（`usage.jsonl`）、
  事件（`records.EVENT_KINDS`）、看板写给 worker 的 `eta.json` / `sched.json` / `returned.json`（`overview.fetch` 的 `put`）、用户级主机清单（`config._set_keys`）：
  加了什么字段、谁写谁读。
- 旧 worker 写的状态没有新字段时看板怎样；共享盘上的旧数据：能一次迁完就迁、删兼容代码，迁不了的留兼容并写明何时可删（AGENTS.md 硬约束第 3 条）。
- `labmon_worker.py` 与 `probe.py` 在服务器上用 Python 3.8 跑、不 import 本包（CI 有一项专门查）。

相关代码：`write_status`、`usage`；`collect.build`、`overview.collect`、`probe.worker_view`、`records.py`、`state.py`。

漏过的：
- worker 写的时刻从文字改成 epoch 秒 `t0` / `t1`、用量行加 `cards` 与 `est_min`、`worker add` 必写 `--project`：当天把共享盘上的旧行一次迁完（11-07）。
- 借用样本 11-06 前缺 `ma`，只能留回退到 11-13（borrow.py）。
- 借用样本 #29 修复前的 `ma` 按保活进程树的 RSS 扣（扣多了），换算不成 PSS，只能取不扣保活的总 anon 留到 11-17（borrow.py `_ram_sample`）。

## 12. 呈现与告警

- 用户在哪看到：工作台（`overview.html`）、项目页（`page.html`）、两页共用的放 `common.js`；同一种文字只有一个函数生成。
- 要不要告警（有稳定的 key，[workbench.md「告警」](workbench.md#告警)）、要不要记事件（记录页）；`labmon worker status` 等命令行的文字。
- 新告警要定受众（维护者 11-10 定，#142）：给用户（只有用户能做、或要人看的事）、只给实验会话（会话自己能处理）还是两边都要（`collect.alert` 的 `to`；工作台 `alertsOf` 同样）。
  只给会话的不进页面上显眼的地方（告警数、红框、红点、系统通知）；两边都要的给用户那份写人话，给会话那份写照着做的操作。
- 告警会唤醒谁：`labmon wait` 收告警与全局快照的 `wake`（`waits.wait_check`），只叫醒相关项目的会话吗（按主机、按项目定向）。
- 会不会误报：任务之间的空档（上一个跑完、下一个还没占卡、worker 还没拉起保活）、worker 刚重启、本机刚回到内网时会不会报；要连续多久才报。
- 会不会重复：同一件事几个项目各报一遍、工作台总览列好几遍、条件来回跳时反复报。
- 页面测试：`tests/pages/snapshots.py` 补字段与分支，`tests/pages/check.js` 补断言。

相关代码：`collect.build`（告警）、`overview.py`、`records.log_events`、`waits.wait_check`、`common.js`。

漏过的：
- 依赖等的任务失败了，占位任务一直等、没有告警（11-07 加 `dep:`）；成环没人报（023774e）。
- 空闲关机告警在任务之间的空档误报，一天报好几次、两分钟后自己消失，两个项目的会话都被唤醒（55203a3 改成有任务在跑不算空闲、连续空闲 5 分钟才报）。
- 同机几个项目都报同一台主机的磁盘余量，总览里同一件事列好几遍（1f8d7f1 合成一条）。

## 13. 实验会话侧

- [SKILL](../skills/lab-experiments/SKILL.md) 里实验会话要不要改做法（只写它要做的事，细节链接到 docs）；旧说法删掉。
- `labmon wait` 的消息：新事件有没有给 wait 的 key，醒来后会话知道该做什么吗；几件事接连发生时会不会漏掉后面的。
- 实验会话会怎么读这件事：`worker status`、项目页的文字会不会让它误判（以为别人在抢卡、以为任务卡死）；
  被挡住时它会不会想绕开（改任务名、换 tag、改清单），报问题用 `labmon report`，由总控评估，值得跟进的开成 `from-experiment` Issue（[SKILL「发现 labmon 的问题」](../skills/lab-experiments/SKILL.md#发现-labmon-的问题)）。

相关代码：`waits.wait_check`、`collect.build`（告警）、`labmon_worker.py` 的 `status` 命令文字；`skills/lab-experiments/SKILL.md`。

漏过的：
- 借卡的要还、已还、对方在用以前叫醒所有项目的会话（1f0eb51 改成只报给在那台主机上有车道的项目）。
- 连续几条事件丢给实验会话，醒来只处理第一条、后面的漏掉（de73136：SKILL 写明醒来先重挂 wait 再逐条处理）。
- 一次 OOM 后同类任务估成 49 GiB、永远排不上，实验会话换名字、tag 绕开，没人报（[scheduler.md](scheduler.md) 11-07）。
- 实验会话把借来主机上看门狗的归属判断错了，这个错误前提写进了 SKILL（f460888 更正）。

## 14. 硬约束

逐条对照 [AGENTS.md「只守真会出事的规则」](../AGENTS.md#只守真会出事的规则)：

- 平台只做像人的操作、验证码由用户输入、验证时不点真平台的最后一步。
- ssh 不新建连接：一律 `lan.ssh_run`；这件事会不会让每轮多一次连接。
- 服务器上不冗余扫盘：能用元数据就不遍历；临时文件放 `/tmp`，共享盘只写结果与日志。
- 不直接 kill 保活；组内别人的服务器只读（例外只有保活守护与借用，范围见 AGENTS.md）。
- 测试不弹授权框、不发通知，不碰真平台、不连服务器。

漏过的：看门狗认定会把对方的实验队列判成确定的、暂停后对方后面的实验跟着停（ccae559，见第 4 维）。

### 页面二级操作

小菜单、短确认和短表单使用 AGENTS.md 约定的按钮旁浮窗；多行列表、多项选择用抽屉（#14 把批量续租从浮窗挪进抽屉：6 列表塞进 340px 浮窗被截、横向滚）。确认里的风险按“结论 → 要勾的 → 折叠细节”分层，长路径能断行。核对桌面定位、窄屏贴底、软键盘、Esc 和焦点返回。提交中切换操作或导航时，旧请求的回执不得关闭新弹窗或暴露一次性原文。

## 设计说明模板

贴在 Issue 评论里：

```markdown
## 设计说明

**目标**：要解决什么，现象或需求（附证据：主机、时刻、状态字段、日志行）。

**参考的现有做法**：查过的现成项目与工具（GitHub 上的同类项目、Slurm 等调度器）和本仓库里已有的同类功能，借了什么、哪里不照搬及原因；没找到的写查了哪些。

**方案**：怎么改，改哪几个函数；取代了什么旧做法（同一次提交删掉）。

**通用机制还是个案适配**：是一条以后同类情况都走的规则，还是只为眼前这个例子加的分支；个案适配写明为什么不做成通用的。

**失败与边界场景**：会出错的情况（worker 重启、对方回来用、时钟偏差、任务之间的空档、两件事同时发生……），
动手前先用测试桩与夹具试过，写试出了什么、方案因此改了什么。

**UI 类**（改页面时）：改前、改后各看到了什么，用文字写（截图自己看，不提交进分支；`gh` 传不了图片，要给图就在网页上编辑评论时拖进去）；复用了唯一实现表里哪些组件（`popConfirm`、`tlSvg`、`leaseHtml` 等），新写的为什么不能复用。

**逐维度影响**（docs/interactions.md；不涉及的写“无关”加一句理由）
1. 多主机：
2. 同机多项目：
3. 多卡：
4. 借来的主机：
5. 临时指令与截止：
6. 生命周期：
7. 主机与项目的进出：
8. 任务种类：
9. 真放行与推演：
10. 借用现有数据：
11. 数据契约：
12. 呈现与告警（会唤醒谁、会不会误报或重复）：
13. 实验会话侧：
14. 硬约束：

**要改的文档**：README / docs/… / SKILL.md / docs/concepts.md 唯一实现表

**要加的测试**：在旧代码上会失败的用例；多主机用例（tests/fixtures.Hosts）；页面快照与断言（改了页面时）

**问题**（三类分开写；一个问题一句话，推荐项放第一位）
- 自己定：客观问题，写证据与结论。
- 按推荐先做：除“必须等”之外的主观与设计问题（措辞、缺省值、阈值、估计公式、准入策略、页面布局与细节、数据契约），写推荐项，接着实现，PR 正文再列一次。
- 必须等（只剩直接影响维护者用法的事与安全类，见 AGENTS.md「要等维护者」，2026-11-10 Issue #92）：写选项与推荐，等维护者定。
```
