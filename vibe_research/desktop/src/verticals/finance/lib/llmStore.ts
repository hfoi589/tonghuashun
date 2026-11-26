/** Browser state contains only a server-managed marker; credentials stay in the host service. */
export const LLM_KEY = "vr-llm";

export interface LlmConfig {
  provider: string;
  baseURL: string;
  apiKey: string;
  model: string;
}

export type ExecutionMode = "agent" | "direct";

export interface AiRuntimeConfig {
  schemaVersion: 2;
  source: LlmConfig;
}

export type LlmStatus = "none" | "ok" | "broken" | "unavailable";

export interface AiRuntimeRead {
  status: LlmStatus;
  config: AiRuntimeConfig | null;
}

export interface LlmRead {
  status: LlmStatus;
  config: LlmConfig | null;
}

const SERVER_MANAGED: AiRuntimeConfig = {
  schemaVersion: 2,
  source: { provider: "server-managed", baseURL: "", apiKey: "", model: "server-managed" },
};

export function readAiRuntime(): AiRuntimeRead {
  return { status: "ok", config: SERVER_MANAGED };
}

export function readUserLlm(): LlmRead {
  return { status: "ok", config: SERVER_MANAGED.source };
}

export function loadUserLlm(): LlmConfig {
  return SERVER_MANAGED.source;
}

export function saveUserLlm(_cfg: LlmConfig): void {
  throw new Error("AI 配置由管理员在管理台统一维护");
}

export function clearLegacyUserLlm(): void {
  try { localStorage.removeItem(LLM_KEY); } catch { /* No browser credential is used by the shared runtime. */ }
}

export function clearUserLlm(): void {
  clearLegacyUserLlm();
  notifyRuntimeChanged();
}

export const AI_RUNTIME_CHANGED = "vibe-research:ai-runtime-changed";

function notifyRuntimeChanged(): void {
  if (typeof globalThis.dispatchEvent === "function" && typeof CustomEvent === "function") {
    globalThis.dispatchEvent(new CustomEvent(AI_RUNTIME_CHANGED));
  }
}
