import { LocalLlmError, type LmStudioInvokeOptions } from "../types.js";

type LmStudioMessageContent =
  | string
  | Array<
      | { type: "text"; text: string }
      | { type: "image_url"; image_url: { url: string } }
    >;

interface LmStudioChatResponse {
  choices?: Array<{
    message?: {
      content?: string;
    };
  }>;
  error?: string | { message?: string };
}

function getErrorMessage(payload: LmStudioChatResponse, fallback: string): string {
  if (typeof payload.error === "string") {
    return payload.error;
  }
  return payload.error?.message || fallback;
}

function buildMessageContent(options: LmStudioInvokeOptions): LmStudioMessageContent {
  if (!options.imageUrls?.length) {
    return options.prompt;
  }

  return [
    { type: "text", text: options.prompt },
    ...options.imageUrls.map((url) => ({
      type: "image_url" as const,
      image_url: { url },
    })),
  ];
}

export async function invokeLmStudio(options: LmStudioInvokeOptions): Promise<string> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutSeconds * 1000);

  try {
    const baseUrl = options.endpoint.replace(/\/$/, "");
    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: options.model,
        messages: [
          {
            role: "user",
            content: buildMessageContent(options),
          },
        ],
        max_tokens: options.maxTokens,
        stream: false,
      }),
      signal: controller.signal,
    });

    const responseText = await response.text();
    let payload: LmStudioChatResponse;

    try {
      payload = responseText ? JSON.parse(responseText) : {};
    } catch {
      throw new LocalLlmError("OLLAMA_ERROR", "LM Studio returned invalid JSON");
    }

    if (!response.ok) {
      throw new LocalLlmError(
        "OLLAMA_ERROR",
        getErrorMessage(payload, `LM Studio request failed with status ${response.status}`),
      );
    }

    const content = payload.choices?.[0]?.message?.content;
    if (typeof content !== "string") {
      throw new LocalLlmError("OLLAMA_ERROR", "LM Studio response missing choices[0].message.content");
    }

    if (!content.trim()) {
      throw new LocalLlmError(
        "OLLAMA_ERROR",
        "LM Studio returned empty content",
      );
    }

    return content;
  } catch (error) {
    if (error instanceof LocalLlmError) {
      throw error;
    }

    if (error instanceof Error && error.name === "AbortError") {
      throw new LocalLlmError("MODEL_TIMEOUT", "LM Studio request timed out");
    }

    const message = error instanceof Error ? error.message : "Unable to reach LM Studio";
    throw new LocalLlmError("MODEL_UNAVAILABLE", message);
  } finally {
    clearTimeout(timeout);
  }
}
