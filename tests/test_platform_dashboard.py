"""Fictional fixtures with the same shape as archived technical report v2."""

from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.dashboard import _json, _resolve
from app.store import ResearchStore


REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "skills/stock-research-lab/scripts/research_lab.py"
spec = importlib.util.spec_from_file_location("dashboard_fixture_lab", TOOL)
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
SECURITY = {"security_id": "ZZ-TEST-DEMO", "company": "虚构甲公司（界面验证）", "market": "ZZ", "exchange": "TEST",
            "code": "DEMO", "share_class": "ordinary", "currency": "TST"}
ASOF = "2026-11-13T23:00:00+08:00"


def technical_document():
    frames = {}
    for period, count in (("1d", 365), ("1w", 100), ("1mo", 162), ("6mo", 18), ("1y", 8)):
        if period == "1d":
            dates = [date(2026, 11, 13) - timedelta(days=count - 1 - i) for i in range(count)]
        elif period == "1w":
            dates = [date(2026, 11, 6) - timedelta(days=7 * (count - 1 - i)) for i in range(count)]
        elif period == "1mo":
            dates = []
            for i in range(count):
                month = 2013 * 12 + i
                year, zero = divmod(month + 1, 12)
                dates.append(date(year, zero + 1, 1) - timedelta(days=1))
        elif period == "6mo":
            dates = [date(2017 + (i + 1) // 2, 12 if i % 2 == 0 else 6, 31 if i % 2 == 0 else 30) for i in range(count)]
        else:
            dates = [date(2018 + i, 12, 31) for i in range(count)]
        rows = [{"index": i, "source_index": i, "time": day.isoformat() + "T15:00:00+08:00", "completed": True,
                 "open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i, "volume": 1000 + i,
                 "available_at": "2026-11-13T16:00:00+08:00"} for i, day in enumerate(dates)]
        series = {"sma" + str(n): [None if i < n - 1 else sum(row["close"] for row in rows[i - n + 1:i + 1]) / n
                                  for i in range(count)] for n in (20, 60, 120)}
        series.update({"macd": [None if i < 33 else .3 for i in range(count)],
                       "macd_signal": [None if i < 33 else .2 for i in range(count)],
                       "macd_hist": [None if i < 33 else .1 for i in range(count)],
                       "rsi14": [None if i < 14 else 55 for i in range(count)],
                       "volume_ratio20": [None if i < 20 else 1.2 for i in range(count)]})
        frames[period] = {"status": "ok", "bars": rows, "series": series,
                          "metadata": {"security": dict(SECURITY), "timeframe": period,
                              "context": {"security_id": SECURITY["security_id"], "timeframe": period},
                              "data_asof": rows[-1]["time"], "qualification": "diagnostic",
                              "adjustment": {"mode": "raw", "verified": False},
                              "limitations": ["虚构原价诊断；未授予执行资格"]},
                          "topics": {"conventional": {"latest": {key: values[-1] for key, values in series.items()}}},
                          "facts": [], "propositions": [], "limitations": []}
    return {"schema_version": "stock-technical-report/2", "status": "analyzed_with_qualifications", "security": dict(SECURITY),
            "generated_at": "2026-11-13T18:00:00+08:00", "asof": "2026-11-13T17:00:00+08:00",
            "market_cutoff": "2026-11-13T15:00:00+08:00", "information_cutoff": "2026-11-13T17:00:00+08:00",
            "timeframes": frames, "limitations": ["所有价格与状态仅为虚构验证资料"],
            "review": {"status": "ok", "rows": [{"item_id": "OLD-I1", "condition_status": "cannot_determine",
                "reason": "新旧价格口径不可比，保留旧项；采纳、执行未知。"}], "pnl_computed": False},
            "previous_technical": {"status": "reviewed", "timeframes": {key: {"status": "incomparable",
                "reasons": ["价格口径不同"]} for key in frames}, "result_id": "OLD", "reason": "未形成同口径对照"}}


def build_fixture(base):
    """Build two fictional UI candidates; the second has an identity CSV but no research."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    root, market = base / "private-lab", base / "market-index"
    lab.init(root)
    market.mkdir()
    (market / "sz999998.csv").write_text("fictional source\n股票代码,股票名称\nsz999998,虚构乙公司（尚无研究）\n", encoding="utf-8")
    report = base / "report.md"
    report.write_text("# 虚构技术研究\n\n**日线判断：虚构排列观察。**\n\n"
        "- **方向**：虚构价格与均线状态。\n- **力度**：虚构动量观察，未形成新判断。\n- **来源**：仅供界面验证。\n\n"
        "## 接下来只看两件事\n\n- 核对原价及复权依据。\n- 保留旧条件，不计算命中或收益。\n\n"
        "## 长短周期是否一致\n\n|周期|状态|资料末端|用途|\n|---|---|---|---|\n"
        "|日线|虚构排列观察|2026-11-13|仅作诊断|\n|周线|虚构周线观察|2026-11-06|仅作诊断|\n"
        "|月线|虚构月线观察|2026-06-30|仅作诊断|\n|半年线|均线历史不足|2026-06-30|仅作诊断|\n"
        "|年线|均线历史不足|2025-12-31|仅作诊断|\n", encoding="utf-8")
    old = {"security": dict(SECURITY), "module": "technical", "module_version": "0.4.1", "result_id": "OLD",
           "generated_at": "2026-01-02T18:00:00+08:00", "research_asof": "2026-01-02T17:00:00+08:00",
           "data_asof": None, "report_file": str(report), "evidence": [], "parent_result_ids": [],
           "items": [{"item_id": "OLD-I1", "type": "observation", "text": "虚构跨历史旧关注项",
                      "report_locator": "tracking row", "frozen_at": "2026-01-02T18:00:00+08:00",
                      "definition": {"what": "旧趋势条件", "why": "不能由新空items删除", "next_evidence": "同口径新证据"},
                      "status": "pending", "current_use": "needs_review", "evidence_ids": []}]}
    lab.archive(root, old)
    document = technical_document()
    source = base / "analysis.json"
    source.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    request = {**old, "result_id": "LATEST", "generated_at": document["generated_at"], "research_asof": document["asof"],
               "data_asof": document["market_cutoff"], "parent_result_ids": ["OLD"], "items": [],
               "attachments": [{"source_file": str(source), "relative_path": "analysis.json", "media_type": "application/json"}]}
    lab.archive(root, request)
    return {"root": root, "market_data_dir": market, "security": dict(SECURITY), "document": document,
            "request": request, "source": source, "report": report, "lab_tool": TOOL,
            "no_archive_security_id": "A-share-sz-999998"}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = build_fixture(Path(self.temp.name))
        self.root, self.document = self.fixture["root"], self.fixture["document"]
        self.store = ResearchStore(self.root, TOOL, self.fixture["market_data_dir"])

    def view(self, period="1d", **options):
        return self.store.technical_dashboard(SECURITY["security_id"], period, asof=ASOF, **options)

    def append(self, result_id="NEW", *, document=None, module="technical", role=None, schema="stock-research-components/1",
               generated_at="2026-11-13T19:00:00+08:00", research_asof="2026-11-13T18:00:00+08:00"):
        base = Path(self.temp.name)
        attachments = []
        if document is not None:
            path = base / (result_id + "-analysis.json")
            path.write_text(json.dumps(document, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            attachments.append({"source_file": str(path), "relative_path": "analysis.json", "media_type": "application/json"})
        if role is not None:
            path = base / (result_id + "-components.json")
            path.write_text(json.dumps({"schema_version": schema, "security": SECURITY, "result_id": result_id,
                "content_role": role, "components": [], "published_date": "2026-11-12", "material_impact": True,
                "policy_and_industry_impact": True}, ensure_ascii=False), encoding="utf-8")
            attachments.append({"source_file": str(path), "relative_path": "research-components.json", "media_type": "application/json"})
        request = {**self.fixture["request"], "result_id": result_id, "module": module,
                   "generated_at": generated_at, "research_asof": research_asof,
                   "attachments": attachments}
        lab.archive(self.root, request)
        return request

    def test_default_projection_matches_frozen_values_and_keeps_history_pending(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = self.view()
        self.assertEqual(result["loaded_bar_count"], 252)
        self.assertEqual(result["source_bar_count"], 365)
        self.assertEqual(result["status"], "diagnostic")
        self.assertEqual(result["qualification"], "diagnostic")
        self.assertEqual(result["adjustment"], {"mode": "raw", "verified": False})
        self.assertEqual(len(result["explanations"]), 3)
        self.assertEqual(len(result["period_states"]), 5)
        self.assertEqual(result["source"]["result_id"], "LATEST")
        self.assertEqual([row["item_id"] for row in result["pending_items"]], ["OLD-I1"])
        self.assertEqual(result["review"]["rows"][0]["condition_status"], "cannot_determine")
        self.assertEqual(result["previous_technical"]["timeframes"]["1d"]["status"], "incomparable")
        last = result["bars"][-1]
        self.assertEqual(last["close"], self.document["timeframes"]["1d"]["bars"][-1]["close"])
        self.assertEqual(last["macd"]["histogram"], .1)
        self.assertEqual(last["sma60"], self.document["timeframes"]["1d"]["series"]["sma60"][-1])
        self.assertEqual(before, {path: (self.root / path).read_bytes() for path in before})
        json.dumps(result, allow_nan=False)

    def test_s4_numbered_reasons_and_next_observation_reach_dashboard(self):
        self.fixture['report'].write_text('# 虚构S4技术研究\n\n**日线判断：冻结日线偏弱。**\n\n'
            '> 继承原行情，未重新计算。\n\n## 三条依据\n\n'
            '1. **方向：** 虚构收盘低于均线。\n2. **力度：** 虚构动量尚未修复。\n3. **背景：** 缺少合格行业序列。\n\n'
            '## 下一观察：只保留两条\n\n- **延续观察：** 等待同口径日线。\n- **修复观察：** 核对动态均线。\n\n'
            '## 五周期状态\n\n|周期|冻结状态|纳入末端|用途|\n|---|---|---|---|\n'
            '| 日线 | 偏弱 | 2026-11-13 | 仅诊断 |\n| 周线 | 分歧 | 2026-11-06 | 仅诊断 |\n'
            '|月线|分歧|2026-10-30|仅诊断|\n|半年线|不足|2026-06-30|未判断|\n|年线|不足|2025-12-31|未判断|\n', encoding='utf-8')
        self.append(document=self.document, role='full_research')
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        result = self.view()
        self.assertEqual(result['source']['result_id'], 'NEW')
        self.assertEqual(result['summary']['title'], '冻结日线偏弱。')
        self.assertEqual(result['explanations'], ['方向： 虚构收盘低于均线。', '力度： 虚构动量尚未修复。', '背景： 缺少合格行业序列。'])
        self.assertEqual(result['conditions'], ['延续观察： 等待同口径日线。', '修复观察： 核对动态均线。'])
        self.assertEqual(result['period_states'][0]['status'], '偏弱')
        self.assertEqual(result['period_states'][1]['status'], '分歧')
        self.assertEqual(result['loaded_bar_count'], 252)
        self.assertTrue(all(path.read_bytes() == payload for path, payload in before.items()))

    def test_ma5_uses_full_same_period_history_before_slicing(self):
        for period in ("1d", "1w", "1mo", "6mo", "1y"):
            with self.subTest(period=period):
                result = self.view(period, limit=3)
                source = self.document["timeframes"][period]["bars"]
                self.assertEqual(result["bars"][0]["ma5"], sum(row["close"] for row in source[-7:-2]) / 5)
                self.assertEqual(result["bars"][-1]["ma5"], sum(row["close"] for row in source[-5:]) / 5)
                if period in {"6mo", "1y"}:
                    self.assertTrue(all(row["sma20"] is None and row["sma60"] is None for row in result["bars"]))

    def test_date_window_slices_bars_and_existing_indicators_together(self):
        result = self.view(date_from="2026-01-01", date_to="2026-01-10", limit=2000)
        self.assertEqual(result["loaded_bar_count"], 10)
        self.assertEqual(result["bars"][0]["date"], "2026-01-01")
        source = self.document["timeframes"]["1d"]
        index = next(i for i, row in enumerate(source["bars"]) if row["time"].startswith("2026-01-01"))
        self.assertEqual(result["bars"][0]["sma20"], source["series"]["sma20"][index])
        self.assertEqual(self.view(date_from="2020-01-01", date_to="2020-01-31")["bars"], [])

    def test_disabled_periods_limits_and_dates_fail_before_archive_read(self):
        with patch.object(self.store, "context", side_effect=AssertionError("invalid requests cannot read archive")):
            for period, options in [("5m", {}), ("1d", {"limit": True}), ("1d", {"limit": 2001}),
                                    ("1d", {"limit": 0}), ("1d", {"date_from": "2026-2-01"}),
                                    ("1d", {"date_from": "2026-02-02", "date_to": "2026-02-01"})]:
                with self.subTest(period=period, options=options), self.assertRaises(ValueError):
                    self.view(period, **options)

    def test_no_archive_identity_and_historical_asof_do_not_reuse_latest_prices(self):
        other = self.store.technical_dashboard(self.fixture["no_archive_security_id"])
        self.assertEqual(other["security"]["security_id"], self.fixture["no_archive_security_id"])
        self.assertEqual(other["bars"], [])
        historical = self.store.technical_dashboard(SECURITY["security_id"], asof="2026-02-01T18:00:00+08:00")
        self.assertEqual(historical["bars"], [])
        self.assertIsNone(historical["roles"]["formal_technical"])

    def test_ma5_resets_on_declared_boundaries_and_missing_prices(self):
        rows = self.document["timeframes"]["1d"]["bars"]
        rows[-6]["mechanical_gap"] = True
        rows[-1]["close"] = None
        self.append(document=self.document)
        result = self.view(limit=8)["bars"]
        self.assertTrue(result[-6]["segment_break"])
        self.assertTrue(all(row["ma5"] is None for row in result[-6:-2]))
        self.assertIsNotNone(result[-2]["ma5"])
        self.assertIsNone(result[-1]["ma5"])

    def test_unfinished_tail_is_excluded_without_compressing_an_interior_gap(self):
        frame = self.document["timeframes"]["1d"]
        frame["bars"][-1]["completed"] = False
        frame["metadata"]["data_asof"] = frame["bars"][-2]["time"]
        self.append(document=self.document)
        result = self.view(limit=3)
        self.assertEqual(result["source_bar_count"], 364)
        self.assertEqual(result["bars"][-1]["time"], frame["bars"][-2]["time"])
        frame["bars"][-3]["completed"] = False
        self.append("INTERIOR", document=self.document, generated_at="2026-11-13T20:00:00+08:00",
                    research_asof="2026-11-13T19:00:00+08:00")
        with self.assertRaisesRegex(ValueError, "Unfinished"):
            self.view()

    def test_wrong_identity_dates_misalignment_and_long_period_gaps_are_errors(self):
        for mutation in ("identity", "frame_identity", "availability", "series_length", "annual_gap"):
            document = technical_document()
            if mutation == "identity":
                document["security"]["company"] = "Another company"
            elif mutation == "frame_identity":
                document["timeframes"]["1y"]["metadata"]["security"]["company"] = "Another company"
            elif mutation == "availability":
                document["timeframes"]["1y"]["bars"][-1]["available_at"] = "2026-11-14T16:00:00+08:00"
            elif mutation == "series_length":
                document["timeframes"]["1y"]["series"]["sma20"].pop()
            else:
                frame = document["timeframes"]["1y"]
                frame["bars"].pop(3)
                for values in frame["series"].values():
                    values.pop(3)
            fixture = build_fixture(Path(self.temp.name) / mutation)
            fixture["source"].write_text(json.dumps(document, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            lab.archive(fixture["root"], {**fixture["request"], "result_id": mutation,
                "generated_at": "2026-11-13T19:00:00+08:00", "research_asof": "2026-11-13T18:00:00+08:00"})
            store = ResearchStore(fixture["root"], TOOL)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                store.technical_dashboard(SECURITY["security_id"], "1y", asof=ASOF)

    def test_legacy_calculation_context_does_not_replace_manifest_research_time(self):
        self.document["asof"] = "2026-11-13T18:30:00+08:00"
        self.document["generated_at"] = "2026-11-13T18:31:00+08:00"
        self.append(document=self.document)
        result = self.view()
        self.assertEqual(result["source"]["research_asof"], "2026-11-13T18:00:00+08:00")
        self.assertEqual(result["source"]["analysis_asof"], "2026-11-13T18:30:00+08:00")
        earlier = self.store.technical_dashboard(SECURITY["security_id"], asof="2026-11-13T18:35:00+08:00")
        self.assertEqual(earlier["source"]["result_id"], "LATEST")

    def test_nonfinite_and_boolean_chart_values_are_rejected(self):
        for payload in (b'{"x":NaN}', b'{"x":1e999}', b'{"x":Infinity}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _json(payload)
        self.document["timeframes"]["1d"]["series"]["rsi14"][-1] = True
        self.append(document=self.document)
        with self.assertRaises(ValueError):
            self.view()

    def test_broker_material_impact_does_not_replace_formal_research_but_new_research_does(self):
        self.append("F0", module="fundamental", role="full_research", generated_at="2026-11-13T18:15:00+08:00")
        self.append("BROKER", module="fundamental", role="broker_source", research_asof="2026-11-13T18:30:00+08:00")
        roles = self.store.research_roles(SECURITY["security_id"], ASOF)
        self.assertEqual(roles["formal_fundamental"]["result_id"], "F0")
        self.assertEqual(roles["supplementary"][0]["result_id"], "BROKER")
        self.assertEqual(roles["supplementary"][0]["published_date"], "2026-11-12")
        self.assertEqual(roles["supplementary"][0]["publication_precision"], "date_only")
        self.assertFalse(roles["broker_import_enabled"])
        context = self.store.context(SECURITY["security_id"], ASOF)
        self.assertEqual(context["results"][0]["result_id"], "BROKER")
        self.assertEqual(context["roles"]["formal_fundamental"]["result_id"], "F0")
        request = self.append("INCREMENT", module="fundamental", role="incremental_research",
                              generated_at="2026-11-13T20:00:00+08:00", research_asof="2026-11-13T19:00:00+08:00")
        self.assertEqual(self.store.research_roles(SECURITY["security_id"], ASOF)["formal_fundamental"]["result_id"], "INCREMENT")
        # A distinct later research cutoff makes the formal replacement explicit in this fixture.
        component = Path(self.temp.name) / "CORRECTION-components.json"
        component.write_text(json.dumps({"schema_version": "stock-research-components/1", "security": SECURITY,
            "result_id": "CORRECTION", "content_role": "correction", "components": []}), encoding="utf-8")
        lab.archive(self.root, {**request, "result_id": "CORRECTION", "generated_at": "2026-11-13T21:00:00+08:00",
            "research_asof": "2026-11-13T20:00:00+08:00", "attachments": [{"source_file": str(component),
            "relative_path": "research-components.json", "media_type": "application/json"}]})
        self.assertEqual(self.store.research_roles(SECURITY["security_id"], ASOF)["formal_fundamental"]["result_id"], "CORRECTION")

    def test_unknown_explicit_schema_never_falls_back_to_legacy_and_old_import_is_analysis(self):
        self.append(document=self.document, role="broker_source", schema="future-components/9")
        roles = self.store.research_roles(SECURITY["security_id"], ASOF)
        self.assertEqual(roles["formal_technical"]["result_id"], "LATEST")
        self.assertEqual(next(row for row in roles["unknown"] if row["result_id"] == "NEW")["content_role"], "unknown")
        request = {**self.fixture["request"], "result_id": "IMPORTED", "import_id": "IMP1", "source_file": str(self.fixture["report"])}
        lab.import_report(self.root, request)
        imported = self.store.research_roles(SECURITY["security_id"])["formal_technical"]
        self.assertEqual(imported["result_id"], "IMPORTED")
        self.assertEqual(imported["record_origin"], "imported")
        self.assertEqual(imported["content_role"], "full_research")

    def test_projection_uses_shared_reader_without_repeated_full_scan_and_checks_hash(self):
        with patch.object(self.store, "_scan", wraps=self.store._scan) as scan:
            self.view()
            self.assertEqual(scan.call_count, 1)
        attachment = next((self.root / "securities").rglob("analysis.json"))
        attachment.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.view()

    def test_role_resolution_retains_only_selected_analysis_for_chart_requests(self):
        self.append(document=self.document)
        context = self.store.context(SECURITY["security_id"], ASOF)
        roles, documents, _ = _resolve(self.store, context, keep_technical=True)
        self.assertEqual(roles["formal_technical"]["result_id"], "NEW")
        self.assertEqual(set(documents), {("NEW", "analysis.json")})
        self.assertEqual(_resolve(self.store, context)[1], {})

    def test_same_day_publication_cannot_occur_after_source_generation(self):
        self.append("BROKER", module="fundamental", role="broker_source")
        component = next((self.root / "securities").rglob("research-components.json"))
        payload = json.loads(component.read_text(encoding="utf-8"))
        payload["published_at"] = "2026-11-13T20:00:00+08:00"
        payload["published_date"] = "2026-11-13"
        source = Path(self.temp.name) / "future-publication.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        request = {**self.fixture["request"], "result_id": "FUTURE-PUBLICATION", "module": "fundamental",
            "generated_at": "2026-11-13T19:00:00+08:00", "research_asof": "2026-11-13T18:00:00+08:00",
            "attachments": [{"source_file": str(source), "relative_path": "research-components.json", "media_type": "application/json"}]}
        payload["result_id"] = request["result_id"]
        source.write_text(json.dumps(payload), encoding="utf-8")
        lab.archive(self.root, request)
        with self.assertRaisesRegex(ValueError, "Publication"):
            self.store.research_roles(SECURITY["security_id"], ASOF)


if __name__ == "__main__":
    unittest.main()
