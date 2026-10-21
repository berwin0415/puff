ARG NODE_IMAGE=node:24.22.0-bookworm-slim@sha256:156e9bc7a0a267cc6ca77b4283e32b123479342df684ce297455d7b95ff5a6b5
FROM ${NODE_IMAGE}

ENV NODE_OPTIONS=--max-old-space-size=1024 \
    CODEX_HOME=/state/codex-home \
    DO_NOT_TRACK=1 \
    OTEL_SDK_DISABLED=true

# These are the complete official three-package Linux arm64 runtime closure.
# No npm resolution, install scripts, source compilation, or model calls occur.
COPY sources/main /opt/materials/sources/main
COPY SOURCE_MANIFEST.json REGISTRY_MANIFEST.json LAB_ADAPTER_MANIFEST.json /opt/materials/
COPY registry /opt/materials/registry
COPY packages /opt/materials/packages
COPY harness-lab /opt/materials/harness-lab
RUN node /opt/materials/harness-lab/verify-source.mjs \
    && node --check /opt/materials/harness-lab/entrypoint.mjs \
    && node --check /opt/materials/harness-lab/http-mcp-bridge.mjs \
    && /opt/materials/packages/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex --version > /opt/materials/CLI_VERSION.txt \
    && node --version > /opt/materials/NODE_VERSION.txt \
    && dpkg-query -W > /opt/materials/SYSTEM_PACKAGES.txt

ENTRYPOINT ["node", "/opt/materials/harness-lab/entrypoint.mjs"]
