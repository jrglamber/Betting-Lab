from __future__ import annotations

"""Keep the curated homepage responsive while its database snapshot refreshes.

The curated dashboard is assembled by synchronous database-heavy code behind an
async route wrapper.  Running that wrapper directly on the ASGI event loop can
stall unrelated HTTP requests while a cold snapshot is rebuilt.  This patch
re-registers only the final homepage route as a normal synchronous FastAPI
endpoint, so FastAPI executes the render in its threadpool.  It also serves the
last successful HTML immediately while a stale copy is refreshed in a daemon
thread.

Presentation only: no collector, selection, settlement, staking, Telegram or
betting authority is changed.
"""

import asyncio
import inspect
import threading
import time
from typing import Any, Dict, Optional

from fastapi.responses import HTMLResponse

VERSION = "DASHBOARD_RESPONSIVENESS_V1"
FRESH_SECONDS = 20.0

_LOCK = threading.Lock()
_CACHE: Optional[Dict[str, Any]] = None
_REFRESHING = False


def _clone(entry: Dict[str, Any]) -> HTMLResponse:
    return HTMLResponse(
        content=str(entry.get("html") or ""),
        status_code=int(entry.get("status_code") or 200),
        headers=dict(entry.get("headers") or {}),
    )


def install(app) -> Dict[str, Any]:
    global _CACHE, _REFRESHING

    root = next(
        (
            r for r in list(app.router.routes)
            if getattr(r, "path", None) == "/"
            and "GET" in (getattr(r, "methods", set()) or set())
        ),
        None,
    )
    if root is None:
        return {"installed": False, "version": VERSION, "reason": "root_not_found"}
    if getattr(root.endpoint, "_dashboard_responsive", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = root.endpoint
    name = root.name
    include = getattr(root, "include_in_schema", True)
    app.router.routes.remove(root)

    def render() -> Any:
        response = original()
        if inspect.isawaitable(response):
            # This function is reached through a synchronous FastAPI endpoint,
            # therefore it runs in the threadpool and has no active event loop.
            response = asyncio.run(response)
        return response

    def store(response: Any) -> None:
        global _CACHE
        if not isinstance(response, HTMLResponse):
            return
        headers = {
            k: v for k, v in response.headers.items()
            if k.lower() != "content-length"
        }
        entry = {
            "at": time.monotonic(),
            "html": response.body.decode("utf-8", errors="replace"),
            "status_code": response.status_code,
            "headers": headers,
        }
        with _LOCK:
            _CACHE = entry

    def refresh_background() -> None:
        global _REFRESHING
        try:
            response = render()
            store(response)
        except Exception:
            # Keep serving the last known-good page.  A later request will retry.
            pass
        finally:
            with _LOCK:
                _REFRESHING = False

    def responsive_home():
        global _REFRESHING
        now = time.monotonic()
        launch_refresh = False
        cached = None
        with _LOCK:
            if _CACHE is not None:
                cached = dict(_CACHE)
                age = now - float(_CACHE.get("at") or 0.0)
                if age > FRESH_SECONDS and not _REFRESHING:
                    _REFRESHING = True
                    launch_refresh = True

        if cached is not None:
            if launch_refresh:
                threading.Thread(
                    target=refresh_background,
                    daemon=True,
                    name="dashboard-refresh",
                ).start()
            return _clone(cached)

        # First render after a deployment has no stale copy to serve.  It still
        # runs in FastAPI's threadpool, so other routes remain responsive.
        response = render()
        store(response)
        return response

    responsive_home._dashboard_responsive = True
    app.add_api_route(
        "/",
        responsive_home,
        methods=["GET"],
        response_class=HTMLResponse,
        name=name,
        include_in_schema=include,
    )
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "fresh_seconds": FRESH_SECONDS,
        "policy": "serve_last_good_while_refreshing",
        "execution": "fastapi_threadpool",
        "presentation_only": True,
    }
