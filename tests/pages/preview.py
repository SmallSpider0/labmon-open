"""改页面时的离线预览：用页面测试的快照（snapshots.py）经 collect.write_page 渲染两页到临时目录（退出时删掉），起一个只读的静态服务看。

    python3.11 tests/pages/preview.py [--port 8799] [--kind normal|evil|sparse|dense|many|overdue] [--as admin|public|user|empty] [--prefix /pfx]

不 ssh、不连平台、不发通知，不碰 8765 上的看板与 ~/.local/state/labmon（状态目录指到临时目录）。
目录布局照看板服务：/ 是工作台，/p/<项目>/ 是项目页，页面定时取的 /overview.json、/records.json、
/p/<项目>/snapshot.json、events.json 都放了同一份快照，取得到。没放的只有：
- 组内服务器详情 /fleet/<id>.json、/fleet/person/<名字>.json、/fleet/plan.json、/fleet/op.json：GET 返回 404，抽屉里的曲线与人员详情不出；
- 所有 POST（立即采集、续租、平台操作、改权重、登录、账号的操作）：返回 501，页面上提示“立即采集失败”之类。
浏览器控制台里只会有这几类 404 / 501，别的报错都是页面的问题。
看板账号（#160）：--as 定“我是谁”（admin 从内网入口来的管理员、public 从公网入口来的管理员、user 普通用户），在临时状态目录里建几个假账号、
会话与令牌，页面上的 ME、/api/me、/api/users（管理员）与普通用户看到的快照（users.view）都照看板服务的同一套函数出；/login 是登录页。
--prefix /pfx：照 TX 的 nginx 把整套挂在 /pfx/ 下（#165），看页面上的请求与链接是不是都留在前缀里（出了前缀的在控制台是 404）。
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import signal
import sys
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# 快照函数按名字取：labmon 要等 main 把 LABMON_STATE 指到临时目录后才导入（导入本模块不改环境、不建目录，测试也能直接调 build）
KINDS = {"normal": ("overview", "project", "records", "project_events"),
         "evil": ("overview_evil", "project_evil", "records_evil", "project_events_evil"),
         "sparse": ("overview_sparse", "project_sparse", "records", "project_events"),
         "dense": ("overview_dense", "project_dense", "records", "project_events"),
         "many": ("overview_dense", "project_many", "records", "project_events"),
         "overdue": ("overview", "project_overdue", "records", "project_events_overdue")}


AS = {"admin": ("boss", "lan"), "public": ("boss", "public"), "user": ("amy", "lan"), "empty": ("carol", "lan")}     # --as：用哪个假账号、从哪个入口来


def accounts(who: str):
    """在当前 LABMON_STATE（main 已指到临时目录）里建几个假账号、会话与令牌，返回 (who 的 Ident, 入口)。口令哈希的轮数调低，只为快。"""
    from labmon import users as U
    it, U.ITER = U.ITER, 1000
    try:
        return _accounts(U, who)
    finally:
        U.ITER = it


def _accounts(U, who: str):
    ua = {"mac": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
          "phone": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
          "win": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
    from labmon import fleetboot as FB
    raw = {}
    for name, role, pw in (("boss", "admin", True), ("amy", "user", True), ("bob", "user", True), ("carol", "user", who == "empty")):
        person = {"amy": "OWNER", "bob": "PERSON3"}.get(name, name)
        FB.set_person(person, create=True, roster_only=True)
        code = U.add(name, role, "本机命令行" if name == "boss" else "boss", person=person)
        if pw:
            raw[name] = U.setup(name, code, name + " 的假口令", "lan", ua["mac"])[0]
    raw["public"] = U.login("boss", "boss 的假口令", "public", ua["phone"])[0]
    U.login("amy", "amy 的假口令", "lan", ua["win"])
    U.token_add("boss", "部署脚本", ["deploy"])
    U.token_add("amy", "读数据的脚本", ["read"])
    for _ in range(U.FAIL_MAX):                      # bob 连着输错，锁着
        try:
            U.login("bob", "错的", "lan")
        except U.Denied:
            pass
    from labmon import projects as PJ
    from labmon.state import save_json
    for pid, owner in (("proj-a", "amy"), ("proj-b", "bob"), ("proj-old", "amy")):
        f = PJ.cfg_file(pid)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f'[project]\nid = "{pid}"\n')
        save_json(f.parent / "meta.json", {"owner": owner})
    name, entry = AS[who]
    return U.ident({"Cookie": f"{U.COOKIE}={raw['public' if who == 'public' else name]}"}), entry


def build(out: Path, kind: str = "normal", who=None) -> list[str]:
    """渲染到 out，返回项目 id。who：(Ident, 入口)，给了就照看板服务填 ME、给普通用户换成 users.view 的快照，另写 /api/me、/api/users 与登录页。"""
    from labmon import collect as C
    from pages import snapshots as S
    ov, pj, rec, ev = (getattr(S, n) for n in KINDS[kind])
    snap = ov()
    out.mkdir(parents=True, exist_ok=True)
    me = None
    if who:
        from labmon import fleet as F, users as U
        ident, entry = who
        from unittest.mock import patch
        hosts = [{"id": "srv-a", "label": "主机 A", "users": ["amy"]}, {"id": "srv-b", "label": "主机 B"},
                 {"id": "srv-old2", "label": "旧主机", "returned": True}]
        with patch.object(U, "user_hosts", return_value=hosts):
            acc = U.access(ident, snap) if ident.role != "admin" else None
            snap, me = U.view(ident, snap, acc), U.me(ident, entry)
            listing = U.listing(directory=True) if ident.role == "admin" else None
        me.update(commit="a1b2c3d000000000000000000000000000000000",
                  push={"set": True, "kind": "bark", "target": "Bark · abc…xyz"} if ident.name == "amy" else {"set": False})
        (out / "api").mkdir(exist_ok=True)
        (out / "api" / "me.json").write_text(json.dumps(me, ensure_ascii=False))
        if listing:
            people = (snap.get("fleet") or {}).get("ops", {}).get("people", {})
            registered = {**F.registered_people(), **people}
            with patch.object(F, "registered_people", return_value=registered):
                listing["person_choices"] = U.free_people({u["name"]: {"fleet_person": u["fleet_person"]} for u in listing["users"]})
                listing["people"] = sorted(registered)
            (out / "api" / "users.json").write_text(json.dumps(listing, ensure_ascii=False))
        (out / "login.html").write_text(C.render_page("login.html", {"empty": False, "entry": entry, "entry_name": U.ENTRIES[entry], "next": "",
                                                                     "pw_min": U.PW_MIN, "lock_min": U.LOCK_S // 60, "fails": U.FAIL_MAX}))
    if not who or ident.role == "admin":
        from labmon import fleet as F, fleetview as FV
        from labmon.platform import NAME_RE
        fleet = snap.get("fleet") or {}
        people = set(fleet.get("ops", {}).get("people", {})) | {h["holder"] for h in fleet.get("holders", [])}
        if people:
            details = out / "fleet" / "person"
            details.mkdir(parents=True, exist_ok=True)
            for n in people:
                if NAME_RE.fullmatch(n):
                    (details / f"{n}.json").write_text(json.dumps(FV.person(n, t=snap["generated"]), ensure_ascii=False))
    fill = (lambda f: f.write_text(C.fill(f.read_text(), "ME", me))) if me else (lambda f: None)
    C.write_page("overview.html", snap, out / "index.html", title="调度总览")
    fill(out / "index.html")
    (out / "overview.json").write_text(json.dumps(snap, ensure_ascii=False))
    from labmon.records import cat_of
    rows = rec()                                    # 形状照 serve 的 /records.json：{rows, more, cats}（不按查询串筛，各分类的条数照实给）
    if who and (see := U.visible(ident, acc)):
        rows = [r for r in rows if see(r)]
    cats: dict = {}
    for r in rows:
        cats[cat_of(r.get("kind"))] = cats.get(cat_of(r.get("kind")), 0) + 1
    (out / "records.json").write_text(json.dumps({"rows": rows, "more": False, "cats": cats}, ensure_ascii=False))
    ids = []
    for p in snap.get("projects") or []:
        d = out / "p" / p["id"]
        d.mkdir(parents=True, exist_ok=True)
        ps = {**pj(), "id": p["id"], "title": p.get("title") or p["id"]}
        C.write_page("page.html", ps, d / "index.html", title=ps["title"])
        fill(d / "index.html")
        (d / "snapshot.json").write_text(json.dumps(ps, ensure_ascii=False))
        (d / "events.json").write_text(json.dumps(ev(), ensure_ascii=False))
        ids.append(p["id"])
    return ids


class Handler(SimpleHTTPRequestHandler):
    """静态目录，另把看板服务的几个无扩展名的路由对到文件上（/login、/api/me、/api/users）。"""
    ALIAS = {"/login": "/login.html", "/api/me": "/api/me.json", "/api/users": "/api/users.json"}
    prefix = ""                                     # --prefix：上面几条也挂在前缀下

    def translate_path(self, path):
        p = urlsplit(path).path
        al = {self.prefix + k: self.prefix + v for k, v in self.ALIAS.items()}
        return super().translate_path(al.get(p, path))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--kind", choices=KINDS, default="normal", help="快照：正常、恶意字符串、字段大量缺省、高并行短任务、大量小规模并行（项目页的紧凑视图）、借来的主机过了约定时间")
    ap.add_argument("--as", dest="who", choices=AS, default="admin", help="我是谁：内网管理员、公网管理员、普通用户、刚加入的普通用户")
    ap.add_argument("--prefix", default="", help="照反代挂在这个路径前缀下（如 /pfx，#165）")
    a = ap.parse_args()
    pfx = "/" + a.prefix.strip("/") if a.prefix.strip("/") else ""
    if a.port == 8765:
        sys.exit("8765 是看板服务的端口，换一个")
    for s in (signal.SIGTERM, signal.SIGHUP):     # 被 kill、关掉终端也走 finally（kill -9 接不住）
        signal.signal(s, lambda *_: sys.exit(0))
    tmp = tempfile.mkdtemp(prefix="labmon-preview-")
    os.environ.update(LABMON_NO_NOTIFY="1", LABMON_NO_KEYCHAIN="1", LABMON_LAN="off",
                      LABMON_STATE=str(Path(tmp, "state")), LABMON_HOSTS=str(ROOT / "tests" / "_no_user_hosts.toml"))
    try:     # 退出（Ctrl-C、端口被占、渲染出错）都删掉临时目录
        top = Path(tmp, "site")
        out = top / pfx.lstrip("/") if pfx else top
        ids = build(out, a.kind, accounts(a.who))
        hosts = [h["id"] for h in json.loads((out / "overview.json").read_text()).get("hosts") or []]
        try:
            Handler.prefix = pfx
            srv = ThreadingHTTPServer(("127.0.0.1", a.port), functools.partial(Handler, directory=str(top)))
        except OSError as e:
            sys.exit(f"端口 {a.port} 用不了（{e.strerror or e}），换一个 --port")
        print(f"预览：http://127.0.0.1:{a.port}{pfx}/  （目录 {out}，退出时删除）")
        print(f"路由：#/  #/host/{hosts[0] if hosts else '<id>'}  #/p/{ids[0] if ids else '<id>'}  #/fleet  #/me  #/fleet/people  /login（--as {a.who}）", flush=True)
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
