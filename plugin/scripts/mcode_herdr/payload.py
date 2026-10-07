"""把 mcode 钩子的 stdin JSON 解析成 Payload。

字段名来自实测（见设计文档 §2.6/§2.8），解析保持宽容：
缺失、空字符串、纯空白字符串一律归一为 None，非空字符串去掉首尾空白，
避免下游到处判空，也避免带空白的 id 被原样拼进 mcode 恢复命令。

布尔标记同理只认真布尔值：waiting_for_user 取自
tool_response.details.waiting_for_user，是「这个人还在等回答吗」的权威信号，
与工具叫什么无关 —— mcode 0.6.3 里 ask_user / ExitPlanMode / request_feature_enable
三个会阻塞人的工具全都带这个字段（实测 bundle：三者都是 waiting_for_user=true +
terminate=true），所以解析层与状态机都不需要认识工具名。
缺失、null 或任何非布尔取值一律归一为 False，绝不抛。

stop_hook_active 只解析不使用：它是 stopHookActive: state.active（coordinator.ts:425），
只有 Stop 钩子已经拦截过一次（用来防止钩子死循环）时才为真。本插件从不拦截 Stop，
所以它在线上恒为 false —— 是无用的死字段，不是漏掉的信号。留着它是因为它确实在线上。

只有 SessionEnd 带 reason，取自**顶层** payload.reason（不埋在 tool_response 里），
它是「这次结束到底发生了什么」的权威信号。走同一套 _text 宽容归一：非字符串、缺失、
纯空白一律 None，绝不抛。

注意这里收到的是**线上取值，不是 mcode 内部的联合类型**。内部
archive 与 idle_timeout 在上线前会被 compatibleSessionEndReason（runner.ts）
合并成 other，resume_other 被改写成 resume。所以线上的取值只有：
logout（含义是**账号登出**，不是进程退出）、clear、resume、other。想看内部语义得读
TS 源码，不能直接把那个 union 当线格式抄过来。
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
    # 解析但从不读取：本插件不拦截 Stop，所以它线上恒为 false（见模块 docstring）
    stop_hook_active: bool
    waiting_for_user: bool = False
    # SessionEnd 的顶层 payload.reason；非 SessionEnd 事件恒为 None，见模块 docstring
    session_end_reason: Optional[str] = None


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _waiting_for_user(data: dict) -> bool:
    """取 tool_response.details.waiting_for_user，逐层缺失/null 都退化成 False。

    逐层 isinstance 是为了容忍线上真实存在的几种形态：老版本工具根本不回
    tool_response、tool_response 里没有 details、details 为 null。任何一层不合预期
    都只意味着「没证据说还在等」，此时按 False 走原有的清障路径，不会静默卡死在 blocked。
    """
    response = data.get("tool_response")
    if not isinstance(response, dict):
        return False
    details = response.get("details")
    if not isinstance(details, dict):
        return False
    value = details.get("waiting_for_user")
    return value if isinstance(value, bool) else False


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
        waiting_for_user=_waiting_for_user(data),
        session_end_reason=_text(data.get("reason")),
    )
