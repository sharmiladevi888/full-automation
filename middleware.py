"""Agent 4: Architecture + Agent 5: Error Handling

Centralized middleware stack:
- Global exception handler (no more 500s leaking stack traces)
- Request timing / metrics
- CORS properly configured
- Security headers
- Request ID injection
"""
import time
import uuid
import traceback
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


def setup_middleware(app: FastAPI):
    """Attach all middleware to the app."""

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Callable):
        """Inject a unique request ID for tracing."""
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.middleware("http")
    async def timing_middleware(request: Request, call_next: Callable):
        """Track request duration."""
        start = time.perf_counter()
        response = await call_next(request)
        duration = (time.perf_counter() - start) * 1000  # ms
        response.headers["X-Response-Time"] = f"{duration:.1f}ms"
        # Log slow requests
        if duration > 5000:
            print(f"[SLOW] {request.method} {request.url.path} took {duration:.0f}ms")
        return response

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next: Callable):
        """Add security headers to all responses."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Prevent MIME sniffing on generated content
        if request.url.path.startswith("/data/"):
            response.headers["Content-Security-Policy"] = "default-src 'none'"
        return response


def setup_exception_handlers(app: FastAPI):
    """Global exception handlers - never leak stack traces to client."""

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Catch-all: log the full trace, return clean error to client."""
        req_id = getattr(request.state, 'request_id', 'unknown')
        # Log full traceback server-side
        print(f"[ERROR][{req_id}] {request.method} {request.url.path}")
        print(traceback.format_exc())
        
        # Categorize errors for useful client messages
        err_str = str(exc).lower()
        
        if "rate_limit" in err_str or "429" in err_str:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limited. Please wait a moment and try again.", "request_id": req_id},
            )
        if "402" in err_str or "wallet" in err_str or "billing" in err_str:
            return JSONResponse(
                status_code=402,
                content={"detail": "Billing issue. Check your API provider balance.", "request_id": req_id},
            )
        if "timeout" in err_str:
            return JSONResponse(
                status_code=504,
                content={"detail": "Request timed out. Try again or reduce quality.", "request_id": req_id},
            )
        if "not found" in err_str or "404" in err_str:
            return JSONResponse(
                status_code=404,
                content={"detail": "Resource not found.", "request_id": req_id},
            )
        
        # Generic 500
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Check logs for details.", "request_id": req_id},
        )

    @app.exception_handler(ValueError):
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc)},
        )


class APIError(Exception):
    """Structured API error with status code."""

    def __init__(self, message: str, status_code: int = 500, details: dict = None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content={"detail": self.message, **self.details},
        )
