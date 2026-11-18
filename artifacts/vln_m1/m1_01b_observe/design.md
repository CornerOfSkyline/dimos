# VLN-M1-01B-OBSERVE 设计 — 回合窗口语义

修复提交 `37ddb68`。

## 一、四类图像语义（任务书 §三，逐类规则）

| 类 | 场景 | 判定（修复后） |
|---|---|---|
| **A 同步工具结果** | 回合内 observe 完成并带图 | **VALID**：调用戳 ∈ 产出回合窗口（门控先于消费回合的 open 执行，`_turn_opened_at` 即产出回合的打开时刻）→ 放行进入模型 |
| **B 后台工具结果** | 工具先返回任务标识，图像后到 | **VALID**（同 A）：判据是**调用的发起时刻**（invocation stamp），不是图像的到达时刻——后台延迟到达不改变“由上一回合发起”的事实 |
| **C 已失效工具结果** | 来源任务失效/被替代 | **INVALID**：同名**更新调用**的 stamp 更晚 → superseded 分支丢弃；且若 stamp 早于上回合窗口 → leftover 分支丢弃 |
| **D 旧任务迟到图像** | 原任务已结束多回合，图像才到 | **INVALID**：stamp 早于最后打开回合的窗口 → 丢弃（重复执行根因的防线，GATE S1–S3 语义在此仍由窗口差保证——生产中回合间隔为秒级，跨任务图必然跨过 ≥1 个完整窗口） |

**门控顺序**（`_process_message` 开头）：USER_COMMAND **永不拦截** → TOOL_RESULT：① 窗口判定（stamp 早于 `_turn_opened_at` − 0.05s ⇒ 丢弃）→ ② superseded 判定（同名更新调用 ⇒ 丢弃）→ ③ 年龄辅助（活图像 >15s ⇒ 丢弃）→ 放行。

## 二、机制变更（最小修复，零架构重写）

**移除**：`_close_agent_turn` 不再清除 invocation 登记（那是误杀根源）。
**新增**：`_open_agent_turn` 记录本回合打开时刻（`_turn_opened_at`），上一回合时刻存 `_prev_turn_opened_at`（现为调试辅助）。
**判定改为纯时间算术**：`_tool_invocation_is_live = stamp ∈ [上一打开回合, +∞)` 且未被同名更新调用超越。容差从 0.5s 收紧为具名常量 `_INVOCATION_TOLERANCE_S = 0.05`（同线程打点/开窗时钟抖动吸收；回合边界在真实负载下由 LLM 延迟（秒级）主导，50ms 不影响边界锐度）。

## 三、关联模型（任务书 §四“优先采用调用关联”的落地形态）

```
tool_call_id（LangGraph 内部）──┐
invocation_id = 工具名 + 发起时刻（invocation map 的键值）← 实际实现
agent_turn_id = _turn_opened_at 回合窗口 ────────────────────┘
run_id（VLN 任务）—— 技能层 M1-01 状态机，消息层只透传，不变
```
采用“工具名+时刻”而非逐 UUID：artefact 的 UUID 在调用返回后才铸（`call_tool` 内），逐 UUID 关联需 McpServer 透传 progress_token（更大改动）；工具名级 scoping 与 CapabilityRegistry 同名 takeover 语义一致，且时间戳天然隔离同名连续调用。若未来需要帧级精确关联（如视频流多帧），再评估 McpServer 透传——已记录为候选。

**未采用**的替代方案：图像直接注入 LangGraph 当前执行上下文（多模态 ToolMessage）——需逐 provider 评估多模态 ToolMessage 支持（OpenAI 兼容性是 `_append_image_to_history` 注释明确记录的坑），改动面大；现修复保留既有回灌机制零迁移。

## 四、修复后消息流（正常 observe）

```
用户指令 → 回合打开(T1) → 模型调 observe → call_tool 打点(stamp∈T1 窗口)
→ 图像入队 → 流结束 → 回合关闭（登记保留）→ 循环消费图像
→ 门控：stamp ≥ T1−0.05 ✓ → 放行 → 进入模型历史（视觉可用）
```
```
旧任务图像(D)：stamp < T_n−0.05 → 丢弃（日志+cockpit 说明，模型不可见）
用户显式重复指令：USER_COMMAND 不设门 → 新 run_id（技能层保证）
同名连续调用：c2 的 stamp > c1 图像 stamp+0.05 → c1 图像若迟到即被 superseded 丢弃
```

## 五、刻意边界（记录，不视为缺陷）

1. **紧邻回合的重复投递**（生产中毫秒级不可能、测试坍缩时钟下出现）：50ms 容差内放行——模型多看一次同图（一次有界推理），**不会触发运动**（无 USER_COMMAND）；跨任务边界的重复由窗口拦截（T09 两形态分别断言）。
2. 15s 年龄辅助保留：活窗口内排队过久的图像仍按陈旧丢弃（长 VLN 阻塞场景）。
3. 硬 worker 死亡、24h 超时等 M1-01 既有遗留不变。


## 六、FOLLOWUP 追记：双层机制（可见性与运动授权分离，任务书 A–E 逐项）

**机制**（提交 83bf4a5，最小改动：Agent 侧两个函数 + 一常量）：
- 第一层（OBSERVE，不变）：回合窗口门控决定 TOOL_RESULT **可见性**（图像是否进模型）。
- 第二层（新增）：`_process_message` 记录每回合的驱动来源 `_turn_origin`（"user" | "tool_result"，finally 清除）；`_mcp_tool_call` 在 **tool_result 驱动的回合**拒绝**发起**后台运动工具（`_PRODUCT_TURN_MOTION_TOOLS = active_vln, stream_vln`）——返回结构化拒绝文本（模型可转述用户），**运动零发生**。

| 要求 | 满足方式 |
|---|---|
| A 正常 observe 进模型 | 第一层不动；第二层不碰非运动工具 |
| B 旧产物不能仅凭 TOOL_RESULT 触发运动 | 第二层硬拒绝（发生在工具调用进入 MCP 之前，对抗性模型无从绕过——T06/T12 结构性证明） |
| C 用户显式重复指令可新 run | `_turn_origin=user` 时零拦截（T07/T11 既有测试不变） |
| D 运行中任务可合法观察/续接 | 只拦"从产物回合**新发起**运动"；任务内 observe/续接与非运动工具不受影响（T11 验证核验路径） |
| E 可见性与授权分离 | 两层机制的字面实现 |

**关联模型现状 vs 优先评估项**：tool_call_id（LangGraph 内部）/ invocation_id（工具名+时刻，OBSERVE 已实现）/ agent_turn_id（`_turn_opened_at` 窗口 + 新增 `_turn_origin`）/ run_id（M1-01 技能层，消息层透传不变）/ Movement Capability（McpServer 侧不变——第二层在到达能力仲裁**之前**就拒绝了产物驱动的运动调用，天然与能力授权正交）。逐 tool_call_id 的帧级关联仍是候选增强（需 McpServer 透传）；本机制以回合来源实现同样的安全边界，改动面最小。

**刻意不做**：不恢复"close 即清除"（会再杀 observe）；不给产物回合的图像打"禁止运动"提示替代硬层（提示可被对抗模型无视）；不改 MCP 架构（拒绝在 Agent 工具适配层完成）。
