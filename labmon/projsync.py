"""项目目录里的 monitor.toml（工作副本）与看板上登记的那份同步（#161；看板上那份为准，登记见 projects.py）。

    labmon project push [-c monitor.toml]   推上去（第一次推就是登记）；看板上在上次同步之后改过的拒绝（412），先 pull
    labmon project pull [-c monitor.toml]   拉下来覆盖本地；本地有没同步的改动时先存成 .labmon/<配置文件名>.mine（monitor.toml.mine）
    labmon wait 每轮 sync：只本地改了推、只看板上改了（页面上改的）拉、两边都改了不动只报一条给实验会话

同步记录在 monitor.toml 旁边的 .labmon/<配置文件名>.sync.json（{id, ver 上次同步时的版本, t}；同一目录里可以有几个项目的配置，各记各的、不共用一个文件，两个 wait 同时写也不互相冲掉）；.labmon/ 自带只写 * 的 .gitignore，项目仓库不多出要提交的东西。
本地改没改 = 本地文本的版本与 ver 比，看板上改没改 = 看板上的版本与 ver 比（版本号是内容哈希，projects.ver_of）。
done_when 指的文件只从本地往看板上走（页面不改它）：与看板上的不一样就随推上传。
取、推只经 _get、_put 两个函数，经看板接口（client.call 的 GET/PUT /api/project/<id>/config，#162）：在哪台电脑上跑都一样。
本地这份只做 TOML 解析（config.brief：id 与 done_when 的文件），整套校验在看板那边（要用户级主机清单），推上去不过回 400（projects.Refused）。
"""
from __future__ import annotations

import time
from pathlib import Path

from urllib.parse import quote

from . import client
from . import projects as PJ
from .config import ConfigError, brief, done_key
from .state import load_json, save_json, write_atomic


def _refused(e: client.Failed) -> PJ.Refused:
    return PJ.Refused(e.code, e.msg, e.data.get("ver"))


def _get(pid: str, patience: float = client.PATIENCE) -> dict | None:
    """看板上的配置（projects.get_config 的样子）；没登记返回 None。看板回了别的错抛 projects.Refused，连不上抛 client.Unreachable。"""
    try:
        return client.call("GET", f"/api/project/{quote(pid, safe='')}/config", patience=patience)
    except client.Failed as e:
        if e.code == 404:
            return None
        raise _refused(e) from e


def _put(pid: str, text: str, files: dict, base: str | None, patience: float = client.PATIENCE, admin: bool = False) -> dict:
    """推上去（PUT，If-Match 带上次同步的版本）：看板回了错抛 projects.Refused（400 配置不对、412 看板上改过……）。
    admin：用管理员令牌（client.token(admin=True)：admin.token 优先；labmon project push --admin，改 [[hosts]]、[[queues]] 要）。"""
    try:
        return client.call("PUT", f"/api/project/{quote(pid, safe='')}/config", {"text": text, "files": files},
                           headers={"If-Match": f'"{base}"'} if base else None, patience=patience, admin=admin)
    except client.Failed as e:
        raise _refused(e) from e


def _sf(cfg_path: str | Path) -> Path:
    """这个配置文件的同步记录：.labmon/<配置文件名>.sync.json。"""
    p = Path(cfg_path).expanduser().resolve()
    return p.parent / ".labmon" / f"{p.name}.sync.json"


def _rec(cfg_path: str | Path) -> dict:
    """这个配置文件的同步记录 {id, ver, t}（没有是空的）。"""
    r = load_json(_sf(cfg_path), {})
    return r if isinstance(r, dict) else {}


def _mine(cfg_path: str | Path) -> Path:
    """pull 时本地没同步的改动存在哪：.labmon/<配置文件名>.mine。"""
    p = Path(cfg_path).expanduser().resolve()
    return p.parent / ".labmon" / f"{p.name}.mine"


def mark(cfg_path: str | Path, pid: str, ver: str) -> None:
    """记下这次同步到的版本（推成了、拉成了、两边本来就一样）：整个覆盖这个配置文件自己的记录，同目录别的配置的不碰。"""
    f = _sf(cfg_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    gi = f.parent / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    save_json(f, {"id": pid, "ver": ver, "t": round(time.time())})


def base_of(cfg_path: str | Path, pid: str) -> str | None:
    r = _rec(cfg_path)
    return r.get("ver") if r.get("id") == pid else None


def _local(cfg: dict) -> dict:
    """本地 done_when 用到的文件 {monitor.toml 里写的路径: 内容（没有为 None）}。cfg 是 config.brief 读的（done 是 {写的路径: 本地路径}）。"""
    return {src: (Path(f).read_text() if Path(f).is_file() else None) for src, f in cfg["done"].items()}


def _load(p: Path, text: str) -> dict:
    """读本地这份（status、push、pull 共用）：只做 TOML 解析（config.brief），语法不对抛 ConfigError（wait 第一轮、命令行退出 3）。"""
    return brief(p, text)


def _files_differ(local: dict, server: dict) -> bool:
    return {done_key(s): PJ.ver_of(t) for s, t in local.items() if t is not None} != server


def status(cfg_path: str | Path, patience: float = client.PATIENCE) -> dict:
    """{state, id, local, server, base}。state：same 一样、files 只有 done_when 的文件要推、push 只本地改了、pull 只看板上改了、
    conflict 两边都改了（或项目目录里没有同步记录、两边又不一样）、unregistered 看板上没登记、archived 已归档。读不了本地配置抛 ConfigError。"""
    p = Path(cfg_path).expanduser().resolve()
    text = p.read_text() if p.is_file() else None
    if text is None:
        raise ConfigError(f"找不到配置文件 {p}")
    cfg = _load(p, text)
    pid, lv = cfg["id"], PJ.ver_of(text)
    srv, base = _get(pid, patience), base_of(p, pid)
    r = {"id": pid, "local": lv, "server": srv and srv["ver"], "base": base, "cfg": cfg, "text": text, "srv": srv}
    if srv is None:
        return dict(r, state="unregistered")
    if srv["archived"]:
        return dict(r, state="archived")
    sv = srv["ver"]
    if lv == sv:
        st = "files" if _files_differ(_local(cfg), srv["files"]) else "same"
    elif lv == base:
        st = "pull"
    elif sv == base:
        st = "push"
    else:
        st = "conflict"
    return dict(r, state=st)


def push(cfg_path: str | Path, admin: bool = False) -> str:
    """推上去（labmon project push；--admin 用管理员令牌）：第一次推就是登记；看板上在上次同步之后改过的抛 projects.Refused（412），没有同步记录的已登记项目 428。返回一句说明。"""
    p = Path(cfg_path).expanduser().resolve()
    text = p.read_text() if p.is_file() else None
    if text is None:
        raise ConfigError(f"找不到配置文件 {p}")
    cfg = _load(p, text)
    pid = cfg["id"]
    r = _put(pid, text, _local(cfg), base_of(p, pid), admin=admin)
    mark(p, pid, r["ver"])
    if r["created"]:
        if r["archived"]:
            return f"登记了项目 {pid}：它已归档，看板不采；要接着用 labmon project restore {pid}"
        return f"登记了项目 {pid}：看板服务最迟下一轮全局采集时开始采它"
    return f"推上去了 {pid}：" + ("、".join(r["changed"]) if r["changed"] else "没有改动")


def pull(cfg_path: str | Path) -> str:
    """拉下来覆盖本地（labmon project pull）。本地有没同步的改动（与上次同步的版本不同）时先把本地那份存成 .labmon/<配置文件名>.mine。返回一句说明。"""
    p = Path(cfg_path).expanduser().resolve()
    text = p.read_text() if p.is_file() else None
    try:
        pid = _load(p, text)["id"] if text is not None else None
    except ConfigError:
        pid = None
    pid = pid or _rec(p).get("id")
    if not pid:
        raise ConfigError(f"{p} 读不出项目 id，也没有同步记录：说不出拉哪个项目")
    srv = _get(pid)
    if srv is None:
        raise ConfigError(f"看板上没有项目 {pid}：labmon project push 登记")
    text = p.read_text() if p.is_file() else None       # 取看板上那份时本地可能又存了：按覆盖前这一刻的定要不要留 .mine
    if text == srv["text"]:
        mark(p, pid, srv["ver"])
        return f"{pid} 本地与看板上一样，不用拉"
    kept = ""
    if text is not None and PJ.ver_of(text) != base_of(p, pid):
        mine = _mine(p)
        write_atomic(mine, text)
        kept = f"；本地没同步的改动存到了 {mine}"
    write_atomic(p, srv["text"])
    mark(p, pid, srv["ver"])
    return f"拉下了看板上 {pid} 的配置，覆盖了 {p}{kept}"


def sync(cfg_path: str | Path, patience: float = client.PATIENCE) -> dict:
    """labmon wait 每轮的同步：{act, id, msg, key}。act：same 没事、push 推了、pull 拉了、conflict 两边都改了（不动）、error 推不上（code 是看板回的状态码，400 是配置不对）、
    unregistered 看板上没登记、archived 已归档（wait 自己报）。key 是冲突与推不上那条告警的键（同一对版本只报一次）。
    patience：连不上看板时重试多久（client.call；wait 传 0，由它自己退避、按门槛报 server_unreachable）。"""
    st = status(cfg_path, patience)
    pid, s = st["id"], st["state"]
    out = {"act": s, "id": pid, "msg": None, "key": None}
    if s in ("unregistered", "archived"):
        out["msg"] = f"看板上没有项目 {pid}：在项目目录里 labmon project push -c {cfg_path} 登记" if s == "unregistered" else None
        return out
    if s == "same":
        if st["base"] != st["server"]:
            mark(cfg_path, pid, st["server"])
        return out
    if s == "pull":
        p = Path(cfg_path).expanduser().resolve()
        if (p.read_text() if p.is_file() else None) != st["text"]:     # 比过之后本地又存了一版：照它重比，不覆盖
            return sync(cfg_path, patience)
        write_atomic(p, st["srv"]["text"])
        mark(cfg_path, pid, st["server"])
        return dict(out, msg=f"看板上改了 {pid} 的 monitor.toml（页面上改的），已拉回 {cfg_path}：随项目一起提交，别当成误改回滚")
    if s == "conflict":
        return dict(out, key=f"sync_conflict:{st['local']}:{st['server']}",
                    msg=f"看板上的 {pid} 配置在上次同步之后改过（多半是页面上改的），项目目录里的 {cfg_path} 也改过，两边都没动："
                        f"labmon project pull -c {cfg_path} 拉回来（本地这份存到 .labmon/{Path(cfg_path).name}.mine），把要的改动再改一遍，wait 会推上去")
    try:
        r = _put(pid, st["text"], _local(st["cfg"]), st["server"] if s == "files" else st["base"], patience)
    except PJ.Refused as e:
        return dict(out, act="error", code=e.code, key=f"sync_error:{st['local']}:{PJ.ver_of(e.msg)}", msg=f"{cfg_path} 推不上看板：{e.msg}")
    mark(cfg_path, pid, r["ver"])
    return dict(out, act="push", msg=f"推上去了 {pid}：" + ("、".join(r["changed"]) or "没有改动"))


def say(st: dict, cfg_path: str | Path) -> str:
    """labmon check 那一行：与看板上那份的同步状态与该怎么办。"""
    pid = st["id"]
    return {"same": f"与看板上的 {pid} 配置一样",
            "files": f"配置与看板上一样，done_when 的文件有改动：labmon wait 会推上去（或 labmon project push -c {cfg_path}）",
            "push": f"本地改了、看板上没改：labmon wait 会推上去（或 labmon project push -c {cfg_path}）",
            "pull": f"看板上改过（多半是页面上改的）、本地没改：labmon wait 会拉回来（或 labmon project pull -c {cfg_path}）",
            "conflict": f"看板上与本地都改过：labmon project pull -c {cfg_path} 拉回来（本地这份存到 .labmon/{Path(cfg_path).name}.mine），再把要的改动改一遍",
            "unregistered": f"看板上还没有项目 {pid}：labmon project push -c {cfg_path} 登记",
            "archived": f"项目 {pid} 已归档：要接着用先 labmon project restore {pid}"}[st["state"]]
