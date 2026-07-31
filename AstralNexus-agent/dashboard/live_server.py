"""HTTP/SSE boundary for the AstralNexus live swarm console."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
from typing import Any, Callable

from .state import LiveRunState


def _pointer_run_dir(pointer: Path) -> Path | None:
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        raw = data.get("run_dir") if isinstance(data, dict) else data
        if raw:
            candidate = Path(str(raw)).expanduser().resolve()
            if (candidate / "trace.jsonl").exists():
                return candidate
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def resolve_run_dir(home: Path, run_id: str | None = None) -> Path:
    """Resolve a run while honoring the active CLI run pointer."""
    home = Path(home).expanduser().resolve()
    if (home / "trace.jsonl").exists():
        return home
    pointer = _pointer_run_dir(home / "current_run.json")
    if pointer is not None and (not run_id or run_id == "latest"):
        return pointer
    run_roots: list[Path] = [
        home / "runs",
        home / ".astralnexus_deploy" / "runs",
    ]
    for ancestor in (Path.cwd(), *Path.cwd().resolve().parents):
        run_roots.extend((
            ancestor / "02_运行产物" / ".astralnexus_deploy" / "runs",
            ancestor / "02_运行产物" / "90_部署状态"
            / ".astralnexus_deploy" / "runs",
            ancestor / ".astralnexus_deploy" / "runs",
        ))
    unique_roots: list[Path] = []
    seen: set[Path] = set()
    for root in run_roots:
        root = root.resolve()
        if root not in seen:
            seen.add(root)
            unique_roots.append(root)
    if run_id and run_id != "latest":
        for runs in unique_roots:
            direct = runs / run_id
            if (direct / "trace.jsonl").exists():
                return direct
        raise FileNotFoundError(f"运行目录不存在或没有 trace.jsonl: {run_id}")
    candidates = [
        path
        for runs in unique_roots if runs.exists()
        for path in runs.glob("*")
        if path.is_dir() and (path / "trace.jsonl").exists()
    ]
    if not candidates:
        roots = ", ".join(str(root) for root in unique_roots[:4])
        raise FileNotFoundError(f"没有找到运行目录，已检查: {roots}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


class LiveDashboardServer:
    """Tail auditable run files and publish one consistent live projection."""

    def __init__(
        self,
        run_dir: Path,
        host: str = "127.0.0.1",
        port: int = 8787,
        pointer_path: Path | None = None,
    ):
        self.run_dir = Path(run_dir).resolve()
        self.host = host
        self.port = int(port)
        self.pointer_path = (
            Path(pointer_path).resolve() if pointer_path else None
        )
        self.state = LiveRunState(self.run_dir)
        self._subscribers: set[queue.Queue] = set()
        self._subscriber_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._app = self._build_app()

    def _load_trace(self) -> int:
        count = self._load_jsonl(
            self.run_dir / "trace.jsonl", self.state.apply
        )
        count += self._load_jsonl(
            self.run_dir / "runtime_events.jsonl", self.state.apply
        )
        count += self._load_jsonl(
            self.run_dir / "agent_lifecycle.jsonl",
            self.state.apply_lifecycle,
        )
        return count

    @staticmethod
    def _load_jsonl(path: Path, callback: Callable[[dict[str, Any]], None]) -> int:
        if not path.exists():
            return 0
        count = 0
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    callback(json.loads(line))
                    count += 1
                except json.JSONDecodeError:
                    continue
        return count

    def _tail_trace(self) -> None:
        paths = self._stream_paths()
        positions = {
            name: path.stat().st_size if path.exists() else 0
            for name, (path, _) in paths.items()
        }
        while not self._stop.wait(0.25):
            if self._switch_selected_run():
                paths = self._stream_paths()
                positions = {
                    name: path.stat().st_size if path.exists() else 0
                    for name, (path, _) in paths.items()
                }
                self._publish({
                    "event_type": "dashboard.snapshot",
                    "payload": self.state.snapshot(),
                })
                continue
            for name, (path, callback) in paths.items():
                positions[name] = self._tail_file(
                    path, positions.get(name, 0), callback
                )

    def _stream_paths(
        self,
    ) -> dict[str, tuple[Path, Callable[[dict[str, Any]], None]]]:
        return {
            "trace": (self.run_dir / "trace.jsonl", self.state.apply),
            "runtime": (
                self.run_dir / "runtime_events.jsonl",
                self.state.apply,
            ),
            "lifecycle": (
                self.run_dir / "agent_lifecycle.jsonl",
                self.state.apply_lifecycle,
            ),
        }

    def _switch_selected_run(self) -> bool:
        if not self.pointer_path:
            return False
        selected = _pointer_run_dir(self.pointer_path)
        if selected is None or selected == self.run_dir:
            return False
        self.run_dir = selected
        self.state = LiveRunState(selected)
        self._load_trace()
        return True

    def _tail_file(
        self,
        path: Path,
        position: int,
        callback: Callable[[dict[str, Any]], None],
    ) -> int:
        if not path.exists():
            return 0
        try:
            size = path.stat().st_size
            if size < position:
                position = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                while True:
                    line = handle.readline()
                    if not line:
                        break
                    position = handle.tell()
                    try:
                        raw_event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    callback(raw_event)
                    published = (
                        raw_event
                        if callback == self.state.apply
                        else {
                            "event_type": "agent.lifecycle.updated",
                            "payload": raw_event,
                        }
                    )
                    self._publish(published)
        except OSError:
            return position
        return position

    def _publish(self, event: dict[str, Any]) -> None:
        with self._subscriber_lock:
            for subscriber in list(self._subscribers):
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                        subscriber.put_nowait(event)
                    except queue.Empty:
                        pass

    def _build_app(self):
        from flask import Flask, Response, jsonify

        app = Flask(__name__)
        html_path = Path(__file__).with_name("live_dashboard.html")

        @app.get("/")
        def index():
            return html_path.read_text(encoding="utf-8")

        @app.get("/api/snapshot")
        def snapshot():
            return jsonify(self.state.snapshot())

        @app.get("/api/node/<node_id>")
        def node_output(node_id: str):
            nodes = self.state.snapshot()["nodes"]
            current = next(
                (item for item in nodes if item["id"] == node_id),
                {"id": node_id},
            )
            candidates = sorted(
                self.run_dir.rglob(f"*{node_id}*output.json"),
                key=lambda path: path.stat().st_mtime,
            )
            if candidates:
                try:
                    current["full_output"] = json.loads(
                        candidates[-1].read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    pass
            return jsonify(current)

        @app.get("/events")
        def events():
            subscriber: queue.Queue = queue.Queue(maxsize=500)
            with self._subscriber_lock:
                self._subscribers.add(subscriber)
            initial = {
                "event_type": "dashboard.snapshot",
                "payload": self.state.snapshot(),
            }

            def stream():
                try:
                    yield _sse(initial)
                    while not self._stop.is_set():
                        try:
                            yield _sse(subscriber.get(timeout=15))
                        except queue.Empty:
                            yield ": heartbeat\n\n"
                finally:
                    with self._subscriber_lock:
                        self._subscribers.discard(subscriber)

            return Response(
                stream(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        return app

    def start(self, *, blocking: bool = False) -> None:
        self._load_trace()
        self._thread = threading.Thread(
            target=self._tail_trace,
            name="astralnexus-dashboard-tail",
            daemon=True,
        )
        self._thread.start()
        if blocking:
            self._app.run(
                host=self.host,
                port=self.port,
                threaded=True,
                use_reloader=False,
            )
            return
        threading.Thread(
            target=lambda: self._app.run(
                host=self.host,
                port=self.port,
                threaded=True,
                use_reloader=False,
            ),
            name="astralnexus-dashboard-http",
            daemon=True,
        ).start()

    def stop(self) -> None:
        self._stop.set()


def _sse(event: dict[str, Any]) -> str:
    return (
        "event: trace\ndata: "
        + json.dumps(event, ensure_ascii=False, default=str)
        + "\n\n"
    )


def run_dashboard(
    home: Path,
    run_id: str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
) -> int:
    home = Path(home).expanduser().resolve()
    try:
        selected_run = resolve_run_dir(home, run_id)
    except FileNotFoundError:
        if run_id not in {None, "latest"}:
            raise
        selected_run = home / "dashboard_waiting"
        selected_run.mkdir(parents=True, exist_ok=True)
        (selected_run / "trace.jsonl").touch(exist_ok=True)
    server = LiveDashboardServer(
        selected_run,
        host=host,
        port=port,
        pointer_path=(
            home / "current_run.json"
            if not run_id or run_id == "latest"
            else None
        ),
    )
    print(f"AstralNexus 实时 Dashboard: http://{host}:{port}/")
    print(f"运行目录: {server.run_dir}")
    try:
        server.start(blocking=True)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="AstralNexus live swarm dashboard"
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    cli_args = parser.parse_args()
    raise SystemExit(
        run_dashboard(
            cli_args.home, cli_args.run_id, cli_args.host, cli_args.port
        )
    )
