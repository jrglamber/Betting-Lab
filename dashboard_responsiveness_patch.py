from __future__ import annotations

"""Keep the curated homepage responsive while its database snapshot refreshes.

The curated dashboard is assembled by synchronous database-heavy code behind an
async route wrapper. Running that wrapper directly on the ASGI event loop can
stall unrelated HTTP requests while a cold snapshot is rebuilt. This patch also
owns the adaptive research bridge so the research layer is installed even when
Railway uses a service-level start-command override that launches dashboard_app.

No live betting authority is added.
"""

import asyncio
import inspect
import threading
import time
from typing import Any, Dict, Optional

from fastapi.responses import HTMLResponse

VERSION = "DASHBOARD_RESPONSIVENESS_V2"
BUILD_VERSION = "0.19.78"
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


def _install_adaptive_layer(app) -> Dict[str, Any]:
    """Install the research-only adaptive layer through the entrypoint we know is live.

    Imports are deliberately deferred until dashboard_app has completed its
    existing imports. This avoids circular bootstrap dependencies while making
    the deployment independent of Railway's configured start command.
    """
    try:
        import web as web_module
        import dashboard_layout_v2 as layout
        import adaptive_winner_research as adaptive_core
        from adaptive_winner_research_safety_patch import install as install_adaptive_safety
        from adaptive_tpred4_identity_patch import install as install_tpred4_identity

        adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
        layout.VERSION = BUILD_VERSION
        web_module.VERSION = BUILD_VERSION
        app.version = BUILD_VERSION

        safety = install_adaptive_safety(web_module.db)
        identity = install_tpred4_identity(web_module.db)
        adaptive = adaptive_core.install(
            app,
            web_module.db,
            web_module.BASE_STYLE,
            tennis_engine=getattr(web_module, "tennis_engine", None),
        )

        # Never expose the heavyweight maintenance endpoint publicly. Normal
        # maintenance runs from the wrapped tennis research cycle.
        app.router.routes[:] = [
            route for route in app.router.routes
            if getattr(route, "path", None) != "/admin/adaptive-research/run"
        ]

        # Reassert release identity after all adaptive route work.
        layout.VERSION = BUILD_VERSION
        web_module.VERSION = BUILD_VERSION
        app.version = BUILD_VERSION
        print(
            "ADAPTIVE_DASHBOARD_BRIDGE "
            + str({
                "ok": True,
                "version": BUILD_VERSION,
                "safety": safety,
                "identity": identity,
                "adaptive": adaptive,
            }),
            flush=True,
        )
        return {
            "ok": True,
            "version": BUILD_VERSION,
            "safety": safety,
            "identity": identity,
            "adaptive": adaptive,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "version": BUILD_VERSION,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(f"ADAPTIVE_DASHBOARD_BRIDGE_ERROR {result}", flush=True)
        return result


def install(app) -> Dict[str, Any]:
    global _CACHE, _REFRESHING

    # Install adaptive research BEFORE capturing the final root endpoint. Its
    # homepage-link wrapper is then itself wrapped synchronously below, preserving
    # the responsiveness guarantee.
    adaptive = _install_adaptive_layer(app)

    root = next(
        (
            r for r in list(app.router.routes)
            if getattr(r, "path", None) == "/"
            and "GET" in (getattr(r, "methods", set()) or set())
        ),
        None,
    )
    if root is None:
        return {
            "installed": False,
            "version": VERSION,
            "reason": "root_not_found",
            "adaptive": adaptive,
        }
    if getattr(root.endpoint, "_dashboard_responsive", False):
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "build_version": BUILD_VERSION,
            "adaptive": adaptive,
        }

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
            # Keep serving the last known-good page. A later request will retry.
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

        # First render after a deployment has no stale copy to serve. It still
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
        "build_version": BUILD_VERSION,
        "fresh_seconds": FRESH_SECONDS,
        "policy": "serve_last_good_while_refreshing",
        "execution": "fastapi_threadpool",
        "adaptive": adaptive,
        "presentation_only": True,
    }
