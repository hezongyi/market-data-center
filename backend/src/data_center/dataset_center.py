"""Small, durable control-plane for dataset-centred EURUSD maintenance."""
from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4


class DatasetStateError(RuntimeError):
    """The durable managed-dataset control plane cannot be read safely."""


def managed_dataset_root(canonical_root: Path, dataset_id: str | None) -> Path:
    """Resolve the only canonical root a managed dataset may read or write."""
    root = Path(canonical_root)
    if dataset_id is None:
        return root
    if not dataset_id or Path(dataset_id).name != dataset_id or dataset_id in {".", ".."}:
        raise ValueError("invalid managed dataset id")
    return root / "datasets" / dataset_id / "canonical"


@dataclass
class DatasetMember:
    symbol: str
    history_start: str | None = None
    derived_targets: tuple[str, ...] | None = None
    status: str = "active"


@dataclass
class ManagedDataset:
    dataset_id: str
    name: str
    provider: str = "dukascopy"
    asset_class: str = "fx"
    price_type: str = "bid"
    base_timeframe: str = "1m"
    history_start: str | None = None
    derived_targets: tuple[str, ...] = ("5m",)
    notes: str = ""
    schedule: str = "manual"
    status: str = "active"
    version: int = 1
    members: dict[str, DatasetMember] = field(default_factory=dict)

    def member(self, symbol: str) -> DatasetMember:
        """Resolve the canonical member identity used by every dataset seam."""
        return self.members[symbol.upper()]

    def effective_derived_targets(self, symbol: str) -> tuple[str, ...]:
        member = self.member(symbol)
        return (member.derived_targets
                if member.derived_targets is not None else self.derived_targets)

    def as_dict(self) -> dict:
        value = asdict(self)
        value["derived_targets"] = list(self.derived_targets)
        value["members"] = {}
        for key, member in self.members.items():
            raw = asdict(member)
            raw["derived_targets"] = list(member.derived_targets) if member.derived_targets is not None else None
            raw["effective_history_start"] = member.history_start or self.history_start
            effective_targets = self.effective_derived_targets(key)
            raw["effective_derived_targets"] = list(effective_targets)
            value["members"][key] = raw
        return value


class DatasetCenter:
    """JSON-backed store; writes are atomic and scoped to one canonical root."""
    def __init__(self, root: Path):
        self.path = Path(root) / "datasets" / "managed.json"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = RLock()
        self._items: dict[str, ManagedDataset] = {}
        self._requests: dict[tuple[str, str], dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._items = {}
            self._requests = {}
            return
        try:
            payload = json.loads(self.path.read_text())
            items = {}
            for raw in payload.get("datasets", []):
                raw = dict(raw)
                members = {}
                for key, value in raw.pop("members", {}).items():
                    value.pop("effective_history_start", None)
                    value.pop("effective_derived_targets", None)
                    if value.get("derived_targets") is not None:
                        value["derived_targets"] = tuple(value["derived_targets"])
                    members[key] = DatasetMember(**value)
                raw["derived_targets"] = tuple(raw.get("derived_targets", ()))
                items[raw["dataset_id"]] = ManagedDataset(**raw, members=members)
            requests: dict[tuple[str, str], dict] = {}
            stored_requests = payload.get("requests", [])
            if isinstance(stored_requests, dict):
                # Migrate the original flat string-keyed representation.  The
                # value carries the authoritative dataset identity; stripping
                # only that exact prefix also preserves legacy raw keys.
                request_values = []
                for stored_key, raw_value in stored_requests.items():
                    value = dict(raw_value)
                    dataset_id = value["dataset_id"]
                    prefix = f"{dataset_id}:"
                    value.setdefault(
                        "idempotency_key",
                        stored_key.removeprefix(prefix),
                    )
                    request_values.append(value)
            elif isinstance(stored_requests, list):
                request_values = stored_requests
            else:
                raise TypeError("managed dataset requests must be a list or object")
            for raw_value in request_values:
                value = dict(raw_value)
                request_key = (value["dataset_id"], value["idempotency_key"])
                if request_key in requests:
                    raise ValueError("duplicate managed dataset request identity")
                requests[request_key] = value
        except (AttributeError, OSError, ValueError, TypeError, KeyError) as exc:
            raise DatasetStateError(f"managed dataset state is unreadable: {self.path}") from exc
        self._items = items
        self._requests = requests

    @contextmanager
    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "datasets": [d.as_dict() for d in self._items.values()],
            "requests": list(self._requests.values()),
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2))
        temporary.replace(self.path)

    def create(self, *, dataset_id: str, name: str, notes: str = "", history_start: str | None = None) -> ManagedDataset:
        with self._lock, self._file_lock():
            self._load()
            if dataset_id in self._items:
                raise ValueError("dataset already exists")
            managed_dataset_root(self.path.parent.parent, dataset_id)
            item = ManagedDataset(dataset_id=dataset_id, name=name, notes=notes, history_start=history_start)
            self._items[dataset_id] = item
            dataset_root = managed_dataset_root(self.path.parent.parent, dataset_id).parent
            dataset_root.mkdir(parents=True, exist_ok=True)
            managed_dataset_root(self.path.parent.parent, dataset_id).mkdir(parents=True, exist_ok=True)
            (dataset_root / "OWNERSHIP.json").write_text(json.dumps({"dataset_id": dataset_id, "provider": item.provider, "asset_class": item.asset_class, "base_timeframe": item.base_timeframe, "price_type": item.price_type}, indent=2))
            self._save()
            return item

    def get(self, dataset_id: str) -> ManagedDataset:
        with self._lock, self._file_lock():
            self._load()
            try: return self._items[dataset_id]
            except KeyError as exc: raise KeyError(dataset_id) from exc

    def list(self) -> list[ManagedDataset]:
        with self._lock, self._file_lock():
            self._load()
            return list(self._items.values())

    @contextmanager
    def locked_dataset(self, dataset_id: str):
        """Hold the control-plane lock across a dataset publication boundary."""
        with self._lock, self._file_lock():
            self._load()
            try:
                item = self._items[dataset_id]
            except KeyError as exc:
                raise KeyError(dataset_id) from exc
            yield item

    def update(self, dataset_id: str, *, expected_version: int, **changes) -> ManagedDataset:
        with self._lock, self._file_lock():
            self._load()
            try: item = self._items[dataset_id]
            except KeyError as exc: raise KeyError(dataset_id) from exc
            if item.version != expected_version:
                raise ValueError(f"dataset version conflict: expected {expected_version}, current {item.version}")
            unknown = set(changes) - {"name", "notes", "history_start", "status",
                                      "provider", "asset_class", "price_type",
                                      "base_timeframe", "derived_targets"}
            if unknown:
                raise ValueError("unknown dataset fields: " + ", ".join(sorted(unknown)))
            immutable = {key for key in ("provider", "asset_class", "price_type", "base_timeframe", "derived_targets") if key in changes}
            if immutable:
                raise ValueError("dataset definition is immutable: " + ", ".join(sorted(immutable)))
            if "status" in changes and changes["status"] not in {"active", "paused", "archived"}:
                raise ValueError("status must be active, paused or archived")
            applied = False
            for key in ("name", "notes", "history_start", "status"):
                if key in changes and changes[key] is not None and getattr(item, key) != changes[key]:
                    setattr(item, key, changes[key]); applied = True
            if not applied:
                raise ValueError("dataset update does not change configuration")
            item.version += 1
            self._save(); return item

    def add_member(self, dataset_id: str, member: DatasetMember, *, expected_version: int) -> ManagedDataset:
        with self._lock, self._file_lock():
            self._load()
            try: item = self._items[dataset_id]
            except KeyError as exc: raise KeyError(dataset_id) from exc
            if item.version != expected_version:
                raise ValueError(f"dataset version conflict: expected {expected_version}, current {item.version}")
            if item.provider != "dukascopy" or item.asset_class != "fx" or item.price_type != "bid" or item.base_timeframe != "1m":
                raise ValueError("dataset definition is immutable")
            symbol = member.symbol.upper()
            if symbol != "EURUSD":
                raise ValueError("P2.1 only supports EURUSD")
            if member.derived_targets is not None and any(target != "5m" for target in member.derived_targets):
                raise ValueError("P2.1 only supports 5m derived target")
            if symbol in item.members:
                raise ValueError("member already exists")
            item.members[symbol] = DatasetMember(
                symbol=symbol, history_start=member.history_start,
                derived_targets=member.derived_targets, status=member.status)
            item.version += 1; self._save(); return item

    def submit_request(self, dataset_id: str, *, symbol: str, start: str, end: str,
                       idempotency_key: str | None = None) -> tuple[dict, bool]:
        with self._lock, self._file_lock():
            self._load()
            try: item = self._items[dataset_id]
            except KeyError as exc: raise KeyError(dataset_id) from exc
            if item.status == "archived": raise ValueError("archived dataset is read-only")
            symbol = symbol.upper()
            if symbol not in item.members: raise ValueError("member is not registered")
            try:
                start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("maintenance range must use ISO-8601 timestamps") from exc
            if start_at.tzinfo is None or end_at.tzinfo is None or start_at >= end_at:
                raise ValueError("maintenance range must be timezone-aware and non-empty")
            raw_key = idempotency_key or str(uuid4())
            key = (dataset_id, raw_key)
            if key in self._requests:
                existing = self._requests[key]
                if (existing["symbol"], existing["start"], existing["end"]) != (symbol, start, end):
                    raise ValueError("idempotency key was already used for another request")
                return existing, False
            value = {"request_id": str(uuid4()), "idempotency_key": raw_key,
                     "action_key_version": 2, "dataset_id": dataset_id, "symbol": symbol,
                     "start": start, "end": end, "status": "queued",
                     "created_at": datetime.now(timezone.utc).isoformat()}
            self._requests[key] = value; self._save(); return value, True

    def request(self, dataset_id: str, *, symbol: str, start: str, end: str,
                idempotency_key: str | None = None) -> dict:
        return self.submit_request(
            dataset_id, symbol=symbol, start=start, end=end,
            idempotency_key=idempotency_key)[0]

    def discard_unattached_request(self, dataset_id: str, idempotency_key: str,
                                   request_id: str) -> None:
        """Compensate a control-plane refusal without deleting an accepted replay."""
        with self._lock, self._file_lock():
            self._load()
            key = (dataset_id, idempotency_key)
            value = self._requests.get(key)
            if value and value.get("request_id") == request_id and not value.get("execution"):
                del self._requests[key]
                self._save()

    def requests(self, dataset_id: str) -> list[dict]:
        with self._lock, self._file_lock():
            self._load()
            return [r for r in self._requests.values() if r["dataset_id"] == dataset_id]

    def attach_execution(self, dataset_id: str, idempotency_key: str, execution: dict) -> dict:
        with self._lock, self._file_lock():
            self._load()
            value = self._requests.get((dataset_id, idempotency_key))
            if value is None:
                raise KeyError(idempotency_key)
            if value.get("dataset_id") != dataset_id:
                raise DatasetStateError("managed request dataset identity mismatch")
            value["execution"] = execution
            value["status"] = execution.get("status", value["status"])
            self._save()
            return value
