#!/usr/bin/env python3
"""Google Colab 等无固定 SSH 算力节点的零依赖轻量推模式客户端（Python 3.8+ 标准库）。

仅做纯只读元数据上报与启动前防呆校验（os.statvfs 查云盘/本地盘余量、nvidia-smi、/proc、stdout PROGRESS i/N），
不遍历云盘目录、不做文件同步或传输服务。

用法 1（CLI 包装）：
    python3 labmon_ephemeral.py run \\
        --url https://board.example.com \\
        --project proj-a --job exp1_seed0 \\
        --drive-root /content/drive/MyDrive/labmon \\
        --artifact-dir /content/drive/MyDrive/labmon/projects/proj-a/runs/exp1_seed0 \\
        -- python3 train.py --seed 0

用法 2（Notebook 上下文管理器）：
    import labmon_ephemeral
    with labmon_ephemeral.track(project="proj-a", job="exp1_seed0",
                                drive_root="/content/drive/MyDrive/labmon",
                                artifact_dir="/content/drive/MyDrive/labmon/projects/proj-a/runs/exp1_seed0") as tr:
        for step in range(1, 101):
            tr.progress(step, 100, f"loss={0.5 / step:.4f}")

令牌与地址读取顺序：命令行参数 > 环境变量（LABMON_TOKEN / LABMON_URL）> Colab Secrets（google.colab.userdata）。
严禁将令牌硬编码进 Notebook。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

GIB = float(1 << 30)
PROGRESS_RE = re.compile(r"PROGRESS\s+(\d+)/(\d+)")
DEFAULT_INTERVAL = 30
DEFAULT_TTL = 180
DEFAULT_GRACE = 15
LAST_LINE_MAX = 200
STORAGE_MAX = 4


def _colab_secret(name: str) -> Optional[str]:
    try:
        from google.colab import userdata  # type: ignore[import-not-found]
        val = userdata.get(name)
        return str(val).strip() if val else None
    except Exception:  # noqa: BLE001
        return None


def resolve_token(explicit: Optional[str] = None) -> str:
    """按参数 > 环境变量 > Colab Secrets 顺序解析令牌；绝不打印原文。"""
    tok = (explicit or os.environ.get("LABMON_TOKEN") or _colab_secret("LABMON_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError("未找到令牌：请通过环境变量 LABMON_TOKEN、Colab Secrets（LABMON_TOKEN）或 --token 提供 project 令牌")
    return tok


def resolve_url(explicit: Optional[str] = None) -> str:
    """按参数 > 环境变量 > Colab Secrets 顺序解析看板根地址。"""
    url = (explicit or os.environ.get("LABMON_URL") or _colab_secret("LABMON_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("未找到看板地址：请通过环境变量 LABMON_URL、Colab Secrets（LABMON_URL）或 --url 提供")
    if not (url.startswith("https://") or url.startswith("http://")):
        raise RuntimeError("看板地址须以 https:// 或 http:// 开头")
    return url


def check_drive_mount(drive_root: Optional[str], *, allow_unmounted: bool = False) -> None:
    """启动前只读防呆校验：确认 --drive-root 确为已挂载的云盘目录，防未挂载时产物写入容器本地盘被回收清空。"""
    if not drive_root or allow_unmounted:
        return
    path = os.path.abspath(os.path.expanduser(drive_root))
    if not os.path.exists(path):
        raise RuntimeError(
            "云盘路径不存在：{}。请先执行 drive.mount('/content/drive') 并确认目录已就绪（测试时可用 --allow-unmounted 跳过）".format(path)
        )
    # 沿路径向上检查是否经过非根挂载点（或设备号与 / 不同）
    mounted = False
    cur = path
    while cur and cur != "/":
        if os.path.ismount(cur):
            mounted = True
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if not mounted:
        try:
            mounted = os.stat(path).st_dev != os.stat("/").st_dev
        except OSError:
            mounted = False
    if not mounted:
        raise RuntimeError(
            "云盘路径 {} 似乎位于容器本地根盘而非已挂载的 Google Drive（未检测到挂载点）："
            "请先执行 drive.mount('/content/drive')，避免实例回收后产物全部丢失".format(path)
        )


def collect_gpus() -> List[Dict[str, Any]]:
    """调用 nvidia-smi 采集 GPU 状态；无 GPU 运行时返回 []。"""
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
        "--format=csv,noheader,nounits",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []

    def _f(s: str) -> Optional[float]:
        s = s.strip()
        if not s or s.upper() in ("N/A", "[N/A]", "[NOT SUPPORTED]"):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    out: List[Dict[str, Any]] = []
    for line in r.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        out.append({
            "index": idx,
            "uuid": parts[1] or "GPU-{}".format(idx),
            "name": parts[2] or "GPU",
            "util": _f(parts[3]),
            "mem_used": _f(parts[4]) or 0.0,
            "mem_total": _f(parts[5]) or 0.0,
            "temp": _f(parts[6]) if len(parts) > 6 else None,
            "power": _f(parts[7]) if len(parts) > 7 else None,
            "power_limit": _f(parts[8]) if len(parts) > 8 else None,
        })
    return out


class _CpuSampler:
    """只读 /proc/stat 两次采样差分计算 CPU 占用百分比。"""

    def __init__(self) -> None:
        self._prev: Optional[Tuple[int, int]] = self._read()

    @staticmethod
    def _read() -> Optional[Tuple[int, int]]:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as fh:
                line = fh.readline()
            if not line.startswith("cpu "):
                return None
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            return total, idle
        except (OSError, ValueError, IndexError):
            return None

    def sample(self) -> Optional[float]:
        cur = self._read()
        if cur is None:
            return None
        prev = self._prev
        self._prev = cur
        if prev is None:
            return None
        dt = cur[0] - prev[0]
        di = cur[1] - prev[1]
        if dt <= 0:
            return None
        return round(max(0.0, min(100.0, (dt - di) / float(dt) * 100.0)), 1)


def collect_mem() -> Optional[Dict[str, float]]:
    """只读 /proc/meminfo 或 cgroup 内存统计，返回 {used_gb, limit_gb}。"""
    try:
        info: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    info[parts[0][:-1]] = int(parts[1]) * 1024
        tot = info.get("MemTotal")
        avail = info.get("MemAvailable", info.get("MemFree"))
        if tot and avail is not None:
            used = max(0, tot - avail)
            return {"used_gb": round(used / GIB, 2), "limit_gb": round(tot / GIB, 2)}
    except (OSError, ValueError):
        pass
    return None


def _statvfs_entry(kind: str, label: str, path: str) -> Optional[Dict[str, Any]]:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    if total <= 0:
        return None
    return {
        "kind": kind,
        "label": label,
        "path": path,
        "total_gb": round(total / GIB, 2),
        "avail_gb": round(avail / GIB, 2),
    }


def collect_storage(drive_root: Optional[str] = None, extra_local: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """仅用 os.statvfs 获取云盘与本地盘余量（零目录遍历），最多返回 4 项。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def _add(kind: str, label: str, path: str) -> None:
        if len(out) >= STORAGE_MAX or not path or path in seen or not os.path.exists(path):
            return
        entry = _statvfs_entry(kind, label, path)
        if entry:
            seen.add(path)
            out.append(entry)

    if drive_root:
        p = os.path.abspath(os.path.expanduser(drive_root))
        label = "Google Drive" if "/drive/" in p or "MyDrive" in p else "云盘"
        _add("cloud", label, p)
    local_candidates = list(extra_local or [])
    if not local_candidates:
        local_candidates = ["/content"] if os.path.isdir("/content") else ["/tmp"]
    for lp in local_candidates:
        _add("local", "本地盘 {}".format(lp), lp)
    return out


def default_host_id(gpus: List[Dict[str, Any]]) -> str:
    """根据 GPU 型号或主机名生成简洁的默认临时主机标识。"""
    if gpus:
        name = str(gpus[0].get("name") or "gpu").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
        for short in ("h100", "a100", "l4", "v100", "t4", "p100"):
            if short in slug:
                slug = short
                break
        slug = slug[:16] or "gpu"
        return "colab-{}-{}".format(slug, secrets.token_hex(2))
    hn = re.sub(r"[^a-zA-Z0-9_.-]+", "-", socket.gethostname()).strip("-")[:16] or "cpu"
    return "colab-{}-{}".format(hn.lower(), secrets.token_hex(2))


def default_label(host_id: str, gpus: List[Dict[str, Any]]) -> str:
    if gpus:
        name = str(gpus[0].get("name") or "GPU").replace("NVIDIA ", "").replace("Tesla ", "")
        return "Colab {} ({})".format(name, host_id)[:60]
    return "Colab ({})".format(host_id)[:60]


def parse_progress(line: str) -> Optional[List[int]]:
    m = PROGRESS_RE.search(line)
    if not m:
        return None
    cur, tot = int(m.group(1)), int(m.group(2))
    return [cur, tot] if tot > 0 else None


def post_push(url: str, token: str, payload: Dict[str, Any], timeout: float = 15.0) -> Tuple[int, Dict[str, Any]]:
    """向看板 POST /api/ephemeral/push 上报一次心跳，返回 (HTTP 状态码, 响应 JSON)。"""
    endpoint = url.rstrip("/") + "/api/ephemeral/push"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "labmon-ephemeral/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {"error": str(e)}
        return e.code, body if isinstance(body, dict) else {"error": str(e)}
    except OSError as e:
        return 0, {"error": str(e)}


class EphemeralTracker:
    """轻量心跳上报器，同时支撑 CLI 包装与 Notebook 上下文管理器。"""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        project: str,
        job: str,
        host_id: Optional[str] = None,
        instance_id: Optional[str] = None,
        label: Optional[str] = None,
        est_min: Optional[float] = None,
        gpus: Optional[List[int]] = None,
        interval: float = DEFAULT_INTERVAL,
        ttl_s: int = DEFAULT_TTL,
        drive_root: Optional[str] = None,
        artifact_dir: Optional[str] = None,
        local_paths: Optional[List[str]] = None,
        allow_unmounted: bool = False,
        poster=post_push,
    ) -> None:
        check_drive_mount(drive_root, allow_unmounted=allow_unmounted)
        self.url = url.rstrip("/")
        self.token = token
        self.project = project
        self.job_id = job
        self._poster = poster
        self._cpu = _CpuSampler()
        init_gpus = collect_gpus()
        self.host_id = host_id or default_host_id(init_gpus)
        self.instance_id = instance_id or ("run-" + secrets.token_hex(4))
        self.label = label or default_label(self.host_id, init_gpus)
        self.est_min = est_min
        self.job_gpus = list(gpus) if gpus is not None else [g["index"] for g in init_gpus]
        self.interval = max(5.0, float(interval))
        self.ttl_s = max(90, min(600, int(ttl_s)))
        self.drive_root = drive_root
        self.artifact_dir = artifact_dir
        self.local_paths = local_paths

        self._lock = threading.Lock()
        self._push_lock = threading.Lock()
        self._stop = threading.Event()
        self._finished = False
        self._thread: Optional[threading.Thread] = None
        self._start_ts = time.time()
        self._end_ts: Optional[float] = None
        self._status = "running"
        self._rc: Optional[int] = None
        self._progress: Optional[List[int]] = None
        self._last_line: Optional[str] = None
        self._log_mtime: Optional[float] = None
        self._warned: set = set()

    def progress(self, current: int, total: int, line: Optional[str] = None) -> None:
        """手动更新当前任务的进度（Notebook 模式常用）。"""
        now = time.time()
        with self._lock:
            if total > 0 and current >= 0:
                self._progress = [int(current), int(total)]
            text = "PROGRESS {}/{}".format(int(current), int(total))
            if line:
                text = "{} {}".format(text, str(line).strip())
            self._last_line = text[:LAST_LINE_MAX]
            self._log_mtime = now

    def feed_line(self, line: str) -> None:
        """解析子进程输出的一行文本，更新 PROGRESS 与尾行。"""
        clean = " ".join(str(line).splitlines()).strip()
        if not clean:
            return
        now = time.time()
        pr = parse_progress(clean)
        with self._lock:
            if pr is not None:
                self._progress = pr
            self._last_line = clean[:LAST_LINE_MAX]
            self._log_mtime = now

    def build_payload(self, *, closing: bool = False) -> Dict[str, Any]:
        gpus = collect_gpus()
        cpu_pct = self._cpu.sample()
        mem = collect_mem()
        storage = collect_storage(self.drive_root, self.local_paths)
        with self._lock:
            job = {
                "id": self.job_id,
                "status": self._status,
                "gpus": self.job_gpus,
                "start_ts": round(self._start_ts, 3),
                "end_ts": round(self._end_ts, 3) if self._end_ts is not None else None,
                "rc": self._rc,
                "est_min": self.est_min,
                "progress": list(self._progress) if self._progress else None,
                "last_line": self._last_line,
                "log_mtime": round(self._log_mtime, 3) if self._log_mtime is not None else None,
                "artifact_dir": self.artifact_dir,
            }
        return {
            "project": self.project,
            "host_id": self.host_id,
            "instance_id": self.instance_id,
            "label": self.label,
            "ttl_s": self.ttl_s,
            "closing": closing,
            "client_t": round(time.time(), 3),
            "gpus": gpus,
            "cpu_pct": cpu_pct,
            "cpu_count": os.cpu_count(),
            "mem": mem,
            "storage": storage,
            "jobs": [job],
        }

    def push_once(self, *, closing: bool = False) -> Tuple[int, Dict[str, Any]]:
        with self._push_lock:
            if not closing and self._finished:
                return 0, {}
            payload = self.build_payload(closing=closing)
            retries = 3 if closing else 1
            code, body = 0, {}
            for attempt in range(retries):
                if attempt > 0:
                    payload["client_t"] = round(time.time(), 3)
                code, body = self._poster(self.url, self.token, payload)
                if code == 200:
                    for w in body.get("warnings") or []:
                        if w not in self._warned:
                            self._warned.add(w)
                            print("[labmon-ephemeral] 警告：{}".format(w), file=sys.stderr, flush=True)
                    return code, body
                if code in (400, 401, 403, 404, 413):
                    print("[labmon-ephemeral] 上报被拒（HTTP {}）：{}".format(code, body.get("error") or body), file=sys.stderr, flush=True)
                    return code, body
                if attempt + 1 < retries:
                    time.sleep(2.0 * (attempt + 1))
            if code != 200:
                print("[labmon-ephemeral] 上报未成功（HTTP {}）：{}".format(code, body.get("error") or body), file=sys.stderr, flush=True)
            return code, body

    def start(self) -> None:
        self._finished = False
        self.push_once(closing=False)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="labmon-ephemeral-hb", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            if self._finished:
                break
            self.push_once(closing=False)

    def finish(self, *, ok: bool, rc: int = 0, note: Optional[str] = None) -> None:
        self._finished = True
        self._stop.set()
        with self._lock:
            self._status = "done" if ok else "failed"
            self._rc = int(rc)
            self._end_ts = time.time()
            if note:
                self._last_line = str(note)[:LAST_LINE_MAX]
                self._log_mtime = self._end_ts
        # _push_lock 保证等待任何在途普通心跳完全返回后，才发送最终 closing=True 状态
        self.push_once(closing=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


@contextlib.contextmanager
def track(
    *,
    project: str,
    job: str,
    url: Optional[str] = None,
    token: Optional[str] = None,
    host_id: Optional[str] = None,
    instance_id: Optional[str] = None,
    label: Optional[str] = None,
    est_min: Optional[float] = None,
    gpus: Optional[List[int]] = None,
    interval: float = DEFAULT_INTERVAL,
    ttl_s: int = DEFAULT_TTL,
    drive_root: Optional[str] = None,
    artifact_dir: Optional[str] = None,
    local_paths: Optional[List[str]] = None,
    allow_unmounted: bool = False,
    poster=post_push,
):
    """Notebook 上下文管理器：自动启动后台心跳并在退出或异常时上报终态。"""
    tr = EphemeralTracker(
        url=resolve_url(url),
        token=resolve_token(token),
        project=project,
        job=job,
        host_id=host_id,
        instance_id=instance_id,
        label=label,
        est_min=est_min,
        gpus=gpus,
        interval=interval,
        ttl_s=ttl_s,
        drive_root=drive_root,
        artifact_dir=artifact_dir,
        local_paths=local_paths,
        allow_unmounted=allow_unmounted,
        poster=poster,
    )
    tr.start()
    try:
        yield tr
    except BaseException as exc:
        tr.finish(ok=False, rc=1, note="{}: {}".format(type(exc).__name__, exc))
        raise
    else:
        tr.finish(ok=True, rc=0)


def run_command_wrapped(
    cmd: List[str],
    tracker: EphemeralTracker,
    *,
    log_file: Optional[str] = None,
    grace_s: float = DEFAULT_GRACE,
) -> int:
    """运行实验子进程，实时透传输出、捕获 PROGRESS，并在收到 SIGTERM 时透传并等待子进程回写 checkpoint。"""
    if not cmd:
        raise RuntimeError("请提供要执行的命令（在 -- 之后）")
    tracker.start()
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=(os.name == "posix"),
    )

    sig_caught = [0]
    grace_timer: List[Optional[threading.Timer]] = [None]

    def _kill_proc() -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass

    def _on_signal(signum: int, _frame: Any) -> None:
        sig_caught[0] = signum
        if proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
            if grace_timer[0] is None:
                tm = threading.Timer(max(0.05, float(grace_s)), _kill_proc)
                tm.daemon = True
                tm.start()
                grace_timer[0] = tm

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if log_fh is not None:
                log_fh.write(line)
                log_fh.flush()
            tracker.feed_line(line)
        try:
            rc = proc.wait(timeout=grace_s if sig_caught[0] else None)
        except subprocess.TimeoutExpired:
            _kill_proc()
            rc = proc.wait()
    finally:
        if grace_timer[0] is not None:
            grace_timer[0].cancel()
        if proc.stdout is not None:
            proc.stdout.close()
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGINT, prev_int)
        if log_fh is not None:
            log_fh.close()

    if rc < 0:
        rc = 128 + abs(rc)
    tracker.finish(ok=(rc == 0), rc=rc)
    return rc


def _parse_gpus(raw: Optional[str]) -> Optional[List[int]]:
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "" or raw.lower() == "none":
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip() != ""]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="labmon 推模式轻量临时 worker 客户端（零第三方依赖）")
    sub = ap.add_subparsers(dest="sub", required=True)
    rp = sub.add_parser("run", help="包装执行一条实验命令并定期上报心跳与进度")
    rp.add_argument("--url", default=None, help="看板公网入口地址（缺省读 LABMON_URL 或 Colab Secrets）")
    rp.add_argument("--token", default=None, help="project 令牌（缺省读 LABMON_TOKEN 或 Colab Secrets）")
    rp.add_argument("--project", required=True, help="项目 id（与 monitor.toml 一致）")
    rp.add_argument("--job", required=True, help="任务 id")
    rp.add_argument("--host-id", default=None, help="临时主机标识（缺省自动生成，不能与 hosts.toml 重名）")
    rp.add_argument("--instance-id", default=None, help="本次实例唯一标识（缺省自动生成）")
    rp.add_argument("--label", default=None, help="页面显示名")
    rp.add_argument("--est", type=float, default=None, help="预计时长（分钟）")
    rp.add_argument("--gpus", default=None, help="所用 GPU 序号，逗号分隔；纯 CPU 写 none")
    rp.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="心跳间隔秒数（默认 30）")
    rp.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="服务端失联超时秒数（90–600，默认 180）")
    rp.add_argument("--grace", type=float, default=DEFAULT_GRACE, help="收到 SIGTERM 后等待子进程回写 checkpoint 的宽限秒数（默认 15）")
    rp.add_argument("--drive-root", default=None, help="云盘根目录（如 /content/drive/MyDrive/labmon），启动前校验挂载并只读 statvfs 上报余量")
    rp.add_argument("--artifact-dir", default=None, help="本次任务的云盘持久产物目录（上限 256 字符）")
    rp.add_argument("--log-file", default=None, help="本地实时日志路径（推荐 /tmp/labmon_<job>.log）")
    rp.add_argument("--allow-unmounted", action="store_true", help="跳过 --drive-root 的挂载点防呆校验（仅用于离线测试）")
    rp.add_argument("cmd", nargs=argparse.REMAINDER, help="在 -- 之后写要执行的实验命令")

    args = ap.parse_args(argv)
    if args.sub == "run":
        cmd = list(args.cmd)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            ap.error("请在 -- 之后提供要执行的命令")
        try:
            tracker = EphemeralTracker(
                url=resolve_url(args.url),
                token=resolve_token(args.token),
                project=args.project,
                job=args.job,
                host_id=args.host_id,
                instance_id=args.instance_id,
                label=args.label,
                est_min=args.est,
                gpus=_parse_gpus(args.gpus),
                interval=args.interval,
                ttl_s=args.ttl,
                drive_root=args.drive_root,
                artifact_dir=args.artifact_dir,
                allow_unmounted=args.allow_unmounted,
            )
        except RuntimeError as e:
            print("[labmon-ephemeral] {}".format(e), file=sys.stderr)
            return 2
        return run_command_wrapped(cmd, tracker, log_file=args.log_file, grace_s=args.grace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
