# 运行与部署

2026-10-19 首版已在开发机完成本地安装、stdio 与 HTTP 进程验证，目标为回环地址上的模拟平台。没有实际业务部署、真实 Jenkins/K8s 连接、公开域名或 PyPI 发布；不把本地服务测试记为业务上线。

## 已验证的启动方式

安装入口见根 README。仓库采用 `uv sync --locked --no-editable --python 3.12`，入口程序为 `.venv/bin/jenkins-k8s-mcp`。也可用 pip 在独立 Python 3.12 虚拟环境安装本地构建的 wheel。

```bash
jenkins-k8s-mcp --config /absolute/path/config.local.yaml check-config
jenkins-k8s-mcp --config /absolute/path/config.local.yaml serve --transport stdio
jenkins-k8s-mcp --config /absolute/path/config.local.yaml serve --transport streamable-http
```

check-config 仅检查配置。实际平台访问才加载引用的 Jenkins 凭据或 kubeconfig 认证。示例中的两类连接可以分别使用。

HTTP 默认监听 127.0.0.1:8000，路径 /mcp，启动前设置配置引用的令牌环境变量。远程使用者应单独管理服务运行账号、HTTPS 或隧道与具体 Host/Origin 白名单。当前没有已部署反向代理、容器或共享基础设施。

## 运行状态与备份

默认状态目录为 ~/.local/state/jenkins-k8s-mcp，也可配置独立路径。每个状态目录只允许一个服务进程，stdio 与 HTTP 若同时启动应使用不同目录，并接受操作记录互不共享。

目录保存 operations.sqlite3、SQLite WAL/SHM、fingerprint.key、server.lock 及 discovery 缓存。数据库与指纹密钥限制本机访问，目录及配置由运行者控制。密钥用于请求指纹，不是 MCP 访问令牌。不要只保留数据库而丢弃指纹密钥。

备份时先停止服务，完整保留状态目录和使用者配置，在访问受控的位置保存；不要把它们加入 Git 或分发包。停止服务会失去内存中的待执行载荷和 exec 输出，不能把备份描述为可恢复的远程进程快照。恢复后提交中状态转为 unknown，检查平台回执后再决定下一步。

## 升级与回退

升级前记录当前安装版本、配置和状态目录备份，停止旧服务，再安装经验证的 wheel 并恢复原启动参数。首版没有已完成的生产升级或跨版本迁移记录。

回退使用原安装包及对应状态备份，保留失败版本的目录，不物理丢弃历史。不能靠回退工具包自动回滚 Jenkins Job 或 K8s 资源。

## 待实际部署后补齐

实际主机、网络路由、服务管理方式、真实平台版本、权限账号、客户端和逐项读写验收尚无事实。本项目已有文档和实现不构成对真实业务系统的变更授权。
