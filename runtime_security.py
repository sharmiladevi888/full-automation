"""Runtime hardening for the FastAPI app and its supported launchers.

The wrapper installs this module after importing app.py, which lets us harden
legacy route functions without duplicating the large application module.
"""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

import config


_SESSION_TTL_SECONDS = 30 * 86400
try:
    _MAX_REVOKED_SESSIONS = max(
        100, min(1_000_000, int(os.environ.get("SESSION_REVOKE_MAX", "10000"))))
except (TypeError, ValueError):
    _MAX_REVOKED_SESSIONS = 10_000
_REVOKED_PATH = os.path.join(config.DATA_DIR, "revoked_sessions.json")
_REVOKED_LOCK = threading.RLock()
# Store only SHA-256 digests of tokens on disk; raw bearer tokens never enter
# the revocation file. Values are expiry timestamps so the set is bounded over
# time as well as by count.
_REVOKED_SESSIONS = {}


def _token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _prune_revoked_locked(now=None):
    now = now or time.time()
    changed = False
    for digest, expires in list(_REVOKED_SESSIONS.items()):
        try:
            expired = float(expires) <= now
        except (TypeError, ValueError):
            expired = True
        if expired:
            _REVOKED_SESSIONS.pop(digest, None)
            changed = True
    if len(_REVOKED_SESSIONS) > _MAX_REVOKED_SESSIONS:
        excess = len(_REVOKED_SESSIONS) - _MAX_REVOKED_SESSIONS
        victims = sorted(_REVOKED_SESSIONS.items(), key=lambda item: float(item[1]))[:excess]
        for digest, _expires in victims:
            _REVOKED_SESSIONS.pop(digest, None)
        changed = True
    return changed


def _persist_revoked_locked():
    directory = os.path.dirname(_REVOKED_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(_REVOKED_SESSIONS, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _REVOKED_PATH)
        tmp_path = None
    except Exception as exc:
        print(f"[security] could not persist session revocations: {exc}", flush=True)
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


def _load_revoked_sessions():
    try:
        with open(_REVOKED_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return
    now = time.time()
    with _REVOKED_LOCK:
        if isinstance(raw, dict):
            for digest, expires in raw.items():
                if isinstance(digest, str) and len(digest) == 64:
                    try:
                        _REVOKED_SESSIONS[digest] = float(expires)
                    except (TypeError, ValueError):
                        continue
        elif isinstance(raw, list):
            # Migrate a possible old list-of-raw-tokens file without keeping
            # those tokens in memory or on disk after the next write.
            for token in raw:
                if token:
                    _REVOKED_SESSIONS[_token_digest(token)] = now + _SESSION_TTL_SECONDS
        if _prune_revoked_locked(now):
            _persist_revoked_locked()


def _is_revoked(token: str) -> bool:
    digest = _token_digest(token)
    with _REVOKED_LOCK:
        changed = _prune_revoked_locked()
        denied = digest in _REVOKED_SESSIONS
        if changed:
            _persist_revoked_locked()
        return denied


def _revoke(token: str):
    with _REVOKED_LOCK:
        _prune_revoked_locked()
        _REVOKED_SESSIONS[_token_digest(token)] = time.time() + _SESSION_TTL_SECONDS
        _prune_revoked_locked()
        _persist_revoked_locked()


_load_revoked_sessions()


def _sign_session(secret: bytes, email: str) -> str:
    payload = {
        "email": email,
        "exp": int(time.time()) + _SESSION_TTL_SECONDS,
        "jti": __import__("secrets").token_urlsafe(16),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def _verify_session(secret: bytes, token: str) -> Optional[str]:
    if not token or "." not in token or _is_revoked(token):
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
        if not isinstance(data, dict):
            return None
        email = data.get("email")
        exp = int(data.get("exp") or 0)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(email, str) or not email or exp <= int(time.time()):
        return None
    return email


def _cookie_secure_enabled():
    return (os.environ.get("COOKIE_SECURE", "").strip().lower()
            in ("1", "true", "yes")) or (
        os.environ.get("CS_ENV", "").strip().lower() == "production")


def _install_secure_cookie_hook():
    if getattr(StarletteResponse, "_bugwatch_cookie_hook", False):
        return
    original = StarletteResponse.set_cookie

    def secure_set_cookie(self, *args, **kwargs):
        key = args[0] if args else kwargs.get("key")
        if key == "cs_session" and _cookie_secure_enabled():
            kwargs["secure"] = True
        return original(self, *args, **kwargs)

    StarletteResponse.set_cookie = secure_set_cookie
    StarletteResponse._bugwatch_cookie_hook = True


def _install_upload_validation():
    if getattr(StarletteUploadFile, "_bugwatch_upload_hook", False):
        return
    import security

    original = StarletteUploadFile.read
    video_exts = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".mpeg", ".mpg"}
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
    audio_exts = {".mp3", ".wav", ".ogg", ".webm", ".m4a", ".mp4"}

    async def validated_read(self, *args, **kwargs):
        filename = getattr(self, "filename", "") or ""
        content_type = (getattr(self, "content_type", "") or "").split(";", 1)[0].strip().lower()
        ext = os.path.splitext(filename)[1].lower()
        if content_type.startswith("video/") or ext in video_exts:
            allowed = security.ALLOWED_VIDEO_MIMES
        elif content_type.startswith("image/") or ext in image_exts:
            allowed = security.ALLOWED_IMAGE_MIMES
        elif content_type.startswith("audio/") or ext in audio_exts:
            allowed = security.ALLOWED_AUDIO_MIMES
        else:
            raise __import__("fastapi").HTTPException(415, "Unsupported upload type")
        if not content_type:
            inferred = security.EXTENSION_MIMES.get(ext, "")
            content_type = inferred
        ok, message = security.validate_upload(filename, content_type, allowed)
        if not ok:
            raise __import__("fastapi").HTTPException(415, message)
        return await original(self, *args, **kwargs)

    StarletteUploadFile.read = validated_read
    StarletteUploadFile._bugwatch_upload_hook = True


def _install_safe_app_hooks(app_module):
    # Corrupt vault JSON or a non-dict vault must not take down auth middleware.
    if callable(getattr(app_module, "load_vault", None)) and not getattr(
            app_module, "_bugwatch_safe_vault", False):
        original_load_vault = app_module.load_vault

        def safe_load_vault():
            try:
                data = original_load_vault()
                return data if isinstance(data, dict) else {}
            except Exception as exc:
                print(f"[security] vault load failed safely: {type(exc).__name__}", flush=True)
                return {}

        app_module.load_vault = safe_load_vault
        app_module._bugwatch_safe_vault = True

    # Route calls to the process-tree-aware runner while preserving the
    # CompletedProcess-like interface expected by legacy app.py code.
    if callable(getattr(app_module, "_run_capture", None)) and not getattr(
            app_module, "_bugwatch_safe_capture", False):
        import process_manager
        import subprocess

        def safe_capture(args, timeout=600):
            result = process_manager.run_safe(list(args), timeout=timeout)
            return subprocess.CompletedProcess(
                args, result.returncode, result.stdout, result.stderr)

        app_module._run_capture = safe_capture
        app_module._bugwatch_safe_capture = True

    _install_upload_validation()
    _install_secure_cookie_hook()


def _install_queue_scope(app_module):
    """Attach the current account to queue jobs and enforce owner checks."""
    try:
        import store
        queue = app_module.image_queue.QUEUE
    except Exception:
        return
    if getattr(queue, "_bugwatch_scope_patched", False):
        return

    snapshot = getattr(app_module, "_img_settings_snapshot", None)
    if callable(snapshot):
        def scoped_snapshot(request):
            settings = dict(snapshot(request) or {})
            settings["_tenant_email"] = store.current_scope()
            return settings
        app_module._img_settings_snapshot = scoped_snapshot

    original_submit = queue.submit
    def scoped_submit(prompts, params, settings, project_id, metas=None):
        scoped = dict(settings or {})
        scoped["_tenant_email"] = store.current_scope()
        return original_submit(prompts, params, scoped, project_id, metas)
    queue.submit = scoped_submit

    original_get = queue.get_batch
    def owned_get(batch_id):
        batch = original_get(batch_id)
        if batch is None:
            return None
        owner = (queue._settings.get(batch_id) or {}).get("_tenant_email", "")
        if config.AUTH_REQUIRED and owner != store.current_scope():
            return None
        return batch
    queue.get_batch = owned_get

    original_cancel = queue.cancel
    def owned_cancel(batch_id):
        if owned_get(batch_id) is None:
            return False
        return original_cancel(batch_id)
    queue.cancel = owned_cancel

    original_retry_job = queue.retry_job
    def owned_retry_job(job_id, settings=None):
        job = queue._jobs.get(job_id)
        owner = ((queue._settings.get(job.batch_id) or {}).get("_tenant_email", "")
                 if job else None)
        if config.AUTH_REQUIRED and owner != store.current_scope():
            return False
        return original_retry_job(job_id, settings)
    queue.retry_job = owned_retry_job

    original_retry_failed = queue.retry_failed
    def owned_retry_failed(batch_id, settings=None):
        if owned_get(batch_id) is None:
            return 0
        return original_retry_failed(batch_id, settings)
    queue.retry_failed = owned_retry_failed

    render_fn = queue._render_fn
    if callable(render_fn):
        def scoped_render(prompt, params, settings, project_id):
            token = store.set_user_scope((settings or {}).get("_tenant_email", ""))
            try:
                return render_fn(prompt, params, settings, project_id)
            finally:
                store.reset_user_scope(token)
        queue._render_fn = scoped_render
    queue._bugwatch_scope_patched = True


class RuntimeSecurityMiddleware(BaseHTTPMiddleware):
    """Protect mounted data, auth attempts, and request account scope."""

    def __init__(self, app, verify_fn):
        super().__init__(app)
        self._verify_fn = verify_fn

    async def _call_scoped(self, request, call_next, email):
        path = request.url.path
        if not (path.startswith("/api/") or path == "/data"
                or path.startswith("/data/")):
            return await call_next(request)
        import store
        scope_token = store.set_user_scope(email or "")
        try:
            return await call_next(request)
        finally:
            store.reset_user_scope(scope_token)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        token = request.cookies.get("cs_session", "")
        email = self._verify_fn(token) if token else None

        if (request.method == "POST" and path == "/api/auth/logout"
                and token and email):
            _revoke(token)

        if path == "/data" or path.startswith("/data/"):
            if config.AUTH_REQUIRED and not email:
                return JSONResponse({"detail": "login required"}, status_code=401)
            if config.AUTH_REQUIRED:
                import store
                prefix = store.scope_url_prefix(email)
                if path != "/data" and not path.startswith(prefix):
                    return JSONResponse({"detail": "media not found"}, status_code=404)

        limiter = None
        client_ip = None
        if path == "/api/auth/login" or path == "/api/auth/signup":
            import security
            limiter = (security.login_limiter if path.endswith("/login")
                       else security.signup_limiter)
            client_ip = security.get_client_ip(request)
            if limiter.is_blocked(client_ip):
                return JSONResponse({"detail": "Too many attempts. Try again later."},
                                    status_code=429,
                                    headers={"Retry-After": "300"})
            limiter.record(client_ip)

        try:
            response = await self._call_scoped(request, call_next, email)
        except Exception:
            raise
        if limiter is not None and response.status_code < 400:
            limiter.reset(client_ip)
        return response


def install(app_module) -> None:
    """Install all wrapper hardening once."""
    if getattr(app_module, "_bugwatch_runtime_security", False):
        return
    secret = app_module._SESSION_KEY

    def sign(email: str) -> str:
        return _sign_session(secret, email)

    def verify(token: str) -> Optional[str]:
        return _verify_session(secret, token)

    _install_safe_app_hooks(app_module)
    _install_queue_scope(app_module)
    app_module._sign_session = sign
    app_module._verify_session = verify
    app_module.app.add_middleware(RuntimeSecurityMiddleware, verify_fn=verify)
    app_module._bugwatch_runtime_security = True
