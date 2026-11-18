# VLN-M1-01 验收报告 — VLN 任务生命周期管理与执行安全加固

分支：`task/vln-m1-01-task-lifecycle`（自 main @ `95ebead` 切出，main 未动）
实现提交：`d02041a`（设计文档、运行时、两技能接线、26 个新测试、M0 测试适配）
基线：main = `95ebeadb381a8d7d3b0e7a72ca771e568a79801f`（VLN-M0 合并态）

---

## 一、修改文件与提交

| 文件 | 改动 |
|---|---|
| `dimos/dimos/navigation/experimental/vln_task.py` | 新增：共享任务运行时（状态机/RunHandle/快照/取消） |
| `dimos/dimos/navigation/experimental/active_vln/skill.py` | 接入运行时；入口 try/finally 零速+关流；per-run 取消；`get_vln_task_status` |
| `dimos/dimos/navigation/experimental/stream_vln/skill.py` | 同上（两技能语义一致） |
| `dimos/dimos/navigation/experimental/active_vln/skill.py::_pd_drive` | 新增可选 `cancel` 事件参数（默认行为不变） |
| `…/test_vln_task.py`（新）、`…/test_active_vln.py`、`…/test_stream_vln.py` | 26 个新生命周期测试；M0 测试的 stub 适配 |
| `artifacts/vln_m1/m1_01/{design.md,test_report.md,report.md}` | 设计、测试报告、本报告 |

## 二、状态机设计（详见 design.md 第二部分）

```
CREATED → RUNNING → {STOPPING} → COMPLETED | FAILED | CANCELLED
```
- 每次任务唯一 `run_id`；终止原因独立记录：
  `POLICY_STOP / TURN_BUDGET_EXHAUSTED / USER_CANCELLED / POLICY_ERROR / SENSOR_UNAVAILABLE / EXECUTION_ERROR`。
- **COMPLETED 只表示任务正常结束，不等同导航目标到达**（无场景真值，报告不虚构成功）。
- 阶段 `INFERENCE / ACTION_EXECUTION / WAITING_OBSERVATION / STOPPING`；
  每次更新刷新 `updated_at` = 心跳；查询时超 30s 未刷新的 RUNNING/STOPPING 标记 `stale_s`。
- STOPPING ≠ 已停止：终态只由 run 循环真正退出时经 handle 落定（先写者胜），
  状态不靠 `_running.is_set()` 推断。

## 三、Agent 查询接口

两技能容器各注册 `get_vln_task_status(run_id: str | None = None) -> str`（`@skill`，instant，无能力占用）：
- 结构化 JSON（TaskSnapshot 全字段 + `stale_s`）；`run_id=None` 查最近一次（运行中/结束后均可）。
- 历史保留最近 8 条；worker 已死 → RPC 本身失败（不会谎报 RUNNING）；worker 挂死 → stale 标记。
- 进度经 `tool_update` 推 ToolStream progress 帧，不用大模型推理刷新状态。
- 蓝图按 `vln_policy` 只部署其一，无同名冲突（两容器同签名同语义）。

## 四、CAP_MOVEMENT 处理结果（对照任务书五种情形）

| 情形 | 结果 |
|---|---|
| 正常完成 | **已修复**：run finally 关流 → `dimos/tool_stopped` 帧带 acquire_token → McpServer 按 token 释放（M0 曾永久泄漏——本阶段以测试复现该缺口并修复） |
| 用户停止 | run 退出即关流释放；stop 工具的抢占-重试链随之成功 |
| 任务异常 | McpServer 异常路径 finally 释放（既有行为）+ 技能侧零速/关流 |
| RPC 超时 | 24h 兜底保留（M0 临时规避）；正常 episode 分钟级，stop 工具秒级抢占 |
| Worker 崩溃 | 优雅停机（Module.stop→关流）已覆盖；**硬死亡残留 = 已知限制**（需协调器级存活信号，属通用 RPC 架构改动，按任务约束仅提交设计+测试证据：`test_t14_hard_death_residue_is_bounded_and_recoverable` 证明残留有界且可由 takeover/stop 恢复；机器人侧安全由 cmd_vel 静默超时保障）——**验收 H 以"明确阻塞报告"形式满足**，不擅自重写架构 |

## 五、执行安全加固（任务书 §五）

1. **并发时序**：每 run 专属 cancel 事件；run 循环持局部 handle 引用（绝不读"当前 handle"指针）；推理返回后/每动作前/PD 每周期检查取消——过期结果整批丢弃（T07 证明）。同一技能任何时刻至多一个运动权限实例（RUNNING/STOPPING 均拒绝二次启动，T06）。
2. **零速度保障**：入口外层 try/finally 全路径发布零 Twist + cancel_goal + 关流；未预期异常被捕获并落定 FAILED/EXECUTION_ERROR（T08–T11 逐一验证）。
3. **状态与资源一致性**：STOPPING 与终态语义如上；快照先写者胜；释放走 token 而非工具名（陈旧 token 匹配不到即不误释放）。

## 六、与 M0 的兼容性

- 45 个 M0 测试全部通过；模型协议/动作语义/PD 参数零改动（`_pd_drive` 仅加可选 cancel）。
- M0 验收报告与运行证据（artifacts/vln_m0/）未触碰。
- 运行中的 DimOS 栈未停止、未重启（其代码为 M0 态，行为不变；新功能待下次启动生效）。
- `dimos/agents/mcp/` + `test_capabilities` 回归 47/48（唯一失败为已归因的环境性 LCM 问题，与本分支无关）。

## 七、验收对照（A–J）

| 项 | 结论 | 证据 |
|---|---|---|
| A 两策略一致状态模型 | **PASS** | 共享 vln_task.py；T15 参数化两侧同款断言 |
| B Agent 可查询运行状态 | **PASS** | get_vln_task_status；T03/T04 |
| C 每次执行唯一 run_id | **PASS** | uuid4；T13 |
| D 停止后不执行旧策略动作 | **PASS** | T07（迟到批整批丢弃） |
| E 无并发第二运动实例 | **PASS** | T06（RUNNING/STOPPING 均拒绝） |
| F 所有异常终止零速度 | **PASS** | T08/T09/T10/T11 + M0 既有异常路径测试 |
| G 正常完成后 CAP 正确释放 | **PASS** | T12（真 ToolStream + 真 CapabilityRegistry） |
| H Worker 崩溃风险 | **PASS（含阻塞报告）** | 优雅停机已修复；硬死亡残留有界可恢复（T14），完整修复需架构级改动——按约束仅设计+证据 |
| I M0 45 测试继续通过 | **PASS** | test_report.md |
| J 不改推理协议/动作语义/PD 参数 | **PASS** | diff 审查；M0 测试语义断言未变 |

## 八、遗留风险

1. **硬 worker 死亡的能力残留**（H 的阻塞项）：修复需协调器存活信号→代发停止帧，建议 M1 后续或专项处理；现残余有界（takeover/stop 抢占/24h 超时）。
2. 24h RPC 超时仍为兜底（M0 临时规避），在 H 完整修复前保留。
3. `get_vln_task_status` 同名存在于两容器，依赖蓝图单选部署（`vln_policy`）——两容器同栈部署会串名，已记录。
4. STOPPING 期间 stop 工具抢占重试的 6s 窗口（mcp_server 既有）若推理长时间阻塞，重试可能仍拒绝（机器人已停，能力稍后随 run 退出释放）——低频、非安全问题。
5. 运行中的栈仍是 M0 代码——新生命周期功能需下次启动生效。

## 九、结论

M1-01 验收 A–J 全 PASS（H 含明确阻塞报告）。未推送、未合并、未进入 M1-02，等待审核。
