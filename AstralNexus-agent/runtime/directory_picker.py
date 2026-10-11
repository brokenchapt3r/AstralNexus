"""The directory-picking seam: base contract, native/browse backends, and the
adaptive boot-time chooser.

Port of four upstream packages (commit 5badb15, all read in full):
- ``@deepseek-ai/dsh-host-directory-picker`` (base, 44 lines): how the
  web-GUI host lets an operator select a workspace directory. Backends
  differ in INTERACTION SHAPE, not just mechanism, so the service exposes a
  discriminated capability instead of one method set: a ``native`` backend
  opens one OS chooser on the host's display, while a ``browse`` backend
  serves listing/creation primitives for an in-app browser (and thereby
  works for remote clients no OS dialog can reach). Consumers switch on
  ``capability()["kind"]``; the documented default for an unknown kind is to
  HIDE the picking affordance rather than fail. One implementation per
  context (services single slot); the capability object must stay stable for
  the service lifetime.
- ``@deepseek-ai/dsh-host-directory-picker-native`` (303 lines): per-platform
  chooser dispatch — darwin osascript (user cancel = code 1 + "User
  canceled"/-128 → None), win32 real folder dialog (its own process), linux
  zenity → kdialog fallback (code 1 → cancel; both missing → a loud
  install-hint failure); anything else unsupported. Cancellation always
  propagates before a cancel-shaped exit code is read.
- ``@deepseek-ai/dsh-host-directory-picker-browse`` (234 lines): one-level
  listing and single-segment child creation over the host filesystem.
  Hidden entries are FLAGGED BUT RETURNED (policy belongs to the surface),
  symlinks are followed to directories (broken/cyclic silently skipped),
  scope is the whole filesystem with two hard rails: only fully qualified
  paths are served (drive-less rooted forms like ``\\foo`` resolve against
  the process's current drive — refused), and the name-ascending window is
  O(max_entries) regardless of directory size (boundedInsert eviction).
  ``truncated`` tells the surface whether to offer a narrowing input.
- ``@deepseek-ai/dsh-host-directory-picker-auto`` (152 lines): boot-time
  resolution — ``native`` requires EVERY signal that the operator can see
  the host display and the backend can serve it (loopback-only bind, no SSH
  launch, servable display session: assumed on darwin/win32, requiring
  DISPLAY/WAYLAND_DISPLAY plus a zenity/kdialog binary on linux); anything
  ambiguous resolves to ``browse``, which works everywhere.

Python adaptations (documented):
- cordis Service/ctx → plain ABC + the host services single slot
  (``directory_picker``); duplicate registration is the slot's nature.
- win32 dialog: upstream drives a Node worker child speaking a closed
  showing/done/error message union with a koffi WM_CLOSE abort lever against
  the thread blocked in Show. Natively the dialog runs in an STA PowerShell
  child (System.Windows.Forms.FolderBrowserDialog), and the abort lever is
  terminating that child: the dialog dies with its process, the pick settles
  cancelled. The graceful WM_CLOSE retry ladder (150 ms × 20) exists to
  unwind a blocked JS worker; a killed process needs no unwind. The upstream
  worker synthesizes an Alt keypress for foreground activation; the .NET
  dialog owns its activation and needs none. The dialog GENERATION follows
  the PowerShell host's .NET: Windows PowerShell 5.1 (.NET Framework) shows
  the classic SHBrowseForFolder tree, .NET Core hosts show the modern
  IFileOpenDialog — both return the selected absolute path, so the seam's
  contract is generation-agnostic (UseDescriptionAsTitle, a .NET Core
  title nicety, is absent on Framework and deliberately not set).
- raceAbort → cancellation checkpoints between synchronous steps (fs reads
  are not retractable in Python either; late results are simply discarded).
- localeCompare → Python code-point ordering (documented approximation of
  the locale collation; stable and total like upstream's).
- z.object({maxEntries}) → validate_browse_config with snake_case
  ``max_entries`` (natural, ≥ 1, default 1000 — GitHub's Web UI cutoff).
- auto's Loader-entry mounting (backend package + client-ui surface package
  as two entries) → constructing the resolved backend into the host services
  slot plus HTTP routes; the client-ui surface packages belong to the B6
  client program and are recorded as a subset deferral — the native web
  frontend consumes the routes instead.
"""
from __future__ import annotations

import os
import posixpath
import ntpath
import re
import subprocess
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from AstralNexus.runtime import native_command as nc

__all__ = [
    "DirectoryPickerError", "DirectoryPicker", "NativeDirectoryPicker",
    "pick_native_directory", "pick_win32_directory", "BrowseDirectoryPicker",
    "validate_browse_config", "ancestry_crumbs", "fully_qualified",
    "bounded_insert", "directory_row", "can_execute", "has_linux_chooser_binary",
    "resolve_directory_picker_backend", "BACKEND_PACKAGES", "SURFACE_PACKAGES",
    "mount_directory_picker", "DIALOG_TITLE",
]

Cancel = nc.Cancel


# ------------------------------------------------------------------- base


class DirectoryPickerError(RuntimeError):
    """Typed failure thrown by browse primitives so consumers can map
    business codes without string matching."""

    def __init__(self, code: str, path: str, message: str):
        super().__init__(message)
        self.code = code
        self.path = path
        self.name = "DirectoryPickerError"


class DirectoryPicker(ABC):
    """Abstract directory-picking service. Subclass, implement capability(),
    and register as the host's ``directory_picker`` service (one
    implementation per context). The capability object must be stable for
    the service lifetime: consumers may capture it across calls."""

    SERVICE_KEY = "directory_picker"

    @abstractmethod
    def capability(self) -> Dict[str, Any]:
        """The interaction capability: a stable discriminated object."""
        raise NotImplementedError


def _throw_cancelled(cancel: Cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise RuntimeError("directory picker operation was cancelled")


# ------------------------------------------------------------------ native

DIALOG_TITLE = "Select Workspace Directory"

_WIN32_DIALOG_SCRIPT = """
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select Workspace Directory'
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.Write($dialog.SelectedPath)
}
"""


def _run_dialog_powershell(cancel: Cancel) -> Dict[str, str]:
    """The real win32 dialog half: one STA PowerShell child showing
    FolderBrowserDialog (.NET's IFileOpenDialog wrapper). Cancel terminates
    the child; the dialog dies with its process."""
    return nc.run_native_command(
        "powershell.exe",
        ["-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
         "-Command", _WIN32_DIALOG_SCRIPT],
        cancel)


def pick_win32_directory(cancel: Cancel = None,
                         internals: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Prompt the operator with a real Win32 folder dialog in its own
    process. Returns the chosen absolute path, or None if cancelled at the
    dialog; cancellation of the token settles as an abort."""
    internals = internals or {}
    runner = internals.get("dialog_runner") or _run_dialog_powershell
    nc._throw_if_cancelled(cancel)
    try:
        result = runner(cancel)
    except nc.NativeCommandError as error:
        if error.cancelled:
            raise RuntimeError("native directory picker aborted") from error
        raise RuntimeError(
            f"win32 folder dialog failed: {error}") from error
    selected = re.sub(r"[\r\n]+$", "", result["stdout"])
    return selected if selected != "" else None


def _output_path(stdout: str) -> Optional[str]:
    """The chooser's reported path, or null for a cancel-shaped exit."""
    trimmed = re.sub(r"[\r\n]+$", "", stdout)
    return trimmed if trimmed != "" else None


def _rethrow_if_cancelled(cancel: Cancel) -> None:
    nc._throw_if_cancelled(cancel)


def pick_native_directory(cancel: Cancel = None,
                          internals: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Prompt the operator with the platform's native folder chooser and
    resolve to the selected absolute path, or None if the operator cancels.

    A cancelled signal rejects even if the platform chooser also exits
    cancel-shaped, so callers never confuse a token abort with the
    operator's decision. Only darwin, linux, and win32 are supported.
    """
    internals = internals or {}
    platform = internals.get("platform") or sys.platform
    run = internals.get("run") or nc.run_native_command
    if platform == "darwin":
        try:
            result = run("osascript", [
                "-e", 'set selectedFolder to choose folder with prompt '
                      '"Select Workspace Directory"',
                "-e", "POSIX path of selectedFolder",
            ], cancel)
            return _output_path(result["stdout"])
        except nc.NativeCommandError as error:
            _rethrow_if_cancelled(cancel)
            stderr = error.stderr or ""
            if (error.code == 1
                    and re.search(r"(?:User canceled|-128)", stderr, re.IGNORECASE)):
                return None
            raise
    if platform == "win32":
        return pick_win32_directory(cancel, internals)
    if platform == "linux":
        try:
            result = run("zenity", ["--file-selection", "--directory",
                                    f"--title={DIALOG_TITLE}"], cancel)
            return _output_path(result["stdout"])
        except nc.NativeCommandError as error:
            _rethrow_if_cancelled(cancel)
            if error.code == 1:
                return None
            if error.code != "ENOENT":
                raise
        try:
            result = run("kdialog", ["--getexistingdirectory", ".",
                                     "--title", DIALOG_TITLE], cancel)
            return _output_path(result["stdout"])
        except nc.NativeCommandError as error:
            _rethrow_if_cancelled(cancel)
            if error.code == 1:
                return None
            if error.code == "ENOENT":
                raise RuntimeError(
                    "no supported native directory picker found "
                    "(install zenity or kdialog)") from error
            raise
    raise RuntimeError(f"native directory picker is unsupported on {platform}")


class NativeDirectoryPicker(DirectoryPicker):
    """The ctx.directoryPicker native implementation (stable capability
    object per service life).

    One interaction shape: pick() opens a single blocking OS-native folder
    chooser on the host's own display (macOS sheet, Windows dialog, Linux
    zenity/kdialog) and resolves to the chosen absolute POSIX path, or None
    if the operator cancels. The chooser activates as foreground on its own.
    Only viable when the operator sits at the host's screen; remote
    deployments compose the browse backend instead.
    """

    def __init__(self, internals: Optional[Dict[str, Any]] = None):
        self._internals = internals
        self._capability: Dict[str, Any] = {
            "kind": "native",
            "pick": lambda cancel=None: pick_native_directory(cancel, internals),
        }

    def capability(self) -> Dict[str, Any]:
        return self._capability


# ------------------------------------------------------------------ browse


def ancestry_crumbs(target: str) -> List[Dict[str, Any]]:
    """Ancestor chain from the filesystem root to target inclusive — the
    breadcrumb rows of a listing, every one a jump target."""
    crumbs: List[Dict[str, Any]] = []
    current = target
    while True:
        parent = os.path.dirname(current)
        crumbs.insert(0, {
            "name": current if parent == current else os.path.basename(current),
            "path": current,
            "hidden": False,
        })
        if parent == current:
            return crumbs
        current = parent


_QUALIFIED_WIN32 = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2}[^\\/]+[\\/]+[^\\/]+)")


def fully_qualified(path: str, platform: Optional[str] = None) -> bool:
    """True when the path names one fixed filesystem location regardless of
    process state: POSIX-absolute on POSIX; on Windows only drive-qualified
    (``C:\\…``) or complete UNC (``\\\\server\\share…``) forms. Rooted
    drive-less forms (``\\foo``, ``/foo``) and incomplete UNC prefixes
    (``\\\\``, ``\\\\server``) pass isAbsolute yet still resolve against the
    process's current drive."""
    platform = platform or sys.platform
    if platform == "win32":
        return ntpath.isabs(path) and _QUALIFIED_WIN32.match(path) is not None
    return posixpath.isabs(path)


def bounded_insert(window: List[Dict[str, Any]], candidate: Dict[str, Any],
                   keep: int) -> bool:
    """Insert a streamed candidate into the name-sorted bounded window,
    evicting the name-largest candidate when the window exceeds keep.
    Memory over an arbitrarily large level stays O(keep)."""
    if (len(window) == keep
            and candidate["name"] >= window[keep - 1]["name"]):
        return True
    lo = 0
    hi = len(window)
    while lo < hi:
        mid = (lo + hi) // 2
        if candidate["name"] < window[mid]["name"]:
            hi = mid
        else:
            lo = mid + 1
    window.insert(lo, candidate)
    if len(window) <= keep:
        return False
    window.pop()
    return True


def directory_row(parent: str, name: str, is_directory: bool,
                  is_symbolic_link: bool, cancel: Cancel) -> Optional[Dict[str, Any]]:
    """One listing row. Symlinks are followed to directories; broken or
    cyclic links are silently skipped."""
    if is_symbolic_link:
        try:
            _throw_cancelled(cancel)
            if not os.path.isdir(os.path.join(parent, name)):
                return None
        except RuntimeError:
            return None
        is_directory = True
    return {
        "name": name,
        "path": os.path.join(parent, name),
        "hidden": name.startswith("."),
        "isDirectory": is_directory,
    }


def validate_browse_config(config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """z.object({maxEntries: natural().min(1).default(1000)}) natively:
    snake_case, unknown keys stripped, invalid values loud."""
    config = dict(config or {})
    result: Dict[str, Any] = {}
    if "max_entries" in config and config["max_entries"] is not None:
        value = config["max_entries"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("max_entries must be an integer >= 1")
        result["max_entries"] = value
    else:
        result["max_entries"] = 1000
    return result


class BrowseDirectoryPicker(DirectoryPicker):
    """The ctx.directoryPicker browse implementation (stable capability
    object per service life).

    One interaction shape: a directory browser served entirely from the
    host's filesystem — list one directory level (entries, breadcrumb
    ancestry, truncated flag) and create a single child directory. The
    client renders its own in-app browser; nothing renders on the host's
    display, so this backend also serves remote clients that a native OS
    dialog cannot reach.
    """

    def __init__(self, config: Optional[Mapping[str, Any]] = None,
                 home: Optional[str] = None):
        self.config = validate_browse_config(config)
        self._home = home
        self._capability: Dict[str, Any] = {
            "kind": "browse",
            "list": self.list,
            "createDirectory": self.create_directory,
        }

    def capability(self) -> Dict[str, Any]:
        return self._capability

    def _home_directory(self) -> str:
        return self._home if self._home is not None else os.path.expanduser("~")

    def list(self, path: Optional[str] = None,
             cancel: Cancel = None) -> Dict[str, Any]:
        """List one directory level. ``path`` must be fully qualified when
        present; absent it lists the process user's home. Children are
        returned in ascending name order, capped at max_entries with
        ``truncated`` telling whether the level holds more.
        """
        if path is not None and not fully_qualified(path):
            raise DirectoryPickerError(
                "directory-unreadable", path,
                f'cannot list "{path}": not a fully qualified path')
        home = self._home_directory()
        target = os.path.abspath(path if path is not None else home)
        keep = self.config["max_entries"] + 1
        window: List[Dict[str, Any]] = []
        evicted = False
        try:
            _throw_cancelled(cancel)
            with os.scandir(target) as iterator:
                for entry in iterator:
                    if not entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                        continue
                    candidate = {"name": entry.name, "path": entry.path}
                    if bounded_insert(window, candidate, keep):
                        evicted = True
        except (OSError, ValueError) as error:
            _throw_cancelled(cancel)
            message = str(error)
            raise DirectoryPickerError(
                "directory-unreadable", target,
                f"cannot list {target}: {message}") from error
        except RuntimeError:
            raise
        entries: List[Dict[str, Any]] = []
        truncated = False
        for candidate in window:
            _throw_cancelled(cancel)
            row = directory_row(target, candidate["name"], True,
                                os.path.islink(candidate["path"]), cancel)
            if row is None:
                continue
            if len(entries) == self.config["max_entries"]:
                truncated = True
                break
            entries.append(row)
        # upstream reads `truncated` from the entry phase alone (the window
        # bound is max_entries + 1, so a full window always fills the phase
        # unless every row was a broken symlink — where upstream still says
        # false); `evicted` stays the streamed-memory fact it is upstream
        del evicted
        return {
            "path": target,
            "home": home,
            "crumbs": ancestry_crumbs(target),
            "entries": entries,
            "truncated": truncated,
        }

    def create_directory(self, path: str, name: str) -> str:
        """Create one child directory under a fully qualified ``path``;
        ``name`` must be a single path segment. Resolves to the created
        absolute path."""
        if not fully_qualified(path):
            raise DirectoryPickerError(
                "directory-create-failed", path,
                f'cannot create under "{path}": not a fully qualified parent path')
        parent = os.path.abspath(path)
        if (name.strip() == "" or name == "." or name == ".."
                or re.search(r"[/\\]", name)):
            raise DirectoryPickerError(
                "directory-create-failed", os.path.join(parent, name),
                f'"{name}" is not a single path segment')
        target = os.path.join(parent, name)
        try:
            os.mkdir(target)
            return target
        except FileExistsError as error:
            raise DirectoryPickerError(
                "directory-exists", target, f"{target} already exists") from error
        except OSError as error:
            raise DirectoryPickerError(
                "directory-create-failed", target,
                f"cannot create {target}: {error}") from error


# -------------------------------------------------------------------- auto

LINUX_CHOOSER_BINARIES = ("zenity", "kdialog")

BACKEND_PACKAGES = {
    "native": "@deepseek-ai/dsh-host-directory-picker-native",
    "browse": "@deepseek-ai/dsh-host-directory-picker-browse",
}

SURFACE_PACKAGES = {
    "native": "@deepseek-ai/dsh-client-ui-directory-picker-native",
    "browse": "@deepseek-ai/dsh-client-ui-directory-picker-browse",
}


def can_execute(candidate: str) -> bool:
    """Whether the current process may execute the candidate path."""
    return os.access(candidate, os.X_OK)


def has_linux_chooser_binary(path_value: Optional[str],
                             is_executable: Callable[[str], bool] = can_execute) -> bool:
    """Scan a PATH value for one of the native backend's Linux chooser
    binaries (zenity, KDialog fallback)."""
    for directory in (path_value or "").split(os.pathsep):
        if directory == "":
            continue
        for name in LINUX_CHOOSER_BINARIES:
            if is_executable(os.path.join(directory, name)):
                return True
    return False


def _present(value: Optional[str]) -> bool:
    return value is not None and value != ""


def resolve_directory_picker_backend(facts: Dict[str, Any]) -> str:
    """Resolve which backend serves this boot. ``native`` requires every
    signal that the operator can see the host display and the native backend
    can serve it: a loopback-only bind (an all-interfaces bind admits remote
    browsers no OS chooser can reach), no SSH launch (under SSH port-
    forwarding the chooser would open on the unattended server), and a
    servable display session — assumed on darwin/win32, requiring
    DISPLAY/WAYLAND_DISPLAY plus a chooser binary on linux, and never true
    elsewhere. Anything ambiguous resolves to ``browse``, which works
    everywhere."""
    if facts["bind_host"] != "127.0.0.1":
        return "browse"
    if facts["ssh"]:
        return "browse"
    if facts["platform"] in ("darwin", "win32"):
        return "native"
    if facts["platform"] != "linux" or not facts["linux_chooser"]:
        return "browse"
    env = facts["env"]
    if _present(env.get("DISPLAY")) or _present(env.get("WAYLAND_DISPLAY")):
        return "native"
    return "browse"


def mount_directory_picker(bind_host: str,
                           ssh: bool,
                           env: Optional[Mapping[str, str]] = None,
                           platform: Optional[str] = None,
                           browse_config: Optional[Mapping[str, Any]] = None,
                           native_internals: Optional[Dict[str, Any]] = None,
                           ) -> tuple:
    """One boot-time sample → the resolved backend instance plus its kind.

    The caller samples exactly once per boot, so the mounted capability
    stays stable for the service lifetime as the seam requires.
    """
    env_mapping = env if env is not None else os.environ
    resolved_platform = platform or sys.platform
    kind = resolve_directory_picker_backend({
        "bind_host": bind_host,
        "platform": resolved_platform,
        "ssh": ssh,
        "env": env_mapping,
        "linux_chooser": has_linux_chooser_binary(env_mapping.get("PATH")),
    })
    if kind == "native":
        return NativeDirectoryPicker(native_internals), kind
    return BrowseDirectoryPicker(browse_config), kind
