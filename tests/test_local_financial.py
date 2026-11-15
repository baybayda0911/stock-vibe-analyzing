"""Fictional XBX financial snapshots; no real market data or external calls."""

import copy
import csv
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.local_financial import FIELDS, load_financial


SECURITY = {'security_id': 'A-share-sz-000001', 'company': '虚构本地财务公司', 'market': 'A-share',
            'exchange': 'sz', 'code': '000001', 'share_class': 'A', 'currency': 'CNY'}
ASOF = '2026-11-15T18:00:00+08:00'
OBSERVED = '2026-11-15T18:00:01+08:00'


def row(period, public, revenue, profit, *, cfo=None, cost=None):
    value = {'stock_code': 'sz000001', 'statement_format': '一般企业', 'report_date': period,
             'publish_date': public, '抓取时间': public + ' 02:01:03.123456'}
    values = {name: '' for name in FIELDS}
    values.update(revenue=revenue, cost=cost if cost is not None else revenue * .6,
                  parent_profit=profit, consolidated_profit=profit * 1.25,
                  cfo=cfo if cfo is not None else profit * .5, capex=profit * .1,
                  inventory=80, receivables=40, registered_capital=50)
    value.update({field: str(values[name]) for name, field in FIELDS.items()})
    return value


class LocalFinancialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base/'sz000001/sz000001_一般企业.csv'
        self.path.parent.mkdir()
        self.rows = [row('20250331', '2025-04-25', 100, 10), row('20250630', '2025-08-21', 250, 25),
                     row('20250930', '2025-10-25', 450, 45), row('20251231', '2026-04-25', 700, 70),
                     row('20260331', '2026-04-25', 200, 20), row('20260630', '2026-08-21', 500, 50),
                     row('20260930', '2026-10-25', 900, 90)]

    def write(self, rows=None, encoding='utf-8-sig', columns=None):
        columns = columns or ['stock_code', 'statement_format', 'report_date', 'publish_date', '抓取时间', *FIELDS.values()]
        stream = io.StringIO(newline='')
        stream.write('虚构本地财务字段说明\n')
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(self.rows if rows is None else rows)
        self.path.write_bytes(stream.getvalue().encode(encoding))

    def load(self, **kwargs):
        return load_financial(self.path, kwargs.pop('security', SECURITY),
                              asof=kwargs.pop('asof', ASOF), observed_at=kwargs.pop('observed_at', OBSERVED), **kwargs)

    def test_cumulative_quarterly_ttm_and_cash_denominator(self):
        self.write()
        before = self.path.read_bytes()
        value = self.load()
        latest, quarter = value['latest_ytd'], value['latest_quarter']
        self.assertEqual(latest['revenue'], 900)
        self.assertEqual(quarter['revenue'], 400)
        self.assertEqual(quarter['parent_profit'], 40)
        self.assertAlmostEqual(quarter['gross_margin'], .4)
        self.assertAlmostEqual(latest['cash_conversion'], 45/112.5)
        self.assertEqual(latest['fcf'], 36)
        self.assertEqual(value['ttm']['revenue'], 1150)
        self.assertEqual(value['ttm']['parent_profit'], 115)
        self.assertAlmostEqual(quarter['parent_profit_qoq'], 1/3)
        self.assertEqual(latest['parent_profit_yoy'], 1)
        self.assertEqual(value['document']['tables']['annual'][0]['period_basis'], 'annual')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(value['provenance']['forecasts_included_as_actual'])
        self.assertNotIn('share_capital', latest)
        self.assertEqual(latest['registered_capital'], 50)

    def test_version_selection_is_not_first_publication_or_file_order(self):
        amended = row('20250630', '2026-10-26', 260, 26)
        self.write([amended, *reversed(self.rows)])
        value = self.load()
        june = next(row for row in value['document']['tables']['cumulative'] if row['report_date']=='2025-06-30')
        self.assertEqual(june['revenue'],260)
        self.assertEqual(june['publish_date'],'2026-10-26')
        self.assertEqual(june['first_local_publish_date'],'2025-08-21')
        q3 = next(row for row in value['document']['tables']['quarterly'] if row['report_date']=='2025-09-30')
        self.assertEqual(q3['revenue'],190)
        self.assertEqual(q3['publish_date'],'2026-10-26')
        self.assertEqual(len(q3['operand_versions']),2)
        old = self.load(asof='2026-10-25T23:59:59.999999+08:00')
        self.assertEqual(next(row for row in old['document']['tables']['cumulative'] if row['report_date']=='2025-06-30')['revenue'],250)
        self.assertTrue(old['provenance']['historical_reconstruction'])

    def test_intraday_date_only_publication_is_not_assumed_available(self):
        self.write()
        value = self.load(asof='2026-10-25T12:00:00+08:00')
        self.assertEqual(value['latest_ytd']['report_date'],'2026-06-30')
        self.assertGreater(value['provenance']['excluded_after_cutoff'],0)
        self.assertEqual(value['provenance']['capture_time_timezone'],'unknown')
        self.assertEqual(value['provenance']['unit_qualification'],'declared_not_independently_verified')

    def test_missing_prior_or_amount_is_not_filled_from_other_period(self):
        rows = [row for row in self.rows if row['report_date']!='20260630']
        rows[-1]['C_ncf_from_oa@xbx']=''
        self.write(rows)
        value=self.load()
        self.assertIsNone(value['latest_quarter']['revenue'])
        self.assertFalse(value['latest_quarter']['difference_complete'])
        self.assertIsNone(value['latest_ytd']['cash_conversion'])
        self.assertIsNone(value['ttm']['cfo'])
        self.assertEqual(value['latest_ytd']['inventory'],80)

    def test_duplicate_version_conflict_rejected_identical_duplicate_accepted(self):
        self.write([*self.rows,copy.deepcopy(self.rows[-1])])
        self.assertEqual(self.load()['latest_ytd']['revenue'],900)
        conflict=copy.deepcopy(self.rows[-1]);conflict['R_revenue@xbx']='901'
        self.write([*self.rows,conflict])
        with self.assertRaisesRegex(ValueError,'Conflicting'):
            self.load()

    def test_wrong_identity_statement_unit_and_schema_fail_closed(self):
        self.write()
        for security in (SECURITY|{'currency':'USD'}, SECURITY|{'code':'000002'}, SECURITY|{'security_id':'other'}):
            with self.subTest(security=security), self.assertRaises(ValueError):
                self.load(security=security)
        with self.assertRaises(ValueError): self.load(unit='CNY_ten_thousand')
        for key, value in [('stock_code','sz000002'),('statement_format','银行'),('publish_date','2026-02-30'),('report_date','2026-09-30')]:
            broken=copy.deepcopy(self.rows);broken[-1][key]=value;self.write(broken)
            with self.subTest(key=key), self.assertRaises(ValueError): self.load()
        self.write(columns=['stock_code', 'statement_format', 'report_date', 'publish_date', '抓取时间', *FIELDS.values(), 'R_revenue@xbx'])
        with self.assertRaisesRegex(ValueError,'field contract'): self.load()

    def test_invalid_amounts_rejected_zero_and_negative_denominators_unscored(self):
        for bad in ['nan','Infinity','1_000','1e1000','1e-9999','not-a-number']:
            broken=copy.deepcopy(self.rows);broken[-1]['R_np@xbx']=bad;self.write(broken)
            with self.subTest(value=bad), self.assertRaises(ValueError): self.load()
        for denominator in ('0','-5'):
            broken=copy.deepcopy(self.rows);broken[-1]['R_np@xbx']=denominator;self.write(broken)
            self.assertIsNone(self.load()['latest_ytd']['cash_conversion'])

    def test_encoding_hash_and_timezone_boundaries(self):
        import hashlib
        self.write(encoding='gb18030')
        value=self.load()
        self.assertEqual(value['provenance']['encoding'],'gb18030')
        self.assertEqual(value['provenance']['source_sha256'],hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(value['provenance']['source_bytes'],len(self.path.read_bytes()))
        with self.assertRaisesRegex(ValueError,'timezone'): self.load(asof='2026-11-15T18:00:00')
        with self.assertRaisesRegex(ValueError,'timezone'): self.load(observed_at='2026-11-15T18:00:00')
        with self.assertRaisesRegex(ValueError,'No financial version'): self.load(asof='2000-01-01T18:00:00+08:00')


if __name__=='__main__':
    unittest.main()
