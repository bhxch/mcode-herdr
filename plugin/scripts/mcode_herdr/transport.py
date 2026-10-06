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
    conn = None
    try:
        # socket() 自己也会抛（fd 耗尽 EMFILE），所以构造和超时设置都得在守卫区内，
        # 否则异常会逃出 report()，而且 finally 里的 close 也没机会执行
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(herdr.SOCKET_TIMEOUT)
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
        reply = json.loads(buf.decode("utf-8", "replace"))
        # 只有 {"id":..., "result":...} 形状的 herdr 应答才算“已接收”：
        # 数组/null/裸数字/乱码都不是应答（null 还会让 in 判断抛 TypeError 逃出去），
        # 把它们误判成成功，调用方就会记下一个 herdr 根本不知道的状态，
        # 而 decide() 按 last_reported 去重会把同一事件永久压掉，pane 再无恢复触发地发散
        return isinstance(reply, dict) and "error" not in reply
    except (OSError, ValueError, json.JSONDecodeError):
        # socket.timeout 即 TimeoutError，是 OSError 子类，0.5s 超时已在此被吞掉
        return False
    finally:
        if conn is not None:
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
    resume: Optional[list] = None
    if resume_argv:
        try:
            validate_resume_argv(resume_argv)
        except ValueError:
            # 恢复命令校验失败说明 herdr 同样会拒收它，硬发只会白白丢掉这次上报；
            # 恢复命令只是增强能力、状态转移才是主功能，所以降级为不带恢复命令继续上报，
            # 而不是把 ValueError 抛出去把状态上报一起赔进去
            pass
        else:
            resume = list(resume_argv)
            params["resume_argv"] = resume
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
    if resume:
        argv += ["--"] + resume
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
