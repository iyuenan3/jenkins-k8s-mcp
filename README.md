# Jenkins K8s MCP

[![tests](https://github.com/iyuenan3/jenkins-k8s-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/iyuenan3/jenkins-k8s-mcp/actions/workflows/tests.yml)

通用的 Jenkins 与 Kubernetes 管理 MCP。每个使用者配置自己的连接，支持本地 stdio 和远程 Streamable HTTP。

首版已实现，版本号为 `0.1.0`。68 项本地测试覆盖执行核心、真实 MCP 进程、官方 Kubernetes 客户端和模拟平台，CI 在 Ubuntu 与 macOS 上验证。真实业务系统验收尚未进行，也未发布到 PyPI。

## 能力

- Jenkins：Folder 与 Job 查询、创建、复制、重命名、移动、配置、启停和删除；内联或 GitSCM Pipeline、多分支扫描；参数化构建、队列控制、精确构建跟踪、增量日志、产物及报告。
- Kubernetes：动态发现内建 API 与 CRD，通用创建、更新、patch、apply 和删除；重启、扩缩容、状态与历史；ConfigMap、Secret、环境变量、镜像与资源配置；事件、日志、指标、端点及非交互 exec。
- 操作管理：多连接、固定目标预览、可选额外确认、请求去重、操作记录和结果核验。写入响应丢失保留结果未知，不自动重复提交。

两个平台可独立启用。全部启用时注册 37 个工具，实际清单及 JSON schema 通过 `tools` 命令或 MCP `tools/list` 获取。插件、资源及权限差异以实际平台响应为准。

## 安装与配置

需要 Python 3.12 或更高版本，当前验证版本为 Python 3.12。在仓库根目录执行：

```bash
uv sync --locked --no-editable --python 3.12
cp config.example.yaml config.local.yaml
```

编辑 [配置示例](config.example.yaml) 的本地副本，填入 Jenkins 地址、凭据环境变量名称，以及 kubeconfig 路径和明确的 context。只使用一个平台时删除另一个连接即可。Jenkins 的用户名和 API token 放在启动进程的环境变量或本机凭据文件中，真实配置不提交到 Git。

```bash
.venv/bin/jenkins-k8s-mcp --config config.local.yaml check-config
.venv/bin/jenkins-k8s-mcp --config config.local.yaml tools
```

以上命令不连接业务平台。`tools` 使用配置的状态目录，同一目录只允许一个服务进程；在线服务也可直接调用 `server_info`。

## MCP 客户端

支持 stdio 的客户端可按其配置格式填写如下内容。路径改为本机绝对路径，并把凭据配置到客户端启动环境。

```json
{
  "mcpServers": {
    "jenkins-k8s": {
      "command": "/absolute/path/jenkins-k8s-mcp/.venv/bin/jenkins-k8s-mcp",
      "args": ["--config", "/absolute/path/config.local.yaml", "serve", "--transport", "stdio"]
    }
  }
}
```

HTTP 入口在设置配置所引用的 `MCP_AUTH_TOKEN` 后启动，令牌至少 16 字节：

```bash
.venv/bin/jenkins-k8s-mcp --config config.local.yaml serve --transport streamable-http
```

默认地址为 `http://127.0.0.1:8000/mcp`。客户端需要支持 Streamable HTTP，并发送 `Authorization: Bearer <令牌>`。远程入口应使用 HTTPS 或受控隧道，并明确配置 `allowed_hosts`、适用时配置 `allowed_origins`。当前使用静态令牌，不提供 OAuth 自动发现。

## 操作与边界

从 `connections_list`、`connection_check` 和平台查询工具开始。写操作可先传 `preview_only: true`，核对返回的目标、参数与版本条件，再用 `operation_execute` 消费固定计划。默认直接执行当前已授权请求；连接开启 `extra_confirmation` 后，需由本机管理员核对预览与计划哈希，再运行：

```bash
.venv/bin/jenkins-k8s-mcp --config config.local.yaml approve <operation_id> --yes
```

通过 `operation_get`、`operation_wait` 和 `operation_output` 续查本次操作。平台受理、资源收敛与业务验收分别表达。计划和输出短期保存在内存，长期记录只保存目标、版本、状态及原生回执；服务重启不会重放未完成写入。

exec 支持明确的 argv、Pod 和容器，当前不支持交互 stdin 或 TTY。关闭通道或等待超时不代表远端进程已停止。Jenkins 文件参数及特殊插件参数需要额外适配；重复构建使用当前配置及可读的历史参数，不保证重现原始 Git 提交。

完整配置、工具、输出限制和错误语义见 [SPEC](AIREADME/SPEC.md)，安装、状态备份与运行边界见 [DEPLOYMENT](AIREADME/DEPLOYMENT.md)。

## 开发

```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q -W error
uv build
```

协议测试只使用回环地址上的模拟服务，不需要业务凭据。修改源码后，用 `uv sync --locked --no-editable` 刷新安装，再运行子进程测试。AI 协作者从 [AGENTS.md](AGENTS.md) 和 [AIREADME](AIREADME/INDEX.md) 接手。

公开仓库：[iyuenan3/jenkins-k8s-mcp](https://github.com/iyuenan3/jenkins-k8s-mcp)。许可证尚未确定。
