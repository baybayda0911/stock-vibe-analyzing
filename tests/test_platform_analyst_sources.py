"""Fictional report indexes: dates are labels, not read reports or formal research."""

import csv
import importlib.util
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.analyst_sources import FIELDS, _rows, analyst_updates
from app.store import ResearchStore


TOOL = Path(__file__).resolve().parents[1] / "skills/stock-research-lab/scripts/research_lab.py"
spec = importlib.util.spec_from_file_location("analyst_fixture_lab", TOOL)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
SECURITY = {"security_id": "A-share-sz-300001", "company": "虚构研究样本", "market": "A-share",
            "exchange": "sz", "code": "300001", "share_class": "A", "currency": "CNY"}
NOW = "2026-11-14T17:00:00+08:00"


class FakeStore:
    _regular = ResearchStore._regular
    lab = lab

    def context(self, security_id):
        if security_id != SECURITY["security_id"]:
            raise KeyError(security_id)
        return {"security": dict(SECURITY), "roles": {"formal_fundamental": {
            "result_id": "FORMAL-F", "research_asof": "2026-11-10T18:00:00+08:00",
            "generated_at": "2026-11-10T19:00:00+08:00"}}}


def row(published="2026-11-11", **overrides):
    value = dict(zip(FIELDS, ["sz300001", published, "虚构机构", "虚构研究员", "公司研究,行业研究",
                    "测试标题，包含逗号", "1_买入", "维持", "--", "2026-11-13 09:00:00", "2026-11-13 10:00:00"]))
    value.update(overrides)
    return value


class AnalystSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "analysts"
        self.directory.mkdir()
        self.file = self.directory / "sz300001.csv"
        self.store = FakeStore()
        self.clock = patch.object(lab, "now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def write(self, records, encoding="gb18030"):
        text = io.StringIO(newline="")
        writer = csv.writer(text)
        writer.writerow(["虚构来源说明，不是表头"])
        writer.writerow(FIELDS)
        for record in records:
            writer.writerow([record[name] for name in FIELDS])
        self.file.write_bytes(text.getvalue().encode(encoding))

    def read(self, **kwargs):
        return analyst_updates(self.store, SECURITY["security_id"], self.directory, **kwargs)

    def test_gbk_quoted_fields_original_dates_and_no_invented_body(self):
        self.write([row()])
        result = self.read()
        record = result["records"][0]
        self.assertEqual(record["publication_date"], "2026-11-11")
        self.assertEqual(record["report_category"], "公司研究,行业研究")
        self.assertEqual(result["formal_fundamental"]["result_id"], "FORMAL-F")
        self.assertTrue(record["newer_than_formal"])
        self.assertEqual(record["content_status"], "metadata_only")
        self.assertEqual(record["views"], [])
        self.assertEqual(record["forecasts"], [])
        self.assertIsNone(record["acquired_at"])
        self.assertIsNone(record["imported_at"])
        self.assertFalse(record["can_replace_formal"])
        self.assertFalse(result["formal_import_enabled"])

    def test_unknown_month_precision_and_future_dates_remain_honest(self):
        self.write([row("--"), row("2026-11"), row("2026-12-01"), row("2026-13-02")])
        result = self.read()
        self.assertEqual(result["summary"]["future_date_excluded"], 1)
        self.assertEqual(result["summary"]["unknown_date_count"], 2)
        month = next(item for item in result["records"] if item["publication_date_precision"] == "month")
        self.assertEqual(month["publication_date"], "2026-11")
        self.assertIsNone(month["newer_than_formal"])
        self.assertEqual(result["summary"]["newer_than_formal_count"], 0)

    def test_source_ingestion_or_mtime_changes_do_not_create_new_report_identity(self):
        self.write([row()])
        original = self.read()["records"][0]
        self.write([row(**{"入库时间": "2026-11-14 23:00:00", "抓取时间": "2026-11-15 01:00:00"})])
        os.utime(self.file, (1800000000, 1800000000))
        changed = self.read()["records"][0]
        self.assertEqual(original["source_id"], changed["source_id"])
        self.assertNotEqual(original["row_sha256"], changed["row_sha256"])
        self.assertEqual(changed["publication_date"], "2026-11-11")
        self.assertEqual(changed["discovered_at"], NOW)

    def test_metadata_revisions_retained_and_acquisition_only_duplicates_grouped(self):
        self.write([row(), row(), row(**{"抓取时间": "2026-11-14 09:00:00"}),
                    row(**{"研究人员": "另一虚构作者"})])
        result = self.read()
        self.assertEqual(result["summary"]["records_total"], 2)
        self.assertEqual({item["revision_count"] for item in result["records"]}, {2})
        self.assertEqual(len({item["revision_group_id"] for item in result["records"]}), 1)
        self.assertEqual(sorted(len(item["index_observations"]) for item in result["records"]), [1, 2])

    def test_utf8_index_and_unknown_target_price_are_preserved(self):
        self.write([row(**{"目标价位": "10—12（期限未知）"})], encoding="utf-8-sig")
        record = self.read()["records"][0]
        self.assertEqual(record["target_price_raw"], "10—12（期限未知）")
        self.assertNotIn("target_price", record)

    def test_wrong_stock_row_and_mismatched_context_are_rejected(self):
        self.write([row(**{"股票代码": "sz300002"})])
        with self.assertRaisesRegex(ValueError, "证券代码"):
            self.read()
        with self.assertRaisesRegex(ValueError, "证券身份"):
            self.read(context={"security": {**SECURITY, "security_id": "A-share-sz-300002", "code": "300002"}})

    def test_no_index_configuration_or_file_does_not_mean_no_reports(self):
        unconfigured = analyst_updates(self.store, SECURITY["security_id"])
        self.assertEqual(unconfigured["status"], "index_unavailable")
        missing = self.read()
        self.assertEqual(missing["records"], [])
        self.assertIsNone(missing["source"])
        self.assertIn("不据此判断不存在研报", "".join(missing["limitations"]))

    def test_historical_cutoff_cannot_read_today_index_as_old_knowledge(self):
        self.write([row()])
        result = self.read(asof="2026-11-13T23:00:00+08:00")
        self.assertEqual(result["status"], "historical_unavailable")
        self.assertEqual(result["records"], [])
        self.assertIsNone(result["source"])
        with self.assertRaisesRegex(ValueError, "未来"):
            self.read(asof="2026-11-15T23:00:00+08:00")

    def test_invalid_limits_and_untrusted_path_guard_fail_closed(self):
        self.write([row()])
        for limit in [True, 0, 101, "10"]:
            with self.assertRaises(ValueError):
                self.read(limit=limit)
        with patch.object(self.store, "_regular", side_effect=ValueError("linked source")):
            with self.assertRaisesRegex(ValueError, "linked source"):
                self.read()

    def test_malformed_empty_or_duplicate_headers_rejected(self):
        for text in ["", "wrong,columns\na,b\n", ",".join([*FIELDS, FIELDS[0]]) + "\n"]:
            with self.assertRaises(ValueError):
                _rows(text.encode("utf-8"))
        self.file.write_text(",".join(FIELDS) + "\nsz300001,2026-11-11\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "列数"):
            self.read()


if __name__ == "__main__":
    unittest.main()
