"""Host-native OS integrations: shell-free command runner, path openers, and
file-association catalogs.

Port of ``@deepseek-ai/dsh-native-command`` (commit 5badb15, lib/index.js 928
lines read in full — the bundle inlines every types/ module: runner,
path-opener, desktop-entry, file-applications-linux, file-applications-
windows, types, file-applications). Contract surface:

- ``run_native_command``: NO shell — argv is never a shell string; utf8
  capture; cancellation terminates the child; failures carry code + partial
  stdout/stderr.
- path openers with three intents (default / association / text-editor):
  browser-renderable documents (html/htm/xhtml/svg) prefer the default
  browser when the platform can NAME one (macOS LaunchServices https
  handler, Linux $BROWSER), else the default application; the text-editor
  intent never consults the browser; Windows hands every intent to Explorer
  (the shell's own default-application resolution — the one a double-click
  uses); WSL translates every path for the Windows desktop via wslpath.
- ``explorer_target``: Explorer parses its own command line and splits at
  commas and equals signs (escaped), and REJECTS percent-encoded non-ASCII
  in a file URI (opening Documents instead) while resolving literal
  characters — so non-ASCII escapes are decoded back; verbatim ``\\\\?\\``
  prefixes arrive as the ordinary drive/UNC URI.
- ``run_explorer``: Explorer exits 1 after handing off to the running
  desktop process — exit 1 means the shell took it; cancellation wins over
  a delegate's exit 1.
- ``can_open_native_path``: headless Linux answers false so surfaces show
  path text instead of a button that would spawn xdg-open into nothing.
- XDG desktop-entry parsing reads literal fields only (never interprets
  executable command text; action sections excluded); icon lookup follows
  hicolor size order → scalable → pixmaps.
- Linux associations come from GIO (content-type → mime handlers) with
  localized Name resolution; Windows associations run the C# COM adapter
  (SHAssocEnumHandlers; Invoke preserves packaged-app/DDE handling; the
  shared Shell reference — not an arbitrary delay — owns the asynchronous
  launch lifetime) inside a Unicode STA PowerShell process where **paths are
  base64-encoded data, never PowerShell expressions**; macOS uses JXA with
  AppKit, collapsing duplicate self-update copies for display (highest
  version wins, default wins outright) while launch validation keeps every
  registered copy.
- ``open_native_file_application`` rejects stale or arbitrary application
  identifiers against the complete registered list.

Python adaptations (documented):
- execFile + AbortSignal → subprocess (shell=False) + a threading.Event
  cancel token; abort → terminate/kill and ``NativeCommandError(code=
  "ABORT_ERR", cancelled=True)``; the error message text is native, the
  code/stdout/stderr contract is upstream's (spawn failure code "ENOENT",
  nonzero exit code = the exit status int, partial captures preserved).
- async → sync throughout; the ``internals`` seam (platform/run/env/
  os_release) is upstream's own deterministic-test seam, kept verbatim —
  Python's sys.platform values ("win32"/"linux"/"darwin") equal Node's
  process.platform values, so no mapping exists.
- pathToFileURL → PureWindowsPath.as_uri() (same UTF-8 percent escaping);
  the verbatim-prefix normalization Node's path resolution performs is done
  explicitly before encoding.
- fs/promises → os/pathlib builtins; mkdtemp + BOM-prefixed temp script +
  force-remove in finally, exactly as upstream.
"""
from __future__ import annotations

import base64
import os
import platform as _platform_mod
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "NativeCommandError", "run_native_command", "BROWSER_DOCUMENTS",
    "mac_bundle_for_https", "explorer_target", "can_open_native_path",
    "open_native_path", "open_native_associated_path", "open_native_text_file",
    "native_file_manager", "reveal_native_path", "desktop_entry_fields",
    "desktop_data_directories", "read_icon", "desktop_application_icon",
    "desktop_file", "linux_file_applications", "windows_file_applications",
    "parse_native_file_applications", "parse_mac_applications",
    "dedupe_mac_applications", "compare_versions", "native_file_applications",
    "open_native_file_application", "desktop_target",
]

Cancel = Optional[threading.Event]

# A host refusing console-window creation fails the child with this status
# before any user code runs (see run_native_command's documented retry).
_STATUS_DLL_INIT_FAILED = 0xC0000142


class NativeCommandError(RuntimeError):
    """A native command failure carrying the upstream error contract:
    ``code`` (exit status int, "ENOENT"-style spawn failure, or "ABORT_ERR"
    for cancellation) plus the captured stdout/stderr (partial on abort)."""

    def __init__(self, message: str, *, code: Any = None, stdout: str = "",
                 stderr: str = "", cancelled: bool = False,
                 cause: Optional[BaseException] = None):
        super().__init__(message)
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.cancelled = cancelled
        if cause is not None:
            self.__cause__ = cause


def _throw_if_cancelled(cancel: Cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise NativeCommandError("native command was cancelled",
                                 code="ABORT_ERR", cancelled=True)


def run_native_command(command: str, args: Sequence[str], cancel: Cancel = None,
                       *, env: Optional[Dict[str, str]] = None,
                       cwd: Optional[str] = None) -> Dict[str, str]:
    """Run a host command with utf8 stdio, cancel propagation, Windows hide.

    Never a shell: argv elements arrive at CreateProcess/execve untouched.

    Capture medium is a pair of anonymous temporary files rather than OS
    pipes (documented deviation): identical capture contract — utf8 decode
    with U+FFFD replacement, partial output preserved on cancel — with no
    pipe-buffer deadlock surface, and it runs under hosts whose sandbox
    refuses child pipe creation while allowing inheritable file handles.

    ``windowsHide`` degrades once (documented deviation): a host that
    refuses console-window creation fails the child with STATUS_DLL_INIT_
    FAILED (0xC0000142) BEFORE any user code runs — the identical argv is
    retried once without the hide flag, so hiding stays cosmetic and the
    command semantics never change.
    """
    argv = [command, *args]
    hide_window = sys.platform == "win32"
    attempt = 0
    while True:
        attempt += 1
        kwargs: Dict[str, Any] = {"env": env, "cwd": cwd}
        if hide_window:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = tempfile.TemporaryFile()
        err = tempfile.TemporaryFile()
        try:
            try:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=err, **kwargs)
            except FileNotFoundError as error:
                raise NativeCommandError(str(error), code="ENOENT", cause=error) from error
            except OSError as error:
                raise NativeCommandError(str(error), code=getattr(error, "winerror", None),
                                         cause=error) from error
            cancelled = False
            while True:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    from AstralNexus.runtime.process_control import stop_process_tree
                    stop_process_tree(process, grace_seconds=5)
                    break
                try:
                    process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if (hide_window and not cancelled and attempt == 1
                    and process.returncode == _STATUS_DLL_INIT_FAILED):
                hide_window = False
                continue
            out.seek(0)
            err.seek(0)
            stdout = out.read().decode("utf-8", errors="replace")
            stderr = err.read().decode("utf-8", errors="replace")
            if cancelled:
                raise NativeCommandError("native command was cancelled",
                                         code="ABORT_ERR", cancelled=True,
                                         stdout=stdout, stderr=stderr)
            if process.returncode != 0:
                raise NativeCommandError(
                    f"command {command!r} exited with code {process.returncode}",
                    code=process.returncode, stdout=stdout, stderr=stderr)
            return {"stdout": stdout, "stderr": stderr}
        finally:
            out.close()
            err.close()


# --------------------------------------------------------------- path-opener

BROWSER_DOCUMENTS = frozenset({".html", ".htm", ".xhtml", ".svg"})

_LS_PREFERRED_VERSIONS = re.compile(r"LSHandlerPreferredVersions\s*=\s*\{[^}]*\};")
_LS_HTTPS_BLOCK = re.compile(r"\{[^{}]*LSHandlerURLScheme\s*=\s*\"?https\"?;[^{}]*\}")
_LS_ROLE_ALL = re.compile(r"LSHandlerRoleAll\s*=\s*\"?([\w.-]+)\"?;")


def mac_bundle_for_https(plist: str) -> Optional[str]:
    """The macOS bundle registered for https — the default browser as
    LaunchServices records it. The nested version dict is stripped first
    because it carries its own LSHandlerRoleAll."""
    stripped = _LS_PREFERRED_VERSIONS.sub("", plist)
    match = _LS_HTTPS_BLOCK.search(stripped)
    if match is None:
        return None
    role = _LS_ROLE_ALL.search(match.group(0))
    return role.group(1) if role is not None else None


def _open_in_browser(path: str, cancel: Cancel, platform: str,
                     run: Callable[..., Any], env: Mapping[str, str]) -> bool:
    """Open one browser-renderable document with the default browser. True
    when a browser took it; false when the platform cannot name one (the
    caller then uses the default application)."""
    if platform == "darwin":
        try:
            result = run("defaults",
                         ["read", "com.apple.LaunchServices/com.apple.launchservices.secure"],
                         cancel)
            bundle = mac_bundle_for_https(result["stdout"])
        except NativeCommandError:
            return False
        if bundle is None:
            return False
        run("open", ["-b", bundle, path], cancel)
        return True
    if platform == "linux":
        browser = env.get("BROWSER")
        if browser is None or browser == "":
            return False
        run(browser, [path], cancel)
        return True
    return False


def _present(value: Optional[str]) -> bool:
    return value is not None and value != ""


def _env_of(internals: Dict[str, Any]) -> Mapping[str, str]:
    env = internals.get("env")
    return env if env is not None else os.environ


def _is_wsl(internals: Dict[str, Any]) -> bool:
    env = _env_of(internals)
    if _present(env.get("WSL_DISTRO_NAME")) or _present(env.get("WSL_INTEROP")):
        return True
    os_release = internals.get("os_release")
    if os_release is None:
        os_release = _platform_mod.release()
    return "microsoft" in os_release.lower()


_NON_ASCII_ESCAPES = re.compile(r"(?:%[89A-F][0-9A-F])+", re.IGNORECASE)


def explorer_target(windows_path: str) -> str:
    """Encode one Windows path as the target Explorer can receive intact.

    Explorer splits its own command line at commas and equals signs (both
    escaped); it rejects percent-encoded non-ASCII in a file URI and opens
    Documents instead, while it resolves literal characters — so non-ASCII
    escapes are decoded back and ASCII escapes stand. Verbatim ``\\\\?\\`` /
    ``\\\\?\\UNC\\`` prefixes arrive as the ordinary drive or UNC URI.
    """
    path = windows_path
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\") or path.startswith("\\\\.\\"):
        path = path[4:]
    uri = PureWindowsPath(path).as_uri()
    decoded = _NON_ASCII_ESCAPES.sub(
        lambda match: _decode_uri_component(match.group(0)), uri)
    return decoded.replace(",", "%2C").replace("=", "%3D")


def _decode_uri_component(segment: str) -> str:
    """decodeURIComponent over one run of percent escapes (invalid UTF-8
    becomes U+FFFD, matching JS)."""
    from urllib.parse import unquote
    return unquote(segment, encoding="utf-8", errors="replace")


def _run_explorer(args: Sequence[str], cancel: Cancel,
                  run: Callable[..., Any]) -> None:
    """Hand one target to Explorer, accepting its delegated-handoff exit 1.
    Cancellation wins over a delegate's exit 1."""
    try:
        run("explorer.exe", list(args), cancel)
    except NativeCommandError as error:
        _throw_if_cancelled(cancel)
        if error.code != 1:
            raise


def _open_windows_path(path: str, cancel: Cancel, run: Callable[..., Any]) -> None:
    _run_explorer([explorer_target(path)], cancel, run)


def _open_wsl_path(path: str, cancel: Cancel, run: Callable[..., Any]) -> None:
    translated = run("wslpath", ["-w", path], cancel)
    _throw_if_cancelled(cancel)
    windows_path = re.sub(r"[\r\n]+$", "", translated["stdout"])
    if windows_path == "":
        raise RuntimeError("wslpath returned no Windows path")
    _open_windows_path(windows_path, cancel, run)


def _open_native_path_with_intent(path: str, cancel: Cancel, intent: str,
                                  internals: Optional[Dict[str, Any]] = None) -> None:
    """Dispatch one shell-free platform command for the requested open intent."""
    internals = internals or {}
    platform = internals.get("platform") or sys.platform
    run = internals.get("run") or run_native_command
    env = _env_of(internals)
    wsl = platform == "linux" and _is_wsl(internals)
    if (not wsl and intent == "default"
            and Path(path).suffix.lower() in BROWSER_DOCUMENTS
            and _open_in_browser(path, cancel, platform, run, env)):
        return
    if platform == "darwin":
        run("open", ["-t", path] if intent == "text-editor" else [path], cancel)
        return
    if platform == "win32":
        _open_windows_path(path, cancel, run)
        return
    if platform == "linux":
        if wsl:
            _open_wsl_path(path, cancel, run)
            return
        run("xdg-open", [path], cancel)
        return
    raise RuntimeError(f"native path opener is unsupported on {platform}")


def can_open_native_path(internals: Optional[Dict[str, Any]] = None) -> bool:
    """Whether opening a path plausibly reaches a desktop on this host. A
    headless or containerised Linux host answers false — what lets a surface
    show a path as text instead of a button spawning xdg-open into nothing."""
    internals = internals or {}
    platform = internals.get("platform") or sys.platform
    if platform in ("darwin", "win32"):
        return True
    if platform != "linux":
        return False
    env = _env_of(internals)
    return (_is_wsl(internals) or _present(env.get("DISPLAY"))
            or _present(env.get("WAYLAND_DISPLAY")))


def open_native_path(path: str, cancel: Cancel = None,
                     internals: Optional[Dict[str, Any]] = None) -> None:
    """Open with the default application — or the default browser for
    documents a browser renders."""
    _open_native_path_with_intent(path, cancel, "default", internals)


def open_native_associated_path(path: str, cancel: Cancel = None,
                                internals: Optional[Dict[str, Any]] = None) -> None:
    """Open through the file-type association, including HTML and SVG."""
    _open_native_path_with_intent(path, cancel, "association", internals)


def open_native_text_file(path: str, cancel: Cancel = None,
                          internals: Optional[Dict[str, Any]] = None) -> None:
    """Open a text document for editing; macOS bypasses the association so a
    YAML association with a browser cannot consume the gesture."""
    _open_native_path_with_intent(path, cancel, "text-editor", internals)


def native_file_manager(internals: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The supported file-manager action, or None on unsupported platforms —
    without inspecting the browser's platform."""
    internals = internals or {}
    platform = internals.get("platform") or sys.platform
    if platform == "darwin":
        return "finder"
    if platform == "win32" or (platform == "linux" and _is_wsl(internals)):
        return "explorer"
    if platform == "linux":
        return "directory"
    return None


def reveal_native_path(path: str, cancel: Cancel = None,
                       internals: Optional[Dict[str, Any]] = None) -> None:
    """Reveal a file in Finder/Explorer, or open its parent in the Linux
    default file manager. Explorer exit 1 is a delegated handoff, not proof
    of selection."""
    _throw_if_cancelled(cancel)
    internals = internals or {}
    platform = internals.get("platform") or sys.platform
    run = internals.get("run") or run_native_command
    manager = native_file_manager({**internals, "platform": platform})
    if manager == "finder":
        run("open", ["-R", path], cancel)
        return
    if manager == "explorer":
        windows_path = path
        if platform == "linux":
            translated = run("wslpath", ["-w", path], cancel)
            _throw_if_cancelled(cancel)
            windows_path = re.sub(r"[\r\n]+$", "", translated["stdout"])
            if windows_path == "":
                raise RuntimeError("wslpath returned no Windows path")
        _run_explorer(["/select,", explorer_target(windows_path)], cancel, run)
        return
    if manager == "directory":
        run("xdg-open", [os.path.dirname(path)], cancel)
        return
    raise RuntimeError(f"native file manager is unsupported on {platform}")


# -------------------------------------------------------------- desktop-entry


def desktop_entry_fields(text: str) -> Dict[str, str]:
    """Read the main desktop-entry section without interpreting executable
    commands; action sections are excluded."""
    main = False
    fields: Dict[str, str] = {}
    for line in re.split(r"\r?\n", text):
        trimmed = line.strip()
        if trimmed.startswith("["):
            main = trimmed == "[Desktop Entry]"
            continue
        if not main or trimmed.startswith("#"):
            continue
        separator = trimmed.find("=")
        if separator > 0:
            fields[trimmed[:separator].strip()] = trimmed[separator + 1:].strip()
    return fields


def desktop_data_directories(home: str, env: Mapping[str, str]) -> List[str]:
    """XDG application and icon roots in desktop precedence order."""
    data_home = env.get("XDG_DATA_HOME")
    if data_home is None:
        data_home = str(Path(home) / ".local" / "share")
    data_dirs = env.get("XDG_DATA_DIRS")
    if data_dirs is None:
        data_dirs = "/usr/local/share:/usr/share"
    return [data_home, *[entry for entry in data_dirs.split(":") if entry]]


def read_icon(path: str) -> Optional[Dict[str, Any]]:
    """Read an installed PNG or SVG icon; absent paths and directories have
    no pixels."""
    if path.endswith(".png"):
        content_type = "image/png"
    elif path.endswith(".svg"):
        content_type = "image/svg+xml"
    else:
        return None
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            return {"bytes": handle.read(), "contentType": content_type}
    except OSError:
        return None


_ICON_SIZES = ("512x512", "256x256", "128x128", "64x64", "48x48", "32x32")


def desktop_application_icon(name: str, directories: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Resolve an absolute icon path or an installed hicolor/pixmaps icon name."""
    if os.path.isabs(name):
        return read_icon(name)
    for directory in directories:
        for size in _ICON_SIZES:
            for extension in ("png", "svg"):
                icon = read_icon(str(Path(directory) / "icons" / "hicolor" / size
                                     / "apps" / f"{name}.{extension}"))
                if icon is not None:
                    return icon
        scalable = read_icon(str(Path(directory) / "icons" / "hicolor" / "scalable"
                                 / "apps" / f"{name}.svg"))
        if scalable is not None:
            return scalable
        for extension in ("png", "svg"):
            icon = read_icon(str(Path(directory) / "pixmaps" / f"{name}.{extension}"))
            if icon is not None:
                return icon
    return None


# ------------------------------------------------- file-applications (Linux)


def desktop_file(root: str, desktop_id: str, directory: Optional[str] = None) -> Optional[str]:
    """Find a desktop id, including ids derived from nested application
    directories (relative path separators read as dashes)."""
    directory = directory if directory is not None else root
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            candidate = os.path.relpath(entry.path, root).replace(os.sep, "-")
            if candidate == desktop_id:
                return entry.path
    for entry in entries:
        if entry.is_dir():
            found = desktop_file(root, desktop_id, entry.path)
            if found is not None:
                return found
    return None


def linux_file_applications(path: str, cancel: Cancel,
                            run: Callable[..., Any], env: Mapping[str, str]) -> List[Dict[str, Any]]:
    """Query all registered file handlers from GIO without executing
    desktop-entry command text; the desktop's default is marked."""
    info = run("gio", ["info", "-a", "standard::content-type", path], cancel)
    match = re.search(r"standard::content-type:\s*(\S+)", info["stdout"])
    if match is None:
        raise RuntimeError("GIO did not identify the file content type")
    mime = match.group(1)
    result = run("env", ["LC_ALL=C", "gio", "mime", mime], cancel)
    stdout = result["stdout"]
    preferred_match = re.search(r"^Default application.*:\s*(.+\.desktop)\s*$",
                                stdout, re.MULTILINE)
    preferred = preferred_match.group(1) if preferred_match is not None else None
    ids: List[str] = []
    for desktop_id in ([preferred] if preferred is not None else []) + [
            line.strip() for line in re.split(r"\r?\n", stdout)
            if re.match(r"^\s+.*\.desktop\s*$", line)]:
        if desktop_id not in ids:
            ids.append(desktop_id)
    home = env.get("HOME")
    if home is None:
        home = os.path.expanduser("~")
    directories = desktop_data_directories(home, env)
    raw_locale = env.get("LC_ALL")
    if raw_locale is None:
        raw_locale = env.get("LC_MESSAGES")
    if raw_locale is None:
        raw_locale = env.get("LANG")
    if raw_locale is None:
        raw_locale = ""
    locale = re.sub(r"\..*$", "", raw_locale)
    applications: List[Dict[str, Any]] = []
    for desktop_id in ids:
        _throw_if_cancelled(cancel)
        entry_path: Optional[str] = None
        for directory in directories:
            entry_path = desktop_file(str(Path(directory) / "applications"), desktop_id)
            if entry_path is not None:
                break
        if entry_path is None:
            continue
        with open(entry_path, encoding="utf-8") as handle:
            fields = desktop_entry_fields(handle.read())
        name = fields.get(f"Name[{locale}]")
        if name is None:
            name = fields.get(f"Name[{re.sub(r'_.*', '', locale)}]")
        if name is None:
            name = fields.get("Name")
        if name is None or fields.get("Hidden") == "true":
            continue
        icon: Optional[Dict[str, Any]] = None
        if fields.get("Icon") is not None:
            icon = desktop_application_icon(fields["Icon"], directories)
        applications.append({
            "id": entry_path,
            "name": name,
            "default": desktop_id == preferred,
            "icon": None if icon is None
            else "data:{};base64,{}".format(
                icon["contentType"], base64.b64encode(icon["bytes"]).decode("ascii")),
        })
    return applications


# ----------------------------------------------- file-applications (Windows)

WINDOWS_ASSOCIATIONS = r'''
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

public static class DshFileAssociations {
  [ComImport, Guid("973810ae-9599-4b88-9e4d-6ee98c9552da"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IEnumHandlers {
    [PreserveSig] int Next(uint count, out IHandler handler, out uint fetched);
  }
  [ComImport, Guid("f04061ac-1659-4a3f-a954-775aa57fc083"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IHandler {
    void GetName([MarshalAs(UnmanagedType.LPWStr)] out string name);
    void GetUIName([MarshalAs(UnmanagedType.LPWStr)] out string name);
    void GetIconLocation([MarshalAs(UnmanagedType.LPWStr)] out string path, out int index);
    [PreserveSig] int IsRecommended();
    void MakeDefault([MarshalAs(UnmanagedType.LPWStr)] string description);
    void Invoke(IDataObject data);
    void CreateInvoker(IDataObject data, [MarshalAs(UnmanagedType.Interface)] out object invoker);
  }
  [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IShellItem {
    void BindToHandler(IntPtr context, ref Guid handler, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IDataObject data);
  }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHAssocEnumHandlers(string extension, uint filter, out IEnumHandlers handlers);
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  static extern void SHCreateItemFromParsingName(string path, IntPtr context, ref Guid iid, out IShellItem item);
  [DllImport("shlwapi.dll", CharSet = CharSet.Unicode)]
  static extern int AssocQueryString(uint flags, uint kind, string association, string extra, StringBuilder output, ref uint size);
  [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
  static extern int SHDefExtractIcon(string path, int index, uint flags, out IntPtr large, out IntPtr small, uint size);
  [DllImport("user32.dll")]
  static extern bool DestroyIcon(IntPtr icon);
  [DllImport("shlwapi.dll", CharSet = CharSet.Unicode)]
  static extern int SHLoadIndirectString(string source, StringBuilder output, uint size, IntPtr reserved);

  [DllImport("shlwapi.dll", PreserveSig = false)]
  static extern void SHCreateThreadRef(IntPtr count, out IntPtr reference);
  [DllImport("shlwapi.dll", PreserveSig = false)]
  static extern void SHSetThreadRef(IntPtr reference);
  [DllImport("shell32.dll")]
  static extern void SHSetInstanceExplorer(IntPtr reference);
  [StructLayout(LayoutKind.Sequential)]
  struct Message {
    public IntPtr window;
    public uint message;
    public UIntPtr wParam;
    public IntPtr lParam;
    public uint time;
    public int x, y;
    public uint reserved;
  }
  [DllImport("user32.dll", SetLastError = true)]
  static extern UIntPtr SetTimer(IntPtr window, UIntPtr id, uint milliseconds, IntPtr callback);
  [DllImport("user32.dll")]
  static extern bool KillTimer(IntPtr window, UIntPtr id);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  static extern int GetMessage(out Message message, IntPtr window, uint min, uint max);
  [DllImport("user32.dll")]
  static extern bool TranslateMessage(ref Message message);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  static extern IntPtr DispatchMessage(ref Message message);

  // Shell handlers may return before their asynchronous launch work releases the host.
  // The shared Shell reference, rather than an arbitrary delay, owns that lifetime.
  static void WithShellLifetime(Action action) {
    IntPtr count = Marshal.AllocHGlobal(sizeof(int));
    IntPtr reference = IntPtr.Zero;
    try {
      Marshal.WriteInt32(count, 0);
      SHCreateThreadRef(count, out reference);
      SHSetThreadRef(reference);
      SHSetInstanceExplorer(reference);
      int owned = Marshal.ReadInt32(count);
      try { action(); }
      finally {
        if (Marshal.ReadInt32(count) > owned) {
          // USER_TIMER_MINIMUM wakes the STA to inspect the reference count while dispatching COM work.
          UIntPtr timer = SetTimer(IntPtr.Zero, UIntPtr.Zero, 10, IntPtr.Zero);
          if (timer == UIntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
          try {
            while (Marshal.ReadInt32(count) > owned) {
              Message message;
              if (GetMessage(out message, IntPtr.Zero, 0, 0) <= 0) throw new InvalidOperationException("Shell handoff message loop ended");
              TranslateMessage(ref message);
              DispatchMessage(ref message);
            }
          } finally { KillTimer(IntPtr.Zero, timer); }
        }
      }
    } finally {
      SHSetInstanceExplorer(IntPtr.Zero);
      SHSetThreadRef(IntPtr.Zero);
      int remaining = reference == IntPtr.Zero ? 0 : Marshal.Release(reference);
      // An interrupted drain leaves its counter owned by the terminating helper process.
      if (remaining == 0) Marshal.FreeHGlobal(count);
    }
  }

  public sealed class Application {
    public string id;
    public string name;
    public string icon;
    public bool @default;
  }
  static string Associated(string extension, uint kind) {
    uint size = 0;
    AssocQueryString(0, kind, extension, null, null, ref size);
    if (size == 0) return null;
    var text = new StringBuilder((int)size);
    return AssocQueryString(0, kind, extension, null, text, ref size) == 0 ? text.ToString() : null;
  }
  // Resolve a packaged application's indirect icon reference, for example
  // @{Microsoft.WindowsNotepad_...?ms-resource://.../NotepadAppList.png}, to
  // the resource file the Shell would draw; an unresolved reference is null.
  static string ResolveIndirectIcon(string source) {
    var output = new StringBuilder(1024);
    return SHLoadIndirectString(source, output, (uint)output.Capacity, IntPtr.Zero) == 0 ? output.ToString() : null;
  }
  // Read one icon source into a 32px PNG: an image file directly, otherwise the
  // resource the Shell extracts at the given index. A missing resource is null.
  static string PngFromIconSource(string source, int index) {
    IntPtr large = IntPtr.Zero, small = IntPtr.Zero;
    try {
      if (Path.GetExtension(source).Equals(".png", StringComparison.OrdinalIgnoreCase)) {
        using (var original = Image.FromFile(source))
        using (var resized = new Bitmap(original, new Size(32, 32)))
        using (var stream = new MemoryStream()) {
          resized.Save(stream, ImageFormat.Png);
          return "data:image/png;base64," + Convert.ToBase64String(stream.ToArray());
        }
      }
      if (SHDefExtractIcon(source, index, 0, out large, out small, 32) != 0 || large == IntPtr.Zero) return null;
      using (var image = Icon.FromHandle(large))
      using (var bitmap = image.ToBitmap())
      using (var stream = new MemoryStream()) {
        bitmap.Save(stream, ImageFormat.Png);
        return "data:image/png;base64," + Convert.ToBase64String(stream.ToArray());
      }
    } catch (Exception) {
      // Missing icon resources do not make the application unusable.
      return null;
    } finally {
      if (large != IntPtr.Zero) DestroyIcon(large);
      if (small != IntPtr.Zero) DestroyIcon(small);
    }
  }
  // Best-effort fallback: some handlers return their executable path from GetName, but a
  // packaged handler's name may be an AUMID or family name, which extracts no icon.
  static string IconData(IHandler handler, string id) {
    string source = null; int index = 0;
    try { handler.GetIconLocation(out source, out index); } catch (Exception) {
      // A handler without a drawable icon location falls back to its name.
    }
    if (!String.IsNullOrEmpty(source)) {
      source = Environment.ExpandEnvironmentVariables(source);
      if (source.StartsWith("@{", StringComparison.Ordinal) && source.EndsWith("}", StringComparison.Ordinal)) {
        var resolved = ResolveIndirectIcon(source);
        if (!String.IsNullOrEmpty(resolved)) source = resolved;
      }
      var icon = PngFromIconSource(source, index);
      if (icon != null) return icon;
    }
    return String.IsNullOrEmpty(id) || String.Equals(id, source, StringComparison.OrdinalIgnoreCase)
      ? null
      : PngFromIconSource(id, 0);
  }
  static void Visit(string path, Action<IHandler> visit) {
    var extension = Path.GetExtension(path);
    if (extension.Length == 0) return;
    IEnumHandlers handlers;
    SHAssocEnumHandlers(extension, 0, out handlers);
    try {
      while (true) {
        IHandler handler; uint fetched;
        int result = handlers.Next(1, out handler, out fetched);
        Marshal.ThrowExceptionForHR(result);
        if (result != 0 || fetched == 0) break;
        try { visit(handler); } finally { Marshal.FinalReleaseComObject(handler); }
      }
    } finally { Marshal.FinalReleaseComObject(handlers); }
  }
  public static Application[] List(string path) {
    var extension = Path.GetExtension(path);
    var executable = extension.Length == 0 ? null : Associated(extension, 2);
    var appId = extension.Length == 0 ? null : Associated(extension, 21);
    var apps = new List<Application>();
    var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    Visit(path, delegate(IHandler handler) {
      string id, name;
      // A corrupt registered handler (real host: one .txt handler on the
      // migration machine throws E_FAIL from GetName) must not poison the
      // whole catalog — upstream propagates and loses every entry.
      try { handler.GetName(out id); handler.GetUIName(out name); }
      catch (Exception) { return; }
      if (!seen.Add(id)) return;
      apps.Add(new Application { id = id, name = name, icon = IconData(handler, id),
        @default = String.Equals(id, executable, StringComparison.OrdinalIgnoreCase) || String.Equals(id, appId, StringComparison.OrdinalIgnoreCase) });
    });
    return apps.ToArray();
  }
  public static void Open(string path, string application) {
    WithShellLifetime(delegate { OpenRegistered(path, application); });
  }
  static void OpenRegistered(string path, string application) {
    bool opened = false;
    Visit(path, delegate(IHandler handler) {
      string id;
      try { handler.GetName(out id); } catch (Exception) { return; }
      if (opened || !String.Equals(id, application, StringComparison.OrdinalIgnoreCase)) return;
      var iid = typeof(IShellItem).GUID;
      IShellItem item;
      SHCreateItemFromParsingName(path, IntPtr.Zero, ref iid, out item);
      IDataObject data = null;
      try {
        var bhid = new Guid("b8c0bd9f-ed24-455c-83e6-d5390c4fe8c4");
        var dataIid = typeof(IDataObject).GUID;
        item.BindToHandler(IntPtr.Zero, ref bhid, ref dataIid, out data);
        handler.Invoke(data);
        opened = true;
      } finally {
        if (data != null) Marshal.FinalReleaseComObject(data);
        Marshal.FinalReleaseComObject(item);
      }
    });
    if (!opened) throw new InvalidOperationException("Application is not registered for this file");
  }
}'''


def windows_file_applications(path: str, application: Optional[str],
                              cancel: Cancel, run: Callable[..., Any]) -> str:
    """Execute the Windows Shell adapter in a Unicode STA PowerShell process.

    The adapter source is a private temporary script file, removed once the
    call settles; the path and application arrive base64-encoded — paths are
    encoded data, never PowerShell expressions. Query mode (application is
    None) emits a JSON array.
    """
    path_b64 = base64.b64encode(path.encode("utf-8")).decode("ascii")
    application_b64 = base64.b64encode((application or "").encode("utf-8")).decode("ascii")
    tail = ("ConvertTo-Json -InputObject @([DshFileAssociations]::List($path)) "
            "-Depth 4 -Compress" if application is None
            else "[DshFileAssociations]::Open($path, $application)")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
        "Add-Type -ReferencedAssemblies System,System.Core,System.Drawing "
        "-TypeDefinition @'\n"
        f"{WINDOWS_ASSOCIATIONS}\n"
        "'@\n"
        f"$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{path_b64}'))\n"
        f"$application = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{application_b64}'))\n"
        f"{tail}\n")
    directory = tempfile.mkdtemp(prefix="dsh-native-command-")
    script_path = str(Path(directory) / "associations.ps1")
    try:
        with open(script_path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(script)
        return run("powershell.exe",
                   ["-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy",
                    "Bypass", "-File", script_path], cancel)["stdout"]
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -------------------------------------------------------------------- types

_ICON_URL = re.compile(r"^data:image/(?:png|svg\+xml);base64,[A-Za-z0-9+/=]+$")


def parse_native_file_applications(value: Any) -> List[Dict[str, Any]]:
    """Validate file-association metadata received from a native process or
    authenticated Host."""
    if not isinstance(value, list):
        raise ValueError("Invalid native application list")
    applications: List[Dict[str, Any]] = []
    for entry in value:
        if (not isinstance(entry, dict)
                or "id" not in entry or "name" not in entry
                or "default" not in entry or "icon" not in entry
                or not isinstance(entry["id"], str) or entry["id"] == ""
                or not isinstance(entry["name"], str)
                or not isinstance(entry["default"], bool)
                or not (entry["icon"] is None
                        or (isinstance(entry["icon"], str)
                            and _ICON_URL.match(entry["icon"]) is not None))):
            raise ValueError("Invalid native application entry")
        applications.append({"id": entry["id"], "name": entry["name"],
                             "default": entry["default"], "icon": entry["icon"]})
    return applications


def _mac_field(field: Any) -> Optional[str]:
    """Validate one optional string field of a macOS entry; missing fields,
    null, and empty strings read as None."""
    if field is None:
        return None
    if not isinstance(field, str):
        raise ValueError("Invalid native application entry")
    return field if field != "" else None


def parse_mac_applications(value: Any) -> List[Dict[str, Any]]:
    """Validate every entry of the decoded macOS query output, base fields
    and grouping metadata alike."""
    if not isinstance(value, list):
        raise ValueError("Invalid native application list")
    applications: List[Dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Invalid native application entry")
        bundle = _mac_field(entry.get("bundle"))
        version = _mac_field(entry.get("version"))
        base = parse_native_file_applications([entry])[0]
        applications.append({**base, "bundle": bundle, "version": version})
    return applications


def compare_versions(left: Optional[str], right: Optional[str]) -> int:
    """Order two dotted version strings numerically; non-numeric segments
    count as 0, and a missing version sorts lowest."""
    if left is None or right is None:
        if left == right:
            return 0
        return -1 if left is None else 1
    a = left.split(".")
    b = right.split(".")

    def number(segment: Optional[str]) -> int:
        if segment is None:
            return 0
        try:
            return int(segment)
        except ValueError:
            return 0

    for index in range(max(len(a), len(b))):
        difference = (number(a[index]) if index < len(a) else 0) \
            - (number(b[index]) if index < len(b) else 0)
        if difference != 0:
            return difference
    return 0


def dedupe_mac_applications(applications: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse duplicate registrations of the same application for display.

    Self-updating apps leave extra copies on disk and LaunchServices
    registers every one, so the raw handler list repeats the app. Copies
    sharing a bundle identifier and display name collapse to the system
    default, else the highest version, at the group's first position —
    matching Finder, which shows one entry per app and splits only deliberate
    side-by-side installs (distinct display names). Launch validation
    bypasses this collapse.
    """
    order: List[Dict[str, Any]] = []
    groups: Dict[str, int] = {}
    for app in applications:
        if app.get("bundle") is None:
            order.append(app)
            continue
        key = f"{app['bundle']}\u0000{app['name']}"
        index = groups.get(key)
        if index is None:
            groups[key] = len(order)
            order.append(app)
            continue
        held = order[index]
        if held.get("default"):
            continue
        if app.get("default") or compare_versions(app.get("version"),
                                                  held.get("version")) > 0:
            order[index] = app
    return [{"id": app["id"], "name": app["name"], "default": app["default"],
             "icon": app["icon"]} for app in order]


# --------------------------------------------------- file-applications (all)

MAC_APPLICATIONS = '''
ObjC.import('AppKit');
function run(argv) {
  var workspace = $.NSWorkspace.sharedWorkspace;
  var file = $.NSURL.fileURLWithPath(argv[0]);
  var preferred = workspace.URLForApplicationToOpenURL(file);
  var preferredPath = preferred.isNil() ? null : ObjC.unwrap(preferred.path);
  var urls = workspace.URLsForApplicationsToOpenURL(file);
  var apps = [];
  for (var i = 0; i < urls.count; i++) {
    var url = urls.objectAtIndex(i);
    var path = ObjC.unwrap(url.path);
    var image = null;
    if (argv[1] === 'icons') {
      var icon = workspace.iconForFile(path);
      var thumbnail = $.NSImage.alloc.initWithSize($.NSMakeSize(32, 32));
      thumbnail.lockFocus;
      icon.drawInRectFromRectOperationFraction($.NSMakeRect(0, 0, 32, 32), $.NSZeroRect, $.NSCompositingOperationSourceOver, 1);
      thumbnail.unlockFocus;
      var bitmap = $.NSBitmapImageRep.imageRepWithData(thumbnail.TIFFRepresentation);
      var png = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
      image = png.isNil() ? null : 'data:image/png;base64,' + ObjC.unwrap(png.base64EncodedStringWithOptions(0));
    }
    var bundle = $.NSBundle.bundleWithURL(url);
    var bundleId = bundle.isNil() || bundle.bundleIdentifier.isNil() ? null : ObjC.unwrap(bundle.bundleIdentifier);
    var version = bundle.isNil() ? null : bundle.objectForInfoDictionaryKey('CFBundleShortVersionString');
    apps.push({
      id: path,
      name: ObjC.unwrap($.NSFileManager.defaultManager.displayNameAtPath(path)),
      default: path === preferredPath,
      icon: image,
      bundle: bundleId,
      version: version === null || version.isNil() ? null : String(ObjC.unwrap(version))
    });
  }
  return JSON.stringify(apps);
}'''


def desktop_target(path: str, cancel: Cancel,
                   internals: Dict[str, Any]) -> Dict[str, str]:
    """Resolve the desktop that owns the file, including Windows
    applications reached from WSL."""
    _throw_if_cancelled(cancel)
    platform = internals.get("platform") or sys.platform
    if platform == "linux" and native_file_manager(internals) == "explorer":
        run = internals.get("run") or run_native_command
        translated = run("wslpath", ["-w", path], cancel)
        _throw_if_cancelled(cancel)
        windows_path = re.sub(r"[\r\n]+$", "", translated["stdout"])
        if windows_path == "":
            raise RuntimeError("wslpath returned no Windows path")
        return {"platform": "win32", "path": windows_path}
    return {"platform": platform, "path": path}


def _query_file_applications(path: str, cancel: Cancel,
                             internals: Dict[str, Any], display: bool) -> List[Dict[str, Any]]:
    """Query handler metadata; display queries render macOS icons and
    collapse duplicate copies, launch validation keeps every registered copy."""
    _throw_if_cancelled(cancel)
    target = desktop_target(path, cancel, internals)
    run = internals.get("run") or run_native_command
    if target["platform"] == "linux":
        env = _env_of(internals)
        return linux_file_applications(path, cancel, run, env)
    if target["platform"] == "darwin":
        result = run("osascript", ["-l", "JavaScript", "-e", MAC_APPLICATIONS,
                                   target["path"], "icons" if display else "handlers"],
                     cancel)
        import json
        applications = parse_mac_applications(json.loads(result["stdout"]))
        return dedupe_mac_applications(applications) if display else applications
    if target["platform"] != "win32":
        return []
    import json
    stdout = windows_file_applications(target["path"], None, cancel, run)
    return parse_native_file_applications(json.loads(stdout))


def native_file_applications(path: str, cancel: Cancel = None,
                             internals: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """List registered handlers in OS preference order, including the
    current default and application icons."""
    return _query_file_applications(path, cancel, internals or {}, True)


def open_native_file_application(path: str, application: str, cancel: Cancel = None,
                                 internals: Optional[Dict[str, Any]] = None) -> None:
    """Open a file in a currently registered handler; stale or arbitrary
    application identifiers are rejected against the COMPLETE registered
    list, so macOS copies collapsed out of the display list stay openable."""
    internals = internals or {}
    target = desktop_target(path, cancel, internals)
    run = internals.get("run") or run_native_command
    if target["platform"] == "win32":
        windows_file_applications(target["path"], application, cancel, run)
        return
    registered = _query_file_applications(path, cancel, internals, False)
    if not any(app["id"] == application for app in registered):
        raise RuntimeError("Application is not registered for this file")
    if target["platform"] == "linux":
        run("gio", ["launch", application, path], cancel)
    else:
        run("open", ["-a", application, path], cancel)
