"""Agent 6: Structured logging."""
import json
import os
import sys
from datetime import datetime, timezone

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "FATAL": 50}
_LEVEL = LEVELS.get(os.environ.get("LOG_LEVEL", "INFO").upper(), 20)
_FORMAT = os.environ.get("LOG_FORMAT", "pretty")

def _emit(level, message, **kwargs):
    if LEVELS.get(level, 0) < _LEVEL: return
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "level": level, "msg": message}
    entry.update(kwargs)
    if _FORMAT == "json":
        print(json.dumps(entry, default=str), file=sys.stderr, flush=True)
    else:
        colors = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARN": "\033[33m", "ERROR": "\033[31m", "FATAL": "\033[35m"}
        c = colors.get(level, "")
        ts = datetime.now().strftime("%H:%M:%S")
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
        print(f"{c}[{ts}] {level:5s}\033[0m {message} {extra}", file=sys.stderr, flush=True)

def debug(msg, **kw): _emit("DEBUG", msg, **kw)
def info(msg, **kw): _emit("INFO", msg, **kw)
def warn(msg, **kw): _emit("WARN", msg, **kw)
def error(msg, **kw): _emit("ERROR", msg, **kw)
def fatal(msg, **kw): _emit("FATAL", msg, **kw)
