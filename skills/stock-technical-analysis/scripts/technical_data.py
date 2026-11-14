"""Frozen local inputs and conservative A-share timeframe aggregation."""

from collections import defaultdict
from datetime import datetime, date, time, timedelta, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy = sibling('harmonic_analysis')
moment = legacy.moment
TZ = timezone(timedelta(hours=8))
FRAMES = ('1d', '1w', '1mo', '6mo', '1y')
MINUTE_FRAMES = ('5m', '15m', '60m')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def period_key(stamp, frame):
    day = moment(stamp).astimezone(TZ).date()
    if frame == '1w':
        return day.isocalendar()[:2]
    if frame == '1mo':
        return day.year, day.month
    if frame == '6mo':
        return day.year, 1 if day.month <= 6 else 2
    if frame == '1y':
        return (day.year,)
    raise ValueError('不支持的日线聚合周期')


def period_end(key, frame):
    if frame == '1w':
        return date.fromisocalendar(*key, 7)
    if frame == '1y':
        return date(key[0], 12, 31)
    if frame == '6mo':
        return date(key[0], 6, 30) if key[1] == 1 else date(key[0], 12, 31)
    year, month = key
    return date(year+1, 1, 1)-timedelta(days=1) if month == 12 else date(year, month+1, 1)-timedelta(days=1)


def period_start(key, frame):
    if frame == '1w':
        return date.fromisocalendar(*key, 1)
    if frame == '1y':
        return date(key[0], 1, 1)
    if frame == '6mo':
        return date(key[0], 1 if key[1] == 1 else 7, 1)
    return date(*key, 1)


def previous_long_period(key, frame):
    if frame == '1y':
        return (key[0]-1,)
    if frame == '6mo':
        return (key[0], 1) if key[1] == 2 else (key[0]-1, 2)
    raise ValueError('连续期间检查仅用于半年/年')


def combine(rows):
    result = {'time': rows[-1]['time'], 'open': rows[0]['open'],
              'high': max(x['high'] for x in rows), 'low': min(x['low'] for x in rows),
              'close': rows[-1]['close'], 'completed': True,
              'available_at': max((x.get('available_at', x['time']) for x in rows), key=moment)}
    for field in ('volume', 'amount'):
        if all(field in x for x in rows):
            result[field] = sum(x[field] for x in rows)
    if 'prev_close' in rows[0]:
        result['prev_close'] = rows[0]['prev_close']
    if any(row.get('mechanical_gap') is True for row in rows):
        result['mechanical_gap']=True
    sessions={row.get('session_id') for row in rows}
    if len(sessions)==1 and None not in sessions:
        result['session_id']=next(iter(sessions))
    return result


def aggregate_daily(bars, frame, cutoff, calendar=None):
    if frame not in ('1w', '1mo', '6mo', '1y'):
        raise ValueError('只允许日线聚合周/月/半年/年')
    groups = defaultdict(list)
    for row in bars:
        groups[period_key(row['time'], frame)].append(row)
    result, incomplete, uncovered = [], 0, 0
    unfinished_periods, incomplete_date_periods = [], []
    calendar = calendar or {}
    supplied = calendar.get('trading_dates', [])
    if not isinstance(supplied, list) or len(set(supplied)) != len(supplied):
        raise ValueError('calendar.trading_dates须无重复日期数组')
    for key, rows in sorted(groups.items()):
        ending = period_end(key, frame)
        if datetime.combine(ending, time.max, TZ) > moment(cutoff):
            incomplete += 1
            unfinished_periods.append(ending.isoformat())
            continue
        actual = {moment(x['time']).astimezone(TZ).date().isoformat() for x in rows}
        if len(actual) != len(rows):
            raise ValueError('日线聚合每个交易日只能有一根行情')
        expected = {x for x in supplied if period_key(x+'T00:00:00+08:00', frame) == key}
        start_of_period = period_start(key, frame)
        covered = bool(expected and calendar.get('coverage_start') and calendar.get('coverage_end') and
                       date.fromisoformat(calendar['coverage_start'])<=start_of_period and
                       date.fromisoformat(calendar['coverage_end'])>=ending)
        if (supplied or frame in ('6mo', '1y')) and not covered:
            uncovered += 1
            continue
        if expected and expected != actual:
            incomplete += 1
            incomplete_date_periods.append(ending.isoformat())
            continue
        value = combine(rows)
        value['period_end'] = ending.isoformat()
        result.append(value)
    continuity = {}
    if frame in ('6mo', '1y'):
        all_complete = result
        first = len(result)-1
        while first > 0:
            current_key = period_key(result[first]['period_end']+'T00:00:00+08:00', frame)
            previous_key = period_key(result[first-1]['period_end']+'T00:00:00+08:00', frame)
            if previous_key != previous_long_period(current_key, frame):
                break
            first -= 1
        excluded_history = all_complete[:max(first, 0)]
        result = all_complete[max(first, 0):]
        latest_key = period_key(cutoff, frame)
        if datetime.combine(period_end(latest_key, frame), time.max, TZ) > moment(cutoff):
            latest_key = previous_long_period(latest_key, frame)
        missing_tail = []
        if result:
            selected_key = period_key(result[-1]['period_end']+'T00:00:00+08:00', frame)
            cursor = latest_key
            while cursor > selected_key:
                missing_tail.append(period_end(cursor, frame).isoformat())
                cursor = previous_long_period(cursor, frame)
        continuity = dict(continuity_policy='latest_contiguous_completed_periods',
            excluded_discontinuous_groups=len(excluded_history),
            excluded_discontinuous_periods=[x['period_end'] for x in excluded_history],
            unfinished_periods=unfinished_periods,
            incomplete_trading_date_periods=incomplete_date_periods,
            latest_expected_completed_period_end=period_end(latest_key, frame).isoformat(),
            selected_latest_period_end=result[-1]['period_end'] if result else None,
            missing_trailing_periods=list(reversed(missing_tail)),
            latest_completed_period_present=bool(result and not missing_tail),
            data_asof=result[-1]['time'] if result else None)
    verified = bool(result and supplied and calendar.get('verified') is True and calendar.get('source'))
    limitations = [] if verified else ['交易日历未独立核验，聚合仅诊断；半年/年须有覆盖完整期间的交易日期']
    if continuity.get('excluded_discontinuous_groups'):
        limitations.append('历史完整期间存在断档；仅最近连续完整段参与计算，较早完整断段保留于元数据，不跨缺期计算指标')
    if continuity.get('missing_trailing_periods'):
        limitations.append('最新应有完整期间缺失；当前连续段仅截至'+continuity['selected_latest_period_end']+'，须按data_asof理解，不能作为最新长周期状态')
    return result, {'aggregation': 'completed_calendar_periods', 'excluded_groups': incomplete,
                    'uncovered_calendar_groups': uncovered,
                    'calendar_verified': verified,
                    'qualification': 'validated' if verified and not continuity.get('missing_trailing_periods') else 'diagnostic',
                    'limitations': limitations, **continuity}


def aggregate_minutes(bars, source_minutes, target_minutes, sessions):
    if source_minutes not in (5, 15) or target_minutes not in (15, 60) or target_minutes % source_minutes:
        raise ValueError('分钟聚合仅支持5→15/60或15→60')
    if not isinstance(sessions, list) or not sessions:
        raise ValueError('必须明确交易会话及分钟结束标签')
    groups, invalid = defaultdict(list), 0
    for row in bars:
        at = moment(row['time']).astimezone(TZ)
        slot = None
        for index, session in enumerate(sessions):
            start = datetime.combine(at.date(), time.fromisoformat(session[0]), TZ)
            end = datetime.combine(at.date(), time.fromisoformat(session[1]), TZ)
            minutes = (at-start).total_seconds()/60
            if start < at <= end and minutes % source_minutes == 0:
                group = int((minutes-1)//target_minutes)
                group_end = min(start+timedelta(minutes=(group+1)*target_minutes), end)
                group_start = start+timedelta(minutes=group*target_minutes)
                slot = (at.date().isoformat(), index, group, group_start, group_end)
                break
        if slot is None:
            invalid += 1
        else:
            groups[slot].append(row)
    if invalid:
        raise ValueError('分钟记录不符合已声明会话/结束标签；不得跨午休或含未知盘后数据')
    result, missing = [], 0
    for key, rows in sorted(groups.items()):
        start, end = key[-2:]
        expected = [start+timedelta(minutes=x) for x in range(source_minutes, int((end-start).total_seconds()/60)+1, source_minutes)]
        if [moment(x['time']) for x in rows] != expected:
            missing += 1
            continue
        result.append(combine(rows))
    return result, {'aggregation': f'{source_minutes}m_to_{target_minutes}m_session_end',
                    'excluded_groups': missing, 'qualification': 'validated' if not missing else 'diagnostic',
                    'limitations': [] if not missing else ['不完整分钟分组已排除']}


def load_dataset(request, request_path, dataset, market_cutoff):
    if not isinstance(dataset, dict):
        raise ValueError('dataset须为对象')
    merged = dict(request)
    merged.update(dataset)
    merged['asof'] = request['asof']
    source = Path(merged['input_file'])
    if not source.is_absolute():
        source = request_path.parent/source
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    rows, meta = legacy.prepare(merged, request_path)
    meta['input_prepared_at']=meta.pop('generated_at')
    if hashlib.sha256(source.read_bytes()).hexdigest() != before:
        raise ValueError('读取期间输入文件发生变化，请重新冻结快照')
    selected = [r for r in rows if moment(r['time']) <= moment(market_cutoff)]
    declarations = merged.get('validation', {})
    if not isinstance(declarations, dict):
        raise ValueError('validation须为对象')
    required = ('price_basis', 'session', 'completion', 'source_identity')
    qualified = all(declarations.get(k) is True for k in required) and bool(declarations.get('evidence'))
    if not selected:
        raise ValueError('市场截止前没有合格记录')
    retrospective = moment(merged['snapshot_available_at']) > moment(market_cutoff)
    meta.update(market_cutoff=market_cutoff, data_asof=selected[-1]['time'], used_bars=len(selected),
                source_sha256=before, source_family=merged.get('source_family',before), validation_declarations=declarations,
                qualification='validated' if qualified else 'diagnostic',
                point_in_time='retrospective_snapshot' if retrospective else 'declared_available_by_cutoff',
                limitations=[] if qualified else ['价格/会话/完成/来源资格尚未全部核验'])
    if retrospective:
        meta['limitations'].append('快照在市场截止后取得，仅按冻结历史回溯，不声称当时已可获得同版数据')
    target_day = moment(market_cutoff).astimezone(TZ).date()
    stale_days = (target_day-moment(selected[-1]['time']).astimezone(TZ).date()).days
    meta['stale_calendar_days'] = stale_days
    if stale_days > int(merged.get('max_stale_calendar_days', 4)):
        meta['qualification'] = 'diagnostic'
        meta['limitations'].append('行情超过声明的新鲜度窗口，不作当前执行依据')
    return selected, meta


def load_close_dataset(request, request_path, dataset, market_cutoff):
    merged=dict(request);merged.update(dataset)
    source=Path(merged['input_file'])
    if not source.is_absolute(): source=request_path.parent/source
    if source.suffix.lower()!='.json':
        raise ValueError('采样价先显式转换为time/close JSON，不猜宽表列语义')
    payload=source.read_bytes();raw=json.loads(payload.decode(merged.get('encoding','utf-8-sig')))
    if not isinstance(raw,list): raise ValueError('close-only须为记录数组')
    available=moment(merged['snapshot_available_at'])
    if available>moment(request['asof']): raise ValueError('快照晚于本次可知截止')
    if not merged.get('time_semantics') or not merged.get('source'):
        raise ValueError('采样价须声明名义观察标签语义与来源')
    result=[];previous=None
    for row in raw:
        if not isinstance(row,dict) or isinstance(row.get('close'),bool): raise ValueError('无效采样记录')
        at=moment(row['time']);price=float(row['close'])
        if not math.isfinite(price) or price<=0: raise ValueError('采样价须为正有限数')
        if previous is not None and at<=previous: raise ValueError('采样时点须严格递增')
        previous=at
        known=moment(row.get('available_at',merged['snapshot_available_at']))
        if at>moment(market_cutoff) or known>moment(request['asof']) or row.get('completed',True) is not True: continue
        result.append(dict(time=at.astimezone(TZ).isoformat(),close=price,available_at=known.isoformat(),completed=True))
    if not result: raise ValueError('截止前无可用采样价')
    return result,dict(price_only=True,timeframe=merged['timeframe'],source=merged['source'],
        source_sha256=hashlib.sha256(payload).hexdigest(),used_bars=len(result),data_asof=result[-1]['time'],
        time_semantics=merged['time_semantics'],snapshot_available_at=merged['snapshot_available_at'],
        adjustment=merged.get('adjustment',{}),qualification='diagnostic',
        point_in_time='retrospective_snapshot' if available>moment(market_cutoff) else 'declared_available_by_cutoff',
        limitations=['仅名义时点采样价，不是已核验结束时刻OHLC；仅运行收盘价所需的价格指标',
                     '缺OHLC／量能，蜡烛、一目、谐波、SMC、海龟与ATR均不运行'])


def prepare_inputs(request, request_path):
    if not isinstance(request, dict):
        raise ValueError('请求须为对象')
    request_path = Path(request_path)
    cutoff = request.get('market_cutoff', request['asof'])
    if moment(cutoff) > moment(request['asof']):
        raise ValueError('市场截止不能晚于本次可知截止')
    datasets = request.get('datasets', [dict(timeframe=request.get('timeframe', '1d'))])
    if not isinstance(datasets, list) or not datasets:
        raise ValueError('datasets须为非空数组')
    results = {}
    for data in datasets:
        if not isinstance(data,dict):
            raise ValueError('dataset须为对象')
        frame = data.get('timeframe')
        if frame not in FRAMES + MINUTE_FRAMES or frame in results:
            raise ValueError('周期不支持或重复')
        if frame in MINUTE_FRAMES:
            results[frame]={'status':'paused','bars':[], 'metadata':{'timeframe':frame,'qualification':'paused',
                'limitations':['分钟级分析已暂停；不读取、计算或聚合分钟数据，当前聚焦日/周/月/半年/年']}}
            continue
        try:
            loader=load_close_dataset if data.get('kind')=='close_only' else load_dataset
            bars, meta = loader(request, request_path, data, cutoff)
            results[frame] = {'status': 'ok', 'bars': bars, 'metadata': meta}
        except (ValueError, OSError, KeyError, TypeError) as error:
            results[frame] = {'status': 'unavailable', 'bars': [], 'metadata': {'limitations': [str(error)]}}
    wanted = request.get('timeframes', list(results) if all(x in MINUTE_FRAMES for x in results) else list(FRAMES))
    if not isinstance(wanted, list) or len(set(wanted)) != len(wanted) or any(x not in FRAMES + MINUTE_FRAMES for x in wanted):
        raise ValueError('timeframes周期数组不合法')
    if '1d' in results and results['1d']['status'] == 'ok':
        daily = results['1d']
        aggregation_error=None
        if request.get('aggregation_input'):
            try:
                aggregate_rows,aggregate_meta=load_dataset(request,request_path,request['aggregation_input'],cutoff)
                daily={'status':'ok','bars':aggregate_rows,'metadata':aggregate_meta}
            except (ValueError,OSError,KeyError,TypeError) as error:
                aggregation_error=str(error)
        for frame in ('1w', '1mo', '6mo', '1y'):
            if frame in wanted and frame not in results:
                if aggregation_error:
                    results[frame]={'status':'unavailable','bars':[], 'metadata':{'limitations':['聚合来源错误:'+aggregation_error]}}
                    continue
                bars, aggregation = aggregate_daily(daily['bars'], frame, cutoff, request.get('calendar'))
                meta = dict(daily['metadata'])
                meta.update(aggregation)
                meta['limitations']=list(daily['metadata'].get('limitations',[]))+aggregation['limitations']
                if daily['metadata']['qualification'] != 'validated':
                    meta['qualification'] = 'diagnostic'
                meta['data_asof'] = bars[-1]['time'] if bars else None
                meta['timeframe']=frame
                results[frame] = {'status': 'ok' if bars else 'unavailable', 'bars': bars, 'metadata': meta}
    for frame in wanted:
        if frame in MINUTE_FRAMES:
            results.setdefault(frame,{'status':'paused','bars':[], 'metadata':{'timeframe':frame,'qualification':'paused',
                'limitations':['分钟级分析已暂停；当前聚焦日/周/月/半年/年']}})
            continue
        results.setdefault(frame, {'status': 'unavailable', 'bars': [], 'metadata': {'limitations': ['未提供合格OHLC来源']}})
    return {frame: results[frame] for frame in wanted}
