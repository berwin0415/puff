# I22 DeepSeek Goal 接线草案

本文件是待 I21 选择后审阅的接线草案。没有应用、stage、构建、运行、测试或调用模型；不能作为 I22 能力结果。原详细批次继续使用冻结镜像，不能用本草案重做或覆盖旧结果。

## 接法

普通 `entrypoint.ts` 的入口路由与默认请求校验保持原样。单独的 `goal-runtime-entrypoint.ts` 只处理带顶层 lifecycle 的新专项请求；调用原 scoped runtime 时明确传入 GoalRuntime helper。默认不传 helper，现有 create/resume/followup/取消流程继续按原分支执行。

独立 `harness-lab-goal` profile 以官方 sdk-minimal 为底，并加载独立 overlay 的四项官方 Goal/checkpoint 插件。profile/home 在容器 `/tmp` 重建，不将 node_modules 的官方链接放入恢复进度。原生 JSONL root 显式为 `/state/sessions`，工作区为 `/workspace`；整个两挂载都必须清点，不能遗漏额外文件。现有上游源码、agent loop 和取消 cause 均不修改。

new 分支仅由可信端调用一次 goals.create。官方 round driver 是唯一自动 round 生产者。完成/blocked 继续由官方 Goal 工具交给模型决定；可信 guard 固定 objective/cap，禁止模型新建、编辑或恢复 Goal。外层没有任务重试或 Agent 循环。

宿主看到 `control-events.jsonl` 的 `pause_handler_installed` 才可发送 SIGUSR2；这仅证明 handler 安装。正常运行信号只设置意图。scoped 公共 pre-step hook 保留原始 claimed 数量，在 await next 前后检查；仅空 claim、空 inbox、同当前 Goal/turn、无在途调用时执行 goals.pause 并返回 reject。非空 claim 不丢弃、不补队列，而拒绝本次暂停请求并让原步骤自然继续；因此该链不能算暂停通过。

必须看到真实 goal/change(operation=pause) 与同 turn 的原生 blocked turn/end，再由公开 Goal/status 事件等待 idle，显式 flush。轮次之间 active/armed 的 transient idle 不算结束。helper 没有自己循环驱动 Agent。保存的暂停结果先是 pause_pending_close；handle、private scopes、runtime 正常关闭且原生文件未变后，才产出 candidate pause index。候选仍将 pause_confirmed 标 false，宿主必须独立完成状态/业务/生命周期/资源/预算验收。

B 启动前验证完整状态清单；原生 agents.resume 后，在任何模型前比对原 Goal 的全部 view、Session header/seq、暂停原文件 hash 与工具面。随后只调用 goals.resume，不重发 prompt/followup，固定 round cap 不增加。已知 cancel/abort 的结束事件缺口保留；正常 pause 路线不借助 cancel、abort、dispose 来制造安全点。

## 文件

| 文件 | 作用 |
| --- | --- |
| adapters/deepseek_harness/goal-runtime-entrypoint.ts | 独立 I22 命令；new/resume 请求和可信绑定；B 禁止 prompt |
| adapters/deepseek_harness/goal-binding.mjs | 完整 scope/source/adapter/模型/工具/声明镜像/原始状态清单校验；不分配新预算 |
| adapters/deepseek_harness/goal-runtime.ts | 公开 Goal API、SIGUSR2 意图、步边界和真实终态证据，正常 close 后输出索引 |
| adapters/deepseek_harness/goal-runtime.cordis.patch.yml | 单独加载四官方插件和 /state/sessions root |
| adapters/deepseek_harness/goal-surface-entrypoint.ts | 未来 P05 无模型校准，固定完整原生 tool schemas；不创建 Goal 或输入任务 |
| adapters/deepseek_harness/scoped-runtime-entrypoint.ts | 仅增加显式 helper 接点；普通 detailed 分支默认不启用 |
| scripts/stage_deepseek_harness.py | 新 --goal-runtime 显式开关；拒绝对当前 detailed context 使用该开关 |

## 宿主绑定

顶层 lifecycle 的精确形状：

```json
{"mode":"new","chain_id":"i22-deepseek-tools-seed-101-001","attempt_id":"worker-a","binding_file":"/config/lifecycle.json"}
```

`request.run_id = chain_id = scope.variant`，B 只将 mode/attempt 改为 resume/worker-b。attempt 不进入 operation_prefix、Session ID 或预算 ID。整链仍沿用 detailed tools case 路径和一次 prepare 的场景；每 attempt 只另建 /evidence 目录。

binding schema 为 `harness-lab-lifecycle/1`，包含 candidate=deepseek_harness、variant=goal-runtime、mode、chain/attempt、完整 scope、session_id；material source/adapter 两摘要；原镜像 ID；模型配置/同 run token 摘要与原 budget_id/cap20；工具配置及实际 tool surface 摘要；完整状态索引摘要；固定 max_goal_rounds；300 秒累计活跃剩余量。B 另绑定已由宿主独立认可的 pause index。具体精确 JSON 字段以 goal-binding.mjs/readGoalBinding 为准；/tmp 同前缀的 binding-new/binding-resume/request-new/request-resume/pause-index/state-index/surface-request.json 给出完整实例。实例的全零摘要、原生 ID/seq/path 和说明文本均为占位；不能直接用于实跑。

模型配置的 canonical hash 输入键为 provider/base_url/model/max_output_tokens/max_retries/thinking/reasoning_effort；provider=deepseek-official、thinking=disabled、reasoning_effort=off。工具配置输入为 url/scope/token_sha256，token 摘要按去除首尾空白后的 UTF8 值。所有对象 hash 均使用递归按 key 排序的紧凑 JSON；数组保持顺序。contract hash 对原生 `{name,description,parameters}` 数组按 name 排序后做同一 hash。

工具 contract 必须先由固定新镜像的无模型 P05 校准产出，关联源码/adapter/实际 Docker image ID 并经宿主接受，不能把 A 运行时自报当作冻结材料。校准仅创建空 Agent、挂相同 HTTP 工具、读取公开 schemas，装有拒绝任何 llm/stream 的钩子且不获真实模型 token。输出 goal-tool-surface.json。预期 HTTP8 + Goal3，但必须实测固定完整 surface，不能假定源码数目等于实际注册面。

state index 的数组按 state 根再 workspace 根、每目录按名字排序深度优先枚举；directories 含两 root 的空 path，files 含 root/path/bytes/sha256。不接受 symlink、特殊文件、硬链接、多/少文件。B 在任何 runtime 写入之前匹配整个清单。/budget 不作为候选 native state，由宿主独立验证同一 calls.sqlite/run.token/cap 与请求行；binding 的 budget_id 只作声明，不证明代理 DB 真正延续。

pause index 的 goal 是完整公共 GoalView，session 包含 id/cwd/next_seq/artifact_sha256。source 指向 A 的 pause-events.jsonl 请求行以及 raw-events.jsonl 中真实 goal/change 和 turn/end 行，并声明 flush 和正常 close 来源。物理文件由 `session-artifact-after-flush.json.native_location.path` 的公开 persistence.locate 返回，不能自己猜路径。候选 close 后输出 native-state-index-after-close.json 和 lifecycle-pause-candidate.json；宿主应按真实文件/hash/事件行及 Docker 退出事实确认，再生成只读 /config/lifecycle-state.json 和 /config/lifecycle-pause.json。候选文件不能自我认证。

镜像内命令：

```text
node --import tsx/esm /opt/materials/harness-lab/goal-runtime-entrypoint.ts --request /config/request.json
```

用 docker create 的 `--entrypoint node` 覆盖原命令，再传余下参数。沿用镜像原 WORKDIR 与 TSX_TSCONFIG_PATH。除原 MODEL_API_BASE_URL/MODEL_NAME 与本地模型 token 外，传 MODEL_RUN_TOKEN_FILE=/budget/run.token 和 HARNESS_LIFECYCLE_IMAGE_ID=固定实际imageID。后者只用于容器内可信声明一致性，宿主仍须用 inspect 核对 A/B 实际镜像。

挂载：/config 内 request.json、scope.json、scope.token、lifecycle.json、lifecycle-state.json；B 另 lifecycle-pause.json，均只读。/budget/run.token 来自原链；/state、/workspace 共享原链，/evidence 每 attempt 独立。128m /tmp 用于重建 profile。使用原 harness-lab_lab 网络，无浏览器挂载。active timeout 传 floor(remaining_seconds)，宿主另外硬约束累计300秒，包含冷启动和正常保存/关闭。

无模型 surface 命令只将路径换成 goal-surface-entrypoint.ts、请求换为 /config/surface.json；不挂真实模型 token，输出需宿主确认 ExitCode=0、OOMKilled=false 与零模型调用后才能冻结。

## 应用前仍须做的门禁

本稿没有语法、类型、上游运行时或 hash 对齐执行结果。I21 选择后，先复核差异和 binding，再在独立 context stage/build，做无模型正反例与 surface 校准；随后最小真实 pause→blocked turn/end→flush/close 门禁。门禁失败就保留原始异常，不能补造终态、清洗 Error/cause 或用 idle 代替。最后才是同变体基线、已有业务写入后暂停、删除A、600秒等待、同预算B续跑与三 seed 正式验收。这里没有实现宿主编排或独立 grader。
