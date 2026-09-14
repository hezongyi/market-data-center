"""Domain error types shared by connectors, ingest, and maintenance runners."""
from __future__ import annotations


class ProviderGapError(ValueError):
    """The provider holds no data for the requested range or window.

    Sparse historical data and session breaks make this an expected condition:
    the platform never synthesizes bars to fill it, so callers classify it as
    ``degraded`` rather than as a failure.  It subclasses ``ValueError`` so
    callers that already catch ``ValueError`` keep working.
    """


class InputUnavailableError(ValueError):
    """A step's fixed input cannot be rebuilt from its persisted references.

    The execution was accepted against a specific set of canonical parts.  If
    one of them is missing or fails verification the step stops and reports
    ``input_unavailable``; it must never fall back to the current catalog and
    silently compute against different data (spec 6.3).
    """


# Run receipts record the error type by name.  ``ValueError`` predates the
# dedicated type and is still accepted so that receipts written by earlier
# workers keep their established meaning.
PROVIDER_GAP_ERROR_TYPES = frozenset({ProviderGapError.__name__})
INPUT_UNAVAILABLE_ERROR_TYPES = frozenset({InputUnavailableError.__name__})
