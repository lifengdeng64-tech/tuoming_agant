from __future__ import annotations

import ast
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

AGGREGATIONS = {
    "sum": "求和",
    "mean": "平均值",
    "median": "中位数",
    "min": "最小值",
    "max": "最大值",
    "count": "计数",
    "nunique": "去重计数",
    "first": "首个值",
    "last": "末个值",
}

_BINARY_OPERATORS: dict[type[ast.operator], tuple[str, int]] = {
    ast.Add: ("＋", 1),
    ast.Sub: ("−", 1),
    ast.Mult: ("×", 2),
    ast.Div: ("÷", 2),
}


def _display(value: Any, resolve_value: Callable[[Any], Any] | None) -> Any:
    return resolve_value(value) if resolve_value else value


def _name(value: Any, resolve_value: Callable[[Any], Any] | None) -> str:
    return str(_display(value, resolve_value))


def _quoted(values: list[str], resolve_value: Callable[[Any], Any] | None = None) -> str:
    return "、".join(f"“{_name(value, resolve_value)}”" for value in values)


def _expression_node(
    node: ast.expr,
    resolve_value: Callable[[Any], Any] | None,
    parent_precedence: int = 0,
    *,
    right_child: bool = False,
) -> tuple[str, bool, int]:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if (
            node.func.id == "col"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return f"“{_name(node.args[0].value, resolve_value)}”", False, 4
        if node.func.id == "safe_divide" and len(node.args) == 2:
            left, _, _ = _expression_node(node.args[0], resolve_value, 2)
            right, _, _ = _expression_node(
                node.args[1], resolve_value, 2, right_child=True
            )
            text = f"{left} ÷ {right}"
            if parent_precedence > 2:
                text = f"（{text}）"
            return text, True, 2
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        symbol, precedence = _BINARY_OPERATORS[type(node.op)]
        left, left_guarded, _ = _expression_node(node.left, resolve_value, precedence)
        right, right_guarded, right_precedence = _expression_node(
            node.right,
            resolve_value,
            precedence,
            right_child=True,
        )
        if right_precedence == precedence and isinstance(node.op, (ast.Sub, ast.Div)):
            right = f"（{right}）"
        text = f"{left} {symbol} {right}"
        if precedence < parent_precedence or (right_child and precedence == parent_precedence):
            text = f"（{text}）"
        return text, left_guarded or right_guarded, precedence
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value, guarded, _ = _expression_node(node.operand, resolve_value, 3)
        return ("−" if isinstance(node.op, ast.USub) else "＋") + value, guarded, 3
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return str(node.value), False, 4
    raise ValueError("unsupported display expression")


def _describe_expression(
    expression: str, resolve_value: Callable[[Any], Any] | None = None
) -> str:
    try:
        root = ast.parse(expression, mode="eval").body
        rendered, guarded, _ = _expression_node(root, resolve_value)
    except (SyntaxError, ValueError):
        return "按已确认的安全计算规则生成"
    suffix = "（分母为 0 时结果留空）" if guarded else ""
    return rendered + suffix


def _describe(
    operation: Any,
    resolve_value: Callable[[Any], Any] | None = None,
    resolve_artifact: Callable[[str], str] | None = None,
) -> str:
    action = operation.action
    if action == "select":
        return f"保留列：{_quoted(operation.columns, resolve_value)}"
    if action == "filter":
        value = _display(operation.value, resolve_value)
        suffix = "" if operation.operator in {"notnull", "isnull"} else f" {value!r}"
        column = _name(operation.column, resolve_value)
        return f"筛选“{column}”：{OPERATORS[operation.operator]}{suffix}"
    if action == "sort":
        direction = "升序" if operation.ascending is True else "降序"
        return f"按{_quoted(operation.columns, resolve_value)}{direction}排序"
    if action == "rename":
        return "重命名：" + "、".join(
            f"“{_name(a, resolve_value)}”→“{_name(b, resolve_value)}”"
            for a, b in operation.mapping.items()
        )
    if action == "cast":
        return "转换类型：" + "、".join(
            f"“{_name(a, resolve_value)}”→{b}" for a, b in operation.mapping.items()
        )
    if action == "fillna":
        values = {
            _name(column, resolve_value): _display(value, resolve_value)
            for column, value in operation.values.items()
        }
        return "填充空值：" + "、".join(
            f"“{column}”填充为 {value!r}" for column, value in values.items()
        )
    if action == "dropna":
        return f"删除空值行：{_quoted(operation.subset or [], resolve_value) or '全部列'}"
    if action == "deduplicate":
        return f"去重：{_quoted(operation.subset or [], resolve_value) or '整行'}"
    if action == "merge":
        right_name = (
            resolve_artifact(operation.right_artifact_id)
            if resolve_artifact
            else operation.right_artifact_id
        )
        return (
            f"与“{right_name}”合并，左表连接键：{_quoted(operation.left_on, resolve_value)}，"
            f"右表连接键：{_quoted(operation.right_on, resolve_value)}"
        )
    if action == "groupby":
        calculations = "、".join(
            f"“{_name(item.column, resolve_value)}”{AGGREGATIONS[item.function]}"
            f"并生成“{_name(item.output, resolve_value)}”"
            for item in operation.aggregations
        )
        return f"按{_quoted(operation.by, resolve_value)}分组，{calculations}"
    if action == "derive":
        return (
            f"计算新列“{_name(operation.column, resolve_value)}”："
            f"{_describe_expression(operation.expression, resolve_value)}"
        )
    if action == "head":
        return f"保留前 {operation.rows} 行"
    if action == "tail":
        return f"保留后 {operation.rows} 行"
    return f"执行白名单操作：{action}"


def describe_plan(
    plan: AnalysisPlan,
    resolve_value: Callable[[Any], Any] | None = None,
    resolve_artifact: Callable[[str], str] | None = None,
) -> list[str]:
    source_name = (
        resolve_artifact(plan.input_artifact_id) if resolve_artifact else plan.input_artifact_id
    )
    lines = [
        f"数据源：{source_name}",
        f"结果名称：{_name(plan.result_name, resolve_value)}",
        *[
            f"步骤 {index}：{_describe(operation, resolve_value, resolve_artifact)}"
            for index, operation in enumerate(plan.operations, 1)
        ],
    ]
    if plan.dashboard is not None:
        measures = _quoted(plan.dashboard.measures, resolve_value)
        dimensions = [
            value
            for value in (plan.dashboard.date_column, plan.dashboard.category_column)
            if value
        ]
        suffix = f"，维度：{_quoted(dimensions, resolve_value)}" if dimensions else ""
        lines.append(
            f"本地生成 BI 仪表盘：指标 {measures}，"
            f"聚合方式：{AGGREGATIONS[plan.dashboard.aggregation]}{suffix}"
        )
    return lines
