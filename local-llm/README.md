# local-llm MCP Server

Hermes Local AI Gateway MCP server backed by `llama-server`.

The current local environment uses one multimodal GGUF model for simple text and image tasks:

- `qwen3.5-9b-q4_k_m.gguf`
- `mmproj-f16.gguf`

Hermes still only sees the `local_llm` tool and task types: `vision`, `extract`, `summary`, `classify`.

## Install

```bash
cd local-llm
pnpm install
pnpm build
```

If `pnpm` is not installed locally, use the pinned package manager through `npx`:

```bash
npx pnpm@10.23.0 install
npx pnpm@10.23.0 build
```

## Start llama-server

Start `llama-server` separately before calling the MCP tool:

```bash
llama-server \
  -m /path/to/qwen3.5-9b-q4_k_m.gguf \
  --mmproj /path/to/mmproj-f16.gguf \
  --host 127.0.0.1 \
  --port 8080
```

Model file paths depend on the local machine and are not hard-coded in this repo.

## MCP Client Mode

This is the recommended mode for Hermes, Codex, Claude Code, Cursor, and other MCP clients. The client starts this stdio server when it needs the tool.

```bash
pnpm start
```

The server exposes one tool: `local_llm`.

MCP Client example:

```json
{
  "mcpServers": {
    "local-llm": {
      "command": "node",
      "args": ["/Users/hanbowen01/repo/puff/local-llm/dist/server.js"]
    }
  }
}
```

Do not run the same MCP stdio server under PM2 or systemd when Hermes is expected to own the stdio connection.

## Tool Input

```json
{
  "task": "vision|extract|summary|classify",
  "prompt": "string",
  "content": "string",
  "attachments": ["/path/to/image.png"]
}
```

## Configuration

Edit `config.yaml` to update the `llama-server` endpoint, task routing, or timeout:

```yaml
llamaServer:
  endpoint: http://localhost:8080
  model: qwen3.5-9b-q4_k_m.gguf
  mmproj: mmproj-f16.gguf
models:
  vision: qwen3.5-9b-q4_k_m.gguf
  extract: qwen3.5-9b-q4_k_m.gguf
  summary: qwen3.5-9b-q4_k_m.gguf
  classify: qwen3.5-9b-q4_k_m.gguf
timeouts:
  default: 120
```

`mmproj-f16.gguf` is loaded by the `llama-server` process. The MCP server only calls the OpenAI-compatible HTTP API.

## Deploy Preparation

```bash
pnpm run deploy:local
```

The deploy script installs dependencies, builds TypeScript, and prepares `logs/`. Use `pnpm run deploy:local` because `pnpm deploy` is reserved by pnpm for workspace deployment.

## Health Check

```bash
pnpm healthcheck
```

The health check verifies:

- `dist/server.js` exists
- `llama-server` `endpoint` from `config.yaml` responds to `/v1/models`
- available model IDs are returned by the server

## Process Supervision

These modes are for local operations or non-MCP-client ownership scenarios. They are not required for normal Hermes stdio usage.

### MCP stdio server with systemd user service

```bash
pnpm service:install
systemctl --user status local-llm.service
journalctl --user -u local-llm.service -f
```

The service template is `deploy/local-llm.service`. It uses `Restart=on-failure`.

### llama-server with systemd user service

Set paths for your local machine, then install the service:

```bash
export LLAMA_SERVER_BIN=/path/to/llama-server
export LLAMA_MODEL_PATH=/path/to/qwen3.5-9b-q4_k_m.gguf
export LLAMA_MMPROJ_PATH=/path/to/mmproj-f16.gguf
export LLAMA_HOST=127.0.0.1
export LLAMA_PORT=8080
pnpm llama:service:install
```

Check logs:

```bash
systemctl --user status llama-server.service
journalctl --user -u llama-server.service -f
```

### PM2 for MCP stdio server

Install PM2 separately if needed:

```bash
pnpm add -g pm2
```

Commands:

```bash
pnpm pm2:start
pnpm pm2:logs
pnpm pm2:restart
pnpm pm2:stop
```

PM2 logs are written to `logs/`.

## Inspect

```bash
npx @modelcontextprotocol/inspector node dist/server.js
```
