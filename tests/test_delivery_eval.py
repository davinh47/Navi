import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import run_delivery_eval as evaluation


class DeliveryEvaluationTests(unittest.TestCase):
    def prediction(self,case):
        expected=case['expected'];ids=['u1','a1']
        return {'case_id':case['packet']['case_id'],'verdict':expected['scope'],
                'scope_labels':[expected['required_label']] if expected['required_label'] else [],
                'evidence_ids':ids,'reason':'Authored response fixture; not a model evaluation.',
                'implementation_risk':{'verdict':'uncertain','labels':[],'evidence_ids':ids,'reason':'Not scored here.'}}

    def test_each_delivery_category_has_positive_negative_and_missing_evidence(self):
        cases=evaluation.cases()
        self.assertEqual(len(cases),9)
        for family in evaluation.review.DELIVERY_LABELS:
            self.assertEqual(sorted(c['polarity'] for c in cases if c['family']==family),
                             ['negative','positive','unknown'])

    def test_delivery_labels_require_real_user_and_action_citations(self):
        for case in evaluation.cases():
            if case['polarity']!='positive':continue
            result=self.prediction(case)
            evaluation.review.validate_results({'results':[result]},[case['packet']])
            for ids in (['u1'],['a1'],['u1','invented']):
                invalid=copy.deepcopy(result);invalid['evidence_ids']=ids
                with self.assertRaises(ValueError):
                    evaluation.review.validate_results({'results':[invalid]},[case['packet']])

    def test_missing_or_uncertain_detection_is_not_scored_as_success(self):
        case=next(c for c in evaluation.cases() if c['polarity']=='positive')
        self.assertEqual(evaluation.score([case],[])['matched'],0)
        result=self.prediction(case);result['verdict']='uncertain'
        self.assertEqual(evaluation.score([case],[result])['matched'],0)
        result['verdict']='drift';result['scope_labels']=[]
        self.assertEqual(evaluation.score([case],[result])['matched'],0)

    def test_live_harness_makes_one_call_without_expected_answers(self):
        cases=evaluation.cases()
        response={'result':{'results':[self.prediction(c) for c in cases]},'usage':{'input_tokens':10,'output_tokens':5}}
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/'run'
            with patch.object(evaluation.review,'run_model',return_value=response) as model, \
                 patch.object(sys,'argv',['run_delivery_eval.py','--live','--output',str(output)]), \
                 redirect_stdout(io.StringIO()):
                evaluation.main()
            self.assertEqual(model.call_count,1)
            self.assertEqual(model.call_args.kwargs,{'model':evaluation.review.MODEL,'effort':evaluation.review.EFFORT})
            packets=model.call_args.args[0]
            self.assertEqual(packets,[c['packet'] for c in cases])
            self.assertTrue(all('expected' not in p and 'polarity' not in p for p in packets))
            self.assertEqual(json.loads((output/'summary.json').read_text())['matched'],9)


if __name__=='__main__':unittest.main()
