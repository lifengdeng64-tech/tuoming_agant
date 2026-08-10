from __future__ import annotations

import ast
import operator
from typing import Any

import pandas as pd


class UnsafeExpressionError(ValueError):
    """Raised when a derived-column expression leaves the arithmetic allowlist."""


BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def evaluate_expression(dataframe: pd.DataFrame, expression: str) -> Any:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError("Derived expression is invalid.") from exc
    return _evaluate_node(dataframe, tree.body)


def _evaluate_node(dataframe: pd.DataFrame, node: ast.AST) -> Any:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.Name):
        return _column(dataframe, node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in BINARY_OPERATORS:
        left = _evaluate_node(dataframe, node.left)
        right = _evaluate_node(dataframe, node.right)
        return BINARY_OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY_OPERATORS:
        return UNARY_OPERATORS[type(node.op)](_evaluate_node(dataframe, node.operand))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "col"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return _column(dataframe, node.args[0].value)
    raise UnsafeExpressionError(
        "Only numeric constants, arithmetic operators and col('column') are allowed."
    )


def _column(dataframe: pd.DataFrame, name: str) -> pd.Series:
    if name not in dataframe.columns:
        raise ValueError(f"Column does not exist: {name}")
    return dataframe[name]
