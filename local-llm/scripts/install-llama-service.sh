#!/usr/bin/env bash
set -euo pipefail

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found. llama-server systemd user service install is only available on Linux/systemd." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_DIR}/config.yaml"
SERVICE_SOURCE="${PROJECT_DIR}/deploy/llama-server.service"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_TARGET="${SERVICE_DIR}/llama-server.service"

read_config() {
  node --input-type=module - "${CONFIG_FILE}" <<'NODE'
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import YAML from 'yaml';

const configPath = process.argv[2];
const config = YAML.parse(readFileSync(configPath, 'utf8')) ?? {};
const llama = config.llamaServer ?? {};
const runtime = llama.runtime ?? {};
const expandHome = (value) => typeof value === 'string' && value.startsWith('~/')
  ? `${homedir()}/${value.slice(2)}`
  : value;

const values = {
  LLAMA_SERVER_BIN: expandHome(process.env.LLAMA_SERVER_BIN ?? llama.binary ?? '/usr/local/lib/ollama/llama-server'),
  LLAMA_LIBRARY_PATH: expandHome(process.env.LLAMA_LIBRARY_PATH ?? llama.libraryPath ?? '/usr/local/lib/ollama'),
  LLAMA_MODEL_PATH: expandHome(process.env.LLAMA_MODEL_PATH ?? llama.modelPath ?? '~/models/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf'),
  LLAMA_MMPROJ_PATH: expandHome(process.env.LLAMA_MMPROJ_PATH ?? llama.mmprojPath ?? '~/models/Qwen3.5-9B-GGUF/mmproj-F16.gguf'),
  LLAMA_HOST: process.env.LLAMA_HOST ?? llama.host ?? '127.0.0.1',
  LLAMA_PORT: String(process.env.LLAMA_PORT ?? llama.port ?? 8080),
  LLAMA_NGL: String(process.env.LLAMA_NGL ?? runtime.ngl ?? 99),
  LLAMA_CONTEXT: String(process.env.LLAMA_CONTEXT ?? runtime.context ?? 4096),
  LLAMA_TEMPERATURE: String(process.env.LLAMA_TEMPERATURE ?? runtime.temperature ?? 0.7),
};

for (const [key, value] of Object.entries(values)) {
  console.log(`${key}=${JSON.stringify(value)}`);
}
NODE
}

eval "$(read_config)"

mkdir -p "${SERVICE_DIR}"
sed \
  -e "s#__LLAMA_SERVER_BIN__#${LLAMA_SERVER_BIN}#g" \
  -e "s#__LLAMA_LIBRARY_PATH__#${LLAMA_LIBRARY_PATH}#g" \
  -e "s#__LLAMA_MODEL_PATH__#${LLAMA_MODEL_PATH}#g" \
  -e "s#__LLAMA_MMPROJ_PATH__#${LLAMA_MMPROJ_PATH}#g" \
  -e "s#__LLAMA_HOST__#${LLAMA_HOST}#g" \
  -e "s#__LLAMA_PORT__#${LLAMA_PORT}#g" \
  -e "s#__LLAMA_NGL__#${LLAMA_NGL}#g" \
  -e "s#__LLAMA_CONTEXT__#${LLAMA_CONTEXT}#g" \
  -e "s#__LLAMA_TEMPERATURE__#${LLAMA_TEMPERATURE}#g" \
  "${SERVICE_SOURCE}" > "${SERVICE_TARGET}"

systemctl --user daemon-reload
systemctl --user enable llama-server.service
systemctl --user restart llama-server.service
systemctl --user status llama-server.service --no-pager
