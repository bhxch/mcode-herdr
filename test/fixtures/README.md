# 钩子样本来源说明

本目录的 JSON 是 mcode 钩子 stdin 载荷的**唯一留档**：仓库不保存任何原始抓包。
因此下面逐个说明每个样本的来源，以及哪些字段是实测的、哪些是为测试构造的。
改动这些文件前请先读这里，避免把构造值误当成线上格式。

## 样本来源

| 文件 | 来源 | 实测 / 构造 |
| --- | --- | --- |
| `session_start.json` | 早期探索时真实抓到的 `SessionStart` 事件 | 实测，仅替换了 `cwd` 等本地路径类取值 |
| `pre_tool_root.json` | 早期探索时真实抓到的 `PreToolUse` 事件（根会话） | 实测，仅替换了 `transcript_path` |
| `stop.json` | 早期探索时真实抓到的 `Stop` 事件 | 实测，`last_assistant_message` 的内容为可读性改写 |
| `post_tool_ask_user_waiting.json` | mcode 0.6.3 真机会话里实时抓到的 `ask_user` `PostToolUse` 事件 | 实测，仅 `tool_input.steps` 的问题正文与部分标识符被改写（见下） |
| `post_tool_plan_mode_waiting.json` | 照着 mcode 0.6.3 **已安装 bundle**（`~/.minimax-code/releases/0.6.3/`）里 `ExitPlanMode` 的实际返回构造 | 构造（未实时抓包），字段形状取自 bundle，不是源码树 |
| `pre_tool_subagent.json` | 依据 mcode `plugin-hooks/src/runner.ts` 的 `subagentFields` 字段契约构造 | 构造，子代理工具事件的字段形状来自源码而非抓包 |
| `post_tool_root.json` | 仿照真实 `PostToolUse` 事件构造，用于验证空值归一 | 构造，`"agent_id": ""` 是防御性取值，非线上格式 |

## 构造细节与注意事项

- **transcript 路径是占位符。** `pre_tool_root.json` 的 `/tmp/x.compatible.jsonl`、
  `pre_tool_subagent.json` 的 `/tmp/y.compatible.jsonl`、`stop.json` 的
  `/tmp/z.compatible.jsonl` 都是占位符。真实抓包一律形如
  `/tmp/minimax-plugin-hooks/transcripts/<32 位十六进制>.compatible.jsonl`，
  参见 `session_start.json`。解析层不依赖 `transcript_path`，故未纳入 Payload 字段。
- **子代理标识符是合成值。** `mvs_child_123`、`turn_child`、`call_function_child_1`
  分别代替真实的子代理 session id、turn id 与 tool_use id。
- **`pre_tool_subagent.json` 的 `agent_id == session_id` 假设是成立的。**
  运行时解析 agent id 时会回退到子会话 id，所以子代理路径上两者本就相同。
- **`post_tool_root.json` 的 `"agent_id": ""` 不是线上格式。** 真实载荷中，
  不存在的键是直接省略而非置空串；且 `agent_id` 只在子代理路径上被填充。
  这里特意写入空串，用来覆盖"空值按缺失处理"这条归一规则，
  对应的测试是 `test_empty_agent_id_is_treated_as_absent`。
- **`stop.json` 的 `last_assistant_message` 内容已改写。** 保留该字段的意义只在于
  其存在性与布尔类型，文本本身无断言价值。
- **`post_tool_ask_user_waiting.json` 是这次抓包的关键证据。** 它记录的是真实时序：
  `ask_user` 工具调用在 `PreToolUse` 之后约 33ms 就带着 `terminate: true` 和
  `details.waiting_for_user: true` 返回，`Stop` 又在约 56ms 后到来 —— 也就是说
  **turn 已经结束，问卷却还开着**。摘录时只改写了 `tool_input.steps` 里的问题正文、
  以及 `tool_use_id` / `session_id` / `turn_id` 这几个无语义的标识符；
  `tool_response.content` 文本、`details` 全字段（含 `waiting_for_user`）和 `terminate`
  均为线上原值，对应测试是 `test_ask_user_post_tool_reports_waiting_for_user`。
  恢复 `blocked` 的真实信号不是这个 `PostToolUse`，而是用户作答时以新 turn
  重新发出的 `UserPromptSubmit`。
- **会阻塞真人的工具不止 `ask_user`。** 在**已安装的 mcode 0.6.3 bundle**
  （`~/.minimax-code/releases/0.6.3/lib/node_modules/@minimax-ai/code/chunks/`）里
  逐个确认过，三个工具返回的 `details.waiting_for_user` 全是 `true`、`terminate` 全是
  `true`，文案分别是 "is waiting for the local user. Stop this turn until the user
  replies." / "is awaiting the local user's decision. This turn ends now"：
  `ask_user`、`ExitPlanMode`（计划模式批准）、`request_feature_enable`（功能开关）。
  早先只挂了 `ask_user`，另外两个工具弹卡时 pane 一直显示 working —— 静默的错配。
  `post_tool_plan_mode_waiting.json` 记的就是 `ExitPlanMode` 这一路的形状：
  `details` 是 `waiting_for_user` + `requestId` + `planPath`（注意这里没有
  `request_id` / `schema_version` / `step_count`，与 `ask_user` 不同）。
  这三个名字的权威来源是 bundle，不是源码树 —— 只读 TypeScript 源码不足以断言
  线上工具名，源码里的名字可能被重写或走别的导出路径。

