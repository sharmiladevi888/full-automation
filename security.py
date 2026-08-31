"""Security helpers for authentication, rate limiting, and uploads."""
import hashlib
import hmac
import os
import re
import threading
import time
from collections import defaultdict
from typing import Tuple


class RateLimiter:
    """Thread-safe in-memory sliding-window rate limiter."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts = defaultdict(list)
        self._lock = threading.RLock()

    def _prune(self, ip: str, now: float):
        recent = [t for t in self._attempts.get(ip, [])
                  if now - t < self.window]
        if recent:
            self._attempts[ip] = recent
        else:
            self._attempts.pop(ip, None)
        return recent

    def is_blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._prune(str(ip or "unknown"), time.time())) >= self.max_attempts

    def record(self, ip: str):
        with self._lock:
            key = str(ip or "unknown")
            recent = self._prune(key, time.time())
            recent.append(time.time())
            self._attempts[key] = recent

    def reset(self, ip: str):
        with self._lock:
            self._attempts.pop(str(ip or "unknown"), None)

    def remaining(self, ip: str) -> int:
        with self._lock:
            recent = self._prune(str(ip or "unknown"), time.time())
            return max(0, self.max_attempts - len(recent))


login_limiter = RateLimiter(max_attempts=5, window_seconds=300)
signup_limiter = RateLimiter(max_attempts=3, window_seconds=600)
api_limiter = RateLimiter(max_attempts=100, window_seconds=60)


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000).hex()


def validate_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email)) and len(email) <= 254


def validate_password(password: str) -> Tuple[bool, str]:
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 128:
        return False, "Password too long"
    if not re.search(r'[A-Za-z]', password):
        return False, "Password must contain at least one letter"
    if not re.search(r'[0-9]', password):
        return False, "Password must contain at least one number"
    return True, ""


ALLOWED_VIDEO_MIMES = {
    "video/mp4", "video/webm", "video/quicktime", "video/x-msvideo",
    "video/x-matroska", "video/mpeg",
}
ALLOWED_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp",
}
ALLOWED_AUDIO_MIMES = {
    "audio/mpeg", "audio/wav", "audio/ogg", "audio/webm", "audio/mp4",
}

# Used only when a multipart part omits Content-Type. Ambiguous .mp4/.webm
# uploads are treated as video by default; an explicit allowed audio type still
# passes validation.
EXTENSION_MIMES = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}


def validate_upload(filename: str, content_type: str, allowed: set) -> Tuple[bool, str]:
    """Validate a media upload using both its declared MIME and extension.

    A missing MIME is accepted only when a known media extension gives us a
    safe inference. Unknown extensions and executable/script extensions are
    rejected rather than silently written into the media tree.
    """
    if not filename:
        return False, "No filename provided"
    ext = os.path.splitext(str(filename))[1].lower()
    dangerous = {".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".js", ".msi"}
    if ext in dangerous:
        return False, f"File type {ext} not allowed"
    allowed = set(allowed or ())
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared:
        if declared not in allowed:
            return False, f"Content type {declared} not allowed"
        return True, ""
    inferred = EXTENSION_MIMES.get(ext, "")
    if not inferred or inferred not in allowed:
        return False, "A recognized media MIME type is required"
    return True, ""


def sanitize_filename(filename: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
    safe = safe.lstrip('.').replace('..', '')
    return safe[:200] if safe else 'unnamed'


def get_client_ip(request) -> str:
    # Prefer the direct peer. A forwarded header is useful behind a trusted
    # reverse proxy, but accepting arbitrary client-supplied values makes an
    # IP-based limiter trivial to bypass.
    peer = getattr(getattr(request, "client", None), "host", None)
    if peer:
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return "unknown"
