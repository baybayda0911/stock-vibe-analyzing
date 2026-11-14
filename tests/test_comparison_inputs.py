"""Fictional unit/price provenance; never reads real research or providers."""
from copy import deepcopy
import hashlib
import json
import unittest

from app.comparison import _financial_units, _raw_prices
from test_platform_server import lab


SECURITY = {'security_id': 'A-share-sh-600001', 'company': '虚构甲公司', 'market': 'A-share',
    'exchange': 'sh', 'code': '600001', 'share_class': 'A', 'currency': 'CNY'}
STAMP = '2026-11-14T16:00:00+08:00'
UNIT_TEXT = 'CNY amounts in yuan; ratios in fractions, multiples not percentages. Display in 亿 = divide by 1e8.'
MONEY = {'revenue': 100, 'parent_profit': 10, 'consolidated_profit': 12, 'cfo': 7, 'capex': 5}


class SourceStore:
    lab = lab

    def __init__(self):
        self.records, self.files = {}, {}

    def add(self, rid, documents):
        attachments = []
        for name, document in documents.items():
            payload = json.dumps(document, ensure_ascii=False, sort_keys=True).encode('utf-8')
            self.files[rid, name] = payload
            attachments.append({'relative_path': name, 'sha256': hashlib.sha256(payload).hexdigest()})
        self.records[rid] = {'result_id': rid, 'security': deepcopy(SECURITY), 'module': 'fundamental',
            'generated_at': STAMP, 'research_asof': STAMP, 'attachments': attachments}
        return self.records[rid]

    def _record_reader(self, record):
        def read(name):
            entry = next(row for row in record['attachments'] if row['relative_path'] == name)
            payload = self.files[record['result_id'], name]
            if hashlib.sha256(payload).hexdigest() != entry['sha256']: raise ValueError('Registered bytes changed')
            return payload, 'application/json', name
        return read

    def context(self, *args, **kwargs):
        return {'security': deepcopy(SECURITY), 'results': list(self.records.values())}


def ref(record, name):
    return {'result_id': record['result_id'], 'attachment': name}


def bridge(rid='F1'):
    return {'schema': 'stock-fundamental-comparison/1', 'module': 'fundamental', 'security': deepcopy(SECURITY), 'result_id': rid,
        'metric_units': UNIT_TEXT, 'financial_bottom_tables': 'financial-bottom-tables.json',
        'metrics': {key + '_ytd_cny': value for key, value in MONEY.items()}}


class ComparisonInputTests(unittest.TestCase):
    def test_money_unit_requires_exact_declaration_and_cumulative_sources_are_checked(self):
        for unit, expected in [('CNY yuan', True), ('CNY unless field is ratio/date/share/price', True),
                ('CNY_10000', False), ('CNY in 亿元', False), (None, False)]:
            with self.subTest(unit=unit):
                store = SourceStore(); document = {'annual': [], 'quarterly': [], 'cumulative': []}
                if unit is not None: document['unit'] = unit
                record = store.add('F1', {'financial-bottom-tables.json': document})
                finance = {'cumulative': [{'source': ref(record, 'financial-bottom-tables.json')}]}
                proof = _financial_units(store, store.context(), finance)['F1', 'financial-bottom-tables.json']
                self.assertEqual(proof['verified'], expected)

    def test_CNY_groups_do_not_override_conflicting_units_or_unknown_schema(self):
        document = {'financial_period': '2026-01-01/2026-09-30', 'current_inputs_Q3_CNY': MONEY,
            'prior_9M_inputs_Q3_CNY': MONEY, 'q3_difference_CNY': MONEY}
        for change, expected in [({}, True), ({'units': '万元'}, False), ({'schema_version': 'unknown/1'}, False)]:
            with self.subTest(change=change):
                store = SourceStore(); record = store.add('F1', {'calculations.json': document | change})
                proof = _financial_units(store, store.context(), {'components': {'latest': ref(record, 'calculations.json')}})
                self.assertEqual(proof['F1', 'calculations.json']['verified'], expected)

    def test_registered_full_bridge_binds_own_calculation_money_values(self):
        calc = {'latest_ytd': deepcopy(MONEY), 'latest_quarter': deepcopy(MONEY)}
        store = SourceStore(); record = store.add('F1', {'calculations.json': calc, 'comparison-ready.json': bridge()})
        finance = {'components': {'latest': ref(record, 'calculations.json')}}
        proof = _financial_units(store, store.context(), finance)['F1', 'calculations.json']
        self.assertTrue(proof['verified']); self.assertEqual(proof['source_refs'][-1]['attachment'], 'comparison-ready.json')
        calc['latest_ytd']['revenue'] *= 10000
        store.add('F1', {'calculations.json': calc, 'comparison-ready.json': bridge()})
        self.assertFalse(_financial_units(store, store.context(), finance)['F1', 'calculations.json']['verified'])

    def test_inherited_unit_bridge_requires_original_registration_and_unchanged_bytes(self):
        original = {'result_id': 'F1', 'latest_ytd': deepcopy(MONEY), 'latest_quarter': deepcopy(MONEY)}
        store = SourceStore(); first = store.add('F1', {'calculations.json': original, 'comparison-ready.json': bridge()})
        original_sha = next(row['sha256'] for row in first['attachments'] if row['relative_path'] == 'calculations.json')
        inherited = deepcopy(original) | {'result_id': 'F2', 's4_inheritance': {'source_result_id': 'F1',
            'sha256': original_sha, 'values_unchanged': True}}
        second = store.add('F2', {'calculations.json': inherited, 'comparison-ready.json': bridge()})
        finance = {'components': {'latest': ref(second, 'calculations.json')}}
        self.assertTrue(_financial_units(store, store.context(), finance)['F2', 'calculations.json']['verified'])
        third = deepcopy(inherited) | {'result_id': 'F3', 's4_inheritance': {'source_result_id': 'F2',
            'sha256': next(row['sha256'] for row in second['attachments'] if row['relative_path'] == 'calculations.json'),
            'values_unchanged': True}, 'limits': ['本次沿用已归档计算，财务期、行情和原计算生成日期未更新；本次仅形成新的研究解释。']}
        store.add('F3', {'calculations.json': third, 'comparison-ready.json': bridge()})
        self.assertTrue(_financial_units(store, store.context(), {'latest': {'source': ref(store.records['F3'], 'calculations.json')}})['F3', 'calculations.json']['verified'])
        inherited['latest_ytd']['cfo'] = 999
        store.add('F2', {'calculations.json': inherited, 'comparison-ready.json': bridge()})
        self.assertFalse(_financial_units(store, store.context(), finance)['F2', 'calculations.json']['verified'])
        store.records.pop('F1')
        self.assertFalse(_financial_units(store, store.context(), finance)['F2', 'calculations.json']['verified'])

    def test_unit_bridge_wrong_security_or_changed_registered_bytes_fail_closed(self):
        wrong = bridge(); wrong['security'] = wrong['security'] | {'company': '不同公司'}
        store = SourceStore(); record = store.add('F1', {'calculations.json': {'latest_ytd': MONEY,
            'latest_quarter': MONEY}, 'comparison-ready.json': wrong})
        finance = {'components': {'latest': ref(record, 'calculations.json')}}
        with self.assertRaises(ValueError): _financial_units(store, store.context(), finance)
        store.files['F1', 'calculations.json'] += b' '
        with self.assertRaises(ValueError): _financial_units(store, store.context(), finance)

    def raw_case(self, mode='qfq'):
        times = ['2026-11-12T15:00:00+08:00', '2026-11-13T15:00:00+08:00']
        rows = [{'date': time[:10], 'open': value, 'high': value + 1, 'low': value - 1, 'close': value}
                for time, value in zip(times, (10, 12))]
        raw = {'source_sha256': 'a' * 64, 'extracted_at': STAMP, 'qualification': 'diagnostic',
            'fields': {'close': '收盘价'}, 'bars': rows}
        definition = {'source_raw_sha256': 'a' * 64, 'method': 'diagnostic-provider-reference-close-chain/1',
            'output_sha256': 'b' * 64, 'anchor_date': '2026-11-13', 'anchor_raw_close': 12,
            'created_at': STAMP, 'implemented_action_dates': ['2026-06-01']}
        analysis = {'schema_version': 'stock-technical-report/2', 'security': deepcopy(SECURITY), 'timeframes': {'1d': {
            'metadata': {'security': deepcopy(SECURITY), 'context': {'security_id': SECURITY['security_id'], 'timeframe': '1d'},
                'data_asof': times[-1], 'adjustment': {'mode': mode}, 'qualification': 'diagnostic',
                'input_sha256': 'b' * 64, 'source_sha256': 'b' * 64},
            'bars': [{'time': time, 'close': value, 'completed': True} for time, value in zip(times, (10, 12))]}}}
        documents = {'analysis.json': analysis, 'raw-ohlcv-snapshot.json': raw, 'reference-definition.json': definition}
        store = SourceStore(); record = store.add('T1', documents); record['module'] = 'technical'
        tech = {'source': ref(record, 'analysis.json'), 'data_asof': times[-1], 'adjustment': {'mode': mode},
            'qualification': 'diagnostic', 'bars': [{'date': row['date'], 'close': row['close']} for row in rows]}
        return store, tech, documents

    def test_raw_reference_conversion_requires_registered_chain_and_returns_nominal_raw(self):
        store, tech, _ = self.raw_case()
        raw = _raw_prices(store, SECURITY['security_id'], tech, STAMP)
        self.assertEqual(raw['points'], {'2026-11-12': 10, '2026-11-13': 12})
        self.assertEqual(raw['basis'], 'raw'); self.assertFalse(raw['qualified']); self.assertEqual(len(raw['source_refs']), 2)

    def test_raw_reference_mismatched_hash_method_coverage_anchor_and_OHLC_are_rejected(self):
        changes = [lambda d: d['reference-definition.json'].update(output_sha256='c' * 64),
            lambda d: d['reference-definition.json'].update(method='unknown/1'),
            lambda d: d['raw-ohlcv-snapshot.json'].update(source_sha256='c' * 64),
            lambda d: d['raw-ohlcv-snapshot.json']['bars'].pop(0),
            lambda d: d['reference-definition.json'].update(anchor_raw_close=999),
            lambda d: d['raw-ohlcv-snapshot.json']['bars'][0].update(high=1),
            lambda d: d['raw-ohlcv-snapshot.json']['bars'][0].update(date='2026-11-11'),
            lambda d: d['reference-definition.json'].update(created_at='2026-11-15T16:00:00+08:00')]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                store, tech, documents = self.raw_case(); change(documents); store.add('T1', documents)
                with self.assertRaises(ValueError): _raw_prices(store, SECURITY['security_id'], tech, STAMP)

    def test_raw_mode_matches_registered_analysis_and_never_trusts_display_mode_only(self):
        store, tech, documents = self.raw_case('raw')
        raw = _raw_prices(store, SECURITY['security_id'], tech, STAMP)
        self.assertEqual(raw['points']['2026-11-13'], 12)
        tech['bars'][-1]['close'] = 99
        with self.assertRaises(ValueError): _raw_prices(store, SECURITY['security_id'], tech, STAMP)
        store, tech, documents = self.raw_case('raw')
        documents['analysis.json']['timeframes']['1d']['metadata']['adjustment']['mode'] = 'qfq'
        store.add('T1', documents)
        with self.assertRaises(ValueError): _raw_prices(store, SECURITY['security_id'], tech, STAMP)


if __name__ == '__main__': unittest.main()
