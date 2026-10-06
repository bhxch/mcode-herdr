# mcode-herdr

mcode 的**本地插件**：把 mcode 会话的 agent 状态与会话恢复命令上报给 herdr，
让 mcode 在 herdr 侧边栏、`herdr agent list`、`herdr agent wait` / `herdr agent prompt`
里成为一个一等 agent，而不是一个 herdr 完全看不见的进程。

- 实现：Python 3 标准库（无第三方依赖）+ POSIX shell 安装脚本
- 平台：**仅 Linux**（原因见 [已知限制](#7-已知限制)）
- 版本基线：mcode 0.6.3、herdr 0.9.3、Python 3.14（本机实测）

---

## 1. 它解决什么问题

herdr 的 agent 识别是编译在二进制里的：`src/agent_resume.rs` 的
`is_official_agent_source` 是一份 17 对 `(source, agent)` 的硬编码白名单，不含 mcode；
herdr 也没有针对 mcode 的屏幕检测规则。**所以在装这个插件之前，herdr 对 mcode 是完全盲的**：

- `herdr agent list` 里没有 mcode —— 本机实测返回 `{"agents":[],"type":"agent_list"}`
- 侧边栏没有条目，mcode 跑完一轮或卡住等你决策时不会通知你
- `herdr agent wait` / `herdr agent prompt` 之类的自动化等不到、够不着 mcode
- herdr server 重启后，pane 里跑着的 mcode 会话无法被拉回来

herdr 不能改（agent kind 列表编译在二进制里），所以走的是 herdr 官方文档
《Add Herdr support to your agent》给的路子：**由 agent 自行上报**。
本插件就是那个上报方。装上之后，一个 mcode 会话在 herdr 侧的状态是
`idle` / `working` / `blocked`，其恢复命令是 `mcode --session <id>`。

---

## 2. 安装与卸载

### 安装

```bash
./install.sh
```

`install.sh` 做四件事：

1. 把 `plugin/` 整目录同步到 `$MINIMAX_DATA_DIR/plugins/mcode-herdr/`
   （默认 `~/.minimax/plugins/mcode-herdr`）；先 `rm -rf` 旧目录再复制，并清掉 `__pycache__`
2. 用 `mcode plugin list -m local --available` 验证本地市场确实发现了它，
   grep 不到就直接失败退出并提示检查 `icon` 字段
3. 检查**登录 shell 的 PATH** 上有没有 `mcode`，没有就打印警告
   （这是会话恢复的硬前提，见 [第 5 节](#5-恢复命令依赖-mcode-在-path-上)）
4. 提示你重启 mcode

几点需要知道的：

- **不需要 `mcode plugin add`。** 本地插件只要躺在 `~/.minimax/plugins/<name>/` 下，
  被扫描到就已经是 installed + enabled（mcode CLI 显式拒绝本地插件的 install 操作，
  `plugin add` 是 Desktop 的功能）。`install.sh` 只是同步 + 验证。
- **用的是复制而不是符号链接**，因为本地市场扫描器不跟随符号链接。
  代价：改完代码要重跑 `./install.sh` 才生效。
- 非默认 profile 用 `MINIMAX_DATA_DIR` 覆盖：

  ```bash
  MINIMAX_DATA_DIR=/path/to/other-profile ./install.sh
  ```

- **装完必须重启 mcode 或开一个新会话才生效**（`install.sh` 最后一行也是这么提示的）：
  已运行的会话不会补挂钩子。

### 卸载

```bash
./uninstall.sh
```

它先对**当前所在 pane** 执行 `herdr pane release-agent`（否则 herdr 会继续把这个 pane
显示为有 agent），再删掉安装目录。其他 pane 里的残留占用要自己
`herdr pane release-agent <pane>` 清理。

注意卸载**不删**插件数据目录下的状态文件，原因见 [第 7 节第 2 条](#72-异常退出后残留的-working)。

---

## 3. 工作原理

```
mcode 生命周期钩子（6 个）
  │  argv[1] = 事件名，stdin = 载荷 JSON
  ▼
plugin/scripts/herdr-report.py ── 读 stdin → Popen(start_new_session=True) ──► worker.py（后台）
                                                                          │
                                                                          ▼
                                                          runtime.run_once()
                            env 找回 → payload 解析 → store.update(decide) → transport 上报
                                                                          │
                                                    Unix socket（优先） / herdr CLI（回落）
```

### 3.1 模块对照表

| 文件 | 职责 |
| --- | --- |
| `plugin/hooks/hooks.json` | 6 个 mcode 事件 → `scripts/herdr-report.py <event>` 的映射 |
| `plugin/scripts/herdr-report.py` | 钩子入口：读 stdin、派生后台进程、**不等**、立刻返回 |
| `plugin/scripts/worker.py` | 后台 worker，调 `runtime.main()` |
| `plugin/scripts/mcode_herdr/env.py` | 沿 `/proc` 父进程链回溯，找回被剥掉的 `HERDR_*` |
| `plugin/scripts/mcode_herdr/payload.py` | 解析 stdin JSON → `Payload`（宽容归一：空白/空串 → `None`） |
| `plugin/scripts/mcode_herdr/store.py` | 每 pane 一个状态文件，`flock` + 临时文件原子替换 |
| `plugin/scripts/mcode_herdr/decide.py` | 纯函数状态机：`(事件, 载荷, 上次状态)` → 决策 |
| `plugin/scripts/mcode_herdr/transport.py` | 双通道上报（socket → CLI）与 `seq` / 应答判定 |
| `plugin/scripts/mcode_herdr/herdr.py` | 协议常量与标签：`source="mcode-herdr"`、`agent="mcode"` |
| `plugin/scripts/mcode_herdr/runtime.py` | 把上面这些串成一次完整上报 |

### 3.2 六个钩子，工具钩子只挂 `ask_user`

`plugin/hooks/hooks.json` 注册了 6 个事件，每个都是
`python3 "${PLUGIN_ROOT}/scripts/herdr-report.py" <event>`，`timeout: 5`：

| mcode 事件 | 传给脚本的动作名 |
| --- | --- |
| `SessionStart` | `session-start` |
| `UserPromptSubmit` | `user-prompt` |
| `PreToolUse`（`"matcher": "ask_user"`） | `pre-tool` |
| `PostToolUse`（`"matcher": "ask_user"`） | `post-tool` |
| `Stop` | `stop` |
| `SessionEnd` | `session-end` |

事件名是**显式传参**的，脚本不靠载荷里的 `hook_event_name` 做路由，所以载荷字段改名不影响分发。

两个工具钩子带 `"matcher": "ask_user"`，**只为问卷工具触发**。mcode 的
`matches()`（`plugin-hooks/src/runner.ts`）对 MINIMAX 格式（本插件的格式）的 matcher，
当 pattern 只含 `[A-Za-z0-9_.:/-]` 时是按 `,`/`|` 切开后做**精确、区分大小写的字符串相等**比较
（`ask_user` 落在这个分支）；只有含其他字符才退化成正则。所以这里既不是 glob 也不是正则。

这条限制是有意为之：不给工具钩子加 matcher 的话，**每一次工具调用都会 fork 一个进程**。
加上之后，钩子只在真正需要上报 `blocked` 时才触发。

### 3.3 入口立即返回，上报交给脱离进程组的后台进程

`herdr-report.py` 只做三件事：读掉 stdin、`Popen` 派生 `worker.py`
（`start_new_session=True`，stdout/stderr 接 `DEVNULL`）、把载荷写进子进程 stdin，
然后**直接返回，不 wait**。`herdr-report.py:38` 还特意给 `Popen` 填上 `returncode`，
避免 GC 时 `__del__` 发 `ResourceWarning` 污染 stderr。

实测（本机，同一 hook 入口连跑 7 次）：**0.0232 ~ 0.0255 秒**返回，上报在后台完成。
`test/test_replay.py` 里也把这个量级写进了断言注释。

钩子挂在内联路径上，**一个字节都不能往 stdout/stderr 写** —— `PreToolUse` 的输出会被
mcode 运行时消费，污染可能改变工具执行结果。回放测试对此有硬断言
（`assertEqual(proc.stdout, "")`）。

### 3.4 找回 `HERDR_*`：`/proc` 父进程链回溯

这是整个插件最硬的一个约束。mcode 派发钩子子进程时给的是一个**严格白名单环境**
（`safeHookEnvironment()`：`PATH HOME LANG TERM SHELL USER TMPDIR TEMP TMP PATHEXT
SystemRoot ComSpec USERPROFILE HOMEDRIVE HOMEPATH APPDATA LOCALAPPDATA`），
**`HERDR_*` 一个都不传**（设计文档 §2.7 的探针实测：钩子里这些变量全空）。

所以 `env.py` 只能自己去找：从自己的 pid 出发，沿 `/proc/<pid>/status` 的 `PPid:`
逐级向上（最多 12 级），找到第一个满足 `HERDR_ENV=1` 且 `HERDR_PANE_ID`、
`HERDR_BIN_PATH` 都非空的祖先 —— 那个进程就是 herdr 拉起的 mcode，它的
`/proc/<pid>/environ` 里有全套 `HERDR_*`。

父进程号必须读 `status` 里的 `PPid:`，不能按位置解析 `/proc/<pid>/stat` ——
`comm` 含空格或括号会直接错位（探针第一版就踩了这个坑，只回溯到 depth=0）。

`runtime._resolve_herdr_env` 还有一条捷径：如果当前进程环境里**本来就有**
`HERDR_ENV=1` 和 `HERDR_PANE_ID`（例如在 herdr pane 里手工跑 `worker.py`、或回放测试注入假环境），
就直接采信，不再翻 `/proc`。生产环境的钩子永远走不到这条分支。

### 3.5 状态：每个 pane 一个 JSON

`store.py` 在 mcode 注入的 `PLUGIN_DATA` 目录里，为每个 pane 存一个 JSON：

```json
{"root_session": "mvs_...", "blocked_by": "mvs_...", "last_reported": "working"}
```

- **文件名**：pane id 里所有非 `[A-Za-z0-9._-]` 的字符换成 `_`，再加 `.json`。
  例如 pane `w4:p3` → `w4_p3.json`，同目录还有一个 `w4_p3.lock`（flock 锁文件）
- **并发安全**：所有读改写都在 `Store.update()` 的**单次持锁**内完成。
  注意 `load()`/`save()` 各自加锁，但两者合起来不是一个临界区 —— 需要
  「读最新值 → 决定 → 写回」整体原子时必须用 `update()`（其回调不可重入同一 pane 的 store）
- **不撕裂**：写入走临时文件 + `os.replace()` 原子替换，读者永远看到完整 JSON。
  文件权限 `0600`：状态里是要拼进 `mcode --session <id>` 的会话标识，不该同机可读
- **不用 `/tmp` 兜底**：拿不到 `PLUGIN_DATA` 就直接什么都不做。状态文件名由 pane id 派生、
  可预测，而临时文件用的是 `os.open(O_CREAT)`，它会跟随预置的符号链接 ——
  在全局可写的 `/tmp` 里等于把状态 JSON 写进攻击者指定的文件

### 3.6 状态机：只在状态变化时上报

`decide.py` 是纯函数，签名 `decide(action, payload, state) -> Decision | None`，
输入是（事件、载荷、上次状态），输出是「报不报、报什么」。规则：

| 动作 | 条件 | 结果 |
| --- | --- | --- |
| `session-start` | 上次状态是 `working` | 忽略（判定为子代理创建） |
| `session-start` | 否则 | 上报 `idle`，认领 `root_session`，附带会话 id 与恢复命令 |
| `user-prompt` | 会话 == `root_session` | 上报 `working`，清 `blocked_by` |
| `pre-tool` | `tool_name == ask_user` | 上报 `blocked`，记 `blocked_by = 本会话` |
| `post-tool` | `tool_name == ask_user` 且 `blocked_by == 本会话` | 上报 `working`，清 `blocked_by` |
| `stop` | 会话 == `root_session` | 上报 `idle` |
| `session-end` | 会话 == `root_session` | `release-agent` 交还 pane，并清空该 pane 的状态 |

**与上次相同的状态不上报**（`last_reported` 去重），因为 herdr 自己的通知也是按状态跃迁
派生并自带去重的，重复上报没有收益。这也是为什么恢复命令必须挂在 `session-start` 上 ——
那是 pane 第一次学到本次会话 id 的时刻，也是唯一能拿到恢复命令去 attach 的时刻
（herdr 会拒绝后到的恢复命令，`resume_not_accepted`）。

### 3.7 双通道上报与「宁可漏报不可误报」

`transport.py` 先试 Unix socket（`HERDR_SOCKET_PATH`，方法
`pane.report_agent` / `pane.release_agent`，超时 0.5s），失败回落
`$HERDR_BIN_PATH pane report-agent` / `release-agent`（超时 1.0s）。两者协议等价。

两个容易踩的点，实现里都刻意做了防护：

- **`seq` 必须严格递增。** herdr 按 **source** 维护一个 seq 高水位
  （`src/terminal/state.rs:1965` 的 `hook_report_is_newer`：要求 `seq > 上次`，且一旦用过 seq，
  之后不带 seq 的上报会被直接拒掉），不满足就**静默丢弃**该次上报，**包括 release**。
  所以每次上报（含 release）都带 `seq`，取自 `time.monotonic_ns()` —— 必须用单调时钟：
  `CLOCK_REALTIME` 会被 NTP 回步或校时打回。危险之处在于 **herdr 丢弃过期上报时照样回 `ok`**
  （`src/app/api/panes.rs:1678-1679`：一旦 `applied` 为假就剥掉 `resume_argv` 走成功分支），
  调用方无从察觉，却已经记下了状态 —— 于是状态机会把同一事件永久去重，pane 再无恢复触发地发散。
- **误报比漏报危险得多。** 漏报只是多等一次重试；误报会让调用方记下一个 herdr
  根本没收到的状态，而状态机按 `last_reported` 去重会把后续重试全压掉 —— 同样是无声发散。
  所以只有**结构完整的 herdr 成功应答**（是 dict、含 `result`、不含 `error`）
  才算发送成功，光秃秃的 `{}`、`{"id":...}`、`null`、数组都不算；
  `last_reported` 只在发送被确认后才推进（`runtime.py` 的 `sent` 判断）。

恢复命令是增强能力、状态转移才是主功能，所以 `resume_argv` 校验失败时
**降级为不带恢复命令继续上报**，而不是把这次状态上报一起赔掉。

### 3.8 彻底静默

「不在 herdr 里就完全消失」是硬要求，`runtime.run_once()` 里对应三个提前返回：
不在 herdr（`env` 找不到）、载荷不是合法 JSON、拿不到 `PLUGIN_DATA`。
`runtime.main()` 与 `herdr-report.py` 都把所有异常吞掉并返回 0。
任何一步出错都**不重试、不报错、不写 stdout/stderr**，绝不拖慢或打断 mcode。

---

## 4. 子代理语义：为什么 `blocked` 是例外

mcode 给每个子代理**独立的 `session_id`**。所以「`session_id != root_session`」
就是一个可靠、且与字段命名无关的子代理判据。`decide.py` 据此做了三处过滤：

- `stop` / `session-end` 来自非 `root_session` 的会话 → **忽略**。
  否则子代理跑完会把 busy 的 pane 直接翻成 `idle`：既是一次假「完成」，
  又会弹一条莫名其妙的通知；而 `session-end` 还会在根会话还在跑的时候把 pane 释放掉
- `session-start` 在 pane 处于 `working` 时到达 → **忽略**，当作子代理被创建。
  子代理总是在父 agent 干活期间被创建的，所以这个启发式够用

但 **`blocked` 不能这样过滤**。子代理完全可能调 `ask_user`，而**此时确实有一个真人
被卡住了** —— pane 明明停在等人回答上，herdr 却显示 `working` 且永远不通知，
这是最坏的一种错配。所以子代理的 `ask_user` 照报 `blocked`。

代价是必须知道**是谁置的位**：状态文件里记 `blocked_by`，`post-tool` 只在
`payload.session_id == blocked_by` 时才把 `blocked` 清回 `working`。
子代理答完问题，只清自己置的那一次阻塞，不会误清根会话正在等的另一次决策。

一句话：`working` / `idle` 按「谁是这个 pane 的根会话」过滤，`blocked` 按
「谁真的把人卡住了」过滤。

---

## 5. 恢复命令依赖 `mcode` 在 PATH 上

会话恢复命令是 **`["mcode", "--session", session_id]`**（`runtime.py`），
上报时作为 `resume_argv` 跟在 herdr 的 `--` 之后。

**首词必须是裸命令名，不能是路径。** herdr 的 `validate_resume_argv`
（`src/agent_resume.rs:80-87`）要求首元素非空、不以 `-` 开头、且只含
`[A-Za-z0-9._-]` —— 绝对路径会被拒。另外还限制：≤64 个参数、总长 ≤8192 字节、
不含控制字符、不含单引号。

于是就有一个**很容易被忽略的约束**：如果 `mcode` 不在 **herdr server 的 PATH** 上，
恢复会**静默失败**（面板状态照常上报，只有恢复不工作）。`install.sh` 会检查
**登录 shell** 的 PATH 上有没有 `mcode`，没有就打警告：

```bash
ln -s "$(command -v mcode)" ~/.local/bin/mcode
# 并确保 ~/.local/bin 在 herdr server 能看到的 PATH 里
```

（顺带一提：这也是为什么源码里不传 `agent_session_path` —— 它和 `agent_session_id`
一样会被白名单挡掉，传了只是噪音。）

---

## 6. 排障

### 6.1 `mcode plugin list -m local --available` 什么都看不到

**几乎总是因为 `plugin.json` 缺 `icon` 字段。** mcode 的 `readManifestIcon()` 无条件要求它，
缺失即判定清单非法；而本地插件的扫描会把校验失败写进 `diagnostics` 后**静默跳过**，
CLI 上没有任何提示（官方插件全都带 `icon`，所以缺字段的本地包看起来就像「市场整体失效」）。
本项目自己就踩过这个坑。

`plugin/.minimax-plugin/plugin.json` 里必须有 `"icon": "icon.png"`，
且 `plugin/icon.png` 真实存在（1×1 PNG）。

### 6.2 herdr 里看不到 mcode agent

按顺序排：

1. **确认会话真的跑在 herdr pane 里**：`echo $HERDR_ENV` 应该是 `1`。
   不在 herdr 里，插件是彻底静默的设计（这是有意的，不是 bug）
2. **确认插件装了且启用了**：`mcode plugin list -m local --available`
   里应能看到 `mcode-herdr@local  enabled`；装完要**重启 mcode / 开新会话**
3. **确认 PATH**：见 [第 5 节](#5-恢复命令依赖-mcode-在-path-上)。
   这一条只影响恢复，不影响状态显示

### 6.3 怎么观察

```bash
herdr agent list                  # 列出 agent 及其状态
herdr agent explain <TARGET>      # 解释某个 agent 的检测状态
```

状态文件在 mcode 注入的 `PLUGIN_DATA` 目录里，每个 pane 一个 JSON
（内容形如 `{"root_session": ..., "blocked_by": ..., "last_reported": ...}`）。
该目录由 mcode 决定，形如 `<profile>/v2/plugin-data/hooks/<插件名>/`
（推导见 mcode 源码 `plugin-system/plugin/runtime/package-storage.ts`；本机 mcode 0.6.3 上
`~/.minimax/v2/plugin-data/hooks/` 下确实存在 `herdr-probe`、`lark` 两个同名目录 ——
但 `mcode-herdr` 自身尚未在本机安装，所以下面这个具体路径未经实跑确认）。直接找：

```bash
ls -l "${MINIMAX_DATA_DIR:-$HOME/.minimax}"/v2/plugin-data/hooks/mcode-herdr/
cat "${MINIMAX_DATA_DIR:-$HOME/.minimax}"/v2/plugin-data/hooks/mcode-herdr/w4_p3.json
```

`last_reported` 停在 `working` 而 mcode 其实早就退出了，就是下面第 7 节第 2 条说的那个坑。

### 6.4 跑测试

```bash
./test/run-tests.sh
```

离线回放，**完全不涉及 herdr 与 mcode**：喂真实的钩子载荷样本
（`test/fixtures/`，每个样本的来源见 `test/fixtures/README.md`），用假 herdr 二进制
逐条记录 argv / socket 请求，再断言调用序列与状态流转。当前 **89 个用例全绿**。
样本里唯一从未实时抓到的是子代理工具事件，它按 mcode 的字段契约构造，
所以子代理路径的字段名变更风险未被真实抓包覆盖。

**验证状态说明**：本仓库自带的是离线回放测试。端到端（在真实 herdr pane 里跑 mcode、
断言 `herdr agent list` 出现 mcode 并正确流转、herdr server 重启后会话恢复）
目前**没有**自动化测试，装好插件后需要手工验证一次。

---

## 7. 已知限制

### 1. 仅 Linux

`/proc` 父进程链回溯是找回 `HERDR_*` 的唯一手段。在非 Linux 平台上插件取不到 pane 上下文，
按 herdr「不在 herdr 里就什么都不做」的约定**退化为彻底无操作** —— 这恰好是正确的降级行为，
但也意味着在 macOS 上这个插件不提供任何功能。

### 2. 异常退出后残留的 `working`

如果 mcode 在一轮进行中被强杀（`kill -9`、终端被关、机器休眠唤醒后进程已死），
`Stop` 永远不会到达，状态文件里的 `last_reported` 就永久停在 `working`。

后果是连锁的：之后**任何**在该 pane 里开启的顶层会话，它的 `SessionStart`
都会被「`working` ⇒ 必然是子代理」这条启发式吞掉（`decide.py`），
于是它自己的 `root_session` 永远建立不起来，后续所有钩子对它也都不生效 ——
**pane 看起来永久忙碌，而且没有任何东西会自己恢复它**。设计上没有 TTL 兜底。

恢复办法：

- **换一个 herdr pane**。状态是按 pane id 存的，新 pane = 新文件，`last_reported` 为空，
  `SessionStart` 立刻被正常接受
- **手动删掉那个 pane 的状态文件**（路径来源见 [6.3](#63-怎么观察)）：

  ```bash
  rm -f "${MINIMAX_DATA_DIR:-$HOME/.minimax}"/v2/plugin-data/hooks/mcode-herdr/w4_p3.json*
  ```

  ⚠️ **重跑 `./install.sh` 并不能解决这个问题**：`install.sh` 只 `rm -rf`
  `<profile>/plugins/mcode-herdr`（插件代码目录），**插件数据目录
  `<profile>/v2/plugin-data/hooks/mcode-herdr/` 在它之外，不会被清掉**。
  `./uninstall.sh` 同理。卸载重装也不会清。

### 3. `agent_session_id` 会被 herdr 丢弃

herdr 的 `session_ref_from_report` 先判 `is_official_agent_source`，
而后者是 17 对硬编码的 `(source, agent)` 白名单（`src/agent_resume.rs:327`），不含 mcode。
所以插件虽然照常上报了 `agent_session_id`，但对自定义 agent source 一律无效 ——
**无害，但也没用**。会话恢复能力**完全依赖 `resume_argv`**，即
[第 5 节](#5-恢复命令依赖-mcode-在-path-上) 那条命令。

### 4. herdr 没有 `done` 这个上报状态

插件只会报 `idle` / `working` / `blocked`（herdr `report-agent --state` 的合法值就是
`idle / working / blocked / unknown`）。**一轮做完就是报 `idle`**：
herdr 自己会在显示层把「`idle` + 未被查看」渲染成 `done`
（`src/app/agent_view.rs` 的 `status_name`）。这不影响 `herdr agent wait --until done`。

### 5. `uninstall.sh` 的 release `seq` 取自 `/proc/uptime`

herdr 丢弃 seq 不递增的上报，而插件用的是 `monotonic_ns()`（开机以来的纳秒）。
如果 `uninstall.sh` 用 `date +%s`（纪元纳秒）来生成 release 的 seq，
这个 `10^18` 量级的值会把该 pane 的 seq 高水位永久抬上去 —— 重装后插件的
monotonic seq 会被 herdr 判为过期而**全部静默丢弃**，插件看起来像彻底坏了。
所以卸载脚本用 `/proc/uptime` 换算出同一时钟族的时间（读不到时才退回纪元值）。
**维护提示：任何新增的 `release` 调用都必须遵守同一条约束。**

---

## 8. 仓库结构

```
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
│   ├── test_*.py
├── install.sh
├── uninstall.sh
└── docs/plans/                    # 设计文档与实现计划
```
