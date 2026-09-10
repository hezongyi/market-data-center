"""Immutable release staging, activation, identity and rollback."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

from data_center.evidence import operation_receipt, write_receipt

RELEASE_FORMAT = "deployment-manifest.v1"
HASH_IGNORED_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
MANIFEST_HASH_FILE = ".deployment.sha256"
HASH_IGNORED_NAMES = {"deployment.json", MANIFEST_HASH_FILE, ".artifact.sha256"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    for item in sorted(path.rglob("*")):
        if not item.is_file() or any(part in HASH_IGNORED_PARTS for part in item.parts):
            continue
        if item.name in HASH_IGNORED_NAMES:
            continue
        digest.update(str(item.relative_to(path)).encode())
        digest.update(sha256_path(item).encode())
    return digest.hexdigest()


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(source), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


@dataclass(frozen=True)
class DeploymentManifest:
    deployment_id: str
    software_version: str
    source_commit: str
    tag: str | None
    artifact_sha256: str
    python_version: str
    constraints_sha256: str | None
    web_ui_asset_sha256: str | None
    created_at: str
    activated_at: str | None
    previous_deployment_id: str | None
    release_format_version: str = RELEASE_FORMAT

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: Path) -> DeploymentManifest:
        payload = json.loads(path.read_text())
        if set(cls.__dataclass_fields__) != set(payload):
            raise ValueError("deployment manifest schema mismatch")
        manifest = cls(**payload)
        if manifest.release_format_version != RELEASE_FORMAT:
            raise ValueError("unsupported deployment manifest format")
        return manifest


def runtime_identity(manifest_path: Path | None = None) -> dict:
    path = Path(manifest_path or os.environ.get("DATACENTER_DEPLOYMENT_MANIFEST", "deployment.json"))
    if not path.is_file():
        raise RuntimeError("deployment manifest is missing")
    hash_path = path.parent / MANIFEST_HASH_FILE
    if not hash_path.is_file() or hash_path.read_text().strip() != sha256_path(path):
        raise RuntimeError("deployment manifest hash mismatch")
    manifest = DeploymentManifest.load(path)
    if sha256_path(path.parent) != manifest.artifact_sha256:
        raise RuntimeError("deployment artifact hash mismatch")
    if (path.parent / ".git").exists():
        raise RuntimeError("deployment may not run from a git checkout")
    version_path = path.parent / "backend" / "src" / "data_center" / "__init__.py"
    if version_path.is_file() and _version(path.parent) != manifest.software_version:
        raise RuntimeError("deployment software version mismatch")
    return manifest.as_dict()


def validated_runtime_identity(
    manifest_path: Path | None,
    evidence_root: Path | None,
    *,
    component: str,
    webui_dist: Path | None = None,
) -> dict:
    """Load identity or fail closed with a path-safe runtime failure receipt."""
    started = utc_now()
    try:
        identity = runtime_identity(manifest_path)
        if webui_dist and sha256_path(Path(webui_dist)) != identity.get("web_ui_asset_sha256"):
            raise RuntimeError("Web UI deployment identity mismatch")
        return identity
    except Exception as exc:
        write_receipt(evidence_root, operation_receipt(
            action="deployment_runtime_failure",
            command=f"data_center.{component} startup",
            started_at=started,
            result="failed",
            failure_stage="runtime_identity",
            error_category=type(exc).__name__,
            details={"component": component},
        ))
        raise


class DeploymentService:
    """The only interface permitted to stage or switch production releases."""

    def __init__(
        self,
        repository: Path,
        release_root: Path,
        *,
        evidence_root: Path | None = None,
        service_names: tuple[str, ...] = (
            "market-data-center-api.service",
            "market-data-center-worker.service",
        ),
        monitor_service: str = "market-data-center-monitor.service",
        ready_url: str = "http://127.0.0.1:18380/api/v1/health/ready",
        systemctl: tuple[str, ...] = ("systemctl", "--user"),
        health_timeout_seconds: float = 45.0,
        protected_ref: str = "origin/main",
    ):
        self.repository = Path(repository).resolve()
        self.release_root = Path(release_root).resolve()
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self.service_names = service_names
        self.monitor_service = monitor_service
        self.ready_url = ready_url
        self.systemctl = systemctl
        self.health_timeout_seconds = health_timeout_seconds
        self.protected_ref = protected_ref
        self.release_root.mkdir(parents=True, exist_ok=True)

    def stage(self, source_ref: str = "HEAD") -> dict:
        started = utc_now()
        try:
            if git(self.repository, "status", "--porcelain"):
                raise ValueError("refusing to stage a dirty checkout")
            commit = git(self.repository, "rev-parse", f"{source_ref}^{{commit}}")
            self._validate_protected_source(commit)
            release_id = f"{commit[:12]}-{uuid4().hex[:8]}"
            target = self.release_root / release_id
            self._export(source_ref, target)
            self._validate_release_tree(target)
            self._build_runtime(target)
            manifest = DeploymentManifest(
                deployment_id=release_id,
                software_version=_version(target),
                source_commit=commit,
                tag=_tag(self.repository, commit),
                artifact_sha256=sha256_path(target),
                python_version=platform.python_version(),
                constraints_sha256=_constraint_hash(target),
                web_ui_asset_sha256=_optional_hash(target / "webui" / "dist"),
                created_at=utc_now(),
                activated_at=None,
                previous_deployment_id=None,
            )
            _atomic_json(target / "deployment.json", manifest.as_dict())
            _atomic_text(target / MANIFEST_HASH_FILE, sha256_path(target / "deployment.json") + "\n")
            runtime_identity(target / "deployment.json")
            _make_read_only(target)
            receipt = self._receipt("deployment_stage", started, "pass", manifest.as_dict())
            return {**manifest.as_dict(), "receipt": receipt}
        except Exception as exc:
            self._receipt("deployment_stage", started, "failed", {}, exc)
            raise

    def activate(self, release_id: str) -> dict:
        return self._switch(release_id, "deployment_activate")

    def rollback(self, release_id: str) -> dict:
        return self._switch(release_id, "deployment_rollback")

    def current(self) -> dict:
        return runtime_identity(self.release_root / "current" / "deployment.json")

    def _switch(self, release_id: str, action: str) -> dict:
        started = utc_now()
        target = self._release_target(release_id)
        identity = runtime_identity(target / "deployment.json")
        previous = self._current_target()
        canonical_hash = _configured_data_hash("DATACENTER_CANONICAL_ROOT")
        ledger_hash = _configured_data_hash("DATACENTER_LEDGER_PATH")
        try:
            self._point_current(target)
            self._restart_and_verify(identity)
        except Exception as exc:
            if previous is not None:
                self._point_current(previous)
                try:
                    self._restart_and_verify(runtime_identity(previous / "deployment.json"))
                except Exception as recovery_exc:
                    self._receipt(action, started, "failed", {
                        "deployment_id": release_id,
                        "failure_recovery": "failed",
                        "recovery_error_category": type(recovery_exc).__name__,
                        "canonical_hash_unchanged": canonical_hash == _configured_data_hash("DATACENTER_CANONICAL_ROOT"),
                        "ledger_hash_unchanged": ledger_hash == _configured_data_hash("DATACENTER_LEDGER_PATH"),
                    }, exc)
                    raise RuntimeError("activation and automatic recovery failed") from recovery_exc
            else:
                current = self.release_root / "current"
                if current.is_symlink():
                    current.unlink()
            self._receipt(action, started, "failed", {
                "deployment_id": release_id,
                "recovered_deployment_id": previous.name if previous else None,
                "canonical_hash_unchanged": canonical_hash == _configured_data_hash("DATACENTER_CANONICAL_ROOT"),
                "ledger_hash_unchanged": ledger_hash == _configured_data_hash("DATACENTER_LEDGER_PATH"),
            }, exc)
            raise
        details = {
            "deployment_id": release_id,
            "previous_deployment_id": previous.name if previous else None,
            "source_commit": identity["source_commit"],
            "canonical_hash_unchanged": canonical_hash == _configured_data_hash("DATACENTER_CANONICAL_ROOT"),
            "ledger_hash_unchanged": ledger_hash == _configured_data_hash("DATACENTER_LEDGER_PATH"),
        }
        return {
            "action": action,
            "result": "pass",
            **details,
            "receipt": self._receipt(action, started, "pass", details),
        }

    def _export(self, source_ref: str, target: Path) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "release.tar"
            with archive.open("wb") as stream:
                subprocess.run(
                    ["git", "-C", str(self.repository), "archive", source_ref],
                    stdout=stream,
                    check=True,
                )
            target.mkdir(parents=True)
            subprocess.run(["tar", "-xf", str(archive), "-C", str(target)], check=True)

    def _validate_protected_source(self, commit: str) -> None:
        try:
            subprocess.run(
                ["git", "-C", str(self.repository), "merge-base", "--is-ancestor", commit, self.protected_ref],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"source commit is not reachable from protected ref {self.protected_ref}") from exc

    @staticmethod
    def _validate_release_tree(target: Path) -> None:
        forbidden_roots = {".git", ".venv", "acceptance-receipts", "node_modules", "var"}
        present = forbidden_roots.intersection(path.name for path in target.iterdir())
        if present or any(target.rglob(".env.local")):
            raise ValueError(f"release source contains forbidden paths: {sorted(present)}")

    def _release_target(self, release_id: str) -> Path:
        if not release_id or Path(release_id).name != release_id or release_id in {".", "..", "current"}:
            raise ValueError("invalid release_id")
        target = self.release_root / release_id
        if target.is_symlink() or not target.is_dir():
            raise ValueError("release_id does not name an immutable release directory")
        resolved = target.resolve()
        if resolved.parent != self.release_root:
            raise ValueError("release_id escapes release root")
        return resolved

    def _build_runtime(self, target: Path) -> None:
        venv = target / ".venv"
        subprocess.run([os.sys.executable, "-m", "venv", str(venv)], check=True)
        constraint = target / "backend" / "constraints" / f"py{os.sys.version_info.major}{os.sys.version_info.minor}.txt"
        command = [str(venv / "bin" / "pip"), "install"]
        if constraint.is_file():
            command += ["-c", str(constraint)]
        command += [str(target / "backend")]
        subprocess.run(command, check=True)
        webui = target / "webui"
        if (webui / "package-lock.json").is_file():
            try:
                subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=webui, check=True)
                subprocess.run(["npm", "run", "build"], cwd=webui, check=True)
            finally:
                shutil.rmtree(webui / "node_modules", ignore_errors=True)

    def _current_target(self) -> Path | None:
        current = self.release_root / "current"
        return current.resolve() if current.is_symlink() else None

    def _point_current(self, target: Path) -> None:
        link = self.release_root / f".current-{uuid4().hex}"
        link.symlink_to(target, target_is_directory=True)
        os.replace(link, self.release_root / "current")

    def _restart_and_verify(self, identity: dict) -> None:
        subprocess.run([*self.systemctl, "daemon-reload"], check=True)
        for service in self.service_names:
            subprocess.run([*self.systemctl, "restart", service], check=True)
        deadline = time.monotonic() + self.health_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urlopen(self.ready_url, timeout=2) as response:
                    payload = json.load(response)["data"]
                    status = response.status
                if status == 200 and all(
                    payload.get(key) == identity[key]
                    for key in ("deployment_id", "software_version", "source_commit")
                ):
                    subprocess.run([*self.systemctl, "start", self.monitor_service], check=True)
                    return
                last_error = RuntimeError("runtime deployment identity mismatch")
            except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(0.5)
        raise RuntimeError("deployment readiness verification failed") from last_error

    def _receipt(self, action: str, started: str, result: str, details: dict, exc: Exception | None = None) -> str | None:
        path = write_receipt(self.evidence_root, operation_receipt(
            action=action,
            command=f"data_center.deployment {action}",
            started_at=started,
            result=result,
            failure_stage="activate_or_verify" if exc else None,
            error_category=type(exc).__name__ if exc else None,
            details=details,
        ))
        return str(path) if path else None


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(value)
    os.replace(temporary, path)


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _version(root: Path) -> str:
    namespace: dict = {}
    exec((root / "backend" / "src" / "data_center" / "__init__.py").read_text(), namespace)  # noqa: S102
    return str(namespace["__version__"])


def _tag(repository: Path, commit: str) -> str | None:
    try:
        return git(repository, "describe", "--tags", "--exact-match", commit) or None
    except subprocess.CalledProcessError:
        return None


def _constraint_hash(root: Path) -> str | None:
    path = root / "backend" / "constraints" / f"py{os.sys.version_info.major}{os.sys.version_info.minor}.txt"
    return sha256_path(path) if path.is_file() else None


def _optional_hash(path: Path) -> str | None:
    return sha256_path(path) if path.exists() else None


def _configured_data_hash(name: str) -> str | None:
    value = os.environ.get(name)
    path = Path(value) if value else None
    if not path or not path.exists():
        return None
    if path.is_file():
        return sha256_path(path)
    digest = hashlib.sha256()
    for item in sorted(path.glob("**/manifest.json")):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(sha256_path(item).encode())
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("stage", "activate", "current", "rollback"))
    parser.add_argument("value", nargs="?")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args()
    service = DeploymentService(args.repository, args.release_root, evidence_root=args.evidence_root)
    if args.command == "stage":
        result = service.stage(args.value or "HEAD")
    elif args.command == "current":
        result = service.current()
    elif args.command == "activate":
        result = service.activate(args.value or parser.error("activate requires release_id"))
    else:
        result = service.rollback(args.value or parser.error("rollback requires release_id"))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
