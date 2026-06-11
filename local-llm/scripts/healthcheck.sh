#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_DIR}/config.yaml"
SERVER_FILE="${PROJECT_DIR}/dist/server.js"

if [[ ! -f "${SERVER_FILE}" ]]; then
  echo "FAIL dist/server.js not found. Run pnpm build first." >&2
  exit 1
fi

read -r ENDPOINT MODEL <<EOF
$(node --input-type=module - "${CONFIG_FILE}" <<'NODE'
import { readFileSync } from 'node:fs';
const configPath = process.argv[2];
const text = readFileSync(configPath, 'utf8');
const endpoint = text.match(/^\s*endpoint:\s*(\S+)\s*$/m)?.[1] ?? 'http://localhost:8080';
const model = text.match(/^\s*model:\s*(\S+)\s*$/m)?.[1] ?? 'qwen3.5-9b-q4_k_m.gguf';
console.log(`${endpoint} ${model}`);
NODE
)
EOF

node --input-type=module - "${ENDPOINT}" "${MODEL}" <<'NODE'
const endpoint = process.argv[2].replace(/\/$/, '');
const expectedModel = process.argv[3];
try {
  const response = await fetch(`${endpoint}/v1/models`, { signal: AbortSignal.timeout(5000) });
  if (!response.ok) {
    console.error(`FAIL llama-server endpoint returned ${response.status}`);
    process.exit(1);
  }
  const payload = await response.json();
  const modelIds = Array.isArray(payload.data) ? payload.data.map((model) => model.id).filter(Boolean) : [];
  console.log(JSON.stringify({
    status: 'healthy',
    endpoint,
    model: expectedModel,
    models: modelIds,
    modelCount: modelIds.length,
  }, null, 2));
} catch (error) {
  console.error(JSON.stringify({
    status: 'unhealthy',
    endpoint,
    model: expectedModel,
    error: error instanceof Error ? error.message : String(error),
    hint: 'Start llama-server with: llama-server -m /path/to/qwen3.5-9b-q4_k_m.gguf --mmproj /path/to/mmproj-f16.gguf --host 127.0.0.1 --port 8080',
  }, null, 2));
  process.exit(1);
}
NODE
