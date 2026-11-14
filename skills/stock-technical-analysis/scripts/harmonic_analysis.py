"""Local harmonic report. No network, account access, orders or implicit installation."""

import argparse
import csv
from datetime import datetime, time, timezone, timedelta
import hashlib
import html
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import stat
import sys
import tempfile


def load_core():
    spec = importlib.util.spec_from_file_location('harmonic_core', Path(__file__).with_name('harmonic_core.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def moment(value, *, date_end=False):
    if not isinstance(value, str):
        raise ValueError('时间必须是ISO字符串')
    if len(value) == 10:
        if not date_end:
            raise ValueError('asof须包含时区及时间')
        return datetime.combine(datetime.fromisoformat(value).date(), time.max,
                                tzinfo=timezone(timedelta(hours=8)))
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('分钟时间须包含时区')
    return result


def read_rows(path, encoding):
    if path.suffix.lower() == '.json':
        rows = json.loads(path.read_text(encoding=encoding))
        if not isinstance(rows, list):
            raise ValueError('行情JSON须为记录数组')
        return rows
    lines = path.read_text(encoding=encoding).splitlines()
    if lines and '交易日期' not in lines[0] and len(lines) > 1 and '交易日期' in lines[1]:
        lines = lines[1:]
    aliases = {'time': ('time', 'timestamp', '交易日期'), 'open': ('open', '开盘价'),
               'high': ('high', '最高价'), 'low': ('low', '最低价'), 'close': ('close', '收盘价'),
               'code': ('code', '股票代码'), 'volume': ('volume', '成交量'),
               'amount': ('amount', '成交额'), 'prev_close': ('prev_close', '前收盘价')}
    reader = csv.DictReader(io.StringIO('\n'.join(lines)))
    headers = reader.fieldnames or []
    columns = {key: next((v for v in values if v in headers), None) for key, values in aliases.items()}
    if any(columns[k] is None for k in ('time', 'open', 'high', 'low', 'close')):
        raise ValueError('缺少完整OHLC，close-only数据不能用于谐波')
    result = []
    for row in reader:
        item = {key: row[column] for key, column in columns.items() if column}
        if 'available_at' in row and row['available_at']:
            item['available_at'] = row['available_at']
        if 'completed' in row:
            value = row['completed'].strip().lower()
            if value not in ('true', 'false'):
                raise ValueError('CSV completed须为true/false')
            item['completed'] = value == 'true'
        result.append(item)
    return result


def prepare(request, request_path):
    security = request.get('security', {})
    if not isinstance(security, dict):
        raise ValueError('security须为对象')
    for field in ('security_id', 'market', 'exchange', 'code', 'currency'):
        if not isinstance(security.get(field), str) or not security[field].strip():
            raise ValueError('security缺少字段: ' + field)
    if security['market'] != 'A-share':
        raise ValueError('本版数据门仅验收A-share；其他市场暂未放行')
    if request.get('timeframe') not in ('1d', '1w', '1mo', '6mo', '1y', '5m', '15m', '60m'):
        raise ValueError('不支持的timeframe')
    asof = moment(request.get('asof'))
    available = moment(request.get('snapshot_available_at'))
    if available > asof:
        raise ValueError('行情快照在研究截止后才可得，不能回填历史研究')
    for field in ('completion_evidence', 'session_evidence'):
        if not isinstance(request.get(field), str) or not request[field].strip():
            raise ValueError('须声明' + field)
    if request.get('bar_time_semantics') not in ('period_label_date', 'bar_end_time'):
        raise ValueError('须声明K线时间是日期标签或结束时刻')
    adjustment = request.get('adjustment', {})
    if not isinstance(adjustment, dict):
        raise ValueError('adjustment须为对象')
    if adjustment.get('mode') not in ('raw', 'qfq', 'hfq'):
        raise ValueError('adjustment.mode须为raw/qfq/hfq')
    if type(adjustment.get('verified')) is not bool or not isinstance(adjustment.get('evidence'), str) or not adjustment['evidence'].strip():
        raise ValueError('须明确复权是否已核验及依据')
    if not isinstance(request.get('source'), str) or not request['source'].strip():
        raise ValueError('须提供source')
    if request.get('data_kind') not in ('market', 'synthetic'):
        raise ValueError('须区分market/synthetic')
    path = Path(request['input_file'])
    if not path.is_absolute():
        path = request_path.parent / path
    raw = read_rows(path, request.get('encoding', 'utf-8-sig'))
    bars, excluded, previous, date_only = [], 0, None, False
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('每根行情须为对象')
        stamp = row.get('time', row.get('timestamp'))
        at = moment(stamp, date_end=True)
        if len(stamp) == 10 and request.get('daily_end_time'):
            if request['timeframe'] != '1d' or not request.get('daily_end_evidence'):
                raise ValueError('明确日线结束时刻需要日线周期及依据')
            at = datetime.combine(at.date(), time.fromisoformat(request['daily_end_time']),
                                  timezone(timedelta(hours=8)))
        if previous is not None and at <= previous:
            raise ValueError('行情须严格按时间递增，不能重复或混用日期精度')
        previous = at
        date_only |= len(stamp) == 10
        if len(stamp) == 10 and request['timeframe'] not in ('1d', '1w', '1mo', '6mo', '1y'):
            raise ValueError('分钟K线须有带时区的结束时间')
        if (len(stamp) == 10) != (request['bar_time_semantics'] == 'period_label_date'):
            raise ValueError('行情时间精度与所声明语义不一致')
        if 'code' in row and str(row['code']).strip().lower() not in {
                security['code'].lower(), security['security_id'].lower(),
                (security['exchange'] + security['code']).lower()}:
            raise ValueError('行情证券代码与请求不一致')
        row_available = moment(row['available_at']) if 'available_at' in row else available
        if at > asof or row_available > asof or row.get('completed', True) is not True:
            excluded += 1
            continue
        if any(isinstance(row.get(k), bool) for k in ('open', 'high', 'low', 'close')):
            raise ValueError('OHLC不能为布尔值')
        values = {k: float(row[k]) for k in ('open', 'high', 'low', 'close')}
        if any(not math.isfinite(v) or v <= 0 for v in values.values()):
            raise ValueError('OHLC须为正的有限数值')
        if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
            raise ValueError('OHLC高低关系错误')
        extra = {}
        if 'mechanical_gap' in row:
            if type(row['mechanical_gap']) is not bool:
                raise ValueError('mechanical_gap须为布尔值')
            extra['mechanical_gap']=row['mechanical_gap']
        if 'session_id' in row:
            if not isinstance(row['session_id'],str) or not row['session_id'].strip():
                raise ValueError('session_id须为非空字符串')
            extra['session_id']=row['session_id']
        for key in ('volume', 'amount', 'prev_close'):
            if key in row and row[key] not in ('', None):
                if isinstance(row[key], bool):
                    raise ValueError(key+'不能为布尔值')
                value = float(row[key])
                if not math.isfinite(value) or value < 0:
                    raise ValueError(key+'须为非负有限数')
                extra[key] = value
        bars.append({'time': at.astimezone(timezone(timedelta(hours=8))).isoformat(), **values,
                     **extra, 'completed': True, 'available_at': row_available.isoformat()})
    if not bars:
        raise ValueError('截止前没有已完成且合格的OHLC')
    metadata = {key: request[key] for key in ('security', 'timeframe', 'asof', 'source', 'data_kind', 'adjustment')}
    metadata.update({key: request[key] for key in ('snapshot_available_at', 'completion_evidence',
                                                 'session_evidence', 'bar_time_semantics')})
    metadata.update(generated_at=datetime.now(timezone.utc).isoformat(),
                    input_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    used_bars=len(bars), excluded_bars=excluded, data_asof=bars[-1]['time'],
                    timestamp_policy='date_end_conservative_not_actual_close' if date_only else 'explicit_bar_end',
                    freshness='requires_source_calendar_review',
                    period_qualification='daily_input_reviewed' if request['timeframe']=='1d' else 'diagnostic_not_calibrated',
                    geometry_price_basis_qualified=bool(adjustment['verified'] and adjustment['mode'] != 'raw'))
    return bars, metadata


def safe_output(output):
    if not output.is_absolute():
        raise ValueError('输出目录须为绝对路径，位于非Git私有目录')
    repo = Path(__file__).resolve().parents[3]
    target = output.resolve()
    skill_root = Path(__file__).resolve().parents[1]
    if target == skill_root or skill_root in target.parents:
        raise ValueError('研究输出不得写入Skill安装目录')
    if (repo/'release-files.txt').is_file() and (target == repo or repo in target.parents):
        raise ValueError('研究输出不得写入源码仓库')
    for ancestor in (output, *output.parents):
        try:
            attrs = ancestor.lstat()
        except FileNotFoundError:
            attrs = None
        if attrs is not None and (stat.S_ISLNK(attrs.st_mode) or getattr(attrs, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('输出路径不得经符号链接或目录联接')
        if (ancestor / '.git').exists():
            raise ValueError('研究输出不得写入Git目录')
    if any((ancestor/'.git').exists() for ancestor in (target, *target.parents)):
        raise ValueError('研究输出不得写入Git目录')
    if output.exists():
        raise ValueError('输出目录已存在，不覆盖历史快照')
    return target


def candidates(result):
    values = result['forming'] + result['previews'] + result['patterns']
    return sorted(values, key=lambda x: (x['first_known_at'], x['candidate_id']))


def render_charts(bars, result, meta, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    import plotly.graph_objects as go

    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    start = max(0, len(bars) - 180)
    selected = [item for item in candidates(result)
                if item['first_known_at'] >= bars[start]['time'] or
                item['status'] in ('forming', 'active_d_preview')][-4:]
    for item in selected:
        start = min(start, item['points']['X']['index'])
    title = f"{meta['security']['security_id']} · {meta['timeframe']} · {meta['adjustment']['mode']}"
    qualifier = '几何价格基准已声明核验；新鲜度另审' if meta['geometry_price_basis_qualified'] else '诊断样本：复权影响未排除，不作当前执行依据'
    if meta['data_kind'] == 'synthetic':
        qualifier = '合成教学数据，不代表真实证券'
    fig, ax = plt.subplots(figsize=(14, 8))
    plot = go.Figure(go.Candlestick(x=[b['time'] for b in bars[start:]],
        open=[b['open'] for b in bars[start:]], high=[b['high'] for b in bars[start:]],
        low=[b['low'] for b in bars[start:]], close=[b['close'] for b in bars[start:]],
        increasing_line_color='#d94b4b', decreasing_line_color='#238b57', name='OHLC'))
    for i in range(start, len(bars)):
        b = bars[i]
        color = '#d94b4b' if b['close'] >= b['open'] else '#238b57'
        ax.vlines(i, b['low'], b['high'], color=color, linewidth=.8)
        height = abs(b['close'] - b['open'])
        ax.add_patch(Rectangle((i-.3, min(b['open'], b['close'])), .6, max(height, b['close']*.0001),
                               facecolor=color, edgecolor=color))
    colors = ['#2363ba', '#9a4bba', '#087f8c', '#b05a20']
    for item, color in zip(selected, colors):
        points = [(letter, p) for letter, p in item['points'].items() if p.get('index') is not None]
        x = [p['index'] for _, p in points]
        y = [p['price'] for _, p in points]
        confirmed = item['status'] == 'geometry_confirmed'
        direction = '看涨' if item['direction']=='bullish' else '看跌'
        state = {'geometry_confirmed':'几何确认','forming':'形成中','active_d_preview':'临时D'}.get(item['status'], item['status'])
        scenario = {'invalidated':'已失效','active':'尚未记录失效','pending':'待确认'}.get(item['scenario_status'],item['scenario_status'])
        label = f"{item['pattern']} {direction} / {state} / {scenario}"
        ax.plot(x, y, '-' if confirmed else '--', marker='o', color=color, label=label,
                markerfacecolor=color if confirmed else 'white', linewidth=1.5)
        for letter, p in points:
            ax.annotate(letter, (p['index'], p['price']), xytext=(4, 5), textcoords='offset points', color=color)
        hover = ['<br>'.join(f'{html.escape(str(k))}: {html.escape(str(v))}' for k,v in p.items()) for _,p in points]
        plot.add_trace(go.Scatter(x=[p['time'] for _,p in points], y=y, mode='lines+markers+text',
            text=[letter for letter,_ in points], textposition='top center', name=label,
            line=dict(color=color, dash='solid' if confirmed else 'dash'),
            hovertext=hover, hoverinfo='text', marker=dict(symbol='circle' if confirmed else 'circle-open')))
        known_index = next((i for i,b in enumerate(bars) if b['time'] >= item['first_known_at']), len(bars)-1)
        end_at = item.get('invalidated_at') or item.get('withdrawn_at') or bars[-1]['time']
        end_index = next((i for i,b in enumerate(bars) if b['time'] >= end_at), len(bars)-1)
        for zone in item['prz']['intervals']:
            ax.fill_between([known_index, end_index], zone['lower'], zone['upper'], color='#dfa520', alpha=.15)
            plot.add_shape(type='rect', x0=bars[known_index]['time'], x1=bars[end_index]['time'],
                y0=zone['lower'], y1=zone['upper'], fillcolor='#dfa520', opacity=.13, line_width=0)
    ticks = list(range(start, len(bars), max(1, (len(bars)-start)//9)))
    ax.set_xticks(ticks, [bars[i]['time'][:10] for i in ticks], rotation=25, ha='right')
    ax.set_ylabel(f"价格 / {meta['security']['currency']} ({meta['adjustment']['mode']})")
    ax.set_title(title + '\n' + qualifier)
    if not selected:
        ax.text(.02,.94, '最近180根无新候选；完整历史集合见JSON', transform=ax.transAxes, color='#666666')
    ax.grid(alpha=.15)
    if selected:
        ax.legend(loc='best', fontsize=8)
    fig.text(.015,.01, f"asof={meta['asof']} | data={meta['data_asof']} | project-harmonic-v1 | 最近180根新候选/当前形成中最多4个；全量见JSON", fontsize=8)
    fig.tight_layout(rect=(0,.03,1,1))
    fig.savefig(output/'chart.png', dpi=145)
    plt.close(fig)
    plot.update_layout(title=html.escape(title+' | '+qualifier), height=780,
                       xaxis_rangeslider_visible=False, yaxis_title='价格 / '+meta['security']['currency'],
                       legend=dict(orientation='h'), margin=dict(t=150))
    plot.add_annotation(text=html.escape(f"asof {meta['asof']} · data {meta['data_asof']} · 最近4候选 · 全量及比例见analysis.json"),
                        xref='paper', yref='paper', x=0, y=1.07, showarrow=False)
    plot.write_html(output/'chart.html', include_plotlyjs=True, full_html=True, auto_open=False)


def markdown_report(result, meta):
    lines = ['# 谐波专题报告', '', f"证券：{meta['security']['security_id']}；周期：{meta['timeframe']}；币种：{meta['security']['currency']}。",
             f"输出时刻：{meta['generated_at']}（UTC系统记录；不同于研究及资料截止）。",
             f"研究截止：{meta['asof']}；行情末端：{meta['data_asof']}；复权：{meta['adjustment']['mode']}。", '',
             f"计算状态：`{result['status']}`；全历史几何确认 {len(result['patterns'])}（其中后续已失效 {sum(p['scenario_status']=='invalidated' for p in result['patterns'])}）；形成中 {len(result['forming'])}；临时D预览 {len(result['previews'])}；撤销 {len(result['withdrawn'])}。历史确认数不代表当前有效信号数。",
             '', '**结论边界**：几何确认只说明所选比例及摆动规则成立，不代表反转确认、已盈利或可以成交。新鲜度仍须按来源、交易日历和周期复核。',
             f"价格基准核验声明：{meta['geometry_price_basis_qualified']}；依据：{meta['adjustment']['evidence']}。",
             f"数据类型：{meta['data_kind']}；来源：{meta['source']}；使用{meta['used_bars']}根，排除截止后或未完成{meta['excluded_bars']}根。",
             f"快照最早可得：{meta['snapshot_available_at']}；周期资格：{meta['period_qualification']}。声明须由宿主核验，不是工具独立证明。",
             f"时间政策：{meta['timestamp_policy']}；日期级数据仅给日精度的可知时间，日末是保守上界，不是真实收盘时刻。", '',
             '![谐波图](chart.png)', '', '[打开离线交互图](chart.html)。琥珀色是PRZ；实线为几何确认；虚线为候选／临时点。图上仅显示最近180根内新出现及当前形成中候选，最多4个；完整历史集合及比例在analysis.json。', '',
             '|形态／方向|几何／情景状态|首次可知|几何确认|PRZ|情景失效界及时间|', '|---|---|---|---|---|---|']
    for item in candidates(result):
        zones = '; '.join(f"{z['lower']:.4f}–{z['upper']:.4f}" for z in item['prz']['intervals'])
        lines.append(f"|{item['pattern']} / {item['direction']}|{item['status']} / {item['scenario_status']}|{item['first_known_at']}|{item.get('confirmed_at') or '待确认'}|{zones}|{item['invalidation']} / {item.get('invalidated_at') or '未记录'}|")
    if not candidates(result):
        lines.append('|无合格候选|仅代表本次规则与连续摆动窗口|||||')
    lines += ['', '## 阅读顺序与后续核验', '',
              '1. 先核证券、截止、周期、复权与行情是否仍有参考性。原价或复权未核验输出仅作诊断；合成数据仅教学。',
              '2. 再看确认点与活动末端。XABC给潜在D的价格集合，不给未来发生日期；到区、几何确认和反转确认分开。',
              '3. 对照analysis.json中的实际比例、checks、PRZ约束、拒绝／撤销原因及events。无交集就无合格PRZ。',
              '4. 结合独立的趋势、量价、市场环境和已有基本面；不把相关形态数当作独立票数。',
              '5. A股看空候选用于回避或已有持仓风险评估，不自动转为可融券订单。T+1、限价、跳空和流动性可能使计划无法执行。', '',
              '本工具不制定个性化仓位、不联网、不下单。研究室由宿主按已配置入口读档／归档报告；本工具不直接登记档案或复制附件。输入与图表请共同留在非Git私有目录。', '',
              f"输入SHA256：`{meta['input_sha256']}`。规则与参数：`{result['metadata']['profile']}`。", '',
              '扫描只覆盖连续已确认XABC/XABCD窗口；默认反向阈值5%、比例容差5%是研究设置，尚未证明适合所有A股或分钟周期。']
    lines += ['', '限制：' + '；'.join(result['limitations'])] if result['limitations'] else []
    return '\n'.join(lines) + '\n'


def run(request_path, output):
    request_path = Path(request_path).resolve()
    request = json.loads(request_path.read_text(encoding='utf-8-sig'))
    if not isinstance(request, dict):
        raise ValueError('请求须为JSON对象')
    if request.get('timeframe') in ('5m','15m','60m'):
        raise ValueError('分钟级分析已暂停；当前聚焦日/周/月/半年/年')
    target = safe_output(Path(output))
    bars, metadata = prepare(request, request_path)
    context = {'security_id': metadata['security']['security_id'], 'timeframe': metadata['timeframe'],
               'adjustment': metadata['adjustment']['mode']}
    result = load_core().analyze_harmonics(bars, profile=request.get('profile'), context=context)
    if result['status'] == 'invalid_input':
        raise ValueError('; '.join(result['limitations']))
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.harmonic-', dir=target.parent))
    try:
        document = {'schema_version': 'stock-harmonic-report/1', 'input_metadata': metadata,
                    'bars': bars, 'analysis': result}
        (staging/'analysis.json').write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        render_charts(bars, result, metadata, staging)
        (staging/'report.md').write_text(markdown_report(result, metadata), encoding='utf-8')
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging)
        raise
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description='A股本地OHLC谐波检测与离线图表；不联网、不交易。')
    parser.add_argument('--request-json', required=True, help='证券、截止、数据、复权及profile请求文件')
    parser.add_argument('--output-dir', required=True, help='不存在的非Git私有绝对目录')
    args = parser.parse_args(argv)
    try:
        result = run(args.request_json, args.output_dir)
    except (ValueError, KeyError, OSError, ImportError, TypeError) as error:
        print('未生成报告：'+str(error), file=sys.stderr)
        return 2
    print(str(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
