import { Link } from "react-router-dom";
import { ArrowUpRight } from "lucide-react";
import { FinanceHomeAgent } from "@/components/ui/FinanceAiDock";
import { Disclaimer } from "@/components/ui/Disclaimer";
import { HomeDashboard } from "@/components/HomeDashboard";
import { HOME_FEATURE_GROUPS } from "@/lib/homeFeatures";

const TERMINAL_QUICK_ACTIONS = [
  { to: "/dark-pool", title: "资金暗盘", tag: "L2 · ETF" },
  { to: "/daily-review", title: "每日复盘", tag: "收盘" },
  { to: "/research", title: "个股深挖", tag: "六阶段" },
  { to: "/debate", title: "多空辩论", tag: "A股" },
  { to: "/signals", title: "产业信号", tag: "追踪" },
  { to: "/backtest", title: "回测", tag: "验证" },
] as const;

/**
 * 首页：大盘指数 → AI COMMAND 命令条 → 专业三栏高密看板（自选行情 | 持仓 + ETF异动 | 快捷工具箱）。
 */
export function Home() {
  return (
    <div className="home-terminal-shell grid gap-2.5">
      <h1 className="sr-only">Vibe Research 研究工作台</h1>
      <HomeDashboard />
      <FinanceHomeAgent />
      <section id="home-features" className="home-toolbox-panel glass min-w-0 overflow-hidden" aria-labelledby="feature-heading">
        <div className="workspace-panel-head">
          <h2 id="feature-heading" className="flex items-center gap-1.5">
            <span className="home-terminal-kicker font-mono text-[10px] font-bold uppercase tracking-widest text-primary">WORKBENCH</span>
            <span>工作台快捷工具箱</span>
          </h2>
          <span className="text-[11px] text-muted-foreground">常用直达</span>
        </div>
        <div className="grid grid-cols-3 gap-1.5 border-b border-border p-2.5 sm:grid-cols-6 lg:grid-cols-2">
          {TERMINAL_QUICK_ACTIONS.map(({ to, title, tag }) => (
            <Link key={to} to={to} className="group flex min-w-0 items-center justify-between gap-1 rounded-md border border-border bg-muted/35 px-2.5 py-1.5 transition-colors hover:border-primary/50 hover:bg-primary/10">
              <div className="min-w-0">
                <span className="block truncate text-xs font-semibold group-hover:text-primary">{title}</span>
                <span className="block truncate font-mono text-[10px] text-muted-foreground">{tag}</span>
              </div>
              <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground group-hover:text-primary" />
            </Link>
          ))}
        </div>
        <div data-feature-grid className="home-feature-matrix grid gap-2 p-2.5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          {HOME_FEATURE_GROUPS.map((group, index) => (
            <div key={group.title} data-feature-category className="min-w-0 rounded-lg border border-border/70 bg-muted/20 p-2">
              <div className="mb-1.5 flex items-center gap-1.5 border-b border-border/60 pb-1">
                <span className="font-mono text-[10px] font-bold text-primary">0{index + 1}</span>
                <h3 className="text-xs font-semibold">{group.title}</h3>
              </div>
              <div className="grid gap-1">
                {group.features.map(({ to, title, detail }) => (
                  <Link key={to} to={to} title={detail} className="group flex min-h-7 items-center gap-1.5 rounded px-1.5 py-1 transition-colors hover:bg-primary/10">
                    <span className="min-w-0 flex-1 truncate text-[11.5px] font-medium group-hover:text-primary">{title}</span>
                    <ArrowUpRight className="h-3 w-3 shrink-0 text-muted-foreground group-hover:text-primary" />
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
      <div className="home-terminal-footer">
        <Disclaimer />
      </div>
    </div>
  );
}
