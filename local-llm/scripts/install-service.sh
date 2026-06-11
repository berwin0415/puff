#!/usr/bin/env bash
set -euo pipefail

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. systemd user service install is only available on Linux/systemd." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_SOURCE="${PROJECT_DIR}/deploy/local-llm.service"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_TARGET="${SERVICE_DIR}/local-llm.service"
NODE_BIN="$(command -v node)"

mkdir -p "${SERVICE_DIR}"
sed \
  -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
  -e "s#__NODE_BIN__#${NODE_BIN}#g" \
  "${SERVICE_SOURCE}" > "${SERVICE_TARGET}"

systemctl --user daemon-reload
systemctl --user enable local-llm.service
systemctl --user restart local-llm.service
systemctl --user status local-llm.service --no-pager
