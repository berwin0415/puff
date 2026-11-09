# contracts

跨服务接口契约目录。

当前为空，原因是仓库只有一个可部署服务 `@puff/api`，且只有两个用于探活/自检的 HTTP 接口，没有服务间调用。

约定：

- 服务之间同步调用用 protobuf（gRPC 或 Connect），面向浏览器/外部的 HTTP 接口用 OpenAPI 3.1。
- 每个契约包归接口提供方所有，消费方只依赖自己语言生成的代码。
- 破坏性变更必须新建版本目录（如 `v2`），旧版本保留到所有消费方迁移完成。
- 生成代码提交到仓库；CI 重新生成并执行 `git diff --exit-code`。
- 契约目录标签：`domain:<域>`、`type:contract`、`lang:<语言>`。

引入第一个契约时，同步补充生成命令、兼容性检查（`buf breaking` / oasdiff）与 CI 步骤。
