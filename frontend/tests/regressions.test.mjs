import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
async function productionModule(file) {
  const result = await build({ entryPoints: [resolve(root, file)], bundle: true,
    format: "esm", platform: "node", write: false });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].contents).toString("base64")}`);
}
const { convertMessage } = await productionModule("src/message-adapter.ts");
const { stopCurrentRun } = await productionModule("src/stop-run.ts");
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
