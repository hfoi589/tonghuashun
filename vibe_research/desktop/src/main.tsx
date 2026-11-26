import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";

import "./index.css";
import { router } from "./verticals/finance/router";
import { hydrateNotes } from "./verticals/finance/lib/notes";
import { hydrateWatch } from "./verticals/finance/lib/watchlist";

/**
 * **组装根**:唯一一处把垂类接进外壳的地方。
 *
 * 🔴 界面整套来自开源版 Vibe-Research(2026-08-20 `ab4ffa0`),原样搬入
 *    `src/verticals/finance/` —— 版式、导航、页面结构一律以那边为准,不再自己发挥。
 *    换个垂类 = 换这一行 import。
 * 🔴 **只有数据与密钥这一层改写**:上游把用户的 API key 存在浏览器 localStorage、
 *    每个请求带着走;我们的密钥只在后端环境变量里。改写集中在
 *    `verticals/finance/lib/{api,llm,agents,notes,watchlist}.ts` 与「接入 AI」页,其余一律照搬。
 */

document.title = "Vibe Research";

const root = document.getElementById("root");
if (!root) throw new Error("找不到 #root");

/**
 * 🔴 **先把台账灌进缓存,再挂载 React**。
 *    研究记录 / 自选在页面里是**同步读**的(`useState(loadNotes)`),
 *    挂载后才灌好的话首屏永远是空的,而且不会重渲染 —— 表现是"我的记录不见了"。
 * 🔴 灌不进去时**不要退化成空列表挂上去** —— 那会让"连不上后端"长得和"你没有记录"
 *    一模一样,用户还可能在上面新建、覆盖。宁可整页说清楚为什么。
 */
function fatal(message: string, detail: string) {
  root!.innerHTML = "";
  const box = document.createElement("div");
  box.style.cssText =
    "max-width:34rem;margin:18vh auto;padding:1.5rem;font:14px/1.7 system-ui,-apple-system,sans-serif;" +
    "color:#e5e7eb;background:#18181b;border:1px solid #3f3f46;border-radius:12px";
  const h = document.createElement("h1");
  h.textContent = message;
  h.style.cssText = "margin:0 0 .75rem;font-size:1.05rem;font-weight:600";
  const p = document.createElement("p");
  p.textContent = detail;
  p.style.cssText = "margin:0 0 1rem;color:#a1a1aa;white-space:pre-wrap";
  const tip = document.createElement("p");
  tip.textContent = "macOS / Linux 请运行 scripts/start；Windows 请运行 scripts\\start.cmd。";
  tip.style.cssText = "margin:0;color:#a1a1aa";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "重新连接";
  retry.style.cssText =
    "margin-top:1rem;padding:.55rem .85rem;color:#fff;background:#e4572e;border:0;border-radius:8px;cursor:pointer";
  retry.addEventListener("click", () => window.location.reload());
  box.append(h, p, tip, retry);
  root!.append(box);
}

Promise.all([hydrateNotes(), hydrateWatch()])
  .then(() => {
    createRoot(root).render(
      <StrictMode>
        <RouterProvider router={router} />
      </StrictMode>,
    );
  })
  .catch((e: unknown) => {
    fatal("连不上后端,先不渲染界面", e instanceof Error ? e.message : String(e));
  });
