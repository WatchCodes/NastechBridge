"""Minimal CLI for contract validation, report ingest, and batch status."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .bridge import Bridge
from .contracts import UnknownSchemaError, is_valid, known_schemas, validate


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _emit(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        document = _load_json(args.file)
    except Exception as exc:
        _emit({"ok": False, "error": f"failed to read {args.file}: {exc}"})
        return 2
    try:
        ok, errors = is_valid(args.schema, document)
    except UnknownSchemaError as exc:
        _emit({"ok": False, "error": str(exc), "known_schemas": known_schemas()})
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)})
        return 2
    _emit({"ok": ok, "schema": args.schema, "errors": errors})
    return 0 if ok else 1


def cmd_ingest(args: argparse.Namespace) -> int:
    try:
        report = _load_json(args.file)
    except Exception as exc:
        _emit({"ok": False, "error": f"failed to read {args.file}: {exc}"})
        return 2
    if args.kind not in {"migration-report", "upstream-report"}:
        _emit({"ok": False, "error": f"unsupported ingest kind: {args.kind!r}"})
        return 2
    bridge = Bridge(args.root)
    try:
        if args.kind == "migration-report":
            path = bridge.ingest_migration_report(report)
        else:
            if not args.batch_id:
                _emit({"ok": False, "error": "--batch-id is required for upstream-report ingest"})
                return 2
            path = bridge.ingest_upstream_report(report, batch_id=args.batch_id)
    except UnknownSchemaError as exc:
        _emit({"ok": False, "error": str(exc), "known_schemas": known_schemas()})
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)})
        return 1
    _emit({"ok": True, "kind": args.kind, "path": str(path)})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    bridge = Bridge(args.root)
    manifest = bridge.load_manifest(args.batch_id)
    batch_dir = bridge.batch_dir(args.batch_id)
    readiness_path = batch_dir / "batch-readiness-report.json"
    readiness = None
    if readiness_path.exists():
        try:
            readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        except Exception:
            readiness = None
    _emit(
        {
            "ok": True,
            "batch_id": args.batch_id,
            "manifest": manifest,
            "readiness": readiness,
            "readiness_path": str(readiness_path) if readiness_path.exists() else None,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nastechbridge", description="NastechBridge contract CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="Validate a JSON document against a known schema")
    p_validate.add_argument("--schema", required=True, help="Known schema name")
    p_validate.add_argument("--file", required=True, help="Path to JSON document")
    p_validate.set_defaults(func=cmd_validate)

    p_ingest = sub.add_parser("ingest", help="Ingest a report into the bridge")
    p_ingest.add_argument("--root", required=True, help="Bridge root directory")
    p_ingest.add_argument("--kind", required=True, choices=["migration-report", "upstream-report"])
    p_ingest.add_argument("--file", required=True, help="Path to report JSON")
    p_ingest.add_argument("--batch-id", help="Batch id (required for upstream-report)")
    p_ingest.set_defaults(func=cmd_ingest)

    p_status = sub.add_parser("status", help="Show batch manifest and readiness status")
    p_status.add_argument("--root", required=True, help="Bridge root directory")
    p_status.add_argument("--batch-id", required=True, help="Batch id")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
