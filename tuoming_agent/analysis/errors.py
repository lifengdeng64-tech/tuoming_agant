class AnalysisWorkflowError(RuntimeError):
    """Base error for the persisted analysis workflow."""


class AnalysisServiceError(AnalysisWorkflowError):
    """Safe, user-facing failure raised while asking a model to create a plan."""

    def __init__(self, message: str, error_code: str):
        super().__init__(message)
        self.public_message = message
        self.error_code = error_code


class AnalysisProviderError(AnalysisServiceError):
    """The configured model provider could not complete the planning request."""


class AnalysisPlanValidationError(AnalysisServiceError):
    """The model replied, but its structured plan did not match the allowlist schema."""


class SecurityPolicyViolation(AnalysisWorkflowError):
    """A security boundary was crossed; this error must never be auto-repaired."""


class RepairableAnalysisError(AnalysisWorkflowError):
    """A business or deterministic quality error that may be replanned."""


class TerminalAnalysisError(AnalysisWorkflowError):
    """An error that cannot make useful progress through replanning."""

