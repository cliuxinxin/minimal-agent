# Programmable Tools Implementation Plan

> **For agentic workers:** Implement the independent tasks below in parallel, then review the integrated application. Steps use checkbox syntax for tracking.

**Goal:** Add persistent Python tools, longer controllable agent runs, and official DeepSeek web search.

**Architecture:** Keep the native tool loop, local stdlib HTTP server, and vanilla desktop UI. Use an atomic on-disk tool registry and subprocess execution. Add DeepSeek retrieval through native tool definitions.

**Tech Stack:** Python 3.11+, Pydantic 2, HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-24-programmable-tools-design.md`

## Global Constraints

- Keep OpenAI and Anthropic provider contracts and existing config files compatible.
- No offline application mode, mobile layout, or new runtime dependency.
- Persist local secrets, uploads, logs, and generated tools outside tracked source.
- Code comments are English; user interface and instructions are Chinese.

## Review Focus

- A failed generated-tool test must not replace an existing tool.
- Saving model settings must not erase an omitted or masked search key.
- Stopping a Python subprocess must leave native model history valid.
- Legitimate changing results must not trigger the repeated-result guard.
- Tool source and web results must render as text and safe HTTP(S) links.

## Task 1: Programming runtime

**Files:** `miniagent/programming.py`, `miniagent/python_worker.py`, `tests/test_programming.py`.

**Interface:** `ProgrammingTools(root, workspace, timeout_seconds=60)` exposes `builtin_tools()`, `load_tools()`, `summaries()`, `errors()`, and `approve(tool, arguments)`.

- [x] Add failing tests for script execution, registration after a passing example, persistence, invalid/duplicate manifests, timeout, cancellation, and output bounds.
- [x] Implement a child-process worker and atomic JSON tool manifests.
- [x] Run focused tests and report results.

## Task 2: Agent limits and registry

**Files:** `miniagent/agent.py`, `miniagent/tools.py`, `tests/test_agent.py`.

**Interface:** `Agent(..., max_steps=80, dynamic_tools=None)` accepts `None` for unlimited requests. `tool_execution_context(should_stop)` and `execution_stopped()` pass cancellation to tools.

- [x] Add failing tests for runs beyond eight requests, explicit/unlimited limits, repeated failure/result guards, dynamic definitions, and cancellation context.
- [x] Implement refreshed definitions, `stalled` outcomes, and cancellation context.
- [x] Run focused tests and report results.

## Task 3: DeepSeek retrieval

**Files:** `miniagent/search.py`, `tests/test_search.py`.

**Interface:** `build_search_tools(api_key)` supplies `web_search` and `fetch_webpage`.

- [x] Add failing contract tests for official payload/auth, empty results, errors, safe links, pagination, and response limits.
- [x] Implement fixed official endpoints with sanitized errors and bounded responses.
- [x] Run focused tests and report results.

## Task 4: Configuration, UI, and integration

**Files:** `miniagent/config.py`, `miniagent/web.py`, `miniagent/builtin_tools.py`, `web/*`, root docs and integration tests.

- [x] Test compatibility, saved-key reuse/removal, allowed programming tools, and secret redaction before changing behavior.
- [x] Remove the calculator, assemble new tool providers, add masked settings, and expose tools inventory.
- [x] Add desktop settings and tools drawer without changing existing send/stop/session guards.
- [x] Update the README, extension guide, ignore rules, and version.
- [x] Run the final full test suite, exercise the rendered UI, review the diff, and prepare the verified GitHub commit.

## Verification evidence

- Desktop browser: 17 interaction and rendering checks passed at 1440x1000 and 1024x768; no console/runtime errors.
- Independent review repros for stdout/result isolation, host-exit cleanup, UTF-8, redaction, and trace bounds passed.
- Live provider credentials and native Windows/macOS launch execution were not available in this environment.
