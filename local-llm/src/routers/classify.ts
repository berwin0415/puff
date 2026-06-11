import { getConfig, getModelForTask } from "../config/model-map.js";
import { invokeLmStudio } from "../providers/lm-studio.js";
import { LocalLlmError, type LocalLlmRequest, type LocalLlmSuccessResponse } from "../types.js";

export async function routeClassify(request: LocalLlmRequest): Promise<LocalLlmSuccessResponse> {
  if (!request.content.trim()) {
    throw new LocalLlmError("INVALID_INPUT", "classify task requires content");
  }

  const config = getConfig();
  const model = getModelForTask("classify");
  const prompt = `${request.prompt}\n\n请对以下内容进行分类、标签生成或风险/意图识别：\n${request.content}`;
  const content = await invokeLmStudio({
    model,
    prompt,
    endpoint: config.endpoint,
    timeoutSeconds: config.timeouts.default,
    maxTokens: 4096,
  });

  return { success: true, model, content };
}
