from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'skills/stock-technical-analysis/scripts/harmonic_analysis.py'
spec = importlib.util.spec_from_file_location('harmonic_analysis', SCRIPT)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def request():
    return {'security': {'security_id': 'TEST.SZ', 'market': 'A-share', 'exchange': 'sz',
                          'code': 'TEST', 'currency': 'CNY'},
            'timeframe': '1d', 'asof': '2026-01-04T23:59:59.999999+08:00',
            'snapshot_available_at': '2026-01-03T23:59:59+08:00',
            'bar_time_semantics': 'period_label_date',
            'completion_evidence': '合成的完整日线', 'session_evidence': '合成，无真实会话含义',
            'adjustment': {'mode': 'raw', 'verified': False, 'evidence': '教学数据，无复权证据'},
            'source': 'unit test synthetic', 'data_kind': 'synthetic', 'input_file': 'bars.json'}


def rows():
    return [{'time': f'2026-01-0{i+1}', 'open': p, 'high': p+1, 'low': p-1, 'close': p}
            for i,p in enumerate((100, 120, 110, 119))]


class HarmonicAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'bars.json').write_text(json.dumps(rows()), encoding='utf-8')
        self.path = self.root/'request.json'
        self.path.write_text(json.dumps(request()), encoding='utf-8')

    def test_asof_excludes_future_and_unfinished_before_core(self):
        items = rows()
        items[1]['completed'] = False
        items[-1]['close'] = 100000
        with patch.object(app, 'read_rows', return_value=items):
            req = request()
            req['asof'] = '2026-01-03T23:59:59.999999+08:00'
            bars, meta = app.prepare(req, self.path)
        self.assertEqual(len(bars), 2)
        self.assertEqual(meta['excluded_bars'], 2)
        self.assertEqual(bars[-1]['close'], 110)

    def test_date_precision_does_not_invent_close_availability(self):
        req = request()
        req['asof'] = '2026-01-04T15:30:00+08:00'
        bars, meta = app.prepare(req, self.path)
        self.assertEqual(len(bars), 3)
        self.assertFalse(meta['geometry_price_basis_qualified'])
        self.assertIn('not_actual_close', meta['timestamp_policy'])

    def test_minute_input_requires_timestamp_with_timezone(self):
        req = request()
        req['timeframe'] = '5m'
        with self.assertRaisesRegex(ValueError, '分钟K线'):
            app.prepare(req, self.path)

    def test_minute_report_entry_pauses_before_reading_input_or_creating_output(self):
        req=request();req['timeframe']='5m'
        self.path.write_text(json.dumps(req),encoding='utf-8')
        with patch.object(app,'prepare',side_effect=AssertionError('paused input must not be read')):
            with self.assertRaisesRegex(ValueError,'分钟级分析已暂停'):
                app.run(self.path,self.root/'output')
        self.assertFalse((self.root/'output').exists())

    def test_half_year_and_year_date_labels_are_parsed_as_long_periods(self):
        for frame in ('6mo','1y'):
            req=request();req['timeframe']=frame
            bars,meta=app.prepare(req,self.path)
            self.assertEqual(len(bars),4)
            self.assertEqual(meta['timeframe'],frame)
            self.assertEqual(meta['period_qualification'],'diagnostic_not_calibrated')
        with self.assertRaisesRegex(ValueError, '时区'):
            app.moment('2026-01-02T09:35:00')

    def test_close_only_csv_is_rejected(self):
        path = self.root/'close.csv'
        path.write_text('time,close\n2026-01-01,12\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '完整OHLC'):
            app.read_rows(path, 'utf-8')

    def test_csv_keeps_availability_and_completed_and_normalizes_timezone(self):
        path = self.root/'gated.csv'
        path.write_text('time,open,high,low,close,completed,available_at\n'
                        '2026-01-01T01:35:00+00:00,10,11,9,10,true,2026-01-02T00:00:00+08:00\n'
                        '2026-01-02T09:40:00+08:00,11,12,10,11,false,2026-01-02T10:00:00+08:00\n'
                        '2026-01-03T09:45:00+08:00,12,13,11,12,true,2026-01-06T00:00:00+08:00\n', encoding='utf-8')
        req = request()
        req.update(input_file='gated.csv', timeframe='5m', bar_time_semantics='bar_end_time')
        bars, meta = app.prepare(req, self.path)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]['time'], '2026-01-01T09:35:00+08:00')
        self.assertEqual(meta['excluded_bars'], 2)
        self.assertEqual(meta['period_qualification'], 'diagnostic_not_calibrated')

    def test_chinese_csv_with_comment_and_identity(self):
        path = self.root/'bars.csv'
        path.write_text('来源说明\n股票代码,交易日期,开盘价,最高价,最低价,收盘价\nszTEST,2026-01-01,10,11,9,10\n', encoding='gb18030')
        req = request()
        req.update(input_file='bars.csv', encoding='gb18030')
        bars, meta = app.prepare(req, self.path)
        self.assertEqual(bars[0]['high'], 11)
        path.write_text(path.read_text(encoding='gb18030').replace('szTEST', 'shOTHER'), encoding='gb18030')
        with self.assertRaisesRegex(ValueError, '代码'):
            app.prepare(req, self.path)

    def test_bad_ohlc_duplicate_time_and_nan_rejected(self):
        for key, value in [('time', '2026-01-01'), ('low', 130), ('close', float('nan'))]:
            items = rows()
            items[1][key] = value
            with self.subTest(key=key), patch.object(app, 'read_rows', return_value=items):
                with self.assertRaises(ValueError):
                    app.prepare(request(), self.path)

    def test_metadata_must_declare_market_source_and_adjustment(self):
        for key, value in [('source', ''), ('adjustment', {'mode': 'qfq', 'verified': True}),
                           ('security', {'market': 'US'})]:
            req = request()
            req[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                app.prepare(req, self.path)

    def test_later_snapshot_is_rejected_and_row_availability_is_filtered(self):
        req = request()
        req['snapshot_available_at'] = '2026-01-05T00:00:00+08:00'
        with self.assertRaisesRegex(ValueError, '回填'):
            app.prepare(req, self.path)
        items = rows()
        items[-1]['available_at'] = '2026-01-05T00:00:00+08:00'
        with patch.object(app, 'read_rows', return_value=items):
            bars, meta = app.prepare(request(), self.path)
        self.assertEqual(len(bars), 3)

    def test_bool_price_and_bad_object_are_rejected(self):
        items = rows()
        items[0]['open'] = True
        with patch.object(app, 'read_rows', return_value=items), self.assertRaises(ValueError):
            app.prepare(request(), self.path)
        req = request()
        req['security'] = []
        with self.assertRaises(ValueError):
            app.prepare(req, self.path)

    def test_output_inside_source_or_git_or_existing_is_refused(self):
        for path in (SCRIPT.parents[3]/'research-private', self.root):
            with self.assertRaises(ValueError):
                app.safe_output(path)
        (self.root/'.git').mkdir()
        with self.assertRaisesRegex(ValueError, 'Git'):
            app.safe_output(self.root/'new')
        installed = self.root/'standalone'/'stock-technical-analysis'/'scripts'/'harmonic_analysis.py'
        with patch.object(app, '__file__', str(installed)):
            with self.assertRaisesRegex(ValueError, 'Skill'):
                app.safe_output(installed.parents[1]/'private-output')
            allowed = self.root.parent/(self.root.name+'-allowed-private-output')
            self.assertEqual(app.safe_output(allowed), allowed)

    def test_failed_render_leaves_no_partial_report(self):
        with patch.object(app, 'render_charts', side_effect=RuntimeError('render failed')):
            with self.assertRaises(RuntimeError):
                app.run(self.path, self.root/'new')
        self.assertFalse((self.root/'new').exists())
        self.assertFalse(list(self.root.glob('.harmonic-*')))

    def test_dangling_link_to_git_output_is_refused(self):
        gitroot = self.root/'other'
        gitroot.mkdir()
        (gitroot/'.git').mkdir()
        link = self.root/'link'
        try:
            link.symlink_to(gitroot/'not-yet-created', target_is_directory=True)
        except OSError:
            self.skipTest('当前Windows会话无符号链接权限')
        with self.assertRaises(ValueError):
            app.safe_output(link/'report')

    def test_snapshot_keeps_bars_hash_profile_and_does_not_overwrite(self):
        def render(bars, result, meta, out):
            (out/'chart.png').write_bytes(b'synthetic-render-placeholder')
            (out/'chart.html').write_text('<html>test</html>', encoding='utf-8')
        with patch.object(app, 'render_charts', side_effect=render):
            path = app.run(self.path, self.root/'new')
            with self.assertRaisesRegex(ValueError, '已存在'):
                app.run(self.path, self.root/'new')
        doc = json.loads((path/'analysis.json').read_text(encoding='utf-8'))
        self.assertEqual(len(doc['bars']), 4)
        self.assertEqual(len(doc['input_metadata']['input_sha256']), 64)
        self.assertIn('generated_at', doc['input_metadata'])
        self.assertEqual(doc['analysis']['metadata']['profile']['version'], '1.0.0')
        self.assertEqual(set(p.name for p in path.iterdir()), {'analysis.json','report.md','chart.png','chart.html'})
        self.assertIn('复权', (path/'report.md').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
