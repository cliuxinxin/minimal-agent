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
export type WorkPlan = {
  goal: string; success_criteria: string[];
  steps: { title: string; status: "pending" | "in_progress" | "completed" }[];
  summary: string; evidence: string[]; uncertainties: string[];
  verification: { status: "not_checked" | "needs_work" | "passed"; summary: string };
  revision: number; updated_at: string;
};
export type DirectionInput = {
  id: string; run_id: string; message: string; status: "pending" | "applied";
  created_at: string; applied_at?: string;
};
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

export function sourceReference(label: string, href: string | undefined, sources: Source[]) {
  const url = safeSourceUrl(href);
  if (!url) return null;
  const index = sources.findIndex((source) => source.id === label && new URL(source.url).href === new URL(url).href);
  return index < 0 ? null : { number: index + 1, source: sources[index]! };
}

export const isSourceIdentifier = (value: string) => /^S[a-f0-9]{6,64}$/i.test(value);

export function readablePlanEvidence(value: string, sources: Source[]): string {
  const markdown = readableSourceMarkdown(value, sources);
  if (!/\bS[a-f0-9]{6,64}\b/i.test(markdown)) return markdown;
  const nodes = [...markdownParser.parse(markdown).children];
  const edits: { start: number; end: number; text: string }[] = [];
  while (nodes.length) {
    const node = nodes.pop();
    if (!node || ["link", "linkReference", "definition", "image", "imageReference"].includes(node.type)) continue;
    if (node.type === "text") {
      const start = node.position?.start.offset, end = node.position?.end.offset;
      if (start !== undefined && end !== undefined) {
        const original = markdown.slice(start, end);
        const readable = original.replace(/\bS[a-f0-9]{6,64}\b/gi, (id) => {
          const index = sources.findIndex((source) => source.id === id);
          return index < 0 ? "未匹配的来源" : `来源 ${index + 1}（${sources[index]!.title}）`;
        });
        if (readable !== original) edits.push({ start, end, text: readable });
      }
    }
    if ("children" in node) nodes.push(...node.children);
  }
  let result = markdown;
  for (const edit of edits.sort((a, b) => b.start - a.start)) result = result.slice(0, edit.start) + edit.text + result.slice(edit.end);
  return result;
}

export function readableSourceMarkdown(markdown: string, sources: Source[]): string {
  if (!markdown.includes("[S")) return markdown;
  const edits: { start: number; end: number; text: string }[] = [];
  const nodes = [...markdownParser.parse(markdown).children];
  while (nodes.length) {
    const node = nodes.pop();
    if (!node) continue;
    if (node.type === "link" && node.children.length === 1 && node.children[0]?.type === "text") {
      const label = node.children[0];
      const reference = sourceReference(label.value, node.url, sources);
      const start = label.position?.start.offset, end = label.position?.end.offset;
      if (start !== undefined && end !== undefined && (reference || isSourceIdentifier(label.value))) {
        edits.push({ start, end, text: reference ? String(reference.number) : "来源待核对" });
      }
    }
    if ("children" in node) nodes.push(...node.children);
  }
  // Apply AST text ranges backwards. Code, destination URLs and native history stay untouched.
  let result = markdown;
  for (const edit of edits.sort((a, b) => b.start - a.start)) {
    result = result.slice(0, edit.start) + edit.text + result.slice(edit.end);
  }
  return result;
}

const strings = (value: unknown, max: number, width: number): string[] => Array.isArray(value)
  ? value.slice(0, max).filter((entry): entry is string => typeof entry === "string" && Boolean(entry.trim())).map((entry) => entry.slice(0, width)) : [];

export function normalizeWorkPlan(value: unknown): WorkPlan | null {
  const item = object(value);
  const goal = text(item?.goal, 500).trim();
  if (!item || !goal) return null;
  const verification = object(item.verification);
  const steps: WorkPlan["steps"] = [];
  if (Array.isArray(item.steps)) for (const entry of item.steps.slice(0, 8)) {
    const step = object(entry), title = text(step?.title, 200).trim();
    if (title) steps.push({ title, status: step?.status === "completed" ? "completed" : step?.status === "in_progress" ? "in_progress" : "pending" });
  }
  return { goal, success_criteria: strings(item.success_criteria, 6, 300), steps,
    summary: text(item.summary, 1200), evidence: strings(item.evidence, 8, 400), uncertainties: strings(item.uncertainties, 6, 300),
    verification: { status: verification?.status === "passed" ? "passed" : verification?.status === "needs_work" ? "needs_work" : "not_checked",
      summary: text(verification?.summary, 1200) }, revision: count(item.revision) ?? 0, updated_at: text(item.updated_at, 80) };
}

export function normalizeSteering(value: unknown): DirectionInput[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>(), output: DirectionInput[] = [];
  for (const entry of value.slice(-100)) {
    const item = object(entry), id = text(item?.id, 64), runId = text(item?.run_id, 64);
    if (!item || !/^[A-Za-z0-9_-]{1,64}$/.test(id) || !runId || seen.has(id) || !["pending", "applied"].includes(String(item.status))) continue;
    const message = text(item.message, 8000);
    if (!message) continue;
    seen.add(id);
    output.push({ id, run_id: runId, message, status: item.status === "applied" ? "applied" : "pending",
      created_at: text(item.created_at, 80), ...(typeof item.applied_at === "string" ? { applied_at: item.applied_at.slice(0, 80) } : {}) });
  }
  return output;
}

export function directionStatus(input: DirectionInput, run?: { id?: string; status?: string } | null) {
  return input.status === "applied" ? "已应用到上下文"
    : run?.id === input.run_id && run.status === "running" ? "已收到，等待安全步骤边界应用" : "已收到，待下次继续时应用";
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
