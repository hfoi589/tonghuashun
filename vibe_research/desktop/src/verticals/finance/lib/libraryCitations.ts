import type { Root, RootContent, Link } from "mdast";

export const LIBRARY_CITATION = /\[资料:([0-9a-f]{32}) p\.(\d+|-)\]/g;
export type CitationDocument = { id: string; name: string; pages: number | null };

/** Display-only transform. Keep original reply and source identifiers unchanged in storage. */
export function libraryCitations(documents: CitationDocument[]) {
  const byId = new Map(documents.map(d => [d.id, d]));
  return () => (tree: Root) => {
    function visit(parent: Root | RootContent) {
      if (!("children" in parent) || ["link", "linkReference", "image", "imageReference"].includes(parent.type)) return;
      const children: RootContent[] = [];
      for (const child of parent.children) {
        if (child.type !== "text") { visit(child); children.push(child); continue; }
        let at = 0;
        for (const match of child.value.matchAll(LIBRARY_CITATION)) {
          const doc = byId.get(match[1]!);
          if (!doc) continue; // Loading/missing metadata must not manufacture a source.
          children.push({ type: "text", value: child.value.slice(at, match.index) });
          const page = Number(match[2]);
          const knownPage = Number.isSafeInteger(page) && page > 0 && doc.pages !== null && page <= doc.pages;
          children.push({ type: "link", url: `/my-reports?report=${doc.id}${knownPage ? `&page=${page}` : ""}`,
            title: `打开本地资料 · ${doc.name} · 引用编号 ${doc.id}`,
            children: [{ type: "text", value: `「${doc.name}」${knownPage ? ` · 第 ${page} 页` : " · 页码未提供或未核实"}` }] } as Link);
          at = match.index! + match[0].length;
        }
        children.push({ ...child, value: child.value.slice(at) });
      }
      parent.children = children as typeof parent.children;
    }
    visit(tree);
  };
}
