import json

import pytest

from nastechbridge.bridge import Bridge


def _migration_report(
    *,
    batch_id="b1",
    source="Owner/Repo",
    destination="WatchCodes/nastech-repo",
    status="VERIFIED",
):
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "source": source,
        "destination": destination,
        "status": status,
        "checks": {
            "created": True,
            "named": True,
            "owned": True,
            "gated": True,
            "license_kept": True,
            "credits": True,
        },
        "receipt": {
            "idempotency_key": f"{batch_id}::{source}",
            "destination_repo_id": 1,
            "source_sha": "a" * 40,
            "destination_sha": "b" * 40,
            "checkpoints": [],
        },
        "license": {"detected": "MIT", "preserved": True},
        "attribution": {"notice": True, "credits": True},
        "verification": {"files_checked": 1, "branding_findings": 0, "integrity": True},
        "pr_url": None,
        "error": None,
    }


def _upstream_report(*, repo="WatchCodes/nastech-repo", upstream="Owner/Repo"):
    return {
        "schema_version": 1,
        "repo": repo,
        "upstream": upstream,
        "upstream_sha": "c" * 40,
        "behind_by": 0,
        "detected_at": "2026-01-01T00:00:00Z",
        "upstream_state": "active",
    }


def test_rejects_invalid_batch_ids(tmp_path):
    b = Bridge(tmp_path / "bridge")
    for bad in ["../escape", "..", "a/b", "a\\b", "", " ", "bad batch", 123, None]:
        with pytest.raises((ValueError, TypeError)):
            b.batch_dir(bad)  # type: ignore[arg-type]
        with pytest.raises((ValueError, TypeError)):
            b.load_manifest(bad)  # type: ignore[arg-type]
        with pytest.raises((ValueError, TypeError)):
            b.consolidate(bad, [])  # type: ignore[arg-type]


def test_ingest_migration_report_validates_type_and_schema(tmp_path):
    b = Bridge(tmp_path / "bridge")
    with pytest.raises(TypeError):
        b.ingest_migration_report(["not-a-dict"])  # type: ignore[arg-type]
    bad = _migration_report()
    bad.pop("status")
    with pytest.raises(Exception):
        b.ingest_migration_report(bad)


def test_ingest_migration_report_idempotent_updates_manifest(tmp_path):
    b = Bridge(tmp_path / "bridge")
    r1 = _migration_report(status="FAILED")
    r1["error"] = "boom"
    p1 = b.ingest_migration_report(r1)
    p2 = b.ingest_migration_report(_migration_report(status="VERIFIED"))
    assert p1 == p2
    manifest = b.load_manifest("b1")
    entries = [e for e in manifest["reports"] if e.get("kind") == "migration-report"]
    assert len(entries) == 1
    assert entries[0]["status"] == "VERIFIED"
    assert entries[0]["destination"] == "WatchCodes/nastech-repo"
    on_disk = json.loads(p1.read_text(encoding="utf-8"))
    assert on_disk["status"] == "VERIFIED"


def test_ingest_migration_report_rejects_idempotency_mismatch(tmp_path):
    b = Bridge(tmp_path / "bridge")
    report = _migration_report()
    report["receipt"]["idempotency_key"] = "b1::Other/Repo"
    with pytest.raises(ValueError, match="idempotency_key mismatch"):
        b.ingest_migration_report(report)


def test_ingest_upstream_report_idempotent(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_upstream_report(_upstream_report(), batch_id="b1")
    b.ingest_upstream_report(_upstream_report(), batch_id="b1")
    manifest = b.load_manifest("b1")
    entries = [e for e in manifest["reports"] if e.get("kind") == "upstream-report"]
    assert len(entries) == 1
    assert entries[0]["source"] == "Owner/Repo"
    assert entries[0]["destination"] == "WatchCodes/nastech-repo"


def test_ingest_upstream_rejects_invalid_batch_id(tmp_path):
    b = Bridge(tmp_path / "bridge")
    with pytest.raises(ValueError):
        b.ingest_upstream_report(_upstream_report(), batch_id="../escape")


def test_manifest_corruption_fails_closed(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_migration_report(_migration_report())
    manifest_path = b.batch_dir("b1") / "manifest.json"
    manifest_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        b.load_manifest("b1")
    with pytest.raises(ValueError):
        b.consolidate("b1", ["Owner/Repo"])


def test_manifest_wrong_shape_fails_closed(tmp_path):
    b = Bridge(tmp_path / "bridge")
    p = b.batch_dir("b1") / "manifest.json"
    p.write_text(json.dumps({"batch_id": "b1", "reports": "nope"}), encoding="utf-8")
    with pytest.raises(ValueError, match="reports must be a list"):
        b.load_manifest("b1")


def test_consolidate_validates_expected(tmp_path):
    b = Bridge(tmp_path / "bridge")
    with pytest.raises(TypeError):
        b.consolidate("b1", "Owner/Repo")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        b.consolidate("b1", ["Owner/Repo", "../bad"])
    with pytest.raises(ValueError):
        b.consolidate("b1", ["not-a-ref"])


def test_consolidate_empty_expected_fail_closed(tmp_path):
    b = Bridge(tmp_path / "bridge")
    report = b.consolidate("b1", [])
    assert report["ready"] is False
    assert report["expected"] == []
    assert report["verified"] == []
    assert report["failed"] == []
    assert report["pending"] == []
    assert "EMPTY_EXPECTED" in report["blocking"]


def test_consolidate_status_aggregation_and_blocking(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_migration_report(_migration_report(source="A/Ok", destination="W/a-ok", status="VERIFIED"))
    b.ingest_migration_report(_migration_report(source="B/Fail", destination="W/b-fail", status="FAILED"))
    b.ingest_migration_report(_migration_report(source="C/Review", destination="W/c-review", status="REQUIRES_REVIEW"))
    b.ingest_migration_report(_migration_report(source="D/Creating", destination="W/d-creating", status="CREATING"))
    report = b.consolidate("b1", ["A/Ok", "B/Fail", "C/Review", "D/Creating", "E/Missing"])

    assert report["ready"] is False
    assert report["verified"] == ["A/Ok"]
    assert {"repo": "B/Fail", "reason": "FAILED"} in report["failed"]
    assert {"repo": "C/Review", "reason": "REQUIRES_REVIEW"} in report["failed"]
    assert {"repo": "D/Creating", "status": "CREATING"} in report["pending"]
    assert {"repo": "E/Missing", "status": "QUEUED"} in report["pending"]
    assert "FAILED:B/Fail:FAILED" in report["blocking"]
    assert "FAILED:C/Review:REQUIRES_REVIEW" in report["blocking"]
    assert "PENDING:D/Creating:CREATING" in report["blocking"]
    assert "PENDING:E/Missing:QUEUED" in report["blocking"]


def test_consolidate_deterministic_output(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_migration_report(_migration_report(source="Z/Repo", destination="W/z-repo", status="VERIFIED"))
    b.ingest_migration_report(_migration_report(source="A/Repo", destination="W/a-repo", status="VERIFIED"))
    r1 = b.consolidate("b1", ["Z/Repo", "A/Repo", "Z/Repo"])
    r2 = b.consolidate("b1", ["A/Repo", "Z/Repo"])
    for k in ["schema_version", "batch_id", "expected", "verified", "failed", "pending", "ready", "blocking"]:
        assert r1[k] == r2[k]
    assert r1["expected"] == ["A/Repo", "Z/Repo"]
    assert r1["verified"] == ["A/Repo", "Z/Repo"]
    assert r1["ready"] is True
    assert r1["blocking"] == []


def test_manifest_reports_are_deterministically_sorted(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_upstream_report(_upstream_report(repo="W/z-repo", upstream="Z/Repo"), batch_id="b1")
    b.ingest_migration_report(_migration_report(source="A/Repo", destination="W/a-repo", status="VERIFIED"))
    b.ingest_migration_report(_migration_report(source="M/Repo", destination="W/m-repo", status="FAILED"))
    manifest = b.load_manifest("b1")
    kinds_dests = [(e["kind"], e["destination"]) for e in manifest["reports"]]
    assert kinds_dests == sorted(kinds_dests)


def test_consolidate_fails_closed_on_malformed_manifest_entry(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_migration_report(_migration_report())
    manifest = b.load_manifest("b1")
    manifest["reports"].append("bad")  # type: ignore[arg-type]
    # write back raw to simulate corruption/tamper
    p = b.batch_dir("b1") / "manifest.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="non-object report entry"):
        b.consolidate("b1", ["Owner/Repo"])


def test_consolidate_fails_closed_on_duplicate_source(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_migration_report(_migration_report(source="A/Repo", destination="W/a1", status="VERIFIED"))
    # tamper manifest to simulate inconsistent duplicate source mapping
    manifest = b.load_manifest("b1")
    manifest["reports"].append(
        {
            "kind": "migration-report",
            "file": "migration-report.W__a2.json",
            "source": "A/Repo",
            "destination": "W/a2",
            "status": "VERIFIED",
        }
    )
    p = b.batch_dir("b1") / "manifest.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    report = b.consolidate("b1", ["A/Repo"])
    assert report["ready"] is False
    assert report["failed"] == [{"repo": "A/Repo", "reason": "DUPLICATE_SOURCE"}]
    assert "FAILED:A/Repo:DUPLICATE_SOURCE" in report["blocking"]


def test_ingest_rejects_path_traversal_batch_id(tmp_path):
    b = Bridge(tmp_path / "bridge")
    bad = _migration_report(batch_id="../escape")
    with pytest.raises(Exception):
        b.ingest_migration_report(bad)
