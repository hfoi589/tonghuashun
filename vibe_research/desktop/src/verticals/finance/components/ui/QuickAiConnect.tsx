import { WilsonOnly } from "@ths/market-session";

/** The application uses one administrator-managed AI runtime for all market accounts. */
export function QuickAiConnect({ onDismiss }: { onDismiss: () => void; storageStatus?: string }) {
  return <div className="rounded-xl border border-border bg-card p-5">
    <h2 className="text-lg font-semibold">AI 由管理员统一配置</h2>
    <p className="mt-2 text-sm text-muted-foreground">所有行情账号共用当前全站 AI 来源。</p>
    <WilsonOnly><a className="mt-4 inline-flex rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground" href="/?adminTab=vibe-ai#admin" onClick={onDismiss}>打开管理台 AI 设置</a></WilsonOnly>
  </div>;
}
