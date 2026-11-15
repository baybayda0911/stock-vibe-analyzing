"""Read a declared XBX general-enterprise snapshot; never fetch, fill or write data."""

import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import math
from pathlib import Path
import re
import stat


SHANGHAI = timezone(timedelta(hours=8))
CONTRACT = 'xbx-general-enterprise/1'
FIELDS = {
    'revenue': 'R_revenue@xbx', 'cost': 'R_operating_cost@xbx',
    'parent_profit': 'R_np_atoopc@xbx', 'consolidated_profit': 'R_np@xbx',
    'cfo': 'C_ncf_from_oa@xbx', 'capex': 'C_cash_paid_for_assets@xbx',
    'cash': 'B_currency_fund@xbx', 'receivables': 'B_account_receivable@xbx',
    'inventory': 'B_inventory@xbx', 'assets': 'B_total_assets@xbx',
    'liabilities': 'B_total_liab@xbx', 'parent_equity': 'B_total_equity_atoopc@xbx',
    'registered_capital': 'B_actual_received_capital@xbx',
}
FLOW = ('revenue', 'cost', 'parent_profit', 'consolidated_profit', 'cfo', 'capex')
LIMITS = [
    '金额使用显式XBX一般企业人民币元合同；CSV没有独立单位列，尚未逐字段独立核验，不能由量级猜单位。',
    'publish_date是本地该版本公开日期，不保证首次披露；first_local_publish_date仅为本地所见最早日期。',
    '抓取时间无已核时区，只保留原文，不作为历史首次可得证明；本次实际读取时间另列。',
    '累计差分使用截止时可见的所选版本；合并范围变化、重述及历史原始披露仍需原件核验。',
    '业绩预告、预测及券商数据不混入已披露实绩；缺值不填0、不前向填充。',
]


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Financial cutoff and observation require an explicit timezone')
    return parsed.astimezone(SHANGHAI)


def _date(value, compact=False):
    if not isinstance(value, str):
        raise ValueError('Financial date must be explicit text')
    if compact:
        if not re.fullmatch(r'\d{8}', value):
            raise ValueError('XBX report_date requires YYYYMMDD')
        value = value[:4] + '-' + value[4:6] + '-' + value[6:]
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('Financial public date requires YYYY-MM-DD')
    return parsed


def _money(value):
    if value is None or value.strip() == '':
        return None
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value):
        raise ValueError('Financial amount is not numeric')
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError('Financial amount is not numeric') from error
    if not parsed.is_finite() or abs(parsed) > Decimal('1e30') or parsed != 0 and float(parsed) == 0:
        raise ValueError('Financial amount must be finite and bounded')
    return parsed


def _read(path, expected_name):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.name != expected_name or path.parent.name != expected_name.split('_')[0]:
        raise ValueError('Use the exact configured security financial file')
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Financial source cannot pass through a link')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= 64 * 1024 * 1024:
        raise ValueError('Financial source must be a bounded regular file')
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('Financial source changed while reading')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return payload, payload.decode(encoding), encoding
        except UnicodeError:
            continue
    raise ValueError('Financial source encoding is not supported')


def _identity(security):
    keys = {'security_id', 'company', 'market', 'exchange', 'code', 'share_class', 'currency'}
    if (not isinstance(security, dict) or set(security) != keys or security.get('market') != 'A-share'
            or security.get('exchange') not in {'sh', 'sz', 'bj'} or security.get('currency') != 'CNY'
            or security.get('share_class') != 'A' or not isinstance(security.get('code'), str)
            or not re.fullmatch(r'\d{6}', security['code']) or not isinstance(security.get('company'), str)
            or not security['company'].strip() or len(security['company']) > 200
            or security.get('security_id') != 'A-share-' + security['exchange'] + '-' + security['code']):
        raise ValueError('Use a complete confirmed CNY A-share identity')
    return security['exchange'] + security['code']


def _ratio(left, right):
    value = float(left / right) if left is not None and right is not None and right > 0 else None
    if value is not None and not math.isfinite(value):
        raise ValueError('Financial ratio exceeds the finite numeric range')
    return value


def _view(row):
    result = {key: float(value) if isinstance(value, Decimal) else value for key, value in row.items() if key != '_amounts'}
    values = row['_amounts']
    result.update({key: float(value) if value is not None else None for key, value in values.items()})
    result['gross_margin'] = _ratio(values['revenue'] - values['cost'], values['revenue']) if all(values[key] is not None for key in ('revenue', 'cost')) else None
    result['parent_net_margin'] = _ratio(values['parent_profit'], values['revenue'])
    result['cash_conversion'] = _ratio(values['cfo'], values['consolidated_profit'])
    result['fcf'] = float(values['cfo'] - values['capex']) if all(values[key] is not None for key in ('cfo', 'capex')) else None
    return result


def _operand(row):
    return {key: row[key] for key in ('report_date', 'publish_date', 'source_row', 'source_sha256')}


def load_financial(source_file, security, *, asof, observed_at, unit='CNY_yuan'):
    """Return version-visible amounts and compatible tables from immutable bytes."""
    code = _identity(security)
    if unit != 'CNY_yuan':
        raise ValueError('Only the declared XBX CNY-yuan provider contract is supported')
    cutoff, observed = _stamp(asof), _stamp(observed_at)
    payload, text, encoding = _read(source_file, code + '_一般企业.csv')
    digest = hashlib.sha256(payload).hexdigest()
    lines = text.splitlines()
    if len(lines) < 3:
        raise ValueError('Financial source is empty')
    reader = csv.DictReader(io.StringIO('\n'.join(lines[1:])))
    columns = reader.fieldnames or []
    required = {'stock_code', 'statement_format', 'report_date', 'publish_date', '抓取时间', *(FIELDS[key] for key in FLOW)}
    if len(columns) != len(set(columns)) or not required <= set(columns):
        raise ValueError('Financial source does not match its declared field contract')
    versions, excluded = {}, 0
    for line_number, raw in enumerate(reader, 3):
        if None in raw or any(value is None for value in raw.values()):
            raise ValueError('Financial CSV row has a different field count')
        if raw['stock_code'] != code or raw['statement_format'] != '一般企业':
            raise ValueError('Financial row belongs to a different security or statement format')
        period, public = _date(raw['report_date'], True), _date(raw['publish_date'])
        if period.strftime('%m-%d') not in {'03-31', '06-30', '09-30', '12-31'} or public < period:
            raise ValueError('Financial period or publication order is invalid')
        amounts = {key: _money(raw.get(field)) for key, field in FIELDS.items()}
        # Date-only publication is conservatively known at that local day's end.
        if period > cutoff.date() or datetime.combine(public, time.max, SHANGHAI) > cutoff:
            excluded += 1
            continue
        key = period.isoformat(), public.isoformat()
        row = {'report_date': key[0], 'publish_date': key[1], 'source_row': line_number,
               'source_sha256': digest, 'capture_time_raw': raw['抓取时间'], '_amounts': amounts}
        if key in versions and versions[key]['_amounts'] != amounts:
            raise ValueError('Conflicting financial amounts for one report/publication version')
        versions.setdefault(key, row)
    if not versions:
        raise ValueError('No financial version is conservatively visible by the cutoff')
    selected, vintage_log = {}, []
    for period in sorted({key[0] for key in versions}):
        rows = [versions[key] for key in sorted(versions) if key[0] == period]
        row = dict(rows[-1], first_local_publish_date=rows[0]['publish_date'], period_basis='cumulative')
        selected[period] = row
        vintage_log.append({'report_date': period, 'first_local_publish_date': rows[0]['publish_date'],
            'selected_publish_date': row['publish_date'], 'versions': [{**_operand(value), 'capture_time_raw': value['capture_time_raw']} for value in rows],
            'changed_fields': [field for field in FIELDS if any(value['_amounts'][field] != rows[0]['_amounts'][field] for value in rows[1:])]})
    quarters = {}
    for period, row in selected.items():
        end = date.fromisoformat(period)
        previous = selected.get({6: f'{end.year}-03-31', 9: f'{end.year}-06-30', 12: f'{end.year}-09-30'}.get(end.month))
        operands = [row] if end.month == 3 else [row, previous] if previous else [row]
        values = dict(row['_amounts'])
        if end.month != 3:
            for field in FLOW:
                values[field] = row['_amounts'][field] - previous['_amounts'][field] if previous and row['_amounts'][field] is not None and previous['_amounts'][field] is not None else None
        quarters[period] = dict(row, _amounts=values, period_basis='single_quarter',
            publish_date=max(value['publish_date'] for value in operands), operand_versions=[_operand(value) for value in operands],
            difference_complete=end.month == 3 or previous is not None)
    annual = [dict(row, period_basis='annual') for period, row in selected.items() if period.endswith('-12-31')]
    latest = selected[max(selected)]
    latest_day = date.fromisoformat(latest['report_date'])
    if latest_day.month == 12:
        ttm_operands = [latest]
    else:
        ttm_operands = [latest, selected.get(f'{latest_day.year-1}-12-31'), selected.get(f'{latest_day.year-1}' + latest['report_date'][4:])]
    ttm = None
    if all(value is not None for value in ttm_operands):
        values = {key: None for key in FIELDS}
        for field in FLOW:
            inputs = [row['_amounts'][field] for row in ttm_operands]
            values[field] = inputs[0] if len(inputs) == 1 else inputs[0] + inputs[1] - inputs[2] if all(value is not None for value in inputs) else None
        ttm = _view(dict(latest, _amounts=values, period_basis='ttm', publish_date=max(row['publish_date'] for row in ttm_operands),
                         operand_versions=[_operand(row) for row in ttm_operands]))
    tables = {'annual': [_view(row) for row in annual], 'quarterly': [_view(row) for row in quarters.values()],
              'cumulative': [_view(row) for row in selected.values()]}
    for name in ('annual', 'quarterly', 'cumulative'):
        indexed = {row['report_date']: row for row in tables[name]}
        for period, row in indexed.items():
            day = date.fromisoformat(period)
            prior = indexed.get(f'{day.year-1}' + period[4:])
            previous_day = date(day.year, day.month-2, 1) - timedelta(days=1)
            previous = indexed.get(previous_day.isoformat()) if name == 'quarterly' else None
            for field in ('revenue', 'parent_profit'):
                for suffix, base in (('yoy', prior), ('qoq', previous)):
                    ratio = _ratio(Decimal(str(row[field])) if row[field] is not None else None,
                                   Decimal(str(base[field])) if base and base[field] is not None else None)
                    row[field + '_' + suffix] = ratio - 1 if ratio is not None else None
    provenance = {'provider_contract': CONTRACT, 'unit_contract': unit, 'unit_qualification': 'declared_not_independently_verified',
        'source_file': Path(source_file).name, 'source_sha256': digest, 'source_bytes': len(payload), 'encoding': encoding,
        'field_mapping': dict(FIELDS), 'observed_at': observed.isoformat(), 'asof': cutoff.isoformat(),
        'historical_reconstruction': observed > cutoff, 'qualification': 'diagnostic',
        'publication_time_precision': 'date_only_conservative_end_of_day', 'capture_time_timezone': 'unknown',
        'excluded_after_cutoff': excluded, 'selected_periods': len(selected), 'visible_versions': len(versions),
        'missing_columns': [field for field in FIELDS.values() if field not in columns], 'versions': vintage_log,
        'limitations': list(LIMITS), 'forecasts_included_as_actual': False}
    document = {'generated_at': observed.isoformat(), 'unit': 'CNY unless field is ratio/date/share/price',
                'security': dict(security), 'tables': tables, 'provenance': provenance}
    return {'document': document, 'latest_ytd': next(row for row in tables['cumulative'] if row['report_date'] == latest['report_date']),
            'latest_quarter': next(row for row in tables['quarterly'] if row['report_date'] == latest['report_date']),
            'ttm': ttm, 'provenance': provenance}
