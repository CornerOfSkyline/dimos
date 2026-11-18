# VLN-M1-01 设计 — VLN 任务生命周期管理与执行安全加固

分支 `task/vln-m1-01-task-lifecycle`（自 main @ `95ebead` 切出）。

## 第一部分：现有架构审查（A–G 逐答，源码位置）

### A. DimOS 如何判断后台 Skill 已经启动？
**没有通用的"已启动"信号。** DimOS 的 background 语义只影响 CAP 所有权与 stop 工具配对
（`mcp_server.py:280-284`"所有权移交给工具流生命周期"；`mcp_server.py:129-137`
`_HOLDER_STOP_TOOLS`），不改变调用阻塞语义。两种既有范式：
- **Patrolling**（异步任务型）：`start_patrol`（`patrolling/module.py:81-97`）立即返回，
  在 `@skill` 线程里 `asyncio.create_task(_patrol_loop)`；调用方 HTTP 即刻得到回包。
- **VLN（M0 现状）**：`stream_vln`/`active_vln` 整段阻塞到 episode 结束才返回
  （运行期以 background 身份持有 CAP_MOVEMENT）。
"启动"的系统性痕迹只有两个：CAP 注册表持有人条目（`capabilities.py:109-119`）与
ToolStream 的首帧（若有）。

### B. 如何判断后台 Skill 正在运行？
- 技能自暴露 RPC：`is_patrolling`（`patrolling/module.py:99-101`，`_patrol_task.done()`）。
- CAP 注册表快照：`CapabilityRegistry.snapshot()`（`capabilities.py:136-139`）。
- **VLN（M0 现状）**：仅技能内部的 `_running` Event（不可经 MCP 查询）。
  M0-GATE 已发现类级共享 bug 并修复为实例属性。

### C. 如何获取 Skill 中间执行进度？
`ToolStream` 是唯一既有进度通道：技能在 `@skill` 调用线程内
`Module.start_tool(name)`（`module.py:581-599`）→ 任意线程
`tool_update(name, message)`（`module.py:605-619`）→
`notifications/progress`（有 progressToken 时）或 `notifications/message` 帧
（`tool_stream.py:176-194`）→ McpServer SSE 扇出（`mcp_server.py:366-387`）。
**VLN（M0 现状）**：从未调用 start_tool——既无进度推送，也埋下了 D 项的释放缺口。

### D. Skill 结束后由谁释放 CAP_MOVEMENT？（M0-GATE 遗留缺口的精确定位）
释放链：技能 `stop_tool(name)`（`module.py:623-632`）→
`ToolStream.stop()` 发布带 `acquire_token` 的 `dimos/tool_stopped` 帧
（`tool_stream.py:196-217`，token 在构造/rebind 时从 skill 上下文捕获，
`tool_stream.py:153-156, 159-174`）→ McpServer 订阅回调按 token
`release_by_token`（`mcp_server.py:372-380`）。token 失配（已被同工具新调用接管）
则匹配不到、不误释放（`capabilities.py:121-134`）。
**M0 缺口（本阶段必须修）**：VLN 技能是 `lifecycle="background"` 但从不
`start_tool` —— 正常结束后**没有任何东西发布停止帧** → CAP_MOVEMENT
由已结束调用的 token 无限期持有（直到同工具 takeover / stop 工具抢占 / 栈重启）。
期间任何其他 movement 工具（navigate_with_text 等）会被拒绝
（background holder 不等待：`mcp_server.py:193-198`）。
M0 实测没暴露它，只因为连续 episode 恰好是同工具 takeover（`capabilities.py:82-83`）。
修复方式 = Patrolling 范式：运行期开流、结束/停止时关流——零新机制。

### E. Worker 异常退出时有哪些事件可观测？
- 优雅停机：`python_worker.py:420-427` 逐模块 `instance.stop()`（触发技能的
  `Module.stop()`）→ join→terminate→kill 三级（`python_worker.py:346-368`）。
- 子进程死亡可查询：`has_exited`（`python_worker.py:204-206`）——**但 McpServer
  不订阅任何 worker 存活信号**；硬死亡时无人代发 tool_stopped 帧 → CAP 残留。
- RPC 层异常（可观测的失败，区别于静默挂死）：`call_sync` 抛异常 →
  `mcp_server.py:286-289` finally 释放该 token（background 成功路径才移交）。
- 机器人侧安全网：cmd_vel 超时（`connection.py:221` 注释"the cmd_vel timeout
  covers replay"）——worker 死亡 ⇒ 速度流静默 ⇒ 运动超时停住；注册表残留是
  **记账问题**而非运动安全问题。
- 上界：background 技能 24h RPC 超时（`mcp/constants.py:25`，M0 引入）兜底释放。

### F. Agent 当前能否查询正在执行的任务？
不能（通用层面无此接口；仅 `is_patrolling` 这类技能私有 RPC）。VLN 无任何查询面。

### G. 能否复用现有状态机制而不引入新框架？
**能，全部复用**：`Module.start_tool/tool_update/stop_tool` + `ToolStream`（进度与
CAP 释放）+ `CapabilityRegistry`（token 记账）+ `@skill`/MCP（查询接口）+
`asyncio`/`threading.Event`（并发原语）。本设计仅新增一个**技能内的轻量任务运行时**
（数据类 + 状态机，纯内存，无数据库/队列/调度器）。

---

## 第二部分：设计 — 统一 VLN Task Runtime

### 状态机

```
CREATED → RUNNING → {STOPPING} → COMPLETED | FAILED | CANCELLED
                ↘ (直接终态：策略说停/预算尽/异常) ↙
```

| 状态 | 语义 |
|---|---|
| `CREATED` | handle 已铸造，run 循环未进入（瞬态） |
| `RUNNING` | run 循环执行中（可能正阻塞在推理） |
| `STOPPING` | **取消已请求但执行实例未退出**——不等于已停止；可能仍在推理/动作中回卷 |
| `COMPLETED` | run 循环正常走完退出（POLICY_STOP / TURN_BUDGET_EXHAUSTED）。**只表示任务正常结束，不等于导航目标到达** |
| `FAILED` | 异常退出（POLICY_ERROR / SENSOR_UNAVAILABLE / EXECUTION_ERROR） |
| `CANCELLED` | 用户停止请求已完成（USER_CANCELLED），执行实例已退出 |

终止原因（独立于状态记录）：`POLICY_STOP`、`TURN_BUDGET_EXHAUSTED`、
`USER_CANCELLED`、`POLICY_ERROR`（推理异常/服务器不可达/非法动作输出）、
`SENSOR_UNAVAILABLE`（相机/odom 丢失）、`EXECUTION_ERROR`（执行器未预期异常）。

阶段 `stage`：`INFERENCE`（策略调用中）/ `ACTION_EXECUTION`（PD 归位执行中）/
`WAITING_OBSERVATION`（等待下一帧观测）/ `STOPPING`（回卷中）。每次变化刷新
`updated_at`（作为查询侧 stale 判定的心跳）。

### TaskSnapshot（查询接口的结构化载荷）

```
run_id, policy_name, instruction, state, current_turn, max_turns, current_stage,
started_at, updated_at, finished_at, termination_reason, error_message
```
外加查询时计算的 `stale_s`（RUNNING/STOPPING 且 updated_at 超过 30s →
带 `stale: true` 标记；**不得把陈旧快照伪装成正常 RUNNING**）。

### RunHandle 与并发安全（§5.1 / 验收 C/D/E）

- 每次任务 `runtime.begin(policy, instruction, max_turns)` 铸造唯一 `run_id`
  （uuid4 hex）与**该 run 专属的 cancel Event**；当前 handle 存于 runtime
  （`_current`），历史快照保留最近 8 条（支持按 run_id 查询与 T13）。
- **旧结果不能驱动机器人**：run 循环只引用自己 handle 的 cancel Event（局部捕获），
  绝不读"当前 handle"指针；推理返回后、每个动作执行前、PD 每个周期都检查
  `handle.cancel`——取消后到达的过期动作序列**整批丢弃**，直接进入 CANCELLED。
- **同一技能任何时刻至多一个运动执行实例**：新调用在 runtime 处被拒
  （RUNNING/STOPPING → 返回当前 run_id+state 的说明），不会产生第二实例；
  MCP 层同工具 takeover 只改记账 token（由 `start_tool` rebind 对齐，
  `module.py:585-592`——正是 Patrolling 的 takeover 范式）。
- **STOPPING ≠ 已停止**：终态只在 run 循环真正退出时由 handle 落定
  （`finish()` 幂等且仅当前 handle 可落定），状态不靠 `_running.is_set()` 推断。

### 零速度保障（§5.2 / 验收 F）

run 循环外层 try/finally：**任何退出路径**（正常/取消/异常/未预期）都发布零 Twist +
`cancel_goal()` + `stop_tool()`（关流=释放 CAP）。`_pd_drive` 每周期读显式传入的
cancel Event（None 时回落 `skill._running`，M0 单测兼容），其退出已保证零速。

### CAP_MOVEMENT 五情形（§5.3 / 验收 G/H）

| 情形 | 修复前（M0 合并态） | 修复后 |
|---|---|---|
| 正常完成 | **泄漏**：无停止帧，token 悬挂至 takeover/抢占/重启 | run finally 关流 → 帧带 token → McpServer 释放 |
| 用户停止 | 抢占已运行 stop 逻辑（机器人安全），但注册表残留 + stop 工具二次 acquire 可能被拒 | run 退出时关流释放；stop 工具的抢占重试随即成功 |
| 任务异常 | RpcCall 异常路径 → finally 释放（`mcp_server.py:286-289`）| 同左（不变）+ run finally 零速/关流 |
| RPC 超时 | 24h 兜底释放（临时规避，保留为上界） | 同左；正常 episode 远小于该值，stop 工具秒级抢占 |
| Worker 崩溃 | 无帧 → 残留 24h；运动安全由 cmd_vel 超时保障 | 优雅停机路径新增：`Module.stop()` 里 `stop_tool`（帧发出）→ 优雅停机不泄漏；**硬死亡残留为已知限制**（需协调器级存活信号=架构改动，按任务要求只提交设计与测试证据，不擅自改通用 RPC 架构） |

### Agent 查询接口（§六 / 验收 B）

两个技能容器各自新增（同签名、同语义，蓝图按 `vln_policy` 只部署其一）：

```python
@skill
def get_vln_task_status(self, run_id: str | None = None) -> str
```
- 返回 JSON 字符串（结构化）；`run_id=None` → 最近一次任务。
- 纯内存读：不占 CAP_MOVEMENT、不触碰运动控制、即时返回（不等待 VLN 技能）。
- 历史保留最近 8 条快照；任务结束后仍可查（验收 T02）。
- Worker 已死 → 该 RPC 本身失败（MCP 返回工具错误，不会谎报 RUNNING）；
  Worker 挂死 → `stale_s/stale` 标记暴露。
- 进度：run 循环经 `tool_update` 推送阶段/轮次变化（ToolStream progress 帧），
  不用大模型推理更新状态。

### 不改动项（验收 I/J）

模型推理协议（HTTP/聊天格式、BGR 序、reset 语义）、动作语义（解析/合并/拒收/
执行步数对应）、PD 参数（增益/钳制/完成阈值）全部保持；`_pd_drive` 仅新增可选
`cancel` 参数（默认行为不变）。

## 测试映射（§七）

| 编号 | 测试 | 位置 |
|---|---|---|
| T01 | begin→RUNNING→状态转换合法性 | test_vln_task.py |
| T02 | 正常结束+快照保留（按 run_id 可查） | 同上 + 技能级 |
| T03 | 推理阻塞中查询（Event 门控，stage=INFERENCE） | test_*_vln.py |
| T04 | 动作执行中查询（Barrier 门控，stage=ACTION_EXECUTION） | 同上 |
| T05 | 用户停止 → CANCELLED/USER_CANCELLED + 零速 | 同上 |
| T06 | RUNNING/STOPPING 期间重复启动被拒 | 同上 |
| T07 | 停止后旧模型结果延迟返回 → 整批丢弃不执行 | 同上（Event 确定性） |
| T08 | 模型异常 → FAILED/POLICY_ERROR + 零速 | 同上 |
| T09 | 执行器异常 → FAILED/EXECUTION_ERROR + 零速 + 关流 | 同上 |
| T10 | 相机/odom 丢失 → FAILED/SENSOR_UNAVAILABLE + 零速 | 同上 |
| T11 | 未预期异常的零速（外层 finally） | 同上 |
| T12 | 正常结束发布带 token 的 tool_stopped 帧 → 释放（对照：无帧=泄漏=M0 缺口复证） | test_vln_task.py（fake 总线 + 真注册表） |
| T13 | 陈旧 run_id 查询不影响新任务 | 同上 |
| T14 | Worker 异常死亡：优雅停机关流路径 + 硬死亡残留复证 + stop 工具 takeover 恢复 | 同上 + 设计记录 |
| T15 | 两策略状态语义一致（同一 runtime + 两侧同款断言） | test_vln_task.py 参数化 |
