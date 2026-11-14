"""Fictional source-only report cache boundaries; no public URLs are requested."""
import hashlib
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.broker_cache import (_public_pdf_url, fetch_report, latest_check, refresh_index,
                              retain_pdf, source_detail, source_file, sources, _publication, enrich_updates)
from app.store import ResearchStore
from app.analyst_sources import FIELDS, analyst_updates
from test_platform_server import lab, TOOL, IDENTITY

URL = 'https://pdf.dfcfw.com/pdf/H3_AP20260105123456_1.pdf'
PAYLOAD = b'%PDF-1.7 fictional example, not a real paper'
RECORD = {'source_id':'index-' + 'a' * 64, 'row_sha256':'b' * 64,
          'security_id':IDENTITY['security_id'], 'title':'虚构研究标题', 'organization':'虚构机构',
          'authors':'虚构作者', 'publication_date':'2026-01-05', 'publication_date_precision':'date'}
PAGES = [{'page':1,'text':'DEMO 虚构研究标题 2026年1月5日 这不是实际研报。'}]


class BrokerCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / 'private'
        lab.init(self.root)
        report = base / 'report.md'
        report.write_text('# 虚构正式研究', encoding='utf-8')
        lab.archive(self.root, {'security':IDENTITY, 'module':'fundamental','module_version':'fixture',
            'result_id':'FORMAL','generated_at':'2026-01-02T18:00:00+08:00',
            'research_asof':'2026-01-02T17:00:00+08:00','data_asof':None,
            'parent_result_ids':[],'report_file':str(report),'evidence':[],'items':[]})
        self.store = ResearchStore(self.root, TOOL)
        self.context = self.store.context(IDENTITY['security_id'])
        self.original_catalog = (self.root / 'catalog.json').read_bytes()

    def retain(self, **kwargs):
        with patch('app.broker_cache._extract', return_value=(PAGES, 'text_extracted', '虚构页')):
            return retain_pdf(self.store, IDENTITY['security_id'], RECORD, PAYLOAD, URL,
                              context=self.context, **kwargs)

    def test_public_destination_allowlist_and_private_dns_are_rejected(self):
        self.assertEqual(_public_pdf_url(URL), URL)
        for url in ('http://pdf.dfcfw.com/pdf/H3_AP123_1.pdf', 'https://127.0.0.1/report.pdf',
                    URL + '?token=secret', URL.replace('pdf.dfcfw.com', 'pdf.dfcfw.com.evil'),
                    URL.replace('/pdf/', '/private/'), URL.replace('https://', 'https://user:pass@')):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _public_pdf_url(url)
        with patch('app.broker_cache.socket.getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):
                _public_pdf_url(URL, resolve=True)

    def test_primary_publication_preserved_and_no_formal_result_is_created(self):
        result = self.retain(views=[{'text':'虚构待核观点','page':1}])
        self.assertEqual(result['publication_date'], '2026-01-05')
        self.assertNotEqual(result['acquired_at'][:10], result['publication_date'])
        self.assertFalse(result['formal_archive'])
        self.assertFalse(result['can_replace_formal'])
        self.assertEqual(source_file(self.store, IDENTITY['security_id'], result['source_id']), PAYLOAD)
        self.assertEqual(len(self.store.context(IDENTITY['security_id'])['results']), 1)
        self.assertEqual((self.root / 'catalog.json').read_bytes(), self.original_catalog)

    def test_repeated_source_is_idempotent_and_old_original_bytes_remain(self):
        first, second = self.retain(), self.retain()
        self.assertEqual(first['source_id'], second['source_id'])
        self.assertEqual(first['cached_at'], second['cached_at'])
        self.assertEqual(len(sources(self.store, IDENTITY['security_id'])), 1)
        projection = {'security':IDENTITY,'records':[dict(RECORD,content_status='metadata_only')]}
        result = enrich_updates(self.store, projection, context=self.context)
        self.assertEqual(result['records'][0]['cached_source_id'], first['source_id'])
        self.assertEqual(result['records'][0]['source_id'], RECORD['source_id'])
        self.assertNotEqual(result['records'][0]['content_status'], 'metadata_only')

    def test_tampered_pdf_or_role_and_invalid_paths_fail_closed(self):
        item = self.retain()
        folder = self.root / '_workspace/platform/broker-cache' / IDENTITY['security_id'] / item['source_id']
        (folder / 'original.pdf').write_bytes(b'%PDF-tampered')
        with self.assertRaises(ValueError):
            source_detail(self.store, IDENTITY['security_id'], item['source_id'])
        for source in ('../../catalog.json', 'broker-123'):
            with self.assertRaises(ValueError):
                source_file(self.store, IDENTITY['security_id'], source)

    def test_unknown_date_is_not_filled_and_future_date_is_rejected(self):
        _publication('2026-01-05','date','2026-01-04T20:00:00+00:00')
        record = {**RECORD, 'publication_date':None, 'publication_date_precision':'unknown'}
        with patch('app.broker_cache._extract', return_value=(PAGES, 'text_extracted', '虚构页')):
            result = retain_pdf(self.store, IDENTITY['security_id'], record, PAYLOAD, URL, context=self.context)
        self.assertIsNone(result['publication_date'])
        with self.assertRaises(ValueError):
            retain_pdf(self.store, IDENTITY['security_id'], {**RECORD,'publication_date':'2099-01-01'}, PAYLOAD, URL)

    def test_cached_metadata_cannot_promote_unread_claims_or_reverse_acquisition_time(self):
        item = self.retain(views=[{'page':1,'text':'虚构原页观点'}])
        path = self.root / '_workspace/platform/broker-cache' / IDENTITY['security_id'] / item['source_id'] / 'source.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        for changed in ({'content_status':'needs_review'}, {'formal_archive':True},
                        {'cached_at':'2020-01-01T00:00:00+08:00'}, {'publication_precision':'timestamp'}):
            with self.subTest(changed=changed):
                path.write_text(json.dumps({**original,**changed}),encoding='utf-8')
                with self.assertRaises(ValueError):
                    source_detail(self.store, IDENTITY['security_id'], item['source_id'])

    def test_unknown_or_unread_source_cannot_provide_claims(self):
        with patch('app.broker_cache._extract', return_value=([], 'needs_ocr', '无文字层')):
            with self.assertRaises(ValueError):
                retain_pdf(self.store, IDENTITY['security_id'], RECORD, PAYLOAD, URL,
                           views=[{'page':1,'text':'不能伪造观点'}])
        with self.assertRaises(ValueError):
            self.retain(forecasts=[{'fiscal_year':2027,'metric':'EPS','unit':'CNY/share','value':2,'page':9}])
        for invalid in ({'fiscal_year':True,'value':2}, {'fiscal_year':2027,'value':True},
                        {'fiscal_year':2027,'value':float('nan')}, {'fiscal_year':2027}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.retain(forecasts=[{'metric':'EPS','unit':'CNY/share','page':1, **invalid}])

    def test_actual_index_projection_contract_flows_into_pdf_fetch(self):
        security = {**IDENTITY,'security_id':'A-share-sz-300001','company':'虚构研究样本',
                    'market':'A-share','exchange':'sz','code':'300001','share_class':'A','currency':'CNY'}
        lab.ensure_security(self.root, security)
        directory = self.root.parent / 'analysts'
        directory.mkdir()
        self.store.analyst_data_dir = directory
        with (directory / 'sz300001.csv').open('w',encoding='utf-8',newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(FIELDS)
            writer.writerow(['sz300001','2026-01-05','虚构机构','虚构作者','公司研究','虚构研究标题','观察','维持','--','',''])
        record = analyst_updates(self.store, security['security_id'], directory)['records'][0]
        pages = [{'page':1,'text':'300001 虚构研究标题 2026年1月5日'}]
        with patch('app.broker_cache._download',return_value=PAYLOAD), \
             patch('app.broker_cache._extract',return_value=(pages,'text_extracted','虚构页')):
            item = fetch_report(self.store, security['security_id'], record['source_id'], URL)
        self.assertEqual(item['publication_date'], '2026-01-05')
        self.assertEqual(item['publication_date_precision'], 'date')
        self.assertFalse(item['can_replace_formal'])

    def test_manual_date_difference_is_explicit_and_bound_to_pdf(self):
        review = {'index_source_id':RECORD['source_id'], 'pdf_sha256':hashlib.sha256(PAYLOAD).hexdigest(),
                  'original_report_date':'2026-01-04', 'evidence_pages':[1],
                  'reason':'封面落款与分发日不同，虚构人工核验', 'date_note':'分别保留，不猜公开小时'}
        result = self.retain(review=review, views=[{'page':1,'text':'虚构原页观点'}])
        self.assertEqual(result['original_report_date'], '2026-01-04')
        self.assertEqual(result['publication_date'], '2026-01-05')
        self.assertEqual(result['content_status'], 'text_reviewed')
        with self.assertRaises(ValueError):
            self.retain(review={**review,'pdf_sha256':'0' * 64})

    def test_explicit_fetch_requires_selected_index_identity(self):
        index = {'records':[RECORD]}
        with patch('app.analyst_sources.analyst_updates', return_value=index), patch('app.broker_cache._download') as download:
            with self.assertRaises(ValueError):
                fetch_report(self.store, IDENTITY['security_id'], 'index-'+'c'*64, URL)
            download.assert_not_called()

    def test_refresh_baseline_and_increment_are_not_formal_research_updates(self):
        index = {'security':IDENTITY,'status':'ok','records':[dict(RECORD)],'source':{'sha256':'a'*64}}
        with patch('app.analyst_sources.analyst_updates', return_value=index):
            first = refresh_index(self.store, IDENTITY['security_id'])
            self.assertEqual(first['check_status']['state'], 'baseline')
            self.assertEqual(first['check_status']['new_count'], 0)
            second = refresh_index(self.store, IDENTITY['security_id'])
            self.assertEqual(second['check_status']['new_count'], 0)
            index['records'].append({**RECORD,'source_id':'index-'+'d'*64})
            third = refresh_index(self.store, IDENTITY['security_id'])
            self.assertEqual(third['check_status']['new_count'], 1)
        self.assertEqual(latest_check(self.store, IDENTITY['security_id'])['new_count'], 1)
        self.assertEqual((self.root / 'catalog.json').read_bytes(), self.original_catalog)


if __name__ == '__main__':
    unittest.main()
