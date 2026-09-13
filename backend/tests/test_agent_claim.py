"""Unit tests for the agent collaboration helpers in scripts/agent_claim.py."""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[2] / "scripts" / "agent_claim.py"
_SPEC = importlib.util.spec_from_file_location("agent_claim", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
# Dataclasses resolve their module through sys.modules, so register it first.
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

annotate_body = _MODULE.annotate_body
claim_status = _MODULE.claim_status
label_plan = _MODULE.label_plan
parse_claim = _MODULE.parse_claim
parse_timestamp = _MODULE.parse_timestamp
provenance_block = _MODULE.provenance_block
render_claim = _MODULE.render_claim

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _comment(body: str, comment_id: int) -> dict:
    return {"id": comment_id, "body": body}


def _claim(agent: str, at: datetime, comment_id: int) -> dict:
    return _comment(render_claim(agent, now=at, plan="p"), comment_id)


def test_parse_timestamp_accepts_z_suffix_on_python_310() -> None:
    assert parse_timestamp("2026-09-13T11:38:35Z") == datetime(
        2026, 9, 13, 11, 38, 35, tzinfo=timezone.utc
    )
    assert parse_timestamp("2026-09-13T13:38:35+02:00") == datetime(
        2026, 9, 13, 11, 38, 35, tzinfo=timezone.utc
    )


def test_claim_round_trip_keeps_agent_and_plan() -> None:
    body = render_claim("alpha", now=NOW, plan="fix handoff", artifact="PR", eta="")
    claim = parse_claim(body, 7)
    assert claim is not None
    assert claim.agent == "alpha"
    assert claim.claimed_at == NOW
    assert claim.plan == "fix handoff"
    assert claim.artifact == "PR"


def test_parse_claim_ignores_other_comments() -> None:
    assert parse_claim("just a discussion comment", 1) is None
    assert parse_claim("<!-- agent-claim -->\nagent: alpha", 2) is None


def test_claim_status_free_mine_taken_and_expired() -> None:
    assert claim_status([], agent="alpha", now=NOW) == ("free", None)

    mine, owner = claim_status([_claim("alpha", NOW - timedelta(hours=1), 1)], agent="alpha", now=NOW)
    assert mine == "mine" and owner is not None and owner.agent == "alpha"

    taken, owner = claim_status([_claim("beta", NOW - timedelta(hours=1), 1)], agent="alpha", now=NOW)
    assert taken == "taken" and owner is not None and owner.agent == "beta"

    expired, _ = claim_status([_claim("beta", NOW - timedelta(hours=30), 1)], agent="alpha", now=NOW)
    assert expired == "expired"


def test_earliest_active_claim_wins_and_expired_ones_are_skipped() -> None:
    comments = [
        _claim("beta", NOW - timedelta(hours=2), 2),
        _claim("alpha", NOW - timedelta(hours=30), 1),
    ]
    state, owner = claim_status(comments, agent="gamma", now=NOW)
    assert state == "taken"
    assert owner is not None and owner.agent == "beta"


def test_release_comment_frees_the_claim() -> None:
    comments = [
        _claim("beta", NOW - timedelta(hours=1), 1),
        _comment("<!-- agent-release -->\nagent: beta\nreason: cannot reproduce", 2),
    ]
    state, owner = claim_status(comments, agent="alpha", now=NOW)
    assert state == "free"
    assert owner is None


def test_agent_may_reclaim_after_releasing() -> None:
    comments = [
        _claim("beta", NOW - timedelta(hours=3), 1),
        _comment("<!-- agent-release -->\nagent: beta\nreason: paused", 2),
        _claim("beta", NOW - timedelta(hours=1), 3),
    ]
    state, owner = claim_status(comments, agent="alpha", now=NOW)
    assert state == "taken"
    assert owner is not None and owner.agent == "beta"


def test_label_plan_is_exclusive_over_status_labels() -> None:
    labels = ["bug", "agent-reported", "status:accepted"]
    add, remove = label_plan(labels, "status:claimed")
    assert add == ["status:claimed"]
    assert remove == ["status:accepted"]

    add, remove = label_plan(labels, "status:accepted")
    assert add == []
    assert remove == []


def test_provenance_block_is_machine_readable_and_idempotent() -> None:
    footer, metadata = provenance_block(
        model="deepseek-flash",
        harness="deepseek-harness",
        reported_at=NOW,
        credential_owner="hezongyi",
        source="docs/webui-user-guide.md review (PR #87)",
        repo_version="0.4.1",
        commit="49ddbc55361d6cd04c25a27b42d1dcb17c2d918b",
        deployment_id="49ddbc55361d-bd11ad0e",
    )
    assert "AI agent" in footer
    assert metadata.startswith("<!-- agent-report")
    assert "credential_owner: hezongyi" in metadata
    assert "baseline_commit: 49ddbc5" in metadata

    body = annotate_body("## problem\n", footer, metadata)
    assert "<!-- agent-report" in body
    assert annotate_body(body, footer, metadata) == body
