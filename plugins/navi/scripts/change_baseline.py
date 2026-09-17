"""Bounded, read-only Git references for explicitly watched files, captured only at opt-in."""
import os
from pathlib import Path
import subprocess
import time

from task_state import digest


def references(workspace, names, limit):
    unavailable = {name:{'status':'unavailable','reason':'git_reference_unavailable'} for name in names}
    if not names:
        return unavailable
    env = {k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    git_directory = workspace
    def git(*args):
        return subprocess.run(['git','--no-pager','--literal-pathspecs','-C',str(git_directory),
            '-c','core.fsmonitor=false',*args],check=True,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env=env,timeout=2).stdout
    try:
        root = Path(os.fsdecode(git('rev-parse','--show-toplevel').rstrip(b'\n')))
        prefix = Path(workspace).relative_to(root)
        git_directory = root
        commit = git('rev-parse','--verify','HEAD').decode().strip()
        result = {}
        for name in names:
            path = str(prefix / name)
            record = {'commit':commit,'captured_at':time.time()}
            entries = git('ls-tree','-z','-l',commit,'--',path).split(b'\0')
            entries = [e for e in entries if e]
            if not entries:
                result[name] = dict(record,status='absent')
                continue
            if len(entries)!=1:
                result[name] = dict(record,status='unavailable',reason='not_single_file')
                continue
            metadata,entry_name=entries[0].split(b'\t',1)
            mode,kind,oid,size=metadata.split()
            if os.fsdecode(entry_name)!=path or mode not in (b'100644',b'100755') or kind!=b'blob':
                result[name] = dict(record,status='unavailable',reason='not_regular_git_blob')
                continue
            if int(size)>limit:
                result[name] = dict(record,status='oversized',size=int(size))
                continue
            raw=git('cat-file','blob',oid.decode())
            if len(raw)>limit or b'\0' in raw:
                result[name]=dict(record,status='unavailable',reason='binary_or_oversized')
                continue
            result[name]=dict(record,status='present',text=raw.decode('utf-8'),sha256=digest(raw.hex()))
        return result
    except (OSError,subprocess.SubprocessError,ValueError,UnicodeError):
        return unavailable
