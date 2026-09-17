"""Conservative CI selection and fail-closed aggregation (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path, PurePosixPath


def select_mode(event: str, paths: list[str], *, regular_files: bool = True) -> str:
    # Unknown paths, empty comparisons, main and manual checks keep the full gate.
    docs = all(
        PurePosixPath(name).suffix == ".md"
        and ("/" not in name or name.startswith(("docs/", "webui/", "backend/", "deploy/", ".github/")))
        for name in paths
    )
    return "docs" if event == "pull_request" and paths and docs and regular_files else "full"


def make_plan(repo: Path, event: str, base: str | None, head: str = "HEAD") -> dict:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=repo)

    head_sha = git("rev-parse", "--verify", f"{head}^{{commit}}").decode().strip()
    if event == "pull_request" and not base:
        raise ValueError("pull_request selection requires a base commit")
    base_sha = git("rev-parse", "--verify", f"{base or head_sha}^{{commit}}").decode().strip()
    names = git("diff", "--no-renames", "--name-only", "-z", f"{base_sha}...{head_sha}")
    paths = [os.fsdecode(name) for name in names.split(b"\0") if name]
    regular = True
    if paths:
        for ref in (base_sha, head_sha):
            entries = git("ls-tree", "-r", "-z", ref, "--", *paths)
            regular &= all(entry.startswith(b"100644 blob ") for entry in entries.split(b"\0") if entry)
    return {"mode": select_mode(event, paths, regular_files=regular),
            "base": base_sha, "head": head_sha, "paths": paths}


def verify_results(mode: str, results: dict[str, str]) -> bool:
    if mode not in {"docs", "full"}:
        return False
    expected = {"plan": "success", "docs": "success",
                "backend": "success" if mode == "full" else "skipped",
                "web": "success" if mode == "full" else "skipped"}
    return results == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--event", required=True)
    plan.add_argument("--base")
    plan.add_argument("--head", default="HEAD")
    plan.add_argument("--github-output", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--mode", required=True)
    for job in ("plan", "docs", "backend", "web"):
        verify.add_argument(f"--{job}", required=True)
    args = parser.parse_args()
    if args.command == "verify":
        results = {job: getattr(args, job) for job in ("plan", "docs", "backend", "web")}
        valid = verify_results(args.mode, results)
        print(json.dumps({"mode": args.mode, "results": results, "pass": valid}))
        return 0 if valid else 1
    payload = make_plan(Path.cwd(), args.event, args.base, args.head)
    print(json.dumps(payload, indent=2))
    if args.github_output:
        with args.github_output.open("a") as stream:
            for key in ("mode", "base", "head"):
                stream.write(f"{key}={payload[key]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
