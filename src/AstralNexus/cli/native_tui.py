"""Launch the AstralNexus Ink frontend against the AstralNexus gateway."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


_VISIBLE_VIEWPORT_CLEAR = "\x1b[H\x1b[2J\x1b[3J\x1b[H"


def _clear_visible_viewport() -> None:
    """Remove an interrupted inline TUI from the screen and scrollback."""
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(_VISIBLE_VIEWPORT_CLEAR)
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def source_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / 'tui' / 'package.json').is_file():
            return parent
    return Path(__file__).resolve().parents[2]


def bundle_path() -> Path:
    source = source_root() / 'tui' / 'dist' / 'entry.js'
    packaged = Path(__file__).resolve().parents[1] / 'tui_assets' / 'entry.mjs'
    return source if source.is_file() or not packaged.is_file() else packaged


def _tui_source_paths() -> list[Path]:
    tui_root = source_root() / "tui"
    paths = [tui_root / "package.json", tui_root / "scripts" / "build.mjs"]
    for directory in (tui_root / "src", tui_root / "packages" / "astralnexus-ink" / "src"):
        if directory.is_dir():
            paths.extend(
                path for path in directory.rglob("*")
                if path.is_file() and path.suffix in {".ts", ".tsx", ".js", ".mjs"}
            )
    return [path for path in paths if path.is_file()]


def bundle_is_stale() -> bool:
    """Return whether the sole Ink bundle is missing or older than its sources."""
    bundle = bundle_path()
    if not bundle.is_file():
        return True
    built_at = bundle.stat().st_mtime_ns
    return any(path.stat().st_mtime_ns > built_at for path in _tui_source_paths())


def ensure_bundle_current(node: str) -> None:
    """Build the canonical TUI when source changed instead of running stale UI."""
    if not bundle_is_stale():
        return
    tui_root = source_root() / "tui"
    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
    if not npm:
        raise RuntimeError("天枢 TUI 源码已更新，但未找到 npm，无法重建唯一 Ink 界面")
    completed = subprocess.run(
        [npm, "run", "build"],
        cwd=str(tui_root),
        env={**os.environ, "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", "")},
        check=False,
    )
    if completed.returncode or not bundle_path().is_file():
        raise RuntimeError("天枢唯一 Ink TUI 构建失败；请在 tui 目录执行 npm run build")


def resolve_node_executable() -> str | None:
    """Resolve Node from AstralNexus configuration or the system PATH."""
    for env_name in ("ASTRALNEXUS_NODE",):
        configured = os.environ.get(env_name, "").strip()
        if configured and Path(configured).is_file():
            return str(Path(configured).resolve())

    return shutil.which("node")


def native_tui_available() -> bool:
    if os.environ.get('ASTRALNEXUS_TUI_SCREEN_READER', '').lower() in {'1', 'true', 'yes'}:
        return True
    return bool(resolve_node_executable() and bundle_path().is_file())


def launch_native_tui() -> int:
    """Run the AstralNexus Ink TUI in the current terminal."""
    if os.environ.get('ASTRALNEXUS_TUI_SCREEN_READER', '').lower() in {'1', 'true', 'yes'}:
        from AstralNexus.tui_gateway.plain import run_plain
        return run_plain()
    node = resolve_node_executable()
    bundle = bundle_path()
    if not node:
        raise RuntimeError("Node.js 20+ 未安装，无法启动原生 Ink TUI")
    ensure_bundle_current(node)

    env = os.environ.copy()
    env["ASTRALNEXUS_TUI_GATEWAY_MODULE"] = "AstralNexus.tui_gateway.entry"
    env["ASTRALNEXUS_PYTHON_SRC_ROOT"] = str(Path(__file__).resolve().parents[2])
    env["ASTRALNEXUS_CWD"] = str(Path.cwd().resolve())
    # The interactive PowerShell entry runs in Ink's supported inline mode.
    # This keeps the terminal primary buffer, native scrollback, mouse-wheel
    # paging, and host text selection, matching the actual `hermes` CLI users
    # interact with. The web dashboard has its own viewport and does not use
    # this launcher.
    env["ASTRALNEXUS_TUI_INLINE"] = "1"
    env["ASTRALNEXUS_TUI_DISABLE_MOUSE"] = "1"
    env["ASTRALNEXUS_TUI_MOUSE_TRACKING"] = "off"
    env.setdefault("ASTRALNEXUS_PYTHON", sys.executable)
    env.setdefault("NODE_ENV", "production")
    node_dir = str(Path(node).parent)
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [node, "--expose-gc", str(bundle)],
            cwd=str(source_root()),
            env=env,
            check=False,
        )
        return int(completed.returncode)
    finally:
        # Node performs the same cleanup on orderly shutdown. This fallback
        # also covers Ctrl+C interrupting subprocess.run or a hard child exit.
        _clear_visible_viewport()
