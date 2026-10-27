from __future__ import annotations

"""Repair the curated homepage wrapper chain under the adaptive entrypoint.

The responsiveness layer intentionally exposes a synchronous FastAPI endpoint so
its heavier renderer executes in FastAPI's threadpool. The 25-Oct research
priority surface historically wrapped that endpoint with an async route. Calling
the synchronous responsive endpoint directly from that active event loop caused
its internal ``asyncio.run`` bridge to fail on a cold render.

This patch replaces only that outer presentation wrapper with an equivalent
synchronous wrapper. No model, candidate, price, settlement, staking, alerting,
or execution logic is changed.
"""

import asyncio
import inspect
from typing import Any, Dict

from fastapi.responses import HTMLResponse

import oct25_research_priorities_patch as priorities

VERSION = "HOMEPAGE_SYNC_BRIDGE_2026_10_27_V1"


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


def install(app, db) -> Dict[str, Any]:
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
    if getattr(endpoint, "_homepage_sync_bridge", False):
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
        }

    original = _closure_value(endpoint, "original")
    if original is None:
        return {
            "installed": False,
            "version": VERSION,
            "reason": "priority_wrapper_original_not_found",
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
        if "id='priorities'" not in html:
            marker = "<section id='multiples'>"
            section = priorities._priority_section(db)
            html = (
                html.replace(marker, section + marker, 1)
                if marker in html
                else html.replace("</body>", section + "</body>", 1)
            )
        if "href='#priorities'" not in html:
            marker = "<a href='#leaderboard'>Research Leaderboard</a>"
            link = "<a href='#priorities'>Priority Watch</a>"
            html = (
                html.replace(marker, marker + link, 1)
                if marker in html
                else html
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

    wrapped._homepage_sync_bridge = True
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
    }
