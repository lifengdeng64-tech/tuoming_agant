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
    return tuple(
        f"{diagnostic_path}: {name}"
        for diagnostic_path, _feedback_path, name in _generated_name_candidates(plan)
        if not _is_valid_generated_name(name)
    )


def generated_name_issue_paths(plan: AnalysisPlan) -> tuple[str, ...]:
    return tuple(
        feedback_path
        for _diagnostic_path, feedback_path, name in _generated_name_candidates(plan)
        if not _is_valid_generated_name(name)
    )


def generated_names(plan: AnalysisPlan) -> tuple[str, ...]:
    return tuple(
        name for _diagnostic_path, _feedback_path, name in _generated_name_candidates(plan)
    )


def _generated_name_candidates(plan: AnalysisPlan) -> list[tuple[str, str, str]]:
    candidates = [("result_name", "result_name", plan.result_name)]
    for operation_index, operation in enumerate(plan.operations):
        operation_path = f"operations[{operation_index}]"
        if isinstance(operation, GroupByOperation):
            candidates.extend(
                (
                    f"{operation_path}.aggregations[{aggregation_index}].output",
                    f"{operation_path}.aggregations[{aggregation_index}].output",
                    aggregation.output,
                )
                for aggregation_index, aggregation in enumerate(operation.aggregations)
            )
        elif isinstance(operation, DeriveOperation):
            candidates.append(
                (f"{operation_path}.column", f"{operation_path}.column", operation.column)
            )
        elif isinstance(operation, RenameOperation):
            candidates.extend(
                (
                    f"{operation_path}.mapping[{source}]",
                    f"{operation_path}.mapping.targets[{target_index}]",
                    target,
                )
                for target_index, (source, target) in enumerate(operation.mapping.items())
            )
        elif isinstance(operation, MergeOperation):
            candidates.extend(
                (
                    f"{operation_path}.suffixes[{suffix_index}]",
                    f"{operation_path}.suffixes[{suffix_index}]",
                    suffix,
                )
                for suffix_index, suffix in enumerate(operation.suffixes)
            )

    return candidates


def _is_valid_generated_name(name: str) -> bool:
    return CJK_PATTERN.search(name) is not None and ASCII_LETTER_PATTERN.search(name) is None
