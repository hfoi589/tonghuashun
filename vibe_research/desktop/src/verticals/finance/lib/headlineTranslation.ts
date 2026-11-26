/**
 * Investment News 标题翻译的纯函数层。
 *
 * 上游 RSS 标题是不可信文本：模型只能把它们当待翻译数据，
 * 返回值也必须逐条按 id 对回原文。解析不出来就缺着，由界面回退英文原题，
 * 不猜、不按顺序硬套。
 */

export interface HeadlineTranslationInput {
  id: string;
  title: string;
}

export const TITLE_TRANSLATION_CACHE_KEY = "vr-radar-title-translations-v1";

const CJK = /[\u3400-\u9fff]/;
const MAX_CACHE_ENTRIES = 1_200;

export function hasChinese(text: string): boolean {
  return CJK.test(text);
}

export function headlineNeedsTranslation(
  item: { title: string; zh?: string },
  cache: ReadonlyMap<string, string>,
  force = false,
): boolean {
  return !hasChinese(item.title) && (force || (!item.zh && !cache.has(item.title)));
}

/** 手动重译落入缓存后要覆盖上游旧 zh；否则按钮显示成功、画面却完全不变。 */
export function displayedHeadlineTranslation(
  item: { title: string; zh?: string },
  cache: ReadonlyMap<string, string>,
): string {
  return cache.get(item.title) || item.zh || "";
}

/** 按条数 + 字符数双上限分批，避免标题偏长时把对话入口的 4K 上限撑破。 */
export function splitHeadlineBatches(
  items: HeadlineTranslationInput[],
  maxItems = 16,
  maxChars = 2_600,
): HeadlineTranslationInput[][] {
  const out: HeadlineTranslationInput[][] = [];
  let batch: HeadlineTranslationInput[] = [];
  let chars = 0;
  for (const item of items) {
    const cost = item.id.length + item.title.length + 24;
    if (batch.length && (batch.length >= maxItems || chars + cost > maxChars)) {
      out.push(batch);
      batch = [];
      chars = 0;
    }
    batch.push(item);
    chars += cost;
  }
  if (batch.length) out.push(batch);
  return out;
}

/**
 * 只收预期 id + 真正含中文的短标题。
 * 模型偶尔包 Markdown fence，允许外壳；内部形状不对则当本批失败。
 */
export function parseHeadlineTranslations(
  raw: string,
  expected: HeadlineTranslationInput[],
): Map<string, string> {
  const start = raw.indexOf("{");
  const end = raw.lastIndexOf("}");
  if (start < 0 || end <= start) throw new Error("模型没有返回可读的 JSON");
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw.slice(start, end + 1));
  } catch {
    throw new Error("模型返回的翻译 JSON 格式不对");
  }
  const rows = (parsed as { items?: unknown } | null)?.items;
  if (!Array.isArray(rows)) throw new Error("模型返回里缺少 items 数组");

  const allowed = new Set(expected.map((x) => x.id));
  const out = new Map<string, string>();
  for (const row of rows) {
    if (!row || typeof row !== "object" || Array.isArray(row)) continue;
    const r = row as Record<string, unknown>;
    const id = typeof r.id === "string" ? r.id : "";
    const zh = typeof r.zh === "string" ? r.zh.trim() : "";
    if (!allowed.has(id) || out.has(id) || !zh || zh.length > 300 || !hasChinese(zh)) continue;
    out.set(id, zh);
  }
  return out;
}

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** 缓存只是省重复翻译的优化；损坏 / 隐私模式下读不到就当空，不会影响原文展示。 */
export function loadHeadlineTranslationCache(storage?: StorageLike): Map<string, string> {
  const out = new Map<string, string>();
  if (!storage) return out;
  try {
    const rows = JSON.parse(storage.getItem(TITLE_TRANSLATION_CACHE_KEY) ?? "[]") as unknown;
    if (!Array.isArray(rows)) return out;
    for (const row of rows.slice(-MAX_CACHE_ENTRIES)) {
      if (!Array.isArray(row) || row.length !== 2) continue;
      const [title, zh] = row;
      if (typeof title === "string" && title && title.length <= 500 && typeof zh === "string" && zh && zh.length <= 300 && hasChinese(zh)) {
        out.set(title, zh);
      }
    }
  } catch { /* 派生缓存坏了就重建，RSS 原题仍照常显示 */ }
  return out;
}

export function saveHeadlineTranslationCache(cache: Map<string, string>, storage?: StorageLike): boolean {
  if (!storage) return false;
  try {
    storage.setItem(TITLE_TRANSLATION_CACHE_KEY, JSON.stringify([...cache.entries()].slice(-MAX_CACHE_ENTRIES)));
    return true;
  } catch {
    return false;
  }
}
