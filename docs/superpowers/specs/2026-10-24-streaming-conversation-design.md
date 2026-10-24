# Streaming conversation design

## Goal

Show real model output and agent work inside the desktop conversation as it happens. Preserve the existing Python startup, provider settings, local data and extensible tool loop.

## Architecture

OpenAI Chat Completions and Anthropic Messages stream through their native SSE protocols. Their adapters assemble a valid native reply while emitting only public text, provider-returned reasoning, and tool argument deltas. The agent emits these observations immediately but executes tools only after the provider has finished a valid reply. Opaque thinking signatures remain in provider history and never enter the browser.

The local server owns each background run independently of the browser connection. It appends one assistant placeholder, updates its ordered segments, and publishes same-origin SSE events. Reconnection replays a bounded journal or sends a canonical snapshot; it never repeats model requests or tool execution.

The existing shell uses EventSource to maintain canonical state. Per the user's explicit follow-up, the conversation uses the mature assistant-ui React runtime and official conversation, reasoning, tool and Markdown components. An ExternalStoreRuntime adapter maps the existing SSE state without duplicating server-side execution. Pinned source dependencies and reproducible build files are included; prebuilt browser assets keep normal use free of Node.js installation, frontend builds and CDN connections.

## Interfaces

- `Agent(..., stream=False)` and `load_agent(..., stream=False)` retain the current nonstream defaults. The web host opts into streaming.
- Provider `stream(messages, tools, system_prompt, *, on_delta, should_stop=None) -> ModelReply` emits `text_delta`, `reasoning_delta`, and `tool_call_delta` dictionaries.
- Core events add those delta types and `model_complete` with `text` and `has_tool_calls`; existing events remain.
- `GET /api/sessions/{id}/events` supports `Last-Event-ID` and `?after=`. Named events are `snapshot`, `run_status`, `segment`, `segment_delta`, and `trace`.
- Each assistant message has `run_id`, `status`, `content`, ordered `segments`, and `segments_omitted`. Text/reasoning segments have stable IDs; tool segments expose preparing/running/completed/error/cancelled states.

## Bounds and behavior

- Keep the latest 512 SSE journal records, 100 coarse trace events and 80 visible segments per run. Limit each text/reasoning segment to 64 KiB and show explicit truncation. Native provider history remains intact.
- Redact credentials across fragment boundaries as well as in complete values. Do not expose model-output payloads, encrypted thinking or signatures.
- Emit SSE heartbeats at most 10 seconds apart. Never hold the session lock while writing to the network.
- Disconnecting the page does not cancel work. Stopping cancels the active streaming read and Python work; partial visible output remains marked incomplete.
- Do not request a universal thinking mode unsupported by some models. Display only actual provider-returned content.
- Preserve desktop layouts at 1024×768 and 1440×1000, configuration persistence, uploads and dynamically created tools.
- Use actual assistant-ui components for the conversation; keep custom code to backend adaptation, localization and application styling. Do not activate client-side tool execution.

## Acceptance

Verify early delivery through real local HTTP streaming, complete native tool history, cancellation during blocked reads, replay without repeated side effects, split-key redaction, Markdown tables/nested lists/code, safe rendered HTML, stale-session guards and the existing regression suite. Test fixtures stay outside the application runtime; paid-provider validation requires user credentials.
