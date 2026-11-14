"""Finite, read-only adapters over frozen research; no research or data acquisition."""

from datetime import date, timedelta, timezone
import json
import math
import re


PERIODS = {"1d": "日线", "1w": "周线", "1mo": "月线", "6mo": "半年线", "1y": "年线"}
SERIES = ("sma20", "sma60", "sma120", "macd", "macd_signal", "macd_hist", "rsi14", "volume_ratio20")
FORMAL = {"full_research", "incremental_research", "correction"}
SOURCE_ROLES = {"source_adaptation", "broker_source"}
SHANGHAI = timezone(timedelta(hours=8))
ADAPTER = "stock-research-dashboard/1"


def _pick(value, names):
    return {key: value[key] for key in names if key in value}


def _number(value):
    if value is None:
        return None
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Chart numbers must be finite numbers or null")
    return value


def _json(payload):
    def decimal(value):
        return _number(float(value))
    def invalid(value):
        raise ValueError("Nonfinite JSON value: " + value)
    value = json.loads(payload.decode("utf-8-sig"), parse_float=decimal, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("Research attachment must be a JSON object")
    return value


def _day(value):
    if value is None:
        return None
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Chart dates must use YYYY-MM-DD")
    return date.fromisoformat(value)


def _technical_identity(store, record, document):
    return store.lab.technical_identity(record, document)


def _resolve(store, context, *, keep_technical=False):
    return store.lab.resolve_roles(store.root, context,
        read_attachment=lambda record, name: store._record_reader(record)(name), keep_technical=keep_technical)


def research_roles(store, context):
    return _resolve(store, context)[0]


def _brief(payload):
    text = payload.decode("utf-8-sig")
    title = re.search(r"^\*\*日线判断[：:](.+?)\*\*", text, re.MULTILINE)
    first = text.split("## ", 1)[0]
    reasons = re.search(r"^##\s+三条依据[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    explanation_text = reasons.group(1) if reasons else first
    explanations = [match[1].replace("**", "").strip() for match in
        re.finditer(r"^\s*(?:[-*]|\d+[.、)）])\s+(.+)$", explanation_text, re.MULTILINE)][:3]
    explanations += ["原报告未提供此条简明依据，需查看来源。"] * (3 - len(explanations))
    conditions = []
    section = re.search(r"^##\s+(?:接下来只看两件事|下一观察)[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    if section:
        conditions = [match[1].replace("**", "").strip() for match in
            re.finditer(r"^\s*(?:[-*]|\d+[.、)）])\s+(.+)$", section.group(1), re.MULTILINE)]
    states = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and cells[0] in PERIODS.values():
            states[cells[0]] = cells[1]
    return {"title": title.group(1).strip() if title else "已归档技术研究", "reason": explanations[0],
            "period": "1d", "origin": "archived_report"}, explanations, conditions, states


def _frame_metadata(store, identity, period, frame):
    if not isinstance(frame, dict) or not isinstance(frame.get("metadata"), dict):
        raise ValueError("Technical timeframe metadata is missing")
    meta = frame["metadata"]
    if "security" in meta and store.lab.security(meta["security"]) != identity:
        raise ValueError("Timeframe security identity differs")
    if "timeframe" in meta and meta["timeframe"] != period:
        raise ValueError("Timeframe metadata differs from its declared period")
    binding = meta.get("context", {})
    if binding and (binding.get("security_id") != identity["security_id"] or binding.get("timeframe") != period):
        raise ValueError("Timeframe context differs from its security or period")
    if meta.get("data_asof") is not None:
        store.lab.stamp(meta["data_asof"], "frame.data_asof")
    return meta


def _period_key(day, period):
    if period == "1w":
        return (day - timedelta(days=day.weekday())).toordinal() // 7
    if period == "1mo":
        return day.year * 12 + day.month
    if period == "6mo":
        return day.year * 2 + (day.month > 6)
    return day.year


def _points(store, document, period, frame):
    rows, series = frame.get("bars"), frame.get("series", {})
    if not isinstance(rows, list) or not isinstance(series, dict):
        raise ValueError("Technical bars and indicator arrays are required")
    for key in SERIES:
        if key in series and (not isinstance(series[key], list) or len(series[key]) != len(rows)):
            raise ValueError("Indicator arrays must have the same window as the bars")
    meta = _frame_metadata(store, document["security"], period, frame)
    cutoff = store.lab.stamp(document["asof"], "analysis.asof")
    market = store.lab.stamp(document["market_cutoff"], "analysis.market_cutoff")
    data = store.lab.stamp(meta["data_asof"], "frame.data_asof") if rows else None
    if data is not None and data > market:
        raise ValueError("Timeframe data cutoff exceeds the frozen market cutoff")
    output, window, previous, previous_row, unfinished = [], [], None, None, False
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Technical bars must be objects")
        stamp = store.lab.stamp(row.get("time"), "bar.time")
        if previous is not None and stamp <= previous:
            raise ValueError("Technical bar times must strictly increase")
        previous = stamp
        if row.get("completed") is not True:
            unfinished = True
            continue
        if unfinished:
            raise ValueError("Unfinished bars cannot be compressed inside a usable segment")
        if stamp > data or stamp > market:
            raise ValueError("Technical bar exceeds its declared data cutoff")
        for name in ("available_at", "first_known_at", "prefix_known_at"):
            if row.get(name) is not None and store.lab.stamp(row[name], name) > cutoff:
                raise ValueError("Technical bar was not available at the research cutoff")
        day = stamp.astimezone(SHANGHAI).date()
        if "mechanical_gap" in row and type(row["mechanical_gap"]) is not bool:
            raise ValueError("Declared mechanical gaps must be boolean")
        if "source_index" in row and (type(row["source_index"]) is not int or row["source_index"] < 0):
            raise ValueError("Source bar indices must be nonnegative integers")
        point = {name: _number(row.get(name)) for name in ("open", "high", "low", "close", "volume")}
        if any(point[name] is not None and point[name] <= 0 for name in ("open", "high", "low", "close")) or point["volume"] is not None and point["volume"] < 0:
            raise ValueError("Technical prices or volume are invalid")
        if all(point[name] is not None for name in ("open", "high", "low", "close")) and not (
            point["low"] <= min(point["open"], point["close"]) <= max(point["open"], point["close"]) <= point["high"]):
            raise ValueError("Inconsistent technical OHLC")
        boundary = row.get("mechanical_gap") is True
        if previous_row is not None:
            old_day = store.lab.stamp(previous_row["time"], "bar.time").astimezone(SHANGHAI).date()
            if day <= old_day or period != "1d" and _period_key(day, period) <= _period_key(old_day, period):
                raise ValueError("A technical timeframe cannot repeat its calendar period")
            gap = period != "1d" and _period_key(day, period) != _period_key(old_day, period) + 1
            boundary = boundary or gap or any(row.get(key) != previous_row.get(key) for key in ("segment_id", "session_id"))
            if "source_index" in row and "source_index" in previous_row:
                boundary = boundary or row["source_index"] != previous_row["source_index"] + 1
            if gap and period in {"6mo", "1y"}:
                raise ValueError("Long-period source crosses a missing complete period")
        if boundary:
            window = []
        window.append(point["close"])
        window = window[-5:]
        ma5 = sum(window) / 5 if len(window) == 5 and all(value is not None for value in window) else None
        point.update(date=day.isoformat(), time=row["time"], ma5=_number(ma5), segment_break=boundary)
        values = {key: _number(series[key][index]) if key in series else None for key in SERIES}
        point.update(_pick(values, ("sma20", "sma60", "sma120", "rsi14", "volume_ratio20")))
        point["macd"] = {"dif": values["macd"], "dea": values["macd_signal"], "histogram": values["macd_hist"]}
        output.append(point)
        previous_row = row
    if output and store.lab.stamp(output[-1]["time"], "bar.time") != data:
        raise ValueError("Timeframe data cutoff differs from its latest completed bar")
    return output


def technical_dashboard(store, security_id, period="1d", *, asof=None, date_from=None, date_to=None, limit=252):
    if period not in PERIODS or type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError("Select an enabled period and 1–2000 display points")
    start, end = _day(date_from), _day(date_to)
    if start and end and start > end:
        raise ValueError("Chart date_from must not exceed date_to")
    context = store.context(security_id, asof, include_roles=False)
    roles, documents, readers = _resolve(store, context, keep_technical=True)
    source = roles["formal_technical"]
    result = {"schema_version": ADAPTER, "security": context["security"], "period": period, "asof": context["asof"],
              "status": "no_research", "source_status": "no_research", "source_label": "尚无可识别的正式技术研究",
              "source": None, "data_asof": None, "generated_at": None, "qualification": "unknown", "adjustment": None,
              "price_currency": context["security"]["currency"], "volume_unit": "unknown",
              "available_periods": [], "bars": [], "loaded_bar_count": 0, "source_bar_count": 0,
              "summary": {"title": "尚无研究", "reason": "没有可展示的正式技术研究", "period": period, "origin": "none"},
              "explanations": [], "conditions": [], "period_states": [], "pending_items": context["pending_items"],
              "review": None, "previous_technical": None, "limitations": [], "reason": "尚无可识别的正式技术研究",
              "roles": roles}
    if source is None:
        if any(row["module"] == "technical" for row in roles["unknown"]):
            result.update(status="unrecognized_role", source_status="unrecognized_role", reason="技术档案角色或模式待核，未回退到最新文件")
        return result
    record = next(row for row in context["results"] if row["result_id"] == source["result_id"])
    attachment = next((entry for entry in record.get("attachments", []) if entry["relative_path"] == "analysis.json"), None)
    result.update(source={**source, "attachment": "analysis.json", "sha256": attachment["sha256"] if attachment else None},
                  generated_at=record["generated_at"], source_label=record["result_id"])
    if attachment is None:
        result.update(status="missing_analysis", source_status="missing_analysis", reason="正式研究未归档可识别的技术JSON，保留报告查阅")
        return result
    document = documents.get((record["result_id"], "analysis.json"))
    reader = readers.setdefault(record["result_id"], store._record_reader(record))
    if document is None:
        document = _json(reader("analysis.json")[0])
    if document.get("schema_version") != "stock-technical-report/2":
        result.update(status="missing_analysis", source_status="missing_analysis", reason="技术JSON模式待适配")
        return result
    _technical_identity(store, record, document)
    result["source"].update(analysis_asof=document["asof"], analysis_generated_at=document["generated_at"],
                            market_cutoff=document["market_cutoff"])
    summary, explanations, conditions, states = _brief(reader()[0])
    frames = document["timeframes"]
    for name, label in PERIODS.items():
        if name not in frames:
            continue
        meta = _frame_metadata(store, record["security"], name, frames[name])
        count = len(frames[name].get("bars", []))
        result["period_states"].append({"period": name, "status": states.get(label, "原报告未提供简明周期状态"),
            "qualification": meta.get("qualification", "unknown"), "data_asof": meta.get("data_asof"), "bar_count": count})
        if count and frames[name].get("status") != "paused":
            result["available_periods"].append(name)
    result.update(summary=summary, explanations=explanations, conditions=conditions,
                  review=_pick(document.get("review", {}), ("status", "rows", "limitations", "pnl_computed")),
                  previous_technical=_pick(document.get("previous_technical", {}), ("status", "result_id", "generated_at", "reason")))
    result["previous_technical"]["timeframes"] = {
        name: _pick(value, ("status", "reasons", "previous_bars", "current_bars", "changes"))
        for name, value in document.get("previous_technical", {}).get("timeframes", {}).items() if name in PERIODS}
    if period not in frames or frames[period].get("status") == "paused":
        result.update(status="no_bars", source_status="no_bars", reason="这份研究没有该周期的完整可用数据")
        return result
    frame = frames[period]
    meta = _frame_metadata(store, record["security"], period, frame)
    points = _points(store, document, period, frame)
    shown = [point for point in points if (start is None or point["date"] >= start.isoformat()) and (end is None or point["date"] <= end.isoformat())][-limit:]
    qualification = meta.get("qualification", "unknown")
    result.update(status="ok" if qualification == "validated" else "diagnostic", source_status="archived_snapshot",
                  qualification=qualification, adjustment=meta.get("adjustment"), data_asof=meta.get("data_asof"),
                  volume_unit=meta.get("volume_unit") if isinstance(meta.get("volume_unit"), str) else "unknown",
                  bars=shown, source_bar_count=len(points), loaded_bar_count=len(shown), reason=None,
                  limitations=list(dict.fromkeys(document.get("limitations", []) + meta.get("limitations", []) + frame.get("limitations", []) +
                      ["MA5仅为同周期完整收盘序列的展示均值，先保留暖启动再切显示窗；不形成新研究或提升原资格。"])),
                  window={"date_from": date_from, "date_to": date_to, "limit": limit,
                          "first": shown[0]["date"] if shown else None, "last": shown[-1]["date"] if shown else None},
                  continuity=_pick(meta, ("continuity_policy", "excluded_discontinuous_periods", "unfinished_periods",
                                         "incomplete_trading_date_periods", "missing_trailing_periods", "latest_completed_period_present")))
    if period != "1d":
        result["summary"] = {"title": states.get(PERIODS[period], "原报告未提供简明周期状态"),
                             "reason": "摘自同份已归档报告的周期状态；三条依据与后续条件保留原报告日线范围。",
                             "period": period, "origin": "archived_report"}
    if not shown:
        result.update(status="no_bars", reason="所选展示范围内没有完整可用数据")
    json.dumps(result, allow_nan=False)
    return result
