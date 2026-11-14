import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'skills/stock-research-archive/scripts/research_archive.py'
TOOL=ROOT/'skills/stock-research-lab/scripts/research_lab.py'
spec=importlib.util.spec_from_file_location('research_archive',SCRIPT)
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
lab=app.load_backend(TOOL)
SECURITY=dict(security_id='ZZ-TEST-DEMO',company='Fictional',market='ZZ',exchange='TEST',code='DEMO',share_class='ordinary',currency='TST')
ASOF='2026-02-03T18:00:00+08:00'


class ArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.root=self.base/'lab';lab.init(self.root)
        report=self.base/'report.md';report.write_text('[page](chart.html)\n[unlisted](secret.txt)\n',encoding='utf-8')
        chart=self.base/'chart.html';chart.write_text('<!doctype html><html><body>synthetic</body></html>',encoding='utf-8')
        lab.archive(self.root,dict(security=SECURITY,module='fundamental',module_version='0.1.0',result_id='R1',
            generated_at='2026-01-02T18:00:00+08:00',research_asof='2026-01-02T17:00:00+08:00',
            data_asof='2026-01-02T16:00:00+08:00',parent_result_ids=[],report_file=str(report),evidence=[],items=[],
            attachments=[dict(source_file=str(chart),relative_path='chart.html',media_type='text/html')]))
        self.record=self.root/'securities/ZZ-TEST-DEMO/fundamental/2026-01-02/R1'
        (self.record/'secret.txt').write_text('UNLISTED_CANARY',encoding='utf-8')

    def test_package_keeps_original_dates_bytes_and_declared_files_only(self):
        output=self.base/'package'
        result=app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'R1',output)
        self.assertEqual(result['files'],4)
        self.assertEqual(result['missing_local_links'],['secret.txt'])
        self.assertFalse((output/'secret.txt').exists())
        manifest=json.loads((output/'archive-package.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['generated_at'],'2026-01-02T18:00:00+08:00')
        for entry in manifest['files']:
            payload=(output/entry['relative_path']).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(),entry['sha256'])
            self.assertEqual(payload,(self.record/entry['relative_path']).read_bytes())
        with self.assertRaises(FileExistsError):
            app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'R1',output)

    def test_package_rejects_future_or_other_security_without_creating_output(self):
        for security,asof in ((SECURITY['security_id'],'2026-01-01T18:00:00+08:00'),('ZZ-TEST-OTHER',ASOF)):
            with self.subTest(security=security),self.assertRaises(ValueError):
                app.package_result(lab,self.root,security,asof,'R1',self.base/'missing')
            self.assertFalse((self.base/'missing').exists())

    def test_output_can_use_only_views_workspace_inside_lab(self):
        good=self.root/'_workspace/views/new'
        self.assertEqual(app.output_path(lab,good,self.root),good)
        for output in (self.root/'securities/new',self.root/'_staging/new',self.root/'_workspace/backups/new'):
            with self.subTest(output=output),self.assertRaises(ValueError):
                app.output_path(lab,output,self.root)
        installed=self.base/'installed';installed.mkdir();(installed/'SKILL.md').write_text('synthetic')
        with self.assertRaises(ValueError): app.output_path(lab,installed/'output',self.root)
        git=self.base/'git';git.mkdir();(git/'.git').write_text('gitdir: synthetic')
        with self.assertRaises(ValueError): app.output_path(lab,git/'output',self.root)

    def test_failed_copy_removes_only_new_staging_and_preserves_original(self):
        report=self.record/'report.md';before=report.read_bytes()
        with patch.object(lab,'copy_source',side_effect=OSError('synthetic copy failure')):
            with self.assertRaises(OSError):
                app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'R1',self.base/'bad')
        self.assertFalse((self.base/'bad').exists());self.assertFalse(list(self.base.glob('.archive-view-*')))
        self.assertEqual(report.read_bytes(),before)

    def test_cli_help_is_isolated_and_missing_backend_does_not_guess_neighbors(self):
        help_result=subprocess.run([sys.executable,'-I','-S','-X','utf8','-B',str(SCRIPT),'--help'],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(help_result.returncode,0,help_result.stderr)
        result=subprocess.run([sys.executable,'-X','utf8','-B',str(SCRIPT),'view',str(self.root),
            '--lab-tool',str(self.base/'research_lab.py'),'--security',SECURITY['security_id'],'--asof',ASOF,
            '--output-dir',str(self.base/'uncreated')],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,2);self.assertFalse((self.base/'uncreated').exists())
        self.assertIn('absolute research_lab.py',json.loads(result.stderr)['error'])

    def test_view_cli_uses_explicit_tool_and_private_workspace(self):
        output=self.root/'_workspace/views/first'
        result=subprocess.run([sys.executable,'-X','utf8','-B',str(SCRIPT),'view',str(self.root),
            '--lab-tool',str(TOOL),'--security',SECURITY['security_id'],'--asof',ASOF,
            '--output-dir',str(output)],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['saved'])
        for name in ('report.md','report.html','archive-view.json'): self.assertTrue((output/name).is_file())
        self.assertEqual(len(lab.context(self.root,SECURITY['security_id'],ASOF)['results']),1)

    def import_broker(self,reference_changes=None,*,include_reference=True):
        original=self.base/'fictional-original.pdf'
        original.write_bytes(b'%PDF-1.4\nFICTIONAL BROKER ORIGINAL\x00\xff\n%%EOF\n')
        reference=dict(security_id=SECURITY['security_id'],import_id='IMPORT-BROKER',
            original_sha256=hashlib.sha256(original.read_bytes()).hexdigest())
        reference.update(reference_changes or {})
        component=self.base/'research-components.json'
        document=dict(schema_version='stock-research-components/1',
            security=SECURITY,result_id='BROKER1',content_role='broker_source',source_ref=reference,
            published_at=None,published_date='2026-02-01',publication_precision='date_only',
            cover_report_date='2026-02-01',original_publication_date='2026-02-01',original_publication_precision='date_only',
            acquired_at='2026-02-02T12:00:00+08:00',extracted_at='2026-02-02T12:01:00+08:00',
            components=[dict(component_id='BROKER-TEXT',kind='broker_source')])
        if not include_reference:
            del document['source_ref']
        component.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
        with patch.object(lab,'now',return_value='2026-02-03T12:00:00+08:00'):
            lab.import_report(self.root,dict(security=SECURITY,module='fundamental',module_version='0.1.0',
                result_id='BROKER1',import_id='IMPORT-BROKER',source_file=str(original),
                original_generated_at='2026-02-01T12:00:00+08:00',original_date_evidence='Fictional cover date',
                research_asof='2026-02-01T12:00:00+08:00',data_asof=None,parent_result_ids=[],
                report_file=str(self.base/'report.md'),evidence=[],items=[],attachments=[
                    dict(source_file=str(component),relative_path='research-components.json',media_type='application/json')]))
        return original

    def test_registered_pdf_survives_backup_restore_export_without_changing_old_records(self):
        original=self.import_broker()
        backup=app.sibling('archive_backup')
        before={name:path.read_bytes() for name,path in backup._source_files(lab,self.root).items()}
        snapshot=self.base/'snapshot';restored=self.base/'restored'
        backup.backup(lab,self.root,snapshot)
        self.assertTrue(backup.verify_snapshot(lab,snapshot)['verified'])
        backup.restore(lab,snapshot,restored)
        exported=self.base/'broker-package'
        result=app.package_result(lab,restored,SECURITY['security_id'],ASOF,'BROKER1',exported)
        package=lab.read_json(exported/'archive-package.json')
        self.assertEqual(result['source_originals'],1)
        self.assertEqual(package['generated_at'],'2026-02-03T12:00:00+08:00')
        provenance=package['source_originals'][0]
        self.assertEqual(provenance['published_date'],'2026-02-01')
        self.assertEqual(provenance['publication_precision'],'date_only')
        self.assertIsNone(provenance['published_at'])
        self.assertEqual(provenance['cover_report_date'],'2026-02-01')
        self.assertEqual(provenance['original_publication_date'],'2026-02-01')
        self.assertEqual(provenance['imported_at'],'2026-02-03T12:00:00+08:00')
        self.assertEqual((exported/provenance['original_path']).read_bytes(),original.read_bytes())
        registration=lab.read_json(exported/provenance['import_metadata_path'])
        self.assertEqual(registration['original_date_evidence'],'Fictional cover date')
        for entry in package['files']:
            self.assertEqual(hashlib.sha256((exported/entry['relative_path']).read_bytes()).hexdigest(),entry['sha256'])
        self.assertEqual({name:path.read_bytes() for name,path in backup._source_files(lab,self.root).items()},before)
        self.assertEqual({name:path.read_bytes() for name,path in backup._source_files(lab,restored).items()},before)

    def test_source_reference_rejects_paths_foreign_security_and_wrong_hash(self):
        for changes in (dict(path='../outside.pdf'),dict(security_id='ZZ-TEST-OTHER'),
                        dict(import_id='../outside'),dict(original_sha256='0'*64)):
            with self.subTest(changes=changes):
                with tempfile.TemporaryDirectory() as temporary:
                    old_base,old_root=self.base,self.root
                    self.base=Path(temporary);self.root=self.base/'lab';lab.init(self.root)
                    (self.base/'report.md').write_text('Fictional supplement',encoding='utf-8')
                    try:
                        self.import_broker(changes)
                        with self.assertRaises(ValueError):
                            app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'BROKER1',self.base/'bad')
                        self.assertFalse((self.base/'bad').exists())
                    finally:
                        self.base,self.root=old_base,old_root

    def test_original_or_import_metadata_tampering_blocks_export(self):
        original=self.import_broker()
        registered=self.root/'securities/ZZ-TEST-DEMO/imports/IMPORT-BROKER'
        path=registered/'original'/original.name
        original_bytes=path.read_bytes()
        path.write_bytes(b'CHANGED ORIGINAL')
        with self.assertRaises(ValueError):
            app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'BROKER1',self.base/'bad-bytes')
        self.assertFalse((self.base/'bad-bytes').exists())
        path.write_bytes(original_bytes)
        metadata=lab.read_json(registered/'import.json')
        metadata['imported_at']='2026-02-04T12:00:00+08:00'
        (registered/'import.json').write_text(json.dumps(metadata),encoding='utf-8')
        with self.assertRaises(ValueError):
            app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'BROKER1',self.base/'bad-future')
        self.assertFalse((self.base/'bad-future').exists())

    def test_source_linked_export_requires_verified_shared_role_capability(self):
        self.import_broker()
        with patch.object(lab,'CAPABILITIES',{},create=True):
            with self.assertRaisesRegex(ValueError,'shared content-role resolver'):
                app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'BROKER1',self.base/'old-backend')
        self.assertFalse((self.base/'old-backend').exists())

    def test_legacy_supplement_without_registered_reference_exports_an_explicit_original_gap(self):
        self.import_broker(include_reference=False)
        output=self.base/'no-original-reference'
        result=app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'BROKER1',output)
        package=lab.read_json(output/'archive-package.json')
        self.assertEqual(result['source_originals'],0)
        self.assertEqual(package['source_original_gaps'],['missing_registered_source_ref'])
        self.assertFalse((output/'original-sources').exists())

    def test_resumed_import_keeps_earlier_import_time_and_later_adaptation_generation(self):
        original_move=lab.move_record
        def fail_adaptation(root,stage,destination):
            if destination.name=='BROKER1':
                raise OSError('Fictional interrupted adaptation')
            return original_move(root,stage,destination)
        with patch.object(lab,'import_report',wraps=lab.import_report) as importer:
            with patch.object(lab,'move_record',side_effect=fail_adaptation):
                original=self.import_broker()
            request=importer.call_args.args[1]
        self.assertFalse(any(row['result_id']=='BROKER1' for row in lab.scan(self.root)['results']))
        with patch.object(lab,'now',return_value='2026-02-03T12:10:00+08:00'):
            resumed=lab.import_report(self.root,request)
        self.assertTrue(resumed['resumed'])
        roles=lab.context(self.root,SECURITY['security_id'],ASOF)['roles']
        self.assertEqual(roles['supplementary'][0]['imported_at'],'2026-02-03T12:00:00+08:00')
        output=self.base/'resumed-package'
        app.package_result(lab,self.root,SECURITY['security_id'],ASOF,'BROKER1',output)
        package=lab.read_json(output/'archive-package.json')
        self.assertEqual(package['generated_at'],'2026-02-03T12:10:00+08:00')
        self.assertEqual(package['source_originals'][0]['imported_at'],'2026-02-03T12:00:00+08:00')
        self.assertEqual((output/package['source_originals'][0]['original_path']).read_bytes(),original.read_bytes())

    def test_archive_view_uses_shared_selection_and_keeps_source_events_separate(self):
        component=self.base/'formal-components.json'
        component.write_text(json.dumps(dict(schema_version='stock-research-components/1',security=SECURITY,
            result_id='FORMAL1',content_role='full_research',components=[dict(component_id='SUMMARY',kind='research')])),encoding='utf-8')
        lab.archive(self.root,dict(security=SECURITY,module='fundamental',module_version='0.1.0',result_id='FORMAL1',
            generated_at='2026-02-02T18:00:00+08:00',research_asof='2026-02-02T17:00:00+08:00',
            data_asof='2026-02-02T16:00:00+08:00',parent_result_ids=[],report_file=str(self.base/'report.md'),
            evidence=[],items=[],attachments=[dict(source_file=str(component),relative_path='research-components.json',media_type='application/json')]))
        self.import_broker()
        views=app.sibling('archive_views')
        document=views.build_view(lab,self.root,SECURITY['security_id'],ASOF)
        self.assertEqual(document['research_selection']['formal_fundamental'],'FORMAL1')
        self.assertEqual({row['result_id'] for row in document['results']},{'R1','FORMAL1','BROKER1'})
        self.assertEqual(document['source_events'][0]['result_id'],'BROKER1')
        self.assertEqual(document['source_events'][0]['published_date'],'2026-02-01')
        output=self.base/'role-view';output.mkdir()
        views.render_view(document,output)
        page=(output/'report.html').read_text(encoding='utf-8')
        self.assertIn('基本面最近正式研究：FORMAL1',page)
        self.assertIn('补充资料公开：2026-02-01',page)
        with patch.object(lab,'CAPABILITIES',{},create=True):
            old=views.build_view(lab,self.root,SECURITY['security_id'],ASOF)
        self.assertIsNone(old['research_selection']['formal_fundamental'])
        self.assertEqual(old['role_resolution'],'unavailable')
        self.assertTrue(all(row['content_role']=='unknown' for row in old['results']))


if __name__=='__main__': unittest.main()
