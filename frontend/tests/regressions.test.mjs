import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const frontendPackage = JSON.parse(await readFile(resolve(root, "package.json"), "utf8"));
async function productionModule(file) {
  const result = await build({ entryPoints: [resolve(root, file)], bundle: true,
    format: "esm", platform: "node", write: false,
    define: { __FRONTEND_VERSION__: JSON.stringify(frontendPackage.version) } });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].contents).toString("base64")}`);
}
const { convertMessage } = await productionModule("src/message-adapter.ts");
const { stopCurrentRun } = await productionModule("src/stop-run.ts");
const { FRONTEND_VERSION, assessRuntimeCompatibility, createRuntimeDiagnostic } =
  await productionModule("src/runtime-compatibility.ts");
const context = (message, error) => ({ sessionId: "session", messages: [message],
  run: { id: message.run_id, status: message.status, step: 2, error } });

test("terminal answer restores text beyond the bounded stream preview without moving tool parts", () => {
  const full = "x".repeat(65536) + "TAIL";
  const message = { role: "assistant", run_id: "run", status: "completed", content: full, segments: [
    { id: "thinking", type: "reasoning", step: 1, text: "模型提供的摘要。" },
    { id: "before", type: "text", step: 1, text: "开始运行。" },
    { id: "tool", type: "tool", step: 1, name: "run_python", status: "completed", arguments: "{}", result: 0 },
    { id: "answer", type: "text", step: 2, text: full.slice(0, 65536), truncated: true },
  ] };
  const result = convertMessage(message, 0, context(message));
  assert.deepEqual(result.content.map(part => part.type), ["reasoning", "text", "tool-call", "text"]);
  assert.equal(result.content.at(-1).text, full);
  assert.equal(result.content.at(-1).providerMetadata.minimal_agent.truncated, false);
  assert.equal(result.content[2].result, 0);
  assert.equal(message.segments.at(-1).text.length, 65536, "canonical bounded snapshot is not mutated");
});

test("streaming previews remain bounded and expose truncation notices", () => {
  const message = { role: "assistant", run_id: "run", status: "running", content: "", segments: [
    { id: "reason", type: "reasoning", step: 1, text: "summary", truncated: true },
    { id: "tool", type: "tool", step: 1, name: "run_python", status: "preparing", arguments: '{"code":"', arguments_truncated: true },
  ] };
  const result = convertMessage(message, 0, context(message));
  assert.equal(result.content[0].providerMetadata.minimal_agent.truncated, true);
  assert.equal(result.content[1].artifact.argumentsTruncated, true);
  assert.equal(result.content[1].artifact.phase, "preparing");
  assert.equal(Object.hasOwn(result.content[1], "result"), false, "preparing tools must not appear completed");
});

test("a model failure cannot fabricate a null result for a tool that never returned", () => {
  const message = { role: "assistant", run_id: "run", status: "error", content: "", segments: [
    { id: "search", type: "tool", name: "web_search", status: "error", arguments: '{"query":"test"}' },
  ] };
  const tool = convertMessage(message, 0, context(message, "Invalid model stream")).content[0];
  assert.equal(Object.hasOwn(tool, "result"), false,
    "missing tool output must stay absent so assistant-ui cannot infer successful completion");
  assert.equal(tool.artifact.phase, "interrupted", "legacy errors without a tool result have unknown execution outcome");
  assert.equal(tool.artifact.displayStatus.type, "incomplete");
});

test("terminal pending tools retain whether execution started", () => {
  for (const [phase, expected] of [["preparing", "not_executed"], ["running", "interrupted"],
    ["not_executed", "not_executed"], ["interrupted", "interrupted"]]) {
    const message = { role: "assistant", run_id: "run", status: "error", content: "", segments: [
      { id: "tool", type: "tool", name: "web_search", status: phase, arguments: "{}" },
    ] };
    const tool = convertMessage(message, 0, context(message)).content[0];
    assert.equal(tool.artifact.phase, expected);
    assert.equal(tool.artifact.displayStatus.type, "incomplete");
    assert.equal(Object.hasOwn(tool, "result"), false);
  }
});

test("actual tool errors remain errors even when the model later completes its answer", () => {
  const error = { code: "search_error", message: "Search was unavailable" };
  const message = { role: "assistant", run_id: "run", status: "completed", content: "Could not search", segments: [
    { id: "tool", type: "tool", name: "web_search", status: "error", arguments: "{}", error },
  ] };
  const tool = convertMessage(message, 0, context(message)).content[0];
  assert.equal(tool.artifact.phase, "error");
  assert.deepEqual(tool.artifact.displayStatus, { type: "incomplete", reason: "error", error });
  assert.equal(Object.hasOwn(tool, "result"), false, "an error is not a successful tool result");
});

test("real null results remain distinguishable from cancelled tools without results", () => {
  const message = { role: "assistant", run_id: "run", status: "cancelled", content: "", segments: [
    { id: "done", type: "tool", name: "run_python", status: "completed", arguments: "{}", result: null },
    { id: "pending", type: "tool", name: "web_search", status: "preparing", arguments: "{}" },
  ] };
  const [done, pending] = convertMessage(message, 0, context(message)).content;
  assert.equal(done.result, null);
  assert.deepEqual(done.artifact.displayStatus, { type: "complete" });
  assert.equal(Object.hasOwn(pending, "result"), false);
  assert.equal(pending.artifact.phase, "not_executed");
  assert.equal(pending.artifact.displayStatus.reason, "cancelled");
});

test("a failed message keeps its own diagnostic and error after another run starts", () => {
  const diagnostic = { schema_version: 1, run_id: "failed-run", status: "error", step: 1,
    connection: { provider: "openai", model: "test-model", endpoint: "https://example.com/v1", stream: true },
    error: { type: "ModelError", message: "Invalid model stream", phase: "stream_parse", field: "choices[0].delta.tool_calls", actual_type: "object" },
    events: [{ event: "model_error", step: 1, time: "2026-10-25T00:00:00Z" }] };
  const message = { role: "assistant", run_id: "failed-run", status: "error", content: "", segments: [], diagnostic };
  const result = convertMessage(message, 0, { sessionId: "session", messages: [message],
    run: { id: "new-run", status: "running", step: 2, diagnostic: { run_id: "new-run" } } });
  assert.deepEqual(result.metadata.custom.diagnostic, diagnostic);
  assert.equal(result.status.error, "Invalid model stream");
  assert.equal(result.metadata.custom.step, 1);
});

test("ambiguous multiple truncated final blocks retain order and offer the complete answer separately", () => {
  const message = { role: "assistant", run_id: "run", status: "completed", content: "first complete\nsecond completeTAIL", segments: [
    { id: "a", type: "text", step: 2, text: "first", truncated: true },
    { id: "r", type: "reasoning", step: 2, text: "summary" },
    { id: "b", type: "text", step: 2, text: "second", truncated: true },
  ] };
  const result = convertMessage(message, 0, context(message));
  assert.deepEqual(result.content.map(part => part.id), ["a", "r", "b"]);
  assert.equal(result.metadata.custom.fullAnswerFallback, message.content);
});

test("error display uses the backend error without duplicating received text", () => {
  const message = { role: "assistant", run_id: "run", status: "error",
    content: "partial output\n\n运行失败：连接中断", segments: [{ id: "a", type: "text", step: 1, text: "partial output" }] };
  const result = convertMessage(message, 0, context(message, "连接中断"));
  assert.equal(result.content[0].text, "partial output");
  assert.equal(result.status.error, "连接中断");
});

test("an error before any output is displayed only by the native error component", () => {
  const message = { role: "assistant", run_id: "run", status: "error",
    content: "运行失败：API Key 无效", segments: [] };
  const result = convertMessage(message, 0, context(message, "API Key 无效"));
  assert.deepEqual(result.content, []);
  assert.equal(result.status.error, "API Key 无效");
  assert.equal(result.status.type, "incomplete");
});

function pendingStop() {
  let state = { sessionId: "session", runId: "old-run", revision: 7, status: "running" };
  let resolveResponse, rejectResponse;
  const response = new Promise((resolve, reject) => { resolveResponse = resolve; rejectResponse = reject; });
  const applied = [], recovered = [], requested = [];
  const work = stopCurrentRun({ read: () => ({ ...state }),
    request: (value) => { requested.push(value); return response; },
    apply: (value) => { applied.push(value); state.status = "stopping"; },
    recover: async (error) => { recovered.push(error); },
  });
  return { work, applied, recovered, requested, resolveResponse, rejectResponse,
    nextRun() { state = { ...state, runId: "new-run", status: "running" }; }, read: () => state };
}

test("late stop success cannot mark the next run as stopping", async () => {
  const fixture = pendingStop();
  assert.equal(fixture.requested[0].runId, "old-run");
  fixture.nextRun();
  fixture.resolveResponse({ session: { run: { id: "old-run", status: "stopping" } } });
  await fixture.work;
  assert.equal(fixture.read().status, "running");
  assert.equal(fixture.read().runId, "new-run");
  assert.deepEqual(fixture.applied, []);
  assert.deepEqual(fixture.recovered, []);
});

test("late stop failure cannot recover over a newer run", async () => {
  const fixture = pendingStop();
  fixture.nextRun();
  fixture.rejectResponse(new Error("old request failed"));
  await fixture.work;
  assert.deepEqual(fixture.applied, []);
  assert.deepEqual(fixture.recovered, []);
});

test("a current stop request still applies its acknowledged state", async () => {
  const fixture = pendingStop();
  fixture.resolveResponse({ accepted: true });
  await fixture.work;
  assert.equal(fixture.read().status, "stopping");
  assert.deepEqual(fixture.applied, [{ accepted: true }]);
});

test("a matching backend permits new model runs using the version embedded at build time", () => {
  const result = assessRuntimeCompatibility({ app: "minimal-agent-web", version: frontendPackage.version,
    workspace_id: "123456abcdef", source_revision: "a".repeat(64) });
  assert.equal(FRONTEND_VERSION, frontendPackage.version);
  assert.equal(result.status, "compatible");
  assert.equal(result.blocksNewRuns, false);
});

test("an older service blocks new model runs even when it has the expected application identity", () => {
  const result = assessRuntimeCompatibility({ app: "minimal-agent-web", version: "0.4.0",
    workspace_id: "123456abcdef" }, "0.4.2");
  assert.equal(result.status, "version_mismatch");
  assert.equal(result.blocksNewRuns, true);
  assert.equal(result.backend.version, "0.4.0", "the displayed version must come from the running service");
  assert.equal(result.frontendVersion, "0.4.2");
});

test("missing or invalid service identity never passes a version match", () => {
  for (const runtime of [null, {}, { version: "0.4.2" }, { app: "another-app", version: "0.4.2" },
    { app: "minimal-agent-web", version: 42 }, { app: "minimal-agent-web", version: "" },
    { app: "minimal-agent-web", version: "0.4.2\nsecret" }, { app: "minimal-agent-web", version: "0.4.2\n" }]) {
    const result = assessRuntimeCompatibility(runtime, "0.4.2");
    assert.equal(result.status, "invalid_identity");
    assert.equal(result.blocksNewRuns, true);
  }
});

test("optional legacy revision metadata does not reject an otherwise matching service", () => {
  const result = assessRuntimeCompatibility({ app: "minimal-agent-web", version: "0.4.2" }, "0.4.2");
  assert.equal(result.blocksNewRuns, false);
  assert.equal(result.backend.source_revision, null);
  assert.equal(result.backend.workspace_id, null);
});

test("copied runtime diagnostics contain only version identity and the page origin", () => {
  const compatibility = assessRuntimeCompatibility({ app: "minimal-agent-web", version: "0.4.0",
    workspace_id: "123456abcdef", source_revision: "b".repeat(64),
    config: { api_key: "secret-key", model: "private-model" },
    messages: [{ content: "private conversation" }], root: "/home/private/workspace" }, "0.4.2");
  const report = createRuntimeDiagnostic(compatibility, {
    pageOrigin: "http://user:secret-password@127.0.0.1:8765/private?token=secret-query#private-fragment",
    checkedAt: "2026-10-25T12:34:56.000Z",
  });
  assert.deepEqual(report, { schema_version: 1, kind: "minimal-agent-runtime",
    frontend_version: "0.4.2", compatibility: "version_mismatch",
    backend: { app: "minimal-agent-web", version: "0.4.0", workspace_id: "123456abcdef", source_revision: "b".repeat(64) },
    page_origin: "http://127.0.0.1:8765", checked_at: "2026-10-25T12:34:56.000Z" });
  assert.doesNotMatch(JSON.stringify(report), /secret|private/);
});

test("unexpected metadata is omitted from safe runtime diagnostics", () => {
  const compatibility = assessRuntimeCompatibility({ app: "secret-app", version: "secret-key",
    workspace_id: "/home/private/workspace", source_revision: "secret-token" }, "0.4.2");
  const report = createRuntimeDiagnostic(compatibility, { pageOrigin: "file:///home/private/index.html", checkedAt: "secret" });
  assert.deepEqual(report.backend, { app: null, version: null, workspace_id: null, source_revision: null });
  assert.equal(report.page_origin, null);
  assert.equal(report.checked_at, null);
  assert.doesNotMatch(JSON.stringify(report), /secret|private/);
});

test("restored interrupted messages remain incomplete and preserve received text", () => {
  const message = { role: "assistant", run_id: "interrupted-run", status: "interrupted",
    content: "已完成文件读取", segments: [{ id: "text", type: "text", text: "已完成文件读取" }] };
  const result = convertMessage(message, 0, context(message));
  assert.equal(result.status.type, "incomplete");
  assert.equal(result.content[0].text, "已完成文件读取");
  assert.equal(result.metadata.custom.uiStatus, "interrupted");
});

test("artifacts are attached to their answer and use validated same-origin routes", () => {
  const id = "a".repeat(32);
  const message = { role: "assistant", run_id: "run", status: "completed", content: "已完成", artifacts: [
    { id, name: "monthly/report.csv", size: 12, change: "created", run_id: "run", created_at: "2026-10-25T10:00:00Z",
      preview_url: `/api/artifacts/${id}`, download_url: `/api/artifacts/${id}/download` },
    { id: "../config.json", name: "unsafe", size: 1 },
    { id: "b".repeat(32), name: "evil.py", preview_url: "https://evil.example/file", download_url: "javascript:alert(1)" },
  ] };
  const result = convertMessage(message, 0, context(message));
  assert.equal(result.metadata.custom.artifacts.length, 1);
  assert.equal(result.metadata.custom.artifacts[0].download_url, `/api/artifacts/${id}/download`);
  assert.equal(result.metadata.custom.artifacts[0].name, "monthly/report.csv");
});

test("source cards retain observed source IDs and reject unsafe or credential-bearing URLs", () => {
  const message = { role: "assistant", run_id: "run", status: "completed", content: "见 [Sabc](https://example.com/a)", sources: [
    { id: "Sabc", title: "来源", url: "https://example.com/a", date: "2026-10-24", read: true },
    { id: "Sbad", title: "bad", url: "javascript:alert(1)", read: false },
    { id: "Scredentials", title: "bad", url: "https://secret:token@example.com/a", read: false },
    { id: "Sslashes", title: "bad", url: "https:\\evil.example", read: false },
  ] };
  const result = convertMessage(message, 0, context(message));
  assert.equal(result.metadata.custom.sources.length, 1);
  assert.equal(result.metadata.custom.sources[0].id, "Sabc");
  assert.equal(result.metadata.custom.sources[0].read, true);
});

test("run metrics preserve unknown tokens and indicate partially reported usage", () => {
  const message = { role: "assistant", run_id: "run", status: "completed", content: "ok", metrics: {
    model_calls: 4, tool_calls: 2, input_tokens: 120, output_tokens: 30, total_tokens: 150,
    known_usage_calls: 2, elapsed_seconds: 12.5, compactions: 1,
  } };
  const result = convertMessage(message, 0, context(message));
  assert.equal(result.metadata.custom.metrics.total_tokens, 150);
  assert.match(result.metadata.custom.metricsSummary, /部分用量/);
  const unknown = convertMessage({ ...message, metrics: { model_calls: 2, tool_calls: 1,
    known_usage_calls: 0, input_tokens: null, output_tokens: null, total_tokens: null,
    elapsed_seconds: 3, compactions: 0 } }, 0, context(message));
  assert.equal(unknown.metadata.custom.metrics.total_tokens, null);
  assert.match(unknown.metadata.custom.metricsSummary, /用量未知/);
});

test("a previous answer keeps its metrics and sources when a new task is running", () => {
  const message = { role: "assistant", run_id: "old", status: "completed", content: "old answer",
    metrics: { model_calls: 1, tool_calls: 0, known_usage_calls: 0, elapsed_seconds: 2, compactions: 0 },
    sources: [{ id: "Sold", title: "old source", url: "https://example.com/old", read: false }] };
  const result = convertMessage(message, 0, { sessionId: "session", messages: [message], run: {
    id: "new", status: "running", metrics: { model_calls: 99, tool_calls: 50, known_usage_calls: 0 } } });
  assert.equal(result.metadata.custom.metrics.model_calls, 1);
  assert.equal(result.metadata.custom.sources[0].id, "Sold");
});

test("invalid numeric usage cannot become misleading zero or negative totals", () => {
  const message = { role: "assistant", run_id: "run", status: "completed", content: "ok", metrics: {
    model_calls: 2, tool_calls: 0, input_tokens: -1, output_tokens: "2", total_tokens: NaN,
    known_usage_calls: 0, elapsed_seconds: 1, compactions: 0,
  } };
  const result = convertMessage(message, 0, context(message));
  assert.equal(result.metadata.custom.metrics.input_tokens, null);
  assert.equal(result.metadata.custom.metrics.output_tokens, null);
  assert.equal(result.metadata.custom.metrics.total_tokens, null);
});

test("a task running in another host is labeled separately without claiming tool interruption", () => {
  const message = { role: "assistant", run_id: "external-run", status: "running", content: "", segments: [
    { id: "tool", type: "tool", status: "running", name: "run_python", arguments: "{}" },
  ] };
  const result = convertMessage(message, 0, { sessionId: "session", messages: [message],
    run: { id: "external-run", status: "running_elsewhere" } });
  assert.equal(result.metadata.custom.uiStatus, "running_elsewhere");
  assert.equal(result.content[0].artifact.phase, "running");
  assert.equal(result.content[0].artifact.displayStatus.type, "running");
  assert.equal(result.status.type, "incomplete");
});

test("unmatched source links are reported without inspecting fenced or inline code examples", () => {
  const message = { role: "assistant", run_id: "sources", status: "completed", content:
    "匹配 [Sgood](https://example.com/a)。\n" +
    "不匹配 [Sgood](https://example.com/wrong) 和 [Sunknown](https://example.com/u)。\n" +
    "```markdown\n[Sexample](https://example.com/example)\n```\n" +
    "行内示例 `[Sinline](https://example.com/inline)`。\n" +
    "~~~\n[Stilde](https://example.com/tilde)\n~~~",
    sources: [{ id: "Sgood", title: "Source", url: "https://example.com/a", read: true }] };
  const result = convertMessage(message, 0, context(message));
  assert.deepEqual(result.metadata.custom.unmatchedSources, ["Sgood", "Sunknown"]);
});

test("source reference validation ignores ordinary links and accepts canonical URL spelling", () => {
  const message = { role: "assistant", run_id: "sources", status: "completed", content:
    "[Sgood](https://EXAMPLE.com/) 与 [普通链接](https://unrelated.example/)。",
    sources: [{ id: "Sgood", title: "Source", url: "https://example.com", read: false }] };
  const result = convertMessage(message, 0, context(message));
  assert.deepEqual(result.metadata.custom.unmatchedSources, []);
});
