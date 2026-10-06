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
- `.minimax-plugin/plugin.json` 的 **`icon` 是必填字段**，缺失会被
  `readManifestIcon` → `requiredString` 拒绝，失败写进 `diagnostics` **静默跳过**，CLI 无任何提示。
  必须放一个真实的 png/jpg/jpeg/webp 文件并在清单里声明相对路径。
- `category` 是枚举，合法值含 `Office / Studio / Design & Sites / Code / Business / Sales /
  Productivity / Science & Healthcare / Education / Other`。
- 钩子子进程环境是**严格白名单**（`agent-modules/plugin-hooks/src/runner.ts:1771` 的
  `safeHookEnvironment()`）：`PATH HOME LANG TERM SHELL USER TMPDIR TEMP TMP PATHEXT SystemRoot
  ComSpec USERPROFILE HOMEDRIVE HOMEPATH APPDATA LOCALAPPDATA`。
  **`HERDR_*` 不会传入**，必须靠 `/proc` 父进程链回溯。
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
  所以**每次上报（含 release）都必须带 seq**。建议 `time.time_ns()`。
- **`agent_session_id` 对自定义 agent 会被丢弃**：
  `src/agent_resume.rs:116` 的 `session_ref_from_report` 先判 `is_official_agent_source`，
  而后者是 18 对硬编码白名单（`src/agent_resume.rs:327`），不含 mcode。
  → 不要依赖它。**但 `resume_argv` 不受此限制**：`record_reported_resume`
  （`src/terminal/state.rs:1975`）只校验 `can_record_reported_resume`，
  即「当前 authority 的 source+agent 标签与我们完全一致」。所以恢复能力靠 `--` 后的恢复命令。
- `resume_argv` 校验（`src/agent_resume.rs:58`）：非空、≤64 个元素、总长 ≤8192 字节、
  无控制字符、**无单引号**、首元素必须是裸命令名（仅 `[A-Za-z0-9._-]`，不以 `-` 开头，不能是路径）。
- 一个 pane 只有一个 `hook_authority`；**不同 source 会被静默拒绝**，故 source 名要稳定。
- `done` 不是上报值，是 herdr 从 `idle + 未被查看` 派生的，所以完成时上报 `idle` 即可。
- 通知由状态跃迁派生并自带去重（`src/app/actions.rs:77`），同状态重复上报不会重复弹窗。

---

## 文件布局

```
/share/rw/repo/tools/mcode-herdr/
  plugin/                                  # 插件本体，安装时同步到 ~/.minimax/plugins/mcode-herdr/
    .minimax-plugin/plugin.json
    icon.png
    hooks/hooks.json
    scripts/
      herdr-report.py                      # 入口：读事件名与 stdin，派生后台进程后立即退出
      mcode_herdr/
        __init__.py
        env.py                             # /proc 父链回溯取 HERDR_*
        payload.py                         # 解析 stdin JSON
        store.py                           # 每 pane 状态，flock + 原子写
        decide.py                          # 纯函数状态机
        transport.py                       # socket 优先、CLI 回落
        herdr.py                           # 协议常量与 argv 拼装
  test/
    run-tests.sh
    fixtures/*.json                        # 真实载荷样本
    fake-herdr/herdr                       # 假 herdr 二进制，记录 argv
    test_env.py
    test_payload.py
    test_store.py
    test_decide.py
    test_transport.py
    test_replay.py                         # 端到端回放：喂真实载荷，断言调用序列
  install.sh
  uninstall.sh
  README.md
```

---

## Task 1: 仓库骨架

**Files:**
- Create: `.gitignore`
- Create: `plugin/scripts/mcode_herdr/__init__.py`

**Step 1: 写 .gitignore**

```
.temp/
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
import os
import tempfile
import unittest
from pathlib import Path

from mcode_herdr.env import discover_herdr_env, read_environ, read_ppid


class EnvDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_proc(self, pid, ppid, env):
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        (d / "environ").write_bytes(b"\0".join(env) + b"\0")
        (d / "status").write_text(f"Name:\tpid\nPPid:\t{ppid}\n")
        return d

    def test_walks_up_until_herdr_env_found(self):
        # self(1) -> mid(2) -> mcode(3) with HERDR, self ppid chain reversed
        self._make_proc(1, 0, ["PATH=/bin"])
        self._make_proc(2, 1, ["PATH=/bin"])
        self._make_proc(3, 2, ["PATH=/bin", "HERDR_ENV=1", "HERDR_PANE_ID=w1:p1",
                               "HERDR_BIN_PATH=/opt/herdr", "HERDR_SOCKET_PATH=/tmp/s"])
        os.environ.clear()
        os.environ.update({"PATH": "/bin"})
        # 模拟自身 pid 为 1，因此直接命中自身（带 HERDR）
        got = discover_herdr_env(proc_root=self.proc, start_pid=3)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")
        self.assertEqual(got["HERDR_SOCKET_PATH"], "/tmp/s")

    def test_returns_none_when_no_herdr_ancestor(self):
        self._make_proc(1, 0, ["PATH=/bin"])
        self._make_proc(2, 1, ["PATH=/bin"])
        os.environ.clear()
        os.environ.update({"PATH": "/bin"})
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=2))

    def test_reads_ppid_from_status_not_stat(self):
        self._make_proc(7, 5, ["PATH=/bin"])
        self.assertEqual(read_ppid(7, self.proc), 5)

    def test_ignores_herdr_env_not_equal_one(self):
        self._make_proc(1, 0, ["PATH=/bin", "HERDR_ENV=", "HERDR_PANE_ID=w1:p1"])
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=1))


if __name__ == "__main__":
    unittest.main()
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_env -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.env'`

**Step 3: 写实现**

```python
# plugin/scripts/mcode_herdr/env.py
"""通过 /proc 父进程链回溯，找出带 HERDR_ENV=1 的祖先（即 mcode 进程）。

mcode 的钩子只继承 safeHookEnvironment() 白名单环境，HERDR_* 不会传入子进程，
所以只能从祖先进程的环境里取回。父进程号必须读 /proc/<pid>/status 的 PPid:，
不能按位置解析 /proc/<pid>/stat —— comm 含空格或括号会错位。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

MAX_ANCESTRY = 12
REQUIRED = ("HERDR_ENV", "HERDR_PANE_ID")


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
    """向上回溯，找到第一个 HERDR_ENV=1 且有 HERDR_PANE_ID 的祖先。

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
Expected: PASS 4 tests

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

**Step 2: 写失败的测试**

```python
# test/test_payload.py
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
```

**Step 3: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_payload -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.payload'`

**Step 4: 写实现**

```python
# plugin/scripts/mcode_herdr/payload.py
"""把 mcode 钩子的 stdin JSON 解析成 Payload。

字段名来自实测（见设计文档 §2.6/§2.8），解析保持宽容：
缺失或空字符串一律归一为 None，避免下游到处判空。
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


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    return None


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
    )
```

**Step 5: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_payload -v`
Expected: PASS 6 tests

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
import tempfile
import unittest
from pathlib import Path

from mcode_herdr.store import PaneState, Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

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

    def test_save_is_atomic_no_temp_left_behind(self):
        self.store.save("w1:p1", PaneState(root_session="a"))
        leftovers = [p.name for p in self.store.path_for("w1:p1").parent.iterdir()
                     if p.name != "w1:p1.json"]
        self.assertEqual(leftovers, [])
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.store'`

**Step 3: 写实现**

```python
# plugin/scripts/mcode_herdr/store.py
"""每个 pane 一份状态，写入加 flock 并原子替换。

并发来自「每次钩子派生独立后台进程」，多个进程可能同时读到同一旧状态。
flock 保证同一 pane 的读-改-写串行；tmp + rename 保证读者永远看到完整 JSON。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

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

    def load(self, pane_id: str) -> PaneState:
        with self._locked(pane_id) as path:
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

    def save(self, pane_id: str, state: PaneState) -> None:
        with self._locked(pane_id) as path:
            tmp = path.with_suffix(".json.tmp.%d" % os.getpid())
            tmp.write_text(json.dumps(asdict(state)))
            os.replace(tmp, path)
```

**Step 4: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_store -v`
Expected: PASS 5 tests

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


def p(event, session_id=ROOT, tool_name=None, agent_id=None, source=None):
    return Payload(event=event, session_id=session_id, tool_name=tool_name,
                   agent_id=agent_id, agent_type=None, source=source,
                   cwd="/tmp", stop_hook_active=False)


class DecideTest(unittest.TestCase):
    def test_session_start_adopts_root_and_reports_idle(self):
        d = decide(Action("session-start"), p("SessionStart", source="startup"), PaneState())
        self.assertEqual(d.kind, Decision.Kind.REPORT)
        self.assertEqual(d.state, "idle")
        self.assertTrue(d.attach_session)
        self.assertTrue(d.resume)

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
        st = PaneState(root_session=ROOT, last_reported="working")
        self.assertIsNone(decide(Action("user-prompt"), p("UserPromptSubmit", session_id=CHILD), st))

    def test_ask_user_pretool_root_reports_blocked(self):
        d = decide(Action("pre-tool"), p("PreToolUse", tool_name="ask_user"), PaneState(root_session=ROOT))
        self.assertEqual(d.state, "blocked")
        self.assertEqual(d.blocked_by, ROOT)

    def test_ask_user_pretool_from_subagent_also_reports_blocked(self):
        # 人确实被问了，子代理提问同样必须报 blocked
        st = PaneState(root_session=ROOT, last_reported="working")
        d = decide(Action("pre-tool"), p("PreToolUse", session_id=CHILD, tool_name="ask_user", agent_id=CHILD), st)
        self.assertEqual(d.state, "blocked")
        self.assertEqual(d.blocked_by, CHILD)

    def test_non_ask_user_pretool_ignored(self):
        self.assertIsNone(decide(Action("pre-tool"), p("PreToolUse", tool_name="bash"), PaneState(root_session=ROOT)))

    def test_posttool_clears_only_own_blocked(self):
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        d = decide(Action("post-tool"), p("PostToolUse", tool_name="ask_user"), st)
        self.assertEqual(d.state, "working")
        self.assertIsNone(d.blocked_by)

    def test_posttool_from_other_session_cannot_clear_root_blocked(self):
        st = PaneState(root_session=ROOT, blocked_by=ROOT, last_reported="blocked")
        self.assertIsNone(decide(Action("post-tool"), p("PostToolUse", session_id=CHILD, tool_name="ask_user"), st))

    def test_stop_root_reports_idle_and_clears_blocked(self):
        st = PaneState(root_session=ROOT, blocked_by=CHILD, last_reported="blocked")
        d = decide(Action("stop"), p("Stop"), st)
        self.assertEqual(d.state, "idle")
        self.assertIsNone(d.blocked_by)

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
- 但 blocked 是例外：子代理调 ask_user 时人确实被卡住了，必须上报，
  并记下 blocked_by，让只有提问者自己的 PostToolUse 能清掉它。
- SessionStart 在 pane 处于 working 时到达 → 必定是子代理创建，忽略。
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
        return _report("working", state)

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
        return _report("working", state, clear_blocked=True)

    if name == "stop":
        if not root or payload.session_id != root:
            return None  # 子代理的 Stop 绝不能把 pane 翻成 idle
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
Expected: PASS 16 tests

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
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from mcode_herdr import transport

ENV = {
    "HERDR_ENV": "1",
    "HERDR_PANE_ID": "w1:p1",
    "HERDR_BIN_PATH": "/opt/herdr",
    "HERDR_SOCKET_PATH": "/tmp/fake.sock",
}


class FakeSocketServer:
    """最小 herdr server：接受一行 JSON，回一行 ok。"""

    def __init__(self, path):
        self.path = Path(path)
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
                data.write(json.dumps({"id": "r1", "result": {"type": "ok"}}).encode() + b"\n")
                data.flush()

    def close(self):
        self._stop = True
        self._sock.close()
        try:
            self.path.unlink()
        except OSError:
            pass


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
            ok = transport.report(self._env(), state="working", seq=7, message="hi")
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
import os
import re
import socket
import subprocess
import time
from typing import Mapping, Optional, Sequence

from . import herdr

_COMMAND_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


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
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(herdr.SOCKET_TIMEOUT)
    try:
        conn.connect(sock_path)
        conn.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return False
        return "error" not in json.loads(buf.decode("utf-8", "replace"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
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
    if resume_argv:
        validate_resume_argv(resume_argv)
        params["resume_argv"] = list(resume_argv)
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
    if resume_argv:
        argv += ["--"] + list(resume_argv)
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
    """herdr 要求 seq 严格递增；time_ns 跨进程也单调。"""
    return time.time_ns()
```

**Step 5: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_transport -v`
Expected: PASS 7 tests

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
import io
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


def run(action, payload_text, env, plugin_data, fake_herdr_log):
    script = textwrap.dedent(f"""
        import json, sys, os
        sys.path.insert(0, {str(ROOT / 'plugin' / 'scripts')!r})
        from mcode_herdr import runtime
        runtime.main(sys.argv[1], sys.stdin, os.environ, blocking=True)
    """)
    env2 = dict(env)
    env2["PLUGIN_DATA"] = str(plugin_data)
    proc = subprocess.run([sys.executable, "-c", script, action],
                          input=payload_text, env=env2, capture_output=True, text=True, timeout=30)
    return proc


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.log = self.data / "herdr-calls.jsonl"
        self.fake = self.data / "herdr"
        self.fake.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            printf '%s\\n' "$*" >> "{self.log}"
            exit 0
            """))
        self.fake.chmod(0o755)
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
        proc = run("user-prompt", (FIXTURES / "stop.json").read_text(), env, self.data, self.log)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(proc.stdout, "")   # 绝不污染钩子 stdout

    def test_session_start_then_prompt_then_stop(self):
        sid = "mvs_test_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": sid,
                                         "source": "startup"}), self.env, self.data, self.log)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid}),
            self.env, self.data, self.log)
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": sid,
                                "stop_hook_active": False}), self.env, self.data, self.log)
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
            self.env, self.data, self.log)
        run("user-prompt", json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": root}),
            self.env, self.data, self.log)
        run("stop", json.dumps({"hook_event_name": "Stop", "session_id": child,
                                "stop_hook_active": False}), self.env, self.data, self.log)
        self.assertNotIn("--state idle", self.calls()[-1])
        self.assertEqual(len(self.calls()), 2)  # 子代理的 stop 被完全忽略

    def test_ask_user_blocked_then_post_clears(self):
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data, self.log)
        run("pre-tool", (FIXTURES / "pre_tool_subagent.json").read_text(),
            self.env, self.data, self.log)
        self.assertIn("--state blocked", self.calls()[-1])
        run("post-tool", json.dumps({"hook_event_name": "PostToolUse", "session_id": "mvs_child_123",
                                     "tool_name": "ask_user", "tool_input": {},
                                     "tool_response": {}, "tool_use_id": "c1"}),
            self.env, self.data, self.log)
        self.assertIn("--state working", self.calls()[-1])

    def test_session_end_releases_with_seq(self):
        root = "mvs_root"
        run("session-start", json.dumps({"hook_event_name": "SessionStart", "session_id": root}),
            self.env, self.data, self.log)
        run("session-end", json.dumps({"hook_event_name": "SessionEnd", "session_id": root}),
            self.env, self.data, self.log)
        self.assertIn("pane release-agent", self.calls()[-1])
        self.assertIn("--seq", self.calls()[-1])


if __name__ == "__main__":
    unittest.main()
```

**Step 2: 跑测试确认失败**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_replay -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcode_herdr.runtime'`

**Step 3: 写 `runtime.py`**

```python
# plugin/scripts/mcode_herdr/runtime.py
"""把「事件 → decide → transport」串起来。

blocking=True 供测试直接调用；生产入口脚本用 blocking=False 的快路径
（派生脱离进程组的后台子进程后立即退出）。
"""
from __future__ import annotations

import sys
from typing import IO, Mapping, Optional

from . import herdr, transport
from .decide import Action, Decision, decide
from .env import discover_herdr_env
from .payload import load_payload
from .store import PaneState, Store


def _resume_argv(session_id: Optional[str]) -> list:
    # 首词必须是 PATH 上的裸命令名，herdr 会拒绝绝对路径。
    return ["mcode", "--session", session_id] if session_id else []


def run_once(action: str, raw_payload: str, env: Mapping[str, str],
             blocking: bool = True) -> int:
    herdr_env = discover_herdr_env()
    if herdr_env is None:
        return 0  # 不在 herdr 里：彻底静默
    try:
        payload = load_payload(raw_payload)
    except ValueError:
        return 0

    pane_id = herdr_env["HERDR_PANE_ID"]
    plugin_data = env.get("PLUGIN_DATA") or "/tmp"
    store = Store(__import__("pathlib").Path(plugin_data))
    state = store.load(pane_id)
    decision = decide(Action(action), payload, state)
    if decision is None:
        return 0

    seq = transport.next_seq()
    if decision.kind is Decision.Kind.RELEASE:
        transport.release(herdr_env, seq)
        store.save(pane_id, PaneState())
        return 0

    resume = _resume_argv(payload.session_id) if decision.resume else None
    transport.report(herdr_env, decision.state, seq,
                     message=decision.message,
                     session_id=payload.session_id if decision.attach_session else None,
                     resume_argv=resume)

    root = decision.new_root_session or state.root_session
    blocked = decision.blocked_by if decision.state == herdr.STATE_BLOCKED else None
    store.save(pane_id, PaneState(root_session=root, blocked_by=blocked,
                                  last_reported=decision.state))
    return 0


def main(argv: list, stdin: IO, env: Mapping[str, str], blocking: bool = True) -> int:
    action = argv[0] if argv else ""
    raw = stdin.read()
    try:
        return run_once(action, raw, env, blocking=blocking)
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
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read()
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "worker.py"), event],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.communicate(input=raw.encode(), timeout=5)
    except Exception:  # noqa: BLE001
        return 0
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
        main([event], sys.stdin, os.environ, blocking=True)
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
```

**Step 6: 跑测试确认通过**

Run: `PYTHONPATH=plugin/scripts python3 -m unittest test.test_replay -v`
Expected: PASS 5 tests

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
mkdir -p "$DEST"
rm -rf "$DEST"
cp -r "$SRC/." "$DEST/"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "$DEST/scripts"/*.py

echo "==> 验证插件已被本地市场识别"
mcode plugin list -m local --available | grep -E '^.\*\].*mcode-herdr@local' \
  || { echo "未识别！请检查 plugin.json 是否有 icon 字段（必填，缺失会被静默跳过）" >&2; exit 1; }

echo "==> 检查恢复命令的 mcode 是否在登录 shell 的 PATH 上"
if ! env -i /bin/sh -lc 'command -v mcode' >/dev/null 2>&1; then
  cat >&2 <<'EOF'
警告：登录 shell 的 PATH 上找不到 mcode。
herdr 恢复会话时会执行 `--` 后的裸命令名 `mcode`，找不到会导致恢复失败。
建议：ln -s "$(command -v mcode)" ~/.local/bin/mcode
      并确保 ~/.local/bin 在登录 shell 的 PATH 中。
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
  "$HERDR_BIN_PATH" pane release-agent "$HERDR_PANE_ID" \
    --source mcode-herdr --agent mcode --seq "$(date +%s)000000000" || true
fi

if [ -d "$DEST" ]; then
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
Expected: 全部 PASS，约 43 个用例

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

在隔离 session 的 mcode 里让它派一个子代理去做会调用 `ask_user` 的事。

**Step 2: 断言父会话不被误翻 idle**

Run: 在子代理运行期间轮询 `herdr agent get <pane>`
Expected: `agent_status` 保持 `working`，不会出现 `idle`

**Step 3: 断言子代理提问会上报 blocked**

Expected: 子代理调用 ask_user 期间 `agent_status: "blocked"`

**Step 4: 断言 blocked 能被正确解除**

子代理收到回答后，Expected: 回到 `working`，最终 turn 结束为 `idle`

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

- [ ] `./test/run-tests.sh` 全绿
- [ ] `mcode plugin list -m local` 显示 `mcode-herdr@local enabled`
- [ ] 在 herdr pane 内跑 mcode，`herdr agent list` 能看到 `mcode` 且状态随 idle/working/blocked 正确流转
- [ ] 子代理运行期间父会话不被误翻 idle；子代理提问会上报 blocked
- [ ] herdr server 重启后能按 `mcode --session <id>` 恢复
- [ ] 不在 herdr 环境时，插件对 mcode 零影响（不拖慢、不输出、不报错）