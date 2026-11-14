"""Tool-free provider contract with fictional app-server streams; no API calls."""

from collections import deque
import json
import os
import re
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from app import research_provider as provider

REAL_RPC = provider._RPC


class FakeRPC:
    instances = []
    bad_tool = None
    bad_config = None
    cancel_at_turn = False
    recovered_turn = None
    lose_turn_response = False
    wrong_profile = False

    def __init__(self, cmd, project, env, cancel, timeout):
        self.cmd, self.project, self.env, self.cancel = cmd, project, env, cancel
        self.pending, self.requests = deque(), []
        self.closed, self.interrupted = False, False
        self.thread = {'id':str(uuid.uuid4()), 'cwd':project, 'projectId':CONFIG['project_id'],
            'ephemeral':False, 'modelProvider':'p0deepseek' if any('model_provider="p0deepseek"' == entry for entry in cmd) else 'openai',
            'source':'vscode', 'sessionId':'test-durable-session'}
        self.instances.append(self)

    def write(self, value):
        self.requests.append(value)

    def request(self, method, params):
        self.requests.append({'method':method, 'params':params})
        if method == 'config/read':
            config = {'features':{name:False for name in provider.DISABLED_FEATURES} | {'skip_host_skill_discovery':True},
                      'mcp_servers':{'fixture':{'enabled':False}}, 'web_search':'disabled','permissions':{},
                      'model_reasoning_effort':'high'}
            for entry in self.cmd:
                match=re.fullmatch(r'permissions\.([a-z0-9_]+)\.filesystem=\{(.*)\}',entry)
                if match:
                    pairs=re.findall(r'("(?:[^"\\]|\\.)*")=("(?:[^"\\]|\\.)*")',match[2])
                    config['permissions'][match[1]]={'filesystem':{json.loads(k):json.loads(v) for k,v in pairs},
                                                    'network':{'enabled':False}}
            if self.bad_config:
                self.bad_config(config)
            return {'config':config}
        if method == 'thread/resume':
            self.thread['id'] = params['threadId']
        if method == 'thread/read':
            self.thread['id'] = params['threadId']
            if self.recovered_turn is not None:
                self.thread['turns'] = [self.recovered_turn]
        if method in {'thread/start','thread/resume','thread/read'}:
            return {'thread':self.thread, 'model':'fixture',
                    'activePermissionProfile':{'id':'unexpected' if self.wrong_profile else params.get('permissions'),'extends':None},'reasoningEffort':'high'}
        if method == 'turn/start':
            if self.cancel_at_turn:
                self.cancel.set()
            items = [dict(type='userMessage', id='user', content=params['input']), dict(type='reasoning', id='reason'),
                     dict(type='agentMessage', id='commentary', phase='commentary', text='Do not persist this progress transcript'),
                     dict(type='agentMessage', id='final', text='{"status":"needs_input","questions":["虚构问题"]}')]
            if self.bad_tool:
                items.insert(1, {'type':self.bad_tool,'id':'unsafe'})
            self.pending.extend({'method':'item/completed','params':{'threadId':self.thread['id'],'turnId':'T1','item':item}} for item in items)
            self.pending.append({'method':'thread/tokenUsage/updated', 'params':{'threadId':self.thread['id'],
                'tokenUsage':{'total':{'inputTokens':12,'outputTokens':3,'totalTokens':15,'unknown':'must-not-leak'}}}})
            self.pending.append({'method':'turn/completed','params':{'threadId':self.thread['id'],
                'turn':{'id':'T1','status':'completed','items':[]}}})
            if self.lose_turn_response:
                FakeRPC.recovered_turn={'id':'T1','status':'completed','items':items}
                raise RuntimeError('simulated turn/start response loss')
            return {'turn':{'id':'T1'}}
        return {}

    def event(self):
        if self.cancel.is_set():
            raise provider._Cancelled()
        return self.pending.popleft()

    def interrupt(self, session):
        self.interrupted = True

    def close(self):
        self.closed = True


CONFIG = {'project_id':'11111111-2222-3333-4444-555555555555','disabled_mcp_servers':['fixture']}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.executable = self.base/'fake-codex.exe'; self.executable.write_bytes(b'fictional, never executed')
        self.project = self.base/'project';self.project.mkdir()
        self.workspace = self.base/'attempt';self.workspace.mkdir()
        self.config = {**CONFIG,'project_dir':str(self.project),'codex_executable':str(self.executable)}
        FakeRPC.instances=[];FakeRPC.bad_tool=None;FakeRPC.bad_config=None;FakeRPC.cancel_at_turn=False;FakeRPC.recovered_turn=None;FakeRPC.lose_turn_response=False;FakeRPC.wrong_profile=False
        self.addCleanup(patch.stopall)
        patch('app.research_provider._RPC',FakeRPC).start()

    def run_fixture(self, **kwargs):
        return provider.run_turn(self.config, 'codex', 'fictional data', self.workspace, **kwargs)

    def test_completed_turn_and_needs_input_are_returned_without_transcript(self):
        events=[]
        result=self.run_fixture(emit=events.append,output_schema={'type':'object'})
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['json']['status'],'needs_input')
        self.assertEqual(result['tool_policy'],'restricted_profile_verified')
        self.assertTrue(FakeRPC.instances[0].closed)
        self.assertTrue(all(entry['kind'] in {'stage','usage','session'} for entry in events))
        self.assertFalse(any('虚构问题' in json.dumps(entry,ensure_ascii=False) for entry in events))
        requests=FakeRPC.instances[0].requests
        turn=next(row for row in requests if row['method']=='turn/start')
        self.assertNotIn('sandboxPolicy',turn['params'])
        self.assertEqual(turn['params']['permissions'],provider._permission(self.config,self.workspace)[0])
        self.assertEqual(turn['params']['effort'],'high')
        self.assertEqual(turn['params']['runtimeWorkspaceRoots'],[str(self.workspace)])
        self.assertEqual([row for row in events if row['kind']=='usage'][0]['usage'],{'inputTokens':12,'outputTokens':3,'totalTokens':15})
        self.assertEqual(len([row for row in events if row['kind']=='session']),3)

    def test_native_tools_are_fail_closed_and_interrupted(self):
        for name in ('commandExecution','mcpToolCall','webSearch','imageView','fileChange','dynamicToolCall',
                     'subAgentActivity','collabAgentToolCall','newUnknownTool'):
            with self.subTest(item=name):
                FakeRPC.bad_tool=name
                result=self.run_fixture()
                self.assertEqual(result['status'],'failed')
                self.assertTrue(FakeRPC.instances[-1].interrupted)
                self.assertIsNone(result['json'])

    def test_any_unverified_feature_or_mcp_blocks_before_thread_creation(self):
        changes=[lambda x:x['features'].update(shell_tool=True),lambda x:x['features'].pop('view_image'),
                 lambda x:x['mcp_servers'].update(new_server={}),lambda x:x.update(web_search='live'),
                 lambda x:x.update(model_reasoning_effort='ultra')]
        for change in changes:
            FakeRPC.bad_config=staticmethod(change)
            result=self.run_fixture()
            self.assertEqual(result['status'],'failed')
            self.assertFalse(any(row['method'].startswith('thread/') for row in FakeRPC.instances[-1].requests))

    def test_native_thread_must_confirm_the_selected_permission_profile_before_paid_turn(self):
        FakeRPC.wrong_profile=True
        result=self.run_fixture()
        self.assertEqual(result['status'],'failed')
        self.assertFalse(any(row['method']=='turn/start' for row in FakeRPC.instances[-1].requests))

    def test_resume_keeps_identity_provider_and_rechecks_current_workspace(self):
        first=self.run_fixture()['session']
        another=self.base/'next-attempt';another.mkdir()
        second=provider.run_turn(self.config,'codex','answer to fictional question',another,session=first)
        self.assertEqual(second['status'],'completed')
        self.assertEqual(second['session']['thread_id'],first['thread_id'])
        resumed=next(row for row in FakeRPC.instances[-1].requests if row['method']=='thread/resume')['params']
        self.assertEqual(resumed['runtimeWorkspaceRoots'],[str(another)])
        self.assertNotIn('projectId',resumed)
        self.assertTrue(resumed['excludeTurns'])
        self.assertEqual(resumed['permissions'],provider._permission(self.config,another)[0])
        self.assertNotIn('sandbox',resumed)
        bad={**first,'project_id':str(uuid.uuid4())}
        self.assertEqual(provider.run_turn(self.config,'codex','invalid',another,session=bad)['status'],'failed')

    def test_deepseek_credential_only_in_environment_and_never_events_or_command(self):
        secret='sk-fictionalProviderSecret_1234567890'
        with patch.dict(os.environ,{provider.DEEPSEEK_ENV:secret,'OPENAI_API_KEY':'fictional-other-key'}):
            events=[]
            result=provider.run_turn(self.config,'deepseek','fictional',self.workspace,emit=events.append)
        self.assertEqual(result['status'],'completed')
        rpc=FakeRPC.instances[-1]
        self.assertEqual(rpc.env[provider.DEEPSEEK_ENV],secret)
        self.assertNotIn('OPENAI_API_KEY',rpc.env)
        self.assertNotIn(secret,json.dumps([rpc.cmd,rpc.requests,events,result]))
        self.assertIn('https://api.deepseek.com',' '.join(rpc.cmd))
        turn=next(row for row in rpc.requests if row['method']=='turn/start')
        self.assertNotIn('effort',turn['params'])
        self.assertFalse(any('model_reasoning_effort=' in entry for entry in rpc.cmd))

    def test_permission_profile_is_per_attempt_read_only_with_explicit_key_deny(self):
        keyfile=self.workspace/'private.txt';keyfile.write_text('fictional')
        config={**self.config,'deepseek_key_file':str(keyfile)}
        name,filesystem=provider._permission(config,self.workspace)
        self.assertEqual(filesystem,{':minimal':'read',self.workspace.as_posix():'read',keyfile.as_posix():'deny'})
        args=provider.command(config,'codex',self.workspace)
        self.assertIn('permissions.'+name+'.network.enabled=false',args)
        self.assertIn('shell_environment_policy.exclude=["*KEY*","*TOKEN*","*SECRET*","*PASSWORD*"]',args)
        other=self.base/'other';other.mkdir()
        self.assertNotEqual(name,provider._permission(config,other)[0])
        FakeRPC.bad_config=staticmethod(lambda x:x['permissions'][name]['filesystem'].update({str(self.base):'read'}))
        result=provider.run_turn(config,'codex','fictional',self.workspace)
        self.assertEqual(result['status'],'failed')
        self.assertFalse(any(row['method'].startswith('thread/') for row in FakeRPC.instances[-1].requests))

    def test_credential_input_is_unique_private_and_redacted(self):
        secret='sk-fictionalKey_123456789'
        self.assertEqual(provider.parse_deepseek_key('DeepSeek API key："'+secret+'"\nOpenAI API key=unused'),secret)
        for value in ('','DeepSeek=x','DeepSeek='+secret+'\nDeepSeek='+secret):
            with self.assertRaises(ValueError):provider.parse_deepseek_key(value)
        self.assertEqual(provider.redact('token '+secret),'token [REDACTED]')
        keyfile=self.base/'credential.txt';keyfile.write_text('DeepSeek='+secret,encoding='utf-16')
        self.assertEqual(provider._secret({**self.config,'deepseek_key_file':str(keyfile)}),secret)

    def test_cancel_before_start_never_opens_model_process(self):
        cancel=threading.Event();cancel.set()
        self.assertEqual(self.run_fixture(cancel=cancel)['status'],'cancelled')
        self.assertEqual(len(FakeRPC.instances),0)

    def test_cancel_with_buffered_completion_does_not_adopt_result(self):
        FakeRPC.cancel_at_turn=True
        result=self.run_fixture()
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['session']['turn_id'],'T1')
        self.assertIsNone(result['json'])
        self.assertTrue(FakeRPC.instances[-1].interrupted)

    def test_availability_is_not_account_verification_or_a_request(self):
        with patch.dict(os.environ,{provider.DEEPSEEK_ENV:'sk-fictionalKey_123456789'}):
            value=provider.availability(self.config)
        self.assertTrue(value['codex']['configured'])
        self.assertTrue(value['deepseek']['configured'])
        self.assertFalse(value['deepseek']['verified'])
        self.assertEqual(len(FakeRPC.instances),0)

    def test_path_traversal_and_wrong_source_rejected(self):
        with self.assertRaises(ValueError):provider.command({**self.config,'project_dir':str(self.project/'..'/'project')},'codex')
        self.assertEqual(provider.run_turn(self.config,'other','fixture',self.workspace)['status'],'failed')

    def test_rpc_server_requests_are_never_approved(self):
        rpc=object.__new__(REAL_RPC)
        rpc.cancel=threading.Event();rpc.deadline=provider.time.monotonic()+5
        rpc.events=provider.queue.Queue();rpc.events.put({'id':7,'method':'item/commandExecution/requestApproval'})
        replies=[];rpc.write=replies.append
        with self.assertRaises(ValueError):rpc.event()
        self.assertEqual(replies[0]['error']['code'],-32601)

    def test_completed_turn_can_be_recovered_without_new_model_or_resume(self):
        session=self.run_fixture()['session']
        FakeRPC.recovered_turn={'id':session['turn_id'],'status':'completed','items':[
            {'id':'user','type':'userMessage','content':[{'type':'text','text':provider.INTENT_PREFIX+session['pending_turn_intent']}]},
            {'id':'final','type':'agentMessage','phase':'final_answer','text':'{"status":"completed"}'}]}
        result=provider.recover_turn(self.config,'codex',self.workspace,session)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['json'],{'status':'completed'})
        self.assertFalse(result['model_invoked'])
        self.assertFalse(any(row['method'] in {'thread/start','thread/resume','turn/start'} for row in FakeRPC.instances[-1].requests))

    def test_confirmed_completed_non_json_turn_is_recovered_for_validation(self):
        session=self.run_fixture()['session']
        FakeRPC.recovered_turn={'id':session['turn_id'],'status':'completed','items':[
            {'type':'userMessage','content':[{'type':'text','text':provider.INTENT_PREFIX+session['pending_turn_intent']}]},
            {'type':'agentMessage','phase':'final_answer','text':'fictional non-JSON result'}]}
        result=provider.recover_turn(self.config,'codex',self.workspace,session)
        self.assertEqual(result['status'],'completed')
        self.assertIsNone(result['json'])
        self.assertEqual(result['final_text'],'fictional non-JSON result')
        self.assertFalse(result['model_invoked'])

    def test_running_unknown_or_missing_turn_cannot_start_a_paid_retry(self):
        session=self.run_fixture()['session']
        for status,expected in [('inProgress','running'),('futureState','unknown'),('failed','retryable'),('interrupted','retryable')]:
            with self.subTest(status=status):
                FakeRPC.recovered_turn={'id':session['turn_id'],'status':status,'items':[{'type':'userMessage',
                    'content':[{'type':'text','text':provider.INTENT_PREFIX+session['pending_turn_intent']}]}]}
                result=provider.recover_turn(self.config,'codex',self.workspace,session)
                self.assertEqual(result['status'],expected)
                self.assertEqual(result['retryable'],expected=='retryable')
                self.assertFalse(any(row['method']=='turn/start' for row in FakeRPC.instances[-1].requests))
        FakeRPC.recovered_turn={'id':'OTHER','status':'completed','items':[]}
        self.assertEqual(provider.recover_turn(self.config,'codex',self.workspace,session)['status'],'unknown')

    def test_recovery_rejects_wrong_identity_or_tool_contaminated_result(self):
        session=self.run_fixture()['session']
        FakeRPC.recovered_turn={'id':session['turn_id'],'status':'completed','items':[{'type':'commandExecution'}]}
        self.assertEqual(provider.recover_turn(self.config,'codex',self.workspace,session)['status'],'unknown')
        self.assertEqual(provider.recover_turn(self.config,'deepseek',self.workspace,session)['status'],'unknown')

    def test_lost_turn_start_response_is_recovered_by_persisted_nonce(self):
        FakeRPC.lose_turn_response=True
        events=[];result=self.run_fixture(emit=events.append)
        self.assertEqual(result['status'],'failed')
        self.assertIsNone(result['session']['turn_id'])
        self.assertEqual(result['session']['turn_state'],'dispatching')
        self.assertEqual([row for row in events if row['kind']=='session'][-1]['session']['pending_turn_intent'],result['session']['pending_turn_intent'])
        recovered=provider.recover_turn(self.config,'codex',self.workspace,result['session'])
        self.assertEqual(recovered['status'],'completed')
        self.assertEqual(recovered['session']['turn_id'],'T1')
        self.assertFalse(any(row['method']=='turn/start' for row in FakeRPC.instances[-1].requests))

    def test_previous_completed_turn_is_not_adopted_for_a_new_dispatch_intent(self):
        old=self.run_fixture()['session']
        old_turn={'id':old['turn_id'],'status':'completed','items':[{'type':'userMessage',
            'content':[{'type':'text','text':provider.INTENT_PREFIX+old['pending_turn_intent']}]}]}
        FakeRPC.lose_turn_response=True
        failed=self.run_fixture(session=old)['session']
        FakeRPC.recovered_turn=old_turn
        self.assertNotEqual(old['pending_turn_intent'],failed['pending_turn_intent'])
        self.assertEqual(provider.recover_turn(self.config,'codex',self.workspace,failed)['status'],'unknown')
