from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.naming import (
    GENERATED_NAME_RULE,
    GeneratedNameValidationError,
    generated_name_issues,
)
from tuoming_agent.security.dlp import PromptSanitizer

PLANNER_SYSTEM_PROMPT = f"""You are a data-operation planner.
Return only valid JSON matching AnalysisPlan.
You never write or execute Python, SQL, shell commands, imports, filesystem calls, or network calls.
Use only these approved actions: select, filter, sort, rename, cast, fillna, dropna,
deduplicate, merge, groupby, derive, head, tail.
Artifact data is already pseudonymized. Use exact artifact IDs and exact schema column names.
For derive expressions, use only arithmetic and col('column name').
Do not place personal data in result_name or safe_summary.
{GENERATED_NAME_RULE}
This rule applies only to result_name, aggregation outputs, derive columns, rename targets,
and merge suffixes. Never translate source column references.
Valid generated-name examples: “品牌营收分析”, “本期营收”, and “营收同比2026”.
Example JSON shape:
{{"input_artifact_id":"exact-id","operations":[{{"action":"head","rows":10}}],
"result_name":"数据预览","safe_summary":"Prepared a local preview"}}.
"""


class SafeAnalysisPlanner:
    def __init__(
        self,
        api_key: str | None,
        base_url: str | None,
        model_name: str,
        sanitizer: PromptSanitizer,
        model: Any | None = None,
    ):
        if model is None:
            if not api_key:
                raise ValueError("An analyst API key is required for natural-language planning.")
            chat_model = ChatOpenAI(
                api_key=api_key,
                base_url=base_url,
                model=model_name,
                temperature=0,
            )
            model = chat_model.with_structured_output(
                AnalysisPlan,
                method=_structured_output_method(base_url),
            )
        self.model = model
        self.sanitizer = sanitizer

    def create_plan(self, safe_request: str, safe_context: dict[str, Any]) -> AnalysisPlan:
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
            result = self.model.invoke(
                [
                    ("system", PLANNER_SYSTEM_PROMPT),
                    ("user", payload),
                ]
            )
            plan = (
                result
                if isinstance(result, AnalysisPlan)
                else AnalysisPlan.model_validate(result)
            )
            self.sanitizer.assert_safe(f"{plan.result_name}\n{plan.safe_summary}")
            issues = generated_name_issues(plan)
            if not issues:
                return plan
            if attempt == 1:
                raise GeneratedNameValidationError(
                    "模型未能生成合规的中文字段名称，请重试。"
                )
            payload_data = {
                **payload_data,
                "generated_name_feedback": {
                    "issues": list(issues),
                    "rule": GENERATED_NAME_RULE,
                },
            }

        raise AssertionError("Planner retry loop exhausted unexpectedly.")


def _structured_output_method(base_url: str | None) -> str:
    hostname = (urlparse(base_url).hostname or "").casefold() if base_url else ""
    if hostname == "deepseek.com" or hostname.endswith(".deepseek.com"):
        return "json_mode"
    return "json_schema"
