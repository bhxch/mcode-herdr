"""每个 pane 一份状态，写入加 flock 并原子替换。

并发来自「每次钩子派生独立后台进程」，多个进程可能同时读到同一旧状态。
flock 只分别串行化每一次 load 和每一次 save；load 与 save 合起来并不构成一个
临界区，两者之间可以夹进别的进程的写，调用方的读-改-写会丢更新。
tmp + rename 是另一层独立保证：读者永远看到完整 JSON，不会读到半截文件。
需要「读最新值 → 决定 → 写回」整体不被穿插的写覆盖时，走 update()。

状态里还记着写它的那台 mcode（pid + 启动指纹）。mcode 被强杀时 Stop 永远不会
到达，last_reported 停在 working，新会话的 SessionStart 又会被「working ⇒ 忽略」
那条守卫吞掉（到达时的 SessionStart 实际是压缩，不是子代理——子代理发的是
SubagentStart，继承会话不会发 SessionStart），pane 从此永远卡住。所以读取时先判
归属是不是还活着：死了就当没写过。
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

from .env import read_starttime

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class PaneState:
    root_session: Optional[str] = None
    blocked_by: Optional[str] = None
    last_reported: Optional[str] = None
    # 写这份状态的 mcode 进程身份。两个字段都原样存字符串、从不参与算术：
    # pid 只用来拼 /proc 路径，starttime 只跟当前读到的值比一次，
    # 所以不存在 int/str 写反导致误判的可能。
    owner_pid: Optional[str] = None
    owner_start: Optional[str] = None


class Store:
    def __init__(self, base: Path, proc_root: Path = Path("/proc")):
        self.base = Path(base)
        # 判活必须查真实进程表：替身 /proc 里没有真 pid，任何状态都会被判死。
        # 只有单测直接构造 Store 时才注入替身，runtime 走默认的真 /proc。
        self.proc_root = Path(proc_root)

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
        state = PaneState(
            root_session=data.get("root_session") or None,
            blocked_by=data.get("blocked_by") or None,
            last_reported=data.get("last_reported") or None,
            owner_pid=data.get("owner_pid") or None,
            owner_start=data.get("owner_start") or None,
        )
        if not self._owner_alive(state):
            # 写这份状态的 mcode 已经不在了：Stop 没来过的痕迹不能再当证据，
            # 丢掉重放。新会话的 SessionStart 因此能正常认领，pane 不会永远卡死。
            return PaneState()
        return state

    def _owner_alive(self, state: PaneState) -> bool:
        """归属进程是不是还活着。

        必须连 starttime 一起比：pid 会被内核回收给毫不相干的进程，只看「这个 pid
        还在」等于让死掉的 mcode 永远显得活着。缺一个字段也当没有归属 —— 证明不了
        活着，就不能拿它当当前状态用；已发布版本写下的文件正落在这条上。
        """
        if not state.owner_pid or not state.owner_start:
            return False
        return read_starttime(state.owner_pid, self.proc_root) == state.owner_start

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
