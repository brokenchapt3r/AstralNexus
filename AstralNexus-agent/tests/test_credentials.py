"""Contract tests for the native credentials family (base/local/anonymous) and
its real PluginHost + WorkbenchHost integration (B1 credentials wave)."""
from __future__ import annotations

import os
import sys
import textwrap
import threading
import time
import uuid as uuid_module
from pathlib import Path

import pytest

from AstralNexus.runtime.credentials import anonymous_user_id as anon
from AstralNexus.runtime.credentials.base import (
    CredentialProvider, credential_key, credential_key_id, credential_key_scope,
    credential_ref, is_credential_key_segment, is_credential_ref_name, parse_credential_key)
from AstralNexus.runtime.credentials.local import (
    CREDENTIALS_FILENAME, CredentialsDocumentError, LocalCredentialProvider,
    assert_storable_api_key, describe_yaml_error, parse_credentials_document,
    render_flat_layout_migration, render_record, render_ref, resolve_spec, validate_config)
from AstralNexus.runtime.dshutil.launch_environment import (
    LaunchEnvironmentLayer, create_launch_environment_snapshot)


# ------------------------------------------------------------------ grammar


class TestCredentialGrammar:
    def test_ref_grammar(self):
        assert credential_ref("DEEPSEEK_API_KEY") == "DEEPSEEK_API_KEY"
        assert credential_ref("_x9") == "_x9"
        assert is_credential_ref_name("A_B")
        for bad in ("9A", "a-b", "a/b", "", "a b", None, 42):
            assert not is_credential_ref_name(bad), bad
            with pytest.raises(TypeError, match="must match"):
                credential_ref(bad)

    def test_key_grammar_and_roundtrip(self):
        key = credential_key("llm-pi-ai", "openai-codex")
        assert key == "llm-pi-ai/openai-codex"
        assert parse_credential_key(key) == key
        assert credential_key_scope(key) == "llm-pi-ai"
        assert credential_key_id(key) == "openai-codex"
        assert is_credential_key_segment("a-9")
        for bad in ("Llm", "9a", "a_b", "", None):
            assert not is_credential_key_segment(bad), bad
        with pytest.raises(TypeError, match="key segment"):
            credential_key("OK", "Bad")
        for bad in ("scope", "a/b/c", "", None, 42):
            with pytest.raises(TypeError, match='must be "<scope>/<id>"'):
                parse_credential_key(bad)
        # The "/" between segments is what keeps a key out of the ref grammar.
        assert not is_credential_ref_name(key)


class _InvariantError(RuntimeError):
    code = "INVARIANT"


class TestProviderFanOut:
    def test_contained_failures_and_invariant_rethrow(self):
        provider = CredentialProvider()
        seen = []

        def good(subject):
            seen.append(subject)

        def broken(subject):
            raise RuntimeError("observer exploded")

        def invariant(subject):
            raise _InvariantError("structure violated")

        provider.on("credentials/reference-updated", broken)
        provider.on("credentials/reference-updated", invariant)
        provider.on("credentials/reference-updated", good)
        with pytest.raises(_InvariantError):
            provider.notify_updated("REF")
        assert seen == ["REF"]  # every listener ran despite the failures

    def test_unsubscribe_idempotent_and_registration_order(self):
        provider = CredentialProvider()
        seen = []
        unwatch = provider.on("credentials/record-updated", lambda key: seen.append(("a", key)))
        provider.on("credentials/record-updated", lambda key: seen.append(("b", key)))
        provider.notify_record_updated("s/i")
        unwatch()
        unwatch()  # idempotent
        provider.notify_record_updated("s/i")
        assert seen == [("a", "s/i"), ("b", "s/i"), ("b", "s/i")]

    def test_awaitable_listener_is_closed_not_awaited(self):
        provider = CredentialProvider()
        closed = []

        async def async_listener(subject):
            closed.append("ran")

        provider.on("credentials/reference-updated", async_listener)
        provider.notify_updated("REF")  # must not raise, must not hang
        assert closed == []

    def test_abstract_seam_methods_refuse(self):
        provider = CredentialProvider()
        for call in (lambda: provider.resolve("A"), lambda: provider.describe("A"),
                     lambda: provider.set("A", "v"), lambda: provider.unset("A"),
                     lambda: provider.read_record("s/i"), lambda: provider.describe_record("s/i"),
                     lambda: provider.list_records(), lambda: provider.modify_record("s/i", lambda c: c),
                     lambda: provider.delete_record("s/i")):
            with pytest.raises(NotImplementedError):
                call()


# -------------------------------------------------------- anonymous user id


@pytest.fixture(autouse=True)
def _clear_anonymous_memo():
    anon._memo.clear()
    yield
    anon._memo.clear()


class TestAnonymousUserId:
    def test_creates_persists_and_memoizes(self, tmp_path):
        home = str(tmp_path)
        first = anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": home}})
        assert anon.UUID_PATTERN.match(first)
        persisted = (tmp_path / anon.ANONYMOUS_USER_ID_FILE_NAME).read_text(encoding="utf-8")
        assert persisted == f"{first}\n"  # a bare UUID line, no wrapper format
        (tmp_path / anon.ANONYMOUS_USER_ID_FILE_NAME).unlink()
        # Memoized per resolved path: a file deleted mid-run keeps the id.
        assert anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": home}}) == first

    def test_scoped_to_home_not_machine(self, tmp_path):
        home_a, home_b = str(tmp_path / "a"), str(tmp_path / "b")
        id_a = anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": home_a}})
        id_b = anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": home_b}})
        assert id_a != id_b  # distinct homes never share an id

    def test_stable_across_calls_and_random_source(self, tmp_path):
        home = str(tmp_path)
        fixed = str(uuid_module.UUID(int=7))
        id_a = anon.get_or_create_anonymous_user_id(
            {"env": {"DSH_HOME": home}, "random_uuid": lambda: fixed})
        assert id_a == fixed
        assert anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": home}}) == fixed

    def test_corrupt_file_mints_fresh_identity(self, tmp_path):
        target = tmp_path / anon.ANONYMOUS_USER_ID_FILE_NAME
        target.write_text("not-a-uuid\n", encoding="utf-8")
        fresh = anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": str(tmp_path)}})
        assert anon.UUID_PATTERN.match(fresh)
        assert target.read_text(encoding="utf-8") == f"{fresh}\n"

    def test_concurrent_first_launch_settles_on_one_id(self, tmp_path):
        home = str(tmp_path)
        results = []
        barrier = threading.Barrier(4)

        def worker():
            anon._memo.clear()
            barrier.wait()
            results.append(anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": home}}))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        persisted = (tmp_path / anon.ANONYMOUS_USER_ID_FILE_NAME).read_text(encoding="utf-8").strip()
        assert anon.UUID_PATTERN.match(persisted)
        # The exclusive-create race settles on the persisted id for every loser.
        assert all(value == persisted for value in results)

    def test_read_only_home_still_returns_usable_id(self, tmp_path, monkeypatch):
        home = str(tmp_path)
        calls = []

        def failing_open(*args, **kwargs):
            calls.append(1)
            raise PermissionError("read-only home")

        monkeypatch.setattr(anon.os, "makedirs", failing_open)
        identified = anon.get_or_create_anonymous_user_id({"env": {"DSH_HOME": home}})
        assert anon.UUID_PATTERN.match(identified)  # best-effort persistence never blocks
        assert calls == [1]


# ----------------------------------------------------------- document face


def _parse(text, filename="<doc>"):
    return parse_credentials_document(text, filename)


class TestDocumentParsing:
    VALID = textwrap.dedent("""
        version: 1
        refs:
          DEEPSEEK_API_KEY: sk-secret-value
        records:
          llm-pi-ai/openai-codex:
            kind: grant
            payload:
              version: 1
              token: tok
    """)

    def test_valid_document(self):
        parsed = _parse(self.VALID)
        assert parsed["refs"] == {"DEEPSEEK_API_KEY": "sk-secret-value"}
        assert parsed["records"]["llm-pi-ai/openai-codex"] == {
            "kind": "grant", "payload": {"version": 1, "token": "tok"}}

    def test_empty_document_is_empty_store(self):
        assert _parse("") == {"refs": {}, "records": {}}
        assert _parse("# only a comment\n") == {"refs": {}, "records": {}}

    def test_non_mapping_root_rejected(self):
        with pytest.raises(TypeError, match="must be a mapping"):
            _parse("- a\n- b\n")
        with pytest.raises(TypeError, match="must be a mapping"):
            _parse("hello\n")

    def test_flat_layout_gets_migration_guidance(self):
        with pytest.raises(CredentialsDocumentError, match="pre-release flat layout") as info:
            _parse("DEEPSEEK_API_KEY: sk\nOTHER: v\n", "/x/.credentials.yaml")
        assert "nest the existing 2 entries under `refs:`" in str(info.value)
        with pytest.raises(CredentialsDocumentError, match="1 entry"):
            _parse("ONLY: v\n")

    def test_version_and_unknown_top_level(self):
        with pytest.raises(CredentialsDocumentError, match='declares version 2'):
            _parse("version: 2\nrefs: {}\n")
        with pytest.raises(CredentialsDocumentError, match='declares version "1"'):
            _parse('version: "1"\nrefs: {}\n')
        with pytest.raises(CredentialsDocumentError, match='unknown top-level key "extra"'):
            _parse("version: 1\nextra: {}\n")

    def test_duplicate_keys_rejected(self):
        with pytest.raises(CredentialsDocumentError, match="invalid document"):
            _parse("version: 1\nrefs:\n  A: x\n  A: y\n")

    def test_parse_diagnostics_never_quote_the_secret_line(self):
        text = "version: 1\nrefs: [unclosed\n  LEAKED_SECRET: s3cr3t\n"
        with pytest.raises(CredentialsDocumentError) as info:
            _parse(text, "/home/u/.credentials.yaml")
        message = str(info.value)
        assert "s3cr3t" not in message and "LEAKED_SECRET" not in message
        assert "/home/u/.credentials.yaml" in message and "line" in message

    def test_refs_validation(self):
        with pytest.raises(TypeError, match="must be a string"):
            _parse("version: 1\nrefs:\n  A: 42\n")
        with pytest.raises(CredentialsDocumentError, match="is empty; remove the key instead"):
            _parse('version: 1\nrefs:\n  A: ""\n')
        with pytest.raises(TypeError, match="must match"):
            _parse("version: 1\nrefs:\n  9bad: v\n")
        with pytest.raises(TypeError, match='"refs" .* must be a mapping'):
            _parse("version: 1\nrefs: [a]\n")

    def test_records_validation(self):
        with pytest.raises(TypeError, match="must be a mapping"):
            _parse("version: 1\nrecords:\n  s/i: 5\n")
        with pytest.raises(TypeError, match='must be "<scope>/<id>"'):
            _parse("version: 1\nrecords:\n  bad-key:\n    kind: grant\n    payload: 1\n")
        with pytest.raises(CredentialsDocumentError, match="has no kind"):
            _parse("version: 1\nrecords:\n  s/i: {}\n")
        with pytest.raises(CredentialsDocumentError, match="unknown kind"):
            _parse('version: 1\nrecords:\n  s/i:\n    kind: "oauth2"\n')
        with pytest.raises(CredentialsDocumentError, match='unknown field "typo"'):
            _parse("version: 1\nrecords:\n  s/i:\n    kind: grant\n    payload: 1\n    typo: x\n")
        with pytest.raises(CredentialsDocumentError, match="has no payload"):
            _parse("version: 1\nrecords:\n  s/i:\n    kind: grant\n")
        with pytest.raises(TypeError, match="non-string or empty key"):
            _parse('version: 1\nrecords:\n  s/i:\n    kind: api-key\n    key: ""\n')
        with pytest.raises(TypeError, match="non-mapping env"):
            _parse("version: 1\nrecords:\n  s/i:\n    kind: api-key\n    env: [a]\n")
        with pytest.raises(TypeError, match="must be a non-empty string"):
            _parse('version: 1\nrecords:\n  s/i:\n    kind: api-key\n    env:\n      NAME: ""\n')

    def test_grant_payload_must_round_trip_json(self):
        with pytest.raises(TypeError, match="non-finite number"):
            _parse("version: 1\nrecords:\n  s/i:\n    kind: grant\n    payload: .inf\n")
        with pytest.raises(TypeError, match="JSON cannot represent"):
            _parse("version: 1\nrecords:\n  s/i:\n    kind: grant\n    payload: 2024-01-02\n")
        # DOCUMENTED DEVIATION: upstream npm-yaml rejects recursive anchors at
        # parse time; ruamel degrades a recursive alias to a null placeholder,
        # which is JSON-lossless and admitted. The cycle guard still protects
        # the in-memory write path, covered next.
        degraded = _parse("version: 1\nrecords:\n  s/i:\n    kind: grant\n    payload: &a\n"
                          "      self: *a\n")
        assert degraded["records"]["s/i"]["payload"] == {"self": None}

    def test_in_memory_cyclic_payload_rejected_on_write(self, provider):
        from AstralNexus.runtime.credentials.local import assert_json_value
        cyclic: dict = {}
        cyclic["self"] = cyclic
        with pytest.raises(TypeError, match="is cyclic"):
            assert_json_value("payload", cyclic, set())
        local = provider()
        with pytest.raises(TypeError, match="is cyclic"):
            local.modify_record("s/i", lambda current: {"kind": "grant", "payload": cyclic})
        assert local.read_record("s/i") is None  # nothing broken was persisted

    def test_assert_storable_api_key_write_path(self):
        with pytest.raises(TypeError, match="omit the field instead"):
            assert_storable_api_key("s/i", {"kind": "api-key", "key": ""})
        with pytest.raises(TypeError, match="must be a non-empty string"):
            assert_storable_api_key("s/i", {"kind": "api-key", "env": {"OK": ""}})
        with pytest.raises(TypeError, match="must match"):
            assert_storable_api_key("s/i", {"kind": "api-key", "env": {"9bad": "v"}})
        assert_storable_api_key("s/i", {"kind": "api-key", "key": "k", "env": {"OK": "v"}})


class TestFlatLayoutMigration:
    def test_recognized_flat_layout_nests_verbatim(self):
        text = "# user note\nDEEPSEEK_API_KEY: sk-1\nOTHER_REF: v\n"
        migrated = render_flat_layout_migration(text)
        assert migrated == ("version: 1\nrefs:\n"
                            "  # user note\n  DEEPSEEK_API_KEY: sk-1\n  OTHER_REF: v\n")
        parsed = _parse(migrated)
        assert parsed["refs"] == {"DEEPSEEK_API_KEY": "sk-1", "OTHER_REF": "v"}

    def test_missing_trailing_newline_is_added(self):
        migrated = render_flat_layout_migration("A: v")
        assert migrated.endswith("  A: v\n")

    @pytest.mark.parametrize("text", [
        "version: 1\nA: v\n",          # already versioned
        "9BAD: v\n",                   # non-addressable key
        'A: ""\n',                     # empty value
        "A:\n  nested: v\n",           # non-scalar value
        "",                            # empty document
        "# only comment\n",            # no entries
        "---\nA: v\n",                 # document directive
        "%TAG ! x\nA: v\n",            # directive line
        "A: 42\n",                     # non-string scalar
    ])
    def test_declined_documents_keep_loud_rejection(self, text):
        assert render_flat_layout_migration(text) is None


class TestRendering:
    BASE = ("version: 1\nrefs:\n"
            "  # first note\n  FIRST: aaa\n"
            "  # second note\n  SECOND: bbb\n"
            "  THIRD: ccc\n")

    def test_set_preserves_untouched_entries_and_comments(self):
        rendered = render_ref(self.BASE, "SECOND", "new-value")
        assert "SECOND: new-value" in rendered
        assert "# first note" in rendered and "# second note" in rendered
        assert "FIRST: aaa" in rendered and "THIRD: ccc" in rendered

    def test_set_new_key_on_absent_document_stamps_version(self):
        rendered = render_ref(None, "NEW", "v")
        assert rendered.startswith("version: 1")
        assert _parse(rendered)["refs"] == {"NEW": "v"}

    def test_delete_middle_entry_takes_own_comment_keeps_next(self):
        rendered = render_ref(self.BASE, "SECOND", None)
        parsed = _parse(rendered)
        assert parsed["refs"] == {"FIRST": "aaa", "THIRD": "ccc"}
        # "# second note" annotated the DELETED entry: it goes with it.
        assert "# second note" not in rendered
        assert "# first note" in rendered and "FIRST: aaa" in rendered

    def test_delete_middle_rehosts_next_entry_comment(self):
        text = ("version: 1\nrefs:\n"
                "  # c1\n  FIRST: a\n"
                "  # c2\n  SECOND: b\n"
                "  # c3\n  THIRD: c\n")
        rendered = render_ref(text, "SECOND", None)
        parsed = _parse(rendered)
        assert parsed["refs"] == {"FIRST": "a", "THIRD": "c"}
        assert "# c2" not in rendered          # deleted entry's comment goes with it
        assert "# c1" in rendered and "# c3" in rendered
        assert rendered.index("# c3") < rendered.index("THIRD")  # re-hosted above THIRD

    def test_delete_first_entry_comment_never_moves_to_new_first(self):
        rendered = render_ref(self.BASE, "FIRST", None)
        parsed = _parse(rendered)
        assert parsed["refs"] == {"SECOND": "bbb", "THIRD": "ccc"}
        assert "# first note" not in rendered   # goes with the deleted entry
        assert "# second note" in rendered      # re-hosted at section level
        assert rendered.index("# second note") < rendered.index("SECOND")

    def test_delete_only_entry_leaves_empty_section(self):
        rendered = render_ref("version: 1\nrefs:\n  # note\n  ONLY: v\n", "ONLY", None)
        assert _parse(rendered) == {"refs": {}, "records": {}}

    def test_record_wholesale_replacement(self):
        text = render_record(None, "s/i", {"kind": "grant", "payload": {"v": 1}})
        assert _parse(text)["records"] == {"s/i": {"kind": "grant", "payload": {"v": 1}}}
        replaced = render_record(text, "s/i", {"kind": "api-key", "key": "k"})
        assert _parse(replaced)["records"] == {"s/i": {"kind": "api-key", "key": "k"}}
        deleted = render_record(replaced, "s/i", None)
        assert _parse(deleted)["records"] == {}


# ------------------------------------------------------------- the provider


def _env(process=None, project=None, user=None):
    layers = []
    if process is not None:
        layers.append(LaunchEnvironmentLayer("process", process))
    if project is not None:
        layers.append(LaunchEnvironmentLayer("project-env", project, path="./.env"))
    if user is not None:
        layers.append(LaunchEnvironmentLayer("user-env", user, path="~/.dsh/.env"))
    return create_launch_environment_snapshot(layers)


@pytest.fixture()
def provider(tmp_path):
    made = []

    def build(config=None, environment=None, watch=False, start=True):
        raw = {"path": str(tmp_path / "home" / CREDENTIALS_FILENAME), "watch": watch,
               "debounceMs": 30}
        raw.update(config or {})
        instance = LocalCredentialProvider(raw, launch_environment=environment or _env(process={}))
        if start:
            instance.start()
        made.append(instance)
        return instance

    yield build
    for instance in made:
        instance.close()


class TestProviderResolution:
    def test_layering_env_wins_then_file_then_dotenv(self, provider):
        environment = _env(process={"FROM_PROCESS": "p"},
                           project={"FROM_PROJECT": "proj", "BOTH": "proj"},
                           user={"FROM_USER": "u", "BOTH": "user"})
        local = provider(environment=environment)
        local.set("FROM_FILE", "f")
        assert local.resolve("FROM_PROCESS") == {"value": "p", "source": "env"}
        assert local.resolve("FROM_FILE") == {"value": "f", "source": "file"}
        assert local.resolve("FROM_PROJECT") == {"value": "proj", "source": "project-env"}
        assert local.resolve("FROM_USER") == {"value": "u", "source": "user-env"}
        assert local.resolve("BOTH") == {"value": "proj", "source": "project-env"}
        assert local.resolve("NOWHERE") is None

    def test_file_wins_over_dotenv(self, provider):
        environment = _env(project={"SHARED": "proj"})
        local = provider(environment=environment)
        local.set("SHARED", "stored")
        assert local.resolve("SHARED") == {"value": "stored", "source": "file"}

    def test_empty_inherited_value_is_absent(self, provider):
        local = provider(environment=_env(process={"BLANK": ""}))
        assert local.resolve("BLANK") is None
        assert local.describe("BLANK") == {"configured": False, "writable": True}

    def test_describe_states(self, provider):
        local = provider(environment=_env(process={"READONLY": "r"}, project={"DOT": "d"}))
        local.set("STORED", "s")
        assert local.describe("READONLY") == {"configured": True, "source": "env", "writable": False}
        assert local.describe("STORED") == {"configured": True, "source": "file", "writable": True}
        assert local.describe("DOT") == {"configured": True, "source": "project-env", "writable": True}
        assert local.describe("NONE") == {"configured": False, "writable": True}
        assert local.list_refs() == ["STORED"]  # metadata only, never values


class TestProviderWrites:
    def test_set_creates_document_and_notifies(self, provider, tmp_path):
        local = provider()
        events = []
        local.on("credentials/reference-updated", events.append)
        local.set("NEW_REF", "value")
        assert events == ["NEW_REF"]
        target = tmp_path / "home" / CREDENTIALS_FILENAME
        parsed = _parse(target.read_text(encoding="utf-8"))
        assert parsed["refs"] == {"NEW_REF": "value"}

    def test_empty_value_refused_loud(self, provider):
        local = provider()
        with pytest.raises(CredentialsDocumentError, match="use unset"):
            local.set("A", "")

    def test_shadowed_write_refused_loud(self, provider):
        local = provider(environment=_env(process={"SHADOW": "env-value"}))
        with pytest.raises(CredentialsDocumentError, match="would be shadowed") as info:
            local.set("SHADOW", "new")
        assert "unset it in the shell you start dsh from" in str(info.value)
        with pytest.raises(CredentialsDocumentError, match="would be shadowed"):
            local.unset("SHADOW")

    def test_unset_removes_entry_and_notifies_once(self, provider):
        local = provider()
        local.set("A", "v")
        events = []
        local.on("credentials/reference-updated", events.append)
        local.unset("A")
        assert events == ["A"]
        assert local.resolve("A") is None
        local.unset("A")  # absent: no-op, no event
        assert events == ["A"]

    def test_write_preserves_hand_comments(self, provider, tmp_path):
        target = tmp_path / "home" / CREDENTIALS_FILENAME
        target.parent.mkdir(parents=True)
        target.write_text("# user header\nversion: 1\nrefs:\n  # keep me\n  KEEP: v\n",
                          encoding="utf-8")
        local = provider()
        local.start()  # fixture built it unstarted? no — fixture starts; second start is reload
        local.set("OTHER", "x")
        text = target.read_text(encoding="utf-8")
        assert "# keep me" in text and "KEEP: v" in text and "OTHER: x" in text

    def test_modify_record_read_decide_replace(self, provider, tmp_path):
        local = provider()
        events = []
        local.on("credentials/record-updated", events.append)
        seen_current = []

        def mutate(current):
            seen_current.append(current)
            return {"kind": "grant", "payload": {"token": "t1"}}

        result = local.modify_record("s/i", mutate)
        assert result == {"kind": "grant", "payload": {"token": "t1"}}
        assert seen_current == [None]
        assert events == ["s/i"]
        assert local.read_record("s/i")["payload"] == {"token": "t1"}
        # Mutations of the returned copy never reach the store.
        result["payload"]["token"] = "tampered"
        assert local.read_record("s/i")["payload"]["token"] == "t1"

        # The mutate decision sees the CURRENT value and may no-op.
        before_text = (tmp_path / "home" / CREDENTIALS_FILENAME).read_text(encoding="utf-8")
        decisions = []

        def keep(current):
            decisions.append(current)
            return None

        assert local.modify_record("s/i", keep) == {"kind": "grant", "payload": {"token": "t1"}}
        assert decisions == [{"kind": "grant", "payload": {"token": "t1"}}]
        assert events == ["s/i"]  # no second event: nothing committed
        assert (tmp_path / "home" / CREDENTIALS_FILENAME).read_text(encoding="utf-8") == before_text

    def test_modify_record_validates_durable_boundary(self, provider):
        local = provider()
        with pytest.raises(TypeError, match="non-finite number"):
            local.modify_record("s/i", lambda current: {"kind": "grant", "payload": float("inf")})
        with pytest.raises(TypeError, match="omit the field instead"):
            local.modify_record("s/i", lambda current: {"kind": "api-key", "key": ""})
        with pytest.raises(CredentialsDocumentError, match="unknown field"):
            local.modify_record("s/i", lambda current: {"kind": "grant", "payload": 1, "x": 2})
        assert local.read_record("s/i") is None  # nothing was persisted

    def test_delete_record(self, provider):
        local = provider()
        local.modify_record("s/i", lambda current: {"kind": "api-key", "key": "k"})
        events = []
        local.on("credentials/record-updated", events.append)
        assert local.describe_record("s/i") == {"configured": True, "kind": "api-key", "writable": True}
        assert local.list_records() == [{"key": "s/i", "kind": "api-key"}]
        local.delete_record("s/i")
        assert events == ["s/i"]
        assert local.describe_record("s/i") == {"configured": False, "writable": True}
        local.delete_record("s/i")  # absent: no-op, no event
        assert events == ["s/i"]

    def test_disposed_provider_refuses_writes(self, provider):
        local = provider()
        local.close()
        with pytest.raises(CredentialsDocumentError, match="is disposed: cannot set"):
            local.set("A", "v")
        with pytest.raises(CredentialsDocumentError, match="is disposed: cannot modify"):
            local.modify_record("s/i", lambda current: None)
        with pytest.raises(CredentialsDocumentError, match="is disposed: cannot delete"):
            local.delete_record("s/i")

    def test_concurrent_writers_serialize(self, provider, tmp_path):
        local = provider()
        errors = []

        def worker(index):
            try:
                local.modify_record(f"scope/k{index}",
                                    lambda current: {"kind": "grant", "payload": {"i": index}})
            except Exception as error:  # pragma: no cover - failure evidence
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        keys = {item["key"] for item in local.list_records()}
        assert keys == {f"scope/k{i}" for i in range(6)}
        parsed = _parse((tmp_path / "home" / CREDENTIALS_FILENAME).read_text(encoding="utf-8"))
        assert len(parsed["records"]) == 6


class TestProviderReload:
    def _write_raw(self, tmp_path, text):
        target = tmp_path / "home" / CREDENTIALS_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def test_external_edit_hot_publishes(self, provider, tmp_path):
        local = provider()
        events = []
        local.on("credentials/reference-updated", events.append)
        self._write_raw(tmp_path, "version: 1\nrefs:\n  EXTERNAL: v1\n")
        local.reconcile_from_disk()
        assert events == ["EXTERNAL"]
        assert local.resolve("EXTERNAL") == {"value": "v1", "source": "file"}
        # Own-state cache means an unchanged reread is a no-op.
        local.reconcile_from_disk()
        assert events == ["EXTERNAL"]

    def test_deleted_file_publishes_empty_store(self, provider, tmp_path):
        local = provider()
        local.set("A", "v")
        (tmp_path / "home" / CREDENTIALS_FILENAME).unlink()
        local.reconcile_from_disk()
        assert local.resolve("A") is None and local.list_refs() == []

    def test_invalid_document_keeps_last_good_on_refresh(self, provider, tmp_path):
        local = provider()
        local.set("GOOD", "v")
        self._write_raw(tmp_path, "version: 1\nrefs: [broken\n")
        local.refresh()  # warns, never raises, never wipes
        assert local.resolve("GOOD") == {"value": "v", "source": "file"}

    def test_write_path_fails_loud_over_invalid_document(self, provider, tmp_path):
        local = provider()
        self._write_raw(tmp_path, "version: 3\nrefs: {}\n")
        with pytest.raises(CredentialsDocumentError, match="declares version 3"):
            local.set("A", "v")
        # The invalid document was NOT overwritten.
        assert "version: 3" in (tmp_path / "home" / CREDENTIALS_FILENAME).read_text(encoding="utf-8")

    def test_flat_layout_migrated_in_place_at_boot(self, provider, tmp_path):
        target = self._write_raw(tmp_path, "# note\nLEGACY_KEY: legacy-value\n")
        local = provider()  # fixture starts it: boot read migrates
        assert local.resolve("LEGACY_KEY") == {"value": "legacy-value", "source": "file"}
        text = target.read_text(encoding="utf-8")
        assert text.startswith("version: 1\nrefs:\n") and "# note" in text

    def test_invalid_document_fails_boot_loud(self, provider, tmp_path):
        self._write_raw(tmp_path, "version: 9\nrefs: {}\n")
        with pytest.raises(CredentialsDocumentError, match="declares version 9"):
            provider(start=False).start()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
class TestOwnerOnlyPosix:
    def test_world_readable_document_refused_before_read(self, provider, tmp_path):
        target = tmp_path / "home" / CREDENTIALS_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("version: 1\nrefs:\n  A: v\n", encoding="utf-8")
        os.chmod(target, 0o644)
        with pytest.raises(CredentialsDocumentError, match="readable beyond its owner") as info:
            provider(start=False).start()
        assert "chmod 600" in str(info.value)


class TestWatcher:
    def _wait_for(self, predicate, limit=10.0):
        deadline = time.time() + limit
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_external_write_hot_publishes_through_watcher(self, provider, tmp_path):
        local = provider(watch=True)
        target = tmp_path / "home" / CREDENTIALS_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        events = []
        local.on("credentials/reference-updated", events.append)
        target.write_text("version: 1\nrefs:\n  WATCHED: w1\n", encoding="utf-8")
        assert self._wait_for(lambda: events == ["WATCHED"]), events
        assert local.resolve("WATCHED") == {"value": "w1", "source": "file"}
        # A second external edit re-publishes.
        target.write_text("version: 1\nrefs:\n  WATCHED: w2\n", encoding="utf-8")
        assert self._wait_for(lambda: local.resolve("WATCHED")["value"] == "w2")

    def test_own_write_is_suppressed(self, provider, tmp_path):
        local = provider(watch=True)
        events = []
        local.on("credentials/reference-updated", events.append)
        local.set("OWN", "v")
        assert events == ["OWN"]  # the synchronous write notified once
        time.sleep(0.4)           # several debounce windows pass
        assert events == ["OWN"]  # the watcher's view of our own write is a no-op

    def test_invalid_external_edit_keeps_last_good(self, provider, tmp_path):
        local = provider(watch=True)
        local.set("SAFE", "v")
        target = tmp_path / "home" / CREDENTIALS_FILENAME
        target.write_text("version: 1\nrefs: [broken\n", encoding="utf-8")
        time.sleep(0.5)
        assert local.resolve("SAFE") == {"value": "v", "source": "file"}


class TestSpecAndConfig:
    def test_resolve_spec_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DSH_HOME", str(tmp_path))
        spec = resolve_spec({})
        assert spec["filename"] == os.path.join(str(tmp_path), CREDENTIALS_FILENAME)
        assert spec["watch"] is True and spec["debounceMs"] == 100
        explicit = resolve_spec({"path": "rel/file.yaml", "watch": False, "debounceMs": 0})
        assert explicit["filename"] == os.path.abspath("rel/file.yaml")
        assert explicit["watch"] is False and explicit["debounceMs"] == 0

    def test_validate_config(self):
        assert validate_config(None) == {}
        with pytest.raises(CredentialsDocumentError, match="must be a mapping"):
            validate_config("x")
        with pytest.raises(CredentialsDocumentError, match="path must be a string"):
            validate_config({"path": 42})
        with pytest.raises(CredentialsDocumentError, match="watch must be a boolean"):
            validate_config({"watch": "yes"})
        with pytest.raises(CredentialsDocumentError, match="debounceMs"):
            validate_config({"debounceMs": -1})
        with pytest.raises(CredentialsDocumentError, match="debounceMs"):
            validate_config({"debounceMs": True})

    def test_yaml_error_description_has_no_content(self):
        from ruamel.yaml.error import MarkedYAMLError
        error = MarkedYAMLError("context", None, "problem", None)
        assert describe_yaml_error(error) == "MarkedYAMLError"


# ----------------------------------------------------- PluginHost integration


@pytest.fixture()
def plugin_host(tmp_path, monkeypatch):
    from AstralNexus.runtime.plugin_host import BUILTIN_MANIFESTS, PluginHost
    from AstralNexus.runtime.workbench import WorkbenchStore

    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    host = PluginHost(WorkbenchStore(tmp_path / "host"))
    host.seed_builtins(BUILTIN_MANIFESTS)
    return host


class TestPluginIntegration:
    def test_builtin_seeded_disabled_by_default(self, plugin_host):
        items = {item["id"]: item for item in plugin_host.inventory()}
        entry = items["credentials-local"]
        assert entry["desired"] == "disabled" and entry["phase"] == "disposed"
        assert entry["kind"] == "native" and entry["service"] == "credentials"
        assert "credentials" not in plugin_host.services  # never started implicitly

    def test_enable_starts_provider_and_disable_withdraws(self, plugin_host, tmp_path):
        document = str(tmp_path / "docs" / CREDENTIALS_FILENAME)
        plugin_host.put_config("credentials-local", {"path": document, "watch": False})
        plugin_host.set_enabled("credentials-local", True)
        assert plugin_host.is_active("credentials-local")
        provider = plugin_host.services["credentials"]
        assert provider.spec["filename"] == os.path.abspath(document)
        provider.set("THROUGH_PLUGIN", "v")
        assert provider.resolve("THROUGH_PLUGIN")["source"] == "file"
        assert "credentials" in plugin_host.active_services()
        plugin_host.set_enabled("credentials-local", False)
        assert "credentials" not in plugin_host.services
        assert provider.is_closed()

    def test_invalid_config_rejected_by_schema(self, plugin_host):
        from AstralNexus.runtime.plugin_host import PluginError
        with pytest.raises(PluginError):
            plugin_host.put_config("credentials-local", {"debounceMs": -5})
        with pytest.raises(PluginError):
            plugin_host.put_config("credentials-local", {"unknown": 1})

    def test_broken_document_fails_activation_isolated(self, plugin_host, tmp_path):
        document = tmp_path / "docs" / CREDENTIALS_FILENAME
        document.parent.mkdir(parents=True)
        document.write_text("version: 77\nrefs: {}\n", encoding="utf-8")
        plugin_host.put_config("credentials-local", {"path": str(document), "watch": False})
        plugin_host.set_enabled("credentials-local", True)
        entry = next(item for item in plugin_host.snapshot() if item["id"] == "credentials-local")
        assert entry["status"] == "failed" or entry.get("last_error")
        state = {item["id"]: item for item in plugin_host.inventory()}["credentials-local"]
        assert state["phase"] == "failed" and "load_failed" in state["last_error"]
        assert "credentials" not in plugin_host.services  # a broken store is never served

    def test_recover_restores_provider(self, plugin_host, tmp_path):
        from AstralNexus.runtime.plugin_host import PluginHost

        document = str(tmp_path / "docs2" / CREDENTIALS_FILENAME)
        plugin_host.put_config("credentials-local", {"path": document, "watch": False})
        plugin_host.set_enabled("credentials-local", True)
        plugin_host.services["credentials"].set("PERSISTED", "v")
        reopened = PluginHost(plugin_host.store, home=plugin_host.home)
        reopened.recover()
        assert reopened.is_active("credentials-local")
        assert reopened.services["credentials"].resolve("PERSISTED") == {
            "value": "v", "source": "file"}
        reopened.services["credentials"].close()


# -------------------------------------------------- WorkbenchHost HTTP surface


class TestWorkbenchHttp:
    def test_credentials_projection_metadata_only(self, tmp_path, monkeypatch):
        from AstralNexus.dashboard.workbench_server import WorkbenchHost

        monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
        instance = WorkbenchHost(tmp_path / "wb")
        try:
            client = instance.app.test_client()
            body = client.get("/api/host/credentials").get_json()
            assert body == {"available": False}  # disabled by default
            snapshot = client.get("/api/host").get_json()
            assert snapshot["credentials"] == body

            document = tmp_path / "docs" / CREDENTIALS_FILENAME
            document.parent.mkdir(parents=True)
            document.write_text("version: 1\nrefs:\n  SECRET_REF: s3cr3t-value\n"
                                "records:\n  demo/key:\n    kind: api-key\n    key: k\n",
                                encoding="utf-8")
            instance.plugins.put_config("credentials-local", {"path": str(document), "watch": False})
            instance.plugins.set_enabled("credentials-local", True)
            response = client.get("/api/host/credentials")
            body = response.get_json()
            assert body["available"] is True
            assert body["refs"] == {"SECRET_REF": {"configured": True, "source": "file",
                                                    "writable": True}}
            assert body["records"] == [{"key": "demo/key", "kind": "api-key"}]
            # The HTTP surface never carries a secret value.
            assert b"s3cr3t-value" not in response.data
            assert "s3cr3t-value" not in str(client.get("/api/host").get_json())

            instance.plugins.set_enabled("credentials-local", False)
            assert client.get("/api/host/credentials").get_json() == {"available": False}
        finally:
            instance.close()


def test_anonymous_identity_converges_across_real_processes(tmp_path):
    import subprocess
    source = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, PYTHONPATH=str(source), PYTHONIOENCODING='utf-8', DSH_HOME=str(tmp_path))
    code = 'from AstralNexus.runtime.credentials.anonymous_user_id import get_or_create_anonymous_user_id; print(get_or_create_anonymous_user_id())'
    children = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment) for _ in range(4)]
    try:
        results = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stderr
            results.append(stdout.strip())
        persisted = (tmp_path / anon.ANONYMOUS_USER_ID_FILE_NAME).read_text(encoding='utf-8').strip()
        assert len(set(results)) == 1 and results[0] == persisted
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
