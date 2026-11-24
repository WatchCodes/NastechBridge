"""Cross-repo contract compatibility using a real CodeFoundry report.

The fixture is a byte copy of CodeFoundry's lab report
``tests/fixtures/lab/created_but_unverified.report.json``. Its hash is pinned so
an accidental replacement cannot silently turn this into a hand-shaped test.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nastechbridge.bridge import Bridge
from nastechbridge.contracts import is_valid

FIXTURE = Path(__file__).parent / "fixtures" / "contracts" / "codefoundry-created-but-unverified.report.json"
FIXTURE_SHA256 = "5e89d706c738768cc1b5f4f74fcc0afbbec572b11dad0d114c51c68f1de1455e"


def test_fixture_is_the_exact_producer_report():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_real_codefoundry_report_is_accepted_and_fails_closed(tmp_path):
    report = json.loads(FIXTURE.read_text(encoding="utf-8"))

    ok, errors = is_valid("migration-report", report)
    assert ok, errors

    bridge = Bridge(tmp_path / "bridge")
    bridge.ingest_migration_report(report)
    verdict = bridge.consolidate(report["batch_id"], [report["source"]])

    assert verdict["ready"] is False
    assert verdict["verified"] == []
    assert verdict["failed"] == [{"repo": "Test/Lab", "reason": "REQUIRES_REVIEW"}]
    assert f"FAILED:{report['source']}:REQUIRES_REVIEW" in verdict["blocking"]

    ingested = next(
        e for e in bridge.load_manifest(report["batch_id"])["reports"]
        if e.get("kind") == "migration-report"
    )
    assert ingested["source"] == report["source"]
    assert ingested["evidence"]["created"] is True
