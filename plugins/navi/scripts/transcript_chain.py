"""Read only host-declared, same-session rollout ancestry, including zstd archives."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

MAX_CHAIN_BYTES = 512 * 1024 * 1024
MAX_SEGMENTS = 32
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'


def header(path):
    with path.open('rb') as stream:
        raw = stream.readline(1024 * 1024)
    row = json.loads(raw)
    if not isinstance(row,dict) or row.get('type') != 'session_meta' or not isinstance(row.get('payload'),dict):
        raise ValueError('chain_session_header_missing')
    return row, hashlib.sha256(raw).hexdigest()


def segment_id(path):
    match = re.search('(' + UUID + r')\.jsonl(?:\.zst)?$', path.name)
    if not match:
        raise ValueError('chain_segment_identity_missing')
    return match[1]


def resolve(root, identity):
    if not isinstance(identity, str) or not re.fullmatch(UUID, identity):
        raise ValueError('invalid_history_base_identity')
    matches = [p for p in root.glob('**/*' + identity + '.jsonl*')
               if p.name.endswith((identity + '.jsonl', identity + '.jsonl.zst'))]
    if len(matches) != 1:
        raise ValueError('history_base_missing_or_ambiguous')
    if matches[0].is_symlink() or not matches[0].resolve().is_relative_to(root.resolve()):
        raise ValueError('history_base_outside_session_store')
    return matches[0]


@contextmanager
def materialize(path, directory):
    """Private bounded scratch files; never alter Codex's logs or load image bytes."""
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('regular_transcript_required')
    if path.suffix != '.zst':
        yield path
        return
    executable = shutil.which('zstd')
    if not executable:
        raise ValueError('compressed_history_requires_zstd')
    target = directory / (path.name + '.decoded')
    # Bound disk allocation as well as runtime, including malformed compressed input.
    import resource
    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_CHAIN_BYTES, MAX_CHAIN_BYTES))
    with target.open('wb') as stream:
        try:
            result = subprocess.run([executable, '-dc', '--', str(path)], stdout=stream,
                stderr=subprocess.DEVNULL, timeout=30, preexec_fn=limits)
        except subprocess.TimeoutExpired as error:
            raise ValueError('compressed_history_timeout') from error
    if result.returncode:
        raise ValueError('compressed_history_decode_failed')
    yield target


def read(target, session, snapshot, previous, commentary_since, parser):
    """A new segment rebuilds the bounded chain; an unchanged leaf resumes its cursor."""
    path = Path(target)
    row, signature = header(path)
    meta = row['payload']
    if meta.get('id') != session:
        raise ValueError('session_identity_mismatch')
    old_chain = (previous or {}).get('chain')
    if (old_chain and old_chain['leaf'] == str(path) and old_chain['header_sha256'] == signature
            and previous['snapshot_identity']['device'] == snapshot['device']
            and previous['snapshot_identity']['inode'] == snapshot['inode']):
        result = parser(target, session, snapshot, previous, commentary_since)
        result['chain'] = old_chain
        return result
    root = next((p for p in path.parents if p.name == 'sessions'), path.parent)
    deadline = time.monotonic() + 60
    with tempfile.TemporaryDirectory(prefix='navi-history-') as temporary:
        scratch = Path(temporary)
        nodes, seen, contexts = [], set(), []
        excluded_live_inputs = []
        current, end, expected_end = path, snapshot['size'], None
        total = 0
        try:
            while True:
                identity = segment_id(current)
                if identity in seen or len(nodes) >= MAX_SEGMENTS:
                    raise ValueError('history_chain_cycle_or_depth')
                seen.add(identity)
                manager = materialize(current, scratch)
                decoded = manager.__enter__()
                contexts.append(manager)
                h, _ = header(decoded);m = h['payload']
                if m.get('id') != session or m.get('session_id',session) != session:
                    raise ValueError('history_chain_session_mismatch')
                if m.get('forked_from_id') or m.get('forked_from') or isinstance(m.get('source'),dict):
                    raise ValueError('fork_or_subagent_history_unsupported')
                base = m.get('history_base')
                if base is not None and not isinstance(base,dict):
                    raise ValueError('unsupported_history_chain')
                start = base.get('end_ordinal_exclusive') if base else 0
                if type(start) is not int or start < 0 or h.get('ordinal') != start:
                    raise ValueError('history_chain_start_ordinal')
                if type(end) is not int or end <= 0 or end > decoded.stat().st_size:
                    raise ValueError('history_chain_byte_boundary')
                total += end
                if total > MAX_CHAIN_BYTES or time.monotonic() > deadline:
                    raise ValueError('history_chain_read_limit')
                # Check the exact referenced prefix, not records appended after the fork boundary.
                count = 0
                with decoded.open('rb') as stream:
                    while stream.tell() < end:
                        raw = stream.readline(min(16*1024*1024+1,end-stream.tell()))
                        if not raw.endswith(b'\n'):
                            raise ValueError('history_chain_incomplete_boundary')
                        record = json.loads(raw)
                        if record.get('ordinal') != start + count:
                            raise ValueError('history_chain_ordinal_gap')
                        count += 1
                        if time.monotonic() > deadline:
                            raise ValueError('history_chain_read_limit')
                if expected_end is not None and start + count != expected_end:
                    raise ValueError('history_chain_end_ordinal')
                if expected_end is not None:
                    # Host restart may exclude an already-hooked submission and replay
                    # it in the new segment. These records are NOT imported authority;
                    # exact identities only reconcile inputs Navi already captured live.
                    from task_state import source_id
                    with decoded.open('rb') as stream:
                        stream.seek(end)
                        for raw in stream:
                            total += len(raw)
                            if total > MAX_CHAIN_BYTES or len(raw)>16*1024*1024 or time.monotonic()>deadline:
                                raise ValueError('history_chain_read_limit')
                            if not raw.endswith(b'\n'):
                                break
                            record=json.loads(raw);value=record.get('payload',{});item=value.get('item',{})
                            if (record.get('type')=='event_msg' and value.get('type')=='item_completed'
                                    and item.get('type')=='UserMessage' and value.get('thread_id')==session):
                                content=item.get('content',[])
                                if content and all(c.get('type') in ('text','local_image') for c in content):
                                    body='\n'.join(c['text'] for c in content if c['type']=='text')
                                    excluded_live_inputs.append(source_id(value['turn_id'],body))
                nodes.append((decoded,current,end))
                if not base:
                    break
                if m.get('history_mode') != 'paginated' or not isinstance(base,dict):
                    raise ValueError('unsupported_history_chain')
                current = resolve(root,base['thread_id'])
                end,expected_end = base['end_byte_offset'],base['end_ordinal_exclusive']
            result = None
            for decoded,original,end in reversed(nodes):
                info = decoded.stat();bound = {'device':info.st_dev,'inode':info.st_ino,'size':end}
                carry = copy.deepcopy(result) if result else None
                if carry:
                    carry.update(read_bytes=0,lines_read=0,snapshot_identity=bound,
                                 prefix_sha256=hashlib.sha256(b'').hexdigest())
                result = parser(str(decoded),session,bound,carry,0)
                if result['status'] != 'ready':
                    return result
            # Rebinding an old inode requires all retained canonical user evidence to agree.
            # This supports host migration/compression without accepting a rewritten requirement.
            if previous:
                old_users = [(m['id'],m['turn_id'],m['text']) for m in previous['messages'] if m['role']=='user']
                new_users = [(m['id'],m['turn_id'],m['text']) for m in result['messages'] if m['role']=='user']
                if new_users[:len(old_users)] != old_users:
                    raise ValueError('history_chain_user_prefix_changed')
            result['snapshot_identity'] = snapshot
            result['parsed_new_bytes'] = total
            result['chain'] = {'leaf':str(path),'header_sha256':signature,'segments':len(nodes),
                               'excluded_live_inputs':excluded_live_inputs,
                               'verification':'declared_same_session_prefixes_and_contiguous_ordinals'}
            return result
        finally:
            for manager in reversed(contexts):
                manager.__exit__(None,None,None)
