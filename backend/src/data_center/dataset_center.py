"""Small, durable control-plane for dataset-centred EURUSD maintenance."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4


@dataclass
class DatasetMember:
    symbol: str
    history_start: str | None = None
    derived_targets: tuple[str, ...] = ("5m",)
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
    status: str = "active"
    version: int = 1
    members: dict[str, DatasetMember] = field(default_factory=dict)

    def as_dict(self) -> dict:
        value = asdict(self)
        value["derived_targets"] = list(self.derived_targets)
        value["members"] = {k: asdict(v) for k, v in self.members.items()}
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
                members = {k: DatasetMember(**v) for k, v in raw.pop("members", {}).items()}
                raw["derived_targets"] = tuple(raw.get("derived_targets", ()))
                self._items[raw["dataset_id"]] = ManagedDataset(**raw, members=members)
            self._requests = payload.get("requests", {})
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
            item = ManagedDataset(dataset_id=dataset_id, name=name, notes=notes, history_start=history_start)
            self._items[dataset_id] = item
            dataset_root = self.path.parent / dataset_id
            dataset_root.mkdir(parents=True, exist_ok=True)
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
            if any(target != "5m" for target in member.derived_targets):
                raise ValueError("P2.1 only supports 5m derived target")
            item.members[member.symbol] = member; item.version += 1; self._save(); return item

    def request(self, dataset_id: str, *, symbol: str, start: str, end: str, idempotency_key: str | None = None) -> dict:
        with self._lock:
            item = self.get(dataset_id)
            if item.status == "archived": raise ValueError("archived dataset is read-only")
            if symbol not in item.members: raise ValueError("member is not registered")
            key = idempotency_key or str(uuid4())
            if key in self._requests: return self._requests[key]
            value = {"request_id": str(uuid4()), "dataset_id": dataset_id, "symbol": symbol, "start": start, "end": end, "status": "queued", "created_at": datetime.now(timezone.utc).isoformat()}
            self._requests[key] = value; self._save(); return value

    def requests(self, dataset_id: str) -> list[dict]:
        return [r for r in self._requests.values() if r["dataset_id"] == dataset_id]

    def attach_execution(self, idempotency_key: str, execution: dict) -> dict:
        with self._lock:
            value = self._requests.get(idempotency_key)
            if value is None:
                raise KeyError(idempotency_key)
            value["execution"] = execution
            value["status"] = execution.get("status", value["status"])
            self._save()
            return value
