"""端到端回放：喂真实载荷，断言上报序列与参数。"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def run(action, payload_text, env, plugin_data):
    script = textwrap.dedent(f"""
        import sys, os
        sys.path.insert(0, {str(ROOT / 'plugin' / 'scripts')!r})
        from mcode_herdr import runtime
        runtime.main([sys.argv[1]], sys.stdin, os.environ, proc_root=os.environ["FAKE_PROC_ROOT"])
    """)
    env2 = dict(env)
    env2["PLUGIN_DATA"] = str(plugin_data)
    env2["FAKE_PROC_ROOT"] = str(plugin_data / "fake-proc")
    subprocess.run([sys.executable, "-c", script, action],
                   input=payload_text, env=env2, capture_output=True, text=True, timeout=30)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.log = self.data / "herdr-calls.jsonl"
        self.fake = self.data / "herdr"
        self.fake.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{self.log}"\nexit 0\n')
        self.fake.chmod(0o755)
        (self.data / "fake-proc").mkdir()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w9:p9",
            "HERDR_BIN_PATH": str(self.fake),
            "HERDR_SOCKET_PATH": str(self.data / "nonexistent.sock"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def calls(self):
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line]

    def test_not_in_herdr_env_does_nothing(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_")}
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, os
                sys.path.insert(0, {str(ROOT / 'plugin' / 'scripts')!r})
                from mcode_herdr import runtime
                runtime.main([sys.argv[1]], sys.stdin, os.environ, proc_root=os.environ["FAKE_PROC_ROOT"])
            """), "user-prompt"],
            input=(FIXTURES / "stop.json").read_text(), env={**env, "FAKE_PROC_ROOT": str(self.data / "fake-proc")},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(proc.stdout, "")   # 绝不污染钩子 stdout

    def test_session_start_then_prompt_then_stop(self):
        sid = "mvs_test_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": sid,
                                         "source": "startup"}), self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid}),
            self.env, self.data)
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": sid,
                                "stop_hook_active": False}), self.env, self.data)
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        self.assertIn("pane report-agent w9:p9", calls[0])
        self.assertIn("--state idle", calls[0])
        self.assertIn("-- mcode --session mvs_test_root", calls[0])
        self.assertIn("--state working", calls[1])
        self.assertIn("--state idle", calls[2])

    def test_child_session_stop_never_reports_idle(self):
        root, child = "mvs_root", "mvs_child"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": child,
                                "stop_hook_active": False}), self.env, self.data)
        self.assertNotIn("--state idle", self.calls()[-1])
        self.assertEqual(len(self.calls()), 2)  # 子代理的 stop 被完全忽略

    def test_ask_user_blocked_then_post_clears(self):
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        run("pre-tool", (FIXTURES / "pre_tool_subagent.json").read_text(), self.env, self.data)
        self.assertIn("--state blocked", self.calls()[-1])
        run("post-tool", json.dumps({"hook_event_name": "PostToolUse", "session_id": "mvs_child_123",
                                     "tool_name": "ask_user", "tool_input": {},
                                     "tool_response": {}, "tool_use_id": "c1"}),
            self.env, self.data)
        self.assertIn("--state working", self.calls()[-1])

    def test_session_end_releases_with_seq(self):
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root}),
            self.env, self.data)
        self.assertIn("pane release-agent", self.calls()[-1])
        self.assertIn("--seq", self.calls()[-1])


if __name__ == "__main__":
    unittest.main()
