# 对外契约

适用版本：0.1.0。工具和输入、输出 JSON schema 的唯一来源为实际 MCP 注册，使用 `tools` CLI 或 `tools/list` 获取。已验证 stdio、Streamable HTTP 和本地模拟平台，真实业务平台兼容性仍待逐项验收。

## 连接与配置

配置文件为 YAML，使用全局参数 `--config PATH`，或环境变量 `JENKINS_K8S_MCP_CONFIG`。未知字段拒绝，未提供配置时不启用平台模块。完整示例见根目录 `config.example.yaml`，字段模型见 `src/jenkins_k8s_mcp/config.py`。

| 配置 | 契约 |
| --- | --- |
| connections[].id / kind | 唯一稳定别名；kind 为 jenkins 或 kubernetes |
| enabled / default / labels | 独立启用、每个平台至多一个默认连接、可选使用者标签 |
| url | Jenkins HTTP(S) 根地址，可含部署前缀；不接受内嵌账号、密码、query 或 fragment |
| username / username_env | Jenkins 用户名或其环境变量引用 |
| token_env / token_file | Jenkins API token 的环境变量或文件引用，不接受内联 token |
| kubeconfig / context | 明确指定文件和 context；另一种模式为 in_cluster: true |
| namespace | namespaced 资源的可选默认 namespace，集群级资源不接受 namespace |
| verify_tls / trust_env | Jenkins TLS 校验和环境代理策略；默认校验 TLS，不使用环境代理；K8s TLS 与认证由所选 kubeconfig 控制 |
| timeout_seconds / max_concurrency | 单连接请求期限和并发上限，默认 30 秒、4 个 |
| extra_confirmation | 默认 false；启用后需要独立的本机管理员确认 |
| state_dir | 默认 ~/.local/state/jenkins-k8s-mcp；同一目录只允许一个服务进程 |
| plan_ttl_seconds | 计划及完成 exec 输出保留期，默认 300 秒，范围 1 至 3600 |
| max_output_bytes / max_items | 默认输出预算 65536 字节、集合请求预算 200 项；工具和平台仍有各自上限 |
| http | host、port、token_env、principal、allowed_hosts、allowed_origins |

连接选择顺序为显式 connection_id、该平台配置的默认连接、唯一启用连接。多目标歧义返回 TARGET_AMBIGUOUS 和候选别名。凭据按首次实际平台访问加载，配置检查不会连接平台。

Jenkins 对象使用连接、完整 Folder/Job 路径、Queue ID、正整数 Build number。K8s 使用 api_version、kind、namespace、name，变更固定实际 UID 和 resourceVersion。禁止通过切换全局 current-context 选择目标。

## 协议与身份

- stdio：标准 MCP stdin/stdout，进程日志使用 stderr。传输身份为本机进程边界下的 local。
- Streamable HTTP：默认 `http://127.0.0.1:8000/mcp`，stateless 会话，JSON 响应。每次请求校验静态 Bearer，令牌从 http.token_env 读取，至少 16 字节；身份为服务端配置的 http.principal，默认 owner。
- HTTP 每次校验 Host；存在 Origin 时必须与 allowed_origins 中的完整来源匹配。默认不允许浏览器来源。远程入口由部署者配置 HTTPS 或受控隧道及实际主机白名单。
- 无令牌或令牌错误返回 HTTP 401，含 WWW-Authenticate；来源或主机不符返回 403。当前不提供 OAuth discovery，也不承诺只能通过 OAuth 接入的客户端兼容。
- 平台原生权限仍为最终上限，discovery 和连接成功不代表获得写权限。annotations 是客户端提示，不是服务端授权机制。

## 实际工具

两个平台均启用时共 37 个；禁用平台不注册对应工具。所有工具返回 object schema，并提供 structuredContent；工具错误标记 isError。完整参数以运行时 schema 为准。

| 分组 | 工具 |
| --- | --- |
| 连接与服务（3） | server_info、connections_list、connection_check |
| 操作与输出（7） | operation_execute、operation_get、operation_list、operation_wait、operation_output、operation_cancel、output_read |
| Jenkins Job（5） | jenkins_jobs_list、jenkins_jobs_search、jenkins_job_get、jenkins_job_config、jenkins_job_change |
| Jenkins Pipeline（2） | jenkins_pipeline_change、jenkins_multibranch_scan |
| Jenkins 构建与证据（8） | jenkins_build_start、jenkins_build_repeat、jenkins_build_get、jenkins_queue_get、jenkins_queue_cancel、jenkins_build_stop、jenkins_build_log、jenkins_build_reports |
| K8s 发现与对象（5） | k8s_discover、k8s_resources_list、k8s_resource_get、k8s_secret_get、k8s_resource_change |
| K8s 工作负载（3） | k8s_workload_restart、k8s_workload_scale、k8s_rollout_history |
| K8s 排障与执行（4） | k8s_pod_logs、k8s_events、k8s_diagnose、k8s_exec |

server_info 返回应用版本、源码内容哈希 content_id、实际注册工具、能力组、依赖和验证边界。能力目录的 implemented 表示本地实现存在，platform_verification 仍要求目标平台验收。

## 写入与固定计划

写操作支持 preview_only 和可选 request_id。默认生成计划后立即在当前请求中执行；preview_only 为 true 时返回计划，使用 operation_execute 执行。extra_confirmation 启用后始终先返回计划，由独立 OS 管理员核对 MCP 预览、目标和 plan_hash，再执行 `jenkins-k8s-mcp --config CONFIG approve OPERATION_ID --yes`。

普通工具参数不能更改传输身份。operation_id、actor_id 或确认文字不作为认证。计划属于产生它的传输身份，只能消费一次，实际载荷固定在服务进程内存中。

预览返回完整目标、相关版本和参数。Jenkins 包含配置摘要、XML 差异或构建参数，默认参数留给平台解析，Password 默认值不回填提交；K8s 包含脱敏的变更前对象和服务端 dry-run 结果。exec 返回精确 argv、容器和超时，不能 dry-run。

同一身份的 request_id 与完整连接配置、动作及载荷绑定。重复请求返回已有操作，参数变化返回 REQUEST_CONFLICT。无 request_id 的两个独立请求可能执行两次。此机制不承诺跨平台 exactly-once，也不在 unknown 时自动重放。

Jenkins 在提交前尽力复核 config.xml 哈希；构建、停止或扫描遇到 config.xml 权限拒绝时，改用 Job 类型、参数及可构建状态签名，明确返回较弱的元数据检查。Jenkins API 没有统一的原子配置条件，检查与写入之间仍有竞态。

K8s 所有资源变更先 dry-run=All，再按同一固定请求提交。已有对象使用 UID/resourceVersion 条件；JSON patch 前置 test，删除使用 DeleteOptions preconditions。apply 已有对象使用 SSA，force_conflicts 默认 false；对象不存在时使用 POST 创建，以原子 AlreadyExists 拒绝同名竞态，不覆盖后来出现的对象。API 缺少所需 verb、版本条件或 dry-run 时明确拒绝，不降级为无条件写入。

## 结果、状态与恢复

操作结果包含 operation_id、connection_id、action、target、preconditions、plan_hash、status、Asia/Shanghai 时间、native 回执、verification、error。执行可能额外返回即时对象结果。

状态为 planned、submitting、accepted、running、succeeded、failed、partial、cancelled、expired、unknown。终态不被迟到的运行中观察覆盖。operation_wait 最多等待 30 秒，wait_timeout 只是等待标记，不把后台操作改为失败。

Jenkins 从本次 Queue ID 获取可归属的 Build，校验 Job 路径和 queueId，不查询 latest 代替本次构建。Queue 过期或回执错配保留 unknown。停止仅针对仍运行的明确 Build；扫描仅报告已请求，不推断扫描完成。

K8s 重启标记、原始 UID、目标 generation 和副本数用于判断本次更新是否被替代。Deployment、StatefulSet、DaemonSet 等按各自状态检查；缩容到零有效。StatefulSet 的 OnDelete 或 partition 不能冒称全部重启完成。写入完成、资源收敛和 business: not_checked 分开表达。

服务重启后，计划失效，submitting 转为 unknown，已保存的原生回执可继续查询。原始载荷和 exec 会话不恢复；不会自动重提写入。取消排队、停止 Build 或关闭 exec 通道是独立有副作用操作，同样经过连接策略，不等价于回滚资源变更。

## 输出、敏感内容与预算

查询结果受预算约束。Job、资源、操作和 discovery 使用相应分页或截断标记。Jenkins 日志使用原生字节游标，服务端截断仅推进已返回部分，保留 UTF-8 边界。K8s 日志使用时间戳和 next_since_time，续读有明确重叠，极端单行超预算时需要调整读取范围，不能承诺无损游标。

大 JSON 结果转成身份绑定的短期快照，output_read 返回 UTF-8 chunk、字节游标和结束标记，拼接后才是完整 JSON。快照保留 60 秒，单个最大 8 MiB，总额 16 MiB、至多 32 份；超额淘汰旧快照。变更输入最大 1 MiB，待执行计划至多 128 个。

长期 SQLite 仅保存必要目标、版本、HMAC 请求指纹、状态及回执，不存参数值、XML、脚本、manifest、差异、argv 或输出。原始计划和预览只在内存保留到执行或过期，完成的 exec 输出按配置 TTL 清理。Jenkins 构建日志按调用从平台读取，不本地持久化。

默认对象摘要隐藏 Secret data/stringData、敏感字段和命名敏感值。脱敏是尽力匹配，不能识别任意业务文本中的全部秘密，因此任何原始输出都不进入长期日志或审计。显式 config.xml、构建证据、日志、exec 和 k8s_secret_get 可返回敏感内容并标记 sensitive_output。Secret 值保持平台的 base64 表达，不自动解码。

exec 支持非交互 argv、独立 stdout/stderr、字节游标、有限输出、1 至 600 秒期限和退出码。执行前检查 Pod UID 与容器 ID，API 仍不提供从检查到 exec 建连的原子 UID 条件。关闭流、超时或断线返回 unknown，remote_termination_confirmed 仅在取得退出状态时为 true，不能冒称远端进程被杀死。当前不支持 stdin/TTY。

## 错误与平台差异

工具错误使用 error.code、message 和可选安全 details；平台原始响应体不回显。主要区分 CONFIG_INVALID、CREDENTIAL_UNAVAILABLE、AUTHENTICATION_FAILED、FORBIDDEN、CONNECTION_FAILED、NOT_FOUND、TARGET_INVALID、TARGET_AMBIGUOUS、TARGET_MISMATCH、UNSUPPORTED、UNSUPPORTED_PARAMETER、CONFLICT、VALIDATION_FAILED、WAIT_TIMEOUT、CURSOR_EXPIRED、OUTPUT_LIMIT、PLAN_EXPIRED、PLAN_CONSUMED、CONFIRMATION_REQUIRED、REQUEST_CONFLICT 和 RESULT_UNKNOWN。

计划前失败返回工具错误；已建立操作后的执行失败写入该操作状态。可能已提交但未取得可归属响应时为 unknown；读失败或权限缺口不外推写入失败。

Jenkins 结构化 Pipeline 编辑支持 workflow-job 的内联与单远程、单分支 GitSCM；其他类型使用显式原生 XML。文件及特殊插件参数返回不支持。移动依赖 Folder relocation，报告依赖相应插件，未在真实插件矩阵验收。重复构建使用当前配置和可读的历史标量参数，不保证原始 Git 提交或不可读凭据重现。

配置示例不绑定任何组织服务清单。当前仅在 Python 3.12 验证锁定依赖；不把一组模拟平台测试外推为所有 Jenkins 插件、K8s 版本或 MCP 客户端均已兼容。
