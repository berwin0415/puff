import { getConfig, getModelForTask } from "../config/model-map.js";
import { invokeLlamaServer } from "../providers/llama-server.js";
import { LocalLlmError, type LocalLlmRequest, type LocalLlmSuccessResponse } from "../types.js";

export async function routeSummary(request: LocalLlmRequest): Promise<LocalLlmSuccessResponse> {
  if (!request.content.trim()) {
    throw new LocalLlmError("INVALID_INPUT", "summary task requires content");
  }

  const config = getConfig();
  const model = getModelForTask("summary");
  const prompt = `${request.prompt}\n\n请总结以下内容，保留关键事实和结论：\n${request.content}`;
  const content = await invokeLlamaServer({
    model,
    prompt,
    endpoint: config.llamaServer.endpoint,
    timeoutSeconds: config.timeouts.default,
  });

  return { success: true, model, content };
}
