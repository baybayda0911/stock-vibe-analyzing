"""Fictional continuity fixtures only; private research never enters tests."""

import copy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.continuity import continuity_dashboard, definition_digest
from app.store import ResearchStore


REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "skills/stock-research-lab/scripts/research_lab.py"
spec = importlib.util.spec_from_file_location("continuity_fixture_lab", TOOL)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
SECURITY = {"security_id": "ZZ-TEST-CONT", "company": "虚构延续甲", "market": "ZZ", "exchange": "TEST",
            "code": "CONT", "share_class": "ordinary", "currency": "CNY"}
EARLY = "2026-10-01T18:00:00+08:00"
DEADLINE = "2026-11-01T18:00:00+08:00"
LATER = "2026-11-02T18:00:00+08:00"
ASOF = "2026-11-13T23:00:00+08:00"
CRITERION = {"metric": "parent_profit", "period": "2026-Q3", "unit": "CNY", "basis": "single_quarter",
             "operator": "gte", "target": 100, "tolerance": 0}


def observation(key="OBS"):
    return {"item_id": key, "type": "observation", "text": "虚构现金跟踪项", "report_locator": "关注项表第1行",
            "frozen_at": EARLY, "definition": {"what": "现金及回款", "why": "警戒文字包含30%，不作为数值预测。",
                                                "next_evidence": "下次正式财报及账龄说明"},
            "status": "pending", "current_use": "needs_review", "evidence_ids": ["OLD"]}


def prediction(key="PRED", *, structured=True, deadline=DEADLINE):
    definition = {"metric": "归母净利", "expected": "至少100", "unit": "CNY", "period": "2026-Q3",
                  "deadline": deadline, "baseline": "上期90", "tolerance": "0", "evaluation_rule": "同口径实际值达到目标"}
    if structured:
        definition["deterministic_rule"] = copy.deepcopy(CRITERION)
    return {**observation(key), "type": "prediction", "definition": definition, "text": "虚构数值预测"}


class ContinuityFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "private-root"
        lab.init(self.root)
        self.store = ResearchStore(self.root, TOOL)

    def append(self, key, items=(), documents=None, *, generated=EARLY, research=EARLY, data=None,
               parents=(), role="full_research", reconstruction=False, evidence=None):
        report = self.base / (key + ".md")
        report.write_text("# 虚构研究\n\n所有内容只用于测试。\n", encoding="utf-8")
        documents = copy.deepcopy(documents or {})
        documents["research-components.json"] = {"schema_version": "stock-research-components/1",
            "security": SECURITY, "result_id": key, "content_role": role, "components": []}
        attachments = []
        for name, document in documents.items():
            source = self.base / (key + "-" + name)
            source.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            attachments.append({"source_file": str(source), "relative_path": name, "media_type": "application/json"})
        request = {"security": SECURITY, "module": "fundamental", "module_version": "0.3.1", "result_id": key,
            "generated_at": generated, "research_asof": research, "data_asof": data, "report_file": str(report),
            "parent_result_ids": list(parents), "historical_reconstruction": reconstruction,
            "items": list(items), "evidence": evidence if evidence is not None else [
                {"evidence_id": "OLD", "source": "虚构原始披露", "public_at": "2026-09-30T18:00:00+08:00"}],
            "attachments": attachments}
        lab.archive(self.root, request)
        return request

    def rule_document(self, source, item, *, key=None, declared=EARLY, criterion=None):
        key = key or source
        return {"schema_version": "stock-research-verification/1", "security": SECURITY, "result_id": key,
            "rules": [{"rule_id": "RULE-" + item["item_id"], "item_id": item["item_id"], "source_result_id": source,
                       "original_report_locator": item["report_locator"], "definition_sha256": definition_digest(item["definition"]),
                       "declared_at": declared, "criterion": copy.deepcopy(criterion or CRITERION),
                       "window_end": item["definition"].get("deadline", DEADLINE)}]}

    def facts_document(self, key="FACT", *, value=110, basis="single_quarter", unit="CNY", public_at=LATER, data=LATER):
        return {"schema_version": "stock-research-facts/1", "security": SECURITY, "result_id": key, "facts": [
            {"fact_id": "VALUE", "metric": "parent_profit", "period": "2026-Q3", "unit": unit, "basis": basis,
             "value": value, "public_at": public_at, "data_asof": data, "evidence_ids": ["ACTUAL"]}]}

    def facts(self, **kwargs):
        document = self.facts_document(**kwargs)
        return self.append(document["result_id"], documents={"verification-facts.json": document}, generated=LATER,
                           research=LATER, evidence=[{"evidence_id": "ACTUAL", "source": "虚构实际披露", "public_at": LATER}])

    def review(self, key, updates, *, generated=LATER, data=None):
        report = self.base / (key + ".md")
        report.write_text("# 虚构追加回顾\n", encoding="utf-8")
        request = {"review_id": key, "security_id": SECURITY["security_id"], "generated_at": generated,
                   "research_asof": generated, "data_asof": data, "report_file": str(report),
                   "evidence": [{"evidence_id": "NEW", "source": "虚构回顾证据", "public_at": LATER}], "updates": updates}
        lab.review(self.root, request)

    def dashboard(self, asof=ASOF):
        return continuity_dashboard(self.store, SECURITY["security_id"], asof)


class ContinuityTests(ContinuityFixture):
    def test_old_items_survive_latest_empty_result_and_do_not_write(self):
        self.append("F1", [observation()])
        self.append("F2", generated=LATER, research=LATER, parents=["F1"])
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        dashboard = self.dashboard()
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(dashboard["summary"]["total_items"], 1)
        self.assertEqual(dashboard["summary"]["pending_items"], 1)
        self.assertEqual(dashboard["items"][0]["source"]["result_id"], "F1")
        self.assertEqual(dashboard["items"][0]["original"]["definition"], observation()["definition"])
        self.assertEqual(dashboard["timeline"][0]["source"]["parent_result_ids"], ["F1"])
        self.assertFalse(dashboard["pnl_computed"])
        self.assertFalse(dashboard["hit_rate_computed"])

    def test_original_status_and_dates_separate_from_latest_mixed_review(self):
        self.append("F1", [observation()], data="2026-09-30T15:00:00+08:00")
        self.review("R1", [{"item_id": "OBS", "status": "observed", "reason": "取得披露，仍需核查口径。",
                            "evidence_ids": ["NEW"]}], data=None)
        item = self.dashboard()["items"][0]
        self.assertEqual(item["original"]["status"], "pending")
        self.assertEqual(item["effective"]["status"], "observed")
        self.assertEqual(item["original"]["data_asof"], "2026-09-30T15:00:00+08:00")
        self.assertIsNone(item["effective"]["data_asof"])
        self.assertIsNone(item["reviews"][0]["data_asof"])
        self.assertEqual(item["reviews"][0]["reason_categories"], [])
        self.assertEqual(item["evidence"]["latest"][0]["evidence_id"], "NEW")
        self.assertEqual(item["check"]["state"], "reviewed")
        self.assertEqual(item["prospective"]["qualification"], "not_scored")

    def test_frozen_structured_numeric_rule_yields_candidate_not_review(self):
        item = prediction()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        self.facts()
        dashboard = self.dashboard()
        entry = dashboard["items"][0]
        self.assertEqual(entry["check"]["state"], "candidate")
        self.assertEqual(entry["check"]["candidate_status"], "fulfilled")
        self.assertEqual(entry["effective"]["status"], "pending")
        self.assertTrue(entry["check"]["requires_confirmation"])
        self.assertFalse(entry["check"]["persisted"])
        self.assertTrue(entry["prospective"]["eligible"])
        self.assertEqual(dashboard["summary"]["reviews"], 0)
        self.assertEqual(dashboard["attention_queue"][0]["next_step"]["action"], "review_candidate")

    def test_explicit_numeric_failure_and_tolerance_range(self):
        criterion = CRITERION | {"operator": "between", "target": [100, 120], "tolerance": 2}
        item = prediction()
        item["definition"]["deterministic_rule"] = criterion
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item, criterion=criterion)})
        self.facts(value=97)
        self.assertEqual(self.dashboard()["items"][0]["check"]["candidate_status"], "not_fulfilled")

    def test_future_window_is_not_due_and_no_early_success(self):
        future = "2026-12-31T18:00:00+08:00"
        item = prediction(deadline=future)
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        self.facts()
        entry = self.dashboard()["items"][0]
        self.assertEqual(entry["check"]["state"], "not_due")
        self.assertIsNone(entry["check"]["candidate_status"])

    def test_prose_numbers_are_not_rules_and_unknown_window_stays_unknown(self):
        self.append("F1", [prediction(structured=False), observation()])
        self.facts()
        entries = {row["item_id"]: row for row in self.dashboard()["items"]}
        self.assertEqual(entries["PRED"]["check"]["state"], "needs_evidence")
        self.assertEqual(entries["OBS"]["check"]["state"], "observation")
        self.assertEqual(entries["OBS"]["window"]["state"], "unknown")
        self.assertIsNone(entries["OBS"]["window"]["deadline"])
        self.assertFalse(entries["OBS"]["prospective"]["eligible"])

    def test_price_basis_mismatch_is_incomparable(self):
        item = prediction()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        self.facts(basis="qfq_not_raw")
        entry = self.dashboard()["items"][0]
        self.assertEqual(entry["check"]["state"], "incomparable")
        self.assertIn("unit_or_basis_mismatch", entry["check"]["coverage_reasons"])
        self.assertIsNone(entry["check"]["candidate_status"])

    def test_unpublished_or_missing_fact_stays_needs_evidence(self):
        item = prediction()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        self.facts(public_at=None)
        entry = self.dashboard()["items"][0]
        self.assertEqual(entry["check"]["state"], "needs_evidence")
        self.assertIn("fact_publication_or_data_asof_unverified", entry["check"]["coverage_reasons"])

    def test_later_rule_adaptation_cannot_be_backdated(self):
        item = prediction(structured=False)
        self.append("F1", [item])
        self.append("ADAPT", documents={"verification-rules.json": self.rule_document("F1", item, key="ADAPT", declared=LATER)},
                    role="source_adaptation", generated=LATER, research=LATER, parents=["F1"])
        self.facts()
        entry = self.dashboard()["items"][0]
        self.assertEqual(entry["check"]["state"], "candidate")
        self.assertFalse(entry["prospective"]["eligible"])
        self.assertIn("later_or_unverified_rule_mapping_cannot_be_backdated", entry["prospective"]["reasons"])

    def test_observation_cannot_be_scored_even_with_an_attached_rule(self):
        item = observation()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        self.facts()
        entry = self.dashboard()["items"][0]
        self.assertEqual(entry["check"]["state"], "observation")
        self.assertIsNone(entry["check"]["candidate_status"])

    def test_historical_reconstruction_excluded_from_prospective_qualification(self):
        item = prediction()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)}, reconstruction=True)
        self.facts()
        self.assertFalse(self.dashboard()["items"][0]["prospective"]["eligible"])

    def test_cross_security_attachment_and_rule_binding_rejected(self):
        item = prediction()
        document = self.rule_document("F1", item)
        document["rules"][0]["definition_sha256"] = "0" * 64
        self.append("F1", [item], {"verification-rules.json": document})
        with self.assertRaisesRegex(ValueError, "bind the frozen"):
            self.dashboard()

    def test_no_attachment_or_chart_json_read_when_no_declared_metadata(self):
        self.append("F1", [observation()])
        original_reader = self.store._record_reader
        reads = []
        def reader(record, kind="result"):
            wrapped = original_reader(record, kind)
            def read(name=None):
                reads.append(name)
                return wrapped(name)
            return read
        self.store._record_reader = reader
        self.dashboard()
        self.assertEqual(reads, ["research-components.json"])

    def test_asof_hides_later_facts_and_reviews(self):
        item = prediction()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        self.facts()
        self.review("R1", [{"item_id": "PRED", "status": "fulfilled", "reason": "虚构研究者确认。", "evidence_ids": ["NEW"]}])
        dashboard = self.dashboard("2026-10-15T18:00:00+08:00")
        self.assertEqual(dashboard["items"][0]["check"]["state"], "not_due")
        self.assertEqual(dashboard["reviews"] if "reviews" in dashboard else dashboard["summary"]["reviews"], 0)
        self.assertEqual(len(dashboard["timeline"]), 1)

    def test_source_role_keeps_supplementary_event_out_of_formal_claim(self):
        self.append("SOURCE", role="broker_source")
        dashboard = self.dashboard()
        self.assertEqual(dashboard["timeline"][0]["event_type"], "source")
        self.assertEqual(dashboard["timeline"][0]["source"]["content_role"], "broker_source")
        self.assertEqual(dashboard["summary"]["total_items"], 0)

    def test_revision_token_stable_across_clock_seconds_changes_after_review(self):
        self.append("F1", [observation()])
        first = self.dashboard()["revision_token"]
        self.assertEqual(first, self.dashboard("2026-11-13T23:00:01+08:00")["revision_token"])
        self.review("R1", [{"item_id": "OBS", "status": "cannot_determine", "reason": "后续资料仍缺同口径覆盖。", "evidence_ids": ["NEW"]}])
        second = self.dashboard()
        self.assertNotEqual(first, second["revision_token"])
        self.assertEqual(second["items"][0]["check"]["state"], "needs_evidence")
        self.assertTrue(second["items"][0]["check"]["has_formal_review"])

    def test_formal_not_due_and_future_review_dates_are_preserved(self):
        item = observation()
        item["review_due_at"] = "2026-12-31T18:00:00+08:00"
        self.append("F1", [item])
        self.assertEqual(self.dashboard()["items"][0]["check"]["state"], "not_due")
        self.review("R1", [{"item_id": "OBS", "status": "not_due", "reason": "原报告等待年度披露，不提前计成功。", "evidence_ids": ["NEW"]}])
        item = self.dashboard()["items"][0]
        self.assertEqual(item["check"]["state"], "not_due")
        self.assertEqual(item["window"]["review_due_at"], "2026-12-31T18:00:00+08:00")

    def test_unsupported_compound_rule_must_not_be_silently_ignored(self):
        item = prediction()
        document = self.rule_document("F1", item)
        document["rules"][0]["criterion"]["and"] = "unimplemented condition"
        self.append("F1", [item], {"verification-rules.json": document})
        with self.assertRaisesRegex(ValueError, "fields cannot be ignored"):
            self.dashboard()

    def test_supplementary_source_cannot_become_actual_observed_evidence(self):
        item = prediction()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        document = self.facts_document("BROKER")
        self.append("BROKER", documents={"verification-facts.json": document}, generated=LATER, research=LATER,
                    role="broker_source", evidence=[{"evidence_id": "ACTUAL", "source": "虚构研报观点", "public_at": LATER}])
        dashboard = self.dashboard()
        self.assertEqual(dashboard["items"][0]["check"]["state"], "needs_evidence")
        self.assertEqual(dashboard["summary"]["candidates"], 0)
        self.assertEqual(dashboard["adapter_warnings"][0]["reason"], "supplementary_or_unknown_source_is_not_new_observed_fact")

    def test_verification_attachment_requires_complete_security_identity(self):
        item = prediction()
        document = self.rule_document("F1", item)
        document["security"] = SECURITY | {"company": "另一家同代码虚构企业"}
        self.append("F1", [item], {"verification-rules.json": document})
        with self.assertRaisesRegex(ValueError, "complete archived identity"):
            self.dashboard()

    def test_conflicting_same_basis_facts_require_selection(self):
        item = prediction()
        self.append("F1", [item], {"verification-rules.json": self.rule_document("F1", item)})
        self.facts(value=110)
        self.facts(key="FACT2", value=90)
        entry = self.dashboard()["items"][0]
        self.assertEqual(entry["check"]["state"], "incomparable")
        self.assertIsNone(entry["check"]["candidate_status"])
        self.assertIn("conflicting_same_basis_facts_require_selection", entry["check"]["coverage_reasons"])


if __name__ == "__main__":
    unittest.main()
