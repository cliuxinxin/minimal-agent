# Streaming Conversation Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans for the independent tasks below.

**Goal:** Stream real model output and visible agent work into the desktop conversation with safe Markdown rendering.

**Architecture:** Native provider streams feed the existing agent loop. The local server owns canonical messages and a bounded SSE journal; assistant-ui renders the ordered conversation through an ExternalStoreRuntime adapter.

**Tech Stack:** Python 3.11+, Pydantic, standard-library HTTP/SSE, browser EventSource, React and assistant-ui in prebuilt local assets.

**Spec:** `docs/superpowers/specs/2026-10-24-streaming-conversation-design.md`

## Global Constraints

- Keep standard OpenAI Chat Completions and Anthropic Messages, existing settings and double-click startup.
- No Node.js runtime, CDN requirement, offline app mode or new mandatory setting.
- Existing user authorization includes updating GitHub main after verification.
- Keep credentials, generated tools, workspace data and run logs out of the repository.

## Review Focus

- Incomplete native streams must never execute tool calls.
- A stop during a blocked network read must finish promptly and retain visible partial output.
- SSE replay must not repeat side effects or append duplicate text.
- Credentials split across chunks and opaque provider signatures must not reach the browser.
- Partial Markdown must become correct final Markdown without losing scroll position or session isolation.

## Task 1: Provider streaming

**Files:** `miniagent/model.py`, new `miniagent/sse.py`, `tests/test_model_streaming.py`.

**Interface:** `stream(messages, tools, system_prompt, *, on_delta, should_stop=None) -> ModelReply`; callback types are specified in the design.

- [x] Write and reproduce tests for early text/tool/reasoning deltas, interrupted termination, native history, parser bounds and blocked-read cancellation.
- [x] Implement bounded native SSE parsing and response assembly with sanitized failures.
- [x] Run the provider tests against real local HTTP fixtures.

## Task 2: Agent and host

**Files:** `miniagent/agent.py`, `miniagent/config.py`, `miniagent/web.py`, `tests/test_agent_streaming.py`, `tests/test_web_streaming.py`.

**Interfaces:** Core streaming opt-in and `model_complete`; the five named browser SSE events and segment schema in the design.

- [x] Reproduce failures for early core observations, unexecuted incomplete tools, native history and cancellation.
- [x] Add the agent streaming path with nonstream compatibility.
- [x] Reproduce failures for inline canonical messages, journal replay/gaps, heartbeat, disconnect and split-key redaction.
- [x] Implement the placeholder, bounded segments, safe event journal and same-origin SSE endpoint.
- [x] Run core, host and existing configuration regression tests.

## Task 3: Frontend and delivery

**Files:** `web/app.js`, `web/index.html`, `web/style.css`, `web/assets/`, `frontend/`, README and extension guide.

**Interface:** EventSource consumes the host contract and the assistant-ui runtime receives text, reasoning and server-owned tool-call parts.

- [x] Replace polling with EventSource, retaining session revision and task lifecycle guards.
- [x] Connect official assistant-ui conversation, reasoning, tool and Markdown components to the existing SSE state.
- [x] Keep inline progress, tool details, real provider reasoning and partial output on stop/error.
- [x] Ship pinned dependency lockfiles, upstream component provenance, licenses and reproducible prebuilt local assets.
- [x] Verify incremental output, Markdown, XSS defenses, reconnection, stopping and desktop layouts in a real browser.
- [x] Review the integrated changes, run the complete Python suite and update version/documentation.
- [ ] Compare the remote main SHA, upload only intended files, fast-forward main and verify the resulting commit/tree.

## Validation record

- Complete Python suite: 178 tests passed.
- Frontend production adapter and stop regressions: 8 tests passed; TypeScript check passed.
- Desktop browser integration: 15 end-to-end checks plus a focused no-output error check passed, with no browser console/runtime errors.
- Rebuilding the committed frontend sources reproduced the JS and CSS SHA-256 hashes.
- Independent backend and frontend reviews approved their accepted fixes. No paid provider request was made.
