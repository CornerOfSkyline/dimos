# VLN-M1-01B 验收报告 — Agent 异步消息时序与重复执行修复

分支：`task/vln-m1-01b-agent-message-lifecycle`（自 M1-01 `bb28ee0` 切出）
提交：`16a61b3`（P0 迟到产物关联+重复执行防护）、`f5f1a21`（P1 实时进度与 LLM 分流）
main 未动；M1-01 原分支保留；运行中的 DimOS/模型/仿真服务全程未触碰；未连接真实 Go2。

---

## 一、修复前完整事件时序（核实结论，见 root_cause.md）

单日志单时钟核实：`Agent message` 时间戳为**消费时刻**（`_record`），artefact 实际在
observe 调用时刻即入队。第 2 次 `active_vln` 的直接输入链：

```
04:32:41 用户指令 → active_vln #1（117s，11 轮，正常完成）
          ↳ 期间 12 条进度帧实时入队（Agent 阻塞无法消费）
04:34:41 任务 A 终止（能力正常释放）→ observe #1 artefact 入队（排 12 帧之后）
04:34:44–52 队列追赶：12 帧逐条触发 LLM×12（"正在走…"）
04:34:54 消费 artefact #1（13s 旧图，"任务已完成"语境下唯一新输入）
04:34:56 LLM 误判为需继续 → active_vln #2（53s 重复运动）
```

04:35:52–53 的 artefact 为第 2 次任务期间 observe 的产物，消费又晚于其结束——
同缺陷的后续无害呈现，非第 2 次调用的原因（时间顺序已核实）。

## 二、两个问题的根因

- **A（集中输出）**：单线程消息循环 + 阻塞型工具 + 进度帧入 LLM 队列 = 积压后逐帧推理。
- **B（重复执行）**：artefact 无来源/时刻标记、无新鲜度判定——旧图在错误语境到达 LLM。
  三个必要条件（帧入队、产物无标记、无陈旧判定）中任一被切断即防住；本修复切断两个。

## 三、修改文件与关键函数

| 文件 | 修改 |
|---|---|
| `dimos/agents/agent.py` | `_append_image_to_history`（+kind/generated_at 标记）；`_process_message`（TOOL_RESULT 新鲜度门控+丢弃日志+可见说明）；`_on_tool_stream_message`（P1 分流到展示流+结构化遥测）；`_vln_progress_telemetry`（新，解析 VLN 进度文本）；`_on_human_input`（USER_COMMAND 标记） |
| `dimos/agents/mcp/constants.py` | `MESSAGE_KIND_KEY`/`GENERATED_AT_KEY`/三类常量/`TOOL_RESULT_STALE_AFTER_S=15s` |
| `dimos/agents/mcp/test_mcp_client_unit.py` | 2 项测试更新为新路由语义 |
| `dimos/navigation/experimental/test_agent_message_lifecycle.py` | 新：T01–T12 17 项 |
| `artifacts/vln_m1/m1_01b/` | root_cause / design / test_report / report |

## 四、消息流向对比

修复前：进度帧→LLM 队列（积压=每帧一次推理）；artefact→队尾（旧图→重复执行）。
修复后：进度帧→`agent.publish` 展示流（0 LLM，cockpit 实时结构化遥测）；artefact→消费侧门控（新鲜进 LLM、陈旧丢弃留痕）；USER_COMMAND→永不拦截。
MCP 线上格式、tool_stopped 能力释放语义零改动。

## 五、验收对照（A–I）

| 项 | 结论 | 证据 |
|---|---|---|
| A 迟到产物不再重复执行 VLN | **PASS** | T05/T06/T08（陈旧产物丢弃，LLM 无由再触发）；机制+根因双切断 |
| B 实时进度不入 LLM 队列 | **PASS** | T01/T03（0 LLM 调用、队列空） |
| C Cockpit 实时收进度 | **PASS** | T02（阻塞期间 5 帧实时到达，结构化字段齐备） |
| D observe 视觉不回归 | **PASS** | T04（新鲜图照常进模型）+ T09 + 既有 19 项 MCP unit 测试 |
| E 显式重复指令允许 | **PASS** | T07（新 run_id；无指令判重） |
| F M1-01 状态机不回归 | **PASS** | T12 ×2 策略 + test_vln_task 26 项全过 |
| G 急停与能力释放不回归 | **PASS** | T11（tool_stopped 语义）+ T12（释放链）+ 急停路径代码未触碰 |
| H 测试全过/环境失败归档 | **PASS** | 158/158（本次连历史 LCM 环境失败也未复现）；mypy/ruff 残项均 stash 验证为 main 既有 |
| I 不改 VLN 模型/PD/M0 证据 | **PASS** | diff 仅 Agent 消息层+constants+测试；M0 证据未触碰 |

## 六、剩余风险与后续建议

1. **15s 新鲜度阈值为常量**（`TOOL_RESULT_STALE_AFTER_S`）：对极慢场景（长推理后紧跟的 observe）可能误弃新鲜图——可后续改为随工具声明可配。
2. **Agent 阻塞模型未改**（框架属性）：回合内无法响应输入（除急停直通道）；回合内流式插入需 LangGraph 级改动——最小设计已在 design.md §六留档，**未擅自重构**。
3. 遥测走聊天流为最小兼容方案；cockpit 后续可按 `additional_kwargs.vln` 渲染专用进度条 UI（前端增强，非本阶段）。
4. artefact 与 run_id 的显式关联未做（图像类产物经 MCP 通用注入，Agent 侧无 run 概念；如需，宜在 McpServer 透传 progress_token/工具上下文时携带——记录为候选）。
5. 24h RPC 超时、硬 worker 死亡残留等 M1-01 已知限制不变。
6. 建议栈重启后用原"红色椅子"指令做一次真实 cockpit 复测（预期见 test_report.md 末节）。

## 七、结论

M1-01B A–I 全 PASS。两个生产缺陷的根因均已定位、修复并以确定性测试覆盖；M0/M1-01 全量回归零失败。未推送、未合并、未进入 M1-02——等待审核。
