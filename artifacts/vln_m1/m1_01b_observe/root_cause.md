# VLN-M1-01B-OBSERVE 根因 — 真实工具调用图像回传生命周期缺陷

分支 `task/vln-m1-01b-observe-result-fix`（自 `91b5e30` 切出）· 修复提交 `37ddb68`

## 一、缺陷确认（第一步：先证据后代码，未先改生产码）

按任务书调用链构造**真实链路复现**（`test_observe_reproduction.py`）：
- 入口：真实 `Agent._process_message`
- 执行器：真实 `langchain.agents.create_agent` 图（真 LangGraph 工具节点，非假 `.stream()`）
- 工具：真实 `Agent._mcp_tool_to_langchain` 产出的 `call_tool`（真实 `_begin_tool_invocation` 打点 → MCP → 真实 `_append_image_to_history` → 真实 `add_message` → 队列）
- 回合关闭：真实 `_process_message.finally → _close_agent_turn`
- 消费：真实队列路径 + 真实门控
- 仅 IO 边缘为假：MCP HTTP 应答（脚本化）、LLM（MockModel 剧本）
- **零 ledger 手术**：不碰 `_tool_invocations`/`_turn_tool_calls`/消息元数据构造预期

复现结果（修复前）：
```
REAL-CHAIN RESULT: artefact_in_history=False  consumed_as_turn=True
TWO-TURN RESULT: artefacts_in_history=0
```
**每次真实 observe 的图像都被自家回合的关闭判死**——消息走了消费回合（`True`）但门控丢弃（不入模型历史 `False`）。

## 二、根因

GATE 提交 `9f91a8c` 的门控语义为“调用所属回合已关闭 ⇒ 遗留物”。但真实 LangGraph 时序中：

```
call_tool（流内）→ 图像入队 → 流结束 → 回合关闭（清登记）
→ 循环才取出图像消费 → 门控：登记已清 ⇒ 丢弃
```

**“上一回合产出的图像由下一回合消费”正是本 Agent（及 LangGraph 生态）一直使用的图像回灌约定**（`_append_image_to_history` 的存在意义）——GATE 的关闭即失效规则把它整体误杀。

## 三、GATE 报告“新鲜 observe 可用”结论为何错误

GATE 的 S5 用的是 `_GateGraph` 假执行器：`observe_call_artefact` 在**回合之外**手工入队消息——其时序是“回合 A 产出图像 → 回合 A 已关 → 回合 B 消费”，但它的 fake 路径**没有走真实 `call_tool` 打点 + 流内排队**，且当时门控读的登记是测试自己塞的（未被任何真实 close 清除）→ S5 通过是**harness 假象**。真实链路（流内打点 + close 清登记）必然失败——这正是本次复现所证。

实际影响：GATE 合入后**所有 observe 图像静默丢失**。两次真实 cockpit 复测“未见异常”是因为：① 复测指令均不含“observe 后看图确认”的流程；② 复测的核心断言（无追赶回复/无重复执行）不受图像丢失影响；③ Agent 对 observe 的回包文本（无图）仍正常返回，掩盖了图像层缺失。

## 四、为什么此缺陷在两次复测中未暴露（影响面评估）

- 指令形态：“往前走到红椅子前停下” → Agent 直接 `active_vln`，从不 observe——图像链路根本未被触碰
- 无用户反馈“模型说看不到东西”——因为 Agent 未被要求描述视觉内容
- `test_t05_verification_observe_after_vln_completion` 这类“任务完成后看图核验”流程在真实复测中未被执行（M0 时期的对应行为是 observe 后 artefact 回灌，属修复后恢复的能力）


## 五、FOLLOWUP 追记（提交 83bf4a5）：OBSERVE 修复重新打开了重复运动之门

**第一阶段真实链路复现**（`test_incident_rebuild.py`，仅 MCP HTTP/LLM 假件，零 ledger 手术）在 `edf8f9c` 上的实测时间线：

```
USER_COMMAND → active_vln(v1) → "Policy said stop."（COMPLETED）→ observe(c1)
→ 图像入队 → 回合关闭 → 消费图像（进模型 ✓ OBSERVE 修复恢复的能力）
→ 对抗性模型从图像回合发出 active_vln(v2) → 第二次运动真实发生（motion=2）
```

**结论：修复前（edf8f9c）允许第二次 active_vln。** 窗口门控无法关闭此门：重复执行的发起者是**消费图像的回合自身的模型决策**——距一张完全合法的图像仅一个回合。若靠丢弃图像来止动，等于重新破坏 observe（层级错误）。

根因结构：OBSERVE 修复把"图像可见性"（第一层）恢复的同时，没有独立回答"产物回合是否有权**发起运动**"（第二层）——任务书 E 项的分离要求正是缺失的一环。

修复后的同链路（对照证据，同一测试内断言）：`active_vln(v1) 执行一次 → 图像可见（artefact=1）→ 对抗性 v2 在工具层被结构化拒绝（MCP 层零调用）→ 拒绝文本回到模型可转述`。
