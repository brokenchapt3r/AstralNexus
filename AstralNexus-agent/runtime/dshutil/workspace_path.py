"""Browser-safe workspace path and display helpers + file address grammar.

Port of ``@deepseek-ai/dsh-util-workspace-path`` (commit 5badb15). Two halves:

1. The ``dsh-resource://file/…`` address grammar (built and parsed without
   touching a filesystem): ``session`` scope for paths read through one
   session, ``absolute`` scope for absolute paths (UNC keeps one empty first
   segment; a drive segment keeps no leading slash). Parsing never resolves
   ``.``/``..``, ignores query/fragment, and refuses invalidly-encoded
   segments (upstream: ``decodeURIComponent`` throws → ``undefined``).
2. Display/resolution helpers: windows-style detection, workspace resolution,
   home abbreviation (POSIX only), title/parts extraction, cwd relativizing,
   and ``file_media_url`` for the authenticated file route.

Python adaptation: percent-encoding follows ``encodeURIComponent`` exactly
(unreserved set plus ``!*'()``; ``:`` restored literal for drive letters);
strict decoding validates every ``%XX`` escape and UTF-8, so malformed
addresses return ``None`` like the upstream catch. ``file_media_url`` joins
per RFC 3986 (``urljoin``), matching WHATWG URL for the accepted bases.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import string
from typing import Optional
from urllib.parse import unquote, urljoin, quote

FILE_ADDRESS_PREFIX = "dsh-resource://file/"
_ENCODE_SAFE = "!*'()"
_DRIVE_SEGMENT = re.compile(r"^[A-Za-z]:$")


def _encode_segment(segment: str) -> str:
    """encodeURIComponent for one id or path segment, keeping ':' literal."""
    return quote(segment, safe=_ENCODE_SAFE).replace("%3A", ":")


def _encode_path(path: str) -> str:
    return "/".join(_encode_segment(segment) for segment in path.split("/"))


def _decode_segment(segment: str) -> str:
    """decodeURIComponent with the same failure surface: raise on malformed input."""
    index = 0
    while index < len(segment):
        if segment[index] == "%":
            escape = segment[index + 1:index + 3]
            if len(escape) != 2 or not all(char in string.hexdigits for char in escape):
                raise ValueError(f"invalid percent-escape in segment: {segment!r}")
            index += 3
        else:
            index += 1
    return unquote(segment, errors="strict")


def is_drive_segment(segment: Optional[str]) -> bool:
    return segment is not None and bool(_DRIVE_SEGMENT.match(segment))


def session_file_address(session_id: str, path: str) -> str:
    """Address of a file read through one Session."""
    normalized = re.sub(r"^(?:\./)+", "", path.replace("\\", "/"))
    return f"{FILE_ADDRESS_PREFIX}session/{_encode_segment(session_id)}/{_encode_path(normalized)}"


def absolute_file_address(path: str) -> str:
    """Address of a file by absolute path (UNC keeps one empty first segment)."""
    normalized = path.replace("\\", "/")
    unc = normalized.startswith("//")
    absolute = re.sub(r"^/+", "", normalized)
    return f"{FILE_ADDRESS_PREFIX}absolute/{'/' if unc else ''}{_encode_path(absolute)}"


@dataclass(frozen=True)
class FileAddress:
    """Parsed parts of a ``dsh-resource://file/…`` address."""
    scope: str
    path: str
    session_id: Optional[str] = None


def parse_file_address(address: str) -> Optional[FileAddress]:
    """Read an address back into parts; ``None`` when not a known-scope file URI."""
    if not isinstance(address, str) or not address.startswith(FILE_ADDRESS_PREFIX):
        return None
    try:
        cut = re.search(r"[?#]", address)
        body = address[len(FILE_ADDRESS_PREFIX):cut.start() if cut else None]
        parts = body.split("/")
        scope, rest = parts[0], parts[1:]
        if scope == "session":
            if not rest or rest[0] == "" or len(rest) < 2:
                return None
            session_id = _decode_segment(rest[0])
            path = "/".join(_decode_segment(segment) for segment in rest[1:])
            return FileAddress(scope="session", path=path, session_id=session_id)
        if scope == "absolute":
            unc = bool(rest) and rest[0] == "" and len(rest) > 1
            raw = rest[1:] if unc else rest
            segments = [_decode_segment(segment) for segment in raw]
            if not segments or segments[0] == "":
                return None
            if unc:
                return FileAddress(scope="absolute", path="//" + "/".join(segments))
            joined = "/".join(segments)
            return FileAddress(scope="absolute",
                               path=joined if is_drive_segment(segments[0]) else "/" + joined)
        return None
    except (ValueError, UnicodeDecodeError):
        return None


def is_windows_style_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[/\\]", value)) or value.startswith("\\\\")


def is_absolute_workspace_path(path: str) -> bool:
    """Absolute in either accepted spelling: POSIX, Windows drive, or UNC."""
    return path.startswith("/") or is_windows_style_path(path)


def resolve_workspace_path(cwd: Optional[str], path: str) -> str:
    """Workspace-relative path in Host-facing spelling; absolute paths pass through."""
    if is_absolute_workspace_path(path):
        return path
    if cwd is None or cwd == "":
        return path
    separator = "\\" if is_windows_style_path(cwd) and "\\" in cwd else "/"
    root = re.sub(r"[/\\]+$", "", cwd)
    tail = re.sub(r"^[/\\]+", "", path)
    return f"{root}{separator}{tail}"


def abbreviate_home_path(path: str, home: Optional[str]) -> str:
    """``~``/``~/…`` for a POSIX home and its descendants; otherwise verbatim."""
    if home is None or home == "":
        return path
    if is_windows_style_path(path) or is_windows_style_path(home):
        return path
    root = re.sub(r"/+$", "", home)
    if root in ("", "/"):
        return path
    if re.sub(r"/+$", "", path) == root:
        return "~"
    if path.startswith(f"{root}/"):
        return "~" + path[len(root):]
    return path


def workspace_title_of(path: str) -> str:
    """Final non-empty segment for display; '' for a separator-only path."""
    trimmed = re.sub(r"[/\\]+$", "", path)
    separator = max(trimmed.rfind("/"), trimmed.rfind("\\"))
    return trimmed[separator + 1:]


@dataclass(frozen=True)
class PathParts:
    directory: str
    name: str


def path_parts_of(path: str) -> PathParts:
    """Split for display: directories through the last separator, then the name."""
    trimmed = re.sub(r"[/\\]+$", "", path)
    if trimmed == "":
        return PathParts(directory="", name=path)
    cut = max(trimmed.rfind("/"), trimmed.rfind("\\")) + 1
    return PathParts(directory=trimmed[:cut], name=trimmed[cut:])


def file_address_for(session_id: str, cwd: Optional[str], path: str) -> str:
    """Address as a caller holds it: in-workspace absolutes become session-relative."""
    normalized = path.replace("\\", "/")
    if not is_absolute_workspace_path(normalized):
        return session_file_address(session_id, normalized)
    root = "" if cwd is None else re.sub(r"/+$", "", cwd.replace("\\", "/"))
    if root != "" and normalized == root:
        return session_file_address(session_id, "")
    if root != "" and normalized.startswith(f"{root}/"):
        return session_file_address(session_id, normalized[len(root) + 1:])
    return session_file_address(session_id, normalized)


def relativize_to_cwd(text: str, cwd: Optional[str]) -> str:
    """Strip the workspace root for display; unchanged when not rooted there."""
    if cwd is None or cwd == "":
        return text
    root = re.sub(r"[/\\]+$", "", cwd)
    if text.startswith(f"{root}/") or text.startswith(f"{root}\\"):
        return text[len(root) + 1:]
    return text


def file_media_url(base: str, path: str) -> Optional[str]:
    """File URL through the authenticated route; None for unsupported input."""
    if not re.match(r"^https?:", base) and not base.startswith("dsh-app://app/"):
        return None
    if not is_absolute_workspace_path(path):
        return None
    if re.match(r"^[/\\]{2}", path):
        return None
    if re.search(r"[\u0000-\u001f\u007f]", path):
        return None
    relative = f"api/file?path={quote(path, safe=_ENCODE_SAFE)}"
    if re.match(r"^https?:", base):
        return urljoin(base, relative)
    # WHATWG-style resolution for the opaque dsh-app base (urljoin refuses
    # non-hierarchical schemes): drop the last segment unless the base ends in '/'.
    root = base if base.endswith("/") else base[:base.rfind("/") + 1]
    return root + relative
