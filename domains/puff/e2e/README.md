# domains/puff/e2e

puff 域内跨应用的端到端测试目录。

当前为空。单个应用内部的 e2e 测试继续放在各自目录：

- `services/api/test/*.e2e-spec.ts`
- `apps/desktop/test/*.e2e-spec.ts`

当一个用例需要同时启动多个 puff 项目（例如 Web + API 的完整链路）时，在这里新建独立项目，标签为 `domain:puff`、`type:e2e`、`lang:ts`。
