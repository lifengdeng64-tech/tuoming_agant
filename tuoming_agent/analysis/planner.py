from __future__ import annotations

import json
from typing import Any

from langchain_core.exceptions import OutputParserException
from pydantic import ValidationError

from tuoming_agent.analysis.errors import AnalysisPlanValidationError, AnalysisProviderError
from tuoming_agent.analysis.models import AnalysisPlan, FillnaOperation, FilterOperation
from tuoming_agent.analysis.naming import (
    GENERATED_NAME_RULE,
    GeneratedNameValidationError,
    generated_name_issue_paths,
    generated_names,
)
from tuoming_agent.providers import AnalysisModelProvider, create_provider, provider_error_details
from tuoming_agent.security.dlp import PromptSanitizer
from tuoming_agent.settings import PROVIDER_BY_ID, ModelSettings, NetworkSettings

PLANNER_SYSTEM_PROMPT = f"""You are a data-operation planner.
Return only valid JSON matching AnalysisPlan.
You never write or execute Python, SQL, shell commands, imports, filesystem calls, or network calls.
Use only these approved actions: select, filter, sort, rename, cast, fillna, dropna,
deduplicate, merge, groupby, derive, head, tail.
When the user asks for a chart, dashboard, BI board, trend, comparison, or KPI, populate
the optional dashboard object. Dashboard measures must contain one to four exact numeric
output column names; aggregation is sum, mean, min, max, or count; date_column and
category_column must be exact output column names or null. All charts and aggregations
will be rendered locally; never send rows or chart data to the model.
If the user only asks for a dashboard and no data transformation is needed, operations may
be empty. Otherwise, express all cleaning and analysis as ordered allowlisted operations.
Artifact data is already pseudonymized. Use exact artifact IDs and exact schema column names.
For derive expressions, use only arithmetic, col('column name'), and
safe_divide(numerator, denominator). Use safe_divide for completion rates, ratios, and
year-over-year calculations so a zero denominator returns null. Never emit SQL functions,
conditionals, comparisons, or Python helpers inside an expression.
Do not place personal data in result_name or safe_summary.
Apply row filters before aggregation. For “剔除临时停业”, filter the exact status column
with operator "ne" and value “临时停业” before grouping.
For weighted completion, do not average row-level completion percentages. Prefer additive
numerators and denominators: group by the requested dimensions, sum actual and target values,
then derive completion = safe_divide(summed actual, summed target). For completion同比, also sum the
prior-period actual and target values, derive prior completion, then derive
同比 = safe_divide(current completion, prior completion) - 1. If the schema instead provides
a metric and an explicit weight, derive metric * weight before grouping, sum that contribution
and the weight, then divide the two sums. Use only columns that actually exist in the supplied
schema.
{GENERATED_NAME_RULE}
This rule applies only to result_name, aggregation outputs, derive columns, rename targets,
and merge suffixes. Never translate source column references.
Valid generated-name examples: “品牌营收分析”, “本期营收”, and “营收同比2026”.
Example JSON shape:
{{"input_artifact_id":"exact-id","operations":[{{"action":"head","rows":10}}],
"result_name":"数据预览","safe_summary":"Prepared a local preview","dashboard":null}}.
"""


class SafeAnalysisPlanner:
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model_name: str,
        sanitizer: PromptSanitizer,
        model: Any | None = None,
        *,
        provider_name: str = "deepseek",
        provider: AnalysisModelProvider | None = None,
        network_settings: NetworkSettings | None = None,
    ):
        if model is None:
            if not api_key:
                raise ValueError("An analyst API key is required for natural-language planning.")
            definition = PROVIDER_BY_ID.get(provider_name, PROVIDER_BY_ID["openai_compatible"])
            settings = ModelSettings(
                provider=definition.id,
                base_url=base_url or definition.base_url,
                model_name=model_name,
            )
            provider = provider or create_provider(settings, api_key, network_settings)
            model = provider.structured_model(AnalysisPlan)
        self.model = model
        self.sanitizer = sanitizer

        self.api_key = api_key or ""

    def create_plan(
        self, safe_request: str, safe_context: dict[str, Any], tenant_id: str
    ) -> AnalysisPlan:
        payload_data = {
            "request": safe_request,
            "workspace_context": safe_context,
            "analysis_plan_schema": AnalysisPlan.model_json_schema(),
        }
        for attempt in range(2):
            payload = json.dumps(
                payload_data,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self.sanitizer.assert_safe(payload)
            try:
                result = self.model.invoke(
                    [
                        ("system", PLANNER_SYSTEM_PROMPT),
                        ("user", payload),
                    ]
                )
            except (OutputParserException, ValidationError) as exc:
                raise AnalysisPlanValidationError(
                    "模型已响应，但返回的分析计划格式不符合安全规则，请重试或拆分分析步骤。",
                    "plan_validation",
                ) from exc
            except Exception as exc:
                details = provider_error_details(exc, self.api_key)
                raise AnalysisProviderError(details.message, details.code) from exc
            try:
                plan = (
                    result
                    if isinstance(result, AnalysisPlan)
                    else AnalysisPlan.model_validate(result)
                )
            except ValidationError as exc:
                raise AnalysisPlanValidationError(
                    "模型已响应，但返回的分析计划格式不符合安全规则，请重试或拆分分析步骤。",
                    "plan_validation",
                ) from exc
            self.sanitizer.assert_tenant_safe(
                tenant_id,
                json.dumps(
                    _model_generated_values(plan),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ),
            )
            issue_paths = generated_name_issue_paths(plan)
            if not issue_paths:
                return plan
            if attempt == 1:
                raise GeneratedNameValidationError("模型未能生成合规的中文字段名称，请重试。")
            payload_data = {
                **payload_data,
                "generated_name_feedback": {
                    "issues": list(issue_paths),
                    "rule": GENERATED_NAME_RULE,
                },
            }

        raise AssertionError("Planner retry loop exhausted unexpectedly.")


def _model_generated_values(plan: AnalysisPlan) -> list[Any]:
    values: list[Any] = [plan.safe_summary, *generated_names(plan)]
    for operation in plan.operations:
        if isinstance(operation, FilterOperation):
            values.append(operation.value)
        elif isinstance(operation, FillnaOperation):
            values.extend(operation.values.values())
    return values
