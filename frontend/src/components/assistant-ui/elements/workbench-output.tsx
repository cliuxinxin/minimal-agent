import { useAuiState } from "@assistant-ui/react";
import { FileCode2Icon, DownloadIcon, ExternalLinkIcon, BookOpenIcon, FileSearchIcon } from "lucide-react";
import type { Artifact, Source, RunMetrics } from "@/workbench-data";
import { sizeLabel } from "@/workbench-data";

function ArtifactCard({ artifact }: { artifact: Artifact }) {
  return <div className="artifact-card" data-artifact-id={artifact.id}>
    <FileCode2Icon className="artifact-icon" aria-hidden />
    <div className="artifact-card-info"><strong title={artifact.name}>{artifact.name}</strong>
      <small>{artifact.change === "modified" ? "已更新" : "已生成"} · {sizeLabel(artifact.size)}</small></div>
    <button type="button" className="artifact-preview" onClick={() => {
      window.dispatchEvent(new CustomEvent("minimal-agent:preview-artifact", { detail: { id: artifact.id } }));
    }}>预览</button>
    <a className="artifact-download" href={artifact.download_url} download aria-label={`下载 ${artifact.name}`}>
      <DownloadIcon aria-hidden /><span>下载</span>
    </a>
  </div>;
}

function SourceCard({ source }: { source: Source }) {
  const date = source.date ? new Date(source.date) : null;
  const dateLabel = date && !Number.isNaN(date.getTime()) ? date.toLocaleDateString("zh-CN") : "日期未提供";
  return <a className="source-card" href={source.url} target="_blank" rel="noopener noreferrer" data-source-id={source.id}>
    <span className="source-card-top"><span className="source-reference">{source.id}</span>
      <span className={`source-read ${source.read ? "read" : ""}`}>{source.read ? <BookOpenIcon aria-hidden /> : <FileSearchIcon aria-hidden />}
        {source.read ? "已读取原文" : "搜索摘要"}</span></span>
    <strong>{source.title}<ExternalLinkIcon aria-hidden /></strong>
    <small>{new URL(source.url).hostname} · {dateLabel}</small>
  </a>;
}

export function WorkbenchOutput() {
  const artifacts = useAuiState((s) => s.message.metadata.custom.artifacts) as Artifact[] | undefined;
  const sources = useAuiState((s) => s.message.metadata.custom.sources) as Source[] | undefined;
  const metrics = useAuiState((s) => s.message.metadata.custom.metrics) as RunMetrics | null;
  const summary = useAuiState((s) => s.message.metadata.custom.metricsSummary);
  const unmatched = useAuiState((s) => s.message.metadata.custom.unmatchedSources) as string[] | undefined;
  const artifactWarning = useAuiState((s) => s.message.metadata.custom.artifactWarning);
  return <>
    {artifacts && artifacts.length > 0 ? <section className="message-artifacts" aria-label="本次任务生成的文件">
      <h3>生成的文件 <span>{artifacts.length}</span></h3>
      <div className="artifact-list">{artifacts.map((artifact) => <ArtifactCard key={artifact.id} artifact={artifact} />)}</div>
    </section> : null}
    {typeof artifactWarning === "string" && artifactWarning ? <p className="source-reference-warning" role="status">{artifactWarning}</p> : null}
    {sources && sources.length > 0 ? <section className="message-sources" aria-label="本次任务的资料来源">
      <h3>资料来源 <span>{sources.length}</span></h3>
      <div className="source-list">{sources.map((source) => <SourceCard key={source.id} source={source} />)}</div>
    </section> : null}
    {unmatched && unmatched.length > 0 ? <p className="source-reference-warning" title={unmatched.join("、")}>部分引用未匹配本次来源，请核对。</p> : null}
    {typeof summary === "string" && summary ? <p className="message-metrics" data-testid="run-metrics" title={metrics
      ? `输入 token：${metrics.input_tokens ?? "未知"}；输出 token：${metrics.output_tokens ?? "未知"}。用量为接口实际返回值；部分用量不代表完整消耗。` : undefined}>{summary}</p> : null}
  </>;
}
