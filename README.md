# 透明数据安全工作台

一个本地优先的数据清洗与分析工作区。原始文件在本地使用 AES-GCM 加密保存，敏感字段经过租户隔离的 HMAC-SHA256 稳定 token 化后，才进入分析链路。

## 核心能力

- 同一租户、同一语义域和同一标准化值，跨文件、跨会话保持相同 token。
- 不同租户和不同语义域彼此隔离，避免无意的跨域关联。
- SQLite 持久化工作区、数据集版本、对话、字段策略和制品血缘。
- 按文件内容 SHA-256 去重，支持追加 CSV、XLSX、XLSM 和 Excel 多 Sheet。
- 分析结果保存为脱敏 Parquet 制品，可以继续作为后续分析输入。
- 外部模型只接收脱敏消息、schema、行数和 artifact ID，不接收原始数据行。
- Pydantic 分析计划和白名单执行器取代任意 Python 代码执行。

## 工作台

- **概览**：数据集、上传文件、制品、近期消息和安全审计一屏查看。
- **数据**：增量上传文件，在表格中确认敏感字段、语义域与标准化策略。
- **分析**：明确选择主数据制品，先预览中文结构化计划；确认后才在本地白名单执行，并通过确定性质量门。
- **结果**：在脱敏预览和授权还原间切换，分别下载安全版与还原版文件。

界面使用单视图导航，避免每次 Streamlit 重跑同时加载所有页面；工作区 ID 和当前视图会保留在 URL 中，刷新后可以恢复。

## 安装

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m tuoming_agent.keygen
```

把最后一个命令生成的值写入 `.env` 的 `MASKING_MASTER_KEY`。这个密钥必须稳定保存；丢失后无法还原历史映射，改变后 token 也会变化。

自然语言分析还需要配置：

```dotenv
ANALYST_API_KEY=your-key
ANALYST_BASE_URL=https://api.deepseek.com
ANALYST_MODEL_NAME=deepseek-chat
ANALYSIS_MAX_REPAIR_ATTEMPTS=3
```

分析运行、计划版本和执行尝试保存在 SQLite 中，刷新或重启后仍可继续确认。业务执行或质量错误会在配置上限内生成修复计划，每一版都必须重新确认；安全拒绝会立即停止，绝不自动修复。

## 运行

```powershell
streamlit run app.py
```

默认数据目录是 `.tuoming-data/`，可通过 `TUOMING_DATA_DIR` 修改。生产环境应把 `MASKING_MASTER_KEY` 放在密钥管理服务中，而不是普通 `.env` 文件。

## 验证

```powershell
python -m pytest
python -m ruff check .
python -m compileall -q tuoming_agent
```

## 安全边界

token 化属于可逆伪名化，不等同于不可逆匿名化。授权还原只按 artifact 的字段血缘执行，不进行全表字符串替换。当前 MVP 使用配置中的租户 ID 进行隔离；对公网部署前仍需接入真实身份认证、KMS、操作权限、备份和集中审计。
