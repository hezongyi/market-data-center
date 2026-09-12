"""Bounded lineage summaries shared by raw and derived run receipts."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable


def compact_source_hashes(values: Iterable[str]) -> dict[str, int | str | None]:
    """Summarize source hashes without serializing an unbounded hash array.

    The digest is over the sorted unique hashes, making it stable across part
    ordering and duplicate observations.  First/last values aid diagnostics;
    callers that need the complete set can recover it from the immutable input
    parts rather than inflating every receipt.
    """
    hashes = sorted({str(value) for value in values})
    encoded = json.dumps(hashes, separators=(",", ":"), ensure_ascii=True).encode()
    summary: dict[str, int | str | None] = {
        "source_hash_count": len(hashes),
        "source_hash_digest": hashlib.sha256(encoded).hexdigest(),
        "first_source_hash": hashes[0] if hashes else None,
        "last_source_hash": hashes[-1] if hashes else None,
    }
    return summary
