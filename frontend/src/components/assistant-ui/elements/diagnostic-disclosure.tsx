"use client";

import { useMemo, useState } from "react";
import { useAuiState } from "@assistant-ui/react";
import { BugIcon, CheckIcon, ChevronDownIcon, CopyIcon, DownloadIcon } from "lucide-react";
import type { RunDiagnostic } from "@/run-diagnostic";
import { Button } from "@/components/ui/button";
import { CollapsibleRoot, CollapsibleTrigger, CollapsibleContent } from "./collapsible-root";

const phaseLabels: Record<string, string> = {
  request: "构建模型请求",
  transport: "连接模型服务",
  response_headers: "接收响应头",
  model_request: "模型请求",
  http_request: "模型请求",
  stream_decode: "解码模型流",
  stream_event: "读取模型流事件",
  stream_delta: "校验流式字段",
  stream_finish: "校验流式结束状态",
  tool_arguments: "校验工具参数",
  stream_parse: "解析模型流",
  stream_read: "接收模型流",
  response_parse: "解析模型响应",
  tool_registration: "加载工具",
  agent_run: "执行任务",
  host_run: "本地运行",
};

export function DiagnosticDisclosure() {
  const diagnostic = useAuiState((s) => s.message.metadata.custom.diagnostic) as RunDiagnostic | undefined;
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const json = useMemo(() => diagnostic ? JSON.stringify(diagnostic, null, 2) : "", [diagnostic]);
  if (!diagnostic) return null;

  const error = diagnostic.error || {};
  const connection = diagnostic.connection || {};
  const fields = [
    ["运行 ID", diagnostic.run_id],
    ["提供方 / 模型", [connection.provider, connection.model].filter(Boolean).join(" / ")],
    ["失败阶段", error.phase ? `${phaseLabels[error.phase] || error.phase} · 第 ${diagnostic.step} 步` : undefined],
    ["错误代码", error.code],
    ["异常字段", error.field],
    ["预期 / 实际类型", [error.expected, error.actual_type].filter(Boolean).join(" / ")],
    ["流片段序号", error.chunk_index],
    ["请求 ID", error.request_id],
    ["HTTP 状态", error.http_status],
    ["已用时间", error.elapsed_ms === undefined ? undefined : `${error.elapsed_ms} ms`],
    ["模型端点", connection.endpoint],
  ].filter(([, value]) => value !== undefined && value !== "");

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(json);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([json + "\n"], { type: "application/json;charset=utf-8" }));
    const link = document.createElement("a");
    const name = diagnostic.run_id.replace(/[^a-zA-Z0-9_-]/g, "-").slice(0, 100) || "run";
    link.href = url;
    link.download = `minimal-agent-${name}.diagnostic.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  return (
    <CollapsibleRoot className="diagnostic-disclosure" data-slot="diagnostic-disclosure">
      <CollapsibleTrigger className="diagnostic-trigger" aria-label="诊断信息">
        <BugIcon aria-hidden />
        <span>诊断信息</span>
        <span className="diagnostic-trigger-hint">查看失败位置与运行记录</span>
        <ChevronDownIcon className="diagnostic-chevron" aria-hidden />
      </CollapsibleTrigger>
      <CollapsibleContent className="diagnostic-content">
        <div className="diagnostic-actions">
          <span className="diagnostic-caption">服务端已脱敏的诊断记录</span>
          <Button variant="outline" size="sm" onClick={copy}>
            {copyState === "copied" ? <CheckIcon aria-hidden /> : <CopyIcon aria-hidden />}
            {copyState === "copied" ? "已复制" : "复制诊断 JSON"}
          </Button>
          <Button variant="outline" size="sm" onClick={download}>
            <DownloadIcon aria-hidden />下载诊断 JSON
          </Button>
        </div>
        {copyState === "failed" && <p className="diagnostic-copy-error" role="status">无法访问剪贴板，请下载 JSON，或选中下方内容复制。</p>}
        {(diagnostic.log_error || diagnostic.storage_error) &&
          <p className="diagnostic-storage-warning" data-slot="diagnostic-storage-warning" role="status">
            本机{diagnostic.log_error && diagnostic.storage_error ? "日志和诊断文件" : diagnostic.log_error ? "运行日志" : "诊断文件"}未能完整保存。下方诊断仍可查看，请下载 JSON 留存。
          </p>}
        <dl className="diagnostic-fields">
          {fields.map(([label, value]) => <div key={String(label)}><dt>{label}</dt><dd>{value}</dd></div>)}
        </dl>
        <p className="diagnostic-json-label">完整诊断 JSON</p>
        <pre className="diagnostic-json" data-slot="diagnostic-json" aria-label="诊断 JSON" tabIndex={0}>{json}</pre>
      </CollapsibleContent>
    </CollapsibleRoot>
  );
}
