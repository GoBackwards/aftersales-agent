"""Offline, content-addressed policy releases with explicit review gates.

Hashes prove local consistency, not publisher authenticity or reviewer identity.
The active pointer is read once by a service; existing requests retain their
selected immutable release. No fetch, model call, or business write occurs here.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import uuid

from .public_policy import _digest, _ordinary_path, _read_file, _timestamp
from .public_policy_bundles import PolicyEvidenceBundles
from .public_policy_structure import _json

SCHEMA = "knowledge-release-v1"
MATRIX_SCHEMA = "policy-support-matrix-v1"
REVIEWS_SCHEMA = "knowledge-reviews-v1"
CHANNELS = {"development", "reviewed"}
_BINDINGS = ("corpus_sha256", "structure_sha256", "relations_sha256")
_ROLES = ("corpus", "structure", "relations", "support_matrix", "reviews")


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value, name, limit=4000):
    if type(value) is not str or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"RELEASE_{name}_INVALID")
    return value


def _relative(value):
    _text(value, "PATH", 1024)
    if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or "\\" in value or any(c in value for c in (":", "?", "#"))
            or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ValueError("RELEASE_PATH_INVALID")
    return value


def _directory(value, *, create=False):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("RELEASE_PATH_TRAVERSAL")
    for component in reversed((path, *path.parents)):
        if component.exists() or component.is_symlink():
            _ordinary_path(component, file=False)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    _ordinary_path(path, file=False)
    return path


def _file(root, relative):
    path = root.joinpath(*PurePosixPath(_relative(relative)).parts)
    _ordinary_path(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("RELEASE_PATH_OUTSIDE_ROOT")
    return path


def _write_new(path, raw):
    _directory(path.parent, create=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def _lock(store):
    path = store / ".release.lock"
    try:
        handle = path.open("xb")
    except FileExistsError:
        raise ValueError("RELEASE_STORE_LOCKED: investigate the writer before removing a stale lock") from None
    try:
        with handle:
            handle.write(_bytes({"pid": os.getpid(), "created_at": _now()}))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        path.unlink()


def _bindings(corpus, structure, relations):
    return dict(zip(_BINDINGS, (_sha(_read_file(p)) for p in (corpus, structure, relations)), strict=True))


def _review_summary(matrix, reviews, bindings, matrix_sha256, documents, *, created_at):
    document_by_id = {doc["document_id"]: doc for doc in documents}
    document_ids = set(document_by_id)
    if (type(matrix) is not dict or matrix.get("schema_version") != MATRIX_SCHEMA
            or matrix.get("effective_dates_status") != "unknown"
            or any(matrix.get(key) != value for key, value in bindings.items())):
        raise ValueError("RELEASE_MATRIX_BINDING_INVALID")
    scopes, checks = matrix.get("scopes"), matrix.get("required_checks")
    if (type(scopes) is not list or not scopes or type(checks) is not list or not checks
            or len(scopes) > 200 or len(checks) > 2000):
        raise ValueError("RELEASE_MATRIX_COVERAGE_INVALID")
    scope_by_id, check_by_id = {}, {}
    for scope in scopes:
        if type(scope) is not dict:
            raise ValueError("RELEASE_MATRIX_SCOPE_INVALID")
        key = _text(scope.get("scope_id"), "SCOPE_ID", 128)
        if key in scope_by_id:
            raise ValueError("RELEASE_MATRIX_DUPLICATE_SCOPE")
        _text(scope.get("support_status"), "SUPPORT_STATUS", 128)
        _text(scope.get("answer_boundary"), "ANSWER_BOUNDARY")
        docs, required = scope.get("document_ids"), scope.get("critical_check_ids")
        if (type(docs) is not list or not docs or any(type(d) is not str for d in docs)
                or len(set(docs)) != len(docs) or not set(docs) <= document_ids
                or type(required) is not list or not required
                or any(type(c) is not str for c in required) or len(set(required)) != len(required)):
            raise ValueError("RELEASE_MATRIX_SCOPE_COVERAGE_INVALID")
        scope_by_id[key] = scope
    for check in checks:
        if type(check) is not dict:
            raise ValueError("RELEASE_MATRIX_CHECK_INVALID")
        key = _text(check.get("check_id"), "CHECK_ID", 128)
        scope_id = check.get("scope_id")
        if key in check_by_id or type(scope_id) is not str or scope_id not in scope_by_id:
            raise ValueError("RELEASE_MATRIX_CHECK_SCOPE_INVALID")
        _text(check.get("kind"), "CHECK_KIND", 128)
        _text(check.get("description"), "CHECK_DESCRIPTION")
        docs = check.get("document_ids")
        if (type(docs) is not list or not docs or any(type(d) is not str for d in docs)
                or len(set(docs)) != len(docs)
                or not set(docs) <= set(scope_by_id[scope_id]["document_ids"])
                or key not in scope_by_id[scope_id]["critical_check_ids"]):
            raise ValueError("RELEASE_MATRIX_CHECK_DOCUMENT_INVALID")
        refs = check.get("evidence_refs")
        if type(refs) is not list or not refs:
            raise ValueError("RELEASE_MATRIX_SOURCE_REFS_REQUIRED")
        referenced_docs = set()
        for ref in refs:
            if (type(ref) is not dict or set(ref) != {"document_id", "chunk_id", "parent_sha256", "start", "end", "excerpt_sha256"}
                    or type(ref["document_id"]) is not str or ref["document_id"] not in docs
                    or type(ref["chunk_id"]) is not str):
                raise ValueError("RELEASE_MATRIX_SOURCE_REF_INVALID")
            document = document_by_id[ref["document_id"]]
            parent = next((c for c in document["chunks"] if c["chunk_id"] == ref["chunk_id"]), None)
            start, end = ref["start"], ref["end"]
            if (parent is None or parent["sha256"] != ref["parent_sha256"]
                    or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(parent["content"])
                    or _sha(parent["content"][start:end].encode("utf-8")) != ref["excerpt_sha256"]):
                raise ValueError("RELEASE_MATRIX_SOURCE_REF_MISMATCH")
            referenced_docs.add(ref["document_id"])
        if referenced_docs != set(docs):
            raise ValueError("RELEASE_MATRIX_SOURCE_REF_COVERAGE_INVALID")
        check_by_id[key] = check
    for scope in scopes:
        if set(scope["critical_check_ids"]) != {k for k, c in check_by_id.items() if c["scope_id"] == scope["scope_id"]}:
            raise ValueError("RELEASE_MATRIX_CHECK_COVERAGE_INVALID")
    if "source_inventory" in matrix:
        inventory = matrix["source_inventory"]
        if type(inventory) is not list or len(inventory) != len(document_by_id):
            raise ValueError("RELEASE_MATRIX_SOURCE_INVENTORY_INVALID")
        seen_documents = set()
        for row in inventory:
            if (type(row) is not dict or type(row.get("document_id")) is not str
                    or row["document_id"] not in document_by_id or row["document_id"] in seen_documents):
                raise ValueError("RELEASE_MATRIX_SOURCE_INVENTORY_INVALID")
            seen_documents.add(row["document_id"])
            document = document_by_id[row["document_id"]]
            if any(row.get(field) != document[field] for field in (
                    "title", "url", "fetched_at", "raw_sha256", "text_sha256", "effective_from", "effective_to")):
                raise ValueError("RELEASE_MATRIX_SOURCE_INVENTORY_MISMATCH")
    if (type(reviews) is not dict or reviews.get("schema_version") != REVIEWS_SCHEMA
            or reviews.get("matrix_sha256") != matrix_sha256
            or any(reviews.get(key) != value for key, value in bindings.items())
            or type(reviews.get("records")) is not list or len(reviews["records"]) > 20000):
        raise ValueError("RELEASE_REVIEW_BINDING_INVALID")
    latest, seen, human_records, ai_records = {}, set(), 0, 0
    for record in reviews["records"]:
        if type(record) is not dict or type(record.get("check_id")) is not str or record["check_id"] not in check_by_id:
            raise ValueError("RELEASE_REVIEW_CHECK_INVALID")
        kind, decision = record.get("reviewer_type"), record.get("decision")
        if (type(kind) is not str or type(decision) is not str
                or kind not in {"ai", "human"} or decision not in {"accepted", "rejected", "needs_review"}):
            raise ValueError("RELEASE_REVIEW_TYPE_INVALID")
        _text(record.get("reviewer_id"), "REVIEWER", 200)
        _text(record.get("notes"), "REVIEW_NOTES", 10000)
        timestamp = _timestamp(record.get("reviewed_at"))
        instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if instant > datetime.fromisoformat(created_at.replace("Z", "+00:00")):
            raise ValueError("RELEASE_REVIEW_AFTER_CREATION")
        identity = (record["check_id"], instant)
        if identity in seen:
            raise ValueError("RELEASE_REVIEW_AMBIGUOUS_ORDER")
        seen.add(identity)
        if kind == "human":
            human_records += 1
            if decision == "accepted" and (record.get("human_attestation") is not True
                    or record.get("review_method") != "manual_original_source_comparison"):
                raise ValueError("RELEASE_HUMAN_ATTESTATION_REQUIRED")
        else:
            ai_records += 1
            if record.get("human_attestation") not in (None, False):
                raise ValueError("RELEASE_AI_CANNOT_ATTEST_HUMAN")
        prior = latest.get(record["check_id"])
        if prior is None or instant > prior[0]:
            latest[record["check_id"]] = (instant, record)
    accepted = sorted(key for key, (_, row) in latest.items()
                      if row["reviewer_type"] == "human" and row["decision"] == "accepted")
    return {"required_checks": len(checks), "human_accepted_checks": len(accepted),
            "human_accepted_check_ids": accepted,
            "pending_check_ids": sorted(set(check_by_id) - set(accepted)),
            "human_records": human_records, "ai_records": ai_records,
            "reviewed_channel_eligible": len(accepted) == len(checks),
            "reviewer_identity_verified": False,
            "review_authenticity": "local_reviewer_self_declaration_not_identity_authentication",
            "effective_dates_verified": False, "current_policy_verified": False}


def _corpus_files(corpus_path):
    payload = _json(_read_file(corpus_path))
    result = {"corpus/corpus.json": corpus_path}
    for doc in payload["documents"]:
        paths = [doc["raw_path"], doc["text_path"]]
        for item in doc.get("image_transcriptions", []):
            paths.extend(item[f"{name}_path"] for name in ("asset", "transcription", "review"))
        for relative in paths:
            key = "corpus/" + _relative(relative)
            source = _file(corpus_path.parent, relative)
            if key in result and result[key] != source:
                raise ValueError("RELEASE_ASSET_PATH_COLLISION")
            result[key] = source
    return result


def build_release(store_dir, corpus_path, structure_path, relations_path,
                  support_matrix_path, reviews_path, *, label, created_at=None):
    """Copy only validated referenced files into a new content-addressed release."""
    _text(label, "LABEL", 200)
    timestamp = _timestamp(created_at or _now())
    inputs = [Path(p).absolute() for p in (corpus_path, structure_path, relations_path,
                                         support_matrix_path, reviews_path)]
    corpus, structure, relations, matrix_path, reviews_path = inputs
    bundle = PolicyEvidenceBundles(corpus, structure, relations)
    bindings = _bindings(corpus, structure, relations)
    matrix_raw, reviews_raw = _read_file(matrix_path), _read_file(reviews_path)
    matrix, reviews = _json(matrix_raw), _json(reviews_raw)
    documents = bundle.index.source_index.documents
    summary = _review_summary(matrix, reviews, bindings, _sha(matrix_raw), documents, created_at=timestamp)
    sources = _corpus_files(corpus)
    sources.update({"structure.json": structure, "relations.json": relations,
                    "support-matrix.json": matrix_path, "reviews.json": reviews_path})
    raw_files = {name: _read_file(path) for name, path in sources.items()}
    if any(_sha(raw_files[name]) != bindings[key] for name, key in zip(
            ("corpus/corpus.json", "structure.json", "relations.json"), _BINDINGS, strict=True)):
        raise ValueError("RELEASE_SOURCE_CHANGED")
    if raw_files["support-matrix.json"] != matrix_raw or raw_files["reviews.json"] != reviews_raw:
        raise ValueError("RELEASE_REVIEW_SOURCE_CHANGED")
    assets = {name: {"path": name, "sha256": _sha(raw), "bytes": len(raw)}
              for name, raw in sorted(raw_files.items())}
    manifest = {"schema_version": SCHEMA, "label": label, "created_at": timestamp,
                "scope": bundle.index.source_index.scope, "captured_at": bundle.index.source_index.captured_at,
                "effective_from": None, "effective_to": None, "review_summary": summary,
                "bindings": bindings, "assets": assets,
                "entrypoints": dict(zip(_ROLES, ("corpus/corpus.json", "structure.json", "relations.json",
                                                  "support-matrix.json", "reviews.json"), strict=True)),
                "source_truth": "local_hash_consistency_not_publisher_authentication",
                "index_policy": "lexical_in_memory_no_vector_index_release",
                "read_only_reference": True}
    raw_manifest = _bytes(manifest)
    release_id = _sha(raw_manifest)
    store = _directory(store_dir, create=True)
    releases = _directory(store / "releases", create=True)
    with _lock(store):
        destination = releases / release_id
        if destination.exists():
            raise ValueError("RELEASE_ALREADY_EXISTS")
        staging = releases / (".building-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            for name, raw in raw_files.items():
                _write_new(staging.joinpath(*PurePosixPath(name).parts), raw)
            _write_new(staging / "manifest.json", raw_manifest)
            _validate_directory(staging, release_id)
            # A source mutation during copying is refused; no partial release is published.
            if any(_read_file(path) != raw_files[name] for name, path in sources.items()):
                raise ValueError("RELEASE_SOURCE_CHANGED")
            staging.rename(destination)
        finally:
            if staging.exists():
                # Generated, resolved staging path is checked before recursive cleanup.
                if staging.resolve().parent != releases.resolve() or not staging.name.startswith(".building-"):
                    raise ValueError("RELEASE_STAGING_PATH_INVALID")
                shutil.rmtree(staging)
    return {"release_id": release_id, "manifest_sha256": release_id,
            "manifest_path": str(destination / "manifest.json"), "asset_count": len(assets),
            "review_summary": deepcopy(summary), "label": label}


def _validate_directory(directory, release_id, *, include_bundles=False):
    _digest(release_id)
    _ordinary_path(directory, file=False)
    raw = _read_file(directory / "manifest.json")
    if _sha(raw) != release_id:
        raise ValueError("RELEASE_MANIFEST_HASH_MISMATCH")
    manifest = _json(raw)
    expected = {"schema_version", "label", "created_at", "scope", "captured_at", "effective_from",
                "effective_to", "review_summary", "bindings", "assets", "entrypoints", "source_truth",
                "index_policy", "read_only_reference"}
    if (type(manifest) is not dict or set(manifest) != expected or manifest["schema_version"] != SCHEMA
            or manifest["read_only_reference"] is not True
            or manifest["effective_from"] is not None or manifest["effective_to"] is not None
            or manifest["source_truth"] != "local_hash_consistency_not_publisher_authentication"
            or manifest["index_policy"] != "lexical_in_memory_no_vector_index_release"):
        raise ValueError("RELEASE_MANIFEST_INVALID")
    _text(manifest["label"], "LABEL", 200)
    _timestamp(manifest["created_at"])
    _timestamp(manifest["captured_at"])
    entrypoints, assets = manifest["entrypoints"], manifest["assets"]
    if (type(entrypoints) is not dict or set(entrypoints) != set(_ROLES)
            or type(assets) is not dict or not assets or len(assets) > 20000):
        raise ValueError("RELEASE_ASSETS_INVALID")
    paths = {}
    for name, asset in assets.items():
        _relative(name)
        if (type(asset) is not dict or set(asset) != {"path", "sha256", "bytes"}
                or asset["path"] != name or type(asset["bytes"]) is not int or asset["bytes"] <= 0):
            raise ValueError("RELEASE_ASSET_INVALID")
        path = _file(directory, name)
        raw_asset = _read_file(path)
        if _sha(raw_asset) != _digest(asset["sha256"]) or len(raw_asset) != asset["bytes"]:
            raise ValueError("RELEASE_ASSET_HASH_MISMATCH")
        paths[name] = path
    if any(type(name) is not str or name not in paths for name in entrypoints.values()):
        raise ValueError("RELEASE_ENTRYPOINT_INVALID")
    corpus, structure, relations, matrix_path, reviews_path = [paths[entrypoints[role]] for role in _ROLES]
    bundle = PolicyEvidenceBundles(corpus, structure, relations)
    bindings = _bindings(corpus, structure, relations)
    expected_paths = {p.relative_to(directory).as_posix() for p in _corpus_files(corpus).values()}
    expected_paths.update(entrypoints.values())
    if set(assets) != expected_paths:
        raise ValueError("RELEASE_ASSET_INVENTORY_MISMATCH")
    summary = _review_summary(_json(_read_file(matrix_path)), _json(_read_file(reviews_path)), bindings,
                              _sha(_read_file(matrix_path)), bundle.index.source_index.documents,
                              created_at=manifest["created_at"])
    if (manifest["bindings"] != bindings or manifest["review_summary"] != summary
            or manifest["scope"] != bundle.index.source_index.scope
            or manifest["captured_at"] != bundle.index.source_index.captured_at):
        raise ValueError("RELEASE_DERIVED_METADATA_MISMATCH")
    return (manifest, bundle) if include_bundles else manifest


def validate_release(store_dir, release_id):
    """Revalidate all bytes, exact spans, cross-file bindings, and review gates."""
    _digest(release_id)
    store = _directory(store_dir)
    return deepcopy(_validate_directory(store / "releases" / release_id, release_id))


def _channel(channel):
    if type(channel) is not str or channel not in CHANNELS:
        raise ValueError("RELEASE_CHANNEL_INVALID")
    return channel


def _read_pointer(store, channel, *, required=True):
    path = store / f"active-{_channel(channel)}.json"
    if not path.exists() and not path.is_symlink():
        if required:
            raise ValueError("RELEASE_NO_ACTIVE_VERSION")
        return None
    pointer = _json(_read_file(path))
    if (type(pointer) is not dict or set(pointer) != {"schema_version", "channel", "generation", "release_id",
                                                    "manifest_sha256", "event_sha256"}
            or pointer["schema_version"] != "knowledge-active-v1" or pointer["channel"] != channel
            or type(pointer["generation"]) is not int or pointer["generation"] < 1
            or pointer["release_id"] != pointer["manifest_sha256"]):
        raise ValueError("RELEASE_ACTIVE_POINTER_INVALID")
    _digest(pointer["release_id"])
    _digest(pointer["event_sha256"])
    events = _history(store, channel, pointer)
    if events[0]["release_id"] != pointer["release_id"]:
        raise ValueError("RELEASE_ACTIVE_HISTORY_MISMATCH")
    return pointer


def _history(store, channel, pointer):
    events, digest, generation = [], pointer["event_sha256"], pointer["generation"]
    while digest is not None:
        _digest(digest)
        raw = _read_file(store / "history" / channel / f"{digest}.json")
        if _sha(raw) != digest:
            raise ValueError("RELEASE_HISTORY_HASH_MISMATCH")
        event = _json(raw)
        if (type(event) is not dict or set(event) != {"schema_version", "channel", "generation", "release_id",
                "previous_event_sha256", "previous_release_id", "operation", "reason", "created_at"}
                or event["schema_version"] != "knowledge-activation-event-v1" or event["channel"] != channel
                or type(event["generation"]) is not int or event["generation"] != generation
                or event["operation"] not in {"activate", "rollback"}):
            raise ValueError("RELEASE_HISTORY_INVALID")
        _digest(event["release_id"])
        _text(event["reason"], "REASON", 2000)
        _timestamp(event["created_at"])
        if events and events[-1]["previous_release_id"] != event["release_id"]:
            raise ValueError("RELEASE_HISTORY_CHAIN_MISMATCH")
        events.append(event)
        digest, generation = event["previous_event_sha256"], generation - 1
        if generation < 0 or len(events) > 10000:
            raise ValueError("RELEASE_HISTORY_LIMIT")
    if generation != 0 or events[-1]["previous_release_id"] is not None:
        raise ValueError("RELEASE_HISTORY_INCOMPLETE")
    return events


def release_history(store_dir, *, channel="development"):
    store = _directory(store_dir)
    pointer = _read_pointer(store, channel, required=False)
    return deepcopy(_history(store, channel, pointer)) if pointer else []


def _switch(store_dir, release_id, *, expected_generation, channel, reason, operation):
    _channel(channel)
    _digest(release_id)
    _text(reason, "REASON", 2000)
    if type(expected_generation) is not int or expected_generation < 0:
        raise ValueError("RELEASE_EXPECTED_GENERATION_INVALID")
    store = _directory(store_dir)
    with _lock(store):
        previous = _read_pointer(store, channel, required=False)
        generation = previous["generation"] if previous else 0
        if generation != expected_generation:
            raise ValueError("RELEASE_CONCURRENT_UPDATE: expected_generation does not match")
        manifest = validate_release(store, release_id)
        if channel == "reviewed" and not manifest["review_summary"]["reviewed_channel_eligible"]:
            raise ValueError("RELEASE_HUMAN_REVIEW_REQUIRED")
        if previous and previous["release_id"] == release_id:
            raise ValueError("RELEASE_ALREADY_ACTIVE")
        if operation == "rollback" and (not previous or release_id not in {
                event["release_id"] for event in _history(store, channel, previous)}):
            raise ValueError("RELEASE_ROLLBACK_REQUIRES_PRIOR_ACTIVATION")
        event = {"schema_version": "knowledge-activation-event-v1", "channel": channel,
                 "generation": generation + 1, "release_id": release_id,
                 "previous_event_sha256": previous["event_sha256"] if previous else None,
                 "previous_release_id": previous["release_id"] if previous else None,
                 "operation": operation, "reason": reason, "created_at": _now()}
        event_raw = _bytes(event)
        event_sha = _sha(event_raw)
        _write_new(store / "history" / channel / f"{event_sha}.json", event_raw)
        pointer = {"schema_version": "knowledge-active-v1", "channel": channel,
                   "generation": generation + 1, "release_id": release_id,
                   "manifest_sha256": release_id, "event_sha256": event_sha}
        temporary = store / (f".active-{channel}-{uuid.uuid4().hex}.tmp")
        try:
            _write_new(temporary, _bytes(pointer))
            os.replace(temporary, store / f"active-{channel}.json")
        finally:
            if temporary.exists():
                temporary.unlink()
    return deepcopy(pointer)


def activate_release(store_dir, release_id, *, expected_generation, channel="development", reason):
    return _switch(store_dir, release_id, expected_generation=expected_generation,
                   channel=channel, reason=reason, operation="activate")


def resolve_active_release(store_dir, *, channel="development", include_bundles=False):
    """Read one atomic pointer, validate its immutable bundle, and pin its paths.

    With ``include_bundles`` it also returns what that one validation built,
    the (resolved, manifest, bundles) of resolve_release_with_bundles, so a
    read-only consumer can start from it instead of validating again.
    """
    store = _directory(store_dir)
    pointer = _read_pointer(store, channel)
    validated = resolve_release_with_bundles(store, pointer["release_id"])
    resolved = validated[0]
    if channel == "reviewed" and not resolved["review_summary"]["reviewed_channel_eligible"]:
        raise ValueError("RELEASE_HUMAN_REVIEW_REQUIRED")
    active = {**resolved, **pointer}
    return (active, validated) if include_bundles else active


def resolve_release_with_bundles(store_dir, release_id):
    """Validate once and retain the parsed bundle for a read-only consumer.

    Resolves a pinned release without consulting active state. The returned
    bundle is only read: a graph snapshot keeps it for route lookups and holds
    no session data in it. validate_release keeps its original return contract.
    """
    store = _directory(store_dir)
    manifest, bundles = _validate_directory(store / "releases" / release_id, release_id,
                                            include_bundles=True)
    return _resolved_release(store, release_id, manifest), manifest, bundles


def _resolved_release(store, release_id, manifest):
    directory = store / "releases" / release_id
    return {"release_id": release_id, "manifest_sha256": release_id,
            **{f"{role}_path": str(directory / name) for role, name in manifest["entrypoints"].items()},
            "label": manifest["label"], "captured_at": manifest["captured_at"],
            "effective_from": None, "effective_to": None, "scope": deepcopy(manifest["scope"]),
            "review_summary": deepcopy(manifest["review_summary"]), "bindings": deepcopy(manifest["bindings"]),
            "read_only_reference": True, "pinned_at_resolution": True}
