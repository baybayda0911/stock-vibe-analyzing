"""Fictional service-frozen research; no keys, network or model calls."""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.research_materials import prepare, validate_and_archive, validate_output, SCHEMA, ResearchSelectionRequired
from app.broker_cache import retain_pdf, source_file, sources
from app.broker_archive import archive_source
import test_platform_fundamentals as finance_fixture
from test_platform_dashboard import technical_document


SECURITY = finance_fixture.SECURITY


class ResearchMaterialTests(unittest.TestCase):
    setUp = finance_fixture.FundamentalProjectionTests.setUp
    append = finance_fixture.FundamentalProjectionTests.append
    incremental = finance_fixture.FundamentalProjectionTests.incremental

    def task(self, *, module='fundamental', mode='continue', key='TASK1'):
        modules = ['fundamental', 'technical'] if module == 'joint' else [module]
        return {'task_id': key, 'security_id': SECURITY['security_id'], 'module': module, 'mode': mode,
            'research_asof': finance_fixture.ASOF, 'result_ids': {name: key + '-' + name for name in modules},
            'review_id': key + '-REVIEW', 'source_ids': [], 'item_ids': [], 'question': '虚构持续研究问题', 'scope': '只用冻结材料',
            'provider':'codex'}

    def directory(self, task):
        directory = self.root/'_workspace/platform/research-tasks'/task['task_id']/'attempt-1'
        directory.mkdir(parents=True)
        return directory

    def prepared(self, **kwargs):
        task = self.task(**kwargs); directory = self.directory(task)
        result = prepare(self.store, task, directory)
        task['input_fingerprint'] = result['input_fingerprint']
        return task, directory, result

    def output(self, task, directory):
        snapshot = json.loads((directory/'frozen-input.json').read_text(encoding='utf-8'))
        modules = ['fundamental', 'technical'] if task['module'] == 'joint' else [task['module']]
        reports=[]
        for module in modules:
            reports.append({'module':module, 'conclusion':'虚构新解释，资料日期与实际覆盖保持旧值。',
                'report_markdown':'\n\n'.join('## '+str(index)+'. 虚构章节\n\n这是有内容的虚构研究说明，不代表真实公司判断或新取得事实。' for index in range(1,11)),
                'new_observations':[], 'review_notes':[{'item_id':row['item_id'],'status':'keep',
                    'reason':'尚无新实际证据，继续沿原定义核查，不修改原观察窗口。'} for row in snapshot['old_items'] if row['module']==module]})
        return {'schema_version':SCHEMA,'status':'completed','questions':[],'reports':reports}

    def snapshot(self, directory):
        return json.loads((directory/'frozen-input.json').read_text(encoding='utf-8'))

    def old_bytes(self):
        return {path:path.read_bytes() for path in (self.root/'securities').rglob('*') if path.is_file()}

    def add_technical(self):
        document=technical_document();document['security']=copy.deepcopy(SECURITY)
        for frame in document['timeframes'].values():
            frame['metadata']['security']=copy.deepcopy(SECURITY)
            frame['metadata']['context']['security_id']=SECURITY['security_id']
        paths=[]
        for name,payload,media in [('analysis.json',json.dumps(document).encode(),'application/json'),
                ('technical-1d.png',b'\x89PNG\r\n\x1a\nfictional graph','image/png'),
                ('details.html',b'<html><body>Fictional six-topic details</body></html>','text/html')]:
            path=self.base/name;path.write_bytes(payload)
            paths.append({'source_file':str(path),'relative_path':name,'media_type':media})
        report=self.base/'technical.md';report.write_text('# 虚构技术\n\n**日线判断：旧诊断。**\n\n![虚构图](technical-1d.png)',encoding='utf-8')
        self.store.lab.archive(self.root,{'security':SECURITY,'module':'technical','module_version':'fixture','result_id':'T1',
            'generated_at':'2026-11-13T18:30:00+08:00','research_asof':'2026-11-13T18:00:00+08:00',
            'data_asof':'2026-11-13T15:00:00+08:00','report_file':str(report),'evidence':[],'items':[],
            'parent_result_ids':[],'attachments':paths})
        return document

    def reviewed_source(self):
        payload=b'%PDF-1.7 fictional broker source'
        pages=[{'page':1,'text':'FIN 虚构研报 2026年1月5日 来源测试'}]
        record={'source_id':'index-'+'a'*64,'row_sha256':'b'*64,'security_id':SECURITY['security_id'],
            'title':'虚构研报','organization':'虚构机构','authors':'虚构作者','publication_date':'2026-01-05',
            'publication_date_precision':'date'}
        with patch('app.broker_cache._extract',return_value=(pages,'text_extracted','虚构原文')):
            source=retain_pdf(self.store,SECURITY['security_id'],record,payload,
                'https://pdf.dfcfw.com/pdf/H3_AP20260105123456_1.pdf',
                review={'index_source_id':record['source_id'],'pdf_sha256':hashlib.sha256(payload).hexdigest(),
                    'reason':'虚构页级身份核对','evidence_pages':[1]},views=[{'text':'虚构券商观点，非公司实际业绩','page':1}])
        with patch('app.broker_archive.compatibility',return_value={'enabled':True}):
            archive_source(self.store,SECURITY['security_id'],source['source_id'])
        return source

    def test_prepare_freezes_once_and_does_not_send_machine_paths(self):
        task,directory,result=self.prepared()
        self.assertTrue(result['has_fundamental_data'])
        self.assertNotIn(str(self.root),result['prompt'])
        self.assertIn('stock-fundamental-analysis',result['prompt'])
        self.assertIn('stock-research-archive',result['prompt'])
        with patch.object(self.store,'context',side_effect=AssertionError('must reuse frozen material')):
            again=prepare(self.store,task,directory)
        self.assertEqual(result['input_fingerprint'],again['input_fingerprint'])
        self.assertEqual(result['prompt'],again['prompt'])

    def test_changed_frozen_file_or_task_fingerprint_is_rejected(self):
        task,directory,result=self.prepared()
        source=self.snapshot(directory)['sources'][0]
        (directory/source['report_file']).write_text('altered',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'fingerprint'):prepare(self.store,task,directory)
        (directory/source['report_file']).write_text('# restored but not same bytes',encoding='utf-8')
        task['input_fingerprint']='0'*64
        with self.assertRaises(ValueError):prepare(self.store,task,directory)

    def test_full_fundamental_updates_dashboard_and_preserves_old_source_bytes(self):
        task,directory,result=self.prepared(mode='full')
        old=self.old_bytes()
        output=self.output(task,directory)
        output['reports'][0]['new_observations']=[{'text':'虚构新观察','what':'现金与利润配比','why':'避免仅看利润',
            'next_evidence':'下次同口径现金流披露'}]
        saved=validate_and_archive(self.store,task,directory,output)
        self.assertEqual(saved['results'][0]['content_role'],'full_research')
        view=self.store.fundamental_dashboard(SECURITY['security_id'])
        self.assertEqual(view['source']['result_id'],task['result_ids']['fundamental'])
        self.assertEqual(view['latest']['revenue'],1200)
        self.assertEqual(view['latest']['report_date'],'2026-09-30')
        record=next(row for row in self.store.context(SECURITY['security_id'])['results'] if row['result_id']==task['result_ids']['fundamental'])
        data=json.loads(self.store._record_reader(record)('financial-bottom-tables.json')[0])
        self.assertEqual(data['generated_at'],'2026-11-01T18:00:00+08:00')
        self.assertTrue(data['s4_inheritance']['values_unchanged'])
        self.assertEqual(record['original_items'][0]['type'],'observation')
        self.assertEqual(record['original_items'][0]['current_use'],'needs_review')
        self.assertTrue(all(path.read_bytes()==payload for path,payload in old.items()))

    def test_continue_copies_calculation_without_changing_its_original_dates(self):
        self.append('F2',{'calculations.json':self.incremental()},role='incremental_research',parents=['F1'])
        task,directory,result=self.prepared()
        saved=validate_and_archive(self.store,task,directory,self.output(task,directory))
        self.assertEqual(saved['results'][0]['content_role'],'incremental_research')
        context=self.store.context(SECURITY['security_id'])
        record=next(row for row in context['results'] if row['result_id']==task['result_ids']['fundamental'])
        calculation=json.loads(self.store._record_reader(record)('calculations.json')[0])
        self.assertEqual(calculation['generated_at'],'2026-11-13T18:00:00+08:00')
        self.assertEqual(calculation['financial_period'],'2026-01-01/2026-09-30')
        self.assertEqual(self.store.fundamental_dashboard(SECURITY['security_id'])['latest_quarter']['revenue'],800)
        review=next(row for row in context['reviews'] if row['review_id']==task['review_id'])
        self.assertIsNone(review['data_asof'])
        self.assertEqual(review['updates'][0]['status'],'pending')
        self.assertNotIn('valid_until',review['updates'][0])

    def test_same_output_reuses_results_and_review_while_different_output_cannot(self):
        task,directory,_=self.prepared();output=self.output(task,directory)
        validate_and_archive(self.store,task,directory,output)
        saved=validate_and_archive(self.store,task,directory,output)
        self.assertTrue(saved['results'][0]['reused'])
        context=self.store.context(SECURITY['security_id'])
        self.assertEqual(len(context['results']),2)
        self.assertEqual(len(context['reviews']),1)
        changed=copy.deepcopy(output);changed['reports'][0]['conclusion']='另一个结论'
        with self.assertRaisesRegex(ValueError,'frozen model output'):validate_and_archive(self.store,task,directory,changed)

    def test_model_paths_extra_metadata_or_missing_old_review_are_rejected(self):
        task,directory,_=self.prepared();output=self.output(task,directory)
        for change in [lambda row:row.update(report_file='/outside/auth.json'),
                lambda row:row.update(execution={'provider':'forged','actual_model':'forged','platform_version':'99.0.0'}),
                lambda row:row.update(review_notes=[]),
                lambda row:row['review_notes'][0].update(status='fulfilled')]:
            altered=copy.deepcopy(output);change(altered['reports'][0])
            with self.assertRaises(ValueError):validate_and_archive(self.store,task,directory,altered)
        self.assertEqual(len(self.store.context(SECURITY['security_id'])['results']),1)

    def test_execution_provenance_is_service_owned_and_retains_archive_generation(self):
        task=self.task();task['provider']='deepseek'
        directory=self.directory(task);prepare(self.store,task,directory)
        task['session']={'model':'deepseek-chat','thread_id':'private-thread-not-for-report',
                         'config_path':'C:\\private\\engine.json'}
        output=self.output(task,directory)
        altered=copy.deepcopy(output);altered['execution']={'actual_model':'forged'}
        with self.assertRaises(ValueError):validate_and_archive(self.store,task,directory,altered)
        saved=validate_and_archive(self.store,task,directory,output)
        record=next(row for row in self.store.context(SECURITY['security_id'])['results']
                    if row['result_id']==saved['results'][0]['result_id'])
        read=self.store._record_reader(record)
        component=json.loads(read('research-components.json')[0])
        inheritance=json.loads(read('research-inheritance.json')[0])
        version=(Path(__file__).resolve().parents[1]/'VERSION').read_text(encoding='utf-8-sig').strip()
        expected={'provider':'deepseek','actual_model':'deepseek-chat','platform_version':version}
        self.assertEqual(component['execution'],expected)
        self.assertEqual(inheritance['execution'],expected)
        self.assertEqual(inheritance['generated_at'],record['generated_at'])
        body=self.store.document(saved['results'][0]['report_id'])[0].decode('utf-8')
        self.assertIn('本次执行来源：deepseek；实际模型：deepseek-chat；平台版本：'+version,body)
        self.assertNotIn('private-thread-not-for-report',body+json.dumps(component)+json.dumps(inheritance))
        self.assertNotIn('engine.json',body+json.dumps(component)+json.dumps(inheritance))
        task['session']['model']='another-model-after-commit'
        replay=validate_and_archive(self.store,task,directory,output)
        self.assertTrue(replay['results'][0]['reused'])
        self.assertEqual(json.loads(read('research-components.json')[0])['execution'],expected)

    def test_complete_fundamental_requires_ten_substantive_chapters(self):
        task,directory,_=self.prepared(mode='full');output=self.output(task,directory)
        output['reports'][0]['report_markdown']='## 1. 只有一章\n\n缺乏完整研究章节，不能冒称完整。'
        with self.assertRaisesRegex(ValueError,'ten substantive'):validate_and_archive(self.store,task,directory,output)

    def test_natural_wait_for_annual_report_cannot_invent_not_due_deadline(self):
        task,directory,prepared=self.prepared();output=self.output(task,directory)
        note=output['reports'][0]['review_notes'][0]
        note.update(status='not_due',reason='等年报披露后再看，不是结构化期限。')
        with self.assertRaisesRegex(ValueError,note['item_id']):validate_output(task,self.snapshot(directory),output)
        self.assertIn('自然语句不是明示期限',prepared['prompt'])
        self.assertIn('摘要／看板的60根窗口不等于继承图片',prepared['prompt'])

    def test_full_fundamental_keeps_registered_a_charts_in_its_report(self):
        source=self.base/'graph-history.json';source.write_text(json.dumps(self.history),encoding='utf-8')
        report=self.base/'graph-source.md';report.write_text('# 虚构图表研究',encoding='utf-8')
        attachments=[{'source_file':str(source),'relative_path':'financial-bottom-tables.json','media_type':'application/json'}]
        for name in ('annual-A-2025.png','quarterly-a.png'):
            image=self.base/name;image.write_bytes(b'\x89PNG\r\n\x1a\nfictional A graph')
            attachments.append({'source_file':str(image),'relative_path':name,'media_type':'image/png'})
        self.store.lab.archive(self.root,{'security':SECURITY,'module':'fundamental','module_version':'fixture','result_id':'FGRAPH',
            'generated_at':'2026-11-13T18:00:00+08:00','research_asof':'2026-11-13T17:00:00+08:00','data_asof':None,
            'parent_result_ids':['F1'],'report_file':str(report),'evidence':[],'items':[],'attachments':attachments})
        task,directory,_=self.prepared(mode='full')
        result=validate_and_archive(self.store,task,directory,self.output(task,directory))
        body=self.store.document(result['results'][0]['report_id'])[0].decode('utf-8')
        self.assertIn('](annual-A-2025.png)',body)
        self.assertIn('](quarterly-a.png)',body)
        self.assertIn('新解释不更新原估值情景或价格资格',body)

    def test_service_does_not_repeat_model_referenced_registered_charts(self):
        self.add_technical()
        source=self.base/'dedup-history.json';source.write_text(json.dumps(self.history),encoding='utf-8')
        report=self.base/'dedup-source.md';report.write_text('# 虚构图表研究',encoding='utf-8')
        attachments=[{'source_file':str(source),'relative_path':'financial-bottom-tables.json','media_type':'application/json'}]
        for name in ('annual-A.png','quarterly-A.png'):
            image=self.base/name;image.write_bytes(b'\x89PNG\r\n\x1a\nfictional A graph')
            attachments.append({'source_file':str(image),'relative_path':name,'media_type':'image/png'})
        self.store.lab.archive(self.root,{'security':SECURITY,'module':'fundamental','module_version':'fixture','result_id':'FDEDUP',
            'generated_at':'2026-11-13T18:00:00+08:00','research_asof':'2026-11-13T17:00:00+08:00','data_asof':None,
            'parent_result_ids':['F1'],'report_file':str(report),'evidence':[],'items':[],'attachments':attachments})
        task,directory,_=self.prepared(module='joint',mode='full');output=self.output(task,directory)
        output['reports'][0]['report_markdown']+='\n\n![继承年度](annual-A.png)\n\n![继承季度](quarterly-A.png)'
        output['reports'][1]['report_markdown']+='\n\n![继承技术](technical-1d.png)'
        saved=validate_and_archive(self.store,task,directory,output)
        for row in saved['results']:
            body=self.store.document(row['report_id'])[0].decode('utf-8')
            for image in ('annual-A.png','quarterly-A.png') if row['module']=='fundamental' else ('technical-1d.png',):
                self.assertEqual(body.count(']('+image+')'),1)

    def test_needs_input_saves_no_result_and_no_empty_security(self):
        task,directory,_=self.prepared()
        before=self.old_bytes()
        output={'schema_version':SCHEMA,'status':'needs_input','questions':[{'id':'q1','question':'缺少实际披露，先核哪项？',
            'options':['补充原始数据','继续等待']}],'reports':[]}
        saved=validate_and_archive(self.store,task,directory,output)
        self.assertTrue(saved['needs_input'])
        self.assertEqual(saved['results'],[])
        self.assertEqual(self.old_bytes(),before)

    def test_cancel_before_commit_does_not_create_formal_record(self):
        task,directory,_=self.prepared()
        with self.assertRaises(InterruptedError):validate_and_archive(self.store,task,directory,self.output(task,directory),lambda:True)
        self.assertEqual(len(self.store.context(SECURITY['security_id'])['results']),1)

    def test_joint_partial_commit_recovers_without_rearchiving_first_module(self):
        original=self.add_technical()
        task,directory,_=self.prepared(module='joint',mode='full');output=self.output(task,directory)
        calls=[]
        def cancel():
            calls.append(1)
            return len(calls)==3
        with self.assertRaises(InterruptedError):validate_and_archive(self.store,task,directory,output,cancel)
        context=self.store.context(SECURITY['security_id'])
        self.assertIn(task['result_ids']['fundamental'],{row['result_id'] for row in context['results']})
        self.assertNotIn(task['result_ids']['technical'],{row['result_id'] for row in context['results']})
        saved=validate_and_archive(self.store,task,directory,output)
        self.assertTrue(saved['results'][0]['reused'])
        technical=next(row for row in self.store.context(SECURITY['security_id'])['results'] if row['result_id']==task['result_ids']['technical'])
        document=json.loads(self.store._record_reader(technical)('analysis.json')[0])
        self.assertEqual(document['generated_at'],original['generated_at'])
        self.assertEqual(document['timeframes']['1d']['bars'],original['timeframes']['1d']['bars'])
        self.assertEqual(document['market_cutoff'],original['market_cutoff'])
        self.assertIn(task['result_ids']['fundamental'],technical['parent_result_ids'])
        self.assertEqual(self.store._record_reader(technical)('details.html')[0],b'<html><body>Fictional six-topic details</body></html>')
        dashboard=self.store.technical_dashboard(SECURITY['security_id'])
        self.assertEqual(dashboard['source']['result_id'],task['result_ids']['technical'])
        self.assertEqual(dashboard['source']['analysis_generated_at'],original['generated_at'])

    def test_broker_update_remains_supplement_and_tracks_actual_result_id(self):
        source=self.reviewed_source()
        task=self.task(mode='broker_update');task['source_ids']=[source['source_id']];task['research_asof']=self.store.lab.now()
        directory=self.directory(task);prepared=prepare(self.store,task,directory);task['input_fingerprint']=prepared['input_fingerprint']
        saved=validate_and_archive(self.store,task,directory,self.output(task,directory))
        context=self.store.context(SECURITY['security_id'])
        self.assertEqual(context['roles']['formal_fundamental']['result_id'],'F1')
        self.assertEqual(saved['results'][0]['content_role'],'source_adaptation')
        self.assertEqual(saved['source_usage'][0]['result_ids'],[task['result_ids']['fundamental']])
        self.assertFalse(saved['source_usage'][0]['formal_replaced'])
        self.assertEqual(sources(self.store,SECURITY['security_id'])[0]['publication_date'],'2026-01-05')

    def test_later_manual_review_is_not_overwritten_by_stale_task(self):
        task,directory,_=self.prepared()
        report=self.base/'manual-review.md';report.write_text('人工新回顾',encoding='utf-8')
        now=self.store.lab.now()
        self.store.lab.review(self.root,{'security_id':SECURITY['security_id'],'review_id':'NEW-MANUAL',
            'generated_at':now,'research_asof':now,'data_asof':None,'evidence':[],'report_file':str(report),
            'updates':[{'item_id':'OLD-CASH','status':'cannot_determine','reason':'新人工说明','current_use':'needs_review'}]})
        saved=validate_and_archive(self.store,task,directory,self.output(task,directory))
        self.assertTrue(saved['warnings']);self.assertEqual(saved['review_ids'],[])
        context=self.store.context(SECURITY['security_id'])
        self.assertEqual(context['pending_items'][0]['status'],'cannot_determine')
        self.assertEqual(len(context['reviews']),1)

    def test_explicit_observed_item_can_be_revisited_without_reopening_history(self):
        report=self.base/'historical.md';report.write_text('历史观察已完成',encoding='utf-8')
        self.store.lab.review(self.root,{'security_id':SECURITY['security_id'],'review_id':'OBSERVED-REVIEW',
            'generated_at':'2026-11-12T18:00:00+08:00','research_asof':'2026-11-12T17:00:00+08:00',
            'data_asof':None,'evidence':[],'report_file':str(report),
            'updates':[{'item_id':'OLD-CASH','status':'observed','current_use':'historical','reason':'仅保留历史观察'}]})
        task=self.task();task['item_ids']=['OLD-CASH'];directory=self.directory(task)
        result=prepare(self.store,task,directory);task['input_fingerprint']=result['input_fingerprint']
        snapshot=self.snapshot(directory);item=snapshot['old_items'][0]
        self.assertTrue(item['review_keep_only'])
        self.assertEqual(item['source_result_id'],'F1');self.assertEqual(item['module'],'fundamental')
        output=self.output(task,directory)
        for status in ('cannot_determine','not_due'):
            invalid=copy.deepcopy(output);invalid['reports'][0]['review_notes'][0]['status']=status
            with self.assertRaisesRegex(ValueError,'Closed or historical'):validate_and_archive(self.store,task,directory,invalid)
        validate_and_archive(self.store,task,directory,output)
        context=self.store.context(SECURITY['security_id'])
        state=next(row for row in context['results'] if row['result_id']=='F1')['item_states'][0]
        self.assertEqual(state['status'],'observed');self.assertEqual(state['current_use'],'historical')
        self.assertFalse(any(row['item_id']=='OLD-CASH' for row in context['pending_items']))
        self.assertEqual(state['definition'],item['definition'])

    def test_explicit_item_from_another_module_is_rejected_before_model(self):
        self.add_technical()
        task=self.task(module='technical');task['item_ids']=['OLD-CASH'];directory=self.directory(task)
        with self.assertRaisesRegex(ValueError,'requested module'):prepare(self.store,task,directory)

    def test_fundamental_freezes_technical_as_dated_reference_without_extra_output(self):
        self.add_technical()
        old=self.old_bytes();task,directory,prepared=self.prepared(module='fundamental')
        snapshot=self.snapshot(directory);reference=snapshot['cross_module_references']['technical']
        self.assertEqual(reference['result_id'],'T1');self.assertEqual(reference['reference_use'],'historical_only')
        self.assertEqual(reference['data_asof'],'2026-11-13T15:00:00+08:00')
        source=next(row for row in snapshot['sources'] if row['result_id']=='T1')
        self.assertTrue(source['reference_only']);self.assertEqual(source['files'],[])
        self.assertIn('historical_only只能历史对照',prepared['prompt'])
        self.assertEqual({row['module'] for row in snapshot['old_items']},{'fundamental'})
        saved=validate_and_archive(self.store,task,directory,self.output(task,directory))
        self.assertEqual([row['module'] for row in saved['results']],['fundamental'])
        context=self.store.context(SECURITY['security_id'])
        record=next(row for row in context['results'] if row['result_id']==task['result_ids']['fundamental'])
        self.assertNotIn('analysis.json',{row['relative_path'] for row in record['attachments']})
        body=self.store.document(saved['results'][0]['report_id'])[0].decode('utf-8')
        self.assertIn('技术面：T1',body);self.assertIn('仅作历史对照，不作为当前结论',body)
        self.assertTrue(all(path.read_bytes()==payload for path,payload in old.items()))

    def test_technical_freezes_fundamental_reference_but_not_its_items_or_components(self):
        self.add_technical();task,directory,_=self.prepared(module='technical')
        snapshot=self.snapshot(directory)
        self.assertEqual(snapshot['cross_module_references']['fundamental']['result_id'],'F1')
        self.assertEqual(snapshot['cross_module_references']['fundamental']['research_asof'],'2026-11-01T18:00:00+08:00')
        self.assertEqual(snapshot['old_items'],[])
        self.assertEqual(next(row for row in snapshot['sources'] if row['result_id']=='F1')['files'],[])
        saved=validate_and_archive(self.store,task,directory,self.output(task,directory))
        self.assertEqual([row['module'] for row in saved['results']],['technical'])
        record=next(row for row in self.store.context(SECURITY['security_id'])['results'] if row['result_id']==task['result_ids']['technical'])
        self.assertNotIn('financial-bottom-tables.json',{row['relative_path'] for row in record['attachments']})
        self.assertFalse(any(row.get('source_result_id')=='F1' for row in record['evidence']))
        self.assertIn('基本面：F1',self.store.document(saved['results'][0]['report_id'])[0].decode('utf-8'))

    def test_cross_module_reference_cannot_cross_the_requested_knowledge_cutoff(self):
        self.add_technical();task=self.task();task['research_asof']='2026-11-12T23:00:00+08:00'
        directory=self.directory(task);prepare(self.store,task,directory)
        snapshot=self.snapshot(directory)
        self.assertEqual(snapshot['cross_module_references'],{})
        self.assertNotIn('T1',{row['result_id'] for row in snapshot['sources']})

    def test_over_fifty_old_items_rejects_preparation_without_truncating_history(self):
        context=self.store.context(SECURITY['security_id'],finance_fixture.ASOF)
        original=context['pending_items'][0]
        context['pending_items']=[{**copy.deepcopy(original),'item_id':'OLD-'+str(index)} for index in range(51)]
        before=copy.deepcopy(context['pending_items']);task=self.task();directory=self.directory(task)
        with patch.object(self.store,'context',return_value=context):
            with self.assertRaisesRegex(ResearchSelectionRequired,'基本面有51个.*尚未启动模型'):
                prepare(self.store,task,directory)
        self.assertEqual(context['pending_items'],before)
        self.assertFalse((directory/'frozen-input.json').exists())
        self.assertEqual(len(self.store.context(SECURITY['security_id'])['results']),1)
