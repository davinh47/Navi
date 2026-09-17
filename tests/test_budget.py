import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/navi/scripts'))
import budget
import task_state


class BudgetTests(unittest.TestCase):
    def test_legacy_migration_preserves_usage_and_known_custom_limits(self):
        for limit in (3,24,8):
            state=task_state.new_state('migration')
            state['budget']={'review_limit':limit,'reviews_used':24,'reminders_used':12,'reminder_limit':12}
            state['reviews']={'old':{'created_at':100}}
            budget.migrate(state)
            self.assertEqual(state['budget']['reviews_used'],24)
            self.assertEqual(state['budget']['reminders_used'],12)
            self.assertEqual(state['budget']['review_limit'],None if limit in (3,24) else limit)
            self.assertEqual(state['budget']['review_times'],[100])
            before=copy.deepcopy(state);budget.migrate(state)
            self.assertEqual(before,state)

    def test_new_explicit_three_is_not_migrated_away(self):
        state=task_state.new_state('new');state['budget'].update(review_limit=3,limit_origin='explicit')
        budget.migrate(state)
        self.assertEqual(state['budget']['review_limit'],3)

    def test_review_hourly_window_exact_boundary_and_custom_budget_precedence(self):
        state=task_state.new_state('rate')
        state['budget']['review_times']=[100,101,102,103,104,105]
        self.assertEqual(budget.review_gate(state,3699)['resumes_at'],3700)
        self.assertIsNone(budget.review_gate(state,3700))
        state['budget'].update(review_limit=0)
        self.assertEqual(budget.review_gate(state,3700)['reason'],'review_budget_exhausted')

    def test_review_rollup_preserves_missing_usage_without_inventing_zero(self):
        state=task_state.new_state('rollup')
        state['reviews']={str(i):{'status':'failed','created_at':i} for i in range(budget.KEEP_REVIEWS)}
        state['reviews']['0']['output']={'usage':{'input_tokens':10,'cached_input_tokens':4,'output_tokens':2}}
        budget.trim_reviews(state)
        state['reviews']['next']={'status':'completed','created_at':100,'output':{'usage':{'input_tokens':20,'cached_input_tokens':5,'output_tokens':3}}}
        budget.trim_reviews(state)
        result=budget.total_usage(state)
        self.assertEqual(result['reported_input_plus_output'],35)
        self.assertEqual(result['cached_input_tokens'],9)
        self.assertEqual(result['omitted_review_usage']['missing'],1)
        self.assertFalse(result['complete_for_finished_reviews'])
        self.assertEqual(state['review_rollup']['reviews'],2)

    def test_unfinished_reviews_are_not_evicted(self):
        state=task_state.new_state('running')
        state['reviews']={str(i):{'status':'running','created_at':i} for i in range(budget.KEEP_REVIEWS)}
        before=copy.deepcopy(state)
        with self.assertRaises(ValueError):budget.trim_reviews(state)
        self.assertEqual(before,state)
        state['reviews']['0'].update(status='completed',supervision_token='worker')
        with self.assertRaises(ValueError):budget.trim_reviews(state)
        state['reviews']['0']['supervision_processed']=True
        budget.trim_reviews(state)
        self.assertNotIn('0',state['reviews'])

    def test_reminder_rollover_protects_running_followup_and_reports_eviction(self):
        state=task_state.new_state('reminders')
        state['reminder_transport']={'entries':{str(i):{'created_at':i,'status':'pending'} for i in range(budget.KEEP_REMINDERS)}}
        state['reviews']={'followup':{'status':'running','followup':{'reminder_id':'0'}}}
        budget.trim_reminders(state)
        self.assertIn('0',state['reminder_transport']['entries'])
        self.assertNotIn('1',state['reminder_transport']['entries'])
        self.assertEqual(state['reminder_transport']['omitted']['status_counts'],{'pending_evicted':1})


if __name__=='__main__':unittest.main()
