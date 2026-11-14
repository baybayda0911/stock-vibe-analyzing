"""Six finite technical research topics on a frozen, completed OHLC(V) prefix.

Import and the numerical core use only the standard library. TA-Lib candlestick
recognition is loaded on demand; its absence never fabricates an empty signal.
No data fetching, market aggregation, orders, position inference or P&L lives here.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path


VERSION = '1.0.0'
DEFAULT_PROFILE = {'profile_id': 'project-technical-v1', 'version': VERSION,
                   'candlestick_backend': 'auto', 'smc_swing_radius': 2,
                   'smc_order_block_lookback': 20,
                   'harmonic_reversal_threshold': 0.05, 'harmonic_relative_tolerance': 0.05}
TOPICS = ('conventional', 'candlestick', 'ichimoku', 'harmonic', 'smc', 'turtle')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:24]


def _moment(value):
    if not isinstance(value, str) or not value:
        raise ValueError('time must be an ISO date or timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('time must be an ISO date or timestamp') from error


def _stamp(moment):
    return moment.astimezone(timezone.utc).isoformat() if moment.tzinfo is not None else moment.isoformat()


def _max_time(*values):
    return max(values, key=_moment)


def _finite(value, name, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    if value < 0 or (positive and value == 0):
        raise ValueError(name + (' must be positive' if positive else ' cannot be negative'))
    return float(value)


def _profile(profile):
    result = dict(DEFAULT_PROFILE)
    if profile is not None:
        if not isinstance(profile, dict) or set(profile) - set(result):
            raise ValueError('unsupported profile fields')
        result.update(profile)
    if (result['profile_id'], result['version']) != ('project-technical-v1', VERSION):
        raise ValueError('unsupported technical profile version')
    if result['candlestick_backend'] not in ('auto', 'talib', 'project-limited-v1'):
        raise ValueError('unsupported candlestick backend')
    for name in ('smc_swing_radius', 'smc_order_block_lookback'):
        if isinstance(result[name], bool) or not isinstance(result[name], int) or not 1 <= result[name] <= 100:
            raise ValueError(name + ' must be an integer between 1 and 100')
    threshold = _finite(result['harmonic_reversal_threshold'], 'harmonic reversal threshold')
    tolerance = _finite(result['harmonic_relative_tolerance'], 'harmonic tolerance', positive=False)
    if threshold >= 1 or tolerance > 0.1:
        raise ValueError('harmonic threshold must be below 1 and tolerance at most 0.1')
    return result


def _normalize(rows, asof=None, *, close_only=False):
    if not isinstance(rows, list):
        raise ValueError('bars must be a list')
    cutoff = _moment(asof) if asof is not None else None
    normalized, previous, excluded, interrupted, prefix_known = [], None, 0, False, None
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError('each bar must be an object')
        time = _moment(row.get('time', row.get('timestamp')))
        available = _moment(row.get('available_at', row.get('time', row.get('timestamp'))))
        if previous is not None and time <= previous:
            raise ValueError('bar times must strictly increase')
        previous = time
        known = max(time, available)
        if row.get('completed', True) is not True or (cutoff is not None and known > cutoff):
            excluded += 1
            interrupted = True
            continue
        if interrupted:
            raise ValueError('unavailable or unfinished bar inside the usable prefix; do not compress a missing interval')
        prefix_known = known if prefix_known is None else max(prefix_known, known)
        item = {'index': len(normalized), 'source_index': index, 'time': _stamp(time),
                'available_at': _stamp(available), 'first_known_at': _stamp(known),
                'prefix_known_at': _stamp(prefix_known),
                'close': _finite(row.get('close'), 'close'), 'completed': True}
        if 'mechanical_gap' in row:
            if type(row['mechanical_gap']) is not bool:
                raise ValueError('mechanical_gap must be boolean when declared')
            item['mechanical_gap'] = row['mechanical_gap']
        if 'session_id' in row:
            if not isinstance(row['session_id'], str) or not row['session_id'].strip():
                raise ValueError('session_id must be a nonempty string when declared')
            item['session_id'] = row['session_id']
        if not close_only:
            item.update({name: _finite(row.get(name), name) for name in ('open', 'high', 'low')})
            if not item['low'] <= min(item['open'], item['close']) <= max(item['open'], item['close']) <= item['high']:
                raise ValueError('inconsistent OHLC')
            item['volume'] = None if row.get('volume') is None else _finite(row['volume'], 'volume', positive=False)
        normalized.append(item)
    return normalized, excluded


def sma(values, period):
    """Trailing arithmetic mean; any missing member makes that window missing."""
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError('period must be a positive integer')
    return [None if index + 1 < period or any(v is None for v in values[index-period+1:index+1])
            else sum(values[index-period+1:index+1]) / period for index in range(len(values))]


def _wilder(values, period):
    result, seed, previous = [], [], None
    for value in values:
        if value is None:
            seed, previous = [], None
        elif previous is None:
            seed.append(value)
            if len(seed) == period:
                previous = sum(seed) / period
        else:
            previous = (previous * (period - 1) + value) / period
        result.append(previous)
    return result


def atr(bars, period=14):
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError('period must be a positive integer')
    ranges = [None] + [max(bar['high'] - bar['low'], abs(bar['high'] - bars[index-1]['close']),
                          abs(bar['low'] - bars[index-1]['close']))
                       for index, bar in enumerate(bars) if index] if bars else []
    return _wilder(ranges, period)


def _topic(status='ok', **values):
    return {'status': status, 'limitations': [], **values}


def _source_group(context):
    if 'source_group' in context:
        if not isinstance(context['source_group'], str) or not context['source_group'].strip():
            raise ValueError('context.source_group must be a nonempty string')
        return context['source_group']
    return 'ohlcv:' + _digest([context.get('security_id'), context.get('timeframe'), context.get('adjustment')])


def _fact(topic, metric, value, bars, source_group, *, relation='neutral', proposition_id=None,
          definition='', falsification='Recompute on the next completed qualified bar; retain this snapshot.'):
    latest = bars[-1]
    result = {'topic': topic, 'metric': metric, 'value': value, 'time': latest['time'],
              'first_known_at': max((bar['first_known_at'] for bar in bars), key=_moment),
              'data_cutoff': latest['time'], 'source_group': source_group, 'relation': relation,
              'proposition_id': proposition_id, 'definition': definition, 'falsification': falsification}
    result['fact_id'] = _digest(result)
    return result


def _conventional(bars, group, *, close_only=False):
    close = [bar['close'] for bar in bars]
    keys = ('sma20', 'sma60', 'sma120', 'ema12', 'ema26', 'rsi14', 'atr14',
            'macd', 'macd_signal', 'macd_hist', 'obv')
    series = {name: [None] * len(bars) for name in keys}
    dependency, backend_version = None, None
    try:
        import numpy as np
        import talib
    except ImportError as error:
        dependency = str(error)
    else:
        backend_version = talib.__version__
        if talib.get_compatibility() != 0 or any(talib.get_unstable_period(name) != 0 for name in ('EMA', 'RSI', 'ATR')):
            raise ValueError('TA-Lib requires default compatibility0 and unstable periods0 for EMA/RSI/ATR')
        arrays = {key: np.asarray([bar[key] for bar in bars], dtype='float64')
                  for key in (('close',) if close_only else ('high', 'low', 'close'))}
        converted = lambda values: [float(value) if math.isfinite(value) else None for value in values]
        for period in (20, 60, 120):
            series['sma'+str(period)] = converted(talib.SMA(arrays['close'], timeperiod=period))
        for period in (12, 26):
            series['ema'+str(period)] = converted(talib.EMA(arrays['close'], timeperiod=period))
        series['rsi14'] = converted(talib.RSI(arrays['close'], timeperiod=14))
        if not close_only:
            series['atr14'] = converted(talib.ATR(arrays['high'], arrays['low'], arrays['close'], timeperiod=14))
        for name, values in zip(('macd', 'macd_signal', 'macd_hist'),
                                talib.MACD(arrays['close'], fastperiod=12, slowperiod=26, signalperiod=9)):
            series[name] = converted(values)
        volume_end = next((i for i, bar in enumerate(bars) if bar.get('volume') is None), len(bars))
        if volume_end:
            series['obv'][:volume_end] = converted(talib.OBV(arrays['close'][:volume_end], np.asarray([bar['volume'] for bar in bars[:volume_end]], dtype='float64')))
    volume = []
    volume_ratio = []
    for index, bar in enumerate(bars):
        volume.append(bar.get('volume'))
        prior = volume[max(0, index-20):index]
        average = sum(prior) / 20 if len(prior) == 20 and all(v is not None for v in prior) else None
        volume_ratio.append(bar['volume'] / average if bar.get('volume') is not None and average not in (None, 0) else None)
    series.update(volume_ratio20=volume_ratio,
                  price_change=[None] + [close[i] / close[i-1] - 1 for i in range(1, len(close))] if close else [])
    result = _topic('dependency_missing' if dependency else 'ok', backend='talib', backend_version=backend_version, series=series,
                    latest={key: values[-1] if values else None for key, values in series.items()}, facts=[],
                    definitions={'sma': 'TA-Lib SMA20/60/120, default compatibility and unstable period0',
                                 'ema': 'TA-Lib EMA12/26, default compatibility and unstable period0',
                                 'macd': 'TA-Lib MACD(12,26,9), library warm-up masks retained; histogram not doubled',
                                 'rsi': 'TA-Lib RSI14, including its flat-series convention',
                                 'atr': 'TA-Lib ATR14, no invented previous close at index0',
                                 'volume_ratio20': 'current volume / mean of preceding20 volumes; no missing/zero fill',
                                 'obv': 'TA-Lib OBV, first-volume baseline; first missing volume ends continuity without rewriting the earlier prefix'})
    if dependency:
        result['limitations'].append('TA-Lib/numpy unavailable; primary indicators were not run: ' + dependency)
    if not bars:
        result['status'] = 'insufficient_history'
        return result
    latest = result['latest']
    trend = (close[-1] > latest['sma60'] and latest['sma20'] > latest['sma60']) if latest['sma60'] is not None else None
    result['facts'].append(_fact('conventional', 'close_and_sma20_above_sma60', trend, bars, group,
                                  relation='not_run' if dependency else 'missing' if trend is None else 'support' if trend else 'conflict',
                                  proposition_id='trend_continuation_long', definition='close>SMA60 and SMA20>SMA60',
                                  falsification='A later completed close or SMA20 at/below SMA60 negates this defined alignment.'))
    for metric in ('macd_hist', 'rsi14', 'atr14', 'volume_ratio20', 'obv'):
        result['facts'].append(_fact('conventional', metric, latest[metric], bars, group,
                                      relation='not_run' if dependency and metric != 'volume_ratio20' else 'missing' if latest[metric] is None else 'neutral',
                                      definition=result['definitions'].get(metric, result['definitions'].get(metric.split('_')[0], metric))))
    if any(bar.get('volume') is None for bar in bars):
        result['limitations'].append('volume data are missing; corresponding volume/OBV results remain missing, never zero-filled')
    if close_only:
        result['limitations'].append('close-only input: ATR, OHLC geometry and volume are not available; no open/high/low were fabricated')
        if not dependency:
            result['status'] = 'partial'
    if latest['sma60'] is None and not dependency:
        result['status'] = 'partial'
        result['limitations'].append('fewer than60 completed bars; the60-bar trend condition is missing')
    return result


def _relative_strength(bars, benchmark, asof, group):
    result = _topic('missing', series={'relative_index': [None] * len(bars),
                                      'stock_index': [None] * len(bars), 'benchmark_index': [None] * len(bars)},
                    common_dates=[], latest=None, facts=[])
    if benchmark is None:
        result['limitations'].append('no benchmark supplied')
        return result
    if not isinstance(benchmark, dict) or benchmark.get('comparable') is not True or not benchmark.get('comparison_basis') or not benchmark.get('benchmark_id'):
        result.update(status='not_comparable')
        result['limitations'].append('benchmark identity and explicit common currency/price-basis/window qualification are required')
        return result
    try:
        reference, excluded = _normalize(benchmark.get('bars'), asof, close_only=True)
    except (ValueError, TypeError, OverflowError) as error:
        result.update(status='invalid_input')
        result['limitations'].append(str(error))
        return result
    index = {bar['time']: bar for bar in reference}
    common = [(i, bar, index[bar['time']]) for i, bar in enumerate(bars) if bar['time'] in index]
    result.update(common_dates=[bar['time'] for _, bar, _ in common],
                  benchmark_id=benchmark['benchmark_id'], comparison_basis=benchmark['comparison_basis'], excluded_bars=excluded)
    if len(common) < 2:
        result['status'] = 'insufficient_history'
        result['limitations'].append('at least two exactly aligned common dates are required; no forward-fill')
        return result
    stock0, reference0 = common[0][1]['close'], common[0][2]['close']
    for i, stock, ref in common:
        stock_index, reference_index = stock['close'] / stock0 * 100, ref['close'] / reference0 * 100
        result['series']['stock_index'][i] = stock_index
        result['series']['benchmark_index'][i] = reference_index
        result['series']['relative_index'][i] = stock_index / reference_index * 100
    result['status'] = 'ok'
    latest = common[-1]
    result['latest'] = {'time': latest[1]['time'], 'relative_index': result['series']['relative_index'][latest[0]],
                        'first_known_at': _max_time(*(b['first_known_at'] for _, stock, ref in common for b in (stock, ref)))}
    fact = _fact('conventional', 'relative_index', result['latest']['relative_index'],
                 [stock for _, stock, _ in common], group,
                 relation='support' if result['latest']['relative_index'] > 100 else 'conflict' if result['latest']['relative_index'] < 100 else 'neutral',
                 proposition_id='relative_outperformance', definition='100*(stock/stock_at_common_start)/(benchmark/benchmark_at_common_start)',
                 falsification='The same frozen common-window relative index at/below100 negates outperformance; do not change the start after observing results.')
    fact['first_known_at'] = result['latest']['first_known_at']
    fact['source_groups'] = [group, 'benchmark:' + str(benchmark['benchmark_id'])]
    fact['fact_id'] = _digest({key: value for key, value in fact.items() if key != 'fact_id'})
    result['facts'].append(fact)
    return result


def _candle_event(name, score, index, bars, backend, group):
    result = {'pattern': name, 'raw_score': int(score), 'direction': 'bullish' if score > 0 else 'bearish' if score < 0 else 'neutral',
              'index': index, 'occurred_at': bars[index]['time'],
              'first_known_at': bars[index]['prefix_known_at'],
              'confirmed_at': bars[index]['prefix_known_at'],
              'backend': backend, 'source_group': group,
              'falsification': 'Pattern recognition is a past-bar geometry observation, not a future reversal forecast.'}
    result['event_id'] = _digest(result)
    return result


def _candlesticks(bars, backend, group):
    result = _topic(backend=backend, supported_patterns=[], events=[], latest=[], facts=[])
    if backend in ('auto', 'talib'):
        try:
            import numpy as np
            import talib
            from talib import abstract
        except ImportError:
            if backend == 'talib':
                result.update(status='dependency_missing', backend='talib')
                result['limitations'].append('TA-Lib/numpy unavailable; recognition was not run')
                return result
            result['limitations'].append('TA-Lib unavailable; only the explicitly listed finite project rules were run')
        else:
            names = sorted(talib.get_function_groups()['Pattern Recognition'])
            lookbacks = {name: int(abstract.Function(name).lookback) for name in names}
            arrays = [np.asarray([bar[key] for bar in bars], dtype='float64') for key in ('open', 'high', 'low', 'close')]
            result.update(backend='talib', backend_version=talib.__version__, supported_patterns=names,
                          lookbacks=lookbacks, eligible_patterns=[name for name in names if len(bars) > lookbacks[name]])
            for name in names:
                scores = getattr(talib, name)(*arrays)
                result['events'].extend(_candle_event(name, score, index, bars, 'talib', group)
                                        for index, score in enumerate(scores) if score)
    if result['backend'] != 'talib':
        result.update(backend='project-limited-v1', supported_patterns=['doji', 'bullish_engulfing', 'bearish_engulfing', 'hammer', 'shooting_star'],
                      lookbacks={'doji':0,'bullish_engulfing':1,'bearish_engulfing':1,'hammer':0,'shooting_star':0},
                      eligible_patterns=['doji','hammer','shooting_star'] + (['bullish_engulfing','bearish_engulfing'] if len(bars)>1 else []),
                      definitions={'doji': 'positive range; body<=10% of range; neutral geometry',
                                   'engulfing': 'opposite nonzero bodies; current real body inclusively covers prior body and strictly enlarges at least one edge',
                                   'hammer': 'nonzero body<=35% range; lower wick>=2*body; upper wick<=body; no prior-trend filter',
                                   'shooting_star': 'nonzero body<=35% range; upper wick>=2*body; lower wick<=body; no prior-trend filter'})
        for i, bar in enumerate(bars):
            body, span = abs(bar['close'] - bar['open']), bar['high'] - bar['low']
            if span == 0:
                continue
            patterns = []
            if body <= span * 0.1:
                patterns.append(('doji', 1))
            lower = min(bar['open'], bar['close']) - bar['low']
            upper = bar['high'] - max(bar['open'], bar['close'])
            if 0 < body <= span * 0.35 and lower >= 2 * body and upper <= body:
                patterns.append(('hammer', 100))
            if 0 < body <= span * 0.35 and upper >= 2 * body and lower <= body:
                patterns.append(('shooting_star', -100))
            if i:
                before = bars[i-1]
                if (bar['close'] - bar['open']) * (before['close'] - before['open']) < 0:
                    lo, hi = min(bar['open'], bar['close']), max(bar['open'], bar['close'])
                    old_lo, old_hi = min(before['open'], before['close']), max(before['open'], before['close'])
                    if lo <= old_lo and hi >= old_hi and (lo < old_lo or hi > old_hi):
                        patterns.append(('bullish_engulfing' if bar['close'] > bar['open'] else 'bearish_engulfing',
                                         100 if bar['close'] > bar['open'] else -100))
            for name, score in patterns:
                event = _candle_event(name, score, i, bars, 'project-limited-v1', group)
                if name == 'doji':
                    event.update(direction='neutral', raw_score=0)
                    event['event_id'] = _digest({k: v for k, v in event.items() if k != 'event_id'})
                result['events'].append(event)
    if bars:
        result['latest'] = [event for event in result['events'] if event['index'] == len(bars)-1]
        result['facts'].append(_fact('candlestick', 'recognized_latest_patterns',
                                      [{'pattern': item['pattern'], 'direction': item['direction'], 'raw_score': item['raw_score']} for item in result['latest']],
                                      bars, group, relation='neutral' if result['eligible_patterns'] else 'missing',
                                      definition='Recognized current-bar candle geometry under the recorded backend; raw scores are not probabilities.',
                                      falsification='This past-bar label is revised only if the frozen OHLC is revised; future reaction requires a separate condition.'))
    result['status'] = ('insufficient_history' if not result['eligible_patterns'] else
                        'partial' if len(result['eligible_patterns']) < len(result['supported_patterns']) else
                        'ok' if result['events'] else 'no_candidates')
    return result


def _midpoint(bars, period):
    return [None if i+1 < period else (max(bar['high'] for bar in bars[i-period+1:i+1]) +
                                      min(bar['low'] for bar in bars[i-period+1:i+1])) / 2 for i in range(len(bars))]


def _ichimoku(bars, group):
    tenkan, kijun, span_b = _midpoint(bars, 9), _midpoint(bars, 26), _midpoint(bars, 52)
    span_a = [None if a is None or b is None else (a+b)/2 for a, b in zip(tenkan, kijun)]
    visible_a = [span_a[i-26] if i >= 26 else None for i in range(len(bars))]
    visible_b = [span_b[i-26] if i >= 26 else None for i in range(len(bars))]
    series = {'tenkan9': tenkan, 'kijun26': kijun, 'span_a_computed': span_a,
              'span_b_computed': span_b, 'cloud_a_visible': visible_a, 'cloud_b_visible': visible_b,
              'chikou_computed': [bar['close'] for bar in bars]}
    plot = []
    for name, values, shift in (('span_a', span_a, 26), ('span_b', span_b, 26),
                                 ('chikou', series['chikou_computed'], -26)):
        for i, value in enumerate(values):
            if value is not None:
                destination = i + shift
                plot.append({'line': name, 'value': value, 'source_index': i, 'plot_index': destination,
                             'plot_time': bars[destination]['time'] if 0 <= destination < len(bars) else None,
                             'first_known_at': bars[i]['prefix_known_at']})
    result = _topic('ok' if bars and visible_b[-1] is not None else 'insufficient_history',
                    backend='project-ichimoku-9-26-52-v1', series=series, plot=plot,
                    latest={key: values[-1] if values else None for key, values in series.items()}, facts=[],
                    definitions={'forward_spans': 'computed at source index, plotted26 bars ahead; no future timestamp is invented',
                                 'current_cloud': 'use spans computed26 bars earlier, never the current projected cloud',
                                 'chikou': 'current close plotted26 bars back; it remains known at its source current bar'})
    if bars:
        above = bars[-1]['close'] > max(visible_a[-1], visible_b[-1]) if visible_b[-1] is not None else None
        result['facts'].append(_fact('ichimoku', 'close_above_visible_cloud', above, bars, group,
                                      relation='missing' if above is None else 'support' if above else 'conflict',
                                      proposition_id='trend_continuation_long', definition=result['definitions']['current_cloud'],
                                      falsification='A later completed close at/below the upper edge negates the defined above-cloud condition.'))
    if result['status'] != 'ok':
        result['limitations'].append('78 completed bars are required for a current visible52-bar cloud with26-bar displacement')
    return result


def _smc_event(kind, direction, index, bars, group, **extra):
    event = {'type': kind, 'direction': direction, 'index': index, 'occurred_at': bars[index]['time'],
             'first_known_at': bars[index]['prefix_known_at'],
             'confirmed_at': bars[index]['prefix_known_at'],
             'source_group': group, **extra}
    event['event_id'] = _digest(event)
    return event


def _update_zones(zones, events, index, bars, group):
    bar = bars[index]
    known = bar['prefix_known_at']
    for zone in zones:
        if zone['created_index'] >= index or zone['status'] in ('filled', 'gapped_through', 'invalidated'):
            continue
        bull = zone['direction'] == 'bullish'
        overlap = bar['high'] >= zone['lower'] and bar['low'] <= zone['upper']
        if overlap and zone['touched_at'] is None:
            zone.update(touched_at=known, status='partial' if zone['kind'] == 'FVG' else 'mitigated')
            events.append(_smc_event(zone['kind'].lower()+'_touched', zone['direction'], index, bars, group, zone_id=zone['zone_id'], path_order='unknown within OHLC bar'))
        if zone['kind'] == 'FVG':
            far = zone['lower'] if bull else zone['upper']
            reached = bar['low'] <= far <= bar['high']
            gapped = bar['high'] < far if bull else bar['low'] > far
            if reached or gapped:
                state = 'filled' if reached else 'gapped_through'
                zone.update(status=state, filled_at=known if reached else None,
                            gapped_through_at=known if gapped else None)
                events.append(_smc_event('fvg_'+state, zone['direction'], index, bars, group, zone_id=zone['zone_id'],
                                          path_order='range observation only; no inferred intrabar trade path'))
        elif (bar['close'] < zone['lower'] if bull else bar['close'] > zone['upper']):
            zone.update(status='invalidated', invalidated_at=known)
            events.append(_smc_event('order_block_invalidated', zone['direction'], index, bars, group, zone_id=zone['zone_id']))


def _zone(kind, direction, lower, upper, source_index, created_index, bars, group):
    value = {'kind': kind, 'direction': direction, 'lower': lower, 'upper': upper,
             'source_index': source_index, 'occurred_at': bars[source_index]['time'],
             'created_index': created_index,
             'first_known_at': bars[created_index]['prefix_known_at'],
             'status': 'active', 'touched_at': None, 'filled_at': None, 'invalidated_at': None,
             'gapped_through_at': None, 'source_group': group}
    value['zone_id'] = _digest(value)
    return value


def _gap_exclusion(bars, start, end):
    boundaries = [i for i in range(start+1, end+1) if bars[i].get('mechanical_gap') is True]
    sessions = sorted({bar['session_id'] for bar in bars[start:end+1] if 'session_id' in bar})
    if boundaries or len(sessions) > 1:
        return {'reason': 'declared mechanical gap or explicit session boundary; no imbalance/order-block attribution',
                'mechanical_boundary_indices': boundaries, 'session_ids': sessions}
    return None


def _smc(bars, profile, group):
    radius, lookback = profile['smc_swing_radius'], profile['smc_order_block_lookback']
    events, zones, swings, levels = [], [], [], {'H': None, 'L': None}
    broken, regime = set(), 0
    for i, bar in enumerate(bars):
        _update_zones(zones, events, i, bars, group)
        center = i - radius
        if center >= radius:
            neighbours = bars[center-radius:center] + bars[center+1:i+1]
            for kind, key, compare in (('H', 'high', lambda x, y: x > y), ('L', 'low', lambda x, y: x < y)):
                if all(compare(bars[center][key], other[key]) for other in neighbours):
                    point = {'kind': kind, 'price': bars[center][key], 'index': center,
                             'occurred_at': bars[center]['time'], 'confirmed_index': i,
                             'confirmed_at': bar['prefix_known_at']}
                    point['point_id'] = _digest([group, radius, kind, center, point['price']])
                    swings.append(point)
                    levels[kind] = point
                    events.append(_smc_event('swing_confirmed', 'high' if kind == 'H' else 'low', i, bars, group, swing=deepcopy(point)))
        for kind, direction in (('H', 1), ('L', -1)):
            point = levels[kind]
            crossed = point is not None and (bar['close'] > point['price'] if direction == 1 else bar['close'] < point['price'])
            if not crossed or point['point_id'] in broken:
                continue
            label = 'CHoCH' if regime and regime != direction else 'BOS'
            regime = direction
            broken.add(point['point_id'])
            name = 'bullish' if direction == 1 else 'bearish'
            events.append(_smc_event(label, name, i, bars, group, level=point['price'], swing_id=point['point_id'],
                                     falsification='A later opposite closing break changes this finite structural regime; no institutional flow is inferred.'))
            for j in range(i-1, max(-1, i-lookback-1), -1):
                opposite = bars[j]['close'] < bars[j]['open'] if direction == 1 else bars[j]['close'] > bars[j]['open']
                if opposite:
                    excluded = _gap_exclusion(bars, j, i)
                    if excluded:
                        events.append(_smc_event('order_block_excluded', name, i, bars, group, source_index=j, **excluded))
                        break
                    zone = _zone('order_block', name, bars[j]['low'], bars[j]['high'], j, i, bars, group)
                    zones.append(zone)
                    events.append(_smc_event('order_block_created', name, i, bars, group, zone_id=zone['zone_id'], source_index=j))
                    break
        if i >= 2:
            if bar['low'] > bars[i-2]['high']:
                direction, lower, upper = 'bullish', bars[i-2]['high'], bar['low']
            elif bar['high'] < bars[i-2]['low']:
                direction, lower, upper = 'bearish', bar['high'], bars[i-2]['low']
            else:
                continue
            excluded = _gap_exclusion(bars, i-2, i)
            if excluded:
                events.append(_smc_event('fvg_excluded', direction, i, bars, group, source_index=i-2, **excluded))
                continue
            zone = _zone('FVG', direction, lower, upper, i-2, i, bars, group)
            zones.append(zone)
            events.append(_smc_event('fvg_created', direction, i, bars, group, zone_id=zone['zone_id'], lower=lower, upper=upper))
    result = _topic('ok' if events else 'no_candidates', backend='project-smc-finite-v1', swings=swings,
                   events=events, zones=zones, latest_regime='bullish' if regime == 1 else 'bearish' if regime == -1 else 'undetermined',
                   definitions={'swing': f'strict center extreme with{radius} bars each side; confirmed only after right bars complete; ties omitted',
                                'BOS_CHoCH': 'close strictly breaks latest confirmed high/low once; opposite last-break direction is CHoCH, same/initial is BOS',
                                'FVG': 'three-bar nonoverlap: current low>high[-2] or current high<low[-2]; lifecycle only after creation; gap-through differs from observed fill',
                                'order_block': f'last opposite-body candle among preceding{lookback} bars before closing break; full candle high/low; known only at break',
                                'gap_exclusion': 'mechanical_gap=true declares boundary before that bar; FVG three-bar and order-block source-to-break windows crossing it or distinct explicit session_ids are excluded',
                                'interpretation': 'finite OHLC price structures, not evidence of institutions, orders or holdings'})
    result['limitations'].append('only explicitly declared mechanical/session boundaries were excluded; absent flags do not rule out corporate actions, halts or other mechanical causes')
    result['facts'] = [_fact('smc', 'last_closing_break_regime', result['latest_regime'], bars, group,
                              relation='support' if regime == 1 else 'conflict' if regime == -1 else 'neutral',
                              proposition_id='trend_continuation_long', definition=result['definitions']['BOS_CHoCH'],
                              falsification='A later opposite confirmed closing break changes the finite last-break regime.')] if bars else []
    return result


def _channel(bars, period, key, operation):
    return [None if i < period else operation(bar[key] for bar in bars[i-period:i]) for i in range(len(bars))]


def _turtle(bars, group):
    series = {'entry_high20': _channel(bars, 20, 'high', max), 'entry_high55': _channel(bars, 55, 'high', max),
              'exit_low10': _channel(bars, 10, 'low', min), 'exit_low20': _channel(bars, 20, 'low', min),
              'n20': atr(bars, 20)}
    events = []
    for name, channel, compare in (('long_entry20', 'entry_high20', lambda close, line: close > line),
                                  ('long_entry55', 'entry_high55', lambda close, line: close > line),
                                  ('long_exit10', 'exit_low10', lambda close, line: close < line),
                                  ('long_exit20', 'exit_low20', lambda close, line: close < line)):
        conditions = [None if level is None else compare(bar['close'], level)
                      for bar, level in zip(bars, series[channel])]
        series[name] = conditions
        for i, condition in enumerate(conditions):
            if condition and (i == 0 or conditions[i-1] is not True):
                events.append(_smc_event(name+'_condition', 'long' if 'entry' in name else 'exit_observation', i, bars, group,
                                          level=series[channel][i], n_known_at_close=series['n20'][i],
                                          falsification='Condition only; later closes relative to frozen-window channels must be reviewed. It is not an order or position record.'))
    latest = {key: values[-1] if values else None for key, values in series.items()}
    if latest['n20'] is not None:
        latest['two_n_distance'] = 2 * latest['n20']
    else:
        latest['two_n_distance'] = None
    result = _topic('ok' if len(bars) >= 56 else 'partial' if bars else 'insufficient_history',
                    backend='project-turtle-long-conditions-v1', series=series, latest=latest, events=events, facts=[],
                    definitions={'channels': 'entry20/55 highs and exit10/20 lows exclude the current bar',
                                 'N': 'Wilder20 true-range average from index1; current N known only at close',
                                 'condition': 'strict completed CLOSE breakout; intrabar highs/lows are not assumed fills',
                                 'two_n_distance': 'volatility distance only, not a personal stop, guaranteed loss bound, order or sizing instruction',
                                 'scope': 'long-side research conditions only; no legacy win-skip, pyramiding, transaction simulation or inferred holding'})
    if bars:
        state = latest['long_entry20']
        result['facts'].append(_fact('turtle', 'long_entry20', state, bars, group,
                                      relation='missing' if state is None else 'support' if state else 'neutral',
                                      proposition_id='trend_continuation_long', definition=result['definitions']['condition'],
                                      falsification='A later close below the preceding10-bar exit low negates the stated long trend-following condition; execution remains separate.'))
    return result


def _harmonic(bars, profile, context, enabled):
    if not enabled:
        return _topic('not_run', analysis=None, reason='explicitly disabled by caller')
    try:
        path = Path(__file__).with_name('harmonic_core.py')
        spec = importlib.util.spec_from_file_location('_technical_harmonic_core', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (OSError, ImportError) as error:
        return _topic('dependency_missing', analysis=None, reason=str(error))
    geometry_context = {key: context[key] for key in ('security_id', 'timeframe', 'adjustment', 'currency', 'price_basis') if key in context}
    result = module.analyze_harmonics(bars, profile={'reversal_threshold': profile['harmonic_reversal_threshold'],
                                                   'relative_tolerance': profile['harmonic_relative_tolerance']}, context=geometry_context)
    availability = {}
    known = None
    for bar in bars:
        known = bar['first_known_at'] if known is None else _max_time(known, bar['first_known_at'])
        availability[bar['time']] = known
    availability_events = []
    for event in result['events']:
        adjusted = deepcopy(event)
        adjusted.update(engine_bar_end_at=event['at'], engine_event_id=event['event_id'],
                        at=availability.get(event['at'], event['at']))
        adjusted['event_id'] = _digest({key: value for key, value in adjusted.items() if key != 'event_id'})
        availability_events.append(adjusted)
    facts = [_fact('harmonic', 'geometry_sets',
                   {key: [{'candidate_id': item['candidate_id'], 'revision_id': item['revision_id'],
                           'pattern': item['pattern'], 'direction': item['direction'], 'status': item['status']}
                          for item in result[key]] for key in ('patterns', 'forming', 'previews', 'withdrawn')},
                   bars, _source_group(context), relation='missing' if result['status'] == 'insufficient_history' else 'neutral',
                   definition='Finite project-harmonic-v1 geometry, separate confirmed prefix and active D.',
                   falsification='Each candidate retains its own strict adverse PRZ boundary; later invalidation never erases the earlier geometric confirmation.')]
    return _topic(result['status'], analysis=result, availability_events=availability_events, facts=facts,
                   availability_policy='analysis preserves legacy bar-end geometry fields/IDs; availability_events.at and wrapper facts use actual input-prefix availability')


def analyze_technical(bars, *, benchmark=None, profile=None, context=None, asof=None, include_harmonic=True):
    """Calculate six topics on qualified bars; all results are research facts.

    benchmark is {bars, benchmark_id, comparable:true, comparison_basis}. Market
    identity, corporate actions, sessions and aggregation belong to the caller's
    data gate. Each topic retains missing/not-comparable/not-run states.
    """
    result = {'status': 'invalid_input', 'metadata': {}, 'bars': [], 'series': {},
              'topics': {}, 'facts': [], 'propositions': [], 'limitations': []}
    try:
        chosen = _profile(profile)
        if type(include_harmonic) is not bool:
            raise ValueError('include_harmonic must be boolean')
        context = {} if context is None else deepcopy(context)
        if not isinstance(context, dict):
            raise ValueError('context must be an object')
        _digest(context)
        normalized, excluded = _normalize(bars, asof)
        group = _source_group(context)
        result.update(bars=normalized, metadata={'engine_version': VERSION, 'profile': chosen,
                                                'context': context, 'input_count': len(bars), 'used_count': len(normalized),
                                                'excluded_count': excluded, 'asof': asof,
                                                'data_cutoff': normalized[-1]['time'] if normalized else None,
                                                'first_known_at': max((bar['first_known_at'] for bar in normalized), key=_moment, default=None),
                                                'source_group': group,
                                                'input_sha256': hashlib.sha256(json.dumps(bars, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
                                                'availability_policy': 'known=max(bar end, available_at); absent available_at uses bar end; caller must retain snapshot provenance',
                                                'scope': 'no aggregation, exchange calendar, corporate-action qualification, orders or performance evaluation'})
        if not normalized:
            result['status'] = 'insufficient_history'
            result['limitations'].append('no completed bars available at the research cutoff')
            result['topics'] = {name: _topic('insufficient_history') for name in TOPICS}
            return result
        conventional = _conventional(normalized, group)
        conventional['relative_strength'] = _relative_strength(normalized, benchmark, asof, group)
        conventional['facts'].extend(conventional['relative_strength']['facts'])
        result['series'] = conventional['series']
        result['topics'] = {'conventional': conventional,
                            'candlestick': _candlesticks(normalized, chosen['candlestick_backend'], group),
                            'ichimoku': _ichimoku(normalized, group),
                            'harmonic': _harmonic(normalized, chosen, context, include_harmonic),
                            'smc': _smc(normalized, chosen, group),
                            'turtle': _turtle(normalized, group)}
        result['facts'] = [fact for topic in result['topics'].values() for fact in topic.get('facts', [])]
        result['propositions'] = [
            {'proposition_id': 'trend_continuation_long', 'text': 'Defined long trend alignment or breakout conditions are present.',
             'horizon': 'current completed bar; persistence must be checked on subsequent bars',
             'falsification': 'Use the particular fact threshold and original frozen window; no promise of future positive returns.'},
            {'proposition_id': 'relative_outperformance', 'text': 'Stock outperformed the qualified benchmark over the frozen common window.',
             'horizon': 'historical common window only', 'falsification': 'Relative index at/below100 over the same window negates this fact.'}]
        incomplete = any(topic['status'] in ('partial', 'insufficient_history', 'dependency_missing', 'invalid_input')
                         for topic in result['topics'].values())
        result['status'] = 'partial' if incomplete else 'ok'
        if conventional['relative_strength']['status'] != 'ok':
            result['limitations'].append('relative strength is missing, incomparable or unavailable; standalone stock topics remain usable')
        json.dumps(result, allow_nan=False)
        return result
    except (ValueError, TypeError, OverflowError, KeyError) as error:
        result.update(status='invalid_input', limitations=[str(error)], topics={}, facts=[], series={})
        return result


def replay_technical(bars, **kwargs):
    """Explicit per-prefix replay; useful for causality checks, not a backtest."""
    return [analyze_technical(bars[:end], **kwargs) for end in range(1, len(bars)+1)]


def analyze_close_prices(rows, *, profile=None, context=None, asof=None):
    """Run only genuine close-dependent indicators; never synthesize OHLC bars."""
    result = {'status': 'invalid_input', 'metadata': {'price_only': True}, 'bars': [], 'series': {},
              'topics': {}, 'facts': [], 'propositions': [], 'limitations': []}
    try:
        chosen = _profile(profile)
        context = {} if context is None else deepcopy(context)
        if not isinstance(context, dict):
            raise ValueError('context must be an object')
        _digest(context)
        normalized, excluded = _normalize(rows, asof, close_only=True)
        group = _source_group(context)
        result.update(bars=normalized, metadata={'engine_version': VERSION, 'profile': chosen,
                                                'context': context, 'input_count': len(rows), 'used_count': len(normalized),
                                                'excluded_count': excluded, 'asof': asof, 'price_only': True,
                                                'data_cutoff': normalized[-1]['time'] if normalized else None,
                                                'first_known_at': normalized[-1]['prefix_known_at'] if normalized else None,
                                                'source_group': group,
                                                'input_sha256': hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
                                                'availability_policy': 'known=max(bar end, available_at); absent available_at uses bar end; caller snapshot provenance required',
                                                'scope': 'close-only SMA/EMA/MACD/RSI/returns; no OHLC, ATR, volume, price structures or trading conclusions'})
        if not normalized:
            result['status'] = 'insufficient_history'
            result['topics'] = {name: _topic('insufficient_history') for name in TOPICS}
            result['limitations'].append('no completed close observations available at cutoff')
            return result
        conventional = _conventional(normalized, group, close_only=True)
        conventional['relative_strength'] = _relative_strength(normalized, None, asof, group)
        result['series'] = conventional['series']
        result['facts'] = conventional['facts']
        result['topics'] = {'conventional': conventional}
        for topic in TOPICS[1:]:
            result['topics'][topic] = _topic('missing_fields', missing_fields=['open', 'high', 'low'],
                                             reason='not run: close-only prices cannot establish OHLC geometry', facts=[])
        result['propositions'] = [
            {'proposition_id': 'trend_continuation_long', 'text': 'Defined close-price trend alignment is present.',
             'horizon': 'current completed close; persistence requires subsequent observations',
             'falsification': 'Close and SMA20 must remain above SMA60 under the frozen definition; no forecast or execution eligibility is implied.'}]
        result['status'] = 'partial'
        result['limitations'].append('only close-dependent conventional indicators ran; the other five topics require actual OHLC and were not run')
        json.dumps(result, allow_nan=False)
        return result
    except (ValueError, TypeError, OverflowError, KeyError) as error:
        result.update(status='invalid_input', limitations=[str(error)], topics={}, facts=[], series={})
        return result
