import { useEffect, useState } from "react";
import type { AiRuntimeRead } from "@/lib/llmStore";

const blank: AiRuntimeRead = { status: "none", config: null };

export function useAiRuntime() {
  const [runtime, setRuntime] = useState<AiRuntimeRead>(blank);
  useEffect(() => {
    let active = true;
    const refresh = () => {
      void fetch("/api/vibe/runtime", { credentials: "include", cache: "no-store" })
        .then(async (response) => {
          if (!response.ok) throw new Error(`runtime status ${response.status}`);
          return await response.json() as { configured?: boolean; provider?: string | null; model?: string | null };
        })
        .then((status) => {
          if (!active) return;
          const config = status.configured && status.provider ? {
            schemaVersion: 2 as const,
            source: { provider: status.provider, baseURL: "", apiKey: "", model: status.model ?? "" },
          } : null;
          setRuntime({ status: config ? "ok" : "none", config });
        })
        .catch(() => { if (active) setRuntime(blank); });
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => { active = false; window.removeEventListener("focus", refresh); };
  }, []);
  return runtime;
}
