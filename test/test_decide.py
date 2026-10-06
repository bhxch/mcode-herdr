import unittest

from mcode_herdr.decide import Action, Decision, decide
from mcode_herdr.payload import Payload
from mcode_herdr.store import PaneState

ROOT = "sess-root"
CHILD = "sess-child"


def p(event, session_id=ROOT, tool_name=None, agent_id=None, source=None):
    return Payload(event=event, session_id=session_id, tool_name=tool_name,
                   agent_id=agent_id, agent_type=None, source=source,
                   cwd="/tmp", stop_hook_active=False)


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

    def test_ask_user_pretool_root_reports_blocked(self):
        d = decide(Action("pre-tool"), p("PreToolUse", tool_name="ask_user"), PaneState(root_session=ROOT))
        self.assertEqual(d.state, "blocked")
        self.assertEqual(d.blocked_by, ROOT)
        self.assertEqual(d.message, "等待你的决策")  # herdr 通知里给用户看的文案

    def test_ask_user_pretool_from_subagent_also_reports_blocked(self):
        # 人确实被问了，子代理提问同样必须报 blocked
        st = PaneState(root_session=ROOT, last_reported="working")
        d = decide(Action("pre-tool"), p("PreToolUse", session_id=CHILD, tool_name="ask_user", agent_id=CHILD), st)
        self.assertEqual(d.state, "blocked")
        self.assertEqual(d.blocked_by, CHILD)

    def test_ask_user_pretool_without_session_id_ignored(self):
        # 没有 session_id 就无从记 blocked_by，这个守卫必须挡住，不许上报 blocked
        self.assertIsNone(decide(Action("pre-tool"), p("PreToolUse", tool_name="ask_user", session_id=None), PaneState(root_session=ROOT)))

    def test_non_ask_user_pretool_ignored(self):
        self.assertIsNone(decide(Action("pre-tool"), p("PreToolUse", tool_name="bash"), PaneState(root_session=ROOT)))

    def test_posttool_clears_only_own_blocked(self):
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        d = decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)

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

    def test_stop_root_reports_idle_and_clears_blocked(self):
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        d = decide(Action("stop"), p("Stop"), st)
        self.assertEqual(d.state, "idle")
        self.assertIsNone(d.blocked_by)

    def test_stop_from_child_never_flips_to_idle(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("stop"), p("Stop", session_id=CHILD), st))

    def test_session_end_releases_only_current_root(self):
        d = decide(Action("session-end"), p("SessionEnd"), PaneState(root_session=ROOT))
        self.assertEqual(d.kind, Decision.Kind.RELEASE)

    def test_session_end_from_child_does_not_release(self):
        self.assertIsNone(decide(Action("session-end"), p("SessionEnd", session_id=CHILD), PaneState(root_session=ROOT)))

    def test_unchanged_state_is_deduped(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("user-prompt"), p("UserPromptSubmit"), st))

    def test_unknown_action_ignored(self):
        self.assertIsNone(decide(Action("precompact"), p("PreCompact"), PaneState(root_session=ROOT)))

