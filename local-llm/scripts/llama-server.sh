#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_DIR}/config.yaml"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/llama-server.log"
PID_FILE="${LOG_DIR}/llama-server.pid"

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
  endpoint: llama.endpoint ?? 'http://127.0.0.1:8080',
  host: llama.host ?? '127.0.0.1',
  port: String(llama.port ?? 8080),
  binary: expandHome(llama.binary ?? '/usr/local/lib/ollama/llama-server'),
  libraryPath: expandHome(llama.libraryPath ?? '/usr/local/lib/ollama'),
  modelPath: expandHome(llama.modelPath ?? '~/models/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf'),
  mmprojPath: expandHome(llama.mmprojPath ?? '~/models/Qwen3.5-9B-GGUF/mmproj-F16.gguf'),
  ngl: String(runtime.ngl ?? 99),
  context: String(runtime.context ?? 4096),
  temperature: String(runtime.temperature ?? 0.7),
};

for (const [key, value] of Object.entries(values)) {
  console.log(`${key}=${JSON.stringify(value)}`);
}
NODE
}

eval "$(read_config)"

is_port_listening() {
  lsof -ti:"${port}" >/dev/null 2>&1
}

status() {
  if ! is_port_listening; then
    echo "llama-server is stopped on ${host}:${port}"
    return 1
  fi

  if curl -fsS "${endpoint%/}/v1/models" >/dev/null 2>&1; then
    echo "llama-server is healthy at ${endpoint}"
    return 0
  fi

  echo "llama-server port ${port} is listening, but /v1/models is unavailable"
  return 1
}

start() {
  mkdir -p "${LOG_DIR}"

  if is_port_listening; then
    echo "llama-server already appears to be running on ${host}:${port}"
    status || true
    return 0
  fi

  LD_LIBRARY_PATH="${libraryPath}:${LD_LIBRARY_PATH:-}" nohup "${binary}" \
    -m "${modelPath}" \
    --mmproj "${mmprojPath}" \
    --host "${host}" --port "${port}" \
    -ngl "${ngl}" -c "${context}" --temp "${temperature}" \
    > "${LOG_FILE}" 2>&1 &

  echo $! > "${PID_FILE}"
  echo "llama-server started with pid $(cat "${PID_FILE}") on ${host}:${port}"
  echo "logs: ${LOG_FILE}"
}

stop() {
  local stopped=0

  if curl -fsS -X POST "${endpoint%/}/close" >/dev/null 2>&1; then
    stopped=1
    echo "llama-server close endpoint accepted shutdown"
  fi

  if is_port_listening; then
    local port_pids
    port_pids="$(lsof -ti:"${port}" 2>/dev/null || true)"
    if [[ -n "${port_pids}" ]]; then
      kill ${port_pids} 2>/dev/null || true
      stopped=1
      echo "llama-server stopped by port ${port}: ${port_pids}"
    fi
  fi

  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      stopped=1
      echo "llama-server stopped by pidfile: ${pid}"
    fi
    rm -f "${PID_FILE}"
  fi

  if [[ "${stopped}" == "0" ]]; then
    echo "llama-server is not running on ${host}:${port}"
  fi
}

restart() {
  stop
  start
}

logs() {
  mkdir -p "${LOG_DIR}"
  touch "${LOG_FILE}"
  tail -f "${LOG_FILE}"
}

case "${1:-status}" in
  start)
    start
    ;;
  stop)
    stop
    ;;
  restart)
    restart
    ;;
  status)
    status
    ;;
  logs)
    logs
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}" >&2
    exit 2
    ;;
esac
