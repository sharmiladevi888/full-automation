"""Agent 7: Standardized API responses."""
from typing import Any, Optional, Dict
from fastapi.responses import JSONResponse

def success(data: Any = None, meta: Optional[Dict] = None, status: int = 200) -> JSONResponse:
    body = {"ok": True}
    if data is not None: body["data"] = data
    if meta: body["meta"] = meta
    return JSONResponse(content=body, status_code=status)

def error(message: str, status: int = 400, details: Optional[Dict] = None) -> JSONResponse:
    body = {"ok": False, "error": message}
    if details: body["details"] = details
    return JSONResponse(content=body, status_code=status)

def created(data: Any = None) -> JSONResponse:
    return success(data=data, status=201)

def paginated(items: list, total: int, page: int = 1, per_page: int = 20) -> JSONResponse:
    return success(data=items, meta={"total": total, "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page})

def not_found(resource: str = "Resource") -> JSONResponse:
    return error(f"{resource} not found", status=404)

def rate_limited(retry_after: int = 60) -> JSONResponse:
    resp = error("Rate limited. Try again later.", status=429)
    resp.headers["Retry-After"] = str(retry_after)
    return resp
