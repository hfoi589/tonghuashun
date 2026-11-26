import type { Root, RootContent, Text, Link } from "mdast";

const ID = /^(?:ev|calc)-[a-zA-Z0-9-]+$/;
const EVIDENCE_ID = /^ev-[a-zA-Z0-9-]+$/;
const CALCULATION_ID = /^calc-[a-zA-Z0-9-]+$/;

export interface EvidenceCitationRow {
  id: string;
  endpoint: string;
  field: string;
  value: string | number | boolean | null;
  unit: string;
  currency: string;
  period: string;
  as_of?: string;
  source: string;
  note?: string;
  record_key?: string;
}

/** Assign E numbers in the same order as the current run's evidence list. */
export function evidenceCitationNumbers(rows: readonly { id: string }[]): Map<string, number> {
  const numbers = new Map<string, number>();
  for (const row of rows) if (row.id && !numbers.has(row.id)) numbers.set(row.id, numbers.size + 1);
  return numbers;
}

function markdownCell(value: unknown): string {
  return String(value ?? "").replace(/\r?\n/g, " ").replace(/\|/g, "\\|");
}

/** Serialize the safe evidence subset into the same G index consumed by report citation links. */
export function evidenceAppendixMarkdown(rows: readonly EvidenceCitationRow[]): string {
  if (!rows.length) return "";
  const numbers = evidenceCitationNumbers(rows);
  const lines = [
    "## G. 证据索引（本场辩论共用资料）",
    "",
    "| 序号 | id | 端点脚本 | 字段 | 值 | 单位 | 币种 | 期间 | as_of | 来源 | 说明 | record_key |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|",
  ];
  for (const row of rows) {
    const number = numbers.get(row.id);
    if (number === undefined) continue;
    lines.push(`| E${number} | ${markdownCell(row.id)} | ${markdownCell(row.endpoint)} | ${markdownCell(row.field)} | ${markdownCell(row.value)} | ${markdownCell(row.unit)} | ${markdownCell(row.currency)} | ${markdownCell(row.period)} | ${markdownCell(row.as_of)} | ${markdownCell(row.source)} | ${markdownCell(row.note)} | ${markdownCell(row.record_key)} |`);
  }
  return lines.join("\n");
}

/** Split a saved debate note so its evidence index can stay folded while citations remain linked. */
export function splitEvidenceAppendix(markdown: string): { report: string; appendix: string } {
  const lines = markdown.split(/\r?\n/);
  const index = lines.findIndex((line) => /^##\s+G\.\s+证据索引/.test(line));
  if (index < 0) return { report: markdown, appendix: "" };
  return { report: lines.slice(0, index).join("\n").trimEnd(), appendix: lines.slice(index).join("\n") };
}

function cells(line: string): string[] {
  return line.split("|").slice(1, -1).map((part) => part.trim());
}

/** Add display-only serials to old run appendices without changing their audited files. */
export function withEvidenceSerials(markdown: string): string {
  const lines = markdown.split(/\r?\n/);
  let section: "calcs" | "evidence" | null = null;
  let tableHeaderSeen = false;
  let needsSerial = false;
  let serial = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";
    if (/^##\s+C\.\s+计算 DAG/.test(line)) {
      section = "calcs";
      tableHeaderSeen = false;
      needsSerial = false;
      serial = 0;
      continue;
    }
    if (/^##\s+G\.\s+证据索引/.test(line)) {
      section = "evidence";
      tableHeaderSeen = false;
      needsSerial = false;
      serial = 0;
      continue;
    }
    if (section && /^##\s+/.test(line)) section = null;
    if (!section || !line.trimStart().startsWith("|")) continue;
    const values = cells(line);
    if (!tableHeaderSeen) {
      if (values[0] === "序号") { tableHeaderSeen = true; continue; }
      const legacyHeader = section === "calcs" ? values[0] === "calc id" : values[0] === "id";
      if (!legacyHeader) continue;
      tableHeaderSeen = true;
      needsSerial = true;
      lines[i] = `| 序号 | ${values.join(" | ")} |`;
      const separator = cells(lines[i + 1] ?? "");
      if (separator.length) lines[i + 1] = `|---|${separator.map((part) => `${part}|`).join("")}`;
      continue;
    }
    const id = needsSerial ? values[0] ?? "" : values[1] ?? "";
    const isSectionId = section === "calcs" ? CALCULATION_ID.test(id) : EVIDENCE_ID.test(id);
    if (!isSectionId) continue;
    serial++;
    const label = `${section === "calcs" ? "C" : "E"}${serial}`;
    lines[i] = needsSerial
      ? `| ${label} | ${values.join(" | ")} |`
      : `| ${[label, ...values.slice(1)].join(" | ")} |`;
  }
  return lines.join("\n");
}

function citationSection(markdown: string, section: "calcs" | "evidence"): string[] {
  const out: string[] = [];
  let inSection = false;
  let fence: { char: string; length: number } | null = null;
  for (const line of markdown.split(/\r?\n/)) {
    const marker = line.match(/^\s*(`{3,}|~{3,})/);
    if (marker?.[1]) {
      if (!fence) fence = { char: marker[1][0]!, length: marker[1].length };
      else if (marker[1][0] === fence.char && marker[1].length >= fence.length) fence = null;
      continue;
    }
    if (fence) continue;
    const header = section === "calcs" ? /^##\s+C\.\s+计算 DAG/ : /^##\s+G\.\s+证据索引/;
    if (header.test(line)) { inSection = true; continue; }
    if (inSection && /^##\s+/.test(line)) inSection = false;
    if (inSection) out.push(line);
  }
  return out;
}

/** Evidence IDs map to their visible index row number; legacy indexes use row order. */
export function appendixEvidenceNumbers(markdown: string): Map<string, number> {
  return appendixCitationNumbers(markdown, "evidence");
}

/** Calculation IDs map to their visible DAG row number; legacy DAGs use row order. */
export function appendixCalculationNumbers(markdown: string): Map<string, number> {
  return appendixCitationNumbers(markdown, "calcs");
}

function appendixCitationNumbers(markdown: string, section: "calcs" | "evidence"): Map<string, number> {
  const lines = citationSection(withEvidenceSerials(markdown), section);
  const numbers = new Map<string, number>();
  let legacySerial = 0;
  const validId = section === "calcs" ? CALCULATION_ID : EVIDENCE_ID;
  for (const line of lines) {
    if (!line.trimStart().startsWith("|")) continue;
    const values = cells(line);
    if (values[0] === "序号" || values[0] === "---") continue;
    const serial = values[0]?.match(/^(?:C|E)?(\d+)$/)?.[1];
    if (serial && validId.test(values[1] ?? "")) {
      numbers.set(values[1]!, Number(serial));
      continue;
    }
    // Older archived runs store the ID in the first column.
    if (validId.test(values[0] ?? "")) numbers.set(values[0]!, ++legacySerial);
  }
  return numbers;
}

/** IDs referenced by the report and calculation DAG, including both index layouts. */
export function appendixIds(markdown: string): Set<string> {
  const ids = new Set<string>();
  let section: "calcs" | "evidence" | null = null;
  let fence: { char: string; length: number } | null = null;
  for (const line of markdown.split(/\r?\n/)) {
    const marker = line.match(/^\s*(`{3,}|~{3,})/);
    if (marker?.[1]) {
      if (!fence) fence = { char: marker[1][0]!, length: marker[1].length };
      else if (marker[1][0] === fence.char && marker[1].length >= fence.length) fence = null;
      continue;
    }
    if (fence) continue;
    if (/^##\s+C\.\s+计算 DAG/.test(line)) { section = "calcs"; continue; }
    if (/^##\s+G\.\s+证据索引/.test(line)) { section = "evidence"; continue; }
    if (section && /^##\s+/.test(line)) section = null;
    const row = cells(line);
    const first = row[0] ?? "";
    if (ID.test(first)) ids.add(first);
    if (section === "calcs" && CALCULATION_ID.test(row[1] ?? "")) ids.add(row[1]!);
    if (section === "evidence" && EVIDENCE_ID.test(row[1] ?? "")) ids.add(row[1]!);
  }
  return ids;
}

/** Transform text nodes only, never HTML, code, existing links or images. */
export function citationPlugin(ids: Set<string>, prefix: string, appendix = false, evidenceNumbers?: ReadonlyMap<string, number>, calculationNumbers?: ReadonlyMap<string, number>) {
  return () => (tree: Root) => {
    const anchored = new Set<string>();
    function visit(parent: Root | RootContent) {
      if (!("children" in parent) || ["link", "linkReference", "image", "imageReference"].includes(parent.type)) return;
      if (appendix && parent.type === "tableRow") {
        const firstCell = parent.children[0]?.children;
        const secondCell = parent.children[1]?.children;
        const firstNode = firstCell?.length === 1 ? firstCell[0] : null;
        const secondNode = secondCell?.length === 1 ? secondCell[0] : null;
        const first = firstNode?.type === "text" ? firstNode.value.trim() : "";
        const second = secondNode?.type === "text" ? secondNode.value.trim() : "";
        const id = ID.test(first) ? first : /^(?:C|E)?\d+$/.test(first) && ID.test(second) ? second : "";
        if (id && ids.has(id) && !anchored.has(id)) {
          parent.data = { ...parent.data, hProperties: { id: `${prefix}${id}`, tabIndex: -1 } };
          anchored.add(id);
        }
      }
      const children: RootContent[] = [];
      for (const child of parent.children) {
        if (child.type !== "text") { visit(child); children.push(child); continue; }
        const matches = [...child.value.matchAll(/(?<![a-zA-Z0-9_-])(?:ev|calc)-[a-zA-Z0-9-]+(?![a-zA-Z0-9_-])/g)];
        let at = 0;
        for (const match of matches) {
          const id = match[0];
          const isEvidence = EVIDENCE_ID.test(id);
          const numbers = isEvidence ? evidenceNumbers : calculationNumbers;
          if (!ids.has(id) && !numbers) continue;
          const start = match.index!;
          children.push({ type: "text", value: child.value.slice(at, start) } as Text);
          const number = numbers?.get(id);
          const visible = appendix || !numbers ? id : isEvidence ? `E${number ?? "?"}` : `C${number ?? "?"}`;
          const url = numbers && number === undefined ? "#" : `#${prefix}${id}`;
          const title = isEvidence
            ? number === undefined ? "证据索引序号缺失" : `跳转到证据索引第 ${number} 条`
            : number === undefined ? "计算 DAG 序号缺失" : `跳转到计算 DAG 第 ${number} 条`;
          children.push({ type: "link", url, title, children: [{ type: "text", value: visible }] } as Link);
          at = start + id.length;
        }
        children.push({ ...child, value: child.value.slice(at) });
      }
      parent.children = children as typeof parent.children;
    }
    visit(tree);
  };
}
