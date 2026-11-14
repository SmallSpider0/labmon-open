"""专用算力池的离线执行协议。协调者持有账本，launcher 实现由现场验收提供。"""
from __future__ import annotations

import hmac
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


class Refused(ValueError):
    pass


@dataclass(frozen=True)
class Grant:
    attempt_id: str
    account: str
    project: str
    job: str
    fingerprint: str              # 受信适配器绑定队首、清单、.env 和工作区
    node: str
    physical_host: str
    generation: str
    devices: tuple[str, ...]
    cpu_cores: float
    ram_gib: float
    until: float


@dataclass(frozen=True)
class Node:
    node: str
    physical_host: str
    generation: str
    devices: frozenset[str]
    cpu_cores: float
    ram_gib: float
    observed_at: float
    dedicated: bool = False
    accepting: bool = False
    isolation_verified: bool = False


class Coordinator:
    """仅由受信服务持有。账本目录只给协调者，launcher 不能直接访问数据库。"""

    def __init__(self, path: Path, launcher_keys: dict[str, str], *, enabled: bool = False, max_age: float = 30, clock=time.time):
        self.path = path
        self.keys = dict(launcher_keys)
        self.enabled = enabled
        self.max_age = max_age
        self.clock = clock
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, grant_json TEXT NOT NULL, phase TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        finally:
            db.close()       # 异常时 SQLite 回滚未提交的事务

    def authenticate(self, node: str, key: str) -> None:
        expected = self.keys.get(node)
        if not expected or not hmac.compare_digest(expected.encode(), key.encode()):
            raise Refused("launcher 无权访问该节点")

    def check(self, grant: Grant, node: Node) -> None:
        now = self.clock()
        if not self.enabled or not node.isolation_verified:
            raise Refused("只试算：执行隔离尚未验收")
        if not node.dedicated or not node.accepting:
            raise Refused("节点未获准接单")
        if not math.isfinite(node.observed_at) or not 0 <= now - node.observed_at <= self.max_age:
            raise Refused("节点状态已过期")
        if not grant.physical_host or not grant.generation or (
            grant.node, grant.physical_host, grant.generation
        ) != (node.node, node.physical_host, node.generation):
            raise Refused("节点、物理机或执行代际不符")
        if not math.isfinite(grant.until) or grant.until <= now:
            raise Refused("承诺已过期")
        if not all((grant.attempt_id, grant.account, grant.project, grant.job, grant.fingerprint)):
            raise Refused("承诺缺少任务身份")
        if not grant.devices or not all(grant.devices) or len(grant.devices) != len(set(grant.devices)) or not set(grant.devices) <= node.devices:
            raise Refused("整卡分配不属于本节点")
        if not all(math.isfinite(want) and math.isfinite(have) and 0 < want <= have for want, have in (
            (grant.cpu_cores, node.cpu_cores), (grant.ram_gib, node.ram_gib)
        )):
            raise Refused("CPU 或内存限额无效")

    @staticmethod
    def lookup(db, attempt_id: str) -> tuple[Grant, str]:
        row = db.execute("SELECT grant_json, phase FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise Refused("不存在受信承诺")
        return Grant(**json.loads(row[0])), row[1]

    @staticmethod
    def update(db, attempt_id: str, phase: str) -> None:
        db.execute("UPDATE attempts SET phase=? WHERE id=?", (phase, attempt_id))

    def capacity(self, db, grant: Grant, node: Node) -> None:
        cpu, ram = grant.cpu_cores, grant.ram_gib
        active = db.execute("SELECT grant_json FROM attempts WHERE phase != 'released' AND id != ?", (grant.attempt_id,))
        for (raw,) in active:
            other = Grant(**json.loads(raw))
            if (grant.account, grant.project, grant.job) == (other.account, other.project, other.job):
                raise Refused("该队首已有未结束的尝试")
            if set(grant.devices).intersection(other.devices):
                raise Refused("分配的 GPU 仍被占用")
            if grant.node == other.node:
                cpu += other.cpu_cores
                ram += other.ram_gib
        if cpu > node.cpu_cores or ram > node.ram_gib:
            raise Refused("节点 CPU 或内存余量不足")

    def reserve(self, grant: Grant, node: Node) -> None:
        """受信调度适配器签发。影子输出没有调用资格。"""
        with self.transaction() as db:
            self.check(grant, node)
            self.capacity(db, grant, node)
            try:
                db.execute("INSERT INTO attempts VALUES (?, ?, 'reserved')",
                           (grant.attempt_id, json.dumps(asdict(grant)),))
            except sqlite3.IntegrityError as exc:
                raise Refused("尝试已签发") from exc

    def launch(self, attempt_id: str, node: Node, key: str, backend) -> None:
        """受信 launcher 实现 matches/start。认领落盘后才启动，任何中断都留下占用。"""
        self.authenticate(node.node, key)
        with self.transaction() as db:
            grant, phase = self.lookup(db, attempt_id)
            if phase != "reserved":
                raise Refused("承诺已领取或结束")
            self.check(grant, node)
            self.capacity(db, grant, node)
            self.update(db, attempt_id, "unknown")
        with self.transaction() as db:
            _, phase = self.lookup(db, attempt_id)
            if phase != "unknown":
                raise Refused("承诺已取消")
            self.check(grant, node)
            if not backend.matches(grant):
                self.update(db, attempt_id, "released")
            else:
                backend.start(grant)
                self.update(db, attempt_id, "running")
                return
        raise Refused("账户授权、工作区或队首已变")

    def request_stop(self, attempt_id: str, account: str) -> None:
        """由受信账号适配器传入认证过的账号。停止请求只记录，launcher 负责实际清理。"""
        with self.transaction() as db:
            grant, phase = self.lookup(db, attempt_id)
            if account != grant.account:
                raise Refused("只能停止本账号的尝试")
            if phase == "reserved":
                self.update(db, attempt_id, "released")
            if phase in ("unknown", "running"):
                self.update(db, attempt_id, "stopping")

    def reconcile(self, attempt_id: str, node: Node, key: str, backend) -> None:
        """launcher 证实尝试未在运行、已清理且设备可用后才释放。"""
        self.authenticate(node.node, key)
        with self.transaction() as db:
            grant, phase = self.lookup(db, attempt_id)
            if (node.node, node.physical_host, node.generation) != (grant.node, grant.physical_host, grant.generation):
                raise Refused("结束回执与承诺代际不符")
            if phase == "released":
                return
            if phase == "reserved" and self.clock() >= grant.until:
                self.update(db, attempt_id, "released")
                return
            if phase == "reserved":
                raise Refused("尝试尚未过期")
            if phase == "stopping":
                backend.stop(grant)
            if not backend.cleaned(grant):
                raise Refused("尝试尚未核实清理")
            self.update(db, attempt_id, "released")
