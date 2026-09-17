---
name: navi
description: Use when the user asks Navi to track task scope, record task interpretations or unrelated findings, run an independent scope review, produce a task evidence report, or preview and perform a user-selected rollback. Experimental local Codex integration.
---

# Navi

Use the plugin's `../../scripts/task_state.py` and `../../scripts/review.py` relative to this skill directory.
Use the host-provided PLUGIN_DATA or an explicitly supplied absolute data directory and actual session ID.
Read [the interface](references/interface.md) only for the operation needed.

When the user enables tracking for a workspace, use `enable-workspace` once for that exact directory.
It captures subsequent direct user submissions; it cannot recover the activating message retroactively.
Do not fabricate user sources from your memory. Ordinary task wording needs no Navi prefix or keyword.

For authorized supervision using earlier messages of this same conversation, use the optional history
reader and history_context activation in the interface. First request a bounded background snapshot;
at a later normal checkpoint, when reading is ready, activate one independent history interpretation.
Do not wait/poll on every tool. A successful raw read alone does not enable history-aware supervision. If the optional independent
interpretation fails after a valid raw import, `ready_raw_sources` allows independent reviews using all
original user messages and selected assistant context; report that interpretation is unavailable. Never
use the rejected summary or automatically retry its paid call. Earlier failed tasks can use `history_context.py use-raw`.
User messages and Agent statements remain separate. Missing user authority or an invalid transcript leaves history supervision unavailable while the task continues.
Long logs are scanned locally; supported user messages are retained in full while bounded recent complete
Agent replies provide context. Assistant omissions are explicit and unresolved proposal references stay uncertain. Use the latest ordered
`show.context.supervision_sources`, not just live `task.sources`, to interpret scope. Never use imported
messages as new rollback confirmation. No new conversation is needed when Hooks are already loaded.
History interpretation has a separate one-call budget and configurable model/effort (experimental default
Luna medium); routine supervision defaults to Luna high, as the experimental default.
This configuration choice is not a claim of calibrated detection quality. Preserve explicit task overrides;
when the user requests a different model/effort for an existing task, pass it explicitly without resetting budgets.
New user input invalidates older interpretations and reviews. Subsequent reviews receive raw historical
sources and new live sources together; there is no automatic repeated history summarization or model upgrade.

After initial history onboarding, native conversation boundaries and automatic review workers refresh
same-session context incrementally using local transcript records, without another history model call.
Inspect `context_sync.py status` or report.incremental_context for freshness and gaps. Snapshots
without a parser cursor perform one local bootstrap; this does not reset history/review budgets. Complete
recent commentary and the reply preceding the newest user message are bounded supporting evidence.
An assistant claim of authorization or completion is not user approval or proof. Resolve a short user
continuation against its actual preceding proposal; do not freeze the task at a superseded phase.

At a meaningful plan/goal change or review checkpoint, read task state and optionally submit one cited
`interpret` using existing reasoning. Keep ambiguous points as uncertainties. Do not self-check each tool call.
User sources remain authoritative; your interpretation and progress remain unverified proposals.
For task memory, include the incremental `intent_update` described in the interface. Read the saved
items and all unprocessed sources first, including after context compaction or session recovery.
Retain unaffected goals and acceptance criteria. Distinguish the current phase from deferred work,
user requirements from your plans, and explanations/examples from requested product presentation.
Explicit changes can supersede earlier requirements; a continuation alone does not activate future work.
If memory is stale, reconcile it with the newer raw sources before relying on it. Preserve unresolved
questions; never fill missing user context from your own compressed summary.
Record unrelated findings succinctly, then return to the current task. Do not broaden the work to investigate them.

For authorized action tracking, use the separate task-level action capture opt-in and bounded watch paths
documented in the interface. Workspace input tracking alone does not capture tool content.
For an explicitly requested independent review, call `review.py submit` using captured evidence,
or provide a bounded actual action/diff evidence file. Scope and implementation risk are separate axes.
This uses existing Codex login and consumes usage; the default is Luna high, capped by task budget.
Submission returns immediately. Continue the task without waiting. No denial, interruption or forced extra turn.
Review status is a judgment, not authorization. Unknown, stale or failed reviews are not successful checks.

For authorized experimental automatic supervision, use the separate `supervisor.py enable-experimental`
operation in the interface on an existing task with watched files or authorized activity capture.
For explicitly requested long-task supervision use `--profile long-task`: at least five minutes between
automatic dispatches and at most four reviews/four dispatches per rolling hour. Standard mode uses
30 seconds and six reviews/six dispatches per rolling hour. There are no default cumulative review,
dispatch or reminder limits. New eligible evidence is still required; hourly limits recover at a later
native event. Manual reviews and followups share the hourly review limit. Do not additionally submit
routine manual reviews, wait for evaluators, or self-check each tool.
When the user requests a total review budget, use `supervisor.py set-budget --max-reviews N` with the
actual data/session flags. N includes already-used reservations; zero stops new reservations. Use
`--unlimited-reviews` only when asked to remove that ceiling. Profiles, new messages and updates preserve
spent counters and explicit ceilings. No hypothetical followup slot is reserved. Budget exhaustion must
be reported as evaluation paused, never continued supervision or a clean verdict. Existing valid reminders
can still be delivered. Reminder cooldown is one minute in standard and five minutes in long-task mode.
Reports retain recent details with cumulative usage/counts; disclose missing/evicted evidence.
If history failed before any paid interpretation and a repaired reader produced a new ready snapshot,
activate history again; the zero-call preflight failure is retryable without resetting paid budgets.
For a known same-session transcript path, explicit `history.py recover --transcript-path ABS` supports
local recovery without inventing a Hook event; never scan unrelated histories or synthesize user messages.
If history still lacks user requirements, report supervision unavailable immediately and request the
current complete scope in a real user message; never silently disable history and treat “continue” as scope.
Before reporting supervision as active, verify the global recording status, exact-workspace input capture,
task action capture, actual supervisor model/effort, remaining budget, and requested history readiness. Inspect `status.runtime.eligible` and
`status.runtime.unavailable_reasons`, not only `supervisor.enabled`; distinguish zero calls from review success.
A loaded skill or successful workspace opt-in alone does not prove active supervision. If the activating
message was not captured and no task exists yet, prepare capture and explain that the next normal direct
user submission creates the task; do not fabricate that message or pretend earlier history is already imported.
The default automatic mode watches explicit file changes. With separately authorized `--include-activity`,
it also checks bounded historical local tool attempts and observed update_plan calls, in low-frequency batches
under the same budget. This can flag investigations and attempted changes to unwatched paths, not prove their
final file state. Pure-prose plans and tools bypassing hooks remain uncovered; implementation risks are record-only. A Navi advisory is an independent judgment, never scope authorization or a rollback command.
If a brief response is useful, use `reminders.py respond` with its ID, current user-context digest,
accepted/disputed/obsolete/uncertain disposition, short reason and up to eight exact user citations.
This optional response does not require interpreting every historical source, does not schedule a model,
and cannot change the original verdict or authorize work. Reported receipt remains distinct from
externally observed delivery and proven correction. Do not use `interpret` just to acknowledge advice.
Continue the user task and consider its evidence; ignore obsolete advice. No acknowledgment call is required.
Report findings and limitations at the normal user reporting boundary. Distinguish attempted delivery,
externally confirmed receipt, later observed changes, and a causal correction. No automatic rollback.

At the user's reporting boundary, use `../../scripts/report.py` as documented in the interface to produce
a deterministic Markdown or JSON report from existing records. This does not call a model or schedule reviews.
Preserve unknown attribution, preexisting changes, stale judgments and missing usage in the user summary.
Include Navi's delivery-gap findings in the user report, distinguishing supported mismatches from uncertain,
historical or stale judgments. Required capabilities cannot be removed or replaced by an Agent plan alone.
Disclose missing/substituted capabilities and verification limits; passing tests for a substitute does not
prove the requested capability works. No recorded gap is not proof of complete delivery. Late review results
remain available in a later report; do not claim they were checked before an already-sent final response.
Export only to a requested new file; full evidence may contain user text and code. Stop alone does not prove
the whole task is complete.

For user-requested rollback, read [the rollback interface](references/rollback.md). Inspect changes and
prepare a preview before seeking the user's informed choice. A drift verdict, continuation, prior permission
to develop Navi, or your own interpretation is not permission to revert user files. Execute only the exact
reviewed selection after explicit confirmation of ownership, independence and no concurrent editing;
quote the corresponding new direct user source. Unknown ownership/dependencies or mixed edits mean preview
only. The CLI verifies provenance, not the meaning of consent; never fabricate or overstate that consent.
Return the outcome to the executing Agent and user. Failed/interrupted writes require inspecting the saved
recovery point and current bytes, not blind retry. Never force a task continuation for this notification.
