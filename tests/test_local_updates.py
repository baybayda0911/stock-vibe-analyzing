"""Fictional append-only data updates; the technical calculator is counted and local."""
import csv
from datetime import date, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from app.local_updates import LocalUpdates, _encoded
from app.local_market import HOLIDAYS_2026
from app.store import ResearchStore
from test_local_financial import FIELDS, row
from test_platform_server import lab, TOOL

SECURITY = {'security_id':'A-share-sz-000001','company':'虚构本地财务公司','market':'A-share',
            'exchange':'sz','code':'000001','share_class':'A','currency':'CNY'}
NOW = '2026-11-15T10:00:00+08:00'


class LocalUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'private'
        self.quant = self.base/'quant'
        self.market = self.quant/'stock-trading-data-pro'
        self.market.mkdir(parents=True)
        self.finance = self.quant/'stock-fin-data-xbx/sz000001/sz000001_一般企业.csv'
        self.finance.parent.mkdir(parents=True)
        self._data()
        lab.init(self.root)
        report = self.base/'old.md'; report.write_text('# 原研究\n\n原报告不可覆盖。\n',encoding='utf-8')
        component=self.base/'old-components.json'
        component.write_bytes(_encoded({'schema_version':'stock-research-components/1','security':SECURITY,
            'result_id':'OLD-F','content_role':'full_research','components':[]}))
        lab.archive(self.root,{'security':SECURITY,'module':'fundamental','module_version':'fixture','result_id':'OLD-F',
            'generated_at':'2026-11-14T18:00:00+08:00','research_asof':'2026-11-14T17:00:00+08:00',
            'data_asof':None,'report_file':str(report),'parent_result_ids':[],'evidence':[],
            'attachments':[{'relative_path':'research-components.json','source_file':str(component),'media_type':'application/json'}],
            'items':[{'item_id':'OLD-OBS','type':'observation','text':'原现金观察','report_locator':'old section',
                'frozen_at':'2026-11-14T18:00:00+08:00','definition':{'what':'现金','why':'质量','next_evidence':'后续披露'},
                'status':'pending','current_use':'needs_review','evidence_ids':[]}]})
        self.store = ResearchStore(self.root,TOOL,self.market)
        config = self.root/'_workspace/platform/local-data-config.json'
        config.parent.mkdir(parents=True,exist_ok=True)
        config.write_bytes(_encoded({'schema_version':'stock-local-update-config/1','enabled':True,'technical_python':sys.executable}))
        self.now = patch.object(self.store.lab,'now',return_value=NOW)
        self.now.start(); self.addCleanup(self.now.stop)
        self.manager = LocalUpdates(self.store)
        self.addCleanup(self.manager.close)
        runtime=patch.object(self.manager,'_runtime',return_value={'probe':'fictional-test','talib':'fictional-version'})
        runtime.start();self.addCleanup(runtime.stop)

    def _data(self):
        finance_rows=[row('20250930','2025-10-25',450,45),row('20251231','2026-04-25',700,70),
            row('20260331','2026-04-25',200,20),row('20260630','2026-08-21',500,50),row('20260930','2026-10-25',900,90)]
        stream=io.StringIO(newline='');stream.write('虚构财务说明\n')
        writer=csv.DictWriter(stream,fieldnames=list(finance_rows[0]));writer.writeheader();writer.writerows(finance_rows)
        self.finance.write_text(stream.getvalue(),encoding='utf-8-sig')
        stream=io.StringIO(newline='');stream.write('虚构行情\n')
        fields=['股票代码','股票名称','交易日期','开盘价','最高价','最低价','收盘价','前收盘价','成交量','成交额']
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for day in ('2026-11-09','2026-11-10','2026-11-11','2026-11-12','2026-11-13'):
            writer.writerow(dict(zip(fields,['sz000001',SECURITY['company'],day,10,12,9,11,11,100,1000])))
        (self.market/'sz000001.csv').write_text(stream.getvalue(),encoding='utf-8-sig')
        holidays=set()
        for start,end in HOLIDAYS_2026:
            first,last=date.fromisoformat('2026-'+start),date.fromisoformat('2026-'+end)
            holidays.update(first+timedelta(days=i) for i in range((last-first).days+1))
        days=[date(2026,1,1)+timedelta(days=i) for i in range(365)]
        days=[str(day) for day in days if day.weekday()<5 and day not in holidays]
        (self.quant/'period_offset.csv').write_text('虚构计划\n交易日期,W_0\n'+'\n'.join(day+',1' for day in days)+'\n',encoding='utf-8')

    def _fake_technical(self, job, folder):
        target=folder/'fake-technical';target.mkdir(exist_ok=True)
        (target/'report.md').write_text('# 虚构本地技术重算\n',encoding='utf-8')
        market=self.manager._read_frozen(job,'market.json')
        document={'security':job['security'],'timeframes':{'1d':{'bars':market['bars']}}}
        (target/'analysis.json').write_bytes(_encoded(document))
        (target/'research-components.json').write_bytes(_encoded(self.manager._component(job,'technical')))
        (target/'local-update-provenance.json').write_bytes(_encoded({'update_id':job['update_id'],
            'source_hashes':job['source_hashes'],'algorithm':job['algorithm']}))
        self.manager._archive(job,'technical',target,market['market_cutoff'])

    def preview(self):
        return self.manager.preview(SECURITY['security_id'])

    def wait(self):
        self.manager._thread.join(timeout=20)
        self.assertFalse(self.manager._thread.is_alive())

    def test_preview_is_read_only_idempotent_and_public_payload_is_bounded(self):
        before={path:path.read_bytes() for path in (self.root/'securities').rglob('*') if path.is_file()}
        first=self.preview();second=self.preview()
        self.assertEqual(first['update_id'],second['update_id'])
        self.assertEqual(first['status'],'prepared')
        self.assertEqual(first['summary']['market_last_date'],'2026-11-13')
        self.assertEqual(first['summary']['financial_period'],'2026-09-30')
        self.assertEqual(first['results'],[])
        self.assertTrue(self.manager.public(SECURITY['security_id'])['available'])
        self.assertNotIn(str(self.quant),json.dumps(first))
        self.assertNotIn('technical_python',json.dumps(first))
        self.assertEqual(before,{path:path.read_bytes() for path in before})
        self.assertEqual(len(lab.scan(self.root)['results']),1)

    def test_same_source_admits_new_sessions_and_public_versions_without_reusing_old_snapshot(self):
        market_path=self.market/'sz000001.csv'
        with market_path.open('a',encoding='utf-8',newline='') as output:
            csv.writer(output).writerow(['sz000001',SECURITY['company'],'2026-11-16',11,13,10,12,11,120,1440])
        revision=row('20260930','2026-11-16',950,95)
        with self.finance.open('a',encoding='utf-8',newline='') as output:
            csv.DictWriter(output,fieldnames=list(revision)).writerow(revision)
        index_dir=self.quant/'stock-main-index-data';index_dir.mkdir()
        (index_dir/'sh000300.csv').write_text('虚构指数\ncandle_end_time,close,index_code\n'
            '2026-11-13,100,sh000300\n2026-11-16,102,sh000300\n',encoding='utf-8')
        clock=self.store.lab.now
        clock.return_value='2026-11-16T15:29:00+08:00'
        first=self.preview();old=self.manager._read(first['update_id'])
        frozen={path:path.read_bytes() for path in self.manager._path(first['update_id']).iterdir()}
        clock.return_value='2026-11-16T15:31:00+08:00'
        second=self.preview();new=self.manager._read(second['update_id'])
        self.assertNotEqual(first['update_id'],second['update_id'])
        self.assertEqual(old['source_hashes'],new['source_hashes'])
        self.assertEqual(first['summary']['market_last_date'],'2026-11-13')
        self.assertEqual(second['summary']['market_last_date'],'2026-11-16')
        self.assertEqual(old['eligible_selection']['index_last_date'],'2026-11-13')
        self.assertEqual(new['eligible_selection']['index_last_date'],'2026-11-16')
        self.assertEqual(first['summary']['financial_versions'],second['summary']['financial_versions'])
        clock.return_value='2026-11-16T15:32:00+08:00'
        self.assertEqual(self.preview()['update_id'],second['update_id'])
        self.manager._verify(old);self.manager._verify(new)
        clock.return_value='2026-11-17T00:01:00+08:00'
        third=self.preview();newest=self.manager._read(third['update_id'])
        self.assertNotEqual(second['update_id'],third['update_id'])
        self.assertEqual(new['source_hashes'],newest['source_hashes'])
        self.assertEqual(second['summary']['market_last_date'],third['summary']['market_last_date'])
        self.assertEqual(third['summary']['financial_versions'],second['summary']['financial_versions']+1)
        self.assertNotEqual(new['eligible_selection']['financial_versions_sha256'],
                            newest['eligible_selection']['financial_versions_sha256'])
        clock.return_value='2026-11-17T00:02:00+08:00'
        self.assertEqual(self.preview()['update_id'],third['update_id'])
        self.manager._verify(newest)
        self.assertTrue(all(path.read_bytes()==payload for path,payload in frozen.items()))
        self.assertEqual(len(lab.scan(self.root)['results']),1)

    def test_explicit_start_appends_exact_two_results_and_keeps_old_items(self):
        before={path:path.read_bytes() for path in (self.root/'securities').rglob('*') if path.is_file()}
        job=self.preview()
        with patch.object(self.manager,'_calculate_technical',side_effect=self._fake_technical) as calculate:
            self.manager.start(SECURITY['security_id'],job['update_id']);self.wait()
            final=self.manager.public(SECURITY['security_id'])['latest']
            self.assertEqual(final['status'],'completed')
            self.assertEqual(len(final['results']),2)
            self.manager.start(SECURITY['security_id'],job['update_id'])
            self.assertEqual(self.preview()['update_id'],job['update_id'])
            self.assertEqual(calculate.call_count,1)
        context=self.store.context(SECURITY['security_id'])
        self.assertEqual(len(context['results']),3)
        self.assertEqual(len(context['reviews']),0)
        self.assertEqual([item['item_id'] for item in context['pending_items']],['OLD-OBS'])
        self.assertTrue(all(path.read_bytes()==payload for path,payload in before.items()))
        new=next(row for row in context['results'] if row['module']=='fundamental' and row['result_id']!='OLD-F')
        reader=self.store._record_reader(new)
        finance=json.loads(reader('financial-bottom-tables.json')[0]);calc=json.loads(reader('calculations.json')[0])
        self.assertEqual(finance['result_id'],new['result_id'])
        self.assertEqual(finance['local_data_update']['update_id'],job['update_id'])
        self.assertIsNone(calc['ratios']['ttm_pe_parent_profit'])
        self.assertEqual(calc['ttm']['parent_profit'],115)
        self.assertEqual(new['parent_result_ids'],['OLD-F'])

    def test_changed_live_source_or_frozen_payload_rejects_before_calculator(self):
        job=self.preview()
        path=self.market/'sz000001.csv';raw=path.read_bytes();path.write_bytes(raw+b'\n')
        with patch.object(self.manager,'_calculate_technical') as calculate:
            with self.assertRaisesRegex(ValueError,'来源已变化'):
                self.manager.start(SECURITY['security_id'],job['update_id'])
            self.assertFalse(calculate.called)
        path.write_bytes(raw)
        frozen=self.manager._path(job['update_id'])/'financial.json';frozen.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'指纹'):
            self.manager.start(SECURITY['security_id'],job['update_id'])
        self.assertEqual(len(lab.scan(self.root)['results']),1)

    def test_partial_retry_skips_already_archived_financial_result(self):
        job=self.preview()
        with patch.object(self.manager,'_calculate_technical',side_effect=ValueError('private-file secret detail')):
            self.manager.start(SECURITY['security_id'],job['update_id']);self.wait()
        partial=self.manager.public(SECURITY['security_id'])['latest']
        self.assertEqual(partial['status'],'partial')
        self.assertEqual(len(partial['results']),1)
        self.assertNotIn('secret',partial['error'])
        with patch.object(self.manager,'_financial_output',side_effect=AssertionError('must not rerun saved F')), \
             patch.object(self.manager,'_calculate_technical',side_effect=self._fake_technical):
            self.manager.start(SECURITY['security_id'],job['update_id']);self.wait()
        self.assertEqual(self.manager.public(SECURITY['security_id'])['latest']['status'],'completed')
        self.assertEqual(len(lab.scan(self.root)['results']),3)

    def test_shutdown_waits_for_worker_and_duplicate_start_does_not_recalculate(self):
        job=self.preview();entered=threading.Event();release=threading.Event()
        def waiting(job,folder):
            entered.set();self.assertTrue(release.wait(10));self._fake_technical(job,folder)
        with patch.object(self.manager,'_calculate_technical',side_effect=waiting) as calculate:
            self.manager.start(SECURITY['security_id'],job['update_id'])
            self.assertTrue(entered.wait(10))
            self.manager.start(SECURITY['security_id'],job['update_id'])
            closing=threading.Thread(target=self.manager.close);closing.start()
            self.assertTrue(closing.is_alive())
            release.set();closing.join(timeout=10)
            self.assertFalse(closing.is_alive());self.assertEqual(calculate.call_count,1)
        self.assertEqual(self.manager.public(SECURITY['security_id'])['latest']['status'],'completed')

    def test_cross_security_and_unconfigured_requests_are_rejected(self):
        job=self.preview()
        with self.assertRaisesRegex(ValueError,'不属于'):
            self.manager.start('A-share-sz-000002',job['update_id'])
        config=self.root/'_workspace/platform/local-data-config.json'
        config.write_bytes(_encoded({'schema_version':'stock-local-update-config/1','enabled':False,'technical_python':sys.executable}))
        self.assertFalse(self.manager.public(SECURITY['security_id'])['available'])
        with self.assertRaises(ValueError):self.preview()

    def test_restart_requires_explicit_resume_without_automatic_calculation(self):
        public=self.preview();job=self.manager._read(public['update_id'])
        job['status']='running';self.manager._save(job)
        with patch.object(LocalUpdates,'_calculate_technical') as calculate:
            restarted=LocalUpdates(self.store)
            self.addCleanup(restarted.close)
            self.assertEqual(restarted.public(SECURITY['security_id'])['latest']['status'],'interrupted')
            self.assertFalse(calculate.called)
            self.assertEqual(len(lab.scan(self.root)['results']),1)

    def test_tampered_source_path_is_rejected_before_reading_outside_contract(self):
        public=self.preview();job=self.manager._read(public['update_id'])
        outside=self.base/'fictional-private.txt';outside.write_text('fictional placeholder',encoding='utf-8')
        job['source_paths']['market']=str(outside);self.manager._save(job)
        with patch.object(self.store,'_regular',wraps=self.store._regular) as read:
            with self.assertRaisesRegex(ValueError,'固定证券合同'):
                self.manager.start(SECURITY['security_id'],public['update_id'])
            self.assertFalse(any(call.args[0]==outside for call in read.call_args_list))

    def test_known_technical_command_has_frozen_market_background_and_source_binding(self):
        index_dir=self.quant/'stock-main-index-data';index_dir.mkdir()
        index_path=index_dir/'sh000300.csv';index_path.write_text('fictional index',encoding='utf-8')
        index={'bars':[{'time':'2026-11-13T15:30:00+08:00','close':100,'available_at':NOW}],
            'source_sha':hashlib.sha256(index_path.read_bytes()).hexdigest(),'provenance':{'limitations':['Fictional raw price index, not total return']}}
        with patch('app.local_market.load_index',return_value=index):
            job=self.preview()
        seen=[]
        def calculator(command,**kwargs):
            seen.append(command)
            self.assertEqual(command[4],str(TOOL.parents[3]/'skills/stock-technical-analysis/scripts/technical_analysis.py'))
            self.assertEqual(command[5:7],['--mode','analyze'])
            request=json.loads(Path(command[8]).read_bytes())
            self.assertFalse(request['validation']['price_basis'])
            self.assertFalse(request['adjustment']['verified'])
            self.assertEqual(request['market']['id'],'CSI300')
            self.assertEqual(request['market']['bars'],index['bars'])
            output=Path(command[10]);output.mkdir()
            (output/'report.md').write_text('# Fictional technical result\n',encoding='utf-8')
            bars=json.loads(Path(request['input_file']).read_bytes())
            (output/'analysis.json').write_bytes(_encoded({'security':SECURITY,'timeframes':{'1d':{'bars':bars,
                'topics':{'conventional':{'status':'ok','backend':'talib','backend_version':'0.6.8'}},
                'metadata':{'input_sha256':hashlib.sha256(Path(request['input_file']).read_bytes()).hexdigest()}}}}))
            return SimpleNamespace(returncode=0)
        with patch('app.local_updates.subprocess.run',side_effect=calculator):
            self.manager.start(SECURITY['security_id'],job['update_id']);self.wait()
        final=self.manager.public(SECURITY['security_id'])['latest']
        self.assertEqual(final['status'],'completed',final)
        self.assertEqual(len(seen),1)
        def degraded(command,**kwargs):
            result=calculator(command,**kwargs)
            analysis_path=Path(command[10])/'analysis.json'
            value=json.loads(analysis_path.read_bytes())
            value['timeframes']['1d']['topics']['conventional']['status']='dependency_missing'
            analysis_path.write_bytes(_encoded(value))
            return result
        with patch('app.local_updates.subprocess.run',side_effect=degraded):
            with self.assertRaisesRegex(ValueError,'backend did not actually run'):
                self.manager._calculate_technical(self.manager._read(job['update_id']),self.manager._path(job['update_id']))
        self.assertEqual(len(lab.scan(self.root)['results']),3)

    def test_missing_numerical_backend_stops_before_preview_job_or_archive(self):
        with patch.object(self.manager,'_runtime',side_effect=ValueError('required backend missing')):
            with self.assertRaises(ValueError):self.preview()
        self.assertEqual(self.manager._all(),[])
        self.assertEqual(len(lab.scan(self.root)['results']),1)

    def test_short_timeframe_warmup_stays_partial_when_real_backend_is_present(self):
        public=self.preview();job=self.manager._read(public['update_id'])
        frame_status={'value':'partial'}
        def calculate(command,**kwargs):
            request=json.loads(Path(command[8]).read_bytes());output=Path(command[10]);output.mkdir()
            (output/'report.md').write_text('# Fictional short-history result\n\nWarm-up remains insufficient.\n',encoding='utf-8')
            bars=json.loads(Path(request['input_file']).read_bytes())
            daily={'bars':bars,'metadata':{'input_sha256':hashlib.sha256(Path(request['input_file']).read_bytes()).hexdigest()},
                'topics':{'conventional':{'status':'ok','backend':'talib','backend_version':'0.6.8'}}}
            long={'bars':bars[:1],'topics':{'conventional':{'status':frame_status['value'],'backend':'talib',
                'backend_version':'0.6.8','limitations':['fewer than60 completed bars; the60-bar trend condition is missing']}}}
            (output/'analysis.json').write_bytes(_encoded({'security':SECURITY,'timeframes':{'1d':daily,'6mo':long,'1y':long}}))
            return SimpleNamespace(returncode=0)
        with patch('app.local_updates.subprocess.run',side_effect=calculate),patch.object(self.manager,'_archive') as archive:
            self.manager._calculate_technical(job,self.manager._path(public['update_id']))
            self.assertEqual(archive.call_count,1)
            saved=json.loads((archive.call_args.args[2]/'analysis.json').read_bytes())
            self.assertEqual(saved['timeframes']['6mo']['topics']['conventional']['status'],'partial')
            self.assertEqual(saved['timeframes']['1y']['topics']['conventional']['limitations'],
                ['fewer than60 completed bars; the60-bar trend condition is missing'])
            frame_status['value']='dependency_missing'
            with self.assertRaisesRegex(ValueError,'backend did not actually run'):
                self.manager._calculate_technical(job,self.manager._path(public['update_id']))
            self.assertEqual(archive.call_count,1)

    def test_runtime_version_change_requires_new_preview_without_starting_old_job(self):
        job=self.preview()
        with patch.object(self.manager,'_runtime',return_value={'probe':'fictional-test','talib':'different-version'}):
            with self.assertRaisesRegex(ValueError,'算法或配置已变化'):
                self.manager.start(SECURITY['security_id'],job['update_id'])
            newer=self.preview()
        self.assertNotEqual(newer['update_id'],job['update_id'])
        self.assertEqual(len(lab.scan(self.root)['results']),1)

    def test_transient_windows_replace_conflict_retries_without_deleting_old_state(self):
        public=self.preview();job=self.manager._read(public['update_id'])
        path=self.manager._path(public['update_id'])/'job.json';before=path.read_bytes()
        replace=os.replace;calls=[]
        def occupied(source,destination):
            calls.append((source,destination))
            if len(calls)<3:
                self.assertEqual(path.read_bytes(),before)
                error=PermissionError('Fictional Windows sharing conflict');error.winerror=32
                raise error
            return replace(source,destination)
        job['stage_label']='写入已完成'
        with patch('app.local_updates.os.replace',side_effect=occupied),patch('app.local_updates.wall_clock.sleep') as sleep:
            self.manager._save(job)
        self.assertEqual(len(calls),3)
        self.assertEqual(sleep.call_count,2)
        self.assertEqual(self.manager._read(public['update_id'])['stage_label'],'写入已完成')

    def test_exhausted_windows_replace_preserves_running_record_and_clears_active(self):
        public=self.preview();job=self.manager._read(public['update_id'])
        job['status']='running';self.manager._save(job)
        path=self.manager._path(public['update_id'])/'job.json';before=path.read_bytes()
        error=PermissionError('Fictional persistent Windows sharing conflict');error.winerror=5
        self.manager._active=job['update_id']
        with patch.object(self.manager,'_verify'),patch.object(self.manager,'_stage'), \
             patch.object(self.manager,'_existing',return_value=True), \
             patch('app.local_updates.os.replace',side_effect=error) as replace, \
             patch('app.local_updates.wall_clock.sleep') as sleep:
            with self.assertRaises(PermissionError):self.manager._run(job['update_id'])
        self.assertEqual(replace.call_count,10)
        self.assertEqual(sleep.call_count,9)
        self.assertIsNone(self.manager._active)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.manager._read(job['update_id'])['status'],'running')
        restarted=LocalUpdates(self.store);self.addCleanup(restarted.close)
        self.assertEqual(restarted.public(SECURITY['security_id'])['latest']['status'],'interrupted')
        self.assertEqual(len(lab.scan(self.root)['results']),1)


if __name__=='__main__':
    unittest.main()
