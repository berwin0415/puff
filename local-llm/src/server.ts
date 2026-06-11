#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  type CallToolResult,
} from "@modelcontextprotocol/sdk/types.js";
import {
  handleLocalLlmTool,
  LOCAL_LLM_TOOL_NAME,
  localLlmInputSchema,
} from "./tools/local-llm.js";

const server = new Server(
  {
    name: "local-llm",
    version: "0.1.0",
  },
  {
    capabilities: {
      tools: {},
    },
  },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: LOCAL_LLM_TOOL_NAME,
      description:
        "用于调用本地 AI 能力，适用于图片理解、OCR 后文本处理、信息抽取、JSON结构化输出、分类、标签生成和简单总结。",
      inputSchema: localLlmInputSchema,
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request): Promise<CallToolResult> => {
  if (request.params.name !== LOCAL_LLM_TOOL_NAME) {
    return {
      content: [
        {
          type: "text",
          text: JSON.stringify(
            {
              success: false,
              error: {
                code: "INVALID_TASK",
                message: `Unknown tool: ${request.params.name}`,
              },
            },
            null,
            2,
          ),
        },
      ],
      isError: true,
    };
  }

  const text = await handleLocalLlmTool(request.params.arguments ?? {});
  return {
    content: [
      {
        type: "text",
        text,
      },
    ],
  };
});

const transport = new StdioServerTransport();
await server.connect(transport);
