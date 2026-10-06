"""端到端回放：喂真实载荷，断言上报序列与参数。"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from mcode_herdr.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
HOOK = ROOT / "plugin" / "scripts" / "herdr-report.py"


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
    # 返回 CompletedProcess：调用方要断言 returncode 与 stdout，光看返回值会把它们丢掉
    return subprocess.run([sys.executable, "-c", script, action],
                          input=payload_text, env=env2, capture_output=True, text=True, timeout=30)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.log = self.data / "herdr-calls.jsonl"
        self.fake = self.data / "herdr"
        (self.data / "fake-proc").mkdir()
        self.set_herdr_exit(0)
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

    def set_herdr_exit(self, code):
        """落盘一份给定退出码的假 herdr。

        退出码必须写进脚本：假 herdr 必须能失败，否则 transport.report 永远返回 True，
        「上报没被确认就不许推进 last_reported」这条分支就永远走不到。
        """
        self.fake.write_text(
            '#!/bin/sh\n'
            f'printf \'%s\\n\' "$*" >> "{self.log}"\n'
            f'exit {code}\n'
        )
        self.fake.chmod(0o755)

    def calls(self):
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line]

    def state_file(self):
        return json.loads(Store(self.data).path_for("w9:p9").read_text())

    def wait_for(self, predicate, what):
        """轮询到条件成立；后台 worker 是异步的，固定 sleep 会 flaky。"""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"{what} 在 10s 内始终没有发生")

    def test_not_in_herdr_env_does_nothing(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_")}
        proc = run("user-prompt", (FIXTURES / "stop.json").read_text(), env, self.data)
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
        # 只有认领那一刻该带会话 id：后续每次都带上会把子代理的 session id 也写进
        # herdr 的 pane 状态，resume 命令就会指向错的会话
        self.assertIn("--agent-session-id mvs_test_root", calls[0])
        self.assertNotIn("--agent-session-id", calls[1])
        self.assertNotIn("--agent-session-id", calls[2])

    def test_failed_report_does_not_advance_last_reported(self):
        """R7-C：last_reported 只在 herdr 确认收到之后才推进。"""
        self.set_herdr_exit(3)
        sid = "mvs_test_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": sid,
                                        "source": "startup"}), self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid}),
            self.env, self.data)
        # 两次上报都被拒，绝不能把 idle/working 记成「已报告」：decide() 按 last_reported
        # 去重，记上去等于让同一事件被永久压掉，pane 再没有触发点能和 herdr 重新对上
        self.assertIsNone(self.state_file()["last_reported"])
        # 所以同一个 user-prompt 必须被重试出第二次 CLI 调用，而不是被当成重复事件吞掉
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid}),
            self.env, self.data)
        working = [c for c in self.calls() if "--state working" in c]
        self.assertEqual(len(working), 2)

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

    def test_session_end_clears_state_so_next_session_is_adopted(self):
        """release 必须清状态，否则新会话永远接不上 pane。"""
        old, new = "mvs_root", "mvs_next"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": old}),
            self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": old}),
            self.env, self.data)
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": old}),
            self.env, self.data)
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": new}),
            self.env, self.data)
        # 残留的 last_reported="working" 会让 decide() 把 B 的 SessionStart 当成子代理直接忽略，
        # pane 就再也不会被 B 认领 —— 这是能直接被用户看见的故障
        self.assertIn(f"--agent-session-id {new}", self.calls()[-1])
        self.assertIn(f"-- mcode --session {new}", self.calls()[-1])
        state = self.state_file()
        self.assertEqual(state["root_session"], new)      # 不是残留的 old
        self.assertEqual(state["last_reported"], "idle")  # 是 B 自己的 idle，不是残留的 working

    def test_hook_detaches_worker_and_returns_immediately(self):
        """真起子进程跑钩子：herdr-report.py → worker.py 这段唯一的自动化覆盖。

        本文件其余用例都直接调 runtime.main，只测到 runtime 为止；钩子「读 stdin、
        派后台进程、立刻返回、不写任何字节」全部落在 runtime 之外，只能真跑才测得到。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = Path(tmp.name)
        log = data / "herdr-calls.jsonl"
        pgid_file = data / "shim-pgid.txt"
        fake = data / "herdr"
        # 先记调用再睡：transport.CLI_TIMEOUT 是 1s，超时会把 shim 杀掉，
        # 所以「记下调用」必须排在 sleep 前面；而 sleep 2 远大于 1s，
        # 钩子一旦改成等 worker，单次钩子的耗时就必然冲破下面的阈值。
        # 最后把自身 pgid 落盘：worker 不设 start_new_session 时 shim 会和钩子同组。
        fake.write_text(
            '#!/bin/sh\n'
            f'printf \'%s\\n\' "$*" >> "{log}"\n'
            f'cat /proc/$$/stat > "{pgid_file}"\n'
            'sleep 2\n'
            'exit 0\n'
        )
        fake.chmod(0o755)
        env = dict(self.env)
        env["HERDR_BIN_PATH"] = str(fake)
        env["HERDR_SOCKET_PATH"] = str(data / "nonexistent.sock")
        env["PLUGIN_DATA"] = str(data)

        payload = (FIXTURES / "pre_tool_subagent.json").read_text()
        # 选 ask_user 而不是 stop：ask_user 的上报不依赖先有 session-start（stop 需要
        # root_session 匹配），所以单次钩子调用就能产生一次上报，不必先等上一轮后台
        # worker 落完状态，也就避免了两次脱离调用之间的竞态
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(HOOK), "pre-tool"], input=payload, env=env,
                              capture_output=True, text=True, timeout=30)
        elapsed = time.monotonic() - started

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")   # PreToolUse 的 stdout 被运行时消费，一个字节都不能有
        self.assertEqual(proc.stderr, "")
        # 钩子只做「读 stdin + Popen」，实测 0.023~0.040s；1.0s 留了 25 倍余量给慢机器。
        # 阈值不往低压是有原因的：反过来看，阻塞实现必然等到 worker 撞满
        # transport.CLI_TIMEOUT（写死 1.0s）才返回，是 1.069~1.081s 的结构下界，
        # 1.0 卡在这条硬下界之上一点点，换成 0.5s 只会让「正常实现别误报」的余量变紧。
        self.assertLess(elapsed, 1.0)

        # 上报是后台异步做的，等它落日志而不是死等固定时长
        self.wait_for(lambda: log.exists() and "pane report-agent w9:p9" in log.read_text(),
                      "脱离出来的 worker 上报 herdr 调用")

        # 脱离进程组：shim 的 pgid 就是 worker 的 pgid，必须跟调用方（本进程）不同。
        # start_new_session 没了它就等于钩子同组，整套测试仍然全绿 —— 所以显式断言。
        # 文件是 cat 边写边建，所以要等到真能解析出 pgrp 为止，不能只看文件存不存在。
        def worker_pgid():
            try:
                return int(pgid_file.read_text().rsplit(")", 1)[1].split()[2])
            except (OSError, IndexError, ValueError):
                return None

        deadline = time.monotonic() + 10
        while worker_pgid() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        pgid = worker_pgid()
        self.assertIsNotNone(pgid, "shim 始终没能报出自己的进程组")
        self.assertNotEqual(pgid, os.getpgid(0))


if __name__ == "__main__":
    unittest.main()
