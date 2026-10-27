# OpenHands 实验报告

**版本** openhands-sdk 1.56.0，提交 `33d8aa29c3078d074799e2fbf5e847aeb2d71360`（MIT）
**本轮深度** 初筛 + 详细五类（网页、扩展、两类规则已补齐）
**定位** 会话持久设计最完整，但本轮只测了核心库入口

[返回框架索引](README.md) · [总体实验设计](../02-experiment-design.md)

---

## 一、测试对象与入口 —— 先划清边界

**这里最容易出错，所以最先说。** OpenHands 有多个产品层次，本轮**只用了其中一个**：

| 组件 | 本轮是否使用 |
| --- | --- |
| `software-agent-sdk` 的 `openhands-sdk` | ✅ 使用 |
| `OpenHands/OpenHands` 的 Agent Canvas（前端/编排） | ❌ 不安装、不构建 |
| Agent Server（REST/WebSocket） | ❌ 不使用 |
| 内置 terminal / browser 工具 | ❌ 不使用 |
| Docker-in-Docker / 宿主 Docker socket | ❌ 不需要 |

来源核验里明确记录了 `used_source_ids: [sdk]`、`unused_source_ids: [canvas]`。**不能把未安装的 Agent Server 或远程 Workspace 的能力计入本入口。**

**入口**：公开的 `LLM`、`Agent`、`Conversation` 和本地 Workspace。模型、Agent 循环和工具执行都在候选容器内。`NeverConfirm` 让已授权的普通操作自动执行。

---

## 二、能力边界总表

| 能力项 | 框架自带 | 实验补充 | 本轮结论 |
| --- | --- | --- | --- |
| Agent 循环 | ✅ `Conversation` | — | 正常 |
| 工具执行器 | ✅ 官方工具执行器 | 工具名与参数结构接线 | 正常 |
| 会话持久化 | ✅ `Conversation` 的 `persistence_dir` + `conversation_id` | 未启用 | **未验证** |
| 跨容器继续 | ⚠️ 官方有 pause/persistence 示例 | 未接 | **未验证** |
| 工具执行前拦截 | ❌ 无 guard | 无 | 拒绝只靠服务端 |
| 浏览器接入 | ⚠️ 有内置 browser 工具，本轮未用 | — | 未验证 |
| 业务扩展 | ❌ 无 | **未接通** | 见 4.1 |
| 多租户 | ⚠️ 本地/远程 workspace 隔离方式不同 | 未接 | 未验证 |
| 模型计费防护 | ❌ | 独立计数代理 | 可用 |

**注意一个配置上的事实**：`configs/candidates/openhands.yaml` 里的 `display_name` 写的是 “OpenHands SDK / Agent Server”，但本轮入口**没有运行 Agent Server**。本报告统一称 **OpenHands**，并明确只评价 SDK 核心入口。

---

## 三、逐项实验

### 实验一：独立运行与额度

**效果**：两个新环境均完成真实模型与工具调用，2/2。模拟累计限额通过。

### 实验二：多步工具业务 —— 分页问题再次出现

**首轮结果**：两轮均通过（6、7 次请求）。

**本轮补充测试**（新数据）：

| 商品数据 | 结果 | 模型请求 | 失败项 |
| --- | --- | ---: | --- |
| 第一组（首轮） | 通过／通过 | 6、7 | — |
| **第二组**（本轮新增） | **未通过** | 6 | `all_pages_read` |
| **第三组**（本轮新增） | **通过** | 8 | — |

第二组的失败模式与 Pi 的第三组完全一致：**15 项检查里 14 项通过，只有 `all_pages_read` 失败**。报告正确、三笔写入正确、作用域和审计都完整，就是分页不合规。

**跨框架观察**：Pi（补充组 1 次）、OpenHands（补充组 1 次）、Deep Agents（多组反复）、OpenClaw（多组）。四条不同框架、不同接入方式，都在同一约束上翻车。这更像**任务约束与模型行为的匹配问题**，而不是某个框架的实现缺陷。这一点在汇总篇只按框架分别记录，容易读成“某个框架不稳定”，实际是共性问题。

### 实验三：网页操作 —— 已接通，3/3 通过

**框架自带**：OpenHands 依赖里本来就带 `mcp==1.28.1` 和 `fastmcp`，不需要新增依赖。

**实验补充**：官方的 `ClientTool` 执行器是**同步**接口，而 MCP 客户端是异步的。这里没有伪造同步 API，而是用专用事件循环加线程，把真实异步 `call_tool` 投递过去。工具名、描述、schema 全部来自 MCP 服务。

**一个需要修的真实差异**：OpenHands 会把**模型没提供的参数填成 `null`** 再交给工具，而 MCP 服务按 JSON Schema 校验不接受 null，导致 `browser_snapshot` 反复报 `expected string, received null`。修法是只剔除 `null` 值，不补默认值——这不改变任何真实取值。

**效果**：

| 商品数据 | 结果 | 模型请求 |
| --- | --- | ---: |
| 第一组 | 通过 | 20 |
| 第二组 | 通过 | 17 |
| 第三组 | 通过 | 19 |

**3/3 通过。** 第一组正好用满 20 次，没有重试空间。

### 实验四：业务扩展、规则与控制

#### 4.1 新增工具与持久业务状态 —— 已接通，3/3 通过

**本轮已补齐，而且印证了此前的一个推测。**

先前报告里写过：“OpenHands 的 `Conversation` 本身就有 `persistence_dir` 和 `conversation_id`，官方还提供 pause/persistence 示例，可能比其他初筛框架更容易补上扩展。”现在可以确认：**业务状态确实能直接写进原生 Conversation 快照**，不需要另建一套存储。

**效果**：6、6、6 次请求，**3/3 通过**。三个种子的请求数完全一致，是所有路线里最稳定的。

#### 4.2 敏感操作拒绝 —— 本轮新增，通过

**本轮补充测试**（新数据）：

| 项目 | 结果 |
| --- | --- |
| 判定 | **通过**（11/11 项） |
| 模型请求 | 2 |
| 业务写入 | 0 |
| 拒绝所在层 | **`fixture_execution`（业务服务端 403）** |
| 候选层是否有拒绝记录 | **无** |

接入说明里原本的表述需要修正：它说“敏感任务的实验状态及可信 deny 事件**还须后续公共编排接入和验收**，本初筛入口不据回答文字伪造拒绝判定”。本轮补充实验**完成了这项验收**：确实产生了可信的 deny 事件，且来自真实执行（`ledger` 有 `sensitive_export` 的 403 记录），不是模型口头拒绝。

但同样地，**拒绝发生在服务端，不在框架层**。

#### 4.3 取消 —— 未接通

编排层拒绝。`Conversation` 有停止路径，本轮未接线。

---

## 四、本轮效果汇总

| 实验 | 结果 | 来源 |
| --- | --- | --- |
| 一 独立运行 | 2/2；限额通过 | 首轮 |
| 二 多步工具 | 首轮 2/2；**本轮补充 1/2**（第二组分页失败） | 首轮 + 本轮 |
| 三 网页 | **3/3 通过**（20／17／19 次） | **本轮补齐** |
| 四 敏感拒绝 | **3/3 通过**（各 2 次，服务端层） | **本轮补齐** |
| 四 扩展 | **3/3 通过**（各 6 次） | **本轮补齐** |
| 四 超量拒绝 | **3/3 通过**（各 2 次） | **本轮补齐** |
| 四 取消 | **未接通**（编排拒绝） | 本轮尝试 |

**本轮正式运行**：15 次（五类 × 三组），共 104 次模型请求，**15/15 通过**。

**已知失败**：只有此前补充测试里的一次分页违规（第二组）。正式三组未再出现。取消用例未接通属于实验限制。

---

## 五、适用条件与后续

**适合**：需要完整会话对象和持久化设计、且愿意自己接控制的场景。`Conversation` 的持久化模型是本轮所有初筛框架里最接近“可恢复”的。

**必须先说清的不是能力，而是边界**：本轮只测了 SDK 核心。Agent Server、Canvas、内置浏览器、远程 Workspace 全部没有测。**任何关于“OpenHands 能做什么”的结论都只限于这个入口。**

**本轮已确认的一点**：业务扩展可以直接写进原生 Conversation 状态，接入成本是四条补齐路线里最低的（依赖已含 MCP，接口是现成的）。

**如果要继续，优先做两件事**：

1. 用 `persistence_dir` + `conversation_id` 验证跨容器继续。这是它与其他初筛框架最大的潜在差异点，本轮只验证了“状态能写进去”，没有验证“换容器能接着跑”。
2. 明确是否要评估 Agent Server 入口——如果需要服务化控制，那是另一套材料。

---

## 六、来源

- 候选配置：[configs/candidates/openhands.yaml](../../../configs/candidates/openhands.yaml)
- 接入说明：[adapters/openhands/README.md](../../../adapters/openhands/README.md)
- 初筛记录：[docs/validation/openhands.md](../../validation/openhands.md)
- 来源锁定：[adapters/openhands/LOCK_PROVENANCE.json](../../../adapters/openhands/LOCK_PROVENANCE.json)
- 本轮补充运行：`/Users/hanbowen01/harness-lab-data/runs/2026-10-27/openhands/`
  - `sensitive/supp-case-openhands-151514`（通过）
  - `tools/supp-tools-openhands-202-152032`（`all_pages_read` 失败）
  - `tools/supp-tools-openhands-303-152128`（通过）

[上一篇：Google ADK](google_adk.md) · [返回框架索引](README.md) · [下一篇：Codex SDK](codex_sdk.md)
