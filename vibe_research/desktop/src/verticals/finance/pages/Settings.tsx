import { PageHeader } from "@/components/ui/PageHeader";

/** The shared AI configuration moved to the authenticated THS administrator console. */
export function Settings() {
  return <div>
    <PageHeader title="AI 设置" subtitle="模型来源由管理员统一维护，并对所有市场账号生效。" />
    <section className="glass p-4">
      <p className="text-sm text-muted-foreground">AI 配置入口已收拢到管理员管理台。</p>
    </section>
  </div>;
}
