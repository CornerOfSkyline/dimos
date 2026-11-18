# VLN-M1-01B 测试报告

分支 `task/vln-m1-01b-agent-message-lifecycle` · 提交 `16a61b3`（P0）+ `f5f1a21`（P1）
全部 mock/离线；并发测试 Event/构造式确定性门控（零 sleep 竞态）。

## 执行命令

```bash
cd dimos
.venv/bin/python -m pytest dimos/navigation/experimental/ \
  dimos/agents/mcp/test_mcp_client_unit.py dimos/agents/mcp/test_mcp_server.py \
  dimos/agents/test_annotation.py dimos/agents/test_skill_result.py \
  dimos/agents/test_capabilities.py dimos/agents/test_utils.py \
  -p no:cacheprovider --no-cov -q
# => 158 passed, 0 failed, 3 deselected
```

## 01B 新增测试（T01–T12，17 项全 PASS）

`dimos/navigation/experimental/test_agent_message_lifecycle.py`：

| 编号 | 测试 | 断言要点 |
|---|---|---|
| T01 | test_t01_progress_frames_never_reach_the_llm_queue | 10 条进度帧 → 队列空 + 10 条结构化展示帧（policy/turn/max/stage 全对）+ **LLM 调用 0 次** |
| T01 | test_t01_progress_telemetry_parses_and_non_vln_passes_through | stream_vln 文本解析正确；非 VLN 工具返回 None（原样通过） |
| T02 | test_t02_cockpit_receives_live_progress_during_a_blocking_skill | （模拟）阻塞技能运行期间，展示通道实时收到 5 帧 turn 1-5 |
| T03 | test_t03_post_completion_progress_cannot_start_tasks | 任务完成后 12 条迟来进度帧 → 队列空、LLM 0 次、历史无 active_vln |
| T04 | test_t04_fresh_observe_image_reaches_the_model | 新鲜 artefact（1s）正常进入 LLM 并可见——视觉能力零回归 |
| T05 | test_t05_late_observe_image_is_dropped_not_executed | 20s 旧图被丢弃：LLM 仅 1 次（原指令回合）、历史无 artefact、cockpit 收到带原因的说明帧 |
| T06 | test_t06_task_a_completion_then_late_products_stay_inert | 任务 A 完成 + 60s 迟到产物 + 迟到进度帧 → 全部惰性（LLM 1 次、队列空、旧图未达模型） |
| T07 | test_t07_explicit_second_command_runs_a_new_task | 用户显式重复相同指令 → 两个 USER_COMMAND 回合都执行（新 run_id 合法） |
| T08 | test_t08_post_stop_late_products_inert | 停止后迟来产物 → 无新推理、旧图未达模型 |
| T09 | test_t09_cross_turn_results_isolated | 无关工具（look_out_for）的新鲜 artefact 正常进入且携带正确 origin/kind，不串扰 |
| T10 | test_t10_non_image_tool_results_unchanged | 同步 ToolMessage 返回路径完全不过门控（历史原样保留） |
| T11 | test_t11_tool_stopped_frames_ignored_by_agent_progress_path | tool_stopped 帧不进 agent 展示也不进队列（专属 McpServer 释放路径） |
| T11 | test_t11_progress_frame_wire_shape_unchanged | MCP 线上帧形状（progressToken 绑定）与修复前逐字段一致 |
| T12 | test_t12_vln_lifecycle_regressions_intact ×2 策略 | M1-01 状态机（begin 拒绝/STOPPING/退出后新 run）零回归 |
| T12 | test_t12_capability_release_chain_intact | tool_stopped 帧 → 注册表按 token 释放，链路零回归 |
| — | test_concurrency_is_deterministic_gate_based | 门控纯时间算术、同步复现（确定性证明） |

## 更新的既有测试（2 项，语义随 P1 更新）

`dimos/agents/mcp/test_mcp_client_unit.py`：`test_tool_stream_notification_becomes_human_message`、`test_tool_stream_progress_frame_becomes_human_message` —— 从"帧入队为 HumanMessage"改为"帧发布到展示流、不入队"（新路由的规范断言，含 MESSAGE_KIND 检查）。

## 回归结果

| 套件 | 结果 |
|---|---|
| M0 45 个 VLN 测试 | PASS（test_active_vln + test_stream_vln 全数在 158 内） |
| M1-01 26 个生命周期测试 | PASS（test_vln_task 全数在 158 内） |
| 01B 17 个新测试 | PASS |
| MCP server / client unit / tool_stream | 47/48→**本次 158 全量中全过**；此前归因环境性的 LCM 5s 启动超时（M0-GATE 时在旧 main 复现过）本次未出现——栈空闲时段全绿，归因记录保持 |
| ruff | 仅剩 2 个既有 RUF001（M0 紧急停词表的全角标点，main 上同在）——非本分支引入 |
| mypy | agent.py 2 个既有 overload 报错（init_chat_model，M0 时代 ollama base_url 代码，stash 验证 HEAD 同在）——非本分支引入 |

## MuJoCo 真实仿真复测

按约束未主动复测（运行中的栈仍为 M1-01 代码，未重启未中断用户实例）。修复为纯 Agent 消息层，技能层/模型层/运动层零改动——建议下次栈重启后由用户在 cockpit 复跑"红色椅子"场景观察：① 执行期间进度应实时出现在聊天流；② 任务结束后不应再有"正在走…"的追赶回复；③ observe 旧图不应触发第二次任务（如再遇重复，stale 丢弃的日志行 `Dropped stale tool result` 会留下证据）。
