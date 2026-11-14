"""Local, lag-membership equal-weight SW-labelled proxies; never official SW indices."""

import argparse
from collections import deque
import csv
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import tempfile


TZ=timezone(timedelta(hours=8))
BASIS='vendor-close-over-reference-chain:daily:local-SW-equal-lagged-membership:v1'
SW={1:'新版申万一级行业名称',2:'新版申万二级行业名称',3:'新版申万三级行业名称'}


def _moment(value):
    at=datetime.fromisoformat(value.replace('Z','+00:00'))
    if at.utcoffset() is None:
        raise ValueError('Timestamp needs a timezone')
    return at


def _number(value):
    if value in (None,'','nan','NaN'):
        return None
    if isinstance(value,bool):
        raise ValueError('Boolean is not numeric data')
    result=float(value)
    if not math.isfinite(result):
        raise ValueError('Numeric observations must be finite')
    return result


def _stamp(day):
    return datetime.combine(date.fromisoformat(day),time(15),TZ).isoformat()


def _normalise(rows):
    result=[];previous=None
    for raw in rows:
        day=raw['date'];date.fromisoformat(day)
        if previous is not None and day<=previous:
            raise ValueError('Stock dates must increase strictly')
        previous=day
        row={'date':day}
        for field in ('close','prev_close','volume','amount'):
            row[field]=_number(raw.get(field))
        if any(row[k] is not None and row[k]<0 for k in ('volume','amount')):
            raise ValueError('Volume/amount cannot be negative')
        for level in SW:
            value=raw.get(f'sw{level}')
            row[f'sw{level}']=value.strip() if isinstance(value,str) and value.strip() not in ('','nan','NaN') else None
        result.append(row)
    return result


def _levels(rows):
    result={};recent=deque(maxlen=20);value=None
    for row in rows:
        close,reference=row['close'],row['prev_close']
        if close is None or reference is None or min(close,reference)<=0:
            recent.clear();value=None
            continue
        ratio=_number(close/reference)
        value=100.0 if value is None else _number(value*ratio)
        recent.append(value)
        result[row['date']]={'level':value,'above_ma20':value>sum(recent)/20 if len(recent)==20 else None}
    return result


def compute_context(stocks, *, target_rows, target_code, level, start_date, market_cutoff,
                    asof, snapshot_available_at, trading_dates, date_label_evidence,
                    source_metadata=None, security_id=None):
    """Rows use date/close/prev_close/volume/amount/sw1/sw2/sw3; stocks yields(code,rows)."""
    if type(level) is not int or level not in SW:
        raise ValueError('SW level must be 1, 2 or 3')
    cutoff,known,snapshot=_moment(market_cutoff),_moment(asof),_moment(snapshot_available_at)
    if cutoff>known or snapshot>known:
        raise ValueError('Market/snapshot cutoff exceeds actual knowledge cutoff')
    if not isinstance(date_label_evidence,str) or not date_label_evidence.strip():
        raise ValueError('Daily15:00 observation labels need explicit source evidence')
    date.fromisoformat(start_date)
    calendar=list(trading_dates)
    if calendar!=sorted(set(calendar)):
        raise ValueError('Trading dates must be unique and sorted')
    for day in calendar:
        date.fromisoformat(day)
    previous={day:calendar[i-1] if i else None for i,day in enumerate(calendar)}
    dates=[day for day in calendar if day>=start_date and _moment(_stamp(day))<=cutoff]
    if not dates:
        raise ValueError('No observation dates within the requested window')
    target=_normalise(target_rows)
    target=[row for row in target if _moment(_stamp(row['date']))<=cutoff]
    if not target:
        raise ValueError('No target data at market cutoff')
    names={n:target[-1][f'sw{n}'] for n in SW}
    if not names[level]:
        raise ValueError('Target has no industry name for requested level')
    aggregates={n:{day:dict(expected=0,valid=0,rising=0,falling=0,flat=0,return_sum=0.0,
                           ma_eligible=0,above_ma20=0,amount=0.0,amount_eligible=0,target_included=False)
                   for day in dates} for n in SW if names[n]}
    seen=set()
    for code,raw in stocks:
        if not isinstance(code,str) or not code or code in seen:
            raise ValueError('Stock identities must be nonempty and unique')
        seen.add(code)
        rows=[r for r in _normalise(raw) if _moment(_stamp(r['date']))<=cutoff]
        observations={r['date']:r for r in rows}
        adjusted=_levels(rows)
        for day in dates:
            prior=observations.get(previous[day]);current=observations.get(day)
            if prior is None:
                continue
            for n in aggregates:
                if prior[f'sw{n}']!=names[n]:
                    continue
                agg=aggregates[n][day];agg['expected']+=1
                if (current is None or current['close'] is None or current['prev_close'] is None
                        or min(current['close'],current['prev_close'])<=0
                        or current['volume'] is None or current['volume']<=0):
                    continue
                change=_number(current['close']/current['prev_close']-1)
                agg['valid']+=1;agg['return_sum']+=change
                agg['rising' if change>0 else 'falling' if change<0 else 'flat']+=1
                agg['target_included'] |= code==target_code
                above=adjusted.get(day,{}).get('above_ma20')
                if above is not None:
                    agg['ma_eligible']+=1;agg['above_ma20']+=int(above)
                if current['amount'] is not None:
                    agg['amount_eligible']+=1;agg['amount']+=current['amount']
    target_adjusted=_levels(target)
    stock_bars=[dict(time=_stamp(day),close=target_adjusted[day]['level'],available_at=snapshot_available_at)
                for day in dates if day in target_adjusted]
    if stock_bars:
        base=stock_bars[0]['close']
        for row in stock_bars:
            row['close']=row['close']/base*100
    limitations=['自建等权申万标签组合，非官方申万指数；不是全收益指数或实际可交易组合。',
        '历史日行分类来自当前供应商快照，未证明每次分类变更当时公开可得，只有回溯弱代理资格。',
        '以上一共同交易观察日的分类纳入本日收益；无有效成交/报价的成分不补0，覆盖率逐日保留。',
        'close/当日前收参考价的链式收益依赖供应商参考价，同行全部权息未逐事件核验。',
        '文件池由请求明示；未认证完整历史上市/退市证券宇宙，不能据此宣称无幸存偏差。']
    if target[-1]['date']!=dates[-1]:
        limitations.append('目标股票行情未覆盖市场截止末观察日，不补齐价格。')
    group='local-sw-inputs:'+hashlib.sha256(json.dumps(source_metadata or {},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    levels={}
    for n,days in aggregates.items():
        records=[];bars=[];value=100.0;reset_count=0
        for day,agg in days.items():
            row=dict(date=day,expected_constituents=agg['expected'],valid_returns=agg['valid'],
                missing_returns=agg['expected']-agg['valid'],coverage=agg['valid']/agg['expected'] if agg['expected'] else None,
                rising=agg['rising'],falling=agg['falling'],flat=agg['flat'],
                rising_share=agg['rising']/agg['valid'] if agg['valid'] else None,
                ma20_eligible=agg['ma_eligible'],above_ma20=agg['above_ma20'],
                above_ma20_share=agg['above_ma20']/agg['ma_eligible'] if agg['ma_eligible'] else None,
                amount=agg['amount'] if agg['amount_eligible'] else None,amount_constituents=agg['amount_eligible'],
                target_included=agg['target_included'],daily_return=agg['return_sum']/agg['valid'] if agg['valid']>=2 else None)
            records.append(row)
            if row['daily_return'] is None:
                if bars: reset_count+=1
                bars=[];value=100.0
                continue
            value=100.0 if not bars else _number(value*(1+row['daily_return']))
            bars.append(dict(time=_stamp(day),close=value,available_at=snapshot_available_at))
        reference=dict(id=f'LOCAL-SW{n}-{names[n]}-EW-LAG1',benchmark_id=f'LOCAL-SW{n}-{names[n]}-EW-LAG1',
            currency='CNY',bars=bars,available_at=snapshot_available_at,snapshot_available_at=snapshot_available_at,
            comparable=True,comparison_basis=BASIS,proxy=True,classification_known=False,
            classification_available_at=snapshot_available_at,source_group=group,
            classification_system=f'vendor 新版申万{n}级名称',membership_source='逐日行标签，前一共同交易观察日归属',
            method='每日有效成分等权close/前收参考价收益，至少2家；缺日后仅保留最新连续有效段',
            official_index=False,total_return=False,target_included=any(r['target_included'] for r in records),
            limitations=limitations+(['有效成分不足导致链条重启；仅输出最新连续段，不把缺日收益当0。'] if reset_count else []))
        levels[str(n)]=dict(reference=reference,breadth=records,latest=records[-1] if records else None,
            coverage=dict(input_securities=len(seen),days=len(records),continuous_proxy_points=len(bars),chain_resets=reset_count))
    selected=levels[str(level)]
    return dict(schema_version='local-sw-industry-context/1',status='weak_proxy' if selected['reference']['bars'] else 'unavailable',
        target_code=target_code,classification={str(n):names[n] for n in names},selected_level=level,
        reference=selected['reference'],stock_bars=stock_bars,breadth=selected['breadth'],coverage=selected['coverage'],levels=levels,
        metadata=dict(security_id=security_id,target_code=target_code,market_cutoff=market_cutoff,asof=asof,
            snapshot_available_at=snapshot_available_at,qualification='weak_proxy',comparison_basis=BASIS,
            date_label_evidence=date_label_evidence,source_group=group,source_metadata=source_metadata or {},
            classification_asof=target[-1]['date'],limitations=limitations,
            ma20_definition='参考收益链最近20个有效价格观察，完整暖启动；分母只计可算MA20成员',
            return_definition='成分收益close/前收参考价-1，使用t-1行业分类；当日有效成分等权',
            target_dependence='行业组合包含目标股时不能视为独立证据，target_included逐日保留'))


def read_snapshot(path, *, start_date, end_date, warmup_rows=21):
    """Read only the CSV suffix needed for the requested window plus a MA20/lag buffer."""
    path=Path(path)
    before=path.stat()
    with path.open('rb') as handle:
        preamble=handle.readline();header_bytes=handle.readline();data_start=handle.tell()
        header=next(csv.reader([header_bytes.decode('gb18030').lstrip('\ufeff')]))
        required={'股票代码','交易日期','收盘价','前收盘价','成交量',*SW.values()}
        if not required.issubset(header):
            raise ValueError(f'{path.name}: missing columns {sorted(required-set(header))}')
        limit=min(before.st_size,262144)
        while True:
            start=max(data_start,before.st_size-limit);handle.seek(start)
            payload=handle.read()
            if start>data_start:
                payload=payload.partition(b'\n')[2]
            source_rows=list(csv.DictReader(io.StringIO(payload.decode('gb18030')),fieldnames=header))
            eligible=[r for r in source_rows if r['交易日期']<=end_date]
            if (eligible and eligible[-1]['交易日期']<start_date) or sum(r['交易日期']<start_date for r in eligible)>=warmup_rows or start==data_start:
                break
            limit=min(before.st_size,limit*2)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('Source changed while reading; freeze a new snapshot')
    rows=[];code=path.stem
    prior=[r for r in eligible if r['交易日期']<start_date][-warmup_rows:]
    selected=prior+[r for r in eligible if r['交易日期']>=start_date]
    for row in selected:
        if row['股票代码']!=code:
            raise ValueError('CSV code and filename identity differ')
        rows.append(dict(date=row['交易日期'],close=row['收盘价'],prev_close=row['前收盘价'],volume=row['成交量'],
                         amount=row.get('成交额'),**{f'sw{n}':row.get(field) for n,field in SW.items()}))
    return code,rows,dict(file=str(path.resolve()),size_bytes=before.st_size,mtime_ns=before.st_mtime_ns,
        read_suffix_bytes=len(payload),rows_selected=len(rows),header_sha256=hashlib.sha256(header_bytes).hexdigest(),
        suffix_sha256=hashlib.sha256(payload).hexdigest(),full_file_hashed=False,entire_data_section_read=start==data_start,
        fingerprint_scope='CSV header and exact read suffix, not a claimed full-file content fingerprint')


def build_from_request(request):
    paths=[Path(p).resolve() for p in request['files']]
    if not paths or len(paths)!=len(set(paths)):
        raise ValueError('Provide a nonempty explicit, unique CSV pool')
    target_path=Path(request['target_file']).resolve()
    if target_path not in paths:
        raise ValueError('Target must belong to the explicit CSV pool')
    end=_moment(request['market_cutoff']).astimezone(TZ).date().isoformat()
    code,target,description=read_snapshot(target_path,start_date=request['start_date'],end_date=end)
    if code!=request['target_code']:
        raise ValueError('Target code differs from CSV')
    manifests=[]
    def stocks():
        for path in paths:
            if path==target_path:
                current,rows,meta=code,target,description
            else:
                current,rows,meta=read_snapshot(path,start_date=request['start_date'],end_date=end)
            manifests.append(meta)
            yield current,rows
    calendar=request.get('trading_dates')
    if calendar is None:
        calendar=json.loads(Path(request['trading_dates_file']).read_text(encoding='utf-8-sig'))
    result=compute_context(stocks(),target_rows=target,target_code=code,level=request.get('level',2),
        start_date=request['start_date'],market_cutoff=request['market_cutoff'],asof=request['asof'],
        snapshot_available_at=request['snapshot_available_at'],trading_dates=calendar,
        date_label_evidence=request['date_label_evidence'],source_metadata={'file_chunks':manifests},
        security_id=request.get('security_id'))
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request-json',required=True);parser.add_argument('--output-dir',required=True)
    args=parser.parse_args(argv)
    request=json.loads(Path(args.request_json).read_text(encoding='utf-8-sig'))
    spec=importlib.util.spec_from_file_location('harmonic_analysis',Path(__file__).with_name('harmonic_analysis.py'))
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    target=guard.safe_output(Path(args.output_dir))
    result=build_from_request(request)
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.industry-',dir=target.parent))
    try:
        with (stage/'industry-context.json').open('x',encoding='utf-8') as handle:
            json.dump(result,handle,ensure_ascii=False,indent=2,allow_nan=False)
            handle.write('\n')
        stage.rename(target)
    except BaseException:
        if stage.resolve().parent==target.parent.resolve():
            shutil.rmtree(stage)
        raise
    print(json.dumps(dict(output=str(target),status=result['status'],coverage=result['coverage']),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
