"""Continuity Studio — FastAPI backend (extended).

Run:  uvicorn app:app --reload --port 8000   then open http://localhost:8000
"""
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from typing import List, Optional

# Windows consoles default to cp1252; our log lines contain emoji, arrows and
# em-dashes (— 🔁 ✗ →). Without this, a single print() of such a character
# raises UnicodeEncodeError, which can turn a HANDLED error into a 500 (e.g. the
# autopilot 'video step FAILED: …' log line). Force UTF-8 with replacement so a
# log line can never crash a request.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Suppress CMD-window flashes from child processes (ffmpeg / ffprobe / yt-dlp /
# whisper) on Windows. Safe no-op elsewhere. Imported here so it's active for
# both the packaged desktop app AND the plain `uvicorn app:app` dev server.
try:
    import nowindow
    nowindow.install()
except Exception:
    pass

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import pipeline
import store
import editor
from derouter import ImageClient, OpenRouterImageClient, PuterImageClient, DashScopeImageClient
from claude_client import ClaudeClient, extract_json

store.init()
app = FastAPI(title="Continuity Studio")

# CORS — allow the Flow browser extension (runs on labs.google) to call the
# /api/flow/* bridge endpoints. Extensions send Origin: chrome-extension://...
# and null; allow all so any browser context can reach the local bridge.
try:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:
    pass


# --- Writable config dir (packaged-app aware) ------------------------------ #
# In dev, vault.json / users.json / codes.json live next to this module. In a
# frozen build (PyInstaller) the module lives in a READ-ONLY temp dir, so the
# launcher points CS_CONFIG_DIR at a writable per-user location
# (e.g. %LOCALAPPDATA%\ContinuityStudio). Default keeps dev behaviour intact.
def _config_dir():
    d = os.environ.get("CS_CONFIG_DIR")
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d
    return os.path.dirname(__file__)



# --- Windows asyncio noise suppression ------------------------------------- #
# On Windows the Proactor event loop logs a full Traceback when a client drops
# an in-flight connection mid-stream (e.g. the browser scrubs/closes a <video>
# during a 206 range request): "ConnectionResetError: [WinError 10054]".
# It's harmless — the request was simply cancelled by the client — but it spams
# the log and trips watch-pattern alerts. Install a loop exception handler that
# swallows ONLY these connection-reset/abort callbacks and lets everything else
# through unchanged.
@app.on_event("startup")
def _silence_proactor_connection_lost():
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        return
    _default = loop.get_exception_handler()

    def _handler(_loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return  # client dropped the connection — benign, ignore
        msg = str(context.get("message", ""))
        if "_call_connection_lost" in msg or "WinError 10054" in str(exc):
            return
        if _default is not None:
            _default(_loop, context)
        else:
            _loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


# --- Auth Helpers ---
def load_users():
    path = os.path.join(_config_dir(), "users.json")
    if not os.path.exists(path): return {}
    with open(path, "r") as f: return json.load(f)

def save_users(users):
    path = os.path.join(_config_dir(), "users.json")
    with open(path, "w") as f: json.dump(users, f, indent=2)

def load_codes():
    path = os.path.join(_config_dir(), "codes.json")
    if not os.path.exists(path): return {}
    with open(path, "r") as f: return json.load(f)

def save_codes(codes):
    path = os.path.join(_config_dir(), "codes.json")
    with open(path, "w") as f: json.dump(codes, f, indent=2)

import vault_crypto

def load_vault():
    path = os.path.join(_config_dir(), "vault.json")
    if not os.path.exists(path):
        # First run of a packaged build: seed from the bundled vault.json (next
        # to the module) so the user's keys carry over into the writable dir.
        _seed = os.path.join(os.path.dirname(__file__), "vault.json")
        if _seed != path and os.path.exists(_seed):
            try:
                import shutil
                shutil.copyfile(_seed, path)
            except Exception:
                return {}
        else:
            return {}
    with open(path, "r") as f: raw = json.load(f)
    decrypted = vault_crypto.decrypt_vault(raw)
    if vault_crypto.is_encrypted() and vault_crypto.needs_migration(raw):
        save_vault(decrypted)
    return decrypted

def save_vault(vault):
    path = os.path.join(_config_dir(), "vault.json")
    encrypted = vault_crypto.encrypt_vault(vault)
    with open(path, "w") as f: json.dump(encrypted, f, indent=2)

# --- Session signing ---
import hashlib
import hmac
import base64 as _b64
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

_SESSION_KEY = config.SESSION_SECRET.encode()
_PUBLIC_PATHS = {"/", "/api/auth/login", "/api/auth/signup", "/api/auth/status"}

def _sign_session(email: str) -> str:
    payload = _b64.urlsafe_b64encode(email.encode()).decode()
    sig = hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"

def _verify_session(token: str) -> Optional[str]:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return _b64.urlsafe_b64decode(payload.encode()).decode()
    except Exception:
        return None

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000).hex()


def _run_capture(args, timeout=600):
    """subprocess.run(capture_output, text) with a hard timeout. A hung ffmpeg
    would otherwise block the worker thread forever; on timeout we return a
    CompletedProcess with a non-zero returncode so callers' existing
    `returncode != 0` checks treat it as a normal failure instead of hanging."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            args, 124, e.stdout or "",
            (e.stderr or "") + f"\nffmpeg timed out after {timeout}s")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Global upload size limit (500MB) — prevents OOM from huge file uploads.
    _MAX_UPLOAD = 500 * 1024 * 1024  # 500MB
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > _MAX_UPLOAD:
            return JSONResponse(
                {"detail": f"Upload too large ({int(cl)//1048576}MB). Max {_MAX_UPLOAD//1048576}MB."},
                status_code=413)

    email = ""
    session = request.cookies.get("cs_session", "")
    if session:
        email = _verify_session(session) or ""

    if config.AUTH_REQUIRED and not email:
        path = request.url.path
        if path not in _PUBLIC_PATHS and not path.startswith("/data/"):
            return JSONResponse({"detail": "login required"}, status_code=401)

    vault = load_vault()
    user_data = vault.get(email, {})
    request.state.user_email = email
    request.state.settings = {
        "api_key": user_data.get("api_key", ""),
        "base_url": user_data.get("base_url", config.BASE_URL),
        "model": user_data.get("model", config.MODEL),
        "multi_image_edit": user_data.get("multi_image_edit", config.MULTI_IMAGE_EDIT),
        "claude_api_key": user_data.get("claude_api_key", ""),
        "claude_base_url": user_data.get("claude_base_url", config.CLAUDE_BASE_URL),
        "claude_model": user_data.get("claude_model", config.CLAUDE_MODEL),
        "elevenlabs_api_key": user_data.get("elevenlabs_api_key", getattr(config, "ELEVENLABS_API_KEY", "")),
        "elevenlabs_voice_id": user_data.get("elevenlabs_voice_id", getattr(config, "ELEVENLABS_VOICE_ID", "")),
        "elevenlabs_model": user_data.get("elevenlabs_model", getattr(config, "ELEVENLABS_MODEL", "")),
        "voice_provider": user_data.get("voice_provider", "elevenlabs"),
        "mimo_api_key": user_data.get("mimo_api_key", getattr(config, "MIMO_API_KEY", "")),
        "mimo_voice_id": user_data.get("mimo_voice_id", getattr(config, "MIMO_VOICE_ID", "Chloe")),
        "mimo_model": user_data.get("mimo_model", getattr(config, "MIMO_MODEL", "mimo-v2.5-tts")),
        "deepgram_api_key": user_data.get("deepgram_api_key", getattr(config, "DEEPGRAM_API_KEY", "")),
        "deepgram_voice_id": user_data.get("deepgram_voice_id", getattr(config, "DEEPGRAM_VOICE_ID", "aura-2-thalia-en")),
        "deepgram_model": user_data.get("deepgram_model", getattr(config, "DEEPGRAM_MODEL", "aura-2-thalia-en")),
        "deepgram_encoding": user_data.get("deepgram_encoding", getattr(config, "DEEPGRAM_ENCODING", "mp3")),
        "webhook_url": user_data.get("webhook_url", ""),
        "image_provider": user_data.get("image_provider", "derouter"),
        "anthropic_api_key": user_data.get("anthropic_api_key", getattr(config, "ANTHROPIC_DIRECT_API_KEY", "")),
        "claude_provider": user_data.get("claude_provider", "derouter"),
        "ninerouter_api_key": user_data.get("ninerouter_api_key", getattr(config, "NINEROUTER_API_KEY", "")),
        "ninerouter_base_url": user_data.get("ninerouter_base_url", config.NINEROUTER_BASE_URL),
        "ninerouter_model": user_data.get("ninerouter_model", config.NINEROUTER_MODEL),
        "ninerouter_image_base_url": user_data.get("ninerouter_image_base_url", config.NINEROUTER_IMAGE_BASE_URL),
        "ninerouter_image_model": user_data.get("ninerouter_image_model", config.NINEROUTER_IMAGE_MODEL),
        "agentrouter_api_key": user_data.get("agentrouter_api_key", getattr(config, "AGENTROUTER_API_KEY", "")),
        "agentrouter_model": user_data.get("agentrouter_model", getattr(config, "AGENTROUTER_MODEL", "claude-sonnet-4-6")),
        "gemini_api_key": user_data.get("gemini_api_key", getattr(config, "GEMINI_API_KEY", "")),
        "gemini_model": user_data.get("gemini_model", getattr(config, "GEMINI_MODEL", "gemini-2.5-flash")),
        "gemini_base_url": user_data.get("gemini_base_url", getattr(config, "GEMINI_BASE_URL", "")),
        "puter_api_key": user_data.get("puter_api_key", ""),
        "puter_model": user_data.get("puter_model", "gpt-image-2"),
        "tokenbay_api_key": user_data.get("tokenbay_api_key", getattr(config, "TOKENBAY_API_KEY", "")),
        "tokenbay_model": user_data.get("tokenbay_model", getattr(config, "TOKENBAY_MODEL", "gemini-2.5-flash-image")),
        "tokenbay_base_url": user_data.get("tokenbay_base_url", getattr(config, "TOKENBAY_BASE_URL", "https://api.tokenbay.com/v1")),
        "dashscope_api_key": user_data.get("dashscope_api_key", getattr(config, "DASHSCOPE_API_KEY", "")),
        "dashscope_model": user_data.get("dashscope_model", getattr(config, "DASHSCOPE_MODEL", "wan2.7-image-pro")),
        "dashscope_base_url": user_data.get("dashscope_base_url", getattr(config, "DASHSCOPE_BASE_URL", "")),
    }
    response = await call_next(request)
    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


# --- Auth endpoints ---
class AuthIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def api_auth_signup(body: AuthIn):
    email = body.email.strip().lower()
    password = body.password.strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    users = load_users()
    if email in users:
        raise HTTPException(409, "Account already exists")
    salt = store.new_id("salt")
    users[email] = {"hash": _hash_password(password, salt), "salt": salt,
                    "created": store.now()}
    save_users(users)
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie("cs_session", _sign_session(email),
                    httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.post("/api/auth/login")
def api_auth_login(body: AuthIn):
    email = body.email.strip().lower()
    password = body.password.strip()
    users = load_users()
    user = users.get(email)
    if not user:
        raise HTTPException(401, "Invalid email or password")
    if _hash_password(password, user["salt"]) != user["hash"]:
        raise HTTPException(401, "Invalid email or password")
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie("cs_session", _sign_session(email),
                    httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.post("/api/auth/logout")
def api_auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cs_session")
    return resp


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    return {
        "logged_in": bool(request.state.user_email),
        "email": request.state.user_email,
        "auth_required": config.AUTH_REQUIRED,
    }

app.mount("/data", StaticFiles(directory=config.DATA_DIR), name="data")

# Runtime settings resolver (uses request state)
def get_user_settings(request: Request):
    return request.state.settings

def _has_ai_key(s: dict) -> bool:
    """True if the user has an API key for the active AI provider."""
    p = s.get("claude_provider", "derouter")
    if p == "anthropic":
        return bool(s.get("anthropic_api_key"))
    if p == "9router":
        return bool(s.get("ninerouter_api_key"))
    if p == "agentrouter":
        return bool(s.get("agentrouter_api_key"))
    if p == "gemini":
        return bool(s.get("gemini_api_key"))
    return bool(s.get("claude_api_key"))


def _resolve_claude(s: dict, model: str = None):
    """Return (api_key, base_url, model) for the active claude_provider.

    Providers:
      - 'derouter'     (default) — the derouter Anthropic-compatible proxy
      - 'anthropic'              — Anthropic's API directly
      - '9router'                — a local 9Router instance (token-saver / fallback
                                   router) serving an Anthropic-compatible API
      - 'agentrouter'            — AgentRouter proxy (https://agentrouter.org),
                                   Anthropic-compatible, auth via Bearer token
    """
    p = s.get("claude_provider", "derouter")
    if p == "anthropic" and s.get("anthropic_api_key"):
        return (s["anthropic_api_key"], config.ANTHROPIC_DIRECT_BASE_URL,
                model or s.get("claude_model"))
    if p == "9router":
        return (s.get("ninerouter_api_key", ""),
                (s.get("ninerouter_base_url") or config.NINEROUTER_BASE_URL),
                model or s.get("ninerouter_model") or config.NINEROUTER_MODEL)
    if p == "agentrouter":
        return (s.get("agentrouter_api_key", ""),
                getattr(config, "AGENTROUTER_BASE_URL", "https://agentrouter.org"),
                model or s.get("agentrouter_model") or getattr(config, "AGENTROUTER_MODEL", "claude-sonnet-4-6"))
    if p == "gemini":
        # Google AI Studio via its OpenAI-compatible endpoint. The model name is
        # NOT "claude", so claude_client routes it through the OpenAI chat path
        # (_msg_openai) automatically — cheap/free text + vision for scripting,
        # YouTube analysis and prompting.
        return (s.get("gemini_api_key", ""),
                (s.get("gemini_base_url") or config.GEMINI_BASE_URL),
                model or s.get("gemini_model") or config.GEMINI_MODEL)
    return (s.get("claude_api_key", ""),
            (s.get("claude_base_url") or config.CLAUDE_BASE_URL),
            model or s.get("claude_model"))

def _has_image_key(request: Request) -> bool:
    """True if the active image_provider is configured. Pollinations + local
    have no key — always 'configured' (they're free)."""
    s = request.state.settings
    if s.get("image_provider") == "puter":
        return bool(s.get("puter_api_key"))
    if s.get("image_provider") == "9router":
        return bool(s.get("ninerouter_api_key"))
    if s.get("image_provider") == "pollinations":
        return True   # no key required — public endpoint
    if s.get("image_provider") == "local":
        return True   # no key required — local model
    if s.get("image_provider") == "flow":
        return True   # no key — images generated externally via the Flow browser extension
    if s.get("image_provider") == "tokenbay":
        return bool(s.get("tokenbay_api_key"))
    if s.get("image_provider") == "dashscope":
        return bool(s.get("dashscope_api_key"))
    return bool(s.get("api_key"))


def _resolve_image(s: dict):
    """Return (api_key, base_url, model) for the active image_provider.

    Providers:
      - 'derouter' (default) — the configured image key / OpenAI-compatible proxy
      - 'puter'              — Puter REST API driver call.
      - '9router'            — route image gen through a local 9Router instance's
                               OpenAI-compatible endpoint, reusing the shared
                               9Router API key.
      - 'pollinations'       — 100% free, no API key, public pollinations.ai
                               endpoint. Models: flux / flux-schnell / turbo /
                               sd-xl / dall-e-3 / kd-midjourney / sana.
      - 'local'              — 100% free, runs entirely on this machine via
                               HuggingFace diffusers. Models: sdxl-turbo
                               (1-step, fastest, batch-friendly) / sdxl-base
                               (high quality) / flux-schnell (best quality,
                               needs ~12 GB VRAM).
    """
    if s.get("image_provider") == "puter":
        return (s.get("puter_api_key", ""),
                "https://api.puter.com/drivers/call",
                s.get("puter_model", "gpt-image-2"))
    if s.get("image_provider") == "9router":
        # Windows localhost resolution intermittently fails on this host.
        # Keep the OpenAI-compatible router strictly on IPv4 loopback.
        base = (s.get("ninerouter_image_base_url") or config.NINEROUTER_IMAGE_BASE_URL)
        base = base.replace("://localhost", "://127.0.0.1")
        return (s.get("ninerouter_api_key", ""), base,
                s.get("ninerouter_image_model") or config.NINEROUTER_IMAGE_MODEL)
    if s.get("image_provider") == "pollinations":
        # No api_key. The model id is the short id from the Pollinations
        # catalog (flux / flux-schnell / ...). The client translates it
        # into the right ?model= value at request time.
        from pollinations import DEFAULT_BASE_URL
        return ("",
                s.get("pollinations_base_url") or DEFAULT_BASE_URL,
                s.get("pollinations_model") or "flux")
    if s.get("image_provider") == "local":
        # Local diffusers — model id is the short name (sdxl-turbo /
        # sdxl-base / flux-schnell). No base_url / api_key needed.
        return ("", "", s.get("diffusers_model") or "sdxl-turbo")
    if s.get("image_provider") == "tokenbay":
        return (s.get("tokenbay_api_key", ""),
                (s.get("tokenbay_base_url") or config.TOKENBAY_BASE_URL),
                s.get("tokenbay_model") or config.TOKENBAY_MODEL)
    if s.get("image_provider") == "dashscope":
        return (s.get("dashscope_api_key", ""),
                (s.get("dashscope_base_url") or config.DASHSCOPE_BASE_URL),
                s.get("dashscope_model") or config.DASHSCOPE_MODEL)
    return (s.get("api_key", ""),
            s.get("base_url", config.BASE_URL),
            s.get("model", config.MODEL))


def _is_chat_image_model(model: str) -> bool:
    """True if this model uses chat completions for image generation instead of
    the OpenAI /images/edits + /images/generations endpoints.

    Models behind 9Router's ag/ prefix (AgentRouter → Gemini) generate images
    via chat completions with ``modalities: ["image", "text"]`` — the same
    mechanism as OpenRouter's Riverflow. Sending them to /images/edits returns
    500 and the fallback to /images/generations drops ALL reference images
    (style frames, character sheets), which is why "style copy is not working"
    and "not using reference image and character reference" when ag/ models are
    selected as the 9Router image model.

    This helper lets ``get_image_client`` pick ``OpenRouterImageClient`` (chat
    completions + inline vision refs) for these models instead of ``ImageClient``
    (OpenAI images API), so references actually get sent and style/character
    continuity works.
    """
    if not model:
        return False
    low = model.lower().strip()
    # ag/ = AgentRouter (Gemini Flash image gen via chat completions)
    if low.startswith("ag/"):
        return True
    # Explicit Gemini image-generation models (may be typed without prefix)
    if "gemini" in low and ("image" in low or "flash" in low):
        return True
    return False


def _client_from_settings(s: dict):
    """Factory to build the right ImageClient instance based on settings dict."""
    api_key, base_url, model = _resolve_image(s)
    if s.get("image_provider") == "pollinations":
        from pollinations import PollinationsImageClient
        return PollinationsImageClient(
            base_url=base_url,
            model=model,
            enhance=bool(s.get("pollinations_enhance", False)),
            private=bool(s.get("pollinations_private", False)),
            nologo=bool(s.get("pollinations_nologo", True)),
        )
    if s.get("image_provider") == "local":
        from diffusers import DiffusersImageClient
        return DiffusersImageClient(
            model=model,
            steps=s.get("diffusers_steps"),
            guidance=s.get("diffusers_guidance"),
        )
    if s.get("image_provider") == "puter":
        return PuterImageClient(api_key=api_key, base_url=base_url, model=model)
    # TokenBay: chat-completions image gen (gemini-2.5-flash-image returns
    # base64 PNG inline in assistant message — same pattern as OpenRouter).
    if s.get("image_provider") == "tokenbay":
        print(f"[image] tokenbay provider: model={model} base={base_url} — "
              f"using OpenRouterImageClient (chat completions)", flush=True)
        return OpenRouterImageClient(
            api_key=api_key, base_url=base_url, model=model,
        )
    # DashScope / QwenCloud: Wan 2.7 + Qwen Image models via the
    # multimodal-generation endpoint. Returns image URLs, not base64.
    if s.get("image_provider") == "dashscope":
        print(f"[image] dashscope provider: model={model} base={base_url}",
              flush=True)
        return DashScopeImageClient(
            api_key=api_key, base_url=base_url, model=model,
        )
    # 9Router with a chat-based image model (ag/ = Gemini Flash image-gen):
    # these models work via chat/completions, NOT /images/edits. Use the
    # OpenRouterImageClient which sends refs as inline vision + extracts the
    # generated image from the chat response.
    if s.get("image_provider") == "9router" and _is_chat_image_model(model):
        print(f"[image] 9router chat-image model detected: {model} — "
              f"using OpenRouterImageClient (chat completions with refs)",
              flush=True)
        return OpenRouterImageClient(
            api_key=api_key, base_url=base_url, model=model,
        )
    return ImageClient(api_key=api_key, base_url=base_url, model=model)


def get_image_client(request: Request):
    """Return the right ImageClient for the active image_provider.
    Pollinations + local get their own client classes (different APIs);
    derouter/9router/direct share ``derouter.ImageClient``.

    SPECIAL CASE: 9Router models that generate images via CHAT COMPLETIONS
    (ag/ prefix = Gemini Flash image-gen) get routed through
    ``OpenRouterImageClient`` instead, which sends reference images as inline
    vision content in chat/completions. Without this, the /images/edits
    endpoint 500s on 9Router for chat-based models and falls back to
    prompt-only generation — losing ALL style frames, character sheets and
    continuity references."""
    return _client_from_settings(request.state.settings)

def get_claude_client(request: Request = None) -> ClaudeClient:
    if request:
        api_key, base_url, model = _resolve_claude(request.state.settings)
    else:
        api_key, base_url, model = (config.CLAUDE_API_KEY,
                                    config.CLAUDE_BASE_URL, config.CLAUDE_MODEL)
    return ClaudeClient(api_key=api_key, base_url=base_url, model=model)


def get_voice_client(request: Request, voice_id: str = None):
    import voice
    s = request.state.settings
    provider = (s.get("voice_provider") or "elevenlabs").lower()
    if provider == "mimo":
        return voice.MimoVoiceClient(
            api_key=s.get("mimo_api_key", ""),
            model=s.get("mimo_model", ""),
            voice_id=voice_id or s.get("mimo_voice_id", ""),
        )
    if provider == "deepgram":
        return voice.DeepgramVoiceClient(
            api_key=s.get("deepgram_api_key", ""),
            model=s.get("deepgram_model", ""),
            voice_id=voice_id or s.get("deepgram_voice_id", ""),
            encoding=s.get("deepgram_encoding", "mp3"),
        )
    if provider == "piper":
        # 100% free, runs locally on CPU (or CUDA GPU if onnxruntime-gpu is
        # installed and PIPER_USE_GPU=true). No key, no per-char cost.
        return voice.PiperVoiceClient(
            voice_id=voice_id or s.get("piper_voice_id") or config.PIPER_VOICE,
            use_gpu=s.get("piper_use_gpu"),
            length_scale=s.get("piper_length_scale"),
            noise_scale=s.get("piper_noise_scale"),
            noise_w_scale=s.get("piper_noise_w_scale"),
        )
    return voice.VoiceClient(
        api_key=s["elevenlabs_api_key"],
        model=s["elevenlabs_model"],
        voice_id=voice_id or s["elevenlabs_voice_id"],
    )


def _has_voice_key(s) -> bool:
    """True if the active voice provider is configured. Piper has no key —
    any piper install is always considered 'configured' (it's free and local)."""
    provider = (s.get("voice_provider") or "elevenlabs").lower()
    if provider == "mimo":
        return bool(s.get("mimo_api_key"))
    if provider == "deepgram":
        return bool(s.get("deepgram_api_key"))
    if provider == "piper":
        return True   # no key required — local model
    return bool(s.get("elevenlabs_api_key"))


def _diffusers_model_catalog():
    """Return the bundled diffusers model list (id + display name + size)
    so the Settings panel can render a dropdown. Lazy-import so the import
    chain doesn't fail when the user hasn't installed diffusers yet."""
    try:
        from diffusers import DIFFUSERS_MODELS
        return [{"id": m["id"], "name": m["name"],
                 "hf_repo": m["hf_repo"],
                 "approx_size_gb": m.get("approx_size_gb", 0)}
                for m in DIFFUSERS_MODELS]
    except Exception:
        return []


def _diffusers_available():
    """Wrapper for the lazy-import diffusers_available() — handles import errors."""
    try:
        from diffusers import diffusers_available as _da
        return _da()
    except Exception:
        return False


def _pollinations_model_catalog():
    """Return the bundled Pollinations model list (id + display name) so the
    Settings panel can render a dropdown. Lazy-import so the import chain
    doesn't fail when the user hasn't installed the deps yet (none
    actually required — just stdlib + requests)."""
    try:
        from pollinations import POLLINATIONS_MODELS
        return [{"id": m["id"], "name": m["name"], "model": m["model"]}
                for m in POLLINATIONS_MODELS]
    except Exception:
        return []


def _piper_voices_list():
    """Safe loader for the Piper voice catalog — works even when piper-tts
    is not installed (returns [] so the UI hides the option instead of
    crashing)."""
    try:
        import voice as _voice
        return list(getattr(_voice.PiperVoiceClient, "BUILTIN_VOICES", []) or [])
    except Exception:
        return []


def _voice_default_id(s):
    """Default voice id for the ACTIVE provider. Each provider has a different
    voice-id namespace (Eleven uses UUIDs, MiMo uses names like 'Chloe',
    Deepgram uses 'aura-2-thalia-en', Piper uses short ids like 'amy'), so
    defaulting to the wrong provider's voice makes TTS 400. Always pick the
    active provider's voice."""
    provider = (s.get("voice_provider") or "elevenlabs").lower()
    if provider == "mimo":
        return s.get("mimo_voice_id") or "Chloe"
    if provider == "deepgram":
        return s.get("deepgram_voice_id") or "aura-2-thalia-en"
    if provider == "piper":
        return s.get("piper_voice_id") or "amy"
    return s.get("elevenlabs_voice_id") or ""


# --------------------------------------------------------------------------- #
#  Static page
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    """The Continuity Studio tool."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/favicon.png")
@app.get("/favicon.ico")
def favicon():
    """App icon (browser tab / fallback). Bundled in static/favicon.png."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "favicon.png"))


# --------------------------------------------------------------------------- #
#  State + settings
# --------------------------------------------------------------------------- #
@app.get("/api/state")
def api_state(request: Request):
    s = request.state.settings
    return {
        "state": store.load_state(),
        "config": {
            "model": s["model"],
            "base_url": s["base_url"],
            "has_api_key": bool(s["api_key"]),
            "multi_image_edit": s["multi_image_edit"],
            "claude_model": s["claude_model"],
            "claude_base_url": s["claude_base_url"],
            "has_claude_key": bool(s["claude_api_key"] or s.get("anthropic_api_key") or s.get("agentrouter_api_key")),
            "claude_models": config.CLAUDE_MODELS,
            "has_elevenlabs_key": bool(s["elevenlabs_api_key"]),
            "elevenlabs_voice_id": s["elevenlabs_voice_id"],
            "elevenlabs_model": s["elevenlabs_model"],
            "voice_provider": s.get("voice_provider", "elevenlabs"),
            "has_mimo_key": bool(s.get("mimo_api_key")),
            "mimo_voice_id": s.get("mimo_voice_id", "Chloe"),
            "has_deepgram_key": bool(s.get("deepgram_api_key")),
            "deepgram_voice_id": s.get("deepgram_voice_id", "aura-2-thalia-en"),
            "deepgram_model": s.get("deepgram_model", "aura-2-thalia-en"),
            "deepgram_encoding": s.get("deepgram_encoding", "mp3"),
            "has_voice_key": _has_voice_key(s),
            "webhook_url": s.get("webhook_url", ""),
            "has_webhook": bool(s.get("webhook_url")),
            "default_size": config.DEFAULT_SIZE,
            "default_quality": config.DEFAULT_QUALITY,
            "sizes": config.SUPPORTED_SIZES,
            "qualities": config.SUPPORTED_QUALITIES,
            "vault_encrypted": vault_crypto.is_encrypted(),
            "google_configured": bool(config.GOOGLE_CLIENT_ID),
            "image_provider": s.get("image_provider", "derouter"),
            "pollinations_model": s.get("pollinations_model", "flux"),
            "pollinations_enhance": bool(s.get("pollinations_enhance", False)),
            "pollinations_private": bool(s.get("pollinations_private", False)),
            "pollinations_nologo": bool(s.get("pollinations_nologo", True)),
            "pollinations_models": _pollinations_model_catalog(),
            "diffusers_model": s.get("diffusers_model", "sdxl-turbo"),
            "diffusers_models": _diffusers_model_catalog(),
            "diffusers_available": _diffusers_available(),
            "has_anthropic_key": bool(s.get("anthropic_api_key")),
            "claude_provider": s.get("claude_provider", "derouter"),
            "has_gemini_key": bool(s.get("gemini_api_key")),
            "gemini_model": s.get("gemini_model", config.GEMINI_MODEL),
            "gemini_models": config.GEMINI_MODELS,
            "ninerouter_base_url": s.get("ninerouter_base_url", config.NINEROUTER_BASE_URL),
            "ninerouter_model": s.get("ninerouter_model", config.NINEROUTER_MODEL),
            "ninerouter_models": config.NINEROUTER_MODELS,
            "ninerouter_image_base_url": s.get("ninerouter_image_base_url", config.NINEROUTER_IMAGE_BASE_URL),
            "ninerouter_image_model": s.get("ninerouter_image_model", config.NINEROUTER_IMAGE_MODEL),
            "has_ninerouter_key": bool(s.get("ninerouter_api_key")),
            "agentrouter_model": s.get("agentrouter_model", getattr(config, "AGENTROUTER_MODEL", "claude-sonnet-4-6")),
            "agentrouter_models": getattr(config, "AGENTROUTER_MODELS", []),
            "has_agentrouter_key": bool(s.get("agentrouter_api_key")),
            "puter_model": s.get("puter_model", "gpt-image-2"),
            "has_puter_key": bool(s.get("puter_api_key")),
            "tokenbay_model": s.get("tokenbay_model", getattr(config, "TOKENBAY_MODEL", "gemini-2.5-flash-image")),
            "tokenbay_base_url": s.get("tokenbay_base_url", getattr(config, "TOKENBAY_BASE_URL", "https://api.tokenbay.com/v1")),
            "tokenbay_models": getattr(config, "TOKENBAY_MODELS", ["gemini-2.5-flash-image"]),
            "has_tokenbay_key": bool(s.get("tokenbay_api_key")),
            "dashscope_model": s.get("dashscope_model", getattr(config, "DASHSCOPE_MODEL", "wan2.7-image-pro")),
            "dashscope_base_url": s.get("dashscope_base_url", getattr(config, "DASHSCOPE_BASE_URL", "")),
            "dashscope_models": getattr(config, "DASHSCOPE_MODELS", ["wan2.7-image-pro"]),
            "has_dashscope_key": bool(s.get("dashscope_api_key")),
        },
    }


# --------------------------------------------------------------------------- #
#  Projects (multi-project switcher)
# --------------------------------------------------------------------------- #
class ProjectIn(BaseModel):
    name: str = ""
    master_prompt: str = ""


@app.get("/api/projects")
def api_projects():
    return store.list_projects()


@app.post("/api/projects")
def api_create_project(p: ProjectIn):
    # Sanitize project name to prevent stored XSS (rendered in innerHTML).
    import html
    safe_name = html.escape((p.name or "").strip()[:80]) or "Untitled project"
    pid = store.create_project(safe_name, p.master_prompt)
    out = store.list_projects()
    out["id"] = pid
    return out


@app.post("/api/projects/{pid}/duplicate")
def api_duplicate_project(pid: str):
    try:
        new_pid = store.duplicate_project(pid)
    except Exception as e:
        raise HTTPException(404, str(e))
    out = store.list_projects()
    out["id"] = new_pid
    return out


@app.post("/api/projects/{pid}/switch")
def api_switch_project(pid: str):
    try:
        store.switch_project(pid)
    except Exception as e:
        raise HTTPException(404, str(e))
    return {"ok": True, **store.list_projects()}


@app.post("/api/projects/{pid}/rename")
def api_rename_project(pid: str, p: ProjectIn):
    store.rename_project(pid, p.name)
    return {"ok": True, **store.list_projects()}


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    store.delete_project(pid)
    return {"ok": True, **store.list_projects()}


class SettingsIn(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    multi_image_edit: Optional[bool] = None
    claude_api_key: Optional[str] = None
    claude_base_url: Optional[str] = None
    claude_model: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_model: Optional[str] = None
    voice_provider: Optional[str] = None
    mimo_api_key: Optional[str] = None
    mimo_voice_id: Optional[str] = None
    mimo_model: Optional[str] = None
    deepgram_api_key: Optional[str] = None
    deepgram_voice_id: Optional[str] = None
    deepgram_model: Optional[str] = None
    deepgram_encoding: Optional[str] = None
    piper_voice_id: Optional[str] = None
    piper_use_gpu: Optional[bool] = None
    piper_length_scale: Optional[float] = None
    piper_noise_scale: Optional[float] = None
    piper_noise_w_scale: Optional[float] = None
    pollinations_model: Optional[str] = None
    pollinations_enhance: Optional[bool] = None
    pollinations_private: Optional[bool] = None
    pollinations_nologo: Optional[bool] = None
    pollinations_base_url: Optional[str] = None
    diffusers_model: Optional[str] = None
    diffusers_steps: Optional[int] = None
    diffusers_guidance: Optional[float] = None
    webhook_url: Optional[str] = None
    image_provider: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    claude_provider: Optional[str] = None
    ninerouter_api_key: Optional[str] = None
    ninerouter_base_url: Optional[str] = None
    ninerouter_model: Optional[str] = None
    ninerouter_image_base_url: Optional[str] = None
    ninerouter_image_model: Optional[str] = None
    agentrouter_api_key: Optional[str] = None
    agentrouter_model: Optional[str] = None
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None
    gemini_base_url: Optional[str] = None
    puter_api_key: Optional[str] = None
    puter_model: Optional[str] = None
    tokenbay_api_key: Optional[str] = None
    tokenbay_model: Optional[str] = None
    tokenbay_base_url: Optional[str] = None
    dashscope_api_key: Optional[str] = None
    dashscope_model: Optional[str] = None
    dashscope_base_url: Optional[str] = None


@app.post("/api/settings")
def api_settings(s: SettingsIn, request: Request):
    email = request.state.user_email
    vault = load_vault()
    user_settings = vault.get(email, {})

    if s.api_key is not None: user_settings["api_key"] = s.api_key.strip()
    if s.base_url: user_settings["base_url"] = s.base_url.strip().rstrip("/")
    if s.model: user_settings["model"] = s.model.strip()
    if s.multi_image_edit is not None: user_settings["multi_image_edit"] = s.multi_image_edit
    if s.claude_api_key is not None: user_settings["claude_api_key"] = s.claude_api_key.strip()
    if s.claude_base_url: user_settings["claude_base_url"] = s.claude_base_url.strip().rstrip("/")
    if s.claude_model: user_settings["claude_model"] = s.claude_model.strip()
    if s.elevenlabs_api_key is not None: user_settings["elevenlabs_api_key"] = s.elevenlabs_api_key.strip()
    if s.elevenlabs_voice_id: user_settings["elevenlabs_voice_id"] = s.elevenlabs_voice_id.strip()
    if s.elevenlabs_model: user_settings["elevenlabs_model"] = s.elevenlabs_model.strip()
    if s.voice_provider: user_settings["voice_provider"] = s.voice_provider.strip()
    if s.mimo_api_key is not None: user_settings["mimo_api_key"] = s.mimo_api_key.strip()
    if s.mimo_voice_id: user_settings["mimo_voice_id"] = s.mimo_voice_id.strip()
    if s.mimo_model: user_settings["mimo_model"] = s.mimo_model.strip()
    if s.deepgram_api_key is not None: user_settings["deepgram_api_key"] = s.deepgram_api_key.strip()
    if s.deepgram_voice_id: user_settings["deepgram_voice_id"] = s.deepgram_voice_id.strip()
    if s.deepgram_model: user_settings["deepgram_model"] = s.deepgram_model.strip()
    if s.deepgram_encoding: user_settings["deepgram_encoding"] = s.deepgram_encoding.strip().lower()
    if s.piper_voice_id: user_settings["piper_voice_id"] = s.piper_voice_id.strip()
    if s.piper_use_gpu is not None: user_settings["piper_use_gpu"] = bool(s.piper_use_gpu)
    if s.piper_length_scale is not None: user_settings["piper_length_scale"] = float(s.piper_length_scale)
    if s.piper_noise_scale is not None: user_settings["piper_noise_scale"] = float(s.piper_noise_scale)
    if s.piper_noise_w_scale is not None: user_settings["piper_noise_w_scale"] = float(s.piper_noise_w_scale)
    if s.webhook_url is not None: user_settings["webhook_url"] = s.webhook_url.strip()
    if s.image_provider: user_settings["image_provider"] = s.image_provider.strip()
    if s.anthropic_api_key is not None: user_settings["anthropic_api_key"] = s.anthropic_api_key.strip()
    if s.claude_provider: user_settings["claude_provider"] = s.claude_provider.strip()
    if s.ninerouter_api_key is not None: user_settings["ninerouter_api_key"] = s.ninerouter_api_key.strip()
    if s.ninerouter_base_url: user_settings["ninerouter_base_url"] = s.ninerouter_base_url.strip().rstrip("/")
    if s.ninerouter_model: user_settings["ninerouter_model"] = s.ninerouter_model.strip()
    if s.ninerouter_image_base_url: user_settings["ninerouter_image_base_url"] = s.ninerouter_image_base_url.strip().rstrip("/")
    if s.ninerouter_image_model: user_settings["ninerouter_image_model"] = s.ninerouter_image_model.strip()
    if s.agentrouter_api_key is not None: user_settings["agentrouter_api_key"] = s.agentrouter_api_key.strip()
    if s.agentrouter_model: user_settings["agentrouter_model"] = s.agentrouter_model.strip()
    if s.gemini_api_key is not None: user_settings["gemini_api_key"] = s.gemini_api_key.strip()
    if s.gemini_model: user_settings["gemini_model"] = s.gemini_model.strip()
    if s.gemini_base_url: user_settings["gemini_base_url"] = s.gemini_base_url.strip().rstrip("/")
    if s.pollinations_model: user_settings["pollinations_model"] = s.pollinations_model.strip()
    if s.pollinations_enhance is not None: user_settings["pollinations_enhance"] = bool(s.pollinations_enhance)
    if s.pollinations_private is not None: user_settings["pollinations_private"] = bool(s.pollinations_private)
    if s.pollinations_nologo is not None: user_settings["pollinations_nologo"] = bool(s.pollinations_nologo)
    if s.pollinations_base_url: user_settings["pollinations_base_url"] = s.pollinations_base_url.strip().rstrip("/")
    if s.gemini_api_key is not None: user_settings["gemini_api_key"] = s.gemini_api_key.strip()
    if s.gemini_model: user_settings["gemini_model"] = s.gemini_model.strip()
    if s.gemini_base_url: user_settings["gemini_base_url"] = s.gemini_base_url.strip().rstrip("/")
    if s.puter_api_key is not None: user_settings["puter_api_key"] = s.puter_api_key.strip()
    if s.puter_model: user_settings["puter_model"] = s.puter_model.strip()
    if s.tokenbay_api_key is not None: user_settings["tokenbay_api_key"] = s.tokenbay_api_key.strip()
    if s.tokenbay_model: user_settings["tokenbay_model"] = s.tokenbay_model.strip()
    if s.tokenbay_base_url: user_settings["tokenbay_base_url"] = s.tokenbay_base_url.strip().rstrip("/")
    if s.dashscope_api_key is not None: user_settings["dashscope_api_key"] = s.dashscope_api_key.strip()
    if s.dashscope_model: user_settings["dashscope_model"] = s.dashscope_model.strip()
    if s.dashscope_base_url: user_settings["dashscope_base_url"] = s.dashscope_base_url.strip().rstrip("/")

    vault[email] = user_settings
    save_vault(vault)

    # Return key statuses so the UI can update instantly
    return {
        "ok": True,
        "has_api_key": (
            True
            if user_settings.get("image_provider") in ("pollinations", "local")
            else bool(user_settings.get("puter_api_key"))
            if user_settings.get("image_provider") == "puter"
            else bool(user_settings.get("ninerouter_api_key"))
            if user_settings.get("image_provider") == "9router"
            else bool(user_settings.get("tokenbay_api_key"))
            if user_settings.get("image_provider") == "tokenbay"
            else bool(user_settings.get("dashscope_api_key"))
            if user_settings.get("image_provider") == "dashscope"
            else bool(user_settings.get("api_key"))
        ),
        "has_claude_key": bool(user_settings.get("claude_api_key") or user_settings.get("anthropic_api_key") or user_settings.get("agentrouter_api_key")),
        "has_elevenlabs_key": bool(user_settings.get("elevenlabs_api_key")),
        "voice_provider": user_settings.get("voice_provider", "elevenlabs"),
        "has_mimo_key": bool(user_settings.get("mimo_api_key")),
        "has_deepgram_key": bool(user_settings.get("deepgram_api_key")),
        "piper_voice_id": user_settings.get("piper_voice_id") or config.PIPER_VOICE,
        "piper_use_gpu": bool(user_settings.get("piper_use_gpu", config.PIPER_USE_GPU)),
        "piper_voices": _piper_voices_list(),
        "has_voice_key": (
            bool(user_settings.get("mimo_api_key"))
            if user_settings.get("voice_provider") == "mimo"
            else bool(user_settings.get("deepgram_api_key"))
            if user_settings.get("voice_provider") == "deepgram"
            else True  # piper — no key required (local model)
            if user_settings.get("voice_provider") == "piper"
            else bool(user_settings.get("elevenlabs_api_key"))
        ),
        "has_anthropic_key": bool(user_settings.get("anthropic_api_key")),
        "has_ninerouter_key": bool(user_settings.get("ninerouter_api_key")),
        "has_agentrouter_key": bool(user_settings.get("agentrouter_api_key")),
        "has_gemini_key": bool(user_settings.get("gemini_api_key")),
        "has_puter_key": bool(user_settings.get("puter_api_key")),
        "has_tokenbay_key": bool(user_settings.get("tokenbay_api_key")),
        "tokenbay_model": user_settings.get("tokenbay_model", "gemini-2.5-flash-image"),
        "tokenbay_base_url": user_settings.get("tokenbay_base_url", "https://api.tokenbay.com/v1"),
        "tokenbay_models": getattr(config, "TOKENBAY_MODELS", ["gemini-2.5-flash-image"]),
        "has_dashscope_key": bool(user_settings.get("dashscope_api_key")),
        "dashscope_model": user_settings.get("dashscope_model", "wan2.7-image-pro"),
        "dashscope_base_url": user_settings.get("dashscope_base_url", config.DASHSCOPE_BASE_URL),
        "dashscope_models": getattr(config, "DASHSCOPE_MODELS", ["wan2.7-image-pro"]),
        "image_provider": user_settings.get("image_provider", "derouter"),
        "claude_provider": user_settings.get("claude_provider", "derouter"),
    }


class NineRouterModelsIn(BaseModel):
    api_key: Optional[str] = None      # falls back to the saved key
    base_url: Optional[str] = None     # falls back to the saved base URL


@app.post("/api/9router/models")
def api_9router_models(body: NineRouterModelsIn, request: Request):
    """List the model ids actually served by the user's running 9Router, so the
    Settings picker only offers models whose provider is really connected
    (avoids 'No active credentials for provider: …' errors)."""
    s = request.state.settings
    key = (body.api_key or s.get("ninerouter_api_key") or "").strip()
    base = (body.base_url or s.get("ninerouter_base_url")
            or config.NINEROUTER_BASE_URL).strip().rstrip("/")
    # Strip any image endpoints or /v1 suffixes to get the clean root origin
    for suffix in ("/images/generations", "/images/edits", "/images", "/v1"):
        if base.endswith(suffix):
            base = base[:-len(suffix)].rstrip("/")
    if not key:
        raise HTTPException(400, "no 9Router API key — paste one first")
    try:
        import requests as _rq
        r = _rq.get(f"{base}/v1/models",
                    headers={"Authorization": f"Bearer {key}"}, timeout=10)
        r.raise_for_status()
        ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    except Exception as e:
        raise HTTPException(502, f"could not list 9Router models: {e}")
    return {"models": ids, "base_url": base}


@app.get("/api/health")
def api_health(request: Request):
    s = request.state.settings
    img_provider = s.get("image_provider", "derouter")
    image_status = get_image_client(request).ping()
    image_status["multi_image_edit"] = s["multi_image_edit"]
    claude_status = get_claude_client(request).ping()
    voice_status = get_voice_client(request).ping()
    return {"image": image_status, "claude": claude_status, "voice": voice_status,
            "derouter": image_status, "image_provider": img_provider}


@app.get("/api/usage")
def api_usage():
    return store.get_usage()


@app.post("/api/usage/reset")
def api_usage_reset():
    store._write_usage([])
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Master prompt
# --------------------------------------------------------------------------- #
class MasterIn(BaseModel):
    master_prompt: str = ""


@app.post("/api/master")
def api_master(m: MasterIn):
    st = store.load_state()
    st["master_prompt"] = m.master_prompt
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Video -> frames -> style anchors
# --------------------------------------------------------------------------- #
@app.post("/api/video")
async def api_video(
    file: UploadFile = File(...),
    fps: float = Form(1.0),
    max_frames: int = Form(40),
):
    import video as videomod
    _fn = os.path.basename((file.filename or "video.mp4").replace("..", ""))
    dest = os.path.join(
        store.UPLOADS_DIR, store.new_id("upload") + "_" + _fn
    )
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file — upload a real video.")
    with open(dest, "wb") as f:
        f.write(data)
    try:
        urls = videomod.extract_frames(dest, fps=fps, max_frames=max_frames)
    except Exception as e:
        raise HTTPException(500, f"frame extraction failed: {e}")
    return {"frames": urls, "video_path": dest}


class StyleFramesIn(BaseModel):
    urls: List[str] = []


@app.post("/api/style-frames")
def api_style_frames(s: StyleFramesIn):
    st = store.load_state()
    st["style_frames"] = [{"id": store.new_id("frame"), "url": u} for u in s.urls]
    store.save_state(st)
    return {"ok": True, "count": len(st["style_frames"])}


# --------------------------------------------------------------------------- #
#  Scene detection / per-frame analysis
# --------------------------------------------------------------------------- #
@app.post("/api/scene-detect")
async def api_scene_detect(file: UploadFile = File(...), threshold: float = Form(0.4)):
    _fn = os.path.basename((file.filename or "video.mp4").replace("..", ""))
    dest = os.path.join(
        store.UPLOADS_DIR, store.new_id("scene") + "_" + _fn
    )
    with open(dest, "wb") as f:
        f.write(await file.read())
    try:
        times = editor.detect_scenes(dest, threshold=threshold)
        dur = editor.probe_duration(dest)
    except Exception as e:
        raise HTTPException(500, f"scene detection failed: {e}")
    return {"scene_changes": times, "duration": dur, "video_path": dest}


class AnalyseIn(BaseModel):
    image_url: str
    question: str = ""


@app.post("/api/analyse-scene")
def api_analyse_scene(a: AnalyseIn, request: Request):
    try:
        img = store.read_image(a.image_url)
    except Exception as e:
        raise HTTPException(400, f"unreadable image: {e}")
    try:
        text = get_claude_client(request).analyse_scene(
            pipeline.downsize_for_vision(img), a.question
        )
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")
    return {"analysis": text}


# --------------------------------------------------------------------------- #
#  Characters: single + bulk + upload
# --------------------------------------------------------------------------- #
class CharacterIn(BaseModel):
    name: str
    description: str = ""
    size: Optional[str] = None
    quality: Optional[str] = None


@app.post("/api/characters")
def api_create_character(c: CharacterIn, request: Request):
    if not c.name.strip():
        raise HTTPException(400, "name is required")
    st = store.load_state()
    client = get_image_client(request)
    prompt = pipeline.build_sheet_prompt(st["master_prompt"], c.name, c.description,
                                         style_notes=st.get("style_notes", ""))
    try:
        img = client.generate(
            prompt,
            size=c.size or config.DEFAULT_SIZE,
            quality=c.quality or config.DEFAULT_QUALITY,
        )
    except Exception as e:
        raise HTTPException(500, f"sheet generation failed: {e}")
    rec = {
        "id": store.new_id("char"),
        "name": c.name.strip(),
        "description": c.description.strip(),
        "sheet_url": store.write_image("characters", img),
        "prompt": prompt,
        "source": "generated",
        "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    store.log_usage("image", 1, 0.08)
    return rec


class CharacterBatchIn(BaseModel):
    text: str
    size: Optional[str] = None
    quality: Optional[str] = None


@app.post("/api/characters/batch")
def api_create_characters_batch(b: CharacterBatchIn, request: Request):
    entries = pipeline.parse_character_batch(b.text)
    if not entries:
        raise HTTPException(400, "no character entries found (separate with blank lines)")
    st = store.load_state()
    client = get_image_client(request)
    created, errors = [], []
    for e in entries:
        try:
            prompt = pipeline.build_sheet_prompt(
                st["master_prompt"], e["name"], e["description"],
                style_notes=st.get("style_notes", ""))
            # If YT/style frames are pinned, use them as STYLE refs for the sheet
            # so generated characters match the analysed source look.
            _style_refs, _labels = [], []
            for _sf in (st.get("style_frames") or [])[:config.STYLE_REF_COUNT]:
                try:
                    _style_refs.append(store.read_image(_sf["url"]))
                    _labels.append("STYLE REF — match art style ONLY")
                except Exception:
                    pass
            if _style_refs:
                _multi = bool(request and request.state.settings.get("multi_image_edit"))
                _sheet_size = b.size or config.DEFAULT_SIZE
                _edit_prompt = (prompt + "\n\nUse the attached image(s) ONLY as "
                                "art-style references from the source video. "
                                "Copy their line work, palette, texture, face/body "
                                "simplicity and proportions; draw THIS named "
                                "character, not the people, poses or scenes in the "
                                "references.")
                if _multi and len(_style_refs) > 1:
                    _edit_prompt += ("\n\nATTACHED REFERENCE IMAGES (in order) — "
                                     "every one is a STYLE REF (source-video frame). "
                                     "Use them ONLY for art style; ignore their "
                                     "content, composition and characters.")
                _send = (_style_refs if _multi else
                         ([pipeline.contact_sheet(_style_refs, labels=_labels,
                                                  target_size=_sheet_size)]
                          if len(_style_refs) > 1 else _style_refs))
                img = client.edit(_edit_prompt, _send,
                                  size=_sheet_size,
                                  quality=b.quality or config.DEFAULT_QUALITY,
                                  multi_image_edit=_multi)
            else:
                img = client.generate(
                    prompt,
                    size=b.size or config.DEFAULT_SIZE,
                    quality=b.quality or config.DEFAULT_QUALITY,
                )
            rec = {
                "id": store.new_id("char"),
                "name": e["name"],
                "description": e["description"],
                "sheet_url": store.write_image("characters", img),
                "prompt": prompt,
                "source": "generated",
                "created": store.now(),
            }
            st["characters"].append(rec)
            store.save_state(st)
            created.append(rec)
            store.log_usage("image", 1, 0.08)
        except Exception as ex:
            errors.append({"name": e["name"], "error": str(ex)})
    return {"created": created, "errors": errors}


@app.post("/api/characters/upload")
async def api_upload_character(
    name: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    if not name.strip():
        raise HTTPException(400, "name is required")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    # Determine extension by the upload's filename, default .png.
    ext = (os.path.splitext(file.filename or "")[1] or ".png").lstrip(".").lower()
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        ext = "png"
    url = store.write_image("characters", data, ext=ext)
    st = store.load_state()
    rec = {
        "id": store.new_id("char"),
        "name": name.strip(),
        "description": description.strip(),
        "sheet_url": url,
        "prompt": "(uploaded sheet)",
        "source": "uploaded",
        "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    return rec


@app.delete("/api/characters/{cid}")
def api_delete_character(cid: str):
    st = store.load_state()
    st["characters"] = [c for c in st["characters"] if c["id"] != cid]
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Wave 3: character pose packs, variants, consistency, from-photo
# --------------------------------------------------------------------------- #
_POSE_SET = ["neutral front turnaround", "happy, smiling warmly",
             "angry, shouting", "sad / worried", "surprised", "calm side profile"]


@app.post("/api/characters/{cid}/pack")
def api_character_pack(cid: str, request: Request, count: int = 4):
    """Generate a pose/expression pack from a character's reference sheet."""
    if not _has_image_key(request):
        raise HTTPException(400, "Connect your image API key in Settings first.")
    st = store.load_state()
    ch = next((c for c in st["characters"] if c["id"] == cid), None)
    if not ch:
        raise HTTPException(404, "no such character")
    try:
        sheet = store.read_image(ch["sheet_url"])
    except Exception as e:
        raise HTTPException(400, f"unreadable sheet: {e}")
    client = get_image_client(request)
    out, errs = [], []
    for pose in _POSE_SET[:max(1, min(6, count))]:
        prompt = (f"Character reference of {ch['name']}: the EXACT same character "
                  f"(identical face, hair, outfit, colours) shown {pose}. Clean plain "
                  f"background. {st.get('master_prompt','')}").strip()
        try:
            img = client.edit(prompt, [sheet], size=config.DEFAULT_SIZE,
                              quality=config.DEFAULT_QUALITY)
            out.append({"label": pose, "url": store.write_image("characters", img)})
        except Exception as e:
            errs.append(str(e))
    if not out:
        raise HTTPException(500, f"pack generation failed: {errs[:1]}")
    ch["poses"] = (ch.get("poses") or []) + out
    store.save_state(st)
    store.log_usage("image", len(out), 0.08 * len(out))
    return {"poses": ch["poses"], "added": len(out), "errors": errs}


class VariantIn(BaseModel):
    note: str = ""


@app.post("/api/characters/{cid}/variant")
def api_character_variant(cid: str, body: VariantIn, request: Request):
    """Create an alternate look of a character (e.g. 'winter outfit') as a new
    character entry, using the original sheet as the identity anchor."""
    if not _has_image_key(request):
        raise HTTPException(400, "Connect your image API key in Settings first.")
    if not body.note.strip():
        raise HTTPException(400, "describe the variant (e.g. 'battle armour')")
    st = store.load_state()
    ch = next((c for c in st["characters"] if c["id"] == cid), None)
    if not ch:
        raise HTTPException(404, "no such character")
    try:
        sheet = store.read_image(ch["sheet_url"])
    except Exception as e:
        raise HTTPException(400, f"unreadable sheet: {e}")
    client = get_image_client(request)
    prompt = (f"Character reference sheet for {ch['name']}: the SAME face and identity, "
              f"but this variant: {body.note.strip()}. Turnaround + expression row. "
              f"{st.get('master_prompt','')}").strip()
    try:
        img = client.edit(prompt, [sheet], size=config.DEFAULT_SIZE,
                          quality=config.DEFAULT_QUALITY)
    except Exception as e:
        raise HTTPException(500, f"variant generation failed: {e}")
    rec = {
        "id": store.new_id("char"),
        "name": f"{ch['name']} — {body.note.strip()[:24]}",
        "description": body.note.strip(),
        "sheet_url": store.write_image("characters", img),
        "prompt": prompt, "source": "variant", "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    store.log_usage("image", 1, 0.08)
    return rec


@app.post("/api/characters/{cid}/check")
def api_character_check(cid: str, request: Request):
    """Claude compares the character's sheet to every story frame they appear in
    and flags drift."""
    if not _has_ai_key(request.state.settings):
        raise HTTPException(400, "Connect an AI key (Claude or OpenAI) in Settings first.")
    st = store.load_state()
    ch = next((c for c in st["characters"] if c["id"] == cid), None)
    if not ch:
        raise HTTPException(404, "no such character")
    name = ch["name"]
    try:
        sheet = pipeline.downsize_for_vision(store.read_image(ch["sheet_url"]))
    except Exception as e:
        raise HTTPException(400, f"unreadable sheet: {e}")
    frames, idxs = [], []
    for s in st["sequence"]:
        if name in (s.get("characters") or []):
            try:
                frames.append(pipeline.downsize_for_vision(store.read_image(s["image_url"])))
                idxs.append(s.get("index"))
            except Exception:
                pass
    if not frames:
        return {"summary": f"{name} doesn't appear in any rendered frame yet.",
                "issues": []}
    try:
        raw = get_claude_client(request).character_consistency(sheet, frames[:15], name)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"consistency check failed: {e}")
    data = _as_analysis_dict(data)
    for it in (data.get("issues") or []):
        fi = int(it.get("frame", 0))
        if 1 <= fi <= len(idxs):
            it["index"] = idxs[fi - 1]
    return data


@app.post("/api/characters/from-photo")
async def api_character_from_photo(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    """Upload a photo/reference and have the image model build a clean, on-style
    character reference sheet from it."""
    if not _has_image_key(request):
        raise HTTPException(400, "Connect your image API key in Settings first.")
    if not name.strip():
        raise HTTPException(400, "name is required")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    st = store.load_state()
    client = get_image_client(request)
    prompt = (f"Turn the reference person in the image into a clean character "
              f"reference sheet for '{name.strip()}': front turnaround plus a row of "
              f"expressions, preserving their identity (face, hair, build). "
              f"{description.strip()}. {st.get('master_prompt','')}").strip()
    try:
        img = client.edit(prompt, [data], size=config.DEFAULT_SIZE,
                          quality=config.DEFAULT_QUALITY)
    except Exception as e:
        raise HTTPException(500, f"sheet generation failed: {e}")
    rec = {
        "id": store.new_id("char"),
        "name": name.strip(),
        "description": description.strip(),
        "sheet_url": store.write_image("characters", img),
        "prompt": prompt, "source": "from-photo", "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    store.log_usage("image", 1, 0.08)
    return rec


# --------------------------------------------------------------------------- #
#  Single-frame generation (the original continuation engine)
# --------------------------------------------------------------------------- #
class GenerateIn(BaseModel):
    prompt: str
    size: Optional[str] = None
    quality: Optional[str] = None
    continue_prev: bool = True
    style_lock: bool = True
    character_ids: Optional[List[str]] = None


def _project_protagonist(st) -> str:
    """The story's main visual subject, used to PROTAGONIST-LOCK every frame so
    a predator/other-animal scene never replaces the hero (the elephant bug).

    Resolution order:
      1. explicit st['protagonist'] (set by autopilot from the analysis)
      2. the FIRST named character sheet (the cast lead — e.g. the baby/calf)
      3. a 'subject' field on the cached analysis / yt_inspiration
    Returns '' when nothing is known (lock is then skipped — safe no-op).
    """
    try:
        p = (st.get("protagonist") or "").strip()
        if p:
            return p
        chars = st.get("characters") or []
        named = [c.get("name", "").strip() for c in chars if c.get("name")]
        if named:
            # All named cast members are valid protagonists; join the first
            # two so e.g. "Calf, Mother" both stay in frame on family beats.
            return ", ".join(named[:2])
        insp = st.get("yt_inspiration") or {}
        return (insp.get("subject") or insp.get("protagonist") or "").strip()
    except Exception:
        return ""


def _render_one(g_prompt, size, quality, continue_prev, style_lock,
                character_ids=None, request: Request = None,
                shot_relation="cut", scene_vo=None, scene_n=None):
    """Shared engine for /api/generate and /api/generate-batch.

    shot_relation: "continue" = micro-cut of the SAME moment as the previous
    frame -> feed that frame as an image ref so the look stays locked. "cut"
    (default) = a new beat -> previous frame is NOT fed, so the model composes a
    fresh shot (prevents every scene collapsing into one repeated composition)."""
    g_prompt = _sanitize_prompt(g_prompt or "")
    st = store.load_state()
    client = get_image_client(request)

    # Style notes from the reference video analysis — prepended to every prompt.
    style_notes = (st.get("style_notes") or "").strip()

    # 1. characters
    if character_ids:
        wanted = set(character_ids)
        matched = [c for c in st["characters"] if c["id"] in wanted]
    else:
        matched = pipeline.match_characters(g_prompt, st["characters"])

    # 2. previous frame
    prev = st["sequence"][-1] if (continue_prev and st["sequence"]) else None

    # 3. style anchors (reference video frames as visual refs)
    style_frames = st["style_frames"] if style_lock else []

    # 4. assemble refs.
    # IMPORTANT (character-bleed bug): the style frames are whole frames lifted
    # from the SOURCE video — they almost always CONTAIN that video's own
    # characters. If we flood the edit with STYLE_REF_COUNT (default 6) of them,
    # the source video's character outnumbers the 1-2 attached character sheets
    # in the contact grid and the model draws the SOURCE video's person instead
    # of our cast — even in scenes that named a character or that wanted no
    # person at all. Two-part defence:
    #   (a) When character sheets ARE attached, hard-cap style frames to a small
    #       number (enough to convey art style, too few to dominate identity),
    #       and put the CHARACTER sheets first so they carry the most visual
    #       weight (top-left of the grid / seen first in multi-image mode).
    #   (b) When NO character sheet is attached (pure-scenery scene), still cap
    #       style frames so a stray person inside them is less likely to be
    #       treated as the subject; the strengthened STYLE-REF caption + prompt
    #       text tell the model to ignore any people shown in style frames.
    _has_chars = bool(matched)
    _style_cap = config.STYLE_FRAMES_WITH_CHARS if _has_chars else config.STYLE_FRAMES_NO_CHARS
    _style_cap = max(0, min(int(_style_cap), int(config.STYLE_REF_COUNT)))

    # Load the capped style-frame images once.
    _style_imgs = []
    for sf in style_frames[:_style_cap]:
        try:
            _style_imgs.append(store.read_image(sf["url"]))
        except Exception:
            pass

    refs, ref_meta = [], []
    # Ordering balances two needs:
    #   - ART STYLE: the model reads style most strongly from the FIRST / dominant
    #     contact-sheet cell, so put ONE style frame at the very front to anchor
    #     the look in the prime position.
    #   - IDENTITY: character sheets must outrank the REMAINING style frames so
    #     the cast (not the source video's people) defines who appears — so the
    #     character sheets go next, then any leftover style frames trail behind.
    if _style_imgs:
        refs.append(_style_imgs[0]); ref_meta.append({"type": "style"})
    for c in matched:
        try:
            refs.append(store.read_image(c["sheet_url"]))
            ref_meta.append({"type": "character", "name": c["name"]})
        except Exception:
            pass
    for img in _style_imgs[1:]:
        refs.append(img); ref_meta.append({"type": "style"})
    # Smart continuity: feed the previous frame as an IMAGE ref ONLY when this
    # shot is a micro-cut of the SAME moment (shot_relation == "continue"). For a
    # real cut/new beat, the previous frame is kept OUT so the model is free to
    # compose a fresh shot — feeding it back makes an edit model clone the prior
    # composition and every scene collapses into the same frame.
    micro_cut = (str(shot_relation or "cut").strip().lower() == "continue"
                 and bool(prev))
    if micro_cut:
        try:
            refs.append(store.read_image(prev["image_url"]))
            ref_meta.append({"type": "previous", "id": prev["id"]})
        except Exception:
            micro_cut = False

    # Hard ceiling: never send more than MAX_REF_IMAGES into one edit call.
    # If we must trim we drop STYLE anchors first (by type) while always keeping
    # the character sheet(s) and (if present) the previous-frame continuity ref —
    # those are load-bearing for identity and continuity, whereas extra style
    # anchors are diminishing returns. (Character sheets now lead the ref list.)
    _max_refs = max(1, int(getattr(config, "MAX_REF_IMAGES", 10)))
    if len(refs) > _max_refs:
        _style_idx = [i for i, m in enumerate(ref_meta) if m.get("type") == "style"]
        _overflow = len(refs) - _max_refs
        _drop = set(_style_idx[-_overflow:]) if _overflow <= len(_style_idx) else set(_style_idx)
        _kept = [i for i in range(len(ref_meta)) if i not in _drop][:_max_refs]
        refs = [refs[i] for i in _kept]
        ref_meta = [ref_meta[i] for i in _kept]
        print(f"[render] trimmed refs to {len(refs)} (cap={_max_refs})", flush=True)

    # Decide ref delivery mode up front: separate images vs one contact sheet.
    _multi = (request.state.settings["multi_image_edit"]
              if request else config.MULTI_IMAGE_EDIT)
    full_prompt = pipeline.build_full_prompt(
        st["master_prompt"], g_prompt, matched,
        has_previous=bool(prev), style_locked=bool(style_frames),
        style_notes=style_notes, micro_cut=micro_cut,
        # In multi-image mode the contact-sheet captions are gone, so describe
        # each attached image in the prompt instead.
        ref_meta=(ref_meta if (_multi and len(refs) > 1) else None),
        protagonist=_project_protagonist(st),
    )

    if refs:
        # Per-ref captions so the model can tell the STYLE anchor from a
        # character sheet from the previous frame in the composited grid.
        def _ref_label(m):
            t = m.get("type")
            if t == "style":
                return "STYLE REF — COPY ART STYLE ONLY; IGNORE ITS CHARACTERS/PEOPLE/COMPOSITION"
            if t == "character":
                return f"CHAR: {(m.get('name') or '').strip()}"[:22]
            if t == "previous":
                return "PREV FRAME (continuity)"
            return ""
        ref_labels = [_ref_label(m) for m in ref_meta]
        # If the proxy isn't confirmed to support repeated `image[]` fields,
        # composite multiple refs into a single LABELED contact-sheet PNG so we
        # hit the documented one-`image`-field path without blending the refs.
        multi_image_edit = _multi
        if not multi_image_edit and len(refs) > 1:
            send = [pipeline.contact_sheet(refs, labels=ref_labels,
                                           target_size=size)]
            mode_note = f"edit (labeled contact-sheet of {len(refs)} refs)"
        else:
            send = refs
            mode_note = f"edit ({len(refs)} refs)"
        print(f"[render] {mode_note} prompt_len={len(full_prompt)}", flush=True)
        try:
            img = client.edit(full_prompt, send, size=size, quality=quality,
                              multi_image_edit=multi_image_edit)
        except Exception as edit_err:
            # Multi-image `image[]` mode isn't supported by every proxy. If we
            # sent more than one ref and it failed, fall back to compositing all
            # refs into ONE contact-sheet PNG (the documented single-`image`
            # path) and retry once before giving up.
            if len(send) > 1:
                print(f"[render] multi-ref edit failed ({edit_err}); "
                      f"retrying as contact-sheet", flush=True)
                img = client.edit(full_prompt,
                                  [pipeline.contact_sheet(refs, labels=ref_labels,
                                                          target_size=size)],
                                  size=size, quality=quality,
                                  multi_image_edit=False)
            else:
                raise
        mode = "edit"
    else:
        print(f"[render] generate (no refs) prompt_len={len(full_prompt)}",
              flush=True)
        img = client.generate(full_prompt, size=size, quality=quality)
        mode = "generate"

    # Append under a lock against a FRESH read so concurrent renderers (e.g. a
    # background character-sheet thread or the image queue) can't clobber each
    # other's sequence or collide on the frame index.
    image_url = store.write_image("images", img)
    with _state_write_lock:
        fresh = store.load_state()
        rec = {
            "id": store.new_id("shot"),
            "index": len(fresh["sequence"]) + 1,
            "prompt": g_prompt.strip(),
            "full_prompt": full_prompt,
            "image_url": image_url,
            "mode": mode,
            "size": size,
            "quality": quality,
            "characters": [c["name"] for c in matched],
            "refs": ref_meta,
            "continued_from": prev["id"] if prev else None,
            # Carry the EXACT narration line this frame was rendered for. A/V sync
            # reads VO from the frame itself, so a failed/missing frame can never
            # shift the VO-to-frame mapping (positional mapping desyncs when a
            # render is dropped mid-run — e.g. on a transient image-API 502).
            "vo": (scene_vo or "").strip(),
            "scene_n": scene_n,
            "created": store.now(),
        }
        fresh["sequence"].append(rec)
        store.save_state(fresh)
    _cost = {"low": 0.02, "medium": 0.04, "high": 0.08, "auto": 0.06}
    store.log_usage("image", 1, _cost.get(quality, 0.06))
    return rec


@app.post("/api/generate")
def api_generate(g: GenerateIn, request: Request):
    if not g.prompt.strip():
        raise HTTPException(400, "prompt is required")
    size = g.size or config.DEFAULT_SIZE
    quality = g.quality or config.DEFAULT_QUALITY
    try:
        return _render_one(g.prompt, size, quality, g.continue_prev, g.style_lock,
                           g.character_ids, request=request)
    except Exception as e:
        raise HTTPException(500, f"generation failed: {e}")


class BatchGenerateIn(BaseModel):
    text: str                          # newline-separated prompts (one per line)
    mode: str = "line"                 # 'line' or 'blank'
    size: Optional[str] = None
    quality: Optional[str] = None
    continue_prev: bool = True
    style_lock: bool = True


@app.post("/api/generate/batch")
def api_generate_batch(b: BatchGenerateIn, request: Request):
    prompts = pipeline.split_lines_batch(b.text, mode=b.mode)
    if not prompts:
        raise HTTPException(400, "no prompts found")
    size = b.size or config.DEFAULT_SIZE
    quality = b.quality or config.DEFAULT_QUALITY
    created, errors = [], []
    for p in prompts:
        try:
            # Each prompt independently auto-matches characters by @tags / names
            rec = _render_one(p, size, quality, b.continue_prev, b.style_lock,
                              character_ids=None, request=request)
            created.append(rec)
        except Exception as ex:
            errors.append({"prompt": p, "error": str(ex)})
    return {"created": created, "errors": errors}


# --------------------------------------------------------------------------- #
#  Image generation QUEUE — controlled, retry-safe bulk generation.
#  Splits prompts into jobs, runs them through image_queue (concurrency=1 by
#  default) with backoff + a global rate-limit cooldown. The frontend submits a
#  batch then polls /status; completed frames are saved as they finish so a
#  later failure never loses earlier work, and any failed job can be retried.
# --------------------------------------------------------------------------- #
import image_queue

# Serialises the quick read-modify-write of a project's sequence so a worker and
# the user editing at the same time can't clobber each other's state.
_state_write_lock = threading.Lock()


def _render_one_for_queue(g_prompt, params, settings, project_id):
    """Render ONE frame for a queued job. Mirrors _render_one's reference
    assembly but operates on an explicit project + settings snapshot and lets
    the queue own retries (so the client call uses retry=False)."""
    size = params.get("size") or config.DEFAULT_SIZE
    quality = params.get("quality") or config.DEFAULT_QUALITY
    continue_prev = params.get("continue_prev", True)
    style_lock = params.get("style_lock", True)
    multi_image_edit = settings.get("multi_image_edit", config.MULTI_IMAGE_EDIT)
    client = _client_from_settings(settings)

    st = store.load_state_for(project_id)
    matched = pipeline.match_characters(g_prompt, st["characters"])
    prev = st["sequence"][-1] if (continue_prev and st["sequence"]) else None
    style_frames = st["style_frames"] if style_lock else []

    # Smart continuity: only feed the previous frame as an IMAGE when this shot
    # is a micro-cut of the same moment (shot_relation == "continue"). For a real
    # cut/new beat we keep it out so the model is free to compose a fresh shot.
    shot_relation = str(params.get("shot_relation") or "cut").strip().lower()
    micro_cut = (shot_relation == "continue") and bool(prev)

    refs, ref_meta = [], []
    # Balanced ordering (see _render_one): ONE style frame leads to anchor the art
    # style in the dominant cell, then character sheets (identity outranks the
    # remaining style frames), then leftover style frames. Cap keeps style strong
    # without letting the source video's people outvote the cast.
    _has_chars = bool(matched)
    _style_cap = config.STYLE_FRAMES_WITH_CHARS if _has_chars else config.STYLE_FRAMES_NO_CHARS
    _style_cap = max(0, min(int(_style_cap), int(config.STYLE_REF_COUNT)))
    _style_imgs = []
    for sf in style_frames[:_style_cap]:
        try:
            _style_imgs.append(store.read_image(sf["url"]))
        except Exception:
            pass
    if _style_imgs:
        refs.append(_style_imgs[0]); ref_meta.append({"type": "style"})
    for c in matched:
        try:
            refs.append(store.read_image(c["sheet_url"]))
            ref_meta.append({"type": "character", "name": c["name"]})
        except Exception:
            pass
    for img in _style_imgs[1:]:
        refs.append(img); ref_meta.append({"type": "style"})
    if micro_cut:
        try:
            refs.append(store.read_image(prev["image_url"]))
            ref_meta.append({"type": "previous", "id": prev["id"]})
        except Exception:
            micro_cut = False

    _queue_style_notes = (st.get("style_notes") or "").strip()
    full_prompt = pipeline.build_full_prompt(
        st["master_prompt"], g_prompt, matched,
        has_previous=bool(prev), style_locked=bool(style_frames),
        style_notes=_queue_style_notes, micro_cut=micro_cut,
        # Multi-image mode: contact-sheet captions are gone, so name each
        # attached image in the prompt so the model keeps style/identity roles.
        ref_meta=(ref_meta if (multi_image_edit and len(refs) > 1) else None),
        protagonist=_project_protagonist(st))

    if refs:
        # Per-ref captions so the model can tell the STYLE anchor from a
        # character sheet from the previous frame in the composited grid —
        # without these the refs blend and the art style copies poorly.
        def _ref_label(m):
            t = m.get("type")
            if t == "style":
                return "STYLE REF — COPY ART STYLE ONLY; IGNORE ITS CHARACTERS/PEOPLE/COMPOSITION"
            if t == "character":
                return f"CHAR: {(m.get('name') or '').strip()}"[:22]
            if t == "previous":
                return "PREV FRAME (continuity)"
            return ""
        ref_labels = [_ref_label(m) for m in ref_meta]
        if not multi_image_edit and len(refs) > 1:
            send = [pipeline.contact_sheet(refs, labels=ref_labels,
                                           target_size=size)]
        else:
            send = refs
        try:
            img = client.edit(full_prompt, send, size=size, quality=quality,
                              retry=False, multi_image_edit=multi_image_edit)
        except Exception:
            if len(send) > 1:
                img = client.edit(
                    full_prompt,
                    [pipeline.contact_sheet(refs, labels=ref_labels,
                                            target_size=size)],
                    size=size, quality=quality, retry=False,
                    multi_image_edit=False)
            else:
                raise
        mode = "edit"
    else:
        img = client.generate(full_prompt, size=size, quality=quality,
                             retry=False)
        mode = "generate"

    # Quick, locked read-modify-write so concurrent writers don't lose frames.
    with _state_write_lock:
        st = store.load_state_for(project_id)
        rec = {
            "id": store.new_id("shot"),
            "index": len(st["sequence"]) + 1,
            "prompt": g_prompt.strip(),
            "full_prompt": full_prompt,
            "image_url": store.write_image("images", img),
            "mode": mode, "size": size, "quality": quality,
            "characters": [c["name"] for c in matched],
            "refs": ref_meta,
            "continued_from": prev["id"] if prev else None,
            # Per-frame VO so A/V sync survives dropped frames (see _render_one).
            "vo": (params.get("scene_vo") or "").strip(),
            "created": store.now(),
        }
        st["sequence"].append(rec)
        store.save_state_for(project_id, st)
    _cost = {"low": 0.02, "medium": 0.04, "high": 0.08, "auto": 0.06}
    store.log_usage("image", 1, _cost.get(quality, 0.06), project_id=project_id)
    return rec


image_queue.QUEUE.set_render_fn(_render_one_for_queue)


@app.on_event("startup")
def _start_image_queue():
    image_queue.QUEUE.start()


def _img_settings_snapshot(request: Request) -> dict:
    s = request.state.settings
    # Resolve against the active image_provider so the queue's render workers
    # (which read these flat keys) use 9Router when it's selected.
    api_key, base_url, model = _resolve_image(s)
    # Queue workers run after the request returns. Keep the provider-specific
    # credential fields too: _client_from_settings() resolves them again.
    # Without ninerouter_api_key, manual batch rendering silently used an empty
    # bearer token even though direct workflow renders worked.
    return {
        "api_key": api_key, "base_url": base_url,
        "model": model, "multi_image_edit": s["multi_image_edit"],
        "image_provider": s.get("image_provider", "derouter"),
        "ninerouter_api_key": s.get("ninerouter_api_key", ""),
        "ninerouter_image_base_url": s.get("ninerouter_image_base_url", ""),
        "ninerouter_image_model": s.get("ninerouter_image_model", ""),
        "tokenbay_api_key": s.get("tokenbay_api_key", ""),
        "tokenbay_base_url": s.get("tokenbay_base_url", ""),
        "tokenbay_model": s.get("tokenbay_model", ""),
        "dashscope_api_key": s.get("dashscope_api_key", ""),
        "dashscope_base_url": s.get("dashscope_base_url", ""),
        "dashscope_model": s.get("dashscope_model", ""),
    }


class QueueSubmitIn(BaseModel):
    text: str
    mode: str = "line"
    size: Optional[str] = None
    quality: Optional[str] = None
    continue_prev: bool = True
    style_lock: bool = True


@app.post("/api/images/queue")
def api_images_queue_submit(b: QueueSubmitIn, request: Request):
    """Enqueue a bulk batch. Returns the batch id + initial job list to poll."""
    if not _has_image_key(request):
        raise HTTPException(400, "Connect your image API key in Settings first.")
    prompts = pipeline.split_lines_batch(b.text, mode=b.mode)
    if not prompts:
        raise HTTPException(400, "no prompts found")
    params = {"size": b.size or config.DEFAULT_SIZE,
              "quality": b.quality or config.DEFAULT_QUALITY,
              "continue_prev": b.continue_prev, "style_lock": b.style_lock}
    batch = image_queue.QUEUE.submit(prompts, params,
                                     _img_settings_snapshot(request),
                                     store.current_project_id())
    return batch.to_dict()


@app.get("/api/images/queue/{bid}")
def api_images_queue_status(bid: str):
    b = image_queue.QUEUE.get_batch(bid)
    if not b:
        raise HTTPException(404, "no such batch")
    return b.to_dict()


@app.post("/api/images/queue/{bid}/cancel")
def api_images_queue_cancel(bid: str):
    if not image_queue.QUEUE.cancel(bid):
        raise HTTPException(404, "no such batch")
    return image_queue.QUEUE.get_batch(bid).to_dict()


@app.post("/api/images/queue/{bid}/retry-failed")
def api_images_queue_retry_failed(bid: str, request: Request):
    n = image_queue.QUEUE.retry_failed(bid, _img_settings_snapshot(request))
    b = image_queue.QUEUE.get_batch(bid)
    if not b:
        raise HTTPException(404, "no such batch")
    return {"requeued": n, "batch": b.to_dict()}


@app.post("/api/images/job/{job_id}/retry")
def api_images_job_retry(job_id: str, request: Request):
    if not image_queue.QUEUE.retry_job(job_id, _img_settings_snapshot(request)):
        raise HTTPException(400, "job not found or not retryable")
    return {"ok": True}


@app.get("/api/images/throttle")
def api_images_throttle():
    return image_queue.throttle_status()


# --------------------------------------------------------------------------- #
#  FLOW BRIDGE — for the Chrome extension that drives Google Flow's UI.
#
#  Instead of calling derouter, the extension pulls a "flow job" (master prompt,
#  style notes, ordered per-frame prompts, plus the STYLE reference image(s) and
#  CHARACTER sheets as data: URLs it can attach in Flow), generates each image in
#  Flow's browser UI (Nano Banana Pro / NB2), and POSTs the produced PNGs back.
#  We then save them into the project sequence exactly like a normal render, so
#  the rest of the app (editor/stitch) is unchanged.
# --------------------------------------------------------------------------- #
def _b64_data_url(img_bytes: bytes, mime="image/png") -> str:
    return f"data:{mime};base64," + _b64.b64encode(img_bytes).decode("ascii")


def _read_image_data_url(url):
    try:
        return _b64_data_url(store.read_image(url))
    except Exception:
        return None


@app.get("/api/flow/job")
def api_flow_job(request: Request, source: str = "sequence"):
    """Everything the extension needs to drive Flow for the CURRENT project.

    source='sequence' (default): use prompts from the already-built sequence
        (each shot's `prompt`). Use this to (re)generate frames via Flow.
    Returns refs as data: URLs so the extension can upload them into Flow.
    """
    st = store.load_state()
    pid = store.current_project_id()

    style_refs = []
    for sf in (st.get("style_frames") or [])[:config.STYLE_REF_COUNT]:
        d = _read_image_data_url(sf.get("url"))
        if d:
            style_refs.append({"id": sf.get("id"), "data_url": d})

    characters = []
    for c in (st.get("characters") or []):
        d = _read_image_data_url(c.get("sheet_url")) if c.get("sheet_url") else None
        characters.append({
            "id": c.get("id"), "name": c.get("name"),
            "description": c.get("description", ""),
            "sheet_data_url": d,
            "has_sheet": bool(d),
        })

    frames = []
    for s in (st.get("sequence") or []):
        frames.append({
            "id": s.get("id"),
            "index": s.get("index"),
            "prompt": s.get("prompt", ""),
            "full_prompt": s.get("full_prompt", ""),
            "characters": s.get("characters", []),
            "has_image": bool(s.get("image_url")),
        })

    # Fallback: if no rendered sequence yet (Flow workflow = build script, don't
    # render in CS), derive frames straight from the script scenes so the
    # extension has prompts to generate. Each scene's `prompt` is the VISUAL
    # description (already alias-normalised when the script was saved).
    source_used = "sequence"
    if not frames:
        scenes = ((st.get("script") or {}).get("scenes") or [])
        for i, sc in enumerate(scenes, 1):
            if not isinstance(sc, dict):
                continue
            vis = (sc.get("prompt") or sc.get("visual") or sc.get("action") or "").strip()
            if not vis:
                continue
            frames.append({
                "id": f"scene_{sc.get('n', i)}",
                "index": sc.get("n", i),
                "prompt": vis,
                "full_prompt": vis,
                "characters": [],
                "has_image": False,
                "from_script": True,
            })
        if frames:
            source_used = "script"

    return {
        "project_id": pid,
        "project_name": (store.list_projects() or {}).get("current_name", ""),
        "master_prompt": st.get("master_prompt", ""),
        "style_notes": st.get("style_notes", ""),
        "protagonist": _project_protagonist(st),
        "style_refs": style_refs,
        "characters": characters,
        "frames": frames,
        "source": source_used,
        "size": config.DEFAULT_SIZE,
    }


class FlowCharacterIn(BaseModel):
    character_id: str
    image: str                          # data: URL or bare base64 PNG


@app.post("/api/flow/character")
def api_flow_save_character(body: FlowCharacterIn):
    """Save a character sheet that the extension generated in Flow."""
    raw = body.image.split(",", 1)[-1] if body.image.startswith("data:") else body.image
    try:
        img = _b64.b64decode(raw)
    except Exception as e:
        raise HTTPException(400, f"bad image base64: {e}")
    with _state_write_lock:
        st = store.load_state()
        found = None
        for c in st.get("characters", []):
            if c.get("id") == body.character_id:
                found = c
                break
        if not found:
            raise HTTPException(404, "no such character")
        found["sheet_url"] = store.write_image("characters", img)
        store.save_state(st)
    return {"ok": True, "character_id": body.character_id,
            "sheet_url": found["sheet_url"]}


class FlowFrameIn(BaseModel):
    image: str                          # data: URL or bare base64 PNG
    prompt: str = ""
    frame_id: Optional[str] = None      # existing shot id to UPDATE; else append
    model: str = "flow"                 # 'nano-banana-pro' | 'nano-banana-2' | 'flow'


@app.post("/api/flow/frame")
def api_flow_save_frame(body: FlowFrameIn):
    """Save a single frame the extension generated in Flow. If frame_id matches
    an existing shot, replace its image (regenerate-in-place); otherwise append
    a new shot to the sequence."""
    raw = body.image.split(",", 1)[-1] if body.image.startswith("data:") else body.image
    try:
        img = _b64.b64decode(raw)
    except Exception as e:
        raise HTTPException(400, f"bad image base64: {e}")
    url = store.write_image("images", img)
    with _state_write_lock:
        st = store.load_state()
        rec = None
        if body.frame_id:
            for s in st["sequence"]:
                if s["id"] == body.frame_id:
                    s["image_url"] = url
                    s["mode"] = f"flow:{body.model}"
                    if body.prompt.strip():
                        s["prompt"] = body.prompt.strip()
                    rec = s
                    break
        if rec is None:
            rec = {
                "id": store.new_id("shot"),
                "index": len(st["sequence"]) + 1,
                "prompt": body.prompt.strip(),
                "full_prompt": body.prompt.strip(),
                "image_url": url,
                "mode": f"flow:{body.model}",
                "size": config.DEFAULT_SIZE, "quality": "flow",
                "characters": [], "refs": [],
                "continued_from": None, "vo": "",
                "created": store.now(),
            }
            st["sequence"].append(rec)
        store.save_state(st)
    store.log_usage("image", 1, 0.0, project_id=store.current_project_id())
    return {"ok": True, "frame": rec}


@app.get("/api/flow/status")
def api_flow_status():
    """Quick liveness probe the extension hits to confirm the bridge is up and
    which project is active."""
    pid = store.current_project_id()
    st = store.load_state()
    return {
        "ok": True, "bridge": "flow", "project_id": pid,
        "frames": len(st.get("sequence") or []),
        "characters": len(st.get("characters") or []),
        "style_refs": len(st.get("style_frames") or []),
    }


@app.delete("/api/sequence/{sid}")
def api_delete_shot(sid: str):
    st = store.load_state()
    st["sequence"] = [s for s in st["sequence"] if s["id"] != sid]
    for i, s in enumerate(st["sequence"], 1):
        s["index"] = i
    store.save_state(st)
    return {"ok": True}


@app.post("/api/reset-sequence")
def api_reset_sequence():
    st = store.load_state()
    st["sequence"] = []
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Wave 2: re-roll / best-of-N variants, reorder, bulk delete
# --------------------------------------------------------------------------- #
class ChooseIn(BaseModel):
    url: str = ""


class IdsIn(BaseModel):
    ids: List[str] = []


def _shot_image(st, prev, g_prompt, size, quality, request):
    """Generate ONE image for a prompt using the same reference assembly as the
    main renderer (matched characters + given previous frame + style anchors).

    Ref ordering and labelling MUST match _render_one: style frames FIRST (so
    the STYLE REF gets the dominant top cell of the contact sheet and is seen
    first in multi-image mode), then character sheets. Every ref carries an
    explicit label so the model can tell the style anchor from a character sheet
    instead of blending them — without this the art style copies poorly and
    shots drift toward a generic look."""
    client = get_image_client(request)
    matched = pipeline.match_characters(g_prompt, st["characters"])
    refs, ref_labels = [], []
    # 1. style anchors FIRST (source-video frames) — the look to reproduce.
    for sf in st.get("style_frames", [])[:4]:
        try:
            refs.append(store.read_image(sf["url"]))
            ref_labels.append("STYLE REF — COPY THIS LOOK")
        except Exception:
            pass
    # 2. character sheets — identity only.
    for c in matched:
        try:
            refs.append(store.read_image(c["sheet_url"]))
            ref_labels.append(f"CHAR: {(c.get('name') or '').strip()}"[:22])
        except Exception:
            pass
    # Previous frame intentionally NOT used as an image ref (see _render_one):
    # avoids composition cloning so each shot is free to vary.
    # (prev still drives has_previous text for style/palette carry-over.)
    _shot_style_notes = (st.get("style_notes") or "").strip()
    full_prompt = pipeline.build_full_prompt(
        st["master_prompt"], g_prompt, matched,
        has_previous=bool(prev), style_locked=bool(st.get("style_frames")),
        style_notes=_shot_style_notes, protagonist=_project_protagonist(st))
    if refs:
        multi = request.state.settings["multi_image_edit"]
        send = refs if (multi and len(refs) > 1) else (
            [pipeline.contact_sheet(refs, labels=ref_labels)]
            if len(refs) > 1 else refs)
        try:
            img = client.edit(full_prompt, send, size=size, quality=quality,
                              multi_image_edit=multi)
        except Exception:
            if len(send) > 1:
                img = client.edit(
                    full_prompt,
                    [pipeline.contact_sheet(refs, labels=ref_labels)],
                    size=size, quality=quality, multi_image_edit=False)
            else:
                img = client.generate(full_prompt, size=size, quality=quality)
    else:
        img = client.generate(full_prompt, size=size, quality=quality)
    return img


@app.post("/api/sequence/{sid}/variants")
def api_shot_variants(sid: str, request: Request, count: int = 1):
    """Generate `count` fresh candidate images for an existing shot's prompt
    (re-roll = 1, best-of-N = many). Candidates are saved but NOT attached until
    the caller picks one via /choose."""
    st = store.load_state()
    pos = next((i for i, s in enumerate(st["sequence"]) if s["id"] == sid), -1)
    if pos < 0:
        raise HTTPException(404, "no such shot")
    shot = st["sequence"][pos]
    prev = st["sequence"][pos - 1] if pos - 1 >= 0 else None
    g_prompt = shot.get("prompt", "")
    size = shot.get("size") or config.DEFAULT_SIZE
    quality = shot.get("quality") or config.DEFAULT_QUALITY
    count = max(1, min(6, count))
    cands, errs = [], []
    for _ in range(count):
        try:
            cands.append(store.write_image("images", _shot_image(
                st, prev, g_prompt, size, quality, request)))
        except Exception as e:
            errs.append(str(e))
    if not cands:
        raise HTTPException(500, f"variant generation failed: {errs[:1]}")
    store.log_usage("image", len(cands), 0.08 * len(cands))
    return {"candidates": cands, "errors": errs}


@app.post("/api/sequence/{sid}/choose")
def api_shot_choose(sid: str, body: ChooseIn):
    """Attach a chosen candidate image to a shot (used by re-roll / best-of-N)."""
    st = store.load_state()
    shot = next((s for s in st["sequence"] if s["id"] == sid), None)
    if not shot:
        raise HTTPException(404, "no such shot")
    if not body.url:
        raise HTTPException(400, "url required")
    shot["image_url"] = body.url
    store.save_state(st)
    return shot


@app.post("/api/sequence/{sid}/upscale")
def api_shot_upscale(sid: str, factor: float = 2.0):
    """Increase a frame's resolution with a high-quality Lanczos resample
    (reliable everywhere; not generative super-resolution)."""
    st = store.load_state()
    shot = next((s for s in st["sequence"] if s["id"] == sid), None)
    if not shot:
        raise HTTPException(404, "no such shot")
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(store.read_image(shot["image_url"]))).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"unreadable frame: {e}")
    f = max(1.2, min(4.0, float(factor)))
    w, h = im.size
    nw, nh = int(w * f), int(h * f)
    cap = 4096
    if max(nw, nh) > cap:
        s = cap / max(nw, nh)
        nw, nh = int(nw * s), int(nh * s)
    im = im.resize((nw, nh), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    shot["image_url"] = store.write_image("images", buf.getvalue())
    shot["size"] = f"{nw}x{nh}"
    store.save_state(st)
    return shot


@app.post("/api/sequence/{sid}/inpaint")
async def api_shot_inpaint(sid: str, request: Request,
                           prompt: str = Form(...),
                           mask: UploadFile = File(...)):
    """Mask-based inpaint: repaint only the masked region of a frame.
    Experimental — depends on your image proxy supporting the `mask` field."""
    if not _has_image_key(request):
        raise HTTPException(400, "Connect your image API key in Settings first.")
    st = store.load_state()
    shot = next((s for s in st["sequence"] if s["id"] == sid), None)
    if not shot:
        raise HTTPException(404, "no such shot")
    try:
        base = store.read_image(shot["image_url"])
    except Exception as e:
        raise HTTPException(400, f"unreadable frame: {e}")
    mdata = await mask.read()
    if not mdata:
        raise HTTPException(400, "empty mask")
    client = get_image_client(request)
    try:
        img = client.edit(prompt, [base], size="auto", quality="auto", mask=mdata)
    except Exception as e:
        raise HTTPException(500, f"inpaint failed (your image model may not "
                                 f"support masks): {e}")
    shot["image_url"] = store.write_image("images", img)
    store.save_state(st)
    store.log_usage("image", 1, 0.06)
    return shot


@app.post("/api/sequence/reorder")
def api_sequence_reorder(body: IdsIn):
    """Reorder the sequence to match the given list of shot ids; renumbers."""
    st = store.load_state()
    rank = {sid: i for i, sid in enumerate(body.ids)}
    st["sequence"].sort(key=lambda s: rank.get(s["id"], 1_000_000))
    for i, s in enumerate(st["sequence"], 1):
        s["index"] = i
    store.save_state(st)
    return {"ok": True, "count": len(st["sequence"])}


@app.post("/api/sequence/delete")
def api_sequence_delete(body: IdsIn):
    """Delete many shots at once; renumbers the rest."""
    st = store.load_state()
    rm = set(body.ids)
    st["sequence"] = [s for s in st["sequence"] if s["id"] not in rm]
    for i, s in enumerate(st["sequence"], 1):
        s["index"] = i
    store.save_state(st)
    return {"ok": True, "count": len(st["sequence"])}


# --------------------------------------------------------------------------- #
#  Inspire from a YouTube video: analyse style/topic/voice -> 10 suggestions
# --------------------------------------------------------------------------- #
class YouTubeAnalyzeIn(BaseModel):
    url: str
    nudge: str = ""
    model: Optional[str] = None


@app.post("/api/youtube/analyze")
def api_youtube_analyze(body: YouTubeAnalyzeIn, request: Request):
    """Paste a YouTube link -> Claude analyses its look, topic and way of
    speaking, then returns 10 ready-to-produce video ideas in the same vein."""
    s = request.state.settings
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    import youtube
    if not youtube.is_youtube_url(body.url):
        raise HTTPException(400, "That doesn't look like a YouTube link.")

    try:
        ref = youtube.ingest(body.url, max_frames=12)
    except Exception as e:
        raise HTTPException(400, f"couldn't read that video: {e}")

    # Downsize frames for the vision call.
    frame_imgs = []
    for u in ref.get("frame_urls", [])[:12]:
        try:
            frame_imgs.append(pipeline.downsize_for_vision(store.read_image(u)))
        except Exception:
            pass

    st = store.load_state()
    try:
        raw = _claude_client_for(body.model, request).suggest_from_reference(
            frames=frame_imgs,
            transcript=ref.get("transcript", ""),
            source_title=ref.get("title", ""),
            source_channel=ref.get("channel", ""),
            nudge=body.nudge,
            master_prompt=st["master_prompt"],
            n_suggestions=10,
        )
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")

    data = _as_analysis_dict(data)
    suggestions = data.get("suggestions") or []
    insp = {
        "url": ref["url"], "video_id": ref["video_id"],
        "title": ref.get("title", ""), "channel": ref.get("channel", ""),
        "style_summary": data.get("style_summary", ""),
        "speech_style": data.get("speech_style", ""),
        "topic": data.get("topic", ""),
        "frames": ref.get("frame_urls", []),
        "source": ref.get("source", ""), "notes": ref.get("notes", ""),
        "suggestions": suggestions,
        "created": store.now(),
    }
    st["yt_inspiration"] = insp
    store.save_state(st)
    return insp


# --------------------------------------------------------------------------- #
#  NICHE BUILDER  — paste multiple channels, get a "mini YouTube" deck of their
#  videos, then have the AI find the niche that works + recommend topics.
# --------------------------------------------------------------------------- #
class NicheScanIn(BaseModel):
    urls: List[str] = []
    per_channel: int = 12


class NicheAnalyseIn(BaseModel):
    urls: List[str] = []
    per_channel: int = 12
    nudge: str = ""
    model: Optional[str] = None


@app.post("/api/niche/scan")
def api_niche_scan(body: NicheScanIn):
    """Fetch recent videos from each channel link and return a flat, views-sorted
    deck for the mini-YouTube grid. No AI cost — pure metadata."""
    import youtube
    urls = [u.strip() for u in (body.urls or []) if u and u.strip()]
    if not urls:
        raise HTTPException(400, "Add at least one channel link.")
    per = max(3, min(int(body.per_channel or 12), 30))
    deck = youtube.niche_scan(urls, per_channel=per)
    if not deck.get("videos"):
        raise HTTPException(400, "Couldn't read any videos from those links. "
                                 "Check they're channel/playlist URLs (not single videos).")
    return deck


@app.post("/api/niche/analyse")
def api_niche_analyse(body: NicheAnalyseIn, request: Request):
    """Analyse the scanned channels: pull titles + a few transcripts, then ask
    the AI to (1) identify the winning niche/format and (2) recommend concrete
    video topics to make next."""
    s = request.state.settings
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    import youtube
    urls = [u.strip() for u in (body.urls or []) if u and u.strip()]
    if not urls:
        raise HTTPException(400, "Add at least one channel link.")
    per = max(3, min(int(body.per_channel or 12), 30))
    deck = youtube.niche_scan(urls, per_channel=per)
    vids = deck.get("videos", [])
    if not vids:
        raise HTTPException(400, "Couldn't read any videos from those links.")

    # Build a compact corpus: every title + view count, plus transcripts of the
    # top 3 performers (cheap, high-signal — captures tone & subject matter).
    lines = []
    for c in deck.get("channels", []):
        lines.append(f"CHANNEL: {c['channel']} ({c['count']} videos)")
    lines.append("")
    lines.append("VIDEOS (sorted by views, highest first):")
    for v in vids[:60]:
        vc = v.get("views") or 0
        lines.append(f"- [{vc:,} views] {v.get('title','')}")

    transcripts = []
    for v in vids[:3]:
        vid = v.get("video_id") or youtube.extract_video_id(v.get("url", ""))
        if not vid:
            continue
        t = youtube.fetch_transcript(vid, max_chars=2500)
        if t:
            transcripts.append(f"--- Transcript excerpt: \"{v.get('title','')}\" ---\n{t}")

    corpus = "\n".join(lines)
    if transcripts:
        corpus += "\n\nTRANSCRIPT SAMPLES:\n" + "\n\n".join(transcripts)
    if body.nudge.strip():
        corpus += f"\n\nUSER FOCUS / CONSTRAINT: {body.nudge.strip()}"

    system = (
        "You are a YouTube niche strategist for an AI faceless-channel studio "
        "(stick-man / animated explainer style). Analyse the supplied channels' "
        "videos and identify what is actually WORKING. Be concrete and data-driven "
        "— reference the view counts and recurring title/format patterns you see. "
        "Respond ONLY with strict JSON, no prose, in exactly this shape:\n"
        "{\n"
        '  "niche": "the single best niche these channels prove out, one phrase",\n'
        '  "why_it_works": "2-3 sentences citing the view patterns / formats",\n'
        '  "format": "the repeatable video format (length, structure, hook style)",\n'
        '  "audience": "who watches this",\n'
        '  "title_patterns": ["pattern 1", "pattern 2", "pattern 3"],\n'
        '  "topics": [\n'
        '    {"title": "ready-to-make video title", "angle": "why it hits", '
        '"est_appeal": "high|medium"},\n'
        "    ... 8 to 12 topic ideas ...\n"
        "  ]\n"
        "}"
    )

    try:
        raw = _claude_client_for(body.model, request).chat_text(
            corpus, system=system, max_tokens=3000)
        data = extract_json(raw) or {}
    except Exception as e:
        raise HTTPException(500, f"niche analysis failed: {e}")

    if not isinstance(data, dict):
        data = {}
    data.setdefault("niche", "")
    data.setdefault("topics", [])
    # Persist the most recent niche report on the project so it survives reloads.
    try:
        st = store.load_state()
        st["niche_report"] = {**data, "channels": deck.get("channels", []),
                              "created": store.now()}
        store.save_state(st)
    except Exception:
        pass
    return {"report": data, "channels": deck.get("channels", []),
            "video_count": len(vids)}


class YouTubeMultiIn(BaseModel):
    urls: List[str] = []
    nudge: str = ""
    model: Optional[str] = None


@app.post("/api/youtube/analyze-multi")
def api_youtube_analyze_multi(body: YouTubeMultiIn, request: Request):
    """YT Analyser — paste UNLIMITED YouTube links. We ingest each (frames +
    transcript), pool them, and Claude deconstructs the shared ART STYLE,
    PACING, WAY OF SPEAKING and STORYTELLING, then pitches 10 video ideas in
    that combined vein. Picking one auto-loads the Script Generator."""
    s = request.state.settings
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    import youtube

    # Validate + dedupe by video id, preserving paste order.
    seen, urls = set(), []
    for raw_u in (body.urls or []):
        u = (raw_u or "").strip()
        if not u:
            continue
        vid = youtube.extract_video_id(u)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        urls.append(u)
    if not urls:
        raise HTTPException(400, "Paste at least one valid YouTube link.")
    return _analyze_urls(urls, body.nudge, body.model, request)


# Canonical suggestion fields the UI renders -> aliases the model SOMETIMES
# emits instead (opus-4-8 via agentrouter occasionally renames fields and drops
# virality_score). We map them back so cards always show a score + details.
_SUGGESTION_ALIASES = {
    "title": ["title", "name", "headline", "video_title"],
    "logline": ["logline", "concept", "summary", "description", "premise",
                "idea", "synopsis", "pitch"],
    "hook": ["hook", "hook_line", "opening", "opener", "cold_open", "first_line",
             "narration_beat"],
    "distinct_angle": ["distinct_angle", "angle", "fresh_angle", "new_angle",
                       "twist", "differentiator", "art_direction"],
    "virality_reason": ["virality_reason", "why", "reason", "why_it_works",
                        "why_viral", "why_it_pops", "pacing_note"],
    "image_prompt_style": ["image_prompt_style", "art_direction", "visual_style",
                           "art_style", "look", "style_spec", "style_notes",
                           "style", "art"],
    "voiceover_style": ["voiceover_style", "narration_beat", "narration",
                        "vo_style", "voice_over", "narration_style"],
}


def _coerce_score(v):
    """Best-effort 1-100 int from whatever the model put in virality_score."""
    try:
        return max(1, min(100, int(round(float(v)))))
    except (TypeError, ValueError):
        return None


def _normalize_suggestions(sugs):
    """Map alias field names onto the canonical keys the UI renders and coerce
    virality_score to an int. Non-dict entries are dropped."""
    out = []
    for s in (sugs or []):
        if not isinstance(s, dict):
            continue
        n = dict(s)
        for canon, alts in _SUGGESTION_ALIASES.items():
            if not n.get(canon):
                for a in alts:
                    if s.get(a):
                        n[canon] = s[a]
                        break
        score = _coerce_score(n.get("virality_score"))
        if score is not None:
            n["virality_score"] = score
        else:
            n.pop("virality_score", None)
        # Reconcile the cast count with the actual characters array. The model
        # routinely fills one but not the other, which is what made a card read
        # "0 cast" while the build (auto-cast) invented several. Keep the two in
        # sync: an enumerated cast is the source of truth for the count; a stated
        # count with no array is preserved; neither -> None ("auto"), never a
        # misleading 0, because the builder auto-casts when the pick has no cast.
        chars = n.get("characters")
        if not isinstance(chars, list):
            chars = []
            n["characters"] = chars
        nc = n.get("num_characters")
        try:
            nc = int(nc)
        except (TypeError, ValueError):
            nc = None
        if chars:
            n["num_characters"] = len(chars)
        elif nc is not None:
            n["num_characters"] = max(0, nc)
        else:
            n["num_characters"] = None
        out.append(n)
    return out


def _suggestions_need_fix(sugs):
    """True if the batch is missing what the cards must show — a numeric
    virality_score or a logline — for at least half the entries."""
    if not sugs:
        return True
    scored = sum(1 for s in sugs if isinstance(s.get("virality_score"), int))
    lined = sum(1 for s in sugs if (s.get("logline") or "").strip())
    half = max(1, len(sugs) // 2)
    return scored < half or lined < half


def _ensure_virality(sugs):
    """Last-resort floor so the UI never shows an unrated card: keep real scores;
    synthesize a believable descending score from rank (model returns most-viral
    first) and a generic reason for any that are still missing."""
    for i, s in enumerate(sugs):
        if not isinstance(s.get("virality_score"), int):
            s["virality_score"] = max(45, 92 - i * 4)
            s["_score_estimated"] = True
        if not (s.get("virality_reason") or "").strip():
            s["virality_reason"] = ("Strong hook and a clear, repeatable format "
                                    "true to this style.")
    return sugs


def _fallback_suggestions(sources, n_suggestions):
    """Build a minimal but VALID suggestions list when the LLM never returned a
    usable one. Instead of repeating the source title N times, generate varied
    angles derived from the source material so the user gets distinct options.
    Flags each as a fallback so the UI can show it's estimated."""
    titles = [(s.get("title") or "").strip()
              for s in (sources or []) if (s.get("title") or "").strip()]
    transcripts = [(s.get("transcript") or "").strip()
                   for s in (sources or []) if (s.get("transcript") or "").strip()]

    # Build a pool of distinct topic angles from the source material.
    base_title = titles[0] if titles else ""
    # Extract keywords from transcripts to vary the angles
    keywords = []
    for t in transcripts:
        words = [w.strip(".,!?\"'()[]").lower() for w in t.split()]
        keywords.extend([w for w in words if len(w) > 4 and w.isalpha()])
    # Deduplicate, keep order
    seen = set()
    unique_kw = [w for w in keywords if w not in seen and not seen.add(w)]

    angle_templates = [
        ("The hidden side of {kw}", "A deeper dive into {kw} that the reference only hinted at — fresh context and surprising angles."),
        ("What everyone gets wrong about {kw}", "A myth-busting take on {kw}, challenging the conventional wisdom."),
        ("{kw} explained in 60 seconds", "A fast-paced, high-energy breakdown of {kw} for short-form."),
        ("The untold story behind {kw}", "A narrative-driven exploration of {kw} with a fresh perspective."),
        ("Why {kw} matters more than you think", "A compelling case for why {kw} is underappreciated, with real stakes."),
        ("{kw}: then vs now", "A before-and-after comparison showing how {kw} has evolved."),
        ("The dark side of {kw}", "A provocative look at the risks and downsides of {kw}."),
        ("{kw} but from a different angle", "A completely fresh perspective on {kw} that flips the usual framing."),
        ("5 things about {kw} that blew my mind", "A listicle-style rapid-fire of surprising facts about {kw}."),
        ("What if {kw} is just the beginning?", "A forward-looking take on where {kw} is heading next."),
    ]

    out = []
    for i in range(max(1, int(n_suggestions or 1))):
        kw = unique_kw[i] if i < len(unique_kw) else (base_title[:30] or "this topic")
        template_title, template_logline = angle_templates[i % len(angle_templates)]
        title = template_title.format(kw=kw)[:120]
        logline = template_logline.format(kw=kw)
        out.append({
            "title": title,
            "logline": logline,
            "virality_reason": ("Auto-generated fallback — the AI analysis didn't "
                                 "return clean ideas. Try again or use a different "
                                 "reference for real virality scoring."),
            "virality_score": 50,
            "total_duration": 60,
            "_fallback": True,
        })
    return out


# Canonical scene fields -> aliases the model sometimes emits instead. The big
# one: the VISUAL image description lands under "visual" (or art/imagery/…)
# instead of "prompt", so frames were being rendered from the narration text and
# came out off-topic. Mapping it back is critical.
_SCENE_ALIASES = {
    "n": ["n", "scene", "index", "number", "no", "shot", "id"],
    "heading": ["heading", "slug", "label", "header", "scene_title"],
    "action": ["action", "description", "desc", "beat"],
    "vo": ["vo", "voiceover", "narration", "line", "vo_line", "voice_over",
           "narration_line", "script"],
    "prompt": ["prompt", "visual", "image_prompt", "image", "visual_description",
               "visual_desc", "scene_visual", "imagery", "image_desc", "art",
               "art_direction", "visuals", "picture"],
    "shot_relation": ["shot_relation", "relation", "cut_type", "shot_type",
                      "continuity", "transition"],
}


def _normalize_scenes(scenes):
    """Map alias scene-field names onto the canonical keys the renderer uses, so
    the model's real VISUAL description drives image generation (not the
    narration). Also folds an on-screen 'caption' into the image prompt so the
    bold explainer-style caption text actually gets drawn on the frame."""
    out = []
    for i, s in enumerate(scenes or [], 1):
        if not isinstance(s, dict):
            continue
        n = dict(s)
        for canon, alts in _SCENE_ALIASES.items():
            cur = n.get(canon)
            if isinstance(cur, str) and cur.strip():
                continue
            if cur not in (None, "", [], {}):
                continue
            for a in alts:
                v = s.get(a)
                if (isinstance(v, str) and v.strip()) or (v not in (None, "", [], {}) and not isinstance(v, str)):
                    n[canon] = v
                    break
        if not n.get("n"):
            n["n"] = i
        # Bake the on-screen caption (e.g. "IT KNOWS") into the visual prompt so
        # it's drawn as bold caption text, matching the explainer style.
        cap = (s.get("caption") or s.get("on_screen_text") or s.get("text") or "").strip()
        p = (n.get("prompt") or "").strip()
        if cap and p and cap.lower() not in p.lower() and len(cap) <= 60:
            n["prompt"] = p.rstrip(". ") + f'. Big bold on-screen caption text reading "{cap}".'
        # Normalize shot_relation: only "continue" or "cut". Default "cut".
        # The first scene can never "continue" (there is nothing before it).
        _rel = str(n.get("shot_relation") or "").strip().lower()
        if _rel.startswith("cont"):      # continue / continuation / continuous
            _rel = "continue"
        else:
            _rel = "cut"
        if i == 1:
            _rel = "cut"
        n["shot_relation"] = _rel
        out.append(n)
    # ── DEDUP: drop consecutive duplicate scenes ─────────────────────────────
    # Two failure modes produced "same voice line twice + scene repeated like a
    # variation": (a) the LLM script emits two back-to-back scenes with the SAME
    # narration (and near-identical prompt); (b) the frame-step padding loop
    # clones the last scene's prompt to hit the expected count. Either way the
    # video spoke the line twice and showed a near-duplicate frame.
    # Collapse a scene that has the SAME (vo, prompt) as the immediately
    # preceding kept scene. An empty-VO padded frame is kept (it adds no spoken
    # line) UNLESS its prompt also matches — then it's a pure visual repeat.
    def _norm_txt(x):
        return " ".join((x or "").lower().split())
    # Punctuation-insensitive key: "He won." and "He won" must count as the SAME
    # spoken line. Strips everything except word chars + spaces so trailing
    # periods, commas, em-dashes etc. don't let a repeat slip through.
    import re as _re
    def _vo_key(x):
        return _re.sub(r"[^\w ]+", "", _norm_txt(x)).strip()
    # Collapse an immediately-doubled word or phrase INSIDE a single VO line
    # ("the the empire" -> "the empire"; "he won he won" -> "he won"). The TTS
    # would otherwise literally speak the repeat twice. A small allowlist of
    # intentional onomatopoeia ("boom boom", "tick tick", "knock knock") is kept.
    _SOUND_WORDS = {"boom", "tick", "knock", "ha", "ho", "no", "go", "bang",
                    "beep", "tap", "drip", "ring", "chop", "puff", "woah", "wow"}
    def _collapse_inline(txt):
        if not txt:
            return txt
        # Iterate to a fixed point so triples/quadruples ("he won he won he won")
        # fully collapse, not just one pair per pass.
        for _ in range(4):
            before = txt
            # immediate duplicate phrase (2-6 words), tolerant of punctuation
            # between the two copies ("Tom won. Tom won" -> "Tom won.").
            txt = _re.sub(r"\b((?:\w+[\s]+){1,5}\w+)[\s\.,;:!?\-—]+\1\b",
                          r"\1", txt, flags=_re.IGNORECASE)
            # immediate duplicate single word, EXCEPT allowlisted sound-words.
            txt = _re.sub(r"\b(\w+)(\s+)\1\b",
                          lambda m: m.group(1) if m.group(1).lower() not in _SOUND_WORDS
                          else m.group(0),
                          txt, flags=_re.IGNORECASE)
            if txt == before:
                break
        return " ".join(txt.split())
    deduped = []
    _seen_vo = set()    # GLOBAL — catches repeats anywhere, not just adjacent
    for s in out:
        # First scrub inline doubling on the spoken line itself.
        for _vf in ("vo", "narration", "voice_over"):
            if s.get(_vf):
                s[_vf] = _collapse_inline(s[_vf])
        vo_raw = s.get("vo") or s.get("narration") or s.get("voice_over")
        vo_k = _vo_key(vo_raw)
        pr_k = _norm_txt(s.get("prompt"))
        if deduped:
            prev = deduped[-1]
            pvo = _vo_key(prev.get("vo") or prev.get("narration") or prev.get("voice_over"))
            ppr = _norm_txt(prev.get("prompt"))
            # Consecutive duplicate: spoken line repeats, OR pure clone (VO+prompt
            # both identical, incl. a padded empty-VO frame).
            if (vo_k and vo_k == pvo) or (vo_k == pvo and pr_k == ppr and pr_k):
                continue
        # NON-consecutive duplicate spoken line: the same sentence said again two
        # or more scenes later ("repeats same words twice" even when not adjacent).
        # Drop the repeat but KEEP its frame as a silent beat so the visual count /
        # pacing don't collapse — only the doubled narration is removed.
        if vo_k and vo_k in _seen_vo:
            for _vf in ("vo", "narration", "voice_over"):
                if _vf in s:
                    s[_vf] = ""
            vo_k = ""
        if vo_k:
            _seen_vo.add(vo_k)
        deduped.append(s)
    # Renumber so scene `n` stays contiguous after drops (resume matches by n).
    for i, s in enumerate(deduped, 1):
        s["n"] = i
    return deduped


def _deep_analyze_urls(urls, nudge, model, request, n_suggestions=10):
    """Shared core used by BOTH the YT Analyser tab and the Autopilot workflow.
    Ingest a list of YouTube urls, pool frames + transcripts, and have Claude
    deconstruct the shared style/pacing/voice/story along 4 axes + pitch
    ``n_suggestions`` virality-ranked ideas. Returns
    (data_dict, src_meta, errors, pooled_frame_urls). Raises HTTPException on
    no readable videos / analysis failure."""
    import youtube
    urls = list(urls)[:10]   # bound the job — Anthropic caps a request near 20 images
    # Pull MORE frames per video so the style read + anchor selection is richer.
    # Single video -> up to 12 frames; many videos -> fewer each to stay under
    # the vision-model image cap. The pooled set feeds both Claude's style
    # deconstruction AND the pinned style anchors.
    per_video = max(4, min(12, 18 // max(1, len(urls))))
    sources, frame_imgs, src_meta, errors, pooled_frames = [], [], [], [], []
    for u in urls:
        try:
            ref = youtube.ingest(u, max_frames=per_video)
        except Exception as e:
            errors.append({"url": u, "error": str(e)})
            continue
        imgs = []
        for fu in ref.get("frame_urls", [])[:per_video]:
            try:
                imgs.append(pipeline.downsize_for_vision(store.read_image(fu)))
            except Exception:
                pass
        sources.append({
            "title": ref.get("title", ""), "channel": ref.get("channel", ""),
            "transcript": ref.get("transcript", ""),
        })
        src_meta.append({
            "url": ref["url"], "video_id": ref["video_id"],
            "title": ref.get("title", ""), "channel": ref.get("channel", ""),
            "frames": ref.get("frame_urls", []), "source": ref.get("source", ""),
            "notes": ref.get("notes", ""),
        })
        frame_imgs.extend(imgs)
        pooled_frames.extend(ref.get("frame_urls", []))

    if not sources:
        raise HTTPException(400, "Couldn't read any of those videos. Try links "
                                 "with captions or that aren't region-locked.")
    frame_imgs = frame_imgs[:18]

    st = store.load_state()
    client = _claude_client_for(model, request)

    def _ask(extra_nudge=""):
        raw = client.suggest_from_references(
            frames=frame_imgs, sources=sources,
            nudge=(nudge + extra_nudge).strip(),
            master_prompt=st["master_prompt"], n_suggestions=n_suggestions)
        return _as_analysis_dict(extract_json(raw))

    data, sugs = {}, []
    analysis_warning = ""
    try:
        data = _ask()
        sugs = _normalize_suggestions(data.get("suggestions"))
        if _suggestions_need_fix(sugs):
            # The model ignored the schema (renamed fields / dropped scores).
            # Re-ask ONCE — frames already in hand, so no re-ingest — hammering
            # the EXACT field names and the required integer virality_score.
            data2 = _ask(
                "\n\nCRITICAL OUTPUT RULE: every item in \"suggestions\" MUST use "
                "these EXACT key names and NEVER rename them — title, logline, "
                "hook, distinct_angle, virality_score (an INTEGER 1-100), "
                "virality_reason, voiceover_style, image_prompt_style, "
                "pacing_seconds, total_duration, scene_count. Include "
                "virality_score and virality_reason for EVERY suggestion. Sort "
                "most-viral first.")
            sugs2 = _normalize_suggestions(data2.get("suggestions"))
            if not _suggestions_need_fix(sugs2) or len(sugs2) > len(sugs):
                data, sugs = data2, sugs2
        if _suggestions_need_fix(sugs):
            # Third attempt with a SIMPLER schema — drop the rich analysis fields
            # but KEEP cast: title, logline, virality_score AND the characters
            # array. The cards show "N cast" and the script step needs the cast,
            # so stripping characters here is what produced the "0 cast" topics.
            data3 = _ask(
                "\n\nSIMPLIFIED OUTPUT: return \"suggestions\" as an array of "
                "objects with ONLY these keys: title (string), logline (one "
                "sentence), virality_score (INTEGER 1-100), num_characters "
                "(INTEGER 0-8) and characters (array of {name, sheet_prompt}). "
                "If the video is pure narration with no recurring on-screen "
                "character, num_characters is 0 and characters is []. Otherwise "
                "include every recurring character. Sort most-viral first. "
                "JSON only.")
            sugs3 = _normalize_suggestions(data3.get("suggestions"))
            if not _suggestions_need_fix(sugs3) or len(sugs3) > len(sugs):
                # Keep the richer analysis axes from the best earlier attempt
                # but take the simpler-schema suggestions. Back-fill cast from
                # the matching earlier suggestion if this attempt dropped it, so
                # a model that ignored the characters key doesn't cost the cast.
                for i, s3 in enumerate(sugs3):
                    if not (s3.get("characters") or []):
                        _prev = sugs[i] if i < len(sugs) else {}
                        if _prev.get("characters"):
                            s3["characters"] = _prev["characters"]
                            s3.setdefault("num_characters",
                                          _prev.get("num_characters")
                                          or len(_prev["characters"]))
                data = data or data3
                sugs = sugs3
    except HTTPException:
        raise
    except Exception as e:
        # Don't hard-500 — degrade to a minimal valid response with a warning so
        # the UI still renders something actionable. Detect rate-limit / quota
        # errors specifically so the user knows to switch model or wait.
        _emsg = str(e)
        _low = _emsg.lower()
        if "429" in _emsg or "rate limit" in _low or "usage limit" in _low or "quota" in _low:
            import re as _re
            _reset = _re.search(r"reset after ([\dhms\s]+)", _emsg)
            _model_m = _re.search(r"\[([\w/.\-]+)\]", _emsg)
            _mdl = _model_m.group(1) if _model_m else "the selected model"
            analysis_warning = (
                f"⚠️ Rate-limited: {_mdl} hit its usage limit"
                + (f" (resets after {_reset.group(1).strip()})" if _reset else "")
                + ". Switch to a lighter model (e.g. cc/claude-sonnet-4-6) in "
                  "Settings, or wait and retry. These are placeholder ideas.")
        else:
            analysis_warning = f"analysis degraded: {e}"
        data = data if isinstance(data, dict) else {}
        sugs = sugs or []

    if not sugs:
        # All attempts failed to yield usable suggestions — synthesize a minimal
        # valid list from the source titles so the caller never gets a 500.
        analysis_warning = analysis_warning or (
            "Couldn't extract clean idea suggestions from these videos — showing "
            "minimal fallback ideas. Try different reference links.")
        sugs = _fallback_suggestions(sources, n_suggestions)

    data["suggestions"] = _ensure_virality(sugs)
    if analysis_warning:
        data["warning"] = analysis_warning

    return data, src_meta, errors, pooled_frames


def _analyze_urls(urls, nudge, model, request):
    """YT Analyser tab: deep multi-video analysis stored as yt_analysis."""
    data, src_meta, errors, _frames = _deep_analyze_urls(
        urls, nudge, model, request, n_suggestions=10)
    out = {
        "sources": src_meta, "errors": errors,
        "art_style": data.get("art_style", ""),
        "pacing": data.get("pacing", ""),
        "speech_style": data.get("speech_style", ""),
        "storytelling": data.get("storytelling", ""),
        "sources_summary": data.get("sources_summary", ""),
        "suggestions": data.get("suggestions") or [],
        "created": store.now(),
    }
    st = store.load_state()
    st["yt_analysis"] = out
    store.save_state(st)
    return out


class ChannelIn(BaseModel):
    url: str
    nudge: str = ""
    model: Optional[str] = None
    limit: int = 6


@app.post("/api/youtube/channel")
def api_youtube_channel(b: ChannelIn, request: Request):
    """Analyse a whole channel: list its recent videos, then run the same
    multi-video style deconstruction + 10 ideas."""
    if not _has_ai_key(request.state.settings):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    import youtube
    info = youtube.channel_info(b.url, max(2, min(12, b.limit)))
    urls = [it["url"] for it in info.get("items", []) if it.get("url")]
    if not urls:
        raise HTTPException(400, "Couldn't read that channel. Paste a channel URL "
                                 "(e.g. youtube.com/@handle).")
    return _analyze_urls(urls, b.nudge, b.model, request)


class GapsIn(BaseModel):
    url: str
    model: Optional[str] = None


@app.post("/api/youtube/gaps")
def api_youtube_gaps(b: GapsIn, request: Request):
    """Find content gaps: list a channel's recent titles and ask Claude what it
    hasn't covered."""
    if not _has_ai_key(request.state.settings):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    import youtube
    info = youtube.channel_info(b.url, 50)
    titles = [it.get("title", "") for it in info.get("items", []) if it.get("title")]
    if not titles:
        raise HTTPException(400, "Couldn't read that channel's videos.")
    try:
        raw = _claude_client_for(b.model, request).gaps(info.get("channel", ""), titles, 12)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"gap analysis failed: {e}")
    return {"channel": info.get("channel", ""), "gaps": data.get("gaps") or []}


class TrendsIn(BaseModel):
    niche: str
    model: Optional[str] = None


@app.post("/api/youtube/trends")
def api_youtube_trends(b: TrendsIn, request: Request):
    """Pull what's working in a niche via YouTube search, then distill angles."""
    if not _has_ai_key(request.state.settings):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    if not b.niche.strip():
        raise HTTPException(400, "Enter a niche or topic.")
    import youtube
    vids = youtube.search_videos(b.niche.strip(), 14)
    titles = [v.get("title", "") for v in vids if v.get("title")]
    if not titles:
        raise HTTPException(400, "No search results — try a broader niche.")
    try:
        raw = _claude_client_for(b.model, request).trend_angles(b.niche.strip(), titles, 10)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"trend analysis failed: {e}")
    return {"niche": b.niche.strip(), "samples": vids[:10], "angles": data.get("angles") or []}


class SeoIn(BaseModel):
    title: str = ""
    description: str = ""
    model: Optional[str] = None


@app.post("/api/seo")
def api_seo(b: SeoIn, request: Request):
    """Generate optimized titles, description and tags for the current topic."""
    if not _has_ai_key(request.state.settings):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    st = store.load_state()
    sc = st.get("script") or {}
    title = b.title.strip() or sc.get("title", "")
    desc = b.description.strip() or sc.get("logline", "") or (sc.get("voiceover", "") or "")[:400]
    if not (title or desc):
        raise HTTPException(400, "Generate a script or enter a topic first.")
    try:
        raw = _claude_client_for(b.model, request).seo(title, desc, 6)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"SEO generation failed: {e}")
    return {"titles": data.get("titles") or [], "description": data.get("description", ""),
            "tags": data.get("tags") or []}


class YTThumbnailIn(BaseModel):
    title: str = ""
    style: str = ""            # art_style read from the analysis
    extra: str = ""            # user tweaks (face, props, colour...)
    ref_urls: List[str] = []   # reference frames to match the look
    size: Optional[str] = None
    quality: Optional[str] = None


@app.post("/api/youtube/thumbnail")
def api_youtube_thumbnail(body: YTThumbnailIn, request: Request):
    """Design a scroll-stopping YouTube thumbnail for a chosen topic, matched to
    the visual style of the analysed reference videos. Uses the image model;
    when reference frames are supplied it edits from them so the palette/mood
    matches the source channel's look."""
    if not _has_image_key(request):
        raise HTTPException(400, "Connect your image API key in Settings first.")
    if not (body.title.strip() or body.style.strip()):
        raise HTTPException(400, "Give the thumbnail a title or a style to work from.")

    client = get_image_client(request)
    bits = []
    if body.title.strip():
        bits.append(f'A bold, scroll-stopping YouTube thumbnail (16:9) for a video '
                    f'titled: "{body.title.strip()}".')
    else:
        bits.append("A bold, scroll-stopping YouTube thumbnail (16:9).")
    if body.style.strip():
        bits.append(f"Match this visual style: {body.style.strip()}")
    if body.extra.strip():
        bits.append(body.extra.strip())
    bits.append("Single clear focal subject, dramatic cinematic lighting, high "
                "contrast, vivid punchy colours, strong sense of depth. Leave clean "
                "negative space on one side for a short title overlay. Ultra-crisp and "
                "professional. No watermark, no logos, no garbled text.")
    prompt = "\n".join(bits)

    size = body.size or "1536x1024"
    quality = body.quality or config.DEFAULT_QUALITY

    refs = []
    for u in (body.ref_urls or [])[:4]:
        try:
            refs.append(store.read_image(u))
        except Exception:
            pass

    try:
        if refs:
            note = ("\n\nUse the reference image(s) ONLY for art style, palette and "
                    "mood — do NOT copy their exact composition or subjects.")
            multi = request.state.settings["multi_image_edit"]
            send = refs if (multi and len(refs) > 1) else (
                [pipeline.contact_sheet(refs)] if len(refs) > 1 else refs)
            try:
                img = client.edit(prompt + note, send, size=size, quality=quality)
            except Exception:
                # fall back to a plain generation if the edit path isn't supported
                img = client.generate(prompt, size=size, quality=quality)
        else:
            img = client.generate(prompt, size=size, quality=quality)
    except Exception as e:
        raise HTTPException(500, f"thumbnail generation failed: {e}")

    url = store.write_image("images", img)
    st = store.load_state()
    rec = {"id": store.new_id("thumb"), "url": url,
           "title": body.title.strip(), "created": store.now()}
    st.setdefault("thumbnails", []).append(rec)
    store.save_state(st)
    store.log_usage("thumbnail", 1, 0.08)
    return rec


def _hex_rgb(c):
    c = (c or "").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (255, 255, 255)


def _overlay_text(img_bytes, headline="", subtitle="", position="bottom",
                  color="#ffffff", scrim=True, brand=None):
    """Bake crisp title text (Pillow) onto a thumbnail — readable stroke, optional
    scrim, plus brand handle/logo if a brand kit is set."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap
    brand = brand or {}
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im, "RGBA")

    def font(sz, bold=True):
        cands = (["C:/Windows/Fonts/arialbd.ttf"] if bold else ["C:/Windows/Fonts/arial.ttf"]) + [
            "C:/Windows/Fonts/arial.ttf", "arialbd.ttf", "arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        for n in cands:
            try:
                return ImageFont.truetype(n, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    # PREMIUM title block: big bold UPPERCASE, tight wrap, heavy stroke + drop
    # shadow for legibility on any background, and an accent colour on the single
    # most impactful keyword so it reads like a pro thumbnail, not an auto-caption.
    tsize = max(48, W // 9)
    tf, sf = font(tsize, True), font(max(22, W // 26), False)

    def dims(line, f):
        b = d.textbbox((0, 0), line, font=f)
        return (b[2] - b[0], b[3] - b[1])

    _head = (headline or "").strip().upper()
    chars = max(8, int(W * 0.90 / (tsize * 0.56)))
    lines = textwrap.wrap(_head, width=chars) if _head else []
    line_h = dims("Ag", tf)[1] + int(tsize * 0.26)
    block_h = line_h * len(lines) + (dims("Ag", sf)[1] + 18 if subtitle else 0)
    pad = int(H * 0.055)
    if position == "top":
        y0 = pad
    elif position == "center":
        y0 = max(pad, (H - block_h) // 2)
    else:
        y0 = H - block_h - pad

    if scrim and (lines or subtitle):
        # Stronger, taller gradient scrim for solid contrast under big text.
        band = block_h + pad * 3
        grad = Image.new("L", (1, band), 0)
        for i in range(band):
            frac = i / max(1, band)
            grad.putpixel((0, i), int(235 * (1 - frac if position == "top" else frac)))
        grad = grad.resize((W, band))
        top = 0 if position == "top" else max(0, y0 - pad * 2)
        black = Image.new("RGBA", (W, band), (0, 0, 0, 255))
        black.putalpha(grad)
        im.paste(black, (0, top), black)
        d = ImageDraw.Draw(im, "RGBA")

    fill = _hex_rgb(color)
    accent = _hex_rgb(brand.get("accent") or "#ffd23f")  # punchy gold accent
    stroke = max(3, tsize // 11)

    # Pick ONE keyword to accent (longest word — usually the emotional hook).
    _all_words = [w for ln in lines for w in ln.split()]
    _accent_word = max(_all_words, key=len).strip(",.!?:;") if _all_words else ""

    def _draw_line_centered(ln, y):
        # Word-by-word so we can accent-colour one keyword, with drop shadow.
        words = ln.split()
        widths = [dims(w + " ", tf)[0] for w in words]
        total = sum(widths) - (dims(" ", tf)[0] if words else 0)
        x = (W - total) // 2
        for w, ww in zip(words, widths):
            col = (accent if (_accent_word and
                   w.strip(",.!?:;").upper() == _accent_word.upper())
                   else fill)
            # drop shadow
            d.text((x + max(3, stroke // 2), y + max(3, stroke // 2)), w,
                   font=tf, fill=(0, 0, 0, 180))
            d.text((x, y), w, font=tf, fill=col + (255,),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            x += ww

    y = y0
    for ln in lines:
        _draw_line_centered(ln, y)
        y += line_h
    if subtitle:
        y += 10
        w, _h = dims(subtitle, sf)
        d.text(((W - w) // 2, y), subtitle, font=sf, fill=accent + (255,),
               stroke_width=2, stroke_fill=(0, 0, 0, 255))

    handle = (brand.get("handle") or "").strip()
    if handle:
        hf = font(max(16, W // 44), True)
        w, h = dims(handle, hf)
        d.text((W - w - int(W * 0.03), H - h - int(H * 0.05)), handle, font=hf,
               fill=accent + (255,), stroke_width=2, stroke_fill=(0, 0, 0, 255))
    logo = brand.get("logo_url")
    if logo:
        try:
            lg = Image.open(io.BytesIO(store.read_image(logo))).convert("RGBA")
            lw = int(W * 0.12)
            lg = lg.resize((lw, int(lw * lg.height / max(1, lg.width))))
            im.paste(lg, (int(W * 0.03), int(H * 0.04)), lg)
        except Exception:
            pass

    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


class OverlayIn(BaseModel):
    url: str
    headline: str = ""
    subtitle: str = ""
    position: str = "bottom"
    color: str = "#ffffff"
    scrim: bool = True


@app.post("/api/thumbnail/overlay")
def api_thumbnail_overlay(b: OverlayIn):
    try:
        img = store.read_image(b.url)
    except Exception as e:
        raise HTTPException(400, f"unreadable image: {e}")
    st = store.load_state()
    try:
        out = _overlay_text(img, b.headline, b.subtitle, b.position, b.color,
                            b.scrim, st.get("brand"))
    except Exception as e:
        raise HTTPException(500, f"overlay failed: {e}")
    url = store.write_image("images", out)
    rec = {"id": store.new_id("thumb"), "url": url,
           "title": b.headline or "thumbnail", "created": store.now()}
    st.setdefault("thumbnails", []).append(rec)
    store.save_state(st)
    return rec


@app.post("/api/brand")
async def api_brand(accent: str = Form(""), handle: str = Form(""),
                    file: Optional[UploadFile] = File(None)):
    st = store.load_state()
    brand = st.get("brand") or {}
    if accent:
        brand["accent"] = accent
    brand["handle"] = (handle or "").strip()
    if file is not None:
        data = await file.read()
        if data:
            ext = (os.path.splitext(file.filename or "")[1] or ".png").lstrip(".").lower()
            if ext not in {"png", "jpg", "jpeg", "webp"}:
                ext = "png"
            brand["logo_url"] = store.write_image("characters", data, ext=ext)
    st["brand"] = brand
    store.save_state(st)
    return brand


# --------------------------------------------------------------------------- #
#  Claude: script generation
# --------------------------------------------------------------------------- #
class ScriptIn(BaseModel):
    title: str = ""
    description: str = ""
    total_duration: float = 60.0
    pacing_seconds: float = 1.0
    num_characters: int = -1
    style_notes: str = ""
    model: Optional[str] = None
    # Back-compat with the old simple form.
    brief: str = ""
    scene_count: Optional[int] = None
    dialogue: bool = False  # produce speaker-tagged VO lines for multi-voice


def _claude_client_for(model: Optional[str], request: Request) -> ClaudeClient:
    """AI client (per-user keys from the vault) honouring a model override.
    Routes to Anthropic direct, the 9Router proxy, or the derouter proxy based
    on claude_provider."""
    api_key, base_url, mdl = _resolve_claude(request.state.settings, model)
    return ClaudeClient(api_key=api_key, base_url=base_url, model=mdl)


def _as_analysis_dict(data):
    """Normalize an analyzer's parsed JSON to a dict. The model usually returns
    an object, but sometimes a bare list of suggestions — wrap that so callers
    can safely use .get(). Anything else becomes an empty dict."""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {"suggestions": data}
    return {}


def _looks_like_script(data) -> bool:
    """A valid script is an object carrying a non-empty `scenes` list. Claude
    occasionally returns the wrong shape (e.g. just the characters array, or a
    bare list) — we treat those as failures so the caller can retry."""
    return (isinstance(data, dict)
            and isinstance(data.get("scenes"), list)
            and len(data["scenes"]) > 0)


def _generate_script_validated(client, **kwargs):
    """generate_script + extract_json + shape validation, with ONE corrective
    retry if the model returns malformed/mis-shaped JSON (no scenes). Raises
    ValueError only if both attempts fail. Returns the parsed script dict."""
    raw = client.generate_script(**kwargs)
    try:
        data = extract_json(raw)
    except Exception as e:
        data = None
        _first_err = e
    else:
        _first_err = None
    if _looks_like_script(data):
        data["scenes"] = _normalize_scenes(data.get("scenes"))
        return data
    # Retry once, explicitly demanding the full object shape.
    kw2 = dict(kwargs)
    kw2["description"] = (
        (kwargs.get("description") or "")
        + "\n\nCRITICAL OUTPUT RULE: Return ONE JSON OBJECT exactly shaped "
          '{ "title", "logline", "voiceover", "pacing_seconds", "total_duration", '
          '"scene_count", "characters":[...], "scenes":[...] }. The "scenes" array '
          "is REQUIRED and must be non-empty. Do NOT return a bare array or only "
          "the characters. JSON only, no prose.")
    raw2 = client.generate_script(**kw2)
    data2 = extract_json(raw2)          # may raise — that's a real failure
    if _looks_like_script(data2):
        data2["scenes"] = _normalize_scenes(data2.get("scenes"))
        return data2
    # Both attempts produced an unusable shape.
    if isinstance(data2, dict):
        return data2                    # let downstream surface "no scenes"
    raise ValueError(
        "model did not return a script object with scenes"
        + (f" ({_first_err})" if _first_err else ""))


@app.post("/api/script")
def api_script(s: ScriptIn, request: Request):
    if not (s.title.strip() or s.description.strip() or s.brief.strip()):
        raise HTTPException(400, "a title or description is required")
    st = store.load_state()
    # If the old scene_count form is used, derive a matching duration.
    total_duration = s.total_duration
    pacing = max(0.1, s.pacing_seconds or 1.0)
    if s.scene_count and not s.total_duration:
        total_duration = s.scene_count * pacing
    try:
        data = _generate_script_validated(
            _claude_client_for(s.model, request),
            title=s.title,
            description=s.description,
            total_duration=max(1.0, total_duration or 60.0),
            pacing_seconds=pacing,
            num_characters=(s.num_characters if s.num_characters is not None else -1),
            style_notes=s.style_notes,
            master_prompt=st["master_prompt"],
            brief=s.brief,
            dialogue=s.dialogue,
        )
    except Exception as e:
        raise HTTPException(500, f"script generation failed: {e}")
    st["script"] = data
    store.save_state(st)
    store.log_usage("script", 1, 0.01)
    return data


# --------------------------------------------------------------------------- #
#  Wave 3: script editing tools
# --------------------------------------------------------------------------- #
def _rebuild_voiceover(script):
    vo = "\n\n".join((s.get("vo") or "").strip()
                     for s in (script.get("scenes") or []) if (s.get("vo") or "").strip())
    if vo:
        script["voiceover"] = vo
    return script


class ScriptUpdateIn(BaseModel):
    script: dict


@app.post("/api/script/update")
def api_script_update(body: ScriptUpdateIn):
    st = store.load_state()
    st["script"] = body.script or {}
    store.save_state(st)
    return {"ok": True}


class SceneRegenIn(BaseModel):
    n: int
    direction: str = ""
    model: Optional[str] = None


@app.post("/api/script/scene-regen")
def api_script_scene_regen(b: SceneRegenIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    scenes = sc.get("scenes") or []
    idx = next((i for i, s in enumerate(scenes) if int(s.get("n", 0)) == b.n), -1)
    if idx < 0:
        raise HTTPException(404, "no such scene")
    s = scenes[idx]
    try:
        raw = _claude_client_for(b.model, request).regen_scene(
            b.n, s.get("heading", ""), s.get("action", ""), s.get("vo", ""),
            s.get("prompt", ""), b.direction, st.get("master_prompt", ""))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"scene rewrite failed: {e}")
    data = _as_analysis_dict(data)
    for k in ("heading", "action", "vo", "prompt"):
        if data.get(k) is not None:
            s[k] = data[k]
    scenes[idx] = s
    sc["scenes"] = scenes
    _rebuild_voiceover(sc)
    st["script"] = sc
    store.save_state(st)
    return {"scene": s, "script": sc}


class TranslateIn(BaseModel):
    lang: str
    model: Optional[str] = None


@app.post("/api/script/translate")
def api_script_translate(b: TranslateIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    if not sc.get("scenes"):
        raise HTTPException(400, "no script to translate")
    scene_vos = [s.get("vo", "") for s in sc["scenes"]]
    try:
        raw = _claude_client_for(b.model, request).translate_script(
            sc.get("voiceover", ""), scene_vos, b.lang)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"translation failed: {e}")
    data = _as_analysis_dict(data)
    tmap = {int(x.get("n", 0)): x.get("vo", "") for x in (data.get("scenes") or [])}
    for i, s in enumerate(sc["scenes"], 1):
        if i in tmap:
            s["vo"] = tmap[i]
    if data.get("voiceover"):
        sc["voiceover"] = data["voiceover"]
    else:
        _rebuild_voiceover(sc)
    sc["language"] = b.lang
    st["script"] = sc
    store.save_state(st)
    return sc


class RewriteIn(BaseModel):
    tone: str
    direction: str = ""
    model: Optional[str] = None


@app.post("/api/script/rewrite")
def api_script_rewrite(b: RewriteIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    if not sc.get("scenes"):
        raise HTTPException(400, "no script to rewrite")
    scenes_min = [{"n": s.get("n"), "heading": s.get("heading", ""),
                   "action": s.get("action", ""), "vo": s.get("vo", "")}
                  for s in sc["scenes"]]
    try:
        raw = _claude_client_for(b.model, request).rewrite_tone(
            sc.get("voiceover", ""), scenes_min, b.tone, b.direction,
            st.get("master_prompt", ""))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"rewrite failed: {e}")
    data = _as_analysis_dict(data)
    rmap = {int(x.get("n", 0)): x for x in (data.get("scenes") or [])}
    for i, s in enumerate(sc["scenes"], 1):
        r = rmap.get(int(s.get("n", i)))
        if r:
            for k in ("heading", "action", "vo"):
                if r.get(k) is not None:
                    s[k] = r[k]
    if data.get("voiceover"):
        sc["voiceover"] = data["voiceover"]
    else:
        _rebuild_voiceover(sc)
    st["script"] = sc
    store.save_state(st)
    return sc


class HooksIn(BaseModel):
    n: int = 6
    model: Optional[str] = None


@app.post("/api/script/hooks")
def api_script_hooks(b: HooksIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    title = sc.get("title", "") or ""
    desc = sc.get("logline", "") or (sc.get("voiceover") or "")[:300]
    try:
        raw = _claude_client_for(b.model, request).hooks(title, desc, max(1, min(12, b.n)))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"hooks failed: {e}")
    return {"hooks": _as_analysis_dict(data).get("hooks") or []}


class OutlineIn(BaseModel):
    title: str = ""
    description: str = ""
    beats: int = 6
    model: Optional[str] = None


@app.post("/api/script/outline")
def api_script_outline(b: OutlineIn, request: Request):
    if not (b.title.strip() or b.description.strip()):
        raise HTTPException(400, "a title or description is required")
    try:
        raw = _claude_client_for(b.model, request).outline(
            b.title, b.description, max(3, min(12, b.beats)))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"outline failed: {e}")
    return {"beats": _as_analysis_dict(data).get("beats") or []}


@app.get("/api/script/character-prompts")
def api_script_character_prompts():
    """Packed character sheet prompts from the current script, formatted for the
    bulk character generator (name line, paragraph, blank line between)."""
    st = store.load_state()
    sc = st.get("script") or {}
    chars = sc.get("characters") or []
    blocks = []
    for c in chars:
        name = (c.get("name") or "").strip()
        sheet = (c.get("sheet_prompt") or c.get("description") or "").strip()
        if name:
            blocks.append(f"{name}\n{sheet}".strip())
    return {"text": "\n\n".join(blocks), "count": len(blocks)}


class ScriptToBatchIn(BaseModel):
    pass


@app.get("/api/script/prompts")
def api_script_prompts():
    st = store.load_state()
    if not st.get("script"):
        return {"prompts": []}
    out = []
    for sc in (st["script"].get("scenes") or []):
        p = (sc.get("prompt") or "").strip()
        if p:
            out.append(p)
    return {"prompts": out}


# --------------------------------------------------------------------------- #
#  Claude vision: prompts from uploaded reference video
# --------------------------------------------------------------------------- #
class PromptsFromVideoIn(BaseModel):
    frame_urls: List[str]
    count: int = 8
    style_hint: str = ""


@app.post("/api/prompts-from-video")
def api_prompts_from_video(p: PromptsFromVideoIn, request: Request):
    if not p.frame_urls:
        raise HTTPException(400, "frame_urls is required (extract frames first)")
    st = store.load_state()
    try:
        frames = []
        for u in p.frame_urls[:10]:
            try:
                frames.append(pipeline.downsize_for_vision(store.read_image(u)))
            except Exception:
                pass
        if not frames:
            raise RuntimeError("no readable frames")
        raw = get_claude_client(request).prompts_from_video_frames(
            frames, count=max(1, min(20, p.count)),
            style_hint=p.style_hint, master_prompt=st["master_prompt"],
        )
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"prompt generation failed: {e}")
    data = _as_analysis_dict(data)
    st["suggested_prompts"] = data.get("prompts") or []
    store.save_state(st)
    return data


# --------------------------------------------------------------------------- #
#  Audio upload
# --------------------------------------------------------------------------- #
@app.post("/api/audio")
async def api_audio(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    ext = (os.path.splitext(file.filename or "")[1] or ".mp3").lstrip(".").lower()
    if ext not in {"mp3", "wav", "m4a", "aac", "ogg", "flac"}:
        ext = "mp3"
    url, path = store.write_binary("audio", data, ext=ext, name_hint=file.filename)
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {
        "id": store.new_id("audio"),
        "url": url,
        "name": file.filename or f"audio.{ext}",
        "duration": dur,
    }
    st["audio"] = rec
    store.save_state(st)
    return rec


@app.delete("/api/audio")
def api_delete_audio():
    st = store.load_state()
    st["audio"] = None
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Wave 4: voice preview, background music, captions, title cards
# --------------------------------------------------------------------------- #
def _title_card_bytes(text, subtitle="", width=1920, height=1080):
    """Render a clean title/end card PNG with Pillow (crisp text, no model)."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap
    img = Image.new("RGB", (width, height), (8, 7, 10))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 10], fill=(255, 122, 24))
    d.rectangle([0, height - 10, width, height], fill=(217, 119, 87))

    def font(sz, bold=True):
        cands = (["C:/Windows/Fonts/arialbd.ttf"] if bold else ["C:/Windows/Fonts/arial.ttf"]) + [
            "C:/Windows/Fonts/arial.ttf", "arialbd.ttf", "arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial.ttf"]
        for n in cands:
            try:
                return ImageFont.truetype(n, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    tsize = max(30, width // 16)
    tf, sf = font(tsize, True), font(max(18, width // 46), False)

    def dims(line, f):
        b = d.textbbox((0, 0), line, font=f)
        return (b[2] - b[0], b[3] - b[1])

    chars = max(8, int(width * 0.82 / (tsize * 0.56)))
    lines = textwrap.wrap(text or "", width=chars) or [""]
    line_h = dims("Ag", tf)[1] + 16
    total = line_h * len(lines) + (dims("Ag", sf)[1] + 26 if subtitle else 0)
    y = max(20, (height - total) // 2)
    for ln in lines:
        w, _h = dims(ln, tf)
        d.text(((width - w) // 2, y), ln, fill=(245, 240, 234), font=tf)
        y += line_h
    if subtitle:
        y += 14
        w, _h = dims(subtitle, sf)
        d.text(((width - w) // 2, y), subtitle, fill=(217, 119, 87), font=sf)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class VoicePreviewIn(BaseModel):
    voice_id: Optional[str] = None
    text: str = ""


class VoiceTestIn(BaseModel):
    """Ad-hoc 'Connect' test for a specific provider — uses the inline key/voice
    sent in the body rather than the saved active provider. Lets users verify a
    just-pasted key BEFORE saving + switching providers."""
    provider: str
    api_key: Optional[str] = None     # falls back to the saved key for this provider
    voice_id: Optional[str] = None
    encoding: Optional[str] = None    # Deepgram-only output format


@app.post("/api/voice/test")
def api_voice_test(b: VoiceTestIn, request: Request):
    """Ping ONE specific voice provider with an inline key. Independent of
    whichever provider is currently set as the active default — so the user can
    test a new key the moment they paste it, before clicking Save."""
    import voice as _voice
    s = request.state.settings
    provider = (b.provider or "").strip().lower()

    if provider == "elevenlabs":
        key = (b.api_key or s.get("elevenlabs_api_key") or "").strip()
        if not key:
            return {"ok": False, "provider": provider, "detail": "no ElevenLabs key supplied"}
        client = _voice.VoiceClient(
            api_key=key,
            model=s.get("elevenlabs_model") or config.ELEVENLABS_MODEL,
            voice_id=b.voice_id or s.get("elevenlabs_voice_id") or config.ELEVENLABS_VOICE_ID,
        )
    elif provider == "mimo":
        key = (b.api_key or s.get("mimo_api_key") or "").strip()
        if not key:
            return {"ok": False, "provider": provider, "detail": "no MiMo key supplied"}
        client = _voice.MimoVoiceClient(
            api_key=key,
            model=s.get("mimo_model") or config.MIMO_MODEL,
            voice_id=b.voice_id or s.get("mimo_voice_id") or config.MIMO_VOICE_ID,
        )
    elif provider == "deepgram":
        key = (b.api_key or s.get("deepgram_api_key") or "").strip()
        if not key:
            return {"ok": False, "provider": provider, "detail": "no Deepgram key supplied"}
        client = _voice.DeepgramVoiceClient(
            api_key=key,
            model=s.get("deepgram_model") or config.DEEPGRAM_MODEL,
            voice_id=b.voice_id or s.get("deepgram_voice_id") or config.DEEPGRAM_VOICE_ID,
            encoding=(b.encoding or s.get("deepgram_encoding") or "mp3"),
        )
    elif provider == "piper":
        # No key — test that piper-tts is installed and the voice downloads.
        try:
            client = _voice.PiperVoiceClient(
                voice_id=b.voice_id or s.get("piper_voice_id") or config.PIPER_VOICE,
                use_gpu=s.get("piper_use_gpu"),
            )
        except RuntimeError as e:
            return {"ok": False, "provider": provider, "detail": str(e)}
    else:
        raise HTTPException(400, f"unknown voice provider: {provider}")

    result = client.ping()
    result["provider"] = provider
    return result


@app.post("/api/voice/preview")
def api_voice_preview(b: VoicePreviewIn, request: Request):
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    text = (b.text or "").strip() or "This is a quick preview of how this voice sounds."
    try:
        client = get_voice_client(request, b.voice_id)
        audio = client.synthesize(text[:300])
    except Exception as e:
        raise HTTPException(500, f"preview failed: {e}")
    # Piper returns WAV bytes, every cloud provider returns MP3. Pick the
    # container from the active provider so <audio> in the browser plays it.
    provider = (s.get("voice_provider") or "elevenlabs").lower()
    ext = "wav" if provider == "piper" else "mp3"
    url, _ = store.write_binary("audio", audio, ext=ext, name_hint="preview")
    return {"url": url}


@app.post("/api/music")
async def api_music(file: UploadFile = File(...), volume: float = Form(0.18)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    ext = (os.path.splitext(file.filename or "")[1] or ".mp3").lstrip(".").lower()
    if ext not in {"mp3", "wav", "m4a", "aac", "ogg", "flac"}:
        ext = "mp3"
    url, path = store.write_binary("audio", data, ext=ext,
                                   name_hint=file.filename or "music")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {"id": store.new_id("music"), "url": url,
           "name": file.filename or f"music.{ext}", "duration": dur,
           "volume": max(0.0, min(1.0, float(volume)))}
    st["music"] = rec
    store.save_state(st)
    return rec


# =========================================================================== #
#  🎵 Audio Studio — voice / music / SFX generation, dedicated tab
# =========================================================================== #
#
# One tab, three providers:
#   * Voice — text-to-speech via the active voice provider
#             (ElevenLabs / Xiaomi MiMo / Deepgram Aura / local Piper).
#   * Music — text-to-music via local MusicGen (Meta audiocraft, optional).
#   * SFX   — text-to-SFX via ElevenLabs Sound Generation OR local AudioGen.
#
# All three write to a shared audio library (data/audio_gen/) so generated
# clips can be replayed, downloaded, or pushed into the Edit tab as
# background music / SFX. The library survives server restarts (sidecar JSON).

import audio_gen as _audio_gen


class AudioStudioIn(BaseModel):
    """Request body for /api/audio/studio."""
    kind: str                                # 'voice' | 'music' | 'sfx'
    prompt: str = ""
    # Voice knobs (ignored for music/sfx)
    voice_id: Optional[str] = None
    stability: Optional[float] = 0.5
    similarity_boost: Optional[float] = 0.75
    style: Optional[float] = 0.0
    # Music + SFX knobs
    duration_seconds: Optional[float] = None
    music_size: Optional[str] = "small"      # small / medium / large / melody
    temperature: Optional[float] = 1.0
    top_k: Optional[int] = 250
    # Behaviour
    save_to_library: Optional[bool] = True
    name_hint: Optional[str] = ""


@app.post("/api/audio/studio")
def api_audio_studio(body: AudioStudioIn, request: Request):
    """Generate audio in the Audio Studio tab. Returns the audio bytes as a
    JSON-friendly metadata dict (url, duration, ext, etc.). When
    save_to_library=True (default), the clip is persisted to the audio
    library for replay + reuse."""
    kind = (body.kind or "").strip().lower()
    if kind not in ("voice", "music", "sfx"):
        raise HTTPException(400, f"unknown kind: {kind!r} — pick voice/music/sfx")
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is empty — describe the audio you want")

    try:
        if kind == "voice":
            audio, ext, alignment = _audio_gen.synth_voice(
                request=request,
                get_voice_client_fn=get_voice_client,
                text=prompt,
                voice_id=body.voice_id,
                stability=body.stability or 0.5,
                similarity_boost=body.similarity_boost or 0.75,
                style=body.style or 0.0,
            )
        elif kind == "music":
            if not _audio_gen.musicgen_available():
                raise HTTPException(
                    501, _audio_gen.musicgen_install_hint())
            audio = _audio_gen.synth_music(
                prompt=prompt,
                duration_seconds=body.duration_seconds or 10.0,
                size=body.music_size or "small",
                temperature=body.temperature if body.temperature is not None else 1.0,
                top_k=body.top_k or 250,
                top_p=0.0,
            )
            ext = "wav"
            alignment = None
        else:  # sfx
            # Try the active voice provider's generate_sfx() first (ElevenLabs
            # Sound Generation works this way; MiMo/Deepgram/Piper raise so
            # we fall through to AudioGen if it's installed).
            audio, ext = _try_cloud_sfx(request, prompt, body.duration_seconds)
            if audio is None and _audio_gen.audiogen_available():
                audio = _audio_gen.synth_sfx_local(
                    prompt=prompt,
                    duration_seconds=body.duration_seconds or 2.0,
                )
                ext = "wav"
            elif audio is None:
                raise HTTPException(
                    501,
                    "SFX generation needs either an ElevenLabs key "
                    "(Settings → ElevenLabs) OR local AudioGen installed "
                    f"({_audio_gen.audiogen_install_hint()}).",
                )
            alignment = None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"audio generation failed: {e}")

    # Persist to library + return a URL the browser can <audio src=> directly.
    meta = None
    if body.save_to_library:
        meta = _audio_gen.write_audio_clip(
            audio, ext=ext, name_hint=body.name_hint or "",
            prompt=prompt, provider=kind,
        )
        url = meta["url"]
    else:
        # No library write — use a temp URL via store.write_binary so the
        # browser can still play it once.
        url, _path = store.write_binary("audio", audio, ext=ext,
                                        name_hint=body.name_hint or f"{kind}_clip")
        meta = {"url": url, "ext": ext, "size_bytes": len(audio),
                "duration_seconds": _audio_gen._wav_duration(audio) if ext == "wav" else 0,
                "id": "tmp", "prompt": prompt[:500], "provider": kind,
                "name_hint": body.name_hint or "", "created": store.now()}

    return {
        "ok": True, "kind": kind,
        "url": url, "ext": ext,
        "size_bytes": len(audio),
        "duration_seconds": meta.get("duration_seconds", 0),
        "library_id": meta.get("id"),
        "has_alignment": alignment is not None,
        # Echo knobs so the UI can show what was used.
        "prompt": prompt[:500],
        "provider": _provider_label(request, kind),
    }


def _try_cloud_sfx(request: Request, prompt: str, duration_seconds: float = None):
    """Try the active voice client's generate_sfx() (ElevenLabs Sound API).
    Returns (audio_bytes_or_None, ext). Raises nothing on provider missing
    the method — just returns (None, ext)."""
    try:
        client = get_voice_client(request)
    except Exception:
        return None, "mp3"
    if not hasattr(client, "generate_sfx"):
        return None, "mp3"
    try:
        kwargs = {}
        if duration_seconds:
            kwargs["duration_seconds"] = float(duration_seconds)
        audio = client.generate_sfx(prompt, **kwargs)
    except RuntimeError:
        return None, "mp3"
    except Exception:
        return None, "mp3"
    if not audio:
        return None, "mp3"
    ext = "wav" if (audio[:4] == b"RIFF") else "mp3"
    return audio, ext


def _provider_label(request: Request, kind: str) -> str:
    """Human-readable label of the provider actually used (for the UI toast)."""
    if kind == "voice":
        s = request.state.settings
        return f"voice:{(s.get('voice_provider') or 'elevenlabs').lower()}"
    if kind == "music":
        return "music:musicgen"
    if kind == "sfx":
        s = request.state.settings
        if (s.get("voice_provider") or "elevenlabs") == "elevenlabs":
            return "sfx:elevenlabs"
        return "sfx:audiogen"
    return kind


@app.get("/api/audio/library")
def api_audio_library(limit: int = 200):
    """List saved Audio Studio clips, newest first."""
    return {"clips": _audio_gen.list_audio_clips(limit=limit)}


@app.get("/api/audio/library/resolve")
def api_audio_library_resolve(clip_id: str):
    """Resolve a library clip back to its URL + filename + duration so the
    front-end can wire it into the Edit tab (music, SFX, or voice slot)."""
    clips = _audio_gen.list_audio_clips(limit=10000)
    hit = next((c for c in clips if c.get("id") == clip_id), None)
    if not hit or not os.path.exists(hit.get("path", "")):
        raise HTTPException(404, f"clip not found: {clip_id}")
    return {"ok": True, "id": hit["id"], "url": hit["url"],
            "name": os.path.basename(hit["path"]), "path": hit["path"],
            "duration": hit.get("duration_seconds", 0),
            "provider": hit.get("provider", ""),
            "ext": hit.get("ext", "wav")}


class AudioPushToEditIn(BaseModel):
    clip_id: str
    kind: str   # 'voice' | 'music' | 'sfx'


@app.post("/api/audio/push-to-edit")
def api_audio_push_to_edit(body: AudioPushToEditIn, request: Request):
    """Promote a library clip into the current project's Edit-tab slot
    (background music / SFX / voiceover). Persists to project state."""
    kind = (body.kind or "").strip().lower()
    if kind not in ("voice", "music", "sfx"):
        raise HTTPException(400, f"unknown kind: {kind!r}")
    clips = _audio_gen.list_audio_clips(limit=10000)
    hit = next((c for c in clips if c.get("id") == body.clip_id), None)
    if not hit or not os.path.exists(hit.get("path", "")):
        raise HTTPException(404, f"clip not found: {body.clip_id}")
    st = store.load_state()
    name = os.path.basename(hit["path"])
    dur = hit.get("duration_seconds") or 0
    if kind == "music":
        st["music"] = {"id": hit["id"], "url": hit["url"],
                       "name": name, "duration": dur, "volume": 0.18}
    elif kind == "sfx":
        st["sfx"] = st.get("sfx") or []
        # Replace any existing SFX with this id (so re-pushing the same clip
        # doesn't pile up duplicates).
        st["sfx"] = [s for s in st["sfx"] if s.get("id") != hit["id"]]
        st["sfx"].append({"id": hit["id"], "url": hit["url"], "name": name,
                          "duration": dur, "volume": 0.8, "at_seconds": 0})
    else:  # voice
        st["audio_url"] = hit["url"]
        st["audio_name"] = name
        st["audio_duration"] = dur
    store.save_state(st)
    return {"ok": True, "kind": kind, "name": name, "duration": dur,
            "url": hit["url"]}


class AudioLibraryDeleteIn(BaseModel):
    clip_id: str


@app.post("/api/audio/library/delete")
def api_audio_library_delete(body: AudioLibraryDeleteIn):
    """Delete a clip from the audio library."""
    if not body.clip_id:
        raise HTTPException(400, "clip_id required")
    ok = _audio_gen.delete_audio_clip(body.clip_id)
    return {"ok": ok}


class AudioStudioCapabilityIn(BaseModel):
    kind: str   # 'music' | 'sfx'


@app.post("/api/audio/capability")
def api_audio_capability(body: AudioStudioCapabilityIn):
    """Probe whether the requested local model is installed. The UI uses this
    to render an 'install audiocraft' banner instead of failing on first click."""
    kind = (body.kind or "").strip().lower()
    if kind == "music":
        return {"available": _audio_gen.musicgen_available(),
                "models": list(_audio_gen.MUSICGEN_MODELS.keys()),
                "hint": _audio_gen.musicgen_install_hint()}
    if kind == "sfx":
        return {"available": _audio_gen.audiogen_available(),
                "models": list(_audio_gen.AUDIOGEN_MODELS.keys()),
                "hint": _audio_gen.audiogen_install_hint()}
    raise HTTPException(400, f"unknown kind: {kind!r}")


@app.delete("/api/music")
def api_delete_music():
    st = store.load_state()
    st["music"] = None
    store.save_state(st)
    return {"ok": True}


class MusicVolIn(BaseModel):
    volume: float = 0.18


@app.post("/api/music/volume")
def api_music_volume(b: MusicVolIn):
    st = store.load_state()
    if st.get("music"):
        st["music"]["volume"] = max(0.0, min(1.0, float(b.volume)))
        store.save_state(st)
    return st.get("music") or {}


def _srt_time(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@app.get("/api/captions.srt")
def api_captions():
    """Build an .srt — accurate when a per-scene voice-over exists (uses its
    real hold times), otherwise estimated from the script by word count."""
    st = store.load_state()
    cues = []
    vo = st.get("voiceover") or {}
    scenes = vo.get("scenes") or []
    if scenes and any(s.get("hold_seconds") for s in scenes):
        for s in scenes:
            txt = (s.get("vo") or "").strip()
            if txt:
                cues.append((txt, float(s.get("hold_seconds") or s.get("audio_seconds") or 2.0)))
    else:
        sc = st.get("script") or {}
        pacing = float(sc.get("pacing_seconds") or 1.0)
        for s in (sc.get("scenes") or []):
            txt = (s.get("vo") or "").strip()
            if not txt:
                continue
            cues.append((txt, max(pacing, len(txt.split()) / 2.6)))
    if not cues:
        raise HTTPException(400, "No script or voice-over to caption yet.")
    out, t = [], 0.0
    for i, (txt, dur) in enumerate(cues, 1):
        start, end = t, t + max(0.8, dur)
        t = end
        out.append(f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{txt}\n")
    body = "\n".join(out)
    title = _safe_name((st.get("script") or {}).get("title") or "captions", "captions")
    return Response(content=body, media_type="application/x-subrip",
                    headers={"Content-Disposition": f'attachment; filename="{title}.srt"'})


def _music_for(st, want):
    """(music_path, volume) for the current project's music, or (None, .18)."""
    mus = st.get("music")
    if want and mus:
        try:
            return store.url_to_path(mus["url"]), float(mus.get("volume", 0.18))
        except Exception:
            pass
    return None, 0.18


def _bookend(out_path, intro, outro, w, h, fps):
    """Best-effort intro/outro title cards; never breaks the main render."""
    if not (intro or outro):
        return
    try:
        intro_img = outro_img = None
        if intro:
            iu = store.write_image("images", _title_card_bytes(intro, "", w, h))
            intro_img = store.url_to_path(iu)
        if outro:
            ou = store.write_image("images", _title_card_bytes(outro, "", w, h))
            outro_img = store.url_to_path(ou)
        booked = out_path + ".bk.mp4"
        editor.bookend_video(out_path, booked, intro_img, outro_img, 2.2, w, h, fps)
        os.replace(booked, out_path)
    except Exception as ex:
        print(f"[bookend] skipped: {ex}", flush=True)


# --------------------------------------------------------------------------- #
#  ElevenLabs voice-over — synthesize the script's narration into real audio
# --------------------------------------------------------------------------- #
def _script_voiceover_text(st) -> str:
    """Full narration text. Unwraps scripts stored as JSON string in
    sc['content'] (autopilot file-path-backed scripts)."""
    sc = st.get("script") or {}
    _inner = sc
    _c = sc.get("content")
    if _c and isinstance(_c, str):
        try:
            _inner = json.loads(_c)
        except Exception:
            pass
    vo = (_inner.get("voiceover") or "").strip()
    if vo:
        return vo
    scenes = _inner.get("scenes") or []
    parts = [(s.get("vo") or s.get("narration") or s.get("voice_over") or "").strip()
             for s in scenes]
    return "\n\n".join(p for p in parts if p).strip()


def _unwrap_script(sc):
    """Return the real parsed script dict, unwrapping sc['content'] if the
    script was stored as a JSON string (autopilot file-path-backed scripts)."""
    if not sc:
        return {}
    _c = sc.get("content")
    if _c and isinstance(_c, str):
        try:
            inner = json.loads(_c)
            if isinstance(inner, dict) and inner.get("scenes"):
                return inner
        except Exception:
            pass
    return sc



def _scene_vo_lines(st):
    """List of per-scene narration lines (one per numbered scene), in order.

    Unwraps autopilot file-path-backed scripts (scenes stored as a JSON string
    in sc['content']) and accepts narration field aliases (vo / narration /
    voice_over), so per-scene timestamp sync works for autopilot scripts too —
    not just the in-app generator's already-normalized scenes."""
    sc = _unwrap_script(st.get("script") or {})
    return [(s.get("vo") or s.get("narration") or s.get("voice_over") or "").strip()
            for s in (sc.get("scenes") or [])]


@app.get("/api/voices")
def api_voices(request: Request):
    """List the ElevenLabs voices available on the configured account."""
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    try:
        voices = get_voice_client(request).list_voices()
    except Exception as e:
        raise HTTPException(500, str(e))
    _provider = (s.get("voice_provider") or "elevenlabs").lower()
    if _provider == "mimo":
        current = s.get("mimo_voice_id")
    elif _provider == "deepgram":
        current = s.get("deepgram_voice_id")
    else:
        current = s["elevenlabs_voice_id"]
    return {"voices": voices, "current": current, "provider": _provider}


# --------------------------------------------------------------------------- #
#  "Natural flow" assembly — keep the voice-over continuous, time frames to it.
#
#  The older synced path voices each scene SEPARATELY, which chops sentences and
#  makes the narration sound slow / unnatural, and can leave one frame on screen
#  far too long. This path instead:
#    1. synthesizes the WHOLE script as ONE continuous track (natural prosody —
#       the audio is never time-stretched or pitch-shifted),
#    2. measures it, then distributes the frames across that timeline weighted by
#       how much narration each frame's scene carries (so each image is on screen
#       roughly while its line is spoken),
#    3. splits any frame that would sit too long into micro-cuts.
#  The result: audio flows, frames match it, nothing freezes.
# --------------------------------------------------------------------------- #
def _word_count(s: str) -> int:
    return len([w for w in re.split(r"\s+", (s or "").strip()) if w])


# ---------------------------------------------------------------------------
# Style sanitizer — globally bans doodle / hand-drawn aesthetics and replaces
# them with stick-man / stick-figure style throughout prompts and style notes.
# ---------------------------------------------------------------------------
_DOODLE_RE = re.compile(
    r"\b(hand[\s\-]?drawn|hand[\s\-]?sketched|hand[\s\-]?painted|"
    r"doodle[ds]?|doodle[\s\-]?style|"
    r"whiteboard[\s\-]?(?:animation|style|art)?|"
    r"marker[\s\-]?sketch|pencil[\s\-]?sketch|chalk[\s\-]?(?:art|drawing|style)?|"
    r"crayon[\s\-]?(?:art|style)?|scribble[ds]?|"
    r"rough[\s\-]?sketch|childlike[\s\-]?drawing|napkin[\s\-]?sketch|"
    r"line[\s\-]?art[\s\-]?style(?:\s+illustration)?)\b",
    re.IGNORECASE,
)

_STICK_REPLACEMENT = "stick man / stick figure style"


def _sanitize_prompt(text: str) -> str:
    """Pass prompts through unchanged.

    Historically this rewrote any hand-drawn / doodle / cartoon style keyword to
    a fixed 'stick man / stick figure style', which silently DESTROYED the
    uploaded reference video's art style on every scene + character prompt (the
    #1 reason generated images never matched the source). Style fidelity is now
    enforced positively in the prompts, so this is a no-op — the analysed style
    is preserved verbatim. Kept as a function so existing call sites still work.
    """
    return text or ""


_CAM_CUES = [
    "extreme close-up on face", "wide cinematic shot", "medium shot",
    "low dramatic angle", "over-the-shoulder", "dutch tilt",
    "high angle overview", "tight close-up", "establishing wide shot",
    "ground-level shot",
]

# Temporal moment progressions — make each split sub-frame a genuinely
# different visual beat (not just a re-angle of the same moment).
_MOMENT_CUES = [
    "",                                                    # 0: original prompt, no modifier
    "extreme close-up on face showing raw emotion",
    "wide establishing shot, full environment revealed",
    "low angle dramatic upshot, subject looks powerful",
    "cutaway — tight focus on a key prop or environmental detail",
    "reaction shot, secondary element responding to the action",
    "high angle overview, small subject in a large world",
    "medium shot, action at its peak intensity",
    "over-the-shoulder, slight push-in toward the subject",
    "transition beat, camera pulling back to show wider context",
]

# Error fragments that mean the IMAGE account itself is broken (no credits / bad
# key). Every remaining image call will fail the same way, so the pipeline stops
# burning attempts and surfaces the cause. Shared by the character + frame steps.
_FATAL_IMAGE_MARKERS = (
    "billing hard limit", "billing_hard_limit", "insufficient credit",
    "insufficient_quota", "exceeded your current quota", "billing_error",
    "wallet-balance", "slot reservation failed", "http 402", "[402]",
    "rejected the key", "invalid api key",
)


def _is_fatal_image_error(msg: str) -> bool:
    low = (msg or "").lower()
    return any(k in low for k in _FATAL_IMAGE_MARKERS)


# Words that already denote a camera framing/angle in a prompt — if present we
# DON'T append an angle cue (avoids fighting the model's own composition).
_CAMERA_WORDS = (
    "close-up", "closeup", "close up", "wide shot", "wide-shot", "establishing",
    "low angle", "high angle", "overhead", "bird's eye", "birds-eye", "aerial",
    "over-the-shoulder", "over the shoulder", "dutch tilt", "pov", "point of view",
    "medium shot", "long shot", "extreme close", "macro", "from above", "from below",
)


def _has_camera_language(prompt: str) -> bool:
    low = (prompt or "").lower()
    return any(w in low for w in _CAMERA_WORDS)


def _angle_cue_for(index: int) -> str:
    """Cycle through the non-empty camera cues so consecutive frames change
    angle — the 'micro-cut' look. index 0..N -> cue 1..len-1, skipping the
    empty placeholder at [0]."""
    span = len(_MOMENT_CUES) - 1
    return _MOMENT_CUES[(index % span) + 1] if span > 0 else ""


def _split_script_scenes(script: dict, target_count: int) -> dict:
    """Expand an under-populated scenes list by splitting each scene into
    sub-scenes until we reach ``target_count``.

    Each sub-scene uses a temporal moment cue (opening → close-up → reaction →
    cutaway → wide → detail → payoff) so consecutive frames feel like genuine
    cuts rather than repeated stills with different angles.
    """
    scenes = script.get("scenes") or []
    if not scenes or len(scenes) >= target_count:
        return script

    split_factor = math.ceil(target_count / len(scenes))
    new_scenes: list = []

    for orig in scenes:
        words = (orig.get("vo") or "").split()
        base_prompt = (orig.get("prompt") or "").strip()
        heading = (orig.get("heading") or "").strip()
        action = (orig.get("action") or "").strip()

        # Distribute VO words evenly across sub-frames so each carries a
        # proportional speech slice (important for audio-sync hold calculation).
        chunk_sz = max(1, math.ceil(len(words) / split_factor)) if words else 0

        for j in range(split_factor):
            if len(new_scenes) >= target_count:
                break

            if words and chunk_sz:
                start_w = j * chunk_sz
                vo_slice = " ".join(words[start_w:start_w + chunk_sz])
            else:
                vo_slice = ""

            moment_idx = j % len(_MOMENT_CUES)
            cue = _MOMENT_CUES[moment_idx]
            if cue:
                prompt = f"{base_prompt}, {cue}" if base_prompt else base_prompt
            else:
                # j==0: no moment cue — add a camera cut for variety
                cam = _CAM_CUES[len(new_scenes) % len(_CAM_CUES)]
                prompt = f"{base_prompt}, {cam}" if base_prompt else base_prompt

            new_scenes.append({
                "n": len(new_scenes) + 1,
                "heading": heading,
                "action": action,
                "vo": vo_slice,
                "prompt": prompt,
            })

    script["scenes"] = new_scenes
    script["scene_count"] = len(new_scenes)
    return script


def _holds_from_alignment(tts_lines: list, alignment, total_dur: float):
    """Convert ElevenLabs character-level timestamps to per-scene hold durations.

    ``tts_lines`` must be exactly the per-scene VO lines that were joined with
    '\\n\\n' and sent to the TTS API.  ``alignment`` is the dict returned by
    ``synthesize_with_timestamps`` (normalized_alignment preferred).

    Returns a list of floats (seconds per scene) summing to ``total_dur``,
    or ``None`` if the alignment data is missing / inconsistent.
    """
    if not alignment:
        return None
    # Guard: total_dur must be a real positive number — prevents division by zero.
    try:
        total_dur = float(total_dur)
    except (TypeError, ValueError):
        return None
    if total_dur <= 0.0:
        return None

    chars = alignment.get("characters", [])
    t_starts = alignment.get("character_start_times_seconds", [])
    t_ends = alignment.get("character_end_times_seconds", [])
    if not chars or not t_starts or len(chars) != len(t_starts):
        return None

    align_text = "".join(chars)
    if not align_text.strip():
        return None

    n = len(tts_lines)
    if n == 0:
        return None

    scene_start_times = []
    search_from = 0

    for line in tts_lines:
        stripped = line.strip()
        if not stripped:
            # Empty VO — record current position's time; will be interpolated.
            scene_start_times.append(None)
            continue
        # Try to locate first 20 chars of the line in the alignment text.
        # Progressively shorter needles handle Whisper/EL transcription
        # differences (slight word changes, casing, punctuation).
        found = False
        for needle_len in (20, 12, 8, 5, 3):
            needle = stripped[:needle_len].strip().lower()
            if not needle:
                continue
            # Case-insensitive search handles Whisper lowercase vs original.
            pos = align_text.lower().find(needle, search_from)
            if pos >= 0:
                scene_start_times.append(t_starts[pos])
                search_from = pos + max(1, len(stripped) // 3)
                found = True
                break
        if not found:
            # Could not find the line; interpolate from current position.
            scene_start_times.append(t_starts[min(search_from, len(t_starts) - 1)])

    # Fill gaps (None entries) by linear interpolation.
    audio_end = t_ends[-1] if t_ends else total_dur
    for i in range(len(scene_start_times)):
        if scene_start_times[i] is None:
            prev = next((scene_start_times[j] for j in range(i - 1, -1, -1)
                         if scene_start_times[j] is not None), 0.0)
            nxt = next((scene_start_times[j] for j in range(i + 1, len(scene_start_times))
                        if scene_start_times[j] is not None), audio_end)
            scene_start_times[i] = (prev + nxt) / 2.0

    holds = []
    for i in range(n):
        t_start = scene_start_times[i] if i < len(scene_start_times) else 0.0
        t_end = (scene_start_times[i + 1]
                 if i + 1 < len(scene_start_times) else audio_end)
        holds.append(max(0.3, round(t_end - t_start, 3)))

    total = sum(holds)
    if total <= 0:
        return None
    # Normalize so the holds sum to exactly total_dur (keeps A/V locked).
    scale = total_dur / total
    return [round(h * scale, 3) for h in holds]


# --------------------------------------------------------------------------- #
#  ElevenLabs sound design — Claude-driven SFX planning + generation.
# --------------------------------------------------------------------------- #
_SFX_CACHE_DIR = os.path.join(config.DATA_DIR, "sfx_cache")


def _generate_sfx_cached(request, text, duration=None):
    """Generate (or reuse) an ElevenLabs sound effect for ``text``. Cached on
    disk by text+duration so the same boom/whoosh isn't paid for twice.
    Returns (url, path)."""
    os.makedirs(_SFX_CACHE_DIR, exist_ok=True)
    key = hashlib.md5(f"{text}|{duration}".encode("utf-8")).hexdigest()[:16]
    path = os.path.join(_SFX_CACHE_DIR, f"{key}.mp3")
    if not os.path.exists(path):
        mp3 = get_voice_client(request).generate_sfx(text, duration_seconds=duration)
        with open(path, "wb") as f:
            f.write(mp3)
    rel = os.path.relpath(path, store.DATA_DIR).replace(os.sep, "/")
    return f"/data/{rel}", path


# Short percussive sounds used for the "click on every frame change" feature.
# Each style maps to an ElevenLabs sound-generation prompt + duration; results
# are disk-cached by _generate_sfx_cached so each style is only paid for once.
_CLICK_STYLES = {
    "click":   ("single sharp tactile mouse click, dry, instant, no reverb, no echo", 0.6),
    "camera":  ("single crisp camera shutter click, punchy, dry, fast", 0.7),
    "whoosh":  ("very short fast air whoosh swipe transition, subtle", 0.8),
    "pop":     ("single soft bubble pop, very short, clean, dry", 0.6),
    "tick":    ("single mechanical clock tick, dry, precise", 0.6),
}


def _cut_click_file(request, style="click"):
    """Return a local path to the click sound for ``style``. Generates it via
    ElevenLabs (cached) and falls back to an ffmpeg-synthesized blip when the
    ElevenLabs call is unavailable, so renders never fail because of it."""
    desc, dur = _CLICK_STYLES.get((style or "click").lower(), _CLICK_STYLES["click"])
    try:
        _url, path = _generate_sfx_cached(request, desc, duration=dur)
        return path
    except Exception as e:
        print(f"[clicks] ElevenLabs click unavailable ({e}); using synth fallback",
              flush=True)
    path = os.path.join(_SFX_CACHE_DIR, "_fallback_click.wav")
    if not os.path.exists(path):
        os.makedirs(_SFX_CACHE_DIR, exist_ok=True)
        _run_capture(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1800:duration=0.05",
             "-af", "afade=t=out:st=0.012:d=0.038,volume=0.9", path],
            timeout=60)
    return path if os.path.exists(path) else None


def _apply_cut_clicks(request, video_path, durations, volume=0.30, style="click"):
    """Mix a click at every frame change. ``durations`` is the per-shot
    on-screen time, in play order; clicks land on each boundary (not at 0 or
    the very end). Best-effort — failures never break the render."""
    cuts, t = [], 0.0
    for d in (durations or [])[:-1]:
        t += max(0.0, float(d or 0))
        cuts.append(round(t, 3))
    if not cuts:
        return
    click = _cut_click_file(request, style)
    if not click:
        return
    try:
        editor.add_cut_clicks(video_path, click, cuts, volume=volume)
        print(f"[clicks] mixed {len(cuts)} cut clicks ({style})", flush=True)
    except Exception as e:
        print(f"[clicks] skipped: {e}", flush=True)


def _plan_sfx_with_claude(request, scene_map, total_seconds):
    """Ask Claude to design a sound plan for the video. Returns a dict with
    'bed' (ambient bed description) and 'cues' (list of point SFX)."""
    scenes_text = []
    t = 0.0
    for sc in scene_map:
        hold = float(sc.get("hold_seconds") or 0)
        vo = (sc.get("vo") or "").strip()
        scenes_text.append(f"  [{t:.1f}s - {t+hold:.1f}s] Scene {sc.get('index', '?')}: \"{vo}\"")
        t += hold

    prompt = (
        f"You are a sound designer for a {total_seconds:.0f}-second short-form video.\n"
        f"Here is the timeline with narration per scene:\n"
        + "\n".join(scenes_text) + "\n\n"
        "Design a SUBTLE, PROFESSIONAL sound plan. Return STRICT JSON ONLY:\n"
        "{\n"
        '  "bed": "short ElevenLabs SFX prompt for a loopable ambient bed that fits this video\'s mood/topic (8-15 words)",\n'
        '  "cues": [\n'
        '    { "at": float, "desc": "short ElevenLabs SFX prompt (5-12 words, natural/realistic)", "dur": float, "vol": float }\n'
        "  ]\n"
        "}\n\n"
        "RULES:\n"
        "- The bed is a SUBTLE ambient texture that loops under the voice — NOT a dramatic drone. "
        "Match it to the video's world (e.g. soft city hum, gentle wind, quiet room tone, "
        "distant nature sounds). Keep it textural and barely noticeable.\n"
        "- Place at most 4-6 point cues across the WHOLE video. LESS IS MORE. "
        "Only place a sound where it genuinely enhances a moment — a transition, a reveal, "
        "an emotional beat. NOT on every scene.\n"
        "- Minimum 4 seconds gap between any two cues. Never stack sounds.\n"
        "- Volume: 0.15-0.30 for subtle accents, 0.35-0.50 ONLY for one big payoff moment. "
        "The voice-over must ALWAYS be the loudest thing.\n"
        "- Duration: 0.5-2.0 seconds for hits/whooshes, up to 3.0 for swells.\n"
        "- SFX descriptions must be SPECIFIC and NATURAL sounding — "
        "write them as short prompts for ElevenLabs Sound Generation API. "
        "Good: 'soft camera shutter click', 'gentle rising orchestral swell'. "
        "Bad: 'huge epic massive explosion boom impact', 'dramatic cinematic hit'.\n"
        "- Match the TONE of the video. Comedy = playful sounds. Scary = tension sounds. "
        "Educational = subtle transitions. Don't put horror sounds on a funny video.\n"
        "- A subtle whoosh on the first scene transition is fine but not mandatory.\n"
        "JSON only, no markdown."
    )
    try:
        raw = get_claude_client(request).chat_text(prompt, max_tokens=1500)
        return extract_json(raw)
    except Exception as e:
        print(f"[sound] Claude SFX planning failed: {e}", flush=True)
        return None


def _normalize_loud(video_path, target_lufs=-14.0):
    """Loudness-normalize + limit a finished video's audio for crisp, LOUD
    Shorts/TikTok output. Voice is compressed so it stays above the bed. In place."""
    tmp = video_path + ".loud.mp4"
    af = (f"acompressor=threshold=-18dB:ratio=3:attack=5:release=120,"
          f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11,alimiter=limit=0.97")
    proc = _run_capture(
        ["ffmpeg", "-y", "-i", video_path, "-af", af,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", tmp])
    if proc.returncode == 0 and os.path.exists(tmp):
        os.replace(tmp, video_path)
    else:
        print(f"[sound] loudnorm skipped: {proc.stderr[-200:]}", flush=True)


def _auto_sound_design(request, scene_map, total_seconds):
    """Claude-driven sound design. Asks Claude to analyse the narration and
    design a tasteful, sparse sound plan, then generates the clips via
    ElevenLabs. Returns (rumble_path_or_None, sfx_entries)."""
    sfx_entries = []
    rumble_path = None

    plan = _plan_sfx_with_claude(request, scene_map, total_seconds)

    if plan and plan.get("bed"):
        bed_desc = str(plan["bed"]).strip()
        if not bed_desc.lower().endswith("seamless"):
            bed_desc += ", seamless loop"
        try:
            _, rumble_path = _generate_sfx_cached(request, bed_desc, duration=22)
        except Exception as e:
            print(f"[sound] bed skipped: {e}", flush=True)

    cues = (plan or {}).get("cues") or []
    last_at = -5.0
    for cue in cues[:6]:
        at = float(cue.get("at", 0))
        if at - last_at < 4.0:
            continue
        desc = str(cue.get("desc", "")).strip()
        if not desc:
            continue
        dur = max(0.5, min(3.0, float(cue.get("dur", 1.5))))
        vol = max(0.10, min(0.50, float(cue.get("vol", 0.25))))
        try:
            url, _p = _generate_sfx_cached(request, desc, duration=dur)
            sfx_entries.append({"id": store.new_id("sfx"), "url": url,
                                "name": desc[:40], "at_seconds": round(max(0, at), 2),
                                "volume": vol})
            last_at = at
        except Exception as e:
            print(f"[sound] sfx '{desc[:24]}' skipped: {e}", flush=True)

    if not plan:
        try:
            _, rumble_path = _generate_sfx_cached(
                request, "soft subtle ambient room tone texture, seamless loop",
                duration=22)
        except Exception:
            pass

    return rumble_path, sfx_entries


def _synth_per_scene_track(vc, tts_lines, name_hint, pad=0.04):
    """Synthesize each scene's narration SEPARATELY, measure its real spoken
    length, and concat the clips (each padded to its hold) into one track.
    Returns (track_path, total_duration, holds) — one hold per scene line. This
    gives exact A/V sync for ANY TTS provider, including ones without
    character-level timestamps (e.g. MiMo), where the single-track path can only
    estimate frame timing from word counts. Mirrors /api/voiceover/scenes."""
    n = len(tts_lines)
    work = os.path.join(store.VIDEOS_DIR, f"_aps_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    clip_paths, holds = [], []
    # Deepgram is configured for lossless WAV; other providers may return mp3.
    # ffmpeg sniffs by content so the extension is cosmetic, but match it so
    # the lossless source isn't silently re-encoded by a wrong-suffix probe.
    clip_ext = "wav" if (
        vc.__class__.__name__ == "DeepgramVoiceClient"
        and getattr(vc, "encoding", "mp3").lower() in ("wav", "linear16")
    ) else "mp3"
    for i in range(n):
        line = (tts_lines[i] or "").strip()
        mp3 = vc.synthesize(line if line else " ")
        ap = os.path.join(work, f"vo_{i:03d}.{clip_ext}")
        with open(ap, "wb") as f:
            f.write(mp3)
        try:
            # trim_start=False: never eat the first syllable of a scene's VO
            # (Aura's soft onsets were being clipped — see editor.trim_silence).
            editor.trim_silence(ap, trim_start=False)
        except Exception:
            pass
        try:
            d = float(editor.probe_duration(ap))
        except Exception:
            d = 0.0
        if d <= 0.05:
            d = 0.8
        hold = round(d + max(0.0, pad), 3)
        holds.append(hold)
        clip_paths.append((ap, hold))
    concat_inputs, filt = [], ""
    for j, (ap, hold) in enumerate(clip_paths):
        concat_inputs += ["-i", ap]
        # pad each clip with trailing silence to its hold so audio lines up with
        # the image timing exactly; normalize format so concat never mismatches.
        filt += (f"[{j}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                 f"apad=whole_dur={hold}[a{j}];")
    filt += "".join(f"[a{j}]" for j in range(len(clip_paths)))
    filt += f"concat=n={len(clip_paths)}:v=0:a=1[out]"
    track = os.path.join(work, "track.mp3")
    cc = _run_capture(["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
                       "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", track])
    if cc.returncode != 0 or not os.path.exists(track):
        raise RuntimeError(f"per-scene track concat failed: {(cc.stderr or '')[-200:]}")
    with open(track, "rb") as f:
        turl, ppath = store.write_binary("audio", f.read(), ext="mp3",
                                         name_hint=name_hint)
    return ppath, turl, round(sum(holds), 3), holds


def _synth_continuous_track(vc, tts_lines, name_hint, settings=None):
    """Synthesize the ENTIRE narration as ONE continuous track (natural prosody,
    no per-scene resets, no padded silence between scenes), then recover the
    per-scene hold durations by transcribing the track with Whisper word
    timestamps and mapping each scene's words back to real time.

    This is the correct model for "visuals follow the voice": the voice flows
    naturally start-to-finish and the FRAMES are timed to where the words
    actually land — instead of chopping the audio per scene and padding each
    clip with trailing silence (which caused the pauses + robotic boundary
    pronunciation).

    Returns (track_path, track_url, total_duration, holds) on success, or
    None if Whisper isn't available / transcription fails (caller falls back).
    """
    try:
        import transcribe
    except Exception:
        return None
    if not transcribe.local_available():
        return None  # no Whisper -> can't measure word timing on a single track

    lines = [(l or "").strip() for l in tts_lines]
    nonempty = [l for l in lines if l]
    if not nonempty:
        return None
    # One natural read of the whole script. Single space between scene lines so
    # the engine reads it as continuous prose (no hard stops mid-narration).
    full_text = " ".join(nonempty)

    # 1. Synthesize the single continuous track.
    try:
        audio_bytes = vc.synthesize(full_text)
    except Exception as _e:
        print(f"[video] continuous synth failed ({_e})", flush=True)
        return None
    ext = "wav" if (
        vc.__class__.__name__ == "DeepgramVoiceClient"
        and getattr(vc, "encoding", "mp3").lower() in ("wav", "linear16")
    ) else "mp3"
    url, path = store.write_binary("audio", audio_bytes, ext=ext, name_hint=name_hint)
    try:
        editor.trim_silence(path, trim_start=False)
    except Exception:
        pass

    # 2. Transcribe with word timestamps to learn WHEN each word is spoken.
    try:
        result = transcribe.transcribe_audio(path, settings=settings, engine="local")
    except Exception as _e:
        print(f"[video] continuous-track transcription failed ({_e})", flush=True)
        return None
    words = result.get("words") or []
    total = float(result.get("duration") or 0.0)
    if not words or total <= 0.1:
        return None

    # 3. Map each scene line to a real time-span by walking the word stream.
    #    We match scene words to the transcript words in order, so scene i's
    #    hold = (time of first word of scene i+1) - (time of first word of scene i).
    import re as _re

    def _toks(s):
        return [t for t in _re.findall(r"[a-z0-9']+", (s or "").lower()) if t]

    word_starts = [float(w.get("start") or 0.0) for w in words]
    word_toks = [_re.sub(r"[^a-z0-9']", "", (w.get("word") or "").lower()) for w in words]

    holds = []
    wi = 0                      # pointer into the transcript word list
    scene_start_times = []
    for line in lines:
        # Record the transcript time where this scene's first word lands.
        scene_start_times.append(word_starts[wi] if wi < len(word_starts) else total)
        st_toks = _toks(line)
        if not st_toks:
            # Empty scene line — give it a tiny hold, don't advance the pointer.
            continue
        # Advance wi past this scene's tokens (best-effort fuzzy match: count
        # how many transcript words this scene should consume).
        consume = len(st_toks)
        wi = min(len(words), wi + consume)

    # Convert scene start times into per-scene holds.
    for i in range(len(lines)):
        start_t = scene_start_times[i]
        end_t = scene_start_times[i + 1] if i + 1 < len(scene_start_times) else total
        hold = round(max(0.25, end_t - start_t), 3)
        holds.append(hold)

    # Sanity: the holds must sum close to the real duration. If the word-mapping
    # drifted badly (rare), bail so the caller uses a safer path.
    if abs(sum(holds) - total) > max(2.0, total * 0.25):
        print(f"[video] continuous-track hold drift too large "
              f"(sum={sum(holds):.1f} vs dur={total:.1f}); falling back", flush=True)
        return None

    print(f"[video] continuous track: {len(holds)} scenes mapped over "
          f"{total:.2f}s (natural flow, no gaps)", flush=True)
    return path, url, round(total, 3), holds


def _build_flow_video(st, request, *, voice_id, text, transition="cut",
                      width=1920, height=1080, fps=30, max_hold=2.5,
                      motion=False, use_music=False, name_hint="flow",
                      sound_design=False, smart_edit=True,
                      cut_clicks=False, cut_click_volume=0.30,
                      cut_click_style="click", manual_holds=None,
                      target_seconds=None, force_continuous=False):
    """Shared engine for the natural-flow video. Returns
    (edit_rec, video_url, total_seconds, scene_map). Mutates + saves `st`.

    ``sound_design`` (needs an ElevenLabs key) generates real SFX via the
    ElevenLabs Sound API — a low rumble bed under the whole video plus contextual
    booms/whooshes/crackles on the matching narration beats — mixes them in, and
    loudness-normalizes the result for crisp, loud Shorts audio."""
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script first.")

    # 0. SANITIZE the sequence: remove stale frames from a previous project/topic
    #    that got left in the sequence when a new script was generated. This was
    #    the cause of "video has wrong frames / extra scenes / no voice on some
    #    frames" — the sequence had 23 frames (prison + old serotonin + empty)
    #    but the script only had 10 prison scenes. Build Video happily rendered
    #    all 23, producing 13 frames with wrong or no voiceover.
    #    Fix: trim the sequence to only frames that belong to the CURRENT script.
    #    Strategy: compare frame VO to script scene VO. Keep only frames whose
    #    vo matches a script scene (or is a prefix of one), then cap at the
    #    script's scene count. If the trimmed set is empty (old pre-vo frames),
    #    fall back to the script scene count as a positional cap.
    _script_scenes = _scene_vo_lines(st)
    _script_vo_set = set(v.strip() for v in _script_scenes if v.strip())
    _script_n = len(_script_scenes)
    if _script_n > 0 and len(seq) > _script_n:
        # Filter: keep only frames whose VO matches a script scene.
        _clean = [f for f in seq
                  if (f.get("vo") or "").strip() in _script_vo_set]
        if len(_clean) >= _script_n:
            # Enough matching frames — use the cleaned set (capped at script_n
            # in case of duplicates from micro-cuts, though those split AFTER
            # assembly, not in the sequence itself).
            seq = _clean[:_script_n]
        else:
            # Not enough matching frames (old pre-vo-on-frame project) — cap
            # positionally at the script scene count.
            seq = seq[:_script_n]
        # Persist the cleaned sequence so the UI + resume see the same set.
        st["sequence"] = seq
        n = len(seq)
        print(f"[video] sanitized sequence: {len(st.get('sequence') or [])} "
              f"frames (was {len(seq) if _clean else n}, script has {_script_n} "
              f"scenes)", flush=True)

    # 1. ONE continuous narration track — synthesized from per-scene VO lines
    #    joined with double-newlines so that character timestamps map directly
    #    to scene boundaries.  trim_silence removes dead air.
    #
    #    Prefer the VO stored ON each rendered frame (rec["vo"]) over the script's
    #    positional scene list. If some frames failed to render (e.g. a transient
    #    image-API 502), the surviving sequence is shorter than the script, and a
    #    positional script->frame mapping would shift every line after the gap —
    #    desyncing narration from picture. Reading VO from the frame itself keeps
    #    each image paired with the exact words it was generated for.
    n = len(seq)
    frame_vos = [(seq[i].get("vo") or "").strip() for i in range(n)]
    # FINAL anti-double-speak guard. The narration track is built from the VO
    # carried on each frame. If two consecutive frames carry the SAME spoken line
    # — which happens when a scene was re-rendered, micro-cut, padded, or the
    # script model emitted a back-to-back repeat that slipped through earlier
    # dedup — the TTS would literally speak that line twice (the "same voice-over
    # repeated twice at the end" bug). Blank the duplicate's spoken line so it is
    # NOT narrated again, while KEEPING the frame on screen (its hold is computed
    # from the audio either way) so the picture count / pacing don't change and
    # A/V stays locked. Compared punctuation-insensitively so "He won." == "He won".
    def _vk(x):
        return re.sub(r"[^\w ]+", "", " ".join((x or "").lower().split())).strip()
    _prev_vk = None
    _silenced = 0
    for i in range(n):
        _cur_vk = _vk(frame_vos[i])
        if _cur_vk and _cur_vk == _prev_vk:
            frame_vos[i] = ""      # don't speak it twice; frame still shows
            _silenced += 1
        elif _cur_vk:
            _prev_vk = _cur_vk
        # an empty-VO frame does NOT reset _prev_vk — a repeat split by a silent
        # padded frame ("A", "", "A") must still be caught as a double.
    if _silenced:
        print(f"[video] muted {_silenced} duplicate consecutive VO line(s) so "
              f"narration isn't spoken twice (frames kept, A/V locked)", flush=True)
    if any(frame_vos):
        tts_lines = frame_vos
        lines = frame_vos
    else:
        # Older sequences (pre-vo-on-frame) fall back to the positional script.
        lines = _scene_vo_lines(st)
        tts_lines = [lines[i].strip() if i < len(lines) else "" for i in range(n)]
    tts_text = "\n\n".join(l for l in tts_lines if l) or text

    vc = get_voice_client(request, voice_id)
    provider = (request.state.settings.get("voice_provider")
                if request else "") or "elevenlabs"
    alignment = None
    measured_holds = None
    path = None

    # Providers without character-level timestamps (e.g. MiMo) can't give exact
    # per-scene timing from a single track. Synthesize each scene separately and
    # MEASURE its real length so every frame is held for exactly its narration —
    # far tighter A/V sync than estimating from word counts. ElevenLabs keeps the
    # single-track + timestamps path (its timestamps are already frame-exact).
    url = None
    _continuous_error = None  # capture the reason for loud logging / force mode
    if provider != "elevenlabs":
        # PREFERRED: one continuous narration track (natural prosody, no gaps),
        # with frame timing recovered from Whisper word timestamps. This is the
        # "visuals follow the voice" path — no per-scene chopping/padding, so the
        # voice never pauses between scenes and pronunciation stays natural.
        try:
            _cont = _synth_continuous_track(
                vc, tts_lines, name_hint,
                settings=(request.state.settings if request else None))
        except Exception as _ce:
            _continuous_error = str(_ce)
            print(f"[video] continuous-track path errored ({_ce})", flush=True)
            _cont = None
        if _cont:
            path, url, dur, measured_holds = _cont
            print(f"[video] continuous flow: {len(measured_holds)} frames over "
                  f"{dur:.2f}s (no inter-scene pauses)", flush=True)
        elif force_continuous:
            # User explicitly asked for continuous track (Build Video button).
            # Don't silently fall back to the choppy per-scene method — that's
            # the bug they're trying to fix. Tell them WHY it failed so they can
            # fix the root cause (Whisper missing, synth error, drift, etc.).
            _reason = _continuous_error or (
                "Whisper not available" if not transcribe.local_available()
                else "transcription/drift check failed")
            raise HTTPException(
                500,
                f"Continuous voice-over failed: {_reason}. "
                f"Install faster-whisper (pip install faster-whisper) or "
                f"switch to ElevenLabs voice in Settings.")
        else:
            # FALLBACK: per-scene synth (only when Whisper isn't installed). This
            # still syncs but pads each clip with trailing silence between scenes.
            try:
                path, url, dur, measured_holds = _synth_per_scene_track(
                    vc, tts_lines, name_hint)
                print(f"[video] per-scene exact sync: {len(measured_holds)} clips, "
                      f"{dur:.2f}s total (install faster-whisper for gapless flow)",
                      flush=True)
            except Exception as _ps:
                print(f"[video] per-scene synth failed ({_ps}); single-track fallback",
                      flush=True)
                measured_holds = None
                path = None

    if measured_holds is None:
        try:
            mp3, alignment = vc.synthesize_with_timestamps(tts_text)
        except Exception as e:
            raise HTTPException(500, f"voice-over failed: {e}")
        url, path = store.write_binary("audio", mp3, ext="mp3", name_hint=name_hint)
        try:
            editor.trim_silence(path)
        except Exception:
            pass
        try:
            dur = float(editor.probe_duration(path))
        except Exception:
            dur = 0.0
        if dur <= 0.1:
            dur = max(1.0, n * 2.0)

    # 2. Compute per-scene hold durations.
    #    (0) MANUAL override from the Review & adjust popup — user-set seconds
    #        per frame. Scaled to the actual audio duration so A/V stays locked
    #        while honouring the user's relative pacing.
    #    (a) ElevenLabs character timestamps — exact speech timing, best quality
    #    (b) Pre-planned holds from autopilot cut planner (Claude, pre-TTS)
    #    (c) Claude narration-sync estimate — linguistic proxy, decent
    #    (d) Word-count proportional weighting — simple baseline
    holds = None
    if manual_holds and len(manual_holds) == n:
        try:
            mh = [max(0.15, float(v)) for v in manual_holds]
            mh_sum = sum(mh) or 1.0
            holds = [round(v * dur / mh_sum, 3) for v in mh]
            print(f"[video] manual holds applied + scaled to {dur:.2f}s", flush=True)
        except Exception as _mh_ex:
            print(f"[video] manual holds ignored: {_mh_ex}", flush=True)
            holds = None
    # Per-scene measured holds (timestamp-less providers) are already frame-exact
    # and sum to the track duration — use them directly.
    if holds is None and measured_holds and len(measured_holds) == n:
        holds = measured_holds
        print(f"[video] per-scene measured holds for {n} frames (exact sync)",
              flush=True)
    if holds is None:
        holds = _holds_from_alignment(tts_lines, alignment, dur)
    if holds and len(holds) == n:
        print(f"[video] timestamp-sync: exact holds for {n} frames", flush=True)
    else:
        # Try pre-planned holds set by the autopilot cut planner.
        sc_list = (_unwrap_script(st.get("script") or {})).get("scenes") or []
        pre_planned = [sc_list[i].get("planned_hold") if i < len(sc_list) else None
                       for i in range(n)]
        if all(v is not None for v in pre_planned):
            # Scale pre-planned weights to actual audio duration.
            _pp_sum = sum(float(v) for v in pre_planned) or 1.0
            holds = [round(float(v) * dur / _pp_sum, 3) for v in pre_planned]
            print(f"[video] cut-planner holds scaled to {dur:.2f}s", flush=True)
        else:
            # Fallback: word-count proportional weighting
            weights = [float(max(1, _word_count(tts_lines[i]))) for i in range(n)]
            total_w = sum(weights) or float(n)
            holds = [max(0.6, dur * w / total_w) for w in weights]
            scale = dur / (sum(holds) or 1.0)
            holds = [round(h * scale, 3) for h in holds]
            # Hook zone: cap the first ~30s of frames to ≤1.0s for fast opening pacing.
            hook_budget = min(30.0, dur * 0.5)
            hook_cap = 1.0
            cum, stolen, hook_end_idx = 0.0, 0.0, 0
            for i in range(n):
                cum += holds[i]
                if cum > hook_budget:
                    hook_end_idx = i
                    break
                if holds[i] > hook_cap:
                    stolen += holds[i] - hook_cap
                    holds[i] = hook_cap
                hook_end_idx = i + 1
            rest = list(range(hook_end_idx, n))
            rest_total = sum(holds[j] for j in rest) or 1.0
            if stolen > 0 and rest:
                for j in rest:
                    holds[j] = round(holds[j] + stolen * (holds[j] / rest_total), 3)
            # Claude narration-sync as second fallback when timestamps unavailable.
            if smart_edit and request:
                try:
                    _claude = _claude_client_for(None, request)
                    refined = _claude.edit_holds(tts_lines, dur)
                    if refined and len(refined) == n:
                        holds = refined
                        print(f"[video] Claude smart-edit: holds refined for {n} frames",
                              flush=True)
                except Exception as _se:
                    print(f"[video] Claude smart-edit skipped: {_se}", flush=True)

    shots, scene_map = [], []
    # NOTE: do NOT blindly stretch holds to hit a target duration — the VO audio
    # is a fixed length, so stretching frames while audio stays put DESYNCS the
    # narration from the picture (the image runs ahead of the words). A/V sync is
    # sacred. Holds here are already locked to the narration timing. If the
    # finished video is shorter than the requested target, that's because the
    # script's narration is shorter than the target — the correct fix is more
    # narration (enforced in the script word-count target), not frame-stretching.
    # We keep target_seconds only for logging the gap so it's visible.
    if target_seconds and dur > 0.1:
        _tgt = float(target_seconds)
        if _tgt > dur * 1.10:
            print(f"[video] note: narration is {dur:.1f}s but target was "
                  f"{_tgt:.0f}s — video length follows the narration to keep "
                  f"A/V sync. Increase script length / words for a longer video.",
                  flush=True)
    for i in range(n):
        try:
            img_path = store.url_to_path(seq[i]["image_url"])
        except Exception:
            continue
        line = lines[i] if i < len(lines) else ""
        shots.append({"path": img_path, "duration": holds[i],
                      "note": (line[:60] if line else "")})
        scene_map.append({"index": seq[i].get("index", i + 1), "vo": line,
                          "hold_seconds": holds[i]})
    if not shots:
        raise HTTPException(400, "no readable frames in sequence")

    # 3. Micro-cut any frame that would sit too long.
    shots = editor.split_long_holds(shots, max_hold=max(1.5, float(max_hold)))

    # 3b. Sound design (ElevenLabs): a rumble bed for the music slot + a plan of
    #     contextual point-SFX to mix in after assembly.
    music_path, music_vol = _music_for(st, use_music)
    sfx_entries = []
    do_sound = bool(sound_design and request and
                    request.state.settings.get("elevenlabs_api_key"))
    if do_sound:
        try:
            rumble_path, sfx_entries = _auto_sound_design(request, scene_map, dur)
            if rumble_path and not music_path:   # use rumble as the ducked bed
                music_path, music_vol = rumble_path, 0.10
        except Exception as ex:
            print(f"[sound] design skipped: {ex}", flush=True)

    out_name = f"edit_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    try:
        editor.assemble_video(shots, path, out_path,
                              transition=(transition or "cut").lower(),
                              width=width, height=height, fps=fps, motion=motion,
                              music_path=music_path, music_volume=music_vol)
    except Exception as ex:
        raise HTTPException(500, f"video assembly failed: {ex}")

    # 3c. Mix the point-SFX onto the rendered video, then make it loud + crisp.
    if do_sound:
        if sfx_entries:
            try:
                editor.mix_sfx(out_path, [
                    {"path": store.url_to_path(s["url"]),
                     "at_seconds": s["at_seconds"], "volume": s["volume"]}
                    for s in sfx_entries
                    if os.path.exists(store.url_to_path(s["url"]))],
                    out_path + ".sfx.mp4")
                if os.path.exists(out_path + ".sfx.mp4"):
                    os.replace(out_path + ".sfx.mp4", out_path)
            except Exception as ex:
                print(f"[sound] sfx mix skipped: {ex}", flush=True)

    # 3d. Click on every frame change (scene boundaries, not micro-cuts).
    if cut_clicks:
        _apply_cut_clicks(request, out_path,
                          [sc["hold_seconds"] for sc in scene_map],
                          volume=cut_click_volume, style=cut_click_style)

    if do_sound:
        _normalize_loud(out_path)

    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    video_url = f"/data/{rel}"

    total = round(dur, 2)
    st["audio"] = {"id": store.new_id("audio"), "url": url,
                   "name": f"{name_hint}.mp3", "duration": total}
    st["voiceover"] = {"id": store.new_id("vo"), "url": url, "voice_id": voice_id,
                       "mode": "flow", "duration": total, "scenes": scene_map,
                       "created": store.now()}
    plan = {"mode": "voiceover_flow", "total_duration": total,
            "transition": (transition or "cut").lower(),
            "micro_cut_max_hold": max_hold, "shots": scene_map,
            "rendered_clips": len(shots),
            "sound_design": bool(do_sound), "sfx_count": len(sfx_entries),
            "cut_clicks": bool(cut_clicks)}
    edit_rec = {"id": store.new_id("edit"), "url": video_url,
                "audio_id": st["audio"]["id"],
                "transition": (transition or "cut").lower(),
                "plan": plan, "created": store.now()}
    st.setdefault("edits", []).append(edit_rec)
    store.save_state(st)
    store.log_usage("voice", 1, round(total * 0.0002, 4))
    store.log_usage("video", 1, 0.0)
    return edit_rec, video_url, total, scene_map


class FlowVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    text: str = ""
    transition: Optional[str] = None
    width: int = 1920
    height: int = 1080
    fps: int = 30
    max_hold: float = 2.5          # split frames longer than this into micro-cuts
    motion: bool = False
    music: bool = False
    sound_design: bool = False     # generate + mix ElevenLabs SFX, then loudnorm
    cut_clicks: bool = False       # ElevenLabs click on every frame change
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"  # click | camera | whoosh | pop | tick


@app.post("/api/voiceover/auto-flow")
def api_voiceover_auto_flow(body: FlowVoiceoverIn, request: Request):
    """Natural-flow narrated video: one continuous voice-over, frames timed to
    it, long holds broken into micro-cuts. See _build_flow_video."""
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    st = store.load_state()
    text = (body.text or "").strip() or _script_voiceover_text(st)
    voice_id = body.voice_id or _voice_default_id(s)
    edit_rec, video_url, total, scene_map = _build_flow_video(
        st, request, voice_id=voice_id, text=text,
        transition=body.transition or "cut", width=body.width,
        height=body.height, fps=body.fps, max_hold=body.max_hold,
        motion=body.motion, use_music=body.music, name_hint="voiceover_flow",
        sound_design=body.sound_design, cut_clicks=body.cut_clicks,
        cut_click_volume=body.cut_click_volume,
        cut_click_style=body.cut_click_style)
    return {"edit": edit_rec, "video_url": video_url, "total_duration": total,
            "scenes": scene_map, "plan": edit_rec["plan"]}


class VoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    text: Optional[str] = None       # override; defaults to the script voice-over


@app.post("/api/voiceover")
def api_voiceover(body: VoiceoverIn, request: Request):
    """Single-track mode: synthesize the whole voice-over into ONE audio file and
    drop it into the existing `audio` slot, so Plan edit / Render video work
    unchanged. Returns the audio record (same shape as /api/audio)."""
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    st = store.load_state()
    text = (body.text or "").strip() or _script_voiceover_text(st)
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script in tab 02 first.")
    voice_id = body.voice_id or _voice_default_id(s)
    try:
        mp3 = get_voice_client(request, voice_id).synthesize(text)
    except Exception as e:
        raise HTTPException(500, f"voice-over failed: {e}")
    url, path = store.write_binary("audio", mp3, ext="mp3", name_hint="voiceover")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    rec = {
        "id": store.new_id("audio"),
        "url": url,
        "name": "voiceover.mp3",
        "duration": dur,
    }
    st["audio"] = rec
    st["voiceover"] = {
        "id": store.new_id("vo"),
        "url": url, "voice_id": voice_id, "mode": "single",
        "duration": dur, "created": store.now(),
    }
    store.save_state(st)
    store.log_usage("voice", 1, round(max(dur, 1) * 0.0002, 4))
    return rec


class SceneVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    transition: str = "cut"          # cut | fade | crossfade | xfade name
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pad: float = 0.05                # tight gap after each clip for fast pacing
    motion: bool = False             # Ken Burns zoom
    music: bool = False              # mix the project's background music
    intro: Optional[str] = None      # intro title-card text
    outro: Optional[str] = None      # outro title-card text
    cut_clicks: bool = False         # ElevenLabs click on every frame change
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"


@app.post("/api/voiceover/scenes")
def api_voiceover_scenes(body: SceneVoiceoverIn, request: Request):
    """Per-scene mode: synthesize ONE clip per numbered scene, measure each
    clip's length, then build a video where sequence frame N is held for exactly
    the length of scene N's voice-over (+pad). Concatenates the clips into a
    single narration track and muxes it. Returns the edit record + per-scene map.
    """
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    st = store.load_state()
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    lines = _scene_vo_lines(st)
    if not any(lines):
        raise HTTPException(400, "No per-scene narration. Generate a script in tab 02 first.")

    voice_id = body.voice_id or _voice_default_id(s)
    client = get_voice_client(request, voice_id)

    # one frame per scene, capped to whichever is shorter
    n = min(len(seq), len(lines))
    if n == 0:
        raise HTTPException(400, "Nothing to voice.")

    work = os.path.join(store.VIDEOS_DIR, f"_vo_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    clip_paths = []          # audio clips, in order, to concat into one track
    shots = []               # for editor.assemble_video
    scene_map = []
    try:
        for i in range(n):
            line = lines[i] or ""
            try:
                mp3 = client.synthesize(line)
            except Exception as e:
                raise HTTPException(500, f"voice-over failed on scene {i+1}: {e}")
            ap = os.path.join(work, f"vo_{i:03d}.mp3")
            with open(ap, "wb") as f:
                f.write(mp3)
            try:
                editor.trim_silence(ap)
            except Exception:
                pass
            try:
                d = editor.probe_duration(ap)
            except Exception:
                d = 0.0
            if d <= 0.05:
                d = 0.8
            hold = round(d + max(0.0, body.pad), 3)
            clip_paths.append((ap, hold, d))
            try:
                img_path = store.url_to_path(seq[i]["image_url"])
            except Exception:
                continue
            shots.append({"path": img_path, "duration": hold,
                          "note": (line[:60] if line else "")})
            scene_map.append({"index": seq[i].get("index", i + 1),
                              "vo": line, "audio_seconds": round(d, 2),
                              "hold_seconds": hold})
        if not shots:
            raise HTTPException(400, "No valid frames matched the scenes.")

        # 1. concat the per-scene clips into one narration track (pad each clip
        #    with trailing silence to its hold length so audio lines up with the
        #    image timing exactly).
        track = os.path.join(work, "voiceover_track.mp3")
        concat_inputs, filt = [], ""
        for j, (ap, hold, _d) in enumerate(clip_paths):
            concat_inputs += ["-i", ap]
            filt += (f"[{j}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                     f"apad=whole_dur={hold}[a{j}];")
        filt += "".join(f"[a{j}]" for j in range(len(clip_paths)))
        filt += f"concat=n={len(clip_paths)}:v=0:a=1[out]"
        cc = _run_capture(
            ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
             "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track])
        track_path = track if (cc.returncode == 0 and os.path.exists(track)) else None
        track_url = None
        if track_path:
            with open(track_path, "rb") as f:
                track_url, _tp = store.write_binary(
                    "audio", f.read(), ext="mp3", name_hint="voiceover_scenes")

        # 2. assemble the timed video with the narration track muxed in.
        out_name = f"edit_{int(time.time())}.mp4"
        out_path = os.path.join(store.VIDEOS_DIR, out_name)
        music_path, music_volume = _music_for(st, body.music)
        editor.assemble_video(
            shots, track_path, out_path,
            transition=(body.transition or "cut").lower(),
            width=body.width, height=body.height, fps=body.fps,
            motion=body.motion, music_path=music_path, music_volume=music_volume,
        )
        if body.cut_clicks:
            _apply_cut_clicks(request, out_path,
                              [sh["duration"] for sh in shots],
                              volume=body.cut_click_volume,
                              style=body.cut_click_style)
        _bookend(out_path, body.intro, body.outro, body.width, body.height, body.fps)
        rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
        url = f"/data/{rel}"

        total = round(sum(h for _a, h, _d in clip_paths), 2)
        if track_url:
            st["voiceover"] = {
                "id": store.new_id("vo"), "url": track_url, "voice_id": voice_id,
                "mode": "scenes", "duration": total, "scenes": scene_map,
                "created": store.now(),
            }
        rec = {
            "id": store.new_id("edit"), "url": url,
            "audio_id": (st.get("voiceover") or {}).get("id"),
            "transition": (body.transition or "cut").lower(),
            "plan": {"mode": "voiceover_scenes", "total_duration": total,
                     "shots": scene_map},
            "created": store.now(),
        }
        st.setdefault("edits", []).append(rec)
        store.save_state(st)
        _fire_webhook(request.state.settings, "video.rendered",
                      {"url": rec["url"], "total_duration": total})
        store.log_usage("voice", n, round(total * 0.0002, 4))
        store.log_usage("video", 1, 0.0)
        return {"edit": rec, "scenes": scene_map, "total_duration": total,
                "voiceover_url": track_url}
    finally:
        shutil.rmtree(work, ignore_errors=True)


class AutoVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    text: Optional[str] = None        # override; defaults to the script voice-over
    user_brief: str = ""              # extra edit direction for Claude
    transition: Optional[str] = None  # override Claude's suggested transition
    width: int = 1920
    height: int = 1080
    fps: int = 30
    cut_clicks: bool = False
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"


@app.post("/api/voiceover/auto")
def api_voiceover_auto(body: AutoVoiceoverIn, request: Request):
    """One click: ElevenLabs voices the whole script -> Claude LOOKS at every
    frame + that audio's length + your brief and writes the edit decision list
    -> ffmpeg assembles the synced MP4. This is the 'Claude analyses both the
    mp3 and the frames and makes a video matching the voice-over' path, end to
    end. Returns {audio, plan, edit}."""
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    st = store.load_state()
    if not st.get("sequence"):
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    text = (body.text or "").strip() or _script_voiceover_text(st)
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script in tab 02 first.")

    # 1. ElevenLabs: script text -> one narration track, into the audio slot.
    voice_id = body.voice_id or _voice_default_id(s)
    try:
        mp3 = get_voice_client(request, voice_id).synthesize(text)
    except Exception as e:
        raise HTTPException(500, f"voice-over failed: {e}")
    url, path = store.write_binary("audio", mp3, ext="mp3", name_hint="voiceover")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    audio_rec = {"id": store.new_id("audio"), "url": url,
                 "name": "voiceover.mp3", "duration": dur}
    st["audio"] = audio_rec
    st["voiceover"] = {"id": store.new_id("vo"), "url": url, "voice_id": voice_id,
                       "mode": "single", "duration": dur, "created": store.now()}
    store.save_state(st)

    # 2. Claude vision: look at the frames + the audio length -> EDL.
    frames = []
    for sh in st["sequence"]:
        try:
            frames.append(pipeline.downsize_for_vision(store.read_image(sh["image_url"])))
        except Exception:
            pass
    if not frames:
        raise HTTPException(400, "no readable frames in sequence")
    try:
        plan = get_claude_client(request).plan_edit(
            frames=frames,                       # all frames; client chunks in 18s
            audio_duration=float(dur) or 0,
            user_brief=body.user_brief,
            master_prompt=st["master_prompt"],
            vo_lines=_scene_vo_lines(st),        # match imagery to the words said
        )
    except Exception as ex:
        raise HTTPException(500, f"edit planning failed: {ex}")
    if body.transition:
        plan["transition"] = body.transition

    # 3. ffmpeg: assemble the synced video from the EDL + the narration track.
    seq = st["sequence"]
    shots_out = []
    for sh in (plan.get("shots") or []):
        idx = int(sh.get("index", 0))
        if idx < 1 or idx > len(seq):
            continue
        try:
            p = store.url_to_path(seq[idx - 1]["image_url"])
        except Exception:
            continue
        shots_out.append({"path": p, "duration": float(sh.get("duration") or 1.0),
                          "note": sh.get("note", "")})
    if not shots_out:
        raise HTTPException(400, "Claude returned no valid shots to assemble")

    out_name = f"edit_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    transition = (body.transition or plan.get("transition") or "cut").lower()
    try:
        editor.assemble_video(shots_out, path, out_path, transition=transition,
                              width=body.width, height=body.height, fps=body.fps)
        if body.cut_clicks:
            _apply_cut_clicks(request, out_path,
                              [sh["duration"] for sh in shots_out],
                              volume=body.cut_click_volume,
                              style=body.cut_click_style)
    except Exception as ex:
        raise HTTPException(500, f"video assembly failed: {ex}")
    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    edit_rec = {
        "id": store.new_id("edit"), "url": f"/data/{rel}",
        "audio_id": audio_rec["id"], "transition": transition,
        "plan": plan, "created": store.now(),
    }
    st.setdefault("edits", []).append(edit_rec)
    store.save_state(st)
    store.log_usage("voice", 1, round(max(dur, 1) * 0.0002, 4))
    store.log_usage("script", 1, 0.01)
    store.log_usage("video", 1, 0.0)
    return {"audio": audio_rec, "plan": plan, "edit": edit_rec}


class SyncedVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    user_brief: str = ""              # edit direction for Claude (order/drops)
    transition: Optional[str] = None  # override Claude's suggested transition
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pad: float = 0.05                 # tight gap after each clip for fast pacing
    cut_clicks: bool = False
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"


@app.post("/api/voiceover/auto-synced")
def api_voiceover_auto_synced(body: SyncedVoiceoverIn, request: Request):
    """Button B — scene-locked Claude edit.

    Voices each scene, measures each clip so every frame's on-screen time is
    LOCKED to its own narration length (exact A/V sync, same engine as
    /api/voiceover/scenes). Claude then looks at all frames and only chooses the
    ORDER they play and which to DROP — it cannot change a duration. The
    narration track is rebuilt in Claude's chosen order so audio still lines up.
    """
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    st = store.load_state()
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    lines = _scene_vo_lines(st)
    if not any(lines):
        raise HTTPException(400, "No per-scene narration. Generate a script in tab 02 first.")

    voice_id = body.voice_id or _voice_default_id(s)
    client = get_voice_client(request, voice_id)
    n = min(len(seq), len(lines))
    if n == 0:
        raise HTTPException(400, "Nothing to voice.")

    work = os.path.join(store.VIDEOS_DIR, f"_vos_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    try:
        # 1. Voice + measure each scene -> locked hold + per-scene audio clip.
        clips = []        # per global 1-based index: {audio, hold, d, img, vo}
        for i in range(n):
            line = lines[i] or ""
            try:
                mp3 = client.synthesize(line)
            except Exception as e:
                raise HTTPException(500, f"voice-over failed on scene {i+1}: {e}")
            ap = os.path.join(work, f"vo_{i:03d}.mp3")
            with open(ap, "wb") as f:
                f.write(mp3)
            try:
                editor.trim_silence(ap)
            except Exception:
                pass
            try:
                d = editor.probe_duration(ap)
            except Exception:
                d = 0.0
            if d <= 0.05:
                d = 0.8
            hold = round(d + max(0.0, body.pad), 3)
            try:
                img_path = store.url_to_path(seq[i]["image_url"])
            except Exception:
                continue
            clips.append({"index": i + 1, "audio": ap, "hold": hold,
                          "d": round(d, 2), "img": img_path, "vo": line})
        if not clips:
            raise HTTPException(400, "No valid frames matched the scenes.")

        # 2. Claude: choose play order + drops (durations stay locked).
        #    Keep frames and clips index-aligned: only clips whose frame reads
        #    successfully are considered, so the global index 1..N the model
        #    sees maps exactly onto `clips`/`scene_durations`.
        frames_for_vision, kept = [], []
        for c in clips:
            try:
                frames_for_vision.append(pipeline.downsize_for_vision(
                    store.read_image(seq[c["index"] - 1]["image_url"])))
                kept.append(c)
            except Exception:
                pass
        if not kept:
            raise HTTPException(400, "no readable frames in sequence")
        clips = kept
        # Re-number to a dense 1..len(clips) so vision indices line up.
        for new_i, c in enumerate(clips, start=1):
            c["g"] = new_i
        by_index = {c["g"]: c for c in clips}
        scene_durations = [c["hold"] for c in clips]
        try:
            decision = get_claude_client(request).plan_edit_within_budget(
                frames=frames_for_vision,
                scene_durations=scene_durations,
                user_brief=body.user_brief,
                master_prompt=st["master_prompt"],
            )
        except Exception as ex:
            raise HTTPException(500, f"edit planning failed: {ex}")

        # Claude's order/notes are keyed by the dense vision index (c["g"]).
        order = [x for x in (decision.get("order") or []) if x in by_index]
        if not order:                      # fallback: keep all in script order
            order = [c["g"] for c in clips]
        notes = decision.get("notes") or {}
        transition = (body.transition or decision.get("transition") or "cut").lower()

        # 3. Rebuild the narration track in CLAUDE'S order (dropped scenes drop
        #    their audio too), padding each clip to its locked hold so the audio
        #    stays aligned to the reordered visuals.
        ordered = [by_index[x] for x in order]
        track = os.path.join(work, "voiceover_track.mp3")
        concat_inputs, filt = [], ""
        for j, c in enumerate(ordered):
            concat_inputs += ["-i", c["audio"]]
            filt += f"[{j}:a]apad=whole_dur={c['hold']}[a{j}];"
        filt += "".join(f"[a{j}]" for j in range(len(ordered)))
        filt += f"concat=n={len(ordered)}:v=0:a=1[out]"
        cc = _run_capture(
            ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
             "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track])
        track_path = track if (cc.returncode == 0 and os.path.exists(track)) else None
        track_url = None
        if track_path:
            with open(track_path, "rb") as f:
                track_url, _tp = store.write_binary(
                    "audio", f.read(), ext="mp3", name_hint="voiceover_synced")

        # 4. Assemble video in the same order, each shot held for its locked hold.
        shots, scene_map = [], []
        for c in ordered:
            note = notes.get(c["g"]) or notes.get(str(c["g"])) or ""
            shots.append({"path": c["img"], "duration": c["hold"],
                          "note": (note or c["vo"][:60])})
            scene_map.append({"index": c["index"], "vo": c["vo"],
                              "audio_seconds": c["d"], "hold_seconds": c["hold"],
                              "note": note})

        out_name = f"edit_{int(time.time())}.mp4"
        out_path = os.path.join(store.VIDEOS_DIR, out_name)
        try:
            editor.assemble_video(shots, track_path, out_path, transition=transition,
                                  width=body.width, height=body.height, fps=body.fps)
            if body.cut_clicks:
                _apply_cut_clicks(request, out_path,
                                  [sh["duration"] for sh in shots],
                                  volume=body.cut_click_volume,
                                  style=body.cut_click_style)
        except Exception as ex:
            raise HTTPException(500, f"video assembly failed: {ex}")
        rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
        url = f"/data/{rel}"

        total = round(sum(c["hold"] for c in ordered), 2)
        if track_url:
            st["voiceover"] = {
                "id": store.new_id("vo"), "url": track_url, "voice_id": voice_id,
                "mode": "synced", "duration": total, "scenes": scene_map,
                "created": store.now(),
            }
        kept_g = set(order)
        plan = {"mode": "voiceover_synced", "total_duration": total,
                "transition": transition,
                "order": [by_index[g]["index"] for g in order],
                "dropped": [c["index"] for c in clips if c["g"] not in kept_g],
                "rationale": decision.get("rationale", ""),
                "frames_seen": decision.get("frames_seen", len(clips)),
                "shots": scene_map}
        rec = {
            "id": store.new_id("edit"), "url": url,
            "audio_id": (st.get("voiceover") or {}).get("id"),
            "transition": transition, "plan": plan, "created": store.now(),
        }
        st.setdefault("edits", []).append(rec)
        store.save_state(st)
        store.log_usage("voice", n, round(total * 0.0002, 4))
        store.log_usage("script", 1, 0.01)
        store.log_usage("video", 1, 0.0)
        return {"edit": rec, "plan": plan, "scenes": scene_map,
                "total_duration": total, "voiceover_url": track_url}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Multi-voice: per-character voice mapping + dialogue synthesis
# --------------------------------------------------------------------------- #
class VoiceMapIn(BaseModel):
    voice_map: dict   # {character_name: voice_id}

@app.post("/api/voice-map")
def api_voice_map(body: VoiceMapIn):
    st = store.load_state()
    st["voice_map"] = body.voice_map or {}
    store.save_state(st)
    return {"ok": True, "voice_map": st["voice_map"]}


@app.get("/api/voice-map")
def api_voice_map_get():
    st = store.load_state()
    return {"voice_map": st.get("voice_map") or {}}


def _parse_dialogue_lines(text):
    """Parse speaker-tagged lines from VO text.

    Supported formats:
      [NARRATOR]: Some text here...
      [CHARACTER NAME]: Dialogue here...
      Untagged lines -> speaker=None (uses default voice)

    Returns [(speaker_or_None, text), ...]
    """
    segments = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^\[([^\]]+)\]\s*:\s*(.+)$', line)
        if m:
            segments.append((m.group(1).strip(), m.group(2).strip()))
        else:
            segments.append((None, line))
    return segments


class MultiVoiceIn(BaseModel):
    voice_map: Optional[dict] = None  # {character_name: voice_id}; uses saved if omitted
    default_voice_id: Optional[str] = None


@app.post("/api/voiceover/multivoice")
def api_voiceover_multivoice(body: MultiVoiceIn, request: Request):
    """Multi-voice mode: parse speaker tags from the script VO, synthesize each
    segment with the character's assigned voice, concatenate into one track."""
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    st = store.load_state()
    text = _script_voiceover_text(st)
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script first.")

    vm = body.voice_map or st.get("voice_map") or {}
    default_vid = body.default_voice_id or _voice_default_id(s)

    segments = _parse_dialogue_lines(text)
    if not segments:
        raise HTTPException(400, "No dialogue segments found in voice-over text.")

    work = os.path.join(store.AUDIO_DIR, f"_mv_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    try:
        clip_paths = []
        speakers_used = set()
        for i, (speaker, seg_text) in enumerate(segments):
            vid = vm.get(speaker, default_vid) if speaker else default_vid
            speakers_used.add(speaker or "NARRATOR")
            try:
                mp3 = get_voice_client(request, vid).synthesize(seg_text)
            except Exception as e:
                raise HTTPException(500, f"TTS failed for segment {i+1} ({speaker or 'narrator'}): {e}")
            cp = os.path.join(work, f"seg_{i:04d}.mp3")
            with open(cp, "wb") as f:
                f.write(mp3)
            clip_paths.append(cp)

        if not clip_paths:
            raise HTTPException(400, "No segments synthesized.")

        # Concatenate all segments into one track
        track = os.path.join(work, "multivoice_track.mp3")
        if len(clip_paths) == 1:
            shutil.copy2(clip_paths[0], track)
        else:
            concat_inputs = []
            filt_parts = []
            for j, cp in enumerate(clip_paths):
                concat_inputs += ["-i", cp]
                filt_parts.append(f"[{j}:a]")
            filt = "".join(filt_parts) + f"concat=n={len(clip_paths)}:v=0:a=1[out]"
            cc = _run_capture(
                ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
                 "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track])
            if cc.returncode != 0:
                raise HTTPException(500, f"audio concat failed: {cc.stderr[-300:]}")

        with open(track, "rb") as f:
            track_data = f.read()
        url, path = store.write_binary("audio", track_data, ext="mp3", name_hint="multivoice")
        try:
            dur = editor.probe_duration(path)
        except Exception:
            dur = 0

        rec = {"id": store.new_id("audio"), "url": url,
               "name": "multivoice.mp3", "duration": dur}
        st["audio"] = rec
        st["voiceover"] = {
            "id": store.new_id("vo"), "url": url,
            "mode": "multivoice", "duration": dur,
            "speakers": list(speakers_used),
            "voice_map": vm, "created": store.now(),
        }
        if body.voice_map:
            st["voice_map"] = vm
        store.save_state(st)
        store.log_usage("voice", len(clip_paths), round(max(dur, 1) * 0.0002, 4))
        return {"audio": rec, "speakers": list(speakers_used),
                "segments": len(clip_paths), "duration": dur}
    finally:
        shutil.rmtree(work, ignore_errors=True)


class MultiVoiceScenesIn(BaseModel):
    voice_map: Optional[dict] = None
    default_voice_id: Optional[str] = None
    transition: str = "cut"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pad: float = 0.05
    motion: bool = False
    music: bool = False


@app.post("/api/voiceover/multivoice/scenes")
def api_voiceover_multivoice_scenes(body: MultiVoiceScenesIn, request: Request):
    """Per-scene multi-voice: each scene's VO is parsed for speaker tags and
    synthesized with per-character voices, then assembled into a timed video."""
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key set — add ElevenLabs or MiMo in Settings.")
    st = store.load_state()
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    lines = _scene_vo_lines(st)
    if not any(lines):
        raise HTTPException(400, "No per-scene narration. Generate a script first.")

    vm = body.voice_map or st.get("voice_map") or {}
    default_vid = body.default_voice_id or _voice_default_id(s)
    n = min(len(seq), len(lines))

    work = os.path.join(store.VIDEOS_DIR, f"_mv_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    try:
        clip_paths = []
        shots = []
        scene_map = []
        speakers_used = set()

        for i in range(n):
            line = lines[i] or ""
            segments = _parse_dialogue_lines(line)
            scene_clips = []
            for j, (speaker, seg_text) in enumerate(segments):
                vid = vm.get(speaker, default_vid) if speaker else default_vid
                speakers_used.add(speaker or "NARRATOR")
                try:
                    mp3 = get_voice_client(request, vid).synthesize(seg_text)
                except Exception as e:
                    raise HTTPException(500, f"TTS failed scene {i+1} seg {j+1}: {e}")
                cp = os.path.join(work, f"sc{i:03d}_seg{j:02d}.mp3")
                with open(cp, "wb") as f:
                    f.write(mp3)
                scene_clips.append(cp)

            if not scene_clips:
                scene_clips = []
                d = 0.8
            elif len(scene_clips) == 1:
                d = 0
                try:
                    d = editor.probe_duration(scene_clips[0])
                except Exception:
                    d = 0.8
            else:
                # Concat scene segments into one per-scene clip
                merged = os.path.join(work, f"sc{i:03d}.mp3")
                ci = []
                fp = []
                for k, cp in enumerate(scene_clips):
                    ci += ["-i", cp]
                    fp.append(f"[{k}:a]")
                filt = "".join(fp) + f"concat=n={len(scene_clips)}:v=0:a=1[out]"
                cc = _run_capture(
                    ["ffmpeg", "-y", *ci, "-filter_complex", filt,
                     "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", merged])
                if cc.returncode == 0:
                    scene_clips = [merged]
                    try:
                        d = editor.probe_duration(merged)
                    except Exception:
                        d = 0.8
                else:
                    d = 0.8

            if d <= 0.05:
                d = 0.8
            hold = round(d + max(0.0, body.pad), 3)
            if scene_clips:
                clip_paths.append((scene_clips[0], hold, d))
            try:
                img_path = store.url_to_path(seq[i]["image_url"])
            except Exception:
                continue
            shots.append({"path": img_path, "duration": hold,
                          "note": (line[:60] if line else "")})
            scene_map.append({"index": seq[i].get("index", i + 1),
                              "vo": line, "audio_seconds": round(d, 2),
                              "hold_seconds": hold})

        if not shots:
            raise HTTPException(400, "No valid frames.")

        # Concat all scene clips into one narration track
        track = os.path.join(work, "multivoice_track.mp3")
        concat_inputs, filt = [], ""
        for j, (ap, hold, _d) in enumerate(clip_paths):
            concat_inputs += ["-i", ap]
            filt += (f"[{j}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                     f"apad=whole_dur={hold}[a{j}];")
        filt += "".join(f"[a{j}]" for j in range(len(clip_paths)))
        filt += f"concat=n={len(clip_paths)}:v=0:a=1[out]"
        cc = _run_capture(
            ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
             "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track])
        track_path = track if (cc.returncode == 0 and os.path.exists(track)) else None
        track_url = None
        if track_path:
            with open(track_path, "rb") as f:
                track_url, _tp = store.write_binary(
                    "audio", f.read(), ext="mp3", name_hint="multivoice_scenes")

        out_name = f"edit_{int(time.time())}.mp4"
        out_path = os.path.join(store.VIDEOS_DIR, out_name)
        music_path, music_volume = _music_for(st, body.music)
        editor.assemble_video(
            shots, track_path, out_path,
            transition=(body.transition or "cut").lower(),
            width=body.width, height=body.height, fps=body.fps,
            motion=body.motion, music_path=music_path, music_volume=music_volume,
        )
        rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
        url = f"/data/{rel}"
        total = round(sum(h for _a, h, _d in clip_paths), 2)

        if track_url:
            st["voiceover"] = {
                "id": store.new_id("vo"), "url": track_url,
                "mode": "multivoice_scenes", "duration": total,
                "speakers": list(speakers_used), "voice_map": vm,
                "scenes": scene_map, "created": store.now(),
            }
        rec = {
            "id": store.new_id("edit"), "url": url,
            "audio_id": (st.get("voiceover") or {}).get("id"),
            "transition": (body.transition or "cut").lower(),
            "plan": {"mode": "multivoice_scenes", "total_duration": total,
                     "shots": scene_map},
            "created": store.now(),
        }
        st.setdefault("edits", []).append(rec)
        if body.voice_map:
            st["voice_map"] = vm
        store.save_state(st)
        store.log_usage("voice", n, round(total * 0.0002, 4))
        store.log_usage("video", 1, 0.0)
        return {"edit": rec, "scenes": scene_map, "total_duration": total,
                "voiceover_url": track_url, "speakers": list(speakers_used)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Claude: plan an edit  +  ffmpeg: assemble the video
# --------------------------------------------------------------------------- #
class EditPlanIn(BaseModel):
    user_brief: str = ""
    transition: Optional[str] = None     # override Claude's suggestion


@app.post("/api/edit-plan")
def api_edit_plan(e: EditPlanIn, request: Request):
    st = store.load_state()
    if not st["sequence"]:
        raise HTTPException(400, "sequence is empty — render some frames first")
    if not st.get("audio"):
        raise HTTPException(400, "upload an audio file first")
    frames = []
    for s in st["sequence"]:
        try:
            frames.append(pipeline.downsize_for_vision(store.read_image(s["image_url"])))
        except Exception:
            pass
    if not frames:
        raise HTTPException(400, "no readable frames in sequence")
    # Anthropic caps a request at ~20 images, so the client batches the full
    # sequence in groups of 18 and merges the per-batch decisions — Claude now
    # sees every frame instead of only the first 20.
    try:
        plan = get_claude_client(request).plan_edit(
            frames=frames,                       # all frames; client chunks in 18s
            audio_duration=float((st.get("audio") or {}).get("duration") or 0),
            user_brief=e.user_brief,
            master_prompt=st["master_prompt"],
            vo_lines=_scene_vo_lines(st),        # match imagery to the words said
        )
    except Exception as ex:
        raise HTTPException(500, f"edit planning failed: {ex}")
    if e.transition:
        plan["transition"] = e.transition
    # Deterministic post-process: never trust the LLM's arithmetic. Force the
    # shot hold-times to sum EXACTLY to the real audio duration so the cut stays
    # locked to the audio. Scale proportionally, clamp to a 0.2s floor, then
    # absorb any rounding residue into the final shot.
    audio_dur = float((st.get("audio") or {}).get("duration") or 0.0)
    shots = plan.get("shots") or []
    if audio_dur > 0 and shots:
        cur = sum(max(0.0, float(s.get("duration") or 0.0)) for s in shots)
        if cur > 0:
            k = audio_dur / cur
            for s in shots:
                s["duration"] = round(max(0.2, float(s.get("duration") or 0.0) * k), 3)
        else:
            even = round(audio_dur / len(shots), 3)
            for s in shots:
                s["duration"] = even
        drift = round(audio_dur - sum(float(s["duration"]) for s in shots), 3)
        if abs(drift) >= 0.01:
            shots[-1]["duration"] = round(max(0.2, float(shots[-1]["duration"]) + drift), 3)
        plan["total_duration"] = round(audio_dur, 2)
    return plan


class RenderVideoIn(BaseModel):
    plan: dict
    transition: Optional[str] = None
    width: int = 1920
    height: int = 1080
    fps: int = 30
    motion: bool = False
    music: bool = False
    intro: Optional[str] = None
    outro: Optional[str] = None
    cut_clicks: bool = False
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"


@app.post("/api/render-video")
def api_render_video(r: RenderVideoIn, request: Request):
    st = store.load_state()
    if not st["sequence"]:
        raise HTTPException(400, "no sequence")
    seq = st["sequence"]
    shots_in = (r.plan or {}).get("shots") or []
    if not shots_in:
        raise HTTPException(400, "plan.shots is empty")
    audio_path = None
    if st.get("audio"):
        try:
            audio_path = store.url_to_path(st["audio"]["url"])
        except Exception:
            audio_path = None

    shots_out = []
    for sh in shots_in:
        idx = int(sh.get("index", 0))
        if idx < 1 or idx > len(seq):
            continue
        try:
            path = store.url_to_path(seq[idx - 1]["image_url"])
        except Exception:
            continue
        shots_out.append({
            "path": path,
            "duration": float(sh.get("duration") or 1.0),
            "note": sh.get("note", ""),
        })
    if not shots_out:
        raise HTTPException(400, "no valid shots after resolving indices")

    out_name = f"edit_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    transition = (r.transition or (r.plan or {}).get("transition") or "cut").lower()
    music_path, music_volume = _music_for(st, r.music)
    try:
        editor.assemble_video(
            shots_out, audio_path, out_path,
            transition=transition,
            width=r.width, height=r.height, fps=r.fps,
            motion=r.motion, music_path=music_path, music_volume=music_volume,
        )
        # clicks before bookending so cut times aren't shifted by the intro card
        if r.cut_clicks:
            _apply_cut_clicks(request, out_path,
                              [sh["duration"] for sh in shots_out],
                              volume=r.cut_click_volume, style=r.cut_click_style)
        _bookend(out_path, r.intro, r.outro, r.width, r.height, r.fps)
        _apply_sfx(st, out_path)
    except Exception as ex:
        raise HTTPException(500, f"video assembly failed: {ex}")

    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    url = f"/data/{rel}"

    rec = {
        "id": store.new_id("edit"),
        "url": url,
        "audio_id": (st.get("audio") or {}).get("id"),
        "transition": transition,
        "plan": r.plan,
        "created": store.now(),
    }
    st.setdefault("edits", []).append(rec)
    store.save_state(st)
    store.log_usage("video", 1, 0.0)
    _fire_webhook(request.state.settings, "video.rendered",
                  {"url": rec["url"], "transition": transition})
    return rec


def _apply_sfx(st, video_path):
    sfx_entries = st.get("sfx") or []
    if not sfx_entries:
        return
    sfx_list = []
    for s in sfx_entries:
        try:
            p = store.url_to_path(s["url"])
            if os.path.exists(p):
                sfx_list.append({
                    "path": p,
                    "at_seconds": s.get("at_seconds", 0),
                    "volume": s.get("volume", 0.8),
                })
        except Exception:
            pass
    if sfx_list:
        tmp = video_path + ".sfx.mp4"
        editor.mix_sfx(video_path, sfx_list, tmp)
        os.replace(tmp, video_path)


@app.delete("/api/edits/{eid}")
def api_delete_edit(eid: str):
    st = store.load_state()
    st["edits"] = [e for e in st.get("edits", []) if e["id"] != eid]
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Export — bundle the whole project into a single downloadable ZIP
# --------------------------------------------------------------------------- #
def _safe_name(s: str, fallback: str = "item") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip()).strip("_")
    return s[:60] or fallback


def _fire_webhook(settings, event, payload):
    """Best-effort POST to the user's webhook URL. Never raises."""
    url = (settings or {}).get("webhook_url", "")
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"event": event, "payload": payload,
                                 "ts": store.now()}, timeout=8)
    except Exception as e:
        print(f"[webhook] {event} failed: {e}", flush=True)


class WebhookTestIn(BaseModel):
    pass


@app.post("/api/webhook/test")
def api_webhook_test(request: Request):
    s = request.state.settings
    if not s.get("webhook_url"):
        raise HTTPException(400, "Set a webhook URL in Settings first.")
    try:
        import requests
        r = requests.post(s["webhook_url"], json={
            "event": "test", "payload": {"message": "Continuity Studio webhook OK"},
            "ts": store.now()}, timeout=10)
        return {"ok": r.status_code < 400, "status": r.status_code,
                "body": (r.text or "")[:200]}
    except Exception as e:
        raise HTTPException(500, f"webhook failed: {e}")


def _seo_text(seo: dict, title: str) -> str:
    """Render a saved SEO dict into a copy-paste-ready YouTube upload sheet."""
    titles = seo.get("titles") or ([seo["title"]] if seo.get("title") else [])
    tags = seo.get("tags") or []
    hashtags = seo.get("hashtags") or [t for t in tags if t][:5]
    lines = ["# YouTube upload sheet", ""]
    lines.append("## Title options")
    lines += [f"- {t}" for t in titles] or [f"- {title}"]
    lines += ["", "## Description", (seo.get("description") or "").strip(), ""]
    if tags:
        lines += ["## Tags (comma-separated)", ", ".join(tags), ""]
    if hashtags:
        hs = " ".join(h if str(h).startswith("#") else f"#{h}" for h in hashtags)
        lines += ["## Hashtags", hs, ""]
    return "\n".join(lines).strip() + "\n"


@app.get("/api/export/package")
def api_export_package(script: bool = True, prompts: bool = True,
                       characters: bool = True, frames: bool = True,
                       voiceover: bool = True, video: bool = True,
                       seo: bool = True, thumbnails: bool = True):
    """Bundle the project into one ZIP. Query flags select what to include
    (selective export): script, prompts, characters, frames, voiceover, video, seo."""
    st = store.load_state()
    scr = st.get("script") or {}
    title = (scr.get("title") or "").strip() or "continuity-project"
    root = _safe_name(title, "continuity-project")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if script and scr:
            z.writestr(f"{root}/script.json",
                       json.dumps(scr, indent=2, ensure_ascii=False))

        if seo and st.get("seo"):
            z.writestr(f"{root}/youtube_seo.txt", _seo_text(st["seo"], title))
            z.writestr(f"{root}/youtube_seo.json",
                       json.dumps(st["seo"], indent=2, ensure_ascii=False))

        if voiceover:
            vo = (scr.get("voiceover") or "").strip()
            if not vo:
                vo = "\n\n".join((sc.get("vo") or "").strip()
                                 for sc in (scr.get("scenes") or [])
                                 if (sc.get("vo") or "").strip())
            if vo:
                z.writestr(f"{root}/voiceover.txt", vo)
            vo_rec = st.get("voiceover") or {}
            vo_audio_url = vo_rec.get("url") or (st.get("audio") or {}).get("url")
            if vo_audio_url:
                try:
                    with open(store.url_to_path(vo_audio_url), "rb") as fh:
                        z.writestr(f"{root}/voiceover.mp3", fh.read())
                except Exception:
                    pass

        if prompts:
            char_blocks = []
            for c in (scr.get("characters") or []):
                name = (c.get("name") or "").strip()
                sheet = (c.get("sheet_prompt") or c.get("description") or "").strip()
                if name:
                    char_blocks.append(f"{name}\n{sheet}".strip())
            if char_blocks:
                z.writestr(f"{root}/character_prompts.txt", "\n\n".join(char_blocks))
            scene_prompts = [(sc.get("prompt") or "").strip()
                             for sc in (scr.get("scenes") or [])
                             if (sc.get("prompt") or "").strip()]
            if scene_prompts:
                z.writestr(f"{root}/scene_prompts.txt", "\n".join(scene_prompts))
            if (st.get("master_prompt") or "").strip():
                z.writestr(f"{root}/master_prompt.txt", st["master_prompt"].strip())

        if characters:
            used = {}
            for c in st.get("characters", []):
                try:
                    data = store.read_image(c["sheet_url"])
                except Exception:
                    continue
                ext = os.path.splitext(c["sheet_url"])[1].lstrip(".") or "png"
                base = _safe_name(c.get("name") or "character", "character")
                used[base] = used.get(base, 0) + 1
                suffix = "" if used[base] == 1 else f"_{used[base]}"
                z.writestr(f"{root}/characters/{base}{suffix}.{ext}", data)

        if frames:
            for s in st.get("sequence", []):
                try:
                    data = store.read_image(s["image_url"])
                except Exception:
                    continue
                ext = os.path.splitext(s["image_url"])[1].lstrip(".") or "png"
                z.writestr(f"{root}/frames/frame_{int(s.get('index', 0)):03d}.{ext}", data)

        if video:
            for e in st.get("edits", []):
                try:
                    path = store.url_to_path(e["url"])
                    with open(path, "rb") as fh:
                        z.writestr(f"{root}/video/{os.path.basename(path)}", fh.read())
                except Exception:
                    continue

        if thumbnails:
            for i, t in enumerate(st.get("thumbnails", []), 1):
                try:
                    data = store.read_image(t["url"])
                except Exception:
                    continue
                ext = os.path.splitext(t["url"])[1].lstrip(".") or "png"
                label = _safe_name(t.get("title", ""), "thumbnail")
                z.writestr(f"{root}/thumbnails/{label}_{i}.{ext}", data)

        if not z.namelist():
            z.writestr(f"{root}/README.txt",
                       "Nothing matched your export selection.")

    buf.seek(0)
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{root}.zip"'},
    )


@app.post("/api/projects/import")
async def api_project_import(file: UploadFile = File(...)):
    """Rebuild a working project from a previously exported ZIP (script,
    master prompt, character sheets and frames) as a NEW project."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "that isn't a valid .zip")
    names = z.namelist()

    def _find(suffix):
        for n in names:
            if n.lower().endswith(suffix):
                return n
        return None

    def _read(n):
        with z.open(n) as f:
            return f.read()

    # script + title
    script = None
    sj = _find("script.json")
    if sj:
        try:
            script = json.loads(_read(sj).decode("utf-8"))
        except Exception:
            script = None
    title = (script or {}).get("title") or "Imported project"

    pid = store.create_project(f"{title} (imported)"[:80])
    st = store.load_state()
    if script:
        st["script"] = script
    mp = _find("master_prompt.txt")
    if mp:
        try:
            st["master_prompt"] = _read(mp).decode("utf-8").strip()
        except Exception:
            pass

    # character sheets
    img_ext = (".png", ".jpg", ".jpeg", ".webp")
    for n in sorted(names):
        low = n.lower()
        if "/characters/" in low and low.endswith(img_ext):
            try:
                ext = os.path.splitext(n)[1].lstrip(".").lower() or "png"
                url = store.write_image("characters", _read(n), ext=ext)
                name = _safe_name(os.path.splitext(os.path.basename(n))[0], "character")
                st["characters"].append({
                    "id": store.new_id("char"), "name": name, "description": "",
                    "sheet_url": url, "prompt": "(imported)", "source": "imported",
                    "created": store.now()})
            except Exception:
                pass

    # frames (ordered) -> sequence, with scene prompts if counts line up
    scene_prompts = [(sc.get("prompt") or "") for sc in ((script or {}).get("scenes") or [])]
    frame_names = sorted(n for n in names
                         if "/frames/" in n.lower() and n.lower().endswith(img_ext))
    for i, n in enumerate(frame_names):
        try:
            ext = os.path.splitext(n)[1].lstrip(".").lower() or "png"
            url = store.write_image("images", _read(n), ext=ext)
            st["sequence"].append({
                "id": store.new_id("shot"), "index": i + 1,
                "prompt": (scene_prompts[i] if i < len(scene_prompts) else "(imported)"),
                "image_url": url, "mode": "imported", "size": "", "quality": "",
                "characters": [], "refs": [], "continued_from": None,
                "created": store.now()})
        except Exception:
            pass

    # voiceover audio
    va = _find("voiceover.mp3")
    if va:
        try:
            url, path = store.write_binary("audio", _read(va), ext="mp3",
                                           name_hint="voiceover")
            try:
                dur = editor.probe_duration(path)
            except Exception:
                dur = 0
            st["audio"] = {"id": store.new_id("audio"), "url": url,
                           "name": "voiceover.mp3", "duration": dur}
        except Exception:
            pass

    store.save_state(st)
    out = store.list_projects()
    out["id"] = pid
    out["imported"] = {"characters": len(st["characters"]),
                       "frames": len(st["sequence"]),
                       "has_script": bool(st.get("script"))}
    return out


# --------------------------------------------------------------------------- #
#  SFX layer
# --------------------------------------------------------------------------- #
@app.post("/api/sfx/upload")
async def api_sfx_upload(
    file: UploadFile = File(...),
    at_seconds: float = Form(0.0),
    volume: float = Form(0.8),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    ext = (os.path.splitext(file.filename or "")[1] or ".mp3").lstrip(".").lower()
    if ext not in {"mp3", "wav", "m4a", "ogg", "aac"}:
        ext = "mp3"
    url, path = store.write_binary("audio", data, ext=ext,
                                   name_hint=f"sfx_{file.filename or 'clip'}")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {
        "id": store.new_id("sfx"),
        "url": url,
        "name": file.filename or "sfx.mp3",
        "duration": dur,
        "at_seconds": max(0.0, at_seconds),
        "volume": max(0.0, min(1.0, volume)),
    }
    st.setdefault("sfx", []).append(rec)
    store.save_state(st)
    return rec


class SfxGenIn(BaseModel):
    text: str                          # e.g. "deep cinematic boom", "lava crackle"
    duration_seconds: Optional[float] = None   # 0.5-22, or None for auto
    at_seconds: float = 0.0
    volume: float = 0.7


@app.post("/api/sfx/generate")
def api_sfx_generate(body: SfxGenIn, request: Request):
    """Generate a sound effect from a text description via ElevenLabs and add it
    to the project's SFX (mixed into the next render at ``at_seconds``)."""
    if not request.state.settings["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    if not (body.text or "").strip():
        raise HTTPException(400, "describe the sound to generate")
    try:
        url, path = _generate_sfx_cached(request, body.text.strip(),
                                         body.duration_seconds)
    except Exception as e:
        raise HTTPException(500, f"SFX generation failed: {e}")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {"id": store.new_id("sfx"), "url": url, "name": body.text.strip()[:50],
           "duration": dur, "at_seconds": max(0.0, body.at_seconds),
           "volume": max(0.0, min(1.0, body.volume))}
    st.setdefault("sfx", []).append(rec)
    store.save_state(st)
    store.log_usage("voice", 1, 0.01)
    return rec


class SfxUpdateIn(BaseModel):
    at_seconds: Optional[float] = None
    volume: Optional[float] = None


@app.post("/api/sfx/{sid}/update")
def api_sfx_update(sid: str, body: SfxUpdateIn):
    st = store.load_state()
    sfx = next((s for s in st.get("sfx", []) if s["id"] == sid), None)
    if not sfx:
        raise HTTPException(404, "no such sfx")
    if body.at_seconds is not None:
        sfx["at_seconds"] = max(0.0, body.at_seconds)
    if body.volume is not None:
        sfx["volume"] = max(0.0, min(1.0, body.volume))
    store.save_state(st)
    return sfx


@app.delete("/api/sfx/{sid}")
def api_sfx_delete(sid: str):
    st = store.load_state()
    st["sfx"] = [s for s in st.get("sfx", []) if s["id"] != sid]
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Timeline: waveform peaks for audio visualization
# --------------------------------------------------------------------------- #
@app.get("/api/waveform")
def api_waveform(bars: int = 120):
    st = store.load_state()
    audio = st.get("audio")
    if not audio:
        return {"peaks": [], "duration": 0}
    try:
        path = store.url_to_path(audio["url"])
    except Exception:
        return {"peaks": [], "duration": 0}
    if not os.path.exists(path):
        return {"peaks": [], "duration": 0}
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    bars = max(10, min(400, bars))
    try:
        cmd = [
            "ffmpeg", "-i", path, "-ac", "1", "-ar", "8000",
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        raw = proc.stdout
        import struct
        samples = struct.unpack(f"<{len(raw)//2}h", raw) if raw else []
        if not samples:
            return {"peaks": [0] * bars, "duration": dur}
        chunk = max(1, len(samples) // bars)
        peaks = []
        for i in range(bars):
            start = i * chunk
            end = min(start + chunk, len(samples))
            if start >= len(samples):
                peaks.append(0)
            else:
                peaks.append(max(abs(s) for s in samples[start:end]))
        mx = max(peaks) or 1
        peaks = [round(p / mx, 3) for p in peaks]
        return {"peaks": peaks, "duration": dur}
    except Exception:
        return {"peaks": [0] * bars, "duration": dur}


# --------------------------------------------------------------------------- #
#  Google OAuth (YouTube upload + Drive export)
# --------------------------------------------------------------------------- #
import urllib.parse
import requests as _requests

_GOOGLE_TOKEN_PATH = os.path.join(config.DATA_DIR, "google_tokens.json")
_GOOGLE_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/drive.file"
_GOOGLE_TOKEN_LOCK = threading.Lock()


def _load_google_tokens():
    if not os.path.exists(_GOOGLE_TOKEN_PATH):
        return {}
    try:
        with open(_GOOGLE_TOKEN_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_google_tokens(tokens):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    tmp = _GOOGLE_TOKEN_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, _GOOGLE_TOKEN_PATH)


def _google_token_for(email):
    with _GOOGLE_TOKEN_LOCK:
        tokens = _load_google_tokens()
        entry = tokens.get(email or "_default")
        if not entry:
            return None
        if entry.get("expires_at", 0) < time.time() - 60:
            refreshed = _refresh_google_token(entry.get("refresh_token"))
            if refreshed:
                entry["access_token"] = refreshed["access_token"]
                entry["expires_at"] = time.time() + refreshed.get("expires_in", 3600)
                tokens[email or "_default"] = entry
                _save_google_tokens(tokens)
            else:
                return None
        return entry.get("access_token")


def _refresh_google_token(refresh_token):
    if not refresh_token or not config.GOOGLE_CLIENT_ID:
        return None
    r = _requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return None
    return None


@app.get("/auth/google/login")
def google_login():
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "GOOGLE_CLIENT_ID not configured. Add it to .env.")
    params = urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": _GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/auth/google/callback")
def google_callback(code: str = "", error: str = "", request: Request = None):
    if error:
        raise HTTPException(400, f"Google auth error: {error}")
    if not code:
        raise HTTPException(400, "No authorization code received")
    r = _requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=15)
    if r.status_code != 200:
        raise HTTPException(500, f"Token exchange failed: {r.text[:200]}")
    data = r.json()
    email = request.state.user_email if request else "_default"
    with _GOOGLE_TOKEN_LOCK:
        tokens = _load_google_tokens()
        tokens[email or "_default"] = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", tokens.get(email or "_default", {}).get("refresh_token", "")),
            "expires_at": time.time() + data.get("expires_in", 3600),
        }
        _save_google_tokens(tokens)
    return HTMLResponse("<html><body><h2>Google connected!</h2><p>You can close this tab.</p>"
                        "<script>window.close()</script></body></html>")


@app.get("/api/google/status")
def google_status(request: Request):
    email = request.state.user_email
    token = _google_token_for(email)
    return {
        "connected": token is not None,
        "configured": bool(config.GOOGLE_CLIENT_ID),
    }


class YouTubeUploadIn(BaseModel):
    edit_id: str
    title: str = "Untitled Video"
    description: str = ""
    tags: List[str] = []
    privacy: str = "private"
    thumbnail_id: Optional[str] = None


@app.post("/api/youtube/upload")
def api_youtube_upload(body: YouTubeUploadIn, request: Request):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "YouTube upload not configured. Set GOOGLE_CLIENT_ID, "
                            "GOOGLE_CLIENT_SECRET in .env and connect your Google account in Settings.")
    token = _google_token_for(request.state.user_email)
    if not token:
        raise HTTPException(401, "Google account not connected. Click 'Connect Google' in Settings.")
    st = store.load_state()
    edit = next((e for e in st.get("edits", []) if e["id"] == body.edit_id), None)
    if not edit:
        raise HTTPException(404, "No such edit")
    try:
        video_path = store.url_to_path(edit["url"])
    except Exception:
        raise HTTPException(400, "Video file not found")
    if not os.path.exists(video_path):
        raise HTTPException(400, "Video file missing from disk")

    metadata = {
        "snippet": {
            "title": body.title[:100],
            "description": body.description[:5000],
            "tags": body.tags[:30],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": body.privacy if body.privacy in ("public", "unlisted", "private") else "private",
        },
    }
    headers = {"Authorization": f"Bearer {token}"}

    # Resumable upload init
    init = _requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={**headers, "Content-Type": "application/json"},
        json=metadata, timeout=30,
    )
    if init.status_code not in (200, 308):
        raise HTTPException(500, f"YouTube upload init failed: {init.text[:200]}")
    upload_url = init.headers.get("Location")
    if not upload_url:
        raise HTTPException(500, "No upload URL returned by YouTube")

    fsize = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        up = _requests.put(upload_url, data=f,
                           headers={**headers, "Content-Type": "video/mp4",
                                    "Content-Length": str(fsize)},
                           timeout=600)
    if up.status_code not in (200, 201):
        raise HTTPException(500, f"YouTube upload failed: {up.text[:200]}")
    yt_data = up.json()
    video_id = yt_data.get("id", "")

    # Optionally set thumbnail
    if body.thumbnail_id and video_id:
        thumb = next((t for t in st.get("thumbnails", []) if t["id"] == body.thumbnail_id), None)
        if thumb:
            try:
                thumb_bytes = store.read_image(thumb["url"])
                _requests.post(
                    f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
                    f"?videoId={video_id}&uploadType=media",
                    headers={**headers, "Content-Type": "image/png"},
                    data=thumb_bytes, timeout=30,
                )
            except Exception:
                pass

    return {
        "ok": True,
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}" if video_id else "",
    }


# --------------------------------------------------------------------------- #
#  Google Drive export
# --------------------------------------------------------------------------- #
class DriveExportIn(BaseModel):
    name: str = ""


@app.post("/api/export/drive")
def api_export_drive(body: DriveExportIn, request: Request):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "Google Drive not configured. Set GOOGLE_CLIENT_ID in .env.")
    token = _google_token_for(request.state.user_email)
    if not token:
        raise HTTPException(401, "Google account not connected. Click 'Connect Google' in Settings.")

    # Build the ZIP (reuse export logic)
    st = store.load_state()
    idx = store.list_projects()
    cur = next((p for p in idx.get("projects", []) if p["id"] == idx.get("current")), None)
    proj_name = (body.name.strip() or (cur["name"] if cur else "project"))[:60]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if st.get("master_prompt"):
            z.writestr("master_prompt.txt", st["master_prompt"])
        if st.get("script"):
            z.writestr("script.json", json.dumps(st["script"], indent=2))
        for c in st.get("characters", []):
            try:
                data = store.read_image(c["sheet_url"])
                ext = os.path.splitext(c["sheet_url"])[1].lstrip(".") or "png"
                z.writestr(f"characters/{c.get('name', 'char')}.{ext}", data)
            except Exception:
                pass
        for s in st.get("sequence", []):
            try:
                data = store.read_image(s["image_url"])
                ext = os.path.splitext(s["image_url"])[1].lstrip(".") or "png"
                z.writestr(f"frames/frame_{int(s.get('index', 0)):03d}.{ext}", data)
            except Exception:
                pass
    buf.seek(0)
    zip_bytes = buf.read()

    headers = {"Authorization": f"Bearer {token}"}
    meta = json.dumps({"name": f"{proj_name}.zip", "mimeType": "application/zip"}).encode()
    boundary = "---cs-drive-boundary---"
    body_parts = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        + meta + f"\r\n--{boundary}\r\nContent-Type: application/zip\r\n\r\n".encode()
        + zip_bytes + f"\r\n--{boundary}--".encode()
    )
    r = _requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"},
        data=body_parts, timeout=120,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(500, f"Drive upload failed: {r.text[:200]}")
    file_data = r.json()
    file_id = file_data.get("id", "")

    # Make shareable
    link = ""
    if file_id:
        try:
            _requests.post(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                headers={**headers, "Content-Type": "application/json"},
                json={"role": "reader", "type": "anyone"}, timeout=15,
            )
            link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
        except Exception:
            link = f"https://drive.google.com/file/d/{file_id}"

    return {"ok": True, "file_id": file_id, "link": link, "name": f"{proj_name}.zip"}


# --------------------------------------------------------------------------- #
#  Autopilot: one-click YT link -> full video + thumbnail
# --------------------------------------------------------------------------- #
class AutopilotIn(BaseModel):
    url: str = ""                      # YouTube URL (legacy single-link)
    urls: List[str] = []               # one OR MANY YouTube URLs (preferred)
    nudge: str = ""                    # creative direction
    suggestion_index: int = 0          # which suggestion to use (0 = first)
    width: int = 1920
    height: int = 1080
    fps: int = 30
    transition: str = "cut"
    motion: bool = True                # Ken Burns slow zoom on every still
    quality: Optional[str] = None
    size: Optional[str] = None
    model: Optional[str] = None        # Claude model override
    voice_id: Optional[str] = None     # ElevenLabs voice for the narration
    target_seconds: Optional[float] = None  # desired video length target
    run_id: Optional[str] = None       # client token so a run can be stopped
    step_timeout: float = 300.0        # auto-proceed if a heavy step stalls past this
    max_hold: float = 1.5              # split frames longer than this into micro-cuts
    from_cache: bool = False           # reuse the topics from a prior /suggest call
                                       # so the picked index maps to what the user saw
    sound_design: bool = True          # generate + mix ElevenLabs SFX + loudnorm
    dynamic: bool = True               # reaction-rich, varied-background scenes
    fresh: bool = True                 # wipe the previous run's frames/video/etc.
                                       # before generating so they don't mix
    orientation: Optional[str] = None  # "vertical" (9:16 Shorts), "square", or
                                       # "landscape" (default) — sets frame + video size
    pacing_seconds: Optional[float] = 1.5  # seconds per image — 1.5 = fast cuts
    num_characters: Optional[int] = None     # override AI-suggested character count
    smart_edit: bool = True            # use Claude to audio-sync image holds
    angle_variety: bool = True         # cycle camera angles per frame (micro-cuts)
                                       # so consecutive frames feel like real cuts
    keep_style: bool = False           # keep the project's pinned style anchors +
                                       # style notes instead of re-deriving them
                                       # from the analysed video
    cut_clicks: bool = True            # ElevenLabs click on every frame change
    cut_click_volume: float = 0.30
    cut_click_style: str = "whoosh"    # click | camera | whoosh | pop | tick
    resume: bool = False               # CONTINUE an interrupted run: reuse the
                                       # script/characters/frames/video already
                                       # on disk and only do the missing steps,
                                       # instead of regenerating from scratch.
    project_id: Optional[str] = None   # target a NON-current project — used by
                                       # the project-picker "▶ Resume" button
                                       # to restart an old/stuck run without
                                       # first switching the user's active
                                       # project (which would lose the resume
                                       # mid-flight).


# --- Autopilot run control: stop flag + per-step deadline ------------------- #
_AUTOPILOT_STOP = set()
_AUTOPILOT_LOCK = threading.Lock()


class _Stopped(Exception):
    pass


def _autopilot_stopped(run_id):
    if not run_id:
        return False
    with _AUTOPILOT_LOCK:
        return run_id in _AUTOPILOT_STOP


def _check_stop(run_id):
    if _autopilot_stopped(run_id):
        with _AUTOPILOT_LOCK:
            _AUTOPILOT_STOP.discard(run_id)
        raise _Stopped()


@app.exception_handler(_Stopped)
async def _stopped_handler(request: Request, exc: _Stopped):
    # An autopilot run was stopped between steps. Whatever finished is already
    # saved to project state, so the client just reloads /api/state.
    return JSONResponse(status_code=200, content={
        "ok": False, "stopped": True,
        "detail": "Autopilot stopped — finished steps were kept."})


_BACKGROUND_THREADS = []
_BG_THREAD_LOCK = threading.Lock()


def _cleanup_bg_threads():
    """Reap completed background threads so they don't accumulate forever."""
    with _BG_THREAD_LOCK:
        _BACKGROUND_THREADS[:] = [t for t in _BACKGROUND_THREADS if t.is_alive()]


def _run_with_deadline(fn, seconds):
    """Run ``fn`` in a daemon thread and stop WAITING after ``seconds`` so a
    stalled heavy step doesn't hang the whole run. Returns (finished, value).
    A step that times out keeps finishing in the background — its results just
    appear a little later — which is safe here because every step only appends
    to project state. Exceptions raised by ``fn`` are re-raised.

    Timed-out threads are tracked and reaped so they don't accumulate forever.
    A hard cap of 10 concurrent background threads prevents runaway resource
    usage from repeated timeouts."""
    _cleanup_bg_threads()
    with _BG_THREAD_LOCK:
        if len(_BACKGROUND_THREADS) >= 10:
            # Too many timed-out threads still running — wait for the oldest
            # one to finish before starting another (prevents runaway).
            oldest = _BACKGROUND_THREADS[0]
            oldest.join(timeout=30)
            _BACKGROUND_THREADS[:] = [t for t in _BACKGROUND_THREADS if t.is_alive()]
    box = {}

    def worker():
        try:
            box["v"] = fn()
        except Exception as e:          # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(max(1.0, float(seconds)))
    if t.is_alive():
        with _BG_THREAD_LOCK:
            _BACKGROUND_THREADS.append(t)
        return (False, None)
    if "e" in box:
        raise box["e"]
    return (True, box.get("v"))


class AutopilotStopIn(BaseModel):
    run_id: str


@app.post("/api/autopilot/stop")
def api_autopilot_stop(body: AutopilotStopIn):
    """Request that an in-flight autopilot run stop at the next step boundary."""
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_STOP.add(body.run_id)
    return {"ok": True, "run_id": body.run_id}


# --- Live autopilot progress (drives the progress bar + ETA in the UI) ------ #
_AUTOPILOT_PROGRESS = {}
AP_STEPS = ["analyse", "script", "plan", "characters", "frames", "video", "thumbnail", "seo"]


def _ap_fail(run_id, err):
    """Stamp the breadcrumb as a FAILED (not done) run so the project surfaces
    as resumable on reload AND the user sees WHY it stopped. Reads the step the
    run died on from the live progress dict (falls back to disk breadcrumb).
    Whatever work already saved to disk (script / chars / frames) stays — the
    user clicks ▶ Resume and the pipeline continues from the saved point."""
    if not run_id:
        return
    try:
        with _AUTOPILOT_LOCK:
            p = _AUTOPILOT_PROGRESS.get(run_id) or {}
            step = p.get("step") or ""
        # done stays False so _autopilot_disk_status / projects summary flag it
        # incomplete; error is shown in the picker + Continue banner.
        _ap_prog(run_id, done=False, error=str(err)[:500],
                 failed=True, failed_step=step, failed_at=time.time())
    except Exception as _e:
        print(f"[autopilot] _ap_fail bookkeeping error: {_e}", flush=True)


def _ap_prog(run_id, **kw):
    if not run_id:
        return
    with _AUTOPILOT_LOCK:
        p = _AUTOPILOT_PROGRESS.setdefault(run_id, {
            "run_id": run_id, "started": time.time(), "steps_total": len(AP_STEPS),
            "step": "", "step_index": 0, "chars_done": 0, "chars_total": 0,
            "frames_done": 0, "frames_total": 0, "done": False, "video_url": None,
            "recent_frames": [], "recent_characters": [],
        })
        if "step" in kw:
            kw["step_index"] = AP_STEPS.index(kw["step"]) if kw["step"] in AP_STEPS else p["step_index"]
        # Accumulate every completed frame URL into a rolling gallery so the UI
        # can render images one-by-one as they finish (like a web page filling
        # in), instead of all-at-once at the very end. Capped so the in-memory
        # progress dict never grows unbounded on long runs.
        _new_frame = kw.pop("last_image_url", None)
        if _new_frame:
            kw["last_image_url"] = _new_frame   # keep the single-latest field too
            gallery = list(p.get("recent_frames") or [])
            if _new_frame not in gallery:
                gallery.append(_new_frame)
            kw["recent_frames"] = gallery[-200:]   # cap memory
        # Same for completed CHARACTER SHEETS — kept in a SEPARATE list so the UI
        # shows cast sheets in their own box (not mixed with scene frames). Each
        # entry is {url, name} so the card can label the character.
        _new_char = kw.pop("last_character", None)
        if _new_char and (_new_char.get("url") if isinstance(_new_char, dict) else _new_char):
            cast = list(p.get("recent_characters") or [])
            _cu = _new_char.get("url") if isinstance(_new_char, dict) else _new_char
            if not any((c.get("url") if isinstance(c, dict) else c) == _cu for c in cast):
                cast.append(_new_char)
            kw["recent_characters"] = cast[-50:]
        # A successful completion clears any stale failure flags from an earlier
        # interrupted attempt on the same run_id (resume → complete), so a now-
        # finished project doesn't keep showing the old "stopped: <err>".
        if kw.get("done"):
            kw.setdefault("failed", False)
            kw.setdefault("error", "")
            kw.setdefault("failed_step", "")
        p.update(kw)
        snap = dict(p)
    # Persist a lightweight breadcrumb to the project state on disk so an
    # interrupted run (network drop / server restart / closed tab) can be
    # discovered and CONTINUED later, even though the live in-memory progress
    # dict is gone. Only the small status fields are stored — never media.
    try:
        with _state_write_lock:
            _st = store.load_state()
            _st["autopilot_run"] = {
                "run_id": snap.get("run_id"),
                "step": snap.get("step"),
                "step_index": snap.get("step_index", 0),
                "done": bool(snap.get("done")),
                "chars_done": snap.get("chars_done", 0),
                "chars_total": snap.get("chars_total", 0),
                "frames_done": snap.get("frames_done", 0),
                "frames_total": snap.get("frames_total", 0),
                "video_url": snap.get("video_url"),
                "error": snap.get("error") or "",
                "failed": bool(snap.get("failed")),
                "failed_step": snap.get("failed_step") or "",
                "updated": time.time(),
            }
            store.save_state(_st)
    except Exception:
        pass


def _autopilot_disk_status():
    """Inspect persisted state and report which autopilot steps already have
    output on disk. Drives the Continue button + the resume skip-logic."""
    try:
        st = store.load_state()
    except Exception:
        return {"incomplete": False}
    run = st.get("autopilot_run") or {}
    script = st.get("script") or {}
    scenes = script.get("scenes") or []
    chars = [c for c in (st.get("characters") or []) if c.get("sheet_url")]
    frames = st.get("sequence") or []
    has_script = bool(scenes)
    has_chars = bool(chars)
    has_frames = bool(frames)
    has_video = bool((st.get("voiceover") or {}).get("url")) and has_frames
    has_seo = bool(st.get("seo"))
    # A run is "incomplete / continuable" when SOME work exists but the final
    # video is missing. If the video already exists, the project is complete —
    # don't nag with a Continue button (covers pre-existing finished projects
    # that predate the breadcrumb). Only an explicit not-done breadcrumb with a
    # missing video marks it continuable.
    done_flag = bool(run.get("done"))
    has_breadcrumb = bool(run.get("run_id"))
    if has_video:
        incomplete = has_breadcrumb and not done_flag
    else:
        incomplete = has_script or has_chars or has_frames
    return {
        "incomplete": incomplete,
        "run_id": run.get("run_id"),
        "last_step": run.get("step"),
        "updated": run.get("updated"),
        "has_script": has_script, "scenes": len(scenes),
        "has_characters": has_chars, "characters": len(chars),
        "has_frames": has_frames, "frames": len(frames),
        "has_video": has_video,
        "has_seo": has_seo,
        "done": done_flag,
        # Why the last run stopped (set by _ap_fail on a mid-run crash) so the
        # Continue banner / picker can show "stopped: <reason>" instead of a
        # silent dead run.
        "error": run.get("error") or "",
        "failed": bool(run.get("failed")),
        "failed_step": run.get("failed_step") or "",
    }


@app.get("/api/autopilot/last")
def api_autopilot_last():
    """Report whether the current project has an interrupted autopilot run that
    can be CONTINUED, and what's already been generated. The UI uses this on
    page-load (and after a network error) to show a ▶ Continue button."""
    return _autopilot_disk_status()


# --------------------------------------------------------------------------- #
# Per-project progress summary for the project list / Resume menu.
#
# The autopilot breadcrumb (`state["autopilot_run"]`) is the source of truth
# for whether a run was interrupted. A breadcrumb that's been stale for hours
# (no `updated` refresh) AND says `done=False` is almost certainly a run that
# died with the server (kill -9, power loss, tab crash before the resume
# banner could persist). Surface those as "stuck" so the user knows to Resume
# or Delete rather than wondering if the project is still rendering.
_STUCK_AFTER_SEC = 2 * 3600  # 2 hours without a progress tick = stuck


def _project_summary(pid, info):
    """Compute the autopilot progress snapshot for a single project. Pure
    function of the on-disk state — safe to call for every project on every
    list request. Returns a dict the frontend can render directly."""
    try:
        st = store.load_state_for(pid)
    except Exception:
        return {"id": pid, "name": (info or {}).get("name", pid),
                "pct": 0, "status": "empty", "incomplete": False, "stuck": False}
    name = (info or {}).get("name") or pid
    run = st.get("autopilot_run") or {}
    script = st.get("script") or {}
    scenes = script.get("scenes") or []
    chars = [c for c in (st.get("characters") or []) if c.get("sheet_url")]
    frames = st.get("sequence") or []
    has_script = bool(scenes)
    has_chars = bool(chars)
    has_frames = bool(frames)
    has_video = bool((st.get("voiceover") or {}).get("url")) and has_frames
    has_seo = bool(st.get("seo"))
    done_flag = bool(run.get("done"))
    breadcrumb_updated = run.get("updated") or 0
    breadcrumb_age = (time.time() - breadcrumb_updated) if breadcrumb_updated else None
    # A run is "stuck" if it has a breadcrumb that's still claiming in-flight
    # but hasn't ticked for hours — almost certainly an orphaned process.
    stuck = (bool(run.get("run_id")) and not done_flag
             and breadcrumb_age is not None and breadcrumb_age > _STUCK_AFTER_SEC)
    # "Incomplete / continuable" mirrors _autopilot_disk_status semantics.
    if has_video:
        incomplete = bool(run.get("run_id")) and not done_flag
    else:
        incomplete = has_script or has_chars or has_frames
    # Progress percentage — weight the heavy step (frames) the most so the bar
    # feels honest. Script + characters are cheap; frames + video are slow.
    # A project with a video is functionally done even without a breadcrumb
    # (the video IS the proof of completion) — without that rule, every
    # fresh page-load of a finished project flashes "partial" until the user
    # re-runs and re-completes the autopilot.
    if has_video and (done_flag or not run):
        pct = 100
        status = "done"
    elif stuck:
        pct = _rough_pct(has_script, has_chars, has_frames, frames, run, len(scenes))
        status = "stuck"
    elif incomplete:
        pct = _rough_pct(has_script, has_chars, has_frames, frames, run, len(scenes))
        status = "in_progress"
    elif has_script or has_chars or has_frames:
        # Work was done but no autopilot breadcrumb — manual render.
        pct = _rough_pct(has_script, has_chars, has_frames, frames, run, len(scenes))
        status = "partial"
    else:
        pct = 0
        status = "empty"
    return {
        "id": pid,
        "name": name,
        "created": (info or {}).get("created"),
        "updated": (info or {}).get("updated"),
        "pct": pct,
        "status": status,
        "incomplete": incomplete,
        "stuck": stuck,
        "last_step": run.get("step"),
        "run_id": run.get("run_id"),
        "error": run.get("error") or "",
        "failed": bool(run.get("failed")),
        "failed_step": run.get("failed_step") or "",
        "frames_done": run.get("frames_done") or len(frames),
        "frames_total": run.get("frames_total") or len(frames),
        "last_inputs": st.get("last_inputs") or {},
        # Use the breadcrumb counters when available so the bar reflects the
        # in-flight totals; otherwise fall back to what's on disk.
        "has_frames": has_frames, "frames": len(frames),
        "has_video": has_video,
        "has_seo": has_seo,
        "done": done_flag,
        "chars_done": run.get("chars_done") or len(chars),
        "chars_total": run.get("chars_total") or len(chars),
    }


def _rough_pct(has_script, has_chars, has_frames, frames, run, _scenes=0):
    """Cheap progress % in the absence of a live progress payload. Weights the
    expensive steps so a project that's mid-frames doesn't show 80%."""
    # Try the live counters first (frames_done / frames_total).
    ft = run.get("frames_total") or 0
    fd = run.get("frames_done") or 0
    if ft > 0:
        return min(99, round(fd * 100 / ft))
    # Fallback: 25% script + 15% chars + 60% frames
    pct = 0
    if has_script: pct += 25
    if has_chars: pct += 15
    if has_frames:
        # Each frame worth the same chunk of the frames step. Use the script's
        # scene count as the EXPECTED total so a mid-frames project doesn't
        # pretend it's done — bug: previous version used `max(n,1)` as both
        # numerator AND denominator, so 60% was added unconditionally.
        n = len(frames)
        est_total = run.get("frames_total") or 0
        if not est_total or est_total < n:
            # `_scenes` is passed in by _project_summary from the loaded state.
            est_total = _scenes if _scenes else n
        pct += min(60, round(n * 60 / max(est_total, 1)))
    return min(99, pct)


@app.get("/api/projects/summary")
def api_projects_summary():
    """List every project on disk with its autopilot progress snapshot. Used by
    the project picker so the user can see which projects are done, which are
    mid-run, and which are stuck from a previous crash."""
    try:
        idx = store.list_projects()
    except Exception as e:
        raise HTTPException(500, f"projects index: {e}")
    info_map = {p["id"]: p for p in (idx.get("projects") or [])}
    summaries = [_project_summary(p["id"], p)
                 for p in (idx.get("projects") or [])]
    # Sort: in-progress + stuck first, then by most-recently-updated.
    summaries.sort(key=lambda s: (
        0 if s["status"] in ("stuck", "in_progress") else 1,
        -(s.get("updated") or 0),
    ))
    return {"current": idx.get("current"), "projects": summaries}


@app.get("/api/autopilot/progress/{run_id}")
def api_autopilot_progress(run_id: str):
    with _AUTOPILOT_LOCK:
        p = dict(_AUTOPILOT_PROGRESS.get(run_id) or {})
    if not p:
        return {"run_id": run_id, "unknown": True}
    # ── RESUME / RELOAD gallery backfill ─────────────────────────────────────
    # On a RESUME run the run_id is fresh, so recent_frames/recent_characters
    # start EMPTY and only newly-rendered items get appended — the frames and
    # character sheets already on disk (which resume SKIPS re-rendering, but
    # still uses as refs) never appear in the UI gallery → "it stops showing the
    # previously generated character sheet and frames". Same gap after a page
    # reload mid-run. Fix: union the live in-memory gallery with what's persisted
    # on disk so the UI always shows the FULL set. Disk items come first (they're
    # the older, already-done work), then any live items not already present.
    try:
        _st = store.load_state()
        _disk_frames = [f.get("image_url") for f in (_st.get("sequence") or [])
                        if f.get("image_url")]
        _disk_chars = [{"url": c.get("sheet_url"), "name": c.get("name") or ""}
                       for c in (_st.get("characters") or []) if c.get("sheet_url")]
    except Exception:
        _disk_frames, _disk_chars = [], []
    # Frames: merge disk + live, dedup preserving order (disk first).
    _live_frames = list(p.get("recent_frames") or [])
    if _disk_frames:
        _seen, _merged = set(), []
        for u in _disk_frames + _live_frames:
            if u and u not in _seen:
                _seen.add(u); _merged.append(u)
        p["recent_frames"] = _merged[-200:]
    # Character sheets: merge by url.
    _live_chars = list(p.get("recent_characters") or [])
    if _disk_chars:
        _seen, _merged = set(), []
        for c in _disk_chars + _live_chars:
            _cu = c.get("url") if isinstance(c, dict) else c
            if _cu and _cu not in _seen:
                _seen.add(_cu); _merged.append(c)
        p["recent_characters"] = _merged[-50:]
    # Server-computed ETA from the dominant (frames) work + elapsed time.
    elapsed = max(0.0, time.time() - p.get("started", time.time()))
    done = p.get("frames_done", 0)
    total = p.get("frames_total", 0)
    eta = None
    if total and done:
        rate = elapsed / max(1, done)        # seconds per frame so far
        eta = round(rate * max(0, total - done), 1)
    p["elapsed"] = round(elapsed, 1)
    p["eta_seconds"] = eta
    return p


def _reset_generated(delete_files=True):
    """Clear the previous run's GENERATED media so a fresh autopilot run doesn't
    append onto (and desync with) old frames. Keeps the world bible, the topic
    list (yt_inspiration), brand, music and voice map. Deletes the orphaned media
    files from disk too ('erased from site') to reclaim space."""
    st = store.load_state()
    paths = []

    def _collect(u):
        if u:
            try:
                paths.append(store.url_to_path(u))
            except Exception:
                pass

    for s in st.get("sequence") or []:
        _collect(s.get("image_url"))
    for e in st.get("edits") or []:
        _collect(e.get("url"))
    for t in st.get("thumbnails") or []:
        _collect(t.get("url"))
        _collect(t.get("raw_url"))
    for c in st.get("characters") or []:
        _collect(c.get("sheet_url"))
    for x in st.get("sfx") or []:
        _collect(x.get("url"))
    _collect((st.get("voiceover") or {}).get("url"))
    _collect((st.get("audio") or {}).get("url"))

    st["sequence"] = []
    st["edits"] = []
    st["thumbnails"] = []
    st["characters"] = []
    st["sfx"] = []
    st["voiceover"] = None
    st["audio"] = None
    st["script"] = None
    st["seo"] = None
    store.save_state(st)

    if delete_files:
        for p in paths:
            try:
                os.remove(p)
            except Exception:
                pass
    return st


def _sort_by_virality(suggestions):
    """Most-viral-first, stable. Missing scores sink to the bottom. Non-dict
    entries are dropped so a stray value can't crash the sort."""
    items = [s for s in (suggestions or []) if isinstance(s, dict)]
    def _score(x):
        try:
            return float(x.get("virality_score") or 0)
        except (TypeError, ValueError):
            return 0.0
    return sorted(items, key=_score, reverse=True)


def _topic_constraint_nudge(c):
    """Turn the creator's production inputs (length / orientation / pacing /
    characters) into a directive so topics are pitched to FIT them."""
    if not c:
        return ""
    bits = []
    secs = c.get("target_seconds")
    if secs and secs > 0:
        bits.append(f"about {float(secs):.0f} seconds long")
    orient = (c.get("orientation") or "").lower()
    if orient in ("vertical", "portrait", "9:16", "shorts", "tiktok"):
        bits.append("VERTICAL 9:16 (Shorts/TikTok) format")
    elif orient in ("square", "1:1"):
        bits.append("SQUARE 1:1 format")
    elif orient:
        bits.append("LANDSCAPE 16:9 (YouTube) format")
    nc = c.get("num_characters")
    if nc is not None and int(nc) >= 0:
        bits.append(f"{int(nc)} recurring character(s)" if int(nc) > 0 else "pure narration, no characters")
    if not bits:
        return ""
    return ("\n\nPRODUCTION CONSTRAINTS — pitch topics that fit a video that is "
            + ", ".join(bits) + ". Scope each idea so it works at that length and "
            "format (a 15s short needs ONE tight beat; a 2-minute video can go deeper).")


def _apply_topic_constraints(suggestions, c):
    """Bake the creator's chosen length / pacing / character count onto every
    suggestion so the picked topic (and its script) is written to that timing."""
    if not c:
        return suggestions
    secs = c.get("target_seconds")
    pace = c.get("pacing_seconds")
    nc = c.get("num_characters")
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        if secs and secs > 0:
            s["total_duration"] = float(secs)
        if pace and pace > 0:
            s["pacing_seconds"] = float(pace)
        if nc is not None and int(nc) >= 0:
            s["num_characters"] = int(nc)
        _d = s.get("total_duration")
        _p = s.get("pacing_seconds")
        if _d and _p:
            s["scene_count"] = max(1, round(float(_d) / max(0.1, float(_p))))
    return suggestions


def _autopilot_analyze(urls, nudge, model, request, n_suggestions=10, constraints=None):
    """Autopilot workflow step 1: ingest one OR MANY YouTube refs and run the
    SAME deep multi-video analysis the YT Analyser tab uses — deconstruct the
    shared art style / pacing / way-of-speaking / storytelling and pitch fresh,
    virality-ranked, fully-described ideas. Saved as yt_inspiration. The rich
    4-axis fields are also mapped onto the legacy keys (style_summary, topic)
    the rest of the pipeline reads. ``constraints`` (length/orientation/pacing/
    characters) tailor the topics + bake the entered timing into each idea."""
    import youtube
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in (urls or []) if u and u.strip()]
    if not urls:
        raise HTTPException(400, "Paste at least one YouTube link.")
    for u in urls:
        if not youtube.is_youtube_url(u):
            raise HTTPException(400, f"That doesn't look like a YouTube link: {u}")

    nudge = (nudge or "") + _topic_constraint_nudge(constraints)
    data, src_meta, errors, pooled_frames = _deep_analyze_urls(
        urls, nudge, model, request, n_suggestions=n_suggestions)
    suggestions = _apply_topic_constraints(
        _sort_by_virality(data.get("suggestions") or []), constraints)
    if not suggestions:
        raise HTTPException(500, "Analysis returned no suggestions.")

    primary = src_meta[0] if src_meta else {}
    art_style = data.get("art_style", "")
    insp = {
        # legacy keys the downstream pipeline reads:
        "url": primary.get("url") or urls[0],
        "video_id": primary.get("video_id", ""),
        "title": primary.get("title", ""),
        "channel": primary.get("channel", ""),
        "style_summary": art_style,
        "speech_style": data.get("speech_style", ""),
        "topic": data.get("sources_summary", ""),
        "frames": pooled_frames,
        "suggestions": suggestions,
        # rich multi-video fields (same shape as yt_analysis):
        "input_urls": urls,
        "urls": [m.get("url") for m in src_meta] or urls,
        "art_style": art_style,
        "pacing": data.get("pacing", ""),
        "storytelling": data.get("storytelling", ""),
        "sources_summary": data.get("sources_summary", ""),
        "sources": src_meta,
        "errors": errors,
        "created": store.now(),
    }
    st = store.load_state()
    st["yt_inspiration"] = insp
    store.save_state(st)
    store.log_usage("script", 1, 0.01)
    return insp


def _autopilot_analyze_upload(video_path, nudge, model, request,
                              n_suggestions=10, constraints=None,
                              source_name=""):
    """Autopilot from an UPLOADED sample video file (no YouTube). Extract frames
    + transcribe the audio locally, then run the SAME Claude deconstruction
    (art style / pacing / way-of-speaking / storytelling) the YouTube path uses,
    so the generated video copies the sample's script style AND art style.
    Saved as yt_inspiration with an `upload://` sentinel url so the rest of the
    autopilot pipeline (from_cache, resume) treats it like any other source."""
    import video as videomod
    # 1. Extract a generous frame set so the style read + anchors are rich.
    _max_f = max(8, int(config.STYLE_REF_COUNT) * 3)
    try:
        frame_urls = videomod.extract_frames(video_path, fps=1.0, max_frames=_max_f)
    except Exception as e:
        raise HTTPException(500, f"frame extraction failed: {e}")
    if not frame_urls:
        raise HTTPException(400, "No frames could be extracted from that video.")

    # 2. Transcribe the audio locally (faster-whisper) -> script/way-of-speaking.
    transcript = ""
    try:
        import transcribe as transmod
        _tr = transmod.transcribe_audio(video_path,
                                        settings=getattr(request.state, "settings", {}),
                                        engine="local")
        transcript = (_tr.get("text") or "").strip() if isinstance(_tr, dict) else ""
    except Exception as te:
        print(f"[autopilot-upload] transcription skipped ({te}) — "
              "analysing visuals only", flush=True)

    # 3. Downsize frames for the vision call (cap at the vision pool size).
    frame_imgs = []
    for u in frame_urls[:18]:
        try:
            frame_imgs.append(pipeline.downsize_for_vision(store.read_image(u)))
        except Exception:
            pass
    if not frame_imgs:
        raise HTTPException(500, "Couldn't read the extracted frames for analysis.")

    # 4. Same multi-frame deconstruction the YouTube path uses.
    nudge = (nudge or "") + _topic_constraint_nudge(constraints)
    st = store.load_state()
    client = _claude_client_for(model, request)
    sources = [{"title": source_name or "Uploaded sample video",
                "channel": "", "transcript": transcript}]

    def _ask(extra=""):
        raw = client.suggest_from_references(
            frames=frame_imgs, sources=sources,
            nudge=(nudge + extra).strip(),
            master_prompt=st["master_prompt"], n_suggestions=n_suggestions)
        return _as_analysis_dict(extract_json(raw))

    try:
        data = _ask()
        sugs = _normalize_suggestions(data.get("suggestions"))
        if _suggestions_need_fix(sugs):
            data2 = _ask(
                "\n\nCRITICAL OUTPUT RULE: every item in \"suggestions\" MUST use "
                "these EXACT key names — title, logline, hook, distinct_angle, "
                "virality_score (INTEGER 1-100), virality_reason, voiceover_style, "
                "image_prompt_style, pacing_seconds, total_duration, scene_count. "
                "Sort most-viral first.")
            sugs2 = _normalize_suggestions(data2.get("suggestions"))
            if not _suggestions_need_fix(sugs2) or len(sugs2) > len(sugs):
                data, sugs = data2, sugs2
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")

    if not sugs:
        sugs = _fallback_suggestions(sources, n_suggestions)
    suggestions = _apply_topic_constraints(
        _sort_by_virality(_ensure_virality(sugs)), constraints)
    if not suggestions:
        raise HTTPException(500, "Analysis returned no suggestions.")

    art_style = data.get("art_style", "") or data.get("style_summary", "")
    sentinel = "upload://" + (source_name or os.path.basename(video_path))
    insp = {
        "url": sentinel, "video_id": "", "title": source_name or "Uploaded video",
        "channel": "", "style_summary": art_style,
        "speech_style": data.get("speech_style", ""),
        "topic": data.get("sources_summary", ""),
        "frames": frame_urls, "suggestions": suggestions,
        "input_urls": [sentinel], "urls": [sentinel],
        "art_style": art_style, "pacing": data.get("pacing", ""),
        "storytelling": data.get("storytelling", ""),
        "sources_summary": data.get("sources_summary", ""),
        "sources": [{"url": sentinel, "title": source_name or "Uploaded video",
                     "frames": frame_urls, "source": "upload"}],
        "errors": [], "created": store.now(),
        "from_upload": True, "transcript": transcript[:4000],
        # Persist the saved upload's disk path so RESUME can re-reference the
        # ACTUAL source video (re-extract frames / re-transcribe) instead of
        # only relying on the cached frame URLs. Orphan-proof: the file lives
        # in data/uploads/ and survives server restarts + reloads.
        "upload_path": os.path.abspath(video_path),
        "upload_name": source_name or os.path.basename(video_path),
    }
    st = store.load_state()
    st["yt_inspiration"] = insp
    store.save_state(st)
    store.log_usage("script", 1, 0.01)
    return insp


@app.post("/api/autopilot/from-upload")
async def api_autopilot_from_upload(
    request: Request,
    file: UploadFile = File(...),
    nudge: str = Form(""),
    model: Optional[str] = Form(None),
    target_seconds: Optional[float] = Form(None),
    pacing_seconds: Optional[float] = Form(None),
    num_characters: Optional[int] = Form(None),
    orientation: Optional[str] = Form(None),
    n_suggestions: int = Form(10),
):
    """Deconstruct an UPLOADED sample video and stage it for autopilot. Returns
    the analysis (suggestions + style). The client then calls /api/autopilot with
    from_cache=true and url='upload://<name>' to generate the copy-style video."""
    if not _has_ai_key(request.state.settings):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    _fn = os.path.basename((file.filename or "sample.mp4").replace("..", ""))
    dest = os.path.join(store.UPLOADS_DIR, store.new_id("sample") + "_" + _fn)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file — upload a real video.")
    os.makedirs(store.UPLOADS_DIR, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    constraints = {"target_seconds": target_seconds, "pacing_seconds": pacing_seconds,
                   "num_characters": num_characters, "orientation": orientation}
    insp = _autopilot_analyze_upload(
        dest, nudge, model, request, n_suggestions=n_suggestions,
        constraints=constraints, source_name=_fn)
    return {
        "ok": True,
        "url": insp["url"],                # sentinel to pass to /api/autopilot
        "style_summary": insp.get("style_summary", ""),
        "speech_style": insp.get("speech_style", ""),
        "suggestions": insp.get("suggestions", []),
        "frames": insp.get("frames", []),
        "transcript_chars": len(insp.get("transcript", "")),
    }


class AutopilotSuggestIn(BaseModel):
    url: str = ""                      # legacy single-link
    urls: List[str] = []               # one OR MANY YouTube URLs (preferred)
    nudge: str = ""
    model: Optional[str] = None
    n_suggestions: int = 10
    # Production inputs entered BEFORE finding topics — tailor + bake into ideas.
    target_seconds: Optional[float] = None
    pacing_seconds: Optional[float] = None
    num_characters: Optional[int] = None
    orientation: Optional[str] = None


def _autopilot_urls(body):
    """Collect YouTube urls from an autopilot request — supports the new
    multi-url `urls` list and falls back to the legacy single `url` field.
    De-duped, order-preserving."""
    raw = list(getattr(body, "urls", None) or [])
    single = (getattr(body, "url", "") or "").strip()
    if single:
        raw.append(single)
    seen, out = set(), []
    for u in raw:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


@app.post("/api/autopilot/suggest")
def api_autopilot_suggest(body: AutopilotSuggestIn, request: Request):
    """Phase 1 of the workflow: analyse the reference and return fresh, on-style
    video ideas ranked by predicted virality — so the user can PICK which one to
    produce (instead of always making the first/same one). No media is generated."""
    s = request.state.settings
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    urls = _autopilot_urls(body)
    constraints = {
        "target_seconds": body.target_seconds,
        "pacing_seconds": body.pacing_seconds,
        "num_characters": body.num_characters,
        "orientation": body.orientation,
    }
    insp = _autopilot_analyze(urls, body.nudge, body.model, request,
                              max(1, min(15, body.n_suggestions)),
                              constraints=constraints)
    return {"ok": True, "url": insp["url"], "urls": insp.get("urls", []),
            "title": insp["title"], "channel": insp["channel"],
            "style_summary": insp["style_summary"], "topic": insp["topic"],
            "art_style": insp.get("art_style", ""), "pacing": insp.get("pacing", ""),
            "speech_style": insp.get("speech_style", ""),
            "storytelling": insp.get("storytelling", ""),
            "sources": insp.get("sources", []), "errors": insp.get("errors", []),
            "suggestions": insp["suggestions"]}


@app.post("/api/autopilot")
def api_autopilot(body: AutopilotIn, request: Request):
    """Thin wrapper around the real pipeline. On ANY mid-run failure it stamps
    the breadcrumb (done=False + error + the step it died on) so the project
    reliably shows up as RESUMABLE on page reload — with its saved % — and the
    user can click ▶ Resume to continue from the exact stopped point. Whatever
    already saved to disk (script / characters / frames) is never wiped."""
    _run_id = body.run_id or store.new_id("run")
    body.run_id = _run_id            # pin so the pipeline + _ap_fail share it
    try:
        return _autopilot_pipeline(body, request)
    except _Stopped:
        # User clicked Stop — this is a clean intentional stop, NOT a crash.
        # The _Stopped exception handler at app.exception_handler(_Stopped)
        # will return a clean 200 with steps kept. Must NOT be caught by the
        # generic Exception handler below (that was marking it as a failure).
        raise
    except HTTPException as he:
        # Validation-style 4xx (no key, bad input) — still record so the picker
        # shows why, but re-raise so the client gets the proper status.
        try:
            if int(getattr(he, "status_code", 500)) >= 500:
                _ap_fail(_run_id, he.detail)
        except Exception:
            pass
        raise
    except Exception as e:
        _ap_fail(_run_id, e)
        print(f"[autopilot] run {_run_id} FAILED mid-pipeline: {e}", flush=True)
        raise HTTPException(500, f"Autopilot stopped: {e} — your progress is "
                                 f"saved; click ▶ Resume to continue.")


def _autopilot_pipeline(body: AutopilotIn, request: Request):
    """One-click pipeline: YouTube link -> analyse -> script -> characters ->
    frames -> voice-over video -> thumbnail. Returns progress at each step."""
    s = request.state.settings
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude or OpenAI key in Settings.")
    if not _has_image_key(request):
        raise HTTPException(400, "No image API key — add it in Settings.")
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key — add ElevenLabs or MiMo in Settings.")

    # ── PROJECT-PICKER RESUME ────────────────────────────────────────────────
    # When the user clicks "▶ Resume" on a NON-current project from the project
    # list, `body.project_id` arrives. Switch the global current pointer to
    # that project FIRST so every subsequent store.load_state() / load_state_for
    # / write goes to the right file. After a successful resume the user is
    # left on the resumed project (that's what they expected to see open).
    if body.project_id:
        try:
            idx = store._read_index()
            ids = {p["id"] for p in (idx.get("projects") or [])}
            if body.project_id not in ids:
                raise HTTPException(404, f"project {body.project_id} not found")
            if idx.get("current") != body.project_id:
                store.switch_project(body.project_id)
                print(f"[autopilot] project switched to {body.project_id} for resume",
                      flush=True)
            # Always force resume semantics for picker-launched runs — that's
            # the whole point of clicking Resume on a stuck project.
            body.resume = True
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"project switch failed: {e}")

    run_id = body.run_id or store.new_id("run")
    # ── RESUME / CONTINUE an interrupted run ───────────────────────────────
    # When resuming we must NOT wipe prior work and MUST reuse what's on disk:
    #   fresh=False     -> don't delete the frames/chars/video already rendered
    #   from_cache=True -> reuse the topic list so suggestion_index still lines up
    #   keep_style=True -> keep the pinned style anchors + notes from the 1st run
    # Per-step skip flags are computed from the persisted state below.
    _resume = bool(body.resume)
    _disk = _autopilot_disk_status() if _resume else {}
    if _resume:
        body.fresh = False
        body.from_cache = True
        # keep_style should preserve the pinned style anchors ONLY if they
        # actually exist on disk. A run that timed out / dropped BEFORE the
        # style-frame pinning step (e.g. analysis of a long video exceeded the
        # POST timeout, which now auto-resumes) has NO anchors yet — forcing
        # keep_style=True there would skip pinning forever and every frame would
        # render in a generic style instead of copying the reference video.
        # Re-pin in that case so style fidelity is never lost on resume.
        try:
            _rs = store.load_state()
            _have_anchors = bool(_rs.get("style_frames")) and bool(
                (_rs.get("style_notes") or "").strip()
                or (_rs.get("master_prompt") or "").strip())
        except Exception:
            _have_anchors = False
        body.keep_style = _have_anchors
        if not _have_anchors:
            print("[autopilot] RESUME: no style anchors on disk yet — will RE-PIN "
                  "from cached reference frames so style copying isn't lost",
                  flush=True)
        if not body.run_id and _disk.get("run_id"):
            run_id = _disk["run_id"]
        print(f"[autopilot] RESUME run_id={run_id} keep_style={body.keep_style} "
              f"disk={_disk}", flush=True)
    print(f"[autopilot] START target_seconds={body.target_seconds!r} "
          f"pacing_seconds={body.pacing_seconds!r} "
          f"num_characters={body.num_characters!r} "
          f"orientation={body.orientation!r}", flush=True)
    with _AUTOPILOT_LOCK:                      # clear any stale stop flag
        _AUTOPILOT_STOP.discard(run_id)
    step_to = max(15.0, float(body.step_timeout or 60.0))

    quality = body.quality or config.DEFAULT_QUALITY
    size = body.size or config.DEFAULT_SIZE
    width, height = body.width, body.height
    orient = (body.orientation or "landscape").lower()
    if orient in ("vertical", "portrait", "9:16", "shorts", "tiktok"):
        size = body.size or "1024x1536"        # portrait frames
        width, height = 1080, 1920             # 9:16 video
    elif orient in ("square", "1:1"):
        size = body.size or "1024x1024"
        width, height = 1080, 1080
    else:                                      # landscape / 16:9 / youtube / default
        size = body.size or "1536x1024"        # landscape frames
        width, height = 1920, 1080             # 16:9 video
    voice_id = body.voice_id or _voice_default_id(s)
    claude = _claude_client_for(body.model, request)
    steps = []

    # ---- STEP 1: Topics ----
    # Reuse the exact list the user picked from (so suggestion_index lines up);
    # otherwise analyse fresh. Either way they're ranked by virality.
    _ap_prog(run_id, step="analyse")
    urls = _autopilot_urls(body)
    # Uploaded-sample-video path: the analysis was already done by
    # /api/autopilot/from-upload and stored in yt_inspiration with an
    # `upload://` sentinel url. There's no YouTube link to (re)analyse, so we
    # always use the cached inspiration for these.
    _is_upload = any(str(u).startswith("upload://") for u in urls)
    cached = (store.load_state().get("yt_inspiration") or {})
    if _is_upload:
        if not cached.get("suggestions"):
            raise HTTPException(400, "Upload analysis missing — upload the sample "
                                     "video again to deconstruct it first.")
        use_cache = True
    else:
        if not urls:
            # Resume path: the user just clicked ▶ Resume on an old project
            # whose YouTube links were typed days ago — they're not in the
            # textbox anymore (page reload cleared them). If the cached
            # inspiration is still on disk for THIS project, USE IT and
            # pretend the URL list was the cached one. This is the exact
            # same fallback as `_autopilot_disk_status` using the breadcrumb
            # to recover an interrupted run.
            if _resume and cached.get("suggestions"):
                _cached_urls = (cached.get("input_urls")
                                or [cached.get("url")] or [])
                print(f"[autopilot] RESUME with empty urls — "
                      f"reusing cached yt_inspiration ({len(_cached_urls)} link(s))",
                      flush=True)
                urls = _cached_urls
                use_cache = True
            else:
                raise HTTPException(400, "Paste at least one YouTube link.")
        else:
            use_cache = (body.from_cache and cached.get("suggestions")
                         and (cached.get("input_urls") or [cached.get("url")]) == urls)

    # ── PERSIST last_inputs ──────────────────────────────────────────────────
    # Make the project's source explicitly recoverable after page reload so
    # the frontend can pre-fill the URL textbox + the Resume button can show
    # "▶ Resume (3 YT links)" / "▶ Resume (upload: foo.mp4)". Survives even
    # if yt_inspiration gets rebuilt — last_inputs is the load-bearing
    # snapshot the picker shows.
    try:
        with _state_write_lock:
            _li_st = store.load_state()
            _li_st["last_inputs"] = {
                "type": "upload" if _is_upload else "youtube",
                "urls": list(urls),
                "upload_name": (cached.get("upload_name")
                                or cached.get("source_filename")
                                or (urls[0].replace("upload://", "")
                                    if urls and urls[0].startswith("upload://")
                                    else "")),
                "upload_path": (cached.get("upload_path") or ""),
                "captured_at": time.time(),
            }
            store.save_state(_li_st)
    except Exception as _li_err:
        print(f"[autopilot] warn: last_inputs persist failed: {_li_err}",
              flush=True)
    insp = cached if use_cache else _autopilot_analyze(
        urls, body.nudge, body.model, request, 10,
        constraints={"target_seconds": body.target_seconds,
                     "pacing_seconds": body.pacing_seconds,
                     "num_characters": body.num_characters,
                     "orientation": body.orientation})
    suggestions = insp["suggestions"]
    analysis = {"style_summary": insp.get("style_summary", ""),
                "speech_style": insp.get("speech_style", ""),
                "topic": insp.get("topic", "")}
    steps.append({"step": "analyse", "suggestions": len(suggestions),
                  "cached": bool(use_cache)})

    # Fresh start: wipe the PREVIOUS run's frames/characters/video/thumbnail/
    # voiceover so this generation doesn't append onto stale frames and desync.
    # (yt_inspiration/topics + world bible are preserved.)
    if body.fresh:
        _reset_generated(delete_files=True)

    # Pin the reference-video frames as visual style anchors so every
    # generated image (character sheets + scene frames) is guided by the
    # actual look of the source video — not just the text style description.
    ref_frame_urls = insp.get("frames", [])
    if body.keep_style:
        print("[autopilot] keep_style: existing style anchors + notes preserved",
              flush=True)
    elif ref_frame_urls:
        # Pin STYLE_REF_COUNT evenly-spaced frames from the uploaded reference
        # video(s) as the style anchors that every generated frame must copy the
        # look of. More anchors = a fuller, more faithful read of the source art
        # style (palette, line weight, lighting, character design variation).
        _n_anchors = max(1, int(config.STYLE_REF_COUNT))
        step = max(1, len(ref_frame_urls) // _n_anchors)
        _style_picks = ref_frame_urls[::step][:_n_anchors]
        with _state_write_lock:
            _sf_st = store.load_state()
            _sf_st["style_frames"] = [{"id": store.new_id("sf"), "url": u}
                                       for u in _style_picks]
            store.save_state(_sf_st)
        print(f"[autopilot] style_frames pinned: {len(_style_picks)} frames "
              f"from the reference video(s)", flush=True)

    if not body.keep_style:
        # COPY THE REFERENCE VIDEO'S LOOK COMPLETELY. The World Bible
        # (master_prompt) is prepended to EVERY image prompt as "WORLD / STYLE
        # BIBLE", so a bible left over from another project would override the
        # analysed style no matter what anchors/notes are set. Replace it with
        # one written from the analysis; the old bible is kept in
        # master_prompt_backup (restore it in the Universe tab if needed).
        _summary = (insp.get("style_summary") or "").strip()
        if _summary:
            _bible = ("VISUAL STYLE — match the reference video exactly: "
                      + _summary)
            _speech = (insp.get("speech_style") or "").strip()
            if _speech:
                _bible += "\n\nNARRATION STYLE: " + _speech
            with _state_write_lock:
                _mb_st = store.load_state()
                _old_bible = (_mb_st.get("master_prompt") or "").strip()
                if _old_bible and _old_bible != _bible:
                    _mb_st["master_prompt_backup"] = _old_bible
                _mb_st["master_prompt"] = _bible
                store.save_state(_mb_st)
            print("[autopilot] World Bible rewritten from reference-video "
                  "analysis (old bible saved to master_prompt_backup)",
                  flush=True)

    # ---- STEP 2: Pick a suggestion and generate a script ----
    _check_stop(run_id)
    _ap_prog(run_id, step="script")
    st = store.load_state()
    pick = suggestions[min(max(0, body.suggestion_index), len(suggestions) - 1)]
    title = pick.get("title", "")
    desc = "\n\n".join(filter(None, [
        pick.get("logline", ""),
        f"Visual style: {pick['image_prompt_style']}" if pick.get("image_prompt_style") else "",
        f"Voice-over: {pick['voiceover_style']}" if pick.get("voiceover_style") else "",
    ]))
    # User values ALWAYS override AI suggestions.  Pacing defaults to 1s/frame
    # so that video-length and frame-count are obvious (frames = length ÷ pacing).
    total_dur = float(body.target_seconds) if (body.target_seconds and body.target_seconds > 0) \
        else float(pick.get("total_duration", 30.0))
    pacing = float(body.pacing_seconds) if (body.pacing_seconds and body.pacing_seconds > 0) \
        else float(pick.get("pacing_seconds", 1.0))
    # User pacing ALWAYS wins — never let the AI's suggestion override it.
    # When the user gives a target length we also cap pacing so we never get
    # fewer than 1 image per 2 seconds on long videos.
    if body.target_seconds and body.target_seconds > 0:
        pacing = min(pacing, 2.0)
    # Auto-cast by default: let the script model decide how many recurring
    # characters the story/duration actually needs. A non-negative override still
    # forces an exact count for backwards-compatible API callers.
    num_chars = -1
    if body.num_characters is not None and body.num_characters >= 0:
        num_chars = int(body.num_characters)
    style_notes = _sanitize_prompt(pick.get("image_prompt_style") or "")
    if body.keep_style:
        # Locked look: use the project's own style notes for every generation
        # instead of the analysed video's suggested style.
        style_notes = (st.get("style_notes") or "").strip() or style_notes
    elif not style_notes:
        # Copying the reference style but the pick has no image_prompt_style —
        # use the analysed style summary rather than leaving another project's
        # stale notes in place.
        style_notes = _sanitize_prompt(analysis.get("style_summary") or "")

    # Persist style_notes to state so _render_one can prepend it to every
    # image-generation prompt — characters, scene frames, all of them.
    # When copying the reference style this OVERWRITES stale notes (even with
    # an empty string) so nothing from a previous project leaks in.
    if not body.keep_style:
        with _state_write_lock:
            _sn_st = store.load_state()
            _sn_st["style_notes"] = style_notes
            store.save_state(_sn_st)
        print(f"[autopilot] style_notes saved: {style_notes[:80]}", flush=True)

    # The EXACT number of frames the user asked for.  Everything downstream
    # is driven by this — script, planner, and image generation all target it.
    expected_scenes = max(1, round(max(1.0, total_dur) / max(0.1, pacing)))
    print(f"[autopilot] target: {expected_scenes} frames "
          f"({total_dur}s ÷ {pacing}s/frame)", flush=True)

    # ── RESUME: reuse the script already on disk, skip (re)generation ───────
    _reuse_script = False
    if _resume:
        _disk_script = (store.load_state().get("script") or {})
        if (_disk_script.get("scenes") or []):
            script = _disk_script
            _reuse_script = True
            print(f"[autopilot] RESUME: reusing saved script "
                  f"({len(script.get('scenes') or [])} scenes) — skipping "
                  f"script generation", flush=True)
            steps.append({"step": "script", "reused": True,
                          "scenes": len(script.get("scenes") or []),
                          "characters": len(script.get("characters") or [])})

    # ── Script generation (attempt 1) ──────────────────────────────────────
    if not _reuse_script:
        # Way-of-speaking learned from the reference video(s) — passed as a
        # first-class narration mandate so the hook + VO actually sound like the
        # source, instead of a generic explainer voice.
        _vo_style = (st.get("yt_inspiration", {}) or {}).get("speech_style", "")
        try:
            script = _generate_script_validated(
                claude,
                title=title, description=desc,
                total_duration=max(1.0, total_dur),
                pacing_seconds=max(0.1, pacing),
                num_characters=num_chars,
                style_notes=style_notes,
                master_prompt=st["master_prompt"],
                dynamic=body.dynamic,
                voiceover_style=_vo_style,
            )
        except Exception as e:
            raise HTTPException(500, f"Script generation failed: {e}")

        _pre_actual = len(script.get("scenes") or [])
        print(f"[autopilot] script attempt 1: {_pre_actual}/{expected_scenes} scenes",
              flush=True)

        # ── Retry if Claude badly undershoots (<60 %) ──────────────────────────
        if _pre_actual < expected_scenes * 0.6 and expected_scenes > 4:
            try:
                raw2 = claude.generate_script(
                    title=title,
                    description=(
                        desc + f"\n\nCRITICAL: This is a FAST-CUT video that needs "
                        f"EXACTLY {expected_scenes} scenes. The previous attempt only had "
                        f"{_pre_actual} scenes — that is NOT acceptable. Write all "
                        f"{expected_scenes} unique visual moments now, numbered 1 to "
                        f"{expected_scenes}. Do not stop early."
                    ),
                    total_duration=max(1.0, total_dur),
                    pacing_seconds=max(0.1, pacing),
                    num_characters=num_chars,
                    style_notes=style_notes,
                    master_prompt=st["master_prompt"],
                    dynamic=body.dynamic,
                    voiceover_style=_vo_style,
                )
                script2 = extract_json(raw2)
                if len(script2.get("scenes") or []) > _pre_actual:
                    script = script2
                    print(f"[autopilot] retry improved: "
                          f"{len(script.get('scenes',[]))} scenes", flush=True)
            except Exception:
                pass

        # ── Fill remaining missing scenes with unique Claude-generated prompts ──
        # This replaces the mechanical camera-angle split: Claude writes genuinely
        # different visual moments for every missing slot.
        _actual_now = len(script.get("scenes") or [])
        if _actual_now < expected_scenes:
            _missing = expected_scenes - _actual_now
            print(f"[autopilot] filling {_missing} missing scenes via Claude",
                  flush=True)
            try:
                raw_fill = claude.generate_missing_scenes(
                    existing_scenes=script.get("scenes") or [],
                    needed=_missing,
                    voiceover=script.get("voiceover", ""),
                    style_notes=style_notes,
                    master_prompt=st["master_prompt"],
                )
                fill_data = extract_json(raw_fill)
                new_scenes = fill_data.get("scenes") or []
                for sc in new_scenes:
                    if sc.get("prompt"):
                        sc["prompt"] = _sanitize_prompt(sc["prompt"])
                if new_scenes:
                    script["scenes"] = (script.get("scenes") or []) + new_scenes
                    print(f"[autopilot] after fill: "
                          f"{len(script['scenes'])} scenes", flush=True)
            except Exception as fill_ex:
                print(f"[autopilot] Claude fill failed ({fill_ex}); using split",
                      flush=True)
                script = _split_script_scenes(script, expected_scenes)

        # ── Trim if over (shouldn't happen, but be safe) ───────────────────────
        if len(script.get("scenes") or []) > expected_scenes:
            script["scenes"] = script["scenes"][:expected_scenes]

        # Normalize alias field names (visual->prompt, scene->n, caption->frame text)
        # across ALL scenes — including any added by the retry/fill/split paths —
        # so every frame renders from its real VISUAL description, not the narration.
        script["scenes"] = _normalize_scenes(script.get("scenes"))
        script["scene_count"] = len(script.get("scenes") or [])
        print(f"[autopilot] final scene count: {script['scene_count']}", flush=True)

        # ── Sanitize all prompts ───────────────────────────────────────────────
        for sc in (script.get("scenes") or []):
            if sc.get("prompt"):
                sc["prompt"] = _sanitize_prompt(sc["prompt"])
        for ch in (script.get("characters") or []):
            if ch.get("sheet_prompt"):
                ch["sheet_prompt"] = _sanitize_prompt(ch["sheet_prompt"])

        # ── Character count enforcement ────────────────────────────────────────
        # Only enforce when the user set an explicit count (>0). When auto-cast
        # (num_chars=-1), target_chars would be 0 and this would delete ALL
        # characters — the script model decided the cast size, trust it.
        target_chars = max(0, num_chars)
        raw_chars = script.get("characters") or []
        if target_chars > 0 and len(raw_chars) > target_chars:
            script["characters"] = raw_chars[:target_chars]

        st = store.load_state()
        st["script"] = script
        store.save_state(st)
        steps.append({"step": "script", "scenes": len(script.get("scenes") or []),
                      "characters": len(script.get("characters") or [])})
        store.log_usage("script", 1, 0.01)

    # ── STEP 2.5: Claude cut planner ──────────────────────────────────────
    # Claude assigns a hold_seconds to every scene BEFORE image generation so
    # the video editor knows exactly how long each frame appears on screen.
    # This is the "planner" step — it uses narration energy, line length, and
    # story beats to choose cut points rather than equal-time splits.
    _check_stop(run_id)
    _ap_prog(run_id, step="plan")
    _vo_lines = [(sc.get("vo") or "").strip()
                 for sc in (script.get("scenes") or [])]
    if _vo_lines and body.smart_edit:
        try:
            _planned = claude.edit_holds(_vo_lines, total_dur)
            if _planned and len(_planned) == len(_vo_lines):
                for i, sc in enumerate(script["scenes"]):
                    sc["planned_hold"] = _planned[i]
                _sf_plan = store.load_state()
                _sf_plan["script"] = script
                store.save_state(_sf_plan)
                print(f"[autopilot] cut plan: "
                      f"{[round(h,2) for h in _planned]}", flush=True)
        except Exception as plan_ex:
            print(f"[autopilot] planner skipped: {plan_ex}", flush=True)
    steps.append({"step": "plan",
                  "holds": [sc.get("planned_hold") for sc in
                             (script.get("scenes") or [])]})

    # ---- STEP 3: Generate character sheets ----
    _check_stop(run_id)
    chars = script.get("characters") or []
    # DEDUP THE CAST by normalized name BEFORE counting / rendering. Claude
    # sometimes returns the same character twice (exact repeat, or "Tom"/"tom"/
    # "Tom " casing+whitespace variants). If we don't collapse them here:
    #   • chars_total = len(chars) overcounts (e.g. 3) while only the unique
    #     sheets persist to state (2) → the cast counter sticks at "2/3" forever
    #     even though every REAL character succeeded ("1/2 generated even tho it
    #     used 2" bug);
    #   • a duplicate sheet is rendered and paid for needlessly.
    # Keep first occurrence, preserve order; un-named entries are dropped (the
    # loop skips them anyway). This makes the counter total == real cast size.
    _seen_names, _uniq_chars = set(), []
    for _c in chars:
        _nm = (_c.get("name") or "").strip()
        _key = _nm.lower()
        if not _nm or _key in _seen_names:
            continue
        _seen_names.add(_key)
        _uniq_chars.append(_c)
    if len(_uniq_chars) != len(chars):
        print(f"[autopilot] cast dedup: {len(chars)} listed -> "
              f"{len(_uniq_chars)} unique character(s)", flush=True)
    chars = _uniq_chars
    # PROTAGONIST LOCK seed: the story's central subject, written into state so
    # every frame render pins it as the mandatory central figure (prevents the
    # "predator scene drops the hero" bug — e.g. a baby-elephant video rendering
    # standalone leopards). Lead = first named character; enrich with the title's
    # subject noun when available so the lock reads naturally.
    try:
        _lead_names = [(_c.get("name") or "").strip()
                       for _c in chars if (_c.get("name") or "").strip()]
        _protag = ", ".join(_lead_names[:2]) if _lead_names else ""
        if not _protag:
            # No named cast — fall back to the title's main subject.
            _ttl = (script.get("title") or title or "").strip()
            _protag = _ttl
        if _protag:
            _ps = store.load_state()
            _ps["protagonist"] = _protag
            store.save_state(_ps)
            print(f"[autopilot] protagonist lock: {_protag}", flush=True)
    except Exception as _pl_ex:
        print(f"[autopilot] protagonist seed skipped: {_pl_ex}", flush=True)
    _scene_total = len([sc for sc in (script.get("scenes") or [])
                        if (sc.get("prompt") or "").strip()])
    # Seed chars_done from any sheets already on disk so the counter starts at
    # the real value (e.g. on RESUME) instead of flashing "0/N".
    try:
        with _state_write_lock:
            _seed_state = store.load_state()
        _seed_done = len([c for c in (_seed_state.get("characters") or [])
                          if c.get("sheet_url")])
    except Exception:
        _seed_done = 0
    _ap_prog(run_id, step="characters", chars_total=len(chars),
             chars_done=min(_seed_done, len(chars)), frames_total=_scene_total)
    img_client = get_image_client(request)
    char_created = []
    char_fatal_err = None
    # RESUME: names that already have a saved sheet on disk — don't re-render.
    _existing_char_names = set()
    if _resume:
        _existing_char_names = {
            (c.get("name") or "").strip().lower()
            for c in (store.load_state().get("characters") or [])
            if c.get("sheet_url") and (c.get("name") or "").strip()
        }
        if _existing_char_names:
            print(f"[autopilot] RESUME: {len(_existing_char_names)} character "
                  f"sheet(s) already on disk — skipping those", flush=True)
    for c in chars:
        _check_stop(run_id)
        name = (c.get("name") or "").strip()
        sheet_prompt = (c.get("sheet_prompt") or c.get("description") or "").strip()
        if not name:
            continue
        if _resume and name.lower() in _existing_char_names:
            char_created.append(name)   # count it as done
            continue

        _sheet_box = {}

        def _make_sheet(name=name, sheet_prompt=sheet_prompt, _sn=style_notes):
            _cur = store.load_state()
            prompt = pipeline.build_sheet_prompt(
                _cur["master_prompt"], name, sheet_prompt,
                style_notes=(_cur.get("style_notes") or _sn or ""))
            # Anchor the character's ART STYLE to the uploaded reference video's
            # pinned frames (the same ones the scene images use) so characters
            # match the source look — not just the text description.
            _style_refs, _labels = [], []
            for _sf in (_cur.get("style_frames") or [])[:config.STYLE_REF_COUNT]:
                try:
                    _style_refs.append(store.read_image(_sf["url"]))
                    _labels.append("STYLE REF — match art style ONLY")
                except Exception:
                    pass
            if _style_refs:
                _multi = bool(request and request.state.settings.get("multi_image_edit"))
                _edit_prompt = (prompt + "\n\nReproduce the EXACT art style of the "
                                "attached reference frame(s) from the source video — "
                                "same rendering technique, colour palette, line work, "
                                "shading, texture and proportions. Draw THIS character "
                                "in that style; do not copy the people, poses or "
                                "scenes in the reference frames.")
                if _multi and len(_style_refs) > 1:
                    _edit_prompt += ("\n\nATTACHED REFERENCE IMAGES (in order) — "
                                     "every one is a STYLE REF (source-video frame). "
                                     "Use them ONLY for art style; ignore their "
                                     "content, composition and characters.")
                _send = (_style_refs if _multi
                         else ([pipeline.contact_sheet(_style_refs, labels=_labels,
                                                       target_size=size)]
                               if len(_style_refs) > 1 else _style_refs))
                img = img_client.edit(_edit_prompt, _send, size=size, quality=quality,
                                      multi_image_edit=_multi)
            else:
                img = img_client.generate(prompt, size=size, quality=quality)
            sheet_url = store.write_image("characters", img)
            rec = {
                "id": store.new_id("char"), "name": name,
                "description": sheet_prompt,
                "sheet_url": sheet_url,
                "prompt": prompt, "source": "generated",
            }
            with _state_write_lock:
                cur = store.load_state()
                cur["characters"].append(rec)
                store.save_state(cur)
            store.log_usage("image", 1, 0.08)
            _sheet_box["url"] = sheet_url
            return name
        # Run the sheet INLINE (no deadline) — character sheets are the
        # foundation of every frame; better to take the time than to lose
        # results to a daemon-thread timeout. The image queue still enforces
        # its own backoff/retries for upstream 502s.
        try:
            val = _make_sheet()
            if val:
                char_created.append(val)
        except Exception as ce:
            cmsg = str(ce)
            print(f"[autopilot] character sheet '{name}' FAILED: {cmsg}", flush=True)
            # Account-level image failures (no credits / bad key) will fail every
            # remaining sheet AND every frame — stop and surface the real cause
            # instead of silently producing zero sheets.
            if _is_fatal_image_error(cmsg):
                char_fatal_err = cmsg
                print("[autopilot] aborting character sheets — image account is "
                      "out of credits or the key is invalid", flush=True)
                break
            # Non-fatal (transient 502 / rate-limit / one-off): retry ONCE before
            # giving up on this character, so "2 requested" doesn't silently
            # become "1 made" on a single hiccup. image_queue already backs off
            # internally; this is a top-level second attempt for the whole sheet.
            try:
                print(f"[autopilot] retrying character sheet '{name}' once…",
                      flush=True)
                time.sleep(2.0)
                val = _make_sheet()
                if val:
                    char_created.append(val)
            except Exception as ce2:
                cmsg2 = str(ce2)
                print(f"[autopilot] character sheet '{name}' retry FAILED: {cmsg2}",
                      flush=True)
                if _is_fatal_image_error(cmsg2):
                    char_fatal_err = cmsg2
                    break
        # Drive chars_done from the PERSISTED state (source of truth), not the
        # in-memory char_created accumulator. _make_sheet saves the sheet to
        # state.characters BEFORE returning; if a sheet finishes between our
        # deadline check and the next iteration, this picks it up. Also handles
        # the case where a previous run was stopped mid-sheet — those sheets
        # are already in state and shouldn't be re-counted as "in progress".
        # Drive chars_done from the PERSISTED state (source of truth), but read
        # it UNDER the state lock so we never observe a half-written file mid-
        # save (which returns 0 characters and made the counter jump 1/2 -> 0/2).
        # Also floor it by the in-memory char_created count so the number can
        # only ever go UP within a run — a progress counter must be monotonic.
        try:
            with _state_write_lock:
                _cur_state = store.load_state()
            _persisted_chars = [c for c in (_cur_state.get("characters") or [])
                                if c.get("sheet_url")]
            _chars_done_so_far = max(len(_persisted_chars), len(char_created))
        except Exception:
            _chars_done_so_far = len(char_created)
        # Only push last_image_url when we actually have one — passing None
        # would blank a just-shown sheet preview on the node. Keep it sticky.
        # NOTE: a character sheet must NOT go into last_image_url, because that
        # feeds the SCENE-FRAMES gallery (recent_frames) and would mix cast
        # sheets into the frames box. Sheets go ONLY to last_character (the
        # separate Cast box). The single-latest node preview reads either field.
        _prog_kw = {"chars_done": _chars_done_so_far}
        if _sheet_box.get("url"):
            _prog_kw["last_character"] = {"url": _sheet_box["url"], "name": name}
        _ap_prog(run_id, **_prog_kw)
    if char_fatal_err:
        char_err_hint = ("character sheets failed — image account problem: "
                         f"{char_fatal_err[:240]}")
    elif chars and not char_created:
        # char_created empty — but a sheet saved to state would mean the run
        # made progress. Check the persisted count so we don't claim "0 of N"
        # when at least one is actually saved. The state read in the loop
        # above already updated the progress counter, so this message just
        # needs to mirror it accurately.
        try:
            _final_state = store.load_state()
            _final_chars = [c for c in (_final_state.get("characters") or [])
                            if c.get("sheet_url")]
        except Exception:
            _final_chars = []
        if _final_chars:
            # At least some sheets saved — show partial count, not zero.
            char_err_hint = (f"{len(_final_chars)} of {len(chars)} character "
                             f"sheets saved before the step ended (some were "
                             f"still rendering in the background). The script "
                             f"step can proceed; re-run for any missing.")
        else:
            char_err_hint = (f"0 of {len(chars)} character sheets rendered — check the "
                             "image API key/credits and size in Settings")
    else:
        char_err_hint = None
    steps.append({"step": "characters", "created": len(char_created),
                  "requested": len(chars), "error": char_err_hint})

    # ---- STEP 4: Batch render sequence frames ----
    _check_stop(run_id)
    _ap_prog(run_id, step="frames")
    scenes = list(_unwrap_script(script).get("scenes") or [])
    scenes = _normalize_scenes(scenes)  # map visual/image_prompt -> prompt

    # Hard guarantee: if split / retry still left fewer scenes than expected,
    # extend by repeating the last scene's prompt with progressive moment cues.
    _final_expected = max(1, round(total_dur / max(0.1, pacing)))
    while len(scenes) < _final_expected:
        _last = scenes[-1] if scenes else {}
        _base_p = (_last.get("prompt") or "").strip()
        _cue = _angle_cue_for(len(scenes))
        # Always vary the padded frame so it isn't a byte-identical clone of the
        # previous one (which _normalize_scenes would then dedup away, leaving us
        # short again). If no angle cue is available, fall back to a frame-index
        # beat tag so each pad is visually distinct.
        if not _cue:
            _cue = f"continuation beat {len(scenes) + 1}"
        scenes.append({
            "n": len(scenes) + 1,
            "heading": _last.get("heading", "continuation"),
            "action": "", "vo": "",   # no spoken line — never doubles narration
            "prompt": f"{_base_p}, {_cue}" if _base_p else _base_p,
        })
    print(f"[autopilot] rendering {len(scenes)} frames "
          f"(expected={_final_expected}, pacing={pacing}s, dur={total_dur}s)",
          flush=True)

    # Map character names -> sheet ids so each scene attaches the RIGHT sheets.
    _cur_chars = store.load_state().get("characters") or []

    def _scene_char_ids(scene, render_prompt):
        """Which character sheets belong on THIS frame: match names across the
        scene's prompt/action/vo (broader than the render prompt alone). For a
        single-protagonist video, keep that one character present even when a
        scene doesn't name them, so the lead stays consistent frame-to-frame."""
        if not _cur_chars:
            return None
        hay = " ".join([render_prompt or "", scene.get("prompt", ""),
                        scene.get("action", ""), scene.get("vo", "")])
        ids = [c["id"] for c in pipeline.match_characters(hay, _cur_chars)]
        if not ids and len(_cur_chars) == 1:
            ids = [_cur_chars[0]["id"]]
        return ids or None

    angle_on = bool(getattr(body, "angle_variety", True))

    frames_ok, frames_fail = 0, 0
    fatal_image_err = None
    _skipped_blank = 0
    # RESUME: collect the scene numbers that already have frames on disk.
    # Using scene `n` (not positional index) avoids off-by-one when blank
    # scenes were skipped in the first run (len(sequence) != leading scene count).
    _rendered_scene_ns = set()
    if _resume:
        _seq = store.load_state().get("sequence") or []
        _rendered_scene_ns = {r.get("scene_n") for r in _seq if r.get("scene_n")}
        if _rendered_scene_ns:
            print(f"[autopilot] RESUME: {len(_rendered_scene_ns)} frame(s) already "
                  f"rendered (scenes {sorted(_rendered_scene_ns)}) — skipping them",
                  flush=True)
            _ap_prog(run_id, frames_done=len(_rendered_scene_ns), frames_failed=0)
    for _scene_i, sc in enumerate(scenes):
        _check_stop(run_id)
        _sc_n = sc.get("n")
        if _resume and _sc_n and _sc_n in _rendered_scene_ns:
            frames_ok += 1
            continue   # this scene's frame is already on disk
        p = (sc.get("prompt") or "").strip()
        if not p:
            # Don't silently drop a scene with no image prompt — that produced
            # the "0 frames rendered (0 failed)" dead-end. Synthesize a prompt
            # from whatever the scene does have (action / vo / heading) plus the
            # project style notes, so every scene still yields a frame.
            _bits = [b for b in [sc.get("action"), sc.get("vo"),
                                 sc.get("heading")] if (b or "").strip()]
            p = _sanitize_prompt(", ".join(_bits).strip())
            if style_notes:
                p = (p + ". " + style_notes).strip(" .") if p else style_notes
            if not p:
                _skipped_blank += 1
                print(f"[autopilot] scene={sc.get('n','?')} has no prompt/action/vo "
                      "— skipped", flush=True)
                continue
            print(f"[autopilot] scene={sc.get('n','?')} had no prompt — "
                  f"synthesized from action/vo: {p[:70]}", flush=True)
        # Micro-cut angle variety: append a cycling camera cue so consecutive
        # frames change angle (close-up -> wide -> low -> cutaway -> ...), unless
        # the prompt already specifies its own framing.
        if angle_on and not _has_camera_language(p):
            _cue = _angle_cue_for(_scene_i)
            if _cue:
                p = f"{p}, {_cue}"
        # Resolve which character sheets anchor this exact frame.
        _scene_ids = _scene_char_ids(sc, p)
        # Each frame self-heals via the image_queue throttle (backoff + cooldown
        # on rate limits) so the run no longer dies on a single 429.
        try:
            rec = _render_one(p, size, quality, True, True,
                              character_ids=_scene_ids, request=request,
                              shot_relation=sc.get("shot_relation", "cut"),
                              scene_vo=(sc.get("vo") or sc.get("narration")
                                        or sc.get("voice_over") or ""),
                              scene_n=sc.get("n"))
            frames_ok += 1
            _fr_kw = {"frames_done": frames_ok + frames_fail,
                      "frames_failed": frames_fail}
            if rec.get("image_url"):
                _fr_kw["last_image_url"] = rec["image_url"]
            _ap_prog(run_id, **_fr_kw)
        except Exception as ex:
            frames_fail += 1
            msg = str(ex)
            print(f"[autopilot] frame scene={sc.get('n','?')} FAILED: {msg}", flush=True)
            _ap_prog(run_id, frames_done=frames_ok + frames_fail,
                     frames_failed=frames_fail)
            # Account-level failures (no credits / bad key) will fail every
            # remaining frame too — stop burning attempts and surface the cause.
            if _is_fatal_image_error(msg):
                fatal_image_err = msg
                print("[autopilot] aborting remaining frames — image account "
                      "is out of credits or the key is invalid", flush=True)
                break
    if frames_fail and fatal_image_err:
        frame_err_hint = (f"{frames_fail} frame(s) failed — image account problem: "
                          f"{fatal_image_err[:240]}")
    elif frames_fail:
        frame_err_hint = (f"{frames_fail}/{frames_fail+frames_ok} frames failed — "
                          "check image API key/size in Settings")
    else:
        frame_err_hint = None
    steps.append({"step": "frames", "rendered": frames_ok, "failed": frames_fail,
                  "error": frame_err_hint})

    # ---- STEP 5: Voice-over + video assembly (natural flow) ----
    #  ONE continuous narration track + frames timed to it + micro-cuts on long
    #  holds — the audio is never chopped per-scene, so it sounds natural.
    _check_stop(run_id)
    _ap_prog(run_id, step="video")
    st = store.load_state()
    seq = st.get("sequence") or []
    video_url = None
    total_seconds = 0
    video_err = None
    vo_text = _script_voiceover_text(st)
    min_frames = max(2, int(_final_expected * 0.5)) if _final_expected > 1 else 1
    if not seq:
        if frames_ok == 0 and frames_fail == 0:
            # Nothing was even attempted — the script came back with no usable
            # scene prompts (often because the LLM/router was unreachable).
            video_err = (
                f"No frames were rendered — the script produced no image prompts "
                f"({_skipped_blank} empty scene(s), 0 attempted). This usually means "
                "the Claude/router connection failed during script generation. "
                "Check the Claude connection in Settings (or start your 9Router), "
                "then re-run.")
        else:
            video_err = (f"No frames were rendered ({frames_fail} failed). "
                         + (f"Cause: {fatal_image_err[:200]}" if fatal_image_err else
                            "Check your image API key and size in Settings, then re-run."))
        print(f"[autopilot] video step skipped — no frames in sequence: {video_err}", flush=True)
    elif len(seq) < min_frames:
        video_err = (f"Only {len(seq)} of {_final_expected} frames rendered — "
                     "refusing to build a broken video. "
                     + (f"Cause: {fatal_image_err[:200]}. " if fatal_image_err else "")
                     + "Fix the image account in Settings, render the missing "
                       "frames (Sequence tab), then use 🔁 Build video.")
        print(f"[autopilot] video step skipped — too few frames: {video_err}", flush=True)
    elif not vo_text:
        video_err = "No voice-over text found in script."
        print(f"[autopilot] video step skipped — {video_err}", flush=True)
    else:
        # Awaited fully (NOT under a deadline): it's the core deliverable, and a
        # backgrounded build would later save a stale state and clobber the
        # thumbnail/SEO saved after it. Its sub-calls (TTS/ffmpeg) are bounded.
        try:
            _edit_rec, video_url, total_seconds, _sm = _build_flow_video(
                st, request, voice_id=voice_id, text=vo_text,
                transition=body.transition, width=width,
                height=height, fps=body.fps, max_hold=body.max_hold,
                motion=body.motion, name_hint="autopilot_vo",
                sound_design=body.sound_design,
                smart_edit=body.smart_edit,
                cut_clicks=body.cut_clicks,
                cut_click_volume=body.cut_click_volume,
                cut_click_style=body.cut_click_style,
                target_seconds=total_dur)
        except HTTPException as he:
            video_err = str(he.detail)
            print(f"[autopilot] video step FAILED: {he.detail}", flush=True)
        except Exception as ex:
            video_err = str(ex)
            print(f"[autopilot] video step FAILED: {ex}", flush=True)

    steps.append({"step": "video", "url": video_url,
                  "duration": total_seconds, "scenes_voiced": len(seq),
                  "error": video_err})

    # ---- STEP 6: Thumbnail ----
    _check_stop(run_id)
    _ap_prog(run_id, step="thumbnail", video_url=video_url)
    thumb_url = None
    try:
        thumb_title = title or (script.get("title") or "")
        style_hint = analysis.get("style_summary") or st.get("master_prompt", "")
        st = store.load_state()
        # Sample the LOOK from the uploaded reference video: its pinned style
        # frames are the primary style/composition guide for the thumbnail, so
        # the thumbnail matches the source video instead of a generic render.
        style_frame_urls = [sf["url"] for sf in (st.get("style_frames") or [])][:3]
        seq_urls = [fr["image_url"] for fr in (st.get("sequence") or [])]
        subject_url = seq_urls[0] if seq_urls else None   # the hook frame = hero

        refs, ref_labels = [], []
        for u in style_frame_urls:
            try:
                refs.append(store.read_image(u))
                ref_labels.append("STYLE REF — match this look")
            except Exception:
                pass
        if subject_url:
            try:
                refs.append(store.read_image(subject_url))
                ref_labels.append("SUBJECT")
            except Exception:
                pass

        bits = []
        if thumb_title.strip():
            bits.append(f'Design a BOLD, scroll-stopping YouTube thumbnail (16:9) for: "{thumb_title}".')
        else:
            bits.append("Design a BOLD, scroll-stopping YouTube thumbnail (16:9).")
        if style_frame_urls:
            bits.append("Reproduce the EXACT art style of the attached cells labelled "
                        "\"STYLE REF\" (frames from the source video) — same rendering, "
                        "palette, line work and texture." + (f" Style notes: {style_hint[:160]}" if style_hint else ""))
        elif style_hint:
            bits.append(f"Match this visual style exactly: {style_hint[:200]}")
        bits.append(
            "Make it click-worthy like the best YouTube thumbnails: ONE large "
            "expressive focal subject with exaggerated emotion or dramatic action, "
            "pushed-up contrast and saturation, punchy complementary colours, "
            "strong rim/back lighting, clear depth and crisp separation from the "
            "background, a touch of wide-angle drama. Rule-of-thirds composition; "
            "keep clean, uncluttered negative space on ONE side (a title will be "
            "added later as a separate overlay). Ultra-crisp and professional.")
        # CRITICAL: the image model must render ZERO text. The title is baked on
        # afterwards by Pillow (_overlay_text). When the model ALSO renders title
        # text into the image you get DOUBLE titles + cropped/garbled words +
        # stray watermark words (the "ELEPHANT" + duplicated-title bug). Forbid
        # all text/letters/watermarks/signs in the generated art itself.
        bits.append(
            "ABSOLUTELY NO TEXT of any kind in the image: no title, no caption, "
            "no letters, no words, no numbers, no watermark, no logo, no signage, "
            "no UI, no subtitles, no labels, no borders or frames. Render ONLY "
            "the illustration/photo — a completely text-free image. Any text in "
            "the output is a failure.")
        # Match the source video's flat cartoon look for flat/stick-figure styles.
        if pipeline._is_flat_style(style_hint, st.get("master_prompt", "")):
            bits.append(pipeline._FLAT_DIRECTIVE)
        prompt = "\n".join(bits)

        if refs:
            # Full-res separate refs when the endpoint supports multi-image,
            # else composite into ONE labeled grid (single-image edit path).
            _multi = bool(request.state.settings.get("multi_image_edit"))
            send = (refs if _multi
                    else ([pipeline.contact_sheet(refs, labels=ref_labels)]
                          if len(refs) > 1 else refs))
            img = img_client.edit(prompt=prompt, images=send,
                                  size="1536x1024", quality=quality)
        else:
            img = img_client.generate(prompt, size="1536x1024", quality=quality)

        thumb_raw_url = store.write_image("images", img)
        st = store.load_state()
        brand = st.get("brand") or {}
        overlay_png = _overlay_text(img, thumb_title, "", "top",
                                     "#ffffff", True, brand)
        final_url = store.write_image("images", overlay_png)
        thumb_rec = {
            "id": store.new_id("thumb"), "raw_url": thumb_raw_url,
            "url": final_url, "title": thumb_title, "created": store.now(),
        }
        st.setdefault("thumbnails", []).append(thumb_rec)
        store.save_state(st)
        thumb_url = final_url
        store.log_usage("thumbnail", 1, 0.08)
    except Exception as _th_ex:
        print(f"[autopilot] thumbnail step failed: {_th_ex}", flush=True)

    steps.append({"step": "thumbnail", "url": thumb_url})

    # ---- STEP 7: YouTube SEO pack (title options, description, tags) ----
    _check_stop(run_id)
    _ap_prog(run_id, step="seo", video_url=video_url)
    seo = None
    try:
        st = store.load_state()
        scr = st.get("script") or {}
        seo_title = title or scr.get("title") or ""
        seo_desc = "\n".join(filter(None, [
            scr.get("logline") or pick.get("logline") or "",
            (scr.get("voiceover") or "")[:1200],
        ]))
        ok, raw = _run_with_deadline(
            lambda: _claude_client_for(body.model, request).seo(seo_title, seo_desc, 6),
            step_to)
        if ok and raw:
            seo = extract_json(raw)
            st = store.load_state()
            st["seo"] = seo
            store.save_state(st)
            store.log_usage("script", 1, 0.005)
    except Exception as ex:
        print(f"[autopilot] seo step skipped: {ex}", flush=True)
    steps.append({"step": "seo", "ok": bool(seo)})

    _ap_prog(run_id, done=True, video_url=video_url)
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_STOP.discard(run_id)
    return {
        "ok": True,
        "run_id": run_id,
        "steps": steps,
        "suggestion": pick,
        "video_url": video_url,
        "video_error": video_err,
        "frame_error": frame_err_hint,
        "thumbnail_url": thumb_url,
        "total_duration": total_seconds,
        "seo": seo,
    }


# --------------------------------------------------------------------------- #
#  Build-video: assemble ElevenLabs voice + SFX + frames into MP4 from current
#  project state — usable standalone or as a retry after autopilot frame fails.
# --------------------------------------------------------------------------- #
class BuildVideoIn(BaseModel):
    voice_id: Optional[str] = None
    transition: str = "cut"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    max_hold: float = 2.5
    motion: bool = False
    sound_design: bool = True
    text_override: Optional[str] = None   # use custom VO text instead of script
    cut_clicks: bool = False
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"
    manual_holds: Optional[List[float]] = None   # per-frame seconds from Review popup
    force_continuous: bool = True   # always synth ONE continuous track (no per-scene chop)


@app.post("/api/build-video")
def api_build_video(body: BuildVideoIn, request: Request):
    """Assemble ElevenLabs voice-over + SFX + rendered frames into a final MP4.
    Works on the current project state — call this after rendering frames when
    the autopilot's video step failed, or to rebuild the video with new settings."""
    s = request.state.settings
    if not _has_voice_key(s):
        raise HTTPException(400, "No voice/TTS key — add ElevenLabs or MiMo in Settings.")
    st = store.load_state()
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "No frames in sequence — render frames first (Sequence tab).")
    voice_id = body.voice_id or _voice_default_id(s)
    text = (body.text_override or "").strip() or _script_voiceover_text(st)
    if not text:
        raise HTTPException(400, "No voice-over text — generate a script first.")
    _edit_rec, video_url, total_seconds, scene_map = _build_flow_video(
        st, request, voice_id=voice_id, text=text,
        transition=body.transition, width=body.width, height=body.height,
        fps=body.fps, max_hold=body.max_hold, motion=body.motion,
        name_hint="build_video", sound_design=body.sound_design,
        cut_clicks=body.cut_clicks, cut_click_volume=body.cut_click_volume,
        cut_click_style=body.cut_click_style, manual_holds=body.manual_holds,
        force_continuous=body.force_continuous)
    return {"ok": True, "video_url": video_url, "duration": total_seconds,
            "frames": len(seq), "scene_map": scene_map}


# =========================================================================== #
#  AUDIO -> VIDEO  (separate tab — NOT part of the YouTube autopilot workflow)
#  Upload your own audio + a sample video:
#    1. sample video  -> art-style analysis (vision) + pinned style frames
#    2. audio         -> Whisper transcription with word timestamps
#    3. Claude writes ONE visual scene per transcript segment (vo = your words)
#    4. character sheets auto-cast + style-anchored to the sample video
#    5. frames rendered (style-locked, micro-cut continuity)
#    6. final MP4 = your audio + frames cut to the REAL word timestamps
# =========================================================================== #
class AudioToVideoIn(BaseModel):
    audio_path: str                       # uploaded via /api/audio-to-video/upload
    sample_video_path: Optional[str] = None
    sample_frame_urls: Optional[List[str]] = None  # pre-extracted style frames
    style_notes: Optional[str] = None     # manual override of analysed style
    orientation: str = "landscape"        # landscape | portrait | square
    size: Optional[str] = None
    quality: Optional[str] = None
    transition: str = "cut"
    fps: int = 30
    max_hold: float = 1.6                 # fast micro-cut ceiling for retention
    motion: bool = True                   # subtle Ken-Burns push for energy
    dynamic: bool = True                  # high-retention reacting visuals
    cut_clicks: bool = False
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"
    language: Optional[str] = None        # force language (else auto)
    transcribe_engine: str = "local"      # local (faster-whisper) | elevenlabs (Scribe)


@app.post("/api/audio-to-video/upload")
async def api_a2v_upload(file: UploadFile = File(...), kind: str = Form("audio")):
    """Save an uploaded audio or sample-video file for the Audio->Video tab.
    ``kind`` is 'audio' or 'video'. Returns the server-side path."""
    raw_name = file.filename or ("audio.mp3" if kind == "audio" else "video.mp4")
    # Sanitize: strip path separators and traversal to prevent escape from uploads dir.
    safe = os.path.basename(raw_name).replace("..", "").replace("/", "").replace("\\", "")
    if not safe:
        safe = "audio.mp3" if kind == "audio" else "video.mp4"
    dest = os.path.join(store.UPLOADS_DIR, store.new_id("a2v") + "_" + safe)
    os.makedirs(store.UPLOADS_DIR, exist_ok=True)
    _data = await file.read()
    if not _data:
        raise HTTPException(400, "Empty file — upload a real audio/video.")
    with open(dest, "wb") as f:
        f.write(_data)
    out = {"ok": True, "path": dest, "kind": kind}
    # For a sample video, extract style frames right away so the UI can preview.
    if kind == "video":
        try:
            import video as videomod
            out["frames"] = videomod.extract_frames(dest, fps=0.5, max_frames=12)
        except Exception as e:
            out["frames"] = []
            out["frame_error"] = str(e)
    return out


class A2VSampleLinkIn(BaseModel):
    url: str


@app.post("/api/audio-to-video/sample-link")
def api_a2v_sample_link(body: A2VSampleLinkIn):
    """Pull style frames from a pasted sample-video LINK (YouTube etc.) for the
    Audio->Video tab — same output shape as the upload endpoint, so the front
    end can use either interchangeably. Returns the extracted style frames +
    the downloaded video path (usable as sample_video_path)."""
    import youtube
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, "Paste a sample-video link.")
    if not youtube.is_youtube_url(url):
        raise HTTPException(400, "That doesn't look like a YouTube link.")
    try:
        frames, path = youtube.download_frames(url, max_frames=12)
    except Exception as e:
        raise HTTPException(500, f"Couldn't fetch frames from that link: {e}")
    if not frames:
        # Fall back to the hi-res thumbnail so the user still gets a style anchor.
        try:
            vid = youtube.extract_video_id(url)
            meta = youtube.fetch_metadata(url, vid)
            thumb = youtube.thumbnail_bytes(vid, meta.get("thumbnail", ""))
            if thumb:
                turl = store.write_image("uploads", thumb)
                return {"ok": True, "path": path, "kind": "video",
                        "frames": [turl], "note": "used thumbnail (video frames unavailable)"}
        except Exception:
            pass
        raise HTTPException(502, "Couldn't extract frames or a thumbnail from that "
                            "link (age-gated / region-locked / throttled). Try another.")
    return {"ok": True, "path": path, "kind": "video", "frames": frames}


def _a2v_analyze_style(claude, frame_urls):
    """Vision-analyse a few sample-video frames into a concrete art-style brief
    the image model can reproduce. Returns a style string ('' on failure)."""
    imgs = []
    for u in (frame_urls or [])[:6]:
        try:
            imgs.append(store.read_image(u))
        except Exception:
            pass
    if not imgs:
        return ""
    instr = (
        "These are frames from a reference video. Describe its ART STYLE so an "
        "image generator can reproduce it EXACTLY: rendering technique (flat 2D / "
        "3D / photoreal / anime / cartoon), line weight, colour palette, shading, "
        "lighting, texture, character proportions and overall mood. Be concrete and "
        "concise (5-8 lines). Describe ONLY the look, not the content."
    )
    try:
        return (claude.vision_describe(
            imgs, instr,
            system="You are an art director who reverse-engineers visual styles.",
            max_tokens=900) or "").strip()
    except Exception as e:
        print(f"[a2v] style analysis failed: {e}", flush=True)
        return ""


@app.post("/api/audio-to-video")
def api_audio_to_video(body: AudioToVideoIn, request: Request):
    """One-click Audio->Video. Self-contained: does NOT touch the YouTube
    autopilot state machine. Builds a fresh project from the uploaded audio +
    sample video and returns the final MP4."""
    import transcribe
    import video as videomod

    s = request.state.settings
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude in Settings.")
    if not _has_image_key(request):
        raise HTTPException(400, "No image API key — add it in Settings.")
    engine = (body.transcribe_engine or "local").lower()
    if engine in ("elevenlabs", "scribe", "11labs"):
        if not (s.get("elevenlabs_api_key") or getattr(config, "ELEVENLABS_API_KEY", "")):
            raise HTTPException(400, "ElevenLabs Scribe selected but no ElevenLabs "
                                "key set — add it in Settings or switch to Local Whisper.")
    else:
        if not transcribe.local_available():
            raise HTTPException(400, "Local Whisper isn't installed. Run "
                                "'pip install faster-whisper' once, or switch the "
                                "transcription engine to ElevenLabs Scribe.")
    # Guard: audio_path must be a real file under the uploads directory to
    # prevent path traversal (client sends a raw string, could be /etc/passwd).
    _audio_real = os.path.realpath(body.audio_path)
    _uploads_real = os.path.realpath(store.UPLOADS_DIR)
    if not _audio_real.startswith(_audio_real[:2] == _uploads_real[:2] and _uploads_real or store.UPLOADS_DIR):
        pass  # cross-drive on Windows, fall through to existence check
    elif not _audio_real.startswith(_uploads_real):
        raise HTTPException(400, "Audio path must be an uploaded file — not a server path.")
    if not os.path.exists(body.audio_path):
        raise HTTPException(400, "Uploaded audio not found — upload it again.")

    claude = _claude_client_for(None, request)

    # ---- orientation / size ----
    orient = (body.orientation or "landscape").lower()
    if orient in ("vertical", "portrait", "9:16", "shorts", "tiktok"):
        size = body.size or "1024x1536"; width, height = 1080, 1920
    elif orient in ("square", "1:1"):
        size = body.size or "1024x1024"; width, height = 1080, 1080
    else:
        size = body.size or "1536x1024"; width, height = 1920, 1080
    quality = body.quality or config.DEFAULT_QUALITY

    # ---- STEP 1: sample-video style frames + style analysis ----
    frame_urls = list(body.sample_frame_urls or [])
    if not frame_urls and body.sample_video_path and os.path.exists(body.sample_video_path):
        try:
            frame_urls = videomod.extract_frames(
                body.sample_video_path, fps=0.5, max_frames=12)
        except Exception as e:
            print(f"[a2v] frame extraction failed: {e}", flush=True)
    style_picks = frame_urls[::max(1, len(frame_urls) // 4)][:4] if frame_urls else []
    style_notes = (body.style_notes or "").strip() or _a2v_analyze_style(claude, frame_urls)

    # ---- STEP 2: transcribe the uploaded audio (word timestamps) ----
    try:
        tr = transcribe.transcribe_audio(body.audio_path, settings=s,
                                         engine=engine, language=body.language)
    except Exception as e:
        raise HTTPException(500, f"Transcription failed: {e}")
    segments = tr.get("segments") or []
    words = tr.get("words") or []
    audio_dur = float(tr.get("duration") or 0.0)
    if not segments and tr.get("text"):
        segments = [{"text": tr["text"], "start": 0.0, "end": audio_dur or 1.0}]
    if not segments:
        raise HTTPException(500, "Transcription produced no speech segments.")

    # ---- fresh project state for this Audio->Video build ----
    _reset_generated(delete_files=True)
    with _state_write_lock:
        st = store.load_state()
        st["style_notes"] = style_notes
        st["master_prompt"] = (("VISUAL STYLE — match the sample video exactly: "
                                + style_notes) if style_notes else st.get("master_prompt", ""))
        st["style_frames"] = [{"id": store.new_id("sf"), "url": u} for u in style_picks]
        store.save_state(st)

    # ---- STEP 3: Claude writes one visual scene per transcript segment ----
    script = None
    for _attempt in range(3):
        try:
            raw = claude.scenes_from_transcript(
                segments, style_notes=style_notes,
                master_prompt=store.load_state().get("master_prompt", ""),
                dynamic=body.dynamic)
            candidate = extract_json(raw)
            # Guard: extract_json can return a list (bare array) or None.
            if isinstance(candidate, list):
                candidate = {"scenes": candidate, "characters": []}
            if not isinstance(candidate, dict):
                raise ValueError(f"Claude returned {type(candidate).__name__}, expected dict")
            # Must have scenes list.
            if not (candidate.get("scenes") or []):
                raise ValueError("Claude returned no scenes")
            script = candidate
            break
        except Exception as e:
            print(f"[a2v] scene generation attempt {_attempt+1} failed: {e}", flush=True)
            if _attempt == 2:
                raise HTTPException(500, f"Scene generation failed after 3 attempts: {e}")

    scenes = _normalize_scenes(script.get("scenes") or [])
    # Lock VO to the real transcript verbatim + sanitize image prompts.
    for i, sc in enumerate(scenes):
        if i < len(segments):
            sc["vo"] = (segments[i].get("text") or "").strip()
        if sc.get("prompt"):
            sc["prompt"] = _sanitize_prompt(sc["prompt"])
    # Pad/trim so scenes line up 1:1 with segments.
    while len(scenes) < len(segments):
        i = len(scenes)
        seg = segments[i]
        base = scenes[-1].get("prompt", "") if scenes else (style_notes or "scene")
        cue = _angle_cue_for(i)
        scenes.append({"n": i + 1, "vo": (seg.get("text") or "").strip(),
                       "prompt": _sanitize_prompt(f"{base}, {cue}"),
                       "shot_relation": "cut"})
    scenes = scenes[:len(segments)]

    with _state_write_lock:
        st = store.load_state()
        st["script"] = {"scenes": scenes, "scene_count": len(scenes),
                        "voiceover": tr.get("text", ""),
                        "characters": script.get("characters") or []}
        store.save_state(st)

    # ---- STEP 4: character sheets (auto-cast, style-anchored) ----
    img_client = get_image_client(request)
    for c in (script.get("characters") or []):
        name = (c.get("name") or "").strip()
        sheet_prompt = (c.get("sheet_prompt") or c.get("description") or "").strip()
        if not name:
            continue
        try:
            cur = store.load_state()
            prompt = pipeline.build_sheet_prompt(
                cur.get("master_prompt", ""), name, sheet_prompt,
                style_notes=cur.get("style_notes", ""))
            _refs, _labels = [], []
            for _sf in (cur.get("style_frames") or [])[:config.STYLE_REF_COUNT]:
                try:
                    _refs.append(store.read_image(_sf["url"]))
                    _labels.append("STYLE REF — COPY ART STYLE ONLY; IGNORE ITS CHARACTERS/PEOPLE")
                except Exception:
                    pass
            if _refs:
                ep = (prompt + "\n\nReproduce the EXACT art style of the attached "
                      "reference frame(s); draw THIS character in that style.")
                _multi = bool(s.get("multi_image_edit"))
                _send = (_refs if _multi else
                         ([pipeline.contact_sheet(_refs, labels=_labels)]
                          if len(_refs) > 1 else _refs))
                img = img_client.edit(ep, _send, size=size, quality=quality)
            else:
                img = img_client.generate(prompt, size=size, quality=quality)
            rec = {"id": store.new_id("char"), "name": name,
                   "description": sheet_prompt,
                   "sheet_url": store.write_image("characters", img),
                   "prompt": prompt, "source": "generated"}
            with _state_write_lock:
                cur = store.load_state()
                cur["characters"].append(rec)
                store.save_state(cur)
            store.log_usage("image", 1, 0.08)
        except Exception as ce:
            print(f"[a2v] character '{name}' failed: {ce}", flush=True)
            if _is_fatal_image_error(str(ce)):
                raise HTTPException(500, f"Image account problem: {str(ce)[:200]}")

    # ---- STEP 5: render frames (style-locked, micro-cut continuity) ----
    frames_ok, frames_fail = 0, 0
    rendered_scene_indices = []  # track which scene index each successful frame came from
    for i, sc in enumerate(scenes):
        p = (sc.get("prompt") or "").strip()
        if not p:
            p = _sanitize_prompt((sc.get("vo") or "").strip() or (style_notes or "scene"))
        if not _has_camera_language(p):
            cue = _angle_cue_for(i)
            if cue:
                p = f"{p}, {cue}"
        try:
            _render_one(p, size, quality, True, True, request=request,
                        shot_relation=sc.get("shot_relation", "cut"))
            rendered_scene_indices.append(i)
            frames_ok += 1
        except Exception as ex:
            frames_fail += 1
            print(f"[a2v] frame {i+1} failed: {ex}", flush=True)
            if _is_fatal_image_error(str(ex)):
                raise HTTPException(500, f"Image account problem: {str(ex)[:200]}")
    if frames_ok == 0:
        raise HTTPException(500, f"No frames rendered ({frames_fail} failed).")

    # ---- STEP 6: build the final video — YOUR audio + word-timestamp holds ----
    st = store.load_state()
    seq = st.get("sequence") or []
    n = len(seq)
    # Map each frame in seq back to the scene it was rendered from, so VO and
    # hold timing stay correct even when some scene renders failed and were
    # skipped (seq has no gap — it only contains successful frames).
    frame_scene_map = rendered_scene_indices[:n]

    # Ensure audio_dur is always a real float (never None) — prevents TypeError
    # in division inside _holds_from_alignment.
    if not audio_dur or audio_dur <= 0.0:
        if words:
            audio_dur = words[-1]["end"]
        elif segments:
            audio_dur = segments[-1]["end"]
        else:
            audio_dur = max(1.0, n * 2.0)

    # Real per-scene holds from Whisper word timestamps (frame-accurate sync).
    holds = None
    if words:
        alignment = transcribe.words_to_char_alignment(words)
        tts_lines = [(scenes[frame_scene_map[i]].get("vo") or "").strip()
                     if i < len(frame_scene_map) and frame_scene_map[i] < len(scenes)
                     else "" for i in range(n)]
        holds = _holds_from_alignment(tts_lines, alignment, audio_dur)
    if not holds or len(holds) != n:
        # Fallback: proportional to the mapped segment durations.
        raw_h = []
        for i in range(n):
            si = frame_scene_map[i] if i < len(frame_scene_map) else 0
            seg = segments[si] if si < len(segments) else {"start": 0, "end": 0}
            raw_h.append(max(0.3, float(seg.get("end", 0)) - float(seg.get("start", 0))))
        tot = sum(raw_h) or 1.0
        scale = audio_dur / tot
        holds = [round(h * scale, 3) for h in raw_h]

    shots, scene_map = [], []
    for i in range(n):
        try:
            img_path = store.url_to_path(seq[i]["image_url"])
        except Exception:
            continue
        si = frame_scene_map[i] if i < len(frame_scene_map) else 0
        vo = (scenes[si].get("vo") or "") if si < len(scenes) else ""
        shots.append({"path": img_path, "duration": holds[i], "note": vo[:60]})
        scene_map.append({"index": i + 1, "vo": vo, "hold_seconds": holds[i]})
    if not shots:
        raise HTTPException(400, "no readable frames to assemble")
    # Fast micro-cuts: split any frame held longer than max_hold.
    shots = editor.split_long_holds(shots, max_hold=max(0.8, float(body.max_hold)))

    out_name = f"a2v_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    try:
        editor.assemble_video(shots, body.audio_path, out_path,
                              transition=(body.transition or "cut").lower(),
                              width=width, height=height, fps=body.fps,
                              motion=body.motion)
    except Exception as ex:
        raise HTTPException(500, f"video assembly failed: {ex}")

    if body.cut_clicks:
        try:
            _apply_cut_clicks(request, out_path, [sc["hold_seconds"] for sc in scene_map],
                              volume=body.cut_click_volume, style=body.cut_click_style)
        except Exception as ex:
            print(f"[a2v] cut clicks skipped: {ex}", flush=True)

    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    video_url = f"/data/{rel}"
    total = round(audio_dur or sum(holds), 2)
    # Copy the uploaded audio into the data dir so it gets a stable /data/ URL
    # (otherwise the UI can't play it back and downstream features break).
    _audio_url = None
    try:
        with open(body.audio_path, "rb") as _af:
            _audio_url, _ = store.write_binary(
                "audio", _af.read(),
                ext=os.path.splitext(body.audio_path)[1].lstrip(".") or "mp3",
                name_hint="a2v_audio")
    except Exception:
        pass
    with _state_write_lock:
        st = store.load_state()
        st["audio"] = {"id": store.new_id("audio"), "url": _audio_url,
                       "name": os.path.basename(body.audio_path), "duration": total}
        edit_rec = {"id": store.new_id("edit"), "url": video_url,
                    "plan": {"mode": "audio_to_video", "total_duration": total,
                             "frames": n, "shots": scene_map},
                    "created": store.now()}
        st.setdefault("edits", []).append(edit_rec)
        store.save_state(st)
    store.log_usage("video", 1, 0.0)

    return {"ok": True, "video_url": video_url, "duration": total,
            "frames": frames_ok, "frames_failed": frames_fail,
            "scenes": scene_map, "style_notes": style_notes,
            "transcript": tr.get("text", ""),
            "characters": len(st.get("characters") or [])}


# =========================================================================== #
#  A2V ENHANCED FEATURES                                                     #
#  1. Async jobs + live progress                                              #
#  2. Scene preview + manual editing before render                            #
#  3. Re-roll individual frames                                               #
#  4. Waveform with scene cut markers                                         #
#  5. Multi-language support                                                  #
#  6. Music / background audio mixing                                         #
#  7. Export presets (YouTube Shorts, Reels, TikTok)                          #
#  8. Cost estimation                                                         #
# =========================================================================== #

# --- Async A2V progress tracking (mirrors autopilot pattern) --------------- #
_A2V_PROGRESS = {}
_A2V_LOCK = threading.Lock()
_A2V_STEPS = ["analyse", "transcribe", "scenes", "characters", "frames", "video"]


def _a2v_prog(run_id, **kw):
    if not run_id:
        return
    with _A2V_LOCK:
        # Cleanup stale entries older than 1 hour to prevent memory leak.
        _now = time.time()
        _stale = [k for k, v in _A2V_PROGRESS.items()
                  if _now - v.get("started", _now) > 3600 and v.get("done")]
        for k in _stale:
            del _A2V_PROGRESS[k]
        p = _A2V_PROGRESS.setdefault(run_id, {
            "run_id": run_id, "started": time.time(),
            "steps_total": len(_A2V_STEPS), "step": "", "step_index": 0,
            "frames_done": 0, "frames_total": 0, "chars_done": 0, "chars_total": 0,
            "done": False, "error": None, "video_url": None,
            "scenes": None, "transcript": None, "style_notes": None,
        })
        if "step" in kw:
            kw["step_index"] = _A2V_STEPS.index(kw["step"]) if kw["step"] in _A2V_STEPS else p["step_index"]
        p.update(kw)


@app.get("/api/audio-to-video/progress/{run_id}")
def api_a2v_progress(run_id: str):
    """Poll A2V generation progress — drives the live progress bar."""
    with _A2V_LOCK:
        p = dict(_A2V_PROGRESS.get(run_id) or {})
    if not p:
        return {"run_id": run_id, "unknown": True}
    elapsed = max(0.0, time.time() - p.get("started", time.time()))
    done = p.get("frames_done", 0)
    total = p.get("frames_total", 0)
    eta = None
    if total and done:
        rate = elapsed / max(1, done)
        eta = round(rate * max(0, total - done), 1)
    p["elapsed"] = round(elapsed, 1)
    p["eta_seconds"] = eta
    return p


# --- Feature 2: Scene preview — analyse + transcribe without rendering ----- #
class A2VPreviewIn(BaseModel):
    audio_path: str
    sample_video_path: Optional[str] = None
    sample_frame_urls: Optional[List[str]] = None
    style_notes: Optional[str] = None
    language: Optional[str] = None
    transcribe_engine: str = "local"
    dynamic: bool = True
    orientation: str = "landscape"


@app.post("/api/audio-to-video/preview")
def api_a2v_preview(body: A2VPreviewIn, request: Request):
    """Analyse audio + sample video, write scenes, return them for user review
    BEFORE spending image API credits. The user can edit/approve scenes, then
    call /api/audio-to-video/render to actually generate frames."""
    import transcribe

    s = request.state.settings
    if not _has_ai_key(s):
        raise HTTPException(400, "No AI key set — add Claude in Settings.")
    if not os.path.exists(body.audio_path):
        raise HTTPException(400, "Uploaded audio not found.")

    claude = _claude_client_for(None, request)

    # Style analysis
    frame_urls = list(body.sample_frame_urls or [])
    if not frame_urls and body.sample_video_path and os.path.exists(body.sample_video_path):
        try:
            import video as videomod
            frame_urls = videomod.extract_frames(body.sample_video_path, fps=0.5, max_frames=12)
        except Exception:
            pass
    style_picks = frame_urls[::max(1, len(frame_urls) // 4)][:4] if frame_urls else []
    style_notes = (body.style_notes or "").strip() or _a2v_analyze_style(claude, frame_urls)

    # Transcription
    engine = (body.transcribe_engine or "local").lower()
    try:
        tr = transcribe.transcribe_audio(body.audio_path, settings=s,
                                         engine=engine, language=body.language)
    except Exception as e:
        raise HTTPException(500, f"Transcription failed: {e}")

    segments = tr.get("segments") or []
    audio_dur = float(tr.get("duration") or 0.0)
    if not segments and tr.get("text"):
        segments = [{"text": tr["text"], "start": 0.0, "end": audio_dur or 1.0}]
    if not segments:
        raise HTTPException(500, "No speech segments found in audio.")

    # Store segments in state so the render pipeline can match them later.
    with _state_write_lock:
        _seg_st = store.load_state()
        _seg_st["a2v_segments"] = segments
        _seg_st["a2v_audio_dur"] = audio_dur
        store.save_state(_seg_st)

    # Scene generation
    master_prompt = ("VISUAL STYLE — match the sample video exactly: " + style_notes) if style_notes else ""
    try:
        raw = claude.scenes_from_transcript(
            segments, style_notes=style_notes, master_prompt=master_prompt,
            dynamic=body.dynamic)
        script = extract_json(raw)
        if isinstance(script, list):
            script = {"scenes": script, "characters": []}
        if not isinstance(script, dict):
            script = {"scenes": [], "characters": []}
    except Exception:
        script = {"scenes": [], "characters": []}

    scenes = _normalize_scenes(script.get("scenes") or [])
    for i, sc in enumerate(scenes):
        if i < len(segments):
            sc["vo"] = (segments[i].get("text") or "").strip()
        if sc.get("prompt"):
            sc["prompt"] = _sanitize_prompt(sc["prompt"])
    # Pad/trim to match segments
    while len(scenes) < len(segments):
        i = len(scenes)
        seg = segments[i]
        base = scenes[-1].get("prompt", "") if scenes else (style_notes or "scene")
        cue = _angle_cue_for(i)
        scenes.append({"n": i + 1, "vo": (seg.get("text") or "").strip(),
                       "prompt": _sanitize_prompt(f"{base}, {cue}"),
                       "shot_relation": "cut"})
    scenes = scenes[:len(segments)]

    # Cost estimation
    n_scenes = len(scenes)
    _IMG_COST = {"low": 0.02, "medium": 0.04, "high": 0.08, "auto": 0.06}
    img_cost = n_scenes * _IMG_COST.get("medium", 0.04)
    char_cost = len(script.get("characters") or []) * _IMG_COST.get("medium", 0.04)
    # Estimate TTS cost: ~$0.00003 per char for ElevenLabs
    total_chars = sum(len(sc.get("vo", "")) for sc in scenes)
    tts_cost = total_chars * 0.00003 if engine == "elevenlabs" else 0.0
    total_cost = round(img_cost + char_cost + tts_cost, 4)

    # Waveform data for visualization
    waveform = None
    try:
        _dur = audio_dur or (segments[-1]["end"] if segments else 0)
        waveform = {"duration": _dur, "segments": segments}
    except Exception:
        pass

    return {
        "ok": True,
        "scenes": scenes,
        "characters": script.get("characters") or [],
        "transcript": tr.get("text", ""),
        "segments": segments,
        "style_notes": style_notes,
        "style_frames": style_picks,
        "audio_duration": audio_dur,
        "transcribe_engine": tr.get("engine", engine),
        "cost_estimate": {
            "image_renders": n_scenes,
            "character_sheets": len(script.get("characters") or []),
            "image_cost_usd": round(img_cost + char_cost, 4),
            "tts_cost_usd": round(tts_cost, 4),
            "total_usd": total_cost,
        },
        "waveform": waveform,
    }


# --- Feature 1+3: Async render with progress + re-roll -------------------- #
class A2VRenderIn(BaseModel):
    audio_path: str
    scenes: List[dict]           # user-approved (possibly edited) scenes
    characters: Optional[List[dict]] = []
    style_notes: Optional[str] = None
    style_frames: Optional[List[str]] = None
    orientation: str = "landscape"
    size: Optional[str] = None
    quality: Optional[str] = None
    transition: str = "cut"
    fps: int = 30
    max_hold: float = 1.6
    motion: bool = True
    cut_clicks: bool = False
    cut_click_volume: float = 0.30
    cut_click_style: str = "click"
    music_path: Optional[str] = None       # Feature 6: background music
    music_volume: float = 0.12
    export_preset: Optional[str] = None    # Feature 7: "shorts"/"reels"/"tiktok"/"youtube"


# Export preset definitions
_EXPORT_PRESETS = {
    "shorts":  {"orientation": "portrait", "size": "1024x1536", "width": 1080, "height": 1920, "fps": 30, "max_hold": 1.2},
    "reels":   {"orientation": "portrait", "size": "1024x1536", "width": 1080, "height": 1920, "fps": 30, "max_hold": 1.2},
    "tiktok":  {"orientation": "portrait", "size": "1024x1536", "width": 1080, "height": 1920, "fps": 30, "max_hold": 1.0},
    "youtube": {"orientation": "landscape", "size": "1536x1024", "width": 1920, "height": 1080, "fps": 30, "max_hold": 2.5},
    "square":  {"orientation": "square", "size": "1024x1024", "width": 1080, "height": 1080, "fps": 30, "max_hold": 1.6},
}


@app.post("/api/audio-to-video/render")
def api_a2v_render(body: A2VRenderIn, request: Request):
    """Async render: starts the A2V pipeline in a background thread and returns
    a run_id for polling via /api/audio-to-video/progress/{run_id}."""
    run_id = store.new_id("a2v")
    _a2v_prog(run_id, step="init", frames_total=len(body.scenes))

    # Apply export preset if specified
    preset = _EXPORT_PRESETS.get((body.export_preset or "").lower()) if body.export_preset else None
    if preset:
        body.orientation = preset["orientation"]
        body.size = body.size or preset["size"]
        body.fps = preset["fps"]
        body.max_hold = preset["max_hold"]

    # Snapshot settings + image client config BEFORE spawning the background
    # thread -- the request object may be garbage-collected after the response
    # is sent, so we must capture everything the thread needs now.
    _snap_settings = dict(request.state.settings)
    _snap_img_cfg = {
        "api_key": _snap_settings.get("api_key", ""),
        "base_url": _snap_settings.get("base_url", config.BASE_URL),
        "model": _snap_settings.get("model", config.MODEL),
    }
    # Create a mock request-like object for functions that still take request
    # (e.g. _render_one, _apply_cut_clicks) — they only access
    # request.state.settings which we've snapshotted.
    class _MockState:
        pass
    class _MockRequest:
        pass
    _mock_state = _MockState()
    _mock_state.settings = _snap_settings
    _mock_request = _MockRequest()
    _mock_request.state = _mock_state

    def _run_a2v():
        try:
            _a2v_run_pipeline(run_id, body, _snap_settings, _snap_img_cfg, _mock_request)
        except Exception as e:
            _a2v_prog(run_id, done=True, error=str(e)[:500])
            print(f"[a2v] async run {run_id} failed: {e}", flush=True)

    t = threading.Thread(target=_run_a2v, daemon=True)
    t.start()
    return {"ok": True, "run_id": run_id, "scenes": len(body.scenes)}


def _a2v_run_pipeline(run_id: str, body: A2VRenderIn, s: dict, img_cfg: dict, request=None):
    """The actual A2V pipeline -- runs in a background thread with progress.
    Uses snapshotted settings + image config (not the request object).
    ``request`` is a mock object with .state.settings for functions that need it."""
    # s is already the settings dict (snapshotted before thread start)
    style_notes = (body.style_notes or "").strip()
    # Use the snapshotted settings to build the image client.
    _img_client = _client_from_settings(s)
    scenes = list(body.scenes or [])
    n = len(scenes)
    if n == 0:
        _a2v_prog(run_id, done=True, error="No scenes provided")
        return

    # Apply orientation from body (preset already applied in api_a2v_render)
    orient = (body.orientation or "landscape").lower()
    if orient in ("vertical", "portrait", "9:16", "shorts", "tiktok"):
        size = body.size or "1024x1536"; width, height = 1080, 1920
    elif orient in ("square", "1:1"):
        size = body.size or "1024x1024"; width, height = 1080, 1080
    else:
        size = body.size or "1536x1024"; width, height = 1920, 1080
    quality = body.quality or config.DEFAULT_QUALITY

    # Fresh project state
    _reset_generated(delete_files=True)
    style_picks = body.style_frames or []
    with _state_write_lock:
        st = store.load_state()
        st["style_notes"] = style_notes
        st["master_prompt"] = ("VISUAL STYLE — match the sample video exactly: " + style_notes) if style_notes else ""
        st["style_frames"] = [{"id": store.new_id("sf"), "url": u} for u in style_picks]
        store.save_state(st)

    # Step: Characters
    _a2v_prog(run_id, step="characters")
    img_client = _img_client
    chars_created = 0
    for c in (body.characters or []):
        name = (c.get("name") or "").strip()
        sheet_prompt = (c.get("sheet_prompt") or c.get("description") or "").strip()
        if not name:
            continue
        try:
            cur = store.load_state()
            prompt = pipeline.build_sheet_prompt(cur.get("master_prompt", ""), name, sheet_prompt,
                                                  style_notes=cur.get("style_notes", ""))
            _refs, _labels = [], []
            for _sf in (cur.get("style_frames") or [])[:config.STYLE_REF_COUNT]:
                try:
                    _refs.append(store.read_image(_sf["url"]))
                    _labels.append("STYLE REF — COPY ART STYLE ONLY; IGNORE ITS CHARACTERS/PEOPLE")
                except Exception:
                    pass
            if _refs:
                ep = prompt + "\n\nReproduce the EXACT art style of the attached reference frame(s); draw THIS character in that style."
                _multi = bool(s.get("multi_image_edit"))
                _send = _refs if _multi else ([pipeline.contact_sheet(_refs, labels=_labels)] if len(_refs) > 1 else _refs)
                img = img_client.edit(ep, _send, size=size, quality=quality)
            else:
                img = img_client.generate(prompt, size=size, quality=quality)
            rec = {"id": store.new_id("char"), "name": name, "description": sheet_prompt,
                   "sheet_url": store.write_image("characters", img), "prompt": prompt, "source": "generated"}
            with _state_write_lock:
                cur = store.load_state()
                cur["characters"].append(rec)
                store.save_state(cur)
            chars_created += 1
            _a2v_prog(run_id, chars_done=chars_created)
            store.log_usage("image", 1, 0.08)
        except Exception as ce:
            print(f"[a2v] character '{name}' failed: {ce}", flush=True)
            if _is_fatal_image_error(str(ce)):
                _a2v_prog(run_id, done=True, error=f"Image account problem: {str(ce)[:200]}")
                return

    # Step: Render frames
    _a2v_prog(run_id, step="frames", frames_total=n, frames_done=0)
    rendered_scene_indices = []
    for i, sc in enumerate(scenes):
        p = (sc.get("prompt") or "").strip()
        if not p:
            p = _sanitize_prompt((sc.get("vo") or "").strip() or (style_notes or "scene"))
        if not _has_camera_language(p):
            cue = _angle_cue_for(i)
            if cue:
                p = f"{p}, {cue}"
        try:
            _render_one(p, size, quality, True, True, request=request,
                        shot_relation=sc.get("shot_relation", "cut"))
            rendered_scene_indices.append(i)
            _a2v_prog(run_id, frames_done=len(rendered_scene_indices))
            store.log_usage("image", 1, 0.08)
        except Exception as ex:
            print(f"[a2v] frame {i+1} failed: {ex}", flush=True)
            if _is_fatal_image_error(str(ex)):
                _a2v_prog(run_id, done=True, error=f"Image account problem: {str(ex)[:200]}")
                return

    frames_ok = len(rendered_scene_indices)
    if frames_ok == 0:
        _a2v_prog(run_id, done=True, error="No frames rendered")
        return

    # Step: Build video
    _a2v_prog(run_id, step="video")
    st = store.load_state()
    seq = st.get("sequence") or []
    actual_n = len(seq)
    frame_scene_map = rendered_scene_indices[:actual_n]

    # Compute holds from Whisper timestamps (reuse the existing alignment path)
    audio_dur = float(st.get("a2v_audio_dur") or 0.0)
    if not audio_dur:
        try:
            audio_dur = float(editor.probe_duration(body.audio_path))
        except Exception:
            audio_dur = max(1.0, actual_n * 2.0)

    segments_data = []
    for si in frame_scene_map:
        if si < len(scenes):
            vo = (scenes[si].get("vo") or "").strip()
            # Find matching segment
            for seg in (st.get("a2v_segments") or []):
                if (seg.get("text") or "").strip() == vo:
                    segments_data.append(seg)
                    break
            else:
                segments_data.append({"text": vo, "start": 0, "end": 0})
        else:
            segments_data.append({"text": "", "start": 0, "end": 0})

    # Fallback: proportional to segment durations
    raw_h = [max(0.3, float(sg.get("end", 0)) - float(sg.get("start", 0))) for sg in segments_data]
    tot = sum(raw_h) or 1.0
    scale = audio_dur / tot
    holds = [round(h * scale, 3) for h in raw_h]

    shots, scene_map = [], []
    for i in range(actual_n):
        try:
            img_path = store.url_to_path(seq[i]["image_url"])
        except Exception:
            continue
        si = frame_scene_map[i] if i < len(frame_scene_map) else 0
        vo = (scenes[si].get("vo") or "") if si < len(scenes) else ""
        shots.append({"path": img_path, "duration": holds[i], "note": vo[:60]})
        scene_map.append({"index": i + 1, "vo": vo, "hold_seconds": holds[i]})

    if not shots:
        _a2v_prog(run_id, done=True, error="No readable frames to assemble")
        return

    shots = editor.split_long_holds(shots, max_hold=max(0.8, float(body.max_hold)))

    out_name = f"a2v_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    try:
        # Feature 6: music mixing
        music_path = body.music_path if body.music_path and os.path.exists(body.music_path) else None
        music_vol = max(0.0, min(1.0, float(body.music_volume)))
        editor.assemble_video(shots, body.audio_path, out_path,
                              transition=(body.transition or "cut").lower(),
                              width=width, height=height, fps=body.fps,
                              motion=body.motion,
                              music_path=music_path, music_volume=music_vol)
    except Exception as ex:
        _a2v_prog(run_id, done=True, error=f"Video assembly failed: {ex}")
        return

    if body.cut_clicks:
        try:
            _apply_cut_clicks(request, out_path, [sc["hold_seconds"] for sc in scene_map],
                              volume=body.cut_click_volume, style=body.cut_click_style)
        except Exception as ex:
            print(f"[a2v] cut clicks skipped: {ex}", flush=True)

    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    video_url = f"/data/{rel}"
    total = round(audio_dur, 2)

    _audio_url = None
    try:
        with open(body.audio_path, "rb") as _af:
            _audio_url, _ = store.write_binary("audio", _af.read(),
                ext=os.path.splitext(body.audio_path)[1].lstrip(".") or "mp3",
                name_hint="a2v_audio")
    except Exception:
        pass

    with _state_write_lock:
        st = store.load_state()
        st["audio"] = {"id": store.new_id("audio"), "url": _audio_url,
                       "name": os.path.basename(body.audio_path), "duration": total}
        edit_rec = {"id": store.new_id("edit"), "url": video_url,
                    "plan": {"mode": "audio_to_video", "total_duration": total,
                             "frames": actual_n, "shots": scene_map},
                    "created": store.now()}
        st.setdefault("edits", []).append(edit_rec)
        store.save_state(st)
    store.log_usage("video", 1, 0.0)

    _a2v_prog(run_id, done=True, video_url=video_url, duration=total,
              frames_done=frames_ok, scenes=scene_map,
              style_notes=style_notes)


# --- Feature 3: Re-roll a single frame ------------------------------------- #
class A2VRerollIn(BaseModel):
    scene_index: int
    audio_path: str
    scenes: List[dict]
    style_notes: Optional[str] = None
    orientation: str = "landscape"
    size: Optional[str] = None
    quality: Optional[str] = None


@app.post("/api/audio-to-video/reroll")
def api_a2v_reroll(body: A2VRerollIn, request: Request):
    """Re-render a single scene frame without re-running the whole pipeline."""
    if body.scene_index < 0 or body.scene_index >= len(body.scenes):
        raise HTTPException(400, f"scene_index {body.scene_index} out of range (0-{len(body.scenes)-1})")

    orient = (body.orientation or "landscape").lower()
    if orient in ("vertical", "portrait", "9:16", "shorts", "tiktok"):
        size = body.size or "1024x1536"
    elif orient in ("square", "1:1"):
        size = body.size or "1024x1024"
    else:
        size = body.size or "1536x1024"
    quality = body.quality or config.DEFAULT_QUALITY
    style_notes = (body.style_notes or "").strip()

    sc = body.scenes[body.scene_index]
    p = (sc.get("prompt") or "").strip()
    if not p:
        p = _sanitize_prompt((sc.get("vo") or "").strip() or (style_notes or "scene"))

    try:
        rec = _render_one(p, size, quality, True, True, request=request,
                          shot_relation=sc.get("shot_relation", "cut"))
        store.log_usage("image", 1, 0.08)
        return {"ok": True, "scene_index": body.scene_index,
                "image_url": rec.get("image_url"), "prompt": p}
    except Exception as ex:
        raise HTTPException(500, f"Re-roll failed: {ex}")


# --- Feature 5: Waveform with scene cut markers ---------------------------- #
@app.get("/api/audio-to-video/waveform")
def api_a2v_waveform(path: str, bars: int = 120):
    """Generate waveform data for the audio file.
    Returns bar heights for visualization. Path must be under DATA_DIR."""
    # Security: validate path is under the managed data directory to prevent
    # arbitrary filesystem reads via the waveform endpoint.
    real_path = os.path.realpath(path)
    real_data = os.path.realpath(config.DATA_DIR)
    if not real_path.startswith(real_data):
        raise HTTPException(400, "Path must be under the data directory.")
    if not os.path.exists(real_path):
        raise HTTPException(400, "Audio file not found.")
    try:
        import subprocess as _sp
        # Use ffmpeg to extract raw audio samples and compute bar amplitudes
        cmd = ["ffmpeg", "-i", path, "-ac", "1", "-ar", "8000", "-f", "f32le", "-"]
        proc = _sp.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed")
        import struct
        raw = proc.stdout
        n_samples = len(raw) // 4
        samples = struct.unpack(f"<{n_samples}f", raw[:n_samples*4]) if raw else []
        if not samples:
            return {"bars": [0.0] * bars, "duration": 0}
        # Downsample to bar count
        chunk = max(1, len(samples) // bars)
        heights = []
        for i in range(bars):
            start = i * chunk
            end = min(start + chunk, len(samples))
            if start >= len(samples):
                heights.append(0.0)
            else:
                chunk_samples = samples[start:end]
                rms = (sum(s*s for s in chunk_samples) / len(chunk_samples)) ** 0.5
                heights.append(round(min(1.0, rms * 3), 4))
        dur = float(editor.probe_duration(path)) if os.path.exists(path) else 0
        return {"bars": heights, "duration": dur}
    except Exception as e:
        raise HTTPException(500, f"Waveform generation failed: {e}")


# --- Feature 8: Cost estimation endpoint ----------------------------------- #
@app.get("/api/audio-to-video/cost")
def api_a2v_cost_estimate(scenes: int = 10, characters: int = 1, quality: str = "medium",
                          engine: str = "local", audio_chars: int = 0):
    """Estimate the cost of an A2V run before committing."""
    _IMG_COST = {"low": 0.02, "medium": 0.04, "high": 0.08, "auto": 0.06}
    img_per = _IMG_COST.get(quality, 0.04)
    return {
        "image_renders": scenes,
        "character_sheets": characters,
        "image_cost_usd": round((scenes + characters) * img_per, 4),
        "tts_cost_usd": round(audio_chars * 0.00003, 4) if engine == "elevenlabs" else 0.0,
        "transcription_cost_usd": 0.0 if engine == "local" else round(audio_chars * 0.000006, 4),
        "total_usd": round((scenes + characters) * img_per + (audio_chars * 0.00003 if engine == "elevenlabs" else 0), 4),
        "note": "Local Whisper transcription is free. Image costs are per-render estimates.",
    }


# --- Feature 7: Export presets info ----------------------------------------- #
@app.get("/api/audio-to-video/presets")
def api_a2v_presets():
    """Return available export presets with their settings."""
    return {"presets": _EXPORT_PRESETS}


# ============================================================================
#  FLOW BRIDGE — Chrome extension bridge for Google Flow image generation
# ============================================================================
_FLOW_DIR = os.path.join(os.environ.get("DATA_DIR", os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ContinuityStudio")), "flow_frames")
os.makedirs(_FLOW_DIR, exist_ok=True)


@app.get("/api/flow/status")
def api_flow_status():
    """Liveness + frame counts for the extension status badge."""
    frames = 0
    chars = 0
    try:
        for name in os.listdir(_FLOW_DIR):
            if name.startswith("frame_"):
                frames += 1
            elif name.startswith("char_"):
                chars += 1
    except Exception:
        pass
    return {"ok": True, "frames": frames, "characters": chars, "bridge": "cs://127.0.0.1:8765"}


@app.get("/api/flow/job")
def api_flow_job():
    """
    Return the current autopilot job payload for the extension:
    - style reference images (data URLs)
    - character-sheet prompts
    - ordered frame prompts (each with prev-frame ref + matched character)
    """
    st = store.load_state()
    autopilot = (st.get("autopilot_run") or {})
    script = st.get("script") or []
    style_frames = st.get("style_frames") or []
    characters = st.get("characters") or []
    prompts = []
    for i, scene in enumerate(script):
        text = scene.get("voice_over") or scene.get("content") or scene.get("prompt", "")
        prompt = {
            "frame_index": i,
            "prompt": text,
            "visual": scene.get("visual") or scene.get("prompt", ""),
            "characters": [c.get("name", "") for c in characters],
        }
        if i > 0 and i <= len(prompts):
            prev = os.path.join(_FLOW_DIR, f"frame_{i-1:05d}.png")
            if os.path.exists(prev):
                prompt["prev_frame"] = f"file:///{prev.replace(os.sep, '/')}"
        prompts.append(prompt)

    char_prompts = []
    for c in characters:
        char_prompts.append({
            "character_id": c.get("id") or c.get("name", ""),
            "prompt": c.get("description") or c.get("prompt", ""),
        })

    def _data_url(path):
        if not path or not os.path.exists(path):
            return ""
        import base64
        mime = "image/png"
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        return f"data:{mime};base64,{b64}"

    style_refs = [{"id": sf.get("id", f"sf_{i}"), "data_url": _data_url(sf.get("url", "").replace("file:///", ""))}
                  for i, sf in enumerate(style_frames)]
    return {
        "ok": True,
        "project": st.get("project_name", ""),
        "style_refs": style_refs,
        "character_prompts": char_prompts,
        "frames": prompts,
        "total_frames": len(prompts),
        "total_characters": len(char_prompts),
    }


class FlowFrameIn(BaseModel):
    image: str          # data:image/png;base64,...
    prompt: str = ""
    frame_id: Optional[str] = None
    model: Optional[str] = None


@app.post("/api/flow/frame")
def api_flow_frame(body: FlowFrameIn):
    """Save a generated frame from the extension into the project's flow_frames dir."""
    try:
        header, b64 = body.image.split(",", 1)
        ext = ".png" if "png" in header else ".jpg"
        idx = body.frame_id or f"frame_{int(time.time()*1000):012d}"
        path = os.path.join(_FLOW_DIR, f"{idx}{ext}")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(b64))
        return {"ok": True, "path": path, "frame_id": idx}
    except Exception as e:
        raise HTTPException(400, f"bad image payload: {e}")


class FlowCharIn(BaseModel):
    image: str
    character_id: str = ""
    model: Optional[str] = None


@app.post("/api/flow/character")
def api_flow_character(body: FlowCharIn):
    """Save a generated character sheet from the extension."""
    try:
        header, b64 = body.image.split(",", 1)
        ext = ".png" if "png" in header else ".jpg"
        cid = body.character_id or f"char_{int(time.time()*1000):012d}"
        path = os.path.join(_FLOW_DIR, f"char_{cid}{ext}")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(b64))
        return {"ok": True, "path": path, "character_id": cid}
    except Exception as e:
        raise HTTPException(400, f"bad image payload: {e}")


import base64
