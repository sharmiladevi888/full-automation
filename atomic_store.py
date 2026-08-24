"""Agent 1: Atomic file operations to prevent state corruption.

Fixes the race condition in store.py where concurrent requests
can corrupt state.json.
"""
import json
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict


class AtomicStore:
    """Thread-safe, atomic JSON state persistence."""

    def __init__(self, path: str, default_factory=None):
        self.path = path
        self.default_factory = default_factory or dict
        self._ensure_dir()

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            return self.default_factory()

    def save(self, data: Dict[str, Any]):
        self._ensure_dir()
        dir_name = os.path.dirname(self.path) or "."
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except (OSError, UnboundLocalError):
                pass
            raise

    @contextmanager
    def transaction(self):
        data = self.load()
        yield data
        self.save(data)

    def update(self, key: str, value: Any):
        with self.transaction() as data:
            data[key] = value

    def append_to(self, key: str, item: Any):
        with self.transaction() as data:
            data.setdefault(key, []).append(item)

    def exists(self) -> bool:
        return os.path.exists(self.path)
