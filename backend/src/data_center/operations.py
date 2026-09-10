"""Read-only retention audit and bounded daily backfill via the ingest API."""
import argparse
import ctypes
import errno
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests

from data_center.capacity import CapacityPolicy
from data_center.evidence import (
    operation_receipt,
    source_commit,
    utc_now,
    write_receipt,
)
from data_center.settings import Settings
from data_center.snapshot import ReceiptIndex

BACKUP_FORMAT_V1 = "market-data-center-backup.v1"
BACKUP_FORMAT_V2 = "market-data-center-backup.v2"
COPY_BUFFER_BYTES = 1024 * 1024
AT_FDCWD = -100
RENAME_NOREPLACE = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup_files(root: Path, ledger_path: Path) -> list[tuple[Path, str]]:
    """Return only manifest-published Data Center bytes plus the ledger."""
    root = root.resolve()
    selected: dict[Path, str] = {}
    manifest_root = root / ".manifests"
    for manifest_path in sorted(manifest_root.glob("*.json")):
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("published manifest must be a regular file")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("status") != "published" or not manifest.get("parts"):
            raise ValueError("invalid published manifest")
        selected[manifest_path.resolve()] = str(Path("canonical") / manifest_path.relative_to(root))
        for item in manifest["parts"]:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("unsafe published part path")
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
                raise ValueError("published part is unavailable")
            if path.stat().st_size != item["bytes"] or _sha256(path) != item["sha256"]:
                raise ValueError("published part failed backup validation")
            selected[path] = str(Path("canonical") / relative)
    files = sorted(selected.items(), key=lambda item: item[1])
    ledger_path = ledger_path.resolve()
    if (ledger_path.exists() and ledger_path.is_file() and not ledger_path.is_symlink()
            and ledger_path not in selected):
        files.append((ledger_path, "ledger.sqlite"))
    return files


def _snapshot_ledger(path: Path, directory: Path) -> Path:
    """Take a consistent SQLite snapshot while the worker heartbeat may be writing."""
    snapshot = directory / f".ledger-{uuid4().hex}.snapshot"
    try:
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    except sqlite3.DatabaseError:
        snapshot.unlink(missing_ok=True)
        shutil.copyfile(path, snapshot)
    return snapshot


def _check_backup_path(path: Path, *, base: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"{label} must be inside its configured root")
    return resolved


def _atomic_publish_no_replace(temporary: Path, target: Path) -> None:
    """Atomically rename without replacement; use link/unlink only if renameat2 is unavailable."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        result = renameat2(
            AT_FDCWD, os.fsencode(temporary), AT_FDCWD, os.fsencode(target), RENAME_NOREPLACE
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(target)
        if error not in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise OSError(error, os.strerror(error), target)
    os.link(temporary, target)
    temporary.unlink()


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _publish_and_sync(temporary: Path, target: Path) -> None:
    _atomic_publish_no_replace(temporary, target)
    try:
        _fsync_directory(target.parent)
    except Exception:
        # Return the newly published inode to an identifiable temporary name.
        if target.exists() and not temporary.exists():
            os.rename(target, temporary)
        raise


def _identity(path: Path, *, base: Path | None = None) -> dict:
    stat = path.stat()
    return {
        "relative_path": str(path.relative_to(base)) if base is not None else path.name,
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _read_metadata(archive: tarfile.TarFile) -> dict:
    member = archive.getmember("backup.json")
    if not member.isfile() or member.size > 10 * 1024 * 1024:
        raise ValueError("invalid backup metadata")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("backup metadata is missing")
    return json.loads(stream.read().decode("utf-8"))


def _verify_backup(archive_path: Path) -> dict:
    """Stream every member and prove the archive matches its declared metadata."""
    archive_path = Path(archive_path)
    if archive_path.name.endswith(".partial"):
        raise ValueError("temporary backup artifacts are not restorable")
    with tarfile.open(archive_path, "r:gz") as archive:
        metadata = _read_metadata(archive)
        if metadata.get("format") not in {BACKUP_FORMAT_V1, BACKUP_FORMAT_V2}:
            raise ValueError("unsupported backup format")
        expected = {item["path"]: item for item in metadata.get("files", [])}
        observed = set()
        for member in archive.getmembers():
            if member.name == "backup.json":
                continue
            if (member.isdir() or member.issym() or member.islnk() or not member.isfile()
                    or Path(member.name).is_absolute() or ".." in Path(member.name).parts):
                raise ValueError("unsafe backup member")
            item = expected.get(member.name)
            if item is None or member.size != item["bytes"]:
                raise ValueError("backup member metadata mismatch")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("backup member is unreadable")
            digest = hashlib.sha256()
            size = 0
            while block := stream.read(COPY_BUFFER_BYTES):
                size += len(block)
                digest.update(block)
            if size != item["bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError("backup member hash mismatch")
            observed.add(member.name)
        if observed != set(expected):
            raise ValueError("backup file list is incomplete")
    report = {
        "status": "pass", "format": metadata["format"], "file_count": len(expected),
        "archive_sha256": _sha256(archive_path), "metadata": metadata,
    }
    return report


def verify_backup(archive_path: Path, *, evidence_root: Path | None = None) -> dict:
    started_at = utc_now()
    try:
        report = _verify_backup(archive_path)
    except Exception as exc:
        write_receipt(evidence_root, operation_receipt(
            action="backup_verify", command="data_center.operations verify", started_at=started_at,
            result="failed", failure_stage="verify", error_category=type(exc).__name__,
        ))
        raise
    receipt_path = write_receipt(evidence_root, operation_receipt(
        action="backup_verify", command="data_center.operations verify", started_at=started_at,
        result="pass", details={"format": report["format"], "file_count": report["file_count"]},
    ))
    if receipt_path:
        report["receipt"] = str(receipt_path)
    return report


def create_backup(root: Path, ledger_path: Path, destination: Path, *,
                  require_distinct_device: bool = False, evidence_root: Path | None = None) -> dict:
    """Create a verified v2 archive and atomically publish without overwriting."""
    root, ledger_path, destination = Path(root), Path(ledger_path), Path(destination)
    _check_backup_path(ledger_path, base=root, label="ledger path")
    if destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("backup destination must be outside canonical root")
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    distinct_device = root.stat().st_dev != destination.parent.stat().st_dev
    if require_distinct_device and not distinct_device:
        raise ValueError("backup destination must use a distinct storage device")
    ledger_snapshot = _snapshot_ledger(ledger_path, destination.parent) if ledger_path.is_file() else None
    files = _backup_files(root, ledger_path)
    if ledger_snapshot is not None:
        files = [(ledger_snapshot if archive_path == "ledger.sqlite" else path, archive_path)
                 for path, archive_path in files]
    entries = [{"path": archive_path, "bytes": path.stat().st_size, "sha256": _sha256(path)}
               for path, archive_path in files]
    metadata = {
        "format": BACKUP_FORMAT_V2,
        "source_commit": source_commit(),
        "created_at": utc_now(),
        "canonical_identity": _identity(root),
        "ledger_identity": _identity(ledger_path, base=root),
        "fault_domain": {"distinct_device": distinct_device, "validated": require_distinct_device},
        "files": entries,
    }
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.partial")
    started_at = utc_now()
    try:
        with temporary.open("xb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                for path, archive_path in files:
                    archive.add(path, arcname=archive_path, recursive=False)
                encoded = json.dumps(metadata, sort_keys=True, indent=2).encode()
                info = tarfile.TarInfo("backup.json")
                info.size = len(encoded)
                info.mtime = 0
                archive.addfile(info, io.BytesIO(encoded))
            raw.flush()
            os.fsync(raw.fileno())
        # verify_backup rejects partial names by contract, so validate via a private temporary alias.
        verification_alias = temporary.with_name(temporary.name + ".verify")
        os.link(temporary, verification_alias)
        try:
            verified = _verify_backup(verification_alias)
        finally:
            verification_alias.unlink(missing_ok=True)
        _publish_and_sync(temporary, destination)
    except Exception as exc:
        receipt = operation_receipt(
            action="backup", command="data_center.operations backup", started_at=started_at,
            result="failed", failure_stage="create_or_verify",
            error_category=type(exc).__name__, details={"temporary_artifact": temporary.name},
        )
        write_receipt(evidence_root, receipt)
        raise
    finally:
        if ledger_snapshot is not None:
            ledger_snapshot.unlink(missing_ok=True)
    report = {**metadata, "status": "pass", "archive": str(destination),
              "archive_sha256": verified["archive_sha256"], "file_count": len(entries),
              "bytes": sum(item["bytes"] for item in entries)}
    receipt = operation_receipt(
        action="backup", command="data_center.operations backup", started_at=started_at,
        result="pass", details={"format": BACKUP_FORMAT_V2, "file_count": len(entries),
                                "bytes": report["bytes"], "distinct_device": distinct_device},
    )
    receipt_path = write_receipt(evidence_root, receipt)
    if receipt_path:
        report["receipt"] = str(receipt_path)
    return report


def _restore_backup(archive_path: Path, root: Path, ledger_path: Path) -> dict:
    """Stream into verified temporary files; publish missing targets without overwrite."""
    archive_path, root, ledger_path = Path(archive_path), Path(root), Path(ledger_path)
    _check_backup_path(ledger_path, base=root, label="ledger path")
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    if archive_path.name.endswith(".partial"):
        raise ValueError("temporary backup artifacts are not restorable")
    _verify_backup(archive_path)
    root.mkdir(parents=True, exist_ok=True)
    restored = skipped = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        try:
            metadata = _read_metadata(archive)
        except KeyError as exc:
            raise ValueError("backup metadata is missing") from exc
        if metadata.get("format") not in {BACKUP_FORMAT_V1, BACKUP_FORMAT_V2}:
            raise ValueError("unsupported backup format")
        expected = {item["path"]: item for item in metadata.get("files", [])}
        for member in members:
            if member.name == "backup.json" or member.isdir():
                continue
            if member.issym() or member.islnk() or Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                raise ValueError("unsafe backup member")
            if member.name not in expected or not member.isfile():
                raise ValueError("unexpected backup member")
            target = (root / Path(member.name).relative_to("canonical") if member.name.startswith("canonical/")
                      else ledger_path if member.name == "ledger.sqlite" else None)
            if target is None:
                raise ValueError("unsupported backup member")
            target = target.resolve()
            if member.name.startswith("canonical/") and not target.is_relative_to(root.resolve()):
                raise ValueError("backup path escapes canonical root")
            if target.exists():
                if (not target.is_file() or target.stat().st_size != expected[member.name]["bytes"]
                        or _sha256(target) != expected[member.name]["sha256"]):
                    raise ValueError(f"existing restore target differs: {member.name}")
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("backup member is unreadable")
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.restore.partial")
            digest = hashlib.sha256()
            size = 0
            with temporary.open("xb") as stream:
                while block := source.read(COPY_BUFFER_BYTES):
                    stream.write(block)
                    digest.update(block)
                    size += len(block)
                stream.flush()
                os.fsync(stream.fileno())
            item = expected[member.name]
            if size != item["bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError(f"restored file failed verification: {member.name}")
            try:
                _publish_and_sync(temporary, target)
            except FileExistsError:
                if target.stat().st_size != item["bytes"] or _sha256(target) != item["sha256"]:
                    raise ValueError(f"existing restore target differs: {member.name}")
                skipped += 1
                temporary.unlink(missing_ok=True)
                continue
            restored += 1
    report = {"status": "pass", "format": metadata["format"], "archive": str(archive_path),
              "restored": restored, "skipped": skipped}
    return report


def restore_backup(archive_path: Path, root: Path, ledger_path: Path, *,
                   evidence_root: Path | None = None) -> dict:
    started_at = utc_now()
    try:
        report = _restore_backup(archive_path, root, ledger_path)
    except Exception as exc:
        write_receipt(evidence_root, operation_receipt(
            action="restore", command="data_center.operations restore", started_at=started_at,
            result="failed", failure_stage="verify_or_restore", error_category=type(exc).__name__,
        ))
        raise
    receipt_path = write_receipt(evidence_root, operation_receipt(
        action="restore", command="data_center.operations restore", started_at=started_at,
        result="pass", details={"format": report["format"], "restored": report["restored"],
                                "skipped": report["skipped"]},
    ))
    if receipt_path:
        report["receipt"] = str(receipt_path)
    return report


def _recovery_drill(root: Path, ledger_path: Path, destination: Path, *,
                    require_distinct_device: bool = False,
                    evidence_root: Path | None = None) -> dict:
    """Backup and restore into an isolated directory, then compare every byte."""
    root, ledger_path, destination = Path(root), Path(ledger_path), Path(destination)
    if destination.exists():
        raise FileExistsError(f"drill destination already exists: {destination}")
    destination.mkdir(parents=True)
    archive = destination / "backup.tar.gz"
    report = create_backup(root, ledger_path, archive, require_distinct_device=require_distinct_device,
                           evidence_root=evidence_root)
    restored_root = destination / "restored-canonical"
    restored_ledger = restored_root / "audit" / "data_center.sqlite"
    restored = restore_backup(archive, restored_root, restored_ledger, evidence_root=evidence_root)
    expected = {item["path"]: item["sha256"] for item in report["files"]}
    recovered_files = _backup_files(restored_root, restored_ledger)
    recovered = {name: digest for path, name in recovered_files for digest in [_sha256(path)]}
    if expected != recovered:
        raise ValueError("backup recovery byte comparison failed")
    result = {"status": "pass", "archive": report, "restore": restored, "file_count": len(expected)}
    return result


def recovery_drill(root: Path, ledger_path: Path, destination: Path, *,
                   require_distinct_device: bool = False, evidence_root: Path | None = None) -> dict:
    started_at = utc_now()
    try:
        result = _recovery_drill(
            root, ledger_path, destination, require_distinct_device=require_distinct_device,
            evidence_root=evidence_root,
        )
    except Exception as exc:
        write_receipt(evidence_root, operation_receipt(
            action="recovery_drill", command="data_center.operations recovery-drill",
            started_at=started_at, result="failed", failure_stage="backup_or_restore",
            error_category=type(exc).__name__,
        ))
        raise
    receipt_path = write_receipt(evidence_root, operation_receipt(
        action="recovery_drill", command="data_center.operations recovery-drill",
        started_at=started_at, result="pass",
        details={"file_count": result["file_count"], "byte_identical": True,
                 "distinct_device": result["archive"]["fault_domain"]["distinct_device"]},
    ))
    if receipt_path:
        result["receipt"] = str(receipt_path)
    return result


def retention_audit(root: Path, keep_days=30, capacity_policy: CapacityPolicy | None = None,
                    evidence_root: Path | None = None):
    cutoff = time.time() - keep_days * 86400
    canonical = {"files": 0, "bytes": 0, "older_than_days": keep_days, "old_files": 0, "old_bytes": 0}
    staging = {"files": 0, "bytes": 0}
    published = [path for path, archive_name in _backup_files(root, root / ".no-ledger")
                 if archive_name.startswith("canonical/") and path.suffix == ".parquet"]
    for path in published:
        stat = path.stat()
        canonical["files"] += 1
        canonical["bytes"] += stat.st_size
        if stat.st_mtime < cutoff:
            canonical["old_files"] += 1
            canonical["old_bytes"] += stat.st_size
    for path in (root / ".ingest-staging").rglob("*.parquet"):
        if path.is_file() and not path.is_symlink():
            staging["files"] += 1
            staging["bytes"] += path.stat().st_size
    capacity = (capacity_policy or CapacityPolicy()).inspect(root).as_dict()
    report = {"event": "retention_audit", "checked_at": datetime.now(timezone.utc).isoformat(),
              "policy": "audit_only_no_deletion", "canonical": canonical, "staging": staging,
              "capacity": capacity}
    receipt = operation_receipt(
        action="capacity_check", command="data_center.operations retention-audit",
        started_at=report["checked_at"], result="pass", details={"capacity": capacity},
    )
    receipt_path = write_receipt(evidence_root, receipt)
    if receipt_path:
        report["receipt"] = str(receipt_path)
    return report


def daily_chunks(start: date, end: date):
    if end <= start or (end - start).days > 3660:
        raise ValueError("backfill requires 0 < end-start <= 3660 days")
    while start < end:
        stop = min(start + timedelta(days=365), end)
        yield start, stop
        start = stop


def backfill(base_url, provider, symbol, asset_class, start, end, output, *, canonical_root: Path | None = None,
             capacity_policy: CapacityPolicy | None = None):
    chunks = list(daily_chunks(start, end))
    if canonical_root is not None:
        (capacity_policy or CapacityPolicy()).require_backfill_capacity(
            canonical_root, requested_days=(end - start).days
        )
    if output.exists():
        report = json.loads(output.read_text())
        if report.get("status") == "pass":
            if "run_count" not in report:
                report["run_count"] = len(report.get("runs", []))
                report["row_count"] = sum(entry.get("row_count", 0) for entry in report.get("runs", []))
                report["output_hashes"] = [entry.get("output_hash") for entry in report.get("runs", [])]
                output.write_text(json.dumps(report, indent=2))
            return report
        if report.get("provider") != provider or report.get("symbol") != symbol:
            raise ValueError("existing backfill receipt has different request")
        report["status"] = "running"
    else:
        report = {"status": "running", "provider": provider, "symbol": symbol,
                  "requested_start": start.isoformat(), "requested_end": end.isoformat(), "runs": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    with requests.Session() as session:
        session.trust_env = False
        if os.getenv("DATACENTER_API_KEY"):
            session.headers["X-API-Key"] = os.environ["DATACENTER_API_KEY"]

        def call(method, path, **kwargs):
            response = session.request(method, base_url.rstrip("/") + "/api/v1" + path, timeout=15, **kwargs)
            response.raise_for_status()
            return response.json()["data"]

        try:
            for index, (first, last) in enumerate(chunks):
                if canonical_root is not None:
                    (capacity_policy or CapacityPolicy()).require_ingest_capacity(canonical_root)
                if index < len(report["runs"]) and report["runs"][index].get("receipt", {}).get("status") == "pass":
                    continue
                job = {"job_id": "backfill-" + output.stem, "provider": provider, "symbol": symbol,
                       "run_scope": "migration",
                       "asset_class": asset_class, "timeframe": "1d",
                       "start": first.isoformat() + "T00:00:00Z", "end": last.isoformat() + "T00:00:00Z"}
                receipt = call("POST", "/ingest/runs", json=job)
                entry = {"request": job, "requested_range": {"start": first.isoformat(), "end": last.isoformat()},
                         "receipt": receipt}
                if index < len(report["runs"]):
                    report["runs"][index] = entry
                else:
                    report["runs"].append(entry)
                output.write_text(json.dumps(report, indent=2))
                deadline = time.monotonic() + 480
                while receipt["status"] in {"queued", "running"}:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("backfill polling deadline; inspect recorded run before resubmitting")
                    time.sleep(1)
                    receipt = call("GET", "/runs/" + receipt["run_id"])
                report["runs"][index]["receipt"] = receipt
                output.write_text(json.dumps(report, indent=2))
                if receipt["status"] != "pass" or not receipt.get("row_count"):
                    raise ValueError("backfill run did not pass")
                report["runs"][index]["actual_range"] = {"start": receipt.get("min_ts") or receipt.get("min_date"),
                                                           "end": receipt.get("max_ts") or receipt.get("max_date")}
                report["runs"][index]["row_count"] = receipt.get("row_count")
                report["runs"][index]["output_hash"] = receipt.get("output_hash")
                report["runs"][index]["quality_summary"] = receipt.get("quality_summary")
                time.sleep(5)
            report["status"] = "pass"
            report["run_count"] = len(report["runs"])
            report["row_count"] = sum(entry.get("row_count", 0) for entry in report["runs"])
            report["output_hashes"] = [entry.get("output_hash") for entry in report["runs"]]
        except Exception as exc:
            report.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            output.write_text(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("retention-audit")
    audit.add_argument("--root", type=Path, default=Settings().canonical_root)
    fill = commands.add_parser("backfill")
    fill.add_argument("--base-url", default="http://127.0.0.1:18380")
    fill.add_argument("--provider", choices=["binance", "yfinance", "fixture"], required=True)
    fill.add_argument("--symbol", required=True)
    fill.add_argument("--asset-class", required=True)
    fill.add_argument("--start", type=date.fromisoformat, required=True)
    fill.add_argument("--end", type=date.fromisoformat, required=True)
    fill.add_argument("--output", type=Path, required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--root", type=Path, default=Settings().canonical_root)
    backup.add_argument("--ledger", type=Path, default=Settings().ledger_path)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--allow-same-device", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--root", type=Path, required=True)
    restore.add_argument("--ledger", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    drill = commands.add_parser("recovery-drill")
    drill.add_argument("--root", type=Path, default=Settings().canonical_root)
    drill.add_argument("--ledger", type=Path, default=Settings().ledger_path)
    drill.add_argument("--destination", type=Path, required=True)
    drill.add_argument("--allow-same-device", action="store_true")
    commands.add_parser("rebuild-receipt-index")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "retention-audit":
        print(json.dumps(retention_audit(args.root, capacity_policy=settings.capacity_policy(),
                                         evidence_root=settings.evidence_root)), flush=True)
    elif args.command == "backfill":
        report = backfill(args.base_url, args.provider, args.symbol, args.asset_class, args.start, args.end, args.output,
                          canonical_root=settings.canonical_root, capacity_policy=settings.capacity_policy())
        print(json.dumps({"status": report["status"], "run_count": len(report["runs"])}), flush=True)
    elif args.command == "backup":
        print(json.dumps(create_backup(args.root, args.ledger, args.destination,
                                       require_distinct_device=not args.allow_same_device,
                                       evidence_root=settings.evidence_root)), flush=True)
    elif args.command == "restore":
        print(json.dumps(restore_backup(args.archive, args.root, args.ledger,
                                        evidence_root=settings.evidence_root)), flush=True)
    elif args.command == "verify":
        print(json.dumps(verify_backup(args.archive, evidence_root=settings.evidence_root)), flush=True)
    elif args.command == "recovery-drill":
        print(json.dumps(recovery_drill(args.root, args.ledger, args.destination,
                                       require_distinct_device=not args.allow_same_device,
                                       evidence_root=settings.evidence_root)), flush=True)
    else:
        print(json.dumps(ReceiptIndex(settings.evidence_root).rebuild()), flush=True)


if __name__ == "__main__":
    main()
