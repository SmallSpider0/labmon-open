"""连通状态与租期的判定，看板主机（overview.health）、组内服务器（fleet.record）与项目页（collect.build）共用：
掉线与恢复（link_step）、换了新容器（container_step）、租期到期时刻与提醒档（lease_end、lease_due）。
每台的连通状态 hs 由调用方存：fails（连续连不上几次）、since（第一次连不上的时刻）、down（掉线原因，None 为没掉线）。
"""
from __future__ import annotations

from .config import fmt_time, parse_time

DOWN_S = 150                   # 连不上持续这么久（约 3 轮采集）才算掉线，网络抖一下不报
LEASE_WARN_H = (24, 6)         # 租期到期前这么多小时各提醒一次


def link_step(hs: dict, ok: bool | None, t: float, down_s: float = DOWN_S) -> str | None:
    """连通状态（唯一一处）：记下这一次探测，返回 "down"（这次刚够掉线：连续至少 2 次、
    持续 down_s 秒连不上，缺省 DOWN_S）、"up"（掉线后这次连上了）或 None。判掉线后由调用方写 hs["down"] = 原因；since 留到恢复后再清。
    ok 为 None 是这次没探（本机不在内网）：不计连不上，还没判掉线的计数清零（离开内网前那一次连不上不算数，回来后重新计）。"""
    if ok is None:
        if not hs.get("down"):
            hs["fails"] = 0
        return None
    if ok:
        was = hs.get("down")
        hs.update(fails=0, down=None)
        return "up" if was else None
    hs["fails"] = hs.get("fails", 0) + 1
    if hs["fails"] == 1:
        hs["since"] = t
    return "down" if hs["fails"] >= 2 and t - hs["since"] >= down_s and not hs.get("down") else None


def lease_end(started: float | None, days, until=None) -> float | None:
    """租期到期时刻：优先取显式绝对到期时刻 until（外平台租机 lease_until），否则取容器启动（或作业创建）时刻加 lease_days 天。"""
    if until is not None and until != "":
        if isinstance(until, (int, float)) and not isinstance(until, bool):
            return float(until)
        return parse_time(str(until))
    return started + float(days) * 86400 if started and days else None


def container_step(hs: dict, started: float | None, label: str) -> dict | None:
    """容器启动时刻：记下这次探到的 started；比上次记的变了（差 60 秒以上）就是换了新容器（续租或重启），
    已发的租期提醒清掉（新容器起算新租期），返回要记的 container_new 事件（kind、msg、started；调用方补 host、who 再记）；否则 None。"""
    ev = None
    if started and hs.get("started") and abs(started - hs["started"]) > 60:
        ev = {"kind": "container_new", "msg": f"{label} 换了新容器（{fmt_time(started)} 启动：续租或重启）", "started": round(started)}
        hs["warned"] = []
    if started:
        hs["started"] = started
    return ev


def lease_due(ends: list[tuple[dict, float | None]], t: float, merge_s: float = 0) -> list[list[int]]:
    """租期提醒的判定：到期前 LEASE_WARN_H 小时各提醒一次，已进更近一档的不再补远的那档；
    同一轮里相近到期（merge_s 内）的合成一组。ends：各台的 (连通状态 hs, 到期时刻)，已发的档记在 hs["warned"]。
    若该台到期时刻发生明显变化（如外平台原地续租改了 lease_until、容器未重启），重置 hs["warned"] 以重新计下一期提醒。
    返回要提醒的各组，每组是 ends 的下标（按到期先后）；文字由调用方写。"""
    for hs, e in ends:
        if e:
            prev = hs.get("lease_end")
            if prev is not None and abs(e - prev) > 60:
                hs["warned"] = []
            hs["lease_end"] = round(e)
    out = []
    for h in sorted(LEASE_WARN_H):
        cand = [(i, e) for i, (hs, e) in enumerate(ends) if e and t < e and h not in hs.setdefault("warned", [])]
        if not any(e - t <= h * 3600 for _, e in cand):
            continue
        due = sorted(((i, e) for i, e in cand if e - t <= h * 3600 + merge_s), key=lambda x: x[1])
        for i, _ in due:
            ends[i][0]["warned"] += [x for x in LEASE_WARN_H if x >= h and x not in ends[i][0]["warned"]]
        out.append([i for i, _ in due])
    return out
