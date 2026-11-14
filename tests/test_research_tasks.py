"""Durable lifecycle tests use fictional inputs and a counted provider, never a model service."""
import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.research_tasks import ResearchTasks, digest
from app.research_materials import ResearchSelectionRequired
from app.store import ResearchStore
from test_platform_server import IDENTITY, TOOL, lab


class ResearchTaskTests(unittest.TestCase):
    def test_over_fifty_unselected_old_items_rejects_before_task_or_model(self):
        context=self.store.context(IDENTITY['security_id'])
        pending=[{'item_id':'OLD-'+str(index),'module':'fundamental'} for index in range(51)]
        context['pending_items']=pending
        with patch.object(self.store,'context',return_value=context):
            with self.assertRaisesRegex(ResearchSelectionRequired,'基本面有51个.*分批研究'):
                self.manager.start(self.body())
        self.assertEqual(self.manager.list()['tasks'],[])
        self.assertEqual(self.calls,[]);self.assertEqual(self.commits,0)
        self.assertEqual(len(pending),51)

    def test_explicit_old_item_batch_does_not_silently_drop_unselected_history(self):
        context=self.store.context(IDENTITY['security_id'])
        states=[{'item_id':'OLD-'+str(index),'module':'fundamental'} for index in range(51)]
        context['pending_items']=states
        context['results']=[{'module':'fundamental','item_states':states}]
        with patch.object(self.store,'context',return_value=context):
            selected=self.manager.start(self.body(item_ids=['OLD-0']))['task']
        self.assertEqual(selected['item_ids'],['OLD-0']);self.assertEqual(len(states),51)
        self.assertEqual(selected['status'],'queued');self.assertEqual(self.calls,[])

    def test_recovered_oversized_input_reports_safe_selection_help_without_model(self):
        identity=self.manager.start(self.body())['task']['task_id']
        message='基本面有51个待复核旧项，请从单项入口分批研究，尚未启动模型。'
        with patch.object(self.materials,'prepare',side_effect=ResearchSelectionRequired(message)):
            self.manager.run_one(identity)
        task=self.manager.get(identity)['task']
        self.assertEqual(task['status'],'failed');self.assertEqual(task['failure_reason'],message)
        self.assertEqual(self.calls,[]);self.assertEqual(self.commits,0)

    def test_completed_non_json_is_consumed_and_only_explicit_resume_calls_for_correction(self):
        malformed={'status':'completed','session':{'thread_id':'same','turn_id':'T1','pending_turn_intent':'a'*32},
            'json':None,'final_text':'Not structured JSON'}
        corrected={'status':'completed','session':{'thread_id':'same','turn_id':'T2','pending_turn_intent':'b'*32},
            'json':{'status':'completed','questions':[],'reports':[{'module':'fundamental'}]}}
        self.manager.runner=None
        with patch('app.research_provider.run_turn',side_effect=[malformed,corrected]) as provider, \
             patch('app.research_provider.recover_turn',side_effect=AssertionError('Consumed invalid answer cannot be replayed')):
            identity=self.manager.start(self.body())['task']['task_id'];self.manager.run_one(identity)
            self.assertEqual(self.manager.get(identity)['task']['status'],'failed');self.assertEqual(provider.call_count,1)
            self.manager.resume(self.operation(identity));self.manager.run_one(identity)
        self.assertEqual(provider.call_count,2);self.assertEqual(self.commits,1)

    def test_cancel_at_waiting_transition_wins_without_losing_original_question(self):
        def asking(*args,**kwargs):
            self.calls.append('question')
            return {'status':'completed','session':{'thread_id':'same','turn_id':'T1'},'json':{
                'status':'needs_input','reports':[],'questions':[{'question':'Fictional question','options':[]}]}}
        self.manager.runner=asking;original=self.manager._update
        def cancel_before_waiting(task_id,change):
            if change.__name__=='waiting':self.manager.cancel(self.operation(task_id))
            return original(task_id,change)
        identity=self.manager.start(self.body())['task']['task_id']
        with patch.object(self.manager,'_update',side_effect=cancel_before_waiting):self.manager.run_one(identity)
        task=self.manager.get(identity)['task'];self.assertEqual(task['status'],'cancelled');self.assertEqual(len(task['questions']),1)
        self.manager.resume(self.operation(identity));task=self.manager.get(identity)['task']
        self.assertEqual(task['status'],'waiting_input');self.assertEqual(len(task['questions']),1);self.assertEqual(len(self.calls),1)

    def test_cancel_and_restore_waiting_question_keeps_original_question_once(self):
        def asking(*args,**kwargs):
            self.calls.append('question')
            return {'status':'completed','session':{'thread_id':'same','turn_id':'T1'},'json':{
                'status':'needs_input','reports':[],'questions':[{'question':'Fictional missing preference','options':[]}]}}
        self.manager.runner=asking
        identity=self.manager.start(self.body())['task']['task_id'];self.manager.run_one(identity)
        original=self.manager.get(identity)['task']['questions']
        for _ in range(2):
            self.manager.cancel(self.operation(identity));self.manager.resume(self.operation(identity))
            self.manager.run_one(identity)
            task=self.manager.get(identity)['task']
            self.assertEqual(task['status'],'waiting_input');self.assertEqual(task['questions'],original)
        self.assertEqual(len(self.calls),1)

    def test_consumed_question_turn_is_not_recovered_after_answer(self):
        question={'status':'completed','session':{'thread_id':'same','turn_id':'T1','pending_turn_intent':'a'*32},
            'json':{'status':'needs_input','reports':[],'questions':[{'question':'Choose research scope','options':[]}]}}
        answer={'status':'completed','session':{'thread_id':'same','turn_id':'T2','pending_turn_intent':'b'*32},
            'json':{'status':'completed','questions':[],'reports':[{'module':'fundamental'}]}}
        self.manager.runner=None
        with patch('app.research_provider.run_turn',side_effect=[question,answer]) as provider, \
             patch('app.research_provider.recover_turn',side_effect=AssertionError('Already-consumed turn must not be replayed')):
            identity=self.manager.start(self.body())['task']['task_id'];self.manager.run_one(identity)
            task=self.manager.get(identity)['task']
            self.manager.answer({**self.operation(identity),'question_id':task['questions'][0]['question_id'],'answers':'Research only'})
            self.manager.run_one(identity)
        self.assertEqual(provider.call_count,2);self.assertEqual(self.commits,1)

    def test_completed_old_turn_recovers_orphan_output_without_another_model_call(self):
        completed={'status':'completed','session':{'thread_id':'same','turn_id':'T1','pending_turn_intent':'a'*32},
            'json':{'status':'completed','questions':[],'reports':[{'module':'fundamental'}]}}
        self.manager.runner=None;real_save=self.manager._save;failed=False
        def fail_pointer_once(record):
            nonlocal failed
            if not failed and record.get('output_file') and record['status']=='running':
                failed=True;raise OSError('Simulated crash before completion pointer was saved')
            return real_save(record)
        with patch('app.research_provider.run_turn',return_value=completed) as provider, \
             patch('app.research_provider.recover_turn',return_value={**completed,'model_invoked':False}) as recover, \
             patch.object(self.manager,'_save',side_effect=fail_pointer_once):
            identity=self.manager.start(self.body())['task']['task_id'];self.manager.run_one(identity)
            self.assertEqual(self.manager.get(identity)['task']['status'],'failed')
            self.assertTrue((self.manager._path(identity).parent/'attempts/attempt-0001/model-output.json').exists())
            self.manager.resume(self.operation(identity));self.manager.run_one(identity)
        self.assertEqual(provider.call_count,1);self.assertEqual(recover.call_count,1)
        self.assertEqual(self.manager.get(identity)['task']['status'],'completed');self.assertEqual(self.commits,1)

    def test_unknown_prior_outcome_never_starts_another_paid_turn(self):
        identity=self.manager.start(self.body())['task']['task_id']
        self.manager._update(identity,lambda row:row.update(status='interrupted',session={'thread_id':'same','turn_id':'T1','pending_turn_intent':'a'*32}))
        self.manager.runner=None
        with patch('app.research_provider.recover_turn',return_value={'status':'unknown'}), \
             patch('app.research_provider.run_turn',side_effect=AssertionError('Unknown outcome cannot be retried')) as provider:
            self.manager.resume(self.operation(identity));self.manager.run_one(identity)
        self.assertEqual(provider.call_count,0);self.assertEqual(self.commits,0)
        self.assertEqual(self.manager.get(identity)['task']['status'],'failed')

    def test_shutdown_waits_beyond_ten_seconds_for_archive_and_rejects_new_work(self):
        entered,released,closed=threading.Event(),threading.Event(),threading.Event()
        def held(*args,**kwargs):
            entered.set();self.assertTrue(released.wait(20))
            return self.archive(*args,**kwargs)
        manager=ResearchTasks(self.store,self.project,config=self.config,runner=self.runner,
            materials=SimpleNamespace(prepare=self.prepare,validate_and_archive=held),autostart=True)
        task=manager.start(self.body())['task'];self.assertTrue(entered.wait(5))
        stopper=threading.Thread(target=lambda:(manager.close(),closed.set()));stopper.start()
        try:
            self.assertFalse(closed.wait(10.1))
            with self.assertRaises(FileExistsError):manager.start(self.body(idempotency_key='not-during-stop'))
            self.assertEqual(manager.get(task['task_id'])['task']['status'],'archiving')
        finally:
            released.set();stopper.join(10);manager.close()
        self.assertTrue(closed.is_set());self.assertEqual(manager.get(task['task_id'])['task']['status'],'completed')

    def test_bad_task_does_not_kill_worker_or_block_a_different_queued_task(self):
        first=self.manager.start(self.body())['task']['task_id']
        second=self.manager.start(self.body(idempotency_key='second-job',question='Different research question'))['task']['task_id']
        path=self.manager._path(first);value=json.loads(path.read_text(encoding='utf-8'));value['question']='changed'
        path.write_text(json.dumps(value),encoding='utf-8')
        worker=threading.Thread(target=self.manager._loop);worker.start()
        self.manager._queue.put(first);self.manager._queue.put(second)
        self.manager._queue.join()
        self.manager._closing.set();self.manager._queue.put(None);worker.join(5)
        self.assertFalse(worker.is_alive());self.assertEqual(self.manager.get(second)['task']['status'],'completed')
        self.assertEqual(len(self.calls),1)

    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.base=Path(temp.name);self.root=self.base/'private';self.project=self.base/'project'
        self.project.mkdir();lab.init(self.root);lab.ensure_security(self.root,IDENTITY)
        self.store=ResearchStore(self.root,TOOL)
        executable=self.base/'fake-codex.exe';executable.write_bytes(b'not an executable')
        self.config={'codex_executable':str(executable),'project_dir':str(self.project),
                     'project_id':'00000000-0000-4000-8000-000000000001'}
        self.calls=[];self.commits=0
        self.materials=SimpleNamespace(prepare=self.prepare,validate_and_archive=self.archive)
        self.manager=self.make_manager()
        self.addCleanup(self.manager.close)

    def make_manager(self):
        return ResearchTasks(self.store,self.project,config=self.config,runner=self.runner,
                             materials=self.materials,autostart=False)

    def body(self, **changes):
        return {'security_id':IDENTITY['security_id'],'module':'fundamental','provider':'codex','mode':'full',
                'research_cutoff':'2026-01-03','question':'Fictional company only','scope':'frozen fixtures',
                'source_ids':[],'item_ids':[],'idempotency_key':'fixture-submission-1',**changes}

    def prepare(self,store,task,attempt):
        return {'prompt':'Fictional immutable input','output_schema':{'type':'object'},
                'input_fingerprint':digest({'security':task['security'],'asof':task['research_asof']})}

    def runner(self,config,provider,prompt,workspace,session=None,emit=None,cancel=None,output_schema=None):
        self.calls.append({'provider':provider,'workspace':str(workspace),'session':session,'prompt':prompt})
        emit({'kind':'session','session':{'thread_id':'fixture-thread','provider':provider}})
        emit({'kind':'stage','label':'正在核对虚构资料','text':'只读冻结输入'})
        return {'status':'completed','session':{'thread_id':'fixture-thread','provider':provider},
                'json':{'status':'completed','questions':[],'reports':[{'module':'fundamental'}]}}

    def archive(self,store,task,attempt,output,cancel=None):
        result_id=task['result_ids']['fundamental']
        if not any(row['result_id']==result_id for row in store.lab.scan(store.root)['results']):
            report=attempt/'report.md';report.write_text('# Fictional service-owned archive',encoding='utf-8')
            store.lab.archive(store.root,{'security':task['security'],'module':'fundamental','module_version':'fixture',
                'result_id':result_id,'generated_at':store.lab.now(),'research_asof':task['research_asof'],
                'data_asof':None,'parent_result_ids':[],'report_file':str(report),'evidence':[],'items':[]})
            self.commits+=1
        return {'results':[{'module':'fundamental','content_role':'full_research','result_id':result_id,'report_id':'result:'+result_id}],
                'review_ids':[]}

    def operation(self,task_id):
        task=self.manager.get(task_id)['task']
        return {'task_id':task_id,'revision_token':task['revision_token']}

    def test_duplicate_and_natural_active_submission_only_run_once(self):
        first=self.manager.start(self.body())
        self.assertFalse(first['reused']);identity=first['task']['task_id']
        self.assertEqual(self.manager.start(self.body())['task']['task_id'],identity)
        natural=self.manager.start(self.body(idempotency_key='another-submission'))
        self.assertTrue(natural['reused']);self.assertEqual(natural['task']['task_id'],identity)
        self.manager.run_one(identity);self.manager.run_one(identity)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.commits,1)
        self.assertEqual(self.manager.start(self.body())['task']['status'],'completed')
        self.assertEqual(len(self.manager.list()['tasks']),1)

    def test_parallel_duplicate_start_has_one_durable_identity(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.manager.start(self.body()))) for _ in range(3)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertEqual(len(results),3)
        self.assertEqual(len({row['task']['task_id'] for row in results}),1)
        self.assertEqual(sum(not row['reused'] for row in results),1)

    def test_same_submission_token_with_different_content_is_rejected(self):
        self.manager.start(self.body())
        with self.assertRaises(FileExistsError):self.manager.start(self.body(question='Different'))
        self.assertEqual(len(self.manager.list()['tasks']),1)

    def test_queued_cancel_does_not_call_provider_or_archive_and_can_resume(self):
        identity=self.manager.start(self.body())['task']['task_id']
        task=self.manager.cancel(self.operation(identity))['task']
        self.assertEqual(task['status'],'cancelled')
        self.manager.run_one(identity)
        self.assertEqual(self.calls,[]);self.assertEqual(self.commits,0)
        self.manager.resume(self.operation(identity));self.manager.run_one(identity)
        self.assertEqual(self.manager.get(identity)['task']['status'],'completed')
        self.assertEqual(len(self.calls),1)

    def test_running_cancel_retains_same_session_for_explicit_resume(self):
        started=threading.Event()
        def waiting(config,provider,prompt,workspace,session=None,emit=None,cancel=None,output_schema=None):
            self.calls.append({'session':session})
            emit({'kind':'session','session':{'thread_id':'same','provider':provider}})
            started.set();self.assertTrue(cancel.wait(8))
            return {'status':'cancelled','session':{'thread_id':'same','provider':provider}}
        self.manager.runner=waiting
        identity=self.manager.start(self.body())['task']['task_id']
        worker=threading.Thread(target=self.manager.run_one,args=(identity,));worker.start()
        self.assertTrue(started.wait(5))
        task=self.manager.cancel(self.operation(identity))['task'];self.assertEqual(task['status'],'cancelling')
        worker.join(10);self.assertFalse(worker.is_alive())
        self.assertEqual(self.manager.get(identity)['task']['status'],'cancelled')
        self.assertEqual(self.commits,0)
        self.manager.runner=self.runner
        self.manager.resume(self.operation(identity));self.manager.run_one(identity)
        self.assertEqual(self.calls[-1]['session']['thread_id'],'same')
        self.assertEqual(self.commits,1)

    def test_waiting_question_answer_is_bound_and_does_not_run_until_all_answered(self):
        def asking(*args,**kwargs):
            self.calls.append('question')
            return {'status':'completed','session':{'thread_id':'same'},'json':{'status':'needs_input','reports':[],
                'questions':[{'question':'Choose period','options':['Annual','Quarter']},{'question':'Choose scope','options':[]}]}}
        self.manager.runner=asking
        identity=self.manager.start(self.body())['task']['task_id'];self.manager.run_one(identity)
        task=self.manager.get(identity)['task'];self.assertEqual(task['status'],'waiting_input')
        self.assertEqual(self.commits,0)
        old=self.operation(identity)
        task=self.manager.answer({**old,'question_id':task['questions'][0]['question_id'],'answers':'Annual'})['task']
        self.assertEqual(task['status'],'waiting_input')
        with self.assertRaises(FileExistsError):self.manager.answer({**old,'question_id':task['questions'][0]['question_id'],'answers':'Again'})
        task=self.manager.answer({**self.operation(identity),'question_id':task['questions'][1]['question_id'],'answers':'Local only'})['task']
        self.assertEqual(task['status'],'queued');self.assertEqual(len(self.calls),1)
        self.manager.runner=self.runner;self.manager.run_one(identity)
        self.assertIn('Annual',self.calls[-1]['prompt']);self.assertEqual(self.commits,1)

    def test_restart_marks_queued_interrupted_without_model_and_explicit_resume(self):
        identity=self.manager.start(self.body())['task']['task_id']
        restarted=self.make_manager();self.addCleanup(restarted.close)
        self.assertEqual(restarted.get(identity)['task']['status'],'interrupted');self.assertEqual(self.calls,[])
        task=restarted.get(identity)['task'];restarted.resume({'task_id':identity,'revision_token':task['revision_token']})
        restarted.run_one(identity);self.assertEqual(len(self.calls),1)

    def test_commit_then_failure_resumes_archive_without_second_paid_turn(self):
        committed=False
        def fail_after_save(*args,**kwargs):
            nonlocal committed
            result=self.archive(*args,**kwargs)
            if not committed:
                committed=True;raise OSError('Simulated state/read failure after commit')
            return result
        self.manager.materials=SimpleNamespace(prepare=self.prepare,validate_and_archive=fail_after_save)
        identity=self.manager.start(self.body())['task']['task_id'];self.manager.run_one(identity)
        task=self.manager.get(identity)['task'];self.assertEqual(task['status'],'partial')
        self.assertEqual(len(self.calls),1);self.assertEqual(self.commits,1)
        self.manager.resume(self.operation(identity));self.manager.run_one(identity)
        self.assertEqual(self.manager.get(identity)['task']['status'],'completed')
        self.assertEqual(len(self.calls),1);self.assertEqual(self.commits,1)

    def test_frozen_input_or_engine_change_stops_resume(self):
        identity=self.manager.start(self.body())['task']['task_id']
        self.manager.cancel(self.operation(identity));self.config['different']='changed'
        with self.assertRaises(ValueError):self.manager.resume(self.operation(identity))
        self.assertEqual(self.calls,[])

    def test_old_revision_and_cross_security_item_are_rejected(self):
        task=self.manager.start(self.body())['task'];old={'task_id':task['task_id'],'revision_token':task['revision_token']}
        self.manager.cancel(old)
        with self.assertRaises(FileExistsError):self.manager.resume(old)
        with self.assertRaises(ValueError):self.manager.start(self.body(idempotency_key='other-item',item_ids=['OTHER-I']))
        self.assertEqual(self.calls,[])

    def test_strict_boundaries_and_private_session_not_in_public_task(self):
        for change in [{'path':'outside'},{'provider':'unknown'},{'module':'minute'},{'source_ids':['../outside']},
                       {'item_ids':['I','I']},{'research_cutoff':'2999-01-01'}, {'question':'a'*2001},
                       {'mode':'broker_update','source_ids':[]}]:
            with self.subTest(change=change),self.assertRaises((ValueError,KeyError,TypeError)):
                self.manager.start(self.body(**change))
        task=self.manager.start(self.body())['task'];self.manager.run_one(task['task_id'])
        public=self.manager.get(task['task_id'])['task']
        self.assertNotIn('session',public);self.assertNotIn('engine_fingerprint',public)
        self.assertFalse(public['conversation_visible'])

    def test_task_file_or_saved_output_tampering_does_not_get_replayed(self):
        identity=self.manager.start(self.body())['task']['task_id']
        path=self.manager._path(identity);value=json.loads(path.read_text(encoding='utf-8'));value['provider']='deepseek'
        path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(ValueError):self.manager.get(identity)
        self.assertEqual(self.calls,[])
