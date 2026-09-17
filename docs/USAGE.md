# Using Navi

## Start with an explicit task

State your deliverable and boundaries as ordinary language. For example:

> Use Navi to supervise this task.
>
> Update the settings page to match the new layout. Keep backend wiring deferred.

There are no required semantic keywords for normal requirements. Workspace input capture, task action
capture, and automatic supervision are separate opt-ins. The Navi skill guides the executing agent
through them. The enabling message itself may precede input capture; a later normal user submission
then creates a real source. Never replace it with an invented user message.

For multi-hour work, explicitly select `long-task`. For an existing conversation, authorize reading
this same conversation's earlier requirements. Initial history interpretation has a separate one-call
budget; a failed interpretation can use validated originals while preserving that failure in reports.
Incremental local synchronization subsequently refreshes supporting dialogue without another summary call.

## Model and profile selection

The installed skill documents the exact command interface. For an already-initialized task:

```sh
python3 "$PLUGIN_ROOT/scripts/supervisor.py" enable-experimental \
  --data-directory "$PLUGIN_DATA" --session "$NAVI_SESSION_ID" \
  --profile long-task --model YOUR_AVAILABLE_MODEL --effort high --include-activity
```

`PLUGIN_ROOT` and `PLUGIN_DATA` are host-provided inside hooks; obtain the real installed paths when
using a separate terminal. `NAVI_SESSION_ID` is an example variable that you must set to the actual
conversation ID—it is not a built-in environment variable. Do not copy another task's identifiers.
File watches or authorized activity capture must already be enabled. Model changes preserve spent counters.

## Review budgets

Continuous supervision uses new evidence, minimum dispatch spacing and rolling hourly limits. It has
no default cumulative review or reminder quota. Manual reviews share the same hourly review limit;
requesting them repeatedly cannot bypass it. Rolling limits recover at the next eligible event, without
a background polling loop. Reminders have separate cooldown, deduplication and expiry checks.

To set an optional total ceiling for this conversation (including previous reservations):

```sh
python3 "$PLUGIN_ROOT/scripts/supervisor.py" set-budget \
  --data-directory "$PLUGIN_DATA" --session "$NAVI_SESSION_ID" --max-reviews 40
```

To remove that ceiling while keeping hourly limits and all spent counters:

```sh
python3 "$PLUGIN_ROOT/scripts/supervisor.py" set-budget \
  --data-directory "$PLUGIN_DATA" --session "$NAVI_SESSION_ID" --unlimited-reviews
```

`--max-reviews 0` pauses new review reservations. It does not cancel an already reserved/running call,
stop recording, suppress a valid existing reminder, or stop the executing agent. Budget updates preserve
the current model, profile, activity capture and pending work. The enable command also accepts these
flags. Never clear task data or counters to replenish a budget. Token/dollar ceilings are not implemented;
review counts are not a fixed token allowance. Reports expose missing usage rather than assuming zero.

### Record retention

The report retains the latest 16 review and 48 reminder details and explicitly counts older omitted
entries. Cumulative usage keeps known totals and missing/incomplete counts. Old detailed evidence is
not archived elsewhere; export a report beforehand if you need to keep it. Unfinished review jobs and
active followup references are protected. Existing authority/history size bounds still apply.

## Understand the results

- **Scope drift:** evidence supports a departure from user-authorized work. Eligible findings may
  become advisories; this does not establish authorship or authorize a rollback.
- **Uncertain:** incomplete evidence or an unresolved scope question. Partial coverage cannot establish
  a clean whole-task result; the validator downgrades clean verdicts under partial coverage.
- **Implementation risk:** an independent axis for unnecessary fallbacks or narrow patches. It is
  recorded rather than sent as a production scope advisory in this alpha.
- **Delivery gaps:** scope findings about unauthorized removal/deferral, replacement of a required
  capability, or a completion claim contradicted by observed evidence. These use the same review calls
  and advisory gates. The report lists them separately, including uncertain, stale and late findings.
  Missing sampled code is not proof of absence, and a progress checkpoint is not a completion claim.
  User-approved prototypes and deferred work remain legitimate. No gap recorded does not prove delivery
  is complete; there is no separate mandatory completion audit and an asynchronous result may arrive
  after the agent's final response.
- **Stale:** user context, supporting dialogue, observed evidence, or timing changed. The earlier
  result remains a historical record, not approval of later changes.
- **Failed:** no usable review result. A failed call may still consume usage and budget.

An agent may respond briefly to a reminder as accepted, disputed, obsolete, or uncertain. This is
optional, does not require a full history rewrite, and does not start another review. Keep the original
judgment and agent response distinct. Sending a reminder, receiving it, and correctly changing behavior
are separate claims.

## Report, pause, and forget

Ask the agent for a report at a normal task checkpoint. Report generation does not call a model.
For direct CLI use with actual paths/session values:

```sh
python3 "$PLUGIN_ROOT/scripts/report.py" \
  --data-directory "$PLUGIN_DATA" --session "$NAVI_SESSION_ID" --format markdown
python3 "$PLUGIN_ROOT/scripts/control.py" pause "$PLUGIN_DATA"
python3 "$PLUGIN_ROOT/scripts/control.py" resume "$PLUGIN_DATA"
python3 "$PLUGIN_ROOT/scripts/task_state.py" forget \
  --data-directory "$PLUGIN_DATA" --session "$NAVI_SESSION_ID"
```

Pause applies to this installed plugin's recorder, not just the current task. Forget removes retained
task data and its recovery state; exported reports and Codex's own transcripts are separate.

## Selective rollback

Navi can preview selected changes to already-observed small text files. Review the exact preview,
ownership, dependencies, and concurrent-edit status before explicitly choosing execution. Mixed or
unknown edits remain preview-only. It is not general repository rollback, dependency analysis, or an
automatic response to a drift verdict.

See the packaged [command interface](../plugins/navi/skills/navi/references/interface.md) and
[rollback interface](../plugins/navi/skills/navi/references/rollback.md).
