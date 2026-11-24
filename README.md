# NastechBridge

**Role:** the durable report waitlist, consolidated batch status, and the notification
back to 100ways.

This repository is the durable specification and memory for the NastechBridge role in the
WatchCodes factory. Read this file before doing anything here.

---

## Responsibility

NastechBridge is the factory's memory and its handshake with `NastechResearch/100Ways`.
It stores reports as **committed files, never workflow artifacts**, so a run that ends
does not end the record.

1. Receive `MigrationReport` (from CodeFoundry) and `UpstreamReport` (from SourceSentinel).
2. Store each under `reports/batches/<batch_id>/` on an append-only `reports` branch.
3. Maintain `manifest.json` for the batch — the only file Bridge itself writes.
4. Consolidate: expected inventory vs verified inventory, per repository.
5. Emit a `BatchReadinessReport` and notify 100ways.
6. Keep the registries: `registry/repository-mappings.json`,
   `registry/upstream-registry.json`.

## Layout

```text
reports/
└── batches/
    └── BATCH_ID/
        ├── manifest.json      # only NastechBridge writes this
        ├── RepoAtlas.json
        ├── CodeFoundry.json
        ├── CodeLineage.json
        └── upstream.json
registry/
├── repository-mappings.json
└── upstream-registry.json
```

## Rules that keep it correct

- **One writer per file.** CodeFoundry writes its own report file; only Bridge writes
  `manifest.json`. Two writers on one file is a race, and a race here loses evidence.
- **`batch_id` + `source` is the idempotency key.** Re-running a workflow updates a
  record; it never creates a duplicate repository.
- **Durable, not ephemeral.** Never a workflow artifact — artifacts expire, the waitlist
  must not.
- **Recoverable.** If a notification is lost, 100ways reads the latest batch report
  directly and continues. Notification is an optimisation, not the record.

## The readiness gate

Fail-closed. `ready` is true only when all of these hold:

- expected repository inventory equals the verified inventory
- every destination exists with the correct name and owner
- every mapping is recorded, ordered and consistent
- no `FAILED`, `REQUIRES_REVIEW` or missing report remains
- every repository is `GATED` (zero-brand scan passed)
- final URL replacements are ready

Otherwise `ready` is false, the exact missing and failed items are reported, and
publication stays pending. A later notification re-runs the gate.

## What it never does

- Never pushes application code into `nastech-agent`.
- Never merges, tags, releases or publishes.
- Never declares `READY` for a repository that has not passed both the ownership check and
  the brand gate.

## Contracts

| Direction | Contract | Minimum fields |
|---|---|---|
| consumes | `MigrationReport` | `batch_id`, `repo`, `status`, `checks{…}`, `pr_url`, `error` |
| consumes | `UpstreamReport` | `repo`, `upstream`, `upstream_sha`, `behind_by`, `detected_at` |
| produces | `BatchReadinessReport` | `batch_id`, `expected[]`, `verified[]`, `failed[]`, `pending[]`, `ready`, `generated_at` |

## The status ladder

`DISCOVERED → QUEUED → CREATING → CREATED → VERIFYING → VERIFIED → GATED → READY`

plus `FAILED` and `REQUIRES_REVIEW`. `VERIFIED` means the repository exists with the right
name and owner. `GATED` means the zero-brand gate passed. `READY` requires both. A created
repository is never automatically ready.

## CLI

`nastechbridge` is installed as a console script. Every command prints one JSON object.

- `validate --schema <name> --file <json>` — fail-closed contract check.
- `ingest --root <dir> --kind migration-report|upstream-report --file <json> [--batch-id <id>]`
  — append a report to the durable waitlist.
- `consolidate --root <dir> --batch-id <id> --expected <owner/repo> [...]` — write
  `batch-readiness-report.json`; exit code 0 only when the batch is ready.
- `status --root <dir> --batch-id <id>` — show the manifest and stored readiness.

## Related repositories

- `WatchCodes/RepoAtlas` — where `DISCOVERED` and `QUEUED` come from
- `WatchCodes/CodeFoundry` — where `CREATED` and `VERIFIED` come from
- `WatchCodes/SourceSentinel` — where `UpstreamReport` comes from
- `WatchCodes/CodeLineage` — the mapping registry's authority
- `NastechResearch/100Ways` — publishes only after this gate says ready; a human merges
