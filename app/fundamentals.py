"""Read-only financial projections from explicitly archived, date-visible research."""

from datetime import date, datetime
import math
import re

from .dashboard import SHANGHAI, _day, _json


ALIASES = {
    'revenue': ('revenue',), 'cost': ('cost',), 'parent_profit': ('parent_profit', 'net_profit_parent'),
    'consolidated_profit': ('consolidated_profit', 'net_profit_consolidated'),
    'cfo': ('cfo',), 'capex': ('capex', 'capex_cash'), 'inventory': ('inventory',),
    'receivables': ('receivables',), 'cash': ('cash',), 'assets': ('assets',), 'liabilities': ('liabilities',),
    'parent_equity': ('parent_equity', 'equity_parent'), 'share_capital': ('share_capital',),
    'gross_margin': ('gross_margin',), 'fcf': ('fcf', 'simple_fcf'),
    'parent_profit_yoy': ('parent_profit_yoy', 'net_profit_yoy'),
    'parent_profit_qoq': ('parent_profit_qoq', 'net_profit_qoq'), 'revenue_yoy': ('revenue_yoy',),
    'revenue_qoq': ('revenue_qoq',), 'raw_close': ('raw_close',), 'reference_close': ('reference_close',),
    'pe_known': ('pe_known',), 'market_cap': ('market_cap',),
}
FLOW = ('revenue', 'cost', 'parent_profit', 'consolidated_profit', 'cfo', 'capex')


def _num(value):
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value):
        value = float(value)
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Financial values must be finite numbers or null')
    return value


def _ratio(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def _research_day(record):
    return datetime.fromisoformat(record['research_asof']).astimezone(SHANGHAI).date().isoformat()


def _source(record, attachment=None, field=None):
    value = {key: record.get(key) for key in ('result_id', 'report_id', 'generated_at', 'research_asof', 'data_asof',
                                             'historical_reconstruction', 'current_eligible', 'reasons')}
    if attachment:
        value.update(attachment=attachment, field_path=field,
                     sha256=next(row['sha256'] for row in record.get('attachments', []) if row['relative_path'] == attachment))
    return value


def _read(store, record, name):
    payload = store._record_reader(record)(name)[0]
    value = _json(payload)
    if 'security' in value and store.lab.security(value['security']) != record['security']:
        raise ValueError('Financial attachment and archived security identity differ')
    if 'result_id' in value and value['result_id'] != record['result_id']:
        raise ValueError('Financial attachment result identity differs')
    for key in ('generated_at', 'research_asof', 'market_cutoff', 'market_asof'):
        if value.get(key) is not None:
            ceiling = record['generated_at'] if key == 'generated_at' else record['research_asof']
            if store.lab.stamp(value[key], key) > store.lab.stamp(ceiling, 'frozen cutoff'):
                raise ValueError('Financial attachment exceeds its frozen research date')
    return value


def _canonical(row, record, attachment, field, *, report_date=None, publish_date=None):
    if not isinstance(row, dict):
        raise ValueError('Financial rows must be objects')
    report_date = row.get('report_date', report_date)
    publish_date = row.get('publish_date', publish_date)
    _day(report_date)
    if report_date is None:
        raise ValueError('Financial period is missing')
    if report_date > _research_day(record):
        raise ValueError('Financial period exceeds frozen research cutoff')
    if publish_date is not None:
        _day(publish_date)
        if publish_date > _research_day(record):
            raise ValueError('Financial row was not published by its frozen research cutoff')
    value = {'report_date': report_date, 'publish_date': publish_date, 'source': _source(record, attachment, field)}
    for key, aliases in ALIASES.items():
        supplied = [_num(row[name]) for name in aliases if name in row]
        if len(supplied) > 1 and any(item != supplied[0] for item in supplied[1:]):
            raise ValueError('Financial aliases conflict')
        value[key] = supplied[0] if supplied else None
    for key in ('price_date', 'known_period'):
        value[key] = row.get(key)
        if value[key] is not None:
            _day(value[key])
    if value['price_date'] is not None and value['price_date'] > _research_day(record):
        raise ValueError('Financial history price exceeds frozen research cutoff')
    if value['revenue'] is not None and value['cost'] is not None:
        value['gross_margin'] = _ratio(value['revenue'] - value['cost'], value['revenue'])
    if value['cfo'] is not None and value['capex'] is not None:
        value['fcf'] = value['cfo'] - value['capex']
    value['cash_conversion'] = _ratio(value['cfo'], value['consolidated_profit'])
    value['parent_net_margin'] = _ratio(value['parent_profit'], value['revenue'])
    value['period_basis'] = 'single_quarter' if '.quarterly' in field or 'latest_quarter' in field or 'q3_difference' in field else 'cumulative'
    return value


def _tables(document, record):
    if 'schema_version' in document:
        return None
    tables = document.get('tables', document)
    result = {}
    for key in ('annual', 'quarterly', 'cumulative'):
        rows = tables.get(key)
        if isinstance(rows, dict):
            rows = rows.get('rows')
        if not isinstance(rows, list):
            return None
        result[key] = [_canonical(row, record, 'financial-bottom-tables.json', f'tables.{key}[{index}]')
                       for index, row in enumerate(rows)]
        if len({row['report_date'] for row in result[key]}) != len(result[key]):
            raise ValueError('Duplicate periods in financial table')
        result[key].sort(key=lambda row: row['report_date'])
        if key == 'annual':
            for row in result[key]:
                row['period_basis'] = 'annual'
    return result


def _growth(rows, step):
    indexed = {row['report_date']: row for row in rows}
    for row in rows:
        day = date.fromisoformat(row['report_date'])
        prior_year = indexed.get(f'{day.year - 1:04d}-{day.month:02d}-{day.day:02d}')
        for metric in ('revenue', 'parent_profit'):
            if prior_year:
                ratio = _ratio(row[metric], prior_year[metric])
                row[metric + '_yoy'] = None if ratio is None else ratio - 1
        if step == 'quarterly':
            month = day.year * 12 + day.month - 4
            year, zero = divmod(month, 12)
            end = (31, 30, 30, 31)[zero // 3]
            previous = indexed.get(f'{year:04d}-{zero + 1:02d}-{end:02d}')
            if previous:
                for metric in ('revenue', 'parent_profit'):
                    ratio = _ratio(row[metric], previous[metric])
                    row[metric + '_qoq'] = None if ratio is None else ratio - 1


def _public_date(record, period):
    matches = [item for item in record.get('evidence', []) if item.get('financial_period', '').endswith(period)
               or item.get('balance_date') == period]
    dates = [item.get('public_date') or (item.get('public_at') or '')[:10] for item in matches]
    return next((value for value in dates if value), None)


def _calculation(store, record):
    names = {row['relative_path'] for row in record.get('attachments', [])}
    if 'calculations.json' not in names:
        return None
    value = _read(store, record, 'calculations.json')
    if 'schema_version' in value:
        return None
    if isinstance(value.get('latest_ytd'), dict) and isinstance(value.get('latest_quarter'), dict):
        return value, 'full'
    if (isinstance(value.get('financial_period'), str) and isinstance(value.get('current_inputs_Q3_CNY'), dict)
            and isinstance(value.get('prior_9M_inputs_Q3_CNY'), dict)
            and isinstance(value.get('q3_difference_CNY'), dict) and isinstance(value.get('valuation'), dict)):
        return value, 'incremental'
    return None


def _opening(store, record):
    report = store._record_reader(record)()[0].decode('utf-8-sig')
    for line in report.splitlines():
        line = line.strip().lstrip('> ').strip()
        if line.startswith('**') and line.count('**') >= 2:
            return line.split('**', 2)[1][:650]
    paragraphs = [line.strip() for line in report.splitlines() if line.strip() and not line.startswith(('#', '|', '-', '`'))]
    return paragraphs[0][:650] if paragraphs else '已有正式研究；原报告没有可提取的首段结论。'


def _merge(rows, new, conflicts):
    old = next((row for row in rows if row['report_date'] == new['report_date']), None)
    if old:
        changed = [key for key in ALIASES if old[key] is not None and new[key] is not None
                   and not math.isclose(old[key], new[key], rel_tol=1e-9, abs_tol=1e-6)]
        if changed:
            conflicts.append({'report_date': new['report_date'], 'fields': changed, 'previous_source': old['source'],
                              'period_basis': new['period_basis'],
                              'selected_source': new['source'], 'resolution': '显示最新正式组件，原归档数值和差异来源保留。'})
        for key in ('raw_close', 'reference_close', 'price_date', 'pe_known', 'known_period', 'market_cap'):
            if new[key] is None and old[key] is not None:
                new[key] = old[key]
                new.setdefault('field_sources', {})[key] = old['source']
        rows.remove(old)
    rows.append(new)
    rows.sort(key=lambda row: row['report_date'])


def fundamental_dashboard(store, security_id, asof=None):
    context = store.context(security_id, asof, module='fundamental')
    roles = context['roles']
    selected = roles['formal_fundamental']
    result = {'schema_version': 'stock-fundamental-dashboard/1', 'security': context['security'],
              'status': 'no_research', 'source': None, 'components': {'history': None, 'latest': None, 'valuation': None},
              'overview': {'conclusion': '尚无可识别的正式基本面研究。', 'highlights': [], 'next_checks': []},
              'annual': [], 'quarterly': [], 'cumulative': [], 'latest': None, 'latest_quarter': None, 'valuation': {},
              'observations': [], 'events': [], 'conflicts': [], 'limitations': [], 'price_basis': None,
              'units': {'amount': 'CNY_yuan', 'ratio': 'fraction', 'price': context['security']['currency']},
              'roles': roles, 'research_executed': False}
    if selected is None:
        if context['results']:
            result['status'] = 'unavailable'
            result['limitations'].append('已有历史文件，但角色或财务模式未知，未用任意附件替代正式基本面。')
        return result
    records = {row['result_id']: row for row in context['results']}
    primary = records[selected['result_id']]
    result['source'] = _source(primary)
    result['overview']['conclusion'] = _opening(store, primary)
    # Follow explicit parent lineage; unrelated reports cannot silently become inherited history.
    allowed, pending = {primary['result_id']}, list(primary.get('parent_result_ids', []))
    while pending:
        key = pending.pop()
        if key in allowed or key not in records:
            continue
        allowed.add(key)
        pending.extend(records[key].get('parent_result_ids', []))
    full_ids = {row['result_id'] for row in roles['classified'] if row['content_role'] == 'full_research'}
    candidates = [row for row in context['results'] if row['result_id'] in allowed & full_ids
                  and any(item['relative_path'] == 'financial-bottom-tables.json' for item in row.get('attachments', []))]
    history_record = candidates[0] if candidates else None
    tables = None
    if history_record:
        history = _read(store, history_record, 'financial-bottom-tables.json')
        tables = _tables(history, history_record)
        if tables is not None:
            result['components']['history'] = _source(history_record, 'financial-bottom-tables.json')
            result['price_basis'] = history.get('price_reference') or {'kind': 'archived_raw_diagnostic', 'limits': '原归档期末原价；权息未认证，不按视觉交叉推断经营与定价因果。'}
            result['annual'], result['quarterly'] = tables['annual'], tables['quarterly']
            result['cumulative'] = tables['cumulative']
            result['latest'] = tables['cumulative'][-1] if tables['cumulative'] else None
            result['latest_quarter'] = tables['quarterly'][-1] if tables['quarterly'] else None
    calculation = _calculation(store, primary)
    if calculation:
        document, kind = calculation
        source = _source(primary, 'calculations.json')
        if kind == 'full':
            latest = _canonical(document['latest_ytd'], primary, 'calculations.json', 'latest_ytd')
            quarter = _canonical(document['latest_quarter'], primary, 'calculations.json', 'latest_quarter')
            valuation = {'raw_close': _num(document.get('market', {}).get('raw_close')),
                         'market_date': document.get('market', {}).get('date'),
                         'parent_profit_ttm': _num(document.get('ttm', {}).get('parent_profit')),
                         'pe_ttm_parent': _num(document.get('ratios', {}).get('ttm_pe_parent_profit')),
                         'share_basis': '归档市值／归母TTM；总股本与股类范围保持原报告说明，不据A股代码推断发行人只有A股。',
                         'scenarios': document.get('valuation_scenarios', []), 'scenarios_source': source,
                         'source': source}
            for metric in ('revenue', 'parent_profit'):
                previous = document.get('prior_ytd', {}).get(metric)
                ratio = _ratio(latest[metric], _num(previous))
                latest[metric + '_yoy'] = None if ratio is None else ratio - 1
            result['limitations'].extend(document.get('limits', []))
        else:
            period = document['financial_period'].split('/')[-1]
            _day(period)
            published = _public_date(primary, period)
            latest = _canonical(document['current_inputs_Q3_CNY'], primary, 'calculations.json',
                                'current_inputs_Q3_CNY', report_date=period, publish_date=published)
            previous = result['latest']
            if previous and previous['report_date'] == period:
                for metric in ('inventory', 'receivables', 'cash', 'assets', 'liabilities', 'parent_equity', 'share_capital'):
                    if not any(name in document['current_inputs_Q3_CNY'] for name in ALIASES[metric]) and previous[metric] is not None:
                        latest[metric] = previous[metric]
                        latest.setdefault('field_sources', {})[metric] = previous.get('field_sources', {}).get(metric, previous['source'])
            quarter = _canonical(document['q3_difference_CNY'], primary, 'calculations.json',
                                 'q3_difference_CNY', report_date=period, publish_date=published)
            for metric in ('inventory', 'receivables', 'cash', 'assets', 'liabilities', 'parent_equity', 'share_capital'):
                quarter[metric] = latest[metric]
                quarter.setdefault('field_sources', {})[metric] = latest.get('field_sources', {}).get(metric, latest['source'])
            for metric, aliases in ALIASES.items():
                if metric in FLOW:
                    old = next((document['prior_9M_inputs_Q3_CNY'].get(key) for key in aliases
                                if key in document.get('prior_9M_inputs_Q3_CNY', {})), None)
                    ratio = _ratio(latest[metric], _num(old))
                    latest[metric + '_yoy'] = None if ratio is None else ratio - 1
            inherited = document.get('inheritance', {})
            if history_record and inherited.get('result_id') == history_record['result_id']:
                actual = result['components']['history']['sha256']
                if inherited.get('bottom_table_sha256') != actual:
                    raise ValueError('Inherited financial history fingerprint differs')
            value = document['valuation']
            valuation = {'raw_close': _num(value.get('raw_close')), 'market_date': (document.get('market_cutoff') or '')[:10] or None,
                         'parent_profit_ttm': _num(value.get('ttm_parent_profit')), 'pe_ttm_parent': _num(value.get('pe_issued')),
                         'share_basis': 'A股价等值已发行A+H股本；不是A/H实际合并市值。',
                         'scenarios': [], 'source': source}
            if document.get('qualification'):
                result['limitations'].append(document['qualification'])
        result['latest'], result['latest_quarter'] = latest, quarter
        result['components']['latest'] = source
        result['components']['valuation'] = source
        result['valuation'] = valuation
        if valuation.get('market_date') is not None:
            _day(valuation['market_date'])
            if valuation['market_date'] > _research_day(primary):
                raise ValueError('Valuation market date exceeds frozen research cutoff')
        _merge(result['quarterly'], quarter, result['conflicts'])
        _merge(result['cumulative'], latest, result['conflicts'])
    elif result['latest']:
        result['components']['latest'] = result['components']['history']
        result['limitations'].append('最新正式成果无可识别计算组件；仅保留明示继承的历史财务，不据此更新当前估值。')
    if history_record and not result['valuation'].get('scenarios'):
        if any(row['relative_path'] == 'financial-metrics-and-scenarios.json' for row in history_record.get('attachments', [])):
            metrics = _read(store, history_record, 'financial-metrics-and-scenarios.json')
            scenarios = []
            for row in metrics.get('illustrative_scenarios', []):
                scenarios.append({'name': row.get('label'), 'horizon': row.get('horizon'),
                                  'revenue_cny': _num(row.get('revenue_cny_100m')) * 1e8 if row.get('revenue_cny_100m') is not None else None,
                                  'parent_net_margin': _num(row.get('parent_net_margin')),
                                  'parent_profit_cny': _num(row.get('parent_profit_cny_100m')) * 1e8 if row.get('parent_profit_cny_100m') is not None else None,
                                  'pe_low': _num(row.get('exit_pe_low')), 'pe_high': _num(row.get('exit_pe_high')),
                                  'price_low_cny': _num(row.get('terminal_price_low')), 'price_high_cny': _num(row.get('terminal_price_high')),
                                  'status': '继承的历史假设；最新行情和补充研报没有更新该情景。'})
            result['valuation']['scenarios'] = scenarios
            result['valuation']['scenarios_source'] = _source(history_record, 'financial-metrics-and-scenarios.json')
    for key in ('annual', 'quarterly'):
        _growth(result[key], key)
    if result['latest_quarter']:
        result['latest_quarter'] = next(row for row in result['quarterly'] if row['report_date'] == result['latest_quarter']['report_date'])
    result['observations'] = [item for item in context['pending_items'] if item.get('module') == 'fundamental']
    result['overview']['next_checks'] = result['observations'][:5]
    for item in primary.get('evidence', []):
        public = item.get('public_date') or (item.get('public_at') or '')[:10] or None
        result['events'].append({key: item.get(key) for key in ('evidence_id', 'source', 'url', 'financial_period')} |
                                {'public_date': public, 'publication_precision': item.get('public_at_precision', 'unknown'),
                                 'source_result_id': primary['result_id']})
    latest = result['latest']
    if latest:
        def display(value, suffix='亿元'):
            return '缺数' if value is None else f'{value / 1e8:.2f}{suffix}'
        result['overview']['highlights'] = [
            {'label': '经营兑现', 'text': f"{latest['report_date']}累计营收{display(latest['revenue'])}，归母净利润{display(latest['parent_profit'])}。", 'source': latest['source']},
            {'label': '现金质量', 'text': f"同期经营现金流{display(latest['cfo'])}，简化自由现金流{display(latest['fcf'])}；现金转化使用合并净利润分母。", 'source': latest['source']},
            {'label': '下一证据', 'text': '沿原冻结定义核对下一次披露、交付和回款；打开看板不更新研究资格。'}]
    covered = (result['components']['latest'] or {}).get('result_id') == primary['result_id']
    result['status'] = 'ok' if covered and latest and result['annual'] and result['quarterly'] else 'partial'
    last_full_year = int(_research_day(primary)[:4]) - 1
    result['annual'] = [row for row in result['annual'] if last_full_year - 9 <= int(row['report_date'][:4]) <= last_full_year]
    result['quarterly'] = [row for row in result['quarterly'] if int(row['report_date'][:4]) >= last_full_year - 4]
    result['limitations'].extend(['财务横轴是经营所属期，公告日另列；历史PE与当时可知利润沿冻结原记录，不按现值回填。',
                                 '金额为元、比率为小数；现金转化=CFO/同期合并净利润，简化FCF=CFO−购建长期资产现金。',
                                 '历史原价或参考价格均未借重绘升级为复权认证；估值情景仅展示原假设，非新的目标价或一致预期。',
                                 '补充研报不替代正式研究；原成果与未结项保留，未自动形成新回顾或预测命中。'])
    return result
