"""Encrypt / decrypt sensitive fields in vault.json.

Key source (in priority order):
  1. CS_SECRET env var  →  derive a Fernet key via PBKDF2
  2. data/.secret file  →  auto-generated Fernet key (created on first run)

If neither is available AND cryptography is missing, falls back to plaintext
with a console warning.

Encrypted values are stored as "enc::<base64-ciphertext>" so they're easy to
distinguish from plaintext during migration.
"""
import base64
import os
import threading

import config

# Any key matching *_api_key or *_secret or *_token must be added here so
# vault encryption never writes it plaintext. Audited 2026-06-22 after
# GitGuardian flagged a leaked deepgram_api_key — the omission of that key
# from this set is what allowed the plaintext leak. Keep this list exhaustive.
_SENSITIVE_KEYS = {
    "api_key",
    "claude_api_key",
    "elevenlabs_api_key",
    "anthropic_api_key",
    "openai_api_key",
    "ninerouter_api_key",
    "mimo_api_key",
    "agentrouter_api_key",
    "deepgram_api_key",   # TTS provider — fixed 2026-06-22 (was missing)
    "deepgram_secret",
    "puter_api_key",      # Image provider token — fixed 2026-08-24
}
_ENC_PREFIX = "enc::"
_SECRET_PATH = os.path.join(config.DATA_DIR, ".secret")

_fernet = None
_fallback = False
_INIT_LOCK = threading.Lock()
_decrypt_failures = 0          # track consecutive decrypt failures for auto-recovery
_AUTO_RECOVERY_THRESHOLD = 3   # if this many keys fail, nuke and start fresh


def _init_fernet():
    global _fernet, _fallback
    if _fernet is not None or _fallback:
        return
    with _INIT_LOCK:
        # Double-checked: another thread may have initialized while we waited.
        if _fernet is not None or _fallback:
            return
        _init_fernet_locked()


def _init_fernet_locked():
    global _fernet, _fallback

    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        print("[vault] WARNING: cryptography not installed — keys stored in PLAINTEXT",
              flush=True)
        _fallback = True
        return

    env_secret = os.environ.get("CS_SECRET", "").strip()
    if env_secret:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=b"continuity-studio-vault", iterations=480_000)
        key = base64.urlsafe_b64encode(kdf.derive(env_secret.encode()))
        _fernet = Fernet(key)
        return

    os.makedirs(config.DATA_DIR, exist_ok=True)
    if os.path.exists(_SECRET_PATH):
        with open(_SECRET_PATH, "rb") as f:
            key = f.read().strip()
        try:
            _fernet = Fernet(key)
            return
        except Exception:
            pass

    key = Fernet.generate_key()
    # Atomic write so a concurrent reader never sees a half-written key file.
    tmp = _SECRET_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(key)
    os.replace(tmp, _SECRET_PATH)
    _fernet = Fernet(key)


def _encrypt(value):
    """Encrypt a string value. Returns enc::-prefixed string."""
    _init_fernet()
    if _fallback or not _fernet or not value:
        return value
    ct = _fernet.encrypt(value.encode())
    return _ENC_PREFIX + ct.decode()


def _decrypt(value):
    """Decrypt an enc::-prefixed value. Returns plaintext."""
    global _decrypt_failures
    _init_fernet()
    if not value or not isinstance(value, str):
        return value
    if not value.startswith(_ENC_PREFIX):
        return value
    if _fallback or not _fernet:
        return value
    try:
        result = _fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
        _decrypt_failures = 0  # reset on success
        return result
    except Exception:
        _decrypt_failures += 1
        return value


def encrypt_vault(vault):
    """Return a copy of vault with sensitive fields encrypted."""
    out = {}
    for email, settings in vault.items():
        s = dict(settings)
        for k in _SENSITIVE_KEYS:
            v = s.get(k, "")
            if v and not v.startswith(_ENC_PREFIX):
                s[k] = _encrypt(v)
        out[email] = s
    return out


def decrypt_vault(vault):
    """Return a copy of vault with sensitive fields decrypted."""
    global _decrypt_failures
    _decrypt_failures = 0
    out = {}
    for email, settings in vault.items():
        s = dict(settings)
        for k in _SENSITIVE_KEYS:
            v = s.get(k, "")
            if v:
                s[k] = _decrypt(v)
        out[email] = s

    # Auto-recovery: if ALL encrypted keys failed to decrypt, the .secret key
    # changed (reinstall, different machine, corrupted file). Nuke the vault
    # and regenerate the key so the app starts clean instead of spamming
    # warnings on every request.
    if _decrypt_failures >= _AUTO_RECOVERY_THRESHOLD:
        _nuke_and_recover()
        return {}  # clean vault — user re-enters keys in Settings

    return out


def _nuke_and_recover():
    """Delete the old .secret and vault.json. Called when too many decrypt
    failures indicate the encryption key is irrecoverably mismatched."""
    global _fernet, _fallback
    print(f"[vault] {_decrypt_failures} keys failed to decrypt — encryption key "
          f"mismatch detected. Regenerating vault (you'll re-enter API keys in "
          f"Settings).", flush=True)
    try:
        if os.path.exists(_SECRET_PATH):
            os.remove(_SECRET_PATH)
    except Exception:
        pass
    # Also delete the vault.json in the config dir so the app starts fresh
    # instead of re-reading the undecryptable file in a loop.
    try:
        import config as _cfg
        _vault_path = os.path.join(
            os.environ.get("CS_CONFIG_DIR") or os.path.dirname(__file__),
            "vault.json")
        if os.path.exists(_vault_path):
            os.remove(_vault_path)
    except Exception:
        pass
    # Reset the Fernet state so _init_fernet() creates a fresh key on next call
    _fernet = None
    _fallback = False


def is_encrypted():
    """True if encryption is active (not fallback plaintext)."""
    _init_fernet()
    return _fernet is not None and not _fallback


def needs_migration(vault):
    """True if any sensitive field is plaintext (not enc:: prefixed)."""
    for settings in vault.values():
        for k in _SENSITIVE_KEYS:
            v = settings.get(k, "")
            if v and not v.startswith(_ENC_PREFIX):
                return True
    return False
