"""纯函数状态机：输入（事件、载荷、上一状态），输出（要不要报、报什么）。

设计要点：
- 子代理有独立 session_id，因此「session_id != root_session」就是可靠判据。
- 但 blocked 是例外：子代理调 ask_user 时人确实被卡住了，必须上报，
  并记下 blocked_by，让只有提问者自己的 PostToolUse 能清掉它。
- SessionStart 在 pane 处于 working 时到达 → 必定是子代理创建，忽略。
- 与上次相同的状态不上报，减少噪声（herdr 侧通知本身也按跃迁去重）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .payload import Payload
from .store import PaneState

ASK_USER = "ask_user"


@dataclass(frozen=True)
class Action:
    name: str


@dataclass(frozen=True)
class Decision:
    class Kind(Enum):
        REPORT = "report"
        RELEASE = "release"

    kind: "Decision.Kind"
    state: str = ""
    message: str = ""
    attach_session: bool = False
    resume: bool = False
    blocked_by: Optional[str] = None
    new_root_session: Optional[str] = None


def decide(action: Action, payload: Payload, state: PaneState) -> Optional[Decision]:
    name, root = action.name, state.root_session

    if name == "session-start":
        sid = payload.session_id
        if not sid:
            return None
        if state.last_reported == "working":
            return None  # 父 agent 正在干活时新建的会话，只可能是子代理
        return Decision(
            kind=Decision.Kind.REPORT,
            state="idle",
            attach_session=True,
            resume=True,
            new_root_session=sid,
        )

    if name == "user-prompt":
        if not root or payload.session_id != root:
            return None
        return _report("working", state, clear_blocked=True)

    if name == "pre-tool":
        if payload.tool_name != ASK_USER:
            return None
        sid = payload.session_id
        if not sid:
            return None
        return Decision(
            kind=Decision.Kind.REPORT,
            state="blocked",
            message="等待你的决策",
            blocked_by=sid,
        )

    if name == "post-tool":
        if payload.tool_name != ASK_USER:
            return None
        if not state.blocked_by or payload.session_id != state.blocked_by:
            return None
        return _report("working", state, clear_blocked=True)

    if name == "stop":
        if not root or payload.session_id != root:
            return None  # 子代理的 Stop 绝不能把 pane 翻成 idle
        return _report("idle", state, clear_blocked=True)

    if name == "session-end":
        if not root or payload.session_id != root:
            return None  # 结束的不是当前会话，release 会误清新会话的状态
        return Decision(kind=Decision.Kind.RELEASE)

    return None


def _report(target: str, state: PaneState, clear_blocked: bool = False) -> Optional[Decision]:
    if state.last_reported == target:
        return None
    return Decision(
        kind=Decision.Kind.REPORT,
        state=target,
        blocked_by=None if clear_blocked else state.blocked_by,
    )

