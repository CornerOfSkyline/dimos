# VLN-M1-01B-GATE 验收报告（report.md）

分支 `task/vln-m1-01b-agent-message-lifecycle` · GATE 提交 `9f91a8c`（前序：`16a61b3` P0、`f5f1a21` P1、`527def3` 01B 文档）
未推送、未合并、未进 M1-02；运行中的栈未触碰；M0 原始证据未改动。

---

## 1. 13 秒旧图为何能/不能通过现有门控

**修复前（M1-01B 初版）**：13s < 15s 阈值 → **能通过**。年龄是唯一条件，这是 GATE 审查确认的缺口——闭回合产物在阈值内照样进 LLM（生产事故恰在此区间）。

**GATE 修复后**：主条件为**调用存活性**——该图的 observe 调用所属 Agent 回合已在任务完成时关闭（`_close_agent_turn` 于回合 finally），账目条目清除 → 存活检查 False → **无论 1s/5s/13s 一律拦截**（test_agent_gate_timing.py S1-S3 直接证明）。15s 降级为辅助条件（仅作用于“活调用中排队过久”的情形）。

## 2. 任务关联机制能否阻止失效结果触发运动

**能**。链条：`call_tool` 在 MCP 请求前登记调用（`_begin_tool_invocation`，专用锁防执行器线程死锁）→ 产物携带调用戳入队 → 回合结束（finally）关闭本回合全部调用 → 消费时存活性检查先行拦截闭回合产物 → 丢弃物不产生任何 LLM 回合 → **不可能产生工具调用**。十项场景测试逐一断言 VLN 调用次数（全为 0，除用户显式指令场景）。陈旧 run 的产物（S8，0 秒龄）同样拦截。用户显式重复指令不受任何门控（USER_COMMAND 永不拦截，新 run_id 合法）。

## 3. 正常 observe 是否仍可用

**是**（S5 + 守护项 + 01B T04/T09 + MCP unit 19 项）：属于当前打开回合、新鲜（<15s）的 observe 图像照常进入 LLM 并被使用；连续两个回合各自的活产物都可用（不过度拦截）；同步 ToolMessage 路径完全不受门控（T10）。Agent 可据新鲜视觉继续决策，包括对已完成导航做结果总结（其后的 observe 属新回合、活调用）。

## 4. Cockpit 进度传输链验证结果

四层区分（详见 review.md §三）：
- **后端发布** ✅（代码+T02+真实复测会话证据）
- **relay 转发** ✅（`Chat` 面板 `messages` 通道契约：paced 20Hz、逐帧不丢；relay authoring 40 项回归）
- **浏览器收到** ✅（代码链：chat.json.v1 帧到 viewer；codec 渲染 `[tool:NAME]` 为 role=tool 行）
- **UI 实际渲染** ⚠️ **代码链完整可证，但需用户在 cockpit 界面最终确认**——聊天面板即渲染面（无新前端），上次复测期间应已显示 `turn N/50` 行。此项为唯一待用户确认项；如界面未显示，将排查 agent 流→relay 桥的订阅链。

结构化 `vln` 字段（policy/current_turn/max_turns/stage）在线上随帧携带，供后续 UI 增强使用；未改前端。

## 5. 回归结果

**206/206 通过**：M0 45 + M1-01 26 + 01B 17 + GATE 12 + MCP server/client unit + capability + relay-bridge authoring 40 + chat encoding。
ruff：仅 2 个 main 既有项（紧急停词表全角标点）；mypy：仅 2 个 main 既有项（init_chat_model overload）——均 stash 对照 HEAD 确认非本分支引入。
过程缺陷均已修复并记录（review.md §四：重复 helper、记账死锁、过时注释）。

## 6. 遗留风险与合并建议

**遗留风险**
1. UI 渲染的用户侧确认（上节）——代码链已四层验证，风险低。
2. per-UUID 关联未做（现为工具名+调用时刻记账，与能力注册表 takeover 语义对齐）；若未来需要帧级精确关联，宜由 McpServer 透传 progress_token 上下文——候选增强，非缺陷。
3. 15s 阈值仍为常量（现为辅助条件，误伤面已大幅缩小）。
4. Agent 回合内流式响应（框架级）未动，最小设计仍在 01B design.md 留档。
5. M1-01 的既有遗留（硬 worker 死亡能力残留、24h 超时兜底）不变。

**合并建议**：**建议合并**。M1-01B+GATE 共 4 个功能/修复提交（16a61b3、f5f1a21、9f91a8c + 文档 527def3 与本批），生产缺陷根因双切断+门控加固、206 项回归零失败、真实复测行为符合预期；遗留项均已记录且有明确后续路径，不阻塞合并。合并方式可沿用 M0 的 `--no-ff`（保留阶段历史块）。

**结论**：GATE 六项交付问题全部明确回答；未自动推送/合并，等待审核。
