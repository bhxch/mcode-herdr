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
        tmp.write_text(json.dumps(asdict(state)))
        os.replace(tmp, path)

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
        """
        with self._locked(pane_id) as path:
            state = self._read(path)
            result = fn(state)
            self._write(path, state)
            return result
