import importlib.util
import hashlib
import json
from datetime import date,timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'skills/stock-technical-analysis/scripts/technical_analysis.py'
spec=importlib.util.spec_from_file_location('technical_analysis',SCRIPT)
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)


class TechnicalAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        bars=[dict(time=(date(2026,1,1)+timedelta(days=i)).isoformat(),open=100+i,high=102+i,
                   low=99+i,close=101+i,volume=1000+i,amount=100000+i) for i in range(150)]
        (self.root/'bars.json').write_text(json.dumps(bars),encoding='utf-8')
        self.request=dict(security=dict(security_id='ZZ-TEST-DEMO',company='Fictional',market='A-share',
            exchange='sz',code='DEMO',currency='TST',share_class='ordinary'),
            input_file='bars.json',timeframe='1d',timeframes=['1d'],asof='2026-11-02T23:59:59+08:00',
            market_cutoff='2026-11-02T15:30:00+08:00',information_cutoff='2026-11-02T23:59:59+08:00',
            snapshot_available_at='2026-11-02T20:00:00+08:00',source='synthetic',data_kind='synthetic',
            adjustment=dict(mode='raw',verified=False,evidence='synthetic'),bar_time_semantics='period_label_date',
            completion_evidence='synthetic complete bars',session_evidence='synthetic dates',
            max_stale_calendar_days=365,validation=dict(price_basis=True,session=True,completion=True,source_identity=True,evidence='synthetic declarations'))
        self.path=self.root/'request.json';self.save()

    def save(self):
        self.path.write_text(json.dumps(self.request),encoding='utf-8')

    def industry_fixture(self):
        """Different price and return chains make accidental cross-basis reuse observable."""
        rows=json.loads((self.root/'bars.json').read_text(encoding='utf-8'))
        dates=[(date(2026,11,2)-timedelta(days=len(rows)-1-i)).isoformat()+'T15:00:00+08:00'
               for i in range(len(rows))]
        for row,stamp in zip(rows,dates): row['time']=stamp
        (self.root/'bars.json').write_text(json.dumps(rows),encoding='utf-8')
        self.request['bar_time_semantics']='bar_end_time'
        self.request['background_comparison_basis']='synthetic price return'
        self.request['market']=dict(id='SYNTHETIC-MARKET',label='虚构市场',currency='TST',
            comparable=True,comparison_basis='synthetic price return',
            snapshot_available_at=self.request['snapshot_available_at'],
            bars=[dict(time=stamp,close=100+.5*i) for i,stamp in enumerate(dates)])
        bundle=dict(metadata=dict(security_id=self.request['security']['security_id'],
            market_cutoff=self.request['market_cutoff'],snapshot_available_at=self.request['snapshot_available_at']),
            reference=dict(id='SYNTHETIC-INDUSTRY',currency='TST',comparable=True,
                comparison_basis='synthetic cash-inclusive return chain',available_at=self.request['snapshot_available_at'],
                bars=[dict(time=stamp,close=100*1.001**i) for i,stamp in enumerate(dates)]),
            stock_bars=[dict(time=stamp,close=100*1.002**i) for i,stamp in enumerate(dates)],
            classification={'2':'虚构通信设备'},selected_level=2,
            coverage=dict(input_securities=4,days=len(dates)),
            breadth=[dict(date='2026-11-02',expected_constituents=4,valid_returns=4,
                ma20_eligible=3,above_ma20=2,above_ma20_share=2/3)],
            limitations=['Synthetic present-member proxy; not an official industry index'])
        industry=self.root/'industry-context.json'
        industry.write_text(json.dumps(bundle),encoding='utf-8')
        self.request['industry_context_file']=industry.name
        self.save()
        return bundle,industry

    def test_chart_times_label_local_a_share_clock_without_changing_instant(self):
        bars=[dict(time='2026-11-02T01:35:00+00:00')]
        self.assertEqual(app.chart_times(bars,{'market':'A-share'}),['2026-11-02T09:35:00+08:00'])
        self.assertEqual(app.chart_times(bars,{'market':'other'}),['2026-11-02T01:35:00+00:00'])

    def test_report_explains_strong_and_weak_price_paths_without_signal_votes(self):
        strong=app.calculate(self.request,self.path)
        text=app.report_markdown(strong)
        self.assertIn('偏强：价格高于20根均线',text)
        self.assertIn('图表解读',text)
        self.assertIn('本周期小结',text)
        self.assertIn('最终技术判断',text)
        self.assertNotIn('```json',text)
        rows=json.loads((self.root/'bars.json').read_text())
        for i,row in enumerate(rows):
            row.update(open=300-i,high=302-i,low=299-i,close=301-i)
        (self.root/'bars.json').write_text(json.dumps(rows),encoding='utf-8')
        weak=app.calculate(self.request,self.path)
        text=app.report_markdown(weak)
        self.assertIn('偏弱：价格低于20根均线',text)
        self.assertIn('超卖不能单独确认见底',text)
        strong['timeframes']['1w']=weak['timeframes']['1d']
        self.assertIn('周期分歧',app.report_markdown(strong))

    def test_paused_minutes_do_not_read_even_missing_files_or_run_core(self):
        self.request.update(datasets=[dict(timeframe='5m',kind='close_only',input_file='samples.json',
            time_semantics='synthetic nominal observations')],timeframes=['5m'])
        data=app.sibling('technical_data');core=app.sibling('technical_core')
        self.assertFalse((self.root/'samples.json').exists())
        with patch.object(data,'load_close_dataset',side_effect=AssertionError('paused minutes must not be read')), \
             patch.object(data,'load_dataset',side_effect=AssertionError('paused minutes must not be read')), \
             patch.object(data,'aggregate_minutes',side_effect=AssertionError('paused minutes must not be aggregated')), \
             patch.object(core,'analyze_close_prices',side_effect=AssertionError('paused minutes must not run')), \
             patch.object(core,'analyze_technical',side_effect=AssertionError('paused minutes must not run')):
            doc=app.calculate(self.request,self.path)
        self.assertEqual(doc['status'],'paused')
        minute=doc['timeframes']['5m']
        self.assertEqual(minute['status'],'paused')
        self.assertEqual(minute['metadata']['qualification'],'paused')
        self.assertEqual(minute['bars'],[])
        self.assertEqual(minute['series'],{})
        self.assertTrue(all(value['status']=='not_run' for value in minute['topics'].values()))
        text=app.report_markdown(doc)
        self.assertIn('未提供日线，不能用分钟状态替代波段判断',text)
        self.assertIn('分钟级分析已暂停',text)
        self.assertNotIn('日线研究输入而言，偏强',text)

    def test_old_active_harmonic_is_dated_and_never_current_confirmation(self):
        value=dict(topics={'harmonic':dict(analysis=dict(patterns=[dict(pattern='Butterfly',scenario_status='active',
            points={'D':{'time':'2015-09-02T07:00:00+00:00'}},confirmed_at='2015-09-11T07:00:00+00:00')],
            forming=[],previews=[]))})
        text=app.harmonic_reading(value)
        self.assertIn('D发生日2015-09-02',text)
        self.assertIn('历史旧形态不会自动变成近期买卖信号',text)

    def test_previous_snapshot_checks_identity_dates_and_price_basis_before_changes(self):
        old=app.calculate(self.request,self.path)
        old.update(generated_at='2026-11-02T08:00:00+08:00',asof='2026-11-02T08:00:00+08:00',
                   market_cutoff='2026-11-01T15:00:00+08:00',information_cutoff='2026-11-01T15:00:00+08:00')
        old['timeframes']['1d']['metadata']['source_family']='same-ancestry'
        current=json.loads(json.dumps(old))
        current.update(asof=self.request['asof'],market_cutoff=self.request['market_cutoff'],information_cutoff=self.request['information_cutoff'])
        current['timeframes']['1d']['bars'][-1]['close']+=1
        previous=self.root/'previous.json';previous.write_text(json.dumps(old),encoding='utf-8')
        config=dict(input_file=str(previous),result_id='T0',price_basis_compatible=True,comparison_evidence='synthetic no new action or anchor change')
        result=app.previous_technical(config,current,self.path)
        self.assertEqual(result['timeframes']['1d']['changes']['close']['change'],1)
        result=app.previous_technical({**config,'price_basis_compatible':False},current,self.path)
        self.assertEqual(result['timeframes']['1d']['status'],'incomparable')
        current['timeframes']['1d']['metadata']['adjustment']['mode']='qfq'
        result=app.previous_technical(config,current,self.path)
        self.assertEqual(result['timeframes']['1d']['status'],'incomparable')
        current['security']['currency']='OTHER'
        result=app.previous_technical(config,current,self.path)
        self.assertEqual(result['status'],'historical_only')
        self.assertIn('currency',result['reason'])
        current['security']['currency']='TST'
        old['generated_at']='2027-01-01T00:00:00+08:00';previous.write_text(json.dumps(old),encoding='utf-8')
        self.assertIn('generated_at',app.previous_technical(config,current,self.path)['reason'])

    def test_archive_uses_information_date_and_retains_actual_generation(self):
        doc=app.calculate(self.request,self.path)
        self.request['research_lab']=dict(archive=True,result_id='T-NEW',root='unused',tool='unused')
        out=self.root/'output';out.mkdir();(out/'report.md').write_text('synthetic',encoding='utf-8')
        with patch.object(app,'lab_call',return_value={'status':'ok'}):
            app.archive_result(self.request,doc,out)
        archived=app.read_json(out/'archive-request.json')
        self.assertEqual(archived['research_asof'],doc['information_cutoff'])
        self.assertEqual(archived['generated_at'],doc['generated_at'])
        self.assertEqual(archived['module_version'],'0.4.1')

    def test_narrative_keeps_histogram_improvement_distinct_from_dif_and_rsi(self):
        doc=app.calculate(self.request,self.path)
        daily=doc['timeframes']['1d']
        daily['topics']['conventional']['latest'].update(sma20=260,sma60=270,macd_hist=-2,macd=-12,macd_signal=-10,rsi14=25)
        for key,values in dict(sma20=[261,260],macd_hist=[-3,-2],macd=[-11,-12],rsi14=[26,25]).items():
            daily['series'][key][-2:]=values
        text=app.report_markdown(doc)
        self.assertIn('负柱收窄，支持下行动量减弱；仍是负柱',text)
        self.assertIn('日线MACD柱-3.00→-2.00',text)
        self.assertIn('DIF较前根下降',text)
        self.assertIn('RSI较前根下降',text)
        self.assertIn('价格重回动态SMA20之上，且当根SMA20不低于前一完整根',text)
        self.assertNotIn('若此前为强势',text)
        self.assertNotIn('](archive-status.json)',text)

    def test_background_explains_full_window_lead_and_recent_underperformance(self):
        doc=app.calculate(self.request,self.path)
        dates=[(date(2026,1,1)+timedelta(days=i)).isoformat()+'T15:00:00+08:00' for i in range(24)]
        stock=[100,130,170,200]+[200-2.5*i for i in range(1,21)]
        baseline=[100,110,115,120]+[120-.5*i for i in range(1,21)]
        stock_return,reference_return=stock[-1]/stock[-21]-1,baseline[-1]/baseline[-21]-1
        comparison=dict(id='SYNTHETIC-MARKET',reference='market',status='ok',common_dates=dates,
            stock_index=stock,reference_index=baseline,windows={
                '20':dict(status='ok',stock_return=stock_return,reference_return=reference_return,
                          excess_return=stock_return-reference_return,start=dates[-21],end=dates[-1])})
        doc['background']['comparisons']=[comparison]
        text=app.report_markdown(doc)
        self.assertIn('图示全窗24点，2026-01-01—2026-01-24',text)
        self.assertIn('|50.00%|10.00%|40.00个百分点|',text)
        self.assertIn('全窗仍累计领先，但所列近窗均跑输',text)
        self.assertIn('近20个交易间隔（21点）',text)
        self.assertIn('SYNTHETIC-MARKET：图示全窗24点超额40.00个百分点',text)

    def test_mixed_relations_and_turtle_exit_observation_are_specific(self):
        doc=app.calculate(self.request,self.path)
        daily=doc['timeframes']['1d']
        daily['topics']['conventional']['latest'].update(sma20=260,sma60=240)
        daily['topics']['turtle']['latest'].update(long_exit10=True,long_exit20=True)
        text=app.report_markdown(doc)
        self.assertIn('趋势分歧：价格低于20根均线，20根均线高于60根均线',text)
        self.assertIn('退出观察条件分别为已满足/已满足',text)
        self.assertIn('尚未同时满足时继续保留分歧',text)
        unknown=app.next_conditions({})
        self.assertIn('先补足同一合格口径',unknown[0])

    def test_file_reference_fingerprint_uses_actual_bytes_not_declared_hash(self):
        path=self.root/'reference.json'
        path.write_text(json.dumps([dict(time='2026-01-01T15:00:00+08:00',close=100)]),encoding='utf-8')
        reference=app.load_reference(dict(id='SYNTHETIC',input_file=path.name,source_sha256='stale-declared-hash'),
            self.path,self.request['asof'],self.request['market_cutoff'])
        self.assertEqual(reference['declared_source_sha256'],'stale-declared-hash')
        self.assertEqual(reference['source_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())

    def test_personal_card_integration_maps_data_gate_without_skipping_rule_or_user_checks(self):
        self.request['security']['board']='synthetic-board'
        self.request['adjustment']=dict(mode='qfq',verified=True,evidence='synthetic fixed mapping only')
        self.request['scenario']=dict(action='buy',first_known_at='2026-11-01T12:00:00+08:00',
            trigger='synthetic close over threshold',confirmation='synthetic complete bar',invalidation='synthetic below boundary',
            observation_window='synthetic five sessions',trigger_price='20.002',invalidation_price='19.992',price_basis='qfq',
            price_mapping=dict(validated=True,evidence='synthetic factor two',available_at='2026-11-01T12:00:00+08:00',
                               valid_until='2026-11-03T12:00:00+08:00',adjusted_per_raw='2'))
        self.request['rules']=dict(validated=True,evidence='synthetic rulebook',rule_id='SYNTHETIC',market='A-share',exchange='sz',
            board='synthetic-board',available_at='2026-01-01T00:00:00+08:00',effective_from='2026-01-01T00:00:00+08:00',
            effective_until='2026-12-31T23:59:59+08:00',tick_size='0.01',minimum_quantity='100',quantity_step='100',
            has_price_limit=True,lower_price_limit='9',upper_price_limit='11',state_asof='2026-11-02T23:50:00+08:00',
            state_valid_until='2026-11-03T00:10:00+08:00',session_open=True,suspended=False,t_plus_one=True)
        self.request['user_context']=dict(available_at='2026-11-02T20:00:00+08:00',investment_horizon='synthetic week',
            position_fraction='0',risk_budget='5',requested_quantity='100',holdings_quantity='0',available_cash='1100',
            fee_estimate='1',slippage_buffer='0.01')
        self.save()
        with patch.object(app,'render_background'),patch.object(app,'render_frame'):
            app.run(self.path,self.root/'personal-card')
        doc=app.read_json(self.root/'personal-card/analysis.json');card=doc['execution']
        text=(self.root/'personal-card/report.md').read_text(encoding='utf-8')
        self.assertEqual(doc['timeframes']['1d']['metadata']['qualification'],'validated')
        self.assertEqual(card['data_qualification'],'validated')
        self.assertEqual(card['qualification'],'eligible')
        self.assertEqual(card['status'],'personal_conditional',card)
        self.assertEqual(card['risk_amount_under_assumptions'],'4.00')
        self.assertTrue(all(card['checks'].values()))
        self.assertFalse(card['orders_created']);self.assertFalse(card['can_submit_order'])
        for actual in ('|用户指定数量|100|','|分析触发／失效|20.002 / 19.992|',
                       '|原价触发／失效|10.001 / 9.996|','|取整触发／失效|10.01 / 9.99|',
                       '|用户风险预算|5|','|费用假设|1|','|每单位滑点|0.01|',
                       '|假设风险金额|4.00|','|制度与生效时间|通过|'):
            self.assertIn(actual,text)
        self.assertIn('](details.html)',text)
        audit=app.report_markdown(doc)
        for actual in ('|分析价（qfq）|20.002|19.992|','|未取整原价|10.001|9.996|',
                       '|按报价步长取整|10.01|9.99|','|用户风险预算|5|','|整笔费用假设|1|',
                       '|每单位滑点假设|0.01|','假设下风险金额：4.00','|规则身份、公开及生效时段|通过|'):
            self.assertIn(actual,audit)
        self.request['rules']['suspended']=True
        blocked=app.calculate(self.request,self.path)
        self.assertEqual(blocked['execution']['status'],'blocked')
        self.assertIn('交易会话、停牌或状态有效期未知或受限',app.report_markdown(blocked))
        self.save()
        with patch.object(app,'render_background'),patch.object(app,'render_frame'):
            app.run(self.path,self.root/'blocked-card')
        restricted=(self.root/'blocked-card/report.md').read_text(encoding='utf-8')
        self.assertIn('|当前状态|受限，不能当作已可执行|',restricted)
        self.assertIn('|会话与停牌状态|未通过／待核|',restricted)
        self.assertIn('交易会话、停牌或状态有效期未知或受限',restricted)
        self.assertIn('|用户指定数量|100|',restricted)
        self.request['rules']['suspended']=False
        self.request['validation']['price_basis']=False
        diagnostic=app.calculate(self.request,self.path)['execution']
        self.assertEqual(diagnostic['data_qualification'],'diagnostic')
        self.assertEqual(diagnostic['status'],'blocked')
        self.request['validation']['price_basis']=True
        self.request['user_context'].update(available_at='2027-01-01T00:00:00+08:00',requested_quantity='987654')
        future=app.calculate(self.request,self.path)
        self.assertEqual(future['execution']['status'],'blocked')
        self.assertNotIn('personal_inputs',future['execution'])
        self.assertNotIn('987654',app.report_markdown(future))
        self.request['scenario']={'action':'observe'}
        observe=app.calculate(self.request,self.path)
        self.assertEqual(observe['execution']['status'],'not_requested')
        self.assertNotIn('personal_inputs',observe['execution'])
        self.assertNotIn('### 数量、资金及风险核对',app.report_markdown(observe))
        self.request['scenario']={'action':'buy'}
        self.request.pop('user_context');self.request.pop('rules')
        incomplete=app.calculate(self.request,self.path)
        self.assertEqual(incomplete['execution']['status'],'non_personal')
        self.assertIn('未提供个人情境，只给非个性化条件',app.report_markdown(incomplete))

    def test_invalid_review_is_reported_as_failure_instead_of_missing_old_items(self):
        self.request['review_records']=[dict(item_id='OLD-1',condition_status='not_met')]
        doc=app.calculate(self.request,self.path)
        self.assertEqual(doc['review']['status'],'invalid_input')
        text=app.report_markdown(doc)
        self.assertIn('复盘输入校验失败',text)
        self.assertIn('Unknown condition state',text)
        self.assertNotIn('|未提供原冻结项|',text)

    def test_close_pipeline_keeps_all_topic_statuses_and_does_not_ask_position(self):
        doc=app.calculate(self.request,self.path)
        self.assertEqual(set(doc['timeframes']['1d']['topics']),set(app.LABELS))
        self.assertEqual(doc['execution']['status'],'not_requested')
        self.assertEqual(doc['previous_context']['status'],'unavailable')
        self.assertFalse(doc['review']['pnl_computed'])
        self.assertIn('data_asof',doc['timeframes']['1d']['metadata'])
        self.assertNotIn('generated_at',doc['timeframes']['1d']['metadata'])
        self.assertIn('input_prepared_at',doc['timeframes']['1d']['metadata'])
        self.assertTrue(any(row['topic']=='harmonic' and row['status']!='unavailable' for row in doc['evidence']['rows']))

    def test_fundamental_date_identity_and_expiry_are_not_refreshed(self):
        base=dict(result_id='F1',security_id='ZZ-TEST-DEMO',currency='TST',share_class='ordinary',
            generated_at='2026-11-01T18:00:00+08:00',research_asof='2026-11-01T17:00:00+08:00',
            data_asof='2026-10-30T15:30:00+08:00',valid_until='2026-11-30T18:00:00+08:00',current_use='eligible')
        refs=app.qualified_fundamentals([base],self.request['security'],self.request['asof'],self.request['information_cutoff'])
        self.assertEqual(refs[0]['reference_status'],'eligible')
        for change in (dict(currency='USD'),dict(generated_at='2026-11-03T18:00:00+08:00'),dict(valid_until='2026-10-30T18:00:00+08:00')):
            with self.subTest(change=change):
                ref=app.qualified_fundamentals([{**base,**change}],self.request['security'],self.request['asof'],self.request['information_cutoff'])[0]
                self.assertEqual(ref['reference_status'],'historical_only')

    def test_context_only_inherits_eligible_items_with_original_evidence_identity(self):
        result=dict(result_id='F1',module='fundamental',security=self.request['security'],
            generated_at='2026-11-01T18:00:00+08:00',research_asof='2026-11-01T17:00:00+08:00',
            data_asof='2026-10-30T15:30:00+08:00',evidence=[dict(evidence_id='E1',source='synthetic filing',public_at='2026-10-30')],
            item_states=[dict(item_id='I1',text='Frozen watch item',current_eligible=True,evidence_ids=['E1'],valid_until='2026-11-30T18:00:00+08:00'),
                         dict(item_id='I2',text='Expired claim',current_eligible=False,evidence_ids=['E1'])])
        refs=app.context_fundamentals(dict(result=dict(results=[result],roles=dict(schema_version='stock-research-roles/1',
            resolver_version='stock-research-role-resolver/1',classified=[dict(result_id='F1',content_role='full_research')]))))
        self.assertEqual(refs[0]['claims'],['Frozen watch item'])
        self.assertEqual(refs[0]['facts'][0]['source_result_id'],'F1')
        self.assertEqual(refs[0]['facts'][0]['public_time_precision'],'date_upper_bound')

    def test_fundamental_fact_public_after_information_cutoff_is_not_inherited(self):
        ref=dict(result_id='F-LATE',security_id='ZZ-TEST-DEMO',currency='TST',share_class='ordinary',
            generated_at='2026-11-02T17:00:00+08:00',research_asof='2026-11-01T17:00:00+08:00',
            data_asof='2026-11-01T17:00:00+08:00',current_use='eligible',claims=['future claim 987654'],
            facts=[dict(fact='future claim',value=987654,evidence_id='E1',public_at='2026-11-02T16:00:00+08:00')])
        result=app.qualified_fundamentals([ref],self.request['security'],'2026-11-03T08:00:00+08:00',
                                         '2026-11-01T18:00:00+08:00')[0]
        self.assertEqual(result['reference_status'],'historical_only')
        self.assertEqual(result['facts'],[])
        self.assertEqual(result['claims'],[])
        self.assertNotIn('987654',json.dumps(result))

    def test_failed_plot_is_clean_and_success_never_overwrites(self):
        output=self.root/'new'
        with patch.object(app,'render_background',side_effect=RuntimeError('render failed')):
            with self.assertRaises(RuntimeError): app.run(self.path,output)
        self.assertFalse(output.exists());self.assertFalse(list(self.root.glob('.technical-*')))
        with patch.object(app,'render_background'),patch.object(app,'render_frame'):
            result=app.run(self.path,output)
        self.assertTrue((output/'analysis.json').is_file())
        self.assertEqual(result['archive']['saved'],False)
        with self.assertRaises(ValueError): app.run(self.path,output)

    def test_invalid_json_numeric_and_future_information_are_rejected(self):
        self.path.write_text('{"x":NaN}',encoding='utf-8')
        with self.assertRaises(ValueError): app.read_json(self.path)
        self.request['information_cutoff']='2027-01-01T00:00:00+08:00'
        with self.assertRaises(ValueError): app.calculate(self.request,self.path)

    def test_currency_mismatch_blocks_benchmark_without_blocking_stock(self):
        bars=[dict(time='2026-01-01T23:59:59.999999+08:00',close=100),
              dict(time='2026-01-02T23:59:59.999999+08:00',close=101)]
        self.request.update(background_comparison_basis='nominal prices',market=dict(id='USD-INDEX',currency='USD',
            comparable=True,comparison_basis='nominal prices',bars=bars,snapshot_available_at='2026-11-02T20:00:00+08:00'))
        result=app.calculate(self.request,self.path)
        self.assertEqual(result['background']['comparisons'][0]['status'],'incomparable')
        self.assertTrue(result['timeframes']['1d']['bars'])

    def test_local_industry_context_is_saved_as_weak_proxy_with_coverage_and_actual_source_hash(self):
        bundle,industry=self.industry_fixture()
        frozen=industry.read_bytes()
        with patch.object(app,'render_background'),patch.object(app,'render_frame'):
            app.run(self.path,self.root/'industry-report')
        doc=app.read_json(self.root/'industry-report/analysis.json')
        self.assertEqual(doc['industry_context']['status'],'ok')
        self.assertEqual(doc['industry_context']['input_sha256'],hashlib.sha256(frozen).hexdigest())
        self.assertEqual(industry.read_bytes(),frozen)
        self.assertEqual(app.read_json(self.root/'industry-report/industry-context.json'),bundle)
        comparison=next(row for row in doc['background']['comparisons'] if row['reference']=='industry')
        self.assertEqual(comparison['status'],'ok')
        self.assertEqual(comparison['qualification'],'weak_proxy')
        self.assertTrue(comparison['weak_proxy'])
        self.assertIn('自建等权',comparison['label'])
        self.assertEqual(comparison['coverage'],bundle['coverage'])
        self.assertEqual(comparison['latest_breadth']['valid_returns'],4)
        self.assertEqual(comparison['latest_breadth']['ma20_eligible'],3)
        text=(self.root/'industry-report/report.md').read_text(encoding='utf-8')
        self.assertIn('非官方申万指数',text)
        self.assertIn('2/3（66.7%）',text)
        self.assertNotIn('2/4（',text)

    def test_future_breadth_row_does_not_replace_comparison_end_date_counts(self):
        bundle,industry=self.industry_fixture()
        expected=dict(bundle['breadth'][-1])
        bundle['breadth'].append(dict(date='2026-11-03',expected_constituents=987654,
            valid_returns=987654,ma20_eligible=987653,above_ma20=987653,above_ma20_share=1))
        industry.write_text(json.dumps(bundle),encoding='utf-8')
        doc=app.calculate(self.request,self.path)
        local=next(row for row in doc['background']['comparisons'] if row['reference']=='industry')
        self.assertEqual(local['status'],'ok')
        self.assertEqual(local['common_dates'][-1][:10],'2026-11-02')
        self.assertEqual(local['latest_breadth'],expected)
        self.assertEqual(doc['industry_context']['latest_breadth'],expected)
        self.assertEqual(local['latest_breadth']['valid_returns'],4)
        self.assertEqual(local['latest_breadth']['ma20_eligible'],3)
        self.assertEqual(local['latest_breadth']['above_ma20'],2)
        self.assertNotIn('987654',json.dumps(doc))

    def test_bad_local_industry_identity_future_snapshot_or_currency_only_removes_that_layer(self):
        bundle,industry=self.industry_fixture()
        baseline=app.calculate(self.request,self.path)
        daily=baseline['timeframes']['1d']['bars']
        market=next(row for row in baseline['background']['comparisons'] if row['reference']=='market')
        cases=[('security',lambda value:value['metadata'].update(security_id='ZZ-WRONG-SECURITY'),'身份'),
               ('future',lambda value:value['metadata'].update(snapshot_available_at='2027-01-01T00:00:00+08:00'),'可知截止'),
               ('currency',lambda value:value['reference'].update(currency='OTHER'),'币种')]
        for name,change,reason in cases:
            with self.subTest(case=name):
                invalid=json.loads(json.dumps(bundle));change(invalid)
                industry.write_text(json.dumps(invalid),encoding='utf-8')
                doc=app.calculate(self.request,self.path)
                self.assertEqual(doc['industry_context']['status'],'unavailable')
                self.assertIn(reason,doc['industry_context']['reason'])
                self.assertEqual(doc['timeframes']['1d']['bars'],daily)
                self.assertEqual(next(row for row in doc['background']['comparisons'] if row['reference']=='market'),market)
                self.assertFalse(any(row.get('weak_proxy') for row in doc['background']['comparisons']))
                self.assertFalse(any(row.get('source')=='SYNTHETIC-INDUSTRY' for row in doc['background']['facts']))
                self.assertTrue(any('本地行业组合未纳入' in item for item in doc['background']['limitations']))

    def test_market_price_and_industry_return_chains_keep_separate_matching_stock_bases(self):
        bundle,industry=self.industry_fixture()
        doc=app.calculate(self.request,self.path)
        market=next(row for row in doc['background']['comparisons'] if row['reference']=='market')
        local=next(row for row in doc['background']['comparisons'] if row['reference']=='industry')
        self.assertEqual((market['status'],local['status']),('ok','ok'))
        self.assertNotEqual(self.request['background_comparison_basis'],local['comparison_basis'])
        self.assertEqual(local['comparison_basis'],bundle['reference']['comparison_basis'])
        self.assertAlmostEqual(market['windows']['20']['stock_return'],250/230-1)
        self.assertAlmostEqual(market['windows']['20']['reference_return'],174.5/164.5-1)
        self.assertAlmostEqual(local['windows']['20']['stock_return'],1.002**20-1)
        self.assertAlmostEqual(local['windows']['20']['reference_return'],1.001**20-1)
        self.assertNotAlmostEqual(market['windows']['20']['stock_return'],local['windows']['20']['stock_return'])
        self.assertEqual(market['relative_index'][0],100)
        self.assertEqual(local['relative_index'][0],100)
        self.assertAlmostEqual(local['relative_index'][-1],(1.002/1.001)**149*100)
        # An unmatched market basis must not contaminate or disable the independent industry layer.
        self.request['market']['comparison_basis']='unmatched market convention'
        mixed=app.calculate(self.request,self.path)
        self.assertEqual(next(row for row in mixed['background']['comparisons'] if row['reference']=='market')['status'],'incomparable')
        retained=next(row for row in mixed['background']['comparisons'] if row['reference']=='industry')
        self.assertEqual(retained['status'],'ok')
        self.assertEqual(retained['relative_index'],local['relative_index'])

    def test_bad_background_and_aggregation_do_not_block_qualified_daily(self):
        self.request.update(background_input=dict(timeframe='1d',input_file='missing.csv'),
                            aggregation_input=dict(timeframe='1d',input_file='missing.csv'),timeframes=['1d','1w'])
        result=app.calculate(self.request,self.path)
        self.assertTrue(result['timeframes']['1d']['bars'])
        self.assertEqual(result['timeframes']['1w']['status'],'unavailable')
        self.assertTrue(any('背景来源错误' in v for v in result['background']['limitations']))

    def test_compare_and_screen_require_explicit_pool_and_explain_exclusions(self):
        second={**self.request,'security':{**self.request['security'],'security_id':'ZZ-TEST-OTHER','code':'OTHER'}}
        second_path=self.root/'other.json';second_path.write_text(json.dumps(second),encoding='utf-8')
        pool=self.root/'pool.json'
        request=dict(stocks=[str(self.path),str(second_path)],pool_asof=self.request['asof'],pool_source='explicit synthetic pool',
            conditions=[dict(field='close',op='gt',right_field='sma20')])
        pool.write_text(json.dumps(request),encoding='utf-8')
        app.pool_run(pool,self.root/'screen','screen')
        result=app.read_json(self.root/'screen/comparison.json')
        self.assertEqual(len(result['records']),2)
        request['conditions']=[dict(field='not_a_metric',op='gt',value=0)]
        pool.write_text(json.dumps(request),encoding='utf-8')
        with self.assertRaises(ValueError): app.pool_run(pool,self.root/'bad','screen')
        request['stocks']=[str(self.path)];pool.write_text(json.dumps(request),encoding='utf-8')
        with self.assertRaises(ValueError): app.pool_run(pool,self.root/'one','compare')

    def test_real_lab_tool_can_be_read_without_installing_adjacent_skills(self):
        root=self.root/'lab';tool=ROOT/'skills/stock-research-lab/scripts/research_lab.py'
        subprocess.run([sys.executable,'-X','utf8','-B',str(tool),'init',str(root)],check=True,capture_output=True)
        result=app.lab_call(dict(root=str(root),tool=str(tool)),'context',security='ZZ-TEST-DEMO',asof=self.request['asof'])
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['result']['results'],[])


if __name__=='__main__': unittest.main()
