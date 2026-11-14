"""Conservative manual review appends, tested only in fictional temp archives."""

from unittest.mock import patch

from app.continuity_actions import save_review
from tests.test_platform_continuity import ASOF, LATER, SECURITY, ContinuityFixture, observation


class ContinuityActionTests(ContinuityFixture):
    def setUp(self):
        super().setUp()
        self.now = patch.object(self.store.lab, "now", return_value=ASOF)
        self.now.start()
        self.addCleanup(self.now.stop)

    def body(self, *, operation="keep", item_id="OBS", reason="已人工查阅，等待同口径新资料。"):
        return {"security_id": SECURITY["security_id"], "item_id": item_id, "status": operation,
                "reason": reason, "revision_token": self.store.continuity_dashboard(SECURITY["security_id"])["revision_token"]}

    def test_keep_preserves_effective_status_frozen_definition_and_prior_windows(self):
        original = observation()
        original["review_due_at"] = "2026-12-01T18:00:00+08:00"
        original["valid_until"] = "2026-12-31T18:00:00+08:00"
        self.append("F1", [original])
        self.review("OLD-REVIEW", [{"item_id": "OBS", "status": "supported", "reason": "原人工回顾保留支持。",
                                   "evidence_ids": ["NEW"], "review_due_at": "2026-12-15T18:00:00+08:00"}], generated=LATER)
        old = self.store.lab.scan(self.root)["results"][0]
        item_path = self.store.lab.result_destination(self.root, old) / "items.json"
        original_bytes = item_path.read_bytes()
        result = save_review(self.store, self.body())
        self.assertTrue(result["saved"])
        self.assertEqual(item_path.read_bytes(), original_bytes)
        self.assertEqual(result["report_id"], "review:" + result["review_id"])
        current = result["continuity"]["items"][0]
        self.assertEqual(current["effective"]["status"], "supported")
        self.assertEqual(current["effective"]["current_use"], "needs_review")
        self.assertIsNone(current["effective"]["data_asof"])
        self.assertEqual(current["window"]["review_due_at"], "2026-12-15T18:00:00+08:00")
        self.assertEqual(current["window"]["valid_until"], "2026-12-31T18:00:00+08:00")
        record = next(row for row in self.store.lab.scan(self.root)["reviews"] if row["review_id"] == result["review_id"])
        self.assertEqual(record["evidence"], [])
        self.assertIsNone(record["data_asof"])
        self.assertEqual(set(record["updates"][0]), {"item_id", "status", "reason", "current_use"})
        self.assertEqual(record["updates"][0]["status"], "supported")
        drafts = list((self.root / "_workspace/platform/review-preparation").glob("*/review.md"))
        self.assertEqual(len(drafts), 1)

    def test_withdraw_is_historical_without_overwriting_original(self):
        self.append("F1", [observation()])
        result = save_review(self.store, self.body(operation="withdrawn"))
        item = result["continuity"]["items"][0]
        self.assertEqual(item["effective"]["status"], "withdrawn")
        self.assertEqual(item["effective"]["current_use"], "historical")
        self.assertEqual(item["original"]["status"], "pending")
        self.assertEqual(result["continuity"]["summary"]["pending_items"], 0)

    def test_operator_review_never_reactivates_historical_item(self):
        for operation in ("keep", "cannot_determine", "not_due", "withdrawn"):
            with self.subTest(operation=operation):
                item_id = "HISTORY-" + operation
                original = observation(item_id)
                original["current_use"] = "historical"
                original["review_due_at"] = "2026-12-31T18:00:00+08:00"
                self.append("F-" + operation, [original])
                result = save_review(self.store, self.body(operation=operation, item_id=item_id))
                current = next(row for row in result["continuity"]["items"] if row["item_id"] == item_id)
                self.assertEqual(current["effective"]["current_use"], "historical")
                self.assertFalse(current["effective"]["current_eligible"])
                self.assertEqual(current["original"]["current_use"], "historical")

    def test_cannot_determine_does_not_assert_new_evidence_or_success(self):
        self.append("F1", [observation()])
        result = save_review(self.store, self.body(operation="cannot_determine"))
        item = result["continuity"]["items"][0]
        self.assertEqual(item["effective"]["status"], "cannot_determine")
        self.assertEqual(item["check"]["state"], "needs_evidence")
        record = next(row for row in self.store.lab.scan(self.root)["reviews"] if row["review_id"] == result["review_id"])
        self.assertEqual(record["evidence"], [])
        self.assertFalse(result["continuity"]["hit_rate_computed"])

    def test_not_due_requires_existing_future_window_or_formal_not_due(self):
        self.append("F1", [observation()])
        with self.assertRaisesRegex(ValueError, "不能新造"):
            save_review(self.store, self.body(operation="not_due"))
        self.assertEqual(len(self.store.lab.scan(self.root)["reviews"]), 0)
        original = observation("FUTURE")
        original["review_due_at"] = "2026-12-31T18:00:00+08:00"
        self.append("F2", [original], generated=LATER, research=LATER)
        result = save_review(self.store, self.body(operation="not_due", item_id="FUTURE"))
        item = next(row for row in result["continuity"]["items"] if row["item_id"] == "FUTURE")
        self.assertEqual(item["effective"]["status"], "not_due")
        self.assertEqual(item["window"]["review_due_at"], original["review_due_at"])

    def test_already_formally_not_due_can_keep_it_without_inventing_date(self):
        self.append("F1", [observation()])
        self.review("OLD", [{"item_id": "OBS", "status": "not_due", "reason": "等待披露，尚无明确日历日期。"}])
        result = save_review(self.store, self.body(operation="not_due"))
        item = result["continuity"]["items"][0]
        self.assertEqual(item["effective"]["status"], "not_due")
        self.assertIsNone(item["window"]["review_due_at"])

    def test_exact_body_and_narrow_status_and_reason_validation(self):
        self.append("F1", [observation()])
        baseline = self.body()
        for body in (baseline | {"evidence": []}, baseline | {"status": "fulfilled"}, baseline | {"status": "no_change"},
                     baseline | {"reason": " "}, baseline | {"reason": "甲" * 1001}, baseline | {"revision_token": "x"},
                     baseline | {"status": []}, baseline | {"item_id": "../OBS"}):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    save_review(self.store, body)
        self.assertEqual(len(self.store.lab.scan(self.root)["reviews"]), 0)

    def test_item_must_belong_to_current_security(self):
        self.append("F1", [observation()])
        with self.assertRaisesRegex(ValueError, "当前证券"):
            save_review(self.store, self.body(item_id="MISSING"))

    def test_stale_revision_rejected_before_preparation(self):
        self.append("F1", [observation()])
        body = self.body()
        self.review("CLI", [{"item_id": "OBS", "status": "cannot_determine", "reason": "另一工具已更新。"}])
        with self.assertRaises(FileExistsError):
            save_review(self.store, body)
        self.assertFalse((self.root / "_workspace/platform/review-preparation").exists())
        self.assertEqual(len(self.store.lab.scan(self.root)["reviews"]), 1)

    def test_racing_cli_change_detected_inside_writer_lock(self):
        self.append("F1", [observation()])
        body = self.body()
        real_review = self.store.lab.review
        observed_lock = []
        def racing(root, request, *, check):
            self.review("CLI", [{"item_id": "OBS", "status": "cannot_determine", "reason": "准备草稿时另一工具更新。"}])
            def locked(catalog):
                observed_lock.append((self.root / ".write.lock").exists())
                return check(catalog)
            return real_review(root, request, check=locked)
        with patch.object(self.store.lab, "review", side_effect=racing):
            with self.assertRaises(FileExistsError):
                save_review(self.store, body)
        self.assertEqual(observed_lock, [True])
        self.assertEqual([row["review_id"] for row in self.store.lab.scan(self.root)["reviews"]], ["CLI"])

    def test_committed_review_survives_projection_failure_response(self):
        self.append("F1", [observation()])
        body = self.body()
        actual = self.store.continuity_dashboard
        calls = []
        def unavailable(security_id):
            calls.append(security_id)
            if len(calls) == 3:
                raise OSError("fictional post-save display failure")
            return actual(security_id)
        with patch.object(self.store, "continuity_dashboard", side_effect=unavailable):
            result = save_review(self.store, body)
        self.assertTrue(result["saved"])
        self.assertIsNone(result["continuity"])
        self.assertIn("不要重复提交", result["warning"])
        self.assertEqual(len(self.store.lab.scan(self.root)["reviews"]), 1)

    def test_duplicate_submission_is_stale_not_another_review(self):
        self.append("F1", [observation()])
        body = self.body()
        self.assertTrue(save_review(self.store, body)["saved"])
        with self.assertRaises(FileExistsError):
            save_review(self.store, body)
        self.assertEqual(len(self.store.lab.scan(self.root)["reviews"]), 1)
