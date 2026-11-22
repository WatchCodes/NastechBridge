"""Durable report waitlist and batch readiness consolidation."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .contracts import validate


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Bridge:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.reports = self.root / "reports" / "batches"
        self.registry = self.root / "registry"
        self.reports.mkdir(parents=True, exist_ok=True)
        self.registry.mkdir(parents=True, exist_ok=True)

    def batch_dir(self, batch_id: str) -> Path:
        d = self.reports / batch_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _manifest_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "manifest.json"

    def load_manifest(self, batch_id: str) -> dict[str, Any]:
        p = self._manifest_path(batch_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {"schema_version": 1, "batch_id": batch_id, "reports": [], "updated_at": None}

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = now_iso()
        p = self._manifest_path(manifest["batch_id"])
        p.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def ingest_migration_report(self, report: dict[str, Any]) -> Path:
        validate("migration-report", report)
        batch_id = report["batch_id"]
        bdir = self.batch_dir(batch_id)
        dest = report["destination"].replace("/", "__")
        name = f"migration-report.{dest}.json"
        (bdir / name).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest = self.load_manifest(batch_id)
        manifest.setdefault("reports", [])
        manifest["reports"] = [r for r in manifest["reports"] if not (r.get("kind") == "migration-report" and r.get("destination") == report["destination"])]
        manifest["reports"].append({"kind": "migration-report", "file": name, "source": report["source"], "destination": report["destination"], "status": report["status"]})
        self.save_manifest(manifest)
        return bdir / name

    def ingest_upstream_report(self, report: dict[str, Any], *, batch_id: str) -> Path:
        validate("upstream-report", report)
        bdir = self.batch_dir(batch_id)
        dest = report["repo"].replace("/", "__")
        name = f"upstream-report.{dest}.json"
        (bdir / name).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest = self.load_manifest(batch_id)
        manifest.setdefault("reports", [])
        manifest["reports"] = [r for r in manifest["reports"] if not (r.get("kind") == "upstream-report" and r.get("destination") == report["repo"])]
        manifest["reports"].append({"kind": "upstream-report", "file": name, "source": report["upstream"], "destination": report["repo"], "status": report.get("upstream_state")})
        self.save_manifest(manifest)
        return bdir / name

    def consolidate(self, batch_id: str, expected: list[str]) -> dict[str, Any]:
        manifest = self.load_manifest(batch_id)
        by_dest: dict[str, dict[str, Any]] = {}
        for entry in manifest.get("reports", []):
            if entry.get("kind") == "migration-report":
                by_dest[entry["destination"]] = entry
        verified, failed, pending = [], [], []
        expected_norm = sorted(set(expected))
        for src in expected_norm:
            match = next((e for e in by_dest.values() if e.get("source") == src), None)
            if not match:
                pending.append({"repo": src, "status": "QUEUED"})
            elif match.get("status") == "VERIFIED":
                verified.append(src)
            elif match.get("status") in {"FAILED", "REQUIRES_REVIEW"}:
                failed.append({"repo": src, "reason": match.get("status")})
            else:
                pending.append({"repo": src, "status": match.get("status", "QUEUED")})
        ready = len(verified) == len(expected_norm) and len(failed) == 0 and len(pending) == 0 and len(expected_norm) > 0
        report = {
            "schema_version": 1,
            "batch_id": batch_id,
            "expected": expected_norm,
            "verified": sorted(verified),
            "failed": failed,
            "pending": pending,
            "ready": ready,
            "generated_at": now_iso(),
        }
        validate("batch-readiness-report", report)
        out = self.batch_dir(batch_id) / "batch-readiness-report.json"
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return report
