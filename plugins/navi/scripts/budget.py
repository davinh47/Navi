"""Continuous review policy and bounded local audit retention; no model calls."""

POLICY_VERSION = 2
KEEP_REVIEWS = 16
KEEP_REMINDERS = 48


class ReviewDeferred(ValueError):
    """No call was reserved; an eligible event may retry after the gate clears."""


def migrate(state):
    budget = state['budget']
    if budget.get('policy_version') == POLICY_VERSION:
        return
    old = budget.get('review_limit')
    # Old CLI profiles supplied these fixed defaults; preserve other/custom ceilings.
    budget.update(policy_version=POLICY_VERSION, review_limit=None if old in (None, 3, 24) else old,
                  limit_origin='default' if old in (None, 3, 24) else 'legacy_custom',
                  previous_review_limit=old, reminder_limit=None)
    budget.setdefault('review_times', [j['created_at'] for j in state.get('reviews', {}).values()
                                     if 'created_at' in j])


def limits(state):
    return (180, None, 300) if state.get('supervisor', {}).get('profile') == 'long-task' else (30, None, 60)


def remaining(state):
    ceiling = state['budget'].get('review_limit')
    return None if ceiling is None else max(0, ceiling - state['budget']['reviews_used'])


def review_gate(state, supervision_token=None):
    if remaining(state) == 0:
        return {'reason': 'review_budget_exhausted', 'resumes_at': None}
    # A per-session lock protects callers. Manual and automatic reviews share one slot,
    # including the detached automatic worker's pre-reservation window.
    worker = state.get('supervisor', {}).get('worker')
    if (any(j.get('status') in ('queued', 'running') for j in state.get('reviews', {}).values())
            or worker and worker.get('token') != supervision_token):
        return {'reason': 'review_in_progress', 'resumes_at': None}
    return None


def usage(jobs):
    totals = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}
    known, missing, incomplete = [], [], []
    for key, job in jobs.items():
        raw = (job.get('output') or {}).get('usage')
        if not isinstance(raw, dict):
            if job['status'] not in ('queued', 'cancelled_before_call', 'superseded_before_call') and job.get('error_type') != 'SpawnError':
                missing.append(key)
            continue
        complete = True
        for field in totals:
            number = raw.get(field)
            if type(number) is int and number >= 0:
                totals[field] += number
            else:
                complete = False
        known.append(key)
        if not complete:
            incomplete.append(key)
    return {**totals, 'reported_input_plus_output': totals['input_tokens']+totals['output_tokens'],
            'usage_reported_review_ids': known, 'usage_missing_review_ids': missing,
            'usage_incomplete_review_ids': incomplete, 'complete_for_finished_reviews': not missing and not incomplete,
            'cached_input_is_subset': True, 'reasoning_output_already_in_output': True,
            'main_agent_usage': 'not_collected', 'report_generation_model_calls': 0}


def total_usage(state):
    current = usage(state.get('reviews', {}))
    old = state.get('review_rollup', {})
    for key in ('input_tokens', 'cached_input_tokens', 'output_tokens'):
        current[key] += old.get(key, 0)
    current['reported_input_plus_output'] = current['input_tokens'] + current['output_tokens']
    current['omitted_review_usage'] = {k: old.get(k, 0) for k in
        ('reviews', 'reported', 'missing', 'incomplete')}
    current['complete_for_finished_reviews'] &= not old.get('missing') and not old.get('incomplete')
    return current


def trim_reviews(state):
    """Keep recent details; never evict in-flight or unconsumed automatic jobs."""
    jobs = state.setdefault('reviews', {})
    while len(jobs) >= KEEP_REVIEWS:
        eligible = [(k, j) for k, j in jobs.items() if j['status'] not in ('queued', 'running')
                    and (not j.get('supervision_token') or j.get('supervision_processed'))]
        if not eligible:
            raise ValueError('review storage occupied by unfinished jobs; main task continues')
        key, job = min(eligible, key=lambda item: item[1].get('created_at', 0))
        counts = state.setdefault('review_rollup', {})
        counts['reviews'] = counts.get('reviews', 0) + 1
        for field, condition in [('started', job['status'] in ('completed', 'failed') and job.get('error_type') != 'SpawnError'),
                                 ('completed', job['status'] == 'completed')]:
            counts[field] = counts.get(field, 0) + int(condition)
        totals = usage({key: job})
        for field in ('input_tokens', 'cached_input_tokens', 'output_tokens'):
            counts[field] = counts.get(field, 0) + totals[field]
        for field, source in [('reported', 'usage_reported_review_ids'), ('missing', 'usage_missing_review_ids'),
                              ('incomplete', 'usage_incomplete_review_ids')]:
            counts[field] = counts.get(field, 0) + len(totals[source])
        del jobs[key]


def remember_finding(state, entry):
    if not entry.get('finding_key') or not entry.get('emitted_at'):
        return
    transport = state['reminder_transport']
    context = entry['payload']['user_context_digest']
    seen = {k: v for k, v in transport.get('seen_findings', {}).items() if v['context'] == context}
    seen[entry['finding_key']] = {'context': context, 'at': entry['emitted_at']}
    transport['seen_findings'] = dict(sorted(seen.items(), key=lambda item: item[1]['at'])[-256:])


def trim_reminders(state):
    transport = state['reminder_transport']
    entries = transport['entries']
    # Preserve entries referenced by queued/running or unconsumed followups.
    pinned = {j['followup']['reminder_id'] for j in state.get('reviews', {}).values()
              if j.get('followup') and (j['status'] in ('queued', 'running') or not j.get('supervision_processed'))}
    while len(entries) >= KEEP_REMINDERS:
        eligible = [(k, e) for k, e in entries.items() if k not in pinned]
        if not eligible:
            raise ValueError('reminder storage occupied by active followups')
        key, entry = min(eligible, key=lambda item: item[1]['created_at'])
        # Persist a separate bounded deduplication identity before discarding full details.
        if not transport.get('seen_findings') or entry.get('payload',{}).get('user_context_digest') in {v['context'] for v in transport['seen_findings'].values()}:
            remember_finding(state, entry)
        rollup = transport.setdefault('omitted', {'entries': 0, 'status_counts': {}})
        rollup['entries'] += 1
        counts = rollup['status_counts']
        status = 'pending_evicted' if entry['status'] == 'pending' else entry['status']
        counts[status] = counts.get(status, 0) + 1
        del entries[key]
