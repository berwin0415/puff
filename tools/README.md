# tools

跨项目复用的脚本与检查工具。单个应用自己的构建脚本仍放在该应用目录内。

| 路径 | 用途 |
| --- | --- |
| `checks/project-boundaries.mjs` | 校验项目标签、路径与依赖方向，CI 每个 PR 都运行 |
| `bootstrap` | macOS / Linux / Git Bash：`mise install`（缺失时回退 `pnpm install`）+ `pnpm install` |
| `bootstrap.ps1` | Windows PowerShell：同上 |

## 依赖边界检查

```bash
node tools/checks/project-boundaries.mjs
```

规则来源见 [docs/monorepo-architecture-design.md](../docs/monorepo-architecture-design.md) 的 4.5 节，以及 [docs/rules/root.md](../docs/rules/root.md)。

检查内容：

1. 每个项目必须有且只有一个 `domain:*`、`type:*`、`lang:*` 标签。
2. 路径中的业务域与 `domain` 标签一致；目录类型与 `type` 标签一致。
3. `package.json` 中的 workspace 依赖必须符合依赖矩阵。
4. 应用、服务和 lab 不能被除 e2e 外的项目依赖。
5. 生产依赖不能指向 `type:tool` 项目。
6. `type:lab` 只能依赖同域 lib、platform lib 与 contract；没有 `package.json` 的项目按 `project.json` 登记。

新增业务域或项目类型时，同步修改脚本中的 `DOMAIN_KINDS` / `PLATFORM_KINDS` 与 `isAllowed`。
