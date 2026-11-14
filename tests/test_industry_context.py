from datetime import date,timedelta
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT=Path(__file__).resolve().parents[1]/'skills/stock-technical-analysis/scripts/industry_context.py'
spec=importlib.util.spec_from_file_location('industry_context',SCRIPT)
industry=importlib.util.module_from_spec(spec);spec.loader.exec_module(industry)


def row(day,close=100,reference=100,sw2='A',volume=100):
    return dict(date=day,close=close,prev_close=reference,volume=volume,amount=1000,
                sw1='L1',sw2=sw2,sw3='L3')


class IndustryContextTests(unittest.TestCase):
    def calculate(self,stocks,days,**changes):
        args=dict(target_rows=stocks['TARGET'],target_code='TARGET',level=2,start_date=days[1],
            market_cutoff=days[-1]+'T15:00:00+08:00',asof=days[-1]+'T20:00:00+08:00',
            snapshot_available_at=days[-1]+'T18:00:00+08:00',trading_dates=days,date_label_evidence='synthetic daily observation')
        args.update(changes)
        return industry.compute_context(stocks.items(),**args)

    def test_equal_returns_use_prior_membership_and_never_market_value_growth(self):
        days=['2026-01-01','2026-01-02','2026-01-03']
        stocks={'TARGET':[row(days[0]),row(days[1],120,100),row(days[2],144,120)],
                'PEER':[row(days[0]),row(days[1],90,100),row(days[2],81,90)]}
        result=self.calculate(stocks,days)
        self.assertAlmostEqual(result['breadth'][0]['daily_return'],.05)
        self.assertAlmostEqual(result['reference']['bars'][-1]['close'],105)
        self.assertFalse(result['reference']['official_index'])
        self.assertTrue(result['reference']['proxy'])
        self.assertFalse(result['reference']['classification_known'])
        self.assertEqual(result['reference']['comparison_basis'],result['metadata']['comparison_basis'])

    def test_classification_changes_only_affect_the_following_observation(self):
        days=['2026-01-01','2026-01-02','2026-01-03']
        stocks={'TARGET':[row(day) for day in days],
                'LEAVER':[row(days[0]),row(days[1],120,100,sw2='B'),row(days[2],180,120,sw2='B')],
                'JOINER':[row(days[0],sw2='B'),row(days[1],80,100),row(days[2],88,80)]}
        result=self.calculate(stocks,days)
        self.assertEqual([r['expected_constituents'] for r in result['breadth']],[2,2])
        self.assertAlmostEqual(result['breadth'][0]['daily_return'],.1)
        self.assertAlmostEqual(result['breadth'][1]['daily_return'],.05)

    def test_missing_return_is_not_zero_filled_and_breaks_an_invalid_chain(self):
        days=['2026-01-01','2026-01-02','2026-01-03','2026-01-04']
        stocks={'TARGET':[row(day) for day in days],
                'PEER':[row(days[0]),row(days[1]),row(days[2],volume=0),row(days[3])]}
        result=self.calculate(stocks,days)
        self.assertIsNone(result['breadth'][1]['daily_return'])
        self.assertEqual(result['breadth'][1]['missing_returns'],1)
        self.assertEqual(result['coverage']['chain_resets'],1)
        self.assertEqual(len(result['reference']['bars']),1)
        self.assertEqual(result['reference']['bars'][0]['time'][:10],days[-1])

    def test_ma20_denominator_uses_only_warmed_observations(self):
        days=[(date(2026,1,1)+timedelta(days=i)).isoformat() for i in range(21)]
        stocks={'TARGET':[row(day) for day in days],
                'PEER':[row(day,100*1.01**i,100*1.01**(i-1)) for i,day in enumerate(days)]}
        result=self.calculate(stocks,days)
        self.assertEqual(result['breadth'][0]['ma20_eligible'],0)
        self.assertEqual(result['breadth'][-1]['ma20_eligible'],2)
        self.assertAlmostEqual(result['breadth'][-1]['above_ma20_share'],.5)

    def test_later_snapshot_or_unproven_daily_labels_do_not_gain_qualification(self):
        days=['2026-01-01','2026-01-02'];stocks={'TARGET':[row(d) for d in days]}
        with self.assertRaises(ValueError):
            self.calculate(stocks,days,snapshot_available_at='2026-01-02T21:00:00+08:00')
        with self.assertRaises(ValueError):
            self.calculate(stocks,days,date_label_evidence='')
        result=self.calculate(stocks,days,start_date=days[0],market_cutoff='2026-01-02T14:00:00+08:00')
        self.assertTrue(all(r['time'].startswith('2026-01-01') for r in result['stock_bars']))

    def test_bad_identity_dates_numbers_and_negative_volume_are_rejected(self):
        days=['2026-01-01','2026-01-02'];good=[row(d) for d in days]
        for bad in ([good[1],good[0]],[good[0],{**good[1],'close':True}],
                    [good[0],{**good[1],'amount':float('inf')}],[good[0],{**good[1],'volume':-1}]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):self.calculate({'TARGET':bad},days)
        args=dict(target_rows=good,target_code='TARGET',level=2,start_date=days[0],market_cutoff=days[-1]+'T15:00:00+08:00',
                  asof=days[-1]+'T20:00:00+08:00',snapshot_available_at=days[-1]+'T18:00:00+08:00',
                  trading_dates=days,date_label_evidence='test')
        with self.assertRaises(ValueError):
            industry.compute_context([('TARGET',good),('TARGET',good)],**args)

    def test_stale_lag_membership_is_not_assumed_current(self):
        days=['2026-01-01','2026-01-02','2026-01-03']
        stocks={'TARGET':[row(d) for d in days],'PEER':[row(days[0]),row(days[2],120,100)]}
        result=self.calculate(stocks,days)
        self.assertEqual(result['breadth'][-1]['expected_constituents'],1)
        self.assertIsNone(result['breadth'][-1]['daily_return'])

    def test_tail_reader_reaches_a_historical_window_and_keeps_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'TARGET.csv'
            columns=['股票代码','交易日期','收盘价','前收盘价','成交量','成交额',*industry.SW.values(),'padding']
            with path.open('w',encoding='gb18030',newline='') as handle:
                handle.write('synthetic provider note\n');writer=csv.DictWriter(handle,fieldnames=columns);writer.writeheader()
                for i in range(1000):
                    writer.writerow({'股票代码':'TARGET','交易日期':(date(2020,1,1)+timedelta(days=i)).isoformat(),
                        '收盘价':100,'前收盘价':100,'成交量':100,'成交额':1000,
                        **{field:'A' for field in industry.SW.values()},'padding':'x'*1000})
            code,rows,meta=industry.read_snapshot(path,start_date='2020-03-01',end_date='2020-03-10')
            self.assertEqual(code,'TARGET');self.assertEqual(len(rows),31)
            self.assertEqual(rows[-1]['date'],'2020-03-10')
            self.assertEqual(sum(r['date']<'2020-03-01' for r in rows),21)
            self.assertFalse(meta['full_file_hashed'])
            self.assertTrue(meta['suffix_sha256'])

    def test_cli_is_self_contained_refuses_overwrite_and_cleans_failed_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);days=['2026-01-01','2026-01-02','2026-01-03']
            columns=['股票代码','交易日期','收盘价','前收盘价','成交量','成交额',*industry.SW.values()]
            for code in ('TARGET','PEER'):
                with (root/(code+'.csv')).open('w',encoding='gb18030',newline='') as handle:
                    handle.write('synthetic provider note\n');writer=csv.DictWriter(handle,fieldnames=columns);writer.writeheader()
                    for day in days:
                        writer.writerow({'股票代码':code,'交易日期':day,'收盘价':100,'前收盘价':100,
                                         '成交量':100,'成交额':1000,**{field:'A' for field in industry.SW.values()}})
            request=dict(files=[str(root/'TARGET.csv'),str(root/'PEER.csv')],target_file=str(root/'TARGET.csv'),
                target_code='TARGET',level=2,start_date=days[1],market_cutoff=days[-1]+'T15:00:00+08:00',
                asof=days[-1]+'T20:00:00+08:00',snapshot_available_at=days[-1]+'T18:00:00+08:00',
                trading_dates=days,date_label_evidence='synthetic daily labels')
            path=root/'request.json';path.write_text(json.dumps(request),encoding='utf-8')
            failed=root/'failed'
            with patch.object(industry.json,'dump',side_effect=RuntimeError('write failed')):
                with self.assertRaises(RuntimeError):industry.main(['--request-json',str(path),'--output-dir',str(failed)])
            self.assertFalse(failed.exists());self.assertFalse(list(root.glob('.industry-*')))
            target=root/'output'
            industry.main(['--request-json',str(path),'--output-dir',str(target)])
            original=(target/'industry-context.json').read_bytes()
            with self.assertRaises(ValueError):industry.main(['--request-json',str(path),'--output-dir',str(target)])
            self.assertEqual((target/'industry-context.json').read_bytes(),original)


if __name__=='__main__':unittest.main()
