# Limits and known issues

Navi is an experimental advisory tool, not an execution policy engine or proof of task correctness.

## Coverage

- Up to **12 explicit watched files**, with **4 KiB** snapshots per file. Larger files are reported as
  oversized and unavailable for file-snapshot review; no complete snapshot is supplied. Activity evidence
  may still show attempted changes.
- Up to **48 retained tool events**, with **2 KiB inputs** and **1 KiB outputs** per event. Truncation,
  eviction and unknown authorship remain visible.
- Up to **256 user sources**, **128 KiB user text**, **16 KiB per message**; the reader separately retains
  bounded assistant text, selecting up to **16 KiB** for model support. Complete review packets cap at **64 KiB**.
- Initial local history scans cap at **256 MiB / 15 seconds**. Incremental parsing retains at most
  **4096 message identities** and bounded work per scan. Prefix verification also costs local I/O.
- Total task state caps at **4 MiB**. Known gaps defer model review; they do not stop the main task.
- Nontext/multimodal messages, unsupported host formats, forked history, bypassed hooks, unobserved
  files, and complete dependency relationships may be unavailable.

## Behavior and cost

- Reviews may misunderstand authorization, miss drift, or flag legitimate supporting work.
- Citation validation can reject otherwise plausible model output. Failed calls still consume budget
  and may consume usage; there is no automatic paid retry or silent model upgrade.
- Automatic/manual/follow-up reviews share one slot per conversation and any optional cumulative ceiling.
  A long-lived conversation shares these settings; usage counters never reset. A lost queued/running
  reviewer can hold the slot until recovered; there is no automatic paid retry. Disable/re-enable clears
  the supervisor dispatch reference but does not remove queued/running review records, so it is not a
  general lost-job recovery mechanism. An age over 180 seconds flags suspicion, not confirmed process death.
- Review count limits do not directly cap tokens. Token/dollar ceilings are not implemented.
- Default model names depend on account access. No availability, price or quota equivalence is promised.
- Incremental context collection reduces repeated parsing; model requests still contain repeated
  context. Net token savings and scalable cache reuse have not been demonstrated.
- A partial-coverage result cannot prove everything is within scope. Zero reminders is not a clean bill
  of health, and an emitted reminder is not proof of successful correction.
- Delivery-gap detection shares existing review triggers and bounded evidence. There is no mandatory
  end-of-task audit. Missing sampled code cannot prove a capability is absent, and a completion claim
  outside captured context may be missed. Late results appear in a subsequent report; an already-sent
  final response is not retroactively updated.
- There are no hourly caps or default cumulative quotas. Standard automatic spacing is 30 seconds;
  long-task spacing is 180 seconds. Frequent new evidence can substantially increase usage. Explicit manual
  reviews share concurrency and total budgets but are not subject to automatic spacing. Optional total
  ceilings pause new evaluations visibly; existing valid reminders can still be delivered.
- Detailed records roll over after 16 reviews / 48 reminders. Cumulative counts and known usage remain,
  but old evidence is no longer available. Unfinished jobs are protected and can require recovery.
- Deduplication keeps up to 256 recent emitted finding identities for a user-context version. File
  findings merge by cited file set; activity findings merge by user-context version. Delivery label sets
  additionally distinguish delivery findings from expansion notices. This conservative
  grouping may suppress a distinct later issue; it is not semantic proof that two issues are identical.

## Reporting and configuration authority

- The fixed summary is deterministic and currently Chinese-only. The skill requires verbatim inclusion;
  the host still lets the main agent write its final response. There is no enforced final-response template.
- Counts describe cumulative conversation usage and retained records, not the latest turn. Ordinary reports do not wait;
  task-completion reports using `--final` wait at most 15 seconds for existing reviews. This does not
  require a new final audit or guarantee current coverage. Freshness, partial coverage and delivery uncertainty remain visible;
  see [report fields](REPORTING.md).
- Explicit budget settings persist. Their `limit_origin` records a command-set value, not a verified
  user instruction with an approval revision. Navi cannot independently prevent the executing agent from
  reinstating an old ceiling based on stale conversational context. Cross-conversation authorization
  changes are not automatically synchronized into the current conversation's user sources.

## Rollback

Only bounded, already-observed regular text-file selections are supported. User confirmation,
provenance and independence checks are required; unknown or mixed ownership/dependencies are not
silently reverted. There is no automatic rollback or general Git reset operation.

## Compatibility

macOS is the native-host test target. POSIX offline portability does not establish Linux desktop
compatibility. Windows and non-Codex adapters are not supported. Host upgrades may change hook payloads,
trust behavior, cache paths and transcript records; treat unsupported input as a visible gap.
