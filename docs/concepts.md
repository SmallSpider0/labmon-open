# 唯一实现表

[AGENTS.md「一个概念只有一处实现」](../AGENTS.md#一个概念只有一处实现硬约束)的那张表。动手前在这里搜要动的概念（中文说法、函数名、字段名都可以搜），表里有的只能调用表里的实现、不够用就扩展它；新增一个会被多处用到的概念时补进表里。审查清单第 2 条按这张表核对（[AGENTS.md「审查要点」](../AGENTS.md#审查要点)）。

写法：`worker 的 X` 指 `labmon/worker/`（由 `labmon/worker/__init__.py` 统一导出，按职责分在 `pure`、`common`、`queue`、`disk`、`measure`、`keepalive`、`deps`、`sched`、`forecast`、`offers`、`core`、`fit`、`cli`，入口脚本为 `labmon/labmon_worker.py`；其中 `worker.pure` 是看板与 worker 共用的 Python 3.8 纯模块），没写文件的 JS 名在括号里标文件（common.js、overview_js/<模块>.js（渲染时内联进 overview.html）、page.html）；其余是 `labmon/<模块>.<名字>`。
表里每行只写“概念 → 唯一实现的位置”；口径与行为细节在下面[「各概念的口径」](#各概念的口径)，序号与表里相同。行为的完整说明以 `docs/` 各篇与代码为准。

| # | 概念 | 唯一实现 |
|---|---|---|
| 1 | ssh 参数与执行；按主机隔离长连接与专用密钥 | `lan.ssh_argv`（`ctl_path`、`ext_known_hosts`）、`lan.host_ssh_argv`、`lan.close_host_ssh`、`lan.ssh_run` |
| 2 | 传到服务器上留着用的文件只取 main 上已提交的版本 | `workerctl.committed`（`DEPLOY_REF`、`committed_tree`、`worker_bundle`） |
| 3 | 本机在不在内网；按主机并行探测 | `lan.check`、`lan.gather` |
| 4 | 整文件覆盖写 | `state.write_atomic`；`save_json`、`collect.write_page`、`users.json` 都经它 |
| 5 | 本机状态根目录与其下的状态目录 `overview/` | `state.state_root`、`state.state_dir` |
| 6 | 按文件缓存读出的内容时判文件变没变 | `worker.pure.file_sig`（看板 `state.file_sig` 直接复用：`disk._rows`、`fleetview._hours`、`users._read`、`waits._sig`、`serve.Project._mtimes`；worker `fsig` 传 `strict=True`：`load_hist`、`queue_durs`、`load_eta`、`list_of`、`list_env`、`qenv`） |
| 7 | JSON 状态文件；工作台的全局快照 | `state.load_json`、`state.save_json`；`state.snapshot()` |
| 8 | JSONL | 逐行解析共用 `worker.pure.parse_jsonl`（看板 `state.parse_rows`、`load_rows`、`append_row`（`_lock`）；worker `load_jsonl`、`append_jsonl`（`flock`）） |
| 9 | 组内服务器状态目录里一个 JSON 的读改写 | `fleet.edit_state` |
| 10 | 时刻的文字与解析 | `config.fmt_time`、`config.parse_time`；命令行的相对写法与到期语义 `__main__.parse_until`；worker 与探针（`probe.py`）各自一份 |
| 11 | 项目快照多久没更新算过期 | `config.stale_limit`（系数 `STALE_RUNS`、`STALE_PAD_S`；`waits.collector_stale` 调它；`labmon wait` 连不上看板多久提示一次、连回来报 `server_back` 也按它（`WAIT_OFFLINE_MAX_S`、`server_unreachable`），`__main__.cmd_wait` 用本地 monitor.toml 的 `config.brief`）；项目快照的 `thresholds.stale_s`、配置摘要的 `stale`（`serve.Overview.cfg_info`）下发；页面上 `cfAlerts`（overview_js/project.js、`stale.s`、`stale`） |
| 12 | 时刻要在将来 | `config.check_future`；`parse_until` 把它的 `ConfigError` 转成退出 |
| 13 | 页面上的时刻文字：月-日 时:分、月-日、时:分、今天只写时分 | `ts`、`tsMD`、`tsHM`、`tsNear`（common.js） |
| 14 | 时长文字 | 页面上 `dur`（common.js）；看板 Python 侧 `config.fmt_dur` |
| 15 | 看板服务交给系统托管 | `service._write_and_restart`、`service._args`（`_read`）、`service.restart`、`service.restart_later`、`service.stop`、`service.installed` |
| 16 | 服务日志的一行；系统通知 | `service.log`；`service.notify`（`service.push_conf`、`labmon notify test`、`service.notify_test`）；推送本身（`notify.toml`）只在 `labmon/push.py`（`push.load`、`push.one`、`push.target`、`push.err`） |
| 17 | 秘密；外部机专用 SSH 密钥对（零私钥上传） | `platform.secrets_root`、`platform.secret`、`platform.set_secret`、`platform.del_secret`、`platform.prompt_secret`（`labmon secret`）；平台操作登录取密码 `platform.password`；外部机专用密钥 `platform.ssh_key_path`、`platform.ensure_ssh_key`、`platform.ssh_key_info`、`platform.del_ssh_key`、`platform.ssh_identity`、`platform.ssh_auth_cmd` |
| 18 | 看板服务所在机的体检 | `hostcheck.enabled`、`hostcheck.tick`（`run_ps`、`read_win`、`read_updates`、`read_wsl`、`sample`、`judge`、`due`）、`hostcheck.view`（`hostcheck`）；页面上 `svcCard`（overview_js/home.js） |
| 19 | 看板服务的计划维护 | `maint.until`、`maint.start`、`maint.stop`（`overview/maint.json`）；读它的：`serve.health`、`hostcheck.loop`（`tick`）、`hostcheck.view` |
| 20 | 服务租约（这台是主还是备用；备用时不做的主动动作都先过它） | `lease.remote_sh`（`overview.fetch`、`overview.lease_part`）、`lease.decide`、`lease.tick`、`lease.release`、`lease.take`、`lease.set_marker`；门 `lease.primary`（`fleet.tick`、`labmon fleet probe`）、`lease.need`（`collect.collect`、`labmon collect`、`workerctl.run_out`、`CHANGE`、`override_each`、`borrow.begin`）；看板的写请求 `serve.Handler._standby`、`lease.flag`（`/healthz`）；页面与快照的 `lease.view`、那句话 `lease.text`（`standbyBar`、overview_js/app.js）；备用时照收的写请求 `serve.standby_ok`、发页面时现填 `serve.live_snapshot` |
| 21 | 冷备与恢复 | `backup.make`、`backup.make_bytes`、`backup.pull`、`backup.schedule`、`backup.restore`（`labmon backup`、`labmon server restore`、`config.keep_section`、`backup.MINE`） |
| 22 | 看板服务的两个入口绑哪、内网入口的 TLS、各认哪些 `Host`；`Host` 写法的解析 | `config.server`；`config.host_pair`；本机命令行与部署脚本怎么连内网入口`config.lan_url`（`config.pinned`、`ca`）；`serve.allowed_hosts`、`serve.host_ok`、`serve.tls_wrap`（`Handler.setup`） |
| 23 | 探活；TX 上探它、异常 15 分钟推、恢复再推的看门狗 | `serve.health`；`tools/tx/labmon-watch.py`（`judge`、`step`、`commit`） |
| 24 | 服务端写的跳转与链接回看板根的相对地址 | `serve.root_rel`（`serve.login_url`） |
| 25 | PID 文件；状态目录的 `current`；别的主机放的这台不动 | worker 的 `read_pid`、`read_current`、`drop_current`、`run_elsewhere`（`requests`、`Worker.pool_todo` 调）；探针另读一份 `current`（probe.py） |
| 26 | overrides.json、events.jsonl 读改写的互斥 | worker 的 `flock` |
| 27 | 告警给谁看与给谁的那份文字；页面上显眼列哪些；`labmon wait` 叫不叫醒会话 | `collect.alert`（`AUDIENCE`）、`collect.user_alerts`（`serve.proj_info`）；`waits.wait_check`、`waits._new_alerts`（`REWAKE_S`、`EACH_TIME`）；页面上 `forUser`、`forAgent`、`alertBox`（common.js）；工作台主机告警 `alertsOf` 返回 `{to, msg}`（overview_js/core.js） |
| 28 | 问题报告与普通账号的每日份数 | `__main__.cmd_report`、`records.report`（`records.ReportQuota`）、`records.report_headers`、`tools/issue_watch.py` |
| 29 | 记录；事件种类与分类表；从暂停与优先记录取项目 id | `records.log_events`、`records.records`（`records.EVENT_KINDS`、`records.EVENT_CATS`）、`records.schedule_project`；项目所用主机上哪些记录算它的（`serve.proj_events`、`labmon records`）`records.project_mine`、`labmon records` 取的 `records.project_rows`；项目要登记过`projects.require`；页面上 `recSlice`、`recList`（`recGroup`）、`logPage`（overview_js/sched.js、`logPage` 在 overview_js/home.js） |
| 30 | 主机缺省值；缺省端口；worker 多久没写状态算不在 | `config.host_defaults`；`config.PORT`；`worker.pure.STALE_S`（`config.STALE_S`、worker `STATUS_STALE_S` 共用；`ka_bin`、`workerctl.worker_env`、`config.guest_host`） |
| 31 | 用户级主机清单里的主机；其中在用的 | `config.user_hosts`（`config.guest_host`）；`config.active_hosts`；在用的在前、已归还的在后的全部 `config.all_hosts` |
| 32 | 项目 id | `config.load`（`cfg["id"]`）；看板只认 worker 状态里的 `project`；`projects.project_ids`；命令行核对 `api.unknown_project` |
| 33 | 队列登记；撤登记与看板核登记归属按清单文件认；读一台的登记目录；各主机的登记目录 | `worker.pure.list_path`、`worker.pure.same_list`（worker `matching_regs(by_file=True)`、看板 `users.same_list`、`collect.reg_queues`、`reg_projects`、`pool.locate` 共用）；worker 的 `qslug`、`reg_files`、`regs`、`registry`（`Worker.registry`、`worker add`、`fit`）；`reg_dirs`、`queue_state_conflict`（`worker add`） |
| 34 | 清单的停法与 ROOT 内的停止请求文件 | worker 的 `queue_stop_path`（`workerctl.run_out`）、`write_queue_stop`、`Worker.send_stop` |
| 35 | 一条清单只归一台 | worker 的 `main` 的 add、`Worker.registry`（`peer_regs`、`peers`、`yielded`）、`Worker.adopt_left`（`Worker.load_prev`）、`Worker.start`；锁 `reg_lock`；旧名登记搬回来换了文件名时在跑的改记到新名下 `Worker.rekey` |
| 36 | 清单登记的全局视图 | 全局快照的 `regs`：`overview._regs`（`config.home_host`、`overview.fetch` 的 `_reg_cmd`、`_parse_regs`、`REG_KEYS`；worker 另有一份）；页面上 `projRegSec`、`regRow`、`regQuiet`、`regTally`、`tallyBar`、`tallyHtml`、`REG_ST`（overview_js/project.js） |
| 37 | 项目配置的摘要与页面上改项目的 monitor.toml；计划项的进度 | `serve.Overview.cfg_info`（`projects[].cfg`、`spec`、`config.cfg_spec`）、`serve.Overview.proj_info`；`projects.set_config`→ `config.set_project`（`PROJ_KEYS`、`ALERT_KEYS`、`_proj_val`、`_move_groups`、`_set_markers`，接新块 `_append_blocks`、值的写法 `_toml_val`（`_set_keys`）；`POST /project/config`）；页面上 `projCfgSec`（`cfRow`、`cfDurRow`、`cfRhythm`、`cfMarkers`、`cfGroups`、`cfAlerts`、`cfgSaveBar`、`cfgDiffText`），草稿 `CFE`（`cfgEdit`、`cfgCur`、`cfgDraft`）、改了哪些 `cfgDiff`（`cfgKeep`）、当场校验 `cfgErrs`、画一节要的 `cfgCtx`、打字时就地换 `cfgPatch`、`cfgAct`、`cfgInput`（overview_js/project.js） |
| 38 | 哪几台在用主机共用一个 worker_home（共享域） | `config.home_key`、`config.home_peers`（`config.home_host`、`_same_home`）；`api.hosts` 的 `home_with`、`api.home_text` |
| 39 | 在哪台上操作一台主机目录下的登记与清理临时指令 | `workerctl.via`（`run_out`、`config.home_host`）、`workerctl.clear_overrides`（`borrow.finish`、`fleetops.run_swap`） |
| 40 | 页面上搬、移除登记；能搬去哪些主机 | `serve.reg_move`、`serve.reg_rm`（`POST /reg/move|rm`）；页面上 `regHostBtns`、`regTo`（`freeCards`）、`regAct`（`regMine`），一条登记的进度 `regLeft`（`regRow`、`queueRow`、overview_js/project.js；`queueRow` 在 overview_js/storage.js） |
| 41 | 上一台自用主机；新主机页接上原来的项目；无登记自用主机「给项目用」 | `fleetboot.last_mine`（`back_port`、`regs`、`prev`）；页面上 `adoptSrc`、`adoptCard`、平台操作结果后的一句 `adoptHint`；给项目用 `serve.host_assign`（`POST /host/assign`、`records.tell`）、页面上 `assignSrc`、`assignCard`、`assignDrawer`、`assignAct`（overview_js/project.js） |
| 42 | 项目登记；看板上有哪些项目 | `projects.root`、`cfg_file`、`files_dir`、`out_dir`、`ver_of`、`registered`、`project_ids`、`load_cfg`、`get_config`、`put_config`（`_check`、`_store`、`Refused`）、`set_config`、锁 `_reg_locked`；经接口改 `[[hosts]]`、`[[queues]]` 要平台操作的权限（`projects.GUARDED`、`_guarded`、`put_config`、`ops`、`users.can_ops`）；done_when 文件的位置 `config.done_key`、用到哪些 `config.done_files`；接口 `GET/PUT /api/project/<id>/config`（serve.py） |
| 43 | 项目目录里的 monitor.toml 与看板上那份同步 | `projsync.status`、`push`、`pull`、`sync`、`say`、同步记录 `mark`、`base_of`；取、推只经 `projsync._get`、`_put`（`client.call`、`/api/project/<id>/config`）；本地这份只做 TOML 解析 `config.brief`；`wait` 里 `__main__._sync` |
| 44 | 项目的归档、恢复与永久删除；看板服务里按归档摘下与加回项目 | `projects.archive_block`、`projects.archive`、`projects.restore`、`projects.archived`（`wait`、`check`、`projects.archived_msg`、`by`、`projects.json`、`projects._locked`）；`serve.Server.sync`（`_Collector.resume`）；永久删除 `Server.delete_project` → `projects.delete`（`overview.check_project_idle`）；当前归档列表 `projects.archived_rows`；页面上 `projArchSec`、`archDrawer`、`pjAct`（overview_js/project.js） |
| 45 | 等待放行的请求：字段、清单行 → 任务 → 请求、按名字找清单行与资源提示、`.env` 路径、按主机现状补全 | worker 的 `new_req`；`hints`、`job_of`、`queue_req`；`job_cmd`；`first_rows`；`env_path`；`Worker.prep` |
| 46 | 并行清单 | worker 的 `list_par`（`PAR_RE`、`list_jobs`、`q["par"]`）、`next_jobs`、`run_key`、车道 key `lane_key`、这条清单在跑的 `Worker.lane`、接管时不撞 key 的 `Worker.free_key`、门槛 `Worker.par_gate`（`place`、`Worker.par_t`、补位 `Worker.par_hi`）；推演 `simulate` 的窗口（`put`）、`forecast` 的 `par`；`collect.parse_gpu_queue`、`collect._queued_times` 的 `par`；页面上 `parPack`、`laneTail`（page.html），`api.status` 的车道行 |
| 47 | 挡住一个请求的临时指令；其中持久的 | worker 的 `override_hit`（`lasting=True`） |
| 48 | 项目暂时没实验 | worker 状态里队列的 `empty`（`requests`、`next_jobs`）；`collect._no_work` 算 `no_work_since` 与那条告警（`NO_WORK_WAKE_S`）；借来的主机 `borrow.check`（`IDLE_S`）；页面上 `NO_WORK`、`noWorkTip`、`noWorkBadge`（common.js） |
| 49 | 改配置文件 | `config._blocks`、`config._set_keys`、`config._write_checked`（`check`、`config.load(path, text)`）；锁 `config.HOSTS_LOCK`（`hosts.lock`），改配置文件的函数都加 `config._hosts_edit` |
| 50 | 占位任务的认定 | `worker.pure.is_hold`、`worker.pure.HOLD_RE`（worker 与看板 `config.HOLD_RE`、`collect.is_hold` 共用） |
| 51 | 登录账号在组内人员表里的名字、主机上的使用人；后台与历史记录按已登记的人员统计 | `users.person_of`、`users.people_of`、开通与补绑定前的核对 `users._check_bind`、开通时可选的人 `users.free_people`、`users.add`、`users.bind_person`、名册删人前 `users.removing_person`；删人时唯一的清理入口 `users.remove`（同步调 `config.set_host_users` 与 `config.remove_pool_member`）；`fleet.host_holder`；旧数据 `fleet.legacy_person`；项目算在谁名下`fleet.project_people`，写进 `sched.json` 的人员键 `fleet.share_key`；登记人员 `fleet.registered_people`、`fleetboot.set_person`，人员卡与账号详情共用 `personCard`、`directoryDrawer`（overview_js/fleetops.js） |
| 52 | 组内服务器的状态文件；这一台正借着；借给了谁 | `fleet.state_file`；`fleet.borrowed`；`fleet.borrower` |
| 53 | 共有型算力池（多池配置、权限、落机/空闲窃取/撤离与视图） | `worker.pure.POOL`、`POOL_PREFIX`、`POOL_ID_RE`、`pool_host_id`、`is_pool_id`、`pool_id_of`；`config.POOL_KEYS`、`config.pools`、`config.resolve_pool`、`config.host_pool`、`config.pool_home`、`config.remove_pool_member`；`pool.key`、`pool.member`、`pool.user_pools`、`pool.user_pool_hosts`、`pool.exclusive_pool`、`pool.locate`、`pool.tick`、`pool.summary`、`pool.status_text`；worker 的 `pool_refuse`、`Worker.pool_todo`、`pool_heads`、`fit_head`、`add --pool`；`serve.reg_move` 的 `pool`、`workerctl.run_out` 的 `pool`、`overview._pool_mark`、`collect._lane` 的 `pool`；页面上 `isPoolId`、`canUseHost`、`hostShort`（common.js）、`poolSummaryCard`、`poolPage`（overview_js/sched.js）、`hasPoolCompute`（overview_js/home.js） |
| 54 | 旧借用池配置残留校验 | `config.LEGACY_POOL_KEYS`、`config.check_legacy_pool`（`config.user_hosts` 与 `fleet.conf` 共用，对 `[[hosts]]` 与 `[[fleet.servers]]` 拦截 `lend`/`lend_hours`/`borrow_pool`） |
| 55 | 服务器持有人鉴权 | `users.need_holder`（核组内服务器 `holder` 与非借用自用主机 `fleet.host_holder`，`serve.Handler._own` 的 `fleet_srv` 调用） |
| 56 | 租期到期时刻；租期提醒的档 | `health.lease_end`；`health.lease_due` |
| 57 | 掉线与恢复判定；换了新容器 | `health.link_step`（支持自定义阈值 `down_s`，看板主机、组内服务器、项目页与共有算力池失联撤离共用）；`health.container_step` |
| 58 | 平台操作 | `fleetops.run_*`（`fleetops.OP_NAMES`）；核对的比对 `fleetops.reconcile`、确认作业不在运行 `fleetops.job_stopped`（`platform.json` 的 `stopped`）；停平台作业、提交克隆并等运行 `fleetops.stop_job`、`fleetops.submit_job`（`op.json`）；出错时这一台的状态一句话 `fleetops._note` |
| 59 | 这一台此刻能不能动 | `fleetops._risk`、`fleetops._need_ack`（`lw`、`fleetops._lw`）；页面上 `FBUSY`、`FRISKY`，勾选框的文字 `ackText`（overview_js/fleetops.js） |
| 60 | 换人、换节点、开机的作业名、端口与规格核对；续租、开机换了容器清旧主机密钥；连一台时新主机密钥记在哪份 | `fleetboot.taken_jobs`、`fleetboot.job_name`；`fleetboot.back_port`、`fleetboot.pick_port`（`fleetboot.used_ports`）；`fleetboot.pool_specs`、`fleetboot.spec_of`、`fleetboot.spec_name`、`fleetboot.check_spec`（`fleetboot.check`、`fleetboot.preview`）；页面上 `poolSpecs`、`specName`（overview_js/fleet.js）；`fleet.forget`（`fleetboot.mine`、`known_hosts`）；`fleet._ssh_cmd` |
| 61 | worker 停启保活 | worker 的 `keepalive`、`ka`；`gpu_owners`（`ka_full`、`ka_owned`）、`guest_scan`；推演经快照卡的 `ka` 由 `sim_snapshot` 挡 |
| 62 | 命令行里保活的那几句 | worker 的 `ka_lines`（`labmon worker status`、`api.status`、`keepalive`、`gpu_procs[].t0`） |
| 63 | 命令行里一台的 CPU 那一句 | worker 的 `cpu_line`（`labmon worker status`、`worker fit`、`FitHost.card_lines`、`api.status`、`info.cpu_quota`、`room.cpu`、`running[].acct`） |
| 64 | 要卡时停了的保活又被拉起；ka.sh 日志里哪条 `RESTORED` 是这台的；进程启动时刻 | worker 的 `ka_restarted`、`ka_caller`、`ka_others`；记一次 `Worker.ka_fought`（`Worker.ka_gone`）；`Worker.ka_groups`（`KA_LOG_LAG`、`ka_log_time`）；`started_at`（`host_info`）；重启后找回近 1 小时的次数与“停着保活”（`kafight.hits`、`ka_stop`）`Worker.load_prev` |
| 65 | 要卡时停保活没停成 | worker 的 `Worker.ka_stop`、`Worker.ka_note`、`Worker.ka_stuck`（`ka_lines`、`Worker.snapshot`、`other`、`fit_snapshot`）；看板 `borrow.finish` 的 `stuck`（`borrow.check`）；页面上 `alertsOf`（overview_js/core.js） |
| 66 | 保活看门狗 | worker 的 `watchdogs`（`script_of`、`refs`、`ours`）、`freeze`（`ka_restarted`）、`thaw`、`Worker.watch`；进程持着的文件锁 `held_locks`，平时持的锁 `Worker.note_seen`（`frozen.json`、`seen[].locks`、`locks_t`、`lock_base`），要不要推迟 `Worker.lock_busy`（`Worker.ka_wait`）；`borrow.finish` 调 `thaw`；页面上借用抽屉 `borrowSec` 的看门狗一行 |
| 67 | 平台操作在不在做；哪些服务器正在做 | `fleetops._live`；`fleetops.busy()` |
| 68 | 自用主机当成组内的一台 | `fleetboot.mine`；自用主机的「worker」节 `workerSec`（overview_js/fleet.js） |
| 69 | 服务器的池类型文字 | `fKind`（overview_js/fleet.js） |
| 70 | 人的缩写与头像；账号名换成组内人员名 | `pabbr`、`pfav`（`accAv`、`pabbr`）；`acctPerson`（`pabbr`/`pfav` 在 overview_js/core.js、`accAv` 在 overview_js/account.js、`acctPerson` 在 overview_js/project.js） |
| 71 | 普通用户此刻能用的主机 | `myHosts`（overview_js/home.js） |
| 72 | 组内服务器页面的数据、详情曲线、人员详情；命令行 `labmon fleet status` / `probe` | `fleetview.view`、`fleetview.detail`、`fleetview.person`；`fleetview.show` |
| 73 | 使用记录；按人统计 | 记 `fleet._hour`（`fleet.accs`）；拆 `fleetview._parts`、汇总 `fleetview._by_person`、`fleetview._stats`、借来借出 `fleetview._lending`；页面上 `useBar`、`perHist`（`PKS`、`PSN`、`PSC`、`lendLines`、overview_js/fleetops.js） |
| 74 | worker 状态新鲜度门槛；自用主机与借用中组内服务器的使用样本；逐次样本的一行 | `fleet.worker_fresh`（`fleetboot.mine`、`fleetview.view`、`collect._host_cpu`）、`fleet.from_worker`（`fleetboot.mine`、`fleetview.view`、`sample`）、`fleet.record_mine`（`overview._notices`）；`fleet._series_row`；曲线接口认哪些 id `fleetview.known` |
| 75 | 可共享 | `fleet.shareable`（`analyze`、`from_worker`） |
| 76 | 预备续租、续租后拉起 worker | `renew.reserve`、`renew.cancel`、`renew.begin`、`renew.after`；页面上自用主机的按钮 `renewBtns`，续租一台的确认 `goRenew`（overview_js/sched.js） |
| 77 | 后端看 worker 状态里运行中任务的预计结束 | `renew.eta_of` |
| 78 | 借用 | `borrow.begin`（`fleetops.run_borrow`）、`borrow.cutoff`、`borrow.check`、`borrow.request_return`、`borrow.cancel_return`、`borrow.finish`（`gone=True|"stopped"`）；页面上 `borrowText`、`borrowBadge`（common.js、`borrow.check`），操作按钮 `borrowBtns`（overview_js/fleet.js） |
| 79 | 按钮旁的小弹窗与按钮配色 | `popConfirm`、`popToggle`、`popClose`、`closePop`、`fitPops`；平台操作的弹窗 `opPop`（overview_js/fleet.js） |
| 80 | 平台操作确认的风险分层；会中断什么的那句 | `riskSay`、`riskNeed`、`riskMore`；`busyText`（`renewWarn`、overview_js/fleetops.js） |
| 81 | 批量续租 | `renewDrawer`（`rpList`、`rpBody`、`DH.renew`、overview_js/fleetops.js） |
| 82 | 弹窗里选一个将来的时刻 | `whenPick`（缺省 `whenInit`、`whenNow`，小签 `whenTap`、`whenAdd`（`WHEN_ADD`）、点前失焦 `whenBlur`，控件改了 `whenInput`、`whenRead`、`whenSync`，说明 `whenSay`，`whenT`、`whenPast`、`whenHalf`，提交前 `whenStop`；epoch 秒换成控件的值 `dtLocal`，overview_js/fleet.js） |
| 83 | 主机的名字只有三种写法：短名、全名、组内服务器「使用人」 | `hostShort`（common.js）；`h.label` 直接取；`fName`（overview_js/fleet.js） |
| 84 | 工作台上的项目名与整台主机的操作波及哪些项目 | `projName`、`affects`（`projName` 在 overview_js/home.js、`affects` 在 overview_js/sched.js） |
| 85 | 项目的车道从哪来；不是 monitor.toml 写明的车道收起；跑空登记自动休眠 | `collect.reg_queues`（`REG_KEEP_S`、`.labmon/reg_lanes.json`、`collect.reg_sig`、`serve.Overview.wake_moved`）、`collect.lanes_cfg`（`labmon check`）；补主机与队列 `config.with_queues`（`load`、`_norm_host`、`_norm_queue`）；收起 `collect._quiet`（`_lane_specs`、`quiet`、`_is_done_lane`、`_sync_returned_done`）；自动休眠 `worker.pure.IDLE_REG_S`（`Worker.registry`、`Worker.requests`、`Worker._idle_start`、`Worker.pool_todo`、`probe.queue_gpu_queue` 的 `sleeping`/`done_sig` 短路） |
| 86 | 车道名与车道跑的计划项 | `collect._lane_specs`（`collect._is_done_lane` 不占“车道 N”号）；页面上 `laneName(lane, hosts)`（`laneLabel`、`laneFit`、common.js）、`laneGroups`（page.html），工作台经快照取：worker 状态里的队列（`borrow.late`）按 host、root、queue 找快照车道 `laneOf`（overview_js/home.js） |
| 87 | 运行中的那一格与交回算力池车道上的任务实际在哪台跑 | `probe.py` 的 `current.host`、`collect._lane`；`collect._attempts`；`waits.job_state`；页面上 `rowJobs`、`awayText`、`jobLane`（page.html） |
| 88 | 给 labmon wait 的全局消息 | 全局快照的 `wake`（`borrow.check`、`overview.collect`），`waits.wait_check` 合并（`waits._wakes`、`labmon check`、`waits.check_told`、`api.check`） |
| 89 | 看板侧按名字认一个任务此刻的状态与结果；任务名 `项目/任务` 的写法 | `waits.job_state`、`waits.JobWatch`（`JOB_CONFIRM`、`state`、`restore`）；名字 `worker.pure.job_key`（`waits.job_key` 与 worker `dep_keys` 共用）；按 id 找项目的配置与输出目录 `projects.load_cfg`、`projects.out_dir`（`labmon project archive`） |
| 90 | 看板接口的客户端；连看板一律直连、不走环境变量与系统设置里的代理 | `client.call`（`servers`、`token`）、一条命令的回话打出与退出码 `client.run`；连不上时证书校验没过的那句 `client.ca_hint`；`client.login`、`client.urls`；直连的 opener `client.opener` |
| 91 | 命令行的命令在看板那边的做法 | `api.host`、`api.borrow`、`api.override`（`OVR`）、`api.worker`、`api.project_init`、`api.check`、`api.hosts`（`GET /api/hosts`）；实验会话的只读命令 `api.status`、`api.disk`、`api.records`（`GET /api/project/<id>/status|disk|records`，回 `{rc, out, err, data}`，`--json` 打 `data`：`client.run` 的 `as_json`；借来的主机那一句 `api.borrow_text` |
| 92 | `labmon wait` 的长轮询 | `waits.wait`（`POST /api/wait`）；命令行一轮 `__main__.cmd_wait` |
| 93 | 项目最后一次有 wait 来等的时刻；会话的 wait 断了；一个项目只让一个会话等与会话标识 | `waits.state_file`、`waits.gone_since`、`waits._claim`、`waits.dup_refused`；告警 `collect._wait_gone`（`wait_gone:`、`wait_dup:`）；客户端 `client.session_id` |
| 94 | 页面上的改动告诉实验会话 | `records.tell(project, what, text, src=)`（`overview/tell.jsonl`、`TELL_KEEP_S`、`src`）、`records.told`（`overview._notices`、`wake`、`page:<what>:<项目>:<毫秒>`） |
| 95 | 任务的资源估计；一张卡要多少空闲显存才放；记多少内存、几个核；开跑给几个核；CPU 记账回收 | worker 的 `estimate`（`peak_rows`、`CPU_DEFAULT`、`cpu_shrink`、`cpu_threads`）、`fit`、`fit_list`（`labmon worker fit [--list]`、`place`、`fit_snapshot`、`FitHost`、`known_lists`、`list_row`）；`need_mb`、`card_need`、`ram_need`、`cpu_need`；`cpu_give`（`cpu_limit`、`Worker.cpus_for`、`Worker.acct`、`acct_job`）；窗口核数 `measure` 记的 `use.pks`、`pk0` |
| 96 | 任务要跑多久；用量行的同卡并行数；此刻的 k、推演开跑时的 k 与运行中任务自己的 k | worker 的 `duration`（同类 `same_dur`、借比值 `ratio_dur`（`borrowable`）、截止用的 `with_cut`；挑记录 `near`、`rec_conc`，k 取整 `conc_key`，跑成功 `ran_ok`，比值可信 `trusted`；各清单的写进状态的 `dur`，探针带回看板）；`measure` 记的 `conc`（`card_conc`、`Worker.k_of`）；`Worker.k_now`、`Worker.dur_k`（`durk`、`simulate`、`launch`）、`Worker.k_of`（`run_end`、`io_suspect`、`dur`、`queue_durs`）、在跑同类的总长 `run_tot`（`Worker.run_tot`、`fit`；只认看板 `collect.eta_by_progress` 标进 `eta.json` 的 `prog`）；请求与运行中任务取 `req_dur`（`k_of`、`src=True`、`cut=True`、`gang_dur`、`gang.json`、`cut`），place 认的任务带 `cut`（`job_spec`、`cut_min`、`fits_before(…, 1)`），准入取 `dur_min` |
| 97 | 任务依赖 | 解析 worker 的 `hints`（`deps`、`deps_any`）、`dep_keys`；本机清单的状态 `Worker.list_state`（`marks`、`todo`、`rows_left`）；状态 `Worker.dep_state`（`dep_idx`、`dead`、`dep_where`、`peers`、`peer_ends`、`peer_todo`）；放行 `Worker.dep_block`（`prep` 与 `forecast` 后同轮刷新、`dep`、`dep_cycle`、`dep_note`、`Worker.dep_waiting`、`Worker.dep_next`、`Worker.dep_reaches`）；谁写了依赖等着谁 `Worker.dep_waiters`（`cant_move`、`fit --list`）；`fit --list` 判前置经 `fit_deps`（`dep_state`、`dep_base_ends`、`dep_block`）；何时满足 `dep_end`（`simulate`、`req_dur`、`run_end`、`forecast`、`Worker.dep_base_ends`、`self.ends`），名字 `dep_names`；看板取 plan 的 `deps` 与等待项的 `dep`（`collect._job_times`、`dep:`）；页面上 `depLines`、`holdGuess`（page.html），工作台 `alertsOf`、`rankTip` |
| 98 | 成组起跑 | 解析 worker 的 `hints`（`gang`、`gn`）；认组、读同伴 `Worker.gang_sync`（`gctx`、`gsim`、`gang_when`、`gang_bad`）；挡住 `Worker.gang_block`（`prep`、`dep`、`dep_cycle`）；留资源 `place`（`gholds`、`Worker.gang_hold`、`gang_until`、`acct_job`）；放掉这一轮没重新留的、放行后复核留着的 `Worker.gang_recheck`；一起放行 `Worker.gang_commit`，推演里 `simulate`（`gfirst`、`Worker.gang_eta`）；一起停 `Worker.gang_watch`（`gang_peer`、`gang_last`、`gang_when`）、`Worker.gang_stop`、结果 `Worker.gang_end`、收尾 `Worker.finish`；没开跑的跟着失败 `Worker.gang_lost`（`gang_known`、`gang.json`、`known`、`load_prev`）、`Worker.gang_doomed`；写、读 `gang.json` `Worker.gang_recs`、`gang_publish`、`gang_file`、`gang_cur`，主循环重读 `Worker.gang_poll`；时长 `Worker.gang_dur`（`req_dur`）；停不停保活 `Worker.gang_soon` |
| 99 | 放不放一个请求；资源快照；各卡与全停时还能给的显存、内存与核数 | worker 的 `place(x, S, t, pre)`（`_place`）；`Worker.snapshot`、`sim_snapshot`；`card_free`、`ram_free`、`cpu_free`、`cpu_room_for`、`cpu_free_for`；`Worker.mem_room`、`Worker.cpu_room` |
| 100 | 容器里收不回的内存 | `worker.pure.mem_unevictable`（worker `host_info` 与组内服务器 `fleet.parse` 共用；探针 `fleet_probe.sh` 只送字段） |
| 101 | 卡吃不满是不是真有余量；开跑给的核数给少、给多；建议核数封顶；按峰值估核数；`LABMON_CPUS` | worker 的 `gpu_slack`、`cpu_fit`（`gpu_slack`、`cpu_cls`、`cpu_budget`、`Worker.cpu_hints`）、`cpu_from_peak`（`estimate`、`cpu_fit`）；`cpus_export`（`Worker.cpus_for`、`cpu_fit`）；看板 `collect._host_alerts`（`collect._cpu_fit_alerts`、`collect._cpu_room_text`） |
| 102 | 放不下的原因与原因代码；“CPU 不够”里核被谁记着；因 CPU 被挡多久才告警 | worker 的 `WHY`、`why_text`、`why_of`；`cpu_held`（`fit`、`cpu_warm`、`Worker.acct`）；放不下、tag 到上限的判定 `nofit`、`tag_full`；`Worker.note_waits`（`CPU_ALERT_S`）；看板 `collect._lane` 的 `cpu:` 告警、工作台 `alertsOf` |
| 103 | 放行顺序 | worker 的 `fair_key`（`person_use`）、`admit`、`fits_before(t, dur_min, until)`、`shadow`、模块级 `hold_until` |
| 104 | 等待中任务的排位与解释 | worker 的 `admit` 给的 `pos`、`forecast` 记的 `starts`（`pos`、`start`）；页面上 `rankTip`（overview_js/sched.js） |
| 105 | 放行前等了多久 | worker 的 `tick` 定 `since`、`launch` 记 `wait`（`usage.jsonl`）；调度卡的平均等待 `overview.sched_view` |
| 106 | 调度卡 | `overview.sched_view`（`sched`、`person`、`pk`、`users.view`、`overview/usage/<主机>.jsonl`）；页面上 `schedCard`、`schedAct`，一个项目的权重胶囊 `schedW`（`W_STEPS`、`wStep`）与按钮 `schedBtns`（`schedFlag`），已用 / 应得条 `shareBar`（`schedOn`、`personBar`、`schedPeople`、`pkOf`、`who`）（`projSchedSec`、`SMSG`、overview_js/sched.js；`projSchedSec` 在 overview_js/project.js）；`POST /sched/…`（`serve._sched`、`records.tell`） |
| 107 | 公平份额 | worker 的 `usage`、`card_share`（`cards`）、`share`、`own_runs`、`peers`（`share`）；一个人名下各项目的合计 `worker.pure.person_use`（worker `fair_key` 与看板调度卡 `overview.sched_view` 共用） |
| 108 | 调度设置 | `config.sched` → 各主机的 `sched.json`（`overview.collect`、`owner`）→ worker 的 `policy`（`POLICY`）；改 `[sched.*]` 一行 `config._sched_set`，页面上改权重 `config.set_weight` |
| 109 | 项目级的暂停、优先；`pause --now` 的“只停一次”；暂停是谁下的与普通用户能不能动它 | 改 `config.set_sched_ovr`（`cmd_override`、`POST /sched/pause…`、`by`、`[sched.pause_by]`、`config.pause_by`）；只给某几台的谁下的记在那台指令的 `by`（`workerctl.override_each`、`by`、`override add --by`），快照里找它们 `projects.host_ovr`；判断 `users.pause_ok`（`need_pause`），普通用户写那几台时另带 `expect_by`，worker 在 `overrides.json` 的锁里再核一次现状（`--expect-project`）；到期删行 `config.expire_sched`（`overview.collect`）；随 `config.sched` 下发，worker 的 `sched_ovr` 接进 `self.ovr`；“只停一次”是 worker 指令的 `once`（`load_overrides`、`ONCE_HOLD_S`）；调度卡上 `schedBtns`（`schedFlag`）、`flagTip`（overview_js/sched.js），快照 `sched_view` 的 `focus`、`paused` |
| 110 | 看板写给 worker 的文件 | `overview.fetch` 的 `h["put"]`（`overview.collect`） |
| 111 | 推演 | worker 的 `simulate`、`forecast`（状态的 `plan`）、`run_end`（读看板写回的 `eta.json`）；`collect._queued_times`；页面上 `planLanes`、`planEnd`、`stuckWhy`（overview_js/home.js） |
| 112 | 多主机负载均衡的提示 | worker 的 `offers`（`proj_lanes`、`move_cands`、`cant_move`、`try_move`、`Worker.idle`、`plan_lanes`、`list_of`、`list_env`、`peer_st`、`peer_todo`）、同机 `local_offers`（`Worker.lane_offer`、`Worker.local_moves`、`Worker.move_before`、`Worker.moved_past`、`Worker.local_cards`、`Worker.util10`）、`Worker.lane_room`；看板 `rebalance.suggest`（`rebalance.move_key`、`_needs_miss`、`_text`、`_why`、`_est_text`、`_idle_gain`、`_live`、`_fresh`、`rebalance.log_text`）、`rebalance.keep_offers`、`rebalance._under`、`rebalance._room_text`、`rebalance._queued`、`collect._rebalance`；页面上 `rebalMark`、`rebalTip`（common.js） |
| 113 | 工作台上运行中任务的预计结束 | `etaOf`（overview_js/home.js） |
| 114 | 运行中任务剩余时长说不准时的估计 | `worker.pure.overrun_left`（`OVER_FRAC`、`OVER_MIN_S`；worker `run_end` 与看板 `collect._job_times`、`_segment_eta` 共用） |
| 115 | 各卡与纯 CPU 被谁占着 | `overview._keep_usage`、`overview.occupancy`（`/p/<id>/events.json` 传 `skip` 缺省仅 GPU 占卡） |
| 116 | 工作台存的用量记录；存着记录的主机 | 路径 `state.usage_file`；`state.usage_hosts`（`waits.JobWatch.check`、`collect._attempts`）；项目页的失败尝试 `collect._attempts` |
| 117 | 等待原因的结构化字段 | worker 等待项的 `miss`（`place`）、`local[].how`（`copy`、`redo`、`mixed`）；看板按字段认，不按 `why` 文字 |
| 118 | 容量单位 | `worker.pure.GIB`（`config.GIB` 与 worker 共用）；页面上 `GIB`（common.js） |
| 119 | 时间线 | common.js 的 `tlInit`、`tlRange`、`tlBind`、`tlSvg`、`tlShow`、`jobBar`、`tlPack`、`tlRowLayout`、`tlOverflow`、`tlMoreBar`、`occBars`（`OCC_ST`、`OCC_CARD_LAY`、`OCC_OTHER_LAY`、`occItems`）；拖动布局锁 `tlFrame`、`tlLock`、`tlUnlock`、`tlLockFlag`、`tlLockRow`、`tlLockLw`；「全部」要包住的区间 `tlSpan`；近景按现在前后的任务定宽 `tlFit`（`TL_FIT`、`near()`）；排在谁后面 `afterText` |
| 120 | 项目页时间线的依赖连线 | `depLines`（`depEdges`、`depRoute`、`depVia`）、`depChain`、`depOn`、悬停与点 `depHover`（`DEPH`）；条的位置、行号由 `laneRow` 记进 `c.pos`（page.html） |
| 121 | 触屏上点一格：第一次只看，3 秒内再点同一格才动作 | `tapTwice`（page.html、`gsqClick`） |
| 122 | 自动归组 | `worker.pure.GROUP_RE`（`config.AUTO_GROUP_RE` 用于 `collect._group_of`、`config.set_project`，worker `duration` 借同前缀估时比值共用） |
| 123 | 按估计字宽截断文字与同一估计下的字宽；项目页时间线条上的任务名 | `fit`、`textW`（common.js）；`barName`（page.html） |
| 124 | 正在操作；点了不能打字的地方先让输入框失焦 | common.js 的 `editing`（`typingEl`）与它下面捕获阶段的 click 监听；工作台 `selecting`（overview_js/app.js），项目页 `redraw`（page.html） |
| 125 | 拖动排顺序 | common.js 的 `sortBind`（`SORT`、`sortMove`、`sortEnd`）；计划项排顺序经它（overview_js/project.js） |
| 126 | 本机存的设置 | `mkStore(前缀, 存储)`（common.js）给 `get(k, d)` / `set(k, v)` |
| 127 | worker 读的用量历史；写一行 | worker 的 `load_hist`；`Worker.usage_rec`（`finish`、`gang_doomed`） |
| 128 | 任务的进程归集、CPU、读写与挂起测量；一棵进程树这一段用掉的 CPU | `worker.pure.proc_tree`（支持 `pgids` 与 `exclude`，任务与保活共用）；worker 的 `scan_procs`（含 `pgid`）、`job_procs`、`measure`（`proc_io`、`PEAK_WIN`、`use.sus_s`）、`job_sus`（`usage_rec`、`finish`、`run_end`、`run_tot` 共用）、`cpu_since`（`Worker.ka_cores`）；页面上 `ioText`（overview_js/home.js） |
| 129 | 主机的 CPU 与读写压力；共享盘慢的基线；共享盘此刻慢不慢 | worker 的 `Worker.press`（`press_raw`、`press_rates`、`nfs_counts`、`local_dev`、`disk_counts`、`psi`）、`io_base`、`nfs_slow`（`info.press.nfs.slow`、`sample`、`Worker.slow`）；看板曲线 `overview._series` 的 `thr`、`nfs`、`busy`、`anon` |
| 130 | 容器 CPU 限流与超核的任务 | worker 的 `thr_step`（`Worker.press`、`thr_since`、`load_prev`）、`cpu_over`、`Worker.throttle`（`throttle`）；看板项目告警 `collect._host_alerts` 的 `throttle:`；页面上 `thrText`、`thrTag`、`thrOver`（overview_js/core.js、`alertsOf`） |
| 131 | GPU 利用率的两种口径；近 24 小时平均；项目自己的平均利用率；页面上的利用率文字 | worker 的 `win_mean`（`Worker.util_window` 记 `uwin`、`bwin`；状态的 `util10`、`gpu_busy`（`Worker.busy_view`））；看板 `overview.gpu_busy`、项目快照的 `hosts[].busy`、`history[].busy`（`collect._hosts`）、`collect.gpu_mine`（`u`）；自用主机样本的 `gpus[].busy`（`fleet.from_worker`）；页面上 `utilText`、`utilAvg`、`utilMine`、`gpuBusy`（common.js），组内页取样本的 `fUtil`、`fCardU`（overview_js/fleet.js） |
| 132 | 共享盘慢时推迟读写共享盘的事 | worker 的 `Worker.io_yield`（`Disk.step`、`defer`、`stage_bg`、`disk.yield`）；页面上 `diskProg`（overview_js/storage.js） |
| 133 | 读写拥塞嫌疑 | worker 的 `Worker.io_suspect`（`duration`、`same`、`io_sus`、`peers`、`peer_box`）；事件种类 `io_suspect` |
| 134 | 清单的 `cpus=`、`io=`提示与开跑导出的 `LABMON_CPUS`；核数取整与记账上限 | worker 的 `hints`、`job_cpus`、`cpu_cap`（`cpu_quota`）、`cpu_env`（`THREAD_VARS`、`Worker.start`、`slot_main`）；页面上 `hintText`、`estText`（overview_js/home.js） |
| 135 | 任务日志里这一次的输出 | worker 的 `START_RE`、`log_tail`（`fold`）；探针另有一份 `START_RE` 与去重（probe.py） |
| 136 | 外来 GPU 进程；独占任务会被谁打扰 | worker 的 `sample`、`other_cpu`、`intruders`、`void_check`、`void_job` |
| 137 | 停下重排的任务与发停止请求 | worker 的 `note_stop`（`voided`、`how`）、`send_stop` |
| 138 | 借来的主机上约定归还挡住、这台不会再放的清单 | `borrow.late_jobs`（`borrow.check`、`borrow.late`）；该怎么办的那句 `borrow.late_do`（按截止来自约定还是对方租期；给会话的写进快照主机的 `borrow.late_do`（`borrow_cut`、`msg`、`borrow_due`），`user=True` 给用户的写进 `borrow.late_say`（`borrow_cut`、`alertsOf`、`borrow_due`））；`collect._lane` 的 `borrow_cut`，页面上 `alertsOf`（overview_js/core.js） |
| 139 | 对方轻用时能不能借、对方在用什么 | `borrow.can_borrow`、`borrow.using` |
| 140 | 给对方留的量 | `borrow.reserve_for`、`borrow._keep`、`borrow.keep_text`；页面上 `keepText`（common.js） |
| 141 | 借来的主机上对方的用量、超出预留、扣预留准入、说好不用的时段与作废、快撞上限、被挤掉、OOM 先杀我们 | worker 的 `other`（`sample`）、`guest_scan`（`cpu_hot`、`away_base`）、`guest_block`、`held`、`keep_left`、`away`、`end_away`（`away_state`）、`guest_tight`、`squeezed_by`（`oom_of`）、`oom_check`、`GUEST_ADJ`；看板写、清 `borrow_away` 在 `borrow.begin`、`borrow.check` |
| 142 | labmon 之外的内存与 CPU 要扣掉的保活 | `worker.pure.proc_tree`（worker 与 `fleet.analyze` 共用）；worker 的 `mem_of`、`Worker.ka_cores`（`ka_roots`）；组内服务器的 `fleet.analyze`（`mem_ka`）、探针 `fleet_probe.sh` 的 `pss` 段（字样由 `fleet.fetch` 传） |
| 143 | 后台采集线程 | `serve._Collector` |
| 144 | 页面连不上看板服务 | `api`（`NET_WAIT`）、`netText`、`pollNet`、`offBadge`（common.js） |
| 145 | 看板服务接受哪些请求；页面上的写请求 | `serve.Handler._gate`（`serve.host_ok`、`hosts`、`serve.allowed_hosts`）；页面上 `postJSON`（common.js）；测试里 `tests/fixtures.post_req`；`tools/deploy.py` 的 `http` |
| 146 | 看板的账号、会话与令牌；请求是谁、能不能做；能不能做平台操作；命令行改账号 | `users.check`（`users.ident`、`SCOPES`、`IMPLIES`、`ROLE_SCOPES`）、`users.can_ops`；登录、设置码、改口令 `users.login`、`setup`、`change_pw`（`_attempt`）；`users.token_add`、`revoke`、`add`、`set_role`、`reset`、`remove`、`listing`；读改写 `users._edit`；命令行 `__main__.cmd_user`（`users.local_ok`） |
| 147 | 每条路由要什么权限；两个入口；没登录跳登录页；会话 Cookie 的属性 | `serve.ROUTES`、`serve.route_of`、`serve.Handler._allow`（含 `GET /fleet/[^/]+\.json` 的 `own="fleet_srv"` 与 `GET /api/disk`）；`serve.login_url`、`serve.safe_next`；`serve.Handler._cookie` |
| 148 | 项目主人、主机允许谁用、普通用户能操作哪些对象；普通用户名下最多几个项目；对象鉴权只在一处定 | 项目主人 `users.owner_of`（`meta.json`、`projects.owner`、`projects.set_owner`）；登记新项目前 `users.need_room`；`config.host_users`、`config.set_host_users`；`serve.Handler._own` 按 `serve.ROUTES` 调用 `users.need_project`、`users.need_host`、`users.need_list`、`users.need_reg`、`users.need_room`、`users.need_pause` 与 `users.need_holder`（`fleet_srv`），同一份记成 `Handler.guard`，要在锁里再核的业务函数（`check_owner`、`api.override`、`check`）只调它 |
| 149 | 普通用户看到的全局快照与记录 | `users.access`、`users.view`（`/`、`/overview.json`、`VIEW_KEYS`、`disk.person_storage`、`users._self_fleet`）、`users.visible`（`records.records`、`see`） |
| 150 | 个人推送 | `service.set_user_push`、`service.user_push`、`service.push_view`、`service.user_push_test`、`service.notify`；解析与发送复用 `push` |
| 151 | 客户端分发；最低版本与报告的版本；wait 自动更新；清旧版本目录；skill 随客户端分发与换版本后叫醒会话 | `dist.wheel`、`dist.install_sh`、`dist.install_wheel`（`dist.Installed`）、`dist.version`；`serve.Handler._client`；`client.install_update`（`auto`）；装不上的记录 `dist.note_failure`、`dist.failed_wait`、`dist.clear_failure`；清旧版本 `dist._prune`（`dist._version_dir`、`dist.hold_version`、`__main__.main`）；`dist.client_status`；`users.ident`；`__main__._wait_update`、`_wait_resume`、`_wait_outdated`；skill 入口 `dist.skill_entry`、`dist._link_skill`（`_agent_entries`）、`dist.ensure_skill`，比对 `dist.skill_changes`、那句话 `dist.skill_note`（`client.update`、`__main__._skill_woke`） |
| 152 | 页面上的身份与按它收起按钮；401 回登录页 | `users.me`（`serve.Handler._page`、`collect.fill`）；页面上 `canOps`、`canWrite`、`canWeight`、`opsNote`、`toLogin`（common.js）；登录页 `labmon/login.html`；人员的账号与我的账号 `ACC`、`accLoad`、`accPost`、`accAct`、`directoryDrawer`、`mePage`；共用会话与令牌行 `accItem`、一次性原文 `accShow`（overview_js/account.js，`directoryDrawer` 在 overview_js/fleetops.js） |
| 153 | 共享盘用量；扫描的节奏；按项目与按人投影 | worker 的 `Disk`（`step`、`start`、`done`、`want`、`forced`）、`Worker.disk_ends`；`disk.update`（`_ours`、`_job_writes`、`_write`、`_burst`）、`disk.project_storage`（`storage`、`labmon disk`）、`disk.person_storage`（`VIEW_DISK`、`VIEW_DISK_ALERTS`；`users.view` 与 `api.my_disk` / `labmon disk --me` 共用）；告警 `collect._storage`（`collect.write_groups`、`collect.write_text`、`collect.gb`、`bigwrite:`、`labmon disk`、`collect.dir_name`、`dirName`、`storageChip`、page.html）；拍与拍之间接着做 `Worker.scan_chain`、两种扫描出错共用的 `Worker.disk_do`；限速 `Pace`（`Disk.rate`）；共享盘上的量的文字 `GB`、`dGB`（common.js）；页面上 `diskBarSelf`（overview_js/storage.js） |
| 154 | 本地盘开机以来的新写入 | worker 的 `rootfs_info`（`info.rootfs`）、`LocalDisk`（`scan_units`、`scan_unit`、`prune`、`since`、`storage`）；组内服务器的探针 `fleet_probe.sh` 的 `rootfs`、`shm` 行与 `scan` 段，本机 `fleet._scan`、要不要扫 `fleet.scan_due`、存进状态 `fleet._wrote` |
| 155 | 本地盘的新写入合成一份与那一句话 | `fleet.local_writes`（`fleet.server_lw`、`lw`、`overview._annotate`）、`fleet.lw_text`（`_risk`、`renew`）；页面上 `lwText`（`tests/pages/check.js`、`test_fleet`）；借来的主机归还时说借用期间写的 `borrow.wrote_note`，页面上 `lwLeft` |
| 156 | 本地盘的画法 | 页面上 `lwBar`、`lwSegs`、`lwVal`、`lwBlock`、`lwHeat`、`lwPath`、`hostLw`（overview_js/storage.js） |
| 157 | 共享盘增长的关注档 | 页面上 `diskHeat`、`heatTag`、`heatNum`（`HEAT_HI`、`HEAT_MID`、`burstOf`、overview_js/storage.js） |
| 158 | 保活守护判定 | `fleet.guard_state`、`fleet.guard_act`；页面上 `guardText`、`gdSit`、`probeAt`、`gdIcon`（overview_js/fleet.js） |
| 159 | 在组内服务器上开、停保活；这一台开、停保活的命令 | `fleet.ka_start`、`fleet.ka_stop`（`fleet_guard.sh`）；`fleet.ka_cmd`；重开 `fleet_guard.sh` 的 `repair`，缺口 `guard_gap`（`fleet.guard_act`、`fleet._guard_track`、`fleet.guard_state`） |
| 160 | 停启保活的脚本 | `labmon/ka.sh`（`ka_procs`、`pgids`、`signal_ka`）；worker 经 `Worker.ka` 调（`KA_SH`、`KA_ENV`）；发出去在 `workerctl.run_out` 的 start |
| 161 | 卡上借用期间 worker 开的保活：认出来、登记成看板的保活 | worker 的 `Worker.ka_mark`（`keepalive.mine`）；`fleet.ka_adopt` → `fleet_guard.sh` 的 `adopt`（`ka_logged`、`register`、`start`）；归还 `borrow.finish`，补登记 `fleet.guard_state` 的 `adopt`（`_adopt_due`、`ka_group`） |
| 162 | `ka.sh running` 的读法 | `fleet_guard.sh` 的 `running_groups`（`strict`、`kacheck`）；worker 的 `Worker.ka_groups` |
| 163 | 进程出生时钟 | `fleet_guard.sh` 的 `proc_birth`；探针 `fleet_probe.sh` 的 `birth`（`p1`、`ka_groups`、`guard`）；比对在 `fleet.parse`（`guard.identity`）；worker 要 epoch 秒时用 `started_at` |
| 164 | 所有卡利用率为零 | `fleet.parse` 的每卡 `zero`、`fleet.analyze` 的 `zero_cards`（`guard.zero_cards`、`manual_guard`）；远端动手前复核 `fleet_guard.sh` 的 `gpu_zero` |
| 165 | 组内服务器 GPU 负载归类与按路径归人 | `fleet.classify`、`fleet.mark_keepalive`、`disk.whose`（`disk.owner_of`） |
| 166 | 进程跑的是哪个脚本、相对路径按工作目录还原；ka.sh 管的保活；借用前逐个核对 | `worker.pure.script_arg`（worker 的 `script_of`、`ka_owned` 与看板 `fleet._abs_script` 共用）；worker 的 `ka_owned`；`labmon/ka.sh` 的 `ka_procs`（`ka_owned`、`tests/test_ka.py`）；探针 `fleet_probe.sh` 的 `script_abs`；借用核对 `fleet_guard.sh` 的 `kacheck`（`ka.sh running`、`running_groups strict`），结果的话 `borrow._ka_miss` |
| 167 | 固定配色 | `config.pick_color` |
| 168 | 页面渲染；往页面里填一块数据 | `collect.render_page`（`_inline_js` 按 `/*__JS:…__*/` 内联 `common.js` 与 `overview_js/*.js`）、`collect.write_page`；`collect.fill` |
| 169 | 页面上的租期文字 | `leaseLeft`、`leaseSoon`、`leaseRem`、`leaseText`、`leaseHtml`（overview_js/core.js） |
| 170 | 悬停提示 | common.js 的 `TIP`、`placeTip`、`showTip`、`flashTip`、`hideTip`、`tipCheck`、`tipBind` |
| 171 | 工作台的导航 | `navModel`（overview_js/core.js），`side`、`tabbar`、`morePanel` 只管画 |
| 172 | 换地址与浏览器历史 | `nav`（overview_js/core.js），链接与行的点击都经它；例外是换人后抽屉跟到新的那台 `srvMoved`（overview_js/fleet.js，`history.replaceState`） |
| 173 | 测试里的多主机；不读本机 /proc 与 cgroup；不连 ssh；用闸门看运行中状态；git 不认继承的 `GIT_*` | `tests/fixtures.Hosts`（`Clock`）；`tests/fixtures.no_host`（`worker_env`、`worker_module`）；`tests/bin/ssh`（`fixtures.SSH_GUARD`）；`tests/fixtures.gate`、`release`（`WorkerBase.usage`）；`tests/fixtures.no_git_env`（`setUp`、`setUpModule`） |
| 174 | 测试里起的看板服务要登录；离线预览按身份看 | `tests/fixtures.make_token`、`login`、`bearer_opener`；`tests/pages/preview.py --as`（`accounts`），页面测试的身份 `ADMIN_ME`（`tests/pages/dom.js`） |
| 175 | 部署到看板；哪些改动不用重启、要拉起 worker 或要浏览器核对页面；一次启动以来的 Traceback；页面文字漏 NaN/undefined/Infinity | `tools/deploy.py` 的 `deploy`（`_update_remote`、`make_bundle`、`wait_new`）；服务端与两种模式共用的判断在 `labmon/deploy.py`：`is_doc`、`is_worker`、`has_worker`、`is_page`、`log_text`、`tracebacks`、`apply`、`status`、在跑哪个提交 `RUNNING`；`leak`（`tests/pages/dom.js`、`check.js`、`smoke.js`） |
| 176 | 一台主机上除本项目外其他项目的聚合负载与提示（不含他人明细） | `worker.pure.other_load`、`worker.pure.other_load_text`（`fit.fit_list` 与 `api.status` 共用） |
| 177 | 项目页主机的 CPU 实时占用与时间线同轴折线 | `collect._host_cpu`（`hosts[].cpu`、`history[].cpu_pct`）、`collect._add_hourly`（`util_hourly`、`cpu_hourly` 共用）；页面上 `cpuView`、`hostSeries`、`tlHostHover`（page.html） |
| 178 | 主机分类、独立主机判定与共享域隔离 | `config.host_platform`、`config.host_share`、`config.is_standalone`、`config.host_ssh_key`、`config.home_key`、`config.shell_path`；页面上 `standaloneBadge`（overview_js/core.js） |
| 179 | 独立主机清单目录防污染边界校验 | worker 的 `queue_root_ok`（`worker/cli.py` `add` 经 `realpath` 校验）；看板前置 `workerctl._check_standalone_root`（`workerctl.worker_env` 透传 `LABMON_DATA_ROOT`/`LABMON_LOCAL`/`LABMON_STANDALONE`/`LABMON_ALLOW_OUTSIDE_ROOT`） |
| 180 | 推模式临时主机与云盘存储监控 | 服务端 `ephemeral.py`（`push`、`tick`、`duplicate_jobs`、`overview_hosts`、`project_hosts`、`as_probe`、`online_for_project`、`running_for_project`、`remove_project`）；零依赖客户端 `labmon/labmon_ephemeral.py`（`check_drive_mount`、`collect_storage`、`EphemeralTracker`、`track`、`run_command_wrapped`）；接入 `serve.ROUTES`（`POST /api/ephemeral/push`、`GET /api/client/ephemeral.py`）、`collect.parse_ephemeral_queue` / `_ephemeral_alerts`、`overview.collect` / `check_project_idle`、`projects.archive_block` / `delete`、`users.access`、`api.status` / `disk`、`waits.job_state`；前端 `hostShort`（common.js）及 `overview_js/`、`page.html` 临时分支 |
| 181 | GPU 主机的 GPU 保底 CPU 核、纯 CPU 自动封顶与降 nice、GPU 有限弹性放行 | worker 的 `job_ngpu`、`snap_ngpus`、`gpu_cpu_reserve`、`cpu_limit`、`cpu_room_for`、`cpu_free_for`、`gpu_cpu_flex`、`cpu_only_nice`（真放行 `place`、推演 `simulate`/`forecast`、`fit`、`offers`、`cpu_give`、`cpus_export`、`acct_job`、`cpu_fit`、`cpu_line`、`Worker.start`/`grant` 与 `slot_main` 共用） |
| 182 | 独立主机跨机历史转送与隔离 | 看板 `overview._keep_usage` + `overview._hist_payload`（按主机归属过滤防泄露）经 `overview.fetch` `put`（stdin 避 `ARG_MAX`）下发 `hist_shared.json`；worker `worker/common.py` `load_hist` 标 `shared_hist=True` 仅喂 `estimate`/`duration`，`share`/`dep_state`/`disk_ends`/`_idle_start` 显式跳过 |
| 183 | 独立主机本地工作盘准入闸门（含滞回） | `worker/measure.py` `MeasureMixin.disk_events`（常量 `DISK_WARN_GB=20`、`DISK_STOP_GB=10`、`DISK_HYST_GB=2`，记 `disk_gate` 事件），`worker/core.py` `place` 的 `pre()` 拦截；告警在 `collect._storage`（`disk_low:<hid>:<root>`）与页面 `alertsOf`（overview_js/core.js） |
| 184 | 独立主机本地工作盘扫描、可回收判定与画法 | worker `worker/disk.py` `WorkDisk`（复用 `Disk` + `Pace(LOCAL_IOPS)`，`follow_symlinks=False`，仅扫 `<data_root>`）；看板 `disk.host_work_disk`（14 天闲置 + `>= 1 GiB` + 无活跃队列 → `reclaimable`）与 `disk.project_storage`（`standalone` 字典）；页面 `workDiskBar`/`workDiskBlock`（overview_js/storage.js）、`storageChip`（page.html）与 `api.disk` |
| 185 | 自管本地缓存/临时目录清理 | `worker/cli.py` `clean_local`（默认 `--dry-run`，显式 `--yes` 才删，严格 `realpath` 限定 `<LOCAL>/{cache,jobs}`、跳过运行中任务）；`workerctl.run_out`、`api.worker`、`serve.ROUTES` `POST /api/worker/clean`、`__main__` `labmon worker clean` |
| 186 | 外部主机双入口配置、安全边界与专用密钥管理 | `exthost.py`（`validate_target`、`add_external`、`edit_external`、`remove_external`、`test_external`、`key_external`）+ `config.add_host`/`edit_host`/`remove_host`（过 `HOSTS_LOCK` + `lease.need`）+ `users.need_external_host`/`is_external_host`/`USER_EXT_HOSTS=3` + `platform.ensure_ssh_key`/`ssh_key_info`/`del_ssh_key`/`ssh_auth_cmd` + `lan.host_ssh_argv`/`close_host_ssh`；路由 `serve.ROUTES` `/api/hosts/external*`；CLI `__main__.cmd_host`（`add/edit/test/key/rm`）；GUI `extHostDrawer`/`myExtHostsSec`（overview_js/account.js）与 `common.js` `sendJSON` |

## 现有的兼容代码

[AGENTS.md「一个概念只有一处实现」第 3 条](../AGENTS.md#一个概念只有一处实现硬约束)允许暂留、到期就删的兼容代码（旧数据补不出来的留到期；能一次迁完的迁完就删）。删了的从这里一并去掉，新留的补进来：

- 11-23 页面上内存估计来源没有 `ram_src` 的回退（overview_js/home.js `ramSrc`，#61 之前放行、被新 worker 接管的运行中任务沿用旧 `est`，跑完就没了）。
- 11-23 用量行没有 `conc` 的回退（`labmon/worker/sched.py` `rec_conc`，#67 之前的行：同卡没别的任务的当 1、其余不知道，推不出来）。
- #19 部署那次的迁移（`users.migrate_people` 与它用的旧对应规则 `users._legacy_people`、`serve.serve` 里调它的那几行：#70 之前的账号按主机清单 `[fleet.account_people]`、同名、快照历史认出的人写进账号库）：主与备用的看板服务各启动过一次就删。
- #161 部署那次的迁移（把 launchd plist 里旧的 `-c` 登记进来：`projects.adopt`（连同 `OUT_NAMES`）、`serve.serve` 的 `legacy`、`service._read` 的 `legacy` 与 `service.install` 里迁移的几行、`__main__` 传 `legacy` 的那处）：部署后跑过一次 `labmon service install` 就删。
- #147 部署那次的迁移（`collect._last_probe` 把旧探针缓存 `last_probe_*.json` 里单个对象的 `current` 迁成列表）：部署后各项目采过一轮就删。
- 2026-12-27 项目页旧快照缺 `hosts[].cpu` 的回退（`page.html` `cpuView` 回退读 `h.cpu_pct` / `cgroup.cpu_quota`）与旧状态缺 `cpu_h` 的回填（`collect._long_term`）：#208（2026-11-27）部署后活跃项目采过一轮即不再产生，2026-12-27 后可删。
- 2026-12-28 人员页 30 天用量汇总跳过旧算力池借用方（`fleetview._by_person` 跳过 `POOL_LABEL`）：#212 阶段 1（2026-11-27）部署后 `hours/*.jsonl` 不再产生 `bw["算力池"]`，30 天统计窗口滑出即 2026-12-28 后可删。

## 各概念的口径

表里每一行原来写在 AGENTS.md 的完整说法（概念的口径、调用关系、取舍的来由与 Issue 号）。改了行为时同步改这里与对应的 `docs/`。

1. **ssh 参数与执行**：ssh 参数与执行（共用 ControlMaster 长连接）
   实现：`lan.ssh_argv`、`lan.ssh_run`
2. **传到服务器上留着用的文件只取 main 上已提交的版本**：传到服务器上留着用的文件（`labmon_worker.py`、`worker/*.py` 与随它发的 `ka.sh`、共享盘 README）只取 main 上已提交的版本（分支上没合并的、工作区没提交的都不带）
   实现：`workerctl.committed`（`DEPLOY_REF`、`committed_tree`、`worker_bundle`）
3. **本机在不在内网；按主机并行探测**：本机在不在内网；按主机并行探测（一键一台，各走自己的长连接；有远程连不上时再查一次是不是刚离开内网）
   实现：`lan.check`、`lan.gather`
4. **整文件覆盖写**：整文件覆盖写（写临时文件再改名；要 600 的传 `mode`，临时文件一建出来就是这个权限）
   实现：`state.write_atomic`；`save_json`、`collect.write_page`、`users.json` 都经它
5. **本机状态根目录与其下的状态目录 `overview/`**：本机状态根目录（`LABMON_STATE`，缺省 `~/.local/state/labmon`；`tools/issue_watch.py` 的已见列表也放这里）与其下的状态目录 `overview/`
   实现：`state.state_root`、`state.state_dir`
6. **按文件缓存读出的内容时判文件变没变**：按文件缓存读出的内容时判文件变没变（(inode, 修改时刻纳秒, 大小)；只比 `st_mtime` 时同一时间戳粒度里连写两次会漏读第二次，#31）
   实现：`worker.pure.file_sig`（看板 `state.file_sig` 直接复用：`disk._rows`、`fleetview._hours`、`users._read`、`waits._sig`、`serve.Project._mtimes` 等；worker `fsig` 传 `strict=True`：`load_hist`、`queue_durs`、`load_eta`、`list_of`、`list_env`、`qenv`）
7. **JSON 状态文件；工作台的全局快照**：JSON 状态文件；工作台的全局快照（上一轮 `overview.collect` 写的）
   实现：`state.load_json`、`state.save_json`；`state.snapshot()`
8. **JSONL**：JSONL（逐行记录、按保留期修剪；续写的互斥）
   实现：逐行解析共用 `worker.pure.parse_jsonl`（看板 `state.parse_rows`、`state.load_rows`、`state.append_row` 内有按文件的 `_lock`；worker `load_jsonl`、`append_jsonl` 用量记录、事件、主机压力历史共用，互斥由调用方加 `flock`）
9. **组内服务器状态目录里一个 JSON 的读改写**：组内服务器状态目录里一个 JSON 的读改写（`op.json`、`boot.json`、`people.json`；平台操作线程、HTTP 线程与本机命令行都能改，读改写跨进程互斥）
   实现：`fleet.edit_state`
10. **时刻的文字与解析**：时刻的文字与解析（页面文案、记录、通知、主机清单、命令行里的时刻；worker 写出的一律 epoch 秒）
   实现：`config.fmt_time`、`config.parse_time`（缺省只认绝对时刻：主机清单、记录、`returned` 等落盘的文字都按它读）；相对写法（今天 / 明天 / 后天 HH:MM、只写 HH:MM）只在命令行认，由 `__main__.parse_until` 与 `host return --at` 传 `rel=True`，写进清单前换成带年份的；页面提交的都是 epoch 秒。命令行的 `+30m`、`+3h` 与到期语义（只写日期取当天结束、不带年份的早于现在半年以上才算明年）在 `__main__.parse_until`。worker 与探针（`probe.py`）在服务器上跑、不 import 本包，各自一份
11. **项目快照多久没更新算过期**：项目快照多久没更新算过期（max(3×采集间隔, `stale_min`) + 1 分钟；`labmon wait` 的 `collector_stale`（含 `--job` 等别的项目的）、项目页顶栏“N 分钟未更新”标红、「项目配置」那一行的说法同一个门槛）
   实现：`config.stale_limit`（系数 `STALE_RUNS`、`STALE_PAD_S`；`waits.collector_stale` 调它；`labmon wait` 连不上看板多久提示一次、连回来报 `server_back` 也按它（满 `WAIT_OFFLINE_MAX_S` 才以 `server_unreachable` 退出，#154），`__main__.cmd_wait` 用本地 monitor.toml 的 `config.brief`）；项目快照的 `thresholds.stale_s`、配置摘要的 `stale`（`serve.Overview.cfg_info`）下发；页面上 `cfAlerts`（overview_js/project.js：没改草稿取 `stale.s`，草稿改了采集间隔或这项时拿 `stale` 的系数当场套）
12. **时刻要在将来**：时刻要在将来（借用的约定、优先与命令行的到期；已经过去的拒绝，提示里的说法用参数传）
   实现：`config.check_future`；`parse_until` 把它的 `ConfigError` 转成退出
13. **页面上的时刻文字：月-日 时:分、月-日、时:分、今天只写时分**：页面上的时刻文字（都是 epoch 秒，空值“—”）：月-日 时:分、月-日、时:分、今天只写时分（按看页面时的今天算）
   实现：`ts`、`tsMD`、`tsHM`、`tsNear`（common.js）
14. **时长文字**：时长文字（秒；1 分钟内写秒、90 分钟内写分钟、两天内写小时、再长写天）
   实现：页面上 `dur`（common.js），项目快照里的 `*_min` 字段在调用处乘 60；看板 Python 侧的文案（告警、建议）`config.fmt_dur`，同一口径
15. **看板服务交给系统托管**：看板服务交给系统托管（macOS launchd 的 plist、Linux systemd 用户单元：写、读回参数、重启、过几秒重启、停；`service.MANAGER` 定用哪个，#164）
   实现：`service._write_and_restart`、`service._args`（`_read`）、`service.restart`、`service.restart_later`、`service.stop`、`service.installed`
16. **服务日志的一行；系统通知**：服务日志的一行（`service.LOG`：macOS `~/Library/Logs/labmon.log`，Linux `<状态根>/labmon.log`；serve、borrow、renew 共用）；系统通知（只收给用户的文字：macOS 弹窗，`notify.toml` 配了的另在后台线程推到手机（Bark 或 ntfy，#159），失败只记日志；`LABMON_NO_NOTIFY` 时都不发）
   实现：`service.log`；`service.notify`（推送配置 `service.push_conf`，试推 `labmon notify test` 即 `service.notify_test`）；推送本身（读 `notify.toml`、推一条、Bark 加密）只在 `labmon/push.py`（`push.load`、`push.one`、`push.target`、`push.err`；只用标准库、不 import 本包，TX 的看门狗拷它用，#165）
17. **秘密**：秘密（平台密码、平台登录令牌：macOS 钥匙串，Linux `~/.config/labmon/secrets/<种类>/<账号>`（600）；取、存、删、在终端输入；`LABMON_NO_KEYCHAIN` 时都不碰，`LABMON_SECRETS` 改目录，#164）
   实现：`platform.secret`、`platform.set_secret`、`platform.del_secret`、`platform.prompt_secret`（`labmon secret`）；平台操作登录取密码 `platform.password`
18. **看板服务所在机的体检**：看板服务所在机的体检（只在 WSL 里启用：`LABMON_NO_HOSTCHECK` 时、笔记本上都不启用；Windows 侧经 `powershell.exe` 全路径读，待装更新的离线搜索脚本只有一份 `tools/win/labmon-wu.ps1`，自己跑与 SYSTEM 计划任务共用；各项从何时起；提醒门槛 `LIM`；白天、每个本地日期一次地推，#166）
   实现：`hostcheck.enabled`、`hostcheck.tick`（`run_ps`、`read_win`、`read_updates`、`read_wsl`、`sample`、`judge`、`due`）、`hostcheck.view`（全局快照的 `hostcheck`）；页面上 `svcCard`（overview_js/home.js）
19. **看板服务的计划维护**：看板服务的计划维护（`labmon server maint`：期间体检不推、TX 的看门狗不报，最长 1 天，#165）
   实现：`maint.until`、`maint.start`、`maint.stop`（`overview/maint.json`）；读它的：`serve.health`、`hostcheck.loop`（传给 `tick`）、`hostcheck.view`（小卡上那一行）
20. **服务租约（这台是主还是备用；备用时不做的主动动作都先过它）**：服务租约：这台看板服务是主还是备用（每个 `worker_home` 下的 `labmon_server.json`，远端 `flock` 里读改写；续约夹在 `overview.fetch` 那条 ssh 里、拿到了才写 put；读不到照本机标记 `server.json`；分开时 id 大的放；停服务 SIGTERM 时释放；#163）；备用时不做的事都先过它：采集、组内服务器、平台操作、改 worker 与临时指令、借用、通知与体检推送、看板的写请求
   实现：`lease.remote_sh`（`overview.fetch` 用，读回的那段 `overview.lease_part`）、`lease.decide`、`lease.tick`（备用时每轮）、`lease.release`、`lease.take`、`lease.set_marker`；门 `lease.primary`（`fleet.tick`：看板线程与 `labmon fleet probe` 共用）、`lease.need`（`collect.collect`：项目线程与 `labmon collect` 共用；命令行与 `workerctl.run_out` 的 `CHANGE`、`override_each`、`borrow.begin`）；看板的写请求 `serve.Handler._standby`（POST、PUT）、`lease.flag`（`/healthz`）；页面与快照的 `lease.view`、那句话 `lease.text`（横幅 `standbyBar`，overview_js/app.js）；备用时照收的写请求 `serve.standby_ok`、发页面时现填 `serve.live_snapshot`
21. **冷备与恢复**：冷备与恢复（打包状态根与主机清单，跳过本机身份、日志、锁；不含秘密；笔记本经 `GET /api/admin/backup` 取、留 `KEEP` 份、定时；恢复时旧的挪到旁边、保留这台身份与主机清单的 `[server]` 段（#197））
   实现：`backup.make`、`backup.make_bytes`、`backup.pull`、`backup.schedule`、`backup.restore`（命令行 `labmon backup`、`labmon server restore`；换回这台的那一段 `config.keep_section`，`backup.MINE`）
22. **看板服务的两个入口绑哪、内网入口的 TLS、各认哪些 `Host`；`Host` 写法的解析**：看板服务的两个入口绑哪、内网入口的 TLS、各认哪些 `Host`（主机清单的 `[server]`，#165；端口只在命令行）；`Host` 写法的解析（`主机[:端口]`，不带端口按缺省端口）
   实现：`config.server`；`config.host_pair`；本机命令行与部署脚本怎么连内网入口（HTTPS 时只信那张证书）`config.lan_url`（只信一张证书的 TLS 上下文 `config.pinned`，部署脚本远端模式的 `ca` 也用它）；`serve.allowed_hosts`、`serve.host_ok`、`serve.tls_wrap`（握手在 `Handler.setup`）
23. **探活；TX 上探它、异常 15 分钟推、恢复再推的看门狗**：探活（`GET /healthz`，不要登录，只回 `ok`、`primary`、`last_collect`、`maint_until`）；TX 上探它、异常 15 分钟推、恢复再推的看门狗
   实现：`serve.health`；`tools/tx/labmon-watch.py`（`judge`、`step`、`commit`）
24. **服务端写的跳转与链接回看板根的相对地址**：服务端写的跳转与链接回看板根的相对地址（反代加了路径前缀也对；页面上的请求与链接同样只写相对地址，`test_pages.test_urls_relative` 扫模板）
   实现：`serve.root_rel`（`serve.login_url` 用它）
25. **PID 文件；状态目录的 `current`；别的主机放的这台不动**：PID 文件（lock、worker.pid、保活登记；没有或写坏了当作没有）；状态目录的 `current`（一行一个在跑的任务：名称、PID、开始时刻、放行的主机（11-10 起，#88）；并行清单有几行，#147；开跑追加一行、收尾删自己那行；接管、旁观旧 `gpu_queue.sh` 共用）；别的主机放的这台不动（#88）
   实现：worker 的 `read_pid`、`read_current`（回列表）、`drop_current`；`run_elsewhere`（`requests` 调，等待项记 `elsewhere`；池清单 `Worker.pool_todo` 也调，`pool_heads` 记 `elsewhere`，#80）；看板探针（probe.py，不 import worker）另读一份 `current`（逐行，探针输出的 `current` 是列表），放行的主机不是这台时不查 PID、按在跑算（#95）
26. **overrides.json、events.jsonl 读改写的互斥**：overrides.json、events.jsonl 读改写的互斥（锁在旁边的 `.lock` 上）
   实现：worker 的 `flock`
27. **告警给谁看与给谁的那份文字；页面上显眼列哪些；`labmon wait` 叫不叫醒会话**：告警给谁看（#142：`user` 要用户处理、`agent` 只给实验会话、`both` 两边都要；产出处必须定，写错报错）与给谁的那份文字（`msg` 给会话写操作，`page` 是 `both` 给用户的人话）；页面上显眼列哪些（红框、告警数、侧栏红点、主机圆点、标签页图标只算给用户的；只给会话的收在默认折叠的「给实验会话的提示」里；工作台项目摘要的告警数与列表同一口径）；`labmon wait` 报给会话与两边的，只给用户的不叫醒会话；报过的消失后 6 小时内再出现不叫醒（`failed:` 每次都报，#88）
   实现：`collect.alert`（`AUDIENCE`）、`collect.user_alerts`（`serve.proj_info` 用）；`waits.wait_check`、`waits._new_alerts`（`REWAKE_S`、`EACH_TIME`）；页面上 `forUser`、`forAgent`、`alertBox`（common.js；JS 与 Python 各一份，口径相同）；工作台主机告警 `alertsOf` 返回 `{to, msg}`（overview_js/core.js）
28. **问题报告与普通账号的每日份数**：问题报告（#200）：实验会话提交，看板管理员在记录页查原文，总控的等待只读报告元数据；普通账号 24 小时最多 `REPORT_DAY` 份（#18）
   实现：`__main__.cmd_report`、`records.report`（超了抛 `records.ReportQuota`，接口回 429）、`records.report_headers`、`tools/issue_watch.py`
29. **记录；事件种类与分类表；从暂停与优先记录取项目 id**：记录（事件、平台操作、配置改动：写入与按条件读，页面经 `/records.json` 取）；事件种类与分类表；从暂停与优先记录取项目 id
   实现：`records.log_events`、`records.records`（`records.EVENT_KINDS`、`records.EVENT_CATS`）、`records.schedule_project`；项目所用主机上哪些记录算它的（项目页时间线 `serve.proj_events` 与 `labmon records` 共用）`records.project_mine`、`labmon records` 取的 `records.project_rows`；项目要登记过（没登记 404，取配置、wait、只读命令共用）`projects.require`；页面上 `recSlice`、`recList`（合成事：`recGroup`）、`logPage`（overview_js/sched.js，`logPage` 在 overview_js/home.js）
30. **主机缺省值；缺省端口；worker 多久没写状态算不在**：主机缺省值；缺省端口；worker 多久没写状态算不在（工作台与项目页探针同一口径）
   实现：`config.host_defaults`；`config.PORT`；`worker.pure.STALE_S`（`config.STALE_S` 与 worker `STATUS_STALE_S` 共用；部署路径 `ka_bin` 由 `workerctl.worker_env` 传给 worker，借来的主机经 `config.guest_host` 继承）
31. **用户级主机清单里的主机；其中在用的**：用户级主机清单里的主机（自用的与借来的组内服务器）；其中在用的（去掉已归还的，已补缺省值）
   实现：`config.user_hosts`（借来的长什么样：`config.guest_host`）；`config.active_hosts`；在用的在前、已归还的在后的全部 `config.all_hosts`（自用主机都关了时取平台账号、自己的名字、开回来沿用的端口）
32. **项目 id**：项目 id（看板地址，也是 worker 的项目名：`worker add --project` 必写，pause、focus、借卡的“只给谁用”、作废唤醒都按它认）
   实现：`config.load` 定下 `cfg["id"]`；看板只认 worker 状态里队列与任务的 `project`（来自 `worker add --project`），不按目录猜；链到项目页时只看它在不在 `projects.project_ids` 里（登记了、没归档的），命令行核对用 `api.unknown_project`（在看板那边）
33. **队列登记；撤登记与看板核登记归属按清单文件认；读一台的登记目录；各主机的登记目录**：队列登记（按目录与清单认，文件名带目录哈希；不许两条共用状态目录，跨主机查）；撤登记与看板核登记归属按清单文件认（ROOT/JOBS 拼起来规整后相同就算，从哪里断开都行，#77）；读一台的登记目录（坏了的单列，worker 跳过并记事件）；各主机的登记目录
   实现：`worker.pure.list_path`、`worker.pure.same_list`（worker `matching_regs(by_file=True)`、看板 `users.same_list`、`collect.reg_queues`、`reg_projects`、`pool.locate` 共用）；worker 的 `qslug`、`reg_files`、`regs`、`registry`（`Worker.registry`、`worker add` 查状态目录、`fit` 找清单行都经它）；`reg_dirs`、`queue_state_conflict`（`worker add` 查状态目录有没有被别的登记用着）
34. **清单的停法与 ROOT 内的停止请求文件**：清单的停法与 ROOT 内的停止请求文件（#237）
   实现：worker 的 `queue_stop_path`（HTTP 登记与搬移、本机 `workerctl.run_out` 和远端登记共用语法；远端另外检查实际路径）、`write_queue_stop`、`Worker.send_stop`
35. **一条清单只归一台**：一条清单只归一台（`worker add` 搬来别的主机上比这次旧的登记、更晚的让给那边；两台都登记着时较旧的那台在较新的在用期间让路；搬走后原主机接管手上的到跑完；开跑前核登记还在；add 的查写比删与开跑的核归属到写 `current` 共用一把锁，#95）
   实现：worker 的 `main` 的 add、`Worker.registry`（`peer_regs` 由 `peers` 读、`yielded`）、`Worker.adopt_left`（上一个 worker 的状态 `Worker.load_prev`，升级时等旧 worker 退出后再读）、`Worker.start`；锁 `reg_lock`；旧名登记搬回来换了文件名时在跑的改记到新名下 `Worker.rekey`
36. **清单登记的全局视图**：清单登记的全局视图（各主机目录下登记了哪些清单：在用的、算力池里待放的、已归还的、主机清单里没有的；项目「管理」抽屉的主机与队列）
   实现：全局快照的 `regs`：`overview._regs`（在用主机取状态里的 `queues`；别的读登记文件：每个 worker_home 挑一台在用主机（`config.home_host`，与代办同一台），`overview.fetch` 里的 `_reg_cmd` 读回原文、`_parse_regs` 拆。worker 在服务器上读、看板不 import 它，所以读登记文件有这两份，字段只取 `REG_KEYS`）；页面上 `projRegSec`（每台主机一行，逐条只读的 `regRow` 折起来，项目页已收起的不列：`regQuiet` 按项目快照的 `quiet`）、按状态计数 `regTally`，条与数 `tallyBar`、`tallyHtml`（配色与图例同一份 `REG_ST`）（overview_js/project.js）
37. **项目配置的摘要与页面上改项目的 monitor.toml；计划项的进度**：项目配置的摘要与页面上改项目的 monitor.toml（项目「管理」抽屉的「项目配置」：能改哪些项与取值范围、缺省（页面的档位、当场校验与 `load` 补缺省同一张表）、只改相关行、`load` 整套校验、挪计划项钉颜色、换色、里程碑整组换、自动组排了顺序写成 `[[groups]]`（#150）、出错的那一项 `field`，#122、#133）；计划项的进度（项目摘要的 `groups`、`ungrouped`）
   实现：`serve.Overview.cfg_info`（全局快照 `projects[].cfg`，范围 `spec` 取 `config.cfg_spec`）、`serve.Overview.proj_info`；`projects.set_config`（改的是登记目录里那份，与推同一把锁）→ `config.set_project`（`PROJ_KEYS`、`ALERT_KEYS`、`_proj_val`、`_move_groups`、`_set_markers`，接新块 `_append_blocks`、值的写法 `_toml_val`（与 `_set_keys` 共用）；`POST /project/config`）；页面上 `projCfgSec`（一项一行 `cfRow`、档位 `cfDurRow`、节奏 `cfRhythm`、里程碑 `cfMarkers`、计划项 `cfGroups`、告警 `cfAlerts`、保存条 `cfgSaveBar` 与对照 `cfgDiffText`），草稿 `CFE`（按项目各一份：`cfgEdit`、`cfgCur`、`cfgDraft`）、改了哪些 `cfgDiff`（哪些自动组要写 `cfgKeep`）、当场校验 `cfgErrs`、画一节要的 `cfgCtx`、打字时就地换 `cfgPatch`、`cfgAct`、`cfgInput`（overview_js/project.js）
38. **哪几台在用主机共用一个 worker_home**：哪几台在用主机共用一个 worker_home（同一块共享盘；`labmon host list` 分组那行、`worker status` 末尾那句，#35 A）
   实现：`config.home_peers`（与 `config.home_host` 共用 `_same_home`）；`api.hosts` 的 `home_with`、`api.home_text`
39. **在哪台上操作一台主机目录下的登记与清理临时指令**：在哪台上操作一台主机目录下的登记（在用的就是它；已归还的、主机清单里没有的经共用 worker_home 的在用主机代办，不连已归还的那台；`worker rm` 与归档都经它；算力池的 add、rm 也经它，池在 `config.pool_home`）；借用归还连不上目标机时经共用 `worker_home` 的在用主机删共享盘上的 `overrides.json`，换人/开机接成自用主机时仅清同 id 遗留的借用指令（`guest` 与 `target=borrow` 的 `deadline`）（#171）
   实现：`workerctl.via`（`run_out` 的 rm；挑哪台 `config.home_host`，工作台每轮代读登记也经它）、`workerctl.clear_overrides`（`borrow.finish`、`fleetops.run_swap` 共用）
40. **页面上搬、移除登记；能搬去哪些主机**：页面上搬、移除登记（按项目整台，页面上叫「换到其他主机」「不用这台」：搬 = 逐条在目标主机 `worker add`，别的主机上同一条清单的登记由 worker 删掉；移除 = 逐条 `worker rm`，有一条在跑就整批拒绝；各记一条配置改动、按项目告诉实验会话；#121）；能搬去哪些主机（worker_home 相同的在用主机，借来的只给 `borrow.for` 里的项目；给人挑的空几张卡、租期）
   实现：`serve.reg_move`、`serve.reg_rm`（`POST /reg/move|rm`）；页面上 `regHostBtns`、`regTo`（空卡 `freeCards`）、`regAct`（这项目在这台的登记 `regMine`），一条登记的进度 `regLeft`（`regRow`、主机页 `queueRow`、接上卡共用，overview_js/project.js；`queueRow` 在 overview_js/storage.js）
41. **上一台自用主机；新主机页接上原来的项目；无登记自用主机「给项目用」**：上一台自用主机（同一个池里最近归还的；开机沿用它的端口、新主机页列它的登记）；新主机页接上原来的项目；无登记自用主机通知选定项目的实验会话来登记（#173：不改登记、不替会话挑清单）
   实现：`fleetboot.last_mine`（`back_port` 与全局快照 `regs` 的 `prev` 共用）；页面上 `adoptSrc`、`adoptCard`、平台操作结果后的一句 `adoptHint`；给项目用 `serve.host_assign`（`POST /host/assign`，过 `lease.need`、`users.need_host` + `users.need_project`，经 `records.tell(project, "host", …)` 通知）、页面上 `assignSrc`、`assignCard`、`assignAct`（overview_js/project.js）
42. **项目登记；看板上有哪些项目**：项目登记（#161：状态根的 `projects/<id>/`，取代 launchd plist 的 `-c`；配置以这份为准、done_when 的文件、输出目录；第一次推就是登记；版本号是文本哈希，推带上次同步的版本、对不上 412；推与页面上改同一把锁）；看板上有哪些项目
   实现：`projects.root`、`cfg_file`、`files_dir`、`out_dir`、`ver_of`、`registered`、`project_ids`、`load_cfg`、`get_config`、`put_config`（校验 `_check`、写 `_store`、`Refused`）、`set_config`、锁 `_reg_locked`；经接口改 `[[hosts]]`、`[[queues]]` 要平台操作的权限（`projects.GUARDED`、`_guarded`，`put_config` 的 `ops` 由 `users.can_ops` 给）；done_when 文件的位置 `config.done_key`、用到哪些 `config.done_files`；接口 `GET/PUT /api/project/<id>/config`（serve.py）
43. **项目目录里的 monitor.toml 与看板上那份同步**：项目目录里的 monitor.toml 与看板上那份同步（`labmon project push\|pull`、`labmon wait` 每轮；三方比对：本地、看板上、上次同步的版本；两边都改了不动、报 `sync_conflict:`；拉时本地没同步的存 `.mine`）
   实现：`projsync.status`、`push`、`pull`、`sync`、`say`、同步记录 `mark`、`base_of`；取、推只经 `projsync._get`、`_put`（经 `client.call` 的 GET/PUT `/api/project/<id>/config`，#162）；本地这份只做 TOML 解析 `config.brief`；`wait` 里 `__main__._sync`
44. **项目的归档、恢复与永久删除；看板服务里按归档摘下与加回项目**：项目的归档、恢复与永久删除（能不能归档的原因、移除登记、撤权重与指令、`projects.json`；命令行 `labmon project archive\|restore` 与页面同一处）；看板服务里按归档摘下与加回项目
   实现：`projects.archive_block`、`projects.archive`、`projects.restore`、`projects.archived`（归档后给 `wait`、`check` 的那句 `projects.archived_msg`，记着谁做的 `by`；`projects.json` 读改写跨进程互斥 `projects._locked`）；`serve.Server.sync`（按登记加载、启动时跳过已归档的；摘下时那一轮没做完就恢复的接着用原来的线程：`_Collector.resume`）；永久删除 `Server.delete_project` → `projects.delete`（相关 worker 目录只读核对 `overview.check_project_idle`）；当前归档列表 `projects.archived_rows`；页面上 `projArchSec`、`archDrawer`、`pjAct`（overview_js/project.js）
45. **等待放行的请求：字段、清单行 → 任务 → 请求、按名字找清单行与资源提示、`.env` 路径、按主机现状补全**：等待放行的请求（清单队首或一次单元申请）的字段；清单的一行资源提示解析（`gpus=` 缺省 1，未写 `gpus=` 且显式写 `vram=0` 时自动按 `ngpu=0` 不用卡处理，显式 `gpus=1 vram=0` 仍按 `ngpu=1`）→ 任务 → 请求（真放行与 `fit` 共用）；按名字找清单里的一行（取第一条，接管、旁观、`offers`、`fit` 共用）；按名字取清单行的资源提示（同名的取第一条：`proj_lanes`、`queue_durs`）；一条清单的 `.env` 路径；按主机现状补全（资源估计、挡住它的指令、预计时长、等不来的原因、开始等的时刻、先记的用量）
   实现：worker 的 `new_req`；`hints`、`job_of`、`queue_req`；`job_cmd`；`first_rows`；`env_path`；`Worker.prep`
46. **并行清单**：并行清单（#147：清单里 `# labmon: par=N`，没写为 1、至多 `PAR_MAX`；窗口按清单顺序取没标完、没在跑的前 N − 在跑行数行，等依赖的占格；并行清单的请求与运行中任务的 key 是 `队列 key|任务名`，串行清单仍是队列 key，车道身份一律按队列 key；已有行在跑时要卡的行另过加行门槛：`local_cards` 同一条规则、两次往上加行至少隔 `PAR_STEP_S`，补回近 `PAR_REFILL_S` 同时跑到过的行数（补位，#176）不等间隔、卡吃满也补，限流时都不补，推演里按此刻结论当常量；看板车道的 `par`、`running_n`，页面「并行 k/N」与分道）
   实现：worker 的 `list_par`（`PAR_RE`，`list_jobs` 写进 `q["par"]`）、`next_jobs`、`run_key`、车道 key `lane_key`、这条清单在跑的 `Worker.lane`、接管时不撞 key 的 `Worker.free_key`、门槛 `Worker.par_gate`（`place` 调，往上加行的时刻 `Worker.par_t`，跑到过的行数 `Worker.par_hi`：`Worker.par_level`、`Worker.par_note`）；推演 `simulate` 的窗口（`put`）、`forecast` 的 `par`；`collect.parse_gpu_queue`（多个在跑）、`collect._queued_times` 的 `par`；页面上 `parPack`、`laneTail`（page.html），`api.status` 的车道行
47. **挡住一个请求的临时指令；其中持久的**：挡住一个请求的临时指令（暂停、drain、截止、借来的主机只给谁用与对方超出预留）；其中持久的（不算对方此刻超出预留：推演排除哪些队列、offers 判这台接不接都只看它，此刻才有的原因推演不模拟）
   实现：worker 的 `override_hit`（`lasting=True`）
48. **项目暂时没实验**：项目暂时没实验（队列清单跑空标“空着”、项目标“暂时没实验”，这时不报主机的事；跑空时给会话一条 `no_work:<起点>`，借来的主机空着给 `borrow_idle`，#43）
   实现：worker 状态里队列的 `empty`（`requests` 里 `next_jobs` 为空、也没有在跑的时标）；`collect._no_work` 算 `no_work_since` 与那条告警（`NO_WORK_WAKE_S`）；借来的主机 `borrow.check`（`IDLE_S`）；页面上 `NO_WORK`、`noWorkTip`、`noWorkBadge`（common.js）
49. **改配置文件**：改配置文件（用户级主机清单与项目的 monitor.toml：只改相关行、改完校验再写；读 → 改 → 写整段互斥）
   实现：`config._blocks`、`config._set_keys`、`config._write_checked`（项目配置传 `check` 用 `config.load(path, text)` 整套校验）；锁 `config.HOSTS_LOCK`（线程之间 RLock，进程之间状态根 `hosts.lock` 上的 flock，同一线程可重入，#18），改配置文件的函数都加 `config._hosts_edit`
50. **占位任务的认定**：占位任务的认定（名字里按词含 hold / pause）
   实现：`worker.pure.is_hold`、`worker.pure.HOLD_RE`（worker 与看板 `config.HOLD_RE`、`collect.is_hold` 共用，项目不能另配）
51. **登录账号在组内人员表里的名字、主机上的使用人；后台与历史记录按已登记的人员统计**：登录账号在组内人员表里的名字（只认账号库的显式绑定 `fleet_person`，#19：不读主机清单、不按同名猜）、主机上的使用人；后台与历史记录按已登记的人员统计（#70）
   实现：`users.person_of`、`users.people_of`（批量）、开通与补绑定前的核对 `users._check_bind`、开通时可选的人 `users.free_people`、`users.add`、`users.bind_person`、名册删人前 `users.removing_person`（账号库同一把锁）；删人时唯一的清理入口 `users.remove`；`fleet.host_holder`；旧数据 `fleet.legacy_person`；项目算在谁名下（按人的公平份额，#23）`fleet.project_people`，写进 `sched.json` 的人员键 `fleet.share_key`；登记人员 `fleet.registered_people`、`fleetboot.set_person`，人员卡与账号详情共用 `personCard`、`directoryDrawer`（overview_js/fleetops.js）
52. **组内服务器的状态文件；这一台正借着；借给了谁**：组内服务器的状态文件；这一台正借着；借给了谁（借用中的用量算给他）
   实现：`fleet.state_file`；`fleet.borrowed`；`fleet.borrower`
53. **共有型算力池**：基于实验室常驻共有主机组的共有型算力池（#212 阶段 2）。`hosts.toml` 的 `[pools.<id>]` 支持多池配置，强校验同 `worker_home`、单机单池互斥、禁止本机/回环/独立/借用机入池；缺省 `exclusive = true`（禁止直投单台，显式 `false` 放开）。统一经 `pool`（默认池或唯一池别名）或 `pool:<id>` 提交，支持 `add`/`rm`/`status`/`fit --list`；worker `pool_refuse` 仅拒 `--gpus` 与 `gang=`（放宽 `#87` 允许 `--stop boundary` 与占位任务）。看板每轮经 `pool.tick` 按人公平份额（`fair_key` + `person_use`）+ 负载打分（`fit_head` + `other_load`）即时落机、Work-Stealing 空闲窃取（`POOL_MOVE_COOL_S` 防抖），并在 `drain`/预备续租 `deadline`/离线失联满 10 分钟/归还/移出池时自动经 `reg_move` 撤离；`kind="pool"` 事件归入 `sched` 分类并按本人项目隔离。`pool.summary` 按身份投影：管理员全量、普通池成员仅见所属池聚合与本人待放清单（剥离 `members`）、非成员为 `None`；首页 `home.js` 以 `hasPoolCompute` 将池成员视为已有可用算力，无主机但名下持有组内服务器时给出精准提示。
   实现：`worker.pure.POOL`、`POOL_PREFIX`、`POOL_ID_RE`、`pool_host_id`、`is_pool_id`、`pool_id_of`；`config.POOL_KEYS`、`config.pools`、`config.resolve_pool`、`config.host_pool`、`config.pool_home`；`pool.key`、`pool.member`、`pool.user_pools`、`pool.user_pool_hosts`、`pool.exclusive_pool`、`pool.locate`、`pool.tick`、`pool.summary`、`pool.status_text`；worker 的 `pool_refuse`、`Worker.pool_todo`、`pool_heads`、`fit_head`（`fit --list --json`）、`add --pool`（`load_queue` 保留池 id，`proj_lanes` 跳过）；`serve.reg_move` 的 `pool`、`workerctl.run_out` 的 `pool`、全局视图 `overview._pool_mark`、`collect._lane` 的 `pool`；页面上 `isPoolId`、`canUseHost`、`hostShort`（common.js）、`poolSummaryCard`、`poolPage`（overview_js/sched.js）、`hasPoolCompute`（overview_js/home.js）
54. **旧借用池配置残留校验**：`hosts.toml` 的 `[[fleet.servers]]` 或 `[[hosts]]` 中若残留已移除的旧算力池字段（`lend`、`lend_hours`、`borrow_pool`），`config.user_hosts` 与 `fleet.conf` 共用唯一校验直接拒绝并给出清晰的移除提示，避免残留旧池借用进入调度、采集或 `overview.relaunch`（#212 阶段 1）
   实现：`config.LEGACY_POOL_KEYS`、`config.check_legacy_pool`（`config.user_hosts` 与 `fleet.conf` 的 `fleet._legacy_pool_check` 共用）
55. **服务器持有人鉴权**：判断看板账号是否为指定组内服务器或非借用自用主机的持有人（管理员放行，普通用户须绑定组内人员且与服务器持有人一致；原在 `pool.need_holder`，#212 阶段 1 迁移至 `users.py` 供 `/fleet/<id>.json` 等复核）
   实现：`users.need_holder`（核组内服务器 `holder` 与非借用自用主机 `fleet.host_holder`，`serve.Handler._own` 的 `fleet_srv` 调用）
56. **租期到期时刻；租期提醒的档**：租期到期时刻；租期提醒的档（到期前 24、6 小时各一次，已进更近一档不补远的；相近到期合成一组）
   实现：`health.lease_end`；`health.lease_due`
57. **掉线与恢复判定；换了新容器**：掉线与恢复判定（连续至少 2 次、持续 `DOWN_S` 秒连不上才算掉线；工作台主机、组内服务器、项目页同一条）；换了新容器（续租或重启）
   实现：`health.link_step`；`health.container_step`
58. **平台操作**：平台操作（核对、续租、开保活、关机、开机、换人、换节点、借来用；自用主机与组内服务器同一套；核对把无运行中同名同池作业的记入 `platform.json` 的 `stopped`（不含端口映射不对），续租/开机/换节点新作业起后从中移除；接成自用主机时清掉同 id 遗留借用指令，#171）
   实现：`fleetops.run_*`（中文名 `fleetops.OP_NAMES`）；核对的比对 `fleetops.reconcile`（纯函数）、确认作业不在运行 `fleetops.job_stopped`（`SYNC_GRACE_S`）；停平台作业、提交克隆并等运行 `fleetops.stop_job`、`fleetops.submit_job`（续租、换人、换节点、开关机共用，进度记进 `op.json`）；出错时这一台的状态一句话 `fleetops._note`
59. **这一台此刻能不能动**：这一台此刻能不能动（在跑实验、未识别的 GPU 进程、没探到的、本地盘 labmon 之外写得多（`lw.risk`）的要勾「已说好」；自用主机 worker 在跑只许续租勾「可以中断」，关机、换人、换节点直接拒绝）
   实现：`fleetops._risk`、`fleetops._need_ack`（这一台的 `lw`：`fleetops._lw`）；页面上 `FBUSY`、`FRISKY`，勾选框的文字 `ackText`（overview_js/fleetops.js；续租一台、批量续租、关机、换人、换节点共用）
60. **换人、换节点、开机的作业名、端口与规格核对；续租、开机换了容器清旧主机密钥；连一台时新主机密钥记在哪份**：换人、换节点、开机的作业名（同名已在用就加后缀；真操作只认刚读的平台列表，预览用上次核对存的）、端口（接替的那台的、这人在这个池里上次关掉那台的优先，被占了往后挑；向导预览与真操作同一处取，连同“为什么选它”那句）与规格核对（池内各规格空位取 `pools.json` 预览、提交前实时再核对；仅同池同规格先停后开时 `freed=True`，换规格与换节点要求目标规格此刻有空位）；续租、开机换了容器清旧主机密钥（状态目录的与自用主机自己那份）；连一台时新主机密钥记在哪份（服务器项带 `known_hosts` 的自用主机记在它那份、与实验会话裸 ssh 同一份，其余记在状态目录，#109）
   实现：`fleetboot.taken_jobs`、`fleetboot.job_name`；`fleetboot.back_port`、`fleetboot.pick_port`（已占的 `fleetboot.used_ports`）；`fleetboot.pool_specs`、`fleetboot.spec_of`、`fleetboot.spec_name`、`fleetboot.check_spec`（`fleetboot.check` 与 `fleetboot.preview` 共用）；页面上 `poolSpecs`、`specName`（overview_js/fleet.js）；`fleet.forget`（自用主机的那份取 `fleetboot.mine` 的 `known_hosts`）；`fleet._ssh_cmd`
61. **worker 停启保活**：worker 停启保活（要卡时停、空闲时开、有了别的 GPU 进程让路；借来的主机上 `ka.sh` 停不了的保活：那张卡不放（推演同样不放），用量不算对方的）
   实现：worker 的 `keepalive`、`ka`；`gpu_owners`（`ka_full`，认不认 `ka_owned`）、`guest_scan`；推演经快照卡的 `ka` 由 `sim_snapshot` 挡（#45）
62. **命令行里保活的那几句**：命令行里保活的那几句（在跑：几点起、nvidia-smi 里的 PID 与卡、显存；停着、不拉起的原因、对方停不了的、近 1 小时被拉起；归属取状态里 `gpu_procs` 的 `owner`、`ka`，不另判，#61）
   实现：worker 的 `ka_lines`（`labmon worker status` 与看板的 `api.status` 共用；状态的 `keepalive`、`gpu_procs[].t0`）
63. **命令行里一台的 CPU 那一句**：命令行里一台的 CPU 那一句（容器配额与记账上限、`nproc` 是宿主机的、labmon 在跑的记了几核、labmon 之外与给对方的预留占几核、还能给几核，#112）
   实现：worker 的 `cpu_line`（`labmon worker status`、`worker fit` 的 `FitHost.card_lines` 与看板的 `api.status` 共用；状态的 `info.cpu_quota`、`room.cpu`、`running[].acct`）
64. **要卡时停了的保活又被拉起；ka.sh 日志里哪条 `RESTORED` 是这台的；进程启动时刻**：要卡时停了的保活又被拉起（找调用者与看门狗、记事件与状态的 `kafight`；旧 `gpu_queue.sh` 拉的不算）；ka.sh 日志里哪条 `RESTORED` 是这台的（共享盘上各容器共用：按此刻保活的进程组与它的启动时刻认，#123）；进程启动时刻（epoch 秒，容器启动时刻也用它）
   实现：worker 的 `ka_restarted`、`ka_caller`、`ka_others`；记一次 `Worker.ka_fought`（推迟中保活自己没了 `Worker.ka_gone`）；`Worker.ka_groups`（`KA_LOG_LAG`，日志时刻 `ka_log_time`）；`started_at`（`host_info` 也调）；重启后找回近 1 小时的次数与“停着保活”（状态的 `kafight.hits`、`ka_stop`）`Worker.load_prev`（#124）
65. **要卡时停保活没停成**：要卡时停保活没停成（`ka.sh stop` 失败，原因取它的标准错误末行；`ka.sh running` 说没在跑、GPU 上却有它管的保活也算；连着满 `KA_STUCK_S`、至少两次算停不了：事件 `ka_stuck`、状态的 `keepalive.stuck`；照放这张卡，推演与 fit 当它一直占着；#102）
   实现：worker 的 `Worker.ka_stop`、`Worker.ka_note`、`Worker.ka_stuck`（`ka_lines` 那一句、`Worker.snapshot` 卡的 `other`、`fit_snapshot` 不加回）；看板 `borrow.finish` 的 `stuck`（`borrow.check` 传）；页面上 `alertsOf`（overview_js/core.js）
66. **保活看门狗**：保活看门狗（认定：确定的、可能的，下面在跑程序的不算确定；借来的主机上平时只列出来，保活真被拉起才暂停、还卡时恢复；它正持着平时不持的文件锁时这一轮不暂停、不停保活）
   实现：worker 的 `watchdogs`（`script_of`、`refs`、`ours`）、`freeze`（`ka_restarted` 调）、`thaw`、`Worker.watch`；进程持着的文件锁 `held_locks`，平时持的锁 `Worker.note_seen`（`frozen.json` 的 `seen[].locks`、`locks_t`，读 `lock_base`），要不要推迟 `Worker.lock_busy`（推迟中 `Worker.ka_wait`）；`borrow.finish` 调 `thaw`；页面上借用抽屉 `borrowSec` 的看门狗一行
67. **平台操作在不在做；哪些服务器正在做**：平台操作在不在做；哪些服务器正在做（探测、保活守护、借用、掉线判定、自动预备续租都问它）
   实现：`fleetops._live`；`fleetops.busy()`
68. **自用主机当成组内的一台**：自用主机当成组内的一台（换人、关机、续租、配额与组内页列表；字段与 `fleetview.view` 的 servers 同名，组内页的卡片与抽屉同一套模板，差别只在按钮 `srvBtns` 与操作确认的文字；平台操作口径的 `state`、`work` 与样本 `sample` 分开）
   实现：`fleetboot.mine`；自用主机的「worker」节 `workerSec`（overview_js/fleet.js）
69. **服务器的池类型文字**：服务器的池类型文字（自用 / 公用 / 专用）
   实现：`fKind`（overview_js/fleet.js）
70. **人的缩写与头像；账号名换成组内人员名**：人的缩写与头像（末尾带数字取首字母 + 数字，多段名取各段首字母，否则前两个字；颜色沿用共享盘上这人的配色 `D.disk.colors`（disk.py 经 `config.pick_color` 定），没有的用主色；角上的状态点由调用方传）；账号名换成组内人员名（账号库的 `fleet_person`，读不到写账号名，#15）
   实现：`pabbr`、`pfav`（人员卡、人员抽屉、服务器卡与抽屉、保活守护、分配一台；「我」的头像 `accAv` 取 `pabbr`）；`acctPerson`（项目归属）（`pabbr`/`pfav` 在 overview_js/core.js、`accAv` 在 overview_js/account.js、`acctPerson` 在 overview_js/project.js）
71. **普通用户此刻能用的主机**：普通用户此刻能用的主机（单独分配的 `ME.hosts` 加上快照里给他看的、他的项目有车道的主机；总览「开始使用」与「我的账号」的「能用的主机」同一口径，#15）
   实现：`myHosts`（overview_js/home.js）
72. **组内服务器页面的数据、详情曲线、人员详情；命令行 `labmon fleet status` / `probe`**：组内服务器页面的数据（各台现状、逐小时状态、统计、按人、配额）、详情曲线、人员详情；命令行 `labmon fleet status` / `probe`
   实现：`fleetview.view`、`fleetview.detail`、`fleetview.person`；`fleetview.show`
73. **使用记录；按人统计**：使用记录（每小时一行，组内服务器与自用主机同一套；借用中的那部分记在行的 `bw[借的人]`）；按人统计（拆小时行：借用中的算给借的人、主人那边记“借出”，按小时归一；人员卡、人员详情、`holders` 共用）
   实现：记 `fleet._hour`（当前小时的累计 `fleet.accs`）；拆 `fleetview._parts`、汇总 `fleetview._by_person`、`fleetview._stats`、借来借出 `fleetview._lending`；页面上 `useBar`、`perHist`（状态名 `PKS`、`PSN`、`PSC`，借来借出的明细 `lendLines`，overview_js/fleetops.js）
74. **worker 状态新鲜度门槛；自用主机与借用中组内服务器的使用样本；逐次样本的一行**：`fleet.worker_fresh` 判工作台快照里主机的 worker 状态是否新鲜（有 `status`、没掉线、`worker_up` 为真且 `not lan_off`），自用主机（`fleetboot.mine`）与借用中组内服务器（`fleetview.view`，不新鲜或连不上时回退探测样本，#174）共用；使用样本由 `fleet.from_worker` 把 worker 状态换成与探针 `analyze` 同形的样本（状态只看 GPU 进程，负载按任务与对方进程归组）；自用主机每轮全局采集记一次小时行、每 `interval` 写一行逐次样本，借用中组内服务器的小时行（`bw`）与逐次样本仍由探针统一记；逐次样本的一行（探针与自用主机共用）
   实现：`fleet.worker_fresh`、`fleet.from_worker`（`fleetboot.mine` 与 `fleetview.view` 的 `sample`，卡片与抽屉都取它）、`fleet.record_mine`（`overview._notices` 调）；`fleet._series_row`；曲线接口认哪些 id `fleetview.known`
75. **可共享**：可共享（除保活外的显存、利用率、CPU、内存都低于 `[fleet] share`）
   实现：`fleet.shareable`（`analyze` 与 `from_worker` 共用）
76. **预备续租、续租后拉起 worker**：预备续租（取消时记下这一期，自动预备时段里取消过的不再自动预备）、续租后拉起 worker
   实现：`renew.reserve`、`renew.cancel`、`renew.begin`、`renew.after`；页面上自用主机的按钮 `renewBtns`，续租一台的确认 `goRenew`（overview_js/sched.js，都由主机页与组内服务器抽屉共用）
77. **后端看 worker 状态里运行中任务的预计结束**：后端看 worker 状态里运行中任务的预计结束（续租与借用的截止判断共用）
   实现：`renew.eta_of`
78. **借用**：借用（借来、约定时间与对方租期取早、补齐指令、到点提醒、要还、归还；在 `health.link_step` 判掉线且 `fleetops.job_stopped` 确认平台核对作业不在运行时自动走 `finish(gone="stopped")` 归还，不 ssh 目标机，连不上时经 `workerctl.clear_overrides` 删共享盘上的 `overrides.json`，#171）
   实现：`borrow.begin`（页面上点的经 `fleetops.run_borrow` 在后台跑、显示进度）、`borrow.cutoff`、`borrow.check`、`borrow.request_return`、`borrow.cancel_return`（撤销跑完就还）、`borrow.finish`（`gone=True|"stopped"`）；页面上 `borrowText`、`borrowBadge`（common.js，两个页面共用；项目快照的主机带上 `borrow.check` 算好的 borrow），操作按钮 `borrowBtns`（overview_js/fleet.js，抽屉与主机页共用）
79. **按钮旁的小弹窗与按钮配色**：按钮旁的小弹窗（短确认、短表单、验证码；同一时间一个，状态在 `POP`；点外面、Esc 取消，有输入的点外面不关；窄屏贴在屏幕底部，让出底栏与软键盘）与按钮配色（主色推荐、红框会中断、琥珀框撤销，主色灰掉时灰底灰字）
   实现：`popConfirm`、`popToggle`、`popClose`、`closePop`、`fitPops`；平台操作的弹窗 `opPop`（overview_js/fleet.js）
80. **平台操作确认的风险分层；会中断什么的那句**：平台操作确认的风险分层（#14：一句结论 → 要勾才动的风险与勾选框 → 折起来的细节，开合记在 `POP.more`；续租一台、关机的弹窗与换人向导共用）；会中断什么的那句
   实现：`riskSay`、`riskNeed`、`riskMore`；`busyText`（批量续租的注意 `renewWarn` 也用它，overview_js/fleetops.js）
81. **批量续租**：批量续租（#14：右侧抽屉 `#/fleet/renew`，提醒条点的 `#/fleet/renew/<id>` 只勾那一台；一台一行，本行勾「已说好」，借用中的禁选并提示还掉或点「与平台核对」自动归还，#171；勾选在 `RPICK`；没登录时验证码在抽屉底部（`FCAP.pop = "renew"`），开始后进度在抽屉顶上）
   实现：`renewDrawer`（`rpList`、`rpBorrowed`、`rpBody`；`DH.renew`，overview_js/fleetops.js）
82. **弹窗里选一个将来的时刻**：弹窗里选一个将来的时刻（借卡与改约定的约定时间、优先的到期：日期框加时、分下拉，缺省现在（下一个 5 分钟档），小签在当前值上往后加、「现在」、清空，一句到几点还有多久；晚于租期截止的说明；过去的、没填完的、停在「现在」的标红，点确认时拦下）
   实现：`whenPick`（缺省 `whenInit`、`whenNow`，小签 `whenTap`、`whenAdd`（`WHEN_ADD`）、点前失焦 `whenBlur`，控件改了 `whenInput`、`whenRead`、`whenSync`，说明 `whenSay`，`whenT`、`whenPast`、`whenHalf`，提交前 `whenStop`；epoch 秒换成控件的值 `dtLocal`，overview_js/fleet.js）
83. **主机的名字只有三种写法：短名、全名、组内服务器「使用人」**：主机的名字只有三种写法：短名（表格的主机列、小图、时间线、车道名；直接借来的叫“借 EVA”）、全名（主机清单的 `label`，借来的是「节点（借 EVA）」：告警、标题、悬停）、组内服务器「使用人（id）」
   实现：`hostShort`（common.js）；`h.label` 直接取；`fName`（overview_js/fleet.js）
84. **工作台上的项目名与整台主机的操作波及哪些项目**：工作台上的项目名（几个项目时才写，配色点 + 标题；指令对象总写）与整台主机的操作波及哪些项目
   实现：`projName`、`affects`（`projName` 在 overview_js/home.js、`affects` 在 overview_js/sched.js）
85. **项目的车道从哪来；不是 monitor.toml 写明的车道收起；跑空登记自动休眠**：项目的车道从哪来（worker 登记加上 monitor.toml 可选的 `[[queues]]`：同一清单以登记的主机为准，只在登记里有的接在后面、id「目录末级名/清单」；登记没了有在跑/排队的留 24 小时，已摘登记跑完的、已归还无未完成无失败的、已休眠的与 0 任务空清单立即收起；登记带来的主机与已归还主机的代读主机从用户级主机清单补；`labmon wait` 与项目页主机事件按快照里的主机）；不是 monitor.toml 写明的车道（只在登记里有的、自动发现的）24 小时内全成功跑完的不占“车道 N”号并在页面折起，满 `hide_done_hours` 进 `quiet`；跑空满 `IDLE_REG_S`（2 天，#164）的登记由 worker 自动转休眠（只用 `file_sig` 查清单变动，不读 `.env` 与状态目录，文件变动或重新 `worker add` 自动唤醒；算力池待放清单同规则；已归还代读跑完后探针按 `done_sig` 短路），不再发 `idle_regs:` 提示
   实现：`collect.reg_queues`（纯函数，`REG_KEEP_S`，记忆 `.labmon/reg_lanes.json`；登记变了的项目全局采集后当场重采：`collect.reg_sig`、`serve.Overview.wake_moved`，#78）、`collect.lanes_cfg`（采集与 `labmon check` 用）；补主机与队列 `config.with_queues`（与 `load` 共用 `_norm_host`、`_norm_queue`）；收起 `collect._quiet`（`_lane_specs` 调；收起的登记车道进项目快照的 `quiet`，带 `sleeping`；不占号判定 `collect._is_done_lane`；已归还代读记忆 `collect._sync_returned_done`）；自动休眠 `worker.pure.IDLE_REG_S`（`Worker.registry`、`Worker.requests`、`Worker._idle_start`、`Worker.pool_todo`、`probe.queue_gpu_queue` 的 `sleeping`/`done_sig` 短路）
86. **车道名与车道跑的计划项**：车道名（同机照配置顺序“车道 N”，接可选的用途；24 小时内全成功跑完的非 `monitor.toml` 写明车道不占“车道 N”号、叫「已跑完」；告警与页面都用它；页面上后面接清单名，#47、#164）与车道跑的计划项
   实现：`collect._lane_specs`（`collect._is_done_lane`）；页面上 `laneName(lane, hosts)`（多主机时加主机短名；不带主机、接清单名的 `laneLabel`，时间线行名用它，截到左栏宽度用 `laneFit`（#108），common.js）、`laneGroups`（page.html），工作台经快照取：worker 状态里的队列（推演车道、等待项、`borrow.late`）按 host、root、queue 找快照车道 `laneOf`（overview_js/home.js，对不上用队列名）
87. **运行中的那一格与交回算力池车道上的任务实际在哪台跑**：运行中的那一格实际在哪台跑（清单搬走后原主机接着跑完手上那一格，#100：探针读 `current` 里放行的主机，不是车道主机时进项目快照任务的 `host`；车道仍按登记）；交回算力池的车道上跑完、失败的任务与失败的尝试在哪台跑的（按用量记录补 `host`，#99）
   实现：`probe.py` 的 `current.host`、`collect._lane`；`collect._attempts`；`waits.job_state`；页面上 `rowJobs`（时间线按格子分行）、`awayText`、`jobLane`（page.html）
88. **给 labmon wait 的全局消息**：给 labmon wait 的全局消息（借到卡、到约定时间、对方在用、已归还、作废、页面上动了这个项目）
   实现：全局快照的 `wake`（`borrow.check` 与 `overview.collect`），`waits.wait_check` 合并（按项目与主机挑 `waits._wakes`；`labmon check` 列出还没报过的页面改动 `waits.check_told`，经 `api.check`）
89. **看板侧按名字认一个任务此刻的状态与结果；任务名 `项目/任务` 的写法**：看板侧按名字认一个任务此刻的状态与结果（`labmon wait --job`：看板那边读项目快照 `jobs[]`、`lanes[]`，快照里没有的看工作台存的用量记录；还会跑的优先、不会再跑的认定、作废按全局 `wake` 的 `void:` 的 `job`；开始等之前就已结束、找不到的要两份新快照确认）；任务名 `项目/任务` 的写法
   实现：`waits.job_state`、`waits.JobWatch`（`JOB_CONFIRM`；内部状态经 `state`、`restore` 交给命令行、下次带回）；名字 `worker.pure.job_key`（`waits.job_key` 与 worker `dep_keys` 共用）；按 id 找项目的配置与输出目录 `projects.load_cfg`、`projects.out_dir`（`labmon project archive` 共用）
90. **看板接口的客户端；连看板一律直连、不走环境变量与系统设置里的代理**：看板接口的客户端（命令行会动东西的命令都经它：wait、check、collect、worker、pause 等、project、borrow、host；`client.toml` 的几个地址依次试、退避，会改东西的只在没送到时重试，备用的接着试下一个；令牌 `LABMON_TOKEN` › `admin.token`（要管理员的）› `client.token`；`labmon login` 换令牌，#162）；连看板一律直连、不走环境变量与系统设置里的代理（命令行、`tools/deploy.py`、`backup pull` 共用，#195）
   实现：`client.call`（`servers`、`token`）、一条命令的回话打出与退出码 `client.run`；连不上时证书校验没过的那句 `client.ca_hint`（call、login、取更新包共用，#96）；`client.login`、`client.urls`；直连的 opener `client.opener`
91. **命令行的命令在看板那边的做法**：命令行的命令在看板那边的做法（回 `{rc, out, err}`，命令行原样打出、按 `rc` 退出；改主机清单、ssh 到服务器只在看板服务里；路由在 `serve.ROUTES`、分派 `serve.Handler._cli`）
   实现：`api.host`、`api.borrow`、`api.override`（`OVR`）、`api.worker`、`api.project_init`、`api.check`、`api.hosts`（`GET /api/hosts`）；实验会话的只读命令 `api.status`、`api.disk`、`api.records`（`GET /api/project/<id>/status|disk|records`，回 `{rc, out, err, data}`，`--json` 打 `data`：`client.run` 的 `as_json`；借来的主机那一句 `api.borrow_text`
92. **`labmon wait` 的长轮询**：`labmon wait` 的长轮询（有新告警或等的任务都有结果就回，否则最多挂 `HOLD_MAX` 秒，相关文件变了或 `POLL_S` 秒再判；同时挂着的 `SLOTS` 个，一个普通账号 `ACCOUNT_SLOTS` 个（多了 `AccountBusy` 回 429，#18）；请求号 `rid` 留 `RID_KEEP_S` 秒、重来回原样那份）
   实现：`waits.wait`（`POST /api/wait`）；命令行一轮 `__main__.cmd_wait`
93. **项目最后一次有 wait 来等的时刻；会话的 wait 断了；一个项目只让一个会话等与会话标识**：项目最后一次有 wait 来等的时刻（wait_state.json 的 `wait_t`，只由 wait_check 写，`labmon check` 改的 `t` 不算：挂着时每 `POLL_S` 秒判一次就写一次）；会话的 wait 断了（项目还有活、满 `WAIT_GONE_S` 没来等，看板刚重启从启动时刻起算，#141）；一个项目只让一个会话等（`holder`，满 `WAIT_GONE_S` 没来让出、`--take` 接手、被拦下的记 `refused`，#142）与会话标识；同一会话只留一个普通 `wait`（不带 `--job` 的带实例标识 `wid`，`holder` 记 `w`/`wt`/`old`，新普通 `wait` 直接接替、旧的回 409 不领告警不记 `refused`，`wait --job` 可并存，#165）
   实现：`waits.state_file`、`waits.gone_since`、`waits._claim`、`waits.dup_refused`；告警 `collect._wait_gone`（`wait_gone:`、`wait_dup:`，只给用户）；客户端 `client.session_id`
94. **页面上的改动告诉实验会话**：页面上的改动告诉实验会话（搬、移除登记，恢复项目，暂停与恢复，改 monitor.toml，改借用约定（页面与命令行，`borrow._tell_retime`，#127）；以后别的页面改动同样经它：`what` 一个词、`text` 一句该怎么办）
   实现：`records.tell(project, what, text, src=)`（写 `overview/tell.jsonl`，留 `TELL_KEEP_S`；`src` 缺省「看板页面上」，看板升级时的迁移另写）、`records.told`（`overview._notices` 并进 `wake`，key `page:<what>:<项目>:<毫秒>`）
95. **任务的资源估计；一张卡要多少空闲显存才放；记多少内存、几个核；开跑给几个核；CPU 记账回收**：任务的资源估计（显存、内存逐项：同 tag › 同名近 5 次峰值 ×1.1 › 清单 `vram=`、`ram=` › 同队列峰值 ×1.1 › 0，来源 `src`（显存）、`ram_src`（内存）；OOM 后抬高：标了 `oom` 的用量行不进峰值历史，同 tag、同名里最近一次跑成功之后的 OOM 给那一项下限 max(放行时估计 ×2, OOM 时峰值 ×1.1)（#62）；“近 5 次”按跑成功的数，跑满 5 分钟后失败的峰值只能抬高、不占名额（#134）；单跑时的 GPU 利用率跟着显存的来源；CPU 核数 `cpus=` → 同类 `cores_peak` ×1.1 → 缺省 1 核，`cpus=` 写多了（同类至少 `CPU_SHRINK_N` 次、近 5 次每次都给多了，给了几核按用量行记的那次导出的 `give` 认，#99）只收小记账、开跑仍按它给（#91）；准入与开并行车道前的判断共用：`fit` 按登记过的清单里那一行拼请求，本机优先再别的主机，一次调用只读一遍）；一张卡要有多少空闲显存才放（估计值另留 1 GiB；按历史估的超过整卡时按整卡算）；要记多少内存（按历史估的超过全停时能给的减余量按整份算，原因里写估计与封顶后的量）；要记几个核（按历史估的超过全停时能给的按整份算，原因里写估计与封顶后的核数）；开跑后给几个核（估计封顶到记账上限：运行中的记账、推演里开跑的、`LABMON_CPUS` 同一个数）；运行中任务 CPU 记账的回收（开跑满 `CPU_WARM_S` 后估计那份换成近这么久的 1 分钟窗口核数最大值，不超过给的、不低于 1，#65）
   实现：worker 的 `estimate`（取哪几条峰值 `peak_rows`；`CPU_DEFAULT`；收小记账 `cpu_shrink`，开跑给几核 `cpu_threads`）、`fit`、`fit_list`（`labmon worker fit [--list]`；判断经 `place`：状态拼成快照 `fit_snapshot`、一台的视图 `FitHost`；登记过的清单 `known_lists`、找清单行 `list_row`）；`need_mb`、`card_need`、`ram_need`、`cpu_need`；`cpu_give`（`Worker.cpus_for`、`Worker.acct`）；窗口核数 `measure` 记的 `use.pks`、`pk0`
96. **任务要跑多久；用量行的同卡并行数；此刻的 k、推演开跑时的 k 与运行中任务自己的 k**：任务要跑多久（同型号的同类历史、在跑同类按进度估的（只认看板 `collect.eta_by_progress` 标进 `eta.json` 的 `prog` 的任务，避免无进度时的估时回声，#149）、别的型号上同类的比值、借来的实际/估时比值（同 tag › 同名字前缀 › 同一清单 › 本项目，各取中位数，#113；同型号的没有再看别的型号的，#139）、清单估时；同类里实际/估时 偏出 `RATIO_OK` 的记录当时的估时不可信：不拿它放大改过的估时（同类改取实际用时，#134）；借给别的名字时偏出的只在离那一档中位数 3 倍内、且至少两类都这么偏时才借（#139）；同类与比值的记录按同卡并行数 k 挑（按比例的差，`NEAR_TOL`，#138）；推演、回填、开跑先记的用量与截止前的准入共用，回填再乘 `DUR_MARGIN`；截止前的准入按已含余量的 `cut`（max(中间值 × `DUR_MARGIN`, 同类最长的一次)，清单估时与别的型号 × `CUT_EST_MARGIN`，#139）；清单任务经 `Worker.dur_k` 把清单行的 tag 与所在清单传进去）；用量行的同卡并行数；此刻的 k、推演开跑时的 k 与运行中任务自己的 k
   实现：worker 的 `duration`（同类 `same_dur`、借比值 `ratio_dur`（能借的 `borrowable`）、截止用的 `with_cut`；挑记录 `near`、`rec_conc`，k 取整 `conc_key`，跑成功 `ran_ok`，比值可信 `trusted`；各清单的写进状态的 `dur`，探针带回看板）；`measure` 记的 `conc`（此刻同卡几条 `card_conc`，`Worker.k_of` 的回退也用它）；`Worker.k_now`、`Worker.dur_k`（job_spec 的 `durk`，`simulate` 的 `launch` 调）、`Worker.k_of`（`run_end`、`io_suspect`，状态里运行中那一行的 `dur` 也按它：`queue_durs`）、在跑同类的总长 `run_tot`（模块级，`Worker.run_tot` 与 `fit` 共用；只认看板 `collect.eta_by_progress` 写进 `eta.json` 的 `prog` 且扣挂起）；请求与运行中任务取 `req_dur`（运行中的传 `k_of`；`src=True` 连同依据，截止前跑不完的原因写它；`cut=True` 取截止用的，成组的经 `gang_dur`、别的主机的读 `gang.json` 的 `cut`），place 认的任务带 `cut`（`job_spec`，推演的截止取 `cut_min`、判 `fits_before(…, 1)`），准入取 `dur_min`
97. **任务依赖**：任务依赖（清单行尾 `after=` 跑成功、`afterany=` 跑完，按 项目/任务名 认；各主机把还会跑的写进状态的 `todo`；被等的状态：还会跑的（含 worker 不在、没 stop 过的主机的）盖过以前的结果，再按本机清单的标记，其余看用量记录的最后一次；没满足的同被临时指令挡住：不排位、不算等待、不为它停保活；等不来的（失败、找不到、成环：清单顺序也算依赖）标 `dep`、记事件、告警；找不到要持续满 `DEP_MISS_S` 才报（起算时刻 `dep_miss`，写进状态、接管时找回）；推演排在依赖满足的时刻，占位任务的结束取它）
   实现：解析 worker 的 `hints`（`deps`、`deps_any`）、`dep_keys`；本机清单的状态 `Worker.list_state`（`marks`、`todo`；清单里还会跑的行 `rows_left`，推演也用它）；状态 `Worker.dep_state`（`dep_idx`、`dead`、`dep_where`；别的主机的由 `peers` 读进 `peer_ends`、`peer_todo`，跳过已 `stopped` 主机并按主机归属）；放行 `Worker.dep_block`（`prep` 的 `dep`，且 `tick` 在 `forecast` 后用同轮收敛的 `self.ends` 刷新 `wait` 的原因；成环 `dep_cycle`，等待原因里的说明 `dep_note`，只读判定 `Worker.dep_waiting`；依赖图的边 `Worker.dep_next`，offers 插行前问会不会成环 `Worker.dep_reaches`）；谁写了依赖等着谁 `Worker.dep_waiters`（offers 的 `cant_move` 与 `fit --list` 共用）；`fit --list` 判前置经 `fit_deps`（同一份 `dep_state`、`dep_base_ends`、`dep_block`，#144、#166）；何时满足 `dep_end`（`simulate`、`req_dur`、`run_end` 与 `dep_block` 共用；基础结束表 `Worker.dep_base_ends`，`forecast` 迭代收敛汇进 `self.ends`），名字 `dep_names`；看板取 plan 的 `deps` 与等待项的 `dep`（`collect._job_times`、`dep:` 告警）；页面上 `depLines`、`holdGuess`（page.html），工作台 `alertsOf`、`rankTip`
98. **成组起跑**：成组起跑（清单行尾 `gang=组名:人数`，按 项目 + 组名 认，#180：成员按各台状态 `todo` 的 `gang` 认，此刻怎样经各台的 `gang.json` 交换；人数对不上、成环同依赖挡住；放得下先留资源（留到组里预计一起开跑的时刻，这之前跑得完的回填、同组的彼此不算回填，两组互挡时按 (项目, 组名) 排后的让出；留下的同放行记用量）、本机的都留好、别的主机的都此刻就能才一起放行；按同一次开跑认同伴（在跑的开跑时刻相差 `GANG_JOIN_S` 内；结束的要晚于开跑时见过的那次 `gang_last`，记在 `gang_with` 的第三项）：失败的跟着停、记失败（还没跟上开跑的也记失败），停下重排的、没跟上的跟着停、重排；回填用了留着的资源就不算留好；截止与回填按组里最长的；推演里一起排，排不上的记 `stuck_dep`）
   实现：解析 worker 的 `hints`（`gang`、`gn`）；认组、读同伴 `Worker.gang_sync`（`gctx`、给 place 与推演的 `gsim`；同伴怎样 `gang_when`，人数 `gang_bad`）；挡住 `Worker.gang_block`（`prep` 的 `dep`；成环 `dep_cycle` 的“组”边）；留资源 `place`（`gholds` 即 `Worker.gang_hold`，留到 `gang_until`，记账 `acct_job`）；放掉这一轮没重新留的（让位给屏障的）、放行后复核留着的 `Worker.gang_recheck`；一起放行 `Worker.gang_commit`，推演里 `simulate`（最早何时放得下 `gfirst` → `Worker.gang_eta`）；一起停 `Worker.gang_watch`（同伴在这一次里怎样 `gang_peer`，开跑时见过的结束 `gang_last`；`gang_when` 认同一次同一口径）、`Worker.gang_stop`、结果 `Worker.gang_end`、收尾 `Worker.finish`；没开跑的跟着失败 `Worker.gang_lost`（近来见过的同伴在哪台 `gang_known`，写进 `gang.json` 的 `known`、`load_prev` 读回）、`Worker.gang_doomed`；写、读 `gang.json` `Worker.gang_recs`、`gang_publish`、`gang_file`、`gang_cur`，主循环重读 `Worker.gang_poll`；时长 `Worker.gang_dur`（`req_dur` 不给 k 时经它）；停不停保活 `Worker.gang_soon`
99. **放不放一个请求；资源快照；各卡与全停时还能给的显存、内存与核数**：放不放一个请求（按资源快照定：卡、内存、CPU、tag、独占，外面一层是成组的留资源；真放行与推演同一份）；快照（真放行从实时采样拼，推演换成推演口径）；各卡此刻还能给新任务的显存、还能给的内存与核数；labmon 全停时还能给的内存（收不回的 `mem.anon` 算别人的，页缓存不算）与核数（旧 `gpu_queue.sh` 的任务按实测算别人的）
   实现：worker 的 `place(x, S, t, pre)`（核心 `_place` 只经它调）；`Worker.snapshot`、`sim_snapshot`；`card_free`、`ram_free`、`cpu_free`；`Worker.mem_room`、`Worker.cpu_room`
100. **容器里收不回的内存**：容器里收不回的内存（cgroup 用量 − 文件页：v1 `usage − total_active_file − total_inactive_file`，v2 `memory.current − active_file − inactive_file`；含 shmem 与内核占用）
   实现：`worker.pure.mem_unevictable`（worker `host_info` 与组内服务器 `fleet.parse` 共用；探针 `fleet_probe.sh` 只送 `memuse`、`inact`、`actf` 字段）
101. **卡吃不满是不是真有余量；开跑给的核数给少、给多；建议核数封顶；按峰值估核数；`LABMON_CPUS`**：卡吃不满是不是真有余量（容器在限流（只读状态的 `throttle`，门槛在 `thr_step`）、利用率低于 `UTIL_LOW` 的卡上在跑的被 CPU 卡住；fit 开车道与同机多开车道的提示共用，#35、#37）；开跑给的核数给少了、给多了（满 `CPU_WARM_S` 才判：给少 = `use.pks` 平均 ≥ 给的 × `CPU_BOUND_FRAC` 且所用卡有利用率低于 `UTIL_LOW` 的，给多 = 写了 `cpus=`、`cores_peak` ≤ 给的 × `CPU_SPARE_FRAC` 且少 `CPU_SPARE_MIN` 核；状态里运行中任务的 `cpu_fit`（写状态时补上类 `by`、`cls`：来源与类一起认，#99），看板只读它出只给会话的 `cpu_low:`、`cpu_high:`，同类跨主机合成一条，#46、#91）；给少了的建议核数封顶到这一类每条最多能给的（labmon 全停时能给的减同机别的任务已记的，按本项目同类同时在跑的条数均分，#112）；按峰值估几个核（×1.1 取整至少 1）；开跑时导出的 `LABMON_CPUS`
   实现：worker 的 `gpu_slack`、`cpu_fit`（`gpu_slack` 只取它的 low；类 `cpu_cls`；每条最多能给的 `cpu_budget`，`Worker.cpu_hints` 按快照算）、`cpu_from_peak`（`estimate` 与 `cpu_fit` 共用）；`cpus_export`（`Worker.cpus_for` 与 `cpu_fit` 共用）；看板 `collect._host_alerts`（合并同类 `collect._cpu_fit_alerts`，各台核预算那一句 `collect._cpu_room_text`）
102. **放不下的原因与原因代码；“CPU 不够”里核被谁记着；因 CPU 被挡多久才告警**：放不下的原因（短句与整句一处列全）与原因代码（快照的 `code`，`note_waits` 按 `CPU_CODES` 认因 CPU 被挡）；“CPU 不够”里核被谁记着、是不是记账比实测多挡住的（`cpu_est` 还是 `cpu`）；因 CPU 被挡满 10 分钟才告警（只认 `CPU_ALERT_CODES`，核真被自己的任务用着的不报；等待项的 `cpu_wait`，看板只认这个字段）
   实现：worker 的 `WHY`、`why_text`、`why_of`；`cpu_held`（`fit` 也用；已回收的不算记账比实测多：`cpu_warm`，与 `Worker.acct` 共用）；放不下、tag 到上限的判定 `nofit`、`tag_full`；`Worker.note_waits`（`CPU_ALERT_S`）；看板 `collect._lane` 的 `cpu:` 告警、工作台 `alertsOf`
103. **放行顺序**：放行顺序（先比人：同一人的几个项目用量合起来算一份；同一人的项目之间 focus、用量 ÷ 权重（放行的与成组留下资源的都当轮记上用量）、先来先跑，#23；等满 `wait_cap_min` 卡在资源上的第一个成为屏障，后面的只许回填；独占留卡上的回填同一个判定；真实放行与推演共用）
   实现：worker 的 `fair_key`（各人合计 `person_use`）、`admit`、`fits_before(t, dur_min, until)`（截止前跑得完）、`shadow`（屏障预计何时放行）、模块级 `hold_until`（独占留卡何时腾空）
104. **等待中任务的排位与解释**：等待中任务的排位与解释（第几、这人的用量 `puse`、优先、用量 ÷ 权重、屏障、被谁挡着；预计开始）
   实现：worker 的 `admit` 给的 `pos`、`forecast` 记的 `starts`（状态里等待项的 `pos`、`start`）；页面上 `rankTip`（overview_js/sched.js）
105. **放行前等了多久**：放行前等了多久（从排到队首到放行；被临时指令挡住的时间不算，解除后重新起算）
   实现：worker 的 `tick` 定 `since`、`launch` 记 `wait`（运行中状态与 `usage.jsonl`）；调度卡的平均等待 `overview.sched_view`
106. **调度卡**：调度卡（规则一句话、各项目已用 / 应得、占卡、排队、平均等待；按人分组；改权重、优先）
   实现：`overview.sched_view`（快照的 `sched`；各项目的 `person`、`pk`，普通用户看别人的去掉 `person`：`users.view`；平均等待从本机存的用量记录 `overview/usage/<主机>.jsonl` 按 24 小时窗口算，在跑的取状态）；页面上 `schedCard`、`schedAct`，一个项目的权重胶囊 `schedW`（「权重 − ×1 +」，档 `W_STEPS`、`wStep`，#143）与按钮 `schedBtns`（优先、暂停各一个 `schedFlag`），已用 / 应得条 `shareBar`（近期在用的项目、用量和与人数 `schedOn`，按人分组时人那一行 `personBar`，几个人在用 `schedPeople`，人员键 `pkOf`、名字 `who`）（项目「管理」抽屉的 `projSchedSec` 共用，回执 `SMSG`；overview_js/sched.js，`projSchedSec` 在 overview_js/project.js）；`POST /sched/…`（`serve._sched`；暂停、恢复经 `records.tell` 告诉会话）
107. **公平份额**：公平份额（各项目衰减的 GPU 卡·时，跨主机：各主机 `usage.jsonl` + 别的主机状态里的 `share.run` + 本机在跑的；占几张卡；按人合起来的见「放行顺序」）
   实现：worker 的 `usage`、`card_share`（`cards`）、`share`、`own_runs`、`peers`（状态里的 `share`）；一个人名下各项目的合计共用 `worker.pure.person_use`（worker `fair_key` 与调度卡 `overview.sched_view` 按完整的 `share.use` 算 `puse`）
108. **调度设置**：调度设置（hosts.toml 的 `[sched]`：半衰期、等待上限、权重；缺省值只在 worker）
   实现：`config.sched` → 各主机的 `sched.json`（`overview.collect` 另加各项目算在谁名下的 `owner`，#23）→ worker 的 `policy`（缺省 `POLICY`）；改 `[sched.*]` 一行 `config._sched_set`，页面上改权重 `config.set_weight`
109. **项目级的暂停、优先；`pause --now` 的“只停一次”；暂停是谁下的与普通用户能不能动它**：项目级的暂停、优先（所有主机，含之后加入的：hosts.toml 的 `[sched.pause]`、`[sched.focus]`，#96；只给某台的 `--host` 仍是那台的临时指令）；不带 `--host` 的 `pause --now` 给各台的“只停一次”；暂停是谁下的与普通用户能不能动它（项目级的与只给某几台的同一条；优先只给管理员，路由表管；#16、#27）
   实现：改 `config.set_sched_ovr`（命令行 `cmd_override`、`POST /sched/pause…` 都经它；谁下的 `by` 同一次写进 `[sched.pause_by]`，读 `config.pause_by`）；只给某几台的谁下的记在那台指令的 `by`（`workerctl.override_each` 的 `by`，worker 的 `override add --by`），快照里找它们 `projects.host_ovr`；判断 `users.pause_ok`（`need_pause` 抛 403；归档挑撤哪些也只经它），普通用户写那几台时另带 `expect_by`，worker 在 `overrides.json` 的锁里再核一次现状（同 `--expect-project`，不另判谁能动）；到期删行 `config.expire_sched`（`overview.collect` 每轮调）；随 `config.sched` 下发，worker 的 `sched_ovr` 接进 `self.ovr`；“只停一次”是 worker 指令的 `once`（`load_overrides` 停完照样挡、全局的送到才删，最多 `ONCE_HOLD_S`）；调度卡上 `schedBtns`（`schedFlag`）、`flagTip`（overview_js/sched.js），快照 `sched_view` 的 `focus`、`paused`
110. **看板写给 worker 的文件**：看板写给 worker 的文件（`eta.json`、`sched.json`、`returned.json`（主机清单里已归还的，取 `config.user_hosts` 的 `returned`）；变了才写，与读状态同一条 ssh）
   实现：`overview.fetch` 的 `h["put"]`（`overview.collect` 给出）
111. **推演**：推演（各任务预计何时开始、结束、等谁跑完，排不上的原因；页面上排队任务的预计时间只取它）
   实现：worker 的 `simulate`、`forecast`（状态里的 `plan`，探针带回看板）；运行中任务的预计结束由看板写回 `eta.json`，worker 的 `run_end` 取用（占位任务不用）；项目看板的排队任务按车道与这一轮在跑任务的预计结束对齐 `collect._queued_times`；工作台读 plan 的 `planLanes`、`planEnd`、`stuckWhy`（overview_js/home.js）
112. **多主机负载均衡的提示**：多主机负载均衡的提示（别的主机上同一项目队尾哪几行这台能接、接来几点跑完：同一份 `simulate`，依赖按 `dep_state`；原处跑不了（trapped）与说不出何时放行（unknown）的认定（推演停在依赖上的算 unknown：`simulate` 的 `on_dep` → plan 车道的 `stuck_dep`，#183）；一批只取同一种环境（目录与 `.env`，不比 `QUEUE_STATE`、`QUEUE_LOGS` 两行）、只接到同一种环境的清单后面，插在那条清单里第一条写了依赖的行前面、挪来的依赖行排后；一次至多 `MOVE_MAX` 行、挪来的已不早于原处剩下的最晚完成就停（平衡点，#110）；同机挪到同一种环境、最早跑完的另一条清单（`local` 且 `lane` 非空，车道数不变、不看卡的余量，#110）；同机多开一条车道（`local`、`lane` 为空，卡的真余量与 fit 同一条 `gpu_slack`）；挪来的行没估时的提醒（`nodur` → `rebalance:<项目>:est`）；接收方空着（没有 labmon 任务在跑，`idle`）时挑行跳过挪不动、说不出何时放行的清单，看板没有整批的挪法时按行比（#62）；按项目汇成一条挪法建议，门槛（`_need`）、滞回与目标沿用；看板按这一轮的来源状态截掉已不在原处的行、读失败的主机沿用上一轮的 offers；原因句由看板拼（offers 的 `why` 只放原因、`stop` 是那一行）；`rebalance:<项目>` 告警与 `borrow_cut` 句末的挪法；页面上一句话或原因）
   实现：worker 的 `offers`（`proj_lanes`：还会跑的行取各台的 `todo`，不另算，并判并行窗口是否被等依赖的行占满 `par_dep_win`；`move_cands`（一条队尾为什么不挪与当前缺失但可自补的 `needs_miss` 摘要 `cant_move`，#190；窗口外因占格推后标 `par_dep_full`，#166）、`try_move`；空着 `Worker.idle`；同机的 `local_offers`（挪到另一条清单 `Worker.lane_offer`，同机逐行加 `Worker.local_moves`；插在哪一行前面 `Worker.move_before`，与 `offers` 共用；平衡点 `Worker.moved_past`）、能放的卡 `Worker.local_cards`（`UTIL_FULL`，各卡利用率 `Worker.util10`）；推演的车道 `plan_lanes`；清单行的资源提示 `list_of`、清单的环境 `list_env`；别的主机状态 `peer_st`、worker 不在的 `peer_todo`，其中看板说已归还的 `gone` 不挡）；`rebalance.suggest`（告警 key 带挪法 `rebalance.move_key`（目标与第一行，#109）、缺缓存汇总 `_needs_miss`（#190）、窗口占满标记 `par_dep_full`（#166）、文字 `_text`、原因句 `_why`、没估时的 `_est_text`、空着时按行比 `_idle_gain`，核对 `_live`、`_fresh`；服务日志判变没变时去掉句尾推演时刻 `rebalance.log_text`）、`rebalance.keep_offers`（主机项的 `offers_kept`）；吃不满的主机还能多开几条（#145：worker 的 `Worker.lane_room` 给 offer 的 `room`，看板 `rebalance._under`（`UNDER_BUSY`、`UNDER_KEEP`）放宽门槛到 `GAIN_MIN_S`、句首 `rebalance._room_text`、排队行数 `rebalance._queued`，key `rebalance:<项目>:room:<主机>`）；`collect._rebalance`；页面上一个图标 `rebalMark`、悬停 `rebalTip`（common.js）
113. **工作台上运行中任务的预计结束**：工作台上运行中任务的预计结束（退路顺序：项目看板按进度算的 → plan 里的 run 条 → worker 给这条清单行的时长 `queues[].dur` → 清单估时）
   实现：`etaOf`（overview_js/home.js）
114. **运行中任务剩余时长说不准时的估计**：运行中任务剩余时长说不准时的估计（已用的 20%、至少 10 分钟：worker 估超了预计的，看板估超了估时又看不出进度的（没有进度行、进度停了，#138）与第一段只有一个步数 > 0 的采样、又没有估时或已超估时的）
   实现：`worker.pure.overrun_left`（`OVER_FRAC`、`OVER_MIN_S`；worker `run_end` 与看板 `collect._job_times`、`_segment_eta` 共用）
115. **各卡与纯 CPU 被谁占着**：各卡及非占位纯 CPU 任务（`g=[]`）被谁占着（已跑的用量记录存在本机，加推演；总览与主机页有纯 CPU 任务的主机在卡行下方动态追加 `CPU` 行，项目页经 `/p/<id>/events.json` 传 `skip` 时缺省仅取 GPU 占卡，#207）
   实现：`overview._keep_usage`、`overview.occupancy`（项目页经 `/p/<id>/events.json` 取）
116. **工作台存的用量记录；存着记录的主机**：工作台存的用量记录（各主机 worker 跑完的任务，30 天；时间线已跑占用、调度卡平均等待、项目页失败的尝试都读它）；存着记录的主机（不知道在哪台跑时各台都看）
   实现：路径 `state.usage_file`；`state.usage_hosts`（`waits.JobWatch.check`、`collect._attempts`）；项目页的失败尝试 `collect._attempts`
117. **等待原因的结构化字段**：等待原因的结构化字段（缺本地文件又补不了；本地依赖怎么补）
   实现：worker 等待项的 `miss`（`place` 定）、`local[].how`（`copy`/`redo`/`mixed`）；看板按字段认，不按 `why` 文字
118. **容量单位**：容量单位（GiB = 2^30；挂载点、共享盘、内存）
   实现：`worker.pure.GIB`（`config.GIB` 与 worker 共用）；页面上 `GIB`（common.js）
119. **时间线**：时间线（视图与拖动缩放、刻度、里程碑与现在线、按主机分组的分隔线（行的 `sep`）、任务条、占用条、全量稳定分道（#143）、超 4 道压细与超上限最后一道「+N 个」聚合条（#178）、拖动/滚轮平移缩放布局锁（#143）、图例、左栏按行名放宽（`lw`）；项目页与工作台共用）
   实现：common.js 的 `tlInit`、`tlRange`、`tlBind`、`tlSvg`、`tlShow`、`jobBar`、`tlPack`、`tlRowLayout`、`tlOverflow`、`tlMoreBar`、`occBars`（状态名 `OCC_ST`、行布局 `OCC_CARD_LAY`/`OCC_OTHER_LAY`、图例 `occItems`）；拖动布局锁 `tlFrame`、`tlLock`、`tlUnlock`、`tlLockFlag`、`tlLockRow`、`tlLockLw`；「全部」要包住的区间 `tlSpan`；近景按现在前后的任务定宽 `tlFit`（参数 `TL_FIT`，两页的 `near()` 给任务）；排在谁后面 `afterText`
120. **项目页时间线的依赖连线**：项目页时间线的依赖连线（#111：边取本项目的 `deps`、两端都画在图上；平时只画关键的：同一车道的、能经别的依赖或车道顺序推出来的不画；悬停或触屏点一格高亮它的整条上下游（只沿依赖），链上不是传递边的线显出；线压在条下面（`tlSvg` 的 `o.under`），直角折线走行缝）
   实现：`depLines`（边 `depEdges`、走法 `depRoute`、传递判定 `depVia`）、`depChain`、`depOn`、悬停与点 `depHover`（状态 `DEPH`）；条的位置、行号由 `laneRow` 记进 `c.pos`（page.html）
121. **触屏上点一格：第一次只看，3 秒内再点同一格才动作**：触屏上点一格：第一次只看（悬停文字），3 秒内再点同一格才动作（收起行小方块定位、任务条跳到任务明细）
   实现：`tapTwice`（page.html；`gsqClick` 与任务条的点击共用）
122. **自动归组**：自动归组（不属于任何 `[[groups]]` 的任务按名字前缀成组，id 即前缀：归组与页面上把自动组写成 `[[groups]]` 时认 id 同一条正则）
   实现：`worker.pure.GROUP_RE`（看板导出为 `config.AUTO_GROUP_RE` 给 `collect._group_of`、`config.set_project` 用，worker `duration` 借同前缀估时比值直接共用）
123. **按估计字宽截断文字与同一估计下的字宽；项目页时间线条上的任务名**：按估计字宽截断文字（缺省截尾；条上的任务名省略中间，可原样留一段前缀）与同一估计下的字宽；项目页时间线条上的任务名（去掉自动分组前缀与 `hold_`）
   实现：`fit`、`textW`（common.js）；`barName`（page.html）
124. **正在操作；点了不能打字的地方先让输入框失焦**：正在操作（正拖着排顺序，焦点在可打字的输入框或下拉框里（勾选框、按钮类不算），或在正文、抽屉里选着文字且 1 分钟内动过选区；定时刷新先不重画，免得冲掉输入）；点了不能打字的地方（按钮、勾选框、文字）先让输入框失焦（Safari 点按钮不挪焦点，#131：按钮不再各自 blur）
   实现：common.js 的 `editing`（能打字的控件 `typingEl`）与它下面捕获阶段的 click 监听；工作台 `selecting`（overview_js/app.js），项目页 `redraw`（page.html）
125. **拖动排顺序**：拖动排顺序（pointer 事件，鼠标与触屏同一套；只从把手起拖，被拖的行跟着指针、其余行实时让位、落点画虚线占位，Esc、拖出列表取消，靠边自动滚；样式 `sort-*` 在用它的页面里；#146）
   实现：common.js 的 `sortBind`（`SORT`，`sortMove`、`sortEnd`）；计划项排顺序经它（overview_js/project.js，键盘用 ↑↓ 按钮）
126. **本机存的设置**：本机存的设置（JSON，读写出错当没有；工作台 `ST`，项目页按标题分 `SS` 这一次浏览、`LS` 长期）
   实现：`mkStore(前缀, 存储)`（common.js）给 `get(k, d)` / `set(k, v)`
127. **worker 读的用量历史；写一行**：worker 读的用量历史（各主机的 `usage.jsonl` 一起看）；写一行（任务结束、没开跑就跟着同伴记失败的成组成员）
   实现：worker 的 `load_hist`；`Worker.usage_rec`（`finish`、`gang_doomed` 共用）
128. **任务的进程归集、CPU、读写与挂起测量；一棵进程树这一段用掉的 CPU**：任务进程归集统一走 `worker.pure.proc_tree`（支持 `pgids` 与 `exclude`）与 `job_procs`（`scan_procs` 顺带解析 `/proc/<pid>/stat` 的 `pgid`）：单元申请（`slot`）先归集，默认按根进程 PPID 树认，当根拥有独立进程组（`pgid == root_pid` 且不同于 worker 自身 `pgid`）时并入同 `pgid` 进程及其子孙（避免脱壳/孤儿子进程漏计，同时防止共享 `pgid` 误拉）；清单任务（及旧 `gpu_queue.sh` 任务）按 PPID 树并入独立会话（`sid == root_pid` 且不同于 worker 自身 `sid`）与独立进程组进程及其子孙，排除 worker 自身子树、其他运行中任务根树及已归属进程（#196）。任务的 CPU、读写与挂起测量（归集进程 `/proc/<pid>/io` 与 CPU 累计求和、此刻速率、1 分钟峰值、累计字节；任一进程处于 `T` 状态（SIGSTOP）时累计挂起秒数 `use.sus_s`，重启接管找回，`usage_rec` 的 `min`、`finish` 日志、`run_end` 与 `run_tot` 统一扣除，用量行另记 `sus` 秒，#149）；一棵进程树这一段用掉的 CPU（累计之和求差，负数按 0；保活进程树的核数也用它）
   实现：worker 的 `measure`（`proc_io`、`PEAK_WIN`、`use.sus_s`）、`job_sus`（`Worker.usage_rec`、`Worker.finish`、`Worker.run_end`、`run_tot` 共用）、`cpu_since`（`Worker.ka_cores` 也调）；页面上 `ioText`（overview_js/home.js）
129. **主机的 CPU 与读写压力；共享盘慢的基线；共享盘此刻慢不慢**：主机的 CPU 与读写压力（CPU 限流、共享盘每次读写的执行时间、本地盘忙碌、PSI：近 1 分钟的此刻值与每 5 分钟一行的 `press.jsonl`）；共享盘慢的基线（近 14 天每秒不少于 2 次的样本的 95 分位、不低于 20 ms、样本不够时取 20 ms）；共享盘此刻慢不慢（超过基线且每秒不少于 `IO_MIN_OPS` 次）
   实现：worker 的 `Worker.press`（`press_raw`、`press_rates`、`nfs_counts`、`local_dev`、`disk_counts`、`psi`）、`io_base`、`nfs_slow`（状态的 `info.press.nfs.slow`，`sample` 记进 `Worker.slow`）；看板曲线 `overview._series` 的 `thr`、`nfs`、`busy`、`anon`
130. **容器 CPU 限流与超核的任务**：容器 CPU 限流与超核的任务（近 1 分钟被限流的周期 ≥ `THR_HOT` 起算、降到 `THR_COOL` 以下结束，持续满 `THR_HOLD_S` 才告警；超核 = 1 分钟窗口核数 ≥ `LABMON_CPUS` × `CPU_OVER_RATIO` 且多出 `CPU_OVER_MIN` 核；门槛只在 worker，看板与 `fit` 只读状态的 `throttle`，#39）
   实现：worker 的 `thr_step`（`Worker.press` 调，起点 `thr_since` 由 `load_prev` 找回）、`cpu_over`、`Worker.throttle`（状态的 `throttle`）；看板项目告警 `collect._host_alerts` 的 `throttle:`；页面上 `thrText`、`thrTag`、`thrOver`（overview_js/core.js：工作台告警 `alertsOf`、主机表、资源卡共用）
131. **GPU 利用率的两种口径；近 24 小时平均；项目自己的平均利用率；页面上的利用率文字**：GPU 利用率的两种口径（#51：worker 每 10 秒一个样本取窗口平均；「在用时多高」近 10 分钟、卡上有保活的那几轮不记，只给 fit；「吃满程度」近 10 分钟与 1 小时、卡上只有保活时记 0，页面、每小时平均与 `gpu_idle` 告警用）；近 24 小时平均；项目自己的平均利用率；页面上的利用率文字
   实现：worker 的 `win_mean`（`Worker.util_window` 记 `uwin`、`bwin`；状态的 `util10`、`gpu_busy`（`Worker.busy_view`））；看板 `overview.gpu_busy`（series 卡的第 4 项）、项目快照的 `hosts[].busy`、`history[].busy`（`collect._hosts`）、`collect.gpu_mine`（用量记录的 `u`）；自用主机样本的 `gpus[].busy`（`fleet.from_worker`）；页面上 `utilText`、`utilAvg`、`utilMine`、`gpuBusy`（common.js），组内页取样本的 `fUtil`、`fCardU`（overview_js/fleet.js）
132. **共享盘慢时推迟读写共享盘的事**：共享盘慢时推迟读写共享盘的事（共享盘扫描、本地盘扫描、拷本地依赖；同一件最多推迟 `IO_YIELD_MAX_S`）
   实现：worker 的 `Worker.io_yield`（`Disk.step` 的 `defer`、`stage_bg` 调它；状态的 `disk.yield`）；页面上 `diskProg`（overview_js/storage.js）
133. **读写拥塞嫌疑**：读写拥塞嫌疑（在跑的已跑过同类历史 `SLOW_RATIO` 倍、共享盘又慢：只记事件、同一任务一次；同一物理机按共享盘 `addr` 归并、计数不相加）
   实现：worker 的 `Worker.io_suspect`（`duration` 的 `same`、运行中任务的 `io_sus`、`peers` 记的 `peer_box`）；事件种类 `io_suspect`
134. **清单的 `cpus=`、`io=`提示与开跑导出的 `LABMON_CPUS`；核数取整与记账上限**：清单的 `cpus=`（进 CPU 记账）、`io=`（只记录）提示与开跑导出的 `LABMON_CPUS`（开跑时给这个任务记账的核数；满 10 分钟后记账按实测回收，它不变；没设的 `OMP_NUM_THREADS`、`MKL_NUM_THREADS`、`OPENBLAS_NUM_THREADS` 缺省同它，清单任务与单元共用，#38）；核数取整（四舍五入至少 1，`cpus=0` 是 0）与记账上限（配额向下取整）
   实现：worker 的 `hints`、`job_cpus`、`cpu_cap`（容器配额 `cpu_quota`）、`cpu_env`（`THREAD_VARS`；`Worker.start` 与 `slot_main` 共用）；页面上 `hintText`、`estText`（overview_js/home.js）
135. **任务日志里这一次的输出**：任务日志里这一次的输出（每次开跑写的分隔行之后：最后一行、进度、状态里的日志末尾、OOM 判断；逐字出现过的行算套话，最后一行跳过、日志末尾折叠、OOM 看原文）
   实现：worker 的 `START_RE`、`log_tail`（`fold`）；探针在服务器上跑、不 import worker，另有一份 `START_RE` 与去重（probe.py 取 `current` 那段）
136. **外来 GPU 进程；独占任务会被谁打扰**：外来 GPU 进程（不是 labmon 的）；独占任务会被谁打扰（放行前等腾空、只为 labmon 的留卡，与运行中作废共用一个判定）
   实现：worker 的 `sample`、`other_cpu`、`intruders`、`void_check`、`void_job`
137. **停下重排的任务与发停止请求**：停下重排的任务（作废、被对方挤掉、跟着成组的同伴停下；给 wait 的 `void:`，文字 `overview._voided`）与发停止请求
   实现：worker 的 `note_stop`（状态里的 `voided`，`how` 区分）、`send_stop`
138. **借来的主机上约定归还挡住、这台不会再放的清单**：借来的主机上约定归还挡住、这台不会再放的清单（约定归还是最早的截止时才算；看板 `borrow_cut:` 告警、工作台告警、`borrow_due` 提醒里跑不完的任务数都取它）
   实现：`borrow.late_jobs`（`borrow.check` 写进全局快照主机的 `borrow.late`，这一轮没拿到 worker 状态时沿用上一次的）；该怎么办的那句 `borrow.late_do`（按截止来自约定还是对方租期；给会话的写进快照主机的 `borrow.late_do`（`borrow_cut` 告警的 `msg`、`borrow_due` 的 wake），`user=True` 给用户的写进 `borrow.late_say`（页面上的 `borrow_cut`、`alertsOf`、弹给用户的 `borrow_due` 提醒））；`collect._lane` 的 `borrow_cut`，页面上 `alertsOf`（overview_js/core.js）
139. **对方轻用时能不能借、对方在用什么**：对方轻用时能不能借、对方在用什么（借来时在轻用记 `borrow_light`，决定要不要预先留）
   实现：`borrow.can_borrow`、`borrow.using`
140. **给对方留的量**：给对方留的量（按对方近 7 天轻用时的用量推荐、被挤到后调高；随 guest 指令下发，hold 定要不要预先留）
   实现：`borrow.reserve_for`、`borrow._keep`、`borrow.keep_text`；页面上 `keepText`（common.js）
141. **借来的主机上对方的用量、超出预留、扣预留准入、说好不用的时段与作废、快撞上限、被挤掉、OOM 先杀我们**：借来的主机上对方的用量（内存按不算页缓存的 `mem.anon`；内存与 CPU 都不含任何保活的进程树，与预留同口径；`ka.sh` 停不了的保活占的核 `other.ka_cpu` 另由 `cpu_room` 扣）、超出预留（CPU 连着两轮才算）、扣预留准入（要不要预先留）、对方说好不用的时段与作废、快撞上限、被挤掉（失败是不是 OOM 的判定与 finish 记用量行的 `oom` 共用）、OOM 先杀我们
   实现：worker 的 `other`（`sample` 里量）、`guest_scan`（`cpu_hot`；说好不用开始时对方已有的 GPU 进程 `away_base`）、`guest_block`、`held`、`keep_left`、`away`、`end_away`（状态的 `away_state`）、`guest_tight`、`squeezed_by`（判定 `oom_of`）、`oom_check`、`GUEST_ADJ`；看板写、清 `borrow_away` 在 `borrow.begin`、`borrow.check`
142. **labmon 之外的内存与 CPU 要扣掉的保活**：labmon 之外的内存与 CPU 要扣掉的保活（GPU 上认作保活的进程整棵进程树，含借来主机上对方另起、`ka.sh` 停不了的；上一轮认过、这一轮漏认但 PID 与启动时刻没变的照样算，停不了的照样算停不了的）：内存扣各进程 PSS 之和（借用的预留与 worker 判超出靠它同口径）；CPU worker 扣这一段的核数，组内探针只有 `ps` 的 %CPU（生命期平均，口径不同）
   实现：`worker.pure.proc_tree`（worker 单元/保活与组内服务器 `fleet.analyze` 共用）；worker 的 `mem_of`、`Worker.ka_cores`（`ka_roots`，`sample` 里扣；上一轮的累计记两种：所有保活合起来、各棵保活树分开，停不了的核数按后者求和）；组内服务器的 `fleet.analyze`（`mem_ka`，序列行记 `mk`；`borrow.reserve_for` 直接取 `ma`），探针 `fleet_probe.sh` 的 `pss` 段只送命令行带保活字样的 GPU 进程与其子孙的 PSS（字样由 `fleet.fetch` 传；探针在未必有 Python 的镜像里跑，另留 awk 一份）
143. **后台采集线程**：后台采集线程
   实现：`serve._Collector`
144. **页面连不上看板服务**：页面连不上看板服务（服务重启的那几秒：fetch 抛 TypeError；页面上的请求都经它，不露浏览器原始报错；只有做两遍也没事的——读数据、立即采集、立即探测——自动重试，别的写请求只提示再点；定时取数顶栏标连不上、提早再取；那几句话、顶栏那一处，#177）
   实现：`api`（`NET_WAIT`）、`netText`、`pollNet`、`offBadge`（common.js）
145. **看板服务接受哪些请求；页面上的写请求**：看板服务接受哪些请求（防跨站，先于登录：`Host` 只认 127.0.0.1、localhost 加这个入口的端口与 `[server]` 另认的；POST、PUT 只认 JSON 类型、带了 `Sec-Fetch-Site` 的只认同源与 `none`，#158）；页面上的写请求（一律带 JSON 类型；登录页、账号管理表单也是，`method="post"`、不写 `action`，提交由脚本截走）
   实现：`serve.Handler._gate`（Host 的判断 `serve.host_ok`，各入口认哪些 `hosts` 由 `serve.allowed_hosts` 定）；页面上 `postJSON`（common.js）；测试里 `tests/fixtures.post_req`；`tools/deploy.py` 的 `http`
146. **看板的账号、会话与令牌；请求是谁、能不能做；能不能做平台操作；命令行改账号**：看板的账号、会话与令牌（#160：口令 PBKDF2 加盐、常数时间比较；设置码、会话、令牌只存 SHA-256；连着输错锁定（不存在的名字同样计数、同样耗时）；作用域与角色取交集；吊销、撤管理员下一次请求就生效；`users.json` 读改写跨进程互斥）；请求是谁、能不能做（唯一鉴权入口）；能不能做平台操作；命令行改账号（只认看板服务的账号本人）
   实现：`users.check`（`users.ident`；`SCOPES`、`IMPLIES`、`ROLE_SCOPES`）、`users.can_ops`；登录、设置码、改口令 `users.login`、`setup`、`change_pw`（都经 `_attempt`）；`users.token_add`、`revoke`、`add`、`set_role`、`reset`、`remove`、`listing`；读改写 `users._edit`；命令行 `__main__.cmd_user`（`users.local_ok`）
147. **每条路由要什么权限；两个入口；没登录跳登录页；会话 Cookie 的属性**：每条路由要什么权限（作用域、只给管理员、只在内网入口；没登记的 GET 按 read、POST 按 admin）；两个入口（内网 `Server`、公网 `Listener`，平台操作的路由在公网入口上 404）；没登录跳登录页（相对地址、`next` 只认看板里的路径）；会话 Cookie 的属性
   实现：`serve.ROUTES`、`serve.route_of`、`serve.Handler._allow`；`serve.login_url`、`serve.safe_next`；`serve.Handler._cookie`
148. **项目主人、主机允许谁用、普通用户能操作哪些对象；普通用户名下最多几个项目；对象鉴权只在一处定**：项目主人、主机允许谁用、普通用户能操作哪些对象（#167）；普通用户名下最多几个项目（`USER_PROJECTS`，含已归档的，#18）；对象鉴权只在一处定（#19）
   实现：项目主人 `users.owner_of`（读 `meta.json` 的 `projects.owner` 只由它调；换主人 `projects.set_owner`）；登记新项目前 `users.need_room`；`config.host_users`、`config.set_host_users`；`serve.Handler._own` 按 `serve.ROUTES` 调用 `users.need_project`、`users.need_host`、`users.need_list`、`users.need_reg`、`users.need_room`、`users.need_pause` 与 `users.need_holder`（`fleet_srv`），同一份记成 `Handler.guard`，要在锁里再核的业务函数（`check_owner`、`api.override` 的 `check`、调度卡）只调它
149. **普通用户看到的全局快照与记录**：普通用户看到的全局快照与记录（自己的项目及可见主机；快照按白名单取，新加的字段默认不给，#17；#211 开放本人共享盘目录与告警、本人持有服务器实时状态与曲线（`fleet.scope = "self"`，借出保留借用人名并脱敏对方进程）；`pool` 暂恒为 `None`；他人明细、人员管理、平台操作、`notices`、`wake`、`hostcheck` 等仍仅管理员可看）
   实现：`users.access`、`users.view`（`/`、`/overview.json`；白名单 `VIEW_KEYS`、共享盘按人投影 `disk.person_storage`、本人服务器投影 `users._self_fleet`）、`users.visible`（`records.records` 的 `see`）
150. **个人推送**：个人推送（同 `notify.toml` 的配置格式，页面只显示打码目标；按账号发送）
   实现：`service.set_user_push`、`service.user_push`、`service.push_view`、`service.user_push_test`、`service.notify`；解析与发送复用 `push`
151. **客户端分发；最低版本与报告的版本；wait 自动更新；清旧版本目录；skill 随客户端分发与换版本后叫醒会话**：客户端分发（从 main 的已提交文件生成 wheel、安装脚本；命令行请求带提交号）；最低版本（只拦确认低于的，比不出先后的只提示，#17）与按令牌显示最近报告的版本；wait 自动更新后继续等待（426 时也先更新一次；装不上的同一版隔一阵才再自动试、不先下载，#18）；装好后删掉没有入口指着的旧版本目录（#18）；skill 随客户端分发（包里照仓库相对位置带 skill 与它链接的文档 `MIRROR`；固定入口随命令切换、智能体的入口没有才建、已有的只提示；旧版本换来的那次由新版本补建）与 wait 换版本后 skill 改了叫醒会话一次（逐节比对，#57）
   实现：`dist.wheel`、`dist.install_sh`、`dist.install_wheel`（回 `dist.Installed`）、`dist.version`；`serve.Handler._client`；`client.install_update`（手动与 wait 共用；wait 传 `auto`）；装不上的记录 `dist.note_failure`、`dist.failed_wait`、`dist.clear_failure`；清旧版本 `dist._prune`（哪一版 `dist._version_dir`；在用的版本 `dist.hold_version`，`__main__.main` 起来就加）；`dist.client_status`（按 Git 历史比较）；`users.ident`（记版本）；`__main__._wait_update`、`_wait_resume`、`_wait_outdated`；skill 入口 `dist.skill_entry`、`dist._link_skill`（`_agent_entries`）、`dist.ensure_skill`，比对 `dist.skill_changes`、那句话 `dist.skill_note`（`client.update` 与 `__main__._skill_woke` 共用）
152. **页面上的身份与按它收起按钮；401 回登录页**：页面上的身份（我是谁、能做什么：与 `GET /api/me` 同一份，看板服务发页面时填进 `let ME`）与按它收起按钮（平台操作、写操作、调度权重；管理员从公网入口来时平台操作换成一句话）；401 回登录页
   实现：`users.me`（`serve.Handler._page` 经 `collect.fill` 填）；页面上 `canOps`、`canWrite`、`canWeight`（权重与优先）、`opsNote`、`toLogin`（common.js）；登录页 `labmon/login.html`；人员的账号与我的账号 `ACC`、`accLoad`、`accPost`、`accAct`、`directoryDrawer`、`mePage`；共用会话与令牌行 `accItem`、一次性原文 `accShow`（overview_js/account.js，`directoryDrawer` 在 overview_js/fleetops.js）
153. **共享盘用量；扫描的节奏；按项目与按人投影**：共享盘用量（扫描、归人、增量、告警；主机页与项目页也取它）；扫描的节奏（共享盘与本地盘 `LocalDisk` 共用：一次做一件、做完不等下一轮接着下一件、限速按扫描者合起来算、耗时由扫描线程自己量，#153）；按项目（#53：登记了清单的项目根拆开扫、项目根下的一级目录各成单位，项目根取各台状态的 `storage.projects`；任务跑完提前重扫按各主机的用量行；按项目的合计、增量、任务写入量与 `disk:`、`bigwrite:` 告警；暴涨的门槛按人、按项目同一条）；按人投影（#211：普通用户与 `GET /api/disk` 共用一份按人过滤，仅下发总盘概况与本人目录/告警，屏蔽他人目录、全组名单、`owners_set` 与 `now`）
   实现：worker 的 `Disk`（`step`、`start`、`done`；项目根 `want`、`forced`）、`Worker.disk_ends`；`disk.update`（按项目 `_ours`、写入量 `_job_writes`/`_write`、门槛 `_burst`）、`disk.project_storage`（项目快照的 `storage`：项目页「本项目占用」与 `labmon disk` 共用）、`disk.person_storage`（`VIEW_DISK`、`VIEW_DISK_ALERTS`；`users.view` 与 `api.my_disk` / `labmon disk --me` 共用）、告警 `collect._storage`（同一组扫描的任务合成一组 `collect.write_groups`、那一句 `collect.write_text`、量的文字 `collect.gb`：`bigwrite:` 与 `labmon disk` 共用；目录叫法 `collect.dir_name`，页面上 `dirName`、`storageChip`，page.html）；拍与拍之间接着做 `Worker.scan_chain`、两种扫描出错共用的 `Worker.disk_do`；限速 `Pace`（接连几件共用的计数 `Disk.rate`）；共享盘上的量的文字 `GB`、`dGB`（common.js）；页面上普通用户容量条 `diskBarSelf`（overview_js/storage.js）
154. **本地盘开机以来的新写入**：本地盘开机以来的新写入（容器根盘测不测得准、私有 `/dev/shm`；worker 扫自己的根盘：与共享盘同一套 `Disk` 调度，只进开机后动过的目录、按前三级目录汇总；各项目缓存与单格临时同一次扫出）
   实现：worker 的 `rootfs_info`（状态的 `info.rootfs`）、`LocalDisk`（`scan_units`、`scan_unit` 的 `prune`、`since`，状态的 `storage`）；组内服务器的探针 `fleet_probe.sh` 的 `rootfs`、`shm` 行与 `scan` 段（find + awk，同一汇总规则：探针在未必有 Python 的镜像里跑，与 worker 各一份），本机 `fleet._scan`、要不要扫 `fleet.scan_due`、存进状态 `fleet._wrote`
155. **本地盘的新写入合成一份与那一句话**：本地盘的新写入合成一份（GiB：labmon 之外的、私有 `/dev/shm`、测得准、过 `LOCAL_ACK` 要勾确认）与那一句话（写了多少、大头、某操作会清空）
   实现：`fleet.local_writes`（组内服务器 `fleet.server_lw`，主机项的 `lw` 在 `overview._annotate`）、`fleet.lw_text`（`_risk`、`renew` 的提醒共用）；页面上 `lwText`（JS 与 Python 各一份，口径相同，`tests/pages/check.js` 与 `test_fleet` 对同一份快照断言同一句）；借来的主机归还时说借用期间写的 `borrow.wrote_note`，页面上 `lwLeft`
156. **本地盘的画法**：本地盘的画法（总量条：labmon 的缓存与单格临时用项目色，labmon 之外按目录一段段斜纹、过门槛用 `--heat`；资源行按配额画，明细按开机以来写的总量画（`lwBar` 的 `whole`），另有目录表、没扫的原因；关注档的小标与数字与共享盘同一套 `heatTag`、`heatNum`）
   实现：页面上 `lwBar`、`lwSegs`、`lwVal`、`lwBlock`、`lwHeat`、`lwPath`、`hostLw`（overview_js/storage.js：主机页资源卡与「本地盘」选项卡、组内卡片与服务器抽屉共用）
157. **共享盘增长的关注档**：共享盘增长的关注档（照近 24 小时、近 7 天里快的速度多少天占满剩余，或这人的 `burst:` 告警；「⇈暴涨」「↑在涨」与紫色 `--heat`：按人列表、人员卡、人员抽屉的小结与各目录共用；本地盘的 `lwHeat` 也经 `heatTag` 出小标）
   实现：页面上 `diskHeat`、`heatTag`、`heatNum`（`HEAT_HI`、`HEAT_MID`；暴涨告警在不在 `burstOf`，overview_js/storage.js）
158. **保活守护判定**：保活守护判定（这一台现在怎样、要不要开、重开或让出；自用主机是 `worker`、借用中是 `borrowed`：保活由 worker 停启；页面文字取它）
   实现：`fleet.guard_state`、`fleet.guard_act`；页面上 `guardText`、`gdSit`、`probeAt`、`gdIcon`（overview_js/fleet.js）
159. **在组内服务器上开、停保活；这一台开、停保活的命令**：在组内服务器上开、停保活（开前看 GPU、确认进程组已占 GPU 才报成功、开后盯着让路、复查空转后恢复；重开时停了旧的、新的没开成回 `gap`，下次探测直接再开一次，#26）；这一台开、停保活的命令（服务器自己写的优先，否则 `[fleet]` 段的）
   实现：`fleet.ka_start`、`fleet.ka_stop`（`fleet_guard.sh`）；`fleet.ka_cmd`；重开 `fleet_guard.sh` 的 `repair`，缺口 `guard_gap`（`fleet.guard_act` 记、`fleet._guard_track` 清、`fleet.guard_state` 判）
160. **停启保活的脚本**：停启保活的脚本（`labmon/ka.sh`，#105：随 worker 发到 `worker_home`，配置读 `ka_bin` 的 `ka.env`；退出码与失败原因写到标准错误是契约，锁与事件日志与共享盘上原来那份同一处）
   实现：`labmon/ka.sh`（`ka_procs`、`pgids`、`signal_ka`）；worker 经 `Worker.ka` 调（`KA_SH`、`KA_ENV`）；发出去在 `workerctl.run_out` 的 start
161. **卡上借用期间 worker 开的保活：认出来、登记成看板的保活**：卡上的保活是借用期间 labmon 的 worker 开的：认出来、登记成看板的保活（不动进程；worker 记下自己开的身份，归还时与还掉以后补登记共用一处核对，#102）
   实现：worker 的 `Worker.ka_mark`（状态的 `keepalive.mine`）；`fleet.ka_adopt` → `fleet_guard.sh` 的 `adopt`（事件日志 `ka_logged`，登记与盯着 `register`，与 `start` 共用）；归还 `borrow.finish`，补登记 `fleet.guard_state` 的 `adopt`（`_adopt_due`，探测样本的 `ka_group`）
162. **`ka.sh running` 的读法**：`ka.sh running` 的读法：退出码 0 就是保活在跑，非 0 是没在跑；输出每行一个进程组号，要认归属时只认列出的进程组，在跑却没列出的算认不了（不当作没在跑再开一个）（#26）
   实现：`fleet_guard.sh` 的 `running_groups`（回 1 没在跑、201 认不了、200 另有不认识的行（`strict`，停用户自己的保活前与借用核对 `kacheck` 用它）、其余是 ka.sh 自己的退出码）；worker 的 `Worker.ka_groups`（None 没在跑、字典可为空）。worker 与探针、守护脚本各自在服务器上跑、不 import 本包，口径相同
163. **进程出生时钟**：进程出生时钟（`/proc/<pid>/stat` 第 22 项，开机以来的时钟数；认同一个进程只比它，不换算成墙上时钟；看板登记的保活在登记行里记下它，#26）
   实现：`fleet_guard.sh` 的 `proc_birth`；探针 `fleet_probe.sh` 的 `birth`（`p1`、`ka_groups`、`guard` 段共用；两个脚本各自经标准输入送去，口径相同）；比对在 `fleet.parse`（`guard.identity`）；worker 要 epoch 秒时用 `started_at`
164. **所有卡利用率为零**：所有卡利用率为零（空转的证据：每张卡至少两条明确的零样本，读不到的、少一张的都不算）
   实现：`fleet.parse` 的每卡 `zero`、`fleet.analyze` 的 `zero_cards`（看板登记的 `guard.zero_cards`、自行开启的 `manual_guard`）；远端动手前复核 `fleet_guard.sh` 的 `gpu_zero`（探针在未必有 Python 的镜像里跑，与本机解析各一份）
165. **组内服务器 GPU 负载归类与按路径归人**：组内服务器 GPU 负载归类（只认保活，其余算实验）与按路径归人
   实现：`fleet.classify`、`fleet.mark_keepalive`、`disk.whose`（归属表同 `disk.owner_of`）
166. **进程跑的是哪个脚本、相对路径按工作目录还原；ka.sh 管的保活；借用前逐个核对**：进程跑的是哪个脚本、相对路径按工作目录还原（#29：认保活时相对路径起的同一脚本与写完整路径起的一样）；ka.sh 管的保活（python 跑 `AGRI_KA_DIR/AGRI_KA_SCRIPT`：命令行写完整路径，或相对路径还原、解开符号链接后是它）；借用前逐个核对
   实现：`worker.pure.script_arg`（worker 的看门狗 `script_of`、`ka_owned` 与看板 `fleet._abs_script` 共用）；worker 的 `ka_owned`；`labmon/ka.sh` 的 `ka_procs`（running 与 stop 发信号共用，#105；规则与 `ka_owned` 同一条，`tests/test_ka.py` 对同一组起法核对两边一致）；探针 `fleet_probe.sh` 的 `script_abs`（只规整路径；探针在未必有 Python 的镜像里跑，另留 shell 一份）；借用核对 `fleet_guard.sh` 的 `kacheck`（读 `ka.sh running` 只经 `running_groups strict`），结果的话 `borrow._ka_miss`
167. **固定配色**：固定配色（项目、计划项、共享盘上的人）
   实现：`config.pick_color`
168. **页面渲染；往页面里填一块数据**：页面渲染（模板 + `/*__JS:<相对路径>__*/` 统一内联 `common.js` 与 `overview_js/*.js` + 数据；写盘的原子写入）；往页面里填一块数据（`/*__DATA__*/null`、`/*__ME__*/null`，`<` 写成 `\u003c`）
   实现：`collect.render_page`（`_inline_js`）、`collect.write_page`；`collect.fill`
169. **页面上的租期文字**：页面上的租期文字（都按页面上的“现在”算：剩余秒数、一天内到期或已过、纯文字「剩 X」/「已过」、主机的「租期剩 X」、带配色的唯一出口）
   实现：`leaseLeft`、`leaseSoon`、`leaseRem`、`leaseText`、`leaseHtml`（overview_js/core.js）
170. **悬停提示**：悬停提示（显示、3 秒后收起、收起；重画后悬停的元素没了就收起；每页绑一次：移出窗口、失焦、滚动时收起，触屏点一下显示 3 秒）
   实现：common.js 的 `TIP`、`placeTip`、`showTip`、`flashTip`、`hideTip`、`tipCheck`、`tipBind`
171. **工作台的导航**：工作台的导航（宽屏侧栏、窄屏底栏与「更多」面板）
   实现：`navModel`（overview_js/core.js），`side`、`tabbar`、`morePanel` 只管画
172. **换地址与浏览器历史**：换地址与浏览器历史（往里点记一条；换栏目、选项卡、筛选、抽屉不记）
   实现：`nav`（overview_js/core.js），链接与行的点击都经它；例外是换人后抽屉跟到新的那台 `srvMoved`（overview_js/fleet.js，在 render 里调，经 nav 会重入，直接 `history.replaceState`）
173. **测试里的多主机；不读本机 /proc 与 cgroup；不连 ssh；用闸门看运行中状态；git 不认继承的 `GIT_*`**：测试里的多主机（共享一个 HOME 的几台 worker、别的主机的假状态与用量记录、建清单、推进时钟）；worker 的测试不读跑测试那台机器的 /proc 与 cgroup；测试不连 ssh（PATH 里的 ssh 换成立刻按连不上返回的假程序）；起真 worker 的用例要在任务还在跑时看状态，让任务停在闸门上、看完再放行（不靠任务睡得够久；要读用量行就等用量行：收尾先写 `.done` 后写它）；测试里起 git（建临时仓库、被测代码自己调的）不认继承来的 `GIT_*` 环境变量（会话里 export 过 `GIT_DIR`、`GIT_WORK_TREE` 时会改到真仓库）
   实现：`tests/fixtures.Hosts`（时钟 `Clock`）；`tests/fixtures.no_host`（`worker_env`、`worker_module` 都经它）；`tests/bin/ssh`（`fixtures.SSH_GUARD` 放到 PATH 最前面）；`tests/fixtures.gate`、`release`（读用量行 `WorkerBase.usage`）；`tests/fixtures.no_git_env`（用例的 `setUp` 或模块的 `setUpModule` 调）
174. **测试里起的看板服务要登录；离线预览按身份看**：测试里起的看板服务要登录（建账号与令牌不算 PBKDF2、不记进记录页；让用例里的 urlopen 都带令牌）；离线预览按身份看（管理员内网、管理员公网、普通用户）
   实现：`tests/fixtures.make_token`、`login`、`bearer_opener`；`tests/pages/preview.py --as`（`accounts`），页面测试的身份 `ADMIN_ME`（`tests/pages/dom.js`）
175. **部署到看板；哪些改动不用重启、要拉起 worker 或要浏览器核对页面；一次启动以来的 Traceback；页面文字漏 NaN/undefined/Infinity**：部署到看板（等平台操作、更新代码（本机 pull；远端打 git bundle 经 `POST /api/admin/deploy` 传过去，服务端快进、等平台操作做完再重启，#164）、按改动重启与拉起 worker、核对）；哪些改动不用重启、要拉起 worker 或要浏览器核对页面；服务日志里一次启动以来的 Traceback；渲染出的页面文字里漏了 NaN/undefined/Infinity（页面测试与部署的冒烟检查共用）
   实现：`tools/deploy.py` 的 `deploy`（远端模式 `_update_remote`、`make_bundle`、`wait_new`）；服务端与两种模式共用的判断在 `labmon/deploy.py`：`is_doc`、`is_worker`、`has_worker`、`is_page`、`log_text`、`tracebacks`、`apply`、`status`、在跑哪个提交 `RUNNING`；`leak`（`tests/pages/dom.js`，`check.js` 与 `smoke.js` 都取它）
176. **一台主机上除本项目外其他项目的聚合负载与提示（不含他人明细）**：只读已有 worker `status.json` 的 `running`、`todo`、`waiting`、`plan`，统计除当前项目外其他项目的在跑数（不含占位）、排队数（不含占位与暂停清单）与预计最晚忙到时刻（`#197`），格式化成极短提示（如 `他项 2 跑/8 排`、`他项 2 跑/8 排，忙至 23:40`），绝不输出他人项目名或任务名
   实现：`worker.pure.other_load`、`worker.pure.other_load_text`（worker 的 `fit.fit_list` 与看板的 `api.status` 共用）
177. **项目页主机的 CPU 实时占用与时间线同轴折线**：项目快照 `hosts[].cpu` 优先复用经 `fleet.worker_fresh` 判定的新鲜 worker 状态（`info.cpu_used` / `cpu_quota` / `nproc` / `press.thr` 与 `throttle`），不新鲜时回退探针 `cgroup` 差分，百分比按 0–100% 封顶（有配额按配额、无配额按可用核数换算）；`_long_term` 经共用 `_add_hourly` 同步维护 30 天 `cpu_hourly`；项目页顶部 CPU 卡片、顶栏悬停、时间线主机行与资源小图共用 `cpuView`，时间线经 `hostSeries` 在 GPU 折线区同轴叠画橙色 CPU 折线并由 `tlHostHover` 按样本自身间隔提供双读数（`#208`）
   实现：`collect._host_cpu`、`collect._add_hourly`；页面上 `cpuView`、`hostSeries`、`tlHostHover`（page.html）
178. **主机分类、独立主机判定与共享域隔离**：主机按正交属性 `platform_kind`（`scm | other | none`）、`share`（`pool | borrow | private`）与 `standalone` 分类（均支持向后兼容缺省推断）；`config.home_key` 对共享集群盘返回 `("shared", worker_home)`、对独立主机返回 `("host", id, worker_home)`，避免多台独立机默认 `worker_home` 撞名串扰；`lease.homes` 在有共享盘主机时排除独立机，`overview.sched_view` 优先集群主机，`disk._df`/`_ours` 排除独立机，`serve.reg_move` 与前端 `regTo`/`adoptSrc` 禁止跨 `home_key` 搬登记，`overview.check_project_idle` 对已归还独立机按最后状态放行；`health.lease_end` 支持外平台 `lease_until`，`renew.check` 跳过非 SCM 主机，`pool._self_bar` 与 `borrow.begin` 统一按 `host_share` 与 `is_standalone` 拦截（`#114`）
   实现：`config.host_platform`、`config.host_share`、`config.is_standalone`、`config.host_ssh_key`、`config.home_key`、`config.shell_path`；页面上 `standaloneBadge`（overview_js/core.js）
179. **独立主机清单目录防污染边界校验**：独立主机缺省 `data_root = ~/.local/share/labmon/work`、`local_dir = <data_root>/.tmp`，`workerctl.project_init` 建 `projects/<项目>/{code,queues,runs}` 隔离分层与独立机 README；`worker add` 在独立主机模式默认拒绝登记 `<data_root>/projects/` 之外的目录（含符号链接越界，可由 `allow_outside_root = true` 显式放行）（`#114`）
   实现：worker 的 `queue_root_ok`（`worker/cli.py`）；看板前置 `workerctl._check_standalone_root`、`workerctl.worker_env`
180. **推模式临时主机与云盘存储监控**：面向 Google Colab 等无 SSH 入口、无实验室共享盘的短生命周期 GPU 实例（`#213`）。服务端状态独立存在 `overview/ephemeral/<host_id>.json`（绝不写 `hosts.toml`，绝不写入 `labmon/disk.py`），经 `POST /api/ephemeral/push`（`project` 令牌、128 KiB 上限、5s 节流、单项目 5 台活跃上限、TTL 180s 钳制 90–600s、过 `lease.need`）接收推模式心跳与纯 `statvfs` 元数据 `storage[]`；超时结转残留 `running` 任务为 `failed` 并复用 `overview._keep_usage` 写 `usage/<host_id>.jsonl`、产 `ephemeral_down:`（`to="both"`）；同项目多在线实例同时 `running` 同 `job_id` 返回 `warnings` 并产 `ephemeral_dup:`（`to="agent"`）；云盘剩余 `< 1 GiB` 标红并产 `ephemeral_disk_low:`（`to="agent"`）；无缝接入 `overview.occupancy`、`collect._segment_eta`、`waits.JobWatch` 与 `labmon status`/`disk` 独立分区
   实现：`ephemeral.py`、`labmon_ephemeral.py`、`serve.py`、`collect.py`、`overview.py`、`projects.py`、`users.py`、`api.py`、`waits.py`、前端 `common.js`/`overview_js/`/`page.html`
181. **GPU 主机的 GPU 保底 CPU 核、纯 CPU 自动封顶与降 nice、GPU 有限弹性放行**：带 GPU 且设了 CPU 配额的主机经 `snap_ngpus`（即使本轮全纯 CPU 请求未查显存使 `S["cards"]` 为空，快照 `S["ngpus"]` 仍保留主机实际 GPU 卡数）与 `gpu_cpu_reserve` 为 GPU 任务预留 `min(max(2, len(cards) * 2), cpu_cap // 4)` 核保底（无 GPU 机与无配额机为 0）；纯 CPU 任务（`ngpu == 0`）经 `cpu_limit` / `cpu_give` / `cpu_need` 自动封顶到 `cpu_cap - reserve`（提示值超过 `cpu_cap` 仍报 `nofit_cpu`）并导出封顶后的 `LABMON_CPUS`，经 `cpu_free_for` 仅扣尚未被在跑 GPU 任务填满的保底核（不浪费余核），经 `cpu_only_nice` 在启动时自动降优先级 `nice +10`（清单优先 `nice -n 10` 缺失回退 `os.setpriority`，单元 `os.nice`，异常静默）；纯 CPU 实测超核挤占保底区把 `cpu_free` 挤满且 GPU 空闲时，经 `gpu_cpu_flex` 按启动后实际分配记账核数（`cpu_give`）校验并允许非占位、非整机独占的低核 GPU 任务在保底核范围内（GPU 总记账 `<= min(reserve, cpu_room)` 且仅放空闲卡）有限弹性放行并在事件与日志写明原因（`#225`）
   实现：worker 的 `job_ngpu`、`snap_ngpus`、`gpu_cpu_reserve`、`cpu_limit`、`cpu_room_for`、`cpu_free_for`、`gpu_cpu_flex`、`cpu_only_nice`（`GPU_CPU_RESERVE_PER_CARD`、`GPU_CPU_RESERVE_DIV`、`CPU_ONLY_NICE`；真放行 `place`、推演 `simulate`/`forecast`、`fit`、`offers`、`cpu_give`、`cpus_export`、`acct_job`、`cpu_fit`、`cpu_line`、`Worker.start`/`grant` 与 `slot_main` 共用）
