"""Verified private-lab directory snapshots; the caller supplies its lab module.

Digests detect changed bytes, not authorship or trusted dates. Snapshots are
unencrypted; another directory on the same disk does not provide disk recovery.
"""

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile


SCHEMA = "stock-research-archive/2"
LEGACY_SCHEMA = "stock-research-archive/1"
_MANIFEST_FIELDS = {"schema_version", "kind", "created_at", "lab_schema_version", "files"}
_ROOT_FILES = {"README.md", "config.local.json"}
_WATCHLIST = "platform/watchlist.json"


def _relative(name):
    if not isinstance(name, str) or not name:
        raise ValueError("Snapshot paths must be nonempty relative text")
    path = PurePosixPath(name)
    if (path.is_absolute() or path.as_posix() != name or "\\" in name
            or any(part in {".", ".."} or part.casefold() == ".git"
                   or part.endswith((".", " ")) or re.search(r'[<>:"|?*\x00-\x1f]', part)
                   or re.fullmatch(r"CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]",
                                   part.split(".")[0].rstrip(" ."), re.IGNORECASE)
                   for part in path.parts)):
        raise ValueError("Unsafe or reserved snapshot path")
    return path


def _regular(lab, path):
    lab.checked_path(path, reject_git=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Snapshot files must be ordinary files without links")


def _walk(lab, root):
    lab.checked_path(root, reject_git=True)
    if not root.is_dir():
        raise ValueError("Snapshot directory is missing")
    files, directories, names = {}, set(), {}

    def failed(error):
        raise error

    for directory, folders, filenames in os.walk(root, followlinks=False, onerror=failed):
        current = Path(directory)
        lab.checked_path(current, reject_git=True)
        for name in folders + filenames:
            path = current / name
            relative = path.relative_to(root).as_posix()
            _relative(relative)
            folded = relative.casefold()
            if folded in names:
                raise ValueError("Snapshot paths repeat with different casing")
            names[folded] = relative
            lab.checked_path(path, reject_git=True)
            if name in folders:
                if not stat.S_ISDIR(path.stat().st_mode):
                    raise ValueError("Snapshot contains a non-directory")
                directories.add(relative)
            else:
                _regular(lab, path)
                files[relative] = path
    return files, directories


def _watchlist(lab, root):
    path = lab.checked_path(root / _WATCHLIST, reject_git=True)
    if os.path.lexists(path.parent) and not path.parent.is_dir():
        raise ValueError("Watchlist parent must be a directory")
    if not os.path.lexists(path):
        return None
    _regular(lab, path)
    value = lab.read_json(path)
    if (set(value) != {"schema_version", "updated_at", "securities"}
            or value["schema_version"] != "stock-research-watchlist/1"):
        raise ValueError("Unsupported watchlist schema")
    lab.stamp(value["updated_at"], "watchlist.updated_at")
    identities = set()
    for security in lab.sequence(value["securities"], "watchlist.securities"):
        identity = lab.security(security)
        if set(security) != set(lab.IDENTITY_FIELDS) or identity["security_id"] in identities:
            raise ValueError("Watchlist requires complete, unique security identities")
        identities.add(identity["security_id"])
    return path


def _source_files(lab, root):
    files, _ = _walk(lab, root / "securities")
    files = {"securities/" + name: path for name, path in files.items()}
    for name in _ROOT_FILES:
        path = root / name
        _regular(lab, path)
        files[name] = path
    watchlist = _watchlist(lab, root)
    if watchlist is not None:
        files[_WATCHLIST] = watchlist
    return files


def _fingerprint(lab, path):
    _regular(lab, path)
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"size_bytes": size, "sha256": digest.hexdigest()}


def _entries(lab, files):
    return [{"path": name, **_fingerprint(lab, path)} for name, path in sorted(files.items())]


def _manifest_entries(lab, manifest):
    if (set(manifest) != _MANIFEST_FIELDS or manifest["schema_version"] not in (SCHEMA, LEGACY_SCHEMA)
            or manifest["kind"] != "private-research-lab-snapshot"
            or manifest["lab_schema_version"] != lab.SCHEMA
            or not isinstance(manifest["files"], list)):
        raise ValueError("Unsupported snapshot manifest")
    lab.stamp(manifest["created_at"], "snapshot.created_at")
    files, directories, names = set(), {"securities"}, {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "size_bytes", "sha256"}:
            raise ValueError("Invalid snapshot file entry")
        path = _relative(entry["path"])
        allowed_watchlist = manifest["schema_version"] == SCHEMA and entry["path"] == _WATCHLIST
        if (entry["path"] not in _ROOT_FILES and not allowed_watchlist
                and (len(path.parts) < 2 or path.parts[0] != "securities")
                or type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0
                or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("Invalid snapshot file scope or fingerprint")
        if entry["path"] in files:
            raise ValueError("Duplicate snapshot file")
        files.add(entry["path"])
        for length in range(1, len(path.parts) + 1):
            name = "/".join(path.parts[:length])
            previous = names.setdefault(name.casefold(), name)
            if previous != name:
                raise ValueError("Snapshot paths repeat with different casing")
            if length < len(path.parts):
                directories.add(name)
    if not _ROOT_FILES.issubset(files) or files & directories:
        raise ValueError("Snapshot root files are missing or file paths overlap")
    return files, directories


def _counts(entries):
    return {"files": len(entries), "bytes": sum(entry["size_bytes"] for entry in entries)}


def _destination(lab, output, source, *, backup=False):
    output = lab.checked_path(output, reject_git=True)
    workspace = source / "_workspace" / "backups"
    internal_backup = backup and output != workspace and output.is_relative_to(workspace)
    if (output == output.parent or output == source
            or output.is_relative_to(source) and not internal_backup):
        raise ValueError("Unsafe output location within the source")
    if os.path.lexists(output):
        raise FileExistsError("Output already exists; snapshots and labs are never overwritten")
    if output.parent.exists() and not output.parent.is_dir():
        raise ValueError("Output parent must be a directory")
    _relative(output.name)
    for ancestor in output.parents:
        if os.path.lexists(ancestor / "SKILL.md"):
            raise ValueError("Private output cannot enter a Skill source or installation directory")
        config = ancestor / "config.local.json"
        if config.is_file():
            marker = lab.read_json(config)
            if marker.get("schema_version") == lab.SCHEMA and marker.get("kind") == "private-research-lab":
                lab.lab_root(ancestor)
                if output.is_relative_to(ancestor / "securities"):
                    raise ValueError("Output cannot enter an initialized lab's securities subtree")
    return output


def _copy(lab, files, entries, destination):
    for entry in entries:
        target = lab.inside(destination, *_relative(entry["path"]).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        _regular(lab, files[entry["path"]])
        digest = lab.copy_source(files[entry["path"]], target)
        if digest != entry["sha256"] or target.stat().st_size != entry["size_bytes"]:
            raise ValueError("Source changed while copying snapshot files")


def _cleanup(lab, staging, parent):
    if staging.parent != parent or not staging.name.startswith(".archive-"):
        raise ValueError("Refusing to remove an unrelated directory")
    lab.checked_path(staging, reject_git=True)
    shutil.rmtree(staging)


def verify_snapshot(lab, snapshot):
    """Verify every manifest entry, exact payload membership and lab records."""
    snapshot = lab.checked_path(snapshot, reject_git=True)
    if snapshot == snapshot.parent or not snapshot.is_dir():
        raise ValueError("Snapshot must be a directory")
    if {path.name for path in snapshot.iterdir()} != {"snapshot.json", "payload"}:
        raise ValueError("Snapshot must contain only snapshot.json and payload")
    _regular(lab, snapshot / "snapshot.json")
    manifest = lab.read_json(snapshot / "snapshot.json")
    expected_files, expected_directories = _manifest_entries(lab, manifest)
    payload = snapshot / "payload"
    files, directories = _walk(lab, payload)
    if set(files) != expected_files or directories != expected_directories:
        raise ValueError("Snapshot payload has extra or missing paths")
    actual = _entries(lab, files)
    if {entry["path"]: entry for entry in actual} != {entry["path"]: entry for entry in manifest["files"]}:
        raise ValueError("Snapshot file fingerprint mismatch")
    _watchlist(lab, payload)
    lab.lab_root(payload)
    lab.scan(payload)
    return {"verified": True, "snapshot": str(snapshot), "counts": _counts(actual), "manifest": manifest}


def backup(lab, root, output):
    """Publish a full-file snapshot only after source and copied bytes agree."""
    root = lab.lab_root(root)
    output = _destination(lab, output, root, backup=True)
    staging = None
    try:
        with lab.writer(root):
            lab.scan(root)
            entries = _entries(lab, _source_files(lab, root))
            manifest = {"schema_version": SCHEMA, "kind": "private-research-lab-snapshot",
                        "created_at": lab.now(), "lab_schema_version": lab.SCHEMA, "files": entries}
            _manifest_entries(lab, manifest)
            output.parent.mkdir(parents=True, exist_ok=True)
            _destination(lab, output, root, backup=True)
            staging = Path(tempfile.mkdtemp(prefix=".archive-", dir=output.parent))
            payload = staging / "payload"
            (payload / "securities").mkdir(parents=True)
            _copy(lab, _source_files(lab, root), entries, payload)
            if _entries(lab, _source_files(lab, root)) != entries:
                raise ValueError("Source file set or bytes changed during backup")
            lab.write_json(staging / "snapshot.json", manifest)
            verify_snapshot(lab, staging)
            _destination(lab, output, root, backup=True)
            os.rename(staging, output)
            staging = None
    finally:
        if staging is not None:
            _cleanup(lab, staging, output.parent)
    return {"saved": True, "snapshot": str(output), "counts": _counts(entries), "schema_version": SCHEMA}


def restore(lab, snapshot, output):
    """Restore unchanged records into a new lab and rebuild its derived catalog."""
    snapshot = lab.checked_path(snapshot, reject_git=True)
    output = _destination(lab, output, snapshot)
    verified = verify_snapshot(lab, snapshot)
    manifest = verified["manifest"]
    output.parent.mkdir(parents=True, exist_ok=True)
    _destination(lab, output, snapshot)
    staging = Path(tempfile.mkdtemp(prefix=".archive-", dir=output.parent))
    try:
        (staging / "securities").mkdir()
        files, _ = _walk(lab, snapshot / "payload")
        _copy(lab, files, manifest["files"], staging)
        (staging / "_staging").mkdir()
        catalog = lab.reindex(staging)
        if _entries(lab, _source_files(lab, staging)) != sorted(manifest["files"], key=lambda entry: entry["path"]):
            raise ValueError("Restored files do not match the snapshot")
        if verify_snapshot(lab, snapshot)["manifest"] != manifest:
            raise ValueError("Snapshot changed during restoration")
        _destination(lab, output, snapshot)
        os.rename(staging, output)
        staging = None
    finally:
        if staging is not None:
            _cleanup(lab, staging, output.parent)
    return {"restored": True, "root": str(output), "counts": verified["counts"],
            "catalog_updated": True, "catalog_counts": catalog["counts"]}
