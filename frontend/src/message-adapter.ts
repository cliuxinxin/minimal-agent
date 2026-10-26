import type { MessageStatus, ThreadAssistantMessagePart, ThreadMessageLike, ToolCallMessagePartStatus } from "@assistant-ui/react";
import type { RunDiagnostic } from "./run-diagnostic";
import { normalizeArtifacts, normalizeSources, normalizeMetrics, formatMetrics, unmatchedSourceReferences,
  readableSourceMarkdown, normalizeWorkPlan, normalizeSteering } from "./workbench-data";

type Segment = {
  id: string;
  type: "text" | "reasoning" | "tool";
  step?: number;
  text?: string;
  truncated?: boolean;
  name?: string;
  call_id?: string;
  status?: string;
  arguments?: string;
  arguments_truncated?: boolean;
  result?: unknown;
  error?: unknown;
};
type Message = {
  role: "user" | "assistant";
  run_id?: string;
  content: string;
  status?: string;
  segments?: Segment[];
  segments_omitted?: number;
  diagnostic?: RunDiagnostic;
  artifacts?: unknown;
  sources?: unknown;
  metrics?: unknown;
  artifact_warning?: string;
  work_plan?: unknown;
  steering?: unknown;
};
export type ConversationInput = {
  sessionId: string | null;
  messages: Message[];
  run?: { id: string; status: string; step?: number; error?: string | null; diagnostic?: RunDiagnostic; metrics?: unknown } | null;
  busy?: boolean;
};
export type ToolArtifact = {
  phase?: string;
  segmentId?: string;
  callId?: string;
  argumentsTruncated?: boolean;
  displayStatus?: ToolCallMessagePartStatus;
};
const running = (status?: string) => status === "running" || status === "stopping";

export function convertMessage(message: Message, index: number, input: ConversationInput): ThreadMessageLike {
  const id = message.run_id || `${input.sessionId || "draft"}-${message.role}-${index}`;
  if (message.role === "user") return { id, role: "user", content: message.content };
  const isElsewhere = message.status === "running_elsewhere" ||
    (message.run_id === input.run?.id && input.run?.status === "running_elsewhere");
  const isRunning = !isElsewhere && running(message.status);
  const report = message.diagnostic ?? (message.run_id === input.run?.id ? input.run?.diagnostic : undefined);
  const diagnostic = report?.run_id === message.run_id ? report : undefined;
  const metrics = normalizeMetrics(message.metrics ?? (message.run_id === input.run?.id ? input.run?.metrics : undefined));
  const sources = normalizeSources(message.sources);
  const status: MessageStatus = isElsewhere ? { type: "incomplete", reason: "cancelled" }
    : isRunning ? { type: "running" }
    : message.status === "cancelled" ? { type: "incomplete", reason: "cancelled" }
      : ["error", "interrupted", "step_limit", "stalled", "running_elsewhere"].includes(message.status || "") ? { type: "incomplete", reason: "error",
        error: (message.run_id === input.run?.id ? input.run?.error : undefined) || diagnostic?.error?.message ||
          (message.status === "interrupted" ? "后台已重启，任务停在上次保存的位置。" : message.status === "running_elsewhere"
            ? "任务仍在其他启动窗口运行；这里显示上次保存的进度。" : "本次任务未完成，可查看本机运行记录。") }
        : { type: "complete", reason: "stop" };
  let segments = message.segments || [];
  let fullAnswerFallback: string | undefined;
  if (!isRunning && status.type === "complete" && message.content) {
    const textParts = segments.filter((part) => part.type === "text");
    const finalStep = textParts.at(-1)?.step;
    const finalParts = textParts.filter((part) => part.step === finalStep);
    const truncated = finalParts.filter((part) => part.truncated);
    if (truncated.length) {
      // Stream snapshots are bounded, but the terminal answer is canonical and complete.
      // Restore a truncated final block when its exact surrounding text proves the split.
      const index = finalParts.findIndex((part) => part.id === truncated[0].id);
      const before = finalParts.slice(0, index).map((part) => part.text || "").join("");
      const after = finalParts.slice(index + 1).map((part) => part.text || "").join("");
      if (truncated.length === 1 && message.content.startsWith(before) && message.content.endsWith(after) &&
        message.content.length >= before.length + after.length) {
        const restored = message.content.slice(before.length, after.length ? -after.length : undefined);
        segments = segments.map((part) => part.id === truncated[0].id ? { ...part, text: restored, truncated: false } : part);
      } else {
        // Multiple truncated blocks cannot be split reliably from joined native text.
        fullAnswerFallback = message.content;
      }
    }
  }
  const content: ThreadAssistantMessagePart[] = [];
  for (const [position, segment] of segments.entries()) {
    if (segment.type === "tool") {
      let args = {};
      try { args = JSON.parse(segment.arguments || "{}"); } catch { /* Partial args remain in argsText. */ }
      if (!args || typeof args !== "object" || Array.isArray(args)) args = {};
      let phase = segment.status;
      if (!isRunning && !isElsewhere && phase === "preparing") phase = "not_executed";
      else if (!isRunning && !isElsewhere && phase === "running") phase = "interrupted";
      // Older snapshots discarded the original execution phase on a parent error.
      // With no tool result or tool error, report the unknown outcome as interrupted.
      else if (["error", "cancelled"].includes(phase || "") &&
        !Object.hasOwn(segment, "result") && segment.error === undefined) phase = "interrupted";
      const cancelled = message.status === "cancelled" || segment.status === "cancelled";
      const displayStatus: ToolCallMessagePartStatus = phase === "completed" ? { type: "complete" }
        : ["preparing", "running"].includes(phase || "") ? { type: "running" }
          : { type: "incomplete", reason: cancelled ? "cancelled" : "error",
            ...(segment.error === undefined ? {} : { error: segment.error }) };
      content.push({
        type: "tool-call",
        // The provider call ID can arrive late; the host's segment ID is stable from the first delta.
        toolCallId: segment.id,
        toolName: segment.name || "准备调用工具",
        args,
        argsText: segment.arguments || "",
        // Absence is meaningful: only a real tool_result can supply a result, including null.
        ...(Object.hasOwn(segment, "result") ? { result: segment.result } : {}),
        isError: ["error", "not_executed", "interrupted"].includes(phase || ""),
        // assistant-ui infers a completed tool whenever a result exists. Keep the
        // host's execution outcome explicit for the standard ToolFallback renderer.
        artifact: { phase, displayStatus, segmentId: segment.id, callId: segment.call_id,
          argumentsTruncated: Boolean(segment.arguments_truncated) } satisfies ToolArtifact,
      });
      continue;
    }
    if (!segment.text) continue;
    const partRunning = isRunning && position === segments.length - 1;
    content.push({
      type: segment.type,
      id: segment.id,
      text: segment.text,
      status: partRunning ? { type: "running" }
        : !isRunning && status.type === "incomplete" && position === segments.length - 1
          ? { type: "incomplete", reason: message.status === "cancelled" ? "cancelled" : "error" }
          : { type: "complete" },
      providerMetadata: { minimal_agent: { truncated: Boolean(segment.truncated) } },
    });
  }
  if (!content.length && message.content && !isRunning && message.status !== "error") {
    content.push({ type: "text", text: message.content });
  }
  return {
    id, role: "assistant", content, status,
    metadata: {
      runId: message.run_id,
      custom: {
        uiStatus: isElsewhere ? "running_elsewhere" : message.status || "completed",
        step: message.run_id === input.run?.id ? input.run?.step || 0 : diagnostic?.step || 0,
        segmentsOmitted: message.segments_omitted || 0,
        fullAnswerFallback,
        readableFullAnswer: fullAnswerFallback ? readableSourceMarkdown(fullAnswerFallback, sources) : undefined,
        copyAnswer: !isRunning && message.content ? readableSourceMarkdown(message.content, sources) : undefined,
        workPlan: normalizeWorkPlan(message.work_plan),
        steering: normalizeSteering(message.steering),
        diagnostic,
        artifacts: normalizeArtifacts(message.artifacts),
        artifactWarning: typeof message.artifact_warning === "string" ? message.artifact_warning.slice(0, 500) : undefined,
        sources,
        unmatchedSources: isRunning || isElsewhere ? [] : unmatchedSourceReferences(message.content, sources),
        metrics,
        metricsSummary: formatMetrics(metrics),
      },
    },
  };
}

