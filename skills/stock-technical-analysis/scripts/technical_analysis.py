"""Local technical research, comparison and explicit-pool screening. No data fetching or orders."""

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import importlib.util


def sibling(name):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LABELS={'conventional':'常规指标与量价','candlestick':'蜡烛形态','ichimoku':'一目均衡',
        'harmonic':'谐波','smc':'SMC价格结构','turtle':'海龟多头条件'}
PERIODS={'1d':'日线','1w':'周线','1mo':'月线','6mo':'半年线','1y':'年线','60m':'60分钟','15m':'15分钟','5m':'5分钟'}
PERSONAL_FIELDS={'available_at':'个人资料可得时点','investment_horizon':'研究期限','position_fraction':'现有仓位比例',
    'risk_budget':'用户风险预算','requested_quantity':'用户指定数量','holdings_quantity':'用户声明持有数量',
    'holding_cost':'用户声明持仓成本','sellable_quantity':'用户声明可卖数量','available_cash':'用户声明可用资金',
    'fee_estimate':'整笔费用假设','slippage_buffer':'每单位滑点假设'}
STATES={'ok':'已运行','partial':'部分可用','unavailable':'资料不可用','diagnostic':'仅诊断',
        'validated':'已声明核验','missing_fields':'缺必要字段','dependency_missing':'缺运行依赖',
        'insufficient_history':'历史不足','no_candidates':'本规则无候选','not_run':'未运行',
        'not_requested':'本次不需要','not_comparable':'不可比','invalid_input':'输入错误','paused':'已暂停'}


def read_json(path):
    def reject(value):
        raise ValueError('JSON含非有限常量:'+value)
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),parse_constant=reject)


def write_json(path,value):
    with Path(path).open('x',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2,allow_nan=False)
        handle.write('\n')


def cell(value,limit=180):
    if value is None:
        return '未知／缺失'
    text=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,allow_nan=False)
    text=text.replace('|','／').replace('\n','；')
    return text if len(text)<=limit else text[:limit]+'…（完整见JSON）'


def last(series,key):
    return series.get(key,[None])[-1] if series.get(key) else None


def load_reference(reference,request_path,asof,market_cutoff):
    if reference is None:
        return None
    if not isinstance(reference,dict):
        raise ValueError('基准须为对象')
    result=dict(reference)
    if 'input_file' in result:
        path=Path(result['input_file'])
        if not path.is_absolute():
            path=request_path.parent/path
        result['bars']=read_json(path)
        if result.get('source_sha256') is not None:
            result['declared_source_sha256']=result['source_sha256']
        result['source_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    data=sibling('technical_data')
    if not isinstance(result.get('bars'),list):
        raise ValueError('基准必须有规范close记录数组')
    rows=[]
    for row in result['bars']:
        if not isinstance(row,dict):
            raise ValueError('基准记录须为对象')
        if data.moment(row['time'])<=data.moment(market_cutoff):
            item=dict(row)
            item.setdefault('available_at',result.get('snapshot_available_at',row['time']))
            rows.append(item)
    result['bars']=rows
    result.setdefault('benchmark_id',result.get('id'))
    result.setdefault('id',result.get('benchmark_id'))
    if result.get('id')!=result.get('benchmark_id'):
        raise ValueError('基准id与benchmark_id不一致')
    result.setdefault('available_at',result.get('snapshot_available_at'))
    return result


def qualified_fundamentals(refs,security,asof,information_cutoff):
    data=sibling('technical_data')
    results=[]
    for raw in refs or []:
        if not isinstance(raw,dict):
            raise ValueError('基本面引用须为对象')
        ref=dict(raw)
        reasons=[]
        if ref.get('security_id')!=security['security_id']:
            reasons.append('证券身份不一致')
        for field in ('currency','share_class'):
            if security.get(field) and ref.get(field)!=security[field]:
                reasons.append(field+'未知或不一致')
        for key in ('generated_at','research_asof','data_asof'):
            value=ref.get(key)
            if value is None:
                reasons.append(key+'未知')
            elif data.moment(value)>data.moment(asof if key=='generated_at' else information_cutoff):
                reasons.append(key+'晚于对应截止')
        if not ref.get('result_id'):
            reasons.append('缺原成果ID')
        if ref.get('valid_until') and data.moment(ref['valid_until'])<data.moment(asof):
            reasons.append('引用有效期已过')
        if ref.get('current_use') not in ('eligible','direct_reference'):
            reasons.append('原成果未获当前引用资格')
        allowed=[];excluded=[]
        for fact in ref.get('facts',[]):
            public=fact.get('public_at')
            if not public:
                excluded.append({'evidence_id':fact.get('evidence_id'),'reason':'原证据公开时间未知'})
            elif data.moment(public)>data.moment(information_cutoff):
                excluded.append({'evidence_id':fact.get('evidence_id'),'reason':'原证据晚于本次信息截止'})
            else:
                allowed.append(fact)
        if ref.get('facts'):
            ref['facts']=allowed
            ref['claims']=[f.get('fact',f.get('metric')) for f in allowed]
            ref['excluded_evidence']=excluded
            if not allowed:
                reasons.append('没有信息截止前可引用的原证据')
        if any('晚于' in reason or '不一致' in reason for reason in reasons):
            ref['facts']=[];ref['claims']=[]
        ref['reference_status']='eligible' if not reasons else 'historical_only'
        ref['qualification_reasons']=reasons
        results.append(ref)
    return results


def context_fundamentals(context):
    references=[]
    resolved=context.get('result',{})
    roles=resolved.get('roles',{})
    if not isinstance(roles,dict) or roles.get('schema_version')!='stock-research-roles/1' or roles.get('resolver_version')!='stock-research-role-resolver/1':
        return references
    formal={r['result_id'] for r in roles.get('classified',[]) if r.get('content_role') in
            {'full_research','incremental_research','correction'}}
    for result in resolved.get('results',[]):
        if result.get('result_id') not in formal: continue
        if result.get('module')!='fundamental': continue
        states=[x for x in result.get('item_states',[]) if x.get('current_eligible')]
        evidence={e['evidence_id']:e for e in result.get('evidence',[])}
        facts=[]
        for state in states:
            for identity in state.get('evidence_ids',[]):
                source=evidence.get(identity,{})
                public=source.get('public_at')
                public_bound=(public+'T23:59:59.999999+08:00') if public and len(public)==10 else public
                facts.append(dict(fact_id=state['item_id']+':'+identity,source_result_id=result['result_id'],
                    evidence_id=identity,fact=state['text'],value=state['text'],topic='fundamental',relation='neutral',
                    source=source.get('source'),public_at=public_bound,public_at_original=public,
                    public_time_precision='date_upper_bound' if public and len(public)==10 else 'timestamp_or_unknown',
                    first_known_at=result['generated_at'],data_cutoff=state.get('data_asof',result.get('data_asof')),
                    source_group=result['result_id']+':'+identity))
        references.append(dict(result_id=result['result_id'],security_id=result['security']['security_id'],
            currency=result['security'].get('currency'),share_class=result['security'].get('share_class'),
            generated_at=result['generated_at'],research_asof=result.get('research_asof'),data_asof=result.get('data_asof'),
            valid_until=min((s['valid_until'] for s in states if s.get('valid_until')),default=None),
            current_use='eligible' if states else 'historical',claims=[s['text'] for s in states],facts=facts))
    return references


def lab_call(config,command,*,request=None,security=None,asof=None):
    if not isinstance(config,dict) or not config.get('tool') or not config.get('root'):
        return {'status':'unavailable','reason':'未配置研究室工具与私有根'}
    tool=Path(config['tool'])
    if not tool.is_absolute() or tool.name!='research_lab.py' or not tool.is_file() or tool.is_symlink():
        return {'status':'unavailable','reason':'研究室工具路径未核实'}
    args=[sys.executable,'-X','utf8','-B',str(tool),command,str(config['root'])]
    if request:
        args+=['--request-json',str(request)]
    if security:
        args+=['--security',security,'--asof',asof]
    done=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=120)
    if done.returncode:
        return {'status':'failed','reason':done.stderr.strip() or done.stdout.strip()}
    try:
        return {'status':'ok','result':json.loads(done.stdout)}
    except ValueError:
        return {'status':'failed','reason':'研究室未返回合法JSON'}


def calculate(request,request_path):
    data=sibling('technical_data')
    core=sibling('technical_core')
    outputs=sibling('research_outputs')
    if not isinstance(request,dict) or not isinstance(request.get('security'),dict):
        raise ValueError('请求须包含security对象')
    for key in ('security_id','market','exchange','code','currency'):
        if not isinstance(request['security'].get(key),str) or not request['security'][key].strip():
            raise ValueError('证券缺少'+key)
    asof=request['asof']
    data.moment(asof)
    cutoff=request.get('market_cutoff',asof)
    information=request.get('information_cutoff',asof)
    if data.moment(information)>data.moment(asof):
        raise ValueError('信息截止不能晚于本次可知截止')
    old=lab_call(request.get('research_lab'),'context',security=request['security']['security_id'],asof=asof)
    inputs=data.prepare_inputs(request,request_path)
    market=load_reference(request.get('market'),request_path,asof,cutoff)
    industry=load_reference(request.get('industry'),request_path,asof,cutoff)
    for reference in (market,industry):
        if reference is not None and reference.get('currency')!=request['security']['currency']:
            reference['comparable']=False
            reference['identity_limitation']='基准币种未知或不一致，未提供合格换汇／对冲口径'
    analyzed={}
    for frame,value in inputs.items():
        if value['status']!='ok':
            analyzed[frame]={'status':value['status'],'metadata':value['metadata'],'bars':[],
                'topics':{name:{'status':'not_run','limitations':value['metadata']['limitations']} for name in LABELS},
                'series':{},'facts':[],'limitations':value['metadata']['limitations']}
            continue
        meta=value['metadata']
        context={'security_id':request['security']['security_id'],'timeframe':frame,
                 'adjustment':meta.get('adjustment',{}).get('mode'),
                 'source_group':request['security']['security_id']+':'+meta.get('source_family',meta.get('source_sha256',meta.get('input_sha256','unknown')))}
        method=core.analyze_close_prices if meta.get('price_only') else core.analyze_technical
        result=method(value['bars'],profile=request.get('profile'),context=context,asof=asof,
                      **({'benchmark':market if frame=='1d' and (market or {}).get('technical_comparable',True) else None} if not meta.get('price_only') else {}))
        result['metadata'].update(meta)
        analyzed[frame]=result
    daily=analyzed.get('1d',{})
    background_rows=daily.get('bars',[])
    background_meta=daily.get('metadata',{})
    if request.get('background_input'):
        try:
            background_rows,background_meta=data.load_dataset(request,request_path,request['background_input'],cutoff)
        except (ValueError,OSError,KeyError,TypeError) as error:
            background_rows=[];background_meta={'qualification':'diagnostic','limitations':['背景来源错误:'+str(error)]}
    background=outputs.market_background(background_rows,market,industry,asof=asof,
        metadata={'security_id':request['security']['security_id'],'information_cutoff':information,
                  'price_basis':background_meta.get('adjustment'),
                  'qualification':background_meta.get('qualification'),
                  'snapshot_available_at':background_meta.get('snapshot_available_at'),
                  'comparison_basis':request.get('background_comparison_basis'),
                  'source_group':request['security']['security_id']+':'+background_meta.get('source_family',background_meta.get('source_sha256','unknown'))})
    background['limitations'].extend(background_meta.get('limitations',[]))
    for comparison in background.get('comparisons',[]):
        reference=market if comparison['reference']=='market' else industry
        if reference and reference.get('label'):
            comparison['label']=reference['label']
    industry_context=attach_industry_context(background,request,request_path)
    supplied=request.get('fundamental_refs')
    references=qualified_fundamentals(context_fundamentals(old) if supplied is None else supplied,
                                    request['security'],asof,information)
    fundamental_facts=[fact for ref in references if ref['reference_status']=='eligible' for fact in ref.get('facts',[])]
    evidence=outputs.evidence_table(analyzed,background,fundamental_facts,asof=asof)
    scenario=request.get('scenario',{'action':'observe'})
    data_qualification=daily.get('metadata',{}).get('qualification','diagnostic')
    execution=outputs.execution_conditions(scenario,request['security'],request.get('rules'),request.get('user_context'),
        asof=asof,qualification='eligible' if data_qualification=='validated' else data_qualification)
    execution['data_qualification']=data_qualification
    if execution.get('status')!='not_requested':
        execution['analysis_price_basis']=scenario.get('price_basis','未知') if isinstance(scenario,dict) else '未知'
        user=request.get('user_context')
        if (execution.get('status') in ('blocked','personal_conditional') and isinstance(user,dict)
                and user.get('available_at') and data.moment(user['available_at'])<=data.moment(asof)):
            execution['personal_inputs']={key:user[key] for key in PERSONAL_FIELDS if key in user}
    reviews=outputs.review_summary(request.get('review_records',[]),asof=asof)
    result={'schema_version':'stock-technical-report/2',
            'status':'analyzed_with_qualifications' if any(v.get('bars') for v in analyzed.values()) else 'paused' if analyzed and all(v['status']=='paused' for v in analyzed.values()) else 'no_usable_data',
            'generated_at':datetime.now(timezone.utc).isoformat(),
            'security':request['security'],'asof':asof,'market_cutoff':cutoff,'information_cutoff':information,
            'timeframes':analyzed,'background':background,'evidence':evidence,'execution':execution,
            'review':reviews,'fundamental_references':references,'previous_context':old,'industry_context':industry_context,
            'limitations':['图形和指标为有限规则下的研究事实，不代表可成交、反转或收益保证。',
                          '没有真实用户交易反馈，不推断采纳、成交或个人盈亏。']}
    result['previous_technical']=previous_technical(request.get('previous_technical'),result,request_path)
    return result


def attach_industry_context(background,request,request_path):
    """Compare a local industry proxy on its own return basis, separate from the market."""
    if not request.get('industry_context_file'):
        return {'status':'not_supplied'}
    try:
        if request.get('industry'):
            raise ValueError('官方行业输入与本地行业组合须择一，不能混用')
        path=Path(request['industry_context_file'])
        if not path.is_absolute(): path=request_path.parent/path
        bundle=read_json(path);meta=bundle['metadata'];reference=dict(bundle['reference'])
        data=sibling('technical_data')
        if meta.get('security_id')!=request['security']['security_id']:
            raise ValueError('行业组合的目标证券身份不一致')
        cutoff=request.get('market_cutoff',request['asof'])
        if data.moment(meta['snapshot_available_at'])>data.moment(request['asof']):
            raise ValueError('行业快照晚于本次可知截止')
        age=data.moment(cutoff)-data.moment(meta['market_cutoff'])
        if age.total_seconds()<0 or age.days>4:
            raise ValueError('行业资料截止晚于本次或已经过旧')
        if reference.get('currency')!=request['security']['currency']:
            raise ValueError('行业组合币种不一致')
        stock_rows=[row for row in bundle['stock_bars'] if data.moment(row['time'])<=data.moment(cutoff)]
        reference['bars']=[row for row in reference['bars'] if data.moment(row['time'])<=data.moment(cutoff)]
        for rows in (stock_rows,reference['bars']):
            if not rows or (data.moment(cutoff)-data.moment(rows[-1]['time'])).days>4:
                raise ValueError('行业或目标比较序列缺少本次可用的近期观察')
        reference.update(proxy=True,classification_known=False)
        layer=sibling('research_outputs').market_background(stock_rows,industry=reference,asof=request['asof'],
            metadata={'security_id':request['security']['security_id'],'comparison_basis':reference['comparison_basis'],
                      'qualification':'diagnostic','snapshot_available_at':meta['snapshot_available_at'],
                      'source_group':meta.get('source_group','local-industry-target-return-chain')})
        comparison=next(row for row in layer['comparisons'] if row['reference']=='industry')
        breadth=bundle.get('breadth',[])
        if not isinstance(breadth,list):
            raise ValueError('行业广度须为按日观察列表')
        end=str(comparison.get('common_dates',[])[-1])[:10] if comparison.get('common_dates') else None
        eligible=[row for row in breadth if isinstance(row,dict) and row.get('date')==end]
        latest_breadth=eligible[-1] if eligible else {}
        level=str(bundle.get('selected_level',2))
        name=bundle.get('classification',{}).get(level,reference['id'])
        label='申万'+{'1':'一级','2':'二级','3':'三级'}.get(level,level)+'·'+name+'（自建等权）'
        comparison.update(weak_proxy=True,label=reference.get('label',label),
                          comparison_basis=reference['comparison_basis'],coverage=bundle.get('coverage',{}),
                          latest_breadth=latest_breadth)
        background['comparisons']=[row for row in background['comparisons'] if row['reference']!='industry']+[comparison]
        background['facts'].extend(layer.get('facts',[]))
        background['limitations']=[item for item in background['limitations'] if not item.startswith('industry:')]
        background['limitations'].extend('industry: '+item for item in comparison['limitations'])
        background['status']='partial'
        return {'status':comparison['status'],'input_file':str(path),'input_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'metadata':meta,'latest_breadth':latest_breadth,
                'coverage':bundle.get('coverage',{}),'limits':bundle.get('limitations',meta.get('limitations',[]))}
    except (ValueError,KeyError,TypeError,OSError,StopIteration) as error:
        background['limitations'].append('本地行业组合未纳入：'+str(error))
        return {'status':'unavailable','reason':str(error)}


def frame_assessment(value):
    bars=value.get('bars',[])
    latest=value.get('topics',{}).get('conventional',{}).get('latest',{})
    close=bars[-1]['close'] if bars else None
    short,medium=latest.get('sma20'),latest.get('sma60')
    state='unknown'
    if all(x is not None for x in (close,short,medium)):
        state='strong' if close>short>medium else 'weak' if close<short<medium else 'mixed'
    trend={'strong':'偏强：价格高于20根均线，20根均线高于60根均线',
           'weak':'偏弱：价格低于20根均线，20根均线低于60根均线',
           'mixed':'趋势分歧：价格与20／60根均线未形成一致排列',
           'unknown':'尚不可判：价格或20／60根均线不足'}[state]
    if state=='mixed':
        relation=lambda left,right:'高于' if left>right else '低于' if left<right else '等于'
        trend=f'趋势分歧：价格{relation(close,short)}20根均线，20根均线{relation(short,medium)}60根均线'
    hist=latest.get('macd_hist')
    momentum='MACD暖启动或依赖不足，不能判断动量。'
    if hist is not None:
        momentum=f'MACD柱{number(hist)}，'+('柱为正，DIF高于信号线。' if hist>0 else '柱为负，DIF低于信号线。' if hist<0 else 'DIF与信号线重合。')
        previous=value.get('series',{}).get('macd_hist',[])
        if len(previous)>1 and previous[-2] is not None:
            change=hist-previous[-2]
            momentum+='柱较前一根'+('上升' if change>0 else '下降' if change<0 else '不变')+'；柱值变化只描述快慢线差距。'
    dif,signal=latest.get('macd'),latest.get('macd_signal')
    if dif is not None and signal is not None:
        momentum+=f' DIF为{number(dif)}，信号线为{number(signal)}，'+('两线均低于零轴。' if dif<0 and signal<0 else '两线均高于零轴。' if dif>0 and signal>0 else '两线分别处于零轴两侧，或其中一线为零。')
        prior_dif=value.get('series',{}).get('macd',[])
        if len(prior_dif)>1 and prior_dif[-2] is not None:
            momentum+='DIF较前根'+('上升' if dif>prior_dif[-2] else '下降' if dif<prior_dif[-2] else '不变')+'，须与柱值分别观察。'
    rsi=latest.get('rsi14')
    if rsi is not None:
        momentum+=f' RSI14为{number(rsi)}，'+('处于低位区，超卖不能单独确认见底。' if rsi<30 else '处于高位区，超买不能单独确认见顶。' if rsi>70 else '处于常用30—70观察区间，不是独立买卖条件。')
        prior_rsi=value.get('series',{}).get('rsi14',[])
        if len(prior_rsi)>1 and prior_rsi[-2] is not None:
            momentum+='RSI较前根'+('上升。' if rsi>prior_rsi[-2] else '下降。' if rsi<prior_rsi[-2] else '不变。')
    ratio=latest.get('volume_ratio20')
    volume='缺少合格量能，不能验证放量、缩量或资金流向。' if ratio is None else f'本根量为此前20根平均量的{number(ratio)}倍；这只是相对成交量，不识别机构建仓或成交方向。'
    return dict(state=state,trend=trend,close=close,latest=latest,momentum=momentum,volume=volume)


def previous_technical(config,document,request_path):
    if config is None:
        return {'status':'not_supplied','reason':'未提供上一份技术计算快照，不能自动比较数值。','timeframes':{}}
    result={'status':'historical_only','timeframes':{},'result_id':config.get('result_id') if isinstance(config,dict) else None}
    try:
        if not isinstance(config,dict) or not config.get('input_file') or not config.get('result_id'):
            raise ValueError('上一份技术快照须有input_file及原result_id')
        path=Path(config['input_file'])
        if not path.is_absolute(): path=request_path.parent/path
        old=read_json(path);data=sibling('technical_data')
        if not isinstance(old,dict) or not isinstance(old.get('security'),dict) or not isinstance(old.get('timeframes'),dict):
            raise ValueError('旧技术快照须为含security及timeframes的计算对象')
        for key in ('security_id','share_class','currency'):
            if not document['security'].get(key) or old.get('security',{}).get(key)!=document['security'][key]:
                raise ValueError('旧技术'+key+'未知或不一致')
        for field,current in (('generated_at','asof'),('asof','asof'),('market_cutoff','market_cutoff'),('information_cutoff','information_cutoff')):
            if not old.get(field) or data.moment(old[field])>data.moment(document[current]):
                raise ValueError('旧技术'+field+'未知或晚于本次对应截止')
        result.update(generated_at=old['generated_at'],market_cutoff=old['market_cutoff'],
                      information_cutoff=old['information_cutoff'],source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        declared=config.get('price_basis_compatible') is True and isinstance(config.get('comparison_evidence'),str) and config['comparison_evidence'].strip()
        for frame,current in document['timeframes'].items():
            prior=old.get('timeframes',{}).get(frame,{})
            if not isinstance(prior,dict) or not isinstance(prior.get('metadata',{}),dict) or not isinstance(prior.get('bars',[]),list):
                raise ValueError('旧技术周期结构不合格')
            reasons=[];a=prior.get('metadata',{});b=current.get('metadata',{})
            before,after=prior.get('bars',[]),current.get('bars',[])
            if not declared: reasons.append('未声明并举证旧新价格口径兼容')
            if not before or not after: reasons.append('旧新至少一方没有可用行情')
            else:
                if data.moment(before[0]['time'])!=data.moment(after[0]['time']): reasons.append('历史窗口起点不同')
                if data.moment(before[-1]['time'])>data.moment(after[-1]['time']): reasons.append('旧行情末端晚于本次')
            if a.get('adjustment',{}).get('mode')!=b.get('adjustment',{}).get('mode'): reasons.append('复权模式不同')
            if not a.get('source_family') or a.get('source_family')!=b.get('source_family'): reasons.append('原始来源谱系未知或不同')
            row={'status':'incomparable' if reasons else 'comparable','reasons':reasons,
                 'previous_bars':len(before),'current_bars':len(after)}
            if not reasons:
                prior_summary,current_summary=frame_assessment(prior),frame_assessment(current)
                row.update(previous_trend=prior_summary['trend'],current_trend=current_summary['trend'],
                           comparison_evidence=config['comparison_evidence'],changes={})
                for metric in ('close','sma20','sma60','rsi14','macd_hist'):
                    left=prior_summary['close'] if metric=='close' else prior_summary['latest'].get(metric)
                    right=current_summary['close'] if metric=='close' else current_summary['latest'].get(metric)
                    if left is not None and right is not None:
                        row['changes'][metric]={'previous':left,'current':right,'change':right-left}
            result['timeframes'][frame]=row
        result.update(status='reviewed',reason='仅作旧新事实回顾；窗口长度变化及动态均线变化不等于突破冻结阈值，不自动评价预测命中。')
    except (ValueError,KeyError,TypeError,OSError) as error:
        result['reason']=str(error)
    return result


def number(value):
    return '缺失' if value is None else f'{value:,.2f}'


def topic_readings(value):
    topics=value.get('topics',{});bars=value.get('bars',[])
    close=bars[-1]['close'] if bars else None
    lines=[]
    ichi=topics.get('ichimoku',{})
    cloud=ichi.get('latest',{});a,b=cloud.get('cloud_a_visible'),cloud.get('cloud_b_visible')
    if a is not None and b is not None and close is not None:
        position='上方' if close>max(a,b) else '下方' if close<min(a,b) else '内部'
        lines.append(f'**一目图**：价格处于当前可见云层{position}，云层下沿/上沿为{number(min(a,b))}/{number(max(a,b))}。这是以前计算后移到当前的云，不是用未来云确认今天。')
    else: lines.append('**一目图**：当前可见云所需历史或OHLC不足，不能判断价格相对云层的位置。')
    smc=topics.get('smc',{})
    regime={'bullish':'向上','bearish':'向下','undetermined':'尚未确定'}.get(smc.get('latest_regime'),'不可判')
    zones=[z for z in smc.get('zones',[]) if z.get('status') in ('active','partial')]
    lines.append(f'**SMC图**：最近已确认收盘结构方向为{regime}；有{len(zones)}个机内仍活动/部分回补的价格区。图中矩形是有限规则标记，不是机构订单，也不保证回补；未排除机械原因时仅诊断。')
    turtle=topics.get('turtle',{}).get('latest',{})
    if turtle.get('entry_high20') is not None:
        triggered=turtle.get('long_entry20')
        state_text=lambda state:'已满足' if state is True else '未满足' if state is False else '不可判'
        lines.append(f'**海龟图**：此前20/55根高点为{number(turtle.get("entry_high20"))}/{number(turtle.get("entry_high55"))}；20根收盘突破条件'+state_text(triggered)+f'。此前10/20根低点为{number(turtle.get("exit_low10"))}/{number(turtle.get("exit_low20"))}；收盘低于10根/20根低点的退出观察条件分别为'+state_text(turtle.get('long_exit10'))+'/'+state_text(turtle.get('long_exit20'))+'。条件成立也不推定已有持仓或成交，诊断周期不升级为执行建议。')
    else: lines.append('**海龟图**：历史或高低价不足，不计算有效通道，不能把空白当作没有交易信号。')
    candle=topics.get('candlestick',{})
    latest=candle.get('latest',[])
    labels='、'.join(x.get('pattern','未知')+'（'+{'bullish':'看多几何','bearish':'看空几何'}.get(x.get('direction'),'方向待定')+'）' for x in latest)
    lines.append('**蜡烛图**：'+('末根标签为'+labels+'。' if latest else '末根没有返回可识别标签。' if candle.get('status')=='ok' else '当前缺少字段、依赖或暖启动，不能判断末根形态。')+'图面只标最近少量历史标签；标签不是反转概率，还须结合趋势位置与随后完整K线验证。')
    return lines


def harmonic_reading(value):
    topic=value.get('topics',{}).get('harmonic',{})
    analysis=topic.get('analysis') or {}
    if not analysis:
        return '没有可用谐波计算，不能写成“未发现形态”。'
    patterns=analysis.get('patterns',[]);forming=analysis.get('forming',[]);previews=analysis.get('previews',[])
    text=f'本窗口返回{len(patterns)}个已确认几何、{len(forming)}个形成中、{len(previews)}个临时D预览。'
    if patterns:
        sample=patterns[-1]
        d=sample.get('points',{}).get('D',{})
        text+=f'列表末项为{sample.get("pattern","未知")}，D发生日{str(d.get("time","未知"))[:10]}、几何确认日{str(sample.get("confirmed_at","未知"))[:10]}，机内情景状态{sample.get("scenario_status","未知")}。'
        text+='全部候选须核D发生日、确认日、原窗口和失效界；机内active只表示尚未触设定失效规则，历史旧形态不会自动变成近期买卖信号。'
    else: text+='无候选只限本规则、窗口和价格口径，不代表其他方法必然无信号。'
    return text+' PRZ是几何潜在反转区，触区和几何确认均不保证反转。'


def background_windows(comparison):
    rows=[]
    dates=comparison.get('common_dates',[])
    stock,reference=comparison.get('stock_index',[]),comparison.get('reference_index',[])
    if len(dates)>1 and len(dates)==len(stock)==len(reference) and stock[0]>0 and reference[0]>0:
        stock_return,reference_return=stock[-1]/stock[0]-1,reference[-1]/reference[0]-1
        rows.append(dict(label=f'图示全窗{len(dates)}点',start=dates[0],end=dates[-1],
                         stock_return=stock_return,reference_return=reference_return,
                         excess_return=stock_return-reference_return,full_window=True))
    for window,entry in comparison.get('windows',{}).items():
        if entry.get('status')=='ok':
            rows.append({**entry,'label':f'近{window}个交易间隔（{int(window)+1}点）','full_window':False})
    return rows


def evidence_readings(document,assessments):
    rows=[]
    for frame,value in document['timeframes'].items():
        summary=assessments[frame];latest=summary['latest'];meta=value['metadata']
        scope=PERIODS.get(frame,frame)+'；'+STATES.get(meta.get('qualification'),'资格待核')
        if not value.get('bars'):
            rows.append((scope+'无可用行情','未运行，不能判断本周期方向','缺失不是中性或无信号'))
            continue
        measured=f"价格{number(summary['close'])}，SMA20/60={number(latest.get('sma20'))}/{number(latest.get('sma60'))}"
        rows.append((scope+'：'+measured,summary['trend'],
                     '同周期排列；与日线期限不同，不合并投票' if frame!='1d' else '波段方向主证据；价格口径或资格改变须重核'))
    daily=document['timeframes'].get('1d',{});summary=assessments.get('1d',{})
    latest=summary.get('latest',{});hist=latest.get('macd_hist')
    hist_series=daily.get('series',{}).get('macd_hist',[])
    if hist is not None:
        prior=hist_series[-2] if len(hist_series)>1 else None
        fact='日线MACD柱'+(number(prior)+'→' if prior is not None else '')+number(hist)
        meaning=('负柱收窄，支持下行动量减弱；仍是负柱' if hist<0 and prior is not None and hist>prior else
                 '负柱扩大，DIF相对信号线的负差扩大' if hist<0 and prior is not None and hist<prior else
                 '正柱缩短，短窗动量减弱；仍是正柱' if hist>0 and prior is not None and hist<prior else
                 '正柱扩大，DIF相对信号线的正差扩大' if hist>0 and prior is not None and hist>prior else
                 '柱为负，DIF仍低于信号线' if hist<0 else '柱为正，DIF高于信号线' if hist>0 else '快慢线差为零')
        rows.append((fact,meaning,f"DIF={number(latest.get('macd'))}，信号线={number(latest.get('macd_signal'))}；不能只凭柱变化确认趋势反转"))
    rsi=latest.get('rsi14')
    if rsi is not None:
        rows.append((f'日线RSI14={number(rsi)}','动量处于低位区' if rsi<30 else '动量处于高位区' if rsi>70 else '位于30—70观察区',
                     '与MACD/均线同源；不是独立反转票'))
    cloud=daily.get('topics',{}).get('ichimoku',{}).get('latest',{})
    a,b=cloud.get('cloud_a_visible'),cloud.get('cloud_b_visible');close=summary.get('close')
    if a is not None and b is not None and close is not None:
        position='上方' if close>max(a,b) else '下方' if close<min(a,b) else '内部'
        rows.append((f'日线价格{number(close)}，可见云{number(min(a,b))}—{number(max(a,b))}',
                     '价格在当前可见云'+position,'与均线同源的另一条件；不是未来云，也不增加独立证据'))
    smc=daily.get('topics',{}).get('smc',{})
    if smc.get('latest_regime'):
        rows.append(('日线最近收盘结构：'+{'bullish':'向上','bearish':'向下','undetermined':'未定'}.get(smc['latest_regime'],smc['latest_regime']),
                     '保留最近已确认结构方向','有限规则及机械原因核验范围内；不是机构资金结论'))
    turtle=daily.get('topics',{}).get('turtle',{}).get('latest',{})
    if turtle.get('entry_high20') is not None:
        rows.append((f"日线此前20根高点{number(turtle['entry_high20'])}",
                     '20根收盘突破条件已满足' if turtle.get('long_entry20') is True else '20根收盘突破条件未满足',
                     '排除当前根；条件与实际成交分别判断'))
    for comparison in document['background'].get('comparisons',[]):
        if comparison.get('status')!='ok': continue
        windows=background_windows(comparison)
        if windows:
            measured='；'.join(row['label']+'超额'+f"{row['excess_return']*100:.2f}个百分点" for row in windows)
            rows.append((str(comparison.get('id'))+'：'+measured,'分别描述长窗累计及近窗相对强弱','窗口不同可能反向；不能证明机构行为或经营周期'))
    return rows


def next_conditions(value):
    summary=frame_assessment(value);state=summary['state'];latest=summary['latest']
    series=value.get('series',{}).get('sma20',[])
    slope='SMA20前根值不足，暂不能核“停止下行”。'
    if len(series)>1 and series[-2] is not None and series[-1] is not None:
        slope=f'SMA20前根{number(series[-2])}→本根{number(series[-1])}，'+('仍在下行。' if series[-1]<series[-2] else '本根未继续下行。')
    if state=='weak':
        return ['**当前延续条件**：目前是价格<SMA20<SMA60。下一完整日线若两项不等式都继续成立，保留弱势排列；价格不再低于SMA20，或SMA20不再低于SMA60，都会使这条严格弱势排列不再成立，但不自动构成强势排列。',
                f"**修复观察**：当前价格{number(summary['close'])}、SMA20={number(latest.get('sma20'))}。"+slope+'延续原修复关注：价格重回动态SMA20之上，且当根SMA20不低于前一完整根，才同时满足“收回均线＋均线停止下行”的观察条件；这仍不是反转已确认。']
    if state=='strong':
        return ['**当前延续条件**：目前是价格>SMA20>SMA60。下一完整日线仍满足这两项不等式，才保留强势排列；价格不再高于SMA20，或SMA20不再高于SMA60，这条排列条件就不再成立，须区分短窗回落与中期转弱。',
                '**下一核验**：结合本次MACD柱、DIF及相对强弱是否继续同向；单根正柱或量能放大不替代排列与原失效规则。'+slope]
    if state=='mixed':
        return ['**当前分歧**：'+summary['trend']+'。'+slope,
                '**下一核验**：价格>SMA20且SMA20>SMA60时才形成强势排列；价格<SMA20且SMA20<SMA60时才形成弱势排列。尚未同时满足时继续保留分歧，单项关系改变只更新该项事实，不提前宣布反转。']
    return ['**资料补齐后再核条件**：当前日线价格或SMA20/60不足，先补足同一合格口径的连续日线和暖启动；不指定不存在的均线价，也不以分钟采样替代日线确认。']


def execution_limitation(message):
    translations={
        'Only eligible inputs can support a personal conditional card':'价格资料未取得个人条件卡所需数据资格；诊断或缺失不能升级。',
        'Verified contemporaneous adjusted-to-raw mapping is missing':'缺少本时点可核的复权价到原价映射。',
        'Validated date, market, exchange and board-specific rules are required':'缺少已核日期、市场、交易所及板块匹配的规则。',
        'Trading session, suspension or state freshness is unknown or restricted':'交易会话、停牌或状态有效期未知或受限。',
        'Settlement rule is unknown; next bar never proves T+1 eligibility':'结算规则未知；下一根K线不证明已经满足T+1。',
        'No personal context: observe conditions without personal quantity or position advice':'未提供个人情境，只给非个性化条件，不给数量或仓位建议。',
        'Personal facts are missing an asof-qualified availability statement':'个人资料缺少合格可得时点，或晚于本次截止，未用于个人卡。',
        'Fees or slippage assumptions are unknown; no bounded personal risk claim':'费用或滑点假设未知，不能宣称个人风险已获完整测算。',
        'Requested quantity exceeds supplied risk budget after rounding':'报价取整及成本计入后，用户指定数量超过风险预算。',
        'Requested purchase exceeds supplied cash':'用户指定买入金额超过声明的可用资金。',
        'Requested sale exceeds known settled sellable quantity':'用户指定卖出数量超过已知结算可卖量。',
        'Requested quantity violates the supplied minimum or quantity increment':'用户指定数量不符合已提供的最小数量或数量步长。',
        'Rounded condition lies outside current limits; do not clip the original definition':'取整价格超出当前限价；不能截到限价内暗改原条件。',
        'Applicable price limits are unknown':'适用限价制度或上下限未知。',
        'Scenario first-known timestamp is unknown':'情景首次可知时点未知。',
        'Scenario conditions or observation window are incomplete':'情景触发、确认、失效或观察期限不完整。',
        'Numeric trigger and invalidation are missing':'缺少数值触发价或失效价。',
        'Existing-position cost is unknown':'已有持仓成本未知。',
        'Same-day purchase quantity is unknown; next bar is not T+1':'当日新买数量未知，下一根K线不能替代T+1核验。',
        'Qualified security identity is missing':'缺少合格证券身份。',
        'Investment horizon must be explicit nonempty text':'需要明确的研究期限。'}
    if message.startswith('Personal facts missing: '):
        fields=message.split(': ',1)[1].split(', ')
        return '个人资料缺少：'+'、'.join(PERSONAL_FIELDS.get(field,field) for field in fields)+'。'
    return translations.get(message,'输入或规则校验提示：'+message)


def execution_card(execution,currency):
    status_names={'non_personal':'非个性化条件','blocked':'执行条件受限','personal_conditional':'有条件的个人研究卡','invalid_input':'输入不合格'}
    action_names={'buy':'买入','increase':'加仓','sell':'卖出','reduce':'减仓','stop_loss':'止损'}
    lines=['条件卡状态：'+status_names.get(execution.get('status'),'受限或待核')+'。',
           '动作：'+action_names.get(execution.get('action'),'未知')+f'；价格及金额币种：{currency}。数据原资格：'+
           str(execution.get('data_qualification','未知'))+'；条件卡数据门：'+str(execution.get('qualification','未知'))+'。','',
           '|条件|本次实际定义|','|---|---|']
    for key,label in (('trigger','触发'),('confirmation','确认'),('invalidation','失效'),('observation_window','期限')):
        lines.append(f"|{label}|{cell(execution.get('conditions',{}).get(key),600)}|")
    prices=execution.get('prices',{})
    lines+=['','### 价格如何映射','', '|价格层|触发价|失效价|','|---|---|---|',
        f"|分析价（{cell(execution.get('analysis_price_basis'))}）|{cell(prices.get('analysis_trigger'))}|{cell(prices.get('analysis_invalidation'))}|",
        f"|未取整原价|{cell(prices.get('raw_trigger'))}|{cell(prices.get('raw_invalidation'))}|",
        f"|按报价步长取整|{cell(prices.get('rounded_trigger'))}|{cell(prices.get('rounded_invalidation'))}|",'',
        f"报价步长：{cell(prices.get('tick_size'))}；触发价取整偏差：{cell(prices.get('trigger_rounding_difference'))}；取整后单位风险距离：{cell(prices.get('risk_distance'))}。",
        '映射依据：'+cell(prices.get('mapping_evidence'),600)+'。']
    inputs=execution.get('personal_inputs',{})
    lines+=['','### 数量、资金及风险核对','',
        '用户指定数量：'+cell(execution.get('requested_quantity'))+'；此数来自用户，不是模块推荐仓位。']
    if inputs:
        lines+=['','|个人声明或成本假设|本次值|','|---|---|']
        for key,label in PERSONAL_FIELDS.items():
            if key=='requested_quantity': continue
            lines.append('|'+label+'|'+cell(inputs.get(key))+'|')
    else:
        lines+=['本次没有使用合格个人资料，资金及风险预算未核；不补造数量、成本、仓位或费用。']
    lines+=['',f"假设下风险金额：{cell(execution.get('risk_amount_under_assumptions'))} {currency}。",
        '测算定义为（取整后单位风险距离＋每单位滑点假设）×用户指定数量＋整笔费用假设；滑点和费用是输入假设，止损跳空或无法成交可能导致更大损失。','',
        '### 实际通过与受限项','', '|核对项|结果|','|---|---|']
    check_names={'dated_security_rules':'规则身份、公开及生效时段','trigger_within_limits':'触发价在适用限价内',
        'invalidation_within_limits':'失效价在适用限价内','session_and_state':'交易会话及停牌状态',
        'quantity_rule':'最小数量及数量步长','sellable_quantity':'结算可卖数量','risk_budget':'风险预算',
        'cash_under_declared_costs':'按声明费用及滑点的资金预算'}
    for key,state in execution.get('checks',{}).items():
        lines.append('|'+check_names.get(key,key)+'|'+('通过' if state is True else '未通过' if state is False else '未知')+'|')
    if not execution.get('checks'): lines.append('|尚无可核项目|未核|')
    limitations=execution.get('limitations',[])
    lines+=['','**本次具体限制**：']
    if limitations:
        lines+=['','\n'.join('- '+cell(execution_limitation(str(reason)),700) for reason in limitations)]
    else:
        lines+=['本次提供的数据、规则和个人假设范围内未返回阻断项；这是有条件研究结果，不表示情景已经触发、用户已采纳或可以自动下单。']
    if execution.get('settlement_note'):
        lines+=['T+1说明：不假定当日新买入的数量能在下一根分钟K线卖出。']
    lines+=['生成订单：否；可提交订单：否。用户决定并反馈真实执行，条件成立不保证成交。']
    return lines


def chart_times(bars,security):
    data=sibling('technical_data')
    zone=data.TZ if security.get('market')=='A-share' else timezone.utc
    return [data.moment(row['time']).astimezone(zone).isoformat() for row in bars]


def render_frame(frame,value,security,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    bars=value['bars']
    start=max(0,len(bars)-180)
    shown=bars[start:]
    x=list(range(len(shown)))
    times=chart_times(shown,security)
    close=[r['close'] for r in shown]
    series=value['series']
    name=frame+'价格采样' if value['metadata'].get('price_only') else frame+'完整OHLC'
    title=security['security_id']+' · '+name+' · '+STATES.get(value['metadata'].get('qualification'),'资格待核')
    fig,axes=plt.subplots(4,1,figsize=(14,11),sharex=True,gridspec_kw={'height_ratios':[4,1.5,1.5,1.5]})
    plot=make_subplots(rows=4,cols=1,shared_xaxes=True,vertical_spacing=.04,row_heights=[.47,.18,.18,.17])
    axes[0].plot(x,close,color='#273e5f',label='价格')
    if not value['metadata'].get('price_only'):
        plot.add_trace(go.Candlestick(x=times,open=[b['open'] for b in shown],high=[b['high'] for b in shown],
            low=[b['low'] for b in shown],close=close,increasing_line_color='#d94b4b',decreasing_line_color='#238b57',name='OHLC'),row=1,col=1)
    else:
        plot.add_trace(go.Scatter(x=times,y=close,name='名义时点采样价，非OHLC'),row=1,col=1)
    for key,color in [('sma20','#d98720'),('sma60','#396db0'),('sma120','#8251a2')]:
        values=series.get(key,[None]*len(bars))[start:]
        axes[0].plot(x,[np.nan if v is None else v for v in values],color=color,label=key)
        plot.add_trace(go.Scatter(x=times,y=values,name=key,line=dict(color=color,width=1.3)),row=1,col=1)
    hist=series.get('macd_hist',[None]*len(bars))[start:]
    axes[1].bar(x,[np.nan if v is None else v for v in hist],color=['#d94b4b' if v is not None and v>=0 else '#238b57' for v in hist])
    if not any(v is not None for v in hist):
        axes[1].text(.02,.5,'历史不足，MACD未计算',transform=axes[1].transAxes)
    for key in ('macd','macd_signal'):
        values=series.get(key,[None]*len(bars))[start:]
        axes[1].plot(x,[np.nan if v is None else v for v in values],label=key)
        plot.add_trace(go.Scatter(x=times,y=values,name=key),row=2,col=1)
    plot.add_trace(go.Bar(x=times,y=hist,name='MACD histogram（未乘2）'),row=2,col=1)
    rsi=series.get('rsi14',[None]*len(bars))[start:]
    axes[2].plot(x,[np.nan if v is None else v for v in rsi],color='#8f4a8c')
    if not any(v is not None for v in rsi):
        axes[2].text(.02,.5,'历史不足，RSI未计算',transform=axes[2].transAxes)
    axes[2].axhline(30,color='#999',linestyle='--'); axes[2].axhline(70,color='#999',linestyle='--')
    plot.add_trace(go.Scatter(x=times,y=rsi,name='RSI14'),row=3,col=1)
    volume=[b.get('volume') for b in shown]
    axes[3].bar(x,[np.nan if v is None else v for v in volume],color='#748c9d')
    if not any(v is not None for v in volume):
        axes[3].text(.02,.6,'无合格成交量，不生成量价结论',transform=axes[3].transAxes)
    plot.add_trace(go.Bar(x=times,y=volume,name='成交量（单位见来源）'),row=4,col=1)
    for ax,label in zip(axes,['价格','MACD','RSI14','成交量／源单位']):
        ax.set_ylabel(label); ax.grid(alpha=.15)
    axes[0].legend(loc='best',fontsize=8);axes[1].legend(loc='best',fontsize=8)
    ticks=list(range(0,len(shown),max(1,len(shown)//8)))
    axes[3].set_xticks(ticks,[times[i][:16].replace('T',' ') for i in ticks],rotation=25,ha='right')
    axes[3].set_xlabel('横轴时区 UTC'+times[0][-6:])
    fig.suptitle(title+'\n'+cell(value['metadata'].get('limitations',[]),120),fontsize=12)
    fig.tight_layout(rect=(0,0,1,.94));fig.savefig(out/f'technical-{frame}.png',dpi=140);plt.close(fig)
    plot.update_layout(title=html.escape(title),height=1000,xaxis_rangeslider_visible=False,legend=dict(orientation='h'))
    plot.write_html(out/f'technical-{frame}.html',include_plotlyjs=True,auto_open=False)
    if value['metadata'].get('price_only'):
        return
    fig,axs=plt.subplots(2,2,figsize=(15,10))
    for ax in axs.flat:
        ax.plot(x,close,color='#34465b',linewidth=1);ax.grid(alpha=.15)
        ax.set_xticks(ticks,[times[i][:10] for i in ticks],rotation=25,ha='right')
    ichi=value['topics'].get('ichimoku',{}).get('series',{})
    for key in ('tenkan9','kijun26','cloud_a_visible','cloud_b_visible'):
        values=ichi.get(key,[None]*len(bars))[start:]
        axs[0,0].plot(x,[np.nan if v is None else v for v in values],label=key,linewidth=.8)
    axs[0,0].set_title('一目：当前可见云层；未来投影不当当前信号');axs[0,0].legend(fontsize=7)
    smc=value['topics'].get('smc',{})
    for zone in [z for z in smc.get('zones',[]) if z['status'] in ('active','partial','mitigated') and z['created_index']>=start][-4:]:
        axs[0,1].fill_between([zone['created_index']-start,len(shown)-1],zone['lower'],zone['upper'],alpha=.17,label=zone['kind']+' '+zone['direction'])
    axs[0,1].set_title('SMC有限价格结构；不代表真实机构订单')
    if axs[0,1].get_legend_handles_labels()[0]: axs[0,1].legend(fontsize=7)
    turtle=value['topics'].get('turtle',{}).get('series',{})
    for key,values in turtle.items():
        if key.startswith(('entry','exit','high','low')):
            axs[1,0].plot(x,[np.nan if v is None else v for v in values[start:]],label=key,linewidth=.7,linestyle='--')
    axs[1,0].set_title('海龟：此前完整K线通道，不代表实际成交')
    if axs[1,0].get_legend_handles_labels()[0]: axs[1,0].legend(fontsize=7)
    events=[e for e in value['topics'].get('candlestick',{}).get('events',[]) if e['index']>=start][-5:]
    grouped={}
    for event in events: grouped.setdefault(event['index'],[]).append(event['pattern'])
    for index,names in grouped.items():
        i=index-start
        axs[1,1].scatter([i],[close[i]],color='#b64678',s=18)
        right=i>len(shown)*.7
        label='\n'.join(names[:2])+(f'\n另{len(names)-2}项见JSON' if len(names)>2 else '')
        axs[1,1].annotate(label,(i,close[i]),xytext=(-5 if right else 3,7),textcoords='offset points',
                          fontsize=6,ha='right' if right else 'left',clip_on=True)
    axs[1,1].set_title('蜡烛标签：原函数值不是反转概率')
    fig.suptitle(title);fig.tight_layout(rect=(0,0,1,.96));fig.savefig(out/f'topics-{frame}.png',dpi=140);plt.close(fig)
    harmonic=value['topics'].get('harmonic',{}).get('analysis')
    if harmonic:
        meta=dict(value['metadata']);meta.setdefault('geometry_price_basis_qualified',False)
        sibling('harmonic_analysis').render_charts(bars,harmonic,meta,out)
        (out/'chart.png').rename(out/f'harmonic-{frame}.png')
        (out/'chart.html').rename(out/f'harmonic-{frame}.html')


def render_background(background,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','DejaVu Sans']
    comparisons=[value for value in background.get('comparisons',[]) if value.get('common_dates')]
    fig,axes=plt.subplots(2,max(1,len(comparisons)),figsize=(max(1,len(comparisons))*7,8),squeeze=False)
    for column,comparison in enumerate(comparisons):
        times=comparison['common_dates'];x=list(range(len(times)))
        name=comparison.get('label',comparison.get('id',comparison.get('reference','benchmark')))
        axes[0,column].plot(x,comparison['reference_index'],label='基准')
        axes[0,column].plot(x,comparison['stock_index'],label='同窗个股',linestyle='--')
        axes[0,column].set_title(name,fontsize=10)
        axes[1,column].plot(x,comparison['relative_index'],label='个股／本栏基准')
        ticks=list(range(0,len(times),max(1,len(times)//8)))
        for ax in axes[:,column]:
            ax.set_xticks(ticks,[times[i][:10] for i in ticks],rotation=20,ha='right')
    for column in range(axes.shape[1]):
        axes[1,column].axhline(100,color='#999',linestyle='--')
        axes[0,column].set_ylabel('本栏共同起点100');axes[1,column].set_ylabel('本栏相对指数，起点100')
    for ax in axes.flat:
        ax.grid(alpha=.15)
        if ax.get_legend_handles_labels()[0]: ax.legend(fontsize=8)
    if not comparisons:
        axes[0,0].text(.05,.5,'缺合格市场／行业资料，仅保留个股分析',transform=axes[0,0].transAxes)
    fig.suptitle('市场／行业技术背景：各列独立口径，不替代经营周期')
    fig.tight_layout();fig.savefig(out/'market-background.png',dpi=140);plt.close(fig)


def report_markdown(document):
    frames=document['timeframes'];assessments={key:frame_assessment(value) for key,value in frames.items()}
    daily=assessments.get('1d',{});primary=frames.get('1d',{})
    primary_trend=daily.get('trend','尚不可判：未提供日线，不能用分钟状态替代波段判断')
    mature=primary.get('metadata',{}).get('qualification')=='validated'
    conclusion=('就本次日线研究输入而言，' if mature else '仅作日线方向诊断，')+primary_trend+'。'
    relative='没有合格固定窗口比较，不能判断相对市场强弱'
    for comparison in document['background'].get('comparisons',[]):
        window=comparison.get('windows',{}).get('20',{})
        if comparison.get('status')=='ok' and window.get('status')=='ok':
            relative=f"相对{comparison.get('id') or '合格基准'}，近20个交易间隔（21点）算术超额{window['excess_return']*100:.2f}个百分点"
            break
    security=document['security']
    lines=['# '+security.get('company',security['security_id'])+'｜技术面分析','',
        f"证券：{security['security_id']}；股类：{security.get('share_class','未知')}；币种：{security['currency']}。",
        f"价格截止：{document['market_cutoff']}；基本面信息截止：{document['information_cutoff']}。",
        f"实际生成：{document['generated_at']}；本次可知资料截止：{document['asof']}。",'',
        '## 1. 一页结论：技术面到底怎样','',f'**{conclusion}**','',
        '这里以日线回答波段趋势，以周月看较长背景，以分钟看短线状态；各层不合并投票。技术“偏强/偏弱”描述冻结价格路径，不是企业价值或未来收益。','',
        '|判断层|当前判断|证据与使用边界|','|---|---|---|',
        f"|日线趋势|{primary_trend}|价格、SMA20和SMA60的已定义排列；不是主观总分|",
        f"|日线动量|{daily.get('momentum','无日线动量可判断')}|MACD和RSI是同一价格序列的不同描述，不算两张独立选票|",
        f"|相对强弱|{relative}|固定窗口历史比较，不等于未来收益或经营景气|",
        f"|量能|{daily.get('volume','无日线量能可判断')}|不能据量能标签推定机构买卖|",
        f"|资料成熟度|{'已声明日线研究资格' if mature else '日线资格不足或只有诊断'}|来源声明不等于程序独立证明全部制度、复权和历史可得性|",'',
        '### 多周期速览','', '|周期|方向|末端与资格|','|---|---|---|']
    for frame,value in frames.items():
        meta=value['metadata'];summary=assessments[frame]
        label=summary['trend'] if value.get('bars') else '未运行，不能判断强弱'
        kind='价格采样；' if meta.get('price_only') else ''
        lines.append(f"|{PERIODS.get(frame,frame)}|{label}|{kind}{meta.get('data_asof','未知')}；{STATES.get(meta.get('qualification'),'资格待核')}|")
    states={v['state'] for v in assessments.values()}
    if 'strong' in states and 'weak' in states:
        lines+=['','**周期分歧**：部分周期偏强、部分偏弱，表示观察期限或价格口径不同；不能用长期背景或分钟反弹覆盖日线弱势，也不能把未核原价长期信号当确认。']
    lines+=['','## 2. 市场背景：相对大盘是强是弱','',
        '读图：上图把共同起点归一为100，比较同窗价格表现；下图是个股/基准相对指数，向上表示相对改善。两条原始指数点位不能直接相减。','',
        '![市场行业背景](market-background.png)','', '|基准与窗口|个股收益|基准收益|算术超额|解读|','|---|---|---|---|---|']
    usable_background=False
    background_notes=[]
    for c in document['background'].get('comparisons',[]):
        name=c.get('id') or ('市场' if c.get('reference')=='market' else '行业')
        if c.get('status')!='ok':
            lines.append(f"|{name}|不可比较|不可比较|缺失|该层未取得可比数据，不能补成行业强弱|")
            continue
        periods=background_windows(c)
        for entry in periods:
            usable_background=True
            excess=entry['excess_return'];direction='跑赢基准' if excess>0 else '跑输基准' if excess<0 else '与基准持平'
            label=entry['label']+f"，{entry['start'][:10]}—{entry['end'][:10]}"
            lines.append(f"|{name}，{label}|{entry['stock_return']:.2%}|{entry['reference_return']:.2%}|{excess*100:.2f}个百分点|{direction}|")
        full=next((row for row in periods if row['full_window']),None)
        recent=[row for row in periods if not row['full_window']]
        if full and recent:
            if full['excess_return']>0 and all(row['excess_return']<0 for row in recent):
                interpretation='图示全窗仍累计领先，但所列近窗均跑输：较早阶段形成的累计优势尚未完全消失，近期相对表现已经转弱。'
            elif full['excess_return']<0 and all(row['excess_return']>0 for row in recent):
                interpretation='图示全窗仍累计落后，但所列近窗均跑赢：近期相对修复尚未抹平较早阶段的落后。'
            else:
                interpretation='长窗与各近窗分别按表中起止判断；窗口同向则是不同期限上的同向描述，窗口分歧则保留各自事实。'
            background_notes.append(f'**{name}长短窗解读**：'+interpretation+'取样起点和期限不同，不能把这种反差当作数据矛盾，也不能由此推定机构建仓、减仓或经营周期。')
    lines+=['',*background_notes,'',('**背景小结**：同时保留图示全窗和固定近窗结果；与日线方向分别看。' if usable_background else '**背景小结**：缺少合格基准，本次只判断个股价格，不给市场或行业排名。')+
        '行业价格强弱不等于行业经营周期；市场偏弱不自动否决所有策略。','',
        '## 3. 逐周期读图、指标含义与小结','']
    for frame,value in frames.items():
        summary=assessments[frame];meta=value['metadata'];latest=summary['latest']
        lines += [f'### {PERIODS.get(frame,frame)}：'+summary['trend'],'']
        if not value.get('bars'):
            lines+=['本周期没有可用行情，不能输出趋势、结构或“无信号”结论。',cell('; '.join(meta.get('limitations',[])),500),'']
            continue
        lines+=['上栏看价格与均线，中栏看MACD快慢线及柱值，第三栏看RSI，底栏看成交量；先看当前方向，再看是否存在修复线索。', '',
            '|指标|末端值|说明|','|---|---|---|',f"|价格|{number(summary['close'])}|本周期最后真实观察值|",
            f"|SMA20 / SMA60 / SMA120|{number(latest.get('sma20'))} / {number(latest.get('sma60'))} / {number(latest.get('sma120'))}|分别使用20/60/120根，分钟与日线单位不同|",
            f"|MACD柱 / RSI14|{number(latest.get('macd_hist'))} / {number(latest.get('rsi14'))}|MACD柱未乘2；RSI低值不等于买点|",
            f"|ATR14|{number(latest.get('atr14'))}|波动距离，缺高低价时不计算；不是保证损失上限|",'',
            f'![{frame}技术图](technical-{frame}.png)','',f'[打开{frame}离线交互图](technical-{frame}.html)','',
            '**图表解读**：'+summary['momentum'],summary['volume'],'',
            f"**本周期小结**：{summary['trend']}。"+('此图仅为名义时点价格采样，不能确认完整分钟K线、量价或高低结构。' if meta.get('price_only') else '')+
            ('当前资格为诊断，不能升级为执行确认。' if meta.get('qualification')!='validated' else '方向判断依赖本次已声明的研究数据口径。'),'']
    lines+=['## 4. 专题图：是否给出额外确认','']
    for frame,value in frames.items():
        if not value.get('bars') or value['metadata'].get('price_only'): continue
        lines += [f'### {PERIODS.get(frame,frame)}专题分面','',f'![{frame}专题图](topics-{frame}.png)','']
        for text in topic_readings(value): lines += [text,'']
        if value['topics'].get('harmonic',{}).get('analysis'):
            lines += [f'![{frame}谐波](harmonic-{frame}.png)','',harmonic_reading(value),'',f'[打开{frame}谐波交互图](harmonic-{frame}.html)','']
        lines+=['**专题小结**：这些方法核验不同价格条件，不是独立资金来源；形态、结构或突破条件未形成，仍可保留常规趋势判断。','']
    lines+=['### 全周期计算状态','', '|周期|专题|实际状态|','|---|---|---|']
    for frame,value in frames.items():
        for name,topic in value['topics'].items():
            lines.append(f"|{PERIODS.get(frame,frame)}|{LABELS.get(name,name)}|{STATES.get(topic['status'],topic['status'])}|")
    lines+=['','## 5. 支持与冲突：哪些信息真正改变判断','',
        '|本次实际事实|它支持或冲突的条件|期限、来源或资格差异|','|---|---|---|']
    for fact,meaning,scope in evidence_readings(document,assessments):
        lines.append('|'+cell(fact,600)+'|'+cell(meaning,400)+'|'+cell(scope,400)+'|')
    lines+=['',
        '本表是阅读摘要，完整逐命题事实、原始值、支持/冲突状态、同源依赖与反证见[计算及审计附件](analysis.json)。同一日线派生多个指标和周月周期，不重复计票。','',
        '## 6. 操作条件：下一步观察什么','',*next_conditions(primary),'',
        '以上每次使用当根和前一完整根重算的动态均线，不是把本次数值冻结成目标价或个人止损价。原观察的定义和期限继续保留，新条件须另存版本。',
        '**背景与量能**：按原固定窗口看相对强弱是否改善；有合格量能时再核量价，缺分钟OHLC时不作分钟结构确认。','']
    execution=document['execution']
    if execution.get('status')=='not_requested':
        lines+=['本次为研究，没有请求个人交易操作，因此未索取持仓、成本或仓位，也未形成个人执行卡。']
    else:
        lines+=execution_card(execution,security['currency'])
    lines+=['','## 7. 与前次技术及基本面研究衔接','']
    previous=document.get('previous_technical',{})
    lines+=['### 前次技术事实对比','',f"原成果：{previous.get('result_id') or '未提供'}；{previous.get('reason','未提供可比较快照')}。",'',
        '|周期|前后窗口根数|可比性及变化|','|---|---|---|']
    for frame,row in previous.get('timeframes',{}).items():
        description='；'.join(row.get('reasons',[]))
        if row['status']=='comparable':
            names={'close':'价格','sma20':'SMA20','sma60':'SMA60','rsi14':'RSI14','macd_hist':'MACD柱'}
            description='；'.join(f"{names[k]} {number(v['previous'])}→{number(v['current'])}" for k,v in row['changes'].items())
        lines.append(f"|{frame}|{row['previous_bars']}→{row['current_bars']}|{description or '无可比较数值'}|")
    lines+=['','窗口增长和均线变动仅是事实更新。原研究生成、研究及行情截止分别保留；下面复盘原冻结观察，不用新条件追认旧预测。','',
        '### 基本面引用','', '|原成果|本次用途|资格或限制|','|---|---|---|']
    for ref in document['fundamental_references']:
        label='限定事实引用' if ref['reference_status']=='eligible' else '仅历史回顾'
        lines.append(f"|{ref.get('result_id')}|{label}|{cell('; '.join(ref['qualification_reasons'])) or '宿主已核本次限定事实；不刷新原预测资格'}|")
        if ref.get('original_lab_current_eligible') is False:
            lines.append('\n原档案当前资格仍为false；本次重读披露的有限事实不回写旧档。')
    if not document['fundamental_references']: lines.append('|未提供|不引用|价格图不补经营事实|')
    lines+=['','基本面回答经营与价值，技术面回答价格条件。增长强可以与价格弱并存，不能用其中一方覆盖另一方的反证。','',
        '## 8. 原关注项复盘','', '|原成果／项目|条件结果|执行事实|本次解释|','|---|---|---|---|']
    review_labels={'pending':'待后续验证','supported':'支持增强','weakened':'支持减弱','falsified':'已证伪','fulfilled':'已达到原条件',
        'partially_fulfilled':'部分达到','not_fulfilled':'未达到','not_due':'未到核验期','unknown':'未知','cannot_determine':'证据不足，不能判断',
        'not_triggered':'未触发','triggered_pending':'触发待确认','confirmed':'已确认','invalidated':'已失效','observed':'已观察',
        'no_change':'无实质变化','not_applicable':'不适用','withdrawn':'已撤回','expired':'已过期','executed':'用户反馈已执行',
        'user_did_not_trade':'用户反馈未交易','partial':'用户反馈部分执行','triggered_restricted':'触发但受限'}
    rows=document['review'].get('rows',[])
    for row in rows:
        lines.append(f"|{row.get('source_result_id')}／{row['item_id']}|{review_labels.get(row['condition_status'],row['condition_status'])}|{review_labels.get(row['execution_status'],row['execution_status'])}|{cell(row.get('reason'),500)}|")
    if document['review'].get('status')=='invalid_input':
        lines.append('|复盘输入校验失败|本轮未完成复盘|未知|'+cell('; '.join(document['review'].get('limitations',[])),600)+'|')
        lines+=['','本次复盘记录未通过输入校验，不能把校验失败写成没有旧关注项，须修正记录后再复核原条件。']
    elif not rows:
        lines.append('|未提供原冻结项|不能判断|未知|不造命中率、采纳或成交|')
    lines+=['','回顾的是原条件，不是事后另选阈值。无成交反馈不能推断未交易，也不生成假成交或0收益。','',
        '## 9. 最终技术判断与下一次更新','',f'**{conclusion}**','',
        '下一次研究优先检查日线排列与相对强弱是否改变，再核动量变化与量价是否支持趋势判断；分钟采样只能补短线观察。任何新条件或新期限应另存版本，原判断和原阈值保留。',
        '若数据资格不足，保留有依据的方向诊断与缺口，不升级为当前个性化执行结论。技术状态与基本面价值分别给出，不能把短线反弹等同于经营兑现。','',
        '研究室先读旧档，运行目录的archive-status.json记录保存及索引回执；归档副本的附件与完整性以该成果manifest为准。归档失败不回退源码；报告存在不表示已采纳交易计划。','',
        '## 10. 数据、方法和核验边界','',
        'OHLC分别是开盘、最高、最低、收盘，V是成交量。仅收盘采样不能还原根内高低和路径；空量能图表示缺数，不是成交量为0。',
        '完整输入、来源、参数、实际运行状态、逐项证据和首次可知时点随[analysis.json](analysis.json)冻结。资料今天取得可回看昨日，不能声称昨日已经有同版快照。','',
        '|周期|实际来源|价格／字段口径|','|---|---|---|']
    for frame,value in frames.items():
        meta=value['metadata']
        lines.append(f"|{frame}|{cell(meta.get('source','未提供'),300)}|{meta.get('adjustment',{}).get('mode','未知')}；"+('仅采样价' if meta.get('price_only') else 'OHLC或缺数据，见计算状态')+'|')
    lines+=['',*document['limitations'],'']
    return '\n'.join(lines)


def archive_result(request,document,target):
    config=request.get('research_lab',{})
    if not config.get('archive'):
        return {'status':'not_requested','saved':False,'reason':'未配置自动归档'}
    if not config.get('result_id'):
        return {'status':'failed','saved':False,'reason':'缺新成果ID，未归档'}
    extensions={'.png':'image/png','.html':'text/html','.json':'application/json'}
    attachments=[dict(source_file=str(p),relative_path=p.name,media_type=extensions[p.suffix])
                 for p in sorted(target.iterdir()) if p.suffix in extensions and p.name!='archive-status.json']
    frontmatter=(Path(__file__).resolve().parents[1]/'SKILL.md').read_text(encoding='utf-8-sig').split('---',2)[1]
    versions=re.findall(r'^  version: ["\x27]?(\d+\.\d+\.\d+)["\x27]?\s*$',frontmatter,re.MULTILINE)
    if len(versions)!=1:
        raise ValueError('技术Skill版本缺失或不唯一，未归档')
    archive=dict(security=request['security'],module='technical',module_version=versions[0],
        result_id=config['result_id'],generated_at=document['generated_at'],research_asof=document['information_cutoff'],
        data_asof=document['timeframes'].get('1d',{}).get('metadata',{}).get('data_asof'),
        parent_result_ids=config.get('parent_result_ids',[]),report_file=str(target/'report.md'),
        evidence=config.get('evidence',[]),items=config.get('items',[]),attachments=attachments)
    path=target/'archive-request.json';write_json(path,archive)
    return lab_call(config,'archive',request=path)


def run(request_path,output):
    request_path=Path(request_path).resolve()
    request=read_json(request_path)
    target=sibling('harmonic_analysis').safe_output(Path(output))
    document=calculate(request,request_path)
    target.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.technical-',dir=target.parent))
    try:
        write_json(stage/'analysis.json',document)
        render_background(document['background'],stage)
        for frame,value in document['timeframes'].items():
            if value.get('bars'):
                render_frame(frame,value,document['security'],stage)
        details=report_markdown(document)
        gallery=[]
        for frame,value in document['timeframes'].items():
            if not value.get('bars'): continue
            for prefix in ('technical','topics','harmonic'):
                file=f'{prefix}-{frame}.png'
                if (stage/file).is_file():
                    gallery.append('<figure><img loading="lazy" src="'+file+'" alt="'+html.escape(PERIODS.get(frame,frame))+'专题图"></figure>')
        (stage/'details.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>专题与方法附录</title>'
            '<style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;max-width:1100px;margin:32px auto;padding:0 20px}pre{white-space:pre-wrap;overflow-wrap:anywhere}img{width:100%}</style>'
            '<a href="report.html">返回简明报告</a><h1>专题与方法附录</h1><p>下列为完整审阅文本及图表，不增加独立证据。</p><pre>'
            +html.escape(details)+'</pre>'+''.join(gallery)+'</html>',encoding='utf-8')
        if document['industry_context'].get('status')=='ok':
            path=Path(document['industry_context']['input_file'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=document['industry_context']['input_sha256']:
                raise ValueError('行业快照在运行期间发生变化')
            write_json(stage/'industry-context.json',read_json(path))
        display_document={**document,'execution':{**document['execution'],
            'limitations':[execution_limitation(value) for value in document['execution'].get('limitations',[])]}}
        presentation=sibling('report_presentation').render_brief(display_document,
            {frame:frame_assessment(value) for frame,value in document['timeframes'].items()},stage)
        write_json(stage/'presentation-summary.json',presentation)
        stage.rename(target)
    except BaseException:
        shutil.rmtree(stage)
        raise
    status=archive_result(request,document,target)
    write_json(target/'archive-status.json',status)
    return {'output':str(target),'status':document['status'],'archive':status}


def pool_run(request_path,output,mode):
    request_path=Path(request_path).resolve();request=read_json(request_path)
    stocks=request.get('stocks',[])
    if not isinstance(stocks,list) or not stocks or mode=='compare' and not 2<=len(stocks)<=5:
        raise ValueError('比较须明确指定2—5只；筛选须明确证券池')
    if mode=='screen' and (not request.get('pool_asof') or not request.get('pool_source') or not request.get('conditions')):
        raise ValueError('筛选须冻结pool_asof、证券池来源与非空条件')
    target=sibling('harmonic_analysis').safe_output(Path(output));target.parent.mkdir(parents=True,exist_ok=True)
    records=[];seen=set()
    for file in stocks:
        p=Path(file)
        if not p.is_absolute(): p=request_path.parent/p
        stock_request=read_json(p)
        sid=stock_request['security']['security_id']
        if sid in seen: raise ValueError('证券池存在重复证券')
        seen.add(sid)
        doc=calculate(stock_request,p)
        daily=doc['timeframes'].get('1d',{})
        series=daily.get('series',{})
        close=daily.get('bars',[{}])[-1].get('close') if daily.get('bars') else None
        values={'close':close,'sma20':last(series,'sma20'),'sma60':last(series,'sma60'),'rsi14':last(series,'rsi14'),
                'amount':daily.get('bars',[{}])[-1].get('amount') if daily.get('bars') else None,
                'volume_ratio20':last(series,'volume_ratio20'),
                'qualification':daily.get('metadata',{}).get('qualification'),
                'market_cutoff':doc['market_cutoff'],'market_id':stock_request.get('market',{}).get('benchmark_id')}
        reasons=[]
        if mode=='screen':
            if sibling('technical_data').moment(doc['asof'])>sibling('technical_data').moment(request['pool_asof']):
                reasons.append({'reason':'个股分析可知截止晚于证券池冻结截止'})
            if values['qualification']!='validated':
                reasons.append({'reason':'未取得筛选所需价格资料资格'})
        for condition in request.get('conditions',[]):
            if (not isinstance(condition,dict) or set(condition)-{'field','op','value','right_field'} or
                condition.get('field') not in values or condition.get('op') not in ('gt','gte','lt','lte','eq') or
                ('value' in condition)==('right_field' in condition) or
                'right_field' in condition and condition['right_field'] not in values):
                raise ValueError('筛选只允许明示指标与gt/gte/lt/lte/eq条件')
            value=values[condition['field']]
            limit=values[condition['right_field']] if 'right_field' in condition else condition['value']
            try:
                matched=False if value is None or limit is None else {'gt':lambda:value>limit,'gte':lambda:value>=limit,'lt':lambda:value<limit,
                    'lte':lambda:value<=limit,'eq':lambda:value==limit}[condition['op']]()
            except TypeError:
                matched=False
            if not matched: reasons.append(dict(condition=condition,actual=value,reason='缺数／不满足'))
        records.append({'security_id':sid,'values':values,'candidate':not reasons,'exclusions':reasons,
                        'source_request_sha256':sibling('technical_data').digest(stock_request)})
    comparable=len({(r['values']['market_cutoff'],r['values']['market_id']) for r in records})==1
    result={'mode':mode,'pool_asof':request.get('pool_asof'),'pool_source':request.get('pool_source'),
            'comparable_baseline':comparable,'records':records,'limitations':[
                '筛选只形成候选，不生成买入名单；比较不使用总分排名。',
                '当前共同截止／市场ID一致仍须核股类、币种及价格口径；不等于全面可比。']}
    stage=Path(tempfile.mkdtemp(prefix='.technical-pool-',dir=target.parent))
    try:
        write_json(stage/'comparison.json',result)
        text=['# 指定证券比较／条件筛选','',f'共同截止与市场ID：{comparable}','',
              '|证券|指标与资格|是否候选|排除原因|','|---|---|---|---|']
        text += [f"|{r['security_id']}|{cell(r['values'],400)}|{r['candidate']}|{cell(r['exclusions'],400)}|" for r in records]
        (stage/'report.md').write_text('\n'.join(text)+'\n',encoding='utf-8');stage.rename(target)
    except BaseException:
        shutil.rmtree(stage);raise
    return {'output':str(target),'records':len(records),'comparable_baseline':comparable}


def main(argv=None):
    parser=argparse.ArgumentParser(description='六专题技术研究及四输出；只读本地数据，不联网或交易。')
    parser.add_argument('--request-json',required=True)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--mode',choices=('analyze','compare','screen'),default='analyze')
    args=parser.parse_args(argv)
    try:
        result=run(args.request_json,args.output_dir) if args.mode=='analyze' else pool_run(args.request_json,args.output_dir,args.mode)
    except (ValueError,KeyError,TypeError,OSError,ImportError,subprocess.SubprocessError) as error:
        print(json.dumps({'status':'failed','reason':str(error)},ensure_ascii=False),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    raise SystemExit(main())
