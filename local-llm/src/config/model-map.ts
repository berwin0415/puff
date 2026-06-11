import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import YAML from "yaml";
import type { GatewayConfig, LocalLlmTask } from "../types.js";

const DEFAULT_MODEL = "qwen3.5-9b-q4_k_m.gguf";

const DEFAULT_CONFIG: GatewayConfig = {
  llamaServer: {
    endpoint: "http://127.0.0.1:8080",
    host: "127.0.0.1",
    port: 8080,
    binary: "/usr/local/lib/ollama/llama-server",
    libraryPath: "/usr/local/lib/ollama",
    modelPath: "~/models/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf",
    mmprojPath: "~/models/Qwen3.5-9B-GGUF/mmproj-F16.gguf",
    model: DEFAULT_MODEL,
    mmproj: "mmproj-f16.gguf",
    runtime: {
      ngl: 99,
      context: 4096,
      temperature: 0.7,
      maxTokens: 4096,
    },
  },
  models: {
    vision: DEFAULT_MODEL,
    extract: DEFAULT_MODEL,
    summary: DEFAULT_MODEL,
    classify: DEFAULT_MODEL,
  },
  timeouts: {
    default: 120,
  },
};

const CURRENT_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(CURRENT_DIR, "../../..");
const CONFIG_PATH = resolve(PROJECT_ROOT, "config.yaml");

let cachedConfig: GatewayConfig | undefined;

function readConfigFile(): Partial<GatewayConfig> {
  if (!existsSync(CONFIG_PATH)) {
    return {};
  }

  const rawConfig = readFileSync(CONFIG_PATH, "utf8");
  return YAML.parse(rawConfig) ?? {};
}

function mergeConfig(fileConfig: Partial<GatewayConfig>): GatewayConfig {
  return {
    llamaServer: {
      ...DEFAULT_CONFIG.llamaServer,
      ...fileConfig.llamaServer,
      runtime: {
        ...DEFAULT_CONFIG.llamaServer.runtime,
        ...fileConfig.llamaServer?.runtime,
      },
    },
    models: {
      ...DEFAULT_CONFIG.models,
      ...fileConfig.models,
    },
    timeouts: {
      ...DEFAULT_CONFIG.timeouts,
      ...fileConfig.timeouts,
    },
  };
}

export function getConfig(): GatewayConfig {
  cachedConfig ??= mergeConfig(readConfigFile());
  return cachedConfig;
}

export function getModelForTask(task: LocalLlmTask): string {
  return getConfig().models[task];
}
