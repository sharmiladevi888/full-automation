"""Best-effort live model catalogs for configured providers.

The UI may keep bundled fallback lists for offline/local startup, but active
provider lists are refreshed here with a short cache so settings do not go
stale as providers add/remove models.
"""
import hashlib
import threading
import time
from urllib.parse import urlsplit

import requests


_CACHE = {}
_CACHE_LOCK = threading.RLock()
_CACHE_TTL = 120.0


def _models_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    for suffix in ("/images/generations", "/images/edits", "/images"):
        if base.endswith(suffix):
            base = base[:-len(suffix)].rstrip("/")
    if base.endswith("/v1") or base.endswith("/openai") or base.endswith("/v1beta"):
        return base + "/models"
    return base + "/v1/models"


def fetch_models(base_url: str, api_key: str = "") -> list:
    """Return model ids from a provider, or [] without raising."""
    url = _models_url(base_url)
    key = (api_key or "").strip()
    if not url or not key:
        return []
    cache_key = hashlib.sha256((url + "\n" + key).encode()).hexdigest()
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit and now - hit[0] < _CACHE_TTL:
            return list(hit[1])
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {key}", "x-api-key": key},
            timeout=5,
        )
        if r.status_code >= 400:
            return []
        data = r.json()
        ids = [str(item.get("id")) for item in (data.get("data") or [])
               if isinstance(item, dict) and item.get("id")]
        ids = list(dict.fromkeys(ids))[:200]
    except Exception:
        return []
    with _CACHE_LOCK:
        _CACHE[cache_key] = (now, ids)
    return list(ids)


def catalogs_for_settings(settings: dict) -> dict:
    """Fetch only catalogs relevant to the user's active provider."""
    settings = settings or {}
    out = {}

    claude_provider = (settings.get("claude_provider") or "derouter").lower()
    if claude_provider == "9router":
        text_base = settings.get("ninerouter_base_url", "")
        text_key = settings.get("ninerouter_api_key", "")
        ids = fetch_models(text_base, text_key)
        if ids:
            out["claude_models"] = ids
            out["ninerouter_models"] = ids
    elif claude_provider == "gemini":
        ids = fetch_models(settings.get("gemini_base_url", ""),
                           settings.get("gemini_api_key", ""))
        if ids:
            out["gemini_models"] = ids
    elif claude_provider == "agentrouter":
        ids = fetch_models("https://agentrouter.org", settings.get("agentrouter_api_key", ""))
        if ids:
            out["claude_models"] = ids
            out["agentrouter_models"] = ids
    else:
        ids = fetch_models(settings.get("claude_base_url", ""),
                           settings.get("claude_api_key", ""))
        if ids:
            out["claude_models"] = ids

    image_provider = (settings.get("image_provider") or "derouter").lower()
    if image_provider == "9router":
        ids = fetch_models(settings.get("ninerouter_image_base_url", ""),
                           settings.get("ninerouter_api_key", ""))
        if ids:
            out["ninerouter_image_models"] = ids
    elif image_provider == "derouter":
        ids = fetch_models(settings.get("base_url", ""), settings.get("api_key", ""))
        if ids:
            out["image_models"] = ids
    return out
