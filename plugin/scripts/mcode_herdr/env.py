"""通过 /proc 父进程链回溯，找出带 HERDR_ENV=1 的祖先（即 mcode 进程）。

mcode 的钩子只继承 safeHookEnvironment() 白名单环境，HERDR_* 不会传入子进程，
所以只能从祖先进程的环境里取回。父进程号必须读 /proc/<pid>/status 的 PPid:，
不能按位置解析 /proc/<pid>/stat —— comm 含空格或括号会错位。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

MAX_ANCESTRY = 12
REQUIRED = ("HERDR_ENV", "HERDR_PANE_ID")


def read_environ(pid: int, proc_root: Path) -> dict:
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except (OSError, ValueError):
        return {}
    out = {}
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        text = chunk.decode("utf-8", "replace")
        key, sep, value = text.partition("=")
        if sep:
            out[key] = value
    return out


def read_ppid(pid: int, proc_root: Path) -> Optional[int]:
    try:
        for line in (proc_root / str(pid) / "status").read_text(errors="replace").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def discover_herdr_env(proc_root: Path = Path("/proc"), start_pid: Optional[int] = None) -> Optional[Mapping[str, str]]:
    """向上回溯，找到第一个 HERDR_ENV=1 且有 HERDR_PANE_ID 的祖先。

    找不到时返回 None —— 调用方据此完全静默退出。
    """
    pid = os.getpid() if start_pid is None else start_pid
    proc_root = Path(proc_root)
    for _ in range(MAX_ANCESTRY):
        if pid is None or pid <= 1:
            return None
        env = read_environ(pid, proc_root)
        if env.get("HERDR_ENV") == "1" and all(env.get(name) for name in REQUIRED):
            return {k: v for k, v in env.items() if k.startswith("HERDR_")}
        pid = read_ppid(pid, proc_root)
    return None