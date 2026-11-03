import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/navi/scripts'))
import history
import transcript_chain as chain
from task_state import source_id


class TranscriptChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'sessions';self.root.mkdir()
        self.session='00000000-0000-0000-0000-000000000001'
        self.second='00000000-0000-0000-0000-000000000002'

    def message(self,body,ordinal):
        return {'ordinal':ordinal,'type':'event_msg','payload':{'type':'item_completed',
            'thread_id':self.session,'turn_id':'turn-'+str(ordinal),
            'item':{'id':'item-'+str(ordinal),'type':'UserMessage','content':[{'type':'text','text':body}]}}}

    def segment(self,identity,start,rows,base=None):
        path=self.root/('rollout-'+identity+'.jsonl')
        meta={'id':self.session,'history_mode':'paginated'}
        if base:meta['history_base']=base
        records=[{'ordinal':start,'type':'session_meta','payload':meta}]+rows
        path.write_text(''.join(json.dumps(r)+'\n' for r in records))
        return path

    def read(self,path,previous=None):
        s=path.stat()
        return history.read_snapshot(str(path),self.session,
            {'device':s.st_dev,'inode':s.st_ino,'size':s.st_size},previous,0)

    def pair(self):
        root=self.segment(self.session,0,[self.message('UI only; backend deferred.',1)])
        base={'thread_id':self.session,'end_ordinal_exclusive':2,'end_byte_offset':root.stat().st_size}
        leaf=self.segment(self.second,2,[self.message('Add keyboard navigation.',3)],base)
        return root,leaf,base

    def test_chain_uses_exact_ancestor_boundary_and_incremental_leaf(self):
        root,leaf,base=self.pair()
        with root.open('a') as f:f.write(json.dumps(self.message('NOT_IN_THIS_BRANCH',2))+'\n')
        first=self.read(leaf)
        self.assertEqual(first['status'],'ready');self.assertEqual(first['chain']['segments'],2)
        self.assertEqual([m['text'] for m in first['messages']],['UI only; backend deferred.','Add keyboard navigation.'])
        self.assertEqual(first['chain']['excluded_live_inputs'],[source_id('turn-2','NOT_IN_THIS_BRANCH')])
        with leaf.open('a') as f:f.write(json.dumps(self.message('Backend remains deferred.',4))+'\n')
        with patch.object(chain,'resolve',side_effect=AssertionError('must reuse leaf cursor')):
            second=self.read(leaf,first)
        self.assertEqual(second['user_message_count'],3)
        self.assertEqual(second['parsed_new_bytes'],leaf.stat().st_size-first['read_bytes'])
        self.assertEqual(self.read(leaf,second)['parsed_new_bytes'],0)

    def test_missing_ancestor_and_wrong_session_fail(self):
        root,leaf,base=self.pair();raw=root.read_text();root.unlink()
        with self.assertRaisesRegex(ValueError,'missing_or_ambiguous'):self.read(leaf)
        root.write_text(raw.replace(self.session,'00000000-0000-0000-0000-000000000099'))
        with self.assertRaisesRegex(ValueError,'session_mismatch'):self.read(leaf)

    def test_cycle_wrong_ordinal_and_incomplete_byte_boundary_fail(self):
        root,leaf,base=self.pair();raw=leaf.read_text()
        for changed in [dict(base,thread_id=self.second),dict(base,end_ordinal_exclusive=9),
                        dict(base,end_byte_offset=base['end_byte_offset']-1)]:
            lines=[json.loads(l) for l in raw.splitlines()];lines[0]['payload']['history_base']=changed
            leaf.write_text(''.join(json.dumps(r)+'\n' for r in lines))
            with self.subTest(changed=changed),self.assertRaises(ValueError):self.read(leaf)

    def test_rotation_rebind_preserves_original_users_and_rejects_rewrites(self):
        root,leaf,base=self.pair();previous=self.read(root)
        self.assertEqual(self.read(leaf,previous)['user_message_count'],2)
        old=copy.deepcopy(previous);old['messages'][0]['text']='DIFFERENT REQUIREMENT'
        with self.assertRaisesRegex(ValueError,'user_prefix_changed'):self.read(leaf,old)

    def test_rewrite_of_existing_leaf_is_rejected(self):
        root,leaf,base=self.pair();previous=self.read(leaf)
        leaf.write_text(leaf.read_text().replace('Add keyboard navigation.','Add database navigation.'))
        with self.assertRaises(ValueError):self.read(leaf,previous)

    def test_incremental_ordinal_gap_is_rejected(self):
        root,leaf,base=self.pair();previous=self.read(leaf)
        with leaf.open('a') as stream:stream.write(json.dumps(self.message('Missing earlier input.',5))+'\n')
        with self.assertRaisesRegex(ValueError,'ordinal_gap'):self.read(leaf,previous)

    @unittest.skipUnless(shutil.which('zstd'),'zstd is optional for archived host history')
    def test_compressed_ancestor_and_missing_decoder(self):
        root,leaf,base=self.pair()
        subprocess.run(['zstd','-q',str(root)],check=True);root.unlink()
        self.assertEqual(self.read(leaf)['user_message_count'],2)
        with patch.object(chain.shutil,'which',return_value=None):
            with self.assertRaisesRegex(ValueError,'requires_zstd'):self.read(leaf)


if __name__=='__main__':unittest.main()
