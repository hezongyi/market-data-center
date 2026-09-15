# Market Data Center Agent Guide

This file applies to the whole repository. Read release/operations documents when doing release or production operations; development previews follow the development guide.

## Current Delivery Baseline

- The maintainer approved the EURUSD-first direction on 2026-09-15. Read `docs/specs/2026-09-15-eurusd-first-product-baseline.md`, `docs/development-guide.md`, and `docs/plans/2026-09-15-eurusd-first-implementation.md` before selecting work.
- Prior unfinished development outside this iteration is paused in scheduling, except necessary production maintenance. This does not stop running services or erase history. The product spec maps retained contracts and deferred migration acceptance; old specs must not silently reintroduce legacy parity, multi-hour shadow comparison or crypto rollout as this iteration's gates.
- For WebUI changes, also read `webui/AGENTS.md`: actual shadcn-admin components/theme/interaction are required, not a handwritten CSS approximation. Use the pinned local reference in `docs/references/shadcn-admin.md`.
- For previews, follow `docs/specs/2026-09-15-isolated-preview-environment.md`. Deliver a usable URL, commit, data mode, steps and limitations early. Draft PRs and previews do not require prior review, a release tag or production deployment. Never default a trial-write preview to production.
- Specs define requirements; plans define sequencing; research records evidence; guides describe operating procedures. An approved design is not implemented/accepted/deployed evidence. Preserve historical receipts and record partial supersession explicitly.

## Worktree Safety

- Treat an existing dirty worktree as user-owned. Do not reset, clean, stash, or overwrite unrelated changes.
- For release work or broad changes, create a separate branch/worktree from `origin/main` and verify its status before editing.
- Never push directly to `main`. Branch protection requires an up-to-date pull request and the `verify` check; administrators cannot bypass it.

## Standard Verification

- Python support: 3.10, 3.11, and 3.12. Node baseline: 22.
- Install Python dependencies with the matching committed constraint file, for example:

  ```bash
  python3.11 -m venv .venv
  .venv/bin/python -m pip install -c backend/constraints/py311.txt -e './backend[dev]'
  ```

- During development run relevant checks. Full `bash scripts/ci.sh all` applies to cross-module/high-risk runtime changes and release candidates, not every small local edit. Documentation-only governance changes use documentation/consistency checks plus independent review before merge. Hosted current-commit `verify` remains required before merge; its full matrix has not yet been narrowed. See `docs/development-guide.md`.
- Use isolated canonical, ledger, auth, evidence, and backup roots for tests and previews. Default CI/browser acceptance must not read production data or contact real providers. Explicit bounded live-sandbox acceptance is separate, follows the preview spec, and never writes production roots.

## Playwright and Chromium

- Playwright is pinned in `webui/package-lock.json`; use `npm --prefix webui ci` rather than a global package.
- Hosted CI installs its own browser with `npx --prefix webui playwright install --with-deps chromium`.
- This Ubuntu host has a reusable Chromium cache. Discover the executable instead of assuming a versioned directory:

  ```bash
  export PLAYWRIGHT_BROWSER_EXECUTABLE="$(find "$HOME/.cache/ms-playwright" -type f -path '*/chrome-linux/chrome' | sort -V | tail -1)"
  export LD_LIBRARY_PATH="$HOME/.local/share/playwright-deps-jammy/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  test -x "$PLAYWRIGHT_BROWSER_EXECUTABLE"
  npm --prefix webui test
  ```

- If the cached browser fails to launch, check missing libraries with `ldd "$PLAYWRIGHT_BROWSER_EXECUTABLE" | grep 'not found'` before downloading another browser.

## GitHub Pull Requests

- Git pushes use SSH. Confirm with `ssh -T git@github.com` and `git remote get-url origin`.
- GitHub API and PR operations use the persisted `gh` login. Ensure the user-local binary is available and validate it without printing tokens:

  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  gh auth status
  gh api user --jq .login
  ```

- Create PRs with `gh pr create`, inspect checks with `gh pr view` or `gh pr checks`, and merge only after the current head is `CLEAN` and the latest `verify` check succeeds.
- Do not use an older successful run from the same branch as evidence for a newer commit. Confirm workflow `head_sha` matches the commit being merged or released.
- OAuth tokens, credential-bearing proxy URLs and API keys belong in user configuration or ignored files. Never print credentials or commit them. Do not hard-code machine-local paths as runtime dependencies; documentation may record explicitly scoped, replaceable local reference/evidence paths without secrets.

## Issue Collaboration

- Shared issues are the unit of work for multiple agents. Follow `docs/agent-collaboration.md` for provenance, labels and its 24h lease. Directly assigned local tasks/research do not require creating an issue first. External messages still require authorization.
- When working a shared issue, claim first, one owner per issue, and release the claim when you stop. Use `python scripts/agent_claim.py list --claimable`, `claim`, `progress`, `release`, and `annotate` instead of hand-rolling the API calls.
- Report issues with evidence: a `file:line` reference or a reproducible command with its output, plus the baseline (version, commit, deployment id). Never present "not found" as "does not exist".
- An issue reported by an agent is declared as such (label `agent-reported` plus the invisible `agent-report` metadata block). The GitHub author is the credential owner, not the agent.
- PR gates follow R0/R1/R2 in `docs/development-guide.md`: low-risk documentation/copy/style may use self-review; ordinary behavior changes require one independent review; identity, aggregation, concurrency/recovery, auth, destructive migrations, release mechanisms and governance changes require deeper review and a risk summary. “Changes production behavior” alone does not make every feature high risk.
- User-visible new flows get a usable preview and stage feedback before merge. Technical review does not substitute for product acceptance. Complete functionality is accepted on the integration preview; a giant aggregate PR is not required.
- Check existing authorization before asking again. New uncovered high-risk scope and production activation need explicit authorization; approval of design or a small PR is not implicit approval of production operations. Record actual approval scope, person and time in the delivery record.

## Release Operations

- Follow `docs/release-checklist.md`, `docs/operations-runbook.md`, and `docs/specs/2026-09-10-release-and-operational-sustainability.md`.
- Release tags must be annotated, immutable, reachable from protected `main`, and backed by a successful commit-scoped `verify` run.
- Retain structured receipts for CI, dependency refresh, browser acceptance, capacity checks, backup/restore, releases, and recovery drills.
- Never delete or overwrite canonical parts, manifests, terminal receipts, or the ledger as an automatic capacity response.
