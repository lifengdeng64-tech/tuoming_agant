# Windows 发布与运维指南

本文面向 Tuoming Agent 维护者和企业部署人员。普通用户只需从 GitHub Releases 下载已签名安装包。

## 1. 发布产物

正式 Release 应包含：

```text
TuomingAgent-Setup.exe
TuomingAgent-Setup.exe.sha256
TuomingAgent-Windows-x64.zip
TuomingAgent-Windows-x64.zip.sha256
```

推荐普通用户使用 Inno Setup 安装器。安装范围是当前 Windows 用户：

```text
%LOCALAPPDATA%\Programs\TuomingAgent
```

安装器创建开始菜单快捷方式，可选创建桌面快捷方式。卸载会终止 `TuomingAgent.exe` 及其 Streamlit 子进程并删除程序目录，但保留 `%LOCALAPPDATA%\TuomingAgent` 下的用户数据和 DPAPI 凭据。

## 2. Authenticode

### 2.1 证书

申请可信 CA 签发的 OV 或 EV Code Signing 证书。证书主题应与最终发布主体一致。EV 证书通常使用硬件令牌或云签名服务，不能直接导出为 PFX；此时需要将 `scripts/sign_windows.ps1` 替换为对应服务商的签名命令，同时保留 SHA-256 摘要、RFC 3161 时间戳和签后验证。

当前 GitHub Actions PFX 路径使用：

- Secret `WINDOWS_SIGNING_PFX_BASE64`
- Secret `WINDOWS_SIGNING_PASSWORD`
- Variable `WINDOWS_SIGNING_TIMESTAMP_URL`，默认 `https://timestamp.digicert.com`

生成 Base64 时只在受控维护机执行，不要将 PFX、密码或 Base64 输出写入日志、仓库或 artifact。证书应设置最小权限、轮换日期和吊销流程。

### 2.2 发布门禁

`v*` 标签构建会以 `-RequireSignature` 运行。主 EXE 和安装器任一签名不是 `Valid`，workflow 失败且不会发布。手动 workflow 允许构建 unsigned 开发 artifact，但不得作为正式下载分发。

发布后抽检：

```powershell
Get-AuthenticodeSignature .\dist\TuomingAgent-Setup.exe | Format-List Status,StatusMessage,SignerCertificate
.\scripts\verify_windows_release.ps1 -RequireSignature
```

证书只能消除“未知发布者”；SmartScreen reputation 仍需稳定的签名身份、正常下载历史和低误报率积累。不要建议用户关闭 SmartScreen。

## 3. 构建和发布

维护机需要 Python 3.11+、Inno Setup 6、Windows SDK SignTool，以及启用的 Windows Defender。

```powershell
.\scripts\build_windows.ps1
.\scripts\verify_windows_release.ps1
```

为了让标签版本与安装器版本一致，发布前同步更新 `pyproject.toml`、`tuoming_agent/__init__.py` 和 Inno 默认版本。创建标签前运行完整测试。

GitHub 发布建议：

```powershell
git tag v0.3.0
git push origin v0.3.0
```

CI 会构建 onedir 应用、portable ZIP 和 Inno 安装器，签名并生成 SHA-256 文件。若仓库 Secret 配置了 `VIRUSTOTAL_API_KEY`，正式标签还会上传安装器进行多引擎检查。

## 4. 杀毒误报处理

发布前至少完成：

1. Windows Defender 对主 EXE、ZIP 和安装器的自定义扫描。
2. 在干净 Windows 10/11 虚拟机安装、启动、更新和卸载。
3. 有权限时进行 VirusTotal 扫描；不要上传包含真实用户数据或私有证书的文件。
4. 记录产物 SHA-256、签名证书 thumbprint、扫描时间和引擎结果。
5. 若误报，暂停 Release，保留原始哈希和可复现构建信息，向对应厂商提交 false-positive 样本。

避免无意义的 packer、过度混淆、自修改代码、从临时目录执行未知二进制等高误报行为。PyInstaller onedir 也减少了单文件自解压的启动等待和部分启发式误报信号。

## 5. 更新与回滚演练

应用只接受官方 GitHub Release 资产，下载受主机白名单、重定向次数和 500MiB 上限约束。SHA-256 和 Authenticode 必须验证后才写入更新清单。最多保留两个已验证安装器。

每次发布至少验证：

1. 旧版本可检查到新版本。
2. 企业代理和企业 CA 环境可下载。
3. 篡改 checksum 或安装包时更新被拒绝。
4. 未签名、过期或发布者不匹配时更新被拒绝。
5. 更新安装后工作区和 DPAPI 凭据仍可读取。
6. 设置页可选择上一个已验证安装器执行回滚。
7. 回滚后数据库 schema 仍兼容；涉及不可逆迁移时，必须先阻止回滚或提供逆向迁移。

当前更新是用户确认式，不做后台静默强制更新。安装器以 `/SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS` 启动，launcher 通过 `shutdown.request` 结束服务和托盘进程。

## 6. 备份、迁移和灾难恢复

### 6.1 备份内容与密钥

`.tmbak` 使用 Scrypt 和分帧 AES-GCM 认证加密。SQLite 通过在线 backup API 创建一致快照。归档清单记录每个文件的大小和 SHA-256。API Key 与 `MASKING_MASTER_KEY` 在内部归档前再次加密，不以明文落入临时 ZIP。

备份密码独立于 Windows 登录和 API Key，至少 10 个字符，应用不保存。组织部署时应使用密码管理器或双人保管流程。

### 6.2 恢复流程

1. 在设置页上传 `.tmbak` 并输入密码。
2. 应用写入 `%LOCALAPPDATA%\TuomingAgent\restore-staging`。
3. 校验认证标签、ZIP 路径、清单、文件大小和 SHA-256。
4. 写入 `restore.pending.json`，要求用户从托盘退出并重新启动。
5. launcher 在数据库打开前执行恢复。
6. 现有设置、数据和凭据移动到 `recovery\<timestamp>`。
7. 恢复成功后启动应用；失败时保留旧数据，不进入半恢复状态。

### 6.3 演练与保留

- 升级前和每周创建备份；保留周期按组织数据政策确定。
- 备份复制到不与工作站同故障域的加密介质。
- 每季度在隔离测试账户恢复并验证：工作区、历史消息、token 一致性、artifact、授权还原和模型设置。
- Windows DPAPI 文件本身只能由创建它的用户解密；跨机器、跨域或跨 Windows 用户必须通过 `.tmbak` 迁移。
- `recovery` 目录可能含旧的加密敏感数据，确认恢复和备份后按组织保留政策安全清理。

## 7. 企业代理、TLS 检查和内网模型

设置页支持：

- 继承 Windows/环境系统代理。
- 显式 HTTP/HTTPS 代理 URL。
- PEM/CRT 企业 CA bundle。
- 自定义 OpenAI Compatible Base URL 和模型名。

不允许在代理 URL 中保存用户名或密码，也不允许关闭 TLS 验证。代理认证应由 Windows 系统代理、企业凭据代理或设备管理策略处理。

验收矩阵至少包含：

- 无代理直连。
- 显式代理。
- Windows 系统代理。
- TLS inspection + 企业根 CA。
- DNS 不可达、407、证书链错误、超时和余额不足的可理解提示。
- 内网 OpenAI Compatible API 的 Base URL、模型发现策略和结构化输出兼容性。

## 8. 文件隔离

上传扫描拒绝：

- OOXML 路径穿越、绝对路径、重复内部路径和加密 ZIP entry。
- ZIP 条目过多、总解压体积过大、单 entry 过大和异常压缩比。
- VBA、ActiveX、外部链接、外部数据连接、QueryTable、自定义 UI 和嵌入 OLE 对象。
- CSV NUL 字节和异常超长行。

解析运行在本地且不会执行 Office 宏，但这些检查不能替代通用沙箱、Defender/EDR 或内容净化网关。高风险企业场景可将文件扫描放入低权限 AppContainer/独立进程，并对临时目录施加配额和 ACL。

## 9. 多 Windows 用户、身份和审计

当前安装、LocalAppData 和 DPAPI 都是 Windows 用户级隔离。多人共用设备时必须为每个人使用独立 Windows 账户，禁止共享同一账户。应用内 `tenant_id` 是逻辑隔离，不等同于企业认证。

当前可导出不含原始行、密钥或映射明文的工作区审计 JSONL。正式企业部署仍需另行接入：

- Entra ID/ADFS/OIDC 或组织指定 IdP。
- 用户、组、tenant 和 workspace 的授权映射。
- 带设备身份的审计签名与集中 SIEM 传输。
- 审计队列重试、断网缓存、保留策略和管理员查询。
- 数据驻留、法务保留和事件响应流程。

在这些能力完成前，不应将当前本地逻辑租户宣传为强企业多租户平台。

## 10. Provider 兼容维护

所有 Provider 通过统一工厂和设置模型调用。每次依赖升级应运行 Provider 单元测试，并在至少一个测试账号上执行连接测试和结构化计划 smoke test。预置模型名称变化时更新设置目录；用户仍可填写自定义模型名。对 SDK 主版本升级使用单独 PR，记录 API 差异、代理/CA 行为和错误分类变化。

## 11. 发布验收记录模板

每次 Release 记录：

- Git commit 与 tag。
- Python、PyInstaller、Inno Setup 和 Provider 依赖版本。
- 安装器和 ZIP SHA-256。
- Authenticode 状态、签名主体、证书 thumbprint 和时间戳。
- pytest、Ruff、compileall 结果。
- Defender/VirusTotal 或企业 EDR 结果。
- 干净 Windows 10/11 安装、首次启动、升级、回滚、备份恢复和卸载结果。
- 已知问题、数据库迁移限制和回滚条件。
