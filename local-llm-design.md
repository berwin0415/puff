# Hermes Local AI Gateway MCP 技术需求文档

## 一、项目背景

当前 Hermes Agent 使用 DeepSeek V4 作为主力推理模型。

为了降低成本、提高响应速度，并充分利用本地算力，希望接入 Ollama 本地模型集群，将图片识别、文档处理、结构化抽取等低复杂度任务下沉至本地模型执行。

目标是向 Hermes 暴露一个统一 MCP Tool：

```text
local_llm
```

Hermes 不感知具体模型名称，仅感知能力类型（task）。

由 MCP Server 内部完成模型路由。

---

## 二、总体架构

```text
┌─────────────────────┐
│      Hermes         │
│   DeepSeek V4       │
└──────────┬──────────┘
           │ Tool Call
           ▼
┌─────────────────────┐
│ local_llm MCP Tool  │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Local AI Gateway    │
├─────────────────────┤
│ Vision Router       │
│ Extract Router      │
│ Summary Router      │
│ Classify Router     │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ Ollama Server       │
├─────────────────────┤
│ qwen2.5-vl          │
│ qwen3-8b            │
│ qwen3-4b            │
│ future models       │
└─────────────────────┘
```

---

## 三、设计原则

### 1. Hermes 不感知模型

禁止出现：

```text
qwen3
qwen-vl
gemma
llama
```

Hermes 只看到：

```text
vision
extract
summary
classify
```

---

### 2. 模型路由由 Gateway 决定

Gateway 内部维护：

```ts
const MODEL_MAP = {
    vision: "qwen2.5-vl:7b",
    extract: "qwen3:8b",
    summary: "qwen3:8b",
    classify: "qwen3:4b"
};
```

后续模型升级不影响 Hermes。

---

### 3. 单 Tool 原则

MCP 仅暴露：

```text
local_llm
```

避免出现多个模型工具导致 Tool Selection 混乱。

---

## 四、MCP Tool 设计

Tool Name:

```text
local_llm
```

Description:

```text
用于调用本地 AI 能力。

适用于：

- 图片理解
- OCR 后文本处理
- 信息抽取
- JSON结构化输出
- 分类
- 标签生成
- 简单总结

优先用于低复杂度任务，
避免占用主模型推理资源。
```

---

## 五、输入参数

Schema:

```json
{
  "task": "vision|extract|summary|classify",
  "prompt": "string",
  "content": "string",
  "attachments": []
}
```

字段说明：

| 字段          | 说明   |
| ----------- | ---- |
| task        | 能力类型 |
| prompt      | 任务指令 |
| content     | 文本内容 |
| attachments | 附件列表 |

---

## 六、任务类型定义

### vision

用途：

```text
图片理解
截图分析
UI识别
流程图识别
商品识别
```

模型：

```text
qwen2.5-vl
```

输入：

```json
{
  "task":"vision",
  "prompt":"分析图片内容",
  "attachments":[
      "/tmp/a.png"
  ]
}
```

---

### extract

用途：

```text
实体提取
信息抽取
结构化转换
JSON生成
```

模型：

```text
qwen3:8b
```

输入：

```json
{
  "task":"extract",
  "prompt":"提取用户信息",
  "content":"..."
}
```

输出：

```json
{
  "name":"",
  "phone":"",
  "email":""
}
```

---

### summary

用途：

```text
文档摘要
会议纪要
网页总结
```

模型：

```text
qwen3:8b
```

---

### classify

用途：

```text
内容分类
标签生成
风险识别
意图识别
```

模型：

```text
qwen3:4b
```

---

## 七、Gateway实现

Node.js

推荐：

```text
TypeScript
Fastify
MCP SDK
Ollama SDK
Zod
```

目录结构：

```text
src/

├── server.ts
├── tools/
│   └── local-llm.ts

├── routers/
│   ├── vision.ts
│   ├── extract.ts
│   ├── summary.ts
│   └── classify.ts

├── providers/
│   └── ollama.ts

├── config/
│   └── model-map.ts
```

---

## 八、Ollama Provider

统一封装：

```ts
invoke({
    model,
    prompt,
    images
})
```

内部调用：

```http
POST /api/chat
```

返回统一格式：

```json
{
  "success": true,
  "model": "qwen3:8b",
  "content": "..."
}
```

---

## 九、错误处理

统一返回：

```json
{
  "success": false,
  "error": {
      "code": "MODEL_UNAVAILABLE",
      "message": "..."
  }
}
```

错误类型：

```text
MODEL_UNAVAILABLE
MODEL_TIMEOUT
INVALID_TASK
INVALID_INPUT
OLLAMA_ERROR
```

---

## 十、配置文件

config.yaml

```yaml
ollama:
  endpoint: http://localhost:11434

models:
  vision: qwen2.5-vl:7b
  extract: qwen3:8b
  summary: qwen3:8b
  classify: qwen3:4b

timeouts:
  default: 60
```

---

## 十一、未来扩展

新增任务无需修改 Hermes：

```yaml
rag: bge-m3
ocr: got-ocr
translate: qwen3:8b
rewrite: qwen3:8b
```

Hermes 自动获得新能力。

---

## 十二、验收标准

### 基础能力

* MCP Server 可启动
* Hermes 可发现 local_llm Tool
* Ollama 调用正常

### Vision

上传图片后返回图片描述

### Extract

输入文本后返回结构化 JSON

### Summary

输入长文本后返回摘要

### Classify

输入文本后返回分类结果

### 可维护性

新增模型或替换模型仅修改配置文件，无需修改 Hermes 配置。
