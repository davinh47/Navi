#!/usr/bin/env python3
"""One optional bounded delivery-review replay; no executing agent, hooks or background scheduling."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/navi/scripts'))
import review


def cases():
    values=json.loads((ROOT/'evals/delivery_cases.json').read_text())['cases']
    ids=[c['packet']['case_id'] for c in values]
    if len(ids)!=len(set(ids)):
        raise ValueError('duplicate delivery cases')
    for case in values:
        review.validate_packet(case['packet'])
    return values


def score(cases, results):
    predictions={r['case_id']:r for r in results}
    mismatches=[]
    for case in cases:
        expected=case['expected'];result=predictions.get(case['packet']['case_id'])
        if (not result or result['verdict']!=expected['scope'] or
                expected['required_label'] and expected['required_label'] not in result['scope_labels']):
            mismatches.append({'case_id':case['packet']['case_id'],'expected':expected,'actual':result})
    return {'cases':len(cases),'matched':len(cases)-len(mismatches),'mismatches':mismatches,
            'meaning':'Authored scope/classification checks only; not real-task detection accuracy.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--model',default=review.MODEL)
    parser.add_argument('--effort',default=review.EFFORT)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();selected=cases()
    if not args.live:
        print(json.dumps({'cases':len(selected),'model_calls':0,'live_calls_if_requested':1}))
        return
    output=args.output or ROOT/'.navi-dev'/('delivery-'+str(time.time_ns()))
    output.mkdir(parents=True,exist_ok=False)
    manifest={'cases':selected,'model':args.model,'effort':args.effort,'prompt_version':review.PROMPT_VERSION,
              'review_sha256':hashlib.sha256((ROOT/'plugins/navi/scripts/review.py').read_bytes()).hexdigest()}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    try:
        result=review.run_model([c['packet'] for c in selected],model=args.model,effort=args.effort)
    except Exception as error:
        (output/'failure.json').write_text(json.dumps({'error_type':type(error).__name__,
            'diagnostics':getattr(error,'diagnostics',None),'usage_may_have_been_consumed':True},indent=2)+'\n')
        raise
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    summary={**score(selected,result['result']['results']),'usage':result.get('usage'),'model_calls':1}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'output':str(output),**summary},ensure_ascii=False))


if __name__=='__main__':
    main()
