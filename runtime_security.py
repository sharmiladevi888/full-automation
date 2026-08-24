"""Runtime security hardening for the FastAPI app.

The main app is also exposed through the UI wrapper and desktop launcher. This
module keeps the hardening in a small, testable layer so those launchers can
install it before serving the app without duplicating the full backend.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


_SESSION_TTL_SECONDS = 30 * 86400
_REVOKED_SESSIONS = set()


def _sign_session(secret: bytes, email: str) -> str:
    payload = {
        "email": email,
        "exp": int(time.time()) + _SESSION_TTL_SECONDS,
        "jti": secrets.token_urlsafe(16),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def _verify_session(secret: bytes, token: str) -> Optional[str]:
    if not token or "." not in token or token in _REVOKED_SESSIONS:
        return None
    payload, sig = token.rsplit(".", 1)
    try:
        payload_bytes = payload.encode("ascii")
    except UnicodeEncodeError:
        return None
    expected = hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        email = str(data.get("email") or "")
        exp = int(data.get("exp") or 0)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
    if not email or exp <= int(time.time()):
        return None
    return email


class RuntimeSecurityMiddleware(BaseHTTPMiddleware):
    """Protect mounted data and revoke the bearer token on logout."""

    def __init__(self, app, verify_fn):
        super().__init__(app)
        self._verify_fn = verify_fn

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        token = request.cookies.get("cs_session", "")
        if request.method == "POST" and path == "/api/auth/logout" and token:
            _REVOKED_SESSIONS.add(token)
        if path == "/data" or path.startswith("/data/"):
            # The app's configuration decides whether media is private. When
            # auth is enabled, never let the StaticFiles mount bypass it.
            import config
            if config.AUTH_REQUIRED and not self._verify_fn(token):
                return JSONResponse({"detail": "login required"}, status_code=401)
        return await call_next(request)


def install(app_module) -> None:
    """Install expiring/revocable sessions and auth-gated data access once."""
    if getattr(app_module, "_bugwatch_runtime_security", False):
        return
    secret = app_module._SESSION_KEY

    def sign(email: str) -> str:
        return _sign_session(secret, email)

    def verify(token: str) -> Optional[str]:
        return _verify_session(secret, token)

    # Route functions and auth middleware resolve these names through app.py's
    # module globals, so replacing them hardens every launcher consistently.
    app_module._sign_session = sign
    app_module._verify_session = verify
    app_module.app.add_middleware(RuntimeSecurityMiddleware, verify_fn=verify)
    app_module._bugwatch_runtime_security = True
