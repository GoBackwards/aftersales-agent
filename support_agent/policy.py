"""Read-only policy knowledge for the graph runtime: one pinned release and the card catalogue bound to it.

B6 (first-principles plan K2, §7.3, §8.6): no retrieval. A turn binds the whole catalogue, every card checked against
the release's own source text and the catalogue under the publication cap (D15); the answer context carries its
compact model view (catalog.model_view) and the answer chooses card outcomes by id. What stays here: the release
binding and snapshot cache, the catalogue check, the source drawer of the cards a reply shows, and the directed route
table lookup (D10). This adapter makes no model or network calls and decides nothing about an order.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import RLock
from types import MappingProxyType

from .public_policy import _digest, _read_file, _sha256, _string
from .public_policy_bundles import PolicyEvidenceBundles, TOPICS
from .catalog import (DEFAULT_SOURCE_KIND, MAX_ALIAS_CHARS, MAX_ALIASES, SOURCE_KINDS, alias_problems,
                          cap_problem, publisher_problems)
from .kb_sources import supplementary_evidence


LOADER_VERSION = "graph-policy-knowledge-v1"
_KNOWLEDGE_CACHE = OrderedDict()
_KNOWLEDGE_LOCK = RLock()
_MAX_CACHED_RELEASES = 8


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class PublicPolicyConfig:
    corpus_path: str | Path
    structure_path: str | Path
    relations_path: str | Path
    knowledge_store: str | Path | None = None
    knowledge_release: dict | None = None

    def __post_init__(self):
        if self.knowledge_release is None:
            if self.knowledge_store is not None:
                raise ValueError('知识发布绑定不完整。')
            return
        bound = self.knowledge_release
        required = {'release_id', 'manifest_sha256', 'generation', 'channel'}
        if (not isinstance(bound, Mapping) or not required <= bound.keys()
                or not set(bound) <= required | {'review_status', 'effective_dates'}
                or type(bound['channel']) is not str or bound['channel'] not in {'development', 'reviewed'}
                or type(bound['generation']) is not int or bound['generation'] < 1
                or self.knowledge_store is None):
            raise ValueError('知识发布元数据无效。')
        expected = 'human_attestations_recorded' if bound['channel'] == 'reviewed' else 'development_candidate'
        if (bound.get('review_status', expected) != expected
                or bound.get('effective_dates', 'unverified_snapshot') != 'unverified_snapshot'):
            raise ValueError('知识审核或生效状态声明无效。')
        object.__setattr__(self, 'knowledge_release', MappingProxyType({**bound,
            'review_status': expected, 'effective_dates': 'unverified_snapshot'}))

    def release_metadata(self):
        return dict(self.knowledge_release) if self.knowledge_release is not None else None


def _configuration_key(config):
    if config is None or any(not isinstance(getattr(config, key, None), (str, Path))
                            or not str(getattr(config, key)).strip()
                            for key in ("corpus_path", "structure_path", "relations_path")):
        raise ValueError("公开政策来源配置不完整。")
    paths = tuple(str(Path(getattr(config, key)).resolve())
                  for key in ("corpus_path", "structure_path", "relations_path"))
    metadata = config.release_metadata()
    return (LOADER_VERSION, paths,
            str(Path(config.knowledge_store).resolve()) if config.knowledge_store else None,
            tuple(sorted(metadata.items())) if metadata else None)


@dataclass(frozen=True)
class _SourceSnapshot:
    """Only immutable source data; evidence results are detached copies."""
    corpus_sha256: str
    captured_at: str
    scope: Mapping
    sources: Mapping

    def evidence_by_id(self, identifier):
        return _thaw(self.sources[identifier])


@dataclass(frozen=True)
class PolicyKnowledge:
    """Process-shared knowledge only: no questions, sessions or model state.

    ``evidence`` holds every whole source passage and every authored relation rule of the release (the ids cards
    cite). ``bundles`` are the PolicyEvidenceBundles this snapshot was built from, kept for route lookups; their
    methods only read and return detached copies.
    """
    configuration_key: tuple
    index: _SourceSnapshot
    evidence: Mapping
    sources: Mapping
    metadata: Mapping
    resolved: Mapping | None
    manifest: Mapping | None
    bundles: PolicyEvidenceBundles = field(compare=False, repr=False)


def _validate_binding(config, resolved):
    from .knowledge_release import release_history
    bound = config.release_metadata()
    if resolved["manifest_sha256"] != bound["manifest_sha256"]:
        raise ValueError("知识发布 manifest 与启动时绑定不一致。")
    if bound["channel"] == "reviewed" and not resolved["review_summary"]["reviewed_channel_eligible"]:
        raise ValueError("知识发布缺少完整人工审核。")
    if not any(event["generation"] == bound["generation"] and event["release_id"] == bound["release_id"]
               for event in release_history(config.knowledge_store, channel=bound["channel"])):
        raise ValueError("知识发布代际没有对应的激活记录。")
    for key in ("corpus_path", "structure_path", "relations_path"):
        if Path(getattr(config, key)).resolve() != Path(resolved[key]).resolve():
            raise ValueError("知识资产与绑定版本不一致。")


def _verify_cached_bytes(config, manifest):
    """Recheck disk bytes, without reparsing sources or rebuilding indexes.

    Queries use the retained snapshot. This check does not detect a mutation
    restored between checks; frozen experiment validators remain independent.
    """
    from .knowledge_release import _directory, _file
    from .public_policy import _read_file
    release_id = config.knowledge_release["release_id"]
    directory = _directory(Path(config.knowledge_store) / "releases" / release_id)
    if _sha256(_read_file(_file(directory, "manifest.json"))) != release_id:
        raise ValueError("RELEASE_MANIFEST_HASH_MISMATCH")
    for name, asset in manifest["assets"].items():
        raw = _read_file(_file(directory, name))
        if len(raw) != asset["bytes"] or _sha256(raw) != asset["sha256"]:
            raise ValueError("RELEASE_ASSET_HASH_MISMATCH")


def load_policy_knowledge(config, *, validated=None):
    """Validate/build one pinned release, then reuse an immutable snapshot.

    The lock covers first construction, so concurrent workers cannot publish
    partial or duplicate objects. Cache hits still verify binding and bytes.
    Unpinned development files deliberately do not share this process cache.
    ``validated`` is resolve_release_with_bundles' result for this release from
    a full validation the caller has just run; a first construction then builds
    from it instead of validating the release a second time.
    """
    if not isinstance(config, PublicPolicyConfig):
        raise ValueError("公开政策配置类型无效。")
    key = _configuration_key(config)
    if config.knowledge_release is None:
        bundles = PolicyEvidenceBundles(config.corpus_path, config.structure_path, config.relations_path)
        return PolicyAdapter._build_knowledge(config, bundles, key)
    with _KNOWLEDGE_LOCK:
        cached = _KNOWLEDGE_CACHE.get(key)
        if cached is not None:
            _validate_binding(config, cached.resolved)
            _verify_cached_bytes(config, cached.manifest)
            _KNOWLEDGE_CACHE.move_to_end(key)
            return cached
        if validated is None:
            from .knowledge_release import resolve_release_with_bundles
            validated = resolve_release_with_bundles(config.knowledge_store, config.knowledge_release["release_id"])
        resolved, manifest, bundles = validated
        _validate_binding(config, resolved)
        snapshot = PolicyAdapter._build_knowledge(config, bundles, key, resolved, manifest)
        _verify_cached_bytes(config, manifest)
        _KNOWLEDGE_CACHE[key] = snapshot
        while len(_KNOWLEDGE_CACHE) > _MAX_CACHED_RELEASES:
            _KNOWLEDGE_CACHE.popitem(last=False)
        return snapshot


def resolve_policy_config(store_dir, *, channel="development"):
    """Pin one active pointer and warm the shared graph snapshot exactly once."""
    from .knowledge_release import _directory, _file, _read_pointer
    from .public_policy import _read_file
    from .public_policy_structure import _json
    store = _directory(store_dir)
    pointer = _read_pointer(store, channel)
    directory = _directory(store / "releases" / pointer["release_id"])
    raw = _read_file(_file(directory, "manifest.json"))
    if _sha256(raw) != pointer["release_id"]:
        raise ValueError("RELEASE_MANIFEST_HASH_MISMATCH")
    manifest = _json(raw)
    if type(manifest) is not dict:
        raise ValueError("RELEASE_MANIFEST_INVALID")
    entries = manifest.get("entrypoints", {})
    if not isinstance(entries, dict) or not {"corpus", "structure", "relations"} <= entries.keys():
        raise ValueError("RELEASE_ENTRYPOINT_INVALID")
    config = PublicPolicyConfig(*(_file(directory, entries[role]) for role in ("corpus", "structure", "relations")),
        knowledge_store=store, knowledge_release={key: pointer[key]
            for key in ("release_id", "manifest_sha256", "generation", "channel")})
    load_policy_knowledge(config)
    return config


class PolicyAdapter:
    """One pinned release and, with ``card_catalog``, the catalogue checked against it (B6: no retrieval).

    ``config`` uses the existing PublicPolicyConfig data interface; ``knowledge`` is its shared snapshot.
    """

    def __init__(self, config=None, *, knowledge=None, card_catalog=None):
        self.config = config
        self._evidence, self._sources = {}, {}
        self._index = None
        self._metadata = {}
        self._cards = ()
        if config is None and knowledge is None:
            return
        if knowledge is None:
            knowledge = load_policy_knowledge(config)
        if not isinstance(knowledge, PolicyKnowledge) or knowledge.configuration_key != _configuration_key(config):
            raise ValueError("GRAPH_POLICY_KNOWLEDGE_BINDING_INVALID")
        self.knowledge = knowledge
        self._index, self._metadata = knowledge.index, knowledge.metadata
        self._evidence, self._sources = knowledge.evidence, knowledge.sources
        if card_catalog is not None:
            self._enable_card_catalog(card_catalog)

    def _enable_card_catalog(self, path):
        """Validate authored cards against retained source bytes, per view; refuse a catalogue over the cap (D15).

        Catalog data never enters the raw-release cache. Different-corpus
        unpinned fixtures explicitly fall back; pinned release mismatches fail.
        """
        raw = _read_file(Path(path).absolute())
        try:
            catalog = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            raise ValueError("GRAPH_CARD_CATALOG_INVALID") from None
        if (type(catalog) is not dict or catalog.get("schema_version") != "graph-policy-cards-v1"
                or catalog.get("human_reviewed") is not False
                or type(catalog.get("cards")) is not list or not 1 <= len(catalog["cards"]) <= 1000):
            raise ValueError("GRAPH_CARD_CATALOG_INVALID")
        _digest(catalog.get("corpus_sha256"))
        _digest(catalog.get("knowledge_release_id"))
        metadata = {"status": "enabled", "sha256": _sha256(raw),
            "schema_version": catalog["schema_version"], "card_count": len(catalog["cards"]),
            "knowledge_release_id": catalog["knowledge_release_id"],
            "coverage": "authored_card_groups_not_all_policy_or_semantic_coverage",
            "human_reviewed": False}
        pinned = self._metadata.get("knowledge_release")
        if catalog["corpus_sha256"] != self._metadata["corpus_sha256"]:
            if pinned is not None:
                raise ValueError("GRAPH_CARD_CORPUS_BINDING_MISMATCH")
            metadata.update(status="disabled_fixture_corpus_mismatch", card_count=0)
            self._metadata = _freeze({**self._metadata, "policy_card_catalog": metadata,
                                      "card_catalog_sha256": metadata["sha256"]})
            return
        if pinned is not None and catalog["knowledge_release_id"] != pinned["release_id"]:
            raise ValueError("GRAPH_CARD_RELEASE_BINDING_MISMATCH")
        # Knowledge-base uploads travel inside the versioned catalog. They join
        # this view's evidence only; the shared official snapshot is unchanged.
        evidence, sources = dict(self._evidence), dict(self._sources)
        uploaded = supplementary_evidence(catalog.get("supplementary_sources", []))
        for identifier, (item, source) in uploaded.items():
            if identifier in evidence:
                raise ValueError("GRAPH_SUPPLEMENTARY_EVIDENCE_ID_CONFLICT")
            evidence[identifier], sources[identifier] = _freeze(item), _freeze(source)
        if uploaded:
            metadata["supplementary_source_count"] = len(catalog["supplementary_sources"])
            metadata["supplementary_evidence_count"] = len(uploaded)
        seen = set()
        for card in catalog["cards"]:
            if type(card) is not dict:
                raise ValueError("GRAPH_CARD_CATALOG_INVALID")
            for name in ("id", "title", "scope"):
                _string(card.get(name), limit=4000)
            if card["id"] in seen:
                raise ValueError("GRAPH_CARD_DUPLICATE_ID")
            seen.add(card["id"])
            for name in ("conditions", "outcomes", "exceptions", "evidence_ids", "support_scope_ids"):
                values = card.get(name)
                if type(values) is not list or (name != "exceptions" and not values):
                    raise ValueError("GRAPH_CARD_CATALOG_INVALID")
                for value in values:
                    _string(value, limit=10000)
            premises = card.get("outcome_premises")
            if premises is not None and (type(premises) is not list or len(premises) != len(card["outcomes"])
                    or any(type(rows) is not list or not rows
                           or any(type(part) is not str or not part.strip() for part in rows) for rows in premises)):
                raise ValueError("GRAPH_CARD_OUTCOME_PREMISES_INVALID")
            # B5d1: the source kind and aliases (D16: no law-source card is accepted).
            if card.get("source_kind", DEFAULT_SOURCE_KIND) not in SOURCE_KINDS:
                raise ValueError("GRAPH_CARD_SOURCE_KIND_INVALID")
            aliases = card.get("aliases", [])
            if type(aliases) is not list or len(aliases) > MAX_ALIASES:
                raise ValueError("GRAPH_CARD_CATALOG_INVALID")
            for alias in aliases:
                _string(alias, limit=MAX_ALIAS_CHARS)
            identifiers, references = card["evidence_ids"], card.get("references")
            if (len(identifiers) != len(set(identifiers)) or type(references) is not list
                    or len(references) != len(identifiers)
                    or any(type(ref) is not dict or type(ref.get("evidence_id")) is not str for ref in references)
                    or {ref.get("evidence_id") for ref in references} != set(identifiers)):
                raise ValueError("GRAPH_CARD_REFERENCE_SET_INVALID")
            for ref in references:
                item = evidence.get(ref["evidence_id"])
                if item is None:
                    raise ValueError("GRAPH_CARD_EVIDENCE_MISSING")
                if item["sha256"] != ref.get("parent_sha256"):
                    raise ValueError("GRAPH_CARD_PARENT_HASH_MISMATCH")
                quotes = ref.get("quotes")
                if (type(quotes) is not list or not quotes
                        or any(type(q) is not str or not q or q not in item["text"] for q in quotes)):
                    raise ValueError("GRAPH_CARD_QUOTE_BINDING_MISMATCH")
            if alias_problems(card):   # the editor's rule, so a card edited on disk cannot slip past it
                raise ValueError("GRAPH_CARD_ALIAS_INVALID")
        if publisher_problems(catalog):   # B5f (D8): G10's lexicon, word for word in the source quotes
            raise ValueError("GRAPH_CARD_PUBLISHERS_INVALID")
        over = cap_problem(catalog["cards"])   # D15: the whole model view must fit the answer context (B6)
        if over:
            raise ValueError("GRAPH_CARD_CATALOG_OVER_CAP：" + over)
        if uploaded:
            self._evidence, self._sources = MappingProxyType(evidence), MappingProxyType(sources)
        self._cards = _freeze(catalog["cards"])
        self._metadata = _freeze({**self._metadata, "policy_card_catalog": metadata,
                                  "card_catalog_sha256": metadata["sha256"]})

    def catalogue_cards(self):
        """The bound catalogue's cards, whole and in catalogue order, as detached copies (B6: the answer context
        carries their model view and the answer may cite any of them); [] without a release, without a catalogue,
        or for an unpinned fixture corpus the catalogue is not bound to."""
        return [_thaw(card) for card in self._cards]

    @classmethod
    def _build_knowledge(cls, config, bundles, key, resolved=None, manifest=None):
        self = cls.__new__(cls)
        self._evidence, self._sources = {}, {}
        self._index = bundles.index.source_index
        self._metadata = {
            "corpus_sha256": self._index.corpus_sha256,
            "structure_sha256": bundles.index.structure_sha256,
            "relations_sha256": bundles.relations_sha256,
            "knowledge_release": config.release_metadata(),
            "captured_at": self._index.captured_at,
            "scope": self._index.scope,
        }
        for document in self._index.documents:
            for parent in document["chunks"]:
                citation = f"{document['document_id']}@{document['text_sha256'][:12]}#{parent['chunk_id']}"
                source = self._index.evidence_by_id(citation)
                spans = [{"start": 0, "end": len(parent["content"]), "text": parent["content"],
                          "document_start": parent["start"], "document_end": parent["end"],
                          "sha256": parent["sha256"]}]
                self._add_evidence("parent:" + citation, source, spans)
        for topic in sorted(TOPICS):
            # This existing loader verifies corpus, parents, spans and hashes; the authored relation rules are the
            # evidence some cards cite (rule:<id>).
            bundle = bundles.build([topic], max_context_chars=20000)
            for item in bundle["backend_evidence"]:
                self._add_evidence("rule:" + item["rule_id"], item["source"], item["spans"], rule_id=item["rule_id"])
        index = _SourceSnapshot(self._index.corpus_sha256, self._index.captured_at,
            _freeze(self._index.scope), _freeze({source["citation_id"]: source for source in self._sources.values()}))
        return PolicyKnowledge(key, index, _freeze(self._evidence), _freeze(self._sources),
            _freeze(self._metadata), _freeze(resolved), _freeze(manifest), bundles)

    def _add_evidence(self, identifier, source, spans, *, rule_id=None):
        spans = deepcopy(spans)
        item = {
            "id": identifier, "title": source["title"],
            "text": "\n\n".join(span["text"] for span in spans),
            "source_url": source["url"], "source_id": source["chunk_id"],
            "parent_citation_id": source["citation_id"], "document_id": source["document_id"],
            "sha256": source["sha256"], "spans": spans,
            "scope_note": source.get("scope_note", ""), "coverage": source["coverage"],
        }
        if rule_id is not None:
            item["rule_id"] = rule_id
        previous = self._evidence.get(identifier)
        if previous is not None and previous != item:
            raise ValueError("GRAPH_POLICY_EVIDENCE_ID_CONFLICT")
        self._evidence[identifier] = item
        self._sources[identifier] = source

    def route_reference(self, origin, destination):
        """Look up a directed source-table value in this same release (D10) with the conditions it is verified under.

        The route block is code's; the conditions are shown in the source drawer. Nothing here decides order
        eligibility or actual reimbursement.
        """
        if self.config is None or self._index is None:
            return {"status": "unconfigured", "source_amount_text": None}
        # The bundles this snapshot was built from, kept instead of rebuilt per turn.
        bundles = self.knowledge.bundles
        if (bundles.index.corpus_sha256 != self._metadata["corpus_sha256"]
                or bundles.index.structure_sha256 != self._metadata["structure_sha256"]
                or bundles.relations_sha256 != self._metadata["relations_sha256"]):
            raise ValueError("GRAPH_ROUTE_RELEASE_BINDING_MISMATCH")
        route = bundles.route_lookup.lookup(origin, destination, max_context_chars=3000)
        if route["status"] != "evidence_found":
            return {"status": route["status"], "source_amount_text": None,
                    "requested_route": route["requested_route"], "warnings": route["warnings"]}
        verified = bundles.build(["shipping_compensation"], route_result=route, max_context_chars=20000)
        if not verified["complete"] or verified["source_amount_text"] != route["source_amount_text"]:
            return {"status": "incomplete_conditions", "source_amount_text": None,
                    "requested_route": route["requested_route"], "warnings": verified["warnings"]}
        condition_cards = self.source_cards(["rule:" + item["rule_id"] for item in verified["backend_evidence"]
                                             if item["rule_id"] != "explicit_directed_route"])
        unit = route["evidence"][0]
        content = "\n\n".join(span["text"] for span in unit["spans"])
        route_card = {"source_id": unit["parent_chunk_id"], "citation_id": "route:" + unit["unit_id"],
            "title": unit["title"], "url": unit["url"], "sha256": unit["parent_sha256"],
            "content": content, "spans": deepcopy(unit["spans"]), "scope_note": unit["scope_note"],
            "parent_citation_id": unit["parent_citation_id"],
            "excerpt_sha256": _sha256(content.encode("utf-8"))}
        return {"status": "evidence_found", "requested_route": route["requested_route"],
                "source_amount_text": route["source_amount_text"],
                "amount_semantics": route["amount_semantics"], "eligibility_evaluated": False,
                "corpus_sha256": route["corpus_sha256"], "structure_sha256": route["structure_sha256"],
                "relations_sha256": verified["relations_sha256"],
                "route_source_card": route_card, "condition_source_cards": condition_cards,
                "warnings": list(dict.fromkeys(route["warnings"] + verified["warnings"]))}

    def source_cards(self, evidence_ids):
        """The source drawer of the evidence a reply shows (a cited card's own evidence ids): each whole passage or
        rule as the release (or the catalogue's upload) has it, in the order given. An unknown id raises ValueError."""
        if type(evidence_ids) is not list or any(type(value) is not str for value in evidence_ids):
            raise ValueError("GRAPH_POLICY_SELECTION_INVALID")
        if any(identifier not in self._evidence for identifier in evidence_ids):
            raise ValueError("GRAPH_POLICY_UNKNOWN_EVIDENCE")
        cards = []
        for identifier in dict.fromkeys(evidence_ids):
            item = _thaw(self._evidence[identifier])
            cards.append({
                "source_id": item["source_id"], "citation_id": identifier,
                "title": item["title"], "url": item["source_url"], "sha256": item["sha256"],
                "content": item["text"], "spans": item["spans"],
                "scope_note": item["scope_note"], "parent_citation_id": item["parent_citation_id"],
                "excerpt_sha256": _sha256(item["text"].encode("utf-8")),
                **({"rule_id": item["rule_id"]} if "rule_id" in item else {}),
            })
        return cards
