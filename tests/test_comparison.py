"""Fictional two-to-five security comparisons over real archive boundaries."""
import copy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.comparison import compare
from app.store import ResearchStore
from test_platform_dashboard import technical_document

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / 'skills/stock-research-lab/scripts/research_lab.py'
spec = importlib.util.spec_from_file_location('comparison_fixture_lab', TOOL)
lab = importlib.util.module_from_spec(spec); spec.loader.exec_module(lab)
ASOF = '2026-11-13'


def identity(code, currency='CNY'):
    return {'security_id': 'ZZ-TEST-' + code, 'company': '虚构' + code, 'market': 'ZZ', 'exchange': 'TEST',
            'code': code, 'share_class': 'ordinary', 'currency': currency}


def money(day, scale=1, margin=.4):
    return {'report_date': day, 'publish_date': '2026-10-20', 'revenue': 1000*scale, 'cost': 1000*scale*(1-margin),
        'net_profit_parent': 100*scale, 'net_profit_consolidated': 125*scale, 'cfo': 50*scale,
        'capex_cash': 20*scale, 'receivables': 60*scale, 'inventory': 80*scale}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); self.root=self.base/'private'; lab.init(self.root)
        self.store=ResearchStore(self.root, TOOL); self.a=identity('A');self.b=identity('B')
        self.add_finance(self.a);self.add_finance(self.b,scale=2,margin=.35)

    def add_finance(self, security, *, scale=1, margin=.4, role='full_research', suffix='', change=None):
        key='F'+security['code']+suffix
        source=self.base/(key+'.json'); report=self.base/(key+'.md')
        document={'unit':'CNY yuan','tables':{
            'annual':{'rows':[money('2024-12-31',scale*.5,margin),money('2025-12-31',scale,margin)]},
            'quarterly':{'rows':[money('2025-09-30',scale*.5,margin),money('2026-06-30',scale*.6,margin),
                money('2026-09-30',scale*.8,.3 if scale==1 else .45)]},
            'cumulative':{'rows':[money('2025-09-30',scale*.5,margin),money('2026-06-30',scale*.6,margin),money('2026-09-30',scale,margin)]}}}
        if change: change(document)
        source.write_text(json.dumps(document),encoding='utf-8');report.write_text('# 虚构\n\n**研究结论：仅供流程验证。**',encoding='utf-8')
        component=self.base/(key+'-component.json');component.write_text(json.dumps({'schema_version':'stock-research-components/1',
            'security':security,'result_id':key,'content_role':role,'components':[]}),encoding='utf-8')
        lab.archive(self.root,{'security':security,'module':'fundamental','module_version':'fixture','result_id':key,
            'generated_at':'2026-11-13T18:00:00+08:00','research_asof':'2026-11-13T17:00:00+08:00','data_asof':None,
            'parent_result_ids':[],'report_file':str(report),'items':[],'evidence':[],
            'attachments':[{'source_file':str(source),'relative_path':'financial-bottom-tables.json','media_type':'application/json'},
                {'source_file':str(component),'relative_path':'research-components.json','media_type':'application/json'}]})

    def add_technical(self, security, *, mode='raw', missing_last=False):
        document=technical_document()
        def bind(value):
            if isinstance(value,dict):
                for key,entry in value.items():
                    if key=='security':value[key]=dict(security)
                    elif key=='security_id':value[key]=security['security_id']
                    else:bind(entry)
            elif isinstance(value,list):
                for entry in value:bind(entry)
        bind(document)
        for frame in document['timeframes'].values(): frame['metadata']['adjustment']['mode']=mode
        if missing_last:
            frame=document['timeframes']['1d'];frame['bars'].pop()
            for values in frame['series'].values():values.pop()
            frame['metadata']['data_asof']=frame['bars'][-1]['time']
        key='T'+security['code'];source=self.base/(key+'.json');source.write_text(json.dumps(document),encoding='utf-8')
        report=self.base/(key+'.md');report.write_text('# 虚构\n\n**日线判断：仅诊断。**',encoding='utf-8')
        component=self.base/(key+'-component.json');component.write_text(json.dumps({'schema_version':'stock-research-components/1',
            'security':security,'result_id':key,'content_role':'full_research','components':[]}),encoding='utf-8')
        lab.archive(self.root,{'security':security,'module':'technical','module_version':'fixture','result_id':key,
            'generated_at':'2026-11-13T18:00:00+08:00','research_asof':'2026-11-13T17:00:00+08:00',
            'data_asof':document['timeframes']['1d']['metadata']['data_asof'],'parent_result_ids':[],
            'report_file':str(report),'items':[],'evidence':[],
            'attachments':[{'source_file':str(source),'relative_path':'analysis.json','media_type':'application/json'},
                {'source_file':str(component),'relative_path':'research-components.json','media_type':'application/json'}]})

    def view(self, **options):
        return compare(self.store,[self.a['security_id'],self.b['security_id']],asof=ASOF,**options)

    def rows(self, value):return {r['metric']:r for section in value['sections'] for r in section['rows']}

    def test_common_cumulative_recalculates_consolidated_cash_and_growth_with_sources(self):
        result=self.view(); rows=self.rows(result)
        self.assertEqual(result['financial_period'],'2026-09-30')
        self.assertAlmostEqual(rows['cash_conversion']['values'][0]['value'],.4)
        self.assertEqual(rows['fcf']['values'][0]['value'],30)
        self.assertEqual(rows['revenue_yoy']['values'][0]['value'],1)
        self.assertEqual(rows['gross_margin']['comparability'],'comparable')
        self.assertTrue(all(cell['source_refs'] for cell in rows['revenue_yoy']['values']))
        self.assertTrue(all(ref.get('generated_at') for cell in rows['cash_conversion']['values'] for ref in cell['source_refs']))
        self.assertIn('总排名',result['conclusions'][-1]['text'])

    def test_quarter_and_cumulative_margin_leadership_can_reverse(self):
        cumulative=self.rows(self.view())['gross_margin']['values']
        quarter=self.rows(self.view(period_basis='quarterly'))['gross_margin']['values']
        self.assertGreater(cumulative[0]['value'],cumulative[1]['value'])
        self.assertLess(quarter[0]['value'],quarter[1]['value'])
        annual=self.view(period_basis='annual');self.assertEqual(annual['financial_period'],'2025-12-31')

    def test_frozen_knowledge_date_excludes_later_generated_research(self):
        value=compare(self.store,[self.a['security_id'],self.b['security_id']],asof='2026-11-12')
        self.assertTrue(all(s['status']=='no_research' for s in value['securities']))
        self.assertIsNone(value['financial_period']);self.assertEqual(len(value['conclusions']),1)

    def test_unknown_unit_and_different_currency_do_not_silently_scale_or_rank(self):
        self.add_finance(identity('C','USD'))
        value=compare(self.store,[self.a['security_id'],'ZZ-TEST-C'],asof=ASOF)
        self.assertEqual(self.rows(value)['revenue']['comparability'],'not_comparable')
        def unknown(d):d['unit']='amounts; scale unknown'
        self.add_finance(identity('D'),change=unknown)
        value=compare(self.store,[self.a['security_id'],'ZZ-TEST-D'],asof=ASOF)
        self.assertEqual(self.rows(value)['revenue']['comparability'],'not_comparable')

    def test_explicit_nonshared_period_is_missing_without_forward_filling(self):
        value=self.view(financial_period='2020-09-30')
        row=self.rows(value)['revenue']
        self.assertEqual(row['comparability'],'unavailable');self.assertTrue(all(c['value'] is None for c in row['values']))

    def test_zero_denominator_and_loss_comparative_are_unavailable(self):
        def zero(d):
            d['tables']['cumulative']['rows'][-1]['net_profit_consolidated']=0
            d['tables']['cumulative']['rows'][0]['net_profit_parent']=-10
        self.add_finance(identity('C'),change=zero)
        value=compare(self.store,[self.a['security_id'],'ZZ-TEST-C'],asof=ASOF);rows=self.rows(value)
        self.assertIsNone(rows['cash_conversion']['values'][1]['value'])
        self.assertIsNone(rows['parent_profit_yoy']['values'][1]['value'])
        self.assertEqual(rows['cash_conversion']['comparability'],'unavailable')

    def test_yoy_denominator_publication_and_same_basis_conflicts_are_checked(self):
        original=self.store.fundamental_dashboard
        for issue in ['unknown_publication','same_basis_conflict','other_basis_conflict']:
            def view(sid,asof):
                value=original(sid,asof)
                if sid==self.a['security_id']:
                    previous=next(r for r in value['cumulative'] if r['report_date']=='2025-09-30')
                    if issue=='unknown_publication':previous['publish_date']=None
                    else:value['conflicts'].append({'report_date':'2025-09-30','fields':['revenue'],
                        'period_basis':'single_quarter' if issue=='other_basis_conflict' else 'cumulative'})
                return value
            with self.subTest(issue=issue),patch.object(self.store,'fundamental_dashboard',side_effect=view):
                row=self.rows(self.view())['revenue_yoy']
                self.assertEqual(row['comparability'],'comparable' if issue=='other_basis_conflict' else 'not_comparable')

    def test_broker_supplement_cannot_replace_formal_financial_input(self):
        self.add_finance(self.a,scale=100,role='broker_source',suffix='BROKER')
        self.assertEqual(self.rows(self.view())['revenue']['values'][0]['value'],1000)

    def test_raw_diagnostic_series_uses_n_plus_one_common_dates(self):
        self.add_technical(self.a);self.add_technical(self.b,missing_last=True)
        value=self.view(price_window=20);p=value['price_series']
        self.assertEqual(p['status'],'diagnostic');self.assertEqual(len(p['dates']),21)
        self.assertEqual(p['window']['end'],'2026-11-12')
        self.assertEqual(p['series'][0]['values'][0],100)
        self.assertEqual(self.rows(value)['nominal_price_change']['comparability'],'diagnostic')

    def test_mixed_raw_and_uncertified_reference_without_raw_proof_never_overlay(self):
        self.add_technical(self.a);self.add_technical(self.b,mode='qfq')
        result=self.view();self.assertEqual(result['price_series']['status'],'not_comparable')
        self.assertEqual(result['price_series']['series'],[])

    def test_five_security_read_only_comparison_and_invalid_requests(self):
        for code in ['C','D','E']:self.add_finance(identity(code))
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('app.research_provider.run_turn',side_effect=AssertionError('Read-only comparison cannot invoke a model')):
            result=compare(self.store,['ZZ-TEST-'+c for c in 'ABCDE'],asof=ASOF)
        self.assertEqual(len(result['securities']),5)
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        for ids, options in [(['ZZ-TEST-A'],{}),(['ZZ-TEST-A']*2,{}),(['ZZ-TEST-'+c for c in 'ABCDEF'],{}),
                (['../outside','ZZ-TEST-B'],{}),(['ZZ-TEST-A','ZZ-TEST-B'],{'asof':'2999-01-01'}),
                (['ZZ-TEST-A','ZZ-TEST-B'],{'price_window':True}),(['ZZ-TEST-A','ZZ-TEST-B'],{'period_basis':'mixed'}),
                (['ZZ-TEST-A','ZZ-TEST-B'],{'period_basis':'annual','financial_period':'2026-09-30'})]:
            with self.subTest(ids=ids,options=options),self.assertRaises(ValueError):compare(self.store,ids,**options)

    def test_snapshot_restore_rebuilds_same_comparison_without_workspace(self):
        from test_archive_backup import archive
        before=self.view();snapshot=self.base/'snapshot';restored=self.base/'restored'
        archive.backup(lab,self.root,snapshot);archive.restore(lab,snapshot,restored)
        self.assertFalse((restored/'_workspace').exists())
        after=compare(ResearchStore(restored,TOOL),[self.a['security_id'],self.b['security_id']],asof=ASOF)
        self.assertEqual(before,after)

    def test_catalog_snapshot_is_request_local_and_next_read_rechecks_tampered_source(self):
        original_scan=self.store._scan
        with patch.object(self.store,'_scan',wraps=original_scan) as scan:
            self.view();self.assertEqual(scan.call_count,1)
        record=next(r for r in lab.scan(self.root)['results'] if r['result_id']=='FA')
        path=(self.root/record['path']).parent/'financial-bottom-tables.json'
        path.write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):self.view()
