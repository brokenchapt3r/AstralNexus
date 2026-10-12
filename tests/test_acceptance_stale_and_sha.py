"""P6-B (doc 32 §4): report_not_stale + sha_matches acceptance leaf families.

r14 fraud lesson: the N9RR lineage re-shipped byte-identical copies of voided
reports twice before a 5-hour forensic node caught it by hand.  These tests
pin the decidable FAIL (STALE_REPORT_BYTE_IDENTICAL), the fail-open paths
(missing baseline / above-cap reader-only → UNVERIFIED, never a silent pass),
and hash pinning via sha_matches.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import AstralNexus.runtime.acceptance_checks as ac
from AstralNexus.runtime.acceptance_checks import (
    KIND_REPORT_NOT_STALE,
    KIND_SHA_MATCHES,
    KIND_UNDECIDABLE,
    parse_criterion,
    render_acceptance_observation,
    run_acceptance_checks,
)


def _write(root: Path, relative: str, content: bytes) -> str:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _one(observations, criterion_id="C01"):
    matches = [item for item in observations if item["criterion_id"] == criterion_id]
    assert len(matches) == 1
    return matches[0]


class TestParseNewFamilies:
    def test_report_not_stale_payload(self) -> None:
        kind, payload = parse_criterion(
            "report_not_stale:deliverables/report.json differs_from:archive/superseded/report.json"
        )
        assert kind == KIND_REPORT_NOT_STALE
        assert payload == {
            "path": "deliverables/report.json",
            "baseline": "archive/superseded/report.json",
        }

    def test_family_keyword_case_insensitive(self) -> None:
        kind, _ = parse_criterion("REPORT_NOT_STALE:a.json DIFFERS_FROM:b.json")
        assert kind == KIND_REPORT_NOT_STALE

    def test_sha_matches_payload(self) -> None:
        digest = "a" * 64
        kind, payload = parse_criterion(f"sha_matches:deliverables/x.json sha256:{digest.upper()}")
        assert kind == KIND_SHA_MATCHES
        assert payload == {"path": "deliverables/x.json", "digest": digest}

    def test_sha_matches_bad_digest_is_undecidable(self) -> None:
        assert parse_criterion("sha_matches:x.json sha256:tooshort")[0] == KIND_UNDECIDABLE
        assert parse_criterion("sha_matches:x.json sha256:" + "g" * 64)[0] == KIND_UNDECIDABLE

    def test_prose_stays_undecidable(self) -> None:
        assert parse_criterion("报告不得复用旧版本")[0] == KIND_UNDECIDABLE
        assert parse_criterion("report_not_stale:only-one-arg")[0] == KIND_UNDECIDABLE


class TestReportNotStale:
    def test_differs_holds(self, tmp_path: Path) -> None:
        _write(tmp_path, "new.json", b'{"v": 2}')
        _write(tmp_path, "old.json", b'{"v": 1}')
        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:old.json"], receipts=[], root=tmp_path
        )
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is True

    def test_byte_identical_is_decidable_fail(self, tmp_path: Path) -> None:
        payload = b'{"report": "stale"}'
        _write(tmp_path, "new.json", payload)
        _write(tmp_path, "archive/old.json", payload)
        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:archive/old.json"], receipts=[], root=tmp_path
        )
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is False
        assert item["reason"] == "STALE_REPORT_BYTE_IDENTICAL"
        assert render_acceptance_observation(item).startswith("DOES NOT HOLD")

    def test_missing_new_file_fails(self, tmp_path: Path) -> None:
        _write(tmp_path, "old.json", b"x")
        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:old.json"], receipts=[], root=tmp_path
        )
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is False
        assert item["reason"] == "FILE_NOT_FOUND"

    def test_missing_baseline_is_unverified_not_pass(self, tmp_path: Path) -> None:
        _write(tmp_path, "new.json", b"x")
        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:archive/gone.json"], receipts=[], root=tmp_path
        )
        item = _one(obs)
        assert item["checked"] is False and item["holds"] is None
        assert item["reason"] == "STALE_BASELINE_UNAVAILABLE"
        assert render_acceptance_observation(item).startswith("UNVERIFIED")

    def test_escape_outside_workspace_unverified(self, tmp_path: Path) -> None:
        _write(tmp_path, "new.json", b"x")
        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:../../etc/passwd"], receipts=[], root=tmp_path
        )
        item = _one(obs)
        assert item["checked"] is False and item["holds"] is None
        assert item["reason"] == "FILE_NOT_IN_SHARED_DOMAIN"

    def test_above_cap_streaming_hash(self, tmp_path: Path, monkeypatch) -> None:
        """Above the read cap, a real root stream-hashes; identical bytes still FAIL."""
        monkeypatch.setattr(ac, "FILE_CONTENT_READ_CAP_BYTES", 8)
        payload = b"0123456789abcdef" * 4  # 64 bytes > cap 8
        _write(tmp_path, "new.json", payload)
        _write(tmp_path, "old.json", payload)
        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:old.json"], receipts=[], root=tmp_path
        )
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is False
        assert item["reason"] == "STALE_REPORT_BYTE_IDENTICAL"

    def test_above_cap_same_size_different_bytes_holds(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(ac, "FILE_CONTENT_READ_CAP_BYTES", 8)
        _write(tmp_path, "new.json", b"aaaaaaaaaaaaaaaaaaaa")
        _write(tmp_path, "old.json", b"bbbbbbbbbbbbbbbbbbbb")
        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:old.json"], receipts=[], root=tmp_path
        )
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is True

    def test_reader_only_above_cap_inconclusive(self, tmp_path: Path, monkeypatch) -> None:
        """Reader-only injection with an above-cap file cannot hash → UNVERIFIED."""
        monkeypatch.setattr(ac, "FILE_CONTENT_READ_CAP_BYTES", 8)

        def capped_reader(root: Path, relative: str):
            return 64, None, None  # size known, content withheld

        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:old.json"],
            receipts=[], root=None, reader=capped_reader,
        )
        item = _one(obs)
        assert item["checked"] is False and item["holds"] is None
        assert item["reason"] == "STALE_COMPARISON_INCONCLUSIVE"

    def test_injected_reader_decides_from_content(self) -> None:
        contents = {
            "new.json": b'{"v": 2}',
            "old.json": b'{"v": 1}',
        }

        def reader(root: Path, relative: str):
            data = contents.get(str(relative))
            if data is None:
                return None, None, "FILE_NOT_FOUND"
            return len(data), data, None

        obs = run_acceptance_checks(
            ["report_not_stale:new.json differs_from:old.json"], receipts=[], root=None, reader=reader
        )
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is True


class TestShaMatches:
    def test_matching_digest_holds(self, tmp_path: Path) -> None:
        digest = _write(tmp_path, "x.json", b'{"a": 1}')
        obs = run_acceptance_checks([f"sha_matches:x.json sha256:{digest}"], receipts=[], root=tmp_path)
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is True

    def test_mismatch_is_decidable_fail(self, tmp_path: Path) -> None:
        _write(tmp_path, "x.json", b'{"a": 1}')
        obs = run_acceptance_checks([f"sha_matches:x.json sha256:{'0' * 64}"], receipts=[], root=tmp_path)
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is False
        assert item["reason"] == "SHA_MISMATCH"

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        obs = run_acceptance_checks([f"sha_matches:gone.json sha256:{'0' * 64}"], receipts=[], root=tmp_path)
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is False
        assert item["reason"] == "FILE_NOT_FOUND"

    def test_no_workspace_unverified(self) -> None:
        obs = run_acceptance_checks([f"sha_matches:x.json sha256:{'0' * 64}"], receipts=[], root=None)
        item = _one(obs)
        assert item["checked"] is False and item["holds"] is None
        assert item["reason"] == "WORKSPACE_UNAVAILABLE"


class TestMixedBatch:
    def test_new_families_coexist_with_existing(self, tmp_path: Path) -> None:
        digest = _write(tmp_path, "report.json", b'{"final": true}')
        _write(tmp_path, "archive/report.json", b'{"final": false}')
        criteria = [
            "file:report.json non-empty",
            f"sha_matches:report.json sha256:{digest}",
            "report_not_stale:report.json differs_from:archive/report.json",
            "报告质量高",
        ]
        obs = run_acceptance_checks(criteria, receipts=[], root=tmp_path)
        assert len(obs) == 4
        assert [item["holds"] for item in obs[:3]] == [True, True, True]
        assert obs[3]["checked"] is False
        assert obs[3]["reason"] == "UNDECIDABLE_IN_CODE"

    def test_holds_reason_not_mislabeled_as_internal_error(self, tmp_path: Path) -> None:
        """P5#11 (r15 live): a decided-holds leaf must not carry CHECK_INTERNAL_ERROR."""
        _write(tmp_path, "a.json", b"x")
        obs = run_acceptance_checks(["file:a.json non-empty"], receipts=[], root=tmp_path)
        item = _one(obs)
        assert item["checked"] is True and item["holds"] is True
        assert item["reason"] == ""
        assert render_acceptance_observation(item) == "HOLDS [decided]"
