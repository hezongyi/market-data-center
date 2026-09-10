# Market Data Center Agent Guide

This file applies to the whole repository. Read the referenced release and operations documents before changing production-facing behavior.

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

- Run the unified gate with `bash scripts/ci.sh all`. It covers backend tests, Ruff, dependency and compatibility checks, secret scanning, operations acceptance, Web build, isolated browser acceptance, and service restart acceptance.
- Use isolated canonical, ledger, evidence, and backup roots for tests. Browser and acceptance runs must not read production data or contact real providers.

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
- OAuth tokens, proxy URLs, API keys, and machine-local paths belong in user configuration or ignored files. Never print credentials or commit them.

## Release Operations

- Follow `docs/release-checklist.md`, `docs/operations-runbook.md`, and `docs/specs/2026-09-10-release-and-operational-sustainability.md`.
- Release tags must be annotated, immutable, reachable from protected `main`, and backed by a successful commit-scoped `verify` run.
- Retain structured receipts for CI, dependency refresh, browser acceptance, capacity checks, backup/restore, releases, and recovery drills.
- Never delete or overwrite canonical parts, manifests, terminal receipts, or the ledger as an automatic capacity response.
