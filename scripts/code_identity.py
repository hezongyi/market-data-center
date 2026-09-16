"""Identify the exact checkout, including staged and untracked source changes."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def checkout_identity(repo: Path) -> dict:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=repo)

    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    fingerprint = hashlib.sha256(status)
    fingerprint.update(git("diff", "--binary", "HEAD"))
    # Query untracked paths separately: porcelain rename records have two names.
    for name in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if name:
            path = repo / os.fsdecode(name)
            fingerprint.update(name)
            if path.is_symlink():
                fingerprint.update(os.fsencode(os.readlink(path)))
            elif path.is_file():
                fingerprint.update(path.read_bytes())
    return {
        "checkout": str(repo.resolve()),
        "branch": git("branch", "--show-current").decode().strip() or "detached",
        "commit": git("rev-parse", "HEAD").decode().strip(),
        "dirty": bool(status),
        "worktree_fingerprint": fingerprint.hexdigest(),
    }
