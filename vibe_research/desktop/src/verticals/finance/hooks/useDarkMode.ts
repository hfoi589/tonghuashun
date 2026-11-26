import { useEffect, useState } from "react";

import { storageGet, storageSet } from "@/lib/storage";

// 方案 A（暗色盘口）与方案 C（红头白底）共用主题同步。
// 机制：亮色（方案 C）时给 <html> 加 .light 与 data-ths-skin="light"；暗色（方案 A）为 .dark 与 data-ths-skin="dark"。
export function useDarkMode() {
  const [dark, setDark] = useState(() => {
    const saved = storageGet("ths_ui_skin") || storageGet("vr-theme");
    if (saved) return saved !== "light";
    return true; // 默认方案 A 暗色盘口
  });

  useEffect(() => {
    const skin = dark ? "dark" : "light";
    document.documentElement.dataset.thsSkin = skin;
    document.documentElement.classList.toggle("light", !dark);
    document.documentElement.classList.toggle("dark", dark);
    storageSet("vr-theme", skin);
    storageSet("ths_ui_skin", skin);
    window.dispatchEvent(new CustomEvent("ths-skin-change", { detail: skin }));
  }, [dark]);

  useEffect(() => {
    const onSkinChange = (event: Event) => {
      const next = (event as CustomEvent<string>).detail;
      if (next === "dark" || next === "light") {
        const nextDark = next === "dark";
        setDark((prev) => (prev === nextDark ? prev : nextDark));
      }
    };
    window.addEventListener("ths-skin-change", onSkinChange);
    return () => window.removeEventListener("ths-skin-change", onSkinChange);
  }, []);

  return { dark, toggle: () => setDark((d) => !d) };
}
