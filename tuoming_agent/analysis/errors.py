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


SECURITY_BLOCKED_MESSAGE = "安全策略拒绝了该计划；未执行数据操作，也未向模型发送失败详情。"
QUALITY_FAILED_MESSAGE = "本地结果未通过质量校验；系统仅使用安全错误码生成修订计划。"
EXECUTION_FAILED_MESSAGE = "本地执行未通过；系统仅使用安全错误码生成修订计划。"
RESOURCE_LIMIT_MESSAGE = "本地内存或临时磁盘达到安全上限，请减小数据量或简化分析步骤后重试。"
TERMINAL_FAILED_MESSAGE = "本地分析执行失败，详细异常未保存、未显示，也未发送给模型。"
REPAIR_FAILED_MESSAGE = "修订计划生成失败；详细异常未保存、未显示，也未发送给模型。"
VALIDATION_FAILED_MESSAGE = "计划未通过本地安全校验，请修改要求后重试。"


