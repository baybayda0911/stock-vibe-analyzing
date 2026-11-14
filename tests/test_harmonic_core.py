import copy
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import random
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/stock-technical-analysis/scripts/harmonic_core.py'
spec = importlib.util.spec_from_file_location('harmonic_core', SCRIPT)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def stamp(index):
    return (date(2026, 1, 1) + timedelta(days=index)).isoformat()


def prices(b, c, d):
    return [100.0, 200.0, 200 - 100 * b, 200 - 100 * b + 100 * b * c, 200 - 100 * d]


SAMPLES = {'Gartley': (0.618, 2 - 0.786 / 0.618, 0.786),
           'Bat': (0.45, 0.7, 0.886), 'Butterfly': (0.786, 0.6, 1.27),
           'Crab': (0.6, 0.8, 1.618)}


def pivots(values, mirrored=False):
    return [{'index': index * 2, 'time': stamp(index * 2),
             'kind': ('H' if index % 2 == 0 else 'L') if mirrored else ('L' if index % 2 == 0 else 'H'),
             'price': 400 - price if mirrored else price,
             'first_known_at': stamp(index * 2), 'confirmed_at': stamp(index * 2 + 1)}
            for index, price in enumerate(values)]


def bars(values):
    return [{'time': stamp(index), 'open': price, 'high': price, 'low': price,
             'close': price, 'completed': True} for index, price in enumerate(values)]


def named(result, key, name):
    return [candidate for candidate in result[key] if candidate['pattern'] == name]


class HarmonicCoreTests(unittest.TestCase):
    def test_four_rule_profiles_have_exact_bull_and_bear_positive_examples(self):
        for pattern, parameters in SAMPLES.items():
            for mirrored in (False, True):
                with self.subTest(pattern=pattern, mirrored=mirrored):
                    result = core.analyze_pivots(pivots(prices(*parameters), mirrored),
                                                  profile={'relative_tolerance': 0})
                    found = named(result, 'patterns', pattern)
                    self.assertEqual(len(found), 1, result)
                    candidate = found[0]
                    self.assertEqual(candidate['direction'], 'bearish' if mirrored else 'bullish')
                    self.assertEqual(candidate['status'], 'geometry_confirmed')
                    self.assertTrue(all(check['passed'] for check in candidate['checks'].values()))
                    self.assertTrue(candidate['prz']['valid'])
                    self.assertEqual(candidate['confirmed_at'], stamp(9))
                    self.assertLess(candidate['points']['D']['occurred_at'], candidate['confirmed_at'])
                    json.dumps(result, allow_nan=False)

    def test_actual_ratios_and_meanings_are_reproducible(self):
        result = core.analyze_pivots(pivots(prices(*SAMPLES['Bat'])))
        ratios = named(result, 'patterns', 'Bat')[0]['ratios']
        self.assertAlmostEqual(ratios['AB_XA'], 0.45)
        self.assertAlmostEqual(ratios['BC_AB'], 0.7)
        self.assertAlmostEqual(ratios['AD_XA'], 0.886)
        self.assertAlmostEqual(ratios['CD_AB'], 0.886 / 0.45 - 1 + 0.7)
        self.assertAlmostEqual(ratios['CD_BC'], ratios['CD_AB'] / 0.7)
        self.assertIn('CD_AB', result['metadata']['ratio_meanings'])

    def test_forming_has_intersection_and_no_fabricated_d_time(self):
        for pattern, parameters in SAMPLES.items():
            with self.subTest(pattern=pattern):
                confirmed = pivots(prices(*parameters))[:4]
                result = core.analyze_pivots(confirmed, profile={'relative_tolerance': 0})
                candidate = named(result, 'forming', pattern)[0]
                interval = candidate['prz']['intervals'][0]
                self.assertAlmostEqual(interval['lower'], prices(*parameters)[4])
                self.assertAlmostEqual(interval['upper'], prices(*parameters)[4])
                self.assertIsNone(candidate['points']['D']['time'])
                self.assertIsNone(candidate['points']['D']['index'])
                self.assertIsNone(candidate['confirmed_at'])
                self.assertEqual(candidate['first_known_at'], confirmed[-1]['confirmed_at'])
                self.assertEqual(len(candidate['prz']['constraints']), 3)

    def test_prz_is_common_region_not_union_or_average(self):
        values = prices(0.618, 0.382, 0.786)
        result = core.analyze_pivots(pivots(values)[:4], profile={'relative_tolerance': 0})
        rejected = named(result, 'rejected', 'Gartley')
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0]['prz']['valid'])
        self.assertEqual(rejected[0]['prz']['intervals'], [])
        self.assertFalse(named(result, 'forming', 'Gartley'))

    def test_each_prz_boundary_satisfies_all_d_constraints_and_mirrors(self):
        for pattern, parameters in SAMPLES.items():
            with self.subTest(pattern=pattern):
                source = prices(*parameters)
                bull = named(core.analyze_pivots(pivots(source)[:4]), 'forming', pattern)[0]
                bear = named(core.analyze_pivots(pivots(source, True)[:4]), 'forming', pattern)[0]
                bullish = bull['prz']['intervals'][0]
                bearish = bear['prz']['intervals'][0]
                self.assertAlmostEqual(bearish['lower'], 400 - bullish['upper'])
                self.assertAlmostEqual(bearish['upper'], 400 - bullish['lower'])
                for price in (bullish['lower'], bullish['upper']):
                    completed = named(core.analyze_pivots(pivots(source[:4] + [price])), 'patterns', pattern)
                    self.assertEqual(len(completed), 1)
                self.assertEqual(bull['invalidation']['relation'], 'below')
                self.assertEqual(bear['invalidation']['relation'], 'above')

    def test_d_preview_uses_complete_rules_and_never_confirms_d(self):
        points = pivots(prices(*SAMPLES['Butterfly']))
        active = dict(points[-1], confirmed_at=None)
        result = core.analyze_pivots(points[:4], active_pivot=active)
        preview = named(result, 'previews', 'Butterfly')[0]
        self.assertEqual(preview['status'], 'active_d_preview')
        self.assertIsNone(preview['confirmed_at'])
        self.assertIsNone(preview['points']['D']['confirmed_at'])
        self.assertEqual(result['patterns'], [])
        self.assertTrue(preview['checks']['CD_AB']['passed'])
        active['price'] = points[3]['price'] - 5
        result = core.analyze_pivots(points[:4], active_pivot=active)
        self.assertFalse(named(result, 'previews', 'Butterfly'))

    def test_each_completed_rule_can_reject_an_independent_failed_check(self):
        for pattern, parameters in SAMPLES.items():
            with self.subTest(pattern=pattern):
                points = pivots(prices(*parameters))
                points[-1]['price'] = points[3]['price'] - 1
                result = core.analyze_pivots(points, profile={'relative_tolerance': 0})
                self.assertFalse(named(result, 'patterns', pattern))
                rejected = named(result, 'withdrawn', pattern)
                self.assertEqual(len(rejected), 1)
                self.assertFalse(rejected[0]['checks']['CD_AB']['passed'])

    def test_relative_tolerance_is_symmetric_inclusive_and_bounded(self):
        rule = {'AB_XA': (0.618, 0.618)}
        lower, upper = 0.618 * 0.95, 0.618 * 1.05
        for value in (lower, 0.618, upper):
            self.assertTrue(core._checks({'AB_XA': value}, rule, 0.05)['AB_XA']['passed'])
        for value in (lower - 1e-7, upper + 1e-7):
            self.assertFalse(core._checks({'AB_XA': value}, rule, 0.05)['AB_XA']['passed'])
        interval = {'BC_AB': (0.382, 0.886)}
        self.assertTrue(core._checks({'BC_AB': 0.5}, interval, 0)['BC_AB']['passed'])
        self.assertEqual(core.analyze_pivots([], profile={'relative_tolerance': 0.11})['status'], 'invalid_input')
        self.assertEqual(core.analyze_pivots([], profile={'version': 'unverified'})['status'], 'invalid_input')

    def test_no_candidates_and_insufficient_are_separate(self):
        empty = core.analyze_harmonics([])
        self.assertEqual(empty['status'], 'insufficient_history')
        result = core.analyze_pivots(pivots([100, 200, 110, 195]))
        self.assertEqual(result['status'], 'no_candidates')
        self.assertEqual(result['patterns'], [])
        self.assertEqual(result['forming'], [])

    def test_bad_ohlc_and_time_are_explicitly_rejected(self):
        mutations = ({'close': float('nan')}, {'low': 101}, {'high': 99}, {'open': 0},
                     {'completed': False}, {'time': 'unknown'}, {'close': True})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                rows = bars([100])
                rows[0].update(mutation)
                result = core.analyze_harmonics(rows)
                self.assertEqual(result['status'], 'invalid_input')
                self.assertTrue(result['limitations'])
        duplicate = bars([100, 110])
        duplicate[1]['time'] = duplicate[0]['time']
        self.assertEqual(core.analyze_harmonics(duplicate)['status'], 'invalid_input')
        mixed = bars([100, 110])
        mixed[1]['time'] = '2026-01-02T12:00:00+08:00'
        self.assertEqual(core.analyze_harmonics(mixed)['status'], 'invalid_input')

    def test_pivot_trust_boundary_rejects_zero_legs_labels_and_future(self):
        for change in ({'price': 100}, {'confirmed_at': None}, {'kind': 'L'}, {'price': float('inf')},
                       {'index': 0}, {'time': stamp(0)}, {'first_known_at': stamp(0)},
                       {'confirmed_at': stamp(0)}, {'occurred_at': stamp(0)}, {'revisable': True}):
            with self.subTest(change=change):
                points = pivots([100, 200, 140, 180])
                points[1].update(change)
                self.assertEqual(core.analyze_pivots(points)['status'], 'invalid_input')
        points = pivots(prices(*SAMPLES['Gartley']))
        self.assertEqual(core.analyze_pivots(points, data_cutoff=stamp(8))['status'], 'invalid_input')
        self.assertEqual(core.analyze_pivots(points[:4], active_pivot=points[-1])['status'], 'invalid_input')

    def test_confirmations_lag_occurrence_and_only_prefix_can_match(self):
        values = prices(*SAMPLES['Gartley'])
        rows = bars(values + [140])
        before = core.analyze_harmonics(rows[:5])
        self.assertEqual(len(before['points']), 4)
        self.assertFalse(before['patterns'])
        self.assertEqual(len(named(before, 'previews', 'Gartley')), 1)
        after = core.analyze_harmonics(rows)
        pattern = named(after, 'patterns', 'Gartley')[0]
        self.assertEqual(pattern['points']['D']['occurred_at'], stamp(4))
        self.assertEqual(pattern['points']['D']['confirmed_at'], stamp(5))
        self.assertEqual(pattern['confirmed_at'], stamp(5))

    def test_same_bar_extreme_and_reversal_defers_confirmation(self):
        rows = bars([100, 120, 125, 118])
        rows[2].update(open=120, high=130, low=110, close=125)
        before = core.build_pivots(rows[:3])
        self.assertEqual(len(before['points']), 1)
        self.assertEqual(before['active_point']['price'], 130)
        self.assertTrue(before['ambiguities'])
        after = core.build_pivots(rows)
        self.assertEqual(len(after['points']), 2)
        self.assertEqual(after['points'][-1]['occurred_at'], stamp(2))
        self.assertEqual(after['points'][-1]['confirmed_at'], stamp(3))

    def test_bootstrap_same_bar_order_is_not_invented(self):
        rows = bars([100, 100])
        rows[1].update(open=100, high=120, low=80, close=100)
        result = core.build_pivots(rows)
        self.assertEqual(result['points'], [])
        self.assertIsNone(result['active_point'])
        self.assertTrue(result['ambiguities'])

    def test_equal_extremes_keep_first_occurrence_and_gap_confirms(self):
        result = core.build_pivots(bars([100, 120, 120, 100]))
        self.assertEqual(result['points'][-1]['price'], 120)
        self.assertEqual(result['points'][-1]['occurred_at'], stamp(1))
        self.assertEqual(result['points'][-1]['confirmed_at'], stamp(3))
        self.assertEqual(result['active_point']['price'], 100)

    def test_active_extreme_revision_does_not_rewrite_confirmed_prefix(self):
        rows = bars([100, 120, 125, 130])
        earlier = core.build_pivots(rows[:2])
        later = core.build_pivots(rows)
        self.assertEqual(earlier['points'], later['points'])
        self.assertEqual(earlier['active_point']['price'], 120)
        self.assertEqual(later['active_point']['price'], 130)
        self.assertNotEqual(earlier['active_point']['revision_id'], later['active_point']['revision_id'])

    def test_forming_identity_survives_preview_and_confirmation(self):
        points = pivots(prices(*SAMPLES['Bat']))
        forming = named(core.analyze_pivots(points[:4]), 'forming', 'Bat')[0]
        active = dict(points[-1], confirmed_at=None)
        preview = named(core.analyze_pivots(points[:4], active_pivot=active), 'previews', 'Bat')[0]
        completed = named(core.analyze_pivots(points), 'patterns', 'Bat')[0]
        self.assertEqual(forming['candidate_id'], preview['candidate_id'])
        self.assertEqual(forming['candidate_id'], completed['candidate_id'])
        self.assertEqual(len({forming['revision_id'], preview['revision_id'], completed['revision_id']}), 3)
        other_scale = named(core.analyze_pivots(points, profile={'reversal_threshold': 0.08}), 'patterns', 'Bat')[0]
        self.assertNotEqual(completed['candidate_id'], other_scale['candidate_id'])

    def test_overlapping_shape_identities_are_retained_without_duplicate_scan_results(self):
        points = pivots(prices(0.5, 0.8, 0.886))[:4]
        result = core.analyze_pivots(points)
        self.assertTrue(named(result, 'forming', 'Bat'))
        self.assertTrue(named(result, 'forming', 'Crab'))
        identifiers = [candidate['candidate_id'] for candidate in result['forming']]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        # An overlapping future rule still must not be deduplicated by node IDs alone.
        with patch.dict(core.RULES, {'Bat': copy.deepcopy(core.RULES['Gartley'])}):
            result = core.analyze_pivots(pivots(prices(*SAMPLES['Gartley'])))
        self.assertEqual(len(named(result, 'patterns', 'Gartley')), 1)
        self.assertEqual(len(named(result, 'patterns', 'Bat')), 1)

    def test_prz_touch_does_not_imply_confirmation_and_overshoot_withdraws(self):
        values = prices(*SAMPLES['Gartley'])
        result = core.analyze_harmonics(bars(values))
        self.assertTrue(named(result, 'previews', 'Gartley'))
        self.assertFalse(result['patterns'])
        overshoot = core.analyze_harmonics(bars(values + [110]))
        self.assertFalse(named(overshoot, 'previews', 'Gartley'))
        self.assertFalse(named(overshoot, 'forming', 'Gartley'))
        withdrawn = named(overshoot, 'withdrawn', 'Gartley')[0]
        self.assertEqual(withdrawn['withdrawn_at'], stamp(5))
        self.assertIn('boundary', withdrawn['reason'])
        self.assertEqual(withdrawn['points']['D']['price'], 110)
        self.assertFalse(withdrawn['checks']['AD_XA']['passed'])

    def test_later_invalidation_preserves_geometry_and_original_event(self):
        values = prices(*SAMPLES['Gartley'])
        earlier = core.analyze_harmonics(bars(values + [140]))
        later = core.analyze_harmonics(bars(values + [140, 105]))
        pattern = named(later, 'patterns', 'Gartley')[0]
        self.assertEqual(pattern['status'], 'geometry_confirmed')
        self.assertEqual(pattern['scenario_status'], 'invalidated')
        self.assertEqual(pattern['invalidated_at'], stamp(6))
        old = [event for event in earlier['events'] if event['type'] == 'geometry_confirmed']
        new = [event for event in later['events'] if event['type'] == 'geometry_confirmed']
        self.assertEqual(old, new)
        self.assertTrue(any(event['type'] == 'scenario_invalidated' for event in later['events']))

    def test_replay_matches_independent_prefixes_and_future_cannot_mutate_old_events(self):
        values = prices(*SAMPLES['Gartley']) + [140, 105, 145, 150, 130]
        rows = bars(values)
        original = copy.deepcopy(rows)
        replay = core.replay_harmonics(rows, context={'security_id': 'ZZ-DEMO', 'timeframe': 'day'})
        for end, result in enumerate(replay, 1):
            with self.subTest(end=end):
                self.assertEqual(result, core.analyze_harmonics(rows[:end], context={'security_id': 'ZZ-DEMO', 'timeframe': 'day'}))
                cutoff = result['metadata']['data_cutoff']
                self.assertTrue(all(point['confirmed_at'] <= cutoff for point in result['points']))
                self.assertTrue(all(event['at'] <= cutoff for event in result['events']))
        final_events = {event['event_id']: event for event in replay[-1]['events']}
        for result in replay:
            for event in result['events']:
                if event['type'] in ('candidate_opened', 'geometry_confirmed', 'pivot_confirmed'):
                    self.assertEqual(final_events[event['event_id']], event)
        self.assertEqual(rows, original)
        self.assertEqual(replay[4], core.replay_harmonics(rows[:5], context={'security_id': 'ZZ-DEMO', 'timeframe': 'day'})[-1])

    def test_seeded_ohlc_replay_preserves_all_published_events(self):
        generator = random.Random(17)
        rows, price = [], 100
        for index in range(45):
            previous, price = price, max(3, price * (1 + generator.uniform(-0.3, 0.3)))
            rows.append({'time': stamp(index), 'open': previous, 'close': price,
                         'low': min(previous, price) * (1 - generator.random() * 0.04),
                         'high': max(previous, price) * (1 + generator.random() * 0.04)})
        snapshots = core.replay_harmonics(rows)
        final = snapshots[-1]
        final_events = {event['event_id']: event for event in final['events']}
        for snapshot in snapshots:
            self.assertNotEqual(snapshot['status'], 'invalid_input')
            self.assertEqual(snapshot['points'], final['points'][:len(snapshot['points'])])
            for event in snapshot['events']:
                self.assertEqual(event, final_events[event['event_id']])

    def test_timestamp_alias_and_serialization(self):
        rows = bars(prices(*SAMPLES['Gartley']) + [140])
        for row in rows:
            row['timestamp'] = row.pop('time') + 'T16:00:00+08:00'
        result = core.analyze_harmonics(rows)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(named(result, 'patterns', 'Gartley'))
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)


if __name__ == '__main__':
    unittest.main()
