import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/navi/scripts'))
import action_evidence as actions
import activity_scope
import reminders
import report
import review
import supervisor
import task_state as tasks


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.work=self.root/'work';self.work.mkdir()
        self.data=self.root/'data';self.session='activity';self.serial=0;self.dispatched=[]
        tasks.on_hook(self.data,self.payload('UserPromptSubmit',prompt='Navi: start\nOnly implement the requested display; do not investigate or change authentication.'))
        actions.configure(self.data,self.session,str(self.work),[])
        supervisor.configure(self.data,self.session,True,True)
        reminders.on_hook(self.data,self.payload('UserPromptSubmit'))
    def tearDown(self):self.tmp.cleanup()
    def payload(self,event,**kw):return dict(session_id=self.session,turn_id='turn',hook_event_name=event,**kw)
    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def store(self,s):tasks.save(tasks.paths(self.data,self.session)[1],s)
    def event(self,tool='Bash',value='inspect authentication issue'):
        self.serial+=1
        for event in ('PreToolUse','PostToolUse'):
            actions.on_hook(self.data,self.payload(event,tool_use_id=str(self.serial),tool_name=tool,
                tool_input={'command':value},**({'tool_response':'observed result'} if event=='PostToolUse' else {})))
    def batch(self):
        for _ in range(4):self.event()
    def dispatch(self):
        supervisor.on_hook(self.data,self.payload('PostToolUse'),lambda d,s,t:self.dispatched.append(t))
    def runner(self,packets,**kw):
        p=packets[0];ids=[p['user_sources'][0]['id'],next(a['id'] for a in p['actions'] if a['kind']=='tool_observation')]
        result={'case_id':p['case_id'],'verdict':'drift','scope_labels':['unrelated_work'],'evidence_ids':ids,'reason':'Historical investigation exceeds authorized display task.',
                'implementation_risk':{'verdict':'uncertain','labels':[],'evidence_ids':ids,'reason':'No implementation evidence.'}}
        value={'results':[result]};review.validate_results(value,packets)
        return {'result':value,'usage':{'input_tokens':12,'output_tokens':5,'cached_input_tokens':0}}
    def finish(self,runner=None):supervisor.worker(self.data,self.session,self.dispatched[-1],runner or self.runner,delay=0)

    def test_explicit_activity_optin_only(self):
        s=self.state();s['supervisor']['include_activity']=False;self.store(s);self.batch();self.dispatch()
        self.assertEqual(self.dispatched,[])

    def test_delivery_advisory_from_plan_evidence_discloses_gap(self):
        self.event('update_plan','Remove a required capability from the deliverable');self.dispatch()
        def delivery(packets,**kwargs):
            value=self.runner(packets,**kwargs)
            value['result']['results'][0]['scope_labels']=['scope_reduction']
            review.validate_results(value['result'],packets)
            return value
        self.finish(delivery)
        emitted=reminders.on_hook(self.data,self.payload('PreToolUse',tool_use_id='next'))
        self.assertIn('clearly report the missing or substituted capability',str(emitted))
        self.assertIn('If already resolved',str(emitted))
        gaps=report.generate(self.data,self.session)['delivery_gaps']
        self.assertEqual(gaps[0]['freshness'],'historical_observations')
        self.assertEqual(self.state()['budget']['reviews_used'],1)
    def test_batch_threshold_avoids_per_tool_reviews(self):
        for _ in range(3):self.event();self.dispatch()
        self.assertEqual(self.dispatched,[])
        self.event();self.dispatch();self.finish()
        self.assertEqual(self.state()['budget']['reviews_used'],1)
    def test_plan_protocol_triggers_without_file_change(self):
        self.event('update_plan','Investigate authentication after updating display');self.dispatch();self.finish()
        job=next(iter(self.state()['reviews'].values()))
        self.assertEqual(job['origin'],'automatic_activity_checkpoint');self.assertFalse('file_basis' in job)
    def test_arbitrary_observed_patch_to_unwatched_file_is_evidence_not_success(self):
        self.event('apply_patch','*** Update File: unwatched.py\n-old\n+new');self.batch();self.dispatch();self.finish()
        job=next(iter(self.state()['reviews'].values()))
        self.assertTrue(all(a['kind']!='workspace_observation' for a in job['packet']['actions']))
        self.assertEqual(job['observation_scope'],'historical_attempts_not_current_behavior')
    def test_new_tool_traffic_keeps_historical_evidence_with_conditional_advisory(self):
        self.batch();self.dispatch()
        def ongoing(p,**kw):self.event('Bash','read display');return self.runner(p,**kw)
        self.finish(ongoing);s=self.state();job=next(iter(s['reviews'].values()))
        self.assertFalse(job['stale']);e=next(iter(s['reminder_transport']['entries'].values()))
        self.assertIn('If already resolved',e['payload']['suggestion'])
        self.assertEqual(e['origin'],'independent_activity_review')
        r=report.generate(self.data,self.session)
        self.assertEqual(r['reviews'][0]['freshness'],'historical_observations')
        self.assertEqual(r['summary']['current_scope_drift_judgments'],0)
    def test_new_plan_supersedes_old_plan_advisory(self):
        self.event('update_plan','unrelated investigation');self.dispatch()
        def changed(p,**kw):self.event('update_plan','return to display only');return self.runner(p,**kw)
        self.finish(changed);self.assertFalse(self.state()['reminder_transport']['entries'])
    def test_source_update_supersedes_activity_review(self):
        self.batch();self.dispatch()
        def changed(p,**kw):
            tasks.on_hook(self.data,dict(self.payload('UserPromptSubmit',prompt='Now investigate authentication.'),turn_id='turn2'))
            return self.runner(p,**kw)
        self.finish(changed);self.assertFalse(self.state()['reminder_transport']['entries'])
    def test_watched_file_correction_invalidates_historical_activity(self):
        target=self.work/'view.py';target.write_text('before');actions.configure(self.data,self.session,str(self.work),['view.py'])
        self.batch();self.dispatch()
        def changed(p,**kw):target.write_text('corrected');return self.runner(p,**kw)
        self.finish(changed);self.assertFalse(self.state()['reminder_transport']['entries'])
    def test_aged_and_evicted_evidence_is_not_delivered(self):
        self.batch();self.dispatch();self.finish();s=self.state();entry=next(iter(s['reminder_transport']['entries'].values()))
        entry['activity_cutoff']=time.time()-121;self.store(s)
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse',tool_use_id='next')))
        s=self.state();job=next(iter(s['reviews'].values()));s['action_evidence']['events']=[]
        self.assertTrue(review.is_stale(s,job))
    def test_shared_budget_and_no_repeated_activity_notice(self):
        self.batch();self.dispatch();self.finish()
        reminders.on_hook(self.data,self.payload('PreToolUse',tool_use_id='next'))
        self.batch();s=self.state();s['supervisor']['last_dispatch_at']=0;self.store(s);self.dispatch();self.finish()
        self.assertEqual(len(self.state()['reminder_transport']['entries']),1)
        self.batch();s=self.state();s['supervisor']['last_dispatch_at']=0;self.store(s);self.dispatch();self.finish()
        self.assertEqual(len(self.dispatched),3);self.assertEqual(self.state()['budget']['reviews_used'],3)
    def test_late_result_records_only_without_forced_turn(self):
        self.batch();self.dispatch()
        def ended(p,**kw):reminders.on_hook(self.data,self.payload('Stop'));return self.runner(p,**kw)
        self.finish(ended)
        self.assertEqual(next(iter(self.state()['reminder_transport']['entries'].values()))['status'],'report_only_late')
    def test_risk_only_is_not_activity_scope_advisory(self):
        self.batch();self.dispatch()
        def risk(p,**kw):
            out=self.runner(p,**kw);r=out['result']['results'][0];r.update(verdict='uncertain',scope_labels=[])
            r['implementation_risk'].update(verdict='concern',labels=['overengineering']);return out
        self.finish(risk);self.assertFalse(self.state()['reminder_transport']['entries'])
    def test_no_unchanged_snapshot_retry_and_packet_bound(self):
        for _ in range(9):self.event(value='x'*3000)
        self.dispatch();self.finish();self.dispatch()
        self.assertEqual(len(self.dispatched),1)
        job=next(iter(self.state()['reviews'].values()))
        self.assertLessEqual(len(json.dumps(job['packet']['actions'],ensure_ascii=False).encode()),actions.MAX_EXPORT_BYTES)
    def test_file_followup_priority_is_unchanged(self):
        target=self.work/'view.py';target.write_text('before');actions.configure(self.data,self.session,str(self.work),['view.py'])
        target.write_text('after');actions.refresh(s:=self.state());self.store(s);self.batch()
        self.assertEqual(supervisor.candidate(self.state())['kind'],'detect')


if __name__=='__main__':unittest.main()
