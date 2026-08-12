from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tuoming_agent.analysis.errors import SecurityPolicyViolation
from tuoming_agent.analysis.models import (
    AnalysisPlan,
    CastOperation,
    DeduplicateOperation,
    DeriveOperation,
    DropnaOperation,
    FillnaOperation,
    FilterOperation,
    GroupByOperation,
    HeadOperation,
    MergeOperation,
    RenameOperation,
    SelectOperation,
    SortOperation,
    TailOperation,
)
from tuoming_agent.models import ArtifactRecord, ColumnLineage
from tuoming_agent.storage.base import Repository

MIB = 1024 * 1024
PRIMARY_ARTIFACT_LIMIT = 200 * MIB
SECONDARY_ARTIFACT_LIMIT = 50 * MIB


@dataclass(frozen=True)
class _SourceAuthorization:
    artifact_id: str
    relation_name: str
    path: Path


@dataclass(frozen=True)
class AuthorizedSource:
    artifact_id: str
    relation_name: str
    path: Path
    _authorization: _SourceAuthorization = field(repr=False, compare=False)


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: tuple[Any, ...]
    sources: tuple[AuthorizedSource, ...]
    lineage: dict[str, ColumnLineage]


@dataclass
class _QueryState:
    sql: str
    columns: list[str]
    lineage: dict[str, ColumnLineage]
    order_column: str


class DuckDBCompiler:
    """Compile the structured analysis allowlist without accepting SQL or paths."""

    def __init__(self, repository: Repository):
        self.repository = repository
        self._parameters: list[Any] = []
        self._sources: list[AuthorizedSource] = []

    def compile(self, tenant_id: str, workspace_id: str, plan: AnalysisPlan) -> CompiledQuery:
        self._parameters = []
        self._sources = []
        primary = self._resolve_source(
            tenant_id,
            workspace_id,
            plan.input_artifact_id,
            limit=PRIMARY_ARTIFACT_LIMIT,
            role="primary",
        )
        columns = self._schema_columns(primary)
        order_column = self._internal_order_column(columns)
        source = self._authorize_source(primary)
        state = _QueryState(
            sql=(
                f"SELECT {self._column_list(columns)}, "
                f"row_number() OVER () AS {self._quote(order_column)} "
                f"FROM {self._quote(source.relation_name)}"
            ),
            columns=columns,
            lineage=dict(primary.lineage),
            order_column=order_column,
        )

        for operation in plan.operations:
            state = self._apply(tenant_id, workspace_id, state, operation)

        final_sql = (
            f"SELECT {self._column_list(state.columns)} FROM ({state.sql}) AS "
            f"{self._quote('_final')} ORDER BY {self._quote(state.order_column)}"
        )
        return CompiledQuery(
            sql=final_sql,
            parameters=tuple(self._parameters),
            sources=tuple(self._sources),
            lineage=state.lineage,
        )

    def _apply(
        self, tenant_id: str, workspace_id: str, state: _QueryState, operation: Any
    ) -> _QueryState:
        if isinstance(operation, SelectOperation):
            self._require_columns(state.columns, operation.columns)
            columns = list(operation.columns)
            return self._state(
                state,
                self._select_stage(state, columns),
                columns,
                {name: state.lineage[name] for name in columns if name in state.lineage},
            )
        if isinstance(operation, FilterOperation):
            return self._filter(state, operation)
        if isinstance(operation, SortOperation):
            return self._sort(state, operation)
        if isinstance(operation, RenameOperation):
            return self._rename(state, operation)
        if isinstance(operation, CastOperation):
            return self._cast(state, operation)
        if isinstance(operation, FillnaOperation):
            return self._fillna(state, operation)
        if isinstance(operation, DropnaOperation):
            return self._dropna(state, operation)
        if isinstance(operation, DeduplicateOperation):
            return self._deduplicate(state, operation)
        if isinstance(operation, MergeOperation):
            return self._merge(tenant_id, workspace_id, state, operation)
        if isinstance(operation, GroupByOperation):
            return self._groupby(state, operation)
        if isinstance(operation, DeriveOperation):
            return self._derive(state, operation)
        if isinstance(operation, HeadOperation):
            return self._limit(state, operation.rows, tail=False)
        if isinstance(operation, TailOperation):
            return self._limit(state, operation.rows, tail=True)
        raise ValueError("Operation is not in the DuckDB compiler allowlist.")

    def _filter(self, state: _QueryState, operation: FilterOperation) -> _QueryState:
        self._require_columns(state.columns, [operation.column])
        column = self._quote(operation.column)
        operators = {
            "eq": "=",
            "ne": "!=",
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
        }
        if operation.operator in operators:
            predicate = f"{column} {operators[operation.operator]} {self._bind(operation.value)}"
        elif operation.operator == "contains":
            predicate = f"contains(CAST({column} AS VARCHAR), {self._bind(str(operation.value))})"
        elif operation.operator == "in":
            if not isinstance(operation.value, list):
                raise ValueError("The 'in' filter requires a list value.")
            placeholders = [self._bind(value) for value in operation.value]
            predicate = f"{column} IN ({', '.join(placeholders)})" if placeholders else "FALSE"
        elif operation.operator == "notnull":
            predicate = f"{column} IS NOT NULL"
        elif operation.operator == "isnull":
            predicate = f"{column} IS NULL"
        else:
            raise ValueError("Filter operator is not allowed.")
        sql = f"SELECT * FROM ({state.sql}) AS {self._quote('_filter')} WHERE {predicate}"
        return self._state(state, sql)

    def _sort(self, state: _QueryState, operation: SortOperation) -> _QueryState:
        self._require_columns(state.columns, operation.columns)
        if isinstance(operation.ascending, bool):
            ascending = [operation.ascending] * len(operation.columns)
        else:
            ascending = operation.ascending
            if len(ascending) != len(operation.columns):
                raise ValueError("Sort direction count must match the sort column count.")
        terms = [
            f"{self._quote(column)} {'ASC' if direction else 'DESC'} NULLS LAST"
            for column, direction in zip(operation.columns, ascending, strict=True)
        ]
        ordering = ", ".join(terms)
        sql = (
            f"SELECT {self._column_list(state.columns)}, "
            f"row_number() OVER (ORDER BY {ordering}) AS {self._quote(state.order_column)} "
            f"FROM ({state.sql}) AS {self._quote('_sort')} ORDER BY {ordering}"
        )
        return self._state(state, sql)

    def _rename(self, state: _QueryState, operation: RenameOperation) -> _QueryState:
        self._require_columns(state.columns, list(operation.mapping))
        columns = [operation.mapping.get(name, name) for name in state.columns]
        self._require_unique_output(columns)
        order_column = (
            self._internal_order_column(columns)
            if state.order_column in columns
            else state.order_column
        )
        projections = [
            (
                self._quote(name)
                if operation.mapping.get(name, name) == name
                else f"{self._quote(name)} AS {self._quote(operation.mapping[name])}"
            )
            for name in state.columns
        ]
        order_projection = self._quote(state.order_column)
        if order_column != state.order_column:
            order_projection += f" AS {self._quote(order_column)}"
        projections.append(order_projection)
        lineage = {
            operation.mapping.get(name, name): value for name, value in state.lineage.items()
        }
        sql = f"SELECT {', '.join(projections)} FROM ({state.sql}) AS {self._quote('_rename')}"
        return _QueryState(
            sql=sql,
            columns=columns,
            lineage=lineage,
            order_column=order_column,
        )

    def _cast(self, state: _QueryState, operation: CastOperation) -> _QueryState:
        self._require_columns(state.columns, list(operation.mapping))
        types = {
            "string": "VARCHAR",
            "int64": "BIGINT",
            "float64": "DOUBLE",
            "boolean": "BOOLEAN",
            "datetime": "TIMESTAMP",
        }
        projections = [
            (
                f"CAST({self._quote(name)} AS {types[operation.mapping[name]]}) "
                f"AS {self._quote(name)}"
                if name in operation.mapping
                else self._quote(name)
            )
            for name in state.columns
        ]
        projections.append(self._quote(state.order_column))
        sql = f"SELECT {', '.join(projections)} FROM ({state.sql}) AS {self._quote('_cast')}"
        return self._state(state, sql)

    def _fillna(self, state: _QueryState, operation: FillnaOperation) -> _QueryState:
        self._require_columns(state.columns, list(operation.values))
        prior_parameter_count = len(self._parameters)
        projections = [
            (
                f"COALESCE({self._quote(name)}, {self._bind(operation.values[name])}) "
                f"AS {self._quote(name)}"
                if name in operation.values
                else self._quote(name)
            )
            for name in state.columns
        ]
        self._move_new_parameters_before(prior_parameter_count)
        projections.append(self._quote(state.order_column))
        sql = f"SELECT {', '.join(projections)} FROM ({state.sql}) AS {self._quote('_fillna')}"
        return self._state(state, sql)

    def _dropna(self, state: _QueryState, operation: DropnaOperation) -> _QueryState:
        columns = operation.subset or state.columns
        self._require_columns(state.columns, columns)
        joiner = " AND " if operation.how == "any" else " OR "
        predicate = joiner.join(f"{self._quote(name)} IS NOT NULL" for name in columns)
        sql = f"SELECT * FROM ({state.sql}) AS {self._quote('_dropna')} WHERE {predicate}"
        return self._state(state, sql)

    def _deduplicate(self, state: _QueryState, operation: DeduplicateOperation) -> _QueryState:
        columns = operation.subset or state.columns
        self._require_columns(state.columns, columns)
        partition = self._column_list(columns)
        if operation.keep is False:
            qualification = f"count(*) OVER (PARTITION BY {partition}) = 1"
        else:
            direction = "ASC" if operation.keep == "first" else "DESC"
            qualification = (
                f"row_number() OVER (PARTITION BY {partition} ORDER BY "
                f"{self._quote(state.order_column)} {direction}) = 1"
            )
        sql = (
            f"SELECT * FROM ({state.sql}) AS {self._quote('_deduplicate')} "
            f"QUALIFY {qualification} ORDER BY {self._quote(state.order_column)}"
        )
        return self._state(state, sql)

    def _merge(
        self,
        tenant_id: str,
        workspace_id: str,
        state: _QueryState,
        operation: MergeOperation,
    ) -> _QueryState:
        if len(operation.left_on) != len(operation.right_on):
            raise ValueError("Merge key counts must match.")
        self._require_columns(state.columns, operation.left_on)
        artifact = self._resolve_source(
            tenant_id,
            workspace_id,
            operation.right_artifact_id,
            limit=SECONDARY_ARTIFACT_LIMIT,
            role="secondary",
        )
        right_columns = self._schema_columns(artifact)
        self._require_columns(right_columns, operation.right_on)
        source = self._authorize_source(artifact)

        overlap = (set(state.columns) & set(right_columns)) - set(operation.left_on)
        left_names = [
            f"{name}{operation.suffixes[0]}" if name in overlap else name for name in state.columns
        ]
        paired_same_keys = {
            right
            for left, right in zip(operation.left_on, operation.right_on, strict=True)
            if left == right
        }
        right_entries = [
            (name, f"{name}{operation.suffixes[1]}" if name in overlap else name)
            for name in right_columns
            if name not in paired_same_keys
        ]
        columns = left_names + [output for _, output in right_entries]
        self._require_unique_output(columns)

        left_alias = self._quote("_left")
        right_alias = self._quote("_right")
        projections = [
            self._qualified_projection(left_alias, source_name, output_name)
            for source_name, output_name in zip(state.columns, left_names, strict=True)
        ]
        projections.extend(
            self._qualified_projection(right_alias, source_name, output_name)
            for source_name, output_name in right_entries
        )
        ordering = f"{left_alias}.{self._quote(state.order_column)}"
        projections.append(
            f"row_number() OVER (ORDER BY {ordering} NULLS LAST) AS "
            f"{self._quote(state.order_column)}"
        )
        conditions = [
            f"{left_alias}.{self._quote(left)} = {right_alias}.{self._quote(right)}"
            for left, right in zip(operation.left_on, operation.right_on, strict=True)
        ]
        joins = {
            "inner": "INNER JOIN",
            "left": "LEFT JOIN",
            "right": "RIGHT JOIN",
            "outer": "FULL OUTER JOIN",
        }
        sql = (
            f"SELECT {', '.join(projections)} FROM ({state.sql}) AS {left_alias} "
            f"{joins[operation.how]} {self._quote(source.relation_name)} AS {right_alias} "
            f"ON {' AND '.join(conditions)} ORDER BY {ordering} NULLS LAST"
        )
        lineage = self._merged_lineage(
            state.lineage,
            artifact.lineage,
            state.columns,
            right_columns,
            overlap,
            columns,
            operation,
        )
        return self._state(state, sql, columns, lineage)

    def _groupby(self, state: _QueryState, operation: GroupByOperation) -> _QueryState:
        referenced = operation.by + [item.column for item in operation.aggregations]
        self._require_columns(state.columns, referenced)
        columns = list(operation.by) + [item.output for item in operation.aggregations]
        self._require_unique_output(columns)
        aggregates: list[str] = []
        for item in operation.aggregations:
            column = self._quote(item.column)
            functions = {
                "sum": f"sum({column})",
                "mean": f"avg({column})",
                "median": f"median({column})",
                "min": f"min({column})",
                "max": f"max({column})",
                "count": f"count({column})",
                "nunique": f"count(DISTINCT {column})",
                "first": (
                    f"first({column} ORDER BY {self._quote(state.order_column)}) "
                    f"FILTER (WHERE {column} IS NOT NULL)"
                ),
                "last": (
                    f"first({column} ORDER BY {self._quote(state.order_column)} DESC) "
                    f"FILTER (WHERE {column} IS NOT NULL)"
                ),
            }
            aggregates.append(f"{functions[item.function]} AS {self._quote(item.output)}")
        group_columns = self._column_list(operation.by)
        projection = ", ".join([group_columns, *aggregates])
        grouped = (
            f"SELECT {projection} FROM ({state.sql}) AS {self._quote('_group_input')} "
            f"GROUP BY {group_columns}"
        )
        sql = (
            f"SELECT {self._column_list(columns)}, row_number() OVER (ORDER BY "
            f"{group_columns}) AS "
            f"{self._quote(state.order_column)} FROM ({grouped}) AS {self._quote('_group')} "
            f"ORDER BY {group_columns}"
        )
        lineage = {name: state.lineage[name] for name in operation.by if name in state.lineage}
        return self._state(state, sql, columns, lineage)

    def _derive(self, state: _QueryState, operation: DeriveOperation) -> _QueryState:
        prior_parameter_count = len(self._parameters)
        expression = self._compile_expression(state.columns, operation.expression)
        self._move_new_parameters_before(prior_parameter_count)
        if operation.column in state.columns:
            columns = list(state.columns)
            projections = [
                f"{expression} AS {self._quote(name)}"
                if name == operation.column
                else self._quote(name)
                for name in state.columns
            ]
        else:
            columns = [*state.columns, operation.column]
            self._require_unique_output(columns)
            projections = [self._quote(name) for name in state.columns]
            projections.append(f"{expression} AS {self._quote(operation.column)}")
        projections.append(self._quote(state.order_column))
        sql = f"SELECT {', '.join(projections)} FROM ({state.sql}) AS {self._quote('_derive')}"
        lineage = dict(state.lineage)
        lineage.pop(operation.column, None)
        return self._state(state, sql, columns, lineage)

    def _limit(self, state: _QueryState, rows: int, *, tail: bool) -> _QueryState:
        order = self._quote(state.order_column)
        limit = self._bind(rows)
        if tail:
            sql = (
                f"SELECT * FROM (SELECT * FROM ({state.sql}) AS {self._quote('_tail_input')} "
                f"ORDER BY {order} DESC LIMIT {limit}) AS {self._quote('_tail')} ORDER BY {order}"
            )
        else:
            sql = (
                f"SELECT * FROM ({state.sql}) AS {self._quote('_head')} "
                f"ORDER BY {order} LIMIT {limit}"
            )
        return self._state(state, sql)

    def _compile_expression(self, columns: list[str], expression: str) -> str:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise SecurityPolicyViolation("Derived expression is invalid.") from exc

        binary = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.FloorDiv: "//",
            ast.Mod: "%",
        }

        def compile_node(node: ast.AST) -> str:
            if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
                return self._bind(node.value)
            if isinstance(node, ast.Name):
                self._require_columns(columns, [node.id])
                return self._quote(node.id)
            if isinstance(node, ast.BinOp) and type(node.op) in binary:
                left = compile_node(node.left)
                right = compile_node(node.right)
                return f"({left} {binary[type(node.op)]} {right})"
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
                return f"power({compile_node(node.left)}, {compile_node(node.right)})"
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd | ast.USub):
                operator = "+" if isinstance(node.op, ast.UAdd) else "-"
                return f"({operator}{compile_node(node.operand)})"
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "col"
                and len(node.args) == 1
                and not node.keywords
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                self._require_columns(columns, [node.args[0].value])
                return self._quote(node.args[0].value)
            raise SecurityPolicyViolation(
                "Only numeric constants, arithmetic operators and col('column') are allowed."
            )

        return compile_node(tree.body)

    def _resolve_source(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        *,
        limit: int,
        role: str,
    ) -> ArtifactRecord:
        artifact = self.repository.get_artifact(tenant_id, artifact_id)
        if artifact.workspace_id != workspace_id:
            raise SecurityPolicyViolation("Artifact belongs to another workspace.")
        path = artifact.path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".parquet":
            raise SecurityPolicyViolation("Artifact is not an authorized local Parquet source.")
        if path.stat().st_size > limit:
            raise ValueError(f"The {role} artifact must be {limit // MIB} MiB or smaller.")
        return artifact

    def _authorize_source(self, artifact: ArtifactRecord) -> AuthorizedSource:
        source = AuthorizedSource(
            artifact_id=artifact.id,
            relation_name=f"source_{len(self._sources)}",
            path=artifact.path.resolve(strict=True),
            _authorization=_SourceAuthorization(
                artifact_id=artifact.id,
                relation_name=f"source_{len(self._sources)}",
                path=artifact.path.resolve(strict=True),
            ),
        )
        self._sources.append(source)
        return source

    @staticmethod
    def _schema_columns(artifact: ArtifactRecord) -> list[str]:
        raw_columns = artifact.schema.get("columns")
        if not isinstance(raw_columns, list):
            raise ValueError("Artifact schema has no column metadata.")
        columns: list[str] = []
        for item in raw_columns:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ValueError("Artifact schema contains an invalid column.")
            columns.append(item["name"])
        if not columns:
            raise ValueError("Artifact schema has no columns.")
        DuckDBCompiler._require_unique_output(columns)
        return columns

    @staticmethod
    def _require_columns(available: list[str], requested: list[str]) -> None:
        missing = [name for name in requested if name not in available]
        if missing:
            raise ValueError(f"Columns do not exist: {', '.join(missing)}")

    @staticmethod
    def _require_unique_output(columns: list[str]) -> None:
        if any(not isinstance(name, str) or not name for name in columns):
            raise ValueError("Output column names must be non-empty strings.")
        if len(columns) != len(set(columns)):
            raise ValueError("Output column names must be unique.")

    @staticmethod
    def _internal_order_column(columns: list[str]) -> str:
        name = "__tuoming_row_order"
        while name in columns:
            name += "_"
        return name

    def _select_stage(self, state: _QueryState, columns: list[str]) -> str:
        projection = [*(self._quote(name) for name in columns), self._quote(state.order_column)]
        return f"SELECT {', '.join(projection)} FROM ({state.sql}) AS {self._quote('_select')}"

    @staticmethod
    def _quote(identifier: str) -> str:
        return f'"{identifier.replace(chr(34), chr(34) * 2)}"'

    def _column_list(self, columns: list[str]) -> str:
        return ", ".join(self._quote(name) for name in columns)

    def _bind(self, value: Any) -> str:
        if value is not None and not isinstance(value, str | int | float | bool):
            raise ValueError("Analysis values must be scalar JSON values.")
        self._parameters.append(value)
        return "?"

    def _move_new_parameters_before(self, prior_parameter_count: int) -> None:
        prior = self._parameters[:prior_parameter_count]
        new = self._parameters[prior_parameter_count:]
        self._parameters[:] = [*new, *prior]

    @staticmethod
    def _state(
        previous: _QueryState,
        sql: str,
        columns: list[str] | None = None,
        lineage: dict[str, ColumnLineage] | None = None,
    ) -> _QueryState:
        return _QueryState(
            sql=sql,
            columns=previous.columns if columns is None else columns,
            lineage=previous.lineage if lineage is None else lineage,
            order_column=previous.order_column,
        )

    def _qualified_projection(self, alias: str, source: str, output: str) -> str:
        expression = f"{alias}.{self._quote(source)}"
        return expression if source == output else f"{expression} AS {self._quote(output)}"

    @staticmethod
    def _merged_lineage(
        left_lineage: dict[str, ColumnLineage],
        right_lineage: dict[str, ColumnLineage],
        left_columns: list[str],
        right_columns: list[str],
        overlap: set[str],
        output_columns: list[str],
        operation: MergeOperation,
    ) -> dict[str, ColumnLineage]:
        output: dict[str, ColumnLineage] = {}
        for name in left_columns:
            output_name = f"{name}{operation.suffixes[0]}" if name in overlap else name
            if name in left_lineage and output_name in output_columns:
                output[output_name] = left_lineage[name]
        for name in right_columns:
            output_name = f"{name}{operation.suffixes[1]}" if name in overlap else name
            if (
                name in right_lineage
                and output_name in output_columns
                and output_name not in output
            ):
                output[output_name] = right_lineage[name]
        return output


def is_authorized_source(source: AuthorizedSource) -> bool:
    authorization = source._authorization
    return (
        source.artifact_id == authorization.artifact_id
        and source.relation_name == authorization.relation_name
        and source.path == authorization.path
    )
