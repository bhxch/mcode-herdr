"""工具名的唯一存放处：plugin/hooks/hooks.json 的 matcher。

背景：mcode 0.6.3 里会把一个真人卡住、让 turn 停下等回答的工具不止 `ask_user`
一个。全量订阅钩子跑真实会话时抓到两个漏网的：

| 工具 | 来源 | 返回 |
| --- | --- | --- |
| `ask_user` | packages/agent-tools/src/desktop/local-ask-user.ts:46 | `details.waiting_for_user: true` + `terminate: true` |
| `ExitPlanMode` | packages/agent-extension/src/plan-mode.ts:249-255 | 同上（"awaiting the local user's decision. This turn ends now"）|
| `request_feature_enable` | packages/agent-tools/src/desktop/local-feature-enable.ts:37-46 | 同上（"Stop this turn until the user replies."）|

只有 `ask_user` 挂在 matcher 上时，计划模式批准或功能开关弹出来，人是真的被卡住了，
pane 却一直显示 working —— 没有报错、没有日志，只有面板在骗人。这个测试文件存在的
理由就是把「哪些工具会阻塞人」钉死在唯一那处配置里；对应的「状态机不许再认一遍名单」
由 `test_decide.py::test_state_machine_does_not_know_the_blocking_tool_names` 把关。
"""
import json
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "hooks.json"

# mcode 0.6.3 会阻塞真人的工具。改这个集合时必须重新实测，别只读源码。
BLOCKING_TOOLS = frozenset({"ask_user", "ExitPlanMode", "request_feature_enable"})


def matcher(event):
    with HOOKS.open() as fh:
        hooks = json.load(fh)["hooks"]
    return [entry["matcher"] for entry in hooks[event]]


class HooksConfigTest(unittest.TestCase):
    def test_tool_hooks_cover_every_blocking_tool(self):
        """PreToolUse / PostToolUse 的 matcher 必须覆盖全部三个阻塞工具。

        `|` 分隔是 mcode 支持的写法：MINIMAX 源格式下 pattern 会按 `|` 或 `,` 切开，
        每一段都要满足 `^[A-Za-z0-9_.:/-]+$`，然后做**精确、区分大小写**的成员判断
        （runner.ts:1809-1824）。所以这既不是 glob 也不是正则，写错会静默失配。
        漏掉任何一个工具的后果是静默的：人真被卡住了，pane 却一直显示 working。
        """
        for event in ("PreToolUse", "PostToolUse"):
            with self.subTest(event=event):
                matchers = matcher(event)
                self.assertEqual(len(matchers), 1, "工具钩子应当只有一条 matcher")
                parts = set(matchers[0].split("|"))
                self.assertEqual(parts, BLOCKING_TOOLS)

    def test_tool_hook_matcher_parts_are_exact_names(self):
        """每一段都必须落在 mcode 的精确匹配字符集里，否则会退化成正则分支。

        含 `*`、`(`、`$` 之类的字符会切到 runner.ts 的正则分支，语义从
        「精确成员」变成「模式」—— 那是个只配对一部分工具的静默失配。
        """
        for event in ("PreToolUse", "PostToolUse"):
            for name in matcher(event)[0].split("|"):
                with self.subTest(name=name):
                    self.assertRegex(name, r"^[A-Za-z0-9_.:/-]+$")

    def test_tool_hooks_still_point_at_the_report_entrypoint(self):
        # 改 matcher 时最容易顺手把 command 改坏，这条顺手钉住
        with HOOKS.open() as fh:
            hooks = json.load(fh)["hooks"]
        for event, action in (("PreToolUse", "pre-tool"), ("PostToolUse", "post-tool")):
            with self.subTest(event=event):
                commands = hooks[event][0]["hooks"]
                self.assertEqual(len(commands), 1)
                self.assertIn(f"herdr-report.py\" {action}", commands[0]["command"])

    def test_non_tool_hooks_are_unmatched(self):
        # 其余四个事件没有 matcher：加上去等于给每次调用都 fork 一个进程
        with HOOKS.open() as fh:
            hooks = json.load(fh)["hooks"]
        for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            with self.subTest(event=event):
                self.assertNotIn("matcher", hooks[event][0])


if __name__ == "__main__":
    unittest.main()
