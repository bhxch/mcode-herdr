# plugin/scripts/mcode_herdr/runtime.py
"""把「事件 → decide → transport → 落盘」串起来。

钩子入口 herdr-report.py 会派一个脱离进程组的后台进程调用 main()，
所以这里只管把一次上报做对，任何异常都不得逃出去。
"""
from __future__ import annotations

from pathlib import Path
from typing import IO, Mapping, Optional

from . import herdr, transport
from .decide import Action, Decision, decide
from .env import discover_herdr_env
from .payload import load_payload
from .store import PaneState, Store

_VALID_STATES = frozenset({herdr.STATE_IDLE, herdr.STATE_WORKING, herdr.STATE_BLOCKED})


def _resume_argv(session_id: Optional[str]) -> list:
    # 首词必须是 PATH 上的裸命令名，herdr 会拒绝绝对路径。
    return ["mcode", "--session", session_id] if session_id else []


def _resolve_herdr_env(env: Mapping[str, str], proc_root: Optional[Path]) -> Optional[Mapping[str, str]]:
    if env.get("HERDR_ENV") == "1" and env.get("HERDR_PANE_ID"):
        return {k: v for k, v in env.items() if k.startswith("HERDR_")}
    return discover_herdr_env(proc_root=proc_root or Path("/proc"))


def run_once(action: str, raw_payload: str, env: Mapping[str, str],
             proc_root: Optional[Path] = None) -> int:
    herdr_env = _resolve_herdr_env(env, proc_root)
    if herdr_env is None:
        return 0  # 不在 herdr 里：彻底静默
    try:
        payload = load_payload(raw_payload)
    except ValueError:
        return 0

    pane_id = herdr_env["HERDR_PANE_ID"]
    store = Store(Path(env.get("PLUGIN_DATA") or "/tmp"))

    def step(state: PaneState) -> None:
        decision = decide(Action(action), payload, state)
        if decision is None:
            return
        if decision.kind is Decision.Kind.RELEASE:
            transport.release(herdr_env, transport.next_seq())
            state.root_session = None
            state.blocked_by = None
            state.last_reported = None
            return
        if decision.state not in _VALID_STATES:
            return  # 状态机若将来吐出非法状态，宁可不报也不要让 herdr 拒收
        resume = _resume_argv(payload.session_id) if decision.resume else None
        sent = transport.report(herdr_env, decision.state, transport.next_seq(),
                                message=decision.message,
                                session_id=payload.session_id if decision.attach_session else None,
                                resume_argv=resume)
        state.root_session = decision.new_root_session or state.root_session
        state.blocked_by = decision.blocked_by
        state.last_reported = decision.state if sent else state.last_reported

    store.update(pane_id, step)
    return 0


def main(argv: list, stdin: IO, env: Mapping[str, str],
         proc_root: Optional[Path] = None) -> int:
    try:
        return run_once(argv[0] if argv else "", stdin.read(), env, proc_root=proc_root)
    except Exception:  # noqa: BLE001 - 任何异常都不得影响 mcode
        return 0
