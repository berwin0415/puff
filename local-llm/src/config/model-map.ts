import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import YAML from "yaml";
import type { GatewayConfig, LocalLlmTask } from "../types.js";

const DEFAULT_ENDPOINT = "http://192.168.31.86:1234/v1";
const DEFAULT_MODEL = "qwen3.5-9b";

const DEFAULT_CONFIG: GatewayConfig = {
  endpoint: DEFAULT_ENDPOINT,
  model: DEFAULT_MODEL,
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
const PROJECT_ROOT = resolve(CURRENT_DIR, "../..");
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
    endpoint: fileConfig.endpoint ?? DEFAULT_CONFIG.endpoint,
    model: fileConfig.model ?? DEFAULT_CONFIG.model,
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
