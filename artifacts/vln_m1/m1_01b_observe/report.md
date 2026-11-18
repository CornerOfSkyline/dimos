# VLN-M1-01B-OBSERVE 验收报告

分支 `task/vln-m1-01b-observe-result-fix`（自 M1-01B `91b5e30` 切出）· 修复提交 `37ddb68`
未推送、未合并、未进 M1-02；main 未动；运行栈未触碰；M0 原始证据未改动；VLN 协议/PD 参数零改动；LangGraph 执行器零重写。

## 六项必答

### 1. 原 GATE 是否确实会丢弃真实工具调用产生的新鲜图像？

**是——已用真实调用链复现证明**（先证据后代码）。GATE 的“回合关闭即失效”规则把**每一次**真实 observe 图像判死：复现输出 `artefact_in_history=False, consumed_as_turn=True`（修复前）。GATE 合入后所有 observe 图像被静默丢弃；此前两次 cockpit 复测未暴露，是因为复测流程从未走“observe 后依赖图像”的路径（详见 root_cause.md §三）。

### 2. 原 S5 为什么能够通过？

S5 的 harness 是假执行器（`_GateGraph`）：消息在**回合外**手工入队、门控读到的登记是测试自设且从未被任何真实 close 清除——**假路径绕开了“流内打点+关闭清除”的真实时序**，故而通过。它验证的是“手工造出的 live 登记 + 新鲜图像”组合，不是生产的真实链路。这是本任务书质疑的价值所在：GATE 报告结论在真链上不成立。

### 3. 修复后正常 observe 如何进入模型？

**回合窗口语义**：门控先于消费回合的 `_open_agent_turn` 执行，故 `_turn_opened_at` 恰是**产出回合**的打开时刻；图像的调用戳 ∈ 产出回合窗口 ⇒ VALID。这按构造覆盖生产时序（`call_tool` 在流内打点、图像入队、回合关闭、循环消费——`T02` 显式断言 close→consume 顺序仍放行）。登记不再随回合关闭清除；同名更新调用以 stamp 淘汰旧图；跨窗口旧图丢弃。

### 4. 如何保证旧任务图像不触发重复导航？

三重独立防线（任一独立生效）：① **窗口判定**：旧任务的调用戳早于最后打开回合（生产中任务间隔=秒级 LLM 延迟+执行时间，跨任务图必跨窗口）→ 丢弃；② **superseded 判定**：同名更新调用淘汰旧图；③ **结构性保证**（T06）：门控在模型回合**之前**执行，对抗性模型剧本永远没有“看到旧图”的回合存在——不可能发出 active_vln。USER_COMMAND 不设门，用户显式重复=合法新 run（T07 真工具全链验证）。

### 5. 所有测试结果如何？

**218/218 全过**：M0 45 + M1-01 26 + M1-01B 17 + GATE 12（更新至窗口语义）+ OBSERVE 12（T01–T10 + 真链复现 2）+ MCP/能力/ToolStream/annotation/relay-bridge/chat 全套。ruff/mypy 残项 stash 对照均为 main 既有对（全角标点词表 / init_chat_model overload）。

### 6. 是否建议合并 M1-01B 整体分支？

**建议合并**，且**必须**包含本 OBSERVE 提交——GATE 单独合入会让所有 observe 图像静默丢失（本任务证明的实缺陷）。合并范围：`task/vln-m1-01b-agent-message-lifecycle`（01B 4 提交）+ 本 OBSERVE 分支（1 提交），或以 OBSERVE 分支为最终 HEAD 合并（其历史包含全部）。方式沿用 `--no-ff`。合并前的最小回归面已由本次 218 项给出。

## 遗留与建议

1. **端到端确认项**：栈重启后跑“observe 并让模型描述所见”指令（GATE 期间该能力静默丢失，本修复恢复；测试已证但建议真机一次目击）。
2. 逐 UUID（tool_call_id→invocation→run_id）帧级关联：现为“工具名+时刻”（与能力注册表 takeover 语义对齐）；帧级精确关联需 McpServer 透传 progress_token——候选增强。
3. 紧邻回合重复投递（测试坍缩时钟特有形态）在 50ms 容差内放行：一次有界多余推理、无运动风险（T09 记录边界语义）。
4. M1-01 既有遗留（硬 worker 死亡能力残留、24h RPC 兜底、15s 年龄常量）不变。
5. GATE 流程教训已写入 root_cause：**假执行器 harness 的结论不得外推为真实链路行为**——后续新增生命周期类修复应默认附真实链路复现（本任务的模式）。


## FOLLOWUP 追记（提交 83bf4a5）：重复导航风险的验证与消除

1. **验证结果**：`edf8f9c`（OBSERVE 修复后）确实重新引入重复导航路径——第一阶段真实链路复现记录了完整的第二次 active_vln 执行（时间线/消息顺序/任务状态全部留痕于 root_cause §五）。
2. **消除机制**：产物回合运动守卫（`_turn_origin` + `_mcp_tool_call` 拒绝）——可见性与运动授权分离（任务书 E 的机制化）。用户回合零影响、观察/核验/续接零影响、仅拦"从工具产物回合新发起运动"。
3. **修改文件**：`agent.py`（origin 记录 + 拒绝逻辑 + 运动工具清单常量）、`test_incident_rebuild.py`（事故链 + 对照 + T11–T13）。独立提交 `83bf4a5`，历史保留。
4. **测试**：T01–T13 全过；原 218 项回归全过（总 223）；ruff/mypy 残项均为 main 既有对；`git diff --check` 干净。
5. **尚未解决的风险**：
   - `_PRODUCT_TURN_MOTION_TOOLS` 为清单常量——后续新增运动型 background 技能需手工登记（应后续改为 skill 注册表注记，如 `uses=[CAP_MOVEMENT]+lifecycle` 自动推导）；
   - 50ms 容差的紧邻重复投递仍会多一次有界推理（可见性边界，无运动风险）；
   - 逐 tool_call_id 的帧级产物关联仍未做（工具名+时刻的替代覆盖当前全部已知场景）；
   - M1-01 既有遗留不变（硬 worker 死亡能力残留、24h RPC 兜底、15s 年龄常量）。
6. **合并建议**：维持并强化——M1-01B 整体（含 GATE、OBSERVE、FOLLOWUP）一并 `--no-ff` 合并；**任何一层单独合入都构成已知缺陷**（GATE 单独=丢图，OBSERVE 单独=重复运动之门，FOLLOWUP 依赖前两层语义）。
