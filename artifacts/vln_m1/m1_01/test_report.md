# VLN-M1-01 测试报告

分支 `task/vln-m1-01-task-lifecycle` · 提交 `d02041a` · 全部使用 mock/fake 后端，无真实模型服务器与机器人。

## 执行命令

```bash
cd dimos
.venv/bin/python -m pytest dimos/navigation/experimental/ -p no:cacheprovider --no-cov -q
# 84 passed, 3 deselected (M0 的 45 个 + M1-01 的 26 个 + 同目录其他既有 13 个)
.venv/bin/python -m mypy dimos/navigation/experimental/vln_task.py \
  dimos/navigation/experimental/active_vln/skill.py \
  dimos/navigation/experimental/stream_vln/skill.py
# Success: no issues found in 3 source files
.venv/bin/python -m ruff check dimos/navigation/experimental/   # All checks passed
```

## T01–T15 结果（26 个新测试，全部 PASS）

| 编号 | 测试 | 结果 | 验证内容 |
|---|---|---|---|
| T01 | test_t01_lifecycle_transitions | PASS | CREATED→RUNNING→{STOPPING}→终态全链路；STOPPING 在取消瞬间即出现（request_cancel），终态由 handle 落定 |
| T01 | test_t01_illegal_transitions_rejected | PASS | CREATED→COMPLETED 非法转换抛 ValueError；终态后 finish 为先写者胜的静默 no-op |
| T01 | test_t01_finish_is_first_writer_wins | PASS | 迟到的 finish 不改写终态 |
| T02 | test_t02_completion_and_retention | PASS | 正常结束后按 run_id 仍可查；默认查最近一次 |
| T02 | test_t02_history_is_bounded | PASS | 历史保留 8 条，超龄剔除 |
| — | test_stale_running_snapshot_is_flagged | PASS | RUNNING 超 30s 未刷新 → stale_s 标记；终态永不标记 stale |
| T03 | test_t03_query_during_inference | PASS | 推理阻塞中（Event 门控）查询：RUNNING/INFERENCE/turn 1/指令正确 |
| T04 | test_t04_query_during_action_execution | PASS | 动作执行中（Event 门控）查询：stage=ACTION_EXECUTION |
| T05 | test_t05_user_stop_cancels_and_settles | PASS | 停止后 CANCELLED/USER_CANCELLED、零速、cancel_goal |
| T06 | test_t06_duplicate_start_refused_until_exit | PASS | RUNNING 拒绝二次启动；STOPPING（取消在途）同样拒绝；完全退出后新 run 可启动 |
| T07 | test_t07_stale_result_after_cancel_never_drives | PASS | 推理阻塞中取消、迟到的动作批返回 → 整批丢弃零执行；后续任务 B 正常执行 |
| T08 | test_t08_policy_error_activevln | PASS | 推理异常 → FAILED/POLICY_ERROR + 零速 + 关流 |
| T09 | test_t09_executor_exception_settles_failed | PASS | 执行器异常 → FAILED/EXECUTION_ERROR（error_message 留痕）+ 零速 + 关流 |
| T10 | test_t10_sensor_unavailable | PASS | 启动前相机/odom 缺失拒绝启动；运行中相机丢失 → FAILED/SENSOR_UNAVAILABLE + 零速 |
| T11 | test_t11_unexpected_exception_zero_velocity_and_recovery | PASS | 真实 _execute→_pd_drive 链内未预期异常 → 零速、FAILED、关流；下一 run 正常启动 |
| T12 | test_t12_capability_released_via_tool_stopped_frame | PASS | 真实 Module.start_tool/stop_tool + 真 ToolStream（内存总线）：结束发布带 token 的 dimos/tool_stopped 帧，真 CapabilityRegistry 据此释放 |
| T12 | test_t12_without_the_frame_the_hold_leaks_until_takeover | PASS | M0 缺口复现（无帧=泄漏）+ stop 工具 takeover/释放恢复路径 |
| T13 | test_t13_stale_run_id_queries_and_new_runs | PASS | 旧 run_id 查询只读历史；新 run 不受旧身份影响；request_cancel 只信号当前 handle |
| T14 | test_t14_graceful_module_stop_closes_the_stream | PASS | 优雅停机（Module.stop）关闭活动流 → 帧发布 |
| T14 | test_t14_hard_death_residue_is_bounded_and_recoverable | PASS | 硬死亡残留：其他工具被拒、同工具 takeover + 释放可恢复（已知限制的测试证据） |
| T15 | test_t15_policies_agree_on_semantics ×2 | PASS | 两策略同款断言：状态/阶段/取消/终态/关流/零速语义一致 |
| T15 | test_t15_policy_stop_semantics_agree ×2 | PASS | 策略说停 → COMPLETED/POLICY_STOP（不等同导航成功） |
| T15 | test_t15_turn_budget_semantics_agree ×2 | PASS | 预算耗尽 → COMPLETED/TURN_BUDGET_EXHAUSTED |

并发测试全部使用 `threading.Event`/自定义 Gate（`inference_entered`/`release_inference` 等）做确定性门控，无 sleep 时序碰运气；pytest 线程关闭检查开启（T06 曾抓到未汇合线程并推动修复）。

## M0 兼容性（验收 I）

45 个 M0 测试**全部继续通过**（改动仅为：stub 补齐 runtime 与 tool-stream 三件套的 fake、`_run` 直调改为公入口调用、停止信号从清 `_running` 改为 `request_cancel()`——与实现语义同步）。模型协议（BGR/reset/指令）、动作语义（解析/合并/拒收/整批消费）、PD 参数（增益/钳制/阈值/速率）零改动；`_pd_drive` 仅新增可选 `cancel` 参数，缺省行为与 M0 完全一致。

## 相关回归

- `dimos/agents/mcp/` + `dimos/agents/test_capabilities.py`：47/48 通过；唯一失败为 `test_tool_stream_agent`（LCM handler 启动 5s 超时）——已在 M0 合并前在旧 main 上逐条复现，属环境性既有问题，与本分支无关。
- mypy：3 源文件 0 错误；ruff：0 错误。
- 运行中的 DimOS 栈未重启、未触碰（其上工具列表不变）；新代码经蓝图导入检查：两技能容器均注册 `get_vln_task_status`。
