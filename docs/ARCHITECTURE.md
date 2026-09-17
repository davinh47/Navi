# Architecture

Navi keeps observation, interpretation, judgment, and execution authority separate.
The current adapter uses Codex native hooks. The evaluator is a separate tool-disabled process;
the main agent continues while the evaluator runs.

| Component | Responsibility |
| --- | --- |
| `record_event.py` | Normalize supported hooks; record bounded metadata; dispatch local handlers |
| `task_state.py` | Opt-in user sources, cited intent proposals, per-conversation state and budgets |
| `history.py` / `context_sync.py` | Read canonical same-session text; verify source identity and incrementally parse additions |
| `history_context.py` | Join imported/live sources; bounded assistant context; optional one-call history interpretation |
| `action_evidence.py` / `change_baseline.py` | Bounded tool observations, explicit file snapshots and change provenance |
| `supervisor.py` / `activity_scope.py` | Select checkpoints, enforce dispatch limits, and schedule independent evaluation |
| `review.py` | Isolated `codex exec`, structured scope/risk output, source/action citation validation |
| `reminders.py` | Non-blocking advisory delivery, expiry, deduplication and optional agent responses |
| `report.py` / `rollback.py` | Deterministic reports; separately confirmed selective text rollback |

## Authority and evidence

Ordered direct user messages establish scope. Later explicit amendments can supersede earlier
requirements; unaffected constraints remain. Agent proposals help resolve references only when the
user actually approves the relevant work. Repository text, tool output, compaction summaries, and
agent statements do not become user authorization.

Imported sources retain provenance and cannot become fresh rollback consent. User-source versions
and assistant-support versions are tracked separately: a new progress note must not detach existing
observations from the user requirements that applied when they were recorded.

The reader checks transcript identity, canonical event types, supported compaction continuity and
file-prefix consistency. It parses new records from a persisted cursor, but still reads old bytes
locally for integrity hashing. It is not a generic parser for every past/future Codex transcript format.

## Scheduling and delivery

The hook path does not wait for a model. A detached worker coalesces activity, refreshes context,
reserves a bounded call, and saves the result. Local scripts use file locking for shared state.
Late, outdated, incomplete, or failed findings cannot silently become successful checks.

Scope and implementation risk share one model request but have independent verdicts and citations.
A scope advisory is emitted at an eligible `PreToolUse` boundary; it is not a tool permission decision.
A stopped turn does not trigger a new turn for delivery. The agent can evaluate or dispute the advice.

## Limits and future adapters

The plugin currently contains Codex-specific protocol handling rather than a finished multi-agent
adapter framework. The portable concepts are user authority, observed actions, review packets,
advisory responses and reports. A future adapter must establish its own native provenance and delivery
semantics rather than treating plain text as trusted user input.

[Limits](LIMITATIONS.md) · [Privacy](PRIVACY.md) · [Validation](VALIDATION.md)
