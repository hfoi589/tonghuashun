/**
 * 把 Core 的 AI 入口接到这个垂类上 —— **只提供"行业知道、Core 不知道"的那部分**：
 * 怎么连后端、免责声明怎么说、回答下面挂什么按钮、没配模型时往哪儿引导。
 *
 * 🔴 Core 那边一个行业词都不许有（前端边界棘轮会红），所以文案在这儿而不是那儿。
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Settings, Sparkles, Trash2 } from "lucide-react";

import { AiConsole } from "../../../../core/ai/AiConsole";
import { AiDock } from "../../../../core/ai/AiDock";
import { AiComposer, AiMessages } from "../../../../core/ai/AiMessages";
import { useAiChat } from "../../../../core/ai/useAiChat";
import { backend } from "@/lib/backend";
import { useAiRuntime } from "@/hooks/useAiRuntime";
import { SaveNoteButton } from "@/components/ui/SaveNoteButton";
import { QuickAiConnect } from "@/components/ui/QuickAiConnect";
import { ReportAnswer } from "../ReportAnswer";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";
import { maskPrivateText } from "@/lib/privacy";
import { sourcesMarkdown } from "@/lib/chatSources";
import { WilsonOnly } from "@ths/market-session";

const renderReply = (reply: string) => <ReportAnswer content={reply} />;

/** 隐私模式开着时，对话里自己的金额和股数显示成 ••••（发给 AI 的仍是原文） */
function usePrivateDisplay() {
  const { on } = usePrivacyMode();
  return on ? maskPrivateText : undefined;
}

/** 发一轮对话 —— 两个入口共用同一条通道 */
async function sendTurn({ message, session, signal }: { message: string; session: string; signal: AbortSignal }) {
  const r = await backend.chat(message, session, signal);
  const confirmations: string[] = [];
  for (const task of r.pending_research ?? []) {
    if (signal.aborted) break;
    if (!window.confirm(`启动 ${task.company_name || task.symbol}（${task.symbol}）的${task.endpoints === "full" ? "完整" : "核心"}研究？\n这会使用当前所选 AI 的额度，在后台运行。`)) {
      confirmations.push("研究尚未启动（已取消确认）。");
      continue;
    }
    try {
      const started = await backend.confirmChatResearch(task.id, signal);
      confirmations.push(`研究已启动，任务编号：${started.run_id}。可直接在这里询问进度，也可在研究页查看。`);
    } catch {
      confirmations.push("未能确认研究启动结果，请先在研究页核对任务列表，避免重复启动。");
    }
  }
  // 回答下面列「本轮来源」:查了哪些数据(哪个来源、什么时候取的)、读了哪些网页和资料,一条没查也要说
  const activity = sourcesMarkdown(r)
    + (confirmations.length ? `\n\n${confirmations.join("\n\n")}` : "");
  // 触发产出红线被删掉的行要**说出来**：不说的话，用户看到的是一段被悄悄剪过的回答
  return r.redacted
    ? `${r.reply}\n\n⚠️ 有 ${r.redacted} 行触发产出红线被移除（不给操作建议）。${activity}`
    : r.reply + activity;
}

const setupLink = () => (
  <WilsonOnly>
    <Link
      to="/settings"
      className="flex items-center justify-center gap-2 rounded-lg bg-primary/15 px-3 py-2 text-sm font-medium text-primary hover:bg-primary/25"
    >
      <Settings className="h-4 w-4" /> 管理 AI 设置
    </Link>
  </WilsonOnly>
);

const replyActions = (reply: string, question: string) => (
  <div className="mt-1.5">
    <SaveNoteButton kind="问 Agent" title={`问 Agent · ${question.slice(0, 24) || "对话"}`} content={reply} />
  </div>
);

const HOME_SUGGESTIONS = [
  "今天市场有哪些值得关注的变化？请查最新数据并注明时间。",
  "帮我研究一家公司的基本面、估值和主要风险。",
  "AI 算力产业链最近有哪些新变化？请附来源。",
  "分析一份研报时，哪些结论最需要核对证据？",
];

/** 首页里的命令式对话区：常驻单行 AI COMMAND 命令条，发送或展开建议时再展开回答区，不遮挡自选与看板。 */
export function FinanceHomeAgent() {
  const runtime = useAiRuntime();
  const configured = runtime.status === "ok";
  const chat = useAiChat("home-agent", sendTurn);
  const displayText = usePrivateDisplay();
  const [draft, setDraft] = useState("");
  const [promptsOpen, setPromptsOpen] = useState(false);
  const [setupOpen, setSetupOpen] = useState(false);
  useEffect(() => {
    if (configured) setSetupOpen(false);
  }, [configured]);
  const expanded = chat.msgs.length > 0 || chat.loading || Boolean(chat.err) || Boolean(chat.info);

  return (
    <section
      id="home-agent"
      data-home-agent
      className="home-terminal-ai glass overflow-hidden"
    >
      <div className="home-terminal-ai-bar flex flex-wrap items-center justify-between gap-2 px-3 py-2 sm:flex-nowrap">
        <div className="home-ai-brand flex shrink-0 items-center gap-1.5">
          <span className="home-ai-badge inline-flex items-center gap-1 rounded px-2 py-1 font-mono text-[10px] font-bold uppercase tracking-wider">
            <Sparkles className="h-3 w-3" />
            AI COMMAND
          </span>
        </div>
        <div className="order-3 min-w-0 flex-1 basis-full sm:order-2 sm:basis-auto">
          <AiComposer
            placeholder="说说要查什么、研究什么…（Shift+Enter 换行）"
            disabled={chat.loading || !configured}
            onStop={chat.loading ? chat.abort : undefined}
            onSend={(text) => {
              setPromptsOpen(false);
              void chat.submit(text);
            }}
            value={draft}
            onValueChange={setDraft}
            highlighted
          />
        </div>
        <div className="home-ai-actions order-2 flex shrink-0 items-center gap-1.5 sm:order-3">
          {!expanded && (
            <button
              type="button"
              onClick={() => setPromptsOpen((v) => !v)}
              aria-expanded={promptsOpen}
              className="home-ai-chip-btn rounded border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:border-primary/45 hover:text-primary"
            >
              {promptsOpen ? "收起指令" : "常用指令"}
            </button>
          )}
          {!configured && (
            <button
              type="button"
              onClick={() => setSetupOpen((v) => !v)}
              className="home-ai-chip-btn rounded border border-primary/40 bg-primary/10 px-2 py-1 text-[11px] font-medium text-primary hover:bg-primary/20"
            >
              请接入AI
            </button>
          )}
          {chat.msgs.length > 0 && (
            <button
              type="button"
              onClick={chat.clear}
              title="新对话"
              aria-label="新对话"
              className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground"
            >
              <Trash2 className="h-3 w-3" /> 清空
            </button>
          )}
        </div>
      </div>

      {!expanded && promptsOpen && (
        <div className="border-t border-border/70 px-3 py-2">
          <div className="flex flex-wrap gap-1.5">
            {HOME_SUGGESTIONS.map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => {
                  setDraft(item);
                  setPromptsOpen(false);
                  document.querySelector<HTMLTextAreaElement>("#home-agent textarea")?.focus();
                }}
                className="rounded-md border border-border bg-muted/35 px-2.5 py-1 text-left text-[11px] text-muted-foreground transition-colors hover:border-primary/50 hover:text-foreground"
              >
                {item}
              </button>
            ))}
          </div>
        </div>
      )}

      {expanded && (
        <AiMessages
          msgs={chat.msgs}
          loading={chat.loading}
          err={chat.err}
          info={chat.info}
          renderReply={renderReply}
          onPick={(text) => { setDraft(text); document.querySelector<HTMLTextAreaElement>("#home-agent textarea")?.focus(); }}
          renderReplyActions={replyActions}
          displayText={displayText}
          className="max-h-[360px] border-t border-border/70 px-4 py-3"
        />
      )}
      {!configured && setupOpen && (
        <div className="border-t border-border/70 p-3">
          <QuickAiConnect storageStatus={runtime.status} onDismiss={() => setSetupOpen(false)} />
        </div>
      )}
    </section>
  );
}

/** 底部控制台：一条长期对话，跟着你翻页一起走 */
export function FinanceAiConsole({ open, onClose }: { open: boolean; onClose: () => void }) {
  const runtime = useAiRuntime();
  const displayText = usePrivateDisplay();
  return (
    <AiConsole
      open={open}
      onClose={onClose}
      configured={runtime.status === "ok"}
      copy={{
        title: "研究助手",
        runtime: "用你接入的 AI",
        placeholder: "问点什么…（Shift+Enter 换行）",
        notice: "直接交代任务：AI 可联网搜索、读取网页、取数、计算并查询研究记录。回答附实际工具记录，不构成投资建议。",
        suggestions: ["帮我理一下最近在关注什么", "我该补哪些功课", "解释一下这个产品能干什么"],
      }}
      send={sendTurn}
      renderReplyActions={replyActions}
      renderReply={renderReply}
      renderSetup={setupLink}
      displayText={displayText}
    />
  );
}

export function FinanceAiDock() {
  const runtime = useAiRuntime();
  const displayText = usePrivateDisplay();
  return (
    <AiDock
      configured={runtime.status === "ok"}
      copy={{
        trigger: "问 Agent",
        panel: "研究助手",
        runtime: "用你接入的 AI",
        placeholder: "就这一页的内容问点什么…",
        notice: "AI 可结合当前页面联网搜索、读取网页、获取数据和运行分析工具；会显示实际调用记录。不构成投资建议。",
      }}
      send={sendTurn}
      renderReplyActions={replyActions}
      renderReply={renderReply}
      renderSetup={setupLink}
      displayText={displayText}
    />
  );
}
