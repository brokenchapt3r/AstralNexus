"""Contract tests for the wave-1 dshutil native ports (B1 util layer).

Each class names its upstream package; assertions follow the upstream
contract text (normal / failure / cancellation / recovery paths). The
cross-process lock test spawns a real subprocess with inherited stdio (no
pipes), so it runs inside the sandbox boundary.
"""
from __future__ import annotations

import base64
import gc
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid as uuid_module

import pytest

from AstralNexus.runtime.dshutil import atomic_write as aw
from AstralNexus.runtime.dshutil import chunked_list as cl
from AstralNexus.runtime.dshutil import crypto_util, home_paths, lazy_require
from AstralNexus.runtime.dshutil import launch_environment as le
from AstralNexus.runtime.dshutil import package_manifest as pm
from AstralNexus.runtime.dshutil import time_util, values
from AstralNexus.runtime.dshutil.brand import brand, brand_number, brand_string
from AstralNexus.runtime.dshutil.dsh_deque import Deque
from AstralNexus.runtime.dshutil.timeout import (
    Deadline, IdleWatchdog, MAX_TIMER_DELAY_MS, TimeoutReason, clamp_timeout, timeout_of)


# ------------------------------------------------------- dsh-atomic-write


class TestAtomicWrite:
    def test_write_new_and_replace_with_parents(self, tmp_path):
        target = tmp_path / "deep" / "dir" / "file.json"
        aw.write_file_atomic(target, '{"a": 1}')
        assert target.read_text(encoding="utf-8") == '{"a": 1}'
        aw.write_file_atomic(target, b'{"a": 2}')
        assert target.read_bytes() == b'{"a": 2}'
        assert list(target.parent.glob("*.tmp")) == []

    def test_failure_removes_temp_and_rethrows(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.mkdir()  # a directory cannot be replaced by a file
        with pytest.raises(OSError):
            aw.write_file_atomic(blocker, "x")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_lock_serializes_threads_and_leaves_no_lockfile(self, tmp_path):
        target = tmp_path / "counter.txt"
        target.write_text("0", encoding="utf-8")
        errors = []

        def bump():
            try:
                def operation():
                    current = int(target.read_text(encoding="utf-8"))
                    time.sleep(0.01)  # widen the read-modify-write race window
                    target.write_text(str(current + 1), encoding="utf-8")
                aw.with_file_lock(target, operation)
            except Exception as exc:  # pragma: no cover - failure evidence
                errors.append(exc)

        threads = [threading.Thread(target=bump) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert target.read_text(encoding="utf-8") == "6"
        assert not (tmp_path / "counter.txt.lock").exists()

    def test_lock_wait_times_out_with_named_error(self, tmp_path):
        target = tmp_path / "held.txt"
        holder_ready = threading.Event()
        release = threading.Event()

        def hold():
            with aw.file_lock(target, wait_ms=5000):
                holder_ready.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        assert holder_ready.wait(5)
        try:
            with pytest.raises(TimeoutError, match="timed out waiting for the writer lock"):
                aw.with_file_lock(target, lambda: None, wait_ms=80)
        finally:
            release.set()
            thread.join()

    def test_exited_holder_lock_is_taken_over(self, tmp_path, monkeypatch):
        target = tmp_path / "orphan.txt"
        lock = Path(str(target) + ".lock")
        dead_pid = _find_absent_pid()
        lock.write_text(f"{dead_pid}\n", encoding="utf-8")
        aw.with_file_lock(target, lambda: target.write_text("reclaimed", encoding="utf-8"), wait_ms=500)
        assert target.read_text(encoding="utf-8") == "reclaimed"
        assert not lock.exists()

    def test_live_holder_lock_is_never_taken_over(self, tmp_path):
        target = tmp_path / "live.txt"
        lock = Path(str(target) + ".lock")
        lock.write_text(f"{os.getpid()}\n", encoding="utf-8")  # our own live PID
        with pytest.raises(TimeoutError):
            aw.with_file_lock(target, lambda: None, wait_ms=60)
        assert lock.read_text(encoding="utf-8") == f"{os.getpid()}\n"
        lock.unlink()

    def test_malformed_lock_record_is_not_taken_over(self, tmp_path):
        target = tmp_path / "malformed.txt"
        lock = Path(str(target) + ".lock")
        lock.write_text("not-a-pid", encoding="utf-8")
        with pytest.raises(TimeoutError):
            aw.with_file_lock(target, lambda: None, wait_ms=60)
        lock.unlink()

    def test_cross_process_serialization_without_pipes(self, tmp_path):
        # Real two-process contention: stdio inherited/ignored (sandbox-safe),
        # coordination entirely through the lock file and a shared log file.
        target = tmp_path / "shared.txt"
        target.write_text("", encoding="utf-8")
        log = tmp_path / "log.txt"
        child = (
            "import sys, time\n"
            "sys.path.insert(0, sys.argv[3])\n"
            "from AstralNexus.runtime.dshutil import atomic_write as aw\n"
            "def op():\n"
            "    with open(sys.argv[1], 'a', encoding='utf-8') as f:\n"
            "        f.write(sys.argv[2] + '-enter\\n')\n"
            "        time.sleep(0.15)\n"
            "        f.write(sys.argv[2] + '-exit\\n')\n"
            "aw.with_file_lock(sys.argv[4], op, wait_ms=8000)\n"
        )
        script = tmp_path / "child.py"
        script.write_text(child, encoding="utf-8")
        package_root = str(Path(str(__import__('AstralNexus').__file__)).resolve().parent.parent)
        procs = [subprocess.Popen(
            [sys.executable, "-X", "utf8", str(script), str(log), name, package_root, str(target)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for name in ("A", "B", "C")]
        assert all(proc.wait(timeout=30) == 0 for proc in procs)
        lines = log.read_text(encoding="utf-8").split()
        assert len(lines) == 6
        # No interleaving: each enter is immediately followed by its own exit.
        for index in range(0, 6, 2):
            assert lines[index].endswith("-enter") and lines[index + 1].endswith("-exit")
            assert lines[index].split("-")[0] == lines[index + 1].split("-")[0]


def _find_absent_pid() -> int:
    import psutil  # type: ignore[import-untyped]
    candidate = 4242424
    while psutil.pid_exists(candidate):
        candidate += 1
    return candidate


# ------------------------------------------------------------ dsh-timeout


class TestTimeout:
    def test_clamp_bounds(self):
        assert clamp_timeout(None, 300, 60000) == 300
        assert clamp_timeout(600000, 300, 60000) == 60000
        assert clamp_timeout(250, 300, 60000) == 250
        with pytest.raises(ValueError):
            clamp_timeout(-1, 300, 60000)
        with pytest.raises(ValueError):
            clamp_timeout(math.nan, 300, 60000)
        with pytest.raises(ValueError):
            clamp_timeout(100, 0, 60000)

    def test_timer_delay_ceiling_enforced(self):
        assert MAX_TIMER_DELAY_MS == 2147483647
        with pytest.raises(ValueError, match=r"timeout_ms must be within"):
            Deadline(None, MAX_TIMER_DELAY_MS + 1)

    def test_deadline_expires_with_reason(self):
        with Deadline(None, 60) as deadline:
            assert not deadline.expired
            assert deadline.wait(2) is True
            assert deadline.expired
            reason = timeout_of(deadline)
            assert isinstance(reason, TimeoutReason) and reason.code == "timeout"
            assert timeout_of(deadline, "other-code") is None

    def test_deadline_upstream_cancel_has_no_timeout_reason(self):
        upstream = threading.Event()
        deadline = Deadline(upstream, 30000)
        upstream.set()
        assert deadline.expired
        assert deadline.reason is None
        deadline.close()

    def test_deadline_close_cancels_timer(self):
        deadline = Deadline(None, 50)
        deadline.close()
        time.sleep(0.12)
        assert not deadline.expired

    def test_idle_watchdog_arms_per_call_and_clears(self):
        watchdog = IdleWatchdog(None, 200)
        assert watchdog.next(lambda: "fast") == "fast"
        assert not watchdog.expired and watchdog.reason is None
        result = watchdog.next(lambda: (time.sleep(0.35), "slow")[1])
        assert result == "slow"
        # Upstream clear(): the per-call signal does not outlive the call.
        assert watchdog.reason is None
        watchdog.close()

    def test_idle_watchdog_refuses_double_outstanding_and_disposal(self):
        watchdog = IdleWatchdog(None, 1000)
        errors = []

        def nested():
            try:
                watchdog.next(lambda: None)
            except RuntimeError as exc:
                errors.append(str(exc))

        watchdog.next(nested)
        assert errors == ["idleWatchdog next is already outstanding"]
        watchdog.close()
        with pytest.raises(RuntimeError, match="disposed"):
            watchdog.next(lambda: None)
        with pytest.raises(RuntimeError, match="disposed"):
            watchdog.pulse()

    def test_timeout_reason_message_and_validation(self):
        reason = TimeoutReason("read_idle", 1500)
        assert str(reason) == "read_idle after 1500ms"
        with pytest.raises(ValueError):
            TimeoutReason("", 100)
        with pytest.raises(ValueError):
            TimeoutReason("x", 0)


# ----------------------------------------------------------- dsh-util-time


class TestTimeUtil:
    @pytest.mark.parametrize("value", ["Asia/Shanghai", "America/New_York", "Europe/London"])
    def test_canonical_zones_pass_through(self, value):
        assert time_util.canonical_client_time_zone(value) == value

    def test_utc_special_case_and_rejections(self):
        assert time_util.canonical_client_time_zone("UTC") == "UTC"
        for bad in ("", "   ", " Asia/Shanghai", "Asia/Shanghai ", "asia/", "Asia/",
                    "Asia/Invalid_Zone_Name_XYZ", "GMT+08:00", "NoSlash", None, 42,
                    ["Asia/Shanghai"]):
            assert time_util.canonical_client_time_zone(bad) is None, bad

    def test_alias_documented_deviation(self):
        # Documented Python deviation: aliases resolve but canonicalize to
        # themselves (upstream ICU maps US/Eastern → America/New_York; the pip
        # tzdata zones list contains aliases, so no offline canonical test).
        assert time_util.canonical_client_time_zone("US/Eastern") == "US/Eastern"
        assert time_util.canonical_client_time_zone("America/New_York") == "America/New_York"


# ------------------------------------------------------------- dsh-deque


class TestDeque:
    def test_fifo_and_front_insertion(self):
        deque = Deque()
        for value in range(5):
            deque.push_back(value)
        deque.push_front(-1)
        assert deque.size == 6
        assert [deque.pop_front() for _ in range(6)] == [-1, 0, 1, 2, 3, 4]
        assert deque.pop_front() is None

    def test_growth_keeps_order_and_compaction_bounds_vacant_storage(self):
        deque = Deque()
        for value in range(100):
            deque.push_back(value)
        assert deque.size == 100 and len(deque._buffer) >= 100
        for expected in range(96):
            assert deque.pop_front() == expected
        # Compacted to at most a quarter, never below MIN_CAPACITY.
        assert len(deque._buffer) <= 128 and len(deque._buffer) >= 16
        assert [deque.pop_front() for _ in range(4)] == [96, 97, 98, 99]
        assert deque.pop_front() is None

    def test_popped_slot_releases_reference_and_clear_frees(self):
        deque = Deque()
        marker = object()
        deque.push_back(marker)
        deque.push_back("second")
        assert deque.pop_front() is marker
        assert deque._buffer[(deque._head - 1) % len(deque._buffer)] is None
        deque.clear()
        assert deque.size == 0 and deque._buffer == []


# ------------------------------------------------------- dsh-chunked-list


class TestChunkedList:
    def test_append_is_persistent_and_snapshots_stable(self):
        head = cl.append_chunked_list(None, "a")
        snapshot = head
        head = cl.append_chunked_list(head, "b")
        assert list(cl.iterate_chunked_list(snapshot)) == ["a"]
        assert list(cl.iterate_chunked_list(head)) == ["a", "b"]

    def test_capacity_boundary_splits_chunks(self):
        head = None
        for index in range(65):
            head = cl.append_chunked_list(head, index)
        assert list(cl.iterate_chunked_list(head)) == list(range(65))
        assert len(head.values) == 1 and len(head.previous.values) == 64

    def test_checkpoint_roundtrip_and_validation(self):
        head = None
        for index in range(70):
            head = cl.append_chunked_list(head, index)
        data = cl.chunked_list_to_json(head)
        restored = cl.chunked_list_from_json(data)
        assert list(cl.iterate_chunked_list(restored)) == list(range(70))
        cl.validate_chunked_list(data, value_validator=lambda v: isinstance(v, int))
        with pytest.raises(ValueError, match="non-empty"):
            cl.validate_chunked_list({"values": []})
        with pytest.raises(ValueError, match="unknown fields"):
            cl.validate_chunked_list({"values": [1], "surprise": True})
        with pytest.raises(ValueError, match="capacity"):
            cl.validate_chunked_list({"values": list(range(65))})
        with pytest.raises(ValueError, match="caller validator"):
            cl.validate_chunked_list({"values": ["x"]}, value_validator=lambda v: isinstance(v, int))
        with pytest.raises(ValueError, match="non-empty chunked list"):
            cl.validate_chunked_list(None)


# --------------------------------------------------------- dsh-home-paths


class TestHomePaths:
    def test_resolution_precedence_and_blank_env_is_unset(self, monkeypatch):
        monkeypatch.delenv("DSH_HOME", raising=False)
        default = home_paths.resolve_dsh_home(env={})
        assert default == os.path.abspath(home_paths.default_dsh_home())
        assert home_paths.resolve_dsh_home(env={"DSH_HOME": "   "}) == default
        env_home = os.path.abspath(str(Path("/x") / "envhome"))
        assert home_paths.resolve_dsh_home(env={"DSH_HOME": str(Path("/x") / "envhome")}) == env_home
        assert home_paths.resolve_dsh_home("configured", env={"DSH_HOME": "envhome"}) == os.path.abspath("configured")

    def test_tilde_expansion_supported_prefixes_only(self):
        home = os.path.expanduser("~")
        assert home_paths.expand_home_path("~") == home
        assert home_paths.expand_home_path("~/x") == os.path.join(home, "x")
        assert home_paths.expand_home_path("~\\y") == os.path.join(home, "y")
        assert home_paths.expand_home_path("~user/x") == "~user/x"
        assert home_paths.expand_home_path("/abs") == "/abs"

    def test_home_and_cache_joining_and_display(self, monkeypatch):
        monkeypatch.delenv("DSH_HOME", raising=False)
        home = home_paths.resolve_dsh_home()
        assert home_paths.dsh_home_path("plugins", "x") == os.path.join(home, "plugins", "x")
        assert home_paths.dsh_cache_path() == os.path.join(home, "cache")
        assert home_paths.dsh_cache_path("seg") == os.path.join(home, "cache", "seg")
        assert home_paths.dsh_cache_path({"dsh_home": "cfg"}, "seg") == os.path.join(
            os.path.abspath("cfg"), "cache", "seg")
        assert home_paths.dsh_home_display(home_paths.default_dsh_home()) == "~/.dsh"
        assert home_paths.dsh_home_display(str(Path("/somewhere/else"))) == "$DSH_HOME"

    def test_canonicalize_watch_path(self, tmp_path):
        existing = (tmp_path / "watch").resolve()
        existing.mkdir()
        assert home_paths.canonicalize_watch_path(existing) == str(existing)
        missing = existing / "not" / "yet"
        assert home_paths.canonicalize_watch_path(missing) == str(existing / "not" / "yet")
        regular_file = tmp_path / "file.txt"
        regular_file.write_text("x", encoding="utf-8")
        with pytest.raises(OSError):
            home_paths.canonicalize_watch_path(regular_file / "under-file")


# --------------------------------------------------------- dsh-util-values


class TestValues:
    def test_assert_never(self):
        with pytest.raises(RuntimeError, match=r"unexpected value in switch: 3"):
            values.assert_never(3, "switch")
        with pytest.raises(RuntimeError, match="unexpected value:"):
            values.assert_never(object())

    def test_json_value_accepts_lossless_and_rejects_the_rest(self):
        assert values.is_json_value({"a": [1, "x", True, None, 2.5], "b": {"c": []}})
        for bad in (math.nan, math.inf, -0.0, (1, 2), {1: "x"}, object(), b"bytes"):
            assert values.is_json_value(bad) is False, bad
        cyclic = {}
        cyclic["self"] = cyclic
        assert values.is_json_value(cyclic) is False
        deep_list = []
        deep_list.append(deep_list)
        assert values.is_json_value(deep_list) is False

    def test_snapshot_is_isolated_and_honest(self):
        source = {"list": [1, {"nested": True}], "n": None}
        snapshot = values.snapshot_json_value(source)
        assert snapshot == source and snapshot is not source
        assert snapshot["list"] is not source["list"]
        source["list"][1]["nested"] = "mutated"
        assert snapshot["list"][1]["nested"] is True
        assert values.snapshot_json_value(math.nan) is None
        cyclic = []
        cyclic.append(cyclic)
        assert values.snapshot_json_value(cyclic) is None

    def test_deep_equal_json(self):
        assert values.deep_equal_json({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1})
        assert not values.deep_equal_json({"a": True}, {"a": 1})
        assert not values.deep_equal_json({"a": 1}, {"a": 1, "b": None})
        assert values.deep_equal_json([1, 2.0], [1, 2])
        assert not values.deep_equal_json(None, False)

    def test_deep_freeze_projection(self):
        frozen = values.deep_freeze({"a": [1, {"b": 2}]})
        with pytest.raises(TypeError):
            frozen["a"] = []  # type: ignore[index]
        with pytest.raises(AttributeError):
            frozen["a"].append(1)  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            frozen["a"][1]["b"] = 3  # type: ignore[index]
        shared = {"inner": [1]}
        frozen2 = values.deep_freeze({"x": shared, "y": shared})
        assert frozen2["x"] is frozen2["y"]  # sharing preserved
        cyclic = {}
        cyclic["self"] = cyclic
        with pytest.raises(ValueError, match="cyclic"):
            values.deep_freeze(cyclic)
        with pytest.raises(ValueError, match="not a JSON-lossless"):
            values.deep_freeze(object())

    def test_weak_map_with_values(self):
        class Key:
            pass

        store = values.WeakMapWithValues()
        first, second = Key(), Key()
        store.set(first, "v1")
        store.set(second, "v2")
        assert store.get(first) == "v1" and store.has(second)
        assert store.values == ["v1", "v2"]
        store.set(first, "v3")  # replacement drops the old value; new value goes last (Set semantics)
        assert store.values == ["v2", "v3"]
        assert store.delete(second) and not store.delete(second)
        assert store.values == ["v3"]
        del first
        gc.collect()
        assert store.values == []
        store.set(Key(), "ephemeral")
        store.clear()
        assert store.values == []


# ------------------------------------------------------------ dsh-brand


class TestBrand:
    def test_runtime_boundary_checks(self):
        assert brand_string("ok") == "ok"
        with pytest.raises(TypeError):
            brand_string(42)
        assert brand_number(3) == 3 and brand_number(2.5) == 2.5
        with pytest.raises(TypeError):
            brand_number(True)

    def test_brand_factory_creates_newtype(self):
        TaskId = brand("TaskId")
        assert TaskId.__supertype__ is str
        with pytest.raises(ValueError):
            brand("not an identifier")
        with pytest.raises(ValueError):
            brand("Weird", base=dict)


# -------------------------------------------------------- dsh-util-crypto


class TestCryptoUtil:
    def test_base64_vectors_and_type_gate(self):
        assert crypto_util.bytes_to_base64(b"hello") == base64.b64encode(b"hello").decode() == "aGVsbG8="
        assert crypto_util.bytes_to_base64(bytearray(b"\x00\xff")) == crypto_util.bytes_to_base64(b"\x00\xff")
        with pytest.raises(TypeError):
            crypto_util.bytes_to_base64("already text")  # type: ignore[arg-type]

    def test_random_uuid_is_v4_csprng_shape(self):
        seen = {crypto_util.random_uuid() for _ in range(50)}
        assert len(seen) == 50
        pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        for value in seen:
            assert pattern.match(value), value
            assert uuid_module.UUID(value).version == 4


# -------------------------------------------------------- dsh-lazy-require


class TestLazyRequire:
    def test_success_cached_failure_not_cached(self):
        load = lazy_require.create_lazy_import("json.decoder")
        first = load()
        assert load() is first
        missing = lazy_require.create_lazy_import("no_such_module_xyz_42")
        with pytest.raises(ModuleNotFoundError):
            missing()
        with pytest.raises(ModuleNotFoundError):  # retried, not poisoned
            missing()
        with pytest.raises(ValueError):
            lazy_require.create_lazy_import("  ")


# -------------------------------------------------- dsh-launch-environment


class TestLaunchEnvironment:
    def _snapshot(self):
        return le.create_launch_environment_snapshot([
            {"source": "user-env", "values": {"ONLY_USER": "u", "SHARED": "user"}, "path": "/u/.env"},
            {"source": "project-env", "values": {"SHARED": "project", "ONLY_PROJECT": "p"}},
            {"source": "process", "values": {"SHARED": "process", "ONLY_PROCESS": "proc", "SSH_TTY": ""}},
        ])

    def test_first_layer_in_order_wins_and_reports_source(self):
        snapshot = self._snapshot()
        resolution = snapshot.get("SHARED")
        assert resolution.value == "process" and resolution.source == "process"
        assert snapshot.get("ONLY_USER").source == "user-env"
        assert snapshot.get("ONLY_USER").path == "/u/.env"
        assert snapshot.get("ABSENT") is None
        assert snapshot.get_from("SHARED", ["user-env", "process"]).value == "user"

    def test_windows_case_insensitive_single_resolution(self):
        snapshot = le.create_launch_environment_snapshot([
            {"source": "process", "values": {"Path": "one", "PATH": "two"}}])
        assert snapshot.get("path").value == "one"  # first spelling wins
        assert snapshot.get("PATH").value == "one"

    def test_ssh_only_from_process_layer(self):
        assert le.launched_through_ssh(self._snapshot()) is False  # empty marker
        ssh_process = le.create_launch_environment_snapshot([
            {"source": "process", "values": {"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}}])
        assert le.launched_through_ssh(ssh_process) is True
        ssh_env_file = le.create_launch_environment_snapshot([
            {"source": "project-env", "values": {"SSH_TTY": "/dev/pts/0"}}])
        assert le.launched_through_ssh(ssh_env_file) is False

    def test_launch_environment_of_fallback(self):
        assert le.launch_environment_of({}).get("PATH") is not None or sys.platform != "win32"
        registered = self._snapshot()
        assert le.launch_environment_of({le.DSH_LAUNCH_ENVIRONMENT_KEY: registered}) is registered
        with pytest.raises(ValueError):
            le.LaunchEnvironmentLayer(source="mystery", values={})


# ---------------------------------------------------- dsh-package-manifest


class TestPackageManifest:
    FULL = {
        "name": "example-dsh-plugin", "version": "1.0.0",
        "engines": {"node": ">=24", "dsh": "0.1.5-alpha.1"},
        "dsh": {"manifestVersion": 1, "bundle": {"patch": "./cordis.patch.yml"},
                "client": {"platform": "web"}, "title": {"en": "Example", "zh": "示例"}},
        "private_extra": True,
    }

    def test_full_manifest_parses_declared_fields(self):
        parsed = pm.parse_dsh_package_manifest(self.FULL)
        assert parsed.name == "example-dsh-plugin" and parsed.version == "1.0.0"
        assert parsed.engines.dsh == "0.1.5-alpha.1" and parsed.engines.npm is None
        assert parsed.dsh.manifest_version == 1
        assert parsed.dsh.bundle.patch_paths() == ["./cordis.patch.yml"]
        assert parsed.dsh.client.platform == "web"
        assert parsed.dsh.title == {"en": "Example", "zh": "示例"}
        assert parsed.extra == {"private_extra": True}

    def test_patch_list_keeps_order(self):
        data = {**self.FULL, "dsh": {**self.FULL["dsh"], "bundle": {"patch": ["a.yml", "b.yml"]}}}
        assert pm.parse_dsh_package_manifest(data).dsh.bundle.patch_paths() == ["a.yml", "b.yml"]

    def test_failures_are_loud(self):
        with pytest.raises(ValueError, match="name is required"):
            pm.parse_dsh_package_manifest({"version": "1.0.0"})
        assert pm.parse_dsh_package_manifest({"version": "1.0.0"}, partial=True).name is None
        with pytest.raises(ValueError, match="manifestVersion"):
            pm.parse_dsh_package_manifest({**self.FULL, "dsh": {**self.FULL["dsh"], "manifestVersion": 2}})
        with pytest.raises(ValueError, match="English fallback"):
            pm.parse_dsh_package_manifest({**self.FULL, "dsh": {**self.FULL["dsh"], "title": {"zh": "示例"}}})
        with pytest.raises(ValueError, match="unknown fields"):
            pm.parse_dsh_package_manifest({**self.FULL, "engines": {"dsh": "1", "cargo": "2"}})
        with pytest.raises(ValueError, match="ordered list of paths"):
            pm.parse_dsh_package_manifest({**self.FULL, "dsh": {**self.FULL["dsh"], "bundle": {"patch": []}}})
        with pytest.raises(ValueError, match="must be an object"):
            pm.parse_dsh_package_manifest(["not", "an", "object"])
