import { z } from "zod";
import { routeClassify } from "../routers/classify.js";
import { routeExtract } from "../routers/extract.js";
import { routeSummary } from "../routers/summary.js";
import { routeVision } from "../routers/vision.js";
import {
  LocalLlmError,
  type LocalLlmFailureResponse,
  type LocalLlmRequest,
  type LocalLlmResponse,
} from "../types.js";

export const LOCAL_LLM_TOOL_NAME = "local_llm";

export const localLlmInputSchema = {
  type: "object",
  properties: {
    task: {
      type: "string",
      enum: ["vision", "extract", "summary", "classify"],
      description: "能力类型，支持 vision、extract、summary、classify",
    },
    prompt: {
      type: "string",
      description: "任务指令",
    },
    content: {
      type: "string",
      description: "文本内容",
    },
    attachments: {
      type: "array",
      items: { type: "string" },
      description: "附件路径列表，vision 任务用于传入图片路径",
    },
  },
  required: ["task", "prompt"],
  additionalProperties: false,
} as const;

const inputParser = z.object({
  task: z.enum(["vision", "extract", "summary", "classify"]),
  prompt: z.string().min(1),
  content: z.string().optional().default(""),
  attachments: z.array(z.string()).optional().default([]),
});

function toFailureResponse(error: unknown): LocalLlmFailureResponse {
  if (error instanceof LocalLlmError) {
    return {
      success: false,
      error: {
        code: error.code,
        message: error.message,
      },
    };
  }

  if (error instanceof z.ZodError) {
    return {
      success: false,
      error: {
        code: "INVALID_INPUT",
        message: error.issues.map((issue) => issue.message).join("; "),
      },
    };
  }

  return {
    success: false,
    error: {
      code: "OLLAMA_ERROR",
      message: error instanceof Error ? error.message : "Unknown local_llm error",
    },
  };
}

async function routeRequest(request: LocalLlmRequest): Promise<LocalLlmResponse> {
  switch (request.task) {
    case "vision":
      return routeVision(request);
    case "extract":
      return routeExtract(request);
    case "summary":
      return routeSummary(request);
    case "classify":
      return routeClassify(request);
    default:
      throw new LocalLlmError("INVALID_TASK", `Unsupported task: ${request.task satisfies never}`);
  }
}

export async function handleLocalLlmTool(arguments_: unknown): Promise<string> {
  try {
    const request = inputParser.parse(arguments_);
    const response = await routeRequest(request);
    return JSON.stringify(response, null, 2);
  } catch (error) {
    return JSON.stringify(toFailureResponse(error), null, 2);
  }
}
