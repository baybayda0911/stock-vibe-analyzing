"""Causal, standard-library harmonic geometry; no fetching, trading or plotting.

The four rules are this project's named variant, not a universal market standard.
Ratios and D-price algebra were informed by djoffrey/HarmonicPatterns (MIT);
its ZigZag, scanners, prediction branches and other code are not included here.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math


DEFAULT_PROFILE = {
    'profile_id': 'project-harmonic-v1', 'version': '1.0.0',
    'reversal_threshold': 0.05, 'relative_tolerance': 0.05,
}
RULES = {
    'Gartley': {'AB_XA': (0.618, 0.618), 'BC_AB': (0.382, 0.886),
                'AD_XA': (0.786, 0.786), 'CD_BC': (1.13, 1.618), 'CD_AB': (1.0, 1.0)},
    'Bat': {'AB_XA': (0.382, 0.5), 'BC_AB': (0.382, 0.886),
            'AD_XA': (0.886, 0.886), 'CD_BC': (1.618, 2.618), 'CD_AB': (1.0, None)},
    'Butterfly': {'AB_XA': (0.786, 0.786), 'BC_AB': (0.382, 0.886),
                  'AD_XA': (1.27, 1.27), 'CD_BC': (1.618, 2.618), 'CD_AB': (1.0, None)},
    'Crab': {'AB_XA': (0.382, 0.618), 'BC_AB': (0.382, 0.886),
             'AD_XA': (1.618, 1.618), 'CD_BC': (2.618, 3.618), 'CD_AB': (1.0, None)},
}
RATIO_MEANINGS = {'AB_XA': '|A-B| / |X-A|', 'BC_AB': '|B-C| / |A-B|',
                  'AD_XA': '|A-D| / |X-A|', 'CD_BC': '|C-D| / |B-C|',
                  'CD_AB': '|C-D| / |A-B|'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:24]


def _profile(profile):
    result = dict(DEFAULT_PROFILE)
    if profile is not None:
        if not isinstance(profile, dict) or set(profile) - set(result):
            raise ValueError('profile contains unsupported fields')
        result.update(profile)
    if (result['profile_id'], result['version']) != ('project-harmonic-v1', '1.0.0'):
        raise ValueError('unsupported rule profile or version')
    for key in ('reversal_threshold', 'relative_tolerance'):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'{key} must be a finite number')
    if not 0 < result['reversal_threshold'] < 1:
        raise ValueError('reversal_threshold must be between zero and one')
    if not 0 <= result['relative_tolerance'] <= 0.1:
        raise ValueError('relative_tolerance must be between zero and 0.1')
    return result


def _time(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('time must be a nonempty ISO date or timestamp')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('time must be an ISO date or timestamp') from error


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a finite positive number')
    return float(value)


def _bars(ohlc):
    if not isinstance(ohlc, list):
        raise ValueError('ohlc must be a list')
    result = []
    previous = None
    for index, row in enumerate(ohlc):
        if not isinstance(row, dict):
            raise ValueError(f'bar {index} must be an object')
        stamp = row.get('time', row.get('timestamp'))
        moment = _time(stamp)
        try:
            if previous is not None and moment <= previous:
                raise ValueError('bar times must be strictly increasing')
        except TypeError as error:
            raise ValueError('bar times must use consistent timezone awareness') from error
        if row.get('completed', True) is not True:
            raise ValueError(f'bar {index} is not completed')
        values = {key: _number(row.get(key), f'bar {index} {key}')
                  for key in ('open', 'high', 'low', 'close')}
        if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
            raise ValueError(f'bar {index} OHLC is inconsistent')
        result.append({'index': index, 'time': stamp, **values})
        previous = moment
    return result


def _base(profile, cutoff, context):
    context = {} if context is None else deepcopy(context)
    if not isinstance(context, dict):
        raise ValueError('context must be an object')
    _digest(context)
    return {'status': 'no_candidates', 'metadata': {
        'engine_version': '1.0.0', 'profile': dict(profile), 'context': context,
        'data_cutoff': cutoff, 'ratio_meanings': dict(RATIO_MEANINGS),
        'rules': {name: {ratio: list(bounds) for ratio, bounds in rules.items()} for name, rules in RULES.items()},
        'numeric_epsilon': 1e-12,
        'tolerance_semantics': 'symmetric relative: [lower*(1-t), upper*(1+t)]; exact geometry and positive prices',
        'pivot_semantics': 'completed bars only; earliest equal extreme; at most one confirmation per bar; extend-and-reverse bar defers confirmation',
        'reversal_semantics': 'rise >= low*(1+r); decline <= high*(1-r); bootstrap needs extrema on distinct bars',
        'scenario_invalidation': 'strict adverse breach of PRZ outer boundary after the relevant known time',
        'calendar_expiry': None,
        'scan': {'scope': 'consecutive confirmed XABC/XABCD only', 'windows': 0, 'truncated': False},
    }, 'limitations': [], 'points': [], 'active_point': None, 'patterns': [],
        'forming': [], 'previews': [], 'rejected': [], 'withdrawn': [], 'events': []}


def _invalid(error, profile=None, cutoff=None, context=None):
    # Invalid caller metadata must not hide the actual input diagnostic.
    result = _base(DEFAULT_PROFILE, cutoff, None)
    result['status'] = 'invalid_input'
    result['limitations'] = [str(error)]
    return result


def _point(bar, kind):
    price = bar['high' if kind == 'H' else 'low']
    value = {'index': bar['index'], 'time': bar['time'], 'occurred_at': bar['time'],
             'kind': kind, 'price': price, 'first_known_at': bar['time'],
             'confirmed_at': None, 'confirmed_by_index': None, 'revisable': True}
    value['point_id'] = _digest([kind, bar['index'], bar['time'], price])
    value['revision_id'] = _digest(value)
    return value


def _confirmed(point, bar):
    result = dict(point, confirmed_at=bar['time'], confirmed_by_index=bar['index'], revisable=False)
    result['revision_id'] = _digest({key: value for key, value in result.items() if key != 'revision_id'})
    return result


def _event(kind, at, candidate_id=None, revision_id=None, **extra):
    value = {'type': kind, 'at': at, 'candidate_id': candidate_id, 'revision_id': revision_id, **extra}
    value['event_id'] = _digest(value)
    return value


def _build(bars, profile):
    confirmed, events, ambiguities = [], [], []
    if not bars:
        return confirmed, None, events, ambiguities
    low = _point(bars[0], 'L')
    high = _point(bars[0], 'H')
    active = None
    threshold = profile['reversal_threshold']
    for bar in bars[1:]:
        if active is None:
            if bar['low'] < low['price']:
                low = _point(bar, 'L')
            if bar['high'] > high['price']:
                high = _point(bar, 'H')
            if low['index'] == high['index']:
                if high['price'] >= low['price'] * (1 + threshold):
                    ambiguities.append({'index': bar['index'], 'time': bar['time'], 'reason': 'bootstrap high/low order unknown'})
                continue
            if low['index'] < high['index'] and high['price'] >= low['price'] * (1 + threshold):
                confirmed.append(_confirmed(low, bar))
                active = high
            elif high['index'] < low['index'] and low['price'] <= high['price'] * (1 - threshold):
                confirmed.append(_confirmed(high, bar))
                active = low
            else:
                continue
            events.append(_event('pivot_confirmed', bar['time'], point=deepcopy(confirmed[-1])))
            continue
        up = active['kind'] == 'H'
        extends = bar['high'] > active['price'] if up else bar['low'] < active['price']
        if extends:
            old = active
            active = _point(bar, 'H' if up else 'L')
            events.append(_event('active_point_revised', bar['time'], point=deepcopy(active), replaces_point_id=old['point_id']))
            reverses = bar['low'] <= active['price'] * (1 - threshold) if up else bar['high'] >= active['price'] * (1 + threshold)
            if reverses:
                ambiguities.append({'index': bar['index'], 'time': bar['time'],
                                    'reason': 'same bar extends the extreme and crosses reversal threshold; order unknown'})
            continue
        reverses = bar['low'] <= active['price'] * (1 - threshold) if up else bar['high'] >= active['price'] * (1 + threshold)
        if reverses and bar['index'] > active['index']:
            confirmed.append(_confirmed(active, bar))
            events.append(_event('pivot_confirmed', bar['time'], point=deepcopy(confirmed[-1])))
            active = _point(bar, 'L' if up else 'H')
    return confirmed, active, events, ambiguities


def build_pivots(ohlc, *, profile=None):
    """Build immutable confirmed pivots and one separately revisable extreme."""
    try:
        chosen = _profile(profile)
        bars = _bars(ohlc)
        points, active, events, ambiguities = _build(bars, chosen)
        return {'status': 'ok' if points else 'insufficient_history', 'profile': chosen,
                'data_cutoff': bars[-1]['time'] if bars else None, 'points': points,
                'active_point': active, 'events': events, 'ambiguities': ambiguities,
                'limitations': [] if points else ['not enough ordered threshold reversals to confirm a pivot']}
    except (ValueError, TypeError, OverflowError) as error:
        return {'status': 'invalid_input', 'points': [], 'active_point': None, 'events': [],
                'ambiguities': [], 'limitations': [str(error)]}


def _pivots(points, cutoff, active=None):
    if not isinstance(points, list):
        raise ValueError('confirmed_pivots must be a list')
    if cutoff is not None:
        cutoff_time = _time(cutoff)
    else:
        cutoff_time = None
    result = []
    for index, row in enumerate(points + ([active] if active is not None else [])):
        if not isinstance(row, dict) or row.get('kind') not in ('H', 'L'):
            raise ValueError('pivot kind must be H or L')
        position = row.get('index')
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError('pivot index must be a nonnegative integer')
        stamp = row.get('time', row.get('occurred_at'))
        moment = _time(stamp)
        if row.get('occurred_at') is not None and _time(row['occurred_at']) != moment:
            raise ValueError('pivot time and occurred_at disagree')
        first_known = row.get('first_known_at', stamp)
        first_time = _time(first_known)
        is_active = active is not None and index == len(points)
        confirmation = row.get('confirmed_at')
        if is_active and confirmation is not None:
            raise ValueError('active pivot cannot have a confirmation time')
        if not is_active and confirmation is None:
            raise ValueError('formal pivots require confirmed_at')
        if not is_active and row.get('revisable', False):
            raise ValueError('formal pivots cannot remain revisable')
        confirmation_time = _time(confirmation) if confirmation is not None else None
        if first_time < moment or (confirmation_time is not None and confirmation_time < first_time):
            raise ValueError('pivot times must satisfy occurred <= first known <= confirmed')
        if cutoff_time is not None and max(first_time, confirmation_time or first_time) > cutoff_time:
            raise ValueError('pivot is not known at data_cutoff')
        point = {'index': position, 'time': stamp, 'occurred_at': stamp, 'kind': row['kind'],
                 'price': _number(row.get('price'), 'pivot price'), 'first_known_at': first_known,
                 'confirmed_at': confirmation, 'confirmed_by_index': row.get('confirmed_by_index'),
                 'revisable': is_active}
        point['point_id'] = _digest([point['kind'], position, stamp, point['price']])
        point['revision_id'] = _digest(point)
        if result:
            before = result[-1]
            if position <= before['index'] or moment <= _time(before['time']):
                raise ValueError('pivot occurrence times and indices must strictly increase')
            if point['kind'] == before['kind']:
                raise ValueError('pivot kinds must alternate H/L')
            if point['price'] == before['price']:
                raise ValueError('zero-length pivot leg')
            if ((point['kind'] == 'H' and point['price'] <= before['price']) or
                    (point['kind'] == 'L' and point['price'] >= before['price'])):
                raise ValueError('pivot H/L labels disagree with price direction')
            if not is_active and _time(confirmation) < _time(before['confirmed_at']):
                raise ValueError('pivot confirmation times must be nondecreasing')
        result.append(point)
    return (result[:-1], result[-1]) if active is not None else (result, None)


def _bands(bounds, tolerance):
    lower, upper = bounds
    return lower * (1 - tolerance), None if upper is None else upper * (1 + tolerance)


def _checks(ratios, rules, tolerance):
    result = {}
    for name, value in ratios.items():
        lower, upper = _bands(rules[name], tolerance)
        # Only absorb floating arithmetic noise, never an extra user tolerance.
        fuzz = 1e-12 * max(1, abs(value), abs(lower), abs(upper or 0))
        passed = value >= lower - fuzz and (upper is None or value <= upper + fuzz)
        target = rules[name][0] if rules[name][0] == rules[name][1] else None
        result[name] = {'actual': value, 'target': target, 'nominal': list(rules[name]),
                        'accepted': [lower, upper], 'relative_error': None if target is None else (value - target) / target,
                        'passed': passed}
    return result


def _ratios(points):
    x, a, b, c = [point['price'] for point in points[:4]]
    xa, ab, bc = abs(a - x), abs(b - a), abs(c - b)
    result = {'AB_XA': ab / xa, 'BC_AB': bc / ab}
    if len(points) == 5:
        d = points[4]['price']
        result.update({'AD_XA': abs(d - a) / xa, 'CD_BC': abs(d - c) / bc,
                       'CD_AB': abs(d - c) / ab})
    return result


def _prz(points, rules, tolerance):
    x, a, b, c = [point['price'] for point in points[:4]]
    sign = 1 if a > x else -1
    constraints = []
    for name, origin, scale in (('AD_XA', a, abs(a - x)),
                                ('CD_BC', c, abs(c - b)), ('CD_AB', c, abs(a - b))):
        lo, hi = _bands(rules[name], tolerance)
        edge1 = origin - sign * lo * scale
        edge2 = None if hi is None else origin - sign * hi * scale
        lower = edge2 if sign == 1 else edge1
        upper = edge1 if sign == 1 else edge2
        constraints.append({'ratio': name, 'ratio_interval': [lo, hi], 'price_interval': [lower, upper]})
    lower = max([0.0] + [constraint['price_interval'][0] for constraint in constraints if constraint['price_interval'][0] is not None])
    upper = min([constraint['price_interval'][1] for constraint in constraints if constraint['price_interval'][1] is not None])
    fuzz = 1e-12 * max(1, abs(lower), abs(upper))
    valid = upper > 0 and lower <= upper + fuzz
    if valid and lower > upper:
        upper = lower  # Coincident boundaries can differ only by floating roundoff.
    return {'construction': 'project intersection of all D-dependent continuous constraints and positive prices',
            'valid': valid, 'intervals': [{'lower': lower, 'upper': upper, 'lower_open': lower == 0,
                                        'upper_open': False}] if valid else [],
            'constraints': constraints, 'reason': None if valid else 'D constraints have no common positive price interval'}


def _candidate(points, pattern, profile, context):
    anchors = points[:4]
    rules = RULES[pattern]
    ratios = _ratios(points)
    checks = _checks(ratios, rules, profile['relative_tolerance'])
    if not all(checks[name]['passed'] for name in ('AB_XA', 'BC_AB')):
        return None
    prz = _prz(anchors, rules, profile['relative_tolerance'])
    bullish = anchors[0]['kind'] == 'L'
    candidate_id = _digest([profile, context, pattern, 'bullish' if bullish else 'bearish',
                            [point['point_id'] for point in anchors]])
    known = max((point['confirmed_at'] for point in anchors), key=_time)
    value = {'candidate_id': candidate_id, 'pattern': pattern,
             'direction': 'bullish' if bullish else 'bearish', 'status': 'forming',
             'points': {letter: deepcopy(point) for letter, point in zip('XABCD', points)},
             'ratios': ratios, 'checks': checks, 'prz': prz,
             'first_known_at': known, 'confirmed_at': None, 'withdrawn_at': None,
             'invalidated_at': None, 'scenario_status': 'pending', 'reason': None,
             'invalidation': None}
    if prz['valid']:
        interval = prz['intervals'][0]
        value['invalidation'] = {'price': interval['lower' if bullish else 'upper'],
                                 'relation': 'below' if bullish else 'above',
                                 'semantics': 'strict adverse PRZ boundary breach; observation only'}
    if len(points) == 4:
        value['points']['D'] = {'price': anchors[1]['price'] + (-1 if bullish else 1) *
                              rules['AD_XA'][0] * abs(anchors[1]['price'] - anchors[0]['price']),
                              'index': None, 'time': None, 'occurred_at': None,
                              'first_known_at': known, 'confirmed_at': None, 'revisable': True,
                              'reference_only': True}
    value['revision_id'] = _digest(value)
    return value


def _revise(candidate):
    candidate['revision_id'] = _digest({key: value for key, value in candidate.items() if key != 'revision_id'})


def _opened(candidate):
    # Birth identity is independent of later D revisions and snapshot cutoffs.
    return _event('candidate_opened', candidate['first_known_at'], candidate['candidate_id'],
                  pattern=candidate['pattern'], direction=candidate['direction'])


def analyze_pivots(confirmed_pivots, *, active_pivot=None, data_cutoff=None, profile=None, context=None):
    """Scan consecutive confirmed pivots; an active D is only a preview.

    External points require index, time/occurred_at, kind, price and confirmed_at.
    The caller owns their market-data qualification and frozen pivot provenance.
    """
    try:
        chosen = _profile(profile)
        if data_cutoff is None:
            times = [point.get('confirmed_at') for point in confirmed_pivots if isinstance(point, dict)] if isinstance(confirmed_pivots, list) else []
            if active_pivot is not None and isinstance(active_pivot, dict):
                times.append(active_pivot.get('first_known_at', active_pivot.get('time', active_pivot.get('occurred_at'))))
            data_cutoff = max((time for time in times if time is not None), key=_time, default=None)
        points, active = _pivots(confirmed_pivots, data_cutoff, active_pivot)
        result = _base(chosen, data_cutoff, context)
        result['points'], result['active_point'] = points, active
        result['limitations'].append('pivot-only input cannot reconstruct OHLC touches or first adverse crossings')
        result['metadata']['scan']['windows'] = max(0, len(points) - 3)
        if len(points) < 4:
            result['status'] = 'insufficient_history'
            result['limitations'].append('at least four confirmed alternating pivots are required')
            return result
        for index in range(len(points) - 3):
            anchors = points[index:index + 4]
            d = points[index + 4] if index + 4 < len(points) else None
            for pattern in RULES:
                candidate = _candidate(anchors, pattern, chosen, result['metadata']['context'])
                if candidate is None:
                    continue
                if not candidate['prz']['valid']:
                    candidate.update(status='rejected', reason=candidate['prz']['reason'])
                    _revise(candidate)
                    result['rejected'].append(candidate)
                    continue
                result['events'].append(_opened(candidate))
                if d is not None:
                    completed = _candidate(anchors + [d], pattern, chosen, result['metadata']['context'])
                    if all(check['passed'] for check in completed['checks'].values()):
                        completed.update(status='geometry_confirmed', confirmed_at=d['confirmed_at'], scenario_status='active')
                        _revise(completed)
                        result['patterns'].append(completed)
                        result['events'].append(_event('geometry_confirmed', d['confirmed_at'], completed['candidate_id'],
                                                       completed['revision_id']))
                    else:
                        completed.update(status='withdrawn', withdrawn_at=d['confirmed_at'],
                                         reason='next confirmed D fails the complete geometry')
                        _revise(completed)
                        result['withdrawn'].append(completed)
                        result['events'].append(_event('candidate_withdrawn', d['confirmed_at'], completed['candidate_id'],
                                                       completed['revision_id'], reason=completed['reason']))
                else:
                    result['forming'].append(candidate)
                    if active is not None:
                        boundary = candidate['invalidation']
                        exceeded = (active['price'] < boundary['price'] if boundary['relation'] == 'below'
                                    else active['price'] > boundary['price'])
                        if exceeded:
                            result['forming'].remove(candidate)
                            observed = _candidate(anchors + [active], pattern, chosen, result['metadata']['context'])
                            observed.update(status='withdrawn', withdrawn_at=data_cutoff,
                                            reason='active D exceeded the adverse PRZ boundary')
                            _revise(observed)
                            result['withdrawn'].append(observed)
                            result['events'].append(_event('candidate_withdrawn', data_cutoff, observed['candidate_id'],
                                                           reason=observed['reason']))
                            continue
                        preview = _candidate(anchors + [active], pattern, chosen, result['metadata']['context'])
                        if all(check['passed'] for check in preview['checks'].values()):
                            preview.update(status='active_d_preview', scenario_status='pending')
                            _revise(preview)
                            result['previews'].append(preview)
        result['status'] = 'ok' if result['patterns'] or result['forming'] or result['previews'] else 'no_candidates'
        if not (result['patterns'] or result['forming'] or result['previews']):
            result['limitations'].append('no qualifying geometry in the scanned confirmed pivot windows')
        return result
    except (ValueError, TypeError, OverflowError, ZeroDivisionError) as error:
        return _invalid(error)


def _breach(candidate, bars, after, until=None, include_known_bar=False):
    boundary = candidate['invalidation']
    for bar in bars:
        moment = _time(bar['time'])
        known = _time(after)
        if moment < known or (moment == known and not include_known_bar) or (until is not None and moment > _time(until)):
            continue
        if ((boundary['relation'] == 'below' and bar['low'] < boundary['price']) or
                (boundary['relation'] == 'above' and bar['high'] > boundary['price'])):
            return bar['time']
    return None


def _market_events(result, bars):
    for candidate in result['patterns']:
        breach = _breach(candidate, bars, candidate['confirmed_at'])
        if breach is not None:
            candidate.update(invalidated_at=breach, scenario_status='invalidated')
            # Preserve the immutable geometry event's original revision.
            _revise(candidate)
            result['events'].append(_event('scenario_invalidated', breach, candidate['candidate_id'],
                                           candidate['revision_id'], reason='strict adverse PRZ boundary breach'))
    for candidate in list(result['forming']) + result['withdrawn']:
        breach = _breach(candidate, bars, candidate['first_known_at'], candidate['withdrawn_at'], include_known_bar=True)
        if breach is None:
            continue
        if candidate in result['forming']:
            result['forming'].remove(candidate)
            result['withdrawn'].append(candidate)
        candidate.update(status='withdrawn', withdrawn_at=breach, reason='active D exceeded the adverse PRZ boundary')
        _revise(candidate)
        result['previews'] = [preview for preview in result['previews'] if preview['candidate_id'] != candidate['candidate_id']]
        result['events'] = [event for event in result['events'] if not
                            (event['candidate_id'] == candidate['candidate_id'] and event['type'] == 'candidate_withdrawn')]
        result['events'].append(_event('candidate_withdrawn', breach, candidate['candidate_id'],
                                       reason=candidate['reason']))
    for candidate in result['patterns'] + result['forming'] + result['withdrawn']:
        first_touch = None
        interval = candidate['prz']['intervals'][0]
        for bar in bars:
            if _time(bar['time']) <= _time(candidate['first_known_at']):
                continue
            if candidate['withdrawn_at'] is not None and _time(bar['time']) > _time(candidate['withdrawn_at']):
                break
            if bar['high'] >= interval['lower'] and bar['low'] <= interval['upper']:
                first_touch = bar['time']
                break
        if first_touch is not None:
            result['events'].append(_event('prz_first_touched', first_touch, candidate['candidate_id'],
                                           path_order='unknown within OHLC bar; touch is not confirmation'))
    result['events'].sort(key=lambda event: (_time(event['at']), event['type'], event['event_id']))
    result['status'] = ('insufficient_history' if len(result['points']) < 4 else
                        'ok' if result['patterns'] or result['forming'] or result['previews'] else 'no_candidates')


def analyze_harmonics(ohlc, *, profile=None, context=None):
    """Analyze one frozen, completed OHLC prefix using only its available bars."""
    try:
        chosen = _profile(profile)
        bars = _bars(ohlc)
        points, active, pivot_events, ambiguities = _build(bars, chosen)
        result = analyze_pivots(points, active_pivot=active,
                                data_cutoff=bars[-1]['time'] if bars else None,
                                profile=chosen, context=context)
        if result['status'] == 'invalid_input':
            return result
        result['limitations'].remove('pivot-only input cannot reconstruct OHLC touches or first adverse crossings')
        result['metadata']['bar_count'] = len(bars)
        result['metadata']['ambiguities'] = ambiguities
        if ambiguities:
            result['limitations'].append('some same-bar extreme/reversal orders are unknown; confirmation was deferred')
        result['events'].extend(pivot_events)
        _market_events(result, bars)
        return result
    except (ValueError, TypeError, OverflowError) as error:
        return _invalid(error)


def replay_harmonics(ohlc, *, profile=None, context=None):
    """Return every completed-bar prefix; no future bar is supplied to a step."""
    if not isinstance(ohlc, list):
        return [analyze_harmonics(ohlc, profile=profile, context=context)]
    return [analyze_harmonics(ohlc[:end], profile=profile, context=context)
            for end in range(1, len(ohlc) + 1)]
