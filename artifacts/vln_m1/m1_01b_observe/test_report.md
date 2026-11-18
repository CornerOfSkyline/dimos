# VLN-M1-01B-OBSERVE 测试报告

提交 `37ddb68`。全部走真实调用链（真 LangGraph 执行器 + 真 call_tool + 真队列 + 真门控）；仅 MCP HTTP 应答与 LLM 为脚本化假件；零 ledger 手术。时钟坍缩补偿（生产回合间隔=LLM 秒级延迟 vs harness 毫秒坍缩）已在测试内文档化为“窗口前移/stamp 回拨”两种形式——均为**消息/窗口状态的真实等价设置**，非门控绕过。

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
# => 218 passed, 0 failed, 3 deselected
```

## 第一步复现证据（test_observe_reproduction.py，修复后转绿）

| 测试 | 修复前 | 修复后 |
|---|---|---|
| 真实链路单回合 observe | `artefact_in_history=False`（图像被自家回合关闭判死） | **True**（进模型历史） |
| 两连续回合两图像 | `artefacts_in_history=0` | **2** |

记录的完整真实时序（修复前打印）：invocation_begin → image_queued（流内）→ turn_close_after → 消费（门控丢弃）。全部四项时间戳由测试留痕，无任何手动登记构造。

## T01–T10（test_observe_lifecycle.py，12 项全 PASS）

每项断言四要素：图像→模型？新 LLM 回合？VLN 调用？运动/任务态。

| # | 场景 | 图像进模型 | 额外回合 | VLN 调用 | 结论 |
|---|---|---|---|---|---|
| T01 | 回合内同步返回图像 | ✓ | 1（消费回合） | 0 | 正常 observe 完整可用 |
| T02 | 流内排队、回合关闭后消费（close→consume 顺序显式验证） | ✓ | 1 | 0 | 生产时序本身，窗口判定放行 |
| T03 | 后台延迟图像（先任务标识后图） | ✓（最新调用之图） | 1 | 0 | 后台语义=发起时刻，不看到达时刻 |
| T04 | 旧任务图像在新任务期间到达 | ✗ 丢弃 | 0 | 0 | 跨窗口 leftover 拦截 |
| T05 | VLN 完成后 observe 结果核验 | ✓（新调用新图） | 1 | 0 | 核验可用；**不会**自动重执行（对抗性模型也看不到旧图，T06 结构性证明） |
| T06 | 旧任务图像对抗性触发第二次 active_vln | ✗ | 0 | **0** | 门控先于模型，对抗剧本无从执行 |
| T07 | 用户显式重复相同指令 | — | — | **1**（真工厂构造的假 active_vln 工具全链执行） | USER_COMMAND 永不拦截 |
| T08 | 同名连续调用结果隔离 | 2（各自图）✓；c1 迟到图 ✗ | 0（迟到图被拦） | 0 | superseded 判定 |
| T09 | 重复投递两形态 | 即时：+1 有界回合；延迟：✗ | +1/0 | **0**（两形态均无运动） | 边界语义如实断言 |
| T10 | 用户停止后图像迟到 | ✗ | 0 | 0 | 停止后产物惰性 |

## 回归（任务书 §六 全项）

| 套件 | 结果 |
|---|---|
| M0 45 | PASS（含 PD/协议零改动断言） |
| M1-01 26（Task Runtime/能力释放） | PASS |
| M1-01B 17（消息分类/进度分流） | PASS |
| GATE 12（更新至窗口语义：S1-S3 由窗口差拦截；S10 两形态） | PASS |
| OBSERVE 12（上表+复现 2） | PASS |
| MCP（client unit 19 含曾抓死锁的永不应答测试）+ mcp_server | PASS |
| CapabilityRegistry / ToolStream / annotation / skill_result | PASS |
| relay-bridge authoring 40 + chat encoding | PASS |
| ruff | 仅 2 个 main 既有 RUF001（紧急停词表全角标点） |
| mypy | 仅 2 个 main 既有 init_chat_model overload |

## MuJoCo 复测

未运行新 episode（约束：不动运行栈）。修复影响面=消息层图像判定，与运动链路零交集；建议下次栈重启后补一条“observe 后让模型描述看到什么”的指令做端到端确认（该能力在 GATE 期间被静默丢失，本修复恢复）。
