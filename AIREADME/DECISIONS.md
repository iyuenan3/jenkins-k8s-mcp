# 决策记录

## ADR-001 · 2026-10-19 · 独立通用产品

Problem：既有单项目运维工具容易依赖固定服务、环境与分支，跨项目复用需要修改代码。

Constraint：用户主要自己跨项目使用，也希望其他人配置自己的连接后直接使用。

Decision：独立立项 Jenkins K8s MCP，slug 为 jenkins-k8s-mcp。以平台原生对象为核心，组织信息移到使用者配置；Apollo 不纳入范围。

Alternatives：继续在既有组织专用目录中叠加规则，不能满足通用接入目标。

Tradeoff：需要重新核验通用对象、配置和能力发现，不能直接把既有工具的运行结果外推到新项目。

## ADR-002 · 2026-10-19 · MCP 作为主要交付

Problem：需要在 MCP 与 Skill 之间确定主要形态。

Constraint：核心交付是可配置连接、可被客户端调用的管理能力，执行结果需要可追踪。

Decision：用户选择 MCP。执行核心独立于传输；Skill 以后可提供工作流经验，但不是必需依赖。

Alternatives：仅以 Skill 组织脚本或 CLI 也能执行操作，但本项目优先提供标准 MCP 工具接口。

Tradeoff：需要维护工具 schema 和协议兼容，操作可靠性仍由底层实现与测试负责。

## ADR-003 · 2026-10-19 · 完整管理范围

Problem：管理能力的深度会决定配置模型、工具粒度和验证范围。

Constraint：用户要求 Job 包含尽可能多的操作，并确认 K8s 范围包含 Secret、通用资源配置和 exec。

Decision：按 PRD 能力目录广覆盖，逐项标记平台依赖、实现和验证状态，不沿用原工具的组织白名单或非敏感字段限制。

Alternatives：把新项目收缩为只读观察或固定部署目录，不能满足已确认需求。

Tradeoff：不同 Job 类型、插件、资源和客户端存在差异，需要分阶段实现并明确不支持或未验证的情况。

## ADR-004 · 2026-10-19 · 沿用客户端审批

Problem：固定的第二轮确认口令会重复客户端已有的授权交互。

Constraint：用户接受沿用客户端审批、按连接可开启额外预览确认的建议。

Decision：默认不额外要求复制确认口令，保留精确预览与固定计划执行能力；身份、平台权限、连接策略与客户端审批分别处理，契约见 SPEC。

Alternatives：所有写操作强制再走相同口令确认，会增加已授权操作的重复交互。

Tradeoff：需要在实际客户端测试审批边界，服务端不能从普通工具参数推断人类身份。

## ADR-005 · 2026-10-19 · 两种运行入口

Problem：本机与内网主机访问目标系统的网络条件不同。

Constraint：用户接受本地 stdio 和远程 HTTP 共用一套实现的建议。

Decision：每个使用者独立部署，支持 stdio 与 Streamable HTTP，优先完善个人安装和配置体验。HTTP 访问边界见 SPEC。

Alternatives：只支持一种运行位置，会限制跨项目与不同网络条件下的使用。

Tradeoff：需同时验证两种入口的生命周期、认证和一致结果，不因此建立多租户管理平台。

## ADR-006 · 2026-10-19 · 文档立项与实现分开

Problem：立项需要把已确认取舍保留下来，便于新项目接手。

Constraint：用户本轮要求立项，此前明确先讨论整体设计；尚无真实目标、部署位置或兼容性验证。

Decision：本轮创建 README、共用 AI 入口、AIREADME 和 Git 初始化。当前全部能力标明未实现；工程默认值与待验证项单独记录。

Alternatives：预先生成源码、部署配置和未验证命令，会把设计当成已交付能力。

Tradeoff：立项产物可以直接接手，但应用测试、构建、运行和发布需在后续实现中完成。


## ADR-007 · 2026-10-19 · 首版运行栈与两种入口

Problem：立项方向需要成为可安装、可测试的 MCP，而非工具声明集合。

Constraint：Python 执行核心与传输分离，平台独立启用，配置无组织绑定。

Decision：使用 Python 3.12、官方 MCP Python SDK 1.29.0、httpx、官方 kubernetes 动态客户端、Pydantic、lxml 和 uvicorn。uv.lock 固定本轮实际验证的依赖。HTTP 与 stdio 共用 Engine，HTTP 使用静态 Bearer 和独立 Host/Origin 校验。

Alternatives：手写 MCP 协议会增加协议维护；直接复制既有专用项目会继承业务边界；直接跨到未经本项目验证的 SDK 主版本不提供额外验收证据。

Tradeoff：SDK 1.29.0 是本轮验证选择，不声称最新版本。需要完成其 Settings 的前向类型定义以兼容当前 Pydantic settings 检查。OAuth 和更多客户端需另做真实兼容验证。

## ADR-008 · 2026-10-19 · 元数据持久化与短期载荷

Problem：长操作需要恢复和去重，但持久保存 XML、Secret、argv 或日志会扩大敏感数据暴露。

Constraint：不能丢弃操作历史，不能因响应丢失重复写入，不能用模型参数充当可信身份。

Decision：SQLite 保存目标、前置版本、HMAC 请求指纹、状态及原生回执，原始计划和输出仅放有限的进程内存。事务认领先于副作用，同一状态目录只允许一个服务。额外确认通过独立 OS 管理员 CLI 写入批准位，MCP 无批准工具。

Alternatives：持久化完整请求便于重启后继续执行，但违背敏感载荷边界；仅内存记录不能支持去重恢复；依赖 actor_id 或确认文字不能证明人类授权。

Tradeoff：重启后计划和 exec 输出丢失，提交边界保留 unknown，需检查平台后再决定下一步；不提供跨平台 exactly-once 承诺。

## ADR-009 · 2026-10-19 · 原生条件与精确结果归属

Problem：API 接受、平台执行和业务成功是不同证据，并发操作者可能改变同名对象。

Constraint：不能借用 latest 构建或后续控制器更新，平台本身没有统一事务。

Decision：Jenkins 固定 Queue 和 Build，配置检查尽力进行；构建缺少 config.xml 读取权限时返回较弱的 Job 元数据签名检查。K8s 先 dry-run，再使用 UID/resourceVersion 条件。不存在对象的 apply 使用原子 POST 创建，避免 SSA 在竞态中覆盖别人的新对象。exec 使用独立客户端、明确 argv 和有限输出，只有取得退出状态才确认远端结束。

Alternatives：自动重试写入、以最新对象状态推断本次操作，或无条件 SSA 会掩盖未知状态与竞态；把 exec 关闭描述为 kill 无平台证据。

Tradeoff：Jenkins 与 exec 检查至提交仍存在原生 API 无法消除的竞态。部分插件、参数和控制器策略必须通过实际平台验收，不从模拟结果外推。
