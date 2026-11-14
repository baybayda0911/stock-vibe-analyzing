import copy
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lab = load("archive_test_lab", "skills/stock-research-lab/scripts/research_lab.py")
archive = load("archive_test_backup", "skills/stock-research-archive/scripts/archive_backup.py")
SECURITY = {"security_id": "ZZ-TEST-DEMO", "company": "Fictional Company", "market": "ZZ",
            "exchange": "TEST", "code": "DEMO", "share_class": "ordinary", "currency": "TST"}
AT = "2026-01-02T18:00:00+08:00"


class ArchiveBackupTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "private-lab"
        self.snapshot = self.base / "snapshot"
        self.output = self.base / "restored"
        self.report = self.base / "report.md"
        self.report.write_text("# Fictional research\n[data](assets/data.json)\n", encoding="utf-8")
        self.attachment = self.base / "data.json"
        self.attachment.write_bytes(b'{"fictional":true}\n')
        lab.init(self.root)

    def request(self, result="R1", item="I1", **changes):
        request = {"security": SECURITY, "module": "fundamental", "module_version": "0.1.0",
                   "result_id": result, "generated_at": AT, "research_asof": AT, "data_asof": None,
                   "parent_result_ids": [], "report_file": str(self.report), "evidence": [],
                   "items": [{"item_id": item, "type": "observation", "text": "Watch fictional demand",
                              "report_locator": "section 1", "frozen_at": AT, "status": "pending",
                              "definition": {"what": "demand", "why": "test", "next_evidence": "filing"},
                              "evidence_ids": [], "current_use": "needs_review"}],
                   "attachments": [{"source_file": str(self.attachment), "relative_path": "assets/data.json",
                                    "media_type": "application/json"}]}
        request.update(changes)
        return request

    def populate(self):
        first = lab.archive(self.root, self.request())
        second = self.request("R2", "I2", parent_result_ids=["R1"])
        second["items"][0]["parent_item_ids"] = ["I1"]
        lab.archive(self.root, second)
        lab.review(self.root, {"security_id": SECURITY["security_id"], "review_id": "RV1", "generated_at": AT,
                               "research_asof": AT, "data_asof": None, "report_file": str(self.report), "evidence": [],
                               "updates": [{"item_id": "I1", "status": "cannot_determine",
                                            "reason": "Fictional review", "current_use": "needs_review",
                                            "evidence_ids": []}]})
        lab.plan(self.root, {"security_id": SECURITY["security_id"], "plan_id": "P1", "plan_version_id": "PV1",
                             "version": 1, "source_result_ids": ["R1", "R2"], "generated_at": AT, "frozen_at": AT,
                             "effective_at": AT, "valid_until": "2026-02-02T18:00:00+08:00", "trigger": "test",
                             "confirmation": "test", "invalidation": "test", "exit": "test",
                             "adoption": {"status": "research_only"}})
        lab.execution(self.root, {"security_id": SECURITY["security_id"], "import_id": "X1",
                                  "execution_kind": "simulated", "source_assertion": "user_statement",
                                  "generated_at": AT, "source_file": str(self.report),
                                  "trades": [{"trade_id": "T1", "executed_at": AT, "time_precision": "minute",
                                              "side": "buy", "quantity": "2", "price": "3", "currency": "TST",
                                              "fees": None, "allocations": [{"plan_id": "P1",
                                                                             "plan_version_id": "PV1", "quantity": "1"}]}]})
        lab.import_report(self.root, self.request("R3", "I3", import_id="IMPORT1", source_file=str(self.report),
                                                 original_generated_at=AT, original_date_evidence="Fictional test claim"))
        (self.root / "securities" / "notes.bin").write_bytes(b"extra ordinary file\x00\xff")
        return self.root / first["path"]

    def make_snapshot(self):
        self.populate()
        archive.backup(lab, self.root, self.snapshot)
        return lab.read_json(self.snapshot / "snapshot.json")

    def write_manifest(self, manifest):
        (self.snapshot / "snapshot.json").write_text(json.dumps(manifest), encoding="utf-8")

    def write_watchlist(self, value=None):
        path = self.root / "platform" / "watchlist.json"
        path.parent.mkdir(exist_ok=True)
        value = ({"schema_version": "stock-research-watchlist/1", "updated_at": AT,
                  "securities": [dict(SECURITY)]} if value is None else value)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def assert_no_stage(self, parent=None):
        self.assertEqual(list((parent or self.base).glob(".archive-*")), [])
        self.assertFalse((self.root / ".write.lock").exists())

    def test_roundtrip_preserves_all_ledgers_attachments_paths_and_record_dates(self):
        self.populate()
        (self.root / "_staging" / "unfinished.txt").write_text("excluded", encoding="utf-8")
        (self.root / "_workspace").mkdir()
        (self.root / "_workspace" / "view.html").write_text("excluded", encoding="utf-8")
        (self.root / "catalog.json").write_text("broken derived catalog", encoding="utf-8")
        original = {name: path.read_bytes() for name, path in archive._source_files(lab, self.root).items()}
        result = archive.backup(lab, self.root, self.snapshot)
        checked = archive.verify_snapshot(lab, self.snapshot)
        self.assertTrue(result["saved"] and checked["verified"])
        self.assertEqual(result["counts"], {"files": len(original), "bytes": sum(map(len, original.values()))})
        self.assertFalse((self.snapshot / "payload" / "catalog.json").exists())
        self.assertFalse((self.snapshot / "payload" / "_staging").exists())
        restored = archive.restore(lab, self.snapshot, self.output)
        self.assertEqual(restored["catalog_counts"], {"results": 3, "reviews": 1, "plans": 1,
                                                     "executions": 1, "imports": 1})
        self.assertEqual({name: path.read_bytes() for name, path in archive._source_files(lab, self.output).items()}, original)
        self.assertEqual(list((self.output / "_staging").iterdir()), [])
        self.assertFalse((self.output / "_workspace").exists())
        self.assertEqual((self.root / "catalog.json").read_text(encoding="utf-8"), "broken derived catalog")
        catalog = lab.scan(self.output)
        self.assertEqual(catalog["results"][1]["parent_result_ids"], ["R1"])
        self.assertEqual(catalog["results"][1]["items"][0]["parent_item_ids"], ["I1"])
        self.assertEqual(catalog["results"][2]["original_generated_at"], AT)
        self.assert_no_stage()

    def test_workspace_backup_and_restore_do_not_recurse_into_private_workspace(self):
        self.populate()
        backups = self.root / "_workspace" / "backups"
        restores = self.root / "_workspace" / "restore-tests"
        snapshot, output = backups / "first", restores / "first"
        archive.backup(lab, self.root, snapshot)
        archive.restore(lab, snapshot, output)
        second = backups / "second"
        archive.backup(lab, self.root, second)
        manifest = archive.verify_snapshot(lab, second)["manifest"]
        self.assertTrue(all(not entry["path"].startswith("_workspace/") for entry in manifest["files"]))
        self.assertEqual(lab.scan(self.root)["results"], lab.scan(output)["results"])
        self.assert_no_stage(backups)
        self.assert_no_stage(restores)

    def test_empty_lab_roundtrip(self):
        archive.backup(lab, self.root, self.snapshot)
        restored = archive.restore(lab, self.snapshot, self.output)
        self.assertEqual(restored["counts"]["files"], 2)
        self.assertEqual(lab.scan(self.output)["results"], [])

    def test_watchlist_roundtrip_preserves_bytes_without_entering_catalog_or_copying_jobs_secrets(self):
        original = self.write_watchlist().read_bytes()
        for name in ("platform/jobs/job.json", "platform/secrets.json", "secrets.json",
                     "_workspace/jobs/log.txt", "_workspace/backups/old/snapshot.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fictional excluded material", encoding="utf-8")
        archive.backup(lab, self.root, self.snapshot)
        checked = archive.verify_snapshot(lab, self.snapshot)
        self.assertEqual(checked["manifest"]["schema_version"], "stock-research-archive/2")
        self.assertEqual({entry["path"] for entry in checked["manifest"]["files"]},
                         {"README.md", "config.local.json", "platform/watchlist.json"})
        restored = archive.restore(lab, self.snapshot, self.output)
        self.assertEqual((self.output / "platform" / "watchlist.json").read_bytes(), original)
        self.assertEqual(restored["catalog_counts"], {"results": 0, "reviews": 0, "plans": 0,
                                                     "executions": 0, "imports": 0})
        self.assertEqual(set((self.output / "platform").iterdir()), {self.output / "platform" / "watchlist.json"})
        self.assertFalse((self.output / "_workspace").exists())
        self.assertEqual(list((self.output / "securities").iterdir()), [])
        self.assert_no_stage()

    def test_legacy_snapshot_without_watchlist_still_verifies_and_restores(self):
        archive.backup(lab, self.root, self.snapshot)
        manifest = lab.read_json(self.snapshot / "snapshot.json")
        manifest["schema_version"] = "stock-research-archive/1"
        self.write_manifest(manifest)
        self.assertTrue(archive.verify_snapshot(lab, self.snapshot)["verified"])
        archive.restore(lab, self.snapshot, self.output)
        self.assertFalse((self.output / "platform").exists())
        self.assertEqual(archive._entries(lab, archive._source_files(lab, self.output)), manifest["files"])

    def test_empty_watchlist_roundtrip(self):
        original = self.write_watchlist({"schema_version": "stock-research-watchlist/1", "updated_at": AT,
                                         "securities": []}).read_bytes()
        archive.backup(lab, self.root, self.snapshot)
        archive.restore(lab, self.snapshot, self.output)
        self.assertEqual((self.output / "platform" / "watchlist.json").read_bytes(), original)
        self.assertEqual(lab.scan(self.output)["results"], [])

    def test_legacy_manifest_rejects_watchlist_and_v2_rejects_other_platform_paths(self):
        self.write_watchlist()
        archive.backup(lab, self.root, self.snapshot)
        original = lab.read_json(self.snapshot / "snapshot.json")
        legacy = copy.deepcopy(original)
        legacy["schema_version"] = "stock-research-archive/1"
        self.write_manifest(legacy)
        with self.assertRaisesRegex(ValueError, "scope"):
            archive.verify_snapshot(lab, self.snapshot)
        watchlist = next(entry for entry in original["files"] if entry["path"] == "platform/watchlist.json")
        for name in ("platform/secrets.json", "platform/jobs/job.json", "platform/Watchlist.json",
                     "other-platform/watchlist.json", "platform/watchlist.json/extra"):
            manifest = copy.deepcopy(original)
            manifest["files"].append(dict(watchlist, path=name))
            self.write_manifest(manifest)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "scope"):
                archive.restore(lab, self.snapshot, self.output)
            self.assertFalse(self.output.exists())
        self.write_manifest(original)
        (self.snapshot / "payload" / "platform" / "secrets.json").write_text("fictional", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "extra or missing"):
            archive.verify_snapshot(lab, self.snapshot)
        self.assert_no_stage()

    def test_watchlist_schema_timestamp_identities_and_duplicates_are_validated(self):
        valid = lab.read_json(self.write_watchlist())
        invalid = [{**valid, "schema_version": "future"}, {**valid, "extra": "secret"},
                   {**valid, "updated_at": "2026-01-02"}, {**valid, "securities": {}},
                   {**valid, "securities": [SECURITY, SECURITY]},
                   {**valid, "securities": [{**SECURITY, "extra": "secret"}]},
                   {**valid, "securities": [{**SECURITY, "company": ""}]},
                   {**valid, "securities": [{**SECURITY, "security_id": "ZZ-TEST-OTHER"}]},
                   {**valid, "securities": [{key: value for key, value in SECURITY.items() if key != "currency"}]}]
        for value in invalid:
            self.write_watchlist(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                archive.backup(lab, self.root, self.snapshot)
            self.assertFalse(self.snapshot.exists())
            self.assert_no_stage()

    def test_snapshot_watchlist_is_validated_even_with_matching_fingerprint(self):
        self.write_watchlist()
        archive.backup(lab, self.root, self.snapshot)
        path = self.snapshot / "payload" / "platform" / "watchlist.json"
        path.write_text('{"schema_version":"future"}', encoding="utf-8")
        manifest = lab.read_json(self.snapshot / "snapshot.json")
        for entry in manifest["files"]:
            if entry["path"] == "platform/watchlist.json":
                entry.update(archive._fingerprint(lab, path))
        self.write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "watchlist schema"):
            archive.restore(lab, self.snapshot, self.output)
        self.assertFalse(self.output.exists())
        self.assert_no_stage()

    def test_watchlist_directories_and_linked_parents_are_rejected(self):
        path = self.root / "platform" / "watchlist.json"
        path.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "ordinary"):
            archive.backup(lab, self.root, self.snapshot)
        path.rmdir()
        original = lab.linked
        for marked in (path, path.parent):
            with self.subTest(marked=marked), patch.object(lab, "linked", side_effect=lambda candidate: candidate == marked or original(candidate)):
                if marked == path:
                    path.write_text("fictional", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "reparse"):
                    archive.backup(lab, self.root, self.snapshot)
                path.unlink(missing_ok=True)
        path.parent.rmdir()
        path.parent.write_text("fictional file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "parent must be a directory"):
            archive.backup(lab, self.root, self.snapshot)
        self.assertFalse(self.snapshot.exists())
        self.assert_no_stage()

    def test_watchlist_hardlink_metadata_is_rejected(self):
        watchlist = self.write_watchlist()
        original = Path.stat

        def file_stat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2) if path == watchlist else info

        with patch.object(Path, "stat", new=file_stat), self.assertRaisesRegex(ValueError, "without links"):
            archive.backup(lab, self.root, self.snapshot)
        self.assertFalse(self.snapshot.exists())
        self.assert_no_stage()

    def test_watchlist_changes_addition_or_removal_abort_under_lab_writer_lock(self):
        original_copy = lab.copy_source
        watchlist = self.write_watchlist()
        for change in ("bytes", "added", "removed"):
            self.write_watchlist()
            if change == "added":
                watchlist.unlink()
            changed = False

            def copying(source, destination):
                nonlocal changed
                self.assertTrue((self.root / ".write.lock").exists())
                result = original_copy(source, destination)
                if not changed:
                    changed = True
                    if change == "removed":
                        watchlist.unlink()
                    else:
                        value = {"schema_version": "stock-research-watchlist/1", "updated_at": AT,
                                 "securities": [{**SECURITY, "company": "Changed fictional company"}]}
                        self.write_watchlist(value)
                return result

            with self.subTest(change=change), patch.object(lab, "copy_source", side_effect=copying):
                with self.assertRaises((ValueError, OSError)):
                    archive.backup(lab, self.root, self.snapshot)
            self.assertFalse(self.snapshot.exists())
            self.assert_no_stage()

    def test_refuses_existing_outputs_without_modifying_them(self):
        self.make_snapshot()
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_bytes(b"keep")
        for call in (lambda: archive.backup(lab, self.root, self.snapshot),
                     lambda: archive.restore(lab, self.snapshot, self.output)):
            with self.assertRaises(FileExistsError):
                call()
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertTrue(archive.verify_snapshot(lab, self.snapshot)["verified"])
        self.assert_no_stage()

    def test_refuses_relative_root_source_and_ledger_subtree_outputs(self):
        self.make_snapshot()
        other = self.base / "other-lab"
        lab.init(other)
        for output in (Path("relative"), Path(self.base.anchor), self.root / "bad",
                       self.root / "securities" / "bad", self.root / "_staging" / "bad",
                       other / "securities" / "bad"):
            with self.subTest(output=output), self.assertRaises((ValueError, FileExistsError)):
                archive.backup(lab, self.root, output)
        for output in (self.snapshot / "bad", self.snapshot / "payload" / "bad",
                       other / "securities" / "bad", self.root / "securities" / "bad"):
            with self.subTest(output=output), self.assertRaises((ValueError, FileExistsError)):
                archive.restore(lab, self.snapshot, output)
        with self.assertRaises(ValueError):
            archive.backup(lab, Path("relative"), self.base / "bad")
        self.assert_no_stage()

    def test_refuses_skill_source_and_installation_outputs_without_git(self):
        archive.backup(lab, self.root, self.snapshot)
        skill = self.base / "standalone-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("Fictional skill entry", encoding="utf-8")
        output = skill / "private" / "new"
        for call in (lambda: archive.backup(lab, self.root, output),
                     lambda: archive.restore(lab, self.snapshot, output)):
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, "Skill source or installation"):
                call()
            self.assertFalse(output.parent.exists())
        self.assert_no_stage()

    def test_refuses_git_ancestors_for_backup_verify_and_restore(self):
        self.make_snapshot()
        private_git = self.base / "git-worktree"
        private_git.mkdir()
        (private_git / ".git").write_text("gitdir: fictional", encoding="utf-8")
        for call in (lambda: archive.backup(lab, self.root, private_git / "new"),
                     lambda: archive.restore(lab, self.snapshot, private_git / "new")):
            with self.assertRaisesRegex(ValueError, "Git working tree"):
                call()
        (self.base / ".git").mkdir()
        for call in (lambda: archive.verify_snapshot(lab, self.snapshot),
                     lambda: archive.backup(lab, self.root, self.base / "new")):
            with self.assertRaisesRegex(ValueError, "Git working tree"):
                call()

    def test_reparse_guards_cover_source_snapshot_and_output_ancestor(self):
        self.make_snapshot()
        original = lab.linked
        source = self.root / "securities" / "notes.bin"
        payload = self.snapshot / "payload" / "securities" / "notes.bin"
        for marked, call in ((source, lambda: archive.backup(lab, self.root, self.base / "new")),
                             (payload, lambda: archive.verify_snapshot(lab, self.snapshot)),
                             (self.base, lambda: archive.restore(lab, self.snapshot, self.output))):
            with self.subTest(path=marked), patch.object(lab, "linked", side_effect=lambda path: path == marked or original(path)):
                with self.assertRaisesRegex(ValueError, "reparse"):
                    call()
        self.assert_no_stage()

    def test_real_symlink_is_rejected_when_os_permission_allows(self):
        target = self.base / "outside.bin"
        target.write_bytes(b"outside")
        try:
            (self.root / "securities" / "linked.bin").symlink_to(target)
        except OSError as error:
            self.skipTest(f"OS symlink creation unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "reparse"):
            archive.backup(lab, self.root, self.snapshot)
        self.assert_no_stage()

    def test_hardlinks_are_rejected(self):
        target = self.base / "outside.bin"
        target.write_bytes(b"outside")
        try:
            os.link(target, self.root / "securities" / "hardlink.bin")
        except OSError as error:
            self.skipTest(f"OS hardlink creation unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "without links"):
            archive.backup(lab, self.root, self.snapshot)
        self.assert_no_stage()

    def test_hardlink_metadata_is_rejected_without_os_link_permission(self):
        notes = self.root / "securities" / "notes.bin"
        notes.write_bytes(b"fictional")
        original = Path.stat

        def file_stat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2) if path == notes else info

        with patch.object(Path, "stat", new=file_stat), self.assertRaisesRegex(ValueError, "without links"):
            archive.backup(lab, self.root, self.snapshot)
        self.assertFalse(self.snapshot.exists())
        self.assert_no_stage()

    def test_manifest_rejects_traversal_reserved_ads_case_duplicates_and_overlaps(self):
        original = self.make_snapshot()
        record = next(entry for entry in original["files"] if entry["path"].startswith("securities/"))
        invalid = ("../escape", "/absolute", "C:/escape", "securities/../escape", "securities//a",
                   "securities/./a", "securities\\a", "securities/CON.txt", "securities/NUL .txt",
                   "securities/COM¹.txt", "securities/a:ads", "securities/a.", "securities/a ",
                   "securities/.git/config", "securities/a?.txt", "catalog.json")
        for name in invalid:
            manifest = copy.deepcopy(original)
            manifest["files"].append(dict(record, path=name))
            self.write_manifest(manifest)
            with self.subTest(name=name), self.assertRaises(ValueError):
                archive.restore(lab, self.snapshot, self.output)
            self.assertFalse(self.output.exists())
        for name in (record["path"], record["path"].replace("ZZ-TEST", "zz-test"), record["path"] + "/child"):
            manifest = copy.deepcopy(original)
            manifest["files"].append(dict(record, path=name))
            self.write_manifest(manifest)
            with self.subTest(name=name), self.assertRaises(ValueError):
                archive.verify_snapshot(lab, self.snapshot)
        self.assert_no_stage()

    def test_manifest_metadata_is_strict(self):
        original = self.make_snapshot()
        changes = [{"schema_version": "future"}, {"schema_version": []}, {"kind": "other"}, {"lab_schema_version": "future"},
                   {"created_at": "2026-01-02"}, {"extra": "unknown"}, {"files": {}}]
        for changeset in changes:
            manifest = {**copy.deepcopy(original), **changeset}
            self.write_manifest(manifest)
            with self.subTest(changes=changeset), self.assertRaises(ValueError):
                archive.verify_snapshot(lab, self.snapshot)
        for changeset in ({"size_bytes": True}, {"size_bytes": -1}, {"sha256": "a"}, {"extra": 1}):
            manifest = copy.deepcopy(original)
            manifest["files"][0].update(changeset)
            self.write_manifest(manifest)
            with self.subTest(changes=changeset), self.assertRaises(ValueError):
                archive.verify_snapshot(lab, self.snapshot)

    def test_payload_extra_missing_tamper_and_unlisted_empty_directories_are_rejected(self):
        manifest = self.make_snapshot()
        selected = self.snapshot / "payload" / manifest["files"][0]["path"]
        original = selected.read_bytes()
        selected.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            archive.restore(lab, self.snapshot, self.output)
        selected.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            archive.verify_snapshot(lab, self.snapshot)
        selected.write_bytes(original)
        for relative, directory in (("payload/extra.bin", False), ("payload/extra", True), ("extra", True)):
            extra = self.snapshot / relative
            extra.mkdir() if directory else extra.write_bytes(b"extra")
            with self.subTest(extra=relative), self.assertRaises(ValueError):
                archive.verify_snapshot(lab, self.snapshot)
            extra.rmdir() if directory else extra.unlink()
        self.assertFalse(self.output.exists())
        self.assert_no_stage()

    def test_full_file_hash_covers_ledger_bytes_not_hashed_by_lab_scan(self):
        manifest = self.make_snapshot()
        review = next(entry for entry in manifest["files"] if entry["path"].endswith("review.json"))
        path = self.snapshot / "payload" / review["path"]
        payload = path.read_bytes()
        path.write_bytes(payload.replace(b"Fictional review", b"Different review"))
        lab.scan(self.snapshot / "payload")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            archive.verify_snapshot(lab, self.snapshot)

    def test_source_file_changes_and_new_files_abort_without_publication(self):
        self.populate()
        original_copy = lab.copy_source
        notes = self.root / "securities" / "notes.bin"
        for change in ("bytes", "new-file"):
            before = notes.read_bytes()
            changed = False

            def copying(source, destination):
                nonlocal changed
                self.assertTrue((self.root / ".write.lock").exists())
                result = original_copy(source, destination)
                if not changed:
                    changed = True
                    if change == "bytes":
                        notes.write_bytes(b"x" * len(before))
                    else:
                        (self.root / "securities" / "new.bin").write_bytes(b"new")
                return result

            with self.subTest(change=change), patch.object(lab, "copy_source", side_effect=copying):
                with self.assertRaisesRegex(ValueError, "changed"):
                    archive.backup(lab, self.root, self.snapshot)
            self.assertFalse(self.snapshot.exists())
            self.assert_no_stage()
            notes.write_bytes(before)
            (self.root / "securities" / "new.bin").unlink(missing_ok=True)

    def test_source_read_copy_manifest_and_publish_failures_clean_only_owned_staging(self):
        self.populate()
        unrelated = self.base / ".archive-user-owned"
        unrelated.mkdir()
        (unrelated / "keep").write_bytes(b"keep")
        original_write = lab.write_json

        def broken_copy(source, destination):
            destination.write_bytes(b"partial")
            raise OSError("simulated source read or destination write failure")

        def broken_manifest(path, value):
            if path.name == "snapshot.json":
                raise OSError("simulated manifest write failure")
            return original_write(path, value)

        for failing in (patch.object(archive, "_fingerprint", side_effect=OSError("read failure")),
                        patch.object(lab, "copy_source", side_effect=broken_copy),
                        patch.object(lab, "write_json", side_effect=broken_manifest),
                        patch.object(archive.os, "rename", side_effect=OSError("publish failure"))):
            with failing, self.assertRaises(OSError):
                archive.backup(lab, self.root, self.snapshot)
            self.assertFalse(self.snapshot.exists())
            self.assertEqual(list(self.base.glob(".archive-*")), [unrelated])
            self.assertFalse((self.root / ".write.lock").exists())
            self.assertEqual((unrelated / "keep").read_bytes(), b"keep")

    def test_restore_copy_and_catalog_failures_leave_no_partial_output(self):
        self.make_snapshot()
        for failing in (patch.object(lab, "copy_source", side_effect=OSError("read failure")),
                        patch.object(lab, "reindex", side_effect=OSError("catalog failure"))):
            with failing, self.assertRaises(OSError):
                archive.restore(lab, self.snapshot, self.output)
            self.assertFalse(self.output.exists())
            self.assert_no_stage()
        self.assertTrue(archive.verify_snapshot(lab, self.snapshot)["verified"])

    def test_existing_writer_lock_prevents_backup(self):
        with lab.writer(self.root):
            with self.assertRaises(FileExistsError):
                archive.backup(lab, self.root, self.snapshot)
            self.assertTrue((self.root / ".write.lock").exists())
        self.assertFalse(self.snapshot.exists())
        self.assert_no_stage()


if __name__ == "__main__":
    unittest.main()
