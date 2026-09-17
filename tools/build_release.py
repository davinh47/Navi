#!/usr/bin/env python3
"""Build a local reviewable plugin archive; no publishing, installation or configuration writes."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def build(output):
    source=ROOT/'plugins/navi';version=json.loads((source/'.codex-plugin/plugin.json').read_text())['version']
    output.mkdir(parents=True,exist_ok=True)
    target=output/('navi-'+version+'.zip');checks={}
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                if path.is_symlink():raise ValueError('symlinks not packaged')
                relative='navi/'+str(path.relative_to(source));raw=path.read_bytes()
                archive.writestr(relative,raw);checks[relative]=hashlib.sha256(raw).hexdigest()
        archive.write(ROOT/'docs/INSTALL.md','INSTALL.md')
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        for name,sha in checks.items():assert hashlib.sha256(archive.read(name)).hexdigest()==sha
    result={'status':'built_not_published','version':version,'archive':str(target.resolve()),
            'archive_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'files':checks,
            'scope':'Experimental Codex plugin. See public validation and platform limitations.'}
    (output/'release-manifest.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    print(json.dumps(build(parser.parse_args().output),indent=2))
