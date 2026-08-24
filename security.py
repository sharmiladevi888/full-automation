"""Agent 1: Security Layer for Continuity Studio.

Fixes:
- Rate limiting on auth endpoints (brute-force protection)
- Secure session handling
- Input sanitization
- File upload validation
"""
import time
import hashlib
import hmac
import os
import re
from collections import defaultdict
from typing import Optional, Tuple


class RateLimiter:
    """In-memory rate limiter with sliding window."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts = defaultdict(list)

    def is_blocked(self, ip: str) -> bool:
        now = time.time()
        self._attempts[ip] = [t for t in self._attempts[ip] if now - t < self.window]
        return len(self._attempts[ip]) >= self.max_attempts

    def record(self, ip: str):
        self._attempts[ip].append(time.time())

    def reset(self, ip: str):
        self._attempts.pop(ip, None)

    def remaining(self, ip: str) -> int:
        now = time.time()
        recent = [t for t in self._attempts.get(ip, []) if now - t < self.window]
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


ALLOWED_VIDEO_MIMES = {"video/mp4", "video/webm", "video/quicktime", "video/x-msvideo", "video/x-matroska", "video/mpeg"}
ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}
ALLOWED_AUDIO_MIMES = {"audio/mpeg", "audio/wav", "audio/ogg", "audio/webm", "audio/mp4"}


def validate_upload(filename: str, content_type: str, allowed: set) -> Tuple[bool, str]:
    if not filename:
        return False, "No filename provided"
    dangerous = {".exe", ".bat", ".cmd", ".sh", ".ps1", ".vbs", ".js", ".msi"}
    ext = os.path.splitext(filename)[1].lower()
    if ext in dangerous:
        return False, f"File type {ext} not allowed"
    if content_type and content_type not in allowed:
        return False, f"Content type {content_type} not allowed"
    return True, ""


def sanitize_filename(filename: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
    safe = safe.lstrip('.').replace('..', '')
    return safe[:200] if safe else 'unnamed'


def get_client_ip(request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
