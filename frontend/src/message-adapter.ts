import type { MessageStatus, ThreadAssistantMessagePart, ThreadMessageLike } from "@assistant-ui/react";

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
};
export type ConversationInput = {
  sessionId: string | null;
  messages: Message[];
  run?: { id: string; status: string; step?: number; error?: string | null } | null;
  busy?: boolean;
};
const running = (status?: string) => status === "running" || status === "stopping";

export function convertMessage(message: Message, index: number, input: ConversationInput): ThreadMessageLike {
  const id = message.run_id || `${input.sessionId || "draft"}-${message.role}-${index}`;
  if (message.role === "user") return { id, role: "user", content: message.content };
  const isRunning = running(message.status);
  const status: MessageStatus = isRunning ? { type: "running" }
    : message.status === "cancelled" ? { type: "incomplete", reason: "cancelled" }
      : message.status === "error" ? { type: "incomplete", reason: "error",
        error: message.run_id === input.run?.id ? input.run?.error || "本次任务未完成。" : "本次任务未完成，可查看本机运行记录。" }
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
      const settled = ["completed", "error", "cancelled"].includes(segment.status || "");
      content.push({
        type: "tool-call",
        // The provider call ID can arrive late; the host's segment ID is stable from the first delta.
        toolCallId: segment.id,
        toolName: segment.name || "准备调用工具",
        args,
        argsText: segment.arguments || "",
        ...(settled ? { result: segment.error ?? segment.result ?? null, isError: segment.status === "error" } : {}),
        artifact: { phase: segment.status, segmentId: segment.id, callId: segment.call_id, argumentsTruncated: Boolean(segment.arguments_truncated) },
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
        uiStatus: message.status || "completed",
        step: message.run_id === input.run?.id ? input.run?.step || 0 : 0,
        segmentsOmitted: message.segments_omitted || 0,
        fullAnswerFallback,
      },
    },
  };
}

