"""Agent 4: Architecture — Health check & diagnostics endpoint.

Provides:
- System resource info (disk, memory)
- Service connectivity status
- Version info
- Uptime tracking
"""
import os
import sys
import time
import shutil
import platform
from typing import Dict, Any

import config

_START_TIME = time.time()


def get_health() -> Dict[str, Any]:
    """Comprehensive health check."""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - _START_TIME),
        "version": get_version(),
        "system": get_system_info(),
        "disk": get_disk_info(),
        "config": get_config_summary(),
    }


def get_version() -> Dict[str, str]:
    return {
        "app": "2.0.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def get_system_info() -> Dict[str, Any]:
    info = {
        "os": platform.system(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count(),
    }
    # Memory (best effort)
    try:
        import psutil
        mem = psutil.virtual_memory()
        info["memory_total_gb"] = round(mem.total / (1024**3), 1)
        info["memory_used_pct"] = mem.percent
    except ImportError:
        pass
    return info


def get_disk_info() -> Dict[str, Any]:
    """Disk usage for the data directory."""
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        return {
            "data_dir": config.DATA_DIR,
            "total_gb": round(usage.total / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "used_pct": round((usage.used / usage.total) * 100, 1),
        }
    except Exception:
        return {"error": "unable to check disk"}


def get_config_summary() -> Dict[str, Any]:
    """Non-sensitive config overview."""
    return {
        "image_model": config.MODEL,
        "claude_model": config.CLAUDE_MODEL,
        "default_size": config.DEFAULT_SIZE,
        "default_quality": config.DEFAULT_QUALITY,
        "max_concurrency": config.IMAGE_MAX_CONCURRENCY,
        "auth_required": config.AUTH_REQUIRED,
        "has_image_key": bool(config.API_KEY),
        "has_claude_key": bool(config.CLAUDE_API_KEY),
        "has_elevenlabs_key": bool(config.ELEVENLABS_API_KEY),
        "has_openrouter_key": bool(config.OPENROUTER_API_KEY),
    }


def check_ffmpeg() -> Dict[str, Any]:
    """Check if ffmpeg/ffprobe are available."""
    import subprocess
    result = {"ffmpeg": False, "ffprobe": False}
    for tool in ("ffmpeg", "ffprobe"):
        try:
            p = subprocess.run([tool, "-version"], capture_output=True, timeout=5)
            result[tool] = p.returncode == 0
        except Exception:
            pass
    return result
