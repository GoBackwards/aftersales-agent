"""Synthetic release integrity and publishing tests; no human review or API."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support_agent import knowledge_release as release
from tests.test_public_policy_bundles import bundle_fixture
from tests.test_public_policy_images import save, sha


def release_fixture(root):
    corpus, structure, relations = bundle_fixture(root)
    document = json.loads(corpus.read_text(encoding="utf-8"))["documents"][0]
    document_id = document["document_id"]
    parent = document["chunks"][0]
    ref = {"document_id": document_id, "chunk_id": parent["chunk_id"], "parent_sha256": parent["sha256"],
           "start": 0, "end": len(parent["content"]), "excerpt_sha256": parent["sha256"]}
    bindings = dict(zip(("corpus_sha256", "structure_sha256", "relations_sha256"),
                        (sha(path.read_bytes()) for path in (corpus, structure, relations)), strict=True))
    matrix = {"schema_version": release.MATRIX_SCHEMA, **bindings, "effective_dates_status": "unknown",
              "scopes": [{"scope_id": "test-scope", "document_ids": [document_id],
                          "support_status": "partial_reference", "answer_boundary": "Invented source; no eligibility guarantee",
                          "critical_check_ids": ["test-conditions", "test-amounts"]}],
              "required_checks": [{"check_id": key, "scope_id": "test-scope", "kind": kind,
                                   "document_ids": [document_id], "description": "Compare invented original and exact source",
                                   "evidence_refs": [deepcopy(ref)]}
                                  for key, kind in (("test-conditions", "conditions"), ("test-amounts", "amounts"))]}
    matrix_path = root / "matrix.json"
    matrix_sha = save(matrix_path, matrix)
    reviews = {"schema_version": release.REVIEWS_SCHEMA, **bindings, "matrix_sha256": matrix_sha,
               "records": [{"check_id": check["check_id"], "reviewer_type": "ai", "reviewer_id": "synthetic-ai",
                            "decision": "needs_review", "reviewed_at": "2026-09-20T00:00:00Z",
                            "notes": "Synthetic test only; no human acceptance"} for check in matrix["required_checks"]]}
    reviews_path = root / "reviews.json"
    save(reviews_path, reviews)
    return corpus, structure, relations, matrix_path, reviews_path


class KnowledgeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = release_fixture(self.root)
        self.store = self.root / "store"

    def build(self, label="candidate-a"):
        return release.build_release(self.store, *self.inputs, label=label,
                                     created_at="2026-09-20T01:00:00Z")["release_id"]

    def active(self, key, expected=0, channel="development"):
        return release.activate_release(self.store, key, expected_generation=expected,
                                        channel=channel, reason="Synthetic release test")

    def update_reviews(self, mutator):
        value = json.loads(self.inputs[-1].read_text(encoding="utf-8"))
        mutator(value)
        save(self.inputs[-1], value)

    def update_matrix(self, mutator):
        value = json.loads(self.inputs[-2].read_text(encoding="utf-8"))
        mutator(value)
        digest = save(self.inputs[-2], value)
        self.update_reviews(lambda reviews: reviews.update(matrix_sha256=digest))

    def human_reviews(self):
        # Deliberate synthetic identity used only for testing the declared gate.
        def replace(value):
            for record in value["records"]:
                record.update(reviewer_type="human", reviewer_id="SYNTHETIC-NOT-A-REAL-PERSON",
                              decision="accepted", human_attestation=True,
                              review_method="manual_original_source_comparison",
                              notes="Synthetic gate fixture; no real review occurred")
        self.update_reviews(replace)

    def test_self_contained_hash_addressed_release_preserves_every_referenced_asset(self):
        originals = {path: path.read_bytes() for path in self.inputs}
        key = self.build()
        manifest = release.validate_release(self.store, key)
        directory = self.store / "releases" / key
        self.assertEqual(sha((directory / "manifest.json").read_bytes()), key)
        self.assertEqual(manifest["assets"]["corpus/corpus.json"]["sha256"], sha(self.inputs[0].read_bytes()))
        self.assertTrue(any("image" in name for name in manifest["assets"]))
        self.assertEqual(len(manifest["assets"]), 10)
        self.assertEqual(originals, {path: path.read_bytes() for path in self.inputs})
        self.inputs[0].unlink()
        self.inputs[1].unlink()
        self.assertEqual(release.validate_release(self.store, key), manifest)

    def test_new_release_rejects_same_manifest_overwrite(self):
        key = self.build()
        with self.assertRaisesRegex(ValueError, "ALREADY_EXISTS"):
            self.build()
        self.assertEqual(release.validate_release(self.store, key)["label"], "candidate-a")

    def test_unreferenced_private_files_are_not_copied_into_release(self):
        private = self.inputs[0].parent / "unreferenced-local-config.txt"
        private.write_text("synthetic private placeholder", encoding="utf-8")
        key = self.build()
        directory = self.store / "releases" / key
        self.assertFalse((directory / "corpus" / private.name).exists())
        self.assertNotIn("corpus/" + private.name, release.validate_release(self.store, key)["assets"])

    def test_development_accepts_ai_only_and_reviewed_refuses_it(self):
        key = self.build()
        self.active(key)
        resolved = release.resolve_active_release(self.store)
        self.assertEqual(resolved["release_id"], key)
        self.assertEqual(resolved["review_summary"]["human_records"], 0)
        self.assertFalse(resolved["review_summary"]["current_policy_verified"])
        self.assertIsNone(resolved["effective_from"])
        with self.assertRaisesRegex(ValueError, "HUMAN_REVIEW_REQUIRED"):
            self.active(key, channel="reviewed")
        self.assertFalse((self.store / "active-reviewed.json").exists())

    def test_human_self_declaration_gate_requires_all_checks_but_is_not_identity_proof(self):
        self.human_reviews()
        key = self.build()
        self.active(key, channel="reviewed")
        resolved = release.resolve_active_release(self.store, channel="reviewed")
        self.assertEqual(resolved["review_summary"]["human_accepted_checks"], 2)
        self.assertFalse(resolved["review_summary"]["reviewer_identity_verified"])
        self.assertFalse(resolved["review_summary"]["effective_dates_verified"])

    def test_missing_human_check_never_passes_reviewed_gate(self):
        self.human_reviews()
        self.update_reviews(lambda v: v["records"].pop())
        key = self.build()
        with self.assertRaisesRegex(ValueError, "HUMAN_REVIEW_REQUIRED"):
            self.active(key, channel="reviewed")

    def test_later_rejection_revokes_prior_accepted_review(self):
        self.human_reviews()
        def reject(value):
            record = deepcopy(value["records"][0])
            record.update(decision="rejected", reviewed_at="2026-09-20T00:30:00Z", notes="Found incorrect exception")
            value["records"].append(record)
        self.update_reviews(reject)
        key = self.build()
        self.assertEqual(release.validate_release(self.store, key)["review_summary"]["human_accepted_checks"], 1)
        with self.assertRaisesRegex(ValueError, "HUMAN_REVIEW_REQUIRED"):
            self.active(key, channel="reviewed")

    def test_human_acceptance_without_explicit_attestation_is_invalid(self):
        self.human_reviews()
        self.update_reviews(lambda v: v["records"][0].update(human_attestation=False))
        with self.assertRaisesRegex(ValueError, "HUMAN_ATTESTATION_REQUIRED"):
            self.build()

    def test_ai_cannot_claim_human_attestation(self):
        self.update_reviews(lambda v: v["records"][0].update(human_attestation=True))
        with self.assertRaisesRegex(ValueError, "AI_CANNOT_ATTEST_HUMAN"):
            self.build()

    def test_duplicate_review_timestamp_is_ambiguous_and_rejected(self):
        self.update_reviews(lambda v: v["records"].append(deepcopy(v["records"][0])))
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS_ORDER"):
            self.build()

    def test_foreign_matrix_source_or_review_matrix_binding_is_rejected(self):
        for field, target in (("corpus_sha256", self.inputs[-2]), ("matrix_sha256", self.inputs[-1])):
            original = target.read_bytes()
            value = json.loads(original)
            value[field] = "0" * 64
            save(target, value)
            with self.assertRaisesRegex(ValueError, "BINDING_INVALID"):
                self.build()
            target.write_bytes(original)

    def test_matrix_cannot_omit_critical_check_or_reference_unknown_document(self):
        for mutator in (lambda v: v["required_checks"].pop(),
                        lambda v: v["scopes"][0]["document_ids"].append("unseen-source")):
            originals = [path.read_bytes() for path in self.inputs[-2:]]
            self.update_matrix(mutator)
            with self.assertRaisesRegex(ValueError, "COVERAGE_INVALID"):
                self.build()
            for path, raw in zip(self.inputs[-2:], originals, strict=True):
                path.write_bytes(raw)

    def test_effective_date_cannot_be_invented_by_matrix(self):
        self.update_matrix(lambda v: v.update(effective_dates_status="current"))
        with self.assertRaisesRegex(ValueError, "MATRIX_BINDING_INVALID"):
            self.build()

    def test_matrix_exact_source_refs_cannot_be_missing_or_resealed_to_wrong_spans(self):
        for mutator in (lambda v: v["required_checks"][0].pop("evidence_refs"),
                        lambda v: v["required_checks"][0]["evidence_refs"][0].update(end=1)):
            originals = [path.read_bytes() for path in self.inputs[-2:]]
            self.update_matrix(mutator)
            with self.assertRaisesRegex(ValueError, "SOURCE_REF"):
                self.build()
            for path, raw in zip(self.inputs[-2:], originals, strict=True):
                path.write_bytes(raw)

    def test_review_cannot_be_dated_after_release_creation(self):
        self.update_reviews(lambda v: v["records"][0].update(reviewed_at="2026-09-20T02:00:00Z"))
        with self.assertRaisesRegex(ValueError, "REVIEW_AFTER_CREATION"):
            self.build()

    def test_assets_manifest_and_history_tampering_are_all_refused(self):
        key = self.build()
        pointer = self.active(key)
        directory = self.store / "releases" / key
        paths = [directory / "relations.json", directory / "manifest.json",
                 self.store / "history" / "development" / f"{pointer['event_sha256']}.json"]
        for path in paths:
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
                release.resolve_active_release(self.store)
            path.write_bytes(original)

    def test_resealed_manifest_cannot_escape_release_directory(self):
        key = self.build()
        directory = self.store / "releases" / key
        manifest = json.loads((directory / "manifest.json").read_bytes())
        manifest["assets"]["../outside.json"] = {"path": "../outside.json", "sha256": "0" * 64, "bytes": 1}
        raw = release._bytes(manifest)
        new_key = sha(raw)
        (directory / "manifest.json").write_bytes(raw)
        directory.rename(directory.parent / new_key)
        with self.assertRaisesRegex(ValueError, "PATH_INVALID"):
            release.validate_release(self.store, new_key)

    def test_invalid_release_id_and_channel_are_not_treated_as_paths(self):
        key = self.build()
        for invalid in ("../candidate", "f" * 63, "X" * 64):
            with self.assertRaises(ValueError):
                release.validate_release(self.store, invalid)
        with self.assertRaisesRegex(ValueError, "CHANNEL_INVALID"):
            self.active(key, channel="../reviewed")

    def test_stale_compare_and_swap_does_not_change_active_or_history(self):
        first, second = self.build(), self.build("candidate-b")
        self.active(first)
        with self.assertRaisesRegex(ValueError, "CONCURRENT_UPDATE"):
            self.active(second, expected=0)
        self.assertEqual(release.resolve_active_release(self.store)["release_id"], first)
        self.assertEqual(len(release.release_history(self.store)), 1)

    def test_concurrent_publishers_cannot_both_consume_same_generation(self):
        first, second, third = self.build(), self.build("candidate-b"), self.build("candidate-c")
        self.active(first)
        def attempt(key):
            try:
                return self.active(key, expected=1)
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, (second, third)))
        self.assertEqual(sum(isinstance(row, dict) for row in outcomes), 1)
        self.assertEqual(release.resolve_active_release(self.store)["generation"], 2)
        self.assertEqual(len(release.release_history(self.store)), 2)

    def test_existing_lock_is_not_silently_broken(self):
        key = self.build()
        (self.store / ".release.lock").write_text("unverified existing writer", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "STORE_LOCKED"):
            self.active(key)
        self.assertTrue((self.store / ".release.lock").exists())

    def test_failed_pointer_replace_preserves_active_and_ignores_orphan_history(self):
        first, second = self.build(), self.build("candidate-b")
        self.active(first)
        with patch.object(release.os, "replace", side_effect=OSError("simulated interruption")):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                self.active(second, expected=1)
        self.assertEqual(release.resolve_active_release(self.store)["release_id"], first)
        self.assertEqual(len(release.release_history(self.store)), 1)
        self.assertFalse((self.store / ".release.lock").exists())
        self.assertFalse(list(self.store.glob(".active-*.tmp")))
        self.active(second, expected=1)
        self.assertEqual(len(release.release_history(self.store)), 2)

    def test_channels_have_independent_generations_and_histories(self):
        first = self.build()
        self.human_reviews()
        second = self.build("reviewed-fixture")
        self.active(first)
        self.active(second, channel="reviewed")
        self.assertEqual(release.resolve_active_release(self.store)["release_id"], first)
        self.assertEqual(release.resolve_active_release(self.store, channel="reviewed")["release_id"], second)

    def test_changed_source_during_build_never_publishes_partial_release(self):
        original_read = release._read_file
        calls = 0
        def read(path):
            nonlocal calls
            raw = original_read(path)
            if path == self.inputs[-1]:
                calls += 1
                if calls == 3:
                    return raw + b" "
            return raw
        with patch.object(release, "_read_file", side_effect=read):
            with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
                self.build()
        self.assertEqual(list((self.store / "releases").iterdir()), [])

    def test_symlinked_release_asset_is_rejected_when_platform_allows_creation(self):
        key = self.build()
        path = self.store / "releases" / key / "relations.json"
        target = self.root / "linked-relations.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation privilege unavailable")
        with self.assertRaises(ValueError):
            release.validate_release(self.store, key)


if __name__ == "__main__":
    unittest.main()
