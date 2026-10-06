"""把 mcode 钩子的 stdin JSON 解析成 Payload。

字段名来自实测（见设计文档 §2.6/§2.8），解析保持宽容：
缺失或空字符串一律归一为 None，避免下游到处判空。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Payload:
    event: str
    session_id: Optional[str]
    tool_name: Optional[str]
    agent_id: Optional[str]
    agent_type: Optional[str]
    source: Optional[str]
    cwd: Optional[str]
    stop_hook_active: bool


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    return None


def load_payload(raw: str) -> Payload:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"hook payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("hook payload must be a JSON object")
    event = _text(data.get("hook_event_name"))
    if event is None:
        raise ValueError("hook payload missing hook_event_name")
    return Payload(
        event=event,
        session_id=_text(data.get("session_id")),
        tool_name=_text(data.get("tool_name")),
        agent_id=_text(data.get("agent_id")),
        agent_type=_text(data.get("agent_type")),
        source=_text(data.get("source")),
        cwd=_text(data.get("cwd")),
        stop_hook_active=bool(data.get("stop_hook_active")),
    )
