# 已核验的 linux/arm64 镜像；覆盖时也必须使用固定摘要。
ARG NODE_IMAGE=node:24.22.0-bookworm-slim@sha256:156e9bc7a0a267cc6ca77b4283e32b123479342df684ce297455d7b95ff5a6b5
FROM ${NODE_IMAGE}

ENV CI=true \
    DSH_TELEMETRY_DISABLED=1 \
    TSX_TSCONFIG_PATH=/opt/materials/sources/main/tsconfig.json

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 make g++ git pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pnpm@11.7.0

COPY sources/main /opt/materials/sources/main
COPY SOURCE_MANIFEST.json /opt/materials/
COPY harness-lab/verify-source.mjs /opt/materials/source-verifier/verify-source.mjs

WORKDIR /opt/materials/sources/main
# 适配器变化后仍可复用已核验的源码与依赖层。
# 缓存只提供下载内容；pnpm 仍按冻结的上游锁文件解析。
RUN --mount=type=cache,id=harness-lab-dsh-pnpm-linux-arm64,target=/pnpm/store,sharing=locked \
    node /opt/materials/source-verifier/verify-source.mjs --source-only \
    && pnpm install --frozen-lockfile --store-dir /pnpm/store \
    && node /opt/materials/source-verifier/verify-source.mjs --source-only \
    && dpkg-query -W > /opt/materials/SYSTEM_PACKAGES.txt

COPY LAB_ADAPTER_MANIFEST.json /opt/materials/
COPY harness-lab /opt/materials/harness-lab
RUN node /opt/materials/harness-lab/verify-source.mjs

# I08 使用官方 SDK 源码启动器；DSH 请求显式提供 candidate_options 时，
# 选择 I17 的官方 runProfile/agents.create/agents.resume 入口。
# 两者均不修改固定版本的上游 agent-loop 和会话后端。
ENTRYPOINT ["node", "--import", "tsx/esm", "/opt/materials/harness-lab/entrypoint.ts"]
