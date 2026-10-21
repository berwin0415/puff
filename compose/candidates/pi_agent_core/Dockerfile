# Source identity and dependency lock are both from the fixed official export.
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
# Only the three selected workspaces and root's locked compiler tooling are
# installed. Lifecycle scripts, native UI products and online catalog hydration
# are not executed. npm ci checks the unmodified official package-lock.json.
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

# The orchestration layer supplies a read-only root, 2 GiB, 1 CPU, scoped
# writable workspace/state/evidence mounts and the local model run token.
ENTRYPOINT ["node", "/opt/materials/harness-lab/entrypoint.mjs"]
