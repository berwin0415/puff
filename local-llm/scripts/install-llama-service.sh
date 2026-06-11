#!/usr/bin/env bash
set -euo pipefail

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. llama-server systemd user service install is only available on Linux/systemd." >&2
  exit 1
fi

: "${LLAMA_SERVER_BIN:?Set LLAMA_SERVER_BIN to the llama-server executable path}"
: "${LLAMA_MODEL_PATH:?Set LLAMA_MODEL_PATH to qwen3.5-9b-q4_k_m.gguf path}"
: "${LLAMA_MMPROJ_PATH:?Set LLAMA_MMPROJ_PATH to mmproj-f16.gguf path}"

LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
LLAMA_PORT="${LLAMA_PORT:-8080}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_SOURCE="${PROJECT_DIR}/deploy/llama-server.service"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_TARGET="${SERVICE_DIR}/llama-server.service"

mkdir -p "${SERVICE_DIR}"
sed \
  -e "s#__LLAMA_SERVER_BIN__#${LLAMA_SERVER_BIN}#g" \
  -e "s#__LLAMA_MODEL_PATH__#${LLAMA_MODEL_PATH}#g" \
  -e "s#__LLAMA_MMPROJ_PATH__#${LLAMA_MMPROJ_PATH}#g" \
  -e "s#__LLAMA_HOST__#${LLAMA_HOST}#g" \
  -e "s#__LLAMA_PORT__#${LLAMA_PORT}#g" \
  "${SERVICE_SOURCE}" > "${SERVICE_TARGET}"

systemctl --user daemon-reload
systemctl --user enable llama-server.service
systemctl --user restart llama-server.service
systemctl --user status llama-server.service --no-pager
