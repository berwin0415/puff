# 通过 MCP 调用本地模型 —— 开发指南

本文档说明如何通过 **MCP（Model Context Protocol）** 调用本地 LLM，供其他代码 Agent 使用。我们的实际案例是 `ollama-mcp-server` 注册到 Hermes Agent。

---

## 架构概览

```
┌─────────────────────────────────────────────────┐
│                   其他 Agent                     │
│          (Claude Code / Codex / 你的代码)         │
└──────────────────────┬──────────────────────────┘
                       │ MCP (stdio 或 HTTP)
                       ▼
┌─────────────────────────────────────────────────┐
│                ollama-mcp-server                  │
│          Python -m ollama_mcp.server              │
└──────────────────────┬──────────────────────────┘
                       │ HTTP API (localhost:11434)
                       ▼
┌─────────────────────────────────────────────────┐
│              Ollama 服务                          │
│         (qwen3.5-9b / 其他本地模型)               │
└─────────────────────────────────────────────────┘
```

**关键点：** MCP 是中间的"适配层"——把本地 LLM 的 HTTP API 包装成标准的 MCP 工具，任何支持 MCP 的 Agent 都能直接调用。

---

## 环境信息

| 项目 | 值 |
|------|-----|
| GPU | NVIDIA RTX 5070 Ti 16GB VRAM |
| CUDA | 可用（WSL2 /dev/dxg 透传） |
| Ollama 版本 | 0.30.7 |
| 当前模型 | qwen3.5-9b:latest（Q4_K_M / 5.3GB） |
| Ollama API | http://localhost:11434 |

---

## 方案一：ollama-mcp-server（推荐）

这是目前使用的方式。一句话安装：

```bash
pip install ollama-mcp-server
```

然后启动：

```bash
# 启动 Ollama 服务（如果没运行）
ollama serve

# 启动 MCP 服务器
python3 -m ollama_mcp.server
```

### 注册到 Hermes

在 `~/.hermes/config.yaml` 中添加：

```yaml
mcp_servers:
  ollama:
    command: python3
    args:
      - -m
      - ollama_mcp.server
    enabled: true
```

重启 gateway 后，自动获得 9 个 MCP 工具（对话、健康检查、资源监控等）。

### 暴露的工具

| 工具名 | 功能 | 适用场景 |
|--------|------|---------|
| `list_local_models` | 列出本地模型 | 检查已安装模型 |
| `local_llm_chat` | 聊天对话 | 通用问答、生成 |
| `ollama_health_check` | 健康检查 | Server 状态诊断 |
| `system_resource_check` | 系统资源检查 | GPU/CPU/内存 |
| `suggest_models` | 模型推荐 | 按任务推荐最佳模型 |
| `remove_model` | 删除模型 | 清理磁盘 |
| `start_ollama_server` | 启动 Ollama | 自动恢复服务 |
| `select_chat_model` | 选择模型 | 交互式选择 |
| `test_model_responsiveness` | 响应测试 | 基准测试 |

---

## 方案二：直接调用 Ollama HTTP API（简单粗暴）

不经过 MCP，直接 `curl` 或 HTTP 请求：

```bash
curl http://localhost:11434/api/generate \
  -d '{"model":"qwen3.5-9b","prompt":"你好","stream":false}'
```

```python
import requests
resp = requests.post("http://localhost:11434/api/chat", json={
    "model": "qwen3.5-9b",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": False
})
print(resp.json()["message"]["content"])
```

**适合：** 快速原型、脚本调用、没有 MCP 客户端的环境。

---

## 方案三：自定义 MCP Server

如果你的 Agent 不支持 `ollama-mcp-server`，可以用 Python 实现一个最简单 MCP Server：

```python
import json, sys, requests

def handle_request(request):
    method = request.get("method")
    
    if method == "list_tools":
        return {
            "tools": [{
                "name": "local_chat",
                "description": "与本地模型对话",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "model": {"type": "string", "default": "qwen3.5-9b"}
                    },
                    "required": ["message"]
                }
            }]
        }
    elif method == "call_tool":
        args = request["params"]["arguments"]
        message = args["message"]
        model = args.get("model", "qwen3.5-9b")
        
        resp = requests.post("http://localhost:11434/api/chat", json={
            "model": model,
            "messages": [{"role": "user", "content": message}],
            "stream": False
        })
        content = resp.json()["message"]["content"]
        
        return {"content": [{"type": "text", "text": content}]}

# 启动 stdio MCP Server
for line in sys.stdin:
    request = json.loads(line)
    response = handle_request(request)
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()
```

---

## 当前环境模型

```
$ curl -s http://localhost:11434/api/tags | python3 -m json.tool
{
  "models": [
    {
      "name": "qwen3.5-9b:latest",
      "size": 5680523283,       # ~5.3GB
      "details": {
        "family": "qwen35",
        "parameter_size": "9.0B",
        "quantization_level": "Q4_K_M"
      }
    }
  ]
}
```

---

## 最佳实践

| 场景 | 推荐方式 |
|------|---------|
| 代码 Agent 集成 | ollama-mcp-server + Hermes MCP |
| 脚本/自动化 | Ollama HTTP API |
| 简单测试 | `ollama run qwen3.5-9b` |
| 离线/无网络 | 同上所有方案，通通本地运行 |

---

## 注意事项

- Ollama 启动后会自动加载模型到 GPU，首次请求会加载较慢（5-10s）
- qwen3.5-9b Q4_K_M 占用约 5.5GB VRAM，16GB 绰绰有余
- 系统资源检查 MCP 在 WSL 内看不到 NVIDIA GPU（WSL 限制），但 Ollama 本身会使用 GPU
- MCP 服务器启动后，需要重启 Hermes gateway 或 `/reset` 才会加载新工具
