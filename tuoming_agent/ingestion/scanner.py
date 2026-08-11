from __future__ import annotations

import re

import pandas as pd

from tuoming_agent.security.dlp import PII_PATTERNS

SENSITIVE_COLUMN_NAMES = re.compile(
    r"(姓名|名字|客户|顾客|会员|员工|手机号|电话|邮箱|身份证|证件|地址|门店|酒店|账号|银行卡|"
    r"name|customer|client|member|employee|phone|mobile|email|id.?card|address|store|hotel|account)",
    re.IGNORECASE,
)


def detect_sensitive_columns(dataframe: pd.DataFrame, sample_size: int = 200) -> set[str]:
    detected: set[str] = set()
    for column in dataframe.columns:
        column_name = str(column)
        if SENSITIVE_COLUMN_NAMES.search(column_name):
            detected.add(column_name)
            continue
        values = dataframe[column].dropna().astype(str).head(sample_size)
        if any(pattern.search(value) for value in values for pattern in PII_PATTERNS.values()):
            detected.add(column_name)
    return detected

