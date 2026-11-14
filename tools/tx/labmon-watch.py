#!/usr/bin/env python3
"""TX 上的看门狗（#165）：经隧道探 dashboard-host 看板服务的 /healthz，异常持续 15 分钟推到手机，恢复再推一条。

由 systemd 定时器每 5 分钟跑一次（labmon-watch.timer → labmon-watch.service，装法见 docs/deploy.md「TX」）：

    python3 -I labmon-watch.py --url http://127.0.0.1:<公网入口端口>/healthz [--name "DASHBOARD-HOST 看板"] [--notify notify.toml] [--state state.json]

- 异常有三种：连不上（超时 10 秒、不是 200、回的不是那四项）、primary 是 false（服务租约在别的看板服务手里，lease.flag；还没跑过租约的 null 不算）、
  last_collect 为空或已过 STALE_S。从第一次看到异常起算（采集停了的从最后一次采集起算），满 AFTER_S 推一条：哪一种、从几点起；
  恢复时推一条：断了多久。异常中途换了种类不重推。
- 计划维护（labmon server maint）：记住最后一次探到的 maint_until（维护时服务多半连不上），这之前不推；
  维护结束后还异常的，从维护结束起再算 AFTER_S。
- 推送：import 同目录下拷来的 push.py（仓库里是 labmon/push.py，与看板服务同一份），读 notify.toml（与 dashboard-host 上那份同一种写法；
  systemd 经 LoadCredential 交给它，缺省取 $CREDENTIALS_DIRECTORY/notify.toml）。推送失败这一轮不算推过，下一轮再推；恢复那条同样。
- 状态记在 --state（缺省 $STATE_DIRECTORY/state.json，systemd 的 StateDirectory=labmon-watch 即 /var/lib/labmon-watch/）。
- 不推的时候也在标准输出打一行（进 journal：journalctl -u labmon-watch）。

只用标准库；要 Python 3.11 起（push.py 用 tomllib；TX 自带 3.12）。不 import labmon 包：TX 上不装 labmon。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

AFTER_S = 15 * 60                  # 异常持续这么久才推
STALE_S = 15 * 60                  # 最后一次采集超过这么久算采集停了
TIMEOUT_S = 10
KEYS = {"ok", "primary", "last_collect", "maint_until"}
WHAT = {"down": "连不上", "standby": "不是主（服务租约在别处）", "stale": "采集停了"}


def probe(url: str) -> dict | None:
    """GET /healthz：回四项的 JSON 就返回它，别的（连不上、超时、不是 200、形状不对）为 None。"""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            d = json.loads(r.read())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and set(d) == KEYS and d.get("ok") is True else None


def judge(h: dict | None, now: float) -> tuple[str | None, float]:
    """这一次探到的是哪种异常（None 为正常）与它从何时算起（采集停了的从最后一次采集起，别的从现在）。"""
    if h is None:
        return "down", now
    if h.get("primary") is False:
        return "standby", now
    lc = h.get("last_collect")
    if not isinstance(lc, (int, float)) or now - lc > STALE_S:
        return "stale", lc if isinstance(lc, (int, float)) else now
    return None, now


def step(st: dict, h: dict | None, now: float) -> tuple[dict, str | None]:
    """一轮：按上一轮的状态 st 与这次探到的 h，返回新状态与要推的一句话（None 为不推）。纯函数，便于离线测。
    st：bad（异常种类，正常为 None）、since（异常从何时算起）、alerted（已推过异常）、maint（最后一次探到的 maint_until）。
    推送成功后由调用方把 pending 那件事记上（commit），失败不记，下一轮再推。"""
    st = dict(st)
    old = st.get("maint") or 0
    if h is not None:
        m = h.get("maint_until")
        if m:
            st["maint"] = m
        elif old > now:                  # 提前结束（labmon server maint off）：从这一刻起算
            st["maint"] = now
        # 已经到期的照旧留着：维护结束后还异常的，从结束起再算 AFTER_S（服务回的已是 null）
    maint = st.get("maint") or 0
    kind, since = judge(h, now)
    if kind is None:
        if st.get("alerted"):
            took = now - (st.get("since") or now)
            return {**st, "pending": "ok"}, f"已恢复：前后约 {_dur(took)}（{WHAT.get(st.get('bad'), st.get('bad'))}）"
        return {**st, "bad": None, "since": None, "alerted": False, "pending": None}, None
    if not st.get("bad"):
        st["bad"], st["since"] = kind, since
    elif kind != st["bad"] and not st.get("alerted"):          # 还没推过：按最新的种类说；推过了不重推
        st["bad"] = kind
    st["pending"] = None
    if st.get("alerted") or now < maint:
        return st, None
    start = max(st["since"], maint)
    if now - start < AFTER_S:
        return st, None
    st["pending"] = "bad"
    return st, f"{WHAT[st['bad']]}：从 {time.strftime('%m-%d %H:%M', time.localtime(st['since']))} 起，已 {_dur(now - st['since'])}"


def commit(st: dict) -> dict:
    """推送成功：异常那条记成已推；恢复那条把状态清掉。"""
    if st.get("pending") == "bad":
        return {**st, "alerted": True, "pending": None}
    if st.get("pending") == "ok":
        return {**st, "bad": None, "since": None, "alerted": False, "pending": None}
    return st


def _dur(s: float) -> str:
    m = int(s // 60)
    return f"{m} 分钟" if m < 120 else f"{m / 60:.1f} 小时"


def _push_mod(path: Path):
    spec = importlib.util.spec_from_file_location("labmon_push", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default_push() -> Path:
    here = Path(__file__).resolve().parent
    for p in (here / "push.py", here.parent.parent / "labmon" / "push.py"):     # 装在 TX 上是同目录；在仓库里是 labmon/push.py
        if p.exists():
            return p
    return here / "push.py"


def main(argv: list[str] | None = None, now: float | None = None, fetch=probe, send=None) -> int:
    ap = argparse.ArgumentParser(prog="labmon-watch", description="探看板服务的 /healthz，异常持续 15 分钟推到手机（#165）")
    ap.add_argument("--url", required=True, help="http://127.0.0.1:<公网入口端口>/healthz：经隧道，两头同一个端口")
    ap.add_argument("--name", default="labmon 看板", help="推送标题里怎么称呼它")
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    ap.add_argument("--notify", default=str(Path(cred) / "notify.toml") if cred else None, help="notify.toml（缺省 $CREDENTIALS_DIRECTORY/notify.toml）")
    sd = os.environ.get("STATE_DIRECTORY")
    ap.add_argument("--state", default=str(Path(sd.split(":")[0]) / "state.json") if sd else None, help="状态文件（缺省 $STATE_DIRECTORY/state.json）")
    ap.add_argument("--push-lib", default=str(_default_push()), help="push.py 的路径（缺省同目录下的）")
    a = ap.parse_args(argv)
    if not a.state:
        ap.error("要给 --state（systemd 下由 StateDirectory 给）")
    now = time.time() if now is None else now
    sf = Path(a.state)
    try:
        st = json.loads(sf.read_text())
    except (OSError, ValueError):
        st = {}
    st, msg = step(st, fetch(a.url), now)
    if msg:
        if send is None:
            P = _push_mod(Path(a.push_lib))

            def send(m, title):
                conf = P.load(Path(a.notify)) if a.notify else None
                if conf is None:
                    raise OSError(f"没配推送：{a.notify or '没给 --notify'} 里要写 push = \"bark\" 或 \"ntfy\"")
                try:
                    P.one(conf, m, title)
                except P.ERRORS as e:
                    raise OSError(P.err(e)) from None
        try:
            send(msg, a.name)
            st = commit(st)
            print(f"已推：{a.name} {msg}", flush=True)
        except (OSError, ValueError) as e:
            print(f"推送失败，下一轮再推：{a.name} {msg}（{e}）", flush=True)
    else:
        m = st.get("maint") or 0
        print(f"{a.name}：" + (f"异常（{WHAT[st['bad']]}），{'已推过' if st.get('alerted') else '还没到推的时候'}" if st.get("bad") else "正常")
              + (f"；计划维护到 {time.strftime('%m-%d %H:%M', time.localtime(m))}（不报）" if m > now else ""), flush=True)
    tmp = sf.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(sf)
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        sys.exit("labmon-watch 要 Python 3.11 起（push.py 用 tomllib）")
    sys.exit(main())
