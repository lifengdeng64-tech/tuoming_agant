from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tuoming_agent.analysis.models import AnalysisPlan

OPERATORS = {
    "eq": "等于",
    "ne": "不等于",
    "gt": "大于",
    "gte": "大于等于",
    "lt": "小于",
    "lte": "小于等于",
    "contains": "包含",
    "in": "属于",
    "notnull": "非空",
    "isnull": "为空",
}


def _quoted(values: list[str]) -> str:
    return "、".join(f"“{value}”" for value in values)


def _describe(operation: Any, resolve_value: Callable[[Any], Any] | None = None) -> str:
    action = operation.action
    if action == "select":
        return f"保留列：{_quoted(operation.columns)}"
    if action == "filter":
        value = resolve_value(operation.value) if resolve_value else operation.value
        suffix = "" if operation.operator in {"notnull", "isnull"} else f" {value!r}"
        return f"筛选“{operation.column}”：{OPERATORS[operation.operator]}{suffix}"
    if action == "sort":
        direction = "升序" if operation.ascending is True else "降序"
        return f"按{_quoted(operation.columns)}{direction}排序"
    if action == "rename":
        return "重命名：" + "、".join(f"“{a}”→“{b}”" for a, b in operation.mapping.items())
    if action == "cast":
        return "转换类型：" + "、".join(f"“{a}”→{b}" for a, b in operation.mapping.items())
    if action == "fillna":
        values = {
            column: resolve_value(value) if resolve_value else value
            for column, value in operation.values.items()
        }
        return "填充空值：" + "、".join(
            f"“{column}”填充为 {value!r}" for column, value in values.items()
        )
    if action == "dropna":
        return f"删除空值行：{_quoted(operation.subset or []) or '全部列'}"
    if action == "deduplicate":
        return f"去重：{_quoted(operation.subset or []) or '整行'}"
    if action == "merge":
        return f"与制品 {operation.right_artifact_id} 合并，连接键：{_quoted(operation.left_on)}"
    if action == "groupby":
        outputs = _quoted([item.output for item in operation.aggregations])
        return f"按{_quoted(operation.by)}分组，生成 {outputs}"
    if action == "derive":
        return f"计算新列“{operation.column}”：{operation.expression}"
    if action == "head":
        return f"保留前 {operation.rows} 行"
    if action == "tail":
        return f"保留后 {operation.rows} 行"
    return f"执行白名单操作：{action}"


def describe_plan(
    plan: AnalysisPlan, resolve_value: Callable[[Any], Any] | None = None
) -> list[str]:
    return [
        f"数据源：{plan.input_artifact_id}",
        f"结果名称：{plan.result_name}",
        *[
            f"步骤 {index}：{_describe(operation, resolve_value)}"
            for index, operation in enumerate(plan.operations, 1)
        ],
    ]
