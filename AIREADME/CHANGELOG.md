# 里程碑记录

## 0.1.0-source · 2026-10-19

### Added

- 将首版实现形成内容提交 [499c2e7](https://github.com/iyuenan3/jenkins-k8s-mcp/commit/499c2e7d72fafe98ee941008d6fe79be1fdc03cb)，推送至公开仓库 main。

### Changed

- README 补充 68 项本地测试、CI 徽章和严格验证命令；GitHub About 改为已实现的 37 个工具、两种传输与通用连接描述。
- AIREADME 按约定锚定实际内容提交，配套文档单独提交，保留此前立项和本地实现记录。
- [GitHub CI 39246730089](https://github.com/iyuenan3/jenkins-k8s-mcp/actions/runs/39246730089) 在内容提交 499c2e7 上通过；Ubuntu 与 macOS 各通过 68 项严格警告测试、lint、格式检查及 wheel、源码包构建。

本次公开托管源码，不代表发布 PyPI 包、release tag 或完成真实业务系统验收。

## 0.1.0-local · 2026-10-19

本地候选版本，尚未提交本轮实现、推送或发布应用包。原有公开文档提交继续保留。

### Added

- Python 包、可安装 CLI、配置示例和锁定依赖，stdio 与 Streamable HTTP 共用执行核心。
- 两个平台均启用时注册 37 个工具，覆盖 PRD 的 13 个能力组及逐项依赖提示。
- 固定计划、可选管理员确认、请求去重、SQLite 元数据、原生回执、预算和短期输出。
- 68 项机制与协议测试通过，严格警告检查无警告；真实 SDK 到模拟平台的 HTTP/WS、认证和进程生命周期已验证。
- wheel、源码分发包及独立虚拟环境安装验证；新增 Ubuntu/macOS CI 定义，尚无远程运行记录。

### Changed

- README 补齐安装、配置、两种入口与使用边界，AIREADME 和 AI 入口按当前实现维护。
- 真实业务验收保持独立待进行。特殊插件参数、交互 exec、OAuth 和更多版本矩阵仍需扩展或验证，见 SPEC 与 ROADMAP。

### Fixed

- 避免未知写入自动重试、旧观察覆盖终态、日志截断跳过字节及缺失对象 apply 覆盖并发创建对象。
- 修复 SDK 结构化输出和 exec 资源释放，经验见 MEMORY，执行机制理由见 ADR-007 至 ADR-009。

## docs-published · 2026-10-19

### Added

- 将 16 个立项文件形成首个内容提交 [9af2549](https://github.com/iyuenan3/jenkins-k8s-mcp/commit/9af2549c6d9da05854fbf74797b9d6beafdf1194)，并推送至公开仓库的 `main` 分支。

### Changed

- README 补充规划能力、开发入口和当前 pre-code 状态。
- GitHub About 更新项目描述与 MCP、Jenkins、Kubernetes、DevOps、Python 主题标签。
- AIREADME 同步锚点使用实际内容提交 SHA，后续同步文档单独提交。

本次发布立项文档，尚无应用实现、安装包或应用版本。

## repository-created · 2026-10-19

### Added

- 创建 GitHub 公开仓库 [iyuenan3/jenkins-k8s-mcp](https://github.com/iyuenan3/jenkins-k8s-mcp)。
- 将本地 `origin` 关联到该仓库，使用 HTTPS。

本次建立空仓库和连接，尚未提交或推送本地文件；开源许可证尚未确定。

## pre-code · 2026-10-19

### Added

- 确定项目名称 Jenkins K8s MCP 和 slug jenkins-k8s-mcp。
- 建立 README、AI 工作入口、AIREADME 和忽略规则。
- 保留产品范围、架构、授权方式、两种运行入口与验收要求。
- 明确本阶段仅有文档，没有代码、部署、安装包或真实系统验证。

决策理由见 DECISIONS。本条为立项里程碑，不代表应用版本已经发布。
