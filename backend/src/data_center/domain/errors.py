"""Domain error types shared by connectors, ingest, and maintenance runners."""
from __future__ import annotations


class ProviderGapError(ValueError):
    """The provider holds no data for the requested range or window.

    Sparse historical data and session breaks make this an expected condition:
    the platform never synthesizes bars to fill it, so callers classify it as
    ``degraded`` rather than as a failure.  It subclasses ``ValueError`` so
    callers that already catch ``ValueError`` keep working.
    """


class SessionClosedError(ProviderGapError):
    """The requested window lies entirely outside the instrument's session.

    A weekend window for an FX instrument owes no bars at all, so this is an
    expected and deterministic condition: retrying the same window cannot
    produce output.  It subclasses ``ProviderGapError`` so every caller that
    already treats a gap as ``degraded`` keeps working, while a caller that can
    tell the two apart reports it as ``skipped`` instead.
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
# workers keep their established meaning.  A closed window belongs to the same
# degradation class as a gap: both say "no bars for this window", and neither
# is ever filled with synthesized ones.
PROVIDER_GAP_ERROR_TYPES = frozenset({ProviderGapError.__name__, SessionClosedError.__name__})

#: A subset of the gap class that owes no output at all, so retrying the same
#: window is pointless and the condition is reported as skipped rather than
#: degraded.
SESSION_CLOSED_ERROR_TYPES = frozenset({SessionClosedError.__name__})

INPUT_UNAVAILABLE_ERROR_TYPES = frozenset({InputUnavailableError.__name__})
