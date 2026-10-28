# 实施与验收记录

2026-10-20 开工前重新读取需求、调研计划和实施计划。复核没有阻碍 I01 的问题；I06 先校准规则、样例和判定器，真实候选能力在接入后验证。

执行以[任务表](../../IMPLEMENTATION_PLAN.md)为准。只建设当前一步所需内容；归档材料不自动加载，收费模型请求必须先落实调用数限制。

## I01：配置、依赖与路径

已完成。使用 Python 3.12.13，`uv.lock` 固定依赖；宿主只安装配置和 Docker 编排所需的 PyYAML、HTTPX 及测试工具，未安装候选 SDK 或浏览器依赖。

`uv sync --frozen` 可重建环境。`runner/config.py` 拒绝未知/重复配置项、虚报美元限额、路径穿越和符号链接；同一恢复链路径稳定，实验、变体和用户状态互相分开。根目录配置没有从归档取回实现，仅参考归档的更紧预算，继续使用已保留的 `.env`。

固定镜像中的测试结果：**84 passed**。记录：`runs/2026-10-20/infrastructure/bootstrap/i01-config-check/`，含测试输出和配置/程序哈希。测试在 Linux 容器内执行，宿主没有代办能力任务。

## I02：容器基础

已完成。基础镜像固定为官方 Python `3.12.13-slim-bookworm` 的 Linux arm64 digest：

`sha256:3f9c7f75b916e8279e24136d7a57769aba9e193715be52570417698c52d6eed0`

`bench` 是短时验收程序，不是 Agent 或占位服务。持久目录分别挂载到 `/data/state`、`workspaces`、`fixtures`、`budgets` 和 `runs`，不把数据根的 `old/` 挂进容器。镜像也不包含归档或 `.env`，验收容器不注入模型凭证。

验收镜像 ID：`sha256:5d6fef5a5e012458ff4795afb0fde8cea2a1f26f57d403103377a5880210e633`。容器实测 Linux/aarch64、512 MiB、CPU 配额 `50000/100000`，根文件系统只读。

持久化探针由容器 `3bb5a2fd1541` 写入；删除该容器后，容器 `a11be05086b3` 成功读取，检查两个完整容器 ID 不同。三个验收容器均已删除，网络和证据保留。完整镜像、挂载、配额、退出记录和网络配置见 `runs/2026-10-20/infrastructure/bootstrap/i02-persistence/manifest.json`（相对于数据根目录）。

可重建命令：

```bash
uv sync --frozen
uv export --frozen --no-emit-project --output-file compose/locks/bench-requirements.txt
HARNESS_LAB_DATA_ROOT=/path/to/harness-lab-data docker compose -f compose/docker-compose.yml build bench
HARNESS_LAB_DATA_ROOT=/path/to/harness-lab-data docker compose -f compose/docker-compose.yml run --rm bench check --run-id UNIQUE_ID
```

尚未验证任何候选能力，未发起模型调用。

## I03：候选材料

已完成。八候选的官方来源、开源范围、固定版本、适合入口和未验证问题已写入 [sources.md](sources.md) 与 [screening.md](screening.md)。源码均导出到忽略的 `external/build-context/`，逐文件哈希保存在来源清单。容器内 6 项导出完整性、篡改、穿越、脏工作树及凭证过滤检查通过。源码检查不能代替后续候选运行验收。

## I04 / I07：第一轮基础验收

`i03-i04-proxy-check` 在 Linux arm64 容器共 **111 passed**，包括配置、源码导出、Fixture 账本与故障、基本工具和计数代理。镜像 ID `sha256:22c6f2b671d3293558b20de0704c345630b76a1c457cd7376402586ffc86a13a`；源码、完整输出及容器身份见数据根 `runs/2026-10-20/infrastructure/bootstrap/i03-i04-proxy-check/`。

计数代理以容器内模拟上游验证并发计数、限额前拦截、同数据库重建、工具 JSON 与 SSE 透传及错误状态。未调用远程模型；实际子进程重启和候选限额仍分别待验。后来新增的工具 HTTP 入口不在该镜像中，I04/I07 暂未完成。

## I04：工具与账本完成

`i04-i06-public-check` 固定镜像内 **127 passed**；本次含新增真实工具 HTTP 服务测试：分页/文件/计算/写入、可信 scope 绑定、跨用户 token 与参数替换拒绝、丢回执不自动重试、敏感操作直接拒绝且进程重启后不绕过。Fixture 使用 SQLite 保留请求和实际写入，幂等/非幂等、一次失败和提交后断连接均留独立证据。新代码从零实现，没有加载归档。

## I05：容器浏览器完成

运行 `uv run --frozen python scripts/lab.py infrastructure --run-id i05-browser-check`，所有 13 项断言通过。固定 Playwright 1.64.0 / MCP 0.0.83；显式给 MCP 指定同镜像稳定 Chromium，避免 alpha 依赖查找另一版本浏览器。

容器内浏览器完成登录、第二页数据读取、无效数量的页面校验、有效提交、回执读取和下载。独立容器从种子核对目标、价格/数量、单次实际写入及 CSV 回执；MCP tools/list、navigate、snapshot 可用。证据位于数据根 `runs/2026-10-20/infrastructure/browser/i05-browser-check/`，含版本/镜像/容器、原始 MCP、网站操作、账本、CSV、截图和 trace。

服务镜像 `sha256:f85623fd8daf1aff847f36adbd48157ae5c875cc68cd2fbdc35d1d764e34fb68`；浏览器镜像 `sha256:2bc153a0f4d97c21b26aef46b1c2feadfc56131501546ca73c77ec01e609b3eb`。资源采样有 4 个有效样本、1 个容器切换期间失败样本（保留且未按零值计入）；本次短时基础验收不用于估算实例成本。临时容器已删除。未执行候选或远程模型调用。

## I06：冻结任务和公共判定完成

九项能力的输入、种子、预算、普通自动执行/敏感直接拒绝规则已固定在 `configs/tasks/`。公共 tools/browser/sensitive/scope 判定器已用三种种子和正反样例校准。改错金额、错用户、重复真实写入、漏分页、缺写入请求/工具审计、篡改回执或混入他人账本均失败；正确样例通过。专项恢复、故障、实例成本按实际候选接口在 I22–I25 补判定，不用外层假状态提前算通过。

补验 `i06-i07-audit-check`：固定镜像 `sha256:8e0681fe1f81dd40773af7146ef0604fbfad8a9c16637279a82a8c8d26853a73`，**130 passed in 19.47s**。包括两个真实 proxy CLI 子进程：同预算重启后累计，第 3 次 429，上游只收到 2 次；全程模拟上游，无收费请求。


## I07：编排与故障收尾完成

`i07-cleanup-check`：固定 bench 镜像 `sha256:a48796066487c0ec4e442c77c7f6a166dcc2d8e678c33b47a49fc6cc11b2d7e3`，**132 passed in 19.90s**。新增验证了候选任务收尾时日志写盘失败仍先停止候选与模型代理，单项 Docker 清理超时仍继续清理其他资源；故障测试只替换 Docker transport，在验收容器内运行。

`i07-orchestration-check` 再次完成真实容器浏览器全流程，13 项独立断言通过，临时容器已清理。计数代理的流式/工具内容透传、出网前限额和同数据库子进程重启累计已经验收。真实候选是否全部经过代理仍逐项在 I08–I15 检查；没有在此提前计作候选通过。模型费用缺少可靠单价，保持未知。当前没有真实模型请求。

更正：I05 的 `verdict.json.assertions` 实际为 13 项，早期进度文字误写 14，现已统一。


## I08：DSH 真实模型与工具复验

已连续两次从同一固定镜像创建新容器，种子 101 的公共工具任务均通过 12 项外部判定；模型调用分别 9 次、5 次，实际写入和写入请求均为 3，无重复。完整记录见 [deepseek_harness.md](deepseek_harness.md)。候选重试/重启限额补验完成前 I08 仍为进行中。


## I08：限额补验完成

`i08-dsh-budget-001` 以真实 DSH 镜像和容器内模拟 503 上游，17 项独立断言通过。首轮候选真实重试并触发 cap=2；删除候选、更换计数代理后第二新候选仍被限额拦截，上游累计仅 2 次。内部网络、fake key，不收费。I08 完成；浏览器和已有任务暂停恢复不在此结论范围。

## I09/I10：构建与探针纠错

OpenClaw 首次源码构建在官方 runtime bundle 阶段 SIGKILL，Docker 返回 `cannot allocate memory`；依赖和真实插件资产步骤此前均成功。官方脚本提示完整构建至少需要 4352 MB heap。下一次保留完整官方 recipe，仅为其中的 tsdown 命令增加官方支持的 `--concurrency 1` 并设置 4352 MB heap，不改源码或 Colima。完整构建日志保存于 `openclaw/runtime/i09-openclaw-build-001/`（相对于当天运行目录）。当前没有 OpenClaw 运行结论。

Deep Agents 镜像 `sha256:c6dc3fd634a3e8bf68eaf0b04cb7c9dfa08cd8870f8f46c8fa0bed15b3c83904` 已构建，六个源码 wheel 和 2516 个来源文件核验通过，实际安装无 editable 包。`i10-deepagents-build-001` 保存构建日志和镜像信息。

`i10-deepagents-budget-001` 未运行候选：宿主探针变量名被健康检查循环覆盖，误启动 model-proxy。已停止该容器，失败记录及清理保留，不计作候选能力失败，全程内网模拟、无收费。探针修正后，额外核对候选容器的 Compose 服务名和预期镜像 ID，防止同类误启动被当成候选结果。开始使用新 run ID `i10-deepagents-budget-002` 复验。

## I10：初筛完成

`i10-deepagents-budget-002` 18 项检查全部通过：真实候选重试、两次上游请求后限额、更换候选和计数容器不清零。`i10-deepagents-tools-001/002` 均使用同一镜像、两个全新容器和各自干净数据，真实模型请求各 6 次、业务写入/请求各 3/3，12 项外部判定均通过；原生图状态、SQLite checkpoint 与原始事件已留存。候选内任务执行分别 22.277 / 23.729 秒。详见 [Deep Agents 记录](deepagents_langgraph.md)。I10 已完成，浏览器与恢复仍待专项；开始串行重建 OpenClaw。

## I11：Pi Agent Core 容器初筛

已完成调研步骤。相同镜像、新容器和空状态两轮真实工具任务：第一轮 12 项全过，第二轮仅分页上限失败（page_size=100，任务要求≤5），两轮报告和三笔写入均正确。模型请求分别 6/5 次，原生均 stop，不能用框架 completed 代替任务验收。`i11-pi-budget-001` 的重试、更换候选及 proxy 后累计限额 18 项全过。来源、失败、状态与成本边界见 [Pi 报告](pi_agent_core.md)。

## I09：OpenClaw Gateway 容器初筛

已完成。同镜像两个新容器的真实工具任务分别 10/9 次模型请求，各 3/3 写入，12 项判定全过；budget-004 的重试和更换候选/计数容器累计限额 18 项通过。构建内存、旧配置和只读 cache 的接入错误与原生结果均保留，见 [OpenClaw 报告](openclaw.md)。尚未测继续会话、已有任务恢复、浏览器或共享实例隔离。

## I12：Google ADK 容器初筛

已完成。固定源码、LiteLLM与76条独立依赖锁构建成功，`i12-adk-budget-001` 18项通过；两轮真实工具任务各12项通过，模型请求7/6、写入3/3，原生STOP且事件流正常耗尽。仅验证新会话，不据SQLite文件宣称恢复。见 [ADK报告](google_adk.md)。

## I13：OpenAI Agents SDK 容器初筛

已完成。同一固定镜像、两个新容器，`i13-agents-tools-001/002` 各12项通过，模型请求7/6、写入3/3；原生完成且原始模型终态为stop。`i13-agents-budget-001` 18项通过，验证真实重试和更换候选/计数容器后累计不清零。会话历史和RunState存在不代替恢复验收，见[报告](openai_agents_sdk.md)。

## I14 与详细实验编排准备

`i14-detailed-runner-check` 在新bench镜像内 **145 passed in 19.83s**，包含Codex候选映射、Responses模拟上游验收和13项详细任务准备/负向证据检查。未注入模型凭证，不算候选详细能力通过。Codex官方SDK/CLI镜像构建完成，正在内网模拟预算探针；详细浏览器入口仍需候选镜像重建及真实运行。

后续复核修正DSH敏感拒绝不得覆盖原生截断/缺终态，收紧浏览器原始动作的scope、call_id、native_tool和source重复校验；新增Codex无正文429归因正反检查。`i14-detailed-evidence-check` 新bench容器 **156 passed in 22.11s**。Codex `budget-002` 18项通过；`protocol-001` 原生Responses真实10次200，但统一工具未接，业务任务失败，不关闭I14。详见[Codex记录](codex_sdk.md)。

## I15：OpenHands 接入进行中

固定源码与125条依赖锁构建成功。源码复核先修正官方Agent总会请求risk schema的问题；实际`budget-001`在工具加载时暴露继承类自动命名使八个工具都叫http，0模型请求。明确重写实例name并同时核对导出name/schema后重建，`budget-002`18项通过；真实工具任务开始。失败及三个构建版本均留档，尚未据源码pause接口声称恢复通过。

`i15-openhands-tools-001/002`同镜像、两个新容器均12项通过，模型6/7次、写入3/3；原生FINISHED且finish成功observation，无pending。I15完成，详见[报告](openhands.md)。


I16 补验：`i16-extension-evidence-check-002` 新 bench 容器 **160 passed in 20.96s**，包含新增业务态哈希/原生指针、实际查询别名与可信receipt对应、quantity4真实执行前拒绝及普通数量2输入校准。首轮159通过/1失败是过时的“Codex仅协议准入”测试，现改为只拒绝尚未接的browser，原始失败保留。数量拒绝必须绑定真实write/参数/目标/call_id与原始执行事件，不能靠任意403过关。无真实模型请求；不是候选业务改造通过。

I14已经结束：Codex最终官方MCP配置approve镜像下两新容器工具任务通过（7/8模型请求、写入3/3）；八候选累计限额均已有实测。累计收费模型请求135次，金额未知。详细任务开始后逐轮在[detailed.md](detailed.md)追加，不将静态材料当作运行通过。

`i16-extension-sources-check` 新bench **160 passed in20.99s**，进一步要求每个业务确认态source指向真实receipt finish并匹配可信审计，HTTP工具层不能收到被规则禁止的write。DSH两项业务改造冒烟通过，仍不代表冷读或任务续跑验收。累计真实模型请求152次（含所有初筛失败及本轮冒烟），金额未知。

## 2026-10-20：独立进程验收补验

详细编排现于执行前固定候选镜像ID、名称和配额，结束后从可信宿主Docker快照保存独立观测，验收容器严格检查唯一Worker归属、退出码、OOM、异常状态及配额；完整容器ID是结束时唯一名称归属，不能称启动前独立ID固定。`i18-process-scope-check-001` 在新bench `sha256:ec10ed62c62a2f3355ec34c656649b8d374b5edb77d156e264b87b4ec936127f` 内614项通过（28.60秒），未注入模型凭证。作用域及资源的合成校准不代表I24/I25实测。

追加审计002（`bootstrap/i18-process-audit-002`）仅离线读取41份明确旧manifest：37份快照健康通过，OpenClaw三轮browser因异常退出失败，cancel002因OOM失败；旧记录没有额外独立身份pin，完整进程结论保持unknown或failed。旧verdict未覆盖并留存原字节哈希。001因旧DSH索引格式不同在准备清单时失败，未启动Docker或模型。I18完成调研步骤，保留正式7/15及取消OOM异常，不能表述为全能力通过。
