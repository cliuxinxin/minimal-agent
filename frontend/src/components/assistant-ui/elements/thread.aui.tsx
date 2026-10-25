"use client";

// Adapted from assistant-ui's official Thread element. The local app keeps its
// existing composer; its thread, message parts, disclosures, and scroll behavior
// are provided by assistant-ui. See frontend/UPSTREAM.json and its MIT license.
import {
  ActionBarPrimitive,
  AuiIf,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  groupPartByType,
  useAuiState,
} from "@assistant-ui/react";
import { ArrowDownIcon, CheckIcon, CopyIcon, SparklesIcon } from "lucide-react";
import { MarkdownText } from "./markdown-text";
import {
  Reasoning,
  ReasoningRoot,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
} from "./reasoning.aui";
import { ToolFallback } from "./tool-fallback.aui";
import { TooltipIconButton } from "./tooltip-icon-button";
import { CollapsibleRoot, CollapsibleTrigger, CollapsibleContent } from "./collapsible-root";
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import type { ToolArtifact } from "@/message-adapter";
import { DiagnosticDisclosure } from "./diagnostic-disclosure";
import { WorkbenchOutput } from "./workbench-output";

const groupBy = groupPartByType({ reasoning: ["group-reasoning"] });

export function Thread() {
  return (
    <ThreadPrimitive.Root className="aui-root aui-thread-root chat-ui">
      <ThreadPrimitive.Viewport
        data-slot="aui_thread-viewport"
        id="conversation-scroll"
        className="aui-thread-viewport"
        autoScroll
        scrollToBottomOnRunStart
        scrollToBottomOnInitialize
        scrollToBottomOnThreadSwitch
      >
        <div className="aui-message-list">
          <ThreadPrimitive.Messages>
            {({ message }) => message.role === "user" ? <UserMessage /> : <AssistantMessage />}
          </ThreadPrimitive.Messages>
        </div>
      </ThreadPrimitive.Viewport>
      <ThreadPrimitive.ScrollToBottom asChild>
        <TooltipIconButton
          tooltip="回到最新内容"
          variant="outline"
          className="aui-thread-scroll-to-bottom"
        >
          <ArrowDownIcon />
        </TooltipIconButton>
      </ThreadPrimitive.ScrollToBottom>
    </ThreadPrimitive.Root>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="message user" data-role="user" aria-label="你的消息">
      <div className="message-content"><MessagePrimitive.Parts /></div>
    </MessagePrimitive.Root>
  );
}

function MessageState() {
  const meta = useAuiState((s) => s.message.metadata.custom);
  const status = String(meta.uiStatus || "completed");
  const step = Number(meta.step || 0);
  const statuses: Record<string, string> = {
    running: "运行中", stopping: "正在停止", completed: "已完成", cancelled: "已停止", error: "未完成",
    interrupted: "已中断", step_limit: "已达调用上限", stalled: "已暂停",
    running_elsewhere: "其他窗口运行中",
  };
  return (
    <div className="message-label">
      <SparklesIcon className="mini-mark" aria-hidden />
      <span>Minimal Agent</span>
      <span className="message-status" data-status={status}>
        {statuses[status] || status}{["running", "stopping"].includes(status) && step ? ` · 第 ${step} 步` : ""}
      </span>
    </div>
  );
}

function MessageNotice() {
  const meta = useAuiState((s) => s.message.metadata.custom);
  const incomplete = ["error", "cancelled", "interrupted", "step_limit", "stalled"].includes(String(meta.uiStatus));
  return (
    <>
      {Number(meta.segmentsOmitted) > 0 && (
        <p className="segment-note">已省略较早的 {Number(meta.segmentsOmitted)} 段过程，完整记录见本机 runs 文件夹。</p>
      )}
      {incomplete && (
        <p className="message-outcome">
          {meta.uiStatus === "cancelled" ? "任务已停止" : meta.uiStatus === "interrupted" ? "任务已中断" : "任务未完成"}；以上已收到的内容予以保留。
        </p>
      )}
      <MessagePrimitive.Error>
        <ErrorPrimitive.Root className="aui-message-error-root">
          <ErrorPrimitive.Message />
        </ErrorPrimitive.Root>
      </MessagePrimitive.Error>
    </>
  );
}

function FullAnswer() {
  const answer = useAuiState((s) => s.message.metadata.custom.fullAnswerFallback);
  if (typeof answer !== "string") return null;
  return (
    <CollapsibleRoot className="full-answer-disclosure">
      <CollapsibleTrigger>查看完整最终回复（Markdown 原文）</CollapsibleTrigger>
      <CollapsibleContent><pre>{answer}</pre></CollapsibleContent>
    </CollapsibleRoot>
  );
}

function CopyAnswer() {
  const answer = useAuiState((s) => s.message.metadata.custom.fullAnswerFallback);
  const { isCopied, copyToClipboard } = useCopyToClipboard();
  if (typeof answer === "string") {
    return <TooltipIconButton tooltip="复制完整回复" onClick={() => copyToClipboard(answer)}>
      {isCopied ? <CheckIcon /> : <CopyIcon />}
    </TooltipIconButton>;
  }
  return (
    <ActionBarPrimitive.Copy asChild>
      <TooltipIconButton tooltip="复制回复">
        <AuiIf condition={(s) => s.message.isCopied}><CheckIcon /></AuiIf>
        <AuiIf condition={(s) => !s.message.isCopied}><CopyIcon /></AuiIf>
      </TooltipIconButton>
    </ActionBarPrimitive.Copy>
  );
}

function AssistantMessage() {
  const id = useAuiState((s) => s.message.id);
  const status = useAuiState((s) => String(s.message.metadata.custom.uiStatus || "completed"));
  return (
    <MessagePrimitive.Root className={`message assistant ${status}`} data-run-id={id} data-role="assistant" aria-label="Agent 回复">
      <MessageState />
      <div className="message-content">
        <MessagePrimitive.GroupedParts groupBy={groupBy}>
          {({ part, children }) => {
            switch (part.type) {
              case "group-reasoning": {
                const running = part.status.type === "running";
                return (
                  <ReasoningRoot className="reasoning-segment" streaming={running} defaultOpen>
                    <ReasoningTrigger active={running} />
                    <ReasoningContent aria-busy={running}>
                      <ReasoningText>{children}</ReasoningText>
                    </ReasoningContent>
                  </ReasoningRoot>
                );
              }
              case "text":
                return <div className="text-segment" data-segment-id={part.id}>
                  <MarkdownText />
                  {part.providerMetadata?.minimal_agent?.truncated === true &&
                    <p className="segment-note">此段为截断预览，完整过程见本机运行记录。</p>}
                </div>;
              case "reasoning":
                return <div data-segment-id={part.id}>
                  <Reasoning {...part} />
                  {part.providerMetadata?.minimal_agent?.truncated === true &&
                    <p className="segment-note">此段为截断预览，完整过程见本机运行记录。</p>}
                </div>;
              case "tool-call": {
                const artifact = part.artifact as ToolArtifact | undefined;
                const toolStatus = artifact?.displayStatus ?? part.status;
                return (
                  <div className="inline-tool" data-status={artifact?.phase} data-segment-id={artifact?.segmentId}>
                    <ToolFallback {...part} status={toolStatus} />
                    {artifact?.argumentsTruncated && <p className="segment-note">参数预览已截断，完整参数见本机运行记录。</p>}
                  </div>
                );
              }
              case "indicator":
                return <span className="message-waiting" role="status">等待模型输出…</span>;
              default:
                return null;
            }
          }}
        </MessagePrimitive.GroupedParts>
        <FullAnswer />
        <MessageNotice />
        <WorkbenchOutput />
        <DiagnosticDisclosure />
        <ActionBarPrimitive.Root hideWhenRunning autohide="not-last" className="aui-assistant-action-bar">
          <CopyAnswer />
        </ActionBarPrimitive.Root>
      </div>
    </MessagePrimitive.Root>
  );
}
