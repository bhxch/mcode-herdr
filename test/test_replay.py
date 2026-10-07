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

from mcode_herdr.env import OWNER_PID, OWNER_START, read_starttime
from mcode_herdr.store import PaneState, Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
HOOK = ROOT / "plugin" / "scripts" / "herdr-report.py"

# 祖先进程启动器（测试专用，落进临时目录再执行）。
# 祖先必须是真正 exec 出来的进程：/proc/<pid>/environ 读的是 exec 那一刻的环境块，
# 在测试进程里事后 os.environ["HERDR_ENV"]="1" 是写不进 /proc 的，
# 那样造出来的还是「钩子自己就带着 HERDR_*」这条本来就通得过的分支。
# 两种角色：
#   holder  —— 剥掉 HERDR_* 后跑真实钩子脚本，把结果写进 LAUNCHER_OUT；
#   spawner —— 派一个 holder 就立刻退出，holder 随即被 init 收养，父链被截断成
#              holder -> 1，上面再没有任何带 HERDR_* 的进程（模拟「不在 herdr 里」，
#              不依赖测试进程自己的环境是否干净）。
LAUNCHER = '''"""测试用祖先进程启动器，只在测试里跑。"""
import json
import os
import subprocess
import sys
import time


def ppid():
    with open("/proc/self/status", errors="replace") as fh:
        for line in fh:
            if line.startswith("PPid:"):
                return int(line.split()[1])
    return None


hook, event = sys.argv[1], sys.argv[2]

if os.environ.get("LAUNCHER_ROLE") == "spawner":
    child_env = dict(os.environ)
    child_env["LAUNCHER_ROLE"] = "holder"
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), hook, event],
                             env=child_env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 故意不等 holder，自己先退出把它交给 init；填上 returncode 只是为了不给
    # 析构时的 ResourceWarning 写 stderr，不产生任何等待
    child.returncode = 0
    sys.exit(0)

if os.environ.get("WAIT_FOR_INIT") == "1":
    deadline = time.monotonic() + 10
    while ppid() != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    if ppid() != 1:
        sys.stderr.write("holder was never reparented to init\\n")
        sys.exit(9)

# mcode 只给钩子进程白名单环境，HERDR_* 到不了子进程
child_env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
with open(os.environ["LAUNCHER_PAYLOAD"], "rb") as fh:
    payload = fh.read()
proc = subprocess.run([sys.executable, hook, event], input=payload, env=child_env,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
with open(os.environ["LAUNCHER_OUT"], "w") as fh:
    json.dump({"returncode": proc.returncode,
               "stdout": proc.stdout.decode("utf-8", "replace"),
               "stderr": proc.stderr.decode("utf-8", "replace")}, fh)
sys.exit(proc.returncode)
'''


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
        # 归属 = 本测试进程：生产里这一对键是 herdr-report.py 在派生前回溯到 mcode 后
        # 注入 worker 的，这里手工注入等价的形状。判活查的是真 /proc，所以必须是真 pid。
        self.owner_pid = str(os.getpid())
        self.owner_start = read_starttime(os.getpid(), Path("/proc"))
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w9:p9",
            "HERDR_BIN_PATH": str(self.fake),
            "HERDR_SOCKET_PATH": str(self.data / "nonexistent.sock"),
            OWNER_PID: self.owner_pid,
            OWNER_START: self.owner_start,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _dead_pid(self):
        """一个确实已经不在了的 pid：真起一个进程，等它被回收干净。"""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(Path("/proc/%d" % proc.pid).exists(),
                         "刚回收的 pid 已经被复用，换个时间点再跑")
        return str(proc.pid)

    def _live_pid(self):
        """另一个确实活着的 pid（连同它的启动指纹），用来冒充另一个 mcode。"""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return str(proc.pid), read_starttime(proc.pid, Path("/proc"))

    def _seed_state(self, **fields):
        store = Store(self.data)
        store.save("w9:p9", PaneState(**fields))

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

    def states(self):
        """按调用顺序抽出上报过的状态序列（socket 不可用时全部走 CLI）。"""
        return [c.split("--state ", 1)[1].split()[0]
                for c in self.calls() if "--state " in c]

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

    def test_ask_user_blocked_survives_post_tool_and_stop_until_answered(self):
        """P0 回归：按真机时序回放，blocked 必须一直挂到用户作答为止。

        真机（mcode 0.6.3，0.1s 轮询 pane 状态）测到的时序与结果：
          prompt → blocked（+3103ms，正确）→ 121ms 后被翻成 working（错）
          → 又 123ms 后自称 done（错），而问卷还开在 TUI 上等人回答。
        对应的钩子时序是：PreToolUse(ask_user) → PostToolUse(+33ms，带
        terminate=true / details.waiting_for_user=true) → Stop(+56ms，turn 结束)
        → 35.8s 后用户作答，重新触发 UserPromptSubmit（换了新的 turn_id）。

        所以 blocked 的解除条件只有一个：作答。它既不是 PostToolUse，也不是 Stop。
        """
        root = "mvs_9b41e0c7d5f84a2eb3c6d90f17a48b25"  # 与实测样本同一会话
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        run("pre-tool", json.dumps({"hook_event_name": "PreToolUse", "session_id": root,
                                    "tool_name": "ask_user",
                                    "tool_input": {"mode": "questionnaire"}}),
            self.env, self.data)
        self.assertIn("--state blocked", self.calls()[-1])
        reported_before = len(self.calls())

        # ask_user 提前收工：tool_response 带 terminate + waiting_for_user=true，问卷还开着
        run("post-tool", (FIXTURES / "post_tool_ask_user_waiting.json").read_text(),
            self.env, self.data)
        # turn 随即结束，人还杵在问卷前 —— 绝不能报 idle
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": root,
                                "stop_hook_active": False}), self.env, self.data)

        # 这两步一个字节都不许上报：121ms 的假 working 和随后的 done 正是原 bug
        self.assertEqual(len(self.calls()), reported_before)
        self.assertEqual(self.state_file()["blocked_by"], root)
        self.assertEqual(self.state_file()["last_reported"], "blocked")

        # 作答 = 一次新的 UserPromptSubmit，走的是清障分支
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        self.assertIn("--state working", self.calls()[-1])
        self.assertIsNone(self.state_file()["blocked_by"])

        # 恢复后的 turn 正常收尾
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": root,
                                "stop_hook_active": False}), self.env, self.data)
        self.assertEqual(self.states(), ["idle", "working", "blocked", "working", "idle"])

    def test_ask_user_from_subagent_stays_blocked_until_root_prompt(self):
        """子代理提问同理：blocked_by 记子会话，只能由根会话的 UserPromptSubmit 解开。

        人对着子代理弹出的问卷作答，pane 归谁管都不能变 —— 这条路径的正解是
        user-prompt 分支要求 payload.session_id == root_session，子会话自己的
        UserPromptSubmit 一律忽略，所以不可能被误清。
        """
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        run("pre-tool", (FIXTURES / "pre_tool_subagent.json").read_text(), self.env, self.data)
        self.assertIn("--state blocked", self.calls()[-1])
        reported_before = len(self.calls())

        # 子代理的问卷也开着：它自己的 PostToolUse（waiting_for_user）与随后的 Stop 都空转
        run("post-tool", json.dumps({
            "hook_event_name": "PostToolUse", "session_id": "mvs_child_123",
            "tool_name": "ask_user", "tool_input": {"mode": "questionnaire"},
            "tool_response": {"terminate": True,
                              "details": {"waiting_for_user": True}},
            "tool_use_id": "call_function_child_1"}), self.env, self.data)
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": root,
                                "stop_hook_active": False}), self.env, self.data)
        self.assertEqual(len(self.calls()), reported_before)
        self.assertEqual(self.state_file()["blocked_by"], "mvs_child_123")

        # 人作答：根会话续上新 turn → 解障
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        self.assertEqual(self.states(), ["idle", "working", "blocked", "working"])

    def test_plan_mode_approval_blocks_the_pane_and_survives_its_post_tool(self):
        """回归：计划模式批准也是「停住等一个人」，pane 必须显示 blocked 并保持住。

        旧 matcher 只挂了 ask_user，于是 ExitPlanMode / request_feature_enable
        这两条路（计划模式批准、功能开关）在真机上完全不触发钩子：人正对着
        批准卡片发呆，pane 却一直显示 working，既不通知也不解阻塞。三个工具名
        是在**已安装的 mcode 0.6.3 bundle** 里逐个确认的（见 fixtures/README.md），
        权威来源是 bundle 而不是源码树。

        判据不是工具名而是 tool_response.details.waiting_for_user，所以这里
        喂 ExitPlanMode 的载荷就能走通；后续 Stop 同样不许翻 idle。
        """
        root = "mvs_9b41e0c7d5f84a2eb3c6d90f17a48b25"  # 与实测样本同一会话
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        run("pre-tool", json.dumps({"hook_event_name": "PreToolUse", "session_id": root,
                                    "tool_name": "ExitPlanMode",
                                    "tool_input": {"plan": "## 实施方案"}}),
            self.env, self.data)
        self.assertIn("--state blocked", self.calls()[-1])
        self.assertEqual(self.state_file()["blocked_by"], root)
        reported_before = len(self.calls())

        # 计划模式同样带 terminate 提前收工：turn 结束，卡片还开在 TUI 上等人点批准
        run("post-tool", (FIXTURES / "post_tool_plan_mode_waiting.json").read_text(),
            self.env, self.data)
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": root,
                                "stop_hook_active": False}), self.env, self.data)
        self.assertEqual(len(self.calls()), reported_before)
        self.assertEqual(self.state_file()["last_reported"], "blocked")

        # 用户点了批准：新的 UserPromptSubmit 解障，之后正常收尾
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        self.assertIn("--state working", self.calls()[-1])
        self.assertIsNone(self.state_file()["blocked_by"])
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": root,
                                "stop_hook_active": False}), self.env, self.data)
        self.assertEqual(self.states(), ["idle", "working", "blocked", "working", "idle"])

    def test_session_end_releases_with_seq(self):
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root,
                                       "reason": "logout"}), self.env, self.data)
        self.assertIn("pane release-agent", self.calls()[-1])
        self.assertIn("--seq", self.calls()[-1])

    def test_session_end_clears_state_so_next_session_is_adopted(self):
        """logout 的 release 必须清状态，否则新会话永远接不上 pane。"""
        old, new = "mvs_root", "mvs_next"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": old}),
            self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": old}),
            self.env, self.data)
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": old,
                                       "reason": "logout"}), self.env, self.data)
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": new}),
            self.env, self.data)
        # 残留的 last_reported="working" 会让 decide() 把 B 的 SessionStart 当成子代理直接忽略，
        # pane 就再也不会被 B 认领 —— 这是能直接被用户看见的故障
        self.assertIn(f"--agent-session-id {new}", self.calls()[-1])
        self.assertIn(f"-- mcode --session {new}", self.calls()[-1])
        state = self.state_file()
        self.assertEqual(state["root_session"], new)      # 不是残留的 old
        self.assertEqual(state["last_reported"], "idle")  # 是 B 自己的 idle，不是残留的 working

    def test_idle_timeout_keeps_the_agent_but_logout_releases_it(self):
        """P0 回归：同为 SessionEnd，空闲超时绝不能把 agent 从 pane 上摘掉。

        herdr 的规矩是「只有用户真的退出才交还 pane；同进程内换了会话就报新会话而不是
        release」。mcode 的空闲定时器是 30 分钟，一轮对话结束半小时后它就会发
        SessionEnd，此时进程还在、提示符还杵在那儿。原来的「一切 SessionEnd 都 release」
        让 agent 无故消失 —— 而 herdr 自己的「进程没了就清 agent」安全网在这里根本不会
        触发（pane 的前台进程组里有活着的 mcode）。

        这里喂的是真正的**线上取值** other：mcode 的 compatibleSessionEndReason（runner.ts）
        在序列化前把内部的 idle_timeout 改写成 other。曾把空闲定时器从 30 分钟缩到 20 秒跑
        真机会话，抓到的真实 SessionEnd 就是 reason='other'，不是 'idle_timeout'。

        两个 reason 跑在同一份状态上，构成直接对照：other 必须既不调 release-agent 也不改
        状态，logout 必须调。注意 logout 的含义是**账号登出**而不是「进程退出了」
        （见 decide.py 的 REASON_LOGOUT 注释），它之所以仍然 release，是因为登出后的
        mcode 干不了活 —— 拿 herdr 那句「真的退出」来理解它会得出错误的理由。
        """
        root = "mvs_root"
        for action, extra in (("session-start", {"source": "startup"}),
                              ("user-prompt", {}),
                              ("stop", {"stop_hook_active": False})):
            run(action, json.dumps({"hook_event_name": {"session-start": "SessionStart",
                                                        "user-prompt": "UserPromptSubmit",
                                                        "stop": "Stop"}[action],
                                    "session_id": root, **extra}), self.env, self.data)
        self.assertIn("--state idle", self.calls()[-1])
        before = self.state_file()
        reported_before = len(self.calls())

        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root,
                                       "reason": "other"}), self.env, self.data)
        self.assertEqual(len(self.calls()), reported_before)
        self.assertNotIn("pane release-agent", "\n".join(self.calls()))
        # 状态必须原封不动：清掉 root_session 的话，用户回来敲的第一条 UserPromptSubmit
        # 会因为认不出会话被忽略，pane 要静默到某个无关的 SessionStart 为止
        self.assertEqual(self.state_file(), before)
        self.assertEqual(self.state_file()["root_session"], root)

        # 同一份状态，logout 就必须交还 pane —— 证明分流确实由 reason 驱动
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root,
                                       "reason": "logout"}), self.env, self.data)
        self.assertIn("pane release-agent", self.calls()[-1])
        self.assertIsNone(self.state_file()["root_session"])

    def test_session_end_without_reason_keeps_the_agent_registered(self):
        """早于 reason 字段的 mcode：非退出的事件不许 release。

        保守的代价有界：真退出时 herdr 自己的「agent 进程没了」安全网会在一两秒后收掉它。
        """
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        before = self.state_file()
        reported_before = len(self.calls())
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root}),
            self.env, self.data)
        self.assertNotIn("pane release-agent", "\n".join(self.calls()))
        self.assertEqual(len(self.calls()), reported_before)
        self.assertEqual(self.state_file(), before)

    def test_session_switch_clears_session_identity_without_releasing_pane(self):
        """线上取值 resume（原样 clear 同理）：清会话身份，但 pane 上的 agent 登记留着。

        mcode 换会话时前后是同一个活着的进程，所以归属判活救不了「last_reported 停在
        working」这个陷阱：新会话的 SessionStart 会被当成子代理吞掉，之后它的钩子全被
        忽略，pane 就再也接不上新会话了。

        reason 用 resume 而不是内部的 resume_other：compatibleSessionEndReason（runner.ts）
        在序列化前就把 resume_other 改写成 resume，钩子读到的是后者。
        """
        old, new = "mvs_root", "mvs_next"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": old}),
            self.env, self.data)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": old}),
            self.env, self.data)
        reported_before = len(self.calls())

        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": old,
                                       "reason": "resume"}), self.env, self.data)
        # 一个字节都不许上报：不 release（agent 还该在），也不报 idle（下一轮由新会话报）
        self.assertEqual(len(self.calls()), reported_before)
        self.assertNotIn("pane release-agent", "\n".join(self.calls()))
        state = self.state_file()
        self.assertIsNone(state["root_session"])
        self.assertIsNone(state["last_reported"])

        # 新会话必须认领得下来，且是靠它自己的 SessionStart 报出来的 idle
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": new}),
            self.env, self.data)
        self.assertIn(f"--agent-session-id {new}", self.calls()[-1])
        self.assertIn("--state idle", self.calls()[-1])
        self.assertEqual(self.state_file()["root_session"], new)
        self.assertEqual(self.state_file()["last_reported"], "idle")

    def test_reset_path_stamps_the_owner_too(self):
        """RESET 也是一次状态写入，漏盖归属就会让下一次读回把它当陈旧丢掉。"""
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        # 先把归属换成别人那台活着的 mcode：不换的话，状态里本来就写着当前归属，
        # 「reset 盖了归属」和「reset 没盖」在断言上长得一模一样，这条用例就没有牙齿
        other, other_start = self._live_pid()
        self._seed_state(root_session=root, blocked_by=root, last_reported="working",
                         owner_pid=other, owner_start=other_start)
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root,
                                       "reason": "clear"}), self.env, self.data)
        state = self.state_file()
        self.assertIsNone(state["root_session"])               # 业务字段确实清了
        self.assertIsNone(state["blocked_by"])
        self.assertEqual(state["owner_pid"], self.owner_pid)   # 归属盖成了当前这台
        self.assertEqual(state["owner_start"], self.owner_start)

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

    def hook_via_ancestor(self, event, payload, orphan=False):
        """经由「祖先带 HERDR_*、钩子自己不带」的祖先进程去跑真实钩子脚本。

        返回 (祖先进程的耗时, 钩子自身 returncode/stdout/stderr 的文件)。
        orphan=True 时祖先自己也不带 HERDR_*，且会被 init 收养 —— 整条父链上都没有
        herdr 环境，所以不依赖测试进程自己的环境是否干净。
        """
        launch = self.data / "launch"
        launch.mkdir(exist_ok=True)
        launcher = launch / "launcher.py"
        payload_file = launch / "payload.json"
        out_file = launch / "hook-result.json"
        launcher.write_text(LAUNCHER)
        payload_file.write_text(payload)
        launcher_env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "PLUGIN_DATA": str(self.data),
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "w9:p9",
            "HERDR_BIN_PATH": str(self.fake),
            "HERDR_SOCKET_PATH": str(self.data / "nonexistent.sock"),
            "LAUNCHER_PAYLOAD": str(payload_file),
            "LAUNCHER_OUT": str(out_file),
        }
        if orphan:
            launcher_env = {k: v for k, v in launcher_env.items()
                            if not k.startswith("HERDR_")}
            launcher_env["LAUNCHER_ROLE"] = "spawner"
            launcher_env["WAIT_FOR_INIT"] = "1"
        started = time.monotonic()
        # 祖先必须活到钩子跑完（生产里 mcode 一直活着），所以同步等它返回；
        # 真正异步的是钩子派生出去的 worker
        subprocess.run([sys.executable, str(launcher), str(HOOK), event],
                       env=launcher_env, capture_output=True, text=True, timeout=30)
        return time.monotonic() - started, out_file

    def test_hook_reports_when_herdr_env_only_lives_in_ancestor(self):
        """P0 回归：HERDR_* 只在祖先上时，钩子必须仍然上报。

        生产形态是 mcode 给钩子进程的是白名单环境（没有 HERDR_*），HERDR_* 只存在于
        mcode 自己及其祖先上，env.discover_herdr_env 沿 /proc 父链回溯就是为这条形态写的。
        而这条链只在钩子进程还活着的时候成立：钩子一退出，后台 worker 就被 init 收养，
        父链断在 pid<=1 的守卫上，worker 自己再也回溯不到 HERDR_*，run_once 静默返回 0，
        表现为「钩子一直在触发、状态文件一个都不写」。所以环境必须由父进程在派生前解析，
        再连同 child_env 一起交给 worker。

        本文件其余用例都把 HERDR_* 直接塞进被测进程自己的环境，走的是
        runtime._resolve_herdr_env 的第一条分支，根本碰不到回溯逻辑。
        """
        # shim 先记调用再睡：记调用必须排在 sleep 前面（否则会被 transport 的 1s 超时
        # 杀掉，日志一个字都留不下）；睡 2s 则让下面那条耗时断言有牙齿。
        self.fake.write_text(
            '#!/bin/sh\n'
            f'printf \'%s\\n\' "$*" >> "{self.log}"\n'
            'sleep 2\n'
            'exit 0\n'
        )
        self.fake.chmod(0o755)

        payload_text = (FIXTURES / "session_start.json").read_text()
        sid = json.loads(payload_text)["session_id"]
        elapsed, out_file = self.hook_via_ancestor("session-start", payload_text)

        self.wait_for(out_file.exists, "祖先进程里的钩子返回")
        result = json.loads(out_file.read_text())
        self.assertEqual(result["returncode"], 0, result["stderr"])
        self.assertEqual(result["stdout"], "")   # PreToolUse 的 stdout 被运行时消费
        self.assertEqual(result["stderr"], "")
        # shim 自己要睡满 2s，钩子却 1s 内就回来了 —— 说明真正调 herdr 的是脱离出去的
        # worker 而不是父进程。父进程一旦改成等 worker，这里必然超时。
        self.assertLess(elapsed, 1.0)

        self.wait_for(lambda: self.calls() and "pane report-agent w9:p9" in self.calls()[0],
                      "worker 用祖先进程的 herdr 环境完成上报")
        # 状态必须落在临时 PLUGIN_DATA 下。shim 睡 2s 会被 transport 的 1s 超时杀掉，
        # 所以 last_reported 推进不了属预期（那正是 test_failed_report_... 覆盖的语义），
        # 这里只断言 Store 确实被走过；状态是在那 1s 超时之后才落盘的，所以要等它。
        state_path = Store(self.data).path_for("w9:p9")
        self.wait_for(state_path.exists, "worker 把 pane 状态落到 PLUGIN_DATA")
        state = json.loads(state_path.read_text())
        self.assertEqual(state["root_session"], sid)
        # 归属必须也一起搬进 worker：它只有靠 HERDR_ 前缀的转发才到得了后台进程，
        # 丢了这对键，pane 状态的判活就退化成「永远无法证明活着」
        self.assertIsNotNone(state["owner_pid"])
        self.assertIsNotNone(state["owner_start"])

    def test_stuck_working_state_from_a_dead_mcode_does_not_swallow_the_new_session(self):
        """P0 回归：写状态的 mcode 已经死了，这份状态就当没写过。

        mcode 在一轮进行中被强杀，Stop 永远不会到达，last_reported 停在 working。
        之后同一 pane 里新起的 mcode，它的 SessionStart 会被「working ⇒ 必然是子代理」
        吞掉，root_session 永远建立不起来，后续钩子全部失效 —— pane 看起来永久忙碌，
        没有任何东西会自己恢复它（README §9.2 记的就是这个）。归属进程已死就是判据。
        """
        self._seed_state(root_session="mvs_dead", last_reported="working",
                         owner_pid=self._dead_pid(), owner_start="1")
        sid = "mvs_fresh"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": sid}),
            self.env, self.data)
        self.assertIn(f"--agent-session-id {sid}", self.calls()[-1])
        self.assertEqual(self.state_file()["root_session"], sid)
        # 认领之后这条会话自己的钩子必须真的生效，不能停在「认领得回来、之后全忽略」
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid}),
            self.env, self.data)
        self.assertIn("--state working", self.calls()[-1])

    def test_stuck_working_state_from_a_live_mcode_still_suppresses_subagents(self):
        """子代理抑制规则不能被这个修复反过来打掉：mcode 还活着，working 就是真的在干活。"""
        self._seed_state(root_session="mvs_root", last_reported="working",
                         owner_pid=self.owner_pid, owner_start=self.owner_start)
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": "mvs_child"}),
            self.env, self.data)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.state_file()["root_session"], "mvs_root")

    def test_recycled_owner_pid_does_not_keep_the_pane_stuck(self):
        # 归属 pid 还活着、却已经是另一个进程：只判 pid 存在与否的话，
        # 死掉的 mcode 会被当成一直活着，这个 pane 就再也解不开
        self._seed_state(root_session="mvs_dead", last_reported="working",
                         owner_pid=self.owner_pid, owner_start="0")
        sid = "mvs_fresh"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": sid}),
            self.env, self.data)
        self.assertIn(f"--agent-session-id {sid}", self.calls()[-1])

    def test_legacy_state_without_owner_is_adopted(self):
        """已发布版本写下的状态没有归属字段：证明不了活着，就不能继续当证据。

        恰恰是这类残留最需要被丢掉 —— 它们的 SessionStart 会被吞掉，于是永远等不到
        一次新写入，也就永远不会被新版本接管。
        """
        path = Store(self.data).path_for("w9:p9")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"root_session": "mvs_legacy",
                                    "blocked_by": None, "last_reported": "working"}))
        sid = "mvs_fresh"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": sid}),
            self.env, self.data)
        self.assertIn(f"--agent-session-id {sid}", self.calls()[-1])
        self.assertEqual(self.state_file()["owner_pid"], self.owner_pid)

    def test_every_save_path_stamps_the_owner_including_release(self):
        """两条真正改动状态的路径都要盖上归属：认领那次，和 release 清空那次。"""
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data)
        self.assertEqual(self.state_file()["owner_pid"], self.owner_pid)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data)
        self.assertEqual(self.state_file()["owner_start"], self.owner_start)
        # release 之前先把归属换成别人那台 mcode：不换的话，状态里本来就写着当前归属，
        # 「release 盖了归属」和「release 没盖」在断言上长得一模一样，这条用例就没有牙齿
        other, other_start = self._live_pid()
        self._seed_state(root_session=root, last_reported="working",
                         owner_pid=other, owner_start=other_start)
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root,
                                       "reason": "logout"}), self.env, self.data)
        state = self.state_file()
        self.assertIsNone(state["root_session"])               # release 确实清了业务字段
        self.assertIsNone(state["last_reported"])
        self.assertEqual(state["owner_pid"], self.owner_pid)   # 归属换成了交还 pane 的这台
        self.assertEqual(state["owner_start"], self.owner_start)

    def test_ignored_hook_does_not_steal_ownership(self):
        """归属只在决策真的落到状态上时才转移。

        子代理的钩子被忽略是常态；若空转的钩子也改归属，它等于把归属洗给「当前这个
        pid」，真正持有 working 的老 mcode 一死，状态反而因为新归属还活着而永远解不开。
        """
        other, other_start = self._live_pid()
        self._seed_state(root_session="mvs_root", last_reported="working",
                         owner_pid=self.owner_pid, owner_start=self.owner_start)
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": "mvs_child"}),
            dict(self.env, **{OWNER_PID: other, OWNER_START: other_start}), self.data)
        self.assertEqual(self.calls(), [])   # 子代理语义没变
        self.assertEqual(self.state_file()["owner_pid"], self.owner_pid)

    def test_hook_without_herdr_ancestry_reports_nothing(self):
        """祖先进程链上完全没有 HERDR_*：彻底静默，不上报、不建状态、不抛异常。

        连状态文件的锁文件都不该出现：不在 herdr 里就不该进 Store。
        """
        _, out_file = self.hook_via_ancestor("session-start",
                                             (FIXTURES / "session_start.json").read_text(),
                                             orphan=True)
        self.wait_for(out_file.exists, "祖先进程里的钩子返回")
        result = json.loads(out_file.read_text())
        self.assertEqual(result["returncode"], 0, result["stderr"])
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "")
        # 上报是后台异步做的，负向断言必须留 settle 时间（祖先无 HERDR_* 时钩子压根不派 worker）
        time.sleep(0.5)
        self.assertEqual(self.calls(), [])
        self.assertEqual(sorted(p.name for p in self.data.glob("*.json")), [])
        self.assertEqual(sorted(p.name for p in self.data.glob("*.lock")), [])


if __name__ == "__main__":
    unittest.main()
