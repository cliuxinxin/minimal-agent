/** Small, explicit boundaries for metadata displayed outside the Markdown answer. */
import { unified } from "unified";
import remarkParse from "remark-parse";

const markdownParser = unified().use(remarkParse);
export type Artifact = {
  id: string;
  name: string;
  size: number;
  change: "created" | "modified";
  created_at: string;
  run_id: string;
  preview_url: string;
  download_url: string;
};
export type Source = { id: string; title: string; url: string; date: string | null; read: boolean };
export type RunMetrics = {
  model_calls: number; tool_calls: number; known_usage_calls: number; elapsed_seconds: number;
  input_tokens: number | null; output_tokens: number | null; total_tokens: number | null;
  compactions: number;
};
const object = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
const count = (value: unknown): number | null =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
const text = (value: unknown, limit: number) => typeof value === "string" ? value.slice(0, limit) : "";

export function safeSourceUrl(value: unknown): string | null {
  if (typeof value !== "string" || !/^https?:\/\//i.test(value) || value.length > 8192 || /[\s\\\u0000-\u001f\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && url.hostname && !url.username && !url.password ? value : null;
  } catch { return null; }
}

export function normalizeArtifacts(value: unknown): Artifact[] {
  if (!Array.isArray(value)) return [];
  const output: Artifact[] = [];
  const seen = new Set<string>();
  for (const entry of value.slice(0, 200)) {
    const item = object(entry);
    if (!item || typeof item.id !== "string" || !/^[a-f0-9]{32}$/.test(item.id) || seen.has(item.id)) continue;
    const name = text(item.name, 512);
    if (!name || name.startsWith("/") || name.includes("\\") || /[\u0000-\u001f\u007f]/.test(name) || name.split("/").includes("..")) continue;
    const preview = `/api/artifacts/${item.id}`, download = `${preview}/download`;
    // Never follow URLs supplied by a model, even when an ID happens to look valid.
    if ((item.preview_url !== undefined && item.preview_url !== preview) ||
      (item.download_url !== undefined && item.download_url !== download)) continue;
    seen.add(item.id);
    output.push({ id: item.id, name, size: count(item.size) ?? 0,
      change: item.change === "modified" ? "modified" : "created",
      created_at: text(item.created_at, 64), run_id: text(item.run_id, 64),
      preview_url: preview, download_url: download });
  }
  return output;
}

export function normalizeSources(value: unknown): Source[] {
  if (!Array.isArray(value)) return [];
  const sources = new Map<string, Source>();
  for (const entry of value.slice(0, 100)) {
    const item = object(entry);
    if (!item || typeof item.id !== "string" || !/^S[A-Za-z0-9_-]{1,64}$/.test(item.id)) continue;
    const url = safeSourceUrl(item.url);
    if (!url) continue;
    const previous = sources.get(item.id);
    if (previous && previous.url !== url) continue;
    sources.set(item.id, { id: item.id, title: text(item.title, 300) || new URL(url).hostname,
      url, date: text(item.date, 80) || null, read: item.read === true || previous?.read === true });
  }
  return [...sources.values()];
}

export function unmatchedSourceReferences(markdown: string, sources: Source[]): string[] {
  if (!markdown.includes("[S")) return [];
  const known = new Map(sources.map((source) => [source.id, new URL(source.url).href]));
  const unmatched = new Set<string>();
  // Reuse the Markdown parser so code blocks, inline code and escaped examples
  // cannot be mistaken for citations. This checks provenance, not truthfulness.
  const nodes = [...markdownParser.parse(markdown).children];
  while (nodes.length) {
    const node = nodes.pop();
    if (!node) continue;
    if (node.type === "link" && node.children.length === 1 && node.children[0]?.type === "text") {
      const id = node.children[0].value;
      if (/^S[A-Za-z0-9_-]{1,64}$/.test(id)) {
        const url = safeSourceUrl(node.url);
        if (!url || known.get(id) !== new URL(url).href) unmatched.add(id);
      }
    }
    if ("children" in node) nodes.push(...node.children);
  }
  // Keep the notice deterministic in the order citations first appear.
  return [...unmatched].sort((a, b) => markdown.indexOf(`[${a}]`) - markdown.indexOf(`[${b}]`));
}

export function normalizeMetrics(value: unknown): RunMetrics | null {
  const item = object(value);
  if (!item) return null;
  return {
    model_calls: count(item.model_calls) ?? 0, tool_calls: count(item.tool_calls) ?? 0,
    known_usage_calls: count(item.known_usage_calls) ?? 0, compactions: count(item.compactions) ?? 0,
    elapsed_seconds: typeof item.elapsed_seconds === "number" && Number.isFinite(item.elapsed_seconds) && item.elapsed_seconds >= 0
      ? item.elapsed_seconds : 0,
    input_tokens: count(item.input_tokens), output_tokens: count(item.output_tokens), total_tokens: count(item.total_tokens),
  };
}

export function formatMetrics(value: unknown): string {
  const metrics = normalizeMetrics(value);
  if (!metrics) return "";
  const elapsed = metrics.elapsed_seconds < 60 ? `${metrics.elapsed_seconds.toFixed(1)} 秒`
    : `${Math.floor(metrics.elapsed_seconds / 60)} 分 ${Math.floor(metrics.elapsed_seconds % 60)} 秒`;
  const known = metrics.total_tokens !== null || metrics.input_tokens !== null || metrics.output_tokens !== null;
  const usage = !known ? "用量未知" : `${metrics.known_usage_calls < metrics.model_calls ? "部分用量 " : ""}${
    metrics.total_tokens !== null ? `${metrics.total_tokens.toLocaleString("zh-CN")} token`
      : `输入 ${metrics.input_tokens ?? "未知"} / 输出 ${metrics.output_tokens ?? "未知"} token`}`;
  return `模型 ${metrics.model_calls} 次 · 工具 ${metrics.tool_calls} 次 · ${elapsed} · ${usage}` +
    (metrics.compactions ? ` · 已整理上下文 ${metrics.compactions} 次` : "");
}

export function sizeLabel(size: number): string {
  return size < 1024 ? `${size} B` : size < 1024 * 1024
    ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
}
