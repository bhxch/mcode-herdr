"""纯函数状态机：输入（事件、载荷、上一状态），输出（要不要报、报什么）。

设计要点：
- 子代理有独立 session_id，因此「session_id != root_session」就是可靠判据。
- blocked 是例外：子代理调那类会把人卡住的工具时人确实被卡住了，必须上报，
  并记下 blocked_by，让只有提问者自己的 PostToolUse 能清掉它。
- **状态机不认识任何工具名。** 会阻塞真人的工具名单只写在
  plugin/hooks/hooks.json 的 matcher 里，pre-tool / post-tool 两条分支一律
  按「到达这里的工具已经过 matcher 筛选」处理。理由有二：PreToolUse 阶段
  还没有 tool_response，此刻根本无从知道这次调用会不会真的阻塞人；而把名单
  抄进状态机，就等于让「mcode 新增了一个阻塞工具」变成一次必须记得的改代码
  动作 —— 漏改的后果是静默的：人真被卡住了，pane 却一直显示 working。
  blocker 判据统一取 tool_response.details.waiting_for_user。
- 但「清掉它」的前提是问卷真的被答了。实测那类工具立刻就带着
  tool_response.terminate=true / details.waiting_for_user=true 返回（PreToolUse 后
  约 33ms），随后约 56ms 就来 Stop —— **turn 结束的时候问卷还开着**。
  所以 PostToolUse 只在 waiting_for_user 为假时才允许清障；
  Stop 同样不代表问题已解决：只要 pane 还记着 blocked_by，就不许翻 idle。
  真正的解障信号是用户作答时重新发出的 UserPromptSubmit（user-prompt 分支）。
- SessionStart 在 pane 处于 working 时到达 → 必定是子代理创建，忽略。
- SessionEnd 带顶层 payload.reason，且钩子读到的是**改写后**的线上取值，不是 mcode 内部的
  联合类型（映射见下方常量处的注释）。线上只有 logout 意味着进程真的退出（→ RELEASE）。
  线上 other 是内部 archive 与 idle_timeout 的合流值：空闲超时是「turn 早就结束、人走开了」，
  mcode 还杵在提示符前，一个字节都不许动；而它与 archive 在线上一模一样，分不出来，
  所以 other 只能走「不动」，绝不能当换会话。线上 clear / resume 是同一进程内换了会话：
  mcode 仍活着，只清会话身份（→ RESET），pane 上的 agent 登记留着，状态由接手的新会话的
  SessionStart 报。拿不出来或认不出来的 reason 一律按「不动」处理 —— 看不懂的信号绝不能当退出。
- 与上次相同的状态不上报，减少噪声（herdr 侧通知本身也按跃迁去重）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .payload import Payload
from .store import PaneState

# ---- SessionEnd 的 payload.reason：注意「内部取值」不等于「线上取值」 ----
#
# mcode 内部是五种取值的联合类型，但发到钩子之前会先过一遍
# compatibleSessionEndReason（mcode 0.6.3，packages/agent-modules/plugin-hooks/src/runner.ts:2189-2197，
# 在 runner.ts:1939 处于序列化前应用），映射是：
#
#   内部 logout       -> 线上 logout
#   内部 clear        -> 线上 clear
#   内部 resume_other -> 线上 resume
#   内部 archive      -> 线上 other
#   内部 idle_timeout -> 线上 other
#
# 这张表不是读源码推断的：把 mcode 的空闲定时器从 30 分钟缩到 20 秒跑了一次真机会话，
# 抓到的真实 SessionEnd 是 reason='other'（内部的 idle_timeout 从不上线）。
# 换句话说，在本文件里匹配 reason 时**只能**用右边一列；用左边那列去匹配等于写死代码。
REASON_LOGOUT = "logout"  # 唯一原样上线、且意味着进程真的退出的取值
# 换会话：mcode 进程还活着，只是 root 已经不是这一个了。clear 原样上线，resume 来自内部的
# resume_other。
#
# 刻意**不含** other：它是 archive 与 idle_timeout 的合流值，线上分不出二者，而空闲超时的
# 那一方必须完全不动（见 session-end 分支末尾）。把 other 归到 RESET 会以更隐蔽的形式复现
# 「agent 半小时后从面板上消失」那个故障 —— root_session 被抹掉后，用户回来敲的第一条
# UserPromptSubmit 会因为认不出会话而被忽略。
SESSION_SWITCH_REASONS = frozenset({"clear", "resume"})


@dataclass(frozen=True)
class Action:
    name: str


@dataclass(frozen=True)
class Decision:
    """状态机交给运行时的唯一契约。

    - kind 决定消费方式，三种：
      REPORT 按 state 改 pane 状态并上报；
      RELEASE 交还 pane（只有用户真的退出才用）；
      RESET 只清掉 pane 记住的会话身份（root_session / blocked_by / last_reported），
      **不**交还 pane、**不**上报 —— agent 登记留在原地，下一轮状态由接手的新会话的
      SessionStart 报出去（RELEASE 会让 pane 上凭空少掉一个还活着的 agent）。
    - state 只对 REPORT 有意义，RELEASE / RESET 时为空串。
    - attach_session / resume 只由 session-start 的「认领」路径置位：那是 pane
      第一次学到本次会话的 id，也只有那一刻能拿到恢复命令去 attach。
    - new_root_session 非空时，用它覆盖 store 里已存的 root_session。
    - 不变式：state != "blocked" 的 REPORT 一定带 blocked_by=None，消费方因此
      可以无条件信任 decision.blocked_by。
    """

    class Kind(Enum):
        REPORT = "report"
        RELEASE = "release"
        RESET = "reset"

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
            # 父 agent 正在干活时新建的会话，只可能是子代理。
            # 前提是「这份 working 一定来自一台活着的 mcode」，这个前提由 store 兜住：
            # 归属进程已经失活的状态在 load 时就被丢成空状态，走不到这里。
            return None
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
        # 刻意不看 tool_name：到达这里的工具已经过 hooks.json 的 matcher 筛选，
        # 名单就在那一个地方。而且 PreToolUse 阶段还没有 tool_response，此刻
        # 也无从判断这次调用到底会不会阻塞人 —— 那是 post-tool 的 waiting_for_user
        # 才回答得了的问题。这里报 blocked 是有界的多报：同一个工具随后到达的
        # PostToolUse（没有待答问卷）会把它清回 working。
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
        if not state.blocked_by or payload.session_id != state.blocked_by:
            return None
        if payload.waiting_for_user:
            # 问卷还开着，人还在等：这个 PostToolUse 只是工具提前收工，
            # 不是作答。原实现在这里翻 working，实测只让 blocked 存在了 121ms，
            # 紧接着 Stop 又把 pane 标成 done —— herdr agent wait --until blocked
            # 因此基本永远等不到。保持不动，一个字节都不上报。
            return None
        return _report("working", state, clear_blocked=True)

    if name == "stop":
        if not root or payload.session_id != root:
            return None  # 子代理的 Stop 绝不能把 pane 翻成 idle
        if state.blocked_by:
            # turn 结束 ≠ 问题解决：那类工具是带 terminate 提前收工的，
            # 此刻人还杵在问卷前面。翻 idle 等于对外宣称「任务完成」。
            # 解障交给 user-prompt：作答会重新触发 UserPromptSubmit。
            return None
        return _report("idle", state, clear_blocked=True)

    if name == "session-end":
        if not root or payload.session_id != root:
            # 结束的不是当前会话：任何清理（release 更狠，它还会交还 pane）都会误伤
            # 新会话已经建立起来的这份状态
            return None
        reason = payload.session_end_reason
        if reason == REASON_LOGOUT:
            return Decision(kind=Decision.Kind.RELEASE)
        if reason in SESSION_SWITCH_REASONS:
            return Decision(kind=Decision.Kind.RESET)
        # 线上 other：内部的 archive 与 idle_timeout 合流成它，线上分不出二者。
        # 空闲超时那一方必须一个字节都不动：turn 早就结束、人走开了，mcode 还活着、会话也没换。
        # 这里动状态反而有害：清掉 root_session 的话，用户回来敲的第一条 UserPromptSubmit
        # 会因为认不出会话被忽略，pane 要静默到某个无关的 SessionStart 为止。也正因为分不出
        # idle_timeout，other 才绝不能当换会话的 RESET 处理 —— 那会以更隐蔽的形式重犯同一个
        # 故障（agent 登记还留着，但下一轮状态永远报不出来）。
        #
        # reason 缺失（早于该字段的 mcode）、内部取值（archive / idle_timeout / resume_other
        # 不会上线）或认不出来时同样走这里：看不懂的信号绝不能当退出处理，否则任何非退出事件
        # 都会把 agent 从 pane 上摘掉。保守的代价有界 —— 真退出时 herdr 自己的「agent 进程没了」
        # 安全网会在一两秒后收掉它。
        return None

    return None


def _report(target: str, state: PaneState, clear_blocked: bool = False) -> Optional[Decision]:
    if state.last_reported == target:
        return None
    return Decision(
        kind=Decision.Kind.REPORT,
        state=target,
        blocked_by=None if clear_blocked else state.blocked_by,
    )

