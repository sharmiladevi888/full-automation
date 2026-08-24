# Bugs & Issues Found in full-automation

## Critical

1. **Race condition in state writes** — `store.py` has no locking. Use `atomic_store.py` instead.
2. **Vault decryption crash** — Corrupt `vault.json` takes down the auth middleware. Wrap in try/except.
3. **Zombie ffmpeg processes** — `_run_capture` timeout doesn't kill children. Use `process_manager.py`.

## High

4. **No MIME validation on uploads** — Users can upload .exe as video. Wire `security.validate_upload()`.
5. **No rate limiting on auth** — Brute-force wide open. Wire `security.login_limiter`.
6. **Log files in repo** — Committed with potentially sensitive data. Removed + gitignored.
7. **Backup files in repo** — 160KB of dead weight (.bak, .gate). Run `cleanup.py`.

## Medium

8. **Inconsistent error format** — Some return `{detail}`, others `{error}`. Use `api_response.py`.
9. **No HTTPS on cookies** — Session cookies sent cleartext. Add `secure=True` in production.
10. **Hardcoded model lists** — Go stale as providers update. Fetch dynamically.
11. **Monolithic 441KB app.py** — Split into modules over time.
12. **No brute-force protection** — Fixed with `security.py` rate limiter.

## Fixed by Agent Commits

- ✅ Security module added (rate limiting, validation, sanitization)
- ✅ Atomic state persistence (no more corruption)
- ✅ Process manager (kills zombie ffmpeg)
- ✅ Standardized API responses
- ✅ Structured logging
- ✅ Docker support
- ✅ Test suite
- ✅ Pro UI animations + interactions
