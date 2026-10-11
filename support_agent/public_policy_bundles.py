"""Explicit policy relationships and exact excerpts for read-only references.

Relationships are authored policy selections, not query labels or a semantic
completeness proof. Full source evidence stays in the backend; the separate
compact view has its own character budget and is not phase G's JSON metric.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .public_policy import _digest, _fields, _read_file, _sha256, _string, evidence_chars, image_warnings
from .public_policy_route_lookup import PolicyRouteLookup
from .public_policy_structure import _canonical, _json, _span


SCHEMA = "public-policy-evidence-bundle-v1"
RELATIONS_SCHEMA = "public-policy-relations-v1"
TOPICS = {"shipping_compensation", "shipping_responsibility", "return_gifts"}


class PolicyEvidenceBundles:
    def __init__(self, corpus_path, structure_path, relations_path):
        self.route_lookup = PolicyRouteLookup(corpus_path, structure_path)
        self.index = self.route_lookup.index
        raw = _read_file(Path(relations_path).absolute())
        manifest = _json(raw)
        _fields(manifest, {"schema_version", "corpus_sha256", "authored_by", "human_reviewed",
                           "selection_method", "limitations", "topics", "rules"})
        if (manifest["schema_version"] != RELATIONS_SCHEMA
                or manifest["corpus_sha256"] != self.index.corpus_sha256
                or manifest["human_reviewed"] is not False
                or type(manifest["rules"]) is not list or not manifest["rules"]
                or type(manifest["topics"]) is not dict or set(manifest["topics"]) != TOPICS):
            raise ValueError("BUNDLE_RELATIONS_BINDING_INVALID")
        _string(manifest["authored_by"], limit=100)
        _string(manifest["selection_method"], limit=500)
        if type(manifest["limitations"]) is not list:
            raise ValueError("BUNDLE_RELATIONS_LIMITATIONS_INVALID")
        for limitation in manifest["limitations"]:
            _string(limitation, limit=1000)
        documents = {doc["document_id"]: doc for doc in self.index.source_index.documents}
        self._rules = {}
        for rule in manifest["rules"]:
            _fields(rule, {"rule_id", "document_id", "chunk_id", "parent_sha256", "spans", "purpose"})
            rule_id = _string(rule["rule_id"], limit=100)
            _string(rule["document_id"], limit=128)
            _string(rule["chunk_id"], limit=128)
            _digest(rule["parent_sha256"])
            _string(rule["purpose"], limit=500)
            if rule_id in self._rules or type(rule["spans"]) is not list or not rule["spans"]:
                raise ValueError("BUNDLE_RELATION_RULE_INVALID")
            document = documents.get(rule["document_id"])
            if document is None:
                raise ValueError("BUNDLE_RELATION_SOURCE_MISSING")
            parent = next((p for p in document["chunks"] if p["chunk_id"] == rule["chunk_id"]), None)
            if parent is None or parent["sha256"] != rule["parent_sha256"]:
                raise ValueError("BUNDLE_RELATION_PARENT_MISMATCH")
            selected, previous_end = [], -1
            for spec in rule["spans"]:
                _fields(spec, {"start", "end", "sha256"})
                start, end = spec["start"], spec["end"]
                if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(parent["content"])
                        or start < previous_end):
                    raise ValueError("BUNDLE_RELATION_SPAN_INVALID")
                span = _span(parent, "policy_condition", start, end)
                if span["sha256"] != spec["sha256"]:
                    raise ValueError("BUNDLE_RELATION_SPAN_MISMATCH")
                selected.append(span)
                previous_end = end
            citation_id = f"{document['document_id']}@{document['text_sha256'][:12]}#{parent['chunk_id']}"
            self._rules[rule_id] = {"rule_id": rule_id,
                                    "source": self.index.source_index.evidence_by_id(citation_id),
                                    "parent_start": parent["start"], "parent_end": parent["end"], "spans": selected}
        for rule_ids in manifest["topics"].values():
            if (type(rule_ids) is not list or not rule_ids or any(type(r) is not str for r in rule_ids)
                    or len(set(rule_ids)) != len(rule_ids) or not set(rule_ids) <= self._rules.keys()):
                raise ValueError("BUNDLE_TOPIC_RULES_INVALID")
        if not set(manifest["topics"]["shipping_responsibility"]) <= set(manifest["topics"]["shipping_compensation"]):
            raise ValueError("BUNDLE_COMPENSATION_REQUIRES_RESPONSIBILITY_RULES")
        if set(self._rules) != set(r for ids in manifest["topics"].values() for r in ids):
            raise ValueError("BUNDLE_UNUSED_RELATION_RULE")
        self.relations_sha256 = _sha256(raw)
        self._manifest = deepcopy(manifest)

    def build(self, topics, *, route_result=None, merchant="jd_self_operated", region="CN", max_context_chars=3000):
        if (type(topics) is not list or any(type(topic) is not str or topic not in TOPICS for topic in topics)
                or len(set(topics)) != len(topics) or (not topics and route_result is None)):
            raise ValueError("BUNDLE_TOPICS_INVALID")
        if type(max_context_chars) is not int or not 2 <= max_context_chars <= 20000:
            raise ValueError("BUNDLE_CONTEXT_BUDGET_INVALID")
        _string(merchant, limit=100)
        _string(region, limit=100)
        requested = list(topics)
        if route_result is not None and "shipping_compensation" not in requested:
            requested.append("shipping_compensation")
        result = {
            "schema_version": SCHEMA, "status": "no_evidence", "complete": False,
            "read_only": True, "writes_allowed": False, "requested_topics": requested,
            "corpus_sha256": self.index.corpus_sha256, "structure_sha256": self.index.structure_sha256,
            "relations_sha256": self.relations_sha256,
            "requested_scope": {"merchant": merchant, "region": region},
            "captured_at": self.index.source_index.captured_at,
            "backend_evidence": [], "model_view": [], "context_chars": 2,
            "max_context_chars": max_context_chars,
            "context_budget_unit": "compact_model_view_json_characters_v1",
            "model_view_role": "available_for_rendering_or_future_model_input",
            "completeness_scope": "all_authored_relationship_requirements_not_semantic_completeness",
            "missing_requirements": [], "eligibility_evaluated": False,
            "source_amount_text": None, "amount_semantics": "source_table_value_not_order_payout",
            "requested_route": None, "route_status": None, "route_verification_calls": 0,
            "human_review": "pending", "effective_dates_verified": False,
            "warnings": ["policy_relationships_ai_authored_not_human_reviewed", "effective_dates_unverified",
                         "snapshot_not_current_policy_guarantee", "local_integrity_not_independent_authentication",
                         "no_order_eligibility_decision", "compact_view_budget_differs_from_phase7g_full_evidence"],
            "network_requests": 0, "model_api_calls": 0,
        }
        scope = self.index.source_index.scope
        if merchant != scope["merchant"] or region != scope["region"]:
            result["status"] = "out_of_scope"
            return result
        if "shipping_compensation" in requested:
            result["warnings"].append("free_pickup_and_nonbenefit_compensation_are_alternative_branches")
        verified_route = None
        if route_result is not None:
            if type(route_result) is not dict or type(route_result.get("requested_route")) is not dict:
                raise ValueError("BUNDLE_ROUTE_RESULT_INVALID")
            route = route_result["requested_route"]
            _fields(route, {"origin", "destination"})
            budget = route_result.get("max_context_chars")
            if type(budget) is not int:
                raise ValueError("BUNDLE_ROUTE_RESULT_INVALID")
            verified_route = self.route_lookup.lookup(route["origin"], route["destination"],
                                                     merchant=merchant, region=region, max_context_chars=budget)
            if _canonical(route_result) != _canonical(verified_route):
                raise ValueError("BUNDLE_ROUTE_RESULT_MISMATCH")
            result.update(route_status=verified_route["status"], requested_route=deepcopy(route), route_verification_calls=1)
            result["warnings"].extend(verified_route["warnings"])
        required = list(dict.fromkeys(rule for topic in requested for rule in self._manifest["topics"][topic]))
        backend = [{"evidence_id": f"P{number}", **deepcopy(self._rules[rule_id])}
                   for number, rule_id in enumerate(required, start=1)]
        if verified_route is not None and verified_route["status"] == "evidence_found":
            unit = deepcopy(verified_route["evidence"][0])
            parent = next(parent for doc in self.index.source_index.documents
                          if doc["document_id"] == unit["document_id"] for parent in doc["chunks"]
                          if parent["chunk_id"] == unit["parent_chunk_id"])
            backend.append({"evidence_id": "R1", "rule_id": "explicit_directed_route",
                            "source": unit, "parent_start": parent["start"], "parent_end": parent["end"],
                            "spans": deepcopy(unit["spans"])})
        view = [{"id": item["evidence_id"], "title": item["source"]["title"],
                 "spans": [{"start": span["start"], "end": span["end"], "text": span["text"]}
                           for span in item["spans"]]} for item in backend]
        size = evidence_chars(view)
        result["backend_evidence"] = backend
        result["required_context_chars"] = size
        result["warnings"].extend(image_warnings([item["source"] for item in backend]))
        result["warnings"] = list(dict.fromkeys(result["warnings"]))
        if size > max_context_chars:
            result.update(status="budget_exhausted", missing_requirements=required)
            if verified_route is not None:
                result["missing_requirements"].append("explicit_directed_route")
            result["warnings"].append("mandatory_conditions_not_partially_dropped_to_fit_budget")
            return deepcopy(result)
        result.update(status="evidence_ready", complete=True, model_view=view, context_chars=size)
        if verified_route is not None:
            if verified_route["status"] == "evidence_found":
                result["source_amount_text"] = verified_route["source_amount_text"]
            else:
                result.update(status="route_unavailable", complete=False,
                              missing_requirements=["explicit_directed_route"])
        return deepcopy(result)
