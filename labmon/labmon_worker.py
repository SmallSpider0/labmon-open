#!/usr/bin/env python3
"""labmon worker 入口脚本：实现按职责拆在同目录的 worker/ 包里，只用 Python 3.8 标准库，不 import labmon 其余模块。

服务器端与命令行入口沿用 `python3 <worker_home>/labmon_worker.py <子命令> ...`；
作为模块导入时（如独立 Python 3.8 检查或旧调用方）透明转发到同目录的 worker 包。
"""
import os
import sys

if __package__:
    from . import worker as _worker
else:
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in sys.path:
        sys.path.insert(0, _here)
    import worker as _worker


def __getattr__(name):
    return getattr(_worker, name)


def __dir__():
    return sorted(set(globals()) | set(_worker.__all__))


if __name__ == "__main__":
    sys.exit(_worker.main())
