import copy
import importlib.util
from html.parser import HTMLParser
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lab = load("archive_test_lab", "skills/stock-research-lab/scripts/research_lab.py")
views = load("archive_test_views", "skills/stock-research-archive/scripts/archive_views.py")
SECURITY = {"security_id": "ZZ-TEST-DEMO", "company": "Fictional <Company>", "market": "ZZ",
            "exchange": "TEST", "code": "DEMO", "share_class": "ordinary", "currency": "TST"}
ASOF = "2026-02-04T18:00:00+08:00"


class PageStructure(HTMLParser):
    def __init__(self, payload):
        super().__init__()
        self.depth = 0
        self.visible_text = []
        self.details = []
        self.timeline_links = []
        self.in_timeline = False
        self.feed(payload)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.details.append(attrs)
            self.depth += 1
        if tag == "table":
            self.in_timeline = True
        if tag == "a" and self.in_timeline:
            self.timeline_links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "details":
            self.depth -= 1
        if tag == "table":
            self.in_timeline = False

    def handle_data(self, text):
        if self.depth == 0:
            self.visible_text.append(text)


class ArchiveViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "private-lab"
        lab.init(self.root)
        self.report = self.base / "report.md"
        self.report.write_text("# Template heading never used as a summary\nREPORT_BODY_ONLY_TOKEN\n", encoding="utf-8")

    def request(self, result_id="R1", item_id="I1", day="2026-01-02", **changes):
        generated = day + "T18:00:00+08:00"
        value = {"security": dict(SECURITY), "module": "fundamental", "module_version": "0.1.0",
                 "result_id": result_id, "generated_at": generated, "research_asof": day + "T17:00:00+08:00",
                 "data_asof": day + "T16:00:00+08:00", "parent_result_ids": [], "report_file": str(self.report),
                 "evidence": [{"evidence_id": "E1", "source": "Fictional source <unsafe>",
                               "public_at": "2026-01-01T09:00:00+08:00"}],
                 "items": [{"item_id": item_id, "type": "observation", "text": "Watch fictional demand",
                            "report_locator": "tracking row", "evidence_ids": ["E1"], "frozen_at": generated,
                            "status": "pending", "definition": {"what": "demand", "why": "fictional driver",
                                                                   "next_evidence": "next fictional filing"},
                            "current_use": "eligible", "current_use_reason": "researcher explicitly reviewed",
                            "valid_until": "2026-01-20T18:00:00+08:00",
                            "review_due_at": "2026-01-20T18:00:00+08:00"}]}
        value.update(changes)
        return value

    def archive(self, *args, **kwargs):
        return lab.archive(self.root, self.request(*args, **kwargs))

    def view(self, **kwargs):
        return views.build_view(lab, self.root, SECURITY["security_id"], ASOF, **kwargs)

    def versions(self):
        self.archive()
        request = self.request("R2", "I2", "2026-02-02", module="technical", parent_result_ids=["R1"])
        request["items"][0]["parent_item_ids"] = ["I1"]
        request["items"][0]["text"] = "Track fictional breakout"
        self.archive_request(request)
        future = self.request("FUTURE_RESULT", "FUTURE_ITEM", "2026-03-02", parent_result_ids=["R2"])
        future["items"][0]["text"] = "FUTURE_CONTENT_TOKEN"
        self.archive_request(future)

    def archive_request(self, request):
        return lab.archive(self.root, request)

    def test_all_versions_pending_and_stale_qualification_survive_filters(self):
        self.versions()
        document = self.view(module="technical", date_from="2026-02-01", date_to="2026-02-03", query="breakout")
        self.assertEqual([record["result_id"] for record in document["results"]], ["R2"])
        self.assertEqual({item["item_id"] for item in document["pending_items"]}, {"I1", "I2"})
        old = next(item for item in document["pending_items"] if item["item_id"] == "I1")
        self.assertFalse(old["current_eligible"])
        self.assertIn("validity_window_ended", old["reasons"])
        self.assertEqual(old["valid_until"], "2026-01-20T18:00:00+08:00")
        self.assertEqual(document["parent_chains"], [{"result_id": "R2", "parent_result_ids": ["R1"]},
                                                     {"result_id": "R1", "parent_result_ids": []}])
        self.assertEqual(document["results"][0]["generated_at"], "2026-02-02T18:00:00+08:00")
        self.assertEqual(document["results"][0]["research_asof"], "2026-02-02T17:00:00+08:00")
        self.assertEqual(document["results"][0]["data_asof"], "2026-02-02T16:00:00+08:00")

    def test_future_contents_excluded_from_all_three_outputs(self):
        self.versions()
        lab.review(self.root, {"security_id": SECURITY["security_id"], "review_id": "FUTURE_REVIEW_ID",
            "generated_at": "2026-03-03T18:00:00+08:00", "research_asof": "2026-03-03T17:00:00+08:00",
            "data_asof": None, "report_file": str(self.report), "evidence": [],
            "updates": [{"item_id": "I1", "status": "invalidated", "reason": "FUTURE_REVIEW_CONTENT",
                         "current_use": "historical", "evidence_ids": []}]})
        lab.plan(self.root, {"security_id": SECURITY["security_id"], "plan_id": "FUTURE_PLAN_ID",
            "plan_version_id": "FUTURE_PLAN_VERSION", "version": 1, "source_result_ids": ["FUTURE_RESULT"],
            "generated_at": "2026-03-03T19:00:00+08:00", "frozen_at": "2026-03-03T19:00:00+08:00",
            "effective_at": "2026-03-04T09:00:00+08:00", "valid_until": "2026-04-04T18:00:00+08:00",
            "trigger": "FUTURE_PLAN_CONTENT", "confirmation": "fictional rule", "invalidation": "fictional rule",
            "exit": "fictional rule", "adoption": {"status": "research_only"}})
        out = self.base / "view"
        out.mkdir()
        document = self.view()
        paths = views.render_view(document, out)
        self.assertEqual({Path(value).name for value in paths}, {"report.html", "report.md", "archive-view.json"})
        for value in paths:
            payload = Path(value).read_text(encoding="utf-8")
            self.assertNotIn("FUTURE_CONTENT_TOKEN", payload)
            self.assertNotIn("FUTURE_RESULT", payload)
            self.assertNotIn("FUTURE_ITEM", payload)
            self.assertNotIn("FUTURE_REVIEW", payload)
            self.assertNotIn("FUTURE_PLAN", payload)
        self.assertEqual(len(json.loads((out / "archive-view.json").read_text(encoding="utf-8"))["results"]), 2)
        structure = PageStructure((out / "report.html").read_text(encoding="utf-8"))
        self.assertEqual(len(structure.timeline_links), 2)
        self.assertEqual(len([entry for entry in structure.details if entry.get("class") == "result"]), 2)
        self.assertTrue(all("open" not in entry for entry in structure.details))
        self.assertIn("Watch fictional demand", " ".join(structure.visible_text))
        self.assertIn("Track fictional breakout", " ".join(structure.visible_text))
        self.assertEqual(structure.depth, 0)
        markdown = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("R2 ← R1", markdown)
        self.assertIn("Fictional source &lt;unsafe&gt;", markdown)

    def test_unknown_research_date_explicitly_retained_then_excluded_by_date_filter(self):
        request = self.request("UNDATED", "UNDATED_ITEM", research_asof=None, data_asof=None,
                               import_id="IMPORT1", source_file=str(self.report))
        request["items"][0]["current_use"] = "needs_review"
        lab.import_report(self.root, request)
        document = views.build_view(lab, self.root, SECURITY["security_id"], "2099-01-01T00:00:00+08:00")
        self.assertEqual(document["results"][0]["research_asof"], None)
        self.assertFalse(document["results"][0]["current_eligible"])
        self.assertTrue(any("研究截止未知" in note for note in document["filter_notes"]))
        filtered = views.build_view(lab, self.root, SECURITY["security_id"], "2099-01-01T00:00:00+08:00", date_from="2026-01-01")
        self.assertEqual(filtered["results"], [])
        self.assertEqual(len(filtered["pending_items"]), 1)

    def test_visible_execution_does_not_expose_its_later_plan_link(self):
        self.archive()
        lab.plan(self.root, dict(security_id=SECURITY['security_id'],plan_id='LATER_PLAN_TOKEN',
            plan_version_id='LATER_VERSION_TOKEN',version=1,source_result_ids=['R1'],
            generated_at='2026-02-02T18:00:00+08:00',frozen_at='2026-02-02T18:00:00+08:00',
            effective_at='2026-02-03T09:00:00+08:00',valid_until='2026-03-03T18:00:00+08:00',
            trigger='LATER_TRIGGER_TOKEN',confirmation='synthetic',invalidation='synthetic',exit='synthetic',adoption={'status':'research_only'}))
        lab.execution(self.root, dict(security_id=SECURITY['security_id'],import_id='EARLY_EXEC',execution_kind='simulated',
            source_assertion='user_statement',generated_at='2026-01-03T18:00:00+08:00',source_file=str(self.report),
            trades=[dict(trade_id='VISIBLE_TRADE',executed_at='2026-01-03T10:00:00+08:00',time_precision='minute',
                side='buy',quantity='1',price='1',currency='TST',fees=None,
                allocations=[dict(plan_id='LATER_PLAN_TOKEN',plan_version_id='LATER_VERSION_TOKEN',quantity='1')])]))
        document=views.build_view(lab,self.root,SECURITY['security_id'],'2026-01-04T18:00:00+08:00')
        self.assertEqual(document['plans'],[])
        trade=document['executions'][0]['trades'][0]
        self.assertEqual(trade['trade_id'],'VISIBLE_TRADE')
        self.assertEqual(trade['plan_versions'],[])
        self.assertEqual(trade['unavailable_plan_links_count'],1)
        out=self.base/'past';out.mkdir()
        for path in views.render_view(document,out):
            payload=Path(path).read_text(encoding='utf-8')
            for token in ('LATER_PLAN_TOKEN','LATER_VERSION_TOKEN','LATER_TRIGGER_TOKEN'):
                self.assertNotIn(token,payload)
            self.assertIn('VISIBLE_TRADE',payload)

    def test_visible_revision_does_not_expose_invisible_predecessor(self):
        self.archive()
        plan=dict(security_id=SECURITY['security_id'],plan_id='COMMON_PLAN',version=1,source_result_ids=['R1'],
            plan_version_id='LATER_PREDECESSOR_TOKEN',generated_at='2026-02-02T18:00:00+08:00',
            frozen_at='2026-02-02T18:00:00+08:00',effective_at='2026-01-03T09:00:00+08:00',
            valid_until='2026-03-03T18:00:00+08:00',trigger='LATER_RULE_TOKEN',confirmation='synthetic',
            invalidation='synthetic',exit='synthetic',adoption={'status':'research_only'})
        lab.plan(self.root,plan)
        lab.plan(self.root,{**plan,'version':2,'plan_version_id':'VISIBLE_REVISION',
            'previous_plan_version_id':'LATER_PREDECESSOR_TOKEN','generated_at':'2026-01-03T18:00:00+08:00',
            'frozen_at':'2026-01-03T18:00:00+08:00','effective_at':'2026-01-04T09:00:00+08:00','trigger':'VISIBLE_RULE'})
        document=views.build_view(lab,self.root,SECURITY['security_id'],'2026-01-04T18:00:00+08:00')
        self.assertEqual(document['plans'][0]['plan_version_id'],'VISIBLE_REVISION')
        self.assertNotIn('previous_plan_version_id',document['plans'][0])
        self.assertTrue(document['plans'][0]['previous_plan_not_visible'])
        out=self.base/'past-revision';out.mkdir()
        for path in views.render_view(document,out):
            payload=Path(path).read_text(encoding='utf-8')
            self.assertNotIn('LATER_PREDECESSOR_TOKEN',payload)
            self.assertNotIn('LATER_RULE_TOKEN',payload)
            self.assertIn('VISIBLE_REVISION',payload)

    def test_query_only_searches_verified_metadata_and_frozen_items(self):
        self.archive()
        self.assertEqual(len(self.view(query="FICTIONAL DEMAND")["results"]), 1)
        self.assertEqual(len(self.view(query="source <unsafe>")["results"]), 1)
        self.assertEqual(self.view(query="REPORT_BODY_ONLY_TOKEN")["results"], [])
        self.assertEqual(self.view(query="Template heading")["results"], [])
        for invalid in ({"module": "unknown"}, {"date_from": "2026-02-05", "date_to": "2026-02-01"},
                        {"date_from": "20260101"}, {"date_to": "not-a-date"}, {"query": []}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.view(**invalid)
        invalid_context = lab.context(self.root, SECURITY["security_id"], ASOF)
        invalid_context["results"][0]["module"] = "unsupported"
        with patch.object(lab, "context", return_value=invalid_context), self.assertRaisesRegex(ValueError, "unsupported module"):
            self.view()

    def test_compare_rejects_missing_future_other_security_and_incompatible_identity(self):
        self.versions()
        other = self.request("OTHER_SECURITY", "OTHER_ITEM")
        other["security"].update(security_id="ZZ-TEST-OTHER", code="OTHER")
        self.archive_request(other)
        for right in ("FUTURE_RESULT", "OTHER_SECURITY", "MISSING"):
            with self.subTest(right=right), self.assertRaisesRegex(ValueError, "visible"):
                views.compare(lab, self.root, SECURITY["security_id"], ASOF, "R1", right)
        context = lab.context(self.root, SECURITY["security_id"], ASOF)
        for field in ("share_class", "currency"):
            incompatible = copy.deepcopy(context)
            incompatible["results"][0]["security"][field] = "different"
            with patch.object(lab, "context", return_value=incompatible), self.assertRaisesRegex(ValueError, "same security"):
                views.compare(lab, self.root, SECURITY["security_id"], ASOF, "R1", "R2")

    def test_compare_records_hashes_parents_and_frozen_vs_appended_state(self):
        attachment = self.base / "analysis.json"
        attachment.write_text('{"opaque":"ATTACHMENT_BODY_TOKEN"}', encoding="utf-8")
        attachments = [{"source_file": str(attachment), "relative_path": "analysis.json", "media_type": "application/json"}]
        self.archive(attachments=attachments)
        attachment.write_text('{"opaque":"changed"}', encoding="utf-8")
        self.report.write_text("Changed fictional report\n", encoding="utf-8")
        self.archive("R2", "I2", "2026-02-02", parent_result_ids=["R1"], module="technical", attachments=attachments)
        review = {"security_id": SECURITY["security_id"], "review_id": "RV1", "generated_at": "2026-02-03T18:00:00+08:00",
                  "research_asof": "2026-02-03T17:00:00+08:00", "data_asof": None,
                  "report_file": str(self.report), "evidence": [],
                  "updates": [{"item_id": "I1", "status": "invalidated", "reason": "Fictional invalidation",
                               "current_use": "historical", "evidence_ids": []}]}
        lab.review(self.root, review)
        comparison = views.compare(lab, self.root, SECURITY["security_id"], ASOF, "R1", "R2")
        self.assertTrue(comparison["report_changed"])
        self.assertEqual(comparison["attachment_changes"][0]["relative_path"], "analysis.json")
        self.assertEqual(comparison["parent_relationship"], "left_is_ancestor")
        self.assertEqual(comparison["items"][0]["frozen_items"][0]["status"], "pending")
        self.assertEqual(comparison["items"][0]["states_at_asof"][0]["status"], "invalidated")
        self.assertEqual(comparison["items"][0]["states_at_asof"][0]["applied_review_ids"], ["RV1"])
        self.assertFalse(comparison["numerical_comparison_performed"])
        self.assertTrue(any("不同专业" in note for note in comparison["comparison_limits"]))
        out = self.base / "comparison"
        out.mkdir()
        views.render_view(comparison, out)
        self.assertNotIn("ATTACHMENT_BODY_TOKEN", (out / "report.html").read_text(encoding="utf-8"))
        self.assertEqual(self.view()["reviews"], [{"review_id": "RV1", "item_ids": ["I1"], "source_result_ids": ["R1"]}])

    def test_html_escapes_text_links_only_visible_verified_root_files_and_no_mirroring(self):
        request = self.request()
        request["items"][0]["text"] = '<script>alert("fictional")</script>'
        html_report = self.base / "archived-report.html"
        html_report.write_text("<!doctype html><p>Fictional HTML report</p>", encoding="utf-8")
        request["attachments"] = [{"source_file": str(html_report), "relative_path": "report.html", "media_type": "text/html"}]
        self.archive_request(request)
        document = self.view()
        original = document["results"][0]["files"][0]
        outside = self.base / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        document["results"][0]["files"].extend([
            {"label": "escape", "path": "../outside.md", "sha256": original["sha256"]},
            {"label": "absolute", "path": str(outside), "sha256": original["sha256"]},
            {"label": '<img src=x onerror="bad">', "path": original["path"], "sha256": "0" * 64}])
        out = self.base / "escape"
        out.mkdir()
        views.render_view(document, out)
        payload = (out / "report.html").read_text(encoding="utf-8")
        self.assertNotIn('<script>alert("fictional")</script>', payload)
        self.assertIn("&lt;script&gt;", payload)
        self.assertIn("Fictional &lt;Company&gt;", payload)
        self.assertNotIn("<img src=x", payload)
        self.assertIn((self.root / original["path"]).as_uri(), payload)
        self.assertNotIn(outside.as_uri(), payload)
        self.assertEqual(payload.count('<a href="file:'), 3)
        structure = PageStructure(payload)
        self.assertEqual(structure.timeline_links, [(self.root / original["path"]).with_name("report.html").as_uri()])
        self.assertEqual(len([entry for entry in structure.details if entry.get("class") == "attachments"]), 1)
        self.assertIn("附件（4）", payload)
        self.assertIn((self.root / original["path"]).with_name("report.html").as_uri(), payload)
        self.assertNotIn("Template heading", payload)
        self.assertNotIn("REPORT_BODY_ONLY_TOKEN", payload)
        self.assertNotIn("https://", payload)
        self.assertNotIn("<script", payload)
        with self.assertRaises(FileExistsError):
            views.render_view(document, out)

    def test_regenerating_does_not_renew_stale_references_or_modify_sources(self):
        response = self.archive()
        source = self.root / response["path"]
        before = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}
        first = self.view()
        for suffix in ("one", "two"):
            output = self.base / suffix
            output.mkdir()
            views.render_view(self.view(), output)
        self.assertEqual(first, self.view())
        self.assertFalse(first["results"][0]["current_eligible"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()})

    def test_plan_execution_metadata_retained_outside_result_filters(self):
        self.archive()
        plan = {"security_id": SECURITY["security_id"], "plan_id": "P1", "plan_version_id": "PV1", "version": 1,
                "source_result_ids": ["R1"], "generated_at": "2026-01-02T19:00:00+08:00",
                "frozen_at": "2026-01-02T19:00:00+08:00", "effective_at": "2026-01-03T09:00:00+08:00",
                "valid_until": "2026-02-02T18:00:00+08:00", "trigger": "fictional condition",
                "confirmation": "fictional confirmation", "invalidation": "fictional invalidation", "exit": "fictional exit",
                "adoption": {"status": "research_only"}}
        lab.plan(self.root, plan)
        execution = {"security_id": SECURITY["security_id"], "import_id": "X1", "execution_kind": "real",
                     "source_assertion": "user_statement", "generated_at": "2026-01-03T18:00:00+08:00",
                     "source_file": str(self.report), "trades": [{"trade_id": "T1", "executed_at": "2026-01-03T10:00:00+08:00",
                         "time_precision": "minute", "side": "buy", "quantity": "10", "price": "2", "currency": "TST",
                         "fees": None, "allocations": [{"plan_id": "P1", "plan_version_id": "PV1", "quantity": "5"}]}]}
        lab.execution(self.root, execution)
        document = self.view(query="nonexistent")
        self.assertEqual(document["results"], [])
        self.assertEqual(document["plans"][0]["plan_version_id"], "PV1")
        self.assertEqual(document["executions"][0]["trades"][0]["trade_id"], "T1")
        self.assertNotIn("price", document["executions"][0]["trades"][0])
        self.assertFalse(document["pnl_computed"])


if __name__ == "__main__":
    unittest.main()
