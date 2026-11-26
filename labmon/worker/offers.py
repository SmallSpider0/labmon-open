"""worker 多主机与同机负载均衡挪法建议（offers / local_offers / lane_room）。"""
import math
import os
import re

from . import common as _C
from . import queue as _Q
from . import measure as _M
from . import deps as _DP
from . import sched as _S
from . import forecast as _FC

class OffersMixin:

    # ---- 能接别处的任务（负载均衡的提示，Issue #20 第一档：只提示，不改清单、不改放行）----
    def list_of(self, path):
        """一条清单的 [(任务, 资源提示)]，按修改时间缓存（offers 取各行的资源提示；还会跑哪几行看 todo）；读不到为 None。"""
        try:
            m = _C.fsig(path)
        except OSError:
            return None
        c = self.olists.get(path)
        if not c or c[0] != m:
            c = self.olists[path] = (m, [(n, _Q.hints(cmd)) for n, cmd in _Q.list_jobs({"jobs_path": path})])
        return c[1]

    def list_env(self, root, queue):
        """一条清单的环境：(目录, .env 去掉 QUEUE_STATE、QUEUE_LOGS 两行与空行、各行去掉首尾空白后的内容；没有 .env 或什么都不剩为 None)，.env 按修改时间缓存。任务在清单的目录下跑、
        读这条清单的 .env，offers 只把一批挪来的行取自同一种环境、只接到同一种环境的清单后面（#25 D）。QUEUE_STATE、QUEUE_LOGS 是 labmon
        给这条队列记账的位置，同目录的几条清单按规矩各写各的（两条队列不许共用状态目录），不算任务的环境。"""
        path = _Q.env_path(root, queue)
        try:
            m = _C.fsig(path)
        except OSError:
            return os.path.normpath(root), None
        c = self.olists.get(path)
        if not c or c[0] != m:
            c = self.olists[path] = (m, "\n".join(x.strip() for x in (_C.read(path, "") or "").splitlines()
                                                   if x.strip() and not re.match(r"\s*(export\s+)?QUEUE_(STATE|LOGS)=", x)) or None)
        return os.path.normpath(root), c[1]

    def proj_lanes(self, host, todo, plan, running, held=()):
        """一台主机上各清单还没跑完的部分与预计何时跑完（offers 用）：还会跑的行取那台状态里的 todo（list_state；本机取 self.todo），
        预计时刻取那台的推演（本机取这一次或假设的推演）。[{host, root, queue, project, pend [(任务, 资源提示, 推演的开始, 结束)], run（在跑那个的
        预计结束；没有在跑的为 0）, end, state, dep（unknown 是因为推演停在依赖上）, why, unread}]。
        - end：这条清单预计何时跑完（在跑的与没跑完的预计结束里最晚的；有一个不知道、或 state 是 unknown 就是 None）；
        - state：ok 照常在排；trapped 有没跑完的在推演里排不上（plan 写了 stuck：放不下、时长不明、截止前跑不完…），或被持久的临时指令挡住
          （约定归还、续租截止、drain、只给别的项目用：held 里的，或 todo 的 held 是 drain），在原处跑不了，可以挪（维护者 2026-11-08 定：续租截止
          挡住的也算）；unknown 推演里没有、又说不出为什么（读不了状态目录、队列没出请求），或推演停在依赖上（plan 的 stuck_dep：等的任务跑不成或
          那台说不出何时跑完、前面的占位任务不知道何时放行；挪走的行带着同样的 after=，不是原处跑不了，#183）：不知道原处何时放行，不挪（why 写原因），
          整批推不出（#25）。对方此刻超出预留的清单照常推演（forecast），不在这两种里；
        - 资源提示为 None：清单文件里没有这一行（刚剪过）或读不到清单（unread），不挪。同名的行取第一条（first_rows）。
        清单暂停（paused）、项目在那台暂停（pause）的不列：用户有意停的，不当成要均衡的活，也不算进整批何时跑完（同项目看板的全部完成）。
        running：(目录, 清单) → 在跑的任务名的集合（并行清单有几个，#147）；held：等待项被临时指令挡住的 (目录, 清单)（那台状态的 waiting[].held；本机取 forecast 的等待请求）。"""
        pl = {(ln.get("root"), ln.get("queue")): ln for ln in plan.get("lanes") or [] if ln.get("kind") == "queue"}
        out = []
        for ln in todo:
            if not ln.get("queue") or ln.get("held") in ("paused", "pause") or ln.get("pool"):   # 算力池放的（#79）：落点由看板定，不给挪法建议
                continue
            key = (ln["root"], ln["queue"])
            cur = running.get(key) or set()
            rows = self.list_of(os.path.join(ln["root"], ln["queue"]))
            hs = _Q.first_rows(rows or [])
            times = {j["name"]: j for j in (pl.get(key) or {}).get("jobs") or []}
            pend = [(n, hs.get(n), (times.get(n) or {}).get("t0"), (times.get(n) or {}).get("t1")) for n in ln["jobs"] if n not in cur]
            if not pend and not cur:
                continue
            ce = [(times.get(n) or {}).get("t1") for n in cur]   # 在跑的（并行清单有几行，#147）预计结束里最晚的；有一个不知道为 None
            run = (None if None in ce else max(ce)) if cur else 0
            ts, lp, ok = [run] + [x[3] for x in pend], pl.get(key) or {}, all(x[0] in times for x in pend)
            dep = not ok and lp.get("stuck_dep")
            state = ("ok" if ok else "unknown" if dep else
                     "trapped" if key in held or ln.get("held") == "drain" or lp.get("stuck") else "unknown")
            why = ("%s 上 %s：%s，说不出原处何时放行" % (host, ln["queue"], lp["stuck"]) if dep else
                   "%s 上 %s 推演里没有（读不了状态目录、队列没出请求之类），不知道原处何时放行" % (host, ln["queue"]) if state == "unknown" else None)
            out.append({"host": host, "root": ln["root"], "queue": ln["queue"], "project": ln["project"], "pend": pend, "run": run,
                        "end": None if None in ts or state == "unknown" else max(ts), "state": state, "dep": bool(dep), "unread": rows is None, "why": why})
        return out

    def offers(self, qs, base, now, pol, focus, use, info, held=()):
        """这台能接别的主机上哪些任务、接来后几点跑完（写进状态的 offers，看板汇总成挪法建议；labmon 不改清单、不改放行）。
        每个项目（别的在用主机上有没跑完的清单的）一项。挪哪些：从别的主机上这个项目排得最晚的清单（跑不了的算最晚）取队尾还没开跑的一行，
        那条清单的完工随之提前到它前一行的预计结束，再取此刻最晚的，至多 MOVE_MAX 行，挪来的已比原处剩下的晚跑完就不再往下试（moved_past）；队尾是占位任务、
        原处 MOVE_LEAD_S 内就开跑、needs= 缺了又补不了、有别处的任务 after= 等着它（本机这个项目推演里跟着重推的不算，dep_waiters 的 sim）、清单文件里找不到、
        与前面挑的不是同一种环境（目录与 .env）的就到此为止（只从尾巴取：清单里没写 after= 的先后 labmon 看不出来）；这个项目有清单原处何时放行说不出
        （proj_lanes 的 unknown，那台 worker 不在：状态过了 STATUS_STALE_S、没 worker stop、DEP_STALE_S 内）的一行也不挑，整批推不出（#25）。
        本机这个项目的清单何时跑完说不出时只写原因，只停在依赖上的除外（等的常是别处的这些候选，#167）。
        几点跑完：把前 k 行（按原清单的先后）加进这台的推演，走同一份 simulate（本机别的项目、公平份额、约定截止、依赖都算在里面；
        依赖的状态取 dep_state，别的主机上写了依赖的行也算）：这台已有这个项目、与挪来的行同一种环境（list_env）的清单就接在预计最早跑完的那条后面
        （不建议同机多开车道），没有就当新的一条。这个项目在这台不能用（drain、只给别的项目用、暂停；对方此刻超出预留不算，几小时后的事推演不模拟它）
        或这台在预备续租的只写原因。别的主机的状态过期、worker stop 过（含已归还）的不读（peers）。held：本机等待请求被临时指令挡住的 (目录, 清单)。
        算力池借来的主机（guest 指令的 keep.pool，#25）不给：池里的清单放到哪台由看板定。
        每项 {project, lane 接在本机哪条清单后面（[目录, 清单]，None 要新建）, free 本机这个项目原推演里何时跑完（没有为现在；本机清单停在依赖上、说不出时为 None）,
        ends {"主机|目录|清单": 别的主机上这个项目各清单预计何时跑完（不知道为 None）}, jobs [{host, root, queue, name, there 原处预计结束,
        rest 挪走它之后原处那条清单何时跑完, end 挪来前几行时它们在这台何时都跑完, own 这时本机这个项目自己的清单何时跑完}], why 再往下挪不了的原因
        （只放原因本身）, stop 挪到这台排不上的那一行（why 是推演的原因；与具体某行无关的为 None）, idle 这台此刻空着（Worker.idle：挑行时跳过
        挪不动、说不出何时放行的清单，看板没有整批的挪法时按行比，#62）, room 这台卡有真余量时此刻还能给这个项目再开几条车道、能放的卡吃得多满
        （lane_room；不出为 None，看板据此判吃不满，#145）}：原因句由看板拼（rebalance.suggest，#25 B）。"""
        if (self.keep() or {}).get("pool"):
            return []
        mine = {}
        for j in self.running.values():
            if j.get("q"):
                mine.setdefault((j["q"]["root"], j["q"]["jobs"]), set()).add(j["name"])
        own = self.proj_lanes(self.host, self.todo, self.plan, mine, held)
        others, waiters = [], self.dep_waiters(sim=True)
        for h, st in sorted(self.peer_st.items()):
            run = {}
            for j in st.get("running") or []:
                run.setdefault((j.get("root"), j.get("queue")), set()).add(j["name"])
            hd = {(w.get("root"), w.get("queue")) for w in st.get("waiting") or [] if w.get("held")}
            others += self.proj_lanes(h, st.get("todo") or [], st.get("plan") or {}, run, hd)
        for h, age, _, ln in self.peer_todo:      # 那台 worker 不在（续租、重启）：它的清单还会跑、看不到何时跑完，整批推不出（#25 第 8 条）
            # 算力池里待放的（#24）不算 worker 不在：放到哪台由看板定，不挡挪法建议
            if h not in self.peer_st and h not in self.gone and h != _C.POOL and ln.get("queue") and ln.get("jobs") and ln.get("held") not in ("paused", "pause"):
                others.append({"host": h, "root": ln["root"], "queue": ln["queue"], "project": ln["project"], "pend": [], "run": 0, "end": None,
                               "state": "unknown", "dep": False, "unread": False,
                               "why": "%s 的 worker 不在（状态 %d 分钟没更新），那里的 %s 不知道何时跑完" % (h, age / 60, ln["queue"])})
        renew = [o for o in self.ovr if o["kind"] == "deadline" and o.get("target") != "borrow"]
        cx = {"qs": qs, "base": base, "now": now, "pol": pol, "focus": focus, "use": use, "info": info, "mine": mine, "held": held}
        idle = self.idle(now)
        out = []
        for p in sorted({ln["project"] for ln in others if ln["pend"]}):
            lns = [ln for ln in others if ln["project"] == p]
            mine_p = [ln for ln in own if ln["project"] == p]
            free = None if None in [ln["end"] for ln in mine_p] else max([now] + [ln["end"] for ln in mine_p])
            o = {"project": p, "lane": None, "free": free and round(free), "jobs": [], "why": None, "stop": None, "idle": idle,
                 "ends": {"%s|%s|%s" % (ln["host"], ln["root"], ln["queue"]): ln["end"] and round(ln["end"]) for ln in lns}}
            out.append(o)
            hit = self.override_hit({"project": p, "name": ""}, ("drain", "guest", "pause"), lasting=True)
            # 本机清单只是停在依赖上（等别处的任务，常是等这些候选本身，#167）不挡：挪来后一起重推，own 说得出就有整批
            if hit or renew or any(ln["end"] is None and not ln["dep"] for ln in mine_p):
                o["why"] = (hit[1] if hit else "这台在预备续租（%s），不接别处的任务" % _C.ovr_text(renew[0]) if renew
                            else "这台上本项目的清单推演不出何时跑完")
                continue
            cands, o["why"] = self.move_cands(lns, now, waiters, skip=idle)
            o["room"] = self.lane_room(p, cands, sum(len(ln["pend"]) for ln in lns), cx["base"], now)    # 这台吃不满、还能多开几条（#145）
            # 接在本机这个项目、与挪来的行同一种环境（目录与 .env）的清单里预计最早跑完的那条后面（推演里在排的才算）；没有就当新的一条
            ends = {(ln["root"], ln["queue"]): ln["end"] for ln in mine_p}
            env = cands and self.list_env(cands[0]["root"], cands[0]["queue"])
            tgt = sorted((q for q in qs if q["project"] == p and q["kind"] == "queue"
                          and self.list_env(info[q["key"]]["root"], info[q["key"]]["queue"]) == env),
                         key=lambda q: ends.get((info[q["key"]]["root"], info[q["key"]]["queue"])) or now)[:1]
            o["lane"] = [info[tgt[0]["key"]]["root"], info[tgt[0]["key"]]["queue"]] if tgt and cands else None
            o["before"] = self.move_before(p, cands, tgt[0]) if o["lane"] else None
            src = {(ln["host"], ln["root"], ln["queue"]): ln["end"] for ln in lns}    # 原处各清单挪走前 k 行后何时跑完
            for k in range(1, len(cands) + 1):
                got = self.try_move(p, cands[:k], tgt[0]["key"] if tgt else None, cx, before=o["before"])
                if isinstance(got, dict):
                    o.update(got)
                    break
                c = cands[k - 1]
                o["jobs"].append(dict({x: v for x, v in c.items() if x not in ("h", "i", "lane")}, end=got[0], own=got[1],
                                      dep=bool(c["h"]["deps"] or c["h"]["deps_any"])))
                src[(c["host"], c["root"], c["queue"])] = c["rest"]
                if self.moved_past(got[0], list(src.values())):
                    break
        out += self.local_offers(own, others, now, waiters, renew, cx)
        return out

    def move_before(self, p, cands, q):
        """项目 p 挪来的几行 cands 接进本机清单 q（这一遍推演里的那条，forecast 的 qs 项）时插在哪一行前面：q 里第一条写了 after=/afterany= 的行
        （它要等别的任务，清单顺序也算依赖，接在它后面会被它堵住，#43）；挪来的有（直接或经别的清单间接）等它或它后面哪一行的不插
        （插在前面就成环，Codex 审 #63），为 None：接在末尾。跨主机接过来（offers）与同机挪到另一条清单（lane_offer）共用。"""
        b = next((x["name"] for x in q["jobs"] if x["deps"] or x["deps_any"]), None)
        if b:
            tj = [x["name"] for x in q["jobs"]]
            if self.dep_reaches([k[:2] for c in cands for k in _DP.dep_keys(dict(c["h"], project=p))], {(p, n) for n in tj[tj.index(b):]}):
                return None
        return b

    @staticmethod
    def moved_past(end, rest):
        """挪来的前 k 行都跑完的时刻 end 已不早于原处剩下的（rest：原处各清单挪走这 k 行后何时跑完）里最晚的：再往下多挪一行，挪来的只会更晚跑完、
        原处剩下的也早不过它，整批与按行比（看板的 _idle_gain）都不会更好，offers 与同机的两种挪法据此不再往下试（每试一行要推演一遍，#110）。
        原处有说不出何时跑完的（None）不判，照旧试到 MOVE_MAX。"""
        return end is not None and bool(rest) and None not in rest and end >= max(rest)

    def idle(self, now):
        """这台此刻空着（#62）：没有 labmon 任务在跑（占位任务不算，保活不是 labmon 任务），这一次的推演里也没有 MOVE_LEAD_S 内就开跑的。
        offers 据此在别处挪不动、说不出何时放行的清单上跳过而不是整个项目不挑（move_cands 的 skip），看板按行比（rebalance.suggest）。
        只看 labmon 自己的任务：外来进程占着的显存推演里照算，放不下的 try_move 照样排不上。"""
        if any(not _C.is_hold(j["name"]) for j in self.running.values()):
            return False
        return not any(not x.get("run") and not _C.is_hold(x["name"]) and x["t0"] < now + _C.MOVE_LEAD_S
                       for ln in self.plan.get("lanes") or [] for x in ln["jobs"])

    def local_offers(self, own, others, now, waiters, renew, cx):
        """同机的两种挪法：本机这个项目排得最晚的清单队尾几行
        - 挪到本机另一条清单（#110，lane_offer）：接在同一种环境、预计最早跑完的那条后面；
        - 剪到这台新开的一条车道（#43）。
        挑行（move_cands：从队尾往前、占位、成组、有人 after= 等着、MOVE_LEAD_S 内开跑、缺本地文件的都停）、推演（try_move，同一份 simulate）、
        看板那头的门槛与挑法都同跨主机的。新开车道多两条：卡要有真余量（local_cards：限流、卡都吃满了、利用率低的卡上在跑的被 CPU 卡住都不出），
        估计只有提示值的 GPU 行不挪（没有实测，同卡并行怕 OOM，同 fit）。每项同 offers 的形状（新开车道的也带 room，#145），另带 local: true；lane 为 None 是新开一条，
        否则是接在后面的那条清单；ends 是别的主机上这个项目各清单的，free 是本机这个项目原推演里何时跑完；jobs 的 rest 是挪完后本机原清单何时跑完
        （新推演里，同卡变慢也算进去）。不出的不列（原因不叫醒、页面上也不写：fit 会说）。"""
        out = []
        if renew:
            return out
        for p in sorted({ln["project"] for ln in own if ln["pend"]}):
            if self.override_hit({"project": p, "name": ""}, ("drain", "guest", "pause"), lasting=True):
                continue
            lns = [ln for ln in own if ln["project"] == p and ln["state"] != "trapped"]
            mine_p = [ln for ln in own if ln["project"] == p]
            free = None if None in [ln["end"] for ln in mine_p] else max([now] + [ln["end"] for ln in mine_p])
            ends = {"%s|%s|%s" % (ln["host"], ln["root"], ln["queue"]): ln["end"] and round(ln["end"]) for ln in others if ln["project"] == p}
            lo = free is not None and self.lane_offer(p, lns, free, now, waiters, cx, ends)
            if lo:
                out.append(lo)
            cards, why = self.local_cards()
            if why or any(q.get("project") == p and (q.get("par") or 1) > max(1, len(self.lane(q["key"]))) for q in cx["qs"]):
                continue                             # 这个项目有窗口没满的并行清单（#147）：卡有余量时 worker 自己会给它加行，不另建议新开车道
            # 原推演何时跑完推不出（free 为 None）时算不了收益，只看是不是因为行没有估时（那样的清单推演里排不上，state 是 trapped，也一起看）
            cands, why = self.move_cands(lns if free is not None else mine_p, now, waiters)
            for i, c in enumerate(cands):           # 只有提示值的 GPU 行不挪：没有实测，同卡并行怕 OOM（同 fit）
                e = _S.estimate(self.hist, dict(c["h"], name=c["name"], project=p, root=c["root"], queue=c["queue"]))
                if c["h"]["ngpu"] and e["src"] == "提示值":
                    cands, why = cands[:i], "%s 的显存只有清单提示值、还没有实测，不与别的任务同卡" % c["name"]
                    break
            # gpus：新车道只许用的卡（推演就按它排）；不是全部卡时建议的 worker add 带 --gpus，否则真放行可能放到吃满的卡上（Codex 审 #63）
            o = {"project": p, "local": True, "lane": None, "free": free and round(free), "jobs": [], "why": why, "stop": None,
                 "gpus": cards if cards and len(cards) < len(self.info.get("gpus") or []) else None, "ends": ends}
            if free is None:                         # 只在是因为要挪的行没有估时才留一条 nodur（看板出 :est，让会话写 est_min；Codex 审 #63）
                nd = next((c["name"] for c in cands if (self.dur_k(dict(c["h"], name=c["name"], project=p, root=c["root"], queue=c["queue"]))(self.k_now())
                                                         or {}).get("min") is None), None)
                if nd:
                    out.append(dict(o, stop=nd, why=None, nodur=True))
                continue
            o["room"] = self.lane_room(p, cands, sum(len(ln["pend"]) for ln in lns), cx["base"], now)    # 这台吃不满、还能多开几条（#145）
            self.local_moves(o, p, cands, None, cx, now, local=cards)
            if o["jobs"] or o["stop"]:
                out.append(o)
        return out

    def local_moves(self, o, p, cands, tgt, cx, now, local=None):
        """同机挪法逐个加行：把前 k 个候选从本机原清单拿掉、挪到 tgt（本机清单的 key；None 新开一条只许用 local 这几张卡的车道）推演一遍，
        结果记进 offer o 的 jobs（rest 是新推演里原清单何时跑完：整条剪空、也没在跑的现在就跑完了）；排不上的那一行与原因记进 o（try_move 的 stop、why、nodur）。
        挪来的已比本机剩下的晚跑完（moved_past）就不再往下试。"""
        for k in range(1, len(cands) + 1):
            got = self.try_move(p, cands[:k], tgt, cx, local=local, before=o.get("before"))
            if isinstance(got, dict):
                o.update(got)
                return
            c = cands[k - 1]
            o["jobs"].append(dict({x: v for x, v in c.items() if x not in ("h", "i", "lane")}, end=got[0], own=got[1],
                                  dep=bool(c["h"]["deps"] or c["h"]["deps_any"]), rest=got[2].get((c["root"], c["queue"]), round(now))))
            if self.moved_past(got[0], [got[1]]):
                return

    def lane_offer(self, p, lns, free, now, waiters, cx, ends):
        """同机挪到另一条清单（#110）：本机这个项目排得最晚的清单（同卡几条车道争卡、它排在别的车道后面才跑完，或只是行多）队尾几行，
        接到本机同一种环境（目录与 .env，list_env）、预计最早跑完的另一条清单后面（那条里有写了 after= 的行就插在它前面，move_before），整批能提前多少。
        不加车道、同时跑的条数不变，所以不看卡的余量（local_cards）、只有提示值的 GPU 行照挪；挑行（move_cands）、推演（local_moves → try_move：
        从原队列拿掉、接进那条清单）、看板的门槛都同跨主机的。返回 offers 的一项（local: true，lane 是接在后面的那条 [目录, 清单]），
        没有能挪的为 None（不写原因，同新开车道）。lns：本机这个项目在原处跑得了的清单（proj_lanes）；free：本机这个项目原推演里何时跑完；
        ends：别的主机上这个项目各清单何时跑完。"""
        keys = {(v["root"], v["queue"]): k for k, v in cx["info"].items() if v.get("kind") == "queue"}
        live = [ln for ln in lns if ln["end"] is not None and (ln["root"], ln["queue"]) in keys]
        late = max(live, key=lambda ln: ln["end"], default=None)
        if late is None or not late["pend"]:
            return None
        env = self.list_env(late["root"], late["queue"])
        tgt = min((ln for ln in live if ln is not late and self.list_env(ln["root"], ln["queue"]) == env), key=lambda ln: ln["end"], default=None)
        if tgt is None:
            return None
        cands, why = self.move_cands([ln for ln in lns if ln is not tgt], now, waiters)
        q = next(q for q in cx["qs"] if q["key"] == keys[(tgt["root"], tgt["queue"])])
        o = {"project": p, "local": True, "lane": [tgt["root"], tgt["queue"]], "before": self.move_before(p, cands, q), "free": round(free),
             "jobs": [], "why": why, "stop": None, "gpus": None, "ends": ends}
        self.local_moves(o, p, cands, q["key"], cx, now)
        return o if o["jobs"] else None

    def move_cands(self, lns, now, waiters, skip=False):
        """offers 的候选：别的主机上这个项目的清单 lns 里（同机挪时是本机的清单），按“取此刻排得最晚的那条清单的队尾”挑出的几行（至多 MOVE_MAX），
        与挑不下去的原因。各候选带 host、root、queue、name、there（原处预计结束，跑不了为 None）、
        rest（挪走它后那条清单何时跑完：剩下的行与在跑的预计结束里最晚的）、h（资源提示）、i（在原清单里排第几）、lane（第几条清单）：前 k 行加进推演时按 (lane, i) 排，
        同一条清单里的照原先后。挑到哪一行为止由调用方逐行推演后定：挪过来的已比原处剩下的晚跑完就不再往下试（moved_past）。
        不按本机这个项目原推演里何时跑完预先停：本机有行在等依赖（汇总行 after= 等别处的训练行）、卡有余量能并行时，那不是挪来的行最早何时跑完（#167）。
        waiters：{(项目, 任务): {写了 after= 等它的 (项目, 任务)}}（dep_waiters(sim=True)：本机与别的主机的 todo，本机推演里跟着重推的不算）；
        有不在这批里的等着它就不挪：挪走后等它的何时开始会变。
        有清单原处何时放行说不出（state unknown）的一行也不挑：整批推不出，挪了也算不出提前多少。一批只取同一种环境（list_env：目录与 .env）的行。
        skip（接收方空着，Worker.idle，#62）：看板按行比、不要整批何时跑完，所以说不出何时放行的清单、队尾挪不动的清单（上面那些停的原因）
        跳过这一条、接着看别的清单，不整批停；原因取第一条跳过的。"""
        unk = next((ln for ln in lns if ln["state"] == "unknown"), None)
        if unk and not skip:
            return [], unk["why"]
        end = {i: ln["end"] for i, ln in enumerate(lns)}
        left = {i: list(ln["pend"]) for i, ln in enumerate(lns) if ln["state"] != "unknown"}
        out, why = [], unk and unk["why"]
        while True:
            xs = [i for i in left if left[i]]
            if not xs:
                break
            i = max(xs, key=lambda i: math.inf if end[i] is None else end[i])
            ln, (n, h, t0, t1) = lns[i], left[i][-1]
            if len(out) >= _C.MOVE_MAX:
                if end[i] is None:
                    return out, "%s 上 %s 在原处跑不了的超过 %d 行，一次只挑这么多" % (ln["host"], ln["queue"], _C.MOVE_MAX)
                break
            stop = self.cant_move(ln, n, h, t0, out, now, waiters)
            if stop:
                if not skip:
                    return out, stop
                why, left[i] = why or stop, []
                continue
            left[i].pop()
            ts = [ln["run"]] + [x[3] for x in left[i]]   # 剩下的与在跑的里最晚的（并行清单先开跑的长任务可能比后面的行晚跑完，#147）
            end[i] = None if None in ts else max(ts)
            out.append({"host": ln["host"], "root": ln["root"], "queue": ln["queue"], "name": n, "there": t1 and round(t1),
                        "rest": end[i] and round(end[i]), "h": h, "i": len(left[i]), "lane": i})
        return out, why

    def cant_move(self, ln, n, h, t0, out, now, waiters):
        """清单 ln 的队尾 n（资源提示 h、原处推演的开始 t0）为什么不挪到这批 out 里（move_cands 用）；能挪为 None。"""
        if out and self.list_env(ln["root"], ln["queue"]) != self.list_env(out[0]["root"], out[0]["queue"]):
            return "再往前是 %s 上另一个目录或环境（.env）的清单 %s，一次只挪一种" % (ln["host"], ln["queue"])
        if _C.is_hold(n):
            return "%s 上 %s 的队尾 %s 是占位任务，不挪" % (ln["host"], ln["queue"], n)
        if h is None:
            return ("这台读不到 %s 上的 %s，不挪" % (ln["host"], os.path.join(ln["root"], ln["queue"])) if ln["unread"]
                    else "%s 不在 %s 里（刚剪过？），不挪" % (n, os.path.join(ln["root"], ln["queue"])))
        if h.get("gang"):
            return "%s 上 %s 的队尾 %s 是成组的（gang=%s），挪了要连同组的一起看，不挪" % (ln["host"], ln["queue"], n, h["gang"])
        if t0 is not None and t0 - now < _C.MOVE_LEAD_S:
            return "%s 上的 %s %d 分钟内就开跑，不挪" % (ln["host"], n, _C.MOVE_LEAD_S / 60)
        ws = waiters.get((ln["project"], n), set()) - {(ln["project"], x["name"]) for x in out}
        if ws:
            return "%s 有 %s 写了 after= 等着它，不挪" % (n, "、".join(sorted(x for _, x in ws))[:200])
        if h["needs"]:
            q = _Q.load_queue({"_slug": _Q.qslug(ln["root"], ln["queue"]), "root": ln["root"], "jobs": ln["queue"], "project": ln["project"]})
            job = dict(h, name=n, project=ln["project"], cmd=_Q.job_cmd(q, n) or "")
            miss = [c["pat"] for c in _Q.check_needs(job, self.qenv(q)) if not c["files"] and not c["heal"]]
            if miss:
                return "%s 缺本地文件：%s（这台上没有，也补不了）" % (n, "、".join(miss[:3]))
        return None

    def try_move(self, p, cs, tgt, cx, local=False, before=None):
        """假设把项目 p 的候选 cs 挪到这台推演一遍（同一份 simulate，本机的快照、截止、公平份额与依赖都照真推演）：
        返回 (它们都跑完的时刻, 这时本机这个项目自己的清单何时都跑完, 本机原清单何时跑完 {(目录, 清单): 时刻}（同机挪时才有）)；
        有排不上的返回 {stop 那一行, why 推演里的原因, nodur 是不是因为那一行没有估时（看板据此提醒会话写 est_min，#43）}。
        tgt：接在后面的本机清单的 key（None：当新的一条）；cx：forecast 这一遍的输入（qs、base、info…）。
        挪来的几行按在那条车道里的位置认（接在原有的行后面，车道按顺序放），不按名字：同项目几条清单可能有同名的行。
        local（同机的挪法，local_offers；不是 False 时）：候选是本机清单的队尾，从原队列里拿掉（原样搬过去，估计与时长不另算），接进 tgt 那条本机清单
        （同机挪到另一条清单，lane_offer），没给 tgt 就放进只许用这几张卡的新车道（local 是卡号列表；None 不限）；队尾与候选对不上（清单刚变过）就不挪。
        挪来的几行里没写 after=/afterany= 的排在写了的前面（清单顺序也算依赖：独立的行排在等别的任务的行后面会被它堵住，#43）；
        接在本机清单后面时插在那条清单里第一条写了依赖的行 before 前面（move_before 定，文字里写明）。"""
        jobs, qs, cut = [], [], {}
        if local is not False:
            keys = {(v["root"], v["queue"]): k for k, v in cx["info"].items() if v.get("kind") == "queue"}
            for c in cs:
                cut.setdefault(keys.get((c["root"], c["queue"])), []).append(c["name"])
        for q in cx["qs"]:
            ns = cut.get(q["key"])
            if ns:
                if [x["name"] for x in q["jobs"][-len(ns):]] != ns[::-1]:
                    return {"stop": ns[0], "why": "%s 的队尾刚变过" % cx["info"][q["key"]]["queue"], "nodur": False}
                jobs, q = q["jobs"][-len(ns):] + jobs, dict(q, jobs=q["jobs"][:-len(ns)])
            qs.append(q)
        if None in cut:
            return {"stop": cut[None][0], "why": "推演里没有它所在的清单", "nodur": False}
        for c in sorted(cs, key=lambda c: (c["lane"], c["i"])) if local is False else ():
            f = self.dur_k(dict(c["h"], name=c["name"], project=p, root=c["root"], queue=c["queue"]))   # 与 forecast 拼本机清单行同口径：dur 取此刻的 k，durk 给推演按开跑时同卡的条数取
            jobs.append(self.job_spec(c["name"], c["h"], _S.estimate(self.hist, dict(c["h"], name=c["name"], project=p, root=c["root"], queue=c["queue"])),
                                      (f(self.k_now()) or {}).get("min"), f, (f(self.k_now()) or {}).get("cut")))
        jobs = [x for x in jobs if not (x["deps"] or x["deps_any"])] + [x for x in jobs if x["deps"] or x["deps_any"]]
        key, n0 = tgt or "offer:" + p, 0
        for i, q in enumerate(qs):
            if q["key"] == key:
                n0 = next((n for n, x in enumerate(q["jobs"]) if x["name"] == before), len(q["jobs"]))
                qs[i] = dict(q, jobs=q["jobs"][:n0] + jobs + q["jobs"][n0:])
        if not tgt:
            qs.append({"key": key, "kind": "queue", "project": p, "allowed": None if local is False else local, "since": cx["now"], "jobs": jobs})
        out, stuck = _FC.simulate(cx["now"], cx["base"], qs, cx["focus"], self.cutoff(), pol=cx["pol"], use=cx["use"], ends=self.ends, dead=self.dead, gangs=self.gsim)
        moved = sorted((e for e in out if e["lane"] == key and n0 <= e["_i"] < n0 + len(jobs)), key=lambda e: e["_i"])   # 按在车道里的位置（并行车道可能不按顺序开跑，#147）
        bad = next((i for i, e in enumerate(moved) if e["t1"] is None), len(moved) if len(moved) < len(jobs) else None)
        if bad is not None:
            return {"stop": jobs[bad]["name"], "why": stuck.get(key) or "挪来后不知道何时跑完", "nodur": jobs[bad]["dur"] is None}
        ids = {id(e) for e in moved}
        lanes = self.plan_lanes(cx["base"]["jobs"], [e for e in out if e["lane"] in cx["info"] and id(e) not in ids], {}, cx["info"], {})
        gone = {(cx["info"][k]["root"], cx["info"][k]["queue"]): set(ns) for k, ns in cut.items()}
        todo = [dict(ln, jobs=[n for n in ln["jobs"] if n not in gone.get((ln["root"], ln["queue"]), ())]) for ln in self.todo if ln["project"] == p]
        own = self.proj_lanes(self.host, todo, {"lanes": lanes}, cx["mine"], cx["held"])
        ends = [ln["end"] for ln in own]
        return (round(max(e["t1"] for e in moved)), None if None in ends else round(max([cx["now"]] + ends)),
                {(ln["root"], ln["queue"]): ln["end"] and round(ln["end"]) for ln in own if (ln["root"], ln["queue"]) in gone})

    def local_cards(self):
        """offers 的同机多开车道能往哪几张卡放（#43）：(卡号列表, None)，或 (None, 不出的原因)。与 fit 同一条规则（gpu_slack）：容器在限流不出；
        近 10 分钟利用率不低于 UTIL_FULL 的卡（吃满了）、利用率低于 UTIL_LOW 而上面在跑的被 CPU 卡住的卡（先加核）不放；还没有利用率样本的卡照放。"""
        now = _C.time.time()
        u10, gs = self.util10(now), self.info.get("gpus") or []
        cards = [{"i": g["i"], "uuid": g["uuid"], "util": u10.get(g["uuid"])} for g in gs]
        sl = _S.gpu_slack(cards, list(self.running.values()), self.throttle(now), _M.cpu_cap(self.info.get("cpu_quota")))
        if sl["throttle"] is not None:
            return None, sl["throttle"] + "：先减线程，不在这台多开车道"
        ok = [c["i"] for c in cards if (c["util"] is None or c["util"] < _C.UTIL_FULL) and c["uuid"] not in sl["bound"]]
        if ok or not cards:
            return ok or None, None
        if sl["bound"]:
            return None, "利用率低的卡上在跑的任务跑满了给它的核：先给它们加核（cpus=），不在这台多开车道"
        return None, "各卡近 10 分钟利用率都不低于 %d%%，不在这台多开车道" % _C.UTIL_FULL

    def lane_room(self, p, cands, rows, base, now):
        """offers 的 room（#145）：这台此刻还能给项目 p 再开几条车道、能放的卡吃得多满，看板据此判这台吃不满、提示会话多开车道摊行
        （rebalance.suggest）。卡要有真余量（local_cards，同 fit 的 gpu_slack），候选 cands 的第一行要 GPU（不用卡的车道不看卡）；不出为 None。
        返回 {busy 能放的卡近 UNDER_WIN_S「吃满程度」的平均（bwin，只有保活的那几轮记 0；窗口没满为 None）,
        lanes 用第一行的资源形状在此刻的快照 base 上逐条 place、按 acct_job 记进快照再判下一条（同 FitHost.open），至多 min(ROOM_MAX, rows)}。
        只是此刻的资源：不扣别的项目在等的请求，会话照做前用 fit --list 核对。"""
        if not cands or not cands[0]["h"]["ngpu"]:
            return None
        cards, why = self.local_cards()
        if why or not cards:
            return None
        uu = {g["i"]: g["uuid"] for g in self.info.get("gpus") or []}
        ms = []
        for i in cards:
            b = self.bwin.get(uu.get(i)) or []
            ms.append(_M.win_mean(b, now, _C.UNDER_WIN_S) if b and now - b[0][0] >= _C.UNDER_WIN_S - 60 else None)
        busy = None if not ms or None in ms else round(sum(ms) / len(ms), 1)
        c = cands[0]
        r = dict(c["h"], name=c["name"], project=p, root=c["root"], queue=c["queue"])
        f = self.dur_k(r)
        x = dict(self.job_spec(c["name"], c["h"], _S.estimate(self.hist, r), (f(self.k_now()) or {}).get("min"), f), project=p, allowed=cards)
        S = dict(base, jobs=list(base["jobs"]), holds=dict(base.get("holds") or {}), gholds={})
        n = 0
        while n < min(_C.ROOM_MAX, rows):
            y = dict(x, key="room:%s:%d" % (p, n))
            cs, _, _ = _S.place(y, S, now)
            if cs is None:
                break
            S["jobs"].append(_S.acct_job(y, cs, now, None, S["cpu_cap"]))
            n += 1
        return {"busy": busy, "lanes": n}
