import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

TOOLS=Path(__file__).resolve().parents[1]/'tools'
sys.path.insert(0,str(TOOLS))
import capture_conversation as capture
import common

SCHEMA=(TOOLS.parents[1]/'template/conversations/message.schema.json').read_text()


class ConversationCaptureTests(unittest.TestCase):
    def setUp(self):
        self.account=common.use_account(common.Account('usr_test','pat_test',patient_scopes=('test_patient',)))
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.original_root=capture.ROOT
        capture.ROOT=self.root
        (self.root/'conversations').mkdir()
        (self.root/'conversations/message.schema.json').write_text(SCHEMA)
        self.session=self.root/'source.jsonl'
        self.events=[{'timestamp':'2026-10-01T12:00:00Z','ordinal':0,'type':'session_meta',
                     'payload':{'id':'abcdef01-1234-1234-1234-abcdef012345','timestamp':'2026-10-01T12:00:00Z','cwd':str(self.root)}}]

    def tearDown(self):
        common.reset_account(self.account)
        capture.ROOT=self.original_root
        self.tmp.cleanup()

    def message(self,ordinal,role,text,channel=None):
        return {'timestamp':'2026-10-01T12:00:01Z','ordinal':ordinal,'type':'response_item',
                'payload':{'type':'message','id':None,'role':role,'channel':channel,
                           'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}}

    def write(self,tail=b''):
        self.session.write_bytes(b''.join((json.dumps(e,ensure_ascii=False)+'\n').encode() for e in self.events)+tail)

    def test_roles_exact_bytes_and_repeat_capture(self):
        self.events.extend([self.message(1,'user','One teaspoon, not two.'),
                            self.message(2,'assistant','A possible explanation, not a diagnosis.'),
                            self.message(3,'developer','Private instruction'),
                            self.message(4,'assistant','Private reasoning','analysis')])
        self.write()
        first=capture.capture(self.session)
        self.assertEqual(first['new_messages'],2)
        self.assertEqual(capture.capture(self.session)['new_messages'],0)
        rows=[json.loads(l) for l in (self.root/'conversations/messages.jsonl').read_text().splitlines()]
        self.assertEqual([r['role'] for r in rows],['user','assistant'])
        self.assertTrue(all(r['clinical_assertion']=='not_extracted' for r in rows))
        for r in rows:
            raw=(self.root/r['provenance']['source_event_path']).read_bytes()
            self.assertEqual(json.loads(raw)['payload']['content'][0]['text'],r['message_text'])

    def test_partial_append_repeated_text_and_conflict(self):
        self.events.append(self.message(1,'user','Same wording'))
        self.write(tail=b'{"timestamp":')
        self.assertEqual(capture.capture(self.session)['new_messages'],1)
        self.events.append(self.message(2,'user','Same wording'))
        self.write()
        self.assertEqual(capture.capture(self.session)['new_messages'],1)
        self.events[1]=self.message(1,'user','Changed original wording')
        self.write()
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            capture.capture(self.session)

    def test_workspace_identity_and_question_context(self):
        self.events.append({'timestamp':'2026-10-01T12:00:01Z','ordinal':1,'type':'response_item',
          'payload':{'type':'function_call','name':'functions.request_user_input_async',
                     'arguments':json.dumps({'questions':[{'title':'Where does it feel stuck?'}]})}})
        self.write()
        self.assertEqual(capture.capture(self.session)['new_messages'],1)
        self.events[0]['payload']['cwd']='/unrelated/workspace'
        self.write()
        with self.assertRaisesRegex(ValueError,'health workspace'):
            capture.capture(self.session)

    def test_claude_code_transcript_keeps_user_facing_text_and_clarifications(self):
        base={'sessionId':'abcdef01-1234-1234-1234-abcdef012345','cwd':str(self.root),'timestamp':'2026-10-01T12:00:01Z'}
        def event(kind,uuid,content,**extra):
            return {**base,**extra,'type':kind,'uuid':uuid,'message':{'role':kind,'content':content}}
        self.events=[{'type':'mode','mode':'normal','sessionId':base['sessionId']},
            event('user','u1','Weight 180, BP 120/80'),
            event('assistant','a1',[{'type':'thinking','thinking':'Private reasoning'}]),
            event('assistant','a2',[{'type':'text','text':'Which morning was it?'}]),
            event('assistant','a3',[{'type':'tool_use','id':'t1','name':'Bash','input':{'command':'ls'}}]),
            event('user','u2',[{'type':'tool_result','tool_use_id':'t1','content':'tool output'}]),
            event('assistant','a4',[{'type':'tool_use','id':'t2','name':'AskUserQuestion','input':{'questions':[{'question':'Same day?'}]}}]),
            event('user','u3',[{'type':'tool_result','tool_use_id':'t2','content':'"Same day?"="Yes"'}],toolUseResult={'answers':{'Same day?':'Yes'}}),
            event('user','u4','<system-reminder>injected</system-reminder>'),
            event('user','u5','sidechain',isSidechain=True)]
        self.write()
        self.assertEqual(capture.capture(self.session)['new_messages'],4)
        self.assertEqual(capture.capture(self.session)['new_messages'],0)
        rows=[json.loads(l) for l in (self.root/'conversations/messages.jsonl').read_text().splitlines()]
        self.assertEqual([(r['role'],r['message_mode'],r['source_message_id']) for r in rows],
            [('user','message','u1'),('assistant','message','a2'),('assistant','clarification_prompt','a4'),('user','clarification_answer','u3')])
        self.assertEqual(rows[3]['content_blocks'][0]['answers'],{'Same day?':'Yes'})

    def test_connector_messages_are_preserved_once(self):
        messages=[{'role':'user','text':'Weight 180 today','timestamp':'2026-10-24T12:00:00Z','message_ref':'m1'},
                  {'role':'assistant','text':'Recorded for review.','timestamp':'2026-10-24T12:00:05Z'}]
        path,added=capture.append_connector_messages(self.root,'abcdef01-0000-0000-0000-000000000001','claude.ai',messages,'2026-10-24T12:01:00Z')
        self.assertEqual(added,2)
        first=capture.capture(path,archive_root=self.root)
        self.assertEqual(first['new_messages'],2)
        _,again=capture.append_connector_messages(self.root,'abcdef01-0000-0000-0000-000000000001','claude.ai',messages,'2026-10-24T12:02:00Z')
        self.assertEqual(again,0)
        self.assertEqual(capture.capture(path,archive_root=self.root)['new_messages'],0)
        rows=[json.loads(l) for l in (self.root/'conversations/messages.jsonl').read_text().splitlines()]
        self.assertEqual([(r['role'],r['source_message_id']) for r in rows],[('user','m1'),('assistant',None)])
        session=json.loads((self.root/'conversations/sessions/abcdef01-0000-0000-0000-000000000001/session.json').read_text())
        self.assertEqual(session['source_workspace'],'connector:claude.ai')
        with self.assertRaisesRegex(ValueError,'hexadecimal'):
            capture.append_connector_messages(self.root,'../escape','x',messages,'2026-10-24T12:00:00Z')

    def test_connector_capture_repeats_in_a_fresh_workspace(self):
        sid='abcdef01-0000-0000-0000-000000000002'
        first=[{'role':'user','text':'Weight 243 yesterday','timestamp':'2026-10-25T15:25:58Z'}]
        path,_=capture.append_connector_messages(self.root,sid,'codex',first,'2026-10-25T15:26:00Z')
        capture.capture(path,archive_root=self.root)
        stored=(self.root/'conversations/messages.jsonl').read_text()
        self.assertEqual(json.loads(stored)['provenance']['original_session_path'],f'conversations/connector-sessions/{sid}.jsonl')
        # Every write materializes the archive in a new directory, and earlier captures recorded its absolute path.
        legacy=json.loads(stored);legacy['provenance']['original_session_path']='/tmp/health-workspace-old/'+legacy['provenance']['original_session_path']
        legacy['content_sha256']=capture.digest({k:v for k,v in legacy.items() if k!='content_sha256'})
        moved=Path(self.tmp.name)/'fresh';moved.mkdir()
        import shutil;shutil.copytree(self.root/'conversations',moved/'conversations')
        (moved/'conversations/messages.jsonl').write_text(capture.canonical(legacy)+'\n')
        path,_=capture.append_connector_messages(moved,sid,'codex',first+[{'role':'user','text':'Restarted Thursday','timestamp':'2026-10-25T15:30:00Z'}],'2026-10-25T15:31:00Z')
        self.assertEqual(capture.capture(path,archive_root=moved)['new_messages'],1)
        rows=[json.loads(l) for l in (moved/'conversations/messages.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0],legacy)
        self.assertEqual(rows[1]['provenance']['original_session_path'],f'conversations/connector-sessions/{sid}.jsonl')

    def test_deferred_connector_sources_are_captured_by_the_next_write(self):
        sid='abcdef01-0000-0000-0000-000000000003'
        capture.append_connector_messages(self.root,sid,'codex',[{'role':'assistant','text':'Any swelling today?'},
                                                                  {'role':'user','text':'No'}],'2026-10-26T14:00:00Z')
        self.assertFalse((self.root/'conversations/messages.jsonl').exists())
        self.assertEqual([r['new_messages'] for r in capture.capture_pending(self.root)],[2])
        self.assertEqual(capture.capture_pending(self.root),[])
        capture.append_connector_messages(self.root,sid,'codex',[{'role':'user','text':'Slept 8 hours'}],'2026-10-26T14:01:00Z')
        self.assertEqual([r['new_messages'] for r in capture.capture_pending(self.root)],[1])
        rows=[json.loads(l) for l in (self.root/'conversations/messages.jsonl').read_text().splitlines()]
        self.assertEqual([r['message_text'] for r in rows],['Any swelling today?','No','Slept 8 hours'])

    def test_privacy_gap_persists_without_changing_source_identity(self):
        for ordinal,second,text in [(1,1,'Before gap'),(2,2,'Do not retain this'),(3,3,'Resume recording')]:
            event=self.message(ordinal,'user',text)
            event['timestamp']=f'2026-10-01T12:00:0{second}Z'
            self.events.append(event)
        self.write()
        first=capture.capture(self.session,[('2026-10-01T12:00:02Z','2026-10-01T12:00:03Z')])
        self.assertEqual(first['new_messages'],2)
        rows=[json.loads(l) for l in (self.root/'conversations/messages.jsonl').read_text().splitlines()]
        self.assertEqual([r['source_ordinal'] for r in rows],[1,3])
        self.assertFalse(any('Do not retain this' in r['message_text'] for r in rows))
        self.assertEqual(capture.capture(self.session)['new_messages'],0)
        self.assertIn('no-record',first['source_scope'])
        self.assertFalse((self.root/'conversations/sessions/abcdef01-1234-1234-1234-abcdef012345/messages/2.json').exists())

    def test_excluding_an_archived_message_requires_review(self):
        self.events.append(self.message(1,'user','Already retained'))
        self.write()
        capture.capture(self.session)
        with self.assertRaisesRegex(ValueError,'already archived'):
            capture.capture(self.session,[('2026-10-01T12:00:01Z','2026-10-01T12:00:02Z')])


if __name__=='__main__':
    unittest.main()
