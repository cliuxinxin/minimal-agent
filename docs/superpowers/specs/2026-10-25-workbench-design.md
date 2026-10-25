# Persistent Agent Workbench v0.5.0

## Intent and authorization

The user accepted all six optimization directions discussed on 2026-10-25 and asked to integrate them into the existing project. Continue the existing desktop web application and publish the completed, verified changes to the already-authorized GitHub repository. These are implementation choices, not new user configuration requirements.

## Constraints

- Python 3.11+, standard library and existing Pydantic only for the backend.
- Keep OpenAI Chat Completions and Anthropic Messages provider-native histories and streaming.
- Use the existing assistant-ui frontend and prebuilt assets. No runtime CDN or offline demo mode.
- Chinese interface and documentation; English code comments.
- Keep config.json, workspace/, local_tools/, runs/ and new data/ outside Git.
- Never replay a completed tool automatically during recovery, diagnostics, or transport retry.
- No credentials in browser storage, session config snapshots, exported diagnostics, or source control.

## Approach

Extend the current explicit Agent loop and loopback host with focused modules. A local SQLite store is sufficient for durable sessions; immutable local artifact copies provide stable downloads; small additions to existing drawers expose tool management and connection checks. A server database, new Agent framework, or desktop shell would add setup without solving the identified gaps.

## Features

1. Persist display messages, native model history, source/artifact references and run state in data/sessions.sqlite3. Restore completed conversations. Mark interrupted runs explicitly and allow a user-requested continuation after repairing unresolved native tool results with an unknown-outcome notice. Resume only with credentials belonging to the saved provider/endpoint; never silently switch providers. Keep view access when a credential is missing.
2. On-demand connection checks use the draft settings without saving. Model checks validate a real stream, a harmless echo tool call, and result continuation. Search is separately checked. Reports contain bounded classified errors and no response bodies or keys. Do not make external calls on page load.
3. Detect supported text/code files created or changed during Python/tool execution; archive immutable copies and show per-message artifact cards with preview/download and text diffs when a prior version is available. Constrain file access to archived artifacts and reject symlinks/traversal. No browser execution of generated HTML/JavaScript.
4. Preserve provider usage when available; display model/tool calls and elapsed time, unknown token counts honestly. Compact completed old conversation rounds above a conservative character threshold into a bounded model-generated working summary, preserve recent native turns and tool-result pairing, retain full source history in local checkpoints/logs. Automatic compaction requests count toward the selected request limit; a failed compaction is visible and cannot loop indefinitely.
5. Saved tools gain enable/disable, multiple example tests, optional explicit JSON input fields, previous-version retention, rollback, and a test action. Old manifests continue to load. Test failure preserves working code. Tests execute the existing Python worker, with its existing timeout/output bounds.
6. Record structured sources from actual search/fetch results, with title, URL, date and whether full text was read. Show source cards under the relevant answer. Make source IDs available to the model, link answer references to observed URLs, and flag unmatched reference markers without claiming semantic proof. Cache identical fetches only within one run and label cache hits.

## Recovery boundaries

Restart does not restore a Python process. An unresolved tool result is marked unknown; the agent is instructed to inspect files/state before deciding further actions. Saving chat text alone is insufficient: native histories and their provider identity must be persisted too. Persistence failure must be visible without stranding terminal SSE delivery.

## Verification

Use deterministic local provider fixtures only in tests. Cover restart/continuation, interrupted tools, endpoint changes, private-data redaction, immutable artifact downloads and path boundaries, disabled/rolled-back tools, schema and multi-case tests, usage merging, compaction pairing/limits, source mapping, and old-version startup regression. Verify actual rendered desktop flows with the built assets. Live provider checks remain an explicit user action in the application.
