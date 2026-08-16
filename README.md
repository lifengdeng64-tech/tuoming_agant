# Tuoming Agent

Tuoming Agent 是一个本地优先、隐私保护的 AI 数据分析工作台。CSV 和 Excel 在本机完成文件安全检查、敏感字段识别、稳定脱敏、清洗与白名单分析；外部模型只接收经过 DLP 检查的最小化脱敏上下文。

## Windows 用户：下载、安装、双击使用

[**Download Tuoming Agent for Windows →**](https://github.com/lifengdeng64-tech/tuoming_agant/releases)

推荐下载 `TuomingAgent-Setup.exe`：

1. 双击安装，无需安装 Python、pip 或 Streamlit。
2. 安装器创建开始菜单快捷方式，并可选创建桌面快捷方式。
3. 启动后自动初始化 `%LOCALAPPDATA%\TuomingAgent`、启动本地服务并打开浏览器工作台。
4. 首次运行选择模型服务商、填写 API Key、测试连接并保存。
5. 后续启动会恢复模型设置、工作区、会话、数据集和分析制品。

不希望安装时可下载 `TuomingAgent-Windows-x64.zip`，解压后运行其中的 `TuomingAgent.exe`。当前构建采用 PyInstaller onedir，避免单文件 EXE 每次启动都完整解压依赖。

正式 `v*` Release 必须通过 Authenticode 签名检查才能发布。仓库不会包含签名证书；如果某个安装包仍显示“未知发布者”，不要绕过警告，应等待维护者发布正确签名的版本。

## 首次启动与本地安全

首次启动自动完成：

- 创建用户级应用目录、数据目录和日志目录。
- 生成 256-bit `MASKING_MASTER_KEY`，用当前 Windows 用户作用域的 DPAPI 加密保存。
- 支持 DeepSeek、OpenAI、Anthropic Claude、Google Gemini、通义千问、智谱，以及自定义 OpenAI Compatible API。
- API Key 使用 Windows DPAPI 保存；服务商、模型名、Base URL、代理和企业 CA 路径等非敏感设置保存在 `settings.json`。
- 检测到历史数据库或制品但主密钥缺失时拒绝静默生成新密钥，避免历史 token 变化或数据无法还原。

默认目录：

```text
%LOCALAPPDATA%\TuomingAgent\
├── credentials\       # 当前 Windows 用户 DPAPI 加密的 API Key 与主密钥
├── data\              # SQLite、加密上传文件、脱敏 Parquet 制品
├── backups\           # 用户创建的加密 .tmbak 备份
├── updates\           # 已验证安装器，最多保留两个版本用于回滚
├── recovery\          # 恢复前的旧数据快照
├── logs\              # 启动诊断；不得包含原始数据或密钥
└── settings.json       # 非敏感设置
```

卸载程序只删除应用程序文件，默认保留上述用户数据，便于重装和恢复。需要彻底清除时，应先确认备份可用，再由用户手动删除数据目录。

## 数据流与模型边界

```text
上传 CSV / Excel
        ↓
本机检查压缩炸弹、路径穿越、宏、外链、嵌入对象和异常 CSV
        ↓
本机识别敏感字段并稳定脱敏
        ↓
仅发送脱敏请求、脱敏摘要、schema、行数、目录和 artifact 元数据
        ↓
模型返回受 Pydantic 校验的结构化 AnalysisPlan
        ↓
本机确认并由 DuckDB 白名单执行器执行
        ↓
保存脱敏 Parquet 制品，可继续追问、筛选、合并或授权导出
```

外部模型不会收到原始数据行、原始敏感值、本地文件路径、映射库明文、SQLite/DuckDB 配置、SQL、API Key 或脱敏主密钥。模型请求发送前会检查手机号、身份证、邮箱等疑似明文 PII，命中时阻止发送。API Key 只会作为鉴权凭据发送给用户选择的服务商，不上传到 Tuoming 自有服务器。

## 核心能力

- 同一租户、同一 `masking_domain`、同一标准化值和同一密钥版本，跨文件、跨会话、跨重启得到相同 token。
- 不同租户和不同语义域默认得到不同 token；门店名称、酒店名称、门店名等可绑定到同一语义域。
- 使用 HMAC-SHA256 和至少 80-bit token 有效长度，不使用普通 MD5/SHA 直接脱敏。
- SQLite 持久化工作区、数据集版本、文件、会话、消息、字段策略、token 映射、制品血缘和审计事件。
- 按文件内容 SHA-256 去重；支持同名文件、常见 CSV 编码、Excel 多 Sheet 和不含活动内容的 XLSX/XLSM。
- 追加文件不会清空历史消息、旧数据集或旧制品；通过 `workspace_id` 在刷新和重启后恢复上下文。
- 分析结果保存为脱敏 Parquet 制品，可继续执行“基于刚才结果继续筛选”等操作。
- 白名单操作：`select`、`filter`、`sort`、`rename`、`cast`、`fillna`、`dropna`、`deduplicate`、`merge`、`groupby`、`aggregate`、`derive`、`head`、`tail`。

## 工作台页面

- **概览**：数据集、上传文件、制品、近期消息和安全审计一屏查看。
- **数据**：增量上传文件，在表格中确认疑似敏感字段、语义域与标准化策略。检测结果是建议：可取消勾选以明确保留原值，该决定会写入本地审计日志；未经确认的敏感字段仍会失败关闭。误上传文件可从文件记录删除，系统会先展示数据版本、下游制品和分析任务的影响；有关联时必须二次确认。删除保留聊天文字和租户 Token 映射。
- **分析**：明确选择主数据制品，先预览中文结构化计划；确认后才在本地白名单执行，并通过确定性质量门。
- **结果**：在脱敏预览和授权还原间切换，分别下载安全版与还原版文件。
- **设置 / 模型**：配置服务商、API Key、模型、自定义 Base URL，并测试连接。
- **设置 / 企业网络**：使用系统代理或显式 HTTP/HTTPS 代理，加载企业 PEM/CRT CA；不允许关闭 TLS 验证。
- **设置 / 备份与恢复**：创建带独立密码的加密 `.tmbak`；验证后在下次启动前原子恢复。
- **设置 / 更新与审计**：检查、下载并验证官方更新，选择保留的安装器回滚，导出当前工作区脱敏审计 JSONL。

Provider 采用统一接口。预置模型目录只是便捷选项，服务商更名或发布新模型时可使用“自定义模型名称”和 OpenAI Compatible 配置；SDK/API 的破坏性变化仍需持续维护。

## 更新、回滚与企业网络

更新只从固定的官方 GitHub Release API 和允许的 HTTPS 主机下载。安装前执行：

1. Release 版本和资产名校验。
2. 500MiB 下载上限和重定向主机白名单。
3. SHA-256 校验。
4. Windows Authenticode 有效签名校验；部署方也可配置证书 thumbprint 固定发布者。
5. 安装器保存到受控更新目录，最多保留两个已验证版本。

更新由用户确认后启动 Inno Setup，不静默强制安装。回滚会重新运行已保留、已验证的旧安装器。代理地址不得包含用户名和密码；有凭据的企业代理应由 Windows 系统代理统一管理。TLS 检查环境可以在设置中指定企业 CA 文件，但不能使用 `verify=False`。

## 备份、迁移与灾难恢复

`.tmbak` 使用 Scrypt 派生密钥和分帧 AES-GCM 认证加密，包含 SQLite 一致性快照、数据制品、非敏感设置和迁移所需的加密凭据。备份密码不会保存，丢失后无法恢复。

建议：

- 每次升级前创建备份并复制到受控离线介质。
- 每季度在隔离 Windows 账户或测试机执行一次恢复演练。
- 恢复先写入 staging 并校验清单、大小、SHA-256 和路径，再在下次启动前替换；旧数据移动到 `recovery`，便于人工回退。
- Windows DPAPI 文件不能简单复制到另一个用户；跨用户或跨机器必须使用 `.tmbak` 迁移。

详细发布、签名、杀毒和灾备流程见 [Windows 发布与运维指南](docs/windows-release.md)。

## 上传安全边界

上传前会检查 OOXML ZIP 条目数量、总解压体积、单条目体积、压缩比、重复路径、绝对路径、目录穿越和加密条目，并拒绝 VBA、ActiveX、外部连接、外部链接、QueryTable、嵌入 OLE 对象和自定义 UI。CSV 会拒绝 NUL 字节和异常超长行。错误和日志不包含原始单元格内容。

这些检查降低常见恶意文档风险，但不等同于通用文件沙箱或企业 EDR。生产环境仍应保留 Windows Defender/EDR、下载来源控制和最小权限策略。

## Windows 构建与发布

源码构建需要 Python 3.11+ 和 Inno Setup 6：

```powershell
.\scripts\build_windows.ps1
.\scripts\verify_windows_release.ps1
```

输出：

```text
dist\TuomingAgent\TuomingAgent.exe
dist\TuomingAgent-Windows-x64.zip
dist\TuomingAgent-Setup.exe
对应的 .sha256 文件
```

`v*` 标签触发 GitHub Actions：测试、Ruff、compileall、onedir 构建、主 EXE 与安装器签名、Inno 打包、校验、Windows Defender 扫描，以及配置了 `VIRUSTOTAL_API_KEY` 时的 VirusTotal 上传。正式标签缺少签名机密时构建会失败，不发布 unsigned 正式版本。

## 开发者：从源码运行

普通用户不需要执行本节命令。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[desktop,dev]"
Copy-Item .env.example .env
python -m tuoming_agent.keygen
streamlit run app.py
```

开发模式的 `.env` 仅用于源码运行。不要提交 `.env`、PFX、API Key、主密钥、备份文件或本地数据目录。

项目使用 Streamlit `viewer` 工具栏模式，避免浏览器中 `Ctrl+C` 复制误触“清除缓存”。拉取此配置后需要停止并重新启动 Streamlit 服务。

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
.\scripts\build_windows.ps1
.\scripts\verify_windows_release.ps1
```

可选大文件基准：

```powershell
$env:TUOMING_RUN_LARGE_BENCHMARK = "1"
python -m pytest tests/performance/test_200mb_pipeline.py -m performance -s
```

## 当前生产边界

已实现 Windows 用户级安装与数据隔离、DPAPI、加密迁移备份、更新校验和本地回滚、企业代理/CA、上传隔离、审计导出及发布扫描流程。仍依赖外部资源或企业部署的部分包括：真实 OV/EV Authenticode 证书及 SmartScreen reputation、企业 IdP/OIDC、集中 SIEM/审计服务器、各类企业 EDR 产品矩阵和 VirusTotal 账号。当前租户默认仍是本地逻辑租户；面向多人共用一台机器或受监管组织时，应结合独立 Windows 账户、企业身份、访问控制和集中审计部署。
