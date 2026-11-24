"""Durable report waitlist and batch readiness consolidation."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate

BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REPO_REF_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]{1,100}$")
MIGRATION_TERMINAL = {"VERIFIED", "FAILED", "REQUIRES_REVIEW"}
MIGRATION_FAILURE = {"FAILED", "REQUIRES_REVIEW"}
# GATED is the rung above VERIFIED (the zero-brand gate passed). Both are positive
# migration outcomes; READY is declared only by this component, never by CodeFoundry.
MIGRATION_POSITIVE = {"VERIFIED", "GATED"}
# A bare status string is a claim, not evidence. All six CodeFoundry checks must be
# present and true before a repository may count toward readiness.
REQUIRED_MIGRATION_CHECKS = ("created", "named", "owned", "gated", "license_kept", "credits")
MANIFEST_KINDS = {"migration-report", "upstream-report"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validate_batch_id(batch_id: Any) -> str:
    batch_id = _require_str(batch_id, "batch_id")
    if not BATCH_ID_RE.fullmatch(batch_id):
        raise ValueError(f"batch_id is invalid: {batch_id!r}")
    if Path(batch_id).name != batch_id or "/" in batch_id or "\\" in batch_id or ".." in batch_id:
        raise ValueError(f"batch_id is unsafe: {batch_id!r}")
    return batch_id


def _validate_repo_ref(value: Any, label: str) -> str:
    ref = _require_str(value, label)
    if not REPO_REF_RE.fullmatch(ref):
        raise ValueError(f"{label} is invalid: {ref!r}")
    return ref


def _safe_file_component(repo_ref: str) -> str:
    _validate_repo_ref(repo_ref, "repo_ref")
    component = repo_ref.replace("/", "__")
    if "/" in component or "\\" in component or ".." in component or Path(component).name != component:
        raise ValueError(f"unsafe file component: {repo_ref!r}")
    return component


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    """Replace ``path`` with a complete document using a private temp file.

    Temp names must be unique per call, not per process: several threads can
    ingest into one batch concurrently, and a shared PID-scoped name lets one
    writer replace or unlink another writer's temp file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    data = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _manifest_entry(
    kind: str,
    file: str,
    source: str,
    destination: str,
    status: Any,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": kind,
        "file": file,
        "source": source,
        "destination": destination,
        "status": status,
    }
    if evidence is not None:
        entry["evidence"] = evidence
    return entry


def _migration_evidence(report: dict[str, Any]) -> dict[str, Any]:
    """Extract the verifiable claims from a migration report into the manifest.

    Missing or non-boolean checks are recorded as False so consolidation fails
    closed rather than treating an absent field as a pass.
    """
    checks = report.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    receipt = report.get("receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    evidence: dict[str, Any] = {name: checks.get(name) is True for name in REQUIRED_MIGRATION_CHECKS}
    for key in ("source_sha", "destination_sha"):
        value = receipt.get(key)
        if isinstance(value, str) and value:
            evidence[key] = value
    license_info = report.get("license")
    if isinstance(license_info, dict) and license_info.get("review_required") is True:
        evidence["license_review_required"] = True
    return evidence


class Bridge:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.reports = self.root / "reports" / "batches"
        self.registry = self.root / "registry"
        self.reports.mkdir(parents=True, exist_ok=True)
        self.registry.mkdir(parents=True, exist_ok=True)

    def batch_dir(self, batch_id: str) -> Path:
        batch_id = _validate_batch_id(batch_id)
        d = (self.reports / batch_id).resolve()
        root = self.reports.resolve()
        if root != d and root not in d.parents:
            raise ValueError(f"batch_id escapes reports root: {batch_id!r}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _manifest_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "manifest.json"

    def _manifest_lock_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "manifest.json.lock"

    @contextmanager
    def _manifest_lock(self, batch_id: str) -> Iterator[None]:
        """Serialize the manifest read-modify-write cycle for one batch.

        ``consolidate`` turns manifest entries into a publication readiness
        verdict. Without an exclusive lock, two ingests can each read the same
        snapshot and the last writer can erase a newer entry, making the gate
        fail open. The lock file is an implementation detail, never report
        evidence, so it is excluded from version control.
        """
        batch_id = _validate_batch_id(batch_id)
        lock_path = self._manifest_lock_path(batch_id)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load_manifest(self, batch_id: str) -> dict[str, Any]:
        batch_id = _validate_batch_id(batch_id)
        p = self._manifest_path(batch_id)
        if not p.exists():
            return {"schema_version": 1, "batch_id": batch_id, "reports": [], "updated_at": None}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"manifest is corrupt for batch {batch_id}: {e}") from e
        manifest = _require_dict(raw, "manifest")
        manifest_batch = manifest.get("batch_id")
        if manifest_batch != batch_id:
            raise ValueError(f"manifest batch_id mismatch: expected {batch_id!r}, got {manifest_batch!r}")
        reports = manifest.get("reports", [])
        if not isinstance(reports, list):
            raise ValueError(f"manifest reports must be a list for batch {batch_id}")
        manifest["reports"] = reports
        manifest.setdefault("schema_version", 1)
        manifest.setdefault("updated_at", None)
        return manifest

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest = _require_dict(manifest, "manifest")
        batch_id = _validate_batch_id(manifest.get("batch_id"))
        with self._manifest_lock(batch_id):
            self._save_manifest_unlocked(manifest)

    def _save_manifest_unlocked(self, manifest: dict[str, Any]) -> None:
        manifest = _require_dict(manifest, "manifest")
        batch_id = _validate_batch_id(manifest.get("batch_id"))
        reports = manifest.get("reports", [])
        if not isinstance(reports, list):
            raise ValueError("manifest reports must be a list")
        normalized_reports: list[dict[str, Any]] = []
        for entry in reports:
            if not isinstance(entry, dict):
                raise ValueError("manifest report entry must be an object")
            normalized_reports.append(entry)
        manifest["schema_version"] = 1
        manifest["batch_id"] = batch_id
        manifest["reports"] = sorted(
            normalized_reports,
            key=lambda r: (
                str(r.get("kind", "")),
                str(r.get("destination", "")),
                str(r.get("file", "")),
                str(r.get("source", "")),
            ),
        )
        manifest["updated_at"] = now_iso()
        _atomic_write_json(self._manifest_path(batch_id), manifest)

    def _upsert_manifest_entry(self, batch_id: str, entry: dict[str, Any], *, kind: str, destination: str) -> None:
        # The whole read-modify-write cycle must be under one lock. Splitting
        # the lock around only save_manifest would still allow two writers to
        # read the same snapshot and lose an update.
        with self._manifest_lock(batch_id):
            manifest = self.load_manifest(batch_id)
            reports = []
            for r in manifest.get("reports", []):
                if not isinstance(r, dict):
                    raise ValueError(f"manifest contains non-object report entry for batch {batch_id}")
                if r.get("kind") == kind and r.get("destination") == destination:
                    continue
                reports.append(r)
            reports.append(entry)
            manifest["reports"] = reports
            self._save_manifest_unlocked(manifest)

    def ingest_migration_report(self, report: dict[str, Any]) -> Path:
        report = _require_dict(report, "migration-report")
        validate("migration-report", report)
        batch_id = _validate_batch_id(report["batch_id"])
        source = _validate_repo_ref(report["source"], "source")
        destination = _validate_repo_ref(report["destination"], "destination")
        status = _require_str(report.get("status"), "status")
        receipt = _require_dict(report.get("receipt"), "receipt")
        expected_key = f"{batch_id}::{source}"
        if receipt.get("idempotency_key") != expected_key:
            raise ValueError(
                f"migration-report idempotency_key mismatch: expected {expected_key!r}, got {receipt.get('idempotency_key')!r}"
            )

        bdir = self.batch_dir(batch_id)
        name = f"migration-report.{_safe_file_component(destination)}.json"
        out = bdir / name
        _atomic_write_json(out, report)

        entry = _manifest_entry(
            kind="migration-report",
            file=name,
            source=source,
            destination=destination,
            status=status,
            evidence=_migration_evidence(report),
        )
        self._upsert_manifest_entry(batch_id, entry, kind="migration-report", destination=destination)
        return out

    def ingest_upstream_report(self, report: dict[str, Any], *, batch_id: str) -> Path:
        report = _require_dict(report, "upstream-report")
        validate("upstream-report", report)
        batch_id = _validate_batch_id(batch_id)
        destination = _validate_repo_ref(report["repo"], "repo")
        source = _validate_repo_ref(report["upstream"], "upstream")
        upstream_state = report.get("upstream_state")
        if upstream_state is not None and not isinstance(upstream_state, str):
            raise ValueError("upstream_state must be a string when present")

        bdir = self.batch_dir(batch_id)
        name = f"upstream-report.{_safe_file_component(destination)}.json"
        out = bdir / name
        _atomic_write_json(out, report)

        entry = _manifest_entry(
            kind="upstream-report",
            file=name,
            source=source,
            destination=destination,
            status=upstream_state,
        )
        self._upsert_manifest_entry(batch_id, entry, kind="upstream-report", destination=destination)
        return out

    def _normalize_expected(self, expected: Any) -> list[str]:
        if not isinstance(expected, list):
            raise TypeError("expected must be a list of repository refs")
        refs: list[str] = []
        for i, item in enumerate(expected):
            refs.append(_validate_repo_ref(item, f"expected[{i}]"))
        # Deterministic, deduped; preserve no order dependency from caller.
        return sorted(set(refs))

    def consolidate(self, batch_id: str, expected: list[str]) -> dict[str, Any]:
        batch_id = _validate_batch_id(batch_id)
        expected_norm = self._normalize_expected(expected)
        manifest = self.load_manifest(batch_id)

        by_source: dict[str, dict[str, Any]] = {}
        duplicate_sources: set[str] = set()
        for entry in manifest.get("reports", []):
            if not isinstance(entry, dict):
                raise ValueError(f"manifest contains non-object report entry for batch {batch_id}")
            if entry.get("kind") != "migration-report":
                continue
            src = entry.get("source")
            dst = entry.get("destination")
            if not isinstance(src, str) or not isinstance(dst, str):
                raise ValueError(f"manifest migration-report entry missing source/destination for batch {batch_id}")
            prev = by_source.get(src)
            if prev is not None and prev.get("destination") != dst:
                duplicate_sources.add(src)
            by_source[src] = entry

        verified: list[str] = []
        failed: list[dict[str, str]] = []
        pending: list[dict[str, str]] = []

        for src in expected_norm:
            if src in duplicate_sources:
                failed.append({"repo": src, "reason": "DUPLICATE_SOURCE"})
                continue
            match = by_source.get(src)
            if not match:
                pending.append({"repo": src, "status": "QUEUED"})
                continue
            status = match.get("status")
            if status in MIGRATION_FAILURE:
                failed.append({"repo": src, "reason": str(status)})
                continue
            if status not in MIGRATION_POSITIVE:
                pending.append({"repo": src, "status": str(status) if isinstance(status, str) and status else "QUEUED"})
                continue

            evidence = match.get("evidence")
            if not isinstance(evidence, dict):
                failed.append({"repo": src, "reason": "MISSING_EVIDENCE"})
                continue
            if evidence.get("license_review_required") is True:
                failed.append({"repo": src, "reason": "LICENSE_REVIEW_REQUIRED"})
                continue
            missing = [name for name in REQUIRED_MIGRATION_CHECKS if evidence.get(name) is not True]
            if missing:
                failed.append({"repo": src, "reason": "EVIDENCE_INCOMPLETE:" + ",".join(missing)})
                continue
            verified.append(src)

        verified = sorted(set(verified))
        ready = (
            len(expected_norm) > 0
            and len(failed) == 0
            and len(pending) == 0
            and set(verified) == set(expected_norm)
        )

        blocking: list[str] = []
        for item in failed:
            blocking.append(f"FAILED:{item['repo']}:{item['reason']}")
        for item in pending:
            blocking.append(f"PENDING:{item['repo']}:{item['status']}")
        if len(expected_norm) == 0:
            blocking.append("EMPTY_EXPECTED")

        report = {
            "schema_version": 1,
            "batch_id": batch_id,
            "expected": expected_norm,
            "verified": verified,
            "failed": failed,
            "pending": pending,
            "ready": ready,
            "generated_at": now_iso(),
            "blocking": blocking,
        }
        validate("batch-readiness-report", report)
        out = self.batch_dir(batch_id) / "batch-readiness-report.json"
        _atomic_write_json(out, report)
        return report
