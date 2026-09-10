"""Read-only retention audit and bounded daily backfill via the ingest API."""
import argparse
import hashlib
import io
import json
import os
import shutil
import tarfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from data_center.settings import Settings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup_files(root: Path, ledger_path: Path) -> list[tuple[Path, str]]:
    """Return canonical and ledger files without including live staging or symlinks."""
    root = root.resolve()
    files: list[tuple[Path, str]] = []
    if root.exists():
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if ".ingest-staging" in relative.parts:
                continue
            files.append((path, str(Path("canonical") / relative)))
    ledger_path = ledger_path.resolve()
    if ledger_path.exists() and ledger_path.is_file() and not ledger_path.is_symlink() and ledger_path not in {path for path, _ in files}:
        files.append((ledger_path, "ledger.sqlite"))
    return files


def _check_backup_path(path: Path, *, base: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise ValueError(f"{label} must be inside its configured root")
    return resolved


def create_backup(root: Path, ledger_path: Path, destination: Path) -> dict:
    """Create a non-destructive, compressed backup of published data and the ledger."""
    root, ledger_path, destination = Path(root), Path(ledger_path), Path(destination)
    _check_backup_path(ledger_path, base=root, label="ledger path")
    if destination.exists():
        raise FileExistsError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = _backup_files(root, ledger_path)
    entries = [{"path": archive_path, "bytes": path.stat().st_size, "sha256": _sha256(path)}
               for path, archive_path in files]
    with tarfile.open(destination, "w:gz") as archive:
        for path, archive_path in files:
            archive.add(path, arcname=archive_path, recursive=False)
        metadata = {
            "format": "market-data-center-backup.v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "canonical_root": str(root.resolve()),
            "ledger_path": str(ledger_path.resolve()),
            "files": entries,
        }
        encoded = json.dumps(metadata, sort_keys=True, indent=2).encode()
        info = tarfile.TarInfo("backup.json")
        info.size = len(encoded)
        info.mtime = 0
        archive.addfile(info, io.BytesIO(encoded))
    return {**metadata, "status": "pass", "archive": str(destination),
            "archive_sha256": _sha256(destination), "file_count": len(entries),
            "bytes": sum(item["bytes"] for item in entries)}


def restore_backup(archive_path: Path, root: Path, ledger_path: Path) -> dict:
    """Restore only missing bytes; conflicting existing bytes fail closed."""
    archive_path, root, ledger_path = Path(archive_path), Path(root), Path(ledger_path)
    _check_backup_path(ledger_path, base=root, label="ledger path")
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    root.mkdir(parents=True, exist_ok=True)
    restored = skipped = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        metadata_member = next((item for item in members if item.name == "backup.json"), None)
        if metadata_member is None:
            raise ValueError("backup metadata is missing")
        metadata = json.loads(archive.extractfile(metadata_member).read())
        if metadata.get("format") != "market-data-center-backup.v1":
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
            data = archive.extractfile(member).read()
            if target.exists():
                if not target.is_file() or _sha256(target) != expected[member.name]["sha256"]:
                    raise ValueError(f"existing restore target differs: {member.name}")
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
            if _sha256(target) != expected[member.name]["sha256"]:
                target.unlink(missing_ok=True)
                raise ValueError(f"restored file failed verification: {member.name}")
            restored += 1
    return {"status": "pass", "archive": str(archive_path), "restored": restored, "skipped": skipped}


def recovery_drill(root: Path, ledger_path: Path, destination: Path) -> dict:
    """Backup and restore into an isolated directory, then compare every byte."""
    root, ledger_path, destination = Path(root), Path(ledger_path), Path(destination)
    if destination.exists():
        raise FileExistsError(f"drill destination already exists: {destination}")
    destination.mkdir(parents=True)
    archive = destination / "backup.tar.gz"
    report = create_backup(root, ledger_path, archive)
    restored_root = destination / "restored-canonical"
    restored_ledger = restored_root / "audit" / "data_center.sqlite"
    restored = restore_backup(archive, restored_root, restored_ledger)
    original = {name: digest for path, name in _backup_files(root, ledger_path) for digest in [_sha256(path)]}
    recovered_files = _backup_files(restored_root, restored_ledger)
    recovered = {name: digest for path, name in recovered_files for digest in [_sha256(path)]}
    if original != recovered:
        raise ValueError("backup recovery byte comparison failed")
    return {"status": "pass", "archive": report, "restore": restored, "file_count": len(original)}


def retention_audit(root: Path, keep_days=30):
    cutoff = time.time() - keep_days * 86400
    canonical = {"files": 0, "bytes": 0, "older_than_days": keep_days, "old_files": 0, "old_bytes": 0}
    staging = {"files": 0, "bytes": 0}
    for path in root.rglob("*.parquet"):
        stat = path.stat()
        target = staging if ".ingest-staging" in path.relative_to(root).parts else canonical
        target["files"] += 1
        target["bytes"] += stat.st_size
        if target is canonical and stat.st_mtime < cutoff:
            target["old_files"] += 1
            target["old_bytes"] += stat.st_size
    usage = shutil.disk_usage(root)
    capacity = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
                "free_ratio": usage.free / usage.total if usage.total else None}
    return {"event": "retention_audit", "checked_at": datetime.now(timezone.utc).isoformat(),
            "policy": "audit_only_no_deletion", "canonical": canonical, "staging": staging,
            "capacity": capacity}


def daily_chunks(start: date, end: date):
    if end <= start or (end - start).days > 3660:
        raise ValueError("backfill requires 0 < end-start <= 3660 days")
    while start < end:
        stop = min(start + timedelta(days=365), end)
        yield start, stop
        start = stop


def backfill(base_url, provider, symbol, asset_class, start, end, output):
    chunks = list(daily_chunks(start, end))
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
                if index < len(report["runs"]) and report["runs"][index].get("receipt", {}).get("status") == "pass":
                    continue
                job = {"job_id": "backfill-" + output.stem, "provider": provider, "symbol": symbol,
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
    restore = commands.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--root", type=Path, required=True)
    restore.add_argument("--ledger", type=Path, required=True)
    drill = commands.add_parser("recovery-drill")
    drill.add_argument("--root", type=Path, default=Settings().canonical_root)
    drill.add_argument("--ledger", type=Path, default=Settings().ledger_path)
    drill.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "retention-audit":
        print(json.dumps(retention_audit(args.root)), flush=True)
    elif args.command == "backfill":
        report = backfill(args.base_url, args.provider, args.symbol, args.asset_class, args.start, args.end, args.output)
        print(json.dumps({"status": report["status"], "run_count": len(report["runs"])}), flush=True)
    elif args.command == "backup":
        print(json.dumps(create_backup(args.root, args.ledger, args.destination)), flush=True)
    elif args.command == "restore":
        print(json.dumps(restore_backup(args.archive, args.root, args.ledger)), flush=True)
    else:
        print(json.dumps(recovery_drill(args.root, args.ledger, args.destination)), flush=True)


if __name__ == "__main__":
    main()
