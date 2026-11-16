"""记录：看板与组内服务器的事件、平台操作、配置改动，写在状态目录的 events.jsonl（保留一年），
加上各主机 worker 的事件（overview._keep_log 存的 hostlog/<主机>.jsonl）。写入 log_events，按条件读 records；页面经 /records.json 取。
页面上的改动另经 tell 记一行给实验会话（tell.jsonl，保留 7 天），told 每轮并进全局快照的 wake（#121）。
"""
from __future__ import annotations

import re
import time
import uuid

from .state import append_row, load_rows, state_dir

EVENT_KEEP_S = 365 * 86400
# 记录里每种事件的中文名、色调与分类（页面上的标签与筛选都按这张表）
EVENT_CATS = [("link", "连接"), ("op", "平台操作"), ("config", "配置改动"), ("lease", "租期"), ("borrow", "借用"), ("keepalive", "保活"), ("disk", "共享盘"),
              ("queue", "队列"), ("board", "看板"), ("report", "问题报告")]
EVENT_KINDS = {
    "report": ("问题报告", "mute", "report"),
    "host_down": ("掉线", "bad", "link"), "host_up": ("恢复", "ok", "link"), "container_new": ("新容器", "warn", "link"),
    "fleet_down": ("掉线", "bad", "link"), "fleet_up": ("恢复", "ok", "link"), "fleet_unknown": ("未识别", "warn", "link"),
    "missing": ("缺文件", "bad", "link"), "lan": ("内网", "mute", "link"),
    "op_start": ("开始", "mute", "op"), "op_done": ("完成", "ok", "op"), "op_fail": ("停下", "bad", "op"), "login": ("登录", "mute", "op"),
    "renew": ("续租", "warn", "op"), "fleet_renew": ("续租", "ok", "op"), "fleet_off": ("关机", "warn", "op"), "fleet_boot": ("开机", "ok", "op"),
    "boot": ("开机参数", "mute", "op"),
    "config": ("改配置", "warn", "config"), "project": ("项目", "warn", "config"), "lease": ("租期", "warn", "lease"), "disk": ("共享盘", "bad", "disk"),
    "account": ("账号", "warn", "config"),                               # 看板账号的改动与锁定（users.py，#160）
    "io_suspect": ("读写拥塞嫌疑", "warn", "disk"),                       # worker 记的：任务比同类慢很多、共享盘又慢（只记录、不告警）
    "borrow": ("借用", "warn", "borrow"), "borrow_due": ("约定时间", "warn", "borrow"), "borrow_back": ("对方在用", "warn", "borrow"),
    "borrow_return": ("归还", "ok", "borrow"), "borrow_hit": ("挤到对方", "bad", "borrow"), "guest": ("对方超出预留", "warn", "borrow"),
    "owner_oom": ("挤到对方", "bad", "borrow"),
    "guard_restart": ("重开保活", "ok", "keepalive"), "guard_delay": ("延后保活", "mute", "keepalive"),
    "guard_start": ("开保活", "ok", "keepalive"), "guard_yield": ("让出", "mute", "keepalive"), "guard_fail": ("没开成", "bad", "keepalive"),
    "ka_fight": ("保活被拉起", "warn", "keepalive"), "watchdog": ("看门狗", "warn", "keepalive"),
    "start": ("开始", "mute", "queue"), "grant": ("放行", "mute", "queue"), "done": ("完成", "ok", "queue"), "end": ("结束", "mute", "queue"),
    "failed": ("失败", "bad", "queue"), "interrupt": ("中断", "warn", "queue"), "stop": ("暂停", "warn", "queue"), "gate": ("闸门", "bad", "queue"),
    "failcnt": ("撞上限", "bad", "queue"), "void": ("作废", "bad", "queue"), "squeeze": ("被挤掉", "warn", "queue"), "gang": ("成组", "warn", "queue"), "override": ("指令", "warn", "queue"), "worker": ("worker", "mute", "queue"),
    "relaunch": ("拉起", "warn", "queue"), "wait": ("等待", "mute", "queue"), "dep": ("依赖", "bad", "queue"), "qend": ("队列结束", "mute", "queue"),
    "collect": ("采集", "bad", "board"), "hostcheck": ("看板服务", "warn", "board"),     # hostcheck：看板服务所在机的体检提醒（#166）
    "maint": ("计划维护", "mute", "board"),                               # labmon server maint 开关（maint.py，#165）
    "server": ("看板主备", "warn", "board"),                              # 服务租约：这台成为主、转为备用、改本机标记（lease.py，#163）
}


def log_events(evs: list[dict], t: float | None = None) -> None:
    """记一条或几条记录（自用主机、组内服务器、平台操作、配置改动都记在这一个文件里，保留一年）。
    每条 {"kind", "msg"}，可带 "host"（主机或服务器 id）、"hosts"（一次动了几台）、"who"（与哪个人有关）、"detail"（细节，页面上写在正文下面）；
    kind 见 EVENT_KINDS。msg 是给人看的一句话，细节（改了哪几行等）放 detail。"""
    t = time.time() if t is None else t
    for ev in evs:
        append_row(state_dir() / "events.jsonl", {"t": round(t), **{k: v for k, v in ev.items() if v not in (None, [], "")}}, EVENT_KEEP_S, t)


def report(name: str, title: str, evidence: str, project: str | None, host: str | None,
           occurred: float | None) -> str:
    """由看板登记问题报告。记录时间由看板确定，现象与证据按文本保存。"""
    rid = "R-" + uuid.uuid4().hex
    log_events([{"kind": "report", "report_id": rid, "reporter": name,
                 "project": project, "host": host, "occurred": occurred,
                 "msg": f"{rid} · {name}：{title}", "detail": evidence}])
    return rid


def report_headers() -> list[dict]:
    """值守只读报告编号、报告人和提交时间。原文从记录页查看。"""
    return [{k: e[k] for k in ("report_id", "reporter", "t")}
            for e in records(cat="report", since=time.time() - EVENT_KEEP_S)
            if e.get("kind") == "report" and e.get("report_id") and e.get("reporter")]


def schedule_project(e: dict) -> str | None:
    """暂停与优先记录（含已落盘的全局指令）用 target 存项目 id。"""
    target = e.get("target")
    if e.get("kind") == "override" and e.get("okind") in ("pause", "focus") and isinstance(target, str):
        return target
    return None


def cat_of(kind: str | None) -> str:
    """记录的分类（EVENT_CATS 里的键）；不认识的 kind 算队列（worker 新加的事件）。"""
    return EVENT_KINDS.get(kind, ("", "", "queue"))[2]


def records(host: str | None = None, who: str | None = None, cat: str | None = None, q: str | None = None,
            since: float = 0, before: float | None = None, limit: int | None = None, see=None) -> list[dict]:
    """记录：看板的 events.jsonl 加各主机 worker 的 hostlog（补上 host），新的在前。
    host 按 host 或 hosts；who 按 who，老记录没有 who 的按正文里有没有这个名字；cat 见 EVENT_CATS；
    q 按空格分词，每个词都要在正文、细节、项目或指令目标里出现（不分大小写；worker 的事件正文里常没有项目名，搜项目 id 靠后两项）；
    see：看的人能看哪些（users.visible 绑好看的人，#160），在截 limit 之前筛。"""
    rows = load_rows(state_dir() / "events.jsonl")
    hostlogs = () if cat == "report" else sorted((state_dir() / "hostlog").glob("*.jsonl"))
    for f in hostlogs:
        if not host or f.stem == host:
            rows += [{**e, "host": f.stem} for e in load_rows(f)]
    name, words = who and re.compile(rf"(?<![0-9A-Za-z]){re.escape(who)}(?![0-9A-Za-z])"), (q or "").lower().split()
    def keep(e):
        return ((e.get("t") or 0) >= since and (before is None or e["t"] < before)
                and (not host or e.get("host") == host or host in (e.get("hosts") or []))
                and (not who or e.get("who") == who or (not e.get("who") and name.search(e.get("msg") or "")))
                and (not cat or cat_of(e.get("kind")) == cat) and (see is None or see(e))
                and all(w in f"{e.get('msg') or ''} {e.get('detail') or ''} {e.get('project') or ''} {e.get('target') or ''}".lower() for w in words))
    out = sorted((e for e in rows[::-1] if e.get("t") and keep(e)), key=lambda e: -e["t"])      # 同一秒的按写入先后倒过来
    return out[:limit] if limit else out


TELL_KEEP_S = 7 * 86400


def _tell_file():
    return state_dir() / "tell.jsonl"


def tell(project: str, what: str, text: str, t: float | None = None, src: str = "看板页面上") -> str:
    """页面上的改动告诉这个项目的实验会话（#121）：记一行到状态目录的 tell.jsonl（保留 TELL_KEEP_S），told 每轮并进全局快照的 wake，
    labmon wait 报一次、labmon check 也列出。what 是改动的种类（move、rm、restore、pause、resume、config…，进 wake 的 key）；
    text 写做了什么、要接着用怎么办。返回落盘的那句（「<src> MM-DD HH:MM：text」；src 缺省「看板页面上」，看板升级时的迁移另写）。命令行做的不经这里（会话自己做的，或在终端里另有记录）。"""
    from .config import fmt_time                  # config 在顶层 import 本模块（log_events），这里延迟 import 免得成环
    t = time.time() if t is None else t
    msg = f"{src} {fmt_time(t)}：{text}"
    append_row(_tell_file(), {"t": round(t, 3), "what": what, "project": project, "msg": msg}, TELL_KEEP_S, t)
    return msg


def told(t: float | None = None) -> list[dict]:
    """TELL_KEEP_S 内页面上的改动，给全局快照的 wake：{key "page:<what>:<项目>:<毫秒>", projects [项目], msg}。
    不带 host：搬走、移除后那台可能已不在项目快照里，按主机定向会漏掉。"""
    t = time.time() if t is None else t
    return [{"key": f"page:{r['what']}:{r['project']}:{int(r['t'] * 1000)}", "projects": [r["project"]], "msg": r["msg"]}
            for r in load_rows(_tell_file()) if r.get("t") and t - r["t"] <= TELL_KEEP_S and r.get("project") and r.get("what")]
