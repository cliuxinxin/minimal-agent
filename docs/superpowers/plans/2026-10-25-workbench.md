# Persistent Agent Workbench Implementation Plan

> **For agentic workers:** Use the task briefs below; each owner implements and verifies its independent files, followed by integration review.

**Goal:** Deliver all six approved optimization directions in v0.5.0.

**Architecture:** Keep the existing Python Agent and loopback web host. Add local persistence, diagnostics, artifacts and context management; extend the existing assistant-ui presentation.

**Tech Stack:** Python 3.11+, Pydantic, sqlite3, React, assistant-ui, esbuild.

**Spec:** docs/superpowers/specs/2026-10-25-workbench-design.md

## Global Constraints

- Python 3.11+, standard library and existing Pydantic only for the backend.
- Preserve standard provider APIs, native history, cancellation and SSE behavior.
- No real credentials or paid model calls during automated verification.
- Existing local config/data remain outside Git; publish only source, tests, documentation and prebuilt assets.

## Review Focus

- Interrupted side effects must not be replayed on restore; persistence tests must exercise unresolved calls.
- Provider or endpoint changes must not send saved history/credentials to a different destination.
- Artifact URLs and previews must not expose arbitrary local files or execute generated markup.
- Compaction must not split native tool calls/results or lose signed blocks in retained turns.
- UI requests finishing after a conversation change must not update the wrong conversation.

## Tasks

- [x] Runner/model owner: usage, context compaction, checkpoint boundaries and per-run tool context. Brief: workbench-runner-brief.md.
- [x] Tools owner: artifact registry, saved-tool lifecycle/schema/tests and per-run source metadata/cache. Brief: workbench-tools-brief.md.
- [x] Frontend owner: persistence/recovery UI, diagnostics controls, artifact/source cards, metrics and tool management. Brief: workbench-ui-brief.md.
- [x] Host owner: SQLite session storage, compatible lazy resume, safe interruption repair, draft connection checks, API integration and launcher port registration.
- [x] Integration: focused regression tests, complete backend/frontend suites, build and desktop browser checks.
- [x] Release preparation: inspect diff against v0.4.2, fix concrete issues, update docs/version, and prepare the checked source/assets for a non-forced main update with an expected head.

Each task begins with meaningful failing regressions, implements the smallest complete behavior, runs the focused checks, and records results. Parallel owners have disjoint file boundaries; integration edits are coordinated through messages.

## Verification Record

- Backend: 269 tests passed, including provider history, unresolved side effects, accepted-task recovery, external-owner refresh, revision conflicts, tool lifecycle and artifacts.
- Frontend: 28 regression tests passed; TypeScript checks, app.js syntax check and the production build passed. Prebuilt asset hashes match the build manifest.
- Desktop browser: 29 checks passed at 1440 and 1024 pixel widths. The actual host, SSE transport, runner, Python subprocesses, SQLite, file download and tool lifecycle were exercised against local provider-boundary fixtures.
- Actual host restart preserved sessions, native history, files and sources. A forced host exit after a Python side effect restored an unknown-outcome tool result; explicit continuation completed without replaying that side effect.
- Independent integration review found and resolved two recovery issues: stale external-owner state and a task accepted before its worker started. No real provider credentials or paid requests were used for verification.

Publication uses GitHub Git data APIs because this workspace is a recovered source directory. The uploaded tree must match the local blob manifest, retain existing executable modes, exclude user configuration/data, and update main only while its expected parent is unchanged. The resulting commit on main is the delivery record.
