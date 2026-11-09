# domains/puff/libs

域内共享库目录，只允许 puff 域内的应用与服务依赖。

当前为空。新增库时：

1. 目录命名 kebab-case，包名 `@puff/<短名>`，设置 `"private": true`。
2. 在 `package.json` 的 `exports` 声明对外入口。
3. 新增 `project.json`，标签为 `domain:puff`、`type:lib`、`lang:ts`。
4. 第三方依赖写 `catalog:`，不要在本包内单独锁版本。
5. 若是跨域通用能力（日志、追踪、配置、鉴权等），放到 `platform/libs/`，不要留在本目录。

Nest 使用 `tsc` 构建，消费源码直出的 TS 包会因为 `rootDir` 之外的文件报错。推荐让共享库产出 `dist` 再被引用。
