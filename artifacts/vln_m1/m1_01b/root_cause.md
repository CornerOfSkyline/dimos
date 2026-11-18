# VLN-M1-01B 根因分析 — Agent 异步消息时序与重复执行

日志来源：`~/.local/state/dimos/logs/20261118-123006-9fb4-unitree-go2-agentic-activevln-cockpit/main.jsonl`（ActiveVLN cockpit 栈，M1-01 代码，用户指令“往前走到红色椅子跟前然后停止”）

## 一、核实方法与日志时钟语义（先校正第一轮分析的误读）

`Agent message` 日志行的 timestamp 是 **`_record()` 的调用时刻 = 消息被图回合消费/产出的时刻**，不是入队时刻（`agent.py:494-497`：`_record` 才发布日志+`agent` 流；外部消息经 `add_message`（`agent.py:379-380`）静默入队）。因此：

- `04:34:54.530` 的 artefact 日志行 ≠ 图像晚到 13 秒；它 **04:34:41.67 就入队了**（`_append_image_to_history` 在 `call_tool` 内同步调用，`agent.py:272`）。
- 真正的问题是**消费顺序**：队列 FIFO 使 artefact 排在 12 条进度帧后面，被消费时已是陈旧图像。

## 二、完整事件时间线（单一日志时钟，已核对无跨钟误读）

| 时刻 (UTC) | 事件 | 代码位置 |
|---|---|---|
| 04:32:41.87 | 用户指令（human）入队，Agent 回合开始 | `_on_human_input` → `_message_queue` |
| 04:32:43.99 | 第 1 次 `active_vln` 调用（图回合内工具执行，**阻塞 Agent 线程 117s**） | `mcp_server._handle_tools_call` |
| 04:32:43–04:34:41 | 任务执行 11 轮；**12 条进度帧实时入队**（`turn N/50`），Agent 无法消费（阻塞在图回合里） | 技能 `tool_update` → `_on_tool_stream_message`（`agent.py:208-221`）→ `add_message` |
| 04:34:41.08 | 任务 A 终止："Policy said stop"；SKILL 行 + **Capabilities released on tool-stream stop**（M1-01 关流正常） | 技能 finally → `stop_tool` |
| 04:34:41.67 | Agent 回合恢复，调 `observe` #1 → **artefact #1（旧图）立即入队**（排在 12 条进度帧后） | `call_tool` → `_append_image_to_history`（`agent.py:253-272, 543-555`） |
| 04:34:42–43 | observe #2 artefact 入队；`get_vln_task_status`（同步工具）返回 **COMPLETED**——Agent 明确知道任务已完成 | |
| 04:34:44.53–52.68 | **队列积压追赶消费**：12 条进度帧逐条触发 LLM 推理（12 次“正在走…”回复） | `_thread_loop`（`agent.py:441-451`）逐条 `_process_message` |
| 04:34:54.53 | 回合结束（“需要我做点别的吗？”） | |
| 04:34:54.54 | **消费 artefact #1**：13 秒前的旧图像作为唯一新输入进入 LLM | 队列 FIFO |
| 04:34:56.84 | **qwen3.8 把旧图当作新观察，发起第 2 次 `active_vln`（同指令重复执行）** | 图回合输出 tool_calls |
| 04:34:56.85–04:35:50.30 | 第 2 次任务：53s、11 轮、再次正常完成（独立 run_id；M1-01 隔离正常） | |
| 04:35:52–53 | observe #2/#3 的 artefact 迟到消费（此时已是第 2 次任务之后的语境），未再触发第 3 次 | |

**04:34:56 与 04:35:52–53 顺序核实**：同一日志、同一单调时钟流；04:35:52 的 artefact 是第 2 次任务期间 observe 的产物，消费晚于第 2 次任务结束——不是第 2 次调用的原因，属同类时序缺陷的又一次无害呈现。

## 三、问题 A 根因（消息积压、集中输出）

`_thread_loop` 单线程模型：一条消息 → 一个完整图回合（LLM+工具，同步阻塞至 `state_graph.stream` 耗尽）。`active_vln` 是 117 秒的阻塞型工具，期间队列只能积压；M1-01 的进度帧又主动增加了队列流量（12 条），任务结束后逐帧追赶消费 → **每帧触发一次 LLM 推理**（12 次无意义回复） + artefact 排队延迟。

## 四、问题 B 根因（重复执行）

三个必要条件同时成立：
1. **进度帧以 HumanMessage 入队**（`_on_tool_stream_message` 包装 `[tool:active_vln] turn N`），语义上是遥测，却占用 LLM 决策输入通道；
2. **artefact 图像消息同样以 HumanMessage 入队**，且**入队时刻 ≠ 消费时刻**（阻塞+积压放大延迟），到达 LLM 时已陈旧；
3. **无回合关联/陈旧判定**：`_append_image_to_history` 不记录产物所属回合或生成时间，Agent 端无从判定"这是本回合的观察还是历史残留"。

LLM（qwen3.8）面对"任务已完成 + 一张'新'图像"的组合，判断为需要继续执行——第二调用是框架时序缺陷放大模型判断的结果。**技能/能力层无重复**：两次执行各有独立 run_id、第一次的能力经 tool_stopped 帧正常释放（日志 `Capabilities released`）、第二次是全新合法调用——M1-01 的隔离与释放机制工作正常；缺陷纯在 Agent 消息层。

## 五、修复策略（对应任务书 P0/P1）

**P0（重复执行防护）**：
- `TOOL_RESULT` 类消息（artefact/产物）携带：产生时刻、来源工具、（若有）关联 run_id/回合；入队时若目标工具为**阻塞型已完成 VLN 任务**所属回合之外，且图像年龄超过阈值 → 不入 LLM 队列（记录丢弃原因，图像仍走 UI 通道）。
- 关键设计：**新鲜度判定在消费侧**（入队时无法预知消费时刻），`_process_message` 取出 TOOL_RESULT 类消息时检查年龄：>N 秒 → 标记 stale 附加到内容（让 LLM 明确知道这是旧帧，不得据此新起任务）或直接丢弃（保留日志原因）。
- **指令判重不做字符串比较**：用户显式第二次发送相同指令 = 合法新 run_id（技能层 M1-01 已保证）；防护只作用于**工具产物触发的自主再执行**。

**P1（进度分流）**：
- `_on_tool_stream_message` 不再 `add_message`（不进 LLM 队列）；改为发布到独立的 `agent` 消息流（`agent.publish`，cockpit 现有展示通道），携带结构化字段（run_id/policy/turn/stage——从 M1-01 的 tool_update 文本解析为结构化 JSON）。
- 任务终止时一次性投递结构化终态（COMPLETED/FAILED/CANCELLED + reason + run_id）——同样只进展示通道，不进 LLM 队列（避免终态又触发 LLM 的"要不要再做点什么"推理循环；用户后续问题自然带出终态）。
- **紧急停止不受影响**：`_on_human_input` 直接输入线程处理（`agent.py:287-293`），不经队列。

## 六、必须保持的能力（任务书 §六，逐项对照实现位置）

| 能力 | 位置 | 修复中如何保持 |
|---|---|---|
| observe 返回图像 | `call_tool` → `_append_image_to_history` | 保留入队，仅在消费侧判定陈旧性 |
| 新鲜视觉继续决策 | 同上 | 新鲜（<阈值）artefact 照常进 LLM |
| get_vln_task_status | 技能 `@skill` | 不改 |
| 用户主动第二次相同指令 | `_on_human_input` | 不做指令判重；技能层新 run_id |
| 紧急停止不被阻塞 | `agent.py:287-293` 输入线程直达 | 不改 |
| VLN 协议兼容 | 两 skill | 不改 |
| ToolStream stop 释放令牌 | `stop_tool` → tool_stopped 帧 | 不改 |
| Task Runtime 状态机 | `vln_task.py` | 不改 |
| PD/动作空间 | `skill.py`/`_pd_drive` | 不改 |
