"""Claim, track and annotate agent work on this repository's issues.

The protocol this implements is documented in ``docs/agent-collaboration.md``.
The script only talks to GitHub through the already-authenticated ``gh`` CLI:
it never reads, stores or prints a credential.

Why a lease instead of a lock: GitHub offers no compare-and-swap on labels, so
a claim is a *declaration* (a comment) plus a *re-check* (read the issue again
and retreat if someone claimed earlier).  The lease makes an abandoned claim
recoverable instead of permanently blocking the issue.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

CLAIM_MARKER = "<!-- agent-claim -->"
PROGRESS_MARKER = "<!-- agent-progress -->"
RELEASE_MARKER = "<!-- agent-release -->"
PROVENANCE_MARKER = "<!-- agent-report"
PROVENANCE_TITLE = "**报告来源**"

STATUS_LABELS = (
    "status:triage",
    "status:accepted",
    "status:claimed",
    "status:in-progress",
    "status:review",
    "status:done",
    "status:blocked",
)
ACTIVE_STATUS_LABELS = ("status:claimed", "status:in-progress", "status:review")
CLAIMABLE_STATUS_LABELS = ("status:accepted", "agent-claimable")
LEASE = timedelta(hours=24)
DEFAULT_LEASE_HOURS = 24


class GhError(RuntimeError):
    """Raised when the ``gh`` CLI fails."""


# --------------------------------------------------------------------------- #
# pure helpers (unit tested in backend/tests/test_agent_claim.py)
# --------------------------------------------------------------------------- #
def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating the ``Z`` suffix on 3.10."""
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def render_claim(
    agent: str,
    *,
    now: datetime,
    plan: str = "",
    artifact: str = "",
    eta: str = "",
) -> str:
    """Render the claim comment, marker first so parsing needs no heuristics."""
    return "\n".join(
        [
            "/claim",
            CLAIM_MARKER,
            f"agent: {agent}",
            f"claimed_at: {now.isoformat()}",
            f"plan: {plan or 'n/a'}",
            f"expected_artifact: {artifact or 'n/a'}",
            f"eta: {eta or (now + LEASE).isoformat()}",
        ]
    )


@dataclass(frozen=True)
class Claim:
    agent: str
    claimed_at: datetime
    comment_id: int
    plan: str = ""
    artifact: str = ""
    eta: str = ""


def _field(text: str, name: str) -> str:
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == name:
            return value.strip()
    return ""


def parse_claim(body: str, comment_id: int) -> Claim | None:
    """Parse a claim comment, or return ``None`` when it is not one."""
    if CLAIM_MARKER not in body:
        return None
    agent = _field(body, "agent")
    claimed_at = _field(body, "claimed_at")
    if not agent or not claimed_at:
        return None
    try:
        parsed = parse_timestamp(claimed_at)
    except ValueError:
        return None
    return Claim(
        agent=agent,
        claimed_at=parsed,
        comment_id=comment_id,
        plan=_field(body, "plan"),
        artifact=_field(body, "expected_artifact"),
        eta=_field(body, "eta"),
    )


def claims_from(comments: Sequence[dict[str, Any]]) -> list[Claim]:
    parsed = []
    for comment in comments:
        claim = parse_claim(str(comment.get("body") or ""), int(comment.get("id") or 0))
        if claim is not None:
            parsed.append(claim)
    return sorted(parsed, key=lambda item: (item.claimed_at, item.comment_id))


def live_claims(comments: Sequence[dict[str, Any]]) -> list[Claim]:
    """Claims still standing after release comments are applied in order.

    A release only retracts the claims written *before* it, so an agent may
    release and later re-claim the same issue.
    """
    ordered = sorted(comments, key=lambda item: int(item.get("id") or 0))
    standing: dict[str, Claim] = {}
    for item in ordered:
        body = str(item.get("body") or "")
        agent = _field(body, "agent")
        if RELEASE_MARKER in body and agent:
            standing.pop(agent, None)
            continue
        claim = parse_claim(body, int(item.get("id") or 0))
        if claim is not None:
            standing[claim.agent] = claim
    return sorted(standing.values(), key=lambda item: (item.claimed_at, item.comment_id))


def claim_status(
    comments: Sequence[dict[str, Any]],
    *,
    agent: str,
    now: datetime,
    lease: timedelta = LEASE,
) -> tuple[str, Claim | None]:
    """Return ``(state, claim)`` where state is free | mine | taken | expired."""
    standing = live_claims(comments)
    active = [claim for claim in standing if now - claim.claimed_at < lease]
    if not active:
        return ("expired" if standing else "free"), None
    owner = active[0]
    return ("mine" if owner.agent == agent else "taken"), owner


def label_plan(labels: Iterable[str], target: str | None) -> tuple[list[str], list[str]]:
    """Return the ``(add, remove)`` label lists for a status transition."""
    existing = list(labels)
    if target is None:
        return [], []
    remove = [name for name in existing if name in STATUS_LABELS and name != target]
    add = [] if target in existing else [target]
    return add, remove


def provenance_block(
    *,
    model: str,
    harness: str,
    reported_at: datetime,
    credential_owner: str,
    source: str,
    repo_version: str = "",
    commit: str = "",
    deployment_id: str = "",
) -> tuple[str, str]:
    """Return the ``(visible_footer, metadata_comment)`` pair for an issue body."""
    attribution = (
        f"<sub>{PROVENANCE_TITLE}：本条由 **AI agent** 提交"
        f"（{harness} / model `{model}`），经 `{credential_owner}` 的 `gh` 凭据代发"
        " —— GitHub 上的 author 因此显示为人类账号，**不代表人类逐条撰写**。</sub>"
    )
    invitation = (
        "<sub>欢迎其它 agent 在此讨论、认领或反驳；"
        "认领与状态流转见 `docs/agent-collaboration.md`。</sub>"
    )
    footer = f"\n---\n\n{attribution}\n\n{invitation}"
    fields = [
        "reporter_kind: ai-agent",
        f"model: {model}",
        f"harness: {harness}",
        f"reported_at: {reported_at.isoformat()}",
        "submitted_via: gh-cli",
        f"credential_owner: {credential_owner}",
    ]
    if repo_version:
        fields.append(f"baseline_repo_version: {repo_version}")
    if commit:
        fields.append(f"baseline_commit: {commit}")
    if deployment_id:
        fields.append(f"baseline_deployment_id: {deployment_id}")
    fields.append(f"source: {source}")
    metadata = "\n".join([PROVENANCE_MARKER, *fields, "-->"])
    return footer, metadata


def annotate_body(body: str, footer: str, metadata: str) -> str:
    """Append provenance to an issue body; idempotent."""
    if PROVENANCE_MARKER in body:
        return body
    return f"{body.rstrip()}\n{footer}\n\n{metadata}\n"


# --------------------------------------------------------------------------- #
# gh plumbing
# --------------------------------------------------------------------------- #
def gh(args: Sequence[str], *, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, input=stdin, check=False
    )
    if result.returncode != 0:
        raise GhError(result.stderr.strip() or f"gh exited {result.returncode}")
    return result.stdout


def resolve_repo(explicit: str | None) -> str:
    if explicit:
        return explicit
    return gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()


def api(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    args = ["api", "-X", method, path]
    if payload is None:
        return json.loads(gh(args) or "null")
    return json.loads(gh([*args, "--input", "-"], stdin=json.dumps(payload)) or "null")


def api_list(path: str) -> list[dict[str, Any]]:
    output = gh(["api", "--paginate", "-q", ".[]", path])
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def comments_of(repo: str, number: int) -> list[dict[str, Any]]:
    return api_list(f"repos/{repo}/issues/{number}/comments?per_page=100")


def comment(repo: str, number: int, body: str) -> None:
    api(f"repos/{repo}/issues/{number}/comments", method="POST", payload={"body": body})


def apply_labels(repo: str, number: int, add: Sequence[str], remove: Sequence[str]) -> None:
    for name in add:
        api(f"repos/{repo}/issues/{number}/labels", method="POST", payload={"labels": [name]})
    for name in remove:
        api(f"repos/{repo}/issues/{number}/labels/{name}", method="DELETE")


def issue_labels(issue: dict[str, Any]) -> list[str]:
    return [str(label.get("name")) for label in issue.get("labels") or []]


def is_pull_request(issue: dict[str, Any]) -> bool:
    return "pull_request" in issue


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_list(args: argparse.Namespace, repo: str, now: datetime) -> int:
    issues = [
        issue
        for issue in api_list(f"repos/{repo}/issues?state=open&per_page=100")
        if not is_pull_request(issue)
    ]
    rows = []
    for issue in issues:
        labels = issue_labels(issue)
        number = int(issue["number"])
        state, owner = claim_status(
            comments_of(repo, number), agent=args.agent, now=now
        )
        claimable = not any(name in labels for name in ACTIVE_STATUS_LABELS)
        if args.claimable and not (
            claimable and any(name in labels for name in CLAIMABLE_STATUS_LABELS)
        ):
            continue
        rows.append(
            {
                "number": number,
                "title": str(issue.get("title") or ""),
                "labels": labels,
                "claim_state": state,
                "owner": owner.agent if owner else None,
                "url": str(issue.get("html_url") or ""),
            }
        )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        owner = f" owner={row['owner']}" if row["owner"] else ""
        print(f"#{row['number']:<4} [{','.join(row['labels'])}] {row['claim_state']}{owner} {row['title']}")
    return 0


def cmd_claim(args: argparse.Namespace, repo: str, now: datetime) -> int:
    path = f"repos/{repo}/issues/{args.number}"
    issue = api(path)
    labels = issue_labels(issue)
    comments = comments_of(repo, args.number)
    state, owner = claim_status(comments, agent=args.agent, now=now)

    if state == "taken" and owner is not None:
        print(
            f"#{args.number} is claimed by {owner.agent} at {owner.claimed_at.isoformat()}"
            f" (comment {owner.comment_id}); retreating.",
            file=sys.stderr,
        )
        return 4
    if state == "mine" and owner is not None:
        print(f"#{args.number} is already claimed by {args.agent}; nothing to do.")
        return 0
    if not any(name in labels for name in CLAIMABLE_STATUS_LABELS) and not args.force:
        print(
            f"#{args.number} is not accepted for claiming"
            f" (labels: {', '.join(labels) or 'none'}); use --force to override.",
            file=sys.stderr,
        )
        return 3

    comment(
        repo,
        args.number,
        render_claim(
            args.agent, now=now, plan=args.plan, artifact=args.artifact, eta=args.eta or ""
        ),
    )
    add, remove = label_plan(labels, "status:claimed")
    apply_labels(repo, args.number, add, remove)

    # Re-check: whoever claimed first keeps the issue.
    verified_labels = issue_labels(api(path))
    state, owner = claim_status(
        comments_of(repo, args.number), agent=args.agent, now=now
    )
    if state == "taken" and owner is not None:
        comment(
            repo,
            args.number,
            "\n".join(
                [
                    RELEASE_MARKER,
                    f"agent: {args.agent}",
                    f"reason: earlier claim by {owner.agent} detected after writing",
                ]
            ),
        )
        back_add, back_remove = label_plan(verified_labels, "status:accepted")
        apply_labels(repo, args.number, back_add, back_remove)
        print(
            f"lost the race to {owner.agent}; retracted the claim.", file=sys.stderr
        )
        return 4

    if args.assign:
        try:
            api(path, method="PATCH", payload={"assignees": [args.assign]})
        except GhError as error:
            print(f"label applied, assignment skipped: {error}", file=sys.stderr)
    print(f"claimed #{args.number} as {args.agent} (lease {DEFAULT_LEASE_HOURS}h)")
    return 0


def cmd_progress(args: argparse.Namespace, repo: str, now: datetime) -> int:
    comment(
        repo,
        args.number,
        "\n".join(
            [
                PROGRESS_MARKER,
                f"agent: {args.agent}",
                f"at: {now.isoformat()}",
                f"note: {args.note}",
            ]
        ),
    )
    print(f"progress recorded on #{args.number}")
    return 0


def cmd_status(args: argparse.Namespace, repo: str, now: datetime) -> int:
    if args.label not in STATUS_LABELS:
        print(f"unknown status label {args.label!r}; expected one of {STATUS_LABELS}", file=sys.stderr)
        return 2
    path = f"repos/{repo}/issues/{args.number}"
    labels = issue_labels(api(path))
    add, remove = label_plan(labels, args.label)
    apply_labels(repo, args.number, add, remove)
    print(f"#{args.number} -> {args.label}")
    return 0


def cmd_release(args: argparse.Namespace, repo: str, now: datetime) -> int:
    path = f"repos/{repo}/issues/{args.number}"
    labels = issue_labels(api(path))
    comment(
        repo,
        args.number,
        "\n".join(
            [
                RELEASE_MARKER,
                f"agent: {args.agent}",
                f"at: {now.isoformat()}",
                f"reason: {args.reason}",
            ]
        ),
    )
    target = "status:blocked" if args.to_blocked else "status:accepted"
    add, remove = label_plan(labels, target)
    apply_labels(repo, args.number, add, remove)
    print(f"released #{args.number} back to {target}")
    return 0


def cmd_annotate(args: argparse.Namespace, repo: str, now: datetime) -> int:
    path = f"repos/{repo}/issues/{args.number}"
    issue = api(path)
    body = str(issue.get("body") or "")
    footer, metadata = provenance_block(
        model=args.model,
        harness=args.harness,
        reported_at=now,
        credential_owner=args.credential_owner,
        source=args.source,
        repo_version=args.repo_version,
        commit=args.commit,
        deployment_id=args.deployment_id,
    )
    updated = annotate_body(body, footer, metadata)
    if updated == body:
        print(f"#{args.number} already carries provenance; nothing to do.")
        return 0
    api(path, method="PATCH", payload={"body": updated})
    try:
        api(
            f"repos/{repo}/issues/{args.number}/labels",
            method="POST",
            payload={"labels": ["agent-reported"]},
        )
    except GhError as error:
        print(f"provenance written, label skipped: {error}", file=sys.stderr)
    print(f"annotated #{args.number} with agent provenance")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="OWNER/NAME (default: current gh repo)")
    parser.add_argument("--agent", default="unknown-agent", help="claiming agent id")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    # ``--json`` also works after the subcommand, so the option reads naturally
    # in either position.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", parents=[common], help="list open issues with claim state")
    listing.add_argument("--claimable", action="store_true", help="only claimable issues")
    listing.set_defaults(func=cmd_list)

    claim = sub.add_parser("claim", parents=[common], help="claim an issue (declaration + re-check)")
    claim.add_argument("number", type=int)
    claim.add_argument("--plan", default="")
    claim.add_argument("--artifact", default="")
    claim.add_argument("--eta", default="")
    claim.add_argument("--assign", default="", help="github login to assign")
    claim.add_argument("--force", action="store_true", help="claim without status:accepted")
    claim.set_defaults(func=cmd_claim)

    progress = sub.add_parser("progress", parents=[common], help="record a lease-renewing progress note")
    progress.add_argument("number", type=int)
    progress.add_argument("--note", required=True)
    progress.set_defaults(func=cmd_progress)

    status = sub.add_parser("status", parents=[common], help="move an issue to another status label")
    status.add_argument("number", type=int)
    status.add_argument("label", choices=STATUS_LABELS)
    status.set_defaults(func=cmd_status)

    release = sub.add_parser("release", parents=[common], help="release a claim back to the pool")
    release.add_argument("number", type=int)
    release.add_argument("--reason", required=True)
    release.add_argument("--to-blocked", action="store_true")
    release.set_defaults(func=cmd_release)

    annotate = sub.add_parser("annotate", parents=[common], help="append agent provenance to an issue")
    annotate.add_argument("number", type=int)
    annotate.add_argument("--model", required=True)
    annotate.add_argument("--harness", default="deepseek-harness")
    annotate.add_argument("--source", required=True)
    annotate.add_argument("--credential-owner", default="")
    annotate.add_argument("--repo-version", default="")
    annotate.add_argument("--commit", default="")
    annotate.add_argument("--deployment-id", default="")
    annotate.set_defaults(func=cmd_annotate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        repo = resolve_repo(args.repo)
        if args.command == "annotate" and not args.credential_owner:
            args.credential_owner = gh(["api", "user", "--jq", ".login"]).strip()
        return int(args.func(args, repo, now))
    except GhError as error:
        print(f"gh error: {error}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
