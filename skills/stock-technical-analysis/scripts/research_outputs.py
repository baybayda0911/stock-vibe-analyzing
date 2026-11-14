"""Pure research outputs. No files, network, account access, orders or P&L."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR
import math


def _moment(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be ISO text with timezone')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('Timestamp requires timezone')
    return result


def _finite(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a numeric observation')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Observation must be finite')
    return result


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a price or quantity')
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('Price and quantity must be positive and finite')
    return result


def _invalid(error):
    return {'status': 'invalid_input', 'limitations': [str(error)]}


def execution_conditions(scenario, security, rules=None, user_context=None, *, asof, qualification='diagnostic'):
    """Describe a requested action; caller-supplied rules never become an order."""
    try:
        cutoff = _moment(asof)
        if not isinstance(scenario, dict) or not isinstance(security, dict):
            raise ValueError('scenario and security must be objects')
        action = scenario.get('action')
        if action in (None, '', 'observe'):
            return {'status': 'not_requested', 'limitations': [], 'orders_created': False}
        if action not in {'buy', 'increase', 'sell', 'reduce', 'stop_loss'}:
            raise ValueError('Unsupported action')
        if security.get('market') != 'A-share':
            raise ValueError('Execution conditions currently cover A-share only')
        if rules is not None and not isinstance(rules, dict) or user_context is not None and not isinstance(user_context, dict):
            raise ValueError('rules and user_context must be objects')
        rules, user = rules or {}, user_context or {}
        limitations, checks, prices = [], {}, {}
        result = {'status': 'non_personal', 'action': action, 'qualification': qualification,
                  'conditions': {k: scenario.get(k) for k in ('trigger', 'confirmation', 'invalidation', 'observation_window')},
                  'prices': prices, 'checks': checks, 'requested_quantity': None,
                  'orders_created': False, 'can_submit_order': False, 'limitations': limitations}
        if not isinstance(security.get('security_id'), str) or not security['security_id'].strip():
            limitations.append('Qualified security identity is missing')
        if scenario.get('first_known_at') and _moment(scenario['first_known_at']) > cutoff:
            raise ValueError('Scenario was not known at asof')
        if not scenario.get('first_known_at'):
            limitations.append('Scenario first-known timestamp is unknown')
        if qualification != 'eligible':
            limitations.append('Only eligible inputs can support a personal conditional card')
        if not all(isinstance(scenario.get(k), str) and scenario[k].strip()
                   for k in ('trigger', 'confirmation', 'invalidation', 'observation_window')):
            limitations.append('Scenario conditions or observation window are incomplete')
        raw_trigger = raw_invalidation = None
        if scenario.get('trigger_price') is not None and scenario.get('invalidation_price') is not None:
            trigger, invalidation = _decimal(scenario['trigger_price']), _decimal(scenario['invalidation_price'])
            prices.update(analysis_trigger=str(trigger), analysis_invalidation=str(invalidation))
            if scenario.get('price_basis') == 'raw':
                raw_trigger, raw_invalidation = trigger, invalidation
            else:
                mapping = scenario.get('price_mapping') or {}
                if (isinstance(mapping, dict) and mapping.get('validated') is True and mapping.get('evidence')
                        and mapping.get('available_at') and _moment(mapping['available_at']) <= cutoff
                        and mapping.get('valid_until') and cutoff <= _moment(mapping['valid_until'])):
                    factor = _decimal(mapping['adjusted_per_raw'])
                    raw_trigger, raw_invalidation = trigger / factor, invalidation / factor
                    prices['mapping_evidence'] = mapping['evidence']
                else:
                    limitations.append('Verified contemporaneous adjusted-to-raw mapping is missing')
        else:
            limitations.append('Numeric trigger and invalidation are missing')
        identity_fields = ('market', 'exchange', 'board')
        rule_ok = (rules.get('validated') is True and bool(rules.get('evidence')) and bool(rules.get('rule_id'))
                   and all(security.get(k) and rules.get(k) == security[k] for k in identity_fields))
        if rule_ok:
            rule_ok = bool(rules.get('available_at') and _moment(rules['available_at']) <= cutoff
                           and rules.get('effective_from') and _moment(rules['effective_from']) <= cutoff
                           and rules.get('effective_until') and cutoff <= _moment(rules['effective_until']))
        checks['dated_security_rules'] = bool(rule_ok)
        if not rule_ok:
            limitations.append('Validated date, market, exchange and board-specific rules are required')
        if rule_ok and raw_trigger is not None:
            tick = _decimal(rules['tick_size'])
            buy = action in {'buy', 'increase'}
            rounded_trigger = (raw_trigger / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick
            rounded_invalidation = (raw_invalidation / tick).to_integral_value(rounding=ROUND_FLOOR if buy else ROUND_CEILING) * tick
            if min(rounded_trigger, rounded_invalidation) <= 0 or rounded_trigger == rounded_invalidation:
                raise ValueError('Rounded prices leave no positive risk distance')
            if buy and rounded_trigger <= rounded_invalidation:
                raise ValueError('Long-entry invalidation must be below its trigger')
            prices.update(raw_trigger=str(raw_trigger), raw_invalidation=str(raw_invalidation),
                          rounded_trigger=str(rounded_trigger), rounded_invalidation=str(rounded_invalidation),
                          trigger_rounding_difference=str(rounded_trigger - raw_trigger),
                          risk_distance=str(abs(rounded_trigger - rounded_invalidation)), tick_size=str(tick))
            if type(rules.get('has_price_limit')) is not bool:
                limitations.append('Applicable price limits are unknown')
            elif rules['has_price_limit']:
                low, high = _decimal(rules['lower_price_limit']), _decimal(rules['upper_price_limit'])
                if low > high:
                    raise ValueError('Price limits are reversed')
                checks['trigger_within_limits'] = low <= rounded_trigger <= high
                checks['invalidation_within_limits'] = low <= rounded_invalidation <= high
                if not all(checks[k] for k in ('trigger_within_limits', 'invalidation_within_limits')):
                    limitations.append('Rounded condition lies outside current limits; do not clip the original definition')
            else:
                checks['trigger_within_limits'] = True
        elif raw_trigger is not None:
            prices.update(raw_trigger=str(raw_trigger), raw_invalidation=str(raw_invalidation))
        state_known = bool(rules.get('state_asof') and _moment(rules['state_asof']) <= cutoff
                           and rules.get('state_valid_until') and cutoff <= _moment(rules['state_valid_until']))
        checks['session_and_state'] = bool(state_known and rules.get('session_open') is True and rules.get('suspended') is False)
        if not checks['session_and_state']:
            limitations.append('Trading session, suspension or state freshness is unknown or restricted')
        if type(rules.get('t_plus_one')) is not bool:
            limitations.append('Settlement rule is unknown; next bar never proves T+1 eligibility')
        elif rules['t_plus_one']:
            result['settlement_note'] = 'New same-day purchases are not assumed sellable at the next intraday bar'
        if not user:
            limitations.append('No personal context: observe conditions without personal quantity or position advice')
            return result
        if not user.get('available_at') or _moment(user['available_at']) > cutoff:
            limitations.append('Personal facts are missing an asof-qualified availability statement')
            result['status'] = 'blocked'
            return result
        common = ('investment_horizon', 'position_fraction', 'risk_budget', 'requested_quantity', 'holdings_quantity')
        required = common + (('available_cash',) if action in {'buy', 'increase'} else ('sellable_quantity', 'holding_cost'))
        missing = [k for k in required if user.get(k) is None or user.get(k) == '']
        if missing:
            limitations.append('Personal facts missing: ' + ', '.join(missing))
        else:
            if not isinstance(user['investment_horizon'], str) or not user['investment_horizon'].strip():
                limitations.append('Investment horizon must be explicit nonempty text')
            quantity, risk_budget = _decimal(user['requested_quantity']), _decimal(user['risk_budget'])
            holdings = Decimal(str(user['holdings_quantity']))
            position = Decimal(str(user['position_fraction']))
            if not holdings.is_finite() or holdings < 0 or not position.is_finite() or not 0 <= position <= 1:
                raise ValueError('Holdings and position fraction are invalid')
            if holdings > 0 and user.get('holding_cost') is None:
                limitations.append('Existing-position cost is unknown')
            if user.get('holding_cost') is not None:
                _decimal(user['holding_cost'])
            result['requested_quantity'] = str(quantity)
            if rule_ok:
                step, minimum = _decimal(rules['quantity_step']), _decimal(rules['minimum_quantity'])
                checks['quantity_rule'] = quantity >= minimum and quantity % step == 0
                if action in {'sell', 'reduce', 'stop_loss'} and rules.get('allow_odd_lot_liquidation') is True and quantity == holdings:
                    checks['quantity_rule'] = True
                if not checks['quantity_rule']:
                    limitations.append('Requested quantity violates the supplied minimum or quantity increment')
            if action in {'sell', 'reduce', 'stop_loss'}:
                sellable = Decimal(str(user['sellable_quantity']))
                if not sellable.is_finite() or sellable < 0 or sellable > holdings:
                    raise ValueError('Sellable quantity is invalid')
                if rules.get('t_plus_one') is True and user.get('new_purchase_at'):
                    local = timezone(timedelta(hours=8))
                    bought_at = _moment(user['new_purchase_at'])
                    if bought_at > cutoff:
                        raise ValueError('A personal purchase occurs after asof')
                    if bought_at.astimezone(local).date() == cutoff.astimezone(local).date():
                        if user.get('new_purchase_quantity') is None:
                            limitations.append('Same-day purchase quantity is unknown; next bar is not T+1')
                        else:
                            bought = _decimal(user['new_purchase_quantity'])
                            if bought > holdings:
                                raise ValueError('Same-day purchase exceeds holdings')
                            sellable = min(sellable, holdings - bought)
                checks['sellable_quantity'] = quantity <= sellable
                if not checks['sellable_quantity']:
                    limitations.append('Requested sale exceeds known settled sellable quantity')
            if prices.get('risk_distance'):
                fees = user.get('fee_estimate')
                buffer = user.get('slippage_buffer')
                if fees is None or buffer is None:
                    limitations.append('Fees or slippage assumptions are unknown; no bounded personal risk claim')
                else:
                    fees, buffer = Decimal(str(fees)), Decimal(str(buffer))
                    if not fees.is_finite() or not buffer.is_finite() or min(fees, buffer) < 0:
                        raise ValueError('Fees and slippage buffer must be finite nonnegative assumptions')
                    estimated = (Decimal(prices['risk_distance']) + buffer) * quantity + fees
                    result['risk_amount_under_assumptions'] = str(estimated)
                    checks['risk_budget'] = estimated <= risk_budget
                    if not checks['risk_budget']:
                        limitations.append('Requested quantity exceeds supplied risk budget after rounding')
                if action in {'buy', 'increase'}:
                    cash = Decimal(str(user['available_cash']))
                    if not cash.is_finite() or cash < 0:
                        raise ValueError('Available cash must be finite and nonnegative')
                    required_cash = quantity * Decimal(prices['rounded_trigger'])
                    if isinstance(fees, Decimal):
                        required_cash += fees
                    if isinstance(buffer, Decimal):
                        required_cash += buffer * quantity
                    checks['cash_under_declared_costs'] = required_cash <= cash
                    if not checks['cash_under_declared_costs']:
                        limitations.append('Requested purchase exceeds supplied cash')
        result['status'] = 'personal_conditional' if not limitations else 'blocked'
        result['risk_warning'] = 'Stops and condition prices do not guarantee fills or cap losses; user decides and reports actual trades'
        return result
    except (ValueError, TypeError, KeyError, DecimalException, OverflowError) as error:
        return _invalid(error)


def review_summary(records, *, asof):
    """Keep condition, adoption, execution and issues separate without making trades."""
    try:
        cutoff = _moment(asof)
        if not isinstance(records, list):
            raise ValueError('Review records must be a list')
        issue_tags = {'data_calculation', 'analysis_conditions', 'plan', 'execution', 'external_unknown'}
        execution_states = {'not_applicable', 'unknown', 'not_triggered', 'triggered_restricted',
                            'user_did_not_trade', 'partial', 'executed'}
        adoption_states = {'research_only', 'adopted', 'withdrawn', 'unknown'}
        condition_states = {'pending', 'supported', 'weakened', 'falsified', 'fulfilled', 'partially_fulfilled',
                            'not_fulfilled', 'not_due', 'unknown', 'withdrawn', 'expired', 'not_triggered',
                            'triggered_pending', 'confirmed', 'invalidated', 'observed', 'no_change',
                            'cannot_determine', 'not_applicable'}
        rows, excluded, seen = [], [], []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('item_id'), str) or not record['item_id']:
                raise ValueError('Every review record needs an item_id')
            if record in seen:
                continue
            seen.append(record)
            identity = {'source_result_id': record.get('source_result_id', record.get('result_id')),
                        'item_id': record['item_id']}
            if any(record.get(k) and _moment(record[k]) > cutoff for k in ('generated_at', 'frozen_at', 'first_known_at', 'data_cutoff')):
                excluded.append({**identity, 'reason': 'record_not_available_at_asof'})
                continue
            issues = record.get('issues', [])
            if not isinstance(issues, list) or any(tag not in issue_tags for tag in issues):
                raise ValueError('Review issues must use the five declared categories')
            execution = record.get('execution_status', 'unknown')
            adoption = record.get('adoption_status', 'unknown')
            if execution not in execution_states or adoption not in adoption_states:
                raise ValueError('Unknown execution or adoption state')
            condition = record.get('condition_status', record.get('status', 'unknown'))
            if condition not in condition_states:
                raise ValueError('Unknown condition state')
            row = {**identity, 'original_definition': record.get('definition'), 'condition_status': condition,
                   'adoption_status': adoption, 'execution_status': execution, 'issues': list(dict.fromkeys(issues)),
                   'reason': record.get('reason', ''), 'report_locator': record.get('report_locator'),
                   'evidence_ids': record.get('evidence_ids', []), 'timestamps': {}, 'limitations': []}
            for field in ('generated_at', 'first_known_at', 'frozen_at', 'triggered_at', 'executed_at', 'feedback_received_at', 'valuation_asof'):
                if record.get(field):
                    if _moment(record[field]) <= cutoff:
                        row['timestamps'][field] = record[field]
                    else:
                        row['limitations'].append(field + ' is after asof and excluded')
                        row.update(reason='Future event details excluded', evidence_ids=[], issues=[])
                        if field == 'triggered_at':
                            row['condition_status'] = 'unknown'
                        if field in {'feedback_received_at', 'executed_at'}:
                            row.update(condition_status='unknown', execution_status='unknown', adoption_status='unknown',
                                       reason='Future feedback excluded', evidence_ids=[])
                            row['issues'] = []
            if record.get('feedback_received_at') and _moment(record['feedback_received_at']) > cutoff:
                row['timestamps'].pop('executed_at', None)
            row['pnl_status'] = ('not_applicable' if row['execution_status'] in
                                 {'not_applicable', 'not_triggered', 'triggered_restricted', 'user_did_not_trade'}
                                 else 'not_computed')
            rows.append(row)
        return {'status': 'partial' if excluded else 'ok', 'rows': rows, 'excluded': excluded,
                'limitations': [] if records else ['No prior frozen items supplied'],
                'trades_created': False, 'pnl_computed': False}
    except (ValueError, TypeError, KeyError) as error:
        return _invalid(error)


def _output_source_group(metadata, fallback):
    context = metadata.get('context', {})
    if not isinstance(context, dict):
        raise ValueError('Source context must be an object')
    for field in ('effective_source_group', 'source_group', 'source_sha256'):
        for container in (metadata, context):
            value = container.get(field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError('Source group metadata must be nonempty text')
                return value
    return fallback


def _background_closes(bars, cutoff, available_at=None):
    if not isinstance(bars, (list, tuple)):
        raise ValueError('Close bars must be an array')
    snapshot = _moment(available_at) if available_at is not None else None
    if snapshot is not None and snapshot > cutoff:
        raise ValueError('Source snapshot was unavailable at asof')
    output, previous, excluded = {}, None, 0
    for bar in bars:
        if not isinstance(bar, dict):
            raise ValueError('Close bar must be an object')
        at, close = _moment(bar['time']), _finite(bar['close'])
        if close <= 0 or previous is not None and at <= previous:
            raise ValueError('Close prices must be positive and times strictly increasing')
        previous = at
        completed = bar.get('completed', True)
        if type(completed) is not bool:
            raise ValueError('Completed must be boolean')
        available = _moment(bar['available_at']) if bar.get('available_at') is not None else at
        known = max(at, snapshot or at, available)
        if at > cutoff or known > cutoff or not completed:
            excluded += 1
            continue
        output[at] = {'time': at.isoformat(), 'close': close, 'known': known}
    return output, excluded


def market_background(stock_bars, market=None, industry=None, *, asof, metadata=None):
    """Compare only common completed observations; returns are fractions, indices start at 100."""
    try:
        cutoff = _moment(asof)
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError('Stock metadata must be an object')
        metadata = dict(metadata or {})
        _evidence_json(metadata)
        window_bars = metadata.get('window_bars', 252)
        if type(window_bars) is not int or window_bars <= 0:
            raise ValueError('window_bars must be a positive integer')
        stock, excluded = _background_closes(stock_bars, cutoff, metadata.get('snapshot_available_at'))
        group = _output_source_group(metadata, 'unverified-stock-source')
        limitations = [f'Excluded {excluded} future, unavailable or unfinished stock bars'] if excluded else []
        if metadata.get('snapshot_available_at') is None:
            limitations.append('Stock snapshot availability is undeclared; comparisons remain diagnostic')
            metadata['qualification'] = 'diagnostic'
        comparisons, facts = [], []
        for reference, descriptor in (('market', market), ('industry', industry)):
            comparison = {'reference': reference, 'id': None, 'status': 'missing', 'common_dates': [],
                          'stock_index': [], 'reference_index': [], 'relative_index': [],
                          'windows': {str(n): {'status': 'insufficient_data', 'stock_return': None,
                                               'reference_return': None, 'excess_return': None,
                                               'start': None, 'end': None,
                                               'definition': f'{n} common completed bars require {n+1} prices; fractional returns and arithmetic excess'}
                                      for n in (20, 60, 120)}, 'limitations': []}
            comparisons.append(comparison)
            if descriptor is None:
                comparison['limitations'].append(f'{reference} reference was not supplied')
                continue
            if not isinstance(descriptor, dict):
                comparison.update(status='unavailable', limitations=['Reference must be an object'])
                continue
            try:
                identity = descriptor.get('id')
                if not isinstance(identity, str) or not identity.strip():
                    raise ValueError('Reference id is required')
                comparison['id'] = identity
                if descriptor.get('comparable') is not True or not metadata.get('comparison_basis') or descriptor.get('comparison_basis') != metadata['comparison_basis']:
                    comparison.update(status='incomparable', limitations=['Comparable flag and matching comparison_basis are required'])
                    continue
                if descriptor.get('available_at') is None:
                    raise ValueError('Reference availability is unknown')
                weak = False
                if reference == 'industry':
                    classification_at = descriptor.get('classification_available_at')
                    classified = descriptor.get('classification_known') is True and classification_at is not None and _moment(classification_at) <= cutoff
                    if not classified:
                        if descriptor.get('proxy') is not True:
                            comparison['limitations'].append('Industry membership was not known at asof')
                            continue
                        weak = True
                        comparison['limitations'].append('Weak proxy background only; no qualified industry-membership conclusion')
                baseline, omitted = _background_closes(descriptor.get('bars'), cutoff, descriptor['available_at'])
                if omitted:
                    comparison['limitations'].append(f'Excluded {omitted} future, unavailable or unfinished reference bars')
                common = sorted(set(stock) & set(baseline))
                dates = common[-window_bars:]
                if not dates:
                    comparison.update(status='insufficient_data')
                    comparison['limitations'].append('No common completed observations at identical bar timestamps')
                    continue
                first = dates[0]
                comparison.update(status='ok', qualification='weak_proxy' if weak else metadata.get('qualification', 'diagnostic'),
                                  display_window={'window_bars': window_bars, 'available_common_bars': len(common),
                                                  'used_bars': len(dates), 'start': stock[dates[0]]['time'],
                                                  'end': stock[dates[-1]]['time'],
                                                  'definition': 'Most recent fixed common completed observations; no return-based selection'},
                                  common_dates=[stock[at]['time'] for at in dates],
                                  stock_index=[_finite(stock[at]['close'] / stock[first]['close'] * 100) for at in dates],
                                  reference_index=[_finite(baseline[at]['close'] / baseline[first]['close'] * 100) for at in dates])
                comparison['relative_index'] = [_finite(s / r * 100) for s, r in zip(comparison['stock_index'], comparison['reference_index'])]
                baseline_group = _output_source_group(descriptor, 'reference:' + identity)
                comparison['source_groups'] = sorted({group, baseline_group})
                for n in (20, 60, 120):
                    if len(dates) < n + 1:
                        continue
                    start, end = dates[-n-1], dates[-1]
                    stock_return = _finite(stock[end]['close'] / stock[start]['close'] - 1)
                    reference_return = _finite(baseline[end]['close'] / baseline[start]['close'] - 1)
                    comparison['windows'][str(n)] = {'status': 'ok', 'stock_return': stock_return,
                                                    'reference_return': reference_return,
                                                    'excess_return': _finite(stock_return - reference_return),
                                                    'start': stock[start]['time'], 'end': stock[end]['time'],
                                                    'definition': f'{n} common completed bars require {n+1} prices; fractional returns and arithmetic excess'}
                    known = max(max(stock[at]['known'], baseline[at]['known']) for at in dates[-n-1:])
                    facts.append({'fact_id': f'background-{reference}-{n}', 'topic': 'background',
                                  'metric': f'{reference}_excess_return_{n}_common_bars',
                                  'value': stock_return - reference_return, 'time': stock[end]['time'],
                                  'first_known_at': known.isoformat(), 'data_cutoff': stock[end]['time'],
                                  'source_group': group, 'dependency_groups': comparison['source_groups'],
                                  'source': identity, 'relation': 'neutral', 'proposition_id': None,
                                  'definition': f'Stock return minus reference return over {n} common completed bars; fractional units',
                                  'falsification': 'Descriptive observation, not a directional prediction'})
            except (ValueError, TypeError, KeyError, ArithmeticError) as error:
                comparison.update(status='unavailable', common_dates=[], stock_index=[], reference_index=[], relative_index=[])
                comparison['limitations'].append(str(error))
        for item in comparisons:
            limitations.extend(f"{item['reference']}: {reason}" for reason in item['limitations'])
        partial = any(item['status'] != 'ok' or item.get('qualification') == 'weak_proxy' for item in comparisons)
        return {'status': 'insufficient_data' if not stock else 'partial' if partial else 'ok',
                'asof': asof, 'metadata': metadata, 'comparisons': comparisons, 'facts': facts,
                'propositions': [], 'limitations': limitations}
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        return {**_invalid(error), 'comparisons': [], 'facts': [], 'propositions': []}


def _evidence_json(value):
    import json
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _evidence_unavailable(metadata, cutoff):
    return [field + ' is after asof' for field in ('data_cutoff', 'first_known_at', 'snapshot_available_at',
                                                  'public_at', 'available_at', 'generated_at', 'research_asof', 'data_asof')
            if metadata.get(field) is not None and _moment(metadata[field]) > cutoff]


def evidence_table(technical_by_timeframe, background, fundamental_refs=None, *, asof):
    """Keep qualified observations, explicit gaps and source dependencies without scoring."""
    try:
        cutoff = _moment(asof)
        if not isinstance(technical_by_timeframe, dict) or not isinstance(background, dict):
            raise ValueError('Technical timeframes and background must be objects')
        if fundamental_refs is not None and not isinstance(fundamental_refs, list):
            raise ValueError('Fundamental references must be an array')
        containers, propositions, proposition_ids, limitations = [], [], {}, []
        for timeframe, result in technical_by_timeframe.items():
            if not isinstance(timeframe, str) or not isinstance(result, dict):
                raise ValueError('Each technical timeframe must contain an object')
            meta = result.get('metadata', {})
            if not isinstance(meta, dict):
                raise ValueError('Technical metadata must be an object')
            context = meta.get('context', {})
            if not isinstance(context, dict) or context.get('timeframe', timeframe) != timeframe or meta.get('timeframe', timeframe) != timeframe:
                raise ValueError('Technical timeframe identity is inconsistent')
            containers.append((timeframe, result, meta))
        containers.append(('background', background, background.get('metadata', {})))
        for timeframe, result, meta in containers:
            if not isinstance(meta, dict):
                raise ValueError('Background metadata must be an object')
            if _evidence_unavailable(meta, cutoff):
                limitations.append(timeframe + ': source unavailable; proposition contents excluded')
                continue
            declared = result.get('propositions', [])
            if not isinstance(declared, list):
                raise ValueError('Propositions must be an independent array')
            for proposition in declared:
                if not isinstance(proposition, dict) or not isinstance(proposition.get('proposition_id'), str) or not proposition['proposition_id'].strip():
                    raise ValueError('Proposition requires proposition_id')
                if _evidence_unavailable(proposition, cutoff):
                    limitations.append(timeframe + ': future proposition excluded')
                    continue
                identity, encoded = (timeframe, proposition['proposition_id']), _evidence_json(proposition)
                if identity in proposition_ids and proposition_ids[identity] != encoded:
                    raise ValueError('Conflicting definitions for proposition_id')
                if identity not in proposition_ids:
                    proposition_ids[identity] = encoded
                    propositions.append({**proposition, 'timeframe': timeframe})
        definitions = {(p['timeframe'], p['proposition_id']): p for p in propositions}
        rows, groups, seen_references = [], {}, set()

        def add(fact, timeframe, meta, *, fundamental=False):
            if not isinstance(fact, dict):
                raise ValueError('Evidence fact must be an object')
            _evidence_json(fact)
            result_id, evidence_id = fact.get('source_result_id'), fact.get('evidence_id')
            origin = f'fundamental:{result_id}:{evidence_id}' if fundamental else 'unverified-source'
            group = _output_source_group(meta, _output_source_group(fact, origin))
            if fundamental and not fact.get('effective_source_group') and not fact.get('source_sha256'):
                group = origin + ':' + str(fact.get('source_group', 'unknown-snapshot'))
            identity = fact.get('fact_id') or f'{timeframe}-{len(rows)+1}'
            proposition_id = fact.get('proposition_id')
            proposition = definitions.get((timeframe, proposition_id))
            relation = fact.get('relation', 'neutral')
            if relation not in {'support', 'conflict', 'neutral', 'missing', 'incomparable', 'not_run'}:
                raise ValueError('Unknown evidence relation')
            row = {'fact_id': identity, 'fact': fact.get('fact', fact.get('metric', identity)),
                   'value': fact.get('value'), 'relation': relation, 'status': fact.get('status', 'ok'),
                   'source': fact.get('source', f'{result_id}:{evidence_id}' if fundamental else group),
                   'proposition': (proposition.get('text') or proposition.get('definition') or proposition_id) if proposition else proposition_id,
                   'proposition_id': proposition_id, 'falsification': fact.get('falsification', proposition.get('falsification') if proposition else None),
                   'topic': fact.get('topic', 'fundamental' if fundamental else 'observation'), 'timeframe': timeframe,
                   'source_group': group, 'source_result_id': result_id, 'evidence_id': evidence_id,
                   'qualification': meta.get('qualification', 'diagnostic'),
                   'metric': fact.get('metric'), 'definition': fact.get('definition'), 'time': fact.get('time'),
                   'first_known_at': fact.get('first_known_at', meta.get('first_known_at')),
                   'data_cutoff': fact.get('data_cutoff', meta.get('data_cutoff'))}
            reasons = []
            if fundamental and (not isinstance(result_id, str) or not result_id.strip() or not isinstance(evidence_id, str) or not evidence_id.strip()):
                reasons.append('Fundamental reference lacks original result/evidence identity')
            if fundamental and not fact.get('public_at'):
                reasons.append('Fundamental evidence publication time is unknown')
            for field in ('time', 'first_known_at', 'data_cutoff'):
                value = row[field]
                if value is None:
                    if field != 'time':
                        reasons.append(field + ' is unknown')
                elif _moment(value) > cutoff:
                    reasons.append(field + ' is after asof')
            reasons.extend('Source ' + reason for reason in _evidence_unavailable(meta, cutoff))
            reasons.extend(_evidence_unavailable(fact, cutoff))
            if reasons:
                row.update(fact='Excluded or unqualified evidence: ' + str(identity), value=None, relation='missing', status='unavailable',
                           definition=None, falsification=None, proposition=None, proposition_id=None, metric=None, source=None)
                limitations.extend(str(identity) + ': ' + reason for reason in reasons)
            elif relation in {'support', 'conflict'} and (proposition is None or not row['falsification']):
                row.update(relation='missing', status='proposition_missing')
                limitations.append(str(identity) + ': directional relation requires a defined proposition and falsification')
            if fundamental:
                key = (result_id, evidence_id, group, _evidence_json({k: row[k] for k in ('metric', 'value', 'time', 'proposition_id', 'relation', 'definition', 'falsification')}))
                if key in seen_references:
                    return
                seen_references.add(key)
            rows.append(row)
            dependencies = []
            for field in ('dependency_groups', 'source_groups'):
                values = fact.get(field, [])
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ValueError('Dependency groups must be an array of source identities')
                dependencies.extend(values)
            for dependency in {group, *dependencies}:
                groups.setdefault(dependency, []).append(identity)

        topics = ('conventional', 'candlestick', 'ichimoku', 'harmonic', 'smc', 'turtle')
        for timeframe, result, meta in containers:
            if not isinstance(meta, dict):
                raise ValueError('Background metadata must be an object')
            facts = result.get('facts', [])
            if not isinstance(facts, list):
                raise ValueError('Facts must be an array')
            for fact in facts:
                add(fact, timeframe, meta)
            if timeframe == 'background':
                for comparison in result.get('comparisons', []):
                    if not isinstance(comparison, dict):
                        raise ValueError('Background comparison must be an object')
                    if comparison.get('status') != 'ok':
                        status = comparison.get('status', 'missing')
                        add({'fact_id': 'background-' + str(comparison.get('reference')), 'topic': 'background',
                             'metric': str(comparison.get('reference')) + ' comparison', 'value': None,
                             'relation': 'incomparable' if status == 'incomparable' else 'missing', 'status': status,
                             'first_known_at': asof, 'data_cutoff': asof}, timeframe, meta)
                continue
            declared = result.get('topics', {})
            if not isinstance(declared, dict):
                raise ValueError('Topics must be an object')
            for topic in topics:
                item = declared.get(topic)
                if item is None and any(isinstance(f, dict) and f.get('topic') == topic for f in facts):
                    continue
                if item is not None and not isinstance(item, dict):
                    raise ValueError('Topic result must be an object')
                status = item.get('status', 'not_run') if item is not None else 'not_run'
                if status == 'ok' and any(isinstance(f, dict) and f.get('topic') == topic for f in facts):
                    continue
                relation = 'not_run' if status in {'not_run', 'missing_fields', 'dependency_missing'} else 'incomparable' if status == 'incomparable' else 'neutral' if status in {'ok', 'no_candidates'} else 'missing'
                missing = item.get('missing_fields', []) if item is not None else []
                explanation = ('; missing ' + ', '.join(missing)) if missing else ''
                add({'fact_id': timeframe + '-' + topic + '-status', 'topic': topic, 'metric': topic + ': ' + status + explanation,
                     'value': None, 'relation': relation, 'status': status, 'first_known_at': asof, 'data_cutoff': asof}, timeframe, meta)
        for reference in fundamental_refs or []:
            add(reference, 'fundamental', {}, fundamental=True)
        return {'status': 'partial' if limitations else 'ok', 'rows': rows, 'propositions': propositions,
                'source_groups': sorted(groups), 'dependency_groups': groups, 'limitations': limitations}
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return {**_invalid(error), 'rows': [], 'propositions': [], 'source_groups': [], 'dependency_groups': {}}
