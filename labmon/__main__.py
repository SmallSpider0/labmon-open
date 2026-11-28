"""命令行入口。

会动东西的命令经看板接口由看板服务做（#162，client.py；服务端的做法在 api.py、waits.py）：在哪台电脑上跑都一样，要令牌（labmon login 一次）。
连哪个看板写在 ~/.config/labmon/client.toml（没有时连本机的内网入口），见 docs/api.md。输出与退出码与以前在本机做的一样；
连不上看板服务退出码 2，看板拒绝（没登录、令牌没这个权限、配置不对）退出码 3。

    labmon login [--name 名字] [--admin]                   输口令换一个令牌存进 client.token（--admin：1 小时的 admin 令牌，存 admin.token）
    labmon url <项目 id>                                    看板上这个项目的链接（每个入口一行）
    labmon report "现象" --evidence "证据" [--project ID] [--host ID] [--at 时刻]  把 labmon 问题报给看板
    labmon update                                           换成看板在跑的同一版本（看板发的客户端装的才用；#167）
    labmon collect -c monitor.toml                          让看板立即采集这个项目一次，打出结果与链接
    labmon status  [项目 id] [-c monitor.toml] [--json]     项目现状（各主机与卡、车道、运行中、排队的预计开始、失败、计划项、挪法建议、告警；只读）
    labmon disk    [项目 id | --me] [-c monitor.toml] [--json] 磁盘用量：查项目存储，或 --me（及无 monitor.toml 时）查本人共享盘汇总与总盘余量（只读）
    labmon records [项目 id] [--host H] [--cat C] [-q 词] [-n 30] [--json]   项目的记录（拉起、掉线、续租、失败、临时指令……；只读）
    labmon check   -c monitor.toml [--probe]                 看板校验配置并输出紧凑摘要（--probe 打印完整探针规格 JSON）；末尾列出同步状态与待拉回消息
    labmon wait    -c monitor.toml                          等告警：每轮先与看板上的配置同步，再问看板（长轮询），出现新告警（含借到卡等全局消息）或采集停了即退出（退出码 1）
    labmon wait    -c monitor.toml --job 任务 [--job 项目/任务 …]   同时等这些任务跑完（问看板）：都有结果时退出，全成功 0、其余 4；告警照常退出 1；超过 5 个任务时打汇总并仅列变动/异常项；
                                                            连不上看板不退出、连回来报 server_back；连着 1 小时连不上才以 server_unreachable 告警退出（1）
                                                            一个项目只让一个会话等：已有别的会话在等时拒绝（退出码 3）；--take 改由本会话等（那边下一轮退出）
    labmon host list | return <id> [--at 时间 | --now] | restore <id>  用户级主机清单：查看、标记已归还（借来的：跑完就还）、恢复在用（借来的：撤销跑完就还）；改的要 admin 令牌
    labmon host add <id> --ssh user@host [--port 22] [--platform other|none] [--label 名称] [--until 到期] [--data-root 目录] [--disk-warn 20] [--disk-stop 10] [--disk-quota N]
    labmon host edit|test|rm|key <id> [...]                 外部主机双入口：增删改查、连通测试与专用公钥查看/轮换（零私钥上传）
    labmon borrow [<组内服务器> [--until "明天 18:30"] [--for a1,b] [--away]]   借来给自己的项目跑（见 borrow.py；--away 对方说好约定前不用；要 admin 令牌）；不给参数列出在借的
    labmon borrow <自用主机> --until … [--from 名字]             换给自己借来的：记下约定还的时间（--until none 去掉）
    labmon worker start|stop|status <id|pool[:<池id>]> | add|rm <id|pool[:<池id>]> ROOT JOBS [--gpus 0] [--project P] [--stop term] | limit <id> tag=N
    labmon worker clean <id> [--project P] [--cache] [--scratch] [--dry-run] [--yes]   仅清理本地自管缓存与已结束临时目录（默认预览，绝不动 projects/ 产物）
    labmon worker fit <id> 任务名… [--project P]               开并行车道前：放不放得下（与真放行同一份准入）、卡吃不满是不是 CPU 受限
    labmon worker fit <id|pool[:<池id>]> ROOT JOBS --list --project P  一条清单还会跑的行此刻能在共用 worker 目录（或该共有池）的几台上再开几条车道
                                                            服务器端队列执行器（见 workerctl.py；start、stop、limit 要 admin 令牌）
    labmon pause|resume|focus|unfocus <项目> [--host id] [--until 11-10|+3h] [--now]  临时指令（项目的不带 --host 记在主机清单、所有主机生效；--now 只用于 pause）
    labmon drain|undrain <主机> [--until …]                  这台主机不再放行新任务（要 admin 令牌）
    labmon project init <主机> [项目名]                     按共享盘目录规范建目录（给了项目名连 projects/<项目名>/ 一起建），列出可共用的环境与数据集
    labmon project push|pull [-c monitor.toml]               把项目目录里的配置推到看板上（第一次推就是登记）/ 拉回看板上的（页面上改的；见 projsync.py）
    labmon project push --admin [-c monitor.toml]            改了 [[hosts]]、[[queues]] 时：用 labmon login --admin 建的管理员令牌推
    labmon project archive|restore <项目 id>                 项目做完了收起（要求暂时没实验；移除登记、撤权重与指令、看板不再采集，不删数据）/ 加回（见 projects.py）

只在看板服务那台机器上用的（管的是那台机器上的状态与秘密，不经接口）：

    labmon serve   [--port P] [--public-port Q]             看板服务：按登记的项目定时采集 + HTTP，要登录（见 serve.py；Q 是给隧道接的公网入口）
    labmon service install|status|restart|stop              用 launchd（macOS）或 systemd 用户单元（Linux）托管 serve（见 service.py）
    labmon render  -c monitor.toml [--out DIR]              只用上次的 snapshot.json 重新渲染页面（改页面时用）
    labmon user person <人员名> | add <账号> --person <人员名> [--admin] | reset <名字> | remove <名字> | list | token <名字> --scope S --out 文件   看板账号（见 users.py；在看板这台机器上直接改，
                                                            只认服务的账号本人；加人与重置打出设置码，令牌只写进文件。别的账号操作在页面「组内服务器 → 人员」）
    labmon fleet [status [id]] | probe <id>                  组内服务器（只读监控，见 fleet.py）：各台现状；立即探一台并列出 GPU 进程与归类
    labmon fleet boot [id] | import <id> <Json 文件>         组内服务器的开机参数：查看；导入网页作业详情里复制的 Json 文件
    labmon secret set|rm platform [--user 账号]               平台密码：在看板服务这台机器上放一次（macOS 钥匙串，Linux 600 权限的文件；输入不回显，见 platform.py）
    labmon notify test [文字]                               照 ~/.config/labmon/notify.toml 发一条试推到手机（Bark 或 ntfy，见 service.py）
    labmon hostcheck                                        看板服务跑在 WSL 里时：体检所在的 Windows 机（待装更新、待重启、开机时长、内存、隧道；见 hostcheck.py）
    labmon server maint [30m | 2h | 18:30 | 明天 9:00 | off]  看板服务的计划维护：期间 TX 的看门狗与体检不推提醒（最长 1 天；不给参数看现在；见 maint.py）
    labmon server lease [--take | --release]                服务租约：这台看板服务是主还是备用；强制拿到（租约文件写坏了时）；释放（见 lease.py）
    labmon server restore <备份> [--port P] [--public-port Q]   从冷备恢复成这台的看板服务（原来的状态挪到旁边，见 backup.py）
    labmon backup make [--out 文件] | pull --to 看板地址 [--ca 证书] | schedule --to … [--ca …] | list   冷备：在看板这台打包；笔记本经 API 取、留 14 份、每天定时取
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

from . import backup as BK
from . import client, dist
from . import collect as C
from . import fleet as F
from . import fleetops as FO
from . import fleetview as FV
from . import lease
from . import maint as MT
from . import projects as PJ
from . import projsync as PS
from . import service as S
from . import users as U
from .api import OVR, WORKER_ACTS, borrow_text
from .config import PORT, ConfigError, brief, check_future, fmt_time, parse_time, stale_limit
from .platform import PlatformError
from .serve import serve
from .state import load_json, write_atomic
from .waits import JOB_BAD_RC, HOLD_MAX

GAP = 0              # wait：一轮问下来没有消息时隔多久再问（长轮询已在看板那边等过，0；测试改它的 sleep 推进场景）


def _summary(snap: dict, where: str) -> None:
    hosts = "，".join(f"{h['label']}{'正常' if h['ok'] else '失败：' + str(h['error'])}" for h in snap["hosts"])
    print(f"[labmon] {fmt_time(snap['generated'])} {hosts}；告警 {len(snap['alerts'])} 条；页面 {where}", flush=True)
    for a in snap["alerts"]:
        print("  告警：" + a["msg"], flush=True)


def _call(method: str, path: str, body=None, **kw) -> dict | int:
    """一次性命令的一个请求：成了回 JSON；看板拒绝打原因回 3，连不上回 2（调用方原样返回这个退出码）。"""
    try:
        return client.call(method, path, body, **kw)
    except client.Failed as e:
        print(f"[labmon] {e.msg}" + (f"；{client.ADMIN_HINT}" if kw.get("admin") and e.code == 403 else ""), file=sys.stderr)
        return 3
    except client.Unreachable as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 2


def cmd_host(argv: list[str]) -> int:
    """用户级主机清单与外部主机管理：
    - list / return / restore（查看、标记已归还与恢复在用）；
    - add / edit / test / rm / key（外部主机双入口，走 /api/hosts/external*，零私钥上传）。"""
    sub = argv[0] if argv and not argv[0].startswith("-") else "list"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv
    if sub == "list":
        ap = argparse.ArgumentParser(prog="labmon host list", description="查看用户级主机清单")
        ap.parse_args(rest)
        r = _call("GET", "/api/hosts")
        if isinstance(r, int):
            return r
        if r.get("missing"):
            print(f"[labmon] 没有用户级主机清单 {r['missing']}")
            return 1
        for h in r["hosts"]:
            st = f"已归还 {h['returned']}" if h.get("returned") else "在用"
            extra = borrow_text(h.get("borrow"))
            if h.get("standalone"):
                tag = "外部机" + (f" · {h['owner']}" if h.get("owner") else "")
                extra = f"{tag}{' ' + extra if extra else ''}"
            if not h.get("returned") and h.get("lease_end"):
                lt = f"租期到 {fmt_time(h['lease_end'])}"
                extra = f"{extra}  {lt}" if extra else lt
            ptag = f"  [共有池 {h['pool_host_id']}{'·独占' if h.get('pool_exclusive') else ''}]" if h.get("pool_host_id") else ""
            print(f"  {h['id']:<10} {st:<22} {h.get('ssh') or '本机'}:{h.get('port', 22)}  {h.get('label') or ''}  {extra}{ptag}".rstrip())
        groups: dict = {}                   # 共用 worker 目录的在用主机按目录分组（#35 A；旧看板没有 home_with 时不打）
        for h in r["hosts"]:
            if h.get("home_with") and not h.get("returned"):
                groups.setdefault(h.get("worker_home"), {str(h["id"])}).update(h["home_with"])
        for home, ids in groups.items():
            order = [str(h["id"]) for h in r["hosts"] if str(h["id"]) in ids]
            print(f"  共用 worker 目录 {home}（同一块共享盘，放在这块盘上的文件各台都读得到）：{'、'.join(order)}")
        for p in r.get("pools") or []:
            mode = "独占，仅经池入口提交" if p.get("exclusive", True) else "开放，允许直投单台"
            hs = "、".join(p.get("active_hosts") or []) or "暂无在用主机"
            print(f"  共有算力池 {p['host_id']}（{p.get('label') or p['id']}，{mode}）：主机 {hs}")
        return 0
    if sub in ("return", "restore"):
        ap = argparse.ArgumentParser(prog=f"labmon host {sub}")
        ap.add_argument("id", help="主机 id")
        if sub == "return":
            ap.add_argument("--at", help="归还时间（缺省现在）：2026-11-04 18:40、11-04 18:40、18:40、明天 9:00")
            ap.add_argument("--now", action="store_true", help="借来的组内服务器：正在跑的按各自的停法停下，不等跑完")
        a = ap.parse_args(rest)
        at = None
        if sub == "return" and a.at:
            at = parse_time(a.at, rel=True)
            if at is None:
                print(f"[labmon] 看不懂归还时间：{a.at}（用 2026-11-04 18:40、11-04 18:40、18:40 或 明天 9:00）", file=sys.stderr)
                return 3
        return client.run("POST", f"/api/host/{sub}", {"id": a.id, "at": at, "now": bool(getattr(a, "now", False))}, admin=True, ap=ap)
    if sub in ("add", "edit"):
        ap = argparse.ArgumentParser(prog=f"labmon host {sub}", description="新增或修改外部主机（零私钥上传，走看板专用 Ed25519 公钥）")
        ap.add_argument("id", help="主机 id（小写字母/数字/-/_，如 ext-4090）")
        ap.add_argument("--ssh", help="SSH 地址，如 user@203.0.113.10（管理员留空表示看板本机）")
        ap.add_argument("--port", type=int, help="SSH 端口（缺省 22）")
        ap.add_argument("--label", help="页面显示名称")
        ap.add_argument("--platform", dest="platform_kind", choices=["other", "none"], help="平台分类：other（外租平台）或 none（自有机器）")
        ap.add_argument("--key-mode", dest="ssh_key", choices=["managed", "system"], help="密钥模式：managed（看板专用密钥，缺省）或 system（仅管理员）")
        ap.add_argument("--owner", help="归属看板账号（仅管理员可代指）")
        ap.add_argument("--until", dest="lease_until", help="到期时间（外租平台提醒用，空串清除）")
        ap.add_argument("--data-root", dest="data_root", help="独立工作根目录（缺省 ~/.local/share/labmon/work）")
        ap.add_argument("--python", help="远端 python 路径（缺省 python3）")
        ap.add_argument("--disk-warn", dest="disk_warn_gb", type=float, help="磁盘低水位预警线 GiB（缺省 20）")
        ap.add_argument("--disk-stop", dest="disk_stop_gb", type=float, help="磁盘准入停止线 GiB（缺省 10，0 关闭）")
        ap.add_argument("--disk-quota", dest="disk_quota_gb", type=float, help="工作目录软配额 GiB")
        ap.add_argument("--cpu-quota", dest="cpu_quota", type=float, help="允许占用的 CPU 核数上限")
        ap.add_argument("--mps-vram", dest="mps_vram_gb", type=float, help="Apple Silicon 统一内存显存上限 GiB")
        ap.add_argument("--allow-shared-gpu", dest="allow_shared_gpu", action=argparse.BooleanOptionalAction, default=None,
                        help="检测到宿主机等外部活跃占卡时，允许按剩余显存共享放行而非默认整卡暂停（可用 --no-allow-shared-gpu 关闭）")
        ap.add_argument("--admin", action="store_true", help="使用管理员令牌（管理员操作或内网管理他人/本机外部主机时使用）")
        a = ap.parse_args(rest)
        body: dict = {}
        if sub == "add":
            body["id"] = a.id
            if a.ssh is not None:
                body["ssh"] = a.ssh
            elif not a.admin:
                ap.error("labmon host add 需要 --ssh user@host")
        elif a.ssh is not None:
            body["ssh"] = a.ssh
        for k in ("port", "label", "platform_kind", "ssh_key", "owner", "lease_until", "data_root", "python",
                  "disk_warn_gb", "disk_stop_gb", "disk_quota_gb", "cpu_quota", "mps_vram_gb", "allow_shared_gpu"):
            v = getattr(a, k)
            if v is not None:
                body[k] = v
        path = "/api/hosts/external" if sub == "add" else f"/api/hosts/external/{quote(a.id, safe='')}"
        r = _call("POST" if sub == "add" else "PUT", path, body, admin=a.admin)
        if isinstance(r, int):
            return r
        print(f"[labmon] {r.get('msg') or '已保存'}")
        if r.get("pubkey"):
            print(f"  公钥指纹：{r.get('fingerprint') or '-'}")
            print(f"  专用公钥：{r['pubkey']}")
            print(f"  目标机一行授权命令：\n    {r.get('auth_cmd')}")
        return 0
    if sub == "test":
        ap = argparse.ArgumentParser(prog="labmon host test", description="单次测试外部主机 SSH 连通性、GPU、Python 与工作目录")
        ap.add_argument("id", help="外部主机 id")
        ap.add_argument("--admin", action="store_true", help="使用管理员令牌")
        a = ap.parse_args(rest)
        r = _call("POST", f"/api/hosts/external/{quote(a.id, safe='')}/test", {}, admin=a.admin, timeout=30)
        if isinstance(r, int):
            return r
        if r.get("ok"):
            print(f"[labmon] {r.get('msg') or '连通正常'}")
            return 0
        print(f"[labmon] 连通测试失败：{r.get('error') or '未知错误'}", file=sys.stderr)
        if r.get("hint"):
            print(f"  建议：{r['hint']}", file=sys.stderr)
        return 1
    if sub == "key":
        ap = argparse.ArgumentParser(prog="labmon host key", description="查看或轮换外部主机专用 Ed25519 公钥（零私钥上传）")
        ap.add_argument("id", help="外部主机 id")
        ap.add_argument("--rotate", action="store_true", help="轮换生成新密钥对（旧连接将被断开，需重新授权公钥）")
        ap.add_argument("--admin", action="store_true", help="使用管理员令牌")
        a = ap.parse_args(rest)
        r = _call("POST", f"/api/hosts/external/{quote(a.id, safe='')}/key", {"rotate": a.rotate}, admin=a.admin)
        if isinstance(r, int):
            return r
        print(f"[labmon] {r.get('msg') or '专用公钥'}")
        print(f"  公钥指纹：{r.get('fingerprint') or '-'}")
        print(f"  专用公钥：{r.get('pubkey')}")
        print(f"  目标机一行授权命令：\n    {r.get('auth_cmd')}")
        return 0
    if sub == "rm":
        ap = argparse.ArgumentParser(prog="labmon host rm", description="删除外部主机并销毁看板专用私钥")
        ap.add_argument("id", help="外部主机 id")
        ap.add_argument("--force", action="store_true", help="强制删除（即使仍有在跑或排队任务）")
        ap.add_argument("--admin", action="store_true", help="使用管理员令牌")
        a = ap.parse_args(rest)
        r = _call("DELETE", f"/api/hosts/external/{quote(a.id, safe='')}", {"force": a.force}, admin=a.admin)
        if isinstance(r, int):
            return r
        print(f"[labmon] {r.get('msg') or '已删除'}")
        return 0
    print("用法：labmon host list | return <id> [--at 时间 | --now] | restore <id> | add|edit|test|key|rm <id> ...", file=sys.stderr)
    return 2


def cmd_borrow(argv: list[str]) -> int:
    """借来用：组内服务器（直接借，拉起 worker）或换给自己的自用主机（只记约定时间），经 POST /api/borrow（api.borrow，要 admin 令牌）；不给参数列出在借的。"""
    ap = argparse.ArgumentParser(prog="labmon borrow")
    ap.add_argument("id", nargs="?", help="组内服务器 id（如 27-15152），或换给自己借来的自用主机 id")
    ap.add_argument("--until", help="约定还的时间，精确到分：11-08 18:30、明天 9:00、21:30（今天）、+90m、+6h、+2d；none 去掉（缺省不约定，只按对方租期到期前 1 小时截止）")
    ap.add_argument("--for", dest="projects", help="只给这些项目用（逗号分隔，缺省自己的项目都能用）")
    ap.add_argument("--from", dest="holder", help="换给自己借来的：原使用人")
    ap.add_argument("--away", action="store_true", help="对方说好约定时间前不用：不给对方留余量（要有 --until；改约定时不写就去掉）")
    a = ap.parse_args(argv)
    if not a.id:
        r = _call("GET", "/api/hosts")
        if isinstance(r, int):
            return r
        rows = [h for h in r["hosts"] if h.get("borrow") and not h.get("returned")]
        for h in rows:
            print(f"  {h['id']:<10} {h.get('ssh')}:{h.get('port')}  {borrow_text(h['borrow'])}")
        print(f"[labmon] 在借 {len(rows)} 台" if rows else "[labmon] 没有在借的主机")
        return 0
    until = None if (a.until or "").lower() == "none" else parse_until(a.until)
    return client.run("POST", "/api/borrow", {"id": a.id, "until": until, "for": a.projects, "from": a.holder, "away": a.away}, admin=True, ap=ap)



def cmd_user(argv: list[str]) -> int:
    """labmon user：看板账号（#160，users.py）。在看板服务这台机器上直接改 users.json，只认服务的账号本人（users.local_ok）：
    建第一个管理员、忘了口令时重置、给脚本建令牌。别的账号操作（加人、改角色、吊销）在页面「组内服务器 → 人员」。"""
    ap = argparse.ArgumentParser(prog="labmon user", description="看板账号：在看板服务这台机器上直接改，只认服务的账号本人")
    sub = ap.add_subparsers(dest="act", required=True)
    p = sub.add_parser("add", help="加账号，打出设置码（24 小时有效）：打开看板登录页「用设置码设口令」")
    p.add_argument("name")
    p.add_argument("--admin", action="store_true", help="管理员（还没有管理员时第一个账号必须是）")
    p.add_argument("--person", required=True, help="已登记的组内人员")
    p = sub.add_parser("person", help="在本机先登记组内人员，服务器设置可稍后填写")
    p.add_argument("name")
    p = sub.add_parser("bind", help="给未关联的存量账号补关联已登记的组内人员")
    p.add_argument("name")
    p.add_argument("person")
    p = sub.add_parser("reset", help="重置口令（忘了口令时用），打出新的设置码；这人已登录的浏览器都要重新登录，令牌一并吊销")
    p.add_argument("name")
    p.add_argument("--keep-tokens", action="store_true", help="令牌留着（只是忘了口令、令牌没泄露时：部署令牌与实验会话照常用）")
    p = sub.add_parser("remove", help="删账号：连同它的会话与令牌（最后一个管理员删不掉）")
    p.add_argument("name")
    p = sub.add_parser("public-ops", help="设置管理员公网平台操作")
    p.add_argument("name")
    p.add_argument("mode", choices=("on", "off"))
    sub.add_parser("list", help="列出账号")
    p = sub.add_parser("token", help="给这人建一个令牌：原文只写进 --out 文件（权限 600），不打到终端")
    p.add_argument("name")
    p.add_argument("--scope", required=True, help="作用域，逗号分隔：" + "、".join(f"{k}（{v}）" for k, v in U.SCOPES.items()) + "；admin 1 小时过期")
    p.add_argument("--name", dest="label", default="命令行", help="令牌的名称（「我的账号」里看得到，缺省“命令行”）")
    p.add_argument("--out", required=True, help="写到哪个文件（如部署脚本读的 ~/.config/labmon/deploy.token）")
    a = ap.parse_args(argv)
    try:
        U.local_ok()
        if a.act == "person":
            from . import fleetboot
            fleetboot.set_person(a.name, create=True, roster_only=True)
            print(f"[labmon] 已登记组内人员 {a.name}")
        elif a.act == "bind":
            print(f"[labmon] {U.bind_person(a.name, a.person, '本机命令行')}")
        elif a.act in ("add", "reset"):
            code = U.add(a.name, "admin" if a.admin else "user", "本机命令行", person=a.person) if a.act == "add" else U.reset(a.name, "本机命令行", self_ok=True, keep_tokens=a.keep_tokens)
            print(f"[labmon] {'加了' if a.act == 'add' else '重置了'} {a.name}。设置码 {code}（24 小时内有效，用一次作废）：\n"
                  f"  打开看板 http://127.0.0.1:{PORT}/ → 登录页「用设置码设口令」，填名字、设置码与新口令（至少 {U.PW_MIN} 个字符）")
        elif a.act == "remove":
            print(f"[labmon] {U.remove(a.name, '本机命令行')}")
        elif a.act == "public-ops":
            print(U.set_public_ops(a.name, a.mode == "on", "本机命令行"))
        elif a.act == "list":
            L = U.listing()
            for u in L["users"]:
                st = "口令已设" if u["pw"] else f"等设置码（到 {fmt_time(u['setup'])}）" if u["setup"] else "没有口令"
                print(f"  {u['name']:<16} {U.ROLES.get(u['role'], u['role'])}  {st}  会话 {len(u['sessions'])} 个  令牌 {len(u['tokens'])} 个"
                      + (f"  最后用 {fmt_time(u['seen'])}" if u["seen"] else "") + (f"  锁到 {fmt_time(u['lock'])}" if u["lock"] else ""))
            if not L["users"]:
                print("[labmon] 还没有账号：labmon user person <人员名>，再 labmon user add <账号> --person <人员名> --admin")
        else:
            raw, tok = U.token_add(a.name, a.label, [x.strip() for x in a.scope.split(",") if x.strip()])
            out = Path(a.out).expanduser()
            write_atomic(out, raw + "\n", mode=0o600)
            print(f"[labmon] 建了 {a.name} 的令牌「{tok['name']}」（{'、'.join(tok['scopes'])}"
                  + (f"，{fmt_time(tok['until'])} 过期" if tok["until"] else "") + f"），原文写在 {out}（权限 600）；在看板「我的账号」里能吊销")
    except U.Denied as e:
        print(f"[labmon] {e.msg}", file=sys.stderr)
        return 3
    except (PlatformError, ConfigError) as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 3
    return 0


def cmd_server(argv: list[str]) -> int:
    """labmon server maint：看板服务的计划维护（#165，maint.py）。装更新、重启常驻机之前开，期间 TX 的看门狗（经 /healthz 的 maint_until）与体检不推。
    labmon server lease：服务租约（#163，lease.py）；labmon server restore：从冷备恢复成这台的看板服务（backup.restore）。"""
    if argv[:1] == ["lease"]:
        return lease.cli(argv[1:])
    ap = argparse.ArgumentParser(prog="labmon server", description="看板服务这台机器上的事")
    sub = ap.add_subparsers(dest="act", required=True)
    p = sub.add_parser("maint", help="计划维护：期间 TX 的看门狗与所在机的体检不推提醒；到期自动结束")
    p.add_argument("until", nargs="?", help="开到何时：30m、2h、+30m、18:30、明天 9:00（最长 1 天）；off 提前结束；不给看现在")
    sub.add_parser("lease", help="服务租约：这台是主还是备用（--take 强制拿到，--release 释放；见 labmon server lease --help）")
    p = sub.add_parser("restore", help="从冷备恢复成这台的看板服务：状态根与主机清单换成备份里的，原来的挪到旁边，再装服务")
    p.add_argument("archive", help="备份文件（labmon backup make / pull 得到的 .tar.gz）")
    p.add_argument("--port", type=int, help="内网入口端口（缺省 8765）")
    p.add_argument("--public-port", type=int, help="公网入口端口（给隧道接；不给就不开）")
    a = ap.parse_args(argv)
    if a.act == "restore":
        try:
            print(f"[labmon] {BK.restore(a.archive)}", flush=True)
        except ConfigError as e:
            print(f"[labmon] {e}", file=sys.stderr)
            return 3
        return BK.install_service(a.port, a.public_port)
    try:
        if not a.until:
            print(f"[labmon] {MT.show()}")
        elif a.until == "off":
            print(f"[labmon] {MT.stop('本机命令行')}")
        else:
            w = a.until.strip()
            end = parse_until("+" + w if re.fullmatch(r"\d+(?:\.\d+)?[mhd]", w) else w)     # 30m、2h 不带 + 也认
            print(f"[labmon] {MT.start(end, '本机命令行')}")
    except ConfigError as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 3
    return 0


def cmd_secret(argv: list[str]) -> int:
    """labmon secret：看板服务这台机器上的秘密（#164，platform.secret）：平台密码由维护者放一次。macOS 存钥匙串，Linux 存 600 权限的文件；
    输入不回显、不进命令行参数，原文不打印、不写日志。账号缺省取主机清单里的平台账号（fleetops.platform_conf）。"""
    ap = argparse.ArgumentParser(prog="labmon secret", description="平台密码：在看板服务这台机器上放一次（macOS 钥匙串，Linux 600 权限的文件）")
    ap.add_argument("act", choices=["set", "rm"], help="set 输入并存起来（覆盖旧的）；rm 删掉（连同存着的平台登录令牌）")
    ap.add_argument("kind", choices=["platform"], help="platform：平台密码")
    ap.add_argument("--user", help="平台账号（缺省取主机清单 [fleet] 或自用主机的 platform_user）")
    a = ap.parse_args(argv)
    from . import platform as P
    try:
        user = a.user or FO.platform_conf(F.conf() or {})[1]
        if a.act == "set":
            print(f"[labmon] {P.prompt_secret(a.kind, user)}")
        else:
            P.del_secret(a.kind, user)
            P.drop_session(user)
            print(f"[labmon] 删了平台账号 {user} 存着的密码与登录令牌")
    except (PlatformError, ConfigError) as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 3
    except (KeyboardInterrupt, EOFError):
        print("\n[labmon] 取消了，什么都没存", file=sys.stderr)
        return 3
    return 0


def parse_until(s: str | None) -> float | None:
    """命令行写的到期时刻（借用的约定、临时指令的到期）：MM-DD（当天结束）、MM-DD HH:MM、YYYY-MM-DD[ HH:MM]、今天 / 明天 / 后天 HH:MM、
    只写 HH:MM（今天；以上经 config.parse_time，不写年份的早于现在半年以上才算明年：12 月底写 01-05）、+30m、+3h、+2d。已经过去的拒绝。"""
    if not s:
        return None
    s = s.strip()
    m = re.fullmatch(r"\+(\d+(?:\.\d+)?)([mhd])", s)
    ts = time.time() + float(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]] if m else parse_time(s, future=True, rel=True)
    if ts is None:
        raise SystemExit(f"[labmon] 看不懂到期时间：{s}（用 11-10、11-10 18:00、明天 18:00、+30m、+3h 或 +2d）")
    try:
        check_future(ts)
    except ConfigError as e:
        raise SystemExit(f"[labmon] {e}")
    return ts


def cmd_override(argv: list[str]) -> int:
    """临时指令经 POST /api/override/<动作>（api.override）；drain、undrain 要 admin 令牌；focus、unfocus 只给管理员，普通用户的 resume 只撤自己下的暂停（#16）。"""
    ap = argparse.ArgumentParser(prog=f"labmon {argv[0]}")
    ap.add_argument("target", help="项目名（与 worker 里的项目名一致，见工作台）或主机 id（drain）")
    ap.add_argument("--host", action="append", help="只对这台主机（缺省：所有主机，含之后加入的：记在主机清单的 [sched.pause] / [sched.focus]）")
    ap.add_argument("--until", help="到期：11-10（当天结束）、11-10 18:30、明天 9:00、+30m、+3h、+2d；缺省无期限")
    ap.add_argument("--now", action="store_true", help="pause：立即按各自的停法停下正在跑的")
    a = ap.parse_args(argv[1:])
    kind = OVR[argv[0]][1]
    return client.run("POST", f"/api/override/{argv[0]}", {"target": a.target, "hosts": a.host, "until": parse_until(a.until), "now": a.now},
                      admin=kind == "drain", ap=ap)


def _sync(cfg_path: str) -> dict:
    """wait 每轮与看板上的配置同步（projsync.sync）；本地配置读不了、写不了、看板不收时当一条推不上报（键带那句话的哈希，同一个错只报一次）。
    令牌不认（401、403）的 act 是 denied；连不上看板抛 client.Unreachable。"""
    try:
        return PS.sync(cfg_path, patience=0)
    except PJ.Refused as e:
        if e.code in (401, 403):
            return {"act": "denied", "msg": e.msg}
        msg = f"项目目录里的 {cfg_path} 与看板同步不了：{e}（改好后 wait 下一轮自动接着同步）"
        return {"act": "error", "code": e.code, "key": f"sync_error:{PJ.ver_of(msg)}", "msg": msg}
    except (ConfigError, OSError) as e:
        msg = f"项目目录里的 {cfg_path} 与看板同步不了：{e}（改好后 wait 下一轮自动接着同步）"
        return {"act": "error", "code": 400, "key": f"sync_error:{PJ.ver_of(msg)}", "msg": msg}


WAIT_RESUME = "_LABMON_WAIT_RESUME"
WAIT_UPDATE_RETRY_S = 300
WAIT_OFFLINE_MAX_S = 3600   # wait 连着连不上看板这么久才退出一次（#154）；过了 stale_limit 只提示一次、接着退避重试
WAIT_JOB_BRIEF_N = 5        # wait --job 超过这么多任务时改打单行汇总 + 仅列变动/异常项（#200）
_WAIT_JOB_ST = (("queued", "排队"), ("running", "运行"), ("done", "成功"), ("failed", "失败"),
                ("blocked", "卡住"), ("missing", "找不到"), ("voided", "作废"))
_WAIT_JOB_BAD = frozenset({"blocked", "failed", "missing", "voided"})


def _jobs_tally(st: list[dict]) -> str:
    """批量任务的单行状态计数（如：95 排队、4 运行、1 成功）。"""
    cnt: dict[str, int] = {}
    for x in st:
        k = x.get("st") or ""
        cnt[k] = cnt.get(k, 0) + 1
    known = {k for k, _ in _WAIT_JOB_ST}
    parts = [f"{cnt[k]} {label}" for k, label in _WAIT_JOB_ST if cnt.get(k)]
    other = sum(v for k, v in cnt.items() if k not in known)
    if other:
        parts.append(f"{other} 其他")
    return "、".join(parts) or "无"


def _print_wait_jobs(st: list[dict], shown_st: dict[str, str | None], *, first: bool = False, done: bool = False) -> dict[str, str | None]:
    """打印 wait --job 的任务状态：任务数 <= 5 逐行打印；> 5 时打单行汇总并按场景只逐行列变动或异常项。"""
    if len(st) <= WAIT_JOB_BRIEF_N:
        if first:
            print(f"[labmon] 同时等 {len(st)} 个任务跑完（问看板）：", flush=True)
        else:
            print(f"[labmon] {fmt_time(time.time())} " + ("等的任务都有结果了：" if done else "等的任务："), flush=True)
        for x in st:
            print(f"  - {x['name']}：{x['text']}", flush=True)
        return {x["name"]: x.get("st") for x in st}
    if first:
        print(f"[labmon] 同时等 {len(st)} 个任务跑完（问看板）：{_jobs_tally(st)}", flush=True)
        for x in st:
            if x.get("st") in _WAIT_JOB_BAD:
                print(f"  - {x['name']}：{x['text']}", flush=True)
    elif done:
        if all(x.get("st") == "done" for x in st):
            print(f"[labmon] {fmt_time(time.time())} 等的任务都有结果了：{len(st)} 个任务全部成功", flush=True)
        else:
            print(f"[labmon] {fmt_time(time.time())} 等的任务都有结果了（{len(st)} 个）：{_jobs_tally(st)}", flush=True)
            for x in st:
                if x.get("st") != "done":
                    print(f"  - {x['name']}：{x['text']}", flush=True)
    else:
        print(f"[labmon] {fmt_time(time.time())} 等的任务（{len(st)} 个）：{_jobs_tally(st)}", flush=True)
        for x in st:
            cur = x.get("st")
            if cur in _WAIT_JOB_BAD or (shown_st and shown_st.get(x["name"]) != cur):
                print(f"  - {x['name']}：{x['text']}", flush=True)
    return {x["name"]: x.get("st") for x in st}


def _wait_resume(cfg_path: str, jobs: list[str]) -> dict:
    """exec 后一次性取回上轮状态；只认同一配置与同一任务列表。"""
    try:
        saved = json.loads(os.environ.pop(WAIT_RESUME, "{}"))
    except ValueError:
        return {}
    if not isinstance(saved, dict) or saved.get("config") != cfg_path or saved.get("jobs") != jobs:
        return {}
    return saved


def _wait_update(commit: str | None, cfg_path: str, jobs: list[str], watch: dict | None, take: bool = False, wid: str | None = None) -> str | None:
    """装好才 exec（不返回）。commit：要装的版本（刚收到的看板回话给的；None 时取看板发的那份）。take：--take 还没生效（还没收到过回话），新版本接着带上（#142）。
    wid：普通 wait 的实例标识，exec 后原样带回（#165）。已是看板的版本返回 None；更新失败返回原因，由调用处决定继续等（可更新时）还是退出（426）。"""
    try:
        got, inst = client.install_update(commit, auto=True)
        if inst is None:
            return None
        executable = inst.fixed.resolve()
        if executable.parent.parent.name != got:
            raise dist.Unavailable("另一条命令已换了客户端版本，等下一轮再试")
        env = dict(os.environ)
        resume = {"config": cfg_path, "jobs": jobs, "watch": watch}
        if wid:
            resume["wid"] = wid
        if (dist.skill_of(dist.HERE) / "SKILL.md").is_file():         # 新版本拿它与自己那份比，skill 改了就叫醒会话（_skill_woke，#57）
            resume["skill"] = [str(dist.skill_of(dist.HERE)), dist.version()]
        env[WAIT_RESUME] = json.dumps(resume, separators=(",", ":"))
        args = [str(inst.fixed), "wait", "-c", cfg_path]
        for job in jobs:
            args.append(f"--job={job}")
        if take:
            args.append("--take")
        print(f"[labmon] 已安装 {dist.short(got)}，准备继续等待", flush=True)
        os.execve(executable, args, env)
    except (client.Failed, client.Unreachable, dist.Unavailable, OSError, subprocess.SubprocessError) as e:
        return str(getattr(e, "msg", None) or e)
    return None


def _skill_woke(saved: dict) -> str | None:
    """wait 换了版本、exec 起来的这一轮：上一版带来的 skill 与这一版的不同时回那一句（叫醒会话一次，#57）。"""
    old = saved.get("skill")
    if not (isinstance(old, list) and len(old) == 2 and all(isinstance(x, str) for x in old)):
        return None
    changes = dist.skill_changes(Path(old[0]), dist.skill_of(dist.HERE))
    return dist.skill_note(changes, old[1], dist.version(), "先重挂 labmon wait") if changes else None


def _wait_outdated(msg: str, commit: str | None, cfg_path: str, jobs: list[str], watch: dict | None, take: bool = False, wid: str | None = None) -> int:
    """wait 被拦下（426，低于最低版本）：看板安装的客户端先自动更新一次（装好就 exec 接着等），装不成或不是看板装的以 5 退出（#17）。"""
    print(f"[labmon] [{client.CLIENT_OUTDATED_REASON}] {msg}", file=sys.stderr, flush=True)
    if dist.installed():
        err = _wait_update(commit if isinstance(commit, str) else None, cfg_path, jobs, watch, take, wid)
        if err:
            print(f"[labmon] 自动更新不成：{err}；跑 labmon update 后再挂等待", file=sys.stderr, flush=True)
    return client.CLIENT_OUTDATED_RC


def cmd_wait(cfg_path: str, jobs: list[str] | None = None, take: bool = False) -> int:
    """等告警；给了 jobs 时同时等这些任务跑完。判定在看板那边（waits.wait，POST /api/wait 长轮询）：有新告警或等的任务都有了结果就回，
    否则看板那边挂着等（最多 HOLD_MAX 秒）。告警照常唤醒（退出 1，另列各任务此刻的状态；同一轮任务也都有了结果时照样 1），
    任务都有了结果、没有新告警时退出：全部成功 0，否则 JOB_BAD_RC。
    每轮先与看板上的配置同步（_sync：推了、拉了打一行；两边都改了、推不上当告警报一次；看板上没登记的、第一轮配置就不对的退出 3）。
    连不上看板退避重试，不退出（#154）：连续连不上超过 config.stale_limit（按本地 monitor.toml 的采集间隔与 stale_min，与 collector_stale 同一门槛）往 stderr 提示一次，
    连回来的那一轮多报一条 server_back（退出 1）；连着满 WAIT_OFFLINE_MAX_S 才以 server_unreachable 告警退出 1。
    回话丢在半路时带同一个请求号重试（看板回上次那份，已记成报过的告警不会漏）；等的任务的内部状态（watch）每轮带回看板。
    每轮带会话标识（client.session_id）：一个项目只让一个会话等，别的会话已在等时看板回 409、以 3 退出；take（--take）只在第一轮带上，改由本会话等（#142）。
    普通 wait（无 --job）另带实例标识 wid（自动更新与重连保持不变）：同一会话新起的普通 wait 直接接替，旧的下一轮回 409、以 3 退出（#165）。"""
    try:
        b = brief(cfg_path)
    except ConfigError as e:
        print(f"[labmon] 配置错误：{e}", file=sys.stderr)
        return 3
    limit = stale_limit(b["interval"], b["stale_min"])
    jobs = jobs or []
    saved = _wait_resume(cfg_path, jobs)
    if saved:
        dist.ensure_skill()                            # 旧版本换过来的那次没建 skill 入口：补上
        if msg := _skill_woke(saved):
            print(f"[labmon] {fmt_time(time.time())} 新告警 1 条", flush=True)
            print(f"  - [skill_updated] {msg}", flush=True)
            return 1
    else:
        print(f"[labmon] 等告警：每轮先同步一次 {cfg_path}，再问看板 {b['id']} 有没有告警（看板那边最多挂 {HOLD_MAX}s）", flush=True)
    first, synced, watch = not saved, bool(saved), saved.get("watch")
    wid = None if jobs else (saved.get("wid") if isinstance(saved.get("wid"), str) and saved.get("wid") else uuid.uuid4().hex[:16])
    rid, down, back, sess, told = uuid.uuid4().hex, None, 1.0, client.session_id(), False
    retry_commit, retry_at, hinted = None, 0.0, False
    shown_st: dict[str, str | None] = {}
    while True:
        try:
            r = _sync(cfg_path)
            if r.get("code") == client.CLIENT_OUTDATED:
                return _wait_outdated(r["msg"], None, cfg_path, jobs, watch, take, wid)
            if r["act"] in ("unregistered", "denied"):
                print(f"[labmon] {r['msg']}", file=sys.stderr)
                return 3
            if r["act"] == "error" and not synced and r.get("code") == 400:     # 第一轮配置就不对：同以前读配置出错
                print(f"[labmon] 配置错误：{r['msg']}", file=sys.stderr)
                return 3
            synced = True
            if r["act"] in ("push", "pull"):
                print(f"[labmon] {fmt_time(time.time())} {r['msg']}", flush=True)
                b = brief(cfg_path)                    # 推上去、拉回来的采集间隔、stale_min 改了：连不上的门槛跟着变
                limit = stale_limit(b["interval"], b["stale_min"])
            extra = {r["key"]: r["msg"]} if r.get("key") else {}
            res = client.call("POST", "/api/wait", {"project": b["id"], "jobs": jobs or [], "watch": watch, "hold": 0 if first else HOLD_MAX,
                                                    "extra": extra, "rid": rid, "session": sess, "wid": wid, "take": take}, timeout=HOLD_MAX + 20, patience=0, idem=True)
        except client.Failed as e:
            if e.code == client.CLIENT_OUTDATED:
                return _wait_outdated(e.msg, e.data.get("commit"), cfg_path, jobs, watch, take, wid)
            print(f"[labmon] {e.msg}", file=sys.stderr)
            return 3
        except client.Unreachable as e:
            now = time.time()
            down = down or now
            if now - down > WAIT_OFFLINE_MAX_S:
                print(f"[labmon] {fmt_time(now)} 新告警 1 条", flush=True)
                print(f"  - [server_unreachable] 连不上看板服务已 {(now - down) / 60:.0f} 分钟：{e}。告诉用户先看网络与看板服务（TX 隧道、看板那台机器），"
                      f"然后马上重挂 labmon wait（它自己退避重试，断网时不会退出，连着 {WAIT_OFFLINE_MAX_S // 60} 分钟连不上才退一次）；别绕开看板去直接操作服务器", flush=True)
                return 1
            if now - down > limit and not told:
                told = True
                print(f"[labmon] {fmt_time(now)} 连不上看板服务已 {(now - down) / 60:.0f} 分钟：{e}；接着重试，连回来报 server_back", file=sys.stderr, flush=True)
            time.sleep(back)
            back = min(back * 2, client.BACKOFF_MAX)
            continue
        watch, new, st, done = res.get("watch"), res.get("new") or {}, res.get("jobs") or [], bool(res.get("done"))
        if told:                                       # 这段断网过了门槛：连回来的这一轮叫醒会话，回头看有没有错过的事
            now = time.time()
            new = {"server_back": f"看板服务又连上了：{fmt_time(down)} 到 {fmt_time(now)} 连不上（{(now - down) / 60:.0f} 分钟）。这期间出现又消失的告警不会再报，"
                                  "用 labmon status、labmon check 看看有没有错过的事（任务失败、借到卡、页面上的改动）", **new}
        down, back, rid, take, told = None, 1.0, uuid.uuid4().hex, False, False
        if res.get("client_status") == "unknown" and not hinted:       # 比不出先后（比看板新、分支上跑）：不拦、不换包，提示一次（#17）
            hinted = True
            print(f"[labmon] 看板认不出这个命令行的版本（{dist.short(dist.version())}，看板在跑 {dist.short(res.get('commit'))}）："
                  "照常等；要与看板一致就跑 labmon update", file=sys.stderr, flush=True)
        if jobs and first:
            shown_st = _print_wait_jobs(st, shown_st, first=True)
        first = False
        if not new and not done:
            commit = res.get("commit")
            if res.get("client_status") == "outdated" and isinstance(commit, str) and dist.installed():
                if commit != retry_commit or time.monotonic() >= retry_at:
                    if err := _wait_update(commit, cfg_path, jobs, watch, wid=wid):
                        print(f"[labmon] 暂时更新不了，继续等待：{err}", file=sys.stderr, flush=True)
                    retry_commit, retry_at = commit, time.monotonic() + WAIT_UPDATE_RETRY_S
            time.sleep(GAP)
            continue
        if new:
            print(f"[labmon] {fmt_time(time.time())} 新告警 {len(new)} 条", flush=True)
            for k, m in new.items():
                print(f"  - [{k}] {m}", flush=True)
        if jobs:
            shown_st = _print_wait_jobs(st, shown_st, done=done)
        if new or not done:                            # 有新告警照旧 1：告警已记成报过，退出码不能让人以为不用处理
            return 1
        return 0 if all(x["st"] == "done" for x in st) else JOB_BAD_RC


def cmd_check(cfg_path: str, probe: bool = False) -> int:
    """labmon check：看板按它的用户级主机清单校验项目目录里这份（POST /api/project/<id>/check，api.check），默认打紧凑摘要（probe=True 打完整探针规格）；
    再打与看板上那份的同步状态，最后几行由看板给（不再生效的旧键、wait 还没报过的页面上的改动与归档）。"""
    try:
        b = brief(cfg_path)
        text = Path(cfg_path).expanduser().read_text()
    except (ConfigError, OSError) as e:
        print(f"[labmon] 配置错误：{e}", file=sys.stderr)
        return 3
    try:
        r = client.call("POST", f"/api/project/{quote(b['id'], safe='')}/check", {"text": text, **({"probe": True} if probe else {})})
    except client.Failed as e:
        print(f"[labmon] 配置错误：{e.msg}" if e.code == 400 else f"[labmon] {e.msg}", file=sys.stderr)
        return 3
    except client.Unreachable as e:
        print(f"[labmon] {e}", file=sys.stderr)
        return 2
    sys.stdout.write(r.get("out") or "")
    try:
        print(f"[labmon] {PS.say(PS.status(cfg_path), cfg_path)}")
    except (ConfigError, PJ.Refused, client.Unreachable) as e:
        print(f"[labmon] 与看板上那份比不了：{e}")
    for m in r.get("after") or []:
        print(m)
    hint = client.stale_hint(r.get("commit"))
    if hint:
        print(f"[labmon] {hint}")
    return int(r.get("rc") or 0)


def cmd_collect(cfg_path: str) -> int:
    """labmon collect：让看板立即采集这个项目一次（POST /p/<id>/collect），打出结果与看板上的链接。"""
    try:
        pid = brief(cfg_path)["id"]
    except ConfigError as e:
        print(f"[labmon] 配置错误：{e}", file=sys.stderr)
        return 3
    snap = _call("POST", f"/p/{quote(pid, safe='')}/collect", timeout=600)
    if isinstance(snap, int):
        return snap
    _summary(snap, client.urls(pid)[0])
    return 0



def cmd_report(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="labmon report", description="将 labmon 的问题提交给看板")
    ap.add_argument("title", nargs="?", help="一句话现象")
    ap.add_argument("--evidence", help="实际与预期、对实验的影响及日志证据")
    ap.add_argument("--project", help="项目 id，缺省取当前目录的 monitor.toml")
    ap.add_argument("--host", help="主机 id")
    ap.add_argument("--at", help="发生时刻，缺省只记录提交时间")
    ap.add_argument("--list", action="store_true", help="供总控的等待读取报告编号、报告人和时间（JSON）")
    ap.add_argument("--token-file", type=Path, help="等待专用令牌文件，仅用于 --list")
    a = ap.parse_args(argv)
    if a.token_file and not a.list:
        ap.error("--token-file 只用于 --list")
    if a.list:
        if any(v is not None for v in (a.title, a.evidence, a.project, a.host, a.at)):
            ap.error("--list 不能和提交报告的参数同用")
        try:
            print(json.dumps(client.call("GET", "/api/reports", timeout=8, patience=0, token_path=a.token_file.expanduser() if a.token_file else None), ensure_ascii=False))
        except (client.Failed, client.Unreachable) as e:
            print(f"[labmon] {e}", file=sys.stderr)
            return 3
        return 0
    if not a.title or not a.evidence:
        ap.error("需要填写现象与 --evidence")
    try:
        project = a.project
        if project is None and Path("monitor.toml").is_file():
            project = brief("monitor.toml")["id"]
        occurred = parse_time(a.at, rel=True) if a.at else None
    except (ConfigError, OSError, ValueError, OverflowError) as e:
        ap.error(str(e))
    if a.at and occurred is None:
        ap.error("认不出发生时刻，请写月-日 时:分")
    return client.run("POST", "/api/report", {"title": a.title, "evidence": a.evidence, "project": project,
                                               "host": a.host, "occurred": occurred}, ap=ap, timeout=30)


READS = {"status": "项目现状：各主机与卡、各车道、运行中（进度、预计结束、CPU、日志最后一行）、排队的预计开始、失败、计划项、挪法建议、告警",
         "disk": "项目的磁盘：共享盘上本项目各目录的大小与近 24 小时、7 天的增量，整块盘剩多少、几天满；各主机本地盘上本项目的缓存与单格临时",
         "records": "项目的记录（记录页里与本项目有关的：开始、失败、拉起、掉线、续租、临时指令……），新的在前"}


def cmd_colab(argv: list[str]) -> int:
    """labmon colab sync：在本机调用 google-colab-cli（colab usage）并把个人 CU 摘要同步到看板（绝不上传 Google 凭据）。"""
    from . import ephemeral as EP

    ap = argparse.ArgumentParser(prog="labmon colab", description="个人 Google Colab 自助命令（绝不向看板上传 Google 凭据）")
    sub = ap.add_subparsers(dest="sub", required=True)
    sp = sub.add_parser("sync", help="调用本机 `colab usage` 解析剩余 CU / 消耗速率 / 活跃数并同步到看板")
    sp.add_argument("--colab-bin", default=None, help="指定 google-colab-cli 可执行文件路径（默认按 PATH → ~/.local/bin/colab 查找）")
    sp.add_argument("--dry-run", action="store_true", help="仅打印解析结果，不向看板推送")
    sp.add_argument("--enable", action="store_true", help="同步时顺手开启总览「我的 Colab」个人摘要开关（缺省保持现有开关状态）")
    a = ap.parse_args(argv)
    if a.sub == "sync":
        return EP.sync_colab_usage(colab_bin=a.colab_bin, dry_run=a.dry_run, enable=a.enable)
    return 2


def cmd_read(argv: list[str]) -> int:
    """实验会话的只读命令（#52）：labmon status|disk|records [项目 id]，经 GET /api/project/<id>/<命令>（api.status、api.disk、api.records），
    看板排好版、这里原样打出；--json 打原始数据。项目缺省取 -c 的 monitor.toml（缺省当前目录的）。
    labmon disk 另支持 --me（以及未传项目 id 且当前目录无 monitor.toml 时自动回退）经 GET /api/disk 查本人共享盘汇总（#211）。"""
    what = argv[0]
    ap = argparse.ArgumentParser(prog=f"labmon {what}", description=READS[what])
    ap.add_argument("project", nargs="?", help="项目 id（缺省取 -c 那份 monitor.toml 的 id）")
    ap.add_argument("-c", "--config", default=None, help="项目目录里的配置文件（缺省 monitor.toml）")
    ap.add_argument("--json", action="store_true", help="打原始数据（JSON）：status 是项目快照（docs/schema.md），disk 是存储数据，records 是记录行")
    if what == "disk":
        ap.add_argument("--me", action="store_true", help="查看本人在共享盘的用量汇总与总盘余量（经 GET /api/disk）")
    if what == "records":
        ap.add_argument("--host", help="只看这台主机的")
        ap.add_argument("--cat", help="只看这一类：link 连接、op 平台操作、config 配置改动、lease 租期、borrow 借用、sched 调度、disk 共享盘、queue 队列……")
        ap.add_argument("-q", help="按词搜（空格分开的几个词都要出现）")
        ap.add_argument("-n", type=int, default=30, help="最多几条（缺省 30）")
    a = ap.parse_args(argv[1:])
    if what == "disk" and a.me:
        if a.project or a.config:
            ap.error("--me 不能和项目 id 或 -c 同用")
        return client.run("GET", "/api/disk", ap=ap, timeout=60, as_json=a.json)
    pid = a.project
    if not pid:
        cfg = a.config or "monitor.toml"
        if what == "disk" and a.config is None and not Path(cfg).is_file():
            return client.run("GET", "/api/disk", ap=ap, timeout=60, as_json=a.json)
        try:
            pid = brief(cfg)["id"]
        except ConfigError as e:
            print(f"[labmon] 配置错误：{e}（在项目目录里跑，或写项目 id）", file=sys.stderr)
            return 3
    q = urlencode({k: v for k, v in (("host", getattr(a, "host", None)), ("cat", getattr(a, "cat", None)), ("q", getattr(a, "q", None)),
                                     ("limit", getattr(a, "n", None))) if v})
    return client.run("GET", f"/api/project/{quote(pid, safe='')}/{what}" + (f"?{q}" if q else ""), ap=ap, timeout=60, as_json=a.json)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    dist.hold_version()                                # 看板装的：这版在用，别的命令更新时不删它（#18）
    if argv[:1] and argv[0] in OVR:
        return cmd_override(argv)
    if argv[:1] == ["host"]:
        return cmd_host(argv[1:])
    if argv[:1] == ["borrow"]:
        return cmd_borrow(argv[1:])
    if argv[:1] == ["user"]:
        return cmd_user(argv[1:])
    if argv[:1] == ["server"]:
        return cmd_server(argv[1:])
    if argv[:1] == ["secret"]:
        return cmd_secret(argv[1:])
    if argv[:1] == ["backup"]:
        return BK.cli(argv[1:])
    if argv[:1] == ["login"]:
        lp = argparse.ArgumentParser(prog="labmon login", description="输看板账号的口令换一个令牌，存进 client.toml 旁边的 client.token（权限 600）")
        lp.add_argument("--name", help="看板账号（缺省现问）")
        lp.add_argument("--admin", action="store_true", help="另建 1 小时的 admin 令牌（admin.token）：借卡、归还主机、drain、worker start|stop|limit 用")
        la = lp.parse_args(argv[1:])
        return client.login(la.name, la.admin)
    if argv[:1] == ["report"]:
        return cmd_report(argv[1:])
    if argv[:1] == ["colab"]:
        return cmd_colab(argv[1:])
    if argv[:1] and argv[0] in READS:
        return cmd_read(argv)
    if argv[:1] == ["update"]:
        argparse.ArgumentParser(prog="labmon update", description="从看板下载新版并更新命令行").parse_args(argv[1:])
        return client.update()
    if argv[:1] == ["url"]:
        if len(argv) != 2:
            print("用法：labmon url <项目 id>", file=sys.stderr)
            return 2
        try:
            print("\n".join(client.urls(argv[1])))
        except ConfigError as e:
            print(f"[labmon] {e}", file=sys.stderr)
            return 3
        return 0
    ap = argparse.ArgumentParser(prog="labmon", description="本机各项目在实验室 GPU 服务器上跑实验的统一工具（看板、worker、组内服务器）", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # pause 等临时指令、borrow、user、server、secret、backup、login、url、update、status、disk、records 有自己的参数解析（上面先截走），列在这里只为 --help 能看到
    ap.add_argument("cmd", choices=["collect", "render", "check", "serve", "wait", "service", "host", "worker", "project", "fleet", "notify", "hostcheck", *OVR, "borrow", "user",
                                    "server", "secret", "backup", "login", "url", "update", "report", "colab", *READS],
                    help="见下面的用法；pause / resume / focus / unfocus / drain / undrain / borrow / user / server / secret / backup / login / colab / status / disk / records 各有自己的 --help")
    ap.add_argument("sub", nargs="?", choices=["install", "add", "status", "restart", "stop", "list", "return", "restore", "start", "rm", "init", "limit", "probe", "boot", "import", "fit", "clean", "reset-hist",
                                               "archive", "push", "pull", "test"],
                    help="service 的动作（install/status/restart/stop），host 的动作（list/return/restore/add/edit/test/key/rm），"
                         "worker 的动作（start/stop/status/add/rm/limit/fit/clean/reset-hist），project 的动作（push/pull/init/archive/restore），fleet 的动作（status/probe/boot/import），notify 的动作（test）")
    ap.add_argument("target", nargs="?", help="host / worker / fleet 的主机 id；project init 的主机；project archive / restore 的项目 id；notify test 的文字")
    ap.add_argument("rest", nargs="*", help="worker add / rm 的 ROOT JOBS；worker fit 的任务名；project init 的项目名；fleet import 的 Json 文件")
    ap.add_argument("--gpus", help="worker add：这条队列只用这些卡（nvidia-smi 序号，逗号分隔）")
    ap.add_argument("--project", help="worker add / reset-hist 必写（worker clean 可选）：看板的项目 id（monitor.toml 里的 id；pause、focus、借卡的“只给谁用”都按它认）")
    ap.add_argument("--tag", help="worker reset-hist：按任务 tag 重置历史资源峰值与 OOM 下限（--tag 与 --name 至少写一个）")
    ap.add_argument("--name", help="worker reset-hist：按任务名或去种子前缀重置历史资源峰值与 OOM 下限（--tag 与 --name 至少写一个）")
    ap.add_argument("--stop", help="worker add：暂停时怎么停：boundary（缺省）/ term / file:<相对 ROOT 的路径>")
    ap.add_argument("--list", action="store_true", help="worker fit：位置参数是 ROOT JOBS，看这条清单还会跑的行能在共用 worker 目录的几台在用主机上再开几条车道（要 --project）")
    ap.add_argument("--cache", action="store_true", help="worker clean：仅清理自管缓存（<LOCAL>/cache）")
    ap.add_argument("--scratch", action="store_true", help="worker clean：仅清理已结束任务的临时目录（<LOCAL>/jobs）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="worker clean：仅列出可清理项，不删文件（默认行为）")
    ap.add_argument("--yes", action="store_true", help="worker clean：显式确认实际删除自管缓存/临时目录（绝不碰 projects/ 产物）")
    ap.add_argument("--at", help="host return 的归还时间（缺省为现在）：2026-11-04 18:40、11-04 18:40、18:40、明天 9:00，写进清单的是带年份的")
    ap.add_argument("--now", action="store_true", help="host return 借来的组内服务器：正在跑的按各自的停法停下，不等跑完")
    ap.add_argument("-c", "--config", action="append", help="项目目录里的配置文件（缺省 monitor.toml）")
    ap.add_argument("--probe", action="store_true", help="check：打印各主机的完整探针规格 JSON")
    ap.add_argument("--out", help="render 的输出目录（缺省看板上这个项目的输出目录）")
    ap.add_argument("--admin", action="store_true", help="project push：用 labmon login --admin 建的管理员令牌推（改了 [[hosts]]、[[queues]] 时要）")
    ap.add_argument("--job", action="append", help="wait：同时等这个任务跑完（可多次、也可逗号分隔；写法同清单 after=，别的项目的写 项目/任务）")
    ap.add_argument("--take", action="store_true", help="wait：改由本会话等这个项目（另一个会话正挂着的 wait 下一轮退出，#142）")
    ap.add_argument("--interval", type=int, help="serve / service install 的采集间隔（秒，缺省取配置）")
    ap.add_argument("--port", type=int, help=f"serve 的端口（缺省 {PORT}）")
    ap.add_argument("--public-port", type=int, help="serve / service install：另起公网入口（只绑 127.0.0.1，给隧道接；平台操作不在这里做）；service install 写 0 去掉")
    a = ap.parse_args(argv)
    configs = a.config or ["monitor.toml"]
    if (a.job or a.take) and a.cmd != "wait":
        ap.error("--job、--take 只用于 wait")
    if a.probe and a.cmd != "check":
        ap.error("--probe 只用于 check")
    if a.out and a.cmd != "render":
        ap.error("--out 只用于 render（collect 由看板采，写看板上这个项目的输出目录）")
    if a.cmd == "hostcheck":
        if a.sub or a.target:
            ap.error("用法：labmon hostcheck")
        from . import hostcheck
        return hostcheck.cli()
    if a.cmd == "notify":
        if a.sub != "test" or a.rest:
            ap.error("用法：labmon notify test [文字]（文字有空格时加引号）")
        return S.notify_test(a.target)
    if a.cmd == "fleet":
        if a.sub not in (None, "status", "probe", "boot", "import") or (a.sub in ("probe", "import") and not a.target) \
                or (a.sub == "import" and len(a.rest) != 1):
            ap.error("用法：labmon fleet [status [id]] | probe <id> | boot [id] | import <id> <Json 文件>")
        try:
            if a.sub == "boot":
                return FO.show_boot(a.target)
            if a.sub == "import":
                e = FO.import_json(a.target, Path(a.rest[0]).expanduser().read_text())
                print(f"[labmon] 已存 {a.target} 的开机参数：{e.get('image')}，端口 {e.get('ports')}，{e.get('cpu')} 核 / {e.get('memory')} GB")
                return 0
            return FV.show(a.target, probe=a.sub == "probe")
        except PlatformError as e:
            print(f"[labmon] {e}", file=sys.stderr)
            return 2
        except ConfigError as e:
            print(f"[labmon] {e}", file=sys.stderr)
            return 3
    if a.cmd == "project":
        if a.sub in ("push", "pull"):
            if a.target or len(configs) > 1:
                ap.error(f"用法：labmon project {a.sub} [-c monitor.toml]")
            try:
                print("[labmon] " + (PS.push(configs[0], admin=a.admin) if a.sub == "push" else PS.pull(configs[0])), flush=True)
                return 0
            except (ConfigError, PJ.Refused) as e:
                print(f"[labmon] {e}", file=sys.stderr)
                return 3
            except client.Unreachable as e:
                print(f"[labmon] {e}", file=sys.stderr)
                return 2
        if a.sub not in ("init", "archive", "restore") or not a.target or len(a.rest) > (a.sub == "init"):
            ap.error("用法：labmon project push|pull [-c monitor.toml] | labmon project init <主机> [项目名] | labmon project archive|restore <项目 id>")
        if a.sub == "init":
            return client.run("POST", "/api/project/init", {"host": a.target, "name": a.rest[0] if a.rest else None}, ap=ap)
        r = _call("POST", f"/project/{a.sub}", {"project": a.target})
        if isinstance(r, int):
            return r
        print("[labmon] " + r["msg"], flush=True)
        return 0
    if a.cmd == "worker":
        if a.sub not in WORKER_ACTS or not a.target:
            ap.error("用法：labmon worker start|stop|status <主机> | labmon worker add <主机> ROOT JOBS --project <项目 id> [--gpus 0]"
                     " | labmon worker rm <主机> ROOT JOBS | labmon worker limit <主机> tag=N | labmon worker fit <主机> 任务名… [--project <项目 id>]"
                     " | labmon worker fit <主机> ROOT JOBS --list --project <项目 id>"
                     " | labmon worker clean <主机> [--project <项目 id>] [--cache] [--scratch] [--dry-run] [--yes]"
                     " | labmon worker reset-hist <主机|pool|all> --project <项目 id> [--tag <tag>] [--name <任务名或前缀>]")
        if a.list and (a.sub != "fit" or len(a.rest) != 2 or not a.project):
            ap.error("--list 只用于 worker fit：labmon worker fit <主机> ROOT JOBS --list --project <项目 id>")
        if (a.cache or a.scratch or a.dry_run or a.yes) and a.sub != "clean":
            ap.error("--cache、--scratch、--dry-run、--yes 只用于 worker clean")
        if (a.tag or a.name) and a.sub != "reset-hist":
            ap.error("--tag、--name 只用于 worker reset-hist")
        if a.sub == "clean" and a.rest:
            ap.error("worker clean 不接受额外位置参数：labmon worker clean <主机> [--project <项目 id>] [--cache] [--scratch] [--yes]")
        if a.sub == "reset-hist":
            if a.rest:
                ap.error("worker reset-hist 不接受额外位置参数：labmon worker reset-hist <主机|pool|all> --project <项目 id> [--tag <tag>] [--name <任务名或前缀>]")
            if not a.project:
                ap.error("worker reset-hist 需要 --project <项目 id>（monitor.toml 里的 id）")
            if not (a.tag or a.name):
                ap.error("worker reset-hist 需要至少指定 --tag 或 --name")
            return client.run("POST", "/api/worker/reset-hist", {"host": a.target, "args": [], "project": a.project, "tag": a.tag, "name": a.name}, ap=ap)
        if a.sub == "fit" and not a.rest:
            ap.error("worker fit 需要任务名")
        if a.sub in ("add", "rm") and len(a.rest) != 2:
            ap.error(f"worker {a.sub} 需要 ROOT JOBS")
        if a.sub == "add" and not a.project:            # worker 缺省取共享盘目录名，与看板的项目 id 常不一样：暂停、优先、借卡的“只给谁用”就对不上
            ap.error("worker add 需要 --project <项目 id>（monitor.toml 里的 id）")
        return client.run("POST", f"/api/worker/{a.sub}", {"host": a.target, "args": a.rest, "gpus": a.gpus, "project": a.project, "stop": a.stop,
                                                           **({"list": True} if a.list else {}),
                                                           **({"cache": a.cache, "scratch": a.scratch, "dry_run": a.dry_run, "yes": a.yes} if a.sub == "clean" else {})},
                          admin=a.sub in ("start", "stop", "limit"), ap=ap)
    if a.cmd == "serve":                              # -c 只给迁移用（可删：#161 部署后 labmon service install 去掉了 plist 里的 -c 就删）
        return serve(a.port or PORT, a.interval, a.public_port or None, legacy=a.config or [])
    if a.cmd == "service":
        if a.config:
            ap.error("service 不再带 -c：项目用 labmon project push 登记、labmon project archive 收起")
        fn = {"install": lambda: S.install(a.port, a.interval, a.public_port),
              "status": S.status, "restart": S.restart, "stop": S.stop}.get(a.sub or "status")
        if fn is None:
            ap.error("用法：labmon service install [--port P] [--public-port Q] [--interval 秒] | labmon service status|restart|stop")
        return fn()
    if len(configs) > 1:
        ap.error(f"{a.cmd} 只认一个 -c（给了 {len(configs)} 个）")
    if a.cmd == "check":
        return cmd_check(configs[0], probe=a.probe)
    if a.cmd == "collect":
        return cmd_collect(configs[0])
    if a.cmd == "render":                             # 看板服务那台机器上：只用上次的快照重新渲染（改页面时用）
        try:
            pid = brief(configs[0])["id"]
        except ConfigError as e:
            print(f"[labmon] 配置错误：{e}", file=sys.stderr)
            return 3
        out = Path(a.out or PJ.out_dir(pid)).expanduser()
        snap = load_json(out / "snapshot.json", None)
        if snap is None:
            print(f"[labmon] {out / 'snapshot.json'} 不存在，先跑一次 collect", file=sys.stderr)
            return 2
        print(C.render(snap, out))
        return 0
    return cmd_wait(configs[0], jobs=[x.strip() for v in a.job or [] for x in v.split(",") if x.strip()], take=a.take)


if __name__ == "__main__":
    sys.exit(main())
