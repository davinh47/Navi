# Local interfaces

Invoke scripts with Python 3.9+; no dependencies. Paths below are relative to plugin root.
Every command needs `--data-directory ABS`; task commands also need `--session ACTUAL_SESSION_ID`.

## First activation of the current task

A plain request to supervise the current task includes its relevant same-conversation requirements.
No extra history phrase or repeated scope is required unless the user restricts history access or the
actual evidence is unavailable. The commands below are run by the agent, not a prompt for the user:

```sh
python3 scripts/task_state.py enable-workspace --data-directory ABS --workspace ACTUAL_WORKSPACE
python3 scripts/history.py bootstrap --data-directory ABS --session ACTUAL_SESSION_ID --workspace ACTUAL_WORKSPACE
```

`bootstrap` creates an empty task shell only if needed. No user text argument is accepted. The next
root-session hook from that workspace binds the real turn and transcript. The detached reader requires
a complete supported snapshot containing exactly one canonical user message for that turn. It imports
ordered original user messages and bounded assistant context, keeping assistant plans non-authoritative.
It integrates `ready_raw_sources` directly, with no history interpretation model call. Existing tasks
without history can use the same route without resetting their contract, budgets, baselines or observations;
already requested/integrated history is preserved rather than restarted.

Use `history.py status` and `task_state.py show` at a normal setup checkpoint. Once ready, configure action
capture and automatic supervision as below. A real root tool boundary binds first-turn reminder delivery;
Stop/Interrupt still close delivery and never force a new turn. Missing/partial/wrong-session evidence
keeps supervision unavailable while the main task proceeds. A local reader failure can use `history.py retry`
after its cause is resolved; do not fabricate a submission or reset state. Imported sources never count as
fresh rollback consent. Same-conversation incremental synchronization then uses the existing cursor.

## Optional same-conversation history reading

With user authorization to read earlier messages of this same session, after input capture has created
an active task, run `python3 scripts/history.py request --data-directory ABS --session ACTUAL_SESSION_ID`.
The next root-session Hook supplies the transcript path and schedules bounded background reading.
No path guessing or scanning other conversations. `status` returns role-separated text records and
source positions, plus ready/partial/unavailable/failed/pending/reading/cancelled status. `ready` means
the available canonical text event prefix was read, not that the host retains all historical conversation.
No model call or automatic task interpretation occurs. History lives separately under task.history;
live sources, budgets, file baselines and review packets remain unchanged. This is reading access, not
completed history-aware supervision. A report includes its status and bounded text in JSON.

`request` is idempotent; `retry` explicitly takes a new snapshot on the next Hook; `cancel` prevents
publication of in-flight work and retains any existing result. Do not poll per tool. Reading uses at most
256 MiB, 100,000 lines, 256 user messages and 128 KiB of user text; individual messages over 16 KiB and nontext
content are omitted with visible gaps. Parsing is bounded to 15 seconds; no model retries. Partial,
unknown, forked and unavailable transcripts are never full-authority imports. Original files are read-only.
The task's existing expiry/forget rules also remove its history; host transcripts are never deleted.

## Optional history-aware supervision

After the raw reader is `ready` with no gaps, activate once:

```sh
python3 scripts/history_context.py activate --data-directory ABS --session ACTUAL_SESSION_ID --model gpt-5.6-luna --effort medium
```

`status` exposes queued/running/ready/unavailable/failed/cancelled, actual model/effort, usage,
interpretation freshness and `ready_for_supervision`. Activation returns immediately; no blocking or
self-check loop. `disable` explicitly returns to live-source-only scope checks, so old constraints are
then uncovered. Disabling, reader retry or switching models never replenishes the one-call history budget.
An unavailable attempt before a model reservation can be disabled and activated after fixing its input;
a paid failure is not automatically retried. Existing task forget/expiry removes these records too.

The joining code requires exact turn/text overlaps in the same order for the captured live-source prefix.
A supported history snapshot can recover intervening messages missed while capture was disabled; they
retain transcript_import provenance and never become fresh rollback consent. This does not clear a
recorded capture-gap flag or accept partial/unknown history. A missing boundary, unsupported/partial
history, more than 256 combined user sources, or over 64 KiB of structured
history input makes history-aware judgments unavailable before spending a model call. No truncation or
latest-message-only authority. Bounded supported conversations are the target; very large, multimodal,
compacted or forked conversations may remain unavailable. Reading itself still costs zero model calls.

The independent interpreter runs with tools disabled using existing Codex login. Quotes, roles,
proposal/approval ordering, source coverage, intent schema and generation are checked locally;
semantic correctness remains unverified. It reuses intent memory and keeps original user sources plus
role-separated assistant context in review packets. Assistant plans and completion claims never become
user sources. Explicit user approval of a proposal needs both supporting quotations. Neither a summary
nor an old chat message can authorize a rollback.

Input is bounded to 64 KiB; accepted structured results to 24 KiB. A requested concise output target of
2,000 tokens is a prompt preference, NOT a hard model output-token cap. The model call timeout is 120s.
Exactly one history call reservation per task, separate from the configured supervision-call budget; failed
or cancelled-after-launch requests may consume usage. Usage is reported separately, including unknown
usage on failures. No automatic retries, splitting or more expensive-model escalation.

While requested history is not ready, new events continue recording but scope reviews/reminders are
withheld; this never stops the actor's tools or turns. History is not copied into live user sources or
rollback consent. Watched file baselines and prior tool events are not reconstructed or reset. Only
observations since action tracking began can enter supervision. Late results never force a new turn.
New live messages reach every subsequent packet and invalidate old results/reminders. A stale initial
summary remains labelled and cannot replace raw new authority; no repeated paid reconstruction.
Use `show.context.supervision_sources` for ordered effective source IDs and quotes. Raw `task.sources`
contains only captured live user submissions. Reports expose both history origin and current freshness.

Routine review configuration is independent: `supervisor.py enable-experimental ... --model MODEL
--effort low|medium|high`. Existing configuration is retained if omitted; model/effort changes invalidate
in-flight results without resetting budget. Manual `review.py submit` accepts the same flags.
Current supervisor default is Luna high, an experimental default. Explicit
task settings remain in force; to change an existing task use `--model gpt-5.6-luna --effort high`.
History's Luna medium default remains separate. Neither default is evidence of calibrated supervision
quality. Model changes do not replenish budgets or make missing history available.

## Existing task interfaces

- `scripts/task_state.py enable-workspace --workspace ABS`: explicitly opt in exact workspace. `disable-workspace` stops its automatic captures.
- `scripts/task_state.py show`: returns `task`, `context`, `capture_gap`.
- `scripts/task_state.py interpret --input ABS_JSON`: use the current context digest and exact user quotes:

```json
{"user_context_digest":"CURRENT_DIGEST","contract":{"goal":"Task goal","plan":["Step"]},"citations":[{"source_id":"RECORDED_ID","quote":"Actual source substring"}],"uncertainties":[]}
```

All citations are checked locally. Semantic correctness is not implied by valid citations.
New input makes old interpretations stale. Read every ordered user source, including unprocessed amendments.
The `contract` is a baseline, not a replacement for subsequent user instructions.

## Incremental intent memory

Add `intent_update` to the same `interpret` request. This uses your existing task reasoning, not an
extra evaluator. `show.context.intent_memory` returns the last memory, explicitly `current` or `stale`,
its `interpretation_id`, and `current_phase_id` (null if no active phase was established).
Read the original sources even when memory is current; memory is always unverified.

```json
{
  "base_interpretation_id": null,
  "changes": [{
    "id": "phase-1", "kind": "phase", "text": "Inspect the existing implementation",
    "status": "active", "origin": "user_requirement",
    "citations": [{"source_id": "RECORDED_ID", "quote": "Actual source substring"}]
  }],
  "source_effects": [{
    "source_id": "RECORDED_ID", "quote": "Actual source substring",
    "role": "requirement", "item_ids": ["phase-1"]
  }]
}
```

- `base_interpretation_id`: null for the first intent memory; otherwise the saved memory ID, even if
  stale after new input. Concurrent updates to that parent are rejected; reread before reconciling.
- `changes`: only new or changed items, each with a stable task-local ID. Omitted items remain.
  Kind: `goal`, `acceptance`, `phase`, `deliverable`, `operation`, `constraint`, `plan`, `context`,
  or `presentation`. Kind is stable; supersede an item and create another when its kind changes.
- Status: `active`, `deferred`, `paused`, `superseded`, `cancelled`, or `unknown`.
  At most one phase may be active. Deferred work stays deferred until supported by a new interpretation
  citing user evidence. Status is interpreted intent, not task execution progress or recorder pause.
- Origin: `user_requirement` or `agent_proposal`. Both are your unverified classification.
  A proposed plan does not become authorized by being recorded or marked active.
- Every change needs real quotes and matching `source_effects` linking those sources to its item ID.
  Retire/revise only affected items when the user changes intent; preserve other applicable requirements.
- Effects describe the role of a source for particular items. Role: `requirement`, `fact`,
  `clarification`, `addition`, `replacement`, `cancellation`, `pause`, `resume`, `continuation`,
  `rationale`, `example`, `presentation`, or `uncertain`. Multiple roles per message are allowed.
  A supplied source/item relation replaces that relation's previous interpretation; other relations survive.
- Every `unprocessed_source_ids` entry needs an effect. Unclear impact can use `uncertain` with empty
  `item_ids`, plus an explanation in top-level `uncertainties`. Retain unresolved uncertainties on later updates.
- Empty `changes` is valid, e.g. a continuation that changes no requirement. No user-text keyword rules apply.
- Interpretations without intent memory remain readable. Once intent memory exists, include `intent_update`
  on subsequent interpretations to avoid discarding it. Exact retry of the latest request is idempotent.

Keep `contract` as a concise compatibility summary consistent with the merged items, not just the newest
message. This is not independently verified. Distinguish reasons/examples (`context`) from explicitly
requested user-visible content (`presentation`), and current work from future operations.
When the user explicitly sets a current phase, record a `phase` item; don't leave that boundary only
inside a broad goal summary. A prohibition that applies now is an active `constraint`, while the work
it defers is a separate deferred `operation` or `deliverable`. Deferred constraint status must not be
used to mean an active prohibition. Don't add implementation choices to user requirements: a missing
backend does not authorize mocks or fabricated success. Keep unsupported choices as agent proposals
or uncertainties, including in the compatibility summary. A rationale may clarify intended behavior;
record its explanatory role without turning it into product copy.
Limits: 64 retained items including retired ones, 512 source effects, 32 interpretations and 4 MiB total
task state. Existing input/quote bounds still apply. Capacity or capture gaps fail visibly, never by
dropping old requirements; `capture_gap` means the full current scope is not known.

## Findings and independent review

- `scripts/task_state.py finding --input ABS_JSON`:

```json
{"location":"path:line","summary":"Possible unrelated issue","observation_id":"UNIQUE_OBSERVATION_ID"}
```

- `scripts/review.py submit --evidence ABS_JSON`: explicitly request one independent review. The file contains only actions and coverage; the adapter adds recorded user sources and the latest interpretation.

```json
{"actions":[{"id":"action-1","kind":"write","path":"src/file.py","evidence":"Actual bounded diff or tool evidence","origin":"agent_supplied_diff_unverified"}],"coverage":"complete_for_checkpoint"}
```

Mark `coverage` as `partial` if evidence is incomplete. Never replace missing tool output with invented facts.
Packet cap is 64 KiB including all user sources. Oversized packets are unavailable, not silently truncated.
Origin must identify how evidence was obtained; a self-reported diff is not independently verified provenance.

- `scripts/review.py status`: results, usage, freshness and failures. Repeated identical submissions use the same job; no new model call.
- `scripts/task_state.py forget`: delete task content including embedded reviews.
- `scripts/task_state.py prune`: remove expired task contents on demand (7 days since successful state write).

Global `scripts/control.py pause|resume ABS_DATA` is separate from workspace content opt-in.
If no real session ID or data directory is available, explain the missing integration context; do not guess identities or read unrelated sessions.

## Bounded action evidence

Action content capture is a SEPARATE opt-in on an existing task; workspace input tracking alone does
not enable it. With the user's tracking authorization, enable the actual task and a small relevant file set:

```sh
python3 scripts/action_evidence.py enable --data-directory ABS --session ACTUAL_SESSION_ID \
  --workspace ABS_WORKSPACE --watch relative/file.py --watch another/file.py
```

Subsequent Pre/PostToolUse hooks save bounded input/response observations, IDs, time and user/action
versions. They do not classify semantics or call a model. Explicit watched files are sampled at hook and
review boundaries; cumulative diffs use each file's enable-time baseline. Repeating enable can add paths
without resetting prior baselines or review budget. No automatic shell-path inference or repository scan.
`disable` takes the same data/session/workspace flags and stops further action capture, retaining history.
Global pause and workspace disable also stop content capture. Never enable unrelated tasks automatically.

Limits: 12 watched regular UTF-8 files, 4 KiB each; 48 retained tool observations, input 2 KiB and response
1 KiB each, with truncation flags/hashes and eviction counts. No symlink traversal or outside-workspace
snapshots. Missing, oversized, binary and unreadable files are explicit. Snapshots/commands can contain
sensitive source content; do not watch secret files. Everything stays in the private task state with its
existing 4 MiB cap, seven-day lazy expiry and forget/prune lifecycle. An explicit review sends the selected
packet via the existing Codex account. It may include original user text, code and bounded tool output.

`action_evidence.py export` refreshes watched files and returns the automatically produced evidence.
`review.py submit` without `--evidence` does that under the task lock and then starts the usual detached
review. It never requires waiting or polling from the main Agent. The optional `--evidence` manual interface
remains. Automatic export prefers current watched code, then recent tool observations (22 KB selection
budget); omitted IDs/counts are disclosed. Full ordered user sources are never silently truncated.
All automatic packets have partial coverage: unobserved code/callers/background activity may be missing.
A file diff is an observation with UNKNOWN AUTHORSHIP, not proof that the Agent caused it. An observed
command/patch is an attempted action, not verified success. Never hide these distinctions in reports.

Results retain `verdict` for scope, plus `scope_labels` and a separate `implementation_risk` containing
`verdict` (concern/none_observed/uncertain), labels, source/action evidence IDs and reason. Partial coverage
cannot produce a clean checkpoint on either axis. Risk labels are overengineering/narrow_hardcoding;
scope labels include unrelated_work/phase_expansion/goal_displacement/rationale_as_copy,
scope_reduction/implementation_substitution/unsupported_completion, and unknown.
Delivery labels compare active user requirements with observed implementation and actual completion claims.
Incomplete sampling is not proof of missing implementation; approved prototypes/deferrals and honest progress
reports are not false completion. Delivery scope findings may produce advisory reminders under the existing
gates. The report's `delivery_gaps` retains uncertain and stale judgments with evidence and freshness, even
when no reminder was delivered. This is not a separate end-of-task audit or a guarantee of complete coverage.
These are semantic output labels, not input keyword rules. Necessary validation, constants and authorized
compatibility do not imply risk. Multiple labels share ONE review and the same configured per-conversation review budget.
`status` checks user/action versions and watched file changes. Results from corrected or superseded action
versions are stale; never present them as a current correction. Unwatched or reverted-between-samples
external edits cannot be detected. Automatic scheduling and reminders require the separate opt-in described below. Rollback always requires explicit selection.

## Controlled reminder transport

This is a separately enabled **transport test**, not automatic drift supervision or classification approval.
`reminders.py enable-test` / `disable` take the actual data directory and session flags, on an active task
with action capture when enabling; disabling also works while capture or the task is paused. Enable before a subsequent direct user turn; prior finished turns are not reopened.
`queue-test --input ABS_JSON` accepts only this bounded fixture shape:

```json
{"user_context_digest":"CURRENT_DIGEST","action_version":[0,true],"source_ids":["ACTUAL_USER_SOURCE_ID"],"requirement":"Current user boundary.","evidence":"Explicitly controlled test observation.","suggestion":"Brief suggested next action."}
```

Read current versions from state; do not invent them. Each text field is at most 384 UTF-8 bytes.
The next PreToolUse checks sources, action/file versions, gaps, active turn and expiry, then emits only
`additionalContext`. It never waits for a reviewer, changes tool arguments, decides permissions or emits
Stop continuation. The pending tool still runs: a reminder can affect only subsequent model behavior.
No queue-test call belongs in ordinary per-tool execution. Real review-to-reminder scheduling is described below.

`status` reports pending, emitted_unconfirmed, delivered, stale, cancelled or report-only outcomes.
A successful stdout write is only emitted_unconfirmed. `receipt-test` is for a test observer that has BOTH
matching host context and an Agent reply naming the reminder ID, correlated by turn/tool IDs. It records
externally supplied evidence, not cryptographically verified receipt or adoption. This receipt interface does not automatically verify transcript delivery. Ordinary tasks do not make acknowledgment calls.

Identical fixtures deduplicate. There is no cumulative emission quota. Reminder cooldown is 60 seconds
in standard mode and 300 seconds in long-task mode; pending reminders still expire after five minutes.
At most 48 reminder details are retained; old entries roll into explicit omitted/status counts.
Up to 256 recent emitted finding identities survive detail rollover for deduplication.
User changes, new observed actions, relevant watched-file changes or a capture gap invalidate queued reminders.
Turning off action capture prevents new queues and emissions; disabling the transport cancels pending entries.
Stop/Interrupt/SessionEnd move pending reminders to report-only; results queued after turn closure also stay
report-only. No new turn is started. Already emitted context cannot be recalled by the plugin, so the short
advisory identifies itself as non-authoritative and says to ignore an already resolved observation.
All history lives in the task state with its existing expiry/forget behavior. Adoption remains not_assessed;
only later observed actions can support a separate behavior finding. Unconfirmed/cancelled delivery is not success.

## Experimental automatic file supervision

With authorization to supervise this task and consume its review budget, run once:

```sh
python3 scripts/supervisor.py enable-experimental --data-directory ABS --session ACTUAL_SESSION_ID
```

Requires existing active task and separately enabled action capture with explicit watched files. It does not
watch new files automatically. It replaces controlled-test mode. `disable` cancels pending reminders and
invalidates in-flight results, including when recording is paused. Default remains disabled.

PostToolUse, direct user updates and Stop can dispatch a detached worker if watched content differs from
baseline or an emitted reminder needs a followup. Optional `checkpoint` offers the same bounded decision
at a meaningful plan/phase boundary; do not invoke per tool. The worker coalesces two seconds of changes
before reserving a fresh packet. In standard mode, minimum dispatch spacing is 30 seconds. One reviewer
per conversation includes pending automatic dispatches and queued/running manual or automatic reviews;
there is no hourly dispatch cap. No duplicate evidence or automatic failure retries. Cooldown candidates wait
for a subsequent eligible boundary; there is no persistent polling daemon. Stop may produce report-only
review results without opening another turn.

Automatic packets include all raw user sources and bounded watched file snapshots only. Tool observations
remain stored but are excluded from this evaluator, so read-tool traffic does not invalidate unchanged code.
User changes, watched file/baseline/configuration changes, gaps, pause and supervision changes invalidate
results. This does NOT allow stale historical tool judgments to be delivered under a file-only version.

Every automatic and manual review, including followups, shares one per-conversation review slot and any
explicit total ceiling. Failed reservations still count toward that ceiling. There is no hourly review cap.
No default cumulative ceiling or reserved followup slot applies. Once emitted,
a changed watched snapshot under unchanged user authority can trigger one independent followup. Its result
is still_observed, no_longer_observed or uncertain for that specific earlier concern only; partial global
scope remains uncertain. It does not prove authorship, adoption, global correctness or causal benefit.
Followups never generate a repeated correction prompt; further changes mark their result stale.

Only a cited, current-file scope drift with supported scope labels can become an experimental advisory.
Unknown labels, ambiguous scope results and unavailable cited files remain record-only.
Implementation risk alone never triggers an advisory and never vetoes an independently supported scope drift. No production accuracy claim follows from this gate. Pending findings on the
same cited file set and user version merge to the latest evidence; already emitted findings do not repeat
just because the model gives a different label. This conservative merge may suppress distinct later issues
in the same files. In-flight work is not stopped and already emitted context cannot be recalled.

`supervisor.py status` shows budget, dispatch health, reminder history and followup judgments. A worker
older than 180 seconds is flagged as overdue or possibly lost, not confirmed dead. Disable/re-enable clears
its dispatch reference and invalidates configuration results without resetting budgets, but does not remove
queued/running review records. An abandoned job can still hold the shared review slot; no general automatic
lost-job recovery is implemented. Inspect job/process health rather than clearing task history or counters.
No ordinary per-tool polling or receipt is needed. The report interface below renders the retained evidence and usage.

## User report and change provenance

At a normal user reporting boundary, run:

```sh
python3 scripts/report.py --data-directory ABS --session ACTUAL_SESSION_ID --format brief
```

When preparing to declare the current task complete, append `--final`. This waits at most 15 seconds
for existing queued/running jobs, including a worker already dispatched but not yet reserved. It releases
the state lock between checks and does not reserve calls, retry failures, cancel jobs, or chase unrelated
new reviews. No existing work means immediate reporting. Other reporting boundaries omit `--final`.
JSON `final_wait` records the wait outcome, elapsed time and tracked/pending review IDs; brief and full
Markdown disclose timeout or completion without implying a fresh whole-task clearance. The final file
refresh and normal freshness checks happen after waiting. Do not repeat the command to extend the wait.

Paste this fixed summary verbatim into the user response, rather than paraphrasing supervision status.
It labels cumulative usage separately from retained reviews; it does not claim per-turn counts. Findings
include freshness and concrete capture limits. At most three finding-bearing reviews are expanded; remaining
findings are counted and remain available in the full report. Zero current findings is never a clearance.
`--format markdown` retains the full report and remains the CLI default for compatibility.
`--format json` includes full retained sources, cited evidence, runtime readiness and the same `user_summary`.
`--output ABS_NEW_FILE` explicitly exports
with private permissions and refuses overwriting an existing file. Without it, output is stdout only.
Use `--recorded-only` to avoid refreshing files. When recording/action capture is paused or disabled, file
refresh is skipped automatically and the report says current files were not revalidated.

The reporter never calls a model, schedules a review, changes budget or writes the workspace. Refresh is
in memory only and does not renew retention; existing lazy expiry may still remove expired task state.
Reading/rendering the report in the main Agent's context can of course consume that Agent's normal tokens.
A Stop event does not establish whole-task completion; reports are snapshots, including running/late work.

Action capture establishes report-only provenance for newly watched files. It saves their watch-time
baseline plus a bounded read-only Git HEAD reference when available, before subsequent observed edits.
Git is optional and inspected only when enabling new paths, outside the task lock; no repo scan or Git
command runs in ordinary hooks or report generation. Git metadata stays outside evaluator packets.
Existing watched baselines are never reset; missing references are not retroactively fabricated.

Reports separate HEAD-to-watch-baseline differences from watch-baseline-to-current differences. A difference
already present at watch time is not attributed to the current Agent; a later difference may include other
actors. Zero earlier captured tools is not proof the baseline preceded all task work. Unknown dependencies,
late capture, unreadable content and missing history remain explicit. File citations do not declare the whole
file out of scope. Added/deleted empty files and newline-only edits remain visible.

Scope and risk judgments remain separate and are not counted once per label. Reports include stale/failed/
queued reviews, citations, unverified Agent findings, reminder delivery state and independent followup
observations. Observed return to a baseline is not proof of authorship or adoption. Missing usage is unknown,
not zero; cached input and reasoning output are not added twice. Only independent review usage is collected.
The report itself performs no rollback, dependency analysis or classification. For explicit selected rollback, read [the rollback interface](rollback.md).


## Optional activity scope

Add `--include-activity` to `supervisor.py enable-experimental` only when the user authorized tool/plan
supervision as well as file supervision. Existing tasks keep file-only behavior until explicitly changed.
This mode can work with an empty explicit watch list because it uses already opted-in tool observations.
Four completed calls or a completed official `update_plan` create a candidate; packets retain at most eight
observations and reuse existing byte/budget/cooldown bounds. No command parsing or natural-language keyword
authorization rules. File checks and file followup retain priority. No new review for unchanged evidence.

The additional evidence is historical: it proves observed attempts/responses, not successful writes or
ongoing drift. A short conditional advisory can survive subsequent unrelated tool traffic, but becomes stale
with new user input, a newer plan, watched-file changes, eviction or 120 seconds since the observations.
No more than one attempted activity advisory per user-source version; no repeated correction loop.
Already-resolved historical activity should be disregarded. Do not undo files based on these observations.

Pure prose, tools bypassing hooks, clipped or omitted content, arbitrary unseen file changes and complete
semantic dependencies remain uncovered. Report labels historical observations separately from current file
judgments. The mode is experimental and does not establish general detector accuracy or causal savings.


## Long-task recovery and scheduling

Use the host-provided data directory and the exact session. A known transcript path may be recovered
explicitly with `history.py recover --data-directory ABS --session ID --transcript-path ABS`; this is
recorded as local recovery, not a host Hook. It checks file/session identity and does not search other tasks.
Read `history.py status` once at a normal checkpoint. On ready, call `history_context.py activate`. A repaired
snapshot can retry an earlier unavailable activation only if no history model call was reserved. Paid
failures do not reset or automatically retry. History default remains Luna medium.

The local scan allows 256 MiB / 15 seconds and verifies native contiguous compaction windows; user
messages are never replaced by compaction summaries. Up to 256 user messages / 128 KiB user text are
retained, subject to per-message bounds. Agent commentary and excess replies may be omitted explicitly.
The history model packet is at most 64 KiB, with at most 16 KiB recent complete assistant support;
unresolved references to omitted proposals must remain uncertain. Reviews also keep raw user authority,
without duplicating it in history context; each review packet is capped at 64 KiB.

For long-task supervision explicitly pass `--profile long-task` to `supervisor.py enable-experimental`
alongside `--model gpt-5.6-luna --effort high` and `--include-activity` if activity capture was authorized.
This uses at least 180 seconds between automatic dispatches and 300 seconds between reminder attempts.
Standard mode uses 30-second dispatch spacing and 60-second reminder cooldown. Neither profile has an
hourly review/dispatch cap or default lifetime quota. Manual and automatic reviews share one slot per
conversation. No wall-clock cutoff, goal cancellation or counter reset. Automatic reviews resume at an
eligible Hook after spacing and concurrency gates clear; deferred manual calls are not automatically queued.

`supervisor.py set-budget --max-reviews N` with actual data/session flags optionally sets a conversation
review ceiling, including previous reservations. `--unlimited-reviews` removes it. Budget-only updates do
not change the model/profile/activity flags or invalidate pending work. Already-reserved calls are not
cancelled. `enable-experimental` also accepts these mutually exclusive flags. Reports/status show the
remaining count (null means no cumulative ceiling), active-review gate and explicit budget exhaustion.
The status limits include `max_concurrent_reviews_per_session: 1`; hourly limits are null. Never
forget/recreate task state to renew a ceiling. A busy reviewer defers new reservations without spending.

Explicit limits, counts and existing timestamps persist across profile changes.
The latest 16 review details and 48 reminder details are retained, protecting unfinished review jobs and
active followup references. Evicted detail is explicitly counted, not archived; available usage and missing
usage counts are aggregated without counting cached input twice. Reports cannot cite evicted evidence.
User-authority retention and task-state size limits are unchanged.

`supervisor.py status` includes runtime eligibility, unavailable reasons, started/completed review counts,
history gaps, and scheduling limits. Eligibility is not proof of completed review or full coverage.
Large watched files whose contents cannot be read do not consume file-only evaluations; authorized
activity checks may still assess bounded tool attempts, with unknown final file state and authorship.


## Incremental context and optional advisory responses

After initial history activation, UserPromptSubmit/Stop/SessionStart hooks launch local asynchronous
context synchronization; automatic review workers also synchronize before reserving a model call.
Only this session's host-supplied transcript is read. New records are parsed from a persisted cursor;
the older prefix is hashed locally to detect rewriting. Snapshots without a cursor bootstrap the parser once.
There are zero new history model calls. No tool denial, actor waiting, forced turns or budget resets.
Known gaps defer evaluation, while recording and the main task continue. A later complete boundary
can recover transient partial-line gaps. File replacement/rewriting requires explicit investigation.

`python3 scripts/context_sync.py status --data-directory ABS --session ACTUAL_SESSION_ID`
shows status, last refresh, synchronized byte position and error reason. For explicitly authorized
recovery with a known same-session path, `sync --transcript-path ABS` performs the same local operation.

User authority remains separate from assistant support. A chronological ID list connects retained
messages; new user sources are never discarded to fit assistant context. Recent complete commentary
and the assistant reply preceding the latest user are prioritized within the existing 16 KiB support
budget. Omitted replies stay explicit. A claim of completion is unverified, and a proposed next step
requires subsequent user approval, interpreted in context with still-active constraints.

An optional reminder response is a small independent record, not an intent-memory rebuild:

```json
{"id":"EXISTING_EMITTED_REMINDER_ID","user_context_digest":"CURRENT_DIGEST",
 "disposition":"disputed","reason":"Brief explanation of the observed work",
 "citations":[{"source_id":"RECORDED_USER_ID","quote":"Exact user text"}]}
```

Invoke `python3 scripts/reminders.py respond --data-directory ABS --session ACTUAL_SESSION_ID --input ABS_JSON`.
Allowed dispositions: accepted/disputed/obsolete/uncertain. At most eight citations; the whole response
is at most 4 KiB. Empty citations are permitted for uncertainty or a factual response, never proof of
new authority. Exact resubmission is idempotent. Only the latest response per reminder is retained.
Responses appear in reports and the next ordinary review (most recent four, up to 6 KiB, omissions
reported). They never trigger a review, modify the verdict, confirm external delivery or reset budgets.
