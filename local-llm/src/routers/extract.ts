import { getConfig, getModelForTask } from "../config/model-map.js";
import { invokeLmStudio } from "../providers/lm-studio.js";
import { LocalLlmError, type LocalLlmRequest, type LocalLlmSuccessResponse } from "../types.js";

export async function routeExtract(request: LocalLlmRequest): Promise<LocalLlmSuccessResponse> {
  if (!request.content.trim()) {
    throw new LocalLlmError("INVALID_INPUT", "extract task requires content");
  }

  const config = getConfig();
  const model = getModelForTask("extract");
  const prompt = `${request.prompt}\n\n请从以下内容中提取信息，并优先返回结构化 JSON：\n${request.content}`;
  const content = await invokeLmStudio({
    model,
    prompt,
    endpoint: config.endpoint,
    timeoutSeconds: config.timeouts.default,
    maxTokens: 4096,
  });

  return { success: true, model, content };
}
