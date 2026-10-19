# AGENTS.md

本文件是 Jenkins K8s MCP 的 AI 工作入口。

## 当前阶段

2026-10-19 完成立项，当前为 pre-code。用户已确认产品范围与主要设计方向，尚无实现、运行实例或业务验收。

## 启动顺序

1. 读 AIREADME/INDEX.md。
2. 读 CORE、PRD、DECISIONS，恢复已确认的产品边界。
3. 按任务读 SPEC、ARCHITECTURE、CONVENTIONS、ROADMAP。
4. 实施前检查当前 Git 状态，保留已有改动。

## 任务路由

| 任务 | 必读 |
| --- | --- |
| 跨项目接手 | CORE、RELATIONS、PRD |
| 设计工具或模型 | SPEC、ARCHITECTURE、DECISIONS |
| 接入或扩展操作 | PRD、SPEC、CONVENTIONS |
| 构建与测试 | CONVENTIONS、ROADMAP |
| 部署与真实系统验证 | DEPLOYMENT、CORE、SPEC |
| 版本与问题回溯 | CHANGELOG、MEMORY |

## 实施纪律

- 硬约束见 CORE 的“绝不”。
- 功能范围完整保留，按平台实际能力逐项实现和验证。
- 工具的实现状态、接口声明和真实运行能力必须一致。
- 具体组织的服务清单、环境、分支和地址留在使用者配置。
- 默认授权、额外确认和平台权限的关系见 SPEC。
- 只有文档的仓库不代表获得真实业务系统的变更授权。
- 执行核心与 MCP 传输入口保持分离。
- 新增能力应覆盖适用对象、参数、结果和不确定状态。
- 中文文档使用中文标点，不使用破折号。
- 日期与时间按 Asia/Shanghai 处理，日期与星期用工具核对。
- 提交、推送和发布分别按用户授权执行，精确暂存本次文件。

## 文档维护

- 产品目标和验收变化更新 PRD。
- 对外工具、schema、错误和授权契约变化更新 SPEC。
- 组件、内部数据流和选型变化更新 ARCHITECTURE。
- 重大取舍追加 DECISIONS。
- 实际部署后更新 DEPLOYMENT。
- 已发生的问题追加 MEMORY。
- 阶段成果追加 CHANGELOG。
- 优先级变化更新 ROADMAP。
- 文件状态变化更新 INDEX。
- 根 README 保持为人阅读的简洁入口。

## 验证与 Git

当前只有文档校验，没有应用测试或构建命令。新增代码时补齐可运行命令，再执行相应验证。

AIREADME 锚点在没有内容提交时使用 pre-code。出现首个获授权的内容提交后，以实际内容 SHA 更新 INDEX，再单独提交 AIREADME；后续继续锚定已审阅的内容提交，不锚定 AIREADME 自身提交，不虚构 SHA 或已发布版本。

## 兼容入口

CLAUDE.md 路由到本文件，两种客户端共用同一套项目文档。
