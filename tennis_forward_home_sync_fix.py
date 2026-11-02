from __future__ import annotations

"""Keep the Tennis Forward Readiness homepage link on the synchronous route.

The production homepage intentionally runs as a synchronous FastAPI endpoint so
its existing async bridge executes in FastAPI's worker thread. The first tennis
forward readiness wrapper was async and called that synchronous endpoint from a
running event loop, which made the deeper asyncio.run bridge fail.

This patch unwraps only the tennis presentation wrapper and recreates it as a
synchronous route. No research, pricing, settlement, staking, alerting or betting
logic is changed.
"""

import asyncio
import inspect
from typing import Any, Dict

from fastapi.responses import HTMLResponse

VERSION = "TENNIS_FORWARD_HOME_SYNC_FIX_V1"


def _closure_value(func: Any, name: str) -> Any:
    code = getattr(func, "__code__", None)
    closure = getattr(func, "__closure__", None)
    freevars = tuple(getattr(code, "co_freevars", ()) or ())
    if not closure or name not in freevars:
        return None
    try:
        return closure[freevars.index(name)].cell_contents
    except Exception:
        return None


def install(app) -> Dict[str, Any]:
    route = next(
        (
            r
            for r in list(app.router.routes)
            if getattr(r, "path", None) == "/"
            and "GET" in (getattr(r, "methods", set()) or set())
        ),
        None,
    )
    if route is None:
        return {"installed": False, "version": VERSION, "reason": "root_not_found"}

    endpoint = route.endpoint
    if getattr(endpoint, "_tennis_forward_home_sync_fix", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    # tennis_forward_readiness_patch._surface_home_link closes over the previous
    # (synchronous) homepage endpoint as `original`. Bypass the async wrapper so
    # the prior sync bridge remains in FastAPI's threadpool.
    original = _closure_value(endpoint, "original")
    if original is None:
        return {
            "installed": False,
            "version": VERSION,
            "reason": "tennis_wrapper_original_not_found",
            "endpoint": getattr(endpoint, "__name__", type(endpoint).__name__),
        }

    name = route.name
    include = getattr(route, "include_in_schema", True)
    app.router.routes.remove(route)

    def wrapped():
        response = original()
        if inspect.isawaitable(response):
            response = asyncio.run(response)
        if not isinstance(response, HTMLResponse):
            return response

        html = response.body.decode("utf-8", errors="replace")
        if "href='/tennis-forward-readiness'" not in html:
            link = "<a href='/tennis-forward-readiness'>Tennis Forward Readiness</a>"
            marker = "<a href='/tennis-predictive'>Tennis Predictive</a>"
            html = (
                html.replace(marker, marker + link, 1)
                if marker in html
                else html.replace("<div class='nav'>", "<div class='nav'>" + link, 1)
            )

        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() != "content-length"
        }
        return HTMLResponse(
            content=html,
            status_code=response.status_code,
            headers=headers,
        )

    wrapped._tennis_forward_home_sync_fix = True
    app.add_api_route(
        "/",
        wrapped,
        methods=["GET"],
        response_class=HTMLResponse,
        name=name,
        include_in_schema=include,
    )
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "presentation_only": True,
        "execution": "fastapi_threadpool",
        "tennis_forward_link_preserved": True,
        "betting_authority_changed": False,
    }
