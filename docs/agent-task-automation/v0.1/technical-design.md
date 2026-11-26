# Agent 任务自动化插件 v0.1 技术详细设计

| 项目 | 内容 |
| --- | --- |
| 状态 | 两个项目及插件产物已实现，业务闭环按功能清单验收 |
| 开发仓库 | 当前 puff 仓库 |
| 首个宿主 | Codex |
| 首个工作区 | 当前 puff 仓库 |
| 核心原则 | 宿主适配、工作区绑定和外部能力绑定分离 |
| 业务能力 | 用户安装后选择，不进入插件核心 |

当前仓库承载插件设计、开发和测试。安装后的执行目录由用户选择的工作区绑定决定。当前仓库用于验证这项绑定，也可以通过同一流程绑定其它目录。

## 1. 总体架构

~~~mermaid
flowchart LR
    H[Codex 插件应用] -->|workspace 依赖| P[通用核心库]
    P --> W[工作区绑定]
    P --> C[能力绑定]
    H --> S[安装与 Scheduled Task]
    H --> X[宿主会话与权限]
    W --> R[路径与执行规则]
    C --> T[任务源能力]
    C --> D[交付能力]
    C --> N[通知能力]
    A[Agent] --> T
    A --> D
    A --> N
~~~

### 1.1 通用插件核心

核心提供安装引导、能力选择、工作区绑定、任务上下文、执行规则、结果模板以及宿主无关的暂停恢复语义。

核心不解析业务平台字段，不调用具体平台 API，不保存业务凭据，不实现代码交付逻辑。

### 1.2 Codex 宿主适配

Codex 适配提供 plugin manifest、onboarding skill、当前工作区发现、Scheduled Task 配置、宿主运行上下文、宿主结果展示以及宿主权限和停止能力。

DSH 和 Claude Code 后续各自实现宿主适配，复用通用配置和任务上下文格式。

### 1.3 外部能力绑定

能力由用户安装后选择，核心只保存引用。

~~~mermaid
flowchart TD
    C[能力引用] --> T[task-source]
    C --> D[delivery]
    C --> N[notification]
    C --> V[review]
    T --> A[Agent调用用户选择的能力]
    D --> A
    N --> A
    V --> A
~~~

能力可以是 skill、MCP、CLI 或宿主能力。核心只校验引用、类型、操作列表和启用状态。

### 1.4 工作区绑定

~~~mermaid
flowchart TD
    H[Agent宿主] --> B[workspace binding]
    B --> P[工作区路径]
    B --> E[执行规则]
    B --> C[能力引用]
    B --> S[Scheduled Task]
    E --> M[窗口]
    E --> L[活动任务上限]
    E --> A[自动执行权限]
~~~

工作区绑定不包含业务平台字段。业务平台规则由用户绑定的能力处理。

### 1.5 工具链

复用仓库现有的 Node 24.22.0、TypeScript、tsdown、Vitest、Biome 与 pnpm catalog。插件运行使用 Node 标准库。用户选择的工具依赖由其安装环境提供。

## 2. 目标文件

v0.1 包含两个同域项目，各自维护 `AGENTS.md`、`package.json`、`project.json`、TypeScript 配置和构建、检查、测试脚本。

| 项目 | 包名 | Nx 标签 | 职责 |
| --- | --- | --- | --- |
| `domains/agent-task-automation/libs/agent-task-core` | `@puff/agent-task-core` | `domain:agent-task-automation`、`type:lib`、`lang:ts` | 通用配置、工作区规则、能力引用、宿主接口、工作流和模板 |
| `domains/agent-task-automation/apps/codex-plugin` | `@puff/codex-plugin` | `domain:agent-task-automation`、`type:app`、`lang:ts` | Codex 插件清单、安装引导入口、宿主接口实现和插件打包 |

依赖方向为 `@puff/codex-plugin` → `@puff/agent-task-core`。应用通过 `workspace:*` 声明依赖，使用核心库的公开入口。核心定义宿主接口，由应用提供实现。DSH 和 Claude Code 后续分别新增宿主应用，复用同一个核心库。

~~~text
domains/agent-task-automation/
├── libs/agent-task-core/
│   ├── src/
│   │   ├── config.ts
│   │   ├── execution-context.ts
│   │   ├── host-contract.ts
│   │   └── index.ts
│   ├── skills/task-run/SKILL.md
│   ├── references/
│   │   ├── setup-workflow.md
│   │   └── execution-rules.md
│   ├── templates/
│   │   ├── config.example.json
│   │   ├── task-context.md
│   │   └── run-result.md
│   └── test/
└── apps/codex-plugin/
    ├── plugin.json
    ├── skills/setup/SKILL.md
    ├── scripts/package-plugin.mjs
    ├── src/
    │   ├── adapter.ts
    │   ├── cli.ts
    │   ├── commands.ts
    │   └── store.ts
    ├── references/host.md
    └── test/
~~~

| 文件 | 职责 |
| --- | --- |
| plugin.json | Codex 插件身份和 onboarding 配置 |
| setup/SKILL.md | Codex 配置入口，引用通用引导并衔接 Scheduled Task |
| references/setup-workflow.md | 通用能力选择、工作区配置和只读试跑引导 |
| task-run/SKILL.md | 按绑定能力执行任务的通用工作流 |
| execution-rules.md | 工作窗口、自动执行、活动任务数和暂停恢复 |
| config.ts | 配置、能力引用、工作区路径和规则校验 |
| execution-context.ts | 生成 Agent 任务上下文 |
| 核心库的 src/host-contract.ts | 通用工作区、计划和运行查询接口 |
| Codex 应用的 src/adapter.ts | 将宿主接口映射到 Codex 提供的能力 |
| Codex 应用的 src/commands.ts、src/store.ts | 命令处理、多个工作区配置存储、活动运行和结果摘要 |
| scripts/package-plugin.mjs | 装配 Codex 清单、入口、适配代码和核心资源 |
| templates/run-result.md | 宿主结果摘要和待处理事项 |
| 两个项目的 test/ | 分别验证通用规则和 Codex 适配及打包 |

核心库通过包公开入口提供编译后的 ESM、类型和共享资源。Codex 应用构建时将核心打入 Node 命令，并装配共享工作流、模板、manifest 和 setup。仓库本地 marketplace 指向构建目录。见[构建与本地安装](../../../domains/agent-task-automation/apps/codex-plugin/README.md)。

## 3. 配置接口

~~~typescript
type CapabilityKind = 'skill' | 'mcp' | 'cli' | 'host';

type CapabilityRole =
  | 'task-source'
  | 'delivery'
  | 'notification'
  | 'review';

interface CapabilityBinding {
  role: CapabilityRole;
  ref: string;
  kind: CapabilityKind;
  operations: string[];
  enabled: boolean;
}

interface WorkspaceBinding {
  id: string;
  rootPath: string;
  host: {
    type: string;
    adapterVersion: string;
  };
  capabilities: CapabilityBinding[];
  execution: {
    automaticEnabled: boolean;
    maxActiveRuns: number;
    workspaceMode: 'local' | 'worktree';
    taskScope: string;
    allowedPaths: string[];
    allowedActions: string[];
    timezone: string;
    paused?: boolean;
    windows: Array<{
      days: number[];
      startMinute: number;
      endMinute: number;
    }>;
  };
  schedule?: {
    intervalMinutes: number;
    hostPlanRef?: string;
  };
}

interface PluginConfig {
  schemaVersion: 1;
  workspaces: WorkspaceBinding[];
}
~~~

配置校验和运行规则

- `workspaces` 是可增长的绑定集合。工作区 ID 唯一，rootPath 为用户选定的本地绝对路径，真实目录不能重复绑定到不同 ID。运行前按宿主提供的根目录或其已确认关联的 worktree 校验实际执行目录。修改路径相对于该目录。
- 核心校验宿主标识和适配版本的结构。Codex 应用校验自身支持的宿主类型和版本，示例中的 codex 是该适配提供的值。
- 每个工作区的每个角色最多有一个启用的绑定。ref 由当前宿主解析，核心只保存引用。setup 根据用户选择的工具说明确认 operations，逐项记录允许用于该工作区的动作。实际执行还需符合任务、宿主权限与仓库规则。
- taskScope 是业务无关的范围描述或引用。任务筛选字段、交付规则和必要参数保留在绑定能力的使用方式中。结果摘要可保存任务及成果引用，凭据由宿主或工具保管。
- 自动执行默认关闭。每个工作区最多处理一个活动任务，多个工作区可以各自拥有一个活动任务。windows 以 IANA 时区解释，days 按周一为 1 至周日为 7，窗口开始包含、结束排除。
- schedule 可先保存计划草稿。用户在宿主确认后记录 hostPlanRef，一个计划关联一个工作区，执行时读取当前绑定。配置变更影响已确认范围时重新试跑和确认。

配置示例：

~~~json
{
  "schemaVersion": 1,
  "workspaces": [
    {
      "id": "puff-workspace",
      "rootPath": "/Users/hanbowen01/repo/puff",
      "host": {
        "type": "codex",
        "adapterVersion": "0.1"
      },
      "capabilities": [
        {
          "role": "task-source",
          "ref": "user-selected-task-skill",
          "kind": "skill",
          "operations": ["list", "read"],
          "enabled": true
        },
        {
          "role": "delivery",
          "ref": "user-selected-delivery-skill",
          "kind": "skill",
          "operations": ["prepare", "publish", "observe"],
          "enabled": true
        },
        {
          "role": "notification",
          "ref": "codex-host",
          "kind": "host",
          "operations": ["report"],
          "enabled": true
        }
      ],
      "execution": {
        "automaticEnabled": false,
        "maxActiveRuns": 1,
        "workspaceMode": "worktree",
        "taskScope": "用户确认的任务范围",
        "allowedPaths": ["docs/agent-task-automation"],
        "allowedActions": ["list", "read", "modify", "verify", "observe", "report"],
        "timezone": "Asia/Shanghai",
        "windows": [
          {
            "days": [1, 2, 3, 4, 5],
            "startMinute": 540,
            "endMinute": 1080
          }
        ]
      },
      "schedule": {
        "intervalMinutes": 30
      }
    }
  ]
}
~~~

## 4. 插件 manifest 和 Codex 适配

候选 manifest：

~~~json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "agent-task-automation",
  "version": "0.1.0",
  "description": "Bind Agent capabilities to workspaces and scheduled workflows",
  "extensions": {
    "com.openai": {
      "onboardingSkill": "./skills/setup/SKILL.md"
    }
  }
}
~~~

Codex 适配提供安装后的 setup 入口，将宿主和候选工作区传给引导。计划绑定用户确认的宿主工作区和工作区 ID，并指向 task-run。每次唤醒重新读取已保存的绑定，按需生成本次任务上下文。计划的创建、更新、暂停和运行状态通过宿主支持的入口处理。

当前 Codex 已识别并启用本地产物。原生计划实际运行时仍须核对宿主提供的工作区、计划引用及活动状态。活动状态无法确认时执行只读检查。

## 5. 主流程

### 5.1 安装和首次绑定

~~~mermaid
sequenceDiagram
    actor U as 用户
    participant C as Codex
    participant S as setup skill
    participant W as 工作区绑定
    participant A as 能力绑定
    participant T as Scheduled Task
    U->>C: 安装插件
    C-->>U: 提供配置入口
    U->>S: 开始设置
    S->>C: 读取宿主和工作区
    C-->>S: Codex上下文与工作区
    S->>U: 展示工作区选择
    U->>S: 选择工作区
    S->>U: 选择任务源、交付和通知能力
    U->>S: 确认能力操作范围
    S->>W: 生成工作区规则
    S->>A: 保存能力引用
    S->>A: 使用已确认的读取操作试跑
    A-->>S: 返回任务上下文或阻塞原因
    S-->>U: 展示试跑结果和拟启用范围
    S->>T: 生成 Scheduled Task 配置
    T-->>U: 显示待确认计划
    U->>C: 确认启用
~~~

### 5.2 手动执行

~~~mermaid
sequenceDiagram
    actor U as 用户
    participant C as Codex
    participant R as task-run skill
    participant T as 任务源能力
    participant A as Agent
    participant D as 交付能力
    participant N as 通知能力
    U->>C: 运行当前工作区任务
    C->>R: 加载工作区配置并检查运行门槛
    R->>T: 读取候选任务
    T-->>R: 返回任务上下文
    R->>A: 提供任务、验收和执行规则
    A->>A: 修改代码并验证
    A->>D: 按授权交付并查询结果
    D-->>A: 返回已核对的成果或未知状态
    A->>N: 返回结果或发送通知
    N-->>U: 展示结果与待处理事项
~~~

### 5.3 定时执行

~~~mermaid
sequenceDiagram
    participant T as Codex Scheduled Task
    participant R as task-run skill
    participant W as 工作区绑定
    participant A as Agent
    participant C as 外部能力
    T->>R: 触发工作流
    R->>W: 按工作区 ID 读取当前绑定并核对宿主上下文
    R->>W: 检查窗口、权限和活动任务数
    W-->>R: 允许或阻塞
    R->>A: 提供绑定能力和任务上下文
    A->>C: 使用用户选择的任务、交付和通知能力
    C-->>A: 返回业务结果
    A-->>T: 返回结果和待处理事项
~~~

## 6. 触发策略

触发来源包括安装引导、首次配置、用户手动执行、Codex Scheduled Task 到期、用户暂停恢复取消和宿主运行完成后的结果返回。

手动与定时入口共用工作区核对、窗口、活动任务名额和能力权限检查。自动执行开关只控制到期计划。完整条件、重复触发与取消语义见 [触发策略](./trigger-strategy.md)。

插件只判断通用条件。任务源、交付和通知能力负责各自的业务状态、权限和结果判断。

## 7. 数据拓扑

~~~mermaid
flowchart LR
    CFG[插件配置] --> W[工作区绑定]
    CFG --> C[能力绑定]
    W --> S[Codex Scheduled Task]
    S --> A[Agent]
    A --> T[任务源能力]
    A --> D[交付能力]
    A --> N[通知能力]
    A --> F[工作区代码]
    T --> A
    D --> A
    N --> U[用户结果]
~~~

插件在本机用户目录保存 JSON 配置，并为每个工作区保存当前活动记录与最近一次结果摘要。短时文件锁串行化状态写入，同一工作区的插件入口共用活动名额。Agent 从绑定能力读取业务事实并生成本次上下文。摘要保存任务引用、验证情况、交付引用和待处理事项。宿主负责提供可核对的运行与停止状态。

插件不保存业务平台 token、具体业务平台字段、任务平台状态枚举、CR/卡片/评论协议或具体业务平台 API 响应。

## 8. 测试计划

### 核心配置

- 能力 role、kind 和 operations 校验。
- 工作区路径校验。
- 宿主类型校验。
- 自动执行和运行窗口校验。
- 一个工作区只能有一个活动任务规则。
- 多个工作区可以同时存在并使用不同的能力、规则和活动记录。
- 核心库可独立构建和测试，宿主相关测试通过核心定义的接口传入测试实现。

### Codex 适配

- 当前 Codex 构建版本能加载 manifest。
- setup skill 能启动。
- setup skill 能读取当前工作区。
- 工作区绑定能保存。
- list 能返回所有已绑定工作区的摘要，单个工作区的 configure 不会覆盖其它绑定。
- Scheduled Task 能携带工作区配置和 task-run skill。
- 暂停、恢复和重复触发符合规则。
- 包边界检查确认应用通过公开入口依赖核心库。插件产物包含核心工作流资源，清单和 skill 的相对引用可解析。

### 工作流

- Agent 能加载用户选择的任务源能力。
- Agent 能加载用户选择的交付能力。
- Agent 能使用当前 puff 仓库作为工作区。
- 任务上下文包含工作区、验收和允许动作。
- 外部能力不可用时进入阻塞。
- 交付结果由绑定能力返回，Agent 能展示结果。
- 核心代码不包含具体平台字段和 API。

## 9. 开发前提

代码已实现核心配置、宿主接口、Codex 适配和本地插件装配。用户在安装引导中选择实际能力并生成该工作区的规则。宿主与外部能力的实际验收按[功能清单](../features.md)跟踪。

## 10. 后续版本

- v0.2 增加 DSH、Claude Code 和其他宿主适配。
- v0.2 增加更多工作区类型和公开能力绑定。
- v0.3 增加事件触发、评审跟进和 CI 跟进。
- v0.4 增加办公工作区。
- v0.5 增加团队工作区和组织策略。
