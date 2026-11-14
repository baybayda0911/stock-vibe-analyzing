from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1]/'skills/stock-technical-analysis/scripts/technical_data.py'
spec = importlib.util.spec_from_file_location('technical_data', SCRIPT)
data = importlib.util.module_from_spec(spec)
spec.loader.exec_module(data)


def bar(stamp, price=10):
    return dict(time=stamp,open=price,high=price+1,low=price-1,close=price+.5,
                volume=100,amount=1000,completed=True,available_at='2026-02-02T20:00:00+08:00')


class TechnicalDataTests(unittest.TestCase):
    def test_week_month_exclude_current_unfinished_period(self):
        rows = [bar('2026-01-30T15:30:00+08:00'),bar('2026-02-02T15:30:00+08:00',11)]
        calendar=dict(trading_dates=['2026-01-30','2026-02-02'],verified=True,source='test',
                      coverage_start='2026-01-01',coverage_end='2026-02-28')
        for frame in ('1w','1mo'):
            with self.subTest(frame=frame):
                result, meta = data.aggregate_daily(rows,frame,'2026-02-02T20:00:00+08:00',calendar)
                self.assertEqual(len(result),1)
                self.assertEqual(result[0]['close'],10.5)
                self.assertEqual(meta['excluded_groups'],1)
                self.assertEqual(meta['qualification'],'validated')

    def test_missing_expected_trading_day_is_not_filled(self):
        rows = [bar('2026-01-30T15:30:00+08:00')]
        result,meta=data.aggregate_daily(rows,'1w','2026-02-02T20:00:00+08:00',
            dict(trading_dates=['2026-01-29','2026-01-30'],verified=True,source='test',
                 coverage_start='2026-01-01',coverage_end='2026-02-28'))
        self.assertEqual(result,[])
        self.assertEqual(meta['excluded_groups'],1)

    def test_calendar_for_another_period_does_not_qualify_groups(self):
        result,meta=data.aggregate_daily([bar('2026-01-30T15:30:00+08:00')],'1w','2026-02-02T20:00:00+08:00',
            dict(trading_dates=['2025-01-03'],verified=True,source='test',coverage_start='2025-01-01',coverage_end='2025-12-31'))
        self.assertEqual(result,[])
        self.assertEqual(meta['qualification'],'diagnostic')
        self.assertEqual(meta['uncovered_calendar_groups'],1)

    def test_minutes_do_not_cross_lunch_and_sum_complete_groups(self):
        rows=[]
        for start in ('2026-02-02T09:30:00+08:00','2026-02-02T13:00:00+08:00'):
            at=datetime.fromisoformat(start)
            rows += [bar((at+timedelta(minutes=i)).isoformat(),10+i/100) for i in range(5,121,5)]
        result,meta=data.aggregate_minutes(rows,5,60,[['09:30','11:30'],['13:00','15:00']])
        self.assertEqual(len(result),4)
        self.assertEqual(result[0]['volume'],1200)
        self.assertEqual(result[1]['time'],'2026-02-02T11:30:00+08:00')
        self.assertEqual(result[2]['time'],'2026-02-02T14:00:00+08:00')
        self.assertEqual(meta['excluded_groups'],0)
        bad=rows+[bar('2026-02-02T12:00:00+08:00')]
        with self.assertRaises(ValueError):
            data.aggregate_minutes(bad,5,60,[['09:30','11:30'],['13:00','15:00']])

    def test_missing_minute_drops_group_not_fake_ohlc(self):
        rows=[bar(f'2026-02-02T09:{x}:00+08:00') for x in (35,45)]
        result,meta=data.aggregate_minutes(rows,5,15,[['09:30','11:30']])
        self.assertEqual(result,[])
        self.assertEqual(meta['qualification'],'diagnostic')

    def test_retrospective_snapshot_and_daily_end_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'bars.json').write_text(json.dumps([dict(time='2026-02-02',open=10,high=11,low=9,close=10,volume=200)]),encoding='utf-8')
            request=dict(security=dict(security_id='TEST.SZ',market='A-share',exchange='sz',code='TEST',currency='CNY'),
                input_file='bars.json',timeframe='1d',asof='2026-02-03T12:00:00+08:00',
                market_cutoff='2026-02-02T15:30:00+08:00',snapshot_available_at='2026-02-03T10:00:00+08:00',
                adjustment=dict(mode='raw',verified=False,evidence='synthetic'),source='synthetic',data_kind='synthetic',
                completion_evidence='completed synthetic',session_evidence='synthetic session',
                bar_time_semantics='period_label_date',daily_end_time='15:30:00',daily_end_evidence='source declares completion',
                timeframes=['1d','5m'])
            result=data.prepare_inputs(request,root/'request.json')
            self.assertEqual(result['1d']['bars'][0]['time'],'2026-02-02T15:30:00+08:00')
            self.assertEqual(result['1d']['bars'][0]['volume'],200)
            self.assertEqual(result['1d']['metadata']['point_in_time'],'retrospective_snapshot')
            self.assertEqual(result['1d']['metadata']['qualification'],'diagnostic')
            self.assertEqual(result['5m']['status'],'paused')

    def test_minute_request_pauses_before_reading_or_aggregating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            request=dict(input_file='missing.csv',timeframe='5m',asof='2026-02-03T12:00:00+08:00',timeframes=['5m','15m','60m'])
            with patch.object(data,'load_dataset',side_effect=AssertionError('must not read minute input')), \
                 patch.object(data,'load_close_dataset',side_effect=AssertionError('must not read minute input')), \
                 patch.object(data,'aggregate_minutes',side_effect=AssertionError('must not aggregate minutes')):
                result=data.prepare_inputs(request,root/'request.json')
            self.assertTrue(all(value['status']=='paused' and value['bars']==[] for value in result.values()))
            self.assertTrue(result['5m']['metadata']['limitations'])

    def test_verified_daily_label_time_precedes_same_day_filter_without_future_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'bars.json').write_text(json.dumps([dict(time=day,open=10,high=11,low=9,close=10)
                for day in ('2026-02-01','2026-02-02')]),encoding='utf-8')
            request=dict(security=dict(security_id='TEST.SZ',market='A-share',exchange='sz',code='TEST',currency='CNY'),
                input_file='bars.json',timeframe='1d',asof='2026-02-02T20:00:00+08:00',
                snapshot_available_at='2026-02-02T18:00:00+08:00',
                adjustment=dict(mode='raw',verified=False,evidence='synthetic'),source='synthetic',data_kind='synthetic',
                completion_evidence='synthetic complete',session_evidence='synthetic session',
                bar_time_semantics='period_label_date',daily_end_time='15:00:00',daily_end_evidence='explicit source observation time')
            rows,_=data.load_dataset(request,root/'request.json',request,'2026-02-02T15:00:00+08:00')
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[-1]['time'],'2026-02-02T15:00:00+08:00')
            conservative={k:v for k,v in request.items() if k not in ('daily_end_time','daily_end_evidence')}
            rows,_=data.load_dataset(conservative,root/'request.json',conservative,'2026-02-02T15:00:00+08:00')
            self.assertEqual(len(rows),1)
            early={**request,'asof':'2026-02-02T14:00:00+08:00','snapshot_available_at':'2026-02-02T13:00:00+08:00'}
            rows,_=data.load_dataset(early,root/'request.json',early,'2026-02-02T14:00:00+08:00')
            self.assertEqual(len(rows),1)
            with self.assertRaises(ValueError):
                data.load_dataset({**request,'daily_end_evidence':''},root/'request.json',
                                  {**request,'daily_end_evidence':''},'2026-02-02T15:00:00+08:00')

    def test_historical_price_samples_stay_readable_but_new_minute_research_is_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'sample.json').write_text(json.dumps([dict(time='2026-02-02T14:55:00+08:00',close=10)]),encoding='utf-8')
            request=dict(asof='2026-02-03T12:00:00+08:00',market_cutoff='2026-02-02T15:30:00+08:00',
                snapshot_available_at='2026-02-03T10:00:00+08:00',source='synthetic samples',
                datasets=[dict(timeframe='5m',kind='close_only',input_file='sample.json',time_semantics='nominal_observation')],
                timeframes=['5m','15m','60m'])
            rows,meta=data.load_close_dataset(request,root/'request.json',request['datasets'][0],request['market_cutoff'])
            self.assertEqual(rows[0]['close'],10)
            self.assertNotIn('open',rows[0])
            self.assertTrue(meta['price_only'])
            result=data.prepare_inputs(request,root/'request.json')
            self.assertTrue(all(value['status']=='paused' and not value['bars'] for value in result.values()))

    def test_half_year_and_year_keep_real_endpoints_and_exclude_unfinished_period(self):
        dates=['2025-06-30','2025-12-31','2026-06-30','2026-11-03']
        rows=[bar(day+'T15:00:00+08:00',10+i*2) for i,day in enumerate(dates)]
        calendar=dict(trading_dates=dates,verified=True,source='synthetic sparse calendar',
                      coverage_start='2025-01-01',coverage_end='2026-12-31')
        half,meta=data.aggregate_daily(rows,'6mo','2026-11-03T20:00:00+08:00',calendar)
        self.assertEqual([x['period_end'] for x in half],['2025-06-30','2025-12-31','2026-06-30'])
        self.assertEqual(half[-1]['time'],'2026-06-30T15:00:00+08:00')
        self.assertEqual(meta['excluded_groups'],1)
        year,meta=data.aggregate_daily(rows,'1y','2026-11-03T20:00:00+08:00',calendar)
        self.assertEqual(len(year),1)
        self.assertEqual(year[0]['period_end'],'2025-12-31')
        self.assertEqual((year[0]['open'],year[0]['high'],year[0]['low'],year[0]['close']),(10,13,9,12.5))
        self.assertEqual(year[0]['volume'],200)
        self.assertEqual(meta['excluded_groups'],1)

    def test_long_period_rejects_missing_days_missing_calendar_and_partial_coverage(self):
        rows=[bar('2025-12-31T15:00:00+08:00')]
        calendar=dict(trading_dates=['2025-07-01','2025-12-31'],verified=True,source='synthetic',
                      coverage_start='2025-01-01',coverage_end='2025-12-31')
        for frame in ('6mo','1y'):
            with self.subTest(frame=frame):
                result,meta=data.aggregate_daily(rows,frame,'2026-01-02T20:00:00+08:00',calendar)
                self.assertEqual(result,[])
                self.assertEqual(meta['excluded_groups'],1)
                result,meta=data.aggregate_daily(rows,frame,'2026-01-02T20:00:00+08:00')
                self.assertEqual(result,[])
                self.assertEqual(meta['uncovered_calendar_groups'],1)
                result,meta=data.aggregate_daily(rows,frame,'2026-01-02T20:00:00+08:00',
                                                {**calendar,'trading_dates':['2025-12-31'],'coverage_start':'2025-07-02'})
                self.assertEqual(result,[])
                self.assertEqual(meta['uncovered_calendar_groups'],1)

    def test_default_five_periods_preserve_diagnostic_source_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            dates=['2025-06-30','2025-12-31']
            (root/'bars.json').write_text(json.dumps([bar(day+'T15:00:00+08:00') for day in dates]),encoding='utf-8')
            request=dict(security=dict(security_id='TEST.SZ',market='A-share',exchange='sz',code='TEST',currency='CNY'),
                input_file='bars.json',timeframe='1d',asof='2026-02-02T20:00:00+08:00',
                market_cutoff='2026-02-02T15:00:00+08:00',snapshot_available_at='2026-02-02T18:00:00+08:00',
                adjustment=dict(mode='raw',verified=False,evidence='synthetic unknown actions'),source='synthetic',data_kind='synthetic',
                completion_evidence='synthetic complete',session_evidence='synthetic session',bar_time_semantics='bar_end_time',
                calendar=dict(trading_dates=dates,verified=True,source='synthetic sparse calendar',
                              coverage_start='2025-01-01',coverage_end='2026-12-31'))
            result=data.prepare_inputs(request,root/'request.json')
            self.assertEqual(tuple(result),('1d','1w','1mo','6mo','1y'))
            for frame in ('6mo','1y'):
                self.assertTrue(result[frame]['bars'])
                self.assertEqual(result[frame]['metadata']['qualification'],'diagnostic')
                self.assertEqual(result[frame]['metadata']['timeframe'],frame)

    def test_daily_aggregation_rejects_multiple_rows_on_one_trading_date(self):
        rows=[bar('2025-12-31T14:00:00+08:00'),bar('2025-12-31T15:00:00+08:00')]
        with self.assertRaisesRegex(ValueError,'只能有一根'):
            data.aggregate_daily(rows,'1y','2026-01-02T20:00:00+08:00')

    def test_long_period_indicators_never_bridge_missing_periods_or_hide_missing_tail(self):
        core=data.sibling('technical_core')
        for frame in ('6mo','1y'):
            if frame=='6mo':
                keys=[(2000+i//2,1+i%2) for i in range(24)]
                cutoff='2012-01-02T20:00:00+08:00'
            else:
                keys=[(1980+i,) for i in range(24)]
                cutoff='2004-01-02T20:00:00+08:00'
            dates=[data.period_end(key,frame).isoformat() for key in keys]
            calendar=dict(trading_dates=dates,verified=True,source='synthetic calendar',
                          coverage_start=f'{keys[0][0]}-01-01',coverage_end=dates[-1])
            for missing_tail in (False,True):
                with self.subTest(frame=frame,missing_tail=missing_tail):
                    selected=[day for i,day in enumerate(dates) if i!=20 and not (missing_tail and i==23)]
                    original=[bar(day+'T15:00:00+08:00',10+i) for i,day in enumerate(selected)]
                    snapshot=json.dumps(original,sort_keys=True)
                    result,meta=data.aggregate_daily(original,frame,cutoff,calendar)
                    expected=dates[21:23 if missing_tail else 24]
                    self.assertEqual([row['period_end'] for row in result],expected)
                    self.assertIsNone(core.sma([row['close'] for row in result],20)[-1])
                    self.assertEqual(meta['excluded_discontinuous_groups'],20)
                    self.assertEqual(meta['excluded_discontinuous_periods'],dates[:20])
                    self.assertEqual(meta['unfinished_periods'],[])
                    self.assertEqual(meta['latest_expected_completed_period_end'],dates[-1])
                    self.assertEqual(meta['selected_latest_period_end'],expected[-1])
                    self.assertEqual(meta['data_asof'],expected[-1]+'T15:00:00+08:00')
                    self.assertEqual(meta['missing_trailing_periods'],dates[-1:] if missing_tail else [])
                    self.assertEqual(meta['latest_completed_period_present'],not missing_tail)
                    self.assertEqual(meta['qualification'],'diagnostic' if missing_tail else 'validated')
                    self.assertEqual(json.dumps(original,sort_keys=True),snapshot)


if __name__=='__main__':
    unittest.main()
