import { getConfig, getModelForTask } from "../config/model-map.js";
import { invokeLmStudio } from "../providers/lm-studio.js";
import { LocalLlmError, type LocalLlmRequest, type LocalLlmSuccessResponse } from "../types.js";

export async function routeSummary(request: LocalLlmRequest): Promise<LocalLlmSuccessResponse> {
  if (!request.content.trim()) {
    throw new LocalLlmError("INVALID_INPUT", "summary task requires content");
  }

  const config = getConfig();
  const model = getModelForTask("summary");
  const prompt = `${request.prompt}\n\n请总结以下内容，保留关键事实和结论：\n${request.content}`;
  const content = await invokeLmStudio({
    model,
    prompt,
    endpoint: config.endpoint,
    timeoutSeconds: config.timeouts.default,
    maxTokens: 4096,
  });

  return { success: true, model, content };
}
