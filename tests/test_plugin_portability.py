"""Relocate packaged files; exercise host hook contract without developer tooling or models."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class PluginPortabilityTests(unittest.TestCase):
    def test_package_runs_from_unrelated_directory_and_data_root(self):
        with tempfile.TemporaryDirectory(prefix='navi-relocated-') as name:
            root=Path(name)
            plugin=root/'some package'
            shutil.copytree(ROOT/'plugins/navi',plugin,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            data=root/'user data'
            work=root/'work'
            work.mkdir()
            file=work/'widget.py'
            file.write_text('value = 1\n')
            env=dict(os.environ,PLUGIN_ROOT=str(plugin),PLUGIN_DATA=str(data))
            session='portable-task'
            config=json.loads((plugin/'hooks/hooks.json').read_text())
            def hook(event,**extra):
                command=config['hooks'][event][0]['hooks'][0]['command']
                # Substitute host-owned paths; no source-tree imports or local marketplace helper.
                payload=dict(session_id=session,turn_id='t1',cwd=str(work),hook_event_name=event,**extra)
                proc=subprocess.run(['/bin/sh','-c',command],input=json.dumps(payload),capture_output=True,
                    text=True,cwd=work,env=env,timeout=5)
                self.assertEqual(proc.returncode,0,proc.stderr)
                self.assertEqual(proc.stderr,'')
                self.assertEqual(proc.stdout,'{}\n' if event=='Stop' else '')
            def command(script,op,*args):
                proc=subprocess.run([sys.executable,str(plugin/'scripts'/script),op,'--data-directory',str(data),
                    '--session',session,*args],cwd=work,env=env,capture_output=True,text=True,timeout=5)
                self.assertEqual(proc.returncode,0,proc.stderr+proc.stdout)
                return json.loads(proc.stdout)
            hook('UserPromptSubmit',prompt='Navi: start\nChange value to 2 only.')
            command('action_evidence.py','enable','--workspace',str(work),'--watch','widget.py')
            hook('PreToolUse',tool_use_id='p1',tool_name='apply_patch',tool_input='replace value = 1 with value = 2')
            file.write_text('value = 2\n')
            hook('PostToolUse',tool_use_id='p1',tool_name='apply_patch',tool_input='replace value = 1 with value = 2',tool_response='done')
            hook('Stop')
            state=command('task_state.py','show')
            self.assertFalse(state['capture_gap'])
            self.assertEqual(state['task']['budget']['reviews_used'],0)
            packet=command('action_evidence.py','export')
            observed=next(a for a in packet['actions'] if a['kind']=='workspace_observation')
            self.assertIn('+value = 2',json.loads(observed['evidence'])['cumulative_diff'])
            self.assertEqual(command('review.py','status')['reviews'],{})
            output=root/'report.json'
            generated=subprocess.run([sys.executable,str(plugin/'scripts/report.py'),'--data-directory',str(data),
                '--session',session,'--format','json','--output',str(output)],cwd=work,env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(generated.returncode,0,generated.stdout+generated.stderr)
            report=json.loads(output.read_text())
            self.assertIn('+value = 2',report['files'][0]['observed_diff'])
            self.assertEqual(report['files'][0]['attribution'],'unknown_may_include_concurrent_edits')
            self.assertEqual(report['report_generation_model_calls'],0)
            command('task_state.py','forget')
            self.assertIsNone(command('task_state.py','show')['task'])


if __name__=='__main__':
    unittest.main()
