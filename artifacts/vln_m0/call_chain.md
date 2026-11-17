# VLN-M0 调用链文档 — DimOS Go2 VLN 推理与运动执行闭环

分支 `task/vln-m0-closed-loop-validation`，基线 main @ `79e8eae`。
本文档覆盖 ActiveVLN 与 StreamVLN 两条策略的完整闭环，标注每跳的文件/函数、同步/异步、
数据格式、上下文维护、停止路径与中断风险。

**标注约定**：`[已实现]` = 代码存在且经单元测试覆盖；`[已验证]` = 在 MuJoCo 仿真闭环中
实际运行并留有证据（证据路径见文末）；`[运行时待验证]` = 代码已实现但尚未在真实模型
闭环中验证。修复项以 M0-01 提交 `dec467a` 为准。

---

## 0. 部署形态（一条命令拉起全部）

`~/workspace/dimos_ws/run_go2_streamvln.sh --daemon` 依次拉起：

| 组件 | 进程/端口 | GPU | 说明 |
|---|---|---|---|
| StreamVLN 策略服务器 | `streamvln-deploy/http_realworld_server.py`（打补丁的官方 realworld 服务器副本，:5801） | GPU0 | venv `streamvln-serve`，flash-attn 2.8.3，torch 2.7.1+cu126 锁死 |
| ActiveVLN 策略服务器 | vllm serve（:8003，`unitree-go2-agentic-vln` 系列使用） | GPU1 | venv `activevln-serve`，Arvil/Qwen2.5-VL-3B_rl_r2r_4000 |
| Deno relay + cockpit | :7780（TLS） | — | web 界面与聊天 |
| DimOS 栈本体 | `dimos --simulation run unitree-go2-agentic-activevln-cockpit --vln-policy {activevln\|streamvln}` | — | MuJoCo 仿真 + Agent + McpServer(:9990) + relay bridge |
| Agent 大脑 | 远程 ollama `http://172.23.63.9:11434`（qwen3.8:latest，经 .env `OLLAMA_BASE_URL`） | — | 工具选择与自然语言回复 |

---

## 1. 全链路总览

```
用户（cockpit 聊天 / dimos agent-send / MCP CLI）
  │ 文本消息（WebInput → LCM agent stream）
  ▼
Agent（dimos/agents/agent.py: Agent, LangGraph 工具循环）
  │ ① 紧急停止拦截：_on_human_input → _is_urgent_stop（≤24 字符全词匹配
  │    停止/停下/别动/停/stop/halt）→ _interrupt_movement_skills（输入线程直接调用）
  │ ② 工具选择：LLM 从 McpServer(:9990) 获取的工具表中选择 active_vln / stream_vln
  ▼
McpServer（dimos/agents/mcp/mcp_server.py: _handle_tools_call）
  │ ③ 能力仲裁：CapabilityRegistry.acquire（CAP_MOVEMENT 独占，token 记账；
  │    冲突时 _preempt_capability_holders 经 _HOLDER_STOP_TOOLS 抢占旧持有者）
  │ ④ 调度：@skill 方法经 forkserver worker 进程 RPC 执行；
  │    lifecycle="background" 的技能立即返回 started，配对 stop 工具收尾
  ▼
VLN 技能（dimos/navigation/experimental/{active_vln,stream_vln}/skill.py）
  │ ⑤ 订阅（start() 中注册，非懒订阅）：color_image(Image) / odom(PoseStamped)
  │ ⑥ 策略推理（每轮）：
  │    ActiveVLN:  _chat → OpenAI 兼容 :8003（含 SYSTEM_PROMPT + 历史 + 图像 + 指令）
  │    StreamVLN:  _post_frame → HTTP POST :5801/eval_vln
  │                multipart: image=BGR顺序JPEG + json={"reset":bool,"instruction":str}
  │ ⑦ 动作解析：ActiveVLN _parse_actions（文本→(id,cm/deg)，逐子动作截到 75cm/45°）
  │             StreamVLN _parse_server_actions（校验 id∈{0,1,2,3}，非法即 ValueError
  │             拒绝；全部消费，无 [:3] 截断）→ 共用 _coalesce（同向合并，>75cm/>45°
  │             无损拆分，零丢弃）
  ▼
_pd_drive（PD 速度归位控制器，10 Hz；官方 StreamVLN realworld/pid_controller.py 移植）
  │ ⑧ _relative_goal(pose, forward_m, degrees)：以当前 odom 为基准的世界系增量目标
  │ ⑨ 10 Hz 循环：前向误差 e_p / 偏航误差 e_r → PD 步进（Kp=Kd=3.0/0.5，
  │    v≤0.5 m/s, w≤1.2 rad/s）→ tele_cmd_vel.publish(Twist)
  │    turn_only（原地转）只闭合偏航环；卡死检测（指令速度 3s 无进展→放弃）；
  │    每个退出路径都发布零 Twist
  ▼
MovementManager（dimos/navigation/movement_manager/movement_manager.py）
  │ ⑩ teleop 优先级复用：任何 tele_cmd_vel（含零速）→ _teleop_active=True，
  │    _cancel_goal()（取消 nav 目标 + NaN 兜底）→ 缩放后转发 cmd_vel；
  │    nav_cmd_vel 在 teleop 活跃后 1s 冷却内被丢弃
  ▼
Go2 仿真连接（dimos/robot/unitree/go2/connection.py: UnitreeGo2ConnectionModule）
  │ ⑪ cmd_vel: In[Twist] → 仿真连接（共享内存写 MuJoCo）；odom: Out[PoseStamped]、
  │    color_image: Out[Image] 从仿真流发布（MuJoCo 进程 write_odom →
  │    dimos/simulation/mujoco/mujoco_process.py）
  ▼
MuJoCo 仿真（机器人运动、相机渲染）
  │ ⑫ odom（位姿）与 color_image（第一视角图像）回到 ⑤ 的订阅
  ▼
下一轮推理（⑥）：StreamVLN 在整批动作执行完后 POST 新帧；
ActiveVLN 在每轮动作执行完后带新观测再次请求
```

---

## 2. 分跳细节：文件 / 函数 / 同步异步 / 数据格式

### 2.1 用户输入 → Agent（异步）
- cockpit 聊天经 relay(:7780) → relay bridge 模块 → LCM agent stream；
  `dimos agent-send` 走同一通道。
- `Agent._on_human_input`（`dimos/agents/agent.py:287`）在**输入线程**同步执行：
  若 `_is_urgent_stop()`（`agent.py:504-528`，去礼貌词、全词匹配）则直接
  `_interrupt_movement_skills()`（`agent.py:179`）——**不等 LLM 回合**，这是
  停止延迟的第一层保障。
- 正常消息进入 LangGraph 工具循环（同步阻塞在该回合内）。

### 2.2 Agent → McpServer（进程内取表，跨进程调用）
- 工具表：`McpServer._handle_tools_list`（`mcp_server.py:103`），background 技能
  带 `dimos/lifecycle` 元数据与配对 stop 工具。
- 调用：`_handle_tools_call`（`mcp_server.py:169`）→ 能力仲裁 → worker RPC。
  `stream_vln` / `active_vln` 均 `lifecycle="background"`：MCP 立即返回
  "started"，技能在 worker 内长跑；Agent 聊天线程不被导航阻塞（cockpit 可随时发停）。

### 2.3 McpServer → 技能（worker 进程，独占能力）
- `CapabilityRegistry`（`dimos/agents/capabilities.py:52`）：`CAP_MOVEMENT`
  独占持有；token 记账释放（`release_by_token`）。
- 冲突抢占：新技能要 `CAP_MOVEMENT` 而旧持有者是 background 技能时，
  `_preempt_capability_holders`（`mcp_server.py:140`）先调用其配对 stop 工具
  （`_HOLDER_STOP_TOOLS`：stream_vln→stop_stream_vln 等）再重试获取。
- **实例隔离**：`_running: threading.Event` 与 `_run_lock` 现为 `__init__` 里的
  实例属性（M0-01 修复，此前是类级共享对象——一个实例停止会停掉所有实例的运行）。

### 2.4 技能 → 策略服务（每轮一次，阻塞 HTTP）
- **ActiveVLN**：`_chat`（`active_vln/skill.py`）→ vllm `:8003/v1/chat/completions`；
  消息 = SYSTEM_PROMPT（官方 R2R 语法，`eval/vlnce/eval_vlnce.py` 逐字对齐）+
  逐轮历史 + `PROMPT_TEMPLATE.format(instruction=...)`（**指令每轮都在提示里**）
  + 图像（`_image_to_data_url`：BGR→RGB，80k 像素预算，vLLM 侧无通道翻转）。
  历史由客户端 `conversations` 列表维护，每次调用全新（无跨任务污染）。
- **StreamVLN**：`_post_frame`（`stream_vln/skill.py`）→ `POST :5801/eval_vln`，
  multipart：`image` = **BGR 顺序 JPEG**（`_image_to_bgr_jpeg`；官方客户端契约，
  服务器端 `np.asarray(image)[...,::-1]` 翻回 RGB——此前我方发 RGB，模型看到的是
  红蓝反转的图像，M0-01 已修复）+ `json` = `{"reset": bool, "instruction": str}`
  （**指令随每次 POST 发送**；服务器仅在构建会话时——reset 后首次推理——注入，
  此后存在于 KV cache）。
- 服务器（打补丁副本 `streamvln-deploy/http_realworld_server.py`，与上游差异见
  `http_realworld_server.patch`）：读 `instruction`（缺省回退官方硬编码指令，
  向后兼容）；reset 时 `evaluator.reset_memory()`（清 KV cache/图像历史/记忆
  token）；每 POST 内部推进 4 步（仅 `step_id%4==0` 步做模型推理——SlowFast），
  返回至多 4 个动作 id；回答含 0 后 terminate 锁存（后续 POST 返回 [0] 直到 reset）。

### 2.5 动作解析与合并（同步纯函数）
- `stream_vln._parse_server_actions`：id∉{0,1,2,3} 或非 list → `ValueError`
  （拒绝，绝不静默映射为前进 25cm）；**全部动作消费**（此前 `[:3]` 截断丢弃第
  4 个动作，导致机器人执行步数与服务器内部推进失步——M0-01 已修复）。
- `active_vln._parse_actions`：文本解析出全部子动作，逐子动作钳到 75cm/45°
  （官方执行器 `min(3, magnitude/step)` 语义；不再截断列表）。
- `_coalesce`（共用）：同向合并；前进 >75cm 拆为 75+…，转向 >45° 拆为 45+…
  （带符号：左正右负）——**无损**，任何动作序列的总位移量在合并后守恒
  （此前 4×25cm=100cm 被钳到 75cm，静默丢 25cm——M0-01 已修复）。

### 2.6 执行：PD 速度归位（10 Hz，同步循环内阻塞）
- `_pd_drive`（`active_vln/skill.py`，两技能共用）：目标由 `_relative_goal`
  从当前 odom 生成（前进沿当前偏航、左转为正角度）；循环内读实时 odom，
  发布 `Twist` 到 `tele_cmd_vel`。
- 完成判定：`|e_p|≤0.05m` 且 `|e_r|≤3°`（turn_only 只判偏航）——以 odom 证据
  而非规划器"到达"状态判定；每个退出路径（完成/停止/odom 丢失/超时/卡死）
  都发布零 Twist。
- **阻尼项用实测速度**（odom 差分），与官方 pid_controller.py 一致（它用机器人
  上报的 [v, w]；此前我方用上一周期指令近似，在跟踪快于指令的平台上欠阻尼）。
- **命令频率是稳定性的一部分**：MuJoCo Go2 以 ~2-5× 跟踪指令速度（实测 0.75m
  用时 0.3s @ 0.5 m/s 钳制；单条 0.79 rad/s 指令转 16°）。官方 10 Hz 下每周期
  环路增益 ≈1.08 —— 实测持续继电器振荡、30 s 超时（证据 basic_actions_kp1/
  与早期 basic_actions 运行）；仿真栈配置 `drive_hz=50` 后全部基本动作通过
  （6/6，basic_actions/summary.json）。真实硬件（紧密速度跟踪）沿用官方 10 Hz。
- **A\* 绕过（明确的安全边界）**：PD 直接发速度指令，**无避障**。这是刻意设计：
  A* 规划器到达容差（0.2m/15°）≥ VLN 步长（25cm/15°），目标在接受瞬间即被判
  "完成"。替代安全机制：(a) 卡死检测 3s 放弃→策略看新视野重新决策；
  (b) MovementManager teleop 优先级——任何遥操消息（含零速）立即抢占；
  (c) 三层停止路径（§3）；(d) 仅在仿真中验证。**不要在真实机器人上直接使用
  未经复核的此路径。**

### 2.7 MovementManager（teleop 优先级复用）
- `tele_cmd_vel` 任何消息 → `_on_teleop`：置 `_teleop_active`、`_cancel_goal()`
  （发布 stop_movement + NaN 目标兜底）、缩放转发 `cmd_vel`。
- `nav_cmd_vel` 在 teleop 活跃且距上次 teleop <1s（`tele_cooldown_sec`）时丢弃。
- PD 的最终零 Twist 也走此通道：既停车又保持 nav 目标被取消的状态。

### 2.8 仿真回路
- `cmd_vel` → `UnitreeGo2ConnectionModule`（`connection.py:357-418`，`move()`）
  → MuJoCo 进程（共享内存）；MuJoCo 每步 `write_odom` → 连接模块发布 `odom`
  与 `color_image` → 技能订阅回调更新 `_latest_odom` / `_latest_image` →
  下一轮推理与 PD 步进。

---

## 3. 停止路径（三层，目标 <2s）

| 层 | 触发 | 路径 | 延迟构成 |
|---|---|---|---|
| 1. 输入线程紧急停止 | 聊天输入 ≤24 字符全词"停止"类 | `_on_human_input` → `_interrupt_movement_skills()`：直接调用 7 个停止技能（stop_stream_vln / stop_active_vln / stop_navigation / …）| 输入线程即达；技能清 `_running` + 发零 Twist + cancel_goal。PD 循环 ≤100ms（10 Hz）内退出 |
| 2. MCP 抢占 | 新技能调用与 CAP_MOVEMENT 持有者冲突 | `_preempt_capability_holders` → 调旧持有者的 stop 工具 → 重试获取能力 | 同上 + 一次工具调用 RPC |
| 3. 配对 stop 工具 | Agent/LLM 选择 stop_* 工具、或技能自身策略说停 | `stop_stream_vln` / `stop_active_vln`（清标志 + 零 Twist + cancel_goal）；策略输出动作 0 → 技能返回 "Policy said stop." | Agent 回合 + 技能退出 |

每个退出路径（正常完成 / 策略停 / 预算耗尽 / 服务器不可达 / 非法动作拒绝 /
相机丢失 / odom 丢失 / 卡死放弃 / 用户停止）都会发布零 Twist
（M0-01 修复补齐了 active_vln 的全部路径），`_running` 由 try/finally 释放。
停止延迟实测证据见 `basic_actions/steps.jsonl` 的 `urgent_stop_mid_drive` 记录。

---

## 4. 上下文维护与任务边界

| | ActiveVLN | StreamVLN |
|---|---|---|
| 指令注入 | 每轮提示（PROMPT_TEMPLATE），客户端维护 | 每次 POST 携带；服务器仅在 reset 后首次推理注入会话，其后在 KV cache 中 |
| 历史 | 客户端 `conversations` 列表（每任务新建） | 服务器 `VLNEvaluator`：past_key_values + rgb_list + 记忆 token（SlowFast 每 32 帧重置模型、抽历史帧） |
| 任务重置 | 每次调用全新会话 | 首帧 `reset=true` → `reset_memory()`；跨任务零残留 |
| 终止锁存 | 无（每轮独立） | terminate 锁存到下一次 reset；技能遇 0 即停 |

---

## 5. 闭环中断风险（现状）

已修复（M0-01，`dec467a`，均有单元测试）：
1. StreamVLN 指令从未到达模型（服务器硬编码 + 客户端不发送）→ 双端协议扩展。
2. `[:3]` 截断：整批动作失步（服务器每答推进 4 内部步）。
3. 非法动作 id 静默映射为前进 25cm → 拒绝。
4. `_coalesce` 前进溢出静默丢弃（100cm→75cm）→ 无损拆分。
5. RGB/BGR 通道反转（模型看到颜色反转的图像）。
6. 类级 `_running` Event 跨实例共享。
7. active_vln 相机丢失路径泄漏 `_running`；所有中止路径补零速。

残余风险（记录，不在 M0 修复范围）：
- PD 直驱无避障（见 §2.6 安全边界）；卡在障碍物上时依赖卡死检测（3s）放弃。
- 服务器 terminate 锁存与客户端提前放弃（卡死 break）叠加时，模型内部步计数与
  机器人实际步数可能漂移（官方客户端同样存在；下一帧观测可自愈）。
- 策略服务器单点（宕机→技能返回"unreachable"并停车，不自动重试）。
- MuJoCo 相机帧率与 PD 10 Hz 之间无严格同步（观测取最新帧，官方部署同构）。

---

## 6. 代码已实现 vs 运行时已验证

| 项 | 状态 | 证据 |
|---|---|---|
| 动作解析/合并/拒绝（两技能） | [已实现] 单元测试 44 例 | `pytest dimos/navigation/experimental/{active_vln,stream_vln}` |
| 指令上线（StreamVLN reset+instruction+BGR JPEG） | [已实现] 单元测试抓线验证 | 同上（test_reset_instruction_and_bgr_frame_reach_the_wire） |
| PD 归位/卡死/odom 丢失/紧急停止（含零速收尾） | [已实现] 单元测试 | 同上（test_pd_drive_*） |
| 服务器指令注入（打补丁副本） | [已实现]（与上游 diff 审查）| `streamvln-deploy/http_realworld_server.patch` |
| 基本动作 odom 证据（0.25/0.50m、±15°、序列、停止延迟） | [已验证] | `basic_actions/steps.jsonl`、`basic_actions/summary.json` |
| ActiveVLN 真实模型闭环 ≥3 次 | [运行时待验证] | `activevln_runs/` |
| StreamVLN 真实模型闭环 ≥3 次（含服务器收到指令的日志证据） | [运行时待验证] | `streamvln_runs/` |
| 停止 <2s（真实模型运行中） | [运行时待验证] | `streamvln_runs/` |
