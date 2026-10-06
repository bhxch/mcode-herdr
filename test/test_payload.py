import unittest
from pathlib import Path

from mcode_herdr.payload import load_payload

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return load_payload((FIXTURES / name).read_text())


class PayloadTest(unittest.TestCase):
    def test_session_start_fields(self):
        p = fixture("session_start.json")
        self.assertEqual(p.event, "SessionStart")
        self.assertEqual(p.session_id, "mvs_d662a0f1f8014fd7ac52237ed3d75a47")
        self.assertEqual(p.source, "startup")
        self.assertIsNone(p.tool_name)
        self.assertIsNone(p.agent_id)

    def test_pre_tool_root_has_no_agent_id(self):
        p = fixture("pre_tool_root.json")
        self.assertEqual(p.tool_name, "bash")
        self.assertIsNone(p.agent_id)

    def test_subagent_tool_event_exposes_agent_id(self):
        p = fixture("pre_tool_subagent.json")
        self.assertEqual(p.tool_name, "ask_user")
        self.assertEqual(p.agent_id, "mvs_child_123")
        self.assertEqual(p.session_id, "mvs_child_123")

    def test_empty_agent_id_is_treated_as_absent(self):
        p = fixture("post_tool_root.json")
        self.assertIsNone(p.agent_id)

    def test_ask_user_post_tool_reports_waiting_for_user(self):
        """实测样本：ask_user 带 terminate + waiting_for_user 立刻返回，问卷还开着。"""
        p = fixture("post_tool_ask_user_waiting.json")
        self.assertEqual(p.tool_name, "ask_user")
        self.assertTrue(p.waiting_for_user)

    def test_post_tool_without_details_is_not_waiting(self):
        # post_tool_root.json 的 tool_response 里没有 details，等价于没有等待证据
        self.assertFalse(fixture("post_tool_root.json").waiting_for_user)

    def test_waiting_for_user_defaults_false_without_tool_response(self):
        p = load_payload('{"hook_event_name":"PostToolUse","tool_name":"ask_user"}')
        self.assertFalse(p.waiting_for_user)

    def test_waiting_for_user_tolerates_null_details(self):
        # details 为 null / 非对象都必须退化成 False，绝不能抛 —— 钩子抛异常等于整个插件静默
        for details in ("null", "[]", '"ask_1"'):
            with self.subTest(details=details):
                p = load_payload('{"hook_event_name":"PostToolUse","tool_name":"ask_user",'
                                 f'"tool_response":{{"details":{details},"terminate":true}}}}')
                self.assertFalse(p.waiting_for_user)

    def test_non_boolean_waiting_for_user_is_not_truthy(self):
        # 只认真布尔值：字符串 "true" 不算在等，否则任何脏载荷都能把 pane 永久钉在 blocked
        for value in ('"true"', "1", "null"):
            with self.subTest(value=value):
                p = load_payload('{"hook_event_name":"PostToolUse","tool_name":"ask_user",'
                                 f'"tool_response":{{"details":{{"waiting_for_user":{value}}}}}}}')
                self.assertFalse(p.waiting_for_user)

    def test_stop_fields(self):
        p = fixture("stop.json")
        self.assertEqual(p.event, "Stop")
        self.assertFalse(p.stop_hook_active)

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            load_payload("not json")

    def test_stop_hook_active_true_is_parsed(self):
        p = load_payload('{"hook_event_name":"Stop","stop_hook_active":true}')
        self.assertTrue(p.stop_hook_active)

    def test_non_object_json_raises(self):
        with self.assertRaises(ValueError):
            load_payload("[1,2]")

    def test_missing_hook_event_name_raises(self):
        with self.assertRaises(ValueError):
            load_payload('{"session_id":"mvs_x"}')

    def test_empty_stdin_raises(self):
        with self.assertRaises(ValueError):
            load_payload("")

    def test_text_values_are_stripped_and_blank_becomes_none(self):
        p = load_payload(
            '{"hook_event_name":"  PreToolUse  ","session_id":"  mvs_x  ","tool_name":"   "}'
        )
        self.assertEqual(p.event, "PreToolUse")
        self.assertEqual(p.session_id, "mvs_x")
        self.assertIsNone(p.tool_name)


if __name__ == "__main__":
    unittest.main()
