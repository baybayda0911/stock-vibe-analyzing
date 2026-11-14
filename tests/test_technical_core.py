from copy import deepcopy
from datetime import date, timedelta
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/stock-technical-analysis/scripts/technical_core.py'
spec = importlib.util.spec_from_file_location('technical_core', SCRIPT)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
try:
    import numpy as np
    import talib
except ImportError:
    np = talib = None


def stamp(index):
    return (date(2025, 1, 1) + timedelta(days=index)).isoformat() + 'T16:00:00+08:00'


def bars(prices):
    return [{'time': stamp(i), 'open': price-0.5, 'high': price+1, 'low': price-1,
             'close': price, 'volume': 100.0, 'completed': True} for i, price in enumerate(prices)]


def analyze(rows, **kwargs):
    return core.analyze_technical(rows, profile={'candlestick_backend': 'project-limited-v1'},
                                  include_harmonic=False, **kwargs)


class TechnicalCoreTests(unittest.TestCase):
    def test_standard_library_import_and_missing_dependencies_are_explicit(self):
        command = ('import importlib.util,json;'
                   's=importlib.util.spec_from_file_location("t",' + repr(str(SCRIPT)) + ');'
                   'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                   'r=m.analyze_technical([dict(time="2025-01-01",open=10,high=11,low=9,close=10)],'
                   'include_harmonic=False,profile={"candlestick_backend":"talib"});'
                   'assert r["topics"]["conventional"]["status"]=="dependency_missing";'
                   'assert r["topics"]["candlestick"]["status"]=="dependency_missing";'
                   'assert r["topics"]["harmonic"]["status"]=="not_run";print("ok")')
        completed = subprocess.run([sys.executable, '-I', '-S', '-c', command], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), 'ok')

    def test_bad_values_profiles_and_future_time_contract(self):
        for changes in ({'close': True}, {'close': 0}, {'high': float('inf')}, {'volume': -1},
                        {'low': 101}, {'time': 'unknown'}, {'available_at': 'unknown'}):
            items = bars([100, 101])
            items[0].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(analyze(items)['status'], 'invalid_input')
        for profile in ({'version': 'unsupported'}, {'smc_swing_radius': True}, {'smc_swing_radius': 0},
                        {'harmonic_relative_tolerance': 0.11}, {'extra': 1}):
            self.assertEqual(core.analyze_technical(bars([100]), profile=profile)['status'], 'invalid_input')
        duplicate = bars([100, 101])
        duplicate[1]['time'] = duplicate[0]['time']
        self.assertEqual(analyze(duplicate)['status'], 'invalid_input')
        self.assertEqual(analyze([])['status'], 'insufficient_history')

    def test_availability_filters_future_tail_without_compressing_internal_gap(self):
        items = bars([100, 101, 102])
        items[2]['available_at'] = stamp(5)
        result = analyze(items, asof=stamp(3))
        self.assertEqual(len(result['bars']), 2)
        self.assertEqual(result['metadata']['excluded_count'], 1)
        items[1]['available_at'] = stamp(6)
        items[2]['available_at'] = stamp(2)
        blocked = analyze(items, asof=stamp(3))
        self.assertEqual(blocked['status'], 'invalid_input')
        self.assertIn('compress', blocked['limitations'][0])

    @unittest.skipIf(talib is None, 'TA-Lib is an optional runtime dependency')
    def test_talib_primary_values_and_warmup_are_numerically_correct(self):
        items = bars(range(100, 260))
        result = analyze(items)
        conventional = result['topics']['conventional']
        self.assertEqual(conventional['backend'], 'talib')
        self.assertEqual(conventional['backend_version'], talib.__version__)
        series = result['series']
        self.assertIsNone(series['sma20'][18])
        self.assertAlmostEqual(series['sma20'][19], 109.5)
        self.assertAlmostEqual(series['ema12'][11], 105.5)
        self.assertIsNone(series['rsi14'][13])
        self.assertEqual(series['rsi14'][14], 100)
        self.assertIsNone(series['atr14'][13])
        self.assertAlmostEqual(series['atr14'][14], 2)
        self.assertIsNone(series['macd'][32])
        self.assertAlmostEqual(series['macd'][33], 7)
        self.assertAlmostEqual(series['macd_signal'][33], 7)
        self.assertAlmostEqual(series['macd_hist'][33], 0)
        self.assertEqual(series['obv'][0], 100)
        self.assertEqual(series['obv'][-1], 16000)
        close = np.asarray([row['close'] for row in items], dtype='float64')
        expected = talib.MACD(close)
        for key, values in zip(('macd', 'macd_signal', 'macd_hist'), expected):
            for actual, expected_value in zip(series[key], values):
                self.assertEqual(actual is None, not np.isfinite(expected_value))
                if actual is not None:
                    self.assertAlmostEqual(actual, expected_value, places=12)

    def test_missing_talib_does_not_switch_primary_formulas(self):
        with patch.dict(sys.modules, {'talib': None}):
            result = analyze(bars(range(100, 180)))
        self.assertEqual(result['topics']['conventional']['status'], 'dependency_missing')
        self.assertTrue(all(value is None for value in result['series']['sma20']))
        trend = [fact for fact in result['facts'] if fact['metric'] == 'close_and_sma20_above_sma60'][0]
        self.assertEqual(trend['relation'], 'not_run')
        self.assertAlmostEqual(result['series']['price_change'][1], 0.01)
        self.assertEqual(result['topics']['ichimoku']['status'], 'ok')
        self.assertEqual(result['topics']['turtle']['status'], 'ok')

    def test_volume_ratio_excludes_current_and_missing_volume_is_not_zero(self):
        items = bars(range(100, 125))
        items[20]['volume'] = 10000
        result = analyze(items)
        self.assertEqual(result['series']['volume_ratio20'][20], 100)
        items[19]['volume'] = None
        missing = analyze(items)
        self.assertIsNone(missing['series']['volume_ratio20'][20])
        self.assertIsNone(missing['series']['obv'][20])
        if talib is not None:
            before = analyze(items[:19])
            self.assertEqual(missing['series']['obv'][:19], before['series']['obv'])

    def test_ichimoku_known_source_and_plot_displacement_are_separate(self):
        result = analyze(bars(range(100, 180)))['topics']['ichimoku']
        series = result['series']
        self.assertAlmostEqual(series['tenkan9'][8], 104)
        self.assertAlmostEqual(series['kijun26'][25], 112.5)
        self.assertAlmostEqual(series['span_a_computed'][25], 116.75)
        self.assertIsNone(series['cloud_a_visible'][50])
        self.assertAlmostEqual(series['cloud_a_visible'][51], 116.75)
        self.assertAlmostEqual(series['cloud_b_visible'][77], 125.5)
        projected = [item for item in result['plot'] if item['line'] == 'span_b' and item['source_index'] == 79][0]
        self.assertEqual(projected['plot_index'], 105)
        self.assertIsNone(projected['plot_time'])
        lagging = [item for item in result['plot'] if item['line'] == 'chikou' and item['source_index'] == 79][0]
        self.assertEqual(lagging['plot_index'], 53)
        self.assertEqual(lagging['first_known_at'], projected['first_known_at'])
        self.assertGreater(core._moment(lagging['first_known_at']), core._moment(lagging['plot_time']))

    def test_turtle_prior_channels_n_and_completed_close_conditions(self):
        items = bars([100] * 55 + [150])
        result = analyze(items)['topics']['turtle']
        self.assertEqual(result['series']['entry_high20'][55], 101)
        self.assertEqual(result['series']['entry_high55'][55], 101)
        self.assertTrue(result['latest']['long_entry20'])
        self.assertTrue(result['latest']['long_entry55'])
        self.assertIsNone(result['series']['n20'][19])
        self.assertEqual(result['series']['n20'][20], 2)
        self.assertAlmostEqual(result['latest']['n20'], (2*19+51)/20)
        items[-1].update(close=100, open=100, high=150, low=99)
        touch_only = analyze(items)['topics']['turtle']
        self.assertFalse(touch_only['latest']['long_entry20'])
        self.assertFalse(touch_only['latest']['long_entry55'])
        self.assertEqual(touch_only['latest']['two_n_distance'], 2 * touch_only['latest']['n20'])

    @unittest.skipIf(talib is None, 'TA-Lib is an optional runtime dependency')
    def test_turtle_wilder_n_matches_talib_atr20(self):
        items = bars([100 + (i % 9)*2 + i/10 for i in range(100)])
        result = analyze(items)['topics']['turtle']['series']['n20']
        expected = talib.ATR(*(np.asarray([bar[k] for bar in items], dtype='float64') for k in ('high', 'low', 'close')), timeperiod=20)
        for actual, value in zip(result, expected):
            self.assertEqual(actual is None, not math.isfinite(value))
            if actual is not None:
                self.assertAlmostEqual(actual, value, places=12)

    def test_finite_candles_have_declared_scope_and_zero_range_is_not_doji(self):
        items = bars([10, 10])
        items[0].update(open=10, high=10, low=10, close=10)
        items[1].update(open=10, high=12, low=8, close=10)
        result = analyze(items)['topics']['candlestick']
        self.assertEqual(len(result['supported_patterns']), 5)
        self.assertFalse([event for event in result['events'] if event['index'] == 0])
        doji = [event for event in result['latest'] if event['pattern'] == 'doji'][0]
        self.assertEqual(doji['direction'], 'neutral')
        self.assertEqual(doji['raw_score'], 0)
        self.assertIn('no prior-trend filter', result['definitions']['hammer'])

    @unittest.skipIf(talib is None, 'TA-Lib is an optional runtime dependency')
    def test_all_installed_talib_candle_functions_are_declared_and_run(self):
        items = bars([100 + (i % 7) for i in range(100)])
        result = core.analyze_technical(items, include_harmonic=False, profile={'candlestick_backend': 'talib'})
        topic = result['topics']['candlestick']
        self.assertEqual(topic['backend'], 'talib')
        self.assertEqual(topic['supported_patterns'], sorted(talib.get_function_groups()['Pattern Recognition']))
        arrays = [np.asarray([bar[key] for bar in items], dtype='float64') for key in ('open','high','low','close')]
        scores = talib.CDLDOJI(*arrays)
        actual = {event['index']: event['raw_score'] for event in topic['events'] if event['pattern'] == 'CDLDOJI'}
        self.assertEqual(actual, {i: int(value) for i, value in enumerate(scores) if value})
        short = core.analyze_technical(items[:1], include_harmonic=False, profile={'candlestick_backend':'talib'})['topics']['candlestick']
        self.assertEqual(short['status'], 'insufficient_history')
        self.assertEqual(short['eligible_patterns'], [])
        self.assertTrue(short['lookbacks'])

    def test_smc_swings_bos_choch_are_delayed_and_causal(self):
        items = bars([100, 102, 109, 104, 103, 112, 108, 100, 105, 106, 96])
        before = analyze(items[:4])['topics']['smc']
        self.assertFalse([point for point in before['swings'] if point['index'] == 2])
        at_confirmation = analyze(items[:5])['topics']['smc']
        high = [point for point in at_confirmation['swings'] if point['index'] == 2 and point['kind'] == 'H'][0]
        self.assertEqual(high['confirmed_index'], 4)
        self.assertLess(high['occurred_at'], high['confirmed_at'])
        result = analyze(items)['topics']['smc']
        breaks = [event for event in result['events'] if event['type'] in ('BOS', 'CHoCH')]
        self.assertEqual((breaks[0]['type'], breaks[0]['direction'], breaks[0]['index']), ('BOS','bullish',5))
        self.assertEqual((breaks[1]['type'], breaks[1]['direction'], breaks[1]['index']), ('CHoCH','bearish',7))
        self.assertEqual((breaks[-1]['type'], breaks[-1]['direction'], breaks[-1]['index']), ('BOS','bearish',10))
        self.assertIn('not evidence of institutions', result['definitions']['interpretation'])

    def test_order_block_is_known_at_break_not_backfilled_to_source_candle(self):
        items = bars([100,102,109,104,103,112,101])
        items[4].update(open=105, high=106, low=102, close=103)
        items[5].update(open=104, high=113, low=103, close=112)
        before = analyze(items[:5])['topics']['smc']
        self.assertFalse([zone for zone in before['zones'] if zone['kind'] == 'order_block'])
        confirmed = analyze(items[:6])['topics']['smc']
        block = [zone for zone in confirmed['zones'] if zone['kind'] == 'order_block'][0]
        self.assertEqual((block['source_index'], block['created_index']), (4,5))
        self.assertGreater(core._moment(block['first_known_at']), core._moment(block['occurred_at']))
        later = analyze(items)['topics']['smc']
        invalidated = [zone for zone in later['zones'] if zone['zone_id'] == block['zone_id']][0]
        self.assertEqual(invalidated['status'], 'invalidated')
        self.assertEqual(core._moment(invalidated['invalidated_at']), core._moment(stamp(6)))

    def fvg_rows(self):
        items = bars([10,12,15,13,11])
        items[0].update(open=10, high=11, low=9, close=10)
        items[1].update(open=12, high=13, low=11, close=12)
        items[2].update(open=15, high=16, low=14, close=15)
        items[3].update(open=13, high=15, low=12, close=13)
        items[4].update(open=11, high=12, low=10, close=11)
        return items

    def test_fvg_created_partial_filled_and_gapthrough_remain_distinct(self):
        items = self.fvg_rows()
        born = analyze(items[:3])['topics']['smc']
        zone = [zone for zone in born['zones'] if zone['created_index'] == 2][0]
        self.assertEqual((zone['lower'], zone['upper'], zone['status']), (11,14,'active'))
        self.assertIsNone(zone['filled_at'])
        touched = analyze(items[:4])['topics']['smc']
        self.assertEqual([z for z in touched['zones'] if z['zone_id']==zone['zone_id']][0]['status'], 'partial')
        filled = analyze(items)['topics']['smc']
        self.assertEqual([z for z in filled['zones'] if z['zone_id']==zone['zone_id']][0]['status'], 'filled')
        items[3].update(open=9, high=10, low=8, close=9)
        skipped = analyze(items[:4])['topics']['smc']
        gapped = [z for z in skipped['zones'] if z['zone_id']==zone['zone_id']][0]
        self.assertEqual(gapped['status'], 'gapped_through')
        self.assertIsNone(gapped['filled_at'])
        self.assertIsNone(gapped['touched_at'])

    def test_explicit_mechanical_and_session_boundaries_exclude_fvg_and_order_block(self):
        for declaration in ('mechanical_gap', 'session_id'):
            with self.subTest(declaration=declaration):
                items = self.fvg_rows()[:3]
                if declaration == 'mechanical_gap':
                    items[1]['mechanical_gap'] = True
                else:
                    for i, item in enumerate(items):
                        item['session_id'] = 'AM' if i < 2 else 'PM'
                result = analyze(items)
                self.assertIn(declaration, result['bars'][1 if declaration == 'mechanical_gap' else 2])
                smc = result['topics']['smc']
                self.assertFalse([zone for zone in smc['zones'] if zone['kind']=='FVG'])
                exclusions = [event for event in smc['events'] if event['type']=='fvg_excluded']
                self.assertEqual(len(exclusions), 1)
                self.assertIn('boundary', exclusions[0]['reason'])
        items = bars([100,102,109,104,103,112])
        items[4].update(open=105, high=106, low=102, close=103)
        items[5].update(open=104, high=113, low=103, close=112, mechanical_gap=True)
        result = analyze(items)['topics']['smc']
        self.assertTrue([event for event in result['events'] if event['type']=='BOS'])
        self.assertFalse([zone for zone in result['zones'] if zone['kind']=='order_block'])
        self.assertTrue([event for event in result['events'] if event['type']=='order_block_excluded'])
        items[5]['mechanical_gap'] = 'yes'
        self.assertEqual(analyze(items)['status'], 'invalid_input')

    def test_delayed_availability_never_becomes_an_earlier_structure_date(self):
        items = self.fvg_rows()
        items[0]['available_at'] = stamp(8)
        result = analyze(items, asof=stamp(9))
        smc = result['topics']['smc']
        for event in smc['events']:
            self.assertGreaterEqual(core._moment(event['first_known_at']), core._moment(stamp(8)))
        for zone in smc['zones']:
            for field in ('first_known_at','touched_at','filled_at','invalidated_at'):
                if zone[field] is not None:
                    self.assertGreaterEqual(core._moment(zone[field]), core._moment(stamp(8)))

    def test_relative_strength_common_window_qualifications_and_no_forward_fill(self):
        items = bars([100,105,110])
        reference = [{'time': stamp(0), 'close':200}, {'time': stamp(2), 'close':210}]
        missing = analyze(items)['topics']['conventional']['relative_strength']
        self.assertEqual(missing['status'], 'missing')
        unqualified = analyze(items, benchmark=reference)['topics']['conventional']['relative_strength']
        self.assertEqual(unqualified['status'], 'not_comparable')
        qualified = {'bars':reference,'benchmark_id':'ZZ-INDEX','comparable':True,'comparison_basis':'same currency/price basis and frozen common dates'}
        result = analyze(items, benchmark=qualified)['topics']['conventional']['relative_strength']
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['common_dates'], [core._stamp(core._moment(stamp(i))) for i in (0,2)])
        self.assertIsNone(result['series']['relative_index'][1])
        self.assertAlmostEqual(result['latest']['relative_index'], 110/100/(210/200)*100)
        self.assertEqual(len(result['facts'][0]['source_groups']), 2)

    def test_harmonic_legacy_geometry_and_actual_availability_are_both_preserved(self):
        values = [100,200,138.2,183.2,121.4,140]
        items = [{'time':stamp(i),'open':p,'high':p,'low':p,'close':p,'available_at':stamp(9)} for i,p in enumerate(values)]
        result = core.analyze_technical(items, asof=stamp(10), profile={'candlestick_backend':'project-limited-v1'})
        harmonic = result['topics']['harmonic']
        pattern = [item for item in harmonic['analysis']['patterns'] if item['pattern']=='Gartley'][0]
        self.assertLess(core._moment(pattern['confirmed_at']), core._moment(stamp(9)))
        events = [event for event in harmonic['availability_events'] if event['type']=='geometry_confirmed']
        self.assertTrue(events)
        self.assertTrue(all(core._moment(event['at']) == core._moment(stamp(9)) for event in events))
        self.assertEqual(core._moment(harmonic['facts'][0]['first_known_at']), core._moment(stamp(9)))
        other = core.analyze_technical(items, asof=stamp(10), context={'source_group':'another-snapshot'},
                                       profile={'candlestick_backend':'project-limited-v1'})
        second = [item for item in other['topics']['harmonic']['analysis']['patterns'] if item['pattern']=='Gartley'][0]
        self.assertEqual(pattern['candidate_id'], second['candidate_id'])

    def test_prefixes_keep_numeric_history_and_structure_events(self):
        items = bars([100,102,109,104,103,112,108,100,105,106,96,101,110,111,105])
        original = deepcopy(items)
        snapshots = core.replay_technical(items, include_harmonic=False, profile={'candlestick_backend':'project-limited-v1'})
        final = snapshots[-1]
        final_events = {event['event_id']: event for event in final['topics']['smc']['events']}
        for end, result in enumerate(snapshots, 1):
            self.assertNotEqual(result['status'], 'invalid_input')
            for name, series in result['series'].items():
                self.assertEqual(series, final['series'][name][:end])
            for event in result['topics']['smc']['events']:
                self.assertEqual(event, final_events[event['event_id']])
                self.assertLessEqual(core._moment(event['first_known_at']), core._moment(items[end-1]['time']))
        self.assertEqual(items, original)

    def test_facts_preserve_same_source_group_and_have_explicit_falsification(self):
        result = analyze(bars(range(100,180)), context={'security_id':'ZZ-DEMO','timeframe':'1d','adjustment':'qfq'})
        self.assertEqual(set(result['topics']), set(core.TOPICS))
        self.assertTrue(result['facts'])
        self.assertEqual({fact['source_group'] for fact in result['facts']}, {result['metadata']['source_group']})
        self.assertTrue(all(fact['falsification'] and fact['first_known_at'] for fact in result['facts']))
        self.assertEqual(len(result['metadata']['input_sha256']), 64)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_explicit_parent_source_group_survives_timeframe_derivation(self):
        items = bars(range(100,180))
        daily = analyze(items, context={'timeframe':'1d','source_group':'parent-day-file:abc'})
        weekly = analyze(items, context={'timeframe':'1w','source_group':'parent-day-file:abc'})
        self.assertEqual(daily['metadata']['source_group'], weekly['metadata']['source_group'])
        self.assertEqual({fact['source_group'] for fact in weekly['facts']}, {'parent-day-file:abc'})
        self.assertEqual(analyze(items, context={'source_group':' '})['status'], 'invalid_input')

    def test_close_only_is_partial_and_never_constructs_ohlc_or_atr(self):
        rows = [{'time': stamp(i), 'close': 100+i} for i in range(90)]
        result = core.analyze_close_prices(rows)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['metadata']['price_only'])
        self.assertTrue(all(not ({'open','high','low','volume'} & set(row)) for row in result['bars']))
        self.assertTrue(all(value is None for value in result['series']['atr14']))
        self.assertTrue(all(value is None for value in result['series']['volume_ratio20']))
        self.assertEqual({result['topics'][name]['status'] for name in core.TOPICS[1:]}, {'missing_fields'})
        if talib is not None:
            self.assertAlmostEqual(result['series']['sma20'][-1], 179.5)
            self.assertAlmostEqual(result['series']['macd'][-1], 7)
            self.assertEqual(result['series']['rsi14'][-1], 100)

    def test_close_only_future_availability_and_bad_values(self):
        rows = [{'time':stamp(0),'close':100}, {'time':stamp(1),'close':101,'available_at':stamp(3)}]
        result = core.analyze_close_prices(rows, asof=stamp(2))
        self.assertEqual(result['metadata']['used_count'], 1)
        self.assertEqual(result['metadata']['excluded_count'], 1)
        for bad in (None, True, -1, float('nan')):
            with self.subTest(bad=bad):
                self.assertEqual(core.analyze_close_prices([{'time':stamp(0),'close':bad}])['status'], 'invalid_input')


if __name__ == '__main__':
    unittest.main()
