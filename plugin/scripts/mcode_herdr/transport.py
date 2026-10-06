"""双通道上报：优先 Unix socket 直连，失败回落 $HERDR_BIN_PATH CLI。

两条通道协议等价。任何异常都吞掉并返回 False —— herdr 集成绝不能影响 mcode。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from typing import Mapping, Optional, Sequence

from . import herdr

_COMMAND_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _run_cli(argv: Sequence[str], timeout: float) -> int:
    """独立成函数，便于测试替换。"""
    return subprocess.run(argv, timeout=timeout,
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def validate_resume_argv(argv: Optional[Sequence[str]]) -> None:
    """对齐 herdr src/agent_resume.rs 的 validate_resume_argv。"""
    if not argv:
        raise ValueError("resume argv must not be empty")
    if len(argv) > herdr.RESUME_MAX_ARGS:
        raise ValueError(f"resume argv exceeds {herdr.RESUME_MAX_ARGS} elements")
    if sum(len(a.encode()) for a in argv) > herdr.RESUME_MAX_BYTES:
        raise ValueError("resume argv too large")
    for arg in argv:
        if "'" in arg:
            raise ValueError("resume argv must not contain single quotes")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in arg):
            raise ValueError("resume argv must not contain control characters")
    head = argv[0]
    if head.startswith("-") or "/" in head or not _COMMAND_NAME.match(head):
        raise ValueError("resume command must be a bare command name, not a path")


def _socket_report(env: Mapping[str, str], method: str, params: dict) -> bool:
    sock_path = env.get("HERDR_SOCKET_PATH")
    if not sock_path:
        return False
    payload = {"id": f"{herdr.SOURCE}:{time.time_ns()}", "method": method, "params": params}
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(herdr.SOCKET_TIMEOUT)
    try:
        conn.connect(sock_path)
        conn.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return False
        return "error" not in json.loads(buf.decode("utf-8", "replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        conn.close()


def report(env: Mapping[str, str], state: str, seq: int, *,
           message: str = "", session_id: Optional[str] = None,
           resume_argv: Optional[Sequence[str]] = None) -> bool:
    params = {
        "pane_id": env["HERDR_PANE_ID"],
        "source": herdr.SOURCE,
        "agent": herdr.AGENT,
        "state": state,
        "seq": seq,
    }
    if message:
        params["message"] = message[:400]
    if session_id:
        params["agent_session_id"] = session_id
    if resume_argv:
        validate_resume_argv(resume_argv)
        params["resume_argv"] = list(resume_argv)
        # 不传 agent_session_path：它与 session_ref 一样会被白名单挡掉，传了也是噪音
    if _socket_report(env, "pane.report_agent", params):
        return True
    argv = [env["HERDR_BIN_PATH"], "pane", "report-agent", env["HERDR_PANE_ID"],
            "--source", herdr.SOURCE, "--agent", herdr.AGENT,
            "--state", state, "--seq", str(seq)]
    if message:
        argv += ["--message", message[:400]]
    if session_id:
        argv += ["--agent-session-id", session_id]
    if resume_argv:
        argv += ["--"] + list(resume_argv)
    try:
        return _run_cli(argv, herdr.CLI_TIMEOUT) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def release(env: Mapping[str, str], seq: int) -> bool:
    params = {"pane_id": env["HERDR_PANE_ID"], "source": herdr.SOURCE,
              "agent": herdr.AGENT, "seq": seq}
    if _socket_report(env, "pane.release_agent", params):
        return True
    argv = [env["HERDR_BIN_PATH"], "pane", "release-agent", env["HERDR_PANE_ID"],
            "--source", herdr.SOURCE, "--agent", herdr.AGENT, "--seq", str(seq)]
    try:
        return _run_cli(argv, herdr.CLI_TIMEOUT) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def next_seq() -> int:
    """herdr 要求 seq 严格递增；time_ns 跨进程也单调。"""
    return time.time_ns()
