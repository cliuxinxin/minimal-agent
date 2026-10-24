"use strict";

// The browser only talks to the local host; provider credentials stay in its config.
const $ = (id) => document.getElementById(id);
const state = {
  config: { provider: "openai", model: "", base_url: "", has_api_key: false, configured: false,
    max_steps: 80, has_search_api_key: false },
  sessions: [], files: [], session: null, attachments: [], drafts: new Map(),
  revision: 0, poll: null, sending: false, uploading: false, selecting: false, initialized: false,
  messagesSignature: "", traceSignature: "", toastTimer: null,
  toolInventory: { tools: [], saved_tools: [], tool_errors: [] },
  toolsRequest: 0, toolsRun: null, searchKeyRemoved: false,
};
const active = (run) => run && ["running", "stopping"].includes(run.status);
const busy = () => state.sending || state.uploading || state.selecting;
const providerName = (value) => value === "anthropic" ? "Anthropic" : "OpenAI";

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function icon(name, small = false) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `icon${small ? " small" : ""}`);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

async function api(path, data) {
  let response;
  try {
    response = await fetch(path, {
      method: data === undefined ? "GET" : "POST",
      headers: data === undefined ? {} : { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
      cache: "no-store",
    });
  } catch {
    throw new Error("无法连接本地服务，请确认启动窗口仍在运行。");
  }
  let result;
  try { result = await response.json(); }
  catch { throw new Error("本地服务返回了无效响应，请重新打开页面。"); }
  if (!response.ok) throw new Error(result.error || `请求失败（${response.status}）。`);
  return result;
}

function toast(message) {
  clearTimeout(state.toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3600);
}

function formError(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

function connectionError(message) {
  $("connection-banner").textContent = message || "";
  $("connection-banner").hidden = !message;
}

function saveDraft() {
  state.drafts.set(state.session?.id || "draft", {
    text: $("message-input").value,
    attachments: [...state.attachments],
  });
}

function restoreDraft(key) {
  const draft = state.drafts.get(key) || { text: "", attachments: [] };
  $("message-input").value = draft.text;
  state.attachments = [...draft.attachments];
  resizeInput();
  renderAttachments();
}

function resizeInput() {
  const input = $("message-input");
  input.rows = Math.max(3, Math.min(7, input.value.split("\n").length));
}

function renderConfig() {
  $("config-summary").textContent = state.config.configured
    ? `${providerName(state.config.provider)} · ${state.config.model}` : "尚未配置模型";
  const model = state.session || (state.config.configured ? state.config : null);
  $("model-line").hidden = !model;
  const limit = model?.max_steps === null ? "不限调用次数" : `至多 ${model?.max_steps ?? 80} 次调用`;
  const searching = Boolean(model?.search_enabled ?? model?.has_search_api_key);
  $("model-line").textContent = model
    ? `${providerName(model.provider)} / ${model.model} · ${limit} · ${searching ? "搜索已启用" : "搜索未配置"}` : "";
  $("model-line").title = $("model-line").textContent;
}

function renderSessions() {
  const list = $("session-list");
  list.replaceChildren();
  const items = state.session ? state.sessions : [{ id: null, title: "新对话", status: "idle" }, ...state.sessions];
  for (const session of items) {
    const selected = session.id === (state.session?.id || null);
    const button = node("button", `session-row${selected ? " selected" : ""}`);
    button.type = "button";
    button.disabled = busy();
    button.title = session.title;
    if (selected) button.setAttribute("aria-current", "page");
    button.append(icon("chat"), node("span", "session-name", session.title));
    if (selected || ["running", "stopping"].includes(session.status)) {
      button.append(node("span", `session-dot${["running", "stopping"].includes(session.status) ? " running" : ""}`));
    }
    button.addEventListener("click", () => {
      if (selected) return;
      selectSession(session.id).catch((error) => toast(error.message));
    });
    list.append(button);
  }
}

function renderAttachments() {
  const holder = $("attachments");
  holder.replaceChildren();
  holder.hidden = !state.attachments.length;
  for (const name of state.attachments) {
    const chip = node("span", "attachment");
    chip.append(icon("file"), node("span", "attachment-name", name));
    const remove = node("button");
    remove.type = "button";
    remove.disabled = busy();
    remove.setAttribute("aria-label", `移除附件 ${name}`);
    remove.append(icon("close"));
    remove.addEventListener("click", () => {
      state.attachments = state.attachments.filter((item) => item !== name);
      renderAttachments(); renderFiles(); saveDraft();
    });
    chip.append(remove); holder.append(chip);
  }
}

function attach(name) {
  if (!state.attachments.includes(name)) state.attachments.push(name);
  renderAttachments(); renderFiles(); saveDraft();
}

function sizeLabel(size) {
  return size < 1024 ? `${size} B` : size < 1024 * 1024
    ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function renderFiles() {
  $("file-count").textContent = String(state.files.length);
  const holder = $("file-list");
  holder.replaceChildren();
  if (!state.files.length) holder.append(node("p", "empty-list", "还没有文件，上传一份开始吧。"));
  for (const file of state.files) {
    const row = node("div", "file-row");
    const info = node("div", "file-info");
    const name = node("strong", "", file.name);
    name.title = file.name;
    info.append(name, node("small", "", sizeLabel(file.size)));
    const selected = state.attachments.includes(file.name);
    const button = node("button", `file-action${selected ? " selected" : ""}`, selected ? "已添加" : "添加");
    button.type = "button";
    button.disabled = selected || busy();
    button.setAttribute("aria-label", `${selected ? "已添加" : "添加"} ${file.name}`);
    button.addEventListener("click", () => { attach(file.name); toast(`已添加 ${file.name}`); });
    row.append(icon("file"), info, button); holder.append(row);
  }
}

function renderTools() {
  const { tools, saved_tools: saved, tool_errors: errors } = state.toolInventory;
  const holder = $("saved-tools-list");
  const expanded = new Set([...holder.querySelectorAll("details[open]")].map((item) => item.dataset.tool));
  const scroll = $("tools-dialog").scrollTop;
  const savedNames = new Set(saved.map((tool) => tool.name));
  const descriptions = {
    list_files: "列出工作文件。", read_file: "读取文本文件，支持分段查看。",
    run_python: "编写并运行 Python，处理本机工作文件。",
    create_tool: "测试代码并保存为可重复调用的工具。",
    web_search: "使用 DeepSeek 搜索网页并返回来源。",
    fetch_webpage: "读取指定网页的正文内容。",
  };
  const builtin = $("builtin-tools-list"); builtin.replaceChildren();
  for (const tool of tools.filter((item) => !savedNames.has(item.name))) {
    const row = node("div", "builtin-tool");
    row.append(node("code", "", tool.name), node("p", "", descriptions[tool.name] || tool.description));
    builtin.append(row);
  }
  holder.replaceChildren(); $("saved-tools-count").textContent = String(saved.length);
  if (!saved.length) holder.append(node("p", "tools-empty", "还没有保存的工具。在对话中说“帮我创建一个……”即可开始。"));
  for (const tool of saved) {
    const item = node("details", "saved-tool");
    item.dataset.tool = tool.name; item.open = expanded.has(tool.name);
    const summary = node("summary");
    summary.append(node("strong", "", tool.name), node("span", "", tool.description));
    item.append(summary);
    const updated = new Date(tool.updated_at);
    if (!Number.isNaN(updated.getTime())) item.append(node("p", "tool-updated", `更新于 ${updated.toLocaleString("zh-CN", { hour12: false })}`));
    const sourceHeading = node("div", "tool-source-heading");
    const copy = node("button", "text-button", "复制代码"); copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(tool.code || ""); toast("代码已复制"); }
      catch { toast("复制未成功，请选中代码后复制。"); }
    });
    sourceHeading.append(node("span", "", "Python 源码"), copy);
    item.append(sourceHeading, node("pre", "tool-source", tool.code || ""),
      node("div", "tool-example-heading", "测试输入示例"),
      node("pre", "tool-example", JSON.stringify(tool.example_arguments ?? {}, null, 2)));
    holder.append(item);
  }
  const warnings = errors.map((error) => typeof error === "string" ? error
    : `${error.name || error.file || "工具"}：${error.message || error.error || "无法加载，请检查本机工具文件。"}`);
  formError("tools-warning", warnings.length ? `部分工具未能加载：\n${warnings.join("\n")}` : "");
  $("tools-dialog").scrollTop = scroll;
}

async function refreshTools() {
  const request = ++state.toolsRequest;
  $("tools-loading").hidden = false; $("refresh-tools").disabled = true;
  formError("tools-error", "");
  try {
    const result = await api("/api/tools");
    if (request !== state.toolsRequest) return;
    if (!Array.isArray(result.tools) || !Array.isArray(result.saved_tools) || !Array.isArray(result.tool_errors)) {
      throw new Error("工具列表格式无效，请重新启动本地服务后重试。");
    }
    state.toolInventory = result; renderTools();
  } catch (error) {
    if (request === state.toolsRequest) formError("tools-error", error.message);
  } finally {
    if (request === state.toolsRequest) {
      $("tools-loading").hidden = true; $("refresh-tools").disabled = false;
    }
  }
}

async function openTools() {
  if (busy()) return;
  renderTools(); $("tools-dialog").showModal();
  if (state.session?.run && !active(state.session.run)) state.toolsRun = state.session.run.id;
  await refreshTools();
}

// A deliberately small Markdown renderer. External text never becomes HTML.
function inlineText(element, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let position = 0;
  for (const match of text.matchAll(pattern)) {
    element.append(document.createTextNode(text.slice(position, match.index)));
    const value = match[0];
    if (value.startsWith("`")) element.append(node("code", "", value.slice(1, -1)));
    else if (value.startsWith("**")) element.append(node("strong", "", value.slice(2, -2)));
    else {
      const parts = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(value);
      const link = node("a", "", parts[1]);
      link.href = parts[2]; link.target = "_blank"; link.rel = "noopener noreferrer";
      element.append(link);
    }
    position = match.index + value.length;
  }
  element.append(document.createTextNode(text.slice(position)));
}

function renderMarkdown(text) {
  const holder = node("div", "markdown");
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let paragraph = [], list = null;
  function flush() {
    if (paragraph.length) {
      const p = node("p");
      paragraph.forEach((line, i) => { if (i) p.append(document.createElement("br")); inlineText(p, line); });
      holder.append(p); paragraph = [];
    }
    list = null;
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const language = line.trim().slice(3).trim();
      const code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      const content = code.join("\n");
      const block = node("div", "code-block");
      const header = node("div", "code-heading");
      const copy = node("button", "copy-code");
      copy.type = "button";
      copy.append(icon("copy"), document.createTextNode("复制"));
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(content); toast("代码已复制"); }
        catch { toast("复制未成功，请选中代码后复制。"); }
      });
      header.append(node("span", "", language || "代码"), copy);
      const pre = node("pre"); pre.append(node("code", "", content));
      block.append(header, pre); holder.append(block);
      continue;
    }
    if (!line.trim()) { flush(); continue; }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      flush(); const h = node(`h${Math.min(4, heading[1].length + 1)}`);
      inlineText(h, heading[2]); holder.append(h); continue;
    }
    const bullet = /^\s*(?:([-*])\s+|(\d+)\.\s+)(.+)$/.exec(line);
    if (bullet) {
      if (paragraph.length) flush();
      const type = bullet[2] ? "OL" : "UL";
      if (!list || list.tagName !== type) { list = node(type.toLowerCase()); holder.append(list); }
      const li = node("li"); inlineText(li, bullet[3]); list.append(li); continue;
    }
    list = null; paragraph.push(line);
  }
  flush(); return holder;
}

function renderMessages(force = false) {
  const messages = state.session?.messages || [];
  const signature = JSON.stringify([state.session?.id, messages]);
  $("empty-state").hidden = Boolean(messages.length);
  $("messages").hidden = !messages.length;
  if (!force && signature === state.messagesSignature) return;
  state.messagesSignature = signature;
  const region = $("conversation");
  const nearBottom = region.scrollHeight - region.scrollTop - region.clientHeight < 120;
  const holder = $("messages"); holder.replaceChildren();
  for (const item of messages) {
    const section = node("article", `message ${item.role}${item.status ? ` ${item.status}` : ""}`);
    section.setAttribute("aria-label", item.role === "user" ? "你的消息" : "Agent 回复");
    if (item.role === "user") section.append(node("div", "message-content", item.content));
    else {
      const label = node("div", "message-label");
      const mark = icon("spark"); mark.setAttribute("class", "mini-mark");
      label.append(mark, document.createTextNode("Minimal Agent"));
      const content = node("div", "message-content");
      content.append(renderMarkdown(item.content)); section.append(label, content);
    }
    holder.append(section);
  }
  if (nearBottom || force) requestAnimationFrame(() => { region.scrollTop = region.scrollHeight; });
}

function renderRun() {
  const run = state.session?.run;
  const running = active(run);
  $("open-trace").hidden = !run;
  const button = $("send-button");
  button.classList.toggle("stopping", run?.status === "stopping");
  button.disabled = busy() || !state.initialized || run?.status === "stopping";
  button.setAttribute("aria-label", running ? "停止运行" : "发送消息");
  button.replaceChildren(icon(running ? "stop" : "send"));
  $("run-status").hidden = !running;
  if (run?.status === "stopping") $("run-status").textContent = "正在停止，等待当前请求结束…";
  else if (running) {
    const last = run.events?.at(-1);
    $("run-status").textContent = last?.event === "tool_call"
      ? `正在执行 ${last.data.name}…` : `正在处理${run.step ? ` · 第 ${run.step} 步` : ""}…`;
  }
  // Freeze state changes while a local send, upload, or session switch is pending.
  // Once the POST is acknowledged, a running model does not block drafting the next turn.
  $("message-input").readOnly = state.sending || state.selecting;
  for (const id of ["new-chat", "open-settings", "open-files", "open-tools", "attach-file",
    "summarize-file", "create-tool-prompt", "search-web-prompt", "upload-files"]) $(id).disabled = busy();
  $("session-list").querySelectorAll("button").forEach((item) => { item.disabled = busy(); });
  $("attachments").querySelectorAll("button").forEach((item) => { item.disabled = busy(); });
  $("file-list").querySelectorAll("button").forEach((item) => {
    item.disabled = busy() || item.classList.contains("selected");
  });
  if ($("trace-dialog").open) renderTrace();
}

function renderTrace(force = false) {
  if (!$("trace-dialog").open) return;
  const run = state.session?.run;
  const events = run?.events || [];
  const omitted = run?.omitted_events || 0;
  const signature = run ? JSON.stringify([run.id, run.status, run.step,
    run.event_count ?? events.length, omitted]) : "no-run";
  if (!force && signature === state.traceSignature) return;
  state.traceSignature = signature;
  const holder = $("trace-list");
  const opened = new Set([...holder.querySelectorAll("details[open]")].map((item) => item.dataset.key));
  const scroll = $("trace-dialog").scrollTop;
  holder.replaceChildren();
  if (!run) { holder.append(node("p", "empty-list", "发送一个任务后，这里会显示运行过程。")); return; }
  const statuses = { running: "正在运行", stopping: "正在停止", completed: "已完成", error: "运行失败", cancelled: "已停止" };
  $("trace-subtitle").textContent = `${statuses[run.status] || run.status} · ${run.step} 次模型请求` +
    (omitted ? ` · 仅显示最近 ${events.length} 条记录；已省略 ${omitted} 条，完整记录见本机 runs 文件夹。` : "");
  for (const [index, event] of events.entries()) {
    const data = event.data || {};
    let title, description = "", details = null, kind = "";
    if (event.event === "run_start") { title = "任务开始"; }
    else if (event.event === "model_start") { title = `请求模型 · 第 ${event.step} 步`; }
    else if (event.event === "tool_call") {
      title = data.name; description = "调用工具"; kind = "tool";
      try { details = JSON.stringify(JSON.parse(data.arguments), null, 2); }
      catch { details = data.arguments; }
    } else if (event.event === "tool_result") {
      title = `${data.name} · ${data.ok ? "执行成功" : "未完成"}`;
      kind = data.ok ? "success" : "error";
      details = JSON.stringify(data.ok ? data.result : data.error, null, 2);
    } else if (event.event === "run_end") {
      title = data.status === "completed" ? "任务完成" : data.status === "cancelled" ? "任务已停止" : "任务未完成";
      kind = data.status === "completed" ? "success" : data.status === "cancelled" ? "" : "error";
      description = data.error || "";
    } else continue;
    if (data.truncated || data.result?.truncated || data.error?.truncated) {
      description = `${description ? `${description} · ` : ""}当前显示截断预览，完整内容见本机 runs 文件夹。`;
    }
    const row = node("section", `trace-event ${kind}`);
    const head = node("div", "trace-title");
    const time = new Date(event.time);
    head.append(node("span", "", title), node("span", "trace-time", Number.isNaN(time.getTime()) ? "" : time.toLocaleTimeString("zh-CN", { hour12: false })));
    row.append(head);
    if (description) row.append(node("p", "", description));
    if (details !== null && details !== undefined) {
      const disclosure = node("details");
      disclosure.dataset.key = `${run.id}-${omitted + index}`;
      disclosure.open = opened.has(disclosure.dataset.key);
      disclosure.append(node("summary", "", event.event === "tool_call" ? "查看参数" : "查看结果"), node("pre", "", String(details)));
      row.append(disclosure);
    }
    holder.append(row);
  }
  $("trace-dialog").scrollTop = scroll;
}

function renderSession(force = false) {
  $("conversation-title").textContent = state.session?.title || "新对话";
  renderConfig(); renderSessions(); renderMessages(force); renderRun();
  const run = state.session?.run;
  if ($("tools-dialog").open && run && !active(run) && state.toolsRun !== run.id) {
    state.toolsRun = run.id;
    refreshTools();
  }
}

async function refreshSessions() {
  const result = await api("/api/sessions");
  state.sessions = result.sessions; renderSessions();
}

function schedulePoll(id, revision, delay = 750) {
  clearTimeout(state.poll);
  state.poll = setTimeout(async () => {
    if (revision !== state.revision || state.session?.id !== id) return;
    try {
      const result = await api(`/api/sessions/${id}`);
      if (revision !== state.revision || state.session?.id !== id) return;
      state.session = result.session;
      connectionError(""); renderSession();
      if (active(state.session.run)) schedulePoll(id, revision);
      else await refreshSessions();
    } catch (error) {
      if (revision !== state.revision || state.session?.id !== id) return;
      connectionError(error.message);
      schedulePoll(id, revision, 2500);
    }
  }, delay);
}

async function selectSession(id) {
  if (busy()) return;
  saveDraft();
  clearTimeout(state.poll);
  const revision = ++state.revision;
  if (!id) {
    state.session = null; restoreDraft("draft"); renderSession(true); return;
  }
  state.selecting = true; renderRun();
  try {
    const result = await api(`/api/sessions/${id}`);
    if (revision !== state.revision) return;
    state.session = result.session;
    restoreDraft(id); renderSession(true);
    if (active(state.session.run)) schedulePoll(id, revision);
  } catch (error) {
    if (revision === state.revision && active(state.session?.run)) schedulePoll(state.session.id, revision);
    throw error;
  } finally { state.selecting = false; renderRun(); }
}

function newChat() {
  if (busy()) return;
  saveDraft(); clearTimeout(state.poll); ++state.revision;
  state.session = null; state.drafts.delete("draft"); restoreDraft("draft");
  renderSession(true); $("message-input").focus();
}

async function sendMessage(event) {
  event.preventDefault();
  if (busy()) return;
  if (active(state.session?.run)) { await stopRun(); return; }
  if (state.sending || !state.initialized) return;
  const text = $("message-input").value.trim();
  if (!text && !state.attachments.length) { $("message-input").focus(); return; }
  if (!state.config.configured && !state.session) { openSettings(); return; }
  const attachments = [...state.attachments];
  const message = (text || "请阅读并总结附加的文件。") + (attachments.length
    ? `\n\n附加的工作区文件（可使用 read_file 读取）：\n${attachments.map((name) => `- ${JSON.stringify(name)}`).join("\n")}` : "");
  state.sending = true; renderRun();
  const revision = state.revision;
  let targetId = state.session?.id;
  try {
    if (!targetId) {
      const created = await api("/api/sessions", {});
      targetId = created.session.id;
      if (revision !== state.revision) return;
      state.session = created.session;
    }
    await api(`/api/sessions/${targetId}/messages`, { message });
    if (revision !== state.revision || state.session?.id !== targetId) return;
    $("message-input").value = ""; state.attachments = [];
    state.drafts.delete("draft"); state.drafts.delete(targetId);
    resizeInput(); renderAttachments(); renderFiles();
    const result = await api(`/api/sessions/${targetId}`);
    if (revision !== state.revision || state.session?.id !== targetId) return;
    state.session = result.session; renderSession(true);
    await refreshSessions();
    if (revision === state.revision && active(state.session?.run)) schedulePoll(targetId, revision);
    connectionError("");
  } catch (error) {
    toast(error.message);
    if (targetId && revision === state.revision) {
      // Recover the actual server state after an uncertain POST response.
      schedulePoll(targetId, revision, 500);
    }
  } finally { state.sending = false; renderRun(); }
}

async function stopRun() {
  const session = state.session;
  if (!active(session?.run) || session.run.status === "stopping") return;
  const revision = state.revision;
  try {
    const result = await api(`/api/sessions/${session.id}/stop`, { run_id: session.run.id });
    if (revision !== state.revision || state.session?.id !== session.id) return;
    state.session = result.session; renderSession(); schedulePoll(session.id, revision);
  } catch (error) {
    toast(error.message);
    if (revision === state.revision) schedulePoll(session.id, revision, 100);
  }
}

function keyCanStay() {
  const defaults = { openai: "https://api.openai.com/v1", anthropic: "https://api.anthropic.com" };
  const normalize = (value, provider) => (value.trim() || defaults[provider]).replace(/\/+$/, "");
  return state.config.has_api_key && $("provider").value === state.config.provider &&
    normalize($("base-url").value, $("provider").value) === normalize(state.config.base_url || "", state.config.provider);
}

function updateSettingHints() {
  const canStay = keyCanStay();
  $("api-key").required = !canStay;
  $("api-key").placeholder = canStay ? "已保存，留空继续使用" : "填写 API Key";
  $("key-hint").textContent = canStay
    ? "已保存密钥；留空保留原值。密钥仅保存在本机配置。"
    : "密钥仅保存到本机配置，不保存在浏览器中。";
  $("base-url").placeholder = $("provider").value === "anthropic"
    ? "https://api.anthropic.com" : "https://api.openai.com/v1";
}

function updateSearchKeyHint() {
  const removed = state.searchKeyRemoved;
  const saved = Boolean(state.config.has_search_api_key);
  $("search-api-key").disabled = removed;
  $("toggle-search-key").disabled = removed;
  $("search-api-key").placeholder = removed ? "保存后移除搜索密钥"
    : saved ? "已保存，留空继续使用" : "填写 DeepSeek API Key";
  $("search-key-hint").textContent = removed
    ? "保存后停用联网搜索；撤销清除可保留已有密钥。"
    : saved ? "已保存搜索密钥；留空保留原值。密钥仅保存在本机。"
      : "填入后启用网页搜索与正文读取，可配合任一模型使用。";
  $("clear-search-key").hidden = !saved;
  $("clear-search-key").textContent = removed ? "撤销清除" : "清除已保存的 Key";
}

function openSettings() {
  if (busy()) return;
  $("provider").value = state.config.provider;
  $("model").value = state.config.model;
  $("api-key").value = ""; $("api-key").type = "password";
  $("toggle-key").setAttribute("aria-pressed", "false");
  $("toggle-key").setAttribute("aria-label", "显示 API Key");
  $("base-url").value = state.config.base_url || "";
  $("max-steps").value = state.config.max_steps === null ? "unlimited" : String(state.config.max_steps ?? 80);
  state.searchKeyRemoved = false;
  $("search-api-key").value = ""; $("search-api-key").type = "password";
  $("toggle-search-key").setAttribute("aria-pressed", "false");
  $("toggle-search-key").setAttribute("aria-label", "显示搜索 API Key");
  updateSettingHints(); updateSearchKeyHint(); formError("settings-error", "");
  $("settings-dialog").showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  const button = $("save-settings");
  if (button.disabled) return;
  const payload = {
    provider: $("provider").value, model: $("model").value.trim(),
    api_key: $("api-key").value.trim(), base_url: $("base-url").value.trim(),
    max_steps: $("max-steps").value === "unlimited" ? null : Number($("max-steps").value),
    search_api_key: state.searchKeyRemoved ? null : $("search-api-key").value.trim(),
  };
  const controls = [...$("settings-form").querySelectorAll("input, select, button")];
  const disabledBefore = controls.map((control) => control.disabled);
  controls.forEach((control) => { control.disabled = true; });
  button.textContent = "正在保存…"; formError("settings-error", "");
  const preserved = { text: $("message-input").value, attachments: [...state.attachments] };
  try {
    const result = await api("/api/config", payload);
    state.config = result.config; $("api-key").value = ""; $("search-api-key").value = "";
    $("settings-dialog").close();
    newChat();
    $("message-input").value = preserved.text; state.attachments = preserved.attachments;
    resizeInput(); renderAttachments(); saveDraft(); renderConfig();
    toast("设置已保存，新对话将使用这份配置。");
  } catch (error) { formError("settings-error", error.message); }
  finally {
    controls.forEach((control, index) => { control.disabled = disabledBefore[index]; });
    button.textContent = "保存设置";
  }
}

async function openFiles() {
  if (busy()) return;
  formError("files-error", "");
  renderFiles(); $("files-dialog").showModal();
  try { const result = await api("/api/files"); state.files = result.files; renderFiles(); }
  catch (error) { formError("files-error", error.message); }
}

async function uploadFiles() {
  if (busy()) return;
  const files = [...$("file-input").files];
  if (!files.length) return;
  state.uploading = true; renderRun();
  formError("files-error", "");
  const errors = [];
  let count = 0;
  for (const file of files) {
    try {
      if (!/\.(txt|md|json|csv)$/i.test(file.name)) throw new Error(`${file.name}：只支持 TXT、MD、JSON、CSV。`);
      if (file.size > 2 * 1024 * 1024) throw new Error(`${file.name}：文件不能超过 2 MiB。`);
      let content;
      try { content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer()); }
      catch { throw new Error(`${file.name}：请转换为 UTF-8 编码后上传。`); }
      const result = await api("/api/files", { name: file.name, content });
      state.files.push(result.file); attach(result.file.name); count++;
    } catch (error) { errors.push(error.message); }
  }
  state.files.sort((a, b) => a.name.localeCompare(b.name));
  renderFiles(); $("file-input").value = "";
  state.uploading = false; renderRun();
  if (errors.length) formError("files-error", errors.join("\n"));
  if (count) toast(`已上传并添加 ${count} 个文件`);
}

function bindEvents() {
  $("new-chat").addEventListener("click", newChat);
  $("open-settings").addEventListener("click", openSettings);
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-dialog").addEventListener("close", () => {
    $("api-key").value = ""; $("search-api-key").value = ""; state.searchKeyRemoved = false;
  });
  $("settings-dialog").addEventListener("cancel", (event) => {
    if ($("save-settings").disabled) event.preventDefault();
  });
  $("provider").addEventListener("change", updateSettingHints);
  $("base-url").addEventListener("input", updateSettingHints);
  $("toggle-key").addEventListener("click", () => {
    const show = $("api-key").type === "password";
    $("api-key").type = show ? "text" : "password";
    $("toggle-key").setAttribute("aria-label", show ? "隐藏 API Key" : "显示 API Key");
    $("toggle-key").setAttribute("aria-pressed", String(show));
  });
  $("toggle-search-key").addEventListener("click", () => {
    const show = $("search-api-key").type === "password";
    $("search-api-key").type = show ? "text" : "password";
    $("toggle-search-key").setAttribute("aria-label", show ? "隐藏搜索 API Key" : "显示搜索 API Key");
    $("toggle-search-key").setAttribute("aria-pressed", String(show));
  });
  $("clear-search-key").addEventListener("click", () => {
    state.searchKeyRemoved = !state.searchKeyRemoved;
    $("search-api-key").value = "";
    updateSearchKeyHint();
  });
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => $(button.dataset.close).close());
  });
  document.querySelectorAll("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target !== dialog) return;
      if (dialog.id === "settings-dialog" && $("save-settings").disabled) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  });
  $("message-form").addEventListener("submit", sendMessage);
  $("message-input").addEventListener("input", () => { resizeInput(); saveDraft(); });
  $("message-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (!active(state.session?.run)) $("message-form").requestSubmit();
    }
  });
  $("open-files").addEventListener("click", openFiles);
  $("open-tools").addEventListener("click", openTools);
  $("refresh-tools").addEventListener("click", refreshTools);
  $("attach-file").addEventListener("click", openFiles);
  $("summarize-file").addEventListener("click", () => {
    if (!$("message-input").value) $("message-input").value = "请总结这份文件的主要内容和关键结论。";
    saveDraft(); openFiles();
  });
  $("create-tool-prompt").addEventListener("click", () => {
    if (!$("message-input").value) $("message-input").value = "帮我创建一个文本统计工具：输入一段文本，返回字符数和行数。请先测试，再保存。";
    $("message-input").focus(); resizeInput(); saveDraft();
  });
  $("search-web-prompt").addEventListener("click", () => {
    if (!$("message-input").value) $("message-input").value = "请搜索这个主题的最新资料，并附上来源链接：";
    $("message-input").focus(); resizeInput(); saveDraft();
    if (!(state.session?.search_enabled ?? state.config.has_search_api_key)) {
      toast("在模型设置中填入 DeepSeek 搜索 API Key 后可启用搜索。");
    }
  });
  $("upload-files").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", uploadFiles);
  $("open-trace").addEventListener("click", () => { $("trace-dialog").showModal(); renderTrace(true); });
}

async function initialize() {
  bindEvents(); renderSessions(); renderRun();
  try {
    const result = await api("/api/bootstrap");
    state.config = result.config; state.sessions = result.sessions; state.files = result.files;
    state.initialized = true; renderConfig(); renderSessions(); renderFiles(); renderRun();
    if (state.sessions.length) await selectSession(state.sessions[0].id);
  } catch (error) { connectionError(error.message); }
}

initialize();
