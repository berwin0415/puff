# puff

A tiny desktop companion, currently focused on a local LLM MCP gateway for
Hermes and other code agents.

## What Is Here

- `local-llm/`: TypeScript MCP server that exposes one `local_llm` tool backed
  by a local `llama-server`.
- `docs/`: design notes, local model integration guides, rendering notes, and
  local environment troubleshooting docs.

## Quick Start

```bash
cd local-llm
pnpm install
pnpm build
pnpm llama:start
pnpm healthcheck
```

Start the MCP stdio server manually when needed:

```bash
pnpm start
```

For MCP clients such as Hermes, Codex, Claude Code, or Cursor, register the
built server:

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

See `docs/local-llm-mcp-server.md` for full install, configuration, llama-server
management, health check, and process supervision details.

## MCP Tool

The server exposes a single tool:

```text
local_llm
```

Input shape:

```json
{
  "task": "vision|extract|summary|classify",
  "prompt": "string",
  "content": "string",
  "attachments": ["/path/to/image.png"]
}
```

Hermes and other agents only choose the task type. Model routing and runtime
details stay inside the local gateway.

## Documentation

Start with the docs index:

- [docs/README.md](docs/README.md)

Key documents:

- [docs/local-llm-mcp-server.md](docs/local-llm-mcp-server.md): local MCP
  server usage.
- [docs/hermes-local-ai-gateway-design.md](docs/hermes-local-ai-gateway-design.md):
  Hermes local AI gateway requirements and architecture.
- [docs/local-model-mcp-guide.md](docs/local-model-mcp-guide.md): current
  local model MCP development guide.
- [docs/local-model-mcp-custom-guide.md](docs/local-model-mcp-custom-guide.md):
  custom MCP wrapper example for Ollama.

## Development

```bash
cd local-llm
pnpm dev
pnpm build
pnpm healthcheck
```

The project uses Node.js 20 or newer and `pnpm@10.23.0`.
