# Goals and Live Steering Implementation Plan

> **For agentic workers:** Use subagent-driven-development with disjoint owned files; root integrates and publishes after verification.

**Goal:** Make citations readable and let the user inspect and correct the agent's goal, plan and evidence during execution.

**Architecture:** Extend the existing single-agent native tool loop with public work-plan state and durable queued direction updates. Reuse existing SSE, SQLite and assistant-ui.

**Tech Stack:** Python 3.11+, standard library/Pydantic, React, assistant-ui, esbuild.

**Spec:** docs/superpowers/specs/2026-10-26-goals-and-steering-design.md

## Global Constraints

- Preserve current standard provider APIs, existing configuration, native history, cancellation and local data.
- No paid provider calls or real credentials in verification.
- Public concise planning/judgment is not a claim to expose private reasoning or prove semantic correctness.
- Keep desktop UI and ship prebuilt assets; publish no local configuration or user data.

## Review Focus

- Pending corrections must not split native tool call/results, disappear at final-answer races, or replay after restart.
- A running tool may finish before correction applies; skipped remaining calls must clearly be not executed.
- A model plan/self-check must not appear as host-verified business success.
- Citation mapping must require matching IDs and URLs and preserve code/literal text in readable copies.
- Stale async frontend responses must not overwrite another session/run or erase the user's draft.

## Tasks

- [x] Runner owner: add miniagent/planning.py, optional planning and pending-input handling in agent.py, with provider/cancellation/race regressions in tests/test_goal_steering.py. Follow exact spec contracts; initial failing tests, implementation, focused suite.
- [x] Frontend owner: readable source labels/cards/copy; typed work-plan/steering metadata and live correction composer; frontend regressions, types and production assets. Own frontend/** and web/app.js/index.html/style.css.
- [x] Host owner: enable planning in config; persist/restore plans and applied input IDs; implement durable idempotent steer API, atomic final-input close and SSE presentation; add tests/test_steering_web.py and recovery cases. The existing JSON-backed SQLite schema required no migration.
- [x] Integration: independent race review, full backend/frontend tests, build, browser checks for visible planning/numbered citations/correction and restart without duplicate effects.
- [x] Release preparation: document actual behavior/boundaries, bump v0.6.0 and prepare source and built assets for verified blob-tree publication with an expected previous head.

## Verification record

- Complete backend suite: 297 tests passed. New runner coverage includes both native provider histories, cancellation, final-answer corrections, plan compaction, and repeated-plan loop protection. Host coverage includes durable/idempotent input, failed storage, redaction, restoration and chronological accepted-task recovery.
- Frontend: 41 tests passed, TypeScript check and production build passed. Shipped assets match the build manifest.
- Desktop browser: 18 OpenAI-compatible HTTP/SSE checks and 18 native Anthropic Messages SSE checks passed, including real Python tools and SQLite. Both providers passed in-flight correction, skipped old action, final-response correction, numbered citations/readable copy and actual host restart. 1440/1024 desktop layouts had no overflow or uncaught JavaScript errors.
- Independent review resolved the accepted-task/old-input recovery order and unchanged-plan repetition issues. No real credentials or paid provider requests were used. Test fixtures remain outside the product.

The published GitHub commit is the delivery record. Publication checks every blob and file mode against this verified source tree and updates main without force only if its expected parent remains unchanged.
