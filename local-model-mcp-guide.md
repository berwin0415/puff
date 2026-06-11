# 本地模型 MCP 调用指南

> 通过 MCP（Model Context Protocol）服务调用本地大模型，无需网络，数据不出本机。

## 整体架构

```
其他代码 Agent（Claude Code / Codex / Cursor 等）
        │
        ▼
    Hermes 网关 ─── MCP Server（ollama-mcp）
                              │
                              ▼
                         Ollama 服务（localhost:11434）
                              │
                              ▼
                    本地模型（qwen3.5-9b / 其他 GGUF）
```

## 前提条件

### 1. 安装 Ollama

```bash
# 已安装：/usr/local/bin/ollama v0.30.7
# 启动服务
ollama serve
```

### 2. 安装 ollama-mcp-server

```bash
# Hermes 虚拟环境中已安装
python3 -m ollama_mcp.server
```

### 3. 注册到 Hermes 配置

编辑 `~/.hermes/config.yaml`，添加 MCP 服务：

```yaml
mcp_servers:
  ollama:
    command: /home/berwin/.hermes/hermes-agent/venv/bin/python
    args:
      - -m
      - ollama_mcp.server
    enabled: true
```

## MCP 工具一览

注册后暴露 9 个工具，通过 `hermes mcp test ollama` 验证：

| 工具 | 说明 |
|------|------|
| `list_local_models` | 列出本地已安装的模型 |
| `local_llm_chat` | 与本地模型对话 |
| `ollama_health_check` | 检查 Ollama 服务健康状态 |
| `system_resource_check` | 检查系统资源（CPU/内存） |
| `suggest_models` | 根据需求推荐最适合的本地模型 |
| `remove_model` | 删除指定模型 |
| `start_ollama_server` | 启动 Ollama 服务 |
| `select_chat_model` | 选择对话模型 |
| `test_model_responsiveness` | 测试模型响应速度 |

## 调用方式

### 通过 Hermes 工具（本代理直接调用）

工具命名规则：`mcp_{server_name}_{tool_name}`

```python
# Python execute_code 中调用
from hermes_tools import terminal

# 检查健康
result = terminal("curl -s http://localhost:11434/api/tags")
print(result["output"])

# 对话
result = terminal("""
curl -s http://localhost:11434/api/chat \\
  -d '{"model":"qwen3.5-9b","messages":[{"role":"user","content":"你好"}],"stream":false}'
""")
```

### 其他代码 Agent 通过 Hermes MCP 调用

其他代码 Agent（Claude Code / Codex）可以通过 Hermes 的 `delegate_task` 间接使用本地模型：

```
delegate_task(
    goal="用本地模型 qwen3.5-9b 分析这段代码",
    context="...代码内容...",
    toolsets=["terminal"]
)
```

或者直接通过 HTTP API 调用 Ollama：

```bash
# 聊天
curl http://localhost:11434/api/chat \
  -d '{"model":"qwen3.5-9b","messages":[{"role":"user","content":"你好"}],"stream":false}'

# 生成
curl http://localhost:11434/api/generate \
  -d '{"model":"qwen3.5-9b","prompt":"写一首诗","stream":false}'
```

## 适用场景

| 场景 | 推荐模型 | 说明 |
|------|---------|------|
| 代码生成/审查 | qwen3.5-9b | 9B Q4 适合代码任务，6s 首响应 |
| 简单问答 | qwen3.5-4b | 可以考虑拉个更轻量的 |
| 文本分析 | qwen3.5-9b | 上下文 262K，大文档也能处理 |
| 视觉识别 | 需额外配置 | 需要 mmproj + llama-server |

## 注意事项

- **Ollama 必须在运行状态**，启动命令：`ollama serve`
- **GPU 加速**：RTX 5070 Ti 16GB VRAM，模型约占 5.6GB
- **不支持联网**：本地模型无网络搜索能力
- **性能**：qwen3.5-9b 约 14 tok/s（GPU 推理）

## 安装其他模型

```bash
# 从模型库拉取
ollama pull qwen3.5-4b     # 4B 轻量版
ollama pull qwen3.5-14b    # 14B 更强（需要更多 VRAM）
ollama pull deepseek-r1:7b # DeepSeek R1 7B
```
