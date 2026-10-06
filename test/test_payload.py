import json
import unittest
from pathlib import Path

from mcode_herdr.payload import Payload, load_payload

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

    def test_stop_fields(self):
        p = fixture("stop.json")
        self.assertEqual(p.event, "Stop")
        self.assertFalse(p.stop_hook_active)

    def test_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            load_payload("not json")


if __name__ == "__main__":
    unittest.main()
