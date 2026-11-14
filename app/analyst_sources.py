"""Read the configured local broker-report index without promoting it to research."""

import csv
from datetime import date, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re


SCHEMA = "stock-analyst-updates/1"
FIELDS = ("股票代码", "发布日期", "研究机构", "研究人员", "报告类别", "报告名称",
          "最新投资评级", "调整方向", "目标价位", "入库时间", "抓取时间")
MAX_BYTES = 16 * 1024 * 1024
TZ = timezone(timedelta(hours=8))


def _publication(value):
    raw = str(value or "").strip()
    normalized = raw.replace("/", "-")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            return date.fromisoformat(normalized).isoformat(), "date"
        if re.fullmatch(r"\d{4}-\d{2}", normalized):
            date.fromisoformat(normalized + "-01")
            return normalized, "month"
    except ValueError:
        pass
    return None, "unknown"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _rows(payload):
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("gb18030")
    rows = csv.reader(io.StringIO(text, newline=""))
    header = []
    for number, header in enumerate(rows):
        if number > 4:
            break
        if set(FIELDS) <= set(header):
            if len(header) != len(set(header)):
                raise ValueError("研报索引存在重复列名")
            break
    else:
        raise ValueError("研报索引缺少预期字段")
    if not set(FIELDS) <= set(header):
        raise ValueError("研报索引缺少预期字段")
    records = []
    for values in rows:
        if not values or not any(value.strip() for value in values):
            continue
        if len(values) != len(header):
            raise ValueError("研报索引行与列数不一致")
        records.append(dict(zip(header, values)))
    return records


def analyst_updates(store, security_id, index_dir=None, *, asof=None, limit=20, context=None):
    """Project publication labels and metadata; never infer text, forecasts or old availability."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("研报索引每次显示1至100条")
    store.lab.identifier(security_id, "security_id")
    context = store.context(security_id) if context is None else context
    identity = store.lab.security(context["security"])
    if identity["security_id"] != security_id:
        raise ValueError("研报索引证券身份不一致")
    checked = store.lab.now()
    checked_moment = store.lab.stamp(checked, "checked_at")
    cutoff = checked_moment if asof is None else store.lab.stamp(asof, "asof")
    if cutoff > checked_moment + timedelta(minutes=5):
        raise ValueError("研报检查截止不能位于未来")
    formal = context.get("roles", {}).get("formal_fundamental")
    formal_view = ({key: formal.get(key) for key in ("result_id", "research_asof", "generated_at")}
                   if formal else None)
    formal_date = (store.lab.stamp(formal["research_asof"], "formal.research_asof").astimezone(TZ).date().isoformat()
                   if formal and formal.get("research_asof") else None)
    result = {"schema_version": SCHEMA, "security": identity, "status": "index_unavailable",
              "checked_at": checked, "asof": asof or checked, "formal_fundamental": formal_view,
              "source": None, "records": [], "formal_import_enabled": False,
              "summary": {"records_total": 0, "shown": 0, "unknown_date_count": 0,
                          "newer_than_formal_count": None if formal_date is None else 0,
                          "dates_after_formal": [], "new_since_last_check": None,
                          "change_detection": "no_previous_snapshot", "future_date_excluded": 0},
              "limitations": ["本地索引仅提供线索；未取得正文时不生成观点、摘要或盈利预测。",
                  "原发布日期不等于本平台取得日；入库／抓取文本保留原文，未验证时区，不用文件修改时间代替。",
                  "资料仅作基本面补充，不能替代正式基本面报告；未完成全宿主角色兼容前禁止正式资料导入。"]}
    # shortcut: this is a live index, not historical snapshots; never reconstruct past knowledge from today's file.
    if asof is not None and cutoff < checked_moment:
        result.update(status="historical_unavailable")
        result["limitations"].append("没有该截止前冻结的索引快照，历史视图不展示本次后来取得的资料。")
        return result
    if index_dir is None:
        result["limitations"].append("未配置本地研报索引目录。")
        return result
    if (identity["market"] != "A-share" or identity["exchange"] not in {"sh", "sz", "bj"}
            or identity["share_class"] != "A" or identity["currency"] != "CNY"
            or not re.fullmatch(r"\d{6}", identity["code"])):
        result["limitations"].append("该本地索引仅有明确交易所前缀的人民币A股。")
        return result
    directory = store.lab.checked_path(Path(index_dir), reject_git=True)
    if not directory.is_dir():
        raise ValueError("本地研报索引目录不存在")
    prefix = identity["exchange"] + identity["code"]
    source = directory / (prefix + ".csv")
    if not source.exists():
        store.lab.checked_path(source, reject_git=True)
        result["limitations"].append("该证券没有本地索引文件；不据此判断不存在研报。")
        return result
    source = store._regular(source)
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("研报索引超过本次读取上限")
    payload = source.read_bytes()
    if len(payload) > MAX_BYTES:
        raise ValueError("研报索引读取期间超过上限")
    rows = _rows(payload)
    entries, seen, groups = [], {}, {}
    today = cutoff.astimezone(TZ).date().isoformat()
    future = 0
    for row in rows:
        if row["股票代码"].strip().casefold() != prefix:
            raise ValueError("研报索引行证券代码与文件及目标身份不一致")
        published, precision = _publication(row["发布日期"])
        if published and published > today[:len(published)]:
            future += 1
            continue
        row_hash = _digest(row)
        source_id = "index-" + _digest({key: row[key] for key in FIELDS[:9]})
        observation = {"row_sha256": row_hash, "source_ingested_at_raw": row["入库时间"],
                       "source_captured_at_raw": row["抓取时间"]}
        if source_id in seen:
            if observation not in seen[source_id]["index_observations"]:
                seen[source_id]["index_observations"].append(observation)
            continue
        group_id = _digest([prefix, row["发布日期"].strip(), row["研究机构"].strip(),
                            row["报告名称"].strip()])
        groups[group_id] = groups.get(group_id, 0) + 1
        newer = (published > formal_date[:len(published)] if published and formal_date else None)
        if precision == "month" and formal_date and published == formal_date[:7]:
            newer = None
        entry = {"source_id": source_id, "row_sha256": row_hash,
            "revision_group_id": group_id, "publication_date": published,
            "publication_date_precision": precision, "publication_date_raw": row["发布日期"],
            "title": row["报告名称"].strip() or "标题未知", "organization": row["研究机构"].strip(),
            "authors": row["研究人员"].strip(), "report_category": row["报告类别"].strip(),
            "rating": row["最新投资评级"].strip(), "rating_change": row["调整方向"].strip(),
            "target_price_raw": row["目标价位"], "source_ingested_at_raw": row["入库时间"],
            "source_captured_at_raw": row["抓取时间"], "discovered_at": checked,
            "acquired_at": None, "imported_at": None, "content_status": "metadata_only",
            "content_label": "研报线索，正文未取得", "views": [], "forecasts": [],
            "newer_than_formal": newer, "supplement_only": True, "can_replace_formal": False,
            "index_observations": [observation]}
        entries.append(entry)
        seen[source_id] = entry
    for entry in entries:
        entry["revision_count"] = groups[entry["revision_group_id"]]
    entries.sort(key=lambda row: (row["publication_date"] or "", row["source_id"]), reverse=True)
    known = [row["publication_date"] for row in entries if row["publication_date"]]
    after = [row for row in entries if row["newer_than_formal"]]
    result.update(status="ok", records=entries[:limit], source={"kind": "local_csv_index",
                  "file_name": source.name, "sha256": hashlib.sha256(payload).hexdigest(),
                  "row_count": len(rows), "latest_publication_date": max(known) if known else None})
    result["summary"].update(records_total=len(entries), shown=min(limit, len(entries)),
        unknown_date_count=sum(row["publication_date"] is None for row in entries),
        newer_than_formal_count=len(after) if formal_date else None,
        dates_after_formal=sorted({row["publication_date"] for row in after}, reverse=True),
        future_date_excluded=future)
    return result
