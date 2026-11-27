"""worker 推演（sim_snapshot / simulate / forecast）。"""
import json

from . import common as _C
from . import queue as _Q
from . import deps as _DP
from . import sched as _S

def sim_snapshot(S, cut, gangs=None, first=None):
    """推演用的快照：从真放行的快照（Worker.snapshot）换成推演口径。与真放行不同、且本来就该不同的只有这几处：
    - 卡的上限与空闲都按 总显存 − labmon 之外的占用 − 给对方留的 算：推演不模拟对方、外来进程何时退出，当它们一直占着；
      keep 已扣进去，不再单算（多卡时“先挑没给对方留的”在推演里不起作用）；内存的 room 与 CPU 的 cpu_room 同理，照搬此刻的（labmon 之外的当常量）；
    - 不放的卡（owner）只留有停不了的保活的（卡的 ka）：它不会自己走，真放行一直不放，推演也不放（#45）；对方超出预留、刚回落是此刻才有的，不模拟（#25）。
      对自己的输出再换一次不变（shadow 在推演里再调 simulate）；
    - 没有旧 gpu_queue.sh 的任务、GPU 进程与外来 CPU 占用（它们是此刻的实况，不在时间轴上）；独占留卡与成组留的资源从空的开始，各自推各自的；
      成组的组（gangs，同 Worker.gsim；不给为没有）照真放行的给，成员最早何时放得下记进 first（gfirst，place 写）；
    - 截止（cut）按时刻在时间轴上判断（真放行由 override_hit 在排队前挡住）；原因只要短的。"""
    return dict(S, cards=[dict(c, total=cap, free=cap, keep=0.0, other=0.0) for c in S["cards"] for cap in [c["total"] - c["other"] - c["keep"]]],
                jobs=[dict(j) for j in S["jobs"]], ext=[], gp=[], cpu=None, holds={}, owner={c["uuid"] for c in S["cards"] if c["ka"]}, cut=cut, brief=True,
                gholds={}, gangs=gangs or {}, gfirst={} if first is None else first)


def simulate(now, snap, queues, focus, cut=None, horizon=None, pol=None, use=None, ends=None, dead=(), on_dep=None, gangs=None, first=None):
    """推演：从 now 起按 admit 的规则一轮轮放行（place，与真放行同一份），在跑的到预计结束时腾出资源、队列接着取下一个，直到排完或超出 horizon（秒）。
    只模拟显存（各卡按估计值记账；借来的主机上有停不了的保活的卡不放，同真放行）、内存、CPU（按估计核数记账）、独占（留卡与回填）、tag 上限、截止前的准入与公平份额；
    缺本地文件、拷本地依赖（含共享盘慢时推迟）、内存闸门、对方超出预留这类此刻才有的原因不模拟。
    - snap：资源快照（Worker.snapshot；sim_snapshot 换成推演口径）；
    - queues：[{key, kind, project, allowed（能用的卡序号，None 不限）, since（队首开始等的时刻）, jobs [job_spec…], par（同时最多几行，缺省 1）, gpar（要卡的行只在这条在跑的少于它时排：并行清单的门槛此刻不给加，forecast 给；None 不限）}]，jobs 不含正在跑的；
      每条队列按清单顺序取窗口（同 next_jobs：还没排上的前 par − 在跑数 行），各行的请求 key 按 run_key（并行清单逐行一个，#147），车道按队列 key（快照 jobs 的 lane）；
    - cut：(截止时刻, 没估时的放不放)：续租截止、约定归还前跑不完的不放（同 override_hit 的 deadline）；
    - pol：调度设置（缺省 POLICY）；use：开始时各项目的 GPU 用量（卡·时，不含在跑的），往后按半衰期衰减，再加上推演里跑的；
    - ends、dead：依赖查的各任务预计结束 {(项目, 任务): 时刻} 与跑失败了的（dep_end；forecast 汇的）。写了依赖的任务等依赖都满足才排，
      满足不了的（等的失败了、不知道何时跑完）那条队列停在它这里；占位任务排上后结束取 dep_end（依赖满足它就放行）；
    - gangs：成组的组（Worker.gsim）：成员放得下时先留资源（place），本机的成员都留好了、别的主机上的成员预计都到点（at）就一起开跑，
      还没到点的到点再看（同真放行的 gang_commit）；别的主机上有成员说不出何时能放行（at 为 None）的不留、排不上。first：成员最早何时放得下（{(请求 key, 任务): 时刻}，place 写）。
    时长不明的任务（没估时也没有同类历史；占位任务等的条件不知道何时满足）排上后不知道何时结束（t1 为 None）：它那条队列停在它这里，
    要等它腾出资源的任务也排不上（不能当它 0 分钟就跑完，否则后面的全挤在同一刻）。
    - on_dep（forecast 给，dict）：排不上的队列里停在依赖上的记进去 {队列 key: 那一行的任务名}：写了依赖的行依赖满足不了或不知道何时满足，
      或前面的占位任务不知道何时放行（它的结束就是依赖何时满足），或成组的行同组还排不上（凑不齐、成环、别的主机上的成员说不出何时能放行：
      等的是同伴，挪走的行带着同样的 gang=，同 after=）。offers 据此不把这条清单当“原处跑不了”（#183、#180）。
    返回 (排上的 [{key, lane（队列 key）, name, project, t0, t1, gpus, after（等到谁跑完才轮到它 [[项目, 任务]]）, gang（成组的组名）}], 排不上的 {队列 key: 原因})。"""
    horizon, pol, use0, ends, gangs = _C.FORECAST_S if horizon is None else horizon, pol or _C.POLICY, use or {}, dict(ends or {}), gangs or {}
    S = sim_snapshot(snap, cut, gangs, first)
    out, freed, last, blocked = [], [], {}, {}
    gstuck = {}             # 队列 key → 停在它这里的成组的行（同组还排不上，place 的 gang_unk）：同依赖，记进 on_dep
    stuck_res = {}          # 队列 key → 最后一次是不是卡在资源上（等时长不明的任务腾资源时据此解释）
    put, since = {q["key"]: set() for q in queues}, {q["key"]: q["since"] for q in queues}   # 各队列已排上的行（下标）、开始等的时刻
    rows = [(y["project"], y["cards"], y["t0"], y["t1"]) for y in S["jobs"]]     # 推演里跑过的（含开始时在跑的）：算用量
    t = now

    def used():
        d = 0.5 ** ((t - now) / pol["hl"])
        u = {p: v * d for p, v in use0.items()}
        for p, v in _S.usage(rows, t, pol["hl"]).items():
            u[p] = u.get(p, 0.0) + v
        return u

    def shadow(r):          # 屏障最早何时放行：从此刻的在跑的起，只排它一个（成组的按它自己最早何时放得下算）
        q = {"key": r["key"], "kind": r["kind"], "project": r["project"], "allowed": r["allowed"], "since": r["since"], "jobs": [dict(r, gang=None, lane=None)]}
        o, _ = simulate(t, S, [q], focus, cut, now + horizon - t, pol, ends=ends, dead=dead)
        return o[0]["t0"] if o else None

    def launch(r, cs):
        d = r["dur"]
        if r.get("durk") and cs and not r["hold"]:    # 按开跑时那几张卡上的 labmon 任务数（含自己）取时长（#67）
            k = sum(1 + sum(1 for y in S["jobs"] if not y["hold"] and c["uuid"] in y["gpus"]) for c in cs) / len(cs)
            d = (r["durk"](k) or {}).get("min")
        e = {"key": r["key"], "lane": r["lane"], "_i": r["_i"], "name": r["name"], "project": r["project"], "t0": t, "gpus": [c["uuid"] for c in cs],
             "t1": _DP.dep_end(_DP.dep_keys(r), t, ends, dead) if r["hold"] and _DP.dep_keys(r) else t + d * 60 if d is not None else None,
             "after": [f for f in freed][:3] if t > r["since"] else [], **({"gang": r["gang"]} if r.get("gang") else {})}
        ends[(r["project"], r["name"])] = e["t1"]       # 同一遍里排在它后面、等它的任务就能看到
        S["jobs"].append(dict(e, **_S.acct_job(r, cs, t, e["t1"], S["cpu_cap"], S["cards"])))
        rows.append((r["project"], r["cards"], t, e["t1"]))
        out.append(e)
        put[r["lane"]].add(r["_i"])

    def place_(r):
        if r.get("gpar") and r["ngpu"] > 0 and sum(1 for y in S["jobs"] if (y.get("lane") or y["key"]) == r["lane"]) >= r["gpar"]:
            return "并行清单的门槛此刻不给加要卡的行（卡吃满）", False   # forecast 的 gpar：逐行放行时按已排上的核，同一轮放了一行就到顶；不用卡的照排
        cs, why, res = _S.place(r, S, t)
        r["code"] = S["code"]                     # admit 按它认成组留下的（同 Worker.place）
        gstuck.pop(r["lane"], None)
        if cs is not None:
            return launch(r, cs)
        stuck_res[r["lane"]] = res
        if S["code"] == "gang_unk":
            gstuck[r["lane"]] = r
        return why, res

    while True:
        busy, wake, reqs = {}, [], []
        for y in S["jobs"]:
            busy[y.get("lane") or y["key"]] = busy.get(y.get("lane") or y["key"], 0) + 1
        for q in queues:
            k = q["key"]
            win = [i for i in range(len(q["jobs"])) if i not in put[k]][:max(0, (q.get("wpar") or q.get("par") or 1) - busy.get(k, 0))]   # 窗口（同 next_jobs；wpar 为限流缩小的窗口上限）
            if win:
                blocked.pop(k, None)
            for i in win:
                r = dict(q["jobs"][i], key=_Q.run_key(q, q["jobs"][i]["name"]), lane=k, _i=i, kind=q["kind"], project=q["project"], since=since[k], allowed=q["allowed"],
                         gpar=q.get("gpar"))
                r["charge"] = r["cards"] * (r["dur"] or 0.0) / 60
                d = _DP.dep_end(_DP.dep_keys(r), t, ends, dead) if _DP.dep_keys(r) else None
                if _DP.dep_keys(r) and d is None:     # 依赖满足不了或不知道何时满足：它占着窗口的这一格（串行清单就是停在它这里）
                    blocked.setdefault(k, r)
                elif d and d > t:                 # 依赖还没满足：不参加这一轮，到时再看
                    wake.append(d)
                else:
                    if d:                         # 等依赖的时间不算排队（同真放行：依赖满足后才开始等）
                        since[k] = r["since"] = max(r["since"], d)
                    reqs.append(r)
        last.update({r["lane"]: why for r, why, _ in _S.admit(reqs, focus, pol, used(), t, place_, shadow)})
        # 成组：这一轮没重新留的（让位给屏障）放掉；本机的成员都留好了、别的主机上的预计都到点（at）了就一起开跑，没到点的到点再看（同 Worker.gang_commit）
        S["gholds"] = {k: h for k, h in S["gholds"].items() if h["seen"] >= t}
        for gk, g in sorted(gangs.items()):
            loc = sorted(g["local"])
            hs = [S["gholds"].get(k) for k, _ in loc]
            if not loc or any(h is None or h["name"] != n for h, (_, n) in zip(hs, loc)):
                continue
            if g["at"] <= t:
                for h in hs:
                    del S["gholds"][h["key"]]
                    launch(h["x"], h["cards"])
            else:
                wake.append(g["at"])
        nxt = [y["t1"] for y in S["jobs"] if y["t1"] is not None] + wake
        if not nxt or min(nxt) > now + horizon:
            break
        t = max(t, min(nxt))
        done = [y for y in S["jobs"] if y["t1"] is not None and y["t1"] <= t]
        S["jobs"] = [y for y in S["jobs"] if not (y["t1"] is not None and y["t1"] <= t)]
        freed = [[y["project"], y["name"]] for y in done if not y["hold"]]
        for y in done:
            if (y.get("lane") or y["key"]) in since:
                since[y.get("lane") or y["key"]] = t
    never = {y.get("lane") or y["key"]: y for y in S["jobs"] if y["t1"] is None}       # 排上了但不知道何时结束的：它那条队列的后面排不上
    unknown = "、".join(y["name"] for y in never.values() if not y["hold"])
    stuck = {}
    for q in queues:
        if len(put[q["key"]]) >= len(q["jobs"]):
            continue
        y, b, g = never.get(q["key"]), blocked.get(q["key"]), gstuck.get(q["key"])
        if on_dep is not None and (y["hold"] if y else b or g):
            on_dep[q["key"]] = (y or b or g)["name"]
        stuck[q["key"]] = ("前面的占位任务不知道何时放行" if y["hold"] else "前面的 %s 不知道要跑多久（没估时也没有同类历史）" % y["name"]) if y \
            else "%s 等的 %s 跑不成或不知道何时跑完" % (b["name"], _DP.dep_names(_DP.dep_keys(b), b["project"])) if b \
            else "%s 是成组 %s 的，同组还排不上（凑不齐、成环，或别的主机上的成员说不出何时能放行）" % (g["name"], g["gang"]) if g \
            else "等 %s 腾出资源，它不知道要跑多久" % unknown if unknown and stuck_res.get(q["key"]) else \
            "推演到 %d 天后还没轮到" % (horizon / 86400) if nxt else "一直排不上，" + last.get(q["key"], "原因不明")
    return out, stuck


class ForecastMixin:

    # ---- 推演 ----
    def job_spec(self, n, h, est, dur, durk=None, cut=None):
        """一个任务在准入核心 place 眼里的样子（真放行经 as_job，推演里清单上的每个任务都这么拼）：h 资源提示（ngpu、excl、tag），
        est 资源估计（estimate：vram 每张卡 GiB 与来源 src、ram GiB 与来源 ram_src，cpus 核、cpu_src 来源），dur 预计时长（分钟，不知道为 None）。占位任务不独占、不占卡、不记 CPU，
        依赖（deps、deps_any）带着：推演里等它们满足才排，占位任务的结束取 dep_end。
        durk（推演给）：开跑时卡上有 k 条 labmon 任务（含自己）时的预计时长 durk(k)（duration 的结果，Worker.dur_k）；不给时用 dur。
        cut：截止前的准入用的时长（duration 的 cut，已含余量，#139）；不给时按 dur × DUR_MARGIN。
        成组的带 gang（组名，place 按它留资源）：dur 是组里最长的（截止与回填按整组），durk 是自己的（推演里开跑后何时结束）。"""
        hold = _C.is_hold(n)
        return {"name": n, "ngpu": h["ngpu"], "vram": est["vram"], "src": est["src"], "ram": est["ram"], "ram_src": est["ram_src"], "excl": None if hold else h["excl"],
                "cpus": 0 if hold else est["cpus"], "cpu_src": est["cpu_src"],
                "tag": h["tag"], "dur": dur, "durk": durk, "cut": dur and dur * _C.DUR_MARGIN if cut is None else cut, "hold": hold, "deps": h["deps"], "deps_any": h["deps_any"], "gang": None if hold else h.get("gang"),
                "cards": 0.0 if hold else self.cards(h["ngpu"], est["vram"], h["excl"])}

    def as_job(self, r):
        """补全过的请求（prep）→ place 认的任务：job_spec 加 key、project 与能用的卡（清单登记的 --gpus）。"""
        return dict(self.job_spec(r["name"], r, r["est"], r["dur"], cut=r.get("cut")), key=r["key"], project=r["project"],
                    allowed=r["q"]["gpus"] if r["kind"] == "queue" else None)

    def cutoff(self):
        """推演里的截止：最早的 deadline 指令 (时刻, 没估时的放不放)；没有为 None。真放行里截止由 override_hit 挡。"""
        dl = sorted((o["at"], o.get("target") == "borrow") for o in self.ovr if o["kind"] == "deadline" and o.get("at"))
        return dl[0] if dl else None

    def shadow(self, r, now, pol, focus, limits):
        """屏障（等满 wait_cap 还卡在资源上的请求）预计最早何时放行：只排它一个，在跑的按预计结束腾出资源（simulate）；推不出为 None。
        成组的按它自己最早何时放得下算（不等同伴）。"""
        q = {"key": r["key"], "kind": r["kind"], "project": r["project"], "since": r["since"],
             "allowed": r["q"]["gpus"] if r["kind"] == "queue" else None, "jobs": [dict(self.as_job(r), gang=None)]}
        out, _ = simulate(now, self.snapshot(limits), [q], focus, self.cutoff(), pol=pol, ends=self.ends, dead=self.dead)
        return out[0]["t0"] if out else None

    def forecast(self, queues, reqs, limits, now, pol):
        """推演（simulate）这台主机往后怎么排，写进状态的 plan：项目看板的预计开始与完成、工作台与项目页的时间线都用它。
        被持久的临时指令挡住的队列（暂停、drain、续租截止前跑不完、只给别人用）不推演，看板照旧标暂停；借来的主机上对方此刻超出预留的照常推演
        （真放行挡、推演不模拟，同 simulate；override_hit 的 lasting）；有停不了的保活的卡推演里同样不放（sim_snapshot，#45）。
        输入（在跑的、各队首、清单、历史、临时指令、看板写来的预计结束、调度设置）变了就重算，没变也每 FORECAST_EVERY 秒重算一次。
        依赖查的各任务预计结束（dep_end 的 ends，存进 self.ends 给 req_dur 与等待原因）在这里汇：已跑完的（dep_state 认的）为 0，
        再依次盖上别的主机 plan 里的（peers）、本机在跑的与推演出的。被等的任务可能排在等它的任务后面才推演到，有写了依赖的任务时推两遍，
        第二遍代入第一遍的结果（在跑的占位任务也按它重算预计结束）。"""
        self.load_eta()
        wait = [r for r in reqs if r["key"] not in self.running]
        base_ends = self.dep_base_ends()
        peer_key = tuple(sorted((k, None if v is None else round(v / 60)) for k, v in base_ends.items() if v != 0))   # 跨主机依赖预计结束（按分取整）：变了当轮重推（#166）
        key = (tuple(sorted((r["key"], r["name"], bool(r["held"])) for r in wait)), tuple(sorted((k, j["name"]) for k, j in self.running.items())),
               tuple(q["key"] for q in queues),
               self.durs_key, json.dumps(self.ovr, sort_keys=True), self.eta_m, json.dumps(pol, sort_keys=True), frozenset(self.dep_idx.items()),
               peer_key,
               json.dumps(self.todo),         # 还会跑的行（删了 .failed 重跑时清单与队首都没变）：plan 与写进状态的 todo 同一轮对得上（#25）
               frozenset(self.other["ka"]),   # 借来的主机上有停不了的保活的卡（推演不放它，#45）：出现、消失当轮重推
               # 成组：别的主机上的成员预计何时能一起开跑（此刻就能的记 0，不随时钟变）、本机成员在不在队首
               tuple((gk, g["at"] and (0 if g["at"] <= now else round(g["at"] / 60)), tuple(sorted((k, n, e and e <= now) for (k, n), e in g["local"].items())))
                     for gk, g in sorted(self.gsim.items())))
        cards, shut = self.local_cards()         # 并行清单加行的门槛（par_gate）：推演里按此刻的结论当常量（#147）
        lvl = {q["key"]: self.par_level(q["key"], now) for q in queues if (q.get("par") or 1) > 1}   # 补位到的行数（#176）
        key += (bool(shut), tuple(cards or ()), tuple(sorted(lvl.items())))
        if key == self.plan_key and now - self.plan["t"] < _C.FORECAST_EVERY:
            return
        self.plan_key = key
        # 只排除被持久的挡法挡住的队列：对方此刻超出预留（guest_block）真放行挡、推演不模拟，照常排（#25）
        # 并行清单（#147）窗口里只是某一行截止前跑不完的不排除整条：截止由推演逐行判（simulate 的 cutoff），别的行照排
        held = {_C.lane_key(r) for r in wait if r["held"] for h in [self.override_hit(r, lasting=True)]
                if h and not (h[0] == "deadline" and ((r.get("q") or {}).get("par") or 1) > 1)}
        base = self.snapshot(limits)
        qs, info = [], {}
        heads, lanes = {}, {}                     # 各车道等着的行里开始等得最早的（并行清单窗口里有几行，#147）、在跑的行
        for r in sorted(wait, key=lambda r: -r["since"]):
            heads[_C.lane_key(r)] = r
        for j in self.running.values():
            lanes.setdefault(_C.lane_key(j), []).append(j["name"])
        for q in queues:
            k = q["key"]
            if k in held or (k not in lanes and k not in heads) or k not in self.qlists:
                continue
            cur = lanes.get(k, [])
            marks, pend = _Q.rows_left(q["state"], self.qlists[k][1], cur)
            if marks is None:
                continue
            # 正在跑的队列：下一个会不会被临时指令挡住（截止前跑不完由推演按时刻判断）
            hit = cur and pend and self.override_hit(dict(pend[0][1], q=q, key=k, kind="queue", name=pend[0][0], project=q["project"]), lasting=True)
            if hit and hit[0] != "deadline":
                pend = []
            jobs = [self.job_spec(n, h, self.qest.get(k, {}).get(n) or _S.estimate(self.hist, dict(h, name=n, project=q["project"])),
                                  self.req_dur(x), self.dur_k(dict(h, name=n, project=q["project"], root=q["root"], queue=q["jobs"])), self.req_dur(x, cut=True))
                    for n, h in pend for x in [{"key": k, "name": n, "project": q["project"], "gang": None if _C.is_hold(n) else h["gang"]}]]
            par, wpar, allowed, gpar = q.get("par") or 1, None, q["gpus"], None
            if par > 1 and cur:                   # 已有行在跑：再加的行要过门槛（同真放行 place 里的 par_gate）
                if shut and self.throttle(now):   # 容器在限流：不用卡的行也不加，按此刻在跑的行数排（保留 par 供 run_key 对齐 starts）
                    wpar = max(1, len(cur))
                elif shut or (cards is not None and not [i for i in cards if allowed is None or i in allowed]):
                    gpar = max(1, len(cur), lvl.get(k, 0))   # 卡吃满：要卡的行只补到此刻与近来跑到过的行数（真放行补位照补、不往上加），不用卡的照窗口排
                elif cards is not None:           # 要卡的行只放在门槛给的卡上
                    allowed = [i for i in cards if allowed is None or i in allowed]
            qs.append({"key": k, "kind": "queue", "project": q["project"], "allowed": allowed, "par": par, "gpar": gpar,
                       **({"wpar": wpar} if wpar is not None else {}),
                       "since": heads[k]["since"] if k in heads else now, "jobs": jobs})
            info[k] = {"root": q["root"], "queue": q["jobs"], "project": q["project"], "kind": "queue"}
        for r in wait:
            if r["kind"] == "slot" and _C.lane_key(r) not in held:
                qs.append({"key": r["key"], "kind": "slot", "project": r["project"], "allowed": None, "since": r["since"],
                           "jobs": [self.as_job(r)]})
        for j in self.running.values():
            info.setdefault(_C.lane_key(j), {"root": None, "queue": None, "project": j.get("project"), "kind": j.get("kind", "queue")})
        for r in wait:
            info.setdefault(r["key"], {"root": None, "queue": None, "project": r["project"], "kind": r["kind"]})
        focus = {o.get("target") for o in self.ovr if o["kind"] == "focus"}
        use = _S.usage(self.hrows + self.peer, now, pol["hl"])
        waits = {(_Q.run_key(q, x["name"]), x["name"]): _DP.dep_keys(dict(x, project=q["project"])) for q in qs for x in q["jobs"]}     # 写了依赖的任务
        waits.update({(k, j["name"]): _DP.dep_keys(j) for k, j in self.running.items()})
        waits = {k: v for k, v in waits.items() if v}
        gangs = self.gsim
        local_ends, run_ends = {}, None
        # 有依赖或成组未知成员时从干净的 base_ends 迭代至收敛（最多 6 遍，防多跳链或资源反馈导致 self.ends 与 starts 不自洽，#166）
        max_pass = 6 if (waits or any(e is None for g in gangs.values() for e in g["local"].values())) else 1
        for i in range(max_pass):
            ends_in = dict(base_ends)
            ends_in.update(local_ends)
            ends_in.update(((y["project"], y["name"]), y["t1"]) for y in base["jobs"])
            on_dep = {}
            first = {}                            # 成组成员最早何时放得下（place 记）：写进 gang.json 给同伴，只看本机，几台互相引用也不会越推越晚
            out, stuck = simulate(now, base, qs, focus, self.cutoff(), pol=pol, use=use, ends=ends_in, dead=self.dead, on_dep=on_dep,
                                  gangs=gangs, first=first)
            new_local = {(e["project"], e["name"]): e["t1"] for e in out}
            new_run = {(y["project"], y["name"]): y["t1"] for y in base["jobs"]}
            ends_out = dict(base_ends)
            ends_out.update(new_run)
            ends_out.update(new_local)
            self.ends = ends_out
            # 本机成员还不知道何时放得下的（上一轮推演没有它、不在队首）：下一遍按这一遍里它最早何时放得下算，同组的才留得住
            unk = any(e is None for g in gangs.values() for e in g["local"].values())
            if i + 1 >= max_pass or (not unk and new_local == local_ends and new_run == run_ends):
                break
            local_ends, run_ends = new_local, new_run
            gangs = {gk: dict(g, local={x: first.get(x) if e is None else e for x, e in g["local"].items()}) for gk, g in gangs.items()}
            base = self.snapshot(limits)
        self.plan = {"t": now, "lanes": self.plan_lanes(base["jobs"], out, stuck, info, waits, on_dep)}
        self.starts = {(e["key"], e["name"]): round(e["t0"]) for e in out}
        self.gang_eta = first
        try:                                      # 只是提示：出错不拖垮放行与推演；沿用上一次的（最多 STATUS_STALE_S），免得建议消失一轮再出现（#25）
            self.offer, self.offer_t = self.offers(qs, base, now, pol, focus, use, info, {(r.get("root"), r.get("queue")) for r in wait if r["held"]}), now
        except Exception as e:  # noqa: BLE001
            if now - self.offer_t > _C.STATUS_STALE_S:
                self.offer = []
            if self.noted.get("_offers") != str(e):
                self.wlog("算能接别处的任务出错：%r" % e)
                self.noted["_offers"] = str(e)

    def plan_lanes(self, run, out, stuck, info, waits, on_dep=None):
        """推演结果（simulate 的排上的与排不上的，加开始时在跑的 run）→ 状态里 plan 的车道 [{root, queue, project, kind, jobs, stuck, stuck_dep}]。
        forecast 写状态、offers 读假设推演里本机各车道何时跑完都经它。info：请求 key → {root, queue, project, kind}；waits：写了依赖的任务；
        on_dep：simulate 记的停在依赖上的队列 {key: 那一行}，写成车道的 stuck_dep（别的主机的 offers 据此不当“原处跑不了”，#183）。"""
        idx = {g["uuid"]: g["i"] for g in self.info.get("gpus") or []}
        lanes = {}
        for e in [dict(x, run=True) for x in run] + out:
            ln = lanes.setdefault(e.get("lane") or e["key"], dict(info[e.get("lane") or e["key"]], jobs=[], stuck=None, stuck_dep=None))
            x = {"name": e["name"], "t0": round(e["t0"]), "t1": e["t1"] and round(e["t1"]), "gpus": [idx.get(u) for u in e["gpus"]],
                 **({"run": True} if e.get("run") else {"after": e["after"]} if e.get("after") else {}),
                 **({"gang": e["gang"]} if e.get("gang") else {})}     # 成组的组名：同组的在各台的推演里一起开跑
            wf = waits.get((e["key"], e["name"]))
            if wf:
                x["deps"] = [[p, n] + (["any"] if a else []) for p, n, a in wf]     # 它等谁（看板画依赖连线、写“等 X、Y 跑完”）
            ln["jobs"].append(x)
        for k, why in stuck.items():
            ln = lanes.setdefault(k, dict(info[k], jobs=[], stuck=None, stuck_dep=None))
            ln["stuck"], ln["stuck_dep"] = why, (on_dep or {}).get(k)
        return list(lanes.values())
