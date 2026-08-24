"""Agent 3: Performance — Async image gen queue with circuit breaker."""
import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Any, Dict

import config

class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class ImageJob:
    id: str
    prompt: str
    params: Dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    attempts: int = 0
    created_at: float = field(default_factory=time.time)

class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_time=60.0):
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self.failures = 0
        self.last_failure = 0.0
        self.state = "closed"

    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()
        if self.failures >= self.failure_threshold:
            self.state = "open"

    def record_success(self):
        self.failures = 0
        self.state = "closed"

    def can_proceed(self) -> bool:
        if self.state == "closed": return True
        if self.state == "open" and time.time() - self.last_failure > self.recovery_time:
            self.state = "half-open"
            return True
        return self.state == "half-open"

class ImageQueue:
    def __init__(self, generate_fn: Callable):
        self.generate_fn = generate_fn
        self.max_concurrency = config.IMAGE_MAX_CONCURRENCY
        self.max_retries = config.IMAGE_MAX_RETRIES
        self.backoff_base_ms = config.IMAGE_BACKOFF_BASE_MS
        self.backoff_max_ms = config.IMAGE_BACKOFF_MAX_MS
        self.rate_limit_cooldown_ms = config.IMAGE_RATE_LIMIT_COOLDOWN_MS
        self._jobs: Dict[str, ImageJob] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._circuit = CircuitBreaker()
        self._cooldown_until = 0.0
        self._started = False

    async def submit(self, job: ImageJob) -> str:
        if not self._started:
            self._started = True
            for i in range(self.max_concurrency):
                asyncio.create_task(self._worker())
        self._jobs[job.id] = job
        await self._queue.put(job.id)
        return job.id

    async def _worker(self):
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if not job or job.status == JobStatus.CANCELLED:
                self._queue.task_done()
                continue
            await self._process(job)
            self._queue.task_done()

    async def _process(self, job: ImageJob):
        job.status = JobStatus.RUNNING
        while job.attempts < self.max_retries:
            if not self._circuit.can_proceed():
                await asyncio.sleep(5)
                continue
            if time.time() < self._cooldown_until:
                await asyncio.sleep(self._cooldown_until - time.time())
            job.attempts += 1
            try:
                result = await asyncio.to_thread(self.generate_fn, job.prompt, **job.params)
                job.result = result
                job.status = JobStatus.DONE
                self._circuit.record_success()
                return
            except Exception as e:
                err = str(e).lower()
                if "429" in err or "rate_limit" in err:
                    self._cooldown_until = time.time() + (self.rate_limit_cooldown_ms / 1000)
                    self._circuit.record_failure()
                    job.attempts -= 1
                    continue
                if any(c in err for c in ("500", "502", "503")):
                    self._circuit.record_failure()
                    backoff = min(self.backoff_base_ms * (2 ** (job.attempts - 1)), self.backoff_max_ms)
                    await asyncio.sleep(backoff * (0.8 + random.random() * 0.4) / 1000)
                    continue
                job.error = str(e)
                break
        if job.status != JobStatus.DONE:
            job.status = JobStatus.FAILED
            if not job.error: job.error = f"Failed after {job.attempts} attempts"
