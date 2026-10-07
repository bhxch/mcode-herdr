# mcode-herdr 实现计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 交付一个 mcode 本地插件，把 mcode 的 agent 生命周期状态（idle/working/blocked）与会话恢复命令上报给 herdr，使其在侧边栏、`herdr agent list`、`herdr agent wait/prompt` 中成为一等公民。

**Architecture:** 插件通过 mcode 的生命周期钩子（SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / SessionEnd）感知状态。钩子进程一律「立即派生后台子进程并退出」，上报在后台完成，因此永不拖慢 mcode。状态机是纯函数，输入是（事件、载荷、上一状态），输出是（是否上报、上报什么状态、消息）。上报走双通道：优先 Unix socket 直连（与 herdr 官方集成一致），失败回落 `$HERDR_BIN_PATH` CLI。

**Tech Stack:** Python 3（标准库，无第三方依赖）、POSIX shell（安装脚本）、mcode 本地插件机制、herdr socket API / CLI。

---

## 背景事实（实现时不要重新调研）

以下均已在本机实测确认，可直接依赖：

**mcode 侧**（源码：`/share/rw/repo/github/minimax-code`）

- 插件包放在 `~/.minimax/plugins/<name>/`，**被发现即 installed + enabled**，不需要 `mcode plugin add`。
  `mcode plugin add <name>@local` 在 CLI 上不支持（`mutateLocal` 对 install 抛 `LOCAL_PLUGIN_INSTALL_UNSUPPORTED`）。
- `.minimax-plugin/plugin.json` 的 **`icon` 是必填字段**，缺失即
  `MANIFEST_SCHEMA_INVALID`，失败写进 `diagnostics` **静默跳过**，CLI 无任何提示。
  必须放一个真实的 png/jpg/jpeg/webp 文件并在清单里声明相对路径。
  `exampleQueries` / `apps` / `mcpServers` / `skills` 四个数组同样必填；`hooks` 可选。
  （原文此处引用了 bundle 里一个读取 `icon` 的内层函数名，该名字在 mcode 0.6.3 里
  grep 命中数为 0，无法核实，故只保留可核实的行为。）
- `category` 是枚举，合法值含 `Office / Studio / Design & Sites / Code / Business / Sales /
  Productivity / Science & Healthcare / Education / Other`。
- 钩子子进程环境是**按名字逐个挑出来的白名单**（`agent-modules/plugin-hooks/src/runner.ts:1771` 附近）：
  `PATH HOME LANG TERM SHELL USER TMPDIR TEMP TMP PATHEXT SystemRoot
  ComSpec USERPROFILE HOMEDRIVE HOMEPATH APPDATA LOCALAPPDATA`。
  **`HERDR_*` 不会传入**，必须靠 `/proc` 父进程链回溯。
  （原文此处把该白名单构造写成了一个带括号的函数名，同样 grep 命中数为 0，已去掉名字只留白名单内容。）
- 钩子拿到的环境变量还包括 `PLUGIN_ROOT`、`PLUGIN_DATA`（插件私有数据目录，mode 0700）。
- 事件载荷字段（实测）：
  - 公共：`hook_event_name`、`session_id`、`cwd`、`model`、`turn_id`、`transcript_path`
  - `SessionStart` 额外：`source`
  - `Stop` 额外：`stop_hook_active`、`last_assistant_message`
  - `PreToolUse`：`tool_name`、`tool_input`、`tool_use_id`
  - `PostToolUse`：同上 + `tool_response`
  - 工具类事件在子代理上下文中**额外带 `agent_id` / `agent_type`**（`runner.ts:1962`）
- `matcher` 可按工具名过滤（实测生效）。`ask_user` 的注册名见
  `packages/shared/src/questionnaire.ts:334` 的 `ASK_USER_TOOL_NAME = 'ask_user'`。
- 子代理拥有**独立的 session_id**（`plugin-hooks/src/coordinator.ts:536`）。
- **`ask_user` 不阻塞工具调用**（mcode 0.6.3 真机实测，11 个钩子事件全订阅）。同一轮问答：
  `PreToolUse` → 33ms 后 `PostToolUse`（`terminate=true`、`details.waiting_for_user=true`）
  → 56ms 后 `Stop`（turn 结束、问卷还开在 TUI 上）→ 35.8s 后用户作答，重新触发
  `UserPromptSubmit`（换了新的 `turn_id`）→ 该 turn 收尾。
  **解障信号只有「用户作答」那一次 `UserPromptSubmit`**；`PostToolUse` 带
  `waiting_for_user=true` 是「还在等」的权威信号，不是「已答」。详见设计文档 §2.9。
- **子代理没有 `ask_user` 工具**（mcode 0.6.3 真机实测）：`explore` 与 `mavis` 两种子代理
  都独立报告调不到，只能由顶层 agent 提问。所以「子代理提问也要报 blocked」的分支是
  防御性 / 前瞻性的，当前版本走不到。详见设计文档 §5.1。
- **herdr 环境必须在派生前由钩子进程自己解析**。后台 worker 一脱离就被 init 收养
  （`start_new_session` 不改父子关系），`/proc` 父链断在 `pid<=1` 的守卫上，
  `discover_herdr_env()` 恒返回 `None` → 钩子照常触发、状态文件一个都不写、
  `herdr agent list` 永久为空。真机上量到过 `worker MY_PPID = 1` 的现场。

**herdr 侧**（源码：`/share/rw/repo/github/herdr`，本机 0.9.3）

- CLI：`herdr pane report-agent <PANE> --source S --agent L --state idle|working|blocked|unknown
  [--message M] [--seq N] [--agent-session-id ID] [--agent-session-path P] [-- <RESUME>...]`
- socket API（newline-delimited JSON，`{"id","method","params"}` →
  `{"id","result":{"type":"ok"}}` 或 `{"id","error":{"code","message"}}`）：
  - `pane.report_agent`：必填 `pane_id / source / agent / state`；可选
    `message / seq / agent_session_id / agent_session_path / resume_argv`
  - `pane.release_agent`：必填 `pane_id / source / agent`；可选 `seq`
- **`--seq` 必须严格递增**（`src/terminal/state.rs:1965` 的 `hook_report_is_newer`）。
  **一旦用 seq 上报过，后续不带 seq 的报告会被静默丢弃**——包括 `release-agent`。
  所以**每次上报（含 release）都必须带 seq**。**用 `time.monotonic_ns()`，不要用
  `time.time_ns()`**：herdr 丢弃过期上报时照样回 ok，调用方无从察觉却已经记下状态，
  而状态机按 `last_reported` 去重会把后续重试全压掉；NTP 回步或校时能拨回墙上时钟。
- **`agent_session_id` 对自定义 agent 会被丢弃**：
  `src/agent_resume.rs:116` 的 `session_ref_from_report` 先判 `is_official_agent_source`，
  而后者是 17 对硬编码白名单（`src/agent_resume.rs:327`，本机源码逐条数过），不含 mcode。
  → 不要依赖它。**但 `resume_argv` 不受此限制**：`record_reported_resume`
  （`src/terminal/state.rs:1975`）只校验 `can_record_reported_resume`，
  即「当前 authority 的 source+agent 标签与我们完全一致」。所以恢复能力靠 `--` 后的恢复命令。
- `resume_argv` 校验（`src/agent_resume.rs:58`）：非空、≤64 个元素、总长 ≤8192 字节、
  无控制字符、**无单引号**、首元素必须是裸命令名（仅 `[A-Za-z0-9._-]`，不以 `-` 开头，不能是路径）。
- 一个 pane 只有一个 `hook_authority`；**不同 source 会被静默拒绝**，故 source 名要稳定。
- `done` 不是上报值，是 herdr 从 `idle + 未被查看` 派生的，所以完成时上报 `idle` 即可。
- 通知由状态跃迁派生并自带去重（`src/app/actions.rs:77`），同状态重复上报不会重复弹窗。

---

## 评审期间授权的偏离（本计划与已交付代码的差异）

下面每条都是**逐任务代码评审时明确授权的改动**：代码改了，本计划当时没回写。
现把清单与理由补在这里，正文各 Task 里的代码清单已按交付版本逐字同步。

| 位置 | 计划原本写的 | 交付版本 | 理由 |
|------|------------|---------|------|
| Task 2 `env.py` | `REQUIRED = ("HERDR_ENV", "HERDR_PANE_ID")` | `("HERDR_PANE_ID", "HERDR_BIN_PATH")` | `HERDR_ENV` 已被 `== "1"` 的前置判断覆盖，重复要求没有意义；`HERDR_BIN_PATH` 必须加上，否则 `transport` 的硬下标取值会抛 `KeyError`，而不是干净地不上报 |
| Task 4 `store.py` | `load`/`save` 各自加锁即等于读-改-写原子 | 新增 `_read`/`_write` 两个私有助手与公开的 `Store.update(pane_id, fn) -> Any` | flock 只分别串行化每一次 `load` 和每一次 `save`，**两者合起来不是临界区**；需要整体原子的调用方一律走 `update()`。另：临时文件改用 `os.open(..., 0o600)` + `os.fdopen` 落盘（`write_text` 会按 umask 落成 0644，把要拼进恢复命令的会话标识同机可读），并在 `finally` 里 unlink |
| Task 5 `decide.py` | `post-tool` 无条件清 `blocked`；`stop` 无条件翻 `idle` | `post-tool` 遇 `details.waiting_for_user == true` → 保持 `blocked`；`stop` 在 `blocked_by` 非空时忽略；`user-prompt` 加 `clear_blocked=True` | 真机时序证伪，见「背景事实」的 `ask_user` 条目与设计文档 §2.9。另给 `Decision` 补了不变式契约 docstring：`state != "blocked"` 的 REPORT 必带 `blocked_by=None`，消费方可无条件信任该字段 |
| Task 3 `payload.py` | 只有 `stop_hook_active` 一个布尔 | 新增 `waiting_for_user: bool = False` | 解析 `tool_response.details.waiting_for_user`；`tool_response` 缺失/`null`、缺 `details`、非布尔取值全部降级为 `False`，绝不抛 |
| Task 6 `transport.py` | 构造 socket 在守卫区外；读到换行为止；`"error" not in json.loads(...)`；`time.time_ns()` | socket 构造与 `settimeout` 移进守卫区；新增 `_MAX_REPLY_BYTES = 4096` 与整段读取的 `deadline`；只取首行且要求 JSON **对象**含 `result` 不含 `error`；`monotonic_ns()`；恢复命令校验失败时降级为「不带恢复命令继续上报」 | ①裸 `socket.socket()` 在 fd 耗尽时抛 `OSError`，异常会逃出 `report()` 且 `finally` 的 `close()` 无从执行。②`SOCKET_TIMEOUT` 只约束单次操作，滴字节的对端实测把调用方拖到 **2.30s**（名义 0.5s）。③旧的 `"error" not in ...` 让 `null` 应答抛 `TypeError` 逃出去，`[1,2]` 则被判成**成功**。④墙上时钟会被 NTP 回步拨回。⑤恢复命令是增强能力，状态转移才是主功能，不该一起赔掉（`validate_resume_argv` 本身仍抛） |
| Task 7 `runtime.py` | `blocking` 参数；`PLUGIN_DATA` 缺失回落 `/tmp` | 加 `proc_root` 参数、删 `blocking`；缺 `PLUGIN_DATA` 直接返回 0 | ①`proc_root` 让回放测试不必去翻真实 `/proc`、也不会写进开发者正在用的 pane；②`blocking` 无人使用，脱离后台化是靠派生 worker 进程做的；③状态文件名由 pane id 派生、可预测，而临时文件走 `os.open(O_CREAT)` 会跟随预置符号链接，在全局可写的 `/tmp` 里等于把状态 JSON 写进攻击者指定的文件 |
| Task 7 `herdr-report.py` | 直接派生 worker | **先在父进程里解析 herdr 环境**，经 `env=` 传给子进程；解析不到就不派生 | 承载性改动，不是洁癖：worker 一脱离就被 init 收养，`/proc` 祖先进程链随之消失，回溯恒返回 `None` —— 实测量到 `worker MY_PPID = 1`、状态文件一个都不写、`herdr agent list` 永久为空。另 `proc.returncode = 0` 用于压掉 `Popen.__del__` 的 `ResourceWarning`（它写 stderr，会污染钩子输出） |
| Task 9 `install.sh` | `rm -rf "$DEST"` 无守卫；用 `env -i /bin/sh -lc 'command -v mcode'` 判 PATH | `rm -rf` 前加绝对路径 + 末段名守卫；PATH 检查改为「先看当前环境，再看 herdr server 进程的 PATH」 | ①`DEST` 由 `$MINIMAX_DATA_DIR` / `$HOME` 推导，两者都可能为空或被写歪，`rm -rf` 不可撤销。②登录 shell **不读 `~/.bashrc`**，而本机 mcode 的 PATH 正是从那里加的，所以旧检查每次都误报；真正执行恢复命令的是 herdr server 的环境 |
| Task 8 清单 | 只强调 `icon` 必填 | 补记四个数组 `exampleQueries` / `apps` / `mcpServers` / `skills` 也必填，`hooks` 可选 | 缺字段的插件会被扫描器静默跳过（见「背景事实」） |
| Task 3/5/6/7 各测试 | 计划里的初版测试 | 已按交付版本逐字同步（回放测试从 `blocking=True` 改为 `proc_root=`；`decide`/`payload`/`transport`/`store` 补齐了守卫与并发用例） | 测试同样在评审中被重写。当前 102 个用例全绿 |

一处**刻意的省略**：Task 2 的 `env.py` 清单省略了模块 docstring，因为交付文件里那段
docstring 引用了一个在 mcode 0.6.3 bundle 里 grep 命中数为 0 的函数名。
`from __future__` 起的全部可执行行与交付文件逐行一致。

---

## 文件布局

```
/share/rw/repo/tools/mcode-herdr/
  plugin/                                  # 插件本体，安装时同步到 ~/.minimax/plugins/mcode-herdr/
    .minimax-plugin/plugin.json
    icon.png
    hooks/hooks.json
    scripts/
      herdr-report.py                      # 入口：读事件名与 stdin，先解析 herdr 环境再派生后台进程
      worker.py                            # 后台 worker，调 runtime.main()
      mcode_herdr/
        __init__.py
        env.py                             # /proc 父链回溯取 HERDR_*
        payload.py                         # 解析 stdin JSON
        store.py                           # 每 pane 状态，flock + 原子写
        decide.py                          # 纯函数状态机
        transport.py                       # socket 优先、CLI 回落
        herdr.py                           # 协议常量与 argv 拼装
        runtime.py                         # 串起「事件 → decide → transport → 落盘」
  test/
    run-tests.sh
    fixtures/*.json                        # 载荷样本，来源逐项见 fixtures/README.md
    fake-herdr/herdr                       # 假 herdr 二进制：不落盘，由测试在临时目录里现写
    test_env.py
    test_payload.py
    test_store.py
    test_decide.py
    test_transport.py
    test_replay.py                         # 端到端回放：喂真实载荷，断言调用序列
  probe/                                   # 载荷探针插件，只 dump stdin，用完即卸
  install.sh
  uninstall.sh
  README.md
  docs/plans/
```

---

## Task 1: 仓库骨架

**Files:**
- Create: `.gitignore`
- Create: `plugin/scripts/mcode_herdr/__init__.py`

**Step 1: 写 .gitignore**

```
.temp/
*.log
__pycache__/
*.pyc
```

**Step 2: 建包目录与空 `__init__.py`**

Run: `mkdir -p plugin/scripts/mcode_herdr && touch plugin/scripts/mcode_herdr/__init__.py`

**Step 3: 提交**

```bash
git add .gitignore plugin/scripts/mcode_herdr/__init__.py
git commit -m "chore(plugin): scaffold mcode-herdr plugin layout"
```

---

## Task 2: 环境发现模块 `env.py`

**Files:**
- Create: `plugin/scripts/mcode_herdr/env.py`
- Test: `test/test_env.py`

**Step 1: 写失败的测试**

```python
# test/test_env.py
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcode_herdr.env import MAX_ANCESTRY, discover_herdr_env, read_environ, read_ppid

HERDR_ENV_VARS = [
    "HERDR_ENV=1",
    "HERDR_PANE_ID=w1:p1",
    "HERDR_BIN_PATH=/opt/herdr",
    "HERDR_SOCKET_PATH=/tmp/s",
]


class EnvDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_proc(self, pid, ppid, env):
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        (d / "environ").write_bytes(("\0".join(env) + "\0").encode())
        (d / "status").write_text(f"Name:\tpid\nPPid:\t{ppid}\n")
        return d

    def _make_chain(self, base, length, top_env):
        """造 base -> base+1 -> ... 的父链，只有链顶带 top_env，返回链顶 pid。"""
        for i in range(length - 1):
            self._make_proc(base + i, base + i + 1, ["PATH=/bin"])
        self._make_proc(base + length - 1, 1, top_env)
        return base + length - 1

    def test_walks_up_until_herdr_env_found(self):
        # 100 -> 101 -> 102，HERDR 只在 102，起点自身不带，必须真回溯两步
        self._make_proc(100, 101, ["PATH=/bin"])
        self._make_proc(101, 102, ["PATH=/bin"])
        self._make_proc(102, 1, HERDR_ENV_VARS)
        got = discover_herdr_env(proc_root=self.proc, start_pid=100)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")
        self.assertEqual(got["HERDR_SOCKET_PATH"], "/tmp/s")

    def test_defaults_to_own_pid(self):
        # 不传 start_pid 时，回溯起点取 os.getpid()
        self._make_proc(100, 1, HERDR_ENV_VARS)
        with mock.patch("os.getpid", return_value=100):
            got = discover_herdr_env(proc_root=self.proc)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")

    def test_returns_only_herdr_prefixed_vars(self):
        # 前缀过滤是安全边界：祖先环境可能带密钥，不能整体外泄给下游
        self._make_proc(100, 1, ["PATH=/bin", "AWS_SECRET_ACCESS_KEY=s3cr3t", *HERDR_ENV_VARS])
        got = discover_herdr_env(proc_root=self.proc, start_pid=100)
        self.assertIsNotNone(got)
        self.assertEqual(sorted(got), ["HERDR_BIN_PATH", "HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH"])

    def test_ignores_herdr_env_not_equal_one(self):
        # 2/3 长得像但 HERDR_ENV 不为 1，4 才是真祖先；断言命中 4 而不是 None，
        # 才同时钉住「拒绝坏的」和「继续往上走」，用 pid 1 当诱饵会被守卫短路掉
        self._make_proc(2, 3, ["PATH=/bin", "HERDR_ENV=0",
                               "HERDR_PANE_ID=w1:bad", "HERDR_BIN_PATH=/opt/herdr"])
        self._make_proc(3, 4, ["PATH=/bin", "HERDR_ENV=",
                               "HERDR_PANE_ID=w1:bad", "HERDR_BIN_PATH=/opt/herdr"])
        self._make_proc(4, 1, ["PATH=/bin", "HERDR_ENV=1",
                               "HERDR_PANE_ID=w1:good", "HERDR_BIN_PATH=/opt/herdr"])
        got = discover_herdr_env(proc_root=self.proc, start_pid=2)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:good")

    def test_requires_non_empty_herdr_pane_id(self):
        # REQUIRED 里的每个变量都必须非空：PANE_ID 缺了认不出是哪个 pane，
        # BIN_PATH 缺了下游用硬下标取值会 KeyError，报告被静默吞掉
        cases = [
            ["HERDR_ENV=1", "HERDR_PANE_ID=", "HERDR_BIN_PATH=/opt/herdr"],
            ["HERDR_ENV=1", "HERDR_BIN_PATH=/opt/herdr"],
            ["HERDR_ENV=1", "HERDR_PANE_ID=w1:p1", "HERDR_BIN_PATH="],
            ["HERDR_ENV=1", "HERDR_PANE_ID=w1:p1"],
        ]
        for i, env in enumerate(cases):
            with self.subTest(env=env):
                self._make_proc(100 + i, 1, ["PATH=/bin", *env])
                self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=100 + i))

    def test_finds_env_at_max_ancestry_depth(self):
        # 链长正好 MAX_ANCESTRY，HERDR 在链顶，最后一次迭代正好命中
        self._make_chain(200, MAX_ANCESTRY, HERDR_ENV_VARS)
        got = discover_herdr_env(proc_root=self.proc, start_pid=200)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")

    def test_gives_up_beyond_max_ancestry(self):
        # 链长 MAX_ANCESTRY+1，链顶有 HERDR 但预算耗尽，必须放弃而不是无限往上
        self._make_chain(300, MAX_ANCESTRY + 1, HERDR_ENV_VARS)
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=300))

    def test_does_not_inspect_pid_1(self):
        # pid 1 是 init，就算它带着 HERDR 也不能认成 mcode 祖先（守卫在读 environ 之前）
        self._make_proc(2, 1, ["PATH=/bin"])
        self._make_proc(1, 0, ["PATH=/bin", *HERDR_ENV_VARS])
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=2))

    def test_returns_none_when_no_herdr_ancestor(self):
        self._make_proc(100, 101, ["PATH=/bin"])
        self._make_proc(101, 1, ["PATH=/bin"])
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=100))

    def test_reads_ppid_from_status_not_stat(self):
        # comm 可能含空格/括号，stat 按位置解析必错位；给个冲突值确保只读 status
        d = self._make_proc(7, 5, ["PATH=/bin"])
        (d / "stat").write_text("7 (m c code) S 1 999 1 0")
        self.assertEqual(read_ppid(7, self.proc), 5)

    def test_read_ppid_missing_or_malformed(self):
        d = self._make_proc(7, 5, ["PATH=/bin"])
        (d / "status").write_text("Name:\tpid\nState:\tS (sleeping)\n")
        self.assertIsNone(read_ppid(7, self.proc))
        self.assertIsNone(read_ppid(4242, self.proc))

    def test_read_environ_skips_malformed_entries(self):
        d = self._make_proc(7, 5, ["PATH=/bin"])
        d.joinpath("environ").write_bytes(b"NOEQUALS\0PATH=/bin\0HERDR_PANE_ID=w1:p=1\0BAD\xffKEY=v\0\0")
        self.assertEqual(read_environ(7, self.proc), {
            "PATH": "/bin",
            "HERDR_PANE_ID": "w1:p=1",
            "BAD\ufffdKEY": "v",
        })

    def test_read_environ_missing_proc_returns_empty(self):
        self.assertEqual(read_environ(4242, self.proc), {})


if __name__ == "__main__":
    unittest.main()
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_env -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.env'`

**Step 3: 写实现**

（下面的清单从 `from __future__ import annotations` 起，与交付文件逐行一致。
交付文件的模块 docstring 被刻意省略：它引用了一个在 mcode 0.6.3 bundle 里
grep 命中数为 0 的函数名。）

```python
# plugin/scripts/mcode_herdr/env.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

MAX_ANCESTRY = 12
REQUIRED = ("HERDR_PANE_ID", "HERDR_BIN_PATH")


def read_environ(pid: int, proc_root: Path) -> dict:
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except (OSError, ValueError):
        return {}
    out = {}
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        text = chunk.decode("utf-8", "replace")
        key, sep, value = text.partition("=")
        if sep:
            out[key] = value
    return out


def read_ppid(pid: int, proc_root: Path) -> Optional[int]:
    try:
        for line in (proc_root / str(pid) / "status").read_text(errors="replace").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def discover_herdr_env(proc_root: Path = Path("/proc"), start_pid: Optional[int] = None) -> Optional[Mapping[str, str]]:
    """向上回溯，找到第一个 HERDR_ENV=1 且 HERDR_PANE_ID、HERDR_BIN_PATH 均非空的祖先。

    找不到时返回 None —— 调用方据此完全静默退出。
    """
    pid = os.getpid() if start_pid is None else start_pid
    proc_root = Path(proc_root)
    for _ in range(MAX_ANCESTRY):
        if pid is None or pid <= 1:
            return None
        env = read_environ(pid, proc_root)
        if env.get("HERDR_ENV") == "1" and all(env.get(name) for name in REQUIRED):
            return {k: v for k, v in env.items() if k.startswith("HERDR_")}
        pid = read_ppid(pid, proc_root)
    return None
```

**Step 4: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_env -v`
Expected: PASS 13 tests（`test/test_env.py` 实际用例数）

**Step 5: 提交**

```bash
git add plugin/scripts/mcode_herdr/env.py test/test_env.py
git commit -m "feat(env): 从 /proc 父链回溯取回 herdr 环境"
```

---

## Task 3: 载荷解析 `payload.py`

**Files:**
- Create: `plugin/scripts/mcode_herdr/payload.py`
- Create: `test/fixtures/session_start.json`
- Create: `test/fixtures/pre_tool_root.json`
- Create: `test/fixtures/pre_tool_subagent.json`
- Create: `test/fixtures/post_tool_root.json`
- Create: `test/fixtures/post_tool_ask_user_waiting.json`
- Create: `test/fixtures/stop.json`
- Test: `test/test_payload.py`

**Step 1: 写入真实载荷样本**

`test/fixtures/session_start.json`（实测原文）：

```json
{"source":"startup","hook_event_name":"SessionStart","session_id":"mvs_d662a0f1f8014fd7ac52237ed3d75a47","transcript_path":"/tmp/minimax-plugin-hooks/transcripts/9932ea9f4713af271531c1d8ff1ee4a2.compatible.jsonl","cwd":"/share/rw/repo/test","model":"MiniMax-M3.1-Flash-Preview","permission_mode":"auto"}
```

`test/fixtures/pre_tool_root.json`（实测原文）：

```json
{"tool_name":"bash","tool_input":{"command":"echo herdr-probe-ok","description":"Print probe string"},"tool_use_id":"call_function_bgtgms728wl3_1","hook_event_name":"PreToolUse","session_id":"mvs_c65b1e8d72184dc6b81ba6284bd37594","turn_id":"turn_muw8nbkb_0zs0vj","transcript_path":"/tmp/x.compatible.jsonl"}
```

`test/fixtures/pre_tool_subagent.json`（据 `runner.ts:1962` 的 subagentFields 构造）：

```json
{"agent_id":"mvs_child_123","agent_type":"explore","tool_name":"ask_user","tool_input":{"questions":[]},"tool_use_id":"call_function_child_1","hook_event_name":"PreToolUse","session_id":"mvs_child_123","turn_id":"turn_child","transcript_path":"/tmp/y.compatible.jsonl"}
```

`test/fixtures/post_tool_root.json`：

```json
{"agent_id":"","tool_name":"ask_user","tool_input":{"questions":[]},"tool_response":{"content":[]},"tool_use_id":"call_function_root_2","hook_event_name":"PostToolUse","session_id":"mvs_c65b1e8d72184dc6b81ba6284bd37594","turn_id":"turn_muw8nbkb_0zs0vj"}
```

`test/fixtures/stop.json`（实测原文）：

```json
{"stop_hook_active":false,"last_assistant_message":"收到","hook_event_name":"Stop","session_id":"mvs_d662a0f1f8014fd7ac52237ed3d75a47","turn_id":"turn_muw8m3gu_ch6uv8","transcript_path":"/tmp/z.compatible.jsonl","cwd":"/share/rw/repo/test","model":"MiniMax-M3.1-Flash-Preview","permission_mode":"auto"}
```

`test/fixtures/post_tool_ask_user_waiting.json`（mcode 0.6.3 真机会话**实时抓到**的
`ask_user` `PostToolUse`；这是纠正整条状态机的关键证据 —— `terminate=true` 且
`waiting_for_user=true` 说明 turn 已收工而问卷还开着。摘录时只改写了问题正文与
`tool_use_id` / `session_id` / `turn_id`，`tool_response` 为线上原值）：

```json
{"tool_name":"ask_user","tool_input":{"mode":"questionnaire","requiresExplicitResponse":true,"title":"选择下一步实现方案","steps":[{"id":"step_1","question":"用哪种方案？","options":[{"id":"opt_a","label":"方案 A"},{"id":"opt_b","label":"方案 B"}]}]},"tool_response":{"content":[{"type":"text","text":"Questionnaire ask_55b68e7cd694d64aed6910a3 is waiting for the local user. Stop this turn until the user replies."}],"details":{"request_id":"ask_55b68e7cd694d64aed6910a3","schema_version":2,"step_count":1,"waiting_for_user":true},"terminate":true},"tool_use_id":"call_7fk3n1ab2de94c60","hook_event_name":"PostToolUse","session_id":"mvs_9b41e0c7d5f84a2eb3c6d90f17a48b25","turn_id":"turn_5y2r8tqh_1m4kz9v","cwd":"/share/rw/repo/test","model":"MiniMax-M3.1-Flash-Preview","permission_mode":"auto"}
```

**Step 2: 写失败的测试**

```python
# test/test_payload.py
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
```

**Step 3: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_payload -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.payload'`

**Step 4: 写实现**

```python
# plugin/scripts/mcode_herdr/payload.py
"""把 mcode 钩子的 stdin JSON 解析成 Payload。

字段名来自实测（见设计文档 §2.6/§2.8），解析保持宽容：
缺失、空字符串、纯空白字符串一律归一为 None，非空字符串去掉首尾空白，
避免下游到处判空，也避免带空白的 id 被原样拼进 mcode 恢复命令。

布尔标记同理只认真布尔值：waiting_for_user 取自
tool_response.details.waiting_for_user，是 ask_user 问卷是否仍在等人回答的权威信号
（实测 ask_user 立刻带 terminate=true + waiting_for_user=true 返回，问卷还开着），
缺失、null 或任何非布尔取值一律归一为 False，绝不抛。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Payload:
    event: str
    session_id: Optional[str]
    tool_name: Optional[str]
    agent_id: Optional[str]
    agent_type: Optional[str]
    source: Optional[str]
    cwd: Optional[str]
    stop_hook_active: bool
    waiting_for_user: bool = False


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _waiting_for_user(data: dict) -> bool:
    """取 tool_response.details.waiting_for_user，逐层缺失/null 都退化成 False。

    逐层 isinstance 是为了容忍线上真实存在的几种形态：老版本 ask_user 根本不回
    tool_response、tool_response 里没有 details、details 为 null。任何一层不合预期
    都只意味着「没证据说还在等」，此时按 False 走原有的清障路径，不会静默卡死在 blocked。
    """
    response = data.get("tool_response")
    if not isinstance(response, dict):
        return False
    details = response.get("details")
    if not isinstance(details, dict):
        return False
    value = details.get("waiting_for_user")
    return value if isinstance(value, bool) else False


def load_payload(raw: str) -> Payload:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"hook payload is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("hook payload must be a JSON object")
    event = _text(data.get("hook_event_name"))
    if event is None:
        raise ValueError("hook payload missing hook_event_name")
    return Payload(
        event=event,
        session_id=_text(data.get("session_id")),
        tool_name=_text(data.get("tool_name")),
        agent_id=_text(data.get("agent_id")),
        agent_type=_text(data.get("agent_type")),
        source=_text(data.get("source")),
        cwd=_text(data.get("cwd")),
        stop_hook_active=bool(data.get("stop_hook_active")),
        waiting_for_user=_waiting_for_user(data),
    )
```

**Step 5: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_payload -v`
Expected: PASS 16 tests（`test/test_payload.py` 实际用例数）

**Step 6: 提交**

```bash
git add plugin/scripts/mcode_herdr/payload.py test/
git commit -m "feat(payload): 解析 mcode 钩子载荷为强类型 Payload"
```

---

## Task 4: 每 pane 状态存储 `store.py`

**Files:**
- Create: `plugin/scripts/mcode_herdr/store.py`
- Test: `test/test_store.py`

**Step 1: 写失败的测试**

```python
# test/test_store.py
import json
import multiprocessing
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mcode_herdr.store import PaneState, Store

# 写进程每次写一份可辨认的大载荷：直写目的地时读者能看到截断或半截内容，
# tmp + rename 时读者只能看到某一份完整值。载荷要够大，直写才会留出可见的窗口。
PAYLOAD_SIZE = 512 * 1024
WRITERS = 4
ROUNDS_PER_WRITER = 30
# 丢更新的窗口靠 fn 里的 sleep 撑开。持锁正确时 4 个进程被完全串行化，
# 这一项约 (WRITERS * BUMP_ROUNDS * BUMP_SLEEP_S) ≈ 0.3s。
BUMP_SLEEP_S = 0.01
BUMP_ROUNDS = 8
# 两个并发测试的总时长上限，卡住时判失败而不是挂住 CI。
DEADLINE_S = 30.0


def _writer(base, pane_id, tag, rounds):
    """后台子进程：反复 save 同一 pane，每次一个可辨认的大载荷。"""
    store = Store(Path(base))
    filler = "x" * PAYLOAD_SIZE
    for i in range(rounds):
        store.save(pane_id, PaneState(root_session="%s-%d" % (tag, i), last_reported=filler))


def _bumper(base, pane_id, rounds, sink):
    """后台子进程：在 update() 里对同一计数器自增，把每次读到的值交回父进程。"""
    store = Store(Path(base))
    seen = []
    for _ in range(rounds):
        def bump(state):
            time.sleep(BUMP_SLEEP_S)  # 撑开读改写窗口，让丢更新必现而非偶发
            nxt = int(state.blocked_by or 0) + 1
            state.blocked_by = str(nxt)
            return nxt
        seen.append(store.update(pane_id, bump))
    sink.put(seen)


def _spawn(ctx, target, args):
    # 用 fork：模块已硬依赖 fcntl，测试同样只在 POSIX 上跑，省掉子进程重新导入
    # test.test_store 的麻烦（test 目录还没有 __init__.py）。
    proc = ctx.Process(target=target, args=args)
    proc.start()
    return proc


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _temp_leftovers(self, base):
        return sorted(p.name for p in base.iterdir() if ".tmp." in p.name)

    def _join_within(self, procs):
        """按总时限收子进程；超时就判失败，避免挂住 CI。"""
        deadline = time.monotonic() + DEADLINE_S
        for proc in procs:
            proc.join(max(0.0, deadline - time.monotonic()))
            if proc.is_alive():
                proc.terminate()
                proc.join()
                self.fail("并发子进程超时未退出")

    def test_missing_file_gives_default_state(self):
        state = self.store.load("w1:p1")
        self.assertIsNone(state.root_session)
        self.assertIsNone(state.blocked_by)
        self.assertIsNone(state.last_reported)

    def test_round_trip(self):
        self.store.save("w1:p1", PaneState(root_session="s1", blocked_by="s1", last_reported="blocked"))
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "s1")
        self.assertEqual(got.blocked_by, "s1")
        self.assertEqual(got.last_reported, "blocked")

    def test_panes_are_isolated(self):
        self.store.save("w1:p1", PaneState(root_session="a"))
        self.store.save("w1:p2", PaneState(root_session="b"))
        self.assertEqual(self.store.load("w1:p1").root_session, "a")
        self.assertEqual(self.store.load("w1:p2").root_session, "b")

    def test_corrupt_file_degrades_to_default(self):
        path = self.store.path_for("w1:p1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")
        self.assertIsNone(self.store.load("w1:p1").root_session)

    def test_non_dict_json_degrades_to_default(self):
        # 合法 JSON 但不是对象：parse 不报错，若直接 data.get 会 AttributeError 崩掉钩子
        path = self.store.path_for("w1:p1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]")
        got = self.store.load("w1:p1")
        self.assertIsNone(got.root_session)
        self.assertIsNone(got.blocked_by)
        self.assertIsNone(got.last_reported)

    def test_save_leaves_no_temp_file(self):
        self.store.save("w1:p1", PaneState(root_session="a"))
        path = self.store.path_for("w1:p1")
        keep = {path.name, path.with_suffix(".lock").name}
        leftovers = [p.name for p in path.parent.iterdir() if p.name not in keep]
        self.assertEqual(leftovers, [])

    def test_save_cleans_up_temp_file_when_replace_fails(self):
        # 写完临时文件、replace 之前失败时，pid 后缀的临时文件会永久留在盘上；
        # PLUGIN_DATA 可能是共享 /tmp，攒起来的垃圾还会把状态目录撑大
        with mock.patch("os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.save("w1:p1", PaneState(root_session="a"))
        self.assertEqual(self._temp_leftovers(self.store.base), [])

    def test_state_file_is_owner_only(self):
        # 状态里是要拼进 `mcode --session <id>` 恢复命令的会话标识，跟锁文件一样
        # 只给属主读写；write_text 会按 umask 落成 0644
        self.store.save("w1:p1", PaneState(root_session="s1"))
        mode = stat.S_IMODE(self.store.path_for("w1:p1").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_pane_id_with_path_traversal_stays_inside_base(self):
        # pane_id 来自环境变量，非可信输入；不截断成单段目录名就能借它写到 base 之外
        path = self.store.path_for("../../etc/passwd")
        self.assertEqual(path.parent, self.store.base)
        self.assertEqual(path.name, ".._.._etc_passwd.json")
        self.assertTrue(path.resolve().is_relative_to(self.store.base.resolve()))

    def test_save_creates_base_dir_on_demand(self):
        # PLUGIN_DATA 指向的目录不一定已经被谁创建过
        store = Store(Path(self.tmp.name) / "nested" / "state")
        self.assertFalse(store.base.exists())
        store.save("w1:p1", PaneState(root_session="a"))
        self.assertEqual(store.load("w1:p1").root_session, "a")

    def test_update_persists_transformed_state_and_returns_callback_value(self):
        # 决策在锁内基于最新状态算出，返回值是调用方要拿去上报的东西
        def decide(state):
            state.root_session = state.root_session or "s-root"
            state.blocked_by = "s-blocker"
            return "working"

        self.assertEqual(self.store.update("w1:p1", decide), "working")
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "s-root")
        self.assertEqual(got.blocked_by, "s-blocker")

    def test_update_leaves_state_untouched_when_callback_raises(self):
        # 上报参数非法时决策函数抛错，此时不能把改了一半的状态落盘
        self.store.save("w1:p1", PaneState(root_session="s1"))

        def boom(state):
            state.blocked_by = "half-written"
            raise ValueError("bad resume argv")

        with self.assertRaises(ValueError):
            self.store.update("w1:p1", boom)
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "s1")
        self.assertIsNone(got.blocked_by)

    def test_update_serializes_concurrent_read_modify_write(self):
        # 真正丢更新的形状：4 个进程各 8 次自增。update() 必须在一次持锁内完成
        # load→fn→save，否则互相覆盖，计数既不连续也不到 32。断言的是「各进程
        # 读到的值合起来正好是 1..32、无重复无缺口」，比只查终值更紧。
        ctx = multiprocessing.get_context("fork")
        base, pane = str(self.store.base), "w1:p1"
        sink = ctx.SimpleQueue()
        procs = [_spawn(ctx, _bumper, (base, pane, BUMP_ROUNDS, sink)) for _ in range(WRITERS)]
        try:
            self._join_within(procs)
            seen = []
            while not sink.empty():
                seen.extend(sink.get())
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                proc.join()
        self.assertEqual(sorted(seen), list(range(1, WRITERS * BUMP_ROUNDS + 1)))
        self.assertEqual(self.store.load(pane).blocked_by, str(WRITERS * BUMP_ROUNDS))

    def test_concurrent_writers_never_observed_torn(self):
        # 读者刻意绕开 flock 直接读文件：「读者永远看到完整 JSON」是 tmp + rename
        # 的承诺，herdr CLI 和人肉排查都这么读。若改走 store.load()，锁本身就把并发
        # 读挡住了，把 save 换成直写目的地也照样全绿，测不出原子性。
        base, pane = str(self.store.base), "w1:p1"
        self.store.save(pane, PaneState(root_session="seed"))
        path = self.store.path_for(pane)
        ctx = multiprocessing.get_context("fork")
        complete = {
            json.dumps({"root_session": "w%d-%d" % (tag, i), "blocked_by": None, "last_reported": "x" * PAYLOAD_SIZE},
                       sort_keys=True): None
            for tag in range(WRITERS) for i in range(ROUNDS_PER_WRITER)
        }
        # 起手先落一份合法状态，免得读到「文件还不存在」这种非竞态的失败
        complete[json.dumps({"root_session": "seed", "blocked_by": None, "last_reported": None},
                            sort_keys=True)] = None
        procs = [_spawn(ctx, _writer, (base, pane, "w%d" % tag, ROUNDS_PER_WRITER)) for tag in range(WRITERS)]
        reads = 0
        deadline = time.monotonic() + DEADLINE_S
        try:
            while any(proc.is_alive() for proc in procs):
                self.assertLess(time.monotonic(), deadline, "并发读超时")
                raw = path.read_text()
                # 半截文档既 parse 不过，也对不上任何一份完整值
                self.assertIn(json.dumps(json.loads(raw), sort_keys=True), complete)
                reads += 1
            for proc in procs:
                self.assertEqual(proc.exitcode, 0)
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                proc.join()
        self.assertGreater(reads, 0, "没读到任何一次，测试没意义")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.store'`

**Step 3: 写实现**

```python
# plugin/scripts/mcode_herdr/store.py
"""每个 pane 一份状态，写入加 flock 并原子替换。

并发来自「每次钩子派生独立后台进程」，多个进程可能同时读到同一旧状态。
flock 只分别串行化每一次 load 和每一次 save；load 与 save 合起来并不构成一个
临界区，两者之间可以夹进别的进程的写，调用方的读-改-写会丢更新。
tmp + rename 是另一层独立保证：读者永远看到完整 JSON，不会读到半截文件。
需要「读最新值 → 决定 → 写回」整体不被穿插的写覆盖时，走 update()。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class PaneState:
    root_session: Optional[str] = None
    blocked_by: Optional[str] = None
    last_reported: Optional[str] = None


class Store:
    def __init__(self, base: Path):
        self.base = Path(base)

    def path_for(self, pane_id: str) -> Path:
        return self.base / f"{_SAFE.sub('_', pane_id)}.json"

    @contextmanager
    def _locked(self, pane_id: str):
        path = self.path_for(pane_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.with_suffix(".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _read(self, path: Path) -> PaneState:
        """读一个状态文件；调用方须已持锁。"""
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return PaneState()
        if not isinstance(data, dict):
            return PaneState()
        return PaneState(
            root_session=data.get("root_session") or None,
            blocked_by=data.get("blocked_by") or None,
            last_reported=data.get("last_reported") or None,
        )

    def _write(self, path: Path, state: PaneState) -> None:
        """原子替换一个状态文件；调用方须已持锁。"""
        tmp = path.with_suffix(".json.tmp.%d" % os.getpid())
        try:
            # 不用 write_text：它按 umask 落成 0644，而状态里是要拼进
            # `mcode --session <id>` 恢复命令的会话标识，不该同机可读。
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(asdict(state)))
            os.replace(tmp, path)
        finally:
            # 成功时 tmp 已随 rename 消失，missing_ok 让这条清理不影响成功路径。
            tmp.unlink(missing_ok=True)

    def load(self, pane_id: str) -> PaneState:
        with self._locked(pane_id) as path:
            return self._read(path)

    def save(self, pane_id: str, state: PaneState) -> None:
        with self._locked(pane_id) as path:
            self._write(path, state)

    def update(self, pane_id: str, fn: Callable[[PaneState], Any]) -> Any:
        """在一次持锁内完成 load → fn → save，读改写整体对同 pane 的其他进程原子。

        返回 fn 的返回值：决策通常在锁内基于最新状态算出，调用方要拿它去做
        上报，光拿写回后的状态不够。fn 抛异常时原样抛出且不落盘。

        fn 不可重入：flock 绑在每次 open 的文件描述上，同一 pane 在 fn 里再
        调本 store（load/update）会自死锁，并把该 pane 的锁永久泄漏给后续钩子。
        """
        with self._locked(pane_id) as path:
            state = self._read(path)
            result = fn(state)
            self._write(path, state)
            return result
```

**Step 4: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_store -v`
Expected: PASS 14 tests（`test/test_store.py` 实际用例数）

**Step 5: 提交**

```bash
git add plugin/scripts/mcode_herdr/store.py test/test_store.py
git commit -m "feat(store): 加锁原子保存每 pane 的会话与 blocked 归属"
```

---

## Task 5: 状态机 `decide.py`（核心，纯函数）

**Files:**
- Create: `plugin/scripts/mcode_herdr/decide.py`
- Test: `test/test_decide.py`

**Step 1: 写失败的测试**

```python
# test/test_decide.py
import unittest

from mcode_herdr.decide import Action, Decision, decide
from mcode_herdr.payload import Payload
from mcode_herdr.store import PaneState

ROOT = "sess-root"
CHILD = "sess-child"


def p(event, session_id=ROOT, tool_name=None, agent_id=None, source=None,
      waiting_for_user=False):
    return Payload(event=event, session_id=session_id, tool_name=tool_name,
                   agent_id=agent_id, agent_type=None, source=source,
                   cwd="/tmp", stop_hook_active=False,
                   waiting_for_user=waiting_for_user)


class DecideTest(unittest.TestCase):
    def test_session_start_adopts_root_and_reports_idle(self):
        d = decide(Action("session-start"), p("SessionStart", source="startup"), PaneState())
        self.assertEqual(d.kind, Decision.Kind.REPORT)
        self.assertEqual(d.state, "idle")
        self.assertTrue(d.attach_session)
        self.assertTrue(d.resume)

    def test_session_start_without_session_id_ignored(self):
        # payload 会把缺失/空白 session_id 归一成 None，这条路径真实可达
        self.assertIsNone(decide(Action("session-start"), p("SessionStart", session_id=None), PaneState()))

    def test_session_start_while_working_is_subagent_and_ignored(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("session-start"), p("SessionStart", session_id=CHILD), st))

    def test_session_start_while_idle_adopts_new_root(self):
        st = PaneState(root_session=ROOT, last_reported="idle")
        d = decide(Action("session-start"), p("SessionStart", session_id="sess-new"), st)
        self.assertEqual(d.kind, Decision.Kind.REPORT)
        self.assertEqual(d.new_root_session, "sess-new")

    def test_user_prompt_root_reports_working(self):
        d = decide(Action("user-prompt"), p("UserPromptSubmit"), PaneState(root_session=ROOT))
        self.assertEqual(d.state, "working")

    def test_user_prompt_from_child_ignored(self):
        # last_reported 必须取 "idle"：守卫被删掉时无守卫版本会报 "working"，
        # 若这里取 "working" 就会被 _report 的去重吞掉，测试假绿、守卫失去覆盖。
        st = PaneState(root_session=ROOT, last_reported="idle")
        self.assertIsNone(decide(Action("user-prompt"), p("UserPromptSubmit", session_id=CHILD), st))

    def test_user_prompt_from_root_clears_pending_blocked_by_child(self):
        # 用户在父会话提了新问题，子代理待答的提问已被接管，不该再算有人被阻塞
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        d = decide(Action("user-prompt"), p("UserPromptSubmit"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)

    def test_ask_user_pretool_root_reports_blocked(self):
        d = decide(Action("pre-tool"), p("PreToolUse", tool_name="ask_user"), PaneState(root_session=ROOT))
        self.assertEqual(d.state, "blocked")
        self.assertEqual(d.blocked_by, ROOT)
        self.assertEqual(d.message, "等待你的决策")  # herdr 通知里给用户看的文案

    def test_ask_user_pretool_from_subagent_also_reports_blocked(self):
        # 人确实被问了，子代理提问同样必须报 blocked
        st = PaneState(root_session=ROOT, last_reported="working")
        d = decide(Action("pre-tool"), p("PreToolUse", session_id=CHILD, tool_name="ask_user", agent_id=CHILD), st)
        self.assertEqual(d.state, "blocked")
        self.assertEqual(d.blocked_by, CHILD)

    def test_ask_user_pretool_without_session_id_ignored(self):
        # 没有 session_id 就无从记 blocked_by，这个守卫必须挡住，不许上报 blocked
        self.assertIsNone(decide(Action("pre-tool"), p("PreToolUse", tool_name="ask_user", session_id=None), PaneState(root_session=ROOT)))

    def test_non_ask_user_pretool_ignored(self):
        self.assertIsNone(decide(Action("pre-tool"), p("PreToolUse", tool_name="bash"), PaneState(root_session=ROOT)))

    def test_posttool_clears_only_own_blocked(self):
        # waiting_for_user=False 才代表问卷确实被答掉了（作答会让 ask_user 带着
        # 已完成的 tool_response 真正返回，或直接以 UserPromptSubmit 续上新 turn）
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        d = decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)

    def test_posttool_while_questionnaire_open_keeps_blocked(self):
        """P0 回归：ask_user 带 waiting_for_user 立刻返回时，问卷还开着，绝不清障。

        实测时序 PostToolUse(+33ms) → Stop(+56ms)，turn 在人作答前就结束了。
        原实现在这里翻 working，blocked 只存在 121ms 就被抹掉。
        last_reported 取 "blocked"：无守卫版本会报 "working"，不会被去重吞掉。
        """
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        d = decide(Action("post-tool"),
                   p("PostToolUse", tool_name="ask_user", waiting_for_user=True), st)
        self.assertIsNone(d)

    def test_posttool_while_questionnaire_open_from_child_also_keeps_blocked(self):
        # 子代理提问同一条路：blocked_by 记的是子会话，守卫与等待态都要生效
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        d = decide(Action("post-tool"),
                   p("PostToolUse", session_id=CHILD, tool_name="ask_user",
                     agent_id=CHILD, waiting_for_user=True), st)
        self.assertIsNone(d)

    def test_posttool_from_other_session_cannot_clear_root_blocked(self):
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        self.assertIsNone(decide(Action("post-tool"), p("PostToolUse", session_id=CHILD, tool_name="ask_user"), st))

    def test_posttool_without_blocked_by_ignored(self):
        # 没人被阻塞时没有「清障」可言，post-tool 必须空转
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user"), st))
        # 第二个输入才能暴露守卫的第一段：session_id 也为空时，光看第二段
        # （session_id != blocked_by）会把 None==None 判成「就是提问者」而放行；
        # last_reported 取 "idle" 避免无守卫版本的 working 决策被去重吞掉
        st2 = PaneState(root_session=ROOT, last_reported="idle")
        self.assertIsNone(decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user", session_id=None), st2))

    def test_stop_while_blocked_does_not_flip_to_idle(self):
        """P0 回归：turn 结束不等于问题解决。

        实测 ask_user 是带 terminate 提前收工的，Stop 到达时问卷还开着，
        翻 idle 会让 pane 对外宣称「任务完成」，orchestrator 直接误判收工。
        last_reported 取 "blocked"：无守卫版本会报 "idle"，不会被去重吞掉。
        """
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        self.assertIsNone(decide(Action("stop"), p("Stop"), st))

    def test_stop_root_reports_idle_when_not_blocked(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        d = decide(Action("stop"), p("Stop"), st)
        self.assertEqual(d.state, "idle")
        self.assertIsNone(d.blocked_by)

    def test_stop_is_deduped_when_already_idle(self):
        st = PaneState(root_session=ROOT, last_reported="idle")
        self.assertIsNone(decide(Action("stop"), p("Stop"), st))

    def test_blocked_recovers_to_working_via_user_prompt(self):
        """blocked 不会卡死：作答就是一次新的 UserPromptSubmit，必然回到 working。"""
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        d = decide(Action("user-prompt"), p("UserPromptSubmit"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)
        follow = decide(Action("stop"), p("Stop"),
                        PaneState(root_session=ROOT, last_reported="working"))
        self.assertEqual(follow.state, "idle")

    def test_stop_from_child_never_flips_to_idle(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("stop"), p("Stop", session_id=CHILD), st))

    def test_session_end_releases_only_current_root(self):
        d = decide(Action("session-end"), p("SessionEnd"), PaneState(root_session=ROOT))
        self.assertEqual(d.kind, Decision.Kind.RELEASE)

    def test_session_end_from_child_does_not_release(self):
        self.assertIsNone(decide(Action("session-end"), p("SessionEnd", session_id=CHILD), PaneState(root_session=ROOT)))

    def test_unchanged_state_is_deduped(self):
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("user-prompt"), p("UserPromptSubmit"), st))

    def test_unknown_action_ignored(self):
        self.assertIsNone(decide(Action("precompact"), p("PreCompact"), PaneState(root_session=ROOT)))
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_decide -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.decide'`

**Step 3: 写实现**

```python
# plugin/scripts/mcode_herdr/decide.py
"""纯函数状态机：输入（事件、载荷、上一状态），输出（要不要报、报什么）。

设计要点：
- 子代理有独立 session_id，因此「session_id != root_session」就是可靠判据。
- blocked 是例外：子代理调 ask_user 时人确实被卡住了，必须上报，并记下 blocked_by，
  让只有提问者自己的 PostToolUse 能清掉它。
- 但「清掉它」的前提是问卷真的被答了。实测 ask_user 立刻就带着
  tool_response.terminate=true / details.waiting_for_user=true 返回（PreToolUse 后
  约 33ms），随后约 56ms 就来 Stop —— **turn 结束的时候问卷还开着**。
  所以 ask_user 的 PostToolUse 只在 waiting_for_user 为假时才允许清障；
  Stop 同样不代表问题已解决：只要 pane 还记着 blocked_by，就不许翻 idle。
  真正的解障信号是用户作答时重新发出的 UserPromptSubmit（user-prompt 分支）。
- SessionStart 在 pane 处于 working 时到达 → 忽略。真机 + 源码核实：此时到达的是**压缩**，不是子代理（子代理发 SubagentStart，且继承会话的 beginTurn 会 early-return 不发 SessionStart）。忽略压缩同时避免「问卷待答时被翻成 idle」。
- 与上次相同的状态不上报，减少噪声（herdr 侧通知本身也按跃迁去重）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .payload import Payload
from .store import PaneState

ASK_USER = "ask_user"


@dataclass(frozen=True)
class Action:
    name: str


@dataclass(frozen=True)
class Decision:
    """状态机交给运行时的唯一契约。

    - kind 决定消费方式：REPORT 按 state 改 pane 状态，RELEASE 交还 pane。
    - state 只对 REPORT 有意义，RELEASE 时为空串。
    - attach_session / resume 只由 session-start 的「认领」路径置位：那是 pane
      第一次学到本次会话的 id，也只有那一刻能拿到恢复命令去 attach。
    - new_root_session 非空时，用它覆盖 store 里已存的 root_session。
    - 不变式：state != "blocked" 的 REPORT 一定带 blocked_by=None，消费方因此
      可以无条件信任 decision.blocked_by。
    """

    class Kind(Enum):
        REPORT = "report"
        RELEASE = "release"

    kind: "Decision.Kind"
    state: str = ""
    message: str = ""
    attach_session: bool = False
    resume: bool = False
    blocked_by: Optional[str] = None
    new_root_session: Optional[str] = None


def decide(action: Action, payload: Payload, state: PaneState) -> Optional[Decision]:
    name, root = action.name, state.root_session

    if name == "session-start":
        sid = payload.session_id
        if not sid:
            return None
        if state.last_reported == "working":
            return None  # 父 agent 正在干活时新建的会话，只可能是子代理
        return Decision(
            kind=Decision.Kind.REPORT,
            state="idle",
            attach_session=True,
            resume=True,
            new_root_session=sid,
        )

    if name == "user-prompt":
        if not root or payload.session_id != root:
            return None
        return _report("working", state, clear_blocked=True)

    if name == "pre-tool":
        if payload.tool_name != ASK_USER:
            return None
        sid = payload.session_id
        if not sid:
            return None
        return Decision(
            kind=Decision.Kind.REPORT,
            state="blocked",
            message="等待你的决策",
            blocked_by=sid,
        )

    if name == "post-tool":
        if payload.tool_name != ASK_USER:
            return None
        if not state.blocked_by or payload.session_id != state.blocked_by:
            return None
        if payload.waiting_for_user:
            # 问卷还开着，人还在等：这个 PostToolUse 只是 ask_user 提前收工，
            # 不是作答。原实现在这里翻 working，实测只让 blocked 存在了 121ms，
            # 紧接着 Stop 又把 pane 标成 done —— herdr agent wait --until blocked
            # 因此基本永远等不到。保持不动，一个字节都不上报。
            return None
        return _report("working", state, clear_blocked=True)

    if name == "stop":
        if not root or payload.session_id != root:
            return None  # 子代理的 Stop 绝不能把 pane 翻成 idle
        if state.blocked_by:
            # turn 结束 ≠ 问题解决：ask_user 是带 terminate 提前收工的，
            # 此刻人还杵在问卷前面。翻 idle 等于对外宣称「任务完成」。
            # 解障交给 user-prompt：作答会重新触发 UserPromptSubmit。
            return None
        return _report("idle", state, clear_blocked=True)

    if name == "session-end":
        if not root or payload.session_id != root:
            return None  # 结束的不是当前会话，release 会误清新会话的状态
        return Decision(kind=Decision.Kind.RELEASE)

    return None


def _report(target: str, state: PaneState, clear_blocked: bool = False) -> Optional[Decision]:
    if state.last_reported == target:
        return None
    return Decision(
        kind=Decision.Kind.REPORT,
        state=target,
        blocked_by=None if clear_blocked else state.blocked_by,
    )
```

**Step 4: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_decide -v`
Expected: PASS 25 tests（`test/test_decide.py` 实际用例数）

**Step 5: 提交**

```bash
git add plugin/scripts/mcode_herdr/decide.py test/test_decide.py
git commit -m "feat(decide): 纯函数状态机，子代理 blocked 例外与 seq 去重"
```

---

## Task 6: 传输层 `transport.py`

**Files:**
- Create: `plugin/scripts/mcode_herdr/herdr.py`
- Create: `plugin/scripts/mcode_herdr/transport.py`
- Test: `test/test_transport.py`

**Step 1: 写失败的测试**

```python
# test/test_transport.py
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcode_herdr import transport

ENV = {
    "HERDR_ENV": "1",
    "HERDR_PANE_ID": "w1:p1",
    "HERDR_BIN_PATH": "/opt/herdr",
    "HERDR_SOCKET_PATH": "/tmp/fake.sock",
}


OK_REPLY = json.dumps({"id": "r1", "result": {"type": "ok"}}).encode() + b"\n"


class FakeSocketServer:
    """最小 herdr server：接受一行 JSON，回一行 reply（默认 ok 应答）。"""

    def __init__(self, path, reply=OK_REPLY):
        self.path = Path(path)
        self.reply = reply
        self.requests = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(4)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = conn.makefile("rwb")
                line = data.readline()
                if not line:
                    continue
                self.requests.append(json.loads(line))
                data.write(self.reply)
                data.flush()

    def close(self):
        self._stop = True
        self._sock.close()
        try:
            self.path.unlink()
        except OSError:
            pass


class DripSocketServer(FakeSocketServer):
    """滴字节的对端：定时吐一点字节，且始终不发换行。"""

    def __init__(self, path, step=8, interval=0.1, steps=15):
        super().__init__(path, reply=b"")
        self._step = step
        self._interval = interval
        self._steps = steps

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = conn.makefile("rwb")
                if not data.readline():
                    continue
                for _ in range(self._steps):
                    if self._stop:
                        return
                    try:
                        data.write(b"x" * self._step)
                        data.flush()
                    except OSError:
                        return
                    time.sleep(self._interval)


class FloodSocketServer(FakeSocketServer):
    """狂灌数据且不发换行的对端。"""

    def __init__(self, path, chunk=1 << 20, rounds=8):
        super().__init__(path, reply=b"")
        self._chunk = b"x" * chunk
        self._rounds = rounds

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = conn.makefile("rwb")
                if not data.readline():
                    continue
                conn.settimeout(0.05)  # 让阻塞中的写能定期醒来检查 _stop
                for _ in range(self._rounds):
                    if self._stop:
                        return
                    try:
                        data.write(self._chunk)
                        data.flush()
                    except OSError:
                        return


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "fake.sock"
        self.calls = []
        self._orig_cli = transport._run_cli
        transport._run_cli = self._fake_cli
        self.binlog = Path(self.tmp.name) / "herdr"

    def _fake_cli(self, argv, timeout):
        self.calls.append(argv)
        return 0

    def _fake_cli_code(self, code):
        """让回落 CLI 返回固定码：这样 report() 的聚合结果能反推 socket 通道的判定。"""
        def run(argv, timeout):
            self.calls.append(argv)
            return code
        transport._run_cli = run

    def tearDown(self):
        transport._run_cli = self._orig_cli
        self.tmp.cleanup()

    def _env(self, **over):
        env = dict(ENV)
        env.update(over)
        return env

    def test_socket_channel_used_when_available(self):
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                   state="working", seq=7, message="hi")
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])  # 没走 CLI
        req = server.requests[0]
        self.assertEqual(req["method"], "pane.report_agent")
        params = req["params"]
        self.assertEqual(params["pane_id"], "w1:p1")
        self.assertEqual(params["state"], "working")
        self.assertEqual(params["seq"], 7)
        self.assertEqual(params["message"], "hi")

    def test_falls_back_to_cli_when_socket_missing(self):
        ok = transport.report(self._env(), state="idle", seq=8)
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertEqual(argv[:4], ["/opt/herdr", "pane", "report-agent", "w1:p1"])
        self.assertIn("--seq", argv)
        self.assertIn("8", argv)

    def test_release_falls_back_to_cli_with_seq(self):
        ok = transport.release(self._env(), seq=9)
        self.assertTrue(ok)
        self.assertEqual(self.calls[0][:4], ["/opt/herdr", "pane", "release-agent", "w1:p1"])
        # 必须带 seq：不带会被 hook_report_is_newer 静默丢弃
        self.assertIn("--seq", self.calls[0])

    def test_resume_argv_is_appended_after_double_dash(self):
        transport.report(self._env(), state="idle", seq=10,
                         resume_argv=["mcode", "--session", "abc"])
        argv = self.calls[0]
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["mcode", "--session", "abc"])

    def test_invalid_resume_argv_still_reports_state(self):
        # session_id 来自 hook JSON，带单引号时校验必然失败；herdr 也会拒收这个恢复命令，
        # 所以要丢掉恢复命令但把状态发出去，而不是抛异常把状态上报一起丢掉
        session_id = "abc'def"
        ok = transport.report(self._env(), state="idle", seq=16,
                              session_id=session_id,
                              resume_argv=["mcode", "--session", session_id])
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertNotIn("--", argv)
        # --session 只可能来自 resume_argv（--agent 的值恰好也是 "mcode"，不能拿它当判据）
        self.assertNotIn("--session", argv)
        self.assertIn(session_id, argv)  # 状态本身确实发出去了

    def test_all_channels_failing_returns_false_without_raising(self):
        def boom(argv, timeout):
            raise OSError("nope")
        transport._run_cli = boom
        self.assertFalse(transport.report(self._env(), state="idle", seq=11))

    def test_resume_argv_rejects_path_like_first_word(self):
        with self.assertRaises(ValueError):
            transport.validate_resume_argv(["/usr/bin/mcode", "--session", "a"])

    def test_resume_argv_rejects_quote_and_too_many_args(self):
        with self.assertRaises(ValueError):
            transport.validate_resume_argv(["mcode", "it's"])
        with self.assertRaises(ValueError):
            transport.validate_resume_argv(["mcode"] + ["x"] * 64)

    def test_null_reply_is_not_treated_as_success(self):
        # null 不是 herdr 应答：既不能当成成功，也不能再让 TypeError 逃出 report()
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"null\n")
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=13)
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertEqual(len(self.calls), 1)  # 确实回落到了 CLI，没被假成功短路

    def test_non_object_reply_is_not_treated_as_success(self):
        # 假阳性方向：数组里没有 "error" 就算成功，调用方会记下 herdr 不知道的状态
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"[1,2]\n")
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=14)
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertEqual(len(self.calls), 1)

    def test_release_with_null_reply_returns_false(self):
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"null\n")
        try:
            ok = transport.release(self._env(HERDR_SOCKET_PATH=str(self.sock_path)), seq=15)
        finally:
            server.close()
        self.assertFalse(ok)

    def test_socket_construction_failure_falls_back_to_cli(self):
        # mcode 进程繁忙时可能 fd 耗尽，socket() 构造本身就抛 OSError；
        # 它必须留在守卫区内，否则异常会逃出 report() 且 close 无从执行
        real_socket = socket.socket

        def no_fd(*args, **kwargs):
            raise OSError(24, "Too many open files")

        transport.socket.socket = no_fd  # 共享 stdlib 模块，用完必须还原
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="idle", seq=17)
        finally:
            transport.socket.socket = real_socket
        self.assertTrue(ok)  # 状态改由 CLI 通道发出去
        self.assertEqual(len(self.calls), 1)

    # ---- 应答形状：假阳性方向 ----

    def test_bare_object_reply_is_not_treated_as_success(self):
        # {} / {"id":...} 只说明对端会吐 JSON，没说它收下了这次上报；
        # 误判成成功就会记下 last_reported，decide() 之后把同一事件永久去重，pane 无恢复地发散
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"{}\n")
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=18)
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertEqual(len(self.calls), 1)  # 确实回落到了 CLI，没被假成功短路

    def test_error_reply_falls_back_to_cli(self):
        # 假阳性收紧的对照面：herdr 明确拒绝（error 信封）时必须回落 CLI。
        # 只测“拒绝的应答不能算成功”这类收紧方向的测试，会让人把它一路改成“永远 True”而不被发现，
        # 所以这里用 CLI 返回 0 来证明 socket 通道真的放行了拒绝，才由 CLI 完成这次上报
        self._fake_cli_code(0)
        reply = json.dumps({"id": "r1", "error": {"message": "no such pane"}}).encode() + b"\n"
        server = FakeSocketServer(self.sock_path, reply=reply)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=19)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)

    def test_two_replies_on_one_connection_use_the_first(self):
        # 一条连接上跟了两条应答时，取第一行即可：json.loads 整个 buffer 会报 "Extra data"
        server = FakeSocketServer(self.sock_path, reply=OK_REPLY + OK_REPLY)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=20)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])

    # ---- 整段读取的总时限与缓冲上限 ----

    def test_slow_drip_without_newline_is_bounded_by_the_deadline(self):
        # SOCKET_TIMEOUT 只约束单次 socket 操作：每 0.1s 吐 8 字节就不会触发任何一次超时，
        # 但整段读取被拖到 1.5s。hook 只有 5s，herdr 不能反过来拖住 mcode
        self._fake_cli_code(1)
        server = DripSocketServer(self.sock_path)
        try:
            start = time.monotonic()
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=21)
            elapsed = time.monotonic() - start
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertLess(elapsed, 1.0, f"整段读取没有被总时限截断：{elapsed:.2f}s")

    def test_flood_without_newline_does_not_buffer_without_bound(self):
        # 对端狂灌数据且不发换行：缓冲上限必须让它立刻放弃，而不是在时限内吃下若干兆
        self._fake_cli_code(1)
        server = FloodSocketServer(self.sock_path)
        try:
            start = time.monotonic()
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=22)
            elapsed = time.monotonic() - start
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.35, f"缓冲没有上限，一路吃到了时限：{elapsed:.2f}s")

    # ---- seq 时钟 ----

    def test_next_seq_is_non_decreasing(self):
        seqs = [transport.next_seq() for _ in range(50)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertLess(seqs[0], seqs[-1])

    def test_next_seq_does_not_follow_the_wall_clock(self):
        # 只测“非递减”杀不掉把 monotonic_ns 换回 time_ns 的变异体（单进程内两者都不倒退），
        # 所以这里把墙上时钟钉死：next_seq 必须仍然给出可用的 seq。
        # seq 一旦倒退，herdr 会静默丢弃这次上报却照样回 ok，调用方无从察觉
        real_time_ns = time.time_ns
        time.time_ns = lambda: 1  # 共享 stdlib 模块，用完必须还原
        try:
            seq = transport.next_seq()
        finally:
            time.time_ns = real_time_ns
        self.assertGreater(seq, 1)

    def test_next_seq_is_monotonic_across_processes(self):
        # 选单调时钟的另一半理由：Linux 上 CLOCK_MONOTONIC 是系统级的，两个进程共有一条时间轴；
        # 墙上时钟则可能在另一个进程两次上报之间被校时拨回去。子进程先打印，父进程的值必须更大
        pkg_root = Path(transport.__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-c",
             "from mcode_herdr import transport; print(transport.next_seq())"],
            env=dict(os.environ, PYTHONPATH=str(pkg_root)),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(int(proc.stdout.strip()), transport.next_seq())

    # ---- 两条通道的参数对称性 ----

    def test_release_over_socket_sends_method_and_seq(self):
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.release(self._env(HERDR_SOCKET_PATH=str(self.sock_path)), seq=23)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])  # 走的是 socket，不该回落
        req = server.requests[0]
        self.assertEqual(req["method"], "pane.release_agent")
        # 必须带 seq：漏了会被 herdr 静默丢弃，pane 上就永远挂着那个旧 agent
        self.assertEqual(req["params"]["seq"], 23)

    def test_report_over_socket_includes_resume_argv(self):
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="blocked", seq=24,
                                  resume_argv=["mcode", "--session", "abc"])
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])  # 走的是 socket，不该回落
        # CLI 通道只钉了 "--" 分隔符，socket 通道带没带 resume_argv 之前无人把守
        self.assertEqual(server.requests[0]["params"]["resume_argv"],
                         ["mcode", "--session", "abc"])

    def test_message_is_truncated_on_both_channels(self):
        # 400 是两条通道共同的上限，不能只截其中一边
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="blocked", seq=25, message="x" * 500)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(server.requests[0]["params"]["message"], "x" * 400)

        transport.report(self._env(), state="blocked", seq=26, message="y" * 500)
        argv = self.calls[0]
        self.assertEqual(argv[argv.index("--message") + 1], "y" * 400)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_transport -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.transport'`

**Step 3: 写协议常量 `herdr.py`**

```python
# plugin/scripts/mcode_herdr/herdr.py
"""herdr 上报协议常量。与本机 0.9.3 CLI/源码对齐。"""

SOURCE = "mcode-herdr"       # 稳定且唯一；换名会导致旧 authority 变成别人的
AGENT = "mcode"              # 自定义 agent 用自己的名字，不要冒用 herdr 内置 kind

STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_BLOCKED = "blocked"

SOCKET_TIMEOUT = 0.5
CLI_TIMEOUT = 1.0

RESUME_MAX_ARGS = 64
RESUME_MAX_BYTES = 8192
```

**Step 4: 写 `transport.py`**

```python
# plugin/scripts/mcode_herdr/transport.py
"""双通道上报：优先 Unix socket 直连，失败回落 $HERDR_BIN_PATH CLI。

两条通道协议等价。任何异常都吞掉并返回 False —— herdr 集成绝不能影响 mcode。
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import time
from typing import Mapping, Optional, Sequence

from . import herdr

_COMMAND_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# 单次 recv 的量级：herdr 的 ok/err 应答只有几十字节，超出这个量级还没换行就不是正常应答
_MAX_REPLY_BYTES = 4096


def _run_cli(argv: Sequence[str], timeout: float) -> int:
    """独立成函数，便于测试替换。"""
    return subprocess.run(argv, timeout=timeout,
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def validate_resume_argv(argv: Optional[Sequence[str]]) -> None:
    """对齐 herdr src/agent_resume.rs 的 validate_resume_argv。"""
    if not argv:
        raise ValueError("resume argv must not be empty")
    if len(argv) > herdr.RESUME_MAX_ARGS:
        raise ValueError(f"resume argv exceeds {herdr.RESUME_MAX_ARGS} elements")
    if sum(len(a.encode()) for a in argv) > herdr.RESUME_MAX_BYTES:
        raise ValueError("resume argv too large")
    for arg in argv:
        if "'" in arg:
            raise ValueError("resume argv must not contain single quotes")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in arg):
            raise ValueError("resume argv must not contain control characters")
    head = argv[0]
    if head.startswith("-") or "/" in head or not _COMMAND_NAME.match(head):
        raise ValueError("resume command must be a bare command name, not a path")


def _socket_report(env: Mapping[str, str], method: str, params: dict) -> bool:
    sock_path = env.get("HERDR_SOCKET_PATH")
    if not sock_path:
        return False
    payload = {"id": f"{herdr.SOURCE}:{time.time_ns()}", "method": method, "params": params}
    conn = None
    try:
        # socket() 自己也会抛（fd 耗尽 EMFILE），所以构造和超时设置都得在守卫区内，
        # 否则异常会逃出 report()，而且 finally 里的 close 也没机会执行
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(herdr.SOCKET_TIMEOUT)
        conn.connect(sock_path)
        conn.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        # SOCKET_TIMEOUT 只约束单次 socket 操作，滴字节的对端能把整个读取拖到任意长；
        # 这里在 mcode 的 hook 关键路径上（只有 5s），herdr 不能反过来拖住 mcode，
        # 所以除了逐次超时还要给整段读取一个总时限和缓冲上限
        deadline = time.monotonic() + herdr.SOCKET_TIMEOUT
        while not buf.endswith(b"\n"):
            if time.monotonic() >= deadline or len(buf) >= _MAX_REPLY_BYTES:
                return False
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return False
        # 只取第一行：一条连接上若还跟着别的应答，json.loads 整个 buffer 会报 "Extra data"
        reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
        # 只有 {"id":..., "result":...} 形状的 herdr 应答才算“已接收”：
        # 数组/null/裸数字/乱码都不是应答（null 还会让 in 判断抛 TypeError 逃出去），
        # 光秃秃的 {} / {"id":...} 同理不是应答，只说明对端会吐 JSON 而没说它收下了这次上报，
        # 把它们误判成成功，调用方就会记下一个 herdr 根本不知道的状态，
        # 而 decide() 按 last_reported 去重会把同一事件永久压掉，pane 再无恢复触发地发散
        return isinstance(reply, dict) and "result" in reply and "error" not in reply
    except (OSError, ValueError, json.JSONDecodeError):
        # socket.timeout 即 TimeoutError，是 OSError 子类，0.5s 超时已在此被吞掉
        return False
    finally:
        if conn is not None:
            conn.close()


def report(env: Mapping[str, str], state: str, seq: int, *,
           message: str = "", session_id: Optional[str] = None,
           resume_argv: Optional[Sequence[str]] = None) -> bool:
    params = {
        "pane_id": env["HERDR_PANE_ID"],
        "source": herdr.SOURCE,
        "agent": herdr.AGENT,
        "state": state,
        "seq": seq,
    }
    if message:
        params["message"] = message[:400]
    if session_id:
        params["agent_session_id"] = session_id
    resume: Optional[list] = None
    if resume_argv:
        try:
            validate_resume_argv(resume_argv)
        except ValueError:
            # 恢复命令校验失败说明 herdr 同样会拒收它，硬发只会白白丢掉这次上报；
            # 恢复命令只是增强能力、状态转移才是主功能，所以降级为不带恢复命令继续上报，
            # 而不是把 ValueError 抛出去把状态上报一起赔进去
            pass
        else:
            resume = list(resume_argv)
            params["resume_argv"] = resume
            # 不传 agent_session_path：它与 session_ref 一样会被白名单挡掉，传了也是噪音
    if _socket_report(env, "pane.report_agent", params):
        return True
    argv = [env["HERDR_BIN_PATH"], "pane", "report-agent", env["HERDR_PANE_ID"],
            "--source", herdr.SOURCE, "--agent", herdr.AGENT,
            "--state", state, "--seq", str(seq)]
    if message:
        argv += ["--message", message[:400]]
    if session_id:
        argv += ["--agent-session-id", session_id]
    if resume:
        argv += ["--"] + resume
    try:
        return _run_cli(argv, herdr.CLI_TIMEOUT) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def release(env: Mapping[str, str], seq: int) -> bool:
    params = {"pane_id": env["HERDR_PANE_ID"], "source": herdr.SOURCE,
              "agent": herdr.AGENT, "seq": seq}
    if _socket_report(env, "pane.release_agent", params):
        return True
    argv = [env["HERDR_BIN_PATH"], "pane", "release-agent", env["HERDR_PANE_ID"],
            "--source", herdr.SOURCE, "--agent", herdr.AGENT, "--seq", str(seq)]
    try:
        return _run_cli(argv, herdr.CLI_TIMEOUT) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def next_seq() -> int:
    """herdr 要求 seq 严格递增；monotonic_ns 在 Linux 上是系统级时钟，跨进程也单调。"""
    # 必须用单调时钟而不是墙上时钟：time_ns 取的是 CLOCK_REALTIME，NTP 回步或校时会让它倒退。
    # seq 一旦没有变大，herdr 会静默丢弃这次上报却照样回 ok —— 调用方无从察觉，仍然记下
    # last_reported，之后 decide() 按它把同一事件全部去重，于是 pane 再无恢复触发地发散。
    # 本插件只跑在 Linux 上，CLOCK_MONOTONIC 是系统级的：跨进程一致且永不倒退；
    # 它在重启后归零，但那时 herdr 自己的状态也跟着机器一起没了，无害
    return time.monotonic_ns()
```

**Step 5: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_transport -v`
Expected: PASS 23 tests（`test/test_transport.py` 实际用例数）

**Step 6: 提交**

```bash
git add plugin/scripts/mcode_herdr/herdr.py plugin/scripts/mcode_herdr/transport.py test/test_transport.py
git commit -m "feat(transport): socket 优先、CLI 回落的双通道上报"
```

---

## Task 7: 入口 `herdr-report.py`

**Files:**
- Create: `plugin/scripts/herdr-report.py`
- Test: `test/test_replay.py`

**Step 1: 写失败的回放测试**

```python
# test/test_replay.py
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
        self.assertEqual(json.loads(state_path.read_text())["root_session"], sid)

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
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_replay -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.runtime'`

**Step 3: 写 `runtime.py`**

```python
# plugin/scripts/mcode_herdr/runtime.py
"""把「事件 → decide → transport → 落盘」串起来。

钩子入口 herdr-report.py 会派一个脱离进程组的后台进程调用 main()，
所以这里只管把一次上报做对，任何异常都不得逃出去。
"""
from __future__ import annotations

from pathlib import Path
from typing import IO, Mapping, Optional

from . import herdr, transport
from .decide import Action, Decision, decide
from .env import discover_herdr_env
from .payload import load_payload
from .store import PaneState, Store

_VALID_STATES = frozenset({herdr.STATE_IDLE, herdr.STATE_WORKING, herdr.STATE_BLOCKED})


def _resume_argv(session_id: Optional[str]) -> list:
    # 首词必须是 PATH 上的裸命令名，herdr 会拒绝绝对路径。
    return ["mcode", "--session", session_id] if session_id else []


def _resolve_herdr_env(env: Mapping[str, str], proc_root: Optional[Path]) -> Optional[Mapping[str, str]]:
    # 显式分支是生产主路径：herdr-report.py 在派生 worker 之前就把回溯结果注入子进程
    # 环境，因为 worker 一旦被 reparent 到 init 就再也走不回 /proc 祖先进程链。
    # 它同时覆盖「人在 herdr pane 里手动跑 worker.py」和回放测试注入假环境两种情况。
    # 回退到 /proc 只在既没有显式 HERDR_* 又确实还在 mcode 进程树里时才会命中。
    # 删掉第一条分支会让整个插件静默失效（herdr agent list 永远为空）。
    if env.get("HERDR_ENV") == "1" and env.get("HERDR_PANE_ID"):
        return {k: v for k, v in env.items() if k.startswith("HERDR_")}
    return discover_herdr_env(proc_root=proc_root or Path("/proc"))


def run_once(action: str, raw_payload: str, env: Mapping[str, str],
             proc_root: Optional[Path] = None) -> int:
    herdr_env = _resolve_herdr_env(env, proc_root)
    if herdr_env is None:
        return 0  # 不在 herdr 里：彻底静默
    try:
        payload = load_payload(raw_payload)
    except ValueError:
        return 0

    pane_id = herdr_env["HERDR_PANE_ID"]
    # 拿不到 PLUGIN_DATA 就什么都不做，不回退到 /tmp：状态文件名由 pane id 派生、
    # 可预测，而 store.py 落临时文件用的是 os.open(O_CREAT)，它会跟随预置的符号链接
    # （0600 只管文件权限，管不了路径解析），在全局可写的 /tmp 里等于把状态 JSON
    # 写进攻击者指定的文件。生产环境 mcode 必定注入 PLUGIN_*，回退分支本来就走不到。
    plugin_data = env.get("PLUGIN_DATA")
    if not plugin_data:
        return 0
    store = Store(Path(plugin_data))

    def step(state: PaneState) -> None:
        decision = decide(Action(action), payload, state)
        if decision is None:
            return
        if decision.kind is Decision.Kind.RELEASE:
            transport.release(herdr_env, transport.next_seq())
            state.root_session = None
            state.blocked_by = None
            state.last_reported = None
            return
        if decision.state not in _VALID_STATES:
            return  # 状态机若将来吐出非法状态，宁可不报也不要让 herdr 拒收
        resume = _resume_argv(payload.session_id) if decision.resume else None
        sent = transport.report(herdr_env, decision.state, transport.next_seq(),
                                message=decision.message,
                                session_id=payload.session_id if decision.attach_session else None,
                                resume_argv=resume)
        state.root_session = decision.new_root_session or state.root_session
        state.blocked_by = decision.blocked_by
        state.last_reported = decision.state if sent else state.last_reported

    store.update(pane_id, step)
    return 0


def main(argv: list, stdin: IO, env: Mapping[str, str],
         proc_root: Optional[Path] = None) -> int:
    try:
        return run_once(argv[0] if argv else "", stdin.read(), env, proc_root=proc_root)
    except Exception:  # noqa: BLE001 - 任何异常都不得影响 mcode
        return 0
```

**Step 4: 写入口脚本 `plugin/scripts/herdr-report.py`**

```python
#!/usr/bin/env python3
"""mcode 钩子入口。

钩子跑在工具调用的关键路径上，必须立即返回：读掉 stdin 后派生一个
脱离进程组的后台子进程去做真正的上报，父进程直接退出，且不向
stdout/stderr 写任何字节 —— PreToolUse 的 stdout 会被运行时消费。

herdr 环境必须在派生之前由本进程解析（mcode 只给钩子白名单环境，HERDR_*
要靠 discover_herdr_env 沿 /proc 父链回溯），再连同 child_env 一起交给 worker，
绝不能指望 worker 自己回溯：钩子一退出，worker 就被 init 收养
（start_new_session 只换会话组，不改父子关系），/proc 父链断成 worker -> init，
env.py 里 pid<=1 的守卫立刻返回 None，上报就静默消失了 —— 钩子照常触发，
状态文件却一个都不写。父链只在钩子进程自己身上是完整的，必须在派生前读出来。

解析不到（不在 herdr 里）时连 worker 都不派生：钩子只多几次 /proc 读就退出，
省掉子进程那次解释器启动，也不会有任何东西去抢 mcode 的进程表。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mcode_herdr.env import discover_herdr_env  # noqa: E402


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read()
    try:
        herdr_env = discover_herdr_env()
    except Exception:  # noqa: BLE001 - 任何异常都不得影响 mcode
        return 0
    if not herdr_env:
        return 0  # 不在 herdr 里：不派生 worker，彻底静默
    # worker 自己已经回溯不到 HERDR_*（父进程退出即被 init 收养），只能由这里交给它；
    # runtime._resolve_herdr_env 会优先采信 child_env 里显式存在的 HERDR_*
    child_env = dict(os.environ)
    child_env.update(herdr_env)
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "worker.py"), event],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=child_env,
        )
    except Exception:  # noqa: BLE001
        return 0
    try:
        # 故意不等 worker：钩子阻塞一秒就是 mcode 工具调用路径上多一秒。
        # 子进程已 start_new_session=True（脱离进程组）且父进程先退出，
        # 它会被 init 收养，不会留下僵尸。
        proc.stdin.write(raw.encode())
        proc.stdin.close()
        # Popen 对象此刻仍带着一个未回收的子进程：GC 触发 __del__ 时会发
        # ResourceWarning，而它写的是 stderr —— 在 PYTHONWARNINGS=always 或
        # python -X dev 下会污染钩子输出。填上 returncode 等于告诉 Popen
        # 「已经收过了」，析构便不再告警，且完全不产生等待。
        proc.returncode = 0
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 5: 写 `plugin/scripts/worker.py`**

```python
#!/usr/bin/env python3
"""后台 worker：真正执行上报。由 herdr-report.py 派生。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mcode_herdr.runtime import main  # noqa: E402


if __name__ == "__main__":
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        main([event], sys.stdin, os.environ)
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
```

**Step 6: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_replay -v`
Expected: PASS 11 tests（`test/test_replay.py` 实际用例数）

**Step 7: 提交**

```bash
git add plugin/scripts/herdr-report.py plugin/scripts/worker.py \
        plugin/scripts/mcode_herdr/runtime.py test/test_replay.py
git commit -m "feat(runtime): 钩子入口立即返回，上报交给脱离进程组的后台 worker"
```

---

## Task 8: 插件清单与钩子映射

**Files:**
- Create: `plugin/.minimax-plugin/plugin.json`
- Create: `plugin/icon.png`
- Create: `plugin/hooks/hooks.json`

**Step 1: 生成 icon（必填，否则会被静默跳过）**

```bash
python3 -c "
import base64
open('plugin/icon.png','wb').write(base64.b64decode(
 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='))
"
```

**Step 2: 写 `plugin/.minimax-plugin/plugin.json`**

```json
{
  "schemaVersion": 1,
  "name": "mcode-herdr",
  "displayName": "mcode × herdr",
  "version": "0.1.0",
  "description": "把 mcode 的 agent 状态与会话恢复命令上报给 herdr，让 mcode 在 herdr 中可被识别、等待与恢复。",
  "author": "mavis",
  "icon": "icon.png",
  "category": "Code",
  "exampleQueries": [],
  "apps": [],
  "mcpServers": [],
  "skills": [],
  "hooks": ["hooks/hooks.json"]
}
```

**Step 3: 写 `plugin/hooks/hooks.json`**

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command",
          "command": "python3 \"${PLUGIN_ROOT}/scripts/herdr-report.py\" session-start",
          "timeout": 5 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
          "command": "python3 \"${PLUGIN_ROOT}/scripts/herdr-report.py\" user-prompt",
          "timeout": 5 } ] }
    ],
    "PreToolUse": [
      { "matcher": "ask_user",
        "hooks": [ { "type": "command",
          "command": "python3 \"${PLUGIN_ROOT}/scripts/herdr-report.py\" pre-tool",
          "timeout": 5 } ] }
    ],
    "PostToolUse": [
      { "matcher": "ask_user",
        "hooks": [ { "type": "command",
          "command": "python3 \"${PLUGIN_ROOT}/scripts/herdr-report.py\" post-tool",
          "timeout": 5 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command",
          "command": "python3 \"${PLUGIN_ROOT}/scripts/herdr-report.py\" stop",
          "timeout": 5 } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command",
          "command": "python3 \"${PLUGIN_ROOT}/scripts/herdr-report.py\" session-end",
          "timeout": 5 } ] }
    ]
  }
}
```

**Step 4: 提交**

```bash
git add plugin/
git commit -m "feat(plugin): 添加 mcode-herdr 插件清单与钩子映射"
```

---

## Task 9: 安装与卸载脚本

**Files:**
- Create: `install.sh`
- Create: `uninstall.sh`

**Step 1: 写 `install.sh`**

```bash
#!/usr/bin/env bash
# 同步插件到本地市场并验证。本地插件被发现即 installed+enabled，无需 plugin add。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/plugin"
DATA_DIR="${MINIMAX_DATA_DIR:-$HOME/.minimax}"
DEST="$DATA_DIR/plugins/mcode-herdr"

echo "==> 同步 $SRC -> $DEST"
# rm -rf 不可撤销，而 DEST 由 $MINIMAX_DATA_DIR / $HOME 推导而来，两者都可能为空或
# 被写成奇怪的路径。删之前必须确认 DEST 是绝对路径且末段恰好是 mcode-herdr，
# 否则一次误算就会把别的目录连根删掉且无法恢复。
if [ -z "$DEST" ] || [ "${DEST#/}" = "$DEST" ]; then
  echo "拒绝执行：DEST 不是绝对路径：$DEST" >&2
  exit 1
fi
if [ "${DEST##*/}" != "mcode-herdr" ]; then
  echo "拒绝执行：DEST 末段不是 mcode-herdr：$DEST" >&2
  exit 1
fi
# mkdir -p 在 rm -rf 之前是必需的：cp -r "$SRC/." "$DEST/" 要求目标目录已存在。
mkdir -p "$DEST"
rm -rf "$DEST"
cp -r "$SRC/." "$DEST/"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "$DEST/scripts"/*.py

echo "==> 验证插件已被本地市场识别"
mcode plugin list -m local --available | grep -E '^.\*\].*mcode-herdr@local' \
  || { echo "未识别！请检查 plugin.json 是否有 icon 字段（必填，缺失会被静默跳过）" >&2; exit 1; }

echo "==> 检查恢复命令的 mcode 是否可被 herdr 执行"
# 恢复命令是 `--` 后的裸命令名 `mcode`，由 herdr server 所在环境去解析，
# 不是由登录 shell 解析。之前这里用 `env -i /bin/sh -lc` 判断，而登录 shell
# 不读 ~/.bashrc（本机 mcode 的 PATH 正是 ~/.bashrc:36 加的），于是无论
# mcode 是否真的可用都会误报。改为分两级：先看当前环境，再看 herdr server
# 自己的 PATH（也就是真正执行恢复命令的那份环境）。
resume_ok=0
if command -v mcode >/dev/null 2>&1; then
  resume_ok=1
elif command -v pgrep >/dev/null 2>&1; then
  for pid in $(pgrep -x herdr 2>/dev/null); do
    if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^PATH=' | tr ':' '\n' \
        | grep -q '/mcode$'; then
      resume_ok=1
      break
    fi
  done
fi
if [ "$resume_ok" -eq 0 ]; then
  cat >&2 <<'EOF'
警告：herdr 恢复会话时要执行的裸命令名 `mcode` 当前解析不到，恢复会失败。
herdr server 继承启动它的终端环境；若那里也没有 mcode，可执行：
      ln -s "$(command -v mcode)" ~/.local/bin/mcode
然后重启 herdr server 让它带上新的 PATH。
EOF
fi

echo "完成。重启 mcode（或开新会话）后生效。"
```

**Step 2: 写 `uninstall.sh`**

```bash
#!/usr/bin/env bash
# 移除插件。先释放它对 pane 的占用，否则 herdr 会继续把 pane 显示为有 agent。
set -euo pipefail

DATA_DIR="${MINIMAX_DATA_DIR:-$HOME/.minimax}"
DEST="$DATA_DIR/plugins/mcode-herdr"

if [ -n "${HERDR_BIN_PATH:-}" ] && [ -n "${HERDR_PANE_ID:-}" ]; then
  # seq 必须严格递增。插件用 monotonic_ns（开机以来的纳秒），所以这里也必须
  # 用开机以来的时间；用 date +%s（纪元纳秒）会把高水位抬到 10^18，
  # 重装后 monotonic 的 seq 会被 herdr 判为过期而静默丢弃。
  seq="$(awk '{printf "%d", $1 * 1000000000}' /proc/uptime 2>/dev/null || true)"
  if [ -z "$seq" ]; then
    seq="$(date +%s)000000000"
  fi
  "$HERDR_BIN_PATH" pane release-agent "$HERDR_PANE_ID" \
    --source mcode-herdr --agent mcode --seq "$seq" || true
fi

# rm -rf 不可撤销，而 DEST 由 $MINIMAX_DATA_DIR / $HOME 推导而来，两者都可能为空或
# 被写成奇怪的路径。删之前必须确认 DEST 是绝对路径且末段恰好是 mcode-herdr。
if [ -d "$DEST" ]; then
  if [ -z "$DEST" ] || [ "${DEST#/}" = "$DEST" ]; then
    echo "拒绝执行：DEST 不是绝对路径：$DEST" >&2
    exit 1
  fi
  if [ "${DEST##*/}" != "mcode-herdr" ]; then
    echo "拒绝执行：DEST 末段不是 mcode-herdr：$DEST" >&2
    exit 1
  fi
  echo "==> 删除 $DEST"
  rm -rf "$DEST"
fi
echo "完成。"
```

**Step 3: 跑 shellcheck（若可用）**

Run: `bash -n install.sh && bash -n uninstall.sh`
Expected: 无输出（语法正确）

**Step 4: 提交**

```bash
git add install.sh uninstall.sh
git commit -m "feat(install): 添加插件安装与卸载脚本"
```

---

## Task 10: 离线测试入口

**Files:**
- Create: `test/run-tests.sh`

**Step 1: 写 `test/run-tests.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec env PYTHONPATH="plugin/scripts" python3 -m unittest discover -s test -t . -v "$@"
```

**Step 2: 跑全量测试**

Run: `chmod +x test/run-tests.sh && ./test/run-tests.sh`
Expected: 全部 PASS。当前共 102 个用例（`./test/run-tests.sh`）

**Step 3: 提交**

```bash
git add test/run-tests.sh
git commit -m "test: 添加统一测试入口"
```

---

## Task 11: 真机安装验证

**Files:** 无（验证任务）

**Step 1: 先卸载探针插件**

Run:
```bash
T="$HOME/.minimax/bin/mavis-trash"; "$T" -- "$HOME/.minimax/plugins/herdr-probe" || true
```
Expected: moved to trash

**Step 2: 安装**

Run: `./install.sh`
Expected:
```
==> 同步 .../plugin -> ~/.minimax/plugins/mcode-herdr
==> 验证插件已被本地市场识别
[*] mcode-herdr@local	enabled
完成。...
```

**Step 3: 确认探针已卸载、正式插件在列**

Run: `mcode plugin list -m local --available`
Expected: 只有 `mcode-herdr@local`，没有 `herdr-probe`

**Step 4: 触发一轮真实会话并检查上报**

在当前 herdr pane 里开一个新的 mcode 会话，发一条提示，然后：

Run: `herdr agent list | python3 -c "import json,sys;[print(a['agent'],a['pane_id'],a.get('agent_status')) for a in json.load(sys.stdin)['result']['agents']]"`
Expected: 新 pane 出现 `mcode <pane> idle`（turn 结束后）

**Step 5: 提交（仅当需要修正时）**

```bash
git commit -m "fix(plugin): 真机验证后修正"
```

---

## Task 12: 隔离环境端到端验证

**Files:** 无（验证任务）

**Step 1: 起一个隔离的命名 session**

Run: `herdr --session mcode-herdr-test`
Expected: 打开一个新 session（不触碰你当前的工作区）

**Step 2: 在该 session 内建 pane 并跑 mcode**

Run（在隔离 session 内）:
```bash
pane=$(herdr pane split --current --direction down --no-focus --json | python3 -c "import json,sys;print(json.load(sys.stdin)['result']['pane']['pane_id'])")
herdr pane run "$pane" "mcode"
```

**Step 3: 验证状态流转**

Run: `herdr agent list --json`（在隔离 session 内）
Expected: 出现 `agent: "mcode"`、`agent_status` 随 idle/working 变化

**Step 4: 验证 blocked**

在隔离 session 的 mcode 里让它调用 `ask_user`。
Expected: `agent_status: "blocked"`

**Step 5: 验证重启恢复**

Run: `herdr session stop mcode-herdr-test && herdr --session mcode-herdr-test`
Expected: pane 自动跑起 `mcode --session <原会话 id>`，且 `herdr agent list` 中该 pane 仍是 `mcode`

**Step 6: 若恢复失败**

排查 `mcode` 是否在 herdr server 的 PATH 上（herdr server 继承终端环境，
可能没有 `~/.minimax-code/bin`）。对策是在 `install.sh` 里建 `~/.local/bin/mcode` 符号链接。

---

## Task 13: 子代理专项验证

**Files:** 无（验证任务）

**Step 1: 触发子代理**

在隔离 session 的 mcode 里让它派一个子代理去干活。
（**不要**写成「让子代理调 `ask_user`」—— 见下面 Step 3 的说明。）

**Step 2: 断言父会话不被误翻 idle**

Run: 在子代理运行期间轮询 `herdr agent get <pane>`
Expected: `agent_status` 保持 `working`，不会出现 `idle`

> **真机实测结果：已验证通过。** `explore` 与 `mavis` 两种子代理各跑一轮，
> 期间 pane 全程停在 `working`，从未被翻成 `idle`。

**Step 3: 断言子代理提问会上报 blocked**

Expected: 子代理调用 ask_user 期间 `agent_status: "blocked"`

> **这一条在 mcode 0.6.3 上无法执行。** 真机分别派了 `explore` 与 `mavis` 去尝试提问，
> 两者都独立报告 `ask_user` 对子代理不可用、只有顶层 agent 能调
> （`explore` 报出的工具是 bash/glob/grep/read/web_fetch）。所以本步不是「还没验证」，
> 而是**当前版本下无法构造**：`decide.py` 里对应的分支是前瞻性防御，
> 只能靠构造载荷在回放测试里覆盖。将来 mcode 把 `ask_user` 暴露给子代理时，
> 子会话的 `session_id` 管道已经就位，届时回到本步即可。

**Step 4: 断言 blocked 能被正确解除**

在**顶层** agent 调 `ask_user` 的场景下：人作答后，Expected: 回到 `working`，
最终 turn 结束为 `idle`。
真机时序与实测结果见「背景事实」的 `ask_user` 条目与设计文档 §2.9：
`blocked` 会一直挂到作答触发的那次 `UserPromptSubmit` 为止。

---

## Task 14: README

**Files:**
- Create: `README.md`

**Step 1: 写 README**

必须包含：
- 这是什么、解决什么问题（herdr 目前完全看不见 mcode）
- 安装：`./install.sh`，卸载：`./uninstall.sh`
- 工作原理：6 个钩子 → 状态机 → 双通道上报
- 子代理语义：为何 `blocked` 是例外
- **恢复命令依赖 `mcode` 在 PATH 上**，以及如何修复
- 故障排查：`mcode plugin list -m local` 为空 → 多半是缺 `icon`；herdr 里看不到 mcode → 多半是不在 herdr 环境或 PATH 问题
- 已知限制：非 Linux 上 `/proc` 回溯不可用，插件退化为无操作；`agent_session_id` 被 herdr 白名单丢弃，恢复只依赖恢复命令

**Step 2: 提交**

```bash
git add README.md
git commit -m "docs: 添加 mcode-herdr 使用与排障说明"
```

---

## 完成标准

勾选状态反映**证据来源**，不是「应该能做到」。本计划执行时的真机验证只覆盖了
下面标注了实测的两条；其余各条属于安装后需手工走一遍的验收项。

- [x] `./test/run-tests.sh` 全绿 —— **实测**：102 个用例（可随时复跑）
- [ ] `mcode plugin list -m local` 显示 `mcode-herdr@local enabled` —— 安装后手工确认
- [x] 在 herdr pane 内跑 mcode，`herdr agent list` 能看到 `mcode` 且状态随
      idle/working/blocked 正确流转 —— **实测**：0.1s 轮询真实 herdr，
      `+0ms idle | +2s working | +4s blocked ... held 15.8s ... +18s working | +19s done`
- [x] 子代理运行期间父会话不被误翻 idle —— **实测**：全程停在 `working`
- [ ] 子代理提问会上报 blocked —— **在 mcode 0.6.3 上无法验证**：子代理没有 `ask_user`
      工具（设计文档 §5.1）。该分支只以构造载荷覆盖，是前瞻性防御而非当前可达路径
- [ ] herdr server 重启后能按 `mcode --session <id>` 恢复 —— 安装后手工确认
- [x] 不在 herdr 环境时，插件对 mcode 零影响（不拖慢、不输出、不报错）—— **实测**：
      钩子 stdout/stderr 为空字节，且不在 herdr 里时连后台 worker 都不派生