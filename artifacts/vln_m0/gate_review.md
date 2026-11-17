# VLN-M0-GATE 审查报告（只读检查，不改代码、不运行机器人）

生成时间：2026-11-17 · 分支：`task/vln-m0-closed-loop-validation`

---

## 一、Git 检查

| 项 | 值 |
|---|---|
| HEAD | `823a9314e8ed029a90a7197adca5701f3843a366` |
| main | `79e8eae8c0a5df6f77a0399548cec46e9a1d3774`（未动、未推送、未合并） |
| 提交数 | 6（main..HEAD，见下） |
| diff --stat | 34 files, +3278 / −107 |
| diff --check | **1 处需注意**：`streamvln-deploy/http_realworld_server.patch` 内含上游原文件本身的行尾空白（diff -u 转录的上游代码行；`.py` 部署副本无此问题，属补丁转录性质，不影响应用） |
| status | 干净（仅未跟踪的 `dimos_repo_git_backup/`——git alternates 对象备份，不得删除，未纳入版本管理） |

### main..HEAD 的 6 个提交（完整 SHA）

| # | SHA | 主题 |
|---|---|---|
| 1 | `dec467a3386bdfcd240515715efb806cf9277eb4` | fix(vln): M0-01 — StreamVLN 指令/动作协议修复 + 实例级运行状态 |
| 2 | `4670fbeb67c23a7b7699cda7b9c1017c0e02aad0` | fix(vln): M0-02 — PD 实测速度阻尼 + 仿真 50Hz 命令率；实机运动证据 |
| 3 | `9a405e2bd229548ca300fb7b5357eef704af8477` | fix(mcp): background 技能 worker RPC 不再吃 120s 默认超时 |
| 4 | `2864649b68c013dc3c54672be372516d162811e0` | test(vln): M0-04 — 真实模型闭环证据（4×StreamVLN、3×ActiveVLN） |
| 5 | `ca0e04afb5bc4b17b5e9606b48e4e6c87f817fb9` | docs(vln): M0-05 — A–N 验收报告 + 风险清单 |
| 6 | `823a9314e8ed029a90a7197adca5701f3843a366` | fix(streamvln-deploy): 服务器 import 需要上游两条 sys.path |

---

## 二、MCP 后台任务生命周期（重点审查）

### 1. 哪一层设置了超时
- **注册层**：`dimos/dimos/agents/mcp/mcp_server.py:483-502`（`McpServer.on_system_modules`）为每个技能构造 `RpcCall`；`lifecycle == "background"` 的技能传 `rpc_timeout=BACKGROUND_SKILL_RPC_TIMEOUT`（24h = 86400.0），instant 技能传 `None`（回落默认）。
- **常量**：`dimos/dimos/agents/mcp/constants.py:25` `BACKGROUND_SKILL_RPC_TIMEOUT = 24 * 3600.0`（与 `build` 的既有 24h 同级）。
- **传输层回落**：`dimos/dimos/core/rpc_client.py:98-101`（`RpcCall.__call__` 把 `self._rpc_timeout` 透传给 `call_sync` 的既有 `rpc_timeout` 参数）；`dimos/dimos/protocol/rpc/spec.py:64-95`（`call_sync`：None 时按 `rpc_timeouts` 表 → 方法名 → `default_rpc_timeout`，**默认 120.0，`DEFAULT_RPC_TIMEOUT: float = 120.0`，spec.py:36**——修复未改默认，只给 background 逐调用豁免）。
- Agent 路径（HTTP→McpServer→uvicorn）无 120s 上限，不受影响；`dimos mcp call` CLI 的 `--timeout`（默认 30s）是调用方一侧的耐心值，由 runner 显式传大值。

### 2. 超时后由谁释放 CAP_MOVEMENT
- **修复前（缺陷）**：`mcp_server.py:286-289` `_handle_tools_call` 的 `finally: if caps_held: cap_registry.release_by_token(acquire_token)`——RPC 超时异常也走 finally，**在 worker 仍在驱车时释放能力令牌**（第二个调用者可拿走 movement；仲裁空洞）。这正是 `9a405e2` 的根因；修复后 background 技能 24h 才超时，正常 episode（分钟级）不再触发。
- 修复后路径下，成功返回时 `caps_held = False`（mcp_server.py:281-284），能力所有权移交给工具流生命周期（不再由本函数的 finally 释放）。

### 3. Worker 正常执行结束后如何释放
- 技能方法返回 → `@skill` 包装（`annotation.py:73-119` `_make_skill`/`_stamp_and_log`，打出 "SKILL <name> duration_ms" 日志行——episode 真实结束标志）→ `ToolStream.stop()`（`tool_stream.py:195-215`）发布 `dimos/tool_stopped` 帧，**帧上带 `acquire_token`**（tool_stream.py:156/211）→ McpServer 订阅回调 `_fan_out_to_sse_queues`（mcp_server.py:366-381）识别停止帧后 `cap_registry.release_by_token(token)`——**按 token 释放**。注册表语义（capabilities.py:121-136）：陈旧 token（已被同工具更新调用接管）匹配不到则什么都不释放，并唤醒 acquire 等待者。

### 4. Worker 异常退出如何处理
- **栈内 worker 崩溃（子进程死）**：`python_worker.py:204-206` `has_exited` 暴露退出状态；正常停机走 join→terminate→kill 三级（python_worker.py:346-368），模块逐个 `instance.stop()`（python_worker.py:420-427）——技能的 `stop()`（skill.py:310-320）清 `_running` 并发零 Twist。
- **遗留缺口（如实记录，非本分支引入）**：worker **异常**死亡时无路径发布 `tool_stopped` 帧——已持有的能力令牌会残留到栈重启（CapabilityRegistry 表项不被清）。影响：后续技能调用 CAP_MOVEMENT 会在 acquire 上等待/失败。这是上游架构既有行为，M0 未修（记录于报告 §六.5 与下文风险表）。
- **技能内异常**：两个 skill 的 `_run` 全路径 try/finally（`_running` 释放 + 每个退出路径发零 Twist，见 `test_active_vln.py::test_endpoint_error_stops_at_rest` 等 8 个异常路径单测）；RPC 侧异常值经 `call_sync` 检查 `isinstance(result, BaseException)` 后重抛（spec.py:92-94）。

### 5. 用户 stop 如何抢占能力
三层（`call_chain.md` §3 + 代码）：
1. **输入线程紧急停止**（`agent.py:287-293` `_on_human_input` → `_is_urgent_stop`（agent.py:504-528，≤24 字符全词：停止/停下/别动/停/stop/halt）→ `_interrupt_movement_skills`（agent.py:179-196）**在输入线程直接调用 7 个 stop 技能**，不等 LLM 回合）。实测（agent-send 路径）：投递→技能退出 ~19ms（运行日志 47.375→47.394），端到端含 CLI 启动 1.26s < 2s。
2. **MCP 抢占**：新调用与能力持有者冲突且持有者是 background 时 `_preempt_capability_holders`（mcp_server.py:144-166）先调 `_HOLDER_STOP_TOOLS` 映射（mcp_server.py:133）里持有者的配对 stop 工具（stop_stream_vln/stop_active_vln：清 `_running` + 零 Twist + `cancel_goal`，skill.py:323-333 / stream_vln 对应位），再重试获取；stop 前缀工具调用在能力忙时也走抢占（mcp_server.py:212-228）。
3. **Agent/LLM 显式调用配对 stop 工具**（同 2 的工具面）。

### 6. 资源泄漏 / 永久占用风险
- **令牌**：正常/停止/抢占路径全部按 token 归还（见 3/5）；唯一残留路径是 §4 的 worker 异常死亡（无停止帧）——**已知残余风险，栈重启即清**；stop 工具随时可抢回（抢占路径不依赖令牌归属）。
- **线程**：技能运行在 worker 内、PD 循环 10/50Hz `time.sleep` 驱动，所有退出路径离开循环；episode runner 的 CLI 线程 daemon 化（长时间 CLI 不阻塞记录）。
- **运动残留**：每条退出路径发零 Twist（单测逐路径断言末条 `== Twist.zero()`）；MovementManager 对任何 teleop 消息取消 nav 目标——零 Twist 双保险。
- 测试证据：`test_stream_vln.py`（拒绝/不可达/中断/批量中途停，各断言零速）、`test_active_vln.py`（端点错误/相机丢失/预算/odom 丢失/卡死/紧急停）、`test_tool_stream.py:362-371`（停止帧形状+token）、`test_capabilities.py`（注册表语义）、`test_mcp_server.py`（服务器分发）。真实停止实验：`streamvln_runs/ep3_stop`（agent-send）、`ep4_stop_mcp`（stop 工具）、`activevln_runs/ep3_stop`。

---

## 三、PD 控制器审查

### 1. 50Hz 修改的准确位置
- 默认值：`skill.py:125` `drive_hz: float = 10.0`（官方真机值，**未改**）。
- 仿真注入：`run_go2_streamvln.sh:107` `--streamvlnskill.drive-hz 50` 与 `run_go2_activevln.sh:111,126` `--activevlnskill.drive-hz 50`（各自附 102-105 行注释说明根因）；脚本头注释 `run_go2_streamvln.sh:98-101`。**只改了两个启动脚本的 CLI 参数**，控制器代码无 50 硬编码。
- 循环用值：`skill.py:529` `dt = 1.0 / cfg.drive_hz`。

### 2. 是否只对仿真生效
是。50Hz 仅以启动脚本 CLI 覆盖注入（`--*vlnskill.drive-hz` 走 ModuleConfig 覆盖链）；不传参数即官方 10Hz（真机默认）。M0-02 的反例留档：`basic_actions_kp1/`（10Hz 下 kp 缩放方案 5/6 失败——证明改增益不是正解，改频率才是）。

### 3. 速度反馈计算公式
`skill.py:537-553`（`_pd_drive` 内）：
```
dt_meas = t_now − t_prev            (>1e-3 才有效)
v_meas = (Δx·cos(yaw_prev) + Δy·sin(yaw_prev)) / dt_meas     # 前向轴投影
w_meas = wrapToPi(yaw − yaw_prev) / dt_meas                  # 偏航差分
```
- odom 相邻两帧差分（40Hz odom 实测，消息龄 4.4ms）；odom 静止/同帧 → 0。对齐官方 `pid_controller.py`（`solve(odom, target, vel)` 用机器人上报速度）。
- 控制律（`skill.py:567-574`）：`v = Kp·clip(e_p,±1) − Kd·v_meas`，`w = Kp·clip(e_r,±1) − Kd·w_meas`，再各自钳到 max；**无我方自创项**（审查时曾试过"方向守卫"限制反向指令，因破坏带惯性平台的正常制动而已移除，恢复官方形式）。
- 误差：`e_p` = 目标点在当前航向前向轴投影（skill.py:555-562）；`e_r` = wrapToPi(目标航向−当前)；turn_only 只闭合偏航环（原地转，四足转向位置漂移留给下一策略动作）。

### 4. 参数与单位（`ActiveVlnConfig`，两 skill 共享，官方同值）
| 参数 | 值 | 单位 |
|---|---|---|
| kp_trans / kd_trans | 3.0 / 0.5 | 1/s（误差 m → v m/s） |
| kp_yaw / kd_yaw | 3.0 / 0.5 | 1/s（误差 rad → w rad/s；实现里 e_r 以 rad 入式） |
| max_v / max_w | 0.5 / 1.2 | m/s / rad/s |
| done_m / done_deg | 0.05 / 3.0 | m / deg（完成判据，odom 证据而非规划器状态） |
| drive_hz | 10（默认）/ 50（仿真栈注入） | Hz |
| step_timeout / stuck_m / stuck_s | 30 / 0.03 / 3.0 | s / m / s（卡死放弃→策略重决策） |

### 5. MuJoCo 实测（`basic_actions/steps.jsonl`，50Hz，全部 odom 前后位姿留档）
| 用例 | 请求 | 实际 | 误差 | 耗时 | 指令数 |
|---|---|---|---|---|---|
| forward_0.25m | 0.25m | 0.2163m | −0.034m | 0.372s | 19 |
| forward_0.50m | 0.50m | 0.4523m | −0.048m | 0.538s | 27 |
| left_15deg | +15° | +12.29° | −2.7° | 0.289s | 15 |
| right_15deg | −15° | −13.22° | +1.8° | 0.496s | 25 |
| sequence(25cm+15°+25cm) | 0.50m/+15° | 0.4947m/+11.45° | −0.005m/−3.6° | 0.952s | 49 |
| urgent_stop | 停 | — | — | 0.124s | 停→末非零指令 −0.017s（同拍）；2s 窗内残余位移 0.0m |
判据：|Δdist − 请求| ≤ done_m+5cm；turn ≤ done_deg+0.5°（转向欠 2-3° 为完成阈值内提前收——设计行为，非超差）。

### 6. 真机误用风险（如实评估）
- **存在且已在代码/文档/报告三处警示**：仿真平台以 ~2-5× 跟踪指令速度（0.75m/0.3s @0.5m/s 钳制实测），10Hz 环路增益≈1.08 临界振荡——因此才有 50Hz 仿真注入。真机若跟踪紧密，官方 10Hz 是对的；**真机迁移必须先台架标定**（报告 §六.2 明确：不得把当前参数未经复核用于真机）。
- 防误用栅栏：默认值仍为官方 10Hz（50 只在启动脚本）；`_pd_drive` docstring（skill.py:493-515）写明平台差异与实测数据；`call_chain.md` §2.6 安全边界（PD 直驱**无避障**，仅限仿真，B 绕过 A* 的理由与替代安全机制）。

---

## 四、StreamVLN 服务端补丁审查

### 1. 基于哪个官方上游提交
- 上游树：`/tmp/StreamVLN` = **https://github.com/InternRobotics/StreamVLN.git** @ `e48f6ff7e9201d93aae003e8f64b04c00cec13bc`（"Merge pull request #62 from Sisyphus-hxy/patch-1"，2025-11-02）。
- 被补丁文件上游原始 sha256：`33d7721f041a352aa1b3af8a4d1fd456303465dce1a03f7c357cb858124c34e7`。

### 2. 补丁精确内容（`streamvln-deploy/http_realworld_server.patch`，196 行 diff）
三处（全部标注 "Patch N"）：
1. **指令直通**（核心）：读 `request.form['json']` 里的 `instruction`（缺省回退上游硬编码指令，向后兼容官方客户端）；结尾补句号以配合 evaluator 的 `<instruction>.` 拼接；每请求打印 `instruction: '...'`（部署证据）。
2. **STREAMVLN_CODE_ROOT**：`sys.path` 注入上游代码树——根（`streamvln` 包）+ 其 `streamvln/` 子目录（`model` 包，上游脚本原隐式 sys.path[0]）。部署副本不在上游 checkout 内，必须双条（`823a931` 修复了最初只有一条导致 `No module named 'model'` 的启动失败）。
3. **头注释**说明补丁性质。
**未动**：BGR→RGB 翻转（`np.asarray(image)[...,::-1]`，第 71 行）——刻意不改：它是官方客户端契约的一部分（客户端须发 BGR JPEG，dimos 客户端已对齐），改它会破坏官方客户端。

### 3. instruction 传递与消费路径
客户端 `stream_vln/skill.py::_post_frame`：multipart `json={"reset": bool, "instruction": str}` **每次 POST 都带**（单测 `test_reset_instruction_and_bgr_frame_reach_the_wire` 抓线验证）→ 服务器 `eval_vln()` 读出 → 传给 `evaluator.step(…, instruction, …)` → `VLNEvaluator.step`（`streamvln_agent.py:202-208`）仅在**构建会话时**（`output_ids is None`，即 reset 后首次推理）以 `sources[0]["value"].replace('<instruction>.', instruction_text)` 注入 prompt；此后指令存在于 KV cache（past_key_values），后续 POST 重复携带无副作用。消费证据：4 次真实 episode 的 `server_log_lines` 逐 POST 打印该任务指令（`instruction_reached_server: true` ×4）。

### 4. reset 实现
客户端每 run 首帧 `reset=true`（`_run` 里 per-run 记账，任务间零残留）→ 服务器 `policy_init` 分支：`terminate=False, idx=0, total_generate_time=0`，**`evaluator.reset_memory()`**（`streamvln_agent.py:87-99`：清 rgb/depth/pose/intrinsic/time_ids/action_seq/output_ids/past_key_values + `model.reset_for_env(0)`）→ 慢快记忆全清。证据：服务器日志 `init reset model!!!` + `!!!!!!reset memory!!!!!!`（每次 episode 开头）。

### 5. 动作序列 ↔ 机器人执行对应
- 服务器每 POST 内部 `for i in range(4): evaluator.step(run_model=(step_id%4==0))` —— **每答推进 4 内部步、仅第 4 步推理**（SlowFast），返回至多 4 个 id（0/1/2/3）。
- 官方客户端把整批链入复合目标（`incremental_change_goal(actions)`）；dimos 客户端等价：**全部消费**（`_parse_server_actions` 无 `[:3]`，`823a931` 前的截断 bug 已修）→ `_coalesce` 无损合并（4×25cm→75+25 两个目标）→ 逐目标 PD 归位（odom 证据）→ 下一帧。执行步数与服务器内部推进 1:1（这是 4.2 要求的核心语义，测试 `test_all_returned_actions_are_consumed`）。
- 回答含 0 → 服务器 terminate 锁存（后续 POST 返回 `[0]` 直到 reset）；客户端遇 0 停车返回 "Policy said stop."

### 6. 干净源码可复现性（本次实测验证）
`cd /tmp/StreamVLN && patch -p1 --forward < …/http_realworld_server.patch` → `apply exit 0`，**patched 文件与 `streamvln-deploy/http_realworld_server.py` 字节一致**（`diff -q` 通过），随后 `git checkout --` 还原上游树。README.md 给出完整运行手册（上游树获取、venv/torch/flash-attn 锁版、启动命令、端口、协议）。
**审查发现（轻微）**：`git diff --check` 对 `.patch` 文件报行尾空白——那是上游源文件本身带的历史空白（diff -u 逐行转录），不影响 `patch` 应用；`.py` 副本本身干净。

---

## 五、汇总：关键文件 / 证据 / 风险 / 合并建议

### 关键修改文件
- 技能与执行：`dimos/dimos/navigation/experimental/{active_vln,stream_vln}/skill.py`
- 测试：同目录 `test_active_vln.py`、`test_stream_vln.py`（45 例）
- 工具：`demo_vln_motion_check.py`（M0-02）、`demo_vln_episode_runner.py`（M0-04/05）
- MCP/生命周期：`dimos/dimos/core/rpc_client.py`、`dimos/dimos/agents/mcp/{mcp_server.py,constants.py}`
- 服务端补丁：`streamvln-deploy/`（py + patch + README）
- 启动脚本：`run_go2_activevln.sh`、`run_go2_streamvln.sh`
- 文档/证据：`artifacts/vln_m0/`（call_chain.md、report.md、basic_actions/、streamvln_runs/×4、activevln_runs/×3、basic_actions_kp1/ 反例留档）

### 各项测试证据路径
| 项 | 证据 |
|---|---|
| 45 单元测试（解析/合并/拒收/协议/PD/异常零速/实例隔离） | 源文件 + 运行命令在 `report.md` §四 |
| 基础动作 6/6（odom 前后位姿、误差、耗时、末条零速） | `basic_actions/steps.jsonl`、`summary.json` |
| StreamVLN 4 次真实闭环（指令到达/reset/整批消费/终止原因） | `streamvln_runs/{ep1,ep2,ep3_stop,ep4_stop_mcp}/episode.json` + `steps.jsonl` |
| ActiveVLN 3 次真实闭环 | `activevln_runs/{ep1,ep2,ep3_stop}/…` |
| 停止延迟（3 次中断实验，<2s） | 上三组 `episode.json:stop` + 运行日志时间戳（`call_chain.md` §3 表） |
| MCP 超时修复验证（>120s episode 完整返回） | `activevln_runs/ep1`（SKILL 行 285.8s）+ 提交 `9a405e2` |
| 补丁复现 | 本报告 §四.6（本次实测：patch→字节一致） |

### 已知风险（M0 报告已载）与残余风险（审查新增视角）
1. **worker 异常死亡→能力令牌残留**（上游架构既有，M0 未引入未修复；栈重启清；stop 工具可抢回）——建议列入下一阶段加固清单（如 worker 死亡→广播停止帧）。
2. PD 直驱无避障，仅限仿真（三层停止+卡死放弃兜底）。
3. `drive_hz=50` 是仿真平台参数——真机迁移前必须台架标定（默认 10Hz 未动）。
4. 场景无目标真值：终止标记只到 POLICY_SAID_STOP/STOPPED_BY_USER/TURN_BUDGET_SPENT，未虚构成功。
5. "运行中杀死策略服务器"的实机异常实验未单列（J 项以单测+中断实验佐证）。
6. `.patch` 文件内转录的上游行尾空白（无影响，可留意）。
7. Agent 大脑在紧急停后尝试全部 stop 工具（无害）。

### 是否建议合并 main
**建议合并**（有条件）：
- A–N 14/14 PASS 无阻塞；6 个提交均有明确问题→根因→修复→测试闭环；真实模型闭环 7 次 + 基础动作 6/6 全留痕可重放；上游补丁可字节级复现。
- 条件（不阻塞合并，合并前知悉即可）：§五"残余风险"1、3 已在 M0 报告与本文档记录，移交下一阶段（worker 死亡能力回收；真机 PD 标定）。
- main 未动；本任务（GATE）只读检查，未改任何生产代码、未运行机器人。

**结论维持 M0 验收报告原判：A–N 全 PASS；不开始 M1；不推送不合并（等待人工审核决定）。**
