# Deep Agents T2、进程级恢复与 T6 扩展成本

日期：2026-10-19

本报告补齐 P0 收口后主路线的最小验证缺口。三项均使用真实远程模型
`deepseek-v4-flash`，证据保存在仓库外 `~/harness-lab-data/runs`。

## 1. T2：统一 Execution Playwright 工具

实现方式：在 Deep Agents worker 中增加 `browser_fixture` 工具，工具内部调用
本仓库统一的 `execution.browser.smoke`，浏览器运行在共享 Execution 环境，
不在候选进程内。候选负责工具选择、参数传递和结果回传。

真实运行 run_id `deepagents-browser-05f85713-6d0e-4aea-bf75-0ab3e6a5f4cb`：

```text
events: process_started → graph_started → graph_update ×5
        → graph_completed → process_exited
answer: accepted:harness-lab
screenshot: .../browser/<run_id>/t2-browser.png (exists)
```

判定：**PASS（适配层）**。这证明 Deep Agents 可以通过统一 Execution 浏览器工具
完成 T2，不代表候选有原生浏览器；浏览器会话恢复和候选进程内浏览器仍不在本证据内。

复现：`scripts/run_deepagents_browser.py`；实时测试
`tests/adapters/test_deepagents_browser_live.py`。

## 2. 工具副作用后的进程级暂停与恢复

实现方式：图在 `interrupt_after=["tools"]` 安全点暂停，worker 进程退出；
新进程用同一 `thread_id` 和 SQLite checkpoint 恢复。任务先调用
`fixture_effect` 写入一条幂等副作用，再调用 `fixture_lookup`。

真实运行 run_id `deepagents-recovery-220a32f8-e92e-4fd3-9288-d1ca568d8fa0`：

```text
first process:
  ... → pause_requested → graph_paused → process_exited
  pause_kind: after_tools
  approval_required: false

resume process:
  ... → graph_resumed → graph_update ×4
  → graph_completed → process_exited
  answer: fixture-alpha

fixture_effect_count: 1
duplicate_side_effects: 0
```

判定：**PARTIAL/PASS（进程级）**。已证明进程级暂停、checkpoint 恢复和副作用
幂等；尚未验证 Runner/Execution 容器被杀后的恢复。

复现：`scripts/run_deepagents_side_effect_recovery.py`；实时测试
`tests/adapters/test_deepagents_recovery_live.py`。

## 3. T6：新增一个工具、一个审批节点和一个状态节点

扩展内容：

- 工具：`t6_word_count`；
- 审批节点：`interrupt_on={"t6_word_count": True}`；
- 状态节点：`T6Middleware.before_agent` 写入 `t6_stage="started"`。

真实运行 run_id `deepagents-t6-9c6a7913-b7d6-47b4-8a25-75cc72b7ea7a`：

```text
answer: 3
approval_interrupted: true
state_node_value: started
event_coverage: 9 types
first_run_seconds: 4.597
t6_core_lines: ~36 (worker + adapter T6-specific blocks)
changed_files: worker.py, adapter.py, script, live test
```

判定：**PASS（结构测量）**。这是一次单模型、单次运行的结构性成本记录，
不是人日或统计意义上的成本结论。T6 扩展本身首次运行即通过，没有额外调试。

复现：`scripts/run_deepagents_t6.py`；实时测试
`tests/adapters/test_deepagents_t6_live.py`。

## 4. 更新后的主路线边界

已补齐：

- 统一 Execution Playwright 的 T2（适配层）；
- 工具副作用后的进程级暂停/恢复和幂等；
- 工具 + 审批 + 状态节点的扩展成本结构测量。

仍未验证：

- Runner/Execution **容器级**重启恢复；
- 模型调用中途的即时抢占、cancel、实时 `send_input`；
- 浏览器会话在暂停/恢复后的重建；
- 归一化的每任务 token/费用；
- 多租户隔离和真实业务任务效果。

因此主路线推荐不变，但“下一步是否需要原型化”仍取决于容器级恢复和即时控制
这两项。
