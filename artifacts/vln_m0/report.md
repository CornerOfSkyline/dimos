# VLN-M0 验收报告 — DimOS Go2 VLN 推理与运动执行闭环验证

日期：2026-11-17 · 分支：`task/vln-m0-closed-loop-validation` · 报告生成时的 HEAD：`2864649`
（报告本身随 `report` 提交入库；基线与最新 SHA 见下）

---

## 一、Git 状态

| 项 | 值 |
|---|---|
| 分支 | `task/vln-m0-closed-loop-validation`（自 main 切出） |
| 基线 SHA | `79e8eae`（main，已验证匹配任务要求） |
| 提交（按序） | `dec467a` M0-01 接口修复 · `4670fbe` M0-02 PD 修复+运动证据 · `9a405e2` MCP 超时修复 · `2864649` M0-04 真实模型闭环证据 |
| 变更文件 | 33 个（+3160/−107）：两个 skill + 两个测试文件 + 两个 demo 脚本（`demo_vln_motion_check.py`、`demo_vln_episode_runner.py`）+ `streamvln-deploy/`（服务器补丁副本+patch+README）+ `artifacts/vln_m0/`（证据与文档）+ `rpc_client.py`/`mcp_server.py`/`mcp/constants.py` + 三个启动脚本 |
| 工作区 | 干净（除未跟踪的 `dimos_repo_git_backup/`——git alternates 备份目录，不可删除） |
| main | 未改动、未推送、未合并（按任务约束，待审核） |

## 二、接口修复（发现的问题 / 根因 / 修法 / 对应测试）

以官方部署代码为准绳逐项核对（StreamVLN `realworld/go2_vln_client.py`+`http_realworld_server.py`、ActiveVLN `eval/vlnce/eval_vlnce.py`、官方 `pid_controller.py`）：

1. **StreamVLN 指令从未进入模型**。根因：上游服务器硬编码指令（`http_realworld_server.py:74`），且**上游官方客户端也不发送指令**（只发 `{"reset"}`）——这是官方真机部署自身的缺陷。修法：服务器读请求 `json.instruction`（缺失回退默认，向后兼容），客户端每次 POST 携带指令与 reset。**修复已入项目版本管理**：`streamvln-deploy/http_realworld_server.py`（打补丁部署副本，`STREAMVLN_CODE_ROOT` 定位上游代码树）+ `http_realworld_server.patch`（与上游精确 diff）+ `README.md`（复现方案）。测试：单测 `test_reset_instruction_and_bgr_frame_reach_the_wire`（抓线验证）+ 4 次真实运行的服务器日志（每次 POST 都打印该任务指令）。
2. **动作序列语义失步**。根因：服务器每答内部推进 4 步（SlowFast 每 4 步推理一次）并返回至多 4 个动作，官方客户端全部链接执行；我方客户端 `actions[:3]` 截断——机器人步数与模型内部推进失步且静默丢动作。修法：全部消费。测试：`test_all_returned_actions_are_consumed` 等。
3. **非法动作 id 静默映射为前进 25cm**（`STEP.get(a, 25.0)`）。修法：id∉{0,1,2,3} 或非列表 → `ValueError`，技能拒绝并停车。测试：`test_answers_outside_the_id_space_are_rejected`（10 组参数化）+ 真实路径拒绝后零速（`test_invalid_answer_is_rejected_without_moving`）。
4. **`_coalesce` 前进溢出静默丢弃**（4×25cm 被钳到 75cm，丢 25cm）。修法：无损拆分（75+25）；转向已按 45° 拆分。ActiveVLN 解析同时改为逐子动作钳到官方执行器上限（`min(3, magnitude/step)`→75cm/45°），不再截断列表。测试：`test_coalesce_never_drops_a_run_overflow`、`test_coalesce_is_lossless_in_total_magnitude`。
5. **图像通道反转**。根因：官方客户端发 BGR（cv_bridge 'bgr8'），服务器 `[...,::-1]` 翻回 RGB；我方发的是 RGB → 模型看到红蓝反转的图像。修法：`_image_to_bgr_jpeg`（线上字节序与官方一致，不缩放）。测试：`test_bgr_jpeg_keeps_the_official_wire_order`（红像素按 BGR 序落线）+ 抓线验证。
6. **类级 `_running: threading.Event` 共享**（两个 skill）：一个实例停止会停掉同类所有实例的运行状态。修法：`_running`/`_run_lock` 移入 `__init__`；active_vln 补齐重入守卫与 try/finally（相机丢失路径此前泄漏 `_running`）。测试：`test_run_state_is_not_shared_on_the_class` ×2。
7. **PD 阻尼项用指令速度而非实测速度**（官方用机器人上报 [v,w]）：在跟踪快于指令的平台上欠阻尼。修法：odom 差分实测速度作阻尼项，与官方逐项一致。测试：`test_pd_drive_converges_on_a_plant_tracking_faster_than_commanded`（2.8×/5× 增益+0.2s 动量滞后，50Hz 下前进/转向均收敛）。
8. **10Hz 命令频率在 MuJoCo 平台上临界不稳定**（实测平台以 ~2-5× 跟踪指令，环路增益≈1.08 → 持续继电器振荡、30s 超时）。修法：仿真栈配置 `drive_hz=50`（两个启动脚本注入），真实硬件默认仍为官方 10Hz。证据：`basic_actions_kp1/`（失败的 kp 缩放方案留档）vs `basic_actions/`（6/6 通过）。
9. **MCP background 技能 120s RPC 默认超时**（M0-04 运行中发现）：超时后调用方收到假错误、**且 finally 释放 CAP_MOVEMENT——worker 仍在驱车，能力仲裁出现空洞**。修法：`RpcCall(rpc_timeout=)` 逐调用透传 + MCP 注册 background 技能用 `BACKGROUND_SKILL_RPC_TIMEOUT`（24h）。验证：ep1 重跑完整返回（SKILL 行 285.8s）。
10. **停止延迟测量口径**：`agent-send` CLI 启动 ≈1.3s 混入端到端。修法：以运行日志 "Urgent stop from human input" 投递行时间戳为系统侧零点。证据：投递→最后非零指令 ~20ms（日志 47.375→47.394）。

## 三、仿真闭环（实际使用的模型、环境与每次运行）

**模型/服务**：StreamVLN `mengwei0427/StreamVLN_Video_qwen_1_5_r2r_rxr_envdrop_scalevln_v1_3`（Qwen1.5-1.8B，补入 tokenizer；打补丁官方服务器，GPU0，:5801）；ActiveVLN `Arvil/Qwen2.5-VL-3B_rl_r2r_4000`（vLLM，GPU1，:8003）；Agent 大脑 qwen3.8@远程 Ollama 172.23.63.9。**仿真**：MuJoCo 无头（EGL），`unitree-go2-agentic(-activevln)-cockpit` 栈，办公室场景，PD drive_hz=50。
**闭环**：MCP→技能→策略服务器（指令+reset+BGR JPEG）→动作解析/合并→PD 归位→`tele_cmd_vel`→MovementManager→`cmd_vel`→MuJoCo→odom/相机→下一轮。

| Episode | 指令（摘要） | 轮数 | 动作序列（末轮） | 位移 | 时长 | 终止原因 |
|---|---|---|---|---|---|---|
| streamvln_ep1 | Walk forward and immediately stop when you exit the room. | 8 | `[1, 0]` | 4.24m | 21.5s | POLICY_SAID_STOP |
| streamvln_ep2 | Walk forward, turn left, and stop after you pass the desk. | 8 | `[1, 2, 1, 0]` | 5.13m | 19.7s | POLICY_SAID_STOP |
| streamvln_ep3_stop | Turn right and walk toward the doorway, then stop. | 4 | （中断） | 1.32m | 12.6s | STOPPED_BY_USER（agent-send，投递→末非零指令 ≈0s；端到端 1.26s） |
| streamvln_ep4_stop_mcp | Move forward past the chairs and stop in the open area. | 5 | （中断） | 2.53m | 18.8s | STOPPED_BY_USER（stop_stream_vln 工具，1.41s） |
| activevln_ep1 | Walk forward and stop when you exit the room. | 33 | `turn/forward 序列… stop` | 1.18m | 320.1s | POLICY_SAID_STOP |
| activevln_ep2 | Turn right and walk forward, then stop near the plants. | 50 | `…move forward 75cm, move forward 25cm`（预算尽） | 1.73m | 391.6s | TURN_BUDGET_SPENT |
| activevln_ep3_stop | Move forward past the chairs and stop in the open area. | 6 | （中断） | 1.16m | 24.5s | STOPPED_BY_USER（agent-send，1.26s） |

逐轮完整动作序列与原始速度/odom 轨迹：各 `episode.json` + `steps.jsonl`。服务器指令到达证据：`server_log_lines`（每次 POST 打印该任务指令 + reset 行），`instruction_reached_server=true` ×4。

**基础动作（M0-02）**：`basic_actions/` 6/6 通过（0.25m/0.50m/±15°/序列 0.95s/紧急中断 0.12s），odom 前后位姿、时长、末条零速全部在案。

## 四、测试

| 命令 | 结果 |
|---|---|
| `cd dimos && .venv/bin/python -m pytest dimos/navigation/experimental/active_vln/test_active_vln.py dimos/navigation/experimental/stream_vln/test_stream_vln.py` | **45 PASS / 0 FAIL / 0 BLOCKED**（无 GPU mock：解析、合并、拒收、抓线指令/reset/BGR、PD 含卡死/odom 丢失/紧急停止/放大平台收敛、实例隔离、脚本化策略回合、每异常路径零速） |
| `.venv/bin/python -m dimos.navigation.experimental.active_vln.demo_vln_motion_check --out ../artifacts/vln_m0/basic_actions` | **6 PASS / 0 FAIL**（MuJoCo 实机闭环，odom 证据） |
| `.venv/bin/python -m dimos.navigation.experimental.active_vln.demo_vln_episode_runner --policy … --instruction … --out …` | 7 次真实模型运行（上表），全留痕 |

开发中出现过且已修复的失败（全部留档）：kp 缩放方案 5/6 失败（`basic_actions_kp1/`）；10Hz 振荡 30s 超时（早期 basic_actions 运行，PD 修复后消失）；ep1 首次 ActiveVLN 运行 MCP 120s 假超时（`9a405e2` 修复）。

## 五、验收（A–N 逐项）

| 项 | 结果 | 证据 |
|---|---|---|
| A. ActiveVLN 模型服务可用 | **PASS** | vLLM :8003 `/v1/models` 返回 `activevln`（GPU1）；3 次真实运行全程推理 |
| B. StreamVLN 模型服务可用 | **PASS** | 打补丁服务器 :5801（GPU0）；4 次真实运行全程推理 |
| C. ActiveVLN 指令进入模型 | **PASS** | 指令经 `PROMPT_TEMPLATE` 进入每轮 user 消息（单测抓消息验证）；ep1 33 轮响应与指令相关 |
| D. StreamVLN 指令进入模型 | **PASS** | 4 次 `server_log_lines` 逐 POST 打印该任务指令 + reset 行；`instruction_reached_server=true` ×4 |
| E. 离散动作解析正确 | **PASS** | 单测 45 例（解析/合并/拒收/无损）；真实运行执行与序列一致（odom 位移匹配） |
| F. Go2 基础位移与转向有效 | **PASS** | `basic_actions/` 6/6（odom 前后差与请求一致，误差在 5cm/3° 阈内） |
| G. 相机和 odom 持续更新 | **PASS** | odom 实测 40Hz（M0-02 测量）；每轮推理消费新帧；`steps.jsonl` 轨迹连续 |
| H. 动作后重新观测并推理 | **PASS** | 7 次 episode 多轮闭环（8/8/4/5/33/50/6 轮），逐轮答案随视角变化 |
| I. 用户停止指令有效 | **PASS** | 3 次中断实验（agent-send ×2、stop 工具 ×1），端到端 1.26/1.41s < 2s；系统侧投递→末非零指令 ≈0.02s（日志时间戳） |
| J. 模型异常后速度安全归零 | **PASS** | 单测覆盖全部异常路径（服务器不可达/端点错误/非法动作/odom 丢失/卡死→末条零 Twist）；真实中断实验以零速收尾。注：未单独做"运行中杀死服务器"的实机实验（残留项，见六） |
| K. ActiveVLN ≥3 次真实运行 | **PASS** | ep1（33 轮）/ep2（50 轮）/ep3（6 轮中断），单次远超 2 轮推理 |
| L. StreamVLN ≥3 次真实运行 | **PASS** | 4 次（≥2 条不同指令：4 条），D 项指令证据 + reset 证据 + 整批 4 动作消费（SlowFast 同步） |
| M. 测试记录可重放分析 | **PASS** | `steps.jsonl`（逐事件+原始速度/odom 轨迹）+ `episode.json`（回合摘要）+ 服务器日志行；全部入 git |
| N. 单元测试通过 | **PASS** | 45/45；mypy 2 源文件 0 错误；ruff 0 错误 |

**无 BLOCKED 项。** 模拟测试仅作为异常路径/协议的快速回归；A–L 的结论全部来自真实模型闭环运行。

## 六、风险与遗留（及下一步建议）

**遗留问题**
1. PD 直驱**无避障**（明确的安全边界，见 `call_chain.md` §2.6）：靠卡死检测（3s 无进展→放弃→策略重决策）兜底；仅限仿真。
2. MuJoCo 平台以 ~2-5× 跟踪指令速度；`drive_hz=50` 是**仿真栈参数**。真实硬件上需先以实测速度阻尼+官方 10Hz 做台架标定——**未经复核不得把当前参数用于真机**。
3. "运行中杀死策略服务器"的实机实验未做（J 以单测+中断实验佐证）；建议下一阶段补。
4. MuJoCo 场景无目标真值：策略说停 ≠ 导航成功，所有 episode 只标 POLICY_SAID_STOP/STOPPED_BY_USER/TURN_BUDGET_SPENT（未虚构 COMPLETED）。
5. 调用方中途断开时，background 技能的能力令牌由工具流生命周期持有直到其结束——停止工具可抢占恢复（架构现状，已文档化）。
6. `dimos mcp call` CLI 默认 `--timeout 30s` 小于长 episode 时长（runner 已显式传参）。
7. Agent 大脑（qwen3.8）在紧急停止后会尝试调用全部 stop 工具（无害，日志可见）。
8. StreamVLN 服务器 terminate 锁存与客户端卡死放弃叠加时，模型内部步计数可能与机器人短暂失步（下一帧观测自愈；官方客户端同样存在）。

**下一阶段依赖建议**（仅列出，不开始）：Episode Manager 可直接消费 `episode.json`/`steps.jsonl` 格式；仿真场景补目标真值以支撑 COMPLETED 判定；真机迁移前做 PD 参数台架标定；考虑把 `streamvln-deploy` 指令补丁向上游提 PR。

**按任务要求，M0 到此停止，不自动进入下一阶段。**
