# 源码构建保留经审查的提交及其冻结的 workspace 锁文件。
ARG NODE_IMAGE=node:24.22.0-bookworm-slim@sha256:156e9bc7a0a267cc6ca77b4283e32b123479342df684ce297455d7b95ff5a6b5
FROM ${NODE_IMAGE}

ENV CI=true \
    OPENCLAW_PREFER_PNPM=1 \
    OPENCLAW_RUN_NODE_SKIP_DTS_BUILD=1 \
    OPENCLAW_TSDOWN_MAX_OLD_SPACE_MB=4096 \
    NODE_OPTIONS=--max-old-space-size=4096 \
    RAYON_NUM_THREADS=2 \
    ROLLDOWN_WORKER_THREADS=2 \
    GIT_COMMIT=b6e0872bdb7228e22fac7608345e0529a35ec46e

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 make g++ git pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pnpm@12.11.1

COPY sources/main /opt/materials/sources/main
COPY SOURCE_MANIFEST.json /opt/materials/
COPY harness-lab/verify-source.mjs /opt/materials/source-verifier/verify-source.mjs
# 原始导出保持不变；官方资源准备和构建步骤在独立构建树
# 中生成产物，运行时仍可核验源码来源。
RUN node /opt/materials/source-verifier/verify-source.mjs --source-only \
    && cp -a /opt/materials/sources/main /opt/openclaw

WORKDIR /opt/openclaw
COPY harness-lab/build.npmrc /opt/openclaw/.npmrc
RUN --mount=type=cache,id=harness-lab-openclaw-pnpm-linux-arm64,target=/pnpm/store,sharing=locked \
    pnpm install --frozen-lockfile --store-dir /pnpm/store \
      --config.supportedArchitectures.os=linux \
      --config.supportedArchitectures.cpu=arm64 \
      --config.supportedArchitectures.libc=glibc

# 使用真实插件资源执行官方 Docker 构建；不伪造 A2UI/UI 文件，
# 不更新依赖，也不手工替换为预构建文件。
# 首个初筛镜像保留完整安装；镜像大小与后续运行时裁剪
# 均以实际测量为准，不提前声称已优化。
RUN OPENCLAW_TSDOWN_MAX_OLD_SPACE_MB=4352 RAYON_NUM_THREADS=1 ROLLDOWN_WORKER_THREADS=1 \
    node --input-type=module -e '\
      import { readFileSync, writeFileSync } from "node:fs"; \
      import { spawnSync } from "node:child_process"; \
      const recipe = JSON.parse(readFileSync("package.json", "utf8")).scripts["build:docker"]; \
      const target = "node --import ./scripts/tsx.mjs scripts/tsdown-build.mts"; \
      if (typeof recipe !== "string" || recipe.split(target).length !== 2) throw new Error("预期且仅允许一次官方 tsdown 调用"); \
      const effective = recipe.replace(target, target + " --concurrency 1"); \
      writeFileSync("/opt/materials/BUILD_RECIPE.txt", effective + "\n"); \
      writeFileSync("/opt/materials/BUILD_ENVIRONMENT.json", JSON.stringify({node: process.versions.node, heap_mb: process.env.OPENCLAW_TSDOWN_MAX_OLD_SPACE_MB, rayon_threads: process.env.RAYON_NUM_THREADS, rolldown_workers: process.env.ROLLDOWN_WORKER_THREADS, skip_dts: process.env.OPENCLAW_RUN_NODE_SKIP_DTS_BUILD, tsdown_config_concurrency: 1}, null, 2) + "\n"); \
      console.log("按串行 tsdown 配置执行官方构建流程：", effective); \
      const result = spawnSync("sh", ["-c", effective], { stdio: "inherit", env: process.env }); \
      if (result.error) throw result.error; \
      if (result.signal) console.error("官方构建被以下信号终止：", result.signal); \
      process.exit(result.status ?? 1);' \
    && node scripts/docker/verify-fs-safe-native.mjs --package-root /opt/openclaw --mode require \
    && node -e 'const {DatabaseSync}=require("node:sqlite"); const db=new DatabaseSync(":memory:"); db.close()' \
    && pnpm list --depth 0 --json > /opt/materials/INSTALLED_DEPENDENCIES.json \
    && dpkg-query -W > /opt/materials/SYSTEM_PACKAGES.txt \
    && node --version > /opt/materials/NODE_VERSION.txt \
    && pnpm --version > /opt/materials/PNPM_VERSION.txt

COPY LAB_ADAPTER_MANIFEST.json /opt/materials/
COPY harness-lab /opt/materials/harness-lab
RUN node /opt/materials/harness-lab/verify-source.mjs \
    && node --check /opt/materials/harness-lab/entrypoint.mjs \
    && node --check /opt/materials/harness-lab/tools-plugin/index.mjs \
    && node --check /opt/materials/harness-lab/tools-plugin/business-state.mjs \
    && node --check /opt/materials/harness-lab/tools-plugin/detailed-options.mjs \
    && node --check /opt/materials/harness-lab/tools-plugin/browser-client.mjs

ENV NODE_OPTIONS=--max-old-space-size=1024
ENTRYPOINT ["node", "/opt/materials/harness-lab/entrypoint.mjs"]
