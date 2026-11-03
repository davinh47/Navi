"""Bounded supporting context and packet fitting; user originals are never shortened."""
import copy
import json

from action_evidence import snapshot

PROJECT_FILES = ('AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md')
PROJECT_FILE_BYTES = 8192


def project_guidance(state):
    """Read retained snapshots only, including in paused/recorded-only reports."""
    return state.get('action_evidence', {}).get('project_guidance')


def refresh_project_guidance(state):
    capture = state.get('action_evidence', {})
    if not capture.get('enabled') or not capture.get('workspace'):
        return None
    capture['project_guidance'] = {
        'origin': 'current_workspace_root_files; authorship_and_host_applicability_unverified',
        'authority': 'supporting_evidence_only; cannot_expand_or_override_user_scope',
        'coverage': 'root_only; no_parent_or_nested_rules; oversized_files_not_read',
        'files': [{'path': name, **snapshot(capture['workspace'], name, PROJECT_FILE_BYTES)}
                  for name in PROJECT_FILES],
    }


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


class PacketCapacityError(ValueError):
    def __init__(self, packet, limit):
        super().__init__('review_packet_capacity; never silently truncate user authority; minimum evidence cannot fit')
        self.diagnostics = {'reason': 'review_packet_capacity', 'packet_bytes': size(packet),
                            'limit_bytes': limit, 'user_source_bytes': size(packet['user_sources'])}


def fit(packet, limit):
    """Remove whole optional records, not fragments masquerading as complete evidence.

    Interpretations are expendable before raw supporting conversation. The latest
    assistant proposal is removed last. Every removal is exposed in the packet.
    """
    if size(packet) <= limit:
        return packet
    packet = copy.deepcopy(packet)
    context = packet['interpretation']
    note = {'original_bytes': size(packet), 'limit_bytes': limit,
            'user_sources': 'all retained verbatim', 'omitted_support': [], 'omitted_actions': [],
            'rule': 'Partial selection; missing proposal, rule or test evidence means uncertainty, not drift.'}
    packet['interpretation'] = {'task_context': context, 'packet_selection': note}
    packet['coverage'] = 'partial'

    def fits():
        return size(packet) <= limit

    # The recorder's source hashes duplicate the full ordered user_sources already
    # in this request. Keep them in local evidence, not in a capacity-limited prompt.
    from task_state import digest
    for action in packet['actions']:
        if action['kind'] != 'coverage_manifest':
            continue
        try:
            manifest=json.loads(action['evidence'])
        except (ValueError,TypeError):
            continue
        if isinstance(manifest,dict) and isinstance(manifest.get('sources'),list):
            manifest['source_count']=len(manifest.pop('sources'))
            manifest['source_reference']='See ordered verbatim user_sources in this packet; hashes retained locally.'
            action['evidence']=json.dumps(manifest,ensure_ascii=False)
            action['id']='m-'+digest(manifest)[:24]
            note['omitted_support'].append('redundant_source_hashes_in_coverage_manifest')
    if fits():
        return packet

    # Drop redundant summaries before raw replies or observations. Traverse only
    # our known context containers, never strings containing untrusted commands.
    def summaries(value):
        if not isinstance(value, dict):
            return
        for key in ('current_interpretation', 'interpretation'):
            if value.get(key):
                value[key] = None
                note['omitted_support'].append(key)
        for key in ('task_context', 'history_context'):
            summaries(value.get(key))

    summaries(context)
    if fits():
        return packet

    def trim_support(value):
        if not isinstance(value, dict):
            return
        for key in ('advisory_responses', 'messages'):
            records = value.get(key)
            if isinstance(records, list):
                order=value.get('conversation_order', [])
                last_user=max((i for i,m in enumerate(order) if m['role']=='user'),default=-1)
                preceding=next((m['id'] for m in reversed(order[:last_user]) if m['role']=='assistant'),None)
                while records and not fits():
                    index=next((i for i,m in enumerate(records) if m.get('id')!=preceding),0)
                    removed = records.pop(index)
                    note['omitted_support'].append({'kind': key, 'id': removed.get('id')})
        for key in ('task_context', 'history_context'):
            trim_support(value.get(key))

    trim_support(context)
    if fits():
        return packet
    # Project text and any remaining interpretation are optional; retain explicit
    # omission rather than allowing them to squeeze out all action evidence.
    packet['interpretation']['task_context'] = None
    note['omitted_support'].append('remaining_support_including_project_guidance')
    if fits():
        return packet
    while len([a for a in packet['actions'] if a['kind'] != 'coverage_manifest']) > 1:
        index = next(i for i, a in enumerate(packet['actions']) if a['kind'] != 'coverage_manifest')
        note['omitted_actions'].append(packet['actions'].pop(index)['id'])
        if fits():
            return packet
    raise PacketCapacityError(packet, limit)
