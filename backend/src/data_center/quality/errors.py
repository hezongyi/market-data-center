class QualityError(ValueError):
    """Structured validation failure safe to persist in a run receipt."""

    def __init__(self, message: str, findings: list[dict]):
        super().__init__(message)
        self.findings = findings
