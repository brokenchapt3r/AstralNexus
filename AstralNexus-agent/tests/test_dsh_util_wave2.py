"""Contract tests for wave-2 dshutil native ports (code-language, workspace-path, output-retention)."""
from __future__ import annotations

import pytest

from AstralNexus.runtime.dshutil import code_language as codelang
from AstralNexus.runtime.dshutil import output_retention as ret
from AstralNexus.runtime.dshutil import workspace_path as wp


# ------------------------------------------------- dsh-util-code-language


class TestCodeLanguage:
    @pytest.mark.parametrize("path,language", [
        ("main.py", "python"), ("dir/main.PS1", "powershell"),
        ("C:\\dir\\main.PS1", "powershell"), (".env", "dotenv"),
        ("a/b/app.test.tsx", "typescript"), ("notes.markdown", "markdown"),
        ("Makefile.mk", "make"), ("x.proto", "proto"),
    ])
    def test_language_for_path(self, path, language):
        assert codelang.language_for_path(path) == language

    @pytest.mark.parametrize("path", [".gitignore", "LICENSE", "noext", "dir/", "a.unknownext"])
    def test_unrecognized_returns_none(self, path):
        assert codelang.language_for_path(path) is None

    def test_read_lang_hints(self):
        assert codelang.read_lang_hint_for_path("a.ts") == "ts"
        assert codelang.read_lang_hint_for_path("a.tsx") == "tsx"
        assert codelang.read_lang_hint_for_path("main.tf") == "tf"
        assert codelang.read_lang_hint_for_path("build.gradle") == "gradle"
        assert codelang.read_lang_hint_for_path("s.ps1") == "ps1"
        assert codelang.read_lang_hint_for_path("m.sv") == "sv"
        assert codelang.read_lang_hint_for_path("k.kt") == "kotlin"
        assert codelang.read_lang_hint_for_path("x.unknown") is None

    def test_extension_table_shape(self):
        assert codelang.CODE_HIGHLIGHT_EXTENSIONS == list(codelang.LANGUAGES)
        assert len(set(codelang.CODE_HIGHLIGHT_EXTENSIONS)) == len(codelang.CODE_HIGHLIGHT_EXTENSIONS)
        assert codelang.LANGUAGES["ipynb"] == "json"
        # prototype-key filenames must miss, not resolve inherited members
        assert codelang.language_for_path("foo.constructor") is None
        assert codelang.language_for_path("foo.__proto__") is None


# ------------------------------------------------ dsh-util-workspace-path


class TestWorkspacePath:
    def test_session_address_roundtrip(self):
        address = wp.session_file_address("sess/1", "src\\main.py")
        assert address == "dsh-resource://file/session/sess%2F1/src/main.py"
        parsed = wp.parse_file_address(address)
        assert parsed.scope == "session" and parsed.session_id == "sess/1"
        assert parsed.path == "src/main.py"

    def test_leading_dot_slash_dropped(self):
        assert wp.session_file_address("s", "././a/b.py") == "dsh-resource://file/session/s/a/b.py"

    def test_absolute_address_posix_windows_unc(self):
        parsed = wp.parse_file_address(wp.absolute_file_address("/tmp/x y.txt"))
        assert parsed == wp.FileAddress(scope="absolute", path="/tmp/x y.txt")
        parsed = wp.parse_file_address(wp.absolute_file_address("C:\\dir\\f.py"))
        assert parsed.path == "C:/dir/f.py"  # drive segment keeps no leading slash
        parsed = wp.parse_file_address(wp.absolute_file_address("\\\\server\\share\\f"))
        assert parsed.path == "//server/share/f"

    def test_parse_rejects_malformed(self):
        assert wp.parse_file_address("http://x/file/a") is None
        assert wp.parse_file_address("dsh-resource://file/other/a") is None
        assert wp.parse_file_address("dsh-resource://file/session//a") is None  # empty id
        assert wp.parse_file_address("dsh-resource://file/session/s") is None  # no path
        assert wp.parse_file_address("dsh-resource://file/absolute/%zz/x") is None  # bad escape
        assert wp.parse_file_address("dsh-resource://file/absolute//") is None

    def test_parse_ignores_query_and_fragment(self):
        parsed = wp.parse_file_address("dsh-resource://file/session/s/a/b.py?v=2#frag")
        assert parsed.path == "a/b.py"

    def test_classification_and_resolution(self):
        assert wp.is_absolute_workspace_path("/a/b")
        assert wp.is_absolute_workspace_path("C:/a") and wp.is_absolute_workspace_path("c:\\a")
        assert wp.is_absolute_workspace_path("\\\\srv\\share")
        assert not wp.is_absolute_workspace_path("rel/path")
        assert wp.resolve_workspace_path(None, "rel") == "rel"
        assert wp.resolve_workspace_path("", "rel") == "rel"
        assert wp.resolve_workspace_path("/ws/root", "rel/a") == "/ws/root/rel/a"
        assert wp.resolve_workspace_path("C:\\ws", "rel\\a") == "C:\\ws\\rel\\a"
        assert wp.resolve_workspace_path("/ws", "/abs") == "/abs"

    def test_home_abbreviation_posix_only(self):
        assert wp.abbreviate_home_path("/home/u/x", "/home/u") == "~/x"
        assert wp.abbreviate_home_path("/home/u/", "/home/u") == "~"
        assert wp.abbreviate_home_path("/other/x", "/home/u") == "/other/x"
        assert wp.abbreviate_home_path("C:\\Users\\u\\x", "C:\\Users\\u") == "C:\\Users\\u\\x"
        assert wp.abbreviate_home_path("/x", None) == "/x" and wp.abbreviate_home_path("/x", "/") == "/x"

    def test_title_and_parts(self):
        assert wp.workspace_title_of("/a/b/c/") == "c"
        assert wp.workspace_title_of("C:\\a\\b") == "b"
        assert wp.workspace_title_of("///") == ""
        assert wp.path_parts_of("a/b\\c") == wp.PathParts(directory="a/b\\", name="c")
        assert wp.path_parts_of("/dir/") == wp.PathParts(directory="/", name="dir")
        assert wp.path_parts_of("solo") == wp.PathParts(directory="", name="solo")
        assert wp.path_parts_of("///") == wp.PathParts(directory="", name="///")

    def test_file_address_for_scoping(self):
        assert wp.file_address_for("s", "/ws", "rel/a") == "dsh-resource://file/session/s/rel/a"
        assert wp.file_address_for("s", "/ws", "/ws") == "dsh-resource://file/session/s/"
        assert wp.file_address_for("s", "/ws", "/ws/a/b") == "dsh-resource://file/session/s/a/b"
        assert wp.file_address_for("s", "/ws", "/elsewhere/a") == "dsh-resource://file/session/s//elsewhere/a"
        assert wp.file_address_for("s", "C:\\ws", "C:/ws/a") == "dsh-resource://file/session/s/a"

    def test_relativize_to_cwd(self):
        assert wp.relativize_to_cwd("/ws/a/b", "/ws/") == "a/b"
        assert wp.relativize_to_cwd("C:\\ws\\a", "C:\\ws") == "a"
        assert wp.relativize_to_cwd("/other/a", "/ws") == "/other/a"
        assert wp.relativize_to_cwd("/ws/a", None) == "/ws/a"

    def test_file_media_url(self):
        # encodeURIComponent encodes '/' as %2F — the route receives one query value.
        assert wp.file_media_url("http://127.0.0.1:5073/", "/a b.py") == \
            "http://127.0.0.1:5073/api/file?path=%2Fa%20b.py"
        assert wp.file_media_url("dsh-app://app/", "/a.py").startswith("dsh-app://app/api/file?path=")
        assert wp.file_media_url("ftp://x/", "/a") is None
        assert wp.file_media_url("http://h/", "rel/a") is None
        assert wp.file_media_url("http://h/", "\\\\srv\\share") is None
        assert wp.file_media_url("http://h/", "/a\u0000b") is None
        assert wp.file_media_url("http://h/prefix/", "/a") == "http://h/prefix/api/file?path=%2Fa"


# ------------------------------------------------- dsh-output-retention


class TestItemRetainer:
    def test_head_retention_exact_metadata(self):
        retainer = ret.ItemRetainer({"kind": "head", "max_items": 2})
        assert retainer.push("a") == ret.PushDecision(kept=True, truncated=False)
        assert retainer.push("b") == ret.PushDecision(kept=True, truncated=False)
        assert retainer.push("c") == ret.PushDecision(kept=False, truncated=True)
        result = retainer.finish()
        assert result.items == ["a", "b"] and result.seen == 3 and result.kept == 2
        assert result.truncated and result.omitted == ret.Omitted(kind="exact", count=1)

    def test_no_omission_and_budget_validation(self):
        result = ret.ItemRetainer({"max_items": 5}).finish()
        assert not result.truncated and result.omitted == ret.NONE_OMITTED
        with pytest.raises(ValueError, match="non-negative integer"):
            ret.ItemRetainer({"max_items": -1})
        with pytest.raises(ValueError, match="non-negative integer"):
            ret.ItemRetainer({"max_items": 1.5})
        with pytest.raises(ValueError):
            ret.ItemRetainer({"kind": "tail", "max_items": 1})


class TestTextRetainer:
    def test_head_keeps_prefix_and_counts_bytes(self):
        retainer = ret.TextRetainer({"kind": "head", "max_bytes": 5})
        assert retainer.push("abc").kept
        assert not retainer.push("defgh").kept
        result = retainer.finish()
        assert result.text == "abcde" and result.truncated
        assert result.omitted_bytes == ret.Omitted(kind="exact", count=3)

    def test_tail_rolls_to_last_bytes(self):
        retainer = ret.TextRetainer({"kind": "tail", "max_bytes": 4})
        retainer.push("12345")
        retainer.push("67")
        assert retainer.finish().text == "4567"

    def test_head_tail_both_ends(self):
        retainer = ret.TextRetainer({"kind": "head_tail", "head_bytes": 3, "tail_bytes": 3})
        retainer.push("ABCDEFGH")
        result = retainer.finish()
        assert result.text == "ABC" + "FGH"
        assert result.omitted_bytes.count == 2

    def test_utf8_boundary_trim_only_when_budget_omits(self):
        # "é" is 2 bytes; a head cut at 3 bytes lands mid-codepoint on the 2nd é.
        retainer = ret.TextRetainer({"kind": "head", "max_bytes": 3})
        retainer.push("ééé".encode("utf-8"))  # 6 bytes
        result = retainer.finish()
        assert result.text == "é"  # partial codepoint trimmed, no U+FFFD introduced
        assert result.omitted_bytes.count == 4  # budget 3 minus trimmed codepoint
        # tail cut drops leading continuation bytes
        tail = ret.TextRetainer({"kind": "tail", "max_bytes": 3})
        tail.push("ééé".encode("utf-8"))
        assert tail.finish().text == "é"
        # no omission → no trimming, malformed interior still replaces
        whole = ret.TextRetainer({"kind": "head", "max_bytes": 100})
        whole.push(b"ok\xff\xfe")
        assert whole.finish().text == "ok\ufffd\ufffd"
        assert not whole.finish().truncated

    def test_memory_bound_and_decision_flags(self):
        retainer = ret.TextRetainer({"kind": "tail", "max_bytes": 10})
        for _ in range(100):
            decision = retainer.push("0123456789")
        assert decision == ret.PushDecision(kept=False, truncated=True)
        # only the last chunks are held
        assert sum(len(chunk) for chunk in retainer._suffix_chunks) <= 20
        assert retainer.finish().text == "0123456789"

    def test_strategy_validation(self):
        with pytest.raises(ValueError, match="unknown text retention kind"):
            ret.TextRetainer({"kind": "middle", "max_bytes": 1})
        with pytest.raises(ValueError, match="head_bytes must be"):
            ret.TextRetainer({"kind": "head_tail", "head_bytes": -1, "tail_bytes": 1})


class TestRetentionNotices:
    def test_describe_omitted_wording(self):
        assert ret.describe_omitted(ret.NONE_OMITTED, "items") == ""
        assert ret.describe_omitted(ret.Omitted(kind="exact", count=3), "items") == "Omitted 3 items."
        assert ret.describe_omitted(ret.Omitted(kind="unknown"), "bytes") == "More bytes were omitted."

    def test_format_joins_nonempty_halves(self):
        notice = ret.RetentionNotice(omitted=ret.Omitted(kind="exact", count=2), unit="lines",
                                     extra={"limit": 10})
        assert ret.format_retention_notice(notice, lambda n: f"Limit is {n.extra['limit']}.") == \
            "Omitted 2 lines. Limit is 10."
        assert ret.format_retention_notice(notice, lambda n: "") == "Omitted 2 lines."
        empty = ret.RetentionNotice(omitted=ret.NONE_OMITTED, unit="lines")
        assert ret.format_retention_notice(empty, lambda n: "") == ""


class TestSurrogateSafeTruncation:
    def test_ascii_and_astral_cuts(self):
        assert ret.truncate_without_splitting_surrogate_pair("abcdef", 10) == "abcdef"
        assert ret.truncate_without_splitting_surrogate_pair("abcdef", 3) == "abc"
        # "𝄞" is one codepoint but two UTF-16 units: cap 3 keeps "a" + pair, cap 2 drops the pair
        text = "a𝄞b"
        assert ret.truncate_without_splitting_surrogate_pair(text, 3) == "a𝄞"
        assert ret.truncate_without_splitting_surrogate_pair(text, 2) == "a"
        assert ret.truncate_without_splitting_surrogate_pair(text, 1) == "a"
        assert ret.truncate_without_splitting_surrogate_pair(text, 0) == ""
        with pytest.raises(ValueError):
            ret.truncate_without_splitting_surrogate_pair(text, -1)
