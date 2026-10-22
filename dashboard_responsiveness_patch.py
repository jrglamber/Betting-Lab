from __future__ import annotations

"""Keep the curated homepage responsive while its database snapshot refreshes.

The curated dashboard is assembled by synchronous database-heavy code behind an
async route wrapper. Running that wrapper directly on the ASGI event loop can
stall unrelated HTTP requests while a cold snapshot is rebuilt. This patch also
owns the adaptive research bridge so the research layer is installed even when
Railway uses a service-level start-command override that launches dashboard_app.

The final HTML response is normalized here as well. Several older presentation
patches still assign historical dashboard VERSION values later in startup; those
assignments must not downgrade the release badge/title or hide the adaptive nav
entry that was installed successfully earlier in the bootstrap.

No live betting authority is added.
"""

import asyncio
import inspect
import re
import threading
import time
from typing import Any, Dict, Optional

from fastapi.responses import HTMLResponse

VERSION = "DASHBOARD_RESPONSIVENESS_V3"
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


def _finalize_html_response(response: Any) -> Any:
    """Normalize the user-visible release surface after every legacy wrapper.

    This deliberately happens at render time rather than import time. Older
    pooled-entrypoint presentation patches can therefore keep their historical
    constants without being able to downgrade what the browser actually sees.
    """
    if not isinstance(response, HTMLResponse):
        return response

    html = response.body.decode("utf-8", errors="replace")
    html = re.sub(
        r"(<title>Betting Lab v)[^<]+(</title>)",
        rf"\g<1>{BUILD_VERSION}\g<2>",
        html,
        count=1,
    )
    html = re.sub(
        r"(Betting Lab <span class='pill'>v)[^<]+(</span>)",
        rf"\g<1>{BUILD_VERSION}\g<2>",
        html,
        count=1,
    )

    # Put the adaptive research entry in the actual top navigation. The adaptive
    # module also surfaces a link defensively, but older homepage layouts do not
    # contain its preferred marker and can leave that link lower down the page.
    adaptive_link = "<a href='/adaptive-research'>Adaptive Winner Research</a>"
    predictive_marker = "<a href='#predictive'>Predictive Models</a>"
    topnav_start = html.find("<div class='topnav'>")
    topnav_end = html.find("</div>", topnav_start) if topnav_start >= 0 else -1
    topnav_html = html[topnav_start:topnav_end] if topnav_start >= 0 and topnav_end > topnav_start else ""
    if adaptive_link not in topnav_html:
        if predictive_marker in html:
            html = html.replace(
                predictive_marker,
                predictive_marker + adaptive_link,
                1,
            )
        elif topnav_start >= 0:
            html = html.replace(
                "<div class='topnav'>",
                "<div class='topnav'>" + adaptive_link,
                1,
            )

    headers = {
        k: v for k, v in response.headers.items()
        if k.lower() != "content-length"
    }
    return HTMLResponse(
        content=html,
        status_code=response.status_code,
        headers=headers,
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
        return _finalize_html_response(response)

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
        "final_html_guard": {
            "release_version": BUILD_VERSION,
            "adaptive_topnav": True,
        },
        "presentation_only": True,
    }
