"""Atomic, thread-safe JSON persistence helpers."""
import json
import os
import tempfile
import threading
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict


_LOCKS = defaultdict(threading.RLock)
_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    key = os.path.abspath(path)
    with _LOCKS_GUARD:
        return _LOCKS[key]


class AtomicStore:
    """JSON store with per-path locking and atomic replace-on-write."""

    def __init__(self, path: str, default_factory=None):
        self.path = path
        self.default_factory = default_factory or dict
        self._lock = _lock_for(path)
        self._ensure_dir()

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else self.default_factory()
            except (FileNotFoundError, json.JSONDecodeError, PermissionError, OSError):
                return self.default_factory()

    def save(self, data: Dict[str, Any]):
        with self._lock:
            self._ensure_dir()
            directory = os.path.dirname(self.path) or "."
            fd = None
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fd = None
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self.path)
                tmp_path = None
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    @contextmanager
    def transaction(self):
        # Keep the lock across load -> caller mutation -> save. load/save are
        # re-entrant, so update/append helpers can use the same transaction.
        with self._lock:
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
        with self._lock:
            return os.path.exists(self.path)
