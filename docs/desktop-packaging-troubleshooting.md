# 桌面端 Windows 打包排障记录

本文记录 `@puff/desktop` 在 Windows x64 上接入 Electron、DeepSeek Harness 与 electron-builder 时实际遇到的问题、根因和解决办法。升级 Electron、`@deepseek-ai/dsh`、electron-builder 或 pnpm 前，先读本文并跑完文末回归项。

相关实现：

- [打包脚本](../apps/desktop/scripts/package-win.mjs)
- [afterPack 钩子](../apps/desktop/scripts/after-pack.cjs)
- [electron-builder 配置](../apps/desktop/electron-builder.yml)
- [桌面端规范](./rules/desktop.md)

## 当前打包流水线

`pnpm package:desktop:win` 不是直接对 `apps/desktop` 跑 electron-builder，流水线固定为：

1. 执行 `tsc --noEmit && tsdown`，把 Electron main 构建到 `dist/`。
2. 在系统临时目录创建独立的 runtime 工程，只声明 `@deepseek-ai/dsh@0.1.7-rc.2` 与 `pnpm@11.7.0`。
3. 在该临时目录执行 `pnpm install --prod --config.node-linker=hoisted`，得到包含 peer 依赖的扁平 runtime。
4. 在另一个系统临时目录创建最小 app 工程，只放 `dist/`、最小 `package.json` 和本地 `pnpm` shim。
5. 对临时 app 工程执行 electron-builder，绝对输出到 `apps/desktop/release/desktop`。
6. `afterPack` 把临时 runtime 复制到 `resources/dsh-runtime`。
7. 打包后的主进程从 `resources/dsh-runtime` 解析 dsh 与 pnpm，不再依赖 `app.asar` 中的 `node_modules`。

正常产物特征：

- `app.asar` 只有约 0.1 MB。
- `resources/dsh-runtime` 约 470 MB。
- NSIS 安装包约 217 MB（版本和依赖变化会浮动）。

## 快速定位表

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| `ERR_PNPM_IGNORED_BUILDS` | pnpm 11 默认不执行依赖安装脚本 | 在根 `pnpm-workspace.yaml` 的 `allowBuilds` 精确放行 |
| 首次运行一直显示 `Downloading Electron binary...` | Electron 的 postinstall 未执行 | `allowBuilds.electron: true`，重新安装并验证 `electron --version` |
| Host 报 `node-addon-require-builtin unsupported` | Electron 版本指纹不被支持 | 固定 `electron@44.0.0`，不要用 `^44.x` |
| `Cannot read properties of undefined (reading 'ReadWrite')` | electron-builder 用了缺少 cache-mode API 的 `@electron/get@3` | 根 workspace override 到 `@electron/get@5.1.0` |
| 下载 Electron / NSIS 时 `fetch failed` | GitHub 下载链路不可达 | 使用 `ELECTRON_MIRROR` 与 `ELECTRON_BUILDER_BINARIES_MIRROR` 回退镜像 |
| 打包后启动报缺 `@deepseek-ai/cordis-plugin-group` 等 peer 包 | electron-builder 未收集 pnpm 隔离依赖中的 peer | 临时目录 hoisted install 生成完整 runtime，afterPack 整体复制 |
| `resources/dsh-runtime` 里只有 `package.json` | `extraResources` 过滤了 `node_modules` | 改用 `afterPack` 直接复制 runtime 目录 |
| `Cannot compute electron version from installed node modules` | staging 工程没有 electron 依赖 | 在 electron-builder 配置固定 `electronVersion: 44.0.0` |
| electron-builder 报 `spawn node_modules\.bin\pnpm.CMD ENOENT` | staging 工程没有本地 pnpm shim | 打包前生成 `node_modules/.bin/pnpm.CMD` / `pnpm` |
| `app.asar` 或 `app.asar.unpacked` 异常巨大 | electron-builder 回溯到了仓库 workspace 的 `node_modules` | app 与 runtime staging 都放系统临时目录 |
| `EPERM` / `EBUSY` 删除 `win-unpacked.tmp` 或 `default_app.asar` | Windows 文件监视、索引或杀软锁住新生成文件 | 避开仓库内 staging；释放句柄或重启后清理 |
| `pnpm` 提示删除并重装整个 modules 目录，随后 `tsc` 丢失 | 在 workspace 内用不同 `node-linker` 执行 deploy | 不要在仓库内 deploy；改用隔离临时目录 install |

## 问题与解决办法

### 1. pnpm 拒绝执行依赖构建脚本

现象：

```text
[ERR_PNPM_IGNORED_BUILDS] Ignored build scripts:
@deepseek-ai/dsh-subprocess-local, electron, esbuild, koffi, node-pty, protobufjs
```

原因：pnpm 11 默认不信任第三方依赖的安装脚本。

解决办法：在根 `pnpm-workspace.yaml` 精确列出 `allowBuilds`：

- `@deepseek-ai/dsh-subprocess-local: true`
- `electron: true`
- `esbuild: true`
- `koffi: true`
- `node-pty: true`
- `protobufjs: true`
- `@google/genai: false`
- `electron-winstaller: false`

不要使用 `pnpm approve-builds --all`。新增依赖确需构建脚本时，先确认包来源和脚本内容，再按包放行。

### 2. Electron 二进制在首次启动时才下载

现象：第一次运行 `electron .` 长时间停在：

```text
Downloading Electron binary...
```

根因是安装阶段跳过了 Electron 的 postinstall，首次启动才触发下载。Dsh 运行时本身不依赖此步骤，但开发与打包会受影响。

处理：

1. 确认 `pnpm-workspace.yaml` 中 `electron: true`。
2. 执行 `pnpm install`。
3. 必要时执行 `pnpm --filter @puff/desktop rebuild electron`。
4. 验证：`pnpm --filter @puff/desktop exec electron --version` 输出版本号。

### 3. Electron 版本与原生模块指纹不兼容

现象：打包后的 Host 立即退出，日志出现：

```text
node-addon-require-builtin unsupported: Unsupported/no-context
unsupported Electron runtime fingerprint:
supported Electron versions: 43.0.0, 44.0.0, 45.0.0-alpha.6
```

根因：DSH 依赖的 `node-addon-require-builtin@0.1.6` 只识别特定 Electron 指纹。`electron@44.4.5` 不等于它支持的 `44.0.0`。

处理：

- 将 `apps/desktop/package.json` 的 Electron 固定为精确版本 `44.0.0`，不要写 `^44.0.0`。
- 修改后重新执行 `pnpm install`。
- 必须重新跑 `pnpm --filter @puff/desktop test:e2e` 和 `package:win:dir`；不能只看 `electron --version`。

### 4. electron-builder 读取 cache mode 崩溃

现象：

```text
TypeError: Cannot read properties of undefined (reading 'ReadWrite')
at resolveCacheMode (.../app-builder-lib/.../electronGet.ts)
```

根因：`electron-builder@26.15.3` 实际调用 `@electron/get` 的 `ElectronDownloadCacheMode`，但按其依赖范围解析到了缺少该导出的 `@electron/get@3.0.0`。

处理：在根 `pnpm-workspace.yaml` 固定覆盖：

```yaml
overrides:
  '@electron/get': 5.1.0
```

然后执行 `pnpm install`，再用 `pnpm why @electron/get -r` 确认只解析到 5.1.0。

### 5. Electron / electron-builder 资源下载失败

现象：打包中途出现 `fetch failed`，或下载 NSIS/7zip/Electron 压缩包极慢。

处理：[打包脚本](../apps/desktop/scripts/package-win.mjs) 默认在未显式设置时使用 npmmirror：

```text
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/
```

如果团队有内部镜像，优先在打包环境显式设置这两个变量；脚本只在变量缺失时回退到 npmmirror。

### 6. 打包后缺少 DSH 的 peer 依赖

现象：

```text
ERR_MODULE_NOT_FOUND: Cannot find package '@deepseek-ai/cordis-plugin-group'
imported from .../app.asar/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js
```

根因：DSH 使用 Cordis 的 peer 依赖组合。pnpm 的隔离布局会把部分 peer 放在虚拟 store 的共享层，electron-builder 自己的依赖扫描不会完整收集这些包。

处理：不要把 `apps/desktop` 的 workspace `node_modules` 直接交给 electron-builder。改为：

1. 在临时目录写一个只含 `@deepseek-ai/dsh` 与 `pnpm` 的 `package.json`。
2. 在同一临时目录写只含所需 `allowBuilds` 的 `pnpm-workspace.yaml`。
3. 执行 `pnpm install --prod --config.node-linker=hoisted`。
4. 把这个临时 runtime 原样复制到 `resources/dsh-runtime`。

这样 `@deepseek-ai/cordis-plugin-group`、`cordis-plugin-include`、`cordis-plugin-loader` 等 peer 会出现在 runtime 顶层，Node 才能从 dsh 子包向上解析到。

### 7. `extraResources` 不会复制 `node_modules`

现象：配置了 `extraResources` 后，产物中的 `resources/dsh-runtime` 只有 `package.json`，没有 `node_modules`。

根因：electron-builder 的文件匹配会过滤 `node_modules` 路径。

处理：不要依赖 `extraResources` 搬运 runtime。改用 `afterPack` 钩子：

```js
const source = process.env.PUFF_RUNTIME_DIR;
const target = join(context.appOutDir, 'resources', 'dsh-runtime');
await cp(source, target, { recursive: true, dereference: true });
```

打包脚本通过 `PUFF_RUNTIME_DIR` 把临时 runtime 路径传给钩子。

### 8. staging 工程无法推断 Electron 版本

现象：

```text
Cannot compute electron version from installed node modules
```

根因：临时 app staging 只放 `dist/` 和最小 `package.json`，没有 Electron 依赖。

处理：在 [electron-builder.yml](../apps/desktop/electron-builder.yml) 固定：

```yaml
electronVersion: 44.0.0
```

升级 Electron 时同步修改这里，并重新跑打包冒烟。

### 9. electron-builder 在 staging 中找不到 pnpm

现象：

```text
spawn node_modules\.bin\pnpm.CMD ENOENT
```

根因：electron-builder 会在项目目录探测包管理器；临时 staging 目录没有本地 pnpm shim。

处理：打包前在 staging 生成 shim：

- Windows：`node_modules/.bin/pnpm.CMD`
- POSIX：`node_modules/.bin/pnpm`

shim 使用当前 Node 可执行文件运行仓库已解析到的 pnpm CLI，不依赖系统 PATH。

### 10. app.asar 巨大，runtime 被复制两份

现象：`app.asar` 约 443 MB、`app.asar.unpacked` 约 1 GB，同时 `resources/dsh-runtime` 还有约 470 MB。

根因：electron-builder 的 projectDir 如果位于 `apps/desktop` 子树内，会回溯到 workspace 的 `node_modules`，把生产依赖再收进 asar。

处理：

- staging app 放到系统临时目录，不放在仓库内。
- runtime 也放到另一个系统临时目录。
- electron-builder 通过 `--projectDir <临时 app>` 打包。
- 通过 `--config.directories.output <绝对路径>` 把最终产物写回 `apps/desktop/release/desktop`。

打包后应检查 `app.asar` 大小；若它又变成几百 MB，说明 staging 路径或 projectDir 又回到了仓库依赖树。

### 11. Windows 文件锁导致 EPERM / EBUSY

现象：

```text
EPERM: operation not permitted, rename '...\release\win-unpacked.tmp' -> '...\release\win-unpacked'
EBUSY: resource busy or locked, unlink '...\release\win-unpacked.tmp\resources\default_app.asar'
```

原因通常是 Windows 文件索引、杀软扫描、IDE/Codex 文件监视或仍在退出的 Electron 进程暂时持有文件句柄。不是应用代码错误。

处理：

- 不要使用宽泛的 `rimraf release` 作为日常清理；使用脚本中列出的精确目录。
- 发生锁定时先确认没有残留 `puff.exe` / `electron.exe` 进程，稍后重试。
- 仍锁住时重启资源管理器或开发宿主；最稳妥是重启终端/编辑器后删除该目录。
- 保留被锁目录不会影响新的打包，只要它不在当前输出目录内。
- 全仓库禁止用脚本对 `$HOME`、工作区根目录或其他宽泛路径做递归删除。

### 12. 在 workspace 内 `pnpm deploy` 污染依赖

现象：打包过程中 pnpm 提示：

```text
The modules directories will be removed and reinstalled from scratch. Proceed?
```

继续后根 `node_modules` 被按 production 模式重装，`tsc` 等开发依赖消失。

原因：在仓库 workspace 内用不同 `node-linker` 执行 deploy，pnpm 会认为现有 modules 布局不再匹配。

处理：

- 不要在仓库内执行会改变 node-linker 的 `pnpm deploy`。
- 改为在系统临时目录创建独立 `package.json` + `pnpm-workspace.yaml`，再执行 `pnpm install --prod --config.node-linker=hoisted`。
- 如果不慎触发根 modules 重装提示，选择重建后执行 `pnpm install` 恢复开发依赖；随后跑一遍全仓检查。

## 打包后必查

每次改动 Electron、DSH、pnpm、electron-builder、`package-win.mjs` 或 `after-pack.cjs` 后，至少完成：

```bash
pnpm check
pnpm typecheck
pnpm test
pnpm build
pnpm --filter @puff/desktop test:e2e
pnpm --filter @puff/desktop package:win:dir
```

然后检查：

1. `app.asar` 是否仍在 0.1 MB 量级；异常膨胀说明 runtime 被重复收集。
2. `resources/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json` 是否存在。
3. `resources/dsh-runtime/node_modules/@deepseek-ai/cordis-plugin-group/package.json` 是否存在。
4. 用临时 `DSH_HOME` 和 `--user-data-dir` 启动 `win-unpacked\puff.exe`。
5. 日志中的 DSH URL 必须已脱敏，且能看到 `DSH host ready at http://127.0.0.1:<port>/`。

