"""Small, durable control-plane for dataset-centred EURUSD maintenance."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4


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

    def as_dict(self) -> dict:
        value = asdict(self)
        value["derived_targets"] = list(self.derived_targets)
        value["members"] = {}
        for key, member in self.members.items():
            raw = asdict(member)
            raw["derived_targets"] = list(member.derived_targets) if member.derived_targets is not None else None
            raw["effective_history_start"] = member.history_start or self.history_start
            raw["effective_derived_targets"] = list(member.derived_targets or self.derived_targets)
            value["members"][key] = raw
        return value


class DatasetCenter:
    """JSON-backed store; writes are atomic and scoped to one canonical root."""
    def __init__(self, root: Path):
        self.path = Path(root) / "datasets" / "managed.json"
        self._lock = RLock()
        self._items: dict[str, ManagedDataset] = {}
        self._requests: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text())
            for raw in payload.get("datasets", []):
                members = {}
                for key, value in raw.pop("members", {}).items():
                    value.pop("effective_history_start", None)
                    value.pop("effective_derived_targets", None)
                    if value.get("derived_targets") is not None:
                        value["derived_targets"] = tuple(value["derived_targets"])
                    members[key] = DatasetMember(**value)
                raw["derived_targets"] = tuple(raw.get("derived_targets", ()))
                self._items[raw["dataset_id"]] = ManagedDataset(**raw, members=members)
            self._requests = {}
            for key, value in payload.get("requests", {}).items():
                normalized_key = key if key.startswith(f"{value.get('dataset_id')}:") else f"{value.get('dataset_id')}:{key}"
                self._requests[normalized_key] = value
        except (OSError, ValueError, TypeError):
            return

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"datasets": [d.as_dict() for d in self._items.values()], "requests": self._requests}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2))
        temporary.replace(self.path)

    def create(self, *, dataset_id: str, name: str, notes: str = "", history_start: str | None = None) -> ManagedDataset:
        with self._lock:
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
        try: return self._items[dataset_id]
        except KeyError as exc: raise KeyError(dataset_id) from exc

    def list(self) -> list[ManagedDataset]: return list(self._items.values())

    def update(self, dataset_id: str, **changes) -> ManagedDataset:
        with self._lock:
            item = self.get(dataset_id)
            immutable = {key for key in ("provider", "asset_class", "price_type", "base_timeframe", "derived_targets") if key in changes}
            if immutable:
                raise ValueError("dataset definition is immutable: " + ", ".join(sorted(immutable)))
            if "status" in changes and changes["status"] not in {"active", "paused", "archived"}:
                raise ValueError("status must be active, paused or archived")
            for key in ("name", "notes", "history_start", "status"):
                if key in changes and changes[key] is not None: setattr(item, key, changes[key])
            item.version += 1
            self._save(); return item

    def add_member(self, dataset_id: str, member: DatasetMember) -> ManagedDataset:
        with self._lock:
            item = self.get(dataset_id)
            if item.provider != "dukascopy" or item.asset_class != "fx" or item.price_type != "bid" or item.base_timeframe != "1m":
                raise ValueError("dataset definition is immutable")
            if member.symbol.upper() != "EURUSD":
                raise ValueError("P2.1 only supports EURUSD")
            if member.derived_targets is not None and any(target != "5m" for target in member.derived_targets):
                raise ValueError("P2.1 only supports 5m derived target")
            item.members[member.symbol] = member; item.version += 1; self._save(); return item

    def request(self, dataset_id: str, *, symbol: str, start: str, end: str, idempotency_key: str | None = None) -> dict:
        with self._lock:
            item = self.get(dataset_id)
            if item.status == "archived": raise ValueError("archived dataset is read-only")
            if symbol not in item.members: raise ValueError("member is not registered")
            try:
                start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("maintenance range must use ISO-8601 timestamps") from exc
            if start_at.tzinfo is None or end_at.tzinfo is None or start_at >= end_at:
                raise ValueError("maintenance range must be timezone-aware and non-empty")
            raw_key = idempotency_key or str(uuid4())
            key = f"{dataset_id}:{raw_key}"
            if key in self._requests:
                existing = self._requests[key]
                if (existing["symbol"], existing["start"], existing["end"]) != (symbol, start, end):
                    raise ValueError("idempotency key was already used for another request")
                return existing
            value = {"request_id": str(uuid4()), "idempotency_key": raw_key, "dataset_id": dataset_id, "symbol": symbol, "start": start, "end": end, "status": "queued", "created_at": datetime.now(timezone.utc).isoformat()}
            self._requests[key] = value; self._save(); return value

    def requests(self, dataset_id: str) -> list[dict]:
        return [r for r in self._requests.values() if r["dataset_id"] == dataset_id]

    def attach_execution(self, dataset_id: str, idempotency_key: str, execution: dict) -> dict:
        with self._lock:
            value = self._requests.get(f"{dataset_id}:{idempotency_key}")
            if value is None:
                raise KeyError(idempotency_key)
            value["execution"] = execution
            value["status"] = execution.get("status", value["status"])
            self._save()
            return value
