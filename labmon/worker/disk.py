"""worker 共享盘（Disk）与容器本地盘（LocalDisk）的限速轮流扫描。"""
import json
import os
import stat
import threading

from . import common as _C

# 容器根盘开机以来的新写入（续租、关机、换人会清空，#149）：LocalDisk 按共享盘扫描同一套调度扫容器自己的根盘，只进开机后动过的目录（见 scan_unit 的 prune）
LOCAL_IOPS = 1000        # 本地盘每秒最多这么多次列目录、stat（NVMe 上 ionice 多半不起作用，同样自己数）


LOCAL_DRIFT = 64 * 2 ** 20   # labmon 之外的新写入（根盘已用 − labmon 自己的）比上次对账变了这么多，最近一天有写入的单位提前重扫（看板扫组内服务器用同一个数：fleet.WROTE_DRIFT）


LOCAL_SKIP = ("proc", "sys", "dev", "run")   # 根盘下不扫的（各自挂载的伪文件系统、运行时文件）；别的挂载点（共享盘、/dev/shm）按设备号不进


LOCAL_TOP = 8            # 写进状态的新写入目录取前这么多个，其余合成一项


# 共享盘（组里各服务器挂的同一块 NFS）的用量：拿着扫描租约的一台 worker 扫全盘（见 Disk）。
# NFS 上 ionice 不管用，只能自己数元数据操作限速；总量与剩余直接读 statvfs，明细按冷热轮流扫。
SCAN_IOPS = 200          # 每秒最多这么多次列目录、stat


SCAN_HOT_S = 3600        # 上次扫出变化的单位隔这么久可再扫


SCAN_MAX_S = 7 * 86400   # 一直没变的单位间隔翻倍，最长这么久


SCAN_COST = 50           # 同一单位两次至少隔上次耗时的这么多倍


SCAN_SPLIT_S = 1200      # 一个单位扫超过这么久就停下，拆成它的各子目录分别扫


SCAN_LIST_S = 600        # 多久列一次一、二级目录：看新建、删除与目录自身的修改时间


SCAN_DRIFT = 10 * 2 ** 30   # df 已用比上次对账变了这么多，就把最近一天有修改的单位提前扫


SCAN_LEASE_S = 900       # 别的主机这么久没续扫描租约，本机接着扫


SCAN_SAVE_S = 10         # 一件接一件做的时候索引最多这么久写一次（整份重写；手上没活了立刻写）


SCAN_AGES = (7, 30, 90, 365)   # 按修改时间分龄的界（天）


class Pace:
    """限速：数元数据操作，快过每秒 iops 次就睡；这一件超过 limit 秒抛 TimeoutError（拆开再扫，按这一件自己的用时与次数）。
    rate：同一个扫描者接连几件共用的计数（Disk.rate，#153）：一件接一件做时合起来也守每秒 iops 次（每件各算各的话，不满 20 次的小件从不睡）；
    开始一件时实际已慢于 iops（闲过）就从头算，闲着的时间不攒成一阵猛扫。"""

    def __init__(self, iops, limit=None, rate=None):
        self.iops, self.limit, self.k, self.t = iops, limit, 0, _C.time.time()
        r = self.r = {} if rate is None else rate
        if not r or r["t0"] + r["n"] / iops < self.t:
            r.update(t0=self.t, n=0)

    def __call__(self):
        r = self.r
        r["n"] += 1
        self.k += 1
        if self.limit and self.k % 20 == 0 and _C.time.time() - self.t > self.limit:
            raise TimeoutError
        if r["n"] % 20 == 0:
            ahead = r["n"] / self.iops - (_C.time.time() - r["t0"])
            if ahead > 0:
                _C.time.sleep(ahead)


def dot(d):
    """目录 d 本层的文件这个扫描单位的键：<目录>/.（根目录是 /.）。"""
    return d.rstrip("/") + "/."


def scan_units(root, split, pace, prune=None, skip=()):
    """扫描单位 {路径: 目录自身的修改时刻}：一级目录下的各子目录（拆过的再往下一层），以及根、各一级目录（与拆过的目录）本层的文件（键为 <目录>/.）。
    prune（容器根盘，LocalDisk 给开机时刻）：ctime 早于它的目录整棵不要（overlay 上开机以后动过的目录都已复制到上层、ctime 晚于开机，
    早于开机的只在镜像层里，下面没有新写入）；不跨文件系统（共享盘、/proc 等挂载点不进），skip 里的也不进。"""
    out, base = {}, root.rstrip("/") or "/"
    dev = os.stat(base).st_dev if prune is not None else None

    def walk(d, depth):
        pace()
        try:
            es = list(os.scandir(d))
            out[dot(d)] = os.stat(d).st_mtime
        except OSError:
            return
        for e in es:
            if not e.is_dir(follow_symlinks=False):
                continue
            pace()
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if prune is not None and (e.path in skip or st.st_dev != dev or st.st_ctime < prune):
                continue
            if depth == 0 or e.path in split:
                walk(e.path, depth + 1)
            else:
                out[e.path] = st.st_mtime
    walk(base, 0)
    return out


def scan_unit(path, now, pace, prune=None, since=None):
    """扫一个单位：占用（按块，单位内硬链接只算一次，与 du 一致）、文件数、最新修改、按修改时间分龄的占用、
    直接子项里最大的 10 个 [名字, 占用, 最新修改]。<目录>/. 只看本层的文件。
    prune、since（容器根盘，LocalDisk 给）：ctime 早于 prune 的目录不进（同 scan_units）、不跨文件系统；只数 ctime 不早于 since 的文件
    （开机以来的新写入；借来的主机上 since 是借来的时刻，prune 仍是开机时刻：只按借来时刻剪会漏掉旧目录里新建的文件），
    目录本身的块不算，“最新修改”取 ctime（写进这个容器的时刻）。"""
    flat = path.endswith("/.")
    b = n = 0
    m, age, top, tm, seen = 0.0, [0] * (len(SCAN_AGES) + 1), {}, {}, set()
    first = (path[:-2] or "/") if flat else path
    dev = os.stat(first).st_dev if prune is not None else None
    stack = [(first, None)]
    while stack:
        d, head = stack.pop()
        pace()
        try:
            es = list(os.scandir(d))
        except OSError:
            continue
        for e in es:
            pace()
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            isdir = stat.S_ISDIR(st.st_mode)
            if isdir and (flat or prune is not None and (st.st_dev != dev or st.st_ctime < prune)):
                continue
            if not isdir and st.st_nlink > 1:
                if (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
            h = head or e.name
            if isdir:
                if since is None:
                    b += st.st_blocks * 512
                    top[h] = top.get(h, 0) + st.st_blocks * 512
                stack.append((e.path, h))
                continue
            if since is not None and st.st_ctime < since:
                continue
            sz, t = st.st_blocks * 512, st.st_mtime if since is None else st.st_ctime
            b += sz
            n += 1
            m = max(m, t)
            if not flat:
                top[h] = top.get(h, 0) + sz
                tm[h] = max(tm.get(h, 0), t)
            age[sum(1 for x in SCAN_AGES if now - st.st_mtime > x * 86400)] += sz
    return {"b": b, "n": n, "m": round(m) or None, "age": age,
            "top": sorted(([k, v, round(tm.get(k, 0)) or None] for k, v in top.items()), key=lambda kv: -kv[1])[:10]}


class Disk:
    """共享盘用量。各主机挂的是同一块盘，只有拿着租约（<HOME>/disk/lease）的那台扫，别的主机只续看。
    扫描在后台线程里一次做一件（列单位或扫一个单位），调度在主线程：每拍的 scan_local，拍与拍之间做完一件就接着下一件（Worker.scan_chain，#153）。
    没扫过的先扫；上次有变化的隔 SCAN_HOT_S，没变的间隔翻倍到 SCAN_MAX_S；目录自身修改时刻比上次扫新、有任务跑完（hint）、df 已用变了很多的提前。
    共享盘慢时不开始新的一件（step 的 defer）。限速按扫描者算（rate：接连几件合起来守 iops）；耗时由扫描线程自己量。
    索引 <HOME>/disk/index.json：各单位的占用、文件数、最新修改、分龄、最大子项、扫完的时刻与耗时，以及上一次的占用（看板算增速）；
    接连做的时候最多 SCAN_SAVE_S 写一次。
    容器根盘的新写入（LocalDisk）用同一套调度，只换根、租约、索引的位置、限速与“变了很多”的算法。"""
    key = "scan"             # io_yield 推迟这件事用的键
    iops = SCAN_IOPS

    def __init__(self, root, host):
        self.root, self.host = root.rstrip("/"), host
        self.dir = os.path.join(_C.HOME, "disk")
        self.ixf = os.path.join(self.dir, "index.json")
        self.ix, self.job, self.mine, self.lease_t, self.wait = None, None, False, 0.0, None   # wait：共享盘慢、推迟中的说明
        self.rate, self.saved, self.dirty = {}, 0.0, False   # 接连几件共用的限速计数（Pace）；索引上次写盘的时刻、有没有没写的
        self.roots, self.lsplit = set(), set()   # 登记了清单的项目根（want）；这次列目录用的拆开的目录（列完并进索引的 split）

    def lease(self, now):
        """每分钟看一次租约：别的主机 SCAN_LEASE_S 内续过就不扫；否则（或本来就是本机）续上。"""
        if now - self.lease_t < 60:
            return self.mine
        self.lease_t = now
        f = os.path.join(self.dir, "lease")
        who = (_C.read(f, "") or "").split()
        mine = not who or who[0] == self.host or len(who) < 2 or now - float(who[1]) > SCAN_LEASE_S
        if mine:
            os.makedirs(self.dir, exist_ok=True)
            _C.write(f, "%s %d" % (self.host, now))
            if not self.mine:                   # 刚接手：读上一个扫描者留下的索引
                self.load()
        self.mine = mine
        return mine

    def load(self):
        try:
            self.ix = json.loads(_C.read(self.ixf, "") or "{}")
        except ValueError:
            self.ix = {}

    def want(self, roots):
        """登记了清单的项目根（各台状态的 storage.projects，Worker.scan_local 合起来给）：下一次列目录时把它们连同在二级以下的各级父目录
        一起拆开（与扫得太慢拆开的同一个 split），项目根下的一级目录各成一个单位、按项目分开统计（#53）。只记下来，列目录时才用。"""
        self.roots = set(roots)

    def forced(self):
        """want 的项目根要拆开的目录：根下第二级起到项目根本身（第一级本来就进）。不在这块盘上的不管。"""
        out = set()
        for r in self.roots:
            rel = os.path.relpath(os.path.normpath(r), self.root)
            parts = [x for x in rel.split("/") if x and x != "."]
            if parts and parts[0] != "..":
                out.update(os.path.join(self.root, *parts[:k]) for k in range(2, len(parts) + 1))
        return out

    def hint(self, d):
        """有任务在 d 下跑完：d 里的单位尽快重扫（仍守 SCAN_COST）。"""
        for u, r in ((self.ix or {}).get("units") or {}).items():
            if u == d or u.startswith(d.rstrip("/") + "/"):
                r["due"] = min(r.get("due", 0), r.get("t", 0) + SCAN_COST * r.get("s", 0))

    def lister(self, split, pace):
        return scan_units(self.root, split, pace)

    def scanner(self, u, now, pace):
        return scan_unit(u, now, pace)

    def step(self, now, defer=lambda key, now: None):
        """调度一步：拿着租约、上一件做完了，就收下它的结果、开始下一件（列目录或扫一个单位）。defer（Worker.io_yield）给出推迟的说明时
        这一步先不开始（共享盘慢时让路，推迟有上限，见 IO_YIELD_MAX_S），说明记在 yield（写进状态的 disk）。
        索引有没写的：手上没活了立刻写，接连做的时候隔满 SCAN_SAVE_S 才写。"""
        if not self.lease(now):
            return
        ix = self.ix
        ix.setdefault("units", {})
        ix.setdefault("split", [])
        if self.job and not self.job[0].is_alive():
            self.done()
        if not self.job:
            self.pick(now, defer)
        if self.dirty and (not self.job or now - self.saved >= SCAN_SAVE_S):
            self.save(now)

    def pick(self, now, defer):
        """挑下一件：先列目录（到点了），再扫到期的单位里最早到期的那个。"""
        ix = self.ix
        due = [(r.get("due", 0), u) for u, r in ix["units"].items() if r.get("due", 0) <= now]
        split = set(ix["split"]) | self.forced()
        # 到点了列目录；有新的项目根要拆开时提前列（列失败的隔一分钟再试）
        listing = now - ix.get("listed", 0) >= (60 if split - set(ix["split"]) else SCAN_LIST_S)
        self.wait = (listing or due) and defer(self.key, now) or None
        if self.wait:
            return
        if listing:
            self.lsplit = split
            self.start(now, "list", lambda pace: self.lister(split, pace), None)
            return
        if due:
            u = min(due)[1]
            self.start(now, u, lambda pace: self.scanner(u, now, pace), None if u.endswith("/.") else SCAN_SPLIT_S)

    def start(self, now, what, fn, limit):
        """在后台线程里做一件；扫描本身用了多久由线程自己量（out 的 s），不按主线程哪一拍收的结果算（#153）。"""
        out = {}

        def run():
            t = _C.time.monotonic()
            try:
                out["r"] = fn(Pace(self.iops, limit, self.rate))
            except TimeoutError:
                out["split"] = True
            except Exception as e:  # noqa: BLE001
                out["err"] = repr(e)
            finally:
                out["s"] = _C.time.monotonic() - t
        th = threading.Thread(target=run, daemon=True)
        self.job = (th, what, now, out)
        th.start()

    slack = SCAN_DRIFT       # 对账的量（acct_of）变了这么多才提前扫

    def acct_of(self):
        """对账看的量：共享盘是 df 已用。"""
        return (self.ix.get("df") or {}).get("used")

    def drift(self, now):
        """列完单位后对账：对账的量比上次对账变了 slack 以上（大文件追加、原地写入：目录修改时刻看不出），把最近一天有修改的单位提前扫。"""
        ix = self.ix
        try:
            s = os.statvfs(self.root)
            ix["df"] = {"total": s.f_blocks * s.f_frsize, "free": s.f_bavail * s.f_frsize,
                        "used": (s.f_blocks - s.f_bfree) * s.f_frsize, "t": round(now)}
        except OSError:
            pass
        used = self.acct_of()
        if used is not None and abs(used - ix.get("acct", used)) > self.slack:
            for r in ix["units"].values():
                if r.get("m") and now - r["m"] < 86400:
                    r["due"] = min(r.get("due", 0), r["t"] + SCAN_COST * r.get("s", 0))
            ix["acct"] = used
        ix.setdefault("acct", used)

    def done(self):
        """收下做完的那件：时刻都从它扫完的时刻（开始时刻 + 线程量的耗时）算。"""
        _, what, t0, out = self.job
        self.job = None
        s = out.get("s", 0.0)
        now = t0 + s
        ix, units = self.ix, self.ix["units"]
        if what == "list":
            ix["listed"] = round(now)
            got = out.get("r") or {}
            if got:
                ix["split"] = sorted(set(ix["split"]) | self.lsplit)   # 新拆开的与列出的子单位同一次写进索引：看板按它认“新拆开的”（disk.absorb）
                for u in list(units):
                    if u not in got:
                        units.pop(u)
                for u, dm in got.items():
                    r = units.setdefault(u, {"due": 0})
                    if r.get("t") and dm > r["t"]:     # 目录里增删过东西
                        r["due"] = min(r.get("due", 0), r["t"] + SCAN_COST * r.get("s", 0))
            self.drift(now)
        elif out.get("split"):
            ix["split"] = sorted(set(ix["split"]) | {what})
            units.pop(what, None)
            ix["listed"] = 0                          # 下一轮就列出它的子目录
        else:
            r = units.get(what, {})
            if "r" in out:
                new = out["r"]
                changed = r.get("b") is None or abs(new["b"] - r["b"]) > 2 ** 20 or new["n"] != r.get("n")
                gap = SCAN_HOT_S if changed else min(SCAN_MAX_S, 2 * r.get("gap", SCAN_HOT_S))
                units[what] = dict(new, t=round(now), s=round(s, 1), pb=r.get("b"), pt=r.get("t"), gap=gap,
                                   due=round(now + max(gap, SCAN_COST * s)))
            else:                                     # 出错：一天后再试
                units[what] = dict(r, err=out.get("err"), due=round(now + 86400))
        self.dirty = True

    def save(self, now):
        ix = self.ix
        ix.update(root=self.root, host=self.host, t=round(now))
        os.makedirs(os.path.dirname(self.ixf), exist_ok=True)
        _C.write(self.ixf, json.dumps(ix, ensure_ascii=False, separators=(",", ":")))
        self.saved, self.dirty = now, False

    def brief(self):
        """写进 status.json 的一行：是不是本机在扫、正在扫什么、共享盘慢推迟中的说明（yield）。"""
        return {"root": self.root, "mine": self.mine, "now": self.job[1] if self.job else None, "since": round(self.job[2]) if self.job else None,
                "yield": self.wait}


class LocalDisk(Disk):
    """容器根盘开机以来的新写入（续租、关机、换人会清空，#149），与共享盘的 Disk 同一套调度：一次做一件、Pace 限速、变了的一小时后再扫、
    没变的间隔翻倍、太慢的拆开、任务跑完提前（hint）、共享盘慢时也让路（键 lscan）。不同的只有：每台扫自己的、不要租约；索引在容器本地
    LOCAL/disk.json（续租即清）；只进开机以后动过的目录（scan_units、scan_unit 的 prune：overlay 上就是开机以来写过东西的那些）；
    “变了很多”看 labmon 之外的新写入（根盘已用 − labmon 目录的占用）比上次对账变了 LOCAL_DRIFT（共享盘是 df 已用变了 SCAN_DRIFT），
    变了同样把最近一天有写入的单位提前重扫。
    labmon 自己的目录（LOCAL）拆到 cache/<项目>、jobs/<项目> 一级：同一次扫描给出各项目缓存与单格临时（summary）。
    根盘不是 overlay、或不知道开机时刻（info.rootfs 测不准）时只扫 LOCAL；借来的主机上 since 是借来的时刻（只数借用期间的新写入，给归还确认）。"""
    key = "lscan"
    iops = LOCAL_IOPS
    slack = LOCAL_DRIFT

    def __init__(self, host):
        Disk.__init__(self, _C.ROOTFS, host)
        self.ixf = os.path.join(_C.LOCAL, "disk.json")
        self.mine, self.cfg = True, None

    def setup(self, prune, since, borrow=False):
        """这一轮的扫法：prune 为 None（测不准）只扫 LOCAL；否则扫整个根盘、只数 since 以后的。扫法变了（换了容器、开始或结束借用）整份索引作废。
        borrow：借来的主机，since 是 guest 指令的时刻；借用中指令重下（预留变了）时刻会往后挪，这时沿用这次借用里最早的那个，不作废索引。"""
        if self.ix is None:
            self.load()
        old = (self.ix.get("cfg") or {}).get("since") or 0
        if borrow and prune is not None and since is not None and (self.ix.get("cfg") or {}).get("prune") == prune and prune < old < since:
            since = old
        cfg = {"root": (_C.ROOTFS.rstrip("/") or "/") if prune is not None else _C.LOCAL.rstrip("/"), "prune": prune, "since": since}
        if self.ix.get("cfg") != cfg:
            if self.job:                              # 换扫法时正在扫的那件作废（线程跑完自己结束，结果不要）
                self.job = None
            self.ix = {"cfg": cfg}
        self.cfg, self.root = cfg, cfg["root"]

    def lease(self, now):
        return self.cfg is not None

    def splits(self, split):
        """labmon 目录拆到各项目一级：LOCAL 与它在根下的各级上层、cache、jobs（不拆的话 /tmp/labmon 整个是一个单位）。"""
        L, top, out = _C.LOCAL.rstrip("/"), self.root.rstrip("/"), set(split)
        for d in (L, os.path.join(L, "cache"), os.path.join(L, "jobs")):
            while d != self.root and d.startswith(top + "/"):
                out.add(d)
                d = os.path.dirname(d)
        return out

    def lister(self, split, pace):
        c = self.cfg
        skip = {os.path.join(c["root"], x) for x in LOCAL_SKIP} if c["prune"] is not None else ()
        return scan_units(c["root"], self.splits(split), pace, c["prune"], skip)

    def scanner(self, u, now, pace):
        return scan_unit(u, now, pace, self.cfg["prune"], self.cfg["since"])

    def own(self, u):
        """这个单位是 labmon 自己的（LOCAL 下）。"""
        L = _C.LOCAL.rstrip("/")
        return u == L or u.startswith(L + "/")

    def acct_of(self):
        """对账看 labmon 之外的新写入：根盘已用 − labmon 目录的占用（只扫 labmon 目录时不对账）。"""
        used = (self.ix.get("df") or {}).get("used")
        if self.cfg["prune"] is None or used is None:
            return None
        return used - sum(r.get("b") or 0 for u, r in self.ix["units"].items() if self.own(u))

    def summary(self):
        """写进状态 storage 的：local 各项目的缓存与单格临时、own labmon 目录的总占用（字节）、wrote labmon 之外的新写入（只扫 LOCAL 时为 None）。
        wrote：since 起算的时刻、b 字节、n 文件数、top 前 LOCAL_TOP 个目录 [路径, 字节, 最近写入]（单位的直接子项；本层的文件写成 <目录>/.；
        子项不止 10 个的，其余合在单位自己的路径上）、more [其余几处, 字节]、pending 还没扫的单位数、t 最近一次扫完一个单位的时刻。"""
        ix, L = self.ix or {}, _C.LOCAL.rstrip("/")
        units = ix.get("units") or {}
        local, own, rows, pend, b, n = {}, 0, [], 0, 0, 0
        for u, r in units.items():
            if r.get("b") is None:
                pend += 1
                continue
            if self.own(u):
                own += r["b"]
                rel = os.path.relpath(u, L).split("/")             # 扫得太慢拆开的（cache/<项目>/models 等更深的单位）按前两段归到所属项目
                if len(rel) >= 2 and rel[0] in ("cache", "jobs") and rel[1] != ".":
                    local.setdefault(rel[1], {"cache": 0, "scratch": 0})["cache" if rel[0] == "cache" else "scratch"] += r["b"]
                continue
            b, n = b + r["b"], n + r["n"]
            if u.endswith("/."):
                rows.append([u, r["b"], r.get("m")])
                continue
            tops = [x for x in r.get("top") or [] if x[1]]
            rows += [[u.rstrip("/") + "/" + x[0], x[1], x[2] if len(x) > 2 else None] for x in tops]
            rest = r["b"] - sum(x[1] for x in tops)
            if rest > 0:
                rows.append([u, rest, r.get("m")])
        rows = sorted((x for x in rows if x[1] > 0), key=lambda x: -x[1])
        cfg = ix.get("cfg") or {}
        wrote = {"since": cfg.get("since"), "b": b, "n": n, "top": rows[:LOCAL_TOP], "more": [len(rows[LOCAL_TOP:]), sum(x[1] for x in rows[LOCAL_TOP:])],
                 "pending": pend, "t": max((r.get("t") or 0 for r in units.values()), default=0) or None} if cfg.get("prune") is not None else None
        return {"t": ix.get("t"), "local": local, "own": own if units else None, "wrote": wrote}


class WorkDisk(Disk):
    """独立主机隔离工作盘（<data_root>）的限速轮流扫描（#114）：直接复用 Disk 与 Pace(LOCAL_IOPS)，
    仅扫 <data_root> 一棵隔离目录树（projects/、datasets/、weights/、envs/、labmon/、.tmp/ 等），绝不遍历系统根盘 /；
    scan_units/scan_unit 强制 follow_symlinks=False，本机 Mac 在 datasets/ 建软链接指向外部数据时不重复计容、绝不越界扫描个人目录。
    索引本地化存于 <HOME>/disk/index.json（无需跨机扫描租约），仅将精简汇总 storage.work 写入 status.json 带回看板。"""
    key = "wscan"
    iops = LOCAL_IOPS
    slack = LOCAL_DRIFT

    def __init__(self, host, root=None):
        Disk.__init__(self, (root or _C.DATA_ROOT).rstrip("/"), host)
        self.dir = os.path.join(_C.HOME, "disk")
        self.ixf = os.path.join(self.dir, "index.json")
        self.mine = True

    def lease(self, now):
        if self.ix is None:
            self.load()
        return True

    def splits(self, split):
        """把 projects/<项目>（经 want/forced）与 .tmp/cache、.tmp/jobs 拆开，按项目与公共目录汇总。"""
        out = set(split)
        L, top = _C.LOCAL.rstrip("/"), self.root.rstrip("/")
        for d in (L, os.path.join(L, "cache"), os.path.join(L, "jobs")):
            cur = d
            while cur != top and cur.startswith(top + "/"):
                out.add(cur)
                cur = os.path.dirname(cur)
        return out

    def lister(self, split, pace):
        return scan_units(self.root, self.splits(split), pace)

    def scanner(self, u, now, pace):
        return scan_unit(u, now, pace)

    def total_bytes(self):
        units = (self.ix or {}).get("units") or {}
        vals = [r["b"] for r in units.values() if r.get("b") is not None]
        return sum(vals) if vals else None

    def summary(self, proj_map=None):
        """返回 (work, local)：
        work 写入 status.json 的 storage.work（root、b、t、pending、projects {pid: {b, m, d24, top}}、common {envs, datasets, weights, labmon, archive, tmp, other}）；
        local 写入 status.json 的 storage.local（{pid: {cache, scratch}}，与集群 LocalDisk 同口径）。
        proj_map：{项目目录路径: 登记项目 id}（来自队列登记，Worker.scan_local 传入）；优先按已登记目录映射归属项目 ID，
        避免目录名与 --project 不一致时归到虚构项目或误标可回收。"""
        ix = self.ix or {}
        units = ix.get("units") or {}
        base = self.root.rstrip("/")
        L = _C.LOCAL.rstrip("/")
        pmap = sorted(
            ((os.path.normpath(k).rstrip("/"), v) for k, v in (proj_map or {}).items() if k and v),
            key=lambda kv: -len(kv[0]),
        )

        def _mapped_pid(u_path):
            un = os.path.normpath(u_path).rstrip("/")
            if un.endswith("/."):
                un = un[:-2] or "/"
            for root_p, p_id in pmap:
                if un == root_p or un.startswith(root_p + "/"):
                    return p_id, root_p
            # 若整个 projects/<dir> 尚未拆分子目录而登记根在其下，也归属到该登记项目
            hits = [p_id for root_p, p_id in pmap if root_p.startswith(un + "/")]
            if len(set(hits)) == 1:
                return hits[0], un
            return None, None

        local, projs, common = {}, {}, {"envs": 0, "datasets": 0, "weights": 0, "labmon": 0, "archive": 0, "tmp": 0, "other": 0}
        total_b, pend = 0, 0
        for u, r in units.items():
            if r.get("b") is None:
                pend += 1
                continue
            b = r["b"]
            m = r.get("m")
            total_b += b
            if u == L or u.startswith(L + "/"):
                common["tmp"] += b
                rel_l = os.path.relpath(u, L).split("/")
                if len(rel_l) >= 2 and rel_l[0] in ("cache", "jobs") and rel_l[1] != ".":
                    ent = local.setdefault(rel_l[1], {"cache": 0, "scratch": 0, "m": None})
                    ent["cache" if rel_l[0] == "cache" else "scratch"] += b
                    if m and (not ent["m"] or m > ent["m"]):
                        ent["m"] = m
                continue
            rel = os.path.relpath(u, base) if u.startswith(base + "/") else u
            parts = [x for x in rel.split("/") if x]
            if not parts or parts == ["."]:
                common["other"] += b
                continue
            mpid, mroot = _mapped_pid(u)
            top_dir = parts[0]
            if mpid is not None or (top_dir == "projects" and len(parts) >= 2 and parts[1] != "."):
                pid = mpid if mpid is not None else parts[1]
                pe = projs.setdefault(pid, {"b": 0, "m": None, "d24": 0, "top": [], "dir": None})
                real_root = mroot if mroot is not None else os.path.join(base, "projects", parts[1])
                real_rel = os.path.relpath(real_root, base) if real_root.startswith(base + "/") else real_root
                if not pe["dir"] or len(real_rel) < len(pe["dir"]):
                    pe["dir"] = real_rel
                pe["b"] += b
                if m and (not pe["m"] or m > pe["m"]):
                    pe["m"] = m
                if r.get("pb") is not None and r.get("pt") and r.get("t") and r["t"] - r["pt"] <= 86400 * 2:
                    pe["d24"] += b - r["pb"]
                if mroot is not None:
                    un = os.path.normpath(u).rstrip("/")
                    sub = os.path.relpath(un, mroot) if un != mroot else "."
                else:
                    sub = "/".join(parts[2:]) if len(parts) > 2 else "."
                pe["top"].append([sub, b, m])
            elif top_dir in ("envs", "datasets", "weights", "labmon", "archive"):
                common[top_dir] += b
            else:
                common["other"] += b
        for pid, pe in projs.items():
            pe["top"] = sorted((x for x in pe["top"] if x[1] > 0), key=lambda x: -x[1])[:8]
        work = {"root": base, "b": total_b if units else None, "t": ix.get("t"), "pending": pend,
                "projects": projs, "common": common}
        loc_out = {pid: {"cache": v["cache"], "scratch": v["scratch"], **({"m": v["m"]} if v.get("m") else {})} for pid, v in local.items()}
        return work, loc_out
