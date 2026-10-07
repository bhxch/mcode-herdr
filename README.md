# mcode-herdr

**让 herdr 看见 mcode。**

herdr 的 agent 识别是一份编译在二进制里的白名单，里面没有 mcode。装这个插件之前，
herdr 对 mcode 是**完全盲的**：

```console
$ herdr agent list
{"id":"cli:agent:list","result":{"agents":[],"type":"agent_list"}}
```

- 侧边栏没有条目，mcode 跑完一轮或卡住等你决策时都不会通知你
- `herdr agent wait` / `herdr agent prompt` 这类自动化等不到、也够不着 mcode
- herdr server 重启后，pane 里正跑着的 mcode 会话拉不回来

herdr 二进制改不了（agent kind 列表编译在里面），所以走 herdr 官方文档
《Add Herdr support to your agent》给的路子：**由 agent 自行上报**。本插件就是那个上报方。
装上之后：

| | 装之前 | 装之后 |
| --- | --- | --- |
| `herdr agent list` | 空 | 有 `mcode` 行，状态 `idle` / `working` / `blocked` |
| 侧边栏 | 无条目 | 有条目；卡住等你决策时通知你 |
| `herdr agent wait` / `prompt` | 等不到 | 可以用来做自动化 |
| herdr server 重启后 | 会话无法拉回 | 恢复命令 `mcode --session <id>` |

- 实现：Python 3 标准库（**无第三方依赖**）+ POSIX shell 安装脚本
- 平台：**仅 Linux**（原因见[已知限制](#9-已知限制)）
- 版本基线：mcode 0.6.3、herdr 0.9.3、Python 3.14.7（本机实测）

---

## 1. 环境要求

| 要求 | 版本 | 不满足会怎样 |
| --- | --- | --- |
| 操作系统 | Linux | 插件退化为彻底无操作，见[§9.1](#91-仅-linux) |
| mcode | 0.6.3（开发于该版本） | 钩子事件名/载荷字段变化会失配 |
| herdr | 0.9.3 | 上报协议随版本变 |
| Python 3 | 3.14（本机 3.14.7） | 标准库即可，不需要 pip 装任何东西 |
| mcode 运行时 | 会话必须跑在 herdr pane 里 | 不在 herdr 里，插件按设计完全静默 |

先确认 mcode 的会话确实跑在 herdr 里：

```bash
echo "$HERDR_ENV"    # 期望输出 1
```

---

## 2. 安装

```bash
git clone https://github.com/<owner>/mcode-herdr.git mcode-herdr
cd mcode-herdr
./install.sh
```

（`<owner>` 换成实际仓库归属。）

`install.sh` 做这些事。安装时是唯一有人在场、能低成本发现静默失败的时刻，
所以每种失败都在这里给出**可操作的**结论，而不是留一个静默的 no-op 给以后：

1. 删不掉东西时拒绝执行：`DEST` 必须是绝对路径且末段恰好是 `mcode-herdr`
2. 若 `$DEST` 是**符号链接**则拒绝执行 —— 继续下去只会把你的链接悄悄换成真实目录，
   而 mcode 本来就不认符号链接目录（见[§2.2.1](#221-符号链接会被静默忽略最常见的失败方式)）
3. 把 `plugin/` 整目录**复制**（不是链接）到 `$MINIMAX_DATA_DIR/plugins/mcode-herdr/`
   （默认 `~/.minimax/plugins/mcode-herdr`）；先 `rm -rf` 旧目录再复制，并清掉 `__pycache__`
4. **自检安装产物**：四个清单路径里必须有一个**直接**位于 `$DEST` 下（见
   [§2.2.3](#223-目录层级差一层就没了)）；`hooks/hooks.json`、`scripts/herdr-report.py`、
   `icon.png` 必须真实存在。复制被截断或层级错了当场报错，不留到运行期才表现为
   「插件什么都不做」。通过之后才 `chmod +x scripts/*.py` —— 顺序反了会让 glob 落空的
   报错盖住真正原因
5. 用 `mcode plugin list -m local --available` 验证本地市场**发现了它，而且处于启用态**
   （优先读 `--json` 的 `enabled` 字段，表格的 `[*]`/`[-]` 标记兜底）。
   被禁用会**自动重新启用**并明确告诉你，见[§2.2.4](#224-disable-会跨重装残留)
6. 检查恢复命令 `mcode` 能否被 herdr 执行：先看**当前环境**的 PATH，
   再看 **herdr server 进程**的 PATH（那才是真正执行恢复命令的环境）；两处都找不到才警告，
   见[§7](#7-恢复命令依赖-mcode-在-herdr-server-的-path-上)
7. 提示你重启 mcode

用非默认 profile（换整个 data dir）：

```bash
MINIMAX_DATA_DIR=/path/to/other-profile ./install.sh
```

> `install.sh` 与 `uninstall.sh` 只认 `MINIMAX_DATA_DIR`，其次退到 `~/.minimax`。
> mcode 自己还会再退一档到 `MAVIS_DATA_DIR`（`packages/tui/src/runtime/data-dir.ts:8,27-37`）。
> 只设 `MAVIS_DATA_DIR` 而不设 `MINIMAX_DATA_DIR` 时，两个脚本会装到默认 profile 去 ——
> 要么两个都设，要么把 `MINIMAX_DATA_DIR` 也导出来。

> **装完必须重启 mcode 或开一个新会话才生效**（`install.sh` 最后一行也是这么提示的）：
> 已经跑着的会话不会补挂钩子。

### 2.1 怎么确认装对了

```bash
# 1) 本地市场已经发现了它
mcode plugin list -m local --available
```

输出里要有一行含 `[*]` 和 `mcode-herdr@local`。`--json` 形式下，文档里的
mcode-herdr 条目形如（实测）：

```json
{"name": "mcode-herdr", "installed": true, "enabled": true, "version": "0.1.0"}
```

**`installed` 与 `enabled` 都不需要你去做什么操作**——本地插件被发现即视为已安装已启用，
原因见[§2.2.2](#222-不要跑-mcode-plugin-add)。

唯一的例外是 `enabled`：只要有人跑过一次 `mcode plugin disable`，禁用名单就会
**跨重装残留**，而 mcode 不会为此提示任何东西。所以确认启用态不能只看「列表里有这一行」，
要看前缀是 `[*]` 还是 `[-]`——完整说明和修复命令见[§2.2.4](#224-disable-会跨重装残留)。
`./install.sh` 会替你检查这一项并在禁用时自动恢复。

```bash
# 2) 在 herdr pane 里重启 mcode，跑一轮（随便发一条提示），然后：
herdr agent list
```

出现 `mcode` 行、状态随你的操作在 `idle` / `working` / `blocked` 之间流转，就是装好了。
还是空的话直接看[§8 排障](#8-排障)。

### 2.2 四个静默失败陷阱

**下面四种装法不会报错，只会让你装了个寂寞。** 本节是全文最该先读的部分。

| # | 别这么做 | 实测结果 | 正确做法 |
| --- | --- | --- | --- |
| 1 | `ln -s <repo>/plugin ~/.minimax/plugins/mcode-herdr` | `mcode plugin list -m local --available` 返回**空**，一个字都不提示 | **复制**。`./install.sh` 就是复制；改完代码重跑它 |
| 2 | `mcode plugin add -m local mcode-herdr` | 报 `LOCAL_PLUGIN_INSTALL_UNSUPPORTED`，直接失败 | **不要跑**。被发现即 installed + enabled |
| 3 | 复制到 `~/.minimax/plugins/wrapper/plugin/...`（多套一层目录） | 扫描不到，**无任何诊断** | 插件目录必须**直接**含一份清单文件 |
| 4 | 跑过 `mcode plugin disable -m local mcode-herdr` 之后再重装 | 重装完仍是 `[-] disabled`，插件什么都不做 | `mcode plugin enable -m local mcode-herdr`（`./install.sh` 会自动替你做） |

#### 2.2.1 符号链接会被静默忽略（最常见的失败方式）

把 clone 下来的仓库用软链指过去，是「让插件跟着仓库一起更新」的最顺手做法。
**它不工作，而且没有任何地方会告诉你。**

- 本地市场扫描器直接跳过符号链接子项（`plugin/package/package-readers.ts:107,111`），
  规范化插件根目录时明确以 `rejectSymlink: true` 调用
- 实测把本仓库的 `plugin/` 软链进 `plugins/mcode-herdr` 后，`mcode plugin list`
  **整列为空**，退出码正常，无 stderr
- 卸载时这种根目录还会被拒绝删除（`LOCAL_DELETE_UNSAFE`）

所以更新代码的唯一方式是重跑 `./install.sh`（先删旧目录再复制，不留陈旧文件）。

#### 2.2.2 不要跑 `mcode plugin add`

**对本地插件，`install` 是硬编码不支持的**（`desktop-facade.ts:359-365` 直接抛异常），
实测 CLI 报 `LOCAL_PLUGIN_INSTALL_UNSUPPORTED`。
`enable` / `disable` / `remove` 对本地插件是可用的，只有 `install` 不行。

本地插件**根本没有安装记录**。唯一的本地持久化是一份禁用名单：
`isLocalPluginEnabled()` 判 `row === undefined` 就算启用
（`plugin/runtime/repository.ts:181-189`）—— 没人禁用过就是启用的；
`listInstalledPlugins` 的本地条目完全由目录扫描推出，
本地市场的投影把 `installExists` 硬编码为 `true`（`desktop-facade.ts:630`）。
换句话说，**插件躺在目录里就是装好了**，跑 `plugin add` 只会让你以为装失败了。

**这个模型值得单独记住，因为上面四个陷阱都由它推出。** 具体说：

- 没有注册表、没有安装记录，`installed` / `enabled` 都是**扫描当下**算出来的派生值。
  所以「复制目录」就等于「安装」，而**任何写在文件之外的东西都活不过一次 `rm -rf`**
- 唯一被持久化下来的是那份禁用名单，它存在
  `<dataDir>/v2/sqlite/runtime-state.sqlite` 的表 `local_runtime_plugin_local_disabled` 里，
  **以插件目录的 canonical_root 为键**——注意是**路径**，不是插件名，也不是文件内容
- 因此 `disable` 的效果**与目录里放的是什么完全无关**，重装清不掉它（见[§2.2.4](#224-disable-会跨重装残留)）
- 同理，因为键是路径，把插件**挪到别的目录再装一份**会得到一个全新的、启用的条目；
  旧的禁用记录仍留在库里指向老路径。所以「换个地方重装一下」能绕过 disable，
  是副作用而不是特性
- `enable` / `disable` / `remove` 对本地插件可用，只有 `add`（install）被硬编码不支持

#### 2.2.3 目录层级差一层就没了

一个目录要算插件，必须**直接**包含下面某一份清单文件：

```text
.minimax-plugin/plugin.json   ← 本插件用的就是这个
claude-plugin/plugin.json
.codex-plugin/plugin.json
```

它们的优先级从左到右；另外，在插件根部直接放一个 `plugin.json` 也算
（`plugin/package/package-readers.ts:146-162`）。

复制到 `plugins/wrapper/plugin/...` 这种多一层的地方，**实测**扫描结果就是「什么都没有」：
**没有任何诊断信息**，只有空列表。

#### 2.2.4 `disable` 会跨重装残留

**本插件唯一一个「文件是对的、装法是对的，插件却什么都不做」的坑。**

```bash
mcode plugin disable -m local mcode-herdr     # 之后列表里是 [-] disabled
./install.sh                                 # rm -rf + 重新复制，文件全新
mcode plugin list -m local --available       # 依旧是 [-] disabled
```

原因见[§2.2.2](#222-不要跑-mcode-plugin-add)：禁用名单是 SQLite 里一条
**按 canonical_root 记录**的行（`<dataDir>/v2/sqlite/runtime-state.sqlite`，
表 `local_runtime_plugin_local_disabled`），和目录里的文件内容毫无关系。
`install.sh` 的 `rm -rf` 删的是目录，碰不到数据库。

**修复命令**：

```bash
mcode plugin enable -m local mcode-herdr
```

（如果当初 disable 时走的是非默认 profile，这里也要带上同一个
`MINIMAX_DATA_DIR=...`，否则启用的是另一个 profile 下的条目。）

**`./install.sh` 会替你做这件事**：检测到 `enabled: false` 就自动执行上面的命令，
并在输出里用 `!!` 前缀明确告诉你「已重新启用」以及怎么撤销
（`mcode plugin disable -m local mcode-herdr`）。

这是**有意覆盖你的选择**：既然你主动跑了 `install.sh`，这个动作本身就强烈表达了
「让它能用」的意图；而 mcode 侧对被禁用的插件没有任何提示，把这个状态留给用户去猜
代价更高。自愈只影响这一条记录、可以用 `disable` 原样撤销，所以脚本不静默处理它，
而是每次都喊出来。如果你想让禁用就此生效，重跑一次 `mcode plugin disable` 即可。

### 2.3 关于「本地市场」本身

mcode 的插件市场只有两个源，都是硬编码的：

```text
official    registry
local       directory    <dataDir>/plugins      # 默认 ~/.minimax/plugins
```

- `mcode plugin marketplace` **只有 `list` 和 `upgrade`，没有 add**。
  也就是说，**没有「从 GitHub 加一个市场」这条路**
- 本地市场的位置是单一固定路径 `<dataDir>/plugins`，
  唯一能改的是**整体换 data dir**：`MINIMAX_DATA_DIR`（其次 `MAVIS_DATA_DIR`）
- 顺带一提：mcode 源码里确实有一个能 `git clone` / 下载 GitHub codeload 归档的导入器
  （`plugin/import/github-plugin-importer.ts`，落地见 `plugin-system.ts:545-548`、
  `package-storage.ts:508-544`），但它只挂在 Desktop facade 上
  （`desktop-facade.ts:240-297`），`packages/tui/src/cli/plugin-command.ts` 从不引用它。
  **CLI 里没有 `git+https://` 安装、没有 tarball URL 安装、没有对应 flag。**
  别去找了。

### 2.4 清单要求（顺手写插件时会踩）

`plugin/.minimax-plugin/plugin.json`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `schemaVersion` | ✅ | 必须严格等于 `1` |
| `name` | ✅ | 插件名 |
| `version` | ✅ | 严格 semver |
| `description` / `author` / `category` | ✅ | |
| `icon` | ✅ | **必填，且文件必须真实存在**。缺失 → 整包被静默跳过 |
| `exampleQueries` | ✅ | 数组，可以是空的 |
| `apps` / `mcpServers` / `skills` | ✅ | 数组，可以是空的 |
| `displayName` / `darkIcon` / `hooks` / `hostBindings` | ➖ | 可选 |

其他几条：

- 清单**不能有未知的顶层字段**，多一个就判非法
- 至少要声明一种可执行能力（`apps` / `mcpServers` / `skills` / `hooks` 之一），
  否则清单读取失败 —— 本插件靠 `hooks` 满足
- 清单按**拒绝 BOM 的 UTF-8** 读取
- 带 `hooks` 的插件会被复制进 `<dataDir>/v2/plugin-hook-cache` 下的
  只读内容寻址缓存

缺 `icon` 是最经典的一种失败：官方插件全都带 `icon`，所以缺字段的本地包看起来就像
「整个市场失效了」。详见[§8.1](#81-mcode-plugin-list--m-local---available-什么都看不到)。

---

## 3. 卸载

```bash
./uninstall.sh
```

它先对**当前所在 pane** 执行 `herdr pane release-agent`（否则 herdr 会继续把这个 pane
显示为有 agent），再**写一条本地禁用记录**，最后删掉安装目录。其他 pane 里的残留占用要自己
`herdr pane release-agent <PANE_ID> --source mcode-herdr --agent mcode --seq <N>` 清理。

禁用记录放在删除之前，是为了让**删不掉**的情况也安全：如果 `rm -rf` 因权限等原因失败，
`set -e` 会中止脚本，而留在原地的插件此时已经是 `disabled`，不会在你声明卸载之后继续上报：

```
卸载前:                  [*] mcode-herdr@local  enabled
卸载在 rm 处中断:         [-] mcode-herdr@local  disabled   ← 目录还在，但是禁用的
```

注意这条记录**不会**在卸载后长期存在——目录一旦删掉，mcode 会在下次扫描时自动清理这条孤立
记录（源码里的 `pruneMissingLocalPlugins`）。所以它只兜住「没删干净」这一种情况，
不构成卸载后的长期标记；重装是一次全新的启用。

release 的 `seq` 取自 `/proc/uptime` 换算，而不是 `date +%s`。原因见
[§9.5](#95-uninstallsh-的-release-seq-取自-procuptime)—— 这条约束对任何新增的
release 调用都成立。

注意卸载**不删**插件数据目录下的状态文件，原因见[§9.2](#92-异常退出后残留的-working)。

---

## 4. 工作原理

```text
mcode 生命周期钩子（6 个）
  │  argv[1] = 事件名，stdin = 载荷 JSON
  ▼
plugin/scripts/herdr-report.py ── 读 stdin → 回溯 HERDR_* → Popen(start_new_session=True) ──► worker.py（后台）
                                                                          │
                                                                          ▼
                                                          runtime.run_once()
                            env 取用 → payload 解析 → store.update(decide) → transport 上报
                                                                          │
                                                    Unix socket（优先） / herdr CLI（回落）
```

### 4.1 模块对照表

| 文件 | 职责 |
| --- | --- |
| `plugin/.minimax-plugin/plugin.json` | 插件清单 |
| `plugin/hooks/hooks.json` | 6 个 mcode 事件 → `scripts/herdr-report.py <event>` 的映射 |
| `plugin/scripts/herdr-report.py` | 钩子入口：读 stdin、**先回溯出 herdr 环境**、派生后台进程并把环境传给它、**不等**、立刻返回 |
| `plugin/scripts/worker.py` | 后台 worker，调 `runtime.main()` |
| `plugin/scripts/mcode_herdr/env.py` | 沿 `/proc` 父进程链回溯，找回被剥掉的 `HERDR_*` |
| `plugin/scripts/mcode_herdr/payload.py` | 解析 stdin JSON → `Payload`（宽容归一：空白/空串 → `None`） |
| `plugin/scripts/mcode_herdr/store.py` | 每 pane 一个状态文件，`flock` + 临时文件原子替换 |
| `plugin/scripts/mcode_herdr/decide.py` | 纯函数状态机：`(事件, 载荷, 上次状态)` → 决策 |
| `plugin/scripts/mcode_herdr/transport.py` | 双通道上报（socket → CLI）与 `seq` / 应答判定 |
| `plugin/scripts/mcode_herdr/herdr.py` | 协议常量与标签：`source="mcode-herdr"`、`agent="mcode"` |
| `plugin/scripts/mcode_herdr/runtime.py` | 把上面这些串成一次完整上报 |

### 4.2 六个钩子，工具钩子只挂会阻塞人的三个工具

`plugin/hooks/hooks.json` 注册了 6 个事件，每个都是
`python3 "${PLUGIN_ROOT}/scripts/herdr-report.py" <event>`，`timeout: 5`：

| mcode 事件 | 传给脚本的动作名 |
| --- | --- |
| `SessionStart` | `session-start` |
| `UserPromptSubmit` | `user-prompt` |
| `PreToolUse`（`"matcher": "ask_user\|ExitPlanMode\|request_feature_enable"`） | `pre-tool` |
| `PostToolUse`（同上） | `post-tool` |
| `Stop` | `stop` |
| `SessionEnd` | `session-end` |

事件名是**显式传参**的，脚本不靠载荷里的 `hook_event_name` 做路由，所以载荷字段改名不影响分发。

**会停住 turn、把人卡住的工具不止一个。** mcode 0.6.3 里有三个，在**已安装的 bundle**
（`~/.minimax-code/releases/0.6.3/`，不是源码树）里逐个确认过，返回的都是
`details.waiting_for_user: true` + `terminate: true`：

| 工具 | 什么时候用 | 它停下来等什么 |
| --- | --- | --- |
| `ask_user` | agent 要问用户 | 问卷（`packages/agent-tools/src/desktop/local-ask-user.ts:46`） |
| `ExitPlanMode` | 计划模式 | 对计划的批准（`packages/agent-extension/src/plan-mode.ts:249-255`） |
| `request_feature_enable` | 功能开关 | 开不开某个功能（`packages/agent-tools/src/desktop/local-feature-enable.ts:37-46`） |

**漏掉任何一个，失败方式都是静默的**：mcode 根本不触发钩子，人正对着批准卡片发呆，
pane 却一直显示 `working`，既不通知也等不到。早先只挂了 `ask_user`，计划模式和功能开关
这两条路一直没被覆盖。

mcode 的 `matches()`（`plugin-hooks/src/runner.ts:1809-1824`）对 MINIMAX 格式（本插件的格式）
的 matcher：pattern 只含 `[A-Za-z0-9_.:/-]` 时按 `|` 或 `,` 切开，再对每一段做
**精确、区分大小写的相等**比较；含其他字符才退化成正则。所以上面那个 `|` 串既不是 glob
也不是正则，写错（例如不小心加了 `*`）会静默只匹配上部分工具。

这条限制是有意为之：不给工具钩子加 matcher 的话，**每一次工具调用都会 fork 一个进程**。
加上之后，钩子只在真正匹配到的工具上才触发 —— 仍然是「每个**匹配到的**工具调用一个进程」，
不是「整个会话一个」。

**工具名只存在于这一处。** `decide.py` 刻意不认识它们：到达 `pre-tool` 的事件已经过
matcher 筛选，而 `PreToolUse` 阶段还没有 `tool_response`，本来就无从判断这次调用会不会
阻塞人；`post-tool` 则一律按 `tool_response.details.waiting_for_user` 判断。理由见
[§4.6](#46-状态机只在状态变化时上报)—— 名单一旦抄进状态机，「mcode 新增一个阻塞工具」
就变成一次必须记得的改代码动作，漏改的后果同样是静默的。

### 4.3 入口立即返回，上报交给脱离进程组的后台进程

`herdr-report.py` 只做四件事：读掉 stdin、**先把 herdr 环境回溯出来**、
`Popen` 派生 `worker.py`（`start_new_session=True`，stdout/stderr 接 `DEVNULL`）、
把载荷写进子进程 stdin，然后**直接返回，不 wait**。`herdr-report.py:60` 还特意给
`Popen` 填上 `returncode`，避免 GC 时 `__del__` 发 `ResourceWarning` 污染 stderr。

**耗时**：钩子入口约 0.05s 返回，上报在后台完成，与这个数字无关。
量法与量级见[§6 性能](#6-性能)。

钩子挂在内联路径上，**一个字节都不能往 stdout/stderr 写** —— `PreToolUse` 的输出会被
mcode 运行时消费，污染可能改变工具执行结果。回放测试对此有硬断言
（`assertEqual(proc.stdout, "")`）。

### 4.4 找回 `HERDR_*`：`/proc` 父进程链回溯，以及一个必须知道的 fork 陷阱

这是整个插件最硬的一个约束。mcode 派发钩子子进程时给的是一个**按名字逐个挑出来的
白名单环境**，只含：

```text
PATH HOME LANG TERM SHELL USER TMPDIR TEMP TMP PATHEXT
SystemRoot ComSpec USERPROFILE HOMEDRIVE HOMEPATH APPDATA LOCALAPPDATA
```

**`HERDR_*` 一个都不传**（探针实测：钩子里这些变量全空）。所以 `env.py` 只能自己去找：
从自己的 pid 出发，沿 `/proc/<pid>/status` 的 `PPid:` 逐级向上（最多 12 级），
找到第一个满足 `HERDR_ENV=1` 且 `HERDR_PANE_ID`、`HERDR_BIN_PATH` 都非空的祖先 ——
那个进程就是 herdr 拉起的 mcode，它的 `/proc/<pid>/environ` 里有全套 `HERDR_*`。

父进程号必须读 `status` 里的 `PPid:`，不能按位置解析 `/proc/<pid>/stat` ——
`comm` 含空格或括号会直接错位（探针第一版就踩了这个坑，只回溯到 depth=0）。

> **要 fork 这套设计的话，这是那个坑**：环境回溯**必须由钩子进程自己做，
> 并且必须在派生 worker 之前做完**。worker 一脱离，父进程就退出、worker 被 init 收养
> （`start_new_session` 只换会话组，不改父子关系），`/proc` 父链随之断在 `pid<=1` 的守卫上，
> worker 自己的回溯恒返回 `None`。
>
> 这个坑踩过：真机上量到 `worker MY_PPID = 1`、环境发现返回 `None`、钩子照常触发、
> **状态文件一个都不写**、`herdr agent list` 永远是空的。
>
> 所以 `herdr-report.py` 在父进程里就把结果取出来，连同 `child_env` 一起用 `env=` 交给 worker；
> 解析不到（不在 herdr 里）时连 worker 都不派生。

于是 `runtime._resolve_herdr_env` 有两级：

1. **先采信子进程环境里显式存在的 `HERDR_*`** —— 这是生产主路径。
   注意这里的「显式」**不是 mcode 传了 `HERDR_*`**，而是上一段里钩子入口自己回溯出来、
   用 `env=` 注入进去的。它同时覆盖「人在 herdr pane 里手工跑 `worker.py`」
   （这时你自己的 shell 里就有 `HERDR_*`）和回放测试注入假环境两种情况
2. **没有才回退到 `/proc` 回溯** —— 只在既没有显式 `HERDR_*`、
   又确实还在 mcode 进程树里时才命中

删掉第一条分支，整个插件会静默失效（`herdr agent list` 永远为空）。

### 4.5 状态：每个 pane 一个 JSON

`store.py` 在 mcode 注入的 `PLUGIN_DATA` 目录里，为每个 pane 存一个 JSON：

```json
{"root_session": "mvs_...", "blocked_by": "mvs_...", "last_reported": "working",
 "owner_pid": "1528266", "owner_start": "73529166"}
```

- **`owner_pid` / `owner_start`**：写下这份状态的 mcode 进程的 pid 与其启动时刻
  （`/proc/<pid>/stat` 第 22 字段）。回溯 `HERDR_ENV` 命中的那个祖先进程就是 mcode
  本身，pid 顺手就能拿到，不用额外开销。读状态时先核对归属进程是否还活着，不在就把
  整份状态当作不存在 —— 这是「mcode 崩在 turn 中途 → pane 永久卡死」的修复，
  见[§9.2](#92-异常退出后残留的-working)。pid 会被复用，所以必须连 `starttime` 一起比。
- **文件名**：pane id 里所有非 `[A-Za-z0-9._-]` 的字符换成 `_`，再加 `.json`。
  例如 pane `w4:p3` → `w4_p3.json`，同目录还有一个 `w4_p3.lock`（flock 锁文件）
- **并发安全**：所有读改写都在 `Store.update()` 的**单次持锁**内完成。
  注意 `load()`/`save()` 各自加锁，但两者合起来不是一个临界区 —— 需要
  「读最新值 → 决定 → 写回」整体原子时必须用 `update()`
- **不撕裂**：写入走临时文件 + `os.replace()` 原子替换，读者永远看到完整 JSON。
  文件权限 `0600`：状态里是要拼进 `mcode --session <id>` 的会话标识，不该同机可读
- **不用 `/tmp` 兜底**：拿不到 `PLUGIN_DATA` 就直接什么都不做。状态文件名由 pane id 派生、
  可预测，而临时文件用的是 `os.open(O_CREAT)`，它会跟随预置的符号链接 ——
  在全局可写的 `/tmp` 里等于把状态 JSON 写进攻击者指定的文件

### 4.6 状态机：只在状态变化时上报

`decide.py` 是纯函数，签名 `decide(action, payload, state) -> Decision | None`，
输入是（事件、载荷、上次状态），输出是「报不报、报什么」：

| 动作 | 条件 | 结果 |
| --- | --- | --- |
| `session-start` | 载荷里没有 `session_id` | 忽略 |
| `session-start` | 上次状态是 `working` | 忽略（是上下文压缩，见下） |
| `session-start` | 否则 | 上报 `idle`，认领 `root_session`，附带会话 id 与恢复命令 |
| `user-prompt` | 会话 == `root_session` | 上报 `working`，**清 `blocked_by`** |
| `pre-tool` | 有 `session_id` | 上报 `blocked`，记 `blocked_by = 本会话`（工具名由 matcher 负责筛，状态机不看） |
| `post-tool` | `details.waiting_for_user == true` | **忽略，保持 `blocked`** |
| `post-tool` | 没有待答问卷，且 `blocked_by == 本会话` | 上报 `working`，清 `blocked_by`（防御性路径） |
| `stop` | 会话 != `root_session` | 忽略 |
| `stop` | `blocked_by` 非空 | 忽略（turn 结束 ≠ 问题解决） |
| `stop` | 否则 | 上报 `idle` |
| `session-end` | `reason == "logout"`（账号登出） | `release-agent` 交还 pane，并清空该 pane 的状态 |
| `session-end` | `reason` 是 `clear` / `resume`（换会话） | 只清会话归属，**不** release；新会话自己的 `SessionStart` 会重新认领 |
| `session-end` | `reason` 是 `other`（= 归档或空闲超时），或缺失/认不出 | **完全不动** |

**`SessionEnd` 不等于进程退出，而 `reason` 有两套取值，别混。** mcode 内部是 5 种取值的
联合类型，但发到钩子之前会先过一遍 `compatibleSessionEndReason`
（mcode 0.6.3，`packages/agent-modules/plugin-hooks/src/runner.ts:2189-2197`，在
`runner.ts:1939` 处于序列化前应用）。**钩子读到的是右边一列**：

| 内部取值 | 线上 `payload.reason` | mcode 还在跑吗 | 触发时机 |
|---|---|---|---|
| `logout` | `logout` | **是**（停在登录提示符） | 用户执行 `/logout`，账号被登出 |
| `clear` | `clear` | **是** | 用户执行 `/clear` |
| `resume_other` | `resume` | **是** | 同一进程内从会话 A 切到会话 B |
| `archive` | **`other`** | **是** | 对话被归档 |
| `idle_timeout` | **`other`** | **是** | 空闲 **30 分钟**的定时器 |

三个要点：

- **`logout` 的含义是「账号登出」，不是「进程退出了」**。0.6.3 里
  `createRuntimePluginAuthContextNotifier`
  （`packages/local-runtime-v2/src/application/session/runtime-services-lifecycle.ts:88-100`）
  只在 `authState === "logged_out"` 时发 `endAllSessionsForLogout`，而 `logged_out` 的唯一
  来源是 `/logout` 命令流（`packages/tui/src/tui/controller/product/command-flow.ts:1729-1733`）
  —— 那条分支只调 `refreshAccountStatusNow()`，**不退出**；只有 `/login` 分支才有
  `requestRestart` + `leaveUi`（`command-flow.ts:1707-1714`）。进程真正退出走的是
  `coordinator.dispose()`，它**不发任何钩子**。也就是说：本插件**永远看不到进程退出信号**。
  尽管如此 `logout` 仍然 release，这是有意的取舍 —— 登出后的 mcode 干不了活，让它在面板上
  继续显示 `idle`／「可以输入了」本身就是撒谎；代价有界（只在登出期间 pane 上没有 agent），
  重新登录会发一次全新的 `SessionStart` 重新认领。也不存在「另一个窗口登出把这边健康会话
  释放掉」：`notifyAuthContextChanged` 是从本地命令流经 `launcher.ts:433`（`activeRuntime.host`）
  接到 local runtime 的，跨不到别的窗口；
- **空闲超时上线后是 `other`，不是 `idle_timeout`**。这不是读源码推断的：把 mcode 的空闲
  定时器从 30 分钟缩到 20 秒跑一次真机会话，抓到的真实 `SessionEnd` 就是 `reason='other'`
  —— 早期实现照抄内部联合类型去匹配 `idle_timeout`，那个分支在线上从未被触发过；
- **`other` 是 `archive` 与 `idle_timeout` 的合流值，线上分不出二者**。两者对状态机的要求
  恰好相反（换会话要清归属，空闲超时要完全不动），只能取更保守的那一侧，所以 `other`
  一律按「不动」处理。

herdr 文档也这么要求：

> Only release when the user actually quits. If your agent replaces one session with another in the same process, report the new session instead of releasing.

若对 `SessionEnd` 无条件 release，**空闲 30 分钟后 agent 就会从面板上消失，而 mcode 还坐在输入框前等着** —— 这是本插件实测踩过的坑。所以只有 `logout` 才 release。
**进程真的退出这条根本不靠钩子**：退出走 `coordinator.dispose()`，它不发任何事件，靠的是
herdr 自己的「agent 进程没了」安全网（`available_pane_shell_from_job`，
herdr `src/platform/mod.rs:409`：有非 shell 进程占据前台时它返回 `None`，所以活着的 mcode
自己就能保护住 pane，它一消失 herdr 会在一两秒内清掉）。

换会话那两种线上取值（`clear` / `resume`）必须清掉会话归属：若把 `last_reported` 留在
`working`，紧接着的 `SessionStart` 会撞上「`working` ⇒ 压缩」那条守卫被吞掉，新会话
永远建立不起 `root_session`，后续所有钩子对它都不生效。归属判活救不了这一条 —— 换的是会话，
进程还活着。

线上 `other` 反而必须**一个字节都不动**：会话没变、进程没死，只是人走开了。此时清掉
`root_session`，用户回来敲的第一条 `UserPromptSubmit` 会因为认不出会话被忽略，pane 要
静默到某个无关的 `SessionStart` 为止。正因为 `other` 与归档合流、无法分辨，才只能取这个
更保守的处理 —— 把 `other` 当换会话的 RESET，会以更隐蔽的形式复现同一个故障。

`reason` 缺失或认不出时按「不动」处理：看不懂的信号绝不能当退出处理，而保守的代价有界 ——
进程真退出时 herdr 自己的「agent 进程没了」安全网会在一两秒后收掉它。

**`working` 期间的 `SessionStart` 是压缩，不是子代理。** 早先这里写的理由（「子代理创建时会发
SessionStart」）是**反的**：子代理发的是 `SubagentStart`，而 `beginTurn` 对继承来的会话直接
早退、根本不发 `SessionStart`。`SessionStart` 全库只有三个来源：会话内首次激活插件
（`coordinator.ts:340`）、自动压缩、手动压缩。所以这条守卫真正挡住的是：
**问卷还开着的时候发生压缩** —— 若放行就会翻成 `idle` 并清掉 `blocked_by`，把一个真被卡住的
pane 静默降级成「空闲、可以输入了」。同理，§9.2 那条「pane 永久卡死」的修复依赖的也是
这条守卫，但它跟子代理没关系。

**「怎么知道 pane 被卡住了」的真机依据**（mcode 0.6.3，交互式会话，11 个钩子事件全订阅）：
这类工具**不阻塞**这次工具调用 —— `ask_user` 在 `PreToolUse` 之后约 33ms 就带着
`terminate=true`、`details.waiting_for_user=true` 返回，随后约 56ms 就来 `Stop` ——
**turn 结束的时候问卷还开在 TUI 上**。`ExitPlanMode` / `request_feature_enable` 是同一个
形状（bundle 里同样是 `waiting_for_user=true` + `terminate=true`）。所以：

- 判据是 `PostToolUse` 的 `details.waiting_for_user`，**与工具叫什么无关** —— 这也是
  `decide.py` 不需要认识工具名的原因（名单只在[§4.2](#42-六个钩子工具钩子只挂会阻塞人的三个工具)的 matcher 里）；
- `waiting_for_user=true` 是「还在等」的信号，**不是「已答」**，必须保持 `blocked`；
- `Stop` 只表示这一轮 turn 结束，**不表示问题已解决**，所以 `blocked_by` 非空时
  不翻 `idle`（翻了等于对外宣称「任务完成」）；
- **真正的解障信号是用户作答**：作答算一次新的用户提示，会以新的 `turn_id` 重新触发
  `UserPromptSubmit`（实测那一轮在上一轮 `Stop` 之后约 27s），由 `user-prompt` 分支解开。

按错误语义实现时，真实 herdr 上量到 `blocked` 只存活 **121ms** 就被 `working`、
再被 `done` 覆盖掉；按上表实现后实测：

```text
+0ms idle | +2s working | +4s blocked ... held 15.8s ... +18s working | +19s done
```

**与上次相同的状态不上报**（`last_reported` 去重），因为 herdr 自己的通知也是按状态跃迁
派生并自带去重的，重复上报没有收益。这也是为什么恢复命令必须挂在 `session-start` 上 ——
那是 pane 第一次学到本次会话 id 的时刻，也是唯一能拿到恢复命令去 attach 的时刻
（herdr 会拒绝后到的恢复命令，`resume_not_accepted`）。

### 4.7 双通道上报与「宁可漏报不可误报」

`transport.py` 先试 Unix socket（`HERDR_SOCKET_PATH`，方法
`pane.report_agent` / `pane.release_agent`，超时 0.5s），失败回落
`$HERDR_BIN_PATH pane report-agent` / `release-agent`（超时 1.0s）。两者协议等价。

两个容易踩的点，实现里都刻意做了防护：

- **`seq` 必须严格递增。** herdr 按 **source** 维护一个 seq 高水位
  （`src/terminal/state.rs:1965` 的 `hook_report_is_newer`：要求 `seq > 上次`，
  且一旦用过 seq，之后不带 seq 的上报会被直接拒掉），不满足就**静默丢弃**该次上报，
  **包括 release**。所以每次上报（含 release）都带 `seq`，取自 `time.monotonic_ns()` ——
  必须用单调时钟：`CLOCK_REALTIME` 会被 NTP 回步或校时打回。危险之处在于
  **herdr 丢弃过期上报时照样回 `ok`**（`src/app/api/panes.rs:1678-1679`：
  一旦 `applied` 为假就剥掉 `resume_argv` 走成功分支），调用方无从察觉，却已经记下了状态 ——
  于是状态机会把同一事件永久去重，pane 再无恢复触发地发散。
- **误报比漏报危险得多。** 漏报只是多等一次重试；误报会让调用方记下一个 herdr
  根本没收到的状态，而状态机按 `last_reported` 去重会把后续重试全压掉 —— 同样是无声发散。
  所以只有**结构完整的 herdr 成功应答**（是 dict、含 `result`、不含 `error`）
  才算发送成功，光秃秃的 `{}`、`{"id":...}`、`null`、数组都不算；
  `last_reported` 只在发送被确认后才推进（`runtime.py` 的 `sent` 判断）。

恢复命令是增强能力、状态转移才是主功能，所以 `resume_argv` 校验失败时
**降级为不带恢复命令继续上报**，而不是把这次状态上报一起赔掉。

### 4.8 彻底静默

「不在 herdr 里就完全消失」是硬要求，`runtime.run_once()` 里对应三个提前返回：
不在 herdr（`env` 找不到）、载荷不是合法 JSON、拿不到 `PLUGIN_DATA`。
`runtime.main()` 与 `herdr-report.py` 都把所有异常吞掉并返回 0。
任何一步出错都**不重试、不报错、不写 stdout/stderr**，绝不拖慢或打断 mcode。

---

## 5. 子代理语义：为什么 `blocked` 是例外

mcode 给每个子代理**独立的 `session_id`**。所以「`session_id != root_session`」
就是一个可靠、且与字段命名无关的子代理判据。`decide.py` 据此做了三处过滤：

- `stop` / `session-end` 来自非 `root_session` 的会话 → **忽略**。这条守卫对 release 和
  「换会话只清归属」两条路径**都**生效：不带守卫的话，子代理会话以 `clear` 结束时会把
  正在工作的**父**会话的 `root_session` 抹掉，而父会话不会再有 `SessionStart` 来重新认领，
  pane 就永久停在假 busy 且没有自愈路径
- `session-end` 的线上 `reason` 不是 `logout` → **不 release**，详见[§4.6](#46-状态机只在状态变化时上报)（注意
  空闲超时上线后是 `other`，不是 `idle_timeout`）
- `session-start` 在 pane 处于 `working` 时到达 → **忽略**。这不是子代理（子代理发的是
  `SubagentStart`），而是**上下文压缩**；挡住的是「问卷还开着时发生压缩」被误判成收工

但 **`blocked` 不能这样过滤**。设计上必须假定子代理**可能**调那类会阻塞人的工具，而**此时确实有
一个真人被卡住了** —— pane 明明停在等人回答上，herdr 却显示 `working` 且永远不通知，
这是最坏的一种错配。所以子代理的那类提问照报 `blocked`。

代价是必须知道**是谁置的位**：状态文件里记 `blocked_by`，
`post-tool` 只在 `payload.session_id == blocked_by` 时才可能把 `blocked` 清回 `working`。
子代理答完问题，只清自己置的那一次阻塞，不会误清根会话正在等的另一次决策。

一句话：`working` / `idle` 按「谁是这个 pane 的根会话」过滤，`blocked` 按
「谁真的把人卡住了」过滤。

**关于这个例外，有一条真机结论要讲清楚**（mcode 0.6.3）：**子代理根本没有 `ask_user`
这个工具**。真机分别派了 `explore` 与 `mavis` 两种子代理去尝试提问，两者独立报告
调不到、只有顶层 agent 能调（`explore` 报出自己的工具是 bash / glob / grep / read /
`web_fetch`）。所以上面这条例外分支在当前版本是**前瞻性防御，不是可达路径** ——
回放测试里那条用例是用构造载荷覆盖的。（这条实测只针对 `ask_user`；另两个阻塞工具
`ExitPlanMode` / `request_feature_enable` 没有逐个派子代理验证过，所以这里只作防御性保留。）

子代理的 `session_id` 管道仍然保留：mcode 某个版本真把那类工具暴露给子代理时，
这套机制不用改设计就能接上。**而真机上确实验证过的是**：子代理干活期间，pane 全程停在
`working`，从未被翻成 `idle`。

---

## 6. 性能

钩子入口（`herdr-report.py`）在 mcode 的工具调用关键路径上，所以它的耗时是要盯的数。

| 测量 | 值 |
| --- | --- |
| 隔离 harness，11 次连续采样 | **0.048 ~ 0.051 s** |
| 同一台机器，较繁忙时段 | 0.068 ~ 0.182 s |
| 参照：`python3 -c pass` | ≈ 0.010 s |
| `test/test_replay.py` 的断言阈值 | 1.0 s |

**只当量级看，不要当 SLA**：数值随机器负载浮动。

隔离 harness 的做法：临时目录里的假 herdr 二进制 + 临时 `PLUGIN_DATA`，
让钩子进程**自身环境不含 `HERDR_*`**、把假 `HERDR_*` 放在它的直接父进程上 ——
这正是生产形态（mcode 给钩子白名单环境，`HERDR_*` 只在祖先进程上）。这样既覆盖了
`/proc` 回溯那条路，又保证任何上报都不会落到真实 herdr 上。计时器括住的是
「父进程 → 钩子 → 返回」整段。

真正的上报在脱离进程组的后台进程里完成，不占用这个时间。

---

## 7. 恢复命令依赖 `mcode` 在 herdr server 的 PATH 上

会话恢复命令是 **`["mcode", "--session", session_id]`**（`runtime.py`），
上报时作为 `resume_argv` 跟在 herdr 的 `--` 之后。

**首词必须是裸命令名，不能是路径。** herdr 的 `validate_resume_argv`
（`src/agent_resume.rs:80-87`）要求首元素非空、不以 `-` 开头、且只含
`[A-Za-z0-9._-]` —— 绝对路径会被拒。另外还限制：≤64 个参数、总长 ≤8192 字节、
不含控制字符、不含单引号。

于是就有一个**很容易被忽略的约束**：恢复命令是由 **herdr server 进程**去解析执行的，
不是由你的当前 shell 解析。如果 `mcode` 不在 **herdr server 的 PATH** 上，
恢复会**静默失败**（面板状态照常上报，只有恢复不工作）。

`install.sh` 分两级检查：先看**当前环境**的 PATH 上有没有 `mcode`，没有再读
**herdr server 进程**的 PATH（`/proc/<pid>/environ`）—— 后者才是真正执行恢复命令的那份环境。

> 这里特意**不用「登录 shell 的 PATH」**：`env -i /bin/sh -lc` 那种判断之所以不可用，
> 是因为登录 shell 不读 `~/.bashrc`，而很多机器（这台也是）恰恰是把 mcode 的 PATH
> 加在 `~/.bashrc` 里的 —— 于是无论 mcode 是否真的可用都会误报成「找不到」。
> 读者自己排查时也容易在这里绕半天。

```bash
ln -s "$(command -v mcode)" ~/.local/bin/mcode
# 并确保 ~/.local/bin 在 herdr server 能看到的 PATH 里
# 然后重启 herdr server，让它带上新的 PATH
```

（顺带一提：这也是为什么源码里不传 `agent_session_path` —— 它和 `agent_session_id`
一样会被白名单挡掉，传了只是噪音。）

---

## 8. 排障

### 8.1 `mcode plugin list -m local --available` 什么都看不到

按可能性从高到低：

1. **被禁用了**。`mcode plugin list -m local --available` 里这一行是 `[-] disabled`
   而不是 `[*]` 时，插件文件全都是好的，就是不执行。禁用名单按**路径**记在 SQLite 里，
   **重跑 `./install.sh` 清不掉**（脚本会检测到并自动重新启用，见
   [§2.2.4](#224-disable-会跨重装残留)；手动修复是 `mcode plugin enable -m local mcode-herdr`）。
   注意这行的表现和「完全看不到」不一样：条目还在，只是前缀是 `[-]`
2. **符号链接**。如果你自己动手装过，十有八九是用了 `ln -s`，扫描器直接跳过（见[§2.2](#22-四个静默失败陷阱)）
3. **目录多套了一层**。插件目录必须**直接**含清单文件（见[§2.2](#22-四个静默失败陷阱)）
4. **`plugin.json` 缺 `icon` 字段**。mcode 解析本地插件清单时**无条件**要求 `icon`，
   缺失即判非法（`MANIFEST_SCHEMA_INVALID`）；而本地插件的扫描会把校验失败写进
   `diagnostics` 后**静默跳过**，CLI 上没有任何提示。本项目自己就踩过这个坑

   `plugin/.minimax-plugin/plugin.json` 里必须有 `"icon": "icon.png"`，
   且 `plugin/icon.png` 真实存在（1×1 PNG）。另外 `exampleQueries` / `apps` /
   `mcpServers` / `skills` 四个数组**也都是必填**（`hooks` 才是可选的），
   缺任何一个的后果与缺 `icon` 一样：整包被静默跳过。完整清单见[§2.4](#24-清单要求顺手写插件时会踩)

如果是用 `./install.sh` 装的，第 2、3、4 条已经排除了（脚本复制而非链接、
自检清单层级与必需文件、并且发现不了就会退出）—— 那就查第 1 条。
注意脚本退出码为 0 时它认定的是**「已发现且已启用」**，而不只是「文件复制成功」。

### 8.2 herdr 里看不到 mcode agent

按顺序排：

1. **确认会话真的跑在 herdr pane 里**：`echo $HERDR_ENV` 应该是 `1`。
   不在 herdr 里，插件是彻底静默的设计（这是有意的，不是 bug）
2. **确认插件装了且启用了**：`mcode plugin list -m local --available`
   里应能看到 `mcode-herdr@local`；装完要**重启 mcode / 开新会话**
3. **确认 PATH**：见[§7](#7-恢复命令依赖-mcode-在-herdr-server-的-path-上)。
   这一条只影响恢复，不影响状态显示

### 8.3 怎么观察

```bash
herdr agent list                  # 列出 agent 及其状态
herdr agent explain <TARGET>      # 解释某个 agent 的检测状态
```

状态文件在 mcode 注入的 `PLUGIN_DATA` 目录里，每个 pane 一个 JSON
（内容形如 `{"root_session": ..., "blocked_by": ..., "last_reported": ...}`）。
该目录由 mcode 决定，形如 `<profile>/v2/plugin-data/hooks/<插件名>/`
（推导见 mcode 源码 `plugin-system/plugin/runtime/package-storage.ts`；本机 mcode 0.6.3 上
`~/.minimax/v2/plugin-data/hooks/` 下确实存在 `herdr-probe`、`lark` 两个同名目录 ——
但 `mcode-herdr` 自身尚未在本机安装，所以下面这个具体路径未经实跑确认）。

⚠️ **这个目录是插件的钩子第一次真正触发之后才被创建的。** 刚装完、还没跑过一轮
mcode 会话时，`ls` 它会报「No such file or directory」—— 那不代表装坏了，
只代表还没有任何钩子跑过。跑一轮之后（哪怕只是在 herdr pane 里开一个新会话
再发一条提示）它就会出现。直接找：

```bash
ls -l "${MINIMAX_DATA_DIR:-$HOME/.minimax}"/v2/plugin-data/hooks/mcode-herdr/
cat "${MINIMAX_DATA_DIR:-$HOME/.minimax}"/v2/plugin-data/hooks/mcode-herdr/w4_p3.json
```

`last_reported` 停在 `working` 而 mcode 其实早就退出了，就是下面第 9.2 节说的那个坑。

### 8.4 跑测试

```bash
./test/run-tests.sh
```

离线回放，**完全不涉及 herdr 与 mcode**，也不需要网络：喂真实的钩子载荷样本
（`test/fixtures/`，每个样本的来源见 `test/fixtures/README.md`），用假 herdr 二进制
逐条记录 argv / socket 请求，再断言调用序列与状态流转。当前 **146 个用例全绿**。
样本里从未实时抓到的只有子代理工具事件（按 mcode 的字段契约构造）和计划模式批准
那条阻塞链（按**已安装的 0.6.3 bundle** 的实际返回构造）—— 字段名变更风险未被真实
抓包覆盖；而且如第 5 节所述，mcode 0.6.3 的子代理根本调不到那类工具，子代理路径的
构造动机有限。

**验证状态说明**：本仓库自带的是离线回放测试。端到端（在真实 herdr pane 里跑 mcode、
断言 `herdr agent list` 出现 mcode 并正确流转、herdr server 重启后会话恢复）
目前**没有**自动化测试，装好插件后需要手工验证一次。

---

## 9. 已知限制

### 9.1 仅 Linux

`/proc` 父进程链回溯是找回 `HERDR_*` 的唯一手段，状态文件的并发控制还依赖
`fcntl.flock`。在非 Linux 平台上插件取不到 pane 上下文，
按 herdr「不在 herdr 里就什么都不做」的约定**退化为彻底无操作** —— 这恰好是正确的降级行为，
但也意味着在 macOS 上这个插件不提供任何功能。

### 9.2 异常退出后残留的 `working`

状态文件里除了 `root_session` / `blocked_by` / `last_reported`，还记着**归属它的
mcode 进程**（`owner_pid` + `owner_start`，见[§4.5](#45-状态每个-pane-一个-json)）。因此如果 mcode 在一轮进行中被
强杀（`kill -9`、终端被关、休眠唤醒后进程已死），`Stop` 永远不会到达，
`last_reported` 会停在 `working` —— 但下一次读状态时插件会发现**归属进程已经不在了**
（或 pid 还在、启动时间变了，说明是别的进程复用了这个 pid），于是把这份状态当作不存在，
新的顶层会话的 `SessionStart` 会被正常接受。

所以现在**不需要手工清理**：pane 会在下一个 mcode 会话启动时自动恢复。

判据是归属进程的存活，而不是「`working` 这个值本身」。同一 pane 里 mcode 还活着时，
`working` 仍然会让压缩触发的 `SessionStart` 被正确吞掉 —— 这条守卫不受影响
（它挡的是压缩，不是子代理，见[§4.6](#46-状态机只在状态变化时上报)）。

状态文件里出现 `owner_pid: null`（老版本插件写的、或手工构造的）同样按「归属不明」处理，
即视为过期。

如果确实需要手工干预（例如归属进程还在但状态已错乱）：

- **换一个 herdr pane**。状态按 pane id 存，新 pane = 新文件
- **手动删掉那个 pane 的状态文件**（路径来源见[§8.3](#83-怎么观察)）：

  ```bash
  rm -f "${MINIMAX_DATA_DIR:-$HOME/.minimax}"/v2/plugin-data/hooks/mcode-herdr/w4_p3.json*
  ```

  ⚠️ **重跑 `./install.sh` 并不能解决这个问题**：`install.sh` 只 `rm -rf`
  `<profile>/plugins/mcode-herdr`（插件代码目录），**插件数据目录
  `<profile>/v2/plugin-data/hooks/mcode-herdr/` 在它之外，不会被清掉**。
  `./uninstall.sh` 同理。卸载重装也不会清。

  重启 herdr server 也**不**保证清空：herdr 把 pane 编号持久化在
  `<config>/session.json`（`public_pane_numbers` / `next_public_pane_number`），
  恢复时从最大值继续发号，所以 pane id 不会重复、状态文件也就不会被自动作废。
  归属检查才是让陈旧状态失效的机制。

### 9.3 `agent_session_id` 会被 herdr 丢弃

herdr 的 `session_ref_from_report` 先判 `is_official_agent_source`，
而后者是 17 对硬编码的 `(source, agent)` 白名单（`src/agent_resume.rs:327`），不含 mcode。
所以插件虽然照常上报了 `agent_session_id`，但对自定义 agent source 一律无效 ——
**无害，但也没用**。会话恢复能力**完全依赖 `resume_argv`**，即
[§7](#7-恢复命令依赖-mcode-在-herdr-server-的-path-上) 那条命令。

### 9.4 herdr 没有 `done` 这个上报状态

插件只会报 `idle` / `working` / `blocked`（herdr `report-agent --state` 的合法值就是
`idle / working / blocked / unknown`）。**一轮做完就是报 `idle`**：
herdr 自己会在显示层把「`idle` + 未被查看」渲染成 `done`
（`src/app/agent_view.rs` 的 `status_name`）。这不影响 `herdr agent wait --until done`。

### 9.5 `uninstall.sh` 的 release `seq` 取自 `/proc/uptime`

herdr 丢弃 seq 不递增的上报，而插件用的是 `monotonic_ns()`（开机以来的纳秒）。
如果 `uninstall.sh` 用 `date +%s`（纪元纳秒）来生成 release 的 seq，
这个 `10^18` 量级的值会把该 pane 的 seq 高水位永久抬上去 —— 重装后插件的
monotonic seq 会被 herdr 判为过期而**全部静默丢弃**，插件看起来像彻底坏了。
所以卸载脚本用 `/proc/uptime` 换算出同一时钟族的时间（读不到时才退回纪元值）。
**维护提示：任何新增的 `release` 调用都必须遵守同一条约束。**

### 9.6 子代理目前调不到 `ask_user`

mcode 0.6.3 的 `explore` / `mavis` 两种子代理都没有 `ask_user` 工具（真机验证）。
本插件的子代理 `blocked` 分支因此是前瞻性防御，当前不可达。见[§5](#5-子代理语义为什么-blocked-是例外)。

---

## 10. 仓库结构

```text
.
├── plugin/                        # 插件本体，安装时整体复制到 ~/.minimax/plugins/mcode-herdr/
│   ├── .minimax-plugin/plugin.json
│   ├── icon.png                   # 必填，缺了插件会被静默跳过
│   ├── hooks/hooks.json
│   └── scripts/
│       ├── herdr-report.py
│       ├── worker.py
│       └── mcode_herdr/*.py
├── test/
│   ├── run-tests.sh               # ./test/run-tests.sh
│   ├── fixtures/*.json            # 真实载荷样本，来源见 fixtures/README.md
│   └── test_*.py
├── probe/                         # 一次性探针插件（herdr-probe）：把钩子收到的 stdin
│   └── scripts/dump.sh            # 与 /proc 父链原样落盘。用来确定 mcode 的载荷字段名。
│                                  # 不属于本插件，install.sh 不装它
├── install.sh
├── uninstall.sh
└── docs/plans/                    # 设计文档与实现计划
    ├── 2026-10-06-mcode-herdr-design.md
    └── 2026-10-06-mcode-herdr-implementation-plan.md
```

---

## 11. 自己排查时的两个提醒

1. **插件是彻底静默的**。任何一步出错都不重试、不报错、不写 stdout/stderr。
   排查时不要指望日志 —— 唯一的可观测物是状态文件和 herdr 侧的 agent 状态
2. **别顺手改 release 调用的 seq 来源**，见[§9.5](#95-uninstallsh-的-release-seq-取自-procuptime)
