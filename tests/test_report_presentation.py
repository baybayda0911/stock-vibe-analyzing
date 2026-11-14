import copy
from datetime import date,timedelta
import importlib.util
import json
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch


SCRIPT=Path(__file__).resolve().parents[1]/'skills/stock-technical-analysis/scripts/report_presentation.py'
spec=importlib.util.spec_from_file_location('report_presentation',SCRIPT)
presentation=importlib.util.module_from_spec(spec);spec.loader.exec_module(presentation)


def fixture(descending=False,count=126):
    prices=[100+(-1 if descending else 1)*i/4 for i in range(count)]
    bars=[dict(time=(date(2026,1,1)+timedelta(days=i)).isoformat(),open=close+(.3 if descending else -.3),
               high=close+1,low=close-1,close=close,volume=1000+i) for i,close in enumerate(prices)]
    moving={f'sma{n}':[None if i<n-1 else sum(prices[i-n+1:i+1])/n for i in range(count)] for n in (20,60)}
    state=('unknown' if count<60 else 'weak' if descending else 'strong')
    latest=dict(sma20=moving['sma20'][-1],sma60=moving['sma60'][-1],
                macd_hist=(-.5 if descending else .5),rsi14=(24 if descending else 60),
                price_change=(prices[-1]/prices[-2]-1 if count>1 else None),volume_ratio20=(1.1 if count>20 else None))
    frame=dict(status='ok',bars=bars,series={**moving,'macd_hist':[(-.2 if descending else .2)]*(count-1)+[latest['macd_hist']],
               'volume_ratio20':[None if i<20 else 1.1 for i in range(count)]},
               metadata=dict(qualification='validated',data_asof=bars[-1]['time']),
               topics=dict(conventional=dict(latest=latest)))
    document=dict(security=dict(company='虚构公司（版式验证）',code='SYNTHETIC',currency='CNY'),
        status='analyzed_with_qualifications',market_cutoff='2026-06-30T15:00:00+08:00',
        information_cutoff='2026-06-30T15:00:00+08:00',asof='2026-06-30T18:00:00+08:00',
        generated_at='2026-06-30T10:02:00+00:00',timeframes={'1d':frame},
        background=dict(comparisons=[]),execution=dict(status='not_requested'),review=dict(status='ok',rows=[]))
    assessment={'1d':dict(state=state,close=prices[-1],latest=latest)}
    return document,assessment


class PresentationTests(unittest.TestCase):
    def test_actual_price_order_determines_reader_state_without_votes(self):
        for descending,state in ((False,'strong'),(True,'weak')):
            document,assessment=fixture(descending)
            model=presentation._model(document,assessment)
            self.assertEqual(model['state'],state)
            self.assertEqual(model['close'],document['timeframes']['1d']['bars'][-1]['close'])
            self.assertIn('低于' if descending else '高于',model['reason'])
            self.assertIn('修复' if descending else '改判',''.join(model['conditions']))
            self.assertNotIn('胜率',json.dumps(model,ensure_ascii=False))
        document,assessment=fixture()
        document['timeframes']['1w']=copy.deepcopy(document['timeframes']['1d'])
        assessment['1w']={'state':'mixed'}
        weekly=next(row for row in presentation._model(document,assessment)['frames'] if row[0]=='周线')
        self.assertEqual(weekly[1],'方向分歧（价格与均线未同向）')

    def test_paused_minutes_and_short_annual_history_do_not_become_main_signals(self):
        document,assessment=fixture(count=12)
        document['timeframes']['5m']=dict(status='paused',bars=[],metadata={'qualification':'paused'})
        document['timeframes']['1y']=dict(bars=document['timeframes']['1d']['bars'][:8],metadata={'qualification':'diagnostic'})
        document['timeframes']['6mo']=dict(bars=document['timeframes']['1d']['bars'],metadata={'qualification':'diagnostic'})
        model=presentation._model(document,assessment)
        self.assertEqual(model['state'],'unknown')
        self.assertEqual(len(model['frames']),5)
        self.assertNotIn('5分钟',str(model['frames']))
        self.assertIn(['半年线','均线历史不足，暂不判断','未提供','仅作诊断'],model['frames'])
        self.assertIn(['年线','均线历史不足，暂不判断','未提供','仅作诊断'],model['frames'])
        self.assertEqual(len(document['timeframes']['1y']['bars']),8)
        self.assertTrue(any('仅作诊断' in text for text in model['warnings']))

    def test_weak_industry_proxy_is_named_without_inventing_official_ranking(self):
        document,assessment=fixture()
        document['background']['comparisons']=[dict(status='ok',reference='industry',label='虚构二级行业',
            weak_proxy=True,common_dates=['2026-01-01','2026-01-02'],relative_index=[100,99],
            windows={'20':{'excess_return':-.02}},
            latest_breadth={'valid_returns':91,'above_ma20':7,'ma20_eligible':89})]
        model=presentation._model(document,assessment)
        self.assertIn('落后2.00个百分点',model['relative'][0])
        self.assertIn('非官方申万指数',model['relative'][0])
        self.assertNotIn('排名',model['relative'][0])
        self.assertIn('有效91只',model['breadth'])
        self.assertIn('7/89（7.9%）',model['breadth'])
        self.assertNotIn('7/91',model['breadth'])

    def test_render_is_offline_escaped_compact_and_does_not_mutate_calculations(self):
        document,assessment=fixture(descending=True)
        document['security']['company']='<script>untrusted</script>'
        document['background']['comparisons']=[dict(status='ok',reference='market',label='虚构市场',
            common_dates=['2026-01-01','2026-05-06'],relative_index=[100,120],
            windows={'20':{'excess_return':-.02}})]
        before=copy.deepcopy(document)
        from matplotlib.axes import Axes
        original_bar=Axes.bar;original_line=Axes.axhline;bar_values=[];reference_lines=[]
        def track_bar(axis,x,height,*args,**kwargs):
            bar_values.append(list(height))
            return original_bar(axis,x,height,*args,**kwargs)
        def track_line(axis,y=0,*args,**kwargs):
            reference_lines.append(y)
            return original_line(axis,y,*args,**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(Axes,'bar',track_bar),patch.object(Axes,'axhline',track_line):
                result=presentation.render_brief(document,assessment,Path(tmp))
            text=(Path(tmp)/'report.md').read_text(encoding='utf-8')
            page=(Path(tmp)/'report.html').read_text(encoding='utf-8')
            self.assertEqual(document,before)
            self.assertEqual(result['primary_images'],2)
            self.assertLess(len(text),3500)
            self.assertIn('日线判断：偏弱',text)
            self.assertIn('当日量/此前20日均量',text)
            self.assertIn('收盘≥开盘',text)
            self.assertIn('收盘<开盘',text)
            self.assertIn('影线上下端是最高/最低价',text)
            self.assertIn('图示全窗',text)
            self.assertIn('近20个交易日见上方文字',text)
            self.assertIn('长窗领先与近期走弱可以同时存在',text)
            self.assertIn('落后2.00个百分点',text)
            self.assertEqual(len(bar_values),1)
            self.assertTrue(all(math.isnan(value) for value in bar_values[0][:20]))
            self.assertEqual(bar_values[0][20:],[1.1]*106)
            self.assertIn(1,reference_lines)
            self.assertNotIn('量／原单位',page)
            self.assertIn('2026-06-30 18:02（上海）',page)
            self.assertIn('data:image/png;base64,',page)
            self.assertNotIn('<script>untrusted',page)
            self.assertNotRegex(page,r'<(?:script|img|link)[^>]+(?:src|href)=["\']https?://')
            self.assertNotIn('本次执行条件卡',text)
            self.assertTrue((Path(tmp)/'price-overview.png').is_file())
            with self.assertRaises(FileExistsError):
                presentation.render_brief(document,assessment,Path(tmp))

    def test_missing_ratio_is_not_zero_filled_or_recomputed_from_raw_volume(self):
        document,assessment=fixture(count=12)
        assessment['1d']['latest']['volume_ratio20']=None
        self.assertTrue(all(row['volume']>0 for row in document['timeframes']['1d']['bars']))
        original=copy.deepcopy(document['timeframes']['1d']['bars'])
        with tempfile.TemporaryDirectory() as tmp:
            with patch('matplotlib.axes.Axes.bar') as volume:
                presentation.render_brief(document,assessment,Path(tmp))
            volume.assert_not_called()
            self.assertEqual(document['timeframes']['1d']['bars'],original)
            text=(Path(tmp)/'report.md').read_text(encoding='utf-8')
            self.assertIn('缺少合格量比，不填0',text)

    def test_action_card_shows_real_restriction_without_default_quantity(self):
        document,assessment=fixture()
        document['execution']=dict(status='non_personal',requested_quantity=None,
            prices={'analysis_trigger':'20','analysis_invalidation':'18'},
            checks={'dated_security_rules':False},limitations=['缺少目标日期制度原件'])
        with tempfile.TemporaryDirectory() as tmp:
            presentation.render_brief(document,assessment,Path(tmp))
            text=(Path(tmp)/'report.md').read_text(encoding='utf-8')
            self.assertIn('未提供，不默认',text)
            self.assertIn('缺少目标日期制度原件',text)
            self.assertIn('|制度与生效时间|未通过／待核|',text)
            self.assertNotIn('买入30%',text)


if __name__=='__main__': unittest.main()
