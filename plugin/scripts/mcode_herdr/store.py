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
