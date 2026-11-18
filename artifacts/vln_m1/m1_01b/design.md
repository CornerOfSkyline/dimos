# VLN-M1-01B 设计 — Agent 异步消息时序与重复执行修复

分支 `task/vln-m1-01b-agent-message-lifecycle`（自 M1-01 HEAD `bb28ee0`）
提交：`16a61b3`（P0）+ `f5f1a21`（P1）。根因见 root_cause.md。

## 一、消息分类模型（P0 基石）

外部注入 Agent 的消息分三类（`mcp/constants.py`，`MESSAGE_KIND_KEY` 载于
`HumanMessage.additional_kwargs`）：

| 类别 | 来源 | 处理 |
|---|---|---|
| `USER_COMMAND` | 人类输入（`_on_human_input`） | **永不拦截**——显式重复指令合法（新 run_id，绝无指令字符串判重） |
| `TOOL_RESULT` | 工具产物（`_append_image_to_history`，带 `GENERATED_AT_KEY`） | **消费时新鲜度门控**：>15s 陈旧 → 不入 LLM（丢弃+日志+cockpit 可见说明）；新鲜 → 照常进入 |
| `TOOL_PROGRESS` | 运行遥测（ToolStream 帧） | **永不入 LLM 队列**：分流到 cockpit 展示通道（P1） |

新鲜度判定在**消费侧**而非入队侧（入队时无法预知消费时刻——阻塞回合的延迟只有在取出时才可测量），这是本设计与"入队即判"方案的关键差异。

## 二、P0：重复执行防护（提交 16a61b3）

1. **来源关联**：每个 artefact 记录 origin 工具（`TOOL_ORIGIN_KEY`，既有）、消息类别、生成时刻（新）。（run_id 关联未加：图像类产物由 MCP 通用机制注入，Agent 侧无 per-run 概念，属技能/服务器层——记录为后续可选增强，非本缺陷必需。）
2. **防误触发**：`_process_message` 开头门控——陈旧 TOOL_RESULT 不进入 LLM 回合（根因链上"任务完成+旧图像=重复执行"的第一现场直接切断）。丢弃原因 `logger.info` 记录，并向 cockpit 发布说明帧。
3. **同指令重复保护**：防护只作用于 TOOL_RESULT/TOOL_PROGRESS，**不碰 USER_COMMAND**——用户第二次显式发送相同指令 → 全新合法 run（T07）。技能层 M1-01 的一次一实例语义原样保留（T12）。
4. **能力与状态语义**：零改动（不锁死运动能力；拒绝逻辑仅在 Agent 消息层）。

## 三、P1：进度分流（提交 f5f1a21）

`_on_tool_stream_message` 重写：
- 旧：`add_message(HumanMessage("[tool:...] turn N"))` → 入 LLM 队列（每帧一次推理）。
- 新：`agent.publish(AIMessage(..., additional_kwargs={kind: TOOL_PROGRESS, vln: {...}}))` → cockpit 现有聊天/展示通道，零 LLM 成本。
- VLN 结构化字段：`_vln_progress_telemetry` 解析技能 `tool_update` 文本（"turn 3/50: 4 actions"）→ `{policy, current_turn, max_turns, actions, stage}`。终态经 `get_vln_task_status` 查询（M1-01 既有），不在本层重复推送（避免终态又触发 LLM 推理循环——用户下一句话自然带出）。
- MCP 线上格式零改动（progressToken 帧原样，外 clients 无感知，T11）；`tool_stopped` 帧仍专属于 McpServer 能力释放路径。

## 四、修改前后消息流向

**修复前（根因路径）**：
```
VLN turn N → tool_update → ToolStream → Agent 入队 HumanMessage×12（阻塞中积压）
任务结束 → 12 帧逐个触发 LLM×12（"正在走…"） → observe artefact（13s 旧图）
作为唯一新输入 → LLM 误判 → 第二次 active_vln（53s 重复运动）
```

**修复后**：
```
VLN turn N → tool_update → ToolStream → agent.publish（cockpit 实时结构化进度，0 LLM）
任务结束 → 无积压帧、无额外推理
observe artefact 新鲜(<15s) → 正常进入 LLM（视觉能力不回归）
observe artefact 陈旧(>15s) → 丢弃（日志+可见说明），模型不再看到旧视图
用户显式重复指令 → USER_COMMAND 永拦不住 → 新 run_id 合法启动
```

## 五、测试映射（T01–T12，全过）

T01 进度 0 LLM/10 帧结构化；T02 阻塞中 cockpit 实时收到 5 帧；T03 事后积压进度零任务；T04 新鲜图像照常可用；T05/T06 陈旧图像丢弃+不触发；T07 显式重复合法；T08 停止后产物惰性；T09 跨回合隔离；T10 非图像工具返回零回归；T11 MCP 线上形状/tool_stopped 语义不变；T12 M1-01 状态机+能力释放回归。确定性：门控为纯时间算术（无 sleep/线程竞态），T05/T06 同步复现。

## 六、刻意不做的事（范围约束）

- 不重写 LangGraph 回合执行器（阻塞模型是框架属性；本修复把伤害面——积压推理与陈旧产物——全部消除，而 Agent 回合内流式插入属框架级改动，留档为 M1 后续候选）。
- 不新建前端（复用 agent 聊天流做遥测展示；结构化字段供 cockpit 后续渲染增强）。
- 不动 VLN 模型协议、动作语义、PD 参数、M0/M1-01 证据。
