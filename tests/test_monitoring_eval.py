import copy
import json
import io
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import run_monitoring_eval as evaluation
import build_monitoring_cases


class MonitoringEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cases,self.old,self.hashes=evaluation.frozen_inputs()

    def prediction(self,case,scope=None,risk=None):
        expected=case['expected']
        label=expected['required_label']
        return {'case_id':case['packet']['case_id'],'verdict':scope or expected['scope'],
            'scope_labels':[label] if label in evaluation.review.SCOPE_LABELS else [],
            'evidence_ids':['u1','a1'],'reason':'test',
            'implementation_risk':{'verdict':risk or expected['implementation_risk'],
                'labels':[label] if label in evaluation.review.RISK_LABELS else [],'evidence_ids':['u1','a1'],'reason':'test'}}

    def test_live_entry_uses_explicit_test_model_in_a_clean_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(evaluation, 'ROOT', root), \
                 patch.object(evaluation, 'frozen_inputs', return_value=(self.cases, self.old, self.hashes)), \
                 patch.object(evaluation.review, 'run_model', return_value={'result': {'results': []}}) as model, \
                 patch.object(sys, 'argv', ['run_monitoring_eval.py', '--live']), redirect_stdout(io.StringIO()):
                evaluation.main()
            self.assertEqual(model.call_count, 7)
            for call in model.call_args_list:
                self.assertEqual(call.kwargs, {'model': 'gpt-5.6-luna', 'effort': 'low'})
            manifest = json.loads(next((root/'.navi-dev').glob('*/manifest.json')).read_text())
            self.assertEqual((manifest['model'], manifest['effort']), ('gpt-5.6-luna', 'low'))

    def test_frozen_fixture_is_reproducible_and_all_types_have_three_conditions(self):
        self.assertEqual(build_monitoring_cases.build(),json.loads((ROOT/'evals/monitoring/cases.json').read_text()))
        self.assertEqual(len(self.cases),30)
        self.assertEqual(len(self.old),12)

    def test_batches_have_no_gold_labels_and_only_held_out_repeats(self):
        jobs=evaluation.plan(self.cases,self.old)
        self.assertEqual(len(jobs),7)
        counts={}
        for job in jobs:
            self.assertLessEqual(len(job['packets']),12)
            for packet in job['packets']:
                self.assertEqual(set(packet),{'case_id','user_sources','interpretation','coverage','actions'})
                self.assertNotIn('expected',json.dumps(packet))
                counts[packet['case_id']]=counts.get(packet['case_id'],0)+1
        for case in self.cases+self.old:
            repeat=2 if case in self.cases and case['split']=='held_out' else 1
            self.assertEqual(counts[case['packet']['case_id']],repeat)

    def test_uncertain_positive_is_counted_as_miss(self):
        case=next(c for c in self.cases if c['expected']['scope']=='drift')
        result=evaluation.score([case],[self.prediction(case,scope='uncertain')])
        self.assertEqual(result['scope']['misses_including_uncertain_or_missing'],1)
        self.assertEqual(result['scope']['uncertain_predictions'],1)

    def test_unsupported_unknown_is_separate_from_false_positive_on_clean(self):
        unknown=next(c for c in self.cases if c['polarity']=='unknown')
        clean=next(c for c in self.cases if c['expected']['implementation_risk']=='none_observed')
        result=evaluation.score([unknown,clean],[self.prediction(unknown,risk='concern'),self.prediction(clean,risk='concern')])
        self.assertEqual(result['implementation_risk']['false_positives_on_clean'],1)
        self.assertEqual(result['implementation_risk']['unsupported_flags_on_unknown'],1)

    def test_in_scope_risk_and_required_label_are_scored_separately(self):
        case=next(c for c in self.cases if c['expected']['implementation_risk']=='concern')
        predicted=self.prediction(case)
        predicted['implementation_risk']['labels']=[]
        result=evaluation.score([case],[predicted])
        self.assertEqual(result['scope']['exact'],1)
        self.assertEqual(result['implementation_risk']['exact'],1)
        self.assertEqual(result['required_labels_matched'],0)
        self.assertEqual(len(result['mismatches']),1)

    def test_failed_batch_is_missing_not_clean_and_usage_not_zeroed(self):
        manifest={'cases':self.cases,'legacy':self.old,'jobs':evaluation.plan(self.cases,self.old)}
        output={manifest['jobs'][0]['id']:{'error_type':'ReviewFailure','usage':{'input_tokens':20,'output_tokens':5}}}
        summary=evaluation.summarize(manifest,output)
        self.assertEqual(summary['status'],'incomplete')
        self.assertEqual(summary['total_input_plus_output'],25)
        self.assertEqual(summary['evaluations']['development-1']['total']['scope']['missing_predictions'],15)
        self.assertEqual(summary['legacy']['scope_exact'],0)


if __name__=='__main__':
    unittest.main()
