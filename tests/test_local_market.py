"""Fictional local snapshots; no providers, real data, or network requests."""

import csv
from datetime import date, timedelta
import hashlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.local_market import FIELDS, HOLIDAYS_2026, load_calendar, load_index, load_market


SECURITY = {'security_id': 'A-share-sz-300001', 'company': '虚构测试公司', 'market': 'A-share',
            'exchange': 'sz', 'code': '300001', 'share_class': 'A', 'currency': 'CNY'}
NOW = '2026-11-15T10:00:00+08:00'


class LocalMarketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.market = self.root/'sz300001.csv'
        self.calendar = self.root/'period_offset.csv'

    def quotes(self, changes=None, *, encoding='gb18030'):
        header = ['股票代码', '股票名称', '交易日期', *FIELDS.values()]
        rows = [{'股票代码': 'sz300001', '股票名称': '虚构历史名', '交易日期': '2026-11-12',
                 '开盘价': '10', '最高价': '12', '最低价': '9', '收盘价': '11', '前收盘价': '10', '成交量': '100', '成交额': '1000'},
                {'股票代码': 'sz300001', '股票名称': '虚构测试公司', '交易日期': '2026-11-13',
                 '开盘价': '5', '最高价': '7', '最低价': '4', '收盘价': '6', '前收盘价': '5.5', '成交量': '200', '成交额': '1200'}]
        if changes:
            rows = changes(rows)
        stream = io.StringIO(newline='')
        stream.write('虚构来源广告\r\n')
        writer = csv.DictWriter(stream, fieldnames=header)
        writer.writeheader(); writer.writerows(rows)
        self.market.write_bytes(stream.getvalue().encode(encoding))
        return self.market

    def dates(self, rows=None):
        dates = [date(2026, 1, 1)+timedelta(days=index) for index in range(365)]
        holidays = set()
        for start, end in HOLIDAYS_2026:
            first, last = date.fromisoformat('2026-'+start), date.fromisoformat('2026-'+end)
            holidays.update(first+timedelta(days=index) for index in range((last-first).days+1))
        dates = [str(day) for day in dates if day.weekday()<5 and day not in holidays]
        if rows:
            dates = rows(dates)
        self.calendar.write_text('虚构计划\n交易日期,W_0\n'+'\n'.join(day+',1' for day in dates)+'\n',encoding='utf-8')
        return self.calendar

    def test_daily_frozen_identity_numbers_availability_and_potential_boundary(self):
        path = self.quotes()
        before = path.read_bytes()
        result = load_market(path, SECURITY, observed_at=NOW, asof=NOW)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result['source_sha'], hashlib.sha256(before).hexdigest())
        self.assertEqual(result['lastdate'], '2026-11-13')
        self.assertEqual(result['market_cutoff'], '2026-11-13T15:30:00+08:00')
        self.assertTrue(result['bars'][1]['mechanical_gap'])
        self.assertTrue(all(row['completed'] and row['available_at']==NOW for row in result['bars']))
        self.assertEqual(result['provenance']['units']['volume'], 'unknown')
        self.assertEqual(result['provenance']['units']['amount'], 'unknown')
        self.assertFalse(result['provenance']['adjustment_verified'])

    def test_incomplete_day_and_future_rows_not_claimed_available(self):
        path = self.quotes()
        before_close = '2026-11-13T15:29:59+08:00'
        result = load_market(path, SECURITY, observed_at=before_close, asof=before_close)
        self.assertEqual(result['lastdate'], '2026-11-12')
        self.assertEqual(result['provenance']['excluded'][0]['date'], '2026-11-13')
        with self.assertRaisesRegex(ValueError, 'backdate'):
            load_market(path, SECURITY, observed_at=NOW, asof=before_close)
        with self.assertRaisesRegex(ValueError, 'timezone'):
            load_market(path, SECURITY, observed_at='2026-11-15', asof=NOW)
        exact_close = '2026-11-13T15:30:00+08:00'
        self.assertEqual(load_market(path, SECURITY, observed_at=exact_close, asof=exact_close)['lastdate'], '2026-11-13')

    def test_source_changed_while_reading_is_rejected_and_not_silently_reloaded(self):
        path = self.quotes()
        actual = Path.read_bytes
        def changed(source):
            payload = actual(source)
            with source.open('ab') as output:
                output.write(b'\n')
            return payload
        with patch.object(Path, 'read_bytes', changed), self.assertRaisesRegex(ValueError, 'changed during'):
            load_market(path, SECURITY, observed_at=NOW, asof=NOW)

    def test_same_duplicate_collapses_but_conflicting_quote_fails(self):
        self.quotes(lambda rows: rows + [rows[1].copy()])
        result = load_market(self.market, SECURITY, observed_at=NOW, asof=NOW)
        self.assertEqual(len(result['bars']), 2)
        self.assertEqual(result['provenance']['identical_duplicates_removed'], 1)
        self.quotes(lambda rows: rows + [rows[1] | {'成交额': '1201'}])
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            load_market(self.market, SECURITY, observed_at=NOW, asof=NOW)

    def test_unknown_identity_filename_nonfinite_negative_and_bad_ohlc_fail(self):
        path = self.quotes()
        for identity in (SECURITY | {'code': '300002'}, SECURITY | {'currency':'USD'}, SECURITY | {'security_id':'300001'}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                load_market(path, identity, observed_at=NOW, asof=NOW)
        for field, value in [('收盘价','nan'),('最高价','inf'),('开盘价','0'),('成交量','-1'),('成交额',''),('最低价','12'),('股票代码','sh300001')]:
            with self.subTest(field=field):
                self.quotes(lambda rows: [rows[0],rows[1] | {field:value}])
                with self.assertRaises(ValueError):
                    load_market(path, SECURITY, observed_at=NOW, asof=NOW)

    def test_utf8_and_quoted_multiline_name_parse_without_field_shift(self):
        self.quotes(lambda rows:[rows[0] | {'股票名称':'虚构,\n历史名'}, rows[1]], encoding='utf-8')
        result=load_market(self.market, SECURITY, observed_at=NOW, asof=NOW)
        self.assertEqual(result['bars'][0]['close'],11)
        self.assertEqual(len(result['bars']),2)

    def test_malformed_quote_or_oversized_field_is_a_bounded_validation_error(self):
        header='股票代码,股票名称,交易日期,'+','.join(FIELDS.values())+'\n'
        for row in ('sz300001,"unterminated', 'sz300001,'+'甲'*140000+',2026-11-13,1,1,1,1,1,1,1\n'):
            with self.subTest(length=len(row)):
                self.market.write_text(header+row,encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'quoting or field size'):
                    load_market(self.market,SECURITY,observed_at=NOW,asof=NOW)

    def test_calendar_scoped_match_not_global_or_price_certification(self):
        path = self.dates(lambda rows:['2025-12-31',*rows,'2027-01-04'])
        before = path.read_bytes()
        result=load_calendar(path, observed_at=NOW, asof=NOW)
        self.assertEqual(path.read_bytes(),before)
        self.assertTrue(result['2026_crosscheck']['matched'])
        self.assertEqual(result['2026_crosscheck']['expected_sessions'],242)
        self.assertFalse(result['verified'])
        self.assertTrue(result['session_verified'])
        self.assertEqual(result['verification_start'],'2026-01-01')
        self.assertEqual(result['verification_end'],'2026-12-31')
        self.assertEqual(set(result['trading_dates']),set(result['session_closes']))
        self.assertEqual(result['session_closes']['2026-11-13'],'2026-11-13T15:30:00+08:00')
        self.assertNotIn('adjustment_verified',result)

    def test_wrong_calendar_date_changes_do_not_certify_2026(self):
        path=self.dates(lambda rows:[day for day in rows if day!='2026-11-13'])
        result=load_calendar(path,observed_at=NOW,asof=NOW)
        self.assertFalse(result['2026_crosscheck']['matched'])
        self.assertNotIn('verification_start',result)
        self.assertEqual(result['2026_crosscheck']['missing_dates'],['2026-11-13'])
        self.dates(lambda rows:[*rows,'2026-11-13'])
        with self.assertRaisesRegex(ValueError,'unique'):
            load_calendar(path,observed_at=NOW,asof=NOW)
        self.dates(lambda rows:[*rows,'2026-11-14'])
        with self.assertRaisesRegex(ValueError,'weekdays'):
            load_calendar(path,observed_at=NOW,asof=NOW)

    def test_calendar_does_not_backdate_or_silently_choose_other_session_end(self):
        path=self.dates()
        with self.assertRaisesRegex(ValueError,'backdate'):
            load_calendar(path,observed_at=NOW,asof='2026-11-14T12:00:00+08:00')
        with self.assertRaises(ValueError):
            load_calendar(path,observed_at=NOW,asof=NOW,session_close='15:00')

    def test_CSI300_exact_source_completed_cutoff_and_hash_without_total_return_claim(self):
        folder=self.root/'stock-main-index-data'; folder.mkdir()
        path=folder/'sh000300.csv'
        original=b'candle_end_time,close,index_code\n2026-11-12,4100,sh000300\n2026-11-13,4000,sh000300\n2026-11-16,4200,sh000300\n'
        path.write_bytes(original)
        result=load_index(path,observed_at=NOW,asof=NOW)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(result['source_sha'],hashlib.sha256(original).hexdigest())
        self.assertEqual(result['lastdate'],'2026-11-13')
        self.assertEqual(result['bars'][-1]['close'],4000)
        self.assertEqual(result['bars'][-1]['available_at'],NOW)
        self.assertEqual(len(result['provenance']['excluded']),1)
        before='2026-11-13T15:29:59+08:00'
        self.assertEqual(load_index(path,observed_at=before,asof=before)['lastdate'],'2026-11-12')

    def test_index_wrong_path_identity_nonfinite_and_duplicate_conflict_fail(self):
        folder=self.root/'stock-main-index-data'; folder.mkdir()
        path=folder/'sh000300.csv'
        header='candle_end_time,close,index_code\n'
        for rows in ('2026-11-13,4000,sh000001\n', '2026-11-13,nan,sh000300\n',
                     '2026-11-13,4000,sh000300\n2026-11-13,4001,sh000300\n'):
            path.write_text(header+rows,encoding='utf-8')
            with self.subTest(rows=rows),self.assertRaises(ValueError):
                load_index(path,observed_at=NOW,asof=NOW)
        path.write_text(header+'2026-11-13,4000,sh000300\n'*2,encoding='utf-8')
        self.assertEqual(load_index(path,observed_at=NOW,asof=NOW)['provenance']['identical_duplicates_removed'],1)
        other=self.root/'sh000300.csv'; other.write_bytes(path.read_bytes())
        with self.assertRaises(ValueError):
            load_index(other,observed_at=NOW,asof=NOW)


if __name__ == '__main__':
    unittest.main()
