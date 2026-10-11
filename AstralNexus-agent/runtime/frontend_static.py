"""SPA dist server over the webserver fallback seat.

Port of ``@deepseek-ai/dsh-host-frontend-static`` (commit 5badb15,
lib/index.js 99 lines, read in full): serves a built frontend directory
with explicit index entry points. A readable index renders at the dist root
and the configured index path; missing paths return 404, traversal outside
the dist root is 403, unknown extensions ship as octet-stream, and
non-GET/HEAD is 405. Every index response first passes the connection's
browser authentication, then the webserver's index render (structured
injection rows, then raw taps). Non-index assets stay public. The dist
location is workspace knowledge of the composing application, so
``dist_index`` is supplied by the composition, never hardcoded by a
deployment.

Python adaptations (documented):
- node:http res object → a (status, headers, body) response tuple the
  caller's framework serializes; ``authorize_index`` returns None to let
  the request through or its own response tuple to answer it (upstream:
  the authenticator writes the response and serveStatic returns).
- ``webServer.renderIndex`` (structured injection rows + raw taps) belongs
  to dsh-host-webserver, not yet ported: ``render_index`` is an injected
  callable so the composing host supplies its own injection rendering; the
  ``<base href="./">`` splice after the opening ``<head>`` tag ships here
  verbatim.
- STATIC_MISS_CODES (ENOENT/EISDIR/ENOTDIR) → the matching OSError
  families; on Windows reading a directory raises PermissionError, which
  counts as a miss only when the target IS a directory (a genuine
  permission failure still rethrows, as upstream).
- register_frontend_static_routes() claims the fallback seat on a Flask
  app with a catch-all route (405 for non-GET/HEAD, decoded pathname,
  traversal fence) — the composition point for hosts that serve a dist.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, Optional, Tuple

__all__ = [
    "HTML_MIME", "MIME", "STATIC_MISS_CODES", "serve_static",
    "validate_frontend_static_config", "register_frontend_static_routes",
    "splice_base_href",
]

Response = Tuple[int, Dict[str, str], Any]

HTML_MIME = "text/html; charset=utf-8"

MIME = {
    ".html": HTML_MIME,
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".gz": "application/gzip",
}

#: errno families that mean "not a servable file here" (→ 404), as opposed
#: to a genuine failure (→ rethrow): ENOENT / EISDIR / ENOTDIR.
STATIC_MISS_CODES = frozenset({"ENOENT", "EISDIR", "ENOTDIR"})

_HEAD_OPEN = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)


def splice_base_href(html: str) -> str:
    """Inject ``<base href="./">`` right after the opening head tag — the
    dist renders document-relative regardless of the mount path."""
    return _HEAD_OPEN.sub(lambda match: f'{match.group(0)}<base href="./">',
                          html, count=1)


def validate_frontend_static_config(
        config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """z.object({distIndex: z.string().required()}) natively."""
    config = dict(config or {})
    dist_index = config.get("dist_index")
    if not isinstance(dist_index, str) or dist_index == "":
        raise ValueError("dist_index is required and must be a non-empty string")
    return {"dist_index": dist_index}


def _is_miss(error: OSError, target: str) -> bool:
    if isinstance(error, (FileNotFoundError, NotADirectoryError,
                          IsADirectoryError)):
        return True
    # Windows raises PermissionError when a directory is opened as a file —
    # the EISDIR miss, but only when the target really is a directory
    if isinstance(error, PermissionError) and os.path.isdir(target):
        return True
    return False


def serve_static(pathname: str, dist_root: str, dist_index: str,
                 authorize_index: Callable[[], Optional[Response]],
                 render_index: Callable[[], str]) -> Response:
    """Serve one GET/HEAD static request from the dist root.

    Args:
        pathname: decoded URL pathname of the request.
        dist_root: absolute dist root directory (resolved by the caller).
        dist_index: absolute path of index.html inside dist_root.
        authorize_index: authenticates an index response before its bytes
            are read; returns None to proceed or a response tuple to answer.
        render_index: produces the index.html body (structured injection
            rendering) for the dist root and configured index path.

    Returns:
        (status, headers, body) — 403 on traversal, 404 on a miss, else 200
        with the resolved content type.
    """
    # Node's path.join(distRoot, pathname) concatenates and normalizes; a
    # leading "/" in the pathname must NOT re-root to the drive (Python's
    # os.path.join would), so the root slashes are stripped and the internal
    # ".." runs stay — normalization plus the fence below is what catches
    # traversal, exactly as upstream.
    target = os.path.abspath(os.path.normpath(
        os.path.join(dist_root, pathname.lstrip("/\\"))))
    if target != dist_root and not target.startswith(dist_root + os.sep):
        return 403, {}, b""
    try:
        if target == dist_root or target == dist_index:
            rejection = authorize_index()
            if rejection is not None:
                return rejection
            body: Any = render_index().encode("utf-8")
            content_type = HTML_MIME
        else:
            with open(target, "rb") as handle:
                body = handle.read()
            content_type = MIME.get(os.path.splitext(target)[1],
                                    "application/octet-stream")
    except OSError as error:
        if not _is_miss(error, target):
            raise
        return 404, {}, b""
    return 200, {"content-type": content_type}, body


def register_frontend_static_routes(
        app: Any, dist_index: str,
        authorize_index: Callable[[], Optional[Response]],
        render_index: Optional[Callable[[], str]] = None) -> None:
    """Claim the fallback seat on a Flask app and serve the dist.

    The composed render_index defaults to reading the dist index file and
    splicing the base href — a host with structured injection rows supplies
    its own renderer wrapping this one.
    """
    from flask import request, Response as FlaskResponse

    dist_root = os.path.dirname(os.path.abspath(dist_index))
    absolute_index = os.path.abspath(dist_index)

    def default_render() -> str:
        with open(absolute_index, encoding="utf-8") as handle:
            return splice_base_href(handle.read())

    renderer = render_index or default_render

    @app.route("/<path:pathname>", methods=["GET", "HEAD", "POST", "PUT",
                                             "PATCH", "DELETE", "OPTIONS"])
    @app.route("/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
                             "OPTIONS"])
    def frontend_static_fallback(pathname: str = ""):
        if request.method not in ("GET", "HEAD"):
            return FlaskResponse(status=405)
        from urllib.parse import unquote
        status, headers, body = serve_static(
            unquote("/" + pathname), dist_root, absolute_index,
            authorize_index, renderer)
        return FlaskResponse(body, status=status, headers=headers)
