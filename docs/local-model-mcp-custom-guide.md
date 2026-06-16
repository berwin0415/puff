# 本地模型 MCP 调用指南

> 面向其他代码 Agent（Claude Code / Codex / Cursor 等）如何将本地大模型封装为 MCP 服务，实现工具化调用。

## 为什么需要 MCP

直接在 Agent 里 `curl http://localhost:11434/api/chat` 太原始了：
- 每次都要拼 JSON、处理 stream、解析错误
- Agent 无法感知模型状态（是否在跑、哪个模型可用）
- 没有工具化的交互界面

MCP（Model Context Protocol）把本地模型包装成 **标准工具**，Agent 像调用其他工具一样调用它。

## 架构

```
代码 Agent（Claude Code / Codex / any MCP client）
        │
        ▼
  ┌─────────────────────┐
  │     MCP 服务器       │  ← 你要实现的部分
  │  （stdin/stdout）     │
  └──────┬──────────────┘
         │ HTTP (localhost:11434)
         ▼
  ┌──────────────┐
  │  Ollama 服务  │
  ├──────────────┤
  │ qwen3.5-9b   │
  │ (其他模型...) │
  └──────────────┘
```

## 快速上手：Python 实现 MCP 服务器

### 1. 安装依赖

```bash
pip install mcp httpx
```

### 2. 最小实现（ollama-mcp.py）

```python
import json
import httpx
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.types as types

server = Server("ollama-local")

OLLAMA_BASE = "http://localhost:11434"

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_models",
            description="列出本地已安装的 Ollama 模型",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="chat",
            description="与本地模型对话",
            inputSchema={
                "type": "object",
                "properties": {
                    "model": {
                        "type": "string",
                        "description": "模型名称，如 qwen3.5-9b, deepseek-r1:7b",
                    },
                    "prompt": {"type": "string", "description": "用户输入"},
                },
                "required": ["model", "prompt"],
            },
        ),
        types.Tool(
            name="health",
            description="检查 Ollama 服务是否正常运行",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict
) -> list[types.TextContent]:
    async with httpx.AsyncClient(timeout=120) as client:
        if name == "list_models":
            resp = await client.get(f"{OLLAMA_BASE}/api/tags")
            return [types.TextContent(
                type="text", text=json.dumps(resp.json(), indent=2)
            )]

        if name == "chat":
            resp = await client.post(
                f"{OLLAMA_BASE}/api/chat",
                json={
                    "model": arguments["model"],
                    "messages": [{"role": "user", "content": arguments["prompt"]}],
                    "stream": False,
                },
            )
            result = resp.json()
            return [types.TextContent(
                type="text",
                text=result["message"]["content"],
            )]

        if name == "health":
            try:
                resp = await client.get(f"{OLLAMA_BASE}/api/tags")
                models = resp.json().get("models", [])
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "healthy",
                        "models": len(models),
                        "model_list": [m["name"] for m in models],
                    }),
                )]
            except Exception as e:
                return [types.TextContent(
                    type="text", text=json.dumps({"status": "unhealthy", "error": str(e)})
                )]

    raise ValueError(f"Unknown tool: {name}")

async def main():
    async with server.run_stdio():
        pass

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### 3. 注册到代码 Agent

**Claude Code / Cursor：**
```json
// .claude/mcp.json 或 .cursor/mcp.json
{
  "mcpServers": {
    "ollama-local": {
      "command": "python",
      "args": ["/path/to/ollama-mcp.py"]
    }
  }
}
```

**Hermes Agent（~/.hermes/config.yaml）：**
```yaml
mcp_servers:
  ollama-local:
    command: /home/berwin/.hermes/hermes-agent/venv/bin/python
    args:
      - /path/to/ollama-mcp.py
    enabled: true
```

## 扩展功能

基础实现只有 3 个工具，你可以按需添加：

| 工具 | 功能 | Ollama API |
|------|------|------------|
| `list_models` | 列出模型 | `GET /api/tags` |
| `chat` | 对话 | `POST /api/chat` |
| `generate` | 文本补全 | `POST /api/generate` |
| `health` | 健康检查 | `GET /api/tags` |
| `embed` | 文本嵌入 | `POST /api/embed` |
| `pull_model` | 下载模型 | `POST /api/pull` |
| `show_model` | 模型详情 | `POST /api/show` |
| `check_resources` | 系统资源 | 本地 `psutil` |

## 完整示例仓库

社区已有的 MCP 实现可以直接用：

- **ollama-mcp-server**（已安装）：`pip install ollama-mcp-server` → `python -m ollama_mcp.server`
- **mcp-ollama**（Node.js）：`npx @tumra/mcp-ollama`

两个的区别：
- `ollama_mcp`（Python）：注册到 Hermes，通过 `mcp_ollama_*` 工具名访问
- 自己实现：更灵活，可按需定制工具和行为

## 注意事项

1. **Ollama 必须运行**：`ollama serve`（后台）或 systemd 服务
2. **超时设置**：本地模型推理慢，MCP timeout 建议 ≥ 120s
3. **GPU 加速**：RTX 5070 Ti 16GB VRAM，qwen3.5-9b Q4 约占 5.6GB
4. **模型选择**：简单任务用 qwen3.5-4b（更快），复杂任务用 qwen3.5-9b
5. **stream vs 非 stream**：非 stream 实现简单，stream 能提前显示

## 测试验证

```bash
# 1. 启动 Ollama
ollama serve

# 2. 启动 MCP 服务器（stdio 模式）
python /path/to/ollama-mcp.py

# 3. 用 mcp-cli 测试
npx @modelcontextprotocol/inspector python /path/to/ollama-mcp.py

# 4. 或直接用 curl 测 Ollama
curl http://localhost:11434/api/chat \
  -d '{"model":"qwen3.5-9b","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

## 环境信息

- **系统**：WSL2 (Ubuntu) on Windows 11
- **Ollama**：v0.30.7，路径 `/usr/local/bin/ollama`
- **GPU**：NVIDIA RTX 5070 Ti 16GB VRAM
- **模型**：qwen3.5-9b:latest (Q4_K_M, ~5.3GB)
- **Python**：3.11.15（Hermes venv）
