"""Shared finite role adapters; frozen files and independent date qualifications remain authoritative."""

from datetime import date, timedelta, timezone
import json
import math

RESOLVER = "stock-research-role-resolver/1"
FORMAL = {"full_research", "incremental_research", "correction"}
SOURCE_ROLES = {"source_adaptation", "broker_source"}
SHANGHAI = timezone(timedelta(hours=8))

def _pick(value, names):
    return {key: value[key] for key in names if key in value}

def _json(payload):
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Research JSON numbers must be finite")
        return number
    def invalid(value):
        raise ValueError("Nonfinite JSON value: " + value)
    value = json.loads(payload.decode("utf-8-sig"), parse_float=finite, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("Research attachment must be a JSON object")
    return value

def _day(value):
    if value is None:
        return None
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Dates must use YYYY-MM-DD")
    return date.fromisoformat(value)

def _publication(backend, value):
    published = value.get("published_at")
    published_date = value.get("published_date")
    if published is not None:
        stamp = backend.stamp(published, "published_at")
        if published_date is not None and _day(published_date) != stamp.astimezone(SHANGHAI).date():
            raise ValueError("Publication date and timestamp differ")
        return {"published_at": published, "published_date": stamp.astimezone(SHANGHAI).date().isoformat(),
                "publication_precision": "timestamp"}
    if published_date is not None:
        _day(published_date)
    return {"published_at": None, "published_date": published_date,
            "publication_precision": "date_only" if published_date else "unknown"}


def technical_identity(backend, record, document):
    if backend.security(document.get("security")) != record["security"]:
        raise ValueError("Technical attachment and archived security identity differ")
    generated = backend.stamp(document.get("generated_at"), "analysis.generated_at")
    asof = backend.stamp(document.get("asof"), "analysis.asof")
    market = backend.stamp(document.get("market_cutoff"), "analysis.market_cutoff")
    if market > asof or asof > generated or generated > backend.stamp(record["generated_at"], "generated_at"):
        raise ValueError("Technical attachment dates exceed its frozen research")
    # Legacy reports use asof for calculation context; the manifest retains the independent research cutoff.
    if not isinstance(document.get("timeframes"), dict):
        raise ValueError("Technical attachment has no timeframe mapping")


def resolve_roles(backend, root, context, read_attachment, *, keep_technical=False):
    documents, readers, classified, selected_technical = {}, {}, [], None
    def read(record, name):
        key = record["result_id"], name
        if key in documents:
            return documents[key]
        if record["result_id"] not in readers:
            readers[record["result_id"]] = lambda name=None, record=record: read_attachment(record, name)
        return _json(readers[record["result_id"]](name)[0])
    for record in context["results"]:
        document = None
        attachments = {entry["relative_path"]: entry for entry in record.get("attachments", [])}
        value = {**_pick(record, ("result_id", "report_id", "module", "generated_at", "research_asof", "data_asof",
                                 "original_generated_at", "record_origin", "historical_reconstruction", "current_eligible", "reasons")),
                 "content_role": "unknown", "adapter": None, "reason": "没有可识别的内容角色或已知旧档模式",
                 "published_at": None, "published_date": None, "publication_precision": "unknown",
                 "imported_at": record["generated_at"] if record.get("record_origin") == "imported" else None,
                 "acquired_at": None, "extracted_at": None}
        if "research-components.json" in attachments:
            component = read(record, "research-components.json")
            role = component.get("content_role")
            if component.get("schema_version") != "stock-research-components/1" or role not in FORMAL | SOURCE_ROLES:
                value["reason"] = "组件模式或角色未知，仅保留历史查阅"
            else:
                if (backend.security(component.get("security")) != record["security"]
                        or component.get("result_id") != record["result_id"]):
                    raise ValueError("Component identity differs from its archived research")
                components = component.get("components")
                if not isinstance(components, list) or any(not isinstance(row, dict) or
                    not isinstance(row.get("component_id"), str) or not row["component_id"].strip() or
                    not isinstance(row.get("kind"), str) or not row["kind"].strip() for row in components):
                    raise ValueError("Components must declare their identifiers and content kinds")
                if len({row["component_id"] for row in components}) != len(components):
                    raise ValueError("Component identifiers must be unique")
                value.update(content_role=role, adapter="research-components/1", reason="明示内容角色；未据此进行跨源数值合并",
                             **_publication(backend, component))
                if component.get("source_ref") is not None:
                    ref = component["source_ref"]
                    if (not isinstance(ref, dict) or set(ref) != {"security_id", "import_id", "original_sha256"}
                            or ref["security_id"] != record["security"]["security_id"]
                            or ref["import_id"] != record.get("import_id")
                            or ref["original_sha256"] != record.get("original_sha256")):
                        raise ValueError("Source reference differs from its registered import")
                    backend.identifier(ref["import_id"], "import_id")
                    imported = backend.read_json(backend.inside(root, "securities", ref["security_id"],
                                                              "imports", ref["import_id"], "import.json"))
                    if (imported.get("security_id") != ref["security_id"] or imported.get("import_id") != ref["import_id"]
                            or imported.get("result_id") != record["result_id"]
                            or imported.get("original_sha256") != ref["original_sha256"]
                            or backend.stamp(imported["imported_at"], "imported_at") > backend.stamp(record["generated_at"], "generated_at")
                            or backend.stamp(imported["imported_at"], "imported_at") > backend.stamp(context["asof"], "asof")):
                        raise ValueError("Source import identity or chronology differs")
                    original = backend.inside(root, "securities", ref["security_id"], "imports", ref["import_id"],
                                              "original", imported["source_name"])
                    if backend.digest_file(original) != ref["original_sha256"]:
                        raise ValueError("Registered source original fingerprint differs")
                    value["source_ref"] = dict(ref)
                    value["imported_at"] = imported["imported_at"]
                for key in ("cover_report_date", "original_publication_date", "original_publication_precision", "source_title"):
                    if key in component:
                        value[key] = component[key]
                generated = backend.stamp(record["generated_at"], "generated_at")
                if value["published_at"] and backend.stamp(value["published_at"], "published_at") > generated:
                    raise ValueError("Publication occurs after its archived source was generated")
                if value["published_date"] and _day(value["published_date"]) > generated.astimezone(SHANGHAI).date():
                    raise ValueError("Publication occurs after its archived source was generated")
                for key in ("acquired_at", "extracted_at"):
                    if component.get(key) is not None:
                        if backend.stamp(component[key], key) > generated:
                            raise ValueError("Source processing occurs after its archived record")
                        value[key] = component[key]
        elif record["module"] == "technical" and "analysis.json" in attachments:
            document = read(record, "analysis.json")
            if document.get("schema_version") == "stock-technical-report/2":
                technical_identity(backend, record, document)
                value.update(content_role="full_research", adapter="legacy-technical-report/2", reason="已知技术研究模式；导入不改变原研究及数据资格")
            elif document.get("schema_version") == "stock-harmonic-report/1" and record.get("record_origin") == "imported":
                meta = document.get("input_metadata", {})
                identity = meta.get("security", {})
                if (not isinstance(identity, dict) or any(identity.get(key) != record["security"][key]
                        for key in ("market", "exchange", "code", "currency"))
                        or identity.get("security_id") != record["security"]["code"] + "." + record["security"]["exchange"].upper()):
                    raise ValueError("Legacy imported analysis identity differs from its complete archive identity")
                if (backend.stamp(meta.get("asof"), "legacy.asof") > backend.stamp(meta.get("generated_at"), "legacy.generated_at")
                        or backend.stamp(meta["generated_at"], "legacy.generated_at") > backend.stamp(record["generated_at"], "generated_at")):
                    raise ValueError("Legacy imported analysis exceeds its generation time")
                value.update(content_role="full_research", adapter="legacy-imported-harmonic/1",
                             reason="由完整导入身份绑定的已知谐波专项研究；不扩充为完整五周期结果")
            else:
                value["reason"] = "旧技术附件模式未知，仅保留历史查阅"
        elif record["module"] == "fundamental" and "financial-bottom-tables.json" in attachments:
            document = read(record, "financial-bottom-tables.json")
            tables = document.get("tables", {})
            if ("schema_version" not in document and isinstance(tables, dict)
                    and all(isinstance(tables.get(key), list) or
                            isinstance(tables.get(key), dict) and isinstance(tables[key].get("rows"), list)
                            for key in ("annual", "quarterly", "cumulative", "vintages"))
                    and isinstance(document.get("unit"), str) and document["unit"].startswith("CNY")):
                if backend.stamp(document.get("generated_at"), "bottom_tables.generated_at") > backend.stamp(record["generated_at"], "generated_at"):
                    raise ValueError("Financial bottom tables exceed their generation time")
                value.update(content_role="full_research", adapter="legacy-financial-bottom-tables/1", reason="已知长历史底表研究；不代表最新单期覆盖")
        elif record["module"] == "fundamental" and "calculations.json" in attachments:
            document = read(record, "calculations.json")
            if ("schema_version" not in document and isinstance(document.get("financial_period"), str)
                    and all(isinstance(document.get(key), dict) for key in
                            ("current_inputs_Q3_CNY", "derived", "valuation", "formulas", "inheritance"))):
                if backend.stamp(document.get("generated_at"), "calculations.generated_at") > backend.stamp(record["generated_at"], "generated_at"):
                    raise ValueError("Financial calculations exceed their generation time")
                value.update(content_role="incremental_research", adapter="legacy-financial-incremental/1", reason="已知最近单期增量；不替换全历史底表")
        classified.append(value)
        if selected_technical is None and record["module"] == "technical" and value["content_role"] in FORMAL:
            selected_technical = record["result_id"]
            if keep_technical and document is not None:
                documents[(record["result_id"], "analysis.json")] = document
        document = None
    def latest(module):
        return next((row for row in classified if row["module"] == module and row["content_role"] in FORMAL), None)
    roles = {"schema_version": "stock-research-roles/1", "resolver_version": RESOLVER,
             "security": context["security"], "asof": context["asof"],
             "formal_fundamental": latest("fundamental"), "formal_technical": latest("technical"),
             "supplementary": sorted((row for row in classified if row["content_role"] in SOURCE_ROLES),
                                      key=lambda row: row["published_date"] or "", reverse=True),
             "unknown": [row for row in classified if row["content_role"] == "unknown"], "classified": classified,
             "broker_import_enabled": False,
             "notes": ["资料按发布日期标记，取得或生成较晚不更新正式主结论。",
                       "重大事件、政策或行业影响须形成新的正式完整、增量或更正研究，才更新主结论。",
                       "角色识别不等于当前引用合格；各项有效期和资料截止另行检查。"]}
    return roles, documents, readers

