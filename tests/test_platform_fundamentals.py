"""Fictional financial research only; no real company data in distribution."""

import copy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.fundamentals import fundamental_dashboard
from app.store import ResearchStore


REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / 'skills/stock-research-lab/scripts/research_lab.py'
spec = importlib.util.spec_from_file_location('fundamental_fixture_lab', TOOL)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
SECURITY = {'security_id': 'ZZ-TEST-FIN', 'company': '虚构财务甲', 'market': 'ZZ', 'exchange': 'TEST',
            'code': 'FIN', 'share_class': 'ordinary', 'currency': 'CNY'}
ASOF = '2026-11-13T23:00:00+08:00'


def row(day, revenue=1000, profit=100, cfo=50, *, publish='2026-10-20'):
    return {'report_date': day, 'publish_date': publish, 'revenue': revenue, 'cost': revenue * .6,
            'net_profit_parent': profit, 'net_profit_consolidated': profit * 1.25, 'cfo': cfo,
            'capex_cash': 20, 'inventory': 200, 'receivables': 100, 'assets': 2000, 'raw_close': 10,
            'price_date': day, 'pe_known': 15, 'known_period': '2025-09-30'}


def history_document():
    tables = {'annual': [row('2025-12-31')],
              'quarterly': [row('2026-03-31', 300, 30, 10), row('2026-06-30', 400, 40, 30), row('2026-09-30', 500, 50, 40)],
              'cumulative': [row('2025-09-30', 900, 90, 50), row('2026-06-30', 700, 70, 40), row('2026-09-30', 1200, 120, 80)],
              'vintages': []}
    return {'generated_at': '2026-11-01T18:00:00+08:00', 'unit': 'CNY yuan',
            'market_cutoff': '2026-10-30T15:00:00+08:00',
            'tables': {key: {'rows': value, 'source_csv': 'fictional.csv'} for key, value in tables.items()}}


class FundamentalProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'private-root'
        lab.init(self.root)
        self.store = ResearchStore(self.root, TOOL)
        self.history = history_document()
        self.append('F1', {'financial-bottom-tables.json': self.history}, role=None,
                    generated='2026-11-01T18:30:00+08:00', research='2026-11-01T18:00:00+08:00')

    def append(self, key, documents, *, role='full_research', schema='stock-research-components/1',
               generated='2026-11-13T18:30:00+08:00', research='2026-11-13T18:00:00+08:00', parents=()):
        report = self.base / (key + '.md')
        report.write_text('# 虚构报告\n\n**研究结论：这是虚构经营观察。**\n', encoding='utf-8')
        documents = copy.deepcopy(documents)
        if role is not None:
            documents['research-components.json'] = {'schema_version': schema, 'security': SECURITY,
                'result_id': key, 'content_role': role, 'components': [], 'published_date': '2026-10-30'}
        attachments = []
        for name, value in documents.items():
            source = self.base / (key + '-' + name)
            source.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            attachments.append({'source_file': str(source), 'relative_path': name, 'media_type': 'application/json'})
        request = {'security': SECURITY, 'module': 'fundamental', 'module_version': '0.3.1', 'result_id': key,
                   'generated_at': generated, 'research_asof': research, 'data_asof': None,
                   'parent_result_ids': list(parents), 'report_file': str(report),
                   'items': [{'item_id': 'OLD-CASH', 'type': 'observation', 'text': '虚构现金关注项',
                              'report_locator': 'tracking row', 'frozen_at': generated,
                              'definition': {'what': '现金配比', 'why': '保留旧判据', 'next_evidence': '下次财报'},
                              'status': 'pending', 'current_use': 'needs_review', 'evidence_ids': []}] if key == 'F1' else [],
                   'evidence': [{'evidence_id': 'Q3', 'source': '虚构三季披露', 'public_at': None,
                                 'public_at_precision': 'date_only', 'public_date': '2026-10-20',
                                 'financial_period': '2026-01-01/2026-09-30'}], 'attachments': attachments}
        lab.archive(self.root, request)
        return request

    def incremental(self):
        catalog = lab.scan(self.root)
        old = next(record for record in catalog['results'] if record['result_id'] == 'F1')
        digest = next(item['sha256'] for item in old['attachments'] if item['relative_path'] == 'financial-bottom-tables.json')
        current = row('2026-09-30', 1500, 150, 100)
        current.pop('assets')
        return {'generated_at': '2026-11-13T18:00:00+08:00', 'financial_period': '2026-01-01/2026-09-30',
                'market_cutoff': '2026-11-13T15:00:00+08:00',
                'current_inputs_Q3_CNY': current,
                'prior_9M_inputs_Q3_CNY': row('2025-09-30', 900, 90, 50),
                'q3_difference_CNY': {key: str(value) for key, value in {
                    'revenue': 800, 'cost': 480, 'net_profit_parent': 80, 'net_profit_consolidated': 100,
                    'cfo': 60, 'capex_cash': 30}.items()},
                'derived': {}, 'formulas': {'cash_conversion': 'CFO/consolidated NP'},
                'valuation': {'raw_close': '12', 'ttm_parent_profit': '200', 'pe_issued': '18'},
                'inheritance': {'result_id': 'F1', 'bottom_table_sha256': digest}}

    def view(self, **options):
        return fundamental_dashboard(self.store, SECURITY['security_id'], asof=options.pop('asof', ASOF), **options)

    def test_nested_legacy_history_is_formal_and_normalizes_same_period_cash(self):
        value = self.view()
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['source']['result_id'], 'F1')
        self.assertAlmostEqual(value['latest']['cash_conversion'], 80 / 150)
        self.assertEqual(value['latest']['fcf'], 60)
        self.assertEqual(value['quarterly'][-1]['period_basis'], 'single_quarter')
        self.assertEqual(value['overview']['conclusion'], '研究结论：这是虚构经营观察。')
        self.assertFalse(value['research_executed'])
        self.assertEqual(value['observations'][0]['item_id'], 'OLD-CASH')

    def test_incremental_overlays_only_its_period_and_keeps_component_provenance(self):
        self.append('F2', {'calculations.json': self.incremental()}, role='incremental_research', parents=['F1'])
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        value = self.view()
        self.assertEqual(value['components']['history']['result_id'], 'F1')
        self.assertEqual(value['components']['latest']['result_id'], 'F2')
        self.assertEqual(len(value['quarterly']), 3)
        self.assertEqual(value['quarterly'][-2]['revenue'], 400)
        self.assertEqual(value['latest_quarter']['revenue'], 800)
        self.assertAlmostEqual(value['latest_quarter']['parent_profit_qoq'], 1)
        self.assertAlmostEqual(value['latest']['cash_conversion'], 100 / 187.5)
        self.assertEqual(value['latest_quarter']['cash_conversion'], .6)
        self.assertEqual(value['latest']['publish_date'], '2026-10-20')
        self.assertEqual(value['latest']['assets'], 2000)
        self.assertEqual(value['latest']['field_sources']['assets']['result_id'], 'F1')
        self.assertEqual(value['quarterly'][-1]['field_sources']['raw_close']['result_id'], 'F1')
        self.assertTrue(value['conflicts'])
        self.assertEqual(value['observations'][0]['item_id'], 'OLD-CASH')
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_later_broker_source_never_replaces_formal_report_or_history(self):
        self.append('BROKER', {'calculations.json': self.incremental()}, role='broker_source')
        value = self.view()
        self.assertEqual(value['source']['result_id'], 'F1')
        self.assertEqual(value['latest']['revenue'], 1200)

    def test_unknown_schema_does_not_become_new_formal(self):
        self.append('UNKNOWN', {'calculations.json': self.incremental()}, schema='unsupported/2')
        self.assertEqual(self.view()['source']['result_id'], 'F1')

    def test_future_result_is_not_visible_at_earlier_asof(self):
        self.append('F2', {'calculations.json': self.incremental()}, role='incremental_research', parents=['F1'])
        value = self.view(asof='2026-11-13T18:15:00+08:00')
        self.assertEqual(value['source']['result_id'], 'F1')

    def test_unrelated_full_report_is_not_silently_inherited(self):
        self.append('F2', {'calculations.json': self.incremental()}, role='incremental_research')
        value = self.view()
        self.assertIsNone(value['components']['history'])
        self.assertEqual(value['status'], 'partial')

    def test_inherited_history_hash_must_match_declared_fingerprint(self):
        document = self.incremental()
        document['inheritance']['bottom_table_sha256'] = '0' * 64
        self.append('F2', {'calculations.json': document}, role='incremental_research', parents=['F1'])
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.view()

    def test_body_identity_mismatch_is_rejected_even_if_attachment_hash_is_valid(self):
        document = self.incremental()
        document['security'] = {**SECURITY, 'code': 'OTHER', 'security_id': 'ZZ-TEST-OTHER'}
        self.append('F2', {'calculations.json': document}, role='incremental_research', parents=['F1'])
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.view()

    def test_later_attachment_generation_is_rejected(self):
        document = self.incremental()
        document['generated_at'] = '2026-11-13T19:00:00+08:00'
        self.append('F2', {'calculations.json': document}, role='incremental_research', parents=['F1'])
        with self.assertRaisesRegex(ValueError, 'frozen research date'):
            self.view()

    def test_tampered_archived_attachment_is_rejected_before_projection(self):
        record = lab.scan(self.root)['results'][0]
        path = self.root / Path(record['path']).parent / 'financial-bottom-tables.json'
        path.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.view()

    def test_future_publication_row_is_rejected(self):
        document = copy.deepcopy(self.history)
        document['generated_at'] = '2026-11-13T18:00:00+08:00'
        document['tables']['quarterly']['rows'][-1]['publish_date'] = '2026-11-14'
        self.append('F2', {'financial-bottom-tables.json': document})
        with self.assertRaisesRegex(ValueError, 'not published'):
            self.view()

    def test_conflicting_financial_aliases_are_rejected(self):
        document = self.incremental()
        document['current_inputs_Q3_CNY']['parent_profit'] = 999
        self.append('F2', {'calculations.json': document}, role='incremental_research', parents=['F1'])
        with self.assertRaisesRegex(ValueError, 'aliases conflict'):
            self.view()

    def test_zero_or_negative_profit_does_not_claim_cash_conversion(self):
        document = self.incremental()
        document['current_inputs_Q3_CNY']['net_profit_consolidated'] = -1
        self.append('F2', {'calculations.json': document}, role='incremental_research', parents=['F1'])
        self.assertIsNone(self.view()['latest']['cash_conversion'])

    def test_boolean_is_not_a_financial_amount(self):
        document = self.incremental()
        document['current_inputs_Q3_CNY']['revenue'] = True
        self.append('F2', {'calculations.json': document}, role='incremental_research', parents=['F1'])
        with self.assertRaisesRegex(ValueError, 'finite numbers'):
            self.view()

    def test_known_security_with_no_fundamental_stays_empty(self):
        other = {**SECURITY, 'security_id': 'ZZ-TEST-EMPTY', 'code': 'EMPTY', 'company': '虚构空档'}
        lab.ensure_security(self.root, other)
        value = fundamental_dashboard(self.store, other['security_id'], asof=ASOF)
        self.assertEqual(value['status'], 'no_research')
        self.assertIsNone(value['source'])
        self.assertEqual(value['annual'], [])
        self.assertEqual(value['observations'], [])

    def test_unknown_latest_component_keeps_old_history_explicitly_partial(self):
        self.append('F2', {}, role='correction', parents=['F1'])
        value = self.view()
        self.assertEqual(value['source']['result_id'], 'F2')
        self.assertEqual(value['components']['latest']['result_id'], 'F1')
        self.assertEqual(value['status'], 'partial')
        self.assertTrue(any('无可识别计算组件' in item for item in value['limitations']))

    def test_explicit_unknown_calculation_schema_cannot_reuse_legacy_shape(self):
        document = self.incremental()
        document['schema_version'] = 'unsupported-financial-format/9'
        self.append('F2', {'calculations.json': document}, role='incremental_research', parents=['F1'])
        value = self.view()
        self.assertEqual(value['status'], 'partial')
        self.assertEqual(value['components']['latest']['result_id'], 'F1')
        self.assertEqual(value['latest']['revenue'], 1200)


if __name__ == '__main__':
    unittest.main()
