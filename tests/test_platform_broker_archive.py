"""Fictional promotion, all-host gate and cache-independent registered originals."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from app.broker_archive import archive_source, compatibility
from app.broker_cache import source_detail, source_file, sources
import test_platform_broker_cache as fixtures
from test_platform_broker_cache import PAYLOAD, RECORD
from test_platform_server import IDENTITY, REPO


class BrokerArchiveTests(unittest.TestCase):
    setUp = fixtures.BrokerCacheTests.setUp
    retain = fixtures.BrokerCacheTests.retain

    def reviewed(self):
        return self.retain(views=[{'text':'虚构观点，只作补充','page':1}], review={
            'index_source_id':RECORD['source_id'], 'pdf_sha256':hashlib.sha256(PAYLOAD).hexdigest(),
            'reason':'虚构页级对应测试', 'evidence_pages':[1], 'original_report_date':'2026-01-04'})

    def promote(self, source):
        with patch('app.broker_archive.compatibility', return_value={'enabled':True}):
            return archive_source(self.store, IDENTITY['security_id'], source['source_id'])

    def host_config(self, hosts):
        folder = self.root / '_workspace/platform'
        folder.mkdir(parents=True, exist_ok=True)
        members = [row for row in (REPO/'release-files.txt').read_text(encoding='utf-8').splitlines() if row.startswith('skills/')]
        fingerprints = {}
        for name in members:
            fingerprints[name] = hashlib.sha256((REPO/name).read_bytes()).hexdigest()
            for host in hosts:
                target = Path(host).joinpath(*Path(name).parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO/name, target)
        self.store.lab.write_json(folder/'skill-hosts.json', {'schema_version':'stock-research-hosts/1',
            'role_resolution':'stock-research-role-resolver/1', 'verified_at':self.store.lab.now(),
            'installed_roots':list(map(str,hosts)), 'files':fingerprints})

    def recognized_formal(self):
        component = self.root.parent/'formal-components.json'
        component.write_text(json.dumps({'schema_version':'stock-research-components/1','security':IDENTITY,
            'result_id':'KNOWN-FORMAL','content_role':'full_research',
            'components':[{'component_id':'summary','kind':'research'}]}),encoding='utf-8')
        self.store.lab.archive(self.root,{'security':IDENTITY,'module':'fundamental','module_version':'fixture',
            'result_id':'KNOWN-FORMAL','generated_at':'2026-01-03T18:00:00+08:00',
            'research_asof':'2026-01-03T17:00:00+08:00','data_asof':None,
            'parent_result_ids':[],'report_file':str(self.root.parent/'report.md'),'evidence':[],'items':[],
            'attachments':[{'source_file':str(component),'relative_path':'research-components.json','media_type':'application/json'}]})

    def test_gate_closed_for_missing_or_changed_host_files(self):
        self.assertFalse(compatibility(self.store)['enabled'])
        hosts = [self.root.parent/'host-a', self.root.parent/'host-b']
        self.host_config(hosts)
        self.assertTrue(compatibility(self.store)['enabled'])
        (hosts[0]/'stock-research-lab/scripts/research_lab.py').write_text('old backend', encoding='utf-8')
        self.assertFalse(compatibility(self.store)['enabled'])

    def test_unreviewed_or_gate_closed_cannot_promote(self):
        source = self.retain()
        with self.assertRaises(ValueError):
            archive_source(self.store, IDENTITY['security_id'], source['source_id'])
        with self.assertRaises(ValueError):
            self.promote(source)
        self.assertEqual(len(self.store.context(IDENTITY['security_id'])['results']), 1)

    def test_import_idempotence_publication_and_old_originals_preserved(self):
        self.recognized_formal()
        source = self.reviewed()
        before = {p:p.read_bytes() for p in (self.root/'securities').rglob('*') if p.is_file()}
        old_formal = self.store.context(IDENTITY['security_id'])['roles']['formal_fundamental']
        self.assertEqual(old_formal['result_id'],'KNOWN-FORMAL')
        first = self.promote(source)
        self.assertEqual(first['status'], 'archived')
        self.assertEqual(self.promote(source)['status'], 'already_archived')
        context = self.store.context(IDENTITY['security_id'])
        self.assertEqual(context['roles']['formal_fundamental'], old_formal)
        role = context['roles']['supplementary'][0]
        self.assertEqual(role['published_date'], '2026-01-05')
        self.assertEqual(role['cover_report_date'], '2026-01-04')
        self.assertEqual(role['content_role'], 'broker_source')
        self.assertTrue(all(p.read_bytes() == payload for p,payload in before.items()))
        detail = source_detail(self.store, IDENTITY['security_id'], source['source_id'])
        self.assertTrue(detail['formal_archive'])
        self.assertFalse(detail['can_replace_formal'])
        self.assertEqual(source_file(self.store, IDENTITY['security_id'], source['source_id']), PAYLOAD)
        self.assertEqual(len(sources(self.store, IDENTITY['security_id'])), 1)

    def test_registered_sources_work_without_workspace_cache(self):
        source = self.reviewed()
        self.promote(source)
        with patch('app.broker_cache._base', return_value=self.root/'_workspace/missing-cache'):
            detail = source_detail(self.store, IDENTITY['security_id'], source['source_id'])
            self.assertTrue(detail['formal_archive'])
            self.assertEqual(source_file(self.store, IDENTITY['security_id'], source['source_id']), PAYLOAD)
            self.assertEqual(sources(self.store, IDENTITY['security_id'])[0]['publication_date'], '2026-01-05')
        path = REPO/'skills/stock-research-archive/scripts/archive_backup.py'
        spec = importlib.util.spec_from_file_location('broker_archive_backup_test',path)
        backup = importlib.util.module_from_spec(spec);spec.loader.exec_module(backup)
        snapshot,restored = self.root.parent/'snapshot',self.root.parent/'restored'
        backup.backup(self.store.lab,self.root,snapshot)
        self.assertTrue(backup.verify_snapshot(self.store.lab,snapshot)['verified'])
        backup.restore(self.store.lab,snapshot,restored)
        self.assertFalse((restored/'_workspace').exists())
        restored_store = fixtures.ResearchStore(restored,fixtures.TOOL)
        self.assertEqual(source_file(restored_store,IDENTITY['security_id'],source['source_id']),PAYLOAD)
        self.assertTrue(source_detail(restored_store,IDENTITY['security_id'],source['source_id'])['formal_archive'])
        self.assertEqual(sources(restored_store,IDENTITY['security_id'])[0]['publication_date'],'2026-01-05')

    def test_registered_original_tampering_is_rejected(self):
        source = self.reviewed()
        result = self.promote(source)
        original = self.root/'securities'/IDENTITY['security_id']/'imports'/result['import_id']/'original/original.pdf'
        original.write_bytes(b'%PDF- modified')
        with self.assertRaises(ValueError):
            source_file(self.store, IDENTITY['security_id'], source['source_id'])

    def test_two_aliases_of_one_physical_host_do_not_open_compatibility(self):
        host = self.root.parent/'one-host'
        self.host_config([str(host),str(host)+'/.'])
        self.assertFalse(compatibility(self.store)['enabled'])

    def test_cache_pdf_change_after_validated_read_does_not_change_registered_original(self):
        source = self.reviewed()
        cached = self.root/'_workspace/platform/broker-cache'/IDENTITY['security_id']/source['source_id']/'original.pdf'
        actual = self.store.lab.import_report
        def changed(root, request):
            self.assertNotEqual(Path(request['source_file']),cached)
            self.assertEqual(Path(request['source_file']).read_bytes(),PAYLOAD)
            cached.write_bytes(b'%PDF- changed after validated read')
            return actual(root,request)
        with patch.object(self.store.lab,'import_report',side_effect=changed):
            result = self.promote(source)
        self.assertTrue(result['saved'])
        self.assertEqual(source_file(self.store,IDENTITY['security_id'],source['source_id']),PAYLOAD)
        self.assertTrue(source_detail(self.store,IDENTITY['security_id'],source['source_id'])['formal_archive'])
        self.assertEqual(len(sources(self.store,IDENTITY['security_id'])),1)
        self.assertEqual(self.promote(source)['status'],'already_archived')

    def test_unarchived_corrupt_cache_still_fails_closed(self):
        source = self.reviewed()
        cached = self.root/'_workspace/platform/broker-cache'/IDENTITY['security_id']/source['source_id']/'original.pdf'
        cached.write_bytes(b'%PDF- corrupt unregistered original')
        with self.assertRaises(ValueError):
            sources(self.store,IDENTITY['security_id'])
        with self.assertRaises(ValueError):
            self.promote(source)
        self.assertEqual(self.store.lab.scan(self.root)['imports'],[])

    def test_expected_hash_rejects_changed_preparation_before_any_registration(self):
        source = self.reviewed()
        actual = self.store.lab.import_report
        def changed(root, request):
            self.assertEqual(request['expected_original_sha256'],hashlib.sha256(PAYLOAD).hexdigest())
            Path(request['source_file']).write_bytes(b'%PDF- changed prepared original')
            return actual(root,request)
        with patch.object(self.store.lab,'import_report',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'Original changed'):
                self.promote(source)
        catalog = self.store.lab.scan(self.root)
        self.assertEqual(catalog['imports'],[])
        self.assertEqual(len(catalog['results']),1)
        self.assertEqual(source_file(self.store,IDENTITY['security_id'],source['source_id']),PAYLOAD)
        self.assertEqual(len(self.store.context(IDENTITY['security_id'])['results']),1)

    def test_saved_source_remains_saved_when_post_commit_projection_fails(self):
        source = self.reviewed()
        actual = self.store.context
        calls = []
        def unavailable(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:
                raise OSError('Fictional post-commit projection failure')
            return actual(*args,**kwargs)
        with patch.object(self.store,'context',side_effect=unavailable):
            result = self.promote(source)
        self.assertTrue(result['saved'])
        self.assertTrue(result['formal_archive'])
        self.assertIn('勿重复提交',result['warning'])
        self.assertEqual(len(self.store.lab.scan(self.root)['results']),2)
        self.assertEqual(source_file(self.store,IDENTITY['security_id'],source['source_id']),PAYLOAD)

    def test_interrupted_promotion_can_resume_but_cannot_change_expected_original_hash(self):
        source = self.reviewed()
        move = self.store.lab.move_record
        def interrupted(root,stage,destination):
            if destination.name.startswith('BRS-'):
                raise OSError('Fictional interrupted adaptation')
            return move(root,stage,destination)
        with patch.object(self.store.lab,'move_record',side_effect=interrupted):
            partial = self.promote(source)
        self.assertTrue(partial['partial'])
        self.assertFalse(partial['formal_archive'])
        actual = self.store.lab.import_report
        def wrong_expected(root,request):
            return actual(root,{**request,'expected_original_sha256':'0'*64})
        with patch.object(self.store.lab,'import_report',side_effect=wrong_expected):
            with self.assertRaisesRegex(ValueError,'expected fingerprint'):
                self.promote(source)
        catalog = self.store.lab.scan(self.root)
        self.assertEqual(len(catalog['imports']),1)
        self.assertEqual(len(catalog['results']),1)
        self.assertEqual(self.promote(source)['status'],'archived')
        self.assertEqual(source_file(self.store,IDENTITY['security_id'],source['source_id']),PAYLOAD)
