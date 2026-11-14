import copy
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/stock-research-lab/scripts/research_lab.py'
spec = importlib.util.spec_from_file_location('research_lab', SCRIPT)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
SECURITY = {'security_id': 'ZZ-TEST-DEMO', 'company': 'Fictional Company', 'market': 'ZZ',
            'exchange': 'TEST', 'code': 'DEMO', 'share_class': 'ordinary', 'currency': 'TST'}


class ResearchLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'private-lab'
        self.report = self.base / 'report.md'
        self.report.write_text('# Fictional research\n\nWatch demand.\n', encoding='utf-8')
        lab.init(self.root)

    def request(self, result_id='R1', item_id='I1', **changes):
        request = {'security': dict(SECURITY), 'module': 'fundamental', 'module_version': '0.1.0',
                   'result_id': result_id, 'generated_at': '2026-01-02T18:00:00+08:00',
                   'research_asof': '2026-01-02T17:00:00+08:00', 'data_asof': '2026-01-02T16:00:00+08:00',
                   'parent_result_ids': [], 'report_file': str(self.report),
                   'evidence': [{'evidence_id': 'E1', 'source': 'fictional excerpt p1',
                                 'public_at': '2026-01-02T09:00:00+08:00'}],
                   'items': [{'item_id': item_id, 'type': 'observation', 'text': 'Watch demand',
                              'report_locator': 'section 10, row 1', 'evidence_ids': ['E1'],
                              'frozen_at': '2026-01-02T18:00:00+08:00', 'status': 'pending',
                              'definition': {'what': 'demand', 'why': 'revenue driver', 'next_evidence': 'next filing'},
                              'current_use': 'eligible', 'current_use_reason': 'specified observation remains applicable',
                              'valid_until': '2026-02-02T18:00:00+08:00',
                              'review_due_at': '2026-02-02T18:00:00+08:00'}]}
        request.update(changes)
        return request

    def view(self, asof='2026-02-03T18:00:00+08:00'):
        return lab.context(self.root, SECURITY['security_id'], asof)

    def review_request(self, **changes):
        request = {'security_id': SECURITY['security_id'], 'review_id': 'RV1',
                   'generated_at': '2026-02-03T18:00:00+08:00', 'research_asof': '2026-02-03T17:00:00+08:00',
                   'data_asof': '2026-02-03T16:00:00+08:00', 'report_file': str(self.report), 'evidence': [],
                   'updates': [{'item_id': 'I1', 'status': 'cannot_determine', 'reason': 'No new filing',
                                'evidence_ids': [], 'current_use': 'needs_review'}]}
        request.update(changes)
        return request

    def plan_request(self, **changes):
        request = {'security_id': SECURITY['security_id'], 'plan_id': 'P1', 'plan_version_id': 'PV1',
                   'version': 1, 'source_result_ids': ['R1'], 'generated_at': '2026-01-02T19:00:00+08:00',
                   'frozen_at': '2026-01-02T19:00:00+08:00', 'effective_at': '2026-01-03T09:00:00+08:00',
                   'valid_until': '2026-02-02T18:00:00+08:00', 'trigger': 'test trigger',
                   'confirmation': 'test confirmation', 'invalidation': 'test invalidation', 'exit': 'test exit',
                   'adoption': {'status': 'research_only'}}
        request.update(changes)
        return request

    def execution_request(self, **changes):
        request = {'security_id': SECURITY['security_id'], 'import_id': 'X1', 'execution_kind': 'real',
                   'source_assertion': 'user_statement', 'generated_at': '2026-02-03T18:00:00+08:00',
                   'source_file': str(self.report),
                   'trades': [{'trade_id': 'T1', 'executed_at': '2026-01-03T10:00:00+08:00',
                               'time_precision': 'minute', 'side': 'buy', 'quantity': '10', 'price': '2',
                               'currency': 'TST', 'fees': None,
                               'allocations': [{'plan_id': 'P1', 'plan_version_id': 'PV1', 'quantity': '5'}]}]}
        request.update(changes)
        return request

    def attachment_request(self, result_id='R1', item_id='I1'):
        resources = {'analysis.json': (b'{"synthetic":true}\n', 'application/json'),
                     'chart.png': (base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1eMAAAAASUVORK5CYII='), 'image/png'),
                     'chart.html': (b'<!doctype html><html><body>synthetic chart</body></html>', 'text/html')}
        attachments = []
        for name, (payload, media) in resources.items():
            source = self.base / name
            source.write_bytes(payload)
            attachments.append({'source_file': str(source), 'relative_path': name, 'media_type': media})
        return self.request(result_id, item_id, attachments=attachments)

    def test_attachments_restore_report_links_and_survive_original_removal(self):
        self.report.write_text('![chart](chart.png)\n[interactive](chart.html)\n[data](analysis.json)\n', encoding='utf-8')
        request = self.attachment_request()
        expected = {entry['relative_path']: Path(entry['source_file']).read_bytes() for entry in request['attachments']}
        response = lab.archive(self.root, request)
        destination = self.root / response['path']
        manifest = lab.read_json(destination / 'manifest.json')
        self.assertEqual({e['relative_path'] for e in manifest['attachments']}, set(expected))
        for entry in manifest['attachments']:
            payload = expected[entry['relative_path']]
            self.assertEqual((destination / entry['relative_path']).read_bytes(), payload)
            self.assertEqual(entry['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual(entry['size_bytes'], len(payload))
            self.assertNotIn('source_file', entry)
        for entry in request['attachments']:
            Path(entry['source_file']).unlink()
        self.report.unlink()
        self.assertEqual(len(self.view()['results'][0]['attachments']), 3)
        self.assertEqual(lab.reindex(self.root)['counts']['results'], 1)
        with self.assertRaises(FileExistsError):
            lab.archive(self.root, request)

    def test_attachment_paths_types_and_control_names_are_strict(self):
        request = self.attachment_request()
        bad_paths = ('../chart.png', '/chart.png', 'C:/chart.png', 'a\\chart.png', './chart.png',
                     'a//chart.png', 'a/../chart.png', 'chart.png.', 'con.png', 'NUL .png', 'a?.png',
                     '.git/chart.png', 'manifest.json', 'a/REVIEW.json', 'v001.json')
        invalid = []
        for name in bad_paths:
            entry = dict(request['attachments'][0], relative_path=name)
            invalid.append([entry])
        invalid += [[dict(request['attachments'][0], media_type='text/html')],
                    [dict(request['attachments'][0], extra='undeclared')],
                    [dict(request['attachments'][0], source_file=str(self.base))],
                    [request['attachments'][1], dict(request['attachments'][1], relative_path='CHART.PNG')],
                    [request['attachments'][0], dict(request['attachments'][1], relative_path='analysis.json/chart.png')]]
        for entries in invalid:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                lab.archive(self.root, self.request(attachments=entries))
        self.assertEqual(lab.scan(self.root)['results'], [])
        git_source = self.base / 'git-source'
        git_source.mkdir()
        (git_source / '.git').write_text('gitdir: elsewhere', encoding='utf-8')
        source = git_source / 'data.json'
        source.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Git working tree'):
            lab.archive(self.root, self.request(attachments=[dict(request['attachments'][0], source_file=str(source))]))
        original_linked = lab.linked
        with patch.object(lab, 'linked', side_effect=lambda p: p == Path(request['attachments'][0]['source_file']) or original_linked(p)):
            with self.assertRaisesRegex(ValueError, 'reparse'):
                lab.archive(self.root, request)

    def test_attachment_payloads_and_stored_hashes_are_verified(self):
        request = self.attachment_request()
        for index, payload in ((0, b'{broken'), (0, b'{"value":NaN}'), (1, b'not a PNG'), (2, b'\xff')):
            source = Path(request['attachments'][index]['source_file'])
            original = source.read_bytes()
            source.write_bytes(payload)
            with self.subTest(index=index, payload=payload), self.assertRaises(ValueError):
                lab.archive(self.root, request)
            source.write_bytes(original)
        response = lab.archive(self.root, request)
        target = self.root / response['path'] / 'chart.png'
        original = target.read_bytes()
        old_catalog = (self.root / 'catalog.json').read_bytes()
        target.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, 'Attachment fingerprint'):
            self.view()
        with self.assertRaisesRegex(ValueError, 'Attachment fingerprint'):
            lab.reindex(self.root)
        self.assertEqual((self.root / 'catalog.json').read_bytes(), old_catalog)
        target.unlink()
        with self.assertRaisesRegex(ValueError, 'Attachment is missing'):
            self.view()
        target.write_bytes(original)
        self.assertTrue(lab.reindex(self.root)['catalog_updated'])

    def test_attachment_copy_registration_and_commit_failures_allow_same_id_retry(self):
        copy_source, write_json = lab.copy_source, lab.write_json
        for index, failure in enumerate(('copy', 'registration', 'commit')):
            request = self.attachment_request('R' + str(index), 'I' + str(index))
            if failure == 'copy':
                def broken_copy(source, destination):
                    if Path(source).name == 'analysis.json':
                        raise OSError('simulated attachment copy failure')
                    return copy_source(source, destination)
                context = patch.object(lab, 'copy_source', side_effect=broken_copy)
            elif failure == 'registration':
                def broken_registration(path, value):
                    if path.name == 'manifest.json':
                        raise OSError('simulated attachment registration failure')
                    return write_json(path, value)
                context = patch.object(lab, 'write_json', side_effect=broken_registration)
            else:
                context = patch.object(lab, 'move_record', side_effect=OSError('simulated commit failure'))
            with self.subTest(failure=failure), context, self.assertRaises(OSError):
                lab.archive(self.root, request)
            self.assertEqual(len(lab.scan(self.root)['results']), index)
            self.assertFalse((self.root / '.write.lock').exists())
            self.assertTrue(any((self.root / '_staging').iterdir()))
            response = lab.archive(self.root, request)
            self.assertTrue(response['saved'])
            self.assertEqual(len(lab.scan(self.root)['results']), index + 1)

    def test_attachment_index_failure_and_partial_import_recover_without_overwrite(self):
        request = self.attachment_request()
        with patch.object(lab, 'update_catalog', side_effect=OSError('simulated index failure')):
            response = lab.archive(self.root, request)
        self.assertTrue(response['saved'])
        self.assertFalse(response['catalog_updated'])
        manifest_bytes = (self.root / response['path'] / 'manifest.json').read_bytes()
        self.assertTrue(lab.reindex(self.root)['catalog_updated'])
        self.assertEqual((self.root / response['path'] / 'manifest.json').read_bytes(), manifest_bytes)
        request = self.attachment_request('R2', 'I2')
        request.update(import_id='OLD1', source_file=str(self.report), research_asof=None, data_asof=None)
        request['items'][0]['current_use'] = 'needs_review'
        move_record, calls = lab.move_record, []
        def interrupt_adaptation(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('simulated adaptation interruption')
            return move_record(*args)
        with patch.object(lab, 'move_record', side_effect=interrupt_adaptation):
            partial = lab.import_report(self.root, request)
        self.assertTrue(partial['partial'])
        resumed = lab.import_report(self.root, request)
        self.assertTrue(resumed['resumed'])
        self.assertEqual(len(lab.scan(self.root)['results']), 2)
        self.assertEqual(len(lab.read_json(self.root / resumed['path'] / 'manifest.json')['attachments']), 3)

    def test_rejects_git_file_and_directory_ancestors_without_running_git(self):
        for index, marker_is_file in enumerate((False, True)):
            parent = self.base / f'git-parent-{index}'
            parent.mkdir()
            marker = parent / '.git'
            marker.write_text('gitdir: elsewhere', encoding='utf-8') if marker_is_file else marker.mkdir()
            with self.assertRaisesRegex(ValueError, 'Git working tree'):
                lab.init(parent / 'private')
            self.assertFalse((parent / 'private').exists())
        (self.root / '.git').write_text('gitdir: elsewhere', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Git working tree'):
            lab.archive(self.root, self.request())

    def test_reparse_attribute_and_links_are_rejected(self):
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            self.assertTrue(lab.linked(self.root))
            with self.assertRaisesRegex(ValueError, 'reparse'):
                lab.checked_path(self.root)
        target = self.base / 'linked-root'
        try:
            os.symlink(self.root, target, target_is_directory=True)
        except OSError:
            return  # Windows without symlink privilege still exercises its native reparse flag above.
        with self.assertRaisesRegex(ValueError, 'reparse'):
            lab.archive(target, self.request())

    def test_existing_root_link_inside_records_and_traversal_are_rejected(self):
        with self.assertRaises(FileExistsError):
            lab.init(self.root)
        for bad_id in ('../escape', '/escape', 'a/b', 'a\\b', 'CON', 'a.'):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                lab.archive(self.root, self.request(result_id=bad_id))
        with self.assertRaises(ValueError):
            lab.inside(self.root, '..', 'escape')
        with self.assertRaises(ValueError):
            lab.archive(self.root, self.request(report_file='relative.md'))
        self.assertFalse((self.base / 'escape').exists())

    def test_timestamp_disclosure_and_frozen_definition_validation(self):
        invalid = []
        for field in ('generated_at', 'research_asof', 'data_asof'):
            invalid.append(self.request(**{field: '2026-01-02T15:00:00'}))
        invalid.append(self.request(generated_at='2099-01-02T18:00:00+08:00'))
        invalid.append(self.request(data_asof='2026-01-02T17:30:00+08:00'))
        late = self.request()
        late['evidence'][0]['public_at'] = '2026-01-03T09:00:00+08:00'
        invalid.append(late)
        frozen = self.request()
        frozen['items'][0]['frozen_at'] = '2026-01-02T19:00:00+08:00'
        invalid.append(frozen)
        prediction = self.request()
        prediction['items'][0]['type'] = 'prediction'
        invalid.append(prediction)
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                lab.archive(self.root, request)
        self.assertEqual(lab.scan(self.root)['results'], [])

    def test_same_day_versions_preserve_bytes_and_reject_overwrite_and_reused_items(self):
        first = lab.archive(self.root, self.request())
        destination = self.root / first['path']
        original = (destination / 'report.md').read_bytes()
        self.report.write_text('new fictional report', encoding='utf-8')
        second = lab.archive(self.root, self.request('R2', 'I2', parent_result_ids=['R1']))
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(Path(first['path']).parent, Path(second['path']).parent)
        with self.assertRaises(FileExistsError):
            lab.archive(self.root, self.request())
        with self.assertRaises(FileExistsError):
            lab.archive(self.root, self.request('R3', 'I1'))
        self.assertEqual((destination / 'report.md').read_bytes(), original)

    def test_old_expired_items_survive_new_versions_and_mtime_changes(self):
        lab.archive(self.root, self.request())
        newer = self.request('R2', 'I2', parent_result_ids=['R1'],
                             generated_at='2026-02-03T18:00:00+08:00',
                             research_asof='2026-02-03T17:00:00+08:00', data_asof='2026-02-03T16:00:00+08:00')
        newer['items'][0].update(frozen_at=newer['generated_at'], valid_until='2026-03-03T18:00:00+08:00',
                                review_due_at='2026-03-03T18:00:00+08:00')
        lab.archive(self.root, newer)
        for record in lab.scan(self.root)['results']:
            os.utime(self.root / record['path'], None)
        view = self.view()
        self.assertEqual({item['item_id'] for item in view['pending_items']}, {'I1', 'I2'})
        results = {result['result_id']: result for result in view['results']}
        self.assertFalse(results['R1']['current_eligible'])
        self.assertIn('validity_window_ended', results['R1']['reasons'])
        self.assertTrue(results['R1']['history_usable'])
        self.assertTrue(results['R2']['current_eligible'])
        earlier = self.view('2026-01-03T18:00:00+08:00')
        self.assertEqual([result['result_id'] for result in earlier['results']], ['R1'])
        self.assertEqual(earlier['excluded_results'], [{'result_id': 'R2', 'reasons': ['not_available_at_asof']}])
        self.assertEqual({item['item_id'] for item in earlier['pending_items']}, {'I1'})

    def test_append_review_changes_view_but_not_frozen_items_and_cannot_rewrite_definition(self):
        result = lab.archive(self.root, self.request())
        original = (self.root / result['path'] / 'items.json').read_bytes()
        invalid = self.review_request()
        invalid['updates'][0]['definition'] = {'what': 'changed after event'}
        with self.assertRaisesRegex(ValueError, 'frozen'):
            lab.review(self.root, invalid)
        renewal = self.review_request()
        renewal['updates'][0].update(current_use='eligible', current_use_reason='pretend renewed',
                                     valid_until='2026-03-03T18:00:00+08:00')
        with self.assertRaises(ValueError):
            lab.review(self.root, renewal)
        request = self.review_request()
        request['updates'][0]['status'] = 'observed'
        lab.review(self.root, request)
        self.assertEqual((self.root / result['path'] / 'items.json').read_bytes(), original)
        self.assertEqual(self.view()['pending_items'], [])
        self.assertEqual(self.view()['results'][0]['item_states'][0]['status'], 'observed')
        self.assertEqual(len(self.view('2026-01-03T18:00:00+08:00')['pending_items']), 1)
        with self.assertRaises(FileExistsError):
            lab.review(self.root, request)

    def test_import_keeps_original_and_real_import_time_with_unknown_dates(self):
        request = self.request(import_id='OLD1', source_file=str(self.report), research_asof=None, data_asof=None)
        request['items'][0].update(current_use='needs_review')
        original = self.report.read_bytes()
        response = lab.import_report(self.root, request)
        self.assertIn('/undated/', response['path'])
        manifest = lab.read_json(self.root / response['path'] / 'manifest.json')
        self.assertIsNone(manifest['research_asof'])
        self.assertIsNone(manifest['original_generated_at'])
        self.assertNotEqual(manifest['generated_at'], request['generated_at'])
        items = lab.read_json(self.root / response['path'] / 'items.json')['items']
        self.assertEqual(items[0]['frozen_at'], manifest['generated_at'])
        self.assertEqual(items[0]['prospective_status'], 'unverified')
        original_path = self.root / 'securities/ZZ-TEST-DEMO/imports/OLD1/original/report.md'
        self.assertEqual(original_path.read_bytes(), original)
        with self.assertRaises(ValueError):
            lab.import_report(self.root, self.request('R2', 'I2', import_id='OLD2', source_file=str(self.report),
                                                    original_generated_at='2025-01-01T00:00:00+08:00'))

    def test_archive_retains_unknown_data_coverage_and_date_only_publication(self):
        request = self.request(data_asof=None)
        request['evidence'][0].update(public_at=None, public_at_precision='date_only', public_date='2026-01-02')
        response = lab.archive(self.root, request)
        manifest = lab.read_json(self.root / response['path'] / 'manifest.json')
        self.assertIsNone(manifest['data_asof'])
        self.assertIsNone(manifest['evidence'][0]['public_at'])
        self.assertEqual(manifest['evidence'][0]['public_date'], '2026-01-02')
        result = self.view('2026-01-03T18:00:00+08:00')['results'][0]
        self.assertTrue(result['history_usable'])
        self.assertFalse(result['current_eligible'])
        self.assertIn('research_or_data_asof_unknown', result['reasons'])
        self.assertIn('source_publication_time_unverified', result['reasons'])

    def test_review_unknown_data_coverage_cannot_renew_current_eligibility(self):
        lab.archive(self.root, self.request())
        request = self.review_request(data_asof=None)
        request['evidence'] = [{'evidence_id': 'NEW1', 'source': 'fictional new excerpt',
                                'public_at': '2026-02-03T09:00:00+08:00'}]
        request['updates'][0].update(status='supported', evidence_ids=['NEW1'], current_use='eligible',
                                     current_use_reason='new excerpt supports observation',
                                     valid_until='2026-03-03T18:00:00+08:00',
                                     review_due_at='2026-03-03T18:00:00+08:00')
        response = lab.review(self.root, request)
        self.assertIsNone(lab.read_json(self.root / response['path'] / 'review.json')['data_asof'])
        state = self.view()['results'][0]['item_states'][0]
        self.assertIsNone(state['data_asof'])
        self.assertFalse(state['current_eligible'])
        self.assertIn('research_or_data_asof_unknown', state['reasons'])
        future = self.review_request(review_id='RV2', data_asof=None)
        future['evidence'] = [{'evidence_id': 'LATE', 'source': 'later excerpt',
                               'public_at': '2026-02-04T09:00:00+08:00'}]
        with self.assertRaisesRegex(ValueError, 'not public'):
            lab.review(self.root, future)

    def test_catalog_failure_preserves_completed_report_and_reindex_reads_all_ledgers(self):
        with patch.object(lab, 'update_catalog', side_effect=OSError('simulated index failure')):
            response = lab.archive(self.root, self.request())
        self.assertTrue(response['saved'])
        self.assertFalse(response['catalog_updated'])
        self.assertTrue((self.root / response['path'] / 'report.md').is_file())
        lab.review(self.root, self.review_request())
        lab.plan(self.root, self.plan_request())
        lab.execution(self.root, self.execution_request())
        imported = self.request('R2', 'I2', import_id='OLD1', source_file=str(self.report), research_asof=None, data_asof=None)
        imported['items'][0]['current_use'] = 'needs_review'
        lab.import_report(self.root, imported)
        (self.root / 'catalog.json').write_text('{broken cache', encoding='utf-8')
        self.assertEqual(len(self.view()['results']), 1)
        response = lab.reindex(self.root)
        self.assertEqual(response['counts'], {'results': 2, 'reviews': 1, 'plans': 1, 'executions': 1, 'imports': 1})
        self.assertEqual(len(lab.read_json(self.root / 'catalog.json')['results']), 2)

    def test_tampered_original_is_not_silently_reindexed(self):
        response = lab.archive(self.root, self.request())
        original_index = (self.root / 'catalog.json').read_bytes()
        (self.root / response['path'] / 'report.md').write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            lab.reindex(self.root)
        self.assertEqual((self.root / 'catalog.json').read_bytes(), original_index)

    def test_plan_versions_explicit_allocation_real_simulation_and_trade_month(self):
        lab.archive(self.root, self.request())
        lab.plan(self.root, self.plan_request())
        lab.plan(self.root, self.plan_request(plan_version_id='PV2', version=2, previous_plan_version_id='PV1',
                                           effective_at='2026-01-04T09:00:00+08:00'))
        with self.assertRaises(ValueError):
            lab.plan(self.root, self.plan_request(plan_version_id='PV3', version=3))
        request = self.execution_request()
        request['trades'][0]['time_precision'] = 'day'
        response = lab.execution(self.root, request)
        self.assertIn('/executions/2026-01/', response['path'])
        trade = lab.read_json(self.root / response['path'] / 'trades.json')['trades'][0]
        self.assertEqual(trade['allocations'][0]['plan_version_id'], 'PV1')
        self.assertIn('execution_version_time_needs_review', trade['version_review_reasons'])
        self.assertEqual(trade['unallocated_quantity'], '5')
        self.assertIsNone(trade['fees'])
        simulated = self.execution_request(import_id='SIM1', execution_kind='simulated')
        simulated['trades'][0]['trade_id'] = 'ST1'
        simulated['trades'][0]['allocations'] = []
        lab.execution(self.root, simulated)
        self.assertEqual({record['execution_kind'] for record in self.view()['executions']}, {'real', 'simulated'})
        self.assertFalse(self.view()['pnl_computed'])

    def test_execution_rejects_quantity_future_duplicate_and_mixed_month(self):
        lab.archive(self.root, self.request())
        lab.plan(self.root, self.plan_request())
        invalid = []
        over = self.execution_request()
        over['trades'][0]['allocations'][0]['quantity'] = '11'
        invalid.append(over)
        future = self.execution_request()
        future['trades'][0]['executed_at'] = '2026-03-01T10:00:00+08:00'
        invalid.append(future)
        no_version = self.execution_request()
        del no_version['trades'][0]['allocations'][0]['plan_version_id']
        invalid.append(no_version)
        cross_month = self.execution_request()
        second = copy.deepcopy(cross_month['trades'][0])
        second.update(trade_id='T2', executed_at='2026-02-01T10:00:00+08:00')
        cross_month['trades'].append(second)
        invalid.append(cross_month)
        for request in invalid:
            with self.assertRaises(ValueError):
                lab.execution(self.root, request)
        original = self.execution_request()
        lab.execution(self.root, original)
        with self.assertRaises(FileExistsError):
            lab.execution(self.root, self.execution_request(import_id='X2'))
        repeated = self.execution_request(import_id='X2')
        repeated['trades'][0]['trade_id'] = 'T2'
        with self.assertRaisesRegex(FileExistsError, 'already imported'):
            lab.execution(self.root, repeated)
        correction = self.execution_request(import_id='X3')
        correction['trades'][0].update(trade_id='T3', correction_of_trade_id='T1', correction_reason='user corrected price', price='3')
        lab.execution(self.root, correction)
        self.assertEqual(len(lab.scan(self.root)['executions']), 2)

    def test_execution_unmatched_and_nontrade_feedback_remain_separate(self):
        lab.archive(self.root, self.request())
        request = self.execution_request()
        request['trades'][0]['allocations'] = []
        response = lab.execution(self.root, request)
        trade = lab.read_json(self.root / response['path'] / 'trades.json')['trades'][0]
        self.assertEqual(trade['allocations'], [])
        self.assertEqual(trade['unallocated_quantity'], '10')
        with self.assertRaisesRegex(ValueError, 'requires trades'):
            lab.execution(self.root, self.execution_request(import_id='EMPTY', trades=[]))
        review = self.review_request()
        review['updates'][0].update(status='triggered_pending', reason='Triggered; execution unknown; no user feedback')
        lab.review(self.root, review)
        self.assertEqual(len(self.view()['executions']), 1)
        self.assertFalse(self.view()['pnl_computed'])
        invalid_review = self.review_request(review_id='RV2')
        invalid_review['updates'][0]['issues'] = ['execution']
        with self.assertRaisesRegex(ValueError, 'cannot overwrite'):
            lab.review(self.root, invalid_review)

    def test_execution_allocations_require_objects_and_do_not_overallocate_across_plans(self):
        lab.archive(self.root, self.request())
        lab.plan(self.root, self.plan_request())
        lab.plan(self.root, self.plan_request(plan_id='P2', plan_version_id='PV2'))
        invalid = [[1], [{'plan_id': 'P1', 'plan_version_id': 'PV1', 'quantity': '3'}] * 2,
                   [{'plan_id': 'P1', 'plan_version_id': 'PV1', 'quantity': '6'},
                    {'plan_id': 'P2', 'plan_version_id': 'PV2', 'quantity': '5'}]]
        for allocations in invalid:
            request = self.execution_request()
            request['trades'][0]['allocations'] = allocations
            with self.subTest(allocations=allocations), self.assertRaises(ValueError):
                lab.execution(self.root, request)
        request = self.execution_request()
        request['trades'][0]['allocations'] = [{'plan_id': 'P1', 'plan_version_id': 'PV1', 'quantity': '6'},
                                              {'plan_id': 'P2', 'plan_version_id': 'PV2', 'quantity': '4'}]
        response = lab.execution(self.root, request)
        self.assertEqual(lab.read_json(self.root / response['path'] / 'trades.json')['trades'][0]['unallocated_quantity'], '0')

    def test_correction_chains_are_visible_only_when_known_and_replace_original_facts(self):
        lab.archive(self.root, self.request())
        original = self.execution_request(generated_at='2026-01-04T18:00:00+08:00')
        original['trades'][0]['allocations'] = []
        lab.execution(self.root, original)
        correction = copy.deepcopy(original)
        correction.update(import_id='X2', generated_at='2026-01-03T18:00:00+08:00')
        correction['trades'][0].update(trade_id='T2', correction_of_trade_id='T1', correction_reason='correct price', price='3')
        with self.assertRaisesRegex(ValueError, 'Correction must cite'):
            lab.execution(self.root, correction)
        correction['generated_at'] = '2026-01-05T18:00:00+08:00'
        correction['execution_kind'] = 'simulated'
        with self.assertRaisesRegex(ValueError, 'Correction must cite'):
            lab.execution(self.root, correction)
        correction['execution_kind'] = 'real'
        lab.execution(self.root, correction)
        last = copy.deepcopy(correction)
        last.update(import_id='X3', generated_at='2026-01-06T18:00:00+08:00')
        last['trades'][0].update(trade_id='T3', correction_of_trade_id='T2', price='4')
        lab.execution(self.root, last)
        self.assertEqual(self.view('2026-01-04T19:00:00+08:00')['superseded_trade_ids'], [])
        self.assertEqual(self.view('2026-01-05T19:00:00+08:00')['superseded_trade_ids'], ['T1'])
        view = self.view('2026-01-06T19:00:00+08:00')
        self.assertEqual(view['superseded_trade_ids'], ['T1', 'T2'])
        facts = [trade for record in view['executions'] for trade in record['trades']
                 if trade['trade_id'] not in view['superseded_trade_ids']]
        self.assertEqual([(trade['trade_id'], trade['price']) for trade in facts], [('T3', '4')])
        self.assertEqual(len(view['executions']), 3)
        self.assertFalse(view['pnl_computed'])
        repeat = copy.deepcopy(last)
        repeat['import_id'] = 'X4'
        repeat['trades'][0].update(trade_id='T4', correction_of_trade_id='T1')
        with self.assertRaisesRegex(ValueError, 'Correction must cite'):
            lab.execution(self.root, repeat)

    def test_different_sources_are_not_assumed_to_prove_unique_real_world_trades(self):
        lab.archive(self.root, self.request())
        request = self.execution_request()
        request['trades'][0]['allocations'] = []
        lab.execution(self.root, request)
        another_source = self.base / 'another-feedback.txt'
        another_source.write_text('A separately supplied statement; identity requires user review', encoding='utf-8')
        request.update(import_id='X2', source_file=str(another_source))
        request['trades'][0]['trade_id'] = 'T2'
        lab.execution(self.root, request)
        view = self.view()
        self.assertEqual(len(view['executions']), 2)
        self.assertEqual(view['superseded_trade_ids'], [])
        self.assertFalse(view['pnl_computed'])

    def test_backdated_plan_effective_date_cannot_claim_available_at_execution(self):
        lab.archive(self.root, self.request())
        lab.plan(self.root, self.plan_request())
        lab.plan(self.root, self.plan_request(plan_version_id='PV2', version=2, previous_plan_version_id='PV1',
                                           generated_at='2026-01-06T18:00:00+08:00',
                                           frozen_at='2026-01-06T18:00:00+08:00',
                                           effective_at='2026-01-03T09:00:00+08:00'))
        request = self.execution_request()
        request['trades'][0]['allocations'][0]['plan_version_id'] = 'PV2'
        response = lab.execution(self.root, request)
        trade = lab.read_json(self.root / response['path'] / 'trades.json')['trades'][0]
        self.assertIn('version_not_available_at_execution', trade['version_review_reasons'])
        self.assertEqual(trade['allocations'][0]['plan_version_id'], 'PV2')

    def test_late_written_historical_review_cannot_supersede_later_research_cutoff(self):
        lab.archive(self.root, self.request())
        later_cutoff = self.review_request()
        later_cutoff['updates'][0]['status'] = 'invalidated'
        lab.review(self.root, later_cutoff)
        older_cutoff = self.review_request(review_id='RV2', generated_at='2026-02-04T18:00:00+08:00',
                                           research_asof='2026-02-02T17:00:00+08:00',
                                           data_asof='2026-02-02T16:00:00+08:00')
        older_cutoff['updates'][0]['status'] = 'supported'
        lab.review(self.root, older_cutoff)
        view = self.view('2026-02-05T18:00:00+08:00')
        self.assertEqual(view['results'][0]['item_states'][0]['status'], 'invalidated')
        self.assertEqual(view['results'][0]['item_states'][0]['applied_review_ids'], ['RV2', 'RV1'])
        self.assertEqual(view['pending_items'], [])

    def test_same_feedback_source_can_be_imported_for_two_explicit_securities(self):
        lab.archive(self.root, self.request())
        lab.plan(self.root, self.plan_request())
        lab.execution(self.root, self.execution_request())
        second = self.request('R2', 'I2')
        second['security'].update(security_id='ZZ-TEST-OTHER', code='OTHER', company='Other Fictional Company')
        lab.archive(self.root, second)
        request = self.execution_request(import_id='X2', security_id='ZZ-TEST-OTHER')
        request['trades'][0].update(trade_id='OT1', allocations=[])
        lab.execution(self.root, request)
        self.assertEqual(len(lab.scan(self.root)['executions']), 2)
        same_security = self.execution_request(import_id='X3', security_id='ZZ-TEST-OTHER')
        same_security['trades'][0].update(trade_id='OT2', allocations=[])
        with self.assertRaisesRegex(FileExistsError, 'already imported'):
            lab.execution(self.root, same_security)

    def test_partial_import_preserves_original_and_retry_resumes_without_overwrite(self):
        request = self.request(import_id='OLD1', source_file=str(self.report), research_asof=None, data_asof=None)
        request['items'][0]['current_use'] = 'needs_review'
        move = lab.move_record
        calls = []

        def interrupt_second_move(*args):
            calls.append(args)
            if len(calls) == 2:
                raise OSError('simulated adaptation commit interruption')
            return move(*args)

        with patch.object(lab, 'move_record', side_effect=interrupt_second_move):
            response = lab.import_report(self.root, request)
        self.assertTrue(response['saved'])
        self.assertTrue(response['partial'])
        self.assertFalse(response['result_saved'])
        self.assertEqual(lab.scan(self.root)['results'], [])
        original = self.root / 'securities/ZZ-TEST-DEMO/imports/OLD1/original/report.md'
        original_bytes = original.read_bytes()
        registration_bytes = (original.parent.parent / 'import.json').read_bytes()
        wrong = copy.deepcopy(request)
        wrong['result_id'] = 'DIFFERENT'
        with self.assertRaisesRegex(ValueError, 'metadata differs'):
            lab.import_report(self.root, wrong)
        response = lab.import_report(self.root, request)
        self.assertTrue(response['resumed'])
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual((original.parent.parent / 'import.json').read_bytes(), registration_bytes)
        self.assertEqual(len(lab.scan(self.root)['results']), 1)
        with self.assertRaises(FileExistsError):
            lab.import_report(self.root, request)

    def test_two_processes_cannot_overwrite_one_result(self):
        request_path = self.base / 'request.json'
        request_path.write_text(json.dumps(self.request()), encoding='utf-8')
        command = [sys.executable, '-X', 'utf8', '-B', str(SCRIPT), 'archive', str(self.root), '--request-json', str(request_path)]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
                     for _ in range(2)]
        completed = [process.communicate(timeout=30) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 2], completed)
        self.assertEqual(len(lab.scan(self.root)['results']), 1)
        self.assertFalse((self.root / '.write.lock').exists())

    def test_interrupted_stage_is_not_a_completed_result_and_lock_does_not_expire(self):
        with patch.object(lab, 'move_record', side_effect=OSError('interrupted before commit')):
            with self.assertRaises(OSError):
                lab.archive(self.root, self.request())
        self.assertEqual(lab.scan(self.root)['results'], [])
        self.assertTrue(any((self.root / '_staging').iterdir()))
        lock = self.root / '.write.lock'
        lock.write_text('unresolved prior writer', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            lab.archive(self.root, self.request())
        self.assertEqual(lock.read_text(encoding='utf-8'), 'unresolved prior writer')


if __name__ == '__main__':
    unittest.main()
