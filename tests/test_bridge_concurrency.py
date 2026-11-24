"""Concurrency regression tests for the durable manifest waitlist.

A readiness verdict is derived from manifest.json. An unsynchronized
read-modify-write can let a stale entry replace a newer one and make the
fail-closed gate declare READY from a lost update. These tests pin the
one-writer-per-file rule at the filesystem boundary.
"""
from __future__ import annotations

import fcntl
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest

from nastechbridge import bridge as bridge_module
from nastechbridge.bridge import Bridge


def _report(source: str, destination: str) -> dict:
    return {
        "schema_version": 1,
        "batch_id": "b1",
        "source": source,
        "destination": destination,
        "status": "VERIFIED",
        "checks": {
            "created": True,
            "named": True,
            "owned": True,
            "gated": True,
            "license_kept": True,
            "credits": True,
        },
        "receipt": {
            "idempotency_key": f"b1::{source}",
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


def test_atomic_write_fsyncs_before_replace(tmp_path, monkeypatch):
    events: list[str] = []
    real_fsync = bridge_module.os.fsync
    real_replace = bridge_module.os.replace

    def tracked_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def tracked_replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(bridge_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(bridge_module.os, "replace", tracked_replace)

    bridge_module._atomic_write_json(tmp_path / "manifest.json", {"schema_version": 1})
    assert events == ["fsync", "replace"]


def test_manifest_lock_is_exclusive(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    with bridge._manifest_lock("b1"):
        lock_path = bridge._manifest_lock_path("b1")
        with lock_path.open("r+", encoding="utf-8") as fh, pytest.raises(BlockingIOError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    # The lock is released on exit; a fresh writer can acquire it.
    with lock_path.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def test_ingest_waits_for_external_manifest_lock(tmp_path):
    bridge = Bridge(tmp_path / "bridge")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            bridge.ingest_migration_report, _report("Owner/Repo", "WatchCodes/nastech-repo")
        )
        with bridge._manifest_lock("b1"):
            time.sleep(0.3)
            assert not future.done(), "ingest completed while another writer held the manifest lock"
        # ThreadPoolExecutor re-raises any worker failure here.
        future.result(timeout=3)

    manifest = bridge.load_manifest("b1")
    assert len(manifest["reports"]) == 1


def test_concurrent_ingests_preserve_all_entries_and_use_unique_temp_files(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / "bridge")
    original_replace = bridge_module.os.replace
    temp_paths: list[str] = []
    temp_paths_lock = Lock()

    def tracked_replace(src, dst, *args, **kwargs):
        with temp_paths_lock:
            temp_paths.append(str(src))
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(bridge_module.os, "replace", tracked_replace)

    count = 8
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [
            pool.submit(
                bridge.ingest_migration_report,
                _report(f"Owner/Repo{i}", f"WatchCodes/nastech-repo{i}"),
            )
            for i in range(count)
        ]
        for future in futures:
            future.result(timeout=10)

    manifest = bridge.load_manifest("b1")
    migration_entries = [e for e in manifest["reports"] if e["kind"] == "migration-report"]
    assert len(migration_entries) == count
    assert {e["source"] for e in migration_entries} == {f"Owner/Repo{i}" for i in range(count)}
    assert len(temp_paths) == len(set(temp_paths)), "two writers reused an atomic-write temp path"

    verdict = bridge.consolidate("b1", [f"Owner/Repo{i}" for i in range(count)])
    assert verdict["ready"] is True
