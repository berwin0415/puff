import { extname } from "node:path";
import { readFile } from "node:fs/promises";
import { getConfig, getModelForTask } from "../config/model-map.js";
import { invokeLlamaServer } from "../providers/llama-server.js";
import { LocalLlmError, type LocalLlmRequest, type LocalLlmSuccessResponse } from "../types.js";

const MIME_BY_EXTENSION: Record<string, string> = {
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".bmp": "image/bmp",
};

function getImageMimeType(path: string): string {
  return MIME_BY_EXTENSION[extname(path).toLowerCase()] ?? "image/png";
}

async function readImageAsDataUrl(path: string): Promise<string> {
  try {
    const image = await readFile(path);
    return `data:${getImageMimeType(path)};base64,${image.toString("base64")}`;
  } catch {
    throw new LocalLlmError("INVALID_INPUT", `Unable to read attachment: ${path}`);
  }
}

export async function routeVision(request: LocalLlmRequest): Promise<LocalLlmSuccessResponse> {
  if (request.attachments.length === 0) {
    throw new LocalLlmError("INVALID_INPUT", "vision task requires at least one attachment");
  }

  const config = getConfig();
  const model = getModelForTask("vision");
  const imageUrls = await Promise.all(request.attachments.map(readImageAsDataUrl));
  const prompt = request.content.trim()
    ? `${request.prompt}\n\n补充上下文：\n${request.content}`
    : request.prompt;
  const content = await invokeLlamaServer({
    model,
    prompt,
    imageUrls,
    endpoint: config.llamaServer.endpoint,
    timeoutSeconds: config.timeouts.default,
  });

  return { success: true, model, content };
}
