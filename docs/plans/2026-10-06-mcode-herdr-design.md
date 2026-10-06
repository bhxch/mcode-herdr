# mcode × herdr 集成设计

日期：2026-10-06
状态：设计已确认，待实现

## 1. 目标与背景

让 mcode（MiniMax Code CLI）成为 herdr 的一等公民：在 herdr 侧边栏和 `herdr agent list`
里显示名称与 `idle` / `working` / `blocked` 状态，完成或需要决策时发通知，
支持 `herdr agent wait` 等自动化，并在 herdr server 重启后恢复同一会话。

herdr 没有内置 mcode 支持，且**无法由我们修改**——agent kind 列表编译在 herdr 二进制里。
因此采用 herdr 官方文档《Add Herdr support to your agent》给出的路线：
由 agent 自行上报状态与恢复命令，无需改动 herdr。

参考对象是 herdr 对 Claude Code 的集成（`herdr integration install claude`，
本机版本 v10）。

## 2. 关键调研结论

### 2.1 herdr 侧

| 事实 | 影响 |
|------|------|
| `herdr agent list` 只列出 `claude`（在 `w5:p1`），当前 mcode 会话（`w4:p3`）对 herdr **完全隐形** | 没有 kind，也没有屏幕清单检测。状态**必须**由 mcode 主动上报，无法沿用 Claude Code 那种「只报会话身份、状态靠屏幕检测」的轻量方案 |
| 本机 herdr 0.9.3，`HERDR_BIN_PATH=/home/iflow/.local/bin/herdr`，`HERDR_SOCKET_PATH` 已设置 | 当前环境就是 herdr，可直接开发与验证 |
| `herdr integration install <kind>` 支持 18 个 agent，**不含 mcode** | 走 agent 自上报路线 |
| Claude 集成脚本走 **Unix socket 直连**（`pane.report_agent_session`），非 CLI | 官方偏好 socket；文档推荐 CLI 是为跨平台。两者协议等价 |

Claude 集成脚本里三个值得抄的细节：

- **子代理过滤**：`is_subagent = bool(hook_input.get("agent_id"))`，子代理会话直接 `raise SystemExit(0)`，完全不参与 pane 状态。
- `seq` 用 `time.time_ns()`，`request_id` 追加 6 位随机数防撞。
- socket 连接超时 0.5s。

herdr 协议要点（来自官方文档）：

- 只在 `HERDR_ENV=1` 且 `HERDR_BIN_PATH`/`HERDR_PANE_ID`/`HERDR_SOCKET_PATH` 齐备时上报，其余情况什么都不做。
- `--seq` 必须随每次上报递增，herdr 丢弃不比上次大的报告，因此乱序与迟到天然无害。
- 恢复命令在 `--` 之后；首词必须是 `PATH` 上的命令名（不能是路径）；参数不含撇号与控制字符；≤64 个参数、≤8 KiB。
- **必须先 `report-agent` 占住 pane**，否则恢复命令会被 `resume_not_accepted` 拒绝。
- `blocked` 的语义是「需要用户决策」，通知与等待都依赖语义状态而非显示文本。

### 2.2 mcode 侧

| 事实 | 影响 |
|------|------|
| mcode 0.6.3，版本化安装（`releases/0.6.3` + `current` 指针），61MB node bundle | 改 bundle 会被 `mcode update` 冲掉，必须走插件 |
| 支持完整 Claude Code 钩子事件：`SessionStart` `UserPromptSubmit` `PreToolUse` `PostToolUse` `Stop` `SessionEnd` `PreCompact` `SubagentStop` `Notification` | 状态机所需事件全部具备 |
| 载荷含 `tool_name` / `tool_input` / `tool_response` | 可按工具名识别 `ask_user` |
| `mcode plugin add -m local`，本地市场目录 `~/.minimax/plugins` | 官方扩展点，用户可自行 enable/disable/remove |
| `mcode --session <id>` / `-c` / `--continue` | 可上报恢复命令；模型随会话存储恢复，无需额外 `--model` |
| lark 插件提供可用的 schema 样板：`.minimax-plugin/plugin.json` + `hooks/hooks.json` + `scripts/`，`${PLUGIN_ROOT}` 变量，`timeout` 字段 | 直接照抄结构 |

### 2.3 未确认项（见 §9）

子代理标记字段名不确定。bundle 中 `isSubagentSession(t){return this.subagents.has(t)}` 是内部方法而非载荷字段；
载荷里出现 `agent_name` / `subagent_type`，且存在把 `subagent_type` 改名成 `agent_name` 的转换逻辑。
因此**不依赖字段名做判断**。

### 2.4 阻塞项：CLI 侧本地插件市场不可用（2026-10-06 实测）

原方案依赖 `mcode plugin add -m local` 安装插件。实测结论：**在 mcode 0.6.3 CLI 上这条路走不通。**

已验证的事实：

1. `mcode plugin list -m local --available --json` 恒为 `{"installed":[],"available":[]}`。
   **对照实验**：把已验证可用的 lark 插件（来自官方 registry）完整复制进 `~/.minimax/plugins/lark`
   后依然扫不到。**排除清单格式问题**，是扫描本身不工作。
2. bundle 中存在 `mutateLocal(e,r){if(r==="install")throw new ai("LOCAL_PLUGIN_INSTALL_UNSUPPORTED")}`
   ——本地插件的 `install` 被显式拒绝。
3. `mcode plugin add` 的所有形式均失败：裸名、`@local`、绝对路径、相对路径、末尾斜杠。
4. `importGithubPlugin` 是 DesktopService 的 HTTP 接口（`/minimax-desktop/api/v1/plugins/github/import`），
   CLI 不可用，不构成替代通道。
5. 钩子**只能**来自插件：`hooks/hooks.json` 是 bundle 里唯一的钩子清单常量，
   `config.yaml` 无 hooks 段，也无用户级 hooks 文件。
6. 官方 registry 通道正常（18 个插件正常启用），lark 的 SessionStart 钩子在本会话确实触发过，
   说明**插件钩子机制本身是好的，只是本地插件进不来**。

另有两个扫描器约束（供后续参考）：目录型市场**拒绝符号链接**（`rejectSymlink:!0`），
且校验失败会被写入 `diagnostics` **静默跳过**，无 CLI 可见性。

**结论**：插件形态在此版本不可交付，需要改方案。详见 §11。

### 2.5 阻塞项已解决（2026-10-06，基于 `github/minimax-code` 源码）

查 `packages/local-runtime-v2/src/service/plugin-system/` 源码后定位到真正原因，并已实测验证。

**根因：`icon` 是必填字段。**

`plugin/package/minimax-reader.ts` 的 `readManifest()` **无条件**调用 `readManifestIcon(value.icon)`，
而该函数内部是 `requiredString(value, 'icon')` —— 缺失即 `MANIFEST_SCHEMA_INVALID`。
而 `readLocalPluginCandidate()` 把失败**写入 `diagnostics` 后静默跳过**，CLI 不暴露任何提示。
因为官方插件全部带 `icon`，缺字段的本地包看起来就像「市场整体失效」。

补上 `icon` 后立即生效：

```
[*] herdr-probe@local	enabled
```

另据 `docs/examples.md`：**本地插件不是通过 `plugin add` 安装的**（`mcode plugin add <name>@local`
是 Desktop 功能，CLI 不支持），而是把包放进 `dataDir/plugins/` 下的直接子目录；
**被发现的本地包直接就是 installed + enabled**，无需任何安装命令。
`mcode plugin marketplace list` 打印的目录即活动 profile 的目录。

### 2.6 实测载荷（`mcode exec` 捕获，`probe/scripts/dump.sh`）

```jsonc
// SessionStart
{"source":"startup","hook_event_name":"SessionStart","session_id":"mvs_d662...","transcript_path":"...","cwd":"...","model":"...","permission_mode":"auto"}
// UserPromptSubmit 额外含 prompt、turn_id
// Stop            额外含 stop_hook_active、last_assistant_message
```

字段名与 Claude Code 完全一致（`hook_event_name` / `session_id` / `cwd` / `model`）。
`runner.ts` 的事件必填字段表还确认了：`PreToolUse` 需 `tool_name`+`tool_input`+`tool_use_id`，
`PostToolUse` 额外需 `tool_response`，`SubagentStart`/`SubagentStop` 需 `agent_id`+`agent_type`。

### 2.7 第二个硬约束：钩子拿不到 `HERDR_*`

`agent-modules/plugin-hooks/src/runner.ts` 中钩子子进程的环境是：

```ts
spawn(cmd, args, { env: { ...safeHookEnvironment(), PLUGIN_ROOT: ..., ... } })
```

`safeHookEnvironment()` 是**严格白名单**：`PATH HOME LANG TERM SHELL USER TMPDIR TEMP TMP PATHEXT
SystemRoot ComSpec USERPROFILE HOMEDRIVE HOMEPATH APPDATA LOCALAPPDATA`。
`HERDR_*` 不在其中，探针实测确认钩子里全部为空。

**绕行方案（已验证可行）**：沿 `/proc` 父进程链回溯，找到第一个带 `HERDR_ENV=1` 的祖先即 mcode 进程。
探针实测 depth=1 就命中 `comm=minimax-code`，成功取回 `HERDR_ENV / HERDR_PANE_ID / HERDR_TAB_ID /
HERDR_WORKSPACE_ID / HERDR_SOCKET_PATH / HERDR_BIN_PATH` 全套。

注意：用 `/proc/<pid>/status` 的 `PPid:` 取父进程号，不要用 `/proc/<pid>/stat` 按位置解析——
`comm` 含空格或括号会错位（探针第一版就因此只回溯到 depth=0）。

该方案是 Linux 专属。非 Linux 上取不到 pane 上下文，按 herdr 约定「不在 herdr 里就什么都不做」，
退化为无操作即可——这恰好是正确的降级行为。

### 2.8 实测确认：matcher 可按工具名过滤

`runner.ts` 的 `matches()` 对 `PreToolUse`/`PostToolUse` 使用 `handler.matcher` 匹配工具名。
实测：给两个工具钩子加 `"matcher": "ask_user"` 后，执行 `bash` 工具时
两个钩子**完全不触发**（只捕获到 session-start / user-prompt / stop）。

**这改变了设计取舍**：原方案担心 `PreToolUse` 挂在每次工具调用上、开销太大，
改为「只为 `ask_user` 挂 matcher」后，钩子仅在真正需要上报 `blocked` 时才触发。
fire-and-forget 仍然保留，但不再是高频路径。

## 3. 交付形态

**mcode 本地插件**。不走 wrapper 脚本（拿不到 `blocked` 和可靠的 `session_id`），不改 bundle（升级即丢）。

仓库落位：`/share/rw/repo/tools/mcode-herdr`（独立 git 仓，与 `tools/` 下同级项目惯例一致）。

```
/share/rw/repo/tools/mcode-herdr/
  .minimax-plugin/plugin.json   # 清单，声明 hooks
  hooks/hooks.json              # 事件 → 脚本映射，显式传事件名
  scripts/herdr-report.sh       # 唯一上报脚本
  install.sh                    # 同步到本地市场并安装
  test/                         # 离线回放测试
  docs/plans/2026-10-06-mcode-herdr-design.md
```

安装方式：脚本同步到 `~/.minimax/plugins/herdr/`，再 `mcode plugin add herdr -m local`。
用**同步而非符号链接**——本地市场扫描器是否跟随 symlink 未验证，同步是确定可用的路径；代价是改完代码需重跑 `install.sh`。

## 4. 钩子与状态机

事件路由**不依赖载荷字段名**：`hooks.json` 给每个事件显式传参，脚本靠 `$1` 判定。

| 钩子 | 根会话动作 | 子代理动作 |
|------|-----------|-----------|
| `session-start` | pane 空闲或未持有 agent → 接受为新根；上报 `idle` + 会话身份 + 恢复命令 | pane 正 `working` → **忽略** |
| `user-prompt` | → `working` | 忽略 |
| `pre-tool` 且 `tool_name=ask_user` | → `blocked`，`--message` 说明在等决策 | → `blocked` |
| `post-tool` 且 `tool_name=ask_user` | → 回 `working` | 仅当 blocked 是自己置位的才回，否则忽略 |
| `stop` | → `idle` | **忽略** |
| `session-end` | `release-agent`（仅当结束的正是当前会话） | 忽略 |

`session-start` 必须先于恢复命令发送，否则 herdr 返回 `resume_not_accepted`。
`--source` 固定 `mcode`，`--agent` 固定 `mcode`（不与 herdr 已支持的 agent 重名）。

## 5. 子代理处理

**不按字段名判断，改按「pane 当前是否在干活」判断**，对任何字段命名都免疫：
子代理总是在父 agent 工作期间被创建。

两个关键取舍：

- **`blocked` 是例外**。子代理完全可能调 `ask_user`，此时 pane 确实卡在人身上，必须报 blocked。
  若按「子代理一律忽略」处理，会出现 herdr 侧显示 `working` 却收不到通知的错配。
- **`post-tool` 要认「谁置的位」**。状态文件记 `blocked_by`，子代理的 post 只能清自己置的 blocked，
  否则会误清根会话正等待的决策。

## 6. 上报协议：双通道

**优先 socket 直连，失败回落 CLI。**

- socket：`HERDR_SOCKET_PATH` + `pane.report_agent` / `pane.release_agent`，超时 0.5s，需 python3。
- CLI：`$HERDR_BIN_PATH pane report-agent` / `release-agent`，外层套 `timeout 1`，跨平台。

两者协议等价，`--seq` 语义一致。socket 不可用（非 Unix、无 socket 路径、无 python3）时自动回落。

## 7. 错误处理与降级

核心原则：**herdr 集成绝不能影响 mcode 的正常工作**。

- **不在 herdr 里就彻底消失**：`HERDR_ENV≠1` 或缺关键变量 → 第一行 `exit 0`，不做任何事。
- **静默降级**：socket 失败 → CLI；CLI 也失败 → 丢弃。不重试、不报错、**不写 stdout/stderr**
  （`PreToolUse` 的输出会被运行时消费，污染可能改变工具执行结果）。
- **后台化**：真正的工作用 `setsid` 脱离进程组，钩子立刻 `exit 0`，自带输出重定向到 `/dev/null`。
  钩子挂在内联路径上，必须近乎瞬时。
- **状态文件原子写**：`tmp + mv`；读到半个文件按「无根会话」处理，绝不阻塞。
- **乱序容忍**：`--seq` 用 `time.time_ns()`，herdr 侧负责丢弃过期报告，无需自建队列合并。
- **`resume_not_accepted` 不重试**，仅记录日志。

每 pane 一个状态文件，记录：`root_session_id`、`blocked_by`、`last_state`。

## 8. 测试策略

三层，各有明确通过标准：

### ① 离线回放测试（核心，不依赖 herdr 与 mcode）

假 herdr 二进制把 argv 逐条记进文件，喂构造好的 hook JSON。断言：

- 不在 herdr 环境时**零调用**
- 六个事件各自映射到正确的 state 与参数
- `session-end` 只在同会话时 release
- 子代理四条过滤规则逐条成立
- socket 失败时确实回落到 CLI

### ② 载荷探针（一次性，解决未知）

装一个只把 stdin 原样 dump 到文件的 `SessionStart` + `PreToolUse` 钩子，跑真 `mcode exec`（非交互），
拿到真实载荷确定 `session_id` / `tool_name` 的确切字段名，据此定容错解析。
**结论会回写 §4/§5 的解析逻辑，故排在实现之前。**

### ③ 端到端（隔离环境）

`herdr --session mcode-herdr-test` 起独立会话 → 跑 mcode →
`herdr agent list` 断言出现 `mcode` 且状态流转正确 →
`herdr session stop` 再 start，断言 pane 自动跑起恢复命令且会话 id 一致。

子代理专项：子 agent 调 `ask_user`，断言 blocked 正确、结束后不被误清。

全程不触碰用户当前会话。

## 9. 待验证假设

1. mcode 钩子 stdin 载荷中会话标识的确切字段名（`session_id` 还是 camelCase）。
2. `tool_name` 的确切字段名，以及子代理上下文中工具名是否被改写。
3. `Stop` 的真实触发时机：模型跑完即触发，还是会等后台任务结束。
   若过早触发会导致「仍有后台任务时误报 idle」。
4. 本地插件市场扫描器是否跟随符号链接（本设计用同步规避，但若跟随 symlink 则同步可省去）。
5. `mcode exec` 是否触发钩子；若不触发，探针需改用交互式或临时 TUI。

## 10. 实施步骤

1. 写载荷探针插件，跑 `mcode exec` 拿到真实载荷 → 确定字段名，回写解析逻辑。
2. 实现 `herdr-report.sh`（状态机 + 子代理过滤 + 双通道上报 + 状态文件）。
3. 写 `hooks.json` 与 `plugin.json`。
4. 写离线回放测试并跑绿。
5. 写 `install.sh`（同步 + `mcode plugin add -m local`）。
6. 端到端验证（含子代理专项）。
7. 写 README，提交。

提交遵循 Angular 规范，按步骤原子化提交。