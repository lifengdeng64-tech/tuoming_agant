# 透明数据安全工作台

一个本地优先的数据清洗与分析工作区。原始文件在本地使用 AES-GCM 加密保存，敏感字段经过租户隔离的 HMAC-SHA256 稳定 token 化后，才进入分析链路。

## 数据流与云端边界

```text
首次配置云端模型 API
        ↓
API Key 保存在本机配置中
        ↓
上传 CSV / Excel
        ↓
本地分批读取、识别敏感字段并脱敏
        ↓
只把必要的脱敏请求、schema、行数和 artifact ID 发给云端模型
        ↓
云端模型返回结构化 AnalysisPlan
        ↓
本地校验计划并由用户确认
        ↓
本地 DuckDB 白名单执行、质量检查并保存结果
```

云端模型不会收到原始数据行、本地文件路径、SQL、DuckDB 配置或主密钥，也不能直接执行 Python。它只负责生成受 Pydantic 约束的结构化分析计划；读取、脱敏、计算、还原和导出均在本机完成。

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
- **数据**：增量上传文件，在表格中确认疑似敏感字段、语义域与标准化策略。检测结果是建议：可取消勾选以明确保留原值，该决定会写入本地审计日志；未经确认的敏感字段仍会失败关闭。
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

`.env` 只表示密钥和 API Key 留在本机，并不等同于操作系统级安全存储。不要提交或分享 `.env`；正式面向普通用户交付时，应改用 Windows Credential Manager、DPAPI 或独立密钥管理服务保护这些凭据。

分析运行、计划版本和执行尝试保存在 SQLite 中，刷新或重启后仍可继续确认。业务执行或质量错误会在配置上限内生成修复计划，每一版都必须重新确认；安全拒绝会立即停止，绝不自动修复。

## 运行

```powershell
streamlit run app.py
```

默认数据目录是 `.tuoming-data/`，可通过 `TUOMING_DATA_DIR` 修改。生产环境应把 `MASKING_MASTER_KEY` 放在密钥管理服务中，而不是普通 `.env` 文件。

## 大文件边界与资源设置

当前版本面向 **16GB 内存的普通 Windows 电脑**，验收边界如下：

- CSV 单文件不超过 **200MiB**，XLSX/XLSM 单文件不超过 **100MiB**。
- 合并时主表制品不超过 **200MiB**，辅助表制品不超过 **50MiB**。
- 导入后数据保存在本地加密原件和脱敏 Parquet 中；DuckDB 仅在本机扫描受授权制品。模型只接收脱敏请求、schema、行数和 artifact ID，**不会接收数据行、文件路径、SQL 或 DuckDB 配置**。
- 导入写入前会检查 `TUOMING_DATA_DIR` 所在卷的可用空间，保守要求为输入文件的 3 倍加 DuckDB 临时空间额度。该检查不原子预留空间；磁盘不足时请先清理空间或减小文件。
- CSV 和 Excel 导入均分批处理。结果预览最多 1,000 行；CSV 与还原导出按批流式写入，较大的结果应优先选择 CSV/Parquet，Excel 仍受 100,000 行和估算 50MiB 上限约束。

可通过以下环境变量调整每任务资源上限；安全上限分别是 2GiB、4 线程和 4GiB，超过会拒绝启动任务：

```dotenv
DUCKDB_MEMORY_LIMIT=2GiB
DUCKDB_THREADS=4
DUCKDB_MAX_TEMP_DIRECTORY_SIZE=4GiB
```

DuckDB 内存或临时磁盘达到上限时，服务会返回可操作的资源提示。超过 24 小时的任务临时文件可调用 `tuoming_agent.maintenance.cleanup_stale_files(...)` 清理；清理只识别数据目录下的任务自有模式，不跟随符号链接/重解析点，并保护 SQLite 已引用的上传与制品路径。

## 验证

```powershell
python -m pytest
python -m ruff check .
python -m compileall -q tuoming_agent
```

快速套件包含一个约 1MiB 的真实流水线 smoke 基准。发布前可按需生成近 200MiB CSV 并执行完整验收；测试数据写入 pytest 临时目录，不需要提交大型 fixture：

```powershell
$env:TUOMING_RUN_LARGE_BENCHMARK = "1"
python -m pytest tests/performance/test_200mb_pipeline.py -m performance -s
```

基准报告导入、代表性筛选/分组/排序耗时；安装 `psutil` 时还会报告采样峰值 RSS。200MiB CSV 的目标导入时间为 10 分钟以内，实际结果受磁盘、CPU 和安全扫描内容影响。

当前开发机的一次实际验收结果：约 **208.7MB**、**223,891 行**的 CSV 完成加密、逐批敏感检查、脱敏和 Parquet 入库约耗时 **30.35 秒**；随后筛选约 0.0018 秒、分组约 0.0036 秒、排序约 0.0024 秒。该数据只代表本次测试环境，不作为所有电脑上的固定性能承诺。

## 安全边界

token 化属于可逆伪名化，不等同于不可逆匿名化。授权还原只按 artifact 的字段血缘执行，不进行全表字符串替换。当前 MVP 使用配置中的租户 ID 进行隔离；对公网部署前仍需接入真实身份认证、KMS、操作权限、备份和集中审计。
