"""Read-only continuity over frozen items, appended reviews and explicit checks.

This projection never turns prose into a numerical rule, writes a review, infers
trades or computes a prediction hit rate. Optional rule/fact attachments are
small, hash-bound archive documents, rather than another scan of chart payloads.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math

from .dashboard import _json


SCHEMA = "stock-research-continuity/1"
LABELS = {"pending": "待复核", "not_due": "观察窗口未结束", "needs_evidence": "待补证据",
          "incomparable": "口径不可比", "candidate": "机器候选，待确认", "reviewed": "已有正式回顾",
          "observation": "持续观察，不计预测命中"}
REASON_CATEGORIES = ["source_or_calculation", "operating_assumption", "technical_invalidation",
                     "timing", "execution", "external_or_unknown"]


def definition_digest(definition):
    """Canonical binding used by a declared adapter, never a guessed criterion."""
    return hashlib.sha256(json.dumps(definition, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Verification numbers must be finite and explicitly numeric")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Verification " + name + " must be a nonempty string")
    return value


def _source(record, classification=None):
    classification = classification or {}
    return {key: deepcopy(record.get(key)) for key in
            ("result_id", "report_id", "module", "generated_at", "research_asof", "data_asof",
             "parent_result_ids", "historical_reconstruction", "record_origin", "original_generated_at")} | {
                 "content_role": classification.get("content_role", "unknown")} | {
                     key: deepcopy(classification.get(key)) for key in
                     ("published_at", "published_date", "publication_precision", "imported_at", "acquired_at", "extracted_at", "adapter")}


def _attachments(store, context):
    """Reuse shared role adapters; inspect only declared verification documents."""
    roles, rules, facts, warnings = {}, [], [], []
    shared = (getattr(store.lab, "CAPABILITIES", {}).get("role_resolution") == "stock-research-role-resolver/1"
              and callable(getattr(store.lab, "resolve_roles", None)))
    if not shared:
        raise ValueError("Research lab backend lacks shared role resolution; update the backend first")
    resolved, _, _ = store.lab.resolve_roles(store.root, context,
        read_attachment=lambda record, name: store._record_reader(record)(name), keep_technical=False)
    roles = {row["result_id"]: row for row in resolved["classified"]}
    for record in context["results"]:
        names = {entry["relative_path"] for entry in record.get("attachments", [])}
        reader = None
        for name, schema, group in (("verification-rules.json", "stock-research-verification/1", rules),
                                    ("verification-facts.json", "stock-research-facts/1", facts)):
            if name not in names:
                continue
            reader = reader or store._record_reader(record)
            document = _json(reader(name)[0])
            if document.get("schema_version") != schema:
                warnings.append({"result_id": record["result_id"], "attachment": name, "reason": "unknown_schema"})
                continue
            if (store.lab.security(document.get("security")) != context["security"]
                    or document.get("result_id") != record["result_id"]):
                raise ValueError("Continuity attachment differs from its complete archived identity")
            entries = document.get("rules" if group is rules else "facts")
            if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
                raise ValueError("Verification attachments require an explicit list of objects")
            if group is facts and roles[record["result_id"]]["content_role"] not in {"full_research", "incremental_research", "correction"}:
                warnings.append({"result_id": record["result_id"], "attachment": name,
                                 "reason": "supplementary_or_unknown_source_is_not_new_observed_fact"})
                continue
            group.extend((record, name, deepcopy(row)) for row in entries)
    return roles, rules, facts, warnings


def _criterion(value):
    if not isinstance(value, dict):
        raise ValueError("Verification criterion must be explicit")
    if set(value) - {"metric", "period", "unit", "basis", "operator", "target", "tolerance"}:
        raise ValueError("Unsupported verification criterion fields cannot be ignored")
    for key in ("metric", "period", "unit", "basis"):
        _text(value.get(key), "criterion." + key)
    operator = value.get("operator")
    if operator not in {"gte", "lte", "eq", "between"}:
        raise ValueError("Unsupported deterministic verification operator")
    target = value.get("target")
    if operator == "between":
        if not isinstance(target, list) or len(target) != 2 or _number(target[0]) > _number(target[1]):
            raise ValueError("Verification range must contain ordered finite bounds")
    else:
        _number(target)
    if _number(value.get("tolerance", 0)) < 0:
        raise ValueError("Verification tolerance cannot be negative")
    return value


def _rule_for(store, item, source, raw_rules, cutoff):
    matches = []
    for container, name, value in raw_rules:
        if value.get("item_id") != item["item_id"]:
            continue
        _text(value.get("rule_id"), "rule_id")
        if (value.get("source_result_id") != source["result_id"]
                or value.get("original_report_locator") != item["report_locator"]
                or value.get("definition_sha256") != definition_digest(item["definition"])):
            raise ValueError("Verification rule does not bind the frozen original item")
        declared = store.lab.stamp(value.get("declared_at"), "rule.declared_at")
        if declared > store.lab.stamp(container["generated_at"], "rule.source.generated_at"):
            raise ValueError("Verification rule declaration exceeds its archived freeze")
        _criterion(value.get("criterion"))
        if value.get("window_end") is not None:
            store.lab.stamp(value["window_end"], "rule.window_end")
        matches.append(value | {"attachment": name, "rule_result_id": container["result_id"],
                               "rule_generated_at": container["generated_at"],
                               "report_id": container["report_id"]})
    if len(matches) > 1:
        return None, ["multiple_explicit_rules_require_selection"]
    return (matches[0] if matches else None), []


def _facts_for(store, rule, raw_facts, cutoff):
    exact, mismatches, missing = [], [], []
    criterion = rule["criterion"]
    for record, name, fact in raw_facts:
        if fact.get("metric") != criterion["metric"] or fact.get("period") != criterion["period"]:
            continue
        _text(fact.get("fact_id"), "fact_id")
        if any(fact.get(key) != criterion[key] for key in ("unit", "basis")):
            mismatches.append({"result_id": record["result_id"], "fact_id": fact["fact_id"],
                               "unit": fact.get("unit"), "basis": fact.get("basis"), "reason": "unit_or_basis_mismatch"})
            continue
        value = _number(fact.get("value"))
        ids = fact.get("evidence_ids")
        evidence = {entry["evidence_id"]: entry for entry in record.get("evidence", [])}
        if not isinstance(ids, list) or not ids or any(key not in evidence for key in ids):
            missing.append("fact_has_no_bound_evidence")
            continue
        public_at, data_asof = fact.get("public_at"), fact.get("data_asof")
        if public_at is None or data_asof is None or any(not evidence[key].get("public_at") for key in ids):
            missing.append("fact_publication_or_data_asof_unverified")
            continue
        public = store.lab.stamp(public_at, "fact.public_at")
        data = store.lab.stamp(data_asof, "fact.data_asof")
        frozen = store.lab.stamp(record["generated_at"], "fact.source.generated_at")
        if public > cutoff or data > cutoff:
            missing.append("fact_not_available_at_asof")
            continue
        if (public > frozen or data > frozen
                or any(store.lab.stamp(evidence[key]["public_at"], "evidence.public_at") > public for key in ids)):
            raise ValueError("Verification fact precedes its cited publication or exceeds archive freeze")
        exact.append({**fact, "value": value, "source_result_id": record["result_id"],
                      "report_id": record["report_id"], "attachment": name,
                      "generated_at": record["generated_at"], "evidence": [deepcopy(evidence[key]) for key in ids]})
    if len({row["value"] for row in exact}) > 1:
        return [], mismatches, missing + ["conflicting_same_basis_facts_require_selection"]
    exact.sort(key=lambda row: (store.lab.stamp(row["public_at"], "fact.public_at"),
                                store.lab.stamp(row["generated_at"], "fact.generated_at")), reverse=True)
    return exact, mismatches, missing


def _compare(criterion, actual):
    target, tolerance = criterion["target"], criterion.get("tolerance", 0)
    operator = criterion["operator"]
    matched = (actual >= target - tolerance if operator == "gte" else
               actual <= target + tolerance if operator == "lte" else
               abs(actual - target) <= tolerance if operator == "eq" else
               target[0] - tolerance <= actual <= target[1] + tolerance)
    return "fulfilled" if matched else "not_fulfilled"


def _prospective(store, original, record, rule):
    reasons = []
    if original["type"] != "prediction":
        reasons.append("observation_or_hypothesis_is_not_a_numeric_prediction")
    if record.get("historical_reconstruction") or record.get("record_origin") == "imported":
        reasons.append("historical_or_imported_item_not_proven_prospective")
    if rule is None:
        reasons.append("no_frozen_structured_rule")
    elif (rule["rule_result_id"] != record["result_id"]
          or original["definition"].get("deterministic_rule") != rule["criterion"]
          or store.lab.stamp(rule["declared_at"], "rule.declared_at") > store.lab.stamp(original["frozen_at"], "item.frozen_at")):
        reasons.append("later_or_unverified_rule_mapping_cannot_be_backdated")
    deadline = original["definition"].get("deadline") if original["type"] == "prediction" else None
    if deadline and store.lab.stamp(deadline, "deadline") <= store.lab.stamp(record["generated_at"], "generated_at"):
        reasons.append("original_archive_not_frozen_before_prediction_deadline")
    return {"eligible": not reasons, "qualification": "prospective_frozen_numeric_rule" if not reasons else "not_scored",
            "reasons": reasons, "hit_rate_computed": False}


def _window(store, original, effective, rule, cutoff):
    deadline = original["definition"].get("deadline") if original["type"] == "prediction" else None
    # An adaptation cannot silently shorten the original prediction deadline.
    adapted_end = rule.get("window_end") if rule else None
    ends = [value for value in (deadline, adapted_end) if value is not None]
    window_end = max(ends, key=lambda value: store.lab.stamp(value, "window_end")) if ends else None
    due = window_end or effective.get("review_due_at")
    dates = {"review_due_at": effective.get("review_due_at"), "valid_until": effective.get("valid_until"),
             "deadline": deadline, "window_end": window_end, "adapted_window_end": adapted_end}
    dates["state"] = ("not_due" if due and store.lab.stamp(due, "window_end") > cutoff else "due" if due else "unknown")
    dates["note"] = "日期缺失表示观察窗口未明确，不推造到期日。" if dates["state"] == "unknown" else "按原始或明示适配的窗口判断。"
    return dates


def _next(original, state):
    definition = original["definition"]
    evidence = (definition.get("next_evidence") or definition.get("check_window")
                or definition.get("observation_window") or definition.get("period") or "核对原判据和同口径后续证据")
    action = "review_candidate" if state == "candidate" else "inspect_review" if state == "reviewed" else "wait_for_window" if state == "not_due" else "collect_evidence"
    return {"action": action, "label": "确认候选并追加回顾" if state == "candidate" else "查看正式回顾" if state == "reviewed" else "等待原观察窗口并核查新资料" if state == "not_due" else "补齐同口径证据后复核",
            "reason": evidence}


def continuity_dashboard(store, security_id, asof=None):
    context = store.context(security_id, asof=asof, include_roles=False)
    cutoff = store.lab.stamp(context["asof"], "asof")
    roles, rules, facts, warnings = _attachments(store, context)
    # Newer stores expose originals from their existing scan. The fallback also
    # works with installed older stores and never reads chart JSON a second time.
    originals = {}
    if any("original_items" not in row for row in context["results"]):
        _, catalog = store._scan()
        originals = {row["result_id"]: row["items"] for row in catalog["results"]}
    reviews = sorted(context["reviews"], key=lambda row: (
        store.lab.stamp(row["research_asof"], "research_asof"), store.lab.stamp(row["generated_at"], "generated_at"), row["review_id"]))
    items, timeline = [], []
    for record in context["results"]:
        classification = roles.get(record["result_id"], {})
        role = classification.get("content_role", "unknown")
        source = _source(record, classification)
        timeline.append({"event_type": "source" if role in {"broker_source", "source_adaptation"} else "result",
                         "event_id": record["result_id"], "report_id": record["report_id"],
                         "label": "补充资料" if role in {"broker_source", "source_adaptation"} else "基本面研究" if record["module"] == "fundamental" else "技术面研究",
                         "generated_at": record["generated_at"], "research_asof": record["research_asof"],
                         "data_asof": record["data_asof"], "source": source,
                         "published_at": source["published_at"], "published_date": source["published_date"],
                         "item_count": len(record["item_states"])})
        frozen = {row["item_id"]: row for row in record.get("original_items", originals.get(record["result_id"], []))}
        for effective in record["item_states"]:
            original = deepcopy(frozen[effective["item_id"]])
            item_reviews, review_evidence = [], []
            original_evidence = {entry["evidence_id"]: entry for entry in record.get("evidence", [])}
            evidence_pool = dict(original_evidence)
            previous_cited = original.get("evidence_ids", [])
            for review in reviews:
                for update in review["updates"]:
                    if update["item_id"] != original["item_id"]:
                        continue
                    evidence_pool.update({entry["evidence_id"]: entry for entry in review.get("evidence", [])})
                    cited = update.get("evidence_ids", previous_cited)
                    previous_cited = cited
                    used = [deepcopy(evidence_pool[key]) for key in cited if key in evidence_pool]
                    view = {key: deepcopy(review.get(key)) for key in ("review_id", "report_id", "generated_at", "research_asof", "data_asof")}
                    view.update(status=update["status"], reason=update["reason"], evidence_ids=list(cited),
                                evidence=used, current_use=update.get("current_use"),
                                reason_categories=[], reason_category_status="not_structured_in_original_review")
                    item_reviews.append(view)
                    review_evidence.extend(used)
            rule, rule_reasons = _rule_for(store, original, record, rules, cutoff)
            window = _window(store, original, effective, rule, cutoff)
            coverage = [{"code": reason, "source": "archive_qualification"} for reason in effective.get("reasons", [])]
            exact, mismatches, missing = _facts_for(store, rule, facts, cutoff) if rule else ([], [], [])
            prospective = _prospective(store, original, record, rule)
            latest_review = item_reviews[-1] if item_reviews else None
            candidate, state = None, "pending"
            if latest_review:
                status = latest_review["status"]
                state = ("not_due" if status == "not_due" else "needs_evidence" if status in {"unknown", "cannot_determine"}
                         else "pending" if status in {"pending", "not_triggered", "triggered_pending", "supported", "weakened"}
                         else "reviewed")
                coverage.append({"code": "formal_review_retained_without_new_machine_override", "source": "review"})
            elif window["state"] == "not_due":
                state = "not_due"
                coverage.append({"code": "original_observation_window_not_ended", "source": "window"})
            elif original["type"] in {"observation", "hypothesis", "management_commitment", "technical_scenario"}:
                state = "observation" if original["type"] == "observation" else "needs_evidence"
                coverage.append({"code": "qualitative_or_scenario_requires_explicit_research_review", "source": "definition"})
            elif rule is None:
                state = "needs_evidence"
                coverage.append({"code": "no_explicit_structured_rule_no_prose_threshold_inference", "source": "definition"})
            elif mismatches and not exact:
                state = "incomparable"
            elif exact and not missing:
                state = "candidate"
                candidate = _compare(rule["criterion"], exact[0]["value"])
                if store.lab.stamp(exact[0]["public_at"], "fact.public_at") <= store.lab.stamp(rule["rule_generated_at"], "rule_generated_at"):
                    prospective["eligible"] = False
                    prospective["qualification"] = "not_scored"
                    prospective["reasons"].append("outcome_not_later_than_rule_freeze")
            else:
                state = "incomparable" if "conflicting_same_basis_facts_require_selection" in missing else "needs_evidence"
            coverage.extend({"code": reason, "source": "explicit_rule"} for reason in rule_reasons + missing)
            coverage.extend({"code": row["reason"], "source": "explicit_fact", "fact": row} for row in mismatches)
            if not rule and original["type"] == "observation":
                coverage.append({"code": "observation_not_prediction_no_hit_rate", "source": "definition"})
            if not coverage:
                coverage.append({"code": "explicit_same_basis_facts_available", "source": "explicit_fact"})
            original_view = deepcopy(original) | {key: deepcopy(record.get(key)) for key in ("generated_at", "research_asof", "data_asof")}
            latest_ids = effective.get("evidence_ids", [])
            item = {"item_id": original["item_id"], "module": record["module"], "original": original_view,
                    "effective": {key: deepcopy(effective.get(key)) for key in
                                  ("status", "current_use", "current_use_reason", "current_eligible", "data_asof", "reasons")} |
                                 {"latest_review_id": latest_review["review_id"] if latest_review else None},
                    "source": source, "parent_item_ids": original.get("parent_item_ids", []),
                    "window": window, "prospective": prospective, "reviews": item_reviews,
                    "evidence": {"original": [deepcopy(original_evidence[key]) for key in original.get("evidence_ids", []) if key in original_evidence],
                                 "reviews": review_evidence,
                                 "latest": [deepcopy(evidence_pool[key]) for key in latest_ids if key in evidence_pool],
                                 "coverage": coverage, "facts": exact},
                    "check": {"state": state, "label": LABELS[state], "candidate_status": candidate,
                              "requires_confirmation": True, "coverage_reasons": [row["code"] for row in coverage], "rule": rule,
                              "persisted": False, "has_formal_review": latest_review is not None}, "next_step": _next(original, state)}
            items.append(item)
    for review in reviews:
        timeline.append({"event_type": "review", "event_id": review["review_id"], "report_id": review["report_id"],
                         "label": "追加回顾", "generated_at": review["generated_at"], "research_asof": review["research_asof"],
                         "data_asof": review["data_asof"], "item_ids": [entry["item_id"] for entry in review["updates"]]})
    timeline.sort(key=lambda row: (store.lab.stamp(row["generated_at"], "generated_at"), row["event_id"]), reverse=True)
    counts = Counter(row["check"]["state"] for row in items)
    queue = [{"item_id": row["item_id"], "source_result_id": row["source"]["result_id"], "module": row["module"],
              "state": row["check"]["state"], "label": row["check"]["label"], "text": row["original"]["text"],
              "reason": row["next_step"]["reason"], "next_step": row["next_step"],
              "effective_status": row["effective"]["status"], "data_asof": row["effective"]["data_asof"],
              "frozen_at": row["original"]["frozen_at"], "report_id": row["source"]["report_id"]}
             for row in items if row["effective"]["status"] in store.lab.OPEN_STATUSES]
    rank = {"candidate": 0, "incomparable": 1, "needs_evidence": 2, "pending": 3, "observation": 4, "reviewed": 5, "not_due": 6}
    queue.sort(key=lambda row: (rank[row["state"]], row["item_id"]))
    # Changing wall-clock seconds alone must not invalidate a confirmation. New
    # records, attachment fingerprints, review updates or changed window states do.
    revision = {"schema_version": SCHEMA, "security": context["security"],
                "records": sorted(({key: row.get(key) for key in ("result_id", "report_sha256", "items_sha256", "attachments",
                                     "generated_at", "research_asof", "data_asof", "parent_result_ids", "historical_reconstruction", "record_origin")}
                                   for row in context["results"]), key=lambda row: row["result_id"]),
                "reviews": [{key: row.get(key) for key in ("review_id", "report_sha256", "generated_at", "research_asof", "data_asof", "updates", "evidence")}
                            for row in reviews],
                "states": sorted(({"item_id": row["item_id"], "effective": row["effective"],
                                    "window_state": row["window"]["state"], "check": row["check"]} for row in items),
                                 key=lambda row: row["item_id"])}
    token = definition_digest(revision)
    return {"schema_version": SCHEMA, "security": context["security"], "security_id": security_id, "asof": context["asof"],
            "revision_token": token, "status": "available" if items else "no_items", "summary": {"total_items": len(items),
                "pending_items": len(queue), "not_due": counts["not_due"], "needs_evidence": counts["needs_evidence"],
                "incomparable": counts["incomparable"], "candidates": counts["candidate"],
                "reviewed": sum(row["check"]["has_formal_review"] for row in items),
                "observations": sum(row["original"]["type"] == "observation" for row in items),
                "results": len(context["results"]), "reviews": len(reviews)},
            "items": items, "attention_queue": queue, "timeline": timeline, "adapter_warnings": warnings,
            "reason_category_options": REASON_CATEGORIES, "excluded_results": context["excluded_results"],
            "pnl_computed": False, "hit_rate_computed": False, "read_only": True,
            "limitations": ["所有历史关注项保留原判据；最新报告没有新关注项不会清空旧项。",
                            "机器只执行明示结构化规则，结果是待人工确认的候选，不自动追加正式回顾。",
                            "观察、定性假设和回溯重建不冒充事前预测；不计算命中率或交易收益。",
                            "混合回顾的资料截止可为空；每条来源、证据、冻结日与回顾日期分别展示。",
                            "原回顾没有结构化原因分类时保留空分类和原原因，不从文字推断经营、择时或执行归因。"]}
