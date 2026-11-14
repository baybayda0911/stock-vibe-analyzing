"""Cross-consumer role regression tests using fictional, private temporary records."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT=Path(__file__).resolve().parents[1]
TOOL=ROOT/'skills/stock-research-lab/scripts/research_lab.py'


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lab=load('shared_roles_lab',TOOL)
technical=load('shared_roles_technical',ROOT/'skills/stock-technical-analysis/scripts/technical_analysis.py')
SECURITY=dict(security_id='ZZ-TEST-DEMO',company='Fictional Company',market='ZZ',exchange='TEST',
              code='DEMO',share_class='ordinary',currency='TST')
ASOF='2026-02-04T20:00:00+08:00'


class SharedRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'lab';lab.init(self.root)
        self.report=self.base/'report.md';self.report.write_text('Fictional frozen research',encoding='utf-8')

    def add(self,result_id,role,day='2026-02-01',*,document_changes=None,module='fundamental'):
        document=dict(schema_version='stock-research-components/1',security=SECURITY,result_id=result_id,
            content_role=role,components=[dict(component_id='SUMMARY',kind='research')])
        document.update(document_changes or {})
        source=self.base/(result_id+'-components.json')
        source.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
        generated=day+'T18:00:00+08:00'
        return lab.archive(self.root,dict(security=SECURITY,module=module,module_version='0.1.0',result_id=result_id,
            generated_at=generated,research_asof=day+'T17:00:00+08:00',data_asof=day+'T16:00:00+08:00',
            parent_result_ids=[],report_file=str(self.report),
            evidence=[dict(evidence_id='E1',source='Fictional filing',public_at='2026-01-31T10:00:00+08:00')],
            items=[dict(item_id=result_id+'-ITEM',type='observation',text='Frozen '+result_id,
                report_locator='summary',frozen_at=generated,status='pending',
                definition=dict(what='demand',why='fictional',next_evidence='next filing'),
                evidence_ids=['E1'],current_use='eligible',current_use_reason='Explicit fictional review',
                valid_until='2026-03-01T18:00:00+08:00')],
            attachments=[dict(source_file=str(source),relative_path='research-components.json',media_type='application/json')]))

    def context(self):
        return lab.context(self.root,SECURITY['security_id'],ASOF)

    def populated(self):
        self.add('FORMAL1','full_research')
        self.add('SOURCE1','broker_source','2026-02-03',document_changes=dict(published_date='2026-01-25',
            acquired_at='2026-02-02T10:00:00+08:00',extracted_at='2026-02-02T10:01:00+08:00'))
        self.add('UNKNOWN1','unrecognized','2026-02-04')
        return self.context()

    def test_later_sources_and_unknown_records_never_replace_formal_research_or_hide_old_items(self):
        context=self.populated()
        self.assertEqual(context['roles']['resolver_version'],'stock-research-role-resolver/1')
        self.assertEqual(context['roles']['formal_fundamental']['result_id'],'FORMAL1')
        self.assertEqual([row['result_id'] for row in context['research_results']],['FORMAL1'])
        self.assertEqual([row['result_id'] for row in context['source_results']],['SOURCE1'])
        self.assertEqual([row['result_id'] for row in context['roles']['unknown']],['UNKNOWN1'])
        self.assertEqual({row['item_id'] for row in context['pending_items']},
                         {'FORMAL1-ITEM','SOURCE1-ITEM','UNKNOWN1-ITEM'})
        source=context['roles']['supplementary'][0]
        self.assertEqual(source['published_date'],'2026-01-25')
        self.assertEqual(source['publication_precision'],'date_only')
        self.assertIsNone(source['published_at'])
        self.assertNotEqual(source['published_date'],source['generated_at'][:10])

    def test_cli_roles_matches_context_and_omits_future_research(self):
        expected=self.populated()['roles']
        self.add('FUTURE1','full_research','2026-02-05')
        result=subprocess.run([sys.executable,'-X','utf8','-B',str(TOOL),'roles',str(self.root),
            '--security',SECURITY['security_id'],'--asof',ASOF],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),expected)
        self.assertNotIn('FUTURE1',result.stdout)

    def test_unknown_component_schema_remains_history_without_claiming_a_formal_role(self):
        self.add('UNKNOWN-SCHEMA','full_research',document_changes=dict(schema_version='unrecognized/999'))
        context=self.context()
        self.assertEqual(len(context['results']),1)
        self.assertEqual(context['research_results'],[])
        self.assertIsNone(context['roles']['formal_fundamental'])
        self.assertEqual(context['roles']['classified'][0]['content_role'],'unknown')

    def test_component_identity_and_future_source_metadata_are_rejected(self):
        foreign=dict(SECURITY,security_id='ZZ-TEST-OTHER',code='OTHER')
        for change in (dict(security=foreign),dict(published_date='2026-02-05'),
                       dict(acquired_at='2026-02-05T12:00:00+08:00'),
                       dict(extracted_at='2026-02-05T12:00:00+08:00')):
            with self.subTest(change=change):
                with tempfile.TemporaryDirectory() as temporary:
                    original_root=self.root
                    self.root=Path(temporary)/'lab';lab.init(self.root)
                    try:
                        self.add('BAD1','broker_source',document_changes=change)
                        with self.assertRaises(ValueError): self.context()
                    finally:
                        self.root=original_root

    def test_technical_inheritance_accepts_only_shared_verified_formal_fundamental_items(self):
        context=self.populated()
        references=technical.context_fundamentals(dict(result=context))
        self.assertEqual([row['result_id'] for row in references],['FORMAL1'])
        self.assertEqual(references[0]['claims'],['Frozen FORMAL1'])
        self.assertEqual(references[0]['facts'][0]['source_result_id'],'FORMAL1')
        self.assertEqual(references[0]['facts'][0]['evidence_id'],'E1')
        for alteration in ('missing','resolver','schema'):
            unverified=copy.deepcopy(context)
            if alteration=='missing': del unverified['roles']
            elif alteration=='resolver': unverified['roles']['resolver_version']='old-resolver/0'
            else: unverified['roles']['schema_version']='stock-research-roles/0'
            with self.subTest(alteration=alteration):
                self.assertEqual(technical.context_fundamentals(dict(result=unverified)),[])


if __name__=='__main__': unittest.main()
