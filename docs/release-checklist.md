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

## v0.3.0 protected-main evidence

Release preparation PR #62 merged as protected-main commit `ec79720f65bdf6251d48fb3de0c7b4b2de9b6991`; post-merge `Checks` run `34729985896` passed the Python 3.10, 3.11, and 3.12 backend jobs, the Node 22 browser job, and aggregate `verify`. Local unified CI (`bash scripts/ci.sh all`) produced a passing `operational-receipt.v1` receipt at that commit: 174 tests, Ruff, dependency/compatibility/secret checks, Web build, isolated browser acceptance (29 checks at 1440/390), and service acceptance. `Dependency Refresh` run `34729925217` regenerated all three committed constraints byte-identically and passed the Python matrix plus service and browser acceptance. Annotated tag `v0.3.0` points immutably to that commit, and `Release` run `34730111851` published the GitHub release with `release-receipt.json`.

Post-release rehearsal performed on 2026-09-13 with isolated roots and ephemeral ports; no production release root, systemd unit, or deployment activation was touched. Receipt: `operations/post_release_rehearsal/post-release-rehearsal.json` under the `release-closure-v0.3.0` evidence root. Clean `v0.3.0` checkout (`ec79720`) installed from `backend/constraints/py311.txt` and passed unified CI (174 tests, service acceptance, browser acceptance 29 checks at 1440/390). Rollback checkout `v0.2.0` (`54f00258`) installed from its committed constraints and passed unified CI (87 tests, service acceptance including smoke, browser acceptance at 1440/390). Raw per-leg receipts are retained alongside the rehearsal receipt.

Two environment caveats apply when reproducing this rehearsal:

- `v0.2.0`'s `scripts/ci.sh` predates the Chromium cache auto-discovery fix (`a11ecff`); `PLAYWRIGHT_BROWSER_EXECUTABLE` must be exported as described in `AGENTS.md`, otherwise browser acceptance looks for an absent managed `chromium_headless_shell` and fails.
- `scripts/operations_acceptance.py` asserts that `CapacityPolicy(warning_free_ratio=0.99)` reports `warning`, which requires the filesystem backing `tempfile.gettempdir()` to be more than 1% occupied. On a completely free tmpfs the gate reports `ok` and the drill raises `RuntimeError("warning policy did not block broad backfill")`. This reproduces identically on `v0.2.0` and `v0.3.0`, so it is a harness defect rather than a release or rollback defect, and hosted CI does not expose it because runner `/tmp` is not empty.

## v0.3.1 protected-main evidence

Release preparation PR #67 merged as protected-main commit `b5d1bc95681b4ce996938f0581c5df1ecc95b465`; post-merge `Checks` verify run `34732558041` succeeded and local unified CI passed (178 tests, `software_version=0.3.1`). Committed constraints were unchanged from `v0.3.0` (`e8c18665…`), so the v0.3.0 dependency refresh evidence still applies. Annotated tag `v0.3.1` points immutably to that commit and `Release` run `34732637082` published the GitHub release with `release-receipt.json`.

Immutable production activation on 2026-09-13 (receipts under the data-center evidence root):

- `deployment_stage` produced release `b5d1bc95681b-610c867f`; `deployment_activate` switched `releases/current` to it with canonical and ledger hashes unchanged. API readiness, metrics, the monitor receipt and the served Web UI all reported the same deployment id, version and source commit.
- Failure injection: a candidate release whose runtime was unusable passed the manifest pre-check and failed readiness verification. The failed `deployment_activate` receipt records `deployment_id=ffffffffffff-inject00`, `recovered_deployment_id=b5d1bc95681b-610c867f` and unchanged canonical and ledger hashes, proving automatic restoration of the previous release.
- Rollback rehearsal: `deployment_rollback` to `4016a992669d-b0be2ea0` passed readiness and smoke, then the release was forward-deployed again; both receipts retained.
- Receipt index rebuilt (2244 receipts); active dead letters zero.
- 60-minute monitor soak: 33 runs over 60.2 minutes with a stable identity, no overlapping runs, no catch-up runs (minimum interval 60.7s), maximum evaluation 0.006s, maximum delivery 0.048s, complete receipts, and no webhook side effects while capacity was stable. The monitor timer is configured with `OnUnitInactiveSec=60s` but runs at an effective 120s cadence because of systemd's default `AccuracySec=1min`; the soak criteria were therefore evaluated against that observed cadence.
- Capacity check without relaxed thresholds: `status=ok` (free ratio 0.117 against warning 0.05 and critical 0.02), so no D4 or over-31-day unattended backfill was recorded as prohibited.

## v0.3.2 protected-main evidence

Release preparation PR #70 merged as protected-main commit `a8f6e3d616e92f78a50ce9ec0800d0eb324836b9`; post-merge `Checks` verify run `34733951586` succeeded and local unified CI passed (183 tests, `software_version=0.3.2`). Annotated tag `v0.3.2` points immutably to that commit and `Release` run `34734039553` published the GitHub release with `release-receipt.json`.

`deployment_stage` produced release `a8f6e3d616e9-257b2f31` and `deployment_activate` switched to it with canonical and ledger hashes unchanged; API readiness, metrics and smoke all reported deployment `a8f6e3d616e9-257b2f31`, version `0.3.2`, source commit `a8f6e3d6`.

Outcome verification for the maintenance fix: before the fix the scheduled 1m maintenance reported `result=failed` every approximately 16 minutes with `failed_target_count=1`; on 2026-09-13 at 03:11:52 it reported `result=pass`, `failed_target_count=0`, `degraded_target_count=1` and `degraded_window_count=17` (BTCUSD degraded, seven other targets passing), and the systemd unit finished with `Result=success`. Provider gaps remain visible as `degraded` and no bars are synthesized. The deployment also inherited the fixes for incomplete provider coverage and for the previously environment-dependent browser capacity gate.

`failed` therefore still means something needs attention rather than a provider gap. Two consecutive runs under `v0.3.2` illustrate the distinction: the 03:29:39 run failed on a single `SSLError` window (retried three times and dead-lettered; the only `SSLError` run in the retained ledger), and the 03:46:33 run returned to `result=pass` with `failed_target_count=0`. Transient provider/network errors and structural quality failures keep failing the run by design.

## v0.4.0 protected-main evidence

Release preparation PR #77 merged as protected-main commit `5dfc1b1c6803feb4b577040c0d5e5f21bed9b96e`; the post-merge `Checks` `verify` job succeeded on that commit and local unified CI passed on the release branch (234 tests, browser acceptance 56 checks at 1440/390, `software_version=0.4.0`). Annotated tag `v0.4.0` points immutably to that commit and `Release` run `34751145976` published the GitHub release with `release-receipt.json` (hosted Python 3.10/3.11/3.12, Node 22).

Committed constraints were unchanged from v0.3.3 (`e8c18665…`), so the existing dependency refresh evidence still applies. The v0.4.0 tag also carries its own `Checks` run `34751146044`.

`deployment_stage` produced release `5dfc1b1c6803-377c1193`, and `deployment_activate` switched `releases/current` to it with canonical and ledger hashes unchanged; readiness, metrics and the served Web UI all reported `software_version=0.4.0`, `source_commit=5dfc1b1`, `deployment_id=5dfc1b1c6803-377c1193`. `deployment_rollback` to the previous release `f6f69366d01d-021478b1` (v0.3.3) passed readiness and was followed by a forward activation; all three pointer switches retained receipts and left both hashes unchanged. The rehearsal receipt is `operations/post_release_rehearsal/2026-09-13T102344…json` under the data-center evidence root.

Real-canonical walkthrough on the activated release (read-only): `operations/production_readiness_walkthrough/2026-09-13T103608…json` records readiness and metrics identity (`0.4.0` / `5dfc1b1` / `5dfc1b1c6803-377c1193`), the `/capabilities`, `/runs`+detail, `/quality/findings`, `/operations/{queue,capacity-history}` responses and four side-effect-free maintenance previews taken from production state at that time. The dukascopy EURUSD 1m gap-repair preview reported 11 windows with coverage `not_ready` and a non-zero gap count, the binance BTCUSDT 1m quality and dukascopy EURUSD 1m derive previews were submittable, and the yfinance SPY backfill preview refused to guess an asset class (`asset_class_required`). No preview queued a run or wrote a canonical part.

## v0.4.1 protected-main evidence

Patch release for the operations receipts panel. Release preparation PR #80 merged as protected-main commit `49ddbc55361d6cd04c25a27b42d1dcb17c2d918b`; the post-merge `Checks` `verify` job succeeded on that commit and local unified CI passed on the release branch (235 tests, browser acceptance 57 checks at 1440/390 including the new receipts-panel guard, `software_version=0.4.1`). Annotated tag `v0.4.1` points immutably to that commit and `Release` published the GitHub release with `release-receipt.json` (hosted Python 3.10/3.11/3.12, Node 22). Committed constraints were unchanged from v0.4.0, so the existing dependency refresh evidence still applies.

`deployment_stage` produced release `49ddbc55361d-bd11ad0e` and `deployment_activate` switched `releases/current` to it with canonical and ledger hashes unchanged; readiness and metrics reported `software_version=0.4.1`, `source_commit=49ddbc5`, `deployment_id=49ddbc55361d-bd11ad0e`, the manifest's Web UI dist hash matched the served build, and API, worker and monitor units were active. Receipts: `operations/deployment_stage/2026-09-13T111406…json`, `operations/deployment_activate/2026-09-13T111443…json`, `operations/post_release_walkthrough/2026-09-13T111712…json`.

The walkthrough receipt records the fix taking effect in production state: `GET /api/v1/operations/receipts` returned the thirteen actions the platform actually records (including `deployment_stage`, `deployment_activate`, `deployment_rollback`) and no `release`/`deployment` phantom names. No run was queued and no canonical part was written.

## v0.5.0 protected-main evidence

No separate evidence section was written for `v0.5.0` when it was published; the tag `v0.5.0` points immutably to `3e2362ab0b31a194639f3e4801322bb6d14f1ee6` (release preparation PR #90) and its activation is recorded in `docs/current-state.md` and in the deployment receipts under the data-center evidence root.

## v0.5.1 protected-main evidence

Patch release for the fixes merged after `v0.5.0` (Web UI hand-off between workspaces, explorer submit semantics, explained absence of detailed coverage, capacity measurement source, receipt-readable real-provider acceptance, narrowed warning gate, production configuration guard). Release preparation PR #102 merged as protected-main commit `edc6c1c442e4960fdd82d08fd6db4da279d0e275`; the post-merge `Checks` `verify` run `34804428044` succeeded on that commit (Python 3.10/3.11/3.12 and Node 22 browser jobs green).

Local unified CI (`bash scripts/ci.sh all`) passed on the PR head `d8aed91d7d4644f8f6bac9fab2e22731450484d5`, whose tree is identical to the merged commit: 276 passed / 4 skipped, docs-consistency, Ruff, `pip check`, compatibility, dependency lock, production-env check, secret scan (230 tracked files), operations acceptance, snapshot benchmark, Web build, browser acceptance (57 checks at 1440×1000 and 390×844), and service acceptance; receipt `acceptance-receipts/ci/all.json` with `result=pass`, `software_version=0.5.1`. Committed constraints were unchanged from `v0.5.0`, so the existing dependency refresh evidence still applies.

Annotated tag `v0.5.1` (tag object `859469aaeb41704f14124800c7bcca1923193320`) points immutably to that commit, and `Release` run `34804615895` published the GitHub release with `release-receipt.json` (`release-receipt.v1`, `result=pass`, `tag=v0.5.1`, `commit=edc6c1c4…`, hosted `verify` run `34804428044`).

The real-provider acceptance observation required by step 8 is recorded in `docs/releases/v0.5.1.md`: the most recent retained run (2026-09-14T01:00:21Z) fails `binance` (`ValueError`) and `dukascopy` (`HTTPError`) while `yfinance` and `fred` pass, and those receipts predate the classification added by this release. This tag claims no provider-level green.

**Production activation is deliberately excluded from this release.** The tag exists on protected `main`, but no `deployment_stage` or `deployment_activate` was run for it and production continues to serve `v0.5.0` (`3e2362ab0b31-6005b252`). Activation requires a separate approval and its own receipts; until then the rollback target of record stays `v0.5.0`, and `v0.5.1` is the forward target once activation is approved.

## v0.6.0 protected-main evidence

Release preparation PR (this change) merged as protected-main commit `<filled after merge>`; the post-merge
`Checks` `verify` run is recorded here once it completes. Local unified CI (`bash scripts/ci.sh all`) passed on the
release-preparation head with `software_version=0.6.0`: 442 passed / 5 skipped, Ruff, dependency lock, compatibility,
production-env, secret scan, operations acceptance, snapshot benchmark, Web build, browser acceptance (66 checks at
1440×1000 and 390×844) and service acceptance. Committed constraints are unchanged from `v0.5.1`, so the existing
dependency refresh evidence still applies.

The five delivery layers were merged as protected-main commits `5a64cc1` (#110), `a306fa7` (#111), `affeb9a` (#112),
`e380b5d` (#113) and `7f2b612` (#114), each with a green hosted `verify` (Python 3.10/3.11/3.12 + Node 22 browser,
10/10 checks). The ledger schema moves to 5 in this release; migration, drill and rollback statements are in
`docs/releases/v0.6.0.md`.

## v0.6.1 protected-main evidence

The three fixes were merged as protected-main commits `bfdac89` (takeover evidence, PR #116), `e25c268` (expected
empty windows, PR #117) and `7291a75` (grid-aligned coverage scans, PR #118), each with a green hosted `verify`
(Python 3.10/3.11/3.12 and the Node 22 browser job). Local unified CI on the release branch passed with
`software_version=0.6.1`: 468 passed / 5 skipped, Ruff, dependency lock and compatibility checks, production-env,
secret scan, operations acceptance, Web build, browser acceptance (66 checks at 1440×1000 and 390×844) and service
acceptance. The ledger schema stays at 5, so no migration or restore is involved. Statements, measurements and
rollback notes are in `docs/releases/v0.6.1.md`.

## Release procedure

1. Merge through a protected PR; never release an unmerged feature commit.
2. Confirm the `verify` required check covers all Python matrix jobs and the Node 22 browser job.
3. Run `bash scripts/ci.sh` from a clean checkout using committed locks and retain receipts.
4. Confirm migrations and consumers are compatible or explicitly `not_migrated` with rollback flags.
5. Create an annotated tag exactly once and push it without force. Never move or recreate a published tag.
6. Publish release notes with API/schema/manifest/receipt, Python/Node, dependency, backup, and rollback details.
7. Install from the tagged checkout, start API/worker, run smoke and browser acceptance, then execute the rollback rehearsal.
8. Review the latest scheduled real-provider acceptance receipt as a release observation (it is deliberately outside `bash scripts/ci.sh`, see the operations runbook). Record the outcome, including a known-failing provider, in the release notes rather than treating a green CI gate as provider evidence.
9. Write a release receipt conforming to `docs/schemas/release-receipt.schema.json` and retain it permanently.

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
