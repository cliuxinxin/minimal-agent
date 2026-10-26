# Goals, readable sources and live steering

## Intent

The user cannot understand hash-shaped source labels and wants an agent whose objective, public judgment, evidence and progress can be inspected and corrected while it works. Extend the existing desktop workbench and standard OpenAI/Anthropic tool protocols. Keep setup and dependencies unchanged.

## Chosen approach

Maintain one agent with a structured public work plan and a durable user-input queue. A model tool updates the goal, completion criteria, steps, concise rationale, evidence, uncertainties and self-check. Actual tools and their results remain the external feedback. User direction enters the native conversation only at complete tool-result boundaries. No extra reviewer model is automatically called at every step.

This is the implementer's choice under the user's continuing request to improve the existing project. A prompt-only plan would not provide durable visible state or reliable mid-run control; a separate planner/evaluator service would add configuration and cost beyond the current need. Simple direct answers may omit a planning tool call. Multi-step/tool tasks should first form a plan, update it after significant evidence or user feedback, and check the completion criteria before delivery.

## Public plan contract

Tool name: `update_work_plan`. Agent constructor accepts `planning=False`; application assembly enables it. No user config option is added. The tool is internal bookkeeping, read-only with respect to external files/services.

`Agent.work_plan` is null or a JSON object with:

- `goal`: 1–500 characters.
- `success_criteria`: 1–6 strings, at most 300 characters each.
- `steps`: 1–8 objects with `title` (1–200 characters) and `status` (`pending`, `in_progress`, `completed`). At most one step is in progress.
- `summary`: up to 1200 characters of public status/judgment explaining observations and the next action, not hidden reasoning.
- `evidence`: at most 8 strings of up to 400 characters, pointing to observed source IDs, files or tool/test results.
- `uncertainties`: at most 6 strings of up to 300 characters.
- `verification`: `{status: not_checked|needs_work|passed, summary: string <=1200}`. This is explicitly model self-check, not a host certificate of correctness.
- `revision` and `updated_at`: assigned by the host tool implementation, not model arguments.

Emit `work_plan` with the object under `data.plan`, then a checkpoint. Persist current plan and attach a snapshot to the current assistant message. Include the latest bounded plan as working-state data in subsequent model requests so compaction does not silently discard the goal. A new task may replace the previous plan; previous message snapshots remain unchanged.

## Mid-run direction contract

`POST /api/sessions/{sid}/steer` accepts `{run_id, input_id, message}`. Input ID is a client-generated 1–64 character identifier consisting of letters, digits, underscore or hyphen; text is 1–8000 characters. Same ID and text retry is idempotent. A different payload reusing an ID is a conflict. At most 10 pending inputs and 100 accepted inputs per run. Reject stale, stopping, external-owner or closed-for-input runs without losing the browser draft.

`detail.steering` contains `{id, run_id, message, status: pending|applied, created_at, applied_at?}` records. The current assistant message also receives that run's records. Accepted text is durable before HTTP success. HTTP never mutates the agent's native history.

`Agent.run(..., pending_input=None)` consumes a callback `pending_input(final=False)` returning `{id, message, before_task?}` records. Reading does not remove them. Previous-run pending entries have `before_task=true` on the initial read, so a newer request remains the latest user instruction. New corrections are appended after the current task. `Agent.applied_input_ids` is persisted with native history; application state and these updates share a reentrant state lock. Emit `input_applied` only after native user messages are appended; checkpoint atomically persists history and acknowledgement. Persisted IDs prevent replay after restart.

Check for corrections before model requests, after model replies, and before each tool in a batch. Complete the currently executing tool, skip remaining old-plan tools with an explicit `superseded_by_user` result, then append the correction. Preserve OpenAI and Anthropic call/result pairings. Skipped tools are shown as not executed. Stop takes priority and retains unapplied corrections for an explicit continuation.

Immediately before accepting a final answer, call `pending_input(final=True)`. Under the state lock, the host either returns pending input or closes this run to further input. This prevents accepting a correction that the completed run cannot consume. Inputs pending on an error/cancelled/interrupted run remain visible and are applied when the user continues. No automatic replay of old tools.

## Readable sources and desktop interaction

Keep internal source IDs and original provider-native content. For matching source ID plus actual URL, show stable per-message `[1]`, `[2]` labels, accessible title/domain tooltips, and numbered source cards. Unmatched IDs are visibly pending verification. Readable copy/export uses Markdown AST positions and never rewrites fenced/inline code or arbitrary URLs.

Show the current goal, criteria, current step, expandable public judgment/evidence/uncertainties and self-check in the existing desktop UI. During a run the composer submits a clearly labeled direction update; stopping is an independent button. Distinguish received/pending from applied-to-context. Preserve draft on failure and guard responses across run/session changes. Previous plan snapshots remain inspectable in their own assistant messages. No mobile design is required.

## Verification and boundaries

Use meaningful regressions for provider-native ordering, correction/final-answer races, cancellation priority, exact-once durable acknowledgements, stale-run/idempotent requests, plan persistence, readable citations/copy without code mutation, and UI draft preservation. Run existing suites/build and targeted desktop browser scenarios against local provider-boundary fixtures; no real keys or paid requests. Publish source, tests, docs and built assets to the already-authorized GitHub main using an expected-head update.

Course grounding: CS329Z Lecture 6 pages 20, 33–41, 47–50, 59 and 63; the course's assigned Anthropic Building effective agents article. These motivate explicit goals, environment feedback, dynamic replanning and inspectable event history. They do not establish that more self-talk alone improves correctness.
