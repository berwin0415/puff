# Documentation

This directory collects project notes that are not the top-level project entry
point. Keep the root `README.md` short and link deeper material from here.

## Local LLM And MCP

- [Hermes Local AI Gateway Design](hermes-local-ai-gateway-design.md)
  describes the `local_llm` tool requirements, routing model, and MCP boundary.
- [Local Model MCP Guide](local-model-mcp-guide.md) describes the current
  Ollama MCP integration approach and operating notes.
- [Custom Local Model MCP Guide](local-model-mcp-custom-guide.md) keeps the
  minimal custom MCP server example and alternative implementation notes.
- [Local LLM MCP Server](local-llm-mcp-server.md) is the runnable subproject guide
  for installation, configuration, `llama-server`, health checks, and services.

## Rendering Notes

- [Video Board Rendering Design](video-board-rendering-design.md) documents the
  browser video board rendering module boundary, runtime entities, and flow.
- [Offscreen Rendering Design](offscreen-rendering-design.md) documents the
  Node/offscreen render worker model and its relationship to video board
  rendering.

## Local Environment

- [Homebrew Mirrors](homebrew-mirrors.md) records Homebrew mirror configuration
  and troubleshooting steps.

## Organization Rules

- Keep project entry, quick start, and primary links in the root `README.md`.
- Keep runnable package instructions in the package-level README.
- Put design notes, environment notes, and long-form guides in `docs/`.
- Name docs by topic and purpose, for example `*-design.md`, `*-guide.md`, or
  `*-troubleshooting.md`.
