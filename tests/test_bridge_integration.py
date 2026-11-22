import json

import pytest
from jsonschema.exceptions import ValidationError

from nastechbridge.bridge import Bridge


def _migration_report(
    *,
    batch_id="batch-1",
    source="Owner/Repo",
    destination="WatchCodes/nastech-repo",
    status="VERIFIED",
    error=None,
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
        "error": error,
    }


def _upstream_report(
    *,
    repo="WatchCodes/nastech-repo",
    upstream="Owner/Repo",
    upstream_sha="c" * 40,
    behind_by=0,
    detected_at="2026-01-01T00:00:00Z",
    upstream_state="active",
):
    report = {
        "schema_version": 1,
        "repo": repo,
        "upstream": upstream,
        "upstream_sha": upstream_sha,
        "behind_by": behind_by,
        "detected_at": detected_at,
    }
    if upstream_state is not None:
        report["upstream_state"] = upstream_state
    return report


def test_ingest_migration_report_writes_file_and_manifest(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    report = _migration_report()

    path = bridge.ingest_migration_report(report)

    assert path.exists()
    assert path.name == "migration-report.WatchCodes__nastech-repo.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == report

    manifest = bridge.load_manifest("batch-1")
    assert manifest["batch_id"] == "batch-1"
    assert manifest["updated_at"] is not None
    assert manifest["reports"] == [
        {
            "kind": "migration-report",
            "file": "migration-report.WatchCodes__nastech-repo.json",
            "source": "Owner/Repo",
            "destination": "WatchCodes/nastech-repo",
            "status": "VERIFIED",
        }
    ]


def test_ingest_upstream_report_writes_file_and_manifest(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    report = _upstream_report()

    path = bridge.ingest_upstream_report(report, batch_id="batch-1")

    assert path.exists()
    assert path.name == "upstream-report.WatchCodes__nastech-repo.json"
    assert json.loads(path.read_text(encoding="utf-8")) == report

    manifest = bridge.load_manifest("batch-1")
    assert manifest["reports"] == [
        {
            "kind": "upstream-report",
            "file": "upstream-report.WatchCodes__nastech-repo.json",
            "source": "Owner/Repo",
            "destination": "WatchCodes/nastech-repo",
            "status": "active",
        }
    ]


def test_manifest_consolidates_mixed_reports_and_preserves_kinds(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    bridge.ingest_migration_report(
        _migration_report(source="Owner/One", destination="WatchCodes/nastech-one", status="VERIFIED")
    )
    bridge.ingest_migration_report(
        _migration_report(source="Owner/Two", destination="WatchCodes/nastech-two", status="FAILED", error="gate failed")
    )
    bridge.ingest_upstream_report(
        _upstream_report(repo="WatchCodes/nastech-one", upstream="Owner/One", behind_by=3),
        batch_id="batch-1",
    )

    manifest = bridge.load_manifest("batch-1")

    assert len(manifest["reports"]) == 3
    kinds = {(r["kind"], r["destination"]) for r in manifest["reports"]}
    assert kinds == {
        ("migration-report", "WatchCodes/nastech-one"),
        ("migration-report", "WatchCodes/nastech-two"),
        ("upstream-report", "WatchCodes/nastech-one"),
    }


def test_consolidate_reports_ready_only_when_all_expected_verified(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    bridge.ingest_migration_report(
        _migration_report(source="Owner/One", destination="WatchCodes/nastech-one", status="VERIFIED")
    )
    bridge.ingest_migration_report(
        _migration_report(source="Owner/Two", destination="WatchCodes/nastech-two", status="VERIFIED")
    )

    report = bridge.consolidate("batch-1", ["Owner/Two", "Owner/One"])

    assert report["ready"] is True
    assert report["expected"] == ["Owner/One", "Owner/Two"]
    assert report["verified"] == ["Owner/One", "Owner/Two"]
    assert report["failed"] == []
    assert report["pending"] == []
    assert (bridge.batch_dir("batch-1") / "batch-readiness-report.json").exists()


def test_consolidate_is_fail_closed_for_failed_review_pending_and_missing(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    bridge.ingest_migration_report(
        _migration_report(source="Owner/Ok", destination="WatchCodes/nastech-ok", status="VERIFIED")
    )
    bridge.ingest_migration_report(
        _migration_report(source="Owner/Bad", destination="WatchCodes/nastech-bad", status="FAILED", error="boom")
    )
    bridge.ingest_migration_report(
        _migration_report(
            source="Owner/Review",
            destination="WatchCodes/nastech-review",
            status="REQUIRES_REVIEW",
            error="review",
        )
    )
    bridge.ingest_migration_report(
        _migration_report(source="Owner/Pending", destination="WatchCodes/nastech-pending", status="CREATING")
    )

    report = bridge.consolidate(
        "batch-1",
        ["Owner/Ok", "Owner/Bad", "Owner/Review", "Owner/Pending", "Owner/Missing"],
    )

    assert report["ready"] is False
    assert report["verified"] == ["Owner/Ok"]
    assert {"repo": "Owner/Bad", "reason": "FAILED"} in report["failed"]
    assert {"repo": "Owner/Review", "reason": "REQUIRES_REVIEW"} in report["failed"]
    assert {"repo": "Owner/Pending", "status": "CREATING"} in report["pending"]
    assert {"repo": "Owner/Missing", "status": "QUEUED"} in report["pending"]


def test_consolidate_empty_expected_is_not_ready(tmp_path):
    bridge = Bridge(tmp_path / "bridge")

    report = bridge.consolidate("batch-empty", [])

    assert report["ready"] is False
    assert report["expected"] == []
    assert report["verified"] == []
    assert report["failed"] == []
    assert report["pending"] == []


def test_migration_ingest_is_idempotent_for_same_destination(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    first = _migration_report(status="CREATING")
    second = _migration_report(status="VERIFIED")

    bridge.ingest_migration_report(first)
    bridge.ingest_migration_report(second)

    manifest = bridge.load_manifest("batch-1")
    migration_entries = [r for r in manifest["reports"] if r["kind"] == "migration-report"]
    assert len(migration_entries) == 1
    assert migration_entries[0]["status"] == "VERIFIED"

    on_disk = json.loads((bridge.batch_dir("batch-1") / "migration-report.WatchCodes__nastech-repo.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "VERIFIED"


def test_upstream_ingest_is_idempotent_for_same_destination(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    bridge.ingest_upstream_report(_upstream_report(behind_by=2, upstream_state="active"), batch_id="batch-1")
    bridge.ingest_upstream_report(_upstream_report(behind_by=0, upstream_state="active"), batch_id="batch-1")

    manifest = bridge.load_manifest("batch-1")
    upstream_entries = [r for r in manifest["reports"] if r["kind"] == "upstream-report"]
    assert len(upstream_entries) == 1
    on_disk = json.loads((bridge.batch_dir("batch-1") / "upstream-report.WatchCodes__nastech-repo.json").read_text(encoding="utf-8"))
    assert on_disk["behind_by"] == 0


def test_ingest_migration_report_rejects_invalid_input_without_writing(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    invalid = _migration_report()
    del invalid["receipt"]

    with pytest.raises(ValidationError):
        bridge.ingest_migration_report(invalid)

    batch_dir = bridge.root / "reports" / "batches" / "batch-1"
    assert not (batch_dir / "migration-report.WatchCodes__nastech-repo.json").exists()
    assert not (batch_dir / "manifest.json").exists()


def test_ingest_upstream_report_rejects_invalid_input_without_writing(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    invalid = _upstream_report()
    invalid["behind_by"] = -1

    with pytest.raises(ValidationError):
        bridge.ingest_upstream_report(invalid, batch_id="batch-1")

    batch_dir = bridge.root / "reports" / "batches" / "batch-1"
    assert not (batch_dir / "upstream-report.WatchCodes__nastech-repo.json").exists()
    assert not (batch_dir / "manifest.json").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("status"),
        lambda r: r.__setitem__("status", "UNKNOWN"),
        lambda r: r.__setitem__("extra", True),
        lambda r: r.__setitem__("source", "bad-ref"),
        lambda r: r["receipt"].__setitem__("source_sha", "not-a-sha"),
    ],
)
def test_migration_report_schema_fail_closed(tmp_path, mutate):
    bridge = Bridge(tmp_path / "bridge")
    report = _migration_report()
    mutate(report)

    with pytest.raises(ValidationError):
        bridge.ingest_migration_report(report)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("repo"),
        lambda r: r.__setitem__("upstream_sha", "xyz"),
        lambda r: r.__setitem__("behind_by", "0"),
        lambda r: r.__setitem__("upstream_state", "bogus"),
        lambda r: r.__setitem__("unexpected", "x"),
    ],
)
def test_upstream_report_schema_fail_closed(tmp_path, mutate):
    bridge = Bridge(tmp_path / "bridge")
    report = _upstream_report()
    mutate(report)

    with pytest.raises(ValidationError):
        bridge.ingest_upstream_report(report, batch_id="batch-1")
