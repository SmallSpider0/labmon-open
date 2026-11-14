"""看板服务的计划维护（#165）：装更新、重启常驻机之前开一段，期间 TX 的看门狗与所在机的体检都不推。

    labmon server maint 30m | 2h | 18:30 | 明天 9:00     开到这时（最长 MAX_S）；写法同 --until，不带 + 的 30m 也认
    labmon server maint off                              提前结束
    labmon server maint                                  看现在

状态目录的 maint.json：{"until": epoch 秒, "by": 谁开的, "t": 何时开的}。到期自然失效，不用清。
谁读：/healthz 的 maint_until（serve.health，看门狗记住它，服务停着时照样不报）、体检线程（hostcheck.loop 传给 tick）、
总览「看板服务」小卡（hostcheck.view 的 maint）。别的通知（实验室服务器掉线、租期）照常：维护的是看板服务所在机。
"""
from __future__ import annotations

import time

from .config import ConfigError, fmt_dur, fmt_time
from .records import log_events
from .state import load_json, save_json, state_dir

MAX_S = 86400                       # 最长开一天：忘了关的不至于一直不报


def _file():
    return state_dir() / "maint.json"


def until(t: float | None = None) -> float | None:
    """计划维护到何时；不在维护（没开、已到期、文件坏了）为 None。"""
    t = time.time() if t is None else t
    u = (load_json(_file(), {}) or {}).get("until")
    return float(u) if isinstance(u, (int, float)) and not isinstance(u, bool) and u > t else None


def start(end: float, by: str, t: float | None = None) -> str:
    """开计划维护到 end（epoch 秒）；已在维护的改成新的到期。超过 MAX_S、已经过去的拒绝（ConfigError）。记一条记录。"""
    t = time.time() if t is None else t
    if end <= t:
        raise ConfigError("计划维护的到期时间已经过去")
    if end - t > MAX_S + 60:
        raise ConfigError(f"计划维护最长 {fmt_dur(MAX_S)}（写的到 {fmt_time(end)}）：修得久的到时再开一次")
    save_json(_file(), {"until": round(end), "by": by, "t": round(t)})
    msg = (f"计划维护到 {fmt_time(end)}（{fmt_dur(end - t)}）：期间体检不推提醒；TX 的看门狗每 5 分钟探一次，要等它探到这回才不报，"
           "要停服务、断隧道或重启的，等 5 分钟再动手（TX 上 journalctl -u labmon-watch 看到“计划维护到”就是知道了）")
    log_events([{"kind": "maint", "msg": msg, "detail": f"{by}开的"}], t)
    return msg


def stop(by: str, t: float | None = None) -> str:
    """提前结束计划维护（没在维护就什么都不做）。"""
    t = time.time() if t is None else t
    if until(t) is None:
        return "没在计划维护"
    save_json(_file(), {"until": None, "by": by, "t": round(t)})
    log_events([{"kind": "maint", "msg": "计划维护提前结束", "detail": f"{by}结束的"}], t)
    return "计划维护已结束：看门狗与体检照常提醒"


def show(t: float | None = None) -> str:
    u = until(t)
    return f"计划维护到 {fmt_time(u)}（还有 {fmt_dur(u - (time.time() if t is None else t))}）" if u else "没在计划维护"
