from __future__ import annotations

import re

from tuoming_agent.analysis.models import (
    AnalysisPlan,
    DeriveOperation,
    GroupByOperation,
    MergeOperation,
    RenameOperation,
)

CJK_PATTERN = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
ASCII_LETTER_PATTERN = re.compile(r"[A-Za-z]")

GENERATED_NAME_RULE = (
    "生成的结果名称和新字段名称必须至少包含一个中文字符，"
    "且不得包含 ASCII 英文字母。"
)


class GeneratedNameValidationError(ValueError):
    """Raised when generated analysis names do not satisfy the naming rule."""


def generated_name_issues(plan: AnalysisPlan) -> tuple[str, ...]:
    candidates = [("result_name", plan.result_name)]
    for operation_index, operation in enumerate(plan.operations):
        operation_path = f"operations[{operation_index}]"
        if isinstance(operation, GroupByOperation):
            candidates.extend(
                (
                    f"{operation_path}.aggregations[{aggregation_index}].output",
                    aggregation.output,
                )
                for aggregation_index, aggregation in enumerate(operation.aggregations)
            )
        elif isinstance(operation, DeriveOperation):
            candidates.append((f"{operation_path}.column", operation.column))
        elif isinstance(operation, RenameOperation):
            candidates.extend(
                (f"{operation_path}.mapping[{source}]", target)
                for source, target in operation.mapping.items()
            )
        elif isinstance(operation, MergeOperation):
            candidates.extend(
                (f"{operation_path}.suffixes[{suffix_index}]", suffix)
                for suffix_index, suffix in enumerate(operation.suffixes)
            )

    return tuple(
        f"{path}: {name}" for path, name in candidates if not _is_valid_generated_name(name)
    )


def _is_valid_generated_name(name: str) -> bool:
    return CJK_PATTERN.search(name) is not None and ASCII_LETTER_PATTERN.search(name) is None
