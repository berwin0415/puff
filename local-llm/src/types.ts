export type LocalLlmTask = "vision" | "extract" | "summary" | "classify";

export type LocalLlmErrorCode =
  | "MODEL_UNAVAILABLE"
  | "MODEL_TIMEOUT"
  | "INVALID_TASK"
  | "INVALID_INPUT"
  | "OLLAMA_ERROR";

export interface LocalLlmRequest {
  task: LocalLlmTask;
  prompt: string;
  content: string;
  attachments: string[];
}

export interface LocalLlmSuccessResponse {
  success: true;
  model: string;
  content: string;
}

export interface LocalLlmFailureResponse {
  success: false;
  error: {
    code: LocalLlmErrorCode;
    message: string;
  };
}

export type LocalLlmResponse = LocalLlmSuccessResponse | LocalLlmFailureResponse;

export interface GatewayConfig {
  llamaServer: {
    endpoint: string;
    host: string;
    port: number;
    binary: string;
    libraryPath: string;
    modelPath: string;
    mmprojPath: string;
    model: string;
    mmproj: string;
    runtime: {
      ngl: number;
      context: number;
      temperature: number;
      maxTokens: number;
    };
  };
  models: Record<LocalLlmTask, string>;
  timeouts: {
    default: number;
  };
}

export interface LlamaServerInvokeOptions {
  model: string;
  prompt: string;
  imageUrls?: string[];
  endpoint: string;
  timeoutSeconds: number;
  maxTokens: number;
}

export class LocalLlmError extends Error {
  constructor(
    public readonly code: LocalLlmErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "LocalLlmError";
  }
}
