# Data and privacy

Installing Navi enables its installed hook processes subject to host trust. The default recorder
stores bounded event metadata such as session IDs, event names and working-directory paths. This is
not anonymous telemetry. Navi does not operate a separate telemetry endpoint.

Task input capture requires workspace opt-in. Tool content/file observation and history reading
require separate authorization. Opted-in records can contain user messages, source code, absolute
paths, command inputs/outputs and selected same-conversation replies. Keep the plugin data directory
and any exported reports private.

## Local storage and external inference

Records live in the host-provided `PLUGIN_DATA` directory. Independent model calls run through Codex
using the existing login; Navi does not copy credentials. **When enabled, review/history model requests
send selected evidence to the configured Codex model service.** Local storage does not mean inference
is local-only. Provider/account data policies still apply.

Raw tool logs, model chain-of-thought and compaction summaries are not imported as user authorization.
Unknown message provenance is a gap, not permission to reconstruct or invent user requirements.

## Retention and deletion

Task data expires seven days after its last saved update, with lazy cleanup on access/pruning rather
than a permanent deletion daemon. Activity can extend that retention. Metadata logs rotate at roughly
3 MiB per session; the number of sessions is not automatically bounded. `forget` removes the task state
and its recovery records. It does not erase Codex-owned transcripts, metadata logs or exported reports.
Detailed review/reminder records also roll over at 16/48 entries. Reports expose omitted-detail counts;
cumulative counters and available usage remain until task expiry/forget. Evicted details are not archived.

Pause stops Navi recording and scheduling, while the host may still launch hook processes.

Use the host's actual data directory; do not publish it. Report contents can contain private code even
when the original operation was read-only. Review and redact artifacts before sharing them in issues.
