from nastechbridge.bridge import Bridge


def _report(status="VERIFIED"):
    return {
        "schema_version": 1,
        "batch_id": "b1",
        "source": "Owner/Repo",
        "destination": "WatchCodes/nastech-repo",
        "status": status,
        "checks": {"created": True, "named": True, "owned": True, "gated": True, "license_kept": True, "credits": True},
        "receipt": {"idempotency_key": "b1::Owner/Repo", "destination_repo_id": 1, "source_sha": "a" * 40, "destination_sha": "b" * 40, "checkpoints": []},
        "license": {"detected": "MIT", "preserved": True},
        "attribution": {"notice": True, "credits": True},
        "verification": {"files_checked": 1, "branding_findings": 0, "integrity": True},
        "pr_url": None,
        "error": None,
    }


def test_consolidate_ready(tmp_path):
    b = Bridge(tmp_path / "bridge")
    b.ingest_migration_report(_report("VERIFIED"))
    ready = b.consolidate("b1", ["Owner/Repo"])
    assert ready["ready"] is True
    assert ready["verified"] == ["Owner/Repo"]


def test_consolidate_pending(tmp_path):
    b = Bridge(tmp_path / "bridge2")
    ready = b.consolidate("b2", ["Owner/Repo"])
    assert ready["ready"] is False
    assert ready["pending"] == [{"repo": "Owner/Repo", "status": "QUEUED"}]
