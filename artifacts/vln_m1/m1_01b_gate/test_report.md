# VLN-M1-01B-GATE 测试报告（test_report.md）

提交 `9f91a8c`。全部 mock/离线/注入时钟——**零真实等待**，每个场景明确断言 VLN 调用是否发生。

## 执行命令

```bash
cd dimos
.venv/bin/python -m pytest dimos/navigation/experimental/ \
  dimos/agents/mcp/test_mcp_client_unit.py dimos/agents/mcp/test_mcp_server.py \
  dimos/agents/test_annotation.py dimos/agents/test_skill_result.py \
  dimos/agents/test_capabilities.py \
  dimos/web/relay_bridge/test_chat_encoding.py \
  dimos/web/relay_bridge/test_relay_bridge_authoring.py \
  -p no:cacheprovider --no-cov -q
# => 206 passed, 0 failed, 3 deselected
```

## §三 十项时序测试（test_agent_gate_timing.py，12 项全 PASS）

| # | 场景 | 结果 | VLN 调用断言 |
|---|---|---|---|
| 1 | 旧产物延迟 **1 秒**，任务回合已关闭 | **拦截**（主条件：调用存活性） | 0 次 ✓ |
| 2 | 延迟 **5 秒**，回合已关闭 | **拦截** | 0 次 ✓ |
| 3 | 延迟 **13 秒**（生产事故同龄、旧阈值内），回合已关闭 | **拦截**（旧门控会放行——本 GATE 的核心证明） | 0 次 ✓ |
| 4 | 延迟 **>15 秒** | 拦截（年龄辅助条件） | 0 次 ✓ |
| 5 | **新鲜图像属于当前有效调用** | **放行**，进入 LLM 可用（视觉能力不回归） | 0 次（用图 ≠ 起任务）✓ |
| 6 | 任务 A 的结果在任务 B RUNNING 时到达 | 拦截，不干扰 B | 0 次 ✓ |
| 7 | 用户显式第二条相同指令 | **允许**（新回合执行） | 由用户驱动（合法）✓ |
| 8 | 旧 run 的产物（哪怕 0 秒龄） | 拦截（闭回合即遗留物） | 0 次 ✓ |
| 9 | 任务停止后旧产物到达 | 拦截 | 0 次 ✓ |
| 10 | **同一工具结果重复投递**（回合关闭后） | 第二次拦截；历史恰保留 1 份 | 0 次额外 LLM 回合 ✓ |
| + | 守护：顺序两回合各自的活产物都放行 | 2 份都可用（不过度拦截） | 0 次 ✓ |
| + | 确定性证明：1/5/13/16/30s 注入 | 全部同步判定，全套件 <2s | 0 次 ✓ |

时间注入方式：`generated_at`/`invocation_started` 直接写消息元数据（模拟任意入队/排队延迟），无任何 `sleep(13)`。

## 回归（§五）

| 套件 | 结果 |
|---|---|
| M0 VLN 45 | PASS |
| M1-01 Task Runtime 26 | PASS |
| M1-01B Agent 消息 17（已按新门控语义适配 stub：`_invocation_lock`/`_turn_tool_calls` 注入 + `tool_result` 传递调用戳） | PASS |
| GATE 12（上表） | PASS |
| MCP：mcp_client_unit 19（含曾暴露死锁的 `test_a_tool_call_the_mcp_server_never_answers`——修复后通过）+ mcp_server | PASS |
| ToolStream/CapabilityRegistry | PASS |
| relay bridge authoring 40（agent 通道 paced/顺序）+ chat encoding | PASS |
| ruff | 仅剩 2 个 main 既有 RUF001（紧急停词表全角标点，stash 对照 HEAD 同在） |
| mypy | 仅剩 2 个 main 既有 init_chat_model overload（stash 对照 HEAD 同在）；GATE 新增文件 0 错误 |

## MuJoCo 复测

按约束**未运行**新的 MuJoCo episode（运行中的栈未触碰）；上一轮真实复测（栈 `20261118-140152-bdab`，M1-01B 代码）已证明：0 追赶回复、0 重复执行、5 次 LLM 回合（旧 ~30）。GATE 加固（调用存活性）不影响该结论的方向——新门控只会**更严格**（旧图连“阈值内”的路也被堵死）。如需端到端复证，建议下次栈重启后重跑“红色椅子”指令。
