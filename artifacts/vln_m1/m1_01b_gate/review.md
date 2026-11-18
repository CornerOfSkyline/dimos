# VLN-M1-01B-GATE 审查（review.md）

分支 `task/vln-m1-01b-agent-message-lifecycle` · GATE 提交 `9f91a8c`
只读审查 + 最小修复（调用关联加固）；不进 M1-02。

---

## 一、实现审查结论（§一 7 项，源码定位）

| 项 | 结论 |
|---|---|
| `Agent._on_tool_stream_message`（agent.py:231-） | P1 分流正确：进度帧 → `agent.publish(AIMessage(kind=TOOL_PROGRESS))`，不入 LLM 队列；`tool_stopped` 帧被忽略（专属 McpServer） |
| `Agent._thread_loop`（agent.py:493-501） | 单线程循环未改（框架属性）；`with self._lock` 包住 `_process_message` |
| `Agent._process_message`（agent.py:575-） | GATE 后为双重门控：**调用存活性（主）** → 年龄（辅）；`_open_agent_turn`/`_close_agent_turn`(finally) 界定回合 |
| `_append_image_to_history`（agent.py:1500-） | 携带 `TOOL_ORIGIN_KEY` + `GENERATED_AT_KEY` + **`INVOCATION_STARTED_KEY`**（GATE 新增） |
| 消息类型与时间戳 | `mcp/constants.py`：三分类常量 + `TOOL_RESULT_STALE_AFTER_S=15`（降为辅助）+ `INVOCATION_STARTED_KEY` |
| ToolStream/Agent 展示通道 | `Chat` 面板 `messages` 通道 = `Agent.agent`（cockpit.py:572，`chat.json.v1`、paced 20Hz、"every message must reach the viewer"） |
| VLN run_id 与终态 | `vln_task.py` 状态机未触碰；`get_vln_task_status` 正常 |

**核心问题的回答：修复前 15 秒是唯一条件吗？**——**是**。这正是 GATE 加固的缺口：阈值内的“已关闭任务回合格产物”仍能到达 LLM。现主条件为**调用存活性**：产物所属的工具调用必须属于**仍打开的 Agent 回合**；年龄降为辅助检查（活调用中排队超 15s 的仍按陈旧丢弃）。生产事故里那张 13 秒旧图（<15s）在新门控下被**主条件**拦截。

## 二、工具结果来源关联（§二，实现说明）

- **复用既有标识**：artefact 的 UUID 文本保留（UI 关联）；关联键为**工具名+调用时刻**（`_begin_tool_invocation` 记账，`call_tool` 在 MCP 请求**前**打点）。per-UUID 追踪需 McpServer 侧改造（进度 token 透传）——超出最小修复范围，已记录为候选增强；工具名级记账与 CapabilityRegistry 的同工具 takeover 语义对齐（新调用的时间戳自动淘汰旧产物）。
- **回合生命周期**：`_process_message` 开回合（新活调用集）→ 图流（工具在其中执行、入记账）→ **finally 关回合**（本回合全部调用失效）。此后任何引用本回合调用的产物=遗留物，**无论多新都被丢弃**。
- **死锁教训（过程发现）**：初版用 `self._lock` 记账——langgraph 在**执行器线程**跑工具，与循环线程的锁互等死锁（`test_a_tool_call_the_mcp_server_never_answers` 抓到）。改为**专用 `_invocation_lock`**。
- **不动的部分**：LangGraph 未重写；USER_COMMAND 永不拦截（显式重复=新 run_id，合法）；M1-01 runtime/能力语义零改动。

## 三、实时进度显示链验证（§四，四层区分）

| 层 | 结论 | 证据 |
|---|---|---|
| 后端发布 | ✅ `agent.publish(AIMessage)` | 代码路径 + T02（阻塞期间实时到展示流）；真实复测会话的 `Capabilities released`/`agent` 流在跑 |
| relay 转发 | ✅ `Chat._channels()`（cockpit.py:597-600）声明 `messages=agent` 通道 paced 20Hz；`relay_bridge` 按通道订阅转发；`test_relay_bridge_authoring.py` 40 项（含 agent 通道 paced/顺序/有界队列断言）全过 | 代码 + 40 项回归 |
| 浏览器收到 | ✅（代码链）`chat.json.v1` 帧逐条到 viewer；codec `chat_lines` 把 `[tool:NAME] text` 渲染为 role=tool 行 | `chat_codec.py:45-` + `test_chat_encoding.py` |
| **UI 实际渲染** | ⚠️ **代码链四层可证，但"人眼看到"需用户确认**——上次真实复测中 cockpit 聊天面板应已显示 `turn N/50` 行（聊天流即渲染面，无新 UI 开发）；待用户在 cockpit 确认 | **待确认项**（已在前次复测后请求用户确认，本轮再次列入） |

结构化 `vln` 字段（policy/turn/max/stage）随 BaseMessage JSON 在线上存在，供后续 UI 增强；当前文本行即用户可见进度。**未修改前端**（按约束：能用则不动）。

## 四、GATE 缺陷清单（修复过程中发现并处理）

1. 15s 阈值独木桥（本 GATE 主题）→ 主条件改调用存活性（S1-S3 直接证明 1s/5s/13s 闭回合产物全拦截）。
2. P1 重放时 `_vln_progress_telemetry` 被意外重复定义（mypy no-redef 抓到）→ 已清除。
3. 记账死锁（执行器线程 vs 循环锁）→ 专用锁。
4. `chat_codec.py` 顶部注释仍是 M1-01B 前语义 → 更新（纯注释，零行为改动）。
