---
name: navi
description: Use when the user asks Navi to track task scope, record task interpretations or unrelated findings, run an independent scope review, produce a task evidence report, or preview and perform a user-selected rollback. Experimental local Codex integration.
---

# Navi

Use the plugin's `../../scripts/task_state.py` and `../../scripts/review.py` relative to this skill directory.
Use the host-provided PLUGIN_DATA or an explicitly supplied absolute data directory and actual session ID.
Read [the interface](references/interface.md) only for the operation needed.

“Use Navi to supervise this task” is sufficient activation: supervise the task currently requested,
including its relevant requirements already stated in this conversation. Do not ask the user to repeat
scope or add a history-reading phrase merely because capture was just enabled. This authorizes bounded
same-conversation context import and the normal task observation/review setup; respect explicit limits
on history, capture, models or usage. Never scan other conversations or invent requirements.

Enable input capture once for the exact workspace, then run `history.py bootstrap --workspace ABS`
with the actual data/session flags (see the interface). The next native hook supplies the current turn
and transcript; a detached local reader validates and integrates original sources without a history-model
call. Continue useful work, inspect history readiness at a normal checkpoint, then enable task action
capture and automatic supervision. Prefer establishing watched-file baselines before edits. A task shell
or pending import is not active supervision. No activating-message replay or synthesized hook is needed.
The command preserves existing task configuration, budgets and observations. For an already integrated
task, continue its existing setup. If history is explicitly disallowed or genuinely unavailable, disclose
the specific gap; ask for missing scope only if validated current requirements cannot be obtained.

For a separately requested history interpretation of this same conversation, use the optional history
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
For explicitly requested long-task supervision use `--profile long-task`: at least three minutes between
automatic dispatches. Standard mode uses 30 seconds. There are no hourly review/dispatch caps or default
cumulative quotas. New eligible evidence is still required. One reviewer at a time per conversation includes
manual reviews and automatic followups; other conversations have independent slots. If a review is already
queued/running, continue the task; a deferred manual request is not automatically queued. Automatic reviews
resume at a later eligible native event once the slot is free and spacing permits. Do not additionally
submit routine manual reviews, wait for evaluators, or self-check each tool.
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
A loaded skill or successful workspace opt-in alone does not prove active supervision. On first activation,
use the bootstrap path above instead of routinely requesting another user message. If its reader is pending,
let the next root-session native event dispatch it; failed local reads may be retried after the stated cause
is repaired. Do not bypass transcript identity/provenance checks or treat partial history as complete scope.
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

At the user's reporting boundary, run `../../scripts/report.py` with the actual data/session flags and
`--format brief`, as documented in the interface. Paste the returned Navi summary verbatim as its own section
in the user response. When preparing to declare the current task complete, add `--final` once: it waits
up to 15 seconds for reviews already queued/running or an already-dispatched worker. Progress reports,
phase handoffs and waiting for user continuation use the ordinary command without `--final`. Do not
submit a new review just for reporting, repeatedly call `--final` to extend the wait, or wait indefinitely.
Returned findings still need freshness checks; a finished review may cover an older snapshot. Disclose
unresolved findings, including missing/substituted delivery, in the Navi report; no automatic rollback or
forced repair loop. A timeout does not prevent the task report. Do not replace it with your own status sentence, translate/recalculate its counts,
or describe retained/cumulative records as this turn's results. This report call neither runs a model nor
schedules reviews. The JSON report includes the identical text in `user_summary`; Markdown remains the
full evidence report. Explain task results outside the Navi section. If report generation fails, state
that the Navi report is unavailable and include the actual error type; do not invent a replacement status.
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
