class AnalysisWorkflowError(RuntimeError):
    """Base error for the persisted analysis workflow."""


class SecurityPolicyViolation(AnalysisWorkflowError):
    """A security boundary was crossed; this error must never be auto-repaired."""


class RepairableAnalysisError(AnalysisWorkflowError):
    """A business or deterministic quality error that may be replanned."""


class TerminalAnalysisError(AnalysisWorkflowError):
    """An error that cannot make useful progress through replanning."""

