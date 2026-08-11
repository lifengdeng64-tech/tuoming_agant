# Agent 核心闭环实施计划

## 1. 数据模型与仓储

- 先测试 SQLite 初始化、租户隔离、计划版本、执行尝试和状态转换。
- 新增 workflow 数据类和三张表。
- 实现原子状态转换与运行快照读取。

## 2. 安全执行与质量门

- 先测试候选结果不落盘、安全异常分类和质量失败规则。
- 将执行器拆成 `prepare`（内存候选）与 `execute`（兼容旧调用）。
- 新增确定性 `AnalysisQualityValidator`。

## 3. 工作流服务

- 先测试初始计划、确认/拒绝、反馈改写、重启恢复、业务错误多轮修复、修复上限以及安全拒绝零修复。
- 实现 `AnalysisWorkflowService`，统一编排 planner、executor、validator 和 repository。
- 对每个修复版本执行重复计划检测并要求再次确认。

## 4. 中文计划预览与 UI

- 先测试各白名单操作的中文描述。
- 在 Streamlit 分析页展示当前运行、计划版本、确认/拒绝/反馈控件和质量结果。
- 保持已有消息与制品查看流程兼容。

## 5. 配置与验证

- 新增 `ANALYSIS_MAX_REPAIR_ATTEMPTS`，默认 3，校验为非负整数。
- 更新环境示例和 README。
- 运行全部 pytest、ruff，并审查 git diff；通过后发布 `codex/agent-core-loop` 分支。

