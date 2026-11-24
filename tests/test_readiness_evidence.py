"""Regression tests: readiness must trust evidence, not a bare status string."""
from __future__ import annotations

from nastechbridge.bridge import Bridge


def _report(*, status: str = "VERIFIED", checks: dict | None = None) -> dict:
    doc = {
        "schema_version": 1,
        "batch_id": "ev-batch",
        "source": "Owner/Repo",
        "destination": "WatchCodes/nastech-repo",
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
            "idempotency_key": "ev-batch::Owner/Repo",
            "destination_repo_id": 42,
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
    if checks is not None:
        doc["checks"] = checks
    return doc


def _ingest_and_consolidate(tmp_path, report: dict) -> dict:
    bridge = Bridge(tmp_path / "bridge")
    bridge.ingest_migration_report(report)
    return bridge.consolidate("ev-batch", [report["source"]])


def test_verified_status_without_gate_evidence_is_not_ready(tmp_path):
    report = _report(status="VERIFIED")
    report["checks"]["gated"] = False

    readiness = _ingest_and_consolidate(tmp_path, report)

    assert readiness["ready"] is False
    assert "Owner/Repo" not in readiness["verified"]
    assert any(item["repo"] == "Owner/Repo" for item in readiness["failed"])
    assert any("EVIDENCE_INCOMPLETE" in line for line in readiness["blocking"])


def test_verified_status_with_wrong_owner_evidence_is_not_ready(tmp_path):
    report = _report(status="VERIFIED")
    report["checks"]["owned"] = False

    readiness = _ingest_and_consolidate(tmp_path, report)

    assert readiness["ready"] is False
    assert any(
        "owned" in item.get("reason", "") for item in readiness["failed"]
    ), readiness["failed"]


def test_gated_status_with_full_evidence_is_verified(tmp_path):
    # GATED is the rung above VERIFIED (zero-brand gate passed); it is not pending.
    readiness = _ingest_and_consolidate(tmp_path, _report(status="GATED"))

    assert readiness["ready"] is True
    assert readiness["verified"] == ["Owner/Repo"]
    assert readiness["failed"] == []
    assert readiness["pending"] == []
    assert readiness["blocking"] == []


def test_manifest_entry_carries_evidence(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    bridge.ingest_migration_report(_report(status="VERIFIED"))

    entries = [
        e for e in bridge.load_manifest("ev-batch")["reports"]
        if e.get("kind") == "migration-report"
    ]
    assert len(entries) == 1
    evidence = entries[0].get("evidence")
    assert isinstance(evidence, dict)
    assert evidence["gated"] is True
    assert evidence["named"] is True
    assert evidence["owned"] is True
    assert evidence["created"] is True


def test_full_checks_still_ready(tmp_path):
    readiness = _ingest_and_consolidate(tmp_path, _report(status="VERIFIED"))
    assert readiness["ready"] is True
    assert readiness["verified"] == ["Owner/Repo"]
