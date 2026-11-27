"""平台续租：预备（等这格跑完、到期优先）与无头续租。看板服务在后台线程里跑。

    预备：手动点的立即对主机下 drain：正在跑的任务照常跑完、不再放行新任务，worker 空闲时自动开保活。
          到期前 lease_reserve_h 小时（缺省 24，0 关）还没预备的自动预备，用的是截止准入（worker 的 deadline 指令）：
          截止前跑得完的任务照常放行，跑不完的不放，GPU 不白等。
          全部跑完（或剩下的都跑不完）就通知“可以续租了”，并下 drain 冻结，等人来续租时不再放行新任务。
          截止时刻是到期前 1 小时：有任务预计在截止后才跑完，或已到截止，就不再等，直接通知续租（会中断这些任务）。
          取消预备时记下这一期的到期时刻（kept）：在自动预备的时段里取消过的，这一期不再自动预备（不然下一轮采集又预备回来）。
    续租：与组内服务器同一条流程（fleetops.run_renew，同一个平台登录，48 小时内不用再输验证码）：
          登录或沿用登录 → 探测并核对（worker 有任务在跑要页面上确认会中断）→ 停止旧作业 → 提交新作业、等运行 →
          等 ssh、拉起 worker、解除预备（after）。作业已被平台停掉的，跳过停止，克隆同池最近一个同名作业重新提交。
          出错停在那一步；新作业已在运行时只给“继续”（fleetops 的 ka：等 ssh、拉起 worker），不再碰平台。
    本地盘：续租会重启容器、清空本地盘（/tmp 等）。worker 每 5 分钟看一次待跑任务 needs= 声明的本地文件（status.json 的 local）：
          自动预备、可以续租时的通知与卡片都写明哪些任务会受影响；命令里先 [ -f 文件 ] || 重新生成的算能自愈。
          有不能自愈的，续租完不恢复放行（保持 drain，卡片显示「恢复放行」），免得这些任务一格格失败、拖停队列。

主机清单 [[hosts]] 里要有 platform、platform_user、platform_job、platform_pool（端口取主机的 port）。
状态存在本机状态目录 overview/renew.json：{"res": {主机: 预备}, "kept": {主机: 取消预备时的 {end: 到期, t: 时刻}}, "loss": {主机: 本地文件依赖}, "renewing": {主机: 续租开始时的依赖}}；
续租进度在组内操作的 op.json。
"""
from __future__ import annotations

import threading
import time

from . import fleet as F
from . import platform as P
from .config import fmt_time, host_platform
from .records import log_events
from .service import log
from .state import load_json, save_json, state_dir
from .workerctl import override_each, run_out

MARGIN_S = 3600                  # 截止 = 到期前这么多秒，留给人来输验证码与续租本身
AUTO_H = 24                      # 缺省：到期前这么多小时自动预备（截止准入，跑得完的照常跑）
EXTRA_S = 12 * 3600              # drain、截止指令的兜底期限：到期后再过这么久自动失效，忘了解除也不会一直挡着
LOCK = threading.Lock()


def _file():
    return state_dir() / "renew.json"


def state() -> dict:
    st = load_json(_file(), {})
    st.setdefault("res", {})
    st.setdefault("kept", {})
    st.setdefault("loss", {})
    st.setdefault("renewing", {})
    return st


def _update(fn):
    with LOCK:
        st = state()
        out = fn(st)
        state_dir().mkdir(parents=True, exist_ok=True)
        save_json(_file(), st)
        return out


def _event(hid: str, msg: str, detail: str | None = None) -> None:
    log_events([{"kind": "renew", "host": hid, "msg": msg, "detail": detail}])


def _ovr(action: str, kind: str, hid: str, until: float | None = None, at: float | None = None) -> int:
    """在主机的 worker 上加或撤临时指令（drain / deadline），返回退出码；worker 的回话（生效中：…）或 ssh 连不上的原因记进服务日志。"""
    r = override_each(action, kind, None, [hid], until, False, at)[0]
    log(f"续租 {hid}：指令 {action} {kind} → {r['out'].strip() or ('完成' if r['rc'] == 0 else '退出码 %d' % r['rc'])}")
    return r["rc"]


def _release(hid: str, ovr=_ovr) -> int:
    """撤掉预备时下的 drain 与截止指令。"""
    return ovr("rm", "drain", hid) | ovr("rm", "deadline", hid)


def reserve(hid: str, lease_end: float | None, auto: bool = False, ovr=_ovr) -> str:
    if hid in state()["res"]:
        return "已经预备过了"
    until = lease_end + EXTRA_S if lease_end else None
    dl = lease_end - MARGIN_S if lease_end else None
    if auto and dl:
        rc, mode = ovr("add", "deadline", hid, until, dl), "deadline"
        msg = f"到期前自动预备续租：{fmt_time(dl)} 截止前跑得完的任务照常放行，跑不完的不放"
    else:
        rc, mode = ovr("add", "drain", hid, until), "drain"
        msg = "预备续租：不再放行新任务，正在跑的跑完后开保活"
    if rc:
        raise P.PlatformError(f"给 worker 下指令失败（退出码 {rc}）")
    def put(st):
        st["res"][hid] = {"t": round(time.time()), "auto": auto, "mode": mode, "state": "waiting", "deadline": dl}
        st["kept"].pop(hid, None)
    _update(put)
    _event(hid, msg)
    return msg


def cancel(hid: str, lease_end: float | None = None, ovr=_ovr, t: float | None = None) -> str:
    """撤掉预备、恢复放行。记下这一期的到期时刻：在自动预备的时段里取消的，check 这一期不再自动预备。"""
    if hid not in state()["res"]:
        return "没有预备"
    rc = _release(hid, ovr)
    if rc:
        raise P.PlatformError(f"解除失败（退出码 {rc}）")
    def put(st):
        st["res"].pop(hid, None)
        if lease_end:
            st["kept"][hid] = {"end": round(lease_end), "t": round(t or time.time())}
    _update(put)
    _event(hid, "取消预备续租，恢复放行")
    return "已取消，恢复放行"


def eta_of(j: dict) -> float | None:
    """worker 状态里一个运行中任务的预计结束时刻（续租与借用的判断共用）。"""
    return (j.get("prog") or {}).get("eta_ts")


def _held(w: dict) -> bool:
    """等待中的任务是被临时指令挡住的（worker 状态里等待项的 held：drain、deadline、pause、guest），而不是在等资源。"""
    return bool(w.get("held"))


def loss_of(status: dict | None) -> dict | None:
    """worker 扫出的、续租会清空的本地文件依赖。fatal：会失败的任务；copy：共享盘有主副本、放行前拷回的任务；heal：命令能自己补、续租后重新生成的任务。"""
    rows = ((status or {}).get("local") or {}).get("jobs") or []
    if not rows:
        return None
    copy = lambda r: r.get("how") == "copy"        # noqa: E731
    return {"fatal": [r["job"] for r in rows if not r.get("heal")], "copy": [r["job"] for r in rows if r.get("heal") and copy(r)],
            "heal": [r["job"] for r in rows if r.get("heal") and not copy(r)],
            "dirs": sorted({f.rsplit("/", 1)[0] for r in rows for f in r.get("files", [])})[:3]}


def _names(js: list[str]) -> str:
    return "、".join(js[:3]) + (f" 等 {len(js)} 格" if len(js) > 3 else "")


def loss_text(l: dict | None) -> str:
    if not l:
        return ""
    out = []
    if l["fatal"]:
        out.append(f"续租会重启容器、清空本地盘，{_names(l['fatal'])} 要用的本地文件（{'、'.join(l['dirs'])}）会丢，这些任务会失败，"
                   "续租完先不恢复放行")
    if l.get("copy"):
        out.append(f"{_names(l['copy'])} 的本地文件会丢，共享盘有主副本，续租后放行前拷回来")
    if l["heal"]:
        out.append(f"{_names(l['heal'])} 的本地文件会丢，命令能自己补，续租后重新生成")
    return "；".join(out)


def check(hosts: list[dict], cfg: dict, t: float, reserve_fn=reserve, ovr=_ovr, busy=()) -> list[str]:
    """每轮全局采集后调用：到期前自动预备；预备中的主机判断能否续租。给每台主机填 e["renew"]（预备）与 e["auto_res"]
    （自动预备的时刻 at、这一期取消过不再自动预备 off；页面据此决定「取消预备」要不要先确认），返回要弹通知的消息。
    busy：正在做平台操作的主机（fleetops.busy()，调用方给）：不预备。renew.json 一轮只读一次；本地文件依赖、预备的状态变了才写（一次写完）。"""
    out, st, changes = [], state(), []        # changes：要写回的改动（套在当时的文件内容上，页面同时在改的不覆盖）

    def change(fn):
        fn(st)
        changes.append(fn)

    for e in hosts:
        c, end = cfg.get(e["id"], {}), e.get("lease_end")
        scm = host_platform(c) == "scm" and bool(c.get("platform"))
        auto_h = c.get("lease_reserve_h", AUTO_H)
        if scm and e.get("status") is not None:     # 续租前的本地文件依赖：续租开始时记进进度，续租后据此决定放不放行
            l = loss_of(e["status"])
            if st["loss"].get(e["id"]) != l:
                change(lambda s, hid=e["id"], l=l: s["loss"].__setitem__(hid, l))
        lt = "；".join(x for x in (loss_text(st["loss"].get(e["id"])), F.lw_text(e.get("lw"))) if x)   # 本地文件依赖与本地盘开机以来的新写入（#149）
        k, at = st["kept"].get(e["id"]), end - auto_h * 3600 if scm and end and auto_h else None
        off = bool(k and at and abs(k["end"] - end) < 600 and k["t"] >= at)      # 同一期、在自动预备时段里取消过
        e["auto_res"] = {"at": round(at), "off": off} if at else None
        if (e["id"] not in st["res"] and at and at <= t < end and not off
                and not e.get("error") and e["id"] not in busy):    # 正在续租（或换人、关机）的不预备
            try:
                out.append(f"{e['label']} " + reserve_fn(e["id"], end, auto=True) + (f"。注意：{lt}" if lt else ""))
                st["res"][e["id"]] = state()["res"].get(e["id"])      # reserve 自己写了文件：取它写的
            except P.PlatformError as x:
                out.append(f"{e['label']} 自动预备续租失败：{x}")
        r = st["res"].get(e["id"])
        if r is None:
            e["renew"] = None
            continue
        if r.get("state") == "held":              # 续租完、本地文件已丢：等人处理后点「恢复放行」
            e["renew"] = r
            continue
        sts = e.get("status") or {}
        # 暂停占位（等人删标记才退出）不算在跑：不等它，续租打断了也只是重开后接着停（同借卡的跑完就还）
        run, wait = [j for j in sts.get("running", []) if not j.get("hold")], sts.get("waiting", [])
        names = [j.get("name") for j in run]
        dl = r.get("deadline")
        late = [j for j in run if dl and eta_of(j) and eta_of(j) > dl]
        if e.get("status") is None:
            why, ready = "读不到主机状态", r["state"] == "ready"
        elif not run and (r.get("mode") != "deadline" or all(_held(w) for w in wait)):
            why, ready = "正在跑的都跑完了，保活已接手", True
        elif dl and t >= dl:
            why, ready = f"离到期不到 {MARGIN_S // 3600} 小时，不再等 {'、'.join(names)} 跑完：续租会中断它们", True
        elif late:
            why, ready = ("、".join(f"{j.get('name')} 预计 {fmt_time(eta_of(j), date=False)} 跑完" for j in late)
                          + f"，晚于截止（到期前 {MARGIN_S // 3600} 小时），等不了：续租会中断" + ("它" if len(late) == 1 else "它们")), True
        elif r.get("mode") == "deadline":
            why, ready = f"截止 {fmt_time(dl)} 前跑得完的照常放行", False
        else:
            why, ready = "等正在跑的跑完", False
        if ready and r["state"] != "ready":
            msg = (f"{e['label']} 可以续租了：{why}" + (f"（{fmt_time(end)} 到期）" if end else "")
                   + (f"。注意：{lt}" if lt else ""))
            out.append(msg)
            _event(e["id"], msg)
            if r.get("mode") == "deadline":          # 冻结：等人来续租的这段时间不再放行新任务
                if ovr("add", "drain", e["id"], end + EXTRA_S if end else None) == 0:
                    change(lambda s, hid=e["id"]: s["res"].get(hid, {}).update(mode="drain"))
        now = {"state": "ready" if ready else "waiting", "why": why, "interrupt": names if ready else []}
        if any(r.get(k) != v for k, v in now.items()):
            change(lambda s, hid=e["id"], now=now: s["res"].get(hid, {}).update(now))
        e["renew"] = dict(r)
    if changes:
        _update(lambda s: [fn(s) for fn in changes])
    return out


# ---------- 续租 ----------
# 平台上的那几步（登录或沿用登录、核对作业、克隆、停旧作业、提交新作业、等运行、等 ssh）与组内服务器共用 fleetops.run_renew；
# 这里只管自用主机特有的：开始前记下本地文件依赖，ssh 连上后挡住或放行、拉起 worker。
def _start_worker(hid: str) -> tuple[int, str]:
    """拉起这台的 worker：返回 (退出码, 输出)，输出进续租事件的 detail。"""
    return run_out("start", hid, [])


def begin(hid: str) -> None:
    """续租开始、停旧作业之前：记下此刻扫到的本地文件依赖（新容器里文件已清空，扫不出原来要什么了）。"""
    _update(lambda st: st["renewing"].__setitem__(hid, {"t": round(time.time()), "loss": st["loss"].get(hid)}))


def after(hid: str, label: str, lease_end: float | None, start_worker=_start_worker, ovr=_ovr) -> str:
    """新容器 ssh 已连上：本地文件已丢的先挡住，再拉起 worker；预备过的撤掉指令恢复放行。返回说明，worker 拉不起来就报错。"""
    loss = (state()["renewing"].get(hid) or {}).get("loss") or {}
    notes = []
    if loss.get("fatal"):                     # 先挡住再拉 worker：这些任务要的本地文件已随容器重启清空
        ovr("add", "drain", hid, lease_end + EXTRA_S if lease_end else None)
        why = (f"{_names(loss['fatal'])} 要用的本地文件（{'、'.join(loss['dirs'])}）已随容器重启清空，worker 保持不放行；"
               "补好文件或改好清单后点「恢复放行」")
        _update(lambda st: st["res"].__setitem__(hid, {"t": round(time.time()), "auto": False, "mode": "drain", "state": "held",
                                                        "why": why, "loss": loss}))
    rc, out = start_worker(hid)
    notes.append("worker 已拉起" if rc == 0 else f"worker 拉起失败（退出码 {rc}）")
    if loss.get("fatal"):
        notes.append(f"保持不放行：{_names(loss['fatal'])} 要用的本地文件已清空，处理后点「恢复放行」")
    elif hid in state()["res"]:
        rc2 = _release(hid, ovr)
        notes.append("已恢复放行" if rc2 == 0 else f"解除预备失败（退出码 {rc2}）")
        if rc2 == 0:
            _update(lambda st: st["res"].pop(hid, None))
    if rc:
        raise P.PlatformError("；".join(notes))
    _update(lambda st: st["renewing"].pop(hid, None))
    msg = f"{label} 续租完成" + (f"，新租期约到 {fmt_time(lease_end)}" if lease_end else "") + f"：{'；'.join(notes)}"
    _event(hid, msg, out.strip()[-400:])
    return "；".join(notes)
