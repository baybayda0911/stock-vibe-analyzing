"""Read-only, common-date comparisons; no model, price feed or archive writes."""

from datetime import date, datetime, time, timedelta
from copy import copy, deepcopy
import math

from .dashboard import SHANGHAI, _day, _json


SCHEMA = 'stock-research-comparison/1'
BASES = {'cumulative': '年初至今累计', 'quarterly': '单季', 'annual': '全年'}
WINDOWS = {20, 60, 120, 252}
METRICS = (
    ('operating', 'revenue', '营业收入', 'CNY', '营业收入', ('revenue',)),
    ('operating', 'parent_profit', '归母净利润', 'CNY', '归属于母公司股东的净利润', ('parent_profit',)),
    ('operating', 'revenue_yoy', '营业收入同比', 'ratio', '本期营业收入／上年同期间营业收入−1；上年须大于0', ('revenue',)),
    ('operating', 'parent_profit_yoy', '归母净利润同比', 'ratio', '本期归母净利／上年同期间归母净利−1；上年须大于0', ('parent_profit',)),
    ('operating', 'gross_margin', '销售毛利率（合并）', 'ratio', '（营业收入−营业成本）／营业收入', ('revenue', 'cost')),
    ('operating', 'parent_net_margin', '归母净利率', 'ratio', '归母净利润／营业收入', ('parent_profit', 'revenue')),
    ('cash', 'cfo', '经营现金流净额', 'CNY', '同期合并经营活动现金流净额', ('cfo',)),
    ('cash', 'capex', '购建长期资产现金', 'CNY', '购建固定资产、无形资产等长期资产支付的现金', ('capex',)),
    ('cash', 'fcf', '简化自由现金流', 'CNY', '经营现金流−购建长期资产现金；非可分配股东现金', ('cfo', 'capex')),
    ('cash', 'cash_conversion', '现金转化率', 'ratio', '经营现金流／同期合并净利润；分母须大于0', ('cfo', 'consolidated_profit')),
    ('cash', 'receivables', '期末应收账款', 'CNY', '所选财报期末余额；不是期间流量', ('receivables',)),
    ('cash', 'inventory', '期末存货', 'CNY', '所选财报期末余额；不是期间流量', ('inventory',)),
)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _ratio(a, b):
    return a / b if _finite(a) and _finite(b) and b > 0 else None


def _request(store, security_ids, asof, period_basis, financial_period, price_window):
    if (not isinstance(security_ids, list) or not 2 <= len(security_ids) <= 5
            or any(not isinstance(sid, str) for sid in security_ids) or len(set(security_ids)) != len(security_ids)):
        raise ValueError('Select two to five distinct securities')
    for sid in security_ids: store.lab.identifier(sid, 'security_id')
    if period_basis not in BASES or type(price_window) is not int or price_window not in WINDOWS:
        raise ValueError('Select a supported financial basis and price window')
    now = store.lab.stamp(store.lab.now(), 'now').astimezone(SHANGHAI)
    day = now.date() if asof is None else _day(asof)
    if day is None or day > now.date(): raise ValueError('Observation date cannot be in the future')
    cutoff = now if day == now.date() else datetime.combine(day, time.max, SHANGHAI)
    if financial_period is not None:
        period = _day(financial_period)
        if period is None or period > day or period.strftime('%m-%d') not in {'03-31', '06-30', '09-30', '12-31'}:
            raise ValueError('Select a complete calendar financial period')
        if period_basis == 'annual' and period.month != 12: raise ValueError('Annual period must end in December')
    return cutoff.isoformat(), day.isoformat()


def _refs(row, fields):
    refs = []
    for field in fields:
        source = row.get('field_sources', {}).get(field, row.get('source'))
        if source and source not in refs: refs.append(source)
    return refs


def _table(view, basis):
    rows = list(view.get(basis, []))
    latest = view.get('latest_quarter' if basis == 'quarterly' else 'latest') if basis != 'annual' else None
    if latest and latest['report_date'] not in {r['report_date'] for r in rows}: rows.append(latest)
    expected = {'quarterly': 'single_quarter', 'annual': 'annual', 'cumulative': 'cumulative'}[basis]
    return {row['report_date']: row for row in rows if row.get('period_basis') == expected}


def _financial_cell(stock, table, period, basis, metric, fields):
    sid = stock['security']['security_id']
    row = table.get(period, {})
    used_rows = [row] if row else []
    refs = _refs(row, fields)
    reasons = []
    value = row.get(metric)
    if metric == 'gross_margin': value = _ratio(row.get('revenue', 0) - row.get('cost', 0), row.get('revenue')) if all(_finite(row.get(k)) for k in fields) else None
    elif metric == 'parent_net_margin': value = _ratio(row.get('parent_profit'), row.get('revenue'))
    elif metric == 'cash_conversion': value = _ratio(row.get('cfo'), row.get('consolidated_profit'))
    elif metric == 'fcf': value = row['cfo'] - row['capex'] if all(_finite(row.get(k)) for k in fields) else None
    elif metric.endswith('_yoy'):
        previous = table.get(str(int(period[:4]) - 1) + period[4:], {}) if period else {}
        if previous: used_rows.append(previous)
        ratio = _ratio(row.get(fields[0]), previous.get(fields[0]))
        value = ratio - 1 if ratio is not None else None
        refs += [ref for ref in _refs(previous, fields) if ref not in refs]
        if not previous: reasons.append('缺上年同期间可信金额，未用不同期间或券商预测补齐。')
    if not row: reasons.append('本证券没有所选期间及口径的正式财务组件。')
    if not _finite(value): value = None; reasons.append('缺少所需金额或分母非正，未填0或计算无意义比例。')
    status = 'ok' if value is not None else 'missing'
    if stock['security']['currency'] != 'CNY' or stock['finance'].get('units', {}).get('amount') != 'CNY_yuan':
        value = None; status = 'not_comparable'; reasons.append('金额币种或单位未统一，本版不猜测汇率换算。')
    proofs = [stock.get('units', {}).get((ref['result_id'], ref.get('attachment'))) for ref in refs]
    if row and (not proofs or not all(proof and proof['verified'] for proof in proofs)):
        value = None; status = 'not_comparable'
        reasons.append('源金额单位未经明示声明或注册血缘证明，未按量级猜元／万元或缩放为亿元。')
    for proof in proofs:
        if proof and proof['verified']:
            refs += [ref for ref in proof['source_refs'] if ref not in refs]
    conflicts = [r for r in stock['finance'].get('conflicts', []) if any(
        r['report_date'] == used['report_date'] and r.get('period_basis', used.get('period_basis')) == used.get('period_basis')
        for used in used_rows) and set(r['fields']) & set(fields)]
    if conflicts:
        status = 'not_comparable'; reasons.append('同期间历史组件仍有数值冲突；不据所选新值强排名。')
    if any(used.get('publish_date') is None for used in used_rows):
        status = 'not_comparable'; reasons.append('所选财务版本公开日期未知，不能认证共同知识时点。')
    return {'security_id': sid, 'value': value, 'status': status, 'reasons': reasons,
            'source_refs': refs, 'basis': {'currency': stock['security']['currency'], 'period': period,
                'period_basis': basis, 'scope': 'parent' if metric.startswith('parent_') else 'consolidated',
                'publish_date': row.get('publish_date'), 'date_semantics': '源版本公开日期；历史表不保证为首次披露日'}}


def _row(metric, label, unit, formula, values, *, kind='financial', force_reason=None):
    if force_reason:
        comparable, reason = 'not_comparable', force_reason
    elif any(v['status'] == 'not_comparable' for v in values):
        comparable, reason = 'not_comparable', '至少一股的期间、单位、公开日期或数值资格未核齐；仅并列原值。'
    elif any(v['value'] is None for v in values):
        comparable, reason = 'unavailable', '至少一股缺数，未生成全组排名。'
    elif any(v['status'] == 'diagnostic' for v in values):
        comparable, reason = 'diagnostic', '同窗名义原价诊断；权息和日历未完全认证，不是可投资总回报排名。'
    else:
        comparable, reason = 'comparable', '相同财报期／期间口径、单位和计算分母；可比较已归档事实，不提升交易资格。'
    return {'metric': metric, 'label': label, 'unit': unit, 'formula': formula, 'kind': kind,
            'comparability': comparable, 'reason': reason, 'values': values}


def _financial_units(store, context, finance):
    """Qualify only reviewed legacy money declarations, never infer a scale."""
    records = {row['result_id']: row for row in context['results']}
    refs = [value for value in finance.get('components', {}).values() if isinstance(value, dict)]
    rows = list(finance.get('annual', [])) + list(finance.get('quarterly', [])) + list(finance.get('cumulative', []))
    rows += [finance[key] for key in ('latest', 'latest_quarter') if isinstance(finance.get(key), dict)]
    for row in rows:
        refs += [row['source']] if isinstance(row.get('source'), dict) else []
        refs += [value for value in row.get('field_sources', {}).values() if isinstance(value, dict)]
    output = {}
    for ref in refs:
        key = (ref.get('result_id'), ref.get('attachment'))
        if key in output or key[1] not in {'financial-bottom-tables.json', 'calculations.json'}: continue
        proof = {'verified': False, 'reason': '原组件没有已识别的金额单位声明，不能按数值量级猜测元／万元。', 'source_refs': []}
        output[key] = proof
        record = records.get(key[0])
        if record is None or record.get('module') != 'fundamental': continue
        names = {row['relative_path']: row for row in record.get('attachments', [])}
        if key[1] not in names: continue
        reader = store._record_reader(record)
        document = _json(reader(key[1])[0])
        identity = store.lab.security(record['security'])
        if identity != store.lab.security(context['security']) or 'security' in document and store.lab.security(document['security']) != identity:
            raise ValueError('Financial unit evidence has a different security')
        source = {'result_id': record['result_id'], 'report_id': 'result:' + record['result_id'],
                  'attachment': key[1], 'sha256': names[key[1]]['sha256'],
                  'field_path': 'unit' if 'unit' in document else 'units' if 'units' in document else None}
        proof['source_refs'] = [source]
        if 'schema_version' in document or 'schema' in document:
            proof['reason'] = '财务组件模式未知，单位声明未通过有限适配。'; continue
        declared = [document[name] for name in ('unit', 'units') if name in document]
        known_units = ('CNY', 'CNY_yuan', 'CNY yuan', 'CNY unless field is ratio/date/share/price')
        if declared:
            if not all(type(value) is str and value in known_units for value in declared):
                proof['reason'] = '原组件金额单位不属于已识别的CNY元声明，禁止猜测缩放。'; continue
            proof.update(verified=True, reason='原组件明确声明CNY元，金额与比率／日期／股数分别解释。'); continue
        if key[1] == 'calculations.json' and isinstance(document.get('financial_period'), str) and all(
                isinstance(document.get(name), dict) for name in ('current_inputs_Q3_CNY', 'prior_9M_inputs_Q3_CNY', 'q3_difference_CNY')):
            proof.update(verified=True, reason='已识别增量格式的金额分组以_CNY明示元；比率从同期间金额重算。')
            source['field_path'] = 'current_inputs_Q3_CNY / prior_9M_inputs_Q3_CNY / q3_difference_CNY'; continue
        if 'comparison-ready.json' not in names: continue
        bridge = _json(reader('comparison-ready.json')[0])
        unit_text = 'CNY amounts in yuan; ratios in fractions, multiples not percentages. Display in 亿 = divide by 1e8.'
        if (bridge.get('schema') != 'stock-fundamental-comparison/1' or bridge.get('module') != 'fundamental'
                or bridge.get('metric_units') != unit_text
                or bridge.get('financial_bottom_tables') != 'financial-bottom-tables.json'):
            proof['reason'] = '配套比较组件没有已识别的CNY元／小数声明。'; continue
        if store.lab.security(bridge.get('security')) != identity:
            raise ValueError('Financial unit bridge has a different security')
        original_id = bridge.get('result_id')
        current_record, current_document, visited = record, document, set()
        chain_verified = False
        while original_id != current_record['result_id']:
            inherited = current_document.get('s4_inheritance', {})
            original = records.get(inherited.get('source_result_id'))
            if (original is None or original.get('module') != 'fundamental' or store.lab.security(original['security']) != identity
                    or current_record['result_id'] in visited or inherited.get('values_unchanged') is not True):
                proof['reason'] = '单位声明属于另一成果，缺少可核的同证券无环继承链。'; break
            visited.add(current_record['result_id'])
            original_names = {row['relative_path']: row for row in original.get('attachments', [])}
            if (key[1] not in original_names or 'comparison-ready.json' not in original_names
                    or inherited.get('sha256') != original_names[key[1]]['sha256']
                    or names['comparison-ready.json']['sha256'] != original_names['comparison-ready.json']['sha256']):
                proof['reason'] = '单位声明的原附件哈希或比较组件血缘不一致。'; break
            original_reader = store._record_reader(original)
            original_reader('comparison-ready.json')
            original_document = _json(original_reader(key[1])[0])
            inherited_document = {name: value for name, value in current_document.items() if name != 's4_inheritance'}
            original_values = {name: value for name, value in original_document.items() if name != 's4_inheritance'}
            if 'result_id' in inherited_document: inherited_document['result_id'] = original['result_id']
            notice = '本次沿用已归档计算，财务期、行情和原计算生成日期未更新；本次仅形成新的研究解释。'
            if isinstance(original_values.get('limits', []), list) and inherited_document.get('limits') == original_values.get('limits', []) + [notice]:
                if 'limits' in original_values: inherited_document['limits'] = original_values['limits']
                else: inherited_document.pop('limits')
            if inherited_document != original_values:
                proof['reason'] = '继承组件金额已变化，原单位声明不能直接续用。'; break
            proof['source_refs'].append({'result_id': original['result_id'], 'report_id': 'result:' + original['result_id'],
                'attachment': key[1], 'sha256': original_names[key[1]]['sha256'], 'field_path': 'registered_original'})
            current_record, current_document = original, original_document
        else:
            chain_verified = True
        if not chain_verified: continue
        if key[1] == 'calculations.json':
            values = document.get('latest_ytd', {}); metrics = bridge.get('metrics', {})
            pairs = [('revenue', 'revenue_ytd_cny'), ('parent_profit', 'parent_profit_ytd_cny'),
                ('consolidated_profit', 'consolidated_profit_ytd_cny'), ('cfo', 'cfo_ytd_cny'), ('capex', 'capex_ytd_cny')]
            if (not isinstance(document.get('latest_quarter'), dict) or not isinstance(values, dict) or not isinstance(metrics, dict)
                    or any(name not in values or target not in metrics or
                        not (values[name] is None and metrics[target] is None) and
                        (not _finite(values[name]) or not _finite(metrics[target]) or values[name] != metrics[target])
                        for name, target in pairs)):
                proof['reason'] = '比较组件的明示元金额与正式计算不一致。'; continue
        proof['source_refs'].append({'result_id': record['result_id'], 'report_id': 'result:' + record['result_id'],
            'attachment': 'comparison-ready.json', 'sha256': names['comparison-ready.json']['sha256'], 'field_path': 'metric_units'})
        proof.update(verified=True, reason='已识别比较组件明示CNY元／小数，证券、注册附件及继承血缘一致。')
    return output


def _raw_prices(store, sid, tech, asof):
    """Use the same nominal raw convention, never a normalized mix of raw and qfq."""
    if not tech.get('source') or not tech.get('bars'): return None
    source = tech['source']
    context = store.context(sid, asof, module='technical')
    record = next(r for r in context['results'] if r['result_id'] == source['result_id'])
    names = {a['relative_path']: a for a in record['attachments']}
    reader = store._record_reader(record)
    analysis = _json(reader('analysis.json')[0])
    identity = store.lab.security(record['security'])
    if (record.get('module') != 'technical' or analysis.get('schema_version') != 'stock-technical-report/2' or identity['security_id'] != sid
            or store.lab.security(analysis.get('security')) != identity):
        raise ValueError('Raw price evidence has a different security or analysis schema')
    frame = analysis.get('timeframes', {}).get('1d', {}); meta = frame.get('metadata', {})
    binding = meta.get('context', {})
    mode = (tech.get('adjustment') or {}).get('mode')
    if (binding.get('security_id') != sid or binding.get('timeframe') != '1d'
            or store.lab.security(meta.get('security')) != identity or meta.get('data_asof') != tech.get('data_asof')
            or meta.get('adjustment', {}).get('mode') != mode):
        raise ValueError('Raw price evidence differs from the displayed daily source')
    if mode == 'raw':
        originals = {store.lab.stamp(row['time'], 'bar.time').astimezone(SHANGHAI).date().isoformat(): row['close'] for row in frame['bars'] if row.get('completed') is True}
        points = {}
        for row in tech['bars']:
            if (_day(row.get('date')) is None or not _finite(row.get('close')) or row['close'] <= 0
                    or row['close'] != originals.get(row['date']) or points and row['date'] <= next(reversed(points))):
                raise ValueError('Displayed raw prices differ from their registered analysis')
            points[row['date']] = row['close']
        return {'points': points, 'source_refs': [source], 'basis': 'raw',
                'qualified': tech['qualification'] == meta.get('qualification') == 'validated', 'events': []}
    if mode != 'qfq' or not {'raw-ohlcv-snapshot.json', 'reference-definition.json'} <= names.keys(): return None
    raw = _json(reader('raw-ohlcv-snapshot.json')[0]); definition = _json(reader('reference-definition.json')[0])
    if (raw.get('qualification') != 'diagnostic' or not isinstance(raw.get('bars'), list)
            or not isinstance(raw.get('fields'), dict) or raw['fields'].get('close') != '收盘价'
            or not raw.get('source_sha256') or raw['source_sha256'] != definition.get('source_raw_sha256')
            or definition.get('method') != 'diagnostic-provider-reference-close-chain/1'
            or not definition.get('output_sha256') or definition['output_sha256'] != meta.get('input_sha256')
            or definition['output_sha256'] != meta.get('source_sha256')):
        raise ValueError('Raw snapshot must match its registered reference definition')
    extracted = store.lab.stamp(raw['extracted_at'], 'extracted_at')
    if extracted > store.lab.stamp(record['generated_at'], 'generated_at') or extracted > store.lab.stamp(asof, 'asof'):
        raise ValueError('Raw snapshot is not yet visible at the observation time')
    points = {}
    actual_dates = [store.lab.stamp(row['time'], 'bar.time').astimezone(SHANGHAI).date().isoformat() for row in frame.get('bars', []) if row.get('completed') is True]
    if len(actual_dates) != len(raw['bars']): raise ValueError('Raw snapshot daily coverage differs from its analysis')
    for index, row in enumerate(raw['bars']):
        day = _day(row.get('date'))
        close = row.get('close')
        if (day is None or row['date'] != actual_dates[index] or (points and row['date'] <= next(reversed(points)))
                or any(not _finite(row.get(name)) or row[name] <= 0 for name in ('open', 'high', 'low', 'close'))
                or not row['low'] <= min(row['open'], close) <= max(row['open'], close) <= row['high']):
            raise ValueError('Raw snapshot dates or OHLC are invalid')
        if day.isoformat() > tech['data_asof'][:10]: raise ValueError('Raw prices exceed the frozen market cutoff')
        points[day.isoformat()] = close
    if not points or next(reversed(points)) != tech['data_asof'][:10]: raise ValueError('Raw snapshot has a different end date')
    if definition.get('anchor_date') != next(reversed(points)) or definition.get('anchor_raw_close') != points[next(reversed(points))]:
        raise ValueError('Raw snapshot anchor differs from its reference definition')
    created = store.lab.stamp(definition['created_at'], 'created_at')
    if created > store.lab.stamp(record['generated_at'], 'generated_at') or created > store.lab.stamp(asof, 'asof'):
        raise ValueError('Reference definition is not yet visible at the observation time')
    events = definition.get('implemented_action_dates', [])
    if not isinstance(events, list) or any(_day(day) is None for day in events):
        raise ValueError('Reference definition action dates are invalid')
    return {'points': points, 'source_refs': [{**source, 'attachment': name, 'sha256': names[name]['sha256'],
             'field_path': 'bars.close' if name == 'raw-ohlcv-snapshot.json' else 'source_raw_sha256'}
            for name in ('raw-ohlcv-snapshot.json', 'reference-definition.json')], 'basis': 'raw', 'qualified': False,
            'events': events}


def _prices(stocks, window):
    result = {'status': 'unavailable', 'reason': '缺少各股同口径原价序列。', 'rebased_to': 100,
              'basis': 'CNY名义原价／共同日期起点100；不含分红再投，未消除拆股、除息等机械变化。',
              'window': {'requested_intervals': window, 'intervals': 0, 'points': 0, 'start': None, 'end': None},
              'dates': [], 'series': [], 'excluded': []}
    if any(s['raw'] is None or s['security']['currency'] != 'CNY' for s in stocks):
        result.update(status='not_comparable', reason='原价／参考价不能混图；至少一股缺可核CNY原价，未以归一100掩盖口径差异。')
        return result
    dates = sorted(set.intersection(*(set(s['raw']['points']) for s in stocks)))
    dates = dates[-window-1:]
    if len(dates) != window + 1:
        result['reason'] = '共同日期不足所选N+1价格点，未把较短窗口当完整窗口。'
        return result
    result.update(status='diagnostic', reason='已统一CNY名义原价与N+1共同日期；权息／交易日历未完全认证，仅供价格轨迹诊断。', dates=dates,
                  window={'requested_intervals': window, 'intervals': window, 'points': len(dates), 'start': dates[0], 'end': dates[-1]})
    for stock in stocks:
        raw = stock['raw']; initial = raw['points'][dates[0]]
        result['series'].append({'security_id': stock['security']['security_id'],
            'values': [raw['points'][d] / initial * 100 for d in dates], 'source_refs': raw['source_refs']})
        omitted = sum(dates[0] <= d <= dates[-1] and d not in dates for d in raw['points'])
        events = [d for d in raw['events'] if dates[0] <= d <= dates[-1]]
        result['excluded'].append({'security_id': stock['security']['security_id'], 'noncommon_dates': omitted,
            'known_action_dates': events, 'reason': '交集只对齐观察日期，不认证连续交易日历或全部公司行为；未知事件不等于没有事件。'})
    return result


def _conclusions(sections, names):
    statements = []
    rows = {r['metric']: r for section in sections for r in section['rows']}
    for key, limit in [('revenue', '规模差异不代表增长质量或投资潜力优劣。'),
                       ('gross_margin', '必须结合产品结构、费用和现金，不将毛利率单项当护城河评分。'),
                       ('cash_conversion', '合并净利润作分母；单期现金受回款和备料影响，仍需后续现金桥验证。')]:
        row = rows[key]
        if row['comparability'] != 'comparable': continue
        high, low = max(row['values'], key=lambda v: v['value']), min(row['values'], key=lambda v: v['value'])
        if math.isclose(high['value'], low['value'], rel_tol=1e-9, abs_tol=1e-9): continue
        values = '；'.join(names[v['security_id']] + ('：' + f"{v['value']/1e8:.2f}亿元" if row['unit']=='CNY'
                              else '：' + f"{v['value']*100:.2f}%") for v in row['values'])
        statements.append({'text': f"{high['basis']['period']}，{BASES[high['basis']['period_basis']]}口径：{values}。{names[high['security_id']]}的{row['label']}较高；只针对这一指标。",
            'basis_refs': [{'metric': key, 'security_id': v['security_id'], 'source_refs': v['source_refs']} for v in row['values']], 'limits': [limit]})
    statements.append({'text': 'PE股本范围／估值情景期限和技术价格链须分别核对；本页不产生综合分或投资潜力总排名。',
                       'basis_refs': [], 'limits': ['研报预测不是实际兑现；名义价格变化不是总收益。']})
    return statements


def compare(store, security_ids, *, asof=None, period_basis='cumulative', financial_period=None, price_window=60):
    cutoff, asof_day = _request(store, security_ids, asof, period_basis, financial_period, price_window)
    snapshot = store._scan()
    store = copy(store)
    # One request sees one catalog; every actual attachment read still verifies its registered SHA.
    store._scan = lambda: deepcopy(snapshot)
    stocks, summaries, tables = [], [], []
    for sid in security_ids:
        context = store.context(sid, cutoff)
        finance = store.fundamental_dashboard(sid, cutoff)
        technical = store.technical_dashboard(sid, asof=cutoff, limit=2000)
        raw = _raw_prices(store, sid, technical, cutoff)
        units = _financial_units(store, context, finance)
        records = {record['result_id']: record for record in context['results']}
        for proof in units.values():
            for ref in proof['source_refs']:
                origin = records.get(ref['result_id'], {})
                ref.update({key: origin[key] for key in ('generated_at', 'research_asof', 'data_asof') if key in origin})
        stock = {'security': context['security'], 'finance': finance, 'technical': technical, 'raw': raw, 'units': units}
        stocks.append(stock); tables.append(_table(finance, period_basis))
        summaries.append({'security': context['security'], 'security_id': sid,
            'status': 'ok' if finance.get('source') or technical.get('source') else 'no_research',
            'reason': None if finance.get('source') or technical.get('source') else '观察截止前无合格正式研究；未借后来成果补齐。',
            'financial': {'source': finance.get('source'), 'period': None, 'publish_date': None,
                'generated_at': (finance.get('source') or {}).get('generated_at'), 'research_asof': (finance.get('source') or {}).get('research_asof')},
            'technical': {key: technical.get(key) for key in ('source', 'data_asof', 'generated_at', 'adjustment', 'qualification', 'summary')}})
    periods = sorted(set.intersection(*(set(table) for table in tables)), reverse=True)
    period = financial_period or (periods[0] if periods else None)
    for table, summary in zip(tables, summaries):
        row = table.get(period, {})
        summary['financial'].update(period=row.get('report_date'), publish_date=row.get('publish_date'))
    sections = [{'id': key, 'label': label, 'rows': []} for key, label in
                [('operating', '经营与增长'), ('cash', '现金兑现与资金占用'), ('valuation', '估值与价格要求'), ('technical', '市场与技术')]]
    by_id = {s['id']: s for s in sections}
    for group, metric, label, unit, formula, fields in METRICS:
        cells = [_financial_cell(stock, table, period, period_basis, metric, fields) for stock, table in zip(stocks, tables)]
        by_id[group]['rows'].append(_row(metric, label, unit, formula, cells))
    price_series = _prices(stocks, price_window)
    valuation_cells, close_cells, change_cells = [], [], []
    for stock in stocks:
        sid = stock['security']['security_id']; valuation = stock['finance']['valuation']; tech = stock['technical']
        valuation_cells.append({'security_id': sid, 'value': valuation.get('pe_ttm_parent'),
            'status': 'not_comparable', 'reasons': [valuation.get('share_basis', '股本与股类范围未认证。'),
                '原估值行情日：' + str(valuation.get('market_date') or '未知') + '；原计算最新财务期：'
                + str((stock['finance'].get('latest') or {}).get('report_date') or '未知') + '。本行与上方财务期间选择分开。',
                '各自原估值仅并列；未认证发行人股本、A/H价格及TTM期间一致，不作便宜排名。'],
            'source_refs': [valuation['source']] if valuation.get('source') else [],
            'basis': {'currency': stock['security']['currency'], 'market_date': valuation.get('market_date'),
                      'parent_profit_ttm': valuation.get('parent_profit_ttm'), 'share_basis': valuation.get('share_basis')}})
        raw = stock['raw']; end = price_series['window']['end']
        value = raw['points'].get(end) if raw and end else None
        for cells, metric_value in [(close_cells, value), (change_cells,
                _ratio(value, raw['points'].get(price_series['window']['start'])) - 1 if value is not None and raw else None)]:
            cells.append({'security_id': sid, 'value': metric_value, 'status': 'diagnostic' if metric_value is not None else 'missing',
                'reasons': [price_series['reason']], 'source_refs': raw['source_refs'] if raw else [],
                'basis': {'currency': stock['security']['currency'], 'price_mode': 'raw', 'date': end,
                          'window': price_series['window'], 'total_return': False}})
    by_id['valuation']['rows'].append(_row('pe_ttm_parent', '原归档归母TTM PE', 'multiple', '各股原报告市值口径／归母TTM，股本范围未统一', valuation_cells,
        kind='valuation', force_reason='股本范围与估值基础不同，保留原值和原日期，不作统一PE排名。'))
    by_id['technical']['rows'] += [
        _row('raw_close', '共同末日原价', 'CNY/share', '同一共同观察日名义原价；股价高低不代表贵便宜', close_cells, kind='price'),
        _row('nominal_price_change', f'{price_window}个共同间隔名义价格变化', 'ratio', '共同末日原价／共同起日原价−1；非投资总收益', change_cells, kind='price')]
    result = {'schema_version': SCHEMA, 'asof': cutoff, 'request': {'security_ids': security_ids, 'asof_date': asof_day,
        'period_basis': period_basis, 'financial_period': period, 'price_window': price_window},
        'securities': summaries, 'financial_periods': periods, 'financial_period': period, 'sections': sections,
        'price_series': price_series, 'conclusions': _conclusions(sections, {s['security']['security_id']:s['security']['company'] for s in stocks}), 'research_executed': False,
        'limitations': ['本页只读正式档案和明示附件，不调用模型、Wind或行情服务，不生成或改写研究成果。',
            '共同观察截止是平台可见时点；财报期、源版本公开日期、行情末端、计算及报告生成分别保留。',
            '历史表可能使用后续比较版本，公开日期不保证是首次披露日，禁止据此回填当时已知或预测命中。',
            '现金转化=CFO/合并净利润；归母利润用于股东盈利，单季/累计/全年不能混排。',
            '本页比较已存事实，不延长旧判断有效期或提升未认证的价格、公司行为及交易日历资格。',
            '原价轨迹已统一名义口径，仍未消除全部权息；旧技术指标维持各自原价/参考链，不强技术排名。',
            'PE股本范围、估值情景期限和假设未统一；不比较目标空间、未来收益或给投资潜力总排名。']}
    return result
