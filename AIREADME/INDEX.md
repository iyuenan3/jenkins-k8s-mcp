# Jenkins K8s MCP · AIREADME

通用 Jenkins 与 Kubernetes 管理 MCP。生命周期：active；成熟度：首版实现与本地验证完成，真实平台待验收。

last-synced: 499c2e7d72fafe98ee941008d6fe79be1fdc03cb · 2026-10-19

0.1.0 实现已作为内容提交推送至 GitHub 的 main 分支。本轮 AIREADME 以该实际内容 SHA 为同步锚点，单独提交；锚点不指向文档自身提交。当前为源码托管，尚未发布 PyPI 包、release tag 或部署到真实业务环境。

当前本地验证通过 68 项测试，覆盖执行机制、真实 stdio/HTTP 进程及官方 K8s 客户端到模拟平台的 HTTP/WS。GitHub CI 证据见 CHANGELOG，业务系统接入和真实读写验收待进行；运行与验证边界见 DEPLOYMENT 和 CONVENTIONS。

## 状态

| 文件 | 状态 | 摘要 |
| --- | :---: | --- |
| CORE | ✅ | 身份、使命和硬约束 |
| RELATIONS | ✅ | 平台依赖、使用者和私有配置归属 |
| SPEC | ✅ | 37 个真实工具、配置、认证、状态、预算及兼容边界 |
| ARCHITECTURE | ✅ | 已实现组件、数据流和持久化边界 |
| DEPLOYMENT | ✅ | 已验证本地启动；真实业务部署尚无事实 |
| PRD | ✅ | 已确认范围与逐组实现状态，真实验收待进行 |
| ROADMAP | ✅ | 首版验证、真实接入及兼容性推进方向 |
| CONVENTIONS | ✅ | 实际开发、安装、测试和打包命令 |
| DECISIONS | ✅ | 产品取舍、运行栈及执行机制决策 |
| MEMORY | ✅ | 本项目安装、协议输出和资源释放经验 |
| CHANGELOG | ✅ | 立项、文档托管及本地首版里程碑 |

## 按任务读

- 跨项目了解：CORE、RELATIONS、PRD。
- 实现与扩展：PRD、ARCHITECTURE、SPEC、CONVENTIONS、ROADMAP。
- 调整接口和授权：SPEC、CORE、DECISIONS。
- 真实接入与部署：DEPLOYMENT、RELATIONS、CORE。
- 回溯取舍与问题：DECISIONS、MEMORY、CHANGELOG。
