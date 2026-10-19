# Jenkins K8s MCP

通用的 Jenkins 与 Kubernetes 管理 MCP，面向个人跨项目使用，也支持其他使用者配置自己的连接后独立使用。

**阶段：pre-code，2026-10-19 完成立项。当前仓库包含产品范围、接口草案和架构设计，尚无可运行服务、安装包或部署实例。**

产品范围和验收要求见 [PRD](AIREADME/PRD.md)，整体设计见 [ARCHITECTURE](AIREADME/ARCHITECTURE.md)。文档中的能力均为已确认需求或设计，实际实现与验证状态以后续代码、测试和发布记录为准。

## 规划能力

- Jenkins：Job 与 Pipeline 管理、参数化构建、队列控制、构建跟踪、增量日志、产物和报告。
- Kubernetes：内建资源与 CRD 发现、通用资源变更、重启与扩缩容、配置与 Secret、排障和容器 exec。
- 共用执行核心：多连接、明确目标、变更预览、操作记录和结果核验，同时支持本地 stdio 与远程 Streamable HTTP。

以上能力均待实现，各项平台依赖、实现状态和验收证据将随开发逐项记录。

## 设计入口

- [AIREADME 索引](AIREADME/INDEX.md)：项目文档的唯一导航。
- [产品范围](AIREADME/PRD.md)：Job 管理、构建、K8s 资源、排障、Secret 和 exec。
- [核心边界](AIREADME/CORE.md)：产品身份、授权和凭据边界。
- [接口草案](AIREADME/SPEC.md)：连接、操作和结果模型，尚未冻结工具 schema。
- [架构](AIREADME/ARCHITECTURE.md)：执行核心、平台适配器和 MCP 入口。
- [决策记录](AIREADME/DECISIONS.md)：立项阶段已经确认的取舍。
- [后续方向](AIREADME/ROADMAP.md)：从立项到真实接入验收。

## 接入原则

每个使用者独立管理连接和凭据。服务、Job、namespace、环境名称和分支通过目标系统发现或由请求提供，产品代码不内置组织或项目目录。

设计同时覆盖本地 stdio 和远程 Streamable HTTP，两种入口复用同一执行核心。实际安装与启动说明将在首个可运行版本验证后补齐。

## 开发入口

AI 协作者先读 [AGENTS.md](AGENTS.md)。实现默认采用 Python，具体依赖和兼容版本在首次编码时核验并锁定。

当前尚无应用测试、构建或安装命令。开发前先读取现有产品边界与接口草案，新增能力时同步实际工具清单、测试和对应文档。

## 仓库与许可证

公开仓库：[iyuenan3/jenkins-k8s-mcp](https://github.com/iyuenan3/jenkins-k8s-mcp)。开源许可证尚未确定。
