"""Non-invasive UI patch loader for Continuity Studio.

Why this exists:
  index.html is ~505KB and references fonts + pro-UI assets that aren't
  wired in. Rather than risk corrupting that huge file with a full rewrite,
  this wrapper imports the existing FastAPI `app` unchanged and injects the
  three UI asset tags (fonts, animations.css, ui_extras.css, enhancements.js)
  into the served HTML on the fly, right before </head>.

How to use:
  Instead of:  uvicorn app:app --port 8000
  Run:         uvicorn ui_patch:app --port 8000

  (Docker CMD and desktop launcher can be pointed at ui_patch:app too.)

What it does NOT change:
  - app.py stays byte-for-byte identical
  - index.html stays byte-for-byte identical
  - all routes, auth, middleware behave exactly as before
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Import the existing app untouched
from app import app

# The tags to inject. animations.css MUST come before ui_extras.css so the
# @import font rule in ui_extras loads, and enhancements.js runs after paint.
_INJECT = (
    '<link rel="stylesheet" href="/static/animations.css">'
    '<link rel="stylesheet" href="/static/ui_extras.css">'
    '<script defer src="/static/enhancements.js"></script>'
)


class UIInjectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)

        # Only touch the root HTML document
        if request.url.path != "/":
            return response
        ctype = response.headers.get("content-type", "")
        if "text/html" not in ctype:
            return response

        # Read the streamed body
        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        try:
            html = body.decode("utf-8")
        except UnicodeDecodeError:
            # Not decodable text — pass through untouched
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=ctype,
            )

        # Inject once, right before </head> (fallback: before </body>)
        if _INJECT not in html:
            if "</head>" in html:
                html = html.replace("</head>", _INJECT + "</head>", 1)
            elif "</body>" in html:
                html = html.replace("</body>", _INJECT + "</body>", 1)
            else:
                html = html + _INJECT

        new_body = html.encode("utf-8")
        headers = dict(response.headers)
        headers.pop("content-length", None)  # length changed after injection
        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )


app.add_middleware(UIInjectMiddleware)
