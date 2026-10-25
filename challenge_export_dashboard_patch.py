from __future__ import annotations

"""Presentation-only link for the lightweight challenge export."""

import inspect
from typing import Any, Dict

from fastapi.responses import HTMLResponse

VERSION = "CHALLENGE_EXPORT_DASHBOARD_V1"
LINK = "<a href='/export/challenge.zip'>Challenge Export</a>"


def install(app) -> Dict[str, Any]:
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
    if getattr(root.endpoint, "_challenge_export_dashboard", False):
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
        if "href='/export/challenge.zip'" not in html:
            anchor = "<a href='/export/research-weekly.zip'>Exports</a>"
            if anchor in html:
                html = html.replace(anchor, anchor + LINK, 1)
            else:
                html = html.replace("</body>", f"<div class='nav'>{LINK}</div></body>", 1)
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return HTMLResponse(html, status_code=response.status_code, headers=headers)

    wrapped_home._challenge_export_dashboard = True
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
        "link": "/export/challenge.zip",
        "presentation_only": True,
    }
