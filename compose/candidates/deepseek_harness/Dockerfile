# Verified linux/arm64 image; an override must likewise use a fixed digest.
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
# Adapter changes leave this verified source/dependency layer reusable. The
# cache only supplies downloads; pnpm still resolves the frozen upstream lock.
RUN --mount=type=cache,id=harness-lab-dsh-pnpm-linux-arm64,target=/pnpm/store,sharing=locked \
    node /opt/materials/source-verifier/verify-source.mjs --source-only \
    && pnpm install --frozen-lockfile --store-dir /pnpm/store \
    && node /opt/materials/source-verifier/verify-source.mjs --source-only \
    && dpkg-query -W > /opt/materials/SYSTEM_PACKAGES.txt

COPY LAB_ADAPTER_MANIFEST.json /opt/materials/
COPY harness-lab /opt/materials/harness-lab
RUN node /opt/materials/harness-lab/verify-source.mjs

# I08 uses the official SDK source launcher. An explicit DSH candidate_options
# request selects I17's official runProfile/agents.create/agents.resume entry.
# Both keep the fixed upstream agent-loop and session backend unmodified.
ENTRYPOINT ["node", "--import", "tsx/esm", "/opt/materials/harness-lab/entrypoint.ts"]
