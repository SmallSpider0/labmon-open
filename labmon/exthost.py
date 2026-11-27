"""外部主机（外租 Linux / WSL2 / 本机 Mac 等独立主机）的配置、密钥与连通性管理（唯一实现，#114）。

REST API（/api/hosts/external*）、CLI（labmon host add/edit/test/rm/key）与工作台右侧抽屉共用本模块：
- 统一落盘 ~/.config/labmon/hosts.toml（过 HOSTS_LOCK + lease.need）；
- 零私钥上传路线：看板按 (owner, host_id) 在 LABMON_SECRETS/ssh_keys/ 下生成专用 Ed25519 密钥对，
  接口仅回显公钥、指纹与一行 authorized_keys 授权命令，绝不接受或回显私钥；
- 多用户安全边界：
  * 普通用户仅可自助增删改查本人名下私有外部机（上限 users.USER_EXT_HOSTS = 3 台），强制 standalone=true、
    share="private"、ssh_key="managed"、owner=who.name、users=[who.name]，对其他普通用户完全不可见；
  * 严禁普通用户使用 ssh=""（本机直连）、ssh_key="system"（宿主机默认密钥）、回环/链路本地/未指定/组播地址，
    以及与现有 SCM 主机、组内服务器或本机看板入口重叠的 (host, port)；
  * 删机同步检查在跑/排队任务（非 force 拒绝）、销毁专用私钥、断开 ControlMaster 与清理独立 known_hosts。
"""
from __future__ import annotations

import ipaddress
import json
import re
import shlex
import socket
import subprocess

from . import fleet as F
from . import lan
from . import lease as LS
from . import platform as PF
from . import users as U
from .config import (
    DISK_STOP_GB, DISK_WARN_GB, HOSTS_LOCK, LABEL_MAX, PLATFORM_KINDS,
    STANDALONE_DATA_ROOT, ConfigError, add_host, edit_host, fmt_time,
    host_defaults, host_platform, host_ssh_key, parse_time, remove_host,
    server as server_conf, shell_path, user_hosts,
)
from .state import snapshot

HOST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED_IDS = frozenset({"pool", "new", "all", "local-reserved"})
SSH_TARGET_RE = re.compile(r"^(?:([A-Za-z0-9._][A-Za-z0-9._-]{0,63})@)?([A-Za-z0-9._][A-Za-z0-9._-]{0,252}|\[[0-9A-Fa-f:.]+\])$")
PYTHON_RE = re.compile(r"^[A-Za-z0-9._/~@-]+$")
FORBIDDEN_KEY_FIELDS = frozenset({"private_key", "ssh_private_key", "identity_file", "key_pem", "pem", "key_content", "key_file"})
EXT_PLATFORM_KINDS = tuple(k for k in PLATFORM_KINDS if k != "scm")
TEST_TIMEOUT_S = 15

# 只读连通性探测脚本：返回一行 JSON（os、is_wsl、py、gpus、data_root、free_gb、writable）
_TEST_PY = (
    "import json,os,platform,shutil,subprocess,sys;"
    "dr=os.path.expanduser(sys.argv[1]);"
    "os.makedirs(dr,exist_ok=True);"
    "st=os.statvfs(dr);"
    "free_gb=round(st.f_bavail*st.f_frsize/(1024**3),1);"
    "wr=os.access(dr,os.W_OK);"
    "rel=platform.release().lower();"
    "is_wsl=('microsoft' in rel or 'wsl' in rel);"
    "gpus=[];"
    "nvs=shutil.which('nvidia-smi');"
    "\ntry:\n"
    " if nvs:\n"
    "  out=subprocess.check_output([nvs,'--query-gpu=name,memory.total','--format=csv,noheader'],text=True,timeout=6)\n"
    "  gpus=[x.strip() for x in out.splitlines() if x.strip()]\n"
    " elif sys.platform=='darwin' and platform.machine()=='arm64':\n"
    "  gpus=['Apple Silicon (MPS)']\n"
    "except Exception:\n pass\n"
    "print(json.dumps({'os':platform.system(),'release':platform.release(),'arch':platform.machine(),"
    "'is_wsl':is_wsl,'py':'.'.join(map(str,sys.version_info[:3])),'gpus':gpus,'data_root':dr,'free_gb':free_gb,'writable':wr}))"
)


def _reject_private_key_upload(data: dict) -> None:
    """零私钥上传红线：拒绝请求体携带任何私钥内容或宿主机私钥路径字段。"""
    hit = sorted(k for k in data if k in FORBIDDEN_KEY_FIELDS and data.get(k) not in (None, "", False))
    if hit:
        raise U.Denied(400, f"仅支持零私钥上传（看板自动生成专用公钥），拒绝字段：{'、'.join(hit)}")
    km = data.get("ssh_key") or data.get("key_mode")
    if isinstance(km, str) and km.strip().lower() == "uploaded":
        raise U.Denied(400, "看板只走零私钥上传路线，不支持上传私钥（请使用 managed 自动生成专用公钥）")


def _clean_host(host: str) -> str:
    return host.strip().strip("[]").lower().rstrip(".")


def parse_ssh_target(ssh: str) -> tuple[str | None, str]:
    """解析 [user@]host，拒绝前导 -（防 SSH 选项注入）、空白与控制符；返回 (user, clean_host)。"""
    s = str(ssh or "").strip()
    m = SSH_TARGET_RE.fullmatch(s)
    if not m:
        raise U.Denied(400, f"SSH 地址格式不对：{ssh!r}（写法如 user@203.0.113.10 或 user@gpu.example.com，不能含空格或以 - 开头）")
    user, host = m.group(1), m.group(2)
    clean = _clean_host(host)
    if not clean or clean.startswith("-"):
        raise U.Denied(400, f"SSH 主机名不合法：{ssh!r}")
    return user, clean


def _bad_ip(addr: str) -> str | None:
    """检查 IP 是否属于禁止普通用户探测的回环/链路本地/未指定/组播段；安全返回 None，否则返回原因。"""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if ip.is_loopback:
        return f"禁止指向回环地址（{addr}）"
    if ip.is_link_local:
        return f"禁止指向链路本地地址（{addr}）"
    if ip.is_unspecified:
        return f"禁止指向未指定地址（{addr}）"
    if ip.is_multicast:
        return f"禁止指向组播地址（{addr}）"
    return None


def _existing_endpoints(exclude_id: str | None = None) -> set[tuple[str, int]]:
    """已有 [[hosts]] 与 [[fleet.servers]] 占用的 (clean_host, port) 集合（用于防撞机接管）。"""
    out: set[tuple[str, int]] = set()
    for h in user_hosts():
        hid = str(h.get("id") or "")
        if exclude_id is not None and hid == exclude_id:
            continue
        raw_ssh = str(h.get("ssh") or "").strip()
        if raw_ssh:
            host = _clean_host(raw_ssh.rsplit("@", 1)[-1])
            out.add((host, int(h.get("port") or 22)))
        else:
            out.add(("", 0))
    try:
        fcfg = F.conf()
    except ConfigError:
        fcfg = None
    for s in (fcfg or {}).get("servers") or []:
        raw_ssh = str(s.get("ssh") or "").strip()
        if raw_ssh:
            host = _clean_host(raw_ssh.rsplit("@", 1)[-1])
            out.add((host, int(s.get("port") or 22)))
    return out


def validate_target(ssh: str | None, port, *, is_admin: bool, exclude_id: str | None = None) -> tuple[str, int]:
    """校验外部机 SSH 目标与端口（防 SSRF / 选项注入 / 撞机接管）：
    - port 须为 1..65535 的整数（ssh="" 本机直连时固定 22）；
    - 普通用户严禁 ssh=""（本机直连）、回环/链路本地/未指定/组播地址（含 DNS 解析结果）；
    - 严禁与现有主机或组内服务器使用相同 (host, port)。返回标准化后的 (ssh, port)。"""
    raw_ssh = "" if ssh is None else str(ssh).strip()
    if not raw_ssh:
        if not is_admin:
            raise U.Denied(403, "普通用户不能添加本机直连主机（ssh 不能为空）")
        if ("", 0) in _existing_endpoints(exclude_id):
            raise U.Denied(409, "主机清单里已经有一台本机直连主机（ssh 为空）")
        return "", 22
    if isinstance(port, bool) or port is None or port == "":
        p = 22
    else:
        try:
            p = int(port)
        except (TypeError, ValueError):
            raise U.Denied(400, f"端口要写 1–65535 的整数，写的是 {port!r}") from None
    if not (1 <= p <= 65535):
        raise U.Denied(400, f"端口要在 1–65535 之间，写的是 {p}")
    user, clean = parse_ssh_target(raw_ssh)
    if not is_admin:
        if clean in ("localhost", "ip6-localhost") or clean.endswith(".localhost"):
            raise U.Denied(403, f"普通用户不能指向本机地址（{clean}）")
        if why := _bad_ip(clean):
            raise U.Denied(403, f"普通用户外部主机地址被拒绝：{why}")
        try:
            lan_bind = _clean_host(server_conf().get("lan_bind") or "")
        except ConfigError:
            lan_bind = ""
        if lan_bind and lan_bind not in ("0.0.0.0", "127.0.0.1") and clean == lan_bind:
            raise U.Denied(403, "普通用户不能把外部主机指向看板宿主机地址")
        # 若非字面 IP，尝试解析一次以拦截指向回环/链路本地的域名（离线 DNS 无法解析时不误拦）
        try:
            ipaddress.ip_address(clean)
        except ValueError:
            try:
                infos = socket.getaddrinfo(clean, p, type=socket.SOCK_STREAM)
            except OSError:
                infos = []
            for info in infos:
                addr = info[4][0]
                if why := _bad_ip(addr):
                    raise U.Denied(403, f"域名 {clean} 解析到受限地址（{addr}）：{why}")
    if (clean, p) in _existing_endpoints(exclude_id):
        raise U.Denied(409, f"目标 {clean}:{p} 与已有主机或组内服务器冲突：请核对地址与端口")
    norm_ssh = f"{user}@{clean}" if user else clean
    return norm_ssh, p


def _norm_num(data: dict, key: str, what: str, *, lo: float = 0.0, hi: float = 1e6, allow_zero: bool = False):
    v = data.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise U.Denied(400, f"{what}要填数字")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise U.Denied(400, f"{what}要填数字，写的是 {v!r}") from None
    if (x < lo if allow_zero else x <= lo) or x > hi:
        raise U.Denied(400, f"{what}范围不对：{v!r}")
    return int(x) if x.is_integer() else round(x, 2)


def _norm_path(raw, what: str) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    if any(c in s for c in ("\n", "\r", "\x00")) or not (s.startswith("/") or s == "~" or s.startswith("~/")):
        raise U.Denied(400, f"{what}必须是绝对路径或以 ~/ 开头（不能含换行）：{raw!r}")
    if ".." in s.split("/"):
        raise U.Denied(400, f"{what}不能包含 ..：{raw!r}")
    return s.rstrip("/") or "/"


def _norm_python(raw) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    s = str(raw).strip()
    if not PYTHON_RE.fullmatch(s):
        raise U.Denied(400, f"python 路径格式不对：{raw!r}")
    return s


def _norm_lease(raw) -> str | None:
    if raw is None or str(raw).strip() == "":
        return None
    ts = parse_time(str(raw).strip(), future=True, rel=True)
    if ts is None:
        raise U.Denied(400, f"到期时间看不懂：{raw!r}（用 YYYY-MM-DD HH:MM 或 MM-DD HH:MM）")
    return fmt_time(ts, year=True)


def _norm_fields(data: dict, who: U.Ident, *, existing: dict | None = None) -> dict:
    """校验并规范化外部主机字段（新增与修改共用）。"""
    _reject_private_key_upload(data)
    is_admin = who.role == "admin"
    hid = str(existing["id"]) if existing else str(data.get("id") or "").strip().lower()
    if not existing:
        if not HOST_ID_RE.fullmatch(hid) or hid in RESERVED_IDS:
            raise U.Denied(400, "主机 id 要由小写字母、数字、- 或 _ 组成（1–32 位，字母或数字开头，不能用保留词）")

    ssh_in = data["ssh"] if "ssh" in data else (existing.get("ssh") if existing else "")
    port_in = data["port"] if "port" in data else (existing.get("port", 22) if existing else 22)
    ssh, port = validate_target(ssh_in, port_in, is_admin=is_admin, exclude_id=hid if existing else None)

    pk_in = data.get("platform_kind") if "platform_kind" in data else (host_platform(existing) if existing else None)
    if pk_in is None or pk_in == "":
        pk = "other" if data.get("lease_until") else "none"
    else:
        pk = str(pk_in).strip().lower()
        if pk not in EXT_PLATFORM_KINDS:
            raise U.Denied(400, f"外部主机 platform_kind 只能选 {' / '.join(EXT_PLATFORM_KINDS)}，写的是 {pk_in!r}")

    share_in = data.get("share")
    if share_in is not None and share_in != "":
        sh = str(share_in).strip().lower()
        if sh != "private":
            raise U.Denied(403 if not is_admin else 400, "独立外部主机目前只支持 private（私有）模式")
    sh = "private"

    key_in = data.get("ssh_key") if "ssh_key" in data else data.get("key_mode")
    if key_in == "generate":
        key_in = "managed"
    if not ssh:
        ssh_key = "system"
    elif key_in is None or key_in == "":
        ssh_key = host_ssh_key(existing) if existing and existing.get("ssh") else "managed"
    else:
        ssh_key = str(key_in).strip().lower()
        if ssh_key not in ("managed", "system"):
            raise U.Denied(400, f"ssh_key 只能是 managed（看板专用密钥）或 system（宿主机默认密钥），写的是 {key_in!r}")
    # 确定最终字段后统一执行普通用户权限边界校验（覆盖继承自已有配置的值，防止省略 ssh_key 继承 system 绕过）
    if not is_admin:
        if not ssh:
            raise U.Denied(403, "普通用户只能添加远端 SSH 外部主机，不能将 ssh 留空")
        if sh != "private":
            raise U.Denied(403, "普通用户外部主机强制为私有模式（private）")
        if ssh_key != "managed":
            raise U.Denied(403, "普通用户只能使用看板生成的专用密钥（managed），不能修改使用宿主机默认密钥（system）的主机")

    if not is_admin:
        owner = who.name
        users = [who.name]
    else:
        if "owner" in data:
            o = str(data.get("owner") or "").strip()
            if o:
                acc = U.account(o)
                if not acc:
                    raise U.Denied(400, f"归属账号 {o} 不存在")
                owner = acc["name"]
            else:
                owner = None
        else:
            owner = existing.get("owner") if existing else None
        if "users" in data:
            raw_u = data.get("users")
            if raw_u is None:
                users = [owner] if owner else None
            elif isinstance(raw_u, list):
                users = []
                for x in raw_u:
                    acc = U.account(str(x))
                    if not acc:
                        raise U.Denied(400, f"users 里的账号 {x} 不存在")
                    if acc["name"] not in users:
                        users.append(acc["name"])
            else:
                raise U.Denied(400, "users 要是账号名列表")
        elif existing:
            users = existing.get("users")
        else:
            users = [owner] if owner else None

    label_in = data["label"] if "label" in data else (existing.get("label") if existing else hid)
    label = str(label_in or "").strip() or hid
    if len(label) > LABEL_MAX:
        raise U.Denied(400, f"显示名最多 {LABEL_MAX} 个字")

    data_root = _norm_path(data["data_root"], "data_root") if "data_root" in data else (existing.get("data_root") if existing else None)
    python = _norm_python(data["python"]) if "python" in data else (existing.get("python") if existing else None)
    lease_until = _norm_lease(data["lease_until"]) if "lease_until" in data else (existing.get("lease_until") if existing else None)

    warn_gb = _norm_num(data, "disk_warn_gb", "磁盘预警线", lo=0, hi=100000, allow_zero=True) if "disk_warn_gb" in data else (existing.get("disk_warn_gb") if existing else None)
    stop_gb = _norm_num(data, "disk_stop_gb", "磁盘停止线", lo=0, hi=100000, allow_zero=True) if "disk_stop_gb" in data else (existing.get("disk_stop_gb") if existing else None)
    quota_gb = _norm_num(data, "disk_quota_gb", "磁盘软配额", lo=0, hi=100000) if "disk_quota_gb" in data else (existing.get("disk_quota_gb") if existing else None)
    cpu_quota = _norm_num(data, "cpu_quota", "CPU 核数配额", lo=0, hi=4096) if "cpu_quota" in data else (existing.get("cpu_quota") if existing else None)
    mps_vram_gb = _norm_num(data, "mps_vram_gb", "Apple Silicon 显存上限", lo=0, hi=4096) if "mps_vram_gb" in data else (existing.get("mps_vram_gb") if existing else None)

    eff_warn = DISK_WARN_GB if warn_gb is None else warn_gb
    eff_stop = DISK_STOP_GB if stop_gb is None else stop_gb
    if eff_stop > 0 and eff_warn > 0 and eff_stop >= eff_warn:
        raise U.Denied(400, f"磁盘停止线（{eff_stop} GiB）要小于预警线（{eff_warn} GiB）")

    return {
        "id": hid,
        "label": label,
        "ssh": ssh,
        "port": port,
        "platform_kind": pk,
        "standalone": True,
        "share": sh,
        "ssh_key": ssh_key if ssh else None,
        "owner": owner,
        "users": users or None,
        "data_root": data_root or STANDALONE_DATA_ROOT,
        "python": python,
        "lease_until": lease_until,
        "disk_warn_gb": warn_gb,
        "disk_stop_gb": stop_gb,
        "disk_quota_gb": quota_gb,
        "cpu_quota": cpu_quota,
        "mps_vram_gb": mps_vram_gb,
    }


def _host_view(h: dict, key_info: dict | None = None) -> dict:
    """对外返回的脱敏主机对象（绝不含私钥路径或私钥原文）。"""
    d = host_defaults(h)
    info = key_info
    if info is None and d.get("ssh") and host_ssh_key(d) == "managed":
        try:
            info = PF.ssh_key_info(d.get("owner"), d["id"])
        except PF.PlatformError:
            info = None
    out = {
        "id": d["id"],
        "label": d.get("label") or d["id"],
        "ssh": d.get("ssh") or "",
        "port": d.get("port", 22),
        "platform_kind": d["platform_kind"],
        "standalone": True,
        "share": d["share"],
        "ssh_key": d["ssh_key"],
        "owner": d.get("owner"),
        "users": d.get("users") or [],
        "data_root": d["data_root"],
        "worker_home": d["worker_home"],
        "local_dir": d.get("local_dir"),
        "python": d.get("python", "python3"),
        "lease_until": d.get("lease_until"),
        "disk_warn_gb": d.get("disk_warn_gb"),
        "disk_stop_gb": d.get("disk_stop_gb"),
        "disk_quota_gb": d.get("disk_quota_gb"),
        "cpu_quota": d.get("cpu_quota"),
        "mps_vram_gb": d.get("mps_vram_gb"),
        "ssh_key_fp": info.get("fingerprint") if info else None,
    }
    return out


def _user_ext_count(username: str) -> int:
    low = username.strip().lower()
    return sum(1 for h in user_hosts() if U.is_external_host(h) and (str(h.get("owner") or "").strip().lower() == low))


def add_external(data: dict, who: U.Ident) -> dict:
    """新增外部主机（过 lease.need 与 HOSTS_LOCK）；若用托管密钥自动生成专用 Ed25519 公钥。"""
    LS.need()
    with HOSTS_LOCK:
        if who.role != "admin":
            n = _user_ext_count(who.name)
            if n >= U.USER_EXT_HOSTS:
                raise U.Denied(409, f"你名下已有 {n} 台外部主机（上限 {U.USER_EXT_HOSTS} 台）：删掉不用的再加")
        entry = _norm_fields(data, who)
        hid = entry["id"]
        created_key = False
        key_info = None
        if entry["ssh"] and entry["ssh_key"] == "managed":
            try:
                had = PF.ssh_key_info(entry.get("owner"), hid) is not None
                key_info = PF.ensure_ssh_key(entry.get("owner"), hid)
                created_key = not had
            except PF.PlatformError as e:
                raise U.Denied(409, str(e)) from e
        try:
            add_host(entry, f"{who.name} 添加了外部主机 {hid}（{entry['ssh'] or '本机'}:{entry['port']}）")
        except ConfigError as e:
            if created_key:
                PF.del_ssh_key(entry.get("owner"), hid)
            raise U.Denied(409, str(e)) from e
    return {
        "host": _host_view(entry, key_info),
        "pubkey": key_info["pubkey"] if key_info else None,
        "fingerprint": key_info["fingerprint"] if key_info else None,
        "auth_cmd": key_info["auth_cmd"] if key_info else None,
        "msg": f"已添加外部主机 {hid}" + ("；请复制授权命令到目标机执行后再点「测试连通」" if key_info else ""),
    }


def edit_external(hid: str, data: dict, who: U.Ident) -> dict:
    """修改外部主机配置；改 ssh/port 自动断开旧 ControlMaster 并清旧 known_hosts。"""
    LS.need()
    with HOSTS_LOCK:
        old = U.need_external_host(who, hid)
        new = _norm_fields(data, who, existing=old)
        old_managed = bool(old.get("ssh")) and host_ssh_key(old) == "managed"
        new_managed = bool(new.get("ssh")) and new.get("ssh_key") == "managed"
        conn_changed = (old.get("ssh") or "") != (new.get("ssh") or "") or int(old.get("port") or 22) != int(new.get("port") or 22)
        owner_changed = (old.get("owner") or "") != (new.get("owner") or "")
        key_info = None
        if new_managed:
            try:
                key_info = PF.ensure_ssh_key(new.get("owner"), hid)
            except PF.PlatformError as e:
                raise U.Denied(409, str(e)) from e
        updates = {k: v for k, v in new.items() if k != "id"}
        try:
            edit_host(hid, updates, f"{who.name} 修改了外部主机 {hid} 的配置")
        except ConfigError as e:
            raise U.Denied(409, str(e)) from e
        if conn_changed or owner_changed or (old_managed and not new_managed):
            lan.close_host_ssh(old, drop_known_hosts=conn_changed)
        if old_managed and (not new_managed or owner_changed):
            PF.del_ssh_key(old.get("owner"), hid)
    return {
        "host": _host_view(new, key_info),
        "pubkey": key_info["pubkey"] if key_info else None,
        "fingerprint": key_info["fingerprint"] if key_info else None,
        "auth_cmd": key_info["auth_cmd"] if key_info else None,
        "msg": f"已更新外部主机 {hid}",
    }


def remove_external(hid: str, who: U.Ident, *, force: bool = False) -> dict:
    """移除外部主机：核无在跑/排队任务（非 force 拒删）、从 hosts.toml 移除、断长连接、清独立 known_hosts、销毁专用私钥。"""
    LS.need()
    with HOSTS_LOCK:
        h = U.need_external_host(who, hid)
        if not force:
            snap = snapshot() or {}
            he = next((x for x in snap.get("hosts") or [] if x.get("id") == hid), None)
            st = (he or {}).get("status") or {}
            runs = st.get("running") or []
            waits = st.get("waiting") or []
            todos = st.get("todo") or []
            run_keys = {(j.get("project"), j.get("name")) for j in runs if isinstance(j, dict) and j.get("name")}
            pend_keys = {(w.get("project"), w.get("name")) for w in waits if isinstance(w, dict) and w.get("name")}
            for ln in todos:
                if isinstance(ln, dict):
                    p_id = ln.get("project")
                    for nm in ln.get("jobs") or []:
                        pend_keys.add((p_id, nm))
            running = len(runs)
            queued = max(len(waits), len(pend_keys - run_keys))
            if running or queued:
                raise U.Denied(409, f"主机 {hid} 还有 {running} 个在跑、{queued} 个待跑任务：先停下任务，或加 --force 强制删除")
        try:
            remove_host(hid, f"{who.name} 删除了外部主机 {hid}")
        except ConfigError as e:
            raise U.Denied(409, str(e)) from e
        lan.close_host_ssh(h, drop_known_hosts=True)
        key_removed = PF.del_ssh_key(h.get("owner"), hid) if host_ssh_key(h) == "managed" else False
    return {
        "id": hid,
        "key_removed": key_removed,
        "msg": f"已删除外部主机 {hid}" + ("（已同步销毁专用私钥并断开 SSH 连接）" if key_removed else ""),
    }


def key_external(hid: str, who: U.Ident, *, rotate: bool = False) -> dict:
    """查看或轮换外部机专用 Ed25519 公钥（零私钥上传：只回显公钥、指纹与授权命令）。"""
    LS.need()
    h = U.need_external_host(who, hid)
    if not h.get("ssh") or host_ssh_key(h) != "managed":
        raise U.Denied(409, f"主机 {hid} 当前不是托管专用密钥模式（ssh_key=managed），无需生成专用公钥")
    try:
        info = PF.ensure_ssh_key(h.get("owner"), hid, rotate=rotate)
    except PF.PlatformError as e:
        raise U.Denied(409, str(e)) from e
    if rotate:
        lan.close_host_ssh(h, drop_known_hosts=False)
    return {
        "id": hid,
        "rotated": rotate,
        "pubkey": info["pubkey"],
        "fingerprint": info["fingerprint"],
        "auth_cmd": info["auth_cmd"],
        "msg": f"已轮换 {hid} 的专用密钥，请将新公钥授权到目标机 ~/.ssh/authorized_keys" if rotate else f"已读取 {hid} 的专用公钥",
    }


def test_external(hid: str, who: U.Ident) -> dict:
    """单次测试外部机连通性、OS/GPU、Python 版本与 data_root 可写性（绝不重试）。"""
    LS.need()
    h = U.need_external_host(who, hid)
    # 发起连接前再次做 SSRF/目标校验（防 DNS 重绑定）
    validate_target(h.get("ssh") or "", h.get("port", 22), is_admin=who.role == "admin", exclude_id=hid)
    py = h.get("python") or "python3"
    dr = h.get("data_root") or STANDALONE_DATA_ROOT
    try:
        if not h.get("ssh"):
            r = subprocess.run([py, "-c", _TEST_PY, dr], capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
        else:
            argv = lan.host_ssh_argv(h, "-o", f"ConnectTimeout={TEST_TIMEOUT_S}")
            remote_cmd = f"{shell_path(py)} -c {shlex.quote(_TEST_PY)} {shell_path(dr)}"
            r = lan.ssh_run(argv + [remote_cmd], capture_output=True, text=True, timeout=TEST_TIMEOUT_S + 5)
    except ConfigError as e:
        return {"ok": False, "id": hid, "error": str(e), "hint": "请先生成专用公钥并授权到目标机"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "id": hid, "error": f"连接超时（{TEST_TIMEOUT_S} 秒）", "hint": "核对主机地址、端口与防火墙"}
    except OSError as e:
        return {"ok": False, "id": hid, "error": f"本机执行探测失败：{e}", "hint": "核对 ssh / python 可执行文件"}
    if r.returncode != 0:
        err = (r.stderr or r.stdout or f"退出码 {r.returncode}").strip()
        hint = "请检查目标机 ~/.ssh/authorized_keys 是否已写入看板专用公钥" if ("Permission denied" in err or "publickey" in err) else (
            "目标机主机指纹变化：可删除后重新添加或在编辑里保存重连" if "REMOTE HOST IDENTIFICATION HAS CHANGED" in err else "核对地址、端口与远端 python 路径"
        )
        return {"ok": False, "id": hid, "error": err[:300], "hint": hint}
    try:
        info = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "id": hid, "error": f"探测返回无法解析：{(r.stdout or '')[:200]}", "hint": "检查远端 shell 启动脚本是否打印了额外输出"}
    gpus = info.get("gpus") or []
    os_name = "WSL2 (Linux)" if info.get("is_wsl") else f"{info.get('os', '?')} ({info.get('arch', '?')})"
    msg = (f"连通正常：{os_name}，Python {info.get('py', '?')}，"
           f"GPU：{('、'.join(gpus) if gpus else '无 / 未检测到')}，"
           f"工作目录 {info.get('data_root')} 可用 {info.get('free_gb')} GiB"
           + ("" if info.get("writable") else "（警告：不可写！）"))
    return {"ok": bool(info.get("writable", True)), "id": hid, "info": info, "msg": msg,
            **({} if info.get("writable", True) else {"error": f"工作目录 {info.get('data_root')} 不可写"})}
