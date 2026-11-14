"""Compact, offline technical research presentation; calculations stay in the caller."""

import base64
from datetime import datetime,timezone,timedelta
import html
import math
from pathlib import Path


PERIODS={'1d':'日线','1w':'周线','1mo':'月线','6mo':'半年线','1y':'年线'}
STATES={'strong':'偏强','weak':'偏弱','mixed':'方向分歧','unknown':'暂不判断'}
QUALIFICATIONS={'validated':'本次研究输入','diagnostic':'仅作诊断','paused':'已暂停'}
CHECKS={'dated_security_rules':'制度与生效时间','trigger_within_limits':'触发价限价范围',
        'invalidation_within_limits':'失效价限价范围','session_and_state':'会话与停牌状态',
        'quantity_rule':'数量规则','risk_budget':'风险预算','cash_under_declared_costs':'资金与费用'}


def _number(value, digits=2):
    if value is None or isinstance(value,bool):
        return '缺失'
    try:
        return f'{float(value):,.{digits}f}' if math.isfinite(float(value)) else '缺失'
    except (TypeError,ValueError):
        return '缺失'


def _text(value):
    return html.escape(str(value)).replace('\n',' ').replace('|','／')


def _when(value):
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')+'（上海）'
    except ValueError:
        pass
    return str(value)


def _proxy(comparison):
    return comparison.get('weak_proxy') is True or comparison.get('qualification')=='weak_proxy'


def _comparisons(document):
    return [row for row in document.get('background',{}).get('comparisons',[])
            if row.get('status')=='ok' and len(row.get('common_dates',[]))>=2
            and len(row.get('relative_index',[]))==len(row['common_dates'])][:2]


def _momentum(daily, latest):
    hist=latest.get('macd_hist');rsi=latest.get('rsi14')
    values=daily.get('series',{}).get('macd_hist',[])
    previous=values[-2] if len(values)>1 else None
    if hist is None:
        text='历史或依赖不足，动量暂不判断。'
    elif previous is None:
        text=f'MACD柱为{_number(hist)}，尚无可比前值。'
    elif hist<0:
        text='MACD负柱'+('扩大，短线压力加重。' if hist<previous else '收窄，但尚未转正。' if hist>previous else '持平，尚未转正。')
    else:
        text='MACD柱'+('转正。' if previous<0 else '上升。' if hist>previous else '回落。' if hist<previous else '持平。')
    if rsi is not None:
        text+=f' RSI {_number(rsi)}'+('，低位不等于见底。' if rsi<30 else '，高位不等于见顶。' if rsi>70 else '，只作辅助观察。')
    return text


def _model(document, assessments):
    daily=document.get('timeframes',{}).get('1d',{})
    assessment=assessments.get('1d',{})
    state=assessment.get('state','unknown')
    latest=assessment.get('latest',{})
    close=assessment.get('close')
    ma20,ma60=latest.get('sma20'),latest.get('sma60')
    reasons={'strong':'收盘高于20日均线，且20日均线高于60日均线。',
             'weak':'收盘低于20日均线，且20日均线低于60日均线。',
             'mixed':'价格与两条均线尚未形成一致方向，先分清期限。',
             'unknown':'当前历史或可用指标不足，不给确定趋势结论。'}
    conditions={'strong':['延续：收盘保持在20日均线上方，且20日均线高于60日均线。','改判：跌回20日均线下方，或均线排列转弱时重新评估。'],
                'weak':['延续：收盘仍低于20日均线，且20日均线低于60日均线。','修复：收盘回到20日均线上方，且20日均线不再下降；先看修复，不直接判反转。'],
                'mixed':['先看：价格与20日均线的相对位置是否稳定，再核20日与60日均线方向。','改判：形成一致排列后重新判断，不能把单日反弹当趋势确认。'],
                'unknown':['先补：合格完整日线及足够历史。','暂缓：不把缺失指标、空白图或旧形态当当前买卖信号。']}
    comparisons=_comparisons(document)
    relative=[];breadth=[]
    for comparison in comparisons:
        window=comparison.get('windows',{}).get('20',{})
        name=comparison.get('label') or comparison.get('id') or '参考组合'
        excess=window.get('excess_return')
        phrase=(f'近20个交易日相对{name}'+('落后' if excess<0 else '领先')+f'{_number(abs(excess)*100)}个百分点。'
                if excess is not None else f'{name}近期相对表现不足以判断。')
        if _proxy(comparison):
            phrase+=' 自建行业组合，非官方申万指数。'
        latest_breadth=comparison.get('latest_breadth',{})
        valid=latest_breadth.get('valid_returns');eligible=latest_breadth.get('ma20_eligible');above=latest_breadth.get('above_ma20')
        if valid is not None:
            note=f'行业收益有效{valid}只'
            if eligible and above is not None:
                note+=f'；20日均线上方{above}/{eligible}（{_number(above/eligible*100,1)}%）'
            breadth.append(note+'。收益与均线的有效样本分母分别保留。')
        relative.append(phrase)
    frames=[]
    for frame,label in PERIODS.items():
        value=document.get('timeframes',{}).get(frame,{})
        meta=value.get('metadata',{});summary=assessments.get(frame,{})
        count=len(value.get('bars',[]))
        if not count:
            direction='暂无完整可用K线'
        elif summary.get('state','unknown')=='unknown':
            direction='均线历史不足，暂不判断' if count<60 else '指标缺失，暂不判断'
        else:
            direction=STATES.get(summary.get('state'),'暂不判断')
            if summary.get('state')=='mixed': direction+='（价格与均线未同向）'
        frames.append([label,direction,str(meta.get('data_asof','未提供'))[:10],
                       QUALIFICATIONS.get(meta.get('qualification'),'资格待核')])
    warnings=[]
    qualification=daily.get('metadata',{}).get('qualification')
    if qualification!='validated':
        warnings.append('日线输入仅作诊断或尚不完整，不能升级为执行确认。')
    if any(v.get('metadata',{}).get('qualification')=='diagnostic' for k,v in document.get('timeframes',{}).items() if k in PERIODS and k!='1d'):
        warnings.append('长周期原价、复权或日历未全部核验的部分仅作诊断。')
    if document.get('status')=='paused':
        warnings.append('分钟研究已暂停，本次没有可运行的日线输入。')
    if any(_proxy(row) for row in comparisons):
        warnings.append('行业为自建参考收益组合；成员、覆盖和回溯限制见附录，不称官方申万指数。')
    if not any(row.get('reference')=='industry' for row in comparisons):
        warnings.append('行业层尚无可比结果，不补造行业排名。')
    review=document.get('review',{})
    if review.get('status')=='invalid_input':
        review_note='旧项复核未完成：'+ '；'.join(review.get('limitations',[]))
    elif review.get('rows'):
        labels={'not_fulfilled':'未达到','partially_fulfilled':'部分达到','pending':'待验证',
                'cannot_determine':'证据不足','fulfilled':'已达到','unknown':'未知'}
        review_note='；'.join(labels.get(row.get('condition_status'),'按原条件复核') for row in review['rows'])+'。不据此统计预测命中或交易盈亏。'
    else:
        review_note='本次没有已核的旧项回顾结果，不补造命中率。'
    previous=document.get('previous_technical',{}).get('timeframes',{}).get('1d',{})
    change=[]
    if previous.get('status')=='comparable':
        for key,label in [('close','收盘'),('sma20','20日均线'),('macd_hist','MACD柱')]:
            item=previous.get('changes',{}).get(key)
            if item:
                change.append(f'{label} {_number(item["previous"])}→{_number(item["current"])}')
    return dict(state=state,title=STATES.get(state,'暂不判断'),reason=reasons.get(state,reasons['unknown']),
        close=close,ma20=ma20,ma60=ma60,change=latest.get('price_change'),
        momentum=_momentum(daily,latest),relative=relative,frames=frames,warnings=warnings,
        conditions=conditions.get(state,conditions['unknown']),review_note=review_note,breadth=' '.join(breadth),
        previous='；'.join(change) or '缺少同口径前次快照，不自动比较。',comparisons=comparisons,
        volume=('量比 '+_number(latest.get('volume_ratio20'))+' 倍；量比=当日量/此前20日均量，1倍表示等于此前均量；不据此识别机构身份。'
                if latest.get('volume_ratio20') is not None else '缺少合格量比，不填0或推断资金流。量比=当日量/此前20日均量。'))


def _price_chart(document,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    daily=document.get('timeframes',{}).get('1d',{})
    bars=daily.get('bars',[])
    if not bars:
        return None
    start=max(0,len(bars)-126);shown=bars[start:];series=daily.get('series',{})
    fig,axes=plt.subplots(2,1,figsize=(11.5,6.1),sharex=True,gridspec_kw={'height_ratios':[4,1]})
    candles=all(all(row.get(key) is not None for key in ('open','high','low','close')) for row in shown)
    if candles:
        for i,row in enumerate(shown):
            color='#c55248' if row['close']>=row['open'] else '#258274'
            axes[0].vlines(i,row['low'],row['high'],color=color,linewidth=.8)
            lower=min(row['open'],row['close']);height=abs(row['close']-row['open'])
            if height:
                axes[0].add_patch(Rectangle((i-.32,lower),.64,height,facecolor=color,edgecolor=color))
            else:
                axes[0].hlines(lower,i-.32,i+.32,color=color)
    else:
        axes[0].plot(range(len(shown)),[row['close'] for row in shown],color='#173b60',label='仅价格线，非OHLC')
    for key,color,label in [('sma20','#c68b33','20日均线'),('sma60','#4567a0','60日均线')]:
        values=series.get(key,[])[start:]
        if len(values)==len(shown) and any(value is not None for value in values):
            axes[0].plot(range(len(shown)),[math.nan if value is None else value for value in values],color=color,linewidth=1.25,label=label)
    if axes[0].get_legend_handles_labels()[0]: axes[0].legend(loc='upper left',frameon=False,ncol=3)
    volume_ratio=series.get('volume_ratio20',[])[start:]
    if len(volume_ratio)==len(shown) and any(value is not None for value in volume_ratio):
        axes[1].bar(range(len(shown)),[math.nan if value is None else value for value in volume_ratio],color='#94a7b8',width=.7)
    else:
        axes[1].text(.02,.4,'量比缺失，不代表成交量为0',transform=axes[1].transAxes,color='#596c7f')
    axes[1].axhline(1,color='#657b90',linestyle='--',linewidth=.9)
    ticks=list(range(0,len(shown),max(1,len(shown)//7)))
    if ticks[-1]!=len(shown)-1: ticks.append(len(shown)-1)
    axes[1].set_xticks(ticks,[str(shown[i]['time'])[:10] for i in ticks],rotation=20,ha='right')
    currency=document.get('security',{}).get('currency','价格')
    axes[0].set_ylabel('价格／元' if currency=='CNY' else currency)
    axes[1].set_ylabel('量比／倍')
    axes[0].set_title('日线：看价格与均线的位置；下栏量比=当日量/此前20日均量',loc='left',fontsize=12,pad=12)
    for axis in axes:
        axis.grid(alpha=.15);axis.spines[['top','right']].set_visible(False)
    fig.tight_layout();path=out/'price-overview.png';fig.savefig(path,dpi=150);plt.close(fig)
    return path.name


def _relative_chart(comparisons,out):
    if not comparisons:
        return None
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(len(comparisons),1,figsize=(11.5,2.4*len(comparisons)),squeeze=False)
    for axis,row in zip(axes[:,0],comparisons):
        dates=row['common_dates'];values=row['relative_index'];x=list(range(len(dates)))
        axis.plot(x,values,color='#386f90',linewidth=1.5)
        axis.axhline(100,color='#9aa6b2',linestyle='--',linewidth=.8)
        ticks=list(range(0,len(dates),max(1,len(dates)//6)))
        if ticks[-1]!=len(dates)-1: ticks.append(len(dates)-1)
        axis.set_xticks(ticks,[str(dates[i])[:10] for i in ticks],fontsize=8)
        name=row.get('label') or row.get('id') or row.get('reference','参考')
        qualifier='自建行业组合／非官方申万' if _proxy(row) else '同窗可比口径'
        axis.set_title(f'相对{name}｜{qualifier}',loc='left',fontsize=11,pad=9)
        axis.set_ylabel('相对表现\n起点100',fontsize=9);axis.grid(alpha=.15)
        axis.spines[['top','right']].set_visible(False)
    fig.tight_layout(h_pad=2);path=out/'relative-context.png';fig.savefig(path,dpi=150);plt.close(fig)
    return path.name


def _execution_rows(execution):
    if execution.get('status')=='not_requested':
        return []
    prices=execution.get('prices',{});personal=execution.get('personal_inputs',{})
    rows=[['当前状态',{'personal_conditional':'条件核对通过；仍由用户决定','non_personal':'资料不足，仅列非个性化条件','blocked':'受限，不能当作已可执行'}.get(execution.get('status'),'输入待核')],
          ['用户指定数量',execution.get('requested_quantity') if execution.get('requested_quantity') is not None else '未提供，不默认'],
          ['分析触发／失效',str(prices.get('analysis_trigger','缺失'))+' / '+str(prices.get('analysis_invalidation','缺失'))],
          ['原价触发／失效',str(prices.get('raw_trigger','缺失'))+' / '+str(prices.get('raw_invalidation','缺失'))],
          ['取整触发／失效',str(prices.get('rounded_trigger','缺失'))+' / '+str(prices.get('rounded_invalidation','缺失'))],
          ['假设风险金额',execution.get('risk_amount_under_assumptions','未知')]]
    for key,label in [('risk_budget','用户风险预算'),('fee_estimate','费用假设'),('slippage_buffer','每单位滑点')]:
        if key in personal: rows.append([label,personal[key]])
    rows += [[CHECKS.get(key,key),'通过' if passed else '未通过／待核'] for key,passed in execution.get('checks',{}).items()]
    rows += [['限制',value] for value in execution.get('limitations',[])]
    return rows


def _html_table(headers,rows):
    return '<div class="table-wrap"><table><thead><tr>'+''.join('<th>'+_text(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+_text(x)+'</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def render_brief(document, assessments, out):
    """Write the reader report in an already safety-checked staging directory."""
    out=Path(out)
    for name in ('report.md','report.html','price-overview.png','relative-context.png'):
        if (out/name).exists(): raise FileExistsError(str(out/name))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    with plt.rc_context({'font.sans-serif':['Microsoft YaHei','DejaVu Sans'],'axes.unicode_minus':False}):
        model=_model(document,assessments)
        price=_price_chart(document,out)
        relative=_relative_chart(model['comparisons'],out)
    security=document.get('security',{});company=security.get('company',security.get('security_id','个股'))
    market=_when(document.get('market_cutoff','未知'));information=_when(document.get('information_cutoff','未知'))
    close=_number(model['close']);change=(_number(model['change']*100)+'%' if model['change'] is not None else '涨跌缺失')
    facts=[('方向',model['reason']),('涨跌力度',model['momentum']),('市场／行业',' '.join(model['relative']) or '缺少可比基准，只判断个股价格。')]
    execution_rows=_execution_rows(document.get('execution',{'status':'not_requested'}))
    currency='元' if security.get('currency')=='CNY' else security.get('currency','')
    shown=document.get('timeframes',{}).get('1d',{}).get('bars',[])[-126:]
    candle_note=('K线红色表示收盘≥开盘，绿色表示收盘<开盘；影线上下端是最高/最低价。'
        if shown and all(all(row.get(key) is not None for key in ('open','high','low','close')) for row in shown)
        else '缺完整OHLC，图中仅呈现收盘价线。')
    price_caption=candle_note+'两条均线是最近20/60个交易日的收盘均价。'+model['volume']
    relative_caption='图示全窗各栏独立归一为100，向上表示相对改善；近20个交易日见上方文字。长窗领先与近期走弱可以同时存在；不同口径不比较点位。'+model['breadth']
    md=[f'# {_text(company)}｜技术研究简报','',f'**日线判断：{model["title"]}。**',
        f'收盘 {close} {currency}；{change}。行情截止：{market}；信息截止：{information}。','']
    md += [f'- **{label}**：{text}' for label,text in facts]
    if price: md += ['',f'![日线价格、20日与60日均线及量比]({price})',price_caption]
    if relative: md += ['',f'![市场与行业各自同窗相对表现]({relative})',relative_caption]
    md += ['','## 接下来只看两件事','',*['- '+text for text in model['conditions']],
           '','## 长短周期是否一致','', '|周期|状态|资料末端|用途|','|---|---|---|---|',
           *['|'+'|'.join(_text(value) for value in row)+'|' for row in model['frames']],
           '','**与前次相比**：'+model['previous'],'**原关注项**：'+model['review_note']]
    if execution_rows:
        md += ['','## 本次执行条件卡','', '|核对项|实际结果|','|---|---|',*['|'+'|'.join(_text(value) for value in row)+'|' for row in execution_rows],
               '条件不等于订单或成交；止损价不保证成交，也不是损失上限。']
    md += ['','**数据边界**：'+' '.join(model['warnings'])+' 分钟研究已暂停。',
           '技术状态不替代基本面价值；不同周期和同源指标不计票，不保证收益。',
           '','[专题、方法与完整图表](details.html) · [计算与来源记录](analysis.json)',
           '实际生成：'+_when(document.get('generated_at','未知'))+'；资料可知截止：'+_when(document.get('asof','未知'))+'。','']
    with (out/'report.md').open('x',encoding='utf-8') as handle: handle.write('\n'.join(md))
    def figure(name,caption):
        if not name: return ''
        encoded=base64.b64encode((out/name).read_bytes()).decode('ascii')
        return '<figure><div class="chart-scroll"><img alt="'+_text(caption)+'" src="data:image/png;base64,'+encoded+'"></div><figcaption>'+_text(caption)+'</figcaption></figure>'
    css='''body{margin:0;background:#eef2f6;color:#21354b;font:15px/1.6 "Microsoft YaHei",Arial,sans-serif}main{max-width:1040px;margin:22px auto;background:white;padding:28px 36px;border-top:6px solid #173b60}h1{font-size:25px;margin:0}h2{font-size:18px;margin:22px 0 10px}p{margin:7px 0}.meta,.foot{font-size:12px;color:#607083}.hero{display:flex;justify-content:space-between;gap:18px;align-items:center}.verdict{font-size:27px;font-weight:700;color:#a84638;white-space:nowrap}.strong .verdict{color:#246d58}.mixed .verdict,.unknown .verdict{color:#866724}.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}.fact{background:#f3f6f9;padding:12px;border-left:3px solid #7993aa}.fact b{display:block;margin-bottom:5px}figure{margin:14px 0}.chart-scroll{overflow-x:auto}img{display:block;width:100%;height:auto}figcaption{font-size:12px;color:#617184;margin:4px 0 10px}.conditions{background:#f5f8fb;border:1px solid #d8e2ea;padding:10px 18px}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;background:#edf3f7}td,th{padding:7px 9px;border-bottom:1px solid #e0e7ed;vertical-align:top}details{margin-top:16px;font-size:13px}summary{cursor:pointer;color:#315a7c}a{color:#275e85}.foot{border-top:1px solid #dbe3eb;padding-top:12px;margin-top:18px}.note{font-size:13px;color:#596d80}@media(max-width:650px){main{margin:0;padding:18px}.hero{display:block}.facts{grid-template-columns:1fr}.verdict{font-size:25px}h1{font-size:22px}.chart-scroll img{min-width:640px}}@media print{@page{size:A4;margin:12mm}body{background:white;font-size:10pt}main{margin:0;padding:0;max-width:none;border-top-width:4px}.meta,.foot,figcaption{font-size:8pt}.facts{margin:10px 0}.fact{padding:7px}h1{font-size:19pt}h2{font-size:13pt}figure,.conditions,table{break-inside:avoid}img{max-height:95mm;object-fit:contain}details{font-size:9pt}summary{display:none}details>*{display:block}.foot a{color:inherit}}'''
    page='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'"><title>'+_text(company)+'｜技术研究简报</title><style>'+css+'</style></head><body><main class="'+_text(model['state'])+'"><header class="hero"><div><h1>'+_text(company)+'</h1><p class="meta">'+_text(security.get('code',security.get('security_id','')))+' · 收盘 '+close+' '+_text(currency)+' · '+_text(change)+'</p><p class="meta">行情截止 '+_text(market)+'<br>信息截止 '+_text(information)+'</p></div><div class="verdict">日线'+_text(model['title'])+'</div></header><p class="note">'+_text(' '.join(model['warnings'][:2]))+'</p><div class="facts">'+''.join('<div class="fact"><b>'+_text(label)+'</b>'+_text(text)+'</div>' for label,text in facts)+'</div>'
    page+=figure(price,price_caption)
    page+=figure(relative,relative_caption)
    page+='<section class="conditions"><h2>接下来只看两件事</h2>'+''.join('<p>'+_text(text)+'</p>' for text in model['conditions'])+'</section><section><h2>长短周期是否一致</h2>'+_html_table(['周期','状态','资料末端','用途'],model['frames'])+'</section><section><h2>与前次相比</h2><p>'+_text(model['previous'])+'</p><p class="note">原关注项：'+_text(model['review_note'])+'</p></section>'
    if execution_rows:
        page+='<section><h2>本次执行条件卡</h2>'+_html_table(['核对项','实际结果'],execution_rows)+'<p class="note">条件不等于订单或成交；止损价不保证成交，也不是损失上限。</p></section>'
    page+='<details><summary>数据边界与阅读提醒</summary><p>'+_text(' '.join(model['warnings']))+'</p><p>分钟研究已暂停。技术状态不替代基本面价值；不同周期和同源指标不计票，不保证收益。</p></details><footer class="foot"><p><a href="details.html">专题、方法与完整图表</a> · <a href="analysis.json">计算与来源记录</a></p><p>实际生成 '+_text(_when(document.get('generated_at','未知')))+'；资料可知截止 '+_text(_when(document.get('asof','未知')))+'</p></footer></main></body></html>'
    with (out/'report.html').open('x',encoding='utf-8') as handle: handle.write(page)
    return {'state':model['state'],'files':['report.md','report.html']+[name for name in (price,relative) if name],
            'primary_images':sum(name is not None for name in (price,relative)),'periods':list(PERIODS)}
