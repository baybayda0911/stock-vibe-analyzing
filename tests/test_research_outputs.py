import copy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/stock-technical-analysis/scripts/research_outputs.py'
spec = importlib.util.spec_from_file_location('research_outputs', SCRIPT)
outputs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(outputs)
ASOF = '2026-05-27T10:15:00+08:00'


class ResearchOutputTests(unittest.TestCase):
    def close_bars(self, count=131, step=1):
        start = datetime.fromisoformat('2026-01-01T15:30:00+08:00')
        return [{'time': (start + timedelta(days=i)).isoformat(), 'close': 100 + step * i, 'completed': True}
                for i in range(count)]

    def background(self, stock=None, **changes):
        stock = stock if stock is not None else self.close_bars()
        benchmark = {'id': 'SYNTHETIC-MARKET', 'bars': self.close_bars(step=0.5), 'comparable': True,
                     'comparison_basis': 'synthetic-price', 'available_at': ASOF}
        benchmark.update(changes)
        return outputs.market_background(stock, benchmark, asof=ASOF,
                                          metadata={'comparison_basis': 'synthetic-price', 'snapshot_available_at': ASOF,
                                                    'effective_source_group': 'shared-stock-input', 'qualification': 'eligible'})

    def technical(self, facts=None, **meta):
        metadata = {'context': {'timeframe': '1d'}, 'first_known_at': '2026-05-26T15:30:00+08:00',
                    'data_cutoff': '2026-05-26T15:30:00+08:00', 'effective_source_group': 'shared-stock-input',
                    'qualification': 'diagnostic'}
        metadata.update(meta)
        if facts is None:
            facts = [{'fact_id': 'F1', 'topic': 'conventional', 'metric': 'trend', 'value': 1,
                      'first_known_at': '2026-05-26T15:30:00+08:00', 'data_cutoff': '2026-05-26T15:30:00+08:00',
                      'source_group': 'derived-topic', 'relation': 'support', 'proposition_id': 'P1',
                      'definition': 'Declared synthetic trend', 'falsification': 'A predefined close invalidates it'}]
        return {'metadata': metadata, 'facts': facts, 'topics': {'conventional': {'status': 'ok'}},
                'propositions': [{'proposition_id': 'P1', 'text': 'Conditional trend continuation',
                                  'falsification': 'Predefined invalidation'}]}

    def test_background_uses_common_dates_fractional_returns_and_fixed_display_window(self):
        result = self.background()
        comparison = result['comparisons'][0]
        self.assertEqual(comparison['status'], 'ok', result)
        self.assertEqual(comparison['stock_index'][0], 100)
        self.assertEqual(comparison['reference_index'][0], 100)
        window = comparison['windows']['20']
        self.assertAlmostEqual(window['stock_return'], 230 / 210 - 1)
        self.assertAlmostEqual(window['reference_return'], 165 / 155 - 1)
        self.assertAlmostEqual(window['excess_return'], window['stock_return'] - window['reference_return'])
        long_cutoff = '2027-01-01T00:00:00+08:00'
        long = outputs.market_background(self.close_bars(350),
                                         {'id': 'LONG', 'bars': self.close_bars(350, 0.5), 'comparable': True,
                                          'comparison_basis': 'price', 'available_at': long_cutoff}, asof=long_cutoff,
                                         metadata={'comparison_basis': 'price', 'snapshot_available_at': long_cutoff})
        self.assertEqual(len(long['comparisons'][0]['common_dates']), 252)
        self.assertEqual(long['comparisons'][0]['stock_index'][0], 100)
        json.dumps(result, allow_nan=False)

    def test_missing_reference_days_are_not_forward_filled(self):
        reference = self.close_bars(step=0.5)[::2]
        result = self.background(bars=reference)
        comparison = result['comparisons'][0]
        self.assertEqual(len(comparison['common_dates']), len(reference))
        self.assertEqual(comparison['common_dates'], [b['time'] for b in reference])
        self.assertEqual(comparison['windows']['60']['status'], 'ok')
        self.assertEqual(comparison['windows']['120']['status'], 'insufficient_data')

    def test_bad_or_unavailable_baseline_only_degrades_its_comparison(self):
        for changes in ({'available_at': '2026-06-01T10:00:00+08:00'}, {'comparison_basis': 'total-return'},
                        {'bars': list(reversed(self.close_bars()))}):
            with self.subTest(changes=changes):
                result = self.background(**changes)
                self.assertNotEqual(result['status'], 'invalid_input', result)
                self.assertEqual(result['comparisons'][0]['common_dates'], [])
        bad_stock = self.close_bars()
        bad_stock[0]['close'] = True
        self.assertEqual(self.background(stock=bad_stock)['status'], 'invalid_input')
        self.assertEqual(self.background(stock=list(reversed(self.close_bars())))['status'], 'invalid_input')

    def test_unknown_industry_membership_never_becomes_qualified_industry_strength(self):
        descriptor = {'id': 'SYNTHETIC-PROXY', 'bars': self.close_bars(), 'comparable': True,
                      'comparison_basis': 'price', 'available_at': ASOF}
        metadata = {'comparison_basis': 'price', 'snapshot_available_at': ASOF, 'qualification': 'eligible'}
        result = outputs.market_background(self.close_bars(), industry=descriptor, metadata=metadata, asof=ASOF)
        self.assertEqual(result['comparisons'][1]['status'], 'missing')
        descriptor['proxy'] = True
        weak = outputs.market_background(self.close_bars(), industry=descriptor, metadata=metadata, asof=ASOF)
        self.assertEqual(weak['comparisons'][1]['qualification'], 'weak_proxy')
        descriptor.update(classification_known=True, classification_available_at='2026-01-01T00:00:00+08:00')
        qualified = outputs.market_background(self.close_bars(), industry=descriptor, metadata=metadata, asof=ASOF)
        self.assertEqual(qualified['comparisons'][1]['qualification'], 'eligible')
        unknown_snapshot = outputs.market_background(self.close_bars(), market=descriptor,
                                                     metadata={'comparison_basis': 'price', 'qualification': 'eligible'}, asof=ASOF)
        self.assertEqual(unknown_snapshot['comparisons'][0]['qualification'], 'diagnostic')

    def test_evidence_keeps_dependencies_and_not_run_distinct_without_voting(self):
        technical = self.technical()
        technical['topics']['harmonic'] = {'status': 'not_run'}
        technical['topics']['smc'] = {'status': 'missing_fields', 'missing_fields': ['open', 'high', 'low']}
        second = copy.deepcopy(technical['facts'][0])
        second.update(fact_id='F2', topic='ichimoku', relation='conflict')
        technical['facts'].append(second)
        result = outputs.evidence_table({'1d': technical}, self.background(), asof=ASOF)
        self.assertIn(result['status'], {'ok', 'partial'}, result)
        rows = {row['fact_id']: row for row in result['rows']}
        self.assertEqual(rows['F1']['source_group'], rows['F2']['source_group'])
        self.assertEqual(rows['F1']['relation'], 'support')
        self.assertEqual(rows['F2']['relation'], 'conflict')
        self.assertEqual(rows['1d-harmonic-status']['relation'], 'not_run')
        self.assertEqual(rows['1d-smc-status']['relation'], 'not_run')
        self.assertNotIn('score', result)
        self.assertNotIn('confidence', result)
        json.dumps(result, allow_nan=False)

    def test_future_evidence_and_future_propositions_do_not_leak_semantics(self):
        technical = self.technical(first_known_at='2026-06-01T00:00:00+08:00')
        technical['propositions'][0]['text'] = 'FUTURE-SECRET-PROPOSITION'
        technical['facts'][0].update(value=1234567, definition='FUTURE-SECRET-DEFINITION',
                                      falsification='FUTURE-SECRET-BOUNDARY')
        result = outputs.evidence_table({'1d': technical}, {}, asof=ASOF)
        encoded = json.dumps(result)
        self.assertNotIn('FUTURE-SECRET', encoded)
        self.assertNotIn('1234567', encoded)
        self.assertTrue(any(row['status'] == 'unavailable' for row in result['rows']))

    def test_fundamental_identity_is_scoped_to_original_result_and_snapshot(self):
        reference = {'source_result_id': 'ORIGINAL-A', 'evidence_id': 'E1', 'source_sha256': 'frozen-input',
                     'metric': 'operating-stage', 'value': 'observation', 'relation': 'neutral',
                     'public_at': '2026-05-01T00:00:00+08:00',
                     'first_known_at': '2026-05-01T00:00:00+08:00', 'data_cutoff': '2026-05-01T00:00:00+08:00'}
        refs = [reference, copy.deepcopy(reference), dict(reference, source_result_id='ORIGINAL-B')]
        result = outputs.evidence_table({}, {}, refs, asof=ASOF)
        rows = [r for r in result['rows'] if r['timeframe'] == 'fundamental']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['source_result_id'] for row in rows}, {'ORIGINAL-A', 'ORIGINAL-B'})
        future = dict(reference, public_at='2026-06-01T00:00:00+08:00', value='FUTURE-PUBLICATION')
        self.assertNotIn('FUTURE-PUBLICATION', json.dumps(outputs.evidence_table({}, {}, [future], asof=ASOF)))

    def test_bad_fact_container_and_unfrozen_proposition_are_not_silent_support(self):
        technical = self.technical()
        technical['facts'][0]['relation'] = 'invented-vote'
        self.assertEqual(outputs.evidence_table({'1d': technical}, {}, asof=ASOF)['status'], 'invalid_input')
        technical = self.technical()
        technical['propositions'] = []
        result = outputs.evidence_table({'1d': technical}, {}, asof=ASOF)
        row = next(r for r in result['rows'] if r['fact_id'] == 'F1')
        self.assertEqual(row['relation'], 'missing')
        weekly = self.technical()
        weekly['metadata']['context']['timeframe'] = '1w'
        weekly['propositions'][0]['text'] = 'A different weekly definition with the same local ID'
        result = outputs.evidence_table({'1d': self.technical(), '1w': weekly}, {}, asof=ASOF)
        self.assertNotEqual(result['status'], 'invalid_input', result)
        self.assertEqual({p['timeframe'] for p in result['propositions']}, {'1d', '1w'})

    def scenario(self, **changes):
        value = {'action': 'buy', 'trigger': 'Price exceeds the stated threshold', 'confirmation': 'Complete bar',
                 'invalidation': 'Falls below the stated boundary', 'observation_window': 'Five trading sessions',
                 'trigger_price': '10.001', 'invalidation_price': '9.996', 'price_basis': 'raw',
                 'first_known_at': '2026-05-26T15:30:00+08:00'}
        value.update(changes)
        return value

    def security(self):
        return {'security_id': 'ZZ-SYNTHETIC', 'market': 'A-share', 'exchange': 'SYNTHETIC', 'board': 'fixture-board'}

    def rules(self, **changes):
        value = {'validated': True, 'evidence': 'Synthetic rule fixture; not a real exchange rule',
                 'rule_id': 'SYNTHETIC-RULE', 'market': 'A-share', 'exchange': 'SYNTHETIC', 'board': 'fixture-board',
                 'available_at': '2026-01-01T00:00:00+08:00', 'effective_from': '2026-01-01T00:00:00+08:00',
                 'effective_until': '2026-12-31T23:59:59+08:00', 'tick_size': '0.01',
                 'quantity_step': '100', 'minimum_quantity': '100', 'has_price_limit': True,
                 'lower_price_limit': '9', 'upper_price_limit': '11', 'session_open': True, 'suspended': False,
                 'state_asof': '2026-05-27T10:10:00+08:00', 'state_valid_until': '2026-05-27T10:20:00+08:00',
                 't_plus_one': True}
        value.update(changes)
        return value

    def personal(self, **changes):
        value = {'available_at': '2026-05-27T10:10:00+08:00', 'investment_horizon': 'weeks',
                 'position_fraction': '0', 'risk_budget': '5', 'requested_quantity': '100',
                 'holdings_quantity': '0', 'available_cash': '1100', 'fee_estimate': '1', 'slippage_buffer': '0'}
        value.update(changes)
        return value

    def test_no_action_and_no_personal_context_do_not_invent_execution(self):
        untouched = outputs.execution_conditions({}, {}, asof=ASOF)
        self.assertEqual(untouched['status'], 'not_requested')
        result = outputs.execution_conditions(self.scenario(), self.security(), asof=ASOF)
        self.assertEqual(result['status'], 'non_personal')
        self.assertIsNone(result['requested_quantity'])
        self.assertFalse(result['orders_created'])
        self.assertFalse(result['can_submit_order'])

    def test_personal_card_uses_decimal_rounding_without_changing_original_condition(self):
        scenario = self.scenario()
        original = copy.deepcopy(scenario)
        result = outputs.execution_conditions(scenario, self.security(), self.rules(), self.personal(),
                                              asof=ASOF, qualification='eligible')
        self.assertEqual(result['status'], 'personal_conditional', result)
        self.assertEqual(result['prices']['rounded_trigger'], '10.01')
        self.assertEqual(result['prices']['rounded_invalidation'], '9.99')
        self.assertEqual(result['prices']['risk_distance'], '0.02')
        self.assertEqual(result['risk_amount_under_assumptions'], '3.00')
        self.assertEqual(scenario, original)
        self.assertFalse(result['can_submit_order'])

    def test_verified_raw_mapping_is_required_and_its_availability_is_respected(self):
        scenario = self.scenario(trigger_price='20.002', invalidation_price='19.992', price_basis='qfq')
        result = outputs.execution_conditions(scenario, self.security(), self.rules(), self.personal(),
                                              asof=ASOF, qualification='eligible')
        self.assertEqual(result['status'], 'blocked')
        scenario['price_mapping'] = {'validated': True, 'evidence': 'Synthetic conversion', 'adjusted_per_raw': '2',
                                     'available_at': '2026-05-27T10:10:00+08:00', 'valid_until': ASOF}
        result = outputs.execution_conditions(scenario, self.security(), self.rules(), self.personal(),
                                              asof=ASOF, qualification='eligible')
        self.assertEqual(result['status'], 'personal_conditional', result)
        self.assertEqual(result['prices']['raw_trigger'], '10.001')
        scenario['price_mapping']['available_at'] = '2026-05-28T10:10:00+08:00'
        self.assertEqual(outputs.execution_conditions(scenario, self.security(), self.rules(), self.personal(),
                                                       asof=ASOF, qualification='eligible')['status'], 'blocked')

    def test_diagnostics_unknown_rules_limits_session_and_risk_cannot_grant_personal_card(self):
        for rules, user, qualification in [(self.rules(), self.personal(), 'diagnostic'),
                                            (self.rules(validated=False), self.personal(), 'eligible'),
                                            (self.rules(board='different'), self.personal(), 'eligible'),
                                            (self.rules(upper_price_limit='10'), self.personal(), 'eligible'),
                                            (self.rules(suspended=True), self.personal(), 'eligible'),
                                            (self.rules(session_open=None), self.personal(), 'eligible'),
                                            (self.rules(t_plus_one=None), self.personal(), 'eligible'),
                                            (self.rules(), self.personal(risk_budget='1'), 'eligible'),
                                            (self.rules(), self.personal(fee_estimate=None), 'eligible')]:
            with self.subTest(rules=rules, user=user, qualification=qualification):
                result = outputs.execution_conditions(self.scenario(), self.security(), rules, user,
                                                      asof=ASOF, qualification=qualification)
                self.assertEqual(result['status'], 'blocked', result)
        result = outputs.execution_conditions(self.scenario(trigger_price='10', invalidation_price='9.99'),
                                              self.security(), self.rules(), self.personal(available_cash='1000', fee_estimate='2'),
                                              asof=ASOF, qualification='eligible')
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['checks']['cash_under_declared_costs'])
        slipped = outputs.execution_conditions(self.scenario(trigger_price='10', invalidation_price='9.99'),
                                               self.security(), self.rules(), self.personal(available_cash='1002', fee_estimate='1',
                                                                                          slippage_buffer='0.02', risk_budget='100'),
                                               asof=ASOF, qualification='eligible')
        self.assertEqual(slipped['status'], 'blocked')
        self.assertFalse(slipped['checks']['cash_under_declared_costs'])
        for scenario, security, user in [(self.scenario(first_known_at=None), self.security(), self.personal()),
                                         (self.scenario(trigger={'truthy': True}), self.security(), self.personal()),
                                         (self.scenario(), dict(self.security(), security_id=None), self.personal()),
                                         (self.scenario(), self.security(), self.personal(investment_horizon='  '))]:
            with self.subTest(scenario=scenario, security=security, user=user):
                self.assertEqual(outputs.execution_conditions(scenario, security, self.rules(), user,
                                                               asof=ASOF, qualification='eligible')['status'], 'blocked')

    def test_next_intraday_bar_does_not_unlock_same_day_purchase(self):
        scenario = self.scenario(action='sell', trigger_price='10', invalidation_price='9.9')
        user = self.personal(holdings_quantity='100', sellable_quantity='100', holding_cost='8', risk_budget='100',
                             new_purchase_at='2026-05-27T10:14:00+08:00', new_purchase_quantity='100')
        result = outputs.execution_conditions(scenario, self.security(), self.rules(), user, asof=ASOF, qualification='eligible')
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['checks']['sellable_quantity'])
        tomorrow = '2026-05-28T10:15:00+08:00'
        rules = self.rules(state_asof=tomorrow, state_valid_until=tomorrow)
        result = outputs.execution_conditions(scenario, self.security(), rules, user, asof=tomorrow, qualification='eligible')
        self.assertEqual(result['status'], 'personal_conditional', result)

    def test_quantity_rules_are_supplied_and_odd_lot_exit_is_explicit(self):
        scenario = self.scenario(action='sell', trigger_price='10', invalidation_price='9.9')
        user = self.personal(requested_quantity='50', holdings_quantity='50', sellable_quantity='50', holding_cost='8', risk_budget='100')
        self.assertEqual(outputs.execution_conditions(scenario, self.security(), self.rules(), user,
                                                       asof=ASOF, qualification='eligible')['status'], 'blocked')
        result = outputs.execution_conditions(scenario, self.security(), self.rules(allow_odd_lot_liquidation=True), user,
                                              asof=ASOF, qualification='eligible')
        self.assertEqual(result['status'], 'personal_conditional', result)
        new_rule = self.rules(quantity_step='1', minimum_quantity='20')
        self.assertEqual(outputs.execution_conditions(scenario, self.security(), new_rule, user,
                                                       asof=ASOF, qualification='eligible')['status'], 'personal_conditional')

    def test_bad_numbers_dates_and_future_scenario_are_rejected(self):
        scenarios = [self.scenario(trigger_price=True), self.scenario(trigger_price='NaN'), self.scenario(trigger_price='1e999999'),
                     self.scenario(first_known_at='2026-06-01T10:00:00+08:00'),
                     self.scenario(first_known_at='2026-05-27T10:00:00')]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.assertEqual(outputs.execution_conditions(scenario, self.security(), self.rules(), self.personal(),
                                                               asof=ASOF, qualification='eligible')['status'], 'invalid_input')
        result = outputs.execution_conditions(self.scenario(), self.security(), self.rules(),
                                              self.personal(available_at='2026-06-01T00:00:00+08:00', requested_quantity='987654'),
                                              asof=ASOF, qualification='eligible')
        self.assertEqual(result['status'], 'blocked')
        self.assertIsNone(result['requested_quantity'])
        self.assertNotIn('987654', json.dumps(result))

    def test_review_preserves_multiple_axes_without_trades_or_zero_return(self):
        records = [{'source_result_id': 'R1', 'item_id': 'I1', 'definition': {'trigger': 'prior rule'},
                    'condition_status': 'triggered_pending', 'adoption_status': 'adopted',
                    'execution_status': 'triggered_restricted', 'issues': ['plan', 'execution'],
                    'first_known_at': '2026-05-26T10:00:00+08:00', 'reason': 'T+1 restriction', 'pnl': '999'},
                   {'item_id': 'I2', 'condition_status': 'confirmed', 'execution_status': 'user_did_not_trade',
                    'issues': ['external_unknown']}]
        original = copy.deepcopy(records)
        result = outputs.review_summary(records, asof=ASOF)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['rows'][0]['issues'], ['plan', 'execution'])
        self.assertEqual(result['rows'][0]['original_definition'], {'trigger': 'prior rule'})
        self.assertEqual(result['rows'][1]['pnl_status'], 'not_applicable')
        self.assertNotIn('pnl', result['rows'][0])
        self.assertFalse(result['trades_created'])
        self.assertFalse(result['pnl_computed'])
        self.assertEqual(records, original)

    def test_future_feedback_and_future_items_do_not_leak_review_results(self):
        future = {'item_id': 'I1', 'condition_status': 'falsified', 'execution_status': 'executed',
                  'adoption_status': 'adopted', 'issues': ['execution'], 'reason': 'Future private feedback',
                  'executed_at': '2026-05-27T10:00:00+08:00', 'feedback_received_at': '2026-05-28T10:00:00+08:00',
                  'evidence_ids': ['FUTURE-SECRET']}
        result = outputs.review_summary([future, {'item_id': 'I2', 'first_known_at': '2026-06-01T10:00:00+08:00',
                                                   'reason': 'Hidden future result'}], asof=ASOF)
        self.assertEqual(result['rows'][0]['execution_status'], 'unknown')
        self.assertEqual(result['rows'][0]['condition_status'], 'unknown')
        self.assertEqual(result['rows'][0]['evidence_ids'], [])
        self.assertNotIn('executed_at', result['rows'][0]['timestamps'])
        self.assertNotIn('FUTURE-SECRET', json.dumps(result))
        self.assertNotIn('Hidden future result', json.dumps(result))
        self.assertEqual(result['excluded'][0]['item_id'], 'I2')
        future_trigger = {'item_id': 'I3', 'condition_status': 'confirmed', 'triggered_at': '2026-06-01T00:00:00+08:00',
                          'reason': 'FUTURE-TRIGGER-DETAIL', 'issues': ['analysis_conditions']}
        result = outputs.review_summary([future_trigger], asof=ASOF)
        self.assertEqual(result['rows'][0]['condition_status'], 'unknown')
        self.assertNotIn('FUTURE-TRIGGER-DETAIL', json.dumps(result))

    def test_review_duplicate_records_and_unvalidated_categories_are_not_counted(self):
        record = {'item_id': 'I1', 'condition_status': 'pending', 'issues': ['data_calculation', 'data_calculation']}
        result = outputs.review_summary([record, copy.deepcopy(record)], asof=ASOF)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['issues'], ['data_calculation'])
        self.assertEqual(outputs.review_summary([dict(record, issues=['invented'])], asof=ASOF)['status'], 'invalid_input')
        self.assertEqual(outputs.review_summary([dict(record, execution_status='pretend_fill')], asof=ASOF)['status'], 'invalid_input')


if __name__ == '__main__':
    unittest.main()
