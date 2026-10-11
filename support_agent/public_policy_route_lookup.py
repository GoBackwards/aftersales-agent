"""Explicit directed-route lookup over validated source-table records.

No natural-language extraction, city-to-province mapping, reverse-route fallback,
or order eligibility decision runs here. A reusable instance retains one
validated immutable in-memory snapshot for multiple lookups.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal

from .public_policy import evidence_chars, image_warnings, _string
from .public_policy_structure import StructureIndex


SCHEMA = "public-policy-route-lookup-v1"


class PolicyRouteLookup:
    def __init__(self, corpus_path, structure_path):
        self.index = StructureIndex(corpus_path, structure_path)
        self._routes = defaultdict(list)
        self._endpoints = set()
        for unit in self.index.units:
            if unit["kind"] != "route_record":
                continue
            route = unit["route"]
            self._routes[(route["origin"], route["destination"])].append(unit)
            self._endpoints.update((route["origin"], route["destination"]))

    def lookup(self, origin, destination, *, merchant="jd_self_operated", region="CN", max_context_chars=3000):
        origin = _string(origin, limit=100)
        destination = _string(destination, limit=100)
        source = self.index.source_index
        # Reuse every existing argument check and conservative scope marker.
        # This string is only for the scope guard: endpoints are never inferred
        # from it, and its lexical evidence is not used to fill missing routes.
        guard = source.search(origin + "\n" + destination, top_k=1,
                              max_context_chars=max_context_chars, merchant=merchant, region=region)
        result = {
            "schema_version": SCHEMA, "status": "no_evidence", "read_only": True, "writes_allowed": False,
            "result_kind": "source_table_lookup_not_order_decision", "source_kind": "official_public_web",
            "corpus_sha256": self.index.corpus_sha256, "structure_sha256": self.index.structure_sha256,
            "captured_at": source.captured_at, "scope": source.scope,
            "requested_scope": {"merchant": merchant, "region": region},
            "requested_route": {"origin": origin, "destination": destination},
            "eligibility_evaluated": False, "amount_semantics": "source_table_value_not_order_payout",
            "source_amount_text": None, "matching_record_count": 0, "duplicate_record_count": 0,
            "matched_unit_ids": [], "unsupported_endpoints": [], "evidence": [],
            "max_context_chars": max_context_chars, "context_chars": 2,
            "context_budget_unit": "complete_evidence_compact_json_characters",
            "representative_rule": "first_source_occurrence_when_all_amount_values_agree",
            "human_review": "pending", "effective_dates_verified": False,
            "warnings": ["effective_dates_unverified", "source_policy_human_review_pending",
                         "snapshot_not_current_policy_guarantee", "local_integrity_not_independent_authentication"],
            "network_requests": 0, "model_api_calls": 0,
        }
        if guard["status"] in {"scope_mismatch", "scope_warning"}:
            result.update(status="out_of_scope", scope_guard_status=guard["status"])
            result["warnings"].extend(guard["warnings"])
            result["warnings"] = list(dict.fromkeys(result["warnings"]))
            return result
        result["unsupported_endpoints"] = [
            field for field, value in (("origin", origin), ("destination", destination))
            if value not in self._endpoints
        ]
        if result["unsupported_endpoints"]:
            result["status"] = "unsupported_route_endpoint"
            result["warnings"].append("endpoints_require_exact_source_labels_no_city_or_language_mapping")
            return result
        matches = self._routes.get((origin, destination), [])
        if not matches:
            result["status"] = "route_not_listed"
            result["warnings"].append("unlisted_route_is_not_ineligibility_or_zero_compensation")
            return result
        amounts = {Decimal(unit["route"]["amount_text"]) for unit in matches}
        result.update(matching_record_count=len(matches), duplicate_record_count=len(matches) - len(amounts),
                      matched_unit_ids=[unit["unit_id"] for unit in matches])
        if len(amounts) != 1:
            result["status"] = "conflicting_route_records"
            result["warnings"].append("conflicting_source_amounts_require_review")
            return result
        representative = self.index.validate_evidence(matches[0])
        evidence = [{**representative, "score": 0}]
        context_chars = evidence_chars(evidence)
        if context_chars > max_context_chars:
            result["status"] = "budget_exhausted"
            result["warnings"].append("whole_route_unit_omitted_for_context_budget")
            return result
        result.update(status="evidence_found", source_amount_text=representative["route"]["amount_text"],
                      evidence=evidence, context_chars=context_chars)
        result["warnings"].append("listed_route_amount_does_not_establish_order_eligibility")
        result["warnings"].extend(image_warnings(evidence))
        return deepcopy(result)
