# 实现经验入口

当前尚无本项目运行事故或经过复现的实现踩坑。此文件保留后续“现象、根因、结论或避免方式”的追加记录。

设计阶段的假设与风险在 PRD，已确认的取舍在 DECISIONS；不把外部项目的历史事故记成本项目已经发生的问题。


## 2026-10-19 · macOS 编辑安装的路径文件未被执行

现象：本项目首次 uv 编辑安装完成后，包无法导入；路径和 .pth 内容正确，普通读取成功。实查 .pth 带 UF_HIDDEN，清除后在后续调用中再次出现。

根因：本机 Python site 跳过隐藏 .pth。隐藏标志为何再次出现尚未确认，不能直接归因于 uv 或项目源码。

结论：使用 `uv sync --no-editable`，普通安装在本机可用；pytest 明确使用 src，协议子进程在源码变化后重新普通安装。独立 wheel 环境从工作区外导入，验证分发结果，不能用测试路径注入冒充安装成功。

## 2026-10-19 · MCP 返回注解决定结构化 schema

现象：真实 stdio 与 HTTP 调用均返回成功文本，但 structuredContent 为 None，未生成输出 object schema。

根因：SDK 1.29.0 未把裸 dict 返回注解识别为所需的结构化输出。

结论：工具使用 dict[str, Any] 并显式 structured_output=True；通过 SDK 客户端验证 tools/list 的 outputSchema 和实际 structuredContent，不能仅检查 Python 函数返回值。

## 2026-10-19 · exec 正常关闭后仍遗留 socket

现象：严格警告回归在真实本地 WebSocket exec 结束后报告 unclosed socket；功能断言本身通过。

根因：当前 websocket-client 在 connected 已为 false 时，close 可提前返回。接收到对端关闭帧后仍需显式释放底层 socket。

结论：exec 收尾调用 WebSocket shutdown，专用 ApiClient 同时关闭。修复后对应传输测试及完整 `pytest -q -W error` 均通过，不用屏蔽 ResourceWarning 获取绿色结果。
