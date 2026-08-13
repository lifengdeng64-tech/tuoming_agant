from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.security.dlp import PromptSanitizer

PLANNER_SYSTEM_PROMPT = """You are a data-operation planner.
Return only valid JSON matching AnalysisPlan.
You never write or execute Python, SQL, shell commands, imports, filesystem calls, or network calls.
Use only these approved actions: select, filter, sort, rename, cast, fillna, dropna,
deduplicate, merge, groupby, derive, head, tail.
Artifact data is already pseudonymized. Use exact artifact IDs and exact schema column names.
For derive expressions, use only arithmetic and col('column name').
Do not place personal data in result_name or safe_summary.
Example JSON shape:
{"input_artifact_id":"exact-id","operations":[{"action":"head","rows":10}],
"result_name":"Analysis result","safe_summary":"Prepared a local preview"}.
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
        payload = json.dumps(
            {
                "request": safe_request,
                "workspace_context": safe_context,
                "analysis_plan_schema": AnalysisPlan.model_json_schema(),
            },
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
        plan = result if isinstance(result, AnalysisPlan) else AnalysisPlan.model_validate(result)
        self.sanitizer.assert_safe(f"{plan.result_name}\n{plan.safe_summary}")
        return plan


def _structured_output_method(base_url: str | None) -> str:
    hostname = (urlparse(base_url).hostname or "").casefold() if base_url else ""
    if hostname == "deepseek.com" or hostname.endswith(".deepseek.com"):
        return "json_mode"
    return "json_schema"
