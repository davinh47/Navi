# Explicit selected rollback

Use `../../../scripts/rollback.py` relative to this reference directory, with the actual host `PLUGIN_DATA`
and session ID. This is an explicit tool, never a hook-triggered mutation. No model call is added.

## Supported operation

One existing, owned, single-link UTF-8 regular file, at most 4 KiB, in the explicit watch list, with a
readable text baseline. Select line-change segments; adjacent changes can be one inseparable segment.
Preserve original user changes in the watch baseline and all unselected segments. The watch baseline is
not necessarily task start; authorship and semantic independence cannot be established by diff alone.
File additions/deletions, symlinks, hardlinks, binaries, unobserved files, multi-file transactions and
external effects are unsupported. Unknown or overlapping attribution/dependencies remain preview-only.

## Workflow

All commands take `--data-directory ABS --session ACTUAL_SESSION`.

1. `inspect --path relative/file.py` lists numbered baseline/current change segments. It reads the current
   file without modifying it. Do not treat a model label as proof a segment is eligible for rollback.
2. `prepare --path relative/file.py --select 2 4` saves a single-file preview with exact current/target bytes,
   file identity, selected segments, user-source version, ID and diff. Show the diff and affected selection
   to the user. Explain unknown authorship/dependencies and stop at preview if they cannot be resolved.
3. Obtain explicit informed confirmation of the exact preview, selected-change ownership, independence
   from retained changes, and exclusive editing. Normal user wording is allowed: no keyword classifier.
   Map that actual confirmation to the bounded JSON below; this mapping is the calling Agent's responsibility.
   A generic “continue” is not sufficient to invent these confirmations. This approval is for the described
   rollback only, not for adding/removing unrelated code.
4. `apply --plan rb-ID --confirmation /absolute/path/to/confirmation.json` rechecks eligibility, user
   context, configuration, full current file bytes/identity and cooperative file lock. It persists the
   pre-write recovery content and authorization before writing, then verifies the result. It changes no
   Git index/HEAD, resets no budget and renews no retention. Mode/inode metadata remain on the same file.

```json
{
  "plan_id": "rb-ID-FROM-PREVIEW",
  "source_id": "u-ID-FROM-NEW-DIRECT-USER-SOURCE",
  "quote": "Exact excerpt of actual informed user confirmation",
  "ownership": "user_confirmed_selected_changes",
  "dependencies": "user_confirmed_independent",
  "exclusive_workspace": true
}
```

These are structured interface values, not matching rules for natural-language input. The source must be
one new direct input after the preview; additional intervening updates require a fresh preview. Citation
matching proves the excerpt exists, not that it constitutes permission. Neither this CLI nor the evaluator
is an authentication boundary against a caller that already controls workspace and task data.
If the host did not capture the user's confirmation, do not fabricate it or bypass the check.

## Results and recovery

`status` exposes stored previews, confirmation provenance, outcomes and pre-write bytes. Maximum eight
records per retained task; no silent eviction of recovery points. Records live inside the existing private
7-day task store. Expiry/forget removes them; exports are separate user-owned copies.

For a successful operation, `prepare --restore rb-ID` previews restoration of that operation's saved
pre-write content. It requires current bytes to still equal that operation's result. Show this new preview
and obtain a new direct confirmation before `apply`. Later changes cause refusal, with no fuzzy merge.

`applying` or `write_outcome_unknown` means a crash/write or final-recording failure may have left a partial
change. Inspect status/recovery bytes and the actual file; automatic retries and further rollback plans
are refused for that task. These cases require manual recovery with a separately reviewed diff. Do not
label them successfully applied, failed-with-no-change, or automatically restored.

The write uses a held file descriptor and cooperative lock, with pre/post checks, preserving file metadata.
It is not a transactional filesystem or an atomic compare-and-swap: another process ignoring locks can
still race; power loss can interrupt a write. Exclusive editing confirmation is therefore required.

Return the CLI outcome to the executing Agent immediately. At its next PreToolUse, the plugin also emits
one short historical outcome via additionalContext; that only records attempted delivery, not receipt.
It does not deny tools or force a Stop continuation. Reporting includes rollback records and confirmation
provenance. No mutation or new evaluator call happens during reporting or notification.
