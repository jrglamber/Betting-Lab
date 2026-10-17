from __future__ import annotations

"""Presentation-only bridge from the curated Betting Lab homepage to FHG1."""

import inspect
from typing import Any, Dict

from fastapi.responses import HTMLResponse

VERSION = "FHG1_CURATED_LINK_V1"


def install(app) -> Dict[str, Any]:
    root = next(
        (
            r
            for r in list(app.router.routes)
            if getattr(r, "path", None) == "/"
            and "GET" in (getattr(r, "methods", set()) or set())
        ),
        None,
    )
    if root is None:
        return {"installed": False, "version": VERSION, "reason": "root_not_found"}
    if getattr(root.endpoint, "_fhg1_curated_link", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    original = root.endpoint
    name = root.name
    include = getattr(root, "include_in_schema", True)
    app.router.routes.remove(root)

    async def wrapped_home():
        response = original()
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, HTMLResponse):
            return response
        html = response.body.decode("utf-8", errors="replace")
        if "href='/first-half-goals'" not in html:
            anchor = "<a href='/clv-first'>CLV & Price</a>"
            replacement = "<a href='/first-half-goals'>First-Half Goals</a>" + anchor
            if anchor in html:
                html = html.replace(anchor, replacement, 1)
            else:
                html = html.replace(
                    "</div>",
                    "<a href='/first-half-goals'>First-Half Goals</a></div>",
                    1,
                )
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

    wrapped_home._fhg1_curated_link = True
    app.add_api_route(
        "/",
        wrapped_home,
        methods=["GET"],
        response_class=HTMLResponse,
        name=name,
        include_in_schema=include,
    )
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "route": "/first-half-goals",
    }
