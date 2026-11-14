import http.client
import importlib.util
import json
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.report_view import render_markdown
from app.server import PlatformServer
from app.store import ResearchStore
from app import PHASE

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "skills/stock-research-lab/scripts/research_lab.py"
spec = importlib.util.spec_from_file_location("p1_http_fixture_lab", TOOL)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
IDENTITY = {"security_id": "ZZ-TEST-DEMO", "company": "虚构流程公司", "market": "ZZ", "exchange": "TEST",
            "code": "DEMO", "share_class": "ordinary", "currency": "TST"}


class PlatformHTTPTests(unittest.TestCase):
    def test_old_item_batch_limit_reaches_page_without_starting_a_task(self):
        from app.research_materials import ResearchSelectionRequired
        headers = {'Origin':self.server.origin, 'X-CSRF-Token':self.server.csrf, 'Content-Type':'application/json'}
        message = '基本面有51个待回顾项，请从原关注项选择最多50项分批研究。'
        with patch.object(self.server.research, 'start', side_effect=ResearchSelectionRequired(message)):
            status, _, data = self.request('POST', '/api/research-start', '{}', headers)
        self.assertEqual(status, 400)
        self.assertIn(message, json.loads(data)['error'])
        self.assertEqual(self.server.research.list()['tasks'], [])

    def test_s5_comparison_is_authenticated_strict_and_read_only(self):
        route='/api/comparison?security_ids=ZZ-TEST-A,ZZ-TEST-B&asof=2026-11-13&period_basis=quarterly&financial_period=2026-09-30&price_window=20'
        with patch.object(self.store,'comparison',return_value={'schema_version':'stock-research-comparison/1','research_executed':False}) as call:
            self.assertEqual(self.request('GET',route,authenticated=False)[0],401)
            self.assertEqual(self.request('GET',route+'&path=other')[0],400)
            self.assertEqual(self.request('GET',route+'&asof=2026-11-12')[0],400)
            self.assertEqual(self.request('GET','/api/comparison')[0],400)
            status,_,data=self.request('GET',route);self.assertEqual(status,200)
            self.assertFalse(json.loads(data)['research_executed'])
            call.assert_called_once_with(['ZZ-TEST-A','ZZ-TEST-B'],asof='2026-11-13',period_basis='quarterly',financial_period='2026-09-30',price_window=20)
        headers={'Origin':self.server.origin,'X-CSRF-Token':self.server.csrf,'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/comparison','{}',headers)[0],404)

    def test_s3_routes_keep_auth_parameter_and_write_guards(self):
        sid = IDENTITY['security_id']
        with patch.object(self.store, 'continuity_dashboard', return_value={'security_id':sid}) as call:
            route = '/api/continuity?security_id=' + sid
            self.assertEqual(self.request('GET', route, authenticated=False)[0], 401)
            self.assertEqual(self.request('GET', route + '&path=other')[0], 400)
            self.assertEqual(self.request('GET', route)[0], 200)
            self.assertEqual(call.call_count, 1)
        headers = {'Origin':self.server.origin, 'X-CSRF-Token':self.server.csrf, 'Content-Type':'application/json'}
        request = {'security_id':sid, 'source_id':'broker-'+'a'*32}
        with patch('app.broker_archive.archive_source', return_value={'status':'archived'}) as call:
            self.assertEqual(self.request('POST', '/api/analyst-archive', json.dumps(request), {**headers,'Origin':'https://evil.example'})[0], 403)
            self.assertEqual(self.request('POST', '/api/analyst-archive', json.dumps({**request,'path':'other'}), headers)[0], 400)
            self.assertEqual(self.request('POST', '/api/analyst-archive', json.dumps(request), headers)[0], 200)
            self.assertEqual(call.call_count, 1)
        with patch('app.continuity_actions.save_review', side_effect=FileExistsError('stale')):
            self.assertEqual(self.request('POST', '/api/continuity-review', '{}', headers)[0], 409)

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "private"
        lab.init(self.root)
        report = base / "source.md"
        report.write_text("# P1虚构报告\n\n<script>alert(1)</script>\n\n[危险](javascript:alert)\n\n|指标|状态|\n|---|---|\n|覆盖|虚构|\n", encoding="utf-8")
        lab.archive(self.root, {"security": IDENTITY, "module": "fundamental", "module_version": "fixture",
            "result_id": "R1", "generated_at": "2026-01-02T18:00:00+08:00", "research_asof": "2026-01-02T17:00:00+08:00",
            "data_asof": None, "parent_result_ids": [], "report_file": str(report), "evidence": [], "items": []})
        self.store = ResearchStore(self.root, TOOL)
        self.server = PlatformServer(("127.0.0.1", 0), self.store, base)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.cookie = "p1_session=" + self.server.token

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        values = {"Cookie": self.cookie} if authenticated else {}
        values.update(headers or {})
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def post(self, body, **changes):
        headers = {"Origin": self.server.origin, "X-CSRF-Token": self.server.csrf, "Content-Type": "application/json"}
        headers.update(changes)
        return self.request("POST", "/api/watchlist", json.dumps(body), headers)

    def test_authentication_host_and_bootstrap_never_echo_access_token(self):
        self.assertEqual(self.request("GET", "/api/bootstrap", authenticated=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/open?token=wrong", authenticated=False)[0], 403)
        status, headers, _ = self.request("GET", "/open?token=" + self.server.token, authenticated=False)
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
        status, headers, body = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["providers"], ["codex", "deepseek"])
        self.assertFalse(document["capabilities"]["new_research"])
        self.assertEqual(document["phase"], PHASE)
        self.assertTrue(document["capabilities"]["technical_dashboard"])
        self.assertTrue(document["capabilities"]["fundamental_dashboard"])
        self.assertTrue(document["capabilities"]["analyst_updates"])
        self.assertFalse(document["capabilities"]["broker_formal_import"])
        self.assertNotIn(self.server.token.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_s2_projections_require_authentication_and_strict_parameters(self):
        for path, method in (('/api/fundamental','fundamental_dashboard'),('/api/analyst-updates','analyst_updates')):
            with self.subTest(path=path), patch.object(self.store, method, return_value={'security':IDENTITY,'status':'fixture'}) as call:
                query = path + '?security_id=' + IDENTITY['security_id']
                self.assertEqual(self.request('GET', query, authenticated=False)[0], 401)
                self.assertEqual(self.request('GET', query + '&path=../../catalog.json')[0], 400)
                self.assertEqual(self.request('GET', query + '&security_id=OTHER')[0], 400)
                self.assertEqual(self.request('GET', query)[0], 200)
                self.assertEqual(call.call_count, 1)

    def test_s2_report_operations_retain_origin_csrf_and_body_guards(self):
        headers = {'Origin':self.server.origin,'X-CSRF-Token':self.server.csrf,'Content-Type':'application/json'}
        payload = {'security_id':IDENTITY['security_id']}
        with patch('app.broker_cache.refresh_index',return_value={'check_status':{'state':'baseline'}}) as call:
            self.assertEqual(self.request('POST','/api/analyst-refresh',json.dumps(payload),headers)[0],200)
            self.assertEqual(self.request('POST','/api/analyst-refresh',json.dumps(payload),{**headers,'Origin':'https://evil.example'})[0],403)
            self.assertEqual(self.request('POST','/api/analyst-refresh',json.dumps({**payload,'path':'private'}),headers)[0],400)
            self.assertEqual(call.call_count,1)
        request = {**payload,'record_id':'index-'+'a'*64,'url':'https://pdf.dfcfw.com/pdf/H3_AP123_1.pdf'}
        with patch('app.broker_cache.fetch_report',return_value={'formal_archive':False}) as call:
            self.assertEqual(self.request('POST','/api/analyst-fetch',json.dumps(request),headers)[0],200)
            self.assertEqual(self.request('POST','/api/analyst-fetch',json.dumps(request),{**headers,'X-CSRF-Token':'wrong'})[0],403)
            self.assertEqual(call.call_count,1)

    def test_s2_source_pdf_is_only_authenticated_attachment(self):
        path = '/api/analyst-source-file?security_id=' + IDENTITY['security_id'] + '&source_id=broker-'+'a'*32
        with patch('app.broker_cache.source_file',return_value=b'%PDF-fictional') as call:
            self.assertEqual(self.request('GET',path,authenticated=False)[0],401)
            status, headers, payload = self.request('GET',path)
            self.assertEqual(status,200)
            self.assertEqual(payload,b'%PDF-fictional')
            self.assertIn('attachment;',headers['Content-Disposition'])
            self.assertIn("default-src 'none'",headers['Content-Security-Policy'])
            self.assertEqual(self.request('GET',path+'&path=../../config.local.json')[0],400)
            self.assertEqual(call.call_count,1)

    def test_watchlist_roundtrip_idempotence_and_origin_guards(self):
        change = {"security_id": IDENTITY["security_id"], "tracked": True}
        self.assertEqual(self.post(change, Origin="https://attacker.example")[0], 403)
        self.assertEqual(self.post(change, **{"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.post(change, **{"X-CSRF-Token": "é"})[0], 403)
        self.assertEqual(self.post(change, **{"Content-Type": "text/plain"})[0], 415)
        self.assertFalse((self.root / "platform/watchlist.json").exists())
        self.assertEqual(self.post(change)[0], 200)
        first = (self.root / "platform/watchlist.json").read_bytes()
        self.assertEqual(self.post(change)[0], 200)
        self.assertEqual((self.root / "platform/watchlist.json").read_bytes(), first)
        reloaded = ResearchStore(self.root, TOOL)
        self.assertEqual(reloaded.watchlist()["securities"], [IDENTITY])
        self.assertEqual(self.post({**change, "tracked": False})[0], 200)
        self.assertEqual(self.store.watchlist()["securities"], [])
        self.assertEqual(len(self.store.context(IDENTITY["security_id"])["results"]), 1)

    def test_document_scope_and_escaped_reading(self):
        status, headers, body = self.request("GET", "/api/report?id=result%3AR1")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn('href="javascript:', text)
        self.assertIn("<table>", text)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        for path in ("/api/asset?id=result%3AR1&path=..%2F..%2Fconfig.local.json", "/api/report?id=../config.local.json", "/config.local.json", "/app/server.py"):
            self.assertIn(self.request("GET", path)[0], (400, 404))
        self.assertEqual(self.request("GET", "/api/context?security_id=ZZ-TEST-DEMO&module=fundamental")[0], 200)
        self.assertEqual(self.request("GET", "/api/securities?q=%E8%99%9A%E6%9E%84")[0], 200)

    def test_invalid_write_payload_does_not_create_state(self):
        for body in ({"security_id": IDENTITY["security_id"], "tracked": "true"}, {"security_id": "missing", "tracked": True, "extra": 1}):
            self.assertEqual(self.post(body)[0], 400)
        self.assertFalse((self.root / "platform/watchlist.json").exists())
        self.assertEqual(self.request("OPTIONS", "/api/watchlist")[0], 403)

    def test_technical_projection_is_authenticated_and_only_forwards_bounded_parameters(self):
        data = {"security": IDENTITY, "period": "1w", "status": "empty", "bars": []}
        with patch.object(self.store, "technical_dashboard", create=True, return_value=data) as projection:
            self.assertEqual(self.request("GET", "/api/technical?security_id=ZZ-TEST-DEMO", authenticated=False)[0], 401)
            projection.assert_not_called()
            status, headers, body = self.request("GET", "/api/technical?security_id=ZZ-TEST-DEMO&period=1w&limit=60&date_from=2026-01-01&date_to=2026-02-01")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), data)
            projection.assert_called_once_with(IDENTITY["security_id"], "1w", limit=60, date_from="2026-01-01", date_to="2026-02-01")
            self.assertEqual(headers["Cache-Control"], "no-store")
        with patch.object(self.store, "technical_dashboard", create=True) as projection:
            for query in ("limit=NaN", "period=1d&period=1w", "path=../../config.local.json"):
                self.assertEqual(self.request("GET", "/api/technical?security_id=ZZ-TEST-DEMO&" + query)[0], 400)
            projection.assert_not_called()
        with patch.object(self.store, "technical_dashboard", create=True, side_effect=KeyError("unknown")):
            self.assertEqual(self.request("GET", "/api/technical?security_id=missing")[0], 404)

    def test_research_selection_does_not_expose_access_credentials_or_accept_paths(self):
        data = {"formal_fundamental": "R1", "formal_technical": None, "supplementary": [], "unknown": []}
        with patch.object(self.store, "research_roles", create=True, return_value=data) as selection:
            status, _, body = self.request("GET", "/api/research-roles?security_id=ZZ-TEST-DEMO")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), data)
            selection.assert_called_once_with(IDENTITY["security_id"])
            self.assertNotIn(self.server.token.encode(), body)
        self.assertEqual(self.request("GET", "/api/research-roles?security_id=ZZ-TEST-DEMO&path=private")[0], 400)
        self.assertEqual(self.request("GET", "/api/research-roles")[0], 400)

    def test_shutdown_waits_for_inflight_watchlist_and_releases_writer_lock(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = self.store.lab.write_json
        responses = []
        def delayed(path, value):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test write was not released")
            return original(path, value)
        def close():
            self.server.server_close()
            closed.set()
        with patch.object(self.store.lab, "write_json", side_effect=delayed):
            request = threading.Thread(target=lambda: responses.append(self.post({"security_id": IDENTITY["security_id"], "tracked": True})))
            request.start()
            try:
                self.assertTrue(entered.wait(3))
                headers = {"Origin": self.server.origin, "X-CSRF-Token": self.server.csrf, "Content-Type": "application/json"}
                self.assertEqual(self.request("POST", "/api/shutdown", "{}", headers)[0], 200)
                self.thread.join(timeout=3)
                closing = threading.Thread(target=close)
                closing.start()
                self.assertFalse(closed.wait(0.2))
            finally:
                release.set()
                request.join(timeout=5)
            closing.join(timeout=5)
        self.assertTrue(closed.is_set())
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(self.store.watchlist()["securities"], [IDENTITY])
        self.assertFalse((self.root / ".write.lock").exists())
        self.assertEqual(list((self.root / "platform").glob(".watchlist-*.tmp")), [])


class ReportReaderTests(unittest.TestCase):
    def test_malformed_link_is_local_to_reference_and_tab_heading_still_renders(self):
        text = render_markdown('#\tTitle\n\n[x](http://[invalid)\n\n保留正文', "result:R1", lambda *args: None)
        self.assertIn("<h1", text)
        self.assertIn("Title</h1>", text)
        self.assertIn("原链接格式无效", text)
        self.assertIn("保留正文", text)

    def test_images_never_fetch_external_or_unlisted_sources(self):
        def missing(*args):
            raise KeyError("not listed")
        text = render_markdown('![private](https://attacker.example/image)\n\n![unknown](missing.png)', "result:R1", missing)
        self.assertNotIn('src="https:', text)
        self.assertNotIn("<img", text)
        self.assertIn("图片未在", text)


if __name__ == "__main__":
    unittest.main()
