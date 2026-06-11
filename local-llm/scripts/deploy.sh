#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PNPM_VERSION="10.23.0"

cd "${PROJECT_DIR}"

if command -v pnpm >/dev/null 2>&1; then
  PNPM_CMD=(pnpm)
else
  PNPM_CMD=(npx "pnpm@${PNPM_VERSION}")
fi

"${PNPM_CMD[@]}" install
"${PNPM_CMD[@]}" build
mkdir -p "${PROJECT_DIR}/logs"

cat <<EOF
local-llm deployment prepared.

MCP client mode:
  node ${PROJECT_DIR}/dist/server.js

PM2 mode:
  ${PNPM_CMD[*]} pm2:start

systemd user service mode:
  ${PNPM_CMD[*]} service:install
EOF
