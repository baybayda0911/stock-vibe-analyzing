"""Private, append-only research records; Python 3.10+ standard library only.

Commands: init ROOT; reindex ROOT; list/context ROOT --security ID --asof ISO;
archive/import/review/plan/execution ROOT --request-json ABSOLUTE_JSON_PATH.
All paths are absolute. Known timestamps include an offset. stdout is JSON;
errors are JSON on stderr (exit 2). Saved records survive catalog update failure.
The catalog is disposable; records and appended reviews are authoritative.
No network, Git, broker, model service, or return calculation is used here.

Minimal archive request (fictional security; report_file must already exist):
{
  "security": {"security_id":"ZZ-TEST-DEMO", "company":"Fictional Co",
    "market":"ZZ", "exchange":"TEST", "code":"DEMO",
    "share_class":"ordinary", "currency":"TST"},
  "module":"fundamental", "module_version":"0.1.0", "result_id":"R1",
  "generated_at":"2026-01-02T18:00:00+08:00",
  "research_asof":"2026-01-02T17:00:00+08:00",
  "data_asof":"2026-01-02T16:00:00+08:00", "parent_result_ids":[],
  "report_file":"/ABSOLUTE/report.md",
  "evidence":[{"evidence_id":"E1", "source":"supplied excerpt, page 1",
    "public_at":"2026-01-02T09:00:00+08:00"}],
  "items":[{"item_id":"I1", "type":"observation", "text":"Watch demand",
    "report_locator":"section 10, row 1", "evidence_ids":["E1"],
    "frozen_at":"2026-01-02T18:00:00+08:00", "status":"pending",
    "definition":{"what":"demand", "why":"revenue driver", "next_evidence":"next filing"},
    "current_use":"needs_review", "review_due_at":"2026-02-02T18:00:00+08:00"}]
}
current_use: eligible/needs_review/historical. eligible requires nonempty
current_use_reason, evidence_ids and valid_until. This is an explicit claim by
the researcher; dates alone never grant eligibility. All item types require a
frozen definition; see DEFINITION_FIELDS below. Parent items use parent_item_ids.
archive/review may use data_asof=null rather than invent data coverage hours.
Evidence may use public_at=null with public_at_precision="date_only"/"unknown"
and an optional public_date preserving a known source date. Unknown coverage or
publication time is retained for history and requires review for current use.
Optional attachments: [{"source_file":"/ABSOLUTE/chart.png",
 "relative_path":"chart.png", "media_type":"image/png"}]. JSON, PNG and HTML
files are copied under their report-relative paths in the same atomic result.
Their media type, SHA256 and size are recorded and verified on every scan.
Missing attachments preserves the original archive/import contract. Copy or
registration failures leave only ignored staging files; retry the same result
ID after fixing the source. Never delete a completed result to retry indexing.

Review request: {"security_id":"ZZ-TEST-DEMO", "review_id":"RV1",
 "generated_at":"2026-02-02T18:00:00+08:00",
 "research_asof":"2026-02-02T17:00:00+08:00",
 "data_asof":"2026-02-02T16:00:00+08:00", "report_file":"/ABSOLUTE/review.md",
 "evidence":[], "updates":[{"item_id":"I1", "status":"cannot_determine",
 "reason":"No new filing", "evidence_ids":[], "current_use":"needs_review"}]}
Reviews never edit frozen text/definitions. Missing evidence cannot renew a claim.

Import uses the archive fields plus import_id and source_file (original bytes).
generated_at/frozen_at are assigned the actual import time. research_asof and
data_asof may be null; unknown research_asof goes to undated. Optional
original_generated_at requires nonempty original_date_evidence. Date evidence
is a researcher assertion, not a trusted timestamp; imported items remain
prospective_status=unverified. report_file is the separately written adaptation.
If import returns partial=true, its original is saved and the adaptation is not;
retry the same import_id/result_id and original metadata to resume safely. After
a process crash, first verify the writer has stopped before removing its lock.

Plan request: {"security_id":"ZZ-TEST-DEMO", "plan_id":"P1",
 "plan_version_id":"PV1", "version":1, "source_result_ids":["R1"],
 "generated_at":"2026-01-02T19:00:00+08:00", "frozen_at":"2026-01-02T19:00:00+08:00",
 "effective_at":"2026-01-03T09:00:00+08:00", "valid_until":"2026-02-02T18:00:00+08:00",
 "trigger":"condition", "confirmation":"rule", "invalidation":"rule", "exit":"rule",
 "adoption":{"status":"research_only"}}
Versions append consecutively; v2+ requires previous_plan_version_id. Adoption
adopted/withdrawn additionally requires at. User risk conditions may be recorded.

Execution request: {"security_id":"ZZ-TEST-DEMO", "import_id":"X1",
 "execution_kind":"real", "source_assertion":"user_statement",
 "generated_at":"2026-01-03T18:00:00+08:00", "source_file":"/ABSOLUTE/feedback.txt",
 "trades":[{"trade_id":"T1", "executed_at":"2026-01-03T10:00:00+08:00",
 "time_precision":"minute", "side":"buy", "quantity":"10", "price":"2",
 "currency":"TST", "fees":null,
 "allocations":[{"plan_id":"P1", "plan_version_id":"PV1", "quantity":"5"}]}]}
execution_kind is real/simulated; source_assertion is user_statement/user_supplied_record.
Each batch contains one execution month. Empty allocations remain unmatched.
Corrections append a new trade_id with correction_of_trade_id/correction_reason.
Originals are retained. No newest-version inference and no P&L are performed.

Context returns results with current_eligible_item_ids and item_states/reasons,
pending_items across all historical versions, excluded_results (IDs only, no
future contents), explicit plan versions and separate real/simulated executions.
superseded_trade_ids names trades replaced by corrections visible at this asof;
original feedback stays in executions and must not be added to its replacement.
history_usable means retained for review, not verified or prospectively proven.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid


SCHEMA = "stock-research-lab/1"
MODULES = {"fundamental", "technical"}
ITEM_TYPES = {"prediction", "hypothesis", "management_commitment", "observation", "technical_scenario"}
DEFINITION_FIELDS = {
    "prediction": ("metric", "expected", "unit", "period", "deadline", "baseline", "tolerance", "evaluation_rule"),
    "hypothesis": ("proposition", "mechanism", "support_condition", "falsification_condition", "check_window"),
    "management_commitment": ("quote", "source_public_at", "metric", "period", "revision_context"),
    "observation": ("what", "why", "next_evidence"),
    "technical_scenario": ("timeframe", "inputs", "trigger", "confirmation", "invalidation", "observation_window"),
}
STATUSES = {"pending", "supported", "weakened", "falsified", "fulfilled", "partially_fulfilled",
            "not_fulfilled", "not_due", "unknown", "withdrawn", "expired", "not_triggered",
            "triggered_pending", "confirmed", "invalidated", "observed", "no_change", "cannot_determine"}
OPEN_STATUSES = {"pending", "supported", "weakened", "not_due", "unknown", "not_triggered",
                 "triggered_pending", "cannot_determine"}
INELIGIBLE_STATUSES = {"falsified", "withdrawn", "expired", "invalidated"}
IDENTITY_FIELDS = ("security_id", "company", "market", "exchange", "code", "share_class", "currency")
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
ATTACHMENT_MEDIA = {".json": "application/json", ".png": "image/png", ".html": "text/html"}
RECORD_FILES = {"manifest.json", "items.json", "review.json", "trades.json", "import.json",
                "security.json", "config.local.json", "catalog.json", "report.md"}


def now():
    return datetime.now(timezone.utc).isoformat()


def stamp(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO timestamp with timezone") from error
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} requires timezone")
    return parsed


def identifier(value, field):
    if (not isinstance(value, str) or not ID_PATTERN.fullmatch(value)
            or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value, re.IGNORECASE)):
        raise ValueError(f"Invalid {field}; use letters, digits, hyphens and underscores")
    return value


def nonempty(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value


def sequence(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array")
    return value


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def checked_path(value, *, reject_git=False):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Use an absolute path without traversal")
    for component in path.parts[1:]:
        if component.endswith((".", " ")) or ":" in component:
            raise ValueError("Unsafe path component")
    for part in reversed((path, *path.parents)):
        if os.path.lexists(part):
            if linked(part):
                raise ValueError("Linked/reparse paths are forbidden")
        if reject_git and os.path.lexists(part / ".git"):
            raise ValueError("Private root must not be inside a Git working tree (.git ancestor)")
    return path


def inside(root, *parts):
    target = root.joinpath(*parts)
    if not target.is_relative_to(root):
        raise ValueError("Path escapes private root")
    checked_path(target, reject_git=True)
    return target


def read_json(path):
    checked_path(path)
    with path.open(encoding="utf-8-sig") as handle:
        result = json.load(handle)
    if not isinstance(result, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return result


def write_json(path, value):
    checked_path(path, reject_git=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def copy_source(source, destination):
    source = checked_path(source)
    if not source.is_file():
        raise ValueError("Source must be a regular file")
    digest = hashlib.sha256()
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            digest.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return digest.hexdigest()


def attachment_entries(value, *, stored=False):
    entries, paths = sequence(value, "attachments"), set()
    required = {"relative_path", "media_type"} | ({"sha256", "size_bytes"} if stored else {"source_file"})
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("Attachment requires exactly its declared fields")
        name = nonempty(entry["relative_path"], "attachment.relative_path")
        path = PurePosixPath(name)
        if (path.is_absolute() or path.as_posix() != name or "\\" in name
                or any(part in {".", ".."} or part.casefold() == ".git"
                       or part.endswith((".", " ")) or re.search(r'[<>:"|?*\x00-\x1f]', part)
                       or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", part.split(".")[0].rstrip(" ."), re.IGNORECASE)
                       for part in path.parts)
                or path.name.casefold() in RECORD_FILES or re.fullmatch(r"v\d{3,}\.json", path.name, re.IGNORECASE)):
            raise ValueError("Unsafe or reserved attachment relative_path")
        folded = name.casefold()
        if any(folded == old or folded.startswith(old + "/") or old.startswith(folded + "/") for old in paths):
            raise ValueError("Attachment paths overlap or repeat")
        paths.add(folded)
        if path.suffix.lower() not in ATTACHMENT_MEDIA or entry["media_type"] != ATTACHMENT_MEDIA[path.suffix.lower()]:
            raise ValueError("Attachment media_type does not match JSON, PNG or HTML suffix")
        if stored:
            if (not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                    or type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0):
                raise ValueError("Invalid attachment fingerprint metadata")
        else:
            source = checked_path(entry["source_file"], reject_git=True)
            if not source.is_file():
                raise ValueError("Attachment source must be a regular file")
    return entries


def verify_attachments(directory, entries):
    for entry in attachment_entries(entries, stored=True):
        path = inside(directory, *PurePosixPath(entry["relative_path"]).parts)
        if not path.is_file():
            raise ValueError("Attachment is missing")
        payload = path.read_bytes()
        if len(payload) != entry["size_bytes"] or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError("Attachment fingerprint mismatch")


def lab_root(value):
    root = checked_path(value, reject_git=True)
    if root == root.parent:
        raise ValueError("A drive/filesystem root cannot be a private lab")
    config = read_json(inside(root, "config.local.json"))
    if config.get("schema_version") != SCHEMA or config.get("kind") != "private-research-lab":
        raise ValueError("Root is not an initialized private research lab")
    return root


@contextmanager
def writer(root):
    lock = inside(root, ".write.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, f"pid={os.getpid()} created_at={now()}\n".encode())
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        checked_path(lock, reject_git=True)
        lock.unlink()


def init(root):
    root = checked_path(root, reject_git=True)
    if root == root.parent:
        raise ValueError("A drive/filesystem root cannot be a private lab")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise FileExistsError("Initialization requires a missing or empty directory")
    root.mkdir(parents=True, exist_ok=True)
    with writer(root):
        write_json(inside(root, "config.local.json"), {
            "schema_version": SCHEMA, "kind": "private-research-lab", "created_at": now()})
        inside(root, "securities").mkdir()
        inside(root, "_staging").mkdir()
        with inside(root, "README.md").open("x", encoding="utf-8") as handle:
            handle.write("# 私有股票研究室\n\n研究记录与追加回顾是原始依据，不得覆盖；catalog.json 可从记录重建。\n"
                         "本目录须位于 Git 仓库之外；本工具不提供加密或自动备份。\n"
                         "写入进程崩溃可能留下 .write.lock；确认相关进程已停止后再处理该锁。\n"
                         "保留中断的 _staging 资料供核查；它们不属于已完成的归档。\n")
        write_json(inside(root, "catalog.json"), empty_catalog())
    return {"initialized": True, "schema_version": SCHEMA}


def empty_catalog():
    return {"schema_version": SCHEMA, "rebuilt_at": now(), "results": [], "reviews": [],
            "plans": [], "executions": [], "imports": []}


def scan(root):
    catalog = empty_catalog()
    for directory, folders, files in os.walk(inside(root, "securities"), followlinks=False):
        current = Path(directory)
        for name in folders + files:
            checked_path(current / name, reject_git=True)
        for name in files:
            path = current / name
            relative = path.relative_to(root).as_posix()
            if name == "manifest.json":
                manifest = read_json(path)
                if manifest.get("schema_version") != SCHEMA:
                    raise ValueError("Unsupported manifest schema")
                items_path = inside(root, *path.parent.relative_to(root).parts, "items.json")
                items = read_json(items_path)
                report = inside(root, *path.parent.relative_to(root).parts, "report.md")
                actual = hashlib.sha256(report.read_bytes()).hexdigest()
                if actual != manifest.get("report_sha256"):
                    raise ValueError("Report fingerprint mismatch")
                actual_items = hashlib.sha256(items_path.read_bytes()).hexdigest()
                if actual_items != manifest.get("items_sha256"):
                    raise ValueError("Frozen items fingerprint mismatch")
                verify_attachments(path.parent, manifest.get("attachments", []))
                catalog["results"].append({**manifest, "path": relative, "items": items["items"]})
            elif name == "review.json":
                catalog["reviews"].append({**read_json(path), "path": relative})
            elif name == "trades.json":
                catalog["executions"].append({**read_json(path), "path": relative})
            elif name == "import.json":
                catalog["imports"].append({**read_json(path), "path": relative})
            elif re.fullmatch(r"v\d{3,}\.json", name) and current.parent.name == "plans":
                catalog["plans"].append({**read_json(path), "path": relative})
    for group, key in (("results", "result_id"), ("reviews", "review_id"),
                       ("plans", "plan_version_id"), ("executions", "import_id"), ("imports", "import_id")):
        values = [record[key] for record in catalog[group]]
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate {key} in source records")
        catalog[group].sort(key=lambda record: record[key])
    item_ids = [item["item_id"] for result in catalog["results"] for item in result["items"]]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("Duplicate item_id in source records")
    return catalog


def update_catalog(root, catalog=None):
    catalog = scan(root) if catalog is None else catalog
    temporary = inside(root, f".catalog-{uuid.uuid4().hex}.tmp")
    write_json(temporary, catalog)
    destination = inside(root, "catalog.json")
    if destination.exists() and not destination.is_file():
        raise ValueError("Catalog destination is not a regular file")
    os.replace(temporary, destination)
    return catalog


def reindex(root):
    root = lab_root(root)
    with writer(root):
        catalog = update_catalog(root)
    return {"catalog_updated": True, "counts": {key: len(catalog[key]) for key in
            ("results", "reviews", "plans", "executions", "imports")}}


def committed(root, **values):
    response = {"saved": True, **values}
    try:
        update_catalog(root)
        response["catalog_updated"] = True
    except (OSError, ValueError, KeyError, TypeError) as error:
        response.update(catalog_updated=False, warning=f"Record saved; reindex required: {error}")
    return response


def security(value):
    if not isinstance(value, dict):
        raise ValueError("security must be an object")
    result = {key: nonempty(value.get(key), f"security.{key}") for key in IDENTITY_FIELDS}
    identifier(result["security_id"], "security_id")
    expected = "-".join(result[key] for key in ("market", "exchange", "code"))
    if result["security_id"] != expected:
        raise ValueError("security_id must equal market-exchange-code")
    return result


def ensure_security(root, identity):
    directory = inside(root, "securities", identity["security_id"])
    directory.mkdir(parents=True, exist_ok=True)
    destination = inside(root, "securities", identity["security_id"], "security.json")
    if destination.exists():
        if read_json(destination) != identity:
            raise ValueError("Security identity differs from the existing record")
    else:
        write_json(destination, identity)
    return directory


def times(request, *, allow_unknown=False):
    generated = stamp(request.get("generated_at"), "generated_at")
    if generated > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("generated_at is in the future")
    research_value, data_value = request.get("research_asof"), request.get("data_asof")
    research = None if allow_unknown and research_value is None else stamp(research_value, "research_asof")
    data = None if data_value is None else stamp(data_value, "data_asof")
    if research is not None and research > generated:
        raise ValueError("research_asof cannot exceed generated_at")
    if data is not None and data > generated:
        raise ValueError("data_asof cannot exceed generated_at")
    if data is not None and research is not None and data > research:
        raise ValueError("data_asof cannot exceed research_asof")
    return generated, research, data


def evidence_records(request, research, *, allow_unknown=False):
    records = sequence(request.get("evidence", []), "evidence")
    ids = set()
    for entry in records:
        if not isinstance(entry, dict):
            raise ValueError("Evidence entries must be objects")
        evidence_id = identifier(entry.get("evidence_id"), "evidence_id")
        if evidence_id in ids:
            raise ValueError("Duplicate evidence_id")
        ids.add(evidence_id)
        nonempty(entry.get("source"), "evidence.source")
        public_value = entry.get("public_at")
        public = None if public_value is None else stamp(public_value, "evidence.public_at")
        if public is None:
            nonempty(entry.get("public_at_precision", "unknown"), "evidence.public_at_precision")
        if public and public > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Evidence publication is in the future")
        if public and research and public > research:
            raise ValueError("Evidence was not public at research_asof")
    return records, ids


def current_fields(item, evidence_ids):
    if item.get("status", "pending") not in STATUSES:
        raise ValueError("Invalid item status")
    use = item.get("current_use", "needs_review")
    if use not in {"eligible", "needs_review", "historical"}:
        raise ValueError("Invalid current_use")
    for key in ("valid_until", "review_due_at", "invalidated_at"):
        if item.get(key) is not None:
            stamp(item[key], key)
    cited = sequence(item.get("evidence_ids", []), "evidence_ids")
    if any(value not in evidence_ids for value in cited):
        raise ValueError("Unknown evidence_id")
    if use == "eligible":
        nonempty(item.get("current_use_reason"), "current_use_reason")
        stamp(item.get("valid_until"), "eligible item valid_until")
        if not cited:
            raise ValueError("Eligible items require evidence_ids")


def normalized_result(request, catalog, *, imported=False):
    identity = security(request.get("security"))
    result_id = identifier(request.get("result_id"), "result_id")
    if any(record["result_id"] == result_id for record in catalog["results"]):
        raise FileExistsError("result_id already exists")
    module = request.get("module")
    if module not in MODULES:
        raise ValueError("module must be fundamental or technical")
    nonempty(request.get("module_version"), "module_version")
    generated, research, data = times(request, allow_unknown=imported)
    evidence, evidence_ids = evidence_records(request, research, allow_unknown=imported)
    parent_ids = sequence(request.get("parent_result_ids", []), "parent_result_ids")
    known = {record["result_id"]: record for record in catalog["results"]}
    for parent_id in parent_ids:
        if parent_id not in known or known[parent_id]["security"]["security_id"] != identity["security_id"]:
            raise ValueError("Parent result is missing or belongs to another security")
        if stamp(known[parent_id]["generated_at"], "parent.generated_at") > generated:
            raise ValueError("A parent result cannot be generated after its child")
    existing_ids = {item["item_id"] for record in catalog["results"] for item in record["items"]}
    items = sequence(request.get("items"), "items")
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Items must be objects")
        item = dict(item)
        item_id = identifier(item.get("item_id"), "item_id")
        if item_id in existing_ids:
            raise FileExistsError("item_id already exists; review it or use a new child item")
        existing_ids.add(item_id)
        if item.get("type") not in ITEM_TYPES:
            raise ValueError("Invalid item type")
        for field in ("text", "report_locator"):
            nonempty(item.get(field), f"item.{field}")
        definition = item.get("definition")
        if not isinstance(definition, dict):
            raise ValueError("Each item requires a frozen definition object")
        for field in DEFINITION_FIELDS[item["type"]]:
            nonempty(definition.get(field), f"definition.{field}")
        if item["type"] == "prediction":
            stamp(definition["deadline"], "definition.deadline")
        if item["type"] == "management_commitment":
            public = stamp(definition["source_public_at"], "definition.source_public_at")
            if research and public > research:
                raise ValueError("Management commitment was not public at research_asof")
        frozen = stamp(item.get("frozen_at"), "item.frozen_at")
        if frozen > generated:
            raise ValueError("frozen_at cannot exceed generated_at")
        parent_item_ids = sequence(item.get("parent_item_ids", []), "parent_item_ids")
        if any(value not in {entry["item_id"] for record in catalog["results"] for entry in record["items"]}
               for value in parent_item_ids):
            raise ValueError("Unknown parent_item_id")
        current_fields(item, evidence_ids)
        item.update(result_id=result_id, status=item.get("status", "pending"),
                    current_use=item.get("current_use", "needs_review"),
                    prospective_status="unverified" if imported else "recorded_at_archive")
        normalized.append(item)
    historical = request.get("historical_reconstruction", False)
    if not isinstance(historical, bool):
        raise ValueError("historical_reconstruction must be boolean")
    historical = historical or research is None or generated.astimezone(research.tzinfo).date() > research.date()
    if historical and not imported:
        for item in normalized:
            item["prospective_status"] = "historical_reconstruction"
    manifest = {"schema_version": SCHEMA, "result_id": result_id, "security": identity, "module": module,
                "module_version": request["module_version"], "generated_at": request["generated_at"],
                "research_asof": request.get("research_asof"), "data_asof": request.get("data_asof"),
                "parent_result_ids": parent_ids, "evidence": evidence,
                "historical_reconstruction": historical, "record_origin": "imported" if imported else "research"}
    return manifest, {"schema_version": SCHEMA, "result_id": result_id, "items": normalized}


def stage(root, kind):
    directory = inside(root, "_staging", f"{kind}-{uuid.uuid4().hex}")
    directory.mkdir()
    return directory


def result_destination(root, manifest):
    day = stamp(manifest["research_asof"], "research_asof").date().isoformat() if manifest["research_asof"] else "undated"
    return inside(root, "securities", manifest["security"]["security_id"], manifest["module"], day, manifest["result_id"])


def prepare_result(root, request, manifest, items):
    destination = result_destination(root, manifest)
    if os.path.lexists(destination):
        raise FileExistsError("Result destination already exists")
    attachments = attachment_entries(request.get("attachments", []))
    staging = stage(root, "result")
    manifest["report_sha256"] = copy_source(request["report_file"], staging / "report.md")
    if "attachments" in request:
        manifest["attachments"] = []
    for entry in attachments:
        target = inside(staging, *PurePosixPath(entry["relative_path"]).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = copy_source(entry["source_file"], target)
        payload = target.read_bytes()
        if entry["media_type"] == "image/png":
            if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Attachment PNG signature is invalid")
        else:
            text = payload.decode("utf-8-sig")
            if entry["media_type"] == "application/json":
                json.dumps(json.loads(text), allow_nan=False)
        manifest["attachments"].append({"relative_path": entry["relative_path"], "media_type": entry["media_type"],
                                        "sha256": digest, "size_bytes": len(payload)})
    write_json(staging / "items.json", items)
    manifest["items_sha256"] = hashlib.sha256((staging / "items.json").read_bytes()).hexdigest()
    write_json(staging / "manifest.json", manifest)
    return staging, destination


def move_record(root, staging, destination):
    inside(root, *destination.relative_to(root).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination):
        raise FileExistsError("Record already exists; overwrite is forbidden")
    # The exclusive root writer serializes cooperating processes; IDs never replace prior records.
    staging.rename(destination)


def archive(root, request):
    root = lab_root(root)
    with writer(root):
        manifest, items = normalized_result(request, scan(root))
        ensure_security(root, manifest["security"])
        staging, destination = prepare_result(root, request, manifest, items)
        move_record(root, staging, destination)
        return committed(root, result_id=manifest["result_id"], path=destination.relative_to(root).as_posix())


def import_report(root, request):
    root = lab_root(root)
    request = dict(request)
    request["generated_at"] = now()
    request["items"] = [dict(item, frozen_at=request["generated_at"]) for item in request.get("items", [])]
    import_id = identifier(request.get("import_id"), "import_id")
    original_at = request.get("original_generated_at")
    expected_digest = request.get("expected_original_sha256")
    if expected_digest is not None and (not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)):
        raise ValueError("Expected original fingerprint must be SHA256")
    if original_at is not None:
        original_time = stamp(original_at, "original_generated_at")
        nonempty(request.get("original_date_evidence"), "original_date_evidence")
        if original_time > stamp(request["generated_at"], "generated_at"):
            raise ValueError("Original generation date is in the future")
    with writer(root):
        catalog = scan(root)
        existing = next((record for record in catalog["imports"] if record["import_id"] == import_id), None)
        if existing and any(record.get("import_id") == import_id for record in catalog["results"]):
            raise FileExistsError("import_id already has a completed adaptation")
        manifest, items = normalized_result(request, catalog, imported=True)
        manifest.update(import_id=import_id, original_generated_at=original_at,
                        original_date_evidence=request.get("original_date_evidence"))
        ensure_security(root, manifest["security"])
        if existing:
            expected = {"result_id": manifest["result_id"], "security_id": manifest["security"]["security_id"],
                        "module": manifest["module"], "research_asof": manifest["research_asof"],
                        "data_asof": manifest["data_asof"], "original_generated_at": original_at,
                        "original_date_evidence": request.get("original_date_evidence")}
            if any(existing.get(key) != value for key, value in expected.items()):
                raise ValueError("Interrupted import metadata differs; preserve the original registration")
            original = inside(root, *Path(existing["path"]).parent.parts, "original", existing["source_name"])
            if hashlib.sha256(original.read_bytes()).hexdigest() != existing["original_sha256"]:
                raise ValueError("Interrupted import original fingerprint mismatch")
            if expected_digest is not None and existing["original_sha256"] != expected_digest:
                raise ValueError("Interrupted original differs from the expected fingerprint")
            manifest["original_sha256"] = existing["original_sha256"]
            staging, destination = prepare_result(root, request, manifest, items)
            move_record(root, staging, destination)
            return committed(root, result_id=manifest["result_id"], import_id=import_id, resumed=True,
                             path=destination.relative_to(root).as_posix())
        imported = stage(root, "import")
        (imported / "original").mkdir()
        source = checked_path(request["source_file"])
        digest = copy_source(source, imported / "original" / source.name)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("Original changed before import; no registered record was committed")
        manifest["original_sha256"] = digest
        registration = {"schema_version": SCHEMA, "import_id": import_id, "result_id": manifest["result_id"],
                        "security_id": manifest["security"]["security_id"], "imported_at": request["generated_at"],
                        "source_path": str(source), "source_name": source.name, "original_sha256": digest,
                        "original_generated_at": original_at, "original_date_evidence": request.get("original_date_evidence"),
                        "module": manifest["module"], "research_asof": request.get("research_asof"),
                        "data_asof": request.get("data_asof"), "prospective_status": "unverified"}
        write_json(imported / "import.json", registration)
        staging, destination = prepare_result(root, request, manifest, items)
        import_destination = inside(root, "securities", manifest["security"]["security_id"], "imports", import_id)
        move_record(root, imported, import_destination)
        try:
            move_record(root, staging, destination)
        except (OSError, ValueError) as error:
            return committed(root, import_id=import_id, result_id=manifest["result_id"], partial=True,
                             import_saved=True, result_saved=False,
                             warning=f"Original saved; retry the same import request to resume adaptation: {error}")
        return committed(root, result_id=manifest["result_id"], import_id=import_id,
                         path=destination.relative_to(root).as_posix())


def review(root, request, *, check=None):
    root = lab_root(root)
    with writer(root):
        catalog = scan(root)
        if check is not None:
            check(catalog)
        review_id = identifier(request.get("review_id"), "review_id")
        security_id = identifier(request.get("security_id"), "security_id")
        if any(record["review_id"] == review_id for record in catalog["reviews"]):
            raise FileExistsError("review_id already exists")
        generated, research, _ = times(request)
        evidence, ids = evidence_records(request, research)
        items = {item["item_id"]: (item, result) for result in catalog["results"] for item in result["items"]}
        updates, seen = sequence(request.get("updates"), "updates"), set()
        allowed = {"item_id", "status", "reason", "evidence_ids", "current_use", "current_use_reason",
                   "valid_until", "review_due_at", "invalidated_at"}
        for update in updates:
            if not isinstance(update, dict) or set(update) - allowed:
                raise ValueError("Review cannot overwrite frozen item definitions")
            item_id = update.get("item_id")
            if item_id not in items or item_id in seen:
                raise ValueError("Review item is missing or repeated")
            seen.add(item_id)
            original, result = items[item_id]
            if result["security"]["security_id"] != security_id:
                raise ValueError("Review security differs from item")
            if stamp(result["generated_at"], "result.generated_at") > generated:
                raise ValueError("Review predates its source result")
            if result["research_asof"] and stamp(result["research_asof"], "result.research_asof") > research:
                raise ValueError("Review research_asof predates its source result")
            nonempty(update.get("reason"), "update.reason")
            if update.get("status") not in STATUSES:
                raise ValueError("Review requires an explicit valid status")
            combined = {**original, **update}
            current_fields(combined, ids | set(original.get("evidence_ids", [])))
            if combined.get("current_use") == "eligible" and any(
                    field in update for field in ("current_use", "current_use_reason", "valid_until", "review_due_at")):
                current_fields({**update, "current_use": "eligible"}, ids)
        staging = stage(root, "review")
        record = {"schema_version": SCHEMA, "review_id": review_id, "security_id": security_id,
                  "generated_at": request["generated_at"], "research_asof": request["research_asof"],
                  "data_asof": request["data_asof"], "evidence": evidence, "updates": updates}
        record["report_sha256"] = copy_source(request["report_file"], staging / "review.md")
        write_json(staging / "review.json", record)
        destination = inside(root, "securities", security_id, "reviews", research.date().isoformat(), review_id)
        move_record(root, staging, destination)
        return committed(root, review_id=review_id, path=destination.relative_to(root).as_posix())


def plan(root, request):
    root = lab_root(root)
    with writer(root):
        catalog = scan(root)
        security_id = identifier(request.get("security_id"), "security_id")
        plan_id = identifier(request.get("plan_id"), "plan_id")
        version_id = identifier(request.get("plan_version_id"), "plan_version_id")
        generated = stamp(request.get("generated_at"), "generated_at")
        frozen = stamp(request.get("frozen_at"), "frozen_at")
        effective = stamp(request.get("effective_at"), "effective_at")
        expiry = stamp(request.get("valid_until"), "valid_until")
        if generated > datetime.now(timezone.utc) + timedelta(minutes=5) or frozen > generated or expiry < effective:
            raise ValueError("Invalid plan times")
        versions = [record for record in catalog["plans"] if record["plan_id"] == plan_id]
        version = request.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != len(versions) + 1:
            raise ValueError("Plan versions must append consecutively starting at 1")
        if any(record["plan_version_id"] == version_id for record in catalog["plans"]):
            raise FileExistsError("plan_version_id already exists")
        if versions:
            previous = max(versions, key=lambda record: record["version"])
            if (previous["security_id"] != security_id or request.get("previous_plan_version_id") != previous["plan_version_id"]
                    or effective < stamp(previous["effective_at"], "previous effective_at")):
                raise ValueError("Plan revision must cite its prior version and preserve security/effective ordering")
        sources = sequence(request.get("source_result_ids"), "source_result_ids")
        known = {result["result_id"]: result for result in catalog["results"]}
        if not sources or any(value not in known or known[value]["security"]["security_id"] != security_id
                              or stamp(known[value]["generated_at"], "source.generated_at") > generated for value in sources):
            raise ValueError("Plan requires existing source results for the same security")
        for field in ("trigger", "confirmation", "invalidation", "exit"):
            nonempty(request.get(field), field)
        adoption = request.get("adoption")
        if not isinstance(adoption, dict) or adoption.get("status") not in {"research_only", "adopted", "withdrawn"}:
            raise ValueError("Plan adoption status is required")
        if adoption["status"] != "research_only":
            if stamp(adoption.get("at"), "adoption.at") > generated:
                raise ValueError("Adoption cannot be in the future of this record")
        record = {**request, "schema_version": SCHEMA}
        staging = stage(root, "plan")
        name = f"v{version:03d}.json"
        write_json(staging / name, record)
        destination = inside(root, "securities", security_id, "plans", plan_id, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(destination):
            raise FileExistsError("Plan version already exists")
        (staging / name).rename(destination)
        staging.rmdir()
        return committed(root, plan_id=plan_id, plan_version_id=version_id, path=destination.relative_to(root).as_posix())


def positive(value, field, *, zero=False):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field}")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"Invalid {field}") from error
    if not number.is_finite() or number < 0 or (not zero and number == 0):
        raise ValueError(f"Invalid {field}")
    return number


def execution(root, request):
    root = lab_root(root)
    with writer(root):
        catalog = scan(root)
        security_id = identifier(request.get("security_id"), "security_id")
        import_id = identifier(request.get("import_id"), "import_id")
        generated = stamp(request.get("generated_at"), "generated_at")
        execution_kind = request.get("execution_kind")
        if execution_kind not in {"real", "simulated"}:
            raise ValueError("execution_kind must separate real and simulated trades")
        if request.get("source_assertion") not in {"user_statement", "user_supplied_record"}:
            raise ValueError("source_assertion is required")
        if generated > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("generated_at is in the future")
        if any(record["import_id"] == import_id for record in catalog["executions"]):
            raise FileExistsError("Execution import_id already exists")
        if not inside(root, "securities", security_id, "security.json").is_file():
            raise ValueError("Unknown security")
        known_trades = {trade["trade_id"]: {**trade, "_security_id": record["security_id"],
                                          "_kind": record["execution_kind"], "_generated_at": record["generated_at"]}
                        for record in catalog["executions"] for trade in record["trades"]}
        plans = {record["plan_version_id"]: record for record in catalog["plans"]}
        trades = sequence(request.get("trades"), "trades")
        if not trades:
            raise ValueError("Execution import requires trades")
        normalized, execution_months = [], set()
        seen = set(known_trades)
        corrected = {trade.get("correction_of_trade_id") for trade in known_trades.values()}
        for trade in trades:
            trade = dict(trade)
            trade_id = identifier(trade.get("trade_id"), "trade_id")
            if trade_id in seen:
                raise FileExistsError("Duplicate trade_id; append a new correction ID")
            seen.add(trade_id)
            executed = stamp(trade.get("executed_at"), "executed_at")
            if executed > generated:
                raise ValueError("Trade occurs after feedback generation")
            execution_months.add(executed.date().isoformat()[:7])
            if trade.get("time_precision") not in {"second", "minute", "day", "unknown"}:
                raise ValueError("Trade time_precision is required")
            if trade.get("side") not in {"buy", "sell"}:
                raise ValueError("Trade side must be buy or sell")
            quantity = positive(trade.get("quantity"), "quantity")
            positive(trade.get("price"), "price")
            nonempty(trade.get("currency"), "currency")
            if trade.get("fees") is not None:
                positive(trade["fees"], "fees", zero=True)
            correction = trade.get("correction_of_trade_id")
            if correction is not None:
                if (correction not in known_trades or correction in corrected
                        or known_trades[correction]["_security_id"] != security_id
                        or known_trades[correction]["_kind"] != execution_kind
                        or stamp(known_trades[correction]["_generated_at"], "original feedback.generated_at") > generated):
                    raise ValueError("Correction must cite an existing, not-yet-corrected trade")
                nonempty(trade.get("correction_reason"), "correction_reason")
                corrected.add(correction)
            total, reasons = Decimal(0), []
            allocations = sequence(trade.get("allocations", []), "allocations")
            allocated_versions = set()
            for allocation in allocations:
                if not isinstance(allocation, dict):
                    raise ValueError("Allocation must be an object")
                version_id = allocation.get("plan_version_id")
                if version_id not in plans or version_id in allocated_versions:
                    raise ValueError("Allocation requires a unique existing explicit plan_version_id")
                allocated_versions.add(version_id)
                target = plans[version_id]
                if target["security_id"] != security_id or target["plan_id"] != allocation.get("plan_id"):
                    raise ValueError("Allocation plan/security mismatch")
                total += positive(allocation.get("quantity"), "allocation quantity")
                if (stamp(target["generated_at"], "plan.generated_at") > executed
                        or stamp(target["frozen_at"], "plan.frozen_at") > executed):
                    reasons.append("version_not_available_at_execution")
                if executed < stamp(target["effective_at"], "effective_at") or executed > stamp(target["valid_until"], "valid_until"):
                    reasons.append("execution_outside_plan_window")
                if trade["time_precision"] in {"day", "unknown"}:
                    reasons.append("execution_version_time_needs_review")
            if total > quantity:
                raise ValueError("Allocated quantity exceeds trade quantity")
            trade.update(allocations=allocations, unallocated_quantity=str(quantity - total),
                         version_review_reasons=sorted(set(reasons)), fees=trade.get("fees"))
            normalized.append(trade)
        if len(execution_months) != 1:
            raise ValueError("Split execution imports into actual execution months")
        staging = stage(root, "execution")
        source = checked_path(request["source_file"])
        digest = copy_source(source, staging / ("source" + (source.suffix if source.suffix.lower() in {".txt", ".csv", ".json", ".md"} else ".bin")))
        if (any(record["source_sha256"] == digest and record["execution_kind"] == execution_kind
                and record["security_id"] == security_id for record in catalog["executions"])
                and not all(trade.get("correction_of_trade_id") for trade in normalized)):
            raise FileExistsError("Feedback source was already imported; use explicit corrections")
        record = {"schema_version": SCHEMA, "import_id": import_id, "security_id": security_id,
                  "generated_at": request["generated_at"], "source_sha256": digest, "trades": normalized,
                  "execution_kind": execution_kind, "source_assertion": request["source_assertion"], "pnl_computed": False}
        write_json(staging / "trades.json", record)
        month = next(iter(execution_months))
        destination = inside(root, "securities", security_id, "executions", month, import_id)
        move_record(root, staging, destination)
        return committed(root, import_id=import_id, path=destination.relative_to(root).as_posix(), pnl_computed=False)


CAPABILITIES = {"role_resolution": "stock-research-role-resolver/1"}


def _role_adapter():
    import importlib.util
    spec = importlib.util.spec_from_file_location("research_role_adapter", checked_path(Path(__file__).with_name("research_roles.py")))
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    return adapter


def _role_backend():
    from types import SimpleNamespace
    return SimpleNamespace(security=security, stamp=stamp, identifier=identifier, inside=inside, read_json=read_json,
        digest_file=lambda path: hashlib.sha256(checked_path(path, reject_git=True).read_bytes()).hexdigest())


def technical_identity(record, document):
    return _role_adapter().technical_identity(_role_backend(), record, document)


def resolve_roles(root, value, *, read_attachment=None, keep_technical=False):
    root = lab_root(root)
    if "security" not in value:
        value = {**value, "security": security(read_json(inside(root, "securities", value["security_id"], "security.json")))}
    def read(record, name):
        allowed = {entry["relative_path"]: entry for entry in record.get("attachments", [])}
        if name not in allowed:
            raise ValueError("Role adapter requires an explicitly archived attachment")
        path = inside(result_destination(root, record), *PurePosixPath(name).parts)
        payload = checked_path(path, reject_git=True).read_bytes()
        if len(payload) != allowed[name]["size_bytes"] or hashlib.sha256(payload).hexdigest() != allowed[name]["sha256"]:
            raise ValueError("Role attachment fingerprint differs")
        return payload, allowed[name]["media_type"], name
    return _role_adapter().resolve_roles(_role_backend(), root, value, read_attachment or read, keep_technical=keep_technical)


def context(root, security_id, asof, module=None, *, include_roles=True, _catalog=None):
    root = lab_root(root)
    identifier(security_id, "security_id")
    cutoff = stamp(asof, "asof")
    if module is not None and module not in MODULES:
        raise ValueError("Invalid module")
    # Read source records so a missing, stale or broken cache never hides pending work.
    catalog = scan(root) if _catalog is None else _catalog
    reviews = sorted(catalog["reviews"], key=lambda entry: (stamp(entry["research_asof"], "research_asof"),
                                                          stamp(entry["generated_at"], "generated_at"), entry["review_id"]))
    output, pending, excluded = [], [], []
    for result in catalog["results"]:
        if result["security"]["security_id"] != security_id or module and result["module"] != module:
            continue
        if stamp(result["generated_at"], "generated_at") > cutoff or result["research_asof"] and stamp(result["research_asof"], "research_asof") > cutoff:
            excluded.append({"result_id": result["result_id"], "reasons": ["not_available_at_asof"]})
            continue
        states = []
        for original in result["items"]:
            item = dict(original)
            effective_data_asof = result["data_asof"]
            evidence = {entry["evidence_id"]: entry for entry in result["evidence"]}
            applied_reviews = []
            for entry in reviews:
                if stamp(entry["generated_at"], "generated_at") > cutoff or stamp(entry["research_asof"], "research_asof") > cutoff:
                    continue
                for update in entry["updates"]:
                    if update["item_id"] == item["item_id"]:
                        item.update({key: value for key, value in update.items() if key not in {"reason", "evidence_ids"}})
                        item["evidence_ids"] = update.get("evidence_ids", item.get("evidence_ids", []))
                        evidence.update({entry["evidence_id"]: entry for entry in entry["evidence"]})
                        effective_data_asof = entry["data_asof"]
                        applied_reviews.append(entry["review_id"])
            reasons = []
            if item.get("current_use") != "eligible":
                reasons.append("explicit_current_review_required" if item.get("current_use") != "historical" else "historical_only")
            if item["status"] in INELIGIBLE_STATUSES:
                reasons.append("item_" + item["status"])
            for field, reason in (("valid_until", "validity_window_ended"), ("review_due_at", "review_due")):
                if item.get(field) and stamp(item[field], field) <= cutoff:
                    reasons.append(reason)
            if item.get("invalidated_at") and stamp(item["invalidated_at"], "invalidated_at") <= cutoff:
                reasons.append("invalidated")
            if not item.get("valid_until"):
                reasons.append("validity_window_unknown")
            cited = item.get("evidence_ids", [])
            if not cited or any(value not in evidence or not evidence[value].get("public_at") for value in cited):
                reasons.append("source_publication_time_unverified")
            if result["research_asof"] is None or effective_data_asof is None:
                reasons.append("research_or_data_asof_unknown")
            state = {**item, "current_eligible": not reasons, "history_usable": True,
                     "reasons": reasons, "applied_review_ids": applied_reviews, "data_asof": effective_data_asof}
            states.append(state)
            if item["status"] in OPEN_STATUSES:
                pending.append({**state, "module": result["module"], "source_result_id": result["result_id"]})
        eligible = [item["item_id"] for item in states if item["current_eligible"]]
        output.append({key: value for key, value in result.items() if key != "items"} | {
            "current_eligible": bool(eligible), "current_eligible_item_ids": eligible, "history_usable": True,
            "reasons": sorted({reason for item in states for reason in item["reasons"]}) or ([] if states else ["no_item_qualification"]),
            "original_items": result["items"], "item_states": states})
    output.sort(key=lambda record: (stamp(record["research_asof"], "research_asof") if record["research_asof"]
                                   else datetime.min.replace(tzinfo=timezone.utc),
                                   stamp(record["generated_at"], "generated_at"), record["result_id"]), reverse=True)
    selected_plans = [record for record in catalog["plans"] if record["security_id"] == security_id and stamp(record["generated_at"], "generated_at") <= cutoff]
    selected_executions = [record for record in catalog["executions"] if record["security_id"] == security_id and stamp(record["generated_at"], "generated_at") <= cutoff]
    superseded = {trade["correction_of_trade_id"] for record in selected_executions
                  for trade in record["trades"] if trade.get("correction_of_trade_id")}
    value = {"schema_version": SCHEMA, "security_id": security_id, "asof": asof,
            "results": output, "excluded_results": excluded, "pending_items": pending,
            "plans": selected_plans, "executions": selected_executions,
            "superseded_trade_ids": sorted(superseded), "pnl_computed": False}
    if include_roles:
        # A known empty security is useful to callers before its first result exists.
        profile = inside(root, "securities", security_id, "security.json")
        if profile.exists():
            value["roles"] = resolve_roles(root, value)[0]
            classified = {row["result_id"]: row for row in value["roles"]["classified"]}
            value["research_results"] = [row for row in output if classified[row["result_id"]]["content_role"] in {"full_research", "incremental_research", "correction"}]
            value["source_results"] = [row for row in output if classified[row["result_id"]]["content_role"] in {"source_adaptation", "broker_source"}]
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "archive", "import", "review", "plan", "execution", "reindex", "list", "context", "roles"):
        command = commands.add_parser(name)
        command.add_argument("root", type=Path)
        if name in {"archive", "import", "review", "plan", "execution"}:
            command.add_argument("--request-json", required=True, type=Path)
        if name in {"list", "context", "roles"}:
            command.add_argument("--security", required=True)
            command.add_argument("--asof", required=True)
            command.add_argument("--module", choices=sorted(MODULES))
    args = parser.parse_args(argv)
    try:
        if args.command in {"list", "context", "roles"}:
            result = context(args.root, args.security, args.asof, args.module)
            if args.command == "roles":
                result = result.get("roles", {"schema_version": "stock-research-roles/1", "resolver_version": CAPABILITIES["role_resolution"], "classified": [], "formal_fundamental": None, "formal_technical": None, "supplementary": [], "unknown": []})
        elif args.command in {"init", "reindex"}:
            result = {"init": init, "reindex": reindex}[args.command](args.root)
        else:
            request = read_json(checked_path(args.request_json))
            result = {"archive": archive, "import": import_report, "review": review,
                      "plan": plan, "execution": execution}[args.command](args.root, request)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"saved": False, "error": str(error), "error_type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
