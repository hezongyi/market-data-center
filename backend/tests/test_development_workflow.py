"""Regression tests for CI selection, evidence identity and preview handoff."""
import copy
import importlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
policy = importlib.import_module("ci_policy")
identity = importlib.import_module("code_identity")
receipt = importlib.import_module("ci_receipt")
runner = importlib.import_module("ci_runner")
links = importlib.import_module("docs_link_check")
preview = importlib.import_module("dev_preview")


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "Workflow tests")
    (tmp_path / "README.md").write_text("# Hello\n")
    (tmp_path / "app.py").write_text("value = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    return tmp_path


@pytest.mark.parametrize("event,paths,expected", [
    ("pull_request", ["docs/guide.md", "AGENTS.md"], "docs"),
    ("pull_request", [".github/PULL_REQUEST_TEMPLATE.md"], "docs"),
    ("pull_request", ["docs/guide.md", "backend/src/app.py"], "full"),
    ("pull_request", [".github/workflows/ci.yml"], "full"),
    ("pull_request", ["webui/package-lock.json"], "full"),
    ("pull_request", ["scripts/new.md"], "full"),
    ("pull_request", [], "full"),
    ("push", ["README.md"], "full"),
    ("workflow_dispatch", ["README.md"], "full"),
])
def test_check_selection_is_conservative(event, paths, expected):
    assert policy.select_mode(event, paths) == expected


def test_git_plan_sees_renamed_code_and_symlinked_docs(repository):
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "mv", "app.py", "app.md")
    git(repository, "commit", "-qm", "rename code")
    assert policy.make_plan(repository, "pull_request", base)["mode"] == "full"
    base = git(repository, "rev-parse", "HEAD")
    (repository / "linked.md").symlink_to("README.md")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "symlink")
    assert policy.make_plan(repository, "pull_request", base)["mode"] == "full"


def test_git_plan_accepts_only_resolved_doc_changes(repository):
    base = git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("# Changed\n")
    git(repository, "add", ".")
    git(repository, "commit", "-qm", "docs")
    assert policy.make_plan(repository, "pull_request", base)["mode"] == "docs"
    with pytest.raises(subprocess.CalledProcessError):
        policy.make_plan(repository, "pull_request", "missing-reference")
    with pytest.raises(ValueError):
        policy.make_plan(repository, "pull_request", None)


@pytest.mark.parametrize("mode", ["docs", "full", "", "unknown"])
def test_aggregate_rejects_all_unexpected_job_states(mode):
    names = ("plan", "docs", "backend", "web")
    for values in itertools.product(("success", "skipped", "failure", "cancelled", ""), repeat=4):
        expected = ("success", "success", "skipped", "skipped") if mode == "docs" else ("success",) * 4
        assert policy.verify_results(mode, dict(zip(names, values))) == (mode in {"docs", "full"} and values == expected)


def test_identity_tracks_staged_and_untracked_changes(repository):
    clean = identity.checkout_identity(repository)
    (repository / "app.py").write_text("value = 2\n")
    dirty = identity.checkout_identity(repository)
    assert clean["commit"] == dirty["commit"]
    assert clean["worktree_fingerprint"] != dirty["worktree_fingerprint"]
    git(repository, "add", "app.py")
    staged = identity.checkout_identity(repository)
    assert staged["dirty"] is True
    (repository / "new.py").write_text("value = 3\n")
    untracked = identity.checkout_identity(repository)
    (repository / "new.py").write_text("value = 4\n")
    assert untracked != identity.checkout_identity(repository)
    assert staged != untracked


def matching_receipt():
    source = {"commit": "a", "dirty": True, "worktree_fingerprint": "one"}
    env = {"python": "3.11", "configuration_hash": "config"}
    request = {"scope": "backend-fast", "selectors": ["test_a"],
               "checks": [{"name": "pytest-selected", "command": "pytest test_a"}]}
    value = {"result": "pass", "source": source, "source_after": source, "environment": env,
             "details": {"identity_stable": True, "request": request,
                         "checks": [{**request["checks"][0], "result": "pass", "exit_code": 0}]}}
    return value, source, env, request


def test_evidence_reuse_requires_exact_source_environment_and_coverage():
    value, source, env, request = matching_receipt()
    assert receipt.reusable(value, source, env, request)
    assert not receipt.reusable(value, {**source, "worktree_fingerprint": "two"}, env, request)
    assert not receipt.reusable(value, source, {**env, "configuration_hash": "changed"}, request)
    assert not receipt.reusable(value, source, env, {**request, "selectors": ["test_b"]})
    for corrupt in [None, {}, {**value, "result": "failed"}, {**value, "source_after": {}}, {**value, "details": None}]:
        assert not receipt.reusable(corrupt, source, env, request)
    for field, data in [("checks", []), ("identity_stable", False), ("checks", [{"result": "pass"}])]:
        changed = copy.deepcopy(value)
        changed["details"][field] = data
        assert not receipt.reusable(changed, source, env, request)


def test_check_failure_stops_later_stages_and_records_duration(tmp_path):
    marker = tmp_path / "should-not-run"
    status, stages = runner.run_checks([
        ("failure", [sys.executable, "-c", "raise SystemExit(7)"]),
        ("later", [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]),
    ], tmp_path, dict(os.environ))
    assert status == 7
    assert not marker.exists()
    assert len(stages) == 1 and stages[0]["duration_seconds"] >= 0
    assert stages[0]["result"] == "failed"


def test_runner_rejects_success_when_source_changes_during_checks(repository, monkeypatch):
    (repository / ".gitignore").write_text("acceptance-receipts/\n")
    monkeypatch.setattr(runner, "ROOT", repository)
    monkeypatch.setattr(sys, "argv", ["ci_runner.py", "docs"])
    monkeypatch.delenv("DATACENTER_CI_RECEIPT", raising=False)
    monkeypatch.setattr(runner, "checks", lambda *_: [
        ("mutates", [sys.executable, "-c", "from pathlib import Path; Path('app.py').write_text('changed')"]),
    ])
    assert runner.main() == 1
    report = json.loads(next((repository / "acceptance-receipts/ci").glob("*.json")).read_text())
    assert report["failure_stage"] == "identity-changed"
    assert report["details"]["checks"][0]["result"] == "pass"


def test_document_links_and_duplicate_heading_anchors(tmp_path):
    target = tmp_path / "target.md"
    target.write_text("# 中文标题\n# 中文标题\n")
    source = tmp_path / "source.md"
    source.write_text("[valid](target.md#中文标题-1)\n```md\n[example](not-real.md)\n```\n")
    assert links.check_links([source]) == []
    source.write_text("[missing](target.md#unknown)\n[broken](deleted.md)\n")
    assert len(links.check_links([source])) == 2


def test_preview_card_reports_identity_and_does_not_claim_acceptance(tmp_path):
    source = {"branch": "feature", "commit": "abc", "dirty": False, "worktree_fingerprint": "hash"}
    payload = {"id": "demo", "state": "running", "mode": "fixture", "recorded_identity": source,
               "actual_identity": source, "identity_ok": True, "observed_at": "now",
               "ui_url": "http://127.0.0.1:21001", "api_docs_url": "http://127.0.0.1:21000/docs",
               "processes": {"api": {"running": True}}, "scheduler": {"effective_dispatch": False},
               "data_root": "/tmp/example/data", "logs": "/tmp/example/logs"}
    evidence = tmp_path / "receipt.json"
    evidence.write_text(json.dumps({"result": "pass", "source": {"commit": "old"}}))
    card = preview.render_card(payload, "user@host", evidence)
    assert "ssh -N -L 21001:127.0.0.1:21001 -L 21000:127.0.0.1:21000 user@host" in card
    assert "代码身份匹配=False" in card
    assert "不自动代表产品验收通过" in card
    with pytest.raises(preview.PreviewError):
        preview.render_card(payload, "host; malicious")


def test_release_selection_requires_explicit_annotated_tag(repository, tmp_path):
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    match = re.search(r"      - name: Select release tag.*?        run: \|\n(.*?)      - name:", workflow, re.DOTALL)
    assert match is not None
    script = textwrap.dedent(match.group(1))
    git(repository, "tag", "-a", "v1.2.3", "-m", "release")
    git(repository, "tag", "v1.2.4")
    env = {**os.environ, "GITHUB_OUTPUT": str(tmp_path / "outputs"),
           "GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main"}
    def select(event, tag):
        return subprocess.run(["bash", "-e", "-c", script], cwd=repository,
                              env={**env, "GITHUB_EVENT_NAME": event, "INPUT_TAG": tag},
                              capture_output=True, check=False).returncode
    assert select("workflow_dispatch", "v1.2.3") == 0
    assert select("workflow_dispatch", "v1.2.4") != 0
    assert select("workflow_dispatch", "") != 0
    assert select("workflow_dispatch", "v1.2.3; touch injected") != 0
    assert select("push", "v1.2.3") != 0
    assert not (repository / "injected").exists()


def test_hosted_ci_cannot_reuse_a_local_receipt(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(sys, "argv", ["ci_runner.py", "docs", "--reuse-from", "old.json"])
    with pytest.raises(SystemExit) as stopped:
        runner.main()
    assert stopped.value.code == 2


def test_environment_fingerprint_hides_secrets_and_changes_with_configuration(repository, monkeypatch):
    monkeypatch.setenv("DATACENTER_API_KEY", "private-test-value")
    first = receipt.environment_identity(repository)
    assert "private-test-value" not in json.dumps(first)
    monkeypatch.setenv("DATACENTER_API_KEY", "changed-private-value")
    assert first["configuration_hash"] != receipt.environment_identity(repository)["configuration_hash"]


@pytest.mark.parametrize("name", ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "NODE_OPTIONS", "VITE_API_PROXY_TARGET"])
def test_tool_inputs_invalidate_evidence(repository, monkeypatch, name):
    monkeypatch.delenv(name, raising=False)
    before = receipt.environment_identity(repository)
    monkeypatch.setenv(name, "changed-input")
    assert before["configuration_hash"] != receipt.environment_identity(repository)["configuration_hash"]


def test_docs_scope_rejects_committed_credentials(repository):
    (repository / "README.md").write_text("token: " + "ghp" + "_" + "a" * 30)
    git(repository, "add", ".")
    command = next(command for name, command in runner.checks("docs", []) if name == "secrets")
    command = [command[0], str(ROOT / command[1])]
    result = subprocess.run(command, cwd=repository, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "README.md" in result.stderr
    assert "a" * 30 not in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for the browser cleanup harness")
def test_preview_cleanup_always_attempts_stop_and_preserves_recovery_metadata(tmp_path):
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { cleanupPreview } = require(process.argv[1]);
const root = process.argv[2];
(async () => {
  for (const scenario of ['close-failure', 'stop-failure', 'no-metadata', 'success']) {
    const base = path.join(root, scenario);
    fs.mkdirSync(base);
    const metadataPath = path.join(base, 'preview.json');
    if (scenario !== 'no-metadata') fs.writeFileSync(metadataPath, '{}');
    let attempted = false;
    const operation = cleanupPreview({base, metadataPath,
      browser: {close: async () => { if (scenario === 'close-failure') throw Error('close failed'); }},
      stop: () => { attempted = true; if (scenario === 'stop-failure') throw Error('stop failed');
                    return {state: 'stopped', processes: {api: {running: false}}}; }
    });
    if (scenario === 'success') await operation;
    else await assert.rejects(operation);
    assert.equal(attempted, scenario !== 'no-metadata');
    assert.equal(fs.existsSync(base), ['stop-failure', 'no-metadata'].includes(scenario));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    subprocess.run(["node", "-e", script, str(ROOT / "scripts/preview_cleanup.cjs"), str(tmp_path)], check=True)
