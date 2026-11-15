"""Bounded, read-only vendor daily prices and a separately qualified session calendar."""

import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import math
from pathlib import Path
import re
import stat


TZ = timezone(timedelta(hours=8))
FIELDS = {'open': '开盘价', 'high': '最高价', 'low': '最低价', 'close': '收盘价',
          'prev_close': '前收盘价', 'volume': '成交量', 'amount': '成交额'}
SCHEDULE_SOURCE = 'https://www.szse.cn/disclosure/notice/t20251222_618087.html'
SESSION_SOURCE = 'https://docs.static.szse.cn/www/lawrules/rule/trade/W020260424690713155663.pdf'
# shortcut: only the published 2026 schedule is cross-checked; source another year before certifying its periods.
HOLIDAYS_2026 = (('01-01', '01-03'), ('02-15', '02-23'), ('04-04', '04-06'), ('05-01', '05-05'),
                 ('06-19', '06-21'), ('09-25', '09-27'), ('10-01', '10-07'))


def _moment(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('A source observation and cutoff need explicit timezone-aware timestamps')
    return stamp.astimezone(TZ)


def _times(observed_at, asof):
    observed, cutoff = _moment(observed_at), _moment(asof)
    if observed > cutoff:
        raise ValueError('The source was observed after the requested cutoff; do not backdate acquisition')
    return observed, cutoff


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Source dates must use complete YYYY-MM-DD values')
    return date.fromisoformat(value)


def _rows(source, expected_header):
    path = Path(source)
    if not path.is_absolute() or '..' in path.parts or '.git' in path.parts:
        raise ValueError('Use an explicit regular local data source')
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Local data sources must not traverse links')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= 64 * 1024 * 1024:
        raise ValueError('The local source must be a bounded, ordinary unlinked file')
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(payload) != before.st_size:
        raise ValueError('The local source changed during acquisition; retry with a stable snapshot')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise ValueError('Local CSV encoding is not supported')
    try:
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        for _ in range(4):
            header = next(reader, [])
            if header and header[0] == expected_header:
                break
        else:
            raise ValueError('The required local CSV header is missing')
        if not all(header) or len(header) != len(set(header)):
            raise ValueError('Local CSV fields must be nonempty and unique')
        rows = []
        for row in reader:
            if not row or not any(row):
                continue
            if len(row) != len(header):
                raise ValueError('A local CSV row has an inconsistent field count')
            rows.append(dict(zip(header, row)))
            if len(rows) > 30000:
                raise ValueError('The local daily source exceeds its row limit')
    except csv.Error as error:
        raise ValueError('The local CSV quoting or field size is invalid') from error
    if not rows:
        raise ValueError('The local source has no records')
    return rows, {'source_file': str(path), 'source_sha256': hashlib.sha256(payload).hexdigest(),
                  'source_bytes': len(payload), 'encoding': encoding, 'rows_total': len(rows)}


def _number(value, *, positive):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError('Daily OHLCV fields must be finite numeric values') from error
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise ValueError('Daily prices must be positive and volume/amount nonnegative')
    return result


def load_market(source_file, security, *, observed_at, asof):
    """Keep original quote units, observe once, and admit only completed daily sessions."""
    observed, cutoff = _times(observed_at, asof)
    if (not isinstance(security, dict) or security.get('market') != 'A-share'
            or security.get('exchange') not in {'sh', 'sz', 'bj'} or security.get('share_class') != 'A'
            or security.get('currency') != 'CNY' or not isinstance(security.get('code'), str)
            or re.fullmatch(r'[0-9]{6}', security['code']) is None
            or security.get('security_id') != 'A-share-' + security['exchange'] + '-' + security['code']
            or not isinstance(security.get('company'), str) or not security['company'].strip()):
        raise ValueError('Use the complete canonical A-share security identity')
    code = security['exchange'] + security['code']
    if Path(source_file).name != code + '.csv':
        raise ValueError('The local daily filename must match the selected exchange and code')
    rows, provenance = _rows(source_file, '股票代码')
    required = {'股票代码', '股票名称', '交易日期', *FIELDS.values()}
    if not required <= set(rows[0]):
        raise ValueError('Daily OHLCV and prior-reference fields are required')
    unique, duplicates = {}, 0
    for index, row in enumerate(rows):
        if row['股票代码'] != code or not row['股票名称'].strip():
            raise ValueError('A daily record belongs to another or unknown security')
        day = _day(row['交易日期'])
        numbers = {field: _number(row[label], positive=field not in {'volume', 'amount'}) for field, label in FIELDS.items()}
        if not numbers['low'] <= min(numbers['open'], numbers['close']) <= max(numbers['open'], numbers['close']) <= numbers['high']:
            raise ValueError('Daily OHLC bounds are inconsistent')
        if day in unique:
            if unique[day][0] != numbers:
                raise ValueError('Conflicting duplicate daily records require source review')
            duplicates += 1
        else:
            unique[day] = numbers, index, Decimal(row['前收盘价'])
    bars, excluded, previous_close = [], [], None
    for day, (numbers, index, reference) in sorted(unique.items()):
        end = datetime.combine(day, time(15, 30), TZ)
        if end > cutoff or end > observed:
            excluded.append({'date': day.isoformat(), 'reason': 'session_not_complete_or_after_cutoff'})
            continue
        bar = numbers | {'time': end.isoformat(), 'available_at': observed.isoformat(), 'completed': True, 'source_index': index}
        if previous_close is not None and abs(reference - previous_close) > Decimal('0.00000001'):
            bar['mechanical_gap'] = True
        bars.append(bar)
        previous_close = Decimal(str(numbers['close']))
    if not bars:
        raise ValueError('No completed daily session was available in the acquired source')
    provenance.update(observed_at=observed.isoformat(), asof=cutoff.isoformat(), rows_selected=len(bars),
        identical_duplicates_removed=duplicates, excluded=excluded, field_mapping=FIELDS.copy(),
        units={'price': '人民币元/股，按已核证券报价币种解释供应商原价字段，未独立认证',
               'volume': 'unknown', 'amount': 'unknown'},
        session_completion_upper_bound='北京时间15:30，全天量额的保守完成上界',
        session_source=SESSION_SOURCE, adjustment='raw', adjustment_verified=False,
        limitations=['供应商日期行不能证明盘中可得；本次实际取得时间单独保留，不倒填为当日已取得。',
            '15:30是有规则依据的保守完成界限，不表示每个历史市场或交易日采用相同会话。',
            '前收参考价与前行收盘不连续仅标潜在机械或修订边界，不认定原因或授予复权资格。',
            '缺交易日不补造K线，没有证据不标停牌；成交量和成交额绝对单位仍未核实。'])
    return {'bars': bars, 'provenance': provenance, 'source_sha': provenance['source_sha256'],
            'trading_dates': [row['time'][:10] for row in bars], 'lastdate': bars[-1]['time'][:10],
            'market_cutoff': bars[-1]['time']}


def load_index(source_file, *, observed_at, asof):
    """A single declared nominal-price benchmark; no index or market discovery."""
    observed, cutoff = _times(observed_at, asof)
    path = Path(source_file)
    if path.name != 'sh000300.csv' or path.parent.name != 'stock-main-index-data':
        raise ValueError('Only the declared CSI300 local index source is supported')
    rows, provenance = _rows(path, 'candle_end_time')
    if not {'candle_end_time', 'close', 'index_code'} <= set(rows[0]):
        raise ValueError('The declared index date, close and code fields are required')
    unique, duplicates = {}, 0
    for row in rows:
        if row['index_code'] != 'sh000300':
            raise ValueError('An index record belongs to another benchmark')
        day, close = _day(row['candle_end_time']), _number(row['close'], positive=True)
        if day in unique:
            if unique[day] != close:
                raise ValueError('Conflicting duplicate index closes require source review')
            duplicates += 1
        unique[day] = close
    bars, excluded = [], []
    for day, close in sorted(unique.items()):
        end = datetime.combine(day, time(15, 30), TZ)
        if end > observed or end > cutoff:
            excluded.append({'date': str(day), 'reason': 'session_not_complete_or_after_cutoff'})
            continue
        bars.append({'time': end.isoformat(), 'close': close, 'available_at': observed.isoformat(), 'completed': True})
    if not bars:
        raise ValueError('No completed index session was available in the acquired source')
    provenance.update(observed_at=observed.isoformat(), asof=cutoff.isoformat(), rows_selected=len(bars),
        identical_duplicates_removed=duplicates, excluded=excluded,
        field_mapping={'date': 'candle_end_time', 'close': 'close', 'code': 'index_code'},
        units={'close': '名义价格指数点位，不是每股币值'}, session_source=SESSION_SOURCE,
        limitations=['供应商名义价格指数只作诊断背景，不是经独立认证的全收益序列。',
                    '取得时间按实际记录；15:30是保守完成界限，不是逐日实际发布时间。'])
    return {'bars': bars, 'provenance': provenance, 'source_sha': provenance['source_sha256'],
            'lastdate': bars[-1]['time'][:10], 'market_cutoff': bars[-1]['time']}


def load_calendar(source_file, *, observed_at, asof, session_close='15:30'):
    """A vendor schedule can establish period boundaries without certifying all history."""
    observed, cutoff = _times(observed_at, asof)
    if Path(source_file).name != 'period_offset.csv' or session_close != '15:30':
        raise ValueError('Use the declared vendor calendar and conservative 15:30 session boundary')
    rows, provenance = _rows(source_file, '交易日期')
    dates = [_day(row['交易日期']) for row in rows]
    if len(dates) != len(set(dates)) or any(day.weekday() >= 5 for day in dates):
        raise ValueError('A-share scheduled dates must be unique weekdays')
    dates.sort()
    closed = set()
    for start, end in HOLIDAYS_2026:
        first, last = date.fromisoformat('2026-' + start), date.fromisoformat('2026-' + end)
        closed.update(first + timedelta(days=index) for index in range((last-first).days+1))
    expected = {date(2026, 1, 1) + timedelta(days=index) for index in range(365)}
    expected = {day for day in expected if day.weekday() < 5 and day not in closed}
    supplied = {day for day in dates if day.year == 2026}
    matched = supplied == expected and observed >= datetime(2025, 12, 23, tzinfo=TZ)
    crosscheck = {'year': 2026, 'expected_sessions': len(expected), 'supplied_sessions': len(supplied),
        'missing_dates': sorted(str(day) for day in expected-supplied),
        'unexpected_dates': sorted(str(day) for day in supplied-expected), 'matched': matched,
        'source': SCHEDULE_SOURCE, 'notice_public_date': '2025-12-22'}
    limitations = ['供应商全历史日历仍未认证；仅对本地2026常规安排与已公布日期逐日核对。',
        '突发休市和历史逐日会话变化未作完整认证。',
        '15:30是有规则来源的保守完成上界，不是逐场实际成交时刻。',
        '日历按实际取得时间记录；未来计划日期不代表已取得未来行情。']
    result = {'trading_dates': [str(day) for day in dates],
        'session_closes': {str(day): datetime.combine(day, time(15, 30), TZ).isoformat() for day in dates},
        'coverage_start': str(dates[0]), 'coverage_end': str(dates[-1]),
        'source': 'vendor period_offset.csv SHA256=' + provenance['source_sha256'],
        'source_sha': provenance['source_sha256'], 'available_at': observed.isoformat(), 'verified': False,
        'session_source': SESSION_SOURCE, 'session_verified': True, '2026_crosscheck': crosscheck,
        'limitations': limitations, 'provenance': provenance | {'observed_at': observed.isoformat(), 'asof': cutoff.isoformat()}}
    if matched:
        result.update(verification_start='2026-01-01', verification_end='2026-12-31',
                      verification_source=SCHEDULE_SOURCE + ' ; ' + SESSION_SOURCE)
    return result
