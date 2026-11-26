"""worker 任务依赖（after= / afterany=）与成组起跑（gang=）。"""
import json
import os

from . import common as _C
from . import pure as _P
from . import queue as _Q

def gang_file(host):
    """主机 host 的 gang.json（成组成员此刻怎样，Worker.gang_publish 写）；读不到、写到一半的为 None。"""
    try:
        d = json.loads(_C.read(os.path.join(_C.HOME, "hosts", host, "gang.json"), "") or "")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def gang_bad(ms):
    """一组成员（Worker.gang_sync 的 mem 项 {(项目, 任务): {host, n…}}）人数对不上的说法：各行写的人数不一样、没写人数、凑不齐、多了；对得上为 None。"""
    ns = {v["n"] for v in ms.values()}
    n = next(iter(ns)) if len(ns) == 1 else None
    names = "、".join(sorted(x if v["host"] is None else "%s（%s）" % (x, v["host"]) for (_, x), v in ms.items()))
    return ("人数写得不一样（%s）" % "、".join(sorted(str(x) if x else "没写" for x in ns)) if len(ns) > 1
            else "没写人数（%s）" % names if not n
            else "凑不齐：要 %d 个，只见到 %d 个（%s）" % (n, len(ms), names) if len(ms) < n
            else "多了：要 %d 个，见到 %d 个（%s）；同一个组名同时只能有这么多行没跑完，几组就写几个组名" % (n, len(ms), names) if len(ms) > n
            else None)


def gang_cur(recs, k):
    """gang.json 的 members 里成员 k=(项目, 任务) 此刻的记录（wait、ready、run）；没有为 None。"""
    return next((e for e in recs or [] if (e.get("project"), e.get("name")) == k and e.get("state") in ("wait", "ready", "run")), None)


def gang_last(recs, project, name):
    """成员 (project, name) 最近一次结束的时刻（gang.json 里 done、fail、stop 记录的 t1，按它所在那台的时钟）；没有为 0。
    开跑时记进 gang_with 的第三项：同一次开跑按“谁开跑时已经看到了谁的哪次结束”认，不比两台的时刻（停下重排后几秒内又一起开跑也分得清，不怕时钟偏差）。"""
    return max((e.get("t1") or 0 for e in recs or [] if e.get("project") == project and e.get("name") == name
                and e.get("state") in ("done", "fail", "stop")), default=0)


def gang_peer(recs, project, name, t0, seen):
    """同伴在本机这一次开跑里怎样：fail、stop、done、run（几条都对得上时按这个先后）；还没跟着开跑为 None。
    算这一次的：开跑时刻与 t0 相差 GANG_JOIN_S 以内的 run，和本机开跑时没见过的结束（t1 晚于 seen，即开跑时记下的 gang_last；之前的结束是上一次的）。
    Worker.gang_watch 用：recs 是同伴那台 gang.json 的 members（本机的同伴取 gang_recs）。"""
    got = {e["state"] for e in recs or [] if e.get("project") == project and e.get("name") == name
           and (abs((e.get("t0") or 0) - t0) <= _C.GANG_JOIN_S if e.get("state") == "run" else
                e.get("state") in ("done", "fail", "stop") and (e.get("t1") or 0) > seen)}
    return next((s for s in ("fail", "stop", "done", "run") if s in got), None)


def gang_until(S, x, t):
    """成组成员 x 留的资源要留到何时（组里预计何时能一起开跑）：别的主机上的成员里最晚的（S["gangs"] 的 at），本机别的成员已留好的（S["gholds"]）算 t、
    没留好的取它的预计（local：上次推演里最早何时放得下，这一轮在队首、没被挡住的为现在）；有一个说不出为 None（不留）。place 与 Worker.gang_soon 用。"""
    g = (S.get("gangs") or {}).get((x["project"], x["gang"]))
    if not g or g["at"] is None:
        return None
    ts, gh = [t, g["at"]], S.get("gholds") or {}
    for (k, n), eta in g["local"].items():
        if (k, n) != (x["key"], x["name"]):
            h = gh.get(k)
            ts.append(t if h and h["name"] == n else eta)
    return None if None in ts else max(ts)


def dep_keys(r):
    """一行的依赖（hints 的 deps、deps_any，可带 项目/ 前缀）→ [(项目, 任务, 失败也算)]：不带前缀的是这一行自己的项目（r["project"]）。"""
    return [_P.job_key(a, r["project"]) + (False,) for a in r.get("deps") or []] + [_P.job_key(a, r["project"]) + (True,) for a in r.get("deps_any") or []]


def dep_end(keys, t0, ends, dead=()):
    """依赖（dep_keys）预计何时都满足：等的任务预计结束里最晚的，不早于 t0。推演（simulate：何时能开始，占位任务何时放行）与
    运行中、排队的占位任务的预计时长（Worker.req_dur，run_end 经它）都调这一处；看板取推演里的结果，不另算。
    ends {(项目, 任务): 预计结束}（forecast 汇的）：已跑完的为 0，排上了但不知道何时结束的为 None；dead 是跑失败了的（afterany 的算已结束）。
    有一项满足不了或不知道（after= 的失败了、哪里都找不到、时长不明）为 None。"""
    xs = [t0]
    for p, n, anyok in keys:
        if (p, n) in dead and anyok:
            continue
        x = None if (p, n) in dead else ends.get((p, n))
        if x is None:
            return None
        xs.append(x)
    return max(xs)


def dep_names(keys, project):
    """依赖的文字：本项目的写任务名，别的项目写 项目/任务。"""
    return "、".join(n if p == project else "%s/%s" % (p, n) for p, n, _ in keys)


class DepsMixin:

    def list_state(self, queues, reqs=()):
        """本机各清单此刻的状态（依赖按它认，也写进状态的 todo 给别的主机）：标完的 self.marks {(项目, 任务): done|failed}
        （几条清单说法不一的记 "?"，dep_state 看用量记录的最后一次）；还会跑的 self.todo，每条清单一项
        {project, root, queue, jobs [在跑的在前、其余按清单顺序], deps {任务: [[项目, 任务]…]}（只列写了依赖的）, gang {任务: [组名, 人数]}（只列成组的，
        在跑的也列）, held, par（并行清单才有，#147：依赖图里窗口内的行不算互相排队）}，在跑、在等的单元与登记已撤的在跑任务各一项（root、queue 为 null）。别的主机按它认谁还会跑（dep_state）、组里有谁（gang_sync），
        offers 按它取别的主机上各清单还没跑的行。本机的成组成员另记进 self.gang_local（gang_sync、gang_publish 用）。
        暂停、drain 的清单照样列（任务还会跑），held 写 paused（状态目录有 paused）、pause 或 drain（项目暂停优先于主机 drain：用户有意停的，
        offers 不当成可挪的活，#25），等它的任务的等待原因据此写清。"""
        marks, todo, seen, gl = {}, [], set(), {}
        for q in queues:
            c = self.qlists.get(q["key"])
            if not c:
                continue
            seen.add(q["key"])
            run = self.lane(q["key"])             # 在跑的（并行清单有几行，#147）
            cur = [j["name"] for j in run]
            ms, left = _Q.rows_left(q["state"], c[1], cur)
            ms = ms or set()
            ln = {"project": q["project"], "root": q["root"], "queue": q["jobs"], "jobs": list(cur), "deps": {}, "gang": {}, **({"pool": 1} if q.get("pool") else {}),
                  **({"par": q["par"]} if (q.get("par") or 1) > 1 else {}),
                  "held": "paused" if "paused" in ms else (self.override_hit({"project": q["project"]}, ("pause",))
                                                           or self.override_hit({"project": q["project"]}, ("drain",)) or [None])[0]}
            for n, _ in c[1]:
                v = "done" if n + ".done" in ms else "failed" if n + ".failed" in ms else None
                if v:
                    marks[(q["project"], n)] = v if marks.get((q["project"], n), v) == v else "?"
            for n, h in [(j["name"], j) for j in run] + left:
                if n not in cur:
                    ln["jobs"].append(n)
                    d = dep_keys(dict(h, project=q["project"]))
                    if d:
                        ln["deps"][n] = [[p, x] for p, x, _ in d]
                if h.get("gang") and not _C.is_hold(n):
                    ln["gang"][n] = [h["gang"], h.get("gn")]
                    gl.setdefault((q["project"], n), {"key": _Q.run_key(q, n), "lane": q["key"], "gang": h["gang"], "n": h.get("gn"), "root": q["root"], "queue": q["jobs"]})
            if ln["jobs"]:
                todo.append(ln)
        extra = [j for j in self.running.values() if _C.lane_key(j) not in seen] + [r for r in reqs if r["kind"] == "slot" and r["key"] not in self.running]
        todo += [{"project": j.get("project"), "root": None, "queue": None, "jobs": [j["name"]], "deps": {},
                  "gang": {j["name"]: [j["gang"], j.get("gn")]} if j.get("gang") else {}, "held": None} for j in extra]
        self.marks, self.todo, self.gang_local = marks, todo, gl

    def dep_state(self, queues, now, reqs=()):
        """各任务此刻的状态 {(项目, 任务): pending|done|failed}（self.dep_idx，跑失败的另记 self.dead），依赖的放行判定（dep_block）与推演都按它。依次：
        - pending：还会跑的：本机清单里没标完的、在跑的（list_state），别的主机状态里的 todo（peers；那台 worker 不在、又没 worker stop 的
          DEP_STALE_S 内照样算，续租、重启时不误报找不到）。它盖过以前的结果，只有那台写状态之后才记下的用量记录（已跑完）盖过它；
        - 本机清单里有这一行的按标记：.done 成功、.failed 失败（几条清单说法不一的看用量记录的最后一次，没有记录算失败）；
        - 其余看各主机用量记录里的最后一次（作废、被挤掉重排的不算）。
        哪里都没有的不在表里（dep_block 满 DEP_MISS_S 才报找不到，起算时刻 self.dep_miss 只留这一轮还有人等、仍找不到的）。被等的任务按 项目 + 任务名 认，不看它在哪条清单、哪台主机。
        还会跑的在哪记进 self.dep_where（等待原因写清所在清单暂停、那台 worker 不在），各清单项记进 self.dep_lanes（dep_cycle 找环）。
        本机清单的状态每轮都算（list_state，写进状态给别的主机）；本机与别的主机都没有任务写依赖、也没有成组的任务时其余不算
        （别的主机上写了依赖的行可能被挪到这台，offers 的假设推演按这里的状态判它的依赖；成组的找环也要 dep_lanes，见 gang_block）。"""
        self.list_state(queues, reqs)
        self.peers(now)
        if (not any(ln["deps"] or ln["gang"] for ln in self.todo) and not any(dep_keys(j) for j in self.running.values())
                and not any(ln.get("deps") or ln.get("gang") for _, _, _, ln in self.peer_todo)):
            self.dep_idx, self.dead, self.dep_where, self.dep_lanes, self.dep_miss = {}, set(), {}, [], {}
            return
        last = {}
        for h in self.hist:                       # 按结束时刻排好的：后面的盖前面的
            if not h.get("requeued") and not h.get("void"):
                last[(h.get("project"), h.get("name"))] = ("done" if h.get("rc") == "0" else "failed", h.get("t1") or 0)
        idx = {k: v for k, (v, _) in last.items()}
        idx.update((k, v if v != "?" else idx.get(k, "failed")) for k, v in self.marks.items())
        where, lanes = {}, [(None, 0.0, now, ln) for ln in self.todo] + self.peer_todo
        for host, age, t, ln in lanes:
            for n in ln["jobs"]:
                k = (ln["project"], n)
                if host is not None and last.get(k, ("", 0))[1] > t:    # 那台写状态之后才跑完的：用量记录说了算
                    continue
                if k not in where or age < where[k][1]:                 # 几处都有的取本机的、再取状态最新的
                    where[k] = (host, age, ln.get("held"))
        idx.update((k, "pending") for k in where)
        self.dep_idx, self.dead, self.dep_where, self.dep_lanes = idx, {k for k, v in idx.items() if v == "failed"}, where, lanes
        want = ({(p, x) for ln in self.todo for ds in ln["deps"].values() for p, x in ds} | {k[:2] for r in reqs for k in dep_keys(r)}
                | {k[:2] for r in self.pool_heads for x in [r] + [dict(h, project=r["project"]) for _, h in r.get("win") or ()] for k in dep_keys(x)})
        self.dep_miss = {k: t for k, t in self.dep_miss.items() if k in want and k not in idx}

    def pool_view(self, now):
        """算力池里待放的各清单还要跑的（#25）：{"目录|清单": {head 队首（并行清单取窗口里第一行依赖满足的，#147）, n 还要跑几行, dep 队首的依赖没满足时 [状态, 原因]（dep_block），held 清单暂停,
        elsewhere 交给池之前在别的主机放行、还没收尾的那一格 [主机, 任务]（不算在 head、n 里，#80）}}；
        池里登记着的每条都列（#86）：都跑完了的 head 为 null、n 为 0；这台读不到清单文件的另带 err（一句原因，head 为 null）。
        池目录没有 worker：同一 worker_home 下各台的 worker 都按自己的 dep_state 算一份写进状态的 pool_heads，看板（pool._heads）逐条取最新的那份
        （读得到的优先）：哪一份里都没有的是还没有 worker 读到，不当成跑完了；有行要跑、依赖满足、没暂停、没有 elsewhere 的才放、才为它借。"""
        def head(r):                              # 并行清单（#147）取窗口里第一行依赖满足的；都没满足报第一行的
            blk = None
            for n, h in r.get("win") or [(r["name"], r)]:
                b = self.dep_block(dict(r, name=n, deps=h.get("deps"), deps_any=h.get("deps_any")), now)
                if not b:
                    return n, None
                blk = blk or b
            return r["name"], list(blk)
        out = {}
        for r in self.pool_heads:
            n, dep = head(r) if r["name"] else (None, None)
            out["%s|%s" % (os.path.normpath(r["root"]), r["queue"])] = dict({"head": n, "n": r["n"], "held": r["held"], "elsewhere": r["elsewhere"], "dep": dep},
                                                                            **({"err": r["err"]} if r.get("err") else {}))
        return out

    def dep_block(self, r, now):
        """请求写的依赖（dep_keys）还没都满足：返回 (状态, 原因)，状态 wait（等的还在排、在跑）、failed（after= 等的跑失败了）、
        missing（等的哪里都找不到，持续满 DEP_MISS_S：起算时刻记在 self.dep_miss，时限内算 wait、原因写 MISS_NOTE）、cycle（依赖成环，谁也轮不到：dep_cycle）；
        都满足或没写为 None。挡住期间同被临时指令挡住：不参与排队、不算等待时间、不为它停保活；推演里照样排（排在依赖满足的时刻）。
        wait 以外的在状态里标出（等待项的 dep），看板出告警。"""
        st = [(k, self.dep_idx.get(k[:2])) for k in dep_keys(r)]
        bad = [k for k, v in st if v == "failed" and not k[2]]
        new = [k for k, v in st if v is None and now - self.dep_miss.setdefault(k[:2], now) < _C.DEP_MISS_S]
        miss = [k for k, v in st if v is None and k not in new]
        pend = [k for k, v in st if v == "pending"]
        if bad:
            return "failed", "等的 %s 跑失败了，不会放行：修好重跑（删掉它的 .failed）后接着等；不等了就删掉这一行的 after=" % dep_names(bad, r["project"])
        if miss:
            return "missing", "找不到等的 %s（任务名写错了，或它所在的清单没登记）：改对 after= 后下一轮生效" % dep_names(miss, r["project"])
        ring = pend and self.dep_cycle(r, [k[:2] for k in pend])
        if ring:
            return "cycle", "依赖成环，谁也轮不到：%s。改清单顺序或 after= 后下一轮生效" % ring
        why = []
        if pend:
            e = dep_end([k for k, _ in st], now, self.ends, self.dead)
            note = "；".join(x for x in (self.dep_note(k[:2], r["project"]) for k in pend) if x)
            why.append("等 %s 跑完%s%s" % (dep_names(pend, r["project"]), "（预计 %s）" % _C.now_str(e)[5:16] if e else "", "；" + note if note else ""))
        if new:
            why.append("等 %s（%s）" % (dep_names(new, r["project"]), _C.MISS_NOTE))
        return ("wait", "；".join(why)) if why else None

    def dep_note(self, k, project):
        """还会跑、眼下却不会动的被等任务的说明（等待原因里写）：所在清单暂停、drain，或在 worker 不在的主机上；照常在排的为 None。"""
        host, age, held = self.dep_where.get(k, (None, 0, None))
        why = [{"paused": "所在清单暂停中", "pause": "所在项目暂停中", "drain": "所在主机 drain 中"}.get(held, held)] if held else []
        if host == _C.POOL:
            why.append("在算力池里等着放到某台")
        elif host is not None and age > _C.STATUS_STALE_S:
            why.append("在 %s 上，那台 worker 已 %d 分钟没更新" % (host, age / 60))
        return "%s %s" % (dep_names([k + (False,)], project), "，".join(why)) if why else None

    def dep_next(self):
        """依赖图里一个任务 (项目, 任务) 在等谁：返回 nxt(k) → [(等的任务, "等" 写的依赖里还会跑的 | "排" 同一条清单里排在它前面的（并行清单不算：
        前面哪一行跑完都腾出一格，不是等某一行，#147） | "组" 与它成组的)]。
        清单项取 dep_lanes（本机的与别的主机 todo 里的）。dep_cycle（成环）与 dep_reaches（offers 插行防成环）共用。"""
        pos, grp = {}, {}
        for i, (_, _, _, ln) in enumerate(self.dep_lanes):
            for j, n in enumerate(ln["jobs"]):
                pos.setdefault((ln["project"], n), []).append((i, j))
            for n, (g, _) in (ln.get("gang") or {}).items():
                grp.setdefault((ln["project"], g), set()).add((ln["project"], n))
        mate = {k: ks for ks in grp.values() for k in ks}

        def nxt(k):                               # k 在等的：写的依赖里还会跑的、同一条清单里排在它前面的、与它成组的
            for i, j in pos.get(k, []):
                ln = self.dep_lanes[i][3]
                yield from (((p, x), "等") for p, x in (ln.get("deps") or {}).get(k[1], []) if (p, x) in self.dep_where)
                if (ln.get("par") or 1) <= 1:     # 并行清单（#147）：窗口里的几行不互相排队，前面的哪一行跑完都腾出一格，不算排在谁后面
                    yield from (((ln["project"], x), "排") for x in ln["jobs"][:j])
            yield from ((x, "组") for x in sorted(mate.get(k, ())) if x != k)
        return nxt

    def dep_waiters(self, sim=False):
        """谁写了 after= / afterany= 等着谁：{(项目, 任务): {等它的 (项目, 任务)}}，取本机与别的主机还会跑的（todo、peer_todo：peer_st 的，
        另有 worker 不在、已归还的与池里待放的：接收方空着时跳过说不出何时放行的清单，那边的行可能正等着这里的候选，#62）。
        有人等着的行不挪、不分到新车道：挪走后等它的何时开始会变（offers 的 cant_move、fit --list 共用，#144）。
        sim（offers 用）：本机清单里等同一个项目的任务的行不算（暂停的、算力池放的清单除外）：try_move 把它们连同挪来的行一起重推，
        它们何时跑完进 own，看板按整批比（#167：本机的汇总行 after= 等别处的训练行时，那几行照样能挪过来）。"""
        out = {}
        for ln in self.todo + [x[3] for x in self.peer_todo]:
            here = sim and any(ln is x for x in self.todo) and ln.get("held") not in ("paused", "pause") and not ln.get("pool")
            for n, ds in (ln.get("deps") or {}).items():
                for k in ds:
                    if not (here and k[0] == ln.get("project")):
                        out.setdefault(tuple(k[:2]), set()).add((ln.get("project"), n))
        return out

    def dep_reaches(self, starts, goal):
        """从 starts 里的任务沿“等”“排”“组”走（dep_next），能不能走到 goal 里的任务：offers 把挪来的行插在 goal 那几行前面之前问它，
        走得到就是挪来的行（间接）在等它们，插在前面会成环（#43，Codex 审 #63）。"""
        nxt, todo = self.dep_next(), list(starts)
        seen = set(todo)
        while todo:
            k = todo.pop()
            if k in goal:
                return True
            for x, _ in nxt(k):
                if x not in seen:
                    seen.add(x)
                    todo.append(x)
        return False

    def dep_cycle(self, r, pend, mates=()):
        """依赖成环（清单顺序也算依赖：串行清单一行要等它前面的都跑完；并行清单窗口里的行不算，#147）：从 r 等的还会跑的任务 pend（与 r 成组的 mates）出发，沿“等谁”（写的依赖）、
        “排在谁后面”与“和谁成组”（gang=：要一起开跑，等于等同伴能开跑）找回 r 自己，返回最短的一圈的文字（“B 等 A，A 排在 B 后面”），没有为 None。
        只由“成组”连起来的一圈不算（几个成员本来就互相等着一起开跑）：圈里至少要有一条“等”或“排”（等谁跑完）。
        清单项取 dep_lanes（本机的与别的主机 todo 里的；在跑的排最前、不等谁）。dep_block 与 gang_block 共用。"""
        me = (r["project"], r["name"])
        name = lambda k: k[1] if k[0] == r["project"] else "%s/%s" % k
        if me in pend:
            return "%s 等它自己" % name(me)
        nxt = self.dep_next()
        # 广度优先，状态是 (任务, 走过的路上有没有“等”“排”)：找到的一圈最短
        prev, todo, end = {}, [], None
        for k, how in [(k, "等") for k in pend] + [(k, "组") for k in mates]:
            s = (k, how != "组")
            if s not in prev:
                prev[s] = ((me, False), how)
                todo.append(s)
        while todo and not end:
            s = todo.pop(0)
            for x, how in nxt(s[0]):
                hard = s[1] or how != "组"
                if x == me:
                    if hard:
                        end = (s, how)
                        break
                    continue
                if (x, hard) not in prev:
                    prev[(x, hard)] = (s, how)
                    todo.append((x, hard))
        if not end:
            return None
        steps, s = [(end[0][0], end[1], me)], end[0]
        while s != (me, False):
            p, how = prev[s]
            steps.append((p[0], how, s[0]))
            s = p
        say = {"等": "%s 等 %s", "排": "%s 排在 %s 后面", "组": "%s 与 %s 成组（要一起开跑）"}
        return "，".join(say[how] % (name(a), name(b)) for a, how, b in reversed(steps))

    # ---- 成组（清单行尾 gang=组名:人数，#180）：几个成员都留好资源再一起开跑，一个失败或停下其余的跟着停 ----
    # 各台只经共享盘交换：成员按各自状态里的 todo 认（list_state），此刻怎样看各自的 gang.json（gang_publish 写）。
    # 一轮里：gang_sync（认组、读同伴、gang_watch 停该停的）→ prep 里 gang_block（不能排队的原因）→ admit 里 place 留资源 → gang_commit 一起放行 → 推演 → gang_publish。
    def gang_sync(self, now):
        """每轮在 dep_state 之后、prep 之前：本机有成员的组里有谁、各在哪台、此刻怎样，建 self.gctx {(项目, 组名): {mem {(项目, 任务): {host（本机为 None）,
        age 那台状态已多久没更新, n 写的人数, when 预计何时能一起开跑, now 此刻就能, rec 那台 gang.json 里它的记录}}, at 别的主机上的成员里最晚的 when
        （有一个说不出为 None）, now 别的主机上的都此刻就能, why 说不出的原因}} 与给 place、推演的 self.gsim {(项目, 组名): {at, local {(请求 key, 任务): 预计}}}
        （本机成员的预计先取上次推演的 gang_eta，prep 把这一轮在队首、没被挡住的改成现在）。成员按 todo 认（本机与别的主机的，同 dep_state 的 peer_todo），
        一处同时有几份的取本机的、再取状态最新的。同组别的主机的 gang.json 每轮直接读（固定路径、不到 1 KB；读不到、写到一半的沿用上次的）。
        最后看本机在跑的成员要不要跟着同伴停下（gang_watch）。"""
        mem = {}
        for host, age, _, ln in [(None, 0.0, now, ln) for ln in self.todo] + self.peer_todo:
            for n, (g, gn) in (ln.get("gang") or {}).items():
                ms = mem.setdefault((ln.get("project"), g), {})
                old = ms.get((ln.get("project"), n))
                if old is None or (old["host"] is not None and (host is None or age < old["age"])):
                    ms[(ln.get("project"), n)] = {"host": host, "age": age, "n": gn}
        mine = [gk for gk, ms in sorted(mem.items()) if any(v["host"] is None for v in ms.values())]
        # 近 GANG_LATE_S 内见过的同组别的主机上的成员在哪台（它跑完、失败后就不在 todo 里了，gang_doomed 还要读它的结果）
        self.gang_known.update({(gk, k): (v["host"], now) for gk in mine for k, v in mem[gk].items() if v["host"]})
        self.gang_known = {x: hv for x, hv in self.gang_known.items() if now - hv[1] <= _C.GANG_LATE_S}
        want = {v["host"] for gk in mine for v in mem[gk].values() if v["host"]} \
            | {w[0] for j in self.running.values() for w in j.get("gang_with") or [] if w[0] != self.host} \
            | {h for (gk, _), (h, _) in self.gang_known.items() if gk in mine}
        self.gpeer = {h: gang_file(h) or self.gpeer.get(h) for h in want}
        self.gctx, self.gsim = {}, {}
        for gk in mine:
            ctx = self.gctx[gk] = {"mem": mem[gk], "at": now, "now": True, "why": [], "bad": gang_bad(mem[gk])}
            for k, v in sorted(mem[gk].items()):
                if v["host"] is None:
                    continue
                v["when"], v["now"], why = self.gang_when(k, v, now)
                ctx["now"] = ctx["now"] and v["now"]
                if v["when"] is None:
                    ctx["at"] = None
                    ctx["why"].append(why)
                elif ctx["at"] is not None:
                    ctx["at"] = max(ctx["at"], v["when"])
            if ctx["bad"]:                        # 人数对不上：谁也不留、推演里同组的都不排
                ctx["at"], ctx["now"] = None, False
            loc = {(self.gang_local[k]["key"], k[1]) for k, v in mem[gk].items() if v["host"] is None and k in self.gang_local}
            self.gsim[gk] = {"at": ctx["at"], "local": {x: self.gang_eta.get(x) for x in loc}}
        self.gang_miss = {gk: t for gk, t in self.gang_miss.items() if gk in self.gctx}
        self.gang_watch(now)
        self.gang_doom = self.gang_lost(mine, now)

    def gang_lost(self, mine, now):
        """本机还没开跑的成员里，同一次开跑的同伴已经失败了的（先开跑的那个在这边跟上之前就失败收尾）：{(项目, 任务): 原因}，gang_doomed 记失败。
        认法同 gang_peer：同伴近 GANG_LATE_S 内的 fail 记录里，它开跑时记下的同伴（with）有这一员、记下的“最近一次结束”就是这一员此刻的（之后没跑过）。
        同伴失败后就不在 todo 里了，所以按近来见过的（gang_known）找它在哪台；本机的同伴看 gang_ends。"""
        run = {(j.get("project"), j["name"]) for j in self.running.values()}
        out = {}
        for gk in mine:
            recs = [(e["name"], e) for e in self.gang_ends if (e["project"], e["gang"]) == gk] + [
                ("%s（%s）" % (m[1], h), e) for (g, m), (h, _) in sorted(self.gang_known.items()) if g == gk
                for e in (self.gpeer.get(h) or {}).get("members") or [] if (e.get("project"), e.get("name")) == m]
            for k, m in sorted(self.gang_local.items()):
                if (k[0], m["gang"]) != gk or k in run:
                    continue
                last = gang_last(self.gang_ends, *k)
                for who, e in recs:
                    seen = {(w[0], w[1]): w[2] for w in e.get("with") or [] if len(w) > 2}
                    if e.get("state") == "fail" and now - (e.get("t1") or 0) <= _C.GANG_LATE_S and seen.get((self.host, k[1])) == last:
                        out[k] = "同组的 %s 开跑后失败了，这边还没跟上：同一次开跑的整组记失败，没开跑" % who
                        break
        return out

    def gang_doomed(self, reqs, now):
        """gang_lost 找出的成员（还在队首等着）记失败：.failed、failed 事件、gang.json 的 fail、一行 0 分钟的用量行（rc 为 null：别的主机的依赖按它认失败），
        从这一轮的请求里去掉（留着的资源随之放掉），清单接着往下走。返回剩下的请求。"""
        out = []
        for r in reqs:
            why = r.get("gang") and r["kind"] == "queue" and self.gang_doom.get((r["project"], r["name"]))
            if not why:
                out.append(r)
                continue
            q = r["q"]
            open(os.path.join(q["state"], r["name"] + ".failed"), "w").close()
            self.qlog(q, "失败 %s（%s）" % (r["name"], why))
            self.event("failed", "失败 %s（%s）" % (r["name"], why), name=r["name"], project=r["project"])
            j = dict(r, t0=now, gpus=[], use={}, est=None, excl=None, wait=None, void=None)
            self.gang_end(j, "fail")
            with _C.flock(self.p.usage):
                _C.append_jsonl(self.p.usage, self.usage_rec(j, None, None, False))
        return out

    def gang_when(self, k, v, now):
        """别的主机上的成员 k（gang_sync 的 mem 项 v）此刻怎样：(预计何时能一起开跑：已留好资源的、刚开跑的为现在；说不出为 None,
        是不是此刻就能一起开跑（留好资源、那台 GANG_FRESH_S 内写的，或开跑不到 GANG_JOIN_S）, 说不出时的原因)。按那台 gang.json 里它的记录认，记进 v["rec"]。"""
        who = "%s（%s）" % (k[1], v["host"])
        if v["age"] > _C.STATUS_STALE_S:
            return None, False, "%s 那台 worker 已 %d 分钟没更新" % (who, v["age"] / 60)
        g = self.gpeer.get(v["host"]) or {}
        rec = v["rec"] = gang_cur(g.get("members"), k)
        age = now - (g.get("t") or 0)
        if rec is None or age > _C.STATUS_STALE_S:
            return None, False, "%s 还没给出就绪状态（那台 worker 若是旧版就不认 gang=，会单独放行它）" % who
        if rec["state"] == "ready":
            return now, age <= _C.GANG_FRESH_S, None
        if rec["state"] == "run":
            t0 = rec.get("t0") or 0
            # 本机同组的成员在这一次开跑里已经结束（停下重排、失败）：不再跟上这一次，等它也停下后一起重来。
            # 这一次的结束 = 开跑时刻相差 GANG_JOIN_S 以内、又晚于同伴开跑时见过的那次（它 run 记录的 with：[主机, 任务, gang_last]）
            seen = {(w[0], w[1]): w[2] for w in rec.get("with") or [] if len(w) > 2}
            if any(e["project"] == k[0] and e["gang"] == rec.get("gang") and abs(e["t0"] - t0) <= _C.GANG_JOIN_S
                   and e["t1"] > seen.get((self.host, e["name"]), 0) for e in self.gang_ends):
                return None, False, "%s 这一次开跑里同组这边的已结束，等它也停下后一起重来" % who
            if now - t0 <= _C.GANG_JOIN_S:
                return now, True, None
            return None, False, "%s 已开跑 %d 分钟（没等到这边），会停下重排" % (who, (now - t0) / 60)
        if rec.get("eta") is None:
            return None, False, "%s %s" % (who, rec.get("why") or "推演里排不上，不知道何时能放行")
        return max(now, rec["eta"]), False, None

    def gang_block(self, r, now):
        """成组的成员此刻还不能参加排队的原因（prep 在依赖之后看，记进 r["dep"]，同依赖没满足：不排位、不算等待、不为它停保活）：(状态, 原因)，状态同 dep_block：
        missing 没写人数、凑不齐、人数写得不一样（后两种持续满 DEP_MISS_S，起算时刻 self.gang_miss：两边清单同时加行时那台还没认出）、cycle 一起开跑成环（dep_cycle）、
        wait 组里别的主机上有成员说不出何时能放行（不留资源）。能排队为 None。组里本机别的成员不在这里挡：它们这一轮在不在队首、放不放得下由 place 与 gang_commit 看。"""
        g = r.get("gang")
        if not g or r["kind"] != "queue":
            return None
        gk = (r["project"], g)
        if not r.get("gn"):
            return "missing", "成组 %s 没写人数：写成 gang=%s:人数（几个成员一起开跑）后下一轮生效" % (g, g)
        ctx = self.gctx.get(gk) or {"mem": {}, "why": [], "bad": "凑不齐：还没认出同组的成员"}
        ms, bad = ctx["mem"], ctx["bad"]
        if bad:
            t0 = self.gang_miss.setdefault(gk, now)
            if now - t0 < _C.DEP_MISS_S:
                return "wait", "成组 %s %s；刚加的行要等那台 worker 认出，%d 分钟后仍这样就报找不到" % (g, bad, _C.DEP_MISS_S // 60)
            return "missing", "成组 %s %s：改清单后下一轮生效" % (g, bad)
        self.gang_miss.pop(gk, None)
        ring = self.dep_cycle(r, [], [k for k in ms if k != (r["project"], r["name"])])
        if ring:
            return "cycle", "一起开跑成环，谁也轮不到：%s。同组的成员放在不同的清单里、互不 after=，改后下一轮生效" % ring
        if ctx["why"]:
            return "wait", "成组 %s：%s；先不留资源" % (g, "；".join(ctx["why"]))
        return None

    def gang_dur(self, r, f="min"):
        """成组成员 r 截止前的准入与回填用的时长（分钟）：组里各成员预计时长里最长的（本机的取 queue_durs 算好的，别的主机的取它 gang.json 里的 dur）；
        有一个不知道为 None（约定归还照放、续租截止不放，同 override_hit）。req_dur 不给 k 时经它；运行中任务的预计结束按自己的（run_end 传 k）。
        f="cut"：截止前的准入用的（duration 的 cut，#139），别的主机的取 gang.json 的 cut，没有的（旧 worker 写的）按 dur × DUR_MARGIN。"""
        own = (self.durs.get(_C.lane_key(r), {}).get(r["name"]) or {}).get(f)
        ctx = self.gctx.get((r["project"], r["gang"]))
        ds = [own]
        for k, v in sorted((ctx or {}).get("mem", {}).items()):
            if k == (r["project"], r["name"]):
                continue
            m = self.gang_local.get(k) if v["host"] is None else None
            rec = v.get("rec") or {}
            ds.append((self.durs.get(m["lane"], {}).get(k[1]) or {}).get(f) if m else rec.get("cut", rec.get("dur") and rec["dur"] * _C.DUR_MARGIN) if f == "cut" else rec.get("dur"))
        return None if None in ds else max(ds)

    def gang_soon(self, r, now):
        """成组成员这一轮算不算要卡（gpu_state 停不停保活）：组里预计 GANG_SOON_S 内就能一起开跑（gang_until）。不成组的都算。"""
        if not r.get("gang"):
            return True
        u = gang_until({"gholds": self.gang_hold, "gangs": self.gsim}, r, now)
        return u is not None and u <= now + _C.GANG_SOON_S

    def gang_commit(self, reqs, waiting, now):
        """admit 之后：本机成员都是这一轮的队首、都在这一轮留好了资源（place 记的 gang_hold），别的主机上的成员都此刻就能（gang_when 的 now），
        就按留着的卡一起放行（launch；同伴记进 gang_with，接管时找回），从等待表里去掉。这一轮没重新留的（让位给屏障、不再是队首）已由 gang_recheck 放掉。
        推演里同一条规则在 simulate。先看到全员就绪的那台先跑，另一台看到它开跑（GANG_JOIN_S 内）就跟上；跟不上的由 gang_watch 停下重排。"""
        done = set()
        for gk, ctx in self.gctx.items():
            loc = sorted((r for r in reqs if r.get("gang") and (r["project"], r["gang"]) == gk), key=lambda r: r["key"])
            hs = [self.gang_hold.get(r["key"]) for r in loc]
            if (not loc or not ctx["now"] or len(loc) != sum(v["host"] is None for v in ctx["mem"].values())
                    or any(h is None or h["name"] != r["name"] for h, r in zip(hs, loc))):
                continue
            for r, h in zip(loc, hs):
                del self.gang_hold[r["key"]]
                # 同伴 [主机, 任务, 它最近一次结束的时刻（gang_last：那之后的结束才算这一次的，gang_peer、gang_when 认同一次用）]
                r["gang_with"] = [[v["host"] or self.host, k[1], gang_last(self.gang_ends if v["host"] is None else (self.gpeer.get(v["host"]) or {}).get("members"), *k)]
                                  for k, v in sorted(ctx["mem"].items()) if k != (r["project"], r["name"])]
                got = self.launch(r, h["cards"])
                if got is None:
                    done.add(r["key"])
                else:
                    self.wlog("成组 %s：%s 没放成（%s）" % (gk[1], r["name"], got[0]))
        waiting[:] = [w for w in waiting if w["key"] not in done]

    def gang_watch(self, now):
        """本机在跑的成员按同伴在这一次开跑里怎样（gang_peer：开跑时记下的 gang_with）决定要不要停下（gang_sync 末尾调）：同伴失败（含被作废）→ 按停法停下、记失败；
        同伴停下重排（暂停、被挤掉、容器重启中断、没跟上）或所在主机已归还 → 按停法停下、重新排队；开跑满 GANG_LATE_S 同伴还没跟着开跑 → 同样停下重排。
        同伴跑成功了的不管。已在停的不再看。停法见 gang_stop。"""
        recs = self.gang_recs(now)
        for j in list(self.running.values()):
            if not j.get("gang") or j.get("gang_stop"):
                continue
            for h, n, seen in j.get("gang_with") or []:
                who = n if h == self.host else "%s（%s）" % (n, h)
                st = gang_peer(recs if h == self.host else (self.gpeer.get(h) or {}).get("members"), j.get("project"), n, j["t0"], seen)
                if st == "fail":
                    self.gang_stop(j, "fail", "同组的 %s 失败了" % who)
                elif st == "stop" or (st == "run" and h in self.gone):
                    self.gang_stop(j, "stop", "同组的 %s %s" % (who, "停下重排了" if st == "stop" else "所在的主机已归还"))
                elif st is None and now - j["t0"] > _C.GANG_LATE_S:
                    self.gang_stop(j, "stop", "开跑 %d 分钟了同组的 %s 还没跟着开跑" % ((now - j["t0"]) / 60, who))
                else:
                    continue
                break

    def gang_stop(self, j, kind, why):
        """跟着同伴停下一个成员：kind 是 fail（结束后记失败）或 stop（重新排队）。停法照这条清单登记的：term 发 SIGTERM、file 写停止请求文件；
        boundary 停不下，只记一条，等它自己跑完（跑成功记完成，否则照 kind）。结果由 finish 按 j["gang_stop"] 定（接管时找回）。"""
        mode = j.get("stop") or "boundary"
        j["gang_stop"] = {"kind": kind, "why": why, "sent": mode != "boundary"}
        if mode == "boundary":
            self.event("gang", "成组：%s；%s 登记的停法是 boundary，只能等它自己跑完（成组的清单请登记 --stop term）" % (why, j["name"]),
                       name=j["name"], project=j.get("project"), why=why)
            return
        if not self.send_stop(j, mode):
            j["gang_stop"]["sent"] = False
            self.event("gang", "成组：%s；未能向 %s 发停止请求" % (why, j["name"]),
                       name=j["name"], project=j.get("project"), why=why)
            return
        j["stopping"] = True
        self.event("gang", "成组：%s，按 %s 停下 %s，%s" % (why, mode, j["name"], "记失败" if kind == "fail" else "重新排队"),
                   name=j["name"], project=j.get("project"), why=why)

    def gang_end(self, j, state):
        """记下本机一个成员这一次开跑的结果（done、fail、stop）：写进 gang.json 留 GANG_KEEP_S，同伴按开跑时刻认同一次（gang_peer）。"""
        now = _C.time.time()
        self.gang_ends = [e for e in self.gang_ends if now - e["t1"] < _C.GANG_KEEP_S] + [
            {"project": j.get("project"), "gang": j["gang"], "n": j.get("gn"), "name": j["name"], "state": state, "t0": round(j["t0"]), "t1": round(now),
             "with": j.get("gang_with")}]

    def gang_durs(self, key, name):
        """gang.json 里一个成员的预计时长 dur 与截止用的 cut（分钟，queue_durs 算好的；同伴的 gang_dur 读，#139）。"""
        d = self.durs.get(key, {}).get(name) or {}
        return {"dur": d.get("min"), "cut": d.get("cut")}

    def gang_recs(self, now, waiting=()):
        """本机成员此刻怎样（gang.json 的 members；gang_watch 看本机的同伴也用它）：在跑的 run（开跑时刻 t0，一起开跑的同伴 with 即 gang_with）、留好资源的 ready（从何时 t）、
        其余 wait（推演里最早何时放得下 eta，说不出为 None；它在队首被挡着的原因 why），各带 n、自己的预计时长 dur（分钟），再接上结束的（gang_end，留 GANG_KEEP_S）。"""
        ws, out, seen = {w["key"]: w for w in waiting}, [], set()
        for j in self.running.values():
            if j.get("gang"):
                seen.add((j.get("project"), j["name"]))
                out.append({"project": j.get("project"), "gang": j["gang"], "n": j.get("gn"), "name": j["name"], "state": "run", "t0": round(j["t0"]),
                            "with": j.get("gang_with"), **self.gang_durs(_C.lane_key(j), j["name"])})
        for k, m in sorted(self.gang_local.items()):
            if k in seen:
                continue
            h, w = self.gang_hold.get(m["key"]), ws.get(m["key"])
            e = {"project": k[0], "gang": m["gang"], "n": m["n"], "name": k[1], **self.gang_durs(m["lane"], k[1])}
            if h and h["name"] == k[1]:
                e.update(state="ready", t=round(h["t"]))
            else:
                eta = self.gang_eta.get((m["key"], k[1]))
                e.update(state="wait", eta=eta and round(eta), why=w["why"] if w and w.get("name") in (k[1], None) else None)
            out.append(e)
        return out + [e for e in self.gang_ends if now - e["t1"] < _C.GANG_KEEP_S]

    def gang_publish(self, waiting, now):
        """写 gang.json（推演之后，每轮）：{t 写的时刻, host, members（gang_recs）, known 近来见过的同组别的主机上的成员 [[项目, 组名, 任务, 主机, 何时见到]]
        （gang_known；重启后 load_prev 读回：同伴失败后不在 todo 里了，不记着它在哪台就读不到它的结果，gang_lost 认不出）}。本机没有成员也没有近一天结束的时不写，之前写过的再写一次空的（文件建了不删：
        共享盘上新建的文件别的主机可能要等目录缓存过期才看得到，同伴只在 todo 里见到成员之后才读它）。"""
        recs = self.gang_recs(now, waiting)
        if recs or self.gang_pub:
            known = [[gk[0], gk[1], k[1], h, round(t)] for (gk, k), (h, t) in sorted(self.gang_known.items())]
            _C.write(self.p.gang, json.dumps({"t": round(now, 1), "host": self.host, "members": recs, "known": known}, ensure_ascii=False))
        self.gang_pub = recs

    def gang_poll(self):
        """主循环拍与拍之间（约 2 秒一次）：本机有留好资源的成员时重读同伴的 gang.json，状态变了（就绪、开跑、停下）返回 True，提前进下一轮，
        几台一起开跑相差几秒。没有留着的不读。"""
        if not self.gang_hold:
            return False
        sig = []
        for gk in sorted({h["gang"] for h in self.gang_hold.values()}):
            for k, v in sorted(((self.gctx.get(gk) or {}).get("mem") or {}).items()):
                if v["host"]:
                    rec = gang_cur((gang_file(v["host"]) or {}).get("members"), k)
                    sig.append((v["host"], k, rec and rec["state"]))
        changed = self.gang_sig is not None and sig != self.gang_sig
        self.gang_sig = sig
        return changed

    def gang_moved(self):
        """gang_poll 出错不拖垮主循环：记一次 worker.log，当没变。"""
        try:
            return self.gang_poll()
        except Exception as e:  # noqa: BLE001
            if self.noted.get("_gang") != str(e):
                self.wlog("读成组同伴的状态出错：%r" % e)
                self.noted["_gang"] = str(e)
            return False
