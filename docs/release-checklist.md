# Release checklist

## Required inputs

- semantic version and immutable tag name
- exact commit on protected `main`
- unified local CI receipt and hosted post-merge `verify` run
- Python 3.10/3.11/3.12 and Node 22 compatibility evidence
- committed dependency lock artifacts and dependency refresh receipt
- API, dataset schema, manifest, run receipt, backup, and operational receipt compatibility matrix
- migration/consumer status, backup format, rollback commands, and evidence locations

## R1 baseline (`v0.1.0`)

Tag `v0.1.0` points to merge commit `679dafff539d8e64938cd098f61088e173ae114d`; required post-merge `verify` run `34442430045` succeeded on 2026-09-10. Release evidence is recorded in `docs/releases/v0.1.0.md` and `docs/releases/v0.1.0-receipt.json`.

## v0.2.0 protected-main evidence

PR #4 merged as protected-main commit `35c7ab71c52372d7cb94e5614f95b7271a4bc210`. Post-merge `Checks` run `34461900713` passed Python 3.10, 3.11, and 3.12 backend jobs, the Node 22 browser job, and aggregate `verify`. `Dependency Refresh` run `34461959295` rebuilt byte-identical committed constraints and passed all three Python runtimes plus service and browser acceptance. `Release` run `34461900956` published immutable `v0.1.0` with its release receipt.

Final evidence PR #5 merged as protected-main commit `54f002580a5c72cdd3da57ef12ffa14ea0f6c8c4`; post-merge `Checks` run `34462588514` succeeded. Annotated tag `v0.2.0` points immutably to that commit, and `Release` run `34462830609` published the GitHub release with `release-receipt.json`. Clean-tag installation, unified CI, API/worker start, smoke, browser acceptance, and rollback to `v0.1.0` all passed. The retained operational receipt is `operations/post_release_rehearsal/post-release-rehearsal.json` under the release sustainability evidence root.

## Release procedure

1. Merge through a protected PR; never release an unmerged feature commit.
2. Confirm the `verify` required check covers all Python matrix jobs and the Node 22 browser job.
3. Run `bash scripts/ci.sh` from a clean checkout using committed locks and retain receipts.
4. Confirm migrations and consumers are compatible or explicitly `not_migrated` with rollback flags.
5. Create an annotated tag exactly once and push it without force. Never move or recreate a published tag.
6. Publish release notes with API/schema/manifest/receipt, Python/Node, dependency, backup, and rollback details.
7. Install from the tagged checkout, start API/worker, run smoke and browser acceptance, then execute the rollback rehearsal.
8. Write a release receipt conforming to `docs/schemas/release-receipt.schema.json` and retain it permanently.

## Immutable production activation

1. Create the deployment only from a clean checkout of the exact protected-main commit whose commit-scoped
   `verify` check succeeded. Fetch `origin/main` immediately before staging.
2. Run `python -m data_center.deployment stage COMMIT --release-root RELEASE_ROOT --evidence-root EVIDENCE_ROOT`.
   Retain the manifest, artifact hash, Python constraint hash, Web UI asset hash, and stage receipt.
3. Hash canonical manifests and the ledger before activation. Activate through `data_center.deployment activate`;
   never repoint systemd or install dependencies manually.
4. Verify API, worker, monitor receipt, metrics, readiness, and Web UI report the same deployment ID, version,
   and source commit. Confirm API/worker `WorkingDirectory` and `ExecStart` resolve under `releases/current`.
5. Inject a candidate readiness or identity failure and retain the failed activation receipt proving automatic
   restoration of the previous release. Confirm canonical and ledger hashes did not change.
6. Roll back to the prior verified release, run readiness/smoke/browser checks, then forward-deploy the final
   protected-main release again. Retain both operation receipts.
7. Rebuild the receipt index explicitly and verify latest backup/recovery fields. Acknowledge or resolve every
   historical active dead-letter without modifying terminal runs.
8. Complete a continuous 60-minute monitor soak: no overlap, no immediate catch-up loop, evaluation under five
   seconds, bounded delivery, complete receipts, and no repeated webhook side effect for a stable capacity state.
9. Run the capacity check without relaxing thresholds. While status is `warning`, record Dukascopy D4 and
   unattended backfills over 31 days as prohibited.

Corrections use a new commit and a new semantic version tag. Historical receipts and tags are immutable.

## Rollback

- stop the new API/worker units;
- check out the previous immutable tag and install its matching lock artifact;
- restore configuration without changing canonical data;
- if recovery is required, verify the archive before restore and fail closed on conflicting bytes;
- restart API/worker and run readiness, smoke, query parity, and browser acceptance.
