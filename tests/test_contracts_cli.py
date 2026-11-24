import json

import pytest

from nastechbridge import contracts
from nastechbridge.cli import main


def _migration_report(status="VERIFIED"):
    return {
        "schema_version": 1,
        "batch_id": "b1",
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
            "idempotency_key": "b1::Owner/Repo",
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


def _upstream_report():
    return {
        "schema_version": 1,
        "repo": "WatchCodes/nastech-repo",
        "upstream": "Owner/Repo",
        "upstream_sha": "c" * 40,
        "behind_by": 2,
        "detected_at": "2026-01-01T00:00:00Z",
    }


def test_known_schemas_include_contracts():
    names = contracts.known_schemas()
    for expected in [
        "discovery-manifest",
        "migration-request",
        "repository-mapping",
        "migration-report",
        "upstream-report",
        "batch-readiness-report",
    ]:
        assert expected in names


def test_validate_uses_codefoundry_schema():
    contracts.validate("migration-report", _migration_report())


def test_is_valid_reports_errors():
    ok, errors = contracts.is_valid("migration-report", {"schema_version": 1})
    assert ok is False
    assert errors


def test_unknown_schema_is_fail_closed():
    with pytest.raises(contracts.UnknownSchemaError):
        contracts.load_schema("unknown-schema")
    with pytest.raises(contracts.UnknownSchemaError):
        contracts.validate("unknown-schema", {})
    with pytest.raises(contracts.UnknownSchemaError):
        contracts.is_valid("unknown-schema", {})


def test_cli_validate_success(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_migration_report()), encoding="utf-8")
    rc = main(["validate", "--schema", "migration-report", "--file", str(path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["errors"] == []


def test_cli_validate_unknown_schema_fail_closed(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_migration_report()), encoding="utf-8")
    rc = main(["validate", "--schema", "not-a-schema", "--file", str(path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ok"] is False
    assert "unknown schema" in out["error"]
    assert "known_schemas" in out


def test_cli_validate_invalid_document(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    rc = main(["validate", "--schema", "migration-report", "--file", str(path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False
    assert out["errors"]


def test_cli_ingest_and_status(tmp_path, capsys):
    root = tmp_path / "bridge"
    report_path = tmp_path / "migration-report.json"
    report_path.write_text(json.dumps(_migration_report()), encoding="utf-8")

    rc = main(["ingest", "--root", str(root), "--kind", "migration-report", "--file", str(report_path)])
    ingest_out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert ingest_out["ok"] is True
    assert ingest_out["kind"] == "migration-report"

    rc = main(["status", "--root", str(root), "--batch-id", "b1"])
    status_out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert status_out["ok"] is True
    assert status_out["batch_id"] == "b1"
    assert status_out["manifest"]["batch_id"] == "b1"
    assert len(status_out["manifest"]["reports"]) == 1


def test_cli_ingest_upstream_report_requires_batch_id(tmp_path, capsys):
    root = tmp_path / "bridge"
    report_path = tmp_path / "upstream-report.json"
    report_path.write_text(json.dumps(_upstream_report()), encoding="utf-8")
    rc = main(["ingest", "--root", str(root), "--kind", "upstream-report", "--file", str(report_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ok"] is False
    assert "batch-id" in out["error"]


def test_cli_ingest_upstream_report(tmp_path, capsys):
    root = tmp_path / "bridge"
    report_path = tmp_path / "upstream-report.json"
    report_path.write_text(json.dumps(_upstream_report()), encoding="utf-8")
    rc = main(
        [
            "ingest",
            "--root",
            str(root),
            "--kind",
            "upstream-report",
            "--file",
            str(report_path),
            "--batch-id",
            "b1",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["kind"] == "upstream-report"


def test_cli_consolidate_ready(tmp_path, capsys):
    root = tmp_path / "bridge"
    report_path = tmp_path / "migration-report.json"
    report_path.write_text(json.dumps(_migration_report()), encoding="utf-8")
    assert main(["ingest", "--root", str(root), "--kind", "migration-report", "--file", str(report_path)]) == 0
    capsys.readouterr()

    rc = main(["consolidate", "--root", str(root), "--batch-id", "b1", "--expected", "Owner/Repo"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert out["ready"] is True
    assert out["verified"] == ["Owner/Repo"]
    readiness = root / "reports" / "batches" / "b1" / "batch-readiness-report.json"
    assert readiness.exists()
    assert json.loads(readiness.read_text(encoding="utf-8"))["ready"] is True


def test_cli_consolidate_not_ready_fails_closed(tmp_path, capsys):
    root = tmp_path / "bridge"
    rc = main(["consolidate", "--root", str(root), "--batch-id", "b1", "--expected", "Owner/Repo"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is True
    assert out["ready"] is False
    assert "PENDING:Owner/Repo:QUEUED" in out["blocking"]


def test_cli_consolidate_rejects_bad_expected(tmp_path, capsys):
    rc = main(["consolidate", "--root", str(tmp_path / "bridge"), "--batch-id", "b1", "--expected", "../bad"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ok"] is False


def test_migration_report_accepts_every_codefoundry_checkpoint_stage():
    stages = (
        "start", "validate", "receipt", "scan", "license", "clone",
        "brand", "credits", "gate", "ship", "report",
    )
    report = _migration_report(status="REQUIRES_REVIEW")
    report["checks"]["created"] = False
    report["error"] = "checks not satisfied: created"
    report["receipt"]["checkpoints"] = [
        {"stage": s, "at": "2026-11-24T00:00:00Z", "ok": True} for s in stages
    ]
    ok, errors = contracts.is_valid("migration-report", report)
    assert ok, errors


def test_migration_report_still_rejects_unknown_checkpoint_stage():
    report = _migration_report()
    report["receipt"]["checkpoints"] = [
        {"stage": "not_a_real_stage", "at": "2026-11-24T00:00:00Z", "ok": True}
    ]
    ok, errors = contracts.is_valid("migration-report", report)
    assert ok is False
    assert any("not_a_real_stage" in e for e in errors)
