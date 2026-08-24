# Contributing to Continuity Studio

## Quick Start

```bash
git clone https://github.com/vamsickN/full-automation.git
cd full-automation
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # add your API keys
uvicorn app:app --reload --port 8000
```

## Docker

```bash
docker compose up --build
```

## Tests

```bash
pip install pytest
pytest tests/ -v
```

## Activating the Pro UI

Add to `static/index.html`:

```html
<!-- In <head> -->
<link rel="stylesheet" href="/static/animations.css">

<!-- Before </body> -->
<script src="/static/enhancements.js"></script>
```

Gives you: glassmorphism, cursor glow, ripple clicks, staggered animations, scroll reveals, keyboard shortcuts (Ctrl+1-9), loading bar, card tilt effects.

## Key Modules

| File | Purpose |
|------|--------|
| `security.py` | Rate limiting, input validation, auth helpers |
| `atomic_store.py` | Thread-safe state persistence |
| `process_manager.py` | Safe subprocess with proper kill |
| `image_queue_v2.py` | Async image gen with circuit breaker |
| `api_response.py` | Standardized JSON responses |
| `logger.py` | Structured logging (JSON or pretty) |

## Rules

1. No secrets in git — use .env + vault_crypto
2. Use `atomic_store.py` for state, not raw json.load/dump
3. Use `process_manager.run_safe()` for subprocesses
4. Use `api_response.py` for all API returns
5. Use `logger.py`, not print()
