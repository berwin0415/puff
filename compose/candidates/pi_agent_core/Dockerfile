# 源码身份与依赖锁均来自固定版本的官方导出。
ARG NODE_IMAGE=node:24.22.0-bookworm-slim@sha256:156e9bc7a0a267cc6ca77b4283e32b123479342df684ce297455d7b95ff5a6b5
FROM ${NODE_IMAGE}

ENV CI=true \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NODE_OPTIONS=--max-old-space-size=1536

COPY sources/main /opt/materials/sources/main
COPY SOURCE_MANIFEST.json /opt/materials/
COPY harness-lab/verify-source.mjs /opt/materials/source-verifier/verify-source.mjs
RUN node /opt/materials/source-verifier/verify-source.mjs --source-only \
    && cp -a /opt/materials/sources/main /opt/pi-build

WORKDIR /opt/pi-build
# 仅安装选定的三个 workspace 和根目录锁定的编译工具；
# 不执行生命周期脚本、原生 UI 产品或在线目录补全。
# npm ci 核对未经修改的官方 package-lock.json。
RUN --mount=type=cache,id=harness-lab-pi-npm-linux-arm64,target=/npm-cache,sharing=locked \
    npm ci --ignore-scripts --no-audit --no-fund --cache /npm-cache \
      --workspace packages/telemetry --workspace packages/ai --workspace packages/agent \
      --include-workspace-root \
    && node /opt/materials/source-verifier/verify-source.mjs --source-only

COPY LAB_ADAPTER_MANIFEST.json /opt/materials/
COPY harness-lab /opt/materials/harness-lab
RUN node /opt/materials/harness-lab/build-bundle.mjs \
    && node /opt/materials/harness-lab/verify-source.mjs \
    && cp node_modules/.package-lock.json /opt/materials/INSTALLED_DEPENDENCIES.json \
    && dpkg-query -W > /opt/materials/SYSTEM_PACKAGES.txt \
    && node --version > /opt/materials/NODE_VERSION.txt \
    && npm --version > /opt/materials/NPM_VERSION.txt

# 编排层提供只读根目录、2 GiB 内存、1 CPU，以及限定范围的
# 可写 workspace/state/evidence 挂载和本地模型运行 token。
ENTRYPOINT ["node", "/opt/materials/harness-lab/entrypoint.mjs"]
