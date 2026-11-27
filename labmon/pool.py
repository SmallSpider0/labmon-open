"""算力池基础设施（#212 PR 1 清理旧借用推广型算力池后的最小公共入口；PR 2 将重写为共有型算力池引擎）。

保留项目页池待放渲染与命令行 `worker rm pool` 所需的键格式、定位与基础成员判定，
所有旧版借用推广型算力池逻辑（入池开关、自动借还、收回）已彻底移除。
"""
from __future__ import annotations

import posixpath

from .config import POOL
from .state import snapshot
from .users import Ident

POOL_TELL_S = 2 * 3600         # 池里待放满这么久，给实验会话报 pool_wait:（collect：一条清单一条）


def key(root: str, jobs: str) -> str:
    """一条池清单的稳定键（规整后的 目录|清单）。"""
    return f"{posixpath.normpath(root or '')}|{posixpath.normpath(jobs or '')}"


def member(who: Ident, cfg: dict | None = None) -> bool:
    """是否为算力池成员（PR 1 清理旧借用池后仅管理员为真；PR 2 按共有池成员配置重写）。"""
    return who.role == "admin"


def locate(root: str, jobs: str, snap: dict | None = None) -> str:
    """`labmon worker rm pool` 在哪撤回这条清单：返回目标主机 id。
    优先找池里待放或带 pool 标记的登记，找不到回退到 POOL。"""
    from .users import same_list
    snap = snapshot() if snap is None else snap
    for x in (snap or {}).get("regs") or []:
        for q in x.get("queues") or []:
            if not same_list(q, root, jobs):
                continue
            if x.get("host") == POOL or q.get("pool"):
                return x["host"]
    return POOL
