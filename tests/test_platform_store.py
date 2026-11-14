import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.store import ResearchStore, WATCHLIST_SCHEMA


REPO = Path(__file__).resolve().parents[1]
LAB_TOOL = REPO / "skills/stock-research-lab/scripts/research_lab.py"
spec = importlib.util.spec_from_file_location("platform_test_lab", LAB_TOOL)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
SECURITY = {"security_id": "ZZ-TEST-DEMO", "company": "Fictional Company", "market": "ZZ",
            "exchange": "TEST", "code": "DEMO", "share_class": "ordinary", "currency": "TST"}
ASOF = "2026-02-04T18:00:00+08:00"


class PlatformStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "private-lab"
        lab.init(self.root)
        self.report = self.base / "report.md"
        self.report.write_text("# Fictional report\n<script>alert('untrusted')</script>\n", encoding="utf-8")
        self.store = ResearchStore(self.root, LAB_TOOL)

    def archive(self, result_id="R1", item_id="I1", day="2026-01-02", **changes):
        generated = day + "T18:00:00+08:00"
        request = {"security": dict(SECURITY), "module": "fundamental", "module_version": "0.1.0",
            "result_id": result_id, "generated_at": generated, "research_asof": day + "T17:00:00+08:00",
            "data_asof": day + "T16:00:00+08:00", "parent_result_ids": [], "report_file": str(self.report),
            "evidence": [{"evidence_id": "E1", "source": "Fictional excerpt", "public_at": "2026-01-01T09:00:00+08:00"}],
            "items": [{"item_id": item_id, "type": "observation", "text": "Watch fictional demand",
                "report_locator": "row 1", "evidence_ids": ["E1"], "frozen_at": generated, "status": "pending",
                "definition": {"what": "demand", "why": "fictional driver", "next_evidence": "filing"},
                "current_use": "needs_review", "review_due_at": "2026-02-01T18:00:00+08:00"}]}
        request.update(changes)
        return lab.archive(self.root, request)

    def review(self, review_id="RV1"):
        return lab.review(self.root, {"security_id": SECURITY["security_id"], "review_id": review_id,
            "generated_at": "2026-02-02T18:00:00+08:00", "research_asof": "2026-02-02T17:00:00+08:00",
            "data_asof": None, "report_file": str(self.report), "evidence": [],
            "updates": [{"item_id": "I1", "status": "cannot_determine", "reason": "Fictional data missing",
                         "evidence_ids": [], "current_use": "needs_review"}]})

    def watch_path(self):
        return self.root / "platform/watchlist.json"

    def save_watch(self, value):
        self.watch_path().parent.mkdir(exist_ok=True)
        self.watch_path().write_text(json.dumps(value), encoding="utf-8")

    def test_empty_archive_is_read_only_and_unknown_identity_is_not_fabricated(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.store.overview()["counts"]["results"], 0)
        self.assertEqual(self.store.watchlist()["securities"], [])
        self.assertEqual(self.store.search("600000")["securities"], [])
        with self.assertRaises(KeyError):
            self.store.context("ZZ-TEST-UNKNOWN")
        with self.assertRaises(KeyError):
            self.store.update_watchlist("ZZ-TEST-UNKNOWN", True)
        self.assertEqual(before, {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_watchlist_add_duplicate_remove_preserves_all_archive_bytes(self):
        self.archive()
        original = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        added = self.store.update_watchlist(SECURITY["security_id"], True)
        self.assertEqual(added["securities"], [SECURITY])
        self.assertEqual(set(added), {"schema_version", "updated_at", "securities"})
        self.assertEqual(added, self.store.update_watchlist(SECURITY["security_id"], True))
        removed = self.store.update_watchlist(SECURITY["security_id"], False)
        self.assertEqual(removed["securities"], [])
        self.assertEqual(removed, self.store.update_watchlist(SECURITY["security_id"], False))
        self.assertEqual(original, {path: (self.root / path).read_bytes() for path in original})
        self.assertEqual(self.store.overview()["counts"]["results"], 1)

    def test_watchlist_survives_a_new_process(self):
        self.archive()
        self.store.update_watchlist(SECURITY["security_id"], True)
        code = "from app.store import ResearchStore; import json,sys; from pathlib import Path; print(json.dumps(ResearchStore(Path(sys.argv[1]),Path(sys.argv[2])).watchlist()))"
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(self.root), str(LAB_TOOL)], cwd=REPO,
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["securities"], [SECURITY])

    def test_root_writer_lock_prevents_changes(self):
        self.archive()
        with lab.writer(self.root):
            with self.assertRaises(FileExistsError):
                self.store.update_watchlist(SECURITY["security_id"], True)
        self.assertFalse(self.watch_path().exists())

    def test_failed_atomic_replace_preserves_previous_watchlist(self):
        self.archive()
        self.store.update_watchlist(SECURITY["security_id"], True)
        original = self.watch_path().read_bytes()
        with patch("app.store.os.replace", side_effect=OSError("fictional replace failure")):
            with self.assertRaises(OSError):
                self.store.update_watchlist(SECURITY["security_id"], False)
        self.assertEqual(self.watch_path().read_bytes(), original)
        self.assertEqual(list(self.watch_path().parent.glob(".watchlist-*.tmp")), [])
        self.assertFalse((self.root / ".write.lock").exists())

    def test_bad_watchlist_json_schema_identity_and_directory_fail_closed(self):
        good = {"schema_version": WATCHLIST_SCHEMA, "updated_at": ASOF, "securities": [SECURITY]}
        bad = [{**good, "schema_version": "future/2"}, {**good, "updated_at": "2026-02-04"},
               {**good, "extra": "field"}, {**good, "securities": [{**SECURITY, "source": "extra"}]},
               {**good, "securities": [SECURITY, SECURITY]}, {**good, "securities": [{"security_id": SECURITY["security_id"]}]}]
        for value in bad:
            with self.subTest(value=value):
                self.save_watch(value)
                with self.assertRaises(ValueError):
                    self.store.watchlist()
        self.watch_path().write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.watchlist()
        self.watch_path().unlink()
        self.watch_path().mkdir()
        with self.assertRaises(ValueError):
            self.store.watchlist()

    def test_platform_file_is_not_an_empty_watchlist(self):
        (self.root / "platform").write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.watchlist()

    def test_context_retains_old_pending_items_and_original_dates_without_reindex(self):
        self.archive()
        self.archive("R2", "I2", "2026-02-02", module="technical", parent_result_ids=["R1"])
        self.archive("R3", "I3", "2026-03-02")
        self.review()
        (self.root / "catalog.json").write_text("{bad disposable cache", encoding="utf-8")
        with patch.object(self.store.lab, "reindex", side_effect=AssertionError("GET must not reindex")):
            context = self.store.context(SECURITY["security_id"], ASOF, module="technical")
        self.assertEqual([row["result_id"] for row in context["results"]], ["R2"])
        self.assertEqual({row["item_id"] for row in context["pending_items"]}, {"I1", "I2"})
        self.assertEqual(context["results"][0]["report_id"], "result:R2")
        self.assertEqual(context["results"][0]["research_asof"], "2026-02-02T17:00:00+08:00")
        self.assertNotIn("path", context["results"][0])
        context = self.store.context(SECURITY["security_id"], ASOF)
        self.assertEqual(context["reviews"][0]["report_id"], "review:RV1")
        self.assertIsNone(context["reviews"][0]["data_asof"])
        self.assertEqual((self.root / "catalog.json").read_text(encoding="utf-8"), "{bad disposable cache")

    def test_external_archive_additions_are_visible_without_restarting(self):
        self.assertEqual(self.store.search("DEMO")["securities"], [])
        self.archive()
        self.assertEqual(self.store.search("Fictional")["securities"][0]["security_id"], SECURITY["security_id"])
        self.assertEqual(self.store.search("ZZ-TEST-DEMO")["securities"][0]["code"], "DEMO")

    def test_documents_are_only_reports_and_explicit_attachments(self):
        html = self.base / "view.html"
        html.write_text("<script>untrusted()</script>", encoding="utf-8")
        self.archive(attachments=[{"source_file": str(html), "relative_path": "figures/view.html", "media_type": "text/html"}])
        self.review(review_id="R1")
        payload, media, name = self.store.document("result:R1")
        self.assertEqual(payload, self.report.read_bytes())
        self.assertEqual(name, "report.md")
        self.assertTrue(media.startswith("text/markdown"))
        self.assertEqual(self.store.document("review:R1")[2], "review.md")
        self.assertEqual(self.store.document("result:R1", "figures/view.html"), (html.read_bytes(), "text/html", "figures/view.html"))
        for record_id, path in [("result:R1", "../../config.local.json"), ("result:R1", "manifest.json"),
                                ("result:R1", "items.json"), ("result:R1", str(self.report)),
                                ("result:R1", "config.local.json"), ("review:R1", "figures/view.html"),
                                ("../../config.local.json", None), ("result:../R1", None)]:
            with self.subTest(record_id=record_id, path=path), self.assertRaises(ValueError):
                self.store.document(record_id, path)

    def test_changed_report_attachment_review_and_bad_source_dates_are_errors(self):
        self.archive()
        saved = next((self.root / "securities").rglob("manifest.json"))
        report = saved.parent / "report.md"
        payload = report.read_bytes()
        report.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.overview()
        report.write_bytes(payload)
        self.review()
        review = next((self.root / "securities").rglob("review.md"))
        review.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.context(SECURITY["security_id"], ASOF)
        review.write_bytes(payload)
        value = json.loads(saved.read_text(encoding="utf-8"))
        value["data_asof"] = "not-a-timestamp"
        saved.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.overview()

    def test_document_reader_scans_once_and_checks_each_read_against_frozen_whitelist(self):
        attachment = self.base / "detail.json"
        attachment.write_text('{"fictional": true}', encoding="utf-8")
        self.archive(attachments=[{"source_file": str(attachment), "relative_path": "detail.json", "media_type": "application/json"}])
        with patch.object(self.store, "_scan", wraps=self.store._scan) as scan:
            read = self.store.document_reader("result:R1")
            self.assertEqual(read()[0], self.report.read_bytes())
            self.assertEqual(read("detail.json")[0], attachment.read_bytes())
            for name in ("../config.local.json", str(attachment), "manifest.json", "missing.json"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    read(name)
            archived = next((self.root / "securities").rglob("detail.json"))
            archived.write_text('{"changed": true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                read("detail.json")
            self.assertEqual(scan.call_count, 1)

    def test_declared_attachment_hash_and_metadata_schema_are_validated(self):
        attachment = self.base / "detail.json"
        attachment.write_text('{"fictional": true}', encoding="utf-8")
        self.archive(attachments=[{"source_file": str(attachment), "relative_path": "detail.json", "media_type": "application/json"}])
        saved = next((self.root / "securities").rglob("manifest.json"))
        archived_attachment = saved.parent / "detail.json"
        archived_attachment.write_text('{"changed": true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.store.document("result:R1", "detail.json")
        archived_attachment.write_bytes(attachment.read_bytes())
        metadata = json.loads(saved.read_text(encoding="utf-8"))
        metadata["schema_version"] = "future/2"
        saved.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema"):
            self.store.overview()

    def test_reparse_guard_fails_closed_without_link_creation_privileges(self):
        platform = self.root / "platform"
        platform.mkdir()
        linked = self.store.lab.linked
        with patch.object(self.store.lab, "linked", side_effect=lambda path: path == platform or linked(path)):
            with self.assertRaisesRegex(ValueError, "reparse"):
                self.store.watchlist()

    def test_hardlinked_watchlist_and_document_sources_fail_closed(self):
        attachment = self.base / "detail.json"
        attachment.write_text('{"fictional": true}', encoding="utf-8")
        self.archive(attachments=[{"source_file": str(attachment), "relative_path": "detail.json", "media_type": "application/json"}])
        self.store.update_watchlist(SECURITY["security_id"], True)
        report = next((self.root / "securities").rglob("report.md"))
        archived_attachment = next((self.root / "securities").rglob("detail.json"))
        original_stat = Path.stat
        for target, operation in [(self.watch_path(), self.store.watchlist),
                                  (self.watch_path(), lambda: self.store.update_watchlist(SECURITY["security_id"], False)),
                                  (report, lambda: self.store.document("result:R1")),
                                  (archived_attachment, lambda: self.store.document("result:R1", "detail.json"))]:
            def linked_stat(path, *args, **kwargs):
                info = original_stat(path, *args, **kwargs)
                if path == target and kwargs.get("follow_symlinks", True):
                    fields = list(info)
                    fields[3] = 2
                    return os.stat_result(fields)
                return info
            with self.subTest(target=target), patch.object(Path, "stat", autospec=True, side_effect=linked_stat):
                with self.assertRaisesRegex(ValueError, "without links"):
                    operation()
        self.assertEqual(self.store.watchlist()["securities"], [SECURITY])

    def test_header_short_names_are_aliases_and_archive_identity_stays_authoritative(self):
        identity = {"security_id": "A-share-sz-999999", "company": "虚构公司股份有限公司", "market": "A-share",
                    "exchange": "sz", "code": "999999", "share_class": "A", "currency": "CNY"}
        self.archive(security=identity)
        market = self.base / "local-index"
        market.mkdir()
        for name, company in [("sz999999", "虚构公司"), ("sh999998", "虚构公司")]:
            (market / (name + ".csv")).write_text("fictional source\n股票代码,股票名称\n" + name + "," + company + "\n", encoding="utf-8")
        store = ResearchStore(self.root, LAB_TOOL, market)
        matches = store.search("虚构公司")["securities"]
        self.assertEqual(len(matches), 2)
        archived = next(entry for entry in matches if entry["security_id"] == identity["security_id"])
        self.assertEqual({key: archived[key] for key in lab.IDENTITY_FIELDS}, identity)
        self.assertFalse(archived["requires_identity_confirmation"])
        self.assertEqual(store.context(identity["security_id"], ASOF)["security"], identity)

    def test_missing_or_directory_source_is_not_reported_as_empty_archive(self):
        self.archive()
        source = next((self.root / "securities").rglob("report.md"))
        source.unlink()
        with self.assertRaises(OSError):
            self.store.overview()
        source.mkdir()
        with self.assertRaises((OSError, ValueError)):
            self.store.overview()
        with self.assertRaises(OSError):
            ResearchStore(self.base / "missing-lab", LAB_TOOL)
        with self.assertRaises(ValueError):
            ResearchStore(self.root / ".." / self.root.name, LAB_TOOL)

    def test_local_index_reads_header_only_uses_explicit_prefix_and_marks_confirmation(self):
        market = self.base / "local-index"
        market.mkdir()
        source = market / "sz999999.csv"
        source.write_bytes(("fictional source\n股票代码,股票名称,交易日期\nsz999999,虚构公司,2020-01-01\n" + "\x00" * 100000).encode("gb18030"))
        (market / "sh999998.csv").write_text("notice\n股票代码,股票名称\nsz999998,错误身份\n", encoding="utf-8")
        store = ResearchStore(self.root, LAB_TOOL, market)
        result = store.search("虚构公司")
        identity = result["securities"][0]
        self.assertEqual(identity["security_id"], "A-share-sz-999999")
        self.assertEqual(identity["identity_source"], "local_csv_header")
        self.assertTrue(identity["requires_identity_confirmation"])
        self.assertEqual(result["quote_status"], "not_realtime")
        self.assertEqual(len(result["warnings"]), 1)
        saved = store.update_watchlist(identity["security_id"], True)
        self.assertEqual(set(saved["securities"][0]), set(lab.IDENTITY_FIELDS))
        source.unlink()
        self.assertEqual(ResearchStore(self.root, LAB_TOOL).search("999999")["securities"][0]["identity_source"], "watchlist")

    def test_local_index_rejects_hardlinked_header_without_reading_it(self):
        market = self.base / "local-index"
        market.mkdir()
        outside = self.base / "outside.csv"
        outside.write_text("private data", encoding="utf-8")
        os.link(outside, market / "sz999999.csv")
        with self.assertRaisesRegex(ValueError, "ordinary file"):
            ResearchStore(self.root, LAB_TOOL, market).search("999999")

    def test_exact_local_identity_and_bare_code_only_open_matching_headers(self):
        market = self.base / "local-index"
        market.mkdir()
        for prefix, code, company in [("sz", "999999", "虚构甲"), ("sh", "999999", "虚构乙"),
                                      ("bj", "999999", "虚构丙"), ("sz", "999998", "不相关公司")]:
            (market / (prefix + code + ".csv")).write_text(
                "fictional source\n股票代码,股票名称\n" + prefix + code + "," + company + "\n", encoding="utf-8")
        original_open = Path.open
        for query, expected in [("A-share-sz-999999", ["sz999999.csv"]), ("sz999999", ["sz999999.csv"]),
                                ("999999", ["sh999999.csv", "sz999999.csv", "bj999999.csv"])]:
            store = ResearchStore(self.root, LAB_TOOL, market)
            opened = []
            def track(path, *args, **kwargs):
                if path.parent == market:
                    opened.append(path.name)
                return original_open(path, *args, **kwargs)
            with self.subTest(query=query), patch.object(Path, "open", autospec=True, side_effect=track), \
                 patch.object(Path, "glob", side_effect=AssertionError("Exact lookup cannot enumerate the market")):
                found = store.search(query)["securities"]
            self.assertEqual(opened, expected)
            self.assertEqual(len(found), len(expected))
            self.assertIsNone(store._market_index)

    def test_context_and_watchlist_use_one_verified_header_without_a_name_index(self):
        market = self.base / "local-index"
        market.mkdir()
        target = market / "sz999999.csv"
        target.write_text("fictional source\n股票代码,股票名称\nsz999999,虚构公司\n", encoding="utf-8")
        original_open = Path.open
        for operation in ("context", "watchlist"):
            store = ResearchStore(self.root, LAB_TOOL, market)
            opened = []
            def track(path, *args, **kwargs):
                if path.parent == market:
                    opened.append(path.name)
                return original_open(path, *args, **kwargs)
            with self.subTest(operation=operation), patch.object(Path, "open", autospec=True, side_effect=track), \
                 patch.object(Path, "glob", side_effect=AssertionError("Exact identity cannot enumerate the market")):
                value = (store.context("A-share-sz-999999") if operation == "context"
                         else store.update_watchlist("A-share-sz-999999", True))
            self.assertEqual(opened, ["sz999999.csv"])
            self.assertIsNone(store._market_index)
            self.assertEqual(value["security"]["code"] if operation == "context" else value["securities"][0]["code"], "999999")

    def test_targeted_missing_or_conflicting_header_does_not_invent_identity(self):
        market = self.base / "local-index"
        market.mkdir()
        (market / "sz999999.csv").write_text("fictional source\n股票代码,股票名称\nsh999999,冲突身份\n", encoding="utf-8")
        store = ResearchStore(self.root, LAB_TOOL, market)
        for query in ("999998", "A-share-sh-999999", "UNKNOWN-EX-999999"):
            with self.subTest(query=query):
                self.assertEqual(store.search(query)["securities"], [])
        conflict = store.search("A-share-sz-999999")
        self.assertEqual(conflict["securities"], [])
        self.assertEqual(len(conflict["warnings"]), 1)
        with self.assertRaises(KeyError):
            store.context("A-share-sz-999999")
        with self.assertRaises(KeyError):
            store.update_watchlist("A-share-sz-999999", True)
        self.assertIsNone(store._market_index)

    def test_watchlist_historical_name_before_formal_archive_uses_canonical_name(self):
        market = self.base / "local-index"
        market.mkdir()
        (market / "sz999999.csv").write_text("fictional\n股票代码,股票名称\nsz999999,N虚构\n", encoding="utf-8")
        store = ResearchStore(self.root, LAB_TOOL, market)
        candidate = store.search("999999")["securities"][0]
        self.assertEqual(candidate["name_status"], "historical_name")
        self.assertIn("历史名称", candidate["name_note"])
        store.update_watchlist(candidate["security_id"], True)
        watchlist_bytes = self.watch_path().read_bytes()
        identity = {key: candidate[key] for key in lab.IDENTITY_FIELDS}
        canonical = {**identity, "company": "虚构股份有限公司"}
        self.archive(security=canonical)
        self.assertEqual(store.overview()["securities"][0]["company"], canonical["company"])
        self.assertEqual(store.context(canonical["security_id"])["security"], canonical)
        self.assertEqual(ResearchStore(self.root, LAB_TOOL).watchlist()["securities"], [canonical])
        self.assertEqual(store.search("N虚构")["securities"][0]["company"], canonical["company"])
        self.assertEqual(self.watch_path().read_bytes(), watchlist_bytes)

    def test_watchlist_alias_does_not_relax_the_other_six_identity_fields(self):
        canonical = dict(SECURITY)
        lab.ensure_security(self.root, canonical)
        for field in ("market", "exchange", "code", "share_class", "currency"):
            identity = {**canonical, "company": "Old name", field: "DIFFERENT"}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Conflicting"):
                self.store._merge({canonical["security_id"]: canonical}, [identity])

    def test_linked_watchlist_or_archive_paths_are_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, self.root / "platform", target_is_directory=True)
        except OSError as error:
            self.skipTest("Local account cannot create symbolic links: " + str(error))
        with self.assertRaises(ValueError):
            self.store.watchlist()
        with self.assertRaises(ValueError):
            self.store.update_watchlist(SECURITY["security_id"], True)


if __name__ == "__main__":
    unittest.main()
