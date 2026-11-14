"""Local, read-only archive access and one explicitly scoped watchlist writer."""

import csv
from datetime import date
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid


WATCHLIST_SCHEMA = "stock-research-watchlist/1"
_TIMESTAMPS = {"generated_at", "original_generated_at", "research_asof", "data_asof",
               "frozen_at", "public_at", "source_public_at", "valid_until", "review_due_at",
               "invalidated_at", "effective_at", "executed_at", "imported_at", "at"}


class ResearchStore:
    def __init__(self, root: Path, lab_tool: Path, market_data_dir: Path | None = None):
        script = Path(__file__).resolve().parents[1] / "skills/stock-research-archive/scripts/research_archive.py"
        spec = importlib.util.spec_from_file_location("platform_archive_loader", script)
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        self.lab = loader.load_backend(lab_tool)
        self.root = self.lab.lab_root(root)
        self.market_data_dir = None if market_data_dir is None else self.lab.checked_path(market_data_dir)
        if self.market_data_dir is not None and not self.market_data_dir.is_dir():
            raise ValueError("Local identity index must be a directory")
        self._market_index = None
        self._identity_aliases = {}
        analyst_dir = self.market_data_dir.parent / 'stock-analyst-ranking' if self.market_data_dir else None
        self.analyst_data_dir = self.lab.checked_path(analyst_dir, reject_git=True) if analyst_dir and analyst_dir.is_dir() else None

    def _dates(self, value):
        if isinstance(value, dict):
            for key, entry in value.items():
                if key in _TIMESTAMPS and entry is not None:
                    self.lab.stamp(entry, key)
                elif key == "public_date" and entry is not None:
                    if not isinstance(entry, str) or date.fromisoformat(entry).isoformat() != entry:
                        raise ValueError("Invalid source public_date")
                elif isinstance(entry, (dict, list)):
                    self._dates(entry)
        elif isinstance(value, list):
            for entry in value:
                self._dates(entry)

    def _regular(self, path):
        path = self.lab.checked_path(path, reject_git=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Private files must be ordinary files without links")
        return path

    def _bytes(self, path, digest):
        path = self._regular(path)
        if not path.is_relative_to(self.root):
            raise ValueError("Archived source must stay in the private root")
        payload = path.read_bytes()
        if not isinstance(digest, str) or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("Archived source fingerprint mismatch")
        return payload

    def _identities(self):
        directory = self.lab.inside(self.root, "securities")
        if not directory.is_dir():
            raise ValueError("Private root has no securities directory")
        identities = {}
        for path in directory.iterdir():
            self.lab.checked_path(path, reject_git=True)
            if not path.is_dir():
                raise ValueError("Unexpected file in securities directory")
            identity = self.lab.security(self.lab.read_json(path / "security.json"))
            if identity["security_id"] != path.name:
                raise ValueError("Security directory and identity differ")
            identities[path.name] = identity
        return identities

    def _scan(self):
        self.lab.lab_root(self.root)
        identities = self._identities()
        directory = self.lab.inside(self.root, "securities")
        def fail(error):
            raise error
        for folder, dirs, _ in os.walk(directory, followlinks=False, onerror=fail):
            for name in dirs:
                if name in self.lab.RECORD_FILES or name == "review.md":
                    raise ValueError("An archived record file is a directory")
        catalog = self.lab.scan(self.root)
        for group in ("results", "reviews", "plans", "executions", "imports"):
            for record in catalog[group]:
                if record.get("schema_version") != self.lab.SCHEMA:
                    raise ValueError("Unsupported archived record schema")
                self._dates(record)
                security_id = record["security"]["security_id"] if group == "results" else record["security_id"]
                if security_id not in identities or Path(record["path"]).parts[1] != security_id:
                    raise ValueError("Archived record identity or directory differs")
                folder = self.lab.inside(self.root, *PurePosixPath(record["path"]).parent.parts)
                if group == "results":
                    if self.lab.security(record["security"]) != identities[security_id]:
                        raise ValueError("Archived security identities differ")
                    self.lab.identifier(record["result_id"], "result_id")
                    if record["module"] not in self.lab.MODULES:
                        raise ValueError("Unsupported archived module")
                    generated, research, _ = self.lab.times(record, allow_unknown=True)
                    _, evidence_ids = self.lab.evidence_records(record, research, allow_unknown=True)
                    for item in record["items"]:
                        self.lab.current_fields(item, evidence_ids)
                        if self.lab.stamp(item["frozen_at"], "frozen_at") > generated:
                            raise ValueError("Frozen item occurs after its result")
                        if item["type"] == "prediction":
                            self.lab.stamp(item["definition"]["deadline"], "prediction.deadline")
                    if folder != self.lab.result_destination(self.root, record):
                        raise ValueError("Result directory differs from frozen metadata")
                    items = self.lab.read_json(folder / "items.json")
                    if items.get("schema_version") != self.lab.SCHEMA or items.get("result_id") != record["result_id"]:
                        raise ValueError("Frozen items metadata differs from its result")
                    self._bytes(folder / "report.md", record["report_sha256"])
                elif group == "reviews":
                    self.lab.identifier(record["review_id"], "review_id")
                    _, research, _ = self.lab.times(record)
                    self.lab.evidence_records(record, research)
                    expected = self.lab.inside(self.root, "securities", security_id, "reviews", research.date().isoformat(), record["review_id"])
                    if folder != expected:
                        raise ValueError("Review directory differs from frozen metadata")
                    self._bytes(folder / "review.md", record["report_sha256"])
                elif group == "imports":
                    self.lab.stamp(record["imported_at"], "imported_at")
                    source = self.lab.inside(folder, "original", record["source_name"])
                    self._bytes(source, record["original_sha256"])
                else:
                    self.lab.stamp(record["generated_at"], "generated_at")
                    if group == "executions":
                        sources = [path for path in folder.glob("source.*") if path.is_file()]
                        if len(sources) != 1:
                            raise ValueError("Execution source is missing or ambiguous")
                        self._bytes(sources[0], record["source_sha256"])
        return identities, catalog

    def watchlist(self):
        self.lab.lab_root(self.root)
        path = self.lab.inside(self.root, "platform", "watchlist.json")
        if path.parent.exists() and not path.parent.is_dir():
            raise ValueError("Platform state must be a directory")
        if not path.exists():
            return {"schema_version": WATCHLIST_SCHEMA, "updated_at": None, "securities": []}
        self._regular(path)
        value = self.lab.read_json(path)
        if set(value) != {"schema_version", "updated_at", "securities"} or value["schema_version"] != WATCHLIST_SCHEMA:
            raise ValueError("Unsupported watchlist schema")
        self.lab.stamp(value["updated_at"], "watchlist.updated_at")
        securities = []
        for entry in self.lab.sequence(value["securities"], "securities"):
            if not isinstance(entry, dict) or set(entry) != set(self.lab.IDENTITY_FIELDS):
                raise ValueError("Watchlist requires exactly the complete identity fields")
            identity = self.lab.security(entry)
            profile = self.lab.inside(self.root, "securities", identity["security_id"], "security.json")
            if profile.exists():
                canonical = self.lab.security(self.lab.read_json(self._regular(profile)))
                identity = self._merge({identity["security_id"]: canonical}, [identity])[identity["security_id"]]
            securities.append(identity)
        if len({entry["security_id"] for entry in securities}) != len(securities):
            raise ValueError("Duplicate watchlist identity")
        return {**value, "securities": securities}

    def _merge(self, identities, entries):
        for identity in entries:
            key = identity["security_id"]
            if key in identities:
                if any(identities[key][field] != identity[field] for field in self.lab.IDENTITY_FIELDS if field != "company"):
                    raise ValueError("Conflicting local security identities")
                if identities[key]["company"] != identity["company"]:
                    self._identity_aliases.setdefault(key, set()).add(identity["company"])
            else:
                identities[key] = identity
        return identities

    def _local_index(self, query=None):
        targeted = False
        names = []
        if query is not None:
            query = query.casefold()
            identity = re.fullmatch(r"a-share-(sh|sz|bj)-(\d{6})", query)
            prefixed = re.fullmatch(r"(sh|sz|bj)(\d{6})", query)
            if identity or prefixed:
                match = identity or prefixed
                names = [match[1] + match[2] + ".csv"]
                targeted = True
            elif re.fullmatch(r"\d{6}", query):
                names = [prefix + query + ".csv" for prefix in ("sh", "sz", "bj")]
                targeted = True
            elif query.count("-") >= 2 and self.lab.ID_PATTERN.fullmatch(query):
                # Other complete identities have no mapping in this explicitly A-share index.
                return {}, []
        if not targeted and self._market_index is not None:
            return self._market_index
        entries, warnings = {}, []
        if self.market_data_dir is not None:
            self.lab.checked_path(self.market_data_dir)
            paths = ([self.market_data_dir / name for name in names if os.path.lexists(self.market_data_dir / name)]
                     if targeted else sorted(self.market_data_dir.glob("*.csv")))
            for path in paths:
                if not re.fullmatch(r"(?:sh|sz|bj)\d{6}\.csv", path.name, re.IGNORECASE):
                    continue
                # The shared parent was checked once above; each header still rejects linked files.
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or bool(getattr(info, "st_file_attributes", 0) & 0x400)):
                    raise ValueError("Local identity source must be an ordinary file without links")
                with path.open("rb") as source:
                    lines = [source.readline(65537) for _ in range(3)]
                if any(len(line) > 65536 for line in lines):
                    raise ValueError("Local identity header exceeds its size limit")
                for encoding in ("utf-8-sig", "gb18030"):
                    try:
                        text = b"".join(lines).decode(encoding)
                        break
                    except UnicodeDecodeError:
                        text = None
                if text is None:
                    warnings.append(path.name + ": 无法读取本地证券名称")
                    continue
                rows = list(csv.reader(text.splitlines()))
                header = next((i for i, row in enumerate(rows) if "股票代码" in row and "股票名称" in row), None)
                if header is None or len(rows) <= header + 1:
                    warnings.append(path.name + ": 缺少证券名称头行")
                    continue
                row = dict(zip(rows[header], rows[header + 1]))
                code, company = row.get("股票代码", ""), row.get("股票名称", "").strip()
                if code.casefold() != path.stem.casefold() or not company:
                    warnings.append(path.name + ": 证券身份与文件名不一致")
                    continue
                # The source explicitly supplies exchange prefixes; bare six-digit codes are never inferred.
                exchange = code[:2].casefold()
                identity = self.lab.security({"security_id": f"A-share-{exchange}-{code[2:]}", "company": company,
                    "market": "A-share", "exchange": exchange, "code": code[2:], "share_class": "A", "currency": "CNY"})
                entries[identity["security_id"]] = identity
        if not targeted:
            self._market_index = entries, warnings
        return entries, warnings

    def search(self, query):
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("Search must be text of at most 200 characters")
        query = query.strip().casefold()
        if not query:
            return {"securities": [], "warnings": [], "quote_status": "not_realtime"}
        identities, _ = self._scan()
        archive_ids = set(identities)
        tracked = self.watchlist()["securities"]
        self._merge(identities, tracked)
        had_name_index = self._market_index is not None
        local, warnings = self._local_index(query)
        # Archived identity is authoritative; old CSV first rows may contain former company names.
        for key, identity in local.items():
            identities.setdefault(key, identity)
        tracked_ids = {entry["security_id"] for entry in tracked}
        found = []
        for key, identity in sorted(identities.items()):
            aliases = " ".join(str(value) for value in identity.values()).casefold()
            if key in local:
                aliases += " " + local[key]["company"].casefold()
            aliases += " " + " ".join(self._identity_aliases.get(key, set())).casefold()
            if identity["market"] == "A-share" and identity["exchange"] in {"sh", "sz", "bj"}:
                aliases += " " + identity["exchange"] + identity["code"]
            if (identity["code"].casefold() == query if re.fullmatch(r"\d{6}", query) else query in aliases):
                found.append({**identity, "tracked": key in tracked_ids, "has_archive": key in archive_ids,
                              "identity_source": "archive" if key in archive_ids else "watchlist" if key in tracked_ids else "local_csv_header",
                              "name_status": "archived_identity" if key in archive_ids else "historical_name" if key in local else "watchlist_identity",
                              "name_note": "名称来自本地首条行情记录，是历史名称；当前正式公司名称待核。" if key in local and key not in archive_ids else None,
                              "requires_identity_confirmation": key not in archive_ids and key not in tracked_ids})
        return {"securities": found, "warnings": warnings, "quote_status": "not_realtime",
                "index_note": "首次按公司名称检索需要建立完整本地名称索引，可能较慢；精确代码可直接定位。"
                if not had_name_index and self._market_index is not None and self.market_data_dir is not None else None}

    def update_watchlist(self, security_id, tracked: bool):
        self.lab.identifier(security_id, "security_id")
        if type(tracked) is not bool:
            raise ValueError("tracked must be boolean")
        with self.lab.writer(self.root):
            current = self.watchlist()
            securities = {entry["security_id"]: entry for entry in current["securities"]}
            if (security_id in securities) == tracked:
                return current
            if tracked:
                identities, _ = self._scan()
                self._merge(identities, current["securities"])
                if security_id not in identities:
                    local, _ = self._local_index(security_id)
                    if security_id not in local:
                        raise KeyError("Unknown local security identity")
                    identities[security_id] = local[security_id]
                securities[security_id] = identities[security_id]
            else:
                securities.pop(security_id)
            value = {"schema_version": WATCHLIST_SCHEMA, "updated_at": self.lab.now(),
                     "securities": [securities[key] for key in sorted(securities)]}
            destination = self.lab.inside(self.root, "platform", "watchlist.json")
            destination.parent.mkdir(exist_ok=True)
            self.lab.checked_path(destination.parent, reject_git=True)
            temporary = self.lab.inside(self.root, "platform", ".watchlist-" + uuid.uuid4().hex + ".tmp")
            try:
                self.lab.write_json(temporary, value)
                self.lab.checked_path(destination, reject_git=True)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    self.lab.checked_path(temporary, reject_git=True)
                    temporary.unlink()
            return value

    def overview(self):
        identities, catalog = self._scan()
        tracked = self.watchlist()["securities"]
        self._merge(identities, tracked)
        tracked_ids = {entry["security_id"] for entry in tracked}
        securities = []
        for key, identity in sorted(identities.items()):
            results = [row for row in catalog["results"] if row["security"]["security_id"] == key]
            reviews = [row for row in catalog["reviews"] if row["security_id"] == key]
            dates = [row["research_asof"] for row in results + reviews if row.get("research_asof")]
            securities.append({**identity, "tracked": key in tracked_ids, "result_count": len(results),
                "review_count": len(reviews), "latest_research_asof": max(dates, key=lambda value: self.lab.stamp(value, "research_asof")) if dates else None})
        return {"securities": securities, "counts": {"securities": len(securities), "tracked": len(tracked),
                "results": len(catalog["results"]), "reviews": len(catalog["reviews"])}, "updated_at": self.lab.now()}

    def context(self, security_id, asof=None, module=None, *, include_roles=True):
        self.lab.identifier(security_id, "security_id")
        if module is not None and module not in self.lab.MODULES:
            raise ValueError("Invalid module")
        asof = self.lab.now() if asof is None else asof
        cutoff = self.lab.stamp(asof, "asof")
        identities, catalog = self._scan()
        self._merge(identities, self.watchlist()["securities"])
        if security_id not in identities:
            local, _ = self._local_index(security_id)
            if security_id not in local:
                raise KeyError("Unknown local security identity")
            identities[security_id] = local[security_id]
        # Context's established qualification rules stay in the supplied Lab implementation.
        context = self.lab.context(self.root, security_id, asof, include_roles=False, _catalog=catalog)
        results = []
        for record in context["results"]:
            if module is not None and record["module"] != module:
                continue
            value = {key: entry for key, entry in record.items() if key != "path"}
            value["report_id"] = "result:" + record["result_id"]
            results.append(value)
        reviews = []
        visible_items = {item["item_id"] for record in context["results"] if module is None or record["module"] == module for item in record["item_states"]}
        for record in catalog["reviews"]:
            if record["security_id"] != security_id or any(self.lab.stamp(record[key], key) > cutoff for key in ("generated_at", "research_asof")):
                continue
            if module is not None and not any(update["item_id"] in visible_items for update in record["updates"]):
                continue
            reviews.append({key: entry for key, entry in record.items() if key != "path"} | {"report_id": "review:" + record["review_id"]})
        reviews.sort(key=lambda row: self.lab.stamp(row["research_asof"], "research_asof"), reverse=True)
        result = {"security": identities[security_id], "security_id": security_id, "asof": asof, "module": module,
                "results": results, "reviews": reviews, "pending_items": context["pending_items"],
                "excluded_results": context["excluded_results"], "counts": {"results": len(results), "reviews": len(reviews),
                "pending_items": len(context["pending_items"])}, "pnl_computed": False}
        if include_roles:
            from .dashboard import research_roles
            result["roles"] = research_roles(self, result)
            classified = {row["result_id"]: row for row in result["roles"]["classified"]}
            for record in results:
                record.update({key: value for key, value in classified[record["result_id"]].items() if key in
                    {"content_role", "published_date", "published_at", "publication_precision", "source_ref", "cover_report_date", "source_title"}})
        return result

    def document_reader(self, record_id):
        if not isinstance(record_id, str) or ":" not in record_id:
            raise ValueError("Use a report_id returned by the archive API")
        kind, key = record_id.split(":", 1)
        self.lab.identifier(key, "record_id")
        if kind not in {"result", "review"}:
            raise ValueError("Unsupported report_id")
        _, catalog = self._scan()
        group, field, default = ("results", "result_id", "report.md") if kind == "result" else ("reviews", "review_id", "review.md")
        matches = [record for record in catalog[group] if record[field] == key]
        if not matches:
            raise KeyError("Unknown archived report")
        return self._record_reader(matches[0], kind)

    def _record_reader(self, record, kind="result"):
        # Only source records already checked by _scan/context reach this shared whitelist builder.
        default = "report.md" if kind == "result" else "review.md"
        allowed = {default: {"sha256": record["report_sha256"], "media_type": "text/markdown; charset=utf-8"}}
        if kind == "result":
            allowed.update({entry["relative_path"]: entry for entry in record.get("attachments", [])})
        if kind == "result":
            directory = self.lab.result_destination(self.root, record)
        else:
            directory = self.lab.inside(self.root, *PurePosixPath(record["path"]).parent.parts)
        def read(relative_path=None):
            name = default if relative_path is None else relative_path
            if not isinstance(name, str) or name not in allowed:
                raise ValueError("File is not an explicitly archived report or attachment")
            source = self.lab.inside(directory, *PurePosixPath(name).parts)
            return self._bytes(source, allowed[name]["sha256"]), allowed[name]["media_type"], name
        return read

    def document(self, record_id, relative_path=None):
        return self.document_reader(record_id)(relative_path)

    def research_roles(self, security_id, asof=None):
        return self.context(security_id, asof)["roles"]

    def continuity_dashboard(self, security_id, asof=None):
        from .continuity import continuity_dashboard
        return continuity_dashboard(self, security_id, asof=asof)

    def technical_dashboard(self, security_id, period="1d", *, asof=None, date_from=None, date_to=None, limit=252):
        from .dashboard import technical_dashboard
        return technical_dashboard(self, security_id, period, asof=asof,
                                   date_from=date_from, date_to=date_to, limit=limit)

    def fundamental_dashboard(self, security_id, asof=None):
        from .fundamentals import fundamental_dashboard
        return fundamental_dashboard(self, security_id, asof=asof)

    def comparison(self, security_ids, **options):
        from .comparison import compare
        return compare(self, security_ids, **options)

    def analyst_updates(self, security_id, *, limit=20):
        from .analyst_sources import analyst_updates
        from .broker_cache import enrich_updates
        context = self.context(security_id)
        result = analyst_updates(self, security_id, self.analyst_data_dir, limit=limit, context=context)
        return enrich_updates(self, result, context=context)
