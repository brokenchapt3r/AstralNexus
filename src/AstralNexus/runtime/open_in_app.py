"""Host half of open-in-app: the launchable-application catalog, its
platform resolution, per-application icon extraction, and the three routes
the browser split button posts to.

Port of ``@deepseek-ai/dsh-host-open-in-app`` (commit 5badb15, lib/index.js
1404-line bundle read in full; catalog/resolver/icons/shared/index segments
all inlined here). Contract substance:

- The CATALOG is compile-time data: each entry declares per-platform
  locator chains tried in order; the first VERIFIED launcher wins, so a
  click never launches something this host does not hold.
- One resolution pass yields the map the routes serve and launch from (a
  click, menu open, or page reload never re-runs detection); the Windows
  registry is read AT MOST ONCE per pass. The map is the mutable authority
  the owner holds: a launch that finds its executable gone (ENOENT)
  invalidates that one entry and re-resolves it once.
- Application adapters spawn DETACHED with a credential-scrubbed
  environment and their declared Windows visibility policy; launch success
  is decoupled from process exit — the watch window only catches launchers
  that fail immediately; a child still running when the window closes is
  counted launched and NEVER killed. ``shell-open`` launches (the file
  managers) go through the native-command path opener — the OS shell's open
  verb — instead of a direct spawn.
- Icons: one strategy per platform (macOS bundle .icns → 128px PNG via
  plutil+sips; Windows executable → 32px PNG via ExtractAssociatedIcon,
  the most the stock .NET surface gives without a native addon; Linux
  desktop-entry Icon= → hicolor/pixmaps, no subprocess). Every failure
  resolves null and the icon route answers 404 — the browser renders a
  generic glyph.
- Wire contract of the open route (verbatim messages): 405 with allow, 415
  unsupported-media-type, 413 payload-too-large past the 64 KiB ceiling
  ("anything larger is hostile"), 400 bad-request (unreadable body /
  non-JSON / non-string app|path / unknown-or-unavailable app / non-
  absolute path), 404 not-found (directory does not exist), 200 {ok:true},
  502 launch-failed. JSON responses carry cache-control no-store
  (availability and launch outcomes are live facts); icons carry
  public max-age=3600.

Python adaptations (documented):
- async → sync throughout; Promise.all catalog resolution → sequential
  pass (same once-per-pass registry sharing, same result map).
- internals seam preserved: {platform, ssh, application_roots, env, home,
  run, launch, resolve_executable} — resolve_executable is REQUIRED (a
  caller that omits it fails loud rather than silently resolving every cli
  locator as missing); production wires it to shutil.which through the
  subprocess seam.
- launchDetachedApp → Popen with DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP
  (win32) / start_new_session (posix), stdio DEVNULL, env = scrubbed
  parent env (dsh-subprocess's one scrub definition) overlaid with the
  adapter's explicit entries; windowsHide → CREATE_NO_WINDOW with the
  native_command one-shot 0xC0000142 degrade (sandbox hosts refuse the
  hide flag before any user code runs).
- AbortSignal.timeout(ms) on probe commands → a cancel Event armed by a
  Timer (a timeout surfaces as the runner's ABORT_ERR → output() → null,
  exactly the "any failure → null" contract).
- localeCompare(…, numeric) version-descending sort → a natural-sort key
  (digit runs compare numerically, other runs lexically) — the documented
  approximation of ICU numeric collation.
- The connection trust fence (Host/Origin + login-token cookie) belongs to
  dsh-client-connection (not yet ported): the service takes an injected
  ``fence`` callable answering a rejection status per request; the host
  composition supplies its loopback Origin/Host fence. Recorded as subset,
  not faked.
- Route paths ship verbatim as the shared constants (the B6 browser
  package addresses the document-relative forms); the host registers them
  under its own /api/host prefix.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, NoReturn, Optional, Tuple

from AstralNexus.runtime import native_command as nc
from AstralNexus.runtime.subprocess_runtime import scrubbed_parent_env
from AstralNexus.runtime.dshutil.launch_environment import (
    LaunchEnvironmentLayer, create_launch_environment_snapshot,
    launched_through_ssh)

__all__ = [
    "PATH_TOKEN", "OPEN_IN_APP_CATALOG", "launch_detached_app",
    "resolve_internals", "output", "expand_candidate", "expand_registry_value",
    "parse_registry_dump", "read_windows_registry_view", "RegistryViewOnce",
    "record_launcher", "parse_desktop_entry", "xdg_data_directories",
    "find_desktop_entry", "desktop_launcher", "exec_command", "spec_for",
    "executable_icon", "locate", "resolve_launch", "resolve_open_in_app_apps",
    "launch_args", "is_missing_executable", "run_shell_open", "run_launch",
    "launch_resolved", "extract_bundle_icon_png", "EXTRACT_ICON_PS1",
    "extract_executable_icon_png", "extract_linux_icon", "extract_app_icon",
    "OPEN_IN_APP_APPS_PATH", "OPEN_IN_APP_ICON_PREFIX_PATH",
    "OPEN_IN_APP_OPEN_PATH", "MAX_BODY_BYTES", "parse_open_body",
    "validate_open_in_app_config", "OpenInAppService", "natural_sort_key",
]

Response = Tuple[int, Dict[str, Any], Any]

# ------------------------------------------------------------------ catalog

#: Launch-args token carrying the workspace directory (``--cd={path}``).
PATH_TOKEN = "{path}"


def _mac_app(*fs_names: str) -> Dict[str, Any]:
    """macOS spec checking the known application directories for the named
    bundles."""
    return {"locators": [{"kind": "app", "fsNames": list(fs_names)}]}


def _spec(*locators: Dict[str, Any]) -> Dict[str, Any]:
    """Iconless spec from its locator chain."""
    return {"locators": list(locators)}


def _desktop_spec(desktop_id: str, *locators: Dict[str, Any]) -> Dict[str, Any]:
    """Spec from its locator chain plus the Linux desktop entry owning its
    icon."""
    return {"locators": list(locators), "desktopId": desktop_id}


def _cli(name: str, *args: str) -> Dict[str, Any]:
    """In-process PATH-name locator launching the resolved executable."""
    return {"kind": "cli", "name": name, "args": list(args)}


def _desktop_cli(name: str, *args: str) -> Dict[str, Any]:
    """In-process PATH-name locator meaningful only with a desktop session."""
    return {"kind": "cli", "name": name, "args": list(args),
            "requiresDesktop": True}


def _file(candidates: List[str], *args: str) -> Dict[str, Any]:
    """First-existing-file locator launching the matched candidate."""
    return {"kind": "file", "candidates": candidates, "args": list(args)}


def _app_paths(exe: str, *args: str) -> Dict[str, Any]:
    """Windows ``App Paths`` registry locator for one registered executable
    name."""
    return {"kind": "app-paths", "exe": exe, "args": list(args)}


def _install_record(display_name_prefix: str,
                    relative_launcher: Optional[str] = None,
                    *args: str) -> Dict[str, Any]:
    """Windows Uninstall-record locator verified through the executable it
    points at."""
    return {"kind": "install-record", "displayNamePrefix": display_name_prefix,
            "relativeLauncher": relative_launcher, "args": list(args)}


def _jetbrains(app_id: str, product_name: str, cli_name: str, win_exe: str,
               mac_names: List[str]) -> Dict[str, Any]:
    """JetBrains product entry: known bundle names on macOS (direct-download
    and Toolbox spellings), the newest versioned ``%ProgramFiles%\\JetBrains``
    install or a verified Uninstall record on Windows, PATH command or
    Toolbox shell script on Linux."""
    return {
        "id": app_id,
        "platforms": {
            "darwin": _mac_app(*mac_names),
            "win32": _spec(
                {"kind": "scan", "root": "${ProgramFiles}/JetBrains",
                 "namePrefix": product_name,
                 "relativeLauncher": f"bin/{win_exe}", "args": []},
                _install_record(product_name, f"bin/{win_exe}")),
            "linux": _spec(
                _cli(cli_name),
                _file([f"~/.local/share/JetBrains/Toolbox/scripts/{cli_name}"])),
        },
    }


#: The launch catalog in menu order: file managers, editors and IDEs, Git
#: GUIs, terminals. Finder, Terminal, and Explorer ship with their operating
#: systems, so their locators always resolve there. macOS bundle names list
#: the common install spellings; a bundle renamed or moved outside
#: /Applications and ~/Applications is not detected (Known Limitations).
OPEN_IN_APP_CATALOG: List[Dict[str, Any]] = [
    {"id": "finder", "platforms": {"darwin": _spec({
        "kind": "fixed", "launch": {"kind": "shell-open"},
        "iconPath": "/System/Library/CoreServices/Finder.app"})}},
    {"id": "explorer", "platforms": {"win32": _spec({
        "kind": "fixed", "launch": {"kind": "shell-open"},
        "iconPath": "${SystemRoot}/explorer.exe"})}},
    {"id": "filemanager", "platforms": {"linux": _spec(_desktop_cli("xdg-open"))}},
    {"id": "cursor", "platforms": {
        "darwin": _mac_app("Cursor.app"),
        "win32": _spec(_app_paths("Cursor.exe"), _install_record("Cursor"),
                       _file(["${LOCALAPPDATA}/Programs/cursor/Cursor.exe"])),
        "linux": _spec(_cli("cursor"))}},
    {"id": "vscode", "platforms": {
        "darwin": _mac_app("Visual Studio Code.app"),
        "win32": _spec(
            _app_paths("Code.exe"),
            _install_record("Microsoft Visual Studio Code", "Code.exe"),
            _file(["${LOCALAPPDATA}/Programs/Microsoft VS Code/Code.exe",
                   "${ProgramFiles}/Microsoft VS Code/Code.exe"])),
        "linux": _desktop_spec("code", _cli("code"))}},
    {"id": "vscodeinsiders", "platforms": {
        "darwin": _mac_app("Visual Studio Code - Insiders.app"),
        "win32": _spec(
            _app_paths("Code - Insiders.exe"),
            _install_record("Microsoft Visual Studio Code Insiders",
                            "Code - Insiders.exe"),
            _file(["${LOCALAPPDATA}/Programs/Microsoft VS Code Insiders/Code - Insiders.exe"])),
        "linux": _desktop_spec("code-insiders", _cli("code-insiders"))}},
    {"id": "windsurf", "platforms": {
        "darwin": _mac_app("Windsurf.app"),
        "win32": _spec(_app_paths("Windsurf.exe"), _install_record("Windsurf"),
                       _file(["${LOCALAPPDATA}/Programs/Windsurf/Windsurf.exe"])),
        "linux": _spec(_cli("windsurf"))}},
    {"id": "zed", "platforms": {
        "darwin": _mac_app("Zed.app", "Zed Preview.app"),
        "linux": _desktop_spec("dev.zed.Zed", _cli("zed"),
                               {"kind": "desktop", "desktopId": "dev.zed.Zed",
                                "args": []})}},
    {"id": "sublimetext", "platforms": {
        "darwin": _mac_app("Sublime Text.app"),
        "win32": _spec(_app_paths("sublime_text.exe"),
                       _install_record("Sublime Text"),
                       _file(["${ProgramFiles}/Sublime Text/sublime_text.exe"])),
        "linux": _desktop_spec("sublime_text", _cli("subl"))}},
    {"id": "xcode", "platforms": {"darwin": _spec({"kind": "xcode"})}},
    {"id": "androidstudio", "platforms": {
        "darwin": _mac_app("Android Studio.app"),
        "win32": _spec(_install_record("Android Studio", "bin/studio64.exe"),
                       _file(["${ProgramFiles}/Android/Android Studio/bin/studio64.exe"])),
        "linux": _spec(_cli("studio"),
                       _file(["~/.local/share/JetBrains/Toolbox/scripts/studio",
                              "/opt/android-studio/bin/studio.sh"]))}},
    _jetbrains("intellij", "IntelliJ IDEA", "idea", "idea64.exe",
               ["IntelliJ IDEA.app", "IntelliJ IDEA Ultimate.app",
                "IntelliJ IDEA CE.app"]),
    _jetbrains("pycharm", "PyCharm", "pycharm", "pycharm64.exe",
               ["PyCharm.app", "PyCharm Professional.app", "PyCharm CE.app",
                "PyCharm Community.app"]),
    _jetbrains("webstorm", "WebStorm", "webstorm", "webstorm64.exe",
               ["WebStorm.app"]),
    _jetbrains("phpstorm", "PhpStorm", "phpstorm", "phpstorm64.exe",
               ["PhpStorm.app"]),
    _jetbrains("goland", "GoLand", "goland", "goland64.exe", ["GoLand.app"]),
    _jetbrains("rider", "Rider", "rider", "rider64.exe",
               ["Rider.app", "JetBrains Rider.app"]),
    _jetbrains("rustrover", "RustRover", "rustrover", "rustrover64.exe",
               ["RustRover.app"]),
    {"id": "fork", "platforms": {
        "darwin": _mac_app("Fork.app"),
        "win32": _spec(_install_record("Fork"),
                       _file(["${LOCALAPPDATA}/Fork/Fork.exe"]))}},
    {"id": "sourcetree", "platforms": {"darwin": _mac_app("Sourcetree.app")}},
    {"id": "github", "platforms": {
        "darwin": _mac_app("GitHub Desktop.app"),
        "win32": _spec({"kind": "github-desktop",
                        "root": "${LOCALAPPDATA}/GitHubDesktop"})}},
    {"id": "tower", "platforms": {"darwin": _mac_app("Tower.app")}},
    {"id": "gitkraken", "platforms": {"darwin": _mac_app("GitKraken.app")}},
    {"id": "smartgit", "platforms": {"darwin": _mac_app("SmartGit.app")}},
    {"id": "sublimemerge", "platforms": {
        "darwin": _mac_app("Sublime Merge.app"),
        "win32": _spec(_app_paths("sublime_merge.exe"),
                       _install_record("Sublime Merge"),
                       _file(["${ProgramFiles}/Sublime Merge/sublime_merge.exe"])),
        "linux": _desktop_spec("sublime_merge", _cli("smerge"))}},
    {"id": "ghostty", "platforms": {
        "darwin": _mac_app("Ghostty.app"),
        "linux": _desktop_spec(
            "com.mitchellh.ghostty",
            _cli("ghostty", f"--working-directory={PATH_TOKEN}"),
            {"kind": "desktop", "desktopId": "com.mitchellh.ghostty",
             "args": [f"--working-directory={PATH_TOKEN}"]})}},
    {"id": "warp", "platforms": {"darwin": _mac_app("Warp.app")}},
    {"id": "iterm", "platforms": {"darwin": _mac_app("iTerm.app")}},
    {"id": "kitty", "platforms": {
        "darwin": _mac_app("kitty.app"),
        "linux": _desktop_spec("kitty", _cli("kitty", "--directory"),
                               {"kind": "desktop", "desktopId": "kitty",
                                "args": ["--directory"]})}},
    {"id": "terminal", "platforms": {"darwin": _spec({
        "kind": "fixed",
        "launch": {"kind": "argv", "command": "open", "args": ["-a", "Terminal"]},
        "iconPath": "/System/Applications/Utilities/Terminal.app"})}},
    {"id": "windowsterminal", "platforms": {"win32": _spec(_cli("wt", "-d"))}},
    {"id": "gitbash", "platforms": {"win32": _spec(
        _install_record("Git version", "git-bash.exe", f"--cd={PATH_TOKEN}"),
        _file(["${ProgramFiles}/Git/git-bash.exe"], f"--cd={PATH_TOKEN}"))}},
    {"id": "gnometerminal", "platforms": {"linux": _desktop_spec(
        "org.gnome.Terminal",
        _cli("gnome-terminal", f"--working-directory={PATH_TOKEN}"),
        {"kind": "desktop", "desktopId": "org.gnome.Terminal",
         "args": [f"--working-directory={PATH_TOKEN}"]})}},
    {"id": "konsole", "platforms": {"linux": _desktop_spec(
        "org.kde.konsole", _cli("konsole", "--workdir"),
        {"kind": "desktop", "desktopId": "org.kde.konsole",
         "args": ["--workdir"]})}},
]

# ----------------------------------------------------------------- resolver


def launch_detached_app(command: str, args: List[str],
                        options: Optional[Dict[str, Any]] = None) -> None:
    """Launch one application adapter detached from this process: the child
    gets a credential-scrubbed environment (never the harness's
    *KEY*/*SECRET* variables) plus the adapter's explicit environment
    entries, holds no stdio pipe, and outlives dsh. Launch success is
    decoupled from process exit — launchers such as kitty or the JetBrains
    IDEs stay in the foreground for their whole window lifetime, so the
    watch window only catches launchers that fail immediately: raises on a
    spawn failure and on a nonzero exit inside the window; a child still
    running when the window closes is counted launched, NEVER killed.
    """
    options = options or {}
    watch_ms = int(options.get("watchMs", 2000))
    env = {**scrubbed_parent_env(), **(options.get("env") or {})}
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL, "env": env, "cwd": None}
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if options.get("windowsHide"):
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen([command, *args], **kwargs)
    except OSError:
        if sys.platform == "win32" and options.get("windowsHide"):
            # the hide flag can be refused before any user code runs
            # (0xC0000142 under sandbox hosts) — degrade once, as the
            # native-command runner documents
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
            child = subprocess.Popen([command, *args], **kwargs)
        else:
            raise
    try:
        code = child.wait(timeout=watch_ms / 1000.0)
    except subprocess.TimeoutExpired:
        # still running when the window closes: launched, and it keeps
        # running (never killed)
        return
    if code != 0:
        raise RuntimeError(
            f"launcher exited with code {code}, signal None")


def resolve_internals(internals: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve the injectable facts against the running host.
    ``resolve_executable`` has no host default — the composition's
    subprocess capability provides it — so a caller that omits it fails
    loud here rather than silently resolving every cli locator as missing.
    """
    internals = internals or {}
    home = internals.get("home") or os.path.expanduser("~")
    resolve_executable = internals.get("resolve_executable")
    if resolve_executable is None:
        raise RuntimeError(
            "open-in-app: internals.resolve_executable is required "
            "(the subprocess capability provides it)")
    return {
        "platform": internals.get("platform") or sys.platform,
        "ssh": internals.get("ssh", False),
        "application_roots": internals.get("application_roots")
        or ["/Applications", os.path.join(home, "Applications")],
        "env": internals.get("env") if internals.get("env") is not None
        else os.environ,
        "home": home,
        "run": internals.get("run") or nc.run_native_command,
        "launch": internals.get("launch") or launch_detached_app,
        "resolve_executable": resolve_executable,
    }


def _assert_never(value: Any) -> NoReturn:
    """Closed-union exhaustiveness fence for the catalog's locator kinds."""
    raise RuntimeError(
        f"unhandled open-in-app catalog kind: {json.dumps(value)}")


def output(command: str, args: List[str], timeout_ms: int,
           internals: Dict[str, Any]) -> Optional[str]:
    """Run one bounded host command. Returns stdout on exit 0; None on any
    failure (spawn, nonzero exit, timeout)."""
    cancel = threading.Event()
    timer = threading.Timer(timeout_ms / 1000.0, cancel.set)
    timer.daemon = True
    timer.start()
    try:
        result = internals["run"](command, args, cancel)
        return result["stdout"]
    except Exception:
        return None
    finally:
        timer.cancel()


def is_directory(path: str) -> bool:
    """Probe one path as an existing directory."""
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def is_file(path: str) -> bool:
    """Probe one path as an existing regular file."""
    try:
        return os.path.isfile(path)
    except OSError:
        return False


_VAR_TEMPLATE = re.compile(r"\$\{([^}]+)\}")
_PERCENT_TEMPLATE = re.compile(r"%([^%]+)%")


def expand_candidate(template: str, internals: Dict[str, Any]) -> Optional[str]:
    """Expand ``${VAR}`` references and a leading ``~/``. Expansion is
    string substitution: a candidate keeps its template's ``/`` separators
    after the expanded prefix, which Win32 path APIs accept. None when a
    variable is unset."""
    unset: List[str] = []

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        value = internals["env"].get(name)
        if value is None:
            unset.append(name)
            return match.group(0)
        return value

    expanded = _VAR_TEMPLATE.sub(replace, template)
    if unset:
        return None
    if expanded.startswith("~/"):
        return os.path.join(internals["home"], expanded[2:])
    return expanded


def expand_registry_value(value: str, internals: Dict[str, Any]) -> Optional[str]:
    """Expand ``%VAR%`` references in a Windows registry value; None when a
    variable is unset."""
    unset: List[str] = []

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        found = internals["env"].get(name)
        if found is None:
            unset.append(name)
            return match.group(0)
        return found

    expanded = _PERCENT_TEMPLATE.sub(replace, value)
    return None if unset else expanded


#: ``App Paths`` roots, user hive first (per-user installs shadow machine
#: ones).
APP_PATHS_ROOTS = [
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths",
    r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
]

#: Uninstall-record roots: user hive, 64-bit machine hive, 32-bit machine
#: view.
UNINSTALL_ROOTS = [
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
]

_HK_LINE = re.compile(r"^HK")
_VALUE_LINE = re.compile(r"^\s+(.*?)\s+(REG_SZ|REG_EXPAND_SZ)\s+(.*)$")
_LOCALIZED_DEFAULT = re.compile(r"^\(.*\)$")


def parse_registry_dump(dump: str) -> Dict[str, Dict[str, str]]:
    """Parse ``reg.exe query <root> /s`` output into per-subkey string
    values. The value-name/type/data columns are matched by the REG_* type
    token because the default-value marker localizes (``(Default)``,
    ``(默认)``)."""
    keys: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    for line in re.split(r"\r?\n", dump):
        if _HK_LINE.match(line):
            current = {}
            keys[line.strip()] = current
            continue
        match = _VALUE_LINE.match(line)
        if match is None or current is None:
            continue
        name, data = match.group(1), match.group(3)
        current["(Default)" if _LOCALIZED_DEFAULT.match(name) else name] = \
            data.strip()
    return keys


def read_windows_registry_view(timeout_ms: int,
                               internals: Dict[str, Any]) -> Dict[str, Any]:
    """Build the Windows registry facts for one resolution pass: the App
    Paths table and the Uninstall records, one ``reg.exe query /s`` per
    root. A root that fails or is absent contributes nothing."""
    app_paths: Dict[str, str] = {}
    install_records: List[Dict[str, Optional[str]]] = []
    for root in APP_PATHS_ROOTS:
        dump = output("reg.exe", ["query", root, "/s"], timeout_ms, internals)
        if dump is None:
            continue
        for key, values in parse_registry_dump(dump).items():
            exe = key[key.rfind("\\") + 1:].lower()
            target = values.get("(Default)")
            if not exe.endswith(".exe") or target is None or exe in app_paths:
                continue
            expanded = expand_registry_value(
                re.sub(r'^"|"$', "", target), internals)
            if expanded is not None:
                app_paths[exe] = expanded
    for root in UNINSTALL_ROOTS:
        dump = output("reg.exe", ["query", root, "/s"], timeout_ms, internals)
        if dump is None:
            continue
        for values in parse_registry_dump(dump).values():
            display_name = values.get("DisplayName")
            if display_name is None:
                continue
            install_records.append({
                "displayName": display_name,
                "installLocation": values.get("InstallLocation"),
                "displayIcon": values.get("DisplayIcon"),
            })
    return {"appPaths": app_paths, "installRecords": install_records}


class RegistryViewOnce:
    """Pass-scoped lazy holder so one detection pass reads the registry at
    most once."""

    def __init__(self, timeout_ms: int, internals: Dict[str, Any]):
        self.timeout_ms = timeout_ms
        self.internals = internals
        self.view: Optional[Dict[str, Any]] = None

    def read(self) -> Dict[str, Any]:
        if self.view is None:
            self.view = read_windows_registry_view(self.timeout_ms,
                                                   self.internals)
        return self.view


_ICON_INDEX_SUFFIX = re.compile(r",-?\d+$")


def record_launcher(record: Dict[str, Optional[str]],
                    relative_launcher: Optional[str],
                    internals: Dict[str, Any]) -> Optional[str]:
    """The executable a Windows Uninstall record proves, or None when it
    proves none."""
    install_location = record.get("installLocation")
    if (relative_launcher is not None and install_location is not None
            and install_location != ""):
        expanded = expand_registry_value(
            re.sub(r'^"|"$', "", install_location), internals)
        if expanded is not None:
            candidate = os.path.join(expanded, relative_launcher)
            if is_file(candidate):
                return candidate
    display_icon = record.get("displayIcon")
    if display_icon is not None:
        stripped = re.sub(r'^"|"$', "",
                          _ICON_INDEX_SUFFIX.sub("", display_icon)).strip()
        expanded = expand_registry_value(stripped, internals)
        if (expanded is not None and expanded.lower().endswith(".exe")
                and is_file(expanded)):
            return expanded
    return None


def parse_desktop_entry(text: str) -> Dict[str, str]:
    """The ``[Desktop Entry]`` section's Exec/TryExec/Icon keys (keys
    outside the entry section are ignored — native_command's parser)."""
    fields = nc.desktop_entry_fields(text)
    result: Dict[str, str] = {}
    if "Exec" in fields:
        result["exec"] = fields["Exec"]
    if "TryExec" in fields:
        result["tryExec"] = fields["TryExec"]
    if "Icon" in fields:
        result["icon"] = fields["Icon"]
    return result


def xdg_data_directories(internals: Dict[str, Any]) -> List[str]:
    """XDG data directories in precedence order (XDG_DATA_HOME, then
    XDG_DATA_DIRS)."""
    return nc.desktop_data_directories(internals["home"], internals["env"])


def find_desktop_entry(desktop_id: str,
                       internals: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Read one desktop entry by id from the XDG application directories;
    None when no directory holds it."""
    for data_dir in xdg_data_directories(internals):
        path = os.path.join(data_dir, "applications", f"{desktop_id}.desktop")
        try:
            with open(path, encoding="utf-8") as handle:
                return parse_desktop_entry(handle.read())
        except OSError:
            continue
        except UnicodeDecodeError:
            continue
    return None


_QUOTED_EXEC = re.compile(r'^"([^"]+)"')
_BARE_EXEC = re.compile(r"^\S+")


def exec_command(exec_value: Optional[str]) -> Optional[str]:
    """First token of an ``Exec=`` value: the quoted path or the run up to
    whitespace; None when absent or blank."""
    if exec_value is None:
        return None
    quoted = _QUOTED_EXEC.match(exec_value)
    if quoted is not None:
        return quoted.group(1)
    bare = _BARE_EXEC.match(exec_value)
    return None if bare is None else bare.group(0)


def desktop_launcher(entry: Dict[str, str],
                     internals: Dict[str, Any]) -> Optional[str]:
    """The executable one desktop entry proves: a TryExec when present,
    otherwise Exec's first token; absolute paths verify on disk and bare
    names resolve in-process through the subprocess capability."""
    candidate = entry.get("tryExec")
    if candidate is None:
        candidate = exec_command(entry.get("exec"))
    if candidate is None or candidate == "":
        return None
    if os.path.isabs(candidate):
        return candidate if is_file(candidate) else None
    return internals["resolve_executable"](candidate)


def spec_for(app: Dict[str, Any], platform: str) -> Optional[Dict[str, Any]]:
    """The catalog entry's spec for one platform; None off the declared
    three platforms."""
    if platform in ("darwin", "win32", "linux"):
        return app["platforms"].get(platform)
    return None


def executable_icon(path: str, internals: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Icon source for a resolved executable: Windows extracts from the
    binary itself."""
    if internals["platform"] == "win32":
        return {"kind": "executable", "path": path}
    return None


def natural_sort_key(text: str) -> List[Any]:
    """Digit runs compare numerically, other runs lexically — the
    documented approximation of localeCompare(…, numeric) for version
    directory names."""
    parts: List[Any] = []
    for piece in re.split(r"(\d+)", text):
        parts.append(int(piece) if piece.isdigit() else piece)
    return parts


def locate(locator: Dict[str, Any], probe_timeout_ms: int,
           registry: RegistryViewOnce,
           internals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve one locator to a verified launch, or None when it proves
    nothing."""
    kind = locator["kind"]
    if kind == "fixed":
        icon_path = expand_candidate(locator["iconPath"], internals)
        icon: Optional[Dict[str, str]] = None
        if icon_path is not None:
            icon = ({"kind": "executable", "path": icon_path}
                    if internals["platform"] == "win32"
                    else {"kind": "app-bundle", "path": icon_path})
        return {"launch": locator["launch"], "icon": icon}
    if kind == "app":
        for root in internals["application_roots"]:
            for fs_name in locator["fsNames"]:
                bundle = os.path.join(root, fs_name)
                if is_directory(bundle):
                    return {
                        "launch": {"kind": "argv", "command": "open",
                                   "args": ["-a", bundle]},
                        "icon": {"kind": "app-bundle", "path": bundle}}
        return None
    if kind == "xcode":
        developer = output("xcode-select", ["-p"], probe_timeout_ms, internals)
        if developer is None:
            return None
        bundle = os.path.dirname(os.path.dirname(developer.strip()))
        if not bundle.endswith(".app") or not is_directory(bundle):
            return None
        return {
            "launch": {"kind": "argv", "command": "xed", "args": []},
            "fallbackLaunch": {"kind": "argv", "command": "open",
                               "args": ["-a", bundle]},
            "icon": {"kind": "app-bundle", "path": bundle}}
    if kind == "cli":
        if locator.get("requiresDesktop") is True and not nc.can_open_native_path(
                {"platform": internals["platform"],
                 "env": dict(internals["env"])}):
            return None
        found = internals["resolve_executable"](locator["name"])
        if found is None:
            return None
        return {"launch": {"kind": "argv", "command": found,
                           "args": locator["args"]},
                "icon": executable_icon(found, internals)}
    if kind == "file":
        for candidate in locator["candidates"]:
            path = expand_candidate(candidate, internals)
            if path is not None and is_file(path):
                return {"launch": {"kind": "argv", "command": path,
                                   "args": locator["args"]},
                        "icon": executable_icon(path, internals)}
        return None
    if kind == "scan":
        root = expand_candidate(locator["root"], internals)
        if root is None:
            return None
        try:
            entries = os.listdir(root)
        except OSError:
            return None
        versions = sorted(
            (entry for entry in entries
             if entry.startswith(locator["namePrefix"])),
            key=natural_sort_key, reverse=True)
        for version in versions:
            launcher = os.path.join(root, version, locator["relativeLauncher"])
            if is_file(launcher):
                return {"launch": {"kind": "argv", "command": launcher,
                                   "args": locator["args"]},
                        "icon": executable_icon(launcher, internals)}
        return None
    if kind == "app-paths":
        target = registry.read()["appPaths"].get(locator["exe"].lower())
        if target is None or not is_file(target):
            return None
        return {"launch": {"kind": "argv", "command": target,
                           "args": locator["args"]},
                "icon": {"kind": "executable", "path": target}}
    if kind == "install-record":
        for record in registry.read()["installRecords"]:
            if not record["displayName"].startswith(locator["displayNamePrefix"]):
                continue
            launcher = record_launcher(record, locator.get("relativeLauncher"),
                                       internals)
            if launcher is not None:
                return {"launch": {"kind": "argv", "command": launcher,
                                   "args": locator["args"]},
                        "icon": {"kind": "executable", "path": launcher}}
        return None
    if kind == "github-desktop":
        root = expand_candidate(locator["root"], internals)
        if root is None:
            return None
        try:
            versions = sorted(
                (entry for entry in os.listdir(root)
                 if entry.startswith("app-")),
                key=natural_sort_key, reverse=True)
        except OSError:
            return None
        for version in versions:
            directory = os.path.join(root, version)
            executable = os.path.join(directory, "GitHubDesktop.exe")
            cli = os.path.join(directory, "resources", "app", "cli.js")
            if is_file(executable) and is_file(cli):
                return {
                    "launch": {"kind": "argv", "command": executable,
                               "args": [cli, "open"],
                               "env": {"ELECTRON_RUN_AS_NODE": "1"},
                               "windowsHide": True},
                    "icon": {"kind": "executable", "path": executable}}
        return None
    if kind == "desktop":
        entry = find_desktop_entry(locator["desktopId"], internals)
        if entry is None:
            return None
        launcher = desktop_launcher(entry, internals)
        if launcher is None:
            return None
        return {"launch": {"kind": "argv", "command": launcher,
                           "args": locator["args"]}}
    return _assert_never(locator)


def resolve_with_registry(app: Dict[str, Any], probe_timeout_ms: int,
                          registry: RegistryViewOnce,
                          internals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Resolve one entry against a pass-shared registry view: this
    platform's locators are tried in order and the first verified launcher
    wins."""
    platform_spec = spec_for(app, internals["platform"])
    if platform_spec is None:
        return None
    for locator in platform_spec["locators"]:
        found = locate(locator, probe_timeout_ms, registry, internals)
        if found is not None:
            return found
    return None


def resolve_launch(app: Dict[str, Any], probe_timeout_ms: int,
                   internals: Optional[Dict[str, Any]] = None
                   ) -> Optional[Dict[str, Any]]:
    """Resolve one catalog entry on this host; None during SSH launches or
    when the entry is not installed here."""
    resolved = resolve_internals(internals)
    if resolved["ssh"]:
        return None
    return resolve_with_registry(
        app, probe_timeout_ms,
        RegistryViewOnce(probe_timeout_ms, resolved), resolved)


def resolve_open_in_app_apps(probe_timeout_ms: int,
                             internals: Optional[Dict[str, Any]] = None
                             ) -> Dict[str, Dict[str, Any]]:
    """Resolve the whole catalog once: every entry's verified launcher on
    this host, in menu order, the registry read at most once per pass. The
    returned map is the mutable authority the caller owns. An SSH launch
    returns an empty map without probing."""
    resolved = resolve_internals(internals)
    if resolved["ssh"]:
        return {}
    registry = RegistryViewOnce(probe_timeout_ms, resolved)
    apps: Dict[str, Dict[str, Any]] = {}
    for app in OPEN_IN_APP_CATALOG:
        launch = resolve_with_registry(app, probe_timeout_ms, registry,
                                       resolved)
        if launch is not None:
            apps[app["id"]] = launch
    return apps


def launch_args(args: List[str], path: str) -> List[str]:
    """Substitute the directory token into one launch argv, appending the
    directory when no arg carries one."""
    if any(PATH_TOKEN in arg for arg in args):
        return [arg.replace(PATH_TOKEN, path) for arg in args]
    return [*args, path]


def is_missing_executable(error: BaseException) -> bool:
    """Whether a launch rejection names a missing executable (a stale
    resolution)."""
    return (isinstance(error, nc.NativeCommandError)
            and error.code == "ENOENT") or isinstance(error, FileNotFoundError)


def run_shell_open(path: str, watch_ms: int,
                   internals: Dict[str, Any]) -> str:
    """Open one directory through the OS shell's open verb under the launch
    watch window: the opener completing inside the window decides the
    outcome; an opener still running when it closes counts as launched and
    keeps running (its late settlement is swallowed because the request
    already answered)."""
    outcome: List[str] = []
    done = threading.Event()

    def opener() -> None:
        try:
            nc.open_native_path(path, internals={
                "platform": internals["platform"],
                "run": internals["run"],
                "env": internals["env"]})
            outcome.append("launched")
        except BaseException as error:  # noqa: BLE001 - classified below
            outcome.append("missing" if is_missing_executable(error)
                           else "failed")
        finally:
            done.set()

    worker = threading.Thread(target=opener, daemon=True)
    worker.start()
    if not done.wait(timeout=watch_ms / 1000.0):
        # still opening when the window closes: launched; the daemon
        # thread's late settlement is swallowed
        return "launched"
    return outcome[0]


def run_launch(launch: Dict[str, Any], path: str, watch_ms: int,
               internals: Dict[str, Any]) -> str:
    """Run one launcher and classify how the attempt ended."""
    kind = launch["kind"]
    if kind == "shell-open":
        return run_shell_open(path, watch_ms, internals)
    if kind == "argv":
        options: Dict[str, Any] = {"watchMs": watch_ms}
        if launch.get("env") is not None:
            options["env"] = launch["env"]
        if launch.get("windowsHide") is not None:
            options["windowsHide"] = launch["windowsHide"]
        try:
            internals["launch"](launch["command"],
                                launch_args(launch["args"], path), options)
            return "launched"
        except BaseException as error:  # noqa: BLE001 - classified
            return "missing" if is_missing_executable(error) else "failed"
    return _assert_never(launch)


def launch_resolved(resolved: Dict[str, Any], path: str, watch_ms: int,
                    internals: Optional[Dict[str, Any]] = None) -> str:
    """Launch one resolved application on a directory: the primary launcher,
    then the fallback when the primary fails inside the watch window.
    ``missing`` tells the caller to re-resolve once."""
    completed = resolve_internals(internals)
    primary = run_launch(resolved["launch"], path, watch_ms, completed)
    if primary == "launched" or resolved.get("fallbackLaunch") is None:
        return primary
    fallback = run_launch(resolved["fallbackLaunch"], path, watch_ms,
                          completed)
    if fallback == "launched":
        return "launched"
    return "missing" if primary == "missing" or fallback == "missing" \
        else "failed"


# -------------------------------------------------------------------- icons

#: The associated-icon extraction script. ``-File`` with positional args
#: keeps paths out of the command line's parsing (no quoting/escaping
#: surface); ExtractAssociatedIcon yields 32px, the most the stock .NET
#: surface gives without a native addon (Known Limitations).
EXTRACT_ICON_PS1 = "\n".join([
    "param([string]$Source, [string]$Target)",
    '$ErrorActionPreference = "Stop"',
    "Add-Type -AssemblyName System.Drawing",
    "$icon = [System.Drawing.Icon]::ExtractAssociatedIcon($Source)",
    "if ($null -eq $icon) { exit 1 }",
    "$bitmap = $icon.ToBitmap()",
    "$bitmap.Save($Target, [System.Drawing.Imaging.ImageFormat]::Png)",
    "",
])


def extract_bundle_icon_png(bundle_path: str, timeout_ms: int,
                            internals: Dict[str, Any]) -> Optional[bytes]:
    """Extract one bundle's icon as a 128px PNG: read CFBundleIconFile from
    Info.plist (plutil to JSON; the value may omit the .icns extension),
    fall back to the first Resources/*.icns, then convert with sips through
    a fresh temp file."""
    resources = os.path.join(bundle_path, "Contents", "Resources")
    icon_file: Optional[str] = None
    plist_json = output(
        "plutil",
        ["-convert", "json", "-o", "-",
         os.path.join(bundle_path, "Contents", "Info.plist")],
        timeout_ms, internals)
    if plist_json is not None:
        try:
            declared = json.loads(plist_json).get("CFBundleIconFile")
            if isinstance(declared, str) and declared != "":
                icon_file = declared if declared.endswith(".icns") \
                    else f"{declared}.icns"
        except (ValueError, AttributeError):
            pass
    if icon_file is None:
        try:
            icon_file = next((entry for entry in os.listdir(resources)
                              if entry.endswith(".icns")), None)
        except OSError:
            return None
    if icon_file is None:
        return None
    icns = os.path.join(resources, icon_file)
    if not os.path.exists(icns):
        return None
    work_dir = tempfile.mkdtemp(prefix="dsh-open-in-app-")
    try:
        out_png = os.path.join(work_dir, "icon.png")
        if output("sips", ["-s", "format", "png", "-Z", "128", icns,
                           "--out", out_png], timeout_ms, internals) is None:
            return None
        try:
            with open(out_png, "rb") as handle:
                return handle.read()
        except OSError:
            return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def extract_executable_icon_png(executable_path: str, timeout_ms: int,
                                internals: Dict[str, Any]) -> Optional[bytes]:
    """Extract one Windows executable's associated icon as a 32px PNG."""
    work_dir = tempfile.mkdtemp(prefix="dsh-open-in-app-")
    try:
        script = os.path.join(work_dir, "extract-icon.ps1")
        out_png = os.path.join(work_dir, "icon.png")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(EXTRACT_ICON_PS1)
        if output("powershell.exe",
                  ["-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                   "Bypass", "-File", script, executable_path, out_png],
                  timeout_ms, internals) is None:
            return None
        try:
            with open(out_png, "rb") as handle:
                return handle.read()
        except OSError:
            return None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def extract_linux_icon(desktop_id: str,
                       internals: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One Linux application's icon from its desktop entry's Icon= key —
    native_command's desktop_application_icon already answers the
    {bytes, contentType} icon shape (PNG or SVG)."""
    entry = find_desktop_entry(desktop_id, internals)
    icon = entry.get("icon") if entry is not None else None
    if icon is None or icon == "":
        return None
    return nc.desktop_application_icon(icon, xdg_data_directories(internals))


def extract_app_icon(app: Dict[str, Any], resolved: Dict[str, Any],
                     timeout_ms: int,
                     internals: Optional[Dict[str, Any]] = None
                     ) -> Optional[Dict[str, Any]]:
    """Extract one resolved application's icon on this host; None when this
    host serves none (the icon route answers 404 → generic glyph)."""
    completed = resolve_internals(internals)
    if completed["platform"] == "linux":
        platform_spec = spec_for(app, completed["platform"])
        desktop_id = platform_spec.get("desktopId") if platform_spec else None
        if desktop_id is None:
            return None
        return extract_linux_icon(desktop_id, completed)
    icon_source = resolved.get("icon")
    if icon_source is None:
        return None
    if icon_source["kind"] == "app-bundle":
        icon_bytes = extract_bundle_icon_png(icon_source["path"], timeout_ms,
                                             completed)
    else:
        icon_bytes = extract_executable_icon_png(icon_source["path"],
                                                 timeout_ms, completed)
    if icon_bytes is None:
        return None
    return {"bytes": icon_bytes, "contentType": "image/png"}


# ------------------------------------------------------------------- shared

#: GET route path serving the probed application ids.
OPEN_IN_APP_APPS_PATH = "/open-in-app/apps"
#: GET prefix path serving one PNG bundle icon per application id.
OPEN_IN_APP_ICON_PREFIX_PATH = "/open-in-app/icon"
#: POST route path launching one application on one workspace directory.
OPEN_IN_APP_OPEN_PATH = "/open-in-app/open"

#: Open-route request bodies are tiny JSON objects; anything larger is
#: hostile.
MAX_BODY_BYTES = 64 * 1024


def parse_open_body(text: str) -> Optional[Dict[str, str]]:
    """Validate one open-route body at the wire: JSON object with string
    app/path."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    app = body.get("app")
    path = body.get("path")
    if isinstance(app, str) and isinstance(path, str):
        return {"app": app, "path": path}
    return None


def validate_open_in_app_config(
        config: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """z.object of three boundedMs (number, step 1, min 1, max 6e5,
    required) natively: snake_case keys, loud on invalid."""
    config = dict(config or {})
    result: Dict[str, int] = {}
    for key in ("probe_timeout_ms", "icon_timeout_ms", "launch_watch_ms"):
        value = config.get(key)
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 1 or value > 600000):
            raise ValueError(f"{key} is required and must be an integer "
                             f"between 1 and 600000")
        result[key] = value
    return result


def _send_json(status: int, payload: Dict[str, Any]) -> Response:
    """JSON response (no-store: availability and launch outcomes are live
    facts)."""
    return (status,
            {"content-type": "application/json; charset=utf-8",
             "cache-control": "no-store"},
            json.dumps(payload, ensure_ascii=False))


def _send_method_not_allowed(allow: str) -> Response:
    """405 with the route's one supported method."""
    return 405, {"allow": allow}, b""


class OpenInAppService:
    """The host half: three routes serving the resolved application
    catalog, per-application icons, and the launch endpoint the browser
    split button posts to.

    Security has one home here: every route asks the composition's fence
    for a rejection first (upstream: the connection service's Host/Origin
    fence defeating DNS rebinding and cross-site calls plus the login-token
    cookie authentication). The catalog resolves lazily, once per service
    life, on the first request that needs it; a launch that finds its
    executable gone (ENOENT) invalidates that one entry and re-resolves it
    once.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 fence: Optional[Callable[[], Optional[int]]] = None,
                 internals: Optional[Dict[str, Any]] = None):
        self.config = validate_open_in_app_config(config)
        self._fence = fence
        self._internals = dict(internals or {})
        self._resolutions: Optional[Dict[str, Dict[str, Any]]] = None
        self._icons: Dict[str, Optional[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # -- facts

    def _catalog_internals(self) -> Dict[str, Any]:
        base = dict(self._internals)
        if "resolve_executable" not in base:
            base["resolve_executable"] = shutil.which
        return base

    def availability(self) -> Dict[str, Dict[str, Any]]:
        """Lazy once-per-service-life resolution; the map is the mutable
        authority."""
        with self._lock:
            if self._resolutions is None:
                self._resolutions = resolve_open_in_app_apps(
                    self.config["probe_timeout_ms"],
                    self._catalog_internals())
            return self._resolutions

    def _icon_of(self, app: Dict[str, Any],
                 resolved: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            if app["id"] in self._icons:
                return self._icons[app["id"]]
        icon = extract_app_icon(app, resolved, self.config["icon_timeout_ms"],
                                self._catalog_internals())
        with self._lock:
            self._icons[app["id"]] = icon
        return icon

    def refresh_resolution(self, app: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replace one stale resolution after a missing-executable launch:
        the entry (and its icon) re-resolves once; an entry that no longer
        resolves leaves the map and the next apps read no longer offers
        it."""
        apps = self.availability()
        fresh = resolve_launch(app, self.config["probe_timeout_ms"],
                               self._catalog_internals())
        with self._lock:
            self._icons.pop(app["id"], None)
            if fresh is None:
                apps.pop(app["id"], None)
                return None
            apps[app["id"]] = fresh
        return fresh

    def _rejected(self) -> Optional[Response]:
        if self._fence is None:
            return None
        rejection = self._fence()
        if rejection is None:
            return None
        return rejection, {}, b""

    # -- routes

    def handle_apps(self, method: str) -> Response:
        rejection = self._rejected()
        if rejection is not None:
            return rejection
        if method != "GET":
            return _send_method_not_allowed("GET")
        return _send_json(200, {"apps": list(self.availability().keys())})

    def handle_icon(self, method: str, app_id: str) -> Response:
        rejection = self._rejected()
        if rejection is not None:
            return rejection
        if method != "GET":
            return _send_method_not_allowed("GET")

        def no_icon() -> Response:
            return _send_json(404, {"code": "not-found",
                                    "message": f"no icon for {app_id}"})

        app = next((entry for entry in OPEN_IN_APP_CATALOG
                    if entry["id"] == app_id), None)
        if app is None:
            return no_icon()
        resolved = self.availability().get(app["id"])
        if resolved is None:
            return no_icon()
        icon = self._icon_of(app, resolved)
        if icon is None:
            return no_icon()
        return (200,
                {"content-type": icon["contentType"],
                 "cache-control": "public, max-age=3600"},
                icon["bytes"])

    def handle_open(self, method: str, content_type: str,
                    body: Optional[bytes]) -> Response:
        rejection = self._rejected()
        if rejection is not None:
            return rejection
        if method != "POST":
            return _send_method_not_allowed("POST")
        media = (content_type or "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            return _send_json(415, {
                "code": "unsupported-media-type",
                "message": "content-type must be application/json"})
        if body is None:
            return _send_json(400, {"code": "bad-request",
                                    "message": "request body unreadable"})
        if len(body) > MAX_BODY_BYTES:
            return _send_json(413, {"code": "payload-too-large",
                                    "message": "request body is too large"})
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return _send_json(400, {"code": "bad-request",
                                    "message": "request body unreadable"})
        parsed = parse_open_body(text)
        if parsed is None:
            return _send_json(400, {
                "code": "bad-request",
                "message": 'request body must be JSON with string "app" '
                           'and "path"'})
        app = next((entry for entry in OPEN_IN_APP_CATALOG
                    if entry["id"] == parsed["app"]), None)
        resolved = self.availability().get(app["id"]) if app else None
        if app is None or resolved is None:
            return _send_json(400, {
                "code": "bad-request",
                "message": f"unknown or unavailable app: {parsed['app']}"})
        if parsed["path"] == "" or not os.path.isabs(parsed["path"]):
            return _send_json(400, {
                "code": "bad-request",
                "message": "path must be an absolute directory path"})
        directory = is_directory(parsed["path"])
        if not directory:
            return _send_json(404, {
                "code": "not-found",
                "message": f"directory does not exist: {parsed['path']}"})
        watch_ms = self.config["launch_watch_ms"]
        outcome = launch_resolved(resolved, parsed["path"], watch_ms,
                                  self._catalog_internals())
        if outcome == "missing":
            fresh = self.refresh_resolution(app)
            outcome = ("failed" if fresh is None
                       else launch_resolved(fresh, parsed["path"], watch_ms,
                                            self._catalog_internals()))
        if outcome == "launched":
            return _send_json(200, {"ok": True})
        return _send_json(502, {
            "code": "launch-failed",
            "message": f"failed to launch {app['id']}"})


def sample_ssh_fact(env: Optional[Dict[str, str]] = None) -> bool:
    """The once-per-service-life SSH launch sample (upstream: apply() reads
    launchedThroughSsh(launchEnvironmentOf(ctx)) at plugin load)."""
    snapshot = create_launch_environment_snapshot(
        [LaunchEnvironmentLayer("process", dict(env if env is not None
                                                else os.environ))])
    return launched_through_ssh(snapshot)
