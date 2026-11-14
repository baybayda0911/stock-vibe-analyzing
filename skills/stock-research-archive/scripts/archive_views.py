"""Read-only, offline views over an explicitly supplied research-lab module."""

from datetime import date
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import stat


SCHEMA = "stock-research-archive/view-1"
MODULE_NAMES = {"fundamental": "基本面", "technical": "技术面"}
ROLE_NAMES = {"full_research": "完整研究", "incremental_research": "增量研究", "correction": "研究更正",
              "broker_source": "补充研报", "source_adaptation": "补充资料", "unknown": "角色待核"}
ITEM_FIELDS = ("item_id", "result_id", "type", "text", "report_locator", "frozen_at", "status",
               "current_use", "current_use_reason", "valid_until", "review_due_at", "invalidated_at",
               "prospective_status", "evidence_ids", "current_eligible", "history_usable", "reasons",
               "applied_review_ids", "data_asof", "module", "source_result_id")
DEFINITION_FIELDS = ("metric", "expected", "unit", "period", "deadline", "baseline", "tolerance",
                     "evaluation_rule", "proposition", "mechanism", "support_condition",
                     "falsification_condition", "check_window", "quote", "source_public_at",
                     "revision_context", "what", "why", "next_evidence", "timeframe", "inputs",
                     "trigger", "confirmation", "invalidation", "observation_window")
REASONS = {"explicit_current_review_required": "需要明确复核", "historical_only": "仅供历史回顾",
           "validity_window_ended": "有效期已结束", "review_due": "已到复核日",
           "invalidated": "已失效", "validity_window_unknown": "有效期未知",
           "source_publication_time_unverified": "来源公开时点未核实",
           "research_or_data_asof_unknown": "研究或数据截止未知",
           "no_item_qualification": "没有已确认可用的关注项"}
STATUS_NAMES = {"pending": "待核对", "supported": "获得支持", "weakened": "支持减弱",
                "falsified": "已证伪", "fulfilled": "已兑现", "partially_fulfilled": "部分兑现",
                "not_fulfilled": "未兑现", "not_due": "未到期", "unknown": "未知",
                "withdrawn": "已撤回", "expired": "已过期", "not_triggered": "未触发",
                "triggered_pending": "已触发待确认", "confirmed": "已确认", "invalidated": "已失效",
                "observed": "已观察", "no_change": "未变化", "cannot_determine": "无法判断"}
FIELD_NAMES = {"metric": "指标", "expected": "原预期", "unit": "单位", "period": "期间", "deadline": "期限",
               "baseline": "基准", "tolerance": "容许偏差", "evaluation_rule": "核对规则", "proposition": "原假设",
               "mechanism": "作用机制", "support_condition": "支持条件", "falsification_condition": "证伪条件",
               "check_window": "检查窗口", "quote": "原话", "source_public_at": "来源公开时点",
               "revision_context": "修订背景", "what": "观察内容", "why": "观察原因", "next_evidence": "下一证据",
               "timeframe": "周期", "inputs": "依据", "trigger": "触发条件", "confirmation": "确认条件",
               "invalidation": "失效条件", "observation_window": "观察窗口", "module": "研究专业",
               "module_version": "研究工具版本", "generated_at": "生成时间", "research_asof": "研究截止",
               "data_asof": "数据截止", "original_generated_at": "原报告生成时间", "record_origin": "归档来源",
               "historical_reconstruction": "是否历史重建"}


def _pick(value, fields):
    return {key: value[key] for key in fields if key in value}


def _item(value, visible_items):
    result = _pick(value, ITEM_FIELDS)
    result["definition"] = _pick(value.get("definition", {}), DEFINITION_FIELDS)
    result["parent_item_ids"] = [key for key in value.get("parent_item_ids", []) if key in visible_items]
    return result


def _date(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return parsed


def _source_file(root, relative, digest):
    """Reject traversal, links and changed bytes before constructing a file URI."""
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("Source path must be root-relative")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or not parts.parts or any(part in {".", ".."} or ":" in part for part in parts.parts):
        raise ValueError("Source path must stay inside the lab root")
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("Lab root must be absolute")
    candidate = root.joinpath(*parts.parts)
    for component in reversed((candidate, *candidate.parents)):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked source paths are forbidden")
    if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
        raise ValueError("Source must be a file inside the lab root")
    fingerprint = hashlib.sha256()
    with candidate.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            fingerprint.update(block)
    if not isinstance(digest, str) or fingerprint.hexdigest() != digest:
        raise ValueError("Source fingerprint mismatch")
    return candidate


def _result(record, visible_ids, visible_items):
    value = _pick(record, ("result_id", "security", "module", "module_version", "generated_at",
                           "research_asof", "data_asof", "original_generated_at", "record_origin",
                           "historical_reconstruction", "current_eligible", "history_usable", "reasons",
                           "current_eligible_item_ids", "report_sha256", "items_sha256"))
    value["parent_result_ids"] = [key for key in record.get("parent_result_ids", []) if key in visible_ids]
    value["item_states"] = [_item(item, visible_items) for item in record.get("item_states", [])]
    value["evidence"] = [_pick(source, ("evidence_id", "source", "public_at", "public_at_precision", "public_date"))
                         for source in record.get("evidence", [])]
    folder = PurePosixPath(record["path"]).parent
    value["files"] = [{"label": "原研究报告", "path": (folder / "report.md").as_posix(),
                       "sha256": record["report_sha256"]}]
    value["attachments"] = [_pick(entry, ("relative_path", "media_type", "sha256", "size_bytes"))
                            for entry in record.get("attachments", [])]
    value["files"].extend({"label": entry["relative_path"],
                           "path": (folder / entry["relative_path"]).as_posix(), "sha256": entry["sha256"]}
                          for entry in record.get("attachments", []))
    return value


def _search_text(result):
    metadata = _pick(result, ("result_id", "security", "module", "module_version", "generated_at",
                              "research_asof", "data_asof", "original_generated_at", "record_origin", "evidence",
                              "content_role", "published_date", "published_at", "source_ref", "source_title"))
    metadata["old_items"] = [_pick(item, ("item_id", "type", "text", "definition", "report_locator"))
                             for item in result["item_states"]]
    return json.dumps(metadata, ensure_ascii=False).casefold()


def _shared_roles(lab,root,context):
    version="stock-research-role-resolver/1"
    if (getattr(lab,"CAPABILITIES",{}).get("role_resolution")!=version or
            not callable(getattr(lab,"resolve_roles",None))):
        return None
    roles=context.get("roles")
    if roles is None:
        roles,_,_=lab.resolve_roles(root,context)
    if not isinstance(roles,dict) or roles.get("schema_version")!="stock-research-roles/1" or roles.get("resolver_version")!=version:
        raise ValueError("Unsupported shared content-role resolver")
    return roles


def build_view(lab, root, security_id, asof, *, module=None, date_from=None, date_to=None, query=None):
    """Filter visible results, retaining every historical open item from context."""
    if module is not None and module not in lab.MODULES:
        raise ValueError("Invalid module")
    start, end = _date(date_from, "date_from"), _date(date_to, "date_to")
    if start and end and start > end:
        raise ValueError("date_from must not exceed date_to")
    if query is not None and not isinstance(query, str):
        raise ValueError("query must be text")
    query = query.strip() if query else None
    context = lab.context(root, security_id, asof)
    if any(record["module"] not in lab.MODULES for record in context["results"]):
        raise ValueError("Visible result has an unsupported module")
    visible_ids = {record["result_id"] for record in context["results"]}
    visible_items = {item["item_id"] for record in context["results"] for item in record["item_states"]}
    all_results = [_result(record, visible_ids, visible_items) for record in context["results"]]
    roles=_shared_roles(lab,root,context)
    classified={row["result_id"]:row for row in roles["classified"]} if roles else {}
    for record in all_results:
        role=classified.get(record["result_id"],{})
        record.update(_pick(role,("content_role","published_at","published_date","publication_precision",
                                  "imported_at","acquired_at","extracted_at","source_ref",
                                  "cover_report_date","original_publication_date","original_publication_precision","source_title")))
        record.setdefault("content_role","unknown")
    by_id={record["result_id"]:record for record in all_results}
    source_results=[by_id[row["result_id"]] for row in roles.get("supplementary",[]) if row["result_id"] in by_id] if roles else []
    research_selection={key:(roles[key]["result_id"] if roles and roles.get(key) else None)
                        for key in ("formal_fundamental","formal_technical")}
    results = []
    for record in all_results:
        research_day = lab.stamp(record["research_asof"], "research_asof").date() if record.get("research_asof") else None
        if module is not None and record["module"] != module:
            continue
        if (start or end) and (research_day is None or start and research_day < start or end and research_day > end):
            continue
        if query and query.casefold() not in _search_text(record):
            continue
        results.append(record)
    pending = [_item(item, visible_items) for item in context["pending_items"]]
    reviews = {}
    for record in all_results:
        for item in record["item_states"]:
            for review_id in item.get("applied_review_ids", []):
                review = reviews.setdefault(review_id, {"review_id": review_id, "item_ids": [], "source_result_ids": []})
                if item["item_id"] not in review["item_ids"]:
                    review["item_ids"].append(item["item_id"])
                if record["result_id"] not in review["source_result_ids"]:
                    review["source_result_ids"].append(record["result_id"])
    visible_plan_versions = {record['plan_version_id'] for record in context['plans']}
    plans = []
    for record in context["plans"]:
        value = _pick(record, ("plan_id", "plan_version_id", "version", "previous_plan_version_id", "generated_at",
                               "frozen_at", "effective_at", "valid_until", "trigger", "confirmation", "invalidation", "exit"))
        value["source_result_ids"] = [key for key in record["source_result_ids"] if key in visible_ids]
        value["adoption"] = _pick(record.get("adoption", {}), ("status", "at"))
        if value.get('previous_plan_version_id') and value['previous_plan_version_id'] not in visible_plan_versions:
            value.pop('previous_plan_version_id')
            value['previous_plan_not_visible'] = True
        plans.append(value)
    executions = []
    for record in context["executions"]:
        value = _pick(record, ("import_id", "generated_at", "execution_kind", "source_assertion", "source_sha256"))
        value["trades"] = [_pick(trade, ("trade_id", "executed_at", "time_precision", "side", "currency",
                                           "correction_of_trade_id", "version_review_reasons")) for trade in record["trades"]]
        for summary, trade in zip(value["trades"], record["trades"]):
            allocations = trade.get('allocations', [])
            summary["plan_versions"] = [_pick(allocation, ("plan_id", "plan_version_id"))
                                        for allocation in allocations if allocation['plan_version_id'] in visible_plan_versions]
            hidden = len(allocations) - len(summary['plan_versions'])
            if hidden:
                summary['unavailable_plan_links_count'] = hidden
        executions.append(value)
    return {"schema_version": SCHEMA, "kind": "archive_view", "security_id": security_id, "asof": asof,
            "security": all_results[0]["security"] if all_results else {"security_id": security_id},
            "filters": {"module": module, "date_from": date_from, "date_to": date_to, "query": query},
            "filter_notes": ["日期范围按研究截止日筛选；生成日、研究截止和数据截止分别保留。",
                             "研究截止未知的成果在未设日期范围时保留；设置日期范围后从成果列表排除。",
                             "模块、日期和关键词只筛选成果列表；全部历史未结项、父版本及计划／成交回顾仍保留。",
                             "关键词仅查已核验的研究元数据、来源描述和冻结旧项文字，不解析报告或附件全文。"],
            "counts": {"visible_results": len(all_results), "displayed_results": len(results), "pending_items": len(pending),
                       "supplementary_sources":len(source_results)},
            "role_resolution": "stock-research-role-resolver/1" if roles else "unavailable",
            "research_selection":research_selection,
            "source_events":[_pick(record,("result_id","content_role","published_at","published_date",
                "publication_precision","imported_at","acquired_at","extracted_at","source_ref",
                "cover_report_date","original_publication_date","original_publication_precision","source_title")) for record in source_results],
            "results": results,
            "timeline": [_pick(record, ("result_id", "module", "generated_at", "research_asof", "data_asof", "current_eligible")) for record in results],
            "pending_items": pending,
            "parent_chains": [{"result_id": record["result_id"], "parent_result_ids": record["parent_result_ids"]} for record in all_results],
            "reviews": list(reviews.values()), "plans": plans, "executions": executions,
            "superseded_trade_ids": context["superseded_trade_ids"], "pnl_computed": False,
            "source_overview": {"scope": "当前截止可见的全部成果", "report_count": len(all_results),
                                "attachment_count": sum(len(record["attachments"]) for record in all_results),
                                "evidence_count": sum(len(record["evidence"]) for record in all_results),
                                "modules": sorted({record["module"] for record in all_results})},
            "_private": {"root": str(Path(root).absolute())}}


def compare(lab, root, security_id, asof, left_id, right_id):
    """Compare frozen records and appended states, never infer research outcomes."""
    context = lab.context(root, security_id, asof)
    known = {record["result_id"]: record for record in context["results"]}
    if left_id not in known or right_id not in known:
        raise ValueError("Both results must be visible for this security at asof")
    left, right = known[left_id], known[right_id]
    if any(record["module"] not in lab.MODULES for record in (left, right)):
        raise ValueError("Comparison result has an unsupported module")
    identity_fields = ("security_id", "share_class", "currency")
    if (left["security"]["security_id"] != security_id or
            any(not left["security"].get(key) or left["security"].get(key) != right["security"].get(key) for key in identity_fields)):
        raise ValueError("Comparison requires the same security, share class and currency")
    visible_items = {item["item_id"] for record in context["results"] for item in record["item_states"]}
    summaries = [_result(record, set(known), visible_items) for record in (left, right)]
    items = []
    for record in (left, right):
        relative = (PurePosixPath(record["path"]).parent / "items.json").as_posix()
        frozen = lab.read_json(_source_file(root, relative, record["items_sha256"]))["items"]
        items.append({"result_id": record["result_id"], "frozen_items": [_item(item, visible_items) for item in frozen],
                      "states_at_asof": [_item(item, visible_items) for item in record["item_states"]]})
    fields = ("module", "module_version", "generated_at", "research_asof", "data_asof", "original_generated_at",
              "record_origin", "historical_reconstruction")
    changes = [{"field": key, "left": left.get(key), "right": right.get(key)} for key in fields if left.get(key) != right.get(key)]
    attachments = [{entry["relative_path"]: entry["sha256"] for entry in record.get("attachments", [])} for record in (left, right)]
    attachment_changes = [{"relative_path": key, "left_sha256": attachments[0].get(key), "right_sha256": attachments[1].get(key)}
                          for key in sorted(set(attachments[0]) | set(attachments[1])) if attachments[0].get(key) != attachments[1].get(key)]

    def ancestors(result_id):
        found, todo = set(), list(known[result_id].get("parent_result_ids", []))
        while todo:
            parent = todo.pop()
            if parent in known and parent not in found:
                found.add(parent)
                todo.extend(known[parent].get("parent_result_ids", []))
        return found

    relation = "same_result" if left_id == right_id else "left_is_ancestor" if left_id in ancestors(right_id) else "right_is_ancestor" if right_id in ancestors(left_id) else "no_visible_ancestor_relationship"
    limits = ["仅比较研究元数据、文件摘要、父版本关系、冻结旧项及截止时已追加的状态。",
              "文字或文件变化不能证明形成新结论、预测命中或实际盈亏。",
              "未核对可比日期及价格／财务口径，不计算数值变化。"]
    if left["module"] != right["module"]:
        limits.append("两份成果属于不同专业；并列查阅，不作直接方向评分。")
    return {"schema_version": SCHEMA, "kind": "comparison", "security_id": security_id, "asof": asof,
            "security": summaries[0]["security"], "left": summaries[0], "right": summaries[1],
            "metadata_changes": changes, "report_changed": left["report_sha256"] != right["report_sha256"],
            "attachment_changes": attachment_changes, "parent_relationship": relation, "items": items,
            "comparison_limits": limits, "numerical_comparison_performed": False, "_private": {"root": str(Path(root).absolute())}}


def _display(value):
    if value is None:
        return "未知"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _reason(value):
    if value.startswith("item_"):
        return "关注项" + STATUS_NAMES.get(value[5:], value[5:])
    return REASONS.get(value, value)


def render_view(document, out):
    """Write three standalone files into the caller's already guarded staging dir."""
    out = Path(out)
    if not out.is_dir():
        raise ValueError("Output staging directory must already exist")
    if document.get("kind") not in {"archive_view", "comparison"}:
        raise ValueError("Unsupported view kind")
    root = document["_private"]["root"]
    escape = lambda value: html.escape(_display(value), quote=True)
    company = document["security"].get("company", document["security_id"])
    title = ("成果对照：" if document["kind"] == "comparison" else "个股档案：") + company
    body, markdown = [f"<h1>{escape(title)}</h1>"], [f"# {html.escape(title)}"]

    def paragraph(value):
        body.append(f"<p>{escape(value)}</p>")
        markdown.append(html.escape(_display(value)))

    def heading(value, level=2):
        body.append(f"<h{level}>{escape(value)}</h{level}>")
        markdown.append("#" * level + " " + html.escape(_display(value)))

    def listing(values):
        values = list(values)
        body.append("<ul>" + "".join(f"<li>{escape(value)}</li>" for value in values) + "</ul>")
        markdown.extend("- " + html.escape(_display(value)).replace("\n", " ") for value in values)

    def file_uri(entry):
        try:
            return _source_file(root, entry["path"], entry["sha256"]).as_uri()
        except (OSError, ValueError):
            return None

    def file_paragraph(entry):
        uri = file_uri(entry)
        if uri is None:
            paragraph(entry["label"] + "：文件不可用或未通过核验")
        else:
            body.append(f'<p><a href="{escape(uri)}">{escape(entry["label"])}</a></p>')
            markdown.append(f'[{html.escape(entry["label"])}](<{uri}>)')

    def primary_report_uri(record):
        for entry in record["files"]:
            if PurePosixPath(entry["path"]).name == "report.html":
                uri = file_uri(entry)
                if uri:
                    return uri
        return file_uri(record["files"][0])

    def item_card(item):
        paragraph(f"{item.get('item_id', '')} · {STATUS_NAMES.get(item.get('status'), item.get('status', '未知'))} · {item.get('text', '')}")
        if item.get("definition"):
            body.append("<details><summary>原定判据</summary>")
            paragraph("原定判据：" + "；".join(FIELD_NAMES.get(key, key) + "：" + _display(value) for key, value in item["definition"].items()))
            body.append("</details>")
        paragraph("当前可用：" + ("是" if item.get("current_eligible") else "需复核／仅历史") + "；" + "；".join(_reason(reason) for reason in item.get("reasons", [])))
        if item.get("applied_review_ids"):
            paragraph("已追加回顾：" + "、".join(item["applied_review_ids"]))

    def result_card(record):
        day = record.get("research_asof")
        day = day[:10] if day else "研究日期未知"
        body.append(f'<details class="result"><summary>{escape(day)} · {escape(MODULE_NAMES.get(record["module"], record["module"]))} · {escape(record["result_id"])} · 查看详情</summary>')
        heading(f"{MODULE_NAMES.get(record['module'], record['module'])} · {record['result_id']}", 3)
        paragraph("内容角色："+ROLE_NAMES.get(record.get("content_role"),"角色待核"))
        paragraph(f"研究截止：{_display(record.get('research_asof'))}；数据截止：{_display(record.get('data_asof'))}；生成：{record['generated_at']}")
        if record.get("content_role") in {"broker_source","source_adaptation"}:
            if record.get("source_title"):
                paragraph("资料标题："+_display(record["source_title"]))
            paragraph("补充资料公开："+_display(record.get("published_date") or record.get("published_at"))+
                      "；日期精度："+_display(record.get("publication_precision"))+"；归档："+_display(record.get("imported_at")))
            paragraph("仅作为来源、观点与预测的补充，不替代正式基本面研究。")
        paragraph("历史可查；" + ("有当前可用关注项" if record.get("current_eligible") else "当前使用需复核") + "。")
        if record.get('reasons'):
            paragraph('引用资格说明：' + '；'.join(_reason(reason) for reason in record['reasons']))
        for item in record["item_states"]:
            item_card(item)
        if not record["item_states"]:
            paragraph("这份成果没有结构化摘要，请打开原报告。")
        file_paragraph(record["files"][0])
        if len(record["files"]) > 1:
            body.append(f'<details class="attachments"><summary>附件（{len(record["files"]) - 1}）</summary>')
            for entry in record["files"][1:]:
                file_paragraph(entry)
            body.append("</details>")
        if record["evidence"]:
            body.append(f'<details><summary>来源依据（{len(record["evidence"])}）</summary>')
            listing("来源：" + source.get("source", "未知") + "；公开：" + _display(source.get("public_at") or source.get("public_date")) for source in record["evidence"])
            body.append("</details>")
        body.append("</details>")

    paragraph(f"证券：{document['security_id']}；股类：{document['security'].get('share_class', '未知')}；币种：{document['security'].get('currency', '未知')}")
    paragraph("查档截止：" + document["asof"])
    if document["kind"] == "archive_view":
        counts = document["counts"]
        paragraph(f"可见成果 {counts['visible_results']} 份，筛选后 {counts['displayed_results']} 份；全部历史未结项 {counts['pending_items']} 项。")
        heading("正式研究与补充资料")
        selection=document.get("research_selection",{})
        for key,label in (("formal_fundamental","基本面"),("formal_technical","技术面")):
            paragraph(label+"最近正式研究："+(selection.get(key) or "未识别；需核验内容角色后选择"))
        paragraph("补充来源 "+str(counts.get("supplementary_sources",0))+" 份；资料更新不会覆盖正式主结论。")
        if document.get("role_resolution")=="unavailable":
            paragraph("当前研究室工具未提供统一内容角色识别，未猜测哪一份是最新正式研究。全部可见历史仍保留。")
        heading("查找范围")
        filters = document["filters"]
        paragraph(f"模块：{MODULE_NAMES.get(filters['module'], '全部')}；研究日期：{filters['date_from'] or '不限'} 至 {filters['date_to'] or '不限'}；关键词：{filters['query'] or '不限'}")
        listing(document["filter_notes"])
        heading("按研究日期回看")
        body.append('<div class="timeline"><table><thead><tr><th scope="col">研究日期</th><th scope="col">专业</th><th scope="col">成果</th><th scope="col">报告</th></tr></thead><tbody>')
        for result in document["results"]:
            day = result.get("research_asof")
            day = day[:10] if day else "研究日期未知"
            report_uri = primary_report_uri(result)
            link = f'<a href="{escape(report_uri)}">打开报告</a>' if report_uri else "报告未通过核验"
            role=ROLE_NAMES.get(result.get("content_role"),"角色待核")
            body.append(f'<tr><td>{escape(day)}</td><td>{escape(MODULE_NAMES.get(result["module"], result["module"])+" · "+role)}</td><td>{escape(result["result_id"])}</td><td>{link}</td></tr>')
        body.append("</tbody></table></div>")
        for result in document["results"]:
            result_card(result)
        if not document["results"]:
            paragraph("当前查找范围没有成果。")
        heading("全部历史未结项")
        for item in document["pending_items"]:
            paragraph("原成果：" + item["source_result_id"] + "；模块：" + MODULE_NAMES.get(item["module"], item["module"]))
            item_card(item)
        if not document["pending_items"]:
            paragraph("当前截止没有未结项。")
        body.append(f'<details class="history"><summary>父版本与回顾（{len(document["parent_chains"])} 份成果）</summary>')
        heading("父版本与回顾")
        listing(record["result_id"] + " ← " + ("、".join(record["parent_result_ids"]) or "无可见父版本") for record in document["parent_chains"])
        listing("回顾 " + record["review_id"] + "；涉及旧项：" + "、".join(record["item_ids"]) for record in document["reviews"])
        body.append("</details>")
        heading("计划与成交回顾")
        for record in document["plans"]:
            adoption = {"research_only": "仅研究", "adopted": "已采纳", "withdrawn": "已撤回"}.get(record["adoption"].get("status"), "未知")
            paragraph("计划 " + record["plan_id"] + "；版本 " + record["plan_version_id"] + "；生成 " + record["generated_at"] + "；采纳 " + adoption)
            if record.get('previous_plan_not_visible'):
                paragraph('前序计划版本在本次截止不可见，未展开关联。')
            listing(FIELD_NAMES[field] + "：" + record[field] for field in ("trigger", "confirmation", "invalidation"))
            paragraph("退出条件：" + record["exit"] + "；有效期间：" + record["effective_at"] + " 至 " + record["valid_until"])
        for record in document["executions"]:
            paragraph("成交记录 " + record["import_id"] + "；" + ("真实反馈" if record["execution_kind"] == "real" else "模拟") + "；生成 " + record["generated_at"] + f"；{len(record['trades'])} 条成交")
            for trade in record["trades"]:
                paragraph("成交 " + trade["trade_id"] + "；时间 " + trade["executed_at"] + "；" + {"buy": "买入", "sell": "卖出"}.get(trade["side"], trade["side"]) + "；币种 " + trade["currency"])
                if trade.get("correction_of_trade_id"):
                    paragraph("更正原成交：" + trade["correction_of_trade_id"])
                listing("对应计划 " + version["plan_id"] + "／" + version["plan_version_id"] for version in trade["plan_versions"])
                if trade.get('unavailable_plan_links_count'):
                    paragraph(f"另有{trade['unavailable_plan_links_count']}项计划关联在本次截止不可见，未展示标识；不等于未分配或未交易。")
        paragraph("计划、真实反馈、模拟及更正分别保留；未计算盈亏。")
        heading("来源概览")
        overview = document["source_overview"]
        paragraph(f"全部可见成果：{overview['report_count']} 份报告、{overview['attachment_count']} 个附件、{overview['evidence_count']} 条原始来源记录。附件在原档打开，不复制入本页。")
    else:
        listing(document["comparison_limits"])
        relation = {"same_result": "同一成果", "left_is_ancestor": "左侧是右侧的前序版本", "right_is_ancestor": "右侧是左侧的前序版本", "no_visible_ancestor_relationship": "没有可见的前序版本关系"}
        paragraph("报告文件：" + ("摘要不同" if document["report_changed"] else "摘要相同") + "；父版本关系：" + relation[document["parent_relationship"]])
        heading("研究元数据变化")
        listing(FIELD_NAMES.get(record["field"], record["field"]) + "：" + _display(record["left"]) + " → " + _display(record["right"]) for record in document["metadata_changes"])
        heading("附件摘要变化")
        listing(record["relative_path"] + "：" + _display(record["left_sha256"]) + " → " + _display(record["right_sha256"]) for record in document["attachment_changes"])
        for side in ("left", "right"):
            result_card(document[side])
        heading("冻结旧项与追加状态")
        for group in document["items"]:
            heading(group["result_id"], 3)
            paragraph("冻结原记录")
            for item in group["frozen_items"]:
                item_card(item)
            paragraph("查档截止时的状态")
            for item in group["states_at_asof"]:
                item_card(item)
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            f'<title>{escape(title)}</title><style>body{{max-width:980px;margin:36px auto;padding:0 20px;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;color:#243447;background:#fafafa}}h1,h2,h3{{line-height:1.35}}h2{{margin-top:36px;border-bottom:1px solid #cad4df;padding-bottom:8px}}p,li,td,th,summary{{overflow-wrap:anywhere}}a{{color:#145b9a}}li{{margin:6px 0}}details{{margin:12px 0}}summary{{cursor:pointer;font-weight:600}}details.result{{padding:12px;border:1px solid #cad4df;border-radius:8px;background:white}}details details{{padding-left:12px;border-left:2px solid #e0e6ed}}table{{width:100%;border-collapse:collapse;table-layout:fixed}}th,td{{padding:8px;text-align:left;vertical-align:top;border-bottom:1px solid #dce3eb}}th:first-child{{width:22%}}th:nth-child(2){{width:14%}}th:last-child{{width:18%}}.timeline{{margin:16px 0 24px}}@media(max-width:560px){{body{{margin:20px auto;padding:0 12px;font-size:14px}}th,td{{padding:6px 4px}}}}</style><body>'
            + "\n".join(body) + "</body></html>\n")
    outputs = {"report.html": page, "report.md": "\n\n".join(markdown) + "\n",
               "archive-view.json": json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"}
    for name in outputs:
        if (out / name).exists():
            raise FileExistsError("View output already exists")
    for name, content in outputs.items():
        with (out / name).open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    return [str((out / name).absolute()) for name in outputs]
