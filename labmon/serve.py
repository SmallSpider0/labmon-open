"""本地看板服务：一个进程里定时采集（每个项目一个线程），并用 HTTP 提供页面。

    labmon serve [--port 8765] [--public-port P]

- 项目从登记加载（<状态根>/projects/<id>/，projects.py，#161）：labmon project push 登记、archive 收起；运行中每轮全局采集前对齐（Server.sync），
  经接口推上来的当场加上。以前 launchd plist 里的 -c 只在升级那次迁移用（projects.adopt）。

- 两个入口（#160），都只绑 127.0.0.1：内网入口（--port，Server，持有项目、采集线程与组内服务器这些状态）；
  给了 --public-port 另起公网入口（Listener，给隧道接，共用内网入口的状态）。平台操作按账号许可，部署等纯内网路由用 ROUTES 的 lan，公网入口回 404。
- 一律要登录（users.py）：浏览器用账号口令换会话（Cookie labmon_s），命令行与脚本用令牌（Authorization: Bearer）；没有“来自本机就放行”。
  鉴权只经 Handler._allow：按路由表要的作用域、是否只给管理员调 users.check。没登录的页面 302 到 login?next=…，JSON 回 401。
- 普通用户（本单元只读）看不到组内服务器：/fleet/* 一律 403；/、/overview.json、/records.json 去掉组内相关的部分（users.view、users.visible）。
- 页面仍照常写到各项目输出目录（projects.out_dir）的 index.html，file:// 打开作为后备（只读）；看板服务发页面时把 ME（users.me）填进去。
- 配置文件改动后，下一轮采集自动重读（不用重启服务）。
- 防跨站（Handler._gate，#158）：Host 只认 127.0.0.1、localhost 加这个入口的端口，不认的 403；
  POST、PUT 都要 Content-Type: application/json（没请求体的也带，写 {}），否则 415；带了 Sec-Fetch-Site 的只认 same-origin、none，否则 403。

账号（JSON 进出，都过 _gate；详见 docs/api.md）：
    GET  /login             登录页（不要登录；已登录的直接回 next）
    GET  /healthz           探活（不要登录，#165）：只回 {ok, primary, last_collect, maint_until}，给 TX 的看门狗
    POST /api/login {"name", "password"}、/api/setup {"name", "code", "password"}（用设置码设口令）、/api/logout：成功种、清会话 Cookie
    GET  /api/me            我是谁：名字、角色、作用域、入口、能不能做平台操作（ops）与写操作（write）；浏览器会话另有自己的会话与令牌
    POST /api/password {"old", "new"}、/api/tokens/add {"name", "scopes"}（回原文，只这一次）、/api/revoke {"id"}（自己的会话或令牌）：只认浏览器会话
    POST /api/pool/lend {"id", "on", "hours", "ok"}  入池开关（#24，pool.set_lend）：这台组内服务器的持有人本人；管理员代开要能做平台操作、ok 为真；回 {"msg", "servers"}
    POST /api/pool/reclaim {"id"}  收回（#25，pool.reclaim）：按停法停下池任务、清单回池里重排；持有人本人，或能做平台操作的管理员；回 {"msg", "servers"}
    GET  /api/users、POST /api/users/add {"name", "person", "admin"}（person 是已登记的组内人员；回设置码）、role {"name", "admin"}、
                            reset {"name"}（回设置码；这人的会话与令牌一并失效，#18）、revoke {"name", "id"?}、remove {"name"}：管理员，只在内网入口（#17）

部署（#164，deploy.py；管理员的 deploy 作用域）：
    GET  /api/admin/deploy  在跑的提交（commit）、checkout 现在的（head）、启动时刻、正在做的平台操作（busy）、这次启动以来日志里的 Traceback
    POST /api/admin/deploy {"bundle": git bundle 的 base64}  只在内网入口：快进 checkout 的 main（不是快进、平台操作在做时 409），改了代码的等平台操作做完后重启
    POST /api/worker/start {"host"}  只在内网入口：部署后拉起这台的 worker（workerctl.run_out），回 {"rc", "out"}
    GET  /api/me 另带 commit、started：在跑的代码是哪个提交、何时启动

项目配置（#161，projects.py；docs/api.md）：
    GET  /api/project/<id>/config   看板上的配置 {id, ver, text, files, archived}，头里 ETag: "<ver>"（read）；没登记 404
    PUT  /api/project/<id>/config {"text", "files": {done_when 的路径: 内容或 null}}，头 If-Match: "<上次同步的版本>"（project）：
                            第一次推就是登记（不用 If-Match）；已登记的没带 428、对不上 412（回 {"error", "ver"}）、已归档 409、校验不过 400；
                            改了 [[hosts]]、[[queues]]（projects.GUARDED）要平台操作的权限（admin 作用域、入口获准），不然 403；成功回 {id, ver, created, changed, msg}
    GET  /api/project/<id>/status|disk|records[?host=&cat=&q=&limit=]   实验会话的只读命令（#52，api.status、api.disk、api.records；read，普通用户只看自己的项目）：
                            回 {rc, out, err, data}，命令行打 out，--json 打 data（status 是项目快照原样、disk 是 disk.project_storage、records 是记录行）；没登记 404

路由：
    /                       全局调度页：各主机现读的容量与各任务用量（读 worker 的状态，只读），并列出全部项目
    /overview.json          全局页的数据，页面每 15 秒取一次
    POST /collect[?fresh]   立即重读一次各主机（project 或 deploy 作用域），返回全局快照；离上一轮不到 MIN_GAP 秒时不采，返回现有快照并带 "cached": true；
                            带 fresh（页面刚改了配置或状态）不受 MIN_GAP 限制，且等请求之后才开始的那一轮
    /p/<id>/                看板（id 是登记的项目 id）
    /p/<id>/snapshot.json   快照，页面每 15 秒取一次、原地更新
    /p/<id>/events.json     项目所用主机 30 天内的事件（时间线解释空档）与本项目的存储用量
    /records.json?cat=&host=&who=&q=&before=&limit=&cats=   记录（overview.records）：新的在前，before 往前翻页；返回 {"rows", "more", "cats"}
                                                       （带 cats 时 cats 是不分类、同样筛选下各分类的条数，记录页的选项卡用）
    POST /p/<id>/collect    立即采集一次，返回新快照；离上一轮不到 MIN_GAP 秒时不采，返回现有快照并带 "cached": true
    POST /renew/<主机>/<动作>  预备续租（见 renew.py；管理员、内网入口）：reserve、cancel（取消预备、恢复放行）。
                            续租本身与组内服务器一样走 POST /fleet/op/renew（ids 里放自用主机），「继续」走 ka
    /fleet/<id>.json        组内服务器一台（/fleet/* 只给管理员）近 7 天的探测曲线（打开这台的详情时取）
    /fleet/person/<名字>.json  人员详情：近 30 天逐日各状态的小时数、实验时利用率，按服务器的统计（打开这人的详情时取）
    POST /fleet/probe/<id>  立即探一次这一台（管理员、内网入口），返回新的组内服务器数据
    /fleet/op.json          组内服务器的平台操作：开机参数、上次核对结果、当前操作进度、人与池（操作时页面每 2 秒取一次）
    /fleet/plan.json?kind=&sid=&person=&pool=&spec= 开机/换人/换节点向导的预览：要提交的参数、挑出的端口、规格与本地能核对的各项（不连平台）
    POST /fleet/op/<动作>   平台操作（见 fleetops.py；管理员、入口获准）：captcha（取一张验证码，返回 {"code", "tok"}；要验证码的请求把输入的
                            code 与这张的 tok 一起带回，两个页面先后取过的不拿先取的那张去登录）、sync {"code", "tok"}、renew {"ids", "ack", "code", "tok"}、
                            ka {"ids"}（开保活，不碰平台）、off {"ids", "ack", "code", "tok"}（关机，可选自用主机）、
                            swap {"ids", "person", "ack", "code", "tok"}（换人，可选自用主机，也可换给自己）、
                            open {"ids"（已关机的服务器，可省）, "pool", "person", "code", "tok"}（开机、开新的）、
                            person {"name", "image", "run_cmd", "note", "remove"}（存人的设置；镜像与启动命令都不给就是加入，不碰平台）、
                            defaults {"image", "run_cmd"}（新的人默认）、guard {"id", "on"} 或 {"idle_min", "watch_min"}（保活守护的开关与时间）、
                            disk {"dir", "owner"}（共享盘一级目录归给谁，owner 为空回到缺省）或 {"name", "limit"}（某人的提醒线，GiB；空去掉）、
                            keepalive {"id", "pid"}（把这台上这组负载认作保活：脚本写进主机清单，随后再探一次）、
                            borrow {"id", "until"（epoch 秒，可省）, "for"（项目列表，可省）, "ok"（已和对方说好）, "light"（对方在轻用也借）, "away"（对方说好约定时间前不用）, "from"}（借来用在后台跑、回操作进度，改约定直接回 {"msg"}，见 borrow.py；
                            id 是换给自己的自用主机时只记约定时间）、borrow_return {"id", "now", "cancel"}（要还：跑完就还，now 是正在跑的按停法停下；cancel 是撤销跑完就还）、
                            dismiss、logout
    POST /sched/<动作>      调度卡（project 作用域；改权重只给管理员）：weight {"project", "weight"（null 回到 1）}（写主机清单的 [sched.weight]）、
                            focus / pause {"project", "until"（epoch 秒，可省）}、unfocus / resume {"project"}（写主机清单的 [sched.focus] / [sched.pause]，
                            所有主机生效，同不带 --host 的 labmon focus / pause；暂停、恢复给那个项目的实验会话一条消息 records.tell）。成功回 {"msg"}；失败回 409 {"error"}
    POST /project/<动作>    项目的归档与恢复（project 作用域，见 projects.py）：archive {"project"}（要求暂时没实验；移除登记、撤权重与指令、停止采集）、
                            restore {"project"}（加回采集，登记不恢复；告诉那个项目的实验会话）、
                            delete {"project", "revision"}（管理员，永久删除已确认的归档项目与看板缓存）、
                            config {"project", "set": {…}}（改项目的 monitor.toml 里页面能改的那几项，config.set_project；记一条配置改动、告诉实验会话，#122）。
                            成功回 {"msg"}；不能做、没做完、校验不过回 409 {"error"}
    POST /reg/<动作>        队列登记（project 作用域，#95，#121 按主机成批）：move {"to", "items": [{"host", "root", "jobs", "project", "stop"}]}（逐条在 to 上 worker add：
                            别的主机上同一条清单的登记搬过来；--gpus 不带）、rm {"host", "home", "project", "items": [{"root", "jobs"}]}（逐条 worker rm，已不在用的主机经在用主机代办；
                            有一条在这台上有任务在跑就整批拒绝）。成功回 {"msg"}；失败回 409 {"error"}（做了几条后失败的，error 里带已做的）。
                            做成的按项目给实验会话一条消息（records.tell → 全局快照的 wake）
    POST /host/assign       给项目用（project 作用域，#173）：{"host", "project"} 通知该项目的实验会话可以使用这台自用主机（records.tell(project, "host", …)），
                            不改登记、不替会话挑清单；仅主服务可写，权限按 users.need_host + need_project。成功回 {"msg"}；失败回 400 / 403 / 409 {"error"}
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import ssl
import sys
import threading
import time
import traceback
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import backup as BK
from . import borrow as B
from . import collect as C
from . import deploy as DP
from . import disk as DK
from . import fleet as F
from . import fleetboot as FB
from . import fleetops as FO
from . import fleetview as FV
from . import hostcheck as HC
from . import lan as L
from . import lease as LS
from . import maint as MT
from . import overview as O
from . import platform as PF
from . import pool as PO
from . import projects as PJ
from . import rebalance as RB
from . import renew as R
from . import users as U
from . import waits as WT
from . import workerctl as W
from .worker import queue_stop_path
from . import api as A
from . import client as CL
from .config import CLIENT_HEADER, server as server_conf
from . import dist as DI
from . import service as S
from .config import ALERT_KEYS, HOSTS_LOCK, POOL, ConfigError, active_hosts, cfg_spec, check_future, host_pair, host_users, hosts_file, sched_ovr_text, set_host_users, set_sched_ovr, set_weight, sources, \
    stale_limit, user_hosts, STALE_PAD_S, STALE_RUNS
from .records import ReportQuota, cat_of, log_events, project_mine, records, report, report_headers, schedule_project, tell
from .service import log, notify, rotate_log
from .state import file_sig, load_json, snapshot, state_dir

OFFLINE = "本机不在内网：平台与各服务器都连不上，回到内网再操作"
MIN_GAP = 10                   # 两次“立即采集”至少相隔这么多秒，更频繁的请求直接返回现有快照（页面改了配置后的那次除外）
PUSH_AGAIN_S = 6 * 3600        # 推给项目主人的同一条告警，消失这么久以内又出现的不重推（#167）


_FAIL: dict[str, str] = {}


def trouble(who: str, err: str | None) -> None:
    """采集出错与恢复：traceback 每轮照旧进 labmon.log，记录里只在状态变了时记一条（err 取最后一行）。"""
    was = _FAIL.pop(who, None)
    if err:
        _FAIL[who] = err
        if not was:
            log_events([{"kind": "collect", "msg": f"{who}采集出错：{err.strip().splitlines()[-1][:200]}"}])
    elif was:
        log_events([{"kind": "collect", "msg": f"{who}采集恢复"}])


class _Collector:
    """定时采集的线程骨架：每轮 once()，轮与轮之间等 interval 秒；collect_now 叫醒它并等这一轮做完。"""
    TIMEOUT = 120

    def __init__(self):
        self.wake = threading.Event()
        self.cond = threading.Condition()
        self.started = 0             # 已开始的采集轮数
        self.runs = 0                # 已完成的采集轮数
        self.last_end = 0.0
        self.stop = False            # 项目归档了：这一轮做完就退出线程（Server.sync）
        self.alive = False           # 采集线程起了、还没退出（start 置上，loop 退出时在 cond 里清掉）
        self.round = threading.Lock()  # 一轮采集在做时持着：停服务时等这一轮做完再释放服务租约（halt）

    def once(self) -> None:
        raise NotImplementedError

    def halt(self, timeout: float) -> bool:
        """不再开始新的一轮，等正在做的这一轮做完（最多 timeout 秒）：停服务时先这样，再释放服务租约，免得刚释放又被这一轮续上（#163）。"""
        with self.cond:
            self.stop = True
        return self.round.acquire(timeout=timeout)

    def start(self, name: str) -> None:
        self.alive = True
        threading.Thread(target=self.loop, name=name, daemon=True).start()

    def resume(self) -> bool:
        """撤销 stop（归档后又恢复）：线程还没退出就接着用它采、返回 True；已退出返回 False，由调用方另起。
        同一个输出目录不能有两个采集线程：collect.build 读改写 state.json、history.jsonl，旧的一轮晚做完会覆盖新的。"""
        with self.cond:
            self.stop = False
            return self.alive

    def loop(self) -> None:
        while True:
            with self.cond:
                if self.stop:
                    self.alive = False
                    return
                self.started += 1
            with self.round:
                self.once()
            with self.cond:
                self.runs += 1
                self.last_end = time.time()
                self.cond.notify_all()
            self.wake.wait(self.interval)
            self.wake.clear()

    def collect_now(self, timeout: float | None = None, fresh: bool = False) -> str | None:
        """请求立即采集并等它完成：返回 "done"（这一轮做完了）、"cached"（离上一轮不到 MIN_GAP 秒，没采，拿现有快照）或 None（超时）。
        fresh：页面刚改了配置或状态，不受 MIN_GAP 限制，且要等请求之后才开始的那一轮（正在进行的那轮读的是改之前的）。"""
        with self.cond:
            if not fresh and time.time() - self.last_end < MIN_GAP:
                return "cached"
            n = self.started if fresh else self.runs           # 要等到完成的轮数超过它
            self.wake.set()
            return "done" if self.cond.wait_for(lambda: self.runs > n, timeout or self.TIMEOUT * (1 + (n > self.runs))) else None


class Project(_Collector):
    """一个登记了的项目（projects.py）：自己的采集线程，配置与输出目录都在登记目录里。"""
    TIMEOUT = 150

    def __init__(self, pid: str, interval: int | None = None):
        super().__init__()
        self.id = pid
        self.cfg_path = str(PJ.cfg_file(pid))
        self.cfg = PJ.load_cfg(pid)
        self.mtime = self._mtimes()
        self.out = PJ.out_dir(pid)
        self.out.mkdir(parents=True, exist_ok=True)
        self.interval_arg = interval
        self.alerts: list[str] = []
        self.pushed: dict[str, float] | None = None      # 推给项目主人的告警：key → 最后一次看到（None：重启后第一轮，只记不推）

    @property
    def interval(self) -> int:
        return self.interval_arg or self.cfg["interval"]

    def _mtimes(self) -> tuple:
        # 项目配置与用户级主机清单任一改动都重读；主机清单是后来才建的也算
        return tuple(file_sig(p) for p in sources(self.cfg)) + (hosts_file().exists(),)

    def _reload(self) -> None:
        try:
            m = self._mtimes()
            if m != self.mtime:
                cfg = PJ.load_cfg(self.id)
                if cfg["id"] != self.id:                         # 推、页面都改不了 id（projects.check_config）；有人直接改了登记里的文件才会这样
                    raise ConfigError(f"登记目录里的配置 id 改成了 {cfg['id']}")
                self.cfg, self.mtime = cfg, m
                log(f"{self.id}：配置已改，重新读取")
        except Exception as e:  # noqa: BLE001 — 配置里的任何错（写错类型、缺字段）都只记日志、沿用旧配置，项目线程不退出
            log(f"{self.id}：配置读取失败，沿用旧配置：{e!r}")

    def once(self) -> None:
        if not LS.primary():                         # 备用（服务租约在别的看板服务手里，#163）：不采集，也不记“采集失败”（collect.collect 自己也挡）
            return
        self._reload()
        try:
            snap = C.collect(self.cfg, self.out)
            trouble(f"项目 {self.id} ", None)
            al = [a["msg"] for a in snap["alerts"]]
            if [RB.log_text(x) for x in al] != [RB.log_text(x) for x in self.alerts]:   # 告警只在变了时记一行（挪法建议句尾的推演时刻不算变）
                log(f"{self.id}：告警 {len(al)} 条" + ("：" + "；".join(al) if al else "，都已消失"))
                self.alerts = al
            self.push_owner(snap["alerts"])
        except Exception:  # noqa: BLE001 — 服务不因一次采集出错退出
            err = traceback.format_exc()
            trouble(f"项目 {self.id} ", err)
            log(f"{self.id}：采集出错\n{err}")

    def push_owner(self, alerts: list[dict], t: float | None = None) -> list[str]:
        """普通用户的项目（#167）：新出现的给用户的告警（collect.user_alerts，连不上主机的不算：掉线另推给能用那台的人）推给项目主人。
        同一条（按 key）一直在、或消失不到 PUSH_AGAIN_S 又出现的不重推；服务重启后的第一轮只记不推。返回推了的文字（测试看）。
        管理员的项目不推（管理员看工作台）。"""
        t = time.time() if t is None else t
        cur = [(k, m) for k, m in C.user_alerts(alerts, keys=True) if not k.startswith("ssh_fail:")]
        first, seen = self.pushed is None, {k: v for k, v in (self.pushed or {}).items() if v > t - PUSH_AGAIN_S}
        new = [m for k, m in cur if k not in seen]
        self.pushed = seen | {k: t for k, _ in cur}
        a = U.account(U.owner_of(self.id))
        if first or not new or not a or a["role"] == "admin":
            return []
        notify([f"{self.cfg.get('title') or self.id}：{m}" for m in new], to=[a["name"]])
        return new

    def snapshot(self) -> dict | None:
        return load_json(self.out / "snapshot.json", None)


class Overview(_Collector):
    """全局调度页：一个线程定时读各在用主机的 worker 状态。"""

    def __init__(self, projects: list[Project], interval: int = 60):
        super().__init__()
        self.projects = projects
        self.interval = interval
        self.sync = None             # 每轮采集前按 projects.json 对齐项目（Server.sync，摘下归档的、加回恢复的）
        self.regs_seen: dict | None = None   # 上一轮各项目的登记在哪（collect.reg_sig）：变了的项目当场叫醒（wake_moved）

    def proj_info(self) -> list[dict]:
        """各项目的摘要：给 overview.collect，除 running 外原样进全局快照的 projects（页面要的字段只在这里定，#139）。"""
        out, glob = [], snapshot()
        for p in self.projects:
            s = p.snapshot() or {}
            out.append({"id": p.id, "title": s.get("title") or p.cfg.get("title") or p.id,
                        "owner": U.owner_of(p.id),       # 项目主人（#167；为空、账号已删的归管理员）：「管理」抽屉的「主人」一行
                        # 给用户的告警（C.user_alerts：只给实验会话的不算，#142）；工作台总览列出、算进告警数，连不上工作台自己报，不重复
                        "alerts": len(C.user_alerts(s.get("alerts", []))),
                        "alert_msgs": C.user_alerts([x for x in s.get("alerts", []) if not x["key"].startswith("ssh_fail:")])[:8],
                        "generated": s.get("generated"),
                        "no_work_since": s.get("no_work_since"),
                        # 运行中任务的进度与预计结束（项目看板按日志算好的），工作台按任务名对上
                        "running": [{k: j.get(k) for k in ("id", "progress", "eta_ts", "eta_source", "rate_per_min", "dur_min")}
                                    for j in s.get("jobs", []) if j.get("status") == "running"],
                        # 车道名（项目看板定的）与里程碑：工作台的时间线按主机、目录、清单认出 worker 推演里的任务属于哪条车道
                        # 项目「管理」抽屉按 主机 + root + 清单 对上登记（全局快照的 regs），另取未完成几个（unfinished）、在跑几个（run）、主机已归还（returned）；
                        # 项目探针没读到的车道（collect._lane 的 missing 占位）两个数写空值：说不出，页面退回 worker 状态，不当成空着
                        "lanes": [{**{k: ln.get(k) for k in ("host", "root", "queue", "label", "returned")},
                                   **({"unfinished": None, "run": None} if ln.get("missing") else
                                      {"unfinished": ln.get("unfinished"),
                                       "run": sum(1 for j in s.get("jobs", []) if j.get("lane") == ln.get("id") and j.get("status") == "running")})}
                                  for ln in s.get("lanes", []) if ln.get("root")],
                        "markers": [{"label": m.get("label"), "ts": m.get("ts")} for m in s.get("markers", []) if m.get("ts")],
                        # 计划项（写明的与自动归出来的）各有几个任务、跑完几个，没归进任何组的几个（占位任务不算）：「管理」抽屉看归组对不对（#133）
                        "groups": [{k: g.get(k) for k in ("id", "label", "color", "auto", "total", "done", "failed", "running", "queued")} for g in s.get("groups", [])],
                        "ungrouped": sum(1 for j in s.get("jobs", []) if not j.get("hold") and not j.get("group")),
                        # 项目页收起的只在登记里有的车道（跑完/休眠/已归还/已摘登记/空清单，collect._quiet）：管理抽屉的逐条清单不列（#121、#164）
                        "quiet": s.get("quiet") or [],
                        "cfg": self.cfg_info(p),
                        # 能不能归档（null 能，否则一句原因）：项目「管理」抽屉的「归档」节照它置灰与写原因（#97）
                        "archive_block": PJ.archive_block(p.id, s, glob, p.interval)})
        return out

    @staticmethod
    def cfg_info(p: Project) -> dict:
        """项目配置的摘要（项目「管理」抽屉的「项目配置」节）：看板上那份配置的路径（登记目录里，#161）、输出目录、标题、采集与刷新间隔、车道收起时长、计划项、里程碑、告警阈值。
        页面上能改的那几项经 POST /project/config 写回看板上的 monitor.toml（#122，projects.set_config）；推上来的下一轮自动重读（Project._reload），实验会话的 labmon wait 拉回项目目录。
        interval_fixed：服务启动参数定了采集间隔，monitor.toml 里的不生效。spec：能改的项的说法、单位、范围、缺省（config.cfg_spec，页面按它画档位、当场校验，#133）。
        stale：数据过期的实际门槛（config.stale_limit）：s 按已保存的配置算好的秒数，runs、pad 是它的系数（草稿改了采集间隔或这项时页面拿它们当场套），iv 是它用的采集间隔（monitor.toml 的，#141）。"""
        c = p.cfg
        return {"path": p.cfg_path, "out_dir": str(p.out), "title": c.get("title"), "interval": p.interval, "interval_fixed": bool(p.interval_arg), "spec": cfg_spec(),
                "refresh": c.get("refresh"), "hide_done_hours": c.get("hide_done_hours"),
                "groups": [{"id": g.get("id"), "label": g.get("label"), "color": g.get("color")} for g in c.get("groups", [])], "auto_groups": c.get("auto_groups"),
                "markers": [{"date": m.get("date"), "label": m.get("label"), "ts": m.get("ts")} for m in c.get("markers", [])],   # ts：load 解析好的时刻，页面换成日期框的值（#136）
                "alerts": {k: (c.get("alerts") or {}).get(k) for k in ALERT_KEYS},
                "stale": {"s": stale_limit(c["interval"], c["alerts"]["stale_min"]), "runs": STALE_RUNS, "pad": STALE_PAD_S, "iv": c["interval"]}}

    def once(self) -> None:
        try:
            if self.sync:
                self.sync()
            if not LS.primary():                # 备用（#163）：只读一次租约、能拿就拿；拿到了接着采这一轮
                O.reset_sent()
                if LS.tick() != "primary":
                    trouble("工作台", None)
                    return
            snap = O.collect(self.proj_info(), pool_tick=lambda snap, sw: PO.tick(snap, sw, move=reg_move))   # 算力池的还、放、借（#25）
            if snap is None:                    # 这一轮读出租约在别人手里：已转为备用，什么都不做
                trouble("工作台", None)
                return
            self.wake_moved(snap)
            notify(snap["notices"])             # 掉线、恢复、租期将到：弹系统通知
            push_host_users(snap.get("host_notices") or {})
            for msg in snap["notices"] + O.relaunch(snap):   # 容器重启后自动拉起 worker（主机清单 worker = "auto"）
                log("全局页：" + msg)
            trouble("工作台", None)
        except Exception:  # noqa: BLE001 — 服务不因一次采集出错退出
            trouble("工作台", traceback.format_exc())
            log(f"全局页：采集出错\n{traceback.format_exc()}")


    def wake_moved(self, snap: dict) -> list[str]:
        """登记变了的项目（collect.reg_sig 比上一轮：搬、登记、撤，算力池放到某台，#78）当场叫醒它的采集线程：车道按全局快照的 regs 拼，
        不叫醒要再等一个项目采集间隔（缺省 2 分钟）才跟过去，这期间会话已收到「放到某台」、项目页与 wait --job 还停在原处。
        服务起来后的第一轮只记不叫（各项目线程刚起，本来就在采）。返回叫醒了哪些（测试看）。"""
        sig, seen = C.reg_sig(snap.get("regs")), self.regs_seen
        self.regs_seen = sig
        if seen is None:
            return []
        woke = [p.id for p in self.projects if sig.get(p.id) != seen.get(p.id)]
        for p in self.projects:
            if p.id in woke:
                p.wake.set()
        return woke


def push_host_users(per_host: dict[str, list[str]]) -> None:
    """一台主机的掉线、恢复（overview.health 的 per_host）另推给主机清单里这台 users 列着的人（#167；管理员照旧经 notice 收到）。"""
    hs = {str(h.get("id")): h for h in user_hosts()}
    for hid, msgs in per_host.items():
        who = [n for n in host_users(hs.get(hid)) if (U.account(n) or {}).get("role") == "user"]
        if who and msgs:
            notify(msgs, to=who)


class Fleet:
    """组内服务器：一个线程每分钟最多只读探一台（见 fleet.py），与全局页的采集分开，免得 ssh 慢拖住全局页。"""

    def __init__(self):
        self.lock = threading.Lock()

    def loop(self) -> None:
        while True:
            try:
                self.tick()
                trouble("组内服务器", None)
            except Exception:  # noqa: BLE001 — 服务不因一次探测出错退出
                trouble("组内服务器", traceback.format_exc())
                log(f"组内服务器：探测出错\n{traceback.format_exc()}")
            time.sleep(F.TICK)

    def tick(self, force: str | None = None) -> None:
        with self.lock:                              # 备用（#163）时 fleet.tick 什么都不做
            msgs = F.tick(force=force, busy=FO.busy())
        notify(msgs)                                # 只有掉线、恢复、租期将到弹通知
        for m in msgs:
            log("组内服务器：" + m)


def proj_events(proj: Project, days: int = 30) -> dict:
    """项目页时间线要的主机事件（30 天）、同机别的项目的占用（occupancy，已跑的、在跑的与推演的排队）与配色（本项目的存储在项目快照的 storage，#53）：
    主机按项目快照里的主机（monitor.toml 写的加上登记所在的，collect.lanes_cfg）。同机别的项目的事件（它的任务作废、被挤掉，对它的暂停、优先）不给：会画进本项目的事件带、被当成本项目空档的原因；
    它们占卡的情况画在“同机别的项目”一行。本项目的项目级暂停、优先（所有主机的，记录里没有 host）并进每台主机的事件，暂停区间照画（#96）。"""
    since = time.time() - days * 86400
    hosts = [h["id"] for h in (proj.snapshot() or {}).get("hosts", [])]
    ov = snapshot()
    occ = O.occupancy([h for h in ov.get("hosts", []) if h["id"] in hosts], ov.get("projects", []), since, skip=proj.id)
    glob = [{**e, "glob": True} for e in records(cat="queue", since=since)          # glob：时间线与单台的同种指令分开配对增撤（Codex P2）
            if schedule_project(e) == proj.id and not e.get("host") and not e.get("hosts")]
    return {"events": {h: sorted([e for e in records(host=h, since=since)[::-1] if project_mine(e, proj.id)] + glob[::-1], key=lambda e: e["t"]) for h in hosts},
            "occ": occ, "colors": ov.get("colors") or {}}


class Server(ThreadingHTTPServer):
    """内网入口（#160）：持有项目、采集线程与组内服务器这些状态（board 是自己），平台操作由账号和入口许可控制。
    conf：主机清单的 [server]（config.server，#165）：配了证书就只说 HTTPS（tls_wrap），lan_hosts 是另认的 Host；绑哪由调用方按 lan_bind 给 addr。"""
    daemon_threads = True
    entry = "lan"

    def __init__(self, addr, projects: list[Project] = (), interval: int | None = None, conf: dict | None = None):
        super().__init__(addr, Handler)
        conf = conf or {}
        self.board = self
        self.min_client_commit = conf.get("min_client_commit")
        self.secure = tls_wrap(self, conf.get("tls_cert"), conf.get("tls_key"))    # 有 TLS：会话 Cookie 加 Secure，Host 不带端口的按 443
        self.dport = 443 if self.secure else 80
        port, bind = self.server_address[1], conf.get("lan_bind") or "127.0.0.1"
        own = [] if bind == "0.0.0.0" else [f"{bind}:{port}"]           # 绑的那个地址本身（config.lan_url 经它连）
        self.hosts = allowed_hosts(port, [*(conf.get("lan_hosts") or []), *own], self.dport)   # Host 头认哪些（Handler._gate）
        self.projects = {p.id: p for p in projects}
        self.overview = Overview(list(projects))
        self.overview.sync = self.sync
        self.fleet = Fleet()
        self.held: set[str] = set()          # 登记了、因归档没在采集的项目（启动时跳过的、运行中摘下的）；恢复后 sync 加回
        self.leaving: dict[str, Project] = {}   # 运行中摘下的：id → 那个 Project（它的线程也许还在做最后一轮；恢复时先看它退出没有）
        self.bad: dict[str, float] = {}      # 配置加载不了的项目：id → 当时配置文件的修改时刻（文件改了再试，没改不重复报）
        self.interval = interval             # serve --interval（加上的项目同样按它）
        self.lock = threading.Lock()

    def sync(self) -> list[str]:
        """按登记与 projects.json 对齐看板服务里的项目（#97、#161）：启动时调一次加载全部，之后每轮全局采集前调（命令行推的、恢复的最迟一轮加上），
        页面上点的、经接口推上来的当场调。已归档的停掉采集线程、从项目表与工作台的列表拿掉（记在 held）；登记了、没归档、还没在采集的建 Project、起线程
        （摘下时那一轮没做完的接着用原来的线程）。配置加载不了的只跳过它、记一条，配置文件改了再试。返回做了什么（也记进服务日志）。"""
        with self.lock:
            gone, msgs = PJ.archived(), []
            for p in [p for p in self.projects.values() if p.id in gone]:
                p.stop = True
                p.wake.set()
                self.held.add(p.id)
                self.leaving[p.id] = p
                self.projects = {k: v for k, v in self.projects.items() if k != p.id}
                msgs.append(f"{p.id}：已归档，停止采集")
            for pid in PJ.registered():
                if pid in self.projects:
                    continue
                if pid in gone:
                    if pid not in self.held:
                        self.held.add(pid)
                        msgs.append(f"项目 {pid} 已归档，不采集（labmon project restore {pid} 加回）")
                    continue
                back = pid in self.held
                self.held.discard(pid)
                old = self.leaving.pop(pid, None)
                if old and old.resume():                # 摘下时那一轮还没做完：接着用原来的线程，不另起（免得两个线程写同一个输出目录）
                    self.projects = {**self.projects, pid: old}
                    msgs.append(f"{pid}：已恢复，接着采集")
                    continue
                m = file_sig(PJ.cfg_file(pid))
                if self.bad.get(pid) == m:
                    continue
                try:
                    p = Project(pid, interval=self.interval)
                except Exception as e:  # noqa: BLE001 — 配置里写错类型、缺字段这类不是 ConfigError 的错同样只跳过这个项目，别的项目照常采集
                    self.bad[pid] = m
                    msg = f"项目 {pid} 的配置没加载，这个项目不采集：{e!r}"
                    msgs.append(msg)
                    log_events([{"kind": "collect", "msg": msg}])
                    continue
                self.bad.pop(pid, None)
                self.projects = {**self.projects, pid: p}
                p.start(f"collect-{pid}")
                msgs.append(f"{pid}：已恢复，接着采集" if back else f"{pid}：开始采集")
            if msgs:
                self.overview.projects = list(self.projects.values())
            for m in msgs:
                log(m)
            return msgs

    def delete_project(self, pid: str, revision: str, who: str) -> str:
        """同 sync 互斥；已经摘下的线程也要等它退出，避免最后一轮重建目录。"""
        def check_collector():
            if pid in self.projects:
                raise ConfigError("项目采集正在停止，请稍后重试")
            if old := self.leaving.get(pid):
                with old.cond:
                    if old.alive:
                        raise ConfigError("项目最后一轮采集正在收尾，请稍后重试")

        with self.lock:
            msg = PJ.delete(pid, revision, who, check_collector)
            self.held.discard(pid)
            self.leaving.pop(pid, None)
            self.bad.pop(pid, None)
        self.overview.wake.set()
        return msg

    def handle_error(self, request, client_address):
        """浏览器关页面、刷新时断开连接不算出错，不往日志里写 traceback；TLS 入口上用 http 打来的、握手失败与超时的同样不写（#165）；别的照常写。"""
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ssl.SSLError, TimeoutError)):
            super().handle_error(request, client_address)


class Listener(ThreadingHTTPServer):
    """公网入口（#160）：只绑 127.0.0.1，给隧道接；状态与内网入口共用（board）。路由表里只在内网入口的（平台操作）在这里回 404，
    会话 Cookie 加 Secure（它只经 HTTPS 反代用）。hosts：另认的 Host（[server] 的 public_hosts，#165：nginx 用 Host $host 传进来的，
    不带端口的按 443）；127.0.0.1、localhost 加这个端口照旧认（TX 上的看门狗经隧道直接探 /healthz）。"""
    daemon_threads = True
    entry = "public"
    secure = True
    dport = 443

    def __init__(self, addr, board: Server, hosts: list[str] | None = None):
        super().__init__(addr, Handler)
        self.board = board
        self.hosts = allowed_hosts(self.server_address[1], hosts, self.dport)

    handle_error = Server.handle_error


class Handler(BaseHTTPRequestHandler):
    server: Server | Listener
    server_version = "labmon"

    @property
    def board(self) -> Server:
        """看板的状态（项目、采集线程、组内服务器）：两个入口共用内网入口那一份。"""
        return self.server.board

    @property
    def entry(self) -> str:
        return self.server.entry

    def setup(self):
        """有 TLS 的入口（#165）：握手在这个处理线程里做、限时 HANDSHAKE_S（tls_wrap 不让 accept 握手：一个不说话的连接会挡住所有请求）。
        握手失败抛出去，Server.handle_error 不记。"""
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(HANDSHAKE_S)
            self.request.do_handshake()
            self.request.settimeout(None)
        super().setup()

    def log_message(self, fmt, *args):  # 只记出错的请求，带上回给页面的原因（请求行里没有口令与令牌：令牌只走 Authorization 头，登录走请求体）
        if args and str(args[1])[:1] in "45":
            log(f"{self.entry} {self.client_address[0]} {fmt % args}" + (f"：{self.why}" if getattr(self, "why", None) else ""))

    def _send(self, code: int, body: bytes | str, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
        if isinstance(body, str):
            body = body.encode()
        self.why = None
        if code >= 400 and ctype == "application/json":
            try:
                self.why = json.loads(body).get("error")
            except (ValueError, AttributeError):
                pass
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")         # 工作台把项目页嵌在 iframe 里；别的网站不能嵌（点击劫持）
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        for c in self.cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj):
        return self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def _gate(self) -> bool:
        """看板服务接受哪些请求（#158）；不接受时回了错、返回 False，请求不进路由。所有请求：
        - Host 只认 127.0.0.1、localhost 加这个入口的端口，与 [server] 里这个入口另认的（allowed_hosts，#165）：DNS 重绑定时浏览器带的是攻击者的域名，挡住它就读不到页面数据。
        POST 另要：
        - Content-Type 是 application/json：别的网站能用表单跨站 POST（text/plain 能拼出 JSON 请求体），而表单发不出 JSON 类型；
          网页用 fetch 带上这个类型跨站发，要先过预检（OPTIONS），本服务不答。页面上的写请求都经 common.js 的 postJSON。
        - 带了 Sec-Fetch-Site 的只认 same-origin 与 none（命令行不带）。不看 Origin：页面带 Referrer-Policy: no-referrer，同源 POST 的 Origin 是 null。
        登录、改口令也是 JSON POST，一样过这道。"""
        self.cookies: list[str] = []         # 这次回话要带的 Set-Cookie（登录、退出、会话续期）
        self._b, self._got = None, False     # 请求体只读一遍（_body）：_own 核对象时先读，路由里再用同一份
        h = self.headers.get("Host") or ""
        if not host_ok(h, self.server.dport, self.server.hosts):
            ok = "、".join(sorted(_host_text(x, self.server.dport) for x in self.server.hosts))
            self._json(403, {"error": f"不认这个 Host：{h or '（没带）'}；看板只认 {ok}（另认的写在主机清单 [server] 的 lan_hosts、public_hosts）"})
            return False
        if self.command not in ("POST", "PUT"):
            return True
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "写请求要带 Content-Type: application/json（防别的网站用表单跨站提交）"})
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "none").lower()
        if site not in ("same-origin", "none"):
            self._json(403, {"error": f"不接受跨站请求（Sec-Fetch-Site: {site}）：只认看板自己页面上的操作与命令行"})
            return False
        return True

    # ---------- 鉴权（#160） ----------
    def _allow(self, path: str) -> U.Ident | None | bool:
        """按路由表 ROUTES 鉴权：返回 Ident（登录了、有权限）、None（不要登录的路由）或 False（已回了错，请求不进路由）。
        只在内网入口的路由在公网入口上回 404（视同没注册）；没登录的页面 302 到登录页，别的回 401；没权限 403。"""
        m = "GET" if self.command == "HEAD" else self.command
        r = route_of(m, path)
        self.guard = _NO_GUARD                           # 这个请求的对象检查（_own 定）；同一连接上的上一个请求留下的不算
        if r.lan and self.entry != "lan":
            self._json(404, {"error": "这项操作要在内网入口做"})
            return False
        if r.scope == OPEN:
            return None
        try:
            who = U.check(self.headers, r.scope, admin=r.admin)
        except U.Denied as x:
            if x.code == 401 and m == "GET" and not path.endswith(".json") and not path.startswith("/api/"):
                self._redirect(login_url(path))
            else:
                self._json(x.code, {"error": x.msg})
            return False
        if r.ops and not U.can_ops(who, self.entry):
            self._json(403, {"error": "此账号尚未开启公网平台操作，请从内网入口设置"})
            return False
        if who.cookie:                                   # 会话续了期：重发 Cookie，浏览器里的过期时刻跟着往后挪
            self.cookies.append(self._cookie(who.cookie))
        # 备用的项目与主机权限可能落后于主看板，先让客户端切换入口。
        needs_primary = (m == "POST" or (m == "PUT" and PROJECT_CONFIG_PATH.fullmatch(path))
                         or (m == "GET" and CLI_READS.fullmatch(path)))
        if needs_primary and self._standby(path):
            return False
        minimum = self.board.min_client_commit
        commit = self.headers.get(CLIENT_HEADER, "")
        recovery = path in ("/api/client/wheel", "/api/me", "/api/users", "/api/reports", "/collect", "/api/worker/start") or path.startswith("/api/admin/")
        # 只拦确认低于最低版本的；比不出先后的（比看板新、分支上跑、冷备回退后：unknown）放行，账号详情与 wait 只提示（#17）
        if minimum and who.via == "token" and commit and not recovery:
            if DI.client_status(commit, DP.RUNNING["commit"], minimum) == "below_minimum":
                self._json(CL.CLIENT_OUTDATED, {"error": f"客户端低于最低版本 {minimum[:7]}：跑 labmon update",
                                               "reason": CL.CLIENT_OUTDATED_REASON, "minimum": minimum, "commit": DP.RUNNING["commit"]})
                return False
        if path == "/api/report":
            try:
                size = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                size = -1
            if size < 0 or size > 160 * 1024:
                self._json(400 if size < 0 else 413, {"error": "报告请求体长度无效或太大"})
                return False
        try:
            ok = self._own(r, path, who)
        except U.Denied as x:
            self._json(x.code, {"error": x.msg})
            return False
        return who if ok else False

    def _own(self, r: Route, path: str, who: U.Ident) -> bool:
        """普通用户按对象核（#167，路由表的 own 列）：项目要是他的（users.need_project）、主机要开给他（need_host）、清单登记要是他项目的（need_reg、need_list）。
        不行抛 users.Denied(403)；请求体不是 JSON 对象时已回 400、返回 False。管理员、读的路由（没写 own）不核；没写 own 的写路由普通用户一律 403。
        对象鉴权只在这里定（#19）：项目的主人、登记新项目的名额（need_room）、暂停是不是他下的（need_pause）当场核一次，同一份记成 self.guard(主机)，
        要在锁里再核的业务函数（推配置、改配置、归档、恢复的 check_owner，调度卡与命令行的暂停）都调它，不各自另写。"""
        if who.role == "admin" or r.own == "self" or (r.own is None and r.method == "GET"):
            return True
        if r.own is None:
            raise U.Denied(403, "这件事只有管理员能做：普通用户只能动自己的项目、用开给自己的主机")
        if r.own in ("path", "new"):
            m = PATH_PID.fullmatch(path)
            pid, new = unquote(m[1]) if m else "", r.own == "new"

            def guard(host=None):
                U.need_project(who, pid, new_ok=new)
                if new and r.method == "PUT":            # 第一次推就登记（#161）：名下项目的名额（#18）
                    U.need_room(who, pid)
            self._guard(guard)
            return True
        body = self._body()
        if body is None:
            return False
        s = lambda k: str(body.get(k) or "")   # noqa: E731
        if r.own == "report":
            pid = _report_text(body, "project", "项目", 128)
            hid = _report_text(body, "host", "主机", 128)
            if pid:
                U.need_project(who, pid)
            if hid and hid not in U.access(who).hosts:
                raise U.Denied(403, "只能报告你可见的主机")
        elif r.own == "body":                          # 调度卡、项目「管理」：暂停与恢复另核是不是他下的（项目级的，#16）
            pid, pause = s("project"), path in ("/sched/pause", "/sched/resume")

            def guard(host=None):
                U.need_project(who, pid)
                if pause:
                    U.need_pause(who, pid)
            self._guard(guard)
        elif r.own == "holder":                        # 入池开关（#24）：这台组内服务器的持有人本人
            PO.need_holder(who, s("id"))
        elif r.own == "init":
            U.need_host(who, s("host"))
        elif r.own == "wait":                          # 等的任务（项目/任务）也只许他自己项目的
            U.need_project(who, s("project"))
            for j in _strings(body, "jobs"):
                U.need_project(who, WT.job_key(j, s("project"))[0])
        elif r.own == "override":                      # 暂停：项目是他的，--host 只许他能用的，暂停与恢复要是他下的（项目级、逐台，users.need_pause，#16、#27）
            pid, pause, hosts = s("target"), path.rsplit("/", 1)[-1] in ("pause", "resume"), _strings(body, "hosts")
            for h in hosts:
                U.need_host(who, h)

            def guard(host=None):
                U.need_project(who, pid)
                if pause:
                    U.need_pause(who, pid, host)
            self._guard(guard, hosts)
        elif r.own == "worker":
            sub = path.rsplit("/", 1)[-1]
            args = _worker_args(sub, body)
            U.need_host(who, s("host"))
            if sub == "add":
                U.need_project(who, s("project"))
                U.need_list(who, *args, s("project"))
            elif sub == "rm":                          # 池（#24）：按清单此刻登记在哪核（池里，或已放到借给池的主机上；api.worker 同样经 pool.locate）
                body["project"] = U.need_reg(who, PO.locate(*args) if s("host") == POOL and len(args) == 2 else s("host"), *args)
            elif body.get("project"):
                U.need_project(who, s("project"))
                if sub == "fit" and body.get("list"):     # fit --list 读这条清单：别的项目名下的清单不给看
                    U.need_list(who, *args, s("project"))
        elif r.own == "host_assign":                   # 给项目用（#173）：主机要开给他，项目要是他的
            hid, pid = _assign_args(body)
            U.need_host(who, hid)
            U.need_project(who, pid)
        elif r.own == "reg":                           # 页面上搬、移除登记：逐条核
            snap = snapshot()
            items = _reg_items(body, move=path.endswith("/move"))
            if path.endswith("/move"):
                U.need_host(who, s("to"))
                for it in items:
                    pid, root, jobs, host = (it[k] for k in ("project", "root", "jobs", "host"))
                    U.need_project(who, pid)
                    U.need_reg(who, host, root, jobs, pid, snap)
                    U.need_list(who, root, jobs, pid, snap)
            else:
                U.need_project(who, s("project"))
                U.need_host(who, s("host"))
                for it in items:
                    U.need_reg(who, s("host"), it["root"], it["jobs"], s("project"), snap)
                body["home"] = None   # 代办目录由这台主机的现行清单决定
        return True

    def _guard(self, fn, hosts=()) -> None:
        """_own 定下的对象检查：当场核一次（给了 hosts 就逐台核，否则按项目级核），记成 self.guard 交给要在锁里再核的业务函数。"""
        for h in hosts or (None,):
            fn(h)
        self.guard = fn

    def _cookie(self, raw: str | None) -> str:
        """会话 Cookie：HttpOnly、SameSite=Strict；经 HTTPS 的入口另加 Secure：公网入口（只经 HTTPS 反代用）、配了证书的内网入口（#165）。
        没 TLS 的内网入口不加：Safari 不在 http://127.0.0.1 上存带 Secure 的 Cookie，加了就登录不上。Path 写 /：反代加了前缀时由 nginx 的
        proxy_cookie_path 改成前缀（服务不用知道前缀）。raw 为 None 是清掉。"""
        return (f"{U.COOKIE}={raw or ''}; Path=/; Max-Age={U.SESSION_S if raw else 0}; HttpOnly; SameSite=Strict"
                + ("; Secure" if self.server.secure else ""))

    def _body(self) -> dict | None:
        """POST 的 JSON 请求体；不是 JSON 对象时回 400、返回 None。只读一遍（_own 先读了的，路由里拿同一份）。"""
        if self._got:
            return self._b
        self._got = True
        length = self.headers.get("Content-Length", "0")
        try:
            n = int(length) if re.fullmatch(r"[0-9]+", length) else -1
        except ValueError:
            n = -1
        if n < 0:
            self._json(400, {"error": "Content-Length 要是非负整数"})
            return None
        try:
            raw = self.rfile.read(n) if n else b"{}"
            body = json.loads(raw) if not n or len(raw) == n else None
        except (ValueError, OverflowError):
            body = None
        if not isinstance(body, dict):
            self._json(400, {"error": "请求体不是 JSON 对象"})
            return None
        self._b = body
        return body

    def _redirect(self, to: str):
        self._send(302, "", extra={"Location": to})

    def _route(self) -> tuple[str, Project | None, str]:
        path = urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]
        if parts[:1] == ["p"] and len(parts) >= 2:
            return path, self.board.projects.get(parts[1]), "/".join(parts[2:])
        return path, None, ""

    def _client_tokens(self, tokens: list[dict]) -> None:
        now = time.time()
        for token in tokens:
            seen = token.get("client_seen")
            token["client_status"] = ("inactive" if seen and now - seen > U.CLIENT_RECENT_S else
                                      DI.client_status(token.get("client_commit"), DP.RUNNING["commit"], self.board.min_client_commit))

    def _me(self, who: U.Ident) -> dict:
        """GET /api/me 与页面上的 ME：users.me 加上在跑的代码是哪个提交、何时启动（deploy.RUNNING；部署脚本等它换成新提交，#164）。"""
        out = U.me(who, self.entry) | {"commit": DP.RUNNING["commit"], "started": DP.RUNNING["started"]}
        if out["admin"]:                               # 组内服务器页认“我”（#19：只认账号库的绑定）
            out["fleet_person"], out["fleet_person_error"] = U.person_of(who.name)
        if out["account"]:                             # 浏览器会话（「我的账号」）：自己的推送（只有打码的目标，#167）
            self._client_tokens(out["tokens"])
            out["push"] = S.push_view(who.name)
        return out

    def _page(self, html: str | bytes, who: U.Ident):
        """发一个页面：把 ME 填进去（_me，与 /api/me 同一份；页面按它藏平台操作与写按钮）。"""
        html = html.decode() if isinstance(html, bytes) else html
        return self._send(200, C.fill(html, "ME", self._me(who)))

    def _login_page(self):
        """登录页：已登录的直接回 next；还没有任何账号时页面上写怎么在本机建第一个管理员。"""
        nxt = safe_next(parse_qs(urlsplit(self.path).query).get("next", [""])[0])
        try:
            U.ident(self.headers)
            return self._redirect("./" + nxt)
        except U.Denied:
            pass
        return self._send(200, C.render_page("login.html", {"empty": not U.any_user(), "entry": self.entry, "entry_name": U.ENTRIES[self.entry], "next": nxt,
                                                            "pw_min": U.PW_MIN, "lock_min": U.LOCK_S // 60, "fails": U.FAIL_MAX}))

    # ---------- 请求 ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._gate():
            return
        path, proj, rest = self._route()
        who = self._allow(path)
        if who is False:
            return
        if path == "/login":
            return self._login_page()
        if path == "/favicon.ico":                     # 页面自带图标；浏览器照样来要，回个空的，不进日志
            return self._send(204, b"", "image/x-icon")
        if path == "/healthz":
            return self._json(200, health())
        if path == "/api/reports":
            return self._json(200, {"reports": report_headers()})
        if path == "/api/me":
            return self._json(200, self._me(who))
        if path == "/api/users":
            got = U.listing(directory=True)
            for u in got["users"]:                     # 各人填没填推送（#167；目标不给管理员看）
                self._client_tokens(u["tokens"])
                u["push"] = S.push_view(u["name"]).get("set", False)
            return self._json(200, got)
        if path == "/api/admin/deploy":
            return self._json(200, DP.status())
        if path in ("/api/client/install.sh", "/api/client/wheel"):
            return self._client(path.rsplit("/", 1)[-1])
        if path == "/api/hosts":
            got = A.hosts()
            if who.role != "admin":                    # 普通用户只列开给他的主机（#167）
                ok = U.access(who).allowed
                got["hosts"] = [dict(h, home_with=[x for x in h["home_with"] if x in ok]) for h in got["hosts"] if str(h.get("id")) in ok]
            return self._json(200, got)
        m = PROJECT_CONFIG_PATH.fullmatch(path)
        if m:
            try:
                got = PJ.get_config(unquote(m[1]))
            except PJ.Refused as x:
                return self._json(x.code, {"error": x.msg})
            return self._send(200, json.dumps(got, ensure_ascii=False), "application/json", extra={"ETag": f'"{got["ver"]}"'})
        m = PROJECT_READ_PATH.fullmatch(path)
        if m:
            return self._project_read(unquote(m[1]), m[2], who)
        if path == "/api/admin/backup":                # 冷备（#163）：状态根与主机清单打一个包，不含秘密（backup.make）
            name, data = BK.make_bytes()
            log(f"备份由 {who.name} 取走：{name}，{len(data) // 1024} KiB")
            return self._send(200, data, "application/gzip", extra={"Content-Disposition": f'attachment; filename="{name}"'})
        if path in ("/", "/index.html"):
            if who.role == "admin" and LS.primary():
                f = state_dir() / "index.html"
                snap = snapshot()
                archived = PJ.archived_rows()
                if snap and snap.get("archived", []) != archived:
                    html = C.render_page("overview.html", {**snap, "archived": archived})
                else:
                    html = f.read_bytes() if f.exists() else None
            else:                                      # 磁盘上的 index.html 带全量数据：普通用户现渲染去掉组内部分的；备用时现渲染带上此刻的主备（#163）
                snap = live_snapshot()
                html = C.render_page("overview.html", U.view(who, snap)) if snap else None
            if html is None:
                return self._send(503, "<meta charset=utf-8><meta http-equiv=refresh content=5><p>第一次采集还没完成，稍候自动刷新。")
            return self._page(html, who)
        if path == "/records.json":
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                n, before = min(int(q.get("limit") or 200), 1000), float(q["before"]) if q.get("before") else None
            except ValueError:
                return self._json(400, {"error": "limit、before 要写数字"})
            f = dict(host=q.get("host") or None, who=q.get("who") or None, q=q.get("q") or None, see=U.visible(who))
            rows = records(**f, cat=q.get("cat") or None, before=before, limit=n + 1)
            cats = Counter(cat_of(e.get("kind")) for e in records(**f)) if "cats" in q else None
            return self._json(200, {"rows": rows[:n], "more": len(rows) > n, "cats": cats})
        if path == "/overview.json":
            f = state_dir() / "snapshot.json"
            if not f.exists():
                return self._send(503, "{}", "application/json")
            return self._json(200, U.view(who, live_snapshot()))
        if path == "/fleet/op.json":
            return self._json(200, FO.view())
        if path == "/fleet/plan.json":
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            try:
                cfg = F.conf()
                if not cfg:
                    return self._send(404, "{}", "application/json")
                out = FB.preview(cfg, q.get("kind", "open"), q.get("sid") or None, q.get("person"), q.get("pool"), account=who.name, spec=q.get("spec") or None)
            except (ConfigError, PF.PlatformError) as x:
                return self._json(409, {"error": str(x)})
            return self._json(200, out)
        if path.startswith("/fleet/person/") and path.endswith(".json"):
            name = unquote(path[len("/fleet/person/"):-len(".json")])
            if not PF.NAME_RE.match(name):
                return self._send(404, "{}", "application/json")
            return self._json(200, FV.person(name))
        if path.startswith("/fleet/") and path.endswith(".json"):
            sid = path[len("/fleet/"):-len(".json")]
            try:
                ok = FV.known(sid, F.conf())
            except ConfigError:
                ok = False
            if not ok:
                return self._send(404, "{}", "application/json")
            return self._json(200, FV.detail(sid))
        if proj is None:
            return self._send(404, f"<meta charset=utf-8><p>没有这个项目。<a href='{root_rel(path)}'>返回</a>")
        if not path.endswith("/") and rest == "":
            return self._redirect(path.rsplit("/", 1)[-1] + "/")        # /p/<id> → <id>/：相对地址，反代加了前缀也对（#165）
        if rest in ("", "index.html"):
            f = proj.out / "index.html"
            if not f.exists():
                return self._send(503, "<meta charset=utf-8><meta http-equiv=refresh content=5><p>第一次采集还没完成，稍候自动刷新。")
            return self._page(f.read_bytes(), who)
        if rest == "snapshot.json":
            f = proj.out / "snapshot.json"
            return self._send(200, f.read_bytes(), "application/json") if f.exists() else self._send(503, "{}", "application/json")
        if rest == "events.json":
            ev, see = proj_events(proj), U.visible(who)
            if see:
                ev["events"] = {h: [e for e in es if see(e)] for h, es in ev["events"].items()}
            return self._json(200, ev)
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def _project_read(self, pid: str, what: str, who: U.Ident):
        """GET /api/project/<id>/(status|disk|records)（#52，api.status、api.disk、api.records）：回 {rc, out, err, data}；没登记 404。
        项目主人已由 _own 核过；status 的 CPU 取看的人能看的全局快照（users.view），records 按 users.visible 筛。"""
        try:
            PJ.require(pid)
        except PJ.Refused as x:
            return self._json(x.code, {"error": x.msg})
        if what == "status":
            return self._json(200, A.status(pid, U.view(who, snapshot())))
        if what == "disk":
            return self._json(200, A.disk(pid))
        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        try:
            n = max(1, min(int(q.get("limit") or 30), 1000))
        except ValueError:
            return self._json(400, {"error": "limit 要写数字"})
        return self._json(200, A.records(pid, q.get("host") or None, q.get("cat") or None, q.get("q") or None, n, U.visible(who)))

    def _standby(self, path: str) -> bool:
        """备用（#163）：写请求（POST、PUT）到主那台做，回 409；登录、账号、部署照收（standby_ok）。回了就是 True。
        命令行的接口（#162：wait、check、worker、pause 等，与读的 CLI_READS：主机列表、项目配置）同样过这道：client.toml 里几个地址时客户端认 standby 接着试下一个。"""
        if LS.primary() or standby_ok(path):
            return False
        self._json(409, {"error": LS.text() or "这台看板服务是备用：要动东西到主那台做", "standby": True})   # standby：命令行（client.call）接着试下一个地址
        return True

    def do_POST(self):
        if not self._gate():
            return
        path, proj, rest = self._route()
        who = self._allow(path)
        if who is False:
            return
        # 通过入口、权限与主备检查后统一读完 POST 请求体，避免关连接时截断响应（#206、#209）。
        if self._body() is None:
            return
        if path.startswith("/api/"):
            return self._api(path[len("/api/"):], who)
        if path.startswith("/renew/"):
            return self._renew(path)
        if path.startswith("/fleet/probe/"):
            return self._probe(path[len("/fleet/probe/"):])
        if path.startswith("/fleet/op/"):
            return self._fleet_op(path[len("/fleet/op/"):], who)
        if path.startswith("/sched/"):
            return self._sched(path[len("/sched/"):], who)
        if path.startswith("/project/"):
            return self._project(path[len("/project/"):], who)
        if path.startswith("/reg/"):
            return self._reg(path[len("/reg/"):], who)
        if path == "/host/assign":
            return self._host_assign(who)
        if path != "/collect" and (proj is None or rest != "collect"):
            return self._send(404, "{}", "application/json")
        if path == "/collect":
            done = self.board.overview.collect_now(fresh="fresh" in parse_qs(urlsplit(self.path).query, keep_blank_values=True))
            snap = snapshot() or None
        else:
            done = proj.collect_now()
            snap = proj.snapshot()
        if not done or snap is None:
            return self._json(504, {"error": "采集超时"})
        if done == "cached":                                   # 离上一轮不到 MIN_GAP 秒：没采，回的是现有快照，页面据此提示
            snap["cached"] = True
        return self._json(200, snap)

    def do_PUT(self):
        """推项目配置（#161）：PUT /api/project/<id>/config，见模块说明与 projects.put_config。"""
        if not self._gate():
            return
        path = urlsplit(self.path).path
        who = self._allow(path)
        if who is False:
            return
        m = PROJECT_CONFIG_PATH.fullmatch(path)
        if not m:
            return self._json(404, {"error": "没有这个接口"})
        body = self._body()
        if body is None:
            return
        pid, files = unquote(m[1]), body.get("files") or {}
        if not isinstance(body.get("text"), str) or not isinstance(files, dict):
            return self._json(400, {"error": "请求体要有 text（monitor.toml 的全文）与 files（{done_when 的路径: 内容或 null}）"})
        base = (self.headers.get("If-Match") or "").strip().strip('"') or None
        try:
            r = PJ.put_config(pid, body["text"], files, base, owner=who.name, by=f"经看板接口（{who.name}）",
                              ops=U.can_ops(who, self.entry), check_owner=self.guard)
        except U.Denied as x:
            return self._json(x.code, {"error": x.msg})
        except PJ.Refused as x:
            return self._json(x.code, {"error": x.msg, **({"ver": x.ver} if x.ver else {})})
        self.board.sync()
        p = self.board.projects.get(pid)
        if p and r["changed"]:
            p.wake.set()                                  # 下一轮就按新配置采（once 开头按修改时间重读）
        msg = (f"登记了项目 {pid}" if r["created"] else f"推上来了 {pid}：" + ("、".join(r["changed"]) or "没有改动"))
        if r["created"] or r["changed"]:
            log(f"项目：{msg}（{who.name}）")
        return self._send(200, json.dumps({**r, "msg": msg}, ensure_ascii=False), "application/json", extra={"ETag": f'"{r["ver"]}"'})

    def _api(self, act: str, who: U.Ident | None):
        """账号（#160，users.py）：登录、用设置码设口令、退出不要登录；改口令、自己的令牌只认浏览器会话；/api/users/* 只给管理员（路由表定）。
        口令、设置码、令牌原文只在请求体与这一次回话里，不进日志。"""
        body = self._body()
        if body is None:
            return
        ua = (self.headers.get("User-Agent") or "")[:160]
        s = lambda k: str(body.get(k) or "")   # noqa: E731
        try:
            if act in ("login", "setup"):
                raw, name = (U.login(s("name"), s("password"), self.entry, ua) if act == "login" else
                             U.setup(s("name"), s("code"), s("password"), self.entry, ua))
                self.cookies.append(self._cookie(raw))
                return self._json(200, {"name": name, "msg": "口令设好了，已登录" if act == "setup" else "已登录"})
            if act == "logout":
                U.logout(self.headers)
                self.cookies.append(self._cookie(None))
                return self._json(200, {"msg": "已退出"})
            if act == "password":
                return self._json(200, {"msg": U.change_pw(who, s("old"), s("new"))})
            if act == "tokens/add":
                raw, tok = U.token_add(who.name, s("name"), body.get("scopes") if isinstance(body.get("scopes"), list) else [])
                return self._json(200, {"token": raw, **tok, "commit": DP.RUNNING["commit"], "msg": f"建好了令牌「{tok['name']}」：原文只显示这一次，现在复制"})
            if act == "revoke":
                return self._json(200, {"msg": U.revoke(who.name, who.name, ids=[s("id")])})
            if act == "push":                          # 自己的推送（#167）：{"push": "bark"|"ntfy"|null, "bark"|"ntfy": {...}}，null 是删掉
                msg = S.set_user_push(who.name, body)
                log(f"账号：{who.name} {'删了' if not body.get('push') else '改了'}自己的推送")
                return self._json(200, {"msg": msg, "push": S.push_view(who.name)})
            if act == "push/test":
                return self._json(200, {"msg": S.user_push_test(who.name)})
            if act == "users/add":
                role = "admin" if body.get("admin") else "user"
                person = body.get("person")
                code = U.add(s("name"), role, who.name, person=person)
                return self._json(200, {"code": code,
                                        "msg": f"加了 {s('name')}：把设置码交给这人，24 小时内在登录页「用设置码设口令」"})
            if act == "users/public-ops":
                return self._json(200, {"msg": U.set_public_ops(s("name"), body.get("enabled"), who.name)})
            if act == "users/role":
                return self._json(200, {"msg": U.set_role(s("name"), "admin" if body.get("admin") else "user", who.name)})
            if act == "users/reset":
                return self._json(200, {"code": U.reset(s("name"), who.name),
                                        "msg": f"重置了 {s('name')} 的口令：这人已登录的浏览器都要重新登录，令牌一并吊销（命令行与实验会话要重新 labmon login）"})
            if act == "users/revoke":
                return self._json(200, {"msg": U.revoke(s("name"), who.name, ids=[s("id")] if body.get("id") else None)})
            if act == "users/remove":
                msg = U.remove(s("name"), who.name)
                return self._json(200, {"msg": msg})
            if act == "users/hosts":                   # 给普通用户开主机（#167）：改主机清单各主机项的 users
                a = U.account(s("name"))
                if not a:
                    return self._json(404, {"error": f"没有账号 {s('name')}"})
                if a["role"] == "admin":
                    return self._json(409, {"error": f"{a['name']} 是管理员：本来就能用全部主机"})
                msg = set_host_users(a["name"], _strings(body, "hosts"), who.name)
                self.board.overview.wake.set()
                log(f"账号：{msg}（{who.name}）")
                return self._json(200, {"msg": msg})
            if act == "pool/reclaim":                  # 收回（#25）：持有人本人，或能做平台操作的管理员（pool.reclaim 核）
                if not L.check(force=True)["ok"]:
                    return self._json(409, {"error": OFFLINE})
                try:
                    msg = PO.reclaim(s("id"), who, U.can_ops(who, self.entry), reg_move)
                except ConfigError as x:
                    return self._json(409, {"error": str(x)})
                self.board.overview.wake.set()
                log(f"算力池：{msg}（{who.name}）")
                return self._json(200, {"msg": msg, "servers": PO.my_servers(who)})
            if act == "pool/lend":                     # 入池开关（#24）：持有人本人，或能做平台操作、勾了「已和这人说好」的管理员（pool.set_lend 核）
                on, ok = body.get("on"), body.get("ok", False)
                if not isinstance(on, bool) or not isinstance(ok, bool) or not isinstance(body.get("hours") or "", str):
                    raise U.Denied(400, "on、ok 要是布尔值，hours 要是 HH:MM-HH:MM 或空")
                msg = PO.set_lend(s("id"), on, body.get("hours") or None, who, U.can_ops(who, self.entry), ok)
                log(f"算力池：{msg}（{who.name}）")
                return self._json(200, {"msg": msg, "servers": PO.my_servers(who)})
            if act == "admin/deploy":
                out = DP.apply(body.get("bundle"))
                log(f"部署由 {who.name} 发起：{out['msg']}")
                return self._json(200, out)
            r = self._cli(act, body, who)
            if r is not None:
                return self._json(*r)
        except U.Denied as x:
            return self._json(x.code, {"error": x.msg})
        except PJ.Refused as x:
            return self._json(x.code, {"error": x.msg})
        except (DP.Refused, ConfigError) as x:
            return self._json(409, {"error": str(x)})
        return self._json(404, {"error": f"不认识的动作 {act}"})

    def _cli(self, act: str, body: dict, who: U.Ident):
        """命令行的接口（#162，api.py、waits.py）：返回 (状态码, 回话)，由 _api 发；回话是 {rc, out, err}（命令行原样打出、按 rc 退出）。
        改东西的记一行服务日志（谁、做了什么、退出码）。不认识的动作返回 None。"""
        s = lambda k: str(body.get(k) or "")   # noqa: E731
        num = lambda k: _number(body, k)   # noqa: E731
        if act == "report":
            title = _report_text(body, "title", "现象", 200, required=True)
            evidence = _report_text(body, "evidence", "证据", 12000, required=True)
            pid = _report_text(body, "project", "项目", 128)
            hid = _report_text(body, "host", "主机", 128)
            occurred = body.get("occurred")
            if occurred is not None and (type(occurred) not in (int, float) or not 0 <= occurred <= time.time() + 300):
                raise U.Denied(400, "发生时刻要是已过去的时刻")
            try:
                rid = report(who.name, title, evidence, pid, hid, occurred, capped=who.role != "admin")
            except ReportQuota as x:
                raise U.Denied(429, str(x)) from x
            return 201, {"rc": 0, "out": f"[labmon] 已提交问题报告 {rid}\n", "report_id": rid}
        if act == "wait":
            try:
                jobs = _strings(body, "jobs")
                extra = body.get("extra")
                if extra is not None and not isinstance(extra, dict):
                    raise U.Denied(400, "extra 要是对象")
                rid = json.dumps([who.key, s("project"), jobs, s("rid")]) if s("rid") else None
                r = WT.wait(s("project"), jobs, body.get("watch") if isinstance(body.get("watch"), dict) else None,
                            num("hold") or 0, {str(k): str(v) for k, v in (extra or {}).items()}, rid,
                            account=None if who.role == "admin" else who.name,
                            session=s("session")[:64] or None, take=body.get("take") is True, who=who.name,
                            wid=s("wid")[:64] or None)
            except WT.AccountBusy as x:
                return 429, {"error": str(x)}
            except WT.Busy as x:
                return 503, {"error": str(x)}
            running = DP.RUNNING["commit"]
            status = DI.client_status(self.headers.get(CLIENT_HEADER), running, self.board.min_client_commit)
            return 200, {**r, "commit": running, "client_status": status}
        m = re.fullmatch(r"project/([^/]+)/check", act)
        if m:
            may = None if who.role == "admin" else lambda hid: U.may_host(who, hid)
            return 200, {**A.check(unquote(m[1]), s("text"), may, probe=bool(body.get("probe"))), "commit": DP.RUNNING["commit"]}   # 在跑的提交：命令行与自己的不同时提示 labmon update（#167）
        if act.startswith("worker/"):
            sub = act[len("worker/"):]
            args = _worker_args(sub, body)
            r = A.worker(sub, s("host"), args, body.get("gpus") or None, project=body.get("project") or None,
                         stop=body.get("stop") or None, own=who.role != "admin", lst=bool(body.get("list")),
                         may=None if who.role == "admin" else lambda hid: U.may_host(who, hid))
            what = f"worker {sub} {s('host')} {' '.join(args)}"
        elif act.startswith("override/"):
            verb = act[len("override/"):]
            now = body.get("now", False)
            if not isinstance(now, bool):
                raise U.Denied(400, "now 要是布尔值")
            hosts = _strings(body, "hosts")
            allowed = None
            if who.role != "admin" and verb == "pause" and now and not hosts:
                allowed = [str(h["id"]) for h in active_hosts()]
                for hid in allowed:
                    U.need_host(who, hid)
            r, what = A.override(verb, s("target"), hosts or None, num("until"), now, immediate_hosts=allowed,
                                 by=who.name, check=self.guard, own=who.role != "admin"), f"{verb} {s('target')}"
        elif act == "project/init":
            name = body.get("name") or None
            if name is not None and (not isinstance(name, str) or "/" in name or name in (".", "..") or "\x00" in name):
                raise U.Denied(400, "项目目录名称要是单个目录名")
            r, what = A.project_init(s("host"), name), f"project init {s('host')} {s('name')}"
        elif act == "borrow":
            r, what = A.borrow(s("id"), num("until"), body.get("for") or None, body.get("from") or None, bool(body.get("away")), account=who.name), f"borrow {s('id')}"
        elif act.startswith("host/"):
            r, what = A.host(act[len("host/"):], s("id"), num("at"), bool(body.get("now"))), f"host {act[len('host/'):]} {s('id')}"
        else:
            return None
        if act not in ("worker/status", "worker/fit"):
            log(f"命令行（{who.name}）：{what.strip()}：退出码 {r['rc']}")
        return 200, r

    def _client(self, what: str):
        """由看板发客户端（#167，dist.py）：install.sh?base=<看板根地址> 一个装好命令行的 sh 脚本（嵌着 wheel、填好地址的 client.toml、[server] ca_cert 的 CA），
        wheel 只要包（labmon update）。base 是页面自己的地址（含反代的路径前缀）：要是 http(s) 地址、主机在这个入口认的 Host 里，才写进 client.toml。"""
        try:
            if what == "wheel":
                name, data, commit = DI.wheel(DP.RUNNING["commit"])
                return self._send(200, data, "application/zip", extra={"Content-Disposition": f'attachment; filename="{name}"', "X-Labmon-Commit": commit})
            base = parse_qs(urlsplit(self.path).query).get("base", [""])[0].strip()
            m = DI.parse_base(base)
            scheme = "https" if self.server.secure else "http"
            if not m or m[0] != scheme or not host_ok(m[1], 443 if scheme == "https" else 80, self.server.hosts):
                return self._json(400, {"error": "base 要是这个看板的根地址（http(s)://主机[:端口]/前缀/，主机在这个入口认的 Host 里）"})
            ca = server_conf()["ca_cert"] if self.entry == "lan" and scheme == "https" else None
            pem = None
            if ca:
                try:
                    with open(ca, "rb") as f:
                        pem = f.read()
                except OSError as e:
                    return self._json(503, {"error": f"主机清单 [server] ca_cert 读不了：{e}"})
            name, sh = DI.install_sh(base, pem, self.entry == "public", DP.RUNNING["commit"])
        except (DI.Unavailable, ConfigError) as e:
            return self._json(503, {"error": f"发不了客户端：{e}"})
        return self._send(200, sh, "text/plain; charset=utf-8", extra={"Content-Disposition": f'attachment; filename="{name}"'})

    def _probe(self, sid: str):
        try:
            cfg = F.conf()
        except ConfigError as x:
            return self._json(409, {"error": str(x)})
        if not cfg or sid not in {s["id"] for s in cfg["servers"]}:
            return self._json(404, {"error": f"组内服务器里没有 {sid}"})
        if not L.check(force=True)["ok"]:
            return self._json(409, {"error": OFFLINE})
        self.board.fleet.tick(force=sid)
        snap = snapshot()
        return self._json(200, FV.view(snap.get("hosts") or [], cfg=cfg))

    def _fleet_op(self, act: str, who: U.Ident):
        body = self._body()
        if body is None:
            return
        try:
            cfg = F.conf()
            if not cfg:
                return self._json(404, {"error": "主机清单里没有 [fleet] 段"})
            if act == "captcha" or act in FO.KINDS:
                if not L.check(force=True)["ok"]:
                    return self._json(409, {"error": OFFLINE})
            if act == "captcha":
                return self._json(200, FO.captcha(cfg))
            if act in FO.KINDS:
                kw = {}
                if act != "sync":
                    kw["ids"] = [str(x) for x in body.get("ids") or []]
                if act in ("renew", "off", "swap", "move"):  # 页面上确认过“会中断在跑的实验”的服务器
                    kw["ack"] = [str(x) for x in body.get("ack") or []]
                if act in ("swap", "open"):                # 新的使用人（镜像与启动命令按这人的设置）
                    kw["person"] = body.get("person")
                if act in ("swap", "move", "open"):        # 规格（物理节点）：换人、开机可省（沿用原规格），换节点必选
                    kw["spec"] = body.get("spec")
                if act == "open":
                    kw["pool"] = body.get("pool")
                FO.start(act, cfg, str(body.get("code") or "").strip() or None, tok=body.get("tok") or None, account=who.name, **kw)
            elif act == "person":
                FB.set_person(str(body.get("name") or ""), *(None if body.get(k) is None else str(body[k]) for k in ("image", "run_cmd", "note")),
                              remove=bool(body.get("remove")), cfg=cfg, create=bool(body.get("create")))
            elif act == "defaults":
                FB.set_defaults(str(body.get("image") or ""), str(body.get("run_cmd") or ""))
            elif act == "disk":                            # 共享盘：一级目录归给谁、某人的提醒线
                try:
                    if "dir" in body:
                        DK.set_owner(str(body["dir"]), body.get("owner"))
                    else:
                        DK.set_limit(str(body.get("name") or ""), body.get("limit"))
                except ValueError as x:
                    return self._json(409, {"error": str(x)})
            elif act == "guard":                           # 保活守护：一台的开关，或空闲多久开、开后盯多久
                F.set_guard(**({"sid": str(body["id"]), "on": bool(body.get("on"))} if body.get("id") else
                               {"idle_min": body.get("idle_min"), "watch_min": body.get("watch_min")}))
            elif act == "keepalive":                       # 把一组负载认作保活：它的脚本写进主机清单，页面随后再探一次
                F.mark_keepalive(str(body.get("id") or ""), int(body.get("pid") or 0))
            elif act in ("borrow", "borrow_return"):         # 借用：会 ssh 到这台（借来时拉起 worker、还时停 worker 开保活）
                sid = str(body.get("id") or "")
                srv = next((x for x in cfg["servers"] if x["id"] == sid), None)
                if srv and not L.check(force=True)["ok"]:
                    return self._json(409, {"error": OFFLINE})
                until = float(body["until"]) if body.get("until") else None
                if act == "borrow":
                    check_future(until, "约定时间")          # 已经过去的约定当场报回表单（借来用在后台跑，不等它报错）
                if act == "borrow_return" and body.get("cancel"):
                    msg = B.cancel_return(sid)
                elif act == "borrow_return":
                    snap = snapshot()
                    e = next((x for x in snap.get("hosts", []) if x["id"] == sid), {})
                    msg = B.request_return(sid, bool(body.get("now")), (e.get("status") or {}).get("running"))
                elif srv and not F.borrowed(srv):              # 借来用：后台跑，进度在 op.json（和别的组内操作一样）
                    if not body.get("ok"):
                        return self._json(409, {"error": f"先和 {srv['holder']} 说好再借"})
                    FO.start("borrow", cfg, None, ids=[sid], until=until, projects=body.get("for") or None, light=bool(body.get("light")),
                             away=bool(body.get("away")), account=who.name)
                    log(f"借用：开始借 {sid}")
                    return self._json(200, FO.view())
                elif srv:                                    # 改约定：只改主机清单、补指令，很快
                    msg = B.begin(sid, until, body.get("for") or None, away=bool(body.get("away")), account=who.name)
                else:
                    msg = B.set_swap(sid, until, body.get("from") or None)
                log(f"借用：{msg}")
                return self._json(200, {"msg": msg})
            elif act == "dismiss":
                FO.dismiss()
            elif act == "logout":
                FO.logout()
            else:
                return self._json(404, {"error": f"不认识的动作 {act}"})
        except U.Denied as x:
            return self._json(x.code, {"error": x.msg})
        except (ConfigError, PF.PlatformError) as x:
            return self._json(409, {"error": str(x)})
        what = body.get("ids") or [str(body[k]) for k in ("id", "name", "dir") if body.get(k)]
        log(f"组内服务器平台操作：{act}" + (f" {'、'.join(what)}" if what else ""))
        return self._json(200, FO.view())

    def _renew(self, path: str):
        parts = [x for x in path.split("/") if x]
        if len(parts) != 3:
            return self._json(404, {"error": "not found"})
        hid, act = parts[1], parts[2]
        h = next((x for x in active_hosts() if x["id"] == hid), None)
        if h is None:
            return self._json(404, {"error": f"没有主机 {hid}"})
        if h.get("ssh") and not L.check(force=True)["ok"]:
            return self._json(409, {"error": OFFLINE})
        snap = snapshot()
        e = next((x for x in snap.get("hosts", []) if x["id"] == hid), {})
        try:
            if act == "reserve":
                msg = R.reserve(hid, e.get("lease_end"))
            elif act == "cancel":
                msg = R.cancel(hid, e.get("lease_end"))
            else:
                return self._json(404, {"error": f"不认识的动作 {act}"})
        except PF.PlatformError as x:
            return self._json(409, {"error": str(x)})
        log(f"续租 {hid} {act}：{msg}")
        return self._json(200, {"msg": msg})


    def _sched(self, act: str, who: U.Ident):
        """调度卡：改项目的权重（主机清单的 [sched.weight]）、加撤优先与暂停（[sched.focus]、[sched.pause]，所有主机，含之后加入的，#96）；
        都只改主机清单，看板下一轮随 sched.json 写给各 worker。权重、优先只给管理员（路由表）；暂停记谁下的，普通用户只动自己下的（users.need_pause，#16）。"""
        body = self._body()
        if body is None:
            return
        pid = str(body.get("project") or "")
        if not pid:
            return self._json(400, {"error": "没说是哪个项目"})
        try:
            with HOSTS_LOCK:
                adding = act in ("pause", "focus") or (act == "weight" and body.get("weight") not in (None, 1))
                if adding and (reason := A.unknown_project(pid, require=True)):
                    raise ConfigError(reason)
                if act == "weight":
                    msg = set_weight(pid, body.get("weight"))
                elif act in SCHED_ACTS:
                    kind, on = SCHED_ACTS[act]
                    until = _number(body, "until") if on else None
                    self.guard()                       # 暂停是不是他下的：在写主机清单的同一把锁里再核（_own 定，#16）
                    msg = set_sched_ovr(kind, pid, on, until, by=who.name)
                    if kind == "pause" and "本来就没有" not in msg:   # 暂停、恢复会让会话以为卡住或要接着加任务：告诉它（权重、优先只改排序，不叫醒）
                        tell(pid, act, f"{sched_ovr_text(kind, pid, until)}（所有主机）：不再放行它的新任务，正在跑的跑完，排着的等恢复" if on
                             else f"恢复 {pid}（所有主机）：照常放行它的任务")
                else:
                    return self._json(404, {"error": f"不认识的动作 {act}"})
        except U.Denied as x:
            return self._json(x.code, {"error": x.msg})
        except ConfigError as x:
            return self._json(409, {"error": str(x)})
        log(f"调度：{msg}")
        return self._json(200, {"msg": msg})


    def _project(self, act: str, who: U.Ident):
        """项目的归档与恢复（#97，projects.archive / restore，命令行 labmon project archive|restore 同一处）：做完当场对齐看板服务里的项目（Server.sync）。
        归档后 labmon wait 每次都报“已归档”（记着是页面上还是命令行归档的）；页面上恢复的告诉实验会话一次（records.tell，#121；命令行做的是会话自己，不用告诉）。
        config：改看板上这个项目的 monitor.toml（#122，projects.set_config），当场重读这个项目的配置、叫它的采集线程下一轮就采，告诉实验会话一次
        （它的 labmon wait 每轮把看板上改的拉回项目目录，#161）。"""
        body = self._body()
        if body is None:
            return
        pid = str(body.get("project") or "")
        if not pid:
            return self._json(400, {"error": "没说是哪个项目"})
        try:
            if act == "archive":
                p = self.board.projects.get(pid)
                if p is None:
                    return self._json(404, {"error": f"看板服务里没有项目 {pid}"})
                msg = PJ.archive(pid, p.snapshot(), snapshot(), p.interval, by="看板页面上" if who.via == "session" else f"经命令行（{who.name}）",
                                 check_owner=self.guard, own=who.role != "admin", who=who)
            elif act == "config":
                p = self.board.projects.get(pid)
                if p is None:
                    return self._json(404, {"error": f"看板服务里没有项目 {pid}"})
                if not isinstance(body.get("set"), dict):
                    return self._json(400, {"error": "没说要改什么"})
                said = PJ.set_config(pid, body["set"], check_owner=self.guard)
                if not said:
                    return self._json(200, {"msg": "没有改动"})
                p._reload()
                p.wake.set()                               # 项目页按新配置早点重画（不等，页面随后自己 POST /collect）
                msg = f"改了 {pid} 的 monitor.toml：" + "；".join(said)
                tell(pid, "config", f"{msg}。改的是看板上的那份；labmon wait 下一轮把它拉回项目目录（没挂 wait 的 labmon project pull），随项目一起提交，别当成误改回滚")
                log(f"项目：{msg}")
                return self._json(200, {"msg": msg})
            elif act == "owner":                       # 换项目主人（#167，只给管理员，路由表定）；owner 为空是归回管理员
                o = str(body.get("owner") or "")
                a = U.account(o) if o else None
                if o and not a:
                    return self._json(404, {"error": f"没有账号 {o}"})
                msg = PJ.set_owner(pid, a["name"] if a else None, who.name)
                self.board.overview.wake.set()
            elif act == "delete":
                revision = body.get("revision")
                if not isinstance(revision, str) or not revision:
                    return self._json(400, {"error": "请刷新归档列表并确认永久删除"})
                msg = self.board.delete_project(pid, revision, who.name)
            elif act == "restore":
                msg = PJ.restore(pid, check_owner=self.guard)
                if who.via == "session":
                    tell(pid, "restore", f"恢复了项目 {pid}：看板接着采集它；登记没恢复，要跑先 labmon worker add <主机> ROOT JOBS --project {pid}")
            else:
                return self._json(404, {"error": f"不认识的动作 {act}"})
        except (U.Denied, PJ.Refused) as x:
            return self._json(x.code, {"error": x.msg})
        except ConfigError as x:
            return self._json(409, {"error": str(x), **({"field": x.field} if x.field else {})})   # 改配置出错的那一项：页面标到那一行
        self.board.sync()
        if act == "restore" and pid not in self.board.projects:
            msg += "；看板上它的配置这次没加载上，看服务日志"
        log(f"项目：{msg}")
        return self._json(200, {"msg": msg})

    def _reg(self, act: str, who: U.Ident):
        """搬、移除队列登记（#95；#121 起页面上按主机成批）：搬是在目标主机上 worker add（把别的主机上同一条清单的登记删掉、写到这台），移除是 worker rm
        （已归还、主机清单里没有的主机经 worker_home 相同的在用主机代办）。每条记一条配置改动，按项目告诉实验会话一次（records.tell）；页面随后立即采集。"""
        body = self._body()
        if body is None:
            return
        if act not in ("move", "rm"):
            return self._json(404, {"error": f"不认识的动作 {act}"})
        if not L.check(force=True)["ok"]:
            return self._json(409, {"error": OFFLINE})
        try:
            items = _reg_items(body, move=act == "move")
            own = who.role != "admin"
            if act == "move" and str(body.get("to") or "") in PO.lent_ids():   # 借给算力池的主机上放什么由看板定（#25）
                raise ConfigError(f"{body.get('to')} 是算力池借来的：要用池就让实验会话 labmon worker add pool，放到哪台由看板定")
            msg = reg_move(str(body.get("to") or ""), items, own=own) if act == "move" else \
                reg_rm(str(body.get("host") or ""), items, body.get("home") or None, body.get("project") or None, own=own)
        except U.Denied as x:
            return self._json(x.code, {"error": x.msg})
        except ConfigError as x:
            return self._json(409, {"error": str(x)})
        log(f"队列登记：{msg}")
        return self._json(200, {"msg": msg})

    def _host_assign(self, who: U.Ident):
        """无登记自用主机页的「给项目用」（#173）：经 records.tell 通知实验会话登记，不改任何登记；做完叫醒全局采集把消息尽快写进快照 wake。"""
        body = self._body()
        if body is None:
            return
        try:
            hid, pid = _assign_args(body)
            msg = host_assign(hid, pid)
        except U.Denied as x:
            return self._json(x.code, {"error": x.msg})
        except ConfigError as x:
            return self._json(409, {"error": str(x)})
        self.board.overview.wake.set()
        log(f"主机：{msg}（{who.name}）")
        return self._json(200, {"msg": msg})


def _strings(body: dict, key: str) -> list[str]:
    xs = body.get(key)
    if xs is None:
        return []
    if not isinstance(xs, list) or any(not isinstance(x, str) or "\x00" in x for x in xs):
        raise U.Denied(400, f"{key} 要是字符串列表")
    return xs


def _report_text(body: dict, key: str, label: str, limit: int, *, required: bool = False) -> str | None:
    raw = body.get(key)
    if raw is None and not required:
        return None
    if (not isinstance(raw, str) or len(raw) > limit or not raw.strip()
            or any(ord(c) < 32 and (key != "evidence" or c not in "\n\t\r") for c in raw)
            or any(0xD800 <= ord(c) <= 0xDFFF for c in raw)):
        raise U.Denied(400, f"{label}要是 1 到 {limit} 字的文本")
    return raw.strip() if key in ("project", "host") else raw


def _number(body: dict, key: str) -> float | None:
    n = body.get(key)
    if n is None:
        return None
    try:
        n = float(n)
    except (TypeError, ValueError, OverflowError):
        n = math.nan
    if not math.isfinite(n):
        raise U.Denied(400, f"{key} 要是有限数字")
    return n


def _worker_args(sub: str, body: dict) -> list[str]:
    """授权与 worker 必须对同一组位置参数达成一致；命令行选项单独从 JSON 字段取。"""
    args = _strings(body, "args")
    if any(not x or x.startswith("-") for x in args):
        raise U.Denied(400, "args 只接受位置参数，选项请单独传")
    if body.get("list") not in (None, False, True) or (body.get("list") and (sub != "fit" or len(args) != 2 or not body.get("project"))):
        raise U.Denied(400, "list 只用于 worker fit：位置参数是 ROOT JOBS，要写 project")
    if (sub in ("add", "rm") and len(args) != 2) or (sub in ("start", "stop", "status") and args) or (sub == "fit" and not args):
        raise U.Denied(400, f"worker {sub} 的位置参数数量不对")
    for key in ("project", "gpus") if sub == "add" else ("project", "gpus", "stop"):
        if body.get(key) is not None and (not isinstance(body[key], str) or "\x00" in body[key] or body[key].startswith("-")):
            raise U.Denied(400, f"worker 的 {key} 要是有效的字符串")
    if body.get("gpus") and not re.fullmatch(r"\d+(?:,\d+)*", body["gpus"]):
        raise U.Denied(400, "gpus 要是用逗号分开的 GPU 序号")
    if sub == "add":
        _stop_method(body.get("stop"))
    return args


def _stop_method(stop: str | None) -> None:
    try:
        queue_stop_path(stop)
    except ValueError as e:
        raise U.Denied(400, str(e)) from e


def _assign_args(body: dict) -> tuple[str, str]:
    """POST /host/assign 的参数核对：host 与 project 都要是非空字符串（不含空字符）。"""
    out = []
    for k, label in (("host", "主机"), ("project", "项目")):
        v = body.get(k)
        if not isinstance(v, str) or not v.strip() or "\x00" in v:
            raise U.Denied(400, f"要选定{label}")
        out.append(v.strip())
    return out[0], out[1]


def host_assign(hid: str, pid: str) -> str:
    """通知项目 pid 的实验会话可以使用自用主机 hid（#173）：仅主服务可写（lease.need），不改登记、不替会话挑清单，
    复用 records.tell(pid, "host", …) 让会话在下一轮 labmon wait 收到，并记一条配置改动事件。"""
    LS.need("通知实验会话使用这台主机")
    h = next((x for x in active_hosts() if str(x.get("id")) == hid), None)
    if h is None:
        raise ConfigError(f"没有在用主机 {hid}")
    if h.get("borrow") and not h["borrow"].get("swap"):
        raise ConfigError(f"{hid} 是借来的组内服务器，不是自用主机")
    with PJ._reg_locked():
        if reason := A.unknown_project(pid, require=True):
            raise ConfigError(reason)
    tell(pid, "host", f"自用主机 {hid} 可以给 {pid} 用：请按 skill 评估并登记清单（labmon worker add {hid} ROOT JOBS --project {pid}）")
    log_events([{"kind": "config", "host": hid, "project": pid, "msg": f"看板通知 {pid} 的实验会话使用自用主机 {hid}"}])
    return f"已通知 {pid} 的实验会话使用 {hid}（下一轮 labmon wait 收到）"


def _reg_items(body: dict, move: bool) -> list[dict]:
    items = body.get("items")
    keys = ("root", "jobs", "project", "host") if move else ("root", "jobs")
    if not isinstance(items, list) or not items or any(
        not isinstance(it, dict) or any(not isinstance(it.get(k), str) or not it[k] or it[k].startswith("-") or "\x00" in it[k] for k in keys)
        for it in items
    ):
        raise U.Denied(400, "items 要列出每条登记的目录、清单" + ("、项目和原主机" if move else ""))
    if move:
        for it in items:
            _stop_method(it.get("stop"))
    return items


def _qname(root: str, jobs: str) -> str:
    return f"{root.rstrip('/')}/{jobs}"


def _running(hid: str, root: str, jobs: str) -> list[str]:
    """全局快照里这台主机上这条清单正在跑的任务名（移除登记前查：在跑的不让移除）。"""
    e = next((x for x in (snapshot().get("hosts") or []) if x["id"] == hid), {})
    same = lambda j: os.path.normpath(j.get("root") or "") == os.path.normpath(root) and j.get("queue") == jobs   # noqa: E731
    return [j.get("name") or "?" for j in ((e.get("status") or {}).get("running") or []) if same(j)]


def _qlist(qs: list[str]) -> str:
    return "、".join(qs[:3]) + (f" 等 {len(qs)} 条" if len(qs) > 3 else "")


def reg_move(to: str, items: list, own: bool = False, pool: str | None = None) -> str:
    """把几条登记搬到在用主机 to（项目抽屉的「整台搬到…」、主机页的接上卡）：逐条 workerctl.run_out("add")，带登记的 project 与 stop，不带 --gpus（原主机的卡号）。
    原主机上的那份由 worker add 删掉（原主机手上在跑的跑完后 to 接着放；别处刚登记了更晚的退出码 2，原样报错）。一条失败就停，前面搬成的照样记；
    搬成的按项目各告诉一次实验会话（records.tell，中途失败也告诉已搬的）。返回给页面的一句话。
    pool：算力池放、搬的（pool.tick、pool.reclaim，#25；值是为什么搬，可为空）：to 可以是 POOL（回池里待放）。只记事件、不告诉会话（#88：会话不用动手，
    wait --job 按快照车道认任务，去向在 kind=pool 的事件里，labmon records 与项目页时间线看得到；要会话动手的有 pool_wait: 兜着）。"""
    if not to or not items:
        raise ConfigError("没说搬到哪台、搬哪几条")
    done, moved = [], {}
    try:
        for it in items:
            root, jobs, pid, frm = str(it.get("root") or ""), str(it.get("jobs") or ""), str(it.get("project") or ""), str(it.get("host") or "")
            if not (root and jobs and pid):
                raise ConfigError("登记缺目录、清单或项目")
            with PJ._reg_locked():
                if reason := A.unknown_project(pid, require=True):
                    raise ConfigError(reason)
                rc, out = W.run_out("add", to, [root, jobs], project=pid, stop=it.get("stop") or None, **({"expect_project": pid} if own else {}),
                                    **({"expect_from": frm} if pool is not None and frm and frm != to else {}),   # 算力池按上一轮快照搬：worker 在锁里核对原来那台还登记着（#25）
                                    **({"pool": True} if pool is not None and to != POOL else {}))   # 池放过去的登记带标记（#79：自用主机上与主人自己的清单分开）
            text = " ".join(out.strip().splitlines()[-2:])
            if rc != 0:
                raise ConfigError(f"{_qname(root, jobs)} 没搬到 {to}：{text}" + (f"（已搬 {len(done)} 条）" if done else ""))
            what = f"{_qname(root, jobs)}" + (f" 从 {frm}" if frm and frm != to else "") + f" 搬到 {to}"
            log_events([{"kind": "config", "hosts": sorted({to, frm} - {""}), "project": pid, "msg": f"看板把队列登记 {what}", "detail": text}])
            done.append(text)
            m = moved.setdefault(pid, {"from": [], "qs": []})
            if frm and frm != to and frm not in m["from"]:
                m["from"].append(frm)
            m["qs"].append(jobs)
    finally:
        for pid, m in moved.items():
            if pool is None:
                tell(pid, "move", f"把 {pid}" + (f" 在 {'、'.join(m['from'])} 上" if m["from"] else "") + f"的 {len(m['qs'])} 条清单登记搬到 {to}（{_qlist(m['qs'])}）："
                     f"之后由 {to} 放行，原主机上在跑的跑完为止；再登记新清单写 {to}（labmon worker add {to} ROOT JOBS --project {pid}）")
    return "；".join(done)


def reg_rm(hid: str, items: list, home: str | None = None, project: str | None = None, own: bool = False) -> str:
    """移除一台主机上的几条登记（项目抽屉的「从这台移除」：这个项目在这台的全部登记）：先查一遍，有一条在这台上有任务在跑就整批拒绝（先暂停或等跑完）；
    再逐条 workerctl.run_out("rm")（已不在用的主机经在用主机代办），一条失败就停、报已移除几条。移除成的告诉一次实验会话（records.tell）。"""
    qs = [(str(it.get("root") or ""), str(it.get("jobs") or "")) for it in items or []]
    if not hid or not qs or not all(r and j for r, j in qs):
        raise ConfigError("没说是哪台主机的哪几条登记")
    run = [n for r, j in qs for n in _running(hid, r, j)]
    if run:
        raise ConfigError(f"{hid} 上 {'、'.join(run)} 正在跑：先暂停或等跑完再移除")
    done = []
    try:
        for root, jobs in qs:
            rc, out = W.run_out("rm", hid, [root, jobs], home=home, **({"expect_project": project} if own else {}))
            text = " ".join(out.strip().splitlines()[-2:])
            if rc != 0 or "没登记过" in text:              # 已被别的页面、会话搬走或移除了：不记一条“移除了”
                raise ConfigError(f"{_qname(root, jobs)} 没从 {hid} 上移除：{text}" + (f"（已移除 {len(done)} 条）" if done else ""))
            log_events([{"kind": "config", "host": hid, "project": project, "msg": f"看板移除了 {hid} 上的队列登记 {_qname(root, jobs)}", "detail": text}])
            done.append(jobs)
    finally:
        if done and project:
            tell(project, "rm", f"移除了 {project} 在 {hid} 上的 {len(done)} 条清单登记（{_qlist(done)}）：worker 不再放行它们，清单文件与结果没动；"
                 f"要再跑先 labmon worker add <主机> ROOT JOBS --project {project}")
    return f"移除了 {hid} 上的 {len(done)} 条登记（{_qlist(done)}）"


LOCAL_NAMES = ("127.0.0.1", "localhost")
HANDSHAKE_S = 10                    # TLS 握手限时（秒）


def allowed_hosts(port: int, extra: list[str] | None, dport: int) -> set[tuple[str, int]]:
    """一个入口认的 Host（#158、#165）：127.0.0.1、localhost 加这个入口自己的端口，加上 [server] 里这个入口另认的（config.server 已校验过写法）；
    都换成 (主机, 端口)，没写端口的按 dport（有 TLS 的与公网入口 443，没 TLS 的 80）。"""
    return {(n, port) for n in LOCAL_NAMES} | {host_pair(x, dport) for x in extra or []}


def host_ok(h: str, dport: int, allowed: set[tuple[str, int]]) -> bool:
    """Host 头认不认（Handler._gate）：换成 (主机, 端口) 在 allowed 里；没写端口的按 dport（浏览器访问缺省端口时不带）。"""
    return host_pair(h, dport) in allowed


def _host_text(x: tuple[str, int], dport: int) -> str:
    return x[0] if x[1] == dport else f"{x[0]}:{x[1]}"


def tls_wrap(srv: ThreadingHTTPServer, cert: str | None, key: str | None) -> bool:
    """给入口的监听套上 TLS（#165，[server] 的 tls_cert、tls_key）：TLS 1.2 起；不在 accept 里握手（do_handshake_on_connect=False），
    由 Handler.setup 在处理线程里做。没配证书返回 False（明文）；证书读不了抛 OSError、ssl.SSLError，serve 报错退出，不退回明文。"""
    if not cert:
        return False
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return True


def health(t: float | None = None) -> dict:
    """GET /healthz（不要登录，#165）：给 TX 的看门狗探活，只回这四项，不出主机、项目、人员。
    ok：服务在答；primary：是不是主（服务租约 lease.flag，#163：还没跑过租约的是 None，看门狗只把 False 当异常）；
    last_collect：工作台最后一次采集成功的时刻（全局快照文件的修改时刻，只有 overview.collect 写它：重启不清零，采集一直失败就一直变旧；只 stat、不读快照）；
    maint_until：计划维护到何时（maint.until，不在维护为 None）。"""
    try:
        last = round((state_dir() / "snapshot.json").stat().st_mtime)
    except OSError:
        last = None
    return {"ok": True, "primary": LS.flag(), "last_collect": last, "maint_until": MT.until(t)}


def live_snapshot() -> dict:
    """全局快照，备用时把 server 换成此刻的主备（lease.view）：备用不采集、快照停在当主时的那一轮，横幅要按此刻的说。
    主服务现填归档列表；不回写快照文件：/healthz 的 last_collect 是它的修改时刻（#163）。"""
    snap = snapshot()
    if snap and LS.primary():
        snap["archived"] = PJ.archived_rows()
    elif snap:
        snap["server"] = LS.view()
    return snap


# 备用时照收的写请求（#163）：登录与账号（只动本机的账号库）、部署（换代码、重启，主备都要能部署）。别的写请求回 409。
PROJECT_CONFIG_PATH = re.compile(r"/api/project/([^/]+)/config")
PROJECT_READ_PATH = re.compile(r"/api/project/([^/]+)/(status|disk|records)")   # 实验会话的只读命令（#52）：labmon status、disk、records
CLI_READS = re.compile(r"/api/(hosts|project/[^/]+/(config|status|disk|records)|client/wheel|reports)")     # 命令行读的接口：备用上同样回 409 standby（_standby），命令行接着找主那台
STANDBY_OK = re.compile(r"/api/(login|setup|logout|password|tokens/add|revoke|users/.*|admin/deploy)")


def standby_ok(path: str) -> bool:
    return bool(STANDBY_OK.fullmatch(path))


class Route(NamedTuple):
    """路由表的一行：方法、路径（整段匹配）、要的作用域（users.SCOPES 里的一个，几个里有一个就行的元组；OPEN 不要登录）、
    只给管理员、只在内网入口（公网入口上回 404，视同没注册）、平台操作许可 ops、普通用户按什么核对象（own，#167，OWN 里的一个，由 Handler._own 核；
    普通用户走到没写 own 的写路由一律 403：新写路由忘了写时从严）。"""
    method: str
    path: re.Pattern
    scope: str | tuple
    admin: bool = False
    lan: bool = False
    own: str | None = None
    ops: bool = False


OPEN = ""
# own（#167，Handler._own）：普通用户的这个请求按什么核。path 地址里的项目要是他的；new 同 path，还没登记的也放（第一次推就登记）；
# body 请求体的 project；worker、override、init、wait、reg 见 _own；self 只动自己的账号；holder 请求体 id 那台组内服务器的持有人本人（#24）。
OWN = ("path", "new", "body", "report", "worker", "override", "init", "wait", "reg", "self", "holder", "host_assign")
_R = lambda m, p, scope, admin=False, lan=False, own=None, ops=False: Route(m, re.compile(p), scope, admin, lan, own, ops)   # noqa: E731
# 看板服务的路由表（#160）：每条路由要什么权限只在这里写；Handler._allow 按它调 users.check。
# 没登记的路径 GET 按 read、POST 与 PUT 按管理员的 admin（新写路由忘了登记时从严）。平台操作由 ops 核对账号入口许可；部署等纯内网路由用 lan。
ROUTES = [
    _R("GET", r"/login|/favicon\.ico|/healthz", OPEN),                 # /healthz：TX 的看门狗探活，只回四项（health，#165）
    _R("POST", r"/api/(login|setup|logout)", OPEN),
    _R("GET", r"/api/me", "read"),
    _R("POST", r"/api/users/public-ops", "admin", admin=True, lan=True),
    _R("GET", r"/api/reports", "reports", admin=True),
    _R("POST", r"/api/report", "project", own="report"),
    _R("GET", r"/api/admin/deploy", "deploy", admin=True),             # 部署（#164）：在跑哪个提交、平台操作、日志里的 Traceback
    _R("GET", r"/api/admin/backup", "backup", admin=True, lan=True),   # 冷备（#163）：笔记本每天经内网入口来取一份（backup pull），不含秘密；公网入口不给（#17）
    _R("POST", r"/api/admin/deploy", "deploy", admin=True, lan=True),  # 收 git bundle 快进、重启：等于在这台上跑新代码，只在内网入口
    _R("POST", r"/api/worker/start", "deploy", admin=True, lan=True),  # 拉起 worker（部署后、labmon worker start）：要连服务器、推代码，仍只在内网入口
    # 命令行（#162，api.py）：会改整台主机、借卡、改主机清单在用不在用的要 admin 令牌（实验会话的 project 令牌做不了，交给用户）
    _R("POST", r"/api/worker/(stop|limit)", "admin", admin=True),
    _R("POST", r"/api/worker/(add|rm|status|fit)", "project", own="worker"),
    _R("POST", r"/api/override/(focus|unfocus)", "project", admin=True),   # 优先只给管理员（#16），带不带 --host 都一样
    _R("POST", r"/api/override/(pause|resume)", "project", own="override"),
    _R("POST", r"/api/override/(drain|undrain)", "admin", admin=True),
    _R("POST", r"/api/(borrow|host/return|host/restore)", "admin", admin=True, ops=True),   # 借用与借来的还：会 ssh 到组内服务器
    _R("POST", r"/api/wait", "project", own="wait"),                   # labmon wait 的长轮询（waits.wait）
    _R("POST", r"/api/project/init", "project", own="init"),
    _R("POST", r"/api/pool/lend", "project", own="holder"),            # 入池开关（#24）：持有人本人；管理员代开要能做平台操作（pool.set_lend 核）
    _R("POST", r"/api/pool/reclaim", "project", own="holder"),         # 收回（#25）：持有人本人；管理员要能做平台操作（pool.reclaim 核）
    _R("POST", r"/api/project/[^/]+/check", "project", own="new"),
    _R("GET", r"/api/hosts", "read"),                                  # 普通用户只列他能用的（Handler.do_GET）
    _R("GET", PROJECT_CONFIG_PATH.pattern, "read", own="path"),
    _R("GET", PROJECT_READ_PATH.pattern, "read", own="path"),         # labmon status|disk|records（#52）：只读，普通用户只看自己的项目
    _R("PUT", PROJECT_CONFIG_PATH.pattern, "project", own="new"),     # 推项目配置（#161）：第一次推的人是主人（#167）
    _R("POST", r"/api/(password|tokens/add|revoke|push|push/test)", U.ACCOUNT, own="self"),   # 自己的口令、令牌、推送（#167）：只认浏览器会话
    _R("GET", r"/api/client/(install\.sh|wheel)", "read"),             # 由看板发客户端（#167，dist.py）：登录了就能取
    _R("GET", r"/api/users", "admin", admin=True, lan=True),          # 账号管理只在内网入口（#17）；自己的口令、令牌照常
    _R("POST", r"/api/users/(add|role|reset|revoke|remove|hosts)", "admin", admin=True, lan=True),
    _R("GET", r"/|/index\.html|/overview\.json|/records\.json", "read"),   # 普通用户只看到自己的（users.view、users.visible）
    _R("GET", r"/p/.*", "read", own="path"),                           # 项目页：普通用户只看自己的项目（#167）
    _R("GET", r"/fleet/.*", "read", admin=True),                       # 组内服务器的数据只给管理员（维护者 2026-11-10 定）
    _R("POST", r"/(fleet/op|fleet/probe|renew)/.*", "admin", admin=True, ops=True),
    _R("POST", r"/sched/(weight|focus|unfocus)", "project", admin=True),   # 调度权重（#157 E 节的表）与优先（#16）只给管理员
    _R("POST", r"/project/(owner|delete)", "admin", admin=True),                # 换项目主人（#167）：只给管理员
    _R("POST", r"/(sched|project)/[^/]+", "project", own="body"),
    _R("POST", r"/reg/(move|rm)", "project", own="reg"),
    _R("POST", r"/host/assign", "project", own="host_assign"),
    _R("POST", r"/p/[^/]+/collect", "project", own="path"),
    _R("POST", r"/collect", ("project", "deploy"), admin=True),         # 部署核对也要立即采集（tools/deploy.py 用 deploy 令牌）
]


def route_of(method: str, path: str) -> Route:
    r = next((r for r in ROUTES if r.method == method and r.path.fullmatch(path)), None)
    return r or Route(method, re.compile(re.escape(path)), "read" if method == "GET" else "admin", method != "GET")


PATH_PID = re.compile(r"/(?:p|api/project)/([^/]+)(?:/.*)?")      # own="path"、"new"：地址里的项目 id


def _NO_GUARD(host=None) -> None:
    """管理员、不用按对象核的请求：Handler.guard 什么都不核（#19）。"""


def root_rel(path: str) -> str:
    """从 path 回看板根的相对地址（../ 几层，在根下是 ./）：服务端写的跳转与链接都用它，反代在前面加了路径前缀也不用改（#165）。"""
    return "../" * max(0, path.count("/") - 1) or "./"


def login_url(path: str) -> str:
    """没登录时跳去的登录页：相对地址（root_rel；在根下省掉 ./），next 是相对看板根的原路径（浏览器跳转时带着 #/… 的视图）。"""
    return root_rel(path).removeprefix("./") + "login?next=" + quote(path.lstrip("/"), safe="/")


def safe_next(nxt: str) -> str:
    """登录后回哪：只认看板里的相对路径（不带协议、不以 / 开头、没有 ..），别的回看板根，不当跳转器用。"""
    return nxt if re.fullmatch(r"[A-Za-z0-9_.~%/-]*", nxt or "") and not nxt.startswith("/") and ".." not in nxt else ""


SCHED_ACTS = {"focus": ("focus", True), "unfocus": ("focus", False), "pause": ("pause", True), "resume": ("pause", False)}    # POST /sched/<动作>


def serve(port: int, interval: int | None = None, public_port: int | None = None, legacy: list[str] = ()) -> int:
    """起看板服务：内网入口绑 [server] 的 lan_bind（缺省 127.0.0.1）:port，配了证书就是 HTTPS；给了 public_port 另起公网入口，只绑 127.0.0.1（给隧道接）。
    两个端口都只由命令行给（labmon service 写进托管配置），哪台机器用哪个端口按那台的情况定（#165：dashboard-host 上 8765 被别的程序占着）。
    都要登录（users.py）。项目按登记加载（Server.sync，#161）。[server] 写错、证书读不了、端口占着：记一行日志、退出码 2，不退回明文、不只开一半。
    legacy：可删（#161 迁移）。部署时 launchctl kickstart -k 沿用 launchd 已加载的旧参数，服务带着以前的 -c 起来：没登记的先登记进来（projects.adopt），
    之后 labmon service install 去掉 -c；那次部署做完就删这个参数（连同 __main__ 里传它的那行）。"""
    try:
        conf = server_conf()
        DP.mark_start()
        minimum = conf["min_client_commit"]
        if minimum and minimum not in DI.history(DP.RUNNING["commit"] or ""):
            raise ConfigError("[server] min_client_commit 要是看板当前运行提交或其祖先")
    except (ConfigError, DI.Unavailable) as e:
        log(f"看板服务没起来：{e}")
        return 2
    for c in legacy:
        try:
            note = PJ.adopt(c)
        except Exception as e:  # noqa: BLE001 — 迁移出错只记下，别的项目照常
            note = f"以前用 -c 加的 {c} 没登记进来：{e!r}"
            log_events([{"kind": "project", "msg": note}])
        if note:
            log(note)
    try:                                              # 可删（#19 迁移，users.migrate_people）：部署后主与备用各启动过一次就删这几行
        for note in U.migrate_people():
            log(note)
    except Exception as e:  # noqa: BLE001 — 迁移出错只记下，服务照常起，下次启动再迁
        log(f"账号的人员对应这次没迁（#19，下次启动再迁）：{e!r}")
    try:
        srv = Server((conf["lan_bind"], port), interval=interval, conf=conf)
        pub = Listener(("127.0.0.1", public_port), srv, conf["public_hosts"]) if public_port else None
    except (OSError, ssl.SSLError) as e:
        log(f"看板服务没起来：内网入口 {conf['lan_bind']}:{port}{f' 或公网入口 127.0.0.1:{public_port}' if public_port else ''} 用不了（端口占着，或证书读不了）：{e}")
        return 2
    role = LS.tick()                                  # 先定一次主备再起各线程（#163）：本机标记是主、租约其实在别的看板服务手里时，不先动组内服务器与项目
    srv.sync()
    projects = list(srv.projects.values())
    srv.overview.start("overview")
    threading.Thread(target=srv.fleet.loop, name="fleet", daemon=True).start()
    if HC.enabled():                                  # 看板服务跑在 WSL 里：体检所在的 Windows 机（#166），计划维护期间不推（maint，#165）
        threading.Thread(target=HC.loop, name="hostcheck", daemon=True).start()
    if pub:
        threading.Thread(target=pub.serve_forever, name="public", daemon=True).start()
    rotate_log()
    stopping = threading.Event()

    def bye(signum, _frame):
        """停服务（systemd stop、launchd unload、kill）：等这一轮采集做完、释放服务租约，备用的那台下一轮就接管，不用等 ttl（#163）。
        不放在 ExecStop：那时服务还在采集，释放后这一轮又续上。"""
        if stopping.is_set():
            return
        stopping.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()
        srv.overview.halt(LS.RELEASE_S)
        if LS.primary():
            LS.release()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, bye)
    log(f"服务启动：内网入口 {'https' if srv.secure else 'http'}://{conf['lan_bind']}:{port}/" + (f"，公网入口 127.0.0.1:{public_port}（给隧道接）" if pub else "")
        + f" ；项目 {', '.join(p.id for p in projects)}；{'主' if role == 'primary' else '备用（' + LS.text() + '）'}" + ("" if U.any_user() else "；还没有账号：labmon user person <人员名>，再 labmon user add <账号> --person <人员名> --admin"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        bye(signal.SIGINT, None)
    return 0
