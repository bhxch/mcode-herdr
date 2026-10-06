"""通过 /proc 父进程链回溯，找出带 HERDR_ENV=1 的祖先（即 mcode 进程）。

mcode 的钩子只继承一份固定的白名单环境（PATH HOME LANG TERM SHELL 等十余项），
HERDR_* 不在其中，所以只能从祖先进程的环境里取回。父进程号必须读 /proc/<pid>/status 的 PPid:，
不能按位置解析 /proc/<pid>/stat —— comm 含空格或括号会错位。

回溯命中的那个祖先就是 mcode 自己，所以连它的身份（pid + 启动指纹）一起返回：
状态文件靠这个判断「写这份状态的 mcode 是不是已经死了」。mcode 被 kill -9 之后
Stop 永远不会到达，不做这个判断 pane 会永远停在 working（见 decide.py 的子代理启发式）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Union

MAX_ANCESTRY = 12
REQUIRED = ("HERDR_PANE_ID", "HERDR_BIN_PATH")

# 归属信息必须留在 HERDR_ 前缀里：runtime._resolve_herdr_env 只转发 HERDR_*，
# herdr-report.py 正是靠这份转发把身份从钩子进程搬进脱离出去的 worker。
# transport 拼 CLI argv 时只认白名单里的几个键，这两个不会被误发给 herdr。
OWNER_PID = "HERDR_OWNER_PID"
OWNER_START = "HERDR_OWNER_START"

# /proc/<pid>/stat 的第 22 个字段是 starttime（进程启动以来的时钟数），是区分
# 「同一个 pid 的同一个进程」和「pid 被内核回收后另起的新进程」的标准指纹。
_STARTTIME_FIELD = 22


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


def read_starttime(pid: Union[int, str], proc_root: Path) -> Optional[str]:
    """读 /proc/<pid>/stat 的 starttime，原样返回十进制字符串；读不到返回 None。

    只有 stat 里有 starttime，status 里没有，所以这里必须解析 stat —— 但只能
    从最后一个 ')' 之后开始数：第 2 个字段 comm 是进程名，里面既可能有空格
    （"weird name"）也可能有右括号（"a)b"），按空格切或按第一个 ')' 切都会错位。
    ')' 之后的第一个 token 就是第 3 个字段（state），所以 starttime 是第 20 个 token。

    原样返回字符串而不是 int：pid 和 starttime 在本插件里只当 /proc 路径和一次性
    比对用的记号，从不参与算术；不解析就少一处能把 int/str 写反的地方，而写反的
    后果是「明明活着却被判成过期」或「明明死了却判成活着」。
    """
    try:
        raw = (proc_root / str(pid) / "stat").read_text(errors="replace")
        return raw.rsplit(")", 1)[1].split()[_STARTTIME_FIELD - 3]
    except (OSError, ValueError, IndexError):
        return None


def discover_herdr_env(proc_root: Path = Path("/proc"), start_pid: Optional[int] = None) -> Optional[Mapping[str, str]]:
    """向上回溯，找到第一个 HERDR_ENV=1 且 HERDR_PANE_ID、HERDR_BIN_PATH 均非空的祖先。

    返回值除了那一份 HERDR_*，还带上命中祖先的 OWNER_PID / OWNER_START。
    找不到时返回 None —— 调用方据此完全静默退出。
    """
    pid = os.getpid() if start_pid is None else start_pid
    proc_root = Path(proc_root)
    for _ in range(MAX_ANCESTRY):
        if pid is None or pid <= 1:
            return None
        env = read_environ(pid, proc_root)
        if env.get("HERDR_ENV") == "1" and all(env.get(name) for name in REQUIRED):
            found = {k: v for k, v in env.items() if k.startswith("HERDR_")}
            start = read_starttime(pid, proc_root)
            if start is not None:
                found[OWNER_PID] = str(pid)
                found[OWNER_START] = start
            # 指纹读不出来时两个键都不给：半个身份证明不了任何事，
            # 落盘时只能整体缺省，由 store 当成「归属未知」处理
            return found
        pid = read_ppid(pid, proc_root)
    return None
