"""Production-grade image-generation queue + rate-limit handling.

Why this exists
---------------
The image endpoint (gpt-image via derouter / OpenAI) enforces a tokens-per-
minute rate limit. Firing a whole batch at once trips `rate_limit_exceeded`
("Limit 4000, Used 4000 ...") and occasional `server_error`s. This module is
 the single place that makes image generation reliable:

  * a global THROTTLE — bounded concurrency, minimum spacing between requests,
    and one shared cooldown so a 429 pauses *every* worker (no retry storms);
  * RETRY with exponential backoff for server/5xx/transient errors and a fixed
    cooldown for rate-limit/429 errors;
  * an async JOB QUEUE for bulk prompts with live per-job status, progress that
    survives individual failures, and per-job retry.

All knobs come from config (IMAGE_* env vars). Nothing here bypasses the API's
limits — the correct fix is queue + cooldown + backoff, which is exactly this.
"""
import json
import os
import random
import re
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import config

# --------------------------------------------------------------------------- #
#  Logging (never logs API keys)
# --------------------------------------------------------------------------- #
_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9_\-\.]{6,})")


def _scrub(text: str) -> str:
    return _KEY_RE.sub("[redacted-key]", str(text or ""))


def _log(**fields):
    """Structured one-line log for every image request / retry decision."""
    parts = []
    for k, v in fields.items():
        if v is None or v == "":
            continue
        if k in ("error", "message"):
            v = _scrub(v)[:300]
        parts.append(f"{k}={v}")
    print("[image_queue] " + " ".join(parts), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
#  Error classification + user-facing messages
# --------------------------------------------------------------------------- #
_RATE_MARKERS = ("rate_limit_exceeded", "rate limit reached", "input-images",
                 "too_many_requests", "429", "limit 4000")
_SERVER_MARKERS = ("server_error", "500", "502", "503", "504",
                   "an error occurred while processing", "bad gateway",
                   "service unavailable", "gateway timeout", "524")
_TRANSIENT_MARKERS = ("timeout", "timed out", "connection", "could not reach",
                      "temporarily", "reset by peer", "connection aborted")
_FATAL_MARKERS = ("authenticationerror", "rejected the key", "unauthorized",
                  "401", "invalid_api_key", "no image api key",
                  "content_policy", "invalid_request_error",
                  "moderation", "billing", "insufficient_quota",
                  "402", "wallet-balance", "wallet balance",
                  "slot reservation failed", "payment_required")
# Billing / wallet markers — a fatal sub-class that gets its own clear,
# actionable message (top up the wallet or configure an OpenRouter fallback)
# instead of the generic "request was rejected".
_BILLING_MARKERS = ("402", "wallet-balance", "wallet balance",
                    "slot reservation failed", "insufficient_quota",
                    "payment_required", "billing")


def classify_error(exc) -> str:
    """Return one of: 'rate_limit', 'server', 'fatal'."""
    low = str(exc or "").lower()
    # Auth/quota/policy first — retrying these is pointless.
    if any(m in low for m in _FATAL_MARKERS):
        # ...unless it's actually a rate-limit dressed up with a 4xx code.
        if not any(m in low for m in _RATE_MARKERS):
            return "fatal"
    if any(m in low for m in _RATE_MARKERS):
        return "rate_limit"
    if any(m in low for m in _SERVER_MARKERS):
        return "server"
    if any(m in low for m in _TRANSIENT_MARKERS):
        return "server"
    # Unknown 4xx (bad prompt, unsupported size, ...) — fail fast, don't burn
    # retries on something permanent.
    if re.search(r"\b4\d\d\b", low):
        return "fatal"
    return "server"


def is_billing_error(exc) -> bool:
    """True when the failure is a payment/wallet/quota problem (HTTP 402 etc.).
    These are fatal — never retried — but warrant a top-up / fallback message
    rather than the generic rejection text."""
    low = str(exc or "").lower()
    if any(m in low for m in _BILLING_MARKERS):
        # A 429 quota that's really a rate-limit isn't a billing wall.
        return not any(m in low for m in _RATE_MARKERS)
    return False


def user_message(kind: str, exc=None) -> str:
    if kind == "rate_limit":
        secs = round(config.IMAGE_RATE_LIMIT_COOLDOWN_MS / 1000)
        return (f"Image API rate limit reached. The queue is cooling down for "
                f"about {secs} seconds, then it will continue automatically.")
    if kind == "server":
        return "OpenAI server error. Retrying automatically with backoff."
    # fatal
    if is_billing_error(exc):
        hint = ("Image provider billing error (HTTP 402): the derouter wallet "
                "balance is too low to reserve a render slot. Top up the "
                "derouter wallet, or set an OpenRouter image key "
                "(OPENROUTER_API_KEY) to fall back to a free image model.")
        detail = _scrub(exc) if exc else ""
        return f"{hint} ({detail[:160]})" if detail else hint
    msg = _scrub(exc) if exc else "the request was rejected"
    return f"Image generation failed: {msg[:200]}"


def _extract_codes(exc):
    """Best-effort (status_code, api_error_code) for logging."""
    low = str(exc or "")
    status = None
    m = (re.search(r"HTTP\s*(\d{3})", low) or re.search(r"Status:\s*(\d{3})", low)
         or re.search(r"\[(\d{3})\]", low) or re.search(r"\b(\d{3})\b", low))
    if m:
        status = m.group(1)
    code = None
    m = (re.search(r"code[=:\"\s]+([a-z_]+)", low, re.I)
         or re.search(r"\"code\"\s*:\s*\"([^\"]+)\"", low))
    if m:
        code = m.group(1)
    return status, code


class ImageError(RuntimeError):
    """Raised when image generation finally fails after all retries.
    Carries a user-facing message and the classification."""
    def __init__(self, message, kind="fatal", original=None, attempts=0):
        super().__init__(message)
        self.kind = kind
        self.original = original
        self.attempts = attempts


class Cancelled(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
#  Global throttle — shared by every image request in the process
# --------------------------------------------------------------------------- #
class _Throttle:
    def __init__(self):
        self._sem = threading.Semaphore(config.IMAGE_MAX_CONCURRENCY)
        self._lock = threading.Lock()
        self._cooldown_until = 0.0     # wall-clock; queue paused until then
        self._next_slot = 0.0          # earliest time the next request may start
        self._cooldown_reason = ""
        self._rl_streak = 0            # consecutive rate-limits since last success
        self.inflight = 0
        self.completed = 0
        self.failed = 0

    # -- cooldown (retry-storm prevention) --------------------------------- #
    def trigger_cooldown(self, reason="rate limit"):
        with self._lock:
            # Adaptive: start short and escalate only if the limit stays
            # saturated (no success between hits), capped at one full TPM window.
            # A success resets the streak so normal batches stay fast.
            self._rl_streak += 1
            base = config.IMAGE_RATE_LIMIT_COOLDOWN_MS / 1000.0
            wait = min(base * self._rl_streak, 60.0)
            until = time.time() + wait
            if until > self._cooldown_until:   # extend, never shorten
                self._cooldown_until = until
                self._cooldown_reason = reason

    def note_success(self):
        with self._lock:
            self._rl_streak = 0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def _wait_cooldown(self, should_stop=None, on_wait=None):
        while True:
            remaining = self.cooldown_remaining()
            if remaining <= 0:
                return
            if on_wait:
                on_wait(remaining)
            _sleep_interruptible(min(remaining, 1.0), should_stop)

    # -- spacing + concurrency -------------------------------------------- #
    def _reserve_slot(self):
        """Block until we're allowed to start a request, honouring the minimum
        gap between requests. Caller MUST hold the concurrency semaphore."""
        with self._lock:
            now = time.time()
            start_at = max(now, self._next_slot)
            self._next_slot = start_at + config.IMAGE_REQUEST_DELAY_MS / 1000.0
        wait = start_at - time.time()
        if wait > 0:
            time.sleep(wait)

    def status(self) -> dict:
        return {
            "inflight": self.inflight,
            "completed": self.completed,
            "failed": self.failed,
            "cooling_down": self.cooldown_remaining() > 0,
            "cooldown_remaining": round(self.cooldown_remaining(), 1),
            "max_concurrency": config.IMAGE_MAX_CONCURRENCY,
        }


THROTTLE = _Throttle()


def throttle_status() -> dict:
    return THROTTLE.status()


def _sleep_interruptible(seconds, should_stop=None):
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if should_stop and should_stop():
            raise Cancelled("stopped")
        time.sleep(min(0.5, max(0.0, end - time.time())))


def _backoff_delay(attempt: int) -> float:
    """delay = min(base * 2**attempt + jitter, max)  (seconds)."""
    base = config.IMAGE_BACKOFF_BASE_MS
    raw = base * (2 ** max(0, attempt - 1)) + random.uniform(0, base)
    return min(raw, config.IMAGE_BACKOFF_MAX_MS) / 1000.0


# --------------------------------------------------------------------------- #
#  The retry core — used by EVERY image request (direct or queued)
# --------------------------------------------------------------------------- #
def run_with_retry(fn: Callable, *, index: int = 0, model: str = "",
                   label: str = "image", on_event: Optional[Callable] = None,
                   should_stop: Optional[Callable] = None):
    """Run ``fn`` (which performs ONE image request and returns its result),
    retrying with backoff/cooldown per config. Raises ImageError on final
    failure, or Cancelled if ``should_stop`` fires.

    ``on_event(dict)`` receives live status updates: running / retrying /
    rate_limited / completed / failed — used to drive the per-job UI.

    The concurrency semaphore is released as soon as a request settles (success
    or error) so a long backoff sleep never holds a slot.
    """
    attempt = 0
    while True:
        THROTTLE._wait_cooldown(
            should_stop,
            on_wait=lambda rem: on_event and on_event({
                "status": "rate_limited", "attempt": attempt,
                "next_retry_in": round(rem, 1),
                "message": user_message("rate_limit")}))
        if should_stop and should_stop():
            raise Cancelled("stopped")

        THROTTLE._sem.acquire()
        slot_released = False
        try:
            THROTTLE._reserve_slot()
            if on_event:
                on_event({"status": "running", "attempt": attempt + 1})
            with THROTTLE._lock:
                THROTTLE.inflight += 1
            t0 = time.time()
            try:
                result = fn()
            finally:
                with THROTTLE._lock:
                    THROTTLE.inflight -= 1
            THROTTLE._sem.release()
            slot_released = True
        except Cancelled:
            if not slot_released:
                THROTTLE._sem.release()
            raise
        except BaseException as e:           # noqa: BLE001
            if not slot_released:
                THROTTLE._sem.release()      # don't hold a slot while backing off
                slot_released = True
            attempt += 1
            kind = classify_error(e)
            status, code = _extract_codes(e)
            final = (kind == "fatal") or (attempt > config.IMAGE_MAX_RETRIES)
            delay = None
            if not final:
                if kind == "rate_limit":
                    THROTTLE.trigger_cooldown(reason=f"429 on {label} #{index}")
                    delay = THROTTLE.cooldown_remaining()
                else:
                    delay = _backoff_delay(attempt)
            _log(label=label, index=index, model=model, attempt=attempt,
                 status_code=status, error_code=code, kind=kind,
                 retry_delay=(round(delay, 1) if delay else None),
                 result=("failed" if final else "will_retry"), error=str(e))
            if final:
                with THROTTLE._lock:
                    THROTTLE.failed += 1
                msg = user_message(kind, e)
                if on_event:
                    on_event({"status": "failed", "attempt": attempt,
                              "message": msg, "error": _scrub(e)[:300]})
                raise ImageError(msg, kind=kind, original=e, attempts=attempt)
            if on_event:
                on_event({"status": "retrying", "attempt": attempt,
                          "next_retry_in": round(delay or 0, 1),
                          "message": user_message(kind, e)})
            _sleep_interruptible(delay or 0, should_stop)
            continue
        else:
            THROTTLE.note_success()        # reset the adaptive cooldown streak
            with THROTTLE._lock:
                THROTTLE.completed += 1
            _log(label=label, index=index, model=model, attempt=attempt + 1,
                 status_code=200, result="ok",
                 message=f"{round(time.time() - t0, 1)}s")
            if on_event:
                on_event({"status": "completed", "attempt": attempt + 1})
            return result


# --------------------------------------------------------------------------- #
#  Async batch queue (bulk prompts -> jobs -> live status)
# --------------------------------------------------------------------------- #
_QUEUE_PATH = os.path.join(config.DATA_DIR, "image_queue.json")
TERMINAL = ("completed", "failed", "cancelled")


class Job:
    def __init__(self, batch_id, index, prompt, meta=None):
        self.id = f"job_{batch_id[-6:]}_{index:04d}"
        self.batch_id = batch_id
        self.index = index
        self.prompt = prompt
        self.meta = meta or {}            # per-prompt extras (e.g. shot_relation)
        self.status = "pending"          # pending|running|retrying|rate_limited|completed|failed|cancelled
        self.attempts = 0
        self.retry_count = 0
        self.next_retry_in = 0.0
        self.message = ""
        self.error = ""
        self.result = None               # the shot record (with image_url) on success
        self.created = time.time()
        self.finished = None

    def to_dict(self):
        return {
            "id": self.id, "batch_id": self.batch_id, "index": self.index,
            "prompt": self.prompt[:200], "status": self.status,
            "attempts": self.attempts, "retry_count": self.retry_count,
            "next_retry_in": round(self.next_retry_in, 1),
            "message": self.message, "error": self.error[:300],
            "result": self.result,
            "meta": self.meta,
        }


class Batch:
    def __init__(self, batch_id, project_id, params, total):
        self.id = batch_id
        self.project_id = project_id
        self.params = params
        self.total = total
        self.created = time.time()
        self.cancelled = False
        self.jobs: List[Job] = []

    def counts(self):
        c = {"pending": 0, "running": 0, "retrying": 0, "rate_limited": 0,
             "completed": 0, "failed": 0, "cancelled": 0}
        for j in self.jobs:
            c[j.status] = c.get(j.status, 0) + 1
        return c

    def is_done(self):
        return all(j.status in TERMINAL for j in self.jobs)

    def to_dict(self):
        counts = self.counts()
        return {
            "id": self.id, "project_id": self.project_id, "total": self.total,
            "created": int(self.created), "cancelled": self.cancelled,
            "counts": counts,
            "done": self.is_done(),
            "jobs": [j.to_dict() for j in self.jobs],
            "throttle": throttle_status(),
        }


class _BatchQueue:
    def __init__(self):
        self._lock = threading.RLock()
        self._batches: Dict[str, Batch] = {}
        self._jobs: Dict[str, Job] = {}
        self._settings: Dict[str, dict] = {}   # batch_id -> settings (in memory only)
        self._render_fn: Optional[Callable] = None
        self._pending = []                      # job ids, FIFO
        self._cond = threading.Condition(self._lock)
        self._workers = []
        self._started = False

    # -- wiring ----------------------------------------------------------- #
    def set_render_fn(self, fn):
        self._render_fn = fn

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
            self._load()
            for i in range(config.IMAGE_MAX_CONCURRENCY):
                t = threading.Thread(target=self._worker, name=f"imgq-{i}",
                                     daemon=True)
                t.start()
                self._workers.append(t)

    # -- public API ------------------------------------------------------- #
    def submit(self, prompts: List[str], params: dict, settings: dict,
               project_id: str, metas: Optional[List[dict]] = None) -> Batch:
        bid = f"batch_{int(time.time())}_{random.randint(1000, 9999)}"
        batch = Batch(bid, project_id, params, len(prompts))
        with self._lock:
            self._batches[bid] = batch
            self._settings[bid] = settings
            for i, p in enumerate(prompts):
                m = metas[i] if (metas and i < len(metas)) else None
                job = Job(bid, i, p, meta=m)
                batch.jobs.append(job)
                self._jobs[job.id] = job
                self._pending.append(job.id)
            self._cond.notify_all()
            self._persist()
        return batch

    def get_batch(self, bid) -> Optional[Batch]:
        return self._batches.get(bid)

    def cancel(self, bid) -> bool:
        with self._lock:
            b = self._batches.get(bid)
            if not b:
                return False
            b.cancelled = True
            for j in b.jobs:
                if j.status in ("pending", "retrying", "rate_limited"):
                    j.status = "cancelled"
                    j.message = "cancelled"
            self._persist()
        return True

    def retry_job(self, job_id, settings=None) -> bool:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j or j.status not in ("failed", "cancelled"):
                return False
            b = self._batches.get(j.batch_id)
            if b:
                b.cancelled = False
            if settings:
                self._settings[j.batch_id] = settings
            j.status = "pending"
            j.message = ""
            j.error = ""
            j.next_retry_in = 0.0
            self._pending.append(j.id)
            self._cond.notify_all()
            self._persist()
        return True

    def retry_failed(self, bid, settings=None) -> int:
        n = 0
        with self._lock:
            b = self._batches.get(bid)
            if not b:
                return 0
            b.cancelled = False
            if settings:
                self._settings[bid] = settings
            for j in b.jobs:
                if j.status in ("failed", "cancelled"):
                    j.status = "pending"
                    j.message = ""
                    j.error = ""
                    j.next_retry_in = 0.0
                    self._pending.append(j.id)
                    n += 1
            if n:
                self._cond.notify_all()
                self._persist()
        return n

    # -- worker ----------------------------------------------------------- #
    def _next_job(self):
        with self._cond:
            while True:
                for jid in list(self._pending):
                    j = self._jobs.get(jid)
                    if j and j.status == "pending":
                        b = self._batches.get(j.batch_id)
                        if b and b.cancelled:
                            self._pending.remove(jid)
                            continue
                        self._pending.remove(jid)
                        return j
                    # stale entry
                    if jid in self._pending:
                        self._pending.remove(jid)
                self._cond.wait(timeout=1.0)

    def _worker(self):
        while True:
            job = self._next_job()
            self._run_job(job)

    def _run_job(self, job: Job):
        b = self._batches.get(job.batch_id)
        settings = self._settings.get(job.batch_id)
        if b is None or settings is None:
            job.status = "failed"
            job.message = "This batch can't resume (server was restarted). Press retry."
            self._persist()
            return

        def on_event(ev):
            st = ev.get("status")
            if st == "retrying":
                job.retry_count += 1
            job.status = st
            job.attempts = ev.get("attempt", job.attempts)
            job.next_retry_in = ev.get("next_retry_in", 0.0)
            if ev.get("message"):
                job.message = ev["message"]
            if ev.get("error"):
                job.error = ev["error"]
            self._persist(throttled=True)

        should_stop = lambda: bool(b.cancelled)
        try:
            result = run_with_retry(
                lambda: self._render_fn(job.prompt,
                                        {**b.params, **(job.meta or {})},
                                        settings, b.project_id),
                index=job.index, model=settings.get("model", config.MODEL),
                label="batch", on_event=on_event, should_stop=should_stop)
            job.result = result
            job.status = "completed"
            job.next_retry_in = 0.0
            job.message = ""
        except Cancelled:
            job.status = "cancelled"
            job.message = "cancelled"
        except ImageError as e:
            job.status = "failed"
            job.message = str(e)
            job.error = _scrub(e.original)[:300] if e.original else str(e)
        except Exception as e:            # noqa: BLE001
            job.status = "failed"
            job.message = f"Image generation failed: {_scrub(e)[:200]}"
            job.error = _scrub(e)[:300]
        finally:
            job.finished = time.time()
            self._persist()

    # -- persistence (no secrets ever written) ----------------------------
    _last_persist = 0.0

    def _persist(self, throttled=False):
        now = time.time()
        if throttled and now - self._last_persist < 0.4:
            return
        self._last_persist = now
        try:
            data = {"batches": []}
            for b in self._batches.values():
                d = {"id": b.id, "project_id": b.project_id,
                     "params": b.params, "total": b.total,
                     "created": b.created, "cancelled": b.cancelled,
                     "jobs": [j.to_dict() for j in b.jobs]}
                data["batches"].append(d)
            os.makedirs(config.DATA_DIR, exist_ok=True)
            tmp = _QUEUE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, _QUEUE_PATH)
        except Exception as e:            # noqa: BLE001
            _log(label="persist", result="error", error=str(e))

    def _load(self):
        if not os.path.exists(_QUEUE_PATH):
            return
        try:
            with open(_QUEUE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for d in data.get("batches", []):
            b = Batch(d["id"], d.get("project_id"), d.get("params", {}),
                      d.get("total", 0))
            b.created = d.get("created", time.time())
            b.cancelled = d.get("cancelled", False)
            for jd in d.get("jobs", []):
                j = Job(b.id, jd.get("index", 0), jd.get("prompt", ""),
                        meta=jd.get("meta"))
                j.id = jd.get("id", j.id)
                j.status = jd.get("status", "pending")
                j.attempts = jd.get("attempts", 0)
                j.retry_count = jd.get("retry_count", 0)
                j.message = jd.get("message", "")
                j.error = jd.get("error", "")
                j.result = jd.get("result")
                # Non-terminal jobs can't resume without secrets -> mark failed
                # but keep all COMPLETED progress intact.
                if j.status not in TERMINAL:
                    j.status = "failed"
                    j.message = ("Interrupted by a server restart — completed "
                                 "frames were kept. Press retry to finish this one.")
                b.jobs.append(j)
                self._jobs[j.id] = j
            self._batches[b.id] = b


QUEUE = _BatchQueue()


# --------------------------------------------------------------------------- #
#  Retention and admission guard
# --------------------------------------------------------------------------- #
# Queue results are polled by id, so retaining a bounded history is useful, but
# keeping every completed batch forever turns both process memory and the
# persisted queue file into an unbounded log. Reject oversized submissions and
# prune the oldest terminal batches once the configured caps are reached.
def _queue_int_env(name, default):
    try:
        return max(1, int(float(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


_MAX_PROMPTS_PER_BATCH = _queue_int_env("IMAGE_QUEUE_MAX_PROMPTS", 400)
_MAX_RETAINED_BATCHES = _queue_int_env("IMAGE_QUEUE_MAX_BATCHES", 100)
_MAX_RETAINED_JOBS = _queue_int_env("IMAGE_QUEUE_MAX_JOBS", 5000)


def _prune_terminal_locked(self):
    """Drop oldest completed/failed/cancelled batches under the retention caps.

    Active batches are never removed. This method must be called with
    ``self._lock`` held.
    """
    def job_count():
        return sum(len(b.jobs) for b in self._batches.values())

    while (len(self._batches) > _MAX_RETAINED_BATCHES
           or job_count() > _MAX_RETAINED_JOBS):
        candidates = [b for b in self._batches.values() if b.is_done()]
        if not candidates:
            break
        victim = min(candidates, key=lambda b: (b.created, b.id))
        self._batches.pop(victim.id, None)
        self._settings.pop(victim.id, None)
        for job in victim.jobs:
            self._jobs.pop(job.id, None)
            try:
                self._pending.remove(job.id)
            except ValueError:
                pass


_ORIGINAL_SUBMIT = _BatchQueue.submit
_ORIGINAL_PERSIST = _BatchQueue._persist


def _bounded_submit(self, prompts, params, settings, project_id, metas=None):
    if len(prompts) > _MAX_PROMPTS_PER_BATCH:
        raise ValueError(
            f"Too many prompts in one image batch ({len(prompts)}); "
            f"maximum is {_MAX_PROMPTS_PER_BATCH}.")
    return _ORIGINAL_SUBMIT(self, prompts, params, settings, project_id, metas)


def _bounded_persist(self, throttled=False):
    # Persist under the same lock used by queue mutation so worker callbacks and
    # status reads cannot serialize a partially-mutated batch dictionary.
    with self._lock:
        _prune_terminal_locked(self)
        return _ORIGINAL_PERSIST(self, throttled=throttled)


_BatchQueue._prune_terminal_locked = _prune_terminal_locked
_BatchQueue.submit = _bounded_submit
_BatchQueue._persist = _bounded_persist
