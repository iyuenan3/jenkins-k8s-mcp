# 依赖关系

## 出向

| 依赖 | 用途 | 归属 |
| --- | --- | --- |
| Jenkins Remote Access API | Job、构建、队列、日志及平台可用扩展 | 使用者自己的 Jenkins |
| Kubernetes API | 资源发现、查询、变更、日志、事件、指标和 exec | 使用者自己的集群 |
| MCP 协议与运行库 | 向兼容客户端提供工具 | 产品依赖，实现时核验版本 |
| 本地配置与凭据来源 | 连接地址、认证、context 和可选策略 | 使用者管理，仓库不包含真实值 |

插件、metrics API、认证插件等均按连接的实际能力发现和报告，不假设所有目标一致。

## 入向

支持 MCP 的 AI 客户端。客户端负责自然语言理解、推理和其自身的审批交互；本项目负责可验证的查询与执行能力。

可选 Skill 可在以后承载工作流经验，当前不作为使用本产品的必要依赖。

## 共享资产

当前没有跨项目运行依赖、共享主机或公共凭据。Jenkins 与 K8s 之间的业务关联由使用者可选配置，不要求一项 Jenkins 构建必然对应某个 K8s workload。

既有私有实现只作为理解需求和接口行为的参考。通用代码的迁入须逐段核验，产品文档与样例保持独立、可分发。

## 原始参考

- [Jenkins Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/)
- [Kubernetes API 与资源发现](https://kubernetes.io/docs/concepts/overview/kubernetes-api/)
- [Kubernetes API Concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/)
- [MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

参考页用于追溯设计来源。编码时按实际协议、依赖和服务端版本复核，不能由文档链接推断兼容性已经通过。
