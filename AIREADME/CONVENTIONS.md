# 工程约定

## 身份与命名

项目 slug 固定为 jenkins-k8s-mcp。Python 包为 jenkins_k8s_mcp。代码、工具和模块围绕平台对象与动作，避免组织名、项目名和环境缩写。

工具注册与类型标注产生 JSON schema，catalog 只描述真实工具与依赖。修改 schema 后检查 tools/list 的实际结果。MCP 工具输出使用明确 object 类型标注和 structured_output，避免退化为仅文本。

## 实现与数据纪律

执行核心和 MCP 入口分离。连接及请求不可变地绑定，异步请求不共享可变的全局目标。Jenkins 适配器使用原生 HTTP/XML，K8s 使用独立的官方客户端配置与动态 discovery。

平台 IO 设置超时和预算。查询使用可续读结果，原生日志游标不能越过未交付字节。stdio stdout 只放协议输出，日志使用 stderr，不记录请求、输出或平台原始异常体。

写入只在统一执行路径执行一次。计划认领先于副作用，结果未知时禁止隐式重提。新增平台动作必须说明适用类型、参数、前置条件、原生回执、收敛依据和无法判断的状态。

未知插件配置应保留，原生权限或扩展不支持应如实返回。脱敏不能保证任意文本安全，所有原始载荷与输出都不能进入持久审计。

## 可运行验证

开发与 CI 使用 Python 3.12、uv.lock 和普通安装。修改源码后重新安装，确保协议子进程使用当前代码；普通 pytest 通过 pythonpath 使用 src。

```bash
uv sync --locked --no-editable --python 3.12
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q -W error
uv build
```

- test_execution：目标、认领、身份、去重、恢复、状态和持久数据边界。
- test_jenkins：真实端点契约、类型和参数、配置冲突、日志续读及原生回执。
- test_kubernetes：动态资源、版本条件、dry-run、敏感对象、策略、exec 和不确定状态。
- test_protocols：真实 SDK、loopback HTTP/WS 模拟平台、stdio/HTTP 子进程、认证及生命周期。
- test_security：命名敏感值与非敏感配置的摘要边界。

协议测试需要回环监听权限，无业务平台凭据，也不连接真实业务资源。全部通过只能证明对应机制与本地协议，不代表真实 Job 插件、集群版本或业务验收。新增测试应验证实际边界，避免复写实现。

.github/workflows/tests.yml 定义 Ubuntu 和 macOS 的 Python 3.12 测试、lint 与打包。提交前按本机命令验证，远程 CI 只有实际运行后才记录通过。

文档校验：在项目根运行 `bash ~/.agents/skills/aireadme/scripts/check.sh`，漂移检查追加 `--drift`。此脚本为协作者本机工具，不是应用运行依赖。

## 文档与历史

中文使用中文标点，不使用破折号；日期使用 Asia/Shanghai。

产品目标、接口、结构、部署事实和决策各按 INDEX 职责维护。DECISIONS、MEMORY、CHANGELOG 追加新记录，不覆盖历史结论。根 README 保持为安装和使用入口。

用户授权提交后精确暂存文件。内容提交和后续 AIREADME 锚点提交分开，保留其他未提交改动。推送和发布需要各自明确授权。
