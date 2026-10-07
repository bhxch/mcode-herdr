import unittest
from pathlib import Path

from mcode_herdr.decide import Action, Decision, decide
from mcode_herdr.payload import Payload
from mcode_herdr.store import PaneState

ROOT = "sess-root"
CHILD = "sess-child"

# 会把一个真人卡住的工具。名单**只**出现在 plugin/hooks/hooks.json 的 matcher 里；
# 这里照抄一份只为让用例读起来有名字，状态机本身不许认识它们（见 test_hooks_config.py）。
BLOCKING_TOOLS = ("ask_user", "ExitPlanMode", "request_feature_enable")
# 三个名字之外的合成工具名：mcode 下个版本新增一个会阻塞人的工具时，它长这样。
SYNTHETIC_TOOL = "some_future_blocking_prompt"


def p(event, session_id=ROOT, tool_name=None, agent_id=None, source=None,
      waiting_for_user=False, session_end_reason=None):
    return Payload(event=event, session_id=session_id, tool_name=tool_name,
                   agent_id=agent_id, agent_type=None, source=source,
                   cwd="/tmp", stop_hook_active=False,
                   waiting_for_user=waiting_for_user,
                   session_end_reason=session_end_reason)


class DecideTest(unittest.TestCase):
    def test_session_start_adopts_root_and_reports_idle(self):
        d = decide(Action("session-start"), p("SessionStart", source="startup"), PaneState())
        self.assertEqual(d.kind, Decision.Kind.REPORT)
        self.assertEqual(d.state, "idle")
        self.assertTrue(d.attach_session)
        self.assertTrue(d.resume)

    def test_session_start_without_session_id_ignored(self):
        # payload 会把缺失/空白 session_id 归一成 None，这条路径真实可达
        self.assertIsNone(decide(Action("session-start"), p("SessionStart", session_id=None), PaneState()))

    def test_session_start_while_working_is_subagent_and_ignored(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("session-start"), p("SessionStart", session_id=CHILD), st))

    def test_session_start_while_idle_adopts_new_root(self):
        st = PaneState(root_session=ROOT, last_reported="idle")
        d = decide(Action("session-start"), p("SessionStart", session_id="sess-new"), st)
        self.assertEqual(d.kind, Decision.Kind.REPORT)
        self.assertEqual(d.new_root_session, "sess-new")

    def test_user_prompt_root_reports_working(self):
        d = decide(Action("user-prompt"), p("UserPromptSubmit"), PaneState(root_session=ROOT))
        self.assertEqual(d.state, "working")

    def test_user_prompt_from_child_ignored(self):
        # last_reported 必须取 "idle"：守卫被删掉时无守卫版本会报 "working"，
        # 若这里取 "working" 就会被 _report 的去重吞掉，测试假绿、守卫失去覆盖。
        st = PaneState(root_session=ROOT, last_reported="idle")
        self.assertIsNone(decide(Action("user-prompt"), p("UserPromptSubmit", session_id=CHILD), st))

    def test_user_prompt_from_root_clears_pending_blocked_by_child(self):
        # 用户在父会话提了新问题，子代理待答的提问已被接管，不该再算有人被阻塞
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        d = decide(Action("user-prompt"), p("UserPromptSubmit"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)

    def test_every_blocking_tool_reports_blocked_at_pretool(self):
        """三个会卡住真人的工具在 PreToolUse 都必须报 blocked。

        名单只写在 plugin/hooks/hooks.json 的 matcher 里；状态机不认识任何一个名字，
        这里是「如果这些工具走了钩子，端到端会不会正确上报」的端点断言。
        """
        for tool in BLOCKING_TOOLS + (SYNTHETIC_TOOL,):
            with self.subTest(tool=tool):
                d = decide(Action("pre-tool"), p("PreToolUse", tool_name=tool),
                           PaneState(root_session=ROOT))
                self.assertEqual(d.state, "blocked")
                self.assertEqual(d.blocked_by, ROOT)
                self.assertEqual(d.message, "等待你的决策")  # herdr 通知里给用户看的文案

    def test_blocking_tool_pretool_from_subagent_also_reports_blocked(self):
        # 人确实被问了，子代理提问同样必须报 blocked
        st = PaneState(root_session=ROOT, last_reported="working")
        d = decide(Action("pre-tool"), p("PreToolUse", session_id=CHILD,
                                         tool_name="ask_user", agent_id=CHILD), st)
        self.assertEqual(d.state, "blocked")
        self.assertEqual(d.blocked_by, CHILD)

    def test_blocking_tool_pretool_without_session_id_ignored(self):
        # 没有 session_id 就无从记 blocked_by，这个守卫必须挡住，不许上报 blocked
        self.assertIsNone(decide(Action("pre-tool"), p("PreToolUse", tool_name="ask_user", session_id=None), PaneState(root_session=ROOT)))

    def test_pretool_does_not_inspect_the_tool_name(self):
        """结构性回归：PreToolUse 阶段状态机不许再按工具名分支。

        PreToolUse 还没有 tool_response，此刻无从知道这次调用到底会不会阻塞人；
        唯一能挑出「会阻塞的工具」的是 hooks.json 的 matcher。状态机若在这里
        再认一遍工具名，就得跟着 mcode 的工具清单改代码 —— 那正是本次要根除的
        那类「名单写在代码里、改一处漏一处」的故障。到达这里的每一个工具都被
        当成会阻塞人处理；万一 matcher 失配，多报的 blocked 会被同一个工具随后
        到达的 PostToolUse（没有待答问卷）清掉，代价有界。
        """
        for tool in ("bash", "read", "", None, "Ask_User"):
            with self.subTest(tool=tool):
                d = decide(Action("pre-tool"), p("PreToolUse", tool_name=tool),
                           PaneState(root_session=ROOT))
                self.assertEqual(d.state, "blocked")

    def test_posttool_clears_only_own_blocked(self):
        # waiting_for_user=False 才代表问卷确实被答掉了（作答会让工具带着
        # 已完成的 tool_response 真正返回，或直接以 UserPromptSubmit 续上新 turn）
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        d = decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)

    def test_posttool_keeps_blocked_for_every_blocking_tool_while_waiting(self):
        """P0 回归：带 waiting_for_user 立刻返回时，问卷还开着，绝不清障。

        实测时序 PostToolUse(+33ms) → Stop(+56ms)，turn 在人作答前就结束了。
        原实现在这里翻 working，blocked 只存在 121ms 就被抹掉。
        原 bug 还有一个更隐蔽的形状：另外两个阻塞工具（计划模式批准、功能开关）
        根本不在旧 matcher 里，PreToolUse 都不会触发，人被卡住时 pane 一直显示
        working。matcher 已覆盖三个（见 test_hooks_config.py），这里逐个钉住。
        last_reported 取 "blocked"：无守卫版本会报 "working"，不会被去重吞掉。
        """
        for tool in BLOCKING_TOOLS + (SYNTHETIC_TOOL,):
            with self.subTest(tool=tool):
                st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
                d = decide(Action("post-tool"),
                           p("PostToolUse", tool_name=tool, waiting_for_user=True), st)
                self.assertIsNone(d)

    def test_posttool_keeps_blocked_for_child_session_while_waiting(self):
        # 子代理提问同一条路：blocked_by 记的是子会话，守卫与等待态都要生效
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        d = decide(Action("post-tool"),
                   p("PostToolUse", session_id=CHILD, tool_name="ask_user",
                     agent_id=CHILD, waiting_for_user=True), st)
        self.assertIsNone(d)

    def test_posttool_from_other_session_cannot_clear_root_blocked(self):
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        self.assertIsNone(decide(Action("post-tool"), p("PostToolUse", session_id=CHILD, tool_name="ask_user"), st))

    def test_posttool_without_blocked_by_ignored(self):
        # 没人被阻塞时没有「清障」可言，post-tool 必须空转
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user"), st))
        # 第二个输入才能暴露守卫的第一段：session_id 也为空时，光看第二段
        # （session_id != blocked_by）会把 None==None 判成「就是提问者」而放行；
        # last_reported 取 "idle" 避免无守卫版本的 working 决策被去重吞掉
        st2 = PaneState(root_session=ROOT, last_reported="idle")
        self.assertIsNone(decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user", session_id=None), st2))

    def test_posttool_clears_for_a_matched_tool_without_a_pending_questionnaire(self):
        """匹配到的工具、但没有待答问卷 → 解除阻塞并回到 working。

        判据是 payload.waiting_for_user（工具自己说的「人还在等吗」），
        不是工具叫什么。问过计划模式批准后工具正常返回（waiting_for_user 为假）
        属于这一类。
        """
        for tool in BLOCKING_TOOLS + (SYNTHETIC_TOOL,):
            with self.subTest(tool=tool):
                st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
                d = decide(Action("post-tool"), p("PostToolUse", tool_name=tool), st)
                self.assertEqual(d.state, "working")
                self.assertIsNone(d.blocked_by)

    def test_state_machine_does_not_know_the_blocking_tool_names(self):
        """结构性回归：工具名只允许存在于 hooks.json 的 matcher 里。

        这是本次审计要根除的那一类故障的结构性防线：工具名一旦写进状态机，
        就得跟着 mcode 的工具清单改代码，改一处漏一处就是一次静默错配
        （人真被卡住了，pane 却一直显示 working，而且不会有任何报错）。
        """
        source = (Path(__file__).resolve().parent.parent
                  / "plugin" / "scripts" / "mcode_herdr" / "decide.py").read_text()
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for tool in BLOCKING_TOOLS:
            for quote in ('"', "'"):
                with self.subTest(tool=tool, quote=quote):
                    # 用 assertFalse 而不是 assertNotIn：后者失败时会把整个源码
                    # 打进测试输出，淹掉真正的失败原因
                    self.assertFalse(f"{quote}{tool}{quote}" in code,
                                     f"{tool} 以字面量出现在状态机里，工具名只该在 hooks.json")

    def test_stop_while_blocked_does_not_flip_to_idle(self):
        """P0 回归：turn 结束不等于问题解决。

        实测 ask_user 是带 terminate 提前收工的，Stop 到达时问卷还开着，
        翻 idle 会让 pane 对外宣称「任务完成」，orchestrator 直接误判收工。
        last_reported 取 "blocked"：无守卫版本会报 "idle"，不会被去重吞掉。
        """
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        self.assertIsNone(decide(Action("stop"), p("Stop"), st))

    def test_stop_root_reports_idle_when_not_blocked(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        d = decide(Action("stop"), p("Stop"), st)
        self.assertEqual(d.state, "idle")
        self.assertIsNone(d.blocked_by)

    def test_stop_is_deduped_when_already_idle(self):
        st = PaneState(root_session=ROOT, last_reported="idle")
        self.assertIsNone(decide(Action("stop"), p("Stop"), st))

    def test_blocked_recovers_to_working_via_user_prompt(self):
        """blocked 不会卡死：作答就是一次新的 UserPromptSubmit，必然回到 working。"""
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        d = decide(Action("user-prompt"), p("UserPromptSubmit"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)
        follow = decide(Action("stop"), p("Stop"),
                        PaneState(root_session=ROOT, last_reported="working"))
        self.assertEqual(follow.state, "idle")

    def test_stop_from_child_never_flips_to_idle(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("stop"), p("Stop", session_id=CHILD), st))

    def test_session_end_logout_releases_current_root(self):
        """唯一原样上线的 reason：logout 表示进程真的退了。

        线上 reason 取自 mcode 的 compatibleSessionEndReason（runner.ts）在序列化前的改写结果，
        五个内部取值里只有 logout 和 clear 原样通过；logout 这一个才意味着进程真的退出。
        """
        d = decide(Action("session-end"), p("SessionEnd", session_end_reason="logout"),
                   PaneState(root_session=ROOT))
        self.assertEqual(d.kind, Decision.Kind.RELEASE)

    def test_session_end_logout_from_child_does_not_release(self):
        """结束的不是当前会话，绝不许动 root 的状态。

        reason 必须取 logout：否则子会话的 SessionEnd 会先被 reason 分支放行，这条用例
        变成在测「子会话的 logout」，而 root 匹配守卫从此再没有覆盖。
        """
        self.assertIsNone(decide(Action("session-end"),
                                 p("SessionEnd", session_id=CHILD, session_end_reason="logout"),
                                 PaneState(root_session=ROOT)))

    def test_session_end_wire_other_is_a_complete_no_op(self):
        """P0 回归：空闲超时的**线上取值**是 'other'，它必须一个字节都不动。

        线上取值来自 mcode 的 compatibleSessionEndReason（runner.ts）：内部的 idle_timeout
        在序列化前就被改写成 other，所以钩子读到的 reason 是 'other'，不是 'idle_timeout'。
        曾把 mcode 的空闲定时器从 30 分钟缩到 20 秒跑真机会话，抓到的真实 SessionEnd 就是
        reason='other' —— 这是实测，不是读源码推断。

        这里要挡的是「把 other 当换会话处理」这种更隐蔽的回归：内部的 archive 与
        idle_timeout 在线上合流成同一个 other，状态机分不出二者，而空闲超时的那一方
        必须完全不动，所以 other 只能走 no-op。
        """
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="working")
        d = decide(Action("session-end"), p("SessionEnd", session_end_reason="other"), st)
        # 必须是 None：既不是 RESET（会抹掉 root_session，让用户回来敲的第一条
        # UserPromptSubmit 被忽略），也不是 RELEASE（agent 会从 pane 上凭空消失）
        self.assertIsNone(d)
        # decide 是纯函数，这里顺带钉住「输入状态不被就地改动」
        self.assertEqual(st.root_session, ROOT)
        self.assertEqual(st.blocked_by, ROOT)
        self.assertEqual(st.last_reported, "working")

    def test_session_end_internal_union_values_are_not_wire_values(self):
        """mcode 内部联合类型的三个取值永远不会上线，状态机必须把它们当认不出来处理。

        compatibleSessionEndReason（runner.ts）把 resume_other 改成 resume、把 archive 与
        idle_timeout 改成 other，只有 logout / clear 原样通过。钩子读到的是改写后的值，
        所以拿内部取值去匹配 reason 等于写了永不执行的分支 —— 之前的实现正是如此，
        换会话的 RESET 路径在线上从未被触发过。

        这条用例把「内部取值 ≠ 线上取值」钉死：哪天 mcode 若改成原样下发，本用例会失败，
        提醒重新实测而不是默默继续匹配失效的字符串。
        """
        for reason in ("archive", "idle_timeout", "resume_other"):
            with self.subTest(reason=reason):
                d = decide(Action("session-end"), p("SessionEnd", session_end_reason=reason),
                           PaneState(root_session=ROOT, last_reported="working"))
                self.assertIsNone(d)

    def test_session_end_session_switch_resets_without_releasing(self):
        """线上取值 clear / resume：进程还在，但会话已经换了。

        要清的是**会话身份**，不是 pane 上的 agent 登记：留着 last_reported="working" 的话，
        紧接着到来的新会话 SessionStart 会命中「working ⇒ 必然是子代理」被吞掉，新会话
        永远认领不了这个 pane，之后它的钩子全部被忽略。归属判活在这里救不了 —— 换会话的
        前后是**同一个**活着的进程。

        两个取值都是 compatibleSessionEndReason（runner.ts）改写后真正上线的字符串：
        clear 原样通过（用户执行 /clear），resume 来自内部的 resume_other（同一进程内从
        会话 A 切到 B）。archive 被改写成 other，故不在这一组里。
        """
        for reason in ("clear", "resume"):
            with self.subTest(reason=reason):
                st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="working")
                d = decide(Action("session-end"), p("SessionEnd", session_end_reason=reason), st)
                self.assertEqual(d.kind, Decision.Kind.RESET)
                self.assertNotEqual(d.kind, Decision.Kind.RELEASE)
                self.assertEqual(d.state, "")  # 不上报任何状态，交给新会话的 SessionStart

    def test_session_start_after_a_session_switch_is_not_swallowed(self):
        """换会话之后的新 SessionStart 必须真的认领得下来（上面那个 working 陷阱的正解）。"""
        st = PaneState(root_session=ROOT, last_reported="working")
        reset = decide(Action("session-end"), p("SessionEnd", session_end_reason="resume"), st)
        self.assertEqual(reset.kind, Decision.Kind.RESET)
        # 运行时按 RESET 清空这三个字段后，新会话的 SessionStart 走的就是这条路径
        st.root_session = None
        st.blocked_by = None
        st.last_reported = None
        d = decide(Action("session-start"), p("SessionStart", session_id="sess-new"), st)
        self.assertEqual(d.kind, Decision.Kind.REPORT)
        self.assertEqual(d.new_root_session, "sess-new")

    def test_session_end_without_reason_is_not_released(self):
        """早于 reason 字段的 mcode：不能一律当成「用户退出了」。

        那样的话，任何非退出事件都会把 agent 从 pane 上摘掉。保守代价有界：真退出时
        herdr 自己的「agent 进程没了」安全网会在一两秒后收掉它。
        """
        d = decide(Action("session-end"), p("SessionEnd"), PaneState(root_session=ROOT))
        self.assertIsNot(d.kind if d else None, Decision.Kind.RELEASE)

    def test_session_end_unknown_reason_is_not_released(self):
        """认不出来的 reason 一律不 release：看不懂的信号绝不能当退出处理。"""
        for reason in ("some_future_reason", "LOGOUT", "quitting"):
            with self.subTest(reason=reason):
                d = decide(Action("session-end"), p("SessionEnd", session_end_reason=reason),
                           PaneState(root_session=ROOT))
                self.assertIsNot(d.kind if d else None, Decision.Kind.RELEASE)

    def test_unchanged_state_is_deduped(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("user-prompt"), p("UserPromptSubmit"), st))

    def test_unknown_action_ignored(self):
        self.assertIsNone(decide(Action("precompact"), p("PreCompact"), PaneState(root_session=ROOT)))

