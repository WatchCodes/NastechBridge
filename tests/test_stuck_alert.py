"""24h stuck non-terminal enforcement for the consolidation gate."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nastechbridge.bridge import Bridge


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _manifest(bridge: Bridge, received_at: str, status: str = "QUEUED") -> None:
    bridge.save_manifest(
        {
            "schema_version": 1,
            "batch_id": "stuck-batch",
            "reports": [
                {
                    "kind": "migration-report",
                    "file": "migration-report.WatchCodes__nastech-repo.json",
                    "source": "Owner/Repo",
                    "destination": "WatchCodes/nastech-repo",
                    "status": status,
                    "received_at": received_at,
                }
            ],
        }
    )


def test_stale_non_terminal_report_is_review_blocking(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    stale = datetime.now(UTC) - timedelta(hours=25)
    _manifest(bridge, _iso(stale))

    readiness = bridge.consolidate("stuck-batch", ["Owner/Repo"])

    assert readiness["ready"] is False
    assert readiness["verified"] == []
    assert readiness["failed"] == [
        {"repo": "Owner/Repo", "reason": "STUCK_REQUIRES_REVIEW:QUEUED"}
    ]
    assert readiness["pending"] == []
    assert "STUCK:Owner/Repo:QUEUED" in readiness["blocking"]


def test_fresh_non_terminal_report_is_only_pending(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    _manifest(bridge, _iso(datetime.now(UTC)))

    readiness = bridge.consolidate("stuck-batch", ["Owner/Repo"])

    assert readiness["ready"] is False
    assert readiness["failed"] == []
    assert readiness["pending"] == [{"repo": "Owner/Repo", "status": "QUEUED"}]
    assert "PENDING:Owner/Repo:QUEUED" in readiness["blocking"]
    assert not any("STUCK" in line for line in readiness["blocking"])


def test_manifest_without_received_at_is_not_falsely_stuck(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    bridge.save_manifest(
        {
            "schema_version": 1,
            "batch_id": "stuck-batch",
            "reports": [
                {
                    "kind": "migration-report",
                    "file": "migration-report.WatchCodes__nastech-repo.json",
                    "source": "Owner/Repo",
                    "destination": "WatchCodes/nastech-repo",
                    "status": "QUEUED",
                }
            ],
        }
    )

    readiness = bridge.consolidate("stuck-batch", ["Owner/Repo"])

    assert readiness["pending"] == [{"repo": "Owner/Repo", "status": "QUEUED"}]
    assert not any("STUCK" in line for line in readiness["blocking"])


def test_ingest_records_and_preserves_received_at(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    report = {
        "schema_version": 1,
        "batch_id": "stuck-batch",
        "source": "Owner/Repo",
        "destination": "WatchCodes/nastech-repo",
        "status": "QUEUED",
        "checks": {
            "created": True, "named": True, "owned": True,
            "gated": True, "license_kept": True, "credits": True,
        },
        "receipt": {
            "idempotency_key": "stuck-batch::Owner/Repo",
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
    bridge.ingest_migration_report(report)
    first = next(
        e for e in bridge.load_manifest("stuck-batch")["reports"]
        if e.get("kind") == "migration-report"
    )
    assert isinstance(first.get("received_at"), str) and first["received_at"].endswith("Z")

    # Re-ingesting the same idempotency key must not reset the stuck clock.
    bridge.ingest_migration_report(report)
    second = next(
        e for e in bridge.load_manifest("stuck-batch")["reports"]
        if e.get("kind") == "migration-report"
    )
    assert second["received_at"] == first["received_at"]
